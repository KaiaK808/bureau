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
  # The shepherd's exit-code decisions run for real: the table and the Linear exit code
  # come from the real config.
  sed -n -e '/^BUREAU_EXIT_LINEAR_UNUSABLE=/p' -e '/^shepherd_rc_action() {/,/^}/p' \
    "$REPO_ROOT/templates/scripts/bureau-config.sh" >> "$sb/scripts/bureau-config.sh"

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

# ── Scenarios 7–11: the shepherd's own Linear reads (EXP-1528) ─────
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
  local node='"id":"U1","identifier":"EXP-7","title":"T","description":"D","project":null'
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

# _put_old_reads <sb> — the negative control: today's reads (before EXP-1528),
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
  if ls "$1/tmp" 2>/dev/null | grep -q '^bureau-linear-fault\.'; then
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
  _run_reads "$sb" html -- EXP-7
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
  _run_reads "$sb2" html -- EXP-7
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
  _run_reads "$sb" build html -- EXP-7
  assert_eq "$READS_RC" 27 "shepherd exit when its label read gives up on Linear" || return 1
  assert_eq "$(tr '\n' ' ' < "$sb/curl.log")" "build html " "one state read, then one label read" || return 1
  [ ! -s "$sb/invocations.log" ] || { echo "FAIL: the shepherd walked on past an unread label list"; return 1; }
  grep -q 'reading the labels gave up because Linear stayed unusable.*not-json.*single=1$' "$sb/labels.log.comments" \
    || { echo "FAIL: the halt comment does not name the read and the fault class"; cat "$sb/labels.log.comments"; return 1; }
  grep -q "not-json" "$sb/labels.log.alerts" || { echo "FAIL: the alert does not name the fault class"; return 1; }
  _no_answer_text "$sb" || return 1

  # Negative control: today's label check reads the failure as "no label" and walks on.
  local sb2; sb2=$(make_sandbox label_read_old)
  _use_real_linear_reads "$sb2" && _put_old_reads "$sb2" || return 1
  _run_reads "$sb2" build html -- EXP-7
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
  _run_reads "$sb2" build build build done -- EXP-7
  assert_eq "$READS_RC" 0 "shepherd exit on a free ticket that reaches Done" || return 1
  assert_eq "$(tr '\n' ' ' < "$sb2/invocations.log")" "implement-pipeline.sh " "a free ticket runs its stage once" || return 1
  assert_eq "$(tr '\n' ' ' < "$sb2/curl.log")" "build build build done " "one state, label and branch read per iteration" || return 1
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
# $2 = a replacement read helper appended to the stub config.
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
               'detail_empty|get_issue_detail() { return 0; }|labels' \
               'detail_nolist|get_issue_detail() { printf "%s" "{}"; }|labels' \
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
  _run_reads "$sb" build build html -- EXP-7
  assert_eq "$READS_RC" 27 "shepherd exit when its branch read gives up on Linear" || return 1
  [ ! -s "$sb/invocations.log" ] || { echo "FAIL: a stage ran without its branch"; return 1; }
  grep -q 'reading the branch gave up because Linear stayed unusable.*not-json.*single=1$' "$sb/labels.log.comments" \
    || { echo "FAIL: the halt comment does not name the read and the fault class"; cat "$sb/labels.log.comments"; return 1; }
  _no_answer_text "$sb" || return 1

  # Negative control: today's `|| echo ""` hands the stage an empty branch (in a real
  # repo reset_worktree then ends it with 12; the sandbox's reset lets it run).
  local sb2; sb2=$(make_sandbox branch_read_old)
  _use_real_linear_reads "$sb2" && _put_old_reads "$sb2" || return 1
  _run_reads "$sb2" build build html -- EXP-7
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
    else: os.kill(p.pid, signal.SIGTERM)
    rc = p.wait(timeout=60)
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
  for case_ in 'INT|slow|state' 'INT|build slow|labels' 'INT|build build slow|branch' 'TERM|slow|state'; do
    sig=${case_%%|*}; what=${case_##*|}; queue=${case_#*|}; queue=${queue%|*}
    sb=$(make_sandbox "sig_${sig}_$what")
    _use_real_linear_reads "$sb" || return 1
    rm -f "$sb/bin/sleep"
    printf '%s\n' $queue > "$sb/queue"
    _run_signal "$sb" "$sig" EXP-7
    assert_eq "$READS_RC" 130 "$sig during the $what read: exit" || { cat "$sb/shepherd.err"; return 1; }
    grep -q "interrupted while reading the $what of EXP-7" "$sb/shepherd.err" \
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
  _run_bad_read "$sb" 'get_issue_detail() { return 143; }'
  assert_eq "$READS_RC" 130 "a label read killed by a signal: exit" || return 1
  _nothing_written "$sb" "a label read killed by a signal" || return 1
  sb=$(make_sandbox sig_code_dry)
  _run_bad_read "$sb" 'get_issue_state() { return 143; }' --dry-run EXP-8
  assert_eq "$READS_RC" 130 "a dry-run state read killed by a signal: exit" || return 1
  grep -q "dry-run: interrupted while reading the state of EXP-8" "$sb/shepherd.err" \
    || { echo "FAIL: the dry run did not end as cancelled"; cat "$sb/shepherd.err"; return 1; }
  _no_fault_files "$sb" || return 1

  # Negative controls. Without the signal branch the interrupted read reads as a
  # failed one and labels the ticket; without the dry run's trap the file stays.
  local sb2; sb2=$(make_sandbox sig_old)
  _use_real_linear_reads "$sb2" || return 1
  python3 - "$sb2/scripts/shepherd.sh" <<'PY_EOF' || { echo "FAIL: could not build the negative control"; return 1; }
import pathlib, re, sys
p = pathlib.Path(sys.argv[1]); t = p.read_text()
t, n = re.subn(r'\n  if \[ "\$rc" -gt 128 \]; then\n.*?\n  fi\n', '\n', t, flags=re.S)
if n != 1: sys.exit(1)
p.write_text(t)
PY_EOF
  rm -f "$sb2/bin/sleep"; echo slow > "$sb2/queue"
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
  if ! ls "$sb3/tmp" | grep -q '^bureau-linear-fault\.'; then
    echo "FAIL: negative control: the dry run's fault file goes away without the trap, so this proves nothing"; return 1
  fi
  return 0
}

# ── Run all scenarios ──────────────────────────────────────────────
FAILS=0
for scenario in test_happy_path test_no_merge test_dry_run test_stuck test_linear_unusable_halts test_block_halts \
                test_state_read_unusable_halts test_label_read_unusable_halts test_human_label_when_linear_answers \
                test_dry_run_read_unusable test_read_failure_other_code test_branch_read_unusable_halts \
                test_interrupted_read_is_cancelled; do
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
