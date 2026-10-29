"""Deterministic exit and EPERM races at process-group termination boundaries."""
import argparse
import errno
import importlib.util
import itertools
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

sys.dont_write_bytecode = True
SCRIPTS = Path(os.environ.get('BUREAU_TEST_GROUP_SCRIPTS', Path(__file__).resolve().parents[1] / 'templates/scripts'))


def load(name):
    spec = importlib.util.spec_from_file_location(name.replace('-', '_'), SCRIPTS / (name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


p = load('bureau-provider')
r = load('bureau-runtime')
a = load('bureau-acceptance')
c = load('bureau-conflicts')
d = load('bureau-dispatch')


def denied():
    return PermissionError(errno.EPERM, 'injected group permission failure')


class ProbeClock:
    """Exercise the real backoff/deadline without delaying fault-injection tests."""
    def __init__(self):
        self.now = 0.0
        self.sleeps = []

    def monotonic(self): return self.now

    def sleep(self, delay):
        self.sleeps.append(delay)
        self.now += delay


class ProcessGroupTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='bureau process groups ')
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        subprocess.run(['git', 'init', '-q', str(self.root)], check=True)
        self.control = self.root / '.git' / 'bureau'
        self.child = Mock(pid=123456, returncode=0)
        self.child.poll.return_value = 0
        self.child.wait.return_value = 0
        self.child.communicate.return_value = ('output', None)
        self.inspectors = {}
        self.clocks = {}
        for module in (p, r, a.p, c.provider, d.r):
            if hasattr(module, 'pg'):
                self.inspectors[module.pg] = module.pg.inspect_groups
                fake = patch.object(module.pg, 'inspect_groups', return_value=set())
                fake.start(); self.addCleanup(fake.stop)
                if hasattr(module.pg, 'time'):
                    self.clocks[module.pg] = clock = ProbeClock()
                    fake = patch.object(module.pg, 'time', clock)
                    fake.start(); self.addCleanup(fake.stop)
        self.previous = {sig: signal.getsignal(sig) for sig in
                         (signal.SIGTERM, signal.SIGINT, signal.SIGHUP, signal.SIGALRM)}
        self.addCleanup(self.restore_handlers)

    def restore_handlers(self):
        signal.setitimer(signal.ITIMER_REAL, 0)
        for sig, handler in self.previous.items(): signal.signal(sig, handler)

    def assert_handlers_restored(self):
        for sig, handler in self.previous.items(): self.assertEqual(signal.getsignal(sig), handler)

    def outside(self, options):
        with patch.object(p, 'evidence_store', return_value=p.e.Store(self.control)), \
                patch.object(p.subprocess, 'Popen', return_value=self.child):
            return p.outside_gate(options, self.root, 'fixture', self.root / 'outside.log')

    def test_successful_probe_reprobe_esrch_clears_gate_hold(self):
        for site, expected in (('cleanup', (0, None)), ('signal', (130, 'kill')), ('timeout', (124, 'timeout'))):
            with self.subTest(site=site):
                (self.root / 'outside.log').unlink(missing_ok=True)
                def wait(timeout=None):
                    if timeout == 60:
                        if site == 'signal': signal.getsignal(signal.SIGTERM)(signal.SIGTERM, None)
                        if site == 'timeout': raise subprocess.TimeoutExpired('fixture', timeout)
                    return 0
                self.child.wait.side_effect = wait
                probes = iter([None, None, ProcessLookupError()])
                def killpg(group, signum):
                    if signum == 0:
                        result = next(probes)
                        if result is not None: raise result
                clock = ProbeClock()
                with patch.object(p.pg, 'time', clock), \
                        patch.object(p.os, 'killpg', side_effect=killpg) as killpg_mock, \
                        patch.object(p.pg, 'inspect_groups', return_value={self.child.pid}) as inspect:
                    self.assertEqual(self.outside(dict(timeout=60, untrusted_env='default')), expected)
                self.assertEqual([call.args for call in killpg_mock.call_args_list if call.args[1] == 0],
                                 [(self.child.pid, 0)] * 3)
                self.assertEqual(clock.sleeps, [.01, .02])
                inspect.assert_not_called()
                self.assertEqual(p.pg.process_holds(self.control), [])
                self.assert_handlers_restored()

    def test_successful_group_probes_wait_until_deadline_before_inspection(self):
        for live in (True, False):
            with self.subTest(live=live):
                clock = ProbeClock()
                def inspect(groups):
                    self.assertEqual(clock.now, p.pg.GROUP_REPROBE_SECONDS)
                    return groups if live else set()
                with patch.object(p.pg, 'time', clock), patch.object(p.os, 'killpg') as killpg, \
                        patch.object(p.pg, 'inspect_groups', side_effect=inspect) as inspection:
                    if live:
                        with self.assertRaises(p.pg.TerminationUncertain): p.pg.assert_groups_stopped({self.child.pid})
                    else: p.pg.assert_groups_stopped({self.child.pid})
                self.assertEqual(clock.now, p.pg.GROUP_REPROBE_SECONDS)
                self.assertGreater(len(clock.sleeps), 2)
                self.assertTrue(all(0 < delay <= .1 for delay in clock.sleeps))
                self.assertEqual(killpg.call_count, len(clock.sleeps) + 1)
                self.assertTrue(all(call.args == (self.child.pid, 0) for call in killpg.call_args_list))
                inspection.assert_called_once_with({self.child.pid})

    def test_group_probe_errors_at_deadline_remain_uncertain(self):
        for first, later in ((denied(), denied()), (None, denied()), (None, OSError(errno.EIO, 'probe failed'))):
            with self.subTest(first=first, later=later):
                clock = ProbeClock()
                probes = itertools.chain([first], itertools.repeat(later))
                def killpg(group, signum):
                    error = next(probes)
                    if error is not None: raise error
                with patch.object(p.pg, 'time', clock), patch.object(p.os, 'killpg', side_effect=killpg), \
                        patch.object(p.pg, 'inspect_groups', return_value=set()) as inspect:
                    with self.assertRaises(p.pg.TerminationUncertain): p.pg.assert_groups_stopped({self.child.pid})
                self.assertEqual(clock.now, p.pg.GROUP_REPROBE_SECONDS)
                inspect.assert_not_called()

    def test_zombie_eperm_reprobe_esrch_clears_gate_hold(self):
        # Portable model of macOS's zombie-only group: Linux may return success
        # for the first two probes instead, so inject the kernel errno sequence.
        with patch.object(p.os, 'killpg', side_effect=[denied(), denied(),
                          ProcessLookupError(), ProcessLookupError()]) as killpg:
            self.assertEqual(self.outside(dict(timeout=60, untrusted_env='default')), (0, None))
        self.assertEqual(killpg.call_args_list, [unittest.mock.call(self.child.pid, signum)
                         for signum in (signal.SIGKILL, 0, 0, 0)])
        p.pg.inspect_groups.assert_not_called()
        self.assertEqual(p.pg.process_holds(self.control), [])
        self.child.kill.assert_not_called()
        self.assert_handlers_restored()

    def test_all_shared_callers_reprobe_both_signals_and_checks(self):
        for module in (p, r, a.p, c.provider, d.r):
            for probe in (False, True):
                with self.subTest(module=module.__name__, probe=probe), \
                        patch.object(module.os, 'killpg', side_effect=[denied(), denied(), ProcessLookupError()]) as killpg:
                    if probe: module.pg.assert_groups_stopped({self.child.pid})
                    else: module.signal_group(self.child, signal.SIGTERM)
                    self.assertEqual([call.args[1] for call in killpg.call_args_list],
                                     [0 if probe else signal.SIGTERM, 0, 0])
                    module.pg.inspect_groups.assert_not_called()

    def test_signal_reprobe_reaps_owned_zombie_before_deadline(self):
        for module in (p, r, a.p, c.provider, d.r):
            with self.subTest(module=module.__name__):
                reaped = []
                def poll():
                    reaped.append('provider-exit')
                    return 0
                def killpg(group, signum):
                    if signum == 0 and reaped: raise ProcessLookupError()
                    raise denied()
                self.child.poll.side_effect = poll
                clock = ProbeClock()
                with patch.object(module.pg, 'time', clock), \
                        patch.object(module.os, 'killpg', side_effect=killpg):
                    module.signal_group(self.child, signal.SIGTERM)
                self.assertTrue(reaped, 'the caller must reap its own zombie during the re-probe')
                self.assertEqual(clock.sleeps, [.01])
                self.child.kill.assert_not_called()
                module.pg.inspect_groups.assert_not_called()

    def test_reprobe_deadline_is_shared_across_errors_and_successful_probes(self):
        for later in (denied(), OSError(errno.EIO, 'probe failed'), None):
            with self.subTest(later=later):
                clock = ProbeClock()
                def killpg(group, signum):
                    if killpg_mock.call_count == 1: raise denied()
                    if later is not None: raise later
                with patch.object(p.pg, 'time', clock), \
                        patch.object(p.os, 'killpg', side_effect=killpg) as killpg_mock:
                    with self.assertRaises(p.pg.TerminationUncertain): p.signal_group(self.child, signal.SIGTERM)
                self.assertEqual(clock.now, 2.0)
                self.assertGreater(len(clock.sleeps), 2)
                self.assertTrue(all(0 < delay <= .1 for delay in clock.sleeps))
                self.assertEqual([call.args[1] for call in killpg_mock.call_args_list][1:], [0] * len(clock.sleeps))
                p.pg.inspect_groups.assert_not_called()

    def test_reprobe_deadline_does_not_accept_late_esrch(self):
        clock = Mock()
        clock.monotonic.side_effect = [0, 0, 2.01]  # Scheduler delay overshoots the budget.
        with patch.object(p.pg, 'time', clock), \
                patch.object(p.os, 'killpg', side_effect=[denied(), ProcessLookupError()]) as killpg:
            with self.assertRaises(p.pg.TerminationUncertain): p.signal_group(self.child, signal.SIGTERM)
        self.assertEqual(killpg.call_count, 1)
        p.pg.inspect_groups.assert_not_called()

    def test_outside_group_eperm_at_signal_timeout_and_cleanup(self):
        for site, expected in [('signal', (130, 'kill')), ('timeout', (124, 'timeout')), ('cleanup', (0, None))]:
            with self.subTest(site=site):
                (self.root / 'outside.log').unlink(missing_ok=True)
                options = dict(timeout=60, untrusted_env='default')
                def wait(timeout=None):
                    if timeout == 60:
                        if site == 'signal': signal.getsignal(signal.SIGTERM)(signal.SIGTERM, None)
                        if site == 'timeout': raise subprocess.TimeoutExpired('fixture', timeout)
                    return 0
                self.child.wait.side_effect = wait
                calls = []
                def killpg_at_site(*args):
                    calls.append(args)
                    if len(calls) == 1: raise denied()
                    # A successful re-probe alone must not clear an EPERM.
                with patch.object(p.os, 'killpg', side_effect=killpg_at_site) as killpg:
                    with self.assertRaises(p.GateExecutionError) as failed: self.outside(options)
                self.assertEqual((failed.exception.code, failed.exception.abort), expected if site != 'cleanup' else (25, None))
                self.assertEqual(failed.exception.reason, 'outside gate termination uncertain')
                self.assertGreater(killpg.call_count, 3)
                self.child.kill.assert_not_called()
                self.assertEqual(bool(options.get('_gate_cancelled')), site == 'signal')
                self.assert_handlers_restored()

    def test_outside_live_group_falls_back_and_latches_before_kill(self):
        options = dict(timeout=60, untrusted_env='default')
        self.child.poll.return_value = None
        def kill():
            self.assertTrue(options['_gate_cancelled'])
            self.child.poll.return_value = -signal.SIGKILL
        self.child.kill.side_effect = kill
        def wait(timeout=None):
            if timeout == 60: signal.getsignal(signal.SIGTERM)(signal.SIGTERM, None)
            return -signal.SIGKILL
        self.child.wait.side_effect = wait
        with patch.object(p.os, 'killpg', side_effect=denied()):
            with self.assertRaises(p.GateExecutionError) as failed: self.outside(options)
        self.assertEqual((failed.exception.code, failed.exception.abort), (130, 'kill'))
        self.child.kill.assert_called_once()
        self.assert_handlers_restored()

    def test_outside_failed_direct_kill_preserves_stop_and_restores_handlers(self):
        for cancel in (False, True):
            with self.subTest(cancel=cancel):
                (self.root / 'outside.log').unlink(missing_ok=True)
                options = dict(timeout=60, untrusted_env='default')
                self.child.poll.return_value = None
                self.child.kill.side_effect = denied()
                def wait(timeout=None):
                    if cancel: signal.getsignal(signal.SIGTERM)(signal.SIGTERM, None)
                    raise subprocess.TimeoutExpired('fixture', timeout)
                self.child.wait.side_effect = wait
                with patch.object(p.os, 'killpg', side_effect=denied()), \
                        patch.object(p.pg, 'inspect_groups', return_value={self.child.pid}):
                    with self.assertRaises(p.GateExecutionError) as failure: self.outside(options)
                self.assertEqual((failure.exception.code, failure.exception.abort),
                                 (130, 'kill') if cancel else (124, 'timeout'))
                self.assertEqual(failure.exception.reason, 'outside gate termination uncertain')
                self.assertEqual(bool(options.get('_gate_cancelled')), cancel)
                self.child.kill.assert_called()
                self.assert_handlers_restored()

    def test_outside_cleanup_error_preserves_determined_outcome(self):
        options = dict(timeout=60, untrusted_env='default')
        self.child.wait.side_effect = [0, OSError('cleanup wait failed')]
        with patch.object(p.os, 'killpg', side_effect=denied()):
            with self.assertRaises(p.GateExecutionError) as failure: self.outside(options)
        self.assertEqual(failure.exception.code, 25)
        self.assert_handlers_restored()

    def test_outside_execution_error_restores_handlers(self):
        options = dict(timeout=60, untrusted_env='default')
        self.child.wait.side_effect = [OSError('execution wait failed'), 0]
        with patch.object(p.os, 'killpg', side_effect=ProcessLookupError):
            with self.assertRaisesRegex(p.GateExecutionError, 'outside gate execution failed') as failure: self.outside(options)
        self.assertEqual(str(failure.exception.__cause__), 'execution wait failed')
        self.assert_handlers_restored()

    def test_shared_helpers_live_eperm_fallback_failure_and_esrch(self):
        for module in (p, r):
            with self.subTest(module=module.__name__):
                self.child.reset_mock()
                self.child.poll.return_value = None
                with patch.object(module.os, 'killpg', side_effect=denied()):
                    with self.assertRaises(module.pg.TerminationUncertain): module.signal_group(self.child, signal.SIGTERM)
                    self.child.kill.assert_called_once()
                    self.child.kill.side_effect = denied()
                    with patch.object(module.pg, 'inspect_groups', return_value={self.child.pid}):
                        with self.assertRaises(module.pg.TerminationUncertain): module.signal_group(self.child, signal.SIGKILL)
                self.child.kill.side_effect = None
                self.child.reset_mock()
                with patch.object(module.os, 'killpg', side_effect=ProcessLookupError):
                    module.signal_group(self.child, signal.SIGKILL)
                self.child.kill.assert_not_called()
                self.child.poll.side_effect = OSError('leader status unavailable')
                with patch.object(module.os, 'killpg', side_effect=denied()):
                    with self.assertRaises(module.pg.TerminationUncertain): module.signal_group(self.child, signal.SIGKILL)
                self.child.poll.side_effect = None

    def test_acceptance_eperm_refuses_success_and_preserves_interruption(self):
        for code in (0, 130):
            with self.subTest(code=code), patch.object(a.subprocess, 'Popen', return_value=self.child), \
                    patch.object(a.os, 'killpg', side_effect=denied()):
                self.child.returncode = code
                if code == 130:
                    with self.assertRaises(a.PublicationInterrupted): a.bounded(['gh'], self.root)
                else:
                    with self.assertRaises(a.p.pg.TerminationUncertain): a.bounded(['gh'], self.root)
        self.assertEqual(self.child.communicate.call_count, 2)

    def test_conflicts_eperm_preserves_output_and_trusted_stop(self):
        transaction = c.Transaction.__new__(c.Transaction)
        transaction.repo, transaction.config = self.root, {}
        for code in (0, 130):
            with self.subTest(code=code), patch.object(c.subprocess, 'Popen', return_value=self.child), \
                    patch.object(c.os, 'killpg', side_effect=denied()):
                self.child.returncode = code
                if code == 130:
                    with self.assertRaises(c.ProviderStop) as stopped: transaction.run(['fixture'], trusted=True)
                    self.assertEqual(stopped.exception.code, 130)
                else:
                    transaction.failure_cause = 'human_hold'
                    with self.assertRaises(c.provider.pg.TerminationUncertain): transaction.run(['fixture'])
                self.assertIsNone(transaction.child)

    def test_acceptance_and_conflict_helpers_refuse_live_or_uninspectable_descendants(self):
        for module, inspection_module in ((a, a.p.pg), (c, c.provider.pg)):
            for unavailable in (False, True):
                for code in (0, 130):
                    with self.subTest(module=module.__name__, unavailable=unavailable, code=code):
                        self.child.returncode = code
                        inspection = (dict(side_effect=inspection_module.TerminationUncertain('inspection unavailable'))
                                      if unavailable else dict(return_value={self.child.pid}))
                        with patch.object(module.subprocess, 'Popen', return_value=self.child), \
                                patch.object(module.os, 'killpg', side_effect=denied()), \
                                patch.object(inspection_module, 'inspect_groups', **inspection):
                            if module is a:
                                expected = a.PublicationInterrupted if code == 130 else inspection_module.TerminationUncertain
                                with self.assertRaises(expected): a.bounded(['gh'], self.root)
                            else:
                                transaction = c.Transaction.__new__(c.Transaction)
                                transaction.repo, transaction.config, transaction.failure_cause = self.root, {}, 'human_hold'
                                expected = c.ProviderStop if code == 130 else inspection_module.TerminationUncertain
                                with self.assertRaises(expected): transaction.run(['fixture'], trusted=True)
                                self.assertIsNone(transaction.child)

    def test_dispatch_stop_eperm_race_retains_cancellation(self):
        helper = d.Helpers(self.root)
        helper.child = self.child
        self.child.poll.side_effect = itertools.chain([None], itertools.repeat(0))
        with patch.object(d.os, 'killpg', side_effect=denied()): helper.stop()
        self.assertTrue(helper.stopping)
        with self.assertRaises(d.Stop) as stopped: helper.call('fixture')
        self.assertEqual(stopped.exception.code, 130)

    def test_dispatch_escalation_eperm_retains_cancellation_and_clears_handle(self):
        helper = d.Helpers(self.root, self.root)
        def communicate(timeout=None):
            if self.child.communicate.call_count > 10:
                raise AssertionError('dispatcher escalation exceeded its bound')
            helper.stopping = True
            raise subprocess.TimeoutExpired('fixture', timeout)
        self.child.communicate.side_effect = communicate
        with patch.object(d.subprocess, 'Popen', return_value=self.child), \
                patch.object(d.os, 'killpg', side_effect=denied()), \
                patch.object(d.r, 'save', side_effect=OSError('hold update unavailable')), \
                patch.object(d.r.pg, 'inspect_groups', return_value={self.child.pid}), \
                patch.object(d.time, 'monotonic', side_effect=itertools.count()):
            with self.assertRaises(d.Stop) as stopped: helper.call('fixture')
        self.assertEqual(stopped.exception.code, 130)
        self.assertIsNone(helper.child)
        self.assertLess(self.child.communicate.call_count, 10, 'live stdout survivor must not wedge escalation')
        self.assertTrue(d.r.pg.process_holds(self.root, inactive_only=True))
        with self.assertRaises(d.Stop) as held: d.Helpers(self.root, self.root).call('fixture')
        self.assertEqual(held.exception.code, 25)
        self.assertIn('human_hold', held.exception.output)

    def test_dispatch_hold_is_mandatory_before_spawn(self):
        helper = d.Helpers(self.root, self.control)
        with patch.object(d.r.pg.os, 'fsync', side_effect=OSError('disk full')), \
                patch.object(d.subprocess, 'Popen') as spawn:
            with self.assertRaisesRegex(OSError, 'disk full'): helper.call('fixture')
        spawn.assert_not_called()
        with patch.object(d.r.pg.os, 'scandir', side_effect=OSError('hold store unreadable')), \
                patch.object(d.subprocess, 'Popen') as spawn:
            with self.assertRaises(d.Stop) as held: helper.call('fixture')
        self.assertEqual(held.exception.code, 25)
        spawn.assert_not_called()

    def test_dispatch_active_record_allows_parallel_calls_and_esrch_clears_record(self):
        active = d.r.pg.ProcessHold(self.control, 'outside_gate', self.root)
        self.addCleanup(active.close)
        helper = d.Helpers(self.root, self.control)
        with patch.object(d.subprocess, 'Popen', return_value=self.child), \
                patch.object(d.os, 'killpg', side_effect=ProcessLookupError):
            self.assertEqual(helper.call('fixture'), 'output')
        self.assertEqual(d.r.pg.process_holds(self.control, inactive_only=True), [])
        self.assertEqual(d.r.pg.process_holds(self.control), [active.path])

    def test_uncertain_gate_keeps_exact_ownership_and_worktree(self):
        store = r.Store(self.root)
        run = 'a' * 32
        resources = store.claim('TST-1', self.root, run, 'background')
        registration = store.root / 'workers' / r.hashlib.sha256(str(self.root).encode()).hexdigest()
        registration.parent.mkdir(parents=True)
        registration.write_text('disposable identity')
        progress = self.root / 'unfinished.txt'
        progress.write_text('uncommitted progress')
        leases = store.leases.read_bytes()
        with patch.dict(os.environ, {'BUREAU_RUN_ID': run, 'BUREAU_CURRENT_ISSUE': 'TST-1'}), \
                patch.object(p.os, 'killpg', side_effect=denied()) as killpg:
            started = self.clocks[p.pg].now
            with self.assertRaises(p.GateExecutionError) as failed: self.outside(dict(timeout=60, untrusted_env='default'))
        self.assertEqual(failed.exception.code, 25)
        self.assertEqual(self.clocks[p.pg].now - started, 2.0)
        self.assertTrue(all(call.args[1] == 0 for call in killpg.call_args_list[1:]))
        store.finish_execution(run, resources, False, self.root)
        with self.assertRaisesRegex(r.Conflict, 'human_hold'): store.release(run)
        self.assertTrue(p.pg.process_holds(self.control, inactive_only=True))
        self.assertEqual(store.leases.read_bytes(), leases)
        self.assertEqual(registration.read_text(), 'disposable identity')
        self.assertEqual(progress.read_text(), 'uncommitted progress')
        with self.assertRaises(r.Conflict): store.claim('TST-1', self.root, 'b' * 32, 'background')
        with self.assertRaises(d.Stop) as held: d.Helpers(self.root, self.control).call('fixture')
        self.assertEqual(held.exception.code, 25)

    def test_unowned_dispatch_helper_does_not_retain_other_lane_ownership(self):
        store = r.Store(self.root)
        run = 'b' * 32
        resources = store.claim('TST-1', self.root, run, 'background')
        with patch.dict(os.environ, {}, clear=True):
            active = r.pg.ProcessHold(self.control, 'dispatcher_helper', self.root)
        self.addCleanup(active.close)
        store.finish_execution(run, resources, False, self.root)
        self.assertEqual(r.read(store.leases), {})
        self.assertEqual(r.pg.process_holds(self.control, inactive_only=True), [])

    def test_worker_cleanup_keeps_uncertain_gate_branch_and_registration(self):
        for args in (('config', 'user.name', 'Fixture'), ('config', 'user.email', 'fixture@example.invalid'),
                     ('commit', '-q', '--allow-empty', '-m', 'baseline'), ('branch', '-M', 'fixture-work'),
                     ('update-ref', 'refs/remotes/origin/fixture-work', 'HEAD')):
            subprocess.run(['git', '-C', str(self.root), *args], check=True)
        registration = self.control / 'workers' / r.hashlib.sha256(str(self.root).encode()).hexdigest()
        registration.parent.mkdir(parents=True)
        registration.write_text('worker identity')
        run = 'e' * 32
        with patch.dict(os.environ, {'BUREAU_RUN_ID': run}):
            hold = r.pg.ProcessHold(self.control, 'outside_gate', self.root)
        hold.close()
        source = (SCRIPTS / 'bureau-worker.sh').read_text()
        cleanup = source[source.index('_worker_cleanup() {'):source.index('\ntrap _worker_cleanup EXIT')]
        script = ('REPO_DIR="$1"; WORKTREE="$1"; SCRIPT_DIR="$2"; BUREAU_RUN_ID="$3"; PIPELINE=implement-pipeline.sh\n'
                  + cleanup + '\ntrap _worker_cleanup EXIT\nexit 25\n')
        result = subprocess.run(['/bin/bash', '-c', script, 'fixture', str(self.root), str(SCRIPTS), run],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 25, result.stderr)
        branch = subprocess.check_output(['git', '-C', str(self.root), 'branch', '--show-current'], text=True).strip()
        self.assertEqual(branch, 'fixture-work')
        self.assertEqual(registration.read_text(), 'worker identity')
        # Explicit operator removal restores the ordinary clean-worker cleanup.
        hold.path.unlink()
        result = subprocess.run(['/bin/bash', '-c', script, 'fixture', str(self.root), str(SCRIPTS), run],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 25, result.stderr)
        self.assertEqual(subprocess.check_output(['git', '-C', str(self.root), 'branch', '--show-current'], text=True).strip(), '')

    def test_registration_failure_with_unreaped_child_keeps_leases(self):
        store = r.Store(self.root)
        run = 'c' * 32
        config = self.root / '.bureau.json'
        config.write_text('{}')
        args = argparse.Namespace(workspace=None, entry=None, command=['fixture'], issue='TST-1')
        self.child.wait.side_effect = subprocess.TimeoutExpired('fixture', 1)
        real_save = r.save
        def fail_registration(path, record):
            real_save(path, record)
            if path == store.process_path(run) and record.get('groups') and not record.get('interrupted'):
                raise OSError('registration failed')
        with patch.dict(os.environ, {'BUREAU_RUN_ID': run}, clear=True), \
                patch.object(r, 'config_for', return_value=config), \
                patch.object(r, 'evidence_head', return_value='a' * 40), patch.object(r.e, 'Store'), \
                patch.object(r.subprocess, 'Popen', return_value=self.child), \
                patch.object(r.os, 'killpg', side_effect=denied()), patch.object(r, 'save', side_effect=fail_registration):
            with self.assertRaisesRegex(OSError, 'registration failed'): r.execute(self.root, args, store)
        self.assertEqual(set(r.read(store.leases)), {'issue:TST-1', 'workspace:' + str(self.root)})
        self.assertTrue(r.read(store.process_path(run))['interrupted'])

    def test_outside_esrch_preserves_success_timeout_and_cancellation_and_clears_hold(self):
        for site, expected in (('cleanup', (0, None)), ('signal', (130, 'kill')), ('timeout', (124, 'timeout'))):
            with self.subTest(site=site):
                (self.root / 'outside.log').unlink(missing_ok=True)
                def wait(timeout=None):
                    if timeout == 60:
                        if site == 'signal': signal.getsignal(signal.SIGTERM)(signal.SIGTERM, None)
                        if site == 'timeout': raise subprocess.TimeoutExpired('fixture', timeout)
                    return 0
                self.child.wait.side_effect = wait
                with patch.object(p.os, 'killpg', side_effect=ProcessLookupError):
                    self.assertEqual(self.outside(dict(timeout=60, untrusted_env='default')), expected)
                self.assertEqual(p.pg.process_holds(self.control), [])
                self.assert_handlers_restored()

    def test_successful_signal_needs_reaped_child_and_empty_group(self):
        for live in (False, True):
            with self.subTest(live=live):
                (self.root / 'outside.log').unlink(missing_ok=True)
                with patch.object(p.os, 'killpg'), patch.object(p.pg, 'inspect_groups', return_value={self.child.pid} if live else set()):
                    if live:
                        with self.assertRaises(p.GateExecutionError): self.outside(dict(timeout=60, untrusted_env='default'))
                    else: self.assertEqual(self.outside(dict(timeout=60, untrusted_env='default')), (0, None))

    def test_provider_signal_eperm_preserves_130_and_handlers(self):
        options = p.configuration('implement', {'agents': {'runner': 'claude'}}, {})
        options['untrusted_env'] = 'default'
        def communicate(*args, **kwargs):
            signal.getsignal(signal.SIGTERM)(signal.SIGTERM, None)
            return None, None
        self.child.communicate.side_effect = communicate
        with patch.object(p.subprocess, 'Popen', return_value=self.child), patch.object(p.os, 'killpg', side_effect=denied()):
            code, _, metadata = p._run(options, 'fixture', '', self.root, self.root)
        self.assertEqual(code, 130)
        self.assertEqual(metadata['outcome'], 'cancelled')
        self.assert_handlers_restored()

    def test_runtime_registration_eperm_preserves_failure_and_reaps(self):
        store = r.Store.__new__(r.Store)
        store.root = self.root
        real_save = r.save
        def fail_registration(path, record):
            if record.get('groups'): raise OSError('registration failed')
            real_save(path, record)
        with patch.object(r.subprocess, 'Popen', return_value=self.child), \
                patch.object(r.os, 'killpg', side_effect=denied()), patch.object(r, 'save', side_effect=OSError('registration failed')):
            with self.assertRaisesRegex(OSError, 'registration failed'): store.spawn('run', ['fixture'], self.root, {})
        self.child.wait.assert_not_called()  # A failed write-ahead record prevents launch.
        with patch.object(r.subprocess, 'Popen', return_value=self.child), \
                patch.object(r.os, 'killpg', side_effect=denied()), patch.object(r, 'save', side_effect=fail_registration):
            with self.assertRaisesRegex(OSError, 'registration failed'): store.spawn('run', ['fixture'], self.root, {})
        self.child.wait.assert_called_once()
        self.assertTrue(r.read(store.process_path('run'))['interrupted'])

    def test_runtime_helper_eperm_preserves_timeout_and_reaps(self):
        self.child.communicate.side_effect = [subprocess.TimeoutExpired('fixture', 1), ('', None)]
        with patch.object(r.subprocess, 'Popen', return_value=self.child), patch.object(r.os, 'killpg', side_effect=denied()):
            with self.assertRaises(subprocess.TimeoutExpired): r.shell(self.root, 'fixture', timeout=1)
        self.assertEqual(self.child.communicate.call_count, 1)

    def test_runtime_helper_timeout_with_live_descendant_stays_timeout(self):
        self.child.communicate.side_effect = subprocess.TimeoutExpired('fixture', 1)
        with patch.object(r.subprocess, 'Popen', return_value=self.child), \
                patch.object(r.os, 'killpg', side_effect=denied()), \
                patch.object(r.pg, 'inspect_groups', return_value={self.child.pid}):
            with self.assertRaises(subprocess.TimeoutExpired): r.shell(self.root, 'fixture', timeout=1)
        self.assertEqual(self.child.communicate.call_count, 1)

    def test_provider_timeout_with_live_descendant_stays_124(self):
        options = p.configuration('implement', {'agents': {'runner': 'claude'}}, {})
        options.update(untrusted_env='default', timeout=1)
        self.child.communicate.side_effect = subprocess.TimeoutExpired('fixture', 1)
        with patch.object(p.subprocess, 'Popen', return_value=self.child), \
                patch.object(p.os, 'killpg', side_effect=denied()), \
                patch.object(p.pg, 'inspect_groups', return_value={self.child.pid}), \
                patch.object(p.time, 'monotonic', side_effect=itertools.count(0, 2)):
            code, _, metadata = p._run(options, 'fixture', '', self.root, self.root)
        self.assertEqual(code, 124)
        self.assertEqual(metadata['outcome'], 'timeout')
        self.assertTrue(metadata['termination_uncertain'])
        self.assert_handlers_restored()

    def test_runtime_forward_eperm_preserves_130_and_ownership(self):
        store = Mock(root=self.root)
        store.claim.return_value = ['issue:TST-1']
        store.spawn.return_value = self.child
        config = self.root / 'config.json'
        config.write_text('{}')
        args = argparse.Namespace(workspace=None, entry=None, command=['fixture'], issue='TST-1')
        def wait(timeout=None):
            signal.getsignal(signal.SIGTERM)(signal.SIGTERM, None)
            return 0
        self.child.wait.side_effect = wait
        with patch.dict(os.environ, {}, clear=True), patch.object(r, 'config_for', return_value=config), \
                patch.object(r, 'evidence_head', return_value='a' * 40), patch.object(r.e, 'Store'), \
                patch.object(r.os, 'killpg', side_effect=denied()), \
                patch.object(r.pg, 'inspect_groups', return_value={self.child.pid}):
            self.assertEqual(r.execute(self.root, args, store), 130)
        self.assertTrue(store.finish_execution.call_args.args[2])

    def test_runtime_probe_eperm_always_refuses_release(self):
        store = r.Store.__new__(r.Store)
        with patch.object(r.os, 'killpg', side_effect=denied()), patch.object(r.subprocess, 'check_output') as ps, \
                patch.object(r.pg, 'inspect_groups', self.inspectors[r.pg]):
            ps.return_value = '123456 Z\n'
            with self.assertRaises(r.Conflict): store.assert_groups_stopped({123456})
            ps.return_value = '123456 S\n'
            with self.assertRaises(r.Conflict): store.assert_groups_stopped({123456})
            ps.side_effect = PermissionError('inspection denied')
            with self.assertRaisesRegex(r.Conflict, 'Cannot confirm'): store.assert_groups_stopped({123456})
            ps.assert_not_called()

    def inline_runner(self, target, cancel=False, live=False):
        source = (SCRIPTS / 'implement-pipeline.sh').read_text().split("_POST_IMPLEMENT_RUNNER='\n", 1)[1].split("\n'", 1)[0]
        def killpg(pid, signum):
            if signum == target: raise denied()
            if signum == 0 and not live: raise ProcessLookupError()
        if live:
            self.child.poll.return_value = None
            self.child.kill.side_effect = denied()
        def wait(timeout=None):
            if timeout == 60:
                if cancel: signal.getsignal(signal.SIGTERM)(signal.SIGTERM, None)
                raise subprocess.TimeoutExpired('fixture', timeout)
            return 0
        self.child.wait.side_effect = wait
        with patch.object(sys, 'argv', ['runner', '60', 'fixture', str(self.root / 'status'), str(SCRIPTS)]), \
                patch.object(subprocess, 'Popen', return_value=self.child), patch.object(os, 'killpg', side_effect=killpg), \
                patch.object(subprocess, 'check_output', return_value='123456 S\n' if live else '123456 Z\n'), \
                patch('time.monotonic', side_effect=itertools.count()), patch('time.sleep'):
            exec(compile(source, 'post-implement-runner', 'exec'), {})

    def test_inline_eperm_at_term_probe_and_kill_preserves_timeout_and_signal(self):
        for target in (signal.SIGTERM, 0, signal.SIGKILL):
            for cancel in (False, True):
                with self.subTest(target=target, cancel=cancel):
                    with self.assertRaises(SystemExit) as stopped: self.inline_runner(target, cancel=cancel)
                    self.assertEqual(stopped.exception.code, 143 if cancel else 124)
                    self.assertEqual((self.root / 'status').read_text(), 'signal' if cancel else 'timeout')

    def test_inline_live_eperm_preserves_determined_timeout(self):
        for target in (signal.SIGTERM, 0, signal.SIGKILL):
            with self.subTest(target=target):
                with self.assertRaises(SystemExit) as stopped: self.inline_runner(target, live=True)
                self.assertEqual(stopped.exception.code, 124)
                if target != 0: self.child.kill.assert_called()

    def test_exited_leader_eperm_always_uncertain_and_preserves_stop(self):
        # Model group membership independently from the already-exited leader.
        for membership in ('live', 'unavailable', 'zombies', 'empty'):
            for site, expected in (('signal', (130, 'kill')), ('timeout', (124, 'timeout')), ('cleanup', (0, None))):
                with self.subTest(membership=membership, site=site):
                    (self.root / 'outside.log').unlink(missing_ok=True)
                    options = dict(timeout=60, untrusted_env='default')
                    def wait(timeout=None):
                        if timeout == 60:
                            if site == 'signal': signal.getsignal(signal.SIGTERM)(signal.SIGTERM, None)
                            if site == 'timeout': raise subprocess.TimeoutExpired('fixture', timeout)
                        return 0
                    self.child.wait.side_effect = wait
                    inspection = (dict(side_effect=p.pg.TerminationUncertain('inspection unavailable'))
                                  if membership == 'unavailable' else
                                  dict(return_value={self.child.pid} if membership == 'live' else set()))
                    with patch.object(p.os, 'killpg', side_effect=denied()), patch.object(p.pg, 'inspect_groups', **inspection):
                        with self.assertRaises(p.GateExecutionError) as failed: self.outside(options)
                        self.assertEqual(failed.exception.reason, 'outside gate termination uncertain')
                        self.assertEqual((failed.exception.code, failed.exception.abort),
                                         expected if site != 'cleanup' else (25, None))
                    self.assert_handlers_restored()

    def test_group_snapshot_ignores_zombies_but_refuses_incomplete_inspection(self):
        inspect = self.inspectors[r.pg]
        for output, live in (('123456 Z\n123456 Z+\n', set()), ('', set()),
                             ('123456 Z\n123456 S+\n', {123456})):
            with patch.object(r.subprocess, 'check_output', return_value=output):
                self.assertEqual(inspect({123456}), live)
        for failure in (PermissionError('ps denied'), subprocess.TimeoutExpired('ps', 2)):
            with patch.object(r.subprocess, 'check_output', side_effect=failure):
                with self.assertRaises(r.pg.TerminationUncertain): inspect({123456})
        with patch.object(r.subprocess, 'check_output', return_value='unreadable row\n'):
            with self.assertRaises(r.pg.TerminationUncertain): inspect({123456})


class NativeProcessGroupTests(unittest.TestCase):
    def test_native_zombie_eperm_clears_after_reaping(self):
        child = subprocess.Popen([sys.executable, '-c',
                                  'import sys; print("ready", flush=True); sys.stdin.read()'],
                                 stdin=subprocess.PIPE, stdout=subprocess.PIPE, start_new_session=True)
        def cleanup():
            if child.poll() is None: child.kill()
            child.wait(timeout=3)
            child.stdin.close(); child.stdout.close()
        self.addCleanup(cleanup)
        self.assertEqual(child.stdout.readline(), b'ready\n')
        try: os.killpg(child.pid, 0)
        except PermissionError:
            self.skipTest('sandbox denies probing a live owned process group; native zombie EPERM unavailable')
        child.stdin.close()
        child.stdout.read()  # EOF, without reaping the group leader.
        deadline = time.monotonic() + .5
        while True:
            try: os.killpg(child.pid, 0)
            except PermissionError: break
            if time.monotonic() >= deadline:
                self.skipTest('platform does not return EPERM for an unreaped zombie-only group')
            time.sleep(.01)
        first_probe = threading.Event()
        reaped = threading.Event()
        def reap():
            if first_probe.wait(timeout=3):
                time.sleep(.03)
                child.wait(timeout=3)
                reaped.set()
        reaper = threading.Thread(target=reap)
        reaper.start()
        self.addCleanup(reaper.join, 5)
        real_killpg = os.killpg
        def probe(group, signum):
            try: return real_killpg(group, signum)
            except PermissionError:
                first_probe.set()
                raise
        with tempfile.TemporaryDirectory(prefix='bureau native zombie ') as directory:
            hold = p.pg.ProcessHold(directory, 'outside_gate', directory)
            stopped = False
            try:
                with patch.object(p.os, 'killpg', side_effect=probe): p.signal_group(child, signal.SIGTERM)
                stopped = True
            finally: hold.close(stopped=stopped)
            self.assertTrue(reaped.wait(timeout=1))
            self.assertEqual(p.pg.process_holds(directory), [])


if __name__ == '__main__': unittest.main()
