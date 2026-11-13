#!/usr/bin/env python3
"""Remove reservation and path-shape guards in private runtime copies."""
from pathlib import Path
import shutil
import sys
import tempfile

from mutation_checks import mutation_caught, snapshot_files

sys.dont_write_bytecode = True
from file_overlap_test import ROOT, SCRIPTS

with tempfile.TemporaryDirectory(prefix='bureau-1748-negative-') as scratch:
    scripts = Path(scratch).resolve() / 'scripts'
    shutil.copytree(SCRIPTS, scripts)
    originals = snapshot_files(scripts)
    path = scripts / 'bureau-admission.py'
    code = path.read_text()
    original = '            self.save(state)  # Reservation is durable BEFORE the Build move or writer.'
    assert code.count(original) == 1
    path.write_text(code.replace(original, '            pass  # NEGATIVE CONTROL: no atomic reservation'))
    mutation_caught([sys.executable, str(ROOT/'tests/file_overlap_test.py'),
                     'Fixture.test_simultaneous_contenders_have_exactly_one_admission'],
                    env={'BUREAU_OVERLAP_TEST_SCRIPTS':str(scripts)},
                    originals=originals, expected='AssertionError: Lists differ: [0, 0] != [0, 29]', expected_exit=1)
    print('PASS negative control: removing atomic reservation admits both synchronized contenders; single-admission assertion fails')
    path.write_bytes(originals[path])
    path = scripts / 'bureau-paths.py'
    code = path.read_text()
    original = '        if code and not ('
    assert code.count(original) == 1
    path.write_text(code.replace(original, '        if False and not ('))
    mutation_caught([sys.executable, str(ROOT/'tests/file_overlap_test.py'),
                     'Fixture.test_nonpath_code_spans_admit_next_to_disjoint_holder'],
                    env={'BUREAU_OVERLAP_TEST_SCRIPTS':str(scripts)}, originals=originals,
                    expected=('29 != 0', 'unknown scope (repository-wide)'), expected_exit=1)
    print('PASS negative control: treating non-path code as paths blocks disjoint admission with exit 29')
