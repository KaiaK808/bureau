"""Real local Git/ownership fixtures; only Linear traffic is replaced."""
import argparse
from contextlib import redirect_stdout
import importlib.util
import io
import json
import os
import shutil
import signal
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'templates/scripts/bureau-runtime.py'
spec = importlib.util.spec_from_file_location('runtime', SCRIPT)
r = importlib.util.module_from_spec(spec); spec.loader.exec_module(r)


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='bureau stage ')
        self.addCleanup(self.temp.cleanup)
        self.repo = Path(self.temp.name).resolve() / 'repo'; self.repo.mkdir()
        for args in [('init', '-q', '-b', 'main'), ('config', 'user.name', 'Test'), ('config', 'user.email', 'test@example.invalid'),
                     ('commit', '-q', '--allow-empty', '-m', 'init'), ('switch', '-q', '-c', 'feat/issue')]:
            r.git(self.repo, *args)
        self.config = {'linear': {'teams': [{'key':'TEAM','id':'team','states': {
            'triage':'s1','spec_review':'s2','build':'s3','qa':'s4','build_review':'s5','merge':'s6','done':'s7'}}]},
            'agents': {'qa':True}, 'repo':{}}
        r.save(self.repo / '.bureau.json', self.config)
        self.store = r.Store(self.repo)
        self.state = 's3'; self.comments = []; self.moves = []; self.labels = []; self.label_adds = []
        self.addCleanup(patch.stopall)
        patch.dict(os.environ, {'BUREAU_CONFIG':str(self.repo / '.bureau.json')}).start()
        patch.object(r, 'shell', self.shell).start()

    def shell(self, repo, function, *args):
        if function == 'bureau_issue_snapshot':
            return json.dumps({'id':'uuid','identifier':'TEAM-1','title':'Test','state':{'id':self.state,'name':'Custom state'}, 'labels':{'nodes':[{'name':name} for name in self.labels]}})
        if function == 'get_issue_comments': return json.dumps(self.comments)
        if function == 'post_comment': self.comments.append({'body':args[1]}); return ''
        if function == 'add_issue_label': self.labels.append(args[1]); self.label_adds.append(args[1]); return ''
        if function == 'move_issue': self.state=args[1]; self.moves.append(args[1]); return ''
        raise AssertionError(function)

    def prepare(self, stage='implement'):
        out=io.StringIO()
        with redirect_stdout(out):
            r.prepare(self.repo, argparse.Namespace(issue='TEAM-1',stage=stage,owner='test-app',allow_merge=False),self.store)
        return json.loads(out.getvalue())

    def result(self, run, **updates):
        value={'run_id':run['run_id'],'stage':run['stage'],'head':r.git(self.repo,'rev-parse','HEAD'),
               'outcome':'complete','summary':'Tests passed','artifacts':[], 'tests':[{'command':'test command','exit_code':0}]}
        value.update(updates)
        path=self.repo / 'result.json'; r.save(path,value)
        return argparse.Namespace(run=run['run_id'],result=str(path))

    def finish(self, args):
        with redirect_stdout(io.StringIO()): r.finish(self.repo,args,self.store)

    def test_competing_issue_and_worktree_claims_and_nested_release(self):
        added=self.store.claim('TEAM-1',self.repo,'a','app')
        self.assertEqual(self.store.claim('TEAM-1',self.repo,'a','child'),[])
        self.store.release('a',[])
        with self.assertRaises(r.Conflict): self.store.claim('TEAM-1',self.repo/'other','b','worker')
        with self.assertRaises(r.Conflict): self.store.claim('TEAM-2',self.repo,'b','worker')
        self.store.release('a',added)
        self.store.claim('TEAM-2',self.repo,'b','worker')

    def test_complete_uses_configured_state_and_repeat_is_idempotent(self):
        run=self.prepare(); args=self.result(run)
        self.finish(args); self.finish(args)
        self.assertEqual(self.moves,['s4']); self.assertEqual(len(self.comments),1)
        self.assertTrue(self.comments[0]['body'].startswith('<!-- bureau-branch: feat/issue -->'))
        self.assertEqual(r.read(self.store.leases),{})
        args=self.result(run,summary='Different')
        with self.assertRaises(r.Conflict): self.finish(args)

    def test_stale_state_head_and_config_cannot_advance(self):
        run=self.prepare(); args=self.result(run)
        self.state='s5'
        with self.assertRaises(r.Conflict): self.finish(args)
        self.state='s3'
        r.git(self.repo,'commit','-q','--allow-empty','-m','new work')
        with self.assertRaises(r.Conflict): self.finish(args)
        args=self.result(run)
        self.config['agents']['qa']=False; r.save(self.repo/'.bureau.json',self.config)
        with self.assertRaises(r.Conflict): self.finish(args)
        self.assertEqual(self.moves,[]); self.assertEqual(self.comments,[])

    def test_partial_retains_state_and_preserves_resume_evidence(self):
        run=self.prepare(); self.finish(self.result(run,outcome='blocked',tests=[],summary='Missing credentials'))
        self.assertEqual(self.moves,[])
        saved=r.read(self.store.root/'runs'/(run['run_id']+'.json'))
        self.assertEqual(saved['result']['summary'],'Missing credentials')

    def test_malformed_and_failed_evidence_cannot_complete(self):
        run=self.prepare()
        for update in ({'tests':[]},{'tests':[{'command':'false','exit_code':1}]},{'stage':'qa'},{'artifacts':['../outside']}):
            with self.subTest(update=update),self.assertRaises(ValueError): self.finish(self.result(run,**update))
        self.assertEqual(self.comments,[])

    def test_review_requires_original_head_and_defaults_no_merge(self):
        self.state='s5'; run=self.prepare('code_review')
        with self.assertRaises(ValueError): self.finish(self.result(run))
        args=self.result(run,verdict='APPROVE'); self.finish(args)
        self.assertEqual(self.moves,[])
        self.store.release(run['run_id']); run=self.prepare('code_review')
        r.git(self.repo,'commit','-q','--allow-empty','-m','new work')
        with self.assertRaises(r.Conflict): self.finish(self.result(run,verdict='APPROVE'))

    def test_retry_after_successful_remote_transition_does_not_duplicate(self):
        run=self.prepare(); args=self.result(run)
        original_save=r.save
        def crash_after_remote(path,value):
            if path.name==run['run_id']+'.json' and value.get('status')=='finished': raise OSError('interrupted')
            original_save(path,value)
        with patch.object(r,'save',crash_after_remote),self.assertRaises(OSError): self.finish(args)
        self.assertEqual(self.moves,['s4'])
        self.finish(args)
        self.assertEqual(self.moves,['s4']); self.assertEqual(len(self.comments),1)

    def test_branch_switch_and_competing_marker_cannot_advance(self):
        run=self.prepare()
        r.git(self.repo,'switch','-q','-c','feat/unrelated')
        with self.assertRaises(r.Conflict): self.finish(self.result(run))
        r.git(self.repo,'switch','-q','feat/issue')
        self.comments=[{'body':'<!-- bureau-branch: feat/other -->'}]
        with self.assertRaises(r.Conflict): self.finish(self.result(run))
        self.store.release(run['run_id'])
        with self.assertRaises(r.Conflict): self.prepare()
        self.assertEqual(self.moves,[])

    def test_pause_flag_visible_across_worktrees_and_does_not_release_claims(self):
        run=self.prepare()
        for action, expected in [('pause',True),('unpause',False)]:
            proc=subprocess.run([sys.executable,str(SCRIPT),'--repo',str(self.repo),action],capture_output=True,text=True)
            self.assertEqual(proc.returncode,0,proc.stderr)
            self.assertEqual(json.loads(proc.stdout)['paused'],expected)
            self.store.assert_owner('TEAM-1',self.repo,run['run_id'])

    def test_app_prepare_revokes_disposable_registration(self):
        key=r.hashlib.sha256(str(self.repo).encode()).hexdigest()
        path=self.store.root/'workers'/key; path.parent.mkdir(parents=True); path.write_text('worker')
        self.prepare(); self.assertFalse(path.exists())

    def test_external_worktree_config_and_readonly_status(self):
        wt=Path(self.temp.name)/'app worktree'
        r.git(self.repo,'worktree','add','--detach',str(wt),'HEAD')
        with patch.dict(os.environ,{},clear=True): self.assertEqual(r.config_for(wt),self.repo/'.bureau.json')
        before=list(self.store.root.glob('**/*'))
        proc=subprocess.run([sys.executable,str(SCRIPT),'--repo',str(wt),'status'],capture_output=True,text=True)
        self.assertEqual(proc.returncode,0,proc.stderr)
        self.assertEqual(before,list(self.store.root.glob('**/*')))
        with patch.dict(os.environ,{'BUREAU_CONFIG':str(self.repo/'missing')}), self.assertRaises(ValueError): r.config_for(wt)

    def test_held_branch_and_unregistered_checkout_are_never_modified(self):
        wt=Path(self.temp.name)/'other app checkout'
        r.git(self.repo,'worktree','add','-b','feat/other',str(wt),'HEAD')
        sentinel=wt/'uncommitted.txt'; sentinel.write_text('keep me')
        helper=ROOT/'templates/scripts/bureau-config.sh'
        cmd=['bash','-c','source "$1"; free_branch_from_other_worktrees feat/other "$PWD"','test',str(helper)]
        proc=subprocess.run(cmd,cwd=self.repo,capture_output=True,text=True)
        self.assertEqual(proc.returncode,21,proc.stderr)
        self.assertEqual(r.git(wt,'branch','--show-current'),'feat/other')
        self.assertEqual(sentinel.read_text(),'keep me')
        cmd=['bash','-c','source "$1"; REPO_DIR="$PWD"; reset_worktree "$2" spec-pipeline.sh','test',str(helper),str(wt)]
        proc=subprocess.run(cmd,cwd=self.repo,capture_output=True,text=True)
        self.assertEqual(proc.returncode,21,proc.stderr)
        self.assertEqual(sentinel.read_text(),'keep me')

    def test_explicit_review_stop_survives_environment_loading(self):
        helper=ROOT/'templates/scripts/bureau-config.sh'
        env=dict(os.environ,BUREAU_NO_MERGE='1',BUREAU_STOP_REQUESTED='0')
        env.pop('BUREAU_CALLER_STOP', None)
        for assignment in ('BUREAU_NO_MERGE=0', 'BUREAU_NO_MERGE=0\nBUREAU_STOP_REQUESTED=0', 'BUREAU_STOP_REQUESTED=1'):
            with self.subTest(assignment=assignment):
                settings=self.repo/'operator.env';settings.write_text(assignment+'\n')
                command=['bash','-e','-c','source "$1"; source "$1"; source "$2"; bureau_stop_requested; bash -e -c \'source "$1"; bureau_stop_requested\' child "$1"; echo stopped','test',str(helper),str(settings)]
                result=subprocess.run(command,cwd=self.repo,env=env,capture_output=True,text=True)
                self.assertEqual(result.returncode,0,result.stderr)
                self.assertEqual(result.stdout.strip(),'stopped')
        # The existing default remains permissive; only explicit stop requests lock it.
        env.update(BUREAU_NO_MERGE='0',BUREAU_STOP_REQUESTED='0')
        settings.write_text('BUREAU_NO_MERGE=0\nBUREAU_STOP_REQUESTED=0\n')
        result=subprocess.run(['bash','-e','-c','source "$1"; source "$2"; if bureau_stop_requested; then exit 99; fi; echo "$BUREAU_STOP_REQUESTED"','test',str(helper),str(settings)],cwd=self.repo,env=env,capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(result.stdout.strip(),'0')

    def test_branch_owner_matches_symlinked_checkout_path(self):
        wt=self.repo.parent/'worker with spaces'
        r.git(self.repo,'worktree','add','-b','feat/worker',str(wt),'HEAD')
        alias=self.repo.parent/'worker alias'; alias.symlink_to(wt, target_is_directory=True)
        sentinel=wt/'uncommitted.txt'; sentinel.write_text('preserve worker progress')
        helper=ROOT/'templates/scripts/bureau-config.sh'
        cmd=['bash','-c','source "$1"; free_branch_from_other_worktrees feat/worker "$2"','test',str(helper)]
        own=subprocess.run(cmd+[str(alias)],cwd=wt,capture_output=True,text=True)
        self.assertEqual(own.returncode,0,own.stderr)
        other=subprocess.run(cmd+[str(self.repo.parent/'new worker')],cwd=self.repo,capture_output=True,text=True)
        self.assertEqual(other.returncode,21,other.stderr)
        self.assertIn(str(wt),other.stderr)
        self.assertEqual(r.git(wt,'branch','--show-current'),'feat/worker')
        self.assertEqual(sentinel.read_text(),'preserve worker progress')

    def test_reset_requires_matching_claim_and_registered_worker(self):
        origin=Path(self.temp.name)/'origin.git'
        subprocess.run(['git','init','-q','--bare',str(origin)],check=True)
        r.git(self.repo,'remote','add','origin',str(origin)); r.git(self.repo,'push','-q','origin','main')
        wt=Path(self.temp.name).resolve()/'disposable worker'
        run='a'*32
        self.store.claim('TEAM-1',wt,run,'background',os.getpid())
        helper=ROOT/'templates/scripts/bureau-config.sh'
        env={**os.environ,'BUREAU_RUN_ID':run,'BUREAU_CURRENT_ISSUE':'TEAM-1','BUREAU_WORKSPACE_MODE':'disposable'}
        command=['bash','-c','source "$1"; REPO_DIR="$PWD"; reset_worktree "$2" spec-pipeline.sh','test',str(helper),str(wt)]
        proc=subprocess.run(command,cwd=self.repo,env=env,capture_output=True,text=True)
        self.assertEqual(proc.returncode,0,proc.stdout+proc.stderr)
        (wt/'scratch').write_text('disposable')
        proc=subprocess.run(command,cwd=self.repo,env={**env,'BUREAU_RUN_ID':'b'*32},capture_output=True,text=True)
        self.assertEqual(proc.returncode,21); self.assertTrue((wt/'scratch').exists())
        proc=subprocess.run(command,cwd=self.repo,env=env,capture_output=True,text=True)
        self.assertEqual(proc.returncode,0,proc.stdout+proc.stderr); self.assertFalse((wt/'scratch').exists())
        key=r.hashlib.sha256(str(wt).encode()).hexdigest()
        (self.store.root/'workers'/key).unlink()
        (wt/'scratch').write_text('now user owned')
        proc=subprocess.run(command,cwd=self.repo,env=env,capture_output=True,text=True)
        self.assertEqual(proc.returncode,21); self.assertTrue((wt/'scratch').exists())

    def test_app_ticket_flows_through_review_without_provider_processes(self):
        self.state='s1'
        specdir=self.repo/'specs/001-issue'; specdir.mkdir(parents=True)
        artifacts=[]
        for name in ('spec.md','plan.md','tasks.md'):
            file=specdir/name; file.write_text('# Fixture addition feature\n')
            artifacts.append(str(file.relative_to(self.repo)))
        run=self.prepare('spec'); self.finish(self.result(run,artifacts=artifacts,tests=[]))
        self.assertEqual(self.state,'s2')
        run=self.prepare('spec_review'); self.finish(self.result(run,artifacts=artifacts,tests=[]))
        self.assertEqual(self.state,'s3')
        run=self.prepare('implement')
        (self.repo/'addition.py').write_text('def add(a, b):\n    return a + b\n')
        test=[sys.executable,'-c','from addition import add; assert add(2, 3) == 5; assert add(-3, 3) == 0']
        outcome=subprocess.run(test,cwd=self.repo,check=False)
        self.assertEqual(outcome.returncode,0)
        r.git(self.repo,'add','addition.py','specs')
        r.git(self.repo,'commit','-q','-m','TEAM-1: addition','-m','Bureau-Generated: true')
        self.finish(self.result(run,artifacts=['addition.py'],tests=[{'command':'python addition assertions','exit_code':outcome.returncode}]))
        self.assertEqual(self.state,'s4')
        run=self.prepare('qa'); outcome=subprocess.run(test,cwd=self.repo,check=False)
        self.finish(self.result(run,tests=[{'command':'independent addition assertions','exit_code':outcome.returncode}]))
        self.assertEqual(self.state,'s5')
        run=self.prepare('code_review')
        self.assertIn('def add',r.git(self.repo,'diff','main...HEAD','--','addition.py'))
        self.finish(self.result(run,verdict='APPROVE',summary='Reviewed the implementation and test evidence.'))
        self.assertEqual(self.state,'s5'); self.assertEqual(self.moves,['s2','s3','s4','s5'])
        self.assertEqual(r.read(self.store.leases),{})

    def test_app_actions_run_project_tests_and_preserve_existing_setup(self):
        self.config['repo']['test_command']='python3 -c "assert 2 + 3 == 5"'
        r.save(self.repo/'.bureau.json',self.config)
        action=ROOT/'templates/scripts/bureau-app.sh'
        for mode in ('status','check','test'):
            proc=subprocess.run(['bash',str(action),mode],cwd=self.repo,capture_output=True,text=True)
            self.assertEqual(proc.returncode,0,proc.stdout+proc.stderr)
        wt=Path(self.temp.name)/'setup worktree'
        r.git(self.repo,'worktree','add','--detach',str(wt),'HEAD')
        (self.repo/'.env').write_text('EXAMPLE=source')
        (wt/'.env').write_text('EXAMPLE=custom')
        proc=subprocess.run([sys.executable,str(SCRIPT),'--repo',str(wt),'setup'],capture_output=True,text=True)
        self.assertEqual(proc.returncode,0,proc.stderr)
        self.assertEqual((wt/'.env').read_text(),'EXAMPLE=custom')
        self.assertEqual((wt/'.bureau.json').read_bytes(),(self.repo/'.bureau.json').read_bytes())

    def test_process_ownership_competes_and_cleans_up_on_termination(self):
        env={**os.environ}; env.pop('BUREAU_RUN_ID',None)
        proc=subprocess.Popen([sys.executable,str(SCRIPT),'--repo',str(self.repo),'exec','--issue','TEAM-1','--',
                               sys.executable,'-c','import time; time.sleep(20)'],env=env,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
        self.addCleanup(lambda: proc.poll() is None and proc.kill())
        deadline=time.monotonic()+5
        while not self.store.leases.exists() and time.monotonic()<deadline: time.sleep(.02)
        with self.assertRaises(r.Conflict): self.store.claim('TEAM-1',self.repo,'other','app')
        proc.terminate(); _,err=proc.communicate(timeout=7)
        self.assertEqual(proc.returncode,130,err.decode())
        run=next(iter(r.read(self.store.leases).values()))['run_id']
        self.assertTrue(all(item['interrupted'] for item in r.read(self.store.leases).values()))
        released=subprocess.run([sys.executable,str(SCRIPT),'--repo',str(self.repo),'release',run],capture_output=True,text=True)
        self.assertEqual(released.returncode,0,released.stderr)
        self.assertEqual(r.read(self.store.leases),{})


    def test_app_blockers_apply_durable_queue_exclusion(self):
        self.config['linear']['labels']={'lane2':{'name':'lane-2'},'needs_human':{'name':'human-decision'}}
        r.save(self.repo/'.bureau.json',self.config)
        for stage,state,updates in [('code_review','s5',{'verdict':'BLOCK'}),('implement','s3',{'outcome':'blocked'}),('qa','s4',{'outcome':'blocked'})]:
            with self.subTest(stage=stage):
                self.state=state; self.labels=['lane-2']; self.label_adds=[]
                run=self.prepare(stage); args=self.result(run,**updates)
                self.finish(args); self.finish(args)
                self.assertEqual(self.state,state); self.assertEqual(self.label_adds,['human-decision'])
                self.assertEqual(r.read(self.store.leases),{})
                payload=self.repo/'queue.json'
                payload.write_text(json.dumps({'data':{'issues':{'nodes':[{'identifier':'TEAM-1','priority':1,'createdAt':'2026-01-01','labels':{'nodes':[{'name':v} for v in self.labels]},'inverseRelations':{'nodes':[]}}]}}}))
                helper=ROOT/'templates/scripts/bureau-config.sh'
                command=['bash','-c','source "$1"; API_KEY=fake; CURL_FIXTURE="$2"; curl() { cat "$CURL_FIXTURE"; }; pipeline_pick_next "$3"','test',str(helper),str(payload),stage.replace('_','-')+'-pipeline.sh']
                picked=subprocess.run(command,cwd=self.repo,capture_output=True,text=True)
                self.assertEqual(picked.returncode,0,picked.stderr); self.assertEqual(picked.stdout,'')

    def test_blocker_label_failure_retains_ownership_for_retry(self):
        self.state='s5'; run=self.prepare('code_review'); args=self.result(run,verdict='BLOCK')
        original=self.shell
        def fail_label(repo,function,*values):
            if function=='add_issue_label': raise OSError('Linear unavailable')
            return original(repo,function,*values)
        with patch.object(r,'shell',fail_label),self.assertRaises(OSError): self.finish(args)
        self.store.assert_owner('TEAM-1',self.repo,run['run_id'])
        self.assertEqual(self.comments,[])
        self.finish(args); self.assertEqual(self.label_adds,['needs-human'])

    def test_spec_review_routes_current_configured_labels_and_keeps_pending_target(self):
        self.config['agents'].update(ux=True,copy=True)
        self.config['linear']['teams'][0]['states'].update(design='s8',copy='s9')
        self.config['linear']['labels']={'needs_ux':{'name':'requires-design'},'needs_copy':{'name':'requires-copy'}}
        r.save(self.repo/'.bureau.json',self.config)
        self.state='s2'; run=self.prepare('spec_review'); self.labels=['requires-design']
        args=self.result(run)
        original=self.shell
        def fail_move(repo,function,*values):
            if function=='move_issue': raise OSError('Linear unavailable')
            return original(repo,function,*values)
        with patch.object(r,'shell',fail_move),self.assertRaises(OSError): self.finish(args)
        self.labels=['requires-copy']
        self.finish(args)
        self.assertEqual(self.state,'s8')
        self.state='s2'; self.labels=[]; run=self.prepare('spec_review'); self.labels=['requires-copy']
        self.finish(self.result(run)); self.assertEqual(self.state,'s9')

    def test_changed_retry_cannot_contradict_published_comment(self):
        self.state='s5'; run=self.prepare('code_review'); args=self.result(run,verdict='REQUEST_CHANGES',summary='Fix the missing authorization check')
        original=self.shell
        def fail_move(repo,function,*values):
            if function=='move_issue': raise OSError('Linear unavailable')
            return original(repo,function,*values)
        with patch.object(r,'shell',fail_move),self.assertRaises(OSError): self.finish(args)
        original_result=r.read(Path(args.result))
        with self.assertRaises(r.Conflict): self.finish(self.result(run,verdict='BLOCK',summary='Different decision'))
        self.assertEqual(len(self.comments),1); self.assertIn('VERDICT: REQUEST_CHANGES',self.comments[0]['body'])
        self.assertEqual(self.state,'s5'); self.store.assert_owner('TEAM-1',self.repo,run['run_id'])
        r.save(Path(args.result),original_result); self.finish(args); self.assertEqual(self.state,'s3')

    def test_app_review_comment_reaches_actual_implementation_consumer(self):
        self.state='s5'; run=self.prepare('code_review')
        self.finish(self.result(run,verdict='REQUEST_CHANGES',summary='Reject unsigned requests before processing them'))
        blob=self.repo/'comments.json'; blob.write_text(json.dumps({'comments':self.comments}))
        source=(ROOT/'templates/scripts/implement-pipeline.sh').read_text()
        function=source[source.index('refresh_review_context() {'):source.index('# open_or_update_pr_draft:')]
        command='get_issue_branch_and_comments() { cat "$1"; }; '+function+'\nrefresh_review_context "$1"'
        proc=subprocess.run(['bash','-c',command,'test',str(blob)],capture_output=True,text=True)
        self.assertEqual(proc.returncode,0,proc.stderr)
        self.assertIn('Reject unsigned requests before processing them',proc.stdout)
        self.assertIn('Address ALL fixes before remaining tasks',proc.stdout)

    def test_nested_worker_cancellation_quarantines_until_explicit_recovery(self):
        origin=Path(self.temp.name)/'origin.git'
        subprocess.run(['git','init','-q','--bare',str(origin)],check=True)
        r.git(self.repo,'remote','add','origin',str(origin)); r.git(self.repo,'push','-q','origin','main','feat/issue')
        r.git(self.repo,'switch','-q','main')
        scripts=self.repo/'scripts'; shutil.copytree(ROOT/'templates/scripts',scripts)
        worker=Path(self.temp.name).resolve()/'worker'
        ready=Path(self.temp.name)/'child.pid'
        stage=scripts/'implement-pipeline.sh'
        stage.write_text('#!/bin/bash\nset -euo pipefail\nsource "$(dirname "$0")/bureau-config.sh"\nbureau_issue_snapshot() { printf \'%s\' \'{"state":{"id":"s3"}}\'; }\nbureau_stage_enter "$1" "$@"\npython3 -c "$PROBE_CODE"\n')
        # Writes are deliberately delayed until the owner has exited. A normal
        # stage/provider teardown can likewise outlive its immediate shell.
        trigger=Path(self.temp.name)/'write-now'
        code='import os,signal,time,pathlib; signal.signal(signal.SIGTERM,signal.SIG_IGN); pathlib.Path(os.environ["PROBE_PID"]).write_text(str(os.getpid())); '+ '\nwhile not pathlib.Path(os.environ["PROBE_TRIGGER"]).exists(): time.sleep(.02)\npathlib.Path("late-stage-write").write_text("preserve this work")\ntime.sleep(20)'
        env={**os.environ,'PROBE_PID':str(ready),'PROBE_TRIGGER':str(trigger),'PROBE_CODE':code}; env.pop('BUREAU_RUN_ID',None)
        log=Path(self.temp.name)/'worker.log'
        with log.open('w') as out:
            proc=subprocess.Popen(['bash',str(scripts/'bureau-worker.sh'),'TEAM-1','implement-pipeline.sh',str(worker),'feat/issue'],cwd=self.repo,env=env,stdout=out,stderr=out)
        def cleanup():
            if ready.exists():
                try: os.kill(int(ready.read_text()),signal.SIGKILL)
                except ProcessLookupError: pass
            if proc.poll() is None: proc.kill(); proc.wait()
        self.addCleanup(cleanup)
        deadline=time.monotonic()+5
        while not ready.exists() and time.monotonic()<deadline: time.sleep(.02)
        self.assertTrue(ready.exists(),'creative stage did not start: '+log.read_text())
        run=next(iter(r.read(self.store.leases).values()))['run_id']
        proc.terminate(); self.assertEqual(proc.wait(timeout=7),130)
        self.store.assert_owner('TEAM-1',worker,run)
        with self.assertRaises(r.Conflict): self.store.claim('TEAM-2',worker,'other','background')
        key=r.hashlib.sha256(str(worker).encode()).hexdigest()
        self.assertFalse((self.store.root/'workers'/key).exists())
        release=subprocess.run([sys.executable,str(SCRIPT),'--repo',str(self.repo),'release',run],capture_output=True,text=True)
        self.assertEqual(release.returncode,21,release.stdout+release.stderr)
        trigger.touch()
        deadline=time.monotonic()+3
        while not (worker/'late-stage-write').exists() and time.monotonic()<deadline: time.sleep(.02)
        self.assertTrue((worker/'late-stage-write').exists())
        self.store.assert_owner('TEAM-1',worker,run)


if __name__=='__main__': unittest.main()
