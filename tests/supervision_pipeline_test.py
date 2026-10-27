"""Real bounded ticks/stages/ownership/picker; only external services are faked."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import git_maintenance

ROOT = Path(__file__).resolve().parents[1]


def datetime_from_epoch(value):
    from datetime import datetime, timezone
    return datetime.fromtimestamp(value,timezone.utc).isoformat().replace('+00:00','Z')


def load_script(name, path):
    spec=importlib.util.spec_from_file_location(name,path)
    module=importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    return module

CURL = r'''
import json, os, pathlib, re, sys
path = pathlib.Path(os.environ['LINEAR_FIXTURE'])
items = json.loads(path.read_text())
payload = json.loads(sys.argv[sys.argv.index('-d') + 1]); query = payload['query']; variables = payload.get('variables', {})
if 'completedAt canceledAt' in query and os.environ.get('WAIT_QUERY_LOG'):
    with open(os.environ['WAIT_QUERY_LOG'],'a') as out: out.write(query+'\n')
if 'viewer' in query:
    result = {'viewer': {'id': 'viewer'}}
elif 'issueLabels' in query:
    name = re.search(r'name: \{ eq: "([^"]+)"', query).group(1)
    result = {'issueLabels': {'nodes': [{'id': name, 'team': None}]}}  # workspace-level label
elif 'mutation' in query:
    key = next(key for key,item in items.items() if item['id'] == variables['id'])
    if 'issueAddLabel' in query:
        items[key]['labels']['nodes'].append({'name': variables['lid']}); result = {'issueAddLabel': {'success': True}}
    elif 'issueUpdate' in query:
        items[key]['state'] = {'id': variables['sid'], 'name': variables['sid']}; result = {'issueUpdate': {'success': True}}
    elif 'commentCreate' in query:
        items[key]['comments']['nodes'].append({'body': variables['body'], 'createdAt':'2026-09-09T01:00:00Z'}); result = {'commentCreate': {'success':True}}
    else: raise AssertionError(query)
    path.write_text(json.dumps(items))
else:
    number = re.search(r'number: \{ eq: (\d+)', query)
    state = re.search(r'state: \{ id: \{ eq: "([^"]+)"', query)
    values = list(items.values())
    if number: values = [items['T-' + number.group(1)]]
    elif state: values = [item for item in values if item['state']['id'] == state.group(1)]
    result = {'issues': {'nodes': values}}
print(json.dumps({'data': result}))
'''

GH = r'''
import json, os, pathlib, subprocess, sys
args = sys.argv[1:]
with open(os.environ['GH_LOG'],'a') as out: out.write(json.dumps(args)+'\n')
if args[:2] == ['pr', 'list']:
    branch = args[args.index('--head')+1]; number=int(branch.split('-')[0])
    if args[args.index('--json')+1]=='number,url': print(json.dumps({'number':number,'url':f'https://example.invalid/pull/{number}'}))
    else: print(number)
elif args[:2] == ['pr', 'view']:
    number = int(args[2]); fields = args[args.index('--json')+1]
    metadata = {'state':'OPEN','baseRefName':os.environ.get('PR_BASE_REF','main'),'headRefName':f'{number:03d}-task'}
    if os.environ.get('PR_REFS_JSON') is not None: metadata = json.loads(os.environ['PR_REFS_JSON'])
    if fields == 'state':
        if '--jq' in args: print('OPEN')
        else:
            if os.environ.get('FAIL_GH_HEAD') == '1': sys.exit(1)
            print(json.dumps({'state':'OPEN'}))
    elif fields in ('baseRefName,headRefName','state,baseRefName,mergedAt','state,baseRefName,headRefName','state,mergedAt'):
        if os.environ.get('FAIL_GH_HEAD') == '1': sys.exit(1)
        print(json.dumps({key:metadata[key] for key in fields.split(',') if key in metadata}))
    elif fields == 'url': print(f'https://example.invalid/pull/{number}')
    else: raise AssertionError(args)
elif args[:2] == ['pr','comment']: pass
else: raise AssertionError(args)
'''

CODEX = r'''
import json, os, pathlib, re, subprocess, sys, uuid
if sys.argv[1] == 'login': sys.exit(0)
assert sys.argv[1] == 'exec'
prompt = sys.stdin.read()
if os.environ.get('REVIEW_PROBE') and 'specialist reviewing' in prompt:
    if os.environ.get('REVIEW_MOVE_REF') or os.environ.get('REVIEW_ADVANCE_REF'):
        try: pathlib.Path(os.environ['REVIEW_PROBE'],'ref-mutated').mkdir()
        except FileExistsError: pass
        else:
            if os.environ.get('REVIEW_MOVE_REF'):
                subprocess.run(['git','update-ref','refs/remotes/origin/'+os.environ['REVIEW_MOVE_REF'],os.environ['REVIEW_MOVE_SHA']],check=True)
            if os.environ.get('REVIEW_ADVANCE_REF'):
                subprocess.run(['git','--git-dir',os.environ['ORIGIN_FIXTURE'],'update-ref','refs/heads/'+os.environ['REVIEW_ADVANCE_REF'],os.environ['REVIEW_MOVE_SHA']],check=True)
    match = re.search(r"git diff ([0-9a-f]+\.\.\.[0-9a-f]+) --", prompt)
    if match:
        # Read the same immutable range the real specialist receives, against
        # the actual worker Git history after the local validation merge.
        files=subprocess.check_output(['git','diff','--name-only',match.group(1),'--'],text=True).splitlines()
        detail={'prompt':prompt,'files':files,'head':subprocess.check_output(['git','rev-parse','HEAD'],text=True).strip()}
    else: detail={'prompt':prompt,'files':None}
    pathlib.Path(os.environ['REVIEW_PROBE'],uuid.uuid4().hex+'.json').write_text(json.dumps(detail))
with open(os.environ['MODEL_LOG'],'a') as out: out.write(os.environ.get('BUREAU_CURRENT_ISSUE','?')+'\n')
if 'QA' in prompt and 'tests_added' in prompt:
    result = {'status':'NEEDS_HUMAN','tests_added':0,'tests_failing':0,'coverage_notes':'A product decision needs human attention'}
else:
    # The merger's schema (bureau-review.schema.json) requires the markdown comment and the findings list.
    result = {'verdict':'APPROVE','bugs':0,'security_issues':0,'missing_acceptance':[],'fixes_needed':[],'summary':'Fixture review passed',
              'comment':'## Specialist Summaries\nFixture review passed.','findings':[]}
pathlib.Path(sys.argv[sys.argv.index('-o')+1]).write_text(json.dumps(result))
print(json.dumps({'type':'turn.completed','usage':{'input_tokens':1,'output_tokens':1}}))
'''


class SupervisionPipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='bureau supervision ')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve(); self.build_fixture(self.root)
        states = {s:s for s in ('triage','spec','spec_review','design','build','qa','build_review','merge','done')}
        config = {'linear':{'teams':[{'id':'team','key':'T','name':'Test','states':states}], 'labels':{key:{'id':name,'name':name} for key,name in [('lane2','lane-2'),('needs_human','needs-human'),('needs_ux','needs-ux'),('ai_implementable','ai-implementable')]}},
                  'agents':{'runner':'codex','code_review':True,'qa':True},'repo':{'test_command':'python3 -m unittest discover -s tests'}}
        (self.repo/'.bureau.json').write_text(json.dumps(config)); (self.repo/'.env').write_text('LINEAR_API_KEY=fake\n')
        self.linear = self.root/'linear.json'
        self.items = {f'T-{n}':dict(id=f'issue-{n}',identifier=f'T-{n}',title=f'Task {n}',description='Original work',priority=n,
            createdAt=f'2026-09-0{n}T00:00:00Z',state={'id':'qa' if n==3 else 'build_review','name':'QA' if n==3 else 'Build Review'},
            branchName=f'{n:03d}-task',labels={'nodes':[{'name':'lane-2'},{'name':'ai-implementable'}]}, inverseRelations={'nodes':[]},
            comments={'nodes':[{'body':f'<!-- bureau-branch: {n:03d}-task -->','createdAt':'2026-09-01T00:00:00Z'}]}) for n in (1,2,3)}
        self.linear.write_text(json.dumps(self.items))
        binary = self.root/'bin';binary.mkdir()
        for name,body in [('curl',CURL),('gh',GH),('codex',CODEX)]:
            file=binary/name;file.write_text('#!'+sys.executable+'\n'+body);file.chmod(0o755)
        self.model_log=self.root/'models'; self.gh_log=self.root/'github'
        self.env={**os.environ,'PATH':str(binary)+os.pathsep+os.environ['PATH'],'BUREAU_CONFIG':str(self.repo/'.bureau.json'),
                  'LINEAR_FIXTURE':str(self.linear),'ORIGIN_FIXTURE':str(self.origin),'GH_LOG':str(self.gh_log),'MODEL_LOG':str(self.model_log),
                  'BUREAU_NO_MERGE':'0','BUREAU_STOP_REQUESTED':'0','BUREAU_DRY_RUN':'0','BUREAU_CODEX_USAGE_FILE':str(self.root/'none'),
                  'BUREAU_USAGE_FILE':str(self.root/'none')}
        for key in ('BUREAU_ACTIVE_ENTRY','BUREAU_RUN_ID','BUREAU_CURRENT_ISSUE'): self.env.pop(key,None)

    def build_fixture(self,root):
        """The fixture repository (main plus three task branches) and its bare origin, under root."""
        self.repo = root/'repo'; self.repo.mkdir(parents=True)
        self.git('init','-q','-b','main'); self.git('config','user.email','test@bureau'); self.git('config','user.name','Bureau Test')
        # In the repository's own config, so the stages' git calls get it too (git_maintenance.py).
        self.git(*git_maintenance.OFF_CONFIG)
        # The review build check runs repo.test_command in the worker; like a real Python
        # project, the fixture ignores the bytecode it writes.
        (self.repo/'.gitignore').write_text('.bureau.json\n.env\nlogs/\n.worktrees/\n__pycache__/\n')
        (self.repo/'tests').mkdir(); (self.repo/'tests/test_smoke.py').write_text('import unittest\nclass Smoke(unittest.TestCase):\n    def test_ok(self): self.assertEqual(2 + 3, 5)\n')
        shutil.copytree(ROOT/'templates/scripts',self.repo/'scripts',ignore=shutil.ignore_patterns('__pycache__'))
        if os.environ.get('BUREAU_TEST_TICK_SCRIPT'):
            shutil.copy(os.environ['BUREAU_TEST_TICK_SCRIPT'],self.repo/'scripts/bureau-tick.sh')
        self.git('add','.'); self.git('commit','-qm','test: fixture main')
        self.origin = root/'origin.git'; subprocess.run(['git','init','-q','--bare',str(self.origin)],check=True)
        subprocess.run(['git','--git-dir',str(self.origin),*git_maintenance.OFF_CONFIG],check=True)  # for receive-pack behind a push
        self.git('remote','add','origin',str(self.origin)); self.git('push','-q','origin','main')
        for number in (1,2,3):
            self.git('checkout','-qb',f'{number:03d}-task','main')
            (self.repo/f'task{number}.txt').write_text('Task implementation\n')
            specs=self.repo/f'specs/{number:03d}-task';specs.mkdir(parents=True);(specs/'tasks.md').write_text('- [X] T001 Add behavior\n')
            self.git('add','.'); self.git('commit','-qm','feat: fixture task\n\nBureau-Generated: true'); self.git('push','-q','origin','HEAD')
        self.git('checkout','-q','main')

    def git(self,*args):
        return subprocess.check_output(['git','-C',str(self.repo),*args],stderr=subprocess.STDOUT,text=True).strip()

    def test_fixture_commits_and_pushes_start_no_git_maintenance(self):
        # The whole fixture again, under the trace: the template commit, the origin and every push.
        trace=self.root/'trace2.json'
        with patch.dict(os.environ,{'GIT_TRACE2_EVENT':str(trace)}):
            self.build_fixture(self.root/'traced')
        commands=git_maintenance.commands(trace)
        self.assertTrue(any('commit' in argv for argv in commands),commands)  # both sides were traced
        self.assertTrue(any('receive-pack' in ' '.join(argv) for argv in commands),commands)
        self.assertEqual(git_maintenance.maintenance_started(trace),[])

    def run_tick(self,code=0,extra_env=None):
        proc=subprocess.run(['bash','scripts/bureau-tick.sh'],cwd=self.repo,env={**self.env,**(extra_env or {})},capture_output=True,text=True,timeout=90)
        self.assertEqual(proc.returncode,code,proc.stdout+'\n'+proc.stderr)
        return json.loads((self.repo/'logs/bureau-tick.json').read_text())

    def models(self):
        return self.model_log.read_text().splitlines() if self.model_log.exists() else []

    def test_workspace_rotation_does_not_bypass_other_preserved_work(self):
        for stage in ('spec','qa','code_review'):
            existing=self.repo/'.worktrees'/('tick-'+stage+'-T-1');existing.mkdir(parents=True)
            sentinel=existing/'unfinished.txt';sentinel.write_text('Keep this work')
            result=json.loads(subprocess.check_output(['python3','scripts/bureau-supervision.py','workspace','T-1','--stage',stage],cwd=self.repo,env=self.env,text=True))
            self.assertEqual(result['workspace'],str(existing))
            self.assertEqual(sentinel.read_text(),'Keep this work')

    def test_unpublished_main_merge_does_not_reopen_unchanged_review(self):
        (self.repo/'main-change.txt').write_text('Concurrent main work\n')
        self.git('add','main-change.txt'); self.git('commit','-qm','feat: main advances'); self.git('push','-q','origin','main')
        first=self.run_tick(20);self.assertEqual(first['issue'],'T-1')
        record=json.loads((self.repo/'.git/bureau/review-stops.json').read_text())['T-1']
        self.assertEqual(record['head'],self.git('rev-parse','origin/001-task'))
        self.assertEqual(record['base'],self.git('rev-parse','origin/main'))
        self.assertNotEqual(record['head'],record['reviewed_head'])
        count=self.models().count('T-1')
        second=self.run_tick(20);self.assertEqual(second['issue'],'T-2');self.assertEqual(second['skipped_reviews'],['T-1'])
        self.assertEqual(self.models().count('T-1'),count)
        preserved=self.repo/'.worktrees/tick-code_review-T-1'
        actual=subprocess.check_output(['git','-C',str(preserved),'rev-parse','HEAD'],text=True).strip()
        original_bytes={str(path.relative_to(preserved)):path.read_bytes() for path in preserved.rglob('*') if path.is_file()}
        self.assertEqual(actual,record['reviewed_head'])
        self.assertTrue((self.repo/'.worktrees/tick-code_review-T-2').is_dir())
        self.assertEqual(subprocess.check_output(['git','-C',str(preserved),'branch','--show-current'],text=True).strip(),'')
        subprocess.run(['python3','scripts/bureau-supervision.py','resume','T-1'],cwd=self.repo,env=self.env,check=True,stdout=subprocess.DEVNULL)
        self.assertEqual(self.run_tick(20)['issue'],'T-1')
        self.assertEqual(self.models().count('T-1'),count+4)
        self.assertTrue(list((self.repo/'.worktrees').glob('tick-code_review-T-1.*')))
        self.assertEqual(subprocess.check_output(['git','-C',str(preserved),'rev-parse','HEAD'],text=True).strip(),actual)
        # An authoritative base change reopens the boundary even if the GH PR
        # snapshot has not caught up. The preserved checkout remains untouched.
        (self.repo/'another-main-change.txt').write_text('More main work\n')
        self.git('add','another-main-change.txt');self.git('commit','-qm','feat: main advances again');self.git('push','-q','origin','main')
        detail={'identifier':'T-1','title':'Task 1','description':'Original work','labels':['lane-2','ai-implementable']}
        check=json.loads(subprocess.check_output(['python3','scripts/bureau-supervision.py','check','T-1','--branch','001-task','--state','Build Review'],
                                               cwd=self.repo,env=self.env,text=True,input=json.dumps(detail)))
        self.assertFalse(check['stopped'])
        self.assertEqual(subprocess.check_output(['git','-C',str(preserved),'rev-parse','HEAD'],text=True).strip(),actual)
        self.assertEqual(self.run_tick(20)['issue'],'T-1');self.assertEqual(self.models().count('T-1'),count+8)
        self.assertEqual(subprocess.check_output(['git','-C',str(preserved),'rev-parse','HEAD'],text=True).strip(),actual)
        self.git('checkout','-q','-B','001-task','origin/001-task')
        (self.repo/'new-head-work.txt').write_text('New PR work\n');self.git('add','new-head-work.txt')
        self.git('commit','-qm','fix: new PR head');self.git('push','-q','origin','HEAD');self.git('checkout','-q','main')
        self.assertEqual(self.run_tick(20)['issue'],'T-1');self.assertEqual(self.models().count('T-1'),count+12)
        self.assertEqual(subprocess.check_output(['git','-C',str(preserved),'rev-parse','HEAD'],text=True).strip(),actual)
        self.assertEqual({str(path.relative_to(preserved)):path.read_bytes() for path in preserved.rglob('*') if path.is_file()},original_bytes)


    def test_supervision_imports_nothing_from_the_branch(self):
        # v3.2: every Python start of the scripts carries -I. The worker's cleanup runs its checkpoint
        # in the stage worktree, as the review stage runs its check and stop there; the tick (check,
        # workspace) and reset_worktree's ownership check (bureau-runtime.py assert-owner) run in the
        # checkout the tick starts in. With an empty PYTHONPATH entry Python looked in the working
        # directory first and imported a module found there in place of the standard library. Each
        # module below records its name and argv, then hands over to the real one, so the run goes on.
        mark=self.root/'shadow.log'
        shadow=('import os, sys\n'
                f'with open({str(mark)!r}, "a") as _out: _out.write(__name__ + " " + " ".join(sys.argv) + "\\n")\n'
                '_name, _here, _saved = __name__, os.path.dirname(os.path.abspath(__file__)), sys.path[:]\n'
                'sys.path[:] = [p for p in sys.path if os.path.abspath(p or os.curdir) != _here]\n'
                'del sys.modules[_name]\n'
                'try:\n    import importlib\n    sys.modules[_name] = importlib.import_module(_name)\n'
                'finally:\n    sys.path[:] = _saved\n')
        # The doubles are Python scripts as well, and the review build check runs branch code on
        # purpose; neither is under test here.
        for name,body in [('curl',CURL),('gh',GH),('codex',CODEX)]:
            (self.root/'bin'/name).write_text('#!'+sys.executable+' -I\n'+body)
        config=json.loads((self.repo/'.bureau.json').read_text()); config['repo']['test_command']='true'
        (self.repo/'.bureau.json').write_text(json.dumps(config))
        self.git('checkout','-q','001-task')
        modules=[name+'.py' for name in ('argparse','json','subprocess','hashlib','tempfile','uuid')]
        for name in modules: (self.repo/name).write_text(shadow)
        self.git('add',*modules); self.git('commit','-qm','feat: modules named like the standard library\n\nBureau-Generated: true')
        self.git('push','-q','origin','HEAD'); self.git('checkout','-q','main')
        # The same modules, untracked, in the checkout the tick runs in.
        for name in modules: (self.repo/name).write_text(shadow)
        with (self.repo/'.git/info/exclude').open('a') as out: out.write(''.join(name+'\n' for name in modules))
        # main advances, so the review's local validation merge leaves the worktree ahead of origin
        # and the worker's cleanup keeps it as a checkpoint (bureau-worker.sh, rc 20).
        (self.repo/'main-change.txt').write_text('Concurrent main work\n')
        self.git('add','main-change.txt'); self.git('commit','-qm','feat: main advances'); self.git('push','-q','origin','main')
        empty_entry={'PYTHONPATH':':'}
        first=self.run_tick(20,empty_entry); self.assertEqual((first['issue'],first['outcome']),('T-1','stopped_for_review'))
        worktree=(self.repo/'.worktrees/tick-code_review-T-1').resolve()
        self.assertTrue(all((worktree/name).is_file() for name in modules))
        checkpoints=self.repo/'.git/bureau/review-checkpoints'
        key=hashlib.sha256(str(worktree).encode()).hexdigest()
        self.assertTrue((checkpoints/(key+'.json')).is_file(),list(checkpoints.glob('*')) if checkpoints.exists() else 'none')
        self.assertFalse(mark.exists(),mark.read_text() if mark.exists() else '')
        # Control: the v3.1 checkpoint call, without -I, imports the branch's modules in the worktree.
        worker=self.repo/'scripts/bureau-worker.sh'; text=worker.read_text()
        isolated='python3 -I "$SCRIPT_DIR/bureau-supervision.py" --repo "$WORKTREE" checkpoint'
        self.assertEqual(text.count(isolated),1); worker.write_text(text.replace(isolated,isolated.replace(' -I','')))
        subprocess.run(['python3','-I','scripts/bureau-supervision.py','resume','T-1'],cwd=self.repo,env=self.env,check=True,stdout=subprocess.DEVNULL)
        second=self.run_tick(20,empty_entry); self.assertEqual((second['issue'],second['outcome']),('T-1','stopped_for_review'))
        lines=mark.read_text().splitlines() if mark.exists() else []
        self.assertTrue(lines,'without -I the checkpoint call should have imported the branch modules')
        self.assertTrue(all('bureau-supervision.py --repo '+str(self.repo/'.worktrees') in line and ' checkpoint T-1' in line for line in lines),lines)
        self.assertIn('argparse',[line.split()[0] for line in lines])

    def test_real_stages_stop_skip_notify_and_resume(self):
        first=self.run_tick(20); self.assertEqual(first['issue'],'T-1'); self.assertEqual(first['outcome'],'stopped_for_review')
        self.assertEqual(self.models(),['T-1']*4)
        # Simulate a competing tick whose pick preceded the durable stop. The
        # actual worker rechecks after claiming the issue and invokes no model.
        stale=subprocess.run(['bash','scripts/bureau-worker.sh','T-1','code-review-pipeline.sh',str(self.repo/'.worktrees/tick-code_review-T-1'),'001-task'],
                             cwd=self.repo,env={**self.env,'BUREAU_NO_MERGE':'1'},capture_output=True,text=True,timeout=30)
        self.assertEqual(stale.returncode,20,stale.stdout+stale.stderr);self.assertEqual(self.models(),['T-1']*4)
        second=self.run_tick(20); self.assertEqual(second['issue'],'T-2'); self.assertEqual(second['skipped_reviews'],['T-1'])
        self.assertEqual(self.models().count('T-1'),4)
        third=self.run_tick(25);self.assertEqual(third['issue'],'T-3');self.assertEqual(third['outcome'],'blocked')
        self.assertEqual(third['before'],third['after']); self.assertEqual(third['skipped_reviews'],['T-1','T-2'])
        args=['python3','scripts/bureau-monitor.py','logs/bureau-tick.json','--previous','logs/previous.json']
        report=json.loads(subprocess.check_output(args,cwd=self.repo,env=self.env,text=True));self.assertTrue(report['notify'])
        report=json.loads(subprocess.check_output(args,cwd=self.repo,env=self.env,text=True));self.assertFalse(report['notify'])
        count=len(self.models());wait=self.run_tick();self.assertEqual(wait['outcome'],'waiting');self.assertEqual(len(self.models()),count)
        # Shared status/resume is accessible from another worktree, without
        # moving Linear or giving a tick merge authorization.
        worker=self.repo/'.worktrees/tick-code_review-T-1'
        status=json.loads(subprocess.check_output(['python3',str(self.repo/'scripts/bureau-supervision.py'),'--repo',str(worker),'status'],env=self.env,text=True))
        self.assertEqual(set(status['review_stops']),{'T-1','T-2'})
        subprocess.run(['python3','scripts/bureau-supervision.py','resume','T-1'],cwd=self.repo,env=self.env,check=True,stdout=subprocess.DEVNULL)
        self.assertEqual(self.run_tick(20)['issue'],'T-1');self.assertEqual(self.models().count('T-1'),8)
        # A pushed HEAD invalidates its approval boundary automatically.
        self.git('checkout','-q','001-task');(self.repo/'new-work.txt').write_text('New work\n');self.git('add','new-work.txt')
        self.git('commit','-qm','fix: new work\n\nBureau-Generated: true');self.git('push','-q','origin','HEAD');self.git('checkout','-q','main')
        self.assertEqual(self.run_tick(20)['issue'],'T-1');self.assertEqual(self.models().count('T-1'),12)
        # Material ticket edits reopen same-head reviews; bot digest comments do not.
        items=json.loads(self.linear.read_text());items['T-2']['description']='Updated acceptance criteria';self.linear.write_text(json.dumps(items))
        self.assertEqual(self.run_tick(20)['issue'],'T-2');self.assertEqual(self.models().count('T-2'),8)
        count=len(self.models());failed=self.run_tick(18,{'FAIL_GH_HEAD':'1'});self.assertEqual(failed['outcome'],'failed');self.assertEqual(len(self.models()),count)
        self.assertNotIn(['pr','merge'],[json.loads(line)[:2] for line in self.gh_log.read_text().splitlines()])
        self.assertEqual(json.loads((self.repo/'.git/bureau/leases.json').read_text()),{})
        manifests=[json.loads(p.read_text()) for p in (self.repo/'.git/bureau/evidence/v1/runs').glob('*/manifest.json')]
        self.assertTrue(manifests)
        reviews=[a for m in manifests for a in m['attempts'] if a['stage']=='code_review' and a['phase']=='stage' and a['receipts']]
        self.assertTrue(reviews)
        evidence=self.repo/'.git/bureau/evidence/v1'
        for review in reviews:
            phases={json.loads((evidence/path).read_text())['phase'] for path in review['receipts']}
            self.assertEqual(phases,{'correctness','security','performance','verdict'})
            gates=[g for g in review['gates'] if g['kind']=='gate']
            self.assertEqual(len(gates),1)
            self.assertNotIn('path',gates[0]); self.assertTrue(gates[0]['sha256'])
            self.assertEqual(gates[0]['normalized_exit'],0)
            self.assertTrue(gates[0]['head'])
            self.assertGreater(review['pr']['number'],0); self.assertNotIn('url',review['pr'])
            # The worker has already detached/released and REVIEW_TMP is gone.
            self.assertTrue(any(g['kind']=='artifact' for g in review['gates']))
        qas=[a for m in manifests for a in m['attempts'] if a['stage']=='qa' and a['phase']=='stage']
        self.assertEqual(len(qas),1)
        qa=qas[0]
        self.assertEqual(qa['pr']['number'],3)
        self.assertEqual(len([g for g in qa['gates'] if g['kind']=='gate']),2)
        self.assertTrue(any(g['kind']=='artifact' and g['command_id']=='qa.summary' for g in qa['gates']))
        shutil.rmtree(Path(qa['workspace'])/'logs',ignore_errors=True)
        self.assertTrue(all(g['sha256'] and 'path' not in g for g in qa['gates']))

    def test_manual_merge_observation_resolves_approval_with_authoritative_time(self):
        sup=load_script('supervision_waits',ROOT/'templates/scripts/bureau-supervision.py')
        common=self.repo/'.git/bureau'; now=[200.0]
        evidence=sup.e.Store(common,clock=lambda:now[0])
        evidence.open_wait('T-1','approval','decision','Review approved','maintainer',code=20,generation_id='a'*32)
        detail={'identifier':'T-1','title':'Task','description':'Original','labels':[]}
        head=self.git('rev-parse','HEAD')
        sup.save(common/'review-stops.json',{'T-1':dict(branch='001-task',state='Build Review',head=head,base=head,
                  pr=1,revision='a'*32,ticket_hash=sup.fingerprint(detail,'T-1'))})
        now[0]=300
        with patch.object(sup.e,'Store',return_value=evidence), patch.object(sup.subprocess,'check_output',
                   return_value=json.dumps({'state':'MERGED','mergedAt':'1970-01-01T00:04:10Z'})):
            result=sup.check(self.repo,common,'T-1','001-task','Build Review',detail)
        self.assertFalse(result['stopped'])
        wait=next(evidence.root.joinpath('waits').glob('*.json'))
        value=json.loads(wait.read_text())
        self.assertEqual(value['resolved_at'],250)
        self.assertEqual(value['resolution_observed_at'],300)
        self.assertEqual(evidence.waits('T-1')['tickets'][0]['waiting_seconds'],50)

    def test_stale_pr_observation_cannot_resolve_replacement_boundary(self):
        sup=load_script('stale_supervision',Path(os.environ.get('BUREAU_TEST_SUPERVISION_SCRIPT',ROOT/'templates/scripts/bureau-supervision.py')))
        common=self.repo/'.git/bureau'; now=[100.0]; evidence=sup.e.Store(common,clock=lambda:now[0])
        detail={'identifier':'T-1','title':'Task','description':'Original','labels':[]}
        head=self.git('rev-parse','HEAD')
        old=dict(branch='001-task',state='Build Review',head=head,base=head,pr=1,revision='a'*32,
                 ticket_hash=sup.fingerprint(detail,'T-1'))
        evidence.open_wait('T-1','approval','decision','Approved','maintainer',generation_id='a'*32,pr=1)
        sup.save(common/'review-stops.json',{'T-1':old})
        new=dict(old,revision='b'*32,pr=2)
        def racing_observation(*args,**kwargs):
            now[0]=150
            sup.save(common/'review-stops.json',{'T-1':new})
            evidence.open_wait('T-1','approval','decision','Approved newer PR','maintainer',generation_id='b'*32,pr=2)
            now[0]=200
            return json.dumps({'state':'MERGED','mergedAt':'1970-01-01T00:02:00Z'})
        with patch.object(sup.e,'Store',return_value=evidence),patch.object(sup.subprocess,'check_output',side_effect=racing_observation):
            sup.check(self.repo,common,'T-1','001-task','Build Review',detail)
        current=evidence.open_waits()
        self.assertEqual(len(current),1,'stale PR observation resolved a newer wait')
        self.assertEqual(current[0]['generation'],'b'*32)
        self.assertEqual(current[0]['reached_at'],150)
        self.assertEqual(sup.read(common/'review-stops.json')['T-1'],new)
        period=json.loads((evidence.root/'tickets/T-1.json').read_text())
        self.assertIsNone(period['terminal_observed_at'])

    def test_matching_pr_observation_only_resolves_its_own_wait_generation(self):
        sup=load_script('scoped_supervision',ROOT/'templates/scripts/bureau-supervision.py')
        common=self.repo/'.git/bureau'; now=[100.0]; evidence=sup.e.Store(common,clock=lambda:now[0])
        detail={'identifier':'T-1','title':'Task','description':'Original','labels':[]}; head=self.git('rev-parse','HEAD')
        record=dict(branch='001-task',state='Build Review',head=head,base=head,pr=1,revision='a'*32,ticket_hash=sup.fingerprint(detail,'T-1'))
        evidence.open_wait('T-1','approval','decision','Approved','maintainer',generation_id='a'*32,pr=1)
        sup.save(common/'review-stops.json',{'T-1':record})
        now[0]=150; evidence.open_wait('T-1','ownership','resource','Reconcile','workspace-owner')
        now[0]=200
        with patch.object(sup.e,'Store',return_value=evidence),patch.object(sup.subprocess,'check_output',return_value=json.dumps({'state':'MERGED','mergedAt':'1970-01-01T00:02:00Z'})):
            sup.check(self.repo,common,'T-1','001-task','Build Review',detail)
        self.assertEqual([v['key'] for v in evidence.open_waits()],['ownership'])
        self.assertIsNone(json.loads((evidence.root/'tickets/T-1.json').read_text())['terminal_observed_at'])

    def test_bounded_wait_sweep_closes_operator_completion_in_both_adopter_profiles(self):
        import time
        sup=load_script('sweep_evidence',ROOT/'templates/scripts/bureau-supervision.py')
        for profile,cap in (('ATV',2),('core',1)):
            with self.subTest(profile=profile):
                common=self.repo/'.git/bureau'
                shutil.rmtree(common/'evidence',ignore_errors=True)
                now=time.time(); evidence=sup.e.Store(common,clock=lambda:now-100)
                config=json.loads((self.repo/'.bureau.json').read_text())
                config['agents'].update({s:False for s in ('merge','rebase','code_review','qa','implement','copy','ux','spec_review','spec')})
                config['agents'].update(merge_mode='manual',max_concurrent_issues=cap)
                (self.repo/'.bureau.json').write_text(json.dumps(config))
                items={}
                for n in range(1,7):
                    evidence.clock=lambda n=n:now-100+n
                    evidence.open_wait(f'T-{n}','approval' if n==2 else 'ownership',
                                       'decision' if n==2 else 'resource','Operator work','maintainer',
                                       generation_id='a'*32 if n==2 else None,pr=2 if n==2 else None)
                    items[f'T-{n}']=dict(self.items['T-1'],identifier=f'T-{n}',id=f'issue-{n}',
                                        state={'id':'merge' if n in (2,4) else 'done','name':'Merge' if n in (2,4) else 'Done'})
                items['T-3']['completedAt']=datetime_from_epoch(now-20)
                self.linear.write_text(json.dumps(items)); before=self.linear.read_bytes()
                log=self.root/(profile+'-observations')
                extra={'WAIT_QUERY_LOG':str(log),'PR_REFS_JSON':json.dumps({'state':'MERGED','mergedAt':datetime_from_epoch(now-10)}),
                       'BUREAU_NO_MERGE':'1','BUREAU_STOP_REQUESTED':'1'}
                result=self.run_tick(0,extra)
                self.assertEqual(result['outcome'],'waiting'); self.assertEqual(self.models(),[])
                self.assertTrue(log.exists(),'wait sweep did not run')
                self.assertEqual(len(log.read_text().splitlines()),4,'wait sweep exceeded its per-tick ticket bound')
                checked={'T-'+re.search(r'number: \{ eq: (\d+)',query).group(1) for query in log.read_text().splitlines()}
                self.assertEqual(len(checked),4,'wait sweep repeated a ticket')
                records={v['issue']:v for v in (json.loads(p.read_text()) for p in (evidence.root/'waits').glob('*.json'))}
                self.assertEqual({i for i,v in records.items() if v['state']=='open'},(set(items)-checked)|{'T-4'})
                self.run_tick(0,extra)
                self.assertEqual([v['issue'] for v in evidence.open_waits()],['T-4'])
                records={v['issue']:v for v in (json.loads(p.read_text()) for p in (evidence.root/'waits').glob('*.json'))}
                self.assertIsNone(records['T-1']['resolved_at'])
                self.assertAlmostEqual(records['T-2']['resolved_at'],now-10,places=4)
                self.assertAlmostEqual(records['T-3']['resolved_at'],now-20,places=4)
                self.assertLessEqual(len(log.read_text().splitlines()),8)
                self.assertEqual(self.linear.read_bytes(),before,'wait observation mutated Linear')
                calls=[json.loads(line) for line in self.gh_log.read_text().splitlines()]
                self.assertTrue(all(call[:2]==['pr','view'] for call in calls))

    def test_real_manual_review_digests_private_artifacts_and_sweeps_done_ticket(self):
        sup=load_script('private_review_evidence',ROOT/'templates/scripts/bureau-supervision.py')
        sentinel='PRIVATE_REVIEW_DESCRIPTION_SENTINEL'
        self.set_merge_mode('manual',merge=False,rebase=False)
        # The fake provider quotes this into specialist/review artifacts; the
        # configured build also emits it. The whole shared store is scanned.
        binary=self.root/'bin/codex'; code=binary.read_text()
        code=code.replace("'summary':'Fixture review passed'", "'summary':"+repr(sentinel))
        code=code.replace("'comment':'## Specialist Summaries\\nFixture review passed.'", "'comment':"+repr(sentinel))
        binary.write_text(code)
        config=json.loads((self.repo/'.bureau.json').read_text());config['repo']['test_command']='printf '+sentinel
        (self.repo/'.bureau.json').write_text(json.dumps(config))
        result=self.run_tick(0); self.assertEqual(result['issue'],'T-1')
        evidence=sup.e.Store(self.repo/'.git/bureau'); wait=next(v for v in evidence.open_waits() if v['issue']=='T-1')
        self.assertEqual(wait['pr'],1); self.assertTrue(wait['generation'])
        for path in evidence.root.rglob('*'):
            if path.is_file(): self.assertNotIn(sentinel.encode(),path.read_bytes(),'PRIVACY_SENTINEL in '+str(path))
        self.assertTrue(any(sentinel.encode() in p.read_bytes() for p in (self.repo/'logs/provider-runs').rglob('*') if p.is_file()),'fixture did not actually emit private result text')
        items=json.loads(self.linear.read_text());items['T-1']['state']={'id':'done','name':'Done'};self.linear.write_text(json.dumps(items))
        config['agents'].update(code_review=False,qa=False);(self.repo/'.bureau.json').write_text(json.dumps(config))
        self.run_tick(0)
        self.assertFalse(any(v['issue']=='T-1' for v in evidence.open_waits()),'manual Done ticket was never observed')

    def test_terminal_observation_without_history_keeps_resolution_time_unknown(self):
        runtime=self.repo/'scripts/bureau-runtime.py'
        sup=load_script('supervision_unknown',ROOT/'templates/scripts/bureau-supervision.py')
        evidence=sup.e.Store(self.repo/'.git/bureau')
        wait=evidence.open_wait('T-1','approval','decision','Awaiting manual merge','maintainer',code=20)
        snapshot=dict(self.items['T-1'],state={'id':'done','name':'Done'})
        result=subprocess.run([sys.executable,str(runtime),'--repo',str(self.repo),'observe','T-1'],
                              input=json.dumps(snapshot),capture_output=True,text=True,env=self.env)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        value=json.loads((evidence.root/'waits'/ (wait['wait_id']+'.json')).read_text())
        self.assertEqual(value['state'],'resolved'); self.assertIsNone(value['resolved_at'])
        self.assertIsNone(value['events'][-1]['at']); self.assertIsNotNone(value['events'][-1]['observed_at'])

    # ── agents.merge_mode ────────────────────────────────────────────────
    def set_merge_mode(self, mode, merge_state=True, **agents):
        config=json.loads((self.repo/'.bureau.json').read_text())
        if mode is not None: config['agents']['merge_mode']=mode
        states=config['linear']['teams'][0]['states']
        if merge_state: states['merge']='merge'
        else: states.pop('merge',None)
        config['agents'].update(agents)
        (self.repo/'.bureau.json').write_text(json.dumps(config))

    def queue_worker(self, issue, branch):
        # What queue-loop.sh run_script does: the real worker, one fixed worktree per stage.
        return subprocess.run(['bash','scripts/bureau-worker.sh',issue,'code-review-pipeline.sh',str(self.repo/'.worktrees/queue-code-review'),branch],
                              cwd=self.repo,env=self.env,capture_output=True,text=True,timeout=90)

    def test_manual_without_merge_state_refuses_before_any_work(self):
        # main has moved: the path on which a review that ran would merge origin/main
        # locally and leave the shared queue worktree preserved (every later run: 21).
        (self.repo/'main-change.txt').write_text('Concurrent main work\n')
        self.git('add','main-change.txt'); self.git('commit','-qm','feat: main advances'); self.git('push','-q','origin','main')
        for mode in ('manual', 'Manual'):
            with self.subTest(mode=mode):
                self.set_merge_mode(mode, merge_state=False)
                linear_before=self.linear.read_bytes()
                for issue, branch in (('T-1','001-task'),('T-2','002-task'),('T-1','001-task')):
                    proc=self.queue_worker(issue, branch)
                    self.assertEqual(proc.returncode,24,proc.stdout+proc.stderr)
                    self.assertIn('linear.teams[0].states.merge is not set',proc.stderr)
                self.assertEqual(self.models(),[]); self.assertFalse(self.gh_log.exists())
                self.assertEqual(self.linear.read_bytes(),linear_before)
                self.assertFalse((self.repo/'.git/bureau/review-stops.json').exists())
                tick=subprocess.run(['bash','scripts/bureau-tick.sh','--allow-merge'],cwd=self.repo,env=self.env,capture_output=True,text=True,timeout=90)
                result=json.loads((self.repo/'logs/bureau-tick.json').read_text())
                self.assertEqual((tick.returncode,result['stage'],result['outcome']),(24,'code_review','failed'),tick.stderr)
                self.assertEqual(self.models(),[]); self.assertEqual(self.linear.read_bytes(),linear_before)
        # Negative controls: with a Merge state the same review runs and parks T-1 in
        # Merge; under auto without one the review runs too.
        self.set_merge_mode('manual', merge_state=True)
        proc=self.queue_worker('T-1','001-task')
        self.assertEqual(proc.returncode,0,proc.stdout+proc.stderr)
        self.assertEqual(json.loads(self.linear.read_text())['T-1']['state']['id'],'merge')
        self.assertEqual(self.models(),['T-1']*4)
        self.assertNotIn(['pr','merge'],[json.loads(line)[:2] for line in self.gh_log.read_text().splitlines()])
        config=json.loads((self.repo/'.bureau.json').read_text()); config['agents'].pop('merge_mode')
        config['linear']['teams'][0]['states'].pop('merge'); (self.repo/'.bureau.json').write_text(json.dumps(config))
        proc=self.queue_worker('T-2','002-task')
        self.assertNotEqual(proc.returncode,24,proc.stdout+proc.stderr); self.assertIn('T-2',self.models())

    def test_manual_never_dispatches_merge_or_rebase(self):
        items=json.loads(self.linear.read_text()); items['T-1']['state']={'id':'merge','name':'Merge'}; self.linear.write_text(json.dumps(items))
        self.set_merge_mode('manual', merge=True, rebase=True)
        stages=[]
        for _ in range(3):
            subprocess.run(['bash','scripts/bureau-tick.sh','--allow-merge'],cwd=self.repo,env=self.env,capture_output=True,text=True,timeout=90)
            stages.append(json.loads((self.repo/'logs/bureau-tick.json').read_text())['stage'])
        for stage in ('merge','rebase'):
            forced=subprocess.run(['bash','scripts/bureau-tick.sh','--allow-merge','--stage',stage],cwd=self.repo,env=self.env,capture_output=True,text=True,timeout=90)
            result=json.loads((self.repo/'logs/bureau-tick.json').read_text())
            self.assertEqual((forced.returncode,result['stage'],result['outcome']),(0,'','waiting'),forced.stderr)
        # T-2 is reviewed and parked, T-3 goes through QA; the parked tickets cost nothing.
        self.assertEqual(stages[:2],['code_review','qa'],stages); self.assertNotIn('merge',stages); self.assertNotIn('rebase',stages)
        self.assertEqual(json.loads(self.linear.read_text())['T-2']['state']['id'],'merge')
        self.assertNotIn(['pr','merge'],[json.loads(line)[:2] for line in self.gh_log.read_text().splitlines()])
        # Negative control: under auto the same tick goes to the merge stage first.
        self.set_merge_mode('auto')
        subprocess.run(['bash','scripts/bureau-tick.sh','--allow-merge'],cwd=self.repo,env=self.env,capture_output=True,text=True,timeout=90)
        self.assertEqual(json.loads((self.repo/'logs/bureau-tick.json').read_text())['stage'],'merge')


class WaitingRegisterTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='bureau waits '); self.addCleanup(self.temp.cleanup)
        self.module=load_script('waiting_evidence',Path(os.environ.get('BUREAU_TEST_EVIDENCE_SCRIPT',
                                         ROOT/'templates/scripts/bureau-evidence.py')))
        self.now=80.0
        self.store=self.module.Store(Path(self.temp.name)/'bureau',clock=lambda:self.now)
        self.store.begin_run('a'*32,'T-1','worker','1'*40)

    def wait(self,issue='T-1',key='approval',kind='decision',code=20):
        return self.store.open_wait(issue,key,kind,'A concrete reason','maintainer',code=code)

    def records(self):
        return [json.loads(p.read_text()) for p in (self.store.root/'waits').glob('*.json')]

    def test_repeated_stops_preserve_first_reached_across_restart(self):
        self.now=100; first=self.wait()
        self.now=130; second=self.wait()
        self.store=self.module.Store(Path(self.temp.name)/'bureau',clock=lambda:self.now)
        self.now=160; third=self.wait()
        self.assertEqual(first['wait_id'],second['wait_id']); self.assertEqual(first['wait_id'],third['wait_id'])
        self.assertEqual(third['reached_at'],100)
        self.assertEqual(third['events'],[{'event':'reached','at':100,'run_id':None}])
        self.assertEqual(third['last_observed_at'],160)
        self.now=180; self.store.resolve_waits('T-1',['approval'])
        result=self.store.waits('T-1')['tickets'][0]
        self.assertEqual(result['waiting_seconds'],80)
        self.assertAlmostEqual(result['waiting_share'],.8)
        self.assertEqual(result['open_waits'],[])
        self.assertIsNone(self.records()[0]['resolved_at'])

    def test_overlapping_waits_count_the_union_and_keep_tickets_separate(self):
        self.now=100; self.wait()
        self.now=120; self.wait(key='ci',kind='dependency',code=2)
        self.wait(issue='T-2',key='ownership',kind='resource',code=21)
        self.now=150; self.store.resolve_waits('T-1',['approval'])
        self.now=180; self.store.resolve_waits('T-1',['ci'],resolved_at=170,basis='authoritative-gate-event')
        result=self.store.waits()
        first,second=result['tickets']
        self.assertEqual(first['waiting_seconds'],70); self.assertAlmostEqual(first['waiting_share'],.7)
        self.assertEqual(first['resolution_times_unknown'],1)
        self.assertEqual(len(second['open_waits']),1)
        self.assertEqual(second['open_waits'][0]['responsible_actor'],'maintainer')
        self.assertEqual(second['open_waits'][0]['kind'],'resource')

    def test_unknown_or_impossible_resolution_time_is_never_invented(self):
        self.now=100; self.wait(key='policy',kind='compliance',code=25)
        self.now=200; self.store.resolve_waits('T-1',['policy'],resolved_at=50,terminal=True)
        value=self.records()[0]
        self.assertEqual(value['state'],'open','stale resolution timestamp closed wait'); self.assertIsNone(value['resolved_at'])
        self.assertIsNone(value['resolution_observed_at']); self.assertEqual(len(value['events']),1)
        self.now=300; self.assertEqual(self.store.waits('T-1')['tickets'][0]['waiting_seconds'],200)
        self.store.resolve_waits('T-1',['policy'],terminal=True)
        self.assertIsNone(self.records()[0]['resolved_at'])
        self.assertEqual(self.records()[0]['resolution_observed_at'],300)

    def test_merge_wait_ticks_keep_evidence_reached_and_existing_backoff_clock(self):
        sup=load_script('merge_wait_ticks',ROOT/'templates/scripts/bureau-supervision.py')
        common=Path(self.temp.name)/'bureau'
        with patch.object(sup.e,'Store',return_value=self.store),patch.object(sup.time,'time',side_effect=lambda:self.now):
            self.now=100; sup.merge_wait(common,'T-1','branch','1'*40,'not-yet')
            self.now=150; sup.merge_wait(common,'T-1','branch','1'*40,'not-yet')
            self.assertEqual(self.records()[0]['reached_at'],100)
            # stopped_at still drives the original scheduling/backoff behaviour.
            scheduling=json.loads((common/'merge-gate-waits.json').read_text())['T-1']
            self.assertEqual(scheduling['stopped_at'],150); self.assertEqual(scheduling['gate_waits'],2)
            self.now=200; sup.merge_wait(common,'T-1',clear=True)
        self.assertEqual(self.records()[0]['state'],'resolved')
        self.assertIsNone(self.records()[0]['resolved_at'])

if __name__=='__main__': unittest.main()
