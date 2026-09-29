import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('doctor', ROOT/'templates/scripts/bureau-doctor.py')
d=importlib.util.module_from_spec(spec); spec.loader.exec_module(d)


class DoctorTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='bureau doctor '); self.addCleanup(self.temp.cleanup)
        self.repo=Path(self.temp.name).resolve()
        subprocess.run(['git','init','-q',str(self.repo)],check=True)
        self.config={'version':1,'linear':{'teams':[{'id':'team-id','key':'TEAM','states':{'build':'state-id'}}],
                     'labels':{'lane2':{'id':'label-id','name':'custom-name'}}},
                     'agents':{'spec':'true','spec_review':'false','model':'legacy-model','implement':True,'qa':False,'custom_extension':{'keep':3}},
                     'repo':{'test_command':'python3 test.py'},'extension':['preserve']}
        self.path=self.repo/'.bureau.json';self.path.write_text(json.dumps(self.config))
        self.env=patch.dict(os.environ,{'BUREAU_CONFIG':str(self.path)});self.env.start();self.addCleanup(self.env.stop)

    def test_preview_apply_backup_and_idempotence(self):
        before=self.path.read_bytes()
        result=d.migration(self.path); self.assertTrue(result['changed']);self.assertEqual(self.path.read_bytes(),before)
        result=d.migration(self.path,True); migrated=json.loads(self.path.read_text())
        expected=copy.deepcopy(self.config);expected['version']=2;expected['agents']['runner']='claude'
        expected['agents']['model_compatibility']='v1'
        self.assertEqual(migrated,expected);self.assertEqual(Path(result['backup']).read_bytes(),before)
        self.assertEqual(Path(result['backup']).stat().st_mode & 0o777,0o600)
        self.assertIn(self.repo/'.git',Path(result['backup']).parents)
        after=self.path.read_bytes();self.assertFalse(d.migration(self.path,True)['changed']);self.assertEqual(self.path.read_bytes(),after)

    def test_malformed_and_future_configs_never_change(self):
        for value in ([], {**self.config,'version':3}, {**self.config,'agents':[]},
                      {**self.config,'agents':{'providers':[]}}, {**self.config,'agents':{'implement':{'runner':'unknown'}}}):
            self.path.write_text(json.dumps(value)); before=self.path.read_bytes()
            with self.assertRaises(ValueError): d.migration(self.path,True)
            self.assertEqual(before,self.path.read_bytes())

    def test_installed_doctor_uses_real_provider_resolution_and_managed_hashes(self):
        (self.repo/'AGENTS.md').write_text('User guidance\n')
        subprocess.run([sys.executable,str(ROOT/'scripts/bureau_install.py'),'assets','--repo',str(self.repo),
                        '--target','both','--scope','interfaces','--scope','scripts','--apply'],check=True,stdout=subprocess.DEVNULL)
        result=d.diagnose(self.repo,'app')
        self.assertTrue(result['ok'],result);self.assertEqual(result['drift'],[])
        self.assertEqual(result['effective_stages']['implement']['model'],'legacy-model')
        self.assertNotIn('qa',result['effective_stages'])
        (self.repo/'scripts/bureau-runtime.py').write_text('local edit')
        self.assertIn('scripts/bureau-runtime.py',d.diagnose(self.repo,'app')['drift'])

    def test_doctor_reports_recorded_missing_and_stale_template_source(self):
        installer=[sys.executable,str(ROOT/'scripts/bureau_install.py')]
        subprocess.run(installer+['assets','--repo',str(self.repo),'--target','both','--scope','interfaces','--scope','scripts','--apply'],
                       check=True,stdout=subprocess.DEVNULL)
        path=self.repo/'.bureau-install.json'; manifest=json.loads(path.read_text())
        result=d.diagnose(self.repo,'app')
        self.assertTrue(result['ok'],result); self.assertEqual(result['template_source']['status'],'recorded')
        self.assertEqual(result['template_source']['scopes'],manifest['sources'])
        self.assertEqual(set(manifest['sources']),{'interfaces/claude','interfaces/codex','scripts'})
        proc=subprocess.run(installer+['doctor','--repo',str(self.repo)],capture_output=True,text=True)
        self.assertEqual(proc.returncode,0,proc.stdout+proc.stderr)
        self.assertEqual(json.loads(proc.stdout)['template_source']['scopes'],manifest['sources'])
        # A partial apply of a second scope keeps the record valid for every scope.
        subprocess.run(installer+['assets','--repo',str(self.repo),'--scope','ci','--apply'],check=True,stdout=subprocess.DEVNULL)
        manifest=json.loads(path.read_text()); result=d.diagnose(self.repo,'app')
        self.assertEqual(result['template_source']['status'],'recorded',result['template_source'])
        self.assertEqual(set(result['template_source']['scopes']),{'ci','interfaces/claude','interfaces/codex','scripts'})
        # An installer that predates source recording rewrites hashes and carries the old record along.
        stale=copy.deepcopy(manifest); stale['files']['scripts/queue-loop.sh']='0'*64; path.write_text(json.dumps(stale))
        result=d.diagnose(self.repo,'app')
        self.assertTrue(result['ok'],result); self.assertEqual(result['template_source']['status'],'stale')
        self.assertTrue(any(w.startswith('Recorded template source is stale') for w in result['warnings']),result['warnings'])
        legacy={k:v for k,v in manifest.items() if k not in ('sources','sources_files_sha256')}; path.write_text(json.dumps(legacy))
        proc=subprocess.run(installer+['doctor','--repo',str(self.repo)],capture_output=True,text=True)
        self.assertEqual(proc.returncode,0,proc.stdout+proc.stderr)
        result=json.loads(proc.stdout); self.assertEqual(result['template_source']['status'],'source not recorded')
        self.assertIn('predates source recording',result['template_source']['reason']); self.assertEqual(result['errors'],[])
        self.assertFalse(any('source' in w for w in result['warnings']),result['warnings'])
        path.unlink(); result=d.diagnose(self.repo,'app')
        self.assertTrue(result['ok'],result)
        self.assertEqual(result['template_source'],{'status':'source not recorded','reason':'no installation manifest'})

    def test_partial_apply_after_a_rollback_drops_the_stale_record(self):
        installer=[sys.executable,str(ROOT/'scripts/bureau_install.py'),'assets','--repo',str(self.repo)]
        subprocess.run(installer+['--target','both','--scope','interfaces','--scope','scripts','--apply'],check=True,stdout=subprocess.DEVNULL)
        path=self.repo/'.bureau-install.json'; manifest=json.loads(path.read_text())
        # What an installer without source recording writes when it rolls a script back: the older
        # bytes, their hash in `files`, and the new installer's record carried along unchanged.
        old=b'# rolled back\n'; (self.repo/'scripts/bureau-doctor.py').write_bytes(old)
        manifest['files']['scripts/bureau-doctor.py']=hashlib.sha256(old).hexdigest(); path.write_text(json.dumps(manifest))
        self.assertEqual(d.diagnose(self.repo,'app')['template_source']['status'],'stale')
        subprocess.run(installer+['--scope','ci','--apply'],check=True,stdout=subprocess.DEVNULL)
        after=json.loads(path.read_text())
        self.assertEqual(set(after['sources']),{'ci'})
        self.assertEqual(after['files']['scripts/bureau-doctor.py'],hashlib.sha256(old).hexdigest())
        source=d.template_source(after)
        self.assertEqual((source['status'],set(source['scopes'])),('recorded',{'ci'}))
        # A batch that installs nothing after that leaves no scope recorded, and says so.
        after['sources']={}; after['sources_files_sha256']='x'; path.write_text(json.dumps(after))
        subprocess.run(installer+['--target','codex','--scope','workflows','--apply'],check=True,stdout=subprocess.DEVNULL)
        source=d.template_source(json.loads(path.read_text()))
        self.assertEqual(source['status'],'source not recorded'); self.assertIn('no asset scope has a recorded source',source['reason'])
        self.assertNotIn('predates',source['reason'])

    def test_object_disabled_stays_disabled_in_shell_and_diagnostics(self):
        self.config['agents']['implement']={'enabled':False,'runner':'codex'};self.path.write_text(json.dumps(self.config))
        proc=subprocess.run(['bash','-c','source "$1"; agent_enabled implement','test',str(ROOT/'templates/scripts/bureau-config.sh')],
                            cwd=self.repo,capture_output=True,text=True)
        self.assertEqual(proc.returncode,1,proc.stderr)
        self.assertEqual(d.migrate(self.config)['agents']['implement'],self.config['agents']['implement'])

    def test_scripts_only_install_does_not_mistake_generic_guidance_for_bureau(self):
        subprocess.run([sys.executable,str(ROOT/'scripts/bureau_install.py'),'assets','--repo',str(self.repo),
                        '--target','codex','--scope','scripts','--apply'],check=True,stdout=subprocess.DEVNULL)
        for guidance in ('# Project instructions\nRun the tests.\n',
                         '<!-- bureau-init:begin --><!-- bureau-init:end -->',
                         '<!-- bureau-init:end -->Text<!-- bureau-init:begin -->'):
            (self.repo/'AGENTS.md').write_text(guidance)
            result=d.diagnose(self.repo,'app')
            self.assertFalse(result['ok'],result)
            self.assertFalse(result['interfaces']['AGENTS.md'])
            self.assertIn('No Bureau instruction or command interfaces found; install interfaces',result['errors'])

    def test_background_github_stages_require_gh_but_app_does_not(self):
        subprocess.run([sys.executable,str(ROOT/'scripts/bureau_install.py'),'assets','--repo',str(self.repo),
                        '--target','codex','--scope','interfaces','--scope','scripts','--apply'],check=True,stdout=subprocess.DEVNULL)
        with patch.object(d.shutil,'which',side_effect=lambda name: None if name == 'gh' else '/bin/'+name):
            for stage in ('implement','code_review','merge','rebase'):
                for setting in (True, 'true', {'enabled':True,'runner':'codex'}):
                    self.config['agents']={stage:setting};self.path.write_text(json.dumps(self.config))
                    result=d.diagnose(self.repo,'background')
                    self.assertFalse(result['ok'],(stage,setting,result))
                    self.assertIn('Missing executable: gh',result['errors'])
                    self.assertTrue(d.diagnose(self.repo,'app')['ok'])
            self.config['agents']={'qa':True,'implement':{'enabled':False}}
            self.path.write_text(json.dumps(self.config))
            self.assertTrue(d.diagnose(self.repo,'background')['ok'])


    def test_merge_mode_is_reported_with_its_warnings(self):
        subprocess.run([sys.executable,str(ROOT/'scripts/bureau_install.py'),'assets','--repo',str(self.repo),
                        '--target','codex','--scope','interfaces','--scope','scripts','--apply'],check=True,stdout=subprocess.DEVNULL)
        unknown='the pipelines fall closed to manual'; no_state='linear.teams[0].states.merge is not set'
        # (value, Merge state, reported mode, warnings, errors)
        for value, state, mode, warns, errs in ((None, None, 'auto', (), ()), ('auto', None, 'auto', (), ()),
                                                ('manual', 'merge-id', 'manual', (), ()), ('manual', None, 'manual', (), (no_state,)),
                                                ('Manual', 'merge-id', 'manual', (unknown,), ()), ('auto\n', 'merge-id', 'manual', (unknown,), ()),
                                                (False, None, 'manual', (unknown,), (no_state,))):
            with self.subTest(value=value, state=state):
                config=copy.deepcopy(self.config)
                if value is not None: config['agents']['merge_mode']=value
                if state: config['linear']['teams'][0]['states']['merge']=state
                self.path.write_text(json.dumps(config))
                result=d.diagnose(self.repo,'app')
                self.assertEqual(result['ok'],not errs,result); self.assertEqual(result['merge_mode'],mode)
                for found, wanted in ((result['warnings'], warns), (result['errors'], errs)):
                    merge_found=[w for w in found if 'merge_mode' in w]
                    self.assertEqual(len(merge_found),len(wanted),merge_found)
                    for text in wanted: self.assertTrue(any(text in w for w in merge_found),(text,merge_found))

    def test_post_implement_command_is_reported_and_checked(self):
        subprocess.run([sys.executable,str(ROOT/'scripts/bureau_install.py'),'assets','--repo',str(self.repo),
                        '--target','codex','--scope','interfaces','--scope','scripts','--apply'],check=True,stdout=subprocess.DEVNULL)
        # (value, reported, error expected)
        for value, reported, err in ((None, None, False), ('', None, False), (False, None, False),
                                     ('python3 scripts/regen.py "$BUREAU_ISSUE"', 'python3 scripts/regen.py "$BUREAU_ISSUE"', False),
                                     (['make', 'docs'], None, True), (5, None, True)):
            with self.subTest(value=value):
                config=copy.deepcopy(self.config); config.setdefault('repo',{})
                if value is not None: config['repo']['post_implement_command']=value
                self.path.write_text(json.dumps(config))
                result=d.diagnose(self.repo,'app')
                self.assertEqual(result['post_implement_command'],reported,result)
                found=[e for e in result['errors'] if 'post_implement_command' in e]
                self.assertEqual(bool(found),err,result['errors']); self.assertEqual(result['ok'],not err,result)

    def test_worktree_links_are_reported_and_checked_as_symlinks(self):
        subprocess.run([sys.executable,str(ROOT/'scripts/bureau_install.py'),'assets','--repo',str(self.repo),
                        '--target','codex','--scope','interfaces','--scope','scripts','--apply'],check=True,stdout=subprocess.DEVNULL)
        (self.repo/'.venv'/'bin').mkdir(parents=True); (self.repo/'my env').mkdir(); (self.repo/'dirvenv').mkdir()
        # .venv and "my env" are ignored as a symlink would be; dirvenv/ only as a directory: in
        # the main checkout it is a directory and git calls it ignored, in a stage worktree the
        # link would not be.
        (self.repo/'.gitignore').write_text('.venv\nmy env\ndirvenv/\n')
        missing='does not exist in the main checkout'; symlink='is not ignored as a symlink'
        not_list='must be a list'; plain='must be a plain relative path'; one_line='is not a one-line string'
        # (value, reported, warnings, errors)
        for value, reported, warns, errs in (
                (None, [], (), ()),
                ([], [], (), ()),
                (['.venv', 'my env/'], [dict(path='.venv', status='ok'), dict(path='my env', status='ok')], (), ()),
                (['venv'], [dict(path='venv', status='missing in the main checkout')], (missing,), ()),
                (['dirvenv'], [dict(path='dirvenv', status='not ignored as a symlink')], (symlink,), ()),
                ('.venv', [], (), (not_list,)),
                (['/abs'], [dict(path='/abs', status='invalid')], (), (plain,)),
                (['../up'], [dict(path='../up', status='invalid')], (), (plain,)),
                (['.git'], [dict(path='.git', status='invalid')], (), (plain,)),
                ([7], [], (), (one_line,))):
            with self.subTest(value=value):
                config=copy.deepcopy(self.config); config.setdefault('repo',{})
                if value is not None: config['repo']['worktree_links']=value
                self.path.write_text(json.dumps(config))
                result=d.diagnose(self.repo,'app')
                self.assertEqual(result['worktree_links'],reported,result)
                for found, wanted in ((result['warnings'], warns), (result['errors'], errs)):
                    link_found=[w for w in found if 'worktree_links' in w]
                    self.assertEqual(len(link_found),len(wanted),link_found)
                    for text in wanted: self.assertTrue(any(text in w for w in link_found),(text,link_found))
                self.assertEqual(result['ok'],not errs,result)
        self.assertTrue((self.repo/'.venv').is_dir() and not (self.repo/'.venv').is_symlink())

    def test_migration_preserves_effective_models_across_runner_overrides(self):
        provider=d.module('provider')
        for version in (None, 1):
            for route in ('environment', 'stage', 'default'):
                with self.subTest(version=version,route=route):
                    config=copy.deepcopy(self.config)
                    if version is None: config.pop('version',None)
                    else: config['version']=version
                    config['agents']={'model':'claude-default','implement':{'model':'opus'}}
                    env={'BUREAU_MODEL_IMPLEMENT':'sonnet','BUREAU_CODEX_MODEL_DEFAULT':'codex-default'}
                    if route=='environment': env['BUREAU_RUNNER_IMPLEMENT']='codex'
                    elif route=='stage': config['agents']['implement']['runner']='codex'
                    else: config['agents']['runner']='codex'
                    before=provider.configuration('implement',config,env)
                    self.path.write_text(json.dumps(config));d.migration(self.path,True)
                    migrated=json.loads(self.path.read_text())
                    after=provider.configuration('implement',migrated,env)
                    self.assertEqual(after,before)
                    self.assertEqual(after['model'],'codex-default')
                    self.assertEqual(migrated['agents']['implement']['model'],'opus')
                    self.assertEqual(migrated['agents']['model_compatibility'],'v1')

    def test_explicit_v2_model_semantics_survive_migration(self):
        provider=d.module('provider')
        for version in (1, 2):
            config=copy.deepcopy(self.config);config['version']=version
            config['agents']={'runner':'codex','model_compatibility':'v2','implement':{'model':'codex-stage'}}
            migrated=d.migrate(config)
            self.assertEqual(migrated['agents']['model_compatibility'],'v2')
            self.assertEqual(provider.configuration('implement',migrated,{})['model'],'codex-stage')
        config['agents']['model_compatibility']='invalid'
        with self.assertRaises(ValueError): d.migrate(config)


if __name__=='__main__': unittest.main()
