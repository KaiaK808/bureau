"""Real worktrees and fake package-manager executables; no registry access."""
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
from unittest.mock import patch

import git_maintenance

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = Path(os.environ.get('BUREAU_TEST_SCRIPTS_DIR', ROOT / 'templates/scripts')).resolve()
FAKE = '''#!/usr/bin/env python3
import json, os, pathlib, subprocess, sys, time
root = pathlib.Path.cwd()
with open(os.environ.get('MANAGER_CALLS', root.parent / 'calls.jsonl'), 'a') as output:
    output.write(json.dumps(dict(tool=pathlib.Path(sys.argv[0]).name, argv=sys.argv[1:], cwd=str(root),
        network=os.environ.get('COREPACK_ENABLE_NETWORK'), ignore_path=os.environ.get('YARN_IGNORE_PATH'),
        environment=sorted(os.environ))) + '\\n')
if sys.argv[1:] == ['--version']:
    print(os.environ.get('FAKE_YARN_VERSION', '1.22.22')); sys.exit(0)
if os.environ.get('FAKE_EMPTY'): sys.exit(0)
if os.environ.get('FAKE_SLOW'):
    child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])
    with open(os.environ['MANAGER_CHILD'], 'a') as output: output.write(str(child.pid) + '\\n')
    time.sleep(60)
if os.environ.get('FAKE_FAIL'):
    (root / 'node_modules/partial').mkdir(parents=True, exist_ok=True)
    (root / 'packages/app/node_modules/partial').mkdir(parents=True, exist_ok=True)
    print('cache entry missing', file=sys.stderr); sys.exit(1)
(root / 'node_modules').mkdir(exist_ok=True)
link = root / 'node_modules/lib'
if not link.is_symlink(): link.symlink_to('../packages/lib')
(root / 'packages/app/node_modules').mkdir(parents=True, exist_ok=True)
link = root / 'packages/app/node_modules/lib'
if not link.is_symlink(): link.symlink_to('../../lib')
if os.environ.get('FAKE_MUTATE'):
    with (root / 'package.json').open('a') as output: output.write(' ')
'''


class PackageManagerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='bureau package managers ')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.main = self.root / 'main'; self.main.mkdir()
        self.repo = self.root / 'worker with spaces'
        self.bin = self.root / 'bin'; self.bin.mkdir()
        self.home = self.root / 'home'; self.home.mkdir()
        self.calls = self.root / 'calls.jsonl'
        self.child = self.root / 'child.pid'
        self.env = dict(os.environ, **git_maintenance.OFF_ENV,
                        HOME=str(self.home),
                        PATH=str(self.bin) + os.pathsep + os.environ['PATH'],
                        MANAGER_CALLS=str(self.calls), MANAGER_CHILD=str(self.child))
        for name in ('npm', 'pnpm', 'yarn'):
            path = self.bin / name; path.write_text(FAKE); path.chmod(0o755)
        source = (SCRIPTS / 'bureau-config.sh').read_text()
        start = source.index('restore_worktree_deps() {')
        end = source.index('\n}', start) + 2
        self.function = self.root / 'restore.sh'
        self.function.write_text(source[start:end] + '\n')
        self.git(self.main, 'init', '-q', '-b', 'main')
        self.git(self.main, *git_maintenance.OFF_CONFIG)
        self.git(self.main, 'config', 'user.name', 'Test')
        self.git(self.main, 'config', 'user.email', 'test@example.invalid')

    def git(self, repo, *args):
        return subprocess.run(['git', '-C', str(repo), *args], env=self.env, capture_output=True,
                              text=True, check=True).stdout.strip()

    def setup_manager(self, manager):
        (self.main / 'package.json').write_text(json.dumps(dict(name='workspace', private=True,
            packageManager=manager + ('@9.0.0' if manager == 'pnpm' else '@1.22.22'),
            workspaces=['packages/*'])))
        (self.main / '.gitignore').write_text('node_modules\n')
        for name in ('app', 'lib'):
            folder = self.main / 'packages' / name; folder.mkdir(parents=True)
            (folder / 'package.json').write_text(json.dumps(dict(name=name, version='1.0.0')))
        lock = 'pnpm-lock.yaml' if manager == 'pnpm' else 'yarn.lock'
        (self.main / lock).write_text('lockfileVersion: 9\n' if manager == 'pnpm' else '# yarn lockfile v1\n')
        if manager == 'pnpm':
            (self.main / 'pnpm-workspace.yaml').write_text('packages:\n  - packages/*\n')
        self.git(self.main, 'add', '-A'); self.git(self.main, 'commit', '-q', '-m', 'initial')
        (self.main / 'node_modules').mkdir()
        (self.main / 'node_modules/main-only').write_text('preserve')
        self.git(self.main, 'worktree', 'add', '-q', '--detach', str(self.repo), 'main')

    def restore(self, repo=None, **env):
        script = 'set -euo pipefail\nsource "$1"\nsource "$2/bureau-env.sh"\n_BUREAU_SCRIPTS_DIR="$2"\nrestore_worktree_deps "$3"\n'
        return subprocess.run(['/bin/bash', '-c', script, 'restore', str(self.function), str(SCRIPTS), str(repo or self.repo)],
                              env=dict(self.env, **env), capture_output=True, text=True, timeout=30)

    def invocations(self, installs=False):
        values = [json.loads(line) for line in self.calls.read_text().splitlines()] if self.calls.exists() else []
        return [value for value in values if value['argv'][0] == 'install'] if installs else values

    def assert_installed(self, manager):
        result = self.restore()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        calls = self.invocations(installs=True)
        self.assertEqual(len(calls), 1, 'expected a package-manager install, not an npm-only no-op')
        call = calls[0]
        self.assertEqual(call['tool'], manager); self.assertEqual(call['cwd'], str(self.repo))
        for flag in ('--frozen-lockfile', '--offline', '--ignore-scripts'):
            self.assertIn(flag, call['argv'])
        self.assertEqual(call['network'], '0')
        if manager == 'pnpm':
            self.assertIn('--prefer-offline', call['argv']); self.assertIn('--ignore-pnpmfile', call['argv'])
        else:
            self.assertIn('--non-interactive', call['argv']); self.assertEqual(call['ignore_path'], '1')
        self.assertEqual((self.repo / 'node_modules/lib').resolve(), self.repo / 'packages/lib')
        self.assertEqual((self.repo / 'packages/app/node_modules/lib').resolve(), self.repo / 'packages/lib')
        self.assertTrue((self.main / 'node_modules/main-only').exists())
        self.assertFalse((self.repo / 'node_modules/main-only').exists())
        self.assertIn('installed with ' + manager, result.stdout)

    def test_pnpm_detection_installs_in_worktree_with_safe_flags_and_relative_links(self):
        self.setup_manager('pnpm'); self.assert_installed('pnpm')

    def test_yarn_detection_installs_in_worktree_with_safe_flags_and_relative_links(self):
        self.setup_manager('yarn'); self.assert_installed('yarn')

    def assert_environment_filtered(self, manager, mode):
        self.setup_manager(manager)
        config = self.root / 'operator.json'
        config.write_text(json.dumps({'repo': {'untrusted_env': mode}}))
        startup = self.root / 'startup.sh'; startup.write_text('# fixture only\n')
        # Record the helper's Git children too. No environment values are logged.
        real_git = shutil.which('git', path=self.env['PATH'])
        git_env = self.root / 'git-env.jsonl'
        fake_git = self.bin / 'git'
        fake_git.write_text('#!/usr/bin/env python3\nimport json, os, sys\n'
                            f'with open({str(git_env)!r}, "a") as output:\n'
                            '    output.write(json.dumps(sorted(os.environ)) + "\\n")\n'
                            f'os.execv({real_git!r}, [{real_git!r}, *sys.argv[1:]])\n')
        fake_git.chmod(0o755)
        result = self.restore(GH_TOKEN='fixture-dependency-token',
                              TOKEN_CARRIER='Bearer fixture-dependency-token',
                              _beb_name='reserved-helper-sentinel', BASH_ENV=str(startup), ENV=str(startup),
                              BUREAU_CONFIG=str(config), BUREAU_ENV_FILE=str(self.root / 'private.env'),
                              HERDR_PANE_ID='operator-pane', APPLICATION_SETTING='ordinary-setting')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        calls = self.invocations()
        self.assertEqual([call['argv'][0] for call in calls],
                         ['install'] if manager == 'pnpm' else ['--version', 'install'])
        forbidden = {'GH_TOKEN', 'TOKEN_CARRIER', '_beb_name', 'BASH_ENV', 'ENV',
                     'BUREAU_CONFIG', 'BUREAU_ENV_FILE', 'HERDR_PANE_ID'}
        for call in calls:
            self.assertFalse(forbidden.intersection(call['environment']),
                             'unfiltered variables reached package manager: ' + str(call['environment']))
            self.assertEqual('APPLICATION_SETTING' in call['environment'], mode == 'default')
            self.assertIn('PATH', call['environment'])
        environments = [json.loads(line) for line in git_env.read_text().splitlines()]
        self.assertGreaterEqual(len(environments), 4)
        for environment in environments:
            self.assertFalse(forbidden.intersection(environment),
                             'unfiltered variables reached helper Git: ' + str(environment))
            self.assertEqual('APPLICATION_SETTING' in environment, mode == 'default')

    def test_pnpm_environment_filters_secrets_and_reserved_names(self):
        self.assert_environment_filtered('pnpm', 'default')

    def test_yarn_environment_filters_secrets_and_reserved_names(self):
        self.assert_environment_filtered('yarn', 'default')

    def test_pnpm_environment_honors_clean_mode(self):
        self.assert_environment_filtered('pnpm', 'clean')

    def test_yarn_environment_honors_clean_mode(self):
        self.assert_environment_filtered('yarn', 'clean')

    def test_environment_negative_control_detects_bypassed_filter(self):
        source = self.function.read_text()
        filtered = 'bureau_untrusted_env python3 -I '
        self.assertIn(filtered, source)
        self.function.write_text(source.replace(filtered, 'python3 -I ', 1))
        with self.assertRaisesRegex(AssertionError, 'unfiltered variables reached package manager'):
            self.assert_environment_filtered('pnpm', 'default')

    def test_invalid_environment_mode_refuses_before_starting_the_helper(self):
        self.setup_manager('pnpm')
        config = self.root / 'operator.json'
        config.write_text('{"repo":{"untrusted_env":"invalid"}}')
        result = self.restore(BUREAU_CONFIG=str(config))
        self.assertEqual(result.returncode, 24)
        self.assertIn('repo.untrusted_env', result.stderr)
        self.assertEqual(self.invocations(), [])
        self.assertFalse((self.main / '.git/bureau-deps').exists())

    def test_stamp_skips_and_workspace_manifest_change_invalidates_it(self):
        self.setup_manager('pnpm'); self.assert_installed('pnpm')
        self.assertIn('unchanged, skipped', self.restore().stdout)
        self.assertEqual(len(self.invocations(installs=True)), 1)
        package = self.repo / 'packages/lib/package.json'
        package.write_text(package.read_text() + '\n')
        self.assertEqual(self.restore().returncode, 0)
        self.assertEqual(len(self.invocations(installs=True)), 2)
        self.assertTrue(list((self.main / '.git/bureau-deps').glob('*.json')))
        self.assertFalse(list(self.repo.glob('**/bureau-deps')))

    def test_lock_and_workspace_configuration_changes_invalidate_stamp(self):
        self.setup_manager('pnpm'); self.assert_installed('pnpm')
        for name in ('pnpm-lock.yaml', 'pnpm-workspace.yaml'):
            path = self.repo / name; path.write_text(path.read_text() + '\n')
            self.assertEqual(self.restore().returncode, 0)
        self.assertEqual(len(self.invocations(installs=True)), 3)

    def test_dependency_free_install_can_succeed_without_modules_but_declared_dependencies_cannot(self):
        self.setup_manager('pnpm')
        result = self.restore(FAKE_EMPTY='1')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('unchanged, skipped', self.restore(FAKE_EMPTY='1').stdout)
        self.assertEqual(len(self.invocations(installs=True)), 1)
        package = self.repo / 'packages/lib/package.json'
        manifest = json.loads(package.read_text()); manifest['dependencies'] = {'dependency': '1.0.0'}
        package.write_text(json.dumps(manifest))
        result = self.restore(FAKE_EMPTY='1')
        self.assertEqual(result.returncode, 24)
        self.assertEqual(len(self.invocations(installs=True)), 3)

    def test_failed_installs_are_two_attempts_with_diagnostics_and_no_partial_modules(self):
        self.setup_manager('pnpm')
        result = self.restore(FAKE_FAIL='1')
        self.assertEqual(result.returncode, 24, result.stdout + result.stderr)
        self.assertEqual(len(self.invocations(installs=True)), 2)
        self.assertIn('cache entry missing', result.stdout)
        for name in ('node_modules', 'packages/app/node_modules'):
            self.assertFalse((self.repo / name).exists())
        self.assertFalse(list((self.main / '.git/bureau-deps').glob('*.json')))

    def test_yarn_failure_has_the_same_bound_and_cleanup(self):
        self.setup_manager('yarn')
        result = self.restore(FAKE_FAIL='1')
        self.assertEqual(result.returncode, 24)
        self.assertEqual(len(self.invocations(installs=True)), 2)
        self.assertFalse((self.repo / 'packages/app/node_modules').exists())

    def test_modern_yarn_is_refused_before_installing(self):
        self.setup_manager('yarn')
        result = self.restore(FAKE_YARN_VERSION='4.5.0')
        self.assertEqual(result.returncode, 24)
        self.assertIn('Yarn Classic 1.x', result.stderr)
        self.assertEqual(self.invocations(installs=True), [])

    def assert_yarn_pnp_refused(self, **env):
        result = self.restore(**env)
        self.assertEqual(result.returncode, 24, result.stdout + result.stderr)
        self.assertIn('Yarn PnP is unsupported', result.stderr)
        self.assertEqual(self.invocations(), [], 'PnP must be refused before any Yarn process')
        self.assertFalse((self.repo / 'node_modules').exists())
        self.assertFalse((self.repo / 'packages/app/node_modules').exists())
        self.assertTrue((self.main / 'node_modules/main-only').exists())

    def test_yarn_classic_manifest_pnp_is_refused_even_without_dependencies(self):
        self.setup_manager('yarn')
        package = self.repo / 'package.json'
        manifest = json.loads(package.read_text()); manifest['installConfig'] = {'pnp': True}
        package.write_text(json.dumps(manifest))
        self.assert_yarn_pnp_refused(FAKE_EMPTY='1')

    def test_yarn_pnp_loader_files_are_refused(self):
        self.setup_manager('yarn')
        for name in ('.pnp.js', '.pnp.cjs', '.pnp.loader.mjs', '.pnp.data.json'):
            with self.subTest(name=name):
                path = self.repo / name; path.touch()
                self.assert_yarn_pnp_refused()
                path.unlink()

    def test_yarn_pnp_loader_symlink_is_refused(self):
        self.setup_manager('yarn')
        (self.repo / '.pnp.cjs').symlink_to('missing-loader')
        self.assert_yarn_pnp_refused()

    def test_yarn_classic_rc_pnp_options_are_refused(self):
        self.setup_manager('yarn')
        rc = self.repo / '.yarnrc'
        for option in ('pnp', '--pnp', '--enable-pnp', '--install.pnp', '"--install.enable-pnp"', 'plugnplay'):
            with self.subTest(option=option):
                rc.write_text(option + ' true\n')
                self.assert_yarn_pnp_refused()

    def test_yarn_pnp_in_inherited_rc_files_is_refused(self):
        self.setup_manager('yarn')
        for parent in (self.root, self.home):
            with self.subTest(parent=parent):
                rc = parent / '.yarnrc'; rc.write_text('--install.pnp true\n')
                self.assert_yarn_pnp_refused()
                rc.unlink()

    def test_yarn_pnp_ambiguous_switch_values_fail_closed(self):
        self.setup_manager('yarn')
        rc = self.repo / '.yarnrc'
        for value in ('no', 'null', '0', 'FALSE'):
            with self.subTest(value=value):
                rc.write_text('pnp ' + value + '\n')
                self.assert_yarn_pnp_refused()
                rc.unlink()
                self.assert_yarn_pnp_refused(YARN_PNP=value)

    def test_yarn_pnp_linker_configuration_is_refused(self):
        self.setup_manager('yarn')
        (self.repo / '.yarnrc.yml').write_text('nodeLinker: "pnp" # explicit PnP\n')
        self.assert_yarn_pnp_refused()

    def test_yarn_pnp_environment_switches_are_refused(self):
        self.setup_manager('yarn')
        for name in ('YARN_PNP', 'YARN_ENABLE_PNP', 'YARN_PLUGNPLAY', 'NODE_OPTIONS'):
            with self.subTest(name=name):
                value = '--require /fixture/.pnp.cjs' if name == 'NODE_OPTIONS' else 'true'
                self.assert_yarn_pnp_refused(**{name: value})

    def test_yarn_pnp_cannot_reuse_a_matching_stamp(self):
        self.setup_manager('yarn'); self.assert_installed('yarn')
        self.calls.unlink()
        (self.repo / '.pnp.cjs').touch()
        self.assert_yarn_pnp_refused()

    def test_yarn_classic_disabled_pnp_configuration_is_allowed(self):
        self.setup_manager('yarn')
        package = self.repo / 'package.json'
        manifest = json.loads(package.read_text()); manifest['installConfig'] = {'pnp': False}
        package.write_text(json.dumps(manifest))
        (self.repo / '.yarnrc').write_text('# pnp true\npnp false\n--install.pnp false\n')
        self.assert_installed('yarn')

    def test_multiple_lockfiles_and_package_manager_mismatch_fail_closed(self):
        self.setup_manager('pnpm')
        (self.repo / 'package-lock.json').write_text('{}')
        result = self.restore(); self.assertEqual(result.returncode, 24)
        self.assertIn('multiple package-manager lockfiles', result.stderr)
        (self.repo / 'package-lock.json').unlink()
        (self.repo / 'package.json').write_text('{"packageManager":"yarn@1.22.22"}')
        result = self.restore(); self.assertEqual(result.returncode, 24)
        self.assertIn('packageManager disagrees', result.stderr)
        self.assertEqual(self.invocations(), [])

    def test_main_checkout_is_untouched(self):
        self.setup_manager('pnpm')
        self.assertEqual(self.restore(self.main).returncode, 0)
        self.assertEqual(self.invocations(), [])
        self.assertEqual((self.main / 'node_modules/main-only').read_text(), 'preserve')

    def test_primary_checkout_with_an_external_git_directory_is_untouched(self):
        self.setup_manager('pnpm')
        # Move the metadata outside the primary checkout, preserving the linked
        # worker's gitdir pointer; primary detection must not guess .git's parent.
        external = self.root / 'metadata'
        (self.main / '.git').rename(external)
        (self.main / '.git').write_text('gitdir: ' + str(external) + '\n')
        result = self.restore(self.main)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.invocations(), [])
        self.assertEqual((self.main / 'node_modules/main-only').read_text(), 'preserve')

    def test_symlinked_and_tracked_modules_cannot_reuse_a_matching_stamp(self):
        self.setup_manager('pnpm'); self.assert_installed('pnpm')
        outside = self.root / 'outside'; outside.mkdir(); (outside / 'keep').write_text('keep')
        import shutil
        shutil.rmtree(self.repo / 'node_modules')
        (self.repo / 'node_modules').symlink_to(outside)
        self.assertEqual(self.restore().returncode, 0)
        self.assertFalse((self.repo / 'node_modules').is_symlink())
        self.assertEqual((outside / 'keep').read_text(), 'keep')
        (self.repo / 'node_modules/injected').write_text('untrusted')
        self.git(self.repo, 'add', '-f', 'node_modules/injected')
        self.assertEqual(self.restore().returncode, 0)
        self.assertFalse((self.repo / 'node_modules/injected').exists())
        self.assertEqual(len(self.invocations(installs=True)), 3)

    def test_mutated_manifests_are_not_stamped_or_left_with_dependencies(self):
        self.setup_manager('pnpm')
        result = self.restore(FAKE_MUTATE='1')
        self.assertEqual(result.returncode, 24)
        self.assertIn('changed a manifest or lockfile', result.stderr)
        self.assertFalse((self.repo / 'node_modules').exists())

    def test_timeout_kills_the_installers_child_process(self):
        self.setup_manager('pnpm')
        spec = importlib.util.spec_from_file_location('deps', SCRIPTS / 'bureau-worktree-deps.py')
        deps = importlib.util.module_from_spec(spec); spec.loader.exec_module(deps)
        # Real sleep/child; shorten only the timeout, not the production runner.
        with patch.dict(os.environ, dict(self.env, FAKE_SLOW='1')), patch.object(deps, 'INSTALL_TIMEOUT', .3):
            try:
                self.assertEqual(deps.restore(self.repo), 24)
                pids = [int(value) for value in self.child.read_text().splitlines()]
                self.assertEqual(len(pids), 2)
                for pid in pids:
                    deadline = time.monotonic() + 3
                    while time.monotonic() < deadline:
                        try: os.kill(pid, 0)
                        except ProcessLookupError: break
                        # Linux CI may briefly retain a killed orphan as a
                        # zombie. It cannot write and is already stopped.
                        if sys.platform.startswith('linux'):
                            try:
                                state = Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()[0]
                            except FileNotFoundError:
                                break
                            if state == 'Z':
                                break
                        time.sleep(.05)
                    else: self.fail('installer child still exists: ' + str(pid))
            finally:
                if self.child.exists():
                    for value in self.child.read_text().splitlines():
                        try: os.kill(int(value), signal.SIGKILL)
                        except ProcessLookupError: pass


if __name__ == '__main__':
    unittest.main(argv=[sys.argv[0], *sys.argv[1:]])
