"""Private mutations must fail the positive convergence and legacy-count assertions."""
from pathlib import Path
import sys
import tempfile

from mutation_checks import mutation_caught

ROOT = Path(__file__).resolve().parents[1]
sources = {key: (ROOT / 'templates/scripts' / name).read_text() for key, name in (
    ('BUREAU_TEST_REVIEW_SCRIPT', 'code-review-pipeline.sh'), ('BUREAU_TEST_REVIEW_CONFIG', 'bureau-config.sh'))}
controls = (
    ('no-convergence', 'BUREAU_TEST_REVIEW_SCRIPT', '  VERDICT=$(review_convergence "$MERGED_RAW" "$BUREAU_REVIEW_MIN_SEVERITY")',
     '  VERDICT="$PROVIDER_VERDICT"', 'ReviewRoundPipelineTests.test_minor_only_request_changes_converges',
     'AssertionError: 0 != 20'),
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
     'AssertionError: 4 != 8'),
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
        mutation_caught([sys.executable, str(ROOT / 'tests/review_convergence_test.py'), test],
                        env={env_key:str(path)}, originals={path:source.encode()},
                        timeout=60,expected=expected)
        print('PASS negative control: ' + name + ' fails the unchanged positive assertion')
