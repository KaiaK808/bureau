#!/usr/bin/env python3
"""Conservative process-group signalling and durable unfinished-process holds."""
import errno
import fcntl
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
import uuid


# macOS can deny even signal 0 for an unreaped zombie-only group. Share one
# two-second monotonic budget per signal failure or exit check, never per retry.
# A successful probe can also mean a signalled writer is still exiting.
GROUP_REPROBE_SECONDS = 2.0


class TerminationUncertain(OSError):
    """A group may still contain a writer; a leader's exit is no proof."""


def uncertain_exit(code):
    """Keep a determined timeout/cancellation while carrying a durable hold."""
    return code if code in (124, 130) else 25


def hold_root(repo, env):
    """Mandatory coordination is independent of optional evidence storage."""
    common = subprocess.check_output(['git', '-C', str(repo), 'rev-parse', '--git-common-dir'],
                                     text=True, env=env).strip()
    return (Path(repo) / common).resolve() / 'bureau'


def _signal_group(group, signum, *, wait_for_exit=False, reap=None):
    """Share bounded probes; only a successful exit-check deadline permits ps."""
    error = None
    try: os.killpg(group, signum)
    except ProcessLookupError: return True
    except OSError as exc:
        if exc.errno != errno.EPERM: raise
        error = exc
    else:
        if not wait_for_exit: return False
    deadline = time.monotonic() + GROUP_REPROBE_SECONDS
    delay = .01
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0: break
        time.sleep(min(delay, remaining))
        # A zombie-only group cannot disappear until its owner reaps it. Do
        # that while probing, rather than delaying the provider wrapper until
        # after its enclosing runtime has returned. Only ESRCH proves absence.
        if reap is not None:
            try: reap()
            except OSError: pass
        if time.monotonic() > deadline: break
        try: os.killpg(group, 0)
        except ProcessLookupError: return True
        except OSError as exc: error = exc
        else:
            # Exit checks can inspect after a successful final probe. A failed
            # termination signal still needs ESRCH to resolve its uncertainty.
            if wait_for_exit: error = None
        delay = min(delay * 2, .1)
    if error is not None: raise error
    return False


def inspect_groups(groups):
    """Return visible live groups; a listing can never override a signal error."""
    try:
        output = subprocess.check_output(['/bin/ps', '-axo', 'pgid=,stat='], text=True,
                                         stderr=subprocess.DEVNULL, timeout=2)
        active = set()
        for line in output.splitlines():
            parts = line.split()
            # ps state suffixes can include <, +, etc.
            if len(parts) != 2 or not parts[0].isdigit() or not parts[1][0].isalpha():
                raise ValueError('incomplete process-state row')
            if not parts[1].startswith('Z'):
                active.add(int(parts[0]))
        return set(groups) & active
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        raise TerminationUncertain('Cannot confirm process groups stopped; process-state inspection unavailable') from exc


def assert_groups_stopped(groups):
    """Wait for asynchronous exit; inspect only after a successful probe deadline."""
    pending = set(groups)
    for group in list(pending):
        try:
            if _signal_group(group, 0, wait_for_exit=True): pending.remove(group)
        except OSError as exc:
            raise TerminationUncertain('Cannot confirm process group stopped: ' + str(exc)) from exc
    if pending and inspect_groups(pending):
        raise TerminationUncertain('Background process group still alive; stop it before continuing')


def signal_group(child, signum):
    """EPERM gets a bounded ESRCH re-probe; other errors remain uncertain."""
    try: _signal_group(child.pid, signum, reap=child.poll)
    except OSError as exc:
        # Killing the leader may help, but never proves descendants are gone.
        try:
            if child.poll() is None: child.kill()
        except OSError: pass
        raise TerminationUncertain('Process group termination uncertain: ' + str(exc)) from exc


class ProcessHold:
    """Write ahead of spawn; unlock on return, remove only after confirmed cleanup."""
    def __init__(self, root, kind, workspace, *, run_id=None, issue=None):
        self.uncertain = False
        directory = Path(root) / 'process-holds'
        directory.mkdir(parents=True, exist_ok=True)
        self.path = directory / (uuid.uuid4().hex + '.json')
        pending = self.path.with_suffix('.pending')
        self.handle = pending.open('x')
        try:
            fcntl.flock(self.handle, fcntl.LOCK_EX)
            os.chmod(pending, 0o600)
            json.dump(dict(reason='human_hold', kind=kind, workspace=str(workspace),
                           run_id=run_id if run_id is not None else os.environ.get('BUREAU_RUN_ID'),
                           issue=issue if issue is not None else os.environ.get('BUREAU_CURRENT_ISSUE')), self.handle)
            self.handle.flush()
            os.fsync(self.handle.fileno())
            pending.replace(self.path)
            self.sync_directory()
        except BaseException:
            self.handle.close()
            pending.unlink(missing_ok=True)
            raise

    def sync_directory(self):
        fd = os.open(self.path.parent, os.O_RDONLY)
        try: os.fsync(fd)
        finally: os.close(fd)

    def signal(self, child, signum):
        try: signal_group(child, signum)
        except OSError:
            self.uncertain = True
            raise

    def cleanup(self, child):
        """Latch cleanup failure even when the leader has already exited."""
        try:
            try: self.signal(child, signal.SIGKILL)
            finally: child.wait(timeout=1)
            assert_groups_stopped({child.pid})
        except (OSError, subprocess.TimeoutExpired) as exc:
            self.uncertain = True
            raise TerminationUncertain('Process cleanup uncertain: ' + str(exc)) from exc

    def close(self, stopped=False):
        try:
            if stopped and not self.uncertain:
                self.path.unlink()
                self.sync_directory()
        finally:
            self.handle.close()


def process_holds(root, run=None, inactive_only=False):
    """Active locks allow parallel dispatch; unfinished unlocked records hold it."""
    held = []
    try:
        with os.scandir(Path(root) / 'process-holds') as entries:
            paths = [Path(entry.path) for entry in entries if entry.name.endswith('.json')]
    except FileNotFoundError: return held
    except OSError: return [Path(root) / 'process-holds']  # Unreadable coordination is a hold.
    for path in paths:
        try:
            with path.open() as handle:
                if inactive_only:
                    try: fcntl.flock(handle, fcntl.LOCK_SH | fcntl.LOCK_NB)
                    except BlockingIOError: continue
                try: record = json.load(handle)
                except ValueError: record = {}  # An unreadable identity holds every run.
                # Unowned dispatcher policy helpers have their own global hold;
                # they do not acquire or retain an unrelated lane's leases.
                if (run is not None and isinstance(record, dict)
                        and record.get('kind') == 'dispatcher_helper' and not record.get('run_id')):
                    continue
                if run is None or not isinstance(record, dict) or not record.get('run_id') or record['run_id'] == run:
                    held.append(path)
        except FileNotFoundError: pass  # A confirmed cleanup removed this record.
        except OSError: held.append(path)
    return held


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description='Refuse cleanup while an unfinished process hold exists.')
    parser.add_argument('--root', required=True, type=Path)
    parser.add_argument('--run')
    parser.add_argument('--exit-code', type=int, default=25)
    args = parser.parse_args()
    if process_holds(args.root, args.run):
        print('bureau: human_hold: unfinished process hold; worktree cleanup refused', file=sys.stderr)
        sys.exit(uncertain_exit(args.exit_code))
