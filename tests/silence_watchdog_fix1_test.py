"""Review regressions: real shell exec, async Bash, inherited pipes and withdrawal."""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
import unittest
from unittest.mock import patch

import silence_watchdog_test as watchdog
from ticket_kill_switch_test import WithdrawalFixture, STATES


PROVIDER = r'''#!/usr/bin/env python3
import json, os, pathlib, subprocess, sys, time
root=pathlib.Path(os.environ['SILENCE_ROOT']); args=sys.argv[1:]
if args[0] in ('auth','login'): print('{"loggedIn":true}'); sys.exit(0)
if args[0]=='--help': print('--session-id --output-format --include-partial-messages'); sys.exit(0)
if args[0]=='--version': print('fixture'); sys.exit(0)
sys.stdin.read()
def emit(event): print(json.dumps(event),flush=True)
def message(kind, blocks): emit(dict(type=kind,message=dict(content=blocks)))
def tool(key, name, inputs): message('assistant',[dict(type='tool_use',id=key,name=name,input=inputs)])
with (root/'pids').open('a') as f: f.write(str(os.getpid())+'\n')
mode=os.environ['SILENCE_MODE']; name=pathlib.Path(sys.argv[0]).name
if mode=='inherited-stdout':
    child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)'])
    with (root/'pids').open('a') as f: f.write(str(child.pid)+'\n')
else:
    command=os.environ.get('FIX1_COMMAND','sleep 0.8')
    background=mode.startswith('background')
    if name=='codex': emit(dict(type='item.started',item=dict(id='bash',type='command_execution',command=command)))
    else: tool('bash','Bash',dict(command=command,run_in_background=background))
    child=subprocess.Popen([os.environ.get('FIX1_SHELL','/bin/bash'),'-lc',command])
    with (root/'pids').open('a') as f: f.write(str(child.pid)+'\n')
    if background:
        message('user',[dict(type='tool_result',tool_use_id='bash',content='Command running in background with ID: task-1')])
        tool('await','TaskOutput',dict(task_id='task-1',block=True,timeout=3000))
    child.wait()
    (root/'child-finished').touch()
    if mode=='background-exit': time.sleep(3)
    if background: message('user',[dict(type='tool_result',tool_use_id='await',content='Task task-1 completed')])
result=dict(status='COMPLETE',value='finished')
if name=='codex':
    pathlib.Path(args[args.index('-o')+1]).write_text(json.dumps(result))
    emit(dict(type='turn.completed',usage=dict(input_tokens=12,output_tokens=3)))
else: emit(dict(type='result',result=json.dumps(result),structured_output=result))
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

    def test_shell_exec_keeps_child_allowance(self):
        for runner in ('claude', 'codex'):
            for shell in ('/bin/zsh', '/bin/bash'):
                if not Path(shell).exists(): continue
                for command in ('CI=1 sleep 0.8', 'env CI=1 sleep 0.8', 'cd / && sleep 0.8'):
                    with self.subTest(runner=runner, shell=shell, command=command):
                        self.configure(runner, silence=.25, child=1.5, wall=3)
                        result, _, _ = self.run_provider('shell', code=None, FIX1_SHELL=shell, FIX1_COMMAND=command)
                        self.assertEqual(0, result.returncode, 'shell exec lost its tool allowance: '+result.stderr)
                        self.assertTrue((self.root/'child-finished').exists())
                        (self.root/'child-finished').unlink()

    def test_background_taskoutput_keeps_child_allowance(self):
        self.configure('claude', silence=.25, child=1.5, wall=3)
        result, _, _ = self.run_provider('background', code=None)
        self.assertEqual(0, result.returncode, 'background Bash lost its tool allowance: '+result.stderr)
        self.assertTrue((self.root/'child-finished').exists())

    def test_background_exit_removes_allowance(self):
        self.configure('claude', silence=.25, child=1.5, wall=3)
        result, meta, _ = self.run_provider('background-exit', code=None, FIX1_COMMAND='sleep 0.6')
        self.assertEqual(25,result.returncode,result.stderr)
        self.assertTrue((self.root/'child-finished').exists(),'background child was stopped before finishing')
        self.assertEqual('silence',meta['timeout_kind'])
        self.assertLess(meta['duration_seconds'],1.3,'finished background child kept its allowance')

    def test_watchdog_off_inherited_stdout_preserves_success(self):
        self.configure('codex', silence=0)
        # Watchdog-off handling deliberately observes rather than owns this child.
        # Clean it here even when the baseline returns the erroneous 22.
        try:
            result, meta, _ = self.run_provider('inherited-stdout', code=None)
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


class ProcessIdentity(unittest.TestCase):
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
