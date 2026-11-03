#!/bin/bash
# Replay the production hook launch and EXIT trap with TERM immediately before
# PID registration, then a second TERM during the drain. File barriers pause
# environment setup / the earliest EXIT trap call / the post-hold cancellation check; deadlines are not used
# to guess when a signal should arrive.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PYTHONDONTWRITEBYTECODE=1 python3 - "$ROOT" <<'PY'
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time

scripts = Path(os.environ.get('BUREAU_TEST_LAUNCH_SCRIPTS', Path(sys.argv[1]) / 'templates/scripts'))
source = (scripts / 'implement-pipeline.sh').read_text()
definitions = source[source.index('POST_IMPLEMENT_FAILED=0\n'):source.index('\nopen_or_update_pr_draft() {')]
exit_trap = source[source.index('_push_on_exit() {'):source.index('\ntrap _push_on_exit EXIT')]
checkpoint = source[source.index('_implement_process_checkpoint() {'):source.index('\n}\n', source.index('_implement_process_checkpoint() {')) + 3]
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
    'merge-base --is-ancestor pre-hook HEAD') : ;;
    'rev-list --count pre-hook..HEAD') echo 0 ;;
    *) echo "unexpected git: $*" >&2; return 1 ;;
  esac
}
_implement_process_checkpoint() {
  local hold
  for hold in "$CASE_DIR"/common/bureau/process-holds/*.json; do
    [ ! -f "$hold" ] || { echo STALE_HOLD >> events; return 1; }
  done
  echo CHECKPOINT >> events
}
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
POST_IMPLEMENT_SIGNAL_TRAPS=$(trap -p HUP INT TERM)
trap 'if _push_on_exit; then code=0; else code=$?; fi; printf "STATE:%s:%s:%s\n" "$POST_IMPLEMENT_RUNNER_PID" "${POST_IMPLEMENT_LAUNCH_MARKER:-}" "$POST_IMPLEMENT_CANCEL_FILE" >> events; echo STAGE_EXIT >> events; exit "$code"' EXIT
'''
failures = []


def check(condition, message, events):
    if not condition:
        failures.append(message + ': ' + repr(events))


def wait_for(predicate, message):
    deadline = time.monotonic() + 10
    while not predicate():
        if time.monotonic() >= deadline:
            raise AssertionError(message)
        time.sleep(0.01)


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

    # Review-8 schedule: environment setup is still paused, TERM arrives before
    # PID registration, and a second TERM reaches EXIT at its first trap call.
    # Old code pauses inside the pre-mask `trap -p` substitution; mask-first
    # code pauses immediately after the builtin mask. Neither uses timing to
    # choose when to send a signal. Exercise explicit and default TERM traps.
    for term_handler in ('explicit', 'default'):
        case = root / ('early-exit-' + term_handler)
        case.mkdir()
        early_fixture = r'''
bureau_get() {
  printf 'echo HOOK_START >> "$CASE_DIR/events"; : > "$CASE_DIR/hook-ready"; while [ ! -e "$CASE_DIR/release-hook" ]; do sleep 0.01; done'
}
exit_barrier() {
  [ ! -e "$CASE_DIR/exit-ready" ] || return 0
  echo EARLY_EXIT >> events
  : > "$CASE_DIR/exit-ready"
  while [ ! -e "$CASE_DIR/second-term-sent" ]; do command sleep 0.01; done
}
wait() {
  local code=0 hold
  builtin wait "$@" || code=$?
  for hold in "$CASE_DIR"/common/bureau/process-holds/*.json; do
    [ ! -f "$hold" ] || echo "JOIN_HOLD:${hold##*/}" >> events
  done
  echo JOIN >> events
  return "$code"
}
trap() {
  if [ "${IN_EXIT:-0}" = 1 ] && [ "$*" = '-p HUP INT TERM' ]; then exit_barrier; fi
  builtin trap "$@"
  if [ "${IN_EXIT:-0}" = 1 ] && [ "$*" = ' HUP INT TERM' ]; then exit_barrier; fi
}
inject_term() {
  if [ "$BASH_COMMAND" = 'POST_IMPLEMENT_RUNNER_PID=$!' ]; then
    builtin trap - DEBUG
    echo WINDOW >> events
    while [ ! -e "$CASE_DIR/env-ready" ]; do command sleep 0.01; done
    : > "$CASE_DIR/fork-ready"
    while [ ! -e "$CASE_DIR/first-term-sent" ]; do command sleep 0.01; done
    kill -TERM "$$"
  fi
}
'''
        early_exit_trap = exit_trap.replace('local rc=$?', 'local rc=$?; IN_EXIT=1', 1)
        early_cleanup = cleanup_trap
        if term_handler == 'default':
            early_cleanup = early_cleanup.replace("trap 'exit 143' TERM", 'trap - TERM')
        script = (fixture + definitions + '\n' + early_exit_trap + early_fixture + early_cleanup
                  + '\nset -T\ntrap inject_term DEBUG\nrun_post_implement_command\n')
        proc = subprocess.Popen(['/bin/bash', '-c', script, 'fixture', str(case), str(scripts)],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        observations = []
        def observe(label):
            holds = sorted((case / 'common/bureau/process-holds').glob('*.json'))
            observations.append((label, [path.name for path in holds]))
            check(len(holds) == 1, 'early EXIT lacks its single pre-fork hold at ' + label, observations)
            if holds and observations[0][1]:
                check([path.name for path in holds] == observations[0][1],
                      'launch replaced its hold before joining', observations)
        try:
            wait_for(lambda: (case / 'fork-ready').exists(), 'environment wrapper never paused after fork')
            observe('after-fork')
            (case / 'first-term-sent').touch()
            wait_for(lambda: (case / 'exit-ready').exists(), 'EXIT never reached its first trap call')
            observe('earliest-exit')
            os.kill(proc.pid, signal.SIGTERM)
            (case / 'second-term-sent').touch()
            wait_for(lambda: proc.poll() is not None or bool(list(case.glob('*.cancel'))),
                     'second TERM neither ended EXIT nor reached cancellation')
            observe('after-second-term')
            if proc.poll() is not None:
                with (case / 'events').open('a') as stream: stream.write('STAGE_DEAD\n')
            (case / 'release-env').touch()
            wait_for(lambda: (case / 'hook-ready').exists() or 'ENV_DONE' in (case / 'events').read_text(),
                     'late environment wrapper never completed')
            if (case / 'hook-ready').exists():
                observe('late-hook-running')
        finally:
            for release in ('first-term-sent', 'second-term-sent', 'release-env', 'release-hook'):
                (case / release).touch()
            out, err = proc.communicate(timeout=20)
        events = (case / 'events').read_text().splitlines()
        code = 128 - proc.returncode if proc.returncode < 0 else proc.returncode
        check(code == 143, 'early double TERM must retain exit 143', [code, out, err])
        check('HOOK_START' not in events or ('STAGE_DEAD' in events and any(
                  label == 'late-hook-running' and holds for label, holds in observations)),
              'hook outlived the stage without its hold', events)
        for event in ('CHECKPOINT', 'PUSH', 'STAGE_EXIT'):
            if event in events:
                check('JOIN' in events and events.index('JOIN') < events.index(event),
                      'early EXIT ' + event + ' preceded the join', events)
        check(all(event in events for event in ('JOIN', 'CHECKPOINT', 'PUSH', 'STAGE_EXIT')),
              'early double TERM interrupted the completed drain', events)
        check(any(event == 'JOIN_HOLD:' + name for name in observations[0][1] for event in events),
              'pre-fork hold disappeared before the completed join', events)
        check(not list((case / 'common/bureau/process-holds').glob('*.json')),
              'completed early EXIT drain left a hold', events)
        print('early EXIT (%s): exit=%s holds=%s events=%s' %
              (term_handler, code, observations, ','.join(events)))

    # Pause after the old pre-hold check, or after hold creation on the fixed
    # runner. This reproduces a second TERM while EXIT drains the unregistered
    # job, without relying on when the OS happens to schedule either process.
    hold_line = next(line for line in definitions.splitlines() if line.startswith('hold = pg.ProcessHold'))
    cancel_line = 'if Path(status_file + ".cancel").exists():'
    pause_before = hold_line if definitions.index(cancel_line) < definitions.index(hold_line) else cancel_line
    paused_definitions = definitions.replace(pause_before, '''
case = Path(os.environ["CASE_DIR"])
(case / "runner-ready").touch()
while not (case / "release-runner").exists():
    time.sleep(0.01)
''' + pause_before, 1)
    # Let the real, short hook finish before the runner's next cancellation
    # poll, making an unsafe spawn observable even on a heavily loaded host.
    spawn_line = next(line for line in definitions.splitlines() if 'child = subprocess.Popen(' in line)
    paused_definitions = paused_definitions.replace(spawn_line, spawn_line + '\n    child.wait(timeout=5)', 1)
    double_fixture = r'''
bureau_untrusted_env() {
  [ "$1" != --check ] || return 0
  local code=0
  /usr/bin/env "$@" || code=$?
  echo ENV_DONE >> events
  return "$code"
}
drain_barrier() {
  [ -n "${POST_IMPLEMENT_CANCEL_FILE:-}" ] && [ -e "$POST_IMPLEMENT_CANCEL_FILE" ] || return 0
  [ ! -e "$CASE_DIR/drain-ready" ] || return 0
  echo CANCEL >> events
  echo WAIT_BEGIN >> events
  : > "$CASE_DIR/drain-ready"
  while [ ! -e "$CASE_DIR/second-term-sent" ]; do command sleep 0.01; done
  echo DRAIN_ACK >> events
}
wait() {
  drain_barrier
  local code=0
  builtin wait "$@" || code=$?
  echo JOIN >> events
  return "$code"
}
jobs() {
  drain_barrier
  builtin jobs "$@"
}
inject_term() {
  if [ "$BASH_COMMAND" = 'POST_IMPLEMENT_RUNNER_PID=$!' ]; then
    trap - DEBUG
    echo WINDOW >> events
    while [ ! -e "$CASE_DIR/runner-ready" ]; do command sleep 0.01; done
    kill -TERM "$$"
  fi
}
set -T
trap inject_term DEBUG
run_post_implement_command
'''
    case = root / 'double-term'
    case.mkdir()
    script = fixture + paused_definitions + '\n' + exit_trap + cleanup_trap + double_fixture
    proc = subprocess.Popen(['/bin/bash', '-c', script, 'fixture', str(case), str(scripts)],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        wait_for(lambda: (case / 'drain-ready').exists(), 'EXIT never began draining')
        holds_at_pause = list((case / 'common/bureau/process-holds').glob('*.json'))
        os.kill(proc.pid, signal.SIGTERM)
        (case / 'second-term-sent').touch()
        wait_for(lambda: (case / 'events').exists() and 'DRAIN_ACK' in (case / 'events').read_text()
                 or proc.poll() is not None, 'second TERM was never handled')
        exited_before_release = proc.poll() is not None
        if exited_before_release:
            with (case / 'events').open('a') as stream:
                stream.write('STAGE_DEAD\n')
        check(bool(holds_at_pause), 'runner checked cancellation before acquiring its hold', [])
        check(not exited_before_release or bool(list((case / 'common/bureau/process-holds').glob('*.json'))),
              'second TERM aborted the join without a surviving hold', [])
    finally:
        (case / 'second-term-sent').touch()
        (case / 'runner-ready').touch()
        (case / 'release-runner').touch()
        # fd 9 keeps capture open until the runner finishes even if Bash died.
        out, err = proc.communicate(timeout=20)
    events = (case / 'events').read_text().splitlines()
    check(proc.returncode == 143, 'double TERM must retain exit 143', [proc.returncode, out, err])
    check('HOOK_START' not in events, 'hook started after cancellation/stage exit', events)
    check(all(event in events for event in ('DRAIN_ACK', 'ENV_DONE', 'JOIN', 'CHECKPOINT', 'PUSH', 'STAGE_EXIT')),
          'double TERM interrupted EXIT drain', events)
    for event in ('CHECKPOINT', 'PUSH', 'STAGE_EXIT'):
        if event in events:
            check('JOIN' in events and events.index('JOIN') < events.index(event),
                  event + ' preceded the runner join', events)
    check('STATE:::' in events and not list(case.glob('*.cancel')), 'double TERM left cancellation state', events)
    check(not list((case / 'common/bureau/process-holds').glob('*.json')),
          'confirmed pre-spawn cancellation leaked its hold', events)
    print('double TERM: exit=%s events=%s' % (proc.returncode, ','.join(events)))

    # A wrapper stuck before Python already has its pre-fork hold. Advance Bash's
    # clock on the first drain poll to exercise the real drain deadline
    # without waiting fifteen seconds, then keep the wrapper paused until the
    # stage exits. Its eventual cancellation must not remove the original hold.
    for original_code, expected in ((0, 25), (124, 124), (130, 130), (143, 143)):
        case = root / ('drain-timeout-' + str(original_code))
        case.mkdir()
        timeout_fixture = r'''
bureau_outcome_signal() { echo "OUTCOME:$1" >> events; }
sleep() {
  if [ -n "${POST_IMPLEMENT_CANCEL_FILE:-}" ] && [ -e "$POST_IMPLEMENT_CANCEL_FILE" ]; then
    SECONDS=$((SECONDS + 20))
  else
    command sleep "$@"
  fi
}
inject_exit() {
  if [ "$BASH_COMMAND" = 'POST_IMPLEMENT_RUNNER_PID=$!' ]; then
    trap - DEBUG
    while [ ! -e "$CASE_DIR/env-ready" ]; do sleep 0.01; done
    exit "$ORIGINAL_CODE"
  fi
}
set -T
trap inject_exit DEBUG
run_post_implement_command
'''
        script = (fixture + definitions + '\n' + exit_trap + cleanup_trap
                  + '\nORIGINAL_CODE=' + str(original_code) + '\n' + timeout_fixture)
        proc = subprocess.Popen(['/bin/bash', '-c', script, 'fixture', str(case), str(scripts)],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                check(False, 'drain deadline did not bound the stage', [original_code])
            check(proc.returncode == expected, 'uncertain drain changed its exit code',
                  [original_code, proc.returncode])
            holds = list((case / 'common/bureau/process-holds').glob('*.json'))
            check(bool(holds) and bool(list(case.glob('*.cancel'))),
                  'uncertain drain lost its hold or cancellation request', [original_code])
        finally:
            (case / 'release-env').touch()
            out, err = proc.communicate(timeout=20)
        events = (case / 'events').read_text().splitlines()
        check('OUTCOME:human_hold' in events and 'ENV_DONE' in events,
              'uncertain drain failed to report human_hold or finish its wrapper', [events, out, err])
        check(all(event not in events for event in ('HOOK_START', 'JOIN', 'CHECKPOINT', 'PUSH')),
              'uncertain drain published or claimed a completed join', events)
        check(bool(list((case / 'common/bureau/process-holds').glob('*.json'))),
              'late runner completion removed the unfinished drain hold', events)
        check(sorted(holds) == sorted((case / 'common/bureau/process-holds').glob('*.json')),
              'drain deadline replaced the original hold', events)
        print('drain deadline: original=%s exit=%s hold=%s' % (original_code, proc.returncode, bool(holds)))

    # No launch, including a stale $! for an unrelated job still running. EXIT
    # must return before that job is released, rather than accidentally join it.
    for name, body in (
        ('never-launched', '_stop_post_implement_runner\n'),
        ('unset-traps', '_stop_post_implement_runner\n'),
        ('ignored-traps', '_stop_post_implement_runner\n'),
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
        trap_setup = r'''
trap ':' HUP
trap '' INT
trap 'exit 143' TERM
'''
        if name == 'unset-traps':
            trap_setup = 'trap - HUP INT TERM\n'
        elif name == 'ignored-traps':
            trap_setup = "trap '' HUP INT TERM\n"
        script = fixture + definitions + trap_setup + r'''
saved=$(trap -p HUP INT TERM)
POST_IMPLEMENT_SIGNAL_TRAPS=$saved
''' + body + r'''
[ "$saved" = "$(trap -p HUP INT TERM)" ] || exit 1
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

    # A clean hook and a failed initial hold write use the production launch
    # and join paths. An unusable hold directory keeps the existing exit 25,
    # and cannot even reach environment setup or fork a runner.
    for name, fail_hold in (('success', False), ('hold-write-failed', True)):
        case = root / name
        case.mkdir()
        if fail_hold:
            (case / 'common/bureau').mkdir(parents=True)
            (case / 'common/bureau/process-holds').touch()
        direct_env = r'''
bureau_untrusted_env() {
  [ "$1" != --check ] || return 0
  local code=0
  /usr/bin/env "$@" || code=$?
  echo ENV_DONE >> events
  return "$code"
}
'''
        fail_creation = checkpoint + '\nbureau_outcome_signal() { echo "OUTCOME:$1" >> events; }\n'
        script = (fixture + definitions + '\n' + exit_trap + cleanup_trap + direct_env
                  + (fail_creation if fail_hold else '')
                  + '\nrun_post_implement_command\n[ "$POST_IMPLEMENT_FAILED" = 0 ] || exit 14\n')
        result = subprocess.run(['/bin/bash', '-c', script, 'fixture', str(case), str(scripts)],
                                capture_output=True, text=True, timeout=10)
        events = (case / 'events').read_text().splitlines()
        check(result.returncode == (25 if fail_hold else 0), name + ' exit',
              [result.returncode, result.stdout, result.stderr])
        check(not list((case / 'common/bureau/process-holds').glob('*.json')),
              name + ' left a hold', events)
        if fail_hold:
            check('OUTCOME:human_hold' in events and all(event not in events for event in ('HOOK_START', 'ENV_DONE', 'JOIN', 'PUSH')),
                  'failed hold creation launched a hook or environment wrapper', events)
            check('process hold creation failed' in result.stderr, 'failed hold write was not reported', [result.stderr])
        else:
            check('HOOK_START' in events and 'JOIN' in events and 'CHECKPOINT' in events,
                  'success path did not run, join and checkpoint', events)
            check('STALE_HOLD' not in events, 'success checkpoint saw a stale hold', events)
        print('%s: exit=%s events=%s' % (name, result.returncode, ','.join(events)))

if failures:
    for failure in failures:
        print('FAIL ' + failure, file=sys.stderr)
    sys.exit(1)
print('OK test_post_implement_launch_cancel (Bash launch/EXIT, early double TERM, bounded drain, trap restoration and hold lifecycle)')
PY
