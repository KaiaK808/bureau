"""Injected activity deadlines, native process inspection, pipes and withdrawal."""
import json
import os
import select
import shlex
import signal
import subprocess
import sys
import time
import unittest
from unittest.mock import Mock, patch

import silence_watchdog_test as watchdog
from ticket_kill_switch_test import WithdrawalFixture, STATES


PROVIDER = r'''#!/usr/bin/env python3
import json, os, pathlib, select, signal, subprocess, sys
root=pathlib.Path(os.environ['SILENCE_ROOT']); args=sys.argv[1:]
if args[0] in ('auth','login'): print('{"loggedIn":true}'); sys.exit(0)
if args[0]=='--help': print('--session-id --output-format --include-partial-messages'); sys.exit(0)
if args[0]=='--version': print('fixture'); sys.exit(0)
sys.stdin.read()
ready, writer = os.pipe()
code = 'import os, signal, sys; os.write(int(sys.argv[1]), b"r"); signal.pause()'
child = subprocess.Popen([sys.executable, '-c', code, str(writer)], pass_fds=(writer,))
os.close(writer)
with (root/'pids').open('a') as f: f.write(str(os.getpid())+'\n'+str(child.pid)+'\n')
try:
    assert select.select([ready], [], [], 15)[0], 'inherited-pipe child never signalled ready'
    assert os.read(ready, 1) == b'r'
except BaseException:
    child.kill(); child.wait(timeout=15); raise
finally:
    os.close(ready)
result=dict(status='COMPLETE',value='finished')
pathlib.Path(args[args.index('-o')+1]).write_text(json.dumps(result))
print(json.dumps(dict(type='turn.completed',usage=dict(input_tokens=12,output_tokens=3))),flush=True)
'''


class ProviderRegressions(unittest.TestCase):
    configure = watchdog.Watchdog.configure
    command = watchdog.Watchdog.command
    run_provider = watchdog.Watchdog.run_provider
    no_survivors = watchdog.Watchdog.no_survivors

    def setUp(self):
        watchdog.Watchdog.setUp(self)
        for name in ('claude', 'codex'):
            (self.bin/name).write_text(PROVIDER)

    def test_watchdog_off_inherited_stdout_preserves_success(self):
        self.configure('codex', silence=0, wall=30)
        # Watchdog-off handling deliberately observes rather than owns this child.
        # Clean it here even when the baseline returns the erroneous 22.
        try:
            result, meta, _ = self.run_provider('inherited-stdout', code=None, outer_timeout=45)
        finally:
            for raw in (self.root/'pids').read_text().splitlines():
                try: os.kill(int(raw), signal.SIGKILL)
                except ProcessLookupError: pass
            deadline=time.monotonic()+3
            while time.monotonic()<deadline:
                pids=[int(raw) for raw in (self.root/'pids').read_text().splitlines()]
                for pid in pids:
                    try: os.waitpid(pid,os.WNOHANG)
                    except ChildProcessError: pass
                rows=watchdog.p.a.ProcessTable().snapshot()
                live=[pid for pid in pids if pid in rows]
                if not live: break
                time.sleep(.02)
        self.assertEqual(0, result.returncode, 'watchdog-off pipe observation discarded success: '+result.stderr)
        self.assertEqual('finished',json.loads(result.stdout)['value'])
        self.assertEqual('complete',meta['outcome'])


class Table:
    def __init__(self):
        self.rows = {100: (1, 'provider', False)}
        self.argv = {100: ['provider']}

    def snapshot(self): return dict(self.rows)
    def args_env(self, pid): return self.argv[pid], []


class AllowanceMatrix(unittest.TestCase):
    def tracker(self, runner):
        self.now = 0
        self.table = Table()
        with patch.object(watchdog.p.a, 'ProcessTable', return_value=self.table), \
                patch.object(watchdog.p.a.ctypes, 'CDLL', return_value=Mock(prctl=Mock(return_value=0))):
            self.owned = watchdog.p.a.OwnedProcesses()
        self.owned.attach(100)
        return watchdog.p.a.Activity(runner, .25, 1.5, clock=lambda: self.now)

    @staticmethod
    def message(activity, kind, blocks):
        activity.line(json.dumps(dict(type=kind, message=dict(content=blocks))))

    def tool(self, activity, command, background=False):
        if activity.runner == 'codex':
            activity.line(json.dumps(dict(type='item.started', item=dict(
                id='bash', type='command_execution', command=command))))
        else:
            self.message(activity, 'assistant', [dict(type='tool_use', id='bash', name='Bash',
                         input=dict(command=command, run_in_background=background))])

    def test_shell_exec_keeps_child_allowance(self):
        for runner in ('claude', 'codex'):
            for shell in ('/bin/zsh', '/bin/bash'):
                for command in ('CI=1 sleep 0.8', 'env CI=1 sleep 0.8', 'cd / && sleep 0.8'):
                    for first_scan in ('shell', 'exec'):
                        with self.subTest(runner=runner, shell=shell, command=command, first_scan=first_scan):
                            activity = self.tracker(runner)
                            self.tool(activity, command)
                            self.table.rows[200] = (100, 'tool', False)
                            self.table.argv[200] = ([shell, '-lc', command] if first_scan == 'shell'
                                                    else ['sleep', '0.8'])
                            self.owned.scan()
                            self.now = .25
                            self.assertFalse(activity.expired(self.owned), 'shell exec lost its tool allowance')
                            self.table.argv[200] = ['sleep', '0.8']; self.owned.scan()
                            self.now = .8
                            self.assertFalse(activity.expired(self.owned), 'shell child was stopped before finishing')
                            self.now = 1.499
                            self.assertFalse(activity.expired(self.owned))
                            self.now = 1.5
                            self.assertTrue(activity.expired(self.owned), 'child allowance has no deadline')

    def background(self):
        activity = self.tracker('claude')
        self.tool(activity, 'sleep 0.8', background=True)
        self.table.rows[200] = (100, 'tool', False); self.table.argv[200] = ['sleep', '0.8']
        self.owned.scan()
        self.message(activity, 'user', [dict(type='tool_result', tool_use_id='bash',
                     content='Command running in background with ID: task-1')])
        self.message(activity, 'assistant', [dict(type='tool_use', id='await', name='TaskOutput',
                     input=dict(task_id='task-1', block=True, timeout=3000))])
        return activity

    def test_background_taskoutput_keeps_child_allowance(self):
        activity = self.background()
        self.now = .8
        self.assertFalse(activity.expired(self.owned), 'background Bash lost its tool allowance')
        self.message(activity, 'user', [dict(type='tool_result', tool_use_id='await', content='Task task-1 completed')])
        self.table.rows.pop(200); self.owned.scan()
        self.assertFalse(activity.expired(self.owned), 'completion did not count as progress')
        self.now = 1.05
        self.assertTrue(activity.expired(self.owned))

    def test_background_exit_removes_allowance(self):
        activity = self.background()
        self.now = .25
        self.assertFalse(activity.expired(self.owned))
        self.table.rows.pop(200); self.owned.scan()
        self.now = .6
        self.assertTrue(activity.expired(self.owned), 'finished background child kept its allowance')
        self.assertNotIn('bash', activity.tools)
        self.assertNotIn('bash', activity.background)

    def test_silence_boundary_and_unmatched_children(self):
        for runner in ('claude', 'codex'):
            with self.subTest(runner=runner):
                activity = self.tracker(runner)
                self.tool(activity, 'sleep 0.8')
                self.table.rows[200] = (100, 'unrelated', False); self.table.argv[200] = ['sleep', '30']
                self.owned.scan()
                self.now = .249
                self.assertFalse(activity.expired(self.owned))
                self.now = .25
                self.assertTrue(activity.expired(self.owned), 'unmatched child extended the silence window')


class ProcessIdentity(unittest.TestCase):
    def test_native_table_observes_ready_child(self):
        owned = watchdog.p.a.OwnedProcesses()
        owned.attach(os.getpid())
        code = "import sys; print('ready', flush=True); sys.stdin.read(1)"
        command = 'CI=1 ' + shlex.join([sys.executable, '-c', code])
        child = subprocess.Popen(['/bin/bash', '-lc', command], stdin=subprocess.PIPE,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            self.assertTrue(select.select([child.stdout], [], [], 15)[0], 'child never signalled ready')
            self.assertEqual('ready\n', child.stdout.readline())
            now = [0]
            activity = watchdog.p.a.Activity('codex', 3, 30, clock=lambda: now[0])
            activity.line(json.dumps(dict(type='item.started', item=dict(
                id='bash', type='command_execution', command=command))))
            owned.scan(); now[0] = 4
            self.assertFalse(activity.expired(owned), 'native process inspection lost the ready child')
            child.communicate('x', timeout=15)
            self.assertEqual(0, child.returncode)
            owned.scan()
            self.assertTrue(activity.expired(owned), 'exited native child kept its allowance')
        finally:
            if child.poll() is None: child.kill()
            child.communicate(timeout=15)

    def test_exec_and_observed_descendants_keep_the_same_tool_identity(self):
        class Table:
            rows={100:(1,'provider',False)}
            argv={100:['provider']}
            def snapshot(self): return dict(self.rows)
            def args_env(self,pid): return self.argv[pid], [owned.marker]
        table=Table()
        with patch.object(watchdog.p.a,'ProcessTable',return_value=table):
            owned=watchdog.p.a.OwnedProcesses()
        owned.attach(100)
        now=[0]
        activity=watchdog.p.a.Activity('codex',3,8,clock=lambda:now[0])
        activity.line(json.dumps(dict(type='item.started',item=dict(id='t',type='command_execution',command='sleep 10'))))
        table.rows[200]=(100,'tool',False); table.argv[200]=['sleep','10']; owned.scan()
        now[0]=4; self.assertFalse(activity.expired(owned))
        # exec changes argv, never birth identity; a grandchild belongs to the
        # same tool even after its observed parent exits and it is reparented.
        table.argv[200]=['exec-replacement']
        owned.scan()
        self.assertFalse(activity.expired(owned),'exec lost the identified tool process')
        table.rows[200]=(100,'tool',True)
        table.rows[201]=(200,'descendant',False); table.argv[201]=['helper']; owned.scan()
        self.assertFalse(activity.expired(owned),'a zombie tool parent lost its live descendant')
        del table.rows[200]; table.rows[201]=(1,'descendant',False); owned.scan()
        self.assertFalse(activity.expired(owned),'observed descendant lost its tool association')
        table.rows[201]=(1,'reused-pid',False); owned.scan()
        self.assertTrue(activity.expired(owned),'a reused PID inherited a finished tool allowance')


REVIEW_PROVIDER = r'''#!/usr/bin/env python3
import json, os, pathlib, signal, sys, time
root=pathlib.Path(os.environ['PR9_FIXTURE']); args=sys.argv[1:]
if args[0]=='auth': print('{"loggedIn":true}'); sys.exit(0)
if args[0]=='--help': print('--session-id --output-format --include-partial-messages'); sys.exit(0)
if args[0]=='--version': print('fixture'); sys.exit(0)
phase=os.environ.get('BUREAU_PROVIDER_PHASE','unknown')
with (root/'pids').open('a') as f: f.write(str(os.getpid())+'\n')
with (root/'events').open('a') as f: f.write(json.dumps(dict(kind='pass',phase=phase))+'\n')
def interrupted(sig, frame):
    (root/('interrupted-'+phase)).touch(); sys.exit(128+sig)
signal.signal(signal.SIGTERM,interrupted)
(root/('started-'+phase)).touch()
deadline=time.monotonic()+5
while len(list(root.glob('started-*')))<3 and time.monotonic()<deadline: time.sleep(.01)
if phase=='correctness': (root/'state').write_text(json.loads((root/'control').read_text())['target'])
else:
    until=time.monotonic()+2
    while time.monotonic()<until:
        print(json.dumps(dict(type='stream_event',event=dict(type='content_block_delta',delta=dict(type='text_delta',text='working')))),flush=True)
        time.sleep(.08)
    common=root/'repo with spaces'/'.git'
    (root/('leases-at-'+phase)).write_text((common/'bureau/leases.json').read_text())
(root/('finished-'+phase)).touch()
result=dict(specialist=phase,counts=dict(critical=0,bug=0,minor=0,skip=0),findings=[],summary='clean')
print(json.dumps(dict(type='result',result=json.dumps(result),structured_output=result)),flush=True)
'''


class ReviewWithdrawal(WithdrawalFixture):
    def setUp(self):
        super().setUp()
        if os.environ.get('BUREAU_TEST_SILENCE_SCRIPTS'):
            import shutil
            shutil.copytree(os.environ['BUREAU_TEST_SILENCE_SCRIPTS'], self.repo/'scripts', dirs_exist_ok=True)
        (self.bin/'claude').write_text(REVIEW_PROVIDER)

    def check_withdrawal(self, silence, key, code):
        self.config['agents']['code_review']=dict(enabled=True,timeout_seconds=4,silence_timeout_seconds=silence)
        self.save_config(); self.state('build_review'); self.control(target=STATES[key])
        result=self.worker('code-review')
        self.assertEqual(code,result.returncode,result.stdout+result.stderr)
        for phase in ('correctness','security','performance'):
            self.assertTrue((self.root/('finished-'+phase)).exists(), 'withdrawal cancelled an in-flight '+phase+' call')
            self.assertFalse((self.root/('interrupted-'+phase)).exists())
        for phase in ('security','performance'):
            self.assertIn('issue:EXP-9',json.loads((self.root/('leases-at-'+phase)).read_text()))
        self.assert_released(); self.assert_only_review_reservation()
        self.assertEqual(['lane-2'],json.loads((self.root/'labels').read_text()))

    def test_done_waits_for_current_calls_with_watchdog_off(self): self.check_withdrawal(0,'done',0)
    def test_inactive_waits_for_current_calls_with_watchdog_off(self): self.check_withdrawal(0,'backlog',25)
    def test_cancelled_ticket_waits_for_current_calls_with_watchdog_off(self): self.check_withdrawal(0,'cancelled',26)
    def test_done_waits_for_current_calls_with_watchdog_on(self): self.check_withdrawal(.4,'done',0)
    def test_inactive_waits_for_current_calls_with_watchdog_on(self): self.check_withdrawal(.4,'backlog',25)
    def test_cancelled_ticket_waits_for_current_calls_with_watchdog_on(self): self.check_withdrawal(.4,'cancelled',26)


if __name__=='__main__': unittest.main(verbosity=2)
