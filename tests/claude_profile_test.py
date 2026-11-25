"""Names-only profile diagnostics and child environments; no CLI, Git or network calls."""
import copy
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

from mutation_checks import caught_mutation

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / 'templates/scripts'
spec = importlib.util.spec_from_file_location('profile_doctor', SCRIPTS / 'bureau-doctor.py')
d = importlib.util.module_from_spec(spec); spec.loader.exec_module(d)
p = d.module('provider')
STAGES = ('spec', 'spec_review', 'research', 'ux', 'copy', 'implement', 'qa', 'code_review',
          'conflict_resolver', 'upstream_port', 'upstream_summary')
WARNING = 'Claude background stages inherit'


def without(module, before, after):
    """An in-memory source mutation, leaving the worktree and fixtures unchanged."""
    source = Path(module.__file__).read_text()
    if source.count(before) != 1: raise AssertionError('mutation no longer matches source')
    control = types.ModuleType('profile_control'); control.__file__ = module.__file__
    exec(compile(source.replace(before, after), module.__file__, 'exec'), control.__dict__)
    return control


class ProfileTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='bureau-profile-'); self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.home = self.root / 'home'; self.profile = self.home / '.claude'
        self.profile.mkdir(parents=True)
        self.lean = self.root / 'lean profile'; self.lean.mkdir()
        # This fixture is deliberately not a Git repository. Unrelated doctor
        # probes are mocked; any attempted subprocess is an error.
        self.repo = self.root / 'workspace'; (self.repo / 'scripts').mkdir(parents=True)
        for name in ('bureau-runtime.py', 'bureau-provider.py', 'bureau-stage.md'):
            (self.repo / 'scripts' / name).touch()
        (self.repo / 'AGENTS.md').write_text('<!-- bureau-init:begin -->\nFixture.\n<!-- bureau-init:end -->\n')
        self.config = {'version': 2, 'linear': {'teams': [{'id': 'team', 'key': 'TEAM', 'states': {'merge': 'merge'}}]},
                       'agents': {'runner': 'claude', 'merge_publishers': ['app:reviewer'],
                                  'implement': True, 'qa': True, 'code_review': True},
                       'repo': {'test_command': 'python3 check.py'}}
        env = patch.dict(os.environ, {'HOME': str(self.home), 'PATH': os.defpath}, clear=True)
        env.start(); self.addCleanup(env.stop)

    def diagnose(self, config=None):
        path = self.repo / '.bureau.json'; path.write_text(json.dumps(config or self.config))
        runtime = d.module('runtime')
        with patch.object(runtime, 'root_for', return_value=self.repo), \
                patch.object(runtime, 'config_for', return_value=path), \
                patch.object(d, 'main_checkout', return_value=(self.repo, None)), \
                patch.object(d, 'test_wrapper_diagnostics', return_value={'enabled': False}), \
                patch.object(d, 'codex_gate_duration_warning', return_value=None), \
                patch.object(d, 'uses_git_lfs', return_value=False), \
                patch.object(d, 'claude_logged_in', return_value=(True, None)), \
                patch.object(d.shutil, 'which', return_value='fixture-executable'), \
                patch.object(d.subprocess, 'check_output', return_value=b''), \
                patch.object(d.subprocess, 'run', side_effect=AssertionError('unexpected subprocess')):
            result = d.diagnose(self.repo, 'background')
        self.assertTrue(result['ok'], result)
        return result

    def inventory(self, result, directory=None):
        return result['claude_profiles'][str(directory or self.profile)]

    def inherited(self, warnings):
        found = [w for w in warnings if w.startswith(WARNING)]
        self.assertEqual(len(found), 1, 'inherited profile warning missing or duplicated')
        self.assertIn('implement, qa, code_review', found[0])
        return found[0]

    def seed_names(self, directory=None, mcp=None):
        directory = directory or self.profile
        mcp = mcp or self.home / '.claude.json'
        mcp.write_text(json.dumps({'mcpServers': {
            'beta': {'url': 'https://PRIVATE_ENDPOINT.invalid', 'headers': {'Authorization': 'PRIVATE_HEADER'}},
            'alpha': {'command': 'PRIVATE_COMMAND', 'args': ['PRIVATE_ARGUMENT'], 'env': {'TOKEN': 'PRIVATE_TOKEN'}}},
            'projects': {'ignored': {'mcpServers': {'project-only': {}}}}}))
        (directory / 'settings.json').write_text(json.dumps({
            'enabledPlugins': {'enabled@market': True, 'disabled@market': False},
            'env': {'TOKEN': 'PRIVATE_SETTINGS'}, 'hooks': {'command': 'PRIVATE_HOOK'}}))

    def test_doctor_warns_once_and_lean_profile_is_the_control(self):
        self.inherited(self.diagnose()['warnings'])
        config = copy.deepcopy(self.config)
        config['agents']['providers'] = {'claude': {'config_dir': str(self.lean)}}
        result = self.diagnose(config)
        self.assertFalse(any(w.startswith(WARNING) for w in result['warnings']))
        self.assertEqual(set(result['claude_profiles']), {str(self.lean)})
        self.assertTrue(all(entry['configured'] for entry in result['claude_config_dirs'].values()))

    def test_only_sorted_user_scope_names_are_reported_without_secrets(self):
        self.seed_names()
        before = {f: f.read_bytes() for f in self.home.rglob('*') if f.is_file()}
        result = self.diagnose(); inventory = self.inventory(result)
        self.assertEqual(inventory['mcp_servers']['names'], ['alpha', 'beta'])
        self.assertEqual(inventory['plugins']['names'], ['enabled@market'])
        self.assertEqual(inventory['mcp_servers']['file'], str(self.home / '.claude.json'))
        self.assertEqual(inventory['plugins']['file'], str(self.profile / 'settings.json'))
        self.assertEqual(inventory['stages'], ['implement', 'qa', 'code_review'])
        text = json.dumps(result)
        for hidden in ('PRIVATE_', 'disabled@market', 'project-only', 'Authorization'):
            self.assertNotIn(hidden, text)
        self.assertEqual({f: f.read_bytes() for f in self.home.rglob('*') if f.is_file()}, before)

    def test_doctor_uses_launcher_resolution_and_reads_the_selected_profile(self):
        self.seed_names(self.lean, self.lean / '.claude.json')
        config = copy.deepcopy(self.config)
        for overrides, provider in (({}, {'config_dir': str(self.lean)}),
                                    ({'CLAUDE_CONFIG_DIR': str(self.lean)}, {}),
                                    ({'CLAUDE_CONFIG_DIR': 'lean profile'}, {}),
                                    ({'BUREAU_CLAUDE_CONFIG_DIR': str(self.lean)}, {'config_dir': str(self.profile)})):
            with self.subTest(overrides=overrides), patch.dict(os.environ, overrides), \
                    patch.object(p, 'claude_profile_dir', wraps=p.claude_profile_dir) as resolve:
                config['agents']['providers'] = {'claude': provider}
                # The child interprets an inherited relative path from its cwd.
                with patch.object(d, 'claude_profile_names', wraps=d.claude_profile_names) as read:
                    result = self.diagnose(config)
                self.assertEqual(resolve.call_count, 3)
                expected = self.repo / 'lean profile' if overrides.get('CLAUDE_CONFIG_DIR') == 'lean profile' else self.lean
                self.assertEqual(set(result['claude_profiles']), {str(expected)})
                self.assertEqual(read.call_count, 1, 'one inventory per shared profile')
                configured = bool(provider.get('config_dir') or overrides.get('BUREAU_CLAUDE_CONFIG_DIR'))
                self.assertEqual(any(w.startswith(WARNING) for w in result['warnings']), not configured)
                if expected == self.lean:
                    self.assertEqual(self.inventory(result, expected)['mcp_servers']['names'], ['alpha', 'beta'])
                    self.assertEqual(self.inventory(result, expected)['plugins']['names'], ['enabled@market'])

    def test_missing_files_are_explicitly_empty(self):
        inventory = self.inventory(self.diagnose())
        for kind in ('mcp_servers', 'plugins'):
            self.assertEqual(inventory[kind]['status'], 'empty')
            self.assertEqual(inventory[kind]['names'], [])
            self.assertEqual(inventory[kind]['reason'], 'file missing')

    def test_malformed_files_warn_could_not_check_without_echoing_input(self):
        for kind, path in (('mcp_servers', self.home / '.claude.json'), ('plugins', self.profile / 'settings.json')):
            for invalid in (b'{PRIVATE_MALFORMED', b'\xff', b'[]', b'{"mcpServers":[],"enabledPlugins":[]}'):
                with self.subTest(kind=kind, invalid=invalid):
                    path.write_bytes(invalid)
                    result = self.diagnose(); entry = self.inventory(result)[kind]
                    self.assertEqual(entry['status'], 'could not check')
                    self.assertIsNone(entry['names'])
                    self.assertTrue(any(kind + ' could not check' in w for w in result['warnings']))
                    self.assertNotIn('PRIVATE_MALFORMED', json.dumps(result))
                    path.unlink()

    def test_unreadable_files_warn_could_not_check_without_exception_text(self):
        original = Path.read_text
        for kind, target in (('mcp_servers', self.home / '.claude.json'), ('plugins', self.profile / 'settings.json')):
            target.write_text('{}')
            def read(path, *args, **kwargs):
                if path == target: raise PermissionError('PRIVATE_READ_ERROR')
                return original(path, *args, **kwargs)
            with self.subTest(kind=kind), patch.object(Path, 'read_text', read):
                result = self.diagnose()
            self.assertEqual(self.inventory(result)[kind]['status'], 'could not check')
            self.assertTrue(any(kind + ' could not check' in w for w in result['warnings']))
            self.assertNotIn('PRIVATE_READ_ERROR', json.dumps(result))
            target.unlink()

    def test_legacy_global_config_takes_precedence_over_modern_mcp_file(self):
        self.seed_names()
        legacy = self.profile / '.config.json'
        legacy.write_text('{"mcpServers":{"legacy":{}}}')
        entry = self.inventory(self.diagnose())['mcp_servers']
        self.assertEqual(entry['file'], str(legacy)); self.assertEqual(entry['names'], ['legacy'])

    def test_enabled_helpers_are_included_and_codex_and_disabled_stages_are_excluded(self):
        config = copy.deepcopy(self.config)
        config['agents'].update({s: True for s in STAGES})
        config['agents']['qa'] = {'runner': 'codex'}
        config['agents']['copy'] = {'enabled': False}
        result = self.diagnose(config)
        stages = self.inventory(result)['stages']
        self.assertEqual(set(stages), set(STAGES) - {'qa', 'copy'})
        config['agents'] = {'runner': 'codex', 'merge_publishers': ['app:reviewer'], 'implement': True}
        result = self.diagnose(config)
        self.assertEqual(result['claude_profiles'], {})
        self.assertFalse(any(w.startswith(WARNING) for w in result['warnings']))

    def test_negative_control_removing_warning_fails_the_same_assertion(self):
        effective = {s: p.configuration(s, self.config, os.environ) for s in ('implement', 'qa', 'code_review')}
        warnings = []; d.claude_config_dirs(self.config, effective, p, warnings, self.repo)
        self.inherited(warnings)  # A green baseline before the mutation.
        control = without(d, '    if inherited:\n', '    if False:\n')
        warnings = []; control.claude_config_dirs(self.config, effective, p, warnings, self.repo)
        with caught_mutation() as caught: self.inherited(warnings)
        self.assertIsInstance(caught[0], AssertionError)
        self.assertIn('inherited profile warning missing', str(caught[0]))

    def assert_memory_off(self, provider):
        for stage in STAGES:
            for mode in ('default', 'clean'):
                for configured in (False, True):
                    with self.subTest(stage=stage, mode=mode, configured=configured):
                        env = {**os.environ, 'CLAUDE_CODE_DISABLE_AUTO_MEMORY': '0'}
                        config = {'agents': {'runner': 'claude'}}
                        if configured: config['agents']['providers'] = {'claude': {'config_dir': str(self.lean)}}
                        options = provider.configuration(stage, config, env)
                        phases = ('correctness', 'security', 'performance', 'correctness_late', 'security_late',
                                  'performance_late', 'verdict', 'finding_verifier') if stage == 'code_review' else (stage,)
                        for phase in phases:
                            options['phase'] = phase
                            child = provider.claude_env(options, provider.untrusted_env(env, mode, 'claude'))
                            self.assertEqual(child.get('CLAUDE_CODE_DISABLE_AUTO_MEMORY'), '1', 'auto-memory guard missing')
                        self.assertEqual(env['CLAUDE_CODE_DISABLE_AUTO_MEMORY'], '0', 'parent environment changed')

    def test_real_environment_builder_forces_memory_off_for_all_claude_stages(self):
        self.assert_memory_off(p)
        with patch.dict(os.environ, {'CLAUDE_CODE_DISABLE_AUTO_MEMORY': '0'}):
            self.assertEqual(p.claude_env({'runner': 'claude'}, None)['CLAUDE_CODE_DISABLE_AUTO_MEMORY'], '1')
            self.assertEqual(os.environ['CLAUDE_CODE_DISABLE_AUTO_MEMORY'], '0')
        env = {'CLAUDE_CODE_DISABLE_AUTO_MEMORY': '0'}
        self.assertIs(p.claude_env({'runner': 'codex'}, env), env)

    def test_login_and_capability_probe_use_guarded_child_environments(self):
        with patch.dict(os.environ, {'CLAUDE_CODE_DISABLE_AUTO_MEMORY': '0'}), \
                patch.object(p.shutil, 'which', return_value='fixture-claude'), \
                patch.object(p.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, '{"loggedIn":true}', '')) as run:
            p.auth(p.configuration('implement', self.config, os.environ))
            self.assertEqual(run.call_args.kwargs['env']['CLAUDE_CODE_DISABLE_AUTO_MEMORY'], '1')
        with patch.object(p, '_claude_cli', return_value=('fixture-claude', ['fixture-claude', 1, 1], None)), \
                patch.object(p.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, '--session-id', '')) as run:
            self.assertTrue(p.claude_takes_session_id(self.root / 'cli-cache.json', environ={
                **os.environ, 'CLAUDE_CODE_DISABLE_AUTO_MEMORY': '0'})[0])
            self.assertEqual(run.call_args.kwargs['env']['CLAUDE_CODE_DISABLE_AUTO_MEMORY'], '1')

    def test_negative_control_removing_memory_switch_fails_the_same_assertion(self):
        self.assert_memory_off(p)
        control = without(p, "    result['CLAUDE_CODE_DISABLE_AUTO_MEMORY'] = '1'\n", '')
        # Run the same assertion on one stage without subTest, so the expected
        # AssertionError is caught here rather than consumed by unittest.
        options = control.configuration('implement', self.config, os.environ)
        child = control.claude_env(options, {'CLAUDE_CODE_DISABLE_AUTO_MEMORY': '0'})
        with caught_mutation() as caught:
            self.assertEqual(child.get('CLAUDE_CODE_DISABLE_AUTO_MEMORY'), '1', 'auto-memory guard missing')
        self.assertIsInstance(caught[0], AssertionError)
        self.assertIn('auto-memory guard missing', str(caught[0]))


if __name__ == '__main__': unittest.main()
