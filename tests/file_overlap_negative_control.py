#!/usr/bin/env python3
"""Remove the durable atomic reservation in a private runtime copy."""
from pathlib import Path
import shutil
import sys
import tempfile

sys.dont_write_bytecode = True
from file_overlap_test import Fixture, SCRIPTS

with tempfile.TemporaryDirectory(prefix='bureau-1748-negative-', dir='/private/tmp') as scratch:
    scripts = Path(scratch) / 'scripts'
    shutil.copytree(SCRIPTS, scripts)
    path = scripts / 'bureau-admission.py'
    code = path.read_text()
    original = '            self.save(state)  # Reservation is durable BEFORE the Build move or writer.'
    assert code.count(original) == 1
    path.write_text(code.replace(original, '            pass  # NEGATIVE CONTROL: no atomic reservation'))
    fixture = Fixture('test_simultaneous_contenders_have_exactly_one_admission')
    try:
        fixture.setUp()
        result = fixture.simultaneous(scripts)
        assert sorted(v[0] for v in result) == [0, 0], result
        try:
            fixture.assertEqual(sorted(v[0] for v in result), [0, 29])
        except AssertionError:
            print('PASS negative control: removing atomic reservation admits both synchronized contenders; single-admission assertion fails')
        else:
            raise AssertionError('negative control did not detect missing reservation')
    finally:
        fixture.doCleanups()
