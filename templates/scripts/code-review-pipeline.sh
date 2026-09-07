#!/bin/bash
# Code Review v2: multi-specialist review (correctness + security + performance)
set -euo pipefail

unset CLAUDECODE 2>/dev/null || true

REPO_DIR="$(pwd)"
SCRIPT_REPO="$(cd "$(dirname "$0")/.." && pwd)"
source "$(dirname "$0")/bureau-config.sh"

BUREAU_ENV_FILE="${BUREAU_ENV_FILE:-$SCRIPT_REPO/.env}"
set -a
# shellcheck disable=SC1090
if [ -f .env ]; then source .env
elif [ -f "$BUREAU_ENV_FILE" ]; then source "$BUREAU_ENV_FILE"
else [ -n "${LINEAR_API_KEY:-}" ] || { echo "ERROR: Set LINEAR_API_KEY"; exit 1; }; fi
set +a

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

FILES_CHANGED=$(git diff --name-only "$REVIEW_DIFF" --) || exit 18
FILES_COUNT=$(echo "$FILES_CHANGED" | grep -c . || true)
# shortstat: " 3 files changed, 42 insertions(+), 7 deletions(-)"
DIFF_SHORTSTAT=$(git diff --shortstat "$REVIEW_DIFF" -- | sed 's/^[[:space:]]*//') || exit 18
DIFF_TOTAL=$(echo "$DIFF_SHORTSTAT" | grep -oE '[0-9]+[[:space:]]*insertion|[0-9]+[[:space:]]*deletion' | awk '{s+=$1} END{print s+0}')
DIFF_STATS="${DIFF_SHORTSTAT:-no diff} (~${DIFF_TOTAL:-0} line changes)"
echo "  Files changed: $FILES_COUNT | $DIFF_STATS"

SPEC_DIR=""
for d in "$BUREAU_SPECS_DIR"/*/; do
  [ -d "$d" ] || continue
  dir_name=$(basename "$d")
  slug=$(echo "$dir_name" | sed 's/^[0-9]*-//')
  if echo "$BRANCH" | grep -qi "$slug"; then
    SPEC_DIR="$d"
    break
  fi
done

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
REVIEW_CYCLE_COUNT=$(get_issue_comments "$ISSUE" \
  | jq '[.[] | select(.body | test("Code Review.*Changes Requested"))] | length' 2>/dev/null || echo "0")
CYCLE_NOTE="This is review cycle $((REVIEW_CYCLE_COUNT + 1)) for this issue."
if [ "${REVIEW_CYCLE_COUNT:-0}" -gt 0 ]; then
  CYCLE_NOTE+=$'\nDeclined findings from earlier cycles are pinned — do NOT resurface them unless the underlying code has materially changed. Cite the prior cycle if you do re-raise.'
fi

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

MERGED_REVIEW=$("${CLAUDE[@]}" --schema "$SCRIPT_REPO/scripts/bureau-review.schema.json" "${CAVEMAN_PREFIX}Merge these three specialist reviews into a single verdict for PR #$PR_NUMBER ($ISSUE — $ISSUE_TITLE).

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

Write a human-readable PR comment (Specialist Summaries, All Findings grouped by class, Fixes Needed), then emit a SINGLE fenced json block at the very end:

\`\`\`json
{\"verdict\":\"APPROVE|REQUEST_CHANGES|BLOCK\",\"bugs\":0,\"security_issues\":0,\"missing_acceptance\":[],\"fixes_needed\":[],\"summary\":\"\"}
\`\`\`" 2>"$REVIEW_TMP/merge.stderr")

echo "$MERGED_REVIEW"

echo ""
echo "Phase 2/3: build check"
BUILD_OK=true
if [ -f "package.json" ]; then
  echo "  Running build..."
  if npm run build 2>&1 | tail -20; then echo "  Build passed"
  else echo "  Build failed"; BUILD_OK=false; fi
fi

echo ""
echo "Phase 3/3: post review + route"

# Parse the fenced json block at the end of the merger output.
# Legacy fallbacks (regex over `REVIEW_VERDICT: X` / `## REVIEW_VERDICT`) kept
# as defense-in-depth when the model drops the json block. Any miss falls back
# to BLOCK so a bad parse can never silently auto-merge a PR.
VERDICT=$(parse_claude_json "$MERGED_REVIEW" '.verdict // empty')
if [ -z "$VERDICT" ]; then
  VERDICT=$(echo "$MERGED_REVIEW" \
    | sed 's/\*\*//g' \
    | grep -A1 -E '^#+[[:space:]]*REVIEW_VERDICT[[:space:]]*$|^REVIEW_VERDICT:' \
    | grep -oE '(APPROVE|REQUEST_CHANGES|BLOCK)' \
    | head -1 || true)
fi
VERDICT="${VERDICT:-BLOCK}"

# Severity floor (codex-review hardening): a security finding must never
# auto-APPROVE. The merge step can mis-rank severity — we observed a CRITICAL
# git option-injection get a REQUEST_CHANGES header — so if the merged verdict
# json reports any security_issues, refuse APPROVE and bump to REQUEST_CHANGES
# (back to the fix loop), so a PR can't squash-merge over an open security
# finding even when the parsed verdict is wrong.
_sec_issues=$(parse_claude_json "$MERGED_REVIEW" '.security_issues // 0')
[[ "$_sec_issues" =~ ^[0-9]+$ ]] || _sec_issues=0
if [ "$_sec_issues" -gt 0 ] && [ "$VERDICT" = "APPROVE" ]; then
  echo "  Severity floor: $_sec_issues security finding(s) reported → downgrading APPROVE → REQUEST_CHANGES."
  VERDICT="REQUEST_CHANGES"
fi

MAX_REVIEW_CYCLES="$BUREAU_MAX_REVIEW_CYCLES"
# REVIEW_CYCLE_COUNT was computed at Phase 1 so specialists could reference
# it; reuse here for the loop-breaker check.
echo "  Review cycles: ${REVIEW_CYCLE_COUNT:-0}"

if [ "$VERDICT" = "REQUEST_CHANGES" ] && [ "${REVIEW_CYCLE_COUNT:-0}" -ge "$MAX_REVIEW_CYCLES" ]; then
  echo "  Loop breaker: escalating to needs-human."
  VERDICT="BLOCK"
  ESCALATION_REASON="REQUEST_CHANGES exceeded max_review_cycles=$MAX_REVIEW_CYCLES"
  MERGED_REVIEW="$MERGED_REVIEW

---
**ESCALATED:** $REVIEW_CYCLE_COUNT review cycles (max $MAX_REVIEW_CYCLES). Needs human intervention."
fi

[ "$BUILD_OK" = false ] && VERDICT="REQUEST_CHANGES" && MERGED_REVIEW="$MERGED_REVIEW

BUILD FAILURE: Must be fixed."

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
**Build**: $([ "$BUILD_OK" = true ] && echo "Passed" || echo "FAILED")
**Reviewed head**: \`$REVIEW_HEAD\`
**Target**: \`$PR_BASE_REF\` at \`$REVIEW_BASE\`

---

$MERGED_REVIEW

---
*Automated review by Bureau pipeline*"

gh pr comment "$PR_NUMBER" --body "$REVIEW_COMMENT" || true

echo "  Posted review to PR #$PR_NUMBER"

case "$VERDICT" in
  APPROVE)
    echo "  Code review PASSED"
    if bureau_stop_requested; then
      # Save before the owning worker releases its lease, closing the gap where
      # another tick could start the same paid review. Record the reviewed inputs.
      if [ "${BUREAU_DRY_RUN:-0}" != 1 ]; then
        printf '%s' "$ISSUE_DETAIL" | python3 "$SCRIPT_REPO/scripts/bureau-supervision.py" --repo "$PWD" stop "$ISSUE" \
          --branch "$BRANCH" --state "$ACTUAL_STATE" --head "$REVIEW_HEAD" --base "$REVIEW_BASE" --base-ref "$PR_BASE_REF" --reviewed-head "$(git rev-parse HEAD)" --pr "$PR_NUMBER" >/dev/null
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
      BUREAU_INLINE_MERGE=1 bash "$SCRIPT_REPO/scripts/merge-pipeline.sh" "$ISSUE"
      NEXT_STATE="Done"
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
    if add_issue_label "$ISSUE" "needs-human"; then
      log_escalation "$ISSUE" "code-review" "${REVIEW_CYCLE_COUNT:-0}" \
        "${ESCALATION_REASON:-Code reviewer returned BLOCK verdict}" \
        "$PR_NUMBER" "$BRANCH"
    else
      echo "  WARN: failed to add 'needs-human' label to $ISSUE; will retry on next tick" >&2
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
