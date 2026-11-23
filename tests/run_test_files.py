"""Run selected shell tests with process-group deadlines and optional timings."""
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import tempfile
import time

# Also support the runner's isolated Python invocation.
sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent))
from process_helpers import process_group_running


TIMING_HEADER = 'test_file\telapsed_seconds\toutcome\tos\n'


def runner_os():
    return 'macOS' if os.environ.get('RUNNER_OS', sys.platform) in ('macOS', 'darwin') else 'Linux'


def read_weights(path):
    weights = {}
    for line in path.read_text().splitlines():
        if not line.strip() or line.startswith('#'):
            continue
        name, ubuntu, macos = line.split()
        weights[name] = tuple(None if value == '-' else int(value) for value in (ubuntu, macos))
    return weights


def expected_seconds(name, weights, os_name):
    ubuntu, macos = weights.get(name, (None, None))
    # Unmeasured files use the other OS, or 60s Linux / 120s macOS.
    ubuntu = ubuntu if ubuntu is not None else ((macos + 1) // 2 if macos is not None else 60)
    return (macos if macos is not None else 2 * ubuntu) if os_name == 'macOS' else ubuntu


def timeout_override():
    value = os.environ.get('BUREAU_TEST_FILE_TIMEOUT')
    if value is not None and re.fullmatch(r'[0-9]+', value) is None:
        raise ValueError('BUREAU_TEST_FILE_TIMEOUT must be a positive integer in seconds')
    if value is not None and int(value) <= 0:
        raise ValueError('BUREAU_TEST_FILE_TIMEOUT must be a positive integer in seconds')
    return int(value) if value is not None else None


def reap_group(child):
    # Popen owns the leader; after it exits, reap any other direct child left in its group.
    if child.poll() is not None:
        while True:
            try:
                pid, _ = os.waitpid(-child.pid, os.WNOHANG)
            except ChildProcessError:
                break
            if not pid:
                break


def stop_group(child):
    for signum, grace in ((signal.SIGTERM, 1), (signal.SIGKILL, 3)):
        reap_group(child)
        try:
            os.killpg(child.pid, signum)
        except ProcessLookupError:
            child.wait(timeout=grace)
            reap_group(child)
            return
        except PermissionError:
            pass
        deadline = time.monotonic() + grace
        while True:
            reap_group(child)
            if not process_group_running(child.pid):
                child.wait(timeout=max(.01, deadline - time.monotonic()))
                reap_group(child)
                return
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(.05, remaining))
    raise RuntimeError(f'process group {child.pid} survived cleanup')


def reset_test_signals():
    # Background shells and nohup can leave these ignored across exec.
    for signum in (signal.SIGINT, signal.SIGQUIT, signal.SIGHUP):
        signal.signal(signum, signal.SIG_DFL)
    if 'BUREAU_TEST_RESULTS' in os.environ:
        signal.pthread_sigmask(signal.SIG_UNBLOCK, {signal.SIGINT, signal.SIGTERM, signal.SIGHUP})


def run_file(name, timeout, log, evidence=None):
    started = time.monotonic()
    outcome = 'FAIL'
    if evidence is not None:
        evidence['cleanup_ok'] = True
    # Keep cancellation pending until the child is registered inside its cleanup
    # scope. The child resets this mask before exec; legacy calls stay unchanged.
    # A nested runner inside a test must never write into this run's records.
    child_env = {k: v for k, v in os.environ.items() if k != 'BUREAU_TEST_RESULTS'}
    child_env['PYTHONDONTWRITEBYTECODE'] = '1'
    mask = signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGINT, signal.SIGTERM, signal.SIGHUP}) if evidence is not None else None
    try:
        child = subprocess.Popen(['bash', name], stdout=log, stderr=subprocess.STDOUT, start_new_session=True,
                                 preexec_fn=reset_test_signals,
                                 env=child_env)
    except BaseException:
        if mask is not None:
            signal.pthread_sigmask(signal.SIG_SETMASK, mask)
        raise
    try:
        try:
            if mask is not None:
                signal.pthread_sigmask(signal.SIG_SETMASK, mask)
            child.wait(timeout=max(0, started + timeout - time.monotonic()))
            outcome = 'PASS' if child.returncode == 0 else 'FAIL'
        except subprocess.TimeoutExpired:
            outcome = 'TIMEOUT'
        finally:
            # Successful tests must not leave background children writing either.
            stop_group(child)
    except (OSError, RuntimeError, subprocess.TimeoutExpired) as error:
        if evidence is not None:
            evidence['cleanup_ok'] = False
        print(f'Runner cleanup failed: {error}', file=log)
        if outcome != 'TIMEOUT':
            outcome = 'FAIL'
    return outcome, time.monotonic() - started


def append_result(name, outcome, elapsed, timeout, evidence):
    path = os.environ.get('BUREAU_TEST_RESULTS')
    if path is not None:
        with open(path, 'a') as output:
            output.write(json.dumps(dict(file=name, outcome=outcome, elapsed_seconds=elapsed,
                                         deadline=timeout, **evidence)) + '\n')
            output.flush()


def main(names):
    override = timeout_override()
    weights = read_weights(Path(__file__).with_name('shard-weights.tsv'))
    os_name = runner_os()
    # No Linux subreaper: a terminated orphan adopted here stays a zombie until its
    # file ends, so a test that kills its own orphan would still see it alive.
    timings = os.environ.get('BUREAU_TEST_TIMINGS')
    failed = []
    passed = 0
    with (open(timings, 'w') if timings is not None else open(os.devnull, 'w')) as records:
        records.write(TIMING_HEADER)
        records.flush()
        for name in names:
            timeout = override if override is not None else max(600, 3 * expected_seconds(name, weights, os_name))
            if not Path(name).is_file():
                if 'BUREAU_TEST_RESULTS' in os.environ:
                    append_result(name, 'MISSING', 0, timeout, dict(cleanup_ok=True))
                    failed.append(name)
                continue
            with tempfile.TemporaryFile(mode='w+', errors='replace') as log:
                if 'BUREAU_TEST_RESULTS' in os.environ:
                    evidence = {}
                    outcome, elapsed = run_file(name, timeout, log, evidence)
                    append_result(name, outcome, elapsed, timeout, evidence)
                else:
                    outcome, elapsed = run_file(name, timeout, log)
                records.write(f'{name}\t{elapsed:.6f}\t{outcome}\t{os_name}\n')
                records.flush()
                if outcome == 'TIMEOUT':
                    print(f'TIMEOUT  {name} (deadline {timeout}s)', flush=True)
                print(f'{"PASS" if outcome == "PASS" else "FAIL"}  {name}', flush=True)
                log.seek(0)
                for line in log:
                    if outcome != 'PASS' or line.startswith('SKIP'):
                        print('      ' + line.rstrip('\n'), flush=True)
            if outcome == 'PASS':
                passed += 1
            else:
                failed.append(name)
    print('\n─────────────────────────────')
    print(f'Results: {passed} passed, {len(failed)} failed')
    if failed:
        print('Failed tests:')
        for name in failed:
            print(f'  - {name}')
    return int(bool(failed))


if __name__ == '__main__':
    def interrupted(signum, frame):
        if 'BUREAU_TEST_RESULTS' in os.environ:
            if getattr(interrupted, 'seen', False):
                return  # Let the first interruption's bounded cleanup finish.
            interrupted.seen = True
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, interrupted)
    if 'BUREAU_TEST_RESULTS' in os.environ:
        signal.signal(signal.SIGINT, interrupted)
        signal.signal(signal.SIGHUP, interrupted)
    try:
        sys.exit(main(sys.argv[1:]))
    except KeyboardInterrupt:
        sys.exit(130)
    except (OSError, ValueError) as error:
        sys.exit(str(error))
