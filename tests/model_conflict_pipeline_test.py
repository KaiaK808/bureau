"""Fresh review inputs and pinned rebase publication through actual workers."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import unittest

import supervision_pipeline_test as fixture
from model_conflicts_test import SCRIPTS

RESOLVER = r'''
if 'Resolve only these remaining source conflicts:' in prompt:
    pathlib.Path('tests/test_smoke.py').write_text('import unittest\nclass Smoke(unittest.TestCase):\n    def test_ok(self): self.assertEqual(5, 5)\n')
    if os.environ.get('CONFLICT_REMOTE_ADVANCE'):
        subprocess.run(['git','--git-dir',os.environ['ORIGIN_FIXTURE'],'update-ref','refs/heads/001-task',os.environ['CONFLICT_REMOTE_ADVANCE']],check=True)
'''


class ConflictPipelineTests(unittest.TestCase):
    def setUp(self):
        self.f=fixture.SupervisionPipelineTests(); self.f.setUp(); self.addCleanup(self.f.doCleanups)
        self.repo,self.root,self.git,self.env=self.f.repo,self.f.root,self.f.git,dict(self.f.env)
        # Pick a private copy for base/mutation qualification as well.
        shutil.rmtree(self.repo/'scripts'); shutil.copytree(SCRIPTS,self.repo/'scripts')
        binary=self.root/'bin/codex'; code=binary.read_text()
        code=code.replace("if 'QA' in prompt and 'tests_added' in prompt:",RESOLVER+"\nif 'QA' in prompt and 'tests_added' in prompt:")
        binary.write_text(code)
        gh=self.root/'bin/gh'; code=gh.read_text().replace("elif fields == 'url':", "elif fields == 'mergeStateStatus': print('DIRTY')\n    elif fields == 'url':")
        gh.write_text(code)
        config=json.loads((self.repo/'.bureau.json').read_text())
        config['agents'].update(conflict_resolver={'enabled':True,'max_attempts':1,'timeout_seconds':5},rebase=True)
        (self.repo/'.bureau.json').write_text(json.dumps(config))
        # Install changes are a shared base, not user commits in divergence.
        self.git('checkout','-q','main'); self.git('add','scripts'); self.git('commit','--allow-empty','-qm','test: selected runtime')
        common=self.git('rev-parse','HEAD')
        self.git('checkout','-q','001-task'); self.git('merge','--no-edit',common)
        self.git('push','-q','origin','001-task')
        test=self.repo/'tests/test_smoke.py'
        test.write_text('import unittest\nclass Smoke(unittest.TestCase):\n    def test_ok(self): self.assertEqual(2 + 3, 5)  # feature intent\n')
        self.git('add','tests/test_smoke.py'); self.git('commit','-qm','test: feature\n\nBureau-Generated: true'); self.git('push','-q','origin','001-task')
        self.old_head=self.git('rev-parse','HEAD')
        self.git('checkout','-q','main'); test.write_text('import unittest\nclass Smoke(unittest.TestCase):\n    def test_ok(self): self.assertEqual(1 + 4, 5)  # main intent\n')
        self.git('add','tests/test_smoke.py'); self.git('commit','-qm','test: main'); self.git('push','-q','origin','main')
        self.base=self.git('rev-parse','HEAD')
        self.probe=self.root/'probes'; self.probe.mkdir(); self.env['REVIEW_PROBE']=str(self.probe)
        self.worker=self.root/'worker'

    def run_worker(self,stage):
        return subprocess.run(['bash','scripts/bureau-worker.sh','T-1',stage,str(self.worker),'001-task'],
            cwd=self.repo,env=self.env,capture_output=True,text=True,timeout=60)

    def test_semantic_review_re_pins_and_cannot_reuse_old_approval(self):
        # A structurally valid old-head approval would skip the specialist calls
        # if reuse still ran before the repaired head was pinned.
        root=self.repo/'.git/bureau'; root.mkdir(exist_ok=True)
        detail={'identifier':'T-1','title':'Task 1','description':'Original work','labels':['lane-2','ai-implementable']}
        args=['python3','scripts/bureau-supervision.py','stop','T-1','--branch','001-task','--state','Build Review',
              '--head',self.old_head,'--base',self.base,'--base-ref','main','--pr','1','--verdict','APPROVE',
              '--reviewed-head',self.old_head,'--review-policy','v1:BUG:3:4']
        saved=subprocess.run(args,cwd=self.repo,env=self.env,input=json.dumps(detail),capture_output=True,text=True,timeout=10)
        self.assertEqual(saved.returncode,0,saved.stderr)
        config=json.loads((self.repo/'.bureau.json').read_text())
        config['agents'].update(merge=True,max_review_cycles=3,max_review_rounds=4)
        (self.repo/'.bureau.json').write_text(json.dumps(config))
        result=self.run_worker('code-review-pipeline.sh')
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        head=self.git('rev-parse','origin/001-task'); self.assertNotEqual(head,self.old_head)
        probes=[json.loads(p.read_text()) for p in self.probe.glob('*.json')]
        self.assertEqual(len(probes),3)
        for probe in probes:
            self.assertIn('git diff '+self.base+'...'+head+' --',probe['prompt'])
            self.assertIn('tests/test_smoke.py',probe['files'])
        self.assertEqual(json.loads((root/'review-stops.json').read_text()),{})
        self.assertEqual(json.loads(self.f.linear.read_text())['T-1']['state']['id'],'merge')
        receipts=[json.loads(p.read_text()) for p in (root/'evidence/v1/runs').glob('*/receipts/*.json')]
        self.assertEqual({r['review_inputs']['head'] for r in receipts if 'review_inputs' in r},{head})
        # The repair adds no review round of its own.
        comments=json.loads(self.f.linear.read_text())['T-1']['comments']['nodes']
        rounds=[c for c in comments if 'bureau-review-round:' in c['body']]
        self.assertEqual(len(rounds),2)
        self.assertTrue(all('round=1' in c['body'] for c in rounds))

    def prepare_rebase(self):
        items=json.loads(self.f.linear.read_text()); items['T-1']['state']={'id':'merge','name':'Merge'}
        self.f.linear.write_text(json.dumps(items))

    def test_rebase_conflict_success_publishes_pinned_lease_and_routes_fresh_review(self):
        self.prepare_rebase(); result=self.run_worker('rebase-pipeline.sh')
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        head=self.git('rev-parse','origin/001-task'); self.assertNotEqual(head,self.old_head)
        self.assertEqual(self.git('merge-base',self.base,head),self.base)
        self.assertEqual(json.loads(self.f.linear.read_text())['T-1']['state']['id'],'build_review')
        self.assertIn('fetched lease',result.stdout)

    def test_rebase_lost_lease_is_19_and_preserves_remote_writer(self):
        self.prepare_rebase()
        later=self.git('commit-tree',self.old_head+'^{tree}','-p',self.old_head,'-m','Concurrent remote human')
        self.git('push','-q','origin',later+':refs/heads/remote-writer')
        self.env['CONFLICT_REMOTE_ADVANCE']=later
        result=self.run_worker('rebase-pipeline.sh')
        self.assertEqual(result.returncode,19,result.stdout+result.stderr)
        self.assertEqual(self.git('ls-remote','origin','refs/heads/001-task').split()[0],later)
        self.assertIn('needs-human',[v['name'] for v in json.loads(self.f.linear.read_text())['T-1']['labels']['nodes']])

    def test_rebase_unresolved_is_human_17_instead_of_success(self):
        self.prepare_rebase()
        config=json.loads((self.repo/'.bureau.json').read_text());config['agents']['conflict_resolver']['enabled']=False
        (self.repo/'.bureau.json').write_text(json.dumps(config))
        result=self.run_worker('rebase-pipeline.sh')
        self.assertEqual(result.returncode,17,result.stdout+result.stderr)
        self.assertEqual(self.git('rev-parse','origin/001-task'),self.old_head)
        self.assertIn('needs-human',[v['name'] for v in json.loads(self.f.linear.read_text())['T-1']['labels']['nodes']])

    def test_rebase_failure_comment_errors_preserve_hold_and_withdrawal(self):
        config=json.loads((self.repo/'.bureau.json').read_text())
        config['agents']['conflict_resolver']['enabled']=False
        config['linear']['teams'][0]['states']['cancelled']='cancelled'
        (self.repo/'.bureau.json').write_text(json.dumps(config))
        curl=self.root/'bin/curl'
        injection=r'''
if 'commentCreate' in query and 'Rebase conflict resolution failed' in variables.get('body',''):
    with open(os.environ['COMMENT_ATTEMPTS'],'a') as out: out.write('comment\n')
    target=os.environ.get('COMMENT_WITHDRAW')
    if target:
        items['T-1']['state']={'id':target,'name':target}; path.write_text(json.dumps(items))
    mode=os.environ['COMMENT_ERROR']
    if mode.startswith('transport-'): sys.exit(int(mode.split('-')[1]))
    print('not JSON' if mode=='malformed' else json.dumps({'data':{'commentCreate':{'success':False}}}))
    sys.exit(0)
if 'issueAddLabel' in query and os.environ.get('FAIL_HUMAN_LABEL')=='1': sys.exit(27)
'''
        needle="if 'viewer' in query:"
        self.assertEqual(curl.read_text().count(needle),1)
        curl.write_text(curl.read_text().replace(needle,injection+'\n'+needle))
        self.env['BUREAU_LINEAR_RETRIES']='0'
        cases=[('success-false',False,'',17),('transport-27',False,'',17),
               ('transport-1',False,'',17),('malformed',False,'',17),
               ('transport-27',True,'',17),('success-false',False,'done',0),
               ('transport-27',False,'backlog',25),('transport-1',False,'cancelled',26)]
        actual,expected=[],[]
        for number,(mode,label_down,target,code) in enumerate(cases):
            items=json.loads(self.f.linear.read_text())
            items['T-1']['state']={'id':'merge','name':'Merge'}
            items['T-1']['labels']['nodes']=[{'name':'lane-2'},{'name':'ai-implementable'}]
            self.f.linear.write_text(json.dumps(items))
            hold=self.repo/'.git/bureau/needs-human-held/T-1'; hold.unlink(missing_ok=True)
            attempts=self.root/('comment-attempts-'+str(number))
            self.env.update(COMMENT_ERROR=mode,COMMENT_WITHDRAW=target,
                            FAIL_HUMAN_LABEL=str(int(label_down)),COMMENT_ATTEMPTS=str(attempts))
            self.worker=self.root/('comment-worker-'+str(number))
            result=self.run_worker('rebase-pipeline.sh')
            labels=json.loads(self.f.linear.read_text())['T-1']['labels']['nodes']
            actual.append((mode,label_down,target,result.returncode,
                           'needs-human' in [v['name'] for v in labels],hold.exists(),
                           attempts.read_text() if attempts.exists() else '',
                           self.git('ls-remote','origin','refs/heads/001-task').split()[0],
                           self.git('-C',str(self.worker),'status','--porcelain'),
                           self.git('-C',str(self.worker),'ls-files','-u')))
            expected.append((mode,label_down,target,code,not target and not label_down,
                             not target and label_down,'comment\n',self.old_head,'',''))
        self.assertEqual(actual,expected)

if __name__=='__main__': unittest.main()
