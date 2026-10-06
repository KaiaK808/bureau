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
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/bureau-env.sh"
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
bureau_is_paused() { return 1; }
precondition_claude_auth() { echo auth >> "$LABEL_LOG"; return 0; }

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

# Label read — the ticket carries none of needs-human / blocked / wip. (Without
# this stub the shepherd's label check used to hit "command not found" and read
# it as "no label"; it now halts on a read that fails, see scenario 7 on.)
get_issue_detail() { printf '%s' '{"identifier":"stub","labels":[]}'; }

# Branch resolution — return a fixed dummy branch.
# (No ${var,,} lowercase expansion — bash 3.2 on macOS doesn't support it.)
get_issue_branch() { echo "feat/$1-stub"; }

# Worktree helpers — create the directory so shepherd's `cd "$WORKTREE"`
# succeeds. The real reset_worktree guarantees the dir exists after the call.
reset_worktree()                  { mkdir -p "$1"; }
free_branch_from_other_worktrees(){ :; }

# shepherd's stage loop calls this before each stage; no-op in the
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
  # The shepherd's exit-code decisions run for real: the table and the Linear exit code
  # come from the real config.
  sed -n -e '/^BUREAU_EXIT_LINEAR_UNUSABLE=/p' -e '/^shepherd_rc_action() {/,/^}/p' \
    "$REPO_ROOT/templates/scripts/bureau-config.sh" >> "$sb/scripts/bureau-config.sh"
  # So do the stop boundary (--no-merge) and the merge policy (agents.merge_mode),
  # read from the sandbox .bureau.json by the real lines.
  {
    echo 'BUREAU_CONFIG="${BUREAU_CONFIG:-$PWD/.bureau.json}"'
    echo 'bureau_get() { jq -r "$1" "$BUREAU_CONFIG"; }'
    sed -n '/^# Capture the caller boundary/,/^BUREAU_RUNTIME=/{ /^BUREAU_RUNTIME=/d; p; }' "$REPO_ROOT/templates/scripts/bureau-config.sh"
    sed -n '/^# ── Merge policy (agents.merge_mode)/,/^# ── End of merge policy/p' "$REPO_ROOT/templates/scripts/bureau-config.sh"
  } >> "$sb/scripts/bureau-config.sh"
  # The hold check (bureau_human_hold, v3.1) runs for real on top of the label read,
  # with the needs-human hold block it belongs to, against the sandbox repository.
  sed -n '/^# ── needs-human hold/,/^# ── End of needs-human hold/p' \
    "$REPO_ROOT/templates/scripts/bureau-config.sh" >> "$sb/scripts/bureau-config.sh"

  # Stub pipelines: log invocation, advance to the next happy-path state through
  # the same moves as the real stage, in the same order.
  _make_stub_pipeline() {
    local name="$1" moves="" uuid
    shift
    for uuid in "$@"; do moves="${moves}move_issue \"\$ISSUE\" \"$uuid\""$'\n'; done
    cat > "$sb/scripts/$name" <<PIPELINE_EOF
#!/bin/bash
set -euo pipefail
source "\$(dirname "\$0")/bureau-config.sh"
ISSUE="\${1:-}"
echo "$name" >> "\$INVOCATIONS_LOG"
${moves}exit 0
PIPELINE_EOF
    chmod +x "$sb/scripts/$name"
  }

  # spec-pipeline.sh moves twice: Triage → Spec at its start, Spec → Spec Review
  # at its end. A stub with one move hid the Spec in between from every test.
  _make_stub_pipeline spec-pipeline.sh        "$BUREAU_STATE_SPEC_SIM" "$BUREAU_STATE_SPEC_REVIEW_SIM"
  _make_stub_pipeline spec-review-pipeline.sh "$BUREAU_STATE_BUILD_SIM"
  _make_stub_pipeline ux-pipeline.sh          "$BUREAU_STATE_BUILD_SIM"
  _make_stub_pipeline copy-pipeline.sh        "$BUREAU_STATE_BUILD_SIM"
  _make_stub_pipeline implement-pipeline.sh   "$BUREAU_STATE_BUILD_REVIEW_SIM"
  _make_stub_pipeline qa-pipeline.sh          "$BUREAU_STATE_BUILD_REVIEW_SIM"
  _make_stub_pipeline code-review-pipeline.sh "$BUREAU_STATE_MERGE_SIM"
  _make_stub_pipeline merge-pipeline.sh       "$BUREAU_STATE_DONE_SIM"

  # Real ownership wrapper around the simulated stage state machine.
  git -C "$sb" init -q
  # bureau_load_env lives next to the config the pipelines source (see
  # tests/test_env_read_safety.sh for why .env is parsed, not sourced).
  cp "$REPO_ROOT/templates/scripts/bureau-env.sh" "$sb/scripts/"
  cp "$REPO_ROOT/templates/scripts/bureau-runtime.py" "$sb/scripts/"
  cp "$REPO_ROOT/templates/scripts/bureau-worker.sh" "$sb/scripts/"
  cat >> "$sb/scripts/bureau-config.sh" <<'RUNTIME'
BUREAU_RUNTIME="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/bureau-runtime.py"
RUNTIME
  # Copy real shepherd
  cp "$REAL_SHEPHERD" "$sb/scripts/shepherd.sh"
  chmod +x "$sb/scripts/shepherd.sh"

  echo "$sb"
}

# State UUIDs used by the make_sandbox stub-pipelines. Exported so the
# heredoc-embedded stubs see them at sandbox-build time (the heredoc itself
# does $-expansion at the OUTER bash level).
export BUREAU_STATE_TRIAGE_SIM="s1"
export BUREAU_STATE_SPEC_SIM="s2"
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
  # The shepherd's temp files (fault file, gate report) go to the sandbox: a
  # scenario that kills the shepherd leaves nothing in the user's $TMPDIR.
  mkdir -p "$sb/tmp"
  ( cd "$sb" \
    && TMPDIR="$sb/tmp" \
       STATE_FILE="$sb/state.txt" \
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

  local rc=0
  run_shepherd "$sb" --no-merge EXP-2 || rc=$?
  assert_eq "$rc" "20" "stopped before merge must not report Done"

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

  run_shepherd "$sb" --dry-run --from-stage triage EXP-3
  [ ! -s "$sb/labels.log" ] || { echo "FAIL dry-run called auth or label mutation"; return 1; }
  assert_eq "$(cat "$sb/state.txt")" s5 "dry-run preserves state" || return 1

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
  # The shepherd confirms each unchanged state before re-running; a
  # zero-second wait keeps this scenario fast. The default is held in scenario 19.
  BUREAU_SHEPHERD_CONFIRM_SECONDS=0 run_shepherd "$sb" EXP-4
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

# ── Scenario 5: a stage gives up on Linear (exit 27) → halt, made visible ──
# The stub implement stage leaves a fault class in $_BUREAU_LINEAR_FAULT_FILE — the
# file shepherd hands it through the real bureau-worker.sh and runtime — and exits 27.
# $1 = what the stage writes into the fault file; $2 = "old" runs a shepherd without
# the 27 arm (the negative control).
_run_linear_halt() {
  local sb="$1" fault="$2"
  cat > "$sb/scripts/implement-pipeline.sh" <<STAGE_EOF
#!/bin/bash
set -euo pipefail
source "\$(dirname "\$0")/bureau-config.sh"
echo "implement-pipeline.sh" >> "\$INVOCATIONS_LOG"
printf '%s\n' '$fault' > "\${_BUREAU_LINEAR_FAULT_FILE:?shepherd did not hand the fault file on}"
exit 27
STAGE_EOF
  cat >> "$sb/scripts/bureau-config.sh" <<'REC_EOF'
add_issue_label() { printf '%s\t%s\tsingle=%s\n' "+$1" "$2" "${_BUREAU_LINEAR_SINGLE_ATTEMPT:-0}" >> "$LABEL_LOG"; }
post_comment()    { printf '%s\tsingle=%s\n' "$2" "${_BUREAU_LINEAR_SINGLE_ATTEMPT:-0}" >> "$LABEL_LOG.comments"; }
alert_telegram()  { printf '%s\n' "$4" >> "$LABEL_LOG.alerts"; }
REC_EOF
  echo "s5" > "$sb/state.txt"   # Build → implement-pipeline.sh
  set +e
  run_shepherd "$sb" EXP-5
  LINEAR_HALT_RC=$?
  set -e
}

test_linear_unusable_halts() {
  local sb; sb=$(make_sandbox linear_halt)
  _run_linear_halt "$sb" graphql-errors
  assert_eq "$LINEAR_HALT_RC" "27" "shepherd exit after a stage gave up on Linear" || return 1
  grep -q "^+EXP-5"$'\t'"needs-human"$'\t'"single=1$" "$sb/labels.log" \
    || { echo "FAIL: needs-human was not attempted once, on the single-attempt path"; cat "$sb/labels.log"; return 1; }
  grep -q "gave up because Linear stayed unusable.*graphql-errors.*single=1$" "$sb/labels.log.comments" \
    || { echo "FAIL: the halt comment does not name the fault class"; return 1; }
  grep -q "graphql-errors" "$sb/labels.log.alerts" \
    || { echo "FAIL: the alert does not name the fault class"; return 1; }
  local runs; runs=$(wc -l < "$sb/invocations.log" | tr -d ' ')
  assert_eq "$runs" "1" "the stage runs once; a halt is not retried" || return 1

  # Anything but a name from the fixed list is reported as unknown, never repeated.
  local sb2; sb2=$(make_sandbox linear_halt_text)
  _run_linear_halt "$sb2" 'CANARY-ANSWER <html>'
  grep -q "unknown" "$sb2/labels.log.alerts" || { echo "FAIL: a free-text fault was not reported as unknown"; return 1; }
  if grep -rq "CANARY-ANSWER" "$sb2/labels.log.alerts" "$sb2/labels.log.comments" "$sb2/shepherd.out" "$sb2/shepherd.err"; then
    echo "FAIL: text from the fault file reached an alert, a comment or the log"; return 1
  fi

  # Negative control: without the line that routes 27 to its own arm, the shepherd takes
  # the generic halt — it alerts, but neither labels nor comments, the silence on the
  # ticket this arm exists to end.
  local sb3; sb3=$(make_sandbox linear_halt_old)
  grep -v 'ACTION=linear-halt$' "$sb3/scripts/shepherd.sh" > "$sb3/scripts/shepherd.old" \
    && mv "$sb3/scripts/shepherd.old" "$sb3/scripts/shepherd.sh"
  if grep -q 'ACTION=linear-halt$' "$sb3/scripts/shepherd.sh" || ! grep -q 'shepherd_rc_action' "$sb3/scripts/shepherd.sh"; then
    echo "FAIL: could not build the negative control"; return 1
  fi
  _run_linear_halt "$sb3" graphql-errors
  if grep -q "needs-human" "$sb3/labels.log" 2>/dev/null || [ -s "$sb3/labels.log.comments" ]; then
    echo "FAIL: negative control: the shepherd without the 27 arm still labels or comments, so this proves nothing"; return 1
  fi
  return 0
}

# ── Scenario 6: a BLOCK review halts the shepherd instead of being reviewed again ──
# The stub review stage ends the way the real one does after a BLOCK — with the code
# resolve_verdict_exit gives BLOCK — and leaves the ticket in Build Review. $2 = the exit
# code to use instead (the negative control passes 0, the old BLOCK exit).
_run_block() {
  local sb="$1" code="${2:-}"
  if [ -z "$code" ]; then
    sed -n '/^resolve_verdict_exit() {/,/^}/p' "$REPO_ROOT/templates/scripts/bureau-config.sh" > "$sb/verdict.sh"
    code=$(bash -c 'source "$1"; resolve_verdict_exit BLOCK' _ "$sb/verdict.sh")
    [ -n "$code" ] || { echo "FAIL: resolve_verdict_exit not found in bureau-config.sh"; return 1; }
  fi
  cat > "$sb/scripts/code-review-pipeline.sh" <<STAGE_EOF
#!/bin/bash
echo "code-review-pipeline.sh" >> "\$INVOCATIONS_LOG"
exit $code
STAGE_EOF
  cat >> "$sb/scripts/bureau-config.sh" <<'REC_EOF'
alert_telegram() { printf '%s\n' "$4" >> "$LABEL_LOG.alerts"; }
REC_EOF
  echo "s6" > "$sb/state.txt"   # Build Review
  set +e
  run_shepherd "$sb" EXP-6
  BLOCK_RC=$?
  set -e
}

test_block_halts() {
  local sb; sb=$(make_sandbox block)
  _run_block "$sb"
  assert_eq "$BLOCK_RC" "25" "shepherd exit after a BLOCK review" || return 1
  assert_eq "$(grep -c . "$sb/invocations.log")" "1" "a BLOCK review runs once, never again on the same commit" || return 1
  grep -q "shepherd halt" "$sb/labels.log.alerts" 2>/dev/null \
    || { echo "FAIL: the BLOCK halt raised no alert"; return 1; }

  # Any code the table does not know halts with an alert too.
  local sb2; sb2=$(make_sandbox unknown_code)
  _run_block "$sb2" 99
  assert_eq "$BLOCK_RC" "99" "an unknown code halts with that code" || return 1
  grep -q "shepherd halt" "$sb2/labels.log.alerts" 2>/dev/null \
    || { echo "FAIL: an unknown exit code halted without an alert"; return 1; }

  # Negative control: the old BLOCK exit (0) sends the same ticket into a second review.
  local sb3; sb3=$(make_sandbox block_old)
  _run_block "$sb3" 0
  local runs; runs=$(grep -c . "$sb3/invocations.log")
  if [ "$runs" -lt 2 ]; then
    echo "FAIL: negative control: exit 0 no longer re-runs the review, so this proves nothing"; return 1
  fi
  return 0
}

# ── Scenarios 7–11: the shepherd's own Linear reads ─────
# These run the REAL read family out of bureau-config.sh — get_issue_state,
# get_issue_detail and the fetch with its fault record — against a stubbed curl
# that plays $sb/queue, one answer form per call, the last line repeating (the
# pattern of tests/test_linear_retry.sh). BUREAU_LINEAR_RETRIES=0 keeps the
# ladder at one attempt. The stub sleep records each wait and kills the shepherd
# at the third, so a shepherd that would wait forever ends, and shows it.
_use_real_linear_reads() {
  local sb="$1" fn
  sed -n \
    -e '/^_bureau_linear_classify() {/,/^}/p' -e '/^_bureau_linear_number() {/,/^}/p' \
    -e '/^_bureau_linear_setting() {/,/^}/p'  -e '/^_bureau_linear_record() {/,/^}/p' \
    -e '/^_bureau_linear_fetch() {/,/^}/p'    -e '/^linear_query() {/,/^}/p' \
    -e '/^bureau_issue_snapshot() {/,/^}/p'   -e '/^get_issue_state() {/,/^}/p' \
    -e '/^get_issue_detail() {/,/^}/p'        -e '/^get_issue_branch() {/,/^}/p' \
    -e '/^_bureau_linear_request_limits() {/,/^}/p' -e '/^linear_issue_query() {/,/^}/p' \
    -e '/^_BUREAU_LINEAR_STATUS_MARK=/p'      -e '/^_BUREAU_SHAPE_/p' \
    "$REPO_ROOT/templates/scripts/bureau-config.sh" >> "$sb/scripts/bureau-config.sh"
  # The stub defines get_issue_state and get_issue_detail itself; the real ones
  # come after it and win.
  for fn in _bureau_linear_fetch:1 get_issue_state:2 get_issue_detail:2 get_issue_branch:2; do
    [ "$(grep -c "^${fn%:*}() {" "$sb/scripts/bureau-config.sh")" = "${fn#*:}" ] \
      || { echo "FAIL: the real ${fn%:*} was not appended to the stub config"; return 1; }
  done
  cat >> "$sb/scripts/bureau-config.sh" <<'REC_EOF'
BUREAU_CONFIG="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/.bureau.json"
add_issue_label() { printf '%s\t%s\tsingle=%s\n' "+$1" "$2" "${_BUREAU_LINEAR_SINGLE_ATTEMPT:-0}" >> "$LABEL_LOG"; }
post_comment()    { printf '%s\tsingle=%s\n' "$2" "${_BUREAU_LINEAR_SINGLE_ATTEMPT:-0}" >> "$LABEL_LOG.comments"; }
alert_telegram()  { printf '%s\n' "$4" >> "$LABEL_LOG.alerts"; }
REC_EOF
  mkdir -p "$sb/bin" "$sb/forms" "$sb/tmp"
  # One body serves the state, label and branch reads, so it carries every list they ask
  # for (the branch read asks for the comments; real Linear always returns what was asked).
  local node='"id":"U1","identifier":"EXP-7","title":"T","description":"D","project":null,"branchName":"","comments":{"nodes":[]}'
  # A free ticket still carries a harmless label, so "some label" never passes for "a held label".
  printf '{"data":{"issues":{"nodes":[{%s,"state":{"id":"s5","name":"Build"},"labels":{"nodes":[{"name":"lane-2"}]}}]}}}' "$node" > "$sb/forms/build"
  printf '{"data":{"issues":{"nodes":[{%s,"state":{"id":"s5","name":"Build"},"labels":{"nodes":[{"name":"lane-2"},{"name":"needs-human"}]}}]}}}' "$node" > "$sb/forms/held"
  printf '{"data":{"issues":{"nodes":[{%s,"state":{"id":"s8","name":"Done"},"labels":{"nodes":[]}}]}}}' "$node" > "$sb/forms/done"
  printf '%s' '<html>CANARY-ANSWER 502 Bad Gateway</html>' > "$sb/forms/html"
  cat > "$sb/bin/curl" <<CURL_EOF
#!/bin/bash
q="$sb/queue"
form=\$(head -1 "\$q")
if [ "\$(wc -l < "\$q")" -gt 1 ]; then tail -n +2 "\$q" > "\$q.tmp" && mv "\$q.tmp" "\$q"; fi
echo "\$form" >> "$sb/curl.log"
if [ "\$form" = slow ]; then : > "$sb/in-read"; /bin/sleep 20; form=build; fi
cat "$sb/forms/\$form"
CURL_EOF
  cat > "$sb/bin/sleep" <<SLEEP_EOF
#!/bin/bash
printf '%s\n' "\$1" >> "$sb/sleeps.log"
[ "\$(wc -l < "$sb/sleeps.log")" -ge 3 ] && kill -KILL "\$PPID"
exit 0
SLEEP_EOF
  chmod +x "$sb/bin/curl" "$sb/bin/sleep"
}

# _put_old_reads <sb> — the negative control: the earlier unchecked reads,
# verbatim, defined after the new ones so they win. Checks that they landed.
_put_old_reads() {
  local sb="$1"
  cat > "$sb/old-reads.sh" <<'OLD_EOF'
_shepherd_state() { get_issue_state "$ISSUE" 2>/dev/null || echo ""; }
_shepherd_branch() { get_issue_branch "$ISSUE" 2>/dev/null || echo ""; }
_shepherd_human_label() {
  local forbidden
  for forbidden in needs-human blocked wip; do
    if get_issue_detail "$ISSUE" 2>/dev/null \
         | jq -e --arg L "$forbidden" '.labels | index($L)' >/dev/null 2>&1; then
      printf '%s' "$forbidden"; return 0
    fi
  done
}
OLD_EOF
  awk -v f="$sb/old-reads.sh" '/^# ── Dry run: print the route/ { while ((getline l < f) > 0) print l } { print }' \
    "$sb/scripts/shepherd.sh" > "$sb/scripts/shepherd.old" && mv "$sb/scripts/shepherd.old" "$sb/scripts/shepherd.sh"
  [ "$(grep -c '^_shepherd_state() {' "$sb/scripts/shepherd.sh")" = 2 ] \
    || { echo "FAIL: could not build the negative control"; return 1; }
}

# _run_reads <sb> <queue…> [-- shepherd args] — plays the queue; sets READS_RC.
# A run (not a dry run) reads the labels once before it claims the ticket (the
# hold check, v3.1), so its queue starts with the answer to that read — "build",
# a free ticket — and the loop's reads follow. With _put_old_reads the old label
# check makes up to three reads per check, the one before the claim included.
_run_reads() {
  local sb="$1"; shift
  local forms=()
  while [ $# -gt 0 ] && [ "$1" != -- ]; do forms+=("$1"); shift; done
  [ "${1:-}" = -- ] && shift
  printf '%s\n' "${forms[@]}" > "$sb/queue"
  set +e
  PATH="$sb/bin:$PATH" BUREAU_LINEAR_RETRIES=0 TMPDIR="$sb/tmp" run_shepherd "$sb" "$@"
  READS_RC=$?
  set -e
}

# _no_fault_files <sb> — the shepherd left no fault file behind in its TMPDIR.
_no_fault_files() {
  if grep -q '^bureau-linear-fault\.' <<< "$(ls "$1/tmp" 2>/dev/null)"; then
    echo "FAIL: a fault file was left behind in TMPDIR"; ls "$1/tmp"; return 1
  fi
}

_no_answer_text() {
  if grep -rq CANARY-ANSWER "$1/shepherd.out" "$1/shepherd.err" "$1/labels.log"* 2>/dev/null; then
    echo "FAIL: answer text reached a message"; return 1
  fi
}

# Scenario 7: the state read gets an unusable Linear → exit 27, fault class named.
test_state_read_unusable_halts() {
  local sb; sb=$(make_sandbox state_read)
  _use_real_linear_reads "$sb" || return 1
  _run_reads "$sb" build html -- EXP-7
  assert_eq "$READS_RC" 27 "shepherd exit when its state read gives up on Linear" || return 1
  [ ! -s "$sb/invocations.log" ] || { echo "FAIL: a stage ran without a state"; return 1; }
  [ ! -s "$sb/sleeps.log" ] || { echo "FAIL: the shepherd waited instead of halting"; return 1; }
  grep -q "^+EXP-7"$'\t'"needs-human"$'\t'"single=1$" "$sb/labels.log" \
    || { echo "FAIL: needs-human was not attempted once, on the single-attempt path"; cat "$sb/labels.log"; return 1; }
  grep -q 'reading the state gave up because Linear stayed unusable.*not-json.*single=1$' "$sb/labels.log.comments" \
    || { echo "FAIL: the halt comment does not name the read and the fault class"; cat "$sb/labels.log.comments"; return 1; }
  grep -q "not-json" "$sb/labels.log.alerts" || { echo "FAIL: the alert does not name the fault class"; return 1; }
  _no_answer_text "$sb" || return 1

  # Negative control: today's `|| echo ""` reads the failure as "no state" and waits forever.
  local sb2; sb2=$(make_sandbox state_read_old)
  _use_real_linear_reads "$sb2" && _put_old_reads "$sb2" || return 1
  _run_reads "$sb2" build html -- EXP-7
  if [ "$READS_RC" = 27 ] || [ "$(grep -c '^60$' "$sb2/sleeps.log" 2>/dev/null)" -lt 3 ] \
     || grep -q needs-human "$sb2/labels.log" 2>/dev/null; then
    echo "FAIL: negative control: the old state read no longer waits forever, so this proves nothing"; return 1
  fi
  return 0
}

# Scenario 8: the state read works, the label read gets an unusable Linear → exit 27.
test_label_read_unusable_halts() {
  local sb; sb=$(make_sandbox label_read)
  _use_real_linear_reads "$sb" || return 1
  _run_reads "$sb" build build html -- EXP-7
  assert_eq "$READS_RC" 27 "shepherd exit when its label read gives up on Linear" || return 1
  assert_eq "$(tr '\n' ' ' < "$sb/curl.log")" "build build html " "the hold check, one state read, then one label read" || return 1
  [ ! -s "$sb/invocations.log" ] || { echo "FAIL: the shepherd walked on past an unread label list"; return 1; }
  grep -q 'reading the labels gave up because Linear stayed unusable.*not-json.*single=1$' "$sb/labels.log.comments" \
    || { echo "FAIL: the halt comment does not name the read and the fault class"; cat "$sb/labels.log.comments"; return 1; }
  grep -q "not-json" "$sb/labels.log.alerts" || { echo "FAIL: the alert does not name the fault class"; return 1; }
  _no_answer_text "$sb" || return 1

  # Negative control: today's label check reads the failure as "no label" and walks on.
  local sb2; sb2=$(make_sandbox label_read_old)
  _use_real_linear_reads "$sb2" && _put_old_reads "$sb2" || return 1
  _run_reads "$sb2" build build build build html -- EXP-7
  grep -q implement-pipeline.sh "$sb2/invocations.log" 2>/dev/null \
    || { echo "FAIL: negative control: the old label check no longer walks on, so this proves nothing"; return 1; }
  return 0
}

# Scenario 9: when Linear answers, a held ticket halts and a free one walks on.
test_human_label_when_linear_answers() {
  local sb; sb=$(make_sandbox label_held)
  _use_real_linear_reads "$sb" || return 1
  _run_reads "$sb" build held -- EXP-7
  assert_eq "$READS_RC" 25 "shepherd exit on a ticket labelled needs-human" || return 1
  grep -q "'needs-human' label present on EXP-7" "$sb/shepherd.out" \
    || { echo "FAIL: the halt does not name needs-human"; cat "$sb/shepherd.out"; return 1; }
  [ ! -s "$sb/invocations.log" ] || { echo "FAIL: a stage ran on a held ticket"; return 1; }

  local sb2; sb2=$(make_sandbox label_free)
  _use_real_linear_reads "$sb2" || return 1
  _run_reads "$sb2" build build build build done -- EXP-7
  assert_eq "$READS_RC" 0 "shepherd exit on a free ticket that reaches Done" || return 1
  assert_eq "$(tr '\n' ' ' < "$sb2/invocations.log")" "implement-pipeline.sh " "a free ticket runs its stage once" || return 1
  assert_eq "$(tr '\n' ' ' < "$sb2/curl.log")" "build build build build done " "the hold check, then one state, label and branch read per iteration" || return 1
  return 0
}

# Scenario 10: --dry-run with an unusable Linear exits 27, prints no route, writes nothing.
test_dry_run_read_unusable() {
  local sb; sb=$(make_sandbox dry_read)
  _use_real_linear_reads "$sb" || return 1
  _run_reads "$sb" html -- --dry-run EXP-7
  assert_eq "$READS_RC" 27 "dry-run exit when the state read gives up on Linear" || return 1
  grep -q "could not read the state of EXP-7.*fault: not-json" "$sb/shepherd.err" \
    || { echo "FAIL: the dry run does not name the fault class"; cat "$sb/shepherd.err"; return 1; }
  if grep -q "Current state" "$sb/shepherd.out"; then echo "FAIL: the dry run printed a route"; return 1; fi
  [ ! -e "$sb/labels.log" ] && [ ! -e "$sb/labels.log.comments" ] \
    || { echo "FAIL: the dry run wrote to Linear"; return 1; }
  _no_answer_text "$sb" || return 1
  _no_fault_files "$sb" || return 1

  # When Linear answers, the dry run prints the route and cleans up as well.
  local sb3; sb3=$(make_sandbox dry_read_ok)
  _use_real_linear_reads "$sb3" || return 1
  _run_reads "$sb3" build -- --dry-run EXP-7
  assert_eq "$READS_RC" 0 "dry-run exit when Linear answers" || return 1
  grep -q "Build → implement-pipeline.sh" "$sb3/shepherd.out" \
    || { echo "FAIL: the dry run printed no route"; cat "$sb3/shepherd.out"; return 1; }
  _no_fault_files "$sb3" || return 1

  # Negative control: today's dry run prints "unknown" and reports success.
  local sb2; sb2=$(make_sandbox dry_read_old)
  _use_real_linear_reads "$sb2" && _put_old_reads "$sb2" || return 1
  _run_reads "$sb2" html -- --dry-run EXP-7
  if [ "$READS_RC" != 0 ] || ! grep -q "Current state: unknown" "$sb2/shepherd.out"; then
    echo "FAIL: negative control: the old dry run no longer reports success, so this proves nothing"; return 1
  fi
  return 0
}

# Scenario 11: a read that fails with any other code is neither "no state" nor
# "no label": the shepherd halts with 1, labels needs-human and names the read.
# $2 = a replacement read helper appended to the stub config. A broken label read
# starts with $_FIRST_DETAIL_FREE: the first label read is the hold check before the
# claim (v3.1), which gets a free ticket, so the loop's own read is the one that fails.
_FIRST_DETAIL_FREE='[ -e "$LABEL_LOG.pre" ] || { : > "$LABEL_LOG.pre"; printf "%s" "{\"labels\":[]}"; return 0; };'
_run_bad_read() {
  local sb="$1" helper="$2"; shift 2
  [ $# -gt 0 ] || set -- EXP-8
  printf '%s\n' "$helper" >> "$sb/scripts/bureau-config.sh"
  cat >> "$sb/scripts/bureau-config.sh" <<'REC_EOF'
post_comment()   { printf '%s\n' "$2" >> "$LABEL_LOG.comments"; }
alert_telegram() { printf '%s\n' "$4" >> "$LABEL_LOG.alerts"; }
REC_EOF
  mkdir -p "$sb/bin" "$sb/tmp"
  cat > "$sb/bin/sleep" <<SLEEP_EOF
#!/bin/bash
printf '%s\n' "\$1" >> "$sb/sleeps.log"
[ "\$(wc -l < "$sb/sleeps.log")" -ge 3 ] && kill -KILL "\$PPID"
exit 0
SLEEP_EOF
  chmod +x "$sb/bin/sleep"
  echo "s5" > "$sb/state.txt"   # Build
  set +e
  PATH="$sb/bin:$PATH" TMPDIR="$sb/tmp" run_shepherd "$sb" "$@"
  READS_RC=$?
  set -e
}

test_read_failure_other_code() {
  local name helper what
  for case_ in 'state_exit5|get_issue_state() { return 5; }|state' \
               "detail_empty|get_issue_detail() { $_FIRST_DETAIL_FREE return 0; }|labels" \
               "detail_nolist|get_issue_detail() { $_FIRST_DETAIL_FREE printf \"%s\" \"{}\"; }|labels" \
               'branch_exit5|get_issue_branch() { return 5; }|branch'; do
    name=${case_%%|*}; what=${case_##*|}; helper=${case_#*|}; helper=${helper%|*}
    local sb; sb=$(make_sandbox "bad_$name")
    _run_bad_read "$sb" "$helper"
    assert_eq "$READS_RC" 1 "$name: shepherd exit when the $what read fails" || return 1
    [ ! -s "$sb/invocations.log" ] || { echo "FAIL: $name: a stage ran on an unread $what"; return 1; }
    [ ! -s "$sb/sleeps.log" ] || { echo "FAIL: $name: the shepherd waited instead of halting"; return 1; }
    grep -q "^+EXP-8"$'\t'"needs-human$" "$sb/labels.log" \
      || { echo "FAIL: $name: needs-human was not added"; return 1; }
    grep -q "could not read the $what of this ticket" "$sb/labels.log.comments" \
      || { echo "FAIL: $name: the comment does not name the $what read"; return 1; }
  done

  # The dry run fails the same way, prints no route and writes nothing.
  local sb2; sb2=$(make_sandbox bad_dry_state)
  _run_bad_read "$sb2" 'get_issue_state() { return 5; }' --dry-run EXP-8
  assert_eq "$READS_RC" 1 "dry-run exit when the state read fails with another code" || return 1
  grep -q "could not read the state of EXP-8 (exit 5)" "$sb2/shepherd.err" \
    || { echo "FAIL: the dry run does not name the failed read"; cat "$sb2/shepherd.err"; return 1; }
  if grep -q "Current state" "$sb2/shepherd.out"; then echo "FAIL: the dry run printed a route"; return 1; fi
  [ ! -e "$sb2/labels.log" ] || { echo "FAIL: the dry run wrote to Linear"; return 1; }
  _no_fault_files "$sb2" || return 1
  return 0
}

# Scenario 12: state and labels read fine, the branch read gets an unusable Linear
# → exit 27, not the stage's 12 (no-branch) under the wrong name.
test_branch_read_unusable_halts() {
  local sb; sb=$(make_sandbox branch_read)
  _use_real_linear_reads "$sb" || return 1
  _run_reads "$sb" build build build html -- EXP-7
  assert_eq "$READS_RC" 27 "shepherd exit when its branch read gives up on Linear" || return 1
  [ ! -s "$sb/invocations.log" ] || { echo "FAIL: a stage ran without its branch"; return 1; }
  grep -q 'reading the branch gave up because Linear stayed unusable.*not-json.*single=1$' "$sb/labels.log.comments" \
    || { echo "FAIL: the halt comment does not name the read and the fault class"; cat "$sb/labels.log.comments"; return 1; }
  _no_answer_text "$sb" || return 1

  # Negative control: today's `|| echo ""` hands the stage an empty branch (in a real
  # repo reset_worktree then ends it with 12; the sandbox's reset lets it run).
  local sb2; sb2=$(make_sandbox branch_read_old)
  _use_real_linear_reads "$sb2" && _put_old_reads "$sb2" || return 1
  _run_reads "$sb2" build build build build html -- EXP-7
  grep -q implement-pipeline.sh "$sb2/invocations.log" 2>/dev/null \
    || { echo "FAIL: negative control: the old branch read no longer hands on an empty branch, so this proves nothing"; return 1; }
  return 0
}

# Scenario 13: a read killed by a real signal is a cancelled run. Ctrl-C reaches the
# whole process group; a SIGTERM to the runtime is forwarded to the shepherd's group
# (bureau-runtime.py execute). The "slow" answer marks that a read is in flight.
# $2 = INT (to the group) or TERM (to the runtime); the rest are shepherd arguments.
_run_signal() {
  local sb="$1" sig="$2"; shift 2
  set +e
  python3 - "$sb" "$sig" "$@" <<'PY_EOF'
import os, signal, subprocess, sys, time
sb, sig, *args = sys.argv[1:]
env = dict(os.environ, PATH=f"{sb}/bin:" + os.environ["PATH"], TMPDIR=f"{sb}/tmp", BUREAU_LINEAR_RETRIES="0",
           STATE_FILE=f"{sb}/state.txt", INVOCATIONS_LOG=f"{sb}/invocations.log", LABEL_LOG=f"{sb}/labels.log")
def default_int():  # a background test runner starts children with SIGINT ignored
    signal.signal(signal.SIGINT, signal.SIG_DFL)
with open(f"{sb}/shepherd.out", "w") as out, open(f"{sb}/shepherd.err", "w") as err:
    p = subprocess.Popen(["bash", "scripts/shepherd.sh", "--no-tmux", *args], cwd=sb, env=env,
                         stdout=out, stderr=err, start_new_session=True, preexec_fn=default_int)
    deadline = time.time() + 60
    while not os.path.exists(f"{sb}/in-read"):
        if p.poll() is not None or time.time() > deadline:
            p.kill(); print("the shepherd never reached the slow read", file=sys.stderr); sys.exit(97)
        time.sleep(0.05)
    if sig == "INT": os.killpg(p.pid, signal.SIGINT)
    elif sig == "TERM-SHEPHERD":  # the shepherd alone, its pid left by a stub
        os.kill(int(open(f"{sb}/shepherd.pid").read()), signal.SIGTERM)
    else: os.kill(p.pid, signal.SIGTERM)
    sent = time.time()
    rc = p.wait(timeout=60)
open(f"{sb}/signal-to-exit", "w").write("%d" % (time.time() - sent))
sys.exit(rc if rc >= 0 else 128 - rc)
PY_EOF
  READS_RC=$?
  set -e
}

# _nothing_written <sb> <label> — no needs-human, no comment, no alert, no stage.
_nothing_written() {
  if grep -q needs-human "$1/labels.log" 2>/dev/null || [ -e "$1/labels.log.comments" ] \
     || [ -e "$1/labels.log.alerts" ] || [ -s "$1/invocations.log" ]; then
    echo "FAIL: $2: an interrupted read wrote needs-human, a comment or an alert, or ran a stage"
    cat "$1/labels.log" "$1/labels.log.comments" "$1/labels.log.alerts" 2>/dev/null; return 1
  fi
}

test_interrupted_read_is_cancelled() {
  local case_ sig queue what sb
  for case_ in 'INT|build slow|state' 'INT|build build slow|labels' 'INT|build build build slow|branch' 'TERM|build slow|state'; do
    sig=${case_%%|*}; what=${case_##*|}; queue=${case_#*|}; queue=${queue%|*}
    sb=$(make_sandbox "sig_${sig}_$what")
    _use_real_linear_reads "$sb" || return 1
    rm -f "$sb/bin/sleep"
    printf '%s\n' $queue > "$sb/queue"
    _run_signal "$sb" "$sig" EXP-7
    assert_eq "$READS_RC" 130 "$sig during the $what read: exit" || { cat "$sb/shepherd.err"; return 1; }
    # The INT/TERM trap ends the run as soon as the read returns; the
    # read's own cancelled branch is held by the code-only cases below.
    grep -q "interrupted by SIG$sig — cancelled" "$sb/shepherd.err" \
      || { echo "FAIL: $sig during the $what read: the shepherd did not end as cancelled"; cat "$sb/shepherd.err"; return 1; }
    _nothing_written "$sb" "$sig during the $what read" || return 1
    _no_fault_files "$sb" || return 1
  done

  # The dry run: Ctrl-C ends it, and its fault file goes with it.
  sb=$(make_sandbox sig_dry)
  _use_real_linear_reads "$sb" || return 1
  rm -f "$sb/bin/sleep"; echo slow > "$sb/queue"
  _run_signal "$sb" INT --dry-run EXP-7
  assert_eq "$READS_RC" 130 "Ctrl-C during the dry run's read: exit" || return 1
  _nothing_written "$sb" "Ctrl-C during the dry run" || return 1
  _no_fault_files "$sb" || return 1

  # The same decision without a runtime in between (the runtime reports 130 for any
  # interrupted child): a read that ends above 128 ends the shepherd with 130.
  sb=$(make_sandbox sig_code_loop)
  _run_bad_read "$sb" "get_issue_detail() { $_FIRST_DETAIL_FREE return 143; }"
  assert_eq "$READS_RC" 130 "a label read killed by a signal: exit" || return 1
  _nothing_written "$sb" "a label read killed by a signal" || return 1
  sb=$(make_sandbox sig_code_dry)
  _run_bad_read "$sb" 'get_issue_state() { return 143; }' --dry-run EXP-8
  assert_eq "$READS_RC" 130 "a dry-run state read killed by a signal: exit" || return 1
  grep -q "dry-run: interrupted while reading the state of EXP-8" "$sb/shepherd.err" \
    || { echo "FAIL: the dry run did not end as cancelled"; cat "$sb/shepherd.err"; return 1; }
  _no_fault_files "$sb" || return 1

  # Negative controls. Without the cancelling trap (the release-only trap of
  # before) and without the read's signal branch, the interrupted read reads as a
  # failed one and labels the ticket; without the dry run's trap the file stays.
  local sb2; sb2=$(make_sandbox sig_old)
  _use_real_linear_reads "$sb2" || return 1
  _old_signal_trap "$sb2" || return 1
  python3 - "$sb2/scripts/shepherd.sh" <<'PY_EOF' || { echo "FAIL: could not build the negative control"; return 1; }
import pathlib, re, sys
p = pathlib.Path(sys.argv[1]); t = p.read_text()
t, n = re.subn(r'\n  if \[ "\$rc" -gt 128 \]; then\n    echo "\[shepherd\] interrupted while reading.*?\n  fi\n', '\n', t, flags=re.S)
if n != 1: sys.exit(1)
p.write_text(t)
PY_EOF
  rm -f "$sb2/bin/sleep"; printf '%s\n' build slow > "$sb2/queue"
  _run_signal "$sb2" INT EXP-7
  grep -q needs-human "$sb2/labels.log" 2>/dev/null \
    || { echo "FAIL: negative control: an interrupted read no longer labels without the signal branch, so this proves nothing"; return 1; }
  local sb3; sb3=$(make_sandbox sig_dry_old)
  _use_real_linear_reads "$sb3" || return 1
  grep -v "^  trap 'rm -f \"\$SHEPHERD_FAULT_FILE\" 2>/dev/null || true' EXIT$" "$sb3/scripts/shepherd.sh" > "$sb3/scripts/shepherd.old"
  if cmp -s "$sb3/scripts/shepherd.sh" "$sb3/scripts/shepherd.old"; then echo "FAIL: could not build the negative control"; return 1; fi
  mv "$sb3/scripts/shepherd.old" "$sb3/scripts/shepherd.sh"
  rm -f "$sb3/bin/sleep"; echo slow > "$sb3/queue"
  _run_signal "$sb3" INT --dry-run EXP-7
  if ! grep -q '^bureau-linear-fault\.' <<< "$(ls "$sb3/tmp")"; then
    echo "FAIL: negative control: the dry run's fault file goes away without the trap, so this proves nothing"; return 1
  fi
  return 0
}

# ── Scenario 14: agents.merge_mode manual ends the run at Merge, quietly ──
_set_mode() {  # $1 = sandbox, $2 = JSON value
  jq --argjson v "$2" '.agents.merge_mode = $v' "$1/.bureau.json" > "$1/.bureau.json.tmp" && mv "$1/.bureau.json.tmp" "$1/.bureau.json"
}
_record_alerts() {
  cat >> "$1/scripts/bureau-config.sh" <<'REC_EOF'
alert_telegram() { printf '%s\n' "$4" >> "$LABEL_LOG.alerts"; }
REC_EOF
}

test_merge_mode_manual() {
  local sb; sb=$(make_sandbox manual)
  _set_mode "$sb" '"manual"'; _record_alerts "$sb"
  echo "s1" > "$sb/state.txt"
  local rc=0
  run_shepherd "$sb" EXP-7 || rc=$?
  assert_eq "$rc" "20" "manual: the run ends before merge" || return 1
  if grep -q merge-pipeline.sh "$sb/invocations.log"; then
    echo "FAIL: manual: merge-pipeline.sh was invoked" >&2; return 1
  fi
  assert_eq "$(tr '\n' ' ' < "$sb/invocations.log" | sed 's/ $//')" \
    "spec-pipeline.sh spec-review-pipeline.sh implement-pipeline.sh code-review-pipeline.sh" "manual: stages up to review" || return 1
  if [ -s "$sb/labels.log.alerts" ] || grep -q needs-human "$sb/labels.log"; then
    echo "FAIL: manual: the expected end alerted or labelled needs-human" >&2; return 1
  fi

  # The dry run shows the same boundary.
  local sb2; sb2=$(make_sandbox manual_dry)
  _set_mode "$sb2" '"manual"'
  echo "s6" > "$sb2/state.txt"
  run_shepherd "$sb2" --dry-run EXP-7
  grep -q "Merge → (halt — agents.merge_mode manual)" "$sb2/shepherd.out" \
    || { echo "FAIL: manual: dry run does not show the halt at Merge"; cat "$sb2/shepherd.out"; return 1; }
  return 0
}

# ── Scenario 15: a review that stopped before merge as asked is quiet ──
# The stub review stage ends the way the real one does under --no-merge after an
# APPROVE: exit 20, ticket unmoved. Only that asked-for 20 is quiet.
_run_review_stop() {  # $1 = sandbox, $2 = the stage's exit code, $3… = shepherd flags
  local sb="$1" code="$2"; shift 2
  cat > "$sb/scripts/code-review-pipeline.sh" <<STAGE_EOF
#!/bin/bash
echo "code-review-pipeline.sh" >> "\$INVOCATIONS_LOG"
exit $code
STAGE_EOF
  _record_alerts "$sb"
  echo "s6" > "$sb/state.txt"
  set +e
  run_shepherd "$sb" "$@" EXP-8
  REVIEW_STOP_RC=$?
  set -e
}

test_review_stop_quiet() {
  local sb2; sb2=$(make_sandbox review_stop_nomerge)
  _run_review_stop "$sb2" 20 --no-merge
  assert_eq "$REVIEW_STOP_RC" "20" "--no-merge: review stop ends the run with 20" || return 1
  assert_eq "$(grep -c . "$sb2/invocations.log")" "1" "--no-merge: the review runs once" || return 1
  [ ! -s "$sb2/labels.log.alerts" ] || { echo "FAIL: --no-merge: a review stop alerted"; return 1; }

  # --no-merge silences only that 20: a BLOCK (25) under --no-merge still alerts.
  local sb5; sb5=$(make_sandbox review_block_nomerge)
  _run_review_stop "$sb5" 25 --no-merge
  assert_eq "$REVIEW_STOP_RC" "25" "--no-merge: a BLOCK halts with 25" || return 1
  grep -q "shepherd halt" "$sb5/labels.log.alerts" 2>/dev/null \
    || { echo "FAIL: --no-merge: a BLOCK halted without an alert"; return 1; }

  # manual does not make a 20 quiet: no template stage exits 20 under manual, so one
  # that does is unexpected and alerts.
  local sb; sb=$(make_sandbox review_stop_manual)
  _set_mode "$sb" '"manual"'
  _run_review_stop "$sb" 20
  assert_eq "$REVIEW_STOP_RC" "20" "manual: an unasked 20 halts with its code" || return 1
  grep -q "shepherd halt" "$sb/labels.log.alerts" 2>/dev/null \
    || { echo "FAIL: manual: an unasked 20 halted without an alert"; return 1; }

  # Negative control: a 20 nobody asked for (auto, no --no-merge) still halts with an alert.
  local sb3; sb3=$(make_sandbox review_stop_unasked)
  _set_mode "$sb3" '"auto"'
  _run_review_stop "$sb3" 20
  assert_eq "$REVIEW_STOP_RC" "20" "unasked 20 halts with its code" || return 1
  grep -q "shepherd halt" "$sb3/labels.log.alerts" 2>/dev/null \
    || { echo "FAIL: negative control: an unasked 20 no longer alerts, so this proves nothing"; return 1; }

  # manual silences only that 20: a BLOCK (25) under manual still halts with an alert.
  local sb4; sb4=$(make_sandbox review_block_manual)
  _set_mode "$sb4" '"manual"'
  _run_review_stop "$sb4" 25
  assert_eq "$REVIEW_STOP_RC" "25" "manual: a BLOCK still halts with 25" || return 1
  grep -q "shepherd halt" "$sb4/labels.log.alerts" 2>/dev/null \
    || { echo "FAIL: manual: a BLOCK halted without an alert"; return 1; }
  return 0
}

# ── the shepherd's own calls outside the stages ─────────────
# _mutate <file> <old> <new> — replaces exactly one occurrence; builds a negative
# control out of the current script (CI's shallow checkout has no older copy).
_mutate() {
  python3 - "$1" "$2" "$3" <<'PY_EOF'
import pathlib, sys
p = pathlib.Path(sys.argv[1]); t = p.read_text(); old, new = sys.argv[2], sys.argv[3]
if t.count(old) != 1: sys.exit("could not build the negative control: %d matches for %r" % (t.count(old), old))
p.write_text(t.replace(old, new))
PY_EOF
}

# _old_signal_trap <sb> — the earlier INT/TERM trap: release the claim and go on.
_old_signal_trap() {
  _mutate "$1/scripts/shepherd.sh" "trap '_shepherd_cancelled SIGINT' INT
trap '_shepherd_cancelled SIGTERM' TERM" "trap 'remove_issue_label \"\$ISSUE\" shepherd-focused 2>/dev/null || true' INT TERM"
}

# _record_writes <sb> — label, comment and alert writes land in files.
_record_writes() {
  cat >> "$1/scripts/bureau-config.sh" <<'REC_EOF'
add_issue_label() { printf '%s\t%s\tsingle=%s\n' "+$1" "$2" "${_BUREAU_LINEAR_SINGLE_ATTEMPT:-0}" >> "$LABEL_LOG"; }
post_comment()    { printf '%s\tsingle=%s\n' "$2" "${_BUREAU_LINEAR_SINGLE_ATTEMPT:-0}" >> "$LABEL_LOG.comments"; }
alert_telegram()  { printf '%s\n' "$4" >> "$LABEL_LOG.alerts"; }
REC_EOF
}

# _record_release <sb> — the release records whether it was a single attempt.
_record_release() {
  echo 'remove_issue_label() { printf '"'"'%s\t%s\tsingle=%s\n'"'"' "-$1" "$2" "${_BUREAU_LINEAR_SINGLE_ATTEMPT:-0}" >> "$LABEL_LOG"; }' >> "$1/scripts/bureau-config.sh"
}

# _record_sleeps <sb> [<kill-at>] — a `sleep` on PATH that records its argument and
# returns at once; with <kill-at>, it kills the shepherd at that many waits.
_record_sleeps() {
  mkdir -p "$1/bin"
  cat > "$1/bin/sleep" <<SLEEP_EOF
#!/bin/bash
printf '%s\n' "\$1" >> "$1/sleeps.log"
[ -n "${2:-}" ] && [ "\$(wc -l < "$1/sleeps.log")" -ge "${2:-0}" ] && kill -KILL "\$PPID"
exit 0
SLEEP_EOF
  chmod +x "$1/bin/sleep"
}

# Scenario 16: the start check fails before the claim → exit 10 named and alerted;
# nothing claimed, nothing labeled or commented, no fault file left.
test_start_check_failure() {
  local sb rc
  sb=$(make_sandbox start_check)
  _record_writes "$sb"
  cat >> "$sb/scripts/bureau-config.sh" <<'PRE_EOF'
precondition_linear() { printf 'no-response\n' > "${_BUREAU_LINEAR_FAULT_FILE:-/dev/null}"; echo "ERROR: viewer query returned no id" >&2; exit 10; }
PRE_EOF
  echo s5 > "$sb/state.txt"; mkdir -p "$sb/tmp"
  set +e; TMPDIR="$sb/tmp" run_shepherd "$sb" EXP-16; rc=$?; set -e
  assert_eq "$rc" 10 "start check fails: exit" || { cat "$sb/shepherd.err"; return 1; }
  grep -qx "shepherd did not start (linear-down: no-response)" "$sb/labels.log.alerts" 2>/dev/null \
    || { echo "FAIL: start check: no alert naming the class and fault"; cat "$sb/labels.log.alerts" 2>/dev/null; return 1; }
  if grep -q 'shepherd-focused\|needs-human' "$sb/labels.log" 2>/dev/null || [ -e "$sb/labels.log.comments" ] || [ -s "$sb/invocations.log" ]; then
    echo "FAIL: start check: the ticket was claimed, labeled or commented, or a stage ran"; cat "$sb/labels.log" 2>/dev/null; return 1
  fi
  _no_fault_files "$sb" || return 1

  # A start check ended by a signal is a cancelled run: 130, no alert.
  sb=$(make_sandbox start_check_signal)
  _record_writes "$sb"
  echo 'precondition_linear() { exit 143; }' >> "$sb/scripts/bureau-config.sh"
  echo s5 > "$sb/state.txt"
  set +e; run_shepherd "$sb" EXP-16; rc=$?; set -e
  assert_eq "$rc" 130 "a start check killed by a signal: exit" || return 1
  grep -q "cancelled before EXP-16 was claimed; nothing written" "$sb/shepherd.err" \
    || { echo "FAIL: a start check killed by a signal: the message names a release"; cat "$sb/shepherd.err"; return 1; }
  [ ! -e "$sb/labels.log.alerts" ] || { echo "FAIL: a start check killed by a signal alerted"; return 1; }

  # Negative control: the bare check of before ends with 10 and tells nobody.
  sb=$(make_sandbox start_check_old)
  _record_writes "$sb"
  cat >> "$sb/scripts/bureau-config.sh" <<'PRE_EOF'
precondition_linear() { echo "ERROR: viewer query returned no id" >&2; exit 10; }
PRE_EOF
  _mutate "$sb/scripts/shepherd.sh" '( _BUREAU_LINEAR_FAULT_FILE="$SHEPHERD_FAULT_FILE" precondition_linear ) || _shepherd_start_failed $?' 'precondition_linear' || return 1
  echo s5 > "$sb/state.txt"
  set +e; run_shepherd "$sb" EXP-16; rc=$?; set -e
  assert_eq "$rc" 10 "negative control: the old start check exits 10" || return 1
  [ ! -e "$sb/labels.log.alerts" ] \
    || { echo "FAIL: negative control: the old start check alerted too, so this proves nothing"; return 1; }
  return 0
}

# Scenario 17: the shepherd's own move fails (--from-stage, and the Spec → Triage
# bump) → the halt of a failed read, not a bare exit under set -e.
test_move_failure_halts() {
  local site code sb rc target
  for site in from-stage bump; do
    for code in 27 1; do
      sb=$(make_sandbox "move_${site}_$code")
      _record_writes "$sb"
      cat >> "$sb/scripts/bureau-config.sh" <<MOVE_EOF
move_issue() { printf 'move\n' >> "\$LABEL_LOG"; printf 'graphql-errors\n' > "\${_BUREAU_LINEAR_FAULT_FILE:-/dev/null}"; return $code; }
MOVE_EOF
      if [ "$site" = from-stage ]; then
        echo s5 > "$sb/state.txt"; target=triage
        set +e; run_shepherd "$sb" --from-stage triage EXP-17; rc=$?; set -e
      else
        echo s2 > "$sb/state.txt"; target=Triage   # Spec → bumped to Triage
        set +e; run_shepherd "$sb" EXP-17; rc=$?; set -e
      fi
      assert_eq "$rc" "$code" "$site move fails with $code: exit" || { cat "$sb/shepherd.err"; return 1; }
      grep -q "^+EXP-17"$'\t'"needs-human" "$sb/labels.log" \
        || { echo "FAIL: $site move fails with $code: no needs-human"; return 1; }
      [ -s "$sb/labels.log.comments" ] || { echo "FAIL: $site move fails with $code: no halt comment"; return 1; }
      if [ "$code" = 27 ]; then
        grep -q "graphql-errors) moving the ticket to $target\$" "$sb/labels.log.alerts" \
          || { echo "FAIL: $site move fails with 27: the alert does not name the fault and the move"; cat "$sb/labels.log.alerts"; return 1; }
        grep -q "needs-human"$'\t'"single=1" "$sb/labels.log" \
          || { echo "FAIL: $site move fails with 27: needs-human was not a single attempt"; return 1; }
      else
        grep -qx "shepherd halt (could not move the ticket to $target, exit 1)" "$sb/labels.log.alerts" \
          || { echo "FAIL: $site move fails with 1: no alert naming the move"; cat "$sb/labels.log.alerts"; return 1; }
      fi
      [ ! -s "$sb/invocations.log" ] || { echo "FAIL: $site move fails with $code: a stage ran"; return 1; }
      grep -q "^-EXP-17"$'\t'"shepherd-focused" "$sb/labels.log" \
        || { echo "FAIL: $site move fails with $code: the claim was not released"; return 1; }
      # The ticket is claimed before it moves, so the queue keeps away from it.
      assert_eq "$(grep -n -m 2 -e 'shepherd-focused' -e '^move$' "$sb/labels.log" | cut -d: -f2 | cut -c1-2 | tr '\n' ' ')" "+E mo " \
        "$site move fails with $code: claim before move" || { cat "$sb/labels.log"; return 1; }
    done
  done

  # A typo in --from-stage is refused before anything is claimed or moved.
  sb=$(make_sandbox move_typo)
  _record_writes "$sb"
  echo s5 > "$sb/state.txt"
  set +e; run_shepherd "$sb" --from-stage bulid EXP-17; rc=$?; set -e
  assert_eq "$rc" 1 "--from-stage typo: exit" || return 1
  grep -q "has no matching state" "$sb/shepherd.err" || { echo "FAIL: --from-stage typo: no error"; return 1; }
  [ ! -s "$sb/labels.log" ] || { echo "FAIL: --from-stage typo: the ticket was claimed"; cat "$sb/labels.log"; return 1; }
  if grep -q 'claiming\|Running\|releasing' "$sb/shepherd.out" 2>/dev/null; then
    echo "FAIL: --from-stage typo: the shepherd got as far as the claim"; cat "$sb/shepherd.out"; return 1
  fi

  # A move that ended by a signal only it saw (a code above 128) is a cancelled run.
  sb=$(make_sandbox move_signal)
  _record_writes "$sb"
  echo 'move_issue() { return 143; }' >> "$sb/scripts/bureau-config.sh"
  echo s2 > "$sb/state.txt"
  set +e; run_shepherd "$sb" EXP-17; rc=$?; set -e
  assert_eq "$rc" 130 "a move killed by a signal: exit" || return 1
  grep -q "interrupted while moving EXP-17 to Triage" "$sb/shepherd.err" \
    || { echo "FAIL: a move killed by a signal did not end as cancelled"; cat "$sb/shepherd.err"; return 1; }
  if grep -q needs-human "$sb/labels.log" 2>/dev/null || [ -e "$sb/labels.log.comments" ] || [ -e "$sb/labels.log.alerts" ]; then
    echo "FAIL: a move killed by a signal wrote a label, comment or alert"; return 1
  fi

  # Negative control: the bare moves of before end the shepherd with the move's
  # code and write nothing — no label, no comment, no alert.
  for site in from-stage bump; do
    sb=$(make_sandbox "move_${site}_old")
    _record_writes "$sb"
    echo 'move_issue() { return 27; }' >> "$sb/scripts/bureau-config.sh"
    _mutate "$sb/scripts/shepherd.sh" ' \
    || _shepherd_move_failed "$FROM_STAGE" $?' '' || return 1
    _mutate "$sb/scripts/shepherd.sh" ' \
      || _shepherd_move_failed Triage $?' '' || return 1
    if [ "$site" = from-stage ]; then
      echo s5 > "$sb/state.txt"; set +e; run_shepherd "$sb" --from-stage triage EXP-17; rc=$?; set -e
    else
      echo s2 > "$sb/state.txt"; set +e; run_shepherd "$sb" EXP-17; rc=$?; set -e
    fi
    assert_eq "$rc" 27 "negative control: the old $site move exits with its code" || return 1
    if grep -q needs-human "$sb/labels.log" 2>/dev/null || [ -e "$sb/labels.log.comments" ] || [ -e "$sb/labels.log.alerts" ]; then
      echo "FAIL: negative control: the old $site move made the halt visible too, so this proves nothing"; return 1
    fi
  done
  return 0
}

# Scenario 18: SIGTERM during a wait and Ctrl-C during a stage end the run as
# cancelled — released, and nothing after the signal: no read, no stage, no alert.
test_signal_during_wait_and_stage() {
  local sb
  # (a) SIGTERM to the runtime while the shepherd waits out an answer without a state.
  sb=$(make_sandbox sig_wait)
  _use_real_linear_reads "$sb" || return 1
  _record_release "$sb"
  printf '%s' '{"data":{"issues":{"nodes":[]}}}' > "$sb/forms/nostate"
  printf '#!/bin/bash\n: > "%s/in-read"\nexec /bin/sleep "$@"\n' "$sb" > "$sb/bin/sleep"
  printf '%s\n' build nostate build > "$sb/queue"
  _run_signal "$sb" TERM EXP-18
  # Through the runtime, 130 alone proves nothing: the runtime reports 130 for any
  # interrupted child. The proof is the cancelled message, one read, nothing written.
  assert_eq "$READS_RC" 130 "SIGTERM during the wait: exit" || { cat "$sb/shepherd.err"; return 1; }
  grep -q "interrupted by SIGTERM — cancelled" "$sb/shepherd.err" \
    || { echo "FAIL: SIGTERM during the wait: not ended as cancelled"; cat "$sb/shepherd.err"; return 1; }
  assert_eq "$(wc -l < "$sb/curl.log" | tr -d ' ')" 2 "SIGTERM during the wait: reads (the hold check and one state read, none after the signal)" || return 1
  _nothing_written "$sb" "SIGTERM during the wait" || return 1
  grep -q "^-EXP-18"$'\t'"shepherd-focused"$'\t'"single=1" "$sb/labels.log" \
    || { echo "FAIL: SIGTERM during the wait: not released, or not with a single attempt"; cat "$sb/labels.log"; return 1; }
  _no_fault_files "$sb" || return 1

  # (b) Ctrl-C to the group while a stage runs.
  sb=$(make_sandbox sig_stage)
  _use_real_linear_reads "$sb" || return 1
  rm -f "$sb/bin/sleep"; echo build > "$sb/queue"
  cat > "$sb/scripts/implement-pipeline.sh" <<STAGE_EOF
#!/bin/bash
echo implement-pipeline.sh >> "\$INVOCATIONS_LOG"
: > "$sb/in-read"
/bin/sleep 20
STAGE_EOF
  _run_signal "$sb" INT EXP-18
  # As in (a): 130 comes from the runtime either way; the message and the empty
  # alert log are the proof.
  assert_eq "$READS_RC" 130 "Ctrl-C during a stage: exit" || { cat "$sb/shepherd.err"; return 1; }
  grep -q "interrupted by SIGINT — cancelled" "$sb/shepherd.err" \
    || { echo "FAIL: Ctrl-C during a stage: not ended as cancelled"; cat "$sb/shepherd.err"; return 1; }
  assert_eq "$(wc -l < "$sb/invocations.log" | tr -d ' ')" 1 "Ctrl-C during a stage: stage runs" || return 1
  if grep -q needs-human "$sb/labels.log" 2>/dev/null || [ -e "$sb/labels.log.comments" ] || [ -e "$sb/labels.log.alerts" ]; then
    echo "FAIL: Ctrl-C during a stage: a label, comment or alert was written"; cat "$sb/labels.log.alerts" 2>/dev/null; return 1
  fi
  grep -q "^-EXP-18"$'\t'"shepherd-focused" "$sb/labels.log" || { echo "FAIL: Ctrl-C during a stage: not released"; return 1; }

  # (c) SIGTERM to the shepherd alone during the wait: the wait is cut short, the
  # run ends as cancelled at once instead of after the minute.
  sb=$(make_sandbox sig_wait_direct)
  _use_real_linear_reads "$sb" || return 1
  _record_release "$sb"
  printf '%s' '{"data":{"issues":{"nodes":[]}}}' > "$sb/forms/nostate"
  printf '#!/bin/bash\necho "$PPID" > "%s/shepherd.pid"\necho "$$" > "%s/sleep.pid"\n: > "%s/in-read"\nexec /bin/sleep "$@"\n' "$sb" "$sb" "$sb" > "$sb/bin/sleep"
  printf '%s\n' build nostate build > "$sb/queue"
  _run_signal "$sb" TERM-SHEPHERD EXP-18
  assert_eq "$READS_RC" 130 "SIGTERM to the shepherd during the wait: exit" || { cat "$sb/shepherd.err"; return 1; }
  if kill -0 "$(cat "$sb/sleep.pid")" 2>/dev/null; then
    kill "$(cat "$sb/sleep.pid")" 2>/dev/null
    echo "FAIL: SIGTERM to the shepherd during the wait: its sleep was left running"; return 1
  fi
  [ "$(cat "$sb/signal-to-exit")" -lt 10 ] \
    || { echo "FAIL: SIGTERM to the shepherd during the wait: it waited out the minute"; return 1; }
  assert_eq "$(wc -l < "$sb/curl.log" | tr -d ' ')" 2 "SIGTERM to the shepherd during the wait: reads (the hold check and one state read, none after the signal)" || return 1
  _nothing_written "$sb" "SIGTERM to the shepherd during the wait" || return 1
  grep -q "^-EXP-18"$'\t'"shepherd-focused"$'\t'"single=1" "$sb/labels.log" \
    || { echo "FAIL: SIGTERM to the shepherd during the wait: not released with a single attempt"; cat "$sb/labels.log"; return 1; }

  # Negative controls with the code of before — the release-only trap and a plain
  # foreground sleep (half a second here): a SIGTERM to the shepherd is handled
  # once the sleep returns, and the loop reads on; an interrupted stage takes the
  # halt arm and alerts.
  sb=$(make_sandbox sig_wait_old)
  _use_real_linear_reads "$sb" || return 1
  _old_signal_trap "$sb" || return 1
  _mutate "$sb/scripts/shepherd.sh" '  sleep "$1" &
  SHEPHERD_SLEEP_PID=$!
  wait "$SHEPHERD_SLEEP_PID"' '  sleep "$1"' || return 1
  printf '%s' '{"data":{"issues":{"nodes":[]}}}' > "$sb/forms/nostate"
  printf '#!/bin/bash\necho "$PPID" > "%s/shepherd.pid"\n: > "%s/in-read"\nexec /bin/sleep 0.5\n' "$sb" "$sb" > "$sb/bin/sleep"
  printf '%s\n' build nostate build > "$sb/queue"
  _run_signal "$sb" TERM-SHEPHERD EXP-18
  [ "$(wc -l < "$sb/curl.log" | tr -d ' ')" -gt 2 ] \
    || { echo "FAIL: negative control: the old trap no longer reads on after SIGTERM, so this proves nothing"; return 1; }
  sb=$(make_sandbox sig_stage_old)
  _use_real_linear_reads "$sb" || return 1
  _old_signal_trap "$sb" || return 1
  rm -f "$sb/bin/sleep"; echo build > "$sb/queue"
  cat > "$sb/scripts/implement-pipeline.sh" <<STAGE_EOF
#!/bin/bash
echo implement-pipeline.sh >> "\$INVOCATIONS_LOG"
: > "$sb/in-read"
/bin/sleep 20
STAGE_EOF
  _run_signal "$sb" INT EXP-18
  [ -s "$sb/labels.log.alerts" ] \
    || { echo "FAIL: negative control: the old trap no longer alerts after an interrupted stage, so this proves nothing"; return 1; }
  return 0
}

# Scenario 19: a move is confirmed before the next stage — a read that still shows
# the state the stage left is read again, so the stage does not start twice
# A read that shows the new state costs no extra read and no wait.
test_move_confirmed_before_next_stage() {
  local sb
  _stale_reads() {
    cat >> "$1/scripts/bureau-config.sh" <<'STALE_EOF'
# The move lands, but the next read(s) still show the state it left: $STATE_FILE.times
# (default 1) stale reads per move, only for the first move when $STATE_FILE.once
# exists; with $STATE_FILE.empty, one answer without a state follows the stale one.
move_issue() {
  local old; old=$(cat "$STATE_FILE" 2>/dev/null || echo "")
  printf '%s' "$2" > "$STATE_FILE"
  [ "$old" != "$2" ] || return 0
  if [ ! -e "$STATE_FILE.once" ] || [ ! -e "$STATE_FILE.once-done" ]; then
    printf '%s' "$old" > "$STATE_FILE.stale"; cat "$STATE_FILE.times" 2>/dev/null > "$STATE_FILE.left" || true
    [ -s "$STATE_FILE.left" ] || echo 1 > "$STATE_FILE.left"
    : > "$STATE_FILE.once-done"
  fi
  return 0
}
get_issue_state() {
  echo read >> "$LABEL_LOG.reads"
  # A run that never settles (a shepherd acting on the moment-old Spec bumps the
  # finished spec back, the stubs move on regardless, and no state repeats, so the
  # stuck detector never fires) ends at the 60th read: the read fails and halts.
  [ "$(wc -l < "$LABEL_LOG.reads")" -lt 60 ] || return 1
  local left
  if [ -s "$STATE_FILE.stale" ]; then
    left=$(cat "$STATE_FILE.left"); _uuid_to_name "$(cat "$STATE_FILE.stale")"
    if [ "$left" -le 1 ]; then rm -f "$STATE_FILE.stale"; [ -e "$STATE_FILE.empty" ] && : > "$STATE_FILE.empty-next"; else echo $((left - 1)) > "$STATE_FILE.left"; fi
    return 0
  fi
  if [ -e "$STATE_FILE.empty-next" ]; then rm -f "$STATE_FILE.empty-next" "$STATE_FILE.empty"; return 0; fi
  _uuid_to_name "$(cat "$STATE_FILE" 2>/dev/null || echo "")"
}
STALE_EOF
  }
  local happy="spec-pipeline.sh spec-review-pipeline.sh implement-pipeline.sh code-review-pipeline.sh merge-pipeline.sh"
  sb=$(make_sandbox stale)
  _stale_reads "$sb"; _record_sleeps "$sb" 40
  echo s1 > "$sb/state.txt"
  set +e; PATH="$sb/bin:$PATH" run_shepherd "$sb" EXP-19; local rc=$?; set -e
  assert_eq "$rc" 0 "stale reads: exit" || { cat "$sb/shepherd.err"; return 1; }
  assert_eq "$(tr '\n' ' ' < "$sb/invocations.log" | sed 's/ $//')" "$happy" "stale reads: each stage runs once" || return 1
  # One stale read per stage, each read again after the default 5 s.
  assert_eq "$(tr '\n' ' ' < "$sb/sleeps.log" | sed 's/ $//')" "5 5 5 5 5" "stale reads: the waits" || return 1

  # After the spec stage, a read that is a
  # moment old shows the Spec in between. It is read again, and the finished spec is
  # not bumped back to Triage. Every move is recorded.
  local stale_moves='move_issue() { echo "$2" >> "$LABEL_LOG.moves"; local old; old=$(cat "$STATE_FILE"); printf "%s" "$2" > "$STATE_FILE"; [ "$old" = "$2" ] || { printf "%s" "$old" > "$STATE_FILE.stale"; echo 1 > "$STATE_FILE.left"; }; }'
  sb=$(make_sandbox stale_spec_between)
  _stale_reads "$sb"; _record_sleeps "$sb" 40
  echo "$stale_moves" >> "$sb/scripts/bureau-config.sh"
  echo s1 > "$sb/state.txt"
  set +e; PATH="$sb/bin:$PATH" run_shepherd "$sb" EXP-19; rc=$?; set -e
  assert_eq "$rc" 0 "the Spec in between: exit" || { cat "$sb/shepherd.err"; return 1; }
  assert_eq "$(tr '\n' ' ' < "$sb/labels.log.moves" | sed 's/ $//')" "s2 s3 s5 s6 s7 s8" "the Spec in between: no move back to Triage" || return 1
  grep -q "EXP-19 still reads 'Spec' after the move" "$sb/shepherd.out" \
    || { echo "FAIL: the Spec in between: it was not read again"; cat "$sb/shepherd.out"; return 1; }
  if grep -q "auto-bump" "$sb/shepherd.out"; then echo "FAIL: the Spec in between: bumped"; return 1; fi
  # Negative control: without MOVED_VIA the Spec in between counts as confirmed and the
  # bump sends the finished spec back to Triage, as an installation observed twice.
  sb=$(make_sandbox stale_spec_between_old)
  _stale_reads "$sb"; _record_sleeps "$sb" 40
  echo "$stale_moves" >> "$sb/scripts/bureau-config.sh"
  _mutate "$sb/scripts/shepherd.sh" '0:spec-pipeline.sh) MOVED_VIA="Spec" ;;' '0:spec-pipeline.sh) ;;' || return 1
  echo s1 > "$sb/state.txt"
  set +e; PATH="$sb/bin:$PATH" run_shepherd "$sb" EXP-19; set -e
  grep -q '^s1$' "$sb/labels.log.moves" && grep -q "auto-bump" "$sb/shepherd.out" \
    || { echo "FAIL: negative control: the old shepherd did not bump the finished spec, so this proves nothing"; cat "$sb/labels.log.moves"; return 1; }

  # A read that stays moment-old twice is read again twice: each stage still once.
  sb=$(make_sandbox stale_twice)
  _stale_reads "$sb"; _record_sleeps "$sb" 40
  echo 2 > "$sb/state.txt.times"; : > "$sb/state.txt.once"
  echo s1 > "$sb/state.txt"
  set +e; PATH="$sb/bin:$PATH" run_shepherd "$sb" EXP-19; rc=$?; set -e
  assert_eq "$rc" 0 "twice stale: exit" || { cat "$sb/shepherd.err"; return 1; }
  assert_eq "$(tr '\n' ' ' < "$sb/invocations.log" | sed 's/ $//')" "$happy" "twice stale: each stage runs once" || return 1
  assert_eq "$(tr '\n' ' ' < "$sb/sleeps.log" | sed 's/ $//')" "5 5" "twice stale: the waits" || return 1

  # An answer without a state while confirming is the no-state case, not a state
  # no pipeline knows: one 60 s wait, then on — no needs-human.
  sb=$(make_sandbox stale_empty)
  _stale_reads "$sb"; _record_sleeps "$sb" 40
  : > "$sb/state.txt.once"; : > "$sb/state.txt.empty"
  echo s1 > "$sb/state.txt"
  set +e; PATH="$sb/bin:$PATH" run_shepherd "$sb" EXP-19; rc=$?; set -e
  assert_eq "$rc" 0 "stale, then no state: exit" || { cat "$sb/shepherd.err"; return 1; }
  assert_eq "$(tr '\n' ' ' < "$sb/invocations.log" | sed 's/ $//')" "$happy" "stale, then no state: stages" || return 1
  # The empty answer comes while confirming; the loop goes straight back to its top
  # read, which shows the state — no 60 s wait, no halt.
  assert_eq "$(tr '\n' ' ' < "$sb/sleeps.log" | sed 's/ $//')" "5" "stale, then no state: the waits" || return 1
  if grep -q needs-human "$sb/labels.log" 2>/dev/null; then echo "FAIL: stale, then no state: labeled needs-human"; return 1; fi

  # --from-stage is confirmed against its target: the operator's Build runs even
  # when the first read after the move is moment-old (it showed Build Review).
  sb=$(make_sandbox stale_from_stage)
  _stale_reads "$sb"; _record_sleeps "$sb" 40
  : > "$sb/state.txt.once"
  echo s6 > "$sb/state.txt"
  set +e; PATH="$sb/bin:$PATH" run_shepherd "$sb" --from-stage build EXP-19; rc=$?; set -e
  assert_eq "$rc" 0 "stale read after --from-stage: exit" || { cat "$sb/shepherd.err"; return 1; }
  assert_eq "$(tr '\n' ' ' < "$sb/invocations.log" | sed 's/ $//')" \
    "implement-pipeline.sh code-review-pipeline.sh merge-pipeline.sh" "stale read after --from-stage: stages" || return 1
  assert_eq "$(head -1 "$sb/sleeps.log")" 5 "stale read after --from-stage: waited to confirm" || return 1
  # Negative control: without the target the moment-old Build Review is acted on.
  sb=$(make_sandbox stale_from_stage_old)
  _stale_reads "$sb"; _record_sleeps "$sb" 40
  _mutate "$sb/scripts/shepherd.sh" '  MOVED_TO="$TARGET_NAME"
' '' || return 1
  : > "$sb/state.txt.once"
  echo s6 > "$sb/state.txt"
  set +e; PATH="$sb/bin:$PATH" run_shepherd "$sb" --from-stage build EXP-19; set -e
  if grep -q '^implement-pipeline.sh$' "$sb/invocations.log"; then
    echo "FAIL: negative control: --from-stage without its target still ran Build, so this proves nothing"; return 1
  fi

  # A wait that is not a whole number falls back to 5 s, with a warning.
  sb=$(make_sandbox stale_bad_wait)
  _stale_reads "$sb"; _record_sleeps "$sb" 40
  : > "$sb/state.txt.once"
  echo s1 > "$sb/state.txt"
  set +e; BUREAU_SHEPHERD_CONFIRM_SECONDS=abc PATH="$sb/bin:$PATH" run_shepherd "$sb" EXP-19; rc=$?; set -e
  assert_eq "$rc" 0 "confirm wait 'abc': exit" || { cat "$sb/shepherd.err"; return 1; }
  assert_eq "$(cat "$sb/sleeps.log")" 5 "confirm wait 'abc': falls back to 5" || return 1
  grep -q "BUREAU_SHEPHERD_CONFIRM_SECONDS='abc' is not a whole number" "$sb/shepherd.err" \
    || { echo "FAIL: confirm wait 'abc': no warning"; return 1; }

  # The shepherd's own bump from Spec to Triage is confirmed the same way: one
  # move, not a second one on the moment-old "Spec".
  sb=$(make_sandbox stale_bump)
  _stale_reads "$sb"; _record_sleeps "$sb" 40
  echo 'move_issue() { echo "$2" >> "$LABEL_LOG.moves"; local old; old=$(cat "$STATE_FILE"); printf "%s" "$2" > "$STATE_FILE"; [ "$old" = "$2" ] || { printf "%s" "$old" > "$STATE_FILE.stale"; echo 1 > "$STATE_FILE.left"; }; }' >> "$sb/scripts/bureau-config.sh"
  echo s2 > "$sb/state.txt"
  set +e; PATH="$sb/bin:$PATH" run_shepherd "$sb" EXP-19; rc=$?; set -e
  assert_eq "$rc" 0 "stale read after the bump: exit" || { cat "$sb/shepherd.err"; return 1; }
  assert_eq "$(grep -c '^s1$' "$sb/labels.log.moves")" 1 "stale read after the bump: moves to Triage" || return 1

  # Fresh reads: the loop reads once per state, waits never.
  sb=$(make_sandbox fresh)
  echo 'get_issue_state() { echo read >> "$LABEL_LOG.reads"; _uuid_to_name "$(cat "$STATE_FILE" 2>/dev/null || echo "")"; }' >> "$sb/scripts/bureau-config.sh"
  _record_sleeps "$sb" 40
  echo s1 > "$sb/state.txt"
  set +e; PATH="$sb/bin:$PATH" run_shepherd "$sb" EXP-19; rc=$?; set -e
  assert_eq "$rc" 0 "fresh reads: exit" || return 1
  assert_eq "$(wc -l < "$sb/labels.log.reads" | tr -d ' ')" 6 "fresh reads: one read per state (Triage … Done)" || return 1
  [ ! -e "$sb/sleeps.log" ] || { echo "FAIL: fresh reads: the shepherd waited"; cat "$sb/sleeps.log"; return 1; }

  # A real Spec costs nothing extra: spec review sends the ticket back to Spec once,
  # the bump follows at once, and the second round goes on to Done without a wait.
  sb=$(make_sandbox fresh_spec_back)
  echo 'get_issue_state() { echo read >> "$LABEL_LOG.reads"; _uuid_to_name "$(cat "$STATE_FILE" 2>/dev/null || echo "")"; }' >> "$sb/scripts/bureau-config.sh"
  cat > "$sb/scripts/spec-review-pipeline.sh" <<'SPEC_BACK_EOF'
#!/bin/bash
set -euo pipefail
source "$(dirname "$0")/bureau-config.sh"
ISSUE="${1:-}"
echo spec-review-pipeline.sh >> "$INVOCATIONS_LOG"
if [ "$(grep -c '^spec-review-pipeline.sh$' "$INVOCATIONS_LOG")" = 1 ]; then move_issue "$ISSUE" s2; else move_issue "$ISSUE" s5; fi
exit 0
SPEC_BACK_EOF
  _record_sleeps "$sb" 40
  echo s1 > "$sb/state.txt"
  set +e; PATH="$sb/bin:$PATH" run_shepherd "$sb" EXP-19; rc=$?; set -e
  assert_eq "$rc" 0 "spec sent back: exit" || { cat "$sb/shepherd.err"; return 1; }
  assert_eq "$(tr '\n' ' ' < "$sb/invocations.log" | sed 's/ $//')" \
    "spec-pipeline.sh spec-review-pipeline.sh spec-pipeline.sh spec-review-pipeline.sh implement-pipeline.sh code-review-pipeline.sh merge-pipeline.sh" \
    "spec sent back: stages" || return 1
  [ ! -e "$sb/sleeps.log" ] || { echo "FAIL: spec sent back: the shepherd waited before the bump"; cat "$sb/sleeps.log"; return 1; }

  # Negative control: without the confirmation the stale read starts the stage again.
  sb=$(make_sandbox stale_old)
  _stale_reads "$sb"; _record_sleeps "$sb" 40
  _mutate "$sb/scripts/shepherd.sh" 'CONFIRM_TRIES=3' 'CONFIRM_TRIES=0' || return 1
  echo s1 > "$sb/state.txt"
  set +e; PATH="$sb/bin:$PATH" run_shepherd "$sb" EXP-19; set -e
  [ "$(grep -c '^spec-pipeline.sh$' "$sb/invocations.log")" -ge 2 ] \
    || { echo "FAIL: negative control: a stale read no longer starts the stage twice, so this proves nothing"; return 1; }
  return 0
}

# Scenario 20: Linear answering without a state is not waited out forever: the
# fifth such answer in a row halts with 1, needs-human, a comment and an alert.
test_no_state_is_bounded() {
  local sb
  sb=$(make_sandbox no_state)
  _use_real_linear_reads "$sb" || return 1
  printf '%s' '{"data":{"issues":{"nodes":[]}}}' > "$sb/forms/nostate"
  _record_sleeps "$sb" 12
  _run_reads "$sb" build nostate -- EXP-20
  assert_eq "$READS_RC" 1 "no state: exit" || { cat "$sb/shepherd.err"; return 1; }
  assert_eq "$(tr '\n' ' ' < "$sb/sleeps.log" | sed 's/ $//')" "60 60 60 60" "no state: four waits of the default 60 s" || return 1
  assert_eq "$(wc -l < "$sb/curl.log" | tr -d ' ')" 6 "no state: reads (the hold check, then five state reads)" || return 1
  grep -q "^+EXP-20"$'\t'"needs-human" "$sb/labels.log" || { echo "FAIL: no state: no needs-human"; return 1; }
  grep -q "without a state" "$sb/labels.log.comments" 2>/dev/null || { echo "FAIL: no state: no halt comment"; return 1; }
  grep -qx "shepherd halt (no state in 5 answers)" "$sb/labels.log.alerts" 2>/dev/null \
    || { echo "FAIL: no state: no alert"; cat "$sb/labels.log.alerts" 2>/dev/null; return 1; }
  _no_fault_files "$sb" || return 1

  # The count restarts after an answer with a state: four, a state and a stage,
  # then one more — no halt, the run ends at Done.
  sb=$(make_sandbox no_state_reset)
  _use_real_linear_reads "$sb" || return 1
  printf '%s' '{"data":{"issues":{"nodes":[]}}}' > "$sb/forms/nostate"
  _record_sleeps "$sb" 12
  _run_reads "$sb" build nostate nostate nostate nostate build build build nostate done -- EXP-20
  assert_eq "$READS_RC" 0 "no state, then a state: exit" || { cat "$sb/shepherd.err"; return 1; }
  assert_eq "$(wc -l < "$sb/sleeps.log" | tr -d ' ')" 5 "no state, then a state: waits" || return 1

  # Negative control: without the bound the shepherd waits on until the stub kills it.
  sb=$(make_sandbox no_state_old)
  _use_real_linear_reads "$sb" || return 1
  printf '%s' '{"data":{"issues":{"nodes":[]}}}' > "$sb/forms/nostate"
  _record_sleeps "$sb" 12
  _mutate "$sb/scripts/shepherd.sh" '[ "$NO_STATE_COUNT" -ge "$MAX_NO_STATE" ] && _shepherd_no_state_halt' ':' || return 1
  _run_reads "$sb" build nostate -- EXP-20
  [ "$(wc -l < "$sb/sleeps.log" | tr -d ' ')" -ge 12 ] \
    || { echo "FAIL: negative control: the unbounded wait ended by itself, so this proves nothing"; return 1; }
  if grep -q needs-human "$sb/labels.log" 2>/dev/null; then
    echo "FAIL: negative control: the unbounded wait labeled the ticket, so this proves nothing"; return 1
  fi
  return 0
}

# Scenario 21: a relative --worktree (the form the help recommends), started from the
# repo root. Every stage runs in that worktree, and nothing fails after a stage.
# Before, the relative path went to bureau-worker.sh as it was; the worker changes
# into the worktree and its EXIT cleanup ran `git -C <relative>` from there — exit
# 128 after a finished stage (observed in the rc.1 pilot). Either fix alone closes it:
# the shepherd makes the path absolute, and the worker does the same for any caller.
_run_relative_worktree() {  # $1 = sandbox, $2 = form (split|equals); sets RELWT_RC
  local sb="$1" s
  for s in "$sb"/scripts/*-pipeline.sh; do
    _mutate "$s" 'echo "' 'pwd -P >> "$INVOCATIONS_LOG.cwd"
echo "' || return 1
  done
  echo s1 > "$sb/state.txt"
  set +e
  if [ "$2" = equals ]; then run_shepherd "$sb" --worktree=.worktrees/shepherd-EXP-21 EXP-21
  else run_shepherd "$sb" --worktree .worktrees/shepherd-EXP-21 EXP-21; fi
  RELWT_RC=$?
  set -e
}
_old_worktree_shepherd() {
  python3 - "$1/scripts/shepherd.sh" <<'PY_EOF' || { echo "FAIL: could not build the negative control"; return 1; }
import pathlib, re, sys
p = pathlib.Path(sys.argv[1]); t = p.read_text()
t, n = re.subn(r'\nif \[ -n "\$WORKTREE_OVERRIDE" \]; then\n.*?\nfi\n', '\n', t, flags=re.S)
if n != 1: sys.exit(1)
p.write_text(t)
PY_EOF
}
_old_worktree_worker() {
  _mutate "$1/scripts/bureau-worker.sh" 'case "$WORKTREE" in /*) ;; *) WORKTREE="$REPO_DIR/$WORKTREE" ;; esac' ':'
}
test_relative_worktree() {
  local sb form want combo
  local happy="spec-pipeline.sh spec-review-pipeline.sh implement-pipeline.sh code-review-pipeline.sh merge-pipeline.sh"
  # Both fixes, each form; then each fix alone (the other put back to the old line).
  for combo in 'both|split' 'both|equals' 'shepherd|split' 'worker|split'; do
    form=${combo#*|}
    sb=$(make_sandbox "relwt_${combo%|*}_$form")
    case "${combo%|*}" in
      shepherd) _old_worktree_worker "$sb" || return 1 ;;
      worker)   _old_worktree_shepherd "$sb" || return 1 ;;
    esac
    _run_relative_worktree "$sb" "$form" || return 1
    assert_eq "$RELWT_RC" 0 "relative --worktree ($combo): exit" || { tail -5 "$sb/shepherd.out" "$sb/shepherd.err"; return 1; }
    assert_eq "$(tr '\n' ' ' < "$sb/invocations.log" | sed 's/ $//')" "$happy" "relative --worktree ($combo): stages" || return 1
    want=$(cd "$sb/.worktrees/shepherd-EXP-21" && pwd -P)
    if grep -vqxF "$want" "$sb/invocations.log.cwd"; then
      echo "FAIL: relative --worktree ($combo): a stage ran outside $want"; sort -u "$sb/invocations.log.cwd"; return 1
    fi
    if grep -q 'cannot change to' "$sb/shepherd.err"; then
      echo "FAIL: relative --worktree ($combo): git could not change into the worktree"; return 1
    fi
  done

  # The command handed on (to the tmux window, and to the re-exec under the runtime)
  # carries the absolute path too, in both flag forms. A stub tmux records it.
  for form in '--worktree .worktrees/shepherd-EXP-21' '--worktree=.worktrees/shepherd-EXP-21'; do
    sb=$(make_sandbox relwt_tmux)
    mkdir -p "$sb/bin"
    printf '#!/bin/bash\ncase "$1" in has-session) exit 1 ;; esac\nprintf "%%s\\n" "${@: -1}" > "%s/tmux.cmd"\n' "$sb" > "$sb/bin/tmux"
    chmod +x "$sb/bin/tmux"
    ( cd "$sb" && env -u TMUX PATH="$sb/bin:$PATH" bash scripts/shepherd.sh $form EXP-21 >/dev/null 2>&1 )
    grep -qF -- "$sb/.worktrees/shepherd-EXP-21" "$sb/tmux.cmd" 2>/dev/null \
      || { echo "FAIL: '$form': the command handed to tmux does not carry the absolute worktree"; cat "$sb/tmux.cmd" 2>/dev/null; return 1; }
    if grep -qE -- "--worktree[= ]\.worktrees" "$sb/tmux.cmd"; then
      echo "FAIL: '$form': the command handed to tmux still carries the relative worktree"; cat "$sb/tmux.cmd"; return 1
    fi
  done

  # Negative control: both old lines — the worker's cleanup fails after the first
  # stage, the shepherd halts with 128.
  sb=$(make_sandbox relwt_old)
  _old_worktree_shepherd "$sb" || return 1
  _old_worktree_worker "$sb" || return 1
  _run_relative_worktree "$sb" split || return 1
  assert_eq "$RELWT_RC" 128 "negative control: the old relative --worktree ends with 128" || { tail -5 "$sb/shepherd.err"; return 1; }
  grep -q 'cannot change to' "$sb/shepherd.err" \
    || { echo "FAIL: negative control: the old relative --worktree no longer fails in git, so this proves nothing"; return 1; }
  return 0
}

# ── Scenario 22 (v3.0.1): the shepherd at the merge gate ─────────────────
# A pilot run met a merge stage that did not merge (red CI) and ended
# with 0: the shepherd printed "still reads 'Merge' after the move", ran the stage
# again and its stuck detector labeled the ticket (13). The merge stage now
# reports its gate (tests/test_merge_pipeline_correctness.sh, test_gate_outcome):
# 2 + "not-yet" is waited for, 25 + "blocked" halts with the gate report.
# _make_gate_merge_stub <sb> <outcome…> — a merge stage that plays one outcome
# per call: pending (2, not-yet), red (25, blocked), green (moves to Done, 0),
# old (v3.0.0: 0 without a report, nothing moves).
_make_gate_merge_stub() {
  local sb="$1"; shift
  printf '%s\n' "$@" > "$sb/gate.plan"
  cat > "$sb/scripts/merge-pipeline.sh" <<'GATE_EOF'
#!/bin/bash
set -euo pipefail
source "$(dirname "$0")/bureau-config.sh"
ISSUE="${1:-}"
echo "merge-pipeline.sh" >> "$INVOCATIONS_LOG"
n=$(grep -c . "$INVOCATIONS_LOG")
plan="$(dirname "$STATE_FILE")/gate.plan"
step=$(sed -n "$(grep -c '^merge-pipeline.sh$' "$INVOCATIONS_LOG")p" "$plan")
[ -n "$step" ] || step=$(tail -n 1 "$plan")
report() { [ -n "${BUREAU_MERGE_GATE_REPORT:-}" ] && printf '%s\n%s\n' "$1" "$2" > "$BUREAU_MERGE_GATE_REPORT"; return 0; }
printf '%s\n' "${BUREAU_MERGE_GATE_REPORT:-}" >> "$(dirname "$STATE_FILE")/report.path"
case "$step" in
  pending) report not-yet "ci_green: ci: 1 check(s) still pending on HEAD_SHA"; exit 2 ;;
  red)     report blocked "ci_green: ci: failing check(s) on HEAD_SHA: build + test"; exit 25 ;;
  green)   move_issue "$ISSUE" s8; exit 0 ;;
  old)     exit 0 ;;
  bare2)   exit 2 ;;   # a 2 without a gate report of its own
  back)    move_issue "$ISSUE" s6; exit 0 ;;   # the ticket leaves Merge (the review stub brings it back)
  odd)     report not-yet "ci_green: ci: 1 check(s) still pending on HEAD_SHA"; exit 1 ;;   # a report that does not match its code
esac
GATE_EOF
  chmod +x "$sb/scripts/merge-pipeline.sh"
  echo "s7" > "$sb/state.txt"   # the ticket waits in Merge
}
_run_gate() {  # <sb> [env…] — sets GATE_RC; waits are recorded, never slept
  local sb="$1"; shift
  # The 30th wait kills the shepherd: a gate that is waited for without end (a
  # regression) fails the scenario instead of hanging the suite.
  _record_sleeps "$sb" 30; _record_writes "$sb"
  # The shepherd's temp files (fault file, gate report) go to the sandbox, so a
  # scenario that kills it leaves nothing in the user's $TMPDIR.
  mkdir -p "$sb/tmp"
  set +e
  ( cd "$sb" && env STATE_FILE="$sb/state.txt" INVOCATIONS_LOG="$sb/invocations.log" LABEL_LOG="$sb/labels.log" \
      PATH="$sb/bin:$PATH" TMPDIR="$sb/tmp" BUREAU_SHEPHERD_CONFIRM_SECONDS=0 "$@" \
      bash "$sb/scripts/shepherd.sh" --no-tmux EXP-22 > "$sb/shepherd.out" 2> "$sb/shepherd.err" )
  GATE_RC=$?
  set -e
}
_merge_calls() { grep -c '^merge-pipeline.sh$' "$1/invocations.log" 2>/dev/null || echo 0; }
_no_confirm_line() {
  ! grep -q "after the move" "$1/shepherd.out" \
    || { echo "FAIL: $2: the confirmation loop ran on a merge stage that did not move the ticket"; return 1; }
}

test_merge_gate() {
  local sb
  # Pending, pending, green: waits twice (the poll interval), merges, no label, no stuck.
  sb=$(make_sandbox gate_wait); _make_gate_merge_stub "$sb" pending pending green
  _run_gate "$sb" BUREAU_SHEPHERD_MERGE_POLL_SECONDS=7
  assert_eq "$GATE_RC" 0 "pending → green: the shepherd ends with 0" || { tail -5 "$sb/shepherd.out"; return 1; }
  assert_eq "$(_merge_calls "$sb")" 3 "pending → green: three merge passes" || return 1
  assert_eq "$(grep -c '^7$' "$sb/sleeps.log" 2>/dev/null || echo 0)" 2 "pending → green: two waits of the poll interval" || return 1
  grep -q "needs-human" "$sb/labels.log" 2>/dev/null && { echo "FAIL: pending → green labeled needs-human"; return 1; }
  grep -q "STUCK" "$sb/shepherd.out" && { echo "FAIL: pending → green hit the stuck detector"; return 1; }
  grep -q "merge gate not yet eligible — waiting 7s (0/1800s): ci_green: ci: 1 check(s) still pending" "$sb/shepherd.out" \
    || { echo "FAIL: pending: the wait does not name the gate"; cat "$sb/shepherd.out"; return 1; }
  _no_confirm_line "$sb" "pending → green" || return 1

  # Red: halts at once with 25, needs-human, the gate report in the comment, an alert.
  sb=$(make_sandbox gate_red); _make_gate_merge_stub "$sb" red
  _run_gate "$sb"
  assert_eq "$GATE_RC" 25 "red: the shepherd ends with 25" || { tail -5 "$sb/shepherd.out"; return 1; }
  assert_eq "$(_merge_calls "$sb")" 1 "red: one merge pass" || return 1
  grep -q "^+EXP-22"$'\t'"needs-human" "$sb/labels.log" || { echo "FAIL: red: no needs-human"; return 1; }
  grep -q "the merge gate is blocked" "$sb/labels.log.comments" && grep -q "failing check(s) on HEAD_SHA: build + test" "$sb/labels.log.comments" \
    || { echo "FAIL: red: the comment does not carry the gate report"; cat "$sb/labels.log.comments"; return 1; }
  grep -q "merge gate is blocked" "$sb/labels.log.alerts" || { echo "FAIL: red: no alert"; return 1; }
  grep -q "STUCK" "$sb/shepherd.out" && { echo "FAIL: red: reported as stuck"; return 1; }
  _no_confirm_line "$sb" red || return 1

  # The wait is bounded: pending forever, a 10 s budget at 5 s polls halts on the third pass.
  sb=$(make_sandbox gate_budget); _make_gate_merge_stub "$sb" pending
  _run_gate "$sb" BUREAU_SHEPHERD_MERGE_WAIT_SECONDS=10 BUREAU_SHEPHERD_MERGE_POLL_SECONDS=5
  assert_eq "$GATE_RC" 25 "budget: the shepherd ends with 25" || { tail -5 "$sb/shepherd.out"; return 1; }
  assert_eq "$(_merge_calls "$sb")" 3 "budget: three merge passes (0 s, 5 s, 10 s)" || return 1
  grep -q "was still not eligible after 10s" "$sb/labels.log.comments" && grep -q "still pending" "$sb/labels.log.comments" \
    || { echo "FAIL: budget: the comment does not say the wait ran out, with the gate"; cat "$sb/labels.log.comments"; return 1; }
  grep -q "^+EXP-22"$'\t'"needs-human" "$sb/labels.log" || { echo "FAIL: budget: no needs-human"; return 1; }

  # The wait starts again when the ticket comes back to Merge: 10 s used up, back
  # to Build Review and on to Merge again, the gate is waited for once more.
  sb=$(make_sandbox gate_again); _make_gate_merge_stub "$sb" pending pending back pending green
  _run_gate "$sb" BUREAU_SHEPHERD_MERGE_WAIT_SECONDS=10 BUREAU_SHEPHERD_MERGE_POLL_SECONDS=5
  assert_eq "$GATE_RC" 0 "back to Merge: the wait starts again and it merges" || { tail -5 "$sb/shepherd.out"; return 1; }
  grep -q "not yet eligible — waiting 5s (0/10s)" "$sb/shepherd.out" \
    && [ "$(grep -c 'not yet eligible — waiting 5s (0/10s)' "$sb/shepherd.out")" = 2 ] \
    || { echo "FAIL: back to Merge: the second visit did not start its wait at 0"; grep 'not yet' "$sb/shepherd.out"; return 1; }

  # The gate report counts only with the stage's own code: not-yet with exit 1 is an
  # error of the stage, not a wait.
  sb=$(make_sandbox gate_odd); _make_gate_merge_stub "$sb" odd green
  _run_gate "$sb"
  assert_eq "$GATE_RC" 1 "not-yet with exit 1: the shepherd halts with the stage's 1" || { tail -5 "$sb/shepherd.out"; return 1; }
  grep -q "not yet eligible" "$sb/shepherd.out" && { echo "FAIL: not-yet with exit 1 was waited for"; return 1; }
  grep -q '^bureau-merge-gate\.' <<< "$(ls "$sb/tmp")" && { echo "FAIL: the gate report file was left behind"; return 1; }
  case "$(head -n 1 "$sb/report.path")" in "$sb/tmp/bureau-merge-gate."*) ;;
    *) echo "FAIL: the gate report lives outside the sandbox's TMPDIR: $(head -n 1 "$sb/report.path")"; return 1 ;; esac

  # Leading zeros are base 10 ("08" used to be a syntax error, "010" eight seconds),
  # and the settings are bounded (a huge wait is capped at 6 h, a huge poll at 1 h).
  sb=$(make_sandbox gate_base10); _make_gate_merge_stub "$sb" pending pending green
  _run_gate "$sb" BUREAU_SHEPHERD_MERGE_POLL_SECONDS=08 BUREAU_SHEPHERD_MERGE_WAIT_SECONDS=010
  assert_eq "$GATE_RC" 0 "08/010: merges" || { tail -5 "$sb/shepherd.out"; cat "$sb/shepherd.err"; return 1; }
  grep -q "waiting 8s (8/10s)" "$sb/shepherd.out" || { echo "FAIL: 08/010 are not read as 8 and 10"; grep 'not yet' "$sb/shepherd.out"; return 1; }
  sb=$(make_sandbox gate_huge); _make_gate_merge_stub "$sb" pending green
  _run_gate "$sb" BUREAU_SHEPHERD_MERGE_WAIT_SECONDS=99999999999999999999 BUREAU_SHEPHERD_MERGE_POLL_SECONDS=86400
  assert_eq "$GATE_RC" 0 "huge settings: merges" || { tail -5 "$sb/shepherd.out"; return 1; }
  grep -q "MERGE_WAIT_SECONDS='99999999999999999999' is above 21600" "$sb/shepherd.err" && grep -q "waiting 3600s (0/21600s)" "$sb/shepherd.out" \
    || { echo "FAIL: huge settings were not capped"; cat "$sb/shepherd.err"; grep 'not yet' "$sb/shepherd.out"; return 1; }

  # Settings that are not whole numbers fall back with a warning (poll 0 too: a busy loop).
  sb=$(make_sandbox gate_badenv); _make_gate_merge_stub "$sb" pending green
  _run_gate "$sb" BUREAU_SHEPHERD_MERGE_WAIT_SECONDS=soon BUREAU_SHEPHERD_MERGE_POLL_SECONDS=0
  assert_eq "$GATE_RC" 0 "bad settings: still merges" || return 1
  grep -q "MERGE_WAIT_SECONDS='soon'" "$sb/shepherd.err" && grep -q "MERGE_POLL_SECONDS='0'" "$sb/shepherd.err" \
    || { echo "FAIL: bad settings: no warnings"; cat "$sb/shepherd.err"; return 1; }
  grep -q '^60$' "$sb/sleeps.log" || { echo "FAIL: bad settings: the poll did not fall back to 60 s"; return 1; }

  # Only the stage's own report counts: a 2 without one, after a waited pending,
  # takes the general path (the report file is emptied before every pass).
  sb=$(make_sandbox gate_bare2); _make_gate_merge_stub "$sb" pending bare2 green
  _run_gate "$sb"
  assert_eq "$GATE_RC" 0 "bare 2: still merges" || { tail -5 "$sb/shepherd.out"; return 1; }
  assert_eq "$(grep -c 'merge gate not yet eligible' "$sb/shepherd.out")" 1 "bare 2: only the pass with a report waits for the gate" || return 1
  grep -q "after the move" "$sb/shepherd.out" || { echo "FAIL: bare 2: a 2 without a report should take the general path"; return 1; }

  # Negative control 1: a v3.0.0 merge stage (0 without a report) — the confirmation
  # loop and the stuck detector, exactly as in the pilot run.
  sb=$(make_sandbox gate_old_stage); _make_gate_merge_stub "$sb" old
  _run_gate "$sb"
  assert_eq "$GATE_RC" 13 "negative control (v3.0.0 stage): stuck with 13" || { tail -5 "$sb/shepherd.out"; return 1; }
  grep -q "after the move" "$sb/shepherd.out" || { echo "FAIL: negative control (v3.0.0 stage): no confirmation loop, so this proves nothing"; return 1; }

  # Negative control 2: the v3.0.1 stage with the v3.0.0 shepherd (the gate branch
  # never matches) — a pending CI ends stuck within two passes.
  sb=$(make_sandbox gate_old_shepherd); _make_gate_merge_stub "$sb" pending pending pending green
  _mutate "$sb/scripts/shepherd.sh" 'case "$RC:$GATE_OUTCOME" in' 'case "off" in' || return 1
  _run_gate "$sb"
  assert_eq "$GATE_RC" 13 "negative control (v3.0.0 shepherd): a pending CI ends stuck" || { tail -5 "$sb/shepherd.out"; return 1; }
  return 0
}

# ── Run all scenarios ──────────────────────────────────────────────
FAILS=0
for scenario in test_happy_path test_no_merge test_dry_run test_stuck test_linear_unusable_halts test_block_halts \
                test_state_read_unusable_halts test_label_read_unusable_halts test_human_label_when_linear_answers \
                test_dry_run_read_unusable test_read_failure_other_code test_branch_read_unusable_halts \
                test_interrupted_read_is_cancelled test_merge_mode_manual test_review_stop_quiet \
                test_start_check_failure test_move_failure_halts test_signal_during_wait_and_stage \
                test_move_confirmed_before_next_stage test_no_state_is_bounded test_relative_worktree test_merge_gate; do
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
