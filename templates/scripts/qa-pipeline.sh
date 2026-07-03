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

if [ -f .env ]; then source .env
elif [ -f "$SCRIPT_REPO/.env" ]; then source "$SCRIPT_REPO/.env"
else echo "ERROR: No .env found"; exit 1; fi

CLAUDE=$(claude_cmd_for_stage "qa")
API_KEY="${LINEAR_API_KEY:?Set LINEAR_API_KEY in .env}"

precondition_linear

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
ACTUAL_STATE=$(get_issue_state "$ISSUE")
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

if ! merge_origin_main_or_abort "$ISSUE" "QA"; then
  move_issue "$ISSUE" "$BUREAU_STATE_BUILD"
  exit 17
fi

# Locate the spec dir so build_spec_context has something to load.
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
SPEC_CONTEXT=$(build_spec_context "$SPEC_DIR")
TASKS_FILE=""
[ -n "$SPEC_DIR" ] && [ -f "${SPEC_DIR}tasks.md" ] && TASKS_FILE="${SPEC_DIR}tasks.md"

# Test command detection — kept deliberately narrow. If a repo doesn't match
# one of these, the operator can wire a `scripts/bureau-test.sh` shim and
# re-run; don't let Claude invent test harnesses.
detect_test_cmd() {
  if [ -f "scripts/bureau-test.sh" ]; then
    echo "bash scripts/bureau-test.sh"; return
  fi
  if [ -f "package.json" ] && jq -e '.scripts.test' package.json >/dev/null 2>&1; then
    # Prefer a dedicated CI variant when present (avoids watch mode, etc).
    if jq -e '.scripts["test:ci"]' package.json >/dev/null 2>&1; then
      echo "npm run test:ci --silent"
    else
      echo "npm test --silent"
    fi
    return
  fi
  if [ -f "Cargo.toml" ]; then echo "cargo test --quiet"; return; fi
  if [ -f "pyproject.toml" ] || [ -f "pytest.ini" ] || [ -f "setup.cfg" ]; then
    echo "pytest -q"; return
  fi
  if [ -f "go.mod" ]; then echo "go test ./..."; return; fi
  echo ""
}

TEST_CMD=$(detect_test_cmd)
if [ -z "$TEST_CMD" ]; then
  echo "  No test harness detected — skipping QA. Moving straight to Build Review."
  post_comment "$ISSUE" "ℹ️ QA pipeline: no test harness detected in this repo. Skipping QA and moving to Build Review."
  move_issue "$ISSUE" "$BUREAU_STATE_BUILD_REVIEW"
  exit 0
fi
echo "  Test command: $TEST_CMD"

# Persisted per-QA-run log. The Linear comment surfaces a path pointer
# (rather than truncated stdout) so the operator can `tail -200` the full
# pane buffer when deciding flake-vs-real. Lives under logs/ alongside
# escalations.log; survives until the next worktree reset.
mkdir -p logs
QA_LOG_TS=$(date -u +%Y-%m-%dT%H-%M-%S)
QA_LOG_PATH="logs/qa-$ISSUE-$QA_LOG_TS.log"

QA_TMP=$(mktemp -d)
_qa_cleanup() {
  local rc=$?
  if [ "$rc" = 0 ] || [ "$rc" = 2 ]; then rm -rf "$QA_TMP"; fi
}
trap _qa_cleanup EXIT

echo ""
echo "Phase 1/3: initial test run (no Claude call)"
if eval "$TEST_CMD" > "$QA_TMP/test1.log" 2>&1; then
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
  # real bugs that should land in needs-human. See EXP-487: parked on a
  # `running 0 tests` / `target failed` flake that passed cleanly on rerun.
  sleep 5
  if eval "$TEST_CMD" > "$QA_TMP/test1.retry.log" 2>&1; then
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

  QA_RESULT=$($CLAUDE "You are the QA agent for $ISSUE ($ISSUE_TITLE) on branch $BRANCH.

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

Emit a fenced json block at the very end:

\`\`\`json
{\"status\":\"GREEN|RED|NEEDS_HUMAN\",\"tests_added\":0,\"tests_failing\":0,\"coverage_notes\":\"\"}
\`\`\`" 2>&1)
  echo "$QA_RESULT"
fi

# Commit any changes Claude made (tests are additive — a dirty worktree is expected).
if ! git diff --quiet || ! git diff --cached --quiet; then
  git add -A
  git commit -m "$ISSUE: qa adjustments" --allow-empty || true
  if [ "${BUREAU_DRY_RUN:-0}" = "1" ]; then
    echo "  [DRY_RUN] would: git push origin HEAD ($BRANCH)"
  else
    git push origin HEAD || true
  fi
fi

echo ""
echo "Phase 3/3: final test run + route"
if eval "$TEST_CMD" > "$QA_TMP/test2.log" 2>&1; then
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

# Claude's self-reported status and the objective suite result must agree; if
# they don't, the suite is the oracle.
if [ "$FINAL_GREEN" = true ] && [ "$STATUS" = "RED" ]; then STATUS="GREEN"; fi
if [ "$FINAL_GREEN" = false ] && [ "$STATUS" = "GREEN" ]; then STATUS="RED"; fi

SUMMARY=$(parse_claude_json "$QA_RESULT" '.coverage_notes // "no notes"')

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
    echo "  QA: NEEDS_HUMAN — flagging and leaving in QA"
    if add_issue_label "$ISSUE" "needs-human"; then
      log_escalation "$ISSUE" "qa" 0 "QA flagged NEEDS_HUMAN" 0 "$BRANCH"
    else
      echo "  WARN: failed to add 'needs-human' label to $ISSUE; will retry on next tick" >&2
    fi
    post_comment "$ISSUE" "🚫 QA flagged for human review.

$SUMMARY

Full QA log: \`$QA_LOG_PATH\`"
    NEXT_STATE="QA (needs-human)"
    ;;
  RED|*)
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
