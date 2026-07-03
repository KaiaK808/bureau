#!/bin/bash
# Implement pipeline: Pick Build issue → checkout branch → execute tasks via a
# bounded retry loop → push → PR. Each tick attempts up to MAX_ITER passes
# through Claude, parsing the JSON status block emitted at the end of each
# iteration to decide whether to continue. Terminal states:
#   COMPLETE                  → push + mark PR ready + move to QA / Build Review
#   NEEDS_HUMAN/STUCK/        → push + draft PR + needs-human label + summary
#   CAP_TIME/PARTIAL            comment + no state move (issue stays in Build)
set -euo pipefail

unset CLAUDECODE 2>/dev/null || true

REPO_DIR="$(pwd)"
SCRIPT_REPO="$(cd "$(dirname "$0")/.." && pwd)"
source "$(dirname "$0")/bureau-config.sh"

if [ -f .env ]; then source .env
elif [ -f "$SCRIPT_REPO/.env" ]; then source "$SCRIPT_REPO/.env"
else echo "ERROR: No .env found"; exit 1; fi

CLAUDE=$(claude_cmd_for_stage "implement")
API_KEY="${LINEAR_API_KEY:?Set LINEAR_API_KEY in .env}"

# Retry-loop bounds. MAX_ITER caps the number of Claude passes per tick.
# ITER_TIMEOUT caps wall-time per pass; TOTAL_TIMEOUT caps cumulative wall-time
# so a single tick can't burn unbounded compute even if every iteration is
# productive. Defaults give ≤90 min worst case before the issue is parked for
# human review.
MAX_ITER="${BUREAU_IMPL_MAX_ITER:-3}"
ITER_TIMEOUT="${BUREAU_IMPL_ITER_TIMEOUT:-1800}"
TOTAL_TIMEOUT="${BUREAU_IMPL_TOTAL_TIMEOUT:-5400}"

# Resolve a `timeout`-style wrapper. coreutils ships `timeout` on Linux and as
# `gtimeout` on macOS (via `brew install coreutils`). Fall back to running
# $CLAUDE directly when neither is available — the cumulative TOTAL_TIMEOUT
# check at the top of the loop still bounds total wall time, just not per-iter.
if command -v timeout >/dev/null 2>&1; then
  TIMEOUT_CMD="timeout"
elif command -v gtimeout >/dev/null 2>&1; then
  TIMEOUT_CMD="gtimeout"
else
  echo "  WARN: neither 'timeout' nor 'gtimeout' on PATH — per-iter timeout disabled (cumulative cap still enforced)." >&2
  TIMEOUT_CMD=""
fi

# refresh_review_context: pull the latest "Code Review … Changes Requested"
# comment for $1 and emit the prompt block the implement loop interpolates.
# Returns empty if there's nothing relevant. Called once per iteration so a
# human comment posted mid-run is seen by the next pass.
refresh_review_context() {
  local issue="$1"
  local blob feedback
  blob=$(get_issue_branch_and_comments "$issue" 2>/dev/null || echo '{}')
  feedback=$(printf '%s' "$blob" \
    | jq -r '[.comments[] | select(.body | test("Code Review.*Changes Requested|FIXES_NEEDED"))][0].body // empty' 2>/dev/null || echo "")
  if [ -n "$feedback" ] && [ "${#feedback}" -gt 20 ]; then
    printf '\n--- Code Review Feedback (PRIORITY) ---\n%s\nAddress ALL fixes before remaining tasks.\n--- End feedback ---\n' "$feedback"
  fi
}

# open_or_update_pr_draft: ensure a draft PR exists for $BRANCH; emit its URL.
# Used during intermediate iterations and on non-COMPLETE terminal states so
# reviewers can see in-flight work without QA/code-review picking it up.
open_or_update_pr_draft() {
  local issue="$1" title="$2"
  if [ "${BUREAU_DRY_RUN:-0}" = "1" ]; then
    echo "<dry-run: would create/update draft PR>"
    return 0
  fi
  local existing
  existing=$(gh pr list --head "$BRANCH" --json number --jq '.[0].number' 2>/dev/null || echo "")
  if [ -n "$existing" ]; then
    gh pr view "$existing" --json url --jq '.url'
  else
    gh pr create --draft \
      --title "$issue: $title" \
      --body "## Summary
Implementation of $issue: $title (in progress).

Draft PR — see Linear issue for status.

Generated with [Claude Code](https://claude.com/claude-code)"
  fi
}

# open_or_update_pr_ready: ensure a ready-for-review PR exists for $BRANCH;
# if it was previously draft, mark it ready. Used on COMPLETE.
open_or_update_pr_ready() {
  local issue="$1" title="$2"
  if [ "${BUREAU_DRY_RUN:-0}" = "1" ]; then
    echo "<dry-run: would create or mark-ready PR>"
    return 0
  fi
  local existing
  existing=$(gh pr list --head "$BRANCH" --json number --jq '.[0].number' 2>/dev/null || echo "")
  if [ -n "$existing" ]; then
    # `gh pr ready` is a no-op if the PR is already ready; both are exit 0.
    # A real non-zero exit (transient API error, missing permission, deleted
    # PR) must surface so a human can finish the flip — FR-007.
    if ! gh pr ready "$existing" >/dev/null 2>&1; then
      echo "  WARN: failed to mark PR #$existing as ready-for-review; please flip manually" >&2
    fi
    gh pr view "$existing" --json url --jq '.url'
  else
    gh pr create \
      --title "$issue: $title" \
      --body "## Summary
Implementation of $issue: $title

Implemented from tasks.md spec.
See Linear issue for full context.

Generated with [Claude Code](https://claude.com/claude-code)"
  fi
}

# build_summary_comment: format the consolidated Linear comment posted at the
# end of the pipeline, regardless of terminal status. Takes status, total
# tasks done across iterations, per-iteration log, and PR URL.
build_summary_comment() {
  local status="$1" total_tasks="$2" iter_log="$3" pr_url="$4"
  local header
  case "$status" in
    COMPLETE)    header="🛠️ Implementation complete." ;;
    NEEDS_HUMAN) header="🚧 Implementation halted: NEEDS_HUMAN — Claude flagged tasks requiring human judgment." ;;
    STUCK)       header="🚧 Implementation stuck — no progress in last iteration (no commits, no [X] marks, no review fixes)." ;;
    CAP_TIME)    header="🚧 Implementation hit total time cap (${TOTAL_TIMEOUT}s) before completing." ;;
    PARTIAL)     header="🚧 Implementation made partial progress but exhausted iteration cap (${MAX_ITER}) without COMPLETE." ;;
    *)           header="🚧 Implementation ended with status=$status." ;;
  esac
  printf '%s\n\n**Total tasks done across iterations:** %s\n**Branch:** `%s`\n**PR:** %s\n\nIteration log:\n```\n%s```\n' \
    "$header" "$total_tasks" "$BRANCH" "$pr_url" "$iter_log"
}

precondition_linear

if [ -n "${1:-}" ]; then
  ISSUE="$1"
  echo "Using specified issue: $ISSUE"
else
  echo "Picking next Build issue..."
  ISSUE=$(pipeline_pick_next "$(basename "$0")")

  if [ -z "$ISSUE" ] || [[ ! "$ISSUE" =~ ^[A-Z]+-[0-9]+$ ]]; then
    echo "No qualifying issues found. Queue empty."
    exit 2
  fi
  echo "Picked: $ISSUE"
fi

# State guard runs unconditionally — queue-loop preselects an issue and then
# spawns the pipeline seconds later; in that window the state can change
# (parallel rebase agent, human intervention). Confirm the issue is still in
# Build before doing any work.
ACTUAL_STATE=$(get_issue_state "$ISSUE")
if [ "$ACTUAL_STATE" != "Build" ]; then
  echo "  WARNING: $ISSUE is in '$ACTUAL_STATE', not 'Build'. Skipping."
  exit 2
fi

echo ""
echo "═══════════════════════════════════════"
echo "  Implement Pipeline: $ISSUE"
echo "═══════════════════════════════════════"
echo ""

echo "→ Fetching issue details..."
ISSUE_DETAIL=$(get_issue_detail "$ISSUE")
ISSUE_TITLE=$(echo "$ISSUE_DETAIL" | jq -r '.title // empty')
ISSUE_DESC=$(echo "$ISSUE_DETAIL" | jq -r '.description // empty')
PROJECT_NAME=$(echo "$ISSUE_DETAIL" | jq -r '.project.name // empty')
PROJECT_DESC=$(echo "$ISSUE_DETAIL" | jq -r '.project.description // empty')
echo "  $ISSUE: $ISSUE_TITLE"

echo "→ Finding branch and prior review feedback..."
# Single GraphQL roundtrip for both branch resolution and comment scan; the
# comment array is reused below to extract any prior 'Changes Requested' block.
ISSUE_BLOB=$(get_issue_branch_and_comments "$ISSUE")
BRANCH=$(printf '%s' "$ISSUE_BLOB" | jq -r '.branch // empty')

# EXP-413: fail loud — no silent fresh-from-main fallback. Spec pipeline must
# have produced a branch with artifacts before implement can run. The legacy
# behaviour silently created a new branch from main on lookup failure and
# burned Claude tokens on implementations with zero spec context.
if [ -z "$BRANCH" ] || [[ "$BRANCH" == *" "* ]] || [[ ${#BRANCH} -gt 200 ]]; then
  echo "  ERROR: no bureau-branch marker found for $ISSUE."
  post_comment "$ISSUE" "❌ Implement pipeline cannot start: no bureau-branch marker on this issue. The spec pipeline must produce a branch before implement can run. Routing back to Triage."
  move_issue "$ISSUE" "$BUREAU_STATE_TRIAGE"
  exit 12
fi

echo "  Found branch: $BRANCH"
git fetch origin

if ! git rev-parse --verify "$BRANCH" >/dev/null 2>&1 \
  && ! git rev-parse --verify "origin/$BRANCH" >/dev/null 2>&1; then
  echo "  ERROR: branch '$BRANCH' not found locally or on origin."
  post_comment "$ISSUE" "❌ Implement pipeline cannot start: bureau-branch marker points at \`$BRANCH\` but the branch does not exist. Routing back to Triage — spec pipeline must produce the branch."
  move_issue "$ISSUE" "$BUREAU_STATE_TRIAGE"
  exit 12
fi

# Release the branch from any other worktree before attaching here.
free_branch_from_other_worktrees "$BRANCH" "$(pwd)"
if git rev-parse --verify "origin/$BRANCH" >/dev/null 2>&1; then
  git checkout -B "$BRANCH" "origin/$BRANCH"
else
  git checkout "$BRANCH"
fi

# Implement runs against possibly-stale code if the spec branch was cut before
# recent merges. Conflict here means the spec branch and main have diverged in
# overlapping files — Claude shouldn't try to resolve that. Issue is already in
# Build, so label needs-human and exit; the picker excludes needs-human, so the
# issue stays out of the queue until a human rebases.
if ! merge_origin_main_or_abort "$ISSUE" "Implement"; then
  # If labelling fails (Linear API hiccup), the issue isn't parked and the
  # picker will re-select it next tick — the merge will re-conflict and the
  # label will be retried then. Surface the warning so it's visible in logs;
  # exit 17 either way so the alert classifies as rebase-needed, not error-1.
  add_issue_label "$ISSUE" "needs-human" \
    || echo "  WARN: failed to add 'needs-human' label to $ISSUE; will retry on next tick" >&2
  exit 17
fi

echo ""
echo "Phase 1/2: execute tasks (bounded retry loop, MAX_ITER=$MAX_ITER)"

TASKS_FILE=""
# Match strategy: leading number first, slug-substring as fallback.
#
# Speckit stamps the same `NNN-` prefix onto both the spec dir and the feature
# branch. Branch names get truncated by speckit / Linear when long (observed:
# spec dir `091-wire-mcp-tool-metadata` paired with branch `091-wire-mcp-tool`).
# The post-strip slug then differs and `grep -qi "$slug"` against the branch
# fails — implement falsely reports "tasks.md missing" and routes back to Spec.
# The `NNN-` prefix survives truncation, so match on that first.
BRANCH_NUM=$(echo "$BRANCH" | grep -oE '^[0-9]+' || true)
for f in "$BUREAU_SPECS_DIR"/*/tasks.md; do
  [ -f "$f" ] || continue
  spec_dir=$(basename "$(dirname "$f")")
  spec_num=$(echo "$spec_dir" | grep -oE '^[0-9]+' || true)
  if [ -n "$spec_num" ] && [ -n "$BRANCH_NUM" ] && [ "$spec_num" = "$BRANCH_NUM" ]; then
    TASKS_FILE="$f"
    break
  fi
  # Fallback for branches/spec dirs without a numeric prefix (hand-named, legacy).
  if [ -z "$BRANCH_NUM" ] || [ -z "$spec_num" ]; then
    slug=$(echo "$spec_dir" | sed 's/^[0-9]*-//')
    if [ -n "$slug" ] && echo "$BRANCH" | grep -qi "$slug"; then
      TASKS_FILE="$f"
      break
    fi
  fi
done
# NOTE: no "only one tasks.md exists, use it regardless" fallback.
# That fallback (a) had a SIGPIPE bug (find | head -1 + pipefail = exit 141)
# and (b) was semantically wrong — it would feed the wrong tasks.md to an
# unrelated issue's implement run. If the for-loop above can't match branch
# to spec dir, fall through to "No tasks.md" below and implement from the
# issue description instead.

PROJECT_CONTEXT=""
[ -n "$PROJECT_DESC" ] && PROJECT_CONTEXT="
--- Project context: $PROJECT_NAME ---
$PROJECT_DESC
--- End project context ---
"

DESIGN_CONTEXT=""
if [ -n "$TASKS_FILE" ]; then
  DESIGN_FILE="$(dirname "$TASKS_FILE")/design.md"
  if [ -f "$DESIGN_FILE" ]; then
    echo "  Found design: $DESIGN_FILE"
    DESIGN_CONTEXT="
--- UX/UI Design Artifacts ---
Read $DESIGN_FILE before implementing UI tasks.
--- End design context ---
"
  fi
fi

# Resolve the spec_dir for build_spec_context — same match we did for TASKS_FILE.
SPEC_DIR_MATCH=""
if [ -n "$TASKS_FILE" ]; then
  SPEC_DIR_MATCH="$(dirname "$TASKS_FILE")/"
fi
SPEC_CONTEXT=$(build_spec_context "$SPEC_DIR_MATCH")

# tasks.md is guaranteed by the time Build state is reached: spec-pipeline
# produced it via /speckit-tasks and spec-review aborts if it's missing. If we
# get here without one, the state machine is broken — fail loud and route the
# issue back to Spec for re-tasks.
if [ -z "$TASKS_FILE" ]; then
  echo "  ERROR: no tasks.md found on branch '$BRANCH' despite valid bureau-branch marker."
  post_comment "$ISSUE" "❌ Implement cannot run: \`tasks.md\` is missing on \`$BRANCH\` despite a valid bureau-branch marker. Routing back to Spec so /speckit-tasks can run again."
  move_issue "$ISSUE" "$BUREAU_STATE_SPEC"
  exit 13
fi

NEGATIVE_CONSTRAINTS=$(build_negative_constraints)

echo "  Found tasks: $TASKS_FILE"

# ─── retry loop ───────────────────────────────────────────────────────────
START_TS=$(date +%s)
RESULT=""
STATUS=""
TASKS_DONE_TOTAL=0
COMMITS_TOTAL=0
ITER_LOG=""
i=0
CLAUDE_EXIT=0

# EXP-token-efficiency — /goal-driven path. Closes the EXP-573 / EXP-571 /
# EXP-624 / EXP-627 stuck-detector lineage: instead of bash counting commits
# and parsing self-reported status per iter, delegate completion-evaluation
# to Haiku via Claude Code's `/goal` slash command. Haiku reads the
# transcript after every turn and decides whether the goal is met; the
# single $CLAUDE invocation only returns when Haiku says yes OR Claude
# stops after $MAX_ITER turns.
#
# The full work instructions (spec / project / design / review contexts +
# rules of engagement + JSON schema) go into --append-system-prompt; the
# goal condition itself stays under the documented 4000-char limit and
# describes only the verifiable end-state. parse_claude_json finds the last
# fenced JSON block in the combined transcript — same parse path the iter
# loop used, so the downstream PR / state-move / EXP-622 ready-flip logic
# is unchanged.
#
# Opt-in via .agents.use_goal_loop in .bureau.json (or BUREAU_USE_GOAL_LOOP=1).
# When off, the bash for-loop below runs verbatim — rollback is one flag flip.
if use_goal_loop_enabled; then
  echo "  /goal-driven implementation (use_goal_loop=true; iter loop disabled)"
  HEAD_BEFORE_RUN=$(git rev-parse HEAD)

  REVIEW_CONTEXT=$(refresh_review_context "$ISSUE")

  IMPL_SYSTEM="You are implementing $ISSUE on branch $BRANCH for $ISSUE_TITLE.
$SPEC_CONTEXT
$PROJECT_CONTEXT
$DESIGN_CONTEXT
$REVIEW_CONTEXT
Parent issue: $ISSUE — $ISSUE_TITLE
$ISSUE_DESC
Rules of engagement:
1. Read $TASKS_FILE for the full task list. If review feedback is present above, address those fixes BEFORE remaining tasks.
2. For each task in dependency order:
   a. Read adjacent files before writing new ones — match existing code style.
   b. Implement the smallest change that satisfies the task.
   c. If the task references tests, update or add them. Otherwise do not touch tests — the QA stage handles that.
   d. Commit: '$ISSUE: <task title>'. One task per commit.
   e. Mark the task [X] in $TASKS_FILE.
3. If a task is tagged 'needs-human' or '[Human]', skip and add a comment block at the intended location:
     // needs-human: <task-id> — <why this needs human judgement>

Stop conditions — halt and report status NEEDS_HUMAN when:
- A task requires information that isn't in the spec and cannot be derived from the code.
- A task conflicts with a pinned decision in SPEC.md / plan.md / CLAUDE.md.
- A task can't be implemented without breaking an existing test.
$NEGATIVE_CONSTRAINTS

End every turn with a fenced JSON status block — Haiku reads it to decide if the goal is met:
\`\`\`json
{
  \"status\": \"COMPLETE|PARTIAL|NEEDS_HUMAN|STUCK\",
  \"tasks_done\": 0,
  \"tasks_skipped\": 0,
  \"tasks_needs_human\": 0,
  \"fixed_review_items\": [],
  \"notes\": {
    \"needs_human\": [{\"task_id\": \"\", \"reason\": \"\"}],
    \"skipped\":     [{\"task_id\": \"\", \"reason\": \"\"}],
    \"deviations\":  [{\"task_id\": \"\", \"what\": \"\", \"why\": \"\"}]
  },
  \"prose_notes\": \"\"
}
\`\`\`
Do NOT emit COMPLETE without commits to back it — the bash post-check (and the goal evaluator) will catch lying-COMPLETE."

  GOAL_CONDITION="every '[ ]' checkbox in $TASKS_FILE has become '[X]' AND a fenced JSON block at the end of the turn reports status=COMPLETE with tasks_done > 0. Report status=PARTIAL+commit-summary if you got real work done but couldn't finish; status=NEEDS_HUMAN if a task requires info not in the spec; status=STUCK if no progress is possible. Stop after $MAX_ITER turns regardless."

  set +e
  if [ -n "$TIMEOUT_CMD" ]; then
    RESULT=$($TIMEOUT_CMD "$TOTAL_TIMEOUT" $CLAUDE --append-system-prompt "$IMPL_SYSTEM" "/goal $GOAL_CONDITION" 2>&1)
  else
    RESULT=$($CLAUDE --append-system-prompt "$IMPL_SYSTEM" "/goal $GOAL_CONDITION" 2>&1)
  fi
  CLAUDE_EXIT=$?
  set -e

  record_stage_cost "$RESULT" "$ISSUE" "implement"

  STATUS=$(parse_claude_json "$RESULT" '.status // "PARTIAL"')
  [ -z "$STATUS" ] && STATUS="PARTIAL"
  TASKS_DONE_TOTAL=$(parse_claude_json "$RESULT" '.tasks_done // 0')
  [[ "$TASKS_DONE_TOTAL" =~ ^[0-9]+$ ]] || TASKS_DONE_TOTAL=0
  HEAD_AFTER_RUN=$(git rev-parse HEAD)
  COMMITS_TOTAL=$(git rev-list --count "$HEAD_BEFORE_RUN..$HEAD_AFTER_RUN" 2>/dev/null || echo 0)

  ITER_LOG="  /goal: status=$STATUS tasks_done=$TASKS_DONE_TOTAL commits=$COMMITS_TOTAL"
  [ "$CLAUDE_EXIT" = 124 ] && ITER_LOG+=" (timed out at TOTAL_TIMEOUT=${TOTAL_TIMEOUT}s)"
  ITER_LOG+=$'\n'
  echo "$ITER_LOG"

  if [ "${BUREAU_DRY_RUN:-0}" = "1" ]; then
    echo "  [DRY_RUN] would: git push -u origin HEAD"
  else
    git push -u origin HEAD || true
  fi

  # Lying-COMPLETE backstop (same belt-and-suspenders the iter-loop path
  # carries via the post-loop EXP-571/EXP-624 check). Haiku is good but not
  # infallible; verify against the actual branch state.
  BRANCH_COMMITS_AHEAD=$(git rev-list --count "origin/main..HEAD" 2>/dev/null || echo 0)
  if [ "$STATUS" = "COMPLETE" ] && [ "$BRANCH_COMMITS_AHEAD" -eq 0 ]; then
    echo "  WARN: /goal reported COMPLETE but branch has no commits beyond origin/main — overriding to STUCK."
    STATUS="STUCK"
  fi
fi

if ! use_goal_loop_enabled; then
for (( i=1; i<=MAX_ITER; i++ )); do
  ELAPSED=$(( $(date +%s) - START_TS ))
  REMAINING=$(( TOTAL_TIMEOUT - ELAPSED ))
  if [ "$REMAINING" -le 60 ]; then
    echo "  Total wall-time cap exhausted (${ELAPSED}s elapsed of ${TOTAL_TIMEOUT}s). Stopping."
    STATUS="CAP_TIME"
    break
  fi

  THIS_TIMEOUT=$ITER_TIMEOUT
  [ "$THIS_TIMEOUT" -gt "$REMAINING" ] && THIS_TIMEOUT=$REMAINING

  echo ""
  echo "  → iter $i (per-iter timeout ${THIS_TIMEOUT}s)"

  # Re-fetch review feedback so mid-run human comments are seen by the next pass.
  REVIEW_CONTEXT=$(refresh_review_context "$ISSUE")

  HEAD_BEFORE=$(git rev-parse HEAD)

  # `set +e` around the Claude invocation: timeout-on-iter is normal flow, not
  # an error to bail on. We capture the exit code and decide. There is
  # deliberately no `trap ... EXIT` in this script — a hard crash bails via
  # `set -e` at the outer scope, queue-loop sees non-zero, alert fires, issue
  # stays in Build, next tick re-picks. The retry loop preserves that.
  PROMPT="Implement tasks from $TASKS_FILE for $ISSUE ($ISSUE_TITLE) on branch $BRANCH.

$SPEC_CONTEXT
$PROJECT_CONTEXT
$DESIGN_CONTEXT
$REVIEW_CONTEXT

Parent issue: $ISSUE — $ISSUE_TITLE
$ISSUE_DESC

Rules of engagement:
1. Read $TASKS_FILE for the full task list. If review feedback is present above, address those fixes BEFORE remaining tasks.
2. For each task in dependency order:
   a. Read adjacent files before writing new ones — match existing code style.
   b. Implement the smallest change that satisfies the task.
   c. If the task references tests, update or add them. Otherwise do not touch tests — the QA stage handles that.
   d. Commit: '$ISSUE: <task title>'. One task per commit.
   e. Mark the task [X] in $TASKS_FILE.
3. If a task is tagged 'needs-human' or '[Human]', skip the implementation and add a comment block at the intended location:
     // needs-human: <task-id> — <why this needs human judgement>

Stop conditions — do NOT guess; halt and report status NEEDS_HUMAN when:
- A task requires information that isn't in the spec and cannot be derived from the code.
- A task conflicts with a pinned decision in SPEC.md / plan.md / CLAUDE.md.
- A task can't be implemented without breaking an existing test.

$NEGATIVE_CONSTRAINTS

At the end of your work, emit a single fenced json block so the shell can summarise. Structured fields (needs_human / skipped / deviations) are auditable; \`prose_notes\` is for things that fit none of those buckets. \`fixed_review_items\` MUST list the specific fix-item IDs you addressed in this run (empty array if no review feedback was present).

\`\`\`json
{
  \"status\": \"COMPLETE|PARTIAL|NEEDS_HUMAN\",
  \"tasks_done\": 0,
  \"tasks_skipped\": 0,
  \"tasks_needs_human\": 0,
  \"fixed_review_items\": [],
  \"notes\": {
    \"needs_human\": [{\"task_id\": \"\", \"reason\": \"\"}],
    \"skipped\":     [{\"task_id\": \"\", \"reason\": \"\"}],
    \"deviations\":  [{\"task_id\": \"\", \"what\": \"\", \"why\": \"\"}]
  },
  \"prose_notes\": \"\"
}
\`\`\`"

  set +e
  if [ -n "$TIMEOUT_CMD" ]; then
    RESULT=$($TIMEOUT_CMD "$THIS_TIMEOUT" $CLAUDE "$PROMPT" 2>&1)
  else
    RESULT=$($CLAUDE "$PROMPT" 2>&1)
  fi
  CLAUDE_EXIT=$?
  set -e

  # EXP-671 — record this iteration's token usage + est. $ (no-op unless cost
  # tracking is enabled and the output carries a usage envelope).
  record_stage_cost "$RESULT" "$ISSUE" "implement"

  # CI cost control: amend HEAD's commit message with `[skip ci]` before the
  # iter push. When a PR already exists for $BRANCH (typical for review-cycle
  # re-picks), each push fires `pull_request: synchronize` and re-runs CI on
  # work that isn't even finished. The post-loop block adds one no-skip-ci
  # empty commit so CI runs exactly once on the final state.
  # Only amend when this iter actually produced new commits — empty iters
  # (Claude returned PARTIAL/STUCK without committing) skip the amend.
  HEAD_AFTER=$(git rev-parse HEAD)
  COMMITS_THIS_ITER=$(git rev-list --count "$HEAD_BEFORE..$HEAD_AFTER" 2>/dev/null || echo 0)
  if [ "$COMMITS_THIS_ITER" -gt 0 ]; then
    iter_msg=$(git log -1 --format=%B HEAD)
    case "$iter_msg" in
      *"[skip ci]"*) ;;
      *) git commit --amend -m "[skip ci] $iter_msg" --no-verify >/dev/null ;;
    esac
  fi

  # Push every iteration. queue-loop's reset_worktree hard-resets to origin
  # between picks (CLAUDE.md invariant 5) — unpushed commits would be wiped.
  if [ "${BUREAU_DRY_RUN:-0}" = "1" ]; then
    echo "  [DRY_RUN] would: git push -u origin HEAD"
  else
    git push -u origin HEAD || true
  fi

  STATUS=$(parse_claude_json "$RESULT" '.status // "PARTIAL"')
  [ -z "$STATUS" ] && STATUS="PARTIAL"
  TASKS_DONE=$(parse_claude_json "$RESULT" '.tasks_done // 0')
  [[ "$TASKS_DONE" =~ ^[0-9]+$ ]] || TASKS_DONE=0
  FIXED_REVIEW=$(parse_claude_json "$RESULT" '.fixed_review_items // [] | length')
  [[ "$FIXED_REVIEW" =~ ^[0-9]+$ ]] || FIXED_REVIEW=0
  TASKS_DONE_TOTAL=$(( TASKS_DONE_TOTAL + TASKS_DONE ))

  LINE="iter $i: status=$STATUS tasks_done=$TASKS_DONE commits=$COMMITS_THIS_ITER"
  [ "$CLAUDE_EXIT" = 124 ] && LINE+=" (timed out)"
  echo "    $LINE"
  ITER_LOG+="  $LINE"$'\n'

  COMMITS_TOTAL=$(( COMMITS_TOTAL + COMMITS_THIS_ITER ))

  # Single-strike stuck detector (EXP-573). Runs BEFORE the status-based
  # break so a model that self-reports PARTIAL with zero commits and zero
  # tasks done can't loop forever — force-park instead. Commits are the
  # load-bearing signal: self-reported tasks_done and fixed_review_items
  # are unverifiable hot air without a commit to back them up.
  #
  # COMPLETE skipped (EXP-571, brainhuggers-cli PR #109). status=COMPLETE
  # means "task list is done, no further work needed" — typical when
  # qa-pipeline bounced the ticket to Build after writing tests and
  # ticking them itself, and implement re-runs to find the production
  # code already present. Flagging that as STUCK is a false positive that
  # parks a mergeable ticket. This previously inverted EXP-573's "model
  # lies about COMPLETE" carve-out; the post-loop COMMITS_TOTAL==0 check
  # below remains as belt-and-suspenders for the lying case.
  #
  # NEEDS_HUMAN deliberately NOT in this case — an honest "I can't do this"
  # with zero work is the correct termination and we want it to flow
  # through cleanly rather than being mislabelled STUCK.
  #
  # FIXED_REVIEW also deliberately dropped from the check (was an AND in
  # the previous rule): models would self-report "I considered review
  # items" without committing anything, which let them through. The
  # commit/task floor is enough.
  if [ "$COMMITS_THIS_ITER" -eq 0 ] && [ "$TASKS_DONE" -eq 0 ]; then
    case "$STATUS" in
      PARTIAL)
        # EXP-627: only force-park to STUCK when no iter has committed.
        # A productive-then-exhausted run (commits early, dry late) is not
        # stuck — it's done with what was achievable. Let the loop exit
        # naturally at MAX_ITER with terminal status=PARTIAL so EXP-622's
        # ready-flip logic can take it from there. Promoting to STUCK
        # here would suppress that flip (it only matches PARTIAL) and
        # the PR would stay draft despite real commits landing.
        if [ "$COMMITS_TOTAL" -gt 0 ]; then
          echo "  No work this iteration, but prior iters produced ${COMMITS_TOTAL} commit(s) — letting loop continue toward MAX_ITER."
        else
          echo "  No work evidence this iteration and no prior commits — overriding status=$STATUS → STUCK."
          STATUS="STUCK"
          break
        fi
        ;;
    esac
  fi

  case "$STATUS" in
    COMPLETE|NEEDS_HUMAN) break ;;
  esac
done

# If the loop ran to completion without hitting a terminal break, status is
# either COMPLETE (rare — loop would have broken) or PARTIAL with progress.
# Normalise to PARTIAL so the case below handles it.
if [ "$i" -gt "$MAX_ITER" ] && [ "$STATUS" != "COMPLETE" ] && [ "$STATUS" != "NEEDS_HUMAN" ] && [ "$STATUS" != "STUCK" ] && [ "$STATUS" != "CAP_TIME" ]; then
  STATUS="PARTIAL"
fi

# Belt-and-suspenders for EXP-573: if the loop ended with COMPLETE but the
# branch has no commits beyond origin/main, the model is lying — override
# to STUCK so the issue is parked, not shipped.
#
# Branch-wide check, not COMMITS_TOTAL this tick (EXP-571 / EXP-624). The
# legit case the per-iter exemption above admits — qa-pipeline bounced the
# ticket to Build after writing tests, implement re-runs and sees nothing
# left to do — produces COMMITS_TOTAL=0 in THIS tick but the branch
# still has the prior implement-run's commits. Trusting branch state
# instead of this-tick state lets that path through while still parking
# a truly empty branch claimed as COMPLETE.
BRANCH_COMMITS_AHEAD=$(git rev-list --count origin/main..HEAD 2>/dev/null || echo 0)
if [ "$STATUS" = "COMPLETE" ] && [ "$BRANCH_COMMITS_AHEAD" -eq 0 ]; then
  echo "  WARN: terminal status=COMPLETE but branch has no commits beyond origin/main — overriding to STUCK."
  STATUS="STUCK"
fi
fi  # end of `if ! use_goal_loop_enabled` wrapper around iter-loop + post-loop overrides

echo ""
echo "Phase 2/2: terminal status=$STATUS (after $i iter(s))"

# CI cost control: pair with the per-iter `[skip ci]` amend above. The iter
# pushes don't trigger PR-sync CI; this empty commit at the end of the
# implement run does, so CI runs exactly once per implement-pipeline tick
# instead of once per iter. Skipped when nothing was committed — there's
# nothing for CI to check.
if [ "$COMMITS_TOTAL" -gt 0 ]; then
  if [ "${BUREAU_DRY_RUN:-0}" = "1" ]; then
    echo "  [DRY_RUN] would: git commit --allow-empty + push (CI checkpoint)"
  else
    git commit --allow-empty -m "$ISSUE: bureau implement checkpoint (CI re-trigger)" --no-verify >/dev/null
    git push origin HEAD || true
  fi
fi

PR_URL=""
case "$STATUS" in
  COMPLETE)
    # When QA is configured (opt-in), the implement pipeline routes through QA
    # instead of going straight to code review. QA runs the test suite and
    # writes missing tests before a reviewer sees the PR.
    if [ -n "${BUREAU_STATE_QA:-}" ]; then
      NEXT_STATE_LABEL="QA"
      NEXT_STATE_ID="$BUREAU_STATE_QA"
    else
      NEXT_STATE_LABEL="Build Review"
      NEXT_STATE_ID="$BUREAU_STATE_BUILD_REVIEW"
    fi

    PR_URL=$(open_or_update_pr_ready "$ISSUE" "$ISSUE_TITLE")
    post_comment "$ISSUE" "$(build_summary_comment COMPLETE "$TASKS_DONE_TOTAL" "$ITER_LOG" "$PR_URL")"
    move_issue "$ISSUE" "$NEXT_STATE_ID"
    echo "  Moved $ISSUE to $NEXT_STATE_LABEL"
    ;;

  NEEDS_HUMAN|STUCK|CAP_TIME|PARTIAL)
    # PARTIAL with real commits proceeds to downstream gates as ready-for-review
    # so CI fires on the ready_for_review transition (EXP-622 / FR-001). Every
    # other halt status — and PARTIAL with zero commits — stays draft (FR-002,
    # FR-003). The summary comment, needs-human label, escalation log, and
    # operator status report below are unchanged (FR-005).
    if [ "$STATUS" = "PARTIAL" ] && [ "$COMMITS_TOTAL" -gt 0 ]; then
      PR_URL=$(open_or_update_pr_ready "$ISSUE" "$ISSUE_TITLE")
    else
      PR_URL=$(open_or_update_pr_draft "$ISSUE" "$ISSUE_TITLE")
    fi
    PR_NUMBER=$(gh pr list --head "$BRANCH" --json number --jq '.[0].number' 2>/dev/null || echo "")
    if add_issue_label "$ISSUE" "needs-human"; then
      log_escalation "$ISSUE" "implement" "$i" \
        "$STATUS: $TASKS_DONE_TOTAL tasks done across $i iter(s)" \
        "${PR_NUMBER:-0}" "$BRANCH"
    else
      echo "  WARN: failed to add 'needs-human' label to $ISSUE; will retry on next tick" >&2
    fi
    post_comment "$ISSUE" "$(build_summary_comment "$STATUS" "$TASKS_DONE_TOTAL" "$ITER_LOG" "$PR_URL")"
    NEXT_STATE_LABEL="Build (needs-human)"
    ;;
esac

echo ""
echo "═══════════════════════════════════════"
echo "  Implement pipeline complete: $ISSUE"
echo "  Branch: $BRANCH"
echo "  PR: ${PR_URL:-existing}"
echo "  Status: $NEXT_STATE_LABEL ($STATUS)"
echo "═══════════════════════════════════════"
