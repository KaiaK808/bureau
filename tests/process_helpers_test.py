"""Real live, stopped, zombie and vanished fixtures, plus platform stat parsing."""
import errno
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from process_helpers import process_group_running, process_running


class ProcessHelpers(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='bureau-process-helper-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()

    def fork_fixture(self):
        # EOF on ready proves the child has exited. Its parent deliberately
        # does not wait until cleanup, so the PID deterministically stays Z.
        ready, ready_writer = os.pipe()
        finish_reader, finish = os.pipe()
        pid = os.fork()
        if pid == 0:
            os.close(ready); os.close(finish)
            os.chdir(self.root)
            os.setsid()
            os.write(ready_writer, b'r')
            os.read(finish_reader, 1)
            os._exit(0)
        os.close(ready_writer); os.close(finish_reader)
        self.addCleanup(os.close, ready)
        self.addCleanup(os.close, finish)
        self.addCleanup(self.reap, pid)
        self.assertEqual(b'r', os.read(ready, 1))
        return pid, ready, finish

    @staticmethod
    def reap(pid):
        try: os.kill(pid, signal.SIGKILL)
        except ProcessLookupError: pass
        os.waitpid(pid, 0)

    def test_running_and_stopped_processes_are_running(self):
        pid, ready, finish = self.fork_fixture()
        self.assertTrue(process_running(pid))
        self.assertTrue(process_group_running(pid))
        os.kill(pid, signal.SIGSTOP)
        os.waitpid(pid, os.WUNTRACED)
        self.assertTrue(process_running(pid), 'a stopped writer is still running')
        self.assertTrue(process_group_running(pid))

    def test_zombie_is_gone_before_parent_waits(self):
        pid, ready, finish = self.fork_fixture()
        os.write(finish, b'x')
        self.assertEqual(b'', os.read(ready, 1))
        # The kernel may close the pipe just before marking the task Z. Observe
        # the state without reaping, then make exactly one liveness assertion.
        deadline = time.monotonic() + 3
        while True:
            if sys.platform.startswith('linux'):
                state = Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()[0]
            else:
                state = subprocess.check_output(['ps', '-o', 'stat=', '-p', str(pid)], text=True).strip()
            if state.startswith('Z'): break
            self.assertLess(time.monotonic(), deadline, 'fixture did not become a zombie')
            time.sleep(.01)
        os.kill(pid, 0)  # The old survivor rule sees this unreaped PID as alive.
        self.assertFalse(process_running(pid), 'zombie counted as a running writer')
        self.assertFalse(process_group_running(pid), 'zombie-only group counted as running')

    def test_vanished_pid_is_gone(self):
        pid = os.fork()
        if pid == 0: os._exit(0)
        os.waitpid(pid, 0)
        self.assertFalse(process_running(pid))

    def test_linux_parses_state_after_last_parenthesis(self):
        for state in ('Z', 'S', 'R', 'T', 'D'):
            with self.subTest(state=state), patch('process_helpers.sys.platform', 'linux'), \
                    patch('process_helpers.Path.read_text', return_value=f'42 (a ) (worker)) {state} 1 2 3'):
                self.assertEqual(state != 'Z', process_running(42))
        with patch('process_helpers.sys.platform', 'linux'), \
                patch('process_helpers.Path.read_text', side_effect=FileNotFoundError):
            self.assertFalse(process_running(42))

    def test_linux_group_scan_skips_unreadable_foreign_entry(self):
        group = 42
        foreign, member = self.root/'proc'/'41', self.root/'proc'/'42'

        def read_stat(path, *args, **kwargs):
            if path.parent.name == foreign.name:
                raise PermissionError('foreign stat is hidden')
            return f'42 (sleep) {state} 1 {group} 0'

        for state in ('S', 'Z'):
            with self.subTest(state=state), patch('process_helpers.sys.platform', 'linux'), \
                    patch('process_helpers.os.killpg', return_value=None) as exists, \
                    patch('process_helpers.os.getpgid', return_value=99), \
                    patch('process_helpers.Path.iterdir', return_value=iter([foreign, member])), \
                    patch('process_helpers.Path.read_text', autospec=True, side_effect=read_stat):
                self.assertEqual(state != 'Z', process_group_running(group))
                exists.assert_called_once_with(group, 0)

    def test_group_scan_after_eperm(self):
        group = 42
        for platform in ('darwin', 'linux'):
            for state in ('Z', 'S'):
                with self.subTest(platform=platform, state=state), \
                        patch('process_helpers.sys.platform', platform), \
                        patch('process_helpers.os.killpg', side_effect=PermissionError(errno.EPERM, 'not permitted')) as exists, \
                        patch('process_helpers.Path.iterdir', return_value=iter([self.root/'42'])), \
                        patch('process_helpers.Path.read_text', return_value=f'42 (sleep) {state} 1 {group} 0'), \
                        patch('process_helpers.subprocess.run', return_value=Mock(stdout=f'42 {group} {state}\n')):
                    self.assertEqual(state != 'Z', process_group_running(group))
                    exists.assert_called_once_with(group, 0)

    def test_group_scan_failure_is_running(self):
        failures = (
            ('linux', PermissionError(errno.EACCES, 'proc is hidden')),
            ('darwin', PermissionError(errno.EACCES, 'ps is unavailable')),
            ('darwin', subprocess.CalledProcessError(1, ['ps'])),
            ('darwin', subprocess.TimeoutExpired(['ps'], 1)),
        )
        for platform, failure in failures:
            for probe_error in (None, PermissionError(errno.EPERM, 'not permitted')):
                with self.subTest(platform=platform, failure=type(failure).__name__, probe_error=probe_error), \
                        patch('process_helpers.sys.platform', platform), \
                        patch('process_helpers.os.killpg', side_effect=probe_error), \
                        patch('process_helpers.Path.iterdir', side_effect=failure), \
                        patch('process_helpers.subprocess.run', side_effect=failure):
                    self.assertTrue(process_group_running(42))

    def test_group_probe_other_errors_keep_existing_rules(self):
        for code in (errno.ESRCH, errno.EACCES, errno.EIO):
            with self.subTest(errno=code), \
                    patch('process_helpers.os.killpg', side_effect=OSError(code, 'probe failed')), \
                    patch('process_helpers.Path.iterdir') as proc, \
                    patch('process_helpers.subprocess.run') as ps:
                self.assertEqual(code != errno.ESRCH, process_group_running(42))
                proc.assert_not_called()
                ps.assert_not_called()

    def test_group_without_visible_members_is_running(self):
        for platform in ('darwin', 'linux'):
            with self.subTest(platform=platform), patch('process_helpers.sys.platform', platform), \
                    patch('process_helpers.os.killpg', side_effect=PermissionError(errno.EPERM, 'not permitted')), \
                    patch('process_helpers.Path.iterdir', return_value=iter([])), \
                    patch('process_helpers.subprocess.run', return_value=Mock(stdout='')):
                self.assertTrue(process_group_running(42))

    def test_ps_group_scan_skips_foreign_unreadable_state(self):
        for state in ('S', 'Z'):
            with self.subTest(state=state), patch('process_helpers.sys.platform', 'darwin'), \
                    patch('process_helpers.os.killpg', side_effect=PermissionError(errno.EPERM, 'not permitted')), \
                    patch('process_helpers.subprocess.run', return_value=Mock(stdout=f'41 99\n42 42 {state}\n')) as ps:
                self.assertEqual(state != 'Z', process_group_running(42))
                self.assertEqual(['ps', '-axo', 'pid=,pgid=,stat='], ps.call_args.args[0])

    def test_ps_group_unknown_membership_or_state_is_running(self):
        for row in ('41 ? Z', '42 42', '42 42 ?'):
            with self.subTest(row=row), patch('process_helpers.sys.platform', 'darwin'), \
                    patch('process_helpers.os.killpg', side_effect=PermissionError(errno.EPERM, 'not permitted')), \
                    patch('process_helpers.subprocess.run', return_value=Mock(stdout=f'43 42 Z\n{row}\n')):
                self.assertTrue(process_group_running(42))

    def test_ps_states_and_raced_exit(self):
        for state in ('Z', 'Z+', 'S', 'R+', 'T', 'D'):
            with self.subTest(state=state), patch('process_helpers.sys.platform', 'darwin'), \
                    patch('process_helpers.subprocess.run', return_value=Mock(returncode=0, stdout=state)) as ps:
                self.assertEqual(bool(state) and not state.startswith('Z'), process_running(42))
                self.assertEqual(['ps', '-o', 'stat=', '-p', '42'], ps.call_args.args[0])
        for code in (0, 1):
            with self.subTest(code=code), patch('process_helpers.sys.platform', 'freebsd'), \
                    patch('process_helpers.subprocess.run', return_value=Mock(returncode=code, stdout='', stderr='gone')), \
                    patch('process_helpers.os.kill', side_effect=ProcessLookupError):
                self.assertFalse(process_running(42))
        with patch('process_helpers.sys.platform', 'darwin'), \
                patch('process_helpers.subprocess.run', return_value=Mock(returncode=0, stdout='')), \
                patch('process_helpers.os.kill', return_value=None), self.assertRaises(RuntimeError):
            process_running(42)  # Unreadable state must not hide a live writer.


if __name__ == '__main__': unittest.main(verbosity=2)
