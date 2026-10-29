"""Unchanged positive assertions must reject both private-copy mutations."""
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
controls = [
    ('drop-performance', "for f in values[role]['findings']], base, head)",
     "for f in (values[role]['findings'] if role != 'performance' else [])], base, head)",
     'MergeTests.test_each_specialist_is_preserved_and_template_is_stable'),
    ('bypass-anchors', 'finding = dict(raw)\n        status, _ = anchor_check(finding, base, head)',
     "finding = dict(raw)\n        status, _ = 'valid', None",
     'MergeTests.test_grounding_metadata_cannot_hide_invalid_anchor'),
]
for name, old, new, test in controls:
    with tempfile.TemporaryDirectory(prefix='bureau-review-control-') as directory:
        scripts = Path(directory).resolve() / 'scripts'
        shutil.copytree(ROOT / 'templates/scripts', scripts, ignore=shutil.ignore_patterns('__pycache__'))
        helper = scripts / 'bureau-review.py'; text = helper.read_text()
        if text.count(old) != 1: sys.exit('Mutation target changed: ' + name)
        helper.write_text(text.replace(old, new, 1))
        completed = subprocess.run([sys.executable, str(ROOT / 'tests/review_merge_test.py'), test], cwd=ROOT,
                                   env={**os.environ, 'BUREAU_TEST_REVIEW_SCRIPTS':str(scripts), 'PYTHONDONTWRITEBYTECODE':'1'},
                                   capture_output=True, text=True, timeout=30)
        if completed.returncode == 0 or 'AssertionError' not in completed.stderr:
            sys.exit(name + ' did not fail its positive assertion:\n' + completed.stdout + completed.stderr)
        print('PASS negative control: ' + name + ' rejected by unchanged positive test')
print('2 negative controls passed')
