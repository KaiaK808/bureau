"""Review the actual fetched PR base with real worker Git history and fake services."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import unittest

import supervision_pipeline_test as pipeline_fixture


class ReviewBaseTests(unittest.TestCase):
    def setUp(self):
        self.fixture = pipeline_fixture.SupervisionPipelineTests('test_workspace_rotation_does_not_bypass_other_preserved_work')
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.repo = self.fixture.repo
        self.root = self.fixture.root
        self.git = self.fixture.git
        self.env = {**self.fixture.env, 'BUREAU_NO_MERGE':'1', 'BUREAU_STOP_REQUESTED':'1'}
        self.env.pop('BUREAU_CALLER_STOP', None)
        self.probe = self.root / 'review-probes'; self.probe.mkdir()
        self.env['REVIEW_PROBE'] = str(self.probe)
        self.git_log = self.root / 'git-calls'
        self.env.update(REAL_GIT=shutil.which('git'), GIT_PROBE_LOG=str(self.git_log))
        wrapper = self.root / 'bin/git'
        wrapper.write_text('#!' + sys.executable + '\n' + '''import json, os, sys
with open(os.environ['GIT_PROBE_LOG'],'a') as out: out.write(json.dumps(sys.argv[1:])+'\\n')
os.execv(os.environ['REAL_GIT'],[os.environ['REAL_GIT'],*sys.argv[1:]])
''')
        wrapper.chmod(0o755)
        self.workers = 0

    def commit_file(self, branch, name, text):
        self.git('checkout','-q',branch)
        (self.repo / name).write_text(text)
        self.git('add',name); self.git('commit','-qm','test: advance '+branch)
        self.git('push','-q','origin',branch)
        return self.git('rev-parse','HEAD')

    def stacked(self):
        self.git('checkout','-qb','stack/parent','main')
        self.commit_file('stack/parent','parent.txt','Parent-only feature\n')
        self.git('checkout','-q','001-task')
        self.git('merge','--no-ff','--no-edit','stack/parent')
        self.git('push','-q','origin','001-task')
        base = self.commit_file('stack/parent','parent-later.txt','New target work\n')
        self.commit_file('main','main-only.txt','Unrelated default branch work\n')
        self.env['PR_BASE_REF'] = 'stack/parent'
        return base

    def review(self, code=20, **env):
        self.workers += 1
        worker = self.root / ('review-worker-'+str(self.workers))
        result = subprocess.run(['bash','scripts/bureau-worker.sh','T-1','code-review-pipeline.sh',str(worker),'001-task'],
            cwd=self.repo, env={**self.env,**env}, capture_output=True, text=True, timeout=40)
        self.assertEqual(result.returncode,code,result.stdout+'\n'+result.stderr)
        return worker, result

    def record(self):
        return json.loads((self.repo / '.git/bureau/review-stops.json').read_text())['T-1']

    def check(self, **env):
        detail={'identifier':'T-1','title':'Task 1','description':'Original work','labels':['lane-2','ai-implementable']}
        result=subprocess.run(['python3','scripts/bureau-supervision.py','check','T-1','--branch','001-task','--state','Build Review'],
            cwd=self.repo,env={**self.env,**env},input=json.dumps(detail),capture_output=True,text=True,timeout=10)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        return json.loads(result.stdout)

    def assert_scope(self, base, head, target):
        probes = [json.loads(path.read_text()) for path in self.probe.glob('*.json')]
        self.assertEqual(len(probes),3)
        for probe in probes:
            self.assertEqual(set(probe['files']),{'task1.txt','specs/001-task/tasks.md'})
            self.assertIn('git diff '+base+'...'+head+' --',probe['prompt'])
            self.assertIn('PR target: '+target,probe['prompt'])
            self.assertNotIn('git diff origin/main',probe['prompt'])
        record = self.record()
        self.assertEqual((record['head'],record['base'],record['base_ref']),(head,base,target))
        self.assertNotEqual(record['reviewed_head'],head)
        calls=[json.loads(line) for line in self.git_log.read_text().splitlines()]
        fetches=[args for args in calls if 'fetch' in args]
        self.assertEqual(len(fetches),2,'the pinned-base merge helper must not fetch a newer mutable base')

    def test_main_review_keeps_pinned_inputs_after_validation_merge(self):
        base=self.commit_file('main','main-later.txt','Main advanced\n')
        head=self.git('rev-parse','origin/001-task')
        worker,result=self.review()
        self.assertTrue((worker/'main-later.txt').exists())
        self.assertIn('Files changed: 2',result.stdout)
        self.assert_scope(base,head,'main')
        self.assertTrue(self.check()['stopped'])
        # Stored approvals predating base_ref retain their main interpretation.
        path=self.repo/'.git/bureau/review-stops.json'
        records=json.loads(path.read_text());records['T-1'].pop('base_ref');path.write_text(json.dumps(records))
        self.assertTrue(self.check()['stopped'])

    def test_stacked_review_excludes_parent_and_unrelated_main_changes(self):
        base=self.stacked();head=self.git('rev-parse','origin/001-task')
        worker,result=self.review(REVIEW_MOVE_REF='stack/parent',REVIEW_MOVE_SHA=self.git('rev-parse','origin/main'))
        self.assertTrue((worker/'parent-later.txt').exists())
        self.assertFalse((worker/'main-only.txt').exists())
        self.assertIn('Files changed: 2',result.stdout)
        self.assert_scope(base,head,'stack/parent')
        self.assertTrue(self.check()['stopped'])

    def test_stopped_stack_invalidates_base_changes_and_same_sha_retarget(self):
        base=self.stacked();self.review();record=self.record()
        self.commit_file('main','more-main.txt','Unrelated main advancement\n')
        self.assertTrue(self.check()['stopped'])
        self.git('branch','stack/retarget',base);self.git('push','-q','origin','stack/retarget')
        self.assertFalse(self.check(PR_BASE_REF='stack/retarget')['stopped'])
        path=self.repo/'.git/bureau/review-stops.json';path.write_text(json.dumps({'T-1':record}))
        self.commit_file('stack/parent','more-parent.txt','Target advancement\n')
        self.assertFalse(self.check()['stopped'])

    def test_invalid_or_unfetchable_base_stops_before_creative_work(self):
        head=self.git('rev-parse','origin/001-task')
        for metadata in ({}, {'headRefName':'001-task','baseRefName':None},
                         {'headRefName':'other-task','baseRefName':'main'},
                         {'headRefName':'001-task','baseRefName':'--upload-pack=bad'},
                         {'headRefName':'001-task','baseRefName':'bad:ref'},
                         {'headRefName':'001-task','baseRefName':'missing/branch'}):
            with self.subTest(metadata=metadata):
                worker,_=self.review(code=18,PR_REFS_JSON=json.dumps(metadata))
                self.assertEqual(subprocess.check_output(['git','-C',str(worker),'rev-parse','HEAD'],text=True).strip(),head)
                self.assertEqual(self.fixture.models(),[])
                self.assertFalse(list(self.probe.glob('*.json')))
                self.assertEqual(json.loads(self.fixture.linear.read_text())['T-1']['state']['id'],'build_review')
        # A tag with the requested name is not a branch and must not be fetched
        # accidentally through Git's abbreviated-ref lookup.
        self.git('tag','tag-only');self.git('push','-q','origin','refs/tags/tag-only')
        self.review(code=18,PR_BASE_REF='tag-only')
        self.assertEqual(self.fixture.models(),[])

    def test_remote_base_change_during_review_cannot_publish_approval(self):
        self.stacked()
        worker,result=self.review(code=18,REVIEW_ADVANCE_REF='stack/parent',REVIEW_MOVE_SHA=self.git('rev-parse','origin/main'))
        self.assertIn('PR head or base advanced during review',result.stdout)
        self.assertEqual(len(list(self.probe.glob('*.json'))),3)
        self.assertFalse((self.repo/'.git/bureau/review-stops.json').exists())
        self.assertEqual(json.loads(self.fixture.linear.read_text())['T-1']['state']['id'],'build_review')
        calls=[json.loads(line) for line in self.fixture.gh_log.read_text().splitlines()]
        self.assertNotIn(['pr','comment'],[args[:2] for args in calls])
        self.assertNotIn(['pr','merge'],[args[:2] for args in calls])
        self.assertTrue((worker/'parent-later.txt').exists(),'preserve the checked local validation merge')

    def test_pinned_base_helper_resolves_its_own_conflict_markers(self):
        self.git('checkout','-qb','stack/parent','main')
        base=self.commit_file('stack/parent','CLAUDE.md','Parent convention\n')
        self.commit_file('001-task','CLAUDE.md','Feature convention\n')
        result=subprocess.run(['bash','-e','-c','source scripts/bureau-config.sh; source .env; merge_origin_main_or_abort T-1 Review "$1"',
                               'test',base],cwd=self.repo,env=self.env,capture_output=True,text=True,timeout=15)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        content=(self.repo/'CLAUDE.md').read_text()
        self.assertIn('Parent convention',content);self.assertIn('Feature convention',content)
        self.assertNotIn('>>>>>>>',content)
        self.assertEqual(self.git('diff','--name-only','--diff-filter=U'),'')
        self.assertEqual(self.git('status','--porcelain'),'')


if __name__=='__main__': unittest.main()
