import importlib.util
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
import uuid

ROOT=Path(__file__).resolve().parents[1]
SCRIPT=ROOT/'templates/scripts/bureau-provider.py'
spec=importlib.util.spec_from_file_location('provider',SCRIPT)
p=importlib.util.module_from_spec(spec); spec.loader.exec_module(p)


class ProviderTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='bureau provider '); self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name); self.bin=self.root/'bin'; self.bin.mkdir()
        self.config=self.root/'config.json'; self.config.write_text('{"agents":{"runner":"codex"}}')
        self.prompt=self.root/'prompt.txt'; self.prompt.write_text('Review this fixture')
        fake='#!'+sys.executable+'''
import json, os, pathlib, signal, sys, time
name=pathlib.Path(sys.argv[0]).name
root=pathlib.Path(os.environ['FAKE_ROOT'])
if sys.argv[1] in ('auth','login'):
    if os.environ.get('AUTH_FAIL'): sys.exit(1)
    print(json.dumps({'loggedIn':True})); sys.exit(0)
# A Claude CLI before --session-id (OLD_CLI): its help does not list the flag and it rejects
# the flag as an unknown option, as commander does. HELP_FAIL: the help itself fails.
if sys.argv[1]=='--help':
    with (root/'help-calls').open('a') as calls: calls.write('help\\n')
    (root/'help-env.json').write_text(json.dumps(sorted(os.environ)))
    if os.environ.get('HELP_FAIL'): sys.exit(2)
    print('Usage: claude [options] [command] [prompt]')
    print('  -p, --print   Print response and exit')
    if not os.environ.get('OLD_CLI'): print('  --session-id <uuid>   Use a specific session ID for the conversation')
    sys.exit(0)
if os.environ.get('OLD_CLI') and '--session-id' in sys.argv:
    print("error: unknown option '--session-id'",file=sys.stderr); sys.exit(1)
(root/'argv.json').write_text(json.dumps(sys.argv[1:]))
(root/'stdin.txt').write_text(sys.stdin.read())
if os.environ.get('FORK_IGNORE_TERM'):
    descendant = os.fork()
    if descendant == 0:
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        (root/'heartbeat').write_text(str(time.monotonic_ns()))
        (root/'descendant.tmp').write_text(json.dumps({'pid':os.getpid(),'group':os.getpgrp()}))
        (root/'descendant.tmp').replace(root/'descendant.json')
        while True:
            (root/'heartbeat').write_text(str(time.monotonic_ns()))
            time.sleep(.05)
    while not (root/'descendant.json').exists(): time.sleep(.01)
# What the CLI itself records while it works: Claude its transcript, named by --session-id,
# under TRANSCRIPT_DIR (the test computes that directory); Codex its thread.started event.
if name=='claude' and os.environ.get('TRANSCRIPT_DIR') and '--session-id' in sys.argv:
    folder=pathlib.Path(os.environ['TRANSCRIPT_DIR']); folder.mkdir(parents=True,exist_ok=True)
    (folder/(sys.argv[sys.argv.index('--session-id')+1]+'.jsonl')).write_text('{"type":"user"}\\n')
if name=='codex' and os.environ.get('THREAD'):
    print(json.dumps({'type':'thread.started','thread_id':os.environ['THREAD']}),flush=True)
(root/'ready').touch()
if os.environ.get('IGNORE_TERM'): signal.signal(signal.SIGTERM, signal.SIG_IGN)
if os.environ.get('SLEEP'): time.sleep(float(os.environ['SLEEP']))
if os.environ.get('FAIL'):
    print(os.environ['FAIL'],file=sys.stderr); sys.exit(1)
final=os.environ.get('FINAL','final response')
if name=='codex':
    output=sys.argv[sys.argv.index('-o')+1]
    if not os.environ.get('EMPTY'): pathlib.Path(output).write_text(final)
    print(json.dumps({'type':'turn.completed','usage':{'input_tokens':12,'output_tokens':3}}))
    print('diagnostic APPROVE text should never become the result')
elif os.environ.get('RAW'):
    print(os.environ['RAW'])
else:
    print(json.dumps({'result':final,'usage':{'input_tokens':12,'output_tokens':3},'total_cost_usd':0.01}))
'''
        for name in ('claude','codex'):
            path=self.bin/name; path.write_text(fake); path.chmod(0o755)
        self.env={**os.environ,'PATH':str(self.bin)+os.pathsep+os.environ['PATH'],'FAKE_ROOT':str(self.root),
                  'BUREAU_PROVIDER_LOG_DIR':str(self.root/'evidence')}
        for key in list(self.env):
            if key.startswith(('BUREAU_RUNNER_','BUREAU_MODEL_','BUREAU_CODEX_MODEL_','BUREAU_STAGE_TIMEOUT')): self.env.pop(key)
        # The provider looks for the CLI's own records here, never in the real home.
        self.env.update(CLAUDE_CONFIG_DIR=str(self.root/'claude config'),CODEX_HOME=str(self.root/'codex home'))

    def command(self,*extra):
        return [sys.executable,str(SCRIPT),'--stage','implement','--config',str(self.config),
                '--repo',str(getattr(self,'repo',self.root)),'--prompt-file',str(self.prompt),*extra]

    def run_provider(self,*extra,code=0,**env):
        result=subprocess.run(self.command(*extra),capture_output=True,text=True,env={**self.env,**env},timeout=12)
        self.assertEqual(result.returncode,code,result.stdout+result.stderr)
        return result

    def test_codex_stdin_large_prompt_and_exact_argv(self):
        self.prompt.write_text('quoted `$(no-command)` '+('large '*250000))
        result=self.run_provider(BUREAU_CODEX_MODEL_IMPLEMENT='model with spaces;echo nope')
        args=json.loads((self.root/'argv.json').read_text())
        self.assertEqual(args[args.index('--model')+1],'model with spaces;echo nope')
        self.assertIn(self.prompt.read_text(),(self.root/'stdin.txt').read_text())
        self.assertEqual(args[-1],'-'); self.assertNotIn('--append-system-prompt',args)
        self.assertEqual(result.stdout.strip(),'final response')
        self.assertNotIn('APPROVE',result.stdout)

    def test_invalid_provider_configuration_is_classified(self):
        for config in ({'agents':{'providers':[]}}, {'agents':{'runner':'codex','model':42}},
                       {'agents':{'runner':'codex','implement':{'model':[]},'providers':{'codex':{'model':'valid-model'}}}},
                       {'agents':{'runner':'codex','implement':{'timeout_seconds':'NaN'}}}):
            self.config.write_text(json.dumps(config))
            self.run_provider(code=22)

    def test_claude_envelope_and_cost_tracking(self):
        self.config.write_text('{"agents":{"runner":"claude"},"session":{"cost_tracking":true}}')
        result=self.run_provider(FINAL='```json\n{"status":"COMPLETE"}\n```')
        value=json.loads(result.stdout)
        self.assertEqual(value['provider'],'claude'); self.assertEqual(value['usage']['input_tokens'],12)
        self.assertIn('COMPLETE',value['result'])

    def test_schema_validates_actual_final_and_rejects_invalid(self):
        schema=ROOT/'templates/scripts/bureau-qa.schema.json'
        value={'status':'GREEN','tests_added':0,'tests_failing':0,'coverage_notes':'ok'}
        result=self.run_provider('--schema',str(schema),FINAL=json.dumps(value))
        self.assertEqual(json.loads(result.stdout),value)
        for bad in ('not json','{"status":"GREEN"}',json.dumps({**value,'status':'APPROVE'})):
            result=self.run_provider('--schema',str(schema),FINAL=bad,code=22)
            self.assertEqual(result.stdout,'')

    def test_auth_provider_errors_environment_and_empty_output(self):
        self.run_provider(AUTH_FAIL='1',code=16)
        for message,code in [('quota exceeded',23),('Permission denied by sandbox',24),('backend failed',22),('Not logged in',16)]:
            with self.subTest(message=message): self.run_provider(FAIL=message,code=code)
        self.run_provider(EMPTY='1',code=22)

    def test_timeout_and_cancellation_kill_process_group(self):
        self.run_provider(SLEEP='10',BUREAU_STAGE_TIMEOUT='0.1',code=124)
        (self.root/'ready').unlink()
        proc=subprocess.Popen(self.command(),env={**self.env,'SLEEP':'10'},stdout=subprocess.PIPE,stderr=subprocess.PIPE)
        deadline=time.monotonic()+4
        while not (self.root/'ready').exists() and time.monotonic()<deadline: time.sleep(.02)
        proc.terminate(); stdout,stderr=proc.communicate(timeout=8)
        self.assertEqual(proc.returncode,130,stderr.decode()); self.assertEqual(stdout,b'')

    def test_timeout_and_cancel_stop_descendant_after_leader_exits(self):
        for mode, code in (('timeout', 124), ('term', 130), ('interrupt', 130), ('runtime-timeout', 124)):
            with self.subTest(mode=mode):
                pid_file=self.root/'descendant.json'; pid_file.unlink(missing_ok=True)
                env={**self.env,'FORK_IGNORE_TERM':'1','SLEEP':'30',
                     'BUREAU_STAGE_TIMEOUT':'1' if mode.endswith('timeout') else '30'}
                command=self.command()
                if mode == 'runtime-timeout':
                    subprocess.run(['git','init','-q',str(self.root)],check=True)
                    env['BUREAU_CONFIG']=str(self.config)
                    command=[sys.executable,str(ROOT/'templates/scripts/bureau-runtime.py'),
                             '--repo',str(self.root),'exec','--issue','TEAM-123','--',*command]
                proc=subprocess.Popen(command,env=env,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
                try:
                    deadline=time.monotonic()+4
                    while not pid_file.exists() and time.monotonic()<deadline: time.sleep(.02)
                    self.assertTrue(pid_file.exists(),'fake provider descendant did not start')
                    descendant=json.loads(pid_file.read_text())
                    if not mode.endswith('timeout'): proc.send_signal(signal.SIGTERM if mode == 'term' else signal.SIGINT)
                    stdout,stderr=proc.communicate(timeout=8)
                    self.assertEqual(proc.returncode,code,stderr.decode())
                    self.assertEqual(stdout,b'')
                    heartbeat=(self.root/'heartbeat').read_text()
                    if mode == 'runtime-timeout':
                        self.assertEqual(json.loads((self.root/'.git/bureau/leases.json').read_text()),{})
                    # A killed orphan can briefly be a zombie on CI. It cannot
                    # write; a live descendant can outlast the released lease.
                    deadline=time.monotonic()+2
                    while True:
                        status=subprocess.run(['ps','-p',str(descendant['pid']),'-o','stat='],capture_output=True,text=True)
                        self.assertIn(status.returncode,(0,1),status.stderr)
                        self.assertFalse(status.stderr.strip(),status.stderr)
                        active=bool(status.stdout.strip()) and not status.stdout.strip().startswith('Z')
                        if not active or time.monotonic() >= deadline: break
                        time.sleep(.02)
                    self.assertFalse(active,'provider descendant survived the bounded invocation')
                    time.sleep(.1)
                    self.assertEqual((self.root/'heartbeat').read_text(),heartbeat)
                finally:
                    if pid_file.exists():
                        descendant=json.loads(pid_file.read_text())
                        try:
                            if os.getpgid(descendant['pid']) == descendant['group']:
                                os.killpg(descendant['group'],signal.SIGKILL)
                        except ProcessLookupError: pass
                    if proc.poll() is None: proc.kill()
                    proc.communicate()

    def test_runner_and_model_resolution_is_provider_specific(self):
        config={'version':2,'agents':{'model':'claude-default','implement':{'runner':'codex','model':'codex-stage'},
                          'providers':{'codex':{'model':'codex-default','reasoning_effort':'high'}}}}
        self.assertEqual(p.configuration('implement',config,{})['model'],'codex-stage')
        self.assertEqual(p.configuration('implement',config,{'BUREAU_CODEX_MODEL_IMPLEMENT':'override'})['model'],'override')
        self.assertEqual(p.configuration('qa',config,{'BUREAU_RUNNER_QA':'codex'})['model'],'codex-default')
        self.assertEqual(p.configuration('qa',config,{})['model'],'claude-default')
        self.assertEqual(p.configuration('spec_review',{'agents':{'runner':'codex'}},{})['sandbox'],'workspace-write')
        with self.assertRaises(ValueError): p.configuration('qa',config,{'BUREAU_RUNNER_QA':'typo'})
        with self.assertRaises(ValueError): p.configuration('qa',config,{'BUREAU_SANDBOX_QA':'danger-full-access'})

    def test_legacy_codex_model_selection_reaches_the_actual_cli(self):
        # Existing repos used these generic settings for Claude. Switching the
        # runner through any supported route must still select the Codex env
        # default, including after a v2 migration preserving v1 semantics.
        for version in (None, 1, 2):
            for route in ('environment', 'stage', 'default'):
                with self.subTest(version=version, route=route):
                    agents={'model':'claude-default','implement':{'model':'opus'}}
                    config={'agents':agents}
                    if version is not None: config['version']=version
                    if version == 2: agents['model_compatibility']='v1'
                    env={'BUREAU_MODEL_IMPLEMENT':'sonnet', 'BUREAU_CODEX_MODEL_DEFAULT':'codex-old-default'}
                    if route == 'environment': env['BUREAU_RUNNER_IMPLEMENT']='codex'
                    elif route == 'stage': agents['implement']['runner']='codex'
                    else: agents['runner']='codex'
                    self.config.write_text(json.dumps(config))
                    self.run_provider(**env)
                    argv=json.loads((self.root/'argv.json').read_text())
                    self.assertEqual(argv[argv.index('--model')+1],'codex-old-default')

        # Without an explicit Codex setting, keep the CLI's own default.
        self.config.write_text(json.dumps({'agents':{'runner':'codex','model':'opus','implement':{'model':'sonnet'}}}))
        self.run_provider(BUREAU_MODEL_IMPLEMENT='haiku')
        self.assertNotIn('--model',json.loads((self.root/'argv.json').read_text()))
        self.run_provider(BUREAU_CODEX_MODEL_IMPLEMENT='codex-stage',BUREAU_CODEX_MODEL_DEFAULT='codex-default')
        argv=json.loads((self.root/'argv.json').read_text())
        self.assertEqual(argv[argv.index('--model')+1],'codex-stage')

    def test_v2_model_ownership_and_compatibility_validation(self):
        self.config.write_text(json.dumps({'version':2,'agents':{'runner':'codex','implement':{'model':'codex-v2'}}}))
        self.run_provider(BUREAU_CODEX_MODEL_DEFAULT='lower-priority')
        argv=json.loads((self.root/'argv.json').read_text())
        self.assertEqual(argv[argv.index('--model')+1],'codex-v2')
        self.config.write_text(json.dumps({'agents':{'runner':'codex','model_compatibility':'v2','model':'codex-opt-in'}}))
        self.run_provider()
        argv=json.loads((self.root/'argv.json').read_text())
        self.assertEqual(argv[argv.index('--model')+1],'codex-opt-in')
        for value in ('v3', None, False, {}):
            self.config.write_text(json.dumps({'agents':{'model_compatibility':value}}))
            self.run_provider(code=22)

    def test_headroom_passes_separator_to_the_real_adapter(self):
        self.config.write_text('{"agents":{"runner":"claude","headroom_wrap":true}}')
        wrapper=self.bin/'headroom'
        wrapper.write_text('#!/bin/sh\n[ "$1" = wrap ] && [ "$2" = claude ] && [ "$3" = -- ] || exit 99\nshift 3\nexec claude "$@"\n')
        wrapper.chmod(0o755)
        self.run_provider()

    def test_codex_only_does_not_invoke_claude_auth(self):
        (self.bin/'claude').write_text('#!/bin/sh\necho unexpected Claude >&2\nexit 99\n')
        self.run_provider()


    # ── The provider's own record of a call (v3.2) ─────────────────────────
    def evidence(self):
        runs=sorted((path for path in (self.root/'evidence').iterdir() if path.is_dir()),key=lambda path:path.stat().st_mtime)
        return runs[-1], json.loads((runs[-1]/'result.json').read_text())

    def claude_projects_dir(self,config_dir):
        # Claude Code's rule, written out independently of the adapter: the resolved working
        # directory with every character other than an ASCII letter or digit turned into '-'.
        # The Claude tests work in a directory whose name holds a space, a dot and an underscore.
        if not hasattr(self,'repo'):
            self.repo=self.root/'work tree.v3_2'; self.repo.mkdir()
        slug=''.join(c if c.isascii() and c.isalnum() else '-' for c in os.path.realpath(self.repo))
        self.assertIn('-work-tree-v3-2',slug)
        return Path(config_dir)/'projects'/slug

    def test_claude_gets_a_session_id_and_the_transcript_is_recorded(self):
        self.config.write_text('{"agents":{"runner":"claude"}}')
        folder=self.claude_projects_dir(self.env['CLAUDE_CONFIG_DIR'])
        seen=set()
        for _ in range(2):
            result=self.run_provider(TRANSCRIPT_DIR=str(folder))
            argv=json.loads((self.root/'argv.json').read_text())
            session=argv[argv.index('--session-id')+1]
            self.assertEqual(str(uuid.UUID(session)),session)
            run,metadata=self.evidence()
            self.assertEqual(metadata['session_id'],session)
            self.assertEqual(metadata['transcript'],str(folder/(session+'.jsonl')))
            self.assertTrue(metadata['transcript_found'])
            self.assertIn('Bureau provider transcript: '+metadata['transcript'],result.stderr)
            seen.add(session)
        self.assertEqual(len(seen),2,'every call gets its own session id')

    def test_claude_timeout_keeps_the_transcript_path_although_stdout_is_empty(self):
        self.config.write_text('{"agents":{"runner":"claude"}}')
        folder=self.claude_projects_dir(self.env['CLAUDE_CONFIG_DIR'])
        result=self.run_provider(TRANSCRIPT_DIR=str(folder),SLEEP='10',BUREAU_STAGE_TIMEOUT='0.5',code=124)
        run,metadata=self.evidence()
        self.assertEqual(metadata['outcome'],'timeout')
        self.assertEqual((run/'stdout.log').read_text(),'')
        self.assertTrue(metadata['transcript_found'])
        self.assertEqual(metadata['transcript'],str(folder/(metadata['session_id']+'.jsonl')))
        self.assertTrue(Path(metadata['transcript']).exists())
        self.assertIn('Bureau provider transcript: '+metadata['transcript'],result.stderr)

    def test_claude_transcript_under_home_elsewhere_or_missing(self):
        self.config.write_text('{"agents":{"runner":"claude"}}')
        env={k:v for k,v in self.env.items() if k!='CLAUDE_CONFIG_DIR'}
        home=self.root/'home'
        folder=self.claude_projects_dir(home/'.claude')
        result=subprocess.run(self.command(),capture_output=True,text=True,timeout=12,
                              env={**env,'HOME':str(home),'TRANSCRIPT_DIR':str(folder)})
        self.assertEqual(result.returncode,0,result.stderr)
        _,metadata=self.evidence()
        self.assertEqual(metadata['transcript'],str(folder/(metadata['session_id']+'.jsonl')))
        self.assertTrue(metadata['transcript_found'])
        # Where a Claude version names the project directory differently, the session id finds it.
        other=Path(self.env['CLAUDE_CONFIG_DIR'])/'projects'/'another-name'
        self.run_provider(TRANSCRIPT_DIR=str(other))
        _,metadata=self.evidence()
        self.assertEqual(metadata['transcript'],str(other/(metadata['session_id']+'.jsonl')))
        self.assertTrue(metadata['transcript_found'])
        # Not written at all: the expected path, marked as not found.
        self.run_provider()
        _,metadata=self.evidence()
        expected=self.claude_projects_dir(self.env['CLAUDE_CONFIG_DIR'])/(metadata['session_id']+'.jsonl')
        self.assertEqual(metadata['transcript'],str(expected))
        self.assertFalse(metadata['transcript_found'])

    def test_claude_invalid_result_still_records_the_transcript(self):
        self.config.write_text('{"agents":{"runner":"claude"}}')
        folder=self.claude_projects_dir(self.env['CLAUDE_CONFIG_DIR'])
        self.run_provider(TRANSCRIPT_DIR=str(folder),RAW='no envelope',code=22)
        _,metadata=self.evidence()
        self.assertEqual(metadata['outcome'],'invalid-result')
        self.assertTrue(metadata['transcript_found'])
        self.assertEqual(metadata['transcript'],str(folder/(metadata['session_id']+'.jsonl')))

    def test_codex_thread_id_and_rollout_file_are_recorded_also_on_timeout(self):
        thread='01a0f1b9-222e-7cf0-807b-497141dcd44c'
        day=Path(self.env['CODEX_HOME'])/'sessions'/'2026'/'10'/'01'; day.mkdir(parents=True)
        rollout=day/('rollout-2026-10-01T09-00-00-'+thread+'.jsonl'); rollout.write_text('{}\n')
        result=self.run_provider(THREAD=thread)
        _,metadata=self.evidence()
        self.assertEqual((metadata['session_id'],metadata['transcript'],metadata['transcript_found']),(thread,str(rollout),True))
        self.assertIn('Bureau provider transcript: '+str(rollout),result.stderr)
        self.assertNotIn('--session-id',json.loads((self.root/'argv.json').read_text()))
        self.run_provider(THREAD=thread,SLEEP='10',BUREAU_STAGE_TIMEOUT='0.5',code=124)
        _,metadata=self.evidence()
        self.assertEqual((metadata['outcome'],metadata['session_id'],metadata['transcript']),('timeout',thread,str(rollout)))

    def test_codex_without_a_usable_thread_id_records_none(self):
        for thread in (None,'../../x*','short'):
            with self.subTest(thread=thread):
                env={} if thread is None else {'THREAD':thread}
                self.run_provider(**env)
                _,metadata=self.evidence()
                self.assertIsNone(metadata['session_id']); self.assertIsNone(metadata['transcript'])
                self.assertFalse(metadata['transcript_found'])
                self.assertIn('no thread id',metadata['transcript_note'])
        # A thread id whose rollout file is not there: the id, no path.
        self.run_provider(THREAD='01a0f1b9-0000-7cf0-807b-497141dcd44c')
        _,metadata=self.evidence()
        self.assertEqual((metadata['session_id'],metadata['transcript'],metadata['transcript_found']),
                         ('01a0f1b9-0000-7cf0-807b-497141dcd44c',None,False))

    def help_calls(self):
        path=self.root/'help-calls'
        return len(path.read_text().splitlines()) if path.exists() else 0

    def test_claude_without_session_id_runs_as_before(self):
        # A Claude CLI that does not know --session-id: the call runs as in v3.1.0, without the
        # flag, and the evidence says that no session id was set and why.
        self.config.write_text('{"agents":{"runner":"claude"}}')
        for _ in range(2):
            result=self.run_provider(OLD_CLI='1')
            self.assertEqual(result.stdout.strip(),'final response')
            self.assertNotIn('--session-id',json.loads((self.root/'argv.json').read_text()))
            _,metadata=self.evidence()
            self.assertEqual((metadata['outcome'],metadata['session_id'],metadata['transcript'],metadata['transcript_found']),
                             ('complete',None,None,False))
            self.assertIn('does not take --session-id',metadata['transcript_note'])
            self.assertIn('no session id was set',metadata['transcript_note'])
            self.assertNotIn('Bureau provider transcript',result.stderr)
        self.assertEqual(self.help_calls(),1,'the answer is kept for the unchanged binary')

    def test_session_id_support_is_asked_once_per_binary(self):
        self.config.write_text('{"agents":{"runner":"claude"}}')
        for _ in range(3): self.run_provider(GH_TOKEN='ghp_probe_help_check')
        self.assertEqual(self.help_calls(),1,'one claude --help for three calls')
        self.assertNotIn('GH_TOKEN',json.loads((self.root/'help-env.json').read_text()),'the help check gets the agent environment')
        self.assertIn('--session-id',json.loads((self.root/'argv.json').read_text()))
        # An updated binary (another mtime) is asked again; so is one at another path.
        stat=(self.bin/'claude').stat(); os.utime(self.bin/'claude',ns=(stat.st_atime_ns,stat.st_mtime_ns+10**9))
        self.run_provider()
        self.assertEqual(self.help_calls(),2,'a changed binary is asked again')
        self.run_provider()
        self.assertEqual(self.help_calls(),2)

    def test_failed_help_leaves_the_flag_off_and_is_not_kept(self):
        self.config.write_text('{"agents":{"runner":"claude"}}')
        for expected_calls in (1,2):
            self.run_provider(HELP_FAIL='1')
            self.assertNotIn('--session-id',json.loads((self.root/'argv.json').read_text()))
            _,metadata=self.evidence()
            self.assertIsNone(metadata['session_id'])
            self.assertIn('claude --help failed with exit 2',metadata['transcript_note'])
            self.assertEqual(self.help_calls(),expected_calls)

    def test_claude_project_slug_matches_claude_code(self):
        # Expected values from Claude Code 2.1.286's own functions (k, tx, EQ, copied from its
        # binary and run under node): a space, a dot and an underscore; an emoji (two UTF-16
        # code units) and a non-ASCII letter; two paths over 200 characters (cut, base-36 hash).
        long_ab='/srv/'+'a'*120+'/'+'b'*120
        deep='/srv/\u00dcn\u00efc\u00f6d\u00e9/'+'deep-dir-'*30+'\U0001f680'
        for path,slug in (('/tmp/x y.z_w','-tmp-x-y-z-w'),
                          ('/srv/repo \U0001f680 gr\u00fcn','-srv-repo----gr-n'),
                          (long_ab,'-srv-'+'a'*120+'-'+'b'*74+'-ak6yfc'),
                          (deep,'-srv--n-c-d--'+('deep-dir-'*21)[:187]+'-9adu9o')):
            with self.subTest(path=path[:40]):
                self.assertEqual(p.claude_project_slug(path),slug)
                self.assertLessEqual(len(slug),200+1+7)

    def test_claude_transcript_in_an_emoji_and_a_long_directory(self):
        self.config.write_text('{"agents":{"runner":"claude"}}')
        for name in ('repo \U0001f680 gr\u00fcn','x'*120+'/'+'y'*120):
            with self.subTest(name=name[:20]):
                self.repo=self.root/name; self.repo.mkdir(parents=True)
                slug=p.claude_project_slug(os.path.realpath(self.repo))
                folder=Path(self.env['CLAUDE_CONFIG_DIR'])/'projects'/slug
                self.run_provider(TRANSCRIPT_DIR=str(folder))
                _,metadata=self.evidence()
                self.assertEqual(metadata['transcript'],str(folder/(metadata['session_id']+'.jsonl')))
                self.assertTrue(metadata['transcript_found'])

if __name__=='__main__': unittest.main()
