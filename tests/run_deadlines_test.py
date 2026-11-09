"""Deadlines and timing updates in private copies of the real test harness."""
from contextlib import redirect_stdout
import csv
import inspect
import io
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from process_helpers import process_running, wait_for_processes_exit
import run_test_files as runner

ROOT = Path(__file__).resolve().parents[1]


class RunDeadlinesTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='bureau-run-deadlines. ')
        self.addCleanup(self.temp.cleanup)
        self.fixture = Path(self.temp.name).resolve()
        for name in ('run.sh', 'run_test_files.py', 'process_helpers.py'):
            shutil.copy2(ROOT / 'tests' / name, self.fixture / name)
        self.weights = self.fixture / 'shard-weights.tsv'
        self.weights.write_text('# test_file\tubuntu_seconds\tmacos_seconds\n')
        self.env = {key: value for key, value in os.environ.items()
                    if key not in ('BUREAU_TEST_SHARD', 'BUREAU_TEST_MANIFEST',
                                   'BUREAU_TEST_TIMINGS', 'BUREAU_TEST_FILE_TIMEOUT')}
        self.env.update(BUREAU_TEST_FILE_TIMEOUT='1', TMPDIR=str(self.fixture))
        self.addCleanup(self.cleanup_groups)

    def plant(self, name, body):
        (self.fixture / name).write_text(body)

    def harness(self, invoking=None, **changes):
        return subprocess.run(['/bin/bash', str(self.fixture / 'run.sh')], cwd=invoking or self.fixture,
                              env={**self.env, **changes}, text=True, capture_output=True, timeout=15)

    def cleanup_groups(self):
        for name in ('hang.group', 'child.group'):
            path = self.fixture / name
            if path.exists():
                try: os.killpg(int(path.read_text()), signal.SIGKILL)
                except ProcessLookupError: pass
                except PermissionError:
                    if process_running(int(path.read_text())): raise

    def test_template_imports_leave_no_bytecode_through_either_runner(self):
        scripts = self.fixture / 'templates/scripts'
        scripts.mkdir(parents=True)
        shutil.copy2(ROOT / 'templates/scripts/bureau-paths.py', scripts / 'bureau-paths.py')
        self.plant('import_template.py', '''import importlib.util
from pathlib import Path
spec = importlib.util.spec_from_file_location('template_paths', 'templates/scripts/bureau-paths.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
Path('imported').write_text(module.normalize('./src/file.py:12'))
''')
        self.plant('test_import.sh', '''set -eu
[ "${PYTHONDONTWRITEBYTECODE:-}" = 1 ]
python3 import_template.py
''')
        env = {key: value for key, value in self.env.items()
               if key not in ('PYTHONDONTWRITEBYTECODE', 'PYTHONPYCACHEPREFIX')}
        control = subprocess.run([sys.executable, 'import_template.py'], cwd=self.fixture, env=env,
                                 text=True, capture_output=True, timeout=5)
        self.assertEqual(control.returncode, 0, control.stdout + control.stderr)
        self.assertTrue(list(scripts.rglob('*.pyc')), 'control import did not create bytecode')
        shutil.rmtree(scripts / '__pycache__')

        manifest = self.fixture / 'manifest.txt'
        manifest.write_text('test_import.sh\n')
        commands = (['/bin/bash', str(self.fixture / 'run.sh')],
                    [sys.executable, '-I', str(self.fixture / 'run_test_files.py'), 'test_import.sh'])
        for command in commands:
            for inherited in (None, ''):
                with self.subTest(runner=Path(command[-2]).name, inherited=inherited):
                    run_env = {**env, 'BUREAU_TEST_MANIFEST': str(manifest)}
                    if inherited is not None:
                        run_env['PYTHONDONTWRITEBYTECODE'] = inherited
                    (self.fixture / 'imported').unlink()
                    completed = subprocess.run(command, cwd=self.fixture, env=run_env,
                                               text=True, capture_output=True, timeout=15)
                    self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
                    self.assertIn('PASS  test_import.sh', completed.stdout)
                    self.assertEqual((self.fixture / 'imported').read_text(), 'src/file.py')
                    self.assertEqual(list(self.fixture.rglob('__pycache__')), [])
                    self.assertEqual(list(self.fixture.rglob('*.pyc')), [])

    def assert_permission_cleanup(self, stop, survives):
        child = Mock(pid=12345, returncode=0)
        now = [0.]
        reaped = []
        signals = []

        def killpg(group, signum):
            self.assertEqual(group, child.pid)
            signals.append((signum, now[0]))
            raise PermissionError('signal denied')

        def sleep(seconds):
            now[0] += seconds

        with patch.object(runner, 'stop_group', stop), \
                patch.object(runner.subprocess, 'Popen', return_value=child), \
                patch.object(runner, 'reap_group', side_effect=lambda child: reaped.append(child)), \
                patch.object(runner.os, 'killpg', side_effect=killpg), \
                patch.object(runner, 'process_group_running',
                             side_effect=lambda group: survives or len(reaped) < 3), \
                patch.object(runner.time, 'monotonic', side_effect=lambda: now[0]), \
                patch.object(runner.time, 'sleep', side_effect=sleep):
            log = io.StringIO()
            outcome, elapsed = runner.run_file('test_probe.sh', 5, log)
        self.assertEqual(outcome, 'FAIL' if survives else 'PASS', log.getvalue())
        if survives:
            self.assertIn(f'process group {child.pid} survived cleanup', log.getvalue())
            self.assertEqual([signum for signum, _ in signals], [signal.SIGTERM, signal.SIGKILL])
            self.assertAlmostEqual(signals[1][1], 1)
            self.assertAlmostEqual(elapsed, 4)
        else:
            self.assertEqual(signals, [(signal.SIGTERM, 0)])
            self.assertGreaterEqual(len(reaped), 4)
            self.assertGreater(elapsed, 0)
            self.assertLess(elapsed, 1)

    def cleanup_mutant(self, old, new):
        source = inspect.getsource(runner.stop_group)
        self.assertEqual(source.count(old), 1)
        namespace = {}
        exec(source.replace(old, new), vars(runner), namespace)
        return namespace['stop_group']

    def test_cleanup_recovers_from_transient_permission_error_after_reaping(self):
        self.assert_permission_cleanup(runner.stop_group, False)
        immediate_raise = self.cleanup_mutant(
            'except PermissionError:\n            pass',
            'except PermissionError:\n            if process_group_running(child.pid):\n                raise')
        with self.assertRaisesRegex(AssertionError, 'Runner cleanup failed: signal denied'):
            self.assert_permission_cleanup(immediate_raise, False)

    def test_cleanup_persistent_survivor_fails_after_kill_grace(self):
        self.assert_permission_cleanup(runner.stop_group, True)
        ignores_survivor = self.cleanup_mutant(
            "raise RuntimeError(f'process group {child.pid} survived cleanup')", 'return')
        with self.assertRaisesRegex(AssertionError, "'PASS' != 'FAIL'"):
            self.assert_permission_cleanup(ignores_survivor, True)

    def test_timeout_and_background_cleanup_keep_output_and_continue(self):
        self.plant('child.py', '''import os, signal, time
from pathlib import Path
signal.signal(signal.SIGTERM, signal.SIG_IGN)
Path('child.pid').write_text(str(os.getpid()))
Path('child.group').write_text(str(os.getpgrp()))
while True: time.sleep(1)
''')
        bodies = {
            'test_01_hang.sh': '''echo hang-output
echo hang-stderr >&2
echo $$ > hang.group
trap '' TERM
while :; do sleep 1; done
''',
            'test_02_background.sh': '''python3 child.py &
while [ ! -s child.pid ]; do sleep 0.01; done
echo background-started
''',
            'test_03_fail.sh': 'echo failure-output; exit 7\n',
            'test_04_next.sh': 'echo next-ran > next-ran; echo "SKIP fixture subcase"\n',
        }
        for name, body in bodies.items():
            self.plant(name, body)
        for os_name in ('Linux', 'macOS'):
            with self.subTest(os=os_name):
                try:
                    timings = self.fixture / 'timings.tsv'
                    completed = self.harness(RUNNER_OS=os_name, BUREAU_TEST_TIMINGS=str(timings))
                    output = completed.stdout + completed.stderr
                    self.assertEqual(completed.returncode, 1, output)
                    for text in ('TIMEOUT  test_01_hang.sh (deadline 1s)', 'FAIL  test_01_hang.sh',
                                 'hang-output', 'hang-stderr', 'PASS  test_02_background.sh',
                                 'FAIL  test_03_fail.sh', 'failure-output', 'PASS  test_04_next.sh',
                                 '      SKIP fixture subcase', 'Results: 2 passed, 2 failed'):
                        self.assertIn(text, output)
                    self.assertEqual((self.fixture / 'next-ran').read_text().strip(), 'next-ran')
                    with timings.open() as source:
                        rows = list(csv.DictReader(source, delimiter='\t'))
                    self.assertEqual([row['test_file'] for row in rows], sorted(bodies))
                    self.assertEqual([row['outcome'] for row in rows], ['TIMEOUT', 'PASS', 'FAIL', 'PASS'])
                    self.assertTrue(all(row['os'] == os_name and float(row['elapsed_seconds']) >= 0 for row in rows))
                    self.assertGreaterEqual(float(rows[0]['elapsed_seconds']), 1)
                    self.assertLess(float(rows[0]['elapsed_seconds']), 8)
                    pids = {int((self.fixture / name).read_text()) for name in ('hang.group', 'child.pid')}
                    self.assertFalse(wait_for_processes_exit(pids, time.monotonic() + 3), 'fixture process survived runner')
                finally:
                    self.cleanup_groups()
                    for name in ('hang.group', 'child.group', 'child.pid', 'next-ran'):
                        (self.fixture / name).unlink(missing_ok=True)

    def test_orphan_killed_by_its_test_disappears_while_the_test_runs(self):
        self.plant('test_orphan.sh', '''( sleep 30 & echo $! > orphan.pid )
pid=$(cat orphan.pid) && [ -n "$pid" ] && kill "$pid" || { echo "could not kill the orphan"; exit 2; }
for _ in $(seq 50); do kill -0 "$pid" 2>/dev/null || exit 0; sleep 0.1; done
echo "orphan $pid survived its kill"; exit 1
''')
        completed = self.harness(BUREAU_TEST_FILE_TIMEOUT='10')
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        self.assertIn('PASS  test_orphan.sh', completed.stdout)

    def test_invalid_deadlines_fail_before_running_tests(self):
        self.plant('test_probe.sh', 'echo ran > executed\n')
        for value in ('', '0', '-1', '1.5', '+1', ' 1', '1\n', 'no'):
            with self.subTest(value=value):
                completed = self.harness(BUREAU_TEST_FILE_TIMEOUT=value)
                self.assertNotEqual(completed.returncode, 0)
                self.assertIn('BUREAU_TEST_FILE_TIMEOUT', completed.stderr)
                self.assertFalse((self.fixture / 'executed').exists())

    def test_weighted_defaults_use_os_estimates_and_floor(self):
        names = ['test_linux.sh', 'test_macos.sh', 'test_small.sh', 'test_unknown.sh']
        for name in names:
            self.plant(name, 'exit 0\n')
        measured = dict(zip(names, [(300, None), (None, 501), (1, 1)]))
        previous = Path.cwd()
        try:
            os.chdir(self.fixture)
            for os_name, deadlines in (('Linux', [900, 753, 600, 600]), ('macOS', [1800, 1503, 600, 600])):
                with self.subTest(os=os_name), patch.dict(os.environ, {'RUNNER_OS':os_name}, clear=True), \
                        patch.object(runner, 'read_weights', return_value=measured), \
                        patch.object(runner, 'run_file', return_value=('PASS', .1)) as run, redirect_stdout(io.StringIO()):
                    self.assertEqual(runner.main(names), 0)
                    self.assertEqual([call.args[1] for call in run.call_args_list], deadlines)
        finally:
            os.chdir(previous)

    def test_timings_are_opt_in_and_relative_to_invoking_directory(self):
        self.plant('test_pass.sh', 'exit 0\n')
        completed = self.harness()
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        self.assertEqual(sorted(p.name for p in self.fixture.glob('*.tsv')), ['shard-weights.tsv'])
        invoking = self.fixture / 'invoking'; invoking.mkdir()
        completed = self.harness(invoking=invoking, BUREAU_TEST_TIMINGS='timings.tsv')
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        with (invoking / 'timings.tsv').open() as source:
            row, = csv.DictReader(source, delimiter='\t')
        self.assertEqual((row['test_file'], row['outcome']), ('test_pass.sh', 'PASS'))
        self.assertIn(row['os'], ('Linux', 'macOS'))
        self.assertFalse((self.fixture / 'timings.tsv').exists())
        completed = self.update(invoking / 'timings.tsv')
        self.assertEqual(completed.returncode, 0, completed.stderr)
        name, linux, macos = self.weights.read_text().splitlines()[-1].split()
        self.assertEqual(name, 'test_pass.sh')
        measured, other = (linux, macos) if row['os'] == 'Linux' else (macos, linux)
        self.assertGreaterEqual(int(measured), 1)
        self.assertEqual(other, '-')

    def update(self, *paths):
        return subprocess.run([sys.executable, '-I', str(ROOT / 'tests/update_shard_weights.py'),
                               '--weights', str(self.weights), *map(str, paths)],
                              text=True, capture_output=True, timeout=5)

    def test_update_changes_only_measured_columns_and_preserves_comments(self):
        original = ('# retained comment\r\n# test_file\tubuntu_seconds\tmacos_seconds\r\n'
                    'test_a.sh\t7\t-\r\n'
                    'test_b.sh  -  42\r\n'
                    '# middle comment\r\n'
                    'test_unmeasured.sh\t-\t-\r\n')
        self.weights.write_bytes(original.encode())
        first = self.fixture / 'linux.tsv'
        second = self.fixture / 'macos.tsv'
        third = self.fixture / 'linux-later.tsv'
        first.write_text(runner.TIMING_HEADER + 'test_a.sh\t10.01\tPASS\tLinux\n'
                         'test_new.sh\t2.4\tPASS\tLinux\n')
        second.write_text(runner.TIMING_HEADER + 'test_b.sh\t5.01\tPASS\tmacOS\n')
        third.write_text(runner.TIMING_HEADER + 'test_a.sh\t8.9\tPASS\tLinux\n')
        completed = self.update(first, second, third)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        expected = original.replace('test_a.sh\t7\t-', 'test_a.sh\t11\t-').replace('test_b.sh  -  42', 'test_b.sh  -  6')
        expected += 'test_new.sh\t3\t-\n'
        self.assertEqual(self.weights.read_bytes(), expected.encode())

    def test_update_rejects_failed_and_invalid_timings_without_editing_weights(self):
        saved = self.weights.read_bytes()
        timings = self.fixture / 'timings.tsv'
        for elapsed, outcome, os_name in (('1', 'FAIL', 'Linux'), ('1', 'TIMEOUT', 'Linux'),
                                          ('NaN', 'PASS', 'Linux'), ('Infinity', 'PASS', 'macOS'),
                                          ('-1', 'PASS', 'Linux'), ('1', 'PASS', 'unknown')):
            with self.subTest(elapsed=elapsed, outcome=outcome, os=os_name):
                timings.write_text(runner.TIMING_HEADER + f'test_a.sh\t{elapsed}\t{outcome}\t{os_name}\n')
                completed = self.update(timings)
                self.assertNotEqual(completed.returncode, 0)
                self.assertEqual(self.weights.read_bytes(), saved)


if __name__ == '__main__':
    unittest.main()
