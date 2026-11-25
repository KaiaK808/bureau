#!/bin/bash
# Real Git and production gates, including the actual inline APPROVE result arms.
# Unmeasured: the maintainer schedules this driver under the shared test slot.
set -uo pipefail
export BUREAU_MERGE_TEST_LIBRARY_ONLY=1
source "$(dirname "$0")/test_merge_pipeline_correctness.sh"
unset BUREAU_MERGE_TEST_LIBRARY_ONLY
trap 'chmod -R u+rwx "$SANDBOX_ROOT" 2>/dev/null || true; rm -rf "$SANDBOX_ROOT"' EXIT
export GIT_ALLOW_PROTOCOL=file PYTHONDONTWRITEBYTECODE=1
export GIT_CONFIG_NOSYSTEM=1 GIT_CONFIG_GLOBAL=/dev/null GIT_CEILING_DIRECTORIES="$SANDBOX_ROOT"
mkdir -p "$SANDBOX_ROOT/deny"
for command in gh curl wget ssh linear claude codex; do
  printf '#!/bin/sh\necho "DENIED real transport" >&2\nexit 97\n' > "$SANDBOX_ROOT/deny/$command"
  chmod +x "$SANDBOX_ROOT/deny/$command"
done
export PATH="$SANDBOX_ROOT/deny:$PATH"
ISSUE="TEST-$((1))"
SERIAL=0
FAILS=0

queue_case() {
  local name="$1" mode="$2" mutation="${3:-}" sb rc expected=25 class=security reads=1
  SERIAL=$((SERIAL + 1))
  sb=$(make_sandbox "queue-$SERIAL-$name-$mode-${mutation:-normal}") || return 3
  populate_happy_fixtures "$sb"
  python3 -I -B "$REPO_ROOT/tests/charter_queue_git_test.py" --prepare "$sb" "$name" "$mutation" || return 3
  case "$name" in
    clean|cycle|worktree-only) expected=0; reads=2 ;;
    unenrolled) expected=0; reads=0 ;;
    label-missing|wrong-cycle|unassigned-cycle|approval-read-failed) expected=2; class=charter-queue ;;
    jit-approval) expected=2; class=charter-queue; reads=2 ;;
    label) class=data ;;
    jit-exception) class=data; reads=2 ;;
    exception-read-failed|partial-labels|paginated-labels|partial-error|missing-cycle) class=charter-unclassified ;;
    unreadable-input|truncated-input|ambiguous) class=charter-unclassified; reads=0 ;;
  esac
  if [ "$mode" = inline ]; then
    (
      cd "$sb" || exit 3
      MERGE_REVIEW_FAKE="$REPO_ROOT/tests/lib/merge-review-fake.py" STUB_DIR="$sb/stub_data" \
        INVOCATIONS_LOG="$sb/gh_invocations.log" PATH="$sb/bin:$PATH" \
        BUREAU_MERGE_GATE_REPORT="$sb/stub_data/gate-report" \
        bash "$sb/inline.sh" "$ISSUE" > "$sb/pipeline.out" 2> "$sb/pipeline.err"
    ); rc=$?
  else
    BUREAU_MERGE_GATE_REPORT="$sb/stub_data/gate-report" run_pipeline "$sb" "$ISSUE"; rc=$?
  fi
  if [ "$rc" != "$expected" ]; then
    echo "FAIL queue $name/$mode ${mutation:-normal}: exit $rc, expected $expected" >&2
    tail -12 "$sb/pipeline.out" "$sb/pipeline.err" >&2
    return 1
  fi
  [ "$(cat "$sb/stub_data/linear-reads" 2>/dev/null || echo 0)" = "$reads" ] || return 1
  if [ "$expected" = 0 ]; then
    [ -s "$sb/stub_data/merge_calls.log" ] || return 1
    [ ! -s "$sb/stub_data/handoff.log" ] || return 1
    if [ "$name" = unenrolled ]; then
      [ ! -e "$sb/.git/bureau/charter" ] || return 1
      [ "$(grep -c '/commits/main$' "$sb/stub_data/charter_api.log")" = 1 ] || return 1
    fi
  else
    [ ! -s "$sb/stub_data/merge_calls.log" ] || return 1
    grep -q "$class" "$sb/stub_data/gate-report" || return 1
    if [ "$name" = multiple ]; then grep -q 'data' "$sb/stub_data/gate-report" || return 1; fi
    if [ "$expected" = 25 ]; then
      [ "$(sed -n '1p' "$sb/stub_data/gate-report")" = blocked ] || return 1
      if [ "$mode" = inline ]; then
        grep -q "needs-human $ISSUE code-review" "$sb/stub_data/handoff.log" || return 1
      else
        grep -q "needs-human $ISSUE merge 25" "$sb/stub_data/handoff.log" || return 1
      fi
    else
      [ "$(sed -n '1p' "$sb/stub_data/gate-report")" = not-yet ] || return 1
      [ ! -s "$sb/stub_data/handoff.log" ] || return 1
    fi
  fi
  return 0
}

for mode in dedicated inline; do
  for name in clean cycle label-missing wrong-cycle unassigned-cycle missing-cycle approval-read-failed exception-read-failed partial-labels paginated-labels partial-error path label renamed copied deleted multiple precedence stale-exception unreadable-input truncated-input ambiguous worktree-only unenrolled jit-approval jit-exception; do
    if queue_case "$name" "$mode"; then echo "PASS queue $name/$mode"; else FAILS=$((FAILS + 1)); fi
  done
done
# Baseline first, then remove one enforcement step from a private fixture copy.
# Setup errors (3) cannot masquerade as a rejected negative control (1).
for item in queue:label-missing queue:wrong-cycle path:path label:label classes:multiple rename:renamed rename:copied deleted:deleted read:exception-read-failed unclassified:truncated-input ambiguous:ambiguous priority:precedence stale:stale-exception outcome:label-missing dedicated-handoff:label inline-handoff:label ticket:clean credentials:clean enrollment:unenrolled; do
  mutation="${item%%:*}"; name="${item#*:}"; mode=dedicated
  [ "$mutation" != inline-handoff ] || mode=inline
  if queue_case "$name" "$mode"; then
    if queue_case "$name" "$mode" "$mutation"; then
      echo "FAIL queue negative control survived: $item"; FAILS=$((FAILS + 1))
    else
      rc=$?
      if [ "$rc" = 1 ]; then echo "PASS queue negative control rejected: $item"
      else echo "FAIL queue control setup: $item"; FAILS=$((FAILS + 1)); fi
    fi
  else echo "FAIL queue control baseline: $item"; FAILS=$((FAILS + 1)); fi
done
python3 -I -B "$REPO_ROOT/tests/charter_queue_test.py" || FAILS=$((FAILS + 1))
python3 -I -B "$REPO_ROOT/tests/charter_queue_git_test.py" || FAILS=$((FAILS + 1))
[ "$FAILS" = 0 ] || { echo "FAIL queue assertions: $FAILS"; exit 1; }
echo 'OK test_merge_charter_queue'
