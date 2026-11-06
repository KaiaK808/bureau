#!/usr/bin/env python3
"""Real admission/response parsers with mocked service and Git transports."""
from contextlib import redirect_stderr, redirect_stdout
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch


sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]


class Provenance(unittest.TestCase):
    def setUp(self):
        self.scripts = Path(os.environ.get('BUREAU_OVERLAP_TEST_SCRIPTS', ROOT / 'templates/scripts'))
        spec = importlib.util.spec_from_file_location('admission_provenance', self.scripts / 'bureau-admission.py')
        self.helper = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.helper)
        temporary = tempfile.TemporaryDirectory(prefix='bureau-provenance-test-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.candidate = self.issue(1, 'repo-project', 'feature/candidate')
        self.outside = self.issue(2, 'other-project', 'feature/shared-change')
        self.config = dict(agents=dict(file_overlap_gate=True), linear=dict(
            projects=['repo-project'], teams=[dict(key='TEST', states=dict(spec_review='review'))]))
        self.config_path = self.root / '.bureau.json'
        self.config_path.write_text(json.dumps(self.config))
        self.pr = dict(number=1, body='', state='open',
                       head=dict(ref=self.outside['branch'], sha='b' * 40, repo=dict(id=1)),
                       base=dict(sha='a' * 40, repo=dict(id=1)))
        self.listed = [self.pr]
        self.add_outside = True
        self.changed_path = 'pkg/a.py'
        self.snapshots = 0
        self.comments = []
        self.pr_diffs = 0
        self.output = io.StringIO()
        for context in (
            patch.dict(os.environ, {}, clear=True),
            patch.object(self.helper, 'linear', side_effect=self.linear),
            patch.object(self.helper, 'gh', side_effect=self.gh),
            patch.object(self.helper, 'git', side_effect=self.git),
            patch.object(self.helper.r, 'shell', side_effect=self.shell),
            patch.object(self.helper.r, 'common_for', return_value=self.root / 'common'),
            patch.object(self.helper.r, 'root_for', return_value=self.root),
            patch.object(self.helper.r, 'config_for', return_value=self.config_path),
            patch.object(self.helper.r, 'ticket_checkpoint'),
            patch.object(self.helper.r.e, 'observe'),
            redirect_stdout(self.output), redirect_stderr(self.output),
        ):
            context.__enter__()
            self.addCleanup(context.__exit__, None, None, None)
        self.admission = self.helper.Admission(self.root, self.config)

    @staticmethod
    def issue(number, project, branch):
        identifier = 'TEST-' + str(number)
        return dict(id=identifier, identifier=identifier, project=dict(id=project), branch=branch,
                    state=dict(id='review', type='started'), labels=dict(nodes=[]))

    @staticmethod
    def connection(rows):
        return dict(nodes=rows, pageInfo=dict(hasNextPage=False, endCursor=None))

    def linear(self, repo, query, variables):
        if query == self.helper.ISSUES:
            self.snapshots += 1
            rows = [self.candidate]
            if self.add_outside and self.snapshots >= 2:
                rows.append(self.outside)
            return dict(issues=self.connection(rows))
        if query == self.helper.COMMENTS:
            self.comments.append(variables['id'])
            issue = next(row for row in (self.candidate, self.outside) if row['id'] == variables['id'])
            marker = dict(body='<!-- bureau-branch: ' + issue['branch'] + ' -->', createdAt='2026-01-01')
            return dict(issue=dict(comments=self.connection([marker])))
        if query == self.helper.LABELS:
            return dict(issue=dict(labels=self.connection([])))
        raise AssertionError('unexpected Linear query: ' + query)

    def gh(self, repo, endpoint):
        if '?state=open' in endpoint:
            return [self.listed]
        self.assertEqual(endpoint.rsplit('/', 1)[-1], str(self.pr['number']))
        self.pr_diffs += 1
        return [self.pr]

    def git(self, repo, *args):
        if args[0] in ('check-ref-format', 'cat-file'):
            return b''
        if args[0] in ('rev-parse', 'log', 'merge-base'):
            return ('a' * 40 + '\n').encode()
        if args[0] == 'ls-tree':
            return b'specs/feature/candidate/tasks.md\0pkg/a.py\0'
        if args[0] == 'show':
            return b'- [ ] T001 Update `pkg/a.py`\n'
        if args[0] == 'diff':
            return b'M\0' + self.changed_path.encode() + b'\0'
        raise AssertionError('unexpected Git arguments: ' + repr(args))

    @staticmethod
    def shell(repo, *args):
        if args[0] == 'bureau_ticket_state_class':
            return 'active'
        if args[:2] == ('git', 'fetch'):
            return ''
        raise AssertionError('unexpected shell arguments: ' + repr(args))

    def main(self):
        with patch.object(sys, 'argv', ['bureau-admission.py', '--repo', str(self.root), 'admit',
                                      self.candidate['identifier'], self.candidate['branch']]):
            return self.helper.main()

    def reservations(self):
        return self.helper.r.read(self.admission.path, {}).get('reservations', {})

    def test_final_snapshot_loses_canonical_completeness(self):
        with self.assertRaises(self.helper.SnapshotMoved) as moved:
            self.admission.admit(self.candidate['identifier'], self.candidate['branch'])
        self.assertEqual(moved.exception.code, 27)
        self.assertFalse(self.admission.path.exists())
        self.assertEqual(self.snapshots, 2)
        self.assertEqual(self.comments, [self.candidate['id']])
        with self.assertRaises(self.helper.Stop) as stopped:
            self.admission.admit(self.candidate['identifier'], self.candidate['branch'])
        self.assertEqual(stopped.exception.code, 29)
        self.assertIn('PR #1', str(stopped.exception))
        self.assertNotIn(self.candidate['identifier'], self.reservations())
        self.assertEqual(self.comments, [self.candidate['id']] * 2)
        self.assertEqual(self.pr_diffs, 1)

    def test_shepherd_retries_before_conflicting_reservation(self):
        self.assertEqual(self.main(), 29, self.output.getvalue())
        self.assertEqual(self.snapshots, 4)
        self.assertNotIn(self.candidate['identifier'], self.reservations())
        self.assertEqual(self.comments, [self.candidate['id']] * 2)

    def test_new_outside_issue_without_skipped_pr_does_not_retry(self):
        self.listed = []
        self.assertEqual(self.main(), 0, self.output.getvalue())
        self.assertEqual(self.snapshots, 2)
        self.assertIn(self.candidate['identifier'], self.reservations())

    def test_new_outside_issue_with_disjoint_pr_passes_after_retry(self):
        self.changed_path = 'pkg/b.py'
        self.assertEqual(self.main(), 0, self.output.getvalue())
        self.assertEqual(self.snapshots, 4)
        self.assertIn(self.candidate['identifier'], self.reservations())
        self.assertEqual(self.pr_diffs, 1)

    def test_unchanged_complete_map_keeps_pr_provenance_filter(self):
        self.add_outside = False
        self.assertEqual(self.main(), 0, self.output.getvalue())
        self.assertEqual(self.snapshots, 2)
        self.assertEqual(self.pr_diffs, 0)

    def test_stale_completeness_negative_control(self):
        scripts = self.root / 'control-scripts'
        shutil.copytree(self.scripts, scripts)
        path = scripts / 'bureau-admission.py'
        source = path.read_text()
        original = 'latest_canonical_complete = canonical.keys() == latest.keys()'
        self.assertEqual(source.count(original), 1)

        def regression():
            with patch.dict(os.environ, BUREAU_OVERLAP_TEST_SCRIPTS=str(scripts)):
                result = unittest.TestResult()
                Provenance('test_final_snapshot_loses_canonical_completeness').run(result)
                return result

        baseline = regression()
        self.assertTrue(baseline.wasSuccessful(), baseline.errors + baseline.failures)
        path.write_text(source.replace(original, 'latest_canonical_complete = canonical_complete'))
        stale = regression()
        self.assertFalse(stale.errors, stale.errors)
        self.assertEqual(len(stale.failures), 1, stale.failures)
        self.assertIn('SnapshotMoved not raised', stale.failures[0][1])


if __name__ == '__main__':
    unittest.main(verbosity=2)
