"""Exercise the real control runner with a short timeout and baseline errors."""
import os
import json
import io
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from contextlib import nullcontext
from unittest.mock import Mock, patch

import git_maintenance

import process_helpers
from process_helpers import process_running, running_processes
from process_helpers_test import DeadlineWaits, ProcessHelpers
from selected_cases import selected_cases
from mutation_checks import (_run, baseline_fixture, caught_mutation, fixture_pids, fixture_snapshot, historical_scripts,
                             linux_process_commands, linux_process_identity, macos_cwds, mutation_caught,
                             reap_adopted_fixtures, reaping_fixtures, restored_files, run, snapshot_files,
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

    def control(self, timeout=5, **expectation):
        return mutation_caught(self.command, env={'CONTROL_SOURCE': str(self.source)},
                               originals={self.source:self.baseline.read_bytes()}, timeout=timeout,
                               **(expectation or {'expected':'AssertionError: mutated behavior'}))

    def test_pipeline_supervision_keeps_existing_fixture_and_joins_detached_child(self):
        cwd = self.root/'working'; cwd.mkdir()
        pid_file = cwd/'child.pid'
        self.test.write_text('''import os, subprocess, sys, time
from pathlib import Path
child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)', str(Path.cwd()/'owned')],
                         start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
Path('child.pid').write_text(str(child.pid))
print(Path.cwd())
print('pipeline diagnostic', file=sys.stderr)
''')
        self.addCleanup(self.stop_recorded, pid_file)
        completed = process_helpers.run_pipeline(self.command, root=self.root, cwd=cwd, env={})
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stdout.strip(), str(cwd))
        self.assertEqual(completed.stderr.strip(), 'pipeline diagnostic')
        self.assertTrue(self.test.is_file(), 'the caller owns fixture directory removal')
        self.assertFalse(process_running(int(pid_file.read_text())), 'detached descendant survived pipeline')

    def test_target_timeout_error_is_caught(self):
        # subprocess.run kills and joins this child before unittest reports ERROR.
        self.source.write_text('''import subprocess, sys
def work():
    subprocess.run([sys.executable, '-c', 'import time; time.sleep(30)'], timeout=.05)
''')
        result = self.control(expected='subprocess.TimeoutExpired:')
        self.assertIn('TimeoutExpired', result.stderr)
        self.assertIn('FAILED (errors=1)', result.stderr)

    def test_other_target_error_is_caught(self):
        self.source.write_text('def work(): raise ValueError("mutated behavior")\n')
        self.assertIn('FAILED (errors=1)', self.control(expected='ValueError: mutated behavior').stderr)

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
        self.assertEqual(124, self.control(allow_timeout=True).returncode)
        self.assertFalse(running_processes(map(int, pid.read_text().splitlines())))

    def test_baseline_timeout_fails(self):
        self.baseline.write_text('import time\ndef work(): time.sleep(30)\n')
        with self.assertRaisesRegex(AssertionError, 'unmutated baseline failed:'):
            self.control(timeout=1)

    def test_assertion_control_rejects_mutant_timeout(self):
        self.source.write_text('import time\ndef work(): time.sleep(30)\n')
        with self.assertRaisesRegex(AssertionError, 'mutation was not caught.*got exit 124'):
            self.control(timeout=1)

    def test_expected_text_rejects_unrelated_error(self):
        self.source.write_text('def work(): raise ValueError("unrelated failure")\n')
        with self.assertRaisesRegex(AssertionError, 'mutation was not caught') as error:
            self.control()
        self.assertIn('ValueError: unrelated failure', str(error.exception))

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
        result = self.control(timeout=1, allow_timeout=True)
        self.assertEqual(124, result.returncode)
        self.assertLess(time.monotonic() - started, 8)
        self.assertIsNone(unrelated.poll(), 'timeout cleanup stopped an unrelated process')
        self.assertFalse(running_processes(map(int, pid.read_text().splitlines())))

    def test_rejects_unbounded_deadlines_before_starting_any_process(self):
        for timeout in (None, float('inf'), float('nan'), 0, -1):
            with self.subTest(timeout=timeout), patch('mutation_checks.subprocess.Popen') as start:
                with self.assertRaisesRegex(ValueError, 'positive finite'):
                    run(self.command, timeout=timeout)
                with self.assertRaisesRegex(ValueError, 'positive finite'):
                    mutation_caught(self.command, env={}, originals={}, timeout=timeout, expected_exit=1)
                start.assert_not_called()

    def test_ps_scan_is_bounded_and_does_not_match_a_sibling_root(self):
        output = f'101 python {self.root}/child.py\n102 python {self.root}-sibling/child.py\n'
        output += f'103 python --repo="{self.root}/repo with spaces"\n'
        with patch('mutation_checks.sys.platform', 'darwin'), \
                patch('mutation_checks.subprocess.run', return_value=Mock(stdout=output)) as ps:
            self.assertEqual({101, 103}, fixture_pids(self.root, cwds=lambda: {}))
        self.assertEqual(['ps', '-axo', 'pid=,command='], ps.call_args.args[0])
        self.assertEqual(10, ps.call_args.kwargs['timeout'])
        self.assertTrue(ps.call_args.kwargs['check'])

    def test_ps_timeout_retries_and_uses_successful_argv_snapshot(self):
        output = subprocess.CompletedProcess(['ps'], 0, f'101 python {self.root}/child.py\n', '')
        cwds = Mock(return_value={})
        with patch('mutation_checks.sys.platform', 'darwin'), \
                patch('mutation_checks.subprocess.run',
                      side_effect=[subprocess.TimeoutExpired('ps', 10), output]) as ps:
            self.assertEqual({101}, fixture_pids(self.root, cwds=cwds))
        self.assertEqual(2, ps.call_count)
        self.assertEqual(ps.call_args_list[0], ps.call_args_list[1])
        self.assertEqual(10, ps.call_args.kwargs['timeout'])
        cwds.assert_called_once_with()

    def test_macos_lsof_cwd_scan_parses_pid_fields_and_is_bounded(self):
        output = f'n/ignored\np101\nfcwd\nn{self.root}/with spaces\n'
        output += f'p102\nfcwd\nn{self.root}-sibling\npinvalid\nn/ignored\n'
        with patch('mutation_checks.subprocess.run', return_value=Mock(stdout=output)) as lsof:
            self.assertEqual({101: str(self.root/'with spaces'), 102: str(self.root)+'-sibling'},
                             macos_cwds())
        self.assertEqual(['lsof', '-nP', '-a', '-d', 'cwd', '-Fpn'], lsof.call_args.args[0])
        self.assertEqual(10, lsof.call_args.kwargs['timeout'])
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

    def test_macos_cwd_timeout_retries_and_uses_successful_snapshot(self):
        commands = Mock(return_value=f'101 python {self.root}/child.py\n')
        output = subprocess.CompletedProcess(['lsof'], 0, f'p102\nn{self.root}/nested\n', '')
        with patch('mutation_checks.sys.platform', 'darwin'), \
                patch('mutation_checks.sys.stderr', new_callable=io.StringIO) as report, \
                patch('mutation_checks.subprocess.run',
                      side_effect=[subprocess.TimeoutExpired('lsof', 10), output]) as lsof:
            snapshot = fixture_snapshot(commands=commands)
            self.assertEqual({101, 102}, fixture_pids(self.root, snapshot=snapshot))
            self.assertEqual({101, 102}, fixture_pids(self.root, snapshot=snapshot))
        commands.assert_called_once_with()
        self.assertEqual(2, lsof.call_count)
        self.assertEqual(lsof.call_args_list[0], lsof.call_args_list[1])
        self.assertEqual(10, lsof.call_args.kwargs['timeout'])
        self.assertEqual('', report.getvalue())

    def test_macos_cwd_discovery_failure_reports_argv_fallback(self):
        for error in (FileNotFoundError('lsof unavailable'),
                      subprocess.TimeoutExpired('lsof', 10),
                      subprocess.CalledProcessError(1, 'lsof')):
            commands = Mock(return_value=f'101 python {self.root}/child.py\n'
                                        f'102 python {self.root}-sibling/child.py\n')
            with self.subTest(error=type(error).__name__), \
                    patch('mutation_checks.sys.platform', 'darwin'), \
                    patch('mutation_checks.sys.stderr', new_callable=io.StringIO) as report, \
                    patch('mutation_checks.subprocess.run', side_effect=error) as lsof:
                self.assertEqual({101}, fixture_pids(self.root, commands=commands))
            commands.assert_called_once_with()
            self.assertEqual(2, lsof.call_count)
            self.assertEqual(f'fixture cwd discovery unavailable for {self.root}: {error}; '
                             'falling back to argv matching\n', report.getvalue())

    def test_argv_snapshot_failure_cannot_prove_ownership_or_start_a_run(self):
        for error in (PermissionError('inspection unavailable'),
                      subprocess.TimeoutExpired('ps', 10),
                      subprocess.CalledProcessError(1, 'ps')):
            with self.subTest(error=type(error).__name__), \
                    patch('mutation_checks.sys.platform', 'darwin'), \
                    patch('mutation_checks.subprocess.run', side_effect=error) as ps, \
                    patch('mutation_checks.macos_cwds') as cwds, \
                    patch('mutation_checks.subprocess.Popen') as start:
                with self.assertRaises(type(error)):
                    fixture_pids(self.root)
                self.assertEqual(2, ps.call_count)
                ps.reset_mock()
                with self.assertRaises(type(error)):
                    run(self.command, root=self.root)
                self.assertEqual(2, ps.call_count)
                cwds.assert_not_called()
                start.assert_not_called()

    def test_one_snapshot_answers_all_roots_without_more_process_scans(self):
        sibling = self.root.with_name(self.root.name+'-sibling')
        commands = Mock(return_value=f'101 python {self.root}/child.py\n102 python {sibling}/child.py\n')
        cwds = Mock(return_value={103: str(self.root/'nested'), 104: str(sibling)})
        with patch('mutation_checks.sys.platform', 'darwin'):
            snapshot = fixture_snapshot(commands=commands, cwds=cwds)
            self.assertEqual({101, 103}, fixture_pids(self.root, snapshot=snapshot))
            self.assertEqual({102, 104}, fixture_pids(sibling, snapshot=snapshot))
        commands.assert_called_once_with()
        cwds.assert_called_once_with()

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
        self.assertFalse(running_processes(map(int, pid.read_text().splitlines())))

    def test_linux_scan_finds_cwd_without_a_matching_command(self):
        proc = self.root/'proc'; proc.mkdir()
        for pid, cwd in ((101,self.root), (102,self.root/'inside'), (103,self.root.parent)):
            cwd.mkdir(exist_ok=True)
            directory = proc/str(pid); directory.mkdir(); (directory/'cwd').symlink_to(cwd)
        deleted = proc/'104'; deleted.mkdir()
        (deleted/'cwd').symlink_to(str(self.root)+' (deleted)')
        for pid, path in ((105, str(self.root/'child with spaces.py')),
                          (106, str(self.root)+'-sibling/child.py')):
            directory = proc/str(pid); directory.mkdir()
            (directory/'cmdline').write_bytes(b'python\0' + path.encode() + b'\0')
        with patch('mutation_checks.sys.platform', 'linux'), patch('mutation_checks.PROC', proc), \
                patch('mutation_checks.subprocess.run', return_value=Mock(stdout='101 sleep 30\n')):
            self.assertEqual({101, 102, 104}, fixture_pids(self.root))
            self.assertEqual({101, 102, 104, 105}, fixture_pids(self.root, commands=linux_process_commands))

    def test_linux_identity_keeps_zombie_birth_and_handles_parentheses(self):
        proc = self.root/'proc'; directory = proc/'101'; directory.mkdir(parents=True)
        fields = ['Z', '123'] + ['0'] * 17 + ['456']
        (directory/'stat').write_text('101 (fixture (worker)) ' + ' '.join(fields))
        with patch('mutation_checks.PROC', proc):
            self.assertEqual((123, '456'), linux_process_identity(101))
            self.assertIsNone(linux_process_identity(102))

    def test_reaper_excludes_direct_child_nonchildren_and_reused_pids(self):
        known = {100: 'direct', 103: 'old'}
        identities = {100: (os.getpid(), 'direct'), 101: (os.getpid(), 'owned'),
                      102: (0, 'descendant'), 103: (os.getpid(), 'reused'),
                      104: (os.getpid(), 'outside')}
        with patch('mutation_checks.linux_fixture_identities',
                   return_value={pid: identities[pid] for pid in (100, 101, 102)}) as discover, \
                patch('mutation_checks.linux_process_identity', side_effect=identities.get), \
                patch('mutation_checks.os.waitpid', return_value=(0, 0)) as wait:
            reap_adopted_fixtures(self.root, 100, known)
            wait.assert_called_once_with(101, os.WNOHANG)
            self.assertEqual({101: 'owned', 102: 'descendant', 103: None}, known)
            # Zombies no longer match argv/cwd; their recorded birth still does.
            discover.return_value = {}
            identities[102] = (os.getpid(), 'reused-descendant')
            wait.reset_mock(); wait.return_value = (101, 0)
            reap_adopted_fixtures(self.root, 100, known)
            wait.assert_called_once_with(101, os.WNOHANG)
            self.assertEqual({101: None, 102: None, 103: None}, known)

    def test_macos_does_not_start_an_orphan_reaper(self):
        with patch('mutation_checks.sys.platform', 'darwin'), \
                patch('mutation_checks.threading.Thread') as thread:
            with reaping_fixtures(self.root, Mock(pid=100)):
                pass
            thread.assert_not_called()

    def test_observer_start_failure_still_cleans_command_and_closes_pipes(self):
        child = Mock(pid=100, stdout=io.StringIO(), stderr=io.StringIO())
        with patch('mutation_checks.sys.platform', 'linux'), \
                patch('mutation_checks.subprocess.Popen', return_value=child), \
                patch('mutation_checks.threading.Thread.start', side_effect=RuntimeError('cannot start observer')), \
                patch('mutation_checks.stop_fixtures') as stop:
            with self.assertRaisesRegex(RuntimeError, 'cannot start observer'):
                _run(self.command, env=None, timeout=1, root=self.root)
        self.assertEqual((1, True, True),
                         (stop.call_count, child.stdout.closed, child.stderr.closed))
        stop.assert_called_once_with(self.root, child, observed=None)
        child.communicate.assert_not_called()

    def test_shutdown_reaps_a_zombie_after_the_last_observation_before_root_removal(self):
        observed, release = threading.Event(), threading.Event()
        zombie, reaped = False, False
        join = threading.Thread.join

        def wait(pid, flags):
            nonlocal reaped
            self.assertEqual((101, os.WNOHANG), (pid, flags))
            if zombie:
                reaped = True
                return pid, 0
            observed.set()
            self.assertTrue(release.wait(3), 'shutdown never joined its observer')
            return 0, 0  # Its last observation happened before the exit.

        def finish_thread(thread, *args, **kwargs):
            release.set()  # stopped is already set; no further background sweep.
            return join(thread, *args, **kwargs)

        with tempfile.TemporaryDirectory(dir=self.root) as directory:
            root = Path(directory)
            with patch('mutation_checks.sys.platform', 'linux'), \
                    patch('mutation_checks.fixture_pids', side_effect=lambda *a, **k: set() if zombie else {101}), \
                    patch('mutation_checks.linux_fixture_identities', create=True,
                          side_effect=lambda *a: {} if zombie else {101: (os.getpid(), 'owned')}), \
                    patch('mutation_checks.linux_process_identity', return_value=(os.getpid(), 'owned')), \
                    patch('mutation_checks.os.waitpid', side_effect=wait), \
                    patch('mutation_checks.threading.Thread.join', finish_thread):
                with reaping_fixtures(root, Mock(pid=100)):
                    self.assertTrue(observed.wait(3), 'fixture was never recorded')
                    zombie = True
                self.assertTrue(root.exists())
                self.assertTrue(reaped, 'shutdown lost the recorded zombie before root removal')
        self.assertFalse(root.exists())

    def test_reaper_does_not_overwrite_cached_birth_when_reused_pid_is_discovered(self):
        known = {103: 'original'}
        with patch('mutation_checks.fixture_pids', return_value={103}), \
                patch('mutation_checks.linux_fixture_identities', create=True,
                      return_value={103: (os.getpid(), 'replacement')}), \
                patch('mutation_checks.linux_process_identity', return_value=(os.getpid(), 'replacement')), \
                patch('mutation_checks.os.waitpid', return_value=(0, 0)) as wait:
            reap_adopted_fixtures(self.root, 100, known)
            reap_adopted_fixtures(self.root, 100, known)
            wait.assert_not_called()

    def test_reaper_binds_initial_birth_to_ownership_evidence(self):
        proc = self.root/'proc'; directory = proc/'103'; directory.mkdir(parents=True)
        (directory/'cmdline').write_bytes(b'python\0' + str(self.root/'worker.py').encode() + b'\0')
        (directory/'cwd').symlink_to(self.root)
        birth = 'original'
        known = {}
        readlink = os.readlink

        def ownership(path, *args, **kwargs):
            nonlocal birth
            evidence = readlink(path, *args, **kwargs)
            if Path(path) == directory/'cwd':
                birth = 'replacement'  # PID reuse just after ownership was read.
            return evidence

        with patch('mutation_checks.sys.platform', 'linux'), patch('mutation_checks.PROC', proc), \
                patch('mutation_checks.os.readlink', side_effect=ownership), \
                patch('mutation_checks.linux_process_identity', side_effect=lambda pid: (os.getpid(), birth)), \
                patch('mutation_checks.os.waitpid', return_value=(0, 0)) as wait:
            reap_adopted_fixtures(self.root, 100, known)
            reap_adopted_fixtures(self.root, 100, known)
            self.assertEqual('replacement', birth, 'recycled PID was excluded from discovery')
            wait.assert_not_called()

    def test_reaper_joins_before_propagating_a_command_error(self):
        inspected = threading.Event()
        before = set(threading.enumerate())
        with patch('mutation_checks.sys.platform', 'linux'), \
                patch('mutation_checks.linux_fixture_identities', side_effect=lambda root: inspected.set() or {}):
            with self.assertRaisesRegex(ValueError, 'command failed'):
                with reaping_fixtures(self.root, Mock(pid=100)):
                    self.assertTrue(inspected.wait(3), 'reaper never inspected the fixture')
                    workers = set(threading.enumerate()) - before
                    self.assertEqual(1, len(workers))
                    raise ValueError('command failed')
            self.assertTrue(all(not worker.is_alive() for worker in workers))

    @unittest.skipUnless(sys.platform.startswith('linux'),
                         'Linux child subreaping is required; macOS uses launchd')
    def test_linux_command_observes_detached_grandchild_exit_before_reaping(self):
        self.detached_grandchild_control()

    def detached_grandchild_control(self, helper_source=None):
        recorded = self.root/'detached-pid'
        self.addCleanup(self.stop_recorded, recorded)
        # The command imports the same shared rule from its private fixture.
        (self.root/'process_helpers.py').write_text(
            helper_source if helper_source is not None else Path(process_helpers.__file__).read_text())
        source = f'''import os, signal, subprocess, sys, time
from pathlib import Path
from process_helpers import process_running
recorded = Path({str(recorded)!r})
def work():
    launcher = ('import subprocess, sys; '
                'child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"], '
                'start_new_session=True, stdin=subprocess.DEVNULL, '
                'stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL); print(child.pid)')
    parent = subprocess.Popen([sys.executable, '-c', launcher], stdout=subprocess.PIPE, text=True)
    output, _ = parent.communicate(timeout=3)
    assert parent.returncode == 0
    pid = int(output)
    recorded.write_text(str(pid))
    try:
        fields = Path(f'/proc/{{pid}}/stat').read_text().rsplit(')', 1)[1].split()
        assert int(fields[1]) == os.getppid(), 'grandchild was not adopted by the harness'
        os.kill(pid, signal.SIGKILL)
        # Wait for exit, never for reaping. The single assertion below must work
        # even if the harness has not observed this descendant yet.
        deadline = time.monotonic() + 3
        while True:
            try: state = Path(f'/proc/{{pid}}/stat').read_text().rsplit(')', 1)[1].split()[0]
            except FileNotFoundError: break
            if state == 'Z': break
            assert time.monotonic() < deadline, 'killed detached grandchild did not exit'
            time.sleep(.01)
        assert not process_running(pid), 'killed detached grandchild remains running'
    finally:
        try: os.kill(pid, signal.SIGKILL)
        except ProcessLookupError: pass
'''
        self.baseline.write_text(source)
        self.source.write_text(source + '    raise ValueError("mutation after orphan exit")\n')
        waitpid = os.waitpid

        def defer_background_wait(pid, flags):
            if threading.current_thread().name == 'fixture-orphan-reaper' and recorded.exists():
                try:
                    if pid == int(recorded.read_text()):
                        return 0, 0  # No background reap can help the assertion.
                except ValueError:
                    pass  # raced the command's PID-file write
            return waitpid(pid, flags)

        with patch('mutation_checks.os.waitpid', side_effect=defer_background_wait):
            result = self.control(timeout=10, expected='ValueError: mutation after orphan exit')
        self.assertEqual(1, result.returncode, 'Popen lost the mutation command exit status')
        self.assertIn('mutation after orphan exit', result.stderr)

    @unittest.skipUnless(sys.platform.startswith('linux'), 'Linux zombie regression control')
    def test_linux_regression_rejects_disabled_reaping_and_zombie_rule(self):
        old_rule = '''import os
def process_running(pid):
    try: os.kill(pid, 0)
    except ProcessLookupError: return False
    return True
'''
        with patch('mutation_checks.reaping_fixtures', side_effect=lambda *a: nullcontext()), \
                self.assertRaisesRegex(AssertionError, 'killed detached grandchild remains running'):
            self.detached_grandchild_control(old_rule)

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
        pids = set(map(int, path.read_text().splitlines()))
        for pid in pids:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                continue
            except PermissionError as error:
                raise AssertionError(f'could not kill fixture PID {pid}') from error
        try:
            remaining = process_helpers.wait_for_processes_exit(pids, time.monotonic() + 3)
        finally:
            for pid in pids:
                try: os.waitpid(pid, os.WNOHANG)
                except ChildProcessError: pass
        if remaining:
            raise AssertionError('could not reap fixture PIDs: ' + ', '.join(map(str, sorted(remaining))))

    def test_recorded_cleanup_stops_every_pid_even_when_snapshot_fails(self):
        path = self.root/'recorded-pids'; path.write_text('41\n42\n')
        with patch('mutation_checks.os.kill') as kill, \
                patch('mutation_checks.os.waitpid', side_effect=ChildProcessError) as reap, \
                patch('mutation_checks.time.monotonic', return_value=0), \
                patch('process_helpers.wait_for_processes_exit', side_effect=PermissionError) as inspect:
            with self.assertRaises(PermissionError):
                self.stop_recorded(path)
        self.assertEqual({(41, signal.SIGKILL), (42, signal.SIGKILL)},
                         {call.args for call in kill.call_args_list})
        inspect.assert_called_once_with({41, 42}, 3)
        self.assertEqual({(41, os.WNOHANG), (42, os.WNOHANG)}, {call.args for call in reap.call_args_list})

    def test_final_pipe_drain_timeout_is_bounded_and_preserves_output(self):
        command = [sys.executable, '-c', 'import time; time.sleep(30)']
        communicate = subprocess.Popen.communicate
        calls = []

        def pipe_stays_open(child, *args, **kwargs):
            if child.args == command:
                calls.append(kwargs['timeout'])
                if child.poll() is not None:
                    raise subprocess.TimeoutExpired(child.args, kwargs['timeout'],
                                                    output=b'partial stdout', stderr=b'partial stderr')
            return communicate(child, *args, **kwargs)

        with patch.object(subprocess.Popen, 'communicate', pipe_stays_open):
            result = run(command, timeout=.1)
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
        result = mutation_caught(self.command, env={'CONTROL_SOURCE': str(self.source)}, originals=originals,
                                 expected='ValueError: private mutation')
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
            mutation_caught(self.command, env={'CONTROL_SOURCE': str(self.source)}, originals=originals,
                            expected='AssertionError: mutation')
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
        for args in (('init','-q'), git_maintenance.OFF_CONFIG, ('add','.'),
                     ('-c','user.name=fixture','-c','user.email=fixture@bureau','commit','-qm','old behavior')):
            subprocess.run(['git','-C',str(repo),*args], env=env, check=True, capture_output=True, timeout=5)
        path.write_text('from pathlib import Path\ndef work(): Path(__file__).with_name("companion").read_text()\n')
        (scripts/'companion').write_text('current companion inventory')
        with historical_scripts(repo, 'HEAD') as (private, originals):
            self.assertIn('historical mutation', (private/'subject.py').read_text())
            self.assertEqual('current companion inventory', (private/'companion').read_text())
            result = mutation_caught(self.command, env={'CONTROL_SOURCE':str(private/'subject.py')},
                                     originals=originals, expected='ValueError: historical mutation')
            self.assertIn('historical mutation', result.stderr)
            (private/'companion').unlink()
            # An absent companion must break the baseline, never count as detection.
            with self.assertRaises(FileNotFoundError):
                mutation_caught(self.command, env={'CONTROL_SOURCE':str(private/'subject.py')},
                                originals=originals, expected='ValueError: historical mutation')

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


class MutationVerdicts(unittest.TestCase):
    def check(self, code, output='', *, baseline=0, **expectation):
        with tempfile.TemporaryDirectory(prefix='bureau-mutation-verdict-') as directory:
            root=Path(directory)
            source=root/'subject'; source.write_bytes(b'mutated')
            def completed(command, **kwargs):
                if source.read_bytes()==b'original':
                    return subprocess.CompletedProcess(command,baseline,'','slow baseline' if baseline else '')
                return subprocess.CompletedProcess(command,code,'',output)
            with patch('mutation_checks.fixture_root',return_value=nullcontext(root)), \
                    patch('mutation_checks._run',side_effect=completed) as execute:
                try:
                    return mutation_caught(['probe'],env={},originals={source:b'original'},**expectation)
                finally:
                    self.assertEqual(source.read_bytes(),b'mutated')
                    self.assertEqual(execute.call_count,1 if baseline else 2)

    def test_text_and_exit_must_match(self):
        self.assertEqual(self.check(1,'expected assertion',expected='expected assertion',expected_exit=1).returncode,1)
        for code,output in ((0,'expected assertion'),(1,'unrelated error'),(2,'expected assertion')):
            with self.subTest(code=code,output=output), self.assertRaisesRegex(AssertionError,'mutation was not caught'):
                self.check(code,output,expected='expected assertion',expected_exit=1)

    def test_timeout_requires_opt_in_even_with_matching_output_or_exit(self):
        for expectation in ({'expected':'expected assertion'},{'expected_exit':124}):
            with self.subTest(expectation=expectation), self.assertRaisesRegex(AssertionError,'mutation was not caught'):
                self.check(124,'expected assertion',**expectation)
        self.assertEqual(self.check(124,allow_timeout=True).returncode,124)
        with self.assertRaisesRegex(AssertionError,'mutation was not caught'):
            self.check(1,'unrelated error',allow_timeout=True)

    def test_tuple_requires_every_fragment(self):
        self.assertEqual(self.check(1,'first and second',expected=('first','second')).returncode,1)
        with self.assertRaisesRegex(AssertionError,'mutation was not caught'):
            self.check(1,'first',expected=('first','second'))

    def test_expected_exit_without_text(self):
        self.assertEqual(self.check(2,expected_exit=2).returncode,2)
        with self.assertRaisesRegex(AssertionError,'mutation was not caught'):
            self.check(1,expected_exit=2)

    def test_slow_baseline_is_never_detection(self):
        with self.assertRaisesRegex(AssertionError,'unmutated baseline failed:\nslow baseline'):
            self.check(124,baseline=124,allow_timeout=True)

    def test_expectation_is_required_before_spawning(self):
        for expectation in ({},{'expected':''},{'expected':()},{'expected_exit':0},{'allow_timeout':'yes'}):
            with self.subTest(expectation=expectation), patch('mutation_checks.subprocess.Popen') as start:
                with self.assertRaises(ValueError):
                    mutation_caught(['probe'],env={},originals={},**expectation)
                start.assert_not_called()


class CaseSelection(unittest.TestCase):
    def test_default_and_explicit_selection(self):
        cases={'first':1,'second':2}
        with patch.dict(os.environ,{},clear=True):
            self.assertIs(selected_cases(cases,'CONTROL_CASES'),cases)
            os.environ['CONTROL_CASES']='["second"]'
            self.assertEqual(selected_cases(cases,'CONTROL_CASES'),{'second':2})

    def test_empty_or_unknown_selection_cannot_make_a_green_control(self):
        for value in ('[]','["unknown"]','"first"','[1]'):
            with self.subTest(value=value), patch.dict(os.environ,{'CONTROL_CASES':value}):
                with self.assertRaises(ValueError): selected_cases({'first':1},'CONTROL_CASES')

    def test_withdrawal_selection_preserves_the_full_positive_matrix(self):
        from conflicts_withdrawal_test import ConflictWithdrawal
        case=ConflictWithdrawal('test_withdrawal_before_dependencies_and_between_commands')
        for selection,count in (({},15),({'BUREAU_TEST_WITHDRAWAL_CASES':'["generator-1:done"]'},1)):
            with self.subTest(selection=selection), patch.dict(os.environ,selection,clear=True), \
                    patch.object(case,'start_case'), patch.object(case,'worker',return_value=None) as worker, \
                    patch.object(case,'assert_stopped') as stopped:
                case.test_withdrawal_before_dependencies_and_between_commands()
                self.assertEqual(worker.call_count,count)
                if count==1:
                    stopped.assert_called_once_with(None,'done',0,['dependencies','generator-1'])


if __name__ == '__main__':
    unittest.main(verbosity=2)
