#!/usr/bin/env python3
"""Strict policy validation and doctor diagnostics, without transports."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


def load(path):
    spec = importlib.util.spec_from_file_location(path.stem, path)
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


reader = load(ROOT / 'templates/scripts/bureau-merge-review.py')
doctor = load(Path(sys.argv.pop(1)) if len(sys.argv) > 1 and sys.argv[1].endswith('.py')
              else ROOT / 'templates/scripts/bureau-doctor.py')


class PolicyTests(unittest.TestCase):
    def cli(self, *args):
        with patch.object(reader.Reader, 'api', side_effect=AssertionError('CLI validation must not read GitHub')):
            from contextlib import redirect_stdout, redirect_stderr
            from io import StringIO
            out, err = StringIO(), StringIO()
            with redirect_stdout(out), redirect_stderr(err), patch.object(sys, 'argv', ['bureau-merge-review.py', *args]):
                code = reader.main()
            return code, out.getvalue(), err.getvalue()

    def test_cli_help_without_reads(self):
        for flag in ('--help', '-h'):
            with self.subTest(flag=flag):
                code, out, err = self.cli(flag)
                self.assertEqual(code, 0)
                self.assertIn('usage:', out)
                self.assertEqual(err, '')

    def test_cli_invalid_arguments_fail_without_result_or_reads(self):
        for args in ((), ('policy',), ('unknown', 'owner/repo'), ('policy', 'owner/repo', 'extra'),
                     ('policy', 'bad-repo'), ('threads', 'owner/repo', 'x'), ('threads', 'owner/repo', '0'),
                     ('threads', 'owner/repo', '2147483648'), ('threads', 'owner/repo', '9' * 5000),
                     ('statuses', 'owner/repo', 'bad-sha'), ('review', 'owner/repo', '1', 'TEAM-1'),
                     ('review', 'owner/repo', '1', 'TEAM-1', 'broken-json'),
                     ('review', 'owner/repo', '1', 'TEAM-1', '[' * 2000 + ']' * 2000),
                     ('review', 'owner/repo', '1', '', '{}'),
                     ('review', 'owner/repo', '1', 'TEAM-1\n', '{}'),
                     ('review', 'owner/repo', '1', 'TEAM-1', '[]'),
                     ('review', 'owner/repo', '1', 'TEAM-1', '{"kind":"ok"}'),
                     ('review', 'owner/repo', '1', 'TEAM-1', '{"kind":"unknown"}')):
            with self.subTest(args=args):
                code, out, err = self.cli(*args)
                self.assertNotEqual(code, 0)
                self.assertEqual(out, '')
                self.assertEqual(len(err.splitlines()), 1)
                self.assertNotIn('Traceback', err)

    def test_api_rejects_partial_and_malformed_error_envelopes(self):
        for errors in ([{'message': 'partial response'}], {}, '', False, 0):
            proc = types.SimpleNamespace(returncode=0, stdout=json.dumps(dict(data={}, errors=errors)))
            with self.subTest(errors=errors), patch.object(reader.subprocess, 'run', return_value=proc):
                with self.assertRaises(reader.ReadError):
                    reader.Reader('owner/repo').api('graphql')

    def test_fake_honors_gh_field_method_switch(self):
        with tempfile.TemporaryDirectory() as scratch:
            env = dict(os.environ, STUB_DIR=scratch, GIT_CEILING_DIRECTORIES=scratch)
            env.pop('PR2_GH', None)
            args = [sys.executable, '-I', '-B', str(ROOT / 'tests/lib/merge-review-fake.py'),
                    'api', 'repos/test-owner/test-repo/contents/.bureau.json', '-f', 'ref=' + 'c' * 40]
            self.assertEqual(subprocess.run(args, env=env, capture_output=True).returncode, 1)
            self.assertEqual(subprocess.run(args + ['--method', 'GET'], env=env, capture_output=True).returncode, 0)

    def test_strict_entries(self):
        invalid = [None, 'user:reviewer', {}, [None], [1], [True], ['reviewer'], ['User:reviewer'],
                   ['app:reviewer[bot]'], ['app:app/reviewer'], ['user:reviewer '], ['user:-reviewer'],
                   ['user:reviewer--bot'], ['user:réviewer'], ['user:' + 'a' * 40],
                   ['user:reviewer', 'user:REVIEWER'], ['app:a' + str(i) for i in range(21)]]
        for value in invalid:
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, 'agents.merge_publishers'):
                reader.merge_publishers(dict(agents=dict(merge_publishers=value)))
        self.assertEqual(reader.merge_publishers(dict(agents=dict(merge_publishers=['user:Reviewer', 'app:reviewer']))),
                         ['user:reviewer', 'app:reviewer'])
        self.assertEqual(len(reader.merge_publishers(dict(agents=dict(merge_publishers=['app:a' + str(i) for i in range(20)])))), 20)

    def test_doctor_reports_missing_and_invalid(self):
        config = dict(linear=dict(teams=[dict(id='team', key='TEAM', states=dict(merge='merge'))]), agents={}, repo={})
        for value in (None, ['untyped']):
            if value is not None:
                config['agents']['merge_publishers'] = value
            self.assertTrue(any('agents.merge_publishers' in e for e in doctor.validate(config)))
        config['agents']['merge_publishers'] = ['app:bureau-reviewer']
        self.assertFalse(any('agents.merge_publishers' in e for e in doctor.validate(config)))

    def test_identity_and_discussion_negative_controls(self):
        source = (ROOT / 'templates/scripts/bureau-merge-review.py').read_text()
        good = dict(id='review', body='## Code Review v2 — TEAM-1\n\n**Verdict**: APPROVE',
                    createdAt='2026-01-01T00:00:00Z', lastEditedAt=None, isMinimized=False,
                    author=dict(__typename='User', login='publisher'))
        discussion = dict(good, id='discussion', body='Unrelated discussion', author=None)
        unknown = dict(good, id='unknown', author=None)

        def outcome(text, nodes):
            module = types.ModuleType('private_mutant')
            exec(compile(text, '<private mutant>', 'exec'), module.__dict__)
            instance = module.Reader('owner/repo')
            instance.connection = lambda *args: nodes
            def api(route, fields):
                if route != 'graphql':
                    raise module.ReadError('HTTP 403: /user unsupported')
                return dict(data=dict(node=next(v for v in nodes if v['id'] == fields['id'])))
            instance.api = api
            try:
                return instance.review(1, 'TEAM-1', dict(kind='ok', entries=['user:publisher']))['kind']
            except module.ReadError:
                return 'read'

        self.assertEqual(outcome(source, [good, discussion]), 'ok')
        self.assertEqual(outcome(source, [good, unknown]), 'read')
        controls = (
            ('runtime identity', "        if policy['kind'] != 'ok':", "        self.api('user', {})\n        if policy['kind'] != 'ok':", [good], 'ok'),
            ('all discussion authors', "            body = required(node, 'body', str, empty=True)", "            account(node.get('author'))\n            body = required(node, 'body', str, empty=True)", [good, discussion], 'ok'),
            ('discard unknown candidate', "            actor = account(node.get('author'))", "            if node.get('author') is None:\n                continue\n            actor = account(node.get('author'))", [good, unknown], 'read'),
        )
        for name, before, after, nodes, expected in controls:
            with self.subTest(control=name):
                self.assertEqual(source.count(before), 1)
                with self.assertRaises(AssertionError):
                    self.assertEqual(outcome(source.replace(before, after), nodes), expected)
                print('PASS negative control rejected: ' + name)


if __name__ == '__main__':
    unittest.main()
