import importlib.util
import atexit
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
SCRIPT=Path(os.environ.get('BUREAU_TEST_PROVIDER_SCRIPT',ROOT/'templates/scripts/bureau-provider.py'))
# A constrained validation run can namespace the adapter's otherwise fixed
# /tmp directory name. Only this allocation prefix changes in the private copy.
CODEX_TMP_PREFIX = os.environ.get('BUREAU_TEST_CODEX_TMP_PREFIX', 'bureau-codex-')
if CODEX_TMP_PREFIX != 'bureau-codex-':
    isolated = tempfile.TemporaryDirectory(prefix='bureau-pr7-provider-source-')
    atexit.register(isolated.cleanup)
    shutil.copy(SCRIPT.with_name('bureau-evidence.py'), isolated.name)
    shutil.copy(SCRIPT.with_name('bureau-gates.py'), isolated.name)
    shutil.copy(SCRIPT.with_name('bureau-activity.py'), isolated.name)
    shutil.copy(SCRIPT.with_name('bureau-insights.sh'), isolated.name)
    source = SCRIPT.read_text().replace("prefix='bureau-codex-'", 'prefix=' + repr(CODEX_TMP_PREFIX))
    SCRIPT = Path(isolated.name) / SCRIPT.name; SCRIPT.write_text(source)
spec=importlib.util.spec_from_file_location('provider',SCRIPT)
p=importlib.util.module_from_spec(spec); spec.loader.exec_module(p)

CODEX_RECONNECT_ERRORS = '''{"type":"error","message":"Reconnecting... 1/5 (unexpected status 503 Service Unavailable)"}
{"type":"error","message":"Reconnecting... 2/5 (unexpected status 503 Service Unavailable)"}
{"type":"error","message":"Reconnecting... 3/5 (unexpected status 503 Service Unavailable)"}
'''
CODEX_COMPLETED = '{"type":"turn.completed","usage":{"input_tokens":12,"output_tokens":3}}\n'


class ProviderTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='bureau provider '); self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name).resolve(); self.bin=self.root/'bin'; self.bin.mkdir()
        subprocess.run(['git', '-C', str(self.root), 'init', '-q'], check=True)
        subprocess.run(['git', '-C', str(self.root), 'config', 'maintenance.auto', 'false'], check=True)
        subprocess.run(['git', '-C', str(self.root), '-c', 'user.name=Test', '-c', 'user.email=test@example.invalid',
                        'commit', '-q', '--allow-empty', '-m', 'fixture'], check=True)
        self.config=self.root/'config.json'; self.config.write_text('{"agents":{"runner":"codex"}}')
        self.prompt=self.root/'prompt.txt'; self.prompt.write_text('Review this fixture')
        fake='#!'+sys.executable+'''
import json, os, pathlib, runpy, signal, sys, time
name=pathlib.Path(sys.argv[0]).name
# Under repo.untrusted_env "clean" only CLAUDE_* names reach the CLI.
root=pathlib.Path(os.environ.get('FAKE_ROOT') or os.environ['CLAUDE_FAKE_ROOT'])
if sys.argv[1] in ('auth','login'):
    (root/(name+'-auth-env.json')).write_text(json.dumps(dict(os.environ)))
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
if name=='codex' and os.environ.get('ASSERT_STRICT_SCHEMA'):
    wire=json.loads(pathlib.Path(sys.argv[sys.argv.index('--output-schema')+1]).read_text())
    def strict(node):
        if node.get('type')=='object':
            assert node.get('additionalProperties') is False, 'wire object must be closed'
            assert set(node.get('required',[]))==set(node.get('properties',{})), 'wire optional property rejected'
            for child in node.get('properties',{}).values(): strict(child)
        if isinstance(node.get('items'),dict): strict(node['items'])
        for child in node.get('anyOf',[]): strict(child)
    strict(wire)
(root/(name+'-env.json')).write_text(json.dumps(dict(os.environ)))
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
    (folder/(sys.argv[sys.argv.index('--session-id')+1]+'.jsonl')).write_text(os.environ.get('TRANSCRIPT_CONTENT','{"type":"user"}\\n'))
if name=='codex' and os.environ.get('THREAD'):
    print(json.dumps({'type':'thread.started','thread_id':os.environ['THREAD']}),flush=True)
if os.environ.get('RECORD_SIGNALS'):
    def record(signum, frame):
        with open(root/'signals','a') as out: out.write(signal.Signals(signum).name+chr(10))
        sys.exit(128+signum)
    for name in ('SIGTERM','SIGINT','SIGHUP'): signal.signal(getattr(signal,name),record)
(root/'ready').touch()
if os.environ.get('IGNORE_TERM'): signal.signal(signal.SIGTERM, signal.SIG_IGN)
if name=='codex' and os.environ.get('GATE_COMMAND'):
    runpy.run_path(os.environ['GATE_HELPER'])['emit'](command=os.environ['GATE_COMMAND'],
        mode=os.environ.get('GATE_MODE','abort'), code=int(os.environ.get('GATE_CODE','124')))
if os.environ.get('SLEEP'): time.sleep(float(os.environ['SLEEP']))
if name=='codex' and 'CODEX_STREAM' in os.environ:
    print(os.environ['CODEX_STREAM'],end='')
if os.environ.get('FAIL'):
    print(os.environ['FAIL'],file=sys.stderr); sys.exit(1)
final=os.environ.get('FINAL','final response')
if os.environ.get('FINAL_FILE'): final=pathlib.Path(os.environ['FINAL_FILE']).read_text()
if name=='codex':
    output=sys.argv[sys.argv.index('-o')+1]
    if not os.environ.get('EMPTY'): pathlib.Path(output).write_text(final)
    if 'CODEX_STREAM' not in os.environ:
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
        self.env['GATE_HELPER']=str(ROOT/'tests/lib/codex_gate_events.py')

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
        self.assertTrue(path.name.startswith(CODEX_TMP_PREFIX))
        self.assertFalse(path.exists(),'the Codex temporary directory survived the call')
        return path

    def run_wrapped_provider(self,patch,*extra,code=0,**env):
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
        result=subprocess.run([sys.executable,'-c',source,str(SCRIPT),*self.command(*extra)[2:]],
                              capture_output=True,text=True,env={**self.env,**env},timeout=12)
        self.assertEqual(result.returncode,code,result.stdout+result.stderr)
        self.assertTrue(json.loads((self.root/'parent-env.json').read_text())['unchanged'])
        return result

    def checkpoint_records(self, expired=False):
        store=p.e.Store(self.root/'.git/bureau')
        disabled=store.root.parent.parent/'spec-checkpoints-disabled'
        if disabled.exists(): shutil.rmtree(disabled)
        records={}
        for issue in ('TEAM-1','TEAM-2','TEAM-3'):
            path=store.root/'spec-checkpoints'/(issue+'.json')
            p.e.atomic(path,dict(issue=issue,updated_at=time.time()-(31*86400 if expired else 0)))
            records[issue]=path
        return store,disabled,records

    def test_optional_checkpoint_read_failures_do_not_abort_provider(self):
        for error in ('OSError','PermissionError'):
            for issue in ('TEAM-1','TEAM-2'):
                for reuse_disabled in ('0','1'):
                    with self.subTest(error=error,issue=issue,reuse_disabled=reuse_disabled):
                        store,disabled,records=self.checkpoint_records()
                        healthy={key:path.read_bytes() for key,path in records.items() if key != issue}
                        result=self.run_wrapped_provider('''read=p.e.read
def checkpoint_read(path,*args,**kwargs):
    if path.parent.name=='spec-checkpoints' and path.stem==os.environ['FAULT_TICKET']:
        raise getattr(__import__('builtins'),os.environ['FAULT_ERROR'])('fixture checkpoint read failure')
    return read(path,*args,**kwargs)
p.e.read=checkpoint_read
''',BUREAU_CURRENT_ISSUE='TEAM-1',BUREAU_SPEC_CHECKPOINTS_DISABLED=reuse_disabled,
                            FAULT_TICKET=issue,FAULT_ERROR=error)
                        self.assertEqual(result.stdout.strip(),'final response')
                        self.assertTrue((self.root/'argv.json').is_file(),'provider never ran')
                        self.assertIn('checkpoint reuse disabled for '+issue,result.stderr)
                        self.assertEqual([path.name for path in disabled.iterdir()],[issue+'.json'])
                        self.assertEqual(json.loads((disabled/(issue+'.json')).read_text()),dict(issue=issue,reuse_disabled=True))
                        self.assertEqual(healthy,{key:records[key].read_bytes() for key in healthy})
                        self.assertEqual(self.receipts()[-1]['normalized_exit'],0)

    def test_optional_checkpoint_prune_failures_do_not_abort_provider(self):
        for error in ('OSError','PermissionError'):
            with self.subTest(error=error):
                store,disabled,records=self.checkpoint_records(expired=True)
                result=self.run_wrapped_provider('''unlink=pathlib.Path.unlink
def checkpoint_unlink(path,*args,**kwargs):
    if path.parent.name=='spec-checkpoints' and path.stem=='TEAM-2':
        raise getattr(__import__('builtins'),os.environ['FAULT_ERROR'])('fixture checkpoint prune failure')
    return unlink(path,*args,**kwargs)
pathlib.Path.unlink=checkpoint_unlink
''',BUREAU_CURRENT_ISSUE='TEAM-1',FAULT_ERROR=error)
                self.assertEqual(result.stdout.strip(),'final response')
                self.assertIn('checkpoint reuse disabled for TEAM-2',result.stderr)
                self.assertEqual([path.name for path in disabled.iterdir()],['TEAM-2.json'])
                self.assertTrue(records['TEAM-2'].exists())
                self.assertFalse(records['TEAM-3'].exists(),'a bad ticket stopped unrelated pruning')

    def test_optional_checkpoint_failure_with_closed_stderr_keeps_success(self):
        store,disabled,records=self.checkpoint_records()
        result=self.run_wrapped_provider('''read=p.e.read
def checkpoint_read(path,*args,**kwargs):
    if path.parent.name=='spec-checkpoints' and path.stem=='TEAM-2':
        raise PermissionError('private checkpoint diagnostic must not be printed')
    return read(path,*args,**kwargs)
p.e.read=checkpoint_read
reader,writer=os.pipe(); os.close(reader)
os.dup2(writer,2); os.close(writer)
''',BUREAU_CURRENT_ISSUE='TEAM-1')
        self.assertEqual(result.stdout.strip(),'final response')
        self.assertEqual([path.name for path in disabled.iterdir()],['TEAM-2.json'])
        self.assertEqual(self.receipts()[-1]['normalized_exit'],0)

    def test_optional_checkpoint_scan_and_opt_out_failures_do_not_abort_provider(self):
        for operation in ('scan','opt-out','malformed'):
            with self.subTest(operation=operation):
                store,disabled,records=self.checkpoint_records()
                if operation=='malformed': records['TEAM-2'].write_text('{broken')
                result=self.run_wrapped_provider('''glob=pathlib.Path.glob
read=p.e.read
atomic=p.e.atomic
def checkpoint_glob(path,*args,**kwargs):
    if path.name=='spec-checkpoints' and os.environ['FAULT_OPERATION']=='scan':
        raise PermissionError('fixture checkpoint scan failure')
    return glob(path,*args,**kwargs)
def checkpoint_read(path,*args,**kwargs):
    if path.parent.name=='spec-checkpoints' and path.stem=='TEAM-2' and os.environ['FAULT_OPERATION']=='opt-out':
        raise OSError('fixture checkpoint read failure')
    return read(path,*args,**kwargs)
def checkpoint_atomic(path,*args,**kwargs):
    if path.parent.name=='spec-checkpoints-disabled': raise PermissionError('fixture checkpoint opt-out failure')
    return atomic(path,*args,**kwargs)
pathlib.Path.glob=checkpoint_glob
p.e.read=checkpoint_read
if os.environ['FAULT_OPERATION']=='opt-out': p.e.atomic=checkpoint_atomic
''',BUREAU_CURRENT_ISSUE='TEAM-1',FAULT_OPERATION=operation)
                self.assertEqual(result.stdout.strip(),'final response')
                self.assertIn('WARN:',result.stderr)
                self.assertFalse((disabled/'TEAM-1.json').exists(),'unrelated failure disabled the current ticket')
                if operation=='malformed': self.assertTrue((disabled/'TEAM-2.json').exists())
                if operation=='opt-out': self.assertIn('checkpoint opt-out unavailable for TEAM-2',result.stderr)

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
        command='echo QA gate'
        self.config.write_text(json.dumps(dict(agents=dict(runner='codex'),repo=dict(test_command=command))))
        linked={**plain,'blockers':[dict(kind='gate',command=command,reason='aborted without a prefix')]}
        context=self.root/'qa-context/identity.json'
        result=self.run_provider('--stage','qa','--schema',str(schema),'--gate-context',str(context),
                                 FINAL=json.dumps(linked),GATE_COMMAND=command)
        self.assertEqual(json.loads(result.stdout),linked)
        identity=json.loads(context.read_text())
        receipt=json.loads(Path(identity['receipt']).read_text())
        self.assertEqual(receipt['stage'],'qa')
        self.assertEqual(receipt['gate_attempts'][0]['normalized_exit'],124)

    def test_gate_plan_uses_only_digests_and_rejects_foreign_receipt(self):
        command='echo gate'
        self.config.write_text(json.dumps(dict(agents=dict(runner='codex'),repo=dict(test_command=command))))
        value=self.sandbox_result('gate was killed')
        value['notes']['needs_human'][0].update(kind='gate',command=command)
        context=self.root/'context/identity.json'
        self.run_provider('--schema',str(ROOT/'templates/scripts/bureau-implement.schema.json'),'--gate-context',str(context),
                          FINAL=json.dumps(value),GATE_COMMAND=command)
        identity=json.loads(context.read_text()); receipt=json.loads(Path(identity['receipt']).read_text())
        valid=self.gate_action('plan',value,context=context)
        self.assertTrue(valid['eligible'])
        self.assertFalse(self.gate_action('plan',value,context=context,BUREAU_STAGE_ATTEMPT_ID=uuid.uuid4().hex)['eligible'])
        self.assertFalse(self.gate_action('plan',value,context=context,BUREAU_RUN_ID=uuid.uuid4().hex)['eligible'])
        self.assertFalse(self.gate_action('run',{**valid,'stage_attempt_id':uuid.uuid4().hex})['green'])
        store=p.evidence_store(self.root)
        # PR 3 retains no output copy or result prose. Removing provider
        # originals must not disable digest-only gate reconciliation.
        self.assertIsNone(receipt['structured_result'])
        shutil.rmtree(receipt['provider_logs'])
        self.assertTrue(self.gate_action('plan',value,context=context)['eligible'])
        tampered=json.loads(json.dumps(value)); tampered['prose_notes']='changed result'
        self.assertFalse(self.gate_action('plan',tampered,context=context)['eligible'])
        context.write_text(json.dumps({**identity,'stage_attempt_id':uuid.uuid4().hex}))
        self.assertFalse(self.gate_action('plan',value,context=context)['eligible'])

    def test_gate_rerun_reservation_survives_incomplete_execution(self):
        store=p.evidence_store(self.root); head=p.current_head(self.root)
        run=uuid.uuid4().hex; store.begin_run(run,None,self.root,head)
        owner=store.begin_attempt(run,None,'implement','implement',self.root,head)
        command='echo gate'; gate_id=p.g.digest(command)
        first,reservation=store.reserve_gate_rerun(run,owner,head,command,gate_id,[])
        second,same=store.reserve_gate_rerun(run,owner,head,command,gate_id,[])
        self.assertTrue(first); self.assertFalse(second)
        self.assertEqual(reservation,same); self.assertIsNone(same['result'])

    def test_optional_gate_fields_use_strict_codex_wire_schema(self):
        cases=[('implement',self.sandbox_result()),('qa',dict(status='NEEDS_HUMAN',tests_added=0,tests_failing=0,
                                                            coverage_notes='SANDBOX_GATE: denied bind'))]
        for stage,canonical in cases:
            schema=ROOT/('templates/scripts/bureau-'+stage+'.schema.json')
            wire=json.loads(json.dumps(canonical))
            if stage=='implement': wire['notes']['needs_human'][0].update(kind=None,command=None)
            else: wire['blockers']=None
            with self.subTest(stage=stage):
                result=self.run_provider('--stage',stage,'--schema',str(schema),FINAL=json.dumps(wire),ASSERT_STRICT_SCHEMA='1')
                self.assertEqual(json.loads(result.stdout),canonical)
                argv=json.loads((self.root/'argv.json').read_text())
                sent=Path(argv[argv.index('--output-schema')+1])
                self.assertNotEqual(sent,schema,'canonical optional schema was sent unchanged')
                self.assertTrue(sent.is_file())
                _,metadata=self.evidence(); receipt=json.loads(Path(metadata['receipt']).read_text())
                self.assertIsNone(receipt['structured_result'])
                self.assertEqual(receipt['result_sha256'],p.e.digest(canonical))
                self.assertEqual(receipt['schema_sha256'],p.hashlib.sha256(schema.read_bytes()).hexdigest())
                recovered=self.run_wrapped_provider("""forward=p.run
def lose_forward(*args,**kwargs):
    code,text,metadata=forward(*args,**kwargs)
    return code,'lost forwarding',metadata
p.run=lose_forward
""",'--stage',stage,'--schema',str(schema),FINAL=json.dumps(wire),ASSERT_STRICT_SCHEMA='1')
                self.assertEqual(json.loads(recovered.stdout),canonical)
                self.assertEqual(self.evidence()[1]['structured_source'],'receipt')
        schema=ROOT/'templates/scripts/bureau-qa.schema.json'
        # The strict-wire projection is necessary, not merely a local validator
        # accepting the fixtures. Removing it reproduces the native rejection.
        self.run_wrapped_provider('p.g.codex_schema=lambda value:value\n','--stage','qa','--schema',str(schema),
                                  code=22,FINAL=json.dumps(cases[1][1]),ASSERT_STRICT_SCHEMA='1')
        for bad in ({**cases[1][1],'unknown':None},{**cases[1][1],'status':None}):
            self.run_provider('--stage','qa','--schema',str(schema),code=22,FINAL=json.dumps(bad),ASSERT_STRICT_SCHEMA='1')

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
        # Same unprefixed permission blocker, now linked to actual execution
        # events. Evidence, not the denied-operation wording, lets it reach shell.
        command="printf 'run\\n' >> outside-runs; echo outside-green"
        self.config.write_text(json.dumps(dict(agents=dict(runner='codex'),repo=dict(test_command=command))))
        plain=self.sandbox_result('Operation not permitted; gate aborted')
        plain['notes']['needs_human'][0].update(kind='gate',command=command)
        context=self.root/'context/identity.json'
        result=self.run_provider('--schema',str(schema),'--gate-context',str(context),
                                 FINAL=json.dumps(plain),GATE_COMMAND=command)
        self.assertEqual(json.loads(result.stdout),plain)
        plan=self.gate_action('plan',plain,context=context)
        self.assertTrue(plan['eligible'],plan)
        first=self.gate_action('run',plan)
        second=self.gate_action('run',plan)
        self.assertTrue(first['green'],first)
        self.assertEqual(first,second)
        self.assertEqual((self.root/'outside-runs').read_text().splitlines(),['run'])
        receipt=json.loads(Path(json.loads(context.read_text())['receipt']).read_text())
        attempt=receipt['gate_attempts'][0]
        self.assertEqual(attempt['abort'],'timeout')
        self.assertEqual(attempt['normalized_exit'],124)
        self.assertEqual(attempt['head'],attempt['end_head'])
        self.assertTrue(p.evidence_store(self.root).valid_execution(attempt))
        subprocess.run(['git','-C',str(self.root),'-c','user.name=Test','-c','user.email=test@example.invalid',
                        'commit','-q','--allow-empty','-m','new HEAD'],check=True)
        self.assertFalse(self.gate_action('plan',plain,context=context)['eligible'])
        self.assertFalse(self.gate_action('run',plan)['green'])
        self.assertEqual((self.root/'outside-runs').read_text().splitlines(),['run'])

    def test_insights_compose_with_strict_gate_schema_and_recovery(self):
        values = [('implement', 'implement', self.sandbox_result('SANDBOX_GATE: configured test failed')),
                  ('qa', 'qa', dict(status='NEEDS_HUMAN', tests_added=0, tests_failing=0,
                                   coverage_notes='SANDBOX_GATE: denied bind')),
                  ('code_review', 'review', dict(verdict='APPROVE', bugs=0, security_issues=0,
                       missing_acceptance=[], fixes_needed=[], summary='', comment='', findings=[]))]
        lose_forward = '''forward=p.run
def lose_forward(*args,**kwargs):
    code,text,metadata=forward(*args,**kwargs)
    return code,'lost forwarding',metadata
p.run=lose_forward
'''
        for runner in ('codex', 'claude'):
            self.config.write_text(json.dumps({'agents':{'runner':runner}}))
            for stage, name, base in values:
                schema = ROOT/f'templates/scripts/bureau-{name}.schema.json'
                for form in ('absent', 'null', 'present', 'typed'):
                    if form == 'typed' and stage == 'code_review': continue
                    wire = json.loads(json.dumps(base))
                    if runner == 'codex' and stage == 'implement':
                        wire['notes']['needs_human'][0].update(kind=None, command=None)
                    if runner == 'codex' and stage == 'qa': wire['blockers'] = None
                    expected = json.loads(json.dumps(base))
                    if form == 'null': wire['insights'] = None
                    if form in ('present', 'typed'):
                        wire['insights'] = expected['insights'] = ['A narrow runner cap required splitting a test pass.']
                    if form == 'typed':
                        if stage == 'implement':
                            for value in (wire, expected):
                                value['notes']['needs_human'][0].update(kind='gate', command='echo configured gate')
                        else:
                            wire['blockers'] = expected['blockers'] = [dict(kind='gate', command='echo configured gate', reason='gate hold')]
                    with self.subTest(runner=runner, stage=stage, form=form):
                        args = ('--stage', stage, '--schema', str(schema))
                        result = self.run_provider(*args, FINAL=json.dumps(wire), ASSERT_STRICT_SCHEMA='1')
                        self.assertEqual(json.loads(result.stdout), expected)
                        argv = json.loads((self.root/'argv.json').read_text())
                        strict = (json.loads(Path(argv[argv.index('--output-schema')+1]).read_text())
                                  if runner == 'codex' else json.loads(argv[argv.index('--json-schema')+1]))
                        self.assertIn('insights', strict['required'])
                        field = strict['properties']['insights']
                        if runner == 'codex':
                            self.assertIn({'type':'null'}, field['anyOf'])
                            field = next(item for item in field['anyOf'] if item.get('type') == 'array')
                        self.assertEqual(field['maxItems'], 3)
                        self.assertEqual(field['items']['maxLength'], 280)
                        receipt = json.loads(Path(self.evidence()[1]['receipt']).read_text())
                        self.assertEqual(receipt['result_sha256'], p.e.digest(expected))
                        recovered = self.run_wrapped_provider(lose_forward, *args, FINAL=json.dumps(wire), ASSERT_STRICT_SCHEMA='1')
                        self.assertEqual(json.loads(recovered.stdout), expected)
                        self.assertEqual(self.evidence()[1]['structured_source'], 'receipt')

    def gate_action(self,action,value,context=None,expected_code=0,**env):
        extra=['--gate-action',action]
        if context: extra+=['--gate-context',str(context)]
        proc=subprocess.run(self.command(*extra),input=json.dumps(value),capture_output=True,text=True,
                            env={**self.env,**env},timeout=12)
        self.assertEqual(proc.returncode,expected_code,proc.stdout+proc.stderr)
        return json.loads(proc.stdout)

    def test_machine_gate_outside_red_unknown_missing_and_mixed_blockers(self):
        schema=ROOT/'templates/scripts/bureau-implement.schema.json'
        command='echo outside-red; exit 1'
        self.config.write_text(json.dumps(dict(agents=dict(runner='codex'),repo=dict(test_command=command))))
        value=self.sandbox_result('gate red')
        value['notes']['needs_human'][0].update(kind='gate',command=command)
        context=self.root/'context/identity.json'
        for mode in ('red','unknown','missing_cwd'):
            with self.subTest(mode=mode):
                self.run_provider('--schema',str(schema),'--gate-context',str(context),FINAL=json.dumps(value),
                                  GATE_COMMAND=command,GATE_MODE=mode,GATE_CODE='1')
                plan=self.gate_action('plan',value,context=context)
                self.assertEqual(plan['eligible'],mode=='red',plan)
                if plan['eligible']:
                    outside=self.gate_action('run',plan)
                    self.assertFalse(outside['green'])
                    self.assertIn('outside-red',Path(outside['logs'][0]).read_text())
        for kind in ('product','security','human'):
            mixed=json.loads(json.dumps(value))
            mixed['notes']['needs_human'].append(dict(task_id='T004',reason='independent hold',kind=kind,command=''))
            mixed['tasks_needs_human']=2
            self.run_provider('--schema',str(schema),'--gate-context',str(context),FINAL=json.dumps(mixed),GATE_COMMAND=command)
            self.assertFalse(self.gate_action('plan',mixed,context=context)['eligible'],kind)

    def test_machine_gate_timeout_is_bounded_and_reservation_cannot_retry(self):
        command='echo started >> outside-runs; sleep 2'
        self.config.write_text(json.dumps(dict(agents=dict(runner='codex'),repo=dict(test_command=command))))
        value=self.sandbox_result('gate was killed')
        value['notes']['needs_human'][0].update(kind='gate',command=command)
        context=self.root/'context/identity.json'
        self.run_provider('--schema',str(ROOT/'templates/scripts/bureau-implement.schema.json'),'--gate-context',str(context),
                          FINAL=json.dumps(value),GATE_COMMAND=command,GATE_CODE='137')
        plan=self.gate_action('plan',value,context=context)
        first=self.gate_action('run',plan,BUREAU_STAGE_TIMEOUT='0.15')
        self.assertFalse(first['green'])
        self.assertEqual(first,self.gate_action('run',plan))
        self.assertEqual((self.root/'outside-runs').read_text().splitlines(),['started'])

    def test_machine_gate_cancellation_preserves_130_and_the_log(self):
        command='echo started > outside-ready; sleep 20'
        self.config.write_text(json.dumps(dict(agents=dict(runner='codex'),repo=dict(test_command=command))))
        value=self.sandbox_result('gate aborted')
        value['notes']['needs_human'][0].update(kind='gate',command=command)
        context=self.root/'context/identity.json'
        self.run_provider('--schema',str(ROOT/'templates/scripts/bureau-implement.schema.json'),'--gate-context',str(context),
                          FINAL=json.dumps(value),GATE_COMMAND=command)
        plan=self.gate_action('plan',value,context=context)
        proc=subprocess.Popen(self.command('--gate-action','run'),stdin=subprocess.PIPE,stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE,text=True,env=self.env)
        try:
            proc.stdin.write(json.dumps(plan)); proc.stdin.close(); proc.stdin=None
            deadline=time.monotonic()+5
            while not (self.root/'outside-ready').exists() and time.monotonic()<deadline: time.sleep(.02)
            self.assertTrue((self.root/'outside-ready').exists(),'outside command never started')
            proc.send_signal(signal.SIGTERM)
            stdout,stderr=proc.communicate(timeout=5)
            self.assertEqual(proc.returncode,130,stdout+stderr)
            self.assertFalse(json.loads(stdout)['green'])
            manifest=json.loads(p.evidence_store(self.root).manifest_path(plan['run_id']).read_text())
            record=manifest['attempts'][0]['gate_reruns'][0]['result']
            self.assertEqual(record['normalized_exit'],130)
            self.assertEqual(record['abort'],'kill')
            self.assertTrue(p.evidence_store(self.root).valid_execution(record))
        finally:
            if proc.poll() is None: proc.kill(); proc.communicate()

    def prepared_gate(self, command='echo gate', prefix=False, **events):
        self.config.write_text(json.dumps(dict(agents=dict(runner='codex'),repo=dict(test_command=command))))
        value=self.sandbox_result('SANDBOX_GATE: failed configured gate' if prefix else 'gate aborted')
        if not prefix: value['notes']['needs_human'][0].update(kind='gate',command=command)
        context=self.root/'gate-context/identity.json'
        self.run_provider('--schema',str(ROOT/'templates/scripts/bureau-implement.schema.json'),
                          '--gate-context',str(context),FINAL=json.dumps(value),GATE_COMMAND=command,**events)
        return value,context

    def test_gate_evidence_store_contains_only_metadata_and_digests(self):
        command="printf 'OUTPUT_SECRET_SENTINEL\\n'; : COMMAND_SECRET_SENTINEL"
        value,context=self.prepared_gate(command,GATE_OUTPUT='TOOL_OUTPUT_SECRET_SENTINEL')
        # Force free text through the original result and receipt digest too.
        value['prose_notes']='TICKET_TEXT_SECRET_SENTINEL'
        self.run_provider('--schema',str(ROOT/'templates/scripts/bureau-implement.schema.json'),
                          '--gate-context',str(context),FINAL=json.dumps(value),GATE_COMMAND=command,
                          GATE_OUTPUT='TOOL_OUTPUT_SECRET_SENTINEL')
        plan=self.gate_action('plan',value,context=context)
        outside=self.gate_action('run',plan)
        self.assertTrue(outside['green'],outside)
        self.assertIn('OUTPUT_SECRET_SENTINEL',Path(outside['logs'][0]).read_text())
        store=p.evidence_store(self.root)
        for file in store.root.rglob('*'):
            if not file.is_file(): continue
            raw=file.read_bytes()
            for sentinel in (b'OUTPUT_SECRET_SENTINEL',b'COMMAND_SECRET_SENTINEL',b'TICKET_TEXT_SECRET_SENTINEL',
                             b'TOOL_OUTPUT_SECRET_SENTINEL',command.encode()):
                self.assertNotIn(sentinel,raw,'gate output leaked into evidence: '+str(file))
        receipt=json.loads(Path(json.loads(context.read_text())['receipt']).read_text())
        self.assertIsNone(receipt['structured_result'])
        self.assertEqual(receipt['result_sha256'],p.e.digest(value))
        self.assertEqual(receipt['gate_attempts'][0]['sha256'],p.g.digest('TOOL_OUTPUT_SECRET_SENTINEL'))
        manifest=json.loads(store.manifest_path(plan['run_id']).read_text())
        rerun=manifest['attempts'][0]['gate_reruns'][0]
        self.assertEqual(rerun['command_sha256'],p.g.digest(command))
        self.assertEqual(rerun['result']['stage_log'],outside['logs'][0])
        self.assertNotIn(str(store.root),outside['logs'][0])

    def test_shell_wrappers_and_execution_bound_cwd_fail_closed(self):
        agent=self.root/'agent/bash'; agent.parent.mkdir(); agent.write_text('#!/bin/sh\nexit 1\n');agent.chmod(0o755)
        link=agent.with_name('symlink-bash'); link.symlink_to('/bin/bash')
        for mode in ('agent_bash','agent_symlink_bash','relative_bash','env_agent_bash','borrowed_cwd','duplicate_begin','duplicate_end',
                     'reordered','ambiguous','ambiguous_duplicate','ambiguous_cwd','wrong_end','missing_cwd','env_bash','red'):
            with self.subTest(mode=mode):
                value,context=self.prepared_gate(GATE_MODE=mode)
                self.assertEqual(self.gate_action('plan',value,context=context)['eligible'],mode in ('env_bash','red'))
        for shell in ('/bin/bash','/bin/sh'):
            for option in ('-c','-lc'):
                self.assertEqual(p.g.command_text([shell,option,'echo gate']),'echo gate')
        for argv in ([str(agent),'-c','echo gate'],[str(link),'-c','echo gate'],['bash','-c','echo gate'],['/bin/bash','-c','echo gate','extra'],
                     ['/bin/bash','-x','-c','echo gate'],['/usr/bin/env','bash','-c','echo gate'],
                     ['/usr/bin/env','PATH=.:/bin','bash','-c','echo gate']):
            self.assertIsNone(p.g.command_text(argv))

    def test_prefix_requires_failed_execution_and_current_head(self):
        value,context=self.prepared_gate(prefix=True)
        self.assertTrue(self.gate_action('plan',value,context=context)['eligible'])
        self.assertFalse(self.gate_action('plan',value)['eligible'])
        for events in ({'GATE_MODE':'missing_cwd'},{'GATE_CODE':'0','GATE_MODE':'red'},{'GATE_MODE':'stale'}):
            with self.subTest(events=events):
                value,context=self.prepared_gate(prefix=True,**events)
                self.assertFalse(self.gate_action('plan',value,context=context)['eligible'])
        # A real prefix receipt with zero command executions is insufficient.
        self.run_provider('--schema',str(ROOT/'templates/scripts/bureau-implement.schema.json'),
                          '--gate-context',str(context),FINAL=json.dumps(value))
        self.assertFalse(self.gate_action('plan',value,context=context)['eligible'])

    def test_cancelled_outside_gate_survives_evidence_failures(self):
        for fault in ('write','read','validation'):
            with self.subTest(fault=fault):
                value,context=self.prepared_gate('echo cancelled >> outside-cancel-count; exit 130')
                plan=self.gate_action('plan',value,context=context)
                source='''import importlib.util, os, pathlib, sys
spec=importlib.util.spec_from_file_location('provider',sys.argv[1])
p=importlib.util.module_from_spec(spec); spec.loader.exec_module(p)
del sys.argv[1]
original=p.outside_gate
def after_execution(*args):
    result=original(*args)
    def fail(*args,**kwargs): raise OSError('injected evidence failure')
    if os.environ['FAULT']=='write': p.e.atomic=fail
    elif os.environ['FAULT']=='read': pathlib.Path.read_bytes=fail
    else: p.e.Store.valid_execution=lambda *args: False
    return result
p.outside_gate=after_execution
sys.exit(p.main())
'''
                proc=subprocess.run([sys.executable,'-c',source,str(SCRIPT),*self.command('--gate-action','run')[2:]],
                                    input=json.dumps(plan),text=True,capture_output=True,timeout=12,env={**self.env,'FAULT':fault})
                self.assertEqual(proc.returncode,130,proc.stdout+proc.stderr)
                self.assertFalse(json.loads(proc.stdout)['green'])
                self.assertTrue(json.loads(proc.stdout)['logs'])
                self.assertFalse(self.gate_action('run',plan,expected_code=130 if fault=='validation' else 0)['green'])
        self.assertEqual((self.root/'outside-cancel-count').read_text().splitlines(),['cancelled']*3)

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
        _,metadata=self.evidence()
        self.assertNotIn('recovered_errors',metadata)

    def test_codex_events_recover_reconnect_errors(self):
        stream=CODEX_RECONNECT_ERRORS+CODEX_COMPLETED
        result=self.run_provider('--stage','code_review',CODEX_STREAM=stream,FINAL='APPROVE')
        self.assertEqual(result.stdout.strip(),'APPROVE')
        evidence,metadata=self.evidence()
        self.assertEqual(metadata['outcome'],'complete')
        self.assertEqual(metadata['usage'],{'input_tokens':12,'output_tokens':3})
        self.assertEqual(metadata['recovered_errors'],{'count':3,'first_message':
                         'Reconnecting... 1/5 (unexpected status 503 Service Unavailable)'})
        self.assertIn(stream,(evidence/'stdout.log').read_text())

    def test_codex_events_reject_error_without_later_completion(self):
        stream=CODEX_RECONNECT_ERRORS.splitlines()[0]+'\n'
        result=self.run_provider(CODEX_STREAM=stream,FINAL='APPROVE',code=22)
        self.assertEqual(result.stdout,'')
        self.assertEqual(self.evidence()[1]['outcome'],'provider-error')

    def test_codex_events_reject_error_after_completion(self):
        result=self.run_provider(CODEX_STREAM=CODEX_COMPLETED+CODEX_RECONNECT_ERRORS,FINAL='APPROVE',code=22)
        self.assertEqual(result.stdout,'')
        self.assertEqual(self.evidence()[1]['outcome'],'provider-error')

    def test_codex_events_keep_turn_failed_fatal(self):
        failed='{"type":"turn.failed","error":{"message":"unexpected status 503 Service Unavailable"}}\n'
        for stream in (failed,CODEX_COMPLETED+failed,failed+CODEX_COMPLETED,
                       CODEX_RECONNECT_ERRORS+failed+CODEX_COMPLETED):
            with self.subTest(stream=stream):
                result=self.run_provider(CODEX_STREAM=stream,FINAL='APPROVE',code=22)
                self.assertEqual(result.stdout,'')
                self.assertEqual(self.evidence()[1]['outcome'],'provider-error')

    def test_codex_events_keep_last_completed_usage(self):
        stream=(CODEX_RECONNECT_ERRORS+CODEX_COMPLETED+CODEX_RECONNECT_ERRORS
                +'{"type":"turn.completed","usage":{"input_tokens":97,"output_tokens":11}}\n')
        result=self.run_provider(CODEX_STREAM=stream,FINAL='APPROVE',BUREAU_COST_TRACKING='true')
        value=json.loads(result.stdout)
        self.assertEqual(value['result'],'APPROVE')
        self.assertEqual(value['usage'],{'input_tokens':97,'output_tokens':11})
        self.assertIsNone(value['total_cost_usd'])
        metadata=self.evidence()[1]
        self.assertEqual(metadata['usage'],value['usage'])
        self.assertEqual(metadata['recovered_errors']['count'],6)

    def test_codex_events_truncate_recovered_error_message(self):
        message='Reconnecting... 1/5 (unexpected status 503 Service Unavailable): '+('detail '*80)
        stream=json.dumps({'type':'error','message':message})+'\n'+CODEX_COMPLETED
        self.run_provider(CODEX_STREAM=stream)
        self.assertEqual(self.evidence()[1]['recovered_errors'],{'count':1,'first_message':message[:200]})

    def test_codex_events_still_require_final_message(self):
        result=self.run_provider(CODEX_STREAM=CODEX_RECONNECT_ERRORS+CODEX_COMPLETED,EMPTY='1',code=22)
        self.assertEqual(result.stdout,'')
        metadata=self.evidence()[1]
        self.assertEqual(metadata['outcome'],'invalid-result')
        self.assertEqual(metadata['error'],'provider returned no final message')

    def test_codex_events_still_validate_schema(self):
        schema=ROOT/'templates/scripts/bureau-qa.schema.json'
        stream=CODEX_RECONNECT_ERRORS+CODEX_COMPLETED
        value={'status':'GREEN','tests_added':0,'tests_failing':0,'coverage_notes':'ok'}
        result=self.run_provider('--schema',str(schema),CODEX_STREAM=stream,FINAL=json.dumps(value))
        self.assertEqual(json.loads(result.stdout),value)
        for bad in ('not json','{"status":"GREEN"}',json.dumps({**value,'status':'APPROVE'})):
            with self.subTest(final=bad):
                result=self.run_provider('--schema',str(schema),CODEX_STREAM=stream,FINAL=bad,code=22)
                self.assertEqual(result.stdout,'')
                self.assertEqual(self.evidence()[1]['outcome'],'invalid-result')

    def test_codex_events_keep_process_failure_classification(self):
        stream=CODEX_RECONNECT_ERRORS+CODEX_COMPLETED
        for message,code,outcome in (('quota exceeded',23,'quota'),('Permission denied by sandbox',24,'environment'),
                                     ('backend failed',22,'provider-error'),('Not logged in',16,'auth')):
            with self.subTest(message=message):
                result=self.run_provider(CODEX_STREAM=stream,FAIL=message,code=code)
                self.assertEqual(result.stdout,'')
                evidence,metadata=self.evidence()
                self.assertEqual(metadata['outcome'],outcome)
                self.assertEqual(metadata['provider_exit'],1)
                self.assertIn(stream,(evidence/'stdout.log').read_text())

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

    def complete_implement_result(self):
        return {'status':'COMPLETE','tasks_done':3,'tasks_skipped':0,'tasks_needs_human':0,
                'fixed_review_items':[],'notes':{'needs_human':[],'skipped':[],'deviations':[]},
                'prose_notes':'Implementation and gate complete'}

    def structured_transcript_env(self,*values):
        self.config.write_text('{"agents":{"runner":"claude"}}')
        folder=self.claude_projects_dir(self.env['CLAUDE_CONFIG_DIR'])
        # Claude JSONL: assistant messages contain tool_use blocks, whose input is the result.
        events=[{'type':'user','message':{'role':'user','content':'Implement the fixture'}}]
        for index,value in enumerate(values):
            events.append({'type':'assistant','message':{'role':'assistant','content':[
                {'type':'tool_use','id':'toolu_'+str(index),'name':'StructuredOutput','input':value}]}})
        events.append({'type':'assistant','message':{'role':'assistant','content':[
            {'type':'text','text':'The scheduled wakeup arrived after completion.'}]}})
        return {'TRANSCRIPT_DIR':str(folder),'TRANSCRIPT_CONTENT':'\n'.join(json.dumps(event) for event in events)+'\n'}

    def test_claude_recovers_the_last_structured_output_from_transcript(self):
        schema=ROOT/'templates/scripts/bureau-implement.schema.json'
        latest=self.complete_implement_result()
        earlier={**latest,'status':'PARTIAL','tasks_done':1}
        env=self.structured_transcript_env(earlier,latest)
        # Non-object structured_output values need the same fallback as an absent field.
        for fields in ({},{'structured_output':None},{'structured_output':[]},{'structured_output':'lost'}):
            with self.subTest(fields=fields):
                envelope={'result':'All work is already finished.','usage':{'input_tokens':12},'total_cost_usd':0.01,**fields}
                result=self.run_provider('--schema',str(schema),RAW=json.dumps(envelope),**env)
                self.assertEqual(json.loads(result.stdout),latest)
                _,metadata=self.evidence()
                self.assertEqual((metadata['outcome'],metadata['structured_source']),('complete','transcript'))
                self.assertTrue(metadata['transcript_found'])
                self.assertEqual((metadata['usage'],metadata['total_cost_usd']),({'input_tokens':12},0.01))

    def test_claude_validates_the_last_transcript_result_without_using_an_earlier_one(self):
        schema=ROOT/'templates/scripts/bureau-implement.schema.json'
        earlier=self.complete_implement_result()
        for latest in ({'status':'COMPLETE'},{**earlier,'status':'UNKNOWN'},
                       {**earlier,'tasks_done':'three'},{**earlier,'unexpected':True},None,[], 'invalid'):
            with self.subTest(latest=latest):
                env=self.structured_transcript_env(earlier,latest)
                # Even valid JSON in the prose result cannot supersede the last tool call.
                result=self.run_provider('--schema',str(schema),FINAL=json.dumps(earlier),code=22,**env)
                self.assertEqual(result.stdout,'')
                _,metadata=self.evidence()
                self.assertEqual(metadata['outcome'],'invalid-result')

    def test_claude_without_a_transcript_or_structured_call_still_returns_22(self):
        schema=ROOT/'templates/scripts/bureau-implement.schema.json'
        env=self.structured_transcript_env()
        for transcript_env in ({},env):
            with self.subTest(transcript=bool(transcript_env)):
                result=self.run_provider('--schema',str(schema),FINAL='All work is already finished.',code=22,**transcript_env)
                self.assertEqual(result.stdout,'')
                self.assertIn('result type must be object',result.stderr)
                _,metadata=self.evidence()
                self.assertEqual(metadata['transcript_found'],bool(transcript_env))

    def test_claude_structured_envelope_does_not_read_the_transcript(self):
        schema=ROOT/'templates/scripts/bureau-implement.schema.json'
        value=self.complete_implement_result()
        env=self.structured_transcript_env(value)
        patch='''original_open=p.Path.open
def refuse_transcript_read(path,mode='r',*args,**kwargs):
    if path.suffix=='.jsonl' and 'r' in mode: raise AssertionError('the envelope must not read its transcript')
    return original_open(path,mode,*args,**kwargs)
p.Path.open=refuse_transcript_read
'''
        # A dict in the envelope always takes precedence, even when it fails validation.
        for envelope_value,code in ((value,0),({'status':'COMPLETE'},22)):
            with self.subTest(envelope_value=envelope_value):
                result=self.run_wrapped_provider(patch,'--schema',str(schema),code=code,
                                                 RAW=json.dumps({'structured_output':envelope_value}),**env)
                if code==0:
                    self.assertEqual(json.loads(result.stdout),value)
                    _,metadata=self.evidence()
                    self.assertNotIn('structured_source',metadata)

    def test_claude_without_a_schema_keeps_the_prose_result(self):
        env=self.structured_transcript_env(self.complete_implement_result())
        result=self.run_provider(FINAL='All work is already finished.',**env)
        self.assertEqual(result.stdout.strip(),'All work is already finished.')
        _,metadata=self.evidence()
        self.assertNotIn('structured_source',metadata)

    def test_schema_validates_actual_final_and_rejects_invalid(self):
        schema=ROOT/'templates/scripts/bureau-qa.schema.json'
        value={'status':'GREEN','tests_added':0,'tests_failing':0,'coverage_notes':'ok'}
        result=self.run_provider('--schema',str(schema),FINAL=json.dumps(value))
        self.assertEqual(json.loads(result.stdout),value)
        for observations in ([], ['The runner cap required splitting a test pass.']):
            enriched = {**value, 'insights': observations}
            result = self.run_provider('--stage', 'qa', '--schema', str(schema), FINAL=json.dumps(enriched), BUREAU_CURRENT_ISSUE='TEAM-12')
            self.assertEqual(json.loads(result.stdout), enriched)
            argv = json.loads((self.root/'argv.json').read_text())
            strict = json.loads(Path(argv[argv.index('--output-schema')+1]).read_text())
            self.assertIn('insights', strict['required'])
        # A Codex optional null is a valid wire omission; other malformed
        # observations still warn and drop without failing the stage.
        result = self.run_provider('--schema', str(schema), FINAL=json.dumps({**value, 'insights':None}))
        self.assertEqual(json.loads(result.stdout), value)
        for malformed in (True, 'wrong', {}, [42], ['x'*281], ['x']*4, ['line\nbreak']):
            result = self.run_provider('--schema', str(schema), FINAL=json.dumps({**value, 'insights':malformed}))
            self.assertEqual(json.loads(result.stdout), value)
            self.assertIn('malformed optional insights dropped', result.stderr)
        rows = [json.loads(line) for line in (self.root/'.git/bureau/evidence/v1/insights.jsonl').read_text().splitlines()]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['issue'], 'TEAM-12')
        for bad in ('not json','{"status":"GREEN"}',json.dumps({**value,'status':'APPROVE'})):
            result=self.run_provider('--schema',str(schema),FINAL=bad,code=22)
            self.assertEqual(result.stdout,'')

    def test_insights_content_filter_preserves_provider_success(self):
        schema = ROOT/'templates/scripts/bureau-qa.schema.json'
        value = dict(status='GREEN', tests_added=0, tests_failing=0, coverage_notes='ok')
        safe = 'The runner cap required splitting a test pass.'
        canaries = (
            'LINEAR_API_KEY=private-canary', 'ghp_private-canary', '/Users/private-canary/project', '[INFO] private-canary output',
            '"linear_api_key" = "x"', "'password': 'x'", '"password":"x"', "'password' = 'x'",
            '{"linear_api_key": "x"}', 'Config used "session-cookie" : "x"',
            'abcdefgh password=x',
            'aBcD1234eFgH5678 iJkL9012mNoP3456',
            '"aBcD1234" "eFgH5678" "iJkL9012" "mNoP3456"',
            "'aBcD1234eFgH5678' 'iJkL9012mNoP3456'",
            'abcdefghijklmnop qrstuvwxyzabcdef', '"ABCDEFGHIJKLMNOP" "QRSTUVWXYZABCDEF"',
            '"INFO: CANARY_OUTPUT"', 'Tool returned INFO: CANARY_OUTPUT',
            'Tool returned WARN CANARY_OUTPUT', 'Tool returned ERROR: CANARY_OUTPUT',
            'Tool returned DEBUG CANARY_OUTPUT', 'Tool returned TRACE: CANARY_OUTPUT',
            'Tool returned WARNING: CANARY_OUTPUT', 'Tool returned FATAL CANARY_OUTPUT',
            'Tool returned Traceback (most recent call last)', 'Tool returned stderr CANARY_OUTPUT',
            'Tool returned exit code 1', 'File "worker.py", line 7, in collect',
            'Tool returned File "worker.py", line 7, in collect',
            'at collect (src/worker.js:12:4)', 'at src/worker.js:12:4',
            '$ echo CANARY_OUTPUT', '> echo CANARY_OUTPUT', '  $ echo CANARY_OUTPUT',
        )
        for runner in ('codex', 'claude'):
            for canary in canaries:
                with self.subTest(runner=runner, canary=canary):
                    result = self.run_provider('--stage', 'qa', '--schema', str(schema), BUREAU_RUNNER_QA=runner,
                                               FINAL=json.dumps({**value, 'insights':[canary, safe]}))
                    self.assertEqual(json.loads(result.stdout), {**value, 'insights':[safe]})
                    self.assertIn('unsafe optional observations dropped', result.stderr)
                    self.assertNotIn(canary, result.stderr)
        rows = [json.loads(line) for line in (self.root/'.git/bureau/evidence/v1/insights.jsonl').read_text().splitlines()]
        self.assertEqual(len(rows), 2 * len(canaries))
        self.assertEqual({r['observation'] for r in rows}, {safe})
        receipts = list((self.root/'.git/bureau/evidence/v1/runs').glob('*/receipts/*.json'))
        self.assertEqual(len(receipts), 2 * len(canaries))
        for receipt in receipts:
            self.assertEqual(json.loads(receipt.read_text())['normalized_exit'], 0)

    def test_insights_preserve_prose_skip_and_all_strict_stage_contracts(self):
        for text in ('SKIP', 'Review prose only.'):
            self.assertEqual(self.run_provider(FINAL=text).stdout.strip(), text)
        path = self.root/'.git/bureau/evidence/v1/insights.jsonl'
        self.assertFalse(path.exists())
        goal = {'status':'COMPLETE','insights':['The runner cap required a second goal turn.']}
        self.run_provider(FINAL='Goal complete.\n```json\n'+json.dumps(goal)+'\n```', BUREAU_CURRENT_ISSUE='TEAM-12')
        self.assertEqual(json.loads(path.read_text())['observation'], goal['insights'][0])
        for stage, name, value in (
                ('implement', 'implement', {**self.complete_implement_result(), 'insights':[]}),
                ('qa', 'qa', dict(status='GREEN',tests_added=0,tests_failing=0,coverage_notes='',insights=[])),
                ('code_review', 'review', dict(verdict='APPROVE',bugs=0,security_issues=0,missing_acceptance=[],fixes_needed=[],summary='',comment='',findings=[],insights=[]))):
            schema = ROOT/f'templates/scripts/bureau-{name}.schema.json'
            for runner in ('codex', 'claude'):
                self.config.write_text(json.dumps({'agents':{'runner':runner}}))
                result = self.run_provider('--stage',stage,'--schema',str(schema),FINAL=json.dumps(value))
                self.assertEqual(json.loads(result.stdout), value)
                argv=json.loads((self.root/'argv.json').read_text())
                strict=json.loads(argv[argv.index('--json-schema')+1]) if runner=='claude' else json.loads(Path(argv[argv.index('--output-schema')+1]).read_text())
                self.assertIn('insights',strict['required'])

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

    def test_claude_disallows_deferred_turns_except_for_the_tool_free_summary(self):
        wrapper=self.bin/'headroom'
        wrapper.write_text('#!/bin/sh\n[ "$1" = wrap ] && [ "$2" = claude ] && [ "$3" = -- ] || exit 99\nshift 3\nexec claude "$@"\n')
        wrapper.chmod(0o755)
        system=self.root/'system.txt'; system.write_text('Fixture system context')
        for wrapped in (False,True):
            for stage in ('implement','upstream_summary'):
                with self.subTest(wrapped=wrapped,stage=stage):
                    self.config.write_text(json.dumps({'agents':{'runner':'claude','headroom_wrap':wrapped,
                        'providers':{'claude':{'model':'claude-fixture','reasoning_effort':'high'}}}}))
                    self.run_provider('--stage',stage,'--system-file',str(system))
                    argv=json.loads((self.root/'argv.json').read_text())
                    evidence,metadata=self.evidence()
                    tools=['--tools',''] if stage=='upstream_summary' else [
                        '--dangerously-skip-permissions','--disallowedTools',
                        'ScheduleWakeup,CronCreate,CronDelete,RemoteTrigger,Monitor']
                    self.assertEqual(argv,['-p','--output-format','json','--session-id',metadata['session_id'],
                        *tools,'--append-system-prompt-file',str(evidence/'system.txt'),
                        '--effort','high','--model','claude-fixture'])

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

    # ── A Claude configuration directory per installation ─────────────────
    def recorded_env(self,name):
        return json.loads((self.root/name).read_text())

    def claude_envs(self,config,script=SCRIPT,**env):
        # The environment of the claude child and of its login check, for one call.
        self.config.write_text(json.dumps(config))
        for name in ('claude-env.json','claude-auth-env.json'): (self.root/name).unlink(missing_ok=True)
        result=subprocess.run([sys.executable,str(script),*self.command()[2:]],capture_output=True,text=True,
                              env={**self.env,**env},timeout=12)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        return self.recorded_env('claude-env.json'),self.recorded_env('claude-auth-env.json')

    def profile(self,name):
        path=self.root/'profiles'/name; path.mkdir(parents=True,exist_ok=True)
        return path

    def test_claude_config_dir_reaches_the_child_and_the_login_check(self):
        lean,other=self.profile('lean'),self.profile('other')
        keyed={'agents':{'runner':'claude','providers':{'claude':{'config_dir':str(lean)}}}}
        home=self.root/'home'; tilde=home/'profiles'/'tilde lean'; tilde.mkdir(parents=True)
        for config,env,expected in ((keyed,{},lean),
                                    # The environment override wins over the key.
                                    (keyed,{'BUREAU_CLAUDE_CONFIG_DIR':str(other)},other),
                                    ({'agents':{'runner':'claude'}},{'BUREAU_CLAUDE_CONFIG_DIR':str(other)},other),
                                    # A leading ~/ is the home directory.
                                    ({'agents':{'runner':'claude','providers':{'claude':{'config_dir':'~/profiles/tilde lean'}}}},{'HOME':str(home)},tilde),
                                    ({'agents':{'runner':'claude'}},{'HOME':str(home),'BUREAU_CLAUDE_CONFIG_DIR':'~/profiles/tilde lean'},tilde)):
            with self.subTest(config=config,env=env):
                child,login=self.claude_envs(config,**env)
                self.assertEqual(child['CLAUDE_CONFIG_DIR'],str(expected))
                self.assertEqual(login['CLAUDE_CONFIG_DIR'],str(expected))
                # Only that variable differs from the environment the child got before.
                self.assertEqual({k:v for k,v in child.items() if k!='CLAUDE_CONFIG_DIR'},
                                 {k:v for k,v in p.untrusted_env({**self.env,**env},'default','claude').items() if k!='CLAUDE_CONFIG_DIR'})
                self.assertEqual(p.configuration('implement',config,{**self.env,**env})['config_dir'],str(expected))
                # Claude writes its transcript under that directory, and result.json names it there.
                _,metadata=self.evidence()
                self.assertTrue(metadata['transcript'].startswith(str(expected/'projects')+os.sep),metadata['transcript'])

    def test_claude_config_dir_also_reaches_a_clean_environment_and_the_headroom_wrap(self):
        lean=self.profile('lean')
        wrapper=self.bin/'headroom'
        wrapper.write_text('#!/bin/sh\n[ "$1" = wrap ] && [ "$2" = claude ] && [ "$3" = -- ] || exit 99\nshift 3\nexec claude "$@"\n')
        wrapper.chmod(0o755)
        for config in ({'agents':{'runner':'claude','providers':{'claude':{'config_dir':str(lean)}}},'repo':{'untrusted_env':'clean'}},
                       {'agents':{'runner':'claude','headroom_wrap':True,'providers':{'claude':{'config_dir':str(lean)}}}}):
            with self.subTest(config=config):
                child,login=self.claude_envs(config,CLAUDE_FAKE_ROOT=str(self.root))
                self.assertEqual((child['CLAUDE_CONFIG_DIR'],login['CLAUDE_CONFIG_DIR']),(str(lean),str(lean)))
                if 'repo' in config: self.assertNotIn('FAKE_ROOT',child)

    def without_config_dir(self):
        # The adapter without this change: claude_env leaves the environment as it is.
        source=SCRIPT.read_text()
        changed="        return {**(os.environ if env is None else env), 'CLAUDE_CONFIG_DIR': options['config_dir']}\n"
        self.assertEqual(source.count(changed),1,'the control no longer matches the adapter')
        control=self.root/'control'/'bureau-provider.py'; control.parent.mkdir(exist_ok=True)
        shutil.copy(SCRIPT.with_name('bureau-evidence.py'), control.parent / 'bureau-evidence.py')
        shutil.copy(SCRIPT.with_name('bureau-gates.py'), control.parent / 'bureau-gates.py')
        shutil.copy(SCRIPT.with_name('bureau-activity.py'), control.parent / 'bureau-activity.py')
        control.write_text(source.replace(changed,'        return env\n'))
        return control

    def test_claude_without_a_config_dir_gets_the_environment_it_got_before(self):
        control=self.without_config_dir()
        config={'agents':{'runner':'claude','providers':{'claude':{'model':'claude-test'}}}}
        child,login=self.claude_envs(config)
        before_child,before_login=self.claude_envs(config,script=control)
        self.assertEqual(child,before_child); self.assertEqual(login,before_login)
        # The inherited CLAUDE_CONFIG_DIR stays; without one, none is added.
        self.assertEqual(child['CLAUDE_CONFIG_DIR'],self.env['CLAUDE_CONFIG_DIR'])
        self.env.pop('CLAUDE_CONFIG_DIR')
        child,login=self.claude_envs(config)
        self.assertNotIn('CLAUDE_CONFIG_DIR',child); self.assertNotIn('CLAUDE_CONFIG_DIR',login)
        self.assertEqual(child,p.untrusted_env({**self.env,'FAKE_ROOT':self.env['FAKE_ROOT']},'default','claude'))
        # An empty override is no override, as for the other BUREAU_ overrides.
        child,_=self.claude_envs(config,BUREAU_CLAUDE_CONFIG_DIR='')
        self.assertNotIn('CLAUDE_CONFIG_DIR',child)
        self.assertIsNone(p.configuration('implement',config,{})['config_dir'])

    def test_negative_control_the_env_assertion_fails_without_the_change(self):
        from mutation_checks import caught_mutation
        lean=self.profile('lean')
        config={'agents':{'runner':'claude','providers':{'claude':{'config_dir':str(lean)}}}}
        child,login=self.claude_envs(config)
        self.assertEqual((child['CLAUDE_CONFIG_DIR'],login['CLAUDE_CONFIG_DIR']),(str(lean),str(lean)))
        script=self.without_config_dir()
        with caught_mutation():
            child,login=self.claude_envs(config,script=script)
            self.assertEqual((child['CLAUDE_CONFIG_DIR'],login['CLAUDE_CONFIG_DIR']),(str(lean),str(lean)))

    def test_invalid_claude_config_dir_is_a_configuration_error_before_any_call(self):
        (self.bin/'claude').write_text('#!/bin/sh\necho unexpected Claude >&2\ntouch "$FAKE_ROOT/claude-ran"\nexit 99\n')
        afile=self.root/'not a directory'; afile.write_text('')
        home=self.root/'home'; home.mkdir()
        cases=[(value,{}) for value in ('profiles/lean','./lean','~','~other/lean','',str(self.root/'missing'),str(afile),'~/missing',42,True,[],{})]
        cases+=[(None,{'BUREAU_CLAUDE_CONFIG_DIR':value}) for value in ('profiles/lean','~',str(self.root/'missing'),str(afile))]
        # The override is checked even when the key is valid.
        cases.append((str(self.profile('lean')),{'BUREAU_CLAUDE_CONFIG_DIR':'relative'}))
        for value,env in cases:
            with self.subTest(value=value,env=env):
                provider={} if value is None else {'config_dir':value}
                config={'agents':{'runner':'claude','providers':{'claude':provider}}}
                name='BUREAU_CLAUDE_CONFIG_DIR' if env else 'agents.providers.claude.config_dir'
                with self.assertRaisesRegex(ValueError,name): p.configuration('implement',config,{'HOME':str(home),**env})
                self.config.write_text(json.dumps(config))
                for extra in ((),('--check',),('--describe',)):
                    result=self.run_provider(*extra,code=22,HOME=str(home),**env)
                    self.assertIn('Bureau provider: '+name,result.stderr)
                    self.assertNotIn('unexpected Claude',result.stderr)
                self.assertFalse((self.root/'claude-ran').exists())
                self.assertFalse((self.root/'argv.json').exists())
                self.assertFalse((self.root/'evidence').exists())

    def test_codex_is_unaffected_by_the_claude_config_dir(self):
        (self.bin/'claude').write_text('#!/bin/sh\necho unexpected Claude >&2\nexit 99\n')
        lean=self.profile('lean')
        # A TMPDIR of its own, so the per-call Codex temporary directory does not differ between runs.
        tmp=self.root/'tmp'; tmp.mkdir(); self.env['TMPDIR']=str(tmp)
        baseline={'agents':{'runner':'codex'}}
        self.config.write_text(json.dumps(baseline)); self.run_provider()
        before=self.recorded_env('codex-env.json')
        for provider,env in (({'config_dir':str(lean)},{}),({'config_dir':'relative'},{}),({'config_dir':42},{}),
                             ({},{'BUREAU_CLAUDE_CONFIG_DIR':str(lean)}),({},{'BUREAU_CLAUDE_CONFIG_DIR':'relative'})):
            with self.subTest(provider=provider,env=env):
                config={'agents':{'runner':'codex','providers':{'claude':provider}}}
                self.config.write_text(json.dumps(config))
                self.run_provider(**env)
                after=self.recorded_env('codex-env.json')
                self.assertEqual(after.get('CLAUDE_CONFIG_DIR'),self.env['CLAUDE_CONFIG_DIR'])
                self.assertEqual({k:v for k,v in after.items() if k not in env},{k:v for k,v in before.items() if k not in env})
                self.assertNotIn('config_dir',json.loads(self.run_provider('--describe',**env).stdout))
                self.assertNotIn('config_dir',p.configuration('implement',config,env))

    def test_describe_reports_the_claude_config_dir(self):
        lean,other=self.profile('lean'),self.profile('other')
        self.config.write_text(json.dumps({'agents':{'runner':'claude','providers':{'claude':{'config_dir':str(lean)+'/'}}}}))
        self.assertEqual(json.loads(self.run_provider('--describe').stdout)['config_dir'],str(lean))
        self.assertEqual(json.loads(self.run_provider('--describe',BUREAU_CLAUDE_CONFIG_DIR=str(other)).stdout)['config_dir'],str(other))
        self.config.write_text(json.dumps({'agents':{'runner':'claude'}}))
        self.assertIsNone(json.loads(self.run_provider('--describe').stdout)['config_dir'])


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

    def receipt_schema(self):
        path=self.root/'receipt.schema.json'
        path.write_text(json.dumps({'type':'object','required':['status'],
                                    'properties':{'status':{'enum':['COMPLETE']}}}))
        return path

    def receipts(self):
        return [json.loads(path.read_text()) for path in (self.root/'.git/bureau/evidence/v1/runs').glob('*/receipts/*.json')]

    def test_entire_store_contains_only_metadata_and_digests(self):
        schema=self.receipt_schema()
        sentinels=['PRIVATE_DESCRIPTION_SENTINEL', 'lin_api_abcdefPRIVATE_TOKEN_SENTINEL',
                   'ghp_1234567890ABCDEF_PRIVATE_TOKEN_SENTINEL', 'short']
        payload={'status':'COMPLETE','description':' '.join(sentinels),'summary':' '.join(sentinels)}
        payload_file=self.root/'private-provider-result.json';payload_file.write_text(json.dumps(payload))
        self.run_provider('--schema',str(schema),FINAL_FILE=str(payload_file),BUREAU_CURRENT_ISSUE='TEAM-1',
                          LINEAR_API_KEY=sentinels[1],TELEGRAM_BOT_TOKEN=sentinels[3],
                          TOKEN_CARRIER='prefix-'+sentinels[1]+'-suffix')
        receipt=self.receipts()[0]
        store=p.e.Store(self.root/'.git/bureau'); run=receipt['run_id']; attempt=receipt['stage_attempt_id']
        source=self.root/'review-artifact.txt'; source.write_text(' '.join(sentinels))
        command='printf '+json.dumps(' '.join(sentinels))
        head=p.current_head(self.root)
        for kind in ('gate','artifact'):
            store.gate(run,attempt,source,head,head,command,1,kind)
        # Wait diagnostics and PR URL query strings cannot smuggle text either.
        store.open_wait('TEAM-1','approval','decision',' '.join(sentinels),' '.join(sentinels))
        store.set_pr(run,attempt,{'number':12,'url':'https://example.invalid/pull/12?token='+sentinels[1]})
        files=[path for path in store.root.rglob('*') if path.is_file()]
        self.assertGreater(len(files),3)
        for path in files:
            raw=path.read_bytes()
            for sentinel in sentinels: self.assertNotIn(sentinel.encode(),raw,'PRIVACY_SENTINEL in '+str(path))
        self.assertEqual(receipt['result_sha256'],p.e.digest(payload))
        self.assertIsNone(receipt['structured_result'])
        self.assertFalse(list(store.root.rglob('*.log')))

    def test_optional_evidence_failures_do_not_stop_provider_or_change_error_exit(self):
        schema=self.receipt_schema()
        result=self.run_wrapped_provider('''
def unavailable(*args,**kwargs): raise OSError('injected optional evidence failure')
p.e.Store.begin_run=unavailable
p.e.Store.begin_attempt=unavailable
p.e.Store.end_attempt=unavailable
p.e.Store._link_receipt=unavailable
''','--schema',str(schema),FINAL='{"status":"COMPLETE"}')
        self.assertEqual(json.loads(result.stdout),{'status':'COMPLETE'})
        self.assertEqual(len(self.receipts()),1)
        self.assertEqual(result.stderr.count('bureau evidence: WARN:'),1)
        result=self.run_wrapped_provider('''
def unavailable(*args,**kwargs): raise OSError('injected optional evidence failure')
p.e.Store.begin_run=unavailable
p.e.Store.begin_attempt=unavailable
p.e.Store.put_receipt=unavailable
p.e.Store.end_attempt=unavailable
''','--schema',str(schema),code=124,SLEEP='2',BUREAU_STAGE_TIMEOUT='.05')
        self.assertEqual(result.stdout,''); self.assertEqual(result.stderr.count('bureau evidence: WARN:'),1)

    def test_evidence_identity_lookup_failures_preserve_provider_outcomes(self):
        schema=self.receipt_schema()
        for when in ('start', 'end', 'store'):
            for code, env in ((0, {}), (16, {'FAIL':'Not logged in'}), (22, {'FAIL':'provider failure'}),
                              (23, {'FAIL':'quota exceeded'}), (24, {'FAIL':'Permission denied by sandbox'}),
                              (124, {'SLEEP':'2', 'BUREAU_STAGE_TIMEOUT':'.05'})):
                with self.subTest(when=when, code=code):
                    result=self.run_wrapped_provider('''
original_head=p.current_head
head_calls=0
def unavailable_head(repo):
    global head_calls
    head_calls+=1
    if head_calls == (1 if os.environ['FAIL_WHEN']=='start' else 2):
        raise OSError('injected evidence HEAD lookup failure')
    return original_head(repo)
def unavailable_store(repo): raise OSError('injected evidence store lookup failure')
if os.environ['FAIL_WHEN']=='store': p.evidence_store=unavailable_store
else: p.current_head=unavailable_head
''','--schema',str(schema),code=22 if when=='store' and code==0 else code,
                        FINAL='{"status":"COMPLETE"}',FAIL_WHEN=when,**env)
                    self.assertEqual(bool(result.stdout),code==0 and when!='store')
                    self.assertEqual(result.stderr.count('bureau evidence: WARN:'),1)
                    if when=='store' and code==0: self.assertIn('completion receipt store unavailable',result.stderr)
                    if code==124: self.assertIn('Bureau provider outcome: timeout',result.stderr)

    def test_unestablished_capacity_never_forwards_success(self):
        schema=self.receipt_schema()
        result=self.run_wrapped_provider('''
def unavailable(*args,**kwargs): raise RuntimeError('injected pruning failure')
p.e.Store._prune=unavailable
''','--schema',str(schema),code=22,FINAL='{"status":"COMPLETE"}')
        self.assertEqual(result.stdout,'')
        self.assertEqual(self.receipts(),[])

    def test_receipt_recovery_requires_original_result_with_matching_digest(self):
        schema=self.receipt_schema()
        for replacement in ('deleted','{"status":"COMPLETE","different":true}'):
            result=self.run_wrapped_provider('''
recorded=p.run
def unavailable_original(*args,**kwargs):
    code,text,metadata=recorded(*args,**kwargs)
    directory=pathlib.Path(json.loads(pathlib.Path(metadata['receipt']).read_text())['provider_logs'])
    if os.environ['REPLACEMENT']=='deleted': (directory/'final.txt').unlink()
    else: (directory/'final.txt').write_text(os.environ['REPLACEMENT'])
    return code,'lost forwarding',metadata
p.run=unavailable_original
''','--schema',str(schema),code=22,FINAL='{"status":"COMPLETE"}',REPLACEMENT=replacement)
            self.assertEqual(result.stdout,''); self.assertIn('no matching completion receipt',result.stderr)

    def test_completion_receipt_is_durable_before_stdout(self):
        schema=self.receipt_schema()
        result=self.run_wrapped_provider('''
emit=print
def verified_print(value, *args, **kwargs):
    if isinstance(value,str) and 'COMPLETE' in value:
        receipts=list((root/'.git/bureau/evidence/v1/runs').glob('*/receipts/*.json'))
        assert len(receipts)==1, 'result forwarded before receipt'
        receipt=json.loads(receipts[0].read_text())
        assert receipt['structured_result'] is None
        assert receipt['result_sha256']==p.e.digest({'status':'COMPLETE'})
        assert receipt['normalized_exit']==0
        (root/'before-stdout').write_text(str(receipts[0]))
    return emit(value,*args,**kwargs)
p.print=verified_print
''','--schema',str(schema),'--phase','iteration-1',FINAL='{"status":"COMPLETE"}',BUREAU_CURRENT_ISSUE='TEAM-1')
        self.assertEqual(json.loads(result.stdout),{'status':'COMPLETE'})
        path=Path((self.root/'before-stdout').read_text())
        receipt=json.loads(path.read_text())
        self.assertEqual(receipt['schema_version'],1)
        self.assertEqual(receipt['phase'],'iteration-1')
        self.assertEqual(receipt['start_head'],receipt['end_head'])
        self.assertEqual(receipt['provider_exit'],0)
        self.assertEqual(path.stat().st_mode & 0o777,0o600)
        self.assertTrue(all(pth.stat().st_mode & 0o777 == 0o700 for pth in path.parents if pth.name in ('v1','receipts','runs')))

    def test_stopped_stdout_consumer_keeps_completion_receipt(self):
        schema=self.receipt_schema()
        proc=subprocess.Popen(self.command('--schema',str(schema)),stdout=subprocess.PIPE,stderr=subprocess.PIPE,
                              env={**self.env,'FINAL':'{"status":"COMPLETE"}','SLEEP':'.1'})
        proc.stdout.close(); proc.stdout=None
        _,stderr=proc.communicate(timeout=8)
        self.assertEqual(proc.returncode,0,stderr.decode())
        receipts=self.receipts(); self.assertEqual(len(receipts),1)
        self.assertIsNone(receipts[0]['structured_result'])
        self.assertEqual(receipts[0]['result_sha256'],p.e.digest({'status':'COMPLETE'}))
        self.assertEqual(receipts[0]['normalized_exit'],0)

    def test_parallel_tickets_keep_distinct_completion_receipts(self):
        schema=self.receipt_schema()
        children=[]
        for issue in ('TEAM-1','TEAM-2'):
            payload=json.dumps({'status':'COMPLETE','ticket':issue})
            proc=subprocess.Popen(self.command('--schema',str(schema)),stdout=subprocess.PIPE,stderr=subprocess.PIPE,
                                  env={**self.env,'FINAL':payload,'BUREAU_CURRENT_ISSUE':issue,'SLEEP':'.1'})
            children.append((issue,proc))
        for issue,proc in children:
            out,err=proc.communicate(timeout=8)
            self.assertEqual(proc.returncode,0,err.decode())
            self.assertEqual(json.loads(out)['ticket'],issue)
        receipts=self.receipts(); self.assertEqual(len(receipts),2)
        self.assertEqual(len({v['run_id'] for v in receipts}),2)
        self.assertEqual(len({v['provider_attempt_id'] for v in receipts}),2)
        for receipt in receipts:
            self.assertIsNone(receipt['structured_result'])
            self.assertEqual(receipt['result_sha256'],p.e.digest({'status':'COMPLETE','ticket':receipt['issue']}))

    def test_prose_or_missing_forwarded_result_recovers_only_own_receipt(self):
        schema=self.receipt_schema()
        for lost,cost in [('',False),('A late wakeup answered in prose',False),('',True),('Late prose',True)]:
            with self.subTest(lost=lost,cost=cost):
                result=self.run_wrapped_provider('''
recorded=p.run
def lost_result(*args,**kwargs):
    code,text,metadata=recorded(*args,**kwargs)
    return code,os.environ['LOST_RESULT'],metadata
p.run=lost_result
''','--schema',str(schema),FINAL='{"status":"COMPLETE"}',LOST_RESULT=lost,BUREAU_COST_TRACKING='1' if cost else '0')
                value=json.loads(result.stdout)
                if cost:
                    self.assertEqual(value['provider'],'codex'); value=json.loads(value['result'])
                self.assertEqual(value,{'status':'COMPLETE'})
        self.assertEqual(len(self.receipts()),4)

    def test_foreign_receipt_and_stale_head_never_reconcile(self):
        schema=self.receipt_schema()
        for field,foreign in [('issue','TEAM-2'),('provider_attempt_id','f'*32),('run_id','a'*32),
                              ('stage','qa'),('phase','other-reviewer'),('end_head','0'*40)]:
            with self.subTest(field=field):
                result=self.run_wrapped_provider('''
recorded=p.run
def foreign_receipt(*args,**kwargs):
    code,text,metadata=recorded(*args,**kwargs)
    path=pathlib.Path(metadata['receipt']); value=json.loads(path.read_text())
    value[os.environ['FOREIGN_FIELD']]=os.environ['FOREIGN_VALUE']
    p.e.atomic(path,value)
    return code,'late prose',metadata
p.run=foreign_receipt
''','--schema',str(schema),code=22,FINAL='{"status":"COMPLETE"}',BUREAU_CURRENT_ISSUE='TEAM-1',
                    FOREIGN_FIELD=field,FOREIGN_VALUE=foreign)
                self.assertEqual(result.stdout,'')
                self.assertIn('no matching completion receipt',result.stderr)

    def test_foreign_attempt_in_another_path_is_never_used(self):
        schema=self.receipt_schema()
        self.run_wrapped_provider('''
recorded=p.run
def moved_receipt(*args,**kwargs):
    code,text,metadata=recorded(*args,**kwargs)
    path=pathlib.Path(metadata['receipt']); value=json.loads(path.read_text())
    value['provider_attempt_id']='f'*32; value['issue']='TEAM-2'
    p.e.atomic(path.with_name('f'*32+'.json'),value); path.unlink()
    return code,'late prose',metadata
p.run=moved_receipt
''','--schema',str(schema),code=22,FINAL='{"status":"COMPLETE"}',BUREAU_CURRENT_ISSUE='TEAM-1')

    def test_receipt_write_failure_never_forwards_success(self):
        schema=self.receipt_schema()
        result=self.run_wrapped_provider('''
def denied(*args,**kwargs): raise OSError('receipt disk failure')
p.e.Store.put_receipt=denied
''','--schema',str(schema),code=22,FINAL='{"status":"COMPLETE"}')
        self.assertEqual(result.stdout,''); self.assertIn('receipt disk failure',result.stderr)

    def test_commits_and_prior_receipts_do_not_turn_timeout_or_provider_error_into_success(self):
        schema=self.receipt_schema()
        self.run_provider('--schema',str(schema),FINAL='{"status":"COMPLETE"}')
        before=p.current_head(self.root)
        # Work can be committed before a provider fails; the outcome is still an error.
        subprocess.run(['git','-C',str(self.root),'-c','user.name=Test','-c','user.email=test@example.invalid',
                        'commit','-q','--allow-empty','-m','work completed'],check=True)
        self.assertNotEqual(before,p.current_head(self.root))
        for code,env in [(124,{'SLEEP':'2','BUREAU_STAGE_TIMEOUT':'.05'}),(22,{'FAIL':'provider failed'})]:
            result=self.run_provider('--schema',str(schema),code=code,**env)
            self.assertEqual(result.stdout,'')
            receipt=max(self.receipts(),key=lambda v:v['started_at'])
            self.assertEqual(receipt['normalized_exit'],code); self.assertIsNone(receipt['structured_result'])

if __name__=='__main__': unittest.main()
