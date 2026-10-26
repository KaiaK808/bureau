#!/usr/bin/env python3
"""Local Git, real parsers/ownership, fake service transports; no paid calls."""
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

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = Path(os.environ.get('BUREAU_1748_SCRIPTS', ROOT / 'templates/scripts'))

SERVICE = r'''#!/usr/bin/env python3
import json, os, re, sys
from pathlib import Path
path = Path(os.environ['BUREAU_TEST_STATE'])
data = json.loads(path.read_text())
args = sys.argv[1:]
with path.with_suffix('.calls').open('a') as out: out.write(json.dumps([Path(sys.argv[0]).name, args]) + '\n')
with path.with_suffix('.pids').open('a') as out: out.write(str(os.getpid())+'\n')
if Path(sys.argv[0]).name == 'gh':
    if data.get('gh_fail'): sys.exit(1)
    endpoint = args[-1]
    prs = data.get('prs', [])
    if '?state=open' in endpoint:
        withdrawal = data.pop('withdraw_on_pull', None)
        if withdrawal:
            row = next(i for i in data['issues'] if i['identifier'] == withdrawal['issue'])
            row['state'] = withdrawal['state']
            path.write_text(json.dumps(data))
        size = data.get('pr_page_size', 100)
        print(json.dumps([prs[i:i+size] for i in range(0, len(prs), size)] or [[]]))
    else:
        number = int(endpoint.rsplit('/', 1)[-1])
        found = next(pr for pr in prs if pr['number'] == number)
        if data.get('moving_pr'): found = {**found, 'head': {**found['head'], 'sha': 'f'*40}}
        print(json.dumps([found]))
    sys.exit(0)
payload = json.loads(args[args.index('-d') + 1]); q = payload['query']; v = payload.get('variables', {})
items = data['issues']
size = data.get('linear_page_size', 100)
def connection(rows):
    index = int(v.get('after') or 0); batch = rows[index:index+size]; end = index + len(batch)
    return dict(nodes=batch, pageInfo=dict(hasNextPage=end < len(rows), endCursor=str(end)))
if data.get('linear_fail'):
    result = dict(errors=[dict(message='fixture unavailable')])
elif 'comments(first:100' in q:
    size = data.get('comment_page_size', 100)
    row = next(i for i in items if i['id'] == v['id'])
    rows = row.get('comments', [dict(body='<!-- bureau-branch: '+row['branch']+' -->', createdAt='2026-01-01')])
    if data.get('bad_comments'): rows = ['malformed']
    result = dict(data=dict(issue=dict(comments=connection(rows))))
elif 'labels(first:100' in q:
    size = data.get('label_page_size', 100)
    row = next(i for i in items if i['id'] == v['id'])
    result = dict(data=dict(issue=dict(labels=connection(row['labels']['nodes']))))
elif 'first:100, after:$after' in q:
    result = dict(data=dict(issues=connection(items)))
elif 'issueUpdate' in q:
    row = next(i for i in items if i['id'] == v['id'])
    row['state'] = dict(id=v['sid'], name=v['sid'], type='started')
    path.write_text(json.dumps(data)); result = dict(data=dict(issueUpdate=dict(success=True)))
elif 'viewer' in q:
    result = dict(data=dict(viewer=dict(id='test')))
elif 'issues(' in q:
    withdrawal = data.pop('withdraw_on_branch_lookup', None) if 'branchName' in q else None
    if withdrawal:
        row = next(i for i in items if i['identifier'] == withdrawal['issue'])
        row['state'] = withdrawal['state']
        path.write_text(json.dumps(data))
    number = re.search(r'number:\s*\{\s*eq:\s*(\d+)', q)
    rows = [i for i in items if not number or i['identifier'].endswith('-'+number[1])]
    state = re.search(r'state:\s*\{\s*id:\s*\{\s*eq:\s*"([^"]+)"', q)
    if state: rows = [i for i in rows if i['state']['id'] == state[1]]
    rows = [{**i, 'comments':dict(nodes=i.get('comments', [dict(body='<!-- bureau-branch: '+i['branch']+' -->', createdAt='2026-01-01')]))} for i in rows]
    result = dict(data=dict(issues=dict(nodes=rows)))
elif 'commentCreate' in q:
    result = dict(data=dict(commentCreate=dict(success=True)))
else:
    raise SystemExit('unexpected Linear query: '+q)
if data.get('bad_page') and 'first:100, after:$after' in q:
    result['data']['issues'].pop('pageInfo', None)
print(json.dumps(result), end='')
if '-w' in args:
    print(args[args.index('-w')+1].replace(r'\n','\n').replace('%{http_code}','200'),end='')
'''


def load(file):
    spec = importlib.util.spec_from_file_location('overlap_test_module', SCRIPTS / file)
    value = importlib.util.module_from_spec(spec); spec.loader.exec_module(value)
    return value


class Fixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='bureau-1748-test-', dir='/private/tmp')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name); self.repo = self.root / 'repo'; self.repo.mkdir()
        self.remote = self.root / 'origin.git'; self.bin = self.root / 'bin'; self.bin.mkdir()
        self.children = []; self.addCleanup(self.stop_children)
        self.run_cmd(['git', 'init', '-q', '--bare', str(self.remote)])
        self.g('init', '-q', '-b', 'main'); self.g('config', 'user.email', 'test@bureau'); self.g('config', 'user.name', 'Test')
        self.g('config','maintenance.auto','false'); self.g('config','gc.auto','0'); self.g('config','fetch.writeCommitGraph','false')
        self.g('remote', 'add', 'origin', str(self.remote))
        for name in ('pkg/a.py', 'pkg/b.py', 'lib/code.rb', 'cmd/tool/main.go', 'docs/site/page.html', 'CHANGELOG.md', 'README.md', 'Makefile', 'old name.py'):
            file = self.repo / name; file.parent.mkdir(parents=True, exist_ok=True); file.write_text('base\n')
        (self.repo / '.gitignore').write_text('.bureau.json\n.env\nlogs/\n')
        self.commit('base'); self.base = self.g('rev-parse', 'HEAD'); self.g('push', '-q', 'origin', 'main')
        states = {k:k for k in ('triage','spec','spec_review','design','copy','build','qa','build_review','merge','done','cancelled','duplicate','backlog')}
        self.config = dict(linear=dict(teams=[dict(key='EXP', id='t', name='Test', states=states)], labels=dict(lane2=dict(id='l1',name='lane-2'),needs_human=dict(id='l2',name='needs-human'),needs_ux=dict(id='l3',name='needs-ux'),ai_implementable=dict(id='l4',name='ai-implementable'))), repo=dict(specs_dir='specs',base_branch='main'),agents=dict(file_overlap_gate=True))
        self.cfg = self.repo / '.bureau.json'; self.cfg.write_text(json.dumps(self.config))
        (self.repo / '.env').write_text('LINEAR_API_KEY=fixture-only\n')
        for binary in ('curl', 'gh'):
            file = self.bin / binary; file.write_text(SERVICE); file.chmod(0o755)
        self.state = self.root / 'service.json'; self.data = dict(issues=[],prs=[]); self.save()
        self.env = {**os.environ, 'PATH':str(self.bin)+os.pathsep+os.environ['PATH'], 'BUREAU_CONFIG':str(self.cfg), 'BUREAU_TEST_STATE':str(self.state), 'BUREAU_LINEAR_RETRIES':'0', 'PYTHONDONTWRITEBYTECODE':'1', 'TMPDIR':str(self.root), 'BUREAU_EVIDENCE_DISABLED':'1'}
        for key in ('BUREAU_RUN_ID','BUREAU_CURRENT_ISSUE','BUREAU_ACTIVE_ENTRY','BUREAU_STAGE_ATTEMPT_ID','BUREAU_DRY_RUN','BUREAU_TICKET_CONTROL_DIR'):
            self.env.pop(key, None)

    def stop_children(self):
        for child in self.children:
            if child.poll() is None:
                child.terminate()
                try: child.wait(timeout=3)
                except subprocess.TimeoutExpired: child.kill(); child.wait(timeout=3)
            if child.stdout: child.stdout.close()
            if child.stderr: child.stderr.close()
        self.assertTrue(all(child.poll() is not None for child in self.children))
        if hasattr(self, 'state') and self.state.with_suffix('.pids').exists():
            for pid in set(map(int,self.state.with_suffix('.pids').read_text().splitlines())):
                try: os.kill(pid,0)
                except ProcessLookupError: continue
                self.fail('fake transport survived fixture: '+str(pid))

    def run_cmd(self, cmd, **kw):
        return subprocess.check_output(cmd, text=True, stderr=subprocess.PIPE, **kw).strip()

    def g(self, *args): return self.run_cmd(['git','-C',str(self.repo),*args])
    def commit(self, message): self.g('add','-A'); self.g('commit','-qm',message)
    def save(self): self.state.write_text(json.dumps(self.data))

    def ticket(self, number, paths, state='spec_review', custom='specs'):
        issue = 'EXP-'+str(number); branch = f'{number:03d}-exp-{number}'
        self.g('checkout','-qb',branch,'main')
        directory = self.repo / custom / branch; directory.mkdir(parents=True)
        text = '- [ ] T001 Update '+ ' and '.join('`'+path+'`' for path in paths) if paths else '- [ ] T001 Clarify behavior'
        (directory/'tasks.md').write_text(text+'\n')
        (directory/'spec.md').write_text('# Spec\n\n## Governance and security\nDecision: not required\nRationale: Only local files are changed.\n')
        (directory/'plan.md').write_text('# Plan\n')
        self.commit('tasks '+issue); head = self.g('rev-parse','HEAD'); self.g('push','-q','origin',branch); self.g('checkout','-q','main')
        self.data['issues'].append(dict(id=issue,identifier=issue,title='Local fixture',description='A task',project=None,branch=branch,state=dict(id=state,name=state,type='started'),labels=dict(nodes=[]),children=dict(nodes=[]),inverseRelations=dict(nodes=[])))
        self.save(); return issue,branch,head

    def cli(self, action, ticket, *args, repo=None, scripts=None):
        command=[sys.executable,'-I',str((scripts or SCRIPTS)/'bureau-admission.py'),'--repo',str(repo or self.repo),action,*ticket[:2],*args]
        return self.invoke(command)

    def invoke(self, command, env=None, cwd=None):
        child=subprocess.Popen(command,env=env or self.env,cwd=cwd,text=True,stdout=subprocess.PIPE,stderr=subprocess.PIPE,start_new_session=True)
        self.children.append(child)
        try: out,err=child.communicate(timeout=45)
        except subprocess.TimeoutExpired:
            os.killpg(child.pid,signal.SIGTERM); child.communicate(timeout=5)
            raise
        return subprocess.CompletedProcess(command,child.returncode,out,err)

    def expect(self, code, result):
        self.assertEqual(result.returncode,code,result.stdout+'\n'+result.stderr)
        return result

    def store(self): return json.loads((self.repo/'.git/bureau/admission/v1.json').read_text())

    def withdrawal_config(self):
        # Display names and service types deliberately disagree with the IDs.
        states = self.config['linear']['teams'][0]['states']
        states.update({key:'configured-'+key for key in
                       (*states, 'backlog', 'cancelled', 'duplicate')})
        self.cfg.write_text(json.dumps(self.config))
        for row in self.data['issues']:
            row['state'] = dict(id=states[row['state']['id']], name='Done', type='started')
        self.save()
        return states

    def test_restack_withdrawn_worker_preflight_overrides_overlap_and_evidence(self):
        a=self.ticket(1,['pkg/a.py'],state='build'); b=self.ticket(2,['pkg/a.py'])
        states=self.withdrawal_config(); self.expect(0,self.cli('admit',b))
        entry=self.root/'implement-pipeline.sh'; marker=self.root/'WRITER_STARTED'
        entry.write_text('#!/bin/bash\nprintf started > "$1"\n')
        env={**self.env,'BUREAU_EVIDENCE_DISABLED':'0'}
        for key,code in [('backlog',25),('done',0),('cancelled',26),('duplicate',26),('copy',25)]:
            with self.subTest(state=key):
                if key=='copy':
                    self.config['agents']['ticket_active_states']=['build','spec_review']
                    self.cfg.write_text(json.dumps(self.config))
                self.data['issues'][0]['state']['id']=states[key]; self.save()
                result=self.invoke([sys.executable,'-I',str(SCRIPTS/'bureau-runtime.py'),'--repo',str(self.repo),
                                    'exec','--issue',a[0],'--entry',str(entry),'--','bash',str(entry),str(marker)],env=env)
                self.expect(code,result); self.assertFalse(marker.exists())
                self.assertNotIn(a[0],self.store()['reservations'])
                common=self.repo/'.git/bureau'
                self.assertTrue(list((common/'ticket-control').glob('*/stop-'+str(code))))
                self.assertFalse(json.loads((common/'leases.json').read_text()))
                manifests=[json.loads(p.read_text()) for p in (common/'evidence').rglob('manifest.json')]
                attempts=[a for manifest in manifests for a in manifest['attempts']]
                self.assertTrue(any(p.get('normalized_exit')==code for p in attempts),attempts)
                self.assertFalse(any(p.get('normalized_exit')==29 for p in attempts),attempts)

    def test_restack_withdrawal_during_admission_beats_grant_and_wait(self):
        a=self.ticket(1,['pkg/a.py']); b=self.ticket(2,['pkg/a.py'])
        states=self.withdrawal_config()
        for has_holder in (False,True):
            if has_holder: self.expect(0,self.cli('admit',b))
            for key,code in [('backlog',25),('done',0),('cancelled',26)]:
                with self.subTest(holder=has_holder,state=key):
                    control=self.root/('during-'+str(has_holder)+'-'+key)
                    self.env['BUREAU_TICKET_CONTROL_DIR']=str(control)
                    self.data['issues'][0]['state']['id']=states['spec_review']
                    self.data['withdraw_on_pull']=dict(issue=a[0],state=dict(id=states[key],name='Build',type='started'))
                    self.save(); self.expect(code,self.cli('admit',a))
                    self.assertTrue((control/('stop-'+str(code))).exists())
                    if (self.repo/'.git/bureau/admission/v1.json').exists():
                        self.assertNotIn(a[0],self.store()['reservations'])
                    self.data=json.loads(self.state.read_text())
            self.env.pop('BUREAU_TICKET_CONTROL_DIR')

    def test_restack_latched_withdrawal_wins_after_ticket_returns_active(self):
        a=self.ticket(1,['pkg/a.py']); b=self.ticket(2,['pkg/a.py'])
        states=self.withdrawal_config(); self.expect(0,self.cli('admit',b))
        for key,code in [('backlog',25),('done',0),('cancelled',26)]:
            with self.subTest(state=key):
                control=self.root/('latched-'+key); self.env['BUREAU_TICKET_CONTROL_DIR']=str(control)
                self.data['issues'][0]['state']['id']=states[key]; self.save()
                result=self.invoke(['bash','-ec','source "$1"; bureau_ticket_checkpoint "$2" strict; echo UNEXPECTED',
                                    'checkpoint',str(SCRIPTS/'bureau-config.sh'),a[0]],cwd=self.repo)
                self.expect(code,result); self.assertNotIn('UNEXPECTED',result.stdout)
                self.assertTrue((control/('stop-'+str(code))).exists())
                self.data['issues'][0]['state']['id']=states['build']; self.save()
                self.expect(code,self.cli('admit',a))
                self.assertNotIn(a[0],self.store()['reservations'])

    def test_restack_build_move_consumes_withdrawal_after_its_state_guard(self):
        a=self.ticket(1,['pkg/a.py']); states=self.withdrawal_config()
        for key,code in [('backlog',25),('done',0),('cancelled',26)]:
            with self.subTest(state=key):
                self.data['issues'][0]['state']['id']=states['spec_review']
                self.data['withdraw_on_branch_lookup']=dict(issue=a[0],state=dict(id=states[key],name='Build',type='started'))
                self.save()
                env={**self.env,'BUREAU_CURRENT_ISSUE':a[0],'BUREAU_EXPECTED_STATE_ID':states['spec_review'],
                     'BUREAU_TICKET_CONTROL_DIR':str(self.root/('move-'+key))}
                result=self.invoke(['bash','-ec','source "$1"; move_issue "$2" "$BUREAU_STATE_BUILD"; echo UNEXPECTED',
                                    'move',str(SCRIPTS/'bureau-config.sh'),a[0]],env=env,cwd=self.repo)
                self.expect(code,result); self.assertNotIn('UNEXPECTED',result.stdout)
                self.data=json.loads(self.state.read_text())
                self.assertEqual(states[key],self.data['issues'][0]['state']['id'])
                self.assertNotIn('issueUpdate',self.state.with_suffix('.calls').read_text())

    def test_restack_withdrawn_reservation_waits_for_cooperative_stop_and_lease_release(self):
        a=self.ticket(1,['pkg/a.py'],state='build'); b=self.ticket(2,['pkg/a.py'])
        states=self.withdrawal_config(); self.expect(0,self.cli('admit',a))
        entry=self.root/'implement-pipeline.sh'
        entry.write_text('''#!/bin/bash
set -euo pipefail
source "$1/bureau-config.sh"
trap 'touch "$BUREAU_TEST_STOP/cleanup-started"; while [ ! -f "$BUREAU_TEST_STOP/allow-stop" ]; do sleep .03; done; touch "$BUREAU_TEST_STOP/cleanup-finished"' EXIT
touch "$BUREAU_TEST_STOP/writer-started"
while [ ! -f "$BUREAU_TEST_STOP/withdrawn" ]; do sleep .03; done
bureau_ticket_checkpoint "$BUREAU_CURRENT_ISSUE" strict
touch "$BUREAU_TEST_STOP/UNEXPECTED"
''')
        env={**self.env,'BUREAU_TEST_STOP':str(self.root)}
        child=subprocess.Popen([sys.executable,'-I',str(SCRIPTS/'bureau-runtime.py'),'--repo',str(self.repo),
                                'exec','--issue',a[0],'--entry',str(entry),'--','bash',str(entry),str(SCRIPTS)],
                               env=env,cwd=self.repo,text=True,stdout=subprocess.PIPE,stderr=subprocess.PIPE,start_new_session=True)
        self.children.append(child)
        def wait_for(name):
            deadline=time.monotonic()+20
            while not (self.root/name).exists() and time.monotonic()<deadline and child.poll() is None:
                time.sleep(.03)
            self.assertTrue((self.root/name).exists(),name)
        try:
            wait_for('writer-started')
            self.data['issues'][0]['state']['id']=states['backlog']; self.save(); (self.root/'withdrawn').touch()
            wait_for('cleanup-started')
            self.assertFalse((self.root/'cleanup-finished').exists())
            self.assertIn('issue:'+a[0],json.loads((self.repo/'.git/bureau/leases.json').read_text()))
            self.expect(29,self.cli('admit',b)); self.assertIn(a[0],self.store()['reservations'])
        finally:
            (self.root/'allow-stop').touch()
            out,err=child.communicate(timeout=20)
        self.expect(25,subprocess.CompletedProcess(child.args,child.returncode,out,err))
        self.assertTrue((self.root/'cleanup-finished').exists()); self.assertFalse((self.root/'UNEXPECTED').exists())
        self.assertEqual({},json.loads((self.repo/'.git/bureau/leases.json').read_text()))
        for _ in range(2): self.expect(0,self.cli('admit',b))
        self.assertNotIn(a[0],self.store()['reservations'])

    def test_restack_configured_inactive_holding_state_releases_without_reelection(self):
        a=self.ticket(1,['pkg/a.py'],state='copy'); b=self.ticket(2,['pkg/a.py'])
        states=self.withdrawal_config(); self.expect(0,self.cli('admit',a))
        self.config['agents']['ticket_active_states']=['spec_review','build']
        self.cfg.write_text(json.dumps(self.config))
        self.env['BUREAU_TICKET_CONTROL_DIR']=str(self.root/'inactive-copy')
        self.expect(25,self.invoke(['bash','-ec','source "$1"; bureau_ticket_checkpoint "$2" strict',
                                  'checkpoint',str(SCRIPTS/'bureau-config.sh'),a[0]],cwd=self.repo))
        self.env.pop('BUREAU_TICKET_CONTROL_DIR')
        self.expect(0,self.cli('admit',b))
        self.assertNotIn(a[0],self.store()['reservations'])
        self.assertEqual(states['copy'],self.data['issues'][0]['state']['id'])

    def test_restack_active_configured_ids_ignore_terminal_service_types(self):
        a=self.ticket(1,['pkg/a.py']); b=self.ticket(2,['pkg/a.py'])
        self.withdrawal_config()
        self.data['issues'][0]['state']['type']='completed'
        self.data['issues'][1]['state']['type']='canceled'; self.save()
        self.expect(0,self.cli('admit',a)); self.expect(29,self.cli('admit',b))
        self.assertIn(a[0],self.store()['reservations'])

    def change_tasks(self, ticket, paths):
        self.g('checkout','-q',ticket[1]); file = self.repo/self.config['repo']['specs_dir']/ticket[1]/'tasks.md'
        file.write_text('- [ ] Update '+' and '.join('`'+p+'`' for p in paths)+'\n'); self.commit('changed tasks'); self.g('push','-q','origin',ticket[1]); self.g('checkout','-q','main')

    def pull(self, ticket, number=1, change=None, rename=None):
        self.g('checkout','-q',ticket[1])
        if rename: self.g('mv',*rename)
        if change: (self.repo/change).write_text('changed\n')
        if change or rename: self.commit('implementation'); self.g('push','-q','origin',ticket[1])
        head = self.g('rev-parse','HEAD'); self.g('checkout','-q','main')
        pr = dict(number=number,head=dict(ref=ticket[1],sha=head,repo=dict(id=1)),base=dict(sha=self.base,repo=dict(id=1)),state='open',body='Bureau')
        self.data['prs'].append(pr); self.save(); return pr

    def test_disjoint_paths_admit_together_and_reservations_survive_evidence_pruning(self):
        a=self.ticket(1,['pkg/a.py']); b=self.ticket(2,['pkg/b.py'])
        self.expect(0,self.cli('admit',a)); self.expect(0,self.cli('admit',b))
        shutil.rmtree(self.repo/'.git/bureau/evidence',ignore_errors=True)
        c=self.ticket(3,['pkg/a.py']); self.expect(29,self.cli('admit',c))
        self.assertEqual(set(self.store()['reservations']),{'EXP-1','EXP-2'})

    def test_exact_directory_and_no_substring_overlap(self):
        a=self.ticket(1,['pkg/']); b=self.ticket(2,['pkg/a.py']); c=self.ticket(3,['otherpkg/a.py'])
        self.expect(0,self.cli('admit',a)); result=self.expect(29,self.cli('admit',b)); self.assertIn('EXP-1: pkg/a.py',result.stderr)
        self.expect(0,self.cli('admit',c)); self.expect(29,self.cli('admit',b))

    def test_unknown_scope_exclusive_both_directions(self):
        a=self.ticket(1,[]); b=self.ticket(2,['pkg/b.py']); c=self.ticket(3,['unresolved/**/*.rs'])
        self.expect(0,self.cli('admit',a)); self.assertIn('unknown scope',self.expect(29,self.cli('admit',b)).stderr)
        self.assertIn('unresolved glob',self.expect(29,self.cli('admit',c)).stderr)

    def test_unknown_candidate_waits_on_known_holder(self):
        a=self.ticket(1,['README.md']); b=self.ticket(2,[])
        self.expect(0,self.cli('admit',a)); self.expect(29,self.cli('admit',b))

    def test_changed_tasks_invalidates_scope_and_does_not_grant_expansion_on_retry(self):
        a=self.ticket(1,['pkg/a.py']); b=self.ticket(2,['pkg/b.py'])
        self.expect(0,self.cli('admit',a)); self.expect(0,self.cli('admit',b))
        self.change_tasks(a,['pkg/a.py','pkg/b.py'])
        for _ in range(2): self.expect(29,self.cli('admit',a))
        self.assertIn('pkg/b.py',self.store()['inventories'][a[0]]['paths'])

    def test_fix1_B1_pending_expansion_does_not_make_grantees_wait_on_each_other(self):
        a=self.ticket(1,['pkg/a.py']); b=self.ticket(2,['pkg/b.py'])
        results=[self.cli('admit',a), self.cli('admit',b)]
        self.change_tasks(a,['pkg/a.py','pkg/b.py'])
        for _ in range(2):
            results.extend([self.cli('admit',a), self.cli('admit',b)])
        self.assertEqual([r.returncode for r in results],[0,0,29,0,29,0],
                         [(r.returncode,r.stderr) for r in results])
        store=self.store()
        self.assertEqual(store['inventories'][a[0]]['paths'],['pkg/a.py','pkg/b.py'])
        self.assertEqual(store['reservations'][a[0]]['paths'],['pkg/a.py'])
        self.data['issues'][1]['state'].update(id='done',type='completed'); self.save()
        self.expect(0,self.cli('admit',a))
        self.assertEqual(self.store()['reservations'][a[0]]['paths'],['pkg/a.py','pkg/b.py'])

    def test_fix1_B2_bare_root_dotfiles_in_mixed_tasks_are_reserved(self):
        a=self.ticket(1,['lib/code.rb']); b=self.ticket(2,['.gitignore'])
        self.g('checkout','-q',a[1])
        (self.repo/'specs'/a[1]/'tasks.md').write_text('- [ ] Update lib/code.rb and .gitignore\n')
        self.commit('bare mixed task paths'); self.g('push','-q','origin',a[1]); self.g('checkout','-q','main')
        self.expect(0,self.cli('admit',a)); self.expect(29,self.cli('admit',b))
        self.assertIn('.gitignore',self.store()['reservations'][a[0]]['paths'])
        paths=load('bureau-paths.py')
        for dotfile in ('.gitignore','.env','.editorconfig','.prettierrc.json'):
            with self.subTest(dotfile=dotfile):
                result=paths.extract('Update lib/code.rb and '+dotfile+'.',['lib/code.rb'])
                self.assertTrue(result['paths'] is None or dotfile in result['paths'],result)

    def test_fix1_B2_root_scope_overlaps_all_paths_in_both_directions(self):
        a=self.ticket(1,['.']); b=self.ticket(2,['pkg/a.py'])
        self.expect(0,self.cli('admit',a)); self.expect(29,self.cli('admit',b))
        paths=load('bureau-paths.py')
        for root in ('.','./','././','/'):
            with self.subTest(root=root):
                scope=paths.extract('Update `'+root+'`',['pkg/a.py'])['paths']
                self.assertTrue(paths.intersection(scope,['pkg/a.py']))
                self.assertTrue(paths.intersection(['pkg/a.py'],scope))

    def test_fix1_B2_bare_root_scope_in_mixed_tasks_is_repository_wide(self):
        paths=load('bureau-paths.py')
        for root in ('.','./','././','/'):
            with self.subTest(root=root):
                scope=paths.extract('Update lib/code.rb and '+root,
                                    ['lib/code.rb','pkg/a.py'])['paths']
                self.assertTrue(paths.intersection(scope,['pkg/a.py']),scope)
                self.assertTrue(paths.intersection(['pkg/a.py'],scope),scope)

    def test_current_inventory_can_shrink_while_reservation_retains_old_paths(self):
        a=self.ticket(1,['pkg/a.py','pkg/b.py']); b=self.ticket(2,['pkg/b.py'])
        self.expect(0,self.cli('admit',a)); self.change_tasks(a,['pkg/a.py']); self.expect(29,self.cli('admit',b))
        self.assertEqual(self.store()['inventories'][a[0]]['paths'],['pkg/a.py'])
        self.assertEqual(self.store()['reservations'][a[0]]['paths'],['pkg/a.py','pkg/b.py'])

    def test_unavailable_branch_is_a_service_error_and_retains_prior_reservation(self):
        a=self.ticket(1,['pkg/a.py']); b=self.ticket(2,['pkg/b.py'])
        self.expect(0,self.cli('admit',a))
        self.run_cmd(['git','--git-dir',str(self.remote),'update-ref','-d','refs/heads/'+a[1]])
        self.expect(18,self.cli('admit',b)); self.assertIn(a[0],self.store()['reservations'])

    def test_root_extensionless_other_layouts_globs_and_literal_prefix(self):
        self.config['repo'].update(specs_dir='work',path_prefix_strip='repo/'); self.cfg.write_text(json.dumps(self.config))
        a=self.ticket(1,['repo/Makefile','repo/README.md','repo/lib/*.rb','repo/cmd/tool/'],custom='work')
        self.expect(0,self.cli('admit',a))
        scope=self.store()['inventories'][a[0]]
        self.assertEqual(scope['paths'],['Makefile','README.md','cmd/tool','lib/code.rb'])
        self.assertEqual(len(scope['tasks_digest']),64); self.assertEqual(len(scope['tasks_commit']),40)

    def test_plain_text_root_and_extensionless_paths_with_sentence_punctuation(self):
        a=self.ticket(1,['README.md']); b=self.ticket(2,['Makefile'])
        self.g('checkout','-q',a[1]); (self.repo/'Rakefile').write_text('root executable\n')
        (self.repo/'specs'/a[1]/'tasks.md').write_text('- [ ] T001 Update Makefile, Rakefile, README.md and lib/code.rb.\n')
        self.commit('plain task paths'); self.g('push','-q','origin',a[1]); self.g('checkout','-q','main')
        self.expect(0,self.cli('admit',a)); self.expect(29,self.cli('admit',b))
        self.assertEqual(self.store()['inventories'][a[0]]['paths'],['Makefile','README.md','Rakefile','lib/code.rb'])

    def test_generated_docs_and_changelog_remain_conflicts(self):
        a=self.ticket(1,['docs/site/','CHANGELOG.md']); b=self.ticket(2,['docs/site/page.html']); c=self.ticket(3,['CHANGELOG.md'])
        self.expect(0,self.cli('admit',a)); self.expect(29,self.cli('admit',b)); self.expect(29,self.cli('admit',c))

    def test_rename_both_paths_parked_pr_and_own_pr_exclusion(self):
        a=self.ticket(1,['new name.py'],state='triage'); self.pull(a,rename=('old name.py','new name.py'))
        b=self.ticket(2,['old name.py']); c=self.ticket(3,['new name.py'])
        self.assertIn('PR #1',self.expect(29,self.cli('admit',b)).stderr); self.expect(29,self.cli('admit',c))
        self.expect(0,self.cli('admit',a))

    def test_same_branch_name_in_a_fork_is_not_excluded_as_own_pr(self):
        a=self.ticket(1,['pkg/a.py']); b=self.ticket(2,['pkg/b.py'],state='triage')
        pr=self.pull(b,change='pkg/a.py'); pr['head']['ref']=a[1]; pr['head']['repo']['id']=2; self.save()
        self.assertIn('PR #1',self.expect(29,self.cli('admit',a)).stderr)

    def test_legacy_bureau_pr_with_unlisted_ticket_remains_a_holder(self):
        a=self.ticket(1,['pkg/a.py']); b=self.ticket(2,['pkg/b.py'],state='triage'); pr=self.pull(b,change='pkg/a.py')
        self.data['issues']=self.data['issues'][:1]; pr['head']['ref']='legacy/custom-feature'
        for marker in ('Implemented from tasks.md spec.','Draft PR — see Linear issue for status.'):
            pr['body']=marker+'\nGenerated with [Claude Code](https://claude.com/claude-code)'; self.save()
            self.assertIn('PR #1',self.expect(29,self.cli('admit',a)).stderr)

    def test_existing_project_scope_filters_states_but_never_reservations_or_prs(self):
        self.config['linear']['projects']=['repo-project']; self.cfg.write_text(json.dumps(self.config))
        a=self.ticket(1,['pkg/a.py']); b=self.ticket(2,['pkg/a.py'],state='build_review')
        self.data['issues'][0]['project']=dict(id='repo-project'); self.data['issues'][1]['project']=dict(id='other-project')
        self.data['issues'][1]['comments']=[]; self.save(); self.expect(0,self.cli('admit',a))
        self.pull(b,change='pkg/a.py'); self.assertIn('PR #1',self.expect(29,self.cli('admit',a)).stderr)
        self.data['prs']=[]; self.save()
        self.data['issues'][0]['project']=dict(id='other-project')
        d=self.ticket(3,['pkg/a.py']); self.data['issues'][-1]['project']=dict(id='repo-project'); self.save()
        self.assertIn(a[0],self.expect(29,self.cli('admit',d)).stderr)
        c=self.ticket(4,['pkg/a.py'],state='build_review'); self.data['issues'][-1]['project']=dict(id='repo-project'); self.data['issues'][-1]['comments']=[]
        self.save(); self.assertIn(c[0],self.expect(29,self.cli('admit',a)).stderr)
        self.data['issues'][1].pop('project'); self.save(); self.expect(27,self.cli('admit',a))

    def test_paginated_linear_comments_and_101_prs(self):
        a=self.ticket(1,['pkg/a.py']); b=self.ticket(2,['pkg/b.py']); self.data['linear_page_size']=1
        self.data['issues'][0]['comments']=[dict(body='irrelevant',createdAt='2025-01-01')]*101+[dict(body='<!-- bureau-branch: '+a[1]+' -->',createdAt='2026-01-01')]
        pr=self.pull(b,change='pkg/a.py'); self.data['prs']=[dict(pr,number=n,body='Bureau') for n in range(1,102)]; self.save()
        result=self.expect(29,self.cli('admit',a)); self.assertIn('PR #101',result.stderr)
        calls=[json.loads(line) for line in self.state.with_suffix('.calls').read_text().splitlines()]
        payloads=[json.loads(args[args.index('-d')+1]) for binary,args in calls if binary=='curl']
        self.assertTrue(any(v.get('variables',{}).get('after')=='100' for v in payloads))

    def test_service_failure_and_unavailable_diff_are_errors(self):
        a=self.ticket(1,['pkg/a.py']); b=self.ticket(2,['pkg/b.py']); pr=self.pull(b)
        self.data['linear_fail']=True; self.save(); self.expect(27,self.cli('admit',a)); self.data.pop('linear_fail')
        self.data['gh_fail']=True; self.save(); self.expect(18,self.cli('admit',a)); self.data.pop('gh_fail')
        pr['head']['sha']='e'*40; self.save(); self.expect(18,self.cli('admit',a))
        self.assertFalse((self.repo/'.git/bureau/admission/v1.json').exists())

    def test_incomplete_or_moving_snapshot_fails_closed(self):
        a=self.ticket(1,['pkg/a.py']); b=self.ticket(2,['pkg/b.py']); self.pull(b)
        self.data['bad_page']=True; self.save(); self.expect(27,self.cli('admit',a)); self.data.pop('bad_page')
        self.data['bad_comments']=True; self.save(); self.expect(27,self.cli('admit',a)); self.data.pop('bad_comments')
        self.data['moving_pr']=True; self.save(); self.expect(18,self.cli('admit',a))

    def test_paginated_human_labels_are_not_treated_as_absent(self):
        a=self.ticket(1,['pkg/a.py'])
        self.data['label_page_size']=100
        self.data['issues'][0]['labels']['nodes']=[dict(name='label-'+str(n)) for n in range(100)]+[dict(name='needs-human')]
        self.save(); self.expect(25,self.cli('admit',a))

    def simultaneous(self, scripts=SCRIPTS):
        a=self.ticket(1,['pkg/a.py']); b=self.ticket(2,['pkg/a.py'])
        barrier=self.root/'barrier'; barrier.mkdir()
        private=self.root/'private'; shutil.copytree(scripts,private)
        target=private/'bureau-admission.py'; code=target.read_text()
        code=code.replace('    def admit(self, issue, branch, local=False, continuation=False):\n', '    def admit(self, issue, branch, local=False, continuation=False):\n        import time\n        barrier = Path(os.environ["BUREAU_TEST_BARRIER"])\n        (barrier / issue).touch()\n        deadline = time.monotonic() + 5\n        while len(list(barrier.iterdir())) < 2:\n            if time.monotonic() > deadline: raise Stop(24, "barrier timeout")\n            time.sleep(.01)\n')
        target.write_text(code)
        env={**self.env,'BUREAU_TEST_BARRIER':str(barrier)}
        for ticket in (a,b):
            child=subprocess.Popen([sys.executable,'-I',str(target),'--repo',str(self.repo),'admit',*ticket[:2]],env=env,text=True,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
            self.children.append(child)
        results=[]
        for child in self.children[-2:]:
            out,err=child.communicate(timeout=25); results.append((child.returncode,out,err))
        return results

    def test_simultaneous_contenders_have_exactly_one_admission(self):
        results=self.simultaneous(); self.assertEqual(sorted(v[0] for v in results),[0,29],results)

    def test_build_rework_keeps_reservation(self):
        a=self.ticket(1,['pkg/a.py']); b=self.ticket(2,['pkg/a.py'])
        self.expect(0,self.cli('admit',a)); self.data['issues'][0]['state']['id']='build_review'; self.save()
        self.expect(0,self.cli('admit',a)); self.expect(29,self.cli('admit',b))

    def test_implementation_recheck_catches_a_new_external_pr(self):
        a=self.ticket(1,['pkg/a.py']); b=self.ticket(2,['pkg/b.py'],state='triage')
        self.expect(0,self.cli('admit',a)); self.pull(b,change='pkg/a.py')
        self.expect(29,self.cli('admit',a)); self.assertIn('EXP-1',self.store()['reservations'])

    def test_rework_route_retains_admission_and_writer_rechecks_new_pr(self):
        a=self.ticket(1,['pkg/a.py']); b=self.ticket(2,['pkg/b.py'],state='triage')
        self.expect(0,self.cli('admit',a)); self.data['issues'][0]['state']['id']='build_review'; self.pull(b,change='pkg/a.py')
        self.expect(0,self.cli('admit',a,'--continuation')); self.expect(29,self.cli('admit',a))

    def test_cancellation_releases_after_execution_ownership_stops_but_pr_holds(self):
        a=self.ticket(1,['pkg/a.py']); b=self.ticket(2,['pkg/a.py'])
        self.expect(0,self.cli('admit',a)); self.pull(a,change='pkg/a.py')
        self.data['issues'][0]['state'].update(id='cancelled',type='canceled'); self.save()
        lease=self.repo/'.git/bureau/leases.json'; lease.write_text(json.dumps({'issue:EXP-1':dict(run_id='a'*32,owner='app')}))
        self.expect(29,self.cli('admit',b)); self.assertIn('EXP-1',self.store()['reservations'])
        lease.write_text('{}'); self.assertIn('PR #1',self.expect(29,self.cli('admit',b)).stderr)
        self.assertNotIn('EXP-1',self.store()['reservations']); self.data['prs']=[]; self.save(); self.expect(0,self.cli('admit',b))
        self.expect(26,self.cli('admit',a))

    def test_existing_build_queued_tickets_elect_without_deadlock(self):
        a=self.ticket(1,['pkg/a.py'],state='build'); b=self.ticket(2,['pkg/a.py'],state='build')
        self.expect(0,self.cli('admit',b)); self.expect(29,self.cli('admit',a))
        self.assertEqual(self.store()['migration'],{'EXP-1':'queued','EXP-2':'queued'})
        self.data['issues'][1]['state'].update(id='done',type='completed'); self.save(); self.expect(0,self.cli('admit',a))

    def test_existing_build_unknown_scopes_queue_without_mutual_wait(self):
        a=self.ticket(1,[],state='build'); b=self.ticket(2,[],state='build')
        self.expect(0,self.cli('admit',a)); self.expect(29,self.cli('admit',b))
        self.assertEqual(self.store()['migration'],{'EXP-1':'queued','EXP-2':'queued'})

    def test_known_waiting_driver_is_queued_while_unknown_ownership_stays_held(self):
        a=self.ticket(1,['pkg/a.py'],state='build'); b=self.ticket(2,['pkg/a.py'],state='build'); c=self.ticket(3,['pkg/b.py'])
        self.expect(0,self.cli('admit',b))
        self.env['BUREAU_RUN_ID']='a'*32
        self.expect(29,self.cli('admit',a)); self.assertEqual(self.store()['waiters'][a[0]],'a'*32)
        lease=self.repo/'.git/bureau/leases.json'; lease.write_text(json.dumps({'issue:EXP-1':dict(run_id='a'*32,owner='background')}))
        self.env.pop('BUREAU_RUN_ID'); self.expect(0,self.cli('admit',c))
        self.assertNotIn(a[0],self.store()['reservations'])
        lease.write_text(json.dumps({'issue:EXP-1':dict(run_id='b'*32,owner='unknown')}))
        self.expect(29,self.cli('admit',b)); self.assertIn(a[0],self.store()['reservations'])

    def test_existing_build_source_writer_and_unknown_owner_are_holders(self):
        a=self.ticket(1,['pkg/a.py'],state='build'); b=self.ticket(2,['pkg/a.py'],state='build'); self.pull(a,change='pkg/a.py'); self.data['prs']=[]; self.save()
        self.expect(29,self.cli('admit',b)); self.assertEqual(self.store()['migration']['EXP-1'],'holder')
        self.data['issues'][0]['state'].update(id='done',type='completed'); self.save()
        lease=self.repo/'.git/bureau/leases.json'; lease.write_text(json.dumps({'issue:EXP-1':dict(run_id='a'*32,owner='app')}))
        self.expect(29,self.cli('admit',b))

    def test_review_merge_qa_and_copy_holders(self):
        for state in ('build_review','merge','qa','copy'):
            with self.subTest(state=state):
                # Already written Copy work must retain its footprint too.
                a=self.ticket(len(self.data['issues'])+1,['pkg/a.py'],state=state)
                self.pull(a,change='pkg/a.py'); self.data['prs']=[]; self.save()
                b=self.ticket(len(self.data['issues'])+1,['pkg/a.py'])
                self.expect(29,self.cli('admit',b))
                for row in self.data['issues']: row['state'].update(id='done',type='completed')
                self.save()

    def test_readiness_survives_wait_and_invalidates_on_tasks_or_ticket_edit(self):
        a=self.ticket(1,['pkg/a.py']); b=self.ticket(2,['pkg/a.py']); self.expect(0,self.cli('admit',b))
        self.g('checkout','-q',a[1]); self.expect(0,self.cli('ready',a,'spec_review')); self.expect(29,self.cli('admit',a,'--local'))
        self.expect(0,self.cli('ready-check',a,'spec_review')); self.data['issues'][0]['description']='changed'; self.save()
        self.expect(2,self.cli('ready-check',a,'spec_review')); self.expect(0,self.cli('ready',a,'spec_review'))
        self.g('checkout','-q','main'); self.change_tasks(a,['pkg/b.py']); self.g('checkout','-q',a[1])
        self.expect(2,self.cli('ready-check',a,'spec_review'))

    def test_uncommitted_or_missing_inventory_is_unknown(self):
        a=self.ticket(1,['pkg/a.py']); b=self.ticket(2,['pkg/b.py']); self.expect(0,self.cli('admit',b))
        self.g('checkout','-q',a[1]); file=self.repo/'specs'/a[1]/'tasks.md'; file.write_text('- [ ] `pkg/c.py`\n')
        self.assertIn('uncommitted tasks',self.expect(29,self.cli('admit',a,'--local')).stderr)

    def test_ownership_failure_stays_21_and_human_hold_keeps_pr(self):
        a=self.ticket(1,['pkg/a.py']); b=self.ticket(2,['pkg/a.py'])
        directory=self.repo/'.git/bureau'; directory.mkdir(exist_ok=True)
        (directory/'leases.json').write_text(json.dumps({'issue:EXP-1':dict(run_id='a'*32,owner='app')}))
        self.expect(21,self.cli('admit',a)); (directory/'leases.json').write_text('{}')
        self.data['issues'][0]['labels']['nodes']=[dict(name='needs-human')]; self.pull(a,change='pkg/a.py')
        self.expect(25,self.cli('admit',a)); self.expect(29,self.cli('admit',b))

    def test_gate_is_opt_in_and_invalid_config_fails_closed(self):
        a=self.ticket(1,['pkg/a.py']); self.config['agents']['file_overlap_gate']=False; self.cfg.write_text(json.dumps(self.config))
        self.data['linear_fail']=True; self.save(); self.expect(0,self.cli('admit',a)); self.assertFalse((self.repo/'.git/bureau/admission').exists())
        self.config['agents']['file_overlap_gate']='yes'; self.cfg.write_text(json.dumps(self.config)); self.expect(24,self.cli('admit',a))

    def test_record_and_resume_refresh_the_committed_task_identity(self):
        a=self.ticket(1,['Makefile']); self.g('checkout','-q',a[1]); tasks=self.repo/'specs'/a[1]/'tasks.md'
        self.expect(0,self.cli('record',a,str(tasks))); before=self.store()['inventories'][a[0]]
        self.expect(0,self.cli('record',a,str(tasks))); self.assertEqual(before,self.store()['inventories'][a[0]])
        self.g('checkout','-q','main'); self.change_tasks(a,['README.md']); self.g('checkout','-q',a[1])
        self.expect(0,self.cli('record',a,str(tasks))); after=self.store()['inventories'][a[0]]
        self.assertNotEqual(before['tasks_digest'],after['tasks_digest']); self.assertEqual(after['paths'],['README.md'])

    def test_current_workspace_prepare_waits_without_claim_reset_or_edits(self):
        a=self.ticket(1,['pkg/a.py'],state='build'); b=self.ticket(2,['pkg/a.py']); self.expect(0,self.cli('admit',b))
        app=self.root/'app workspace'; self.g('worktree','add','-q',str(app),a[1]); before=self.g('rev-parse',a[1])
        result=self.invoke([sys.executable,'-I',str(SCRIPTS/'bureau-runtime.py'),'--repo',str(app),'prepare',a[0],'implement'])
        self.expect(29,result); self.assertEqual(self.g('rev-parse',a[1]),before)
        self.assertEqual(self.run_cmd(['git','-C',str(app),'status','--porcelain']),'')
        leases=self.repo/'.git/bureau/leases.json'; self.assertFalse(leases.exists() and json.loads(leases.read_text()))

    def test_app_finish_reuses_prepared_readiness_after_wait(self):
        a=self.ticket(1,['pkg/a.py']); b=self.ticket(2,['pkg/a.py']); self.expect(0,self.cli('admit',b))
        self.g('checkout','-q',a[1])
        command=[sys.executable,'-I',str(SCRIPTS/'bureau-runtime.py'),'--repo',str(self.repo)]
        prepared=self.expect(0,self.invoke(command+['prepare',a[0],'spec_review']))
        context=json.loads(prepared.stdout); run=context['run_id']
        result=self.root/'result.json'; result.write_text(json.dumps(dict(run_id=run,stage='spec_review',outcome='complete',summary='ready',head=a[2],artifacts=['specs/'+a[1]+'/spec.md'])))
        for _ in range(2): self.expect(29,self.invoke(command+['finish',run,'--result',str(result)]))
        self.assertEqual(json.loads((self.repo/'.git/bureau/runs'/(run+'.json')).read_text())['status'],'prepared')
        self.data['issues'][1]['state'].update(id='done',type='completed'); self.save()
        self.expect(0,self.invoke(command+['finish',run,'--result',str(result)]))
        self.assertEqual(json.loads(self.state.read_text())['issues'][0]['state']['id'],'build')

    def test_build_move_guard_runs_before_linear_mutation(self):
        a=self.ticket(1,['pkg/a.py']); b=self.ticket(2,['pkg/a.py']); self.expect(0,self.cli('admit',b))
        result=self.invoke(['bash','-ec','source "$1"; move_issue "$2" build','bureau',str(SCRIPTS/'bureau-config.sh'),a[0]],cwd=self.repo)
        self.expect(29,result); self.assertEqual(json.loads(self.state.read_text())['issues'][0]['state']['id'],'spec_review')

    def test_background_worker_preflight_starts_no_writer_while_waiting(self):
        a=self.ticket(1,['pkg/a.py'],state='build'); b=self.ticket(2,['pkg/a.py']); self.expect(0,self.cli('admit',b))
        entry=self.root/'implement-pipeline.sh'; marker=self.root/'WRITER_STARTED'
        entry.write_text('#!/bin/bash\nprintf started > "$1"\n')
        result=self.invoke([sys.executable,'-I',str(SCRIPTS/'bureau-runtime.py'),'--repo',str(self.repo),'exec','--issue',a[0],'--entry',str(entry),'--','bash',str(entry),str(marker)])
        self.expect(29,result); self.assertFalse(marker.exists())

    def test_background_readiness_polling_does_not_buy_another_spec_review(self):
        a=self.ticket(1,['pkg/a.py']); b=self.ticket(2,['pkg/a.py']); self.expect(0,self.cli('admit',b))
        scripts=self.root/'driver/scripts'; shutil.copytree(SCRIPTS,scripts)
        paid=self.root/'paid'
        with (scripts/'bureau-config.sh').open('a') as out:
            out.write('\nprecondition_runner() { :; }\nrun_stage_for() { printf "paid\\n" >> "$BUREAU_TEST_PAID"; printf \'```json\\n{"review_status":"PASS","ui_work_needed":false,"summary":"ready"}\\n```\\n\'; }\n')
        env={**self.env,'BUREAU_WORKSPACE_MODE':'disposable','BUREAU_TEST_PAID':str(paid)}
        command=['bash',str(scripts/'spec-review-pipeline.sh'),a[0]]
        for _ in range(2): self.expect(29,self.invoke(command,env=env,cwd=self.repo))
        self.assertEqual(paid.read_text().splitlines(),['paid'])
        self.data['issues'][1]['state'].update(id='done',type='completed'); self.save()
        self.expect(0,self.invoke(command,env=env,cwd=self.repo)); self.assertEqual(paid.read_text().splitlines(),['paid'])
        self.assertEqual(json.loads(self.state.read_text())['issues'][0]['state']['id'],'build')

    def test_review_admission_wait_spends_no_round_or_provider_pass(self):
        a=self.ticket(1,['pkg/a.py']); b=self.ticket(2,['pkg/b.py'],state='triage')
        self.expect(0,self.cli('admit',a)); self.data['issues'][0]['state']['id']='build_review'; self.pull(b,change='pkg/a.py')
        scripts=self.root/'driver/scripts'; shutil.copytree(SCRIPTS,scripts); paid=self.root/'paid'
        with (scripts/'bureau-config.sh').open('a') as out:
            out.write('\nprecondition_runner() { :; }\nrun_stage_for() { printf "paid\\n" >> "$BUREAU_TEST_PAID"; return 22; }\n')
        env={**self.env,'BUREAU_WORKSPACE_MODE':'disposable','BUREAU_TEST_PAID':str(paid)}
        for _ in range(2):
            result=self.expect(29,self.invoke(['bash',str(scripts/'code-review-pipeline.sh'),a[0]],env=env,cwd=self.repo))
            self.assertNotIn('code-review failed',result.stderr)
        self.assertFalse(paid.exists())
        self.assertNotIn('bureau-review-round',self.state.with_suffix('.calls').read_text())

    def test_fix1_S1_from_stage_build_preserves_quiet_wait_and_manual_move(self):
        a=self.ticket(1,['pkg/a.py']); holder=self.ticket(2,['pkg/a.py'])
        self.expect(0,self.cli('admit',holder))
        scripts=self.root/'driver/scripts'; shutil.copytree(SCRIPTS,scripts)
        effects=self.root/'effects'
        with (scripts/'bureau-config.sh').open('a') as out:
            # Keep production move_issue/admission; record only the external writes.
            out.write("\nadd_issue_label() { printf 'label %s\\n' \"$2\" >> \"$BUREAU_TEST_EFFECTS\"; }\n"
                      "remove_issue_label() { printf 'release %s\\n' \"$2\" >> \"$BUREAU_TEST_EFFECTS\"; }\n"
                      "post_comment() { echo comment >> \"$BUREAU_TEST_EFFECTS\"; }\n"
                      "alert_telegram() { echo alert >> \"$BUREAU_TEST_EFFECTS\"; }\n"
                      "_shepherd_human_label() { :; }\n")
        env={**self.env,'BUREAU_ACTIVE_ENTRY':str(scripts/'shepherd.sh'),
             'BUREAU_TEST_EFFECTS':str(effects)}
        result=self.invoke(['/bin/bash',str(scripts/'shepherd.sh'),'--no-tmux','--from-stage','build',a[0]],env=env,cwd=self.repo)
        self.expect(29,result)
        self.assertEqual(effects.read_text().splitlines(),['label shepherd-focused','release shepherd-focused'])
        result=self.invoke(['/bin/bash','-c','source "$1"; move_issue "$2" "$BUREAU_STATE_BUILD"',
                            'move',str(scripts/'bureau-config.sh'),a[0]],env=env,cwd=self.repo)
        self.expect(29,result)
        self.assertEqual(effects.read_text().splitlines(),['label shepherd-focused','release shepherd-focused'])
        calls=self.state.with_suffix('.calls').read_text()
        self.assertNotIn('issueUpdate',calls)
        self.assertNotIn('commentCreate',calls)

    def test_fix1_S2_queue_skips_waiters_within_poll_and_retries_next_poll(self):
        holder=self.ticket(3,['pkg/a.py'],state='merge')
        a=self.ticket(1,['pkg/a.py']); b=self.ticket(2,['pkg/b.py'])
        self.expect(0,self.cli('admit',holder))
        for row in self.data['issues']:
            row.update(priority=int(row['identifier'].split('-')[1]),createdAt='2026-01-01')
        self.save()
        scripts=self.repo/'scripts'; shutil.copytree(SCRIPTS,scripts)
        with (self.repo/'.git/info/exclude').open('a') as out: out.write('\nscripts/\n')
        worker=scripts/'bureau-worker.sh'
        worker.write_text("#!/bin/bash\npython3 -I \"$(dirname \"$0\")/bureau-admission.py\" --repo \"$REPO_DIR\" admit \"$1\" \"$4\"\nrc=$?\nprintf '%s %s\\n' \"$1\" \"$rc\" >> \"$BUREAU_TEST_ATTEMPTS\"\nexit \"$rc\"\n")
        code=(scripts/'queue-loop.sh').read_text()
        functions=code[code.index('preselect_issue() {'):code.index('\nwhile true; do')]
        attempts=self.root/'attempts'; alerts=self.root/'alerts'
        driver=self.root/'queue-poll.sh'
        driver.write_text("""#!/bin/bash
set -euo pipefail
REPO_DIR="$PWD"; export REPO_DIR
source "$1/bureau-config.sh"
bureau_load_env --export "$BUREAU_ENV_FILE"
bureau_secret_copy API_KEY LINEAR_API_KEY
session_throttle_guard() { :; }
emit_event() { :; }
alert_telegram() { echo alert >> "$BUREAU_TEST_ALERTS"; }
LOG_FILE="$BUREAU_TEST_ATTEMPTS.log"; MODE=spec-review
"""+functions+"""
rc=0
run_one spec-review-pipeline.sh Review "$REPO_DIR/worker" || rc=$?
echo "poll=$rc"
""")
        env={**self.env,'BUREAU_TEST_ATTEMPTS':str(attempts),'BUREAU_TEST_ALERTS':str(alerts)}
        for _ in range(2):
            result=self.expect(0,self.invoke(['/bin/bash',str(driver),str(scripts)],env=env,cwd=self.repo))
            self.assertIn('poll=0',result.stdout,result.stdout+result.stderr)
        self.assertEqual(attempts.read_text().splitlines(),['EXP-1 29','EXP-2 0']*2)
        self.assertFalse(alerts.exists())
        self.data['issues'][0]['state'].update(id='done',type='completed'); self.save()
        self.expect(0,self.invoke(['/bin/bash',str(driver),str(scripts)],env=env,cwd=self.repo))
        self.assertEqual(attempts.read_text().splitlines()[-1],'EXP-1 0')
        # All-wait poll returns 29 after one attempt per candidate, never loops.
        self.ticket(4,['pkg/b.py'],state='build_review')
        self.data['issues'][0]['state'].update(id='merge',type='started'); self.save()
        attempts.write_text('')
        result=self.expect(0,self.invoke(['/bin/bash',str(driver),str(scripts)],env=env,cwd=self.repo))
        self.assertIn('poll=29',result.stdout)
        self.assertEqual(attempts.read_text().splitlines(),['EXP-1 29','EXP-2 29'])

    def test_fix1_S3_crosscheck_flag_off_keeps_legacy_extraction_and_matching(self):
        pr=self.ticket(3,['lib/code.rb'],state='triage'); self.pull(pr,change='lib/code.rb')
        a=self.ticket(1,['lib/code.rb']); self.g('checkout','-q',a[1])
        tasks=self.repo/'specs'/a[1]/'tasks.md'
        gh=self.bin/'gh'
        gh.write_text("#!/bin/bash\nprintf '3\\t003-exp-3\\tRuby change\\n'\n"); gh.chmod(0o755)
        command=['/bin/bash',str(SCRIPTS/'crosscheck-specs.sh'),str(tasks)]
        for enabled in (None,False,True):
            with self.subTest(enabled=enabled):
                if enabled is None: self.config['agents'].pop('file_overlap_gate',None)
                else: self.config['agents']['file_overlap_gate']=enabled
                self.cfg.write_text(json.dumps(self.config))
                result=self.invoke(command,cwd=self.repo)
                self.expect(3 if enabled else 0,result)
                self.assertIn('paths=1' if enabled else 'paths=0',result.stdout)
        # Legacy matching is substring-based, with literal prefix removal.
        self.g('checkout','-q',pr[1])
        (self.repo/'scripts').mkdir(); (self.repo/'scripts/tool.py').write_text('changed\n')
        self.commit('script PR'); self.g('push','-q','origin',pr[1]); self.g('checkout','-q',a[1])
        for enabled in (None,False,True):
            with self.subTest(prefix_enabled=enabled):
                if enabled is None: self.config['agents'].pop('file_overlap_gate',None)
                else: self.config['agents']['file_overlap_gate']=enabled
                self.config['repo']['path_prefix_strip']='repo/'
                self.cfg.write_text(json.dumps(self.config))
                # Legacy substring matching sees tool.py; opt-in prefix matching does not.
                tasks.write_text('- [ ] Update `./repo/scripts/tool`\n')
                result=self.invoke(command,cwd=self.repo); self.expect(0 if enabled else 3,result)

    def test_paths_reject_unknown_globs_traversal_and_ambiguous_scope(self):
        paths=load('bureau-paths.py')
        for text in ('`$ROOT/pkg/a.py`','`../pkg/a.py`','`missing/*.rs`','`unresolved`','no file references'):
            with self.subTest(text=text): self.assertIsNone(paths.extract(text,['pkg/a.py'])['paths'])
        self.assertEqual(paths.extract('`pkg/` and `Makefile`',['pkg/a.py'])['paths'],['Makefile','pkg'])
        self.assertEqual(paths.intersection(['a.py'],['aa.py']),[])


if __name__ == '__main__':
    unittest.main(verbosity=2)
