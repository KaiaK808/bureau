#!/bin/bash
# Verifies shepherd.sh drives a ticket through every phase in the correct
# order, halts at --no-merge, prints a dry-run route, and detects stuck
# pipelines.
#
# Sandbox structure:
#   $SANDBOX/.bureau.json         (minimal config so shepherd doesn't bail)
#   $SANDBOX/.env                 (stub LINEAR_API_KEY)
#   $SANDBOX/scripts/bureau-config.sh   (STUB — overrides every helper that
#                                        would talk to Linear/claude/tmux)
#   $SANDBOX/scripts/shepherd.sh        (REAL — copied from templates)
#   $SANDBOX/scripts/*-pipeline.sh      (STUBS — log invocation, advance state)
#   $SANDBOX/state.txt            (current state UUID; mutated by move_issue stub)
#   $SANDBOX/invocations.log      (pipelines that have been called, in order)
#
# Each scenario is its own scope so state files reset between tests.

# NOTE: no `set -e`. The assertion helpers below return non-zero on failure;
# the test runner aggregates results and reports a per-scenario summary.
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "$0")" && cd .. && pwd)"
REAL_SHEPHERD="$REPO_ROOT/templates/scripts/shepherd.sh"
SANDBOX_ROOT=$(mktemp -d -t bureau-test.shepherd.XXXXXXXX)
trap 'rm -rf "$SANDBOX_ROOT"' EXIT

# ── Build a fresh sandbox for one scenario ──────────────────────────
make_sandbox() {
  local name="$1"
  local sb="$SANDBOX_ROOT/$name"
  mkdir -p "$sb/scripts"

  cat > "$sb/.env" <<'EOF'
LINEAR_API_KEY=stub
EOF

  # .bureau.json is read only at source-time for a few jq lookups
  # (BUREAU_BRANCH_PREFIX etc). The state-uuid vars below are what shepherd
  # actually uses for state mapping.
  cat > "$sb/.bureau.json" <<'EOF'
{
  "linear": {
    "teams": [{"id": "t", "key": "EXP", "name": "T",
      "states": {"triage":"s1","spec":"s2","spec_review":"s3","design":"s4",
                 "build":"s5","build_review":"s6","done":"s8"}}],
    "labels": {
      "lane2":{"id":"l1","name":"lane-2"},
      "needs_human":{"id":"l2","name":"needs-human"},
      "needs_ux":{"id":"l3","name":"needs-ux"},
      "ai_implementable":{"id":"l4","name":"ai-implementable"}
    },
    "projects": []
  },
  "agents": {"poll_interval_minutes": 30, "max_review_cycles": 3,
             "spec": true, "spec_review": true, "ux": true, "implement": true,
             "qa": false, "code_review": true, "merge": true},
  "repo": {"branch_prefix": "feat", "specs_dir": "specs"}
}
EOF

  # STUB bureau-config.sh — same path shepherd.sh sources, defines every
  # helper shepherd uses as a no-op or simulated mutation. The state machine
  # is a single file ($SANDBOX/state.txt) holding the current UUID.
  cat > "$sb/scripts/bureau-config.sh" <<'STUB_EOF'
#!/bin/bash
# STUB bureau-config.sh for shepherd test. Defines every helper shepherd
# touches. The "Linear state machine" is a single file at $STATE_FILE.

# Sandbox handles
STATE_FILE="${STATE_FILE:-$PWD/state.txt}"
INVOCATIONS_LOG="${INVOCATIONS_LOG:-$PWD/invocations.log}"
LABEL_LOG="${LABEL_LOG:-$PWD/labels.log}"

# State UUID layout (mirrors .bureau.json)
export BUREAU_STATE_TRIAGE="s1"
export BUREAU_STATE_SPEC="s2"
export BUREAU_STATE_SPEC_REVIEW="s3"
export BUREAU_STATE_DESIGN="s4"
export BUREAU_STATE_BUILD="s5"
export BUREAU_STATE_BUILD_REVIEW="s6"
export BUREAU_STATE_MERGE="s7"
export BUREAU_STATE_DONE="s8"

# Non-state config
export BUREAU_BRANCH_PREFIX="feat"
export BUREAU_SPECS_DIR="specs"

# Preconditions: no-op success
precondition_linear() { return 0; }
precondition_claude_auth() { return 0; }

# State helpers — UUID ↔ name map
_uuid_to_name() {
  case "$1" in
    s1) echo "Triage" ;;
    s2) echo "Spec" ;;
    s3) echo "Spec Review" ;;
    s4) echo "Design" ;;
    s5) echo "Build" ;;
    s6) echo "Build Review" ;;
    s7) echo "Merge" ;;
    s8) echo "Done" ;;
    *)  echo "" ;;
  esac
}

get_issue_state() {
  local uuid
  uuid=$(cat "$STATE_FILE" 2>/dev/null || echo "")
  _uuid_to_name "$uuid"
}

move_issue() {
  local _issue="$1" uuid="$2"
  printf '%s' "$uuid" > "$STATE_FILE"
  echo "[stub] move_issue $_issue → $(_uuid_to_name "$uuid") ($uuid)" >&2
}

# Label / comment helpers — log only
# Move the +/- sigil into the argument rather than the format so printf
# doesn't parse a leading '-' format as a flag (which would fail under set -u).
add_issue_label()    { printf '%s\t%s\n' "+$1" "$2" >> "$LABEL_LOG"; }
remove_issue_label() { printf '%s\t%s\n' "-$1" "$2" >> "$LABEL_LOG"; }
post_comment()       { :; }
alert_telegram()     { :; }

# Branch resolution — return a fixed dummy branch.
# (No ${var,,} lowercase expansion — bash 3.2 on macOS doesn't support it.)
get_issue_branch() { echo "feat/$1-stub"; }

# Worktree helpers — create the directory so shepherd's `cd "$WORKTREE"`
# succeeds. The real reset_worktree guarantees the dir exists after the call.
reset_worktree()                  { mkdir -p "$1"; }
free_branch_from_other_worktrees(){ :; }

# EXP-670 — shepherd's stage loop calls this before each stage; no-op in the
# test (the real guard pauses on near-limit usage, no-ops without a signal).
session_throttle_guard()          { return 0; }

# Exit-code → class (real logic; pure)
exit_class() {
  case "$1" in
    0)   echo "ok" ;;
    2)   echo "queue-empty" ;;
    10)  echo "linear-down" ;;
    11)  echo "worktree-dirty" ;;
    12)  echo "no-branch" ;;
    13)  echo "no-tasks" ;;
    14)  echo "build-failed" ;;
    15)  echo "no-pr" ;;
    16)  echo "claude-unauth" ;;
    *)   echo "error-$1" ;;
  esac
}
STUB_EOF

  # Stub pipelines: log invocation, advance to the next happy-path state.
  _make_stub_pipeline() {
    local name="$1" next_uuid="$2"
    cat > "$sb/scripts/$name" <<PIPELINE_EOF
#!/bin/bash
set -euo pipefail
source "\$(dirname "\$0")/bureau-config.sh"
ISSUE="\${1:-}"
echo "$name" >> "\$INVOCATIONS_LOG"
move_issue "\$ISSUE" "$next_uuid"
exit 0
PIPELINE_EOF
    chmod +x "$sb/scripts/$name"
  }

  _make_stub_pipeline spec-pipeline.sh        "$BUREAU_STATE_SPEC_REVIEW_SIM"
  _make_stub_pipeline spec-review-pipeline.sh "$BUREAU_STATE_BUILD_SIM"
  _make_stub_pipeline ux-pipeline.sh          "$BUREAU_STATE_BUILD_SIM"
  _make_stub_pipeline copy-pipeline.sh        "$BUREAU_STATE_BUILD_SIM"
  _make_stub_pipeline implement-pipeline.sh   "$BUREAU_STATE_BUILD_REVIEW_SIM"
  _make_stub_pipeline qa-pipeline.sh          "$BUREAU_STATE_BUILD_REVIEW_SIM"
  _make_stub_pipeline code-review-pipeline.sh "$BUREAU_STATE_MERGE_SIM"
  _make_stub_pipeline merge-pipeline.sh       "$BUREAU_STATE_DONE_SIM"

  # Copy real shepherd
  cp "$REAL_SHEPHERD" "$sb/scripts/shepherd.sh"
  chmod +x "$sb/scripts/shepherd.sh"

  echo "$sb"
}

# State UUIDs used by the make_sandbox stub-pipelines. Exported so the
# heredoc-embedded stubs see them at sandbox-build time (the heredoc itself
# does $-expansion at the OUTER bash level).
export BUREAU_STATE_TRIAGE_SIM="s1"
export BUREAU_STATE_SPEC_REVIEW_SIM="s3"
export BUREAU_STATE_BUILD_SIM="s5"
export BUREAU_STATE_BUILD_REVIEW_SIM="s6"
export BUREAU_STATE_MERGE_SIM="s7"
export BUREAU_STATE_DONE_SIM="s8"

# Stuck-test stub: a spec-pipeline that does NOT advance state.
_make_stuck_stub() {
  local sb="$1"
  cat > "$sb/scripts/spec-pipeline.sh" <<'STUCK_EOF'
#!/bin/bash
set -euo pipefail
source "$(dirname "$0")/bureau-config.sh"
echo "spec-pipeline.sh" >> "$INVOCATIONS_LOG"
# Deliberately do NOT call move_issue — simulates a pipeline that ran but
# failed to advance state (e.g. NEEDS_HUMAN routing).
exit 0
STUCK_EOF
  chmod +x "$sb/scripts/spec-pipeline.sh"
}

run_shepherd() {
  local sb="$1"; shift
  ( cd "$sb" \
    && STATE_FILE="$sb/state.txt" \
       INVOCATIONS_LOG="$sb/invocations.log" \
       LABEL_LOG="$sb/labels.log" \
       bash "$sb/scripts/shepherd.sh" --no-tmux "$@" \
       > "$sb/shepherd.out" 2> "$sb/shepherd.err" )
}

assert_eq() {
  local got="$1" want="$2" label="$3"
  if [ "$got" != "$want" ]; then
    echo "FAIL: $label" >&2
    echo "  got:  $got"  >&2
    echo "  want: $want" >&2
    return 1
  fi
}

# ── Scenario 1: full happy path, Triage → Done ─────────────────────
test_happy_path() {
  local sb; sb=$(make_sandbox happy)
  echo "s1" > "$sb/state.txt"   # Triage

  run_shepherd "$sb" EXP-1

  local got
  got=$(tr '\n' ' ' < "$sb/invocations.log" | sed 's/ $//')
  assert_eq "$got" \
    "spec-pipeline.sh spec-review-pipeline.sh implement-pipeline.sh code-review-pipeline.sh merge-pipeline.sh" \
    "happy-path pipeline sequence"

  # shepherd-focused label applied on entry, removed on exit.
  # NOTE: pattern uses $'\t' (ANSI-C quoted tab) instead of '\t'. GNU grep on
  # Linux treats `\t` as a literal backslash-t; only BSD grep / ugrep
  # interpret it as a tab character — so the previous patterns passed on
  # macOS and silently failed on Linux CI.
  grep -q "^+EXP-1"$'\t'"shepherd-focused$" "$sb/labels.log" \
    || { echo "FAIL: shepherd-focused label was not applied"; return 1; }
  grep -q "^-EXP-1"$'\t'"shepherd-focused$" "$sb/labels.log" \
    || { echo "FAIL: shepherd-focused label was not removed on exit"; return 1; }

  return 0
}

# ── Scenario 2: --no-merge halts before merge-pipeline ─────────────
test_no_merge() {
  local sb; sb=$(make_sandbox nomerge)
  echo "s1" > "$sb/state.txt"

  run_shepherd "$sb" --no-merge EXP-2

  local got
  got=$(tr '\n' ' ' < "$sb/invocations.log" | sed 's/ $//')
  assert_eq "$got" \
    "spec-pipeline.sh spec-review-pipeline.sh implement-pipeline.sh code-review-pipeline.sh" \
    "--no-merge pipeline sequence (merge-pipeline must NOT appear)"

  if grep -q merge-pipeline.sh "$sb/invocations.log"; then
    echo "FAIL: merge-pipeline.sh was invoked despite --no-merge" >&2
    return 1
  fi
  return 0
}

# ── Scenario 3: --dry-run prints route, runs nothing ───────────────
test_dry_run() {
  local sb; sb=$(make_sandbox dryrun)
  echo "s5" > "$sb/state.txt"   # Build

  run_shepherd "$sb" --dry-run EXP-3

  [ ! -s "$sb/invocations.log" ] \
    || { echo "FAIL: --dry-run invoked a pipeline"; cat "$sb/invocations.log"; return 1; }
  grep -q "Current state: Build" "$sb/shepherd.out" \
    || { echo "FAIL: --dry-run did not print current state"; cat "$sb/shepherd.out"; return 1; }
  grep -q "Build → implement-pipeline.sh" "$sb/shepherd.out" \
    || { echo "FAIL: --dry-run route is missing the Build step"; return 1; }
  return 0
}

# ── Scenario 4: stuck pipeline trips MAX_STUCK and exits 13 ────────
test_stuck() {
  local sb; sb=$(make_sandbox stuck)
  _make_stuck_stub "$sb"
  echo "s1" > "$sb/state.txt"   # Triage; stub never advances

  set +e
  run_shepherd "$sb" EXP-4
  local rc=$?
  set -e

  assert_eq "$rc" "13" "stuck shepherd should exit 13"

  # Stuck logic: invocation 1 sets LAST_STATE=Triage; invocation 2 detects
  # STATE==LAST_STATE and increments STUCK_COUNT to 1; the third iteration
  # sees STUCK_COUNT reach MAX_STUCK=2 and exits before invoking again.
  # So two invocations of spec-pipeline are expected.
  local got
  got=$(wc -l < "$sb/invocations.log" | tr -d ' ')
  assert_eq "$got" "2" "stuck pipeline should be invoked exactly 2× before MAX_STUCK trips"

  grep -q "^+EXP-4"$'\t'"needs-human$" "$sb/labels.log" \
    || { echo "FAIL: stuck shepherd did not label needs-human"; return 1; }
  return 0
}

# ── Run all scenarios ──────────────────────────────────────────────
FAILS=0
for scenario in test_happy_path test_no_merge test_dry_run test_stuck; do
  if "$scenario"; then
    echo "  ok   $scenario"
  else
    echo "  FAIL $scenario"
    FAILS=$((FAILS + 1))
  fi
done

if [ "$FAILS" -eq 0 ]; then
  echo "OK test_shepherd"
  exit 0
else
  echo "FAIL test_shepherd ($FAILS scenario(s) failed)"
  exit 1
fi
