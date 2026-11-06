"""Mutation controls require a green baseline and an explicit failure signal."""
from contextlib import contextmanager
import ctypes
import math
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import tarfile
import tempfile
import threading
import time


DEFAULT_TIMEOUT = 300
PROC = Path('/proc')


def process_commands():
    result = subprocess.run(['ps', '-axo', 'pid=,command='], text=True,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            check=True, timeout=1)
    return result.stdout


def macos_cwds():
    """Bound cwd discovery even when a descendant's argv contains no path."""
    result = subprocess.run(['lsof', '-nP', '-a', '-d', 'cwd', '-Fpn'], text=True,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            check=True, timeout=1)
    cwds = {}
    pid = None
    for line in result.stdout.splitlines():
        if line.startswith('p'):
            pid = int(line[1:]) if line[1:].isdigit() else None
        elif line.startswith('n') and pid is not None:
            cwds[pid] = line[1:]
    return cwds


def fixture_pids(root, *, commands=None, cwds=None):
    """Find only commands/cwds inside this resolved, randomly named run root."""
    root = Path(root).resolve()
    output = (commands or process_commands)()
    command_path = re.compile(r'(?<![\w./-])' + re.escape(str(root)) + r'''(?=$|[/\s'\"])''')
    pids = set()
    for line in output.splitlines():
        fields = line.strip().split(None, 1)
        if len(fields) == 2 and fields[0].isdigit() and command_path.search(fields[1]):
            pids.add(int(fields[0]))
    if sys.platform.startswith('linux'):
        # A detached executable need not mention the root in argv. procfs also
        # finds it by cwd, even after its intermediate parent has exited.
        for path in PROC.iterdir():
            if not path.name.isdigit():
                continue
            try:
                # procfs retains a deleted cwd with this suffix. It still
                # belongs to the run even when a fixture removed its directory.
                cwd = Path(os.readlink(path/'cwd').removesuffix(' (deleted)'))
                if cwd == root or root in cwd.parents:
                    pids.add(int(path.name))
            except (OSError, RuntimeError):
                pass  # inaccessible process or a raced exit
    elif sys.platform == 'darwin':
        try:
            discovered = (cwds or macos_cwds)()
        except (OSError, subprocess.SubprocessError) as error:
            print(f'fixture cwd discovery unavailable for {root}: {error}; '
                  'falling back to argv matching', file=sys.stderr)
        else:
            for pid, directory in discovered.items():
                cwd = Path(directory)
                if not cwd.is_absolute():
                    continue
                try:
                    cwd = cwd.resolve()
                    if cwd == root or root in cwd.parents:
                        pids.add(pid)
                except (OSError, RuntimeError):
                    pass  # inaccessible directory or a raced exit
    pids.discard(os.getpid())
    return pids


def stop_fixtures(root, child, *, observed=None):
    """Bound both signal escalation and reaping, and report surviving PIDs."""
    seen = set()
    # Keep retired births across timeout cleanup, the observer and final cleanup.
    known, lock = observed if observed is not None else ({}, threading.Lock())

    def reap_seen():
        if sys.platform.startswith('linux'):
            with lock:
                reap_adopted_fixtures(root, child.pid, known)
        else:
            for pid in seen - {child.pid}:
                try:
                    os.waitpid(pid, os.WNOHANG)
                except ChildProcessError:
                    pass  # macOS launchd reaps detached orphans.

    for sig, grace in ((signal.SIGTERM, 1), (signal.SIGKILL, 3)):
        signalled = set()
        deadline = time.monotonic() + grace
        while True:
            try:
                current = fixture_pids(root)
            except (OSError, subprocess.SubprocessError) as error:
                # Even when inspection is unavailable, never leave our direct
                # child running. Do not pretend detached cleanup was verified.
                try:
                    if child.poll() is None:
                        child.kill()
                    child.wait(timeout=3)
                except (OSError, subprocess.TimeoutExpired) as stop_error:
                    raise AssertionError(f'could not reap fixture PID {child.pid}') from stop_error
                raise AssertionError(f'fixture process inspection failed for {root}: {error}') from error
            if child.poll() is None:
                current.add(child.pid)
            seen.update(current)
            reap_seen()
            if not current:
                return
            for pid in sorted(current - signalled, key=lambda pid: pid == child.pid):
                try:
                    os.kill(pid, sig)
                except (ProcessLookupError, PermissionError):
                    pass  # A live denial remains in the final survivor report.
                signalled.add(pid)
            child.poll()  # Popen owns the direct child's wait status.
            reap_seen()
            if time.monotonic() >= deadline:
                break
            time.sleep(.05)
    current = fixture_pids(root)
    if child.poll() is None:
        current.add(child.pid)
    if current:
        raise AssertionError('could not reap fixture PIDs: ' + ', '.join(map(str, sorted(current))))


def output_text(value):
    # TimeoutExpired.output/stderr can be bytes even with text=True.
    return value.decode(errors='replace') if isinstance(value, bytes) else (value or '')


def finite_timeout(timeout):
    if not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
        raise ValueError('timeout must be a positive finite number of seconds')
    return timeout


def linux_process_commands():
    """Discover argv without starting Popen helpers in the reaper thread."""
    lines = []
    for path in PROC.iterdir():
        if not path.name.isdigit():
            continue
        try:
            argv = (path/'cmdline').read_bytes().replace(b'\0', b' ').decode(errors='replace')
            if argv:
                lines.append(f'{path.name} {argv}')
        except OSError:
            pass  # inaccessible process or a raced exit
    return '\n'.join(lines)


def linux_process_identity(pid):
    """Keep birth identity after zombies lose their argv and cwd."""
    try:
        fields = (PROC/str(pid)/'stat').read_text().rsplit(')', 1)[1].split()
        return int(fields[1]), fields[19]  # parent PID, starttime
    except (OSError, IndexError, ValueError):
        return None  # raced exit or inaccessible process


def linux_fixture_identities(root):
    """Bracket each PID's ownership evidence with its birth, in one discovery.

    None rejects a PID that changed birth while its argv/cwd was inspected.
    """
    root = Path(root).resolve()
    command_path = re.compile(r'(?<![\w./-])' + re.escape(str(root)) + r'''(?=$|[/\s'\"])''')
    found = {}
    for path in PROC.iterdir():
        if not path.name.isdigit():
            continue
        pid = int(path.name)
        if pid == os.getpid():
            continue
        before = linux_process_identity(pid)
        if before is None:
            continue
        try:
            argv = (path/'cmdline').read_bytes().replace(b'\0', b' ').decode(errors='replace')
        except OSError:
            argv = ''
        try:
            cwd = Path(os.readlink(path/'cwd').removesuffix(' (deleted)'))
            owned_cwd = cwd == root or root in cwd.parents
        except (OSError, RuntimeError):
            owned_cwd = False
        after = linux_process_identity(pid)
        if owned_cwd or command_path.search(argv):
            found[pid] = after if after is not None and before[1] == after[1] else None
    return found


def reap_adopted_fixtures(root, child_pid, known):
    """Reap only recorded births adopted by us; None permanently retires a PID."""
    known.pop(child_pid, None)  # Popen exclusively owns its child's status.
    for pid, identity in linux_fixture_identities(root).items():
        if pid == child_pid:
            continue
        birth = identity[1] if identity is not None else None
        if pid not in known:
            known[pid] = birth
        elif known[pid] != birth:
            known[pid] = None  # Never replace an observed birth with a new one.
    reap_recorded_fixtures(child_pid, known)


def reap_recorded_fixtures(child_pid, known):
    """The final sweep needs the cache even after argv/cwd have disappeared."""
    for pid, birth in list(known.items()):
        if pid in (child_pid, os.getpid()) or birth is None:
            continue
        identity = linux_process_identity(pid)
        if identity is None or identity[1] != birth:
            known[pid] = None
        elif identity[0] == os.getpid():
            try:
                waited, _ = os.waitpid(pid, os.WNOHANG)
            except ChildProcessError:
                known[pid] = None  # cleanup may have reaped it first
            else:
                if waited:
                    known[pid] = None


@contextmanager
def reaping_fixtures(root, child):
    """Observe Linux fixture births, then join and reap before root removal."""
    if not sys.platform.startswith('linux'):
        yield
        return
    stopped = threading.Event()
    errors = []
    known = {}
    lock = threading.Lock()

    def reap():
        try:
            while not stopped.is_set():
                with lock:
                    reap_adopted_fixtures(root, child.pid, known)
                stopped.wait(.02)
        except Exception as error:
            errors.append(error)

    reaper = threading.Thread(target=reap, name='fixture-orphan-reaper')
    reaper.start()
    try:
        yield known, lock
    finally:
        stopped.set()
        reaper.join()
        reap_recorded_fixtures(child.pid, known)
        if errors:
            raise AssertionError(f'fixture orphan reaping failed for {root}') from errors[0]


@contextmanager
def fixture_root():
    with tempfile.TemporaryDirectory(prefix='bureau-mutation-run-') as directory:
        if sys.platform.startswith('linux'):
            # Adopt detached orphans so the harness can waitpid after killing
            # them. Discovery still depends only on the unique root.
            if ctypes.CDLL(None, use_errno=True).prctl(36, 1, 0, 0, 0) != 0:
                raise OSError('cannot adopt fixture descendants')
        root = Path(directory).resolve()
        fixture_pids(root)  # Fail before spawning anything if ps is unavailable.
        yield root


def run(command, *, env=None, timeout=DEFAULT_TIMEOUT):
    """Bound a test run, pipe draining and cleanup of its fixture root."""
    timeout = finite_timeout(timeout)
    with fixture_root() as root:
        return _run(command, env=env, timeout=timeout, root=root)


def _run(command, *, env, timeout, root):
    environ = {**os.environ, **(env or {})}
    environ['PYTHONDONTWRITEBYTECODE'] = '1'
    environ['TMPDIR'] = str(root)
    child = subprocess.Popen(command, env=environ, cwd=root, text=True, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, start_new_session=True)
    observed = None
    cleaned = False

    def cleanup():
        nonlocal cleaned
        if not cleaned:
            cleaned = True
            try:
                stop_fixtures(root, child, observed=observed)
            finally:
                child.stdout.close()
                child.stderr.close()

    try:
        with reaping_fixtures(root, child) as observed:
            try:
                try:
                    out, err = child.communicate(timeout=timeout)
                    return subprocess.CompletedProcess(command, child.returncode, out, err)
                except subprocess.TimeoutExpired:
                    stop_fixtures(root, child, observed=observed)
                    try:
                        out, err = child.communicate(timeout=1)
                    except subprocess.TimeoutExpired as error:
                        out, err = output_text(error.output), output_text(error.stderr)
                        err += '\nfixture output drain timed out'
                    return subprocess.CompletedProcess(command, 124, out,
                                                       err + '\n' + str(subprocess.TimeoutExpired(command, timeout)))
            finally:
                cleanup()  # Keep cleanup before the observer's join/final reap.
    finally:
        cleanup()  # Also owns the command when observer startup fails.


def snapshot_files(root):
    """Capture the actual private inventory before constructing mutations."""
    return {path: path.read_bytes() for path in Path(root).rglob('*') if path.is_file()}


@contextmanager
def baseline_fixture(root):
    """Validate in place, join/audit workers, then restore the same fixture paths.

    The caller must stop its processes before leaving the block. A failed
    baseline keeps its files available to the caller's usual cleanup/reporting.
    """
    with tempfile.TemporaryDirectory(prefix='bureau-baseline-snapshot-') as directory:
        snapshot = Path(directory).resolve()/'fixture'
        shutil.copytree(root, snapshot)
        yield
        shutil.rmtree(root)
        shutil.copytree(snapshot, root)


@contextmanager
def historical_scripts(source_root, ref):
    """Overlay old scripts on a complete private inventory; baseline it in place."""
    with tempfile.TemporaryDirectory(prefix='bureau-historical-control-') as directory:
        root = Path(directory).resolve()
        scripts = root/'templates/scripts'
        shutil.copytree(source_root/'templates/scripts', scripts)
        originals = snapshot_files(scripts)
        archive = root/'base.tar'
        with archive.open('wb') as output:
            subprocess.run(['git', '-C', str(source_root), 'archive', ref, 'templates/scripts'],
                           stdout=output, check=True, timeout=30)
        with tarfile.open(archive) as files:
            files.extractall(root)
        yield scripts, originals


@contextmanager
def restored_files(originals):
    """Run a baseline at the mutation's paths, then put its mutations back."""
    changed = {Path(path): Path(path).read_bytes() for path in originals}
    try:
        for path, data in originals.items():
            Path(path).write_bytes(data)
        yield
    finally:
        for path, data in changed.items():
            path.write_bytes(data)


def mutation_caught(command, *, env, originals, timeout=DEFAULT_TIMEOUT, expected=None,
                    expected_exit=None, allow_timeout=False):
    """Require declared output/exit; tuple output fragments must all match.

    Only an explicit timeout control accepts 124. Baseline failures are fatal.
    """
    timeout = finite_timeout(timeout)
    fragments = (expected,) if isinstance(expected, str) else expected
    if fragments is not None and (not isinstance(fragments, tuple) or not fragments
                                  or any(not isinstance(item, str) or not item for item in fragments)):
        raise ValueError('expected must contain nonempty output text')
    if expected_exit is not None and (type(expected_exit) is not int or expected_exit == 0):
        raise ValueError('expected_exit must be a nonzero exit code')
    if type(allow_timeout) is not bool:
        raise ValueError('allow_timeout must be a boolean')
    if fragments is None and expected_exit is None and not allow_timeout:
        raise ValueError('declare expected output, expected_exit or allow_timeout')
    with fixture_root() as root:
        with restored_files(originals):
            baseline = _run(command, env=env, timeout=timeout, root=root)
        assert baseline.returncode == 0, 'unmutated baseline failed:\n' + baseline.stdout + baseline.stderr
        result = _run(command, env=env, timeout=timeout, root=root)
        output = result.stdout + result.stderr
        if result.returncode == 124:
            detected = allow_timeout
        else:
            detected = (result.returncode != 0 and (fragments is not None or expected_exit is not None)
                        and (expected_exit is None or result.returncode == expected_exit)
                        and all(fragment in output for fragment in fragments or ()))
        assert detected, (f'mutation was not caught (expected={expected!r}, exit={expected_exit!r}, '
                          f'allow_timeout={allow_timeout}; got exit {result.returncode}):\n' + output)
        return result


@contextmanager
def caught_mutation():
    """Keep setup/baselines outside; yield caught execution errors for reports."""
    caught = []
    try:
        yield caught
    except KeyboardInterrupt:
        raise
    except BaseException as error:
        # unittest distinguishes FAIL (AssertionError) from ERROR (all others).
        # Match that rule, including SystemExit, while allowing user interrupts.
        caught.append(error)
    else:
        raise AssertionError('mutation was not caught')
