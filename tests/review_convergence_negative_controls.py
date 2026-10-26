"""Private mutations must fail the positive convergence and legacy-count assertions."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
source = (ROOT / 'templates/scripts/code-review-pipeline.sh').read_text()
controls = (
    ('no-convergence', '  VERDICT=$(review_convergence "$MERGED_RAW" "$BUREAU_REVIEW_MIN_SEVERITY")',
     '  VERDICT="$PROVIDER_VERDICT"', 'ReviewRoundPipelineTests.test_minor_only_request_changes_converges',
     'AssertionError: 0 != 20'),
    ('renamed-legacy-header', '🔄 Code Review: **Changes Requested**',
     '🔄 Review: **Changes Requested**', 'CountsTests.test_legacy_header_counts_a_completed_round',
     "AssertionError: {'cycles': 0, 'rounds': 0} != {'cycles': 1, 'rounds': 1}"),
)
with tempfile.TemporaryDirectory(prefix='bureau-pr8-controls-') as scratch:
    for name, old, new, test, expected in controls:
        if source.count(old) != 1: raise SystemExit('CONTROL PATCH FAILED: ' + name)
        path = Path(scratch) / (name + '.sh'); path.write_text(source.replace(old, new))
        completed = subprocess.run([sys.executable, str(ROOT / 'tests/review_convergence_test.py'), test],
                                   env={**os.environ, 'BUREAU_TEST_REVIEW_SCRIPT':str(path)},
                                   capture_output=True, text=True, timeout=60)
        if completed.returncode != 1 or expected not in completed.stderr or 'FAILED (failures=1)' not in completed.stderr:
            raise SystemExit('CONTROL FAILED TO DETECT REGRESSION: ' + name + '\n' + completed.stdout + completed.stderr)
        print('PASS negative control: ' + name + ' fails the unchanged positive assertion')
