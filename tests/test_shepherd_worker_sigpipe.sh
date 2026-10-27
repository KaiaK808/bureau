#!/bin/bash
# A worker-only hangup still alerts once and exits 130 after stdout's reader closes.
set -euo pipefail
source "$(dirname "$0")/lib/pr5-interrupt.sh"
pr5_setup
trap pr5_teardown EXIT
pr5_new_repo

_pr5_env python3 - "$SB" "$REPO" <<'PY'
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

sb, repo = map(Path, sys.argv[1:])
with (sb/'err').open('w') as err:
    run = subprocess.Popen(
        ['bash', 'scripts/shepherd.sh', '--no-tmux', '--worktree',
         '.worktrees/shepherd-EXP-7', 'EXP-7'],
        cwd=repo, stdout=subprocess.PIPE, stderr=err)
    try:
        deadline = time.monotonic() + 30
        while not (sb/'probe-started').exists():
            if run.poll() is not None or time.monotonic() >= deadline:
                raise AssertionError('probe did not start: ' + (sb/'err').read_text())
            time.sleep(.05)
        # The real stage records its ancestry before publishing probe-started.
        # Signal just the worker runtime, leaving the shepherd unsignalled.
        workers = []
        for line in (sb/'run-procs').read_text().splitlines():
            pid, _, command = line.split(None, 2)
            if 'bureau-runtime.py ' in command and str(repo/'scripts/bureau-worker.sh') + ' EXP-7 spec-pipeline.sh' in command:
                workers.append(int(pid))
        assert len(workers) == 1, (sb/'run-procs').read_text()
        run.stdout.close()
        os.kill(workers[0], signal.SIGHUP)
        rc = run.wait(timeout=30)
    finally:
        run.stdout.close()
        if run.poll() is None:
            run.terminate()
            try:
                run.wait(timeout=10)
            except subprocess.TimeoutExpired:
                run.kill()
                run.wait()

alerts = (sb/'alerts.log').read_text()
count = alerts.count('Bureau pipeline alert')
assert rc == 130 and count == 1 and alerts.count('shepherd halt (cancelled-run)') == 1, (
    f'exit={rc}, alerts={count}, wanted exit=130 and exactly one cancelled-run alert\n'
    + alerts + '\n' + (sb/'err').read_text())
print('PASS closed stdout consumer + worker-only SIGHUP: exit 130, exactly one alert')
PY
