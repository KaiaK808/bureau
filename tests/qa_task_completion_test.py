"""Task completion fixtures read real Git objects; QA services remain faked."""
import copy
from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import unittest
import uuid
from unittest.mock import patch

import git_maintenance
import qa_changed_tests_test as changed

ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / 'templates/scripts/bureau-task-completion.py'
c = changed.module(HELPER.parent, HELPER.stem)
a, e = c.a, c.e
TASKS = 'specs/feature/tasks.md'


class CompletionTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='.qa-completion-fixture-', dir=ROOT)
        self.addCleanup(temp.cleanup); self.root = Path(temp.name)
        self.repo = self.root / 'repo'; self.repo.mkdir()
        self.env = {**os.environ, **git_maintenance.OFF_ENV, 'TMPDIR':str(self.root),
                    'GIT_CONFIG_GLOBAL':os.devnull, 'GIT_CONFIG_NOSYSTEM':'1', 'PYTHONDONTWRITEBYTECODE':'1'}
        for key in (*c.GIT_CONTEXT, 'BUREAU_CONFIG', 'BUREAU_RUN_ID', 'BUREAU_STAGE_ATTEMPT_ID'):
            self.env.pop(key, None)
        self.git('init', '-q', '-b', 'main'); git_maintenance.disable(self.repo)
        self.git('config', 'user.email', 'fixture@example.invalid'); self.git('config', 'user.name', 'Fixture')
        self.config = dict(repo=dict(task_completion_check=True))
        self.base_files({'src/value.py':'VALUE = 0\n', 'src/other.py':'VALUE = 0\n', TASKS:'# Tasks\n'})
        self.store = e.Store(self.repo / '.git/bureau'); self.pr = 0

    def git(self, *args):
        return subprocess.check_output(['git', '-C', str(self.repo), *args], env=self.env,
                                       stderr=subprocess.DEVNULL, text=True, timeout=10).strip()

    def write(self, path, source):
        target = self.repo / path; target.parent.mkdir(parents=True, exist_ok=True); target.write_text(source)

    def commit(self, files):
        for path, source in files.items():
            if source is None: (self.repo / path).unlink()
            else: self.write(path, source)
        self.git('add', '--all'); self.git('commit', '-qm', 'test: fixture change')
        return self.git('rev-parse', 'HEAD')

    def base_files(self, files):
        self.base = self.commit({**files, '.bureau.json':json.dumps(self.config)})

    def publish(self, tasks, files=None):
        self.head = self.commit({TASKS:tasks, **(files or {})})

    def begin(self):
        self.run = uuid.uuid4().hex; self.pr += 1
        self.store.begin_run(self.run, None, self.repo, self.head)
        self.attempt = self.store.begin_attempt(self.run, None, 'qa', 'stage', self.repo, self.head)
        self.store.set_pr(self.run, self.attempt, self.pr)
        self.env.update(BUREAU_RUN_ID=self.run, BUREAU_STAGE_ATTEMPT_ID=self.attempt)

    def brief(self):
        with patch.dict(os.environ, self.env, clear=True):
            return a.render(self.store, self.repo, None, self.pr, self.head,
                            'https://example.invalid/repo/pull/' + str(self.pr), base=self.base)

    def state(self):
        files = {str(path.relative_to(self.repo)):path.read_bytes() for path in self.repo.rglob('*')
                 if path.is_file() and '.git' not in path.relative_to(self.repo).parts}
        return (files, (self.repo / '.git/index').read_bytes(), (self.repo / '.git/HEAD').read_bytes(),
                self.git('rev-parse', 'HEAD'), self.git('worktree', 'list', '--porcelain'))

    def scan(self, expected, counts, direct=False):
        self.begin(); before = self.state()
        if direct:
            with patch.dict(os.environ, self.env, clear=True):
                record = c.measure(self.repo, self.base, self.config, TASKS, head=self.head)
            self.store.task_completion(self.run, self.attempt, record)
        else:
            result = self.invoke()
            self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertEqual(before, self.state(), 'measurement changed the checkout, index, HEAD or worktrees')
        record = self.store._manifest(self.run)['attempts'][0].get('task_completion')
        self.assertIsNotNone(record); e.task_completion_shape(record)
        self.assertEqual((self.base, self.head, TASKS, expected),
                         (record['base'], record['head'], record['tasks'], record['verdict']))
        self.assertEqual(dict(zip(('verified', 'flagged', 'unverifiable', 'unchecked'), counts)), record['counts'])
        brief = self.brief()
        self.assertIn(f'- {expected}; verified={counts[0]}; flagged={counts[1]}; unverifiable={counts[2]}; '
                      f'unchecked={counts[3]}; omitted={record["omitted"]}.', brief)
        self.assertLess(brief.index('**Test evidence**'), brief.index('**Task completion**'))
        self.assertLess(brief.index('**Task completion**'), brief.index('**Review evidence**'))
        return record, brief

    def invoke(self, helper=HELPER):
        return subprocess.run([sys.executable, '-I', str(helper), '--repo', str(self.repo),
                               '--base', self.base, '--tasks', TASKS],
                              cwd=self.repo, env=self.env, capture_output=True, text=True, timeout=70)

    def test_phantom_is_flagged_and_private_comparison_mutation_loses_it(self):
        self.publish('# Tasks\n- [X] Update `src/value.py`\n- [ ] Later\n', {'src/other.py':'VALUE = 1\n'})
        record, brief = self.scan('flagged', (0, 1, 0, 1))
        self.assertEqual([dict(line=2, verdict='flagged', rule='no named path changed',
                               paths=['src/value.py'], omitted_paths=0)], record['findings'])
        self.assertIn('task line 2: flagged (no named path changed)', brief)
        scripts = self.root / 'scripts'; shutil.copytree(HELPER.parent, scripts)
        private = scripts / HELPER.name; source = private.read_text()
        start = source.index('def paths_changed('); end = source.index('\n\n', start)
        private.write_text(source[:start] + 'def paths_changed(paths, changed):\n    return True' + source[end:])
        self.begin(); result = self.invoke(private)
        self.assertEqual(0, result.returncode, result.stderr)
        mutated = self.store._manifest(self.run)['attempts'][0]['task_completion']
        self.assertEqual('pass', mutated['verdict']); self.assertEqual(0, mutated['counts']['flagged'])

    def test_correct_mixed_checkboxes_and_unchecked_count(self):
        self.publish('# Tasks\n- [x] Update `src/value.py`\n  * [X] Update `src/other.py`\n'
                     '1. [ ] Later in `missing.py`\n- [xylophone] ordinary text\n',
                     {'src/value.py':'VALUE = 1\n', 'src/other.py':'VALUE = 1\n'})
        record, _ = self.scan('pass', (2, 0, 0, 1))
        self.assertEqual([2, 3], [row['line'] for row in record['findings']])
        self.assertTrue(all(row['rule'] == 'named path changed' for row in record['findings']))

    def test_no_path_ambiguous_and_unsafe_names_never_verify(self):
        self.publish('- [X] Finish the work\n- [x] Update `src/value.py` and `missing/*.py`\n'
                     '- [X] Update `../outside.py`\n- [X] Update `.`\n', {'src/value.py':'VALUE = 1\n'})
        record, _ = self.scan('unverifiable', (0, 0, 4, 0))
        self.assertEqual(['no paths recognized', 'ambiguous paths', 'ambiguous paths', 'unsafe path'],
                         [row['rule'] for row in record['findings']])
        self.assertEqual([[], [], [], []], [row['paths'] for row in record['findings']])

    def test_test_word_requires_added_or_modified_test_even_with_changed_path(self):
        self.base_files({'tests/test_old.py':'assert True\n'})
        self.publish('- [X] Add TESTS for `src/value.py`\n- [x] Run the test\n'
                     '- [x] Update latest contest in `src/value.py`\n',
                     {'src/value.py':'VALUE = 1\n', 'tests/test_old.py':None})
        record, _ = self.scan('flagged', (1, 2, 0, 0))
        self.assertEqual(['no test file changed', 'no test file changed', 'named path changed'],
                         [row['rule'] for row in record['findings']])
        self.publish('- [X] Add tests for `src/value.py`\n', {'tests/test_new.py':'assert True\n'})
        self.scan('pass', (1, 0, 0, 0))

    def test_shared_extractor_handles_prefix_glob_directory_deleted_and_renamed_paths(self):
        self.config['repo']['path_prefix_strip'] = 'project/'
        self.base_files({'old-name':'content\n', 'src/deleted.py':'VALUE = 0\n'})
        self.git('mv', 'old-name', 'new-name')
        self.publish('- [x] Change `project/src/*.py`\n- [x] Change `src/`\n'
                     '- [x] Remove `src/deleted.py`\n- [x] Rename `old-name`\n- [x] Add `new-name`\n',
                     {'src/deleted.py':None})
        original = c.a.paths.extract; calls = []
        def extract(*args):
            calls.append(args); return original(*args)
        with patch.object(c.a.paths, 'extract', extract): record, _ = self.scan('pass', (5, 0, 0, 0), direct=True)
        self.assertEqual(5, len(calls)); self.assertTrue(all(args[2] == 'project/' for args in calls))
        self.assertIn('old-name', record['findings'][3]['paths'])

    def test_pinned_tasks_diff_and_config_ignore_dirty_files(self):
        self.config['agents'] = dict(risk_tier=dict(test_patterns=['checks/*']))
        self.base_files({})
        self.publish('- [x] Add tests in `checks/value.py`\n',
                     {'checks/value.py':'assert True\n', '.bureau.json':'{}\n'})
        self.write(TASKS, '- [x] Update `missing.py`\n'); self.git('add', TASKS)
        self.write(TASKS, '- [x] Uncommitted work\n'); self.write('checks/value.py', 'uncommitted\n')
        alternate = self.root / 'alternate.json'; alternate.write_text('{}')
        self.env['BUREAU_CONFIG'] = str(alternate)
        self.scan('pass', (1, 0, 0, 0))

    def test_bounds_keep_counts_and_brief_omissions(self):
        self.publish('\n'.join(f'- [X] Update `missing-{number}.py`' for number in range(45)))
        record, brief = self.scan('flagged', (0, 45, 0, 0))
        self.assertEqual(40, len(record['findings'])); self.assertEqual(5, record['omitted'])
        block = brief.split('**Task completion**')[1].split('**Review evidence**')[0]
        self.assertEqual(8, block.count('no named path changed')); self.assertIn('- 37 more.', block)

    def test_named_paths_are_bounded_and_any_verified_task_can_establish_pass(self):
        paths = [f'src/value-{number:02d}.py' for number in range(25)]
        self.publish('- [X] Update ' + ', '.join('`' + path + '`' for path in paths) + '\n'
                     '- [x] Finish the work\n', {paths[-1]:'VALUE = 1\n'})
        record, brief = self.scan('pass', (1, 0, 1, 0))
        self.assertEqual(paths[:20], record['findings'][0]['paths'])
        self.assertEqual(5, record['findings'][0]['omitted_paths']); self.assertIn('5 paths omitted', brief)
        self.publish('- [ ] Update `src/value.py`\n')
        record, _ = self.scan('unverifiable', (0, 0, 0, 1))
        self.assertEqual([], record['findings'])

    def test_limits_and_budget_are_unverifiable(self):
        self.publish('- [X] Update `src/value.py`\n', {'src/value.py':'VALUE = 1\n'})
        with patch.object(c, 'MAX_FILES', 0):
            record, _ = self.scan('unverifiable', (0, 0, 1, 0), direct=True)
        self.assertEqual('changed file limit exceeded', record['reason'])
        def expire(*args):
            handler = signal.getsignal(signal.SIGALRM)
            self.assertTrue(callable(handler)); self.assertTrue(0 < signal.getitimer(signal.ITIMER_REAL)[0] <= 60)
            handler(signal.SIGALRM, None)
        with patch.object(c.a.paths, 'extract', expire):
            record, _ = self.scan('unverifiable', (0, 0, 1, 0), direct=True)
        self.assertEqual('budget exhausted', record['reason'])
        self.publish('- [X] Update `src/value.py`\n- [X] Update `src/other.py`\n')
        original = c.a.paths.extract; calls = []
        def partial(*args):
            calls.append(args)
            if len(calls) == 2: expire()
            return original(*args)
        with patch.object(c.a.paths, 'extract', partial):
            record, _ = self.scan('pass', (1, 0, 1, 0), direct=True)
        self.assertEqual('budget exhausted', record['findings'][1]['rule'])
        self.publish('x' * c.MAX_FILE_BYTES + '\n')
        record, _ = self.scan('unverifiable', (0, 0, 0, 0))
        self.assertEqual('file size limit exceeded', record['reason'])

    def test_cancellation_records_unverifiable_and_returns_130(self):
        self.publish('- [x] Update `src/value.py`\n')
        self.begin()
        def interrupt(*args):
            handler = signal.getsignal(signal.SIGTERM); self.assertTrue(callable(handler))
            handler(signal.SIGTERM, None)
        with patch.dict(os.environ, self.env, clear=True), patch.object(c.a.paths, 'extract', interrupt), \
                redirect_stdout(io.StringIO()), patch.object(sys, 'argv',
                ['completion', '--repo', str(self.repo), '--base', self.base, '--tasks', TASKS]):
            self.assertEqual(130, c.main())
        record = self.store._manifest(self.run)['attempts'][0]['task_completion']
        self.assertEqual('interrupted', record['reason']); self.assertEqual('unverifiable', record['verdict'])

    def test_off_and_invalid_settings_leave_record_and_brief_absent(self):
        for value in ('absent', False, None, 0, 1, 'true', [], {}):
            with self.subTest(value=value):
                self.config = dict(repo={} if value == 'absent' else dict(task_completion_check=value))
                self.base_files({})
                self.publish('- [X] Update `missing.py`\n',
                             {'.bureau.json':json.dumps(dict(repo=dict(task_completion_check=True)))})
                self.begin(); brief = self.brief(); result = self.invoke()
                self.assertEqual(0, result.returncode, result.stderr)
                self.assertNotIn('task_completion', self.store._manifest(self.run)['attempts'][0])
                self.assertEqual(brief, self.brief())
                if value == 'absent' or value is False: self.assertEqual('', result.stdout)
                else: self.assertIn('Task completion: not measured (invalid setting)', result.stdout)

    def test_provider_free_resolution_uses_actual_base_after_local_opt_in(self):
        main = self.base
        self.config['agents'] = dict(risk_tier=dict(test_patterns=['checks/*']))
        self.base_files({})
        self.git('update-ref', 'refs/remotes/origin/main', main)
        self.git('update-ref', 'refs/remotes/origin/alternate', self.base)
        self.publish('- [X] Add tests in `checks/value.py`\n', {'checks/value.py':'assert True\n'})
        self.begin(); original = c.Git.prepare; calls = []
        def prepare(git, command):
            if command[0] == 'gh':
                calls.append(command)
                if command[2] == 'list': return b'7'
                return json.dumps(dict(state='OPEN', baseRefName='alternate', headRefName='feature')).encode()
            return original(git, command)
        def invoke():
            with patch.dict(os.environ, self.env, clear=True), patch.object(c.Git, 'prepare', prepare), \
                    redirect_stdout(io.StringIO()), patch.object(sys, 'argv',
                    ['completion', '--repo', str(self.repo), '--base', '', '--branch', 'feature',
                     '--tasks', str(self.repo / TASKS)]):
                return c.main()
        self.assertEqual(0, invoke())
        record = self.store._manifest(self.run)['attempts'][0]['task_completion']
        self.assertEqual(2, len(calls)); self.assertEqual((self.base, TASKS, 'pass'),
                                                     (record['base'], record['tasks'], record['verdict']))
        self.config['repo']['task_completion_check'] = False
        self.base_files({}); self.git('update-ref', 'refs/remotes/origin/main', self.base)
        self.publish('- [X] Update `missing.py`\n'); self.begin(); calls.clear()
        before = self.brief(); self.assertEqual(0, invoke())
        self.assertEqual([], calls); self.assertNotIn('task_completion', self.store._manifest(self.run)['attempts'][0])
        self.assertEqual(before, self.brief())

    def test_sidecar_is_optional_survives_old_writes_and_rejects_corruption(self):
        self.publish('- [X] Update `missing.py`\n')
        record, _ = self.scan('flagged', (0, 1, 0, 0))
        path = self.store.manifest_path(self.run); disk = json.loads(path.read_text())
        legacy = json.loads((changed.FIXTURES / 'manifest.json').read_text())
        self.assertEqual(set(legacy['attempts'][0]), set(disk['attempts'][0]))
        self.store.end_attempt(self.run, self.attempt, 0, self.head)
        self.assertEqual(record, self.store._manifest(self.run)['attempts'][0]['task_completion'])
        mutations = [dict(record, verdict='pass'), dict(record, raw='not metadata'), dict(record, omitted=-1)]
        for field, value in [('line', 0), ('paths', ['../outside']), ('rule', 'unknown prose'), ('verdict', 'verified')]:
            wrong = copy.deepcopy(record); wrong['findings'][0][field] = value; mutations.append(wrong)
        for value in mutations:
            with self.subTest(value=value), self.assertRaises(ValueError): e.task_completion_shape(value)
        sidecar = path.parent / 'task-completion' / (self.attempt + '.json')
        sidecar.write_text(json.dumps(dict(record, verdict='pass')))
        self.assertNotIn('task_completion', self.store._manifest(self.run)['attempts'][0])
        self.assertFalse(list((self.store.root / 'quarantine').glob('*.json')))
        self.assertNotIn('**Task completion**', self.brief())


class QAIntegration(unittest.TestCase):
    def fixture(self):
        fixture = changed.ChangedTests('test_disabled_fixture_and_brief_match_golden')
        self.addCleanup(fixture.doCleanups); fixture.setUp()
        response = fixture.root / 'qa.txt'
        response.write_text('```json\n{"status":"GREEN","tests_added":0,"tests_failing":0,"coverage_notes":"fixture"}\n```\n')
        fixture.env['FAKE_CLAUDE_FIXTURES'] = str(response)
        fixture.write('.git/info/exclude', 'fake_claude_counter\n')
        fixture.config['repo'].pop('changed_test_command')
        fixture.config['repo'].update(task_completion_check=True, test_integrity_check=True)
        fixture.write('.bureau.json', json.dumps(fixture.config))
        fixture.git('add', '.bureau.json'); fixture.git('commit', '-qm', 'test: enable completion evidence')
        fixture.base = fixture.git('rev-parse', 'HEAD')
        fixture.publish({'specs/001-test-branch/tasks.md':'- [X] Update `missing.py`\n'})
        return fixture

    def test_advisory_record_follows_integrity_and_keeps_green_qa(self):
        fixture = self.fixture(); _, outcome = fixture.qa()
        record = fixture.store._manifest(fixture.run)['attempts'][0]['task_completion']
        e.task_completion_shape(record); self.assertEqual('flagged', record['verdict'], record)
        self.assertEqual((0, 'GREEN'), outcome)
        self.assertLess(fixture.output.index('Test integrity:'), fixture.output.index('Task completion: flagged'))
        brief = fixture.brief()
        self.assertLess(brief.index('**Test integrity**'), brief.index('**Task completion**'))
        _, disabled = fixture.qa(enabled=False); self.assertEqual(outcome, disabled)
        self.assertNotIn('task_completion', fixture.store._manifest(fixture.run)['attempts'][0])
        self.assertNotIn('**Task completion**', fixture.brief())

    def test_helper_failure_is_advisory_and_control_exits_propagate(self):
        fixture = self.fixture(); helper = fixture.scripts / HELPER.name
        helper.write_text('raise RuntimeError("fixture helper failure")\n')
        _, outcome = fixture.qa()
        self.assertEqual((0, 'GREEN'), outcome); self.assertIn('Task completion: not measured.', fixture.output)
        self.assertNotIn('task_completion', fixture.store._manifest(fixture.run)['attempts'][0])
        for code in (25, 124, 130):
            with self.subTest(code=code):
                helper.write_text(f'raise SystemExit({code})\n')
                fixture.qa(expected=None, code=code)


if __name__ == '__main__': unittest.main()
