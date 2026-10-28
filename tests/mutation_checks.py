"""Mutation controls require a green baseline, then accept failures or errors."""
from contextlib import contextmanager
import importlib.util
import os
from pathlib import Path
import signal
import subprocess
import tempfile
import time


# Use the shipped native inspector, never a mutated private adapter. Its random
# inherited marker and process birth identities cover setsid/double-fork children
# without signalling unrelated processes or relying on a process group alone.
spec = importlib.util.spec_from_file_location(
    'mutation_activity', Path(__file__).resolve().parents[1]/'templates/scripts/bureau-activity.py')
activity = importlib.util.module_from_spec(spec)
spec.loader.exec_module(activity)


def stop_fixtures(owner, child):
    """Bound both signal escalation and reaping, and report surviving PIDs."""
    for sig, grace in ((signal.SIGTERM, 1), (signal.SIGKILL, 3)):
        deadline = time.monotonic() + grace
        while True:
            owner.scan()
            try:
                owner.signal(sig)
            except PermissionError as error:
                raise AssertionError('could not stop fixture PIDs: ' +
                                     ', '.join(map(str, sorted(owner.current)))) from error
            child.poll()  # Popen owns the direct child's wait status.
            for pid in owner.current:
                if pid == child.pid:
                    continue
                try:
                    os.waitpid(pid, os.WNOHANG)
                except ChildProcessError:
                    pass  # macOS launchd reaps detached orphans.
            owner.scan()
            if not owner.current:
                return
            if time.monotonic() >= deadline:
                break
            time.sleep(.05)
    raise AssertionError('could not reap fixture PIDs: ' + ', '.join(map(str, sorted(owner.current))))


def output_text(value):
    # TimeoutExpired.output/stderr can be bytes even with text=True.
    return value.decode(errors='replace') if isinstance(value, bytes) else (value or '')


def run(command, *, env=None, timeout=90):
    """Bound a test run, pipe draining and cleanup of its owned descendants."""
    environ = {**os.environ, **(env or {})}
    environ['PYTHONDONTWRITEBYTECODE'] = '1'
    with tempfile.TemporaryDirectory(prefix='bureau-mutation-run-') as directory:
        environ['TMPDIR'] = str(Path(directory).resolve())
        owner = activity.OwnedProcesses()
        environ[activity.OWNER_ENV] = owner.token
        child = subprocess.Popen(command, env=environ, text=True, stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE, start_new_session=True)
        try:
            owner.attach(child.pid)
            deadline = None if timeout is None else time.monotonic() + timeout
            while True:
                owner.scan()
                remaining = None if deadline is None else max(0, deadline - time.monotonic())
                try:
                    out, err = child.communicate(timeout=.1 if remaining is None else min(.1, remaining))
                    return subprocess.CompletedProcess(command, child.returncode, out, err)
                except subprocess.TimeoutExpired:
                    if deadline is not None and time.monotonic() >= deadline:
                        break
            stop_fixtures(owner, child)
            try:
                out, err = child.communicate(timeout=1)
            except subprocess.TimeoutExpired as error:
                out, err = output_text(error.output), output_text(error.stderr)
                err += '\nfixture output drain timed out'
            return subprocess.CompletedProcess(command, 124, out,
                                               err + '\n' + str(subprocess.TimeoutExpired(command, timeout)))
        finally:
            try:
                stop_fixtures(owner, child)
            finally:
                child.stdout.close()
                child.stderr.close()


def snapshot_files(root):
    """Capture the actual private inventory before constructing mutations."""
    return {path: path.read_bytes() for path in Path(root).rglob('*') if path.is_file()}


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


def mutation_caught(command, *, env, baseline_env=None, baseline_command=None,
                    originals=None, timeout=90, expected=None):
    """Only the private mutation may fail: baseline ERRORs/timeouts are fatal."""
    with restored_files(originals or {}):
        baseline = run(baseline_command or command,
                       env=env if baseline_env is None else baseline_env, timeout=timeout)
    assert baseline.returncode == 0, 'unmutated baseline failed:\n' + baseline.stdout + baseline.stderr
    result = run(command, env=env, timeout=timeout)
    assert result.returncode != 0, ('mutation was not caught' + (f' ({expected})' if expected else '')
                                    + ':\n' + result.stdout + result.stderr)
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
