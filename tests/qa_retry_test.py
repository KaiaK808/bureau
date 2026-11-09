"""Assertion-only QA retries, coverage proof and bounded evidence."""
import importlib.util
import io
from contextlib import redirect_stdout
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import uuid

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('gates', ROOT / 'templates/scripts/bureau-gates.py')
g = importlib.util.module_from_spec(spec); spec.loader.exec_module(g)

CASE = 'test_case (suite.Cases.test_case) ... '
STABLE = 'test_stable (suite.Cases.test_stable) ... ok\n'
FAILED = (CASE + 'FAIL\n' + STABLE + 'FAIL: test_case (suite.Cases.test_case)\nAssertionError: mismatch\n'
          + '\nRan 2 tests in 0.001s\n\nFAILED (failures=1)\n')
PASSED = CASE + 'ok\n' + STABLE + '\nRan 2 tests in 0.001s\n\nOK\n'
SELECTED = '\nRan 1 test in 0.001s\n\nOK\n'
PYTEST_FAILED = ('tests/test_gate.py::test_case FAILED [100%]\n'
                 '=================================== FAILURES ===================================\n'
                 '__________________________________ test_case __________________________________\n\n'
                 '    def test_case():\n'
                 '>       assert 1 == 2\n'
                 'E       assert 1 == 2\n\n'
                 'tests/test_gate.py:2: AssertionError\n'
                 '=========================== short test summary info ============================\n'
                 'FAILED tests/test_gate.py::test_case - assert 1 == 2\n'
                 '============================== 1 failed in 0.01s ===============================\n')


class RetryTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='qa-retry-')
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)

    def run_gate(self, outputs, command='bash test-gate.sh', heads=None):
        self.calls = []; self.records = []
        def execute(command, log):
            self.calls.append(command)
            item = outputs[len(self.calls) - 1]
            if isinstance(item, Exception): raise item
            code, text, abort = item
            log.write_text(text)
            return code, abort
        return g.qa_run_gate(execute, command, self.root / 'gate.log', 10,
                             lambda: 'a' * 40 if heads is None else next(heads),
                             lambda *args: self.records.append(args))

    def test_assertion_retry_passes_with_two_logs_and_marker(self):
        code, abort, result = self.run_gate([(1, FAILED, None), (0, PASSED, None)])
        self.assertEqual((0, None), (code, abort))
        self.assertEqual(2, len(self.calls))
        self.assertEqual(2, len(self.records))
        self.assertEqual(['suite.Cases.test_case'], result['flaky']['failed_tests'])
        self.assertEqual([FAILED, PASSED], [Path(log).read_text() for log in result['logs']])
        self.assertIsNone(self.records[0][-1])
        self.assertEqual(result['flaky'], self.records[1][-1])

    def test_second_failure_stays_red_without_a_third_run(self):
        code, _, result = self.run_gate([(1, FAILED, None), (1, FAILED, None)])
        self.assertEqual(1, code)
        self.assertEqual(2, len(self.calls))
        self.assertIsNone(result['flaky'])

    def test_native_unittest_selects_failed_cases(self):
        code, _, _ = self.run_gate([(1, FAILED, None), (0, SELECTED, None)], 'python3 -m unittest -q suite')
        self.assertEqual(0, code)
        self.assertEqual('python3 -m unittest -q suite.Cases.test_case', self.calls[1])

    def test_native_pytest_selects_failed_cases(self):
        for reason in ('AssertionError: mismatch', 'assert 1 == 2'):
            with self.subTest(reason=reason):
                output = f'FAILED tests/test_gate.py::test_case - {reason}\n1 failed, 2 passed, 1 deselected, 1 warning in 0.03s\n'
                code, _, _ = self.run_gate([(1, output, None), (0, '1 passed in 0.01s\n', None)], 'pytest -q tests/')
                self.assertEqual(0, code)
                self.assertEqual('pytest -q tests/test_gate.py::test_case', self.calls[1])

    def test_passing_unittest_names_do_not_suppress_retry(self):
        for name in ('test_timeout', 'test_sandbox', 'test_teardown'):
            with self.subTest(name=name):
                attempts = []
                class Cases(unittest.TestCase):
                    def test_case(self):
                        attempts.append(None)
                        self.assertGreater(len(attempts), 1)
                setattr(Cases, name, lambda self: None)
                outputs = []
                for _ in range(2):
                    output = io.StringIO()
                    result = unittest.TextTestRunner(stream=output, verbosity=2).run(
                        unittest.defaultTestLoader.loadTestsFromTestCase(Cases))
                    outputs.append((int(not result.wasSuccessful()), output.getvalue(), None))
                code, _, result = self.run_gate(outputs)
                self.assertEqual(0, code)
                self.assertEqual(2, len(self.calls))
                self.assertEqual(1, len(result['flaky']['failed_tests']))

    def test_failing_unittest_traceback_identities_do_not_suppress_retry(self):
        for name in ('test_case', 'test_timeout', 'test_sandbox', 'test_teardown'):
            for filename in ('test_gate.py', 'test_timeout.py', 'test_sandbox.py', 'test_teardown.py'):
                with self.subTest(name=name, filename=filename):
                    namespace = dict(__name__='suite', unittest=unittest, attempts=[])
                    source = ('class Cases(unittest.TestCase):\n'
                              f'    def {name}(self):\n'
                              '        attempts.append(None)\n'
                              '        self.assertGreater(len(attempts), 1)\n')
                    exec(compile(source, filename, 'exec'), namespace)
                    outputs = []
                    for _ in range(2):
                        output = io.StringIO()
                        result = unittest.TextTestRunner(stream=output, verbosity=2).run(
                            unittest.defaultTestLoader.loadTestsFromTestCase(namespace['Cases']))
                        outputs.append((int(not result.wasSuccessful()), output.getvalue(), None))
                    self.assertIn(f'File "{filename}", line 4, in {name}\n', outputs[0][1])
                    code, _, result = self.run_gate(outputs)
                    self.assertEqual(0, code)
                    self.assertEqual(2, len(self.calls))
                    self.assertEqual(2, len(self.records))
                    self.assertEqual(['suite.Cases.' + name], result['flaky']['failed_tests'])
                    self.assertEqual([item[1] for item in outputs], [Path(log).read_text() for log in result['logs']])

    def test_failing_pytest_traceback_identities_do_not_suppress_retry(self):
        for name in ('test_case', 'test_timeout', 'test_sandbox', 'test_teardown'):
            for filename in ('test_gate.py', 'test_timeout.py', 'test_sandbox.py', 'test_teardown.py'):
                for traceback in ('long', 'short'):
                    with self.subTest(name=name, filename=filename, traceback=traceback):
                        output = PYTEST_FAILED.replace('test_case', name).replace('test_gate.py', filename)
                        if traceback == 'short':
                            output = output.replace(f'    def {name}():\n', f'tests/{filename}:2: in {name}\n')
                        passed = f'tests/{filename}::{name} PASSED [100%]\n1 passed in 0.01s\n'
                        code, _, result = self.run_gate([(1, output, None), (0, passed, None)], 'pytest -v tests/')
                        self.assertEqual(0, code)
                        self.assertEqual(['pytest -v tests/', f'pytest -v tests/{filename}::{name}'], self.calls)
                        self.assertEqual(2, len(self.records))
                        self.assertEqual([f'tests/{filename}::{name}'], result['flaky']['failed_tests'])
                        self.assertEqual([output, passed], [Path(log).read_text() for log in result['logs']])

    def test_passing_pytest_names_do_not_suppress_retry(self):
        for name in ('test_timeout', 'test_sandbox', 'test_teardown'):
            with self.subTest(name=name):
                first = ('tests/test_gate.py::test_case FAILED [ 50%]\n'
                         f'tests/test_gate.py::{name} PASSED [100%]\n'
                         'FAILED tests/test_gate.py::test_case - assert 1 == 2\n1 failed, 1 passed in 0.01s\n')
                passed = ('tests/test_gate.py::test_case PASSED [ 50%]\n'
                          f'tests/test_gate.py::{name} PASSED [100%]\n2 passed in 0.01s\n')
                code, _, result = self.run_gate([(1, first, None), (0, passed, None)])
                self.assertEqual(0, code)
                self.assertEqual(2, len(self.calls))
                self.assertEqual(['tests/test_gate.py::test_case'], result['flaky']['failed_tests'])

    def test_failure_diagnostics_still_exclude_reruns(self):
        for reason in ('timeout', 'sandbox', 'teardown', 'cleanup failed', 'Permission denied'):
            outputs = (FAILED.replace('AssertionError: mismatch', 'AssertionError: ' + reason),
                       f'FAILED tests/test_gate.py::test_case - AssertionError: {reason}\n1 failed in 0.01s\n',
                       PYTEST_FAILED.replace('AssertionError\n', 'AssertionError: ' + reason + '\n'),
                       PYTEST_FAILED.replace('E       assert 1 == 2', 'E       AssertionError: ' + reason))
            for output in outputs:
                with self.subTest(reason=reason, output=output):
                    code, _, result = self.run_gate([(1, output, None)])
                    self.assertEqual(1, code)
                    self.assertEqual(1, len(self.calls))
                    self.assertIsNone(result['flaky'])

    def test_pytest_non_assertion_failures_never_retry(self):
        for reason in ('OSError: [Errno 30] Read-only file system', 'OSError: write failed',
                       'ValueError: bad input', 'RuntimeError: fixture crashed', 'Failed: fixture failed', ''):
            with self.subTest(reason=reason):
                output = 'FAILED tests/test_gate.py::test_case' + (f' - {reason}' if reason else '') + '\n1 failed in 0.01s\n'
                code, _, result = self.run_gate([(1, output, None), (0, '1 passed in 0.01s\n', None)], 'pytest -q tests/')
                self.assertEqual(1, code)
                self.assertEqual(1, len(self.calls))
                self.assertIsNone(result['flaky'])
        output = ('FAILED tests/test_gate.py::test_case - AssertionError: mismatch\n'
                  'FAILED tests/test_gate.py::test_other - OSError: write failed\n2 failed in 0.01s\n')
        self.run_gate([(1, output, None), (0, '2 passed in 0.01s\n', None)], 'pytest -q tests/')
        self.assertEqual(1, len(self.calls), 'one assertion cannot qualify a mixed failure run')

    def test_read_only_denials_exclude_even_assertion_summaries(self):
        for denial in ('OSError: [Errno 30] Read-only file system', 'OSError: EROFS'):
            with self.subTest(denial=denial):
                code, _, result = self.run_gate([(1, denial + '\n' + FAILED, None), (0, PASSED, None)])
                self.assertEqual(1, code)
                self.assertEqual(1, len(self.calls))
                self.assertIsNone(result['flaky'])

    def test_non_assertion_failures_never_retry(self):
        for code, output, abort in (
                (124, FAILED, 'timeout'), (124, FAILED, None), (137, FAILED, None),
                (1, 'runner crashed\n', None), (1, '\nRan 0 tests in 0.001s\n\nOK\n', None),
                (1, FAILED + 'cleanup failed\n', None), (1, 'Operation not permitted\n' + FAILED, None),
                (1, 'Permission denied\n' + FAILED, None), (1, FAILED.replace('failures=1', 'errors=1'), None),
                (1, FAILED.replace('Ran 2 tests', 'Ran 0 tests'), None),
                (1, FAILED.replace('failures=1', 'failures=1, skipped=2'), None)):
            with self.subTest(code=code, output=output, abort=abort):
                _, _, result = self.run_gate([(code, output, abort)])
                self.assertEqual(1, len(self.calls))
                self.assertIsNone(result['flaky'])
                (self.root / 'gate.log').unlink()

    def test_unreadable_or_incomplete_output_never_retries(self):
        for output in ('\ufffd', FAILED.rstrip().rsplit('\n', 1)[0], 'FAILED test_case\n'):
            with self.subTest(output=output):
                self.run_gate([(1, output, None)])
                self.assertEqual(1, len(self.calls))

    def test_timeout_and_cleanup_failure_cannot_become_flakes(self):
        with patch.object(g.time, 'monotonic', side_effect=[0, 11]):
            self.run_gate([(1, FAILED, None)])
        self.assertEqual(1, len(self.calls))
        with self.assertRaisesRegex(OSError, 'cleanup'):
            self.run_gate([OSError('cleanup failure')])
        self.assertEqual(1, len(self.calls))
        self.assertEqual([], self.records)

    def test_passing_rerun_requires_coverage_and_no_added_skips(self):
        for output in (SELECTED, PASSED.replace('OK', 'OK (skipped=1)'), '', PASSED.replace('2 tests', '0 tests'),
                       PASSED.replace(STABLE, ''), PASSED.replace('test_case', 'test_other').replace('test_stable', 'test_another'),
                       PASSED.replace('test_stable', 'test_case')):
            with self.subTest(output=output):
                code, _, result = self.run_gate([(1, FAILED, None), (0, output, None)])
                self.assertNotEqual(0, code)
                self.assertIsNone(result['flaky'])
        first = FAILED.replace('2 tests', '3 tests').replace('failures=1', 'failures=1, skipped=1')
        code, _, result = self.run_gate([(1, first, None), (0, PASSED.replace('2 tests', '3 tests').replace('OK', 'OK (skipped=1)'), None)])
        self.assertNotEqual(0, code, 'a new skip cannot borrow an earlier skipped count')
        self.assertIsNone(result['flaky'])
        code, _, result = self.run_gate([(1, FAILED.replace(STABLE, ''), None), (0, PASSED, None)])
        self.assertNotEqual(0, code, 'the first run must also identify every original case')
        self.assertIsNone(result['flaky'])

    def test_whole_pytest_rerun_requires_original_case_identities(self):
        first = ('tests/test_gate.py::test_case FAILED [ 50%]\ntests/test_gate.py::test_stable PASSED [100%]\n'
                 'FAILED tests/test_gate.py::test_case - assert 1 == 2\n1 failed, 1 passed in 0.01s\n')
        passed = ('tests/test_gate.py::test_case PASSED [ 50%]\ntests/test_gate.py::test_stable PASSED [100%]\n'
                  '2 passed in 0.01s\n')
        for output, expected in ((passed, 0), ('2 passed in 0.01s\n', 1),
                                 (passed.replace('test_case', 'test_other').replace('test_stable', 'test_another'), 1),
                                 (passed.replace('test_stable', 'test_case'), 1)):
            with self.subTest(output=output):
                code, _, result = self.run_gate([(1, first, None), (0, output, None)])
                self.assertEqual(expected, code)
                self.assertEqual(expected == 0, result['flaky'] is not None)
                self.assertEqual(2, len(self.calls))

    def test_changed_head_never_retries(self):
        self.run_gate([(1, FAILED, None)], heads=iter(['a' * 40, 'b' * 40, 'b' * 40]))
        self.assertEqual(1, len(self.calls))

    def test_full_rerun_can_cover_more_than_an_early_stopped_failure(self):
        passed = 'test_extra (suite.Cases.test_extra) ... ok\n' + PASSED.replace('2 tests', '3 tests')
        code, _, result = self.run_gate([(1, FAILED, None), (0, passed, None)])
        self.assertEqual(0, code)
        self.assertIsNotNone(result['flaky'])

    def test_whole_gate_reads_real_unittest_case_identities(self):
        (self.root / 'test_fixture.py').write_text('''from pathlib import Path
import unittest
class Cases(unittest.TestCase):
    def test_case(self):
        marker = Path('ran')
        passed = marker.exists()
        marker.touch()
        self.assertTrue(passed)
    def test_stable(self):
        self.assertEqual(2 + 3, 5)
''')
        (self.root / 'gate.sh').write_text('exec python3 -B -m unittest -v test_fixture\n')
        logs = []
        def execute(command, log):
            self.assertEqual('bash gate.sh', command)
            run = subprocess.run(['/bin/bash', 'gate.sh'], cwd=self.root,
                                 stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=5)
            log.write_text(run.stdout); logs.append(log)
            return run.returncode, None
        code, _, result = g.qa_run_gate(execute, 'bash gate.sh', self.root / 'gate.log', 10,
                                        lambda: 'a' * 40, lambda *args: None)
        self.assertEqual(0, code)
        self.assertEqual(2, len(logs))
        self.assertEqual(['test_fixture.Cases.test_case'], result['flaky']['failed_tests'])

    def test_no_change_guidance_requires_a_qa_bounce_with_flake_evidence(self):
        source = (ROOT / 'templates/scripts/implement-pipeline.sh').read_text()
        helper = source[source.index('refresh_review_context() {'):].split('\n}\n', 1)[0] + '\n}\n'
        flake = '\nFlaky: passed on one rerun; failed tests: suite.Cases.test_case'
        for feedback, expected in (('🔄 QA: tests failing — routing back to Build.\nA named test failed.', False),
                                   ('🔄 QA: tests failing — routing back to Build.' + flake, True),
                                   ('🚫 QA flagged for human review.' + flake, False),
                                   ('FIXES_NEEDED\nA product decision is still missing.' + flake, False)):
            with self.subTest(feedback=feedback):
                script = 'get_issue_branch_and_comments() { printf "%s" "$FEEDBACK_JSON"; }\n' + helper
                result = subprocess.run(['/bin/bash', '-eu', '-c', script + '\nrefresh_review_context fixture'],
                                        env=dict(os.environ, FEEDBACK_JSON=json.dumps(dict(comments=[dict(body=feedback)]))),
                                        capture_output=True, text=True, timeout=5)
                self.assertEqual(0, result.returncode, result.stderr)
                self.assertEqual(expected, 'no change needed, QA red looks flaky' in result.stdout)
                if expected: self.assertIn('A still-red gate or an independent blocker keeps its normal hold', result.stdout)
                else:
                    self.assertEqual('\n--- Feedback to address (PRIORITY) ---\n' + feedback
                                     + '\nAddress ALL fixes before remaining tasks.\n--- End feedback ---\n', result.stdout)

    def test_timeout_setting_is_bounded(self):
        for value, expected in (('1', 1), ('86400', 86400), ('0', 900), ('86401', 900), ('bad', 900)):
            with self.subTest(value=value), patch.dict(os.environ, BUREAU_QA_TEST_TIMEOUT=value):
                self.assertEqual(expected, g.qa_timeout())

    def test_delegated_final_gate_and_replay_keep_the_flake(self):
        spec = importlib.util.spec_from_file_location('provider', ROOT / 'templates/scripts/bureau-provider.py')
        provider = importlib.util.module_from_spec(spec); spec.loader.exec_module(provider)
        e = provider.e; store = e.Store(self.root / 'bureau'); head = 'a' * 40
        run = uuid.uuid4().hex
        store.begin_run(run, None, self.root, head)
        attempt = store.begin_attempt(run, None, 'qa', 'stage', self.root, head)
        context = dict(run_id=run, provider_attempt_id=uuid.uuid4().hex, stage_attempt_id=attempt,
                       issue=None, stage='qa', phase='qa', schema_sha256='b' * 64)
        result = dict(status='GREEN', tests_added=0, tests_failing=0, coverage_notes='Coverage checked.')
        store.put_receipt(dict(**context, provider='codex', outcome='complete', provider_exit=0, normalized_exit=0,
                               structured_result=result, start_head=head, end_head=head,
                               started_at=time.time(), ended_at=time.time(), gate_attempts=[]))
        context_path = self.root / 'context.json'; context_path.write_text(json.dumps(context))
        args = SimpleNamespace(stage='qa', gate_action='check', gate_context=str(context_path), gate_source_head=None)
        config = dict(repo=dict(test_command='python3 -m unittest -q suite'))
        options = dict(runner='codex', phase='qa', gate_outside_sandbox=True, timeout=10)
        calls = []
        def execute(options, repo, command, log):
            calls.append(command)
            log.write_text(FAILED if len(calls) == 1 else SELECTED)
            return (1 if len(calls) == 1 else 0), None
        with patch.object(provider, 'evidence_store', return_value=store), \
             patch.object(provider, 'current_head', return_value=head), \
             patch.object(provider, 'outside_gate', side_effect=execute), \
             patch.dict(os.environ, BUREAU_RUN_ID='', BUREAU_STAGE_ATTEMPT_ID='', BUREAU_CURRENT_ISSUE=''):
            for replay in (False, True):
                with self.subTest(replay=replay), patch.object(provider.sys, 'stdin', io.StringIO(json.dumps(result))):
                    gate = json.loads(provider.gate_action(args, config, options, self.root))
                    self.assertTrue(gate['green'], gate)
                    self.assertEqual(['suite.Cases.test_case'], gate['flakes'][0]['failed_tests'])
                    self.assertEqual(2, len(gate['logs']))
                    self.assertTrue(all(Path(log).is_file() for log in gate['logs']))
                    self.assertEqual(2, len(calls), 'replay must not run tests again')
        gates = store._manifest(run)['attempts'][0]['gates']
        self.assertEqual([1, 0], [gate['normalized_exit'] for gate in gates])
        self.assertIn('flaky', gates[-1])

    def test_delegated_cancellation_survives_bookkeeping_and_executor_errors(self):
        spec = importlib.util.spec_from_file_location('provider', ROOT / 'templates/scripts/bureau-provider.py')
        provider = importlib.util.module_from_spec(spec); spec.loader.exec_module(provider)
        head = 'a' * 40
        context = dict(run_id=uuid.uuid4().hex, stage_attempt_id=uuid.uuid4().hex,
                       provider_attempt_id=uuid.uuid4().hex, issue=None, stage='qa', phase='qa')
        context_path = self.root / 'context.json'; context_path.write_text(json.dumps(context))
        config_path = self.root / 'config.json'; config_path.write_text(json.dumps(dict(repo=dict(test_command='bash gate.sh'))))
        for fault in ('head', 'execution', 'none'):
            for cancelled in (True, False):
                with self.subTest(fault=fault, cancelled=cancelled):
                    options = dict(runner='codex', phase='qa', gate_outside_sandbox=True, timeout=10)
                    store = Mock()
                    store.reserve_gate_rerun.return_value = (True, dict(attempt_id=uuid.uuid4().hex,
                                                                      gate_attempt_ids=[context['provider_attempt_id']]))
                    store.finish_gate_rerun.side_effect = lambda run, owner, attempt, record: record
                    executed = False
                    def current_head(repo):
                        if executed and fault == 'head': raise OSError('injected HEAD read failure')
                        return head
                    def execute(qa_options, repo, command, log):
                        nonlocal executed
                        executed = True
                        if cancelled: qa_options['_gate_cancelled'] = True
                        if fault == 'execution': raise OSError('injected executor failure')
                        log.write_text('cancelled\n' if cancelled else PASSED)
                        return (130, 'kill') if cancelled else (0, None)
                    output = io.StringIO()
                    with patch.object(provider, 'configuration', return_value=options), \
                         patch.object(provider, 'evidence_store', return_value=store), \
                         patch.object(provider, 'current_head', side_effect=current_head), \
                         patch.object(provider, 'outside_gate', side_effect=execute) as outside, \
                         patch.object(provider.g, 'plan', return_value=dict(eligible=True, gate_attempt_ids=[context['provider_attempt_id']])), \
                         patch.object(provider.sys, 'stdin', io.StringIO('{}')), \
                         patch.object(provider.sys, 'argv', ['bureau-provider.py', '--stage', 'qa', '--gate-action', 'check',
                                                            '--repo', str(self.root), '--config', str(config_path),
                                                            '--gate-context', str(context_path)]), \
                         patch.dict(os.environ, BUREAU_RUN_ID='', BUREAU_STAGE_ATTEMPT_ID='', BUREAU_CURRENT_ISSUE=''), \
                         redirect_stdout(output):
                        code = provider.main()
                    self.assertEqual(130 if cancelled else 0, code, output.getvalue())
                    self.assertEqual(fault == 'none' and not cancelled, json.loads(output.getvalue())['green'])
                    self.assertEqual(cancelled, bool(options.get('_gate_cancelled')))
                    self.assertEqual(1, outside.call_count)

    def test_recorded_names_are_bounded(self):
        output = ''.join(f'test_{n} (suite.Cases.test_{n}) ... FAIL\n' for n in range(25))
        output += ''.join(f'FAIL: test_{n} (suite.Cases.test_{n})\n' for n in range(25))
        output += '\nRan 25 tests in 0.001s\n\nFAILED (failures=25)\n'
        passed = ''.join(f'test_{n} (suite.Cases.test_{n}) ... ok\n' for n in range(25))
        code, _, result = self.run_gate([(1, output, None), (0, passed + '\nRan 25 tests in 0.001s\n\nOK\n', None)])
        self.assertEqual(0, code)
        self.assertEqual(20, len(result['flaky']['failed_tests']))
        self.assertEqual(5, result['flaky']['omitted'])


if __name__ == '__main__': unittest.main()
