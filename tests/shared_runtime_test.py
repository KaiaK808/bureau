"""Real local Git/ownership fixtures; only Linear traffic is replaced."""
import argparse
import hashlib
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

import git_maintenance
from governance_cases import (FENCED_GOVERNANCE, GOVERNANCE_FIRST,
                              COMMENT_HIDDEN_GOVERNANCE, COLUMN_ZERO_GOVERNANCE,
                              ROUND8_PROVIDER_INVALID)

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = Path(os.environ.get('BUREAU_TEST_RUNTIME_SCRIPT',ROOT / 'templates/scripts/bureau-runtime.py'))
spec = importlib.util.spec_from_file_location('runtime', SCRIPT)
r = importlib.util.module_from_spec(spec); spec.loader.exec_module(r)


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='bureau stage ')
        self.addCleanup(self.temp.cleanup)
        self.repo = self.make_repo(Path(self.temp.name).resolve() / 'repo')
        self.config = {'linear': {'teams': [{'key':'TEAM','id':'team','states': {
            'triage':'s1','spec_review':'s2','build':'s3','qa':'s4','build_review':'s5','merge':'s6','done':'s7'}}]},
            'agents': {'qa':True}, 'repo':{}}
        r.save(self.repo / '.bureau.json', self.config)
        self.store = r.Store(self.repo)
        self.state = 's3'; self.comments = []; self.moves = []; self.labels = []; self.label_adds = []
        self.addCleanup(patch.stopall)
        patch.dict(os.environ, {'BUREAU_CONFIG':str(self.repo / '.bureau.json')}).start()
        self.real_shell = r.shell
        patch.object(r, 'shell', self.shell).start()

    def make_repo(self, repo):
        # git maintenance off in the repository's own config: r.git and the stages run git with their
        # own environment (git_maintenance.py).
        repo.mkdir()
        for args in [('init', '-q', '-b', 'main'), git_maintenance.OFF_CONFIG, ('config', 'user.name', 'Test'), ('config', 'user.email', 'test@example.invalid'),
                     ('commit', '-q', '--allow-empty', '-m', 'init'), ('switch', '-q', '-c', 'feat/issue')]:
            r.git(repo, *args)
        return repo

    def bare_origin(self, name='origin.git'):
        origin = Path(self.temp.name) / name
        subprocess.run(['git', 'init', '-q', '--bare', str(origin)], check=True)
        subprocess.run(['git', '--git-dir', str(origin), *git_maintenance.OFF_CONFIG], check=True)  # for receive-pack behind a push
        return origin

    def test_fixture_repository_and_push_start_no_git_maintenance(self):
        trace = Path(self.temp.name) / 'trace2.json'
        with patch.dict(os.environ, {'GIT_TRACE2_EVENT': str(trace)}):
            repo = self.make_repo(Path(self.temp.name).resolve() / 'traced repo')
            origin = self.bare_origin('traced origin.git')
            r.git(repo, 'remote', 'add', 'origin', str(origin)); r.git(repo, 'push', '-q', 'origin', 'main', 'feat/issue')
        commands = git_maintenance.commands(trace)
        self.assertTrue(any('commit' in argv for argv in commands), commands)  # both sides were traced
        self.assertTrue(any('receive-pack' in ' '.join(argv) for argv in commands), commands)
        self.assertEqual(git_maintenance.maintenance_started(trace), [])

    def shell(self, repo, function, *args):
        if function == 'bureau_issue_snapshot':
            return json.dumps({'id':'uuid','identifier':'TEAM-1','title':'Test','state':{'id':self.state,'name':'Custom state'}, 'labels':{'nodes':[{'name':name} for name in self.labels]}})
        if function == 'get_issue_comments': return json.dumps(self.comments)
        if function == 'bureau_spec_dir_for_branch': return self.real_shell(repo, function, *args)
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
        origin=self.bare_origin()
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

    def test_reset_links_configured_paths_and_clean_keeps_their_target(self):
        # repo.worktree_links through the real reset_worktree: every reset links the main
        # checkout's .venv into the disposable worker, the worker stays clean (a dirty worker
        # would be preserved and the next reset would stop with 21), and the next reset's
        # clean -fdx removes only the link. Paths contain spaces (the temp dir does).
        origin=self.bare_origin()
        (self.repo/'.gitignore').write_text('.venv\n.bureau.json\n')
        r.git(self.repo,'add','.gitignore'); r.git(self.repo,'commit','-q','-m','ignore venv')
        r.git(self.repo,'remote','add','origin',str(origin)); r.git(self.repo,'push','-q','origin','HEAD:main')
        venv=self.repo/'.venv'/'bin'; venv.mkdir(parents=True); (venv/'python').write_text('main venv')
        config=dict(self.config, repo={'worktree_links':['.venv']}); r.save(self.repo/'.bureau.json',config)
        wt=Path(self.temp.name).resolve()/'disposable worker'
        run='c'*32
        self.store.claim('TEAM-1',wt,run,'background',os.getpid())
        helper=ROOT/'templates/scripts/bureau-config.sh'
        env={**os.environ,'BUREAU_RUN_ID':run,'BUREAU_CURRENT_ISSUE':'TEAM-1','BUREAU_WORKSPACE_MODE':'disposable'}
        command=['bash','-c','source "$1"; REPO_DIR="$PWD"; reset_worktree "$2" spec-pipeline.sh','test',str(helper),str(wt)]
        for attempt in (1,2):
            with self.subTest(reset=attempt):
                proc=subprocess.run(command,cwd=self.repo,env=env,capture_output=True,text=True)
                self.assertEqual(proc.returncode,0,proc.stdout+proc.stderr)
                self.assertIn('Linked .venv from the main checkout',proc.stdout)
                link=wt/'.venv'
                self.assertTrue(link.is_symlink(),proc.stdout+proc.stderr)
                self.assertEqual(os.readlink(link),str(self.repo/'.venv'))
                self.assertEqual((link/'bin'/'python').read_text(),'main venv')
                self.assertEqual(r.git(wt,'status','--porcelain','--untracked-files=all'),'')
                self.assertEqual((venv/'python').read_text(),'main venv')
        self.assertFalse((self.repo/'.venv').is_symlink())

    def test_reset_survives_a_dash_entry_under_errexit(self):
        # The worker runs under set -euo pipefail. An entry starting with '-' used to reach
        # `dirname`, which read it as an option and failed; the failed assignment ended the
        # worker before the stage, for every ticket.
        origin=self.bare_origin()
        (self.repo/'.gitignore').write_text('.venv\n-venv\n.bureau.json\n')
        r.git(self.repo,'add','.gitignore'); r.git(self.repo,'commit','-q','-m','ignore venvs')
        r.git(self.repo,'remote','add','origin',str(origin)); r.git(self.repo,'push','-q','origin','HEAD:main')
        for name in ('-venv','.venv'): (self.repo/name/'bin').mkdir(parents=True)
        r.save(self.repo/'.bureau.json',dict(self.config, repo={'worktree_links':['-venv','.venv']}))
        wt=Path(self.temp.name).resolve()/'disposable worker'
        run='d'*32
        self.store.claim('TEAM-1',wt,run,'background',os.getpid())
        helper=ROOT/'templates/scripts/bureau-config.sh'
        env={**os.environ,'BUREAU_RUN_ID':run,'BUREAU_CURRENT_ISSUE':'TEAM-1','BUREAU_WORKSPACE_MODE':'disposable'}
        command=['bash','-c','set -euo pipefail; source "$1"; REPO_DIR="$PWD"; reset_worktree "$2" spec-pipeline.sh; echo reset-done','test',str(helper),str(wt)]
        proc=subprocess.run(command,cwd=self.repo,env=env,capture_output=True,text=True)
        self.assertEqual(proc.returncode,0,proc.stdout+proc.stderr)
        self.assertIn('reset-done',proc.stdout)
        for name in ('-venv','.venv'):
            self.assertEqual(os.readlink(wt/name),str(self.repo/name))
        self.assertEqual(r.git(wt,'status','--porcelain','--untracked-files=all'),'')

    def test_app_ticket_flows_through_review_without_provider_processes(self):
        self.state='s1'
        r.git(self.repo,'switch','-q','-c','001-team-1')
        specdir=self.repo/'specs/001-team-1'; specdir.mkdir(parents=True)
        artifacts=[]
        for name in ('spec.md','plan.md','tasks.md'):
            file=specdir/name; file.write_text('# Fixture addition feature\n')
            artifacts.append(str(file.relative_to(self.repo)))
        (specdir/'spec.md').write_text('# Feature\n## Governance and security\nDecision: not required\nRationale: The local arithmetic fixture processes no credentials or personal data.\n')
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

    def front_exec(self, script, **env):
        """The wrapper in front of a run (`bureau-runtime.py exec`, as shepherd.sh
        starts it) around `bash -c <script>`, which stands in for the shepherd;
        no signal reaches the wrapper."""
        env={**os.environ,'PY':sys.executable,'RUNTIME':str(SCRIPT),'REPO':str(self.repo),**env}
        env.pop('BUREAU_RUN_ID',None); env.pop('BUREAU_RUN_DEPTH',None)
        return subprocess.run([sys.executable,str(SCRIPT),'--repo',str(self.repo),'exec','--issue','TEAM-1','--','bash','-c',script],
                              env=env,capture_output=True,text=True,timeout=60)

    def release_run(self, run):
        return subprocess.run([sys.executable,str(SCRIPT),'--repo',str(self.repo),'release',run],capture_output=True,text=True)

    def test_child_exit_130_keeps_the_run_interrupted_while_an_inner_wrapper_still_runs(self):
        # A stop sent to an inner process group only (v3.2, O1; the maintainer's
        # decision: a child's 130 counts as interrupted). The shepherd ends with 130
        # while the worker's wrapper of the same run still waits for its stage (a
        # slow EXIT trap, such as implement's deferred push), so no inner wrapper has
        # recorded the interrupt yet. The order is forced with files, without a
        # signal. Before, the wrapper in front returned the 130 and released the
        # leases while the stage still ran, and printed no steps.
        tmp=Path(self.temp.name)
        started, may_end, ended, inner_log = tmp/'stage-started', tmp/'stage-may-end', tmp/'stage-ended', tmp/'inner.log'
        inner_pid=tmp/'inner.pid'
        def end_stage():
            # The inner wrapper and its stage end with the test, also when it fails early,
            # before the temporary directory goes (cleanups run last-in, first-out).
            may_end.touch()
            pid=int(inner_pid.read_text()) if inner_pid.exists() else None
            deadline=time.monotonic()+20
            while pid and time.monotonic()<deadline:
                try: os.kill(pid,0)
                except ProcessLookupError: break
                time.sleep(.05)
        self.addCleanup(end_stage)
        stage=('import pathlib, time\nstarted, may_end = pathlib.Path(%r), pathlib.Path(%r)\nstarted.touch()\n'
               'while not may_end.exists() and started.exists(): time.sleep(.02)\n'
               'pathlib.Path(%r).touch()' % (str(started), str(may_end), str(ended)))
        shepherd=('"$PY" "$RUNTIME" --repo "$REPO" exec --issue TEAM-1 -- "$PY" -c "$STAGE" > "$INNER_LOG" 2>&1 &\n'
                  'echo $! > "$INNER_PID"\nwhile [ ! -e "$STARTED" ]; do sleep .02; done\nexit 130\n')
        proc=self.front_exec(shepherd,STAGE=stage,STARTED=str(started),INNER_LOG=str(inner_log),INNER_PID=str(inner_pid))
        self.assertEqual(proc.returncode,130,proc.stderr)
        self.assertFalse(ended.exists(),'the stage ended first: the order was not forced')
        leases=r.read(self.store.leases,{})
        self.assertEqual(sorted(leases),['issue:TEAM-1','workspace:'+str(self.repo)],proc.stderr)
        self.assertTrue(all(lease['interrupted'] for lease in leases.values()),leases)
        run=leases['issue:TEAM-1']['run_id']
        self.assertEqual(proc.stderr.count('Interrupted Bureau run '+run),1,proc.stderr)
        self.assertIn('bureau-runtime.py release '+run,proc.stderr)
        # The stage still runs under the inner wrapper: release refuses until it ends.
        held=self.release_run(run)
        self.assertEqual(held.returncode,21,held.stdout+held.stderr)
        self.assertIn('still alive',held.stderr)
        may_end.touch()
        deadline=time.monotonic()+15
        while (released:=self.release_run(run)).returncode!=0 and time.monotonic()<deadline: time.sleep(.1)
        self.assertEqual(released.returncode,0,released.stdout+released.stderr+inner_log.read_text())
        self.assertTrue(ended.exists())
        self.assertEqual(r.read(self.store.leases),{})

    def test_child_exit_130_without_an_inner_wrapper_keeps_the_run_interrupted(self):
        # The shepherd stopped while no stage ran (a Linear read, its own wait before a
        # retry): no inner wrapper exists that could record the interrupt.
        proc=self.front_exec('exit 130')
        self.assertEqual(proc.returncode,130,proc.stderr)
        leases=r.read(self.store.leases,{})
        self.assertEqual(len(leases),2,proc.stderr)
        self.assertTrue(all(lease['interrupted'] for lease in leases.values()),leases)
        self.assertEqual(proc.stderr.count('Interrupted Bureau run '),1,proc.stderr)
        self.assertEqual(self.release_run(leases['issue:TEAM-1']['run_id']).returncode,0)

    def test_child_exit_other_than_130_still_releases_the_run(self):
        # Only 130 is a stop: a finished run (0), a halt (1, 25) and a child that died
        # by a signal it did not handle (129, 143) release the leases as before.
        for code in (0, 1, 25, 129, 143):
            with self.subTest(code=code):
                # Each code on its own: leases a failed code left behind would turn the next
                # run into an ownership conflict instead.
                self.store.leases.unlink(missing_ok=True)
                proc=self.front_exec('exit %d' % code)
                self.assertEqual(proc.returncode,code,proc.stderr)
                self.assertEqual(r.read(self.store.leases,{}),{},proc.stderr)
                self.assertNotIn('Interrupted Bureau run',proc.stderr)


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
        r.git(self.repo,'switch','-q','-c','001-team-1')
        specdir=self.repo/'specs/001-team-1'; specdir.mkdir(parents=True)
        (specdir/'spec.md').write_text('## Governance and security\nDecision: not required\nRationale: This copy fixture handles no credentials or personal data.\n')
        artifacts=['specs/001-team-1/spec.md']
        self.state='s2'; run=self.prepare('spec_review'); self.labels=['requires-design']
        args=self.result(run,artifacts=artifacts)
        original=self.shell
        def fail_move(repo,function,*values):
            if function=='move_issue': raise OSError('Linear unavailable')
            return original(repo,function,*values)
        with patch.object(r,'shell',fail_move),self.assertRaises(OSError): self.finish(args)
        self.labels=['requires-copy']
        self.finish(args)
        self.assertEqual(self.state,'s8')
        self.state='s2'; self.labels=[]; run=self.prepare('spec_review'); self.labels=['requires-copy']
        self.finish(self.result(run,artifacts=artifacts)); self.assertEqual(self.state,'s9')

    def test_app_spec_and_review_completion_reject_invalid_governance_even_with_pass(self):
        r.git(self.repo,'switch','-q','-c','001-team-1')
        specdir=self.repo/'specs/001-team-1'; specdir.mkdir(parents=True)
        artifacts=['specs/001-team-1/'+name for name in ('spec.md','plan.md','tasks.md')]
        for name in ('plan.md','tasks.md'): (specdir/name).write_text('# fixture\n')
        for stage, state in (('spec','s1'),('spec_review','s2')):
            self.state=state; run=self.prepare(stage)
            for body in ('# Missing\n','## Governance and security\n','## Governance and security\nTBD\n',
                         '## Governance and security\nDecision: N/A\nRationale: N/A\n'):
                (specdir/'spec.md').write_text(body)
                with self.subTest(stage=stage,body=body),self.assertRaises(ValueError):
                    self.finish(self.result(run,artifacts=artifacts,summary='Model PASS'))
            self.assertEqual(self.moves,[])
            (specdir/'spec.md').write_text('## Governance and security\nDecision: not required\nRationale: This documentation fixture handles no credentials or personal data.\n')
            self.finish(self.result(run,artifacts=artifacts))
            self.moves=[]

    def test_app_spec_and_review_completion_reject_fenced_governance(self):
        r.git(self.repo,'switch','-q','-c','001-team-1')
        specdir=self.repo/'specs/001-team-1'; specdir.mkdir(parents=True)
        artifacts=['specs/001-team-1/'+name for name in ('spec.md','plan.md','tasks.md')]
        valid='## Governance and security\nDecision: not required\nRationale: This documentation fixture handles no credentials or personal data.\n'
        for name in ('plan.md','tasks.md'): (specdir/name).write_text('# fixture\n')
        cases=(
            '````md\n```\n'+valid+'````\n',
            '```md\n``` trailing text\n'+valid+'```\n',
            '~~~~md\n~~~\n'+valid+'~~~~\n',
            '~~~md\n~~~ trailing text\n'+valid+'~~~\n',
            '   ````md\n   ```\n'+valid+'   ````\n',
            '   ~~~~md\n   ~~~\n'+valid+'   ~~~~\n',
        )
        for stage,state in (('spec','s1'),('spec_review','s2')):
            with self.subTest(stage=stage):
                self.state=state; run=self.prepare(stage)
                for body in cases:
                    (specdir/'spec.md').write_text(body)
                    with self.subTest(body=body),self.assertRaisesRegex(ValueError,'Governance and security'):
                        self.finish(self.result(run,artifacts=artifacts,summary='Model PASS'))
                    self.assertEqual(self.moves,[])
                    self.assertEqual(self.comments,[])
                (specdir/'spec.md').write_text(valid+'   ````md\nexample\n   `````\t\n')
                self.finish(self.result(run,artifacts=artifacts))
            self.moves=[]; self.comments=[]

    def test_real_provider_pass_obeys_governance_placement(self):
        r.git(self.repo,'switch','-q','-c','001-team-1')
        specdir=self.repo/'specs/001-team-1'; specdir.mkdir(parents=True)
        artifacts=['specs/001-team-1/'+name for name in ('spec.md','plan.md','tasks.md')]
        for name in ('plan.md','tasks.md'): (specdir/name).write_text('# fixture\n')
        # The fake CLI supplies PASS; the shipped adapter parses its envelope,
        # persists a completion receipt and forwards the actual app result.
        binary=Path(self.temp.name)/'bin'; binary.mkdir()
        cli=binary/'claude'
        cli.write_text('#!'+sys.executable+'\nimport json, os, sys\nfrom pathlib import Path\n'
                       'if sys.argv[1:2]==["auth"]: print(json.dumps({"loggedIn":True}))\n'
                       'elif sys.argv[1:2]==["--help"]: print("fixture CLI")\n'
                       'else: print(json.dumps({"result":Path(os.environ["GOVERNANCE_RESULT"]).read_text()}))\n')
        cli.chmod(0o755)
        prompt=Path(self.temp.name)/'prompt.txt'; prompt.write_text('Return the completed stage result: Model PASS')
        payload=Path(self.temp.name)/'provider-result.json'
        provider=SCRIPT.with_name('bureau-provider.py')
        env={key:value for key,value in os.environ.items() if not key.startswith('BUREAU_')}
        env.update(PATH=str(binary)+os.pathsep+os.environ['PATH'],GOVERNANCE_RESULT=str(payload),
                   BUREAU_PROVIDER_LOG_DIR=str(Path(self.temp.name)/'provider-evidence'))
        def adapter_result(run):
            args=self.result(run,artifacts=artifacts,summary='Model PASS')
            payload.write_text(Path(args.result).read_text())
            proc=subprocess.run([sys.executable,str(provider),'--stage',run['stage'],
                                 '--config',str(self.repo/'.bureau.json'),'--repo',str(self.repo),
                                 '--prompt-file',str(prompt)],env=env,capture_output=True,text=True,timeout=15)
            self.assertEqual(proc.returncode,0,proc.stdout+proc.stderr)
            self.assertEqual(json.loads(proc.stdout)['summary'],'Model PASS')
            Path(args.result).write_text(proc.stdout)
            return args
        for stage,state,target in (('spec','s1','s2'),('spec_review','s2','s3')):
            self.state=state; self.moves=[]; self.comments=[]; run=self.prepare(stage)
            try:
                for name,body in {**FENCED_GOVERNANCE, **COMMENT_HIDDEN_GOVERNANCE, **COLUMN_ZERO_GOVERNANCE, **ROUND8_PROVIDER_INVALID}.items():
                    with self.subTest(stage=stage,case=name):
                        (specdir/'spec.md').write_text(body)
                        args=adapter_result(run)
                        with self.assertRaisesRegex(ValueError,'Governance and security') as error: self.finish(args)
                        if name in FENCED_GOVERNANCE:
                            self.assertIn('move the Governance and security section above the first code block',str(error.exception))
                        self.assertEqual(self.moves,[]); self.assertEqual(self.comments,[])
                        self.assertEqual(self.state,state)
                        self.assertEqual(r.read(self.store.root/'runs'/(run['run_id']+'.json'))['status'],'prepared')
                        self.store.assert_owner('TEAM-1',self.repo,run['run_id'])
            finally:
                self.store.release(run['run_id'])
            # Governance before code completes through the same real adapter.
            for name, body in GOVERNANCE_FIRST.items():
                with self.subTest(stage=stage,case=name,valid=True):
                    self.state=state; self.moves=[]; self.comments=[]; run=self.prepare(stage)
                    try:
                        (specdir/'spec.md').write_text(body)
                        self.finish(adapter_result(run))
                        self.assertEqual(self.moves,[target]); self.assertEqual(self.state,target)
                    finally:
                        self.store.release(run['run_id'])
                    self.moves=[]; self.comments=[]

    def test_app_completion_uses_shared_canonical_directory_resolver(self):
        r.git(self.repo,'switch','-q','-c','codex/001-team-1')
        self.comments=[{'body':'<!-- bureau-branch: codex/001-team-1 -->'}]
        folder=self.repo/'specs/001-team-1'; folder.mkdir(parents=True)
        (folder/'spec.md').write_text('## Governance and security\nDecision: not required\nRationale: This change handles no credentials.\n')
        self.state='s2'; run=self.prepare('spec_review')
        self.assertEqual(run['feature_directory'],str(folder))
        self.finish(self.result(run,artifacts=['specs/001-team-1/spec.md']))
        self.assertEqual(self.moves,['s3'])

    def test_app_completion_rejects_another_tickets_valid_spec(self):
        r.git(self.repo,'switch','-q','-c','001-team-1')
        self.comments=[{'body':'<!-- bureau-branch: 001-team-1 -->'}]
        for name, body in (('001-team-1', '# Missing governance\n'),
                           ('999-other-ticket', '## Governance and security\nDecision: not required\nRationale: This change handles no credentials.\n')):
            folder=self.repo/'specs'/name; folder.mkdir(parents=True)
            for artifact in ('spec.md','plan.md','tasks.md'): (folder/artifact).write_text(body)
        foreign=['specs/999-other-ticket/'+name for name in ('spec.md','plan.md','tasks.md')]
        for stage, state in (('spec','s1'),('spec_review','s2')):
            self.state=state; self.comments=[{'body':'<!-- bureau-branch: 001-team-1 -->'}]; self.moves=[]
            run=self.prepare(stage)
            try:
                with self.subTest(stage=stage):
                    self.assertEqual(run['feature_directory'], str(self.repo/'specs/001-team-1'))
                    with self.assertRaisesRegex(ValueError, 'canonical feature directory'):
                        self.finish(self.result(run,artifacts=foreign,summary='Model PASS'))
                    self.assertEqual(self.moves,[]); self.assertEqual(len(self.comments),1)
                    self.assertEqual(r.read(self.store.root/'runs'/(run['run_id']+'.json'))['status'], 'prepared')
            finally:
                self.store.release(run['run_id'])

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

    def implementation_feedback(self):
        # The implement stage's own reader, over the comments this run posted (newest first).
        blob=self.repo/'comments.json'; blob.write_text(json.dumps({'comments':list(reversed(self.comments))}))
        source=(ROOT/'templates/scripts/implement-pipeline.sh').read_text()
        function=source[source.index('refresh_review_context() {'):source.index('# open_or_update_pr_draft:')]
        command='get_issue_branch_and_comments() { cat "$1"; }; '+function+'\nrefresh_review_context "$1"'
        proc=subprocess.run(['bash','-c',command,'test',str(blob)],capture_output=True,text=True)
        self.assertEqual(proc.returncode,0,proc.stderr)
        return proc.stdout

    def test_app_review_comment_reaches_actual_implementation_consumer(self):
        self.state='s5'; run=self.prepare('code_review')
        self.finish(self.result(run,verdict='REQUEST_CHANGES',summary='Reject unsigned requests before processing them'))
        feedback=self.implementation_feedback()
        self.assertIn('Reject unsigned requests before processing them',feedback)
        self.assertIn('Address ALL fixes before remaining tasks',feedback)

    def test_app_review_block_reaches_implementation_on_a_restart(self):
        # A BLOCK keeps the ticket in Build Review with needs-human; an operator who sends it
        # back to Build gets the review's findings in the implement prompt (v3.2; before, only
        # a VERDICT: REQUEST_CHANGES line counted).
        self.state='s5'; run=self.prepare('code_review')
        self.finish(self.result(run,verdict='BLOCK',summary='Verify the webhook signature before parsing the body'))
        self.assertEqual(self.state,'s5'); self.assertIn('needs-human',self.label_adds)
        self.assertIn('\nVERDICT: BLOCK',self.comments[-1]['body'])
        feedback=self.implementation_feedback()
        self.assertIn('Verify the webhook signature before parsing the body',feedback)
        self.assertIn('Address ALL fixes before remaining tasks',feedback)

    def test_nested_worker_cancellation_quarantines_until_explicit_recovery(self):
        origin=self.bare_origin()
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


    def manifests(self):
        return [r.read(p) for p in (self.store.root/'evidence/v1/runs').glob('*/manifest.json')]

    def test_corrupt_other_ticket_evidence_is_quarantined_without_stopping_stage(self):
        evidence=r.e.Store(self.store.root)
        for name in ('runs/'+('f'*32)+'/manifest.json','waits/'+('e'*32)+'.json','tickets/TEAM-2.json'):
            path=evidence.root/name; path.parent.mkdir(parents=True,exist_ok=True)
            path.write_text('{CORRUPT_PRIVATE_SENTINEL')
        result=self.front_exec('echo stage-ran; exit 14')
        self.assertEqual(result.returncode,14,result.stderr)
        self.assertEqual(result.stdout.strip(),'stage-ran')
        self.assertEqual(result.stderr.count('bureau evidence: WARN:'),1)
        self.assertEqual(len(list((evidence.root/'quarantine').glob('*.json'))),3)
        for path in evidence.root.rglob('*'):
            if path.is_file(): self.assertNotIn(b'CORRUPT_PRIVATE_SENTINEL',path.read_bytes())
        self.assertEqual(r.read(self.store.leases),{})

    def test_enospc_manifest_attempt_gate_wait_and_pruning_are_observational(self):
        import errno
        args=argparse.Namespace(workspace=None,entry='qa-pipeline.sh',issue='TEAM-1',
                                command=['bash','-c','echo ran > evidence-stage-marker; exit 14'])
        with patch.object(r.e,'atomic',side_effect=OSError(errno.ENOSPC,'injected full device')), \
                patch.object(r.e,'_warned',False), patch.object(r.e.os,'write',return_value=0) as diagnostic:
            self.assertEqual(r.execute(self.repo,args,self.store),14)
        self.assertEqual((self.repo/'evidence-stage-marker').read_text().strip(),'ran')
        self.assertEqual(diagnostic.call_count,1)
        self.assertEqual(diagnostic.call_args.args[0],2)
        self.assertIn(b'bureau evidence: WARN:',diagnostic.call_args.args[1])
        self.assertEqual(r.read(self.store.leases),{})

    def test_closed_stderr_finalization_preserves_stage_exits(self):
        source='''
import argparse, importlib.util, pathlib, sys
script, repo, code, fault = sys.argv[1:]
spec = importlib.util.spec_from_file_location('runtime', script)
r = importlib.util.module_from_spec(spec); spec.loader.exec_module(r)
repo = pathlib.Path(repo)
if fault == 'yes':
    def fail(*args, **kwargs):
        (repo / 'finalization-attempted').write_text('attempted')
        raise OSError('injected optional finalization error')
    r.e.Store.end_attempt = fail
args = argparse.Namespace(workspace=None, entry='qa-pipeline.sh', issue='TEAM-1',
                          command=['bash', '-c', 'echo stage-ran; exit ' + code])
sys.exit(r.execute(repo, args, r.Store(repo)))
'''
        for code in (0,14,25,124):
            for fault in ('no','yes'):
                with self.subTest(code=code,fault=fault):
                    marker=self.repo/'finalization-attempted'; marker.unlink(missing_ok=True)
                    reader,writer=os.pipe(); os.close(reader)
                    try:
                        proc=subprocess.Popen([sys.executable,'-c',source,str(SCRIPT),str(self.repo),str(code),fault],
                                              stdout=subprocess.PIPE,stderr=writer,text=True)
                    finally: os.close(writer)
                    try: output,_=proc.communicate(timeout=10)
                    finally:
                        if proc.poll() is None: proc.kill()
                        proc.wait()
                    self.assertEqual(proc.returncode,code,'broken stderr changed stage exit '+str(code))
                    self.assertEqual(output.strip(),'stage-ran')
                    self.assertEqual(marker.exists(),fault=='yes')
                    self.assertEqual(r.read(self.store.leases),{},'finalization skipped execution cleanup')

    def test_unreadable_evidence_and_lock_timeout_preserve_normal_stage_exit(self):
        import fcntl
        evidence=r.e.Store(self.store.root); evidence.root.mkdir(parents=True)
        guard=evidence.root/'guard'; guard.touch(); guard.chmod(0)
        try:
            result=self.front_exec('echo unreadable-stage-ran; exit 18')
            self.assertEqual(result.returncode,18,result.stderr)
            self.assertEqual(result.stdout.strip(),'unreadable-stage-ran')
            self.assertEqual(result.stderr.count('bureau evidence: WARN:'),1)
        finally: guard.chmod(0o600)
        with guard.open('a') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX)
            result=self.front_exec('echo locked-stage-ran; exit 22')
        self.assertEqual(result.returncode,22,result.stderr)
        self.assertEqual(result.stdout.strip(),'locked-stage-ran')
        self.assertEqual(result.stderr.count('bureau evidence: WARN:'),1)

    def test_repeated_execute_ownership_refusals_keep_one_wait_until_stage_succeeds(self):
        evidence=r.e.Store(self.store.root)
        def execute(code):
            return subprocess.run([sys.executable,str(SCRIPT),'--repo',str(self.repo),'exec','--issue','TEAM-1',
                                   '--entry','qa-pipeline.sh','--','bash','-c','exit '+str(code)],
                                  capture_output=True,text=True,env=os.environ,timeout=10)
        first=None
        for _ in range(3):
            result=execute(21); self.assertEqual(result.returncode,21,result.stderr)
            records=[r.read(p) for p in (evidence.root/'waits').glob('*.json')]
            self.assertEqual(len(records),1,'ownership refusal reopened the wait')
            value=records[0]
            if first is None: first=value
            self.assertEqual((value['wait_id'],value['reached_at']), (first['wait_id'],first['reached_at']))
            self.assertEqual(value['state'],'open')
        # A provider/build error does not establish that the refused operation succeeded.
        self.assertEqual(execute(22).returncode,22)
        self.assertEqual(evidence.open_waits()[0]['wait_id'],first['wait_id'])
        self.assertEqual(execute(0).returncode,0)
        self.assertEqual(evidence.open_waits(),[])
        self.assertEqual(len(list((evidence.root/'waits').glob('*.json'))),1)

    def test_global_record_and_size_caps_evict_oldest_completed_and_protect_live_data(self):
        now=[1000.0]; evidence=r.e.Store(self.store.root,clock=lambda:now[0])
        runs=[format(n,'032x') for n in range(1,5)]
        for run in runs[:3]:
            evidence.begin_run(run,'TEAM-1',self.repo,None)
            attempt=evidence.begin_attempt(run,'TEAM-1','qa','stage',self.repo,None)
            evidence.end_attempt(run,attempt,0,None,outer=True); now[0]+=1
        live=runs[3]; evidence.begin_run(live,'TEAM-2',self.repo,None)
        wait=evidence.open_wait('TEAM-2','ownership','resource','Reconcile','workspace-owner',run=live)
        def usage():
            files=[p for p in evidence.root.rglob('*') if p.is_file() and p.name!='guard']
            return len(files),sum(p.stat().st_size for p in files)
        count,_=usage()
        with patch.object(r.e,'MAX_RECORDS',count-1): evidence._prune()
        self.assertLessEqual(usage()[0],count-1,'global record cap exceeded')
        self.assertFalse(evidence.manifest_path(runs[0]).exists(),'global cap did not evict oldest completed run')
        self.assertTrue(evidence.manifest_path(runs[1]).exists())
        _,size=usage(); completed_size=evidence.manifest_path(runs[1]).stat().st_size
        with patch.object(r.e,'MAX_BYTES',size-completed_size): evidence._prune()
        self.assertLessEqual(usage()[1],size-completed_size)
        self.assertFalse(evidence.manifest_path(runs[1]).exists())
        self.assertTrue(evidence.manifest_path(live).exists())
        self.assertEqual(evidence.open_waits()[0]['wait_id'],wait['wait_id'])
        # With no evictable space left, optional evidence skips instead of
        # deleting a live run or wait, and a mandatory receipt fails closed.
        with patch.object(r.e,'MAX_BYTES',1):
            self.assertIsNone(r.e.observe(evidence.begin_run,'9'*32,'TEAM-3',self.repo,None))
            with self.assertRaises(OSError):
                evidence.put_receipt(dict(run_id=live,provider_attempt_id='a'*32,stage_attempt_id=None,issue='TEAM-2'))
        self.assertTrue(evidence.manifest_path(live).exists())
        self.assertEqual(evidence.open_waits()[0]['wait_id'],wait['wait_id'])

    def test_undeletable_completed_run_cannot_bypass_capacity(self):
        evidence=r.e.Store(self.store.root,clock=lambda:1000)
        completed='1'*32
        evidence.begin_run(completed,'TEAM-1',self.repo,None)
        attempt=evidence.begin_attempt(completed,'TEAM-1','qa','stage',self.repo,None)
        evidence.put_receipt(dict(run_id=completed,provider_attempt_id='2'*32,stage_attempt_id=attempt,issue='TEAM-1'))
        evidence.end_attempt(completed,attempt,0,None,outer=True)
        live='3'*32; evidence.begin_run(live,'TEAM-2',self.repo,None)
        def usage():
            files=[p for p in evidence.root.rglob('*') if p.is_file() and p.name!='guard']
            return len(files),sum(p.stat().st_size for p in files)
        before=usage(); remove=r.e.shutil.rmtree
        for fault in (PermissionError('undeletable run'), OSError('I/O failure'), RuntimeError('unexpected housekeeping failure')):
            def deny_completed(path):
                if path==evidence.manifest_path(completed).parent: raise fault
                return remove(path)
            with self.subTest(fault=type(fault).__name__), patch.object(r.e,'MAX_BYTES',before[1]), \
                    patch.object(r.e.shutil,'rmtree',side_effect=deny_completed) as deletion:
                for n in range(4,8):
                    run=format(n,'032x')
                    self.assertIsNone(r.e.observe(evidence.begin_run,run,'TEAM-3',self.repo,None),'housekeeping failure bypassed capacity')
                    self.assertFalse(evidence.manifest_path(run).exists(),'housekeeping failure bypassed capacity')
                    self.assertEqual(usage(),before,'housekeeping failure grew the store')
                with self.assertRaises(r.e.CapacityError):
                    evidence.put_receipt(dict(run_id=live,provider_attempt_id='a'*32,stage_attempt_id=None,issue='TEAM-2'))
                self.assertFalse(evidence.receipt_path(live,'a'*32).exists(),'unbudgeted mandatory receipt was persisted')
                self.assertEqual(usage(),before)
                self.assertGreaterEqual(deletion.call_count,5,'undeletable completed run was not exercised')
        self.assertTrue(evidence.manifest_path(completed).exists())
        self.assertTrue(evidence.receipt_path(completed,'2'*32).exists())

    def test_atomic_staging_and_failed_cleanup_stay_within_budget(self):
        evidence=r.e.Store(self.store.root,clock=lambda:1000)
        run='1'*32; value=evidence.begin_run(run,None,'/work',None)
        path=evidence.manifest_path(run); original=path.read_bytes(); size=len(original)
        def usage():
            files=[p for p in evidence.root.rglob('*') if p.is_file() and p.name!='guard']
            return sum(p.stat().st_size for p in files),len(files)
        unlink=Path.unlink; opened=Path.open
        def deny_cleanup(temp,*args,**kwargs):
            if temp.suffix=='.tmp': raise PermissionError('injected staging unlink failure')
            return unlink(temp,*args,**kwargs)
        peaks=[]
        def deny_replace(*args):
            peaks.append(usage())
            raise OSError('injected atomic replacement failure')
        # Reviewer's one-manifest cap: even an unchanged rewrite must be
        # refused before allocating a complete second file.
        with patch.object(r.e,'MAX_BYTES',size), patch.object(r.e,'MAX_RECORDS',1), \
                patch.object(r.e.os,'replace',side_effect=deny_replace), patch.object(Path,'unlink',deny_cleanup):
            r.e.observe(evidence._write,path,value)
            self.assertTrue(all(b<=size and n<=1 for b,n in peaks+[usage()]),'atomic staging exceeded budget')
            self.assertEqual(peaks,[],'unbudgeted atomic staging was attempted')
            self.assertEqual(path.read_bytes(),original)
        for discard_refused in (False,True):
            peaks.clear()
            def deny_discard(temp,*args,**kwargs):
                if discard_refused and temp.suffix=='.tmp' and args and args[0]=='r+b':
                    raise PermissionError('injected payload discard failure')
                return opened(temp,*args,**kwargs)
            with self.subTest(discard_refused=discard_refused), patch.object(r.e,'MAX_BYTES',2*size), \
                    patch.object(r.e,'MAX_RECORDS',2), patch.object(r.e.os,'replace',side_effect=deny_replace), \
                    patch.object(Path,'unlink',deny_cleanup), patch.object(Path,'open',deny_discard):
                self.assertIsNone(r.e.observe(evidence._write,path,value))
                self.assertEqual(peaks,[(2*size,2)],'replacement failure was not injected after staging')
                self.assertLessEqual(usage()[0],2*size,'failed cleanup exceeded byte cap')
                self.assertEqual(usage()[1],2,'leftover staging file was not counted')
                leftovers=list(path.parent.glob('*.tmp'))
                self.assertEqual(len(leftovers),1)
                self.assertEqual(leftovers[0].stat().st_size,size if discard_refused else 0)
                self.assertEqual(path.read_bytes(),original,'failed atomic replace damaged committed evidence')
                r.e.observe(evidence._write,path,value)
                self.assertEqual(len(peaks),1,'undeletable staging leftover allowed another allocation')
                self.assertLessEqual(usage()[0],2*size)
                self.assertEqual(usage()[1],2)
            # A subsequent prune can reclaim the leftover even inside the
            # protected active run, restoring the original one-file cap.
            with patch.object(r.e,'MAX_BYTES',size), patch.object(r.e,'MAX_RECORDS',1):
                evidence._prune()
                self.assertEqual(usage(),(size,1),'staging leftover could not be pruned')
        legacy=path.with_name('.manifest.json.'+'a'*32)
        legacy.write_bytes(original)
        with patch.object(r.e,'MAX_BYTES',size), patch.object(r.e,'MAX_RECORDS',1):
            evidence._prune()
            self.assertEqual(usage(),(size,1),'legacy staging leftover could not be pruned')

    def test_refused_cursors_do_not_starve_open_waits(self):
        now=[1000]; evidence=r.e.Store(self.store.root,clock=lambda:now[0])
        issues=['TEAM-'+str(n) for n in range(1,5)]
        for issue in issues:
            evidence.open_wait(issue,'approval','decision','Manual merge','maintainer')
        before={p:p.read_bytes() for p in (evidence.root/'waits').glob('*.json')}
        files=[p for p in evidence.root.rglob('*') if p.is_file() and p.name!='guard']
        full_size=sum(p.stat().st_size for p in files)
        # An expired, undeletable run must not defeat the memory fallback either.
        completed='1'*32
        evidence.begin_run(completed,None,'/w',None)
        attempt=evidence.begin_attempt(completed,None,'qa','stage','/w',None)
        evidence.end_attempt(completed,attempt,0,None,outer=True)
        completed_dir=evidence.manifest_path(completed).parent
        for fault in ('budget','expired-run'):
            if fault=='budget': shutil.rmtree(completed_dir)
            else:
                evidence.begin_run(completed,None,'/w',None)
                attempt=evidence.begin_attempt(completed,None,'qa','stage','/w',None)
                evidence.end_attempt(completed,attempt,0,None,outer=True)
            for mode in ('memory','dispatcher'):
                with self.subTest(fault=fault,mode=mode):
                    current=r.e.Store(self.store.root,clock=lambda:now[0])
                    now[0]+=r.e.RETENTION_SECONDS+1000.25
                    queried=[]
                    def snapshot(repo,function,issue,**kwargs):
                        self.assertEqual(function,'bureau_wait_snapshot'); queried.append(issue)
                        if issue=='TEAM-1' or mode=='dispatcher':
                            now[0]+=kwargs['timeout']
                            raise subprocess.TimeoutExpired('slow ticket',kwargs['timeout'])
                        now[0]+=.1
                        return json.dumps(dict(identifier=issue,state=dict(id='s6',name='Merge'),
                                               labels=dict(nodes=[dict(name='needs-human')])))
                    with patch.object(r.e,'MAX_BYTES',full_size if fault=='budget' else r.e.MAX_BYTES), \
                            patch.object(r.e.shutil,'rmtree',side_effect=PermissionError('undeletable expired run')) as removal, \
                            patch.object(r,'shell',side_effect=snapshot), patch.object(r.time,'monotonic',side_effect=lambda:now[0]), \
                            patch.object(r.time,'time',return_value=now[0]), \
                            patch.object(r.e,'atomic',wraps=r.e.atomic) as atomic:
                        for turn in range(4):
                            # The shell dispatcher creates a fresh Python store
                            # each time and carries only an in-memory sweep count.
                            if mode=='dispatcher':
                                with patch.object(sys,'argv',[str(SCRIPT),'--repo',str(self.repo),'observe-open','--rotation',str(turn)]):
                                    self.assertEqual(r.main(),0)
                            else: r.sweep_open_waits(self.repo,self.store,current)
                        self.assertEqual(set(queried),set(issues),'refused cursors starved open waits across four sweeps')
                        if mode=='dispatcher': self.assertEqual(queried,issues)
                        self.assertEqual(atomic.call_count,0,'cursor persistence unexpectedly succeeded')
                        if fault=='expired-run': self.assertGreaterEqual(removal.call_count,4)
                    self.assertEqual({p:p.read_bytes() for p in before},before,'refused cursor write changed persisted waits')

    def test_slow_ticket_does_not_advance_unvisited_observation_cursors(self):
        now=[1000.0]; evidence=r.e.Store(self.store.root,clock=lambda:now[0])
        issues=['TEAM-'+str(n) for n in range(1,5)]
        for issue in issues:
            evidence.open_wait(issue,'approval','decision','Manual merge','maintainer')
        now[0]+=1
        self.assertEqual([i for i,_ in evidence.observation_batch()],issues)
        selection_untouched=all(v['last_checked_at']==1000 for v in evidence.open_waits())
        queried=[]
        def snapshot(repo, function, issue, **kwargs):
            self.assertEqual(function,'bureau_wait_snapshot')
            queried.append(issue)
            if issue=='TEAM-1':
                now[0]+=kwargs['timeout']
                raise subprocess.TimeoutExpired('slow TEAM-1',kwargs['timeout'])
            now[0]+=.1
            return json.dumps(dict(identifier=issue,state=dict(id='s6',name='Merge'),
                                   labels=dict(nodes=[dict(name='needs-human')])))
        sweeps=[]
        with patch.object(r,'shell',side_effect=snapshot), patch.object(r.time,'monotonic',side_effect=lambda:now[0]):
            for _ in range(4):
                start=len(queried)
                r.sweep_open_waits(self.repo,self.store,evidence)
                sweeps.append(queried[start:])
                if len(sweeps)==1:
                    self.assertEqual(sweeps[0],['TEAM-1'])
                    untouched=[v for v in evidence.open_waits() if v['issue']!='TEAM-1']
                    unvisited_untouched=all(v['last_checked_at']==1000 for v in untouched)
        self.assertEqual(set(queried),set(issues),'open waits starved across four sweeps')
        self.assertEqual(sweeps[1][:3],issues[1:],'slow TEAM-1 starved unvisited tickets')
        self.assertTrue(selection_untouched,'selection advanced an unvisited cursor')
        self.assertTrue(unvisited_untouched,'unvisited tickets lost next-sweep priority')
        self.assertEqual(len(evidence.open_waits()),4)

    def test_periodic_ticks_preserve_cursors_and_progress_when_writes_are_refused(self):
        issues=['TEAM-'+str(n) for n in range(1,7)]
        epoch=1730000000
        config_script=Path(os.environ.get('BUREAU_TEST_CONFIG_SCRIPT',ROOT/'templates/scripts/bureau-config.sh'))
        for cadence in (60,1800):
            self.config['agents']['poll_interval_minutes']=cadence//60
            r.save(self.repo/'.bureau.json',self.config)
            (self.store.root/'evidence/v1/waits').mkdir(parents=True,exist_ok=True)
            rotations=[]
            # Fresh real Bash dispatchers, spaced one configured poll apart.
            # Only their network/evidence sink and wall clock are replaced.
            for tick in range(len(issues)):
                result=subprocess.run(['/bin/bash','-ec','''date() { echo "$SWEEP_NOW"; }
source "$CONFIG_SCRIPT"
bureau_evidence() { printf '%s\\n' "$*"; }
bureau_sweep_waits
'''],cwd=self.repo,env={**os.environ,'CONFIG_SCRIPT':str(config_script),'SWEEP_NOW':str(epoch+tick*cadence)},
                                      capture_output=True,text=True,timeout=10)
                self.assertEqual(result.returncode,0,result.stderr)
                self.assertRegex(result.stdout,r'^observe-open --rotation [0-9]+\n$')
                rotations.append(int(result.stdout.split()[-1]))
            for fault in ('none','full','timestamp-growth'):
                refused=fault!='none'
                with self.subTest(cadence=cadence,fault=fault):
                    now=[1000]
                    evidence=r.e.Store(self.store.root,clock=lambda:now[0])
                    shutil.rmtree(evidence.root)
                    for issue in issues:
                        evidence.open_wait(issue,'approval','decision','Manual merge','maintainer')
                    before={p:p.read_bytes() for p in (evidence.root/'waits').glob('*.json')}
                    full_size=sum(p.stat().st_size for p in evidence.root.rglob('*') if p.is_file() and p.name!='guard')
                    # The old unchanged cursor fits exactly, while the next
                    # timestamp is longer and its atomic staging would fail.
                    growth_limit=full_size+max(len(value) for value in before.values())
                    queried=[]
                    def snapshot(repo,function,issue,**kwargs):
                        self.assertEqual(function,'bureau_wait_snapshot'); queried.append(issue)
                        if refused:
                            now[0]+=kwargs['timeout']
                            raise subprocess.TimeoutExpired('slow ticket',kwargs['timeout'])
                        now[0]+=.1
                        return json.dumps(dict(identifier=issue,state=dict(id='s6',name='Merge'),
                                               labels=dict(nodes=[dict(name='needs-human')])))
                    with patch.object(r.e,'MAX_BYTES',full_size if fault=='full' else growth_limit if refused else r.e.MAX_BYTES), \
                            patch.object(r,'shell',side_effect=snapshot), \
                            patch.object(r.time,'monotonic',side_effect=lambda:now[0]), \
                            patch.object(r.time,'time',side_effect=lambda:now[0]), \
                            patch.object(r.e,'atomic',wraps=r.e.atomic) as atomic:
                        for tick,turn in enumerate(rotations):
                            now[0]=float(epoch+tick*cadence)
                            start=len(queried)
                            with patch.object(sys,'argv',[str(SCRIPT),'--repo',str(self.repo),'observe-open','--rotation',str(turn)]):
                                self.assertEqual(r.main(),0)
                            if not refused and tick==1:
                                self.assertEqual(queried[start:start+2],issues[4:],
                                                 'rotation overrode writable persisted cursor order')
                        self.assertEqual(set(queried),set(issues),'periodic ticks starved open waits across N ticks')
                        self.assertEqual(atomic.call_count==0,refused,'cursor write-refusal injection did not take effect')
                    if refused:
                        self.assertEqual({p:p.read_bytes() for p in before},before,'refused cursor writes changed persisted waits')
                    else:
                        self.assertTrue(all(v['last_checked_at']>=epoch for v in evidence.open_waits()))

    def test_differently_sized_waits_do_not_starve_readable_tickets(self):
        issues=['TEAM-'+str(n) for n in range(1,7)]
        epoch=1730000000
        for mode in ('memory','periodic'):
            with self.subTest(mode=mode):
                now=[1000]
                evidence=r.e.Store(self.store.root,clock=lambda:now[0])
                shutil.rmtree(evidence.root,ignore_errors=True)
                small=evidence.open_wait(issues[0],'approval','decision','Manual merge','maintainer')
                now[0]+=1
                large=evidence.open_wait(issues[0],'needs-human:code_review','technical','Blocked','operator',
                                         generation_id='a'*64)
                # Put the larger, newer wait first in filename order, as in the
                # report. A refused write must not suppress the smaller cursor.
                for value, wait_id in ((large,'0'*32),(small,'f'*32)):
                    old=evidence.root/'waits'/(value['wait_id']+'.json')
                    value['wait_id']=wait_id
                    r.e.atomic(old.with_name(wait_id+'.json'),value)
                    old.unlink()
                for issue in issues[1:]:
                    evidence.open_wait(issue,'approval','decision','Manual merge','maintainer')
                small_size=len(r.e.encoded(small))
                small_staging=max(small_size,len(r.e.encoded(dict(small,last_checked_at=-sys.float_info.max))))
                large_size=len(r.e.encoded(large))
                self.assertLess(small_staging,large_size,'fixture must fit only the smaller cursor')
                full_size=sum(p.stat().st_size for p in evidence.root.rglob('*') if p.is_file() and p.name!='guard')
                queried=[]
                def snapshot(repo,function,issue,**kwargs):
                    self.assertEqual(function,'bureau_wait_snapshot'); queried.append(issue)
                    # Successful reads can consume the whole sweep budget too.
                    now[0]+=kwargs['timeout']
                    return json.dumps(dict(identifier=issue,state=dict(id='s6',name='Merge'),
                                           labels=dict(nodes=[dict(name='needs-human')])))
                with patch.object(r.e,'MAX_BYTES',full_size+small_staging), \
                        patch.object(r,'shell',side_effect=snapshot), \
                        patch.object(r.time,'monotonic',side_effect=lambda:now[0]), \
                        patch.object(r.time,'time',side_effect=lambda:now[0]):
                    for turn in range(len(issues)):
                        now[0]=float(epoch+turn*1800)
                        if mode=='periodic':
                            with patch.object(sys,'argv',[str(SCRIPT),'--repo',str(self.repo),'observe-open','--rotation',str(turn)]):
                                self.assertEqual(r.main(),0)
                        else:
                            r.sweep_open_waits(self.repo,self.store,evidence)
                        if turn==0:
                            saved_small=json.loads((evidence.root/'waits'/(small['wait_id']+'.json')).read_text())
                            saved_large=json.loads((evidence.root/'waits'/(large['wait_id']+'.json')).read_text())
                            self.assertEqual(saved_large,large,'larger cursor unexpectedly fit')
                    self.assertEqual(queried,issues,'differently sized waits starved readable tickets across N sweeps')
                    self.assertGreater(saved_small['last_checked_at'],small['last_checked_at'],
                                       'larger cursor refusal suppressed the writable oldest cursor')
                    self.assertEqual(len(evidence.open_waits()),len(issues)+1)

    def test_nested_execute_records_parent_and_every_stage_exit(self):
        result=self.front_exec('"$PY" "$RUNTIME" --repo "$REPO" exec --issue TEAM-1 --entry qa-pipeline.sh -- bash -c "exit 22"')
        self.assertEqual(result.returncode,22,result.stderr)
        manifests=self.manifests(); self.assertEqual(len(manifests),1)
        manifest=manifests[0]; self.assertEqual(manifest['normalized_exit'],22)
        self.assertEqual(len(manifest['attempts']),2)
        outer,inner=manifest['attempts']
        self.assertEqual(inner['stage'],'qa'); self.assertEqual(inner['phase'],'stage')
        self.assertEqual(inner['parent_attempt_id'],outer['attempt_id'])
        self.assertEqual([a['normalized_exit'] for a in manifest['attempts']],[22,22])
        self.assertEqual(inner['receipts'],[])
        self.assertEqual(manifest['final_head'],r.git(self.repo,'rev-parse','HEAD'))

    def test_ownership_failure_is_recorded_before_any_provider_call(self):
        self.store.claim('TEAM-1',self.repo,'a'*32,'app',os.getpid())
        result=self.front_exec('echo should-not-run')
        self.assertEqual(result.returncode,21,result.stderr)
        self.assertEqual(result.stdout,'')
        manifest=self.manifests()[0]
        self.assertEqual(manifest['normalized_exit'],21)
        self.assertEqual(len(manifest['attempts']),1)
        self.assertEqual(manifest['attempts'][0]['receipts'],[])
        self.assertIsNotNone(manifest['attempts'][0]['ended_at'])

    def test_spawn_refusal_records_the_actual_ownership_exit(self):
        run='e'*32
        r.save(self.store.process_path(run),dict(groups=[],interrupted=True))
        result=subprocess.run([sys.executable,str(SCRIPT),'--repo',str(self.repo),'exec','--issue','TEAM-1','--','bash','-c','echo should-not-run'],
                              env={**os.environ,'BUREAU_RUN_ID':run},capture_output=True,text=True,timeout=10)
        self.assertEqual(result.returncode,21,result.stderr); self.assertEqual(result.stdout,'')
        manifest=self.manifests()[0]
        self.assertEqual(manifest['attempts'][0]['normalized_exit'],21)
        self.assertEqual(manifest['attempts'][0]['receipts'],[])

    def test_manifests_keep_protocol_exits_and_missing_workspace_head(self):
        for code in (20,22,124,130):
            with self.subTest(code=code):
                self.store.leases.unlink(missing_ok=True)
                result=self.front_exec('exit '+str(code))
                self.assertEqual(result.returncode,code,result.stderr)
                manifest=max(self.manifests(),key=lambda v:v['started_at'])
                self.assertEqual(manifest['normalized_exit'],code)
                self.assertEqual(manifest['attempts'][0]['normalized_exit'],code)
                if code == 130:
                    self.assertEqual(self.release_run(manifest['run_id']).returncode,0)
        with patch.dict(os.environ,{},clear=False):
            os.environ.pop('BUREAU_RUN_ID',None)
            args=argparse.Namespace(workspace=str(self.repo/'absent-worker'),command=['bash','-c','exit 18'],issue='TEAM-1',entry='bureau-worker.sh')
            self.assertEqual(r.execute(self.repo,args,self.store),18)
        manifest=max(self.manifests(),key=lambda v:v['started_at'])
        self.assertIsNone(manifest['attempts'][0]['start_head'])

    def test_parallel_attempt_updates_keep_all_receipts(self):
        evidence=r.e.Store(self.store.root); run='c'*32
        head=r.git(self.repo,'rev-parse','HEAD')
        evidence.begin_run(run,'TEAM-1',self.repo,head)
        source='''import importlib.util,pathlib,sys,uuid
spec=importlib.util.spec_from_file_location('e',sys.argv[1]); e=importlib.util.module_from_spec(spec); spec.loader.exec_module(e)
s=e.Store(pathlib.Path(sys.argv[2])); run=sys.argv[3]
a=s.begin_attempt(run,'TEAM-1','code_review',sys.argv[4],'.',sys.argv[5])
s.put_receipt(dict(schema_version=1,run_id=run,issue='TEAM-1',provider_attempt_id=uuid.uuid4().hex,stage_attempt_id=a))
s.end_attempt(run,a,0,sys.argv[5])
'''
        children=[subprocess.Popen([sys.executable,'-c',source,str(ROOT/'templates/scripts/bureau-evidence.py'),str(self.store.root),run,str(i),head],
                                    stdout=subprocess.PIPE,stderr=subprocess.PIPE) for i in range(8)]
        for child in children:
            out,err=child.communicate(timeout=15); self.assertEqual(child.returncode,0,out+err)
        manifest=r.read(evidence.manifest_path(run))
        self.assertEqual(len(manifest['attempts']),8)
        self.assertEqual(len({a['attempt_id'] for a in manifest['attempts']}),8)
        for attempt in manifest['attempts']:
            self.assertEqual(len(attempt['receipts']),1)
            receipt=r.read(evidence.root/attempt['receipts'][0])
            self.assertEqual(receipt['stage_attempt_id'],attempt['attempt_id'])

    def test_gate_log_manifest_survives_worker_cleanup_and_keeps_checked_head(self):
        worker=self.repo.parent/'disposable logs'
        r.git(self.repo,'worktree','add','--detach',str(worker),'HEAD')
        evidence=r.e.Store(self.store.root); run='d'*32; head=r.git(worker,'rev-parse','HEAD')
        evidence.begin_run(run,'TEAM-1',worker,head)
        attempt=evidence.begin_attempt(run,'TEAM-1','qa','stage',worker,head)
        log=worker/'gate.log'; payload=b'x'*(1024*1024+200); log.write_bytes(payload)
        with self.assertRaisesRegex(ValueError,'checked HEAD'):
            evidence.gate(run,attempt,log,None,None,'python3 -m unittest',0)
        record=evidence.gate(run,attempt,log,head,head,'python3 -m unittest',0)
        r.git(worker,'commit','-q','--allow-empty','-m','later work')
        final=r.git(worker,'rev-parse','HEAD')
        evidence.set_pr(run,attempt,{'number':42,'url':'https://example.invalid/pull/42'})
        evidence.end_attempt(run,attempt,0,final,outer=True)
        r.git(worker,'clean','-fdx'); r.git(self.repo,'worktree','remove',str(worker))
        manifest=r.read(evidence.manifest_path(run))
        self.assertEqual(manifest['pr']['number'],42)
        self.assertNotEqual(manifest['final_head'],record['head'])
        self.assertNotIn('path',record); self.assertNotIn('command',record)
        self.assertEqual(record['sha256'],hashlib.sha256(payload).hexdigest())
        self.assertEqual(record['bytes'],len(payload))
        self.assertEqual(record['command_sha256'],hashlib.sha256(b'python3 -m unittest').hexdigest())
        self.assertEqual(list(evidence.root.rglob('*.log')),[])

    def test_retention_rotates_complete_runs_with_their_artifacts_only(self):
        now=[1000.0]; evidence=r.e.Store(self.store.root,clock=lambda:now[0])
        runs=[format(n,'032x') for n in range(1,5)]
        for run in runs[:3]:
            evidence.begin_run(run,'TEAM-1',self.repo,None)
            attempt=evidence.begin_attempt(run,'TEAM-1','qa','stage',self.repo,None)
            evidence.end_attempt(run,attempt,0,None,outer=True); now[0]+=1
        with patch.object(r.e,'MAX_RUNS',2): evidence.begin_run(runs[3],'TEAM-1',self.repo,None)
        self.assertFalse(evidence.manifest_path(runs[0]).exists())
        self.assertTrue(all(evidence.manifest_path(run).exists() for run in runs[1:]))
        cancelled='f'*32
        evidence.begin_run(cancelled,'TEAM-2',self.repo,None)
        attempt=evidence.begin_attempt(cancelled,'TEAM-2','qa','stage',self.repo,None)
        evidence.end_attempt(cancelled,attempt,130,None,outer=True)
        evidence.open_wait('TEAM-2','ownership','resource','Inspect interrupted work','workspace-owner',code=21)
        now[0]+=r.e.RETENTION_SECONDS+1
        evidence.begin_run('5'*32,'TEAM-3',self.repo,None)
        self.assertTrue(evidence.manifest_path(cancelled).exists(),'interrupted evidence must not age out')
        self.assertEqual(len(evidence.waits('TEAM-2')['tickets'][0]['open_waits']),1)

    def test_waits_reader_is_read_only_and_status_can_read_background_manifest(self):
        path=self.store.root/'evidence/v1'
        result=subprocess.run([sys.executable,str(SCRIPT),'--repo',str(self.repo),'waits','--issue','TEAM-1'],
                              capture_output=True,text=True,env=os.environ)
        self.assertEqual(result.returncode,0,result.stderr); self.assertFalse(path.exists())
        evidence=r.e.Store(self.store.root); run='b'*32
        evidence.begin_run(run,'TEAM-1',self.repo,None)
        before={str(p.relative_to(path)):p.read_bytes() for p in path.rglob('*') if p.is_file()}
        result=subprocess.run([sys.executable,str(SCRIPT),'--repo',str(self.repo),'status','--run',run],
                              capture_output=True,text=True,env=os.environ)
        self.assertEqual(json.loads(result.stdout)['record_type'],'manifest')
        self.assertEqual(before,{str(p.relative_to(path)):p.read_bytes() for p in path.rglob('*') if p.is_file()})

    def test_isolated_entrypoints_do_not_write_bytecode_into_installed_scripts(self):
        installed=self.repo.parent/'installed scripts'; installed.mkdir()
        for name in ('runtime','provider','supervision','evidence','spec'):
            shutil.copy(ROOT/'templates/scripts'/('bureau-'+name+'.py'),installed)
        before={p.name:p.read_bytes() for p in installed.iterdir()}
        for name,args in [('runtime',['--repo',str(self.repo),'status']),('supervision',['--repo',str(self.repo),'status']),
                          ('provider',['--stage','implement','--config',str(self.repo/'.bureau.json'),'--describe'])]:
            result=subprocess.run([sys.executable,'-I',str(installed/('bureau-'+name+'.py')),*args],
                                  capture_output=True,text=True,env=os.environ)
            self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        self.assertEqual(sorted(p.name for p in installed.iterdir()),sorted(before))
        self.assertTrue(all((installed/name).read_bytes()==data for name,data in before.items()))

if __name__=='__main__': unittest.main()
