"""Private mutations must fail the unchanged positive ADR assertions."""
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
controls = (
    ('no-adr-prompt', 'code-review-pipeline.sh', '$LESSONS_CONTEXT$ADR_CONTEXT', '$LESSONS_CONTEXT', 3,
     'PipelineTests.test_enabled_pipeline_uses_pinned_head_after_checkout_changes',
     "b'PINNED_ACCEPTED' not found"),
    ('unchecked-adr-refs', 'bureau-config.sh', 'and adr_refs_valid and', 'and', 1,
     'ConvergenceTests.test_unknown_reference_blocks', "AssertionError: 'APPROVE' != 'BLOCK'"),
)
with tempfile.TemporaryDirectory(prefix='bureau-adr-controls-') as scratch:
    for name, filename, old, new, count, test, expected in controls:
        scripts = Path(scratch).resolve() / name / 'scripts'
        shutil.copytree(ROOT / 'templates/scripts', scripts)
        path = scripts / filename
        source = path.read_text()
        if source.count(old) != count:
            raise SystemExit('CONTROL PATCH FAILED: ' + name)
        path.write_text(source.replace(old, new))
        completed = subprocess.run([sys.executable, str(ROOT / 'tests/review_adr_test.py'), test],
                                   env={**os.environ, 'BUREAU_TEST_ADR_SCRIPTS': str(scripts)},
                                   capture_output=True, text=True, timeout=90)
        if (completed.returncode != 1 or expected not in completed.stderr
                or 'FAILED (failures=1)' not in completed.stderr):
            raise SystemExit('CONTROL FAILED TO DETECT REGRESSION: ' + name + '\n'
                             + completed.stdout + completed.stderr)
        print('PASS negative control: ' + name + ' fails the unchanged positive assertion')
