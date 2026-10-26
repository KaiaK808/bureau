"""Conflict repair and PR 9 withdrawal through real workers/Git/checkpoints."""
import hashlib
import json
import os
from pathlib import Path
import shlex
import shutil
import sys
import time
import unittest

from ticket_kill_switch_test import CURL, GIT, ROOT, STATES, WithdrawalFixture


STEP = r'''
import json, os, pathlib, sys
root = pathlib.Path(os.environ['PR9_FIXTURE'])
step = sys.argv[1]
with (root/'events').open('a') as f:
    f.write(json.dumps(dict(kind='resolution-step', step=step))+'\n')
control = json.loads((root/'control').read_text())
assert not any(name in os.environ for name in ('LINEAR_API_KEY', 'API_KEY'))
if step == 'dependencies': pathlib.Path('node_modules').mkdir()
elif step.startswith('generator-'): pathlib.Path(step+'.txt').write_text('resolved\n')
if control.get('withdraw_at_step') == step:
    (root/'state').write_text(control['target'])
with (root/'events').open('a') as f:
    f.write(json.dumps(dict(kind='resolution-finished', step=step))+'\n')
if control.get('fail_at_step') == step: sys.exit(3)
'''

GIT_WITH_WITHDRAWAL = '''
control=json.loads((root/'control').read_text())
point = 'fetch' if command == ['fetch','origin','--quiet'] else 'merge' if command[:1] == ['merge'] and '--no-ff' in command else ''
if point and control.get('withdraw_at_git') == point:
    with (root/'events').open('a') as f: f.write(json.dumps(dict(kind='withdrawal', point=point))+'\\n')
    (root/'state').write_text(control['target'])
'''

CHECK_AUTHORIZATION = r'''
if config.strip() not in ('header = "Authorization: fixture-key"',
                           'header = "Authorization: fixture-environment-only-key"'):
    print('{"errors":[{"message":"Unauthenticated"}]}',end='')
    if '-w' in args: print(args[args.index('-w')+1].replace('%{http_code}','401').replace(r'\n','\n'),end='')
    sys.exit(0)
'''


class ConflictWithdrawal(WithdrawalFixture):
    def setUp(self):
        super().setUp()
        scripts = Path(os.environ.get('BUREAU_CONFLICT_TEST_SCRIPTS', ROOT/'templates/scripts'))
        shutil.rmtree(self.repo/'scripts'); shutil.copytree(scripts, self.repo/'scripts')
        wrapper = GIT.replace("os.execv(os.environ['PR9_REAL_GIT']", GIT_WITH_WITHDRAWAL+"\nos.execv(os.environ['PR9_REAL_GIT']")
        (self.bin/'git').write_text(wrapper)
        (self.bin/'curl').write_text(CURL.replace('payload=json.loads',CHECK_AUTHORIZATION+'\npayload=json.loads'))
        npm = self.bin/'npm'
        npm.write_text('#!'+sys.executable+'\n'+STEP.replace('step = sys.argv[1]', "step = 'dependencies'"))
        npm.chmod(0o755)
        self.git('checkout','-q','main')
        with (self.repo/'.gitignore').open('a') as f: f.write('node_modules/\n.nm.tmp/\n')
        for name in ('generator-1.txt','generator-2.txt'): (self.repo/name).write_text('base\n')
        (self.repo/'package.json').write_text('{"name":"fixture","version":"1.0.0","private":true}\n')
        (self.repo/'package-lock.json').write_text('{"lockfileVersion":3}\n')
        self.git('add','.'); self.git('commit','-qm','conflict foundation')
        self.git('checkout','-q',self.branch); self.git('merge','--no-edit','main')
        for name in ('generator-1.txt','generator-2.txt'): (self.repo/name).write_text('ours\n')
        self.git('commit','-qam','feature outputs'); self.git('push','-q','origin',self.branch)
        self.issue_head = self.git('rev-parse','HEAD')
        self.git('checkout','-q','main')
        for name in ('generator-1.txt','generator-2.txt'): (self.repo/name).write_text('theirs\n')
        self.git('commit','-qam','main outputs'); self.git('push','-q','origin','main')
        self.config['repo'].update(test_command=shlex.join([sys.executable,'-c',STEP,'test']),
            conflict_resolution=dict(generated=[dict(globs=[name+'.txt'],command=[sys.executable,'-c',STEP,name])
                                                for name in ('generator-1','generator-2')]))
        self.save_config()

    def start_case(self, name, **control):
        if self.wt.exists(): self.git('-C',str(self.wt),'checkout','--detach','-q')
        self.wt = self.repo/'.worktrees'/name
        self.state('build'); (self.root/'events').write_text('')
        (self.root/'labels').write_text('["lane-2"]')
        (self.root/'fail-reads').unlink(missing_ok=True)
        self.control(**control)

    def steps(self): return [e['step'] for e in self.events() if e['kind']=='resolution-step']

    def assert_stopped(self, result, key, code, steps):
        self.assertEqual(code,result.returncode,result.stdout+'\n'+result.stderr)
        self.assertEqual(steps,self.steps(),self.events())
        self.assertEqual(steps,[e['step'] for e in self.events() if e['kind']=='resolution-finished'])
        self.assert_no_publication(self.events())
        self.assertFalse([e for e in self.events() if e['kind'] in ('label','move','pass')],self.events())
        self.assertEqual(STATES[key],(self.root/'state').read_text())
        self.assertEqual(self.issue_head,self.git('-C',str(self.wt),'rev-parse','HEAD'))
        self.assertEqual('',self.git('-C',str(self.wt),'status','--porcelain'))
        self.assertEqual('',self.git('-C',str(self.wt),'ls-files','-u'))
        for name in ('generator-1.txt','generator-2.txt'):
            self.assertEqual('ours\n',(self.wt/name).read_text())
        self.assertFalse((self.wt/'node_modules').exists())
        self.assertFalse((self.wt/'.nm.tmp').exists())
        self.assert_released()
        worker_key = hashlib.sha256(str(self.wt.resolve()).encode()).hexdigest()
        # Rollback left no unfinished edits or unpublished commits: PR 9 keeps
        # disposable registration and releases the branch for this clean worker.
        self.assertTrue((self.common/'bureau/workers'/worker_key).exists())
        self.assertFalse((self.common/'bureau/preserved'/(worker_key+'.json')).exists())
        self.assertEqual('',self.git('-C',str(self.wt),'branch','--show-current'))
        self.assertFalse((self.common/'bureau/needs-human-held'/'EXP-9').exists())
        self.assertTrue(list((self.common/'bureau/ticket-control').glob('*/stop-'+str(code))))
        self.git('-C',str(self.wt),'checkout','--detach','-q')

    def test_withdrawal_before_transaction(self):
        for key,code in [('done',0),('backlog',25),('cancelled',26)]:
            with self.subTest(state=key):
                self.start_case('fetch-'+key,withdraw_at_git='fetch',target=STATES[key])
                result=self.worker()
                self.assert_stopped(result,key,code,[])
                self.assertNotIn('Branch is behind',result.stdout)

    def test_withdrawal_before_dependencies_and_between_commands(self):
        cases=[('merge',[]),('dependencies',['dependencies']),
               ('generator-1',['dependencies','generator-1']),
               ('generator-2',['dependencies','generator-1','generator-2']),
               ('test',['dependencies','generator-1','generator-2','test'])]
        for point,steps in cases:
            for key,code in [('done',0),('backlog',25),('cancelled',26)]:
                with self.subTest(point=point,state=key):
                    control={'withdraw_at_git' if point=='merge' else 'withdraw_at_step':point,'target':STATES[key]}
                    self.start_case(point+'-'+key,**control)
                    self.assert_stopped(self.worker(),key,code,steps)

    def test_throttled_failed_generator_respects_withdrawal(self):
        for key,code in [('done',0),('backlog',25),('cancelled',26)]:
            with self.subTest(state=key):
                self.start_case('failure-'+key,withdraw_at_step='generator-1',fail_at_step='generator-1',target=STATES[key])
                (self.root/'alerts').write_text('merge-conflict|EXP-9\t'+str(int(time.time()))+'\n')
                result=self.worker()
                self.assertEqual(code,result.returncode,result.stdout+'\n'+result.stderr)
                self.assertEqual(['dependencies','generator-1'],self.steps())
                self.assert_stopped(result,key,code,['dependencies','generator-1'])

    def test_environment_only_key_stops_after_generator_one(self):
        (self.repo/'.env').unlink()
        self.env.update(LINEAR_API_KEY='fixture-environment-only-key')
        self.env.pop('API_KEY',None)
        for key,code in [('done',0),('backlog',25),('cancelled',26)]:
            with self.subTest(state=key):
                self.start_case('environment-'+key,withdraw_at_step='generator-1',target=STATES[key])
                self.assert_stopped(self.worker(),key,code,['dependencies','generator-1'])

    def test_unauthenticated_checkpoint_declines_repair(self):
        (self.repo/'.env').unlink()
        for name in ('LINEAR_API_KEY','API_KEY'): self.env.pop(name,None)
        self.env['BUREAU_TICKET_CONTROL_DIR']=str(self.root/'checkpoint-control')
        self.git('checkout','-q',self.branch)
        result=self.run_cmd('bash','-euo','pipefail','-c','''source scripts/bureau-config.sh
_throttle_should_suppress() { return 0; }
merge_origin_main_or_abort EXP-9 Test || exit 17
''')
        self.assertEqual(17,result.returncode,result.stdout+'\n'+result.stderr)
        self.assertEqual([],self.steps(),self.events())
        self.assert_no_publication(self.events())
        self.assertIn('Conflict resolution declined:',result.stderr)
        self.assertEqual(self.issue_head,self.git('rev-parse','HEAD'))
        self.assertEqual('',self.git('status','--porcelain'))
        self.assertEqual('',self.git('ls-files','-u'))
        self.assertFalse((self.repo/'.git/MERGE_HEAD').exists())
        for name in ('generator-1.txt','generator-2.txt'):
            self.assertEqual('ours\n',(self.repo/name).read_text())
        self.assertFalse((self.repo/'node_modules').exists())
        self.assertFalse((self.repo/'.nm.tmp').exists())

    def test_active_failed_generator_remains_17_with_needs_human(self):
        for throttled in (False,True):
            with self.subTest(throttled=throttled):
                self.start_case('active-'+str(throttled),fail_at_step='generator-1')
                (self.root/'alerts').write_text('merge-conflict|EXP-9\t'+str(int(time.time()))+'\n' if throttled else '')
                result=self.worker()
                self.assertEqual(17,result.returncode,result.stdout+'\n'+result.stderr)
                self.assertEqual(['dependencies','generator-1'],self.steps())
                self.assertIn(dict(kind='label',name='needs-human',add=True),self.events())
                self.assertEqual(0 if throttled else 1,len([e for e in self.events() if e['kind']=='comment']))
                self.assertEqual(self.issue_head,self.git('-C',str(self.wt),'rev-parse','HEAD'))
                self.assertEqual('',self.git('-C',str(self.wt),'status','--porcelain'))
                self.assertEqual(STATES['build'],(self.root/'state').read_text()); self.assert_released()
                self.git('-C',str(self.wt),'checkout','--detach','-q')
                (self.root/'labels').write_text('["lane-2"]')


if __name__=='__main__': unittest.main(verbosity=2)
