#!/bin/bash
# Replay the production hook launch and EXIT trap with TERM immediately before
# PID registration. Delay environment setup until EXIT has requested cancellation
# (or the broken stage has exited), so no ProcessHold can hide the launch race.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PYTHONDONTWRITEBYTECODE=1 python3 - "$ROOT" <<'PY'
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

scripts = Path(sys.argv[1]) / 'templates/scripts'
source = (scripts / 'implement-pipeline.sh').read_text()
definitions = source[source.index('POST_IMPLEMENT_FAILED=0\n'):source.index('\nopen_or_update_pr_draft() {')]
exit_trap = source[source.index('_push_on_exit() {'):source.index('\ntrap _push_on_exit EXIT')]
fixture = r'''
set -euo pipefail
cd "$1"
export CASE_DIR="$1" TMPDIR="$1"
_BUREAU_SCRIPTS_DIR="$2"
ISSUE=TST-1; BRANCH=fixture; TOTAL_TIMEOUT=30; STATUS=COMPLETE; COMMITS_TOTAL=1
EXIT_PUSH_ARMED=1; IMPLEMENT_GATE_TMP="$1/gate"; mkdir -p "$IMPLEMENT_GATE_TMP"
# Keep the capture pipe open until the wrapper has exited, even when a broken
# stage exits first. communicate() therefore drains the negative control too.
exec 9>&2
bureau_get() { printf 'printf "HOOK_START\n" >> "$CASE_DIR/events"'; }
bureau_number() { printf '%s\n' "$2"; }
git() {
  case "$*" in
    'rev-parse HEAD') echo pre-hook ;;
    'rev-parse --git-common-dir') printf '%s\n' "$CASE_DIR/common" ;;
    'status --porcelain --untracked-files=all') : ;;
    'status --porcelain -z --no-renames --untracked-files=all') : ;;
    *) echo "unexpected git: $*" >&2; return 1 ;;
  esac
}
_implement_process_checkpoint() { echo CHECKPOINT >> events; }
push_if_ahead() { echo PUSH >> events; }
wait() {
  local code=0
  builtin wait "$@" || code=$?
  echo JOIN >> events
  return "$code"
}
bureau_untrusted_env() {
  [ "$1" != --check ] || return 0
  : > "$CASE_DIR/env-ready"
  while [ ! -e "$CASE_DIR/release-env" ]; do sleep 0.01; done
  local code=0
  /usr/bin/env "$@" || code=$?
  echo ENV_DONE >> events
  return "$code"
}
'''
cleanup_trap = r'''
trap 'exit 143' TERM
trap 'if _push_on_exit; then code=0; else code=$?; fi; printf "STATE:%s:%s:%s\n" "$POST_IMPLEMENT_RUNNER_PID" "${POST_IMPLEMENT_LAUNCH_MARKER:-}" "$POST_IMPLEMENT_CANCEL_FILE" >> events; echo STAGE_EXIT >> events; exit "$code"' EXIT
'''
failures = []


def check(condition, message, events):
    if not condition:
        failures.append(message + ': ' + repr(events))


with tempfile.TemporaryDirectory(prefix='bureau-launch-cancel.') as temp:
    root = Path(temp)
    case = root / 'pid-gap'
    case.mkdir()
    injection = r'''
inject_term() {
  if [ "$BASH_COMMAND" = 'POST_IMPLEMENT_RUNNER_PID=$!' ]; then
    trap - DEBUG
    echo WINDOW >> events
    while [ ! -e "$CASE_DIR/env-ready" ]; do sleep 0.01; done
    kill -TERM "$$"
  fi
}
set -T
trap inject_term DEBUG
run_post_implement_command
'''
    script = fixture + definitions + '\n' + exit_trap + cleanup_trap + injection
    proc = subprocess.Popen(['/bin/bash', '-c', script, 'fixture', str(case), str(scripts)],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        deadline = time.monotonic() + 10
        while not list(case.glob('*.cancel')) and proc.poll() is None and time.monotonic() < deadline:
            time.sleep(0.01)
        check(bool(list(case.glob('*.cancel'))) or proc.poll() is not None,
              'stage never reached cancellation', [])
    finally:
        (case / 'release-env').touch()
    out, err = proc.communicate(timeout=10)
    events = (case / 'events').read_text().splitlines()
    check(proc.returncode == 143, 'TERM must retain exit 143', [proc.returncode, out, err])
    check('WINDOW' in events, 'TERM was not injected at PID registration', events)
    check('HOOK_START' not in events, 'hook ran despite cancellation during environment setup', events)
    check(all(event in events for event in ('ENV_DONE', 'JOIN', 'CHECKPOINT', 'PUSH', 'STAGE_EXIT')),
          'EXIT did not join and preserve the pre-hook work', events)
    if all(event in events for event in ('ENV_DONE', 'JOIN', 'CHECKPOINT', 'PUSH', 'STAGE_EXIT')):
        check([events.index(event) for event in ('ENV_DONE', 'JOIN', 'CHECKPOINT', 'PUSH', 'STAGE_EXIT')]
              == sorted(events.index(event) for event in ('ENV_DONE', 'JOIN', 'CHECKPOINT', 'PUSH', 'STAGE_EXIT')),
              'publication or stage exit preceded the join', events)
    check('STATE:::' in events, 'launch state was not cleared', events)
    check(not list(case.glob('*.cancel')), 'cancel file leaked', events)

    # No launch, including a stale $! for an unrelated job still running. EXIT
    # must return before that job is released, rather than accidentally join it.
    for name, body in (
        ('never-launched', '_stop_post_implement_runner\n'),
        ('before-fork', r'''
(while [ ! -e "$CASE_DIR/release-unrelated" ]; do sleep 0.01; done) &
unrelated=$!
POST_IMPLEMENT_CANCEL_FILE="$CASE_DIR/pending.cancel"
POST_IMPLEMENT_LAUNCH_MARKER=$(set +u; printf 'previous:%s' "$!")
_stop_post_implement_runner
: > "$CASE_DIR/release-unrelated"
builtin wait "$unrelated"
'''),
        ('already-exited', r'''
POST_IMPLEMENT_CANCEL_FILE="$CASE_DIR/finished.cancel"
(exit 0) &
POST_IMPLEMENT_RUNNER_PID=$!
builtin wait "$POST_IMPLEMENT_RUNNER_PID"
_stop_post_implement_runner
_stop_post_implement_runner
'''),
    ):
        case = root / name
        case.mkdir()
        script = fixture + definitions + '\n' + body + r'''
printf 'STATE:%s:%s:%s\n' "$POST_IMPLEMENT_RUNNER_PID" "${POST_IMPLEMENT_LAUNCH_MARKER:-}" "$POST_IMPLEMENT_CANCEL_FILE" >> events
echo RETURNED >> events
'''
        proc = subprocess.Popen(['/bin/bash', '-c', script, 'fixture', str(case), str(scripts)],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            out, err = proc.communicate(timeout=3)
        except subprocess.TimeoutExpired:
            (case / 'release-unrelated').touch()
            out, err = proc.communicate(timeout=10)
            check(False, name + ' hung before an unrelated job was released', [])
        events = (case / 'events').read_text().splitlines() if (case / 'events').exists() else []
        check(proc.returncode == 0 and 'RETURNED' in events, name + ' did not return promptly', [out, err])
        check('STATE:::' in events, name + ' left launch state behind', events)
        check(not list(case.glob('*.cancel')), name + ' leaked a cancel file', events)

if failures:
    for failure in failures:
        print('FAIL ' + failure, file=sys.stderr)
    sys.exit(1)
print('OK test_post_implement_launch_cancel (Bash launch/EXIT replay and no-launch/exited cases)')
PY
