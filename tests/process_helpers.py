"""Process liveness for fixture assertions: an unreaped zombie is already gone."""
import os
from pathlib import Path
import subprocess
import sys


def process_running(pid):
    """Count sleeping/stopped processes as running, but never zombies."""
    if sys.platform.startswith('linux'):
        try:
            # comm may itself contain spaces and closing parentheses.
            state = (Path('/proc')/str(pid)/'stat').read_text().rsplit(')', 1)[1].split()[0]
        except (FileNotFoundError, ProcessLookupError):
            return False
        return state != 'Z'
    result = subprocess.run(['ps', '-o', 'stat=', '-p', str(pid)], text=True,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=1)
    state = result.stdout.strip()
    if result.returncode or not state:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False  # Including a PID that vanished while ps was reading.
        if not result.returncode:
            raise RuntimeError(f'ps returned no state for running PID {pid}')
        raise subprocess.CalledProcessError(result.returncode, result.args,
                                            result.stdout, result.stderr)
    return not state.startswith('Z')


def process_group_running(group):
    """Check every group member; a zombie-only group has no running writer."""
    if sys.platform.startswith('linux'):
        for path in Path('/proc').iterdir():
            if not path.name.isdigit():
                continue
            try:
                fields = (path/'stat').read_text().rsplit(')', 1)[1].split()
            except (FileNotFoundError, ProcessLookupError):
                continue
            if int(fields[2]) == group and process_running(int(path.name)):
                return True
        return False
    result = subprocess.run(['ps', '-axo', 'pid=,pgid='], text=True,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True, timeout=1)
    return any(process_running(int(pid)) for pid, pgid in
               (line.split() for line in result.stdout.splitlines()) if int(pgid) == group)
