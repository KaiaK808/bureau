"""Real review floors with real commits, fake providers and a floor mutation."""
import re
import subprocess
import unittest

import review_merge_test as review
import semgrep_pipeline_test as shared
from secret_rules_test import fake_keys


class SecretReviewTests(unittest.TestCase):
    def setUp(self):
        self.f = review.PipelineTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.repo, self.root = self.f.repo, self.f.root
        self.key = fake_keys()[0][1]

    def commit_input(self, secret=True):
        self.f.f.git('checkout', '-q', '001-task')
        (self.repo / 'credential.txt').write_text('ordinary line\n' + (self.key if secret else 'ordinary value') + '\n')
        self.f.f.git('add', 'credential.txt')
        self.f.f.git('commit', '-qm', 'test: review scan input')
        self.f.f.git('push', '-q', 'origin', '001-task')
        self.f.f.git('checkout', '-q', 'main')

    def assert_secret_blocks(self, record, body):
        self.assertEqual(record['final_verdict'], 'REQUEST_CHANGES', 'secret finding was approved')
        self.assertIn('**Verdict**: REQUEST_CHANGES', body)
        self.assertIn('SECRET FLOOR', body)
        for text in ('credential.txt', '"line":2', 'aws-access-key-id', '[REDACTED]'):
            self.assertIn(text, body)
        self.assertNotIn(self.key, body)

    def test_committed_key_blocks_approving_reviewers(self):
        self.commit_input()
        record, body, _ = shared.ReviewPipelineTests.run_review(self, 0)
        self.assert_secret_blocks(record, body)

    def test_without_key_the_same_review_approves(self):
        self.commit_input(secret=False)
        record, body, _ = shared.ReviewPipelineTests.run_review(self, 20)
        self.assertEqual(record['final_verdict'], 'APPROVE')
        self.assertNotIn('SECRET FLOOR', body)

    def test_missing_crashing_and_unparseable_helper_are_not_measured(self):
        # Exercise four helper failures without the separate cycle/round caps
        # changing the expected rework outcome on the later fixture runs.
        self.f.config['agents'].update(max_review_cycles=10, max_review_rounds=10)
        self.commit_input(secret=False)
        helper = self.repo / 'scripts/bureau-secrets.py'
        original = helper.read_bytes()
        for replacement in (None, 'raise RuntimeError("fixture failure")\n', 'print("not json")\n',
                            'print(\'{"status":"measured","blocking":false}\')\n'):
            with self.subTest(failure='missing' if replacement is None else replacement.split('(')[0]):
                helper.unlink(missing_ok=True)
                if replacement is not None:
                    helper.write_text(replacement)
                record, body, _ = shared.ReviewPipelineTests.run_review(self, 0)
                self.assertEqual(record['final_verdict'], 'REQUEST_CHANGES')
                self.assertIn('"status":"not_measured"', body)
                self.assertIn('SECRET FLOOR', body)
        helper.write_bytes(original)

    def test_negative_control_removing_floor_fails_blocking_assertion(self):
        self.commit_input()
        record, body, _ = shared.ReviewPipelineTests.run_review(self, 0)
        self.assert_secret_blocks(record, body)
        helper = self.repo / 'scripts/bureau-config.sh'
        text = helper.read_text()
        anchor = 'if [ "${SECRETS_BLOCKED:-0}" = 1 ] && [ "$verdict" = APPROVE ]; then'
        self.assertEqual(text.count(anchor), 1, 'CONTROL PATCH FAILED: secret floor anchor')
        helper.write_text(text.replace(anchor, 'if false; then'))
        # Same real commit and scanner, still masked evidence, now APPROVE/20.
        # Catch the exact assertion used above, proving that it detects removal.
        record, body, _ = shared.ReviewPipelineTests.run_review(self, 20)
        self.assertIn('aws-access-key-id', body)
        with self.assertRaisesRegex(AssertionError, 'secret finding was approved'):
            self.assert_secret_blocks(record, body)

    def test_floor_precedes_caps_preserves_block_and_red_build(self):
        source = (review.SCRIPTS / 'bureau-config.sh').read_text()
        functions = '\n'.join(re.search(r'^' + name + r'\(\) \{\n.*?^\}', source, re.M | re.S)[0]
                              for name in ('decide_review_verdict', 'apply_build_failure', '_review_count', '_review_shown'))
        for verdict, build, cycles, rounds, expected in (
                ('APPROVE', 'true', '0', '1', 'REQUEST_CHANGES'),
                ('APPROVE', 'true', '3', '1', 'BLOCK'), ('APPROVE', 'true', '0', '4', 'BLOCK'),
                ('BLOCK', 'true', '0', '1', 'BLOCK'), ('APPROVE', 'false', '3', '1', 'BLOCK')):
            process = subprocess.run(['/bin/bash', '-c', functions + '\nSECRETS_BLOCKED=1; decide_review_verdict "$@"',
                                      'test', verdict, '0', '0', build, cycles, '3', rounds, '4'],
                                     capture_output=True, text=True, timeout=10)
            self.assertEqual(process.returncode, 0, process.stderr)
            self.assertEqual(process.stdout.splitlines()[0], expected)
            self.assertNotIn('only the build check stayed red', process.stdout)


if __name__ == '__main__':
    unittest.main()
