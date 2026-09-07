import copy
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
