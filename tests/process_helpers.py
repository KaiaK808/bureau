"""Deadline waits and process liveness for fixture assertions."""
import errno
import os
from pathlib import Path
import subprocess
import sys
import time


COMMAND_TIMEOUT_SECONDS = 45
# Full pipelines include setup/re-entry, the judged stage, then routing/cleanup.
# The macOS CI log reached the review's BLOCK/25 cleanup inside the old single
# command budget. Allow one command budget per phase; these are correctness
# checks, not latency checks. A hung pipeline still exhausts this finite budget.
PIPELINE_TIMEOUT_SECONDS = 3 * COMMAND_TIMEOUT_SECONDS
PROCESS_EXIT_TIMEOUT_SECONDS = 5


def process_snapshot(command, *, check=False):
    """Allow a loaded host one retry, with a finite budget for each scan."""
    for attempt in range(2):
        try:
            return subprocess.run(command, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                  check=check, timeout=10)
        except (OSError, subprocess.SubprocessError):
            if attempt:
                raise


def communicate_until(child, deadline):
    """Drain both pipes and reap the leader within the caller's shared budget."""
    return child.communicate(timeout=max(0, deadline - time.monotonic()))


def run_pipeline(command, *, root, cwd, env):
    """Drain output and join fixture descendants before returning to assertions."""
    from mutation_checks import run
    return run(command, root=root, cwd=cwd, env=env, timeout=PIPELINE_TIMEOUT_SECONDS)


def wait_for_processes_exit(pids, deadline):
    """Return survivors after one shared deadline; inspection errors still fail."""
    remaining = set(pids)
    while remaining:
        if time.monotonic() >= deadline:
            return remaining  # Uninspected PIDs are never assumed stopped.
        remaining = running_processes(remaining)
        if not remaining:
            break
        seconds = deadline - time.monotonic()
        if seconds <= 0:
            break
        time.sleep(min(.05, seconds))
    return remaining


def process_running(pid):
    """Count sleeping/stopped processes as running, but never zombies."""
    if sys.platform.startswith('linux'):
        try:
            # comm may itself contain spaces and closing parentheses.
            state = (Path('/proc')/str(pid)/'stat').read_text().rsplit(')', 1)[1].split()[0]
        except (FileNotFoundError, ProcessLookupError):
            return False
        return state != 'Z'
    return pid in running_processes({pid})


def running_processes(pids):
    """Inspect a whole sweep with one bounded ps, rather than one per PID."""
    pids = set(pids)
    if not pids:
        return set()
    if sys.platform.startswith('linux'):
        return {pid for pid in pids if process_running(pid)}
    result = process_snapshot(['ps', '-o', 'pid=,stat=', '-p', ','.join(map(str, sorted(pids)))])
    states = {}
    for line in result.stdout.splitlines():
        fields = line.split()
        if len(fields) != 2 or not fields[0].isdigit() or fields[1] == '?':
            raise RuntimeError(f'ps returned an unreadable process state: {line!r}')
        states[int(fields[0])] = fields[1]
    for pid in pids - states.keys():
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            continue  # Including a PID that vanished while ps was reading.
        if not result.returncode:
            raise RuntimeError(f'ps returned no state for running PID {pid}')
        raise subprocess.CalledProcessError(result.returncode, result.args,
                                            result.stdout, result.stderr)
    if result.returncode and states:
        raise subprocess.CalledProcessError(result.returncode, result.args,
                                            result.stdout, result.stderr)
    return {pid for pid in pids & states.keys() if not states[pid].startswith('Z')}


def process_group_running(group):
    """Check every group member; a zombie-only group has no running writer."""
    try:
        os.killpg(group, 0)
    except ProcessLookupError:
        return False
    except OSError as error:
        if error.errno != errno.EPERM:
            return True  # Existence/state cannot be disproved.
        # macOS reports EPERM for zombie-only groups; inspect their members.
    if sys.platform.startswith('linux'):
        zombie_seen = False
        try:
            for path in Path('/proc').iterdir():
                if not path.name.isdigit():
                    continue
                try:
                    fields = (path/'stat').read_text().rsplit(')', 1)[1].split()
                    member_group = int(fields[2])
                except (OSError, IndexError, ValueError):
                    # hidepid can deny stat for foreign PIDs. Resolve group
                    # membership separately before discarding the entry.
                    try:
                        member_group = os.getpgid(int(path.name))
                    except ProcessLookupError:
                        continue  # The PID itself has vanished.
                    except OSError:
                        return True  # Membership is unknown; fail closed.
                    if member_group == group:
                        return True  # An owned member's state is unreadable.
                    continue
                if member_group == group:
                    if fields[0] != 'Z':
                        return True
                    zombie_seen = True
        except OSError:
            return True  # Cannot inspect the existing group.
        return not zombie_seen  # No visible members cannot prove zombie-only.
    zombie_seen = False
    try:
        result = process_snapshot(['ps', '-axo', 'pid=,pgid=,stat='], check=True)
        for line in result.stdout.splitlines():
            fields = line.split()
            if int(fields[1]) != group:
                continue  # A foreign member's state is irrelevant.
            if not fields[2].startswith('Z'):
                return True
            zombie_seen = True
    except (OSError, subprocess.SubprocessError, IndexError, ValueError):
        return True  # Failed inspection or unknown membership/state.
    return not zombie_seen  # No visible members cannot prove zombie-only.
