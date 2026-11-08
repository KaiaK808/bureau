"""Merged dispositions must belong to the current successful specialist calls."""
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
import uuid

import review_merge_test as fixture


class DispositionTests(unittest.TestCase):
    def setUp(self):
        self.f = fixture.MergeTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.e = fixture.load('evidence')
        self.store = self.e.Store(self.f.repo / '.git/bureau')
        self.run = uuid.uuid4().hex
        self.store.begin_run(self.run, 'T-1', self.f.repo, self.f.head)
        self.attempt = self.store.begin_attempt(self.run, 'T-1', 'code_review', 'stage', self.f.repo, self.f.head)

    def pair(self, name, path):
        directory = self.f.root / name
        directory.mkdir()
        reports = {role: fixture.specialist(role) for role in self.f.m.ROLES}
        reports['correctness'] = fixture.specialist('correctness', [fixture.finding('Deferred detail', 'MINOR', file=path)])
        for role, report in reports.items():
            (directory / (role + '.txt')).write_text(json.dumps(report))
        self.merge(directory)
        return directory, reports

    def merge(self, directory):
        completed = subprocess.run([sys.executable, str(fixture.SCRIPTS / 'bureau-review.py'), 'merge',
                                    '--directory', str(directory), '--base', self.f.base, '--head', self.f.head],
                                   cwd=self.f.repo, capture_output=True, text=True, timeout=10)
        self.assertEqual(completed.returncode, 0, completed.stderr)

    def supplemented_pair(self):
        directory, reports = self.pair('current-round', 'independent.py')
        late = {role: fixture.specialist(role) for role in self.f.m.ROLES}
        late['security'] = fixture.specialist('security', [fixture.finding('Additional deferred detail', 'MINOR', file='late.py')])
        impeccable = fixture.load('impeccable')
        for role, report in reports.items():
            (directory / (role + '.independent.txt')).write_text(json.dumps(report))
            (directory / (role + '.late.txt')).write_text(json.dumps(late[role]))
            (directory / (role + '.txt')).write_text(json.dumps(impeccable.supplement(directory, role, None)))
        self.merge(directory)
        return directory, {**reports, **{role + '_late': report for role, report in late.items()}}

    def receipts(self, reports, inputs=False):
        paths = []
        for role, report in reports.items():
            original = self.f.root / ('provider-' + role)
            original.mkdir()
            (original / 'final.txt').write_text(json.dumps(report))
            paths.append(self.store.put_receipt(dict(
                run_id=self.run, stage_attempt_id=self.attempt, provider_attempt_id=uuid.uuid4().hex,
                issue='T-1', stage='code_review', phase=role, provider='codex', provider_exit=0,
                normalized_exit=0, outcome='complete', start_head=self.f.head, end_head=self.f.head,
                provider_logs=str(original), structured_result=None, insights_result=report,
                review_inputs=dict(base=self.f.base, head=self.f.head, validation_head=self.f.head) if inputs else None,
                usage=dict(input_tokens=1, output_tokens=1), total_cost_usd=0)))
        return {path: path.read_bytes() for path in paths}

    def collect(self, directory, verdict='APPROVE'):
        completed = subprocess.run([sys.executable, str(fixture.SCRIPTS / 'bureau-evidence.py'),
                                    '--repo', str(self.f.repo), '--round', '1', '--min-severity', 'BUG',
                                    '--provider-verdict', verdict, '--final-verdict', verdict,
                                    '--merge-metadata', str(directory / 'merge-metadata.json'),
                                    '--merge-result', str(directory / 'merged.json')],
                                   env={**os.environ, 'BUREAU_RUN_ID': self.run, 'BUREAU_STAGE_ATTEMPT_ID': self.attempt},
                                   capture_output=True, text=True, timeout=10)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        return completed

    def test_stale_pair_cannot_replace_current_disposition(self):
        stale, _ = self.pair('earlier-round', 'obsolete.py')
        current, reports = self.pair('current-round', 'current.py')
        receipts = self.receipts(reports)
        self.collect(current)
        ledger = self.store.root / 'review-dispositions.jsonl'
        self.assertTrue(ledger.exists(), 'current merge must collect its disposition')
        before = ledger.read_bytes()
        rows = [json.loads(line) for line in before.splitlines()]
        self.assertEqual([(r['disposition'], r['path']) for r in rows], [('deferred', 'current.py')])
        self.collect(stale)  # Same run, stage and round; only the supplied pair changes.
        self.assertEqual({path: path.read_bytes() for path in receipts}, receipts)
        self.assertEqual([json.loads(line)['path'] for line in ledger.read_bytes().splitlines()], ['current.py'],
                         'stale pair replaced the current disposition')
        self.assertEqual(ledger.read_bytes(), before, 'stale pair replaced the current disposition')

    def test_forged_pair_with_current_input_digests_is_rejected(self):
        stale, _ = self.pair('forged-round', 'obsolete.py')
        current, reports = self.pair('current-round', 'current.py')
        receipts = self.receipts(reports)
        self.collect(current)
        ledger = self.store.root / 'review-dispositions.jsonl'
        before = ledger.read_bytes()
        metadata = json.loads((stale / 'merge-metadata.json').read_text())
        metadata['specialist_sha256'] = json.loads((current / 'merge-metadata.json').read_text())['specialist_sha256']
        (stale / 'merge-metadata.json').write_text(json.dumps(metadata))
        rejected = self.collect(stale)
        self.assertIn('optional evidence unavailable or invalid', rejected.stderr)
        self.assertEqual(ledger.read_bytes(), before, 'copied current digests authenticated obsolete findings')
        self.assertEqual({path: path.read_bytes() for path in receipts}, receipts)

    def test_missing_extra_and_different_input_digests_preserve_current_rows(self):
        current, reports = self.pair('current-round', 'current.py')
        self.receipts(reports)
        self.collect(current)
        ledger = self.store.root / 'review-dispositions.jsonl'
        before = ledger.read_bytes()
        metadata_path = current / 'merge-metadata.json'
        metadata = json.loads(metadata_path.read_text())
        expected = metadata['specialist_sha256']
        for changed in (None, {}, {k: v for k, v in expected.items() if k != 'performance'},
                        {**expected, 'verdict': 'f' * 64}, {**expected, 'correctness': 'f' * 64},
                        {**expected, 'security': expected['performance'], 'performance': expected['security']}):
            with self.subTest(digests=changed):
                candidate = dict(metadata)
                if changed is None: candidate.pop('specialist_sha256')
                else: candidate['specialist_sha256'] = changed
                metadata_path.write_text(json.dumps(candidate))
                rejected = self.collect(current)
                self.assertIn('optional evidence unavailable or invalid', rejected.stderr)
                self.assertEqual(ledger.read_bytes(), before)

    def test_receipt_digests_and_originals_are_independent_of_caller_metadata(self):
        current, reports = self.pair('current-round', 'current.py')
        receipts = self.receipts(reports)
        self.collect(current)
        ledger = self.store.root / 'review-dispositions.jsonl'
        before = ledger.read_bytes()
        path = next(iter(receipts))
        receipt = json.loads(receipts[path])
        original = Path(receipt['provider_logs']) / 'final.txt'
        original_bytes = original.read_bytes()
        for fault in ('legacy-receipt', 'different-digest', 'tampered-original', 'missing-original'):
            with self.subTest(fault=fault):
                changed = dict(receipt)
                if fault == 'legacy-receipt': changed.pop('review_report_sha256')
                if fault == 'different-digest': changed['review_report_sha256'] = 'f' * 64
                if fault == 'tampered-original': original.write_text(json.dumps(fixture.specialist(receipt['phase'])))
                if fault == 'missing-original': original.unlink()
                path.write_text(json.dumps(changed))
                rejected = self.collect(current)
                self.assertIn('optional evidence unavailable or invalid', rejected.stderr)
                self.assertEqual(ledger.read_bytes(), before)
                path.write_bytes(receipts[path]); original.write_bytes(original_bytes)

    def test_matching_inputs_collect_without_a_merger_receipt_or_usage(self):
        current, reports = self.pair('current-round', 'current.py')
        receipts = self.receipts(reports)
        metadata = json.loads((current / 'merge-metadata.json').read_text())
        self.assertEqual(metadata['specialist_sha256'], {role: self.e.digest(report) for role, report in reports.items()})
        completed = self.collect(current)
        self.assertEqual(completed.stderr, '')
        record = self.store._manifest(self.run)['attempts'][0]['review_round']
        self.assertEqual(record['calls'], 3)
        self.assertEqual(record['usage']['input_tokens'], 3)
        self.assertEqual(record['usage']['output_tokens'], 3)
        self.assertEqual(len(record['provider_attempt_ids']), 3)
        for value in receipts.values():
            receipt = json.loads(value)
            self.assertIsNone(receipt['structured_result'])
            self.assertIsNone(receipt['result_sha256'])
            self.assertEqual(receipt['review_report_sha256'], metadata['specialist_sha256'][receipt['phase']])
        rows = [json.loads(line) for line in (self.store.root / 'review-dispositions.jsonl').read_text().splitlines()]
        self.assertEqual([(r['disposition'], r['path'], r['provider_attempt_id']) for r in rows],
                         [('deferred', 'current.py', None)])
        raw = b''.join(p.read_bytes() for p in self.store.root.rglob('*') if p.is_file())
        self.assertNotIn(b'Deferred detail', raw)

    def test_unhashable_optional_report_keeps_success_receipt_for_fallback(self):
        path = self.store.put_receipt(dict(
            run_id=self.run, stage_attempt_id=self.attempt, provider_attempt_id=uuid.uuid4().hex,
            issue='T-1', stage='code_review', phase='correctness', provider='codex', provider_exit=0,
            normalized_exit=0, outcome='complete', structured_result=None,
            insights_result=dict(summary=float('nan'))))
        receipt = json.loads(path.read_text())
        self.assertEqual((receipt['provider_exit'], receipt['normalized_exit'], receipt['outcome']), (0, 0, 'complete'))
        self.assertNotIn('review_report_sha256', receipt)

    def test_late_reports_collect_dispositions_and_publish_complete_context(self):
        current, reports = self.supplemented_pair()
        receipts = self.receipts(reports, inputs=True)
        completed = self.collect(current, 'REQUEST_CHANGES')
        self.assertEqual(completed.stderr, '')
        record = self.store._manifest(self.run)['attempts'][0]['review_round']
        self.assertEqual(record['calls'], 6); self.assertEqual(record['usage']['input_tokens'], 6)
        self.assertEqual({json.loads(value)['phase'] for value in receipts.values()}, set(reports))
        for value in receipts.values():
            receipt = json.loads(value)
            self.assertEqual(receipt['review_report_sha256'], self.e.digest(reports[receipt['phase']]))
        rows = [json.loads(line) for line in (self.store.root / 'review-dispositions.jsonl').read_text().splitlines()]
        self.assertEqual({(r['disposition'], r['path']) for r in rows}, {('deferred', 'independent.py'), ('deferred', 'late.py')})
        self.store.end_attempt(self.run, self.attempt, 0, self.f.head)
        context = fixture.load('review-context')
        result, floor, head = context.current_result(self.f.repo, self.store._manifest(self.run)['issue'],
                                                   self.run, self.attempt, 1, 'REQUEST_CHANGES')
        self.assertEqual(result, json.loads((current / 'merged.json').read_text()))
        self.assertEqual((floor, head), ('BUG', self.f.head))

    def test_late_and_independent_receipts_reject_tampering_and_missing_reports(self):
        current, reports = self.supplemented_pair()
        receipts = self.receipts(reports, inputs=True)
        self.assertEqual(self.collect(current).stderr, '')
        ledger = self.store.root / 'review-dispositions.jsonl'
        retained = next(self.f.root.glob('provider-*/merged-review.json'))
        before = ledger.read_bytes(), retained.read_bytes()
        for phase in ('correctness', 'security_late'):
            path = next(p for p, value in receipts.items() if json.loads(value)['phase'] == phase)
            receipt = json.loads(receipts[path])
            original = Path(receipt['provider_logs']) / 'final.txt'; original_bytes = original.read_bytes()
            for fault in ('missing-digest', 'different-digest', 'tampered-original', 'missing-original', 'failed-call', 'foreign-inputs'):
                with self.subTest(phase=phase, fault=fault):
                    changed = json.loads(receipts[path])
                    if fault == 'missing-digest': changed.pop('review_report_sha256')
                    if fault == 'different-digest': changed['review_report_sha256'] = 'f' * 64
                    if fault == 'tampered-original': original.write_text(json.dumps(fixture.specialist(reports[phase]['specialist'])))
                    if fault == 'missing-original': original.unlink()
                    if fault == 'failed-call': changed['normalized_exit'] = 22
                    if fault == 'foreign-inputs': changed['review_inputs']['head'] = 'f' * 40
                    path.write_text(json.dumps(changed))
                    rejected = self.collect(current)
                    self.assertIn('optional evidence unavailable or invalid', rejected.stderr)
                    self.assertEqual((ledger.read_bytes(), retained.read_bytes()), before)
                    path.write_bytes(receipts[path]); original.write_bytes(original_bytes)
        duplicate = dict(receipt, provider_attempt_id=uuid.uuid4().hex, insights_result=reports[phase])
        self.store.put_receipt(duplicate)
        self.assertIn('optional evidence unavailable or invalid', self.collect(current).stderr)
        self.assertEqual((ledger.read_bytes(), retained.read_bytes()), before)


if __name__ == '__main__':
    unittest.main()
