"""Mutation controls require a green baseline, then accept failures or errors."""
from contextlib import contextmanager
import os
from pathlib import Path
import signal
import subprocess
import tempfile


def signal_group(pid, sig):
    try:
        os.killpg(pid, sig)
    except ProcessLookupError:
        pass
    except PermissionError as error:
        raise AssertionError(f'could not stop fixture process group {pid}') from error


def run(command, *, env=None, timeout=90):
    """Bound a test run and stop its process group, including on timeout."""
    environ = {**os.environ, **(env or {})}
    environ['TMPDIR'] = str(Path(tempfile.gettempdir()).resolve())
    environ['PYTHONDONTWRITEBYTECODE'] = '1'
    with subprocess.Popen(command, env=environ, text=True, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, start_new_session=True) as child:
        try:
            out, err = child.communicate(timeout=timeout)
            return subprocess.CompletedProcess(command, child.returncode, out, err)
        except subprocess.TimeoutExpired as error:
            # Let fixture/runtime handlers propagate cancellation to sessions
            # they own and reap their children before escalating to SIGKILL.
            signal_group(child.pid, signal.SIGTERM)
            try:
                out, err = child.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                signal_group(child.pid, signal.SIGKILL)
                out, err = child.communicate(timeout=5)
            return subprocess.CompletedProcess(command, 124, out, err + '\n' + str(error))
        finally:
            signal_group(child.pid, signal.SIGKILL)


def mutation_caught(command, *, env, baseline_env, baseline_command=None, timeout=90, expected=None):
    """Only the private mutation may fail: baseline ERRORs/timeouts are fatal."""
    baseline = run(baseline_command or command, env=baseline_env, timeout=timeout)
    assert baseline.returncode == 0, 'unmutated baseline failed:\n' + baseline.stdout + baseline.stderr
    result = run(command, env=env, timeout=timeout)
    assert result.returncode != 0, ('mutation was not caught' + (f' ({expected})' if expected else '')
                                    + ':\n' + result.stdout + result.stderr)
    return result


@contextmanager
def caught_mutation():
    """For in-process controls; run the unmutated assertion before entering."""
    try:
        yield
    except KeyboardInterrupt:
        raise
    except BaseException:
        # unittest distinguishes FAIL (AssertionError) from ERROR (all others).
        # Match that rule, including SystemExit, while allowing user interrupts.
        pass
    else:
        raise AssertionError('mutation was not caught')
