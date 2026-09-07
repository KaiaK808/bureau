"""Real bounded ticks/stages/ownership/picker; only external services are faked."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]

CURL = r'''
import json, os, pathlib, re, sys
path = pathlib.Path(os.environ['LINEAR_FIXTURE'])
items = json.loads(path.read_text())
payload = json.loads(sys.argv[sys.argv.index('-d') + 1]); query = payload['query']; variables = payload.get('variables', {})
if 'viewer' in query:
    result = {'viewer': {'id': 'viewer'}}
elif 'issueLabels' in query:
    name = re.search(r'name: \{ eq: "([^"]+)"', query).group(1)
    result = {'issueLabels': {'nodes': [{'id': name}]}}
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
    branch = args[args.index('--head')+1]; print(int(branch.split('-')[0]))
elif args[:2] == ['pr', 'view']:
    number = int(args[2]); fields = args[args.index('--json')+1]
    metadata = {'state':'OPEN','baseRefName':os.environ.get('PR_BASE_REF','main'),'headRefName':f'{number:03d}-task'}
    if os.environ.get('PR_REFS_JSON') is not None: metadata = json.loads(os.environ['PR_REFS_JSON'])
    if fields == 'state':
        if '--jq' in args: print('OPEN')
        else:
            if os.environ.get('FAIL_GH_HEAD') == '1': sys.exit(1)
            print(json.dumps({'state':'OPEN'}))
    elif fields in ('baseRefName,headRefName','state,baseRefName','state,baseRefName,headRefName'):
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
    result = {'verdict':'APPROVE','bugs':0,'security_issues':0,'missing_acceptance':[],'fixes_needed':[],'summary':'Fixture review passed'}
pathlib.Path(sys.argv[sys.argv.index('-o')+1]).write_text(json.dumps(result))
print(json.dumps({'type':'turn.completed','usage':{'input_tokens':1,'output_tokens':1}}))
'''


class SupervisionPipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='bureau supervision ')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve(); self.repo = self.root/'repo'; self.repo.mkdir()
        self.git('init','-q','-b','main'); self.git('config','user.email','test@bureau'); self.git('config','user.name','Bureau Test')
        (self.repo/'.gitignore').write_text('.bureau.json\n.env\nlogs/\n.worktrees/\n')
        (self.repo/'tests').mkdir(); (self.repo/'tests/test_smoke.py').write_text('import unittest\nclass Smoke(unittest.TestCase):\n    def test_ok(self): self.assertEqual(2 + 3, 5)\n')
        shutil.copytree(ROOT/'templates/scripts',self.repo/'scripts',ignore=shutil.ignore_patterns('__pycache__'))
        self.git('add','.'); self.git('commit','-qm','test: fixture main')
        self.origin = self.root/'origin.git'; subprocess.run(['git','init','-q','--bare',str(self.origin)],check=True)
        self.git('remote','add','origin',str(self.origin)); self.git('push','-q','origin','main')
        for number in (1,2,3):
            self.git('checkout','-qb',f'{number:03d}-task','main')
            (self.repo/f'task{number}.txt').write_text('Task implementation\n')
            specs=self.repo/f'specs/{number:03d}-task';specs.mkdir(parents=True);(specs/'tasks.md').write_text('- [X] T001 Add behavior\n')
            self.git('add','.'); self.git('commit','-qm','feat: fixture task\n\nBureau-Generated: true'); self.git('push','-q','origin','HEAD')
        self.git('checkout','-q','main')
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

    def git(self,*args):
        return subprocess.check_output(['git','-C',str(self.repo),*args],stderr=subprocess.STDOUT,text=True).strip()

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


if __name__=='__main__': unittest.main()
