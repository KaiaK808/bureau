"""QA placement, disabled baselines and merge races using the installed runtime."""
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest

import semgrep_pipeline_test as shared
import trivy_test as fixture

ROOT=Path(__file__).resolve().parents[1]
MERGE_RUNNER=shared.MERGE_RUNNER.replace('BUREAU_SEMGREP_VENV="$SEMGREP_TEST_VENV"',
                                      'BUREAU_TRIVY_BIN="$TRIVY_TEST_BINARY"').replace('SEMGREP','TRIVY')
HOOK=shared.MERGE_INPUT_HOOK.replace("git('add', 'a.js')","git('add', 'requirements.txt')").replace("(repo/'a.js').write_text('const fixed = 1;\\n')",
                                   "(repo/'requirements.txt').write_text('safe==2.0\\n')")
PREPARE=r'''
import json,os,pathlib,shlex,shutil,subprocess,sys
sys.dont_write_bytecode=True
root=pathlib.Path(os.environ['TEST_SOURCE']);repo=pathlib.Path(sys.argv[1]);sys.path.insert(0,str(root/'tests'))
import trivy_test as f
installed=json.loads(os.environ['TRIVY_INSTALLED']);installed={k:pathlib.Path(v) if k!='prefix' else v for k,v in installed.items()}
(repo/'scripts/bureau-trivy-pins.json').write_bytes(installed['pins'].read_bytes())
f.inject_isolation(repo/'scripts',installed)
case=os.environ['TRIVY_CASE']
config=dict(qa=dict(trivy=dict(enabled=True,mode='blocking',cache_dir=str(installed['cache']))))
if case=='off':config={}
if case=='disabled':config['qa']['trivy']['enabled']=False
if case=='advisory':config['qa']['trivy']['mode']='advisory'
(repo/'.bureau.json').write_text(json.dumps(config))
def git(*args):return subprocess.check_output(['git','-C',str(repo),*args],text=True).strip()
(repo/'requirements.txt').write_text('safe==1.0\n');git('add','requirements.txt');git('commit','-qm','test: safe head');git('push','-q','origin','test-branch')
base=git('rev-parse','origin/main');head=git('rev-parse','HEAD');initial_head=head
if 'blocking' in case or case in ('head-before-scan-hold','negative-merge-guard'):
    (repo/'requirements.txt').write_text('badpkg==1.0\n');git('add','requirements.txt');git('commit','-qm','test: unsafe head');git('push','-q','origin','test-branch');head=git('rev-parse','HEAD')
m=f.load('trivy',repo/'scripts/bureau-trivy.py')
if case in ('exact','changed','unavailable','isolation','cache-digest','stale'):
    value=m.measure(repo,config,base,head,pr=99);assert value['receipt']['status']=='measured',value
(repo/'scanner-inputs.json').write_text(json.dumps(dict(head=head,base=base)))
if case.endswith(('during-scan','after-gates','during-final-fetch','during-gates')):
    hook=repo/'concurrent-update.py'
    hook.write_text('REPO='+repr(str(repo))+'\nGH='+repr(os.environ['PR2_GH'])+'\nCASE='+repr(case)+
                    '\nGIT='+repr(shutil.which('git'))+'\n'+pathlib.Path(os.environ['TRIVY_HOOK_SOURCE']).read_text())
    if case.endswith('during-scan'):
        installed['settings'].write_text(json.dumps(dict(hook=str(hook),exit=2 if 'failed' in case else 0)))
        if 'helper-failure' in case:
            helper=repo/'scripts/bureau-trivy.sh'
            helper.write_text(helper.read_text()+"""\neval "$(declare -f bureau_trivy | sed '1s/bureau_trivy/bureau_trivy_original/')"
bureau_trivy() {
  bureau_trivy_original "$@" || return $?
  [ "$1" != scan ] || return 24
}
""")
    elif case.endswith('during-final-fetch'):
        (pathlib.Path(os.environ['PR2_GH'])/'git-fetch-hook').write_text(str(hook))
    elif case.endswith('during-gates'):
        gh=pathlib.Path(os.environ['PR2_GH'])/'bin/gh';old='    if [ -n "$JSON" ]; then'
        replacement='''+repr('''    if [ "$JSON" = baseRefOid ]; then
      reads=$(( $(cat "$D/base_oid_reads" 2>/dev/null || echo 0) + 1 )); echo "$reads" > "$D/base_oid_reads"
      if [ "$reads" = 2 ]; then HOOK; fi
    fi
''')+r'''+old
        assert gh.read_text().count(old)==1
        gh.write_text(gh.read_text().replace(old,replacement.replace('HOOK',shlex.quote(sys.executable)+' -I -B '+shlex.quote(str(hook)))))
    else:
        gh=pathlib.Path(os.environ['PR2_GH'])/'bin/gh';old='*/branches/*) out < "$D/branch.json" ;;'
        replacement='''+repr('''*/branches/*)
        out < "$D/branch.json"
        reads=$(( $(cat "$D/base_reads" 2>/dev/null || echo 0) + 1 )); echo "$reads" > "$D/base_reads"
        if [ "$reads" = 2 ]; then HOOK; fi ;;
''')+r'''
        assert gh.read_text().count(old)==1
        gh.write_text(gh.read_text().replace(old,replacement.replace('HOOK',shlex.quote(sys.executable)+' -I -B '+shlex.quote(str(hook)))))
if case=='changed':
    (repo/'requirements.txt').write_text('badpkg==1.0\n');git('add','requirements.txt');git('commit','-qm','test: final head');git('push','-q','origin','test-branch')
if case=='unavailable':installed['binary'].write_text('#!/bin/sh\nexit 97\n')
if case=='isolation':f.inject_isolation(repo/'scripts',installed,available=False)
if case=='cache-digest':(installed['cache']/'db/trivy.db').write_bytes(b'changed')
if case=='stale':f.make_cache(installed['cache'],age=72)
head=git('rev-parse','HEAD')
gh=pathlib.Path(os.environ['PR2_GH'])
(gh/'comments.json').write_text(json.dumps([dict(id='approval',lastEditedAt=None,body=f'## Code Review v2\n**Verdict**: APPROVE\n**Reviewed head**: `{head}`\n**Target**: `main` at `{base}`',createdAt='2026-01-01T00:00:00Z')]))
pr=gh/'pr.json';data=json.loads(pr.read_text());data.update(headRefOid=head,baseRefOid=base);pr.write_text(json.dumps(data))
if case=='head-before-scan-hold':
    data['headRefOid']=initial_head;pr.write_text(json.dumps(data))
    comments=gh/'comments.json';rows=json.loads(comments.read_text())
    rows[0]['body']=rows[0]['body'].replace(f'**Reviewed head**: `{head}`',f'**Reviewed head**: `{initial_head}`');comments.write_text(json.dumps(rows))
    (gh/'move_head_after_reads').write_text('1 '+head)
if case=='negative-merge-guard':
    p=repo/'scripts/merge-pipeline.sh';text=p.read_text();text=text.replace('if [ "$(bureau_get \'.qa | type == "object" and has("trivy")\')" = true ]; then','if false; then');p.write_text(text)
'''
COLLECT=r'''
import json,os,pathlib,re
repo=pathlib.Path(__import__('sys').argv[1]);gh=pathlib.Path(os.environ['PR2_GH'])
value=dict(code=os.environ['LAST_RC'],stdout=os.environ['LAST_STDOUT'],stderr=os.environ['LAST_STDERR'],merged=(gh/'merge_calls.log').exists(),
           receipts=[json.loads(p.read_text()) for p in (repo/'.git/bureau/scans/trivy').glob('*/*/receipt.json')],
           scanned=json.loads((repo/'scanner-inputs.json').read_text()),hook_ran=(gh/'scan-hook-ran').exists(),
           hook_log=(repo/'concurrent-update.py.log').read_text() if (repo/'concurrent-update.py.log').exists() else '',
           calls=(repo/'calls.log').read_text(),gh_calls=(repo/'gh_calls.log').read_text(),
           git_calls=(repo/'git_calls.log').read_text(),
           mark=json.loads((repo/'.git/bureau/merge-gate-waits.json').read_text()).get('TEAM-1') if (repo/'.git/bureau/merge-gate-waits.json').exists() else None)
for key in ('gh_calls','git_calls'):value[key]=re.sub(r'\b[a-f0-9]{40,64}\b','<commit>',value[key].replace(str(repo),'<fixture>'))
pathlib.Path(os.environ['TRIVY_RESULT']).write_text(json.dumps(value))
'''


class MergeTests(unittest.TestCase):
    def cases(self,names):
        results=[]
        with tempfile.TemporaryDirectory(prefix='bureau-trivy-merge-') as temporary:
            root=Path(temporary).resolve();scripts=root/'pins';scripts.mkdir();installed=fixture.fake_install(root,scripts)
            original=installed['binary'].read_bytes()
            prepare=root/'prepare.py';prepare.write_text(PREPARE);collect=root/'collect.py';collect.write_text(COLLECT)
            compile(PREPARE,str(prepare),'exec')
            hook=root/'hook.py';hook.write_text(HOOK);result=root/'result.json'
            env={**os.environ,'PYTHONDONTWRITEBYTECODE':'1','TEST_SOURCE':str(ROOT),'TRIVY_PREPARE':str(prepare),
                 'TRIVY_COLLECT':str(collect),'TRIVY_TEST_BINARY':str(installed['binary']),'TRIVY_RESULT':str(result),
                 'TRIVY_INSTALLED':json.dumps({k:str(v) if isinstance(v,Path) else v for k,v in installed.items()}),
                 'TRIVY_HOOK_SOURCE':str(hook),'BUREAU_CALLER_STOP':'0'}
            for name in names:
                with self.subTest(case=name):
                    installed['binary'].write_bytes(original);installed['binary'].chmod(0o700);installed['settings'].write_text('{}');fixture.make_cache(installed['cache'])
                    p=shared.run_group(['bash','-c',MERGE_RUNNER],cwd=ROOT,env={**env,'TRIVY_CASE':name},capture_output=True,text=True,timeout=90)
                    self.assertEqual(p.returncode,0,p.stdout+p.stderr)
                    value=json.loads(result.read_text());results.append(value)
                    concurrent=name.endswith(('during-scan','after-gates','during-final-fetch','during-gates'))
                    expected='0' if name in ('exact','off','disabled','advisory','negative-merge-guard') else '2' if concurrent and not name.startswith('ci-') else '25'
                    self.assertEqual(value['code'],expected,value['stdout']+'\n'+value['stderr']+'\n'+value['hook_log'])
                    self.assertEqual(value['merged'],expected=='0',value)
                    if concurrent:
                        self.assertTrue(value['hook_ran'],value)
                        self.assertEqual(len(value['receipts']),1)
                        self.assertNotIn('needs-human',value['calls'])
                        self.assertIn('Gate regressed',value['stdout'])
                        self.assertEqual(value['mark']['head'],value['scanned']['head'])
                        if name in ('base-during-gates','base-after-gates'):
                            self.assertIn('PR base commit changed',value['stdout'])
                            self.assertNotIn('base_up_to_date: FAIL',value['stdout'])
                    elif name=='exact':
                        self.assertEqual(len(value['receipts']),1,'unchanged QA receipt must be reused')
                        logged=value['gh_calls'].splitlines();last=max(i for i,row in enumerate(logged) if row.startswith('gh\tapi\t') and '/branches/' in row)
                        self.assertEqual(len(logged[last+1:]),2)
                        self.assertTrue(logged[last+1].startswith('gh\tapi\tgraphql\t'))
                        self.assertIn('baseRefName baseRef{target{oid}}',logged[last+1]);self.assertIn('--match-head-commit',logged[-1])
                        fetches=[row for row in value['git_calls'].splitlines() if '\tfetch\t--no-tags\torigin\t' in row];self.assertEqual(len(fetches),2)
                    elif name in ('changed','unavailable','isolation','cache-digest','stale'):
                        self.assertEqual(len(value['receipts']),2)
                        if name=='changed':self.assertEqual({r['findings_new'] for r in value['receipts']},{0,1})
                    elif name=='head-before-scan-hold':
                        self.assertEqual(value['mark']['head'],value['receipts'][0]['head']);self.assertEqual(value['mark']['outcome'],'blocked')
                    elif expected=='0':self.assertEqual(value['receipts'],[])
        return results

    def test_final_head_changed_remeasures_and_no_qa_state_still_scans(self):
        self.cases(('exact','changed','unavailable','isolation','cache-digest','stale'))

    def test_stale_pass_block_failure_retarget_and_marks(self):
        self.cases(('head-during-scan','base-during-scan','retarget-during-scan','head-blocking-during-scan',
                    'base-blocking-during-scan','retarget-blocking-during-scan','head-failed-during-scan',
                    'head-helper-failure-during-scan','head-before-scan-hold'))

    def test_final_gates_follow_fetch_single_base_read_and_checkpoint(self):
        self.cases(('retarget-after-gates','ci-during-final-fetch','base-during-final-fetch','retarget-during-final-fetch'))
        source=(ROOT/'templates/scripts/merge-pipeline.sh').read_text()
        for line in source.splitlines():
            if line.strip().startswith('gh pr merge '):
                previous=source[:source.index(line)].rstrip().splitlines()[-1]
                self.assertEqual(previous.strip(),'bureau_ticket_checkpoint "$ISSUE"')

    def test_base_commit_advancing_with_green_final_gates_retries(self):
        self.cases(('base-during-gates','base-after-gates'))

    def test_disabled_and_advisory_match_frozen_call_baseline(self):
        golden=json.loads((ROOT/'tests/fixtures/semgrep-disabled-merge.json').read_text())
        for value in self.cases(('off','disabled','advisory')):self.assertEqual({k:value[k] for k in golden},golden)

    def test_negative_control_removed_merge_guard_merges_unsafe_head(self):
        # A changed head without remeasurement is merged by the legacy gate.
        self.cases(('negative-merge-guard',))



QA_RUNNER=r'''
set -euo pipefail
source tests/lib/harness.sh
sandbox_init TEAM-1 test-branch
trap 'teardown' EXIT
export BUREAU_STUB_STATE_QA=state-qa BUREAU_STUB_ISSUE_STATE=QA BUREAU_CONFIG="$SANDBOX/.bureau.json"
export BUREAU_TRIVY_BIN="$TRIVY_TEST_BINARY" GH_STUB_EXISTING_PR=99
python3 -B "$TRIVY_PREPARE" "$SANDBOX"
export FAKE_CLAUDE_BIN="$SANDBOX/logs/provider.sh"
export BUREAU_RUN_ID=$(jq -r .run "$SANDBOX/logs/run.json")
export BUREAU_STAGE_ATTEMPT_ID=$(jq -r .attempt "$SANDBOX/logs/run.json")
run_pipeline qa-pipeline.sh TEAM-1
python3 -B "$TRIVY_COLLECT" "$SANDBOX"
'''
QA_PREPARE=r'''
import json,os,pathlib,shutil,subprocess,sys,uuid
root=pathlib.Path(os.environ['TEST_SOURCE']);repo=pathlib.Path(sys.argv[1]);sys.path.insert(0,str(root/'tests'))
import trivy_test as f
installed=json.loads(os.environ['TRIVY_INSTALLED']);installed={k:pathlib.Path(v) if k!='prefix' else v for k,v in installed.items()}
(repo/'scripts/bureau-trivy-pins.json').write_bytes(installed['pins'].read_bytes());f.inject_isolation(repo/'scripts',installed)
case=os.environ['TRIVY_CASE']
legacy=os.environ.get('TRIVY_LEGACY')
if legacy:(repo/'scripts/qa-pipeline.sh').write_text(pathlib.Path(legacy).read_text())
source=(root/'templates/scripts/bureau-config.sh').read_text();start=source.index('commit_stage_changes() {');end=source.index('# Build the model invocation',start)
p=repo/'scripts/bureau-config.sh';p.write_text(p.read_text()+'\n'+source[start:end])
config=dict(repo=dict(test_command="printf 'fixture gate\\n'"))
if case.startswith('no-harness'):config['repo']['test_command']=''
if case not in ('off','no-harness-off'):
    config['qa']=dict(trivy=dict(enabled=case not in ('disabled','no-harness-disabled'),cache_dir=str(installed['cache']),
                               mode='blocking' if 'blocking' in case or 'negative' in case else 'advisory'))
(repo/'.bureau.json').write_text(json.dumps(config))
(repo/'.gitignore').write_text('.bureau.json\n.env\n.fake-origin.git/\n.specify/\nscripts/\nlogs/\n*.log\n__pycache__/\nfake_claude_counter\n')
(repo/'requirements.txt').write_text('safe==1.0\n' if case.startswith('parsing-') else 'badpkg==1.0\n')
if case.startswith('parsing-yarn-'):
    body='unsupported body text'
    if 'nested-mapping' in case:body='dependencies: missing: 2.0.0'
    if 'nested-sequence' in case:body='dependencies: - missing'
    if 'reserved-question' in case:body='dependencies: ? missing'
    if 'reserved-at' in case:body='dependencies: @missing'
    if 'reserved-percent' in case:body='dependencies: %TAG'
    (repo/'yarn.lock').write_text('"locked@npm:^1":\n  version: 1.0.0\n  '+body+'\n')
if case.startswith('parsing-inline-'):
    data='endpoint = "https://example.invalid # trivy:ignore:AVD-FIXTURE\n'
    if 'interpolation' in case:data='value = "${join("", ["https://example.invalid"])}" # trivy:ignore:X\n'
    if 'docker' in case:data='FROM scratch\nRUN echo secret # trivy:ignore:X\n'
    if 'docker-heredoc' in case:data='FROM alpine\nRUN cat <<EOF.txt\nEOF\n# trivy:ignore:X\nEOF.txt\n'
    if 'yaml-anchor-quote' in case:data='value: &anchor.v1 "keep # literal" # trivy:ignore:X\n'
    if 'yaml-anchor-block' in case:data='value: &anchor.v1 |\n  # trivy:ignore:X\n'
    (repo/('Dockerfile' if 'docker' in case else 'compose.yaml' if 'yaml' in case else 'infra.tf')).write_text(data)
provider=repo/'logs/provider.sh'
provider.write_text('#!/bin/bash\nset -euo pipefail\nprintf "%s\\n" "$*" > "$SANDBOX/logs/prompt.txt"\n'+
    ('printf "# QA added test\\n" > qa_added_test.py\n' if case=='postcommit' else '')+
    "printf '%s\\n' '{\"status\":\"GREEN\",\"tests_added\":0,\"tests_failing\":0,\"coverage_notes\":\"Coverage checked.\"}'\n")
provider.chmod(0o700)
def git(*args):return subprocess.check_output(['git','-C',str(repo),*args],text=True).strip()
git('add','.gitignore','requirements.txt','specs')
if case.startswith('parsing-'):git('add','yarn.lock' if 'yarn' in case else 'Dockerfile' if 'docker' in case else 'compose.yaml' if 'yaml' in case else 'infra.tf')
git('commit','-qm','test: QA source');git('push','-q','origin','test-branch')
(repo/'logs/before-head').write_text(git('rev-parse','HEAD'))
e=f.load('evidence',repo/'scripts/bureau-evidence.py');store=e.Store(repo/'.git/bureau');run=uuid.uuid4().hex
store.begin_run(run,'TEAM-1',repo,git('rev-parse','HEAD'));attempt=store.begin_attempt(run,'TEAM-1','qa','stage',repo,git('rev-parse','HEAD'))
(repo/'logs/run.json').write_text(json.dumps(dict(run=run,attempt=attempt)))
if 'helper-failure' in case:
    helper=repo/'scripts/bureau-trivy.sh';helper.write_text(helper.read_text()+'\nbureau_trivy_original() { :; }\n'+
        'eval "$(declare -f bureau_trivy | sed \'1s/bureau_trivy/bureau_trivy_saved/\')"\n'+
        'bureau_trivy() { if [ "$1" = scan ]; then return 24; else bureau_trivy_saved "$@"; fi; }\n')
if 'negative' in case:
    p=repo/'scripts/qa-pipeline.sh';text=p.read_text();text=text.replace('  qa_trivy_final --publish','  QA_TRIVY_NOTE=""; QA_TRIVY_BLOCKED=0').replace('\nqa_trivy_final\n','\nQA_TRIVY_NOTE=""; QA_TRIVY_BLOCKED=0\n');p.write_text(text)
'''
QA_COLLECT=r'''
import json,os,pathlib,re,subprocess,sys
repo=pathlib.Path(sys.argv[1])
def normalize(text):
    text=text.replace(str(repo),'<fixture>')
    text=re.sub(r'--gate-context \S+','--gate-context <gate-context>',text)
    text=re.sub(r'logs/qa-TEAM-1-[^\s`]+\.log','logs/qa-TEAM-1-<log>.log',text)
    return text
receipts=[json.loads(p.read_text()) for p in (repo/'.git/bureau/scans/trivy').glob('*/*/receipt.json')]
run=json.loads((repo/'logs/run.json').read_text())
sys.path.insert(0,str(pathlib.Path(os.environ['TEST_SOURCE'])/'tests'));import trivy_test as f
e=f.load('evidence',repo/'scripts/bureau-evidence.py');gates=e.Store(repo/'.git/bureau')._manifest(run['run'])['attempts'][0]['gates']
value=dict(code=os.environ['LAST_RC'],stdout=os.environ['LAST_STDOUT'],stderr=os.environ['LAST_STDERR'],receipts=receipts,gates=gates,
    receipt_hashes=[f.load('scan').file_sha(p) for p in (repo/'.git/bureau/scans/trivy').glob('*/*/receipt.json')],
    before=(repo/'logs/before-head').read_text(),head=subprocess.check_output(['git','-C',str(repo),'rev-parse','HEAD'],text=True).strip(),
    published=subprocess.check_output(['git','--git-dir',str(repo/'.fake-origin.git'),'rev-parse','test-branch'],text=True).strip(),
    snapshot=dict(prompt=normalize((repo/'logs/prompt.txt').read_text()) if (repo/'logs/prompt.txt').exists() else '',calls=normalize((repo/'calls.log').read_text())))
pathlib.Path(os.environ['TRIVY_RESULT']).write_text(json.dumps(value))
'''


class QATests(unittest.TestCase):
    def run_case(self,case,legacy_source=None):
        with tempfile.TemporaryDirectory(prefix='bureau-trivy-qa-') as temporary:
            root=Path(temporary).resolve();pins=root/'pins';pins.mkdir();installed=fixture.fake_install(root,pins)
            prepare=root/'prepare.py';prepare.write_text(QA_PREPARE);collect=root/'collect.py';collect.write_text(QA_COLLECT);result=root/'result.json'
            env={**os.environ,'PYTHONDONTWRITEBYTECODE':'1','TEST_SOURCE':str(ROOT),'TRIVY_PREPARE':str(prepare),
                 'TRIVY_COLLECT':str(collect),'TRIVY_TEST_BINARY':str(installed['binary']),'TRIVY_RESULT':str(result),'TRIVY_CASE':case,
                 'TRIVY_INSTALLED':json.dumps({k:str(v) if isinstance(v,Path) else v for k,v in installed.items()})}
            if legacy_source:
                legacy=root/'legacy.sh';legacy.write_text(legacy_source);env['TRIVY_LEGACY']=str(legacy)
            p=shared.run_group(['bash','-c',QA_RUNNER],cwd=ROOT,env=env,capture_output=True,text=True,timeout=90)
            self.assertEqual(p.returncode,0,p.stdout+p.stderr)
            return json.loads(result.read_text())

    def test_disabled_prompts_comments_and_routing_match_frozen_baseline(self):
        golden=json.loads((ROOT/'tests/fixtures/trivy-disabled-qa.json').read_text())
        for case in ('off','disabled','no-harness-off','no-harness-disabled'):
            with self.subTest(case=case):
                value=self.run_case(case);self.assertEqual(value['code'],'0',value['stdout']+value['stderr'])
                self.assertEqual(value['snapshot'],golden['no-harness' if case.startswith('no-harness') else 'normal'])
                self.assertEqual(value['receipts'],[])

    def test_qa_scans_after_its_commit_and_push_and_archives_receipt_digest(self):
        value=self.run_case('postcommit');self.assertEqual(value['code'],'0',value['stdout']+value['stderr'])
        self.assertNotEqual(value['head'],value['before']);self.assertEqual(value['head'],value['published'])
        self.assertEqual(len(value['receipts']),1);receipt=value['receipts'][0]
        self.assertEqual(receipt['head'],value['published']);self.assertEqual(receipt['findings_new'],1)
        self.assertIn('Trivy machine evidence',value['snapshot']['calls']);self.assertIn('move_issue\tTEAM-1\tstate-build-review',value['snapshot']['calls'])
        self.assertTrue(any(row.get('kind')=='artifact' and row.get('command_id')=='qa.summary' and row.get('sha256') in value['receipt_hashes'] for row in value['gates']),value['gates'])
        self.assertNotIn('Trivy',value['snapshot']['prompt'])

    def test_no_harness_scans_and_blocking_holds_in_both_routes(self):
        for case in ('no-harness-advisory','blocking','no-harness-blocking','blocking-helper-failure'):
            with self.subTest(case=case):
                value=self.run_case(case);blocked='blocking' in case
                self.assertEqual(value['code'],'25' if blocked else '0',value['stdout']+value['stderr'])
                self.assertEqual(len(value['receipts']),0 if 'helper-failure' in case else 1)
                if blocked:
                    self.assertNotIn('move_issue\tTEAM-1\tstate-build-review',value['snapshot']['calls']);self.assertIn('needs-human',value['snapshot']['calls'])
                else:self.assertIn('move_issue\tTEAM-1\tstate-build-review',value['snapshot']['calls'])

    def test_parsing_gaps_keep_existing_advisory_and_blocking_routes(self):
        self.parsing_gaps((('yarn','yarn.lock','lockfile format not recognised'),
                           ('inline','infra.tf','inline ignore format not recognised')))

    def test_per_type_and_nested_scalar_gaps_keep_existing_routes(self):
        self.parsing_gaps((('yarn-nested-mapping','yarn.lock','lockfile format not recognised'),
                           ('yarn-nested-sequence','yarn.lock','lockfile format not recognised'),
                           ('inline-interpolation','infra.tf','inline ignore format not recognised'),
                           ('inline-docker','Dockerfile','inline ignore format not recognised')))

    def test_narrowed_shapes_keep_existing_advisory_and_blocking_routes(self):
        self.parsing_gaps((('yarn-reserved-question','yarn.lock','lockfile format not recognised'),
                           ('yarn-reserved-at','yarn.lock','lockfile format not recognised'),
                           ('yarn-reserved-percent','yarn.lock','lockfile format not recognised'),
                           ('inline-yaml-anchor-quote','compose.yaml','inline ignore format not recognised'),
                           ('inline-yaml-anchor-block','compose.yaml','inline ignore format not recognised'),
                           ('inline-docker-heredoc','Dockerfile','inline ignore format not recognised')))

    def parsing_gaps(self,cases):
        for parser,name,reason in cases:
            for mode in ('advisory','blocking'):
                with self.subTest(parser=parser,mode=mode):
                    value=self.run_case('parsing-'+parser+'-'+mode);blocked=mode=='blocking'
                    self.assertEqual(value['code'],'25' if blocked else '0',value['stdout']+value['stderr'])
                    self.assertEqual(len(value['receipts']),1);receipt=value['receipts'][0]
                    self.assertEqual(receipt['status'],'reduced_coverage')
                    self.assertEqual(receipt['findings_new'],0);self.assertEqual(receipt['blocking'],blocked)
                    self.assertIn(reason+': '+name,receipt['reason'])
                    if blocked:
                        self.assertIn('needs-human',value['snapshot']['calls'])
                        self.assertNotIn('move_issue\tTEAM-1\tstate-build-review',value['snapshot']['calls'])
                    else:self.assertIn('move_issue\tTEAM-1\tstate-build-review',value['snapshot']['calls'])

    def test_negative_control_removed_qa_scan_routes_unsafe_head(self):
        value=self.run_case('negative-blocking');self.assertEqual(value['code'],'0');self.assertEqual(value['receipts'],[])
        self.assertIn('move_issue\tTEAM-1\tstate-build-review',value['snapshot']['calls'])


if __name__=='__main__':unittest.main()
