#!/usr/bin/env python3
"""Real local Git, trusted policy/ownership/workers, fake service transports."""
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
import zipfile

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = Path(os.environ.get('BUREAU_DISPATCH_TEST_SCRIPTS', ROOT / 'templates/scripts'))
REAL_GIT = shutil.which('git')
NAMES = dict(triage='Triage', spec='Spec', spec_review='Spec Review', design='Design', copy='Copy',
             build='Build', qa='QA', build_review='Build Review', merge='Merge', done='Done',
             backlog='Backlog', cancelled='Cancelled', duplicate='Duplicate')

SERVICE = r'''#!/usr/bin/env python3
import fcntl, json, os, re, subprocess, sys, time
from pathlib import Path
root = Path(os.environ['DISPATCH_FIXTURE']); args=sys.argv[1:]; binary=Path(sys.argv[0]).name
def event(kind, **kw):
    with (root/'events').open('a') as f: f.write(json.dumps(dict(kind=kind,pid=os.getpid(),at=time.time(),**kw))+'\n')
event('service', binary=binary)
def git(*a): return subprocess.check_output([os.environ['DISPATCH_REAL_GIT'], '--git-dir',str(root/'origin.git'),*a],text=True).strip()
with (root/'service.guard').open('a') as lock:
    fcntl.flock(lock,fcntl.LOCK_EX)
    data=json.loads((root/'service.json').read_text()); rows=data['issues']
    def save():
        p=root/'service.next'; p.write_text(json.dumps(data)); p.replace(root/'service.json')
    def row_for(q,v):
        if v.get('id'): return next(i for i in rows if i['id']==v['id'])
        number=re.search(r'number:\s*\{\s*eq:\s*(\d+)',q)
        return next(i for i in rows if number and i['identifier']=='TST-'+number[1])
    if binary=='curl':
        payload=json.loads(args[args.index('-d')+1]); q=payload['query']; v=payload.get('variables',{})
        if 'viewer' in q: result=dict(viewer=dict(id='viewer'))
        elif 'issueLabels' in q:
            name=re.search(r'name:\s*\{\s*eq:\s*"([^"]+)"',q).group(1)
            result=dict(issueLabels=dict(nodes=[dict(id='L:'+name,team=None)]))
        elif 'issueAddLabel' in q or 'issueRemoveLabel' in q:
            row=row_for(q,v); name=v['lid'][2:]; add='issueAddLabel' in q
            labels=row['labels']['nodes']; row['labels']['nodes']=[i for i in labels if i['name']!=name]+([dict(name=name)] if add else [])
            event('label',issue=row['identifier'],name=name,add=add); save()
            result={('issueAddLabel' if add else 'issueRemoveLabel'):dict(success=True)}
        elif 'issueUpdate' in q:
            row=row_for(q,v); sid=v['sid']; key=sid.removeprefix('id-')
            row['state']=dict(id=sid,name=data['names'][key],type='completed' if key=='done' else 'started')
            event('move',issue=row['identifier'],state=key); save(); result=dict(issueUpdate=dict(success=True))
        elif 'commentCreate' in q:
            row=next(i for i in rows if i['id']==v.get('iid',v.get('id')))
            body=v['body']; row['comments']['nodes'].insert(0,dict(body=body,createdAt='2026-10-26T00:00:00Z'))
            marker=re.search(r'<!-- bureau-branch: (\S+) -->',body)
            if marker: row['branch']=marker[1]
            event('comment',issue=row['identifier'],body=body); save(); result=dict(commentCreate=dict(success=True))
        elif 'comments(first:100' in q:
            row=row_for(q,v); result=dict(issue=dict(comments=dict(**row['comments'],pageInfo=dict(hasNextPage=False,endCursor=None))))
        elif 'labels(first:100' in q:
            row=row_for(q,v); result=dict(issue=dict(labels=dict(**row['labels'],pageInfo=dict(hasNextPage=False,endCursor=None))))
        else:
            selected=rows
            number=re.search(r'number:\s*\{\s*eq:\s*(\d+)',q)
            if number: selected=[i for i in selected if i['identifier']=='TST-'+number[1]]
            state=re.search(r'state:\s*\{\s*id:\s*\{\s*eq:\s*"([^"]+)"',q)
            if state: selected=[i for i in selected if i['state']['id']==state[1]]
            in_states=re.search(r'state:\s*\{\s*id:\s*\{\s*in:\s*(\[[^]]*\])',q)
            if in_states:
                ids=json.loads(in_states[1]); selected=[i for i in selected if i['state']['id'] in ids]
                if data.get('lag_capacity'): selected=[]
            labels=re.search(r'labels:\s*\{\s*some:\s*\{\s*name:\s*\{\s*in:\s*(\[[^]]*\])',q)
            if labels:
                allowed=set(json.loads(labels[1])); selected=[i for i in selected if allowed & {v['name'] for v in i['labels']['nodes']}]
            if data.get('duplicate_pick') and state: selected=selected*2
            if 'first:100, after:$after' in q:
                selected=[{k:i[k] for k in ('id','identifier','title','description','project','state','labels')} for i in selected]
            result=dict(issues=dict(nodes=selected,pageInfo=dict(hasNextPage=False,endCursor=None)))
            if data.get('slow_snapshot') and 'id identifier title description state' in q:
                event('slow-snapshot'); fcntl.flock(lock,fcntl.LOCK_UN)
                while not (root/'release-snapshot').exists(): time.sleep(.02)
        print(json.dumps(dict(data=result)),end='')
        if '-w' in args: print(args[args.index('-w')+1].replace('%{http_code}','200').replace(r'\n','\n'),end='')
        sys.exit(0)
    prs=data.get('prs',[])
    def pr_for():
        num=int(args[2]) if len(args)>2 and args[2].isdigit() else 1
        return next(p for p in prs if p['number']==num)
    def pr_value(p):
        head=git('rev-parse','refs/heads/'+p['branch']); base=git('rev-parse','refs/heads/main')
        return dict(number=p['number'],url='https://github.com/test/repo/pull/'+str(p['number']),state=p.get('state','OPEN'),
                    mergeStateStatus=p.get('merge_state','CLEAN'),labels=[],headRefName=p['branch'],headRefOid=head,
                    baseRefName='main',baseRefOid=git('merge-base',head,base),isDraft=False,title=p['issue']+' feature',body='Local fixture',comments=p.get('comments',[]))
    if args[:2]==['repo','view']: print('test/repo')
    elif args[:2]==['pr','list']:
        branch=args[args.index('--head')+1] if '--head' in args else None
        found=[p for p in prs if (not branch or p['branch']==branch) and p.get('state','OPEN')=='OPEN']
        if '--state' in args: found=[]
        if '--jq' in args:
            query=args[args.index('--jq')+1]
            if query=='.[0].number': print(found[0]['number'] if found else 'null')
            else: print(json.dumps(pr_value(found[0])) if found else 'null')
        else: print(json.dumps([pr_value(p) for p in found]))
    elif args[:2]==['pr','create']:
        branch=args[args.index('--head')+1] if '--head' in args else subprocess.check_output([os.environ['DISPATCH_REAL_GIT'],'branch','--show-current'],text=True).strip()
        row=next(i for i in rows if i['branch']==branch); num=int(row['identifier'].split('-')[1])
        prs.append(dict(number=num,branch=branch,issue=row['identifier'],comments=[])); data['prs']=prs; save()
        print('https://github.com/test/repo/pull/'+str(num))
    elif args[:2]==['pr','view']:
        p=pr_for(); value=pr_value(p); field=args[args.index('--json')+1]
        if '--jq' in args:
            query=args[args.index('--jq')+1]
            if field=='comments':
                key='Code Review v2' if 'Code Review v2' in query else 'Bureau merge gate'
                bodies=[c['body'] for c in value['comments'] if key in c['body']]
                print(bodies[-1] if bodies else '')
            else: print(value.get(field,''))
        else: print(json.dumps({k:value[k] for k in field.split(',') if k in value}))
        if data.get('withdraw_on_base_read') and field=='state,headRefOid,headRefName,baseRefName':
            row=next(i for i in rows if i['identifier']==p['issue']); row['state']=dict(id='id-backlog',name='Build',type='started'); save()
    elif args[:2]==['pr','comment']:
        p=pr_for(); body=args[args.index('--body')+1]; p.setdefault('comments',[]).append(dict(body=body,createdAt='2026-10-26T00:00:00Z')); save()
        event('pr-comment',issue=p['issue'],body=body)
    elif args[:2]==['pr','merge']:
        p=pr_for(); p['state']='MERGED'; save(); event('merge',issue=p['issue'])
    elif args[:1]==['api']:
        endpoint=next((a for a in args if a.startswith('repos/')),'')
        if 'graphql' in args: print('{"data":{"repository":{"pullRequest":{"reviewThreads":{"nodes":[]}}}}}')
        elif '/pulls?state=open' in endpoint:
            found=[]
            for p in prs:
                if p.get('state','OPEN')!='OPEN': continue
                v=pr_value(p); found.append(dict(number=p['number'],state='open',title=p['issue'],body='',
                    head=dict(ref=p['branch'],sha=v['headRefOid'],repo=dict(id=1,full_name='test/repo')),
                    base=dict(ref='main',sha=git('rev-parse','refs/heads/main'),repo=dict(id=1,full_name='test/repo'))))
            print(json.dumps([found]))
        elif '/check-runs' in endpoint:
            print(json.dumps(dict(check_runs=[dict(status='in_progress' if data.get('pending_ci') else 'completed',conclusion=None if data.get('pending_ci') else 'success',name='fixture-ci')])))
        elif '/pulls/' in endpoint:
            num=int(endpoint.rsplit('/',1)[-1]); p=next(p for p in prs if p['number']==num); v=pr_value(p)
            print(json.dumps([dict(number=num,state='open',title=p['issue'],body='',
                head=dict(ref=p['branch'],sha=v['headRefOid'],repo=dict(id=1,full_name='test/repo')),
                base=dict(ref='main',sha=git('rev-parse','refs/heads/main'),repo=dict(id=1,full_name='test/repo')))]))
        elif '/status' in endpoint: print('[]')
        elif '/branches/main' in endpoint: print(git('rev-parse','refs/heads/main'))
        elif '/compare/' in endpoint: print('1')
        else: print('{}')
    else: print('{}')
'''

PROVIDER = r'''#!/usr/bin/env python3
import json, os, signal, subprocess, sys, time
from pathlib import Path
root=Path(os.environ['DISPATCH_FIXTURE']); args=sys.argv[1:]
if args[:1]==['auth']: print('{"loggedIn":true}'); sys.exit(0)
if args[:1]==['--help']: print('--json-schema --disallowedTools'); sys.exit(0)
if args[:1]==['--version']: print('2.0.0'); sys.exit(0)
issue=os.environ['BUREAU_CURRENT_ISSUE']; phase=os.environ.get('BUREAU_PROVIDER_PHASE','implement'); run=os.environ['BUREAU_RUN_ID']
def event(kind,**kw):
    with (root/'events').open('a') as f: f.write(json.dumps(dict(kind=kind,issue=issue,phase=phase,run=run,pid=os.getpid(),at=time.time(),**kw))+'\n')
event('provider-start')
data=json.loads((root/'service.json').read_text()); row=next(i for i in data['issues'] if i['identifier']==issue)
if row.get('spawn_writer'):
    Path('unfinished-'+issue+'.txt').write_text('interrupted work must survive\n')
    writer="""import os,signal,time
from pathlib import Path
signal.signal(signal.SIGTERM,signal.SIG_IGN)
p=Path(os.environ['DISPATCH_FIXTURE'])
(p/'writer.pid').write_text(str(os.getpid()))
while True:
    with (p/'ticks').open('a') as f: f.write('tick\\n')
    time.sleep(.02)
"""
    child=subprocess.Popen([sys.executable,'-c',writer])
if row.get('hold'):
    while not (root/('release-'+issue)).exists(): time.sleep(.02)
if row.get('fail'): sys.exit(7)
if row.get('quota'):
    print('usage limit',file=sys.stderr); sys.exit(1)
if phase in ('correctness','security','performance'):
    result=dict(specialist=phase,counts=dict(critical=0,bug=0,minor=0,skip=0),findings=[],summary='clean')
elif phase=='verdict':
    result=dict(verdict='APPROVE',bugs=0,security_issues=0,findings=[],fixes_needed=[],missing_acceptance=[],summary='clean',comment='clean')
elif phase in ('specify','plan','tasks'):
    directory=Path(os.environ['SPECIFY_FEATURE_DIRECTORY']); directory.mkdir(exist_ok=True)
    names=dict(specify='spec.md',plan='plan.md',tasks='tasks.md')
    content='# Draft\n## Governance and security\nDecision: not required\nRationale: local files only.\n' if phase=='specify' else '# Draft\n'
    if phase=='tasks': content+='- [ ] T001 Update `'+row['path']+'`\n'
    (directory/names[phase]).write_text(content); result='artifacts written'
else:
    if row.get('human'):
        result=dict(status='NEEDS_HUMAN',tasks_done=0,tasks_skipped=0,tasks_needs_human=1,fixed_review_items=[],notes=dict(needs_human=[dict(task_id='T001',reason='operator decision')],skipped=[],deviations=[]),prose_notes='held')
    else:
        Path(row['path']).write_text('implemented '+issue+'\n')
        for p in Path('specs').glob('*/tasks.md'): p.write_text(p.read_text().replace('[ ]','[X]'))
        subprocess.run([os.environ['DISPATCH_REAL_GIT'],'add',row['path'],'specs'],check=True)
        subprocess.run([os.environ['DISPATCH_REAL_GIT'],'commit','-qm',issue+': implement','-m','Bureau-Generated: true'],check=True)
        result=dict(status='COMPLETE',tasks_done=1,tasks_skipped=0,tasks_needs_human=0,fixed_review_items=[],notes=dict(needs_human=[],skipped=[],deviations=[]),prose_notes='complete')
if row.get('withdraw_after_provider'):
    import fcntl
    with (root/'service.guard').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX); data=json.loads((root/'service.json').read_text()); row=next(i for i in data['issues'] if i['identifier']==issue)
        key=row['withdraw_after_provider']; row['state']=dict(id='id-'+key,name='Build',type='started')
        p=root/'service.next'; p.write_text(json.dumps(data)); p.replace(root/'service.json')
event('provider-end')
print(json.dumps(dict(result=json.dumps(result),structured_output=result)) if isinstance(result,dict) else json.dumps(dict(result=result)))
'''

GIT = r'''#!/usr/bin/env python3
import json, os, sys, time
from pathlib import Path
if 'push' in sys.argv[1:]:
    with (Path(os.environ['DISPATCH_FIXTURE'])/'events').open('a') as f:
        f.write(json.dumps(dict(kind='git-push',pid=os.getpid(),at=time.time(),args=sys.argv[1:]))+'\n')
os.execv(os.environ['DISPATCH_REAL_GIT'],[os.environ['DISPATCH_REAL_GIT'],*sys.argv[1:]])
'''


class DispatcherFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='bureau-dispatch-test-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve(); self.repo = self.root / 'repo with spaces'; self.repo.mkdir()
        self.children = []; self.files = []; self.addCleanup(self.cleanup)
        self.bin = self.root/'bin'; self.bin.mkdir()
        self.state = self.root/'service.json'
        self.data = dict(issues=[], prs=[], names=NAMES); self.save()
        self.env = {k:v for k,v in os.environ.items() if not k.startswith('BUREAU_')}
        self.env.update(PATH=str(self.bin)+os.pathsep+os.environ['PATH'], DISPATCH_FIXTURE=str(self.root),
                        DISPATCH_REAL_GIT=REAL_GIT, BUREAU_LINEAR_RETRIES='0', BUREAU_STOP_GRACE_SECONDS='1',
                        BUREAU_IMPL_MAX_ITER='1', PYTHONDONTWRITEBYTECODE='1', TMPDIR=str(self.root),
                        GIT_CONFIG_GLOBAL='/dev/null', GIT_CONFIG_NOSYSTEM='1', GIT_TERMINAL_PROMPT='0')
        for name,body in [('curl',SERVICE),('gh',SERVICE),('claude',PROVIDER),('git',GIT)]:
            file=self.bin/name; file.write_text(body); file.chmod(0o755)
        self.g('init','-q','-b','main'); self.g('config','user.name','test'); self.g('config','user.email','test@bureau')
        for key,value in [('maintenance.auto','false'),('gc.auto','0'),('fetch.writeCommitGraph','false')]: self.g('config',key,value)
        (self.repo/'.gitignore').write_text('/scripts/\n.bureau.json\n.env\n.worktrees/\nlogs/\n')
        (self.repo/'pkg').mkdir(); (self.repo/'pkg/a.py').write_text('base\n'); (self.repo/'pkg/b.py').write_text('base\n'); (self.repo/'pkg/c.py').write_text('base\n')
        with zipfile.ZipFile(ROOT/'tests/fixtures/speckit-1.2.2.zip') as z:
            for name in ['scripts/bash/common.sh','scripts/bash/create-new-feature.sh']: z.extract(name,self.repo/'.specify')
        self.commit('foundation')
        self.command([REAL_GIT,'init','-q','--bare',str(self.root/'origin.git')])
        self.g('remote','add','origin',str(self.root/'origin.git')); self.g('push','-q','origin','main')
        shutil.copytree(SCRIPTS,self.repo/'scripts')
        for file in (self.repo/'scripts').glob('*.sh'): file.chmod(0o755)
        self.config = dict(linear=dict(teams=[dict(key='TST',id='T',name='Test',states={k:'id-'+k for k in NAMES if k!='qa'})],
                                      labels=dict(lane2=dict(id='L:lane-2',name='lane-2'))),
                           repo=dict(base_branch='main',specs_dir='specs',test_command='true'),
                           agents=dict(runner='claude',spec=True,spec_review=False,implement=True,code_review=False,
                                       merge=True,file_overlap_gate=True,parallel_lanes=2))
        self.save_config(); (self.repo/'.env').write_text('LINEAR_API_KEY=fixture-only\n')
        self.common=self.repo/'.git/bureau'

    def command(self, args, **kw):
        return subprocess.check_output(args,text=True,stderr=subprocess.PIPE,env=self.env,**kw).strip()

    def g(self,*args): return self.command([REAL_GIT,'-C',str(self.repo),*args])
    def commit(self,message): self.g('add','-A'); self.g('commit','-qm',message)
    def save_config(self): (self.repo/'.bureau.json').write_text(json.dumps(self.config))

    def save(self): self.state.write_text(json.dumps(self.data))

    def change(self, mutation):
        import fcntl
        with (self.root/'service.guard').open('a') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX); self.data=json.loads(self.state.read_text()); mutation(self.data)
            file=self.root/'service.next'; file.write_text(json.dumps(self.data)); file.replace(self.state)

    def ticket(self, number, path=None, state='build', **extra):
        issue='TST-'+str(number); branch=f'{number:03d}-ticket-{number}'; path=path or 'pkg/'+chr(96+number)+'.py'
        if state!='triage':
            self.g('checkout','-qb',branch,'main')
            directory=self.repo/'specs'/branch; directory.mkdir(parents=True)
            (directory/'tasks.md').write_text('- [ ] T001 Update `'+path+'`\n')
            (directory/'spec.md').write_text('# Spec\n## Governance and security\nDecision: not required\nRationale: local files only.\n')
            (directory/'plan.md').write_text('# Plan\n'); self.commit('spec '+issue)
            self.g('push','-q','origin',branch); self.g('checkout','-q','main')
        comments=[] if state=='triage' else [dict(body='<!-- bureau-branch: '+branch+' -->',createdAt='2026-10-26T00:00:00Z')]
        row=dict(id=issue,identifier=issue,title='Ticket '+str(number),description='Local fixture',project=None,path=path,
                 branch='' if state=='triage' else branch,branchName='wrong-auto-branch',state=dict(id='id-'+state,name=NAMES[state],type='started'),
                 labels=dict(nodes=[dict(name='lane-2')]),children=dict(nodes=[]),inverseRelations=dict(nodes=[]),
                 comments=dict(nodes=comments),priority=number,createdAt='2026-10-26T00:00:00Z',**extra)
        self.change(lambda d:d['issues'].append(row))
        return issue,branch

    def events(self, kind=None):
        path=self.root/'events'
        rows=[json.loads(v) for v in path.read_text().splitlines()] if path.exists() else []
        return [v for v in rows if kind is None or v['kind']==kind]

    def wait_for(self, predicate, timeout=300):
        deadline=time.monotonic()+timeout
        while time.monotonic()<deadline:
            if predicate(): return
            time.sleep(.05)
        lanes='\n'.join(f.read_text()[-3500:] for f in (self.common/'dispatch-logs').glob('*.log')) if self.common.exists() else ''
        skips=self.common/'dispatch-skips.json'
        message='timeout; skips='+(skips.read_text() if skips.exists() else '{}')+'; output='+self.output()[-6500:]+'\n'+lanes[-14000:]
        print(message,file=sys.stderr,flush=True)
        self.fail(message)

    def start(self, poll=.1, scripts=None):
        path=(scripts or self.repo/'scripts')/'bureau-dispatch.py'
        self.assertTrue(path.is_file(),'continuous dispatcher is missing')
        output=(self.root/('dispatch-'+str(len(self.children))+'.out')).open('w'); self.files.append(output)
        child=subprocess.Popen([sys.executable,'-I',str(path),'--repo',str(self.repo),'run','--poll-seconds',str(poll)],
                               env=self.env,cwd=self.repo,stdout=output,stderr=subprocess.STDOUT,start_new_session=True)
        self.children.append(child); return child

    def invoke(self,command,env=None,timeout=40):
        child=subprocess.Popen(command,env=env or self.env,cwd=self.repo,text=True,
                               stdout=subprocess.PIPE,stderr=subprocess.PIPE,start_new_session=True)
        self.children.append(child)
        try: out,err=child.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            child.terminate(); child.communicate(timeout=25); raise
        return subprocess.CompletedProcess(command,child.returncode,out,err)

    def output(self): return '\n'.join(f.read_text() for f in self.root.glob('dispatch-*.out'))
    def started(self): return [v for v in self.events('provider-start') if v['phase']=='implement']
    def release(self,issue): (self.root/('release-'+issue)).touch()

    def stop(self,child):
        if child.poll() is None: child.terminate()
        self.assertEqual(child.wait(timeout=25),130,self.output())

    def cleanup(self):
        groups=set()
        if hasattr(self,'common'):
            for path in (self.common/'processes').glob('*.json'):
                if path.stem!='f'*32: groups.update(json.loads(path.read_text()).get('groups',[]))
        for child in self.children:
            if child.poll() is None:
                child.terminate()
                try: child.wait(timeout=25)
                except subprocess.TimeoutExpired: child.kill(); child.wait(timeout=3); self.fail('child did not stop')
        for file in self.files: file.close()
        pids={row['pid'] for row in self.events() if 'pid' in row}
        if (self.root/'writer.pid').exists(): pids.add(int((self.root/'writer.pid').read_text()))
        denied_pids=set()
        for pid in pids:
            try: os.kill(pid,0)
            except ProcessLookupError: continue
            except PermissionError:
                denied_pids.add(pid); continue
            self.fail('owned test process survived: '+str(pid))
        deadline=time.monotonic()+5
        denied_groups=set()
        group_count=len(groups)
        while groups and time.monotonic()<deadline:
            remaining=set()
            for group in groups:
                try: os.killpg(group,0)
                except ProcessLookupError: continue
                except PermissionError:
                    denied_groups.add(group); continue
                remaining.add(group)
            groups=remaining
            if groups: time.sleep(.05)
        if os.environ.get('DISPATCH_PROCESS_AUDIT'):
            record=dict(test=self._testMethodName,top_handles=len(self.children),transport_provider_pids=len(pids),
                        runtime_groups=group_count,remaining_groups=sorted(groups),
                        permission_denied_pids=sorted(denied_pids),permission_denied_groups=sorted(denied_groups))
            with open(os.environ['DISPATCH_PROCESS_AUDIT'],'a') as out: out.write(json.dumps(record)+'\n')
        if denied_pids or denied_groups:
            print('\nSKIP sandbox process inspection: EPERM for '+str(len(denied_pids))+' historical PID(s), '+str(len(denied_groups))+' historical group(s); live handles were joined, no stored ID was signalled',flush=True)
        self.assertFalse(groups,'owned runtime process groups survived: '+str(groups))

    def skipped(self,issue,code):
        file=self.common/'dispatch-skips.json'
        return file.exists() and json.loads(file.read_text()).get(issue,{}).get('code')==code

    def test_dynamic_arrival_duplicate_selection_and_fresh_identity(self):
        self.change(lambda d:d.update(duplicate_pick=True))
        foreign=self.root/'foreign-control'; foreign.mkdir(); (foreign/'stop-25').touch()
        self.env.update(BUREAU_RUN_ID='e'*32,BUREAU_RUN_DEPTH='99',BUREAU_ACTIVE_ENTRY='foreign',
                        BUREAU_STAGE_ATTEMPT_ID='d'*32,BUREAU_TICKET_CONTROL_DIR=str(foreign))
        child=self.start(); time.sleep(.4); self.ticket(1,hold=True)
        self.wait_for(lambda:len(self.started())==1)
        time.sleep(.3); self.assertEqual(len(self.started()),1)
        self.release('TST-1'); self.wait_for(lambda:self.skipped('TST-1',20))
        self.ticket(2,hold=True); self.wait_for(lambda:len(self.started())==2)
        self.assertEqual(len({row['run'] for row in self.started()}),2)
        self.assertNotIn('e'*32,{row['run'] for row in self.started()})
        self.assertTrue((foreign/'stop-25').exists())
        workspaces=[key for key in json.loads((self.common/'leases.json').read_text()) if key.startswith('workspace:')]
        self.assertTrue(all('/dispatch-TST-' in key for key in workspaces))
        self.stop(child)

    def test_two_lanes_third_waits_and_refills_without_poll_delay(self):
        for n in (1,2,3): self.ticket(n,hold=True)
        child=self.start(poll=120)
        self.wait_for(lambda:len(self.started())>=2)
        time.sleep(.3); self.assertEqual({v['issue'] for v in self.started()},{'TST-1','TST-2'})
        at=time.monotonic(); self.release('TST-1')
        self.wait_for(lambda:len(self.started())>=3)
        self.assertLess(time.monotonic()-at,120,'refill waited for the idle poll')
        alive=set(); maximum=0
        for event in self.events():
            if event['kind']=='provider-start' and event['phase']=='implement': alive.add(event['issue']); maximum=max(maximum,len(alive))
            elif event['kind']=='provider-end' and event['phase']=='implement': alive.discard(event['issue'])
        self.assertEqual(maximum,2)
        self.stop(child)

    def test_overlap_yields_and_disjoint_candidate_is_not_starved(self):
        self.ticket(1,'pkg/a.py',hold=True); self.ticket(2,'pkg/a.py',hold=True); self.ticket(3,'pkg/c.py',hold=True)
        child=self.start()
        self.wait_for(lambda:self.skipped('TST-2',29) and {v['issue'] for v in self.started()}=={'TST-1','TST-3'})
        self.assertFalse(any(v['name']=='needs-human' for v in self.events('label')))
        self.assertIn('exit=29',self.output()); self.stop(child)

    def test_dispatch_requires_explicit_overlap_gate_even_with_one_lane(self):
        self.ticket(1,'pkg/a.py',hold=True); self.ticket(2,'pkg/a.py',hold=True)
        for lanes in (2,1):
            for gate in ('absent',False,None,'true',1):
                with self.subTest(lanes=lanes,gate=gate):
                    self.config['agents']['parallel_lanes']=lanes
                    self.config['agents'].pop('file_overlap_gate',None)
                    if gate!='absent': self.config['agents']['file_overlap_gate']=gate
                    self.save_config(); child=self.start()
                    try:
                        self.wait_for(lambda:child.poll() is not None or bool(self.started()),timeout=90)
                        self.assertEqual(child.poll(),24,'dispatch started without an explicit true overlap gate: '+self.output())
                        self.assertIn('agents.file_overlap_gate',self.output())
                        self.assertFalse(self.started()); self.assertFalse(self.events('move'))
                    finally:
                        if child.poll() is None: self.stop(child)

    def test_pause_resume_and_one_shot_usage_throttle(self):
        self.ticket(1,hold=True); self.common.mkdir(parents=True); (self.common/'paused').touch()
        usage=self.root/'usage.json'; usage.write_text(json.dumps(dict(pct=99,updated_epoch=int(time.time()),reset_epoch=int(time.time())+300)))
        self.env['BUREAU_USAGE_FILE']=str(usage)
        child=self.start(); time.sleep(.5); self.assertFalse(self.started())
        (self.common/'paused').unlink(); self.wait_for(lambda:self.skipped('TST-1',23))
        self.assertFalse(self.started()); usage.write_text(json.dumps(dict(pct=0,updated_epoch=int(time.time()))))
        self.wait_for(lambda:bool(self.started())); self.stop(child)

    def test_provider_quota_yields_without_human_escalation(self):
        self.ticket(1,quota=True)
        child=self.start(); self.wait_for(lambda:self.skipped('TST-1',23))
        self.assertFalse(any(v['name']=='needs-human' for v in self.events('label')))
        self.stop(child)

    def test_disabled_stage_and_manual_merge_do_not_spend(self):
        self.ticket(1); self.config['agents']['implement']=False; self.config['agents']['merge_mode']='manual'; self.save_config()
        self.ticket(2,state='merge'); child=self.start(); time.sleep(1)
        self.assertFalse(self.events('provider-start')); self.assertFalse(self.events('merge'))
        self.stop(child)
        # --respect-config must also refuse a named ticket, independent of the picker.
        result=self.invoke(['bash',str(self.repo/'scripts/shepherd.sh'),'--no-tmux','--respect-config','--worktree',str(self.root/'named'),'TST-1'],
                           env={**self.env,'BUREAU_FORCE_ALL_AGENTS':'1'})
        self.assertEqual(result.returncode,20,result.stdout+result.stderr); self.assertFalse(self.events('provider-start'))

    def test_named_shepherd_respect_config_checks_stage_before_work(self):
        self.ticket(1); self.config['agents']['implement']=False; self.save_config()
        result=self.invoke(['bash',str(self.repo/'scripts/shepherd.sh'),'--no-tmux','--respect-config','--worktree',str(self.root/'named'),'TST-1'],timeout=90)
        self.assertEqual(result.returncode,20,result.stdout+result.stderr)
        self.assertFalse(self.events('provider-start')); self.assertFalse(self.events('move'))

    def test_queue_dispatch_dry_run_preserves_caller_boundary(self):
        self.ticket(1)
        result=self.invoke(['bash',str(self.repo/'scripts/queue-loop.sh'),'dispatch','1','--no-merge','--dry-run'])
        self.assertEqual(result.returncode,0,result.stdout+result.stderr); self.assertIn('[DRY_RUN] dispatch candidate:',result.stdout)
        self.assertFalse(self.events('provider-start')); self.assertFalse(self.events('move'))
        self.assertFalse((self.common/'dispatch-skips.json').exists())
        result=self.invoke(['bash',str(self.repo/'scripts/queue-loop-supervised.sh'),'dispatch','1','--no-merge','--dry-run'])
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        self.assertFalse(self.events('provider-start')); self.assertFalse(self.events('move'))

    def test_dry_run_excludes_unreconciled_lane_without_acknowledging_it(self):
        issue,_=self.ticket(1); attempt='a'*32
        journal=self.common/'dispatch-lanes'; journal.mkdir(parents=True)
        path=journal/(attempt+'.json')
        record=dict(attempt=attempt,issue=issue,stage='implement',code=25,
                    workspace=str((self.repo/'.worktrees'/('dispatch-'+issue+'-'+attempt)).resolve()))
        path.write_text(json.dumps(record))
        result=self.invoke([sys.executable,'-I',str(self.repo/'scripts/bureau-dispatch.py'),'--repo',str(self.repo),'run','--dry-run'])
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        self.assertIn('[DRY_RUN] dispatch candidate: queue empty',result.stdout)
        self.assertEqual(json.loads(path.read_text()),record)
        self.assertFalse((self.common/'dispatch-skips.json').exists())
        self.assertFalse(self.events('provider-start')); self.assertFalse(self.events('move'))

    def test_batch_reads_canonical_lane_setting_without_override(self):
        self.config['agents']['parallel_lanes']=1; self.save_config()
        command=['bash',str(self.repo/'scripts/orchestrate.sh'),'--execute','--chain','TST-1','--dry-run']
        result=self.invoke(command); self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        self.assertIn('cap 1',result.stdout)

    def test_one_lane_configuration_and_batch_compatibility_precedence(self):
        self.config['agents']['parallel_lanes']=1; self.save_config()
        self.ticket(1,hold=True); self.ticket(2,hold=True)
        child=self.start(); self.wait_for(lambda:len(self.started())==1)
        time.sleep(.3); self.assertEqual(len(self.started()),1)
        self.release('TST-1'); self.wait_for(lambda:len(self.started())==2); self.stop(child)
        command=['bash',str(self.repo/'scripts/orchestrate.sh'),'--execute','--chain','TST-1','--dry-run']
        self.assertIn('cap 1',self.invoke(command).stdout)
        self.assertIn('cap 3',self.invoke(command,env={**self.env,'BUREAU_MAX_CONCURRENT':'3'}).stdout)
        self.assertIn('cap 4',self.invoke(command+['--max-concurrent','4'],env={**self.env,'BUREAU_MAX_CONCURRENT':'3'}).stdout)

    def test_manual_merge_runs_review_then_hands_off(self):
        self.config['agents'].update(code_review=True,merge_mode='manual'); self.save_config()
        issue,branch=self.ticket(1,state='build_review')
        self.change(lambda d:d['prs'].append(dict(number=1,branch=branch,issue=issue,comments=[])))
        child=self.start(); self.wait_for(lambda:self.skipped(issue,20))
        self.assertEqual([v['state'] for v in self.events('move')],['merge'])
        self.assertEqual(len(self.events('provider-start')),4); self.assertFalse(self.events('merge')); self.stop(child)

    def test_human_hold_and_unknown_failure_are_not_immediately_repicked(self):
        self.ticket(1,human=True); self.ticket(2,fail=True)
        child=self.start(); self.wait_for(lambda:self.skipped('TST-1',25) and self.skipped('TST-2',22))
        count=len(self.started()); time.sleep(.6); self.assertEqual(len(self.started()),count)
        self.stop(child); child=self.start(); time.sleep(.6); self.assertEqual(len(self.started()),count); self.stop(child)

    def test_bare_human_exit_without_label_is_persistently_held(self):
        self.ticket(1)
        with (self.repo/'.env').open('a') as out: out.write('BUREAU_FORCE_ALL_AGENTS=1\n')
        file=self.repo/'scripts/implement-pipeline.sh'
        file.write_text('#!/bin/bash\nsource "$(dirname "$0")/bureau-config.sh"\nbureau_load_env --export "$BUREAU_ENV_FILE"\nbureau_stage_enter "$1" "$@"\nif agent_enabled qa; then exit 99; fi\nexit 25\n')
        child=self.start(); self.wait_for(lambda:self.skipped('TST-1',25)); self.stop(child)
        self.assertFalse(any(v['name']=='needs-human' for v in self.events('label')))
        before=len(list((self.common/'dispatch-logs').glob('*.log')))
        child=self.start(); time.sleep(.6); self.stop(child)
        self.assertEqual(len(list((self.common/'dispatch-logs').glob('*.log'))),before)

    def test_human_exit_survives_dispatcher_kill_before_lane_finishes(self):
        self.ticket(1)
        file=self.repo/'scripts/implement-pipeline.sh'
        file.write_text('#!/bin/bash\nsource "$(dirname "$0")/bureau-config.sh"\nbureau_stage_enter "$1" "$@"\n'
                        'echo "$BUREAU_RUN_ID" >> "$DISPATCH_FIXTURE/human-launches"\n'
                        'while [ ! -f "$DISPATCH_FIXTURE/release-human" ]; do sleep .05; done\nexit 25\n')
        child=self.start()
        try:
            self.wait_for(lambda:(self.root/'human-launches').exists())
            child.kill(); self.assertEqual(child.wait(timeout=5),-signal.SIGKILL)
        finally:
            (self.root/'release-human').touch()
        self.wait_for(lambda:not json.loads((self.common/'leases.json').read_text()))
        time.sleep(.2)  # The outer runtime finishes its journal before releasing dispatch.lock.
        restarted=self.start()
        self.wait_for(lambda:self.skipped('TST-1',25) or len((self.root/'human-launches').read_text().splitlines())>1,timeout=90)
        self.assertTrue(self.skipped('TST-1',25),'dispatcher restart lost the unlabeled human exit 25')
        self.assertEqual(len((self.root/'human-launches').read_text().splitlines()),1)
        self.assertIsNone(json.loads((self.common/'dispatch-skips.json').read_text())['TST-1']['until'])
        self.assertFalse(any(v['name']=='needs-human' for v in self.events('label')))
        self.stop(restarted)

    def test_restart_reconciliation_is_idempotent_and_holds_unfinished_launch(self):
        self.config['agents'].update(spec=False,implement=False); self.save_config()
        journal=self.common/'dispatch-lanes'; journal.mkdir(parents=True)
        prior=dict(code=23,attempts=3,until=time.time()+3600,attempt='a'*32)
        (self.common/'dispatch-skips.json').write_text(json.dumps({'TST-1':prior}))
        for number,attempt,code in [(1,'a'*32,23),(2,'b'*32,None)]:
            issue='TST-'+str(number)
            workspace=(self.repo/'.worktrees'/('dispatch-'+issue+'-'+attempt)).resolve()
            (journal/(attempt+'.json')).write_text(json.dumps(dict(attempt=attempt,issue=issue,stage='implement',
                                                                workspace=str(workspace),code=code)))
        child=self.start()
        self.wait_for(lambda:not list(journal.glob('*.json')),timeout=10)
        saved=json.loads((self.common/'dispatch-skips.json').read_text())
        self.assertEqual(saved['TST-1'],prior,'acknowledgment replay advanced the quiet backoff')
        self.assertEqual(saved['TST-2']['code'],130); self.assertIsNone(saved['TST-2']['until'])
        self.assertFalse(self.events('provider-start')); self.assertFalse(self.events('label'))
        self.stop(child)

    def test_pause_does_not_release_an_unlabeled_human_exit(self):
        self.ticket(1)
        file=self.repo/'scripts/implement-pipeline.sh'
        file.write_text('#!/bin/bash\nsource "$(dirname "$0")/bureau-config.sh"\nbureau_stage_enter "$1" "$@"\nwhile [ ! -f "$DISPATCH_FIXTURE/release-human" ]; do sleep .05; done\nexit 25\n')
        child=self.start(); self.wait_for(lambda:bool(list((self.common/'dispatch-ready').glob('*'))))
        (self.common/'paused').touch(); (self.root/'release-human').touch()
        self.wait_for(lambda:self.skipped('TST-1',25))
        self.assertIsNone(json.loads((self.common/'dispatch-skips.json').read_text())['TST-1']['until'])
        (self.common/'paused').unlink(); self.stop(child)

    def test_ticket_cap_one_drains_then_admits_next_triage(self):
        self.config['agents']['max_concurrent_issues']=1; self.save_config()
        self.ticket(1,state='triage',hold=True); self.ticket(2,state='triage',hold=True)
        child=self.start(); self.wait_for(lambda:any(v['phase']=='specify' for v in self.events('provider-start')))
        self.wait_for(lambda:self.skipped('TST-2',28))
        self.assertEqual([v['issue'] for v in self.events('move') if v['state']=='spec'],['TST-1'])
        self.release('TST-1'); self.wait_for(lambda:self.skipped('TST-1',20))
        self.change(lambda d:d['issues'][0].update(state=dict(id='id-done',name='Done',type='completed')))
        self.wait_for(lambda:any(v['issue']=='TST-2' and v['phase']=='specify' for v in self.events('provider-start')))
        self.stop(child)

    def test_atomic_cap_even_when_linear_count_lags_the_move(self):
        self.config['agents']['max_concurrent_issues']=1; self.save_config(); self.change(lambda d:d.update(lag_capacity=True))
        self.ticket(1,state='triage'); self.ticket(2,state='triage')
        outputs=[]
        for issue in ('TST-1','TST-2'):
            out=(self.root/(issue+'.out')).open('w'); self.files.append(out)
            entry=self.repo/'scripts/spec-pipeline.sh'
            code='source "$1/bureau-config.sh"; bureau_load_env --export "$BUREAU_ENV_FILE"; move_issue "$2" "$BUREAU_STATE_SPEC"; sleep 30'
            cmd=[sys.executable,'-I',str(self.repo/'scripts/bureau-runtime.py'),'--repo',str(self.repo),'exec','--issue',issue,
                 '--workspace',str(self.root/issue),'--entry',str(entry),'--','bash','-ec',code,'test',str(self.repo/'scripts'),issue]
            child=subprocess.Popen(cmd,cwd=self.repo,env=self.env,stdout=out,stderr=subprocess.STDOUT,start_new_session=True); self.children.append(child); outputs.append(child)
        self.wait_for(lambda:len([v for v in self.events('move') if v['state']=='spec'])>=1)
        self.wait_for(lambda:any(c.poll()==28 for c in outputs) or len([v for v in self.events('move') if v['state']=='spec'])>1,timeout=30)
        self.assertEqual(len([v for v in self.events('move') if v['state']=='spec']),1)
        self.wait_for(lambda:any(c.poll()==28 for c in outputs))
        self.assertEqual(len([v for v in self.events('move') if v['state']=='spec']),1)
        for c in outputs:
            if c.poll() is None: self.stop(c)

    def test_lock_excludes_second_dispatcher_and_legacy_poller(self):
        self.ticket(1,hold=True); child=self.start(); self.wait_for(lambda:bool(self.started()))
        second=self.start(); self.assertEqual(second.wait(timeout=10),21,self.output())
        result=subprocess.run(['bash',str(self.repo/'scripts/queue-loop.sh'),'implement','1'],env=self.env,cwd=self.repo,capture_output=True,text=True,timeout=10)
        self.assertEqual(result.returncode,21,result.stdout+result.stderr)
        self.stop(child)

    def test_running_legacy_poller_excludes_dispatcher(self):
        output=(self.root/'poller.out').open('w'); self.files.append(output)
        child=subprocess.Popen(['bash',str(self.repo/'scripts/queue-loop.sh'),'implement','1'],cwd=self.repo,
                               env=self.env,stdout=output,stderr=subprocess.STDOUT,start_new_session=True)
        self.children.append(child)
        try:
            self.wait_for(lambda:'Queue Worker Started' in (self.root/'poller.out').read_text())
            second=self.start(); self.assertEqual(second.wait(timeout=15),21,self.output())
        finally:
            try: os.killpg(child.pid,signal.SIGTERM)
            except ProcessLookupError: pass
            child.wait(timeout=10)

    def test_ownership_race_does_not_label_or_signal_live_foreign_owner(self):
        issue,_=self.ticket(1)
        file=self.repo/'scripts/bureau-dispatch.py'; text=file.read_text()
        needle='        try:\n            child = subprocess.Popen(command, cwd=self.repo, env=env, stdout=output,'
        self.assertEqual(text.count(needle),1)
        barrier="        probe = Path(os.environ['DISPATCH_FIXTURE'])\n        (probe/'before-runtime').touch()\n        while not (probe/'owner-ready').exists(): time.sleep(.02)\n"
        file.write_text(text.replace(needle,barrier+needle,1))
        dispatcher=self.start(); self.wait_for(lambda:(self.root/'before-runtime').exists())
        code="from pathlib import Path; import os,time; Path(os.environ['DISPATCH_FIXTURE'],'owner-ready').touch(); time.sleep(120)"
        holder=subprocess.Popen([sys.executable,'-I',str(self.repo/'scripts/bureau-runtime.py'),'--repo',str(self.repo),
                                 'exec','--issue',issue,'--workspace',str(self.root/'foreign-worker'),'--entry','foreign-holder',
                                 '--',sys.executable,'-c',code],cwd=self.repo,env=self.env,
                                stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,start_new_session=True)
        self.children.append(holder)
        self.wait_for(lambda:self.skipped(issue,21))
        self.assertIsNone(holder.poll()); self.assertFalse(any(v['name']=='needs-human' for v in self.events('label')))
        self.stop(dispatcher); self.assertIsNone(holder.poll()); self.stop(holder)

    def test_lane_retains_dispatch_lock_after_parent_loss(self):
        self.ticket(1,hold=True); child=self.start(); self.wait_for(lambda:bool(self.started()))
        child.kill(); self.assertEqual(child.wait(timeout=5),-signal.SIGKILL)
        self.ticket(2,hold=True)
        second=self.start(); self.assertEqual(second.wait(timeout=15),21,self.output())
        # A prior incarnation is never signalled. Let its owned lane finish
        # cooperatively, then a fresh dispatcher can claim the lock.
        self.release('TST-1')
        self.wait_for(lambda:'issue:TST-1' not in json.loads((self.common/'leases.json').read_text()))
        third=self.start(); self.wait_for(lambda:any(v['issue']=='TST-2' for v in self.started()))
        self.stop(third)

    def test_launchers_replace_pollers_and_supervisor_joins_dispatcher(self):
        self.config['agents'].update(spec=False,implement=False,code_review=False,merge=False,workbench_panes=0); self.save_config()
        tmux=self.bin/'tmux'; calls=self.root/'tmux.calls'
        tmux.write_text('#!/usr/bin/env python3\nimport json,os,sys\nfrom pathlib import Path\n'
                        'p=Path(os.environ["DISPATCH_FIXTURE"])/"tmux.calls"\n'
                        'with p.open("a") as f: f.write(json.dumps(sys.argv[1:])+"\\n")\n'
                        'sys.exit(1 if sys.argv[1]=="has-session" else 0)\n'); tmux.chmod(0o755)
        for launcher,args in [('start-agents.sh',['1']),('start-bureau-v2.sh',['1','0'])]:
            calls.write_text(''); result=self.invoke(['bash',str(self.repo/'scripts'/launcher),*args])
            self.assertEqual(result.returncode,0,result.stdout+result.stderr)
            commands=[row for row in map(json.loads,calls.read_text().splitlines()) if row[0]=='send-keys' and any('queue-loop' in a for a in row)]
            self.assertEqual(len(commands),1,commands); self.assertTrue(any('queue-loop-supervised.sh dispatch 1' in a for a in commands[0]))
        self.config['agents']['implement']=True; self.save_config(); self.ticket(1,hold=True)
        output=(self.root/'supervisor.out').open('w'); self.files.append(output)
        child=subprocess.Popen(['bash',str(self.repo/'scripts/queue-loop-supervised.sh'),'dispatch','1'],cwd=self.repo,env=self.env,stdout=output,stderr=subprocess.STDOUT,start_new_session=True)
        self.children.append(child); self.wait_for(lambda:bool(self.started())); self.stop(child)
        self.assertTrue(all(v.get('interrupted') for v in json.loads((self.common/'leases.json').read_text()).values()))

    def test_supervisor_signal_between_fork_and_pid_assignment(self):
        file=self.repo/'scripts/queue-loop-supervised.sh'; text=file.read_text(); needle='  CHILD_PID=$!'
        self.assertEqual(text.count(needle),1)
        file.write_text(text.replace(needle,'  kill -TERM "$$"\n'+needle,1))
        result=self.invoke(['bash',str(file),'dispatch','1'])
        self.assertEqual(result.returncode,130,result.stdout+result.stderr)
        self.assertFalse(self.events('provider-start'))

    def inherited_lane_supervisor(self, cancel):
        self.config['agents']['parallel_lanes']=1; self.save_config()
        self.ticket(1,hold=True); self.ticket(2,hold=True)
        self.env.update(BUREAU_SUPERVISOR_MAX_CRASHES='2',BUREAU_SUPERVISOR_STABILITY_WINDOW='604800')
        supervisor=self.repo/'scripts/queue-loop-supervised.sh'; text=supervisor.read_text()
        self.assertEqual(text.count('BACKOFFS=(10 30 60 300 300)'),1)
        supervisor.write_text(text.replace('BACKOFFS=(10 30 60 300 300)','BACKOFFS=(0 0 0 0 0)',1))
        dispatcher=self.repo/'scripts/bureau-dispatch.py'; text=dispatcher.read_text()
        needle='    sys.exit(main())'; self.assertEqual(text.count(needle),1)
        dispatcher.write_text(text.replace(needle,"    with (Path(os.environ['DISPATCH_FIXTURE'])/'dispatcher-pids').open('a') as out: out.write(str(os.getpid())+'\\n')\n"+needle,1))
        output=(self.root/'supervisor.out').open('w'); self.files.append(output)
        child=subprocess.Popen(['bash',str(supervisor),'dispatch','1'],cwd=self.repo,env=self.env,
                               stdout=output,stderr=subprocess.STDOUT,start_new_session=True)
        self.children.append(child)
        log=self.repo/'logs/supervisor-dispatch.log'
        try:
            self.wait_for(lambda:bool(self.started()))
            pid=int((self.root/'dispatcher-pids').read_text().splitlines()[0])
            os.kill(pid,signal.SIGKILL)
            self.wait_for(lambda:child.poll() is not None or 'waiting for dispatch lock' in log.read_text(),timeout=30)
            self.assertIsNone(child.poll(),'lock contention exhausted the supervisor crash budget: '+log.read_text())
            time.sleep(.3); self.assertIsNone(child.poll())
            self.assertNotIn('giving up',log.read_text())
            self.assertEqual(log.read_text().count('queue-loop crashed'),1)
            if cancel:
                self.stop(child)
                self.assertFalse(self.events('provider-end'),'supervisor signalled the inherited lane')
            else:
                self.release('TST-1')
                self.wait_for(lambda:any(v['issue']=='TST-2' for v in self.started()),timeout=120)
                self.assertIsNone(child.poll()); self.stop(child)
        finally:
            self.release('TST-1'); self.release('TST-2')
            # The inherited lane is deliberately not the supervisor's child.
            # Let it finish and join its own cleanup even if an assertion failed.
            self.wait_for(lambda:'issue:TST-1' not in json.loads((self.common/'leases.json').read_text()),timeout=60)

    def test_supervisor_waits_for_inherited_lane_without_spending_crash_budget(self):
        self.inherited_lane_supervisor(cancel=False)

    def test_supervisor_lock_wait_is_cancellable_and_spares_inherited_lane(self):
        self.inherited_lane_supervisor(cancel=True)

    def test_startup_marker_io_failure_retains_running_child_ownership(self):
        issue,_=self.ticket(1)
        blocked=self.root/'blocked-parent'; blocked.write_text('not a directory')
        code="from pathlib import Path; import os,time; Path(os.environ['DISPATCH_FIXTURE'],'writer.pid').write_text(str(os.getpid())); time.sleep(120)"
        child=subprocess.Popen([sys.executable,'-I',str(self.repo/'scripts/bureau-runtime.py'),'--repo',str(self.repo),
                                'exec','--issue',issue,'--workspace',str(self.root/'writer'),
                                '--entry',str(self.repo/'scripts/implement-pipeline.sh'),'--',sys.executable,'-c',code],
                               env={**self.env,'BUREAU_DISPATCH_READY_FILE':str(blocked/'ready')},cwd=self.repo,
                               stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,start_new_session=True)
        self.children.append(child)
        try:
            self.wait_for(lambda:(self.root/'writer.pid').exists())
            time.sleep(.2); self.assertIsNone(child.poll())
            self.assertIn('issue:'+issue,json.loads((self.common/'leases.json').read_text()))
            self.stop(child)
        finally:
            if child.poll() is None: child.terminate(); child.wait(timeout=25)
            if (self.root/'writer.pid').exists():
                # A failing mutation control must still stop its own fresh
                # writer; this PID comes from this fixture's just-started child.
                try: os.killpg(int((self.root/'writer.pid').read_text()),signal.SIGTERM)
                except ProcessLookupError: pass

    def test_shutdown_joins_writers_retains_leases_and_spares_unrelated_sentinel(self):
        self.ticket(1,hold=True,spawn_writer=True)
        sentinel=subprocess.Popen([sys.executable,'-c','import time; time.sleep(120)']); self.children.append(sentinel)
        child=self.start(); self.wait_for(lambda:(self.root/'writer.pid').exists())
        workspace=Path(next(key[len('workspace:'):] for key in json.loads((self.common/'leases.json').read_text()) if key.startswith('workspace:')))
        self.common.mkdir(exist_ok=True)
        # A prior incarnation's record is not a signal target.
        (self.common/'processes').mkdir(exist_ok=True)
        (self.common/'processes'/('f'*32+'.json')).write_text(json.dumps(dict(groups=[sentinel.pid],interrupted=True)))
        self.stop(child); self.assertIsNone(sentinel.poll())
        leases=json.loads((self.common/'leases.json').read_text()); self.assertTrue(leases)
        self.assertTrue(all(v.get('interrupted') for v in leases.values()))
        self.assertEqual((workspace/'unfinished-TST-1.txt').read_text(),'interrupted work must survive\n')
        ticks=(self.root/'ticks').read_text(); time.sleep(.2); self.assertEqual((self.root/'ticks').read_text(),ticks)
        sentinel.terminate(); sentinel.wait(timeout=3)

    def test_shutdown_during_launch_window_has_no_orphan(self):
        self.ticket(1,hold=True)
        private=self.root/'launch-window'; shutil.copytree(self.repo/'scripts',private)
        file=private/'bureau-dispatch.py'; code=file.read_text()
        needle='        self.lanes[issue] = child, workspace, output'
        self.assertIn(needle,code); file.write_text(code.replace(needle,'        os.kill(os.getpid(), signal.SIGTERM)\n'+needle,1))
        child=self.start(scripts=private); self.assertEqual(child.wait(timeout=40),130,self.output())
        self.assertFalse(self.started()); time.sleep(.2)
        leases=self.common/'leases.json'
        if leases.exists(): self.assertTrue(all(v.get('interrupted') for v in json.loads(leases.read_text()).values()))

    def test_shutdown_during_constructor_survives_handler_handoff(self):
        self.ticket(1,hold=True)
        wrapper=self.bin/'git'; text=wrapper.read_text()
        needle="if 'push' in sys.argv[1:]:"; self.assertEqual(text.count(needle),1)
        barrier="""if '--git-common-dir' in sys.argv[1:]:
    root=Path(os.environ['DISPATCH_FIXTURE']); counter=root/'common-lookups'
    count=int(counter.read_text())+1 if counter.exists() else 1; counter.write_text(str(count))
    if count==2:
        (root/'constructing').touch()
        while not (root/'release-constructor').exists(): time.sleep(.02)
"""
        wrapper.write_text(text.replace(needle,barrier+needle,1))
        child=self.start()
        try:
            self.wait_for(lambda:(self.root/'constructing').exists(),timeout=15)
            child.terminate()
        finally:
            (self.root/'release-constructor').touch()
        self.wait_for(lambda:child.poll() is not None or bool(self.started()),timeout=90)
        self.assertEqual(child.poll(),130,'constructor cancellation was lost at run handler handoff: '+self.output())
        self.assertFalse(self.started()); self.assertFalse(self.events('move'))

    def test_shutdown_during_prelaunch_read_is_responsive(self):
        self.ticket(1); self.change(lambda d:d.update(slow_snapshot=True))
        child=self.start(); self.wait_for(lambda:bool(self.events('slow-snapshot')))
        self.stop(child); self.assertFalse(self.started())

    def test_withdrawal_after_paid_work_wins_over_publication(self):
        for number,state,code in [(1,'backlog',25),(2,'done',0),(3,'cancelled',26)]:
            self.ticket(number,withdraw_after_provider=state)
        child=self.start(); self.wait_for(lambda:all(self.skipped('TST-'+str(n),c) for n,c in [(1,25),(2,0),(3,26)]))
        self.assertFalse(self.events('merge'))
        self.assertFalse(any(e['state'] in ('build_review','merge') for e in self.events('move')))
        self.assertFalse(any(e['kind']=='pr-comment' for e in self.events()))
        self.stop(child)

    def test_interrupted_paid_review_records_round_without_verdict(self):
        self.config['agents']['code_review']=True; self.save_config()
        issue,branch=self.ticket(1,state='build_review',hold=True)
        self.change(lambda d:d['prs'].append(dict(number=1,branch=branch,issue=issue,comments=[])))
        child=self.start(); self.wait_for(lambda:len([v for v in self.events('provider-start') if v['phase'] in ('correctness','security','performance')])==3)
        self.stop(child)
        manifests=[json.loads(p.read_text()) for p in (self.common/'evidence').rglob('manifest.json')]
        rounds=[a['review_round'] for m in manifests for a in m['attempts'] if 'review_round' in a]
        self.assertTrue(rounds,manifests); self.assertTrue(all(v.get('final_verdict') is None for v in rounds),rounds)
        self.assertTrue(all(v.get('provider_verdict') is None for v in rounds),rounds)
        self.assertTrue(all(all(value is None for value in v['usage'].values()) for v in rounds),rounds)
        self.assertFalse(self.events('pr-comment'))

    def stale_pr(self, merge_agent=True):
        self.config['agents'].update(code_review=True,merge=merge_agent); self.save_config()
        issue,branch=self.ticket(1,state='merge' if merge_agent else 'build_review')
        review='Code Review v2\n**Verdict**: APPROVE'
        self.change(lambda d:d['prs'].append(dict(number=1,branch=branch,issue=issue,comments=[dict(body=review,createdAt='2026-10-26T00:00:00Z')])))
        self.change(lambda d:d['issues'][0]['comments']['nodes'].append(dict(body='✅ Code review **APPROVED**.\n<!-- bureau-review-round: round=1 verdict=APPROVE -->',createdAt='2026-10-26T00:00:00Z')))
        (self.repo/'pkg/b.py').write_text('sibling merge\n'); self.commit('sibling lands'); self.g('push','-q','origin','main')
        return issue,branch

    def test_stale_base_merge_forward_requires_fresh_review(self):
        issue,branch=self.stale_pr()
        before=self.g('rev-parse','origin/'+branch); base=self.g('rev-parse','origin/main')
        child=self.start(); self.wait_for(lambda:self.skipped(issue,0),timeout=300)
        after=self.g('rev-parse','origin/'+branch)
        self.assertNotEqual(before,after); self.g('merge-base','--is-ancestor',base,after)
        moves=self.events('move'); self.assertEqual([v['state'] for v in moves],['build_review','merge','done'])
        phases=[v['phase'] for v in self.events('provider-start')]
        self.assertEqual(sorted(phases),['correctness','performance','security','verdict'])
        first_review=min(v['at'] for v in self.events('provider-start'))
        self.assertLess(moves[0]['at'],first_review); self.assertGreater(self.events('merge')[0]['at'],first_review)
        self.assertTrue(any('round=2 ' in v['body'] for v in self.events('comment')))
        pushes=self.events('git-push'); self.assertTrue(pushes)
        self.assertTrue(all(not any(a.startswith('--force') or a=='-f' for a in v['args']) for v in pushes))
        self.stop(child)

    def test_inline_merge_refresh_is_handoff_and_not_done(self):
        issue,branch=self.stale_pr(merge_agent=False)
        # Invoke the real inline merge worker: a refresh returns 20 and carries
        # its report through the review/shepherd interface, never success 0.
        report=self.root/'gate.report'
        result=self.invoke(['bash',str(self.repo/'scripts/bureau-worker.sh'),issue,'merge-pipeline.sh',str(self.root/'inline'),branch],
                           env={**self.env,'BUREAU_DISPATCH_LANE':'1','BUREAU_HELD_BY_SHEPHERD':'1','BUREAU_INLINE_MERGE':'1','BUREAU_MERGE_GATE_REPORT':str(report)},timeout=70)
        self.assertEqual(result.returncode,20,result.stdout+result.stderr)
        self.assertEqual(report.read_text().splitlines()[0],'refreshed')
        self.assertFalse(self.events('merge')); self.assertEqual(self.events('move')[-1]['state'],'build_review')

    def test_dirty_merge_lane_refreshes_through_existing_transaction(self):
        issue,branch=self.stale_pr()
        self.change(lambda d:d['prs'][0].update(merge_state='DIRTY'))
        report=self.root/'dirty.report'
        result=self.invoke(['bash',str(self.repo/'scripts/bureau-worker.sh'),issue,'merge-pipeline.sh',str(self.root/'dirty'),branch],
                           env={**self.env,'BUREAU_DISPATCH_LANE':'1','BUREAU_HELD_BY_SHEPHERD':'1','BUREAU_MERGE_GATE_REPORT':str(report)},timeout=100)
        self.assertEqual(result.returncode,20,result.stdout+result.stderr)
        self.assertEqual(report.read_text().splitlines()[0],'refreshed')
        self.assertFalse(self.events('merge')); self.assertEqual(self.events('move')[-1]['state'],'build_review')

    def test_base_refresh_budget_fails_closed_without_push(self):
        issue,branch=self.stale_pr()
        result=self.invoke(['bash',str(self.repo/'scripts/bureau-worker.sh'),issue,'merge-pipeline.sh',str(self.root/'budget'),branch],
                           env={**self.env,'BUREAU_DISPATCH_LANE':'1','BUREAU_HELD_BY_SHEPHERD':'1','BUREAU_LANE_BASE_REFRESHES':'2'},timeout=100)
        self.assertEqual(result.returncode,25,result.stdout+result.stderr)
        self.assertFalse(self.events('git-push')); self.assertFalse(self.events('move')); self.assertFalse(self.events('merge'))

    def test_persisted_merge_wait_yields_and_refills_with_disjoint_work(self):
        issue,branch=self.ticket(1,state='merge'); self.ticket(2,hold=True)
        self.change(lambda d:d.update(pending_ci=True))
        self.change(lambda d:d['prs'].append(dict(number=1,branch=branch,issue=issue,comments=[dict(body='Code Review v2\n**Verdict**: APPROVE',createdAt='2026-10-26T00:00:00Z')])))
        child=self.start(); self.wait_for(lambda:self.skipped(issue,2) and any(v['issue']=='TST-2' for v in self.started()))
        waits=json.loads((self.common/'merge-gate-waits.json').read_text()); self.assertIn(issue,waits)
        self.assertFalse(any(v['name']=='needs-human' for v in self.events('label')))
        self.change(lambda d:d.update(pending_ci=False)); self.wait_for(lambda:self.skipped(issue,0))
        self.assertFalse(any(v['phase'] in ('correctness','security','performance','verdict') for v in self.events('provider-start')))
        manifests=[json.loads(p.read_text()) for p in (self.common/'evidence').rglob('manifest.json')]
        self.assertFalse(any('review_round' in attempt for m in manifests for attempt in m['attempts']))
        self.stop(child)

    def test_withdrawal_before_base_refresh_prevents_push_and_review(self):
        issue,_=self.stale_pr(); self.change(lambda d:d.update(withdraw_on_base_read=True))
        child=self.start(); self.wait_for(lambda:self.skipped(issue,25))
        self.assertFalse(self.events('git-push')); self.assertFalse(self.events('provider-start'))
        self.assertFalse(self.events('move')); self.assertFalse(self.events('merge')); self.stop(child)


if __name__=='__main__': unittest.main(verbosity=2)
