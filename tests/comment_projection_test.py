"""Exercise the shared projection and other real comment-producing call sites."""
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT/'templates/scripts'
CANARY = 'A narrow runner cap required dividing the private canary pass.'
PUBLIC = 'PUBLIC_STAGE_SUMMARY'


def spec_review_summary():
    source = (SCRIPTS/'spec-review-pipeline.sh').read_text()
    start = source.index('REVIEW_SUMMARY=')
    return source[start:source.index('\nif [ "$REVIEW_STATUS" = "FAIL" ]; then', start)]


def function(source, name):
    return re.search(r'^' + re.escape(name) + r'\(\) \{.*?^\}', source, re.M | re.S)[0]


class CommentProjectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='bureau-pr7-projection-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        config = (SCRIPTS/'bureau-config.sh').read_text()
        self.helpers = '\n'.join(function(config, name) for name in
                                 ('bureau_comment_projection', 'parse_claude_json', 'post_comment'))
        self.helpers += '''
bureau_ticket_checkpoint() { :; }
bureau_cap_comment() { printf '%s' "$1"; }
_resolve_issue_uuid() { echo fixture-id; }
linear_raw() { printf '%s' "$1" > "$PAYLOAD"; echo '{"data":{"commentCreate":{"success":true}}}'; }
'''

    def run_shell(self, source, value):
        result = subprocess.run(['bash', '-euo', 'pipefail', '-c', self.helpers + '\n' + source],
                                env={**os.environ, 'RAW':value, 'PAYLOAD':str(self.root/'payload.json'),
                                     'ISSUE':'TEAM-712', 'BRANCH':'001-test', 'SPEC_DIR':'specs/001-test',
                                     'UPSTREAM_LINK':'https://example.invalid/commit/1', 'LLM_NOTE':'', 'BUREAU_DRY_RUN':'0',
                                     'GOVERNANCE_ERROR':'', 'PROJECTION_INPUT':str(self.root/'projection-input')},
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    def test_projection_nested_and_embedded_fences(self):
        embedded = PUBLIC + '\n```json\n' + json.dumps(dict(insights=[CANARY], status='GREEN')) + '\n```'
        for value in (dict(comment=embedded, insights=[CANARY]),
                      dict(coverage_notes=dict(summary=PUBLIC, insights=[CANARY]), insights=[CANARY]),
                      dict(result=json.dumps(dict(summary=PUBLIC, insights=[CANARY])))):
            with self.subTest(value=value):
                output = self.run_shell('bureau_comment_projection "$RAW"', json.dumps(value))
                self.assertNotIn(CANARY, output)
                self.assertNotIn('insights', output)

    def test_malformed_and_plain_prose_contract(self):
        for text in ('{"insights":[' + json.dumps(CANARY),
                     'Legacy insights = ' + CANARY,
                     '```json\n{"insights":[' + json.dumps(CANARY) + '\n```',
                     '```json\n{"insights":[' + json.dumps(CANARY),
                     'Legacy\n```json\n{"insigh\\u0074s":[' + json.dumps(CANARY)):
            with self.subTest(text=text):
                self.assertNotIn(CANARY, self.run_shell('bureau_comment_projection "$RAW"', text))
        for text in ('SKIP', PUBLIC, 'REVIEW_VERDICT: BLOCK\n' + PUBLIC):
            self.assertEqual(self.run_shell('bureau_comment_projection "$RAW"', text), text)

    def test_projector_failure_preserves_status_and_verdict(self):
        for field, value in (('status', 'NEEDS_HUMAN'), ('verdict', 'BLOCK'), ('verdict', 'APPROVE')):
            with self.subTest(field=field, value=value):
                text = json.dumps({field:value, 'insights':[CANARY]})
                self.assertEqual(self.run_shell('python3() { return 1; }; parse_claude_json "$RAW" \'.' + field + '\'', text).strip(), value)

    def test_other_comment_producers_capture_payloads(self):
        spec = (SCRIPTS/'spec-pipeline.sh').read_text()
        upstream = (SCRIPTS/'upstream-port.sh').read_text()
        start = spec.index('  DIGEST_BODY="')
        digest = spec[start:spec.index('\nfi', start)]
        research = re.search(r'^    if (post_comment .*?); then$', spec, re.M)[1]
        start = upstream.index('    PR_BODY="$(bureau_comment_projection')
        upstream_body = upstream[start:upstream.index('\n  else', start)]
        producers = dict(
            spec_digest='SPEC_DIGEST="$RAW"\n' + digest,
            research='RESEARCH_RAW="$RAW"\n' + research,
            upstream='CLAUDE_OUT="$RAW"\n' + upstream_body + '\npost_comment "$ISSUE" "$PR_BODY"',
            spec_review='REVIEW_RESULT="$RAW"\n' + spec_review_summary() + '\npost_comment "$ISSUE" "$REVIEW_SUMMARY"',
            ux='UX_RESULT="$RAW"\n' + re.search(r'^DESIGN_SUMMARY=.*$', (SCRIPTS/'ux-pipeline.sh').read_text(), re.M)[0] + '\npost_comment "$ISSUE" "$DESIGN_SUMMARY"')
        result = dict(summary=dict(text=PUBLIC, insights=[CANARY]), insights=[CANARY])
        for name, producer in producers.items():
            for form in ('object', 'envelope', 'fenced'):
                text = json.dumps(result)
                if form == 'envelope': text = json.dumps(dict(result=text))
                if form == 'fenced': text = '```json\n' + text + '\n```'
                with self.subTest(producer=name, form=form):
                    self.run_shell(producer, text)
                    payload = json.loads((self.root/'payload.json').read_text())
                    body = payload['variables']['body']
                    self.assertNotIn(CANARY, body, 'other producer transport leaked local insight')
                    self.assertIn(PUBLIC, body)

    def test_governance_error_survives_whole_summary_projection(self):
        reason = 'Governance and security: missing Decision and Rationale before code.'
        config = (SCRIPTS/'bureau-config.sh').read_text()
        projector = function(config, 'bureau_comment_projection').replace(
            'bureau_comment_projection()', 'real_bureau_comment_projection()', 1)
        source = projector + '''
bureau_comment_projection() {
  printf '%s' "$1" > "$PROJECTION_INPUT"
  real_bureau_comment_projection "$1"
}
GOVERNANCE_ERROR=''' + repr(reason) + '\nREVIEW_RESULT="$RAW"\n' + spec_review_summary()
        source += '\npost_comment "$ISSUE" "$REVIEW_SUMMARY"'
        for summary in (PUBLIC, dict(text=PUBLIC, insights=[CANARY]),
                        PUBLIC+'\n```json\n'+json.dumps(dict(insights=[CANARY]))+'\n```',
                        '{"insights":['+json.dumps(CANARY)):
            for failed_projector in (False, True):
                with self.subTest(summary=summary, failed_projector=failed_projector):
                    self.run_shell(('python3() { return 1; };\n' if failed_projector else '') + source,
                                   json.dumps(dict(summary=summary, insights=[CANARY])))
                    self.assertIn(reason, (self.root/'projection-input').read_text())
                    body = json.loads((self.root/'payload.json').read_text())['variables']['body']
                    self.assertIn(reason, body)
                    self.assertNotIn(CANARY, body)


if __name__ == '__main__': unittest.main()
