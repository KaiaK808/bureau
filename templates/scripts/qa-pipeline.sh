#!/bin/bash
# QA pipeline: Pick QA issue → run test suite → write missing tests → route to
# Build Review (green) / Build (red) / needs-human (suite broken).
#
# Opt-in: this script is a no-op if .bureau.json lacks a states.qa UUID. The
# dispatcher in queue-loop.sh should also gate this pipeline via
# `agent_enabled qa` before invoking it.
set -euo pipefail

unset CLAUDECODE 2>/dev/null || true

REPO_DIR="$(pwd)"
SCRIPT_REPO="$(cd "$(dirname "$0")/.." && pwd)"
source "$(dirname "$0")/bureau-config.sh"

BUREAU_ENV_FILE="${BUREAU_ENV_FILE:-$SCRIPT_REPO/.env}"
# BUREAU_ENV_FILE only, never ./.env: in a stage worktree that is a file the branch controls.
if [ -f "$BUREAU_ENV_FILE" ]; then bureau_load_env --export "$BUREAU_ENV_FILE"
else bureau_secret_set LINEAR_API_KEY || { echo "ERROR: Set LINEAR_API_KEY"; exit 1; }; fi

CLAUDE=(run_stage_for qa)
bureau_secret_copy API_KEY LINEAR_API_KEY

precondition_linear
precondition_runner qa

# Opt-in gate — no configured QA state ⇒ nothing to do, treat as queue-empty.
if [ -z "${BUREAU_STATE_QA:-}" ]; then
  echo "QA pipeline: no states.qa in .bureau.json — QA disabled. Exiting as queue-empty."
  exit 2
fi

if [ -n "${1:-}" ]; then
  ISSUE="$1"
  echo "Using specified issue: $ISSUE"
else
  echo "Picking next QA issue..."
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
bureau_ticket_checkpoint "$ISSUE" latched
if [ "$ACTUAL_STATE" != "QA" ]; then
  echo "  WARNING: $ISSUE is in '$ACTUAL_STATE', not 'QA'. Skipping."
  exit 2
fi

echo ""
echo "═══════════════════════════════════════"
echo "  QA Pipeline: $ISSUE"
echo "═══════════════════════════════════════"
echo ""

ISSUE_DETAIL=$(get_issue_detail "$ISSUE")
ISSUE_TITLE=$(echo "$ISSUE_DETAIL" | jq -r '.title // empty')
echo "  $ISSUE: $ISSUE_TITLE"

echo "→ Finding branch..."
BRANCH=$(get_issue_branch "$ISSUE")
if [ -z "$BRANCH" ] || [[ "$BRANCH" == *" "* ]]; then
  echo "  ERROR: no bureau-branch marker found for $ISSUE."
  post_comment "$ISSUE" "❌ QA pipeline cannot start — no bureau-branch marker. Moving back to Build."
  move_issue "$ISSUE" "$BUREAU_STATE_BUILD"
  exit 12
fi
echo "  Branch: $BRANCH"
bureau_record_branch_pr "$BRANCH" || true

git fetch origin
free_branch_from_other_worktrees "$BRANCH" "$(pwd)"
if git rev-parse --verify "origin/$BRANCH" >/dev/null 2>&1; then
  git checkout -B "$BRANCH" "origin/$BRANCH"
elif git rev-parse --verify "$BRANCH" >/dev/null 2>&1; then
  git checkout "$BRANCH"
else
  echo "  ERROR: branch '$BRANCH' not found locally or on origin."
  post_comment "$ISSUE" "❌ QA pipeline cannot start — branch \`$BRANCH\` does not exist. Moving back to Build."
  move_issue "$ISSUE" "$BUREAU_STATE_BUILD"
  exit 12
fi

MERGE_RC=0
merge_origin_main_or_abort "$ISSUE" "QA" || MERGE_RC=$?
[ "$MERGE_RC" = 0 ] || exit "$MERGE_RC"

# After the checkout AND the merge of origin/main — both can change the
# manifests. Idempotent: does nothing when node_modules already matches. A
# failure here is infrastructure (registry, disk), not the PR: stop with 24
# (environment-blocked) instead of building red and judging someone's code.
restore_worktree_deps "$(pwd)" || exit 24

# Locate the spec dir so build_spec_context has something to load.
SPEC_DIR=$(bureau_spec_dir_for_branch "$BRANCH")
SPEC_CONTEXT=$(build_spec_context "$SPEC_DIR")
TASKS_FILE=""
[ -n "$SPEC_DIR" ] && [ -f "${SPEC_DIR}tasks.md" ] && TASKS_FILE="${SPEC_DIR}tasks.md"

# Test command detection — kept deliberately narrow. If a repo doesn't match
# one of these, the operator can wire a `scripts/bureau-test.sh` shim and
# re-run; don't let Claude invent test harnesses.
detect_test_cmd() {
  local configured
  configured=$(bureau_get '.repo.test_command // empty')
  printf '%s' "$configured" | bureau_without_secrets python3 -I "$(dirname "$BUREAU_RUNTIME")/bureau-gates.py"
}

TEST_CMD=$(detect_test_cmd)
if [ -z "$TEST_CMD" ]; then
  echo "  No test harness detected — skipping QA. Moving straight to Build Review."
  post_comment "$ISSUE" "ℹ️ QA pipeline: no test harness detected in this repo. Skipping QA and moving to Build Review."
  move_issue "$ISSUE" "$BUREAU_STATE_BUILD_REVIEW"
  exit 0
fi
echo "  Test command: $TEST_CMD"
# PR code: every test run below goes through bureau_untrusted_env (bureau-env.sh), in a
# child bash under pipefail like the review build check, never through `eval` in this shell.
bureau_untrusted_env --check || exit 24

# Persisted per-QA-run log. The Linear comment surfaces a path pointer
# (rather than truncated stdout) so the operator can `tail -200` the full
# pane buffer when deciding flake-vs-real. Lives under logs/ alongside
# escalations.log until a worktree reset. Run evidence retains only full-stream
# digests, sizes and command IDs/digests outside resets, without a log copy.
mkdir -p logs
QA_LOG_TS=$(date -u +%Y-%m-%dT%H-%M-%S)
QA_LOG_PATH="logs/qa-$ISSUE-$QA_LOG_TS.log"

QA_TMP=$(mktemp -d)
QA_GATE_CONTEXT="$QA_TMP/gate-context.json"
_qa_cleanup() {
  local rc=$?
  if [ -f "${QA_LOG_PATH:-}" ]; then bureau_archive_evidence_dir_log || true; fi
  # The full log is already in $QA_LOG_PATH when a needs-human arm ends with 25
  # because its label could not be written; nothing to keep there either.
  if [ "$rc" = 0 ] || [ "$rc" = 2 ] || [ "${NEEDS_HUMAN_UNMARKED:-0}" = 1 ]; then rm -rf "$QA_TMP" || true; fi
  return "$rc"
}
trap _qa_cleanup EXIT

bureau_archive_evidence_dir_log() {
  [ -n "${BUREAU_RUN_ID:-}" ] && [ -n "${BUREAU_STAGE_ATTEMPT_ID:-}" ] || return 0
  bureau_evidence evidence-gate --kind artifact --path "$QA_LOG_PATH" --head '' \
    --end-head '' --command 'QA summary; gate results recorded separately' --command-id qa.summary || true
}

qa_run_gate() {
  local head rc=0
  head=$(git rev-parse HEAD 2>/dev/null) || head=""
  bureau_untrusted_env bash --noprofile --norc -o pipefail -c "$TEST_CMD" > "$1" 2>&1 || rc=$?
  bureau_record_gate "$1" "$head" "$TEST_CMD" "$rc" qa.test || true
  return "$rc"
}

echo ""
echo "Phase 1/3: initial test run (no Claude call)"
if qa_run_gate "$QA_TMP/test1.log"; then
  echo "  Initial test run: PASSED"
  GREEN_ON_FIRST_TRY=true
  { echo "=== Phase 1/3: initial test run (PASSED) ==="; cat "$QA_TMP/test1.log"; } > "$QA_LOG_PATH"
else
  RC=$?
  echo "  Initial test run: FAILED (exit $RC) — retrying once before engaging Claude…"
  # Flake mitigation: the QA stage is the only mechanically-observable stage
  # (no Claude judgement in Phase 1), so a single retry on non-zero exit
  # cheaply distinguishes test-runner flakes from real failures. ONE retry
  # only — two attempts catches the common single-shot races (parallel
  # cargo, stale target/, transient network); three would mask intermittent
  # real bugs that should land in needs-human. An installation parked on a
  # `running 0 tests` / `target failed` flake that passed cleanly on rerun.
  sleep 5
  if qa_run_gate "$QA_TMP/test1.retry.log"; then
    echo "  Retry passed. First run was a flake."
    {
      echo "=== Phase 1/3: initial test run (FLAKE — exit $RC) ==="
      cat "$QA_TMP/test1.log"
      echo
      echo "=== Phase 1/3 retry: PASSED ==="
      cat "$QA_TMP/test1.retry.log"
    } > "$QA_LOG_PATH"
    post_comment "$ISSUE" "ℹ️ qa-pipeline retry-1: initial test run exited $RC, retry passed — treating as flake. Full log: \`$QA_LOG_PATH\`."
    # Replace test1.log with the passing retry so downstream coverage-check
    # logic (Phase 2/3 with TASKS_FILE) reads the green output, not the flake.
    cp "$QA_TMP/test1.retry.log" "$QA_TMP/test1.log"
    GREEN_ON_FIRST_TRY=true
  else
    echo "  Retry also failed — engaging Claude to diagnose."
    {
      echo "=== Phase 1/3: initial test run (exit $RC) ==="
      cat "$QA_TMP/test1.log"
      echo
      echo "=== Phase 1/3 retry: also failed ==="
      cat "$QA_TMP/test1.retry.log"
    } > "$QA_LOG_PATH"
    GREEN_ON_FIRST_TRY=false
  fi
fi
TEST_LOG_TAIL=$(tail -n 80 "$QA_TMP/test1.log" 2>/dev/null || echo "")

echo ""
echo "Phase 2/3: coverage / repair pass"

if [ "$GREEN_ON_FIRST_TRY" = true ] && [ -z "$TASKS_FILE" ]; then
  # Green tests, no tasks.md — nothing for Claude to do. Skip straight to routing.
  QA_RESULT="$(printf '```json\n{"status":"GREEN","tests_added":0,"tests_failing":0,"coverage_notes":"Tests passed on first run; no tasks.md to cross-check coverage."}\n```')"
else
  # Either tests failed, or they passed but we have a task list to check coverage against.
  # Either way, Claude runs. Intent diverges per branch below via the prompt.
  QA_PROMPT_INTENT=""
  if [ "$GREEN_ON_FIRST_TRY" = true ]; then
    QA_PROMPT_INTENT="Tests PASSED on first run. Your job is coverage only: for each task in $TASKS_FILE, check that at least one test exercises its acceptance criteria. Add tests only for tasks with zero coverage — do not rewrite existing tests, do not refactor source. If every task is covered, emit status GREEN with tests_added: 0 and exit without modifications."
  else
    QA_PROMPT_INTENT="Tests FAILED. Your job is to diagnose and, where the implementation is correct, fix the tests. Read the failure log, inspect the source, then:
- If the test is broken (stale fixture, missing mock, syntax error) and the implementation is correct — fix the test.
- If the TEST reveals a correctness bug in the implementation — STOP. Do NOT edit source to make the test pass. Emit status NEEDS_HUMAN with coverage_notes pointing to the bug.
- If the harness itself is broken (missing dep, config error) — emit status NEEDS_HUMAN."
  fi

  NEGATIVE_CONSTRAINTS_BODY=$(build_negative_constraints)

  # Codex needs a sandbox diagnosis the shell can reconcile with Phase 3.
  # Claude gets no note: its prompt stays byte for byte as before.
  if [ "$(resolve_runner_for_stage qa)" = codex ]; then
    SANDBOX_NOTE="Sandbox: this turn runs inside the Codex sandbox, where binding or listening on a socket, network access and the per-user temp directory can be denied. The test output above comes from the shell outside the sandbox. A test that fails inside the sandbox only for such a reason is not a test failure. Never change, skip or deselect a test to get around the sandbox. If the sandbox is the only thing keeping you from confirming the suite, report status NEEDS_HUMAN with coverage_notes beginning SANDBOX_GATE: and naming the tests and the denied operation. The shell's final run outside the sandbox then decides GREEN or RED. A correctness bug, or a harness that is broken outside the sandbox, stays NEEDS_HUMAN without the prefix."
    SANDBOX_NOTE+=" For a failed or aborted configured gate, you may instead report an exhaustive blockers array, each object with kind gate, command equal to the exact test command above and a free-text reason; coverage_notes needs no prefix. Include every independent blocker with kind product, security or human and an empty command. Only actual Codex command executions at the current HEAD qualify; a reference alone is no evidence. Run the configured command unchanged from the repository root. Never label product, security or decision blockers as gates."
    NEGATIVE_CONSTRAINTS_BODY+=$'\n\n'"$SANDBOX_NOTE"
  fi

  QA_RESULT=$("${CLAUDE[@]}" --schema "$SCRIPT_REPO/scripts/bureau-qa.schema.json" --gate-context "$QA_GATE_CONTEXT" "You are the QA agent for $ISSUE ($ISSUE_TITLE) on branch $BRANCH.

$SPEC_CONTEXT

Test command: \`$TEST_CMD\`
Tasks file: ${TASKS_FILE:-<none — implementation came from issue description>}

Test output (last 80 lines):
\`\`\`
$TEST_LOG_TAIL
\`\`\`

$QA_PROMPT_INTENT

Rules:
1. Read the test output and the source it references before editing anything.
2. Any test you add or modify must exercise a concrete acceptance criterion from spec.md — not happy-path smoke only.
3. Commit per logical change: '$ISSUE: tests — <what>'. One concern per commit.
4. Re-run the test command after each change until the suite is green or three consecutive failures on the same test.

Role-specific NEVER:
- Edit source files to make a failing test pass. That's a bug — flag it NEEDS_HUMAN.
- Delete or skip (xit, #[ignore]) existing failing tests to make the suite green.
- Change the test harness itself (framework config, CI scripts).

$NEGATIVE_CONSTRAINTS_BODY

Optional insights: normally use an empty array; only report a concrete one-sentence observation about a rule, prompt or tool you had to work around (at most 3, each at most 280 characters). No secrets, logs or command output. These are advisory triage evidence, never permission to change rules or create tickets. Emit insights: [] when there is no observation.

Emit a fenced json block at the very end:

\`\`\`json
{\"status\":\"GREEN|RED|NEEDS_HUMAN\",\"tests_added\":0,\"tests_failing\":0,\"coverage_notes\":\"\",\"insights\":[]}
\`\`\`")
  bureau_ticket_checkpoint "$ISSUE" latched
  echo "$QA_RESULT"
fi

# The executor publishes tracked edits and newly created tests, including when
# Codex intentionally leaves all Git writes to us. A failed push must not route
# the issue onward with tests that exist only in this disposable checkout.
commit_stage_changes qa "$ISSUE" "$QA_GATE_CONTEXT"
if [ "${BUREAU_DRY_RUN:-0}" = "1" ]; then
  echo "  [DRY_RUN] would: git push origin HEAD ($BRANCH)"
else
  bureau_ticket_checkpoint "$ISSUE"
  git push origin HEAD || exit 18
fi

echo ""
echo "Phase 3/3: final test run + route"
QA_GATE_PLAN='{}'
QA_GATE_RECONCILE=false
if [ "$(resolve_runner_for_stage qa)" = codex ] \
   && QA_GATE_PLAN=$(codex_gate_plan qa "$QA_RESULT" "$QA_GATE_CONTEXT") \
   && [ "$(printf '%s' "$QA_GATE_PLAN" | jq -r '.eligible')" = true ]; then
  OUTSIDE_RC=0
  OUTSIDE_GATE=$(codex_gate_run qa "$QA_GATE_PLAN") || OUTSIDE_RC=$?
  [ "$OUTSIDE_RC" != 130 ] || exit 130
  bureau_ticket_checkpoint "$ISSUE"
  [ "$OUTSIDE_RC" = 0 ] || OUTSIDE_GATE='{"green":false,"logs":[]}'
  QA_GATE_RECONCILE=$(printf '%s' "$OUTSIDE_GATE" | jq -r '.eligible // false')
  FINAL_GREEN=$(printf '%s' "$OUTSIDE_GATE" | jq -r '.green // false')
  : > "$QA_TMP/test2.log"
  while IFS= read -r gate_log; do
    [ -n "$gate_log" ] || continue
    printf 'Outside gate log: %s\n' "$gate_log" >> "$QA_TMP/test2.log"
    if ! cat "$gate_log" >> "$QA_TMP/test2.log"; then
      printf 'Outside gate log unavailable: %s\n' "$gate_log" >> "$QA_TMP/test2.log"
      FINAL_GREEN=false
      QA_GATE_RECONCILE=false
    fi
  done < <(printf '%s' "$OUTSIDE_GATE" | jq -r '.logs[]?')
elif qa_run_gate "$QA_TMP/test2.log"; then
  FINAL_GREEN=true
else
  FINAL_GREEN=false
fi
{
  echo
  echo "=== Phase 3/3: final test run ($([ "$FINAL_GREEN" = true ] && echo PASSED || echo FAILED)) ==="
  cat "$QA_TMP/test2.log"
} >> "$QA_LOG_PATH"

STATUS=$(parse_claude_json "$QA_RESULT" '.status // empty')
[ -z "$STATUS" ] && STATUS=$([ "$FINAL_GREEN" = true ] && echo "GREEN" || echo "RED")

# The shell gate after a Codex QA turn. Only reconciled gate-only blockers let
# Phase 3 decide GREEN or RED; other blockers stay held.
QA_SANDBOX_GATE_LINE=""
if [ "$QA_GATE_RECONCILE" = true ]; then
  STATUS=$([ "$FINAL_GREEN" = true ] && echo "GREEN" || echo "RED")
  QA_SANDBOX_GATE_LINE="shell gate outside the Codex sandbox $([ "$FINAL_GREEN" = true ] && echo green || echo red): QA continues as $STATUS"
  echo "  $QA_SANDBOX_GATE_LINE"
fi
# Even a claimed GREEN cannot discard declared blockers without reconciliation.
if [ "$QA_GATE_RECONCILE" != true ] && [ "$(resolve_runner_for_stage qa)" = codex ]; then
  DECLARED_HOLDS=$(parse_claude_json "$QA_RESULT" '.blockers // [] | length')
  if [[ "$DECLARED_HOLDS" =~ ^[0-9]+$ ]] && [ "$DECLARED_HOLDS" -gt 0 ]; then STATUS=NEEDS_HUMAN; fi
fi

# Claude's self-reported status and the objective suite result must agree; if
# they don't, the suite is the oracle.
if [ "$FINAL_GREEN" = true ] && [ "$STATUS" = "RED" ] && [ "$(resolve_runner_for_stage qa)" != codex ]; then STATUS="GREEN"; fi
if [ "$FINAL_GREEN" = false ] && [ "$STATUS" = "GREEN" ]; then STATUS="RED"; fi

# Comment-only projection; its failure must not change the status/routing above.
SUMMARY=$(bureau_comment_projection "$(parse_claude_json "$QA_RESULT" '.coverage_notes // "no notes"')")
if [ -n "${QA_SANDBOX_GATE_LINE:-}" ]; then
  SUMMARY="$QA_SANDBOX_GATE_LINE

$SUMMARY"
fi

# The squash-range check, the same one the implement stage runs before its
# hand-off: after the push above, so this stage's work is already on origin,
# and before anything is routed on. Clean is one extra line. Not clean — a
# commit in origin/main..HEAD carries a CI suppressor, or the range could not
# be checked — means NEEDS_HUMAN whatever the suite said: GREEN would hand the
# branch to Build Review, and RED would send it back to Build for a whole agent
# round before the implement stage reports the same finding. The report goes
# first in the summary, the escalation entry names it, the PR gets it too.
QA_ESCALATION_REASON="QA flagged NEEDS_HUMAN"
check_squash_range origin/main
if [ "$SQUASH_CHECK" = "clean" ]; then
  echo "  $SQUASH_REPORT"
else
  echo "$SQUASH_REPORT" >&2
  STATUS="NEEDS_HUMAN"
  SUMMARY="$SQUASH_REPORT

$SUMMARY"
  QA_ESCALATION_REASON="squash-range check $SQUASH_CHECK: a commit message carries a CI suppressor or the range could not be checked"
  comment_on_branch_pr "$BRANCH" "$SQUASH_REPORT"
fi

NEEDS_HUMAN_UNMARKED=0
case "$STATUS" in
  GREEN)
    echo "  QA: GREEN — moving to Build Review"
    post_comment "$ISSUE" "✅ QA **PASSED**. Moving to Build Review.

$SUMMARY

Full QA log: \`$QA_LOG_PATH\`"
    move_issue "$ISSUE" "$BUREAU_STATE_BUILD_REVIEW"
    NEXT_STATE="Build Review"
    ;;
  NEEDS_HUMAN)
    if [ "$FINAL_GREEN" = false ]; then bureau_outcome_signal gate_red || true; else bureau_outcome_signal human_hold || true; fi
    echo "  QA: NEEDS_HUMAN — flagging and leaving in QA"
    if mark_needs_human "$ISSUE" qa; then
      log_escalation "$ISSUE" "qa" 0 "$QA_ESCALATION_REASON" 0 "$BRANCH"
    else
      NEEDS_HUMAN_UNMARKED=1
    fi
    post_comment "$ISSUE" "🚫 QA flagged for human review.

$SUMMARY

Full QA log: \`$QA_LOG_PATH\`"
    NEXT_STATE="QA (needs-human)"
    ;;
  RED|*)
    if [ "$FINAL_GREEN" = false ]; then bureau_outcome_signal gate_red || true; fi
    echo "  QA: RED — moving back to Build"
    post_comment "$ISSUE" "🔄 QA: tests failing — routing back to Build.

$SUMMARY

Full QA log: \`$QA_LOG_PATH\`"
    move_issue "$ISSUE" "$BUREAU_STATE_BUILD"
    NEXT_STATE="Build (rework)"
    ;;
esac

echo ""
echo "═══════════════════════════════════════"
echo "  QA complete: $ISSUE"
echo "  Branch: $BRANCH"
echo "  Status: $STATUS"
echo "  Next: $NEXT_STATE"
echo "═══════════════════════════════════════"

# A needs-human escalation whose label could not be written must not read as
# success to the driver: the local hold keeps the queue away, the
# non-zero exit stops a shepherd.
if [ "$NEEDS_HUMAN_UNMARKED" = 1 ]; then exit 25; fi
