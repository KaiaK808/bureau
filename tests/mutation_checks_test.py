"""Exercise the real control runner with a short timeout and baseline errors."""
import os
import json
import io
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from mutation_checks import (_run, baseline_fixture, caught_mutation, fixture_pids, historical_scripts,
                             macos_cwds, mutation_caught, restored_files, run, snapshot_files,
                             stop_fixtures)


class MutationChecks(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='bureau-mutation-checks-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.source = self.root/'subject.py'
        self.baseline = self.root/'baseline.py'
        self.baseline.write_text('def work(): pass\n')
        self.source.write_bytes(self.baseline.read_bytes())
        self.test = self.root/'probe.py'
        self.test.write_text('''import importlib.util, os, subprocess, sys, unittest
spec = importlib.util.spec_from_file_location('subject', os.environ['CONTROL_SOURCE'])
subject = importlib.util.module_from_spec(spec); spec.loader.exec_module(subject)
class Probe(unittest.TestCase):
    def test_work(self): subject.work()
if __name__ == '__main__': unittest.main()
''')
        self.command = [sys.executable, str(self.test)]

    def control(self, timeout=5):
        return mutation_caught(self.command, env={'CONTROL_SOURCE': str(self.source)},
                               originals={self.source:self.baseline.read_bytes()}, timeout=timeout)

    def test_target_timeout_error_is_caught(self):
        # subprocess.run kills and joins this child before unittest reports ERROR.
        self.source.write_text('''import subprocess, sys
def work():
    subprocess.run([sys.executable, '-c', 'import time; time.sleep(30)'], timeout=.05)
''')
        result = self.control()
        self.assertIn('TimeoutExpired', result.stderr)
        self.assertIn('FAILED (errors=1)', result.stderr)

    def test_other_target_error_is_caught(self):
        self.source.write_text('def work(): raise ValueError("mutated behavior")\n')
        self.assertIn('FAILED (errors=1)', self.control().stderr)

    def test_assertion_failure_is_caught(self):
        self.source.write_text('def work(): assert False, "mutated behavior"\n')
        self.assertIn('FAILED (failures=1)', self.control().stderr)

    def test_surviving_mutation_is_rejected(self):
        self.source.write_text(self.baseline.read_text())
        with self.assertRaisesRegex(AssertionError, 'mutation was not caught'):
            self.control()

    def test_baseline_error_fails_before_mutation(self):
        self.baseline.write_text('def work(): raise ValueError("broken baseline")\n')
        marker = self.root/'mutation-ran'
        self.source.write_text(f'from pathlib import Path\nPath({str(marker)!r}).touch()\n')
        with self.assertRaisesRegex(AssertionError, 'unmutated baseline failed:') as error:
            self.control()
        self.assertIn('FAILED (errors=1)', str(error.exception))
        self.assertFalse(marker.exists())

    def test_outer_timeout_is_caught_and_fixture_processes_are_reaped(self):
        pid = self.root/'pid'
        self.source.write_text(f'''import os, signal, subprocess, sys, time
from pathlib import Path
def work():
    script = Path(os.environ['TMPDIR'])/'sleeper.py'
    script.write_text('import time; time.sleep(30)')
    child = subprocess.Popen([sys.executable, str(script)])
    def stop(*args):
        child.wait(timeout=3)
        raise SystemExit(0)
    signal.signal(signal.SIGTERM, stop)
    Path({str(pid)!r}).write_text(str(os.getpid()) + '\\n' + str(child.pid))
    time.sleep(30)
''')
        self.assertEqual(124, self.control().returncode)
        for raw in pid.read_text().splitlines():
            with self.subTest(pid=raw), self.assertRaises(ProcessLookupError):
                os.kill(int(raw), 0)

    def test_baseline_timeout_fails(self):
        self.baseline.write_text('import time\ndef work(): time.sleep(30)\n')
        with self.assertRaisesRegex(AssertionError, 'unmutated baseline failed:'):
            self.control(timeout=1)

    def test_detached_timeout_reaps_only_its_own_descendants(self):
        # Fail before creating any detached process if inspection is forbidden.
        fixture_pids(self.root)
        # This unrelated sibling shares the parent and platform temp directory.
        unrelated = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])
        self.addCleanup(self.stop_child, unrelated)
        pid = self.root/'detached-pids'
        self.addCleanup(self.stop_recorded, pid)
        sleeper = f'''import os, signal, time
from pathlib import Path
signal.signal(signal.SIGTERM, signal.SIG_IGN)
Path({str(pid)!r}).write_text(str(os.getpid()))
time.sleep(30)
'''
        self.source.write_text(f'''import os, signal, subprocess, sys, time
from pathlib import Path
def work():
    script = Path(os.environ['TMPDIR'])/'detached.py'
    script.write_text({sleeper!r})
    code = 'import os, subprocess, sys; subprocess.Popen([sys.executable, sys.argv[1]], '
    code += 'cwd="/", start_new_session=True, env={{**os.environ, "BUREAU_ACTIVITY_OWNER":"replaced"}})'
    parent = subprocess.Popen([sys.executable, '-c', code, str(script)])
    parent.wait(timeout=3)  # Its descendant is already orphaned before cleanup.
    time.sleep(30)
''')
        started = time.monotonic()
        result = self.control(timeout=1)
        self.assertEqual(124, result.returncode)
        self.assertLess(time.monotonic() - started, 8)
        self.assertIsNone(unrelated.poll(), 'timeout cleanup stopped an unrelated process')
        for raw in pid.read_text().splitlines():
            with self.subTest(pid=raw), self.assertRaises(ProcessLookupError):
                os.kill(int(raw), 0)

    def test_rejects_unbounded_deadlines_before_starting_any_process(self):
        for timeout in (None, float('inf'), float('nan'), 0, -1):
            with self.subTest(timeout=timeout), patch('mutation_checks.subprocess.Popen') as start:
                with self.assertRaisesRegex(ValueError, 'positive finite'):
                    run(self.command, timeout=timeout)
                with self.assertRaisesRegex(ValueError, 'positive finite'):
                    mutation_caught(self.command, env={}, originals={}, timeout=timeout)
                start.assert_not_called()

    def test_ps_scan_is_bounded_and_does_not_match_a_sibling_root(self):
        output = f'101 python {self.root}/child.py\n102 python {self.root}-sibling/child.py\n'
        output += f'103 python --repo="{self.root}/repo with spaces"\n'
        with patch('mutation_checks.sys.platform', 'darwin'), \
                patch('mutation_checks.subprocess.run', return_value=Mock(stdout=output)) as ps:
            self.assertEqual({101, 103}, fixture_pids(self.root, cwds=lambda: {}))
        self.assertEqual(['ps', '-axo', 'pid=,command='], ps.call_args.args[0])
        self.assertEqual(1, ps.call_args.kwargs['timeout'])
        self.assertTrue(ps.call_args.kwargs['check'])

    def test_macos_lsof_cwd_scan_parses_pid_fields_and_is_bounded(self):
        output = f'n/ignored\np101\nfcwd\nn{self.root}/with spaces\n'
        output += f'p102\nfcwd\nn{self.root}-sibling\npinvalid\nn/ignored\n'
        with patch('mutation_checks.subprocess.run', return_value=Mock(stdout=output)) as lsof:
            self.assertEqual({101: str(self.root/'with spaces'), 102: str(self.root)+'-sibling'},
                             macos_cwds())
        self.assertEqual(['lsof', '-nP', '-a', '-d', 'cwd', '-Fpn'], lsof.call_args.args[0])
        self.assertEqual(1, lsof.call_args.kwargs['timeout'])
        self.assertTrue(lsof.call_args.kwargs['check'])

    def test_macos_cwd_scan_reaps_argvless_descendant_and_preserves_sibling(self):
        commands = lambda: (f'101 python -c "import time; time.sleep(30)"\n'
                            f'102 python {self.root}-sibling/child.py\n'
                            '103 python -c "import time; time.sleep(30)"\n')
        directories = {101: str(self.root/'nested with spaces'),
                       102: str(self.root)+'-sibling',
                       103: str(self.root/'..'/'outside'), os.getpid(): str(self.root)}
        stopped = set()
        reaped = []

        def discover(root):
            return fixture_pids(root, commands=commands, cwds=lambda: directories.copy())

        def reap(pid, flags):
            if pid in stopped:
                directories.pop(pid)
                stopped.remove(pid)
                reaped.append(pid)
                return pid, 0
            return 0, 0

        child = Mock(pid=100, poll=Mock(return_value=0))
        with patch('mutation_checks.sys.platform', 'darwin'), \
                patch('mutation_checks.fixture_pids', side_effect=discover), \
                patch('mutation_checks.os.kill', side_effect=lambda pid, sig: stopped.add(pid)) as kill, \
                patch('mutation_checks.os.waitpid', side_effect=reap):
            self.assertEqual({101}, discover(self.root))
            stop_fixtures(self.root, child)
        kill.assert_called_once_with(101, signal.SIGTERM)
        self.assertEqual([101], reaped)
        self.assertIn(102, directories, 'cwd cleanup stopped an unrelated sibling')

    def test_macos_cwd_discovery_failure_reports_argv_fallback(self):
        for error in (FileNotFoundError('lsof unavailable'),
                      subprocess.TimeoutExpired('lsof', 1),
                      subprocess.CalledProcessError(1, 'lsof')):
            with self.subTest(error=type(error).__name__), \
                    patch('mutation_checks.sys.platform', 'darwin'), \
                    patch('mutation_checks.sys.stderr', new_callable=io.StringIO) as report:
                found = fixture_pids(self.root,
                                     commands=lambda: f'101 python {self.root}/child.py\n',
                                     cwds=Mock(side_effect=error))
                self.assertEqual({101}, found)
                self.assertIn(str(self.root), report.getvalue())
                self.assertIn(str(error), report.getvalue())
                self.assertIn('falling back to argv matching', report.getvalue())

    @unittest.skipUnless(sys.platform == 'darwin', 'live macOS cwd discovery')
    def test_live_macos_cwd_cleanup_reaps_python_c_sleeper(self):
        # Exercise real lsof even in sandboxes that deny ps inspection.
        try:
            macos_cwds()
        except (OSError, subprocess.SubprocessError) as error:
            self.skipTest(f'lsof cwd discovery unavailable: {error}')
        sibling = self.root.with_name(self.root.name+'-sibling')
        sibling.mkdir()
        self.addCleanup(sibling.rmdir)
        unrelated = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'], cwd=sibling)
        self.addCleanup(self.stop_child, unrelated)
        pid = self.root/'cwd-pids'
        self.addCleanup(self.stop_recorded, pid)
        command = [sys.executable, '-c', f'''import os, signal, subprocess, sys, time
from pathlib import Path
child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'], start_new_session=True)
def stop(*args):
    child.wait(timeout=3)
    raise SystemExit(0)
signal.signal(signal.SIGTERM, stop)
Path({str(pid)!r}).write_text(str(os.getpid()) + '\\n' + str(child.pid))
time.sleep(30)
''']
        with patch('mutation_checks.fixture_pids',
                   side_effect=lambda root: fixture_pids(root, commands=lambda: '')):
            result = _run(command, env=None, timeout=1, root=self.root)
        self.assertEqual(124, result.returncode)
        self.assertIsNone(unrelated.poll(), 'cwd cleanup stopped an unrelated sibling')
        for raw in pid.read_text().splitlines():
            with self.subTest(pid=raw), self.assertRaises(ProcessLookupError):
                os.kill(int(raw), 0)

    def test_linux_scan_finds_cwd_without_a_matching_command(self):
        proc = self.root/'proc'; proc.mkdir()
        for pid, cwd in ((101,self.root), (102,self.root/'inside'), (103,self.root.parent)):
            cwd.mkdir(exist_ok=True)
            directory = proc/str(pid); directory.mkdir(); (directory/'cwd').symlink_to(cwd)
        deleted = proc/'104'; deleted.mkdir()
        (deleted/'cwd').symlink_to(str(self.root)+' (deleted)')
        with patch('mutation_checks.sys.platform', 'linux'), patch('mutation_checks.PROC', proc), \
                patch('mutation_checks.subprocess.run', return_value=Mock(stdout='101 sleep 30\n')):
            self.assertEqual({101, 102, 104}, fixture_pids(self.root))

    def test_scan_failure_prevents_fixture_launch(self):
        with patch('mutation_checks.fixture_pids', side_effect=PermissionError('inspection denied')), \
                patch('mutation_checks.subprocess.Popen') as start:
            with self.assertRaisesRegex(PermissionError, 'inspection denied'):
                run(self.command)
        start.assert_not_called()

    def test_cleanup_escalates_and_reports_every_survivor(self):
        child = Mock(pid=123, poll=Mock(return_value=0))
        ticks = iter(range(0, 100, 10))
        with patch('mutation_checks.fixture_pids', return_value={456, 789}), \
                patch('mutation_checks.os.waitpid', side_effect=ChildProcessError), \
                patch('mutation_checks.os.kill', side_effect=PermissionError) as kill, \
                patch('mutation_checks.time.monotonic', side_effect=lambda: next(ticks)):
            with self.assertRaisesRegex(AssertionError, 'could not reap fixture PIDs: 456, 789'):
                stop_fixtures(self.root, child)
        self.assertEqual([(pid, sig) for sig in (signal.SIGTERM, signal.SIGKILL) for pid in (456,789)],
                         [call.args for call in kill.call_args_list])

    @staticmethod
    def stop_child(child):
        if child.poll() is None:
            child.kill()
        child.wait(timeout=3)

    @staticmethod
    def stop_recorded(path):
        if not path.exists():
            return
        for raw in path.read_text().splitlines():
            pid = int(raw)
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                continue
            except PermissionError as error:
                raise AssertionError(f'could not kill fixture PID {pid}') from error
            deadline = time.monotonic() + 3
            while True:
                try:
                    os.waitpid(pid, os.WNOHANG)
                except ChildProcessError:
                    pass
                try:
                    os.kill(pid, 0)
                except ProcessLookupError:
                    break
                if time.monotonic() >= deadline:
                    raise AssertionError(f'could not reap fixture PID {pid}')
                time.sleep(.05)

    def test_final_pipe_drain_timeout_is_bounded_and_preserves_output(self):
        communicate = subprocess.Popen.communicate
        calls = []

        def pipe_stays_open(child, *args, **kwargs):
            calls.append(kwargs['timeout'])
            if child.poll() is not None:
                raise subprocess.TimeoutExpired(child.args, kwargs['timeout'],
                                                output=b'partial stdout', stderr=b'partial stderr')
            return communicate(child, *args, **kwargs)

        with patch.object(subprocess.Popen, 'communicate', pipe_stays_open):
            result = run([sys.executable, '-c', 'import time; time.sleep(30)'], timeout=.1)
        self.assertEqual(124, result.returncode)
        self.assertEqual('partial stdout', result.stdout)
        self.assertIn('partial stderr', result.stderr)
        self.assertIn('fixture output drain timed out', result.stderr)
        self.assertEqual(1, calls[-1])

    def test_private_baseline_uses_the_same_files_and_environment(self):
        log = self.root/'contexts'
        source = f'''import json, os
from pathlib import Path
def work():
    with Path({str(log)!r}).open('a') as out:
        out.write(json.dumps([__file__, os.getcwd(), os.environ['TMPDIR']]) + '\\n')
'''
        self.source.write_text(source)
        originals = snapshot_files(self.root)
        mutation = source + '    raise ValueError("private mutation")\n'
        self.source.write_text(mutation)
        result = mutation_caught(self.command, env={'CONTROL_SOURCE': str(self.source)}, originals=originals)
        self.assertIn('private mutation', result.stderr)
        self.assertEqual(mutation, self.source.read_text())
        contexts = [json.loads(line) for line in log.read_text().splitlines()]
        self.assertEqual(2, len(contexts))
        self.assertEqual(contexts[0], contexts[1])
        self.assertEqual(contexts[0][1], contexts[0][2])
        self.assertEqual(str(Path(contexts[0][1]).resolve()), contexts[0][1])

    def test_separate_runs_have_distinct_resolved_roots(self):
        command = [sys.executable, '-c', 'import os; print(os.getcwd()); print(os.environ["TMPDIR"])']
        first = run(command).stdout.splitlines()
        second = run(command).stdout.splitlines()
        self.assertEqual(first[0], first[1])
        self.assertEqual(second[0], second[1])
        self.assertNotEqual(first[0], second[0])
        for directory in (first[0],second[0]):
            self.assertEqual(str(Path(directory).resolve()), directory)
            self.assertFalse(Path(directory).exists())

    def test_private_inventory_error_is_not_a_caught_mutation(self):
        self.source.write_text('import missing_private_dependency\ndef work(): pass\n')
        originals = snapshot_files(self.root)
        self.source.write_text('def work(): assert False, "mutation"\n')
        with self.assertRaisesRegex(AssertionError, 'unmutated baseline failed:') as error:
            mutation_caught(self.command, env={'CONTROL_SOURCE': str(self.source)}, originals=originals)
        self.assertIn('missing_private_dependency', str(error.exception))
        self.assertIn('mutation', self.source.read_text())

    def test_dispatcher_mutation_setup_failure_is_not_caught(self):
        from dispatcher_negative_control import SemaphoreControl
        scripts = self.root/'scripts'; scripts.mkdir()
        (scripts/'bureau-dispatch.py').write_text(
            'while (len(self.lanes) < self.limit) and not self.stopping:\n    pass\n')
        control = SemaphoreControl('test_private_semaphore_bypass'); control.repo = self.root
        with patch.object(control, 'ticket'), patch.object(control, 'start') as start:
            with self.assertRaisesRegex(AssertionError, 'semaphore mutation anchor changed'):
                control.test_private_semaphore_bypass()
            start.assert_not_called()

    def test_dispatcher_baseline_error_is_not_a_caught_mutation(self):
        from dispatcher_negative_control import SemaphoreControl
        scripts = self.root/'scripts'; scripts.mkdir()
        path = scripts/'bureau-dispatch.py'
        original = 'while len(self.lanes) < self.limit and not self.stopping:\n    pass\n'
        path.write_text(original)
        control = SemaphoreControl('test_private_semaphore_bypass'); control.repo = self.root
        with patch.object(control, 'ticket'), patch.object(control, 'start') as start, \
                patch.object(control, 'baseline', side_effect=FileNotFoundError('private inventory')):
            with self.assertRaisesRegex(FileNotFoundError, 'private inventory'):
                control.test_private_semaphore_bypass()
            start.assert_not_called()
        self.assertEqual(original, path.read_text())

    def test_baseline_fixture_restores_paths_inventory_and_state(self):
        before = snapshot_files(self.root)
        with baseline_fixture(self.root):
            self.baseline.write_text('baseline changed state')
            (self.root/'added').touch()
            self.test.unlink()
        self.assertEqual(before, snapshot_files(self.root))
        with self.assertRaisesRegex(ValueError, 'baseline'):
            with baseline_fixture(self.root):
                (self.root/'failure-evidence').touch()
                raise ValueError('baseline')
        self.assertTrue((self.root/'failure-evidence').exists())

    def test_historical_baseline_uses_the_complete_private_inventory(self):
        repo = self.root/'archive-source'; scripts = repo/'templates/scripts'; scripts.mkdir(parents=True)
        path = scripts/'subject.py'
        path.write_text('def work(): raise ValueError("historical mutation")\n')
        env = {**os.environ, 'GIT_CONFIG_GLOBAL':'/dev/null', 'GIT_CONFIG_NOSYSTEM':'1'}
        for args in (('init','-q'), ('add','.'),
                     ('-c','user.name=fixture','-c','user.email=fixture@bureau','commit','-qm','old behavior')):
            subprocess.run(['git','-C',str(repo),*args], env=env, check=True, capture_output=True, timeout=5)
        path.write_text('from pathlib import Path\ndef work(): Path(__file__).with_name("companion").read_text()\n')
        (scripts/'companion').write_text('current companion inventory')
        with historical_scripts(repo, 'HEAD') as (private, originals):
            self.assertIn('historical mutation', (private/'subject.py').read_text())
            self.assertEqual('current companion inventory', (private/'companion').read_text())
            result = mutation_caught(self.command, env={'CONTROL_SOURCE':str(private/'subject.py')},
                                     originals=originals)
            self.assertIn('historical mutation', result.stderr)
            (private/'companion').unlink()
            # An absent companion must break the baseline, never count as detection.
            with self.assertRaises(FileNotFoundError):
                mutation_caught(self.command, env={'CONTROL_SOURCE':str(private/'subject.py')},
                                originals=originals)

    def test_file_restoration_survives_a_baseline_exception(self):
        self.source.write_bytes(b'mutated')
        with self.assertRaisesRegex(ValueError, 'baseline'):
            with restored_files({self.source: b'original'}):
                self.assertEqual(b'original', self.source.read_bytes())
                raise ValueError('baseline')
        self.assertEqual(b'mutated', self.source.read_bytes())

    def test_in_process_failure_and_errors_are_caught(self):
        for error in (AssertionError('mutation'), ValueError('mutation'),
                      subprocess.TimeoutExpired('fixture', .05), SystemExit(2)):
            with self.subTest(error=type(error).__name__), caught_mutation() as caught:
                raise error
            self.assertEqual([error], caught)
        with self.assertRaisesRegex(AssertionError, 'mutation was not caught'):
            with caught_mutation():
                pass

    def test_user_interrupt_is_not_swallowed(self):
        with self.assertRaises(KeyboardInterrupt):
            with caught_mutation():
                raise KeyboardInterrupt


if __name__ == '__main__':
    unittest.main(verbosity=2)
