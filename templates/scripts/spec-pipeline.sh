#!/bin/bash
# Spec pipeline: Pick issue → specify → plan → tasks → push
set -euo pipefail

unset CLAUDECODE 2>/dev/null || true

REPO_DIR="$(pwd)"
SCRIPT_REPO="$(cd "$(dirname "$0")/.." && pwd)"
source "$(dirname "$0")/bureau-config.sh"

BUREAU_ENV_FILE="${BUREAU_ENV_FILE:-$SCRIPT_REPO/.env}"
# BUREAU_ENV_FILE only, never ./.env: in a stage worktree that is a file the branch controls.
if [ -f "$BUREAU_ENV_FILE" ]; then bureau_load_env --export "$BUREAU_ENV_FILE"
else bureau_secret_set LINEAR_API_KEY || { echo "ERROR: Set LINEAR_API_KEY"; exit 1; }; fi

CLAUDE=(run_stage_for spec)
bureau_secret_copy API_KEY LINEAR_API_KEY

precondition_linear
precondition_runner spec

# EXP-491: single-flight / bounded-parallelism gate. When
# BUREAU_MAX_CONCURRENT_ISSUES is non-zero, refuse to pick new Triage work
# until existing in-flight issues drain below the cap. Spec is the only
# stage that admits new work into the pipeline — gating here is sufficient.
# Downstream stages (spec-review, ux, copy, implement, qa, code-review,
# rebase, merge) keep operating on whatever's already in-flight, so a cap
# of 1 still drains the current issue without deadlock.
if [ "${BUREAU_MAX_CONCURRENT_ISSUES:-0}" -gt 0 ]; then
  in_flight=$(count_in_flight_issues)
  if [ "${in_flight:-0}" -ge "$BUREAU_MAX_CONCURRENT_ISSUES" ]; then
    echo "Spec gate: $in_flight in-flight issue(s), cap is $BUREAU_MAX_CONCURRENT_ISSUES. Holding off on new Triage picks."
    exit 2
  fi
fi

# Step 0: Pick or resolve issue
if [ -n "${1:-}" ]; then
  ISSUE="$1"
  echo "Using specified issue: $ISSUE"
else
  echo "Picking next $BUREAU_LABEL_LANE2_NAME Triage issue..."
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
if [ "$ACTUAL_STATE" != "Triage" ]; then
  echo "  WARNING: $ISSUE is in '$ACTUAL_STATE', not 'Triage'. Skipping."
  exit 2
fi

echo ""
echo "═══════════════════════════════════════"
echo "  Spec Pipeline: $ISSUE"
echo "═══════════════════════════════════════"
echo ""

echo "→ Moving $ISSUE to Spec..."
move_issue "$ISSUE" "$BUREAU_STATE_SPEC"
echo "  Done."

# EXP-416 Part A: trap failure and route back to Triage. Installed right after
# the Triage→Spec move so any crash in speckit phases doesn't strand the issue.
# Cleared before the final move_issue → Spec Review so success doesn't fire it.
_spec_recovery() {
  local rc=$?
  [ "$rc" = 0 ] && return 0
  local klass="speckit-failed"
  case "$rc" in
    10) klass="linear-down" ;;
    11) klass="worktree-dirty" ;;
    # Linear just failed every retry: the rollback's own writes get one
    # attempt each, so the halt is not delayed by another full retry ladder.
    27) klass="linear-unusable"; export _BUREAU_LINEAR_SINGLE_ATTEMPT=1 ;;
    16) klass="claude-unauth" ;;
    *)  klass="speckit-failed" ;;
  esac
  echo "RECOVERY: spec-pipeline failed (exit $rc / $klass). Routing $ISSUE back to Triage." >&2
  post_comment "$ISSUE" "❌ Spec pipeline failed (\`$klass\`, exit \`$rc\`). Routing back to Triage for a fresh attempt." || true
  move_issue "$ISSUE" "$BUREAU_STATE_TRIAGE" || true
  alert_telegram "$ISSUE" "spec-pipeline" "$rc" "Spec phase crashed after Triage→Spec move ($klass)" || true
}
trap _spec_recovery EXIT

echo "→ Fetching issue details..."
ISSUE_DETAIL=$(get_issue_detail "$ISSUE")
ISSUE_TITLE=$(echo "$ISSUE_DETAIL" | jq -r '.title // empty')
ISSUE_DESC=$(echo "$ISSUE_DETAIL" | jq -r '.description // empty')
PROJECT_NAME=$(echo "$ISSUE_DETAIL" | jq -r '.project.name // empty')
PROJECT_DESC=$(echo "$ISSUE_DETAIL" | jq -r '.project.description // empty')
echo "  $ISSUE: $ISSUE_TITLE"
[ -n "$PROJECT_NAME" ] && echo "  Project: $PROJECT_NAME"

# Pre-spec research (label-gated, same-tick, best-effort). When the issue
# carries `needs-research` AND `.agents.research` is configured in
# .bureau.json, spawn a $CLAUDE pass with WebFetch/WebSearch to compile
# API/library notes from official docs. The output is injected into
# specify's prompt as authoritative API reference and posted to Linear so
# spec-review can see it.
#
# Failure handling: research is BEST-EFFORT, not a gate. Any non-zero
# exit, or output missing the `<!-- bureau-research: -->` marker, is
# swallowed and the pipeline proceeds to specify without research
# context. This keeps the exit-code protocol (0/2/10–16) untouched and
# degrades to today's behavior on failure. The label is stripped only
# after a successful Linear comment post, so a crashed research run can
# be retried by re-applying the label.
RESEARCH_CONTEXT=""
if agent_enabled research \
   && printf '%s' "$ISSUE_DETAIL" | jq -e '.labels | index("needs-research")' >/dev/null 2>&1; then
  echo ""
  echo "Phase 0/5: research (needs-research label present)"
  CLAUDE_RESEARCH=(run_stage_for research)
  RESEARCH_RAW=$("${CLAUDE_RESEARCH[@]}" "You are doing pre-spec research for a Linear issue. The spec/build agents that come after you have stale training data, so your job is to ground them in current docs.

Issue: $ISSUE_TITLE
Body:
$ISSUE_DESC

Task:
1. Identify every external API, SDK, library, or service the implementation will touch.
2. For each, use WebFetch on the official docs URL (or WebSearch to find it) and read the relevant sections. Prefer primary sources over blog posts.
3. Compile a single markdown digest containing, per dependency: current version, the specific endpoints / method signatures / config keys this task will use, breaking changes in the last ~18 months, and known gotchas.

Do NOT speculate. If docs are unreachable for a dependency, say so explicitly for that dependency rather than guessing.

OUTPUT FORMAT — read this last; this is what the parser checks.

Your output MUST contain this exact line somewhere (preferably the first non-empty line):

    <!-- bureau-research: api1,api2,api3 -->

Where api1,api2,api3 is a comma-separated list of the APIs you researched (no spaces around commas). Examples:
    <!-- bureau-research: aws-bedrock-converse,aws-sigv4 -->
    <!-- bureau-research: stripe-checkout,stripe-webhooks -->

If the issue has no external API surface, emit:

    <!-- bureau-research:  -->
    No external API surface identified.

After the marker, write a concise markdown body with inline doc URLs. No preamble, no closing summary. Be terse." 2>&1 || echo "")

  # Extract the marker from anywhere in the output (not just line 1).
  # Headless claude -p subprocesses regularly prepend markdown headers,
  # wrap in code fences, or add an intro sentence — all valid research
  # content that the strict line-1 check used to discard. The marker
  # itself is what signals "real research happened"; its position is
  # cosmetic.
  if printf '%s' "$RESEARCH_RAW" | grep -q '<!-- bureau-research:'; then
    RESEARCH_CONTEXT="
--- API research (auto-generated; treat as authoritative for API shapes) ---
$RESEARCH_RAW
--- End research ---
"
    if post_comment "$ISSUE" "$RESEARCH_RAW"; then
      remove_issue_label "$ISSUE" "needs-research" || true
      echo "  research complete; label stripped"
    else
      echo "  warning: research comment post failed; label preserved for retry"
    fi
  else
    echo "  research produced no valid output (missing <!-- bureau-research: --> marker); proceeding without it"
  fi
fi

echo ""
echo "Phase 1/5: specify"
PROJECT_CONTEXT=""
[ -n "$PROJECT_DESC" ] && PROJECT_CONTEXT="
--- Project context: $PROJECT_NAME ---
$PROJECT_DESC
--- End project context ---
"

# logs→memory: include human-curated LESSONS.md if present. Empty when absent.
LESSONS_CONTEXT=$(build_lessons_context)
SPECKIT_DIR=.claude/skills
if [ "$(resolve_runner_for_stage spec)" = codex ]; then SPECKIT_DIR=.agents/skills; fi

# .specify/feature.json before specify, so that Phase 1.5 can tell a file this
# run wrote from one it left behind: the file's checksum, whether the directory
# it names exists already, and the branch checked out now (a disposable spec
# worker starts detached on origin/main, so usually none). Only the content
# counts: a stale value written back during the call (by an agent's rewrite or a
# `git checkout` of the file) must not pass as new.
# _fj_read prints feature_directory, or nothing when the file is missing or is
# not valid JSON (jq's own status, 2 or 5, would end the stage under set -e, and
# 2 reads as "queue empty" to both drivers).
_fj_read() {
  local out
  if out=$(jq -r '.feature_directory // empty' .specify/feature.json 2>/dev/null); then
    printf '%s' "$out"
  fi
  return 0
}
FJ_SUM_BEFORE=""
FJ_DIR_EXISTED=""
if [ -f .specify/feature.json ]; then
  FJ_SUM_BEFORE=$(cksum < .specify/feature.json)
  FJ_DIR_BEFORE=$(_fj_read)
  if [ -n "$FJ_DIR_BEFORE" ] && [ -d "$FJ_DIR_BEFORE" ]; then FJ_DIR_EXISTED=1; fi
fi
FJ_BRANCH_BEFORE=$(git branch --show-current 2>/dev/null || true)

"${CLAUDE[@]}" "Read the file $SPECKIT_DIR/speckit-specify/SKILL.md and follow its instructions exactly.

Use this as input:

Feature name: $ISSUE_TITLE
Source: Linear issue $ISSUE
$PROJECT_CONTEXT
$ISSUE_DESC
$RESEARCH_CONTEXT
$LESSONS_CONTEXT"

echo ""
echo "  specify complete"

echo ""
echo "Phase 1.5: ensure feature branch (speckit v0.7.5+ hook-skip workaround)"
# Speckit v0.7.5 moved branch creation into a `before_specify` hook
# (.specify/extensions.yml → speckit.git.feature) that is a SOFT prompt
# directive, not a script-enforced step. Headless `claude -p` subprocesses
# driving speckit-specify reliably skip the chained hook invocation and
# leave the worktree on detached HEAD / main with the spec files as
# untracked changes. We read .specify/feature.json (which specify DOES
# populate reliably) and create the branch from the shell. `git checkout -b`
# carries the untracked spec files onto the new branch.
#
# Upstream tracking: github/spec-kit (issue filed 2026-05-15)
FEATURE_DIR=$(_fj_read)
if [ -z "$FEATURE_DIR" ] || [ ! -d "$FEATURE_DIR" ]; then
  trap - EXIT
  echo "ERROR: .specify/feature.json missing or feature_directory invalid — specify did not complete cleanly."
  post_comment "$ISSUE" "❌ Spec pipeline aborted — .specify/feature.json absent or invalid after speckit-specify. Routing back to Triage." || true
  move_issue "$ISSUE" "$BUREAU_STATE_TRIAGE" || true
  alert_telegram "$ISSUE" "spec-pipeline" "11" "feature.json missing after specify" || true
  exit 11
fi
# A stale feature.json: the file holds byte for byte what it held before the
# call, the directory it names existed before the call, and that directory is
# not this ticket's own. Own means the spec directory named exactly like the
# last segment of the branch checked out before the call (a checkout already on
# the ticket's branch) or of the ticket's newest bureau-branch marker: this stage
# names the branch, and so the marker, after its directory. No fuzzy match: on a
# re-spec the worker starts on origin/main, where the ticket's own directory (on
# its unmerged branch) is missing and the marker would fit a sibling. Linear's
# generated branch name does not count either. Such
# a file is usually the last value merged to main (the file is tracked and kept
# per branch), and plan and tasks would overwrite another ticket's spec. A re-run
# reads the same file, so a human decides (needs-human); when the directory is
# the ticket's after all, posting its marker releases it. A changed file is
# trusted: specify wrote it in this run.
# _fj_named_by <branch>: 0 when FEATURE_DIR is $BUREAU_SPECS_DIR/<last segment>.
_fj_named_by() {
  local want="${BUREAU_SPECS_DIR%/}/${1##*/}"
  [ -d "$want" ] && [ "$(cd "$want" && pwd -P)" = "$FJ_WANT" ]
}
if [ -n "$FJ_DIR_EXISTED" ] && [ "$(cksum < .specify/feature.json)" = "$FJ_SUM_BEFORE" ]; then
  FJ_OWN=""
  FJ_WANT=$(cd "$FEATURE_DIR" && pwd -P)
  if [ -n "$FJ_BRANCH_BEFORE" ] && _fj_named_by "$FJ_BRANCH_BEFORE"; then
    FJ_OWN=1
  fi
  if [ -z "$FJ_OWN" ]; then
    # A failed read ends the stage with its code through the recovery trap; it
    # never counts as "no marker". A comment with an empty body has no first line.
    FJ_MARKER=$(get_issue_comments "$ISSUE" | jq -r '[.[] | (.body // "") | split("\n")[0] // "" | select(test("^<!-- bureau-branch: [^ ]+ -->[[:space:]]*$"))][0] // "" | sub("^<!-- bureau-branch: "; "") | sub(" -->[[:space:]]*$"; "")')
    if [ -n "$FJ_MARKER" ] && _fj_named_by "$FJ_MARKER"; then FJ_OWN=1; fi
  fi
  if [ -z "$FJ_OWN" ]; then
    trap - EXIT
    echo "ERROR: .specify/feature.json is stale — unchanged by speckit-specify, it still names $FEATURE_DIR, which existed before this run and is not $ISSUE's spec directory."
    post_comment "$ISSUE" "❌ Spec pipeline aborted — \`.specify/feature.json\` is unchanged after speckit-specify and still names \`$FEATURE_DIR\`, a spec directory that existed before this run and is not this ticket's by name. Plan and tasks would have written into another ticket's spec, and a re-run reads the same file. Find out why speckit-specify did not record the new feature (a \`feature.json\` tracked on \`main\` keeps the last merged value); if that directory is this ticket's after all, post its marker (troubleshooting: exit 11). Then remove \`needs-human\`. Routing back to Triage." || true
    move_issue "$ISSUE" "$BUREAU_STATE_TRIAGE" || true
    mark_needs_human "$ISSUE" spec 11 || true
    alert_telegram "$ISSUE" "spec-pipeline" "11" "stale feature.json after specify" || true
    exit 11
  fi
fi
FEATURE_BRANCH=$(basename "$FEATURE_DIR")
# The spec directory of this run, with the trailing slash the other stages use.
# Every later step reads this one directory instead of "the newest spec": in a
# fresh worktree all spec directories have the same age, and installs that
# reuse one `NNN-` prefix have many candidates.
SPEC_DIR="${FEATURE_DIR%/}/"
CUR_BRANCH=$(git branch --show-current)
if [ -z "$CUR_BRANCH" ] || [ "$CUR_BRANCH" = "main" ]; then
  echo "  Creating branch $FEATURE_BRANCH from current HEAD"
  git checkout -b "$FEATURE_BRANCH"
elif [ "$CUR_BRANCH" = "$FEATURE_BRANCH" ]; then
  echo "  Already on $FEATURE_BRANCH (idempotent — speckit hook fired, or this is a retry)"
else
  echo "  WARN: on unexpected branch '$CUR_BRANCH', expected '$FEATURE_BRANCH'. Continuing on current."
fi
echo "  branch ready: $(git branch --show-current)"

echo ""
echo "Phase 2/5: plan"
"${CLAUDE[@]}" "Read the file $SPECKIT_DIR/speckit-plan/SKILL.md and follow its instructions exactly.
Work on the spec in $SPEC_DIR (and no other directory under $BUREAU_SPECS_DIR/)."
echo ""
echo "  plan complete"

echo ""
echo "Phase 3/5: tasks"
"${CLAUDE[@]}" "Read the file $SPECKIT_DIR/speckit-tasks/SKILL.md and follow its instructions exactly.
Work on the spec in $SPEC_DIR (and no other directory under $BUREAU_SPECS_DIR/)."
echo ""
echo "  tasks complete"

echo ""
echo "Phase 4/5: crosscheck"
SPEC_TASKS=""
[ -f "${SPEC_DIR}tasks.md" ] && SPEC_TASKS="${SPEC_DIR}tasks.md"
if [ -n "$SPEC_TASKS" ]; then
  crosscheck_open_prs "$ISSUE" "$SPEC_TASKS"
else
  echo "  No tasks.md found — skipping crosscheck"
fi

echo ""
echo "Phase 5/5: push + update Linear"
BRANCH=$(git branch --show-current)
if [ "$BRANCH" = "main" ] || [ -z "$BRANCH" ]; then
  trap - EXIT
  echo "ERROR: Not on a feature branch. Moving $ISSUE back to Triage."
  post_comment "$ISSUE" "❌ Spec pipeline aborted — not on a feature branch after speckit phases. Routing back to Triage." || true
  move_issue "$ISSUE" "$BUREAU_STATE_TRIAGE" || true
  alert_telegram "$ISSUE" "spec-pipeline" "11" "Not on a feature branch after speckit phases" || true
  exit 11
fi

git add -A
# Trailer makes the commit recognizable to branch_is_bureau_only without
# relying on the subject-line regex fallback.
git commit --allow-empty 2>/dev/null \
  -m "$ISSUE: spec artifacts" \
  -m "Bureau-Generated: true" \
  || true
if [ "${BUREAU_DRY_RUN:-0}" = "1" ]; then
  echo "  [DRY_RUN] would: git push -u origin HEAD ($BRANCH)"
else
  git push -u origin HEAD
  echo "  Pushed branch: $BRANCH"
fi

# The spec directory for this issue is $SPEC_DIR, read from .specify/feature.json above.

# Build a digest from the spec artifacts (creative work — Claude).
# Idempotency: pull the most recent existing spec-digest comment so Claude can
# emit `SKIP` if the new digest would be byte-identical. This avoids
# comment-storms when the spec pipeline re-runs (e.g. after a Triage retry)
# without any artifact changes.
PREV_DIGEST=$(get_issue_comments "$ISSUE" \
  | jq -r '[.[] | select(.body | test("\\*\\*Spec Artifacts —"))][0].body // ""' 2>/dev/null || echo "")
SPEC_DIGEST=""
if [ -n "$SPEC_DIR" ]; then
  SPEC_DIGEST=$("${CLAUDE[@]}" "Read the spec artifacts in $SPEC_DIR and write a digest for a Linear comment.

Previous digest on this issue (may be empty if this is the first spec run):
---PREV---
$PREV_DIGEST
---/PREV---

If the digest you would write is byte-identical to the previous one (no spec.md / plan.md / tasks.md changes since), output the literal string \`SKIP\` and nothing else. Otherwise produce the new digest.

Sections (markdown, in this order):
1. **Purpose** — 1-2 sentences from spec.md §Overview. Do not reframe or embellish.
2. **Key Requirements** — top 5 functional requirements from spec.md as a bulleted list. Keep the original wording.
3. **Tech Approach** — 2-3 sentences from plan.md. Name the stack, libraries, and the key design decision.
4. **Risks / Assumptions** — anything plan.md or research.md flagged as risky, unresolved, or assumed. Bullet list. If none are flagged, write 'None flagged.'
5. **Tasks** — copy-paste the full task list from tasks.md verbatim. Preserve checkboxes, IDs, dependency markers, and any [P] tags. Do NOT rephrase or renumber — downstream implementers match against these exactly.

End with:
---
Branch: \`$BRANCH\`
Spec dir: \`$SPEC_DIR\`

Constraints: do not invent requirements. If a section has no source material, write 'Not specified.' Keep sections 1-4 short; section 5 must be complete." 2>&1 || echo "Spec digest generation failed")
fi

# Clear the recovery trap — we're about to succeed.
trap - EXIT

# Idempotency: if Claude detected no artifact change, post no comment. The
# bureau-branch marker is already on the issue from the previous spec run.
SPEC_DIGEST_TRIMMED=$(printf '%s' "$SPEC_DIGEST" | sed 's/^[[:space:]]*//; s/[[:space:]]*$//')
if [ "$SPEC_DIGEST_TRIMMED" = "SKIP" ]; then
  echo "  Spec digest unchanged — no new comment posted."
else
  # Post the combined digest + bureau-branch marker (EXP-413 deterministic
  # branch discovery). The marker is an HTML comment so it renders invisibly
  # in Linear but can be parsed by get_issue_branch() in implement/spec-review/ux.
  DIGEST_BODY="<!-- bureau-branch: $BRANCH -->
**Spec Artifacts — $ISSUE**

$SPEC_DIGEST

---
*Spec branch \`$BRANCH\` pushed. Ready for review.*"
  post_comment "$ISSUE" "$DIGEST_BODY"
fi

move_issue "$ISSUE" "$BUREAU_STATE_SPEC_REVIEW"

echo "  Moved $ISSUE to Spec Review"

echo ""
echo "═══════════════════════════════════════"
echo "  Spec pipeline complete: $ISSUE"
echo "  Branch: $BRANCH"
echo "  Status: Spec Review"
echo "═══════════════════════════════════════"
