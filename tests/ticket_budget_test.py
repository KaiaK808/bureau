"""Ticket policy with real adapters, shell checkpoints and fake providers only."""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
import uuid

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('ticket', ROOT/'templates/scripts/bureau-ticket.py')
t = importlib.util.module_from_spec(spec); spec.loader.exec_module(t)


class Policy(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='bureau-ticket-policy-')
        self.addCleanup(self.temp.cleanup); self.root = Path(self.temp.name).resolve()
        self.now = 1000.0

    def ledger(self, budget=None, progress=None):
        return t.Ledger(self.root, 'TEAM-1', dict(agents=dict(ticket_budget=budget or {}, ticket_progress=progress or {})), lambda: self.now)

    def attempt(self, ledger, usage=None, stage='spec', phase='checker', duration=1):
        identity = dict(run_id=uuid.uuid4().hex, provider_attempt_id=uuid.uuid4().hex,
                        stage_attempt_id=uuid.uuid4().hex, stage=stage, phase=phase, issue='TEAM-1')
        self.assertIsNone(ledger.begin(identity, 'codex'))
        self.now += duration
        ledger.finish(identity['provider_attempt_id'], usage, 0)
        return identity

    def test_tokens_sum_across_stages_reruns_and_verifiers(self):
        ledger = self.ledger(dict(tokens=24))
        for stage, phase in [('spec','plan'), ('implement','implementation'), ('code_review','verifier')]:
            self.attempt(ledger, dict(input_tokens=5, output_tokens=3), stage, phase)
        stop = ledger.check()
        self.assertEqual(('budget',25,24), (stop['terminal_reason'],stop['code'],stop['totals']['tokens']))
        restarted = self.ledger(dict(tokens=24))
        self.assertEqual(3, restarted.check()['totals']['provider_calls'])
        self.assertEqual(24, restarted.check()['totals']['tokens_known'])

    def test_call_and_wall_budgets(self):
        ledger = self.ledger(dict(provider_calls=2, wall_seconds=5))
        self.attempt(ledger, dict(input_tokens=1,output_tokens=1), duration=2)
        self.assertIsNone(ledger.check())
        self.attempt(ledger, dict(input_tokens=1,output_tokens=1), duration=3)
        self.assertEqual('provider_calls',ledger.check()['dimension'])
        self.assertEqual('wall_seconds',self.ledger(dict(wall_seconds=5)).check()['dimension'])

    def test_unknown_stays_unknown_and_requires_explicit_allowance(self):
        ledger = self.ledger(dict(tokens=100))
        self.attempt(ledger)
        stop = ledger.check()
        self.assertTrue(stop['usage_unknown'])
        self.assertIsNone(stop['totals']['tokens'])
        self.assertEqual(1,stop['totals']['unknown_usage'])
        allowed = self.ledger(dict(tokens=100,allow_unknown_usage=True))
        self.assertIsNone(allowed.check())
        self.attempt(allowed)
        with allowed.guard() as value:
            self.assertIsNone(allowed.totals(value)['tokens'])
            self.assertEqual(2,allowed.totals(value)['unknown_usage'])

    def test_abandoned_attempt_is_unknown_and_live_attempt_is_pending(self):
        ledger = self.ledger(dict(tokens=100))
        identity = dict(run_id=uuid.uuid4().hex,provider_attempt_id=uuid.uuid4().hex,stage_attempt_id=uuid.uuid4().hex,
                        stage='qa',phase='checker',issue='TEAM-1')
        ledger.begin(identity,'codex')
        self.assertIsNone(ledger.check())
        with ledger.guard() as value:
            value['attempts'][identity['provider_attempt_id']]['birth'] = 'old-process'
            t.atomic(ledger.path,value)
        self.assertTrue(ledger.check()['usage_unknown'])

    def test_under_budget_and_disabled_are_unaffected(self):
        ledger = self.ledger(dict(tokens=100,provider_calls=10,wall_seconds=10))
        self.attempt(ledger,dict(input_tokens=1,output_tokens=1))
        self.assertIsNone(ledger.check())
        self.assertIsNone(self.ledger().check())

    def test_token_protocol_and_closed_reason_mapping(self):
        self.assertEqual(10,t.token_count(dict(input_tokens=5,output_tokens=3,cache_read_input_tokens=1,cache_creation_input_tokens=1),'claude'))
        self.assertIsNone(t.token_count(dict(input_tokens=5,output_tokens=3),'claude'))
        self.assertEqual(8,t.token_count(dict(input_tokens=5,output_tokens=3,cached_input_tokens=4),'codex'))
        for bad in ({},None,dict(input_tokens=True,output_tokens=3),dict(input_tokens=-1,output_tokens=3)):
            self.assertIsNone(t.token_count(bad,'codex'))
        self.assertEqual({'budget':25,'timeout':124,'watchdog':25,'review_capped':25,'human_hold':25,'ticket_withdrawn':25,'stalled':25}, t.STOP_CODES)
        for reason,code in t.STOP_CODES.items(): self.assertEqual(code,t.stop_code(reason))
        for code in (0,25,26): self.assertEqual(code,t.stop_code('ticket_withdrawn',code))
        with self.assertRaises(KeyError): t.stop_code('invented')
        with self.assertRaises(ValueError): t.stop_code('budget',124)

    def test_actual_usage_envelopes_and_multiple_codex_turns(self):
        output=self.root/'stdout.log'
        events=[dict(type='thread.started'),dict(type='turn.completed',usage=dict(input_tokens=5,output_tokens=3,cached_input_tokens=2)),
                dict(type='turn.completed',usage=dict(input_tokens=2,output_tokens=1))]
        output.write_text('\n'.join(json.dumps(v) for v in events))
        self.assertEqual(11,t.token_count(t.reported_usage('codex',self.root),'codex'))
        events[-1]['usage'].pop('output_tokens')
        output.write_text('\n'.join(json.dumps(v) for v in events))
        self.assertIsNone(t.reported_usage('codex',self.root))
        output.write_text(json.dumps(dict(type='result',usage=dict(input_tokens=5,output_tokens=3,
            cache_read_input_tokens=1,cache_creation_input_tokens=1))))
        self.assertEqual(10,t.token_count(t.reported_usage('claude',self.root),'claude'))

    def test_invalid_config_and_corrupt_ledger_fail_closed(self):
        for config in (dict(tokens=-1),dict(provider_calls=True),dict(wall_seconds=float('inf')),dict(allow_unknown_usage='yes'),dict(typo=1)):
            with self.assertRaises(ValueError): self.ledger(config)
        ledger = self.ledger(dict(tokens=100)); ledger.root.mkdir()
        ledger.path.write_text('{}')
        with self.assertRaises(ValueError): ledger.call('check')

    def test_originating_reason_survives_a_generic_hold(self):
        ledger=self.ledger()
        for reason in ('watchdog','review_capped','budget','stalled'):
            run=uuid.uuid4().hex
            ledger.note_stop(reason,25,run)
            ledger.note_stop('human_hold',25,run)
            self.assertEqual(reason,ledger.terminal(25,run))
        self.assertIsNone(ledger.terminal(20,'another-run'))
        self.assertIsNone(ledger.terminal(130,'another-run'))
        self.assertEqual('timeout',ledger.terminal(124,'another-run'))
        ledger.note_stop('ticket_withdrawn',0,'done-run')
        self.assertEqual('ticket_withdrawn',ledger.terminal(0,'done-run'))

    def test_progress_stalls_and_resets_only_for_new_trusted_facts(self):
        contract = {kind: {kind:dict(command_id='repo.test_command',command='true')} for kind in t.PROGRESS_KINDS}
        ledger = self.ledger(progress=dict(enabled=True,contract=contract))
        self.assertIsNone(ledger.end_turn('turn-1','run-1'))
        proof = self.root/'gate.log'; proof.write_text('green')
        # Activity, checkboxes, commits and provider declarations supply no gate.
        for command, code, start, end in [('different',0,'A','A'),('true',1,'A','A'),('true',0,'A','B')]:
            ledger._record_gate('run-1','turn-2','repo.test_command',command,code,start,end,proof)
        self.attempt(ledger,dict(input_tokens=1,output_tokens=1))
        stop = ledger.end_turn('turn-2','run-1')
        self.assertEqual(('stalled',25),(stop['terminal_reason'],stop['code']))
        ledger._record_gate('run-1','turn-3','repo.test_command','true',0,'A','A',proof)
        self.assertIsNone(ledger.end_turn('turn-3','run-1'))
        with ledger.guard() as value:
            self.assertEqual(0,value['non_improving_turns']); self.assertEqual(3,len(value['facts']))
        # The same gate at a new HEAD, or duplicate turn finalization, earns nothing.
        ledger._record_gate('run-2','turn-4','repo.test_command','true',0,'B','B',proof)
        self.assertIsNone(ledger.end_turn('turn-4','run-2'))
        self.assertIsNone(ledger.end_turn('turn-4','run-2'))
        self.assertEqual('stalled',ledger.end_turn('turn-5','run-2')['terminal_reason'])

    def test_progress_disabled_and_custom_turn_cap(self):
        self.assertIsNone(self.ledger().end_turn('turn','run'))
        ledger = self.ledger(progress=dict(enabled=True,max_non_improving_turns=1))
        self.assertEqual('stalled',ledger.end_turn('turn','run')['terminal_reason'])


FAKE = r'''#!/usr/bin/env python3
import json, os, sys, time
from pathlib import Path
if sys.argv[1] in ('auth','login'): print('{"loggedIn":true}'); sys.exit(0)
if sys.argv[1]=='--help': print('help'); sys.exit(0)
if sys.argv[1]=='--version': print('1.0.0'); sys.exit(0)
root=Path(os.environ['TICKET_FIXTURE'])
with (root/'events').open('a') as f: f.write('start\n')
Path('unfinished.txt').write_text('work survives the safe point\n')
time.sleep(float(os.environ.get('FAKE_DELAY','0')))
usage=json.loads(os.environ.get('FAKE_USAGE','{"input_tokens":5,"output_tokens":3,"cache_read_input_tokens":1,"cache_creation_input_tokens":1}'))
with (root/'events').open('a') as f: f.write('end\n')
if Path(sys.argv[0]).name=='codex':
    Path(sys.argv[sys.argv.index('-o')+1]).write_text('done')
    print(json.dumps(dict(type='turn.completed',usage=usage)))
else: print(json.dumps(dict(result='done',usage=usage)))
'''


class AdapterFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='bureau-ticket-adapter-')
        self.addCleanup(self.temp.cleanup); self.root = Path(self.temp.name).resolve()
        self.repo = self.root/'repo with spaces'; self.repo.mkdir()
        self.scripts = self.repo/'scripts'; shutil.copytree(ROOT/'templates/scripts',self.scripts)
        self.bin = self.root/'bin'; self.bin.mkdir()
        for name in ('codex','claude'):
            path=self.bin/name; path.write_text(FAKE.replace('#!/usr/bin/env python3','#!'+sys.executable)); path.chmod(0o755)
        subprocess.run(['git','init','-q','-b','main',str(self.repo)],check=True)
        subprocess.run(['git','-C',str(self.repo),'-c','user.name=fixture','-c','user.email=test@example.invalid',
                        'commit','--allow-empty','-qm','foundation'],check=True)
        self.env = {k:v for k,v in os.environ.items() if not k.startswith('BUREAU_')}
        self.env.update(PATH=str(self.bin)+os.pathsep+os.environ['PATH'],TICKET_FIXTURE=str(self.root),
                        BUREAU_CURRENT_ISSUE='TEAM-1',BUREAU_TICKET_CONTROL_DIR=str(self.root/'control'),
                        BUREAU_RUN_ID=uuid.uuid4().hex, PYTHONDONTWRITEBYTECODE='1',
                        CLAUDE_CONFIG_DIR=str(self.root/'claude'),CODEX_HOME=str(self.root/'codex'))
        self.config = dict(agents=dict(runner='codex'),linear=dict(teams=[dict(states=dict(build='active',done='done'))]))
        self.prompt=self.root/'prompt'; self.prompt.write_text('work')
        (self.root/'events').touch(); self.save()

    def save(self): (self.repo/'.bureau.json').write_text(json.dumps(self.config))
    def events(self): return (self.root/'events').read_text().splitlines()
    def ledger(self): return json.loads((self.repo/'.git/bureau/tickets/TEAM-1.json').read_text())
    def reset_accounting(self):
        result=subprocess.run([sys.executable,'-I',str(self.scripts/'bureau-ticket.py'),'--repo',str(self.repo),
                               '--issue','TEAM-1','reset','--acknowledge-accounting-loss'],cwd=self.repo,
                              env={k:v for k,v in self.env.items() if k not in ('BUREAU_RUN_ID','BUREAU_STAGE_ATTEMPT_ID')},
                              capture_output=True,text=True,timeout=10)
        self.assertEqual(0,result.returncode,result.stdout+result.stderr)
    def provider(self,stage='implement',phase='implementation',**env):
        return subprocess.run([sys.executable,'-I',str(self.scripts/'bureau-provider.py'),'--repo',str(self.repo),
                               '--config',str(self.repo/'.bureau.json'),'--stage',stage,'--phase',phase,'--prompt-file',str(self.prompt)],
                              env={**self.env,**env},cwd=self.repo,text=True,capture_output=True,timeout=15)

    def loop(self,count=6,**env):
        script = '''source scripts/bureau-config.sh
bureau_issue_snapshot() { printf '%s' '{"identifier":"TEAM-1","state":{"id":"active"}}'; }
for n in $(seq 1 "$1"); do
  run_stage_for implement work
done
echo published > publication
'''
        return subprocess.run(['/bin/bash','-e','-c',script,'fixture',str(count)],cwd=self.repo,
                              env={**self.env,**env},capture_output=True,text=True,timeout=40)


class Adapter(AdapterFixture):
    def test_each_budget_stops_at_post_provider_safe_point(self):
        for dimension,limit in [('tokens',8),('provider_calls',1),('wall_seconds',0.01)]:
            with self.subTest(dimension=dimension):
                path=self.repo/'.git/bureau/tickets/TEAM-1.json'
                if path.exists(): self.reset_accounting()
                shutil.rmtree(self.root/'control',ignore_errors=True); (self.root/'events').write_text('')
                self.config['agents']['ticket_budget']={dimension:limit}; self.save()
                result=self.loop(FAKE_DELAY='0.05')
                self.assertEqual(25,result.returncode,result.stdout+result.stderr)
                self.assertIn('terminal_reason=budget',result.stderr)
                self.assertEqual(['start','end'],self.events())
                self.assertTrue((self.root/'control/stop-25').exists())
                self.assertTrue((self.repo/'unfinished.txt').exists())
                self.assertFalse((self.repo/'publication').exists())

    def test_unknown_usage_stops_with_explanation(self):
        self.config['agents']['ticket_budget']=dict(tokens=100); self.save()
        result=self.loop(FAKE_USAGE='null')
        self.assertEqual(25,result.returncode,result.stdout+result.stderr)
        self.assertIn('"usage_unknown": true',result.stderr)
        self.assertIsNone(next(iter(self.ledger()['attempts'].values()))['tokens'])

    def test_cancellation_survives_accounting_write_failure(self):
        self.config['agents']['ticket_budget']=dict(tokens=100); self.save()
        wrapper='''import importlib.util, sys
spec=importlib.util.spec_from_file_location('adapter',sys.argv.pop(1))
p=importlib.util.module_from_spec(spec); spec.loader.exec_module(p)
def refuse(*args,**kwargs): raise OSError('fixture accounting unavailable')
p.t.Ledger.finish=refuse
sys.exit(p.main())
'''
        child=subprocess.Popen([sys.executable,'-c',wrapper,str(self.scripts/'bureau-provider.py'),
                                '--repo',str(self.repo),'--config',str(self.repo/'.bureau.json'),
                                '--stage','spec','--prompt-file',str(self.prompt)],cwd=self.repo,
                               env={**self.env,'FAKE_DELAY':'10'},text=True,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
        try:
            deadline=time.monotonic()+5
            while not self.events() and time.monotonic()<deadline: time.sleep(.01)
            self.assertEqual(['start'],self.events())
            child.send_signal(signal.SIGTERM)
            stdout,stderr=child.communicate(timeout=10)
            self.assertEqual(130,child.returncode,stdout+stderr)
            self.assertIn('usage remains unknown',stderr)
            self.assertIsNone(next(iter(self.ledger()['attempts'].values()))['tokens'])
        finally:
            if child.poll() is None: child.terminate()
            child.communicate(timeout=10)

    def test_closed_log_reader_preserves_optional_and_mandatory_accounting_exits(self):
        wrapper='''import importlib.util, sys
spec=importlib.util.spec_from_file_location('adapter',sys.argv.pop(1))
p=importlib.util.module_from_spec(spec); spec.loader.exec_module(p)
def refuse(*args,**kwargs): raise OSError('fixture accounting unavailable')
p.t.Ledger.begin=refuse
sys.exit(p.main())
'''
        for enabled,expected in ((False,0),(True,24)):
            with self.subTest(enabled=enabled):
                if enabled: self.config['agents']['ticket_budget']=dict(tokens=100)
                self.save()
                reader,writer=os.pipe(); os.close(reader)
                try:
                    child=subprocess.Popen([sys.executable,'-c',wrapper,str(self.scripts/'bureau-provider.py'),
                                            '--repo',str(self.repo),'--config',str(self.repo/'.bureau.json'),
                                            '--stage','spec','--prompt-file',str(self.prompt)],cwd=self.repo,
                                           env=self.env,text=True,stdout=subprocess.PIPE,stderr=writer)
                finally: os.close(writer)
                try:
                    stdout,_=child.communicate(timeout=15)
                    self.assertEqual(expected,child.returncode,stdout)
                finally:
                    if child.poll() is None: child.terminate()
                    child.communicate(timeout=10)
        self.assertEqual(['start','end'],self.events())

    def test_unknown_allowance_and_under_budget_ticket_continue(self):
        for unknown in (False,True):
            self.config['agents']['ticket_budget']=dict(tokens=100,provider_calls=10,wall_seconds=60,allow_unknown_usage=unknown); self.save()
            result=self.loop(count=2,FAKE_USAGE='null' if unknown else '{"input_tokens":5,"output_tokens":3}')
            self.assertEqual(0,result.returncode,result.stdout+result.stderr)
        self.assertEqual(4,len(self.ledger()['attempts']))
        self.assertIsNone(self.ledger()['terminal_reason'])

    def test_verifier_checker_and_rerun_calls_share_ticket_budget(self):
        self.config['agents']['ticket_budget']=dict(provider_calls=3); self.save()
        self.assertEqual(0,self.provider('spec','writer').returncode)
        self.assertEqual(0,self.provider('code_review','verifier').returncode)
        self.assertEqual(0,self.provider('qa','checker').returncode)
        result=self.provider('implement','rerun')
        self.assertEqual(25,result.returncode,result.stdout+result.stderr)
        self.assertEqual(3,len(self.ledger()['attempts']))
        self.assertCountEqual(['spec','code_review','qa'],[v['stage'] for v in self.ledger()['attempts'].values()])
        self.assertEqual(3,len({v['stage_attempt_id'] for v in self.ledger()['attempts'].values()}))

    def test_evidence_pruning_does_not_reset_ticket_allowance(self):
        self.config['agents']['ticket_budget']=dict(provider_calls=1); self.save()
        result=self.provider()
        self.assertEqual(0,result.returncode,result.stdout+result.stderr)
        shutil.rmtree(self.repo/'.git/bureau/evidence')
        self.env['BUREAU_RUN_ID']=uuid.uuid4().hex
        result=self.provider()
        self.assertEqual(25,result.returncode,result.stdout+result.stderr)
        self.assertEqual(1,len(self.ledger()['attempts']))

    def test_invalid_structured_result_still_counts_reported_usage(self):
        self.config['agents']['ticket_budget']=dict(tokens=8); self.save()
        result=subprocess.run([sys.executable,'-I',str(self.scripts/'bureau-provider.py'),'--repo',str(self.repo),
                               '--config',str(self.repo/'.bureau.json'),'--stage','code_review',
                               '--schema',str(self.scripts/'bureau-review.schema.json'),'--prompt-file',str(self.prompt)],
                              cwd=self.repo,env=self.env,capture_output=True,text=True,timeout=15)
        self.assertEqual(22,result.returncode,result.stdout+result.stderr)
        row=next(iter(self.ledger()['attempts'].values()))
        self.assertEqual((22,8),(row['normalized_exit'],row['tokens']))
        self.assertEqual(25,self.provider().returncode)

    def test_call_slots_are_reserved_across_concurrent_adapters(self):
        self.config['agents']['ticket_budget']=dict(provider_calls=1); self.save()
        command=[sys.executable,'-I',str(self.scripts/'bureau-provider.py'),'--repo',str(self.repo),
                 '--config',str(self.repo/'.bureau.json'),'--stage','code_review','--prompt-file',str(self.prompt)]
        children=[]
        try:
            for n in range(2):
                children.append(subprocess.Popen(command,cwd=self.repo,env={**self.env,'FAKE_DELAY':'.2'},
                                                 text=True,stdout=subprocess.PIPE,stderr=subprocess.PIPE))
            for child in children: child.communicate(timeout=15)
            self.assertCountEqual([0,25],[child.returncode for child in children])
            self.assertEqual(['start','end'],self.events())
            self.assertEqual(1,len(self.ledger()['attempts']))
        finally:
            for child in children:
                if child.poll() is None: child.terminate()
                child.communicate(timeout=10)

    def test_outer_gate_contract_and_stalled_checkpoint(self):
        self.config['agents']['ticket_progress']=dict(enabled=True,contract=dict(qualified_gates=dict(
            suite=dict(command_id='repo.test_command',command='true'))))
        self.config['repo']=dict(test_command='true'); self.save()
        script='''source scripts/bureau-config.sh
bureau_run_gate gate.log true repo.test_command
'''
        self.env['BUREAU_STAGE_ATTEMPT_ID']='a'*32
        # Only actual execution by the runtime qualifies the configured gate.
        result=subprocess.run(['/bin/bash','-e','-c',script],cwd=self.repo,env=self.env,capture_output=True,text=True,timeout=10)
        self.assertEqual(0,result.returncode,result.stdout+result.stderr)
        ledger=t.Ledger(self.repo/'.git/bureau','TEAM-1',self.config)
        self.assertIsNone(ledger.end_turn('turn-1',self.env['BUREAU_RUN_ID']))
        self.assertIsNone(ledger.end_turn('turn-2',self.env['BUREAU_RUN_ID']))
        ledger.end_turn('turn-3',self.env['BUREAU_RUN_ID'])
        result=self.loop(count=1)
        self.assertEqual(25,result.returncode,result.stdout+result.stderr)
        self.assertEqual([],self.events())
        self.assertEqual('stalled',self.ledger()['terminal_reason'])

    def test_pending_accounting_through_a_symlinked_entrypoint(self):
        ledger=t.Ledger(self.repo/'.git/bureau','TEAM-1',self.config)
        identity=dict(run_id=self.env['BUREAU_RUN_ID'],provider_attempt_id=uuid.uuid4().hex,
                      stage_attempt_id=uuid.uuid4().hex,stage='qa',phase='checker',issue='TEAM-1')
        ledger.begin(identity,'codex')
        entry=self.root/'linked ticket tool.py'; entry.symlink_to(self.scripts/'bureau-ticket.py')
        result=subprocess.run([sys.executable,'-I',str(entry),'--repo',str(self.repo),'--issue','TEAM-1','status'],
                              cwd=self.repo,env=self.env,capture_output=True,text=True,timeout=10)
        self.assertEqual(0,result.returncode,result.stdout+result.stderr)
        totals=json.loads(result.stdout)['totals']
        self.assertEqual(1,totals['provider_calls']); self.assertIsNone(totals['tokens'])
        self.assertEqual(0,totals['unknown_usage'])


if __name__ == '__main__': unittest.main(verbosity=2)
