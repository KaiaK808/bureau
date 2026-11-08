"""Audit disposable Git constructors and the harness's batched copy inventory."""
import importlib
import os
from pathlib import Path
import shutil
import stat
import subprocess
import tempfile
import unittest

import git_maintenance

ROOT = Path(__file__).resolve().parents[1]
HARNESS = ROOT/'tests/lib/harness.sh'


class FixtureHygiene(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='bureau-fixture-hygiene-')
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()

    def shell(self, script, *args):
        return subprocess.run(['/bin/bash', '-euc', script, 'fixture', *map(str, args)],
                              check=True, text=True, capture_output=True, timeout=45)

    def test_shared_python_fixture_constructors_disable_maintenance(self):
        fixtures = (
            ('conflicts_test', 'ConflictTests', 'root'),
            ('dispatcher_test', 'DispatcherFixture', 'root'),
            ('file_overlap_test', 'Fixture', 'root'),
            ('silence_watchdog_test', 'Watchdog', 'root'),
            ('ticket_kill_switch_test', 'WithdrawalFixture', 'root'),
            ('supervision_pipeline_test', 'SupervisionPipelineTests', 'root'),
            ('spec_resume_test', 'SpecWorkerTests', 'root'),
            ('installation_test', 'InstallationTests', 'root'),
            ('installer_sources_test', 'Target', 'root'),
            ('doctor_checks_test', 'Repo', 'base'),
            ('doctor_test', 'DoctorTests', 'repo'),
            ('process_group_test', 'ProcessGroupTests', 'root'),
            ('review_merge_test', 'MergeTests', 'root'),
            ('review_adr_test', 'IndexTests', 'repo'),
            ('gate_publication_test', 'PublicationTests', 'repo'),
            ('ticket_budget_test', 'AdapterFixture', 'root'),
        )
        for module, name, root in fixtures:
            with self.subTest(fixture=module+'.'+name):
                fixture = getattr(importlib.import_module(module), name)()
                try:
                    fixture.setUp()
                    git_maintenance.require_disabled(getattr(fixture, root))
                finally:
                    self.assertTrue(fixture.doCleanups(), 'fixture cleanup failed')

    def test_shared_shell_fixture_constructors_disable_maintenance(self):
        for helper, setup in (('harness.sh', 'sandbox_init fixture-issue fixture-branch'),
                              ('pr5-interrupt.sh', 'pr5_setup; pr5_new_repo')):
            with self.subTest(helper=helper):
                # Override removal only while inspecting; the enclosing tempfile
                # owns these directories, and constructors start no workers.
                result = self.shell('export TMPDIR="$2"; source "$1"; '+setup+
                                    '; trap - EXIT; printf "%s\\n" "${SB:-$SANDBOX}"',
                                    ROOT/'tests/lib'/helper, self.root)
                git_maintenance.require_disabled(Path(result.stdout.strip().splitlines()[-1]))

    def test_maintenance_guard_rejects_an_unconfigured_working_or_bare_fixture(self):
        for bare in (False, True):
            with self.subTest(bare=bare), tempfile.TemporaryDirectory(dir=self.root) as directory:
                repo = Path(directory)/'repository with spaces'
                subprocess.run(['git', 'init', '--template=', '-q', *(['--bare'] if bare else []), str(repo)],
                               check=True, timeout=5)
                with self.assertRaisesRegex(AssertionError, 'automatic maintenance enabled'):
                    git_maintenance.require_disabled(repo)
                git_maintenance.disable(repo)
                git_maintenance.require_disabled(repo)

    def test_clone_configuration_is_persisted_and_its_absence_is_detected(self):
        origin = self.root/'origin.git'
        subprocess.run(['git', 'init', '-q', '--bare', str(origin)], check=True, timeout=5)
        git_maintenance.disable(origin)
        for bare in (False, True):
            for configured in (False, True):
                with self.subTest(bare=bare, configured=configured):
                    clone = self.root/f'clone-{bare}-{configured}'
                    subprocess.run(['git', 'clone', '-q', *(['--bare'] if bare else []),
                                    *(git_maintenance.CLONE_CONFIG if configured else ()), str(origin), str(clone)],
                                   check=True, capture_output=True, timeout=5)
                    if configured:
                        git_maintenance.require_disabled(clone)
                    else:
                        with self.assertRaisesRegex(AssertionError, 'automatic maintenance enabled'):
                            git_maintenance.require_disabled(clone)

    @staticmethod
    def inventory(root):
        return {str(path.relative_to(root)): (stat.S_IMODE(path.lstat().st_mode),
                os.readlink(path) if path.is_symlink() else path.read_bytes())
                for path in root.rglob('*') if path.is_symlink() or path.is_file()}

    def assert_inventory(self, expected, actual):
        self.assertEqual(self.inventory(expected), self.inventory(actual), 'copied inventory changed')

    def copied_inventory(self):
        source = self.root/'source'; scripts = source/'templates/scripts'
        shutil.copytree(ROOT/'templates/scripts', scripts, symlinks=True)
        (scripts/'directory').mkdir()
        (scripts/'executable with spaces').write_text('fixture\n')
        (scripts/'executable with spaces').chmod(0o751)
        (scripts/'file-link').symlink_to('executable with spaces')
        (scripts/'directory-link').symlink_to('directory', target_is_directory=True)
        (scripts/'broken-link').symlink_to('missing')
        (scripts/'.hidden').write_text('not in the shell glob\n')
        old = self.root/'old'; new = self.root/'new'; old.mkdir(); new.mkdir()
        self.shell('for f in "$1/templates/scripts/"*; do '
                   '[ ! -f "$f" ] || cp "$f" "$2/"; done', source, old)
        calls = self.root/'copy-calls'
        self.shell('source "$1"; trap - EXIT; REPO_ROOT="$2"; SCRIPTS_DIR="$3"; '
                   'COPY_CALLS="$4"; cp() { printf "copy\\n" >> "$COPY_CALLS"; command cp "$@"; }; '
                   'copy_runtime_scripts', HARNESS, source, new, calls)
        self.assertEqual(['copy'], calls.read_text().splitlines(), 'copy was not batched')
        return old, new

    def test_batched_copy_matches_old_paths_modes_and_symlinks(self):
        self.assert_inventory(*self.copied_inventory())

    def test_inventory_guard_rejects_missing_files_modes_and_symlink_changes(self):
        old, new = self.copied_inventory()
        target = new/'executable with spaces'
        mode = target.stat().st_mode
        target.chmod(0o600)
        with self.assertRaisesRegex(AssertionError, 'copied inventory changed'):
            self.assert_inventory(old, new)
        target.chmod(stat.S_IMODE(mode))
        link = new/'file-link'; link.unlink(); link.symlink_to(target.name)
        with self.assertRaisesRegex(AssertionError, 'copied inventory changed'):
            self.assert_inventory(old, new)
        link.unlink(); shutil.copy(old/'file-link', link)
        target.unlink()
        with self.assertRaisesRegex(AssertionError, 'copied inventory changed'):
            self.assert_inventory(old, new)


if __name__ == '__main__': unittest.main(verbosity=2)
