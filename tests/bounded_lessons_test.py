"""Real lesson shell entrypoint and local Git; no provider/network calls."""
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import unittest

import git_maintenance

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = Path(os.environ.get('BUREAU_TEST_LESSONS_SCRIPTS', ROOT / 'templates/scripts'))
WRAPPER = ('## Learned patterns\n\nThe following are human-curated lessons from prior bureau runs. '
           'Treat as advisory, not binding — they reflect patterns observed across multiple past issues '
           'and may not all apply here. Use them to inform judgment; do not cite them as pinned decisions.\n\n')


def lesson(body, **meta):
    return '<!-- bureau-lesson ' + json.dumps(meta) + ' -->\n' + body + '\n'


class LessonsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='bureau-1754-lessons-')
        self.addCleanup(self.temp.cleanup)
        self.repo = Path(self.temp.name).resolve() / 'repo with spaces'
        self.repo.mkdir()
        self.config = self.repo / '.bureau.json'
        self.config.write_text('{}')
        self.file = self.repo / 'LESSONS.md'
        self.spec = self.repo / 'specs/ticket'
        self.spec.mkdir(parents=True)
        self.git('init', '-q', '-b', 'main')
        self.git(*git_maintenance.OFF_CONFIG)
        self.git('config', 'user.email', 'test@example.invalid')
        self.git('config', 'user.name', 'Lesson Test')
        self.git('commit', '-q', '--allow-empty', '-m', 'test: base')
        self.git('update-ref', 'refs/remotes/origin/main', 'HEAD')

    def git(self, *args):
        return subprocess.check_output(['git', '-C', str(self.repo), *args], stderr=subprocess.STDOUT, text=True).strip()

    def context(self, text=None, tasks='', stage='implement', diff=''):
        if text is not None:
            self.file.write_text(text, encoding='utf-8')
        if tasks is not None:
            (self.spec / 'tasks.md').write_text(tasks, encoding='utf-8')
        before = self.file.read_bytes() if self.file.exists() else None
        call = 'source "$1"; build_lessons_context "$2" "$3"'
        args = ['_', str(SCRIPTS / 'bureau-config.sh'), stage, str(self.spec)]
        if diff is not None:
            call += ' "$4"'
            args.append(diff)
        proc = subprocess.run(['bash', '-c', call, *args],
                              cwd=self.repo, env={**os.environ, 'BUREAU_CONFIG': str(self.config)},
                              capture_output=True, text=True, timeout=15)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.file.read_bytes() if self.file.exists() else None, before, 'LESSONS.md was written')
        return proc.stdout, proc.stderr

    def test_legacy_absent_empty_whitespace_keep_working(self):
        for value in (None, '', ' \n\t\n'):
            with self.subTest(value=value):
                out, err = self.context(value)
                self.assertEqual((out, err), ('', ''))
        text = '# Existing patterns\n\n## API\nDo the old thing.\n\n'
        out, err = self.context(text)
        self.assertEqual(out, WRAPPER + text.rstrip('\n'))
        self.assertEqual(err, '')

    def test_task_selection_exact_glob_directory_general_and_tags(self):
        text = ('Legacy preamble.\n' + lesson('EXACT', paths=['src/api.py'], tags=['api'], owner='team', evidence='https://example.invalid/proof')
                + lesson('GLOB', paths=['src/**/*.py']) + lesson('DIR', paths=['src/'])
                + lesson('UNRELATED', paths=['web/**']) + lesson('TAG_ONLY', tags=['web'])
                + lesson('GENERAL'))
        out, err = self.context(text, '- [ ] T001 Change `src/api.py:17`\n')
        for value in ('EXACT', 'GLOB', 'DIR', 'GENERAL', 'Legacy preamble.'):
            self.assertIn(value, out)
        for value in ('UNRELATED', 'TAG_ONLY', 'bureau-lesson', 'https://example.invalid/proof'):
            self.assertNotIn(value, out)
        self.assertLess(out.index('EXACT'), out.index('GLOB'))
        self.assertLess(out.index('DIR'), out.index('Legacy preamble.'))
        self.assertEqual(err, '')

    def test_task_syntax_new_paths_spaces_links_dotfiles_and_root(self):
        text = ''.join(lesson('MATCH_' + str(i), paths=[p]) for i, p in enumerate(
            ['new/module.py', 'docs/space name.md', '.env.example', 'src/exact.py', 'Makefile']))
        text += lesson('DECOY', paths=['unrelated/file.py'])
        text += lesson('PARTIAL_DECOY', paths=['docs/space']) + lesson('BASENAME_DECOY', paths=['name.md'])
        tasks = ('- [ ] Add new/module.py\n- [ ] Edit `docs/space name.md`\n'
                 '- [ ] Edit .env.example\n- [ ] Edit [module](src/exact.py)\n- [ ] Edit `Makefile`\n')
        out, _ = self.context(text, tasks)
        for i in range(5):
            self.assertIn('MATCH_' + str(i), out)
        self.assertNotIn('DECOY', out)
        self.assertNotIn('PARTIAL_DECOY', out)
        self.assertNotIn('BASENAME_DECOY', out)
        out, _ = self.context(text, '- [ ] Change `.`\n')
        for i in range(5):
            self.assertIn('MATCH_' + str(i), out)

    def test_task_directory_and_glob_scope(self):
        text = lesson('DIRECTORY_MATCH', paths=['src/a.py']) + lesson('NO_MATCH', paths=['other/a.py'])
        for path in ('src/', 'src/*.py', './src/a.py'):
            with self.subTest(path=path):
                out, _ = self.context(text, '- [ ] Edit `' + path + '`\n')
                self.assertIn('DIRECTORY_MATCH', out)
                self.assertNotIn('NO_MATCH', out)

    def test_directory_glob_overlap_and_bracketed_path_equality(self):
        for pattern, path in (('src/api/**', 'src/api/'), ('src/api/', 'src/api/**'),
                              ('src/app/[id]/page.tsx', 'src/app/[id]/page.tsx')):
            with self.subTest(pattern=pattern, path=path):
                text = lesson('MATCH', paths=[pattern]) + lesson('DECOY', paths=['other/**'])
                out, err = self.context(text, 'Edit ' + path + '\n')
                self.assertIn('MATCH', out)
                self.assertNotIn('DECOY', out)
                self.assertEqual(err, '')
        path = 'src/app/[id]/page.tsx'
        (self.repo / path).parent.mkdir(parents=True)
        (self.repo / path).write_text('changed\n')
        self.git('add', '--', path)
        self.git('commit', '-qm', 'test: bracketed route')
        out, err = self.context(lesson('MATCH', paths=[path]), tasks=None, diff='origin/main...HEAD')
        self.assertIn('MATCH', out)
        self.assertEqual(err, '')

    def test_glob_directory_subtree_overlap(self):
        for pattern, directory in (('src/**/*.py', 'src/api/'), ('**/*.md', 'docs/'),
                                   ('src/*/x.py', 'src/api/'), ('src/?pi/x.py', 'src/api/'),
                                   ('src/[ab]pi/x.py', 'src/api/'), ('src/**/api/*.py', 'src/api/')):
            with self.subTest(pattern=pattern, directory=directory):
                out, err = self.context(lesson('MATCH', paths=[pattern]), 'Edit ' + directory + '\n')
                self.assertIn('MATCH', out)
                self.assertEqual(err, '')

    def test_glob_directory_subtrees_remain_disjoint(self):
        for pattern, directory in (('src/web/**', 'src/api/'), ('src/*/x.py', 'src/api/deep/'),
                                   ('src/?pi/x.py', 'src/web/'), ('src/[ab]pi/x.py', 'src/cpi/'),
                                   ('src/*/web/**', 'src/api/deep/')):
            with self.subTest(pattern=pattern, directory=directory):
                out, err = self.context(lesson('NO_MATCH', paths=[pattern]), 'Edit ' + directory + '\n')
                self.assertEqual((out, err), ('', ''))

    def test_glob_task_files_are_not_directory_scopes(self):
        for path, expected in (('src/api/client.ts', False), ('`src/api/client.ts`', False),
                               ('src/api/x.py', True), ('src/api/', True), ('src/api', False)):
            with self.subTest(path=path):
                if path == 'src/api':
                    (self.repo / path).mkdir(parents=True)
                out, err = self.context(lesson('PYTHON', paths=['src/**/*.py']), 'Edit ' + path + '\n')
                self.assertEqual((out, err), (WRAPPER + 'PYTHON' if expected else '', ''))

    def test_glob_diff_files_are_not_directory_scopes(self):
        base = self.git('rev-parse', 'HEAD')
        for path, expected in (('src/api/client.ts', False), ('src/api/x.py', True), ('src/api', False)):
            with self.subTest(path=path):
                self.git('reset', '--hard', '-q', base)
                target = self.repo / path
                if path == 'src/api':
                    # A gitlink has no glob or trailing slash: a concrete scope.
                    target.mkdir(parents=True)
                    self.git('update-index', '--add', '--cacheinfo', '160000,' + base + ',' + path)
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_text('changed\n')
                    self.git('add', '--', path)
                self.git('commit', '-qm', 'test: scoped diff')
                out, err = self.context(lesson('PYTHON', paths=['src/**/*.py']), tasks=None, diff='HEAD^...HEAD')
                self.assertEqual((out, err), (WRAPPER + 'PYTHON' if expected else '', ''))

    def test_task_scope_file_or_pattern_classification(self):
        cases = (('src/**/*.py', 'src/api/**', True),
                 ('src/**/*.py', 'src/api/', True),
                 ('src/**/*.py', 'src/api/client.ts', False),
                 ('src/**/*.py', 'src/api/x.py', True),
                 ('src/web/**', 'src/api/**', False),
                 ('src/web/**', 'src/api/', False),
                 ('src/api/x.py', 'src/**/api/x.py', True),
                 ('src/v2/api/x.py', 'src/**/x.py', True),
                 ('src/**/api/x.py', 'src/api/**', True),
                 ('src/a*/x.py', 'src/*b/x.py', True),
                 ('src/?pi/x.py', 'src/[ab]pi/*.py', True),
                 ('src/*/web/**', 'src/api/deep/**', False),
                 ('src/api/x.py/**', 'src/api/x.py', False))
        for pattern, scope, expected in cases:
            with self.subTest(pattern=pattern, scope=scope):
                out, err = self.context(lesson('SELECTED', paths=[pattern]), 'Edit ' + scope + '\n')
                self.assertEqual((out, err), (WRAPPER + 'SELECTED' if expected else '', ''))

    def test_rejected_paths_do_not_suppress_valid_diff_or_task_paths(self):
        paths = ('app/routes/$userId.tsx', 'app/routes/$other.tsx', 'src/api.ts')
        for path in paths:
            (self.repo / path).parent.mkdir(parents=True, exist_ok=True)
            (self.repo / path).write_text('changed\n')
        self.git('add', '--', *paths)
        self.git('commit', '-qm', 'test: unusual and valid route paths')
        text = lesson('MATCH', paths=['src/api.ts']) + lesson('DECOY', paths=['other/**'])
        for tasks, diff in ((None, 'origin/main...HEAD'),
                            ('Edit `app/routes/$userId.tsx`, `../outside.ts` and `src/api.ts`.', '')):
            with self.subTest(tasks=tasks, diff=diff):
                out, err = self.context(text, tasks, diff=diff)
                self.assertIn('MATCH', out)
                self.assertNotIn('DECOY', out)
                self.assertLessEqual(len(err.splitlines()), 1)
                self.assertNotIn('local diff unavailable', err)

    def test_budget_keeps_whole_entries_and_skips_oversized_entries(self):
        marker = '\n[Lessons truncated to the stage byte budget.]'
        first = 'A' * 7816
        qualified = ('## Reset database state\n'
                     'Run `TRUNCATE users CASCADE;` only against a disposable test database; never against production.')
        for text, expected in ((lesson(first) + lesson(qualified), first),
                               (lesson('HUGE ' + 'B' * 9000), ''),
                               (lesson('FIRST') + lesson('HUGE ' + 'B' * 9000) + lesson('LAST'), 'FIRST\n\nLAST')):
            with self.subTest(expected=expected[:20]):
                out, err = self.context(text)
                self.assertLessEqual(len(out.encode('utf-8')), 8192)
                self.assertEqual(out, WRAPPER + expected + marker)
                self.assertEqual(err, '')
                self.assertEqual((out, err), self.context(text))

    def test_absent_lessons_keeps_base_prompt_bytes(self):
        out, err = self.context(tasks=None)
        self.assertEqual((out, err), ('', ''))
        # Frozen complete prompt bytes from the pre-lessons baseline 2c10e82.
        cases = (('implement-pipeline.sh', 'IMPL_SYSTEM', 2269,
                  'e7a1f85211124255258ad8f89c936f784ba9e99a9adcc19661b6cb3a93cdc377'),
                 ('implement-pipeline.sh', 'PROMPT', 2470,
                  '3d6f5579b4f0597d892c7b0fd427cca694768e709eb6aaa1acc81200c33796a6'),
                 ('qa-pipeline.sh', 'QA_RESULT', 1485,
                  '7714cd52d30505c96e37667b2df43e76900eb13bf9e23aaaa37747d1175fc543'))
        variables = ('ISSUE ISSUE_TITLE BRANCH SPEC_CONTEXT PROJECT_CONTEXT DESIGN_CONTEXT REVIEW_CONTEXT '
                     'ISSUE_DESC TASKS_FILE NEGATIVE_CONSTRAINTS TIMEOUT_NOTE TEST_CMD TEST_LOG_TAIL '
                     'QA_PROMPT_INTENT NEGATIVE_CONSTRAINTS_BODY SCRIPT_REPO QA_GATE_CONTEXT').split()
        env = {**os.environ, **{name: 'fixture-' + name for name in variables}, 'LESSONS_CONTEXT': out}
        for name, symbol, size, digest in cases:
            with self.subTest(symbol=symbol):
                source = (SCRIPTS / name).read_text()
                prefix = '  ' + symbol + '=' + ('$("${CLAUDE[@]}"' if symbol == 'QA_RESULT' else '"')
                source = source[source.index(prefix):]
                assignment = re.match(r'(?s).*?(?<!\\)"\)?\n', source)
                self.assertIsNotNone(assignment)
                script = 'capture_prompt() { printf "%s" "${@: -1}"; }; CLAUDE=(capture_prompt)\n'
                script += assignment.group(0) + '\nprintf "%s" "$' + symbol + '"\n'
                proc = subprocess.run(['bash', '-c', script], cwd=self.repo, env=env,
                                      capture_output=True, timeout=15)
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertEqual(len(proc.stdout), size)
                self.assertEqual(hashlib.sha256(proc.stdout).hexdigest(), digest)

    def test_globs_metadata_alias_and_path_union(self):
        text = (lesson('ALIAS_MATCH', globs=['src/**/*.py'])
                + lesson('UNION_MATCH', paths=['other.py'], globs=['src/*.py'])
                + lesson('DECOY', globs=['web/**']))
        out, err = self.context(text, '`src/a.py`')
        self.assertIn('ALIAS_MATCH', out)
        self.assertIn('UNION_MATCH', out)
        self.assertNotIn('DECOY', out)
        self.assertEqual(err, '')

    def test_multiple_recursive_globs_allow_each_zero_directory(self):
        text = lesson('NESTED', paths=['src/**/api/**/test.py']) + lesson('DECOY', paths=['web/**'])
        for path in ('src/api/test.py', 'src/v2/api/test.py', 'src/api/unit/test.py', 'src/v2/api/unit/test.py'):
            with self.subTest(path=path):
                out, _ = self.context(text, '`' + path + '`')
                self.assertIn('NESTED', out)
                self.assertNotIn('DECOY', out)

    def test_no_scope_keeps_only_untagged_general(self):
        out, _ = self.context(lesson('SCOPED', paths=['src/a.py']) + lesson('TAGGED', tags=['api']) + lesson('GENERAL'))
        self.assertIn('GENERAL', out)
        self.assertNotIn('SCOPED', out)
        self.assertNotIn('TAGGED', out)

    def test_external_links_and_commands_do_not_add_path_scope(self):
        text = lesson('DECOY', paths=['src/a.py']) + lesson('GENERAL')
        out, _ = self.context(text, 'Read https://example.invalid/src/a.py and `python3 -m unittest`.\n')
        self.assertIn('GENERAL', out)
        self.assertNotIn('DECOY', out)

    def test_warning_with_closed_stderr_cannot_change_stage_exit(self):
        self.file.write_text(lesson('OLD', expires='2000-01-01'))
        command = 'source "$1"; build_lessons_context implement "" "" 2>&-; exit 26'
        proc = subprocess.run(['bash', '-c', command, '_', str(SCRIPTS / 'bureau-config.sh')],
                              cwd=self.repo, env={**os.environ, 'BUREAU_CONFIG': str(self.config)},
                              capture_output=True, text=True, timeout=15)
        self.assertEqual(proc.returncode, 26)
        self.assertEqual(proc.stdout, '')

    def test_local_pr_diff_union_and_both_rename_sides(self):
        (self.repo / 'old name.py').write_text('existing\n')
        self.git('add', 'old name.py')
        self.git('commit', '-qm', 'test: before rename')
        base = self.git('rev-parse', 'HEAD')
        self.git('mv', 'old name.py', 'new name.py')
        self.git('commit', '-qm', 'test: rename')
        text = (lesson('OLD_PATH', paths=['old name.py']) + lesson('NEW_PATH', paths=['new name.py'])
                + lesson('TASK_PATH', paths=['src/future.py']) + lesson('DECOY', paths=['src/decoy.py']))
        out, err = self.context(text, '- [ ] Change `src/future.py`\n', stage='code_review', diff=base + '...HEAD')
        for value in ('OLD_PATH', 'NEW_PATH', 'TASK_PATH'):
            self.assertIn(value, out)
        self.assertNotIn('DECOY', out)
        self.assertEqual(err, '')

    def test_invalid_diff_remains_advisory_and_does_not_expand_scope(self):
        out, err = self.context(lesson('MATCH', paths=['src/a.py']) + lesson('DECOY', paths=['web/a.py']),
                                '- [ ] Change `src/a.py`\n', diff='not-a-ref...HEAD')
        self.assertIn('MATCH', out)
        self.assertNotIn('DECOY', out)
        self.assertEqual(len(err.splitlines()), 1)
        self.assertIn('local diff unavailable', err)

    def test_default_diff_uses_configured_local_base(self):
        self.config.write_text(json.dumps({'repo': {'base_branch': 'integration'}}))
        self.git('update-ref', 'refs/remotes/origin/integration', 'HEAD')
        (self.repo / 'diff-only.py').write_text('branch change\n')
        self.git('add', 'diff-only.py')
        self.git('commit', '-qm', 'test: configured base diff')
        out, err = self.context(lesson('DIFF_ONLY', paths=['diff-only.py']) + lesson('DECOY', paths=['absent.py']), diff=None)
        self.assertIn('DIFF_ONLY', out)
        self.assertNotIn('DECOY', out)
        self.assertEqual(err, '')

    def test_unreadable_tasks_keep_general_and_diff_lessons(self):
        (self.spec / 'tasks.md').write_bytes(b'\xff\xfe')
        (self.repo / 'diff-only.py').write_text('branch change\n')
        self.git('add', 'diff-only.py')
        self.git('commit', '-qm', 'test: diff without task inventory')
        out, err = self.context(lesson('GENERAL') + lesson('DIFF_ONLY', paths=['diff-only.py'])
                                + lesson('DECOY', paths=['elsewhere.py']), tasks=None, diff='origin/main...HEAD')
        self.assertIn('GENERAL', out)
        self.assertIn('DIFF_ONLY', out)
        self.assertNotIn('DECOY', out)
        self.assertEqual(len(err.splitlines()), 1)
        self.assertIn('task paths unavailable', err)

    def test_expiry_utc_boundary_and_single_warning(self):
        today = datetime.now(timezone.utc).date()
        text = (lesson('EXPIRED_ONE', expires=str(today - timedelta(days=1)))
                + lesson('EXPIRED_TWO', paths=['src/a.py'], expires='2000-01-01')
                + lesson('TODAY', expires=str(today)) + lesson('FUTURE', expires='9999-12-31'))
        out, err = self.context(text, '- [ ] Change `src/a.py`\n')
        self.assertNotIn('EXPIRED', out)
        self.assertIn('TODAY', out)
        self.assertIn('FUTURE', out)
        self.assertEqual(err.splitlines(), ['bureau lessons: WARN: omitted 2 expired lesson(s)'])

    def test_all_expired_produces_no_wrapper(self):
        out, err = self.context(lesson('OLD', expires='2000-01-01'))
        self.assertEqual(out, '')
        self.assertIn('omitted 1 expired', err)

    def test_invalid_metadata_is_not_promoted_to_general(self):
        text = ('<!-- bureau-lesson {bad json} -->\nMALFORMED\n'
                + lesson('INVALID_PATH', paths=['../outside']) + lesson('INVALID_TYPE', tags='api')
                + lesson('INVALID_DATE', expires='2026-02-30') + lesson('GOOD'))
        out, err = self.context(text)
        self.assertIn('GOOD', out)
        for value in ('MALFORMED', 'INVALID_PATH', 'INVALID_TYPE', 'INVALID_DATE'):
            self.assertNotIn(value, out)
        self.assertEqual(len(err.splitlines()), 1)

    def test_fenced_markers_are_legacy_prose(self):
        text = '# Examples\n```md\n' + lesson('EXAMPLE', paths=['elsewhere']) + '```\n'
        out, err = self.context(text)
        self.assertEqual(out, WRAPPER + text.rstrip('\n'))
        self.assertEqual(err, '')

    def test_per_stage_utf8_budget_is_deterministic_and_includes_wrapper(self):
        self.config.write_text(json.dumps({'agents': {'lessons_budget_bytes': {
            'spec': 480, 'implement': 560, 'qa': 640, 'code_review': 720}}}))
        target = 'TARGET_FIRST ' + '€🙂' * 15
        general = 'GENERAL_LAST ' + 'G' * 100
        marker = '\n[Lessons truncated to the stage byte budget.]'
        text = lesson(general) + lesson(target, paths=['src/a.py']) + lesson('TOO_BIG ' + 'G' * 1000)
        for stage, budget in (('spec', 480), ('implement', 560), ('qa', 640), ('code_review', 720)):
            with self.subTest(stage=stage):
                out, err = self.context(text, '`src/a.py`', stage=stage)
                expected = WRAPPER + target
                if len((expected + '\n\n' + general + marker).encode('utf-8')) <= budget:
                    expected += '\n\n' + general
                self.assertEqual(out, expected + marker)
                self.assertLessEqual(len(out.encode('utf-8')), budget)
                self.assertEqual((out, err), self.context(text, '`src/a.py`', stage=stage))

    def test_default_8192_bytes_and_legacy_is_bounded(self):
        out, err = self.context('ä' * 12000)
        self.assertTrue(out.startswith(WRAPPER))
        self.assertLessEqual(len(out.encode()), 8192)
        self.assertEqual(out, WRAPPER + '\n[Lessons truncated to the stage byte budget.]')
        self.assertEqual(err, '')

    def test_zero_tiny_exact_and_invalid_budget_values(self):
        for budget in (0, 1, len(WRAPPER.encode()) - 1):
            with self.subTest(budget=budget):
                self.config.write_text(json.dumps({'agents': {'lessons_budget_bytes': {'implement': budget}}}))
                out, _ = self.context('GENERAL')
                self.assertEqual(out, '')
        exact = len((WRAPPER + 'GENERAL').encode())
        self.config.write_text(json.dumps({'agents': {'lessons_budget_bytes': {'implement': exact}}}))
        self.assertEqual(self.context('GENERAL'), (WRAPPER + 'GENERAL', ''))
        tiny_utf8 = len((WRAPPER + '\n[Lessons truncated to the stage byte budget.]').encode()) + 1
        self.config.write_text(json.dumps({'agents': {'lessons_budget_bytes': {'implement': tiny_utf8}}}))
        out, err = self.context('🙂' * 100)
        self.assertEqual(out, WRAPPER + '\n[Lessons truncated to the stage byte budget.]')
        self.assertEqual(err, '')
        for bad in (-1, True, 5.5, '500', None, 1048577):
            with self.subTest(invalid=bad):
                self.config.write_text(json.dumps({'agents': {'lessons_budget_bytes': {'implement': bad}}}))
                out, err = self.context('X' * 10000)
                self.assertLessEqual(len(out.encode()), 8192)
                self.assertIn('using 8192 bytes', err)

    def test_no_writes_and_isolated_imports_in_directory_with_spaces(self):
        marker = self.repo / 'import-ran'
        (self.repo / 'json.py').write_text('raise RuntimeError("branch module imported")')
        before = hashlib.sha256((SCRIPTS / 'bureau-config.sh').read_bytes()).hexdigest()
        out, err = self.context(lesson('SELECTED', paths=['src/a.py']), '`src/a.py`')
        self.assertIn('SELECTED', out)
        self.assertNotIn('bureau-lesson', out)
        self.assertFalse(marker.exists())
        self.assertEqual(err, '')
        self.assertEqual(hashlib.sha256((SCRIPTS / 'bureau-config.sh').read_bytes()).hexdigest(), before)


if __name__ == '__main__':
    unittest.main()
