"""Run selected shell tests with process-group deadlines and optional timings."""
import ctypes
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import tempfile
import time

# Also support the runner's isolated Python invocation.
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
    # Popen owns the leader; only reap adopted group members after it exits.
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
            if process_group_running(child.pid):
                raise
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


def run_file(name, timeout, log):
    started = time.monotonic()
    outcome = 'FAIL'
    child = subprocess.Popen(['bash', name], stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
    try:
        try:
            child.wait(timeout=max(0, started + timeout - time.monotonic()))
            outcome = 'PASS' if child.returncode == 0 else 'FAIL'
        except subprocess.TimeoutExpired:
            outcome = 'TIMEOUT'
        finally:
            # Successful tests must not leave background children writing either.
            stop_group(child)
    except (OSError, RuntimeError, subprocess.TimeoutExpired) as error:
        print(f'Runner cleanup failed: {error}', file=log)
        if outcome != 'TIMEOUT':
            outcome = 'FAIL'
    return outcome, time.monotonic() - started


def main(names):
    override = timeout_override()
    weights = read_weights(Path(__file__).with_name('shard-weights.tsv'))
    os_name = runner_os()
    if sys.platform.startswith('linux'):
        # Reap orphaned group members instead of relying on the CI container's init.
        if ctypes.CDLL(None, use_errno=True).prctl(36, 1, 0, 0, 0) != 0:
            raise OSError('cannot adopt test descendants')
    timings = os.environ.get('BUREAU_TEST_TIMINGS')
    failed = []
    passed = 0
    with (open(timings, 'w') if timings is not None else open(os.devnull, 'w')) as records:
        records.write(TIMING_HEADER)
        records.flush()
        for name in names:
            if not Path(name).is_file():
                continue
            timeout = override if override is not None else max(600, 3 * expected_seconds(name, weights, os_name))
            with tempfile.TemporaryFile(mode='w+', errors='replace') as log:
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
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, interrupted)
    try:
        sys.exit(main(sys.argv[1:]))
    except KeyboardInterrupt:
        sys.exit(130)
    except (OSError, ValueError) as error:
        sys.exit(str(error))
