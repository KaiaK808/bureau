"""Real review floors with real commits, fake providers and a floor mutation."""
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
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
        for text in ('credential.txt', '"line":2', 'aws-access-key-id', '[REDACTED]', '"locator":', '"path_sha256":'):
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

    def test_duplicate_valid_blocking_results_are_not_measured(self):
        self.commit_input()
        helper = self.repo / 'scripts/bureau-secrets.py'
        source = helper.read_text()
        anchor = '        print(json.dumps(measure(Path(args.repo).resolve(), args.base, args.head), sort_keys=True))'
        self.assertEqual(source.count(anchor), 1, 'CONTROL PATCH FAILED: helper print anchor')
        helper.write_text(source.replace(anchor,
            '        payload = json.dumps(measure(Path(args.repo).resolve(), args.base, args.head), sort_keys=True)\n'
            '        print(payload)\n        print(payload)'))
        record, body, _ = shared.ReviewPipelineTests.run_review(self, 0)
        self.assertEqual(record['final_verdict'], 'REQUEST_CHANGES')
        self.assertIn('"status":"not_measured"', body)
        self.assertIn('SECRET FLOOR', body)
        self.assertNotIn(self.key, body)

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


class SecretOutputTests(unittest.TestCase):
    """Run the stage's exact output parser with real jq and controlled helper I/O."""
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='bureau-secret-output-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        source = (review.SCRIPTS / 'code-review-pipeline.sh').read_text()
        start = source.index('SECRETS_BLOCKED=1\n')
        end = source.index('\necho ""\necho "Phase 1/3:', start)
        self.block = source[start:end]
        self.base, self.head = 'a' * 40, 'b' * 40

    def payload(self, blocking=True):
        return json.dumps(dict(tool='secrets', base=self.base, head=self.head, status='measured', reason=None,
            findings=[dict(file='source.txt', line=1, rule_id='aws-access-key-id', value='[REDACTED]',
                           locator=dict(blob='c' * 40, path_sha256='d' * 16))] if blocking else [],
            findings_count=int(blocking), findings_omitted=0, findings_suppressed=0, blocking=blocking))

    def run_parser(self, output, flag=None):
        # Only the extracted flag is fault-injected; JSON validation and note
        # projection still use the real jq binary and the unmodified stage block.
        script = '''set -euo pipefail
bureau_without_secrets() { printf '%s' "$SECRET_OUTPUT"; }
jq() {
  if [ "$1" = -r ] && [ "$2" = 'if .blocking then 1 else 0 end' ] && [ "${SECRET_FLAG_OVERRIDE+x}" = x ]; then
    printf '%s' "$SECRET_FLAG_OVERRIDE"
  else
    command jq "$@"
  fi
}
'''
        script += self.block + '\nprintf \'%s\\n\' "$SECRETS_BLOCKED" "$SECRETS_REVIEW_NOTE"\n'
        environment = {**os.environ, 'SECRET_OUTPUT': output, 'REVIEW_BASE': self.base, 'REVIEW_HEAD': self.head,
                       'REVIEW_TMP': str(self.root), 'SCRIPT_REPO': str(self.root)}
        environment.pop('SECRET_FLAG_OVERRIDE', None)
        if flag is not None:
            environment['SECRET_FLAG_OVERRIDE'] = flag
        result = subprocess.run(['/bin/bash', '-c', script], env=environment,
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.split('\n', 1), json.loads((self.root / 'secrets.json').read_text())

    def assert_held_unmeasured(self, output, flag=None):
        (blocked, note), result = self.run_parser(output, flag)
        self.assertEqual(blocked, '1', 'invalid output released the secret floor')
        self.assertEqual(result['status'], 'not_measured')
        self.assertIn('"status":"not_measured"', note)

    def test_exactly_one_object_is_required(self):
        for blocking in (False, True):
            payload = self.payload(blocking)
            (blocked, _), result = self.run_parser(payload)
            self.assertEqual(blocked, str(int(blocking)))
            self.assertEqual(result['status'], 'measured')
            with self.subTest(duplicate_blocking=blocking):
                self.assert_held_unmeasured(payload + '\n' + payload)
        for output in ('', '[]', '[' + self.payload() + ']', 'null', '1', self.payload() + '\ntrailing text'):
            with self.subTest(output_kind=output[:1]):
                self.assert_held_unmeasured(output)

    def test_nonbinary_flags_are_not_measured(self):
        for flag in ('', '2', '01', 'true', ' 1', '1\n1', '0\n0', '0\n1'):
            with self.subTest(flag=flag):
                self.assert_held_unmeasured(self.payload(), flag)

    def test_missing_or_invalid_finding_locators_are_not_measured(self):
        for locator in (None, {}, dict(blob='c' * 41, path_sha256='d' * 16),
                        dict(blob='c' * 40, path_sha256='d' * 15),
                        dict(blob='x' * 40, path_sha256='d' * 16),
                        dict(blob='c' * 40, path_sha256='x' * 16),
                        dict(blob='c' * 40, path_sha256='d' * 16, extra='unexpected')):
            with self.subTest(locator=locator):
                result = json.loads(self.payload())
                if locator is None:
                    del result['findings'][0]['locator']
                else:
                    result['findings'][0]['locator'] = locator
                self.assert_held_unmeasured(json.dumps(result))


if __name__ == '__main__':
    unittest.main()
