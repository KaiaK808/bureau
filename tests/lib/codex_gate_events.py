"""Fake CLI events with real Git boundaries; no provider/network calls."""
import json
import os
from pathlib import Path
import shlex
import subprocess
import time
import uuid


def emit(command=None, code=None, mode=None):
    mode = mode or os.environ.get('FAKE_GATE_MODE', 'abort')
    if command is None: command = os.environ.get('FAKE_GATE_COMMAND') or json.loads(Path('.bureau.json').read_text())['repo']['test_command']
    if mode == 'unknown': command = 'echo unknown gate'
    code = code if code is not None else (137 if mode == 'kill' else 124 if mode in ('abort', 'stale', 'missing_cwd', 'unknown') else 1)
    argv = ['/bin/bash', '-lc', command]
    if mode == 'agent_bash': argv[0] = str(Path.cwd() / 'agent/bash')
    if mode == 'agent_symlink_bash': argv[0] = str(Path.cwd() / 'agent/symlink-bash')
    if mode == 'relative_bash': argv[0] = 'bash'
    if mode == 'env_bash': argv = ['/usr/bin/env', 'PATH=/usr/bin:/bin', 'bash', '-c', command]
    if mode == 'env_agent_bash': argv = ['/usr/bin/env', 'bash', '-c', command]
    thread = str(uuid.uuid4())
    home = Path(os.environ['CODEX_HOME'])
    directory = home / 'sessions/2026/10/23'; directory.mkdir(parents=True, exist_ok=True)
    begin = dict(type='exec_command_begin', call_id='call_gate', command=argv, cwd=str(Path.cwd()))
    output = os.environ.get('GATE_OUTPUT', 'machine gate timeout' if code == 124 else 'machine gate red')
    end = dict(type='exec_command_end', call_id='call_gate', command=argv, exit_code=code, aggregated_output=output)
    events = [begin, end]
    if mode == 'missing_cwd': events = [end]
    elif mode == 'borrowed_cwd': events[0] = {**begin, 'call_id':'unrelated_call'}
    elif mode == 'duplicate_begin': events = [begin, begin, end]
    elif mode == 'duplicate_end': events = [begin, end, end]
    elif mode == 'reordered': events = [end, begin]
    elif mode == 'ambiguous': events += [{**begin, 'call_id':'other'}, {**end, 'call_id':'other'}]
    elif mode == 'ambiguous_duplicate': events += [{**begin, 'call_id':'other'}, {**begin, 'call_id':'other'}, {**end, 'call_id':'other'}]
    elif mode == 'ambiguous_cwd': events = [{**begin, 'cwd':str(Path.cwd()/'other')}, end, {**begin, 'call_id':'other'}, {**end, 'call_id':'other'}]
    elif mode == 'wrong_end': events[1] = {**end, 'aggregated_output':'different execution'}
    rollout = directory / ('rollout-fixture-' + thread + '.jsonl')
    rollout.write_text(''.join(json.dumps(dict(type='event_msg', payload=e))+'\n' for e in events))
    print(json.dumps(dict(type='thread.started', thread_id=thread)), flush=True)
    item = dict(id='item_gate', type='command_execution', command=shlex.join(argv),
                aggregated_output='', exit_code=None, status='in_progress')
    print(json.dumps(dict(type='item.started', item=item)), flush=True)
    time.sleep(.15)
    item.update(exit_code=code, status='failed', aggregated_output=output)
    print(json.dumps(dict(type='item.completed', item=item)), flush=True)
    time.sleep(.15)
    if mode == 'stale':
        subprocess.run(['git', '-c', 'user.name=Test', '-c', 'user.email=test@example.invalid',
                        'commit', '-q', '--allow-empty', '-m', 'new HEAD after execution'], check=True)
