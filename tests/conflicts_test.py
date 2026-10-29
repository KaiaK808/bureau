"""Real local Git through the production merge hook; no remote service calls."""
import errno
import importlib.util
import json
import os
from pathlib import Path
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = Path(os.environ.get('BUREAU_CONFLICT_TEST_SCRIPTS', ROOT / 'templates/scripts'))


class ConflictTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='bureau-conflict-test-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.repo = self.root / 'owned checkout'; self.repo.mkdir()
        self.config = self.root / 'configuration.json'
        self.comments = self.root / 'linear-comments'
        self.env = {**os.environ, 'BUREAU_CONFIG': str(self.config), 'BUREAU_ENV_FILE': str(self.root / 'no-env'),
                    'PYTHONDONTWRITEBYTECODE': '1', 'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_GLOBAL': '/dev/null'}
        for key in ('BUREAU_DRY_RUN', 'BUREAU_RUN_ID', 'BUREAU_STAGE_ATTEMPT_ID', 'BUREAU_ACTIVE_ENTRY', 'BUREAU_CALLER_STOP'):
            self.env.pop(key, None)
        self.git('init', '-q', '-b', 'main')
        self.git('config', 'user.name', 'Bureau fixture'); self.git('config', 'user.email', 'fixture@bureau')
        self.write('.gitignore', '.cache/\nignored.txt\n')
        self.write('ignored.txt', 'private ignored sentinel\n')
        self.write('source.txt', 'first=base\n' + ''.join('context %s\n' % i for i in range(20)) + 'last=base\n')
        self.write('CHANGELOG.md', '# Changes\n\n## Unreleased\n\n## Released\n- Old entry\n')
        self.write('render.py', '''from pathlib import Path
source = Path('source.txt').read_text() + Path('CHANGELOG.md').read_text()
for name in ('docs/site/page.html', 'docs/site/index.html'):
    path = Path(name); path.parent.mkdir(parents=True, exist_ok=True); path.write_text('rendered:' + source)
''')
        self.write('check.py', '''from pathlib import Path
expected = 'rendered:' + Path('source.txt').read_text() + Path('CHANGELOG.md').read_text()
assert all(Path(name).read_text() == expected for name in ('docs/site/page.html', 'docs/site/index.html'))
''')
        self.write('docs/site/page.html', 'base output\n'); self.write('docs/site/index.html', 'base index\n')
        self.commit('foundation')
        self.git('branch', 'feature')
        self.git('init', '--bare', '-q', str(self.root / 'origin.git'))
        self.git('remote', 'add', 'origin', str(self.root / 'origin.git'))
        self.generated = [{'globs': ['docs/site/*.html'], 'command': [sys.executable, 'render.py']}]
        self.settings = {'repo': {'test_command': f'{sys.executable} check.py',
                                  'conflict_resolution': {'generated': self.generated, 'append_only_paths': []}}}

    def write(self, name, text):
        path = self.repo / name; path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

    def git(self, *args, check=True):
        result = subprocess.run(['git', '-C', str(self.repo), *args], env=self.env, capture_output=True)
        if check and result.returncode: self.fail(result.stderr.decode(errors='replace'))
        return result.stdout.decode(errors='surrogateescape').strip()

    def commit(self, name):
        self.git('add', '-A'); self.git('commit', '-qm', name)

    def diverge(self, generated=True, append=False, source_conflict=False, path='CHANGELOG.md'):
        for branch, side in (('feature', 'ours'), ('main', 'theirs')):
            self.git('checkout', '-q', branch)
            text = (self.repo / 'source.txt').read_text()
            field = 'first' if source_conflict or side == 'ours' else 'last'
            self.write('source.txt', text.replace(field + '=base', field + '=' + side))
            if generated:
                self.write('docs/site/page.html', side + ' stale output\n')
                self.write('docs/site/index.html', side + ' stale index\n')
            if append:
                text = (self.repo / path).read_text()
                self.write(path, text.replace('## Unreleased\n', '## Unreleased\n- ' + side + ' entry\n'))
            self.commit(side)
        self.git('push', '-q', 'origin', 'main')
        self.base = self.git('rev-parse', 'main')
        self.git('checkout', '-q', 'feature')
        self.head = self.git('rev-parse', 'HEAD')

    def invoke(self, pinned=False, asynchronous=False):
        self.config.write_text(json.dumps(self.settings))
        script = '''set -euo pipefail
source "$1/bureau-config.sh"
add_issue_label() { :; }
post_comment() { printf '%s\\n' "$2" >> "$FAKE_LINEAR_COMMENTS"; }
_throttle_should_suppress() { return 1; }
_throttle_record() { :; }
'''
        # Services are faked at the transport boundary; the helper, Git and
        # executor environment policy are the real production implementation.
        script += 'if [ "$3" = pinned ]; then merge_origin_main_or_abort EXP-1747 Test "$2" || exit 17; else merge_origin_main_or_abort EXP-1747 Test || exit 17; fi\n'
        command = ['/bin/bash', '-c', script, 'fixture', str(SCRIPTS), self.base, 'pinned' if pinned else 'fetch']
        env = {**self.env, 'FAKE_LINEAR_COMMENTS': str(self.comments)}
        if asynchronous:
            return subprocess.Popen(command, cwd=self.repo, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    text=True, start_new_session=True)
        self.result = subprocess.run(command, cwd=self.repo, env=env, capture_output=True, text=True)
        return self.result.returncode

    def success(self, pinned=False):
        self.assertEqual(self.invoke(pinned), 0, self.result.stdout + self.result.stderr)
        self.assertEqual(self.git('rev-list', '--parents', '-1', 'HEAD').split()[1:], [self.head, self.base])
        self.assertEqual(self.git('status', '--porcelain', '--untracked-files=all'), '')
        self.assertFalse(self.comments.exists())
        self.assertEqual((self.repo / 'ignored.txt').read_text(), 'private ignored sentinel\n')

    def refused(self, verify_index=False):
        before = self.git('status', '--porcelain', '--untracked-files=all')
        if verify_index:
            index = Path(self.git('rev-parse', '--git-path', 'index'))
            if not index.is_absolute(): index = self.repo / index
            saved_index = index.read_bytes()
        self.assertEqual(self.invoke(), 17, self.result.stdout + self.result.stderr)
        # Check before git status legitimately refreshes cached file stats.
        if verify_index: self.assertEqual(index.read_bytes(), saved_index)
        self.assertEqual(self.git('rev-parse', 'HEAD'), self.head)
        self.assertEqual(self.git('status', '--porcelain', '--untracked-files=all'), before)
        self.assertEqual(self.git('ls-files', '-u'), '')
        self.assertFalse((self.repo / '.git/MERGE_HEAD').exists())
        self.assertTrue(self.comments.exists())
        self.assertEqual((self.repo / 'ignored.txt').read_text(), 'private ignored sentinel\n')

    def test_generated_only(self):
        self.diverge(); self.success()
        self.assertIn('first=ours', (self.repo / 'docs/site/page.html').read_text())
        self.assertIn('last=theirs', (self.repo / 'docs/site/page.html').read_text())

    def test_disabled_resolver_allows_six_character_setext_heading(self):
        heading = 'Summary\n======\n'
        resolved = heading + 'Both intended changes retained.\n'
        self.write('notes.md', heading + 'base\n')
        self.write('.gitattributes', 'notes.md conflict-marker-size=5\n')
        self.commit('heading foundation'); self.git('branch', '-f', 'feature', 'main')
        for branch, side in [('feature', 'ours'), ('main', 'theirs')]:
            self.git('checkout', '-q', branch); self.write('notes.md', heading + side + '\n')
            self.commit(side)
        self.base = self.git('rev-parse', 'main')
        self.git('checkout', '-q', 'feature'); self.head = self.git('rev-parse', 'HEAD')
        self.settings['agents'] = {'conflict_resolver': {'enabled': False}}
        self.settings['repo'] = {'test_command': shlex.join([sys.executable, '-c',
            "from pathlib import Path; assert Path('notes.md').read_text() == " + repr(resolved)]),
            'conflict_resolution': {'generated': [{'globs': ['notes.md'], 'command': [sys.executable, '-c',
                "from pathlib import Path; Path('notes.md').write_text(" + repr(resolved) + ")"]}]}}
        self.success(pinned=True)
        self.assertEqual((self.repo / 'notes.md').read_text(), resolved)

    def test_append_only_under_unreleased(self):
        self.diverge(generated=False, append=True)
        self.settings['repo'].update(test_command="grep -q 'ours entry' CHANGELOG.md && grep -q 'theirs entry' CHANGELOG.md",
                                     conflict_resolution={'append_only_paths': ['CHANGELOG.md']})
        self.success()
        text = (self.repo / 'CHANGELOG.md').read_text()
        self.assertLess(text.index('ours entry'), text.index('## Released'))
        self.assertLess(text.index('theirs entry'), text.index('## Released'))

    def test_concurrent_added_fixed_sections_are_refused(self):
        # review1's exact corruption: --union puts C under ours' Fixed and
        # repeats Fixed, despite retaining both side sequences in order.
        for branch, entries in (('feature', ('A', 'B')), ('main', ('C', 'D'))):
            self.git('checkout', '-q', branch)
            text = (self.repo / 'CHANGELOG.md').read_text()
            block = '### Added\n- %s\n### Fixed\n- %s\n' % entries
            self.write('CHANGELOG.md', text.replace('## Unreleased\n', '## Unreleased\n' + block))
            self.commit(branch + ' sections')
        self.git('push', '-q', 'origin', 'main'); self.base = self.git('rev-parse', 'main')
        self.git('checkout', '-q', 'feature'); self.head = self.git('rev-parse', 'HEAD')
        # Exercise Git's union itself so this remains the exact review repro,
        # including the misplaced C entry and duplicated Fixed heading.
        ancestor = self.git('merge-base', self.head, self.base)
        inputs = []
        for stage, ref in (('ours', self.head), ('base', ancestor), ('theirs', self.base)):
            path = self.root / ('union-' + stage)
            path.write_bytes(subprocess.check_output(
                ['git', '-C', str(self.repo), 'show', ref + ':CHANGELOG.md'], env=self.env))
            inputs.append(str(path))
        union = subprocess.check_output(['git', 'merge-file', '-p', '--union', '--', *inputs], env=self.env)
        self.assertEqual(union, b'# Changes\n\n## Unreleased\n### Added\n- A\n### Fixed\n- B\n- C\n### Fixed\n- D\n\n## Released\n- Old entry\n')
        self.settings['repo'].update(test_command='true', conflict_resolution={'append_only_paths': ['CHANGELOG.md']})
        self.refused()

    def test_concurrent_shared_insertion_context_is_refused(self):
        for branch, side in (('feature', 'ours'), ('main', 'theirs')):
            self.git('checkout', '-q', branch)
            text = (self.repo / 'CHANGELOG.md').read_text()
            self.write('CHANGELOG.md', text.replace('## Unreleased\n',
                       '## Unreleased\nNew context\n- ' + side + '\nEnd context\n'))
            self.commit(side + ' context')
        self.git('push', '-q', 'origin', 'main'); self.base = self.git('rev-parse', 'main')
        self.git('checkout', '-q', 'feature'); self.head = self.git('rev-parse', 'HEAD')
        self.settings['repo'].update(test_command='true', conflict_resolution={'append_only_paths': ['CHANGELOG.md']})
        self.refused()

    def test_concurrent_heading_and_parent_section_entry_are_refused(self):
        # Even disjoint new lines can move an entry from Unreleased into a
        # heading introduced only by the other side. Decline that ambiguity.
        for branch, block in (('feature', '### Added\n- A\n'), ('main', '- C\n')):
            self.git('checkout', '-q', branch)
            text = (self.repo / 'CHANGELOG.md').read_text()
            self.write('CHANGELOG.md', text.replace('## Unreleased\n', '## Unreleased\n' + block))
            self.commit(branch + ' section entry')
        self.git('push', '-q', 'origin', 'main'); self.base = self.git('rev-parse', 'main')
        self.git('checkout', '-q', 'feature'); self.head = self.git('rev-parse', 'HEAD')
        self.settings['repo'].update(test_command='true', conflict_resolution={'append_only_paths': ['CHANGELOG.md']})
        self.refused()

    def test_combined_sources_precede_regeneration(self):
        self.diverge(append=True)
        self.settings['repo']['conflict_resolution']['append_only_paths'] = ['CHANGELOG.md']
        self.success()
        for side in ('ours', 'theirs'): self.assertIn(side + ' entry', (self.repo / 'docs/site/page.html').read_text())

    def test_append_repair_also_regenerates_nonconflicted_outputs(self):
        self.diverge(generated=False, append=True)
        self.settings['repo']['conflict_resolution']['append_only_paths'] = ['CHANGELOG.md']
        self.success()
        for side in ('ours', 'theirs'): self.assertIn(side + ' entry', (self.repo / 'docs/site/page.html').read_text())

    def test_clean_merge_requires_no_repair_check(self):
        self.diverge(generated=False)
        self.settings['repo'].pop('test_command')
        self.generated[0]['command'] = ['bureau-not-invoked-on-clean-merge']
        self.success()

    def test_pinned_base_is_not_refetched(self):
        self.diverge()
        self.git('update-ref', 'refs/remotes/origin/main', self.head)
        self.success(pinned=True)

    def test_append_only_unusual_filename(self):
        name = 'notes/Ä changes\twith\nnewline.md'
        self.write(name, (self.repo / 'CHANGELOG.md').read_text()); self.commit('unusual filename')
        self.git('branch', '-f', 'feature', 'HEAD')
        self.diverge(generated=False, append=True, path=name)
        self.settings['repo'].update(test_command='true', conflict_resolution={'append_only_paths': [name]})
        self.success()
        for side in ('ours', 'theirs'): self.assertIn(side + ' entry', (self.repo / name).read_text())

    def test_append_filename_is_not_a_git_pathspec(self):
        name = ':(literal)missing.md'
        self.write(name, (self.repo / 'CHANGELOG.md').read_text()); self.commit('literal pathspec filename')
        self.git('branch', '-f', 'feature', 'HEAD')
        self.diverge(generated=False, append=True, path=name)
        self.settings['repo'].update(test_command='true', conflict_resolution={'append_only_paths': [name]})
        self.success()
        for side in ('ours', 'theirs'): self.assertIn(side + ' entry', (self.repo / name).read_text())

    def test_generated_unusual_filename_and_argv(self):
        name = 'docs/site/- Ä\tline\npage.html'
        self.write(name, 'base\n'); self.commit('unusual generated filename'); self.git('branch', '-f', 'feature', 'HEAD')
        self.diverge()
        self.generated[0]['command'] = [sys.executable, '-c', 'import runpy,sys; from pathlib import Path; runpy.run_path("render.py"); Path(sys.argv[1]).write_text("fresh\\n")', name]
        self.success()
        self.assertEqual((self.repo / name).read_text(), 'fresh\n')

    def test_recursive_globs_and_new_parent_directories(self):
        self.diverge(); self.generated[0]['globs'] = ['docs/**/*.html']
        self.generated[0]['command'] = [sys.executable, '-c',
            'import runpy; from pathlib import Path; runpy.run_path("render.py"); p=Path("docs/new/deep/extra.html"); p.parent.mkdir(parents=True); p.write_text("fresh\\n")']
        self.success(); self.assertEqual((self.repo / 'docs/new/deep/extra.html').read_text(), 'fresh\n')

    def test_unsupported_source_aborts_before_generator(self):
        self.diverge(source_conflict=True)
        self.generated[0]['command'] = [sys.executable, '-c', f'from pathlib import Path; Path({str(self.root / "ran")!r}).touch()']
        self.refused(); self.assertFalse((self.root / 'ran').exists())

    def test_generator_failure_rolls_back(self):
        self.diverge()
        self.generated[0]['command'] = [sys.executable, '-c', 'from pathlib import Path; Path("ignored.txt").write_text("lost"); Path("created/sub").mkdir(parents=True); Path("created/sub/leftover").touch(); raise SystemExit(3)']
        self.refused(); self.assertFalse((self.repo / 'created').exists())

    def test_validation_cleanup_failure_cannot_record_passed(self):
        spec = importlib.util.spec_from_file_location('conflict_validation_fault', SCRIPTS / 'bureau-conflicts.py')
        runtime = importlib.util.module_from_spec(spec); spec.loader.exec_module(runtime)
        for fault in ('cleanup', 'live_descendant', 'inspection_unavailable', 'empty_inspection'):
            with self.subTest(fault=fault):
                transaction = runtime.Transaction(self.repo, self.settings, self.git('rev-parse', 'HEAD'), self.config)
                transaction.branch = 'main'
                transaction.checkpoint = mock.Mock()
                budget, record = mock.Mock(), {}
                transaction.reservations = [(budget, record)]
                child = mock.Mock(pid=123456, returncode=0)
                child.poll.return_value = child.wait.return_value = 0
                original_popen = subprocess.Popen
                def popen(*args, **kwargs):
                    if args[0][0] == '/bin/bash': return child
                    return original_popen(*args, **kwargs)
                # The real outside_gate returns a determined zero, then its
                # group cleanup fails; the caller must reject the transaction.
                inspection = (dict(side_effect=runtime.provider.pg.TerminationUncertain('inspection unavailable'))
                              if fault == 'inspection_unavailable' else dict(return_value=set() if fault == 'empty_inspection' else {child.pid}))
                with mock.patch.object(runtime.provider.subprocess, 'Popen', side_effect=popen), \
                        mock.patch.object(runtime.provider.os, 'killpg', side_effect=OSError('cleanup failed') if fault == 'cleanup'
                                          else PermissionError('group EPERM')), \
                        mock.patch.object(runtime.provider.pg, 'inspect_groups', **inspection):
                    with self.assertRaises(runtime.Declined) as declined:
                        transaction.validate('fixture', self.root)
                self.assertEqual(budget.finish.call_args.kwargs, dict(check_outcome='failed', check_exit=25))
                self.assertIn('outside gate termination uncertain', str(declined.exception))

    def test_uncertain_validation_keeps_merge_index_and_worktree(self):
        self.diverge()
        self.config.write_text(json.dumps(self.settings))
        spec = importlib.util.spec_from_file_location('conflict_hold_fault', SCRIPTS / 'bureau-conflicts.py')
        runtime = importlib.util.module_from_spec(spec); spec.loader.exec_module(runtime)
        transaction = runtime.Transaction(self.repo, self.settings, self.git('rev-parse', 'refs/remotes/origin/main'), self.config)
        transaction.checkpoint = mock.Mock()
        outside = runtime.provider.outside_gate
        child = mock.Mock(pid=123456, returncode=0)
        child.poll.return_value = child.wait.return_value = 0
        def uncertain(*args):
            original_popen = subprocess.Popen
            def popen(*popen_args, **kwargs):
                if popen_args[0][0] == '/bin/bash': return child
                return original_popen(*popen_args, **kwargs)
            with mock.patch.object(runtime.provider.subprocess, 'Popen', side_effect=popen), \
                    mock.patch.object(runtime.provider.os, 'killpg', side_effect=PermissionError('EPERM')):
                return outside(*args)
        with mock.patch.object(runtime.provider, 'outside_gate', side_effect=uncertain):
            with self.assertRaises(runtime.ProviderStop) as held: transaction.execute()
        self.assertEqual(held.exception.code, 25)
        self.assertTrue(self.git('rev-parse', '--verify', 'MERGE_HEAD'))
        self.assertTrue(self.git('diff', '--cached', '--name-only'))
        self.assertTrue(runtime.provider.pg.process_holds(self.repo / '.git/bureau', inactive_only=True))

    def test_uncertain_validation_preserves_stops_when_metadata_writes_fail(self):
        spec = importlib.util.spec_from_file_location('conflict_stop_fault', SCRIPTS / 'bureau-conflicts.py')
        runtime = importlib.util.module_from_spec(spec); spec.loader.exec_module(runtime)
        for code, abort in ((0, None), (124, 'timeout'), (130, 'kill')):
            with self.subTest(code=code):
                transaction = runtime.Transaction(self.repo, self.settings, 'unused', self.config)
                transaction.identity = mock.Mock(return_value='before')
                transaction.checkpoint = mock.Mock()
                transaction.git = mock.Mock(side_effect=AssertionError('uncertain tree must not be read'))
                budget = mock.Mock()
                budget.finish.side_effect = OSError('coordination write failed')
                transaction.reservations = [(budget, {})]
                failure = runtime.provider.GateExecutionError(runtime.provider.pg.TerminationUncertain('EPERM'), (code, abort))
                with mock.patch.object(runtime.provider, 'outside_gate', side_effect=failure):
                    with self.assertRaises(runtime.ProviderStop) as held:
                        transaction.validate('fixture', self.root)
                self.assertEqual(held.exception.code, code or 25)
                transaction.git.assert_not_called()

    def rollback_with_fault(self, fault):
        # Exercise the actual transaction with real Git; only the one denied
        # filesystem operation is simulated, so no foreign UID is required.
        spec = importlib.util.spec_from_file_location('conflict_fault_test', SCRIPTS / 'bureau-conflicts.py')
        runtime = importlib.util.module_from_spec(spec); spec.loader.exec_module(runtime)
        self.config.write_text(json.dumps(self.settings))
        index = self.repo / '.git/index'
        saved_index = index.read_bytes()
        transaction = runtime.Transaction(self.repo, self.settings, self.base, self.config)
        with fault:
            try: transaction.merge()
            except (runtime.Declined, OSError) as exc: failure = exc
            else: self.fail('failing generator was accepted')
        # Assert ignored data first: a clean Git reset alone cannot recover it.
        self.assertEqual((self.repo / 'ignored.txt').read_text(), 'private ignored sentinel\n')
        self.assertEqual(index.read_bytes(), saved_index)
        self.assertEqual(self.git('rev-parse', 'HEAD'), self.head)
        self.assertEqual(self.git('status', '--porcelain', '--untracked-files=all'), '')
        self.assertEqual(self.git('ls-files', '-u'), '')
        self.assertFalse((self.repo / '.git/MERGE_HEAD').exists())
        return failure

    def test_generator_failure_leaves_unchanged_ignored_paths_alone(self):
        self.diverge()
        directory = self.repo / '.cache'
        self.write('.cache/untouched', 'foreign-owned readable cache\n')
        directory.chmod(0o755)
        untouched = (directory, directory / 'untouched')
        saved = {path: path.stat() for path in untouched}
        attempts = []
        chmod = Path.chmod
        def deny_unchanged_chmod(path, mode, **kwargs):
            if path in untouched:
                attempts.append(path)
                raise PermissionError(errno.EPERM, 'simulated foreign UID', str(path))
            return chmod(path, mode, **kwargs)
        self.generated[0]['command'] = [sys.executable, '-c',
            'from pathlib import Path; Path("ignored.txt").write_text("lost"); raise SystemExit(3)']
        self.rollback_with_fault(mock.patch.object(Path, 'chmod', deny_unchanged_chmod))
        self.assertEqual(attempts, [], 'unchanged ignored paths must not be chmodded')
        for path in untouched:
            actual = path.stat()
            self.assertEqual((actual.st_ino, actual.st_mode, actual.st_mtime_ns),
                             (saved[path].st_ino, saved[path].st_mode, saved[path].st_mtime_ns))
        self.assertEqual((directory / 'untouched').read_text(), 'foreign-owned readable cache\n')

    def test_generator_failure_does_not_unlink_unchanged_ignored_files(self):
        self.diverge()
        self.write('.cache/untouched', 'unchanged ignored file\n')
        untouched = self.repo / '.cache/untouched'
        saved = untouched.stat()
        attempts = []
        unlink = Path.unlink
        def deny_unchanged_unlink(path, *args, **kwargs):
            if path == untouched:
                attempts.append(path)
                raise PermissionError(errno.EPERM, 'unchanged file must be preserved', str(path))
            return unlink(path, *args, **kwargs)
        self.generated[0]['command'] = [sys.executable, '-c',
            'from pathlib import Path; Path("ignored.txt").write_text("lost"); raise SystemExit(3)']
        self.rollback_with_fault(mock.patch.object(Path, 'unlink', deny_unchanged_unlink))
        self.assertEqual(attempts, [], 'unchanged ignored files must not be unlinked')
        self.assertEqual(untouched.stat().st_ino, saved.st_ino)
        self.assertEqual(untouched.read_text(), 'unchanged ignored file\n')

    def test_restore_continues_after_a_changed_path_cannot_be_removed(self):
        self.diverge()
        blocked = self.repo / '.cache/blocked'
        unlink = Path.unlink
        attempts = []
        def deny_one_unlink(path, *args, **kwargs):
            if path == blocked:
                attempts.append(path)
                raise PermissionError(errno.EPERM, 'simulated removal failure', str(path))
            return unlink(path, *args, **kwargs)
        self.generated[0]['command'] = [sys.executable, '-c',
            'from pathlib import Path; Path("ignored.txt").write_text("lost"); '
            'Path(".cache").mkdir(); Path(".cache/blocked").touch(); '
            'Path(".cache/removable").touch(); raise SystemExit(3)']
        failure = self.rollback_with_fault(mock.patch.object(Path, 'unlink', deny_one_unlink))
        self.assertTrue(attempts, 'the changed-path failure was not exercised')
        self.assertTrue(blocked.exists(), 'the simulated failure must remain observable')
        self.assertFalse((self.repo / '.cache/removable').exists())
        self.assertIn('restoration', str(failure))

    def test_restore_does_not_follow_a_symlink_whose_unlink_failed(self):
        self.diverge()
        self.write('.cache/a', 'A\n')
        self.write('.cache/b', 'B\n')
        self.write('.cache/c', 'C\n')
        directory = self.repo / '.cache'
        directory.chmod(0o1777)
        blocked, untouched = directory / 'a', directory / 'b'
        saved = untouched.stat()
        unlink = Path.unlink
        attempts = []
        def deny_symlink_unlink(path, *args, **kwargs):
            if path == blocked:
                attempts.append(path)
                # Model a foreign-owned entry in a sticky shared directory.
                raise PermissionError(errno.EPERM, 'simulated sticky-directory unlink denial', str(path))
            return unlink(path, *args, **kwargs)
        self.generated[0]['command'] = [sys.executable, '-c',
            'from pathlib import Path; p=Path(".cache/a"); p.unlink(); p.symlink_to("b"); '
            'Path(".cache/c").write_text("lost"); Path("ignored.txt").write_text("lost"); raise SystemExit(3)']
        failure = self.rollback_with_fault(mock.patch.object(Path, 'unlink', deny_symlink_unlink))
        self.assertTrue(attempts, 'the surviving symlink must exercise the removal failure')
        self.assertTrue(blocked.is_symlink())
        self.assertEqual(os.readlink(blocked), 'b')
        self.assertEqual(untouched.read_text(), 'B\n', 'restoration must not copy A through the surviving symlink')
        actual = untouched.stat()
        self.assertEqual((actual.st_ino, actual.st_mode, actual.st_mtime_ns),
                         (saved.st_ino, saved.st_mode, saved.st_mtime_ns))
        self.assertEqual((directory / 'c').read_text(), 'C\n', 'other changed paths must still restore')
        self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o1777)
        self.assertIn('restoration', str(failure))

    def test_restore_continues_after_a_changed_directory_cannot_be_chmodded(self):
        self.diverge()
        blocked = self.repo / '.cache/z-blocked'
        restored = self.repo / '.cache/a-restored'
        blocked.mkdir(parents=True); restored.mkdir()
        self.write('.cache/a-restored/content', 'saved ignored content\n')
        restored.chmod(0o555)
        def unlock():
            blocked.chmod(0o755); restored.chmod(0o755)
        self.addCleanup(unlock)
        chmod = Path.chmod
        attempts = []
        def deny_one_chmod(path, mode, **kwargs):
            if path == blocked:
                attempts.append(path)
                raise PermissionError(errno.EPERM, 'simulated chmod failure', str(path))
            return chmod(path, mode, **kwargs)
        self.generated[0]['command'] = ['/bin/sh', '-c',
            'printf lost > ignored.txt; chmod 0755 .cache/a-restored; '
            'printf damaged > .cache/a-restored/content; chmod 0000 .cache/*; exit 3']
        failure = self.rollback_with_fault(mock.patch.object(Path, 'chmod', deny_one_chmod))
        self.assertTrue(attempts, 'the changed-directory failure was not exercised')
        self.assertEqual((restored / 'content').read_text(), 'saved ignored content\n')
        self.assertEqual(stat.S_IMODE(restored.stat().st_mode), 0o555)
        self.assertIn('restoration', str(failure))

    def test_restore_only_grants_required_parent_permissions(self):
        self.diverge()
        self.write('.cache/content', 'saved ignored content\n')
        directory = self.repo / '.cache'
        directory.chmod(0o555)
        self.addCleanup(directory.chmod, 0o755)
        chmod = Path.chmod
        attempts = []
        def record_chmod(path, mode, **kwargs):
            if path == self.repo or self.repo in path.parents: attempts.append((path, mode))
            return chmod(path, mode, **kwargs)
        self.generated[0]['command'] = ['/bin/sh', '-c',
            'chmod 0755 .cache; printf damaged > .cache/content; chmod 0555 .cache; '
            'printf lost > ignored.txt; exit 3']
        self.rollback_with_fault(mock.patch.object(Path, 'chmod', record_chmod))
        self.assertEqual((directory / 'content').read_text(), 'saved ignored content\n')
        self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o555)
        self.assertEqual([mode for path, mode in attempts if path == directory], [0o755, 0o555])
        self.assertFalse(any(path == self.repo for path, _ in attempts), 'root needed no chmod')

    def test_readonly_generator_directory_rolls_back(self):
        self.diverge()
        directory = self.repo / 'docs/site'
        mode = stat.S_IMODE(directory.stat().st_mode)
        self.addCleanup(directory.chmod, mode)
        self.generated[0]['command'] = ['/bin/sh', '-c',
            'printf broken > docs/site/page.html; chmod 0555 docs/site; exit 1']
        self.refused(verify_index=True)
        self.assertEqual(stat.S_IMODE(directory.stat().st_mode), mode)

    def test_inaccessible_generator_directories_restore_saved_modes_and_index(self):
        self.diverge()
        parent, directory = self.repo / 'docs', self.repo / 'docs/site'
        def unlock():
            parent.chmod(0o755); directory.chmod(0o755)
        self.addCleanup(unlock)
        parent.chmod(0o555); directory.chmod(0o750)
        self.generated[0]['command'] = ['/bin/sh', '-c',
            'git update-index --assume-unchanged source.txt; printf broken > docs/site/page.html; '
            'printf lost > ignored.txt; chmod 0000 docs/site docs; exit 1']
        self.refused(verify_index=True)
        self.assertEqual(stat.S_IMODE(parent.stat().st_mode), 0o555)
        self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o750)

    def dependency_worker(self):
        self.write('.gitignore', (self.repo / '.gitignore').read_text() + 'node_modules/\n.nm.tmp/\n')
        self.write('package.json', json.dumps({'name': 'conflict-fixture', 'version': '1.0.0',
                                             'scripts': {'test': 'bureau-local-check'}}, indent=2) + '\n')
        self.write('package-lock.json', json.dumps({'name': 'conflict-fixture', 'version': '1.0.0',
                                                  'lockfileVersion': 3, 'packages': {}}, indent=2) + '\n')
        self.commit('npm foundation'); self.git('branch', '-f', 'feature', 'HEAD')
        self.diverge()
        self.git('checkout', '-q', 'main')
        for name in ('package.json', 'package-lock.json'):
            self.write(name, (self.repo / name).read_text().replace('1.0.0', '2.0.0'))
        self.commit('merged manifests advance'); self.git('push', '-q', 'origin', 'main')
        self.base = self.git('rev-parse', 'main')
        self.write('node_modules/.bin/bureau-local-check', '#!/bin/sh\nexec ' + shlex.quote(sys.executable) + ' check.py\n')
        (self.repo / 'node_modules/.bin/bureau-local-check').chmod(0o755)
        self.primary = self.repo
        worker = self.root / 'fresh worker'
        self.git('worktree', 'add', '-q', '-b', 'worker', str(worker), 'feature')
        self.repo = worker
        self.write('ignored.txt', 'private ignored sentinel\n')
        self.env.update(npm_config_cache=str(self.root / 'npm-cache'), npm_config_update_notifier='false')
        self.settings['repo']['test_command'] = 'npm test'
        self.assertFalse((worker / 'node_modules').exists())

    @unittest.skipUnless(shutil.which('npm'), 'npm required for the local-runner reproduction')
    def test_merged_dependencies_are_prepared_before_validation(self):
        self.dependency_worker()
        self.success()
        self.assertIn('Dependencies: cloned from the main checkout', self.result.stdout)
        self.assertEqual(json.loads((self.repo / 'package.json').read_text())['version'], '2.0.0')
        self.assertTrue((self.repo / 'node_modules/.bin/bureau-local-check').is_file())
        self.assertFalse((self.repo / 'node_modules').is_symlink())

    @unittest.skipUnless(shutil.which('npm'), 'npm required for the local-runner reproduction')
    def test_merged_dependencies_are_prepared_before_generator(self):
        self.dependency_worker()
        self.generated[0]['command'] = ['/bin/sh', '-c',
            'test -x node_modules/.bin/bureau-local-check && exec ' + shlex.quote(sys.executable) + ' render.py']
        self.success()

    def test_dependency_preparation_failure_aborts_before_generator_and_check(self):
        self.dependency_worker()
        # Merged manifests cannot use the main checkout's dependency clone.
        package = json.loads((self.repo / 'package.json').read_text()); package['private'] = True
        self.write('package.json', json.dumps(package, indent=2) + '\n')
        self.commit('feature manifest additions'); self.head = self.git('rev-parse', 'HEAD')
        marker = self.root / 'commands-ran'
        self.generated[0]['command'] = ['/bin/sh', '-c', 'touch ' + shlex.quote(str(marker))]
        self.settings['repo']['test_command'] = 'touch ' + shlex.quote(str(marker))
        fake = self.root / 'bin'; fake.mkdir()
        npm_log = self.root / 'npm-prepare'
        (fake / 'npm').write_text('#!/bin/sh\ncat package.json >> ' + shlex.quote(str(npm_log)) + '\nexit 1\n')
        (fake / 'npm').chmod(0o755)
        self.env['PATH'] = str(fake) + os.pathsep + self.env['PATH']
        self.refused()
        self.assertTrue(npm_log.exists(), 'dependency preparation was not attempted')
        self.assertIn('2.0.0', npm_log.read_text())
        self.assertIn('"private": true', npm_log.read_text())
        self.assertFalse(marker.exists(), 'generator/check ran after dependency preparation failed')
        self.assertFalse((self.repo / '.nm.tmp').exists())
        self.assertFalse((self.repo / 'node_modules').exists())

    def test_missing_generator_command(self):
        self.diverge(); self.generated[0]['command'] = ['bureau-no-such-command-1747']; self.refused()

    def test_missing_pandoc(self):
        self.diverge()
        fake = self.root / 'bin'; fake.mkdir()
        self.generated[0]['command'] = [sys.executable, '-c',
            f'import os,subprocess; os.environ["PATH"]={str(fake)!r}; subprocess.run(["pandoc", "--version"], check=True)']
        self.refused()

    def test_generator_cannot_edit_sources_or_ignored_files(self):
        self.diverge()
        for target in ('source.txt', 'ignored.txt', 'new-untracked.txt'):
            with self.subTest(target=target):
                self.generated[0]['command'] = [sys.executable, '-c', f'from pathlib import Path; Path({target!r}).write_text("unauthorized")']
                self.refused(); self.assertFalse((self.repo / 'new-untracked.txt').exists())

    def test_generator_cannot_stage_or_commit(self):
        self.diverge()
        self.generated[0]['command'] = [sys.executable, '-c', 'import subprocess; subprocess.run(["git", "add", "docs/site/page.html"], check=True)']
        self.refused()

    def test_red_validation(self):
        self.diverge(); self.settings['repo']['test_command'] = 'exit 42'; self.refused()

    def test_check_cannot_rewrite_the_checked_tree(self):
        self.diverge(); self.settings['repo']['test_command'] = 'echo changed > source.txt'; self.refused()

    def test_missing_or_unusable_check(self):
        self.diverge()
        for check in (None, False, [], '', '   ', 'bureau-no-such-check-1747'):
            with self.subTest(check=check):
                self.settings['repo']['test_command'] = check; self.refused()

    def test_comment_only_check_is_refused_before_generator(self):
        self.diverge()
        marker = self.root / 'generator-ran'
        self.generated[0]['command'] = ['/bin/sh', '-c', 'touch ' + shlex.quote(str(marker))]
        self.settings['repo']['test_command'] = '# TODO configure tests'
        self.refused()
        self.assertFalse(marker.exists())

    def test_multiline_comment_only_check_is_refused(self):
        self.diverge()
        # Bash ends comments only at LF; CR inside a comment is not a command.
        self.settings['repo']['test_command'] = ' \t\n # TODO configure tests\r echo still a comment\n\t# another comment\n'
        self.refused()

    def test_check_with_leading_comments_executes(self):
        self.diverge()
        self.settings['repo']['test_command'] = '# local check\n \t# explanation\n' + self.settings['repo']['test_command']
        self.success()

    def test_incomplete_output_coverage(self):
        self.diverge(); self.generated[0]['globs'] = ['docs/site/page.html']; self.refused()

    def test_remaining_markers(self):
        self.diverge()
        self.generated[0]['command'] = [sys.executable, '-c', 'from pathlib import Path; Path("docs/site/page.html").write_text("<<<<<<< still bad\\n=======\\n>>>>>>> bad\\n")']
        self.refused()

    def test_markers_in_unchanged_ours_are_also_refused(self):
        self.diverge()
        self.write('docs/site/page.html', '<<<<<<< ours marker\n=======\n>>>>>>> remains\n')
        self.commit('marker in ours'); self.head = self.git('rev-parse', 'HEAD')
        self.generated[0]['command'] = [sys.executable, '-c', 'pass']
        self.settings['repo']['test_command'] = 'true'; self.refused()

    def test_overlapping_nonconflicted_outputs_are_refused(self):
        self.diverge(generated=False, append=True)
        self.settings['repo']['conflict_resolution']['append_only_paths'] = ['CHANGELOG.md']
        self.generated.append({'globs': ['docs/site/index.html'], 'command': [sys.executable, '-c', 'pass']})
        self.refused()

    def test_symlink_output_is_refused(self):
        self.diverge()
        self.generated[0]['command'] = [sys.executable, '-c', 'from pathlib import Path; p=Path("docs/site/page.html"); p.unlink(); p.symlink_to("../../source.txt")']
        self.refused(); self.assertFalse((self.repo / 'docs/site/page.html').is_symlink())

    def test_append_rewrites_deletions_binary_and_ambiguity(self):
        for kind in ('rewrite', 'deletion', 'binary', 'ambiguous', 'unterminated'):
            with self.subTest(kind=kind):
                # Commit adversarial side blobs on each iteration, then undo
                # only test history after verifying the transaction rollback.
                original = self.git('rev-parse', 'feature')
                for branch, side in (('feature', 'ours'), ('main', 'theirs')):
                    self.git('checkout', '-q', branch)
                    base = '# Changes\n\n## Unreleased\n\n## Released\n- Old entry\n'
                    if kind == 'rewrite': text = base.replace('- Old entry', '- ' + side)
                    elif kind == 'deletion': text = base.replace('- Old entry\n', '') + side + '\n'
                    elif kind == 'binary': text = base + '\0' + side + '\n'
                    elif kind == 'ambiguous': text = base.replace('## Unreleased\n', '## Unreleased\n## Released\n' + side + '\n')
                    else: text = base + side
                    self.write('CHANGELOG.md', text); self.commit(side)
                self.git('push', '-q', '--force', 'origin', 'main')
                self.base = self.git('rev-parse', 'main'); self.git('checkout', '-q', 'feature'); self.head = self.git('rev-parse', 'HEAD')
                self.settings['repo'].update(test_command='true', conflict_resolution={'append_only_paths': ['CHANGELOG.md']})
                self.refused()
                self.git('reset', '--hard', original); self.git('branch', '-f', 'main', original)

    def test_modify_delete_is_refused(self):
        self.diverge(generated=False, append=True)
        self.git('rm', 'CHANGELOG.md'); self.commit('delete ours')
        self.head = self.git('rev-parse', 'HEAD')
        self.settings['repo'].update(test_command='true', conflict_resolution={'append_only_paths': ['CHANGELOG.md']})
        self.refused()

    def test_append_add_add_is_refused(self):
        for branch, side in (('feature', 'ours'), ('main', 'theirs')):
            self.git('checkout', '-q', branch); self.write('new-changelog.md', side + '\n'); self.commit(side)
        self.git('push', '-q', 'origin', 'main'); self.base = self.git('rev-parse', 'main')
        self.git('checkout', '-q', 'feature'); self.head = self.git('rev-parse', 'HEAD')
        self.settings['repo'].update(test_command='true', conflict_resolution={'append_only_paths': ['new-changelog.md']})
        self.refused()

    def test_append_mode_change_is_refused(self):
        self.diverge(generated=False, append=True)
        (self.repo / 'CHANGELOG.md').chmod(0o755); self.commit('change file mode'); self.head = self.git('rev-parse', 'HEAD')
        self.settings['repo'].update(test_command='true', conflict_resolution={'append_only_paths': ['CHANGELOG.md']})
        self.refused()

    def test_generator_special_file_rolls_back(self):
        self.diverge()
        self.generated[0]['command'] = [sys.executable, '-c', 'import os; os.mkfifo("surprise-fifo")']
        self.refused(); self.assertFalse((self.repo / 'surprise-fifo').exists())

    def test_commit_hook_cannot_change_validated_tree(self):
        self.diverge()
        hook = self.repo / '.git/hooks/pre-commit'
        hook.write_text('#!/bin/sh\necho changed > source.txt\ngit add source.txt\n'); hook.chmod(0o755)
        self.refused()

    def test_check_command_process_is_reaped(self):
        self.diverge()
        pidfile = self.root / 'check-pid'
        self.settings['repo']['test_command'] = shlex.join([sys.executable, '-c', f'import os; from pathlib import Path; Path({str(pidfile)!r}).write_text(str(os.getpid()))'])
        self.success()
        with self.assertRaises(ProcessLookupError): os.kill(int(pidfile.read_text()), 0)

    def test_interrupted_generator_is_stopped_and_merge_rolled_back(self):
        self.diverge()
        pidfile = self.root / 'generator-pids'
        self.generated[0]['command'] = [sys.executable, '-c',
            f'import os,json,time; from pathlib import Path; Path({str(pidfile)!r}).write_text(json.dumps([os.getpid(),os.getppid()])); time.sleep(30)']
        child = self.invoke(asynchronous=True)
        def cleanup():
            if pidfile.exists():
                try: os.killpg(json.loads(pidfile.read_text())[0], signal.SIGKILL)
                except ProcessLookupError: pass
            if child.poll() is None:
                try: os.killpg(child.pid, signal.SIGKILL)
                except ProcessLookupError: pass
            child.wait(); child.stdout.close(); child.stderr.close()
        self.addCleanup(cleanup)
        deadline = time.monotonic() + 10
        while not pidfile.exists() and child.poll() is None and time.monotonic() < deadline: time.sleep(0.02)
        self.assertTrue(pidfile.exists(), 'generator did not start')
        generator, executor = json.loads(pidfile.read_text())
        os.kill(executor, signal.SIGTERM)
        stdout, stderr = child.communicate(timeout=10)
        self.assertEqual(child.returncode, 17, stdout + stderr)
        for pid in (generator, executor, child.pid):
            with self.assertRaises(ProcessLookupError): os.kill(pid, 0)
        self.assertEqual(self.git('rev-parse', 'HEAD'), self.head)
        self.assertEqual(self.git('status', '--porcelain'), '')
        self.assertFalse((self.repo / '.git/MERGE_HEAD').exists())

    def test_validation_cannot_hide_edits_in_index_flags(self):
        self.diverge()
        flags = self.git('ls-files', '-v')
        self.settings['repo']['test_command'] = 'git update-index --assume-unchanged source.txt; echo hidden > source.txt'
        self.refused()
        self.assertEqual(self.git('ls-files', '-v'), flags)

    def test_generator_and_check_use_untrusted_environment(self):
        self.diverge()
        self.env.update(LINEAR_API_KEY='fixture-secret-canary-1747', GH_TOKEN='fixture-github-1747',
                        CARRIER='contains-fixture-secret-canary-1747-here', HERDR_PANE_ID='private-pane')
        self.generated[0]['command'] = [sys.executable, '-c',
            'import os,runpy; assert not any(k in os.environ for k in ("LINEAR_API_KEY","GH_TOKEN","CARRIER","HERDR_PANE_ID","BUREAU_CONFIG","BUREAU_ENV_FILE")); runpy.run_path("render.py")']
        self.success()
        self.assertFalse((self.repo / '.git/bureau/evidence').exists(), 'repair cannot mint sandbox-gate or review evidence')

    def test_overlapping_output_ownership_is_refused(self):
        self.diverge()
        self.generated.append({'globs': ['docs/**'], 'command': [sys.executable, 'render.py']})
        self.refused()

    def test_existing_merge_is_not_aborted_by_hook(self):
        self.diverge()
        self.git('merge', '--no-ff', '--no-edit', 'origin/main', check=False)
        merge_head = (self.repo / '.git/MERGE_HEAD').read_bytes()
        unmerged = self.git('ls-files', '-u')
        self.assertEqual(self.invoke(), 17)
        self.assertEqual((self.repo / '.git/MERGE_HEAD').read_bytes(), merge_head)
        self.assertEqual(self.git('ls-files', '-u'), unmerged)

    def test_dirty_checkout_is_preserved(self):
        self.diverge(); self.write('source.txt', 'human work\n'); self.write('untracked notes', 'human notes\n')
        self.refused(); self.assertEqual((self.repo / 'source.txt').read_text(), 'human work\n')

    def test_legacy_rules_without_configured_check(self):
        for name in ('.specify/feature.json', 'CLAUDE.md', 'rust/Cargo.lock', 'logs/queue-test.log'):
            self.write(name, 'base\n')
        self.commit('legacy foundation'); self.git('branch', '-f', 'feature', 'HEAD')
        for branch, side in (('feature', 'ours'), ('main', 'theirs')):
            self.git('checkout', '-q', branch)
            for name in ('.specify/feature.json', 'CLAUDE.md', 'rust/Cargo.lock', 'logs/queue-test.log'):
                self.write(name, side + '\n')
            self.commit(side)
        self.git('push', '-q', 'origin', 'main'); self.base = self.git('rev-parse', 'main')
        self.git('checkout', '-q', 'feature'); self.head = self.git('rev-parse', 'HEAD')
        self.settings = {'repo': {}}
        self.success()
        self.assertEqual((self.repo / '.specify/feature.json').read_text(), 'ours\n')
        self.assertEqual((self.repo / 'rust/Cargo.lock').read_text(), 'theirs\n')
        for side in ('ours', 'theirs'): self.assertIn(side, (self.repo / 'CLAUDE.md').read_text())
        self.assertFalse((self.repo / 'logs/queue-test.log').exists())

    def test_configuration_paths_and_commands_fail_closed(self):
        self.diverge()
        for rule in ({'globs': ['../*'], 'command': ['true']}, {'globs': ['/tmp/*'], 'command': ['true']},
                     {'globs': ['docs/**'], 'command': 'true'}, {'globs': [], 'command': ['true']}):
            with self.subTest(rule=rule):
                self.settings['repo']['conflict_resolution']['generated'] = [rule]; self.refused()


if __name__ == '__main__': unittest.main(verbosity=2)
