"""Exercise one-hop publication verification against real Git metadata."""
import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch
import uuid

ROOT = Path(__file__).resolve().parents[1]


def module(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / 'templates/scripts' / (name + '.py'))
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


e, g = module('bureau-evidence'), module('bureau-gates')


class PublicationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='publication space ')
        self.addCleanup(self.temp.cleanup)
        self.repo = Path(self.temp.name)
        self.git('init', '-q', '-b', 'main')
        self.git('config', 'user.name', 'Publication test')
        self.git('config', 'user.email', 'test@bureau.invalid')
        (self.repo / 'feature.py').write_text('original\n')
        self.git('add', 'feature.py')
        self.git('commit', '-qm', 'baseline A')
        self.before = self.git('rev-parse', 'HEAD')
        self.store = e.Store(self.repo / '.git/bureau')
        self.run = uuid.uuid4().hex
        self.store.begin_run(self.run, 'TEAM-123', self.repo, self.before)
        self.command = 'echo configured-command'
        self.result = dict(status='NEEDS_HUMAN', tasks_done=1, tasks_skipped=0, tasks_needs_human=1,
                           notes=dict(needs_human=[dict(kind='gate', command=self.command, reason='gate hold')], skipped=[]))
        self.identity = self.receipt()

    def git(self, *args, input=None):
        return subprocess.check_output(['git', '-C', str(self.repo), *args], input=input,
                                       text=True, stderr=subprocess.DEVNULL).strip()

    def receipt(self, attempt=None):
        if attempt is None:
            # Background runtime attempts have phase=stage; their provider
            # receipt has phase=implement, independently matched by identity.
            attempt = self.store.begin_attempt(self.run, 'TEAM-123', 'implement', 'stage', self.repo, self.before)
        identity = dict(run_id=self.run, provider_attempt_id=uuid.uuid4().hex, stage_attempt_id=attempt,
                        issue='TEAM-123', stage='implement', phase='implement', schema_sha256='1' * 64)
        execution = dict(attempt_id=uuid.uuid4().hex, gate_id=g.digest(self.command), command_sha256=g.digest(self.command),
                         command_id='repo.test_command', head=self.before, end_head=self.before, normalized_exit=1,
                         source='codex.command_execution', environment='sandbox', abort=None,
                         call_id_sha256='2' * 64, duration_seconds=.1, bytes=3, sha256='3' * 64, stage_log=None)
        self.store.put_receipt(dict(**identity, provider='codex', outcome='complete', provider_exit=0, normalized_exit=0,
                                    structured_result=self.result, start_head=self.before, end_head=self.before,
                                    started_at=time.time(), ended_at=time.time(), gate_attempts=[execution]))
        return identity

    def commit(self, trailer=True, message='executor publication B'):
        (self.repo / 'feature.py').write_text(uuid.uuid4().hex + '\n')
        self.git('add', 'feature.py')
        self.git('commit', '-qm', message, *(['-m', 'Bureau-Generated: true'] if trailer else []))
        return self.git('rev-parse', 'HEAD')

    def record(self, after, identity=None):
        return self.store.record_publication(identity or self.identity, 'implement', self.repo, self.before, after)

    def plan(self, identity=None):
        # The production CLI has the same observational boundary.
        return e.observe(g.plan, self.store, self.result, 'implement', [self.command], self.git('rev-parse', 'HEAD'),
                         identity or self.identity, self.repo) or dict(eligible=False)

    def inject_publication(self, after, **extra):
        # Simulate a stale/forged well-shaped record: live Git must still win.
        with self.store.guard():
            manifest = self.store._manifest(self.run)
            attempt = next(a for a in manifest['attempts'] if a['attempt_id'] == self.identity['stage_attempt_id'])
            attempt['publications'] = [dict(provider_attempt_id=self.identity['provider_attempt_id'],
                                            stage_attempt_id=self.identity['stage_attempt_id'], pre_head=self.before,
                                            head=after, parent_head=self.before, bureau_generated=True,
                                            recorded_at=time.time(), **extra)]
            self.store._save_manifest(manifest)

    def test_unchanged_head_needs_no_publication(self):
        self.assertTrue(self.plan()['eligible'])

    def test_verified_one_hop_uses_current_head_and_metadata_only(self):
        after = self.commit()
        record = self.record(after, {**self.identity, 'raw_command':'SECRET-COMMAND', 'output':'SECRET-OUTPUT'})
        self.assertEqual(set(record), set('provider_attempt_id stage_attempt_id pre_head head parent_head bureau_generated recorded_at'.split()))
        self.assertEqual(record['parent_head'], self.before)
        plan = self.plan()
        self.assertTrue(plan['eligible'])
        self.assertEqual(plan['head'], after)
        first, reservation = self.store.reserve_gate_rerun(self.run, self.identity['stage_attempt_id'], after,
                                                          self.command, g.digest(self.command), plan['gate_attempt_ids'])
        second, reused = self.store.reserve_gate_rerun(self.run, self.identity['stage_attempt_id'], after,
                                                     self.command, g.digest(self.command), plan['gate_attempt_ids'])
        self.assertTrue(first)
        self.assertFalse(second)
        self.assertEqual(reservation, reused)
        self.assertEqual(reservation['head'], after)
        for file in self.store.root.rglob('*.json'):
            self.assertNotIn('SECRET-', file.read_text())

    def test_genuine_commit_without_publication_is_blocked(self):
        self.commit()
        self.assertFalse(self.plan()['eligible'])

    def test_extra_commit_on_publication_is_blocked_and_cannot_chain(self):
        after = self.commit()
        self.record(after)
        current = self.commit(message='extra commit on B')
        self.assertFalse(self.plan()['eligible'])
        with self.assertRaises(ValueError):
            self.store.record_publication(self.identity, 'implement', self.repo, after, current)
        self.assertFalse(self.plan()['eligible'])

    def test_publication_from_another_stage_attempt_is_blocked(self):
        other = self.receipt()
        self.record(self.commit(), other)
        self.assertTrue(self.plan(other)['eligible'])
        self.assertFalse(self.plan()['eligible'])

    def test_publication_from_another_provider_attempt_is_blocked(self):
        other = self.receipt(self.identity['stage_attempt_id'])
        self.record(self.commit(), other)
        self.assertTrue(self.plan(other)['eligible'])
        self.assertFalse(self.plan()['eligible'])

    def test_two_parents_rejected_at_record_and_reconciliation(self):
        tree = self.git('rev-parse', 'HEAD^{tree}')
        sibling = self.git('commit-tree', tree, '-p', self.before, input='sibling\n')
        after = self.git('commit-tree', tree, '-p', self.before, '-p', sibling,
                         input='merge B\n\nBureau-Generated: true\n')
        self.git('update-ref', 'HEAD', after)
        with self.assertRaises(ValueError): self.record(after)
        self.inject_publication(after)
        self.assertFalse(self.plan()['eligible'])

    def test_missing_trailer_rejected_at_record_and_reconciliation(self):
        after = self.commit(trailer=False)
        with self.assertRaises(ValueError): self.record(after)
        self.inject_publication(after)
        self.assertFalse(self.plan()['eligible'])

    def test_trailer_in_prose_is_not_a_trailer(self):
        after = self.commit(trailer=False, message='Bureau-Generated: true\n\nOrdinary prose after the line.')
        with self.assertRaises(ValueError): self.record(after)

    def test_someone_else_moves_head_to_sibling_c(self):
        after = self.commit()
        self.record(after)
        self.git('checkout', '-q', '--detach', self.before)
        current = self.commit(message='someone else publishes C')
        self.assertNotEqual(after, current)
        self.assertTrue(e.verify_publication(self.repo, self.before, current))
        self.assertFalse(self.plan()['eligible'])

    def test_known_publication_cannot_be_rewound_to_receipt_head(self):
        self.record(self.commit())
        self.git('reset', '--hard', self.before)
        self.assertFalse(self.plan()['eligible'])

    def test_wrong_timestamp_stays_blocked(self):
        after = self.commit()
        self.inject_publication(after)
        for at in (1, time.time() + 3600):
            with self.store.guard():
                manifest = self.store._manifest(self.run)
                manifest['attempts'][0]['publications'][0]['recorded_at'] = at
                self.store._save_manifest(manifest)
            self.assertFalse(self.plan()['eligible'])

    def test_identity_and_result_checks_stay_strict(self):
        self.record(self.commit())
        for key, value in (('run_id', uuid.uuid4().hex), ('provider_attempt_id', uuid.uuid4().hex),
                           ('stage_attempt_id', uuid.uuid4().hex), ('issue', 'OTHER-123'), ('stage', 'qa'),
                           ('phase', 'qa'), ('schema_sha256', '4' * 64)):
            with self.subTest(key=key):
                self.assertFalse(self.plan({**self.identity, key:value})['eligible'])
        self.result['notes']['needs_human'][0]['reason'] = 'changed result'
        self.assertFalse(self.plan()['eligible'])

    def test_publication_does_not_authorize_other_execution_head(self):
        self.record(self.commit())
        receipt = self.store.receipt_path(self.run, self.identity['provider_attempt_id'])
        value = json.loads(receipt.read_text())
        value['gate_attempts'][0]['head'] = value['gate_attempts'][0]['end_head'] = '5' * 40
        e.atomic(receipt, value)
        self.assertFalse(self.plan()['eligible'])

    def test_write_failure_keeps_head_change_blocked(self):
        after = self.commit()
        with patch.object(self.store, '_save_manifest', side_effect=OSError('unavailable')):
            self.assertIsNone(e.observe(self.record, after))
        self.assertFalse(self.plan()['eligible'])

    def test_publication_allowlist_rejects_raw_fields(self):
        self.inject_publication(self.commit(), raw_command='SECRET-COMMAND')
        self.assertFalse(self.plan()['eligible'])

    def test_publication_from_another_workspace_is_blocked(self):
        after = self.commit()
        with self.assertRaises(ValueError):
            self.store.record_publication(self.identity, 'implement', self.repo.parent, self.before, after)
        self.assertFalse(self.plan()['eligible'])


if __name__ == '__main__':
    unittest.main(verbosity=2)
