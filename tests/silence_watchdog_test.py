"""Real local Git, actual provider JSONL parsing, faked CLIs/Linear. No network."""
import importlib.util
import json
import os
from pathlib import Path
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

import git_maintenance
from ticket_kill_switch_test import WithdrawalFixture, STATES

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = Path(os.environ.get('BUREAU_TEST_PROVIDER_SCRIPT', ROOT/'templates/scripts/bureau-provider.py'))
spec = importlib.util.spec_from_file_location('silence_provider', SCRIPT)
p = importlib.util.module_from_spec(spec); spec.loader.exec_module(p)

PROVIDER = r'''#!/usr/bin/env python3
import json, os, pathlib, signal, subprocess, sys, time
root=pathlib.Path(os.environ['SILENCE_ROOT']); args=sys.argv[1:]
if args[0] in ('auth','login'): print('{"loggedIn":true}'); sys.exit(0)
if args[0]=='--help': print('--session-id --output-format --include-partial-messages'); sys.exit(0)
if args[0]=='--version': print('fixture'); sys.exit(0)
name=pathlib.Path(sys.argv[0]).name; mode=os.environ.get('SILENCE_MODE','silent')
sys.stdin.read()
(root/'argv.json').write_text(json.dumps(args))
with (root/'pids').open('a') as f: f.write(str(os.getpid())+'\n')
phase=os.environ.get('BUREAU_PROVIDER_PHASE','implement')
if os.environ.get('PR9_FIXTURE'):
    with (root/'events').open('a') as f: f.write(json.dumps(dict(kind='pass',phase=phase))+'\n')
    control=json.loads((root/'control').read_text())
    if control.get('target'): (root/'state').write_text(control['target'])
    if phase in ('correctness','performance'): mode='active'
def emit(event): print(json.dumps(event),flush=True)
def progress(i):
    if name=='codex': emit(dict(type='item.completed',item=dict(id=str(i),type='agent_message',text='working '+str(i))))
    else: emit(dict(type='stream_event',event=dict(type='content_block_delta',delta=dict(type='text_delta',text='working '+str(i)))))
def tool(command):
    if name=='codex': emit(dict(type='item.started',item=dict(id='tool',type='command_execution',command=command)))
    else: emit({'type':'assistant','message':{'content':[{'type':'tool_use','id':'tool','name':'Bash','input':{'command':command}}]}})
if mode.startswith('child') or mode in ('unmatched','success-child'):
    duration='.55' if mode=='child-exit' else '10'
    command=[sys.executable,str(root/'writer.py'),duration]
    if mode=='child-orphan': command.append('orphan')
    import shlex
    if mode=='child-late':
        tool(shlex.join(command)); time.sleep(.2)
    subprocess.Popen(command,start_new_session=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    if mode!='child-late': tool('sleep 999' if mode=='unmatched' else shlex.join(command))
elif mode=='unfinished': tool('sleep 10')
(root/'ready').touch()
duration=float(os.environ.get('SILENCE_DURATION','3'))
started=time.monotonic(); i=0
while time.monotonic()-started<duration or (mode=='productive' and i<10):
    if mode in ('active','productive'): progress(i)
    elif mode=='noise':
        print('malformed JSON',flush=True); emit(dict(type='ping')); print('stderr chatter',file=sys.stderr,flush=True)
    elif mode=='partial':
        sys.stdout.write('{"type":"item.completed","item":' if i==0 else ' '); sys.stdout.flush()
    elif mode=='duplicate':
        emit(dict(type='item.started',item=dict(id='repeat',type='reasoning',text='unchanged')))
    if mode=='success-child': break
    i+=1; time.sleep(.08)
result=dict(status='COMPLETE',value='finished')
if name=='codex':
    pathlib.Path(args[args.index('-o')+1]).write_text(json.dumps(result))
    emit(dict(type='turn.completed',usage=dict(input_tokens=12,output_tokens=3)))
else:
    envelope=dict(type='result',result=json.dumps(result),structured_output=result,
                  usage=dict(input_tokens=12,output_tokens=3),total_cost_usd=.02)
    emit(envelope)
'''

WRITER = r'''import os, pathlib, signal, sys, time
root=pathlib.Path(os.environ['SILENCE_ROOT'])
if 'orphan' in sys.argv:
    if os.fork(): os._exit(0)
    os.setsid()
    if os.fork(): os._exit(0)
signal.signal(signal.SIGTERM,signal.SIG_IGN)
with (root/'pids').open('a') as f: f.write(str(os.getpid())+'\n')
until=time.monotonic()+float(sys.argv[1])
while time.monotonic()<until:
    (root/'heartbeat').write_text(str(time.monotonic_ns()))
    time.sleep(.02)
'''


class Watchdog(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='bureau-1723-provider-'); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name); self.bin=self.root/'bin'; self.bin.mkdir()
        self.env={**os.environ, **git_maintenance.OFF_ENV, 'PATH':str(self.bin)+':'+os.environ['PATH'],
                  'SILENCE_ROOT':str(self.root),'BUREAU_PROVIDER_LOG_DIR':str(self.root/'logs'),
                  'CLAUDE_CONFIG_DIR':str(self.root/'claude'), 'CODEX_HOME':str(self.root/'codex')}
        for key in list(self.env):
            if key.startswith(('BUREAU_RUNNER_', 'BUREAU_STAGE_TIMEOUT','BUREAU_MODEL_','BUREAU_PROVIDER_PHASE')): self.env.pop(key)
        for name in ('codex','claude'):
            path=self.bin/name; path.write_text(PROVIDER); path.chmod(0o755)
        (self.root/'writer.py').write_text(WRITER); (self.root/'pids').write_text('')
        subprocess.run(['git','init','-q',str(self.root)],check=True,env=self.env)
        subprocess.run(['git','-C',str(self.root),'-c','user.name=Test','-c','user.email=test@bureau',
                        'commit','-qm','fixture','--allow-empty'],check=True,env=self.env)
        self.config=self.root/'config.json'; self.prompt=self.root/'prompt'; self.prompt.write_text('fixture')
        self.schema=self.root/'schema'; self.schema.write_text(json.dumps(dict(type='object',required=['status','value'],
                          properties=dict(status=dict(type='string'),value=dict(type='string')),additionalProperties=False)))
        self.configure()
        self.addCleanup(self.no_survivors)

    def configure(self, runner='codex', silence=.35, child=1.1, wall=3):
        self.config.write_text(json.dumps(dict(version=2,agents=dict(runner=runner,
            implement=dict(timeout_seconds=wall,silence_timeout_seconds=silence,silence_child_timeout_seconds=child)))))

    def command(self):
        return [sys.executable,str(SCRIPT),'--stage','implement','--repo',str(self.root),'--config',str(self.config),
                '--prompt-file',str(self.prompt),'--schema',str(self.schema)]

    def run_provider(self, mode='silent', code=25, **env):
        started=time.monotonic()
        result=subprocess.run(self.command(),capture_output=True,text=True,env={**self.env,'SILENCE_MODE':mode,**env},timeout=8)
        if code is not None: self.assertEqual(code,result.returncode,result.stdout+result.stderr)
        metadata=json.loads(next((self.root/'logs').glob('*/result.json')).read_text())
        return result,metadata,time.monotonic()-started

    def no_survivors(self):
        live=[]
        for raw in set((self.root/'pids').read_text().splitlines()):
            pid=int(raw)
            try: os.kill(pid,0)
            except ProcessLookupError: continue
            live.append(pid)
            try: os.kill(pid,signal.SIGKILL)
            except ProcessLookupError: pass
            try: os.waitpid(pid,0)
            except ChildProcessError: pass
        self.assertEqual([],live,'owned writers survived reporting; fixture stopped them')

    def test_initial_silence(self):
        result,meta,elapsed=self.run_provider(code=None)
        self.assertLess(elapsed,2.3,'silent provider missed early-stop deadline')
        self.assertEqual(25,result.returncode,result.stdout+result.stderr)
        self.assertEqual('silence',meta['timeout_kind']); self.assertEqual('timeout',meta['outcome'])
        receipt=json.loads(Path(meta['receipt']).read_text())
        self.assertEqual('silence',receipt['timeout_kind']); self.assertEqual(25,receipt['normalized_exit'])
        self.assertIsNone(receipt['result_sha256']); self.assertTrue(all(v is None for v in receipt['usage'].values()))

    def test_claude_initial_silence(self):
        self.configure('claude'); self.run_provider()
        args=json.loads((self.root/'argv.json').read_text())
        self.assertEqual('stream-json',args[args.index('--output-format')+1]); self.assertIn('--verbose',args)
        self.assertIn('--include-partial-messages',args)

    def test_productive_codex_and_exact_stream(self):
        result,meta,_=self.run_provider('productive',0,SILENCE_DURATION='1.1')
        self.assertEqual('finished',json.loads(result.stdout)['value']); self.assertEqual(12,meta['usage']['input_tokens'])
        lines=(Path(meta['evidence'])/'stdout.log').read_bytes().splitlines(keepends=True)
        self.assertGreater(len(lines),8)
        self.assertTrue(all(line.endswith(b'\n') for line in lines))
        self.assertEqual('item.completed',json.loads(lines[0])['type'])

    def test_claude_stream_result_usage_and_receipt_recovery(self):
        self.configure('claude')
        self.config.write_text(self.config.read_text().replace('"agents":', '"session":{"cost_tracking":true},"agents":'))
        result,meta,_=self.run_provider('productive',0,SILENCE_DURATION='1')
        self.assertEqual(.02,json.loads(result.stdout)['total_cost_usd'])
        receipt=json.loads(Path(meta['receipt']).read_text())
        self.assertEqual(dict(status='COMPLETE',value='finished'),p.receipt_result(receipt))
        self.assertEqual(3,receipt['usage']['output_tokens'])
        recovered=p.completion_text(0,'missing forwarding',meta,self.root,self.schema)
        self.assertEqual('finished',json.loads(json.loads(recovered)['result'])['value'])

    def test_legacy_claude_recovery_and_disabled_flag(self):
        self.configure('claude',silence=0)
        _,meta,_=self.run_provider('silent',0,SILENCE_DURATION='.1')
        self.assertNotIn('--verbose',json.loads((self.root/'argv.json').read_text()))
        directory=Path(meta['evidence']); (directory/'stdout.log').write_text('{"result":"{\\"status\\":\\"COMPLETE\\",\\"value\\":\\"old\\"}"}')
        self.assertEqual('old',p.receipt_result(dict(provider='claude',provider_logs=str(directory)))['value'])

    def test_partial_json_is_preserved_without_resetting(self):
        _,meta,elapsed=self.run_provider('partial'); self.assertLess(elapsed,2.3)
        self.assertTrue((Path(meta['evidence'])/'stdout.log').read_bytes().startswith(b'{"type":"item.completed","item":'))

    def test_noise_pings_and_stderr_are_not_activity(self): self.run_provider('noise')
    def test_repeated_lifecycle_is_not_activity(self): self.run_provider('duplicate')

    def test_pending_tool_without_child_uses_normal_window(self):
        _,meta,_=self.run_provider('unfinished'); self.assertLess(meta['duration_seconds'],.95)

    def test_unmatched_owned_process_is_not_a_tool_allowance(self):
        self.configure(child=3)
        _,meta,_=self.run_provider('unmatched'); self.assertLess(meta['duration_seconds'],2)

    def test_owned_detached_tool_gets_fixed_extended_window(self):
        _,meta,_=self.run_provider('child'); self.assertGreaterEqual(meta['duration_seconds'],1.1)
        self.assertLess(meta['duration_seconds'],3)

    def test_child_created_after_tool_start_gets_allowance(self):
        _,meta,_=self.run_provider('child-late'); self.assertGreaterEqual(meta['duration_seconds'],1.1)

    def test_child_exit_removes_allowance_without_sliding_deadline(self):
        _,meta,_=self.run_provider('child-exit'); self.assertLess(meta['duration_seconds'],1.05)

    def test_double_forked_writer_stops_before_report(self):
        self.run_provider('child-orphan')
        before=(self.root/'heartbeat').read_text(); time.sleep(.15)
        self.assertEqual(before,(self.root/'heartbeat').read_text())

    def test_success_also_drains_detached_writers(self): self.run_provider('success-child',0)

    def test_continuous_activity_still_hits_wall_clock(self):
        self.configure(wall=.7)
        _,meta,_=self.run_provider('active',124)
        self.assertEqual('wall-clock',meta['timeout_kind'])

    def cancellation(self, command=None):
        self.configure(silence=2,child=3)
        proc=subprocess.Popen(command or self.command(),stdout=subprocess.PIPE,stderr=subprocess.PIPE,env={**self.env,'SILENCE_MODE':'child-orphan'})
        try:
            deadline=time.monotonic()+4
            while not (self.root/'heartbeat').exists() and time.monotonic()<deadline: time.sleep(.02)
            self.assertTrue((self.root/'heartbeat').exists())
            proc.send_signal(signal.SIGTERM); out,err=proc.communicate(timeout=6)
            self.assertEqual(130,proc.returncode,out+err)
            meta=json.loads(next((self.root/'logs').glob('*/result.json')).read_text())
            self.assertEqual('cancelled',meta['outcome']); self.assertNotIn('timeout_kind',meta)
        finally:
            if proc.poll() is None: proc.kill(); proc.communicate()

    def test_cancellation_reaps_owned_writers(self): self.cancellation()

    def test_cancellation_survives_a_transient_inspection_signal_failure(self):
        wrapper=self.root/'interrupted.py'
        wrapper.write_text('''import importlib.util, signal, sys
spec=importlib.util.spec_from_file_location('provider',sys.argv.pop(1))
p=importlib.util.module_from_spec(spec); spec.loader.exec_module(p)
original=p.a.OwnedProcesses.signal
failed=False
def flaky(self, sig):
    global failed
    if sig==signal.SIGTERM and not failed:
        failed=True
        raise PermissionError('fixture transient signal denial')
    return original(self,sig)
p.a.OwnedProcesses.signal=flaky
sys.exit(p.main())
''')
        self.cancellation([sys.executable,str(wrapper),str(SCRIPT),*self.command()[2:]])

    def test_unowned_neighbor_is_neither_allowance_nor_kill_target(self):
        neighbor=subprocess.Popen(['sleep','10'])
        try:
            _,meta,_=self.run_provider('unfinished'); self.assertLess(meta['duration_seconds'],.95)
            self.assertIsNone(neighbor.poll())
        finally: neighbor.terminate(); neighbor.wait()

    def test_configuration_hierarchy_and_validation(self):
        opts=p.configuration('implement',dict(agents=dict(runner='codex',providers=dict(codex=dict(silence_timeout_seconds=7,silence_child_timeout_seconds=9)))),{})
        self.assertEqual((7,9),(opts['silence_timeout_seconds'],opts['silence_child_timeout_seconds']))
        for bad in (-1,True,'3',None,float('nan'),float('inf'),86401):
            with self.subTest(bad=bad),self.assertRaises(ValueError):
                p.configuration('implement',dict(agents=dict(implement=dict(silence_timeout_seconds=bad))),{})
        with self.assertRaises(ValueError): p.configuration('implement',dict(agents=dict(implement=dict(silence_timeout_seconds=3,silence_child_timeout_seconds=2))),{})
        opts=p.configuration('implement',dict(agents=dict(providers=dict(claude=dict(silence_timeout_seconds=4)),implement=dict(silence_timeout_seconds=0))),{})
        self.assertEqual(0,opts['silence_timeout_seconds'])


class Pipeline(WithdrawalFixture):
    def setUp(self):
        super().setUp()
        if os.environ.get('BUREAU_TEST_SILENCE_SCRIPTS'):
            shutil.copytree(os.environ['BUREAU_TEST_SILENCE_SCRIPTS'],self.repo/'scripts',dirs_exist_ok=True)
        self.env.update(SILENCE_ROOT=str(self.root),SILENCE_DURATION='10')
        (self.root/'writer.py').write_text(WRITER)
        (self.bin/'claude').write_text(PROVIDER)
        self.config['agents']['implement']=dict(enabled=True,silence_timeout_seconds=.35,silence_child_timeout_seconds=1)
        self.config['agents']['code_review']=dict(enabled=True,timeout_seconds=4,silence_timeout_seconds=.45,silence_child_timeout_seconds=1)
        self.save_config()

    def test_implement_stops_once_and_holds_without_outside_gate(self):
        result=self.worker(); self.assertEqual(25,result.returncode,result.stdout+result.stderr)
        self.assertEqual(1,len([e for e in self.events() if e['kind']=='pass']))
        self.assertIn('needs-human',json.loads((self.root/'labels').read_text()))
        self.assertFalse([e for e in self.events() if e['kind'] in ('move','push','gh-publication')])

    def test_withdrawal_wins_silence_matrix(self):
        for index,(key,code) in enumerate([('done',0),('cancelled',26),('backlog',25)]):
            with self.subTest(key=key):
                self.wt=self.repo/'.worktrees'/str(index); self.state('build'); (self.root/'events').write_text('')
                self.control(target=STATES[key]); result=self.worker()
                self.assertEqual(code,result.returncode,result.stdout+result.stderr)
                self.assertEqual(['lane-2'],json.loads((self.root/'labels').read_text()))
                self.assert_no_publication(self.events()); self.assert_released()
                self.git('-C',str(self.wt),'checkout','--detach','-q')

    def test_label_outage_keeps_silence_exit_and_local_picker_hold(self):
        config=self.repo/'scripts/bureau-config.sh'
        with config.open('a') as out: out.write('\nadd_issue_label() { return 27; }\n')
        result=self.worker(); self.assertEqual(25,result.returncode,result.stdout+result.stderr)
        self.assertTrue((self.common/'bureau/needs-human-held/EXP-9').is_file())
        self.assertEqual(1,len([e for e in self.events() if e['kind']=='pass']))
        self.assertFalse([e for e in self.events() if e['kind'] in ('move','push','gh-publication')])

    def test_review_done_withdrawal_wins_sibling_cancellation(self):
        self.state('build_review'); self.control(target=STATES['done'])
        result=self.worker('code-review'); self.assertEqual(0,result.returncode,result.stdout+result.stderr)
        self.assertEqual(['lane-2'],json.loads((self.root/'labels').read_text()))
        self.assert_only_review_reservation(); self.assert_released()

    def test_silent_security_stops_two_live_siblings_and_records_partial_round(self):
        self.state('build_review')
        result=self.worker('code-review'); self.assertEqual(25,result.returncode,result.stdout+result.stderr)
        self.assertEqual({'correctness','security','performance'},{e['phase'] for e in self.events() if e['kind']=='pass'})
        self.assertIn('needs-human',json.loads((self.root/'labels').read_text()))
        manifests=list((self.common/'bureau/evidence/v1/runs').glob('*/manifest.json'))
        rounds=[a['review_round'] for f in manifests for a in json.loads(f.read_text())['attempts'] if 'review_round' in a]
        self.assertEqual(1,len(rounds)); row=rounds[0]
        self.assertEqual(3,row['calls']); self.assertIsNone(row['provider_verdict']); self.assertIsNone(row['final_verdict'])
        self.assertTrue(all(v is None for v in row['usage'].values()))
        self.assertFalse([e for e in self.events() if e['kind']=='gh-publication'])
        receipts=[json.loads(f.read_text()) for f in (self.common/'bureau/evidence/v1/runs').glob('*/receipts/*.json')]
        self.assertEqual(1,len([r for r in receipts if r.get('timeout_kind')=='silence']))
        self.assertEqual(2,len([r for r in receipts if r['normalized_exit']==130]))


class ActivityRules(unittest.TestCase):
    def test_fixed_monotonic_window_and_child_exit(self):
        now=[0]
        activity=p.a.Activity('codex',3,8,clock=lambda:now[0])
        processes=type('Processes',(),{'alive':False,'tool_alive':lambda self,cmd,key:self.alive})()
        activity.line('{"type":"item.started","item":{"id":"t","type":"command_execution","command":"sleep 10"}}')
        now[0]=4
        self.assertTrue(activity.expired(processes),'unfinished item alone got an allowance')
        processes.alive=True; self.assertFalse(activity.expired(processes))
        now[0]=8; self.assertTrue(activity.expired(processes),'live child slid the deadline')
        now[0]=4; processes.alive=False; self.assertTrue(activity.expired(processes))

    def test_noise_duplicate_and_progress_classification(self):
        for runner in ('codex','claude'):
            now=[0]; activity=p.a.Activity(runner,3,8,clock=lambda:now[0])
            for line in ('oops','null','[]','{"type":"ping"}','{"type":"system","subtype":"keepalive"}'):
                now[0]+=1; activity.line(line)
            self.assertEqual(0,activity.last)
            event=({'type':'item.completed','item':{'id':'m','type':'agent_message','text':'progress'}} if runner=='codex'
                   else {'type':'assistant','message':{'id':'m','content':[{'type':'text','text':'progress'}]}})
            activity.line(json.dumps(event)); self.assertEqual(5,activity.last)
            now[0]=6; activity.line(json.dumps(event)); self.assertEqual(5,activity.last)

    def test_tool_completion_removes_allowance_and_replays_do_not_reopen(self):
        activity=p.a.Activity('codex',3,8)
        started=dict(type='item.started',item=dict(id='t',type='command_execution',command='sleep 10'))
        activity.line(json.dumps(started)); self.assertIn('t',activity.tools)
        activity.line(json.dumps({**started,'type':'item.completed'})); self.assertFalse(activity.tools)
        activity.line(json.dumps(started)); self.assertFalse(activity.tools)
        with patch.object(p.a.sys,'platform','darwin'):
            argv=['/Frameworks/Python.app/Contents/MacOS/Python','-c','print(1)']
            self.assertTrue(p.a.command_matches("/usr/local/bin/python3 -c 'print(1)'",argv))
            self.assertFalse(p.a.command_matches("/usr/local/bin/python3 -c 'print(2)'",argv))

    def test_reused_pid_is_never_signalled(self):
        owned=p.a.OwnedProcesses.__new__(p.a.OwnedProcesses)
        owned.root=123; owned.owned={123:'old-birth'}; owned.current={123:(1,'old-birth',False)}
        owned.table=type('Table',(),{'snapshot':lambda self:{123:(1,'new-birth',False)}})()
        with patch.object(p.a.os,'kill') as kill:
            owned.signal(signal.SIGKILL); kill.assert_not_called()


class GateFanout(unittest.TestCase):
    def test_live_reader_preserves_gate_matching_and_recovery(self):
        from provider_adapter_test import ProviderTests
        fixture=ProviderTests('test_gate_plan_uses_only_digests_and_rejects_foreign_receipt')
        fixture.setUp(); self.addCleanup(fixture.doCleanups)
        command="printf 'outside\n' >> outside-runs; echo green"
        fixture.config.write_text(json.dumps(dict(agents=dict(runner='codex',implement=dict(silence_timeout_seconds=1)),
                                                   repo=dict(test_command=command))))
        value=fixture.sandbox_result('gate aborted')
        value['notes']['needs_human'][0].update(kind='gate',command=command)
        context=fixture.root/'context.json'
        fixture.run_provider('--schema',str(ROOT/'templates/scripts/bureau-implement.schema.json'),
                             '--gate-context',str(context),FINAL=json.dumps(value),GATE_COMMAND=command)
        plan=fixture.gate_action('plan',value,context=context)
        self.assertTrue(plan['eligible'],plan)
        self.assertTrue(fixture.gate_action('run',plan)['green'])
        self.assertTrue(fixture.gate_action('run',plan)['green'])
        self.assertEqual(['outside'],(fixture.root/'outside-runs').read_text().splitlines())
        receipt=json.loads(Path(json.loads(context.read_text())['receipt']).read_text())
        self.assertEqual('timeout',receipt['gate_attempts'][0]['abort'])
        self.assertEqual(124,receipt['gate_attempts'][0]['normalized_exit'])
        self.assertNotIn('timeout_kind',receipt)


if __name__=='__main__': unittest.main(verbosity=2)
