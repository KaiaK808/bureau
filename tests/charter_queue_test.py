#!/usr/bin/env python3
"""Pure-Python queue, snapshot and path protocol checks; no real processes."""
import copy
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import types
import unittest
from unittest.mock import patch

SOURCE = Path(__file__).resolve().parents[1] / 'templates/scripts/bureau-charter.py'
spec = importlib.util.spec_from_file_location('charter', SOURCE)
charter = importlib.util.module_from_spec(spec)
spec.loader.exec_module(charter)
ISSUE = 'TEST-' + str(1)
CYCLE = '12345678-1234-1234-1234-123456789abc'


def policy(**values):
    return charter.policy_from_config(dict(agents=dict(merge_charter=dict(enabled=True, **values))))


def snapshot(labels=(), cycle=None):
    return dict(data=dict(issues=dict(nodes=[dict(identifier=ISSUE, cycle=cycle,
                labels=dict(nodes=[dict(name=name) for name in labels], pageInfo=dict(hasNextPage=False)))],
                pageInfo=dict(hasNextPage=False))))


def mutated(old, new):
    source = SOURCE.read_text()
    if source.count(old) != 1:
        raise AssertionError('missing or ambiguous mutation: ' + old)
    module = types.ModuleType('charter_mutant')
    module.__file__ = str(SOURCE)
    exec(compile(source.replace(old, new), '<queue mutation>', 'exec'), module.__dict__)
    return module


class QueueTests(unittest.TestCase):
    def evaluate(self, value, labels=(), cycle=None, failed=False, paths=(), module=charter):
        calls = []
        def read(with_cycle):
            calls.append(with_cycle)
            if failed:
                raise ValueError('service unavailable')
            return dict(labels=list(labels), cycle_id=cycle)
        code, message = module.selector_gates(value, paths, read)
        return code, message, calls

    def test_label_and_cycle_are_exact_separate_queue_modes(self):
        label = policy(approved_queue=dict(label='approved-for-merge'))
        self.assertEqual(self.evaluate(label, labels=['approved-for-merge'])[0], 0)
        self.assertEqual(self.evaluate(label, labels=['Approved-for-merge'])[0], 2)
        self.assertEqual(self.evaluate(label)[0], 2)
        cycle = policy(approved_queue=dict(cycle_id=CYCLE))
        self.assertEqual(self.evaluate(cycle, cycle=CYCLE.upper())[0], 0)
        self.assertEqual(self.evaluate(cycle, cycle='0' + CYCLE[1:])[0], 2)
        self.assertEqual(self.evaluate(cycle, labels=['approved-for-merge'])[0], 2)
        self.assertEqual(self.evaluate(cycle)[2], [True])

    def test_one_shared_snapshot_and_no_unneeded_reads(self):
        value = policy(approved_queue=dict(label='approved'), exceptions=[dict({'class': 'security'}, labels=['sensitive'])])
        code, message, calls = self.evaluate(value, labels=['sensitive'])
        self.assertEqual(code, 25)
        self.assertIn('security', message)
        self.assertEqual(calls, [False])
        self.assertEqual(self.evaluate(policy(exceptions=[dict({'class': 'files'}, paths=['auth/**'])]))[2], [])
        def observation(module):
            self.assertEqual(self.evaluate(value, labels=['sensitive'], module=module)[2], [False])
        observation(charter)
        weakened = mutated("labels = ticket['labels'] if ticket is not None else []",
                           "labels = read_issue(False)['labels'] if ticket is not None else []")
        with self.assertRaises(AssertionError):
            observation(weakened)

    def test_failed_read_waits_or_becomes_unclassified_according_to_dependencies(self):
        value = policy(approved_queue=dict(label='approved'))
        self.assertEqual(self.evaluate(value, failed=True)[0], 2)
        value['exceptions'] = [dict({'class': 'security'}, labels=['sensitive'])]
        code, message, calls = self.evaluate(value, failed=True)
        self.assertEqual(code, 25)
        self.assertIn('charter-unclassified:', message)
        self.assertEqual(calls, [False])
        value['approved_queue'] = None
        self.assertEqual(self.evaluate(value, failed=True)[0], 25)

    def test_all_classes_match_by_either_selector_and_dominate_missing_approval(self):
        value = policy(approved_queue=dict(label='approved'), exceptions=[
            dict({'class': 'security'}, paths=['auth/**'], labels=['sensitive']),
            dict({'class': 'data'}, labels=['schema-change']), dict({'class': 'other'}, paths=['other/**'])])
        code, message, _ = self.evaluate(value, labels=['schema-change'], paths=['auth/old.py'])
        self.assertEqual(code, 25)
        self.assertIn('["security", "data"]', message)
        self.assertNotIn('other', message)
        self.assertEqual(self.evaluate(value, labels=['sensitive'])[0], 25)
        def observation(module):
            result = self.evaluate(value, labels=['schema-change'], paths=['auth/old.py'], module=module)
            self.assertEqual(result[0], 25)
            self.assertIn('["security", "data"]', result[1])
        observation(charter)
        with self.assertRaises(AssertionError):
            observation(mutated('json.dumps(classes)', 'json.dumps(classes[:1])'))

    def test_enforcement_removal_controls(self):
        controls = [
            ('if not approved:', 'if False:', policy(approved_queue=dict(label='approved')), {}, 2, 'charter-queue:'),
            ('if failed and label_exceptions:', 'if False:', policy(approved_queue=dict(label='approved'),
             exceptions=[dict({'class': 'security'}, labels=['sensitive'])]), dict(failed=True), 25, 'charter-unclassified:'),
            ('if path_match or label_match:', 'if label_match:', policy(exceptions=[dict({'class': 'files'}, paths=['auth/**'])]),
             dict(paths=['auth/key']), 25, 'files'),
            ('if path_match or label_match:', 'if path_match:', policy(exceptions=[dict({'class': 'security'}, labels=['sensitive'])]),
             dict(labels=['sensitive']), 25, 'security'),
            ('if lines:', 'if False:', policy(approved_queue=dict(label='approved'),
             exceptions=[dict({'class': 'security'}, paths=['auth/**'])]), dict(paths=['auth/key']), 25, 'security')]
        for old, new, value, args, code, text in controls:
            def observation(module):
                result = self.evaluate(value, module=module, **args)
                self.assertEqual(result[0], code)
                self.assertIn(text, result[1])
            with self.subTest(guard=old, replacement=new):
                observation(charter)
                with self.assertRaises(AssertionError):
                    observation(mutated(old, new))


class ProtocolTests(unittest.TestCase):
    def test_globs_are_root_relative_case_sensitive_and_component_aware(self):
        cases = [('auth/*', 'auth/key', True), ('auth/*', 'auth/deep/key', False),
                 ('auth/**', 'auth/deep/key', True), ('auth/**', 'other/auth/key', False),
                 ('**/key?.py', 'key1.py', True), ('**/key?.py', 'a/b/key1.py', True),
                 ('auth/**/key', 'auth/key', True), ('auth/**/key', 'auth/a/b/key', True),
                 ('auth/key?.py', 'auth/key.py', False), ('auth/key?.py', 'auth/key12.py', False),
                 ('auth/**', 'Auth/key', False), ('auth/**', 'auth/a\nb', True),
                 ('data/a.b', 'data/axb', False), ('data/a b', 'data/a b', True)]
        for pattern, path, expected in cases:
            with self.subTest(pattern=pattern, path=path):
                self.assertEqual(charter.path_matches(pattern, path), expected)
        controls = [("expression += '[^/]*'", "expression += '.*'", 'auth/*', 'auth/deep/key'),
                    ("expression += '[^/]'", "expression += '[^/]*'", 'auth/key?', 'auth/key12'),
                    ('re.fullmatch(expression, path, re.DOTALL)', 're.fullmatch(expression, path, re.DOTALL | re.IGNORECASE)',
                     'auth/**', 'Auth/key')]
        for old, new, pattern, path in controls:
            def observation(module):
                self.assertFalse(module.path_matches(pattern, path))
            observation(charter)
            with self.assertRaises(AssertionError):
                observation(mutated(old, new))

    def test_deleted_renamed_and_copied_paths_include_all_endpoints(self):
        raw = b'D\0auth/deleted\0R100\0auth/old\0public/new\0C100\0auth/source\0public/copy\0M95\0a\tb\n\0'
        self.assertEqual(charter.parse_changes(raw), ['auth/deleted', 'auth/old', 'public/new', 'auth/source', 'public/copy', 'a\tb\n'])
        self.assertEqual(charter.parse_changes(b''), [])

    def test_malformed_and_truncated_protocol_is_rejected(self):
        for raw in (b'M\0auth/key', b'R100\0auth/old\0', b'C100\0auth/source\0', b'X\0a\0',
                    b'R101\0a\0b\0', b'A\0\0', b'A\0/a\0', b'A\0../a\0', b'A\0a//b\0', b'A\0\xff\0'):
            with self.subTest(raw=raw), self.assertRaises((ValueError, UnicodeError)):
                charter.parse_changes(raw)
        def observation(module):
            with self.assertRaises(ValueError):
                module.parse_changes(b'M\0auth/key\0truncated')
        observation(charter)
        with self.assertRaises(AssertionError):
            observation(mutated("if not isinstance(raw, bytes) or (raw and not raw.endswith(b'\\0')):", 'if False:'))

    def test_snapshots_require_complete_labels_identity_and_requested_cycle(self):
        valid = snapshot(['approved'], dict(id=CYCLE))
        self.assertEqual(charter.parse_issue_snapshot(json.dumps(valid), ISSUE, True), dict(labels=['approved'], cycle_id=CYCLE))
        variants = []
        for edit in ('errors', 'identity', 'labels-missing', 'labels-null', 'label-name', 'labels-paginated',
                     'issues-paginated', 'no-issue', 'duplicate-issue', 'cycle-missing', 'cycle-invalid'):
            value = copy.deepcopy(valid); node = value['data']['issues']['nodes'][0]
            if edit == 'errors': value['errors'] = [dict(message='partial')]
            elif edit == 'identity': node['identifier'] = 'different'
            elif edit == 'labels-missing': node.pop('labels')
            elif edit == 'labels-null': node['labels']['nodes'] = None
            elif edit == 'label-name': node['labels']['nodes'] = [dict(name=None)]
            elif edit == 'labels-paginated': node['labels']['pageInfo']['hasNextPage'] = True
            elif edit == 'issues-paginated': value['data']['issues']['pageInfo']['hasNextPage'] = True
            elif edit == 'no-issue': value['data']['issues']['nodes'] = []
            elif edit == 'duplicate-issue': value['data']['issues']['nodes'] *= 2
            elif edit == 'cycle-missing': node.pop('cycle')
            elif edit == 'cycle-invalid': node['cycle'] = dict(id='invalid')
            variants.append((edit, value))
        for edit, value in variants:
            with self.subTest(edit=edit), self.assertRaises((ValueError, TypeError, KeyError)):
                charter.parse_issue_snapshot(json.dumps(value), ISSUE, True)
        for old, new, value in [("if not isinstance(value, dict) or value.get('errors') not in (None, []):", 'if False:', variants[0][1]),
                                ("labels['pageInfo']['hasNextPage'] is not False", 'False', variants[5][1])]:
            def observation(module):
                with self.assertRaises(ValueError):
                    module.parse_issue_snapshot(json.dumps(value), ISSUE, True)
            observation(charter)
            with self.assertRaises(AssertionError):
                observation(mutated(old, new))

    def test_transport_calls_existing_helper_once_and_keeps_failures_explicit(self):
        answer = json.dumps(snapshot(['approved'], dict(id=CYCLE))).encode()
        with patch.object(charter.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, answer, b'')) as run:
            with patch.dict(os.environ, BASH_ENV='untrusted', ENV='untrusted'):
                result = charter.issue_snapshot(Path('.'), ISSUE, True)
            self.assertEqual(result['cycle_id'], CYCLE)
            self.assertEqual(run.call_count, 1)
            args, options = run.call_args
            self.assertIn('linear_raw "$2" "$3"', args[0][4])
            self.assertIn('bureau_load_env --export "$BUREAU_ENV_FILE"', args[0][4])
            self.assertIn('bureau_secret_copy API_KEY LINEAR_API_KEY', args[0][4])
            self.assertEqual(options['env']['_BUREAU_LINEAR_SINGLE_ATTEMPT'], '1')
            self.assertNotIn('BASH_ENV', options['env'])
            self.assertNotIn('ENV', options['env'])
        for answer in (b'{truncated', json.dumps(snapshot()).encode()):
            with patch.object(charter.subprocess, 'run', return_value=subprocess.CompletedProcess([], 1, answer, b'')):
                with self.assertRaises(ValueError):
                    charter.issue_snapshot(Path('.'), ISSUE, False)


if __name__ == '__main__':
    unittest.main()
