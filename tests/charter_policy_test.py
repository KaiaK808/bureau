#!/usr/bin/env python3
"""Pure-Python charter schema checks; no processes, repositories or services."""
import copy
import importlib.util
from pathlib import Path
import types
import unittest

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / 'templates/scripts/bureau-charter.py'
spec = importlib.util.spec_from_file_location('charter', SOURCE)
charter = importlib.util.module_from_spec(spec)
spec.loader.exec_module(charter)


def config(**policy):
    return dict(agents=dict(merge_charter=policy))


class PolicyTests(unittest.TestCase):
    def test_defaults_and_valid_declarations(self):
        self.assertIsNone(charter.policy_from_config({}))
        self.assertEqual(charter.policy_from_config(config()), charter.DEFAULTS)
        for queue in (None, dict(label='approved-for-merge'), dict(cycle_id='12345678-1234-1234-1234-123456789abc')):
            value = config(enabled=True, approved_queue=queue)
            value['agents']['merge_charter']['exceptions'] = [dict(
                {'class': 'security'}, paths=['auth/**', 'src/*/A?.py']), {'class': 'data', 'labels': ['schema-change']}]
            value['agents']['merge_charter'].update(
                suite=dict(id='full', command='bash scripts/check.sh', definition_files=['scripts/check.sh']),
                red_main=dict(workflows=['.github/workflows/ci.yml']), report=True)
            parsed = charter.policy_from_config(value)
            self.assertEqual(parsed['red_main']['poll_seconds'], 60)
            self.assertEqual(parsed['red_main']['timeout_seconds'], 3600)
            self.assertEqual(parsed['approved_queue'], queue)

    def test_invalid_shapes_including_disabled(self):
        cases = [None, False, [], 'enabled', {'unknown': False}]
        cases += [dict(enabled=v) for v in (None, 0, 1, 'false', [], {})]
        cases += [dict(report=v) for v in (None, 0, 'true', [])]
        cases += [dict(approved_queue=v) for v in (True, [], '', {}, {'label': 'yes', 'cycle_id': 'x'},
                  {'label': ''}, {'label': ' '}, {'label': 1}, {'label': []}, {'label': 'yes', 'other': 1},
                  {'cycle_id': ''}, {'cycle_id': 'not-a-uuid'})]
        cases += [dict(exceptions=v) for v in (None, True, {}, '', [None], [True], [[]],
                  [{}], [{'class': ''}], [{'class': ' '}], [{'class': 1}], [{'class': 'x'}],
                  [{'class': 'x', 'paths': [], 'labels': []}], [{'class': 'x', 'paths': None}],
                  [{'class': 'x', 'paths': 'src/**'}], [{'class': 'x', 'labels': 'sensitive'}],
                  [{'class': 'x', 'labels': [True]}], [{'class': 'x', 'labels': ['']}],
                  [{'class': 'x', 'paths': ['src/**'], 'extra': False}],
                  [{'class': 'x', 'labels': ['a']}, {'class': 'x', 'labels': ['b']}])]
        cases += [dict(suite=v) for v in (False, [], '', {}, {'id': 'full'},
                  {'id': '', 'command': 'true', 'definition_files': ['test.sh']},
                  {'id': 'full', 'command': '', 'definition_files': ['test.sh']},
                  {'id': 'full', 'command': ['true'], 'definition_files': ['test.sh']},
                  {'id': 'full', 'command': 'true', 'definition_files': []},
                  {'id': 'full', 'command': 'true', 'definition_files': ['*.sh']},
                  {'id': 'full', 'command': 'true', 'definition_files': ['test.sh'], 'extra': 1})]
        cases += [dict(red_main=v) for v in (False, [], '', {}, {'workflows': []}, {'workflows': 'ci.yml'},
                  {'workflows': ['ci.yml']}, {'workflows': ['.github/workflows/*.yml']},
                  {'workflows': ['.github/workflows/sub/ci.yml']}, {'workflows': ['.github/workflows/ci.txt']},
                  {'workflows': ['.github/workflows/ci.yml'], 'poll_seconds': 0},
                  {'workflows': ['.github/workflows/ci.yml'], 'poll_seconds': True},
                  {'workflows': ['.github/workflows/ci.yml'], 'timeout_seconds': '60'},
                  {'workflows': ['.github/workflows/ci.yml'], 'timeout_seconds': 1},
                  {'workflows': ['.github/workflows/ci.yml'], 'extra': False})]
        cases += [dict(report=True), dict(report=True, suite=dict(id='full', command='true', definition_files=['test.sh']))]
        for raw in cases:
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                charter.policy_from_config(dict(agents=dict(merge_charter=raw)))
        for value in (None, [], {'agents': []}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                charter.policy_from_config(value)

    def test_paths_are_root_relative_and_only_declared_glob_syntax(self):
        invalid = ['', ' ', '/auth/**', '//host/a', 'C:/auth/*', 'C:auth', '\\auth',
                   '~/auth', '../auth', 'auth/../data', 'auth/./data', 'auth//data', 'auth/',
                   '!auth/**', ':(exclude)auth/**', 'auth/!private', 'auth/[ab]', 'auth/{a,b}', 'auth\\**', 'auth/\nsecret']
        for path in invalid:
            with self.subTest(path=path), self.assertRaises(ValueError):
                charter.policy_from_config(config(exceptions=[{'class': 'security', 'paths': [path]}]))
        for path in ('auth/**', '**/tokens?.json', 'src/*/A.py', 'Config.json', 'docs/file name.md'):
            parsed = charter.policy_from_config(config(exceptions=[{'class': 'files', 'paths': [path]}]))
            self.assertEqual(parsed['exceptions'][0]['paths'][0], path)

    def test_duplicate_json_keys_and_selectors(self):
        for raw in ('{"agents":{},"agents":{}}', '{"x":NaN}', '{"x":Infinity}'):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                charter.read_json(raw)
        for entry in ({'class': 'x', 'paths': ['a', 'a']}, {'class': 'x', 'labels': ['a', 'a']}):
            with self.assertRaises(ValueError):
                charter.policy_from_config(config(exceptions=[entry]))

    def test_digest_is_of_normalized_policy(self):
        first = charter.policy_from_config(config())
        second = charter.policy_from_config(config(**copy.deepcopy(charter.DEFAULTS)))
        self.assertEqual(charter.digest(first), charter.digest(second))
        second['enabled'] = True
        self.assertNotEqual(charter.digest(first), charter.digest(second))

    def test_negative_controls_remove_real_schema_guards(self):
        source = SOURCE.read_text()
        controls = [
            ("if type(policy[key]) is not bool:", 'if False:', config(enabled='false')),
            ('if len(queue) != 1:', 'if False:', config(approved_queue={'label': 'yes', 'cycle_id': '12345678-1234-1234-1234-123456789abc'})),
            ('if name in classes:', 'if False:', config(exceptions=[{'class': 'x', 'labels': ['a']}, {'class': 'x', 'labels': ['b']}])),
            ('if not paths and not labels:', 'if False:', config(exceptions=[{'class': 'x'}])),
            ("relative_path(v, w, glob=True)", 'text(v, w)', config(exceptions=[{'class': 'x', 'paths': ['../auth']}]))]
        for old, new, value in controls:
            def observation(module):
                with self.assertRaises(ValueError):
                    module.policy_from_config(value)
            with self.subTest(guard=old):
                observation(charter)  # Unchanged baseline must pass first.
                self.assertEqual(source.count(old), 1)
                mutant = types.ModuleType('charter_mutant')
                exec(compile(source.replace(old, new), '<charter mutation>', 'exec'), mutant.__dict__)
                with self.assertRaises(AssertionError):
                    observation(mutant)


if __name__ == '__main__':
    unittest.main()
