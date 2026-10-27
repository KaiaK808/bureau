#!/bin/bash
# Exercise actual concurrent batch lanes with absent and explicit configuration.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PYTHONDONTWRITEBYTECODE=1 python3 - "$ROOT" <<'PY'
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time

scripts = Path(sys.argv[1]) / 'templates/scripts'
with tempfile.TemporaryDirectory(prefix='bureau-1749-orchestrate-') as directory:
    root = Path(directory)
    worker = root / 'worker.py'
    worker.write_text('''import os, sys, time
from pathlib import Path
root = Path(os.environ['ORCHESTRATE_TEST_RUN'])
(root / (sys.argv[-1] + '.pid')).write_text(str(os.getpid()))
while not (root / 'release').exists(): time.sleep(.02)
''')
    stub = root / 'shepherd.sh'
    stub.write_text('#!/bin/bash\nexec python3 -I "$ORCHESTRATE_TEST_WORKER" "$@"\n')
    schedule = root / 'schedule.json'
    schedule.write_text(json.dumps(dict(serialChains=[], parallelSafe=['TST-1', 'TST-2', 'TST-3', 'TST-4'])))
    for label, agents, expected in [('absent', {}, 3), ('configured', {'parallel_lanes': 2}, 2)]:
        run = root / label
        run.mkdir()
        config = run / '.bureau.json'
        config.write_text(json.dumps(dict(agents=agents)))
        env = {k: v for k, v in os.environ.items() if not k.startswith('BUREAU_')}
        env.update(BUREAU_CONFIG=str(config), SHEPHERD_BIN=str(stub), TMPDIR=str(root),
                   ORCHESTRATE_TEST_RUN=str(run), ORCHESTRATE_TEST_WORKER=str(worker))
        command = ['/bin/bash', str(scripts / 'orchestrate.sh'), '--execute', '--schedule', str(schedule)]
        with (run / 'output.log').open('w') as output:
            child = subprocess.Popen(command, env=env, stdout=output, stderr=subprocess.STDOUT, start_new_session=True)
            try:
                deadline = time.monotonic() + 15
                while len(list(run.glob('*.pid'))) < expected and time.monotonic() < deadline:
                    assert child.poll() is None, (run / 'output.log').read_text()
                    time.sleep(.05)
                # Workers stay held while the real scheduler attempts the next lane.
                time.sleep(3)
                pids = [int(file.read_text()) for file in run.glob('*.pid')]
                assert len(pids) == expected, (label, expected, pids, (run / 'output.log').read_text())
                for pid in pids:
                    os.kill(pid, 0)
            finally:
                (run / 'release').touch()
                try:
                    child.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    os.killpg(child.pid, signal.SIGTERM)
                    try:
                        child.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        os.killpg(child.pid, signal.SIGKILL)
                        child.wait(timeout=5)
        assert child.returncode == 0, (run / 'output.log').read_text()
        assert len(list(run.glob('*.pid'))) == 4, 'all four tickets must complete'
        for pid in [child.pid] + [int(file.read_text()) for file in run.glob('*.pid')]:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                continue
            raise AssertionError('owned test process survived: ' + str(pid))
        try:
            os.killpg(child.pid, 0)
        except ProcessLookupError:
            pass
        else:
            raise AssertionError('owned test process group survived: ' + str(child.pid))
        print('PASS actual orchestrate lanes: ' + label + ' = ' + str(expected) + '; all processes joined')
PY
