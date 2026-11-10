#!/bin/bash
# Check fragment isolation, release output and malformed-input controls without services.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PYTHONDONTWRITEBYTECODE=1 python3 - "$ROOT" <<'PY'
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(sys.argv.pop()).resolve()
sys.path.insert(0, str(ROOT/'tests'))
import git_maintenance

UNRELEASED = ('## [Unreleased]\n\n'
              'Pending changes are recorded in [changes/unreleased/](changes/unreleased/).\n\n')
RELEASED = '## [1.0.0] - 2026-01-01\n\n### Fixed\n\n- Existing entry.\n'
BASELINE = '# Changelog\n\n' + UNRELEASED + RELEASED


class ChangelogTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='.changelog-test-', dir=ROOT)
        self.addCleanup(temp.cleanup)
        self.repo = Path(temp.name)/'repository'
        self.env = dict(os.environ, GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL=os.devnull,
                        **git_maintenance.OFF_ENV)
        subprocess.run(['git', 'init', '--template=', '-q', str(self.repo)],
                       check=True, capture_output=True, env=self.env, timeout=10)
        git_maintenance.disable(self.repo)
        self.git('config', 'user.name', 'Changelog Fixture')
        self.git('config', 'user.email', 'fixture@example.invalid')
        self.write('CHANGELOG.md', BASELINE)
        self.write('changes/unreleased/.gitkeep', '')
        self.script = self.repo/'scripts/changelog.py'
        self.script.parent.mkdir()
        shutil.copy2(ROOT/'scripts/changelog.py', self.script)

    def git(self, *args):
        return subprocess.run(['git', '-C', str(self.repo), *args], check=True,
                              text=True, capture_output=True, env=self.env, timeout=10).stdout.strip()

    def write(self, name, text):
        path = self.repo/name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(text.encode('utf-8') if isinstance(text, str) else text)
        return path

    def fragment(self, name='valid', section='Fixed', entry='Valid entry.'):
        return self.write('changes/unreleased/'+name+'.md', f'section: {section}\n\n- {entry}\n')

    def commit(self):
        self.git('add', '-A')
        self.git('-c', 'commit.gpgSign=false', 'commit', '-qm', 'fixture')
        return self.git('rev-parse', 'HEAD')

    def command(self, *args, expected=0):
        result = subprocess.run([sys.executable, '-B', '-I', str(self.script), *args],
                                cwd=self.repo, env=self.env, text=True, capture_output=True, timeout=10)
        self.assertEqual(expected, result.returncode, result.stdout+result.stderr)
        return result.stdout+result.stderr

    def faulted_release(self, code, expected=2):
        code = ("import os, runpy, sys\n"
                "from pathlib import Path\n"
                "from unittest.mock import patch\n"
                "sys.argv = [sys.argv[1], 'release', '1.1.0', '2026-02-02']\n" + code)
        result = subprocess.run([sys.executable, '-B', '-I', '-c', code, str(self.script)],
                                cwd=self.repo, env=self.env, text=True, capture_output=True, timeout=10)
        if expected is None:
            self.assertNotEqual(0, result.returncode, result.stdout+result.stderr)
        else:
            self.assertEqual(expected, result.returncode, result.stdout+result.stderr)
        return result.stderr

    def release_fixture(self):
        first = self.write('changes/unreleased/a-fixed.md',
                           'section: Fixed\n\n- First entry.\n  Continued details.')
        first.chmod(0o640)
        self.fragment('b-added', 'Added', 'Second entry.')
        self.fragment('c-fixed', 'Fixed', 'Third entry.')
        expected = BASELINE.replace(RELEASED,
                                    '## [1.1.0] - 2026-02-02\n\n'
                                    '### Added\n\n- Second entry.\n\n'
                                    '### Fixed\n\n- First entry.\n  Continued details.\n\n'
                                    '- Third entry.\n\n' + RELEASED)
        return first, expected

    def assert_released(self, expected):
        self.command('release', '1.1.0', '2026-02-02')
        self.assertEqual(expected, (self.repo/'CHANGELOG.md').read_text())
        self.assertEqual(['.gitkeep'], [p.name for p in (self.repo/'changes/unreleased').iterdir()])
        self.command('check')

    def snapshot(self):
        return {str(path.relative_to(self.repo)): path.read_bytes()
                for path in self.repo.rglob('*') if path.is_file()
                and '.git' not in path.relative_to(self.repo).parts}

    def test_two_branches_merge_in_either_order(self):
        base = self.commit()
        for branch, section in (('alpha', 'Fixed'), ('beta', 'Added')):
            self.git('checkout', '-qb', branch, base)
            self.fragment(branch, section, branch+' entry.')
            self.commit()
        expected = ('## [Unreleased]\n\n### Added\n\n- beta entry.\n\n'
                    '### Fixed\n\n- alpha entry.\n\n')
        for first, second in (('alpha', 'beta'), ('beta', 'alpha')):
            with self.subTest(first=first):
                self.git('checkout', '-qb', 'merge-'+first, first)
                self.git('-c', 'commit.gpgSign=false', 'merge', '--no-ff', '--no-edit', second)
                before = self.snapshot()
                self.assertEqual(expected, self.command('render'))
                self.command('check')
                self.assertEqual(before, self.snapshot())
                self.assertEqual(BASELINE, (self.repo/'CHANGELOG.md').read_text())

    def test_release_matches_expected_file_and_refuses_duplicate(self):
        for name, section, entry in (
                ('z-added', 'Added', 'Added last.'), ('security', 'Security', 'Security entry.'),
                ('a-added', 'Added', 'Added first.'), ('changed', 'Changed', 'Changed entry.'),
                ('removed', 'Removed', 'Removed entry.'), ('fixed', 'Fixed', 'Fixed entry.'),
                ('docs', 'Documentation', 'Documentation entry.'),
                ('deprecated', 'Deprecated', 'Deprecated entry.')):
            self.fragment(name, section, entry)
        expected = self.write('expected.md', '# Changelog\n\n' + UNRELEASED +
                              '## [1.1.0] - 2026-02-02\n\n'
                              '### Added\n\n- Added first.\n\n- Added last.\n\n'
                              '### Changed\n\n- Changed entry.\n\n'
                              '### Deprecated\n\n- Deprecated entry.\n\n'
                              '### Removed\n\n- Removed entry.\n\n'
                              '### Fixed\n\n- Fixed entry.\n\n'
                              '### Security\n\n- Security entry.\n\n'
                              '### Documentation\n\n- Documentation entry.\n\n' + RELEASED)
        self.command('release', '1.1.0', '2026-02-02')
        self.assertEqual(expected.read_bytes(), (self.repo/'CHANGELOG.md').read_bytes())
        self.assertEqual(['.gitkeep'], [p.name for p in (self.repo/'changes/unreleased').iterdir()])
        self.command('check')
        before = self.snapshot()
        self.assertIn('already exists', self.command('release', '1.1.0', '2026-02-02', expected=1))
        self.assertEqual(before, self.snapshot())
        self.fragment()
        before = self.snapshot()
        self.assertIn('already exists', self.command('release', '1.1.0', '2026-02-02', expected=1))
        self.assertEqual(before, self.snapshot())

    def test_empty_release_and_invalid_arguments_leave_no_changes(self):
        before = self.snapshot()
        self.assertIn('no changelog fragments', self.command('release', '1.1.0', '2026-02-02', expected=1))
        self.assertEqual(before, self.snapshot())
        self.fragment()
        for args in (('release', '1.1.0', '2026-2-02'), ('release', '1.1.0', '2026-02-30'),
                     ('release', 'bad\nversion', '2026-02-02'), ('release', '1.1.0'), ('unknown',)):
            with self.subTest(args=args):
                before = self.snapshot()
                self.assertIn('error:', self.command(*args, expected=2))
                self.assertEqual(before, self.snapshot())

    def test_failed_atomic_replace_preserves_inputs_and_cleans_temporary_file(self):
        first, expected = self.release_fixture()
        before = self.snapshot()
        output = self.faulted_release(
            "with patch('os.replace', side_effect=OSError('injected replace failure')):\n"
            "    runpy.run_path(sys.argv[0], run_name='__main__')\n")
        self.assertIn('injected replace failure', output)
        self.assertEqual(before, self.snapshot())
        self.assertEqual(0o640, first.stat().st_mode & 0o777)
        self.command('check')
        self.assert_released(expected)

    def test_failed_temporary_write_leaves_fragments_unstaged(self):
        first, expected = self.release_fixture()
        before = self.snapshot()
        output = self.faulted_release(
            "with patch('os.fsync', side_effect=OSError('injected write failure')), "
            "patch('os.rename') as move:\n"
            "    try:\n"
            "        runpy.run_path(sys.argv[0], run_name='__main__')\n"
            "    finally:\n"
            "        move.assert_not_called()\n")
        self.assertIn('injected write failure', output)
        self.assertEqual(before, self.snapshot())
        self.assertEqual(0o640, first.stat().st_mode & 0o777)
        self.command('check')
        self.assert_released(expected)

    def test_failed_fragment_move_restores_inputs_and_allows_retry(self):
        first, expected = self.release_fixture()
        before = self.snapshot()
        output = self.faulted_release(
            "rename = os.rename\n"
            "moved = []\n"
            "def fail_second_fragment(source, destination):\n"
            "    if source.parent.name == 'unreleased':\n"
            "        if moved:\n"
            "            raise OSError('injected fragment move failure')\n"
            "        rename(source, destination)\n"
            "        moved.append(source.name)\n"
            "        return\n"
            "    return rename(source, destination)\n"
            "with patch('os.rename', fail_second_fragment):\n"
            "    try:\n"
            "        runpy.run_path(sys.argv[0], run_name='__main__')\n"
            "    finally:\n"
            "        assert moved == ['a-fixed.md'], moved\n")
        self.assertIn('injected fragment move failure', output)
        self.assertEqual(before, self.snapshot())
        self.assertEqual(0o640, first.stat().st_mode & 0o777)
        self.command('check')
        self.assert_released(expected)

    def test_interruption_after_one_move_restores_inputs_and_allows_retry(self):
        first, expected = self.release_fixture()
        before = self.snapshot()
        output = self.faulted_release(
            "rename = os.rename\n"
            "moved = []\n"
            "def interrupt_after_move(source, destination):\n"
            "    rename(source, destination)\n"
            "    if source.parent.name == 'unreleased':\n"
            "        moved.append(source.name)\n"
            "        raise KeyboardInterrupt('injected interruption')\n"
            "with patch('os.rename', interrupt_after_move):\n"
            "    try:\n"
            "        runpy.run_path(sys.argv[0], run_name='__main__')\n"
            "    finally:\n"
            "        assert moved == ['a-fixed.md'], moved\n", expected=None)
        self.assertIn('injected interruption', output)
        self.assertEqual(before, self.snapshot())
        self.assertEqual(0o640, first.stat().st_mode & 0o777)
        self.command('check')
        self.assert_released(expected)

    def test_interruption_after_replace_preserves_release(self):
        for cleanup_failure in (False, True):
            with self.subTest(cleanup_failure=cleanup_failure):
                self.write('CHANGELOG.md', BASELINE)
                _, expected = self.release_fixture()
                cleanup_patch = (", patch('shutil.rmtree', side_effect=OSError('injected cleanup failure'))"
                                 if cleanup_failure else '')
                output = self.faulted_release(
                    "replace = os.replace\n"
                    "def interrupt_after_replace(source, destination):\n"
                    "    replace(source, destination)\n"
                    "    raise KeyboardInterrupt('injected post-replacement interruption')\n"
                    "with patch('os.replace', interrupt_after_replace)" + cleanup_patch + ":\n"
                    "    runpy.run_path(sys.argv[0], run_name='__main__')\n",
                    expected=2 if cleanup_failure else None)
                self.assertIn('injected post-replacement interruption' if not cleanup_failure
                              else 'injected cleanup failure', output)
                self.assertEqual(expected, (self.repo/'CHANGELOG.md').read_text())
                self.assertEqual(['.gitkeep'],
                                 [p.name for p in (self.repo/'changes/unreleased').iterdir()])
                self.assertEqual([], list(self.repo.glob('.changelog-*')))
                staging = list((self.repo/'changes').glob('.release-*'))
                if cleanup_failure:
                    self.assertEqual(1, len(staging))
                    self.assertIn('Release staging remains: '+str(staging[0]), output)
                    diagnostic = self.command('check', expected=1)
                    self.assertIn('version 1.1.0 is already in CHANGELOG.md', diagnostic)
                    self.assertIn('staged files are released and may be deleted', diagnostic)
                    shutil.rmtree(staging[0])
                else:
                    self.assertEqual([], staging)
                self.command('check')

    def test_failed_restoration_retains_original_and_continues_restoring(self):
        first, expected = self.release_fixture()
        before = self.snapshot()
        output = self.faulted_release(
            "rename = os.rename\n"
            "attempted = []\n"
            "def fail_first_restore(source, destination):\n"
            "    if source.parent.name.startswith('.release-'):\n"
            "        attempted.append(source.name)\n"
            "        if source.name == 'a-fixed.md':\n"
            "            raise OSError('injected restore failure')\n"
            "    return rename(source, destination)\n"
            "with patch('os.rename', fail_first_restore), "
            "patch('os.replace', side_effect=OSError('injected replace failure')):\n"
            "    try:\n"
            "        runpy.run_path(sys.argv[0], run_name='__main__')\n"
            "    finally:\n"
            "        assert attempted == ['a-fixed.md', 'b-added.md', 'c-fixed.md'], attempted\n")
        staging, = (self.repo/'changes').glob('.release-*')
        self.assertIn('injected restore failure', output)
        self.assertIn('Release staging remains: '+str(staging), output)
        self.assertIn('\n  a-fixed.md\n', output)
        retained = dict(before)
        retained[str((staging/first.name).relative_to(self.repo))] = retained.pop(
            str(first.relative_to(self.repo)))
        self.assertEqual(retained, self.snapshot())
        self.assertEqual(0o640, (staging/first.name).stat().st_mode & 0o777)
        diagnostic = self.command('check', expected=1)
        self.assertIn(str(staging.relative_to(self.repo)), diagnostic)
        self.assertIn('version 1.1.0 is not in CHANGELOG.md', diagnostic)
        self.assertIn('move staged files back to changes/unreleased/', diagnostic)
        (staging/first.name).rename(first)
        staging.rmdir()
        self.assertEqual(before, self.snapshot())
        self.command('check')
        self.assert_released(expected)

    def test_failed_staging_cleanup_leaves_correct_changelog_and_reports_remaining_files(self):
        _, expected = self.release_fixture()
        before = self.snapshot()
        output = self.faulted_release(
            "def fail_cleanup(staging):\n"
            "    (staging/'a-fixed.md').unlink()\n"
            "    raise OSError('injected cleanup failure')\n"
            "with patch('shutil.rmtree', fail_cleanup):\n"
            "    runpy.run_path(sys.argv[0], run_name='__main__')\n")
        staging, = (self.repo/'changes').glob('.release-*')
        self.assertIn('injected cleanup failure', output)
        self.assertIn('Release staging remains: '+str(staging), output)
        for name in ('b-added.md', 'c-fixed.md'):
            self.assertIn('\n  '+name+'\n', output)
        retained = dict(before)
        retained['CHANGELOG.md'] = expected.encode('utf-8')
        for name in ('a-fixed.md', 'b-added.md', 'c-fixed.md'):
            original = retained.pop('changes/unreleased/'+name)
            if name != 'a-fixed.md':
                retained[str((staging/name).relative_to(self.repo))] = original
        self.assertEqual(retained, self.snapshot())
        self.assertEqual(expected, (self.repo/'CHANGELOG.md').read_text())
        self.assertIn(str(staging.relative_to(self.repo)), self.command('check', expected=1))
        shutil.rmtree(staging)
        self.command('check')
        before = self.snapshot()
        self.assertIn('already exists', self.command('release', '1.1.0', '2026-02-02', expected=1))
        self.assertEqual(before, self.snapshot())

    def test_empty_staging_directory_blocks_check_until_removed(self):
        self.command('check')
        staging = self.repo/'changes/.release-incomplete'
        staging.mkdir()
        self.assertIn(str(staging.relative_to(self.repo)), self.command('check', expected=1))
        self.assertTrue(staging.is_dir())
        staging.rmdir()
        self.command('check')

    def test_direct_unreleased_entry_fails_until_removed(self):
        self.command('check')
        self.write('CHANGELOG.md', BASELINE.replace(UNRELEASED, UNRELEASED+'- Direct entry.\n\n'))
        self.assertIn('CHANGELOG.md: Unreleased', self.command('check', expected=1))
        self.write('CHANGELOG.md', BASELINE)
        self.command('check')

    def test_invalid_fragments_pass_after_each_defect_is_removed(self):
        valid = 'section: Fixed\n\n- Valid entry.\n'
        for name, text, diagnostic in (
                ('unknown.md', 'section: Other\n\n- Entry.\n', 'first line'),
                ('blank.md', 'section: Fixed\n- Entry.\n', 'blank line'),
                ('two.md', valid+'- Second entry.\n', 'one non-empty'),
                ('Bad_name.md', valid, 'slug'),
                ('empty.md', '', 'first line'),
                ('empty-item.md', 'section: Fixed\n\n-\n', 'one non-empty'),
                ('utf8.md', b'section: Fixed\n\n- \xff\n', 'utf-8'),
                ('space.md', valid.replace('entry.', 'entry. '), 'trailing whitespace'),
                ('tab.md', valid.replace('entry.', 'entry.\t'), 'trailing whitespace'),
                ('end.md', valid+'\n', 'blank lines at end'),
                ('nested.md', valid+'  - Nested item.\n', 'one non-empty')):
            with self.subTest(name=name):
                self.command('check')
                path = self.write('changes/unreleased/'+name, text)
                before = self.snapshot()
                output = self.command('check', expected=1)
                self.assertIn('changes/unreleased/'+name, output)
                self.assertIn(diagnostic, output)
                self.assertEqual(before, self.snapshot())
                path.unlink()
                self.write('changes/unreleased/valid.md', valid)
                self.command('check')
                (self.repo/'changes/unreleased/valid.md').unlink()
                self.command('check')

    def test_indented_continuation_is_preserved(self):
        self.write('changes/unreleased/wrapped.md',
                   'section: Fixed\n\n- A wrapped entry.\n  Validation continues here.\n')
        self.command('check')
        self.assertIn('- A wrapped entry.\n  Validation continues here.\n', self.command('render'))

    def test_real_repository_passes(self):
        result = subprocess.run([sys.executable, '-B', '-I', str(ROOT/'scripts/changelog.py'), 'check'],
                                cwd=ROOT, env=self.env, text=True, capture_output=True, timeout=10)
        self.assertEqual(0, result.returncode, result.stdout+result.stderr)


unittest.main()
PY
