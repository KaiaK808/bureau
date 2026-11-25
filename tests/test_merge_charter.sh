#!/bin/bash
# Real Git and production policy/review parsers; only external services are fake.
# Unmeasured locally: the maintainer schedules this driver in the shared slot.
set -uo pipefail
export BUREAU_MERGE_TEST_LIBRARY_ONLY=1
source "$(dirname "$0")/test_merge_pipeline_correctness.sh"
unset BUREAU_MERGE_TEST_LIBRARY_ONLY
trap 'chmod -R u+rwx "$SANDBOX_ROOT" 2>/dev/null || true; rm -rf "$SANDBOX_ROOT"' EXIT
export GIT_ALLOW_PROTOCOL=file PYTHONDONTWRITEBYTECODE=1
export GIT_CONFIG_NOSYSTEM=1 GIT_CONFIG_GLOBAL=/dev/null
export GIT_CEILING_DIRECTORIES="$SANDBOX_ROOT"
mkdir -p "$SANDBOX_ROOT/deny"
for command in gh curl wget ssh linear claude codex; do
  printf '#!/bin/sh\necho "DENIED real transport" >&2\nexit 97\n' > "$SANDBOX_ROOT/deny/$command"
  chmod +x "$SANDBOX_ROOT/deny/$command"
done
export PATH="$SANDBOX_ROOT/deny:$PATH"
FAILS=0
CASE_SERIAL=0

charter_case() {
  local name="$1" mutation="${2:-}" sb rc expected=25
  CASE_SERIAL=$((CASE_SERIAL + 1))
  sb=$(make_sandbox "charter-$CASE_SERIAL-$name-${mutation:-normal}") || return 3
  populate_happy_fixtures "$sb"
  python3 -I -B "$REPO_ROOT/tests/charter_git_test.py" --prepare "$sb" "$name" "$mutation" || return 3
  case "$name" in clean|unenrolled|disabled-unenrolled|absent) expected=0 ;; esac
  # The inherited library expects its caller's cwd to be the test repository.
  ( cd "$sb" && BUREAU_MERGE_GATE_REPORT="$sb/stub_data/gate-report" run_pipeline "$sb" TEAM-1 ); rc=$?
  # Restore traversal before assertions or the shared sandbox's EXIT cleanup.
  if [ "$name" = non-searchable ]; then chmod 700 "$sb/.git/bureau/charter" || return 3; fi
  if [ "$rc" != "$expected" ]; then
    echo "FAIL charter $name ${mutation:-normal}: got $rc, expected $expected" >&2
    tail -12 "$sb/pipeline.out" "$sb/pipeline.err" >&2
    return 1
  fi
  if [ "$expected" = 25 ]; then
    [ ! -s "$sb/stub_data/merge_calls.log" ] || return 1
    if [ "$name" = unreadable ]; then
      grep -q '^  charter-unreadable:' "$sb/pipeline.out" || return 1
    else
      grep -q '^  charter-policy:' "$sb/pipeline.out" || return 1
    fi
    grep -q 'needs-human TEAM-1 merge 25' "$sb/stub_data/handoff.log" || return 1
    [ "$(sed -n '1p' "$sb/stub_data/gate-report")" = blocked ] || return 1
  else
    [ -s "$sb/stub_data/merge_calls.log" ] || return 1
    if [ "$name" != clean ]; then
      [ ! -e "$sb/.git/bureau/charter" ] || return 1
      # Only the existing publisher read pins main. Charter must add none.
      [ "$(grep -c '/commits/main$' "$sb/stub_data/charter_api.log")" = 1 ] || return 1
      ! grep -q 'charter-' "$sb/pipeline.out" || return 1
    fi
  fi
  return 0
}

for name in clean policy disabled-pr unreadable stale-policy non-searchable common-dir-failed unenrolled disabled-unenrolled absent; do
  if charter_case "$name"; then echo "PASS charter $name"; else FAILS=$((FAILS + 1)); fi
done
# Each observation passes with production code first. Removing its enforcement
# must make the SAME assertion fail, with every existing gate still green.
for pair in wiring:policy classification:policy unreadable:unreadable handoff:policy priority:stale-policy unconditional:unenrolled; do
  if charter_case "${pair#*:}"; then
    if charter_case "${pair#*:}" "${pair%%:*}"; then
      echo "FAIL charter negative control survived: $pair"; FAILS=$((FAILS + 1))
    else
      control_rc=$?
      if [ "$control_rc" = 1 ]; then echo "PASS charter negative control rejected: $pair"
      else echo "FAIL charter control setup: $pair"; FAILS=$((FAILS + 1)); fi
    fi
  else echo "FAIL charter control baseline: $pair"; FAILS=$((FAILS + 1)); fi
done
python3 -I -B "$REPO_ROOT/tests/charter_policy_test.py" || FAILS=$((FAILS + 1))
python3 -I -B "$REPO_ROOT/tests/charter_git_test.py" || FAILS=$((FAILS + 1))
[ "$FAILS" = 0 ] || { echo "FAIL charter assertions: $FAILS"; exit 1; }
echo 'OK test_merge_charter'
