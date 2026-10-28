"""Exercise the real control runner with a short timeout and baseline errors."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from mutation_checks import caught_mutation, mutation_caught


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

    def test_in_process_failure_and_errors_are_caught(self):
        for error in (AssertionError('mutation'), ValueError('mutation'),
                      subprocess.TimeoutExpired('fixture', .05), SystemExit(2)):
            with self.subTest(error=type(error).__name__), caught_mutation():
                raise error
        with self.assertRaisesRegex(AssertionError, 'mutation was not caught'):
            with caught_mutation():
                pass

    def test_user_interrupt_is_not_swallowed(self):
        with self.assertRaises(KeyboardInterrupt):
            with caught_mutation():
                raise KeyboardInterrupt


if __name__ == '__main__':
    unittest.main(verbosity=2)
