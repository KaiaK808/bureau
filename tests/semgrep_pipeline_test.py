"""Actual review/merge stages with fake providers, scanner and isolation probe."""
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import review_merge_test as review
import semgrep_test as fixture

ROOT = Path(__file__).resolve().parents[1]


def normalize(text, root):
    text = text.replace(str(root), '<fixture>')
    text = re.sub(r'\b[a-f0-9]{40,64}\b', '<commit>', text)
    text = re.sub(r'\b[a-f0-9]{32}\b', '<id>', text)
    return text


def run_group(argv, **kwargs):
    timeout = kwargs.pop('timeout', 90)
    kwargs.pop('capture_output', None)
    process = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               start_new_session=True, **kwargs)
    try:
        stdout, stderr = process.communicate(timeout=timeout)
        return subprocess.CompletedProcess(argv, process.returncode, stdout, stderr)
    finally:
        if process.poll() is None:
            try: os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError: pass
            try: process.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                try: os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError: pass
                process.communicate()
        else:
            try: os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError: pass


class ReviewPipelineTests(unittest.TestCase):
    def setUp(self):
        self.f = review.PipelineTests(); self.f.setUp(); self.addCleanup(self.f.doCleanups)
        self.repo, self.root = self.f.repo, self.f.root
        self.installation = fixture.fake_install(self.root, self.repo/'scripts')
        fixture.inject_isolation(self.repo/'scripts', self.installation)
        self.f.f.env['BUREAU_SEMGREP_VENV'] = str(self.installation['venv'])
        self.f.config['review'] = dict(semgrep=dict(enabled=True))
        for role in ('correctness','security','performance'):
            self.f.values[role+'_late'] = review.specialist(role)
        self.f.f.git('checkout','-q','001-task')
        (self.repo/'new.js').write_text('eval(secretValue);\n')
        self.f.f.git('add','new.js'); self.f.f.git('commit','-qm','test: scanner input')
        self.f.f.git('push','-q','origin','001-task'); self.f.f.git('checkout','-q','main')

    def run_review(self, code):
        original = subprocess.run
        def run(argv, **kwargs):
            if argv[:2] == ['bash', 'scripts/bureau-worker.sh']:
                return run_group(argv, **kwargs)
            return original(argv, **kwargs)
        with patch.object(review.subprocess, 'run', side_effect=run):
            return self.f.run_review(code)

    def calls(self):
        path=self.installation['calls']
        return [json.loads(s) for s in path.read_text().splitlines()] if path.exists() else []

    def receipts(self):
        return [json.loads(p.read_text()) for p in (self.repo/'.git/bureau/scans/semgrep').glob('*/*/receipt.json')]

    def snapshot(self, body, capture):
        prompts={p.stem:normalize(p.read_text(),self.root) for p in capture.glob('*.txt')}
        stops=json.loads((self.repo/'.git/bureau/review-stops.json').read_text())
        return dict(prompts=prompts, comment=normalize(body,self.root),verdict=re.search(r'\*\*Verdict\*\*: (\w+)',body)[1],review_policy=stops['T-1']['review_policy'])

    def test_off_prompts_comment_verdict_and_reuse_identity_match_golden(self):
        golden=json.loads((ROOT/'tests/fixtures/semgrep-disabled-review.json').read_text())
        self.f.config.pop('review',None)
        _,body,capture=self.run_review(20)
        self.assertEqual(self.snapshot(body,capture),golden)
        self.assertEqual(self.calls(),[]);self.assertEqual(self.receipts(),[])

    def test_explicitly_disabled_matches_golden(self):
        self.f.config['review']['semgrep']['enabled']=False
        _,body,capture=self.run_review(20)
        golden=json.loads((ROOT/'tests/fixtures/semgrep-disabled-review.json').read_text())
        self.assertEqual(self.snapshot(body,capture),golden)

    def test_semgrep_only_late_evidence_all_three_correlated_receipts(self):
        record,body,capture=self.run_review(20)
        prompts={p.stem:p.read_text() for p in capture.glob('*.txt')}
        self.assertEqual(set(prompts),{'correctness','security','performance','correctness_late','security_late','performance_late'})
        for role in ('correctness','security','performance'):
            self.assertNotIn('fake.rule',prompts[role]);self.assertNotIn('Semgrep',prompts[role])
            self.assertIn('fake.rule',prompts[role+'_late']);self.assertIn('independent judgement',prompts[role+'_late'])
        self.assertEqual(record['calls'],6);self.assertEqual(record['usage']['input_tokens'],6,record)
        self.assertEqual(len(self.calls()),1)  # head-only addition, base empty
        self.assertIn('### Semgrep machine evidence (late)',body);self.assertNotIn('secretValue',body)
        self.assertEqual(self.receipts()[0]['status'],'measured')

    def test_blocking_floor_below_cap_and_block_preserved(self):
        self.f.config['review']['semgrep']['mode']='blocking'
        _,body,_=self.run_review(0)
        self.assertIn('**Verdict**: REQUEST_CHANGES',body);self.assertIn('SEMGREP FLOOR',body)
        self.f.values['security']=review.specialist('security',holds=['Acceptance requires human decision'])
        _,body,_=self.run_review(25)
        self.assertIn('**Verdict**: BLOCK',body)

    def test_missing_durable_receipt_never_reuses_advisory_approval(self):
        path = self.repo/'scripts/bureau-semgrep.sh'
        path.write_text(path.read_text()+'\n# Simulate a persistence/helper failure.\nbureau_semgrep() { return 24; }\n')
        self.run_review(20)
        self.assertEqual(len(self.resume_review()),6)
        self.assertEqual(self.receipts(),[])

    def test_order_build_semgrep_then_caps_and_existing_block(self):
        source=(ROOT/'templates/scripts/bureau-config.sh').read_text()
        names=('decide_review_verdict','apply_build_failure','_review_count','_review_shown')
        functions='\n'.join(re.search(r'^'+name+r'\(\) \{\n.*?^\}',source,re.M|re.S)[0] for name in names)
        def decide(verdict, build='true', cycles='0', rounds='1'):
            proc=subprocess.run(['/bin/bash','-c',functions+'\nSEMGREP_BLOCKED=1; decide_review_verdict "$@"',
                 'test',verdict,'0','0',build,cycles,'3',rounds,'4'],capture_output=True,text=True,timeout=10)
            self.assertEqual(proc.returncode,0,proc.stderr)
            lines=proc.stdout.splitlines();return lines[0],[s.split('\x1f')[0] for s in lines[1:]]
        self.assertEqual(decide('APPROVE'),('REQUEST_CHANGES',['semgrep']))
        self.assertEqual(decide('APPROVE','true','3'),('BLOCK',['semgrep','cycle-cap']))
        self.assertEqual(decide('APPROVE','true','0','4'),('BLOCK',['semgrep','round-cap']))
        self.assertEqual(decide('APPROVE','false','3'),('BLOCK',['build','cycle-cap']))
        self.assertEqual(decide('BLOCK'),('BLOCK',[]))

    def resume_review(self, expected=0):
        # Do not resume/delete the approval: this run must exercise the real reuse path.
        capture=self.root/'reuse-capture';capture.mkdir(exist_ok=True)
        for p in capture.iterdir():p.unlink()
        self.f.config['agents']['merge']=True
        (self.repo/'.bureau.json').write_text(json.dumps(self.f.config))
        process=run_group(['bash','scripts/bureau-worker.sh','T-1','code-review-pipeline.sh',
             str(self.root/'reuse-worker'),'001-task'],cwd=self.repo,
             env={**self.f.f.env,'BUREAU_NO_MERGE':'0','BUREAU_STOP_REQUESTED':'0',
                  'REVIEW_CAPTURE':str(capture),'REVIEW_RESULTS':json.dumps(self.f.values)},capture_output=True,text=True,timeout=90)
        for name in re.findall(r'Specialist outputs preserved at ([^\n]+)',process.stderr):
            self.addCleanup(shutil.rmtree,Path(name),ignore_errors=True)
        self.assertEqual(process.returncode,expected,process.stdout+process.stderr)
        return list(capture.glob('*.txt'))

    def test_approval_reuse_bound_to_pin_rules_and_settings(self):
        self.run_review(20)
        self.assertEqual(self.resume_review(),[])
        for kind in ('pin','rules','settings'):
            with self.subTest(kind=kind):
                # Put the fixture ticket back in Build Review for the next stop.
                items=json.loads(self.f.f.linear.read_text());items['T-1']['state']=dict(id='build_review',name='Build Review')
                self.f.f.linear.write_text(json.dumps(items))
                self.run_review(20)
                if kind=='pin':
                    source=self.installation['site']/'semgrep/__init__.py';source.write_text('# new reviewed pin\n')
                    fixture.refresh_manifest(self.installation['venv'],self.installation['manifest'])
                elif kind=='rules':
                    rules=self.repo/'scripts/bureau-semgrep-rules.yml';rules.write_text(rules.read_text()+'\n# reviewed policy revision\n')
                else:self.f.config['review']['semgrep']['block_severity']='WARNING'
                self.assertEqual(len(self.resume_review()),6)


MERGE_RUNNER=r'''
set -euo pipefail
source tests/lib/harness.sh
source tests/lib/pr2-gate.sh
sandbox_init TEAM-1 test-branch
trap 'teardown' EXIT
pr2_gate_setup
review_clean_specialists
export BUREAU_CONFIG="$SANDBOX/.bureau.json" BUREAU_SEMGREP_VENV="$SEMGREP_TEST_VENV"
python3 -B "$SEMGREP_PREPARE" "$SANDBOX"
export BUREAU_STUB_STATE_MERGE=state-merge BUREAU_STUB_AGENT_ENABLED=merge
export BUREAU_STUB_ISSUE_STATE=Merge BUREAU_STOP_REQUESTED=0 BUREAU_NO_MERGE=0
run_pipeline merge-pipeline.sh TEAM-1
python3 -B "$SEMGREP_COLLECT" "$SANDBOX"
'''


class MergePipelineTests(unittest.TestCase):
    def test_final_head_recheck_reuses_exact_receipt_and_refuses_changed_head(self):
        with tempfile.TemporaryDirectory(prefix='bureau-semgrep-merge-') as tmp:
            root=Path(tmp).resolve(); scripts=root/'manifest';scripts.mkdir()
            installed=fixture.fake_install(root,scripts)
            prepare=root/'prepare.py';collect=root/'collect.py';result=root/'result.json'
            prepare.write_text(r'''import importlib.util,json,os,pathlib,subprocess,sys
sys.dont_write_bytecode=True
root=pathlib.Path(os.environ['TEST_SOURCE']);repo=pathlib.Path(sys.argv[1])
sys.path.insert(0,str(root/'tests'));import semgrep_test as f
installed=json.loads(os.environ['SEMGREP_INSTALLED'])
installed={k:pathlib.Path(v) if k!='prefix' else v for k,v in installed.items()}
source=pathlib.Path(installed['manifest']);(repo/'scripts'/source.name).write_bytes(source.read_bytes())
f.inject_isolation(repo/'scripts',installed)
config=dict(review=dict(semgrep=dict(enabled=True,mode='blocking')))
(repo/'.bureau.json').write_text(json.dumps(config))
def git(*args):return subprocess.check_output(['git','-C',str(repo),*args],text=True).strip()
(repo/'a.js').write_text('const safe = 1;\n');git('add','a.js');git('commit','-qm','test: safe head');git('push','-q','origin','test-branch')
base=git('rev-parse','origin/main');head=git('rev-parse','HEAD')
m=f.load('semgrep',repo/'scripts/bureau-semgrep.py')
value=m.measure(repo,config,base,head,pr=99)
assert value['receipt']['status']=='measured',value
path=repo/'reviewed-receipt.json';path.write_text(json.dumps(value))
if os.environ['SEMGREP_CASE']=='changed':
    (repo/'a.js').write_text('eval(secretValue);\n');git('add','a.js');git('commit','-qm','test: changed final head');git('push','-q','origin','test-branch')
if os.environ['SEMGREP_CASE']=='unavailable':
    source=installed['venv']/'lib'/('python%d.%d' % sys.version_info[:2])/'site-packages/semgrep/__init__.py'
    source.write_text('# tampered after review\n')
if os.environ['SEMGREP_CASE']=='isolation': f.inject_isolation(repo/'scripts',installed,available=False)
head=git('rev-parse','HEAD')
(pathlib.Path(os.environ['PR2_GH'])/'comments.json').write_text(json.dumps([dict(body='## Code Review v2\n**Verdict**: APPROVE',createdAt='2026-01-01T00:00:00Z')]))
pr=pathlib.Path(os.environ['PR2_GH'])/'pr.json';data=json.loads(pr.read_text());data.update(headRefOid=head,baseRefOid=base);pr.write_text(json.dumps(data))
''')
            collect.write_text(r'''import json,os,pathlib,sys
repo=pathlib.Path(sys.argv[1]);gh=pathlib.Path(os.environ['PR2_GH'])
value=dict(code=os.environ['LAST_RC'],stdout=os.environ['LAST_STDOUT'],stderr=os.environ['LAST_STDERR'],
    merged=(gh/'merged').exists(),receipts=[json.loads(p.read_text()) for p in (repo/'.git/bureau/scans/semgrep').glob('*/*/receipt.json')],
    gh_calls=(repo/'gh_calls.log').read_text() if (repo/'gh_calls.log').exists() else '')
pathlib.Path(os.environ['SEMGREP_RESULT']).write_text(json.dumps(value))
''')
            env={**os.environ, 'TEST_SOURCE':str(ROOT),'SEMGREP_PREPARE':str(prepare),'SEMGREP_COLLECT':str(collect),
                 'SEMGREP_TEST_VENV':str(installed['venv']), 'SEMGREP_RESULT':str(result),
                 'SEMGREP_INSTALLED':json.dumps({k:str(v) if isinstance(v,Path) else v for k,v in installed.items()}),
                 'BUREAU_CALLER_STOP':'0'}
            for case in ('exact','changed','unavailable','isolation'):
                with self.subTest(case=case):
                    (installed['site']/'semgrep/__init__.py').write_text('')
                    proc=run_group(['bash','-c',MERGE_RUNNER],cwd=ROOT,env={**env,'SEMGREP_CASE':case},
                                        capture_output=True,text=True,timeout=90)
                    self.assertEqual(proc.returncode,0,proc.stdout+proc.stderr)
                    value=json.loads(result.read_text())
                    self.assertEqual(value['code'],'0' if case=='exact' else '25',value)
                    if case!='exact':
                        self.assertNotIn('gh\tpr\tmerge',value['gh_calls']);self.assertIn('semgrep:',value['stdout'])
                        self.assertEqual(len(value['receipts']),2)
                        if case=='changed': self.assertEqual({r['findings_new'] for r in value['receipts']},{0,1})
                        if case=='unavailable': self.assertIn('unavailable',{r['status'] for r in value['receipts']})
                        if case=='isolation': self.assertIn('not_measured',{r['status'] for r in value['receipts']})
                    else:
                        self.assertIn('gh\tpr\tmerge',value['gh_calls']);self.assertEqual(len(value['receipts']),1)


if __name__=='__main__':unittest.main()
