"""Pure Python rule checks; no Git, providers, network or fixture repositories."""
import importlib.util
from contextlib import redirect_stderr, redirect_stdout
import hashlib
import io
import json
from pathlib import Path
import re
import sys
import unittest
from unittest.mock import Mock, patch

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]


def load(path=None):
    spec = importlib.util.spec_from_file_location('secrets_test', path or ROOT / 'templates/scripts/bureau-secrets.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def fake_keys():
    # Construct every fake credential at runtime, including PEM markers. Never
    # commit a complete token-shaped fixture or trigger repository push protection.
    mixed = 'aB3dE6gH9jK2mN5pQ8sT1vW4yZ7'
    return [
        ('aws-access-key-id', 'AK' + 'IA' + 'B2C3D4E5F6G7H8J9'),
        ('aws-access-key-id', 'AS' + 'IA' + 'B2C3D4E5F6G7H8J9'),
        ('github-token', 'gh' + 'p_' + (mixed * 2)[:36]),
        ('github-token', 'github_' + 'pat_' + mixed[:22] + '_' + (mixed * 3)[:59]),
        ('anthropic-api-key', 'sk-' + 'ant-' + 'api03-' + mixed * 3),
        ('openai-api-key', 'sk-' + (mixed * 2)[:48]),
        ('openai-api-key', 'sk-' + 'proj-' + mixed * 4),
        ('openai-api-key', 'sk-' + 'svcacct-' + mixed * 4),
        ('slack-token', 'xo' + 'xb-' + '12345678901-' * 2 + mixed),
        ('slack-token', 'xo' + 'xp-' + '12345678901-' * 3 + mixed),
        ('slack-token', 'xa' + 'pp-1-' + 'B2C3D4E5F6G-' + '12345678901-' + 'a1b2c3d4' * 8),
        ('stripe-live-key', 'sk_' + 'live_' + mixed),
        ('stripe-live-key', 'rk_' + 'live_' + mixed * 3),
        ('google-api-key', 'AI' + 'za' + (mixed * 2)[:35]),
        *[('private-key-header', '-----' + 'BEGIN ' + kind + 'PRIVATE ' + 'KEY' + '-----')
          for kind in ('', 'RSA ', 'EC ', 'DSA ', 'OPENSSH ', 'ENCRYPTED ')],
        ('generic-secret-assignment', 'password' + '=' + mixed),
        ('generic-secret-assignment', 'SECRET' + ' = "' + mixed + '"'),
        ('generic-secret-assignment', 'password' + '\t=\t' + mixed),
    ]


class SecretRuleTests(unittest.TestCase):
    def setUp(self):
        self.m = load()

    def test_each_required_format_has_a_stable_rule(self):
        self.assertEqual(len({row[0] for row in self.m.SECRET_RULES}), len(self.m.SECRET_RULES))
        for rule, value in fake_keys():
            with self.subTest(rule=rule):
                self.assertIn(rule, self.m.rule_ids(value), 'constructed format was not detected')

    def test_short_low_entropy_and_ordinary_assignments_are_not_findings(self):
        for value in ('password=example', 'secret=' + 'x' * 80, 'password=' + 'abCD' * 20,
                      'password=' + '0123456789abcdef' * 3, 'secret=' + 'documentationplaceholder' * 2,
                      'secret=os.environ["SECRET"]', 'password=${PASSWORD}'):
            with self.subTest(length=len(value)):
                self.assertEqual(self.m.rule_ids(value), [])

    def assert_tab_assignments(self):
        value = 'aB3dE6gH9jK2mN5pQ8sT1vW4yZ7'
        for before, after in (('\t', ''), ('', '\t'), ('\t', '\t'), (' \t', '\t ')):
            self.assertIn('generic-secret-assignment', self.m.rule_ids('password' + before + '=' + after + value),
                          'tab-separated assignment was missed')

    def test_tab_separated_assignments_and_separator_boundaries(self):
        self.assert_tab_assignments()
        value = 'aB3dE6gH9jK2mN5pQ8sT1vW4yZ7'
        for separator in ('t', '\\'):
            self.assertEqual(self.m.rule_ids('password' + separator + '=' + separator + value), [])

    def test_doubled_tab_escape_control_fails_the_positive_assertion(self):
        broken = tuple((rule, re.compile(pattern.pattern.replace(r'[ \x09]', r'[ \\t]')), floor)
                       for rule, pattern, floor in self.m.SECRET_RULES)
        with patch.object(self.m, 'SECRET_RULES', broken):
            with self.assertRaisesRegex(AssertionError, 'tab-separated assignment was missed'):
                self.assert_tab_assignments()

    def test_metadata_masks_all_formats_and_overlapping_matches(self):
        for rule, value in fake_keys():
            with self.subTest(rule=rule):
                masked = self.m.mask_metadata('config/' + value + '.txt')
                self.assertNotIn(value, masked)
                self.assertEqual(self.m.rule_ids(masked), [])
        key = fake_keys()[2][1]
        self.assertEqual(self.m.mask_metadata('password=' + key), self.m.MASK)
        key = fake_keys()[0][1]
        self.assertEqual(self.m.mask_metadata(key + '/' + key + '.txt'),
                         self.m.MASK + '/' + self.m.MASK + '.txt')
        for ordinary in ('config/ordinary.txt', 'password=example', 'secret=' + 'x' * 80):
            self.assertEqual(self.m.mask_metadata(ordinary), ordinary)

    def test_metadata_assignment_matching_preserves_path_separators(self):
        value = fake_keys()[-3][1].split('=', 1)[1]
        name = 'config/password=' + value + '/target.txt'
        self.assertEqual(self.m.mask_metadata(name), 'config/[REDACTED]/target.txt',
                         'metadata assignment consumed the next path component')
        self.assertEqual(self.m.mask_metadata('password=' + value[:12] + '/' + value[12:] + '/target.txt'),
                         'password=' + value[:12] + '/' + value[12:] + '/target.txt')

    def test_token_boundaries_test_keys_and_split_values_are_not_findings(self):
        for rule, value in fake_keys():
            if rule in ('private-key-header', 'generic-secret-assignment'):
                continue
            self.assertNotIn(rule, self.m.rule_ids('prefix_' + value), 'embedded identifier matched')
            self.assertNotIn(rule, self.m.rule_ids(value[:8] + '\n' + value[8:]), 'split token matched')
        self.assertEqual(self.m.rule_ids('sk_' + 'test_' + 'aB3dE6gH9jK2mN5pQ8sT1vW4yZ7'), [])

    def test_hunks_use_physical_lines_and_reject_incomplete_output(self):
        before, after = b'keep\n', b'keep\nadded\x0btext\r\n'
        patch = b'diff --git a/file b/file\n@@ -1,0 +2 @@\n+added\x0btext\r\n'
        self.assertEqual(list(self.m.added_lines(patch, before, after)), [(2, b'added\x0btext\r')])
        for invalid in (b'', b'not a patch\n', patch[:-1], patch.replace(b'+2 @@', b'+3 @@'),
                        patch.replace(b'+added', b'+different'), patch.replace(b'+2 @@', b'+2,2 @@')):
            with self.assertRaises(ValueError):
                list(self.m.added_lines(invalid, before, after))

    def test_add_delete_replace_and_missing_newlines(self):
        cases = [
            (b'', b'new\n', b'@@ -0,0 +1 @@\n+new\n', [(1, b'new')]),
            (b'old\n', b'', b'@@ -1 +0,0 @@\n-old\n', []),
            (b'old\n', b'new\n', b'@@ -1 +1 @@\n-old\n+new\n', [(1, b'new')]),
            (b'old', b'new', b'@@ -1 +1 @@\n-old\n\\ No newline at end of file\n+new\n\\ No newline at end of file\n', [(1, b'new')]),
            (b'a\nb\nc\nd\n', b'A\nb\nc\nD\n', b'@@ -1 +1 @@\n-a\n+A\n@@ -4 +4 @@\n-d\n+D\n', [(1, b'A'), (4, b'D')]),
            (b'old\n', b'+++ text\n', b'@@ -1 +1 @@\n-old\n++++ text\n', [(1, b'+++ text')]),
        ]
        for before, after, diff, expected in cases:
            self.assertEqual(list(self.m.added_lines(diff, before, after)), expected)

    def test_sources_contain_no_complete_fixture_credentials(self):
        # Fragments are consumed into CHANGELOG.md at release time, so scan both.
        paths = [ROOT / name for name in (
            'templates/scripts/bureau-secrets.py', 'templates/scripts/code-review-pipeline.sh',
            'tests/secret_rules_test.py', 'tests/secret_scan_test.py', 'tests/secret_scan_pipeline_test.py',
            'docs/configuration.md', 'docs/site/configuration.html', 'references/runtime-patterns.md',
            'CHANGELOG.md')] + sorted((ROOT / 'changes/unreleased').glob('*.md'))
        for path in paths:
            for number, line in enumerate(path.read_text().split('\n'), 1):
                self.assertEqual(self.m.rule_ids(line), [], path.name + ':' + str(number) + ' contains a token-shaped literal')


class SecretMeasurementUnitTests(unittest.TestCase):
    """Exercise the real blob reader and hunk validation with only Git I/O faked."""
    def setUp(self):
        self.m = load()
        self.base, self.head = 'a' * 40, 'b' * 40

    def measure(self, old, head, blobs=None, patches=None, *, cli=False):
        blobs, patches = blobs or {}, patches or {}

        def git(repo, *args):
            if args == ('merge-base', self.base, self.head):
                return self.base.encode()
            if args[:2] == ('cat-file', '-s'):
                return str(len(blobs[args[2]])).encode()
            if args[:2] == ('cat-file', 'blob'):
                return blobs[args[2]]
            if args[0] == 'diff':
                return patches[args[-1].removeprefix(':(literal)')]
            self.fail('unexpected Git operation')

        with patch.object(self.m.scan, 'tree', side_effect=(old, head, old)), \
                patch.object(self.m.scan, 'git', side_effect=git):
            if cli:
                output, errors = io.StringIO(), io.StringIO()
                with patch.object(sys, 'argv', ['secrets', '--base', self.base, '--head', self.head]), \
                        redirect_stdout(output), redirect_stderr(errors):
                    self.assertEqual(self.m.main(), 0)
                self.assertEqual(errors.getvalue(), '')
                return output.getvalue()
            return self.m.measure(Path('.'), self.base, self.head)

    def measure_additions(self, files, *, cli=False):
        head, blobs, patches = {}, {}, {}
        for name, text in files.items():
            content = (text + '\n').encode()
            # Git blob identity without invoking Git or writing a fixture file.
            blob = hashlib.sha1(b'blob ' + str(len(content)).encode() + b'\0' + content).hexdigest()
            head[name] = ['100644', 'blob', blob]
            blobs[blob] = content
            patches[name] = b'@@ -0,0 +1 @@\n+' + content
        return self.measure({}, head, blobs, patches, cli=cli), head

    def assert_not_measured(self, output):
        result = json.loads(output)
        self.assertEqual((result['status'], result['blocking']), ('not_measured', True))
        self.assertEqual(result['reason'], 'secret scan inputs or result unavailable')
        self.assertEqual(result['findings'], [])
        self.assertEqual((result['findings_count'], result['findings_omitted'], result['findings_suppressed']), (0, 0, 0))
        for _, value in fake_keys():
            self.assertNotIn(value, output)

    def test_scanner_failures_after_an_earlier_finding_are_not_measured(self):
        key, later = fake_keys()[0][1], fake_keys()[2][1]
        files = {'a/' + key + '.txt': key, 'z/source.txt': later}
        for target in ('rule_ids', 'rule_matches'):
            original = getattr(self.m, target)
            for error in (ValueError, RuntimeError):
                def fail(text, **kwargs):
                    if text == later:
                        raise error(later)
                    return original(text, **kwargs)

                with self.subTest(target=target, error=error.__name__), patch.object(self.m, target, side_effect=fail) as scanner:
                    output, _ = self.measure_additions(files, cli=True)
                    self.assert_not_measured(output)
                    self.assertTrue(any(call.args[0] == later for call in scanner.call_args_list))

    def test_redaction_failures_are_not_measured(self):
        key = fake_keys()[0][1]
        files = {'a/' + key + '.txt': key, 'z/source.txt': fake_keys()[2][1]}
        original = self.m.mask_metadata
        for stage in ('masked-values', 'file', 'rule-id'):
            for error in (ValueError, RuntimeError):
                failed = []

                def fail(text, values=()):
                    if ((stage == 'masked-values' and not values)
                            or (stage == 'file' and values and text == 'z/source.txt')
                            or (stage == 'rule-id' and values and text == 'github-token')):
                        failed.append(stage)
                        raise error(key)
                    return original(text, values)

                with self.subTest(stage=stage, error=error.__name__), patch.object(self.m, 'mask_metadata', side_effect=fail):
                    output, _ = self.measure_additions(files, cli=True)
                    self.assert_not_measured(output)
                    self.assertEqual(failed, [stage])

    def test_locator_failures_after_an_earlier_finding_are_not_measured(self):
        key = fake_keys()[0][1]
        first, later = 'a/' + key + '.txt', 'z/source.txt'
        files = {first: key, later: fake_keys()[2][1]}
        for stage in ('sha256', 'hexdigest'):
            for error in (ValueError, RuntimeError):
                first_hash = hashlib.sha256(first.encode())
                failure = error(key)
                if stage == 'hexdigest':
                    failure = Mock()
                    failure.hexdigest.side_effect = error(key)
                with self.subTest(stage=stage, error=error.__name__), \
                        patch.object(self.m.hashlib, 'sha256', side_effect=(first_hash, failure)) as locator:
                    output, _ = self.measure_additions(files, cli=True)
                    self.assert_not_measured(output)
                    self.assertEqual(locator.call_count, 2)

    def test_later_detected_values_and_masked_forms_leave_no_finding_metadata(self):
        key = fake_keys()[0][1]
        password = fake_keys()[-3][1].split('=', 1)[1]
        nested = 'qR4!' + key + '!zS7'
        cases = ((password, password, 'generic-secret-assignment'),
                 (key, '_' + key, 'aws-access-key-id'),
                 (nested, 'qR4![REDACTED]!zS7', 'generic-secret-assignment'),
                 (nested, 'qR4!' + fake_keys()[1][1] + '!zS7', 'generic-secret-assignment'))
        for value, filename, rule in cases:
            with self.subTest(kind=rule, masked_form='[REDACTED]' in filename):
                detection = ('password=' if rule == 'generic-secret-assignment' else '') + value
                files = {'a/' + filename + '.txt': fake_keys()[2][1], 'z/detected.txt': detection}
                # The first finding's rule ID has neither assignment context nor
                # a token boundary. It must use the same scan-wide redaction.
                rules = tuple(('context_' + filename if name == 'github-token' else name, pattern, floor)
                              for name, pattern, floor in self.m.SECRET_RULES)
                with patch.object(self.m, 'SECRET_RULES', rules):
                    output, _ = self.measure_additions(files, cli=True)
                self.assertNotIn(value, output, 'a later detected value escaped through finding metadata')
                result = json.loads(output)
                self.assertEqual((result['status'], result['blocking']), ('measured', True))
                first = result['findings'][0]
                expected = '_[REDACTED]' if filename.startswith('_') else '[REDACTED]'
                self.assertEqual(first['file'], 'a/' + expected + '.txt',
                                 'a masked form of a detected value escaped through the path')
                self.assertEqual(first['rule_id'], 'context_' + expected,
                                 'a masked form of a detected value escaped through the rule ID')

    def test_suppressed_and_omitted_values_still_redact_earlier_metadata(self):
        password = fake_keys()[-3][1].split('=', 1)[1]
        files = {'a/' + password + '.txt': fake_keys()[0][1], 'z/detected.txt': 'password=' + password}
        policy = self.m.scan.module('trivy')
        for suppressed in (False, True):
            with self.subTest(suppressed=suppressed), patch.object(self.m, 'MAX_FINDINGS', 1), \
                    patch.object(policy, 'active_exceptions', return_value=[dict(file='z/detected.txt')] if suppressed else []):
                output, _ = self.measure_additions(files, cli=True)
                self.assertNotIn(password, output, 'a non-published detection escaped through earlier metadata')
                result = json.loads(output)
                self.assertEqual((result['status'], result['blocking']), ('measured', True))
                self.assertEqual(result['findings'][0]['file'], 'a/[REDACTED].txt')
                self.assertEqual(result['findings_suppressed'], int(suppressed))
                self.assertEqual(result['findings_omitted'], int(not suppressed))

    def test_redacted_paths_have_distinct_resolvable_locators_for_identical_blobs(self):
        names = ['config/' + key + '.txt' for _, key in fake_keys()[:2]]
        output, head = self.measure_additions(dict.fromkeys(names, fake_keys()[2][1]), cli=True)
        result = json.loads(output)
        self.assertEqual((result['status'], result['blocking']), ('measured', True))
        findings = result['findings']
        self.assertEqual(len(findings), 2)
        self.assertEqual(len({(row['file'], row['line'], row['rule_id']) for row in findings}), 1)
        self.assertTrue(all('locator' in row for row in findings), 'redacted finding has no resolvable locator')
        self.assertEqual(len({row['locator']['blob'] for row in findings}), 1)
        self.assertEqual(len({row['locator']['path_sha256'] for row in findings}), 2)
        resolved = []
        for row in findings:
            locator = row['locator']
            self.assertRegex(locator['blob'], r'^(?:[0-9a-f]{40}|[0-9a-f]{64})$')
            self.assertRegex(locator['path_sha256'], r'^[0-9a-f]{16}$')
            candidates = [name for name, entry in head.items() if entry[2] == locator['blob']
                          and hashlib.sha256(name.encode()).hexdigest()[:16] == locator['path_sha256']]
            self.assertEqual(len(candidates), 1, 'locator did not resolve to exactly one pinned-tree entry')
            resolved.extend(candidates)
        self.assertEqual(set(resolved), set(names))
        for name in names:
            self.assertNotIn(name, output)

    def assert_clean(self, result):
        self.assertEqual(result['status'], 'measured', 'proven deletion was not measured')
        self.assertFalse(result['blocking'])
        self.assertEqual(result['findings_count'], 0)

    def test_deleted_file_over_one_mib_is_measured_clean(self):
        blob = 'c' * 40
        content = b'x' * (1024 * 1024 + 1)
        self.assertGreater(len(content), self.m.MAX_BYTES)
        self.assert_clean(self.measure({'large.txt': ['100644', 'blob', blob]}, {}, {blob: content}))

    def test_deleted_symlink_is_measured_clean(self):
        blob = 'c' * 40
        self.assert_clean(self.measure({'link': ['120000', 'blob', blob]}, {}, {blob: b'source.txt'}))

    def test_unproven_head_and_present_nonregular_or_large_inputs_stay_held(self):
        with patch.object(self.m.scan, 'tree', side_effect=({}, OSError('tree unavailable'))):
            result = self.m.measure(Path('.'), self.base, self.head)
        self.assertEqual((result['status'], result['blocking']), ('not_measured', True))
        blob = 'c' * 40
        for mode, content in (('120000', b'source.txt'), ('100644', b'x' * (1024 * 1024 + 1))):
            with self.subTest(mode=mode):
                result = self.measure({}, {'present': [mode, 'blob', blob]}, {blob: content})
                self.assertEqual((result['status'], result['blocking']), ('not_measured', True))

    def test_finding_paths_and_rule_context_are_masked_in_stdout(self):
        key = fake_keys()[0][1]
        name, blob, content = 'config/' + key + '.txt', 'c' * 40, (key + '\n').encode()
        head = {name: ['100644', 'blob', blob]}
        patches = {name: b'@@ -0,0 +1 @@\n+' + content}
        rules = (('context-' + key, self.m.SECRET_RULES[0][1], None), *self.m.SECRET_RULES[1:])
        with patch.object(self.m, 'SECRET_RULES', rules):
            output = self.measure({}, head, {blob: content}, patches, cli=True)
        self.assertNotIn(key, output, 'finding metadata leaked a matched value')
        result = json.loads(output)
        self.assertEqual((result['status'], result['blocking']), ('measured', True))
        self.assertEqual(result['findings'], [dict(file='config/[REDACTED].txt', line=1,
                         rule_id='context-[REDACTED]', value='[REDACTED]',
                         locator=dict(blob=blob, path_sha256=hashlib.sha256(name.encode()).hexdigest()[:16]))])


if __name__ == '__main__':
    unittest.main()
