"""Exercise the real control runner with a short timeout and baseline errors."""
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from mutation_checks import caught_mutation, mutation_caught, restored_files, run, snapshot_files


class MutationChecks(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='bureau-mutation-checks-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.source = self.root/'subject.py'
        self.baseline = self.root/'baseline.py'
        self.baseline.write_text('def work(): pass\n')
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
                               baseline_env={'CONTROL_SOURCE': str(self.baseline)}, timeout=timeout)

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
    child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])
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
        # This unrelated process shares the harness's parent and temp platform,
        # but never receives the run's ownership marker.
        unrelated = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])
        self.addCleanup(self.stop_child, unrelated)
        pid = self.root/'detached-pids'
        self.source.write_text(f'''import os, signal, subprocess, sys, time
from pathlib import Path
def work():
    child = subprocess.Popen([sys.executable, '-c',
        'import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(30)'],
        start_new_session=True)
    def stop(*args):
        child.wait(timeout=3)
        raise SystemExit(0)
    signal.signal(signal.SIGTERM, stop)
    Path({str(pid)!r}).write_text(str(os.getpid()) + '\\n' + str(child.pid))
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

    @staticmethod
    def stop_child(child):
        if child.poll() is None:
            child.kill()
        child.wait(timeout=3)

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
        self.source.write_text('def work(): pass\n')
        originals = snapshot_files(self.root)
        mutation = 'def work(): raise ValueError("private mutation")\n'
        self.source.write_text(mutation)
        result = mutation_caught(self.command, env={'CONTROL_SOURCE': str(self.source)}, originals=originals)
        self.assertIn('private mutation', result.stderr)
        self.assertEqual(mutation, self.source.read_text())

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
