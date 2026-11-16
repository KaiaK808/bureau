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
real_git=$(command -v git)
cat > "$PR2_GH/bin/git" <<EOF
#!/bin/bash
{ printf 'git'; for a in "\$@"; do printf '\\t%s' "\$a"; done; printf '\\n'; } >> "\${SANDBOX:?}/git_calls.log"
case " \$* " in
  *" fetch --no-tags origin +refs/heads/main:refs/remotes/origin/main ")
    if [ -s "\$PR2_GH/git-fetch-hook" ]; then
      python3 -I -B "\$(cat "\$PR2_GH/git-fetch-hook")" || exit "\$?"
    fi ;;
esac
exec "$real_git" "\$@"
EOF
chmod +x "$PR2_GH/bin/git"
: > "$SANDBOX/gh_calls.log"
export BUREAU_MERGE_GATE_REPORT="$SANDBOX/merge-gate-report.txt"
export BUREAU_STUB_STATE_MERGE=state-merge BUREAU_STUB_AGENT_ENABLED=merge
export BUREAU_STUB_ISSUE_STATE=Merge BUREAU_STOP_REQUESTED=0 BUREAU_NO_MERGE=0
run_pipeline merge-pipeline.sh TEAM-1
python3 -B "$SEMGREP_COLLECT" "$SANDBOX"
'''


MERGE_INPUT_HOOK = r'''
import json, os, pathlib, subprocess
repo, gh = pathlib.Path(REPO), pathlib.Path(GH)
marker = gh/'scan-hook-ran'
assert not marker.exists(), 'concurrent update must happen only once'
marker.write_text(CASE)
def git(*args):
    return subprocess.check_output([GIT, '-C', str(repo), *args], text=True,
        env={**os.environ, 'GIT_AUTHOR_NAME':'Test', 'GIT_AUTHOR_EMAIL':'test@example.invalid',
             'GIT_COMMITTER_NAME':'Test', 'GIT_COMMITTER_EMAIL':'test@example.invalid'}).strip()
pr = gh/'pr.json'; data = json.loads(pr.read_text())
if CASE.startswith('base-'):
    parent = git('rev-parse', 'origin/main')
    commit = git('--git-dir', str(repo/'.fake-origin.git'), 'commit-tree', git('rev-parse', parent+'^{tree}'),
                 '-p', parent, '-m', 'test: base advances during scan')
    git('--git-dir', str(repo/'.fake-origin.git'), 'update-ref', 'refs/heads/main', commit)
    # GitHub's base/CI gates can already be green for the new base; the scan is still stale.
    data['baseRefOid'] = commit
    (gh/'branch.json').write_text(json.dumps(dict(commit=dict(sha=commit))))
elif CASE.startswith('head-'):
    (repo/'a.js').write_text('const fixed = 1;\n')
    git('add', 'a.js'); git('commit', '-qm', 'test: fix PR head during scan')
    commit = git('rev-parse', 'HEAD')
    git('push', '-q', 'origin', 'test-branch')
    data['headRefOid'] = commit
elif CASE.startswith('retarget-'):
    git('--git-dir', str(repo/'.fake-origin.git'), 'update-ref', 'refs/heads/release', data['baseRefOid'])
    data['baseRefName'] = 'release'  # Same commit, different policy authority.
elif CASE.startswith('ci-'):
    (gh/'check_runs.json').write_text(json.dumps(dict(check_runs=[dict(name='ci', status='completed', conclusion='failure')])))
else:
    raise AssertionError(CASE)
pr.write_text(json.dumps(data))
'''


class MergePipelineTests(unittest.TestCase):
    def test_final_head_recheck_reuses_exact_receipt_and_refuses_changed_head(self):
        self.merge_cases(('exact', 'changed', 'unavailable', 'isolation'))

    def test_scan_input_changes_wait_for_a_new_scan_and_gates_run_after_scanning(self):
        self.merge_cases(('base-during-scan', 'head-during-scan', 'retarget-during-scan', 'ci-during-scan',
                          'retarget-after-gates', 'ci-during-final-fetch', 'base-during-final-fetch'))

    def test_base_commit_advancing_with_green_final_gates_retries(self):
        self.merge_cases(('base-during-gates','base-after-gates'))

    def test_retarget_during_last_fetch_precedes_final_identity_check(self):
        self.merge_cases(('retarget-during-final-fetch',))

    def test_stale_blocking_and_failed_scans_retry_and_marks_bind_to_scanned_head(self):
        self.merge_cases(('head-blocking-during-scan', 'base-blocking-during-scan',
                          'retarget-blocking-during-scan', 'head-failed-during-scan',
                          'head-helper-failure-during-scan'))

    def test_hold_mark_binds_to_scanned_head_instead_of_initial_gate_head(self):
        self.merge_cases(('head-before-scan-hold',))

    def test_disabled_and_advisory_gh_git_calls_match_disabled_baseline(self):
        golden = json.loads((ROOT/'tests/fixtures/semgrep-disabled-merge.json').read_text())
        for value in self.merge_cases(('off', 'disabled', 'advisory')):
            self.assertEqual({key:value[key] for key in golden}, golden)

    def merge_cases(self, cases):
        values = []
        with tempfile.TemporaryDirectory(prefix='bureau-semgrep-merge-') as tmp:
            root=Path(tmp).resolve(); scripts=root/'manifest';scripts.mkdir()
            installed=fixture.fake_install(root,scripts)
            prepare=root/'prepare.py';collect=root/'collect.py';result=root/'result.json'
            hook_source=root/'hook-source.py';hook_source.write_text(MERGE_INPUT_HOOK)
            prepare.write_text(r'''import importlib.util,json,os,pathlib,shutil,subprocess,sys
sys.dont_write_bytecode=True
root=pathlib.Path(os.environ['TEST_SOURCE']);repo=pathlib.Path(sys.argv[1])
sys.path.insert(0,str(root/'tests'));import semgrep_test as f
installed=json.loads(os.environ['SEMGREP_INSTALLED'])
installed={k:pathlib.Path(v) if k!='prefix' else v for k,v in installed.items()}
source=pathlib.Path(installed['manifest']);(repo/'scripts'/source.name).write_bytes(source.read_bytes())
f.inject_isolation(repo/'scripts',installed)
config=dict(review=dict(semgrep=dict(enabled=True,mode='blocking')))
case=os.environ['SEMGREP_CASE']
if case=='off': config={}
if case=='disabled': config['review']['semgrep']['enabled']=False
if case=='advisory': config['review']['semgrep']['mode']='advisory'
(repo/'.bureau.json').write_text(json.dumps(config))
def git(*args):return subprocess.check_output(['git','-C',str(repo),*args],text=True).strip()
(repo/'a.js').write_text('const safe = 1;\n');git('add','a.js');git('commit','-qm','test: safe head');git('push','-q','origin','test-branch')
base=git('rev-parse','origin/main');head=git('rev-parse','HEAD')
initial_head=head
if 'blocking' in case or case=='head-before-scan-hold':
    (repo/'a.js').write_text('eval(secretValue);\n');git('add','a.js');git('commit','-qm','test: blocking head');git('push','-q','origin','test-branch')
    head=git('rev-parse','HEAD')
m=f.load('semgrep',repo/'scripts/bureau-semgrep.py')
if case in ('exact','changed','unavailable','isolation'):
    value=m.measure(repo,config,base,head,pr=99)
    assert value['receipt']['status']=='measured',value
    path=repo/'reviewed-receipt.json';path.write_text(json.dumps(value))
(repo/'scanner-inputs.json').write_text(json.dumps(dict(head=head,base=base)))
if case.endswith(('during-scan','after-gates','during-final-fetch','during-gates')):
    hook=repo/'concurrent-update.py'
    hook.write_text('REPO='+repr(str(repo))+'\nGH='+repr(os.environ['PR2_GH'])+'\nCASE='+repr(case)+
                   '\nGIT='+repr(shutil.which('git'))+'\n'+pathlib.Path(os.environ['SEMGREP_HOOK_SOURCE']).read_text())
    if case.endswith('during-scan'):
        installed['settings'].write_text(json.dumps(dict(hook=str(hook), exit=2 if 'failed' in case else 0)))
        if 'helper-failure' in case:
            helper=repo/'scripts/bureau-semgrep.sh'
            helper.write_text(helper.read_text()+"""\n# Fail the helper after the real scanner changed the inputs.
eval "$(declare -f bureau_semgrep | sed '1s/bureau_semgrep/bureau_semgrep_original/')"
bureau_semgrep() {
  bureau_semgrep_original "$@" || return $?
  [ "$1" != scan ] || return 24
}
""")
    elif case.endswith('during-final-fetch'):
        (pathlib.Path(os.environ['PR2_GH'])/'git-fetch-hook').write_text(str(hook))
    elif case.endswith('during-gates'):
        # Move main after the final fetch/check but before the JIT base gate
        # reads either OID, so that gate observes B consistently and passes.
        gh=pathlib.Path(os.environ['PR2_GH'])/'bin/gh'
        old='    if [ -n "$JSON" ]; then'
        replacement=r"""    if [ "$JSON" = baseRefOid ]; then
      reads=$(( $(cat "$D/base_oid_reads" 2>/dev/null || echo 0) + 1 )); echo "$reads" > "$D/base_oid_reads"
      if [ "$reads" = 2 ]; then HOOK; fi
    fi
"""+old
        assert gh.read_text().count(old)==1
        import shlex
        gh.write_text(gh.read_text().replace(old,replacement.replace('HOOK',shlex.quote(sys.executable)+' -I -B '+shlex.quote(str(hook)))))
    else:
        gh=pathlib.Path(os.environ['PR2_GH'])/'bin/gh'
        old='*/branches/*) out < "$D/branch.json" ;;'
        replacement=r"""*/branches/*)
        out < "$D/branch.json"
        reads=$(( $(cat "$D/base_reads" 2>/dev/null || echo 0) + 1 )); echo "$reads" > "$D/base_reads"
        if [ "$reads" = 2 ]; then HOOK; fi ;;
"""
        assert gh.read_text().count(old)==1
        import shlex
        gh.write_text(gh.read_text().replace(old,replacement.replace('HOOK',shlex.quote(sys.executable)+' -I -B '+shlex.quote(str(hook)))))
if os.environ['SEMGREP_CASE']=='changed':
    (repo/'a.js').write_text('eval(secretValue);\n');git('add','a.js');git('commit','-qm','test: changed final head');git('push','-q','origin','test-branch')
if os.environ['SEMGREP_CASE']=='unavailable':
    source=installed['venv']/'lib'/('python%d.%d' % sys.version_info[:2])/'site-packages/semgrep/__init__.py'
    source.write_text('# tampered after review\n')
if os.environ['SEMGREP_CASE']=='isolation': f.inject_isolation(repo/'scripts',installed,available=False)
head=git('rev-parse','HEAD')
(pathlib.Path(os.environ['PR2_GH'])/'comments.json').write_text(json.dumps([dict(body='## Code Review v2\n**Verdict**: APPROVE',createdAt='2026-01-01T00:00:00Z')]))
pr=pathlib.Path(os.environ['PR2_GH'])/'pr.json';data=json.loads(pr.read_text());data.update(headRefOid=head,baseRefOid=base);pr.write_text(json.dumps(data))
if case=='head-before-scan-hold':
    data['headRefOid']=initial_head;pr.write_text(json.dumps(data))
    (pathlib.Path(os.environ['PR2_GH'])/'move_head_after_reads').write_text('1 '+head)
''')
            collect.write_text(r'''import json,os,pathlib,re,sys
repo=pathlib.Path(sys.argv[1]);gh=pathlib.Path(os.environ['PR2_GH'])
value=dict(code=os.environ['LAST_RC'],stdout=os.environ['LAST_STDOUT'],stderr=os.environ['LAST_STDERR'],
    merged=(gh/'merge_calls.log').exists(),receipts=[json.loads(p.read_text()) for p in (repo/'.git/bureau/scans/semgrep').glob('*/*/receipt.json')],
    gh_calls=(repo/'gh_calls.log').read_text() if (repo/'gh_calls.log').exists() else '',
    git_calls=(repo/'git_calls.log').read_text() if (repo/'git_calls.log').exists() else '',
    scanned=json.loads((repo/'scanner-inputs.json').read_text()),
    hook_ran=(gh/'scan-hook-ran').exists(),
    hook_log=(repo/'concurrent-update.py.log').read_text() if (repo/'concurrent-update.py.log').exists() else '',
    mark=json.loads((repo/'.git/bureau/merge-gate-waits.json').read_text()).get('TEAM-1') if (repo/'.git/bureau/merge-gate-waits.json').exists() else None,
    calls=(repo/'calls.log').read_text() if (repo/'calls.log').exists() else '',
    gate_report=(repo/'merge-gate-report.txt').read_text() if (repo/'merge-gate-report.txt').exists() else '')
pin=re.search(r'--match-head-commit\t([a-f0-9]{40,64})',value['gh_calls'])
value['merge_head']=pin[1] if pin else None
for key in ('gh_calls','git_calls'):
    value[key]=re.sub(r'\b[a-f0-9]{40,64}\b','<commit>',value[key].replace(str(repo),'<fixture>'))
pathlib.Path(os.environ['SEMGREP_RESULT']).write_text(json.dumps(value))
''')
            env={**os.environ, 'TEST_SOURCE':str(ROOT),'SEMGREP_PREPARE':str(prepare),'SEMGREP_COLLECT':str(collect),
                 'SEMGREP_TEST_VENV':str(installed['venv']), 'SEMGREP_RESULT':str(result),
                 'SEMGREP_INSTALLED':json.dumps({k:str(v) if isinstance(v,Path) else v for k,v in installed.items()}),
                 'SEMGREP_HOOK_SOURCE':str(hook_source),
                 'BUREAU_CALLER_STOP':'0'}
            for case in cases:
                with self.subTest(case=case):
                    (installed['site']/'semgrep/__init__.py').write_text('')
                    installed['settings'].write_text('{}')
                    proc=run_group(['bash','-c',MERGE_RUNNER],cwd=ROOT,env={**env,'SEMGREP_CASE':case},
                                        capture_output=True,text=True,timeout=90)
                    self.assertEqual(proc.returncode,0,proc.stdout+proc.stderr)
                    value=json.loads(result.read_text())
                    values.append(value)
                    expected = '0' if case in ('exact','off','disabled','advisory') else '25'
                    concurrent = case.endswith(('during-scan','after-gates','during-final-fetch','during-gates'))
                    if case.startswith(('base-','head-','retarget-')) and concurrent: expected = '2'
                    if concurrent:
                        self.assertTrue(value['hook_ran'], value['stdout']+'\n'+value['stderr']+'\n'+value['git_calls'])
                        self.assertEqual([r['status'] for r in value['receipts']],
                                         ['not_measured' if 'failed' in case else 'measured'], value['hook_log'])
                    self.assertEqual(value['code'],expected,value['stdout']+'\n'+value['stderr'])
                    if concurrent:
                        self.assertTrue(value['hook_ran'])
                        self.assertFalse(value['merged'])
                        self.assertNotIn('gh\tpr\tmerge',value['gh_calls'])
                        self.assertIn('Gate regressed between initial check and merge',value['stdout'])
                        self.assertTrue(value['gate_report'].startswith('blocked\n' if case.startswith('ci-') else 'not-yet\n'))
                        self.assertEqual(len(value['receipts']),1)
                        self.assertEqual(value['receipts'][0]['head'],value['scanned']['head'])
                        self.assertEqual(value['receipts'][0]['base'],value['scanned']['base'])
                        self.assertNotIn('needs-human', value['calls'])
                        self.assertEqual(value['mark']['head'], value['scanned']['head'])
                        if case in ('base-during-gates','base-after-gates'):
                            self.assertIn('PR base commit changed',value['stdout'])
                            self.assertNotIn('base_up_to_date: FAIL',value['stdout'])
                    elif case=='head-before-scan-hold':
                        self.assertFalse(value['merged'])
                        self.assertEqual(value['mark']['outcome'], 'blocked')
                        self.assertEqual(value['mark']['head'], value['receipts'][0]['head'])
                        self.assertEqual(value['mark']['head'], value['scanned']['head'])
                    elif case in ('off','disabled','advisory'):
                        self.assertTrue(value['merged']);self.assertEqual(value['receipts'],[])
                    elif case!='exact':
                        self.assertNotIn('gh\tpr\tmerge',value['gh_calls']);self.assertIn('semgrep:',value['stdout'])
                        self.assertEqual(len(value['receipts']),2)
                        if case=='changed': self.assertEqual({r['findings_new'] for r in value['receipts']},{0,1})
                        if case=='unavailable': self.assertIn('unavailable',{r['status'] for r in value['receipts']})
                        if case=='isolation': self.assertIn('not_measured',{r['status'] for r in value['receipts']})
                    else:
                        self.assertIn('gh\tpr\tmerge',value['gh_calls']);self.assertEqual(len(value['receipts']),1)
                        self.assertIn('--match-head-commit\t<commit>',value['gh_calls'])
                        self.assertEqual(value['receipts'][0]['head'],value['scanned']['head'])
                        self.assertTrue(value['merged'])
                        self.assertEqual(value['merge_head'],value['receipts'][0]['head'])
                        fetches = [line.split('\t') for line in value['git_calls'].splitlines() if '\tfetch\t--no-tags\torigin\t' in line]
                        self.assertEqual(len(fetches),2)
                        self.assertEqual([arg for arg in fetches[-1] if arg.startswith('+refs/heads/')],
                                         ['+refs/heads/main:refs/remotes/origin/main'])
                        # After the final gate's API read, only the one live
                        # base name/OID query and pinned merge touch GitHub.
                        logged=value['gh_calls'].splitlines()
                        last_gate=max(i for i,line in enumerate(logged) if line.startswith('gh\tapi\t') and '/branches/' in line)
                        after_gates=logged[last_gate+1:]
                        self.assertEqual(len(after_gates), 2, after_gates)
                        self.assertTrue(after_gates[0].startswith('gh\tapi\tgraphql\t'))
                        self.assertIn('baseRefName baseRef{target{oid}}',after_gates[0])
                        self.assertTrue(after_gates[1].startswith('gh\tpr\tmerge\t'))
        return values


if __name__=='__main__':unittest.main()
