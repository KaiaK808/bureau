import importlib.util
import json
import os
from pathlib import Path
import signal
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import uuid

ROOT=Path(__file__).resolve().parents[1]
SCRIPT=ROOT/'templates/scripts/bureau-provider.py'
spec=importlib.util.spec_from_file_location('provider',SCRIPT)
p=importlib.util.module_from_spec(spec); spec.loader.exec_module(p)


class ProviderTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='bureau provider '); self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name); self.bin=self.root/'bin'; self.bin.mkdir()
        self.config=self.root/'config.json'; self.config.write_text('{"agents":{"runner":"codex"}}')
        self.prompt=self.root/'prompt.txt'; self.prompt.write_text('Review this fixture')
        fake='#!'+sys.executable+'''
import json, os, pathlib, signal, sys, time
name=pathlib.Path(sys.argv[0]).name
root=pathlib.Path(os.environ['FAKE_ROOT'])
if sys.argv[1] in ('auth','login'):
    if os.environ.get('AUTH_FAIL'): sys.exit(1)
    print(json.dumps({'loggedIn':True})); sys.exit(0)
# A Claude CLI before --session-id (OLD_CLI): its help does not list the flag and it rejects
# the flag as an unknown option, as commander does. HELP_FAIL: the help itself fails.
if sys.argv[1]=='--help':
    with (root/'help-calls').open('a') as calls: calls.write('help\\n')
    (root/'help-env.json').write_text(json.dumps(sorted(os.environ)))
    if os.environ.get('HELP_FAIL'): sys.exit(2)
    print('Usage: claude [options] [command] [prompt]')
    print('  -p, --print   Print response and exit')
    if not os.environ.get('OLD_CLI'): print('  --session-id <uuid>   Use a specific session ID for the conversation')
    sys.exit(0)
if os.environ.get('OLD_CLI') and '--session-id' in sys.argv:
    print("error: unknown option '--session-id'",file=sys.stderr); sys.exit(1)
(root/'argv.json').write_text(json.dumps(sys.argv[1:]))
(root/'stdin.txt').write_text(sys.stdin.read())
tmpdir=os.environ.get('TMPDIR')
(root/'tmpdir.json').write_text(json.dumps({'value':tmpdir,'is_dir':bool(tmpdir and pathlib.Path(tmpdir).is_dir())}))
if os.environ.get('FORK_IGNORE_TERM'):
    descendant = os.fork()
    if descendant == 0:
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        (root/'heartbeat').write_text(str(time.monotonic_ns()))
        (root/'descendant.tmp').write_text(json.dumps({'pid':os.getpid(),'group':os.getpgrp()}))
        (root/'descendant.tmp').replace(root/'descendant.json')
        while True:
            (root/'heartbeat').write_text(str(time.monotonic_ns()))
            time.sleep(.05)
    while not (root/'descendant.json').exists(): time.sleep(.01)
# What the CLI itself records while it works: Claude its transcript, named by --session-id,
# under TRANSCRIPT_DIR (the test computes that directory); Codex its thread.started event.
if name=='claude' and os.environ.get('TRANSCRIPT_DIR') and '--session-id' in sys.argv:
    folder=pathlib.Path(os.environ['TRANSCRIPT_DIR']); folder.mkdir(parents=True,exist_ok=True)
    (folder/(sys.argv[sys.argv.index('--session-id')+1]+'.jsonl')).write_text('{"type":"user"}\\n')
if name=='codex' and os.environ.get('THREAD'):
    print(json.dumps({'type':'thread.started','thread_id':os.environ['THREAD']}),flush=True)
if os.environ.get('RECORD_SIGNALS'):
    def record(signum, frame):
        with open(root/'signals','a') as out: out.write(signal.Signals(signum).name+chr(10))
        sys.exit(128+signum)
    for name in ('SIGTERM','SIGINT','SIGHUP'): signal.signal(getattr(signal,name),record)
(root/'ready').touch()
if os.environ.get('IGNORE_TERM'): signal.signal(signal.SIGTERM, signal.SIG_IGN)
if os.environ.get('SLEEP'): time.sleep(float(os.environ['SLEEP']))
if os.environ.get('FAIL'):
    print(os.environ['FAIL'],file=sys.stderr); sys.exit(1)
final=os.environ.get('FINAL','final response')
if name=='codex':
    output=sys.argv[sys.argv.index('-o')+1]
    if not os.environ.get('EMPTY'): pathlib.Path(output).write_text(final)
    print(json.dumps({'type':'turn.completed','usage':{'input_tokens':12,'output_tokens':3}}))
    print('diagnostic APPROVE text should never become the result')
elif os.environ.get('RAW'):
    print(os.environ['RAW'])
else:
    print(json.dumps({'result':final,'usage':{'input_tokens':12,'output_tokens':3},'total_cost_usd':0.01}))
'''
        for name in ('claude','codex'):
            path=self.bin/name; path.write_text(fake); path.chmod(0o755)
        self.fake=fake
        self.env={**os.environ,'PATH':str(self.bin)+os.pathsep+os.environ['PATH'],'FAKE_ROOT':str(self.root),
                  'BUREAU_PROVIDER_LOG_DIR':str(self.root/'evidence')}
        for key in list(self.env):
            if key.startswith(('BUREAU_RUNNER_','BUREAU_MODEL_','BUREAU_CODEX_MODEL_','BUREAU_STAGE_TIMEOUT')): self.env.pop(key)
        # The provider looks for the CLI's own records here, never in the real home.
        self.env.update(CLAUDE_CONFIG_DIR=str(self.root/'claude config'),CODEX_HOME=str(self.root/'codex home'))

    def command(self,*extra):
        return [sys.executable,str(SCRIPT),'--stage','implement','--config',str(self.config),
                '--repo',str(getattr(self,'repo',self.root)),'--prompt-file',str(self.prompt),*extra]

    def run_provider(self,*extra,code=0,**env):
        result=subprocess.run(self.command(*extra),capture_output=True,text=True,env={**self.env,**env},timeout=12)
        self.assertEqual(result.returncode,code,result.stdout+result.stderr)
        return result

    def sandbox_result(self,reason='SANDBOX_GATE: socket test: Operation not permitted on bind'):
        return {'status':'NEEDS_HUMAN','tasks_done':2,'tasks_skipped':0,'tasks_needs_human':1,
                'fixed_review_items':[],'notes':{'needs_human':[{'task_id':'T003','reason':reason}],
                'skipped':[],'deviations':[]},'prose_notes':'Other tasks are finished'}

    def recorded_tmpdir(self):
        record=json.loads((self.root/'tmpdir.json').read_text())
        self.assertTrue(record['is_dir'],'TMPDIR was not a directory during the call')
        path=Path(record['value'])
        self.assertEqual(path.parent,Path('/tmp').resolve())
        self.assertTrue(path.name.startswith('bureau-codex-'))
        self.assertFalse(path.exists(),'the Codex temporary directory survived the call')
        return path

    def run_wrapped_provider(self,patch,code=0,**env):
        # Instrument the actual adapter in a separate process: allocation and
        # lifecycle failures still run main(), its exit handling and cleanup.
        source='''import importlib.util, json, os, pathlib, signal, sys
spec=importlib.util.spec_from_file_location('provider',sys.argv[1])
p=importlib.util.module_from_spec(spec); spec.loader.exec_module(p)
del sys.argv[1]
root=pathlib.Path(os.environ['FAKE_ROOT'])
allocate=p.tempfile.mkdtemp
def record_allocation(*args,**kwargs):
    path=allocate(*args,**kwargs)
    (root/'allocation.json').write_text(json.dumps({'value':path,'is_dir':pathlib.Path(path).is_dir()}))
    return path
p.tempfile.mkdtemp=record_allocation
original_run=p.run
def record_environment(*args,**kwargs):
    before=dict(os.environ)
    try: return original_run(*args,**kwargs)
    finally: (root/'parent-env.json').write_text(json.dumps({'unchanged':dict(os.environ)==before}))
p.run=record_environment
'''+patch+'\nsys.exit(p.main())\n'
        result=subprocess.run([sys.executable,'-c',source,str(SCRIPT),*self.command()[2:]],
                              capture_output=True,text=True,env={**self.env,**env},timeout=12)
        self.assertEqual(result.returncode,code,result.stdout+result.stderr)
        self.assertTrue(json.loads((self.root/'parent-env.json').read_text())['unchanged'])
        return result

    def test_sandbox_gate_only_requires_a_nonempty_array_of_reason_objects(self):
        good={'reason':'SANDBOX_GATE: denied bind'}
        cases=[(None,False),([],False),({},False),({'notes':[]},False),
               ({'notes':{'needs_human':[good]}},True),({'notes':{'needs_human':[good,good]}},True)]
        for items in ([],None,{},good,'SANDBOX_GATE:',[None],['SANDBOX_GATE:'],[[]],[{}],
                      [{'reason':None}],[{'reason':42}],[{'reason':[]}],[{'reason':{}}],
                      [{'reason':''}],[{'reason':'other'}],[good,{'reason':'other'}]):
            cases.append(({'notes':{'needs_human':items}},False))
        for value,expected in cases:
            with self.subTest(value=value): self.assertIs(p.sandbox_gate_only(value),expected)

    def test_environment_blocked_keeps_the_exception_codex_only(self):
        for runner in ('codex','claude'):
            for prefix in ('SANDBOX_GATE: ',''):
                value=self.sandbox_result(prefix+'Operation not permitted on bind')
                for text in ('Operation not permitted','Permission denied','sandbox denied'):
                    with self.subTest(runner=runner,prefix=prefix,text=text):
                        self.assertIs(p.environment_blocked(value,text,runner,'implement'),runner=='claude' or not prefix)
                self.assertFalse(p.environment_blocked(value,'code failure',runner,'implement'))
                self.assertFalse(p.environment_blocked({**value,'status':'COMPLETE'},'Permission denied',runner,'implement'))
        mixed=self.sandbox_result()
        mixed['notes']['needs_human'].append({'reason':'Permission denied reading a required file'})
        self.assertTrue(p.environment_blocked(mixed,json.dumps(mixed),'codex','implement'))
        self.assertFalse(p.environment_blocked(None,'Permission denied','codex','implement'))

    def test_qa_sandbox_gate_only_requires_needs_human_and_a_string_prefix(self):
        good={'status':'NEEDS_HUMAN','coverage_notes':'SANDBOX_GATE: denied bind'}
        cases=[(good,True),(None,False),([],False),({},False),
               ({'coverage_notes':good['coverage_notes']},False),({'status':'NEEDS_HUMAN'},False)]
        for status in ('GREEN','RED','',None,42,['NEEDS_HUMAN']):
            cases.append(({**good,'status':status},False))
        for notes in (None,42,[],{},False,'','other blocker',' SANDBOX_GATE: bind','other; SANDBOX_GATE: bind'):
            cases.append(({**good,'coverage_notes':notes},False))
        for value,expected in cases:
            with self.subTest(value=value): self.assertIs(p.qa_sandbox_gate_only(value),expected)

    def test_environment_blocked_keeps_the_qa_exception_stage_and_runner_specific(self):
        for runner in ('codex','claude'):
            for prefix in ('SANDBOX_GATE: ',''):
                value={'status':'NEEDS_HUMAN','coverage_notes':prefix+'Operation not permitted on bind'}
                for text in ('Operation not permitted','Permission denied','sandbox denied'):
                    with self.subTest(runner=runner,prefix=prefix,text=text):
                        self.assertIs(p.environment_blocked(value,text,runner,'qa'),runner=='claude' or not prefix)
                self.assertFalse(p.environment_blocked(value,'code failure',runner,'qa'))
                self.assertFalse(p.environment_blocked({**value,'status':'GREEN'},'Permission denied',runner,'qa'))
        qa={'status':'NEEDS_HUMAN','coverage_notes':'SANDBOX_GATE: Permission denied on bind'}
        for stage in ('implement','spec_review','code_review','copy'):
            with self.subTest(stage=stage):
                self.assertTrue(p.environment_blocked(qa,json.dumps(qa),'codex',stage))
        self.assertTrue(p.environment_blocked(self.sandbox_result(),'Operation not permitted','codex','qa'))
        self.assertFalse(p.environment_blocked(None,'Permission denied','codex','qa'))

    def test_codex_qa_sandbox_result_reaches_shell_but_unprefixed_blocker_exits_24(self):
        schema=ROOT/'templates/scripts/bureau-qa.schema.json'
        value={'status':'NEEDS_HUMAN','tests_added':0,'tests_failing':0,
               'coverage_notes':'SANDBOX_GATE: socket test: Operation not permitted on bind'}
        result=self.run_provider('--stage','qa','--schema',str(schema),FINAL=json.dumps(value))
        self.assertEqual(json.loads(result.stdout),value)
        self.config.write_text('{"agents":{"runner":"codex"},"session":{"cost_tracking":true}}')
        result=self.run_provider('--stage','qa','--schema',str(schema),FINAL=json.dumps(value))
        self.assertEqual(json.loads(json.loads(result.stdout)['result']),value)
        plain={**value,'coverage_notes':'socket test: Operation not permitted on bind'}
        result=self.run_provider('--stage','qa','--schema',str(schema),FINAL=json.dumps(plain),code=24)
        self.assertEqual(result.stdout,'')
        self.assertEqual(self.evidence()[1]['outcome'],'environment')
        self.config.write_text('{"agents":{"runner":"claude"}}')
        result=self.run_provider('--stage','qa','--schema',str(schema),FINAL=json.dumps(value),code=24)
        self.assertEqual(result.stdout,'')
        self.assertEqual(self.evidence()[1]['outcome'],'environment')

    def test_codex_sandbox_result_reaches_shell_but_unprefixed_blocker_exits_24(self):
        self.env.pop('TMPDIR',None)
        schema=ROOT/'templates/scripts/bureau-implement.schema.json'
        value=self.sandbox_result()
        result=self.run_provider('--schema',str(schema),FINAL=json.dumps(value))
        self.assertEqual(json.loads(result.stdout),value)
        self.recorded_tmpdir()
        self.config.write_text('{"agents":{"runner":"codex"},"session":{"cost_tracking":true}}')
        result=self.run_provider('--schema',str(schema),FINAL=json.dumps(value))
        self.assertEqual(json.loads(json.loads(result.stdout)['result']),value)
        self.recorded_tmpdir()
        result=self.run_provider('--schema',str(schema),FINAL=json.dumps(self.sandbox_result('Operation not permitted on bind')),code=24)
        self.assertEqual(result.stdout,'')
        self.assertEqual(self.evidence()[1]['outcome'],'environment')
        self.recorded_tmpdir()
        self.config.write_text('{"agents":{"runner":"claude"}}')
        result=self.run_provider('--schema',str(schema),FINAL=json.dumps(value),code=24)
        self.assertEqual(result.stdout,'')
        self.assertIsNone(json.loads((self.root/'tmpdir.json').read_text())['value'])

    def test_codex_tmpdir_is_under_tmp_even_when_temp_and_tmp_point_into_repo(self):
        self.env.pop('TMPDIR',None)
        for key in ('TEMP','TMP'):
            path=self.root/key; path.mkdir(); self.env[key]=str(path)
        self.run_provider()
        self.recorded_tmpdir()

    def test_existing_tmpdir_is_passed_through_and_kept(self):
        path=self.root/'explicit tmp'; path.mkdir()
        self.run_provider(TMPDIR=str(path))
        self.assertEqual(json.loads((self.root/'tmpdir.json').read_text()),{'value':str(path),'is_dir':True})
        self.assertTrue(path.is_dir(),'the caller owns an explicit TMPDIR')

    def test_claude_gets_no_added_tmpdir(self):
        self.env.pop('TMPDIR',None)
        self.config.write_text('{"agents":{"runner":"claude"}}')
        self.run_provider()
        self.assertEqual(json.loads((self.root/'tmpdir.json').read_text()),{'value':None,'is_dir':False})

    def test_codex_tmpdir_is_removed_after_timeout_and_provider_errors(self):
        self.env.pop('TMPDIR',None)
        for code,env in ((124,{'SLEEP':'10','BUREAU_STAGE_TIMEOUT':'0.1'}),
                         (22,{'FAIL':'backend failed'}),(22,{'EMPTY':'1'})):
            with self.subTest(code=code,env=env):
                self.run_provider(code=code,**env)
                self.recorded_tmpdir()

    def test_signals_during_allocation_are_handled_and_tmpdir_is_removed(self):
        self.env.pop('TMPDIR',None)
        for signum in ('SIGTERM','SIGINT','SIGHUP'):
            with self.subTest(signal=signum):
                patch='''def interrupted_allocation(*args,**kwargs):
    path=record_allocation(*args,**kwargs)
    os.kill(os.getpid(),getattr(signal,os.environ['ALLOCATION_SIGNAL']))
    return path
p.tempfile.mkdtemp=interrupted_allocation
'''
                result=self.run_wrapped_provider(patch,code=130,ALLOCATION_SIGNAL=signum,SLEEP='10')
                self.assertEqual(result.stdout,'')
                self.assertEqual(self.evidence()[1]['outcome'],'cancelled')
                record=json.loads((self.root/'allocation.json').read_text())
                self.assertTrue(record['is_dir'])
                self.assertFalse(Path(record['value']).exists())

    def test_allocation_is_cleaned_when_popen_raises(self):
        self.env.pop('TMPDIR',None)
        result=self.run_wrapped_provider('''original_popen=p.subprocess.Popen
def fail_popen(command,*args,**kwargs):
    if command[:2]==['codex','exec']: raise OSError('fixture Popen failure')
    return original_popen(command,*args,**kwargs)
p.subprocess.Popen=fail_popen
''',code=22)
        self.assertIn('fixture Popen failure',result.stderr)
        record=json.loads((self.root/'allocation.json').read_text())
        self.assertTrue(record['is_dir'])
        self.assertFalse(Path(record['value']).exists())

    def test_failed_tmpdir_removal_is_reported_without_failing_the_run(self):
        self.env.pop('TMPDIR',None)
        result=self.run_wrapped_provider('''def fail_remove(path):
    raise OSError('fixture removal failure')
p.shutil.rmtree=fail_remove
''')
        path=Path(json.loads((self.root/'allocation.json').read_text())['value'])
        self.addCleanup(shutil.rmtree,path)
        self.assertTrue(path.is_dir(),'the failed removal was not exercised')
        self.assertIn('Bureau provider: could not remove '+str(path),result.stderr)
        self.assertIn('fixture removal failure',result.stderr)
        self.assertEqual(result.stdout.strip(),'final response')

    def test_codex_stdin_large_prompt_and_exact_argv(self):
        self.prompt.write_text('quoted `$(no-command)` '+('large '*250000))
        result=self.run_provider(BUREAU_CODEX_MODEL_IMPLEMENT='model with spaces;echo nope')
        args=json.loads((self.root/'argv.json').read_text())
        self.assertEqual(args[args.index('--model')+1],'model with spaces;echo nope')
        self.assertIn(self.prompt.read_text(),(self.root/'stdin.txt').read_text())
        self.assertEqual(args[-1],'-'); self.assertNotIn('--append-system-prompt',args)
        self.assertEqual(result.stdout.strip(),'final response')
        self.assertNotIn('APPROVE',result.stdout)

    def test_invalid_provider_configuration_is_classified(self):
        for config in ({'agents':{'providers':[]}}, {'agents':{'runner':'codex','model':42}},
                       {'agents':{'runner':'codex','implement':{'model':[]},'providers':{'codex':{'model':'valid-model'}}}},
                       {'agents':{'runner':'codex','implement':{'timeout_seconds':'NaN'}}}):
            self.config.write_text(json.dumps(config))
            self.run_provider(code=22)

    def test_claude_envelope_and_cost_tracking(self):
        self.config.write_text('{"agents":{"runner":"claude"},"session":{"cost_tracking":true}}')
        result=self.run_provider(FINAL='```json\n{"status":"COMPLETE"}\n```')
        value=json.loads(result.stdout)
        self.assertEqual(value['provider'],'claude'); self.assertEqual(value['usage']['input_tokens'],12)
        self.assertIn('COMPLETE',value['result'])

    def test_schema_validates_actual_final_and_rejects_invalid(self):
        schema=ROOT/'templates/scripts/bureau-qa.schema.json'
        value={'status':'GREEN','tests_added':0,'tests_failing':0,'coverage_notes':'ok'}
        result=self.run_provider('--schema',str(schema),FINAL=json.dumps(value))
        self.assertEqual(json.loads(result.stdout),value)
        for bad in ('not json','{"status":"GREEN"}',json.dumps({**value,'status':'APPROVE'})):
            result=self.run_provider('--schema',str(schema),FINAL=bad,code=22)
            self.assertEqual(result.stdout,'')

    def test_auth_provider_errors_environment_and_empty_output(self):
        self.run_provider(AUTH_FAIL='1',code=16)
        for message,code in [('quota exceeded',23),('Permission denied by sandbox',24),('backend failed',22),('Not logged in',16)]:
            with self.subTest(message=message): self.run_provider(FAIL=message,code=code)
        self.run_provider(EMPTY='1',code=22)

    def test_timeout_and_cancellation_kill_process_group(self):
        self.env.pop('TMPDIR',None)
        self.run_provider(SLEEP='10',BUREAU_STAGE_TIMEOUT='0.1',code=124)
        self.recorded_tmpdir()
        (self.root/'ready').unlink()
        proc=subprocess.Popen(self.command(),env={**self.env,'SLEEP':'10'},stdout=subprocess.PIPE,stderr=subprocess.PIPE)
        try:
            deadline=time.monotonic()+4
            while not (self.root/'ready').exists() and time.monotonic()<deadline: time.sleep(.02)
            self.assertTrue((self.root/'ready').exists(),'fake provider did not start')
            proc.terminate(); stdout,stderr=proc.communicate(timeout=8)
            self.assertEqual(proc.returncode,130,stderr.decode()); self.assertEqual(stdout,b'')
            self.recorded_tmpdir()
        finally:
            if proc.poll() is None: proc.kill()
            proc.communicate()

    def test_hangup_stops_the_call_like_sigterm(self):
        # A hang-up reaches the adapter when SIGHUP goes to a stage's process group (the
        # runtime itself forwards one as SIGTERM). It ends the call as cancelled, 130, and the
        # agent's process group gets SIGTERM, not SIGHUP, which it may ignore (v3.2.0, O1).
        # Under nohup (SIGHUP ignored when the adapter starts) the call goes on, as in v3.1.
        for nohup in (False,True):
            with self.subTest(nohup=nohup):
                for name in ('ready','signals'): (self.root/name).unlink(missing_ok=True)
                disposition=signal.SIG_IGN if nohup else signal.SIG_DFL
                proc=subprocess.Popen(self.command(),env={**self.env,'SLEEP':'10','RECORD_SIGNALS':'1'},
                                      stdout=subprocess.PIPE,stderr=subprocess.PIPE,
                                      preexec_fn=lambda: signal.signal(signal.SIGHUP,disposition))
                try:
                    deadline=time.monotonic()+4
                    while not (self.root/'ready').exists() and time.monotonic()<deadline: time.sleep(.02)
                    self.assertTrue((self.root/'ready').exists(),'fake provider did not start')
                    proc.send_signal(signal.SIGHUP)
                    if nohup:
                        time.sleep(1)
                        self.assertIsNone(proc.poll(),'a call started under nohup ended on SIGHUP')
                        self.assertFalse((self.root/'signals').exists(),'the agent got a signal under nohup')
                        proc.terminate()
                    stdout,stderr=proc.communicate(timeout=8)
                    self.assertEqual(proc.returncode,130,stderr.decode()); self.assertEqual(stdout,b'')
                    self.assertIn(b'Bureau provider outcome: cancelled',stderr)
                    self.assertEqual((self.root/'signals').read_text(),'SIGTERM\n')
                finally:
                    if proc.poll() is None: proc.kill()
                    proc.communicate()

    def test_hangup_on_a_closing_terminal_keeps_the_cancelled_outcome(self):
        # The adapter on a terminal that closes (a pane of a killed tmux session): the kernel's
        # SIGHUP stops the call, and the exit code and the evidence still say cancelled,
        # although the terminal is gone for the adapter's last lines (v3.2.0, O1).
        result=subprocess.run([sys.executable,str(ROOT/'tests/lib/hangup.py'),'--ready',str(self.root/'ready'),
                               '--how','close','--wait','15','--',*self.command()],
                              env={**self.env,'SLEEP':'10','RECORD_SIGNALS':'1'},capture_output=True,text=True,timeout=40)
        self.assertEqual(result.stdout.split()[:1],['rc=130'],result.stdout+result.stderr)
        outcomes=[json.loads(path.read_text()).get('outcome') for path in (self.root/'evidence').glob('*/result.json')]
        self.assertEqual(outcomes,['cancelled'])
        self.assertEqual((self.root/'signals').read_text(),'SIGTERM\n')

    def test_timeout_and_cancel_stop_descendant_after_leader_exits(self):
        for mode, code in (('timeout', 124), ('term', 130), ('interrupt', 130), ('runtime-timeout', 124)):
            with self.subTest(mode=mode):
                pid_file=self.root/'descendant.json'; pid_file.unlink(missing_ok=True)
                env={**self.env,'FORK_IGNORE_TERM':'1','SLEEP':'30',
                     'BUREAU_STAGE_TIMEOUT':'1' if mode.endswith('timeout') else '30'}
                command=self.command()
                if mode == 'runtime-timeout':
                    subprocess.run(['git','init','-q',str(self.root)],check=True)
                    env['BUREAU_CONFIG']=str(self.config)
                    command=[sys.executable,str(ROOT/'templates/scripts/bureau-runtime.py'),
                             '--repo',str(self.root),'exec','--issue','TEAM-123','--',*command]
                proc=subprocess.Popen(command,env=env,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
                try:
                    deadline=time.monotonic()+4
                    while not pid_file.exists() and time.monotonic()<deadline: time.sleep(.02)
                    self.assertTrue(pid_file.exists(),'fake provider descendant did not start')
                    descendant=json.loads(pid_file.read_text())
                    if not mode.endswith('timeout'): proc.send_signal(signal.SIGTERM if mode == 'term' else signal.SIGINT)
                    stdout,stderr=proc.communicate(timeout=8)
                    self.assertEqual(proc.returncode,code,stderr.decode())
                    self.assertEqual(stdout,b'')
                    heartbeat=(self.root/'heartbeat').read_text()
                    if mode == 'runtime-timeout':
                        self.assertEqual(json.loads((self.root/'.git/bureau/leases.json').read_text()),{})
                    # A killed orphan can briefly be a zombie on CI. It cannot
                    # write; a live descendant can outlast the released lease.
                    deadline=time.monotonic()+2
                    while True:
                        status=subprocess.run(['ps','-p',str(descendant['pid']),'-o','stat='],capture_output=True,text=True)
                        self.assertIn(status.returncode,(0,1),status.stderr)
                        self.assertFalse(status.stderr.strip(),status.stderr)
                        active=bool(status.stdout.strip()) and not status.stdout.strip().startswith('Z')
                        if not active or time.monotonic() >= deadline: break
                        time.sleep(.02)
                    self.assertFalse(active,'provider descendant survived the bounded invocation')
                    time.sleep(.1)
                    self.assertEqual((self.root/'heartbeat').read_text(),heartbeat)
                finally:
                    if pid_file.exists():
                        descendant=json.loads(pid_file.read_text())
                        try:
                            if os.getpgid(descendant['pid']) == descendant['group']:
                                os.killpg(descendant['group'],signal.SIGKILL)
                        except ProcessLookupError: pass
                    if proc.poll() is None: proc.kill()
                    proc.communicate()

    def test_runner_and_model_resolution_is_provider_specific(self):
        config={'version':2,'agents':{'model':'claude-default','implement':{'runner':'codex','model':'codex-stage'},
                          'providers':{'codex':{'model':'codex-default','reasoning_effort':'high'}}}}
        self.assertEqual(p.configuration('implement',config,{})['model'],'codex-stage')
        self.assertEqual(p.configuration('implement',config,{'BUREAU_CODEX_MODEL_IMPLEMENT':'override'})['model'],'override')
        self.assertEqual(p.configuration('qa',config,{'BUREAU_RUNNER_QA':'codex'})['model'],'codex-default')
        self.assertEqual(p.configuration('qa',config,{})['model'],'claude-default')
        self.assertEqual(p.configuration('spec_review',{'agents':{'runner':'codex'}},{})['sandbox'],'workspace-write')
        with self.assertRaises(ValueError): p.configuration('qa',config,{'BUREAU_RUNNER_QA':'typo'})
        with self.assertRaises(ValueError): p.configuration('qa',config,{'BUREAU_SANDBOX_QA':'danger-full-access'})

    def test_legacy_codex_model_selection_reaches_the_actual_cli(self):
        # Existing repos used these generic settings for Claude. Switching the
        # runner through any supported route must still select the Codex env
        # default, including after a v2 migration preserving v1 semantics.
        for version in (None, 1, 2):
            for route in ('environment', 'stage', 'default'):
                with self.subTest(version=version, route=route):
                    agents={'model':'claude-default','implement':{'model':'opus'}}
                    config={'agents':agents}
                    if version is not None: config['version']=version
                    if version == 2: agents['model_compatibility']='v1'
                    env={'BUREAU_MODEL_IMPLEMENT':'sonnet', 'BUREAU_CODEX_MODEL_DEFAULT':'codex-old-default'}
                    if route == 'environment': env['BUREAU_RUNNER_IMPLEMENT']='codex'
                    elif route == 'stage': agents['implement']['runner']='codex'
                    else: agents['runner']='codex'
                    self.config.write_text(json.dumps(config))
                    self.run_provider(**env)
                    argv=json.loads((self.root/'argv.json').read_text())
                    self.assertEqual(argv[argv.index('--model')+1],'codex-old-default')

        # Without an explicit Codex setting, keep the CLI's own default.
        self.config.write_text(json.dumps({'agents':{'runner':'codex','model':'opus','implement':{'model':'sonnet'}}}))
        self.run_provider(BUREAU_MODEL_IMPLEMENT='haiku')
        self.assertNotIn('--model',json.loads((self.root/'argv.json').read_text()))
        self.run_provider(BUREAU_CODEX_MODEL_IMPLEMENT='codex-stage',BUREAU_CODEX_MODEL_DEFAULT='codex-default')
        argv=json.loads((self.root/'argv.json').read_text())
        self.assertEqual(argv[argv.index('--model')+1],'codex-stage')

    def test_v2_model_ownership_and_compatibility_validation(self):
        self.config.write_text(json.dumps({'version':2,'agents':{'runner':'codex','implement':{'model':'codex-v2'}}}))
        self.run_provider(BUREAU_CODEX_MODEL_DEFAULT='lower-priority')
        argv=json.loads((self.root/'argv.json').read_text())
        self.assertEqual(argv[argv.index('--model')+1],'codex-v2')
        self.config.write_text(json.dumps({'agents':{'runner':'codex','model_compatibility':'v2','model':'codex-opt-in'}}))
        self.run_provider()
        argv=json.loads((self.root/'argv.json').read_text())
        self.assertEqual(argv[argv.index('--model')+1],'codex-opt-in')
        for value in ('v3', None, False, {}):
            self.config.write_text(json.dumps({'agents':{'model_compatibility':value}}))
            self.run_provider(code=22)

    def test_headroom_passes_separator_to_the_real_adapter(self):
        self.config.write_text('{"agents":{"runner":"claude","headroom_wrap":true}}')
        wrapper=self.bin/'headroom'
        wrapper.write_text('#!/bin/sh\n[ "$1" = wrap ] && [ "$2" = claude ] && [ "$3" = -- ] || exit 99\nshift 3\nexec claude "$@"\n')
        wrapper.chmod(0o755)
        self.run_provider()

    def claude_argv(self,config,**env):
        self.config.write_text(json.dumps(config))
        (self.root/'argv.json').unlink(missing_ok=True)
        self.run_provider(**env)
        return json.loads((self.root/'argv.json').read_text())

    def test_claude_gets_the_resolved_effort(self):
        # Stage, provider and environment, in configuration()'s precedence.
        stage={'agents':{'runner':'claude','implement':{'reasoning_effort':'xhigh'},
                         'providers':{'claude':{'reasoning_effort':'low'}}}}
        provider={'agents':{'runner':'claude','providers':{'claude':{'reasoning_effort':'medium'}}}}
        for config,env,effort in ((stage,{},'xhigh'),(provider,{},'medium'),
                                  (stage,{'BUREAU_REASONING_IMPLEMENT':'max'},'max'),
                                  (provider,{'BUREAU_REASONING_IMPLEMENT':'high'},'high')):
            with self.subTest(config=config,env=env):
                argv=self.claude_argv(config,**env)
                self.assertEqual(argv.count('--effort'),1)
                self.assertEqual(argv[argv.index('--effort')+1],effort)
                self.assertEqual(argv[0],'-p')
                self.assertEqual(p.configuration('implement',config,env)['reasoning'],effort)

    def test_claude_without_effort_runs_as_before(self):
        argv=self.claude_argv({'agents':{'runner':'claude'}})
        self.assertNotIn('--effort',argv)
        # A setting for Codex does not reach Claude.
        argv=self.claude_argv({'agents':{'runner':'claude','providers':{'codex':{'reasoning_effort':'minimal'}}}})
        self.assertNotIn('--effort',argv)
        # Negative control: the same fixture records the flag once it is configured.
        argv=self.claude_argv({'agents':{'runner':'claude','implement':{'reasoning_effort':'low'}}})
        self.assertEqual(argv[argv.index('--effort')+1],'low')

    def test_claude_effort_survives_the_headroom_wrap(self):
        wrapper=self.bin/'headroom'
        wrapper.write_text('#!/bin/sh\n[ "$1" = wrap ] && [ "$2" = claude ] && [ "$3" = -- ] || exit 99\nshift 3\nexec claude "$@"\n')
        wrapper.chmod(0o755)
        plain=self.claude_argv({'agents':{'runner':'claude','implement':{'reasoning_effort':'high'}}})
        wrapped=self.claude_argv({'agents':{'runner':'claude','headroom_wrap':True,'implement':{'reasoning_effort':'high'}}})
        self.assertEqual(wrapped[wrapped.index('--effort')+1],'high')
        # Same argv order with and without the wrap (the session id differs per call).
        strip=lambda argv:[arg for i,arg in enumerate(argv) if not (i and argv[i-1]=='--session-id')]
        self.assertEqual(strip(wrapped),strip(plain))

    def test_claude_rejects_an_effort_only_codex_knows(self):
        (self.bin/'claude').write_text('#!/bin/sh\necho unexpected Claude >&2\nexit 99\n')
        for effort in ('none','minimal','ultra'):
            for config,env in (({'agents':{'runner':'claude','implement':{'reasoning_effort':effort}}},{}),
                               ({'agents':{'runner':'claude','providers':{'claude':{'reasoning_effort':effort}}}},{}),
                               ({'agents':{'runner':'claude'}},{'BUREAU_REASONING_IMPLEMENT':effort})):
                with self.subTest(effort=effort,config=config,env=env):
                    with self.assertRaisesRegex(ValueError,'low, medium, high, xhigh, max'):
                        p.configuration('implement',config,env)
                    self.config.write_text(json.dumps(config))
                    result=self.run_provider(code=22,**env)
                    self.assertIn('reasoning_effort for runner claude must be one of low, medium, high, xhigh, max',result.stderr)
                    self.assertNotIn('unexpected Claude',result.stderr)
                    self.assertFalse((self.root/'argv.json').exists())
                    self.assertFalse((self.root/'evidence').exists())

    def test_codex_effort_is_unchanged(self):
        for effort in ('none','minimal','low','medium','high','xhigh','max','ultra'):
            with self.subTest(effort=effort):
                self.config.write_text(json.dumps({'agents':{'runner':'codex','implement':{'reasoning_effort':effort}}}))
                self.run_provider()
                argv=json.loads((self.root/'argv.json').read_text())
                self.assertEqual(argv[argv.index('-c')+1],'model_reasoning_effort='+json.dumps(effort))
                self.assertNotIn('--effort',argv)
        self.config.write_text(json.dumps({'agents':{'runner':'codex'}}))
        self.run_provider()
        self.assertNotIn('-c',json.loads((self.root/'argv.json').read_text()))
        with self.assertRaises(ValueError): p.configuration('implement',{'agents':{'runner':'codex'}},{'BUREAU_REASONING_IMPLEMENT':'extreme'})

    def test_describe_reports_the_effort_for_both_runners(self):
        for runner,effort in (('claude','xhigh'),('codex','minimal')):
            with self.subTest(runner=runner):
                self.config.write_text(json.dumps({'agents':{'runner':runner,'implement':{'reasoning_effort':effort}}}))
                result=self.run_provider('--describe')
                described=json.loads(result.stdout)
                self.assertEqual((described['runner'],described['reasoning']),(runner,effort))

    def test_codex_only_does_not_invoke_claude_auth(self):
        (self.bin/'claude').write_text('#!/bin/sh\necho unexpected Claude >&2\nexit 99\n')
        self.run_provider()


    # ── The provider's own record of a call (v3.2) ─────────────────────────
    def evidence(self):
        runs=sorted((path for path in (self.root/'evidence').iterdir() if path.is_dir()),key=lambda path:path.stat().st_mtime)
        return runs[-1], json.loads((runs[-1]/'result.json').read_text())

    def claude_projects_dir(self,config_dir):
        # Claude Code's rule, written out independently of the adapter: the resolved working
        # directory with every character other than an ASCII letter or digit turned into '-'.
        # The Claude tests work in a directory whose name holds a space, a dot and an underscore.
        if not hasattr(self,'repo'):
            self.repo=self.root/'work tree.v3_2'; self.repo.mkdir()
        slug=''.join(c if c.isascii() and c.isalnum() else '-' for c in os.path.realpath(self.repo))
        self.assertIn('-work-tree-v3-2',slug)
        return Path(config_dir)/'projects'/slug

    def test_claude_gets_a_session_id_and_the_transcript_is_recorded(self):
        self.config.write_text('{"agents":{"runner":"claude"}}')
        folder=self.claude_projects_dir(self.env['CLAUDE_CONFIG_DIR'])
        seen=set()
        for _ in range(2):
            result=self.run_provider(TRANSCRIPT_DIR=str(folder))
            argv=json.loads((self.root/'argv.json').read_text())
            session=argv[argv.index('--session-id')+1]
            self.assertEqual(str(uuid.UUID(session)),session)
            run,metadata=self.evidence()
            self.assertEqual(metadata['session_id'],session)
            self.assertEqual(metadata['transcript'],str(folder/(session+'.jsonl')))
            self.assertTrue(metadata['transcript_found'])
            self.assertIn('Bureau provider transcript: '+metadata['transcript'],result.stderr)
            seen.add(session)
        self.assertEqual(len(seen),2,'every call gets its own session id')

    def test_claude_timeout_keeps_the_transcript_path_although_stdout_is_empty(self):
        self.config.write_text('{"agents":{"runner":"claude"}}')
        folder=self.claude_projects_dir(self.env['CLAUDE_CONFIG_DIR'])
        result=self.run_provider(TRANSCRIPT_DIR=str(folder),SLEEP='10',BUREAU_STAGE_TIMEOUT='0.5',code=124)
        run,metadata=self.evidence()
        self.assertEqual(metadata['outcome'],'timeout')
        self.assertEqual((run/'stdout.log').read_text(),'')
        self.assertTrue(metadata['transcript_found'])
        self.assertEqual(metadata['transcript'],str(folder/(metadata['session_id']+'.jsonl')))
        self.assertTrue(Path(metadata['transcript']).exists())
        self.assertIn('Bureau provider transcript: '+metadata['transcript'],result.stderr)

    def test_claude_transcript_under_home_elsewhere_or_missing(self):
        self.config.write_text('{"agents":{"runner":"claude"}}')
        env={k:v for k,v in self.env.items() if k!='CLAUDE_CONFIG_DIR'}
        home=self.root/'home'
        folder=self.claude_projects_dir(home/'.claude')
        result=subprocess.run(self.command(),capture_output=True,text=True,timeout=12,
                              env={**env,'HOME':str(home),'TRANSCRIPT_DIR':str(folder)})
        self.assertEqual(result.returncode,0,result.stderr)
        _,metadata=self.evidence()
        self.assertEqual(metadata['transcript'],str(folder/(metadata['session_id']+'.jsonl')))
        self.assertTrue(metadata['transcript_found'])
        # Where a Claude version names the project directory differently, the session id finds it.
        other=Path(self.env['CLAUDE_CONFIG_DIR'])/'projects'/'another-name'
        self.run_provider(TRANSCRIPT_DIR=str(other))
        _,metadata=self.evidence()
        self.assertEqual(metadata['transcript'],str(other/(metadata['session_id']+'.jsonl')))
        self.assertTrue(metadata['transcript_found'])
        # Not written at all: the expected path, marked as not found.
        self.run_provider()
        _,metadata=self.evidence()
        expected=self.claude_projects_dir(self.env['CLAUDE_CONFIG_DIR'])/(metadata['session_id']+'.jsonl')
        self.assertEqual(metadata['transcript'],str(expected))
        self.assertFalse(metadata['transcript_found'])

    def test_claude_invalid_result_still_records_the_transcript(self):
        self.config.write_text('{"agents":{"runner":"claude"}}')
        folder=self.claude_projects_dir(self.env['CLAUDE_CONFIG_DIR'])
        self.run_provider(TRANSCRIPT_DIR=str(folder),RAW='no envelope',code=22)
        _,metadata=self.evidence()
        self.assertEqual(metadata['outcome'],'invalid-result')
        self.assertTrue(metadata['transcript_found'])
        self.assertEqual(metadata['transcript'],str(folder/(metadata['session_id']+'.jsonl')))

    def test_codex_thread_id_and_rollout_file_are_recorded_also_on_timeout(self):
        thread='01a0f1b9-222e-7cf0-807b-497141dcd44c'
        day=Path(self.env['CODEX_HOME'])/'sessions'/'2026'/'10'/'01'; day.mkdir(parents=True)
        rollout=day/('rollout-2026-10-01T09-00-00-'+thread+'.jsonl'); rollout.write_text('{}\n')
        result=self.run_provider(THREAD=thread)
        _,metadata=self.evidence()
        self.assertEqual((metadata['session_id'],metadata['transcript'],metadata['transcript_found']),(thread,str(rollout),True))
        self.assertIn('Bureau provider transcript: '+str(rollout),result.stderr)
        self.assertNotIn('--session-id',json.loads((self.root/'argv.json').read_text()))
        self.run_provider(THREAD=thread,SLEEP='10',BUREAU_STAGE_TIMEOUT='0.5',code=124)
        _,metadata=self.evidence()
        self.assertEqual((metadata['outcome'],metadata['session_id'],metadata['transcript']),('timeout',thread,str(rollout)))

    def test_codex_without_a_usable_thread_id_records_none(self):
        for thread in (None,'../../x*','short'):
            with self.subTest(thread=thread):
                env={} if thread is None else {'THREAD':thread}
                self.run_provider(**env)
                _,metadata=self.evidence()
                self.assertIsNone(metadata['session_id']); self.assertIsNone(metadata['transcript'])
                self.assertFalse(metadata['transcript_found'])
                self.assertIn('no thread id',metadata['transcript_note'])
        # A thread id whose rollout file is not there: the id, no path.
        self.run_provider(THREAD='01a0f1b9-0000-7cf0-807b-497141dcd44c')
        _,metadata=self.evidence()
        self.assertEqual((metadata['session_id'],metadata['transcript'],metadata['transcript_found']),
                         ('01a0f1b9-0000-7cf0-807b-497141dcd44c',None,False))

    def help_calls(self):
        path=self.root/'help-calls'
        return len(path.read_text().splitlines()) if path.exists() else 0

    def test_claude_without_session_id_runs_as_before(self):
        # A Claude CLI that does not know --session-id: the call runs as in v3.1.0, without the
        # flag, and the evidence says that no session id was set and why.
        self.config.write_text('{"agents":{"runner":"claude"}}')
        for _ in range(2):
            result=self.run_provider(OLD_CLI='1')
            self.assertEqual(result.stdout.strip(),'final response')
            self.assertNotIn('--session-id',json.loads((self.root/'argv.json').read_text()))
            _,metadata=self.evidence()
            self.assertEqual((metadata['outcome'],metadata['session_id'],metadata['transcript'],metadata['transcript_found']),
                             ('complete',None,None,False))
            self.assertIn('does not take --session-id',metadata['transcript_note'])
            self.assertIn('no session id was set',metadata['transcript_note'])
            self.assertNotIn('Bureau provider transcript',result.stderr)
        self.assertEqual(self.help_calls(),1,'the answer is kept for the unchanged binary')

    def test_session_id_support_is_asked_once_per_binary(self):
        self.config.write_text('{"agents":{"runner":"claude"}}')
        for _ in range(3): self.run_provider(GH_TOKEN='ghp_probe_help_check')
        self.assertEqual(self.help_calls(),1,'one claude --help for three calls')
        self.assertNotIn('GH_TOKEN',json.loads((self.root/'help-env.json').read_text()),'the help check gets the agent environment')
        self.assertIn('--session-id',json.loads((self.root/'argv.json').read_text()))
        # An updated binary (another mtime) is asked again; so is one at another path.
        stat=(self.bin/'claude').stat(); os.utime(self.bin/'claude',ns=(stat.st_atime_ns,stat.st_mtime_ns+10**9))
        self.run_provider()
        self.assertEqual(self.help_calls(),2,'a changed binary is asked again')
        self.run_provider()
        self.assertEqual(self.help_calls(),2)

    def test_failed_help_leaves_the_flag_off_and_is_not_kept(self):
        self.config.write_text('{"agents":{"runner":"claude"}}')
        for expected_calls in (1,2):
            self.run_provider(HELP_FAIL='1')
            self.assertNotIn('--session-id',json.loads((self.root/'argv.json').read_text()))
            _,metadata=self.evidence()
            self.assertIsNone(metadata['session_id'])
            self.assertIn('claude --help failed with exit 2',metadata['transcript_note'])
            self.assertEqual(self.help_calls(),expected_calls)

    def test_claude_project_slug_matches_claude_code(self):
        # Expected values from Claude Code 2.1.286's own functions (k, tx, EQ, copied from its
        # binary and run under node): a space, a dot and an underscore; an emoji (two UTF-16
        # code units) and a non-ASCII letter; two paths over 200 characters (cut, base-36 hash).
        long_ab='/srv/'+'a'*120+'/'+'b'*120
        deep='/srv/\u00dcn\u00efc\u00f6d\u00e9/'+'deep-dir-'*30+'\U0001f680'
        for path,slug in (('/tmp/x y.z_w','-tmp-x-y-z-w'),
                          ('/srv/repo \U0001f680 gr\u00fcn','-srv-repo----gr-n'),
                          (long_ab,'-srv-'+'a'*120+'-'+'b'*74+'-ak6yfc'),
                          (deep,'-srv--n-c-d--'+('deep-dir-'*21)[:187]+'-9adu9o')):
            with self.subTest(path=path[:40]):
                self.assertEqual(p.claude_project_slug(path),slug)
                self.assertLessEqual(len(slug),200+1+7)

    def test_claude_transcript_in_an_emoji_and_a_long_directory(self):
        self.config.write_text('{"agents":{"runner":"claude"}}')
        for name in ('repo \U0001f680 gr\u00fcn','x'*120+'/'+'y'*120):
            with self.subTest(name=name[:20]):
                self.repo=self.root/name; self.repo.mkdir(parents=True)
                slug=p.claude_project_slug(os.path.realpath(self.repo))
                folder=Path(self.env['CLAUDE_CONFIG_DIR'])/'projects'/slug
                self.run_provider(TRANSCRIPT_DIR=str(folder))
                _,metadata=self.evidence()
                self.assertEqual(metadata['transcript'],str(folder/(metadata['session_id']+'.jsonl')))
                self.assertTrue(metadata['transcript_found'])

    # ── A cached answer gone stale behind an unchanged wrapper (v3.2) ──────
    def wrap_claude(self,old):
        # bin/claude is a wrapper that never changes (as a mise or asdf shim is); the CLI it
        # starts, cli/claude, is swapped between a version with --session-id and one without.
        cli=self.root/'cli'; cli.mkdir(exist_ok=True)
        lines=self.fake.split('\n',1)
        (cli/'claude').write_text(lines[0]+'\n'+("import os; os.environ['OLD_CLI']='1'\n" if old else '')+lines[1])
        (cli/'claude').chmod(0o755)
        wrapper=self.bin/'claude'
        text='#!/bin/sh\nexec "'+str(cli/'claude')+'" "$@"\n'
        if not wrapper.exists() or wrapper.read_text()!=text:
            wrapper.write_text(text); wrapper.chmod(0o755)

    def cache(self):
        return json.loads((self.root/'evidence'/'.claude-cli.json').read_text())

    def test_downgraded_cli_behind_a_wrapper_reruns_without_the_flag(self):
        self.config.write_text('{"agents":{"runner":"claude"}}')
        self.wrap_claude(old=False)
        self.run_provider()
        self.assertIn('--session-id',json.loads((self.root/'argv.json').read_text()))
        self.assertTrue(self.cache()['session_id'])
        wrapper_stat=(self.bin/'claude').stat()
        self.wrap_claude(old=True)
        self.assertEqual((self.bin/'claude').stat().st_mtime_ns,wrapper_stat.st_mtime_ns,'the wrapper did not change')
        result=self.run_provider()
        self.assertEqual(result.stdout.strip(),'final response')
        self.assertNotIn('--session-id',json.loads((self.root/'argv.json').read_text()))
        run,metadata=self.evidence()
        self.assertEqual((metadata['outcome'],metadata['session_id'],metadata['transcript_found'],metadata['session_id_rejected']),
                         ('complete',None,False,True))
        self.assertIn('rejected --session-id',metadata['transcript_note'])
        self.assertIn("error: unknown option '--session-id'",(run/'session-id-rejected.stderr.log').read_text())
        self.assertEqual((self.cache()['session_id'],self.cache()['source']),(False,'rejected by the CLI'))
        self.assertEqual(self.help_calls(),1,'the refusal is the answer; no extra --help')
        self.run_provider()
        self.assertNotIn('--session-id',json.loads((self.root/'argv.json').read_text()))
        self.assertNotIn('session_id_rejected',self.evidence()[1])
        self.assertEqual(self.help_calls(),1)

    def test_upgraded_cli_behind_a_wrapper_is_asked_again_after_a_day(self):
        self.config.write_text('{"agents":{"runner":"claude"}}')
        self.wrap_claude(old=True)
        self.run_provider()
        self.assertNotIn('--session-id',json.loads((self.root/'argv.json').read_text()))
        self.wrap_claude(old=False)
        self.run_provider()
        self.assertNotIn('--session-id',json.loads((self.root/'argv.json').read_text()),'a fresh "no" holds')
        self.assertEqual(self.help_calls(),1)
        saved=self.cache(); saved['checked']=int(time.time())-86400-1  # a day and a second ago
        (self.root/'evidence'/'.claude-cli.json').write_text(json.dumps(saved))
        self.run_provider()
        self.assertEqual(self.help_calls(),2,'a "no" older than a day is asked again')
        self.assertIn('--session-id',json.loads((self.root/'argv.json').read_text()))
        self.assertIsNotNone(self.evidence()[1]['session_id'])
        # A "no" written before the check time was kept (no "checked" field) is asked again too.
        saved=self.cache(); saved['session_id']=False; del saved['checked']
        (self.root/'evidence'/'.claude-cli.json').write_text(json.dumps(saved))
        self.run_provider()
        self.assertEqual(self.help_calls(),3)
        self.assertIn('--session-id',json.loads((self.root/'argv.json').read_text()))
        # A "no" dated in the future (a clock set back) is not trusted either.
        saved=self.cache(); saved['session_id']=False; saved['checked']=int(time.time())+10*86400
        (self.root/'evidence'/'.claude-cli.json').write_text(json.dumps(saved))
        self.run_provider()
        self.assertEqual(self.help_calls(),4)

    def test_cache_file_that_cannot_be_replaced_leaves_no_temporary_file(self):
        self.config.write_text('{"agents":{"runner":"claude"}}')
        (self.root/'evidence'/'.claude-cli.json').mkdir(parents=True)
        self.run_provider()
        self.assertIn('--session-id',json.loads((self.root/'argv.json').read_text()))
        self.assertEqual(sorted(path.name for path in (self.root/'evidence').iterdir() if path.name.startswith('.claude-cli.json.')),[])

if __name__=='__main__': unittest.main()
