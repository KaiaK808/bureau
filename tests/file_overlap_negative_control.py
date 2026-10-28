#!/usr/bin/env python3
"""Remove the durable atomic reservation in a private runtime copy."""
from pathlib import Path
import shutil
import sys
import tempfile

from mutation_checks import mutation_caught

sys.dont_write_bytecode = True
from file_overlap_test import ROOT, SCRIPTS

with tempfile.TemporaryDirectory(prefix='bureau-1748-negative-') as scratch:
    scripts = Path(scratch).resolve() / 'scripts'
    shutil.copytree(SCRIPTS, scripts)
    path = scripts / 'bureau-admission.py'
    code = path.read_text()
    original = '            self.save(state)  # Reservation is durable BEFORE the Build move or writer.'
    assert code.count(original) == 1
    path.write_text(code.replace(original, '            pass  # NEGATIVE CONTROL: no atomic reservation'))
    mutation_caught([sys.executable, str(ROOT/'tests/file_overlap_test.py'),
                     'Fixture.test_simultaneous_contenders_have_exactly_one_admission'],
                    env={'BUREAU_1748_SCRIPTS':str(scripts)},
                    baseline_env={'BUREAU_1748_SCRIPTS':str(SCRIPTS)})
    print('PASS negative control: removing atomic reservation admits both synchronized contenders; single-admission assertion fails')
