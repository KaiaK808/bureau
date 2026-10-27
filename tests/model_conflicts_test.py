"""Opt-in semantic conflict repair through real Git and the real provider adapter."""
import importlib.util
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = Path(os.environ.get('BUREAU_MODEL_CONFLICT_SCRIPTS', ROOT/'templates/scripts'))

PROVIDER = r'''
import json, os, pathlib, re, subprocess, sys, time
root = pathlib.Path(ROOT)
if sys.argv[1:3] == ['login','status']:
    if json.loads((root/'control').read_text()).get('mode')=='unauth': sys.exit(1)
    print('Logged in'); sys.exit(0)
prompt = sys.stdin.read()
c = json.loads((root/'control').read_text())
with (root/'calls').open('a') as out: out.write('provider\n')
assert not any(k in os.environ for k in ('API_KEY','LINEAR_API_KEY','GH_TOKEN'))
with (root/'pids').open('a') as out: out.write(str(os.getpid())+'\n')
if c.get('mode') in ('timeout','silence'): time.sleep(10)
if c.get('mode') == 'error': sys.exit(3)
paths = json.loads(re.search(r'source conflicts: (\[[^\n]+\])',prompt)[1])
for name in paths:
    p=pathlib.Path(name)
    if c.get('mode') != 'false':
        p.write_text('bad\n' if c.get('mode') == 'red' else '<<<<<<< leftover\n' if c.get('mode') == 'markers' else 'resolved\n')
    if c.get('mode') == 'chmod': p.chmod(0o755)
if c.get('mode') == 'outside': pathlib.Path('unrelated.txt').write_text('unauthorized\n')
if c.get('mode') == 'ignored': pathlib.Path('ignored.txt').write_text('unauthorized\n')
if c.get('mode') == 'untracked': pathlib.Path('new.txt').write_text('unauthorized\n')
if c.get('mode') == 'metadata':
    path=subprocess.check_output(['git','rev-parse','--git-path','MERGE_MSG'],text=True).strip(); pathlib.Path(path).write_text('unauthorized message')
if c.get('mode') == 'index_flags': subprocess.run(['git','update-index','--assume-unchanged','unrelated.txt'],check=True)
if c.get('mode') == 'index': subprocess.run(['git','add','--',*paths],check=True)
if c.get('withdraw'): (root/'state').write_text(c['withdraw'])
pathlib.Path(sys.argv[sys.argv.index('-o')+1]).write_text('Resolved all conflicts successfully.')
print(json.dumps({'type':'turn.completed','usage':{'input_tokens':1,'output_tokens':1}}))
'''

SERVICE = r'''
import json, pathlib, sys
root=pathlib.Path(ROOT)
if sys.argv[1] == 'snapshot':
    if json.loads((root/'control').read_text()).get('fail_snapshot'): sys.exit(27)
    print(json.dumps(dict(identifier='TEAM-1',state=dict(id=(root/'state').read_text()),labels=dict(nodes=[]))))
else:
    with (root/'events').open('a') as f: f.write(json.dumps(sys.argv[1:])+'\n')
    if sys.argv[1]=='label' and json.loads((root/'control').read_text()).get('label_down'): sys.exit(27)
'''

CHECK = r'''
import json, os, pathlib, sys
root=pathlib.Path(ROOT)
assert not any(k in os.environ for k in ('API_KEY','LINEAR_API_KEY','GH_TOKEN'))
with (root/'calls').open('a') as out: out.write('check\n')
with (root/'pids').open('a') as out: out.write(str(os.getpid())+'\n')
for name in ('source.txt','second.txt'):
    assert pathlib.Path(name).read_text() == 'resolved\n', name
c=json.loads((root/'control').read_text())
if c.get('withdraw_check'): (root/'state').write_text(c['withdraw_check'])
if c.get('check_edit'): pathlib.Path('source.txt').write_text('rewritten by check\n')
'''


class ModelConflictTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='bureau-1750-model-')
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name).resolve(); self.repo=self.root/'repo'; self.repo.mkdir()
        self.scripts=self.root/'scripts'; shutil.copytree(SCRIPTS,self.scripts)
        self.bin=self.root/'bin'; self.bin.mkdir()
        self.control={}; self.save_control(); (self.root/'state').write_text('build')
        (self.root/'latch').mkdir()
        for name, source in [('codex',PROVIDER),('service',SERVICE)]:
            path=self.bin/name; path.write_text('#!'+sys.executable+'\nROOT='+repr(str(self.root))+'\n'+source); path.chmod(0o755)
        (self.root/'check.py').write_text('ROOT='+repr(str(self.root))+'\n'+CHECK)
        with (self.scripts/'bureau-config.sh').open('a') as out:
            out.write('\nbureau_issue_snapshot() { "$FAKE_SERVICE" snapshot; }\n'
                      'add_issue_label() { "$FAKE_SERVICE" label "$@"; }\n'
                      'post_comment() { "$FAKE_SERVICE" comment "$@"; }\n'
                      '_throttle_should_suppress() { return 1; }\n_throttle_record() { :; }\n')
        self.env={**os.environ,'PATH':str(self.bin)+os.pathsep+os.environ['PATH'],
                  'BUREAU_CONFIG':str(self.root/'config.json'),'FAKE_SERVICE':str(self.bin/'service'),
                  'BUREAU_TICKET_CONTROL_DIR':str(self.root/'latch'),'BUREAU_ENV_FILE':'/dev/null',
                  'PYTHONDONTWRITEBYTECODE':'1','GIT_CONFIG_NOSYSTEM':'1','GIT_CONFIG_GLOBAL':'/dev/null'}
        self.settings={'version':2,'repo':{'test_command':shlex.join([sys.executable,str(self.root/'check.py')])},
                       'agents':{'runner':'codex','conflict_resolver':{'enabled':True,'max_attempts':1,'timeout_seconds':5}},
                       'linear':{'teams':[{'states':{'build':'build','build_review':'review','done':'done','cancelled':'cancelled'}}]}}
        self.git('init','-q','-b','main'); self.git('config','user.name','Bureau Test'); self.git('config','user.email','test@bureau')
        self.git('config','maintenance.auto','false'); self.git('config','gc.auto','0')
        for name,text in [('source.txt','base\n'),('second.txt','base\n'),('unrelated.txt','preserve\n'),('generated.txt','base\n'),('.gitignore','ignored.txt\n')]:
            (self.repo/name).write_text(text)
        self.commit('foundation'); (self.repo/'ignored.txt').write_text('private\n')
        self.git('branch','feature'); self.git('init','-q','--bare',str(self.root/'origin.git'))
        self.git('--git-dir',str(self.root/'origin.git'),'config','maintenance.auto','false')
        self.git('--git-dir',str(self.root/'origin.git'),'config','gc.auto','0')
        self.git('remote','add','origin',str(self.root/'origin.git'))

    def git(self,*args,check=True):
        r=subprocess.run(['git','-C',str(self.repo),*args],env=self.env,capture_output=True,text=True,timeout=15)
        if check and r.returncode: self.fail(r.stderr)
        return r.stdout.strip()

    def commit(self,name,bureau=True):
        self.git('add','-A'); self.git('commit','-qm',name+('\n\nBureau-Generated: true' if bureau else ''))

    def save_control(self): (self.root/'control').write_text(json.dumps(self.control))

    def diverge(self,multi=False,local_human=False,remote_human=False,generated=False):
        self.git('checkout','-q','feature'); (self.repo/'source.txt').write_text('ours\n')
        if not multi: (self.repo/'second.txt').write_text('ours\n')
        self.commit('first source',not remote_human)
        if multi:
            (self.repo/'second.txt').write_text('ours\n'); self.commit('second source')
        if generated: (self.repo/'generated.txt').write_text('ours stale\n'); self.commit('feature output')
        self.git('push','-q','origin','feature')
        if local_human:
            (self.repo/'unrelated.txt').write_text('human local\n'); self.commit('human local',False)
        self.head=self.git('rev-parse','HEAD'); self.remote=self.git('rev-parse','origin/feature')
        self.git('checkout','-q','main')
        (self.repo/'source.txt').write_text('theirs\n'); (self.repo/'second.txt').write_text('theirs\n')
        if generated: (self.repo/'generated.txt').write_text('theirs stale\n')
        self.commit('main source',False); self.git('push','-q','origin','main')
        self.base=self.git('rev-parse','HEAD'); self.git('checkout','-q','feature')

    def invoke(self,operation='merge'):
        (self.root/'config.json').write_text(json.dumps(self.settings)); self.save_control()
        if operation=='merge':
            command=['/bin/bash','-e','-o','pipefail','-c','source "$1/bureau-config.sh"; rc=0; '
                     'merge_origin_main_or_abort TEAM-1 Test "$2" || rc=$?; exit "$rc"',
                     'model-fixture',str(self.scripts),self.base]
        else:
            command=['/bin/bash','-e','-o','pipefail','-c','source "$1/bureau-config.sh"; rc=0; '
                     '( bureau_exec_runtime python3 -I "$1/bureau-conflicts.py" --repo "$PWD" --config "$BUREAU_CONFIG" '
                     '--base "$2" --issue TEAM-1 --operation rebase ) || rc=$?; '
                     'bureau_ticket_checkpoint TEAM-1; case "$rc" in 0|10|16|21|25|26|27|130) exit "$rc";; esac; '
                     'mark_needs_human TEAM-1 rebase 17 || true; exit 17',
                     'model-fixture',str(self.scripts),self.base]
        self.result=subprocess.run(command,cwd=self.repo,env=self.env,capture_output=True,text=True,timeout=35)
        if (self.root/'pids').exists():
            for raw in (self.root/'pids').read_text().splitlines():
                with self.assertRaises(ProcessLookupError, msg='test provider/check survived: '+raw): os.kill(int(raw),0)
        return self.result.returncode

    def records(self):
        paths=list((self.repo/'.git/bureau/conflict-resolver').glob('*.json'))
        return json.loads(paths[0].read_text())['attempts'] if paths else []

    def calls(self): return (self.root/'calls').read_text().splitlines() if (self.root/'calls').exists() else []

    def refused(self,operation='merge',code=17):
        before={p:p.read_bytes() for p in self.repo.iterdir() if p.is_file()}
        index=(self.repo/'.git/index').read_bytes()
        self.assertEqual(self.invoke(operation),code,self.result.stdout+self.result.stderr)
        self.assertEqual((self.repo/'.git/index').read_bytes(),index)
        self.assertEqual(self.git('rev-parse','HEAD'),self.head)
        self.assertEqual(self.git('symbolic-ref','--short','HEAD'),'feature')
        self.assertEqual(self.git('ls-files','-u'),'')
        self.assertEqual(self.git('status','--porcelain'),'')
        self.assertEqual({p:p.read_bytes() for p in self.repo.iterdir() if p.is_file()},before)
        self.assertEqual(self.git('rev-parse','origin/feature'),self.remote)
        if code==17: self.assertIn('label', (self.root/'events').read_text())

    def test_merge_checks_actual_tree_and_records_evidence(self):
        self.diverge(); self.assertEqual(self.invoke(),0,self.result.stdout+self.result.stderr)
        self.assertEqual(self.calls(),['provider','check'])
        self.assertEqual(self.git('rev-list','--parents','-1','HEAD').split()[1:],[self.head,self.base])
        self.assertEqual(self.records()[0]['check_outcome'],'passed')
        receipts=[json.loads(p.read_text()) for p in (self.repo/'.git/bureau/evidence/v1/runs').glob('*/receipts/*.json')]
        self.assertTrue(any(v.get('conflict_resolution',{}).get('operation')=='merge' for v in receipts))
        self.assertEqual(self.git('rev-parse','origin/feature'),self.remote,'merge hook must not publish')

    def test_multicommit_rebase_checks_only_after_full_rebase(self):
        self.diverge(multi=True); self.settings['agents']['conflict_resolver']['max_attempts']=2
        self.assertEqual(self.invoke('rebase'),0,self.result.stdout+self.result.stderr)
        self.assertEqual(self.calls(),['provider','provider','check'])
        self.assertEqual([v['attempt'] for v in self.records()],[1,2])
        self.assertEqual({v['base'] for v in self.records()},{self.base})
        self.assertEqual(self.git('merge-base','HEAD',self.base),self.base)

    def test_later_rebase_conflict_exhaustion_rolls_back_entire_rebase(self):
        self.diverge(multi=True); self.refused('rebase'); self.assertEqual(self.calls(),['provider'])
        self.assertEqual(len(self.records()),1)

    def test_red_check_rolls_back_and_is_human_17(self):
        self.diverge(); self.control['mode']='red'; self.refused()
        self.assertEqual(self.calls(),['provider','check']); self.assertEqual(self.records()[0]['check_outcome'],'failed')

    def test_provider_error_rolls_back_and_consumes_attempt(self):
        self.diverge(); self.control['mode']='error'; self.refused(); self.assertEqual(len(self.records()),1)

    def test_provider_timeout_rolls_back_and_consumes_attempt(self):
        self.diverge(); self.control['mode']='timeout'; self.settings['agents']['conflict_resolver']['timeout_seconds']=.3
        self.refused(); self.assertEqual(len(self.records()),1)

    def test_false_resolved_claim_is_rejected(self):
        self.diverge(); self.control['mode']='false'; self.refused(); self.assertEqual(len(self.records()),1)

    def test_surviving_markers_are_rejected(self):
        self.diverge(); self.control['mode']='markers'; self.refused(); self.assertEqual(len(self.records()),1)

    def test_out_of_scope_tracked_edit_is_rejected(self):
        self.diverge(); self.control['mode']='outside'; self.refused(); self.assertEqual(len(self.records()),1)

    def test_out_of_scope_ignored_edit_is_rejected(self):
        self.diverge(); self.control['mode']='ignored'; self.refused(); self.assertEqual(len(self.records()),1)

    def test_out_of_scope_untracked_edit_is_rejected(self):
        self.diverge(); self.control['mode']='untracked'; self.refused(); self.assertEqual(len(self.records()),1)

    def test_provider_staging_is_rejected(self):
        self.diverge(); self.control['mode']='index'; self.refused(); self.assertEqual(len(self.records()),1)

    def test_provider_cannot_rewrite_operation_metadata(self):
        self.diverge(); self.control['mode']='metadata'; self.refused(); self.assertEqual(len(self.records()),1)

    def test_provider_cannot_hide_index_flag_changes(self):
        self.diverge(); self.control['mode']='index_flags'; self.refused(); self.assertEqual(len(self.records()),1)

    def test_provider_mode_change_is_rejected(self):
        self.diverge(); self.control['mode']='chmod'; self.refused(); self.assertEqual(len(self.records()),1)

    def test_validation_cannot_rewrite_checked_source(self):
        self.diverge(); self.control['check_edit']=True; self.refused(); self.assertEqual(len(self.records()),1)

    def test_local_human_commit_is_refused_even_with_bureau_remote(self):
        self.diverge(local_human=True); self.refused(); self.assertEqual(self.calls(),[])
        # Actual local tip/base arguments must inspect this human commit.
        script='source "$1/bureau-config.sh"; branch_is_bureau_only feature HEAD "$2"'
        r=subprocess.run(['/bin/bash','-c',script,'fixture',str(self.scripts),self.base],cwd=self.repo,env=self.env)
        self.assertEqual(r.returncode,1)

    def test_remote_human_commit_is_refused(self):
        self.diverge(remote_human=True); self.refused(); self.assertEqual(self.calls(),[])
        # Rebase's conflict refusal is always a human 17, even with opt-in off.
        self.settings['agents']['conflict_resolver']['enabled']=False
        self.refused('rebase')

    def test_missing_or_comment_only_check_declines_without_spending(self):
        self.diverge()
        for command in (None,'  \n# no check\n'):
            self.settings['repo']['test_command']=command; self.refused(); self.assertEqual(self.calls(),[])
        self.settings['repo']['test_command']='false'; self.refused(); self.assertEqual(len(self.records()),1)

    def test_default_disabled_and_invalid_attempt_limits_fail_closed(self):
        self.diverge(); self.settings['agents'].pop('conflict_resolver'); self.refused()
        for limit in (0,3,True,'2'):
            self.settings['agents']['conflict_resolver']={'enabled':True,'max_attempts':limit}; self.refused()
        self.assertEqual(self.calls(),[])
        self.settings['agents']['conflict_resolver']={'enabled':True}; self.assertEqual(self.invoke(),0)

    def test_budget_survives_head_restart_operation_and_evidence_pruning(self):
        self.diverge(); self.control['mode']='red'; self.refused()
        shutil.rmtree(self.repo/'.git/bureau/evidence'); self.git('commit','--allow-empty','-qm','new head\n\nBureau-Generated: true')
        self.head=self.git('rev-parse','HEAD'); self.control['mode']='valid'; self.refused('rebase')
        self.assertEqual(self.calls(),['provider','check']); self.assertEqual(len(self.records()),1)

    def test_budget_can_allow_two_distinct_operations_but_never_three(self):
        self.diverge(); self.settings['agents']['conflict_resolver']['max_attempts']=2
        self.control['mode']='error'; self.refused(); self.refused('rebase'); self.refused()
        self.assertEqual(self.calls(),['provider','provider']); self.assertEqual(len(self.records()),2)

    def test_reservation_persistence_failure_declines_before_provider(self):
        self.diverge(); path=self.repo/'.git/bureau/conflict-resolver'; path.parent.mkdir(); path.write_text('blocked')
        self.refused(); self.assertEqual(self.calls(),[])
        path.unlink(); self.assertEqual(self.invoke(),0)

    def test_cancellation_after_provider_wins_and_rolls_back(self):
        self.diverge(); self.control['withdraw']='cancelled'; self.refused(code=26); self.assertEqual(len(self.records()),1)
        self.assertEqual(self.calls(),['provider'])

    def test_done_after_provider_wins_and_rolls_back(self):
        self.diverge(); self.control['withdraw']='done'; self.refused(code=0); self.assertEqual(len(self.records()),1)
        self.assertEqual(self.calls(),['provider'])

    def test_paused_after_check_wins_and_rolls_back(self):
        self.diverge(); self.control['withdraw_check']='paused'; self.refused(code=25); self.assertEqual(len(self.records()),1)
        self.assertEqual(self.calls(),['provider','check'])

    def test_generated_outputs_follow_model_sources(self):
        self.diverge(generated=True)
        command=[sys.executable,'-c',"from pathlib import Path; assert Path('source.txt').read_text() == 'resolved\\n'; Path('generated.txt').write_text('fresh\\n')"]
        self.settings['repo']['conflict_resolution']={'generated':[{'globs':['generated.txt'],'command':command}]}
        self.assertEqual(self.invoke(),0,self.result.stdout+self.result.stderr)
        self.assertEqual((self.repo/'generated.txt').read_text(),'fresh\n')
        self.assertEqual(self.calls(),['provider','check'])

    def test_recreated_worktree_does_not_replenish_allowance(self):
        self.diverge(); self.control['mode']='red'; self.refused()
        original=self.repo; self.git('checkout','-q','main')
        worker=self.root/'replacement'; self.git('worktree','add','-q',str(worker),'feature')
        self.repo=worker; self.control['mode']='valid'
        self.assertEqual(self.invoke(),17,self.result.stdout+self.result.stderr)
        self.assertEqual(self.git('rev-parse','HEAD'),self.head)
        self.assertEqual(self.calls(),['provider','check'])
        self.repo=original; self.assertEqual(len(self.records()),1)

    def test_external_check_background_writer_is_reaped(self):
        self.diverge()
        command="import subprocess,sys,pathlib; child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)']); pathlib.Path("+repr(str(self.root/'writer.pid'))+").write_text(str(child.pid))"
        self.settings['repo']['test_command'] += ' && ' + shlex.join([sys.executable,'-c',command])
        self.assertEqual(self.invoke(),0,self.result.stdout+self.result.stderr)
        pid=int((self.root/'writer.pid').read_text())
        spec=importlib.util.spec_from_file_location('check_writer_activity',SCRIPTS/'bureau-activity.py')
        activity=importlib.util.module_from_spec(spec); spec.loader.exec_module(activity)
        row=activity.ProcessTable().snapshot().get(pid)
        self.assertTrue(row is None or row[2],'external-check background writer survived')

    def test_concurrent_reservations_share_one_durable_slot(self):
        self.diverge()
        code="""import importlib.util,pathlib,sys
spec=importlib.util.spec_from_file_location('budget_runtime',sys.argv[1]); m=importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
b=m.AttemptBudget(pathlib.Path(sys.argv[2]),'TEAM-1',sys.argv[3])
try: b.reserve(1,sys.argv[4],'merge',dict(runner='codex'),['source.txt'])
except m.Declined: sys.exit(3)
"""
        root=self.repo/'.git/bureau'
        children=[subprocess.Popen([sys.executable,'-I','-c',code,str(self.scripts/'bureau-conflicts.py'),str(root),self.base,self.head],
                   stdout=subprocess.PIPE,stderr=subprocess.PIPE,env=self.env) for _ in range(4)]
        codes=[]
        for child in children:
            out,err=child.communicate(timeout=10); codes.append(child.returncode)
        self.assertEqual(sorted(codes),[0,3,3,3])
        self.assertEqual(len(self.records()),1)

    def test_unreadable_ledger_never_replenishes_allowance(self):
        self.diverge(); self.control['mode']='red'; self.refused()
        path=next((self.repo/'.git/bureau/conflict-resolver').glob('*.json')); path.write_text('{broken')
        self.control['mode']='valid'; self.refused('rebase')
        self.assertEqual(path.read_text(),'{broken')
        self.assertEqual(self.calls(),['provider','check'])

    def test_provider_authentication_retains_precondition_16(self):
        self.diverge(); self.control['mode']='unauth'; self.refused(code=16)
        self.assertEqual(self.calls(),[]); self.assertEqual(len(self.records()),1)

    def test_strict_ticket_read_failure_retains_27_without_spending(self):
        self.diverge(); self.control['fail_snapshot']=True; self.refused(code=27)
        self.assertEqual(self.calls(),[]); self.assertEqual(self.records(),[])

    def test_label_outage_keeps_conflict_17_and_durable_human_hold(self):
        self.diverge(); self.control.update(mode='error',label_down=True); self.refused()
        self.assertTrue((self.repo/'.git/bureau/needs-human-held/TEAM-1').is_file())
        self.assertEqual(len(self.records()),1)

    def test_silence_retains_25_and_receipt_timeout_kind(self):
        self.diverge(); self.control['mode']='silence'
        self.settings['agents']['conflict_resolver'].update(silence_timeout_seconds=.3,silence_child_timeout_seconds=.3)
        self.refused(code=25)
        receipts=[json.loads(p.read_text()) for p in (self.repo/'.git/bureau/evidence/v1/runs').glob('*/receipts/*.json')]
        self.assertTrue(any(v.get('timeout_kind')=='silence' and v['normalized_exit']==25 for v in receipts))

if __name__=='__main__': unittest.main()
