#!/usr/bin/env python3
"""Offline detector double; assert the real invocation boundary."""
import json
import os
from pathlib import Path
import sys
import time

assert sys.argv[1:4] == ['detect', '--json', '--no-config'], sys.argv
assert all(p.startswith('./') and Path(p).is_file() for p in sys.argv[4:]), sys.argv
assert os.environ['DO_NOT_TRACK'] == os.environ['IMPECCABLE_NO_TELEMETRY'] == '1'
assert Path(os.environ['IMPECCABLE_BIN']).resolve() == Path(__file__).resolve()
with open(os.environ['FAKE_DETECTOR_CALLS'], 'a') as out:
    out.write(json.dumps(dict(argv=sys.argv[1:], telemetry=[os.environ['DO_NOT_TRACK'],
                   os.environ['IMPECCABLE_NO_TELEMETRY']], binary=os.environ['IMPECCABLE_BIN'], directory=str(Path.cwd()))) + '\n')
mode = os.environ.get('FAKE_DETECTOR_MODE', 'normal')
if mode == 'timeout':
    if os.environ.get('FAKE_DETECTOR_PID'):
        Path(os.environ['FAKE_DETECTOR_PID']).write_text(str(os.getpid()))
    time.sleep(5)
if mode == 'exit1':
    sys.exit(1)
if mode == 'malformed':
    print('{broken'); sys.exit(0)
if mode == 'bad-shape':
    print('[{"antipattern":"low-contrast"}]'); sys.exit(2)
rows = []
for name in sys.argv[4:]:
    text = Path(name).read_text()
    for marker, antipattern, category in [('linear-gradient', 'ai-color-palette', 'slop'), ('#aaa', 'low-contrast', 'quality')]:
        if marker in text:
            rows.append(dict(antipattern=antipattern, name='Fixture finding', description='Planted UI finding',
                             severity='warning', category=category,
                             file=os.environ.get('FAKE_DETECTOR_FILE', str(Path(name).resolve())), line=0, snippet=marker))
print(json.dumps(rows))
sys.exit(2 if rows else 0)
