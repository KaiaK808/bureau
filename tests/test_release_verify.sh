#!/bin/bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PYTHONDONTWRITEBYTECODE=1 python3 - "$ROOT" <<'PY'
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(sys.argv.pop()).resolve()
sys.path.insert(0, str(ROOT/'tests'))
import git_maintenance


class ReleaseVerifyTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='.release-verify-test-', dir=ROOT)
        self.addCleanup(temp.cleanup)
        self.repo = Path(temp.name)/'repository with spaces'
        self.env = dict(os.environ, GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL=os.devnull,
                        **git_maintenance.OFF_ENV)
        subprocess.run(['git', 'init', '--template=', '-q', str(self.repo)],
                       check=True, capture_output=True, env=self.env, timeout=10)
        git_maintenance.disable(self.repo)
        self.git('config', 'user.name', 'Release Fixture')
        self.git('config', 'user.email', 'fixture@example.invalid')
        for name in ('templates/runtime.sh', 'tests/test_runtime.sh', 'scripts/check.py',
                     'docs/release.md', 'scripts/release_verify.py', 'tests/test_release_verify.sh'):
            self.write(name, 'baseline\n')
        self.base = self.commit()

    def git(self, *args):
        return subprocess.run(['git', '-C', str(self.repo), *args], check=True,
                              text=True, capture_output=True, env=self.env, timeout=10).stdout.strip()

    def write(self, name, content):
        path = self.repo/name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)

    def commit(self):
        self.git('add', '-A')
        self.git('-c', 'commit.gpgSign=false', 'commit', '-qm', 'fixture', '--allow-empty')
        return self.git('rev-parse', 'HEAD')

    def verify(self, first, second, expected, cwd=None):
        result = subprocess.run([sys.executable, str(ROOT/'scripts/release_verify.py'), first, second],
                                cwd=cwd or self.repo, env=self.env, text=True, capture_output=True, timeout=15)
        self.assertEqual(expected, result.returncode, result.stdout+result.stderr)
        return result.stdout+result.stderr

    def test_identical_trees_pass(self):
        self.assertIn('PASS', self.verify(self.base, self.commit(), 0))

    def test_changed_template_fails_and_names_path(self):
        self.write('templates/runtime.sh', 'changed\n')
        self.assertIn('templates/runtime.sh', self.verify(self.base, self.commit(), 1))

    def test_literal_pathspec_environment_cannot_hide_changes(self):
        self.write('templates/runtime.sh', 'changed\n')
        release = self.commit()
        self.env['GIT_LITERAL_PATHSPECS'] = '1'
        self.assertIn('templates/runtime.sh', self.verify(self.base, release, 1))

    def test_docs_only_change_passes(self):
        self.write('docs/release.md', 'release documentation\n')
        self.assertIn('PASS', self.verify(self.base, self.commit(), 0))

    def test_scripts_and_tests_are_checked(self):
        self.write('scripts/check.py', 'changed script\n')
        self.write('tests/test_runtime.sh', 'changed test\n')
        output = self.verify(self.base, self.commit(), 1)
        self.assertIn('scripts/check.py', output)
        self.assertIn('tests/test_runtime.sh', output)

    def test_subdirectory_and_relative_diff_configuration(self):
        self.git('config', 'diff.relative', 'true')
        self.write('templates/runtime.sh', 'changed\n')
        output = self.verify(self.base, self.commit(), 1, cwd=self.repo/'docs')
        self.assertIn('templates/runtime.sh', output)

    def test_additions_deletions_and_modes_are_checked(self):
        self.write('templates/new file.sh', 'new\n')
        (self.repo/'tests/test_runtime.sh').unlink()
        (self.repo/'scripts/check.py').chmod(0o755)
        output = self.verify(self.base, self.commit(), 1)
        for name in ('templates/new file.sh', 'tests/test_runtime.sh', 'scripts/check.py'):
            self.assertIn(name, output)

    def test_only_verifier_and_its_test_are_excluded(self):
        self.write('scripts/release_verify.py', 'new verifier\n')
        self.write('tests/test_release_verify.sh', 'new verifier test\n')
        self.assertIn('PASS', self.verify(self.base, self.commit(), 0))

    def test_invalid_ref_fails(self):
        for refs in (('missing-ref', self.base), (self.base, 'missing-ref')):
            with self.subTest(refs=refs):
                self.assertIn('Release verification failed', self.verify(*refs, 2))


unittest.main()
PY
