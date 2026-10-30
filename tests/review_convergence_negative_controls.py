"""Private mutations must fail the positive convergence and legacy-count assertions."""
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sources = {key: (ROOT / 'templates/scripts' / name).read_text() for key, name in (
    ('BUREAU_TEST_REVIEW_SCRIPT', 'code-review-pipeline.sh'), ('BUREAU_TEST_REVIEW_CONFIG', 'bureau-config.sh'))}
controls = (
    ('no-minor-convergence', 'BUREAU_TEST_REVIEW_CONFIG', 'else "APPROVE" end\' 2>/dev/null) || verdict=BLOCK',
     'else "REQUEST_CHANGES" end\' 2>/dev/null) || verdict=BLOCK', 'DecisionTests.test_validated_findings_matrix',
     "AssertionError: 'REQUEST_CHANGES' != 'APPROVE'"),
    ('renamed-legacy-header', 'BUREAU_TEST_REVIEW_SCRIPT', '🔄 Code Review: **Changes Requested**',
     '🔄 Review: **Changes Requested**', 'CountsTests.test_legacy_header_counts_a_completed_round',
     "AssertionError: {'cycles': 0, 'rounds': 0} != {'cycles': 1, 'rounds': 1}"),
    ('unmatched-fixes-approve', 'BUREAU_TEST_REVIEW_CONFIG',
     'any(.fixes_needed[]; . as $fix | (test("\\\\S") and any($deferred[]; . == $fix)) | not)',
     'false', 'DecisionTests.test_exact_required_fixes_can_be_deferred_but_uncertain_matches_cannot',
     "AssertionError: 'APPROVE' != 'REQUEST_CHANGES'"),
    ('unbound-review-policy', 'BUREAU_TEST_REVIEW_SCRIPT',
     'REVIEW_POLICY="v1:$BUREAU_REVIEW_MIN_SEVERITY:$BUREAU_MAX_REVIEW_CYCLES:$BUREAU_MAX_REVIEW_ROUNDS"',
     'REVIEW_POLICY="v1:BUG:3:4"', 'ReviewRoundPipelineTests.test_stricter_severity_does_not_reuse_a_deferred_minor_approval',
     'AssertionError: 3 != 6'),
    ('ordinary-marker-is-metadata', 'BUREAU_TEST_REVIEW_CONFIG',
     '$owned and ($last | startswith("<!-- bureau-review-round:") and (test("-->\\\\s*\\\\S") | not))',
     '($body | contains("<!-- bureau-review-round:"))',
     'ReviewRoundPipelineTests.test_ordinary_marker_mention_does_not_stop_a_paid_review', 'AssertionError: 27 != 20'),
)
with tempfile.TemporaryDirectory(prefix='bureau-pr8-controls-') as scratch:
    for name, env_key, old, new, test, expected in controls:
        source = sources[env_key]
        if source.count(old) != 1: raise SystemExit('CONTROL PATCH FAILED: ' + name)
        path = Path(scratch).resolve() / (name + '.sh'); path.write_text(source.replace(old, new))
        completed = subprocess.run([sys.executable, str(ROOT / 'tests/review_convergence_test.py'), test],
                                   env={**os.environ, env_key:str(path)},
                                   capture_output=True, text=True, timeout=60)
        if (completed.returncode != 1 or expected not in completed.stderr
                or re.search(r'FAILED \(failures=[1-9][0-9]*\)\s*$', completed.stderr) is None):
            raise SystemExit('CONTROL FAILED TO DETECT REGRESSION: ' + name + '\n' + completed.stdout + completed.stderr)
        print('PASS negative control: ' + name + ' fails the unchanged positive assertion')
