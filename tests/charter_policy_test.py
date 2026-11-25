#!/usr/bin/env python3
"""Pure-Python charter checks; filesystem fixtures, no child processes or services."""
import copy
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import types
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / 'templates/scripts/bureau-charter.py'
spec = importlib.util.spec_from_file_location('charter', SOURCE)
charter = importlib.util.module_from_spec(spec)
spec.loader.exec_module(charter)


def config(**policy):
    return dict(agents=dict(merge_charter=policy))


class PolicyTests(unittest.TestCase):
    def test_defaults_and_valid_declarations(self):
        self.assertIsNone(charter.policy_from_config({}))
        self.assertEqual(charter.policy_from_config(config()), charter.DEFAULTS)
        for queue in (None, dict(label='approved-for-merge'), dict(cycle_id='12345678-1234-1234-1234-123456789abc')):
            value = config(enabled=True, approved_queue=queue)
            value['agents']['merge_charter']['exceptions'] = [dict(
                {'class': 'security'}, paths=['auth/**', 'src/*/A?.py']), {'class': 'data', 'labels': ['schema-change']}]
            value['agents']['merge_charter'].update(
                suite=dict(id='full', command='bash scripts/check.sh', definition_files=['scripts/check.sh']),
                red_main=dict(workflows=['.github/workflows/ci.yml']), report=True)
            parsed = charter.policy_from_config(value)
            self.assertEqual(parsed['red_main']['poll_seconds'], 60)
            self.assertEqual(parsed['red_main']['timeout_seconds'], 3600)
            self.assertEqual(parsed['approved_queue'], queue)

    def test_invalid_shapes_including_disabled(self):
        cases = [None, False, [], 'enabled', {'unknown': False}]
        cases += [dict(enabled=v) for v in (None, 0, 1, 'false', [], {})]
        cases += [dict(report=v) for v in (None, 0, 'true', [])]
        cases += [dict(approved_queue=v) for v in (True, [], '', {}, {'label': 'yes', 'cycle_id': 'x'},
                  {'label': ''}, {'label': ' '}, {'label': 1}, {'label': []}, {'label': 'yes', 'other': 1},
                  {'cycle_id': ''}, {'cycle_id': 'not-a-uuid'})]
        cases += [dict(exceptions=v) for v in (None, True, {}, '', [None], [True], [[]],
                  [{}], [{'class': ''}], [{'class': ' '}], [{'class': 1}], [{'class': 'x'}],
                  [{'class': 'x', 'paths': [], 'labels': []}], [{'class': 'x', 'paths': None}],
                  [{'class': 'x', 'paths': 'src/**'}], [{'class': 'x', 'labels': 'sensitive'}],
                  [{'class': 'x', 'labels': [True]}], [{'class': 'x', 'labels': ['']}],
                  [{'class': 'x', 'paths': ['src/**'], 'extra': False}],
                  [{'class': 'x', 'labels': ['a']}, {'class': 'x', 'labels': ['b']}])]
        cases += [dict(suite=v) for v in (False, [], '', {}, {'id': 'full'},
                  {'id': '', 'command': 'true', 'definition_files': ['test.sh']},
                  {'id': 'full', 'command': '', 'definition_files': ['test.sh']},
                  {'id': 'full', 'command': ['true'], 'definition_files': ['test.sh']},
                  {'id': 'full', 'command': 'true', 'definition_files': []},
                  {'id': 'full', 'command': 'true', 'definition_files': ['*.sh']},
                  {'id': 'full', 'command': 'true', 'definition_files': ['test.sh'], 'extra': 1})]
        cases += [dict(red_main=v) for v in (False, [], '', {}, {'workflows': []}, {'workflows': 'ci.yml'},
                  {'workflows': ['ci.yml']}, {'workflows': ['.github/workflows/*.yml']},
                  {'workflows': ['.github/workflows/sub/ci.yml']}, {'workflows': ['.github/workflows/ci.txt']},
                  {'workflows': ['.github/workflows/ci.yml'], 'poll_seconds': 0},
                  {'workflows': ['.github/workflows/ci.yml'], 'poll_seconds': True},
                  {'workflows': ['.github/workflows/ci.yml'], 'timeout_seconds': '60'},
                  {'workflows': ['.github/workflows/ci.yml'], 'timeout_seconds': 1},
                  {'workflows': ['.github/workflows/ci.yml'], 'extra': False})]
        cases += [dict(report=True), dict(report=True, suite=dict(id='full', command='true', definition_files=['test.sh']))]
        for raw in cases:
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                charter.policy_from_config(dict(agents=dict(merge_charter=raw)))
        for value in (None, [], {'agents': []}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                charter.policy_from_config(value)

    def test_paths_are_root_relative_and_only_declared_glob_syntax(self):
        invalid = ['', ' ', '/auth/**', '//host/a', 'C:/auth/*', 'C:auth', '\\auth',
                   '~/auth', '../auth', 'auth/../data', 'auth/./data', 'auth//data', 'auth/',
                   '!auth/**', ':(exclude)auth/**', 'auth/!private', 'auth/[ab]', 'auth/{a,b}', 'auth\\**', 'auth/\nsecret']
        for path in invalid:
            with self.subTest(path=path), self.assertRaises(ValueError):
                charter.policy_from_config(config(exceptions=[{'class': 'security', 'paths': [path]}]))
        for path in ('auth/**', '**/tokens?.json', 'src/*/A.py', 'Config.json', 'docs/file name.md'):
            parsed = charter.policy_from_config(config(exceptions=[{'class': 'files', 'paths': [path]}]))
            self.assertEqual(parsed['exceptions'][0]['paths'][0], path)

    def test_duplicate_json_keys_and_selectors(self):
        for raw in ('{"agents":{},"agents":{}}', '{"x":NaN}', '{"x":Infinity}'):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                charter.read_json(raw)
        for entry in ({'class': 'x', 'paths': ['a', 'a']}, {'class': 'x', 'labels': ['a', 'a']}):
            with self.assertRaises(ValueError):
                charter.policy_from_config(config(exceptions=[entry]))

    def test_digest_is_of_normalized_policy(self):
        first = charter.policy_from_config(config())
        second = charter.policy_from_config(config(**copy.deepcopy(charter.DEFAULTS)))
        self.assertEqual(charter.digest(first), charter.digest(second))
        second['enabled'] = True
        self.assertNotEqual(charter.digest(first), charter.digest(second))

    def test_negative_controls_remove_real_schema_guards(self):
        source = SOURCE.read_text()
        controls = [
            ("if type(policy[key]) is not bool:", 'if False:', config(enabled='false')),
            ('if len(queue) != 1:', 'if False:', config(approved_queue={'label': 'yes', 'cycle_id': '12345678-1234-1234-1234-123456789abc'})),
            ('if name in classes:', 'if False:', config(exceptions=[{'class': 'x', 'labels': ['a']}, {'class': 'x', 'labels': ['b']}])),
            ('if not paths and not labels:', 'if False:', config(exceptions=[{'class': 'x'}])),
            ("relative_path(v, w, glob=True)", 'text(v, w)', config(exceptions=[{'class': 'x', 'paths': ['../auth']}]))]
        for old, new, value in controls:
            def observation(module):
                with self.assertRaises(ValueError):
                    module.policy_from_config(value)
            with self.subTest(guard=old):
                observation(charter)  # Unchanged baseline must pass first.
                self.assertEqual(source.count(old), 1)
                mutant = types.ModuleType('charter_mutant')
                mutant.__file__ = str(SOURCE)
                exec(compile(source.replace(old, new), '<charter mutation>', 'exec'), mutant.__dict__)
                with self.assertRaises(AssertionError):
                    observation(mutant)


class EnrollmentSafetyTests(unittest.TestCase):
    """Real filesystem state; Git and service process boundaries never execute."""
    def setUp(self):
        scratch = tempfile.TemporaryDirectory(prefix='bureau-charter-policy-', dir=tempfile.gettempdir())
        self.addCleanup(scratch.cleanup)
        self.root = Path(scratch.name)
        self.common = self.root / 'repository' / '.git'
        self.common.mkdir(parents=True)
        self.real_common_dir = charter.common_dir
        common = patch.object(charter, 'common_dir', return_value=self.common)
        common.start()
        self.addCleanup(common.stop)
        processes = patch.object(charter.subprocess, 'run', side_effect=AssertionError('unexpected process'))
        self.process = processes.start()
        self.addCleanup(processes.stop)

    def record(self, common=None):
        path = (common or self.common) / 'bureau/charter/enrollment.json'
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps(dict(version=1, repository='owner/repository', default_branch='main',
                                       default_commit='a' * 40, policy_digest='b' * 64)))
        return path

    def command(self, operation, *, repo=None, local=False):
        output = io.StringIO()
        arguments = ['--repo', str(repo or self.common.parent), operation]
        if local:
            arguments.append('--local')
        with redirect_stdout(output):
            code = charter.main(arguments)
        return code, output.getvalue().strip()

    def metadata(self, gitdir=None):
        gitdir = gitdir or self.common
        gitdir.mkdir(parents=True, exist_ok=True)
        (gitdir / 'HEAD').write_text('ref: refs/heads/main\n')
        for name in ('objects', 'refs'):
            (gitdir / name).mkdir(exist_ok=True)

    def test_non_enrolled_local_probe_adds_no_git_or_service_call(self):
        self.metadata()
        # Use the real Git boundary: reverting the local path would start Git.
        with patch.object(charter, 'common_dir', self.real_common_dir):
            for policy in (None, {'enabled': False}, {'enabled': True}):
                if policy is not None:
                    (self.common.parent / '.bureau.json').write_text(json.dumps(config(**policy)))
                with self.subTest(policy=policy):
                    # Initial probe plus both gate evaluations must stay process-free.
                    for _ in range(3):
                        self.assertEqual(self.command('status', local=True), (0, 'not-enrolled'))
        self.process.assert_not_called()
        self.assertFalse((self.common / 'bureau').exists())
        old = 'common = local_common_dir(args.repo) if args.local else None'
        source = SOURCE.read_text()
        self.assertEqual(source.count(old), 1)
        mutant = types.ModuleType('charter_lookup_mutant')
        mutant.__file__ = str(SOURCE)
        exec(compile(source.replace(old, 'common = None'), '<charter lookup mutation>', 'exec'), mutant.__dict__)
        # Restoring the old Git lookup must trip the denied process boundary.
        with redirect_stdout(io.StringIO()), self.assertRaisesRegex(AssertionError, 'unexpected process'):
            mutant.main(['--repo', str(self.common.parent), 'status', '--local'])

    def test_local_probe_resolves_linked_and_separate_git_directories(self):
        self.metadata()
        self.record()
        for layout in ('linked-relative', 'linked-absolute', 'separate'):
            checkout = self.root / ('checkout with spaces ' + layout)
            checkout.mkdir()
            if layout.startswith('linked'):
                gitdir = self.common / 'worktrees' / layout
                gitdir.mkdir(parents=True)
                (gitdir / 'HEAD').write_text('a' * 40 + '\n')
                shared = '../..' if layout.endswith('relative') else str(self.common)
                (gitdir / 'commondir').write_text(shared + '\n')
            else:
                gitdir = self.common
            target = str(gitdir) if layout.endswith('absolute') else os.path.relpath(gitdir, checkout)
            (checkout / '.git').write_text('gitdir: ' + target + '\n')
            nested = checkout / 'nested'
            nested.mkdir()
            with self.subTest(layout=layout):
                for repo in (checkout, nested):
                    self.assertEqual(self.command('status', repo=repo, local=True), (0, 'enrolled'))
        self.process.assert_not_called()

    def test_local_probe_keeps_enrollment_parent_and_record_validation(self):
        self.metadata()
        path = self.record()
        for parent in (self.common, path.parent.parent, path.parent):
            with self.subTest(parent=parent.name):
                parent.chmod(0o600)
                try:
                    self.assertEqual(self.command('status', local=True)[0], 25)
                finally:
                    parent.chmod(0o700)
        path.write_text('{broken')
        self.assertEqual(self.command('status', local=True)[0], 25)
        path.unlink()
        path.parent.chmod(0o600)
        try:
            self.assertEqual(self.command('status', local=True)[0], 25)
        finally:
            path.parent.chmod(0o700)
        path.parent.rmdir()
        other = self.root / 'other charter'
        other.mkdir()
        path.parent.symlink_to(other, target_is_directory=True)
        self.assertEqual(self.command('status', local=True)[0], 25)
        self.process.assert_not_called()

    def test_local_probe_rejects_indeterminate_git_metadata(self):
        checkout = self.root / 'uncertain checkout'
        checkout.mkdir()
        marker = checkout / '.git'
        self.assertEqual(self.command('status', repo=checkout, local=True)[0], 25)
        for value in ('invalid\n', 'gitdir: \n', 'gitdir: missing\n', 'gitdir: missing\nextra\n'):
            with self.subTest(marker=value):
                marker.write_text(value)
                self.assertEqual(self.command('status', repo=checkout, local=True)[0], 25)
        marker.unlink()
        marker.symlink_to(checkout / 'missing')
        self.assertEqual(self.command('status', repo=checkout, local=True)[0], 25)
        marker.unlink()
        marker.mkdir()
        self.assertEqual(self.command('status', repo=checkout, local=True)[0], 25)
        self.metadata(marker)
        shared = marker / 'commondir'
        for value in ('', 'missing\n', 'missing\nextra\n'):
            with self.subTest(commondir=value):
                shared.write_text(value)
                self.assertEqual(self.command('status', repo=checkout, local=True)[0], 25)
        shared.unlink()
        shared.symlink_to(checkout / 'missing')
        self.assertEqual(self.command('status', repo=checkout, local=True)[0], 25)
        self.process.assert_not_called()

    def test_status_distinguishes_absence_from_enrollment(self):
        self.assertEqual(self.command('status'), (0, 'not-enrolled'))
        path = self.record()
        self.assertEqual(self.command('status'), (0, 'enrolled'))
        path.write_text('{broken')
        code, message = self.command('status')
        self.assertEqual(code, 25)
        self.assertIn('invalid', message)
        self.process.assert_not_called()

    def test_non_searchable_parent_is_unusable_even_without_a_record(self):
        path = self.record()
        for directory in (path.parent, path.parent.parent):
            with self.subTest(parent=directory.name):
                directory.chmod(0o600)
                try:
                    code, _ = self.command('status')
                    self.assertEqual(code, 25)
                    with self.assertRaises((ValueError, OSError)):
                        charter.enrollment(self.common.parent)
                finally:
                    directory.chmod(0o700)
        path.unlink()
        path.parent.chmod(0o600)
        try:
            self.assertEqual(self.command('status')[0], 25)
        finally:
            path.parent.chmod(0o700)

    def test_failed_or_empty_common_directory_lookup_is_unusable(self):
        # Exercise the real common_dir implementation while only Git's result is fake.
        for result in (subprocess.CompletedProcess([], 128, b'', b'failed'),
                       subprocess.CompletedProcess([], 0, b'', b'')):
            self.process.side_effect = None
            self.process.return_value = result
            with self.subTest(returncode=result.returncode), patch.object(charter, 'common_dir', self.real_common_dir):
                self.assertEqual(self.command('status')[0], 25)
        self.assertFalse((self.common / 'bureau').exists())

    def test_unenroll_refuses_symlinked_parent_without_touching_other_repository(self):
        other = self.root / 'other repository' / '.git'
        other.mkdir(parents=True)
        target = self.record(other)
        original = target.read_bytes()
        bureau = self.common / 'bureau'
        bureau.mkdir()
        for link, destination in ((bureau / 'charter', target.parent), (bureau, target.parent.parent)):
            with self.subTest(parent=link.name):
                if link == bureau:
                    bureau.rmdir()
                link.symlink_to(destination, target_is_directory=True)
                try:
                    code, message = self.command('unenroll')
                    self.assertEqual(code, 25)
                    self.assertIn('symlink', message)
                    self.assertEqual(target.read_bytes(), original)
                    self.assertTrue(link.is_symlink())
                finally:
                    link.unlink()

    def test_unenroll_can_remove_a_corrupt_regular_record(self):
        path = self.record()
        path.write_text('{broken')
        self.assertEqual(self.command('unenroll')[0], 0)
        self.assertFalse(path.exists())

    def test_fetch_uses_the_existing_hardened_remote_runner(self):
        sha = 'a' * 40
        self.process.side_effect = [subprocess.CompletedProcess([], 0, b'', b''),
                                    subprocess.CompletedProcess([], 0, (sha + '\n').encode(), b'')]
        with patch.dict(os.environ, BASH_ENV='untrusted-startup', ENV='untrusted-startup'):
            charter.GitHub(self.common.parent, 'owner/repository').fetch(sha)
        arguments, options = self.process.call_args_list[0]
        command = arguments[0]
        self.assertEqual(command[:3], ['bash', '--noprofile', '--norc'])
        self.assertIn('source "$1"', command[4])
        self.assertIn('_bureau_remote_git_hooks_mode()', command[4])
        self.assertIn('git "$@"', command[4])
        self.assertEqual(Path(command[6]), SOURCE.with_name('bureau-env.sh'))
        self.assertEqual(command[7:], ['-C', str(self.common.parent), 'fetch', '--no-tags', '--no-write-fetch-head', 'origin', sha])
        self.assertNotIn('BASH_ENV', options['env'])
        self.assertNotIn('ENV', options['env'])


if __name__ == '__main__':
    unittest.main()
