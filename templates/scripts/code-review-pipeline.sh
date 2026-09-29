#!/bin/bash
# Code Review v2: multi-specialist review (correctness + security + performance)
set -euo pipefail

unset CLAUDECODE 2>/dev/null || true

REPO_DIR="$(pwd)"
SCRIPT_REPO="$(cd "$(dirname "$0")/.." && pwd)"
source "$(dirname "$0")/bureau-config.sh"

# agents.merge_mode = manual needs a Merge state: an approved ticket is parked
# there for the human who merges. Without one there is nowhere to put it, so
# refuse now — before .env, Linear, gh, git or a paid review. 24: the repo is
# not set up for the mode it asks for (bureau-doctor.py reports it too).
if merge_mode_lacks_merge_state; then
  echo "code-review-pipeline: agents.merge_mode is manual but linear.teams[0].states.merge is not set — configure the Merge state or set merge_mode to auto." >&2
  exit 24
fi

BUREAU_ENV_FILE="${BUREAU_ENV_FILE:-$SCRIPT_REPO/.env}"
if [ -f .env ]; then bureau_load_env --export .env
elif [ -f "$BUREAU_ENV_FILE" ]; then bureau_load_env --export "$BUREAU_ENV_FILE"
else [ -n "${LINEAR_API_KEY:-}" ] || { echo "ERROR: Set LINEAR_API_KEY"; exit 1; }; fi

# Honor BUREAU_MODEL_CODE_REVIEW / .agents.code_review.model like every other
# pipeline (EXP-490). Without this, code review silently ignored the per-stage
# model knob and stuck to the CLI default — making it ineligible for the
# cheap-model migration the per-stage map was designed for.
CLAUDE=(run_stage_for code_review)
API_KEY="${LINEAR_API_KEY:?Set LINEAR_API_KEY in .env}"
REVIEW_TMP=$(mktemp -d)
# Preserve REVIEW_TMP only on real failures. 0 = success, 2 = queue-empty —
# both are clean early exits with no specialist output to inspect.
_review_cleanup() {
  local rc=$?
  if [ "$rc" = 0 ] || [ "$rc" = 2 ]; then
    rm -rf "$REVIEW_TMP"
  else
    echo "code-review failed (exit $rc). Specialist outputs preserved at $REVIEW_TMP" >&2
  fi
}
trap _review_cleanup EXIT

# review_text_from_merger <merger output>: the text the PR comment and the ticket get.
# The merger answers with the object of bureau-review.schema.json, whose "comment" is
# its markdown for humans (summaries, findings with file:line, fixes needed): that
# comment, then the rest of the object as a fenced JSON verdict. A verdict block over
# 16 KB drops its findings list and says how many it held (the comment names them),
# so the block always survives the comment size cap. An answer without a verdict or
# without a comment string (an older schema, a fixture) is printed unchanged: the
# comment's lines never reach the text-verdict fallback, which reads line starts and
# finds none inside a JSON string. A cost-tracking envelope is unwrapped as
# parse_claude_json does. Decides nothing: the verdict is read from the merger's
# answer itself.
review_text_from_merger() {
  local text
  if text=$(printf '%s' "$1" | jq -r '
      (if type == "object" and (.result | type) == "string" then (.result | fromjson?) else . end)
      | select(type == "object" and (.comment | type) == "string" and (.verdict | type) == "string")
      | (del(.comment)) as $v
      | (if ($v | tojson | length) > 16000
         then ($v | del(.findings)) + {findings_omitted: ($v.findings | length? // 0)} else $v end) as $v
      | .comment + "\n\n```json\n" + ($v | tojson) + "\n```"' 2>/dev/null) && [ -n "$text" ]; then
    printf '%s' "$text"
  else
    printf '%s' "$1"
  fi
}

# _review_release_worktree: after an APPROVE whose inline merge did not go through, put
# the disposable worktree back to the reviewed head. The stage then ends with 2 or 25,
# and bureau-worker.sh keeps a worktree that is ahead of origin (the local validation
# merge) or dirty (build-check output) as unfinished work and refuses to reset it, so
# the next run of the gate could not start. Review work is never unpublished: the
# verdict is on the PR and the ticket.
_review_release_worktree() {
  [ "${BUREAU_WORKSPACE_MODE:-}" = disposable ] || return 0
  git reset --hard --quiet "$REVIEW_HEAD" && git clean -fd --quiet \
    || echo "  WARN: could not reset the review worktree to $REVIEW_HEAD; the worker may keep it" >&2
}

precondition_linear
precondition_runner code_review

if [ -n "${1:-}" ]; then
  ISSUE="$1"
  echo "Using specified issue: $ISSUE"
else
  echo "Picking next Build Review issue..."
  ISSUE=$(pipeline_pick_next "$(basename "$0")")

  if [ -z "$ISSUE" ] || [[ ! "$ISSUE" =~ ^[A-Z]+-[0-9]+$ ]]; then
    echo "No qualifying issues found. Queue empty."
    exit 2
  fi
  echo "Picked: $ISSUE"
fi

# State guard runs unconditionally — see implement-pipeline.sh for rationale.
bureau_stage_enter "$ISSUE" "$@"

ACTUAL_STATE=$(get_issue_state "$ISSUE")
if [ "$ACTUAL_STATE" != "Build Review" ]; then
  echo "  WARNING: $ISSUE is in '$ACTUAL_STATE', not 'Build Review'. Skipping."
  exit 2
fi

echo ""
echo "═══════════════════════════════════════"
echo "  Code Review v2 Pipeline: $ISSUE"
echo "═══════════════════════════════════════"
echo ""

echo "→ Fetching issue details..."
ISSUE_DETAIL=$(get_issue_detail "$ISSUE")
ISSUE_TITLE=$(echo "$ISSUE_DETAIL" | jq -r '.title // empty')
ISSUE_DESC=$(echo "$ISSUE_DETAIL" | jq -r '.description // empty')
echo "  $ISSUE: $ISSUE_TITLE"

echo "→ Finding branch and PR..."
BRANCH=$(get_issue_branch "$ISSUE")

if [ -z "$BRANCH" ] || [[ "$BRANCH" == -* ]] || ! git check-ref-format "refs/heads/$BRANCH" >/dev/null 2>&1; then
  echo "  ERROR: no bureau-branch marker found for $ISSUE."
  post_comment "$ISSUE" "❌ Code review aborted — no bureau-branch marker. Moving back to Build."
  move_issue "$ISSUE" "$BUREAU_STATE_BUILD"
  exit 12
fi
echo "  Branch: $BRANCH"

# Recheck under the worker's issue lease: another tick may have selected this
# ticket just before the previous reviewer saved its stop and released ownership.
if bureau_stop_requested; then
  REVIEW_STOP=$(printf '%s' "$ISSUE_DETAIL" | python3 "$SCRIPT_REPO/scripts/bureau-supervision.py" --repo "$PWD" check "$ISSUE" \
    --branch "$BRANCH" --state "$ACTUAL_STATE") || exit 18
  if [ "$(printf '%s' "$REVIEW_STOP" | jq -r .stopped)" = true ]; then
    echo "Review already approved at the unchanged head; still stopped before merge."
    exit 20
  fi
fi

PR_NUMBER=$(gh pr list --head "$BRANCH" --json number --jq '.[0].number' 2>/dev/null || echo "")
if ! [[ "$PR_NUMBER" =~ ^[1-9][0-9]*$ ]]; then
  echo "  ERROR: no PR found for branch $BRANCH"
  post_comment "$ISSUE" "❌ Code review aborted — no open PR for branch \`$BRANCH\`. Moving back to Build."
  move_issue "$ISSUE" "$BUREAU_STATE_BUILD"
  exit 15
fi

PR_URL=$(gh pr view "$PR_NUMBER" --json url --jq '.url')
echo "  PR: #$PR_NUMBER ($PR_URL)"

PR_STATE=$(gh pr view "$PR_NUMBER" --json state --jq '.state' 2>/dev/null || echo "UNKNOWN")
if [ "$PR_STATE" = "MERGED" ]; then
  echo "  PR already merged — moving to Done."
  post_comment "$ISSUE" "✅ PR #$PR_NUMBER already merged. Moving to Done."
  move_issue "$ISSUE" "$BUREAU_STATE_DONE"
  exit 0
fi
if [ "$PR_STATE" != OPEN ]; then
  echo "  ERROR: PR #$PR_NUMBER is not confirmed open."
  exit 15
fi

# Read the target from this PR, not the repository's default branch. Fetch both
# branch tips explicitly, then use immutable commits throughout the review.
PR_REFS=$(gh pr view "$PR_NUMBER" --json baseRefName,headRefName) || exit 18
PR_BASE_REF=$(printf '%s' "$PR_REFS" | jq -er --arg branch "$BRANCH" \
  'select(.headRefName == $branch) | .baseRefName | select(type == "string" and length > 0)') || {
  echo "  ERROR: cannot resolve the matching PR head and base branch."
  exit 18
}
if [[ "$PR_BASE_REF" == -* ]] || ! git check-ref-format "refs/heads/$PR_BASE_REF" >/dev/null 2>&1; then
  echo "  ERROR: PR base is not a safe branch ref."
  exit 18
fi
git fetch --no-tags origin \
  "+refs/heads/$BRANCH:refs/remotes/origin/$BRANCH" \
  "+refs/heads/$PR_BASE_REF:refs/remotes/origin/$PR_BASE_REF" || exit 18
REVIEW_HEAD=$(git rev-parse --verify "refs/remotes/origin/$BRANCH^{commit}") || exit 18
REVIEW_BASE=$(git rev-parse --verify "refs/remotes/origin/$PR_BASE_REF^{commit}") || exit 18
REVIEW_DIFF="$REVIEW_BASE...$REVIEW_HEAD"
# Refuse another checkout's ownership before attaching this registered worker.
free_branch_from_other_worktrees "$BRANCH" "$(pwd)"
git checkout -B "$BRANCH" "$REVIEW_HEAD"

# Review may merge its target locally for the build check. The durable boundary
# tracks the two fetched remote inputs, not that unpublished local merge SHA.
if ! merge_origin_main_or_abort "$ISSUE" "Code Review" "$REVIEW_BASE"; then
  move_issue "$ISSUE" "$BUREAU_STATE_BUILD"
  exit 17
fi

# After the checkout AND the merge of origin/main — both can change the
# manifests. Idempotent: does nothing when node_modules already matches. A
# failure here is infrastructure (registry, disk), not the PR: stop with 24
# (environment-blocked) instead of building red and judging someone's code.
restore_worktree_deps "$(pwd)" || exit 24

FILES_CHANGED=$(git diff --name-only "$REVIEW_DIFF" --) || exit 18
FILES_COUNT=$(echo "$FILES_CHANGED" | grep -c . || true)
# shortstat: " 3 files changed, 42 insertions(+), 7 deletions(-)"
DIFF_SHORTSTAT=$(git diff --shortstat "$REVIEW_DIFF" -- | sed 's/^[[:space:]]*//') || exit 18
DIFF_TOTAL=$(echo "$DIFF_SHORTSTAT" | grep -oE '[0-9]+[[:space:]]*insertion|[0-9]+[[:space:]]*deletion' | awk '{s+=$1} END{print s+0}')
DIFF_STATS="${DIFF_SHORTSTAT:-no diff} (~${DIFF_TOTAL:-0} line changes)"
echo "  Files changed: $FILES_COUNT | $DIFF_STATS"

SPEC_DIR=$(bureau_spec_dir_for_branch "$BRANCH")

# Assemble the authoritative context for all three specialists. Every reviewer
# reads the same grounding so SPEC-override findings get classified SKIP
# instead of being re-reported every cycle (this was the round-after-round
# regression we kept hand-triaging).
SPEC_CONTEXT="SCOPE DISCIPLINE — READ BEFORE FLAGGING:"
[ -f "SPEC.md" ]                    && SPEC_CONTEXT+=$'\n- SPEC.md (repo root) is the project-level source of truth — read it.'
[ -f "CLAUDE.md" ]                  && SPEC_CONTEXT+=$'\n- CLAUDE.md (repo root) captures project conventions and non-goals — read it.'
[ -n "$SPEC_DIR" ] && [ -f "$SPEC_DIR/spec.md" ]     && SPEC_CONTEXT+=$'\n- '"$SPEC_DIR"$'spec.md — per-ticket requirements and acceptance criteria.'
[ -n "$SPEC_DIR" ] && [ -f "$SPEC_DIR/plan.md" ]     && SPEC_CONTEXT+=$'\n- '"$SPEC_DIR"$'plan.md — pinned technical decisions (stack, layout, etc.) for this ticket.'
[ -n "$SPEC_DIR" ] && [ -f "$SPEC_DIR/research.md" ] && SPEC_CONTEXT+=$'\n- '"$SPEC_DIR"$'research.md — rationale for the decisions in plan.md.'
[ -n "$SPEC_DIR" ] && [ -f "$SPEC_DIR/tasks.md" ]    && SPEC_CONTEXT+=$'\n- '"$SPEC_DIR"$'tasks.md — per-task breakdown.'
SPEC_CONTEXT+=$'\n\nA finding that contradicts a pinned decision in any of the above is NOT a bug — classify it SKIP and cite the source in the finding ("skipped: SPEC.md §Stack pins Go 1.22"). Do NOT repeat a SKIP across review cycles — if a prior triage comment on this ticket already declined or deferred a finding, do not resurface it. Pinned decisions win over general best-practice; the user has already weighed the tradeoff.\n\nClassify: CRITICAL (security / data loss / silent corruption) / BUG (real defect, not a spec disagreement) / MINOR (style, micro-opt, defensible nit) / SKIP (contradicts a pinned decision — include citation).'

# logs→memory: include human-curated LESSONS.md if present. Empty when absent.
LESSONS_CONTEXT=$(build_lessons_context)

echo ""
echo "Phase 1/3: multi-specialist review (3 passes in parallel)"

# Pre-compute the cycle number so specialists know whether they're seeing this
# code for the first time or the Nth. They can reference cycle 1's findings
# explicitly in their prose; the merger uses it for escalation.
# The comments are read first and the count must be a number: a failed read used to
# become cycle 0 (`|| echo "0"`), which switched the loop breaker off exactly when Linear
# was unreliable. The stage ends before any paid review: 10 (Linear down) and 27 (Linear
# unusable) pass through; any other failure is an answer that could not be parsed, 27.
_comments_rc=0
_review_comments=$(get_issue_comments "$ISSUE") || _comments_rc=$?
if [ "$_comments_rc" != 0 ]; then
  echo "  ERROR: could not read the comments of $ISSUE to count review cycles (exit $_comments_rc)." >&2
  case "$_comments_rc" in 10|"$BUREAU_EXIT_LINEAR_UNUSABLE") exit "$_comments_rc" ;; *) exit "$BUREAU_EXIT_LINEAR_UNUSABLE" ;; esac
fi
REVIEW_CYCLE_COUNT=$(printf '%s' "$_review_comments" \
  | jq '[.[] | select(.body | test("Code Review.*Changes Requested"))] | length' 2>/dev/null) || REVIEW_CYCLE_COUNT=""
if ! [[ "$REVIEW_CYCLE_COUNT" =~ ^[0-9]+$ ]]; then
  echo "  ERROR: the comments of $ISSUE did not give a review cycle count." >&2
  exit "$BUREAU_EXIT_LINEAR_UNUSABLE"
fi
CYCLE_NOTE="This is review cycle $((REVIEW_CYCLE_COUNT + 1)) for this issue."
if [ "${REVIEW_CYCLE_COUNT:-0}" -gt 0 ]; then
  CYCLE_NOTE+=$'\nDeclined findings from earlier cycles are pinned — do NOT resurface them unless the underlying code has materially changed. Cite the prior cycle if you do re-raise.'
fi

# A review that stopped before merge (`--no-merge`, BUREAU_STOP_REQUESTED) recorded its
# APPROVE with the inputs it judged. Run again without a stop on exactly those inputs —
# the same PR and base branch, the same head and base commits, the same ticket text and
# state — that approval is reused instead of paying the three specialists and the merger
# again. `bureau-supervision.py reuse` removes the record whether it matches or not, so an
# approval is used at most once and never for inputs it did not see; any doubt (no record,
# a record without a recorded verdict, an unreadable file) means the full review. The
# build check in Phase 2 still runs: it is cheap next to the model review and the
# environment may have changed since, and a red build folds the reused APPROVE exactly
# as it folds a fresh one (decide_review_verdict).
#
# The same record is written when the inline merge after an APPROVE found its gate not
# yet decided (merge_gate_wait, see the APPROVE branch below). Reusing such a record
# posts no new comment on the ticket or the PR while the verdict stays APPROVE: the PR
# already carries the APPROVE of this head, which the merge gate reads, and a gate that
# is polled must not add two comments per poll.
REUSED_APPROVAL=0
REUSED_GATE_WAIT=0
STAGE_EXIT=""   # set when an APPROVE's inline merge did not go through (2 or 25)
if ! bureau_stop_requested && [ "${BUREAU_DRY_RUN:-0}" != 1 ]; then
  if REUSE=$(printf '%s' "$ISSUE_DETAIL" | python3 "$SCRIPT_REPO/scripts/bureau-supervision.py" --repo "$PWD" reuse "$ISSUE" \
      --branch "$BRANCH" --state "$ACTUAL_STATE" --head "$REVIEW_HEAD" --base "$REVIEW_BASE" \
      --base-ref "$PR_BASE_REF" --pr "$PR_NUMBER"); then
    if [ "$(printf '%s' "$REUSE" | jq -r '.reuse' 2>/dev/null)" = true ]; then
      REUSED_APPROVAL=1
      REUSED_AT=$(printf '%s' "$REUSE" | jq -r '.stopped_at | floor | todate' 2>/dev/null) || REUSED_AT=""
      [ "$(printf '%s' "$REUSE" | jq -r '.merge_gate_wait' 2>/dev/null)" = true ] && REUSED_GATE_WAIT=1
      echo "  Reusing the approval recorded ${REUSED_AT:-earlier} for head $REVIEW_HEAD — no new specialist review."
      if [ "$REUSED_GATE_WAIT" = 1 ]; then
        echo "  (recorded while the merge gate was not yet decided; the gate runs again)"
      else
        post_comment "$ISSUE" "♻️ Code review: reusing the approval recorded ${REUSED_AT:-earlier} for head \`$REVIEW_HEAD\` on \`$PR_BASE_REF\` at \`$REVIEW_BASE\` — PR #$PR_NUMBER, both commits and the ticket are unchanged, so no new model review runs. The build check runs again." || true
      fi
    else
      echo "  No reusable approval: $(printf '%s' "$REUSE" | jq -r '.reason // "unknown"' 2>/dev/null)"
    fi
  else
    # Only a failure to read or lock review-stops.json itself ends up here (the error
    # is printed above); a mismatch or an unreadable ticket detail is a normal "no".
    echo "  WARN: the review boundary file could not be checked; running a full review." >&2
  fi
fi

if [ "$REUSED_APPROVAL" = 1 ]; then
  # The merger's answer the recorded approval stands for. The recorded APPROVE came out
  # of decide_review_verdict, so the merger's security count was a readable 0 (a missing,
  # unreadable or positive count ends REQUEST_CHANGES or BLOCK) and the specialist's
  # CRITICAL count was not above 0 (it may have been unreadable, which is only noted in
  # the review). 0 for both reproduces that approval without repeating such a note.
  MERGED_REVIEW="Reused the approval recorded ${REUSED_AT:-earlier} for head \`$REVIEW_HEAD\` on \`$PR_BASE_REF\` at \`$REVIEW_BASE\`: PR #$PR_NUMBER, both commits and the ticket are unchanged since that review, so no new specialist review ran. That review's findings are in its own comment.

\`\`\`json
{\"verdict\":\"APPROVE\",\"bugs\":0,\"security_issues\":0,\"missing_acceptance\":[],\"fixes_needed\":[],\"summary\":\"Reused the recorded approval of an unchanged head and base.\",\"findings\":[]}
\`\`\`"
  MERGED_RAW="$MERGED_REVIEW"
  _sec_critical=0
else
# The paid review below keeps its original indentation: the prompts are multi-line
# strings, and indenting them would change what the models read.

# Large-diff guard — specialists sample critical paths instead of exhaustive
# enumeration when the diff exceeds the configured threshold. Repos with
# mature CI / type-safety tune this higher; legacy repos cap lower. Set via
# .agents.code_review_sampling_threshold in .bureau.json (default 500).
DIFF_GUIDANCE=""
if [ "${DIFF_TOTAL:-0}" -gt "${BUREAU_CODE_REVIEW_SAMPLING_THRESHOLD:-500}" ]; then
  DIFF_GUIDANCE=$'\nDiff is large — prefer sampling critical paths (data handlers, auth, external I/O) over exhaustive enumeration. Still cite file:line for every finding.'
fi

# Caveman (token-efficiency Layer 2): when caveman_level != off, prepend the
# /caveman directive so review PROSE comes back compressed. Review-prose only —
# the trailing fenced json verdict block must stay valid (parse_claude_json
# reads it), so we say so explicitly. Both claude and codex honor /caveman.
# No-op when off. Set via .agents.caveman_level or BUREAU_CAVEMAN_LEVEL.
CAVEMAN_PREFIX=""
_cav=$(caveman_level)
if [ "$_cav" != "off" ]; then
  CAVEMAN_PREFIX="/caveman $_cav
(Compress prose only — keep file paths, code, error strings, and the final fenced \`\`\`json block byte-exact and valid JSON.)

"
  echo "  Caveman: review prose compressed at level '$_cav'."
fi

echo "  Starting correctness review..."
"${CLAUDE[@]}" "${CAVEMAN_PREFIX}You are a CORRECTNESS specialist reviewing $ISSUE ($ISSUE_TITLE). Branch: $BRANCH, PR: #$PR_NUMBER.

$SPEC_CONTEXT

$LESSONS_CONTEXT

$CYCLE_NOTE

Diff: $DIFF_STATS$DIFF_GUIDANCE

PR target: $PR_BASE_REF. Review the pinned PR inputs with 'git diff $REVIEW_DIFF --'. The checkout may include a local validation merge of that base; keep findings scoped to this immutable diff. Check: logic errors, null/undefined, race conditions, error handling, acceptance criteria satisfaction, task completion.

For every finding, cite file:line. Classify CRITICAL (data-loss / silent corruption) / BUG (real defect) / MINOR (style, nit) / SKIP (contradicts a pinned decision — include citation to the pin).

Emit human-readable prose for the PR reviewer, then a SINGLE fenced json block at the very end:

\`\`\`json
{\"specialist\":\"correctness\",\"counts\":{\"critical\":0,\"bug\":0,\"minor\":0,\"skip\":0},\"acceptance_gaps\":[],\"findings\":[{\"file\":\"path\",\"line\":0,\"class\":\"BUG\",\"msg\":\"\",\"skip_citation\":null}],\"summary\":\"\"}
\`\`\`" > "$REVIEW_TMP/correctness.txt" 2>"$REVIEW_TMP/correctness.stderr" &
CORRECT_PID=$!

echo "  Starting security review..."
"${CLAUDE[@]}" "${CAVEMAN_PREFIX}You are a SECURITY specialist reviewing $ISSUE ($ISSUE_TITLE). Branch: $BRANCH, PR: #$PR_NUMBER.

$SPEC_CONTEXT

$LESSONS_CONTEXT

$CYCLE_NOTE

Diff: $DIFF_STATS$DIFF_GUIDANCE

PR target: $PR_BASE_REF. Review the pinned PR inputs with 'git diff $REVIEW_DIFF --'. The checkout may include a local validation merge of that base; keep findings scoped to this immutable diff. Check: injection, auth/authz, secrets, data exposure, CORS/CSRF, dependency vulns, input validation.

For every finding, cite file:line. Classify CRITICAL / BUG / MINOR / SKIP (with pin citation).

Emit human-readable prose, then a SINGLE fenced json block:

\`\`\`json
{\"specialist\":\"security\",\"counts\":{\"critical\":0,\"bug\":0,\"minor\":0,\"skip\":0},\"findings\":[{\"file\":\"path\",\"line\":0,\"class\":\"BUG\",\"msg\":\"\",\"skip_citation\":null}],\"summary\":\"\"}
\`\`\`" > "$REVIEW_TMP/security.txt" 2>"$REVIEW_TMP/security.stderr" &
SEC_PID=$!

echo "  Starting performance review..."
"${CLAUDE[@]}" "${CAVEMAN_PREFIX}You are a PERFORMANCE specialist reviewing $ISSUE ($ISSUE_TITLE). Branch: $BRANCH, PR: #$PR_NUMBER.

$SPEC_CONTEXT

$LESSONS_CONTEXT

$CYCLE_NOTE

Diff: $DIFF_STATS$DIFF_GUIDANCE

PR target: $PR_BASE_REF. Review the pinned PR inputs with 'git diff $REVIEW_DIFF --'. The checkout may include a local validation merge of that base; keep findings scoped to this immutable diff. Check: database N+1, rendering blocks, bundle size regression, memory leaks, network waterfalls, algorithmic complexity relative to the baseline.

Performance is the most likely axis to over-flag. Only raise BUG for measurable regressions — not speculative micro-optimisations.

For every finding, cite file:line. Classify CRITICAL / BUG / MINOR / SKIP (with pin citation).

Emit human-readable prose, then a SINGLE fenced json block:

\`\`\`json
{\"specialist\":\"performance\",\"counts\":{\"critical\":0,\"bug\":0,\"minor\":0,\"skip\":0},\"findings\":[{\"file\":\"path\",\"line\":0,\"class\":\"BUG\",\"msg\":\"\",\"skip_citation\":null}],\"summary\":\"\"}
\`\`\`" > "$REVIEW_TMP/performance.txt" 2>"$REVIEW_TMP/performance.stderr" &
PERF_PID=$!

echo "  Waiting for specialists..."
wait $CORRECT_PID 2>/dev/null; echo "    Correctness: done"
wait $SEC_PID 2>/dev/null; echo "    Security: done"
wait $PERF_PID 2>/dev/null; echo "    Performance: done"

CORRECTNESS_REVIEW="Failed"
SECURITY_REVIEW="Failed"
PERFORMANCE_REVIEW="Failed"
[ -s "$REVIEW_TMP/correctness.txt" ] && CORRECTNESS_REVIEW=$(<"$REVIEW_TMP/correctness.txt")
[ -s "$REVIEW_TMP/security.txt" ]    && SECURITY_REVIEW=$(<"$REVIEW_TMP/security.txt")
[ -s "$REVIEW_TMP/performance.txt" ] && PERFORMANCE_REVIEW=$(<"$REVIEW_TMP/performance.txt")
# The security specialist's own CRITICAL count, read from the whole review before the
# ARG_MAX guard below trims it for the merge prompt: a provider envelope (cost tracking
# wraps the output as JSON) cut to its last KB is no longer JSON, and the count would be
# lost without a sound.
_sec_critical=$(parse_claude_json "$SECURITY_REVIEW" '.counts.critical')

# ARG_MAX guard: a codex specialist review can run 300-400KB; three of them
# inlined into the merge prompt below as a single shell argument overflow
# ARG_MAX (~1MB on macOS) and the merge call dies with exit 126 (E2BIG) before
# the model ever runs. The decision-relevant content — the findings list and
# the trailing fenced-json verdict — sits at the END of each review, so keep
# the tail when one is oversized. Tunable via BUREAU_REVIEW_MERGE_CAP_KB.
# (Claude reviews are ~5-20KB so this is a no-op for the default runner.)
_rv_cap=$(( ${BUREAU_REVIEW_MERGE_CAP_KB:-60} * 1024 ))
for _rv in CORRECTNESS_REVIEW SECURITY_REVIEW PERFORMANCE_REVIEW; do
  if [ "$(printf '%s' "${!_rv}" | wc -c)" -gt "$_rv_cap" ]; then
    printf -v "$_rv" '…[%s truncated to last %dKB for merge — full review in %s]…\n%s' \
      "$_rv" "$(( _rv_cap/1024 ))" "$REVIEW_TMP" "$(printf '%s' "${!_rv}" | tail -c "$_rv_cap")"
  fi
done

echo ""
echo "  Merging findings..."

MERGED_RAW=$("${CLAUDE[@]}" --schema "$SCRIPT_REPO/scripts/bureau-review.schema.json" "${CAVEMAN_PREFIX}Merge these three specialist reviews into a single verdict for PR #$PR_NUMBER ($ISSUE — $ISSUE_TITLE).

### Correctness
$CORRECTNESS_REVIEW

### Security
$SECURITY_REVIEW

### Performance
$PERFORMANCE_REVIEW

Verdict rules:
- APPROVE: no CRITICAL and no BUG across all three specialists.
- REQUEST_CHANGES: at least one BUG that isn't a style nit. MINOR findings alone do NOT warrant changes.
- BLOCK: any CRITICAL security finding, OR the findings require human judgment (ambiguous acceptance criteria, architectural disagreement).

Do NOT request changes for MINOR findings, style preferences, or hypothetical edge cases.

Answer with ONE JSON object (the schema is enforced). \"comment\" is the human-readable PR comment in markdown: Specialist Summaries, All Findings grouped by class with file:line for each, Fixes Needed. The implementer reworks from this comment, so name every CRITICAL and BUG finding with its file:line. \"findings\" lists the CRITICAL and BUG findings and the MINOR ones worth reading as {file, line, class, msg} (line 0 when a finding has no single line; class CRITICAL, BUG, MINOR or SKIP). The verdict fields decide; the comment explains them:

\`\`\`json
{\"verdict\":\"APPROVE|REQUEST_CHANGES|BLOCK\",\"bugs\":0,\"security_issues\":0,\"missing_acceptance\":[],\"fixes_needed\":[],\"summary\":\"\",\"comment\":\"## Specialist Summaries\\n…\",\"findings\":[{\"file\":\"path\",\"line\":0,\"class\":\"BUG\",\"msg\":\"\"}]}
\`\`\`" 2>"$REVIEW_TMP/merge.stderr")
MERGED_REVIEW=$(review_text_from_merger "$MERGED_RAW")
fi

echo "$MERGED_REVIEW"

echo ""
echo "Phase 2/3: build check"
# Order: the configured repo.test_command, then the scripts/bureau-test.sh shim,
# then `npm run build`. The first two are the QA stage's first two steps
# (detect_test_cmd); QA then goes on to npm test, cargo test, pytest and go test,
# review does not — a repo without either gets the npm build or nothing. A repo
# with none of the three is "not checked": said on stderr and in the review
# comment, never "Passed", and the verdict stays as the reviewers gave it. The
# command runs in this worktree under pipefail (as QA's runs do), and the
# verdict comes from its own exit status, never from a pipe into `tail`. Its full
# output is REVIEW_TMP/build.log, which survives only when the stage exits
# non-zero; the last 20 lines are always in the stage output.
BUILD_OK=true
BUILD_STATUS="Passed"
BUILD_CMD=$(bureau_get '.repo.test_command // empty')
if [ -z "$BUILD_CMD" ] && [ -f "scripts/bureau-test.sh" ]; then BUILD_CMD="bash scripts/bureau-test.sh"; fi
if [ -z "$BUILD_CMD" ] && [ -f "package.json" ]; then BUILD_CMD="npm run build"; fi
if [ -n "$BUILD_CMD" ]; then
  echo "  Running build check: $BUILD_CMD"
  BUILD_RC=0
  BUILD_TREE_BEFORE=$(git status --porcelain --untracked-files=all 2>/dev/null | sort || true)
  # PR code: runs without the Bureau secrets (bureau_untrusted_env, bureau-env.sh).
  bureau_untrusted_env --check || exit 24
  bureau_untrusted_env bash -o pipefail -c "$BUILD_CMD" </dev/null >"$REVIEW_TMP/build.log" 2>&1 || BUILD_RC=$?
  tail -20 "$REVIEW_TMP/build.log"
  # A dirty worktree makes the worker keep a stopped or failed review's worktree
  # as unfinished work (bureau-worker.sh). A warning only: it never changes the
  # verdict and never stops the stage.
  BUILD_TREE_NEW=$(comm -13 <(printf '%s\n' "$BUILD_TREE_BEFORE") \
    <(git status --porcelain --untracked-files=all 2>/dev/null | sort) || true)
  BUILD_TREE_UNTRACKED=$(printf '%s\n' "$BUILD_TREE_NEW" | grep '^??' || true)
  BUILD_TREE_TRACKED=$(printf '%s\n' "$BUILD_TREE_NEW" | grep -v '^??' | grep . || true)
  if [ -n "$BUILD_TREE_UNTRACKED" ]; then
    { echo "  WARN: the build check left new files git does not ignore; add them to .gitignore:"
      printf '%s\n' "$BUILD_TREE_UNTRACKED" | sed -n '1,20s/^/    /p'; } >&2 || true
  fi
  if [ -n "$BUILD_TREE_TRACKED" ]; then
    { echo "  WARN: the build check changed tracked files; it must not write to them:"
      printf '%s\n' "$BUILD_TREE_TRACKED" | sed -n '1,20s/^/    /p'; } >&2 || true
  fi
  if [ "$BUILD_RC" = 0 ]; then echo "  Build passed"
  else echo "  Build failed (exit $BUILD_RC)"; BUILD_OK=false; BUILD_STATUS="FAILED"; fi
else
  BUILD_STATUS="not checked (no repo.test_command, no scripts/bureau-test.sh, no package.json)"
  echo "  WARN: build $BUILD_STATUS" >&2
fi

echo ""
echo "Phase 3/3: post review + route"

# Parse the fenced json block at the end of the merger output. The legacy text
# form (`REVIEW_VERDICT: X` / `## REVIEW_VERDICT`) is kept as defense-in-depth when
# the model drops the json verdict; it accepts only an exact verdict word
# (review_verdict_from_text), so "NOT_APPROVED — BLOCK" can no longer read as
# APPROVE. Any miss falls back to BLOCK so a bad parse can never auto-merge a PR.
# The verdict and the counts come from the merger's own answer (MERGED_RAW), not from
# the text built for the comments (review_text_from_merger): the schema requires the
# verdict, and the text fallback below only matters for an answer without one.
VERDICT=$(parse_claude_json "$MERGED_RAW" '.verdict // empty')
if [ -z "$VERDICT" ]; then
  VERDICT=$(review_verdict_from_text "$MERGED_REVIEW")
fi
VERDICT="${VERDICT:-BLOCK}"

# The verdict rules run as one ordered decision in bureau-config.sh
# (`decide_review_verdict`): verdict check, security count, the security
# specialist's CRITICAL count, the security floor, the build fold, and the cycle
# cap last, so the cap also sees a REQUEST_CHANGES the build fold produced. The
# security count is read without a default: a missing field is unreadable, not 0.
MAX_REVIEW_CYCLES="$BUREAU_MAX_REVIEW_CYCLES"
# REVIEW_CYCLE_COUNT was computed at Phase 1 so specialists could reference
# it; reuse here for the loop-breaker check.
echo "  Review cycles: ${REVIEW_CYCLE_COUNT:-0}"
_sec_issues=$(parse_claude_json "$MERGED_RAW" '.security_issues')
_decision=$(decide_review_verdict "$VERDICT" "$_sec_issues" "$_sec_critical" "$BUILD_OK" "$REVIEW_CYCLE_COUNT" "$MAX_REVIEW_CYCLES")
VERDICT=$(printf '%s\n' "$_decision" | head -n 1)
ESCALATION_REASON=""
while IFS=$'\037' read -r _rule _reason _text; do
  [ -n "$_rule" ] || continue
  echo "  Verdict rule $_rule: $_text"
  [ -z "$_reason" ] || ESCALATION_REASON="$_reason"   # at most one rule carries a reason
  MERGED_REVIEW="$MERGED_REVIEW

$_text"
done <<< "$(printf '%s\n' "$_decision" | tail -n +2)"

# A provider can take long enough for the PR to be retargeted or either remote
# branch to advance. Preserve its evidence without publishing a stale verdict.
CURRENT_PR=$(gh pr view "$PR_NUMBER" --json state,baseRefName,headRefName) || exit 18
if ! printf '%s' "$CURRENT_PR" | jq -e --arg head "$BRANCH" --arg base "$PR_BASE_REF" \
  '.state == "OPEN" and .headRefName == $head and .baseRefName == $base' >/dev/null; then
  echo "  ERROR: PR identity or target changed during review; retained review output needs reconciliation."
  exit 18
fi
CURRENT_REFS=$(git ls-remote --exit-code origin "refs/heads/$BRANCH" "refs/heads/$PR_BASE_REF") || exit 18
CURRENT_HEAD=$(printf '%s' "$CURRENT_REFS" | awk -v ref="refs/heads/$BRANCH" '$2 == ref {print $1}')
CURRENT_BASE=$(printf '%s' "$CURRENT_REFS" | awk -v ref="refs/heads/$PR_BASE_REF" '$2 == ref {print $1}')
if [ "$CURRENT_HEAD" != "$REVIEW_HEAD" ] || [ "$CURRENT_BASE" != "$REVIEW_BASE" ]; then
  echo "  ERROR: PR head or base advanced during review; retained review output needs reconciliation."
  exit 18
fi

REVIEW_COMMENT="## Code Review v2 — $ISSUE

**Verdict**: $VERDICT
**Build**: $BUILD_STATUS
**Reviewed head**: \`$REVIEW_HEAD\`
**Target**: \`$PR_BASE_REF\` at \`$REVIEW_BASE\`

---

$MERGED_REVIEW

---
*Automated review by Bureau pipeline*"

if [ "$REUSED_GATE_WAIT" = 1 ] && [ "$VERDICT" = APPROVE ]; then
  echo "  PR #$PR_NUMBER already carries the APPROVE of this head — no new review comment."
else
  # GitHub refuses a comment over 65,536 characters, and the failure is swallowed here;
  # the cap keeps the header with the verdict the merge gate reads.
  REVIEW_COMMENT_BODY=$(bureau_cap_comment "$REVIEW_COMMENT") || REVIEW_COMMENT_BODY="$REVIEW_COMMENT"
  gh pr comment "$PR_NUMBER" --body "$REVIEW_COMMENT_BODY" || true
  echo "  Posted review to PR #$PR_NUMBER"
fi

case "$VERDICT" in
  APPROVE)
    echo "  Code review PASSED"
    if bureau_merge_is_manual; then
      # agents.merge_mode = manual: a human merges. Park the ticket in Merge as
      # the visible "awaiting merge" position (the stage refused at its start
      # when there is no Merge state); merge-pipeline.sh refuses there. Moving
      # is not merging, so this holds under a requested stop too.
      echo "  APPROVED — routing to Merge for a manual merge (agents.merge_mode manual)."
      post_comment "$ISSUE" "✅ Code review **APPROVED**. PR #$PR_NUMBER awaits a manual merge (\`agents.merge_mode\` is manual)."
      move_issue "$ISSUE" "$BUREAU_STATE_MERGE"
      NEXT_STATE="Merge (manual)"
    elif bureau_stop_requested; then
      # Save before the owning worker releases its lease, closing the gap where
      # another tick could start the same paid review. Record the reviewed inputs.
      if [ "${BUREAU_DRY_RUN:-0}" != 1 ]; then
        printf '%s' "$ISSUE_DETAIL" | python3 "$SCRIPT_REPO/scripts/bureau-supervision.py" --repo "$PWD" stop "$ISSUE" \
          --branch "$BRANCH" --state "$ACTUAL_STATE" --head "$REVIEW_HEAD" --base "$REVIEW_BASE" --base-ref "$PR_BASE_REF" --reviewed-head "$(git rev-parse HEAD)" --pr "$PR_NUMBER" \
          --verdict APPROVE >/dev/null
      fi
      post_comment "$ISSUE" "✅ Code review **APPROVED**. Stopped before merge as requested."
      echo "Review complete; stopped before merge."
      exit 20
    elif agent_enabled "merge" && [ -n "${BUREAU_STATE_MERGE:-}" ]; then
      post_comment "$ISSUE" "✅ Code review **APPROVED**. PR #$PR_NUMBER awaiting merge gate."
      move_issue "$ISSUE" "$BUREAU_STATE_MERGE"
      NEXT_STATE="Merge"
    else
      # Keep inline completion, but use exactly the same full gate set and JIT
      # checks as the dedicated merge worker, including when no Merge state exists.
      # Its exit code is the outcome, and the review acts on it (it used to report
      # Done whatever happened):
      #   0   merged; the merge stage moved the ticket to Done.
      #   2   the gate is not yet decided (checks pending or not started, GitHub still
      #       computing): the APPROVE is recorded for reuse like a --no-merge stop, so
      #       the next pick runs the build check and the gate again without a model
      #       review; the stage ends with 2 and the shepherd waits as at Merge.
      #   25  the gate is decided against the merge: needs-human, the gate lines on the
      #       ticket, the stage ends with 25.
      # Any other code passes through (15 no PR, 18 the merge call failed, 20 a stop).
      GATE_REPORT="$REVIEW_TMP/merge-gate.report"
      INLINE_RC=0
      BUREAU_INLINE_MERGE=1 BUREAU_MERGE_GATE_REPORT="$GATE_REPORT" \
        bash "$SCRIPT_REPO/scripts/merge-pipeline.sh" "$ISSUE" || INLINE_RC=$?
      GATE_OUTCOME=$(head -n 1 "$GATE_REPORT" 2>/dev/null || true)
      GATE_LINES=$(sed -n '2,$p' "$GATE_REPORT" 2>/dev/null | sed -e '/^$/d' -e 's/^/- /' || true)
      # A caller that asked for the gate report (the shepherd) gets it as from the merge stage.
      if [ -n "${BUREAU_MERGE_GATE_REPORT:-}" ] && [ -s "$GATE_REPORT" ]; then
        cp "$GATE_REPORT" "$BUREAU_MERGE_GATE_REPORT" 2>/dev/null \
          || echo "  WARN: could not write the gate report to $BUREAU_MERGE_GATE_REPORT" >&2
      fi
      case "$INLINE_RC:$GATE_OUTCOME" in
        0:*)
          NEXT_STATE="Done"
          [ "${BUREAU_DRY_RUN:-0}" != 1 ] || NEXT_STATE="Done (dry run: nothing was merged)"
          ;;
        2:not-yet)
          echo "  APPROVED — PR #$PR_NUMBER is not merged yet: its merge gate is not decided."
          if [ "${BUREAU_DRY_RUN:-0}" != 1 ] && ! printf '%s' "$ISSUE_DETAIL" | python3 "$SCRIPT_REPO/scripts/bureau-supervision.py" --repo "$PWD" stop "$ISSUE" \
              --branch "$BRANCH" --state "$ACTUAL_STATE" --head "$REVIEW_HEAD" --base "$REVIEW_BASE" --base-ref "$PR_BASE_REF" --reviewed-head "$(git rev-parse HEAD)" --pr "$PR_NUMBER" \
              --verdict APPROVE --merge-gate-wait >/dev/null; then
            echo "  WARN: the approval could not be recorded; the next run reviews the PR again." >&2
          fi
          if [ "$REUSED_GATE_WAIT" != 1 ]; then
            post_comment "$ISSUE" "⏳ Code review **APPROVED**. PR #$PR_NUMBER is not merged yet: its merge gate is not decided.

${GATE_LINES:-- (the merge stage left no gate lines)}

The approval is recorded for head \`$REVIEW_HEAD\`; the next run checks the build and the gate again without a new model review." || true
          fi
          _review_release_worktree
          NEXT_STATE="Build Review (merge gate not yet decided; the next run tries again)"
          STAGE_EXIT=2
          ;;
        25:*)
          echo "  APPROVED, but the merge gate is blocked — needs human review"
          # The APPROVE is recorded as well: once a human clears the blocker (re-runs
          # a flaky check) and removes needs-human, the next run reuses it instead of
          # paying a new review. The record holds the labels from the start of this
          # run, so it matches again once the label is gone; a push, a moved base or
          # an edited ticket means a new review.
          if [ "${BUREAU_DRY_RUN:-0}" != 1 ] && ! printf '%s' "$ISSUE_DETAIL" | python3 "$SCRIPT_REPO/scripts/bureau-supervision.py" --repo "$PWD" stop "$ISSUE" \
              --branch "$BRANCH" --state "$ACTUAL_STATE" --head "$REVIEW_HEAD" --base "$REVIEW_BASE" --base-ref "$PR_BASE_REF" --reviewed-head "$(git rev-parse HEAD)" --pr "$PR_NUMBER" \
              --verdict APPROVE >/dev/null; then
            echo "  WARN: the approval could not be recorded; the next run reviews the PR again." >&2
          fi
          if mark_needs_human "$ISSUE" code-review; then
            log_escalation "$ISSUE" "code-review" "${REVIEW_CYCLE_COUNT:-0}" \
              "Merge gate blocked after APPROVE" "$PR_NUMBER" "$BRANCH"
          fi
          post_comment "$ISSUE" "🛑 Code review **APPROVED**, but the merge gate is blocked: PR #$PR_NUMBER was not merged. Needs human.

${GATE_LINES:-- (the merge stage left no gate lines)}

Clear the blocker and remove needs-human. The approval is recorded for head \`$REVIEW_HEAD\`: the next run checks the build and the gate again without a new model review, unless the PR, its base or the ticket changed."
          _review_release_worktree
          NEXT_STATE="Build Review (needs-human: merge gate blocked)"
          STAGE_EXIT=25
          ;;
        *)
          echo "  The inline merge ended with $INLINE_RC${GATE_OUTCOME:+ ($GATE_OUTCOME)}; nothing was merged by this review." >&2
          exit "$INLINE_RC"
          ;;
      esac
    fi
    ;;
  REQUEST_CHANGES)
    echo "  Changes requested — moving back to Build"
    post_comment "$ISSUE" "🔄 Code Review: **Changes Requested** (cycle ${REVIEW_CYCLE_COUNT:-0}/$MAX_REVIEW_CYCLES)

$MERGED_REVIEW"
    move_issue "$ISSUE" "$BUREAU_STATE_BUILD"
    NEXT_STATE="Build (rework)"
    ;;
  BLOCK|*)
    echo "  Blocked — needs human review"
    # A label that cannot be written holds the ticket locally (mark_needs_human);
    # the BLOCK still ends the stage with 25 below.
    if mark_needs_human "$ISSUE" code-review; then
      log_escalation "$ISSUE" "code-review" "${REVIEW_CYCLE_COUNT:-0}" \
        "${ESCALATION_REASON:-Code reviewer returned BLOCK verdict}" \
        "$PR_NUMBER" "$BRANCH"
    fi
    post_comment "$ISSUE" "🚫 Code review **BLOCKED** — needs human review.

$MERGED_REVIEW"
    NEXT_STATE="Build Review (needs-human)"
    ;;
esac

echo ""
echo "═══════════════════════════════════════"
echo "  Code Review v2 complete: $ISSUE"
echo "  PR: #${PR_NUMBER:-none}"
echo "  Verdict: ${VERDICT:-UNKNOWN}"
echo "  Next: $NEXT_STATE"
echo "═══════════════════════════════════════"
# An APPROVE whose inline merge did not go through ends with that merge's outcome
# (2 not yet, 25 blocked), not with the APPROVE's 0.
[ -z "${STAGE_EXIT:-}" ] || exit "$STAGE_EXIT"
# The verdict alone decides the exit code: a BLOCK must not look like a clean
# review to the driver (resolve_verdict_exit in bureau-config.sh).
exit "$(resolve_verdict_exit "${VERDICT:-}")"
