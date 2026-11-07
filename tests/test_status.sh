#!/bin/bash
# Exercise the legacy dashboard reader without starting its live refresh loop.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
python3 - "$ROOT/templates/scripts/bureau-status.sh" <<'PY'
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile

text = Path(sys.argv[1]).read_text()
reader = re.search(r'^      last=.+$', text, re.M).group().strip()
serial = re.sub(r'tail -n [0-9]+ "\$log" \| ', '', reader)
serial = serial.replace("grep -v '^---$'", "grep -v '^---$' \"$log\"")
assert reader != serial, 'dashboard tail bound missing'
functions = '\n'.join(re.search(r'^' + name + r'\(\) \{\n.*?^\}', text, re.M | re.S).group()
                      for name in ('parse_status', 'parse_issue'))
output = '\nprintf "%s\\0%s\\0%s" "$last" "$(parse_status "$last")" "$(parse_issue "$last")"'

with tempfile.TemporaryDirectory(prefix='bureau-status-test-') as scratch:
    root = Path(scratch); log = root / 'queue.log'; (root / 'bin').mkdir()
    calls = root / 'tail.calls'
    shim = root / 'bin/tail'
    shim.write_text('#!/bin/bash\nprintf "%s\\n" "$*" >> "$TAIL_CALLS"\nexec "$REAL_TAIL" "$@"\n')
    shim.chmod(0o755)
    env = dict(os.environ, PATH=str(root / 'bin') + os.pathsep + os.environ['PATH'],
               REAL_TAIL=shutil.which('tail'), TAIL_CALLS=str(calls))

    def run(code):
        calls.write_text('')
        result = subprocess.run(['/bin/bash', '-c', 'set -euo pipefail\nlog=$1\n' + functions + '\n' + code + output,
                                 'probe', str(log)], cwd=root, env=env, capture_output=True, timeout=10)
        assert result.returncode == 0 and not result.stderr, result
        return result.stdout, calls.read_text().splitlines()

    for content in ('', '\n---\n', 'Queue Worker Started\n', 'queue empty\n---\n\n',
                    'Next check\n', 'done.\n', 'error (exit 1)\n', 'working...\n',
                    'old activity\n' * 10000 + 'queue empty\n---\n\n',
                    'quoted "value" $(touch executed) \\ text\n---\n'):
        log.write_text(content)
        current, used = run(reader); previous, control = run(serial)
        assert current == previous, (content[-100:], current, previous)
        assert sorted(used) == sorted(['-n 200 ' + str(log), '-1']), used
        assert len(control) == 1, 'negative control still has a bounded input tail'
        assert not (root / 'executed').exists()
    log.write_text('old activity\n' + '\n---\n' * 200)
    current, _ = run(reader); previous, _ = run(serial)
    assert current == b'\0---\0' and previous.startswith(b'old activity\0'), (current, previous)
    print('PASS dashboard fixture output is byte-identical; input is bounded to 200 lines, with a negative control')
PY
