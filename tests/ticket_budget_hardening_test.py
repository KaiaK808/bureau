"""Each safety regression also runs against a private, deliberately broken copy."""
import importlib.util
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
import unittest

from ticket_budget_test import AdapterFixture


class Hardening(AdapterFixture):
    mutants = False

    def replace(self, file, old, new):
        path = self.scripts / file
        text = path.read_text()
        self.assertEqual(1, text.count(old), 'negative-control anchor changed: ' + old)
        path.write_text(text.replace(old, new, 1))

    def setUp(self):
        super().setUp()
        if self.mutants: self.break_guard()
        spec = importlib.util.spec_from_file_location('private_ticket', self.scripts / 'bureau-ticket.py')
        self.policy = importlib.util.module_from_spec(spec); spec.loader.exec_module(self.policy)

    def break_guard(self):
        name = self._testMethodName
        if name == 'test_latch_precedes_diagnostics':
            self.replace('bureau-config.sh', '  _BUREAU_TICKET_STOPPED=1\n',
                         '  echo "stop diagnostic before preservation" >&2\n  _BUREAU_TICKET_STOPPED=1\n')
        elif name == 'test_incomplete_turns_are_unknown':
            path = self.scripts / 'bureau-ticket.py'
            text = path.read_text(); start = text.index('def reported_usage('); end = text.index('\n\ndef reported_lower_bound(', start)
            # Restore the lossy parser; use the real adapter and raw output.
            text = text[:start] + '''def reported_usage(provider, directory, complete=True):
    events = []
    for line in (Path(directory) / 'stdout.log').read_text().splitlines():
        try: events.append(json.loads(line))
        except ValueError: pass
    turns = [v.get('usage') for v in events if v.get('type') == 'turn.completed']
    if not turns or any(token_count(v, provider) is None for v in turns): return None
    return {key: sum(v[key] for v in turns) for key in ('input_tokens', 'output_tokens')}
''' + text[end:]
            path.write_text(text)
        elif name == 'test_lost_accounting_requires_explicit_reset':
            self.replace('bureau-ticket.py', "            if witnessed - set(value['attempts']): value['accounting_lost'] = True",
                         '            if False: pass  # deliberately ignore lost accounting')
        elif name == 'test_quiet_waits_do_not_spend_progress':
            self.replace('bureau-runtime.py', 't.completed_work_turn(code)', 'code not in (20, 21, 25, 26, 130)')
        elif name == 'test_gate_proof_cannot_be_submitted':
            self.replace('bureau-ticket.py', "    gate = sub.add_parser('execute-gate')", '''    proof = sub.add_parser('gate')
    for name in ('path', 'head', 'end-head', 'command', 'command-id'): proof.add_argument('--' + name, required=True)
    proof.add_argument('--exit-code', type=int, required=True)
    gate = sub.add_parser('execute-gate')''')
            self.replace('bureau-ticket.py', "        elif args.action == 'reset': ledger.reset()", '''        elif args.action == 'gate':
            ledger._record_gate(run, os.environ.get('BUREAU_STAGE_ATTEMPT_ID'), args.command_id, args.command,
                                args.exit_code, args.head, args.end_head, args.path)
        elif args.action == 'reset': ledger.reset()''')
        elif name == 'test_review_build_is_a_qualified_gate':
            self.replace('bureau-ticket.py', "GATE_IDS = ('repo.test_command', 'qa.test', 'review.build')",
                         "GATE_IDS = ('repo.test_command', 'qa.test')")
        elif name == 'test_standalone_stops_record_reasons':
            self.replace('bureau-runtime.py', '                if args.issue:\n                    terminal_reason =',
                         '                if dispatch_record is not None and args.issue:\n                    terminal_reason =')
        elif name == 'test_nested_terminal_reason_is_validated':
            path = self.scripts / 'bureau-ticket.py'; text = path.read_text()
            start = text.index("            prior = value.get('last_stop')")
            end = text.index("            if witnessed - set(value['attempts'])", start)
            path.write_text(text[:start] + text[end:])
        else: self.fail('no negative control for ' + name)

    def ledger_object(self):
        return self.policy.Ledger(self.repo / '.git/bureau', 'TEAM-1', self.config)

    def cli(self, *args, **env):
        return subprocess.run([sys.executable, '-I', str(self.scripts / 'bureau-ticket.py'),
                               '--repo', str(self.repo), '--issue', 'TEAM-1', *args],
                              cwd=self.repo, env={**self.env, **env}, capture_output=True, text=True, timeout=15)

    def runtime(self, code, entry='implement-pipeline.sh'):
        env = {k: v for k, v in self.env.items() if k not in ('BUREAU_RUN_ID', 'BUREAU_STAGE_ATTEMPT_ID')}
        return subprocess.run([sys.executable, '-I', str(self.scripts / 'bureau-runtime.py'), '--repo', str(self.repo),
                               'exec', '--issue', 'TEAM-1', '--workspace', str(self.repo), '--entry', entry,
                               '--', '/bin/bash', '-c', 'exit ' + str(code)], cwd=self.repo, env=env,
                              capture_output=True, text=True, timeout=15)

    def test_latch_precedes_diagnostics(self):
        self.config['agents']['ticket_budget'] = dict(provider_calls=1); self.save()
        self.assertEqual(0, self.provider().returncode)
        cases = [('latched', None, 25), ('strict', 'done', 0), ('strict', 'parked', 25), ('strict', 'cancelled', 26)]
        self.config['linear']['teams'][0]['states']['cancelled'] = 'cancelled'
        for mode, state, code in cases:
            if state:
                self.config['agents'].pop('ticket_budget', None); self.save()
            for sink in ('pipe', 'closed'):
                with self.subTest(mode=mode, state=state, sink=sink):
                    control = self.root / ('stop-' + str(state) + '-' + sink)
                    snapshot = json.dumps(dict(identifier='TEAM-1', state=dict(id=state))) if state else ''
                    script = ('exec 2>&-\n' if sink == 'closed' else '') + '''source scripts/bureau-config.sh
bureau_ticket_checkpoint TEAM-1 "$1" "$2"
echo unauthorized > continued
'''
                    reader, writer = os.pipe(); os.close(reader)
                    try:
                        child = subprocess.Popen(['/bin/bash', '-e', '-c', script, 'fixture', mode, snapshot],
                                                 cwd=self.repo, env={**self.env, 'BUREAU_TICKET_CONTROL_DIR': str(control)},
                                                 stdout=subprocess.PIPE, stderr=writer, text=True)
                    finally: os.close(writer)
                    try:
                        stdout, _ = child.communicate(timeout=15)
                        self.assertTrue((control / ('stop-' + str(code))).exists(), 'latch must precede diagnostics: ' + stdout)
                        self.assertEqual(code, child.returncode, stdout)
                        self.assertFalse((self.repo / 'continued').exists())
                    finally:
                        if child.poll() is None: child.terminate()
                        child.communicate(timeout=10)

    def test_incomplete_turns_are_unknown(self):
        self.config['agents']['ticket_budget'] = dict(tokens=100); self.save()
        binary = self.bin / 'codex'
        binary.write_text(binary.read_text() + '''
tail = os.environ.get('FAKE_TAIL', '')
if tail == 'failed': print(json.dumps(dict(type='turn.failed')))
elif tail == 'started': print(json.dumps(dict(type='turn.started')))
elif tail == 'item': print(json.dumps(dict(type='item.started', item=dict(type='command_execution'))))
elif tail == 'truncated': print('{"type":"turn.compl', end='')
elif tail == 'cancelled':
    print(json.dumps(dict(type='turn.started')), flush=True)
    (root/'tail-ready').touch()
    time.sleep(30)
''')
        for tail in ('started', 'failed', 'item', 'truncated'):
            with self.subTest(tail=tail):
                self.reset_accounting()
                result = self.provider(FAKE_TAIL=tail)
                self.assertIn(result.returncode, (0, 22), result.stderr)
                stop = self.cli('check')
                self.assertEqual(25, stop.returncode, 'incomplete turn usage must hold: ' + stop.stdout + stop.stderr)
                self.assertTrue(json.loads(stop.stdout)['usage_unknown'])
                self.assertIsNone(next(iter(self.ledger()['attempts'].values()))['tokens'])
                self.assertEqual(8, json.loads(stop.stdout)['totals']['tokens_known'])
                before = self.events()
                self.assertEqual(25, self.provider().returncode)
                self.assertEqual(before, self.events())
        self.reset_accounting()
        command = [sys.executable, '-I', str(self.scripts / 'bureau-provider.py'), '--repo', str(self.repo),
                   '--config', str(self.repo / '.bureau.json'), '--stage', 'implement', '--prompt-file', str(self.prompt)]
        child = subprocess.Popen(command, cwd=self.repo, env={**self.env, 'FAKE_TAIL': 'cancelled'},
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            deadline = time.monotonic() + 5
            while not (self.root / 'tail-ready').exists() and time.monotonic() < deadline: time.sleep(.01)
            self.assertTrue((self.root / 'tail-ready').exists())
            child.send_signal(signal.SIGTERM)
            stdout, stderr = child.communicate(timeout=10)
            self.assertEqual(130, child.returncode, stdout + stderr)
            self.assertIsNone(next(iter(self.ledger()['attempts'].values()))['tokens'])
            self.assertEqual(25, self.cli('check').returncode)
        finally:
            if child.poll() is None: child.terminate()
            child.communicate(timeout=10)
        self.config['agents']['ticket_budget'] = dict(tokens=8, allow_unknown_usage=True); self.save()
        self.reset_accounting()
        self.assertEqual(0, self.provider(FAKE_TAIL='started').returncode)
        stop = self.cli('check')
        self.assertEqual(25, stop.returncode, stop.stdout + stop.stderr)
        self.assertFalse(json.loads(stop.stdout)['usage_unknown'])
        self.assertEqual(8, json.loads(stop.stdout)['totals']['tokens_known'])
        # Claude streaming also needs a final measured result, rather than an
        # earlier result followed by unmeasured activity from another turn.
        self.config['agents'].update(runner='claude', ticket_budget=dict(tokens=100)); self.save()
        self.reset_accounting()
        binary = self.bin / 'claude'
        binary.write_text(binary.read_text().replace("dict(result='done',usage=usage)",
                          "dict(type='result',result='done',usage=usage)") +
                          "\nprint(json.dumps(dict(type='assistant',message='unmeasured turn')))\n")
        result = self.provider()
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        stop = self.cli('check')
        self.assertEqual(25, stop.returncode, stop.stdout + stop.stderr)
        self.assertTrue(json.loads(stop.stdout)['usage_unknown'])
        self.assertEqual(10, json.loads(stop.stdout)['totals']['tokens_known'])

    def test_lost_accounting_requires_explicit_reset(self):
        self.config['agents']['ticket_budget'] = dict(provider_calls=2, allow_unknown_usage=True); self.save()
        path = self.repo / '.git/bureau/tickets/TEAM-1.json'
        for damage in ('missing', 'empty', 'empty-object', 'unreadable', 'attempts', 'receipts-only', 'pruned'):
            with self.subTest(damage=damage):
                self.reset_accounting()
                self.assertEqual(0, self.provider().returncode)
                before = self.events()
                if damage == 'attempts':
                    value = self.ledger(); value['attempts'] = {}; path.write_text(json.dumps(value))
                elif damage == 'empty': path.write_text('')
                elif damage == 'empty-object': path.write_text('{}')
                else:
                    path.unlink()
                    if damage == 'unreadable': path.mkdir()
                    elif damage == 'receipts-only': path.with_suffix('.attempts.json').unlink()
                    elif damage == 'pruned':
                        import shutil
                        shutil.rmtree(self.repo / '.git/bureau/evidence')
                stop = self.cli('check')
                self.assertEqual(25, stop.returncode, 'lost accounting must hold: ' + stop.stdout + stop.stderr)
                self.assertTrue(json.loads(stop.stdout)['accounting_lost'])
                self.assertEqual('budget', json.loads(stop.stdout)['terminal_reason'])
                self.assertEqual(25, self.provider().returncode)
                self.assertEqual(before, self.events())
                if path.is_dir(): path.rmdir()
                self.reset_accounting()
                self.assertEqual(0, self.provider().returncode)
        leases = self.repo / '.git/bureau/leases.json'
        leases.write_text(json.dumps({'issue:TEAM-1': dict(run_id='a' * 32)}))
        result = self.cli('reset', '--acknowledge-accounting-loss', BUREAU_RUN_ID='')
        self.assertEqual(24, result.returncode)
        self.assertIn('ownership', result.stderr)

    def test_quiet_waits_do_not_spend_progress(self):
        self.config['agents']['ticket_progress'] = dict(enabled=True); self.save()
        for code in (2, 10, 16, 23, 28, 29, 20, 21, 24, 25, 26, 27, 124):
            result = self.runtime(code)
            self.assertEqual(code, result.returncode, 'wait must not count as progress: ' + result.stdout + result.stderr)
        with self.ledger_object().guard() as value:
            self.assertEqual(0, value['non_improving_turns']); self.assertEqual({}, value['turns'])
        self.assertEqual(0, self.runtime(0).returncode)
        result = self.runtime(0)
        self.assertEqual(25, result.returncode, result.stdout + result.stderr)
        self.assertEqual(('stalled', 2), (self.ledger()['terminal_reason'], self.ledger()['non_improving_turns']))
        self.reset_accounting()
        self.assertEqual(14, self.runtime(14).returncode)
        self.assertEqual(1, self.ledger()['non_improving_turns'])
        self.assertEqual(25, self.runtime(0).returncode)

    def configure_gate(self, command_id):
        command = 'echo executed >> executed; printf green'
        self.config['repo'] = dict(test_command=command)
        self.config['agents']['ticket_progress'] = dict(enabled=True, contract=dict(qualified_gates=dict(
            acceptance=dict(command_id=command_id, command=command))))
        self.save(); self.env['BUREAU_STAGE_ATTEMPT_ID'] = 'a' * 32
        return command

    def test_gate_proof_cannot_be_submitted(self):
        command = self.configure_gate('repo.test_command')
        ledger = self.ledger_object(); ledger.end_turn('one', self.env['BUREAU_RUN_ID'])
        proof = self.repo / 'README.md'; proof.write_text('invented green output')
        head = subprocess.check_output(['git', '-C', str(self.repo), 'rev-parse', 'HEAD'], text=True).strip()
        self.cli('gate', '--path', str(proof), '--head', head, '--end-head', head, '--command', command,
                 '--command-id', 'repo.test_command', '--exit-code', '0')
        self.assertEqual({}, self.ledger()['facts'], 'caller proof must not establish progress')
        result = subprocess.run(['/bin/bash', '-e', '-c', '''source scripts/bureau-config.sh
bureau_record_gate README.md "$1" "$2" 0 repo.test_command
''', 'fixture', head, command], cwd=self.repo, env=self.env, capture_output=True, text=True, timeout=15)
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual({}, self.ledger()['facts'])
        self.assertFalse((self.repo / 'executed').exists())
        forged = self.cli('execute-gate', '--path', str(proof), '--command', command,
                          '--command-id', 'repo.test_command', '--exit-code', '0')
        self.assertEqual(2, forged.returncode)
        result = self.cli('execute-gate', '--path', str(self.repo / 'actual.log'), '--command', command,
                          '--command-id', 'repo.test_command')
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertEqual('executed\n', (self.repo / 'executed').read_text())
        self.assertIsNone(ledger.end_turn('two', self.env['BUREAU_RUN_ID']))
        self.assertEqual(0, self.ledger()['non_improving_turns'])
        self.assertEqual(1, len(self.ledger()['facts']))

    def test_review_build_is_a_qualified_gate(self):
        command = self.configure_gate('review.build')
        result = self.runtime(0)
        self.assertEqual(0, result.returncode, 'review.build must qualify: ' + result.stdout + result.stderr)
        result = subprocess.run(['/bin/bash', '-e', '-c', '''source scripts/bureau-config.sh
bureau_run_gate review.log "$1" review.build
''', 'fixture', command], cwd=self.repo, env=self.env, capture_output=True, text=True, timeout=15)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertEqual('green', (self.repo / 'review.log').read_text())
        self.assertIsNone(self.ledger_object().end_turn('after-review', self.env['BUREAU_RUN_ID']))
        self.assertEqual(0, self.ledger()['non_improving_turns'])
        self.assertEqual(1, len(self.ledger()['facts']))

    def test_standalone_stops_record_reasons(self):
        for code, reason in ((25, 'human_hold'), (124, 'timeout'), (26, 'ticket_withdrawn')):
            result = self.runtime(code, 'shepherd.sh')
            self.assertEqual(code, result.returncode, result.stdout + result.stderr)
            self.assertTrue((self.repo / '.git/bureau/tickets/TEAM-1.json').exists(), 'standalone stop must persist reason')
            self.assertEqual(reason, self.ledger()['terminal_reason'])
            self.assertEqual((code, reason), (self.ledger()['last_stop']['code'], self.ledger()['last_stop']['terminal_reason']))

    def test_nested_terminal_reason_is_validated(self):
        self.config['agents']['ticket_budget'] = dict(tokens=100); self.save()
        ledger = self.ledger_object(); ledger.note_stop('budget', 25, self.env['BUREAU_RUN_ID'])
        path = ledger.path
        original = json.loads(path.read_text())
        for reason, code in (('invented', 25), ('timeout', 25), (None, 25), ('budget', 124)):
            value = json.loads(json.dumps(original)); value['last_stop'].update(terminal_reason=reason, code=code)
            path.write_text(json.dumps(value))
            result = self.cli('check')
            self.assertEqual(24, result.returncode, 'nested reason must be rejected: ' + result.stdout + result.stderr)
            self.assertEqual('', result.stdout)
            result = self.runtime(25, 'shepherd.sh')
            self.assertEqual(24, result.returncode, result.stdout + result.stderr)
        path.write_text(json.dumps(original))
        self.assertEqual('budget', ledger.terminal(25, self.env['BUREAU_RUN_ID']))


if __name__ == '__main__':
    negative = '--negative-control' in sys.argv
    if negative:
        Hardening.mutants = True
        for name in unittest.defaultTestLoader.getTestCaseNames(Hardening):
            setattr(Hardening, name, unittest.expectedFailure(getattr(Hardening, name)))
    result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(Hardening))
    if negative and len(result.expectedFailures) != 8:
        sys.exit('each of the eight guard removals must fail its regression assertion')
    markers = dict(
        test_latch_precedes_diagnostics='latch must precede diagnostics',
        test_incomplete_turns_are_unknown='incomplete turn usage must hold',
        test_lost_accounting_requires_explicit_reset='lost accounting must hold',
        test_quiet_waits_do_not_spend_progress='wait must not count as progress',
        test_gate_proof_cannot_be_submitted='caller proof must not establish progress',
        test_review_build_is_a_qualified_gate='review.build must qualify',
        test_standalone_stops_record_reasons='standalone stop must persist reason',
        test_nested_terminal_reason_is_validated='nested reason must be rejected')
    if negative:
        for case, failure in result.expectedFailures:
            if markers[case._testMethodName] not in failure:
                sys.exit('negative control failed before reaching its safety assertion: ' + case._testMethodName)
    if negative and result.wasSuccessful(): print('PASS: all eight private guard removals were detected')
    sys.exit(not result.wasSuccessful())
