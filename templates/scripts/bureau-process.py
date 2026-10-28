#!/usr/bin/env python3
"""Owned process-group signalling and conservative, zombie-aware inspection."""
import os
import subprocess


class TerminationUncertain(OSError):
    """A group may still contain a writer; a leader's exit is no proof."""


def inspect_groups(groups):
    """Return live groups from a complete macOS/Linux ps snapshot, or refuse."""
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


def assert_groups_stopped(groups, *, inspect=False):
    """Never signal stored IDs. EPERM and successful probes require inspection."""
    pending = set(groups)
    if not inspect:
        for group in list(pending):
            try: os.killpg(group, 0)
            except ProcessLookupError: pending.remove(group)
            except PermissionError: pass
    if pending and inspect_groups(pending):
        raise TerminationUncertain('Background process group still alive; stop it before continuing')


def signal_group(child, signum):
    """EPERM is a failure unless a complete inspection proves no live member."""
    try: os.killpg(child.pid, signum)
    except ProcessLookupError: return
    except PermissionError:
        # Killing the leader may help, but never proves descendants are gone.
        if child.poll() is None:
            try: child.kill()
            except OSError: pass
        assert_groups_stopped({child.pid}, inspect=True)
