"""The new enabled-mode assertions must detect the released v3.5.0 runtime."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile

from review_adr_test import old_scripts

ROOT = Path(__file__).resolve().parents[1]
with tempfile.TemporaryDirectory(prefix='bureau-adr-v350-') as scratch:
    scripts = Path(scratch).resolve() / 'scripts'
    old_scripts(scripts)
    # Old schemas cannot parse new fields; this test deliberately uses an old
    # result so failure demonstrates missing ADR grounding, not schema drift.
    cases = (
        ('PipelineTests.test_enabled_pipeline_uses_pinned_head_after_checkout_changes',
         "b'PINNED_ACCEPTED' not found"),
        ('ConvergenceTests.test_optional_valid_refs_and_all_malformed_refs',
         "'APPROVE' != 'BLOCK'"),
    )
    for case, expected in cases:
        completed = subprocess.run([sys.executable, str(ROOT / 'tests/review_adr_test.py'), case],
                                   env={**os.environ, 'BUREAU_TEST_ADR_SCRIPTS':str(scripts)},
                                   capture_output=True, text=True, timeout=90)
        if completed.returncode != 1 or expected not in completed.stderr or 'ERROR:' in completed.stderr:
            raise SystemExit('FAIL v3.5.0 negative control: ' + case + '\n' + completed.stdout + completed.stderr)
        print('PASS v3.5.0 negative control: ' + case + ' fails its positive assertion')
