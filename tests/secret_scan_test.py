"""Real Git branch diffs and base-controlled secret exceptions; offline fixtures."""
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from secret_rules_test import fake_keys, load


class SecretScanTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='bureau-secret-test-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.repo = self.root / 'repo'
        self.repo.mkdir()
        self.env = {**os.environ, 'GIT_CONFIG_GLOBAL': os.devnull, 'GIT_CONFIG_NOSYSTEM': '1',
                    'GIT_CEILING_DIRECTORIES': str(self.root), 'GIT_AUTHOR_NAME': 'Fixture',
                    'GIT_COMMITTER_NAME': 'Fixture', 'GIT_AUTHOR_EMAIL': 'fixture@example.invalid',
                    'GIT_COMMITTER_EMAIL': 'fixture@example.invalid', 'PYTHONDONTWRITEBYTECODE': '1'}
        self.m = load()
        self.key = fake_keys()[0][1]
        self.git('init', '-qb', 'main')
        self.write('source.txt', 'safe\n')
        self.commit()
        self.base = self.head
        self.git('checkout', '-qb', 'work')

    def git(self, *args):
        return subprocess.check_output(['git', '-c', 'maintenance.auto=false', '-c', 'gc.auto=0',
                                       '-C', str(self.repo), *args], env=self.env,
                                       text=True, stderr=subprocess.DEVNULL, timeout=15).strip()

    def write(self, name, content):
        target = self.repo / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)

    def commit(self):
        self.git('add', '.')
        self.git('commit', '-qm', 'test: secret scan fixture')
        self.head = self.git('rev-parse', 'HEAD')

    def measure(self, **kw):
        return self.m.measure(self.repo, self.base, self.head, **kw)

    def test_added_key_is_masked_and_same_diff_without_key_is_clean(self):
        self.write('source.txt', 'safe\n' + self.key + '\n')
        self.commit()
        result = self.measure()
        self.assertEqual(result['status'], 'measured')
        self.assertTrue(result['blocking'])
        self.assertEqual(result['findings'], [dict(file='source.txt', line=2,
                         rule_id='aws-access-key-id', value='[REDACTED]')])
        self.assertNotIn(self.key, json.dumps(result))
        self.write('source.txt', 'safe\nordinary addition\n')
        self.commit()
        control = self.measure()
        self.assertEqual(control['status'], 'measured')
        self.assertFalse(control['blocking'])
        self.assertEqual(control['findings_count'], 0)

    def test_existing_key_and_deleted_key_do_not_block(self):
        self.git('checkout', '-q', 'main')
        self.write('source.txt', self.key + '\nkeep\n')
        self.commit()
        self.base = self.head
        self.git('checkout', '-B', 'work', 'main')
        self.write('source.txt', self.key + '\nkeep\nnew ordinary line\n')
        self.commit()
        self.assertFalse(self.measure()['blocking'])
        self.write('source.txt', 'keep\n')
        self.commit()
        self.assertFalse(self.measure()['blocking'])

    def assert_deletion_measures_clean(self, name):
        self.commit()
        self.base = self.head
        self.git('checkout', '-B', 'work', 'main')
        (self.repo / name).unlink()
        self.commit()
        result = self.measure()
        self.assertEqual(result['status'], 'measured', 'deletion left the scan unmeasured')
        self.assertFalse(result['blocking'])
        self.assertEqual(result['findings_count'], 0)

    def test_deleted_file_over_one_mib_measures_clean(self):
        self.git('checkout', '-q', 'main')
        self.write('large.txt', 'x' * (1024 * 1024 + 1))
        self.assertGreater((self.repo / 'large.txt').stat().st_size, 1024 * 1024)
        self.assert_deletion_measures_clean('large.txt')

    def test_deleted_symlink_measures_clean(self):
        self.git('checkout', '-q', 'main')
        (self.repo / 'link').symlink_to('source.txt')
        self.assert_deletion_measures_clean('link')

    def test_credential_filename_is_masked_in_findings_and_cli_stdout(self):
        name = self.key + '.txt'
        self.write(name, self.key + '\n')
        self.commit()
        result = self.measure()
        self.assertEqual((result['status'], result['blocking']), ('measured', True))
        self.assertEqual(result['findings'], [dict(file='[REDACTED].txt', line=1,
                         rule_id='aws-access-key-id', value='[REDACTED]')])
        process = subprocess.run(['python3', '-I', '-B', self.m.__file__, '--repo', str(self.repo),
                                  '--base', self.base, '--head', self.head], env=self.env,
                                 capture_output=True, text=True, timeout=15)
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertEqual(json.loads(process.stdout), result)
        self.assertNotIn(self.key, process.stdout + process.stderr)

    def test_uses_merge_base_and_literal_paths(self):
        name = 'nested/space [literal].txt'
        self.git('checkout', '-q', 'main')
        self.write('base-only.txt', self.key + '\n')
        # The target independently gained this same key after the fork. It is
        # still a branch addition, even though a base-tip diff would hide it.
        self.write(name, 'safe\n' + self.key + '\n')
        self.commit()
        self.base = self.head
        self.git('checkout', '-q', 'work')
        self.write(name, 'safe\n' + self.key + '\n')
        self.commit()
        result = self.measure()
        self.assertEqual(result['status'], 'measured')
        self.assertEqual([(f['file'], f['line']) for f in result['findings']], [(name, 2)])

    def test_base_exception_future_passes_and_expired_blocks(self):
        self.git('checkout', '-q', 'main')
        now = datetime.now(timezone.utc)
        rows = [dict(id='aws-access-key-id', file='source.txt', owner='security-team',
                     reason='Approved fixture', expires=(now + timedelta(hours=1)).isoformat())]
        self.write(self.m.EXCEPTIONS, json.dumps(rows))
        self.commit()
        self.base = self.head
        self.git('checkout', '-B', 'work', 'main')
        self.write('source.txt', self.key + '\n')
        self.commit()
        active = self.measure(now=now)
        self.assertEqual(active['status'], 'measured')
        self.assertFalse(active['blocking'])
        self.assertEqual(active['findings_suppressed'], 1)
        expired = self.measure(now=now + timedelta(hours=2))
        self.assertEqual(expired['status'], 'measured')
        self.assertTrue(expired['blocking'])
        self.assertEqual(expired['findings_suppressed'], 0)

    def test_head_exception_cannot_suppress_itself(self):
        rows = [dict(id='aws-access-key-id', owner='security-team', reason='Head proposal', expires='2099-01-01')]
        self.write(self.m.EXCEPTIONS, json.dumps(rows))
        self.write('source.txt', self.key + '\n')
        self.commit()
        result = self.measure()
        self.assertTrue(result['blocking'])
        self.assertEqual(result['findings_suppressed'], 0)

    def test_exception_requires_owner_reason_expiry_and_exact_selectors(self):
        valid = dict(id='aws-access-key-id', owner='security-team', reason='Fixture', expires='2099-01-01')
        invalid = [{k: v for k, v in valid.items() if k != missing} for missing in ('owner', 'reason', 'expires')]
        invalid += [dict(valid, expires='invalid'), dict(valid, id='*'), dict(valid, kind='coverage', file='source.txt')]
        for row in invalid:
            with self.subTest(fields=sorted(row)):
                self.write(self.m.EXCEPTIONS, json.dumps([row]))
                self.commit()
                self.base = self.head
                self.assertEqual(self.measure()['status'], 'not_measured')
                self.assertTrue(self.measure()['blocking'])

    def test_unreadable_or_malformed_diff_and_scanner_failure_are_not_measured(self):
        self.write('source.txt', self.key + '\n')
        self.commit()
        original = self.m.scan.git
        for mode in ('unreadable', 'malformed', 'empty', 'scanner'):
            def fail(repo, *args):
                if args[0] == 'diff' and '--name-only' not in args:
                    if mode == 'unreadable':
                        raise subprocess.CalledProcessError(128, ['git', 'diff'], output=self.key.encode())
                    return b'' if mode == 'empty' else b'not a patch\n'
                return original(repo, *args)
            target = patch.object(self.m, 'rule_ids', side_effect=ValueError(self.key)) if mode == 'scanner' else patch.object(self.m.scan, 'git', side_effect=fail)
            with self.subTest(mode=mode), target:
                result = self.measure()
                self.assertEqual(result['status'], 'not_measured')
                self.assertTrue(result['blocking'])
                self.assertNotIn(self.key, json.dumps(result))

    def test_finding_budget_and_file_limit_never_turn_findings_green(self):
        self.write('source.txt', (self.key + '\n') * 25)
        self.commit()
        result = self.measure()
        self.assertTrue(result['blocking'])
        self.assertEqual((len(result['findings']), result['findings_omitted']), (20, 5))
        with patch.object(self.m, 'MAX_BYTES', 4):
            result = self.measure()
        self.assertEqual(result['status'], 'not_measured')
        self.assertTrue(result['blocking'])


if __name__ == '__main__':
    unittest.main()
