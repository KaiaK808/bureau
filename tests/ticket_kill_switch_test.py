"""Cooperative withdrawal at real Bureau boundaries. No network/provider calls."""
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import tempfile
import unittest
import zipfile

import git_maintenance
from mutation_checks import baseline_fixture, caught_mutation

ROOT = Path(__file__).resolve().parents[1]
REAL_GIT = shutil.which('git')
STATES = {key: 'custom-id-' + key for key in
          ('triage', 'spec', 'spec_review', 'design', 'copy', 'build', 'qa', 'build_review', 'merge',
           'done', 'cancelled', 'duplicate', 'backlog')}

CURL = r'''#!/usr/bin/env python3
import json, os, pathlib, re, sys
root=pathlib.Path(os.environ['PR9_FIXTURE']); args=sys.argv[1:]
config=sys.stdin.read() if '-K' in args else ''
payload=json.loads(args[args.index('-d')+1]); q=payload['query']; v=payload.get('variables',{})
def event(kind, **kw):
    with (root/'events').open('a') as f: f.write(json.dumps(dict(kind=kind, **kw))+'\n')
state=(root/'state').read_text(); labels=json.loads((root/'labels').read_text())
issue=dict(id='U9',identifier='EXP-9',title='withdrawal fixture',description='test',project=None,
           state=dict(id=state,name='Done'),labels=dict(nodes=[dict(name=n) for n in labels]),
           priority=1,createdAt='2026-10-26T00:00:00Z',inverseRelations=dict(nodes=[]))
if 'commentCreate' in q:
    event('comment',body=v['body']); result=dict(commentCreate=dict(success=True))
elif 'issueUpdate' in q:
    event('move',state=v['sid']); (root/'state').write_text(v['sid']); result=dict(issueUpdate=dict(success=True))
elif 'issueAddLabel' in q or 'issueRemoveLabel' in q:
    name=v['lid'][2:]; add='issueAddLabel' in q; event('label',name=name,add=add)
    labels=list(set(labels+[name])) if add else [n for n in labels if n!=name]
    (root/'labels').write_text(json.dumps(labels)); result={('issueAddLabel' if add else 'issueRemoveLabel'):dict(success=True)}
elif 'issueLabels' in q:
    name=re.search(r'name: \{ eq: "([^"]+)"',q).group(1)
    result=dict(issueLabels=dict(nodes=[dict(id='L:'+name,team=None)]))
elif 'viewer' in q: result=dict(viewer=dict(id='viewer'))
elif 'comments' in q or 'branchName' in q:
    comments=[dict(body='<!-- bureau-branch: 009-ticket-switch -->',createdAt='2026-10-26T00:00:00Z')]
    result=dict(issues=dict(nodes=[dict(branchName='wrong-branch',comments=dict(nodes=comments))]))
elif 'nodes { id identifier title description state' in q:
    control=json.loads((root/'control').read_text())
    count=1+sum(json.loads(line)['kind']=='snapshot' for line in (root/'events').read_text().splitlines())
    if count==control.get('withdraw_on_snapshot'):
        state=control['target']; (root/'state').write_text(state); issue['state']['id']=state
    event('snapshot',state=state)
    if (root/'fail-reads').exists() or count==control.get('fail_on_snapshot') or count in control.get('fail_on_snapshots',[]): print('{}'); sys.exit(0)
    if count==control.get('fail_after_snapshot'): (root/'fail-reads').touch()
    result=dict(issues=dict(nodes=[] if (root/'no-state').exists() else [issue]))
elif 'identifier title description project' in q:
    result=dict(issues=dict(nodes=[issue]))
elif 'nodes { id }' in q: result=dict(issues=dict(nodes=[dict(id='U9')]))
else: # Shared picker, filters use configured IDs and labels in production.
    result=dict(issues=dict(nodes=[issue]))
print(json.dumps(dict(data=result)),end='')
if '-w' in args: print(args[args.index('-w')+1].replace('%{http_code}','200').replace(r'\n','\n'),end='')
'''

PROVIDER = r'''#!/usr/bin/env python3
import json, os, pathlib, signal, subprocess, sys, time
root=pathlib.Path(os.environ['PR9_FIXTURE']); args=sys.argv[1:]
if args[:1]==['auth']: print('{"loggedIn":true}'); sys.exit(0)
if args[:1]==['--help']: print('--json-schema --disallowedTools'); sys.exit(0)
if args[:1]==['--version']: print('2.0.0'); sys.exit(0)
phase=os.environ.get('BUREAU_PROVIDER_PHASE','implement')
if '--append-system-prompt-file' in args:
    system=pathlib.Path(args[args.index('--append-system-prompt-file')+1]).read_text()
    if '(research)' in system: phase='research'
with (root/'pids').open('a') as f: f.write(str(os.getpid())+'\n')
if os.environ.get('PR9_PROCESS_AUDIT'):
    with open(os.environ['PR9_PROCESS_AUDIT'],'a') as f: f.write(str(os.getpid())+'\n')
with (root/'events').open('a') as f: f.write(json.dumps(dict(kind='pass',phase=phase))+'\n')
control=json.loads((root/'control').read_text()); target=control.get('target')
if phase in ('correctness','security','performance'):
    if phase!='correctness':
        # A sibling can now be cancelled before the old artificial delay ends.
        # Capture the real lease while the writer is still alive, including
        # on cooperative shutdown, rather than requiring it to finish its work.
        def record_lease():
            common=root/'repo with spaces'/'.git'
            (root/('leases-at-'+phase)).write_text((common/'bureau/leases.json').read_text())
        def stop_specialist(signum,frame):
            record_lease(); sys.exit(128+signum)
        signal.signal(signal.SIGTERM,stop_specialist)
    (root/('started-'+phase)).touch()
    if target:
        deadline=time.monotonic()+10
        while len(list(root.glob('started-*')))!=3 and time.monotonic()<deadline: time.sleep(.01)
    if phase!='correctness':
        time.sleep(.2)
        record_lease()
    result=dict(specialist=phase,counts=dict(critical=0,bug=0,minor=0,skip=0),findings=[],summary='clean')
elif phase=='verdict':
    result=dict(verdict='APPROVE',bugs=0,security_issues=0,missing_acceptance=[],fixes_needed=[],
                findings=[],summary='clean',comment='clean')
elif phase in ('specify','plan','tasks'):
    directory=pathlib.Path(os.environ['SPECIFY_FEATURE_DIRECTORY']); directory.mkdir(exist_ok=True)
    names=dict(specify='spec.md',plan='plan.md',tasks='tasks.md')
    (directory/names[phase]).write_text('# Draft\n## Governance and security\nDecision: not required\nRationale: fixture only\n' if phase=='specify' else '# Draft\n')
    result='spec artifacts generated'
else:
    pathlib.Path('unfinished.txt').write_text('unfinished provider work\n')
    if control.get('complete'):
        for p in pathlib.Path('specs').glob('*/tasks.md'): p.write_text(p.read_text().replace('[ ]','[X]'))
    result=dict(status='COMPLETE' if control.get('complete') else 'PARTIAL',tasks_done=1,tasks_skipped=0,
                tasks_needs_human=0,fixed_review_items=[],notes=dict(needs_human=[],skipped=[],deviations=[]),prose_notes='work kept')
if control.get('commit'):
    pathlib.Path('finished.txt').write_text(control.get('commit_text','finished portion\n'))
    subprocess.run([os.environ['PR9_REAL_GIT'],'add','finished.txt'],check=True)
    subprocess.run([os.environ['PR9_REAL_GIT'],'commit','-qm','EXP-9: partial work','-m','Bureau-Generated: true'],check=True)
if target: (root/'state').write_text(target)
if control.get('signal_phase') == phase:
    os.kill(os.getppid(), signal.SIGTERM)
    time.sleep(10)  # The real provider adapter stops and reaps this process.
(root/'provider-done').touch()
if control.get('fail_after_pass'): (root/'fail-reads').touch()
print(json.dumps(dict(result=json.dumps(result) if isinstance(result,dict) else result, structured_output=result) if isinstance(result,dict) else dict(result=result)))
'''

GH = r'''#!/usr/bin/env python3
import json, os, pathlib, signal, subprocess, sys, time
root=pathlib.Path(os.environ['PR9_FIXTURE']); a=sys.argv[1:]
if a[:2] in (['pr','create'],['pr','comment'],['pr','merge'],['pr','ready']):
    with (root/'events').open('a') as f: f.write(json.dumps(dict(kind='gh-publication',args=a))+'\n')
if a[:2]==['pr','list']:
    if 'number,url' in a: print('{"number":9,"url":"https://github.com/test/repo/pull/9"}')
    elif 'number' in a: print('9')
    else: print('[]')
elif a[:2]==['pr','create']: print('https://github.com/test/repo/pull/9')
elif a[:2]==['pr','view']:
    field=a[a.index('--json')+1] if '--json' in a else ''
    head=subprocess.check_output([os.environ['PR9_REAL_GIT'],'rev-parse','origin/009-ticket-switch'],text=True).strip()
    base=subprocess.check_output([os.environ['PR9_REAL_GIT'],'rev-parse','origin/main'],text=True).strip()
    data=dict(url='https://github.com/test/repo/pull/9',number=9,state='OPEN',headRefName='009-ticket-switch',
              baseRefName='main',headRefOid=head,baseRefOid=base,isDraft=False,mergeStateStatus='CLEAN',labels=[],comments=[],title='EXP-9 feature',body='feature')
    if field=='title,body' and json.loads((root/'control').read_text()).get('withdraw_at_merge'):
        (root/'state').write_text('custom-id-backlog')
        with (root/'events').open('a') as f: f.write('{"kind":"withdrawal"}\n')
    if field=='comments' and json.loads((root/'control').read_text()).get('withdraw_at_acceptance'):
        (root/'state').write_text('custom-id-backlog')
    if field=='comments' and json.loads((root/'control').read_text()).get('signal_at_acceptance'):
        os.kill(os.getppid(),signal.SIGTERM)
        time.sleep(10)  # bounded() must stop and reap this lookup on interruption.
    if field=='comments' and '--jq' in a and 'Code Review v2' in a[a.index('--jq')+1]:
        print('Code Review v2\n**Verdict**: APPROVE'); sys.exit(0)
    print(data[field] if '--jq' in a and field in data and not isinstance(data[field],(dict,list)) else json.dumps({k:data[k] for k in field.split(',') if k in data}))
elif a[:2]==['repo','view']: print('test/repo')
elif a[:1]==['api']:
    if 'graphql' in a: print('{"data":{"repository":{"pullRequest":{"reviewThreads":{"nodes":[]}}}}}')
    elif any('/check-runs' in x for x in a): print('{"check_runs":[{"status":"completed","conclusion":"success","name":"fixture-ci"}]}')
    elif any('/status' in x for x in a): print('[]')
    elif any('/branches/main' in x for x in a): print(subprocess.check_output([os.environ['PR9_REAL_GIT'],'rev-parse','origin/main'],text=True).strip())
    else: print('{}')
else: print('{}')
'''

GIT = r'''#!/usr/bin/env python3
import json, os, pathlib, sys
root=pathlib.Path(os.environ['PR9_FIXTURE']); a=sys.argv[1:]
command=[]; i=0
while i<len(a):
    if a[i] in ('-C','-c','--git-dir','--work-tree','--config-env'): i+=2
    elif a[i].startswith('-'): i+=1
    else: command=a[i:]; break
if command[:1]==['push']:
    with (root/'events').open('a') as f: f.write('{"kind":"push"}\n')
if command[:1]==['rev-list'] and (root/'late-read-failure').exists() and (root/'provider-done').exists():
    (root/'fail-reads').touch()
os.execv(os.environ['PR9_REAL_GIT'],[os.environ['PR9_REAL_GIT'],*a])
'''


class WithdrawalFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='bureau-pr9-fixture-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.repo = self.root/'repo with spaces'; self.repo.mkdir()
        self.bin = self.root/'bin'; self.bin.mkdir()
        self.env = {k:v for k,v in os.environ.items() if not k.startswith('BUREAU_')}
        self.env.update(PATH=str(self.bin)+':'+os.environ['PATH'], PR9_FIXTURE=str(self.root),
                        PR9_REAL_GIT=REAL_GIT, BUREAU_IMPL_MAX_ITER='3', BUREAU_LINEAR_RETRIES='0',
                        BUREAU_ALERT_THROTTLE_FILE=str(self.root/'alerts'), TMUX='', PYTHONDONTWRITEBYTECODE='1')
        for name, body in [('curl',CURL),('claude',PROVIDER),('gh',GH),('git',GIT)]:
            p=self.bin/name; p.write_text(body); p.chmod(0o755)
        for name, content in [('events',''),('pids',''),('labels','["lane-2"]'),('control','{}'),('state',STATES['build'])]:
            (self.root/name).write_text(content)
        self.git('init','-q','-b','main'); self.git(*git_maintenance.OFF_CONFIG)
        self.git('config','user.name','fixture'); self.git('config','user.email','test@bureau')
        (self.repo/'.gitignore').write_text('.env\n.bureau.json\n/scripts/\n.worktrees/\nlogs/\n')
        (self.repo/'source.txt').write_text('base\n')
        self.git('add','.gitignore','source.txt'); self.git('commit','-qm','foundation')
        origin=self.root/'origin.git'; subprocess.run([REAL_GIT,'init','-q','--bare',str(origin)],check=True)
        subprocess.run([REAL_GIT,'--git-dir',str(origin),*git_maintenance.OFF_CONFIG],check=True)
        self.git('remote','add','origin',str(origin)); self.git('push','-q','origin','main')
        self.branch='009-ticket-switch'; self.git('checkout','-qb',self.branch)
        spec=self.repo/'specs'/self.branch; spec.mkdir(parents=True)
        (spec/'tasks.md').write_text('# Tasks\n- [ ] T001 First\n- [ ] T002 Second\n')
        (spec/'spec.md').write_text('# Spec\n## Governance and security\nDecision: not required\nRationale: fixture only\n')
        with zipfile.ZipFile(ROOT/'tests/fixtures/speckit-1.2.2.zip') as z:
            for name in ['scripts/bash/common.sh','scripts/bash/create-new-feature.sh']: z.extract(name,self.repo/'.specify')
        self.git('add','specs','.specify'); self.git('commit','-qm','spec foundation')
        self.git('push','-q','origin',self.branch); self.git('checkout','-q','main')
        # Spec reset starts on main, so it needs the pinned CLI output there too.
        self.git('merge','--ff-only',self.branch); self.git('push','-q','origin','main')
        self.git('checkout','-q',self.branch)
        (self.repo/'source.txt').write_text('feature change\n')
        self.git('commit','-qam','feature implementation'); self.git('push','-q','origin',self.branch)
        self.git('checkout','-q','main')
        shutil.copytree(ROOT/'templates/scripts',self.repo/'scripts')
        (self.repo/'.env').write_text('LINEAR_API_KEY=fixture-key\n')
        self.config=dict(version=2,linear=dict(teams=[dict(key='EXP',id='T',name='fixture',states=STATES)],
                         labels=dict(lane2=dict(id='L:lane-2',name='lane-2'))),
                         agents=dict(runner='claude',implement=True,code_review=True,merge=True),repo=dict(base_branch='main',specs_dir='specs',test_command='true'))
        self.save_config()
        self.common=self.repo/'.git'; self.wt=self.repo/'.worktrees/worker'
        self.addCleanup(self.assert_no_providers)

    def git(self,*args):
        return subprocess.check_output([REAL_GIT,'-C',str(self.repo),*args],text=True,stderr=subprocess.DEVNULL).strip()

    def save_config(self): (self.repo/'.bureau.json').write_text(json.dumps(self.config))
    def control(self,**kw): (self.root/'control').write_text(json.dumps(kw))
    def state(self,key): (self.root/'state').write_text(STATES.get(key,key))
    def events(self): return [json.loads(l) for l in (self.root/'events').read_text().splitlines()]
    def run_cmd(self,*cmd,timeout=45):
        result=subprocess.run(cmd,cwd=self.repo,env=self.env,capture_output=True,text=True,timeout=timeout)
        self.result=result
        return result
    def worker(self,stage='implement'):
        return self.run_cmd('bash','scripts/bureau-worker.sh','EXP-9',stage+'-pipeline.sh',str(self.wt),self.branch)
    def assert_released(self): self.assertEqual({},json.loads((self.common/'bureau/leases.json').read_text()))
    def assert_no_publication(self,events):
        self.assertFalse([e for e in events if e['kind'] in ('push','gh-publication','comment')],events)
    def assert_only_review_reservation(self):
        events=self.events()
        first_pass=next(i for i,e in enumerate(events) if e['kind']=='pass')
        # PR 8 reserves the round before specialists can withdraw the ticket.
        self.assertEqual([dict(kind='comment',body='Code review round 1 started.\n<!-- bureau-review-round: round=1 -->')],
                         [e for e in events[:first_pass] if e['kind'] in ('push','gh-publication','comment')],events)
        self.assert_no_publication(events[first_pass:])
    def assert_no_providers(self):
        live=[]
        for pid in set((self.root/'pids').read_text().splitlines()):
            try: os.kill(int(pid),0)
            except ProcessLookupError: continue
            else:
                live.append(pid)
                os.kill(int(pid),signal.SIGKILL)
        self.assertEqual([],live,'provider still running (stopped by test cleanup)')
    def assert_withdrawn(self,result,code,target,stage='implement'):
        self.assertEqual(code,result.returncode,result.stdout+'\n'+result.stderr)
        events=self.events(); passes=[e for e in events if e['kind']=='pass']
        self.assertEqual(1,len(passes),events)
        self.assert_no_publication(events)
        moves=[e['state'] for e in events if e['kind']=='move']
        self.assertEqual([STATES['spec']] if stage=='spec' else [],moves)
        self.assertEqual(target,(self.root/'state').read_text())
        self.assert_released()
        key=__import__('hashlib').sha256(str(self.wt.resolve()).encode()).hexdigest()
        self.assertFalse((self.common/'bureau/workers'/key).exists())
        self.assertTrue((self.common/'bureau/preserved'/(key+'.json')).exists())
        self.assertTrue(self.git('-C',str(self.wt),'status','--porcelain'))


class Withdrawal(WithdrawalFixture):
    def test_provider_withdrawal_matrix(self):
        # Same fixture reset between cases; each new run owns a fresh worker.
        for index,(key,code) in enumerate([('backlog',25),('cancelled',26),('duplicate',26),('done',0),('unknown-id',25)]):
            with self.subTest(key=key):
                self.wt=self.repo/'.worktrees'/str(index); self.state('build')
                (self.root/'events').write_text(''); self.control(target=STATES.get(key,key))
                result=self.worker(); self.assert_withdrawn(result,code,STATES.get(key,key))
                self.assertEqual('unfinished provider work\n',(self.wt/'unfinished.txt').read_text())
                self.git('-C',str(self.wt),'checkout','--detach','-q')

    def test_spec_withdrawal_never_rolls_back_or_discards(self):
        for index,(key,code) in enumerate([('backlog',25),('cancelled',26),('done',0),('unknown-id',25)]):
            with self.subTest(state=key):
                self.wt=self.repo/'.worktrees'/f'spec-{index}'; self.state('triage')
                (self.root/'events').write_text(''); self.control(target=STATES.get(key,key))
                self.assert_withdrawn(self.worker('spec'),code,STATES.get(key,key),'spec')
                feature=json.loads((self.wt/'.specify/feature.json').read_text())
                self.assertIn('# Draft',(Path(feature['feature_directory'])/'spec.md').read_text())
                self.git('-C',str(self.wt),'checkout','--detach','-q')

    def spec_best_effort_signal(self,phase):
        self.state('triage'); self.control(signal_phase=phase)
        if phase=='research':
            self.config['agents']['research']=True; self.save_config()
            (self.root/'labels').write_text('["lane-2","needs-research"]')
        result=self.worker('spec'); self.assertEqual(130,result.returncode,result.stdout+result.stderr)
        self.assertEqual(phase,[e for e in self.events() if e['kind']=='pass'][-1]['phase'])
        self.assertEqual([STATES['spec']],[e['state'] for e in self.events() if e['kind']=='move'])
        self.assertEqual(STATES['spec'],(self.root/'state').read_text())
        self.assertFalse([e for e in self.events() if e['kind']=='comment'])
        leases=json.loads((self.common/'bureau/leases.json').read_text())
        self.assertTrue(leases); self.assertTrue(all(lease['interrupted'] for lease in leases.values()))

    def test_spec_best_effort_research_signal_remains_130(self):
        self.spec_best_effort_signal('research')

    def test_spec_best_effort_digest_signal_remains_130(self):
        self.spec_best_effort_signal('digest')

    def test_active_transition_remains_normal(self):
        self.control(complete=True)
        result=self.worker(); self.assertEqual(0,result.returncode,result.stderr)
        self.assertEqual(STATES['qa'],(self.root/'state').read_text()); self.assert_released()
        self.assertTrue([e for e in self.events() if e['kind']=='push'])

    def test_own_done_transition_allows_existing_bookkeeping(self):
        self.state('design')
        self.mini_stage('''move_issue "$ISSUE" "$BUREAU_STATE_DONE"
post_comment "$ISSUE" 'normal completion'
bureau_ticket_checkpoint "$ISSUE"''')
        result=self.worker('ux'); self.assertEqual(0,result.returncode,result.stdout+result.stderr)
        self.assertEqual(STATES['done'],(self.root/'state').read_text())
        self.assertEqual(1,len([e for e in self.events() if e['kind']=='comment']))
        self.assert_released()

    def mini_stage(self,body):
        (self.repo/'scripts/ux-pipeline.sh').write_text('''#!/bin/bash
set -euo pipefail
source "$(dirname "$0")/bureau-config.sh"
bureau_load_env --export "$BUREAU_ENV_FILE"
bureau_secret_copy API_KEY LINEAR_API_KEY
ISSUE="$1"
bureau_stage_enter "$ISSUE" "$@"
'''+body+'\n')

    def test_additional_refresh_failure_warns_exactly_once_across_captures(self):
        self.state('design'); self.control(fail_after_pass=True)
        self.mini_stage('''RESULT=$(run_stage_for ux 'fixture pass')
bureau_ticket_checkpoint "$ISSUE" latched
bureau_ticket_checkpoint "$ISSUE"
bureau_ticket_checkpoint "$ISSUE"
echo publication >> "$PR9_FIXTURE/published"''')
        result=self.worker('ux'); self.assertEqual(0,result.returncode,result.stderr)
        self.assertEqual(1,result.stderr.count('bureau ticket checkpoint: WARN:'),result.stderr)
        self.assertTrue((self.root/'published').exists()); self.assert_released()

    def test_additional_failure_leaves_callers_linear_retry_and_fault_intact(self):
        self.control(fail_on_snapshots=[1,2])
        result=self.run_cmd('bash','-euo','pipefail','-c','''source scripts/bureau-config.sh
bureau_load_env --export "$BUREAU_ENV_FILE"
bureau_secret_copy API_KEY LINEAR_API_KEY
BUREAU_TICKET_CONTROL_DIR="$PR9_FIXTURE/checkpoint-control"
_BUREAU_LINEAR_FAULT_FILE="$PR9_FIXTURE/caller-fault"
_BUREAU_LINEAR_SINGLE_ATTEMPT=0
BUREAU_LINEAR_RETRIES=1
BUREAU_LINEAR_RETRY_WAIT_1=0
printf 'caller-fault' > "$_BUREAU_LINEAR_FAULT_FILE"
bureau_ticket_checkpoint EXP-9
[ "$_BUREAU_LINEAR_SINGLE_ATTEMPT" = 0 ]
[ "$(cat "$_BUREAU_LINEAR_FAULT_FILE")" = caller-fault ]
bureau_issue_snapshot EXP-9
[ "$(cat "$_BUREAU_LINEAR_FAULT_FILE")" = caller-fault ]''')
        self.assertEqual(0,result.returncode,result.stdout+result.stderr)
        self.assertEqual(3,len([e for e in self.events() if e['kind']=='snapshot']))
        self.assertEqual(1,result.stderr.count('bureau ticket checkpoint: WARN:'))
        self.assertIn('attempt 1 of 2',result.stderr)
        self.assertEqual('EXP-9',json.loads(result.stdout)['identifier'])

    def test_withdrawal_preserves_commits_on_origin_without_new_publication(self):
        for index,(key,code) in enumerate([('backlog',25),('cancelled',26),('done',0)]):
            with self.subTest(state=key):
                self.wt=self.repo/'.worktrees'/f'preserve-{index}'; self.state('build')
                (self.root/'events').write_text(''); self.control(target=STATES[key],commit=True,commit_text=f'finished portion {key}\n')
                result=self.worker(); self.assertEqual(code,result.returncode,result.stdout+result.stderr)
                events=self.events()
                self.assertEqual(1,len([e for e in events if e['kind']=='pass']))
                self.assertEqual(1,len([e for e in events if e['kind']=='push']))
                self.assertFalse([e for e in events if e['kind'] in ('comment','gh-publication','move','label')],events)
                self.assertIn('before the stage ends',result.stderr)
                self.assertIn('  pushed '+self.branch,result.stdout)
                self.assertEqual(self.git('-C',str(self.wt),'rev-parse','HEAD'),self.git('rev-parse','origin/'+self.branch))
                self.assertEqual('unfinished provider work\n',(self.wt/'unfinished.txt').read_text())
                self.assertEqual(STATES[key],(self.root/'state').read_text()); self.assert_released()
                self.git('-C',str(self.wt),'checkout','--detach','-q')

    def test_signal_preservation_push_ignores_later_withdrawal(self):
        self.control(target=STATES['backlog'],commit=True,signal_phase='implement')
        result=self.worker(); self.assertEqual(130,result.returncode,result.stdout+result.stderr)
        events=self.events()
        self.assertEqual(1,len([e for e in events if e['kind']=='push']))
        self.assertFalse([e for e in events if e['kind'] in ('comment','gh-publication','move','label')],events)
        self.assertIn('(provider exit 130)',result.stderr)
        self.assertEqual(self.git('-C',str(self.wt),'rev-parse','HEAD'),self.git('rev-parse','origin/'+self.branch))
        leases=json.loads((self.common/'bureau/leases.json').read_text())
        self.assertTrue(leases); self.assertTrue(all(lease['interrupted'] for lease in leases.values()))

    def test_shepherd_reports_worker_130_before_consuming_withdrawal(self):
        config=self.repo/'scripts/bureau-config.sh'
        with config.open('a') as out:
            out.write('\nalert_telegram() { printf "%s\\n" "$3" >> "$PR9_FIXTURE/halt-alerts"; }\n')
        (self.repo/'scripts/implement-pipeline.sh').write_text('''#!/bin/bash
set -euo pipefail
source "$(dirname "$0")/bureau-config.sh"
bureau_load_env --export "$BUREAU_ENV_FILE"
bureau_secret_copy API_KEY LINEAR_API_KEY
bureau_stage_enter "$1" "$@"
printf '%s' custom-id-backlog > "$PR9_FIXTURE/state"
mkdir -p "$BUREAU_TICKET_CONTROL_DIR"
: > "$BUREAU_TICKET_CONTROL_DIR/stop-25"
exit 130
''')
        result=self.run_cmd('bash','scripts/shepherd.sh','--no-tmux','--worktree',str(self.wt),'EXP-9')
        self.assertEqual(130,result.returncode,result.stdout+result.stderr)
        self.assertIn('[shepherd] implement-pipeline.sh exit=130 (cancelled-run)',result.stdout)
        self.assertIn('halted (cancelled-run)',result.stdout)
        self.assertEqual('130\n',(self.root/'halt-alerts').read_text())
        self.assertFalse([e for e in self.events() if e['kind'] in ('comment','move','gh-publication')])

    def test_negative_control_post_provider_is_required(self):
        # An outage begins at the first post-pass Git comparison. Uncommitted
        # work needs no preservation push; a comment is still new publication.
        # The immediate post-pass read sees withdrawal; later reads cannot.
        self.control(target=STATES['backlog']); (self.root/'late-read-failure').touch()
        with baseline_fixture(self.root):
            self.assert_withdrawn(self.worker(),25,STATES['backlog'])
            self.assert_no_providers()
        config=self.repo/'scripts/bureau-config.sh'; source=config.read_text()
        needle='  [ "$rc" != 130 ] || exit 130\n  bureau_ticket_checkpoint "${BUREAU_CURRENT_ISSUE:-${ISSUE:-}}"\n'
        self.assertEqual(1,source.count(needle))
        config.write_text(source.replace(needle,'  [ "$rc" != 130 ] || exit 130\n',1))
        with caught_mutation():
            result=self.worker()
            # Exercise the unchanged positive assertions. A TimeoutExpired or
            # another mutation-induced ERROR detects the defect as well.
            self.assert_no_publication(self.events())
            self.assert_withdrawn(result,25,STATES['backlog'])
        self.assert_released()
        print('NEGATIVE CONTROL: removing the post-provider checkpoint posts a new comment and fails the withdrawal assertion')

    def test_custom_active_keys_and_strict_entry(self):
        self.config['agents']['ticket_active_states']=['build','backlog']; self.save_config(); self.state('backlog')
        self.mini_stage('echo authorized >> "$PR9_FIXTURE/published"')
        result=self.worker('ux'); self.assertEqual(0,result.returncode,result.stderr); self.assertTrue((self.root/'published').exists())
        self.config['agents']['ticket_active_states']=['not-configured']; self.save_config()
        self.assertEqual(24,self.worker('ux').returncode)
        self.config['agents'].pop('ticket_active_states'); self.save_config(); (self.root/'fail-reads').touch()
        self.assertEqual(27,self.worker('ux').returncode); self.assert_released()

    def test_active_to_active_conflict_remains_21(self):
        self.state('design')
        self.mini_stage('''printf '%s' custom-id-build > "$PR9_FIXTURE/state"
move_issue "$ISSUE" "$BUREAU_STATE_BUILD_REVIEW"''')
        result=self.worker('ux'); self.assertEqual(21,result.returncode,result.stderr)
        self.assertEqual(STATES['build'],(self.root/'state').read_text())
        self.assertFalse([e for e in self.events() if e['kind']=='move']); self.assert_released()

    def assert_mandatory_guard_stop(self,stage,active,key,code):
        self.state(active); (self.root/'events').write_text('')
        self.control(withdraw_on_snapshot=3,target=STATES.get(key,key),fail_after_snapshot=3)
        result=self.worker(stage)
        self.assertEqual(code,result.returncode,result.stdout+result.stderr)
        events=self.events()
        self.assertEqual([STATES[active],STATES[active],STATES.get(key,key)],
                         [e['state'] for e in events if e['kind']=='snapshot'][:3])
        self.assertFalse([e for e in events if e['kind']=='pass'])
        self.assert_no_publication(events)
        self.assertFalse([e for e in events if e['kind'] in ('move','label')])
        self.assertTrue(list((self.common/'bureau/ticket-control').glob('*/stop-'+str(code))))
        self.assert_released()

    def test_mandatory_implement_guard_withdrawal_matrix(self):
        for index,(key,code) in enumerate([('done',0),('cancelled',26),('duplicate',26),('backlog',25),('unknown-id',25)]):
            with self.subTest(state=key):
                self.wt=self.repo/'.worktrees'/f'mandatory-{index}'
                (self.root/'fail-reads').unlink(missing_ok=True)
                self.assert_mandatory_guard_stop('implement','build',key,code)

    def test_mandatory_other_stage_guards_consume_done_before_routing(self):
        self.config['linear']['labels']['needs_copy']=dict(id='L:needs-copy',name='needs-copy')
        self.save_config()
        for stage,active in [('spec','triage'),('spec-review','spec_review'),('ux','design'),
                             ('copy','copy'),('qa','qa'),('code-review','build_review')]:
            with self.subTest(stage=stage):
                self.wt=self.repo/'.worktrees'/('mandatory-'+stage)
                (self.root/'fail-reads').unlink(missing_ok=True)
                self.assert_mandatory_guard_stop(stage,active,'done',0)

    def test_mandatory_guard_read_failure_remains_strict(self):
        self.control(fail_on_snapshot=3)
        result=self.worker()
        self.assertEqual(27,result.returncode,result.stdout+result.stderr)
        self.assertFalse([e for e in self.events() if e['kind']=='pass'])
        self.assert_no_publication(self.events()); self.assert_released()
        self.assertFalse(list((self.common/'bureau/ticket-control').glob('*/stop-*')))

    def test_shepherd_mandatory_inactive_read_survives_failed_refreshes(self):
        self.control(withdraw_on_snapshot=3,target=STATES['backlog'],fail_after_snapshot=3)
        result=self.run_cmd('bash','scripts/shepherd.sh','--no-tmux','--worktree',str(self.wt),'EXP-9')
        self.assertEqual(25,result.returncode,result.stdout+result.stderr)
        self.assertEqual([STATES['build'],STATES['build'],STATES['backlog']],
                         [e['state'] for e in self.events() if e['kind']=='snapshot'][:3])
        self.assert_no_publication(self.events())
        self.assertFalse([e for e in self.events() if e['kind'] in ('pass','move')])
        self.assertEqual([dict(kind='label',name='shepherd-focused',add=True),
                          dict(kind='label',name='shepherd-focused',add=False)],
                         [e for e in self.events() if e['kind']=='label'])
        self.assertEqual(['lane-2'],json.loads((self.root/'labels').read_text()))
        self.assertTrue(list((self.common/'bureau/ticket-control').glob('*/stop-25')))
        self.assert_released()

    def test_state_projection_preserves_empty_and_ignores_display_names(self):
        command='''source scripts/bureau-config.sh
bureau_load_env --export "$BUREAU_ENV_FILE"
bureau_secret_copy API_KEY LINEAR_API_KEY
get_issue_state EXP-9'''
        # The transport always calls every display name Done, even Build.
        self.state('build')
        self.assertEqual('Build\n',self.run_cmd('bash','-c',command).stdout)
        self.state('unknown-id')
        self.assertEqual('State ID unknown-id\n',self.run_cmd('bash','-c',command).stdout)
        (self.root/'no-state').touch()
        result=self.run_cmd('bash','-c',command)
        self.assertEqual(0,result.returncode,result.stderr)
        self.assertEqual('',result.stdout)
        self.assertEqual(10,self.worker().returncode)
        self.assert_released()

    def test_manual_acceptance_and_external_park(self):
        self.config['agents']['merge_mode']='manual'; self.save_config()
        body='''move_issue "$ISSUE" "$BUREAU_STATE_MERGE"
%s
bureau_acceptance_brief "$ISSUE" 009-ticket-switch --pr 9 --head "$(git rev-parse origin/009-ticket-switch)" --url https://github.com/test/repo/pull/9 --verdict APPROVE'''
        self.state('design'); self.mini_stage(body % ':')
        result=self.worker('ux'); self.assertEqual(0,result.returncode,result.stderr)
        publications=[e for e in self.events() if e['kind'] in ('comment','gh-publication')]
        self.assertEqual(2,len(publications),result.stdout+result.stderr)
        self.assertIn('bureau-acceptance',str(publications)); self.assert_released()
        self.state('design'); (self.root/'events').write_text('')
        self.mini_stage(body % "printf '%s' custom-id-backlog > \"$PR9_FIXTURE/state\"")
        result=self.worker('ux'); self.assertEqual(25,result.returncode,result.stderr)
        self.assertFalse([e for e in self.events() if e['kind'] in ('comment','gh-publication')]); self.assert_released()

    def test_acceptance_rechecks_after_destination_lookup(self):
        self.state('design'); self.control(withdraw_at_acceptance=True)
        self.mini_stage('''bureau_acceptance_brief "$ISSUE" 009-ticket-switch --pr 9 --head "$(git rev-parse origin/009-ticket-switch)" --url https://github.com/test/repo/pull/9 --verdict APPROVE''')
        result=self.worker('ux'); self.assertEqual(25,result.returncode,result.stderr)
        # Ticket delivery precedes the PR lookup that withdraws the ticket.
        self.assertEqual(1,len([e for e in self.events() if e['kind']=='comment']))
        self.assertFalse([e for e in self.events() if e['kind']=='gh-publication'])
        self.assertEqual(STATES['backlog'],(self.root/'state').read_text()); self.assert_released()

    def test_suppressed_acceptance_post_stays_pending_and_retries_same_head(self):
        self.config['agents']['merge_mode']='manual'; self.save_config(); self.state('merge')
        self.mini_stage('''bureau_acceptance_brief "$ISSUE" 009-ticket-switch --pr 9 --head "$(git rev-parse origin/009-ticket-switch)" --url https://github.com/test/repo/pull/9 --verdict APPROVE''')
        # Worker entry, stage entry, brief entry, two authorizations, then the
        # real post_comment's internal checkpoint. Done exits that shell 0.
        self.control(withdraw_on_snapshot=6,target=STATES['done'])
        result=self.worker('ux'); self.assertEqual(0,result.returncode,result.stdout+result.stderr)
        self.assert_no_publication(self.events()); self.assert_released()
        ledger=next((self.common/'bureau/evidence/v1/acceptance').glob('*.json'))
        record=json.loads(ledger.read_text())
        self.assertEqual('pending',record['ticket'])
        self.assertEqual('pending',record['pull_request'])
        head=self.git('rev-parse','origin/'+self.branch)
        self.assertEqual(head,record['head'])
        self.state('merge'); self.control(); (self.root/'events').write_text('')
        result=self.worker('ux'); self.assertEqual(0,result.returncode,result.stdout+result.stderr)
        record=json.loads(ledger.read_text())
        self.assertEqual(head,record['head'])
        self.assertEqual(('delivered','delivered'),(record['ticket'],record['pull_request']))
        self.assertEqual(1,len([e for e in self.events() if e['kind']=='comment']))
        self.assertEqual(1,len([e for e in self.events() if e['kind']=='gh-publication']))
        self.assert_released()

    def test_acceptance_signal_remains_130(self):
        self.state('design'); self.control(signal_at_acceptance=True)
        self.mini_stage('''bureau_acceptance_brief "$ISSUE" 009-ticket-switch --pr 9 --head "$(git rev-parse origin/009-ticket-switch)" --url https://github.com/test/repo/pull/9 --verdict APPROVE''')
        result=self.worker('ux'); self.assertEqual(130,result.returncode,result.stdout+result.stderr)
        self.assertFalse([e for e in self.events() if e['kind']=='gh-publication'])
        self.assertEqual(STATES['design'],(self.root/'state').read_text())
        leases=json.loads((self.common/'bureau/leases.json').read_text())
        self.assertTrue(leases); self.assertTrue(all(lease['interrupted'] for lease in leases.values()))

    def test_queue_and_shepherd_withdrawal(self):
        # One real queue iteration, then another fresh pick, plus real shepherd.
        # Only the loop's wait is omitted; picker/dispatch/worker/config stay real.
        queue=(self.repo/'scripts/queue-loop.sh').read_text()
        run_script=queue[queue.index('run_script() {'):queue.index('\nrun_one() {')]
        prefix='''set -euo pipefail
source scripts/bureau-config.sh
bureau_load_env --export "$BUREAU_ENV_FILE"
bureau_secret_copy API_KEY LINEAR_API_KEY
REPO_DIR="$PWD"; LOG_FILE="$PR9_FIXTURE/queue.log"; MODE=implement
CAPACITY_WAIT=false
preselect_issue() { pipeline_pick_next "$1"; }
'''
        for index,(key,code) in enumerate([('backlog',25),('cancelled',26),('done',0),('unknown-id',25)]):
            for driver in ('queue','shepherd'):
                with self.subTest(state=key,driver=driver):
                    self.state('build'); (self.root/'events').write_text('')
                    self.control(target=STATES.get(key,key)); self.wt=self.repo/'.worktrees'/f'{driver}-{index}'
                    if driver=='queue':
                        command=prefix+run_script+f'\nrun_script implement-pipeline.sh fixture "{self.wt}" && exit 0; exit $?'
                        result=self.run_cmd('bash','-c',command)
                    else:
                        self.env['BUREAU_SHEPHERD_CONFIRM_SECONDS']='0'
                        result=self.run_cmd('bash','scripts/shepherd.sh','--no-tmux','--worktree',str(self.wt),'EXP-9')
                    self.assert_withdrawn(result,code,STATES.get(key,key))
                    self.assertNotIn('shepherd-focused',json.loads((self.root/'labels').read_text()))
                    if driver=='queue':
                        before=len([e for e in self.events() if e['kind']=='pass'])
                        result=self.run_cmd('bash','-c',command)
                        self.assertEqual(code,result.returncode,result.stderr)
                        self.assertEqual(before,len([e for e in self.events() if e['kind']=='pass']))
                    self.git('-C',str(self.wt),'checkout','--detach','-q')


    def test_final_merge_checkpoint_after_green_gates(self):
        self.state('merge'); self.control(withdraw_at_merge=True)
        result=self.worker('merge'); self.assertEqual(25,result.returncode,result.stdout+result.stderr)
        self.assertIn('ALL GATES PASS',result.stdout)
        self.assertEqual(STATES['backlog'],(self.root/'state').read_text())
        self.assertFalse([e for e in self.events() if e['kind'] in ('gh-publication','comment','move')],self.events())
        self.assert_released()

    def test_inline_merge_withdrawal_is_not_a_blocked_gate(self):
        self.config['agents']['merge']=False; self.save_config()
        self.state('build_review'); self.control(withdraw_at_merge=True)
        result=self.worker('code-review'); self.assertEqual(25,result.returncode,result.stdout+result.stderr)
        self.assertIn('ALL GATES PASS',result.stdout)
        self.assertEqual(STATES['backlog'],(self.root/'state').read_text())
        # The review comment predates withdrawal in gh's title/body read.
        events=self.events(); withdrawal=next(i for i,e in enumerate(events) if e['kind']=='withdrawal')
        self.assertFalse([e for e in events[withdrawal+1:]
                          if e['kind'] in ('comment','move','label','gh-publication')],events)
        self.assertEqual(1,len([e for e in events[:withdrawal] if e['kind']=='gh-publication']))
        self.assertNotIn('merge gate is blocked',result.stdout)
        self.assert_released()

    def codex_fixture(self, before):
        self.config['agents']['runner']='codex'
        command=("printf 'gate\\n' >> \"$PR9_FIXTURE/gates\"; "
                 "printf '%s' custom-id-backlog > \"$PR9_FIXTURE/state\"; "
                 "printf '%s\\n' '{\"kind\":\"withdrawal\"}' >> \"$PR9_FIXTURE/events\"; "
                 "git rev-parse origin/009-ticket-switch > \"$PR9_FIXTURE/remote-at-withdrawal\"")
        self.config['repo']['test_command']=command; self.save_config()
        self.env.update(CODEX_HOME=str(self.root/'codex'),FAKE_GATE_COMMAND=command,
                        PR9_EVENT_EMITTER=str(ROOT/'tests/lib/codex_gate_events.py'))
        self.control(target=STATES['backlog'] if before else '')
        program=r'''#!/usr/bin/env python3
import json, os, pathlib, runpy, sys
root=pathlib.Path(os.environ['PR9_FIXTURE'])
if sys.argv[1]=='login': sys.exit(0)
assert sys.argv[1]=='exec'
sys.stdin.read()
with (root/'pids').open('a') as f: f.write(str(os.getpid())+'\n')
if os.environ.get('PR9_PROCESS_AUDIT'):
    with open(os.environ['PR9_PROCESS_AUDIT'],'a') as f: f.write(str(os.getpid())+'\n')
with (root/'events').open('a') as f: f.write('{"kind":"pass","phase":"codex"}\n')
pathlib.Path('unfinished.txt').write_text('Codex work remains available\n')
for path in pathlib.Path('specs').glob('*/tasks.md'): path.write_text(path.read_text().replace('[ ]','[X]'))
runpy.run_path(os.environ['PR9_EVENT_EMITTER'])['emit'](mode='abort')
result=dict(status='NEEDS_HUMAN',tasks_done=2,tasks_skipped=0,tasks_needs_human=1,fixed_review_items=[],
            notes=dict(needs_human=[dict(task_id='T002',kind='gate',command=os.environ['FAKE_GATE_COMMAND'],reason='unprefixed gate abort')],skipped=[],deviations=[]),prose_notes='gate needs shell recovery')
pathlib.Path(sys.argv[sys.argv.index('-o')+1]).write_text(json.dumps(result))
target=json.loads((root/'control').read_text()).get('target')
if target: (root/'state').write_text(target)
print(json.dumps(dict(type='turn.completed',usage=dict(input_tokens=1,output_tokens=1))))
'''
        binary=self.bin/'codex'; binary.write_text(program); binary.chmod(0o755)

    def test_pr6_withdrawn_provider_starts_no_outside_gate(self):
        self.codex_fixture(before=True)
        result=self.worker(); self.assert_withdrawn(result,25,STATES['backlog'])
        self.assertFalse((self.root/'gates').exists())

    def test_pr6_withdrawal_during_outside_gate_stops_publication(self):
        self.codex_fixture(before=False)
        result=self.worker(); self.assertEqual(25,result.returncode,result.stdout+result.stderr)
        self.assertEqual('gate\n',(self.root/'gates').read_text())
        events=self.events(); withdrawal=next(i for i,e in enumerate(events) if e['kind']=='withdrawal')
        self.assertFalse([e for e in events[withdrawal+1:] if e['kind'] in ('push','gh-publication','comment','move')],events)
        self.assertEqual((self.root/'remote-at-withdrawal').read_text().strip(),self.git('rev-parse','origin/'+self.branch))
        self.assertEqual('Codex work remains available\n',(self.wt/'unfinished.txt').read_text())
        self.assert_released()


    def test_parallel_review_drains_every_writer_before_release(self):
        self.state('build_review'); self.control(target=STATES['backlog'])
        result=self.worker('code-review'); self.assertEqual(25,result.returncode,result.stdout+result.stderr)
        self.assertEqual(3,len([e for e in self.events() if e['kind']=='pass']))
        for phase in ('security','performance'):
            leases=json.loads((self.root/('leases-at-'+phase)).read_text())
            self.assertIn('issue:EXP-9',leases); self.assertIn('workspace:'+str(self.wt.resolve()),leases)
        self.assert_released()
        self.assert_only_review_reservation()

    def test_parallel_signal_wins_over_later_withdrawal(self):
        self.state('build_review'); self.control(target=STATES['backlog'],signal_phase='correctness')
        result=self.worker('code-review'); self.assertEqual(130,result.returncode,result.stdout+result.stderr)
        self.assertEqual(3,len([e for e in self.events() if e['kind']=='pass']))
        for phase in ('security','performance'):
            self.assertIn('issue:EXP-9',json.loads((self.root/('leases-at-'+phase)).read_text()))
        leases=json.loads((self.common/'bureau/leases.json').read_text())
        self.assertTrue(leases); self.assertTrue(all(lease['interrupted'] for lease in leases.values()))
        self.assert_only_review_reservation()


if __name__=='__main__': unittest.main(verbosity=2)
