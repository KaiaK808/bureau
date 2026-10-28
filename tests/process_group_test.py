"""Deterministic EPERM races at owned process-group termination boundaries."""
import argparse
import errno
import importlib.util
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
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


class ProcessGroupTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='bureau process groups ')
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.child = Mock(pid=123456, returncode=0)
        self.child.poll.return_value = 0
        self.child.wait.return_value = 0
        self.child.communicate.return_value = ('output', None)
        self.previous = {sig: signal.getsignal(sig) for sig in
                         (signal.SIGTERM, signal.SIGINT, signal.SIGHUP, signal.SIGALRM)}
        self.addCleanup(self.restore_handlers)

    def restore_handlers(self):
        signal.setitimer(signal.ITIMER_REAL, 0)
        for sig, handler in self.previous.items(): signal.signal(sig, handler)

    def assert_handlers_restored(self):
        for sig, handler in self.previous.items(): self.assertEqual(signal.getsignal(sig), handler)

    def outside(self, options):
        with patch.object(p.subprocess, 'Popen', return_value=self.child):
            return p.outside_gate(options, self.root, 'fixture', self.root / 'outside.log')

    def test_outside_group_eperm_at_signal_timeout_and_cleanup(self):
        for site, expected in [('signal', (130, 'kill')), ('timeout', (124, 'timeout')), ('cleanup', (0, None))]:
            with self.subTest(site=site):
                (self.root / 'outside.log').unlink(missing_ok=True)
                options = dict(timeout=60, untrusted_env='default')
                def wait(timeout=None):
                    if timeout is not None:
                        if site == 'signal': signal.getsignal(signal.SIGTERM)(signal.SIGTERM, None)
                        if site == 'timeout': raise subprocess.TimeoutExpired('fixture', timeout)
                    return 0
                self.child.wait.side_effect = wait
                calls = []
                def killpg_at_site(*args):
                    calls.append(args)
                    if len(calls) == 1: raise denied()
                with patch.object(p.os, 'killpg', side_effect=killpg_at_site) as killpg:
                    self.assertEqual(self.outside(options), expected)
                self.assertEqual(killpg.call_count, 1 if site == 'cleanup' else 2)
                self.child.kill.assert_not_called()
                self.assertEqual(bool(options.get('_gate_cancelled')), site == 'signal')
                self.assertFalse(options.get('_gate_execution_failed'))
                self.assert_handlers_restored()

    def test_outside_live_group_falls_back_and_latches_before_kill(self):
        options = dict(timeout=60, untrusted_env='default')
        self.child.poll.return_value = None
        def kill():
            self.assertTrue(options['_gate_cancelled'])
            self.child.poll.return_value = -signal.SIGKILL
        self.child.kill.side_effect = kill
        def wait(timeout=None):
            if timeout is not None: signal.getsignal(signal.SIGTERM)(signal.SIGTERM, None)
            return -signal.SIGKILL
        self.child.wait.side_effect = wait
        with patch.object(p.os, 'killpg', side_effect=denied()):
            self.assertEqual(self.outside(options), (130, 'kill'))
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
                with patch.object(p.os, 'killpg', side_effect=denied()):
                    self.assertEqual(self.outside(options), (130, 'kill') if cancel else (124, 'timeout'))
                self.assertEqual(bool(options.get('_gate_cancelled')), cancel)
                self.assertTrue(options['_gate_execution_failed'])
                self.child.kill.assert_called()
                self.assert_handlers_restored()

    def test_outside_cleanup_error_preserves_determined_outcome(self):
        options = dict(timeout=60, untrusted_env='default')
        self.child.wait.side_effect = [0, OSError('cleanup wait failed')]
        with patch.object(p.os, 'killpg', side_effect=denied()):
            self.assertEqual(self.outside(options), (0, None))
        self.assertTrue(options['_gate_execution_failed'])
        self.assert_handlers_restored()

    def test_outside_execution_error_restores_handlers(self):
        options = dict(timeout=60, untrusted_env='default')
        self.child.wait.side_effect = [OSError('execution wait failed'), 0]
        with patch.object(p.os, 'killpg', side_effect=denied()):
            with self.assertRaisesRegex(OSError, 'execution wait failed'): self.outside(options)
        self.assert_handlers_restored()

    def test_shared_helpers_live_eperm_fallback_failure_and_esrch(self):
        for module in (p, r):
            with self.subTest(module=module.__name__):
                self.child.reset_mock()
                self.child.poll.return_value = None
                with patch.object(module.os, 'killpg', side_effect=denied()):
                    module.signal_group(self.child, signal.SIGTERM)
                    self.child.kill.assert_called_once()
                    self.child.kill.side_effect = denied()
                    with self.assertRaises(PermissionError): module.signal_group(self.child, signal.SIGKILL)
                self.child.kill.side_effect = None
                self.child.reset_mock()
                with patch.object(module.os, 'killpg', side_effect=ProcessLookupError):
                    module.signal_group(self.child, signal.SIGKILL)
                self.child.kill.assert_not_called()

    def test_acceptance_eperm_preserves_success_and_interruption(self):
        for code in (0, 130):
            with self.subTest(code=code), patch.object(a.subprocess, 'Popen', return_value=self.child), \
                    patch.object(a.os, 'killpg', side_effect=denied()):
                self.child.returncode = code
                if code == 130:
                    with self.assertRaises(a.PublicationInterrupted): a.bounded(['gh'], self.root)
                else: self.assertEqual(a.bounded(['gh'], self.root), (0, 'output'))
        self.assertEqual(self.child.communicate.call_count, 4)

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
                else: self.assertEqual(transaction.run(['fixture']), 'output')
                self.assertIsNone(transaction.child)

    def test_dispatch_stop_eperm_race_retains_cancellation(self):
        helper = d.Helpers(self.root)
        helper.child = self.child
        self.child.poll.side_effect = [None, 0]
        with patch.object(d.os, 'killpg', side_effect=denied()): helper.stop()
        self.assertTrue(helper.stopping)
        with self.assertRaises(d.Stop) as stopped: helper.call('fixture')
        self.assertEqual(stopped.exception.code, 130)

    def test_dispatch_escalation_eperm_retains_cancellation_and_clears_handle(self):
        helper = d.Helpers(self.root)
        def communicate(timeout=None):
            helper.stopping = True
            self.child.communicate.side_effect = None
            raise subprocess.TimeoutExpired('fixture', timeout)
        self.child.communicate.side_effect = communicate
        with patch.object(d.subprocess, 'Popen', return_value=self.child), \
                patch.object(d.os, 'killpg', side_effect=denied()), \
                patch.object(d.time, 'monotonic', side_effect=[1, 10]):
            with self.assertRaises(d.Stop) as stopped: helper.call('fixture')
        self.assertEqual(stopped.exception.code, 130)
        self.assertIsNone(helper.child)

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
        with patch.object(r.subprocess, 'Popen', return_value=self.child), \
                patch.object(r.os, 'killpg', side_effect=denied()), patch.object(r, 'save', side_effect=OSError('registration failed')):
            with self.assertRaisesRegex(OSError, 'registration failed'): store.spawn('run', ['fixture'], self.root, {})
        self.child.wait.assert_called_once()

    def test_runtime_helper_eperm_preserves_timeout_and_reaps(self):
        self.child.communicate.side_effect = [subprocess.TimeoutExpired('fixture', 1), ('', None)]
        with patch.object(r.subprocess, 'Popen', return_value=self.child), patch.object(r.os, 'killpg', side_effect=denied()):
            with self.assertRaises(subprocess.TimeoutExpired): r.shell(self.root, 'fixture', timeout=1)
        self.assertEqual(self.child.communicate.call_count, 2)

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
                patch.object(r.os, 'killpg', side_effect=denied()):
            self.assertEqual(r.execute(self.root, args, store), 130)
        self.assertTrue(store.finish_execution.call_args.args[2])

    def test_runtime_probe_eperm_requires_positive_process_state_inspection(self):
        store = r.Store.__new__(r.Store)
        with patch.object(r.os, 'killpg', side_effect=denied()), patch.object(r.subprocess, 'check_output') as ps:
            ps.return_value = '123456 Z\n'
            store.assert_groups_stopped({123456})
            ps.return_value = '123456 S\n'
            with self.assertRaises(r.Conflict): store.assert_groups_stopped({123456})
            ps.side_effect = PermissionError('inspection denied')
            with self.assertRaisesRegex(r.Conflict, 'Cannot confirm'): store.assert_groups_stopped({123456})

    def inline_runner(self, target, cancel=False, live=False):
        source = (SCRIPTS / 'implement-pipeline.sh').read_text().split("_POST_IMPLEMENT_RUNNER='\n", 1)[1].split("\n'", 1)[0]
        def killpg(pid, signum):
            if signum == target: raise denied()
            if signum == 0: raise ProcessLookupError()
        if live:
            self.child.poll.return_value = None
            self.child.kill.side_effect = denied()
        def wait(timeout=None):
            if timeout is not None:
                if cancel: signal.getsignal(signal.SIGTERM)(signal.SIGTERM, None)
                raise subprocess.TimeoutExpired('fixture', timeout)
            return 0
        self.child.wait.side_effect = wait
        with patch.object(sys, 'argv', ['runner', '60', 'fixture', str(self.root / 'status')]), \
                patch.object(subprocess, 'Popen', return_value=self.child), patch.object(os, 'killpg', side_effect=killpg), \
                patch('time.monotonic', side_effect=[0, 1]), patch('time.sleep'):
            exec(compile(source, 'post-implement-runner', 'exec'), {})

    def test_inline_eperm_at_term_probe_and_kill_preserves_timeout_and_signal(self):
        for target in (signal.SIGTERM, 0, signal.SIGKILL):
            for cancel in (False, True):
                with self.subTest(target=target, cancel=cancel):
                    with self.assertRaises(SystemExit) as stopped: self.inline_runner(target, cancel=cancel)
                    self.assertEqual(stopped.exception.code, 143 if cancel else 124)
                    self.assertEqual((self.root / 'status').read_text(), 'signal' if cancel else 'timeout')

    def test_inline_live_eperm_direct_kill_failure_surfaces(self):
        for target in (signal.SIGTERM, 0, signal.SIGKILL):
            with self.subTest(target=target):
                with self.assertRaises(PermissionError): self.inline_runner(target, live=True)
                self.child.kill.assert_called()


if __name__ == '__main__': unittest.main()
