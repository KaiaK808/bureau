#!/usr/bin/env python3
"""Restore each pre-fix admission behavior in an isolated runtime copy."""
from pathlib import Path
import shutil
import sys
import tempfile

from file_overlap_test import ROOT, SCRIPTS
from mutation_checks import mutation_caught, snapshot_files

sys.dont_write_bytecode = True

controls = (
    ('test_admission_ignores_unrelated_project_title_edit', (
        ('snapshot = relevant_issues(snapshot, projects, retained)', 'snapshot = dict(snapshot)'),
        ('latest = relevant_issues(latest, projects, latest_retained)', 'latest = dict(latest)'),
        ('if admission_identity(latest) != admission_identity(snapshot):', 'if latest != snapshot:'),
    ), ('27 != 0', 'Linear snapshot changed during admission; retry')),
    ('test_shepherd_admission_retries_moved_snapshot', (
        ('            tries = 3\n',
         "            tries = 3 if os.environ.get('BUREAU_DISPATCH_LANE') == '1' else 1\n"),
    ), ('27 != 0', 'Linear snapshot changed during admission; retry')),
    ('test_admission_cross_project_canonical_only_pr_remains_holder', (
        ("pr_scopes(self.repo, listed, canonical, state['inventories'], canonical_complete)",
         "pr_scopes(self.repo, listed, canonical, state['inventories'])"),
    ), ('0 != 29',)),
    ('test_admission_comment_pagination_lease_race_retries_into_overlap_wait', (
        ('latest = relevant_issues(latest, projects, latest_retained)',
         'latest = relevant_issues(latest, projects, retained)'),
    ), ('SnapshotMoved not raised',)),
)

with tempfile.TemporaryDirectory(prefix='bureau-admission-negative-') as directory:
    scripts = Path(directory).resolve() / 'scripts'
    shutil.copytree(SCRIPTS, scripts)
    originals = snapshot_files(scripts)
    path = scripts / 'bureau-admission.py'
    original = path.read_text()
    for name, replacements, diagnostics in controls:
        code = original
        for before, after in replacements:
            assert code.count(before) == 1, before
            code = code.replace(before, after)
        path.write_text(code)
        result = mutation_caught(
            [sys.executable, str(ROOT / 'tests/file_overlap_test.py'), 'Fixture.' + name],
            env={'BUREAU_OVERLAP_TEST_SCRIPTS': str(scripts)}, originals=originals,
            expected=diagnostics, expected_exit=1)
        assert result.returncode == 1, result.stdout + result.stderr
        assert 'FAIL: ' + name in result.stderr, result.stdout + result.stderr
        for diagnostic in diagnostics:
            assert diagnostic in result.stderr, result.stdout + result.stderr
        print('PASS negative control: restoring pre-fix behavior fails ' + name + ' with the expected assertion')
