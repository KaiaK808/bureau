"""Expected assertion failures against the base and one regeneration mutation."""
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest

from mutation_checks import mutation_caught

ROOT = Path(__file__).resolve().parents[1]


class NegativeControls(unittest.TestCase):
    def check_withdrawal_failure(self, scripts, case):
        return self.check_failure(scripts, [case], 'conflicts_withdrawal_test.py', 'ConflictWithdrawal')

    @unittest.skipUnless(os.environ.get('BUREAU_CONFLICT_WITHDRAWAL_BASELINE'), 'explicit pre-integration ref required')
    def test_pre_integration_bypasses_withdrawal(self):
        with tempfile.TemporaryDirectory(prefix='bureau-conflicts-withdrawal-base-') as directory:
            root = Path(directory).resolve(); archive = root / 'base.tar'
            baseline = os.environ['BUREAU_CONFLICT_WITHDRAWAL_BASELINE']
            with archive.open('wb') as output:
                subprocess.run(['git', '-C', str(ROOT), 'archive', baseline, 'templates/scripts'], stdout=output, check=True)
            with tarfile.open(archive) as files: files.extractall(root)
            for case in ('test_withdrawal_before_transaction', 'test_throttled_failed_generator_respects_withdrawal'):
                self.check_withdrawal_failure(root / 'templates/scripts', case)
            print('PASS pre-integration control: fetch and throttled-failure withdrawal assertions fail on ' + baseline)

    def test_bypass_command_checkpoints_breaks_rollback_and_step_boundary(self):
        with tempfile.TemporaryDirectory(prefix='bureau-conflicts-withdrawal-mutation-') as directory:
            scripts = Path(directory).resolve() / 'scripts'; shutil.copytree(ROOT / 'templates/scripts', scripts)
            path = scripts / 'bureau-conflicts.py'; source = path.read_text()
            self.assertEqual(source.count('self.checkpoint()'), 10)
            path.write_text(source.replace('self.checkpoint()', 'None  # private mutation: no command checkpoint'))
            self.check_withdrawal_failure(scripts, 'test_withdrawal_before_dependencies_and_between_commands')
            print('PASS command-boundary control: removing transaction checkpoints starts later steps or commits withdrawn work')

    def check_failure(self, scripts, cases, module='conflicts_test.py', group='ConflictTests'):
        output = ''
        for case in cases:
            result = mutation_caught([sys.executable, str(ROOT/'tests'/module), group+'.'+case],
                env={'BUREAU_CONFLICT_TEST_SCRIPTS': str(scripts)},
                baseline_env={'BUREAU_CONFLICT_TEST_SCRIPTS': str(ROOT/'templates/scripts')},timeout=None)
            output += result.stdout + result.stderr
        return output

    @unittest.skipUnless(os.environ.get('BUREAU_CONFLICT_BASELINE'), 'explicit baseline ref required; shallow CI need not retain build history')
    def test_base_lacks_configured_repairs(self):
        with tempfile.TemporaryDirectory(prefix='bureau-conflicts-base-') as directory:
            root = Path(directory).resolve(); archive = root / 'base.tar'
            baseline = os.environ['BUREAU_CONFLICT_BASELINE']
            with archive.open('wb') as output:
                subprocess.run(['git', '-C', str(ROOT), 'archive', baseline, 'templates/scripts'], stdout=output, check=True)
            with tarfile.open(archive) as files: files.extractall(root)
            cases = ['test_generated_only', 'test_append_only_under_unreleased',
                     'test_combined_sources_precede_regeneration', 'test_pinned_base_is_not_refetched',
                     'test_append_only_unusual_filename', 'test_generated_unusual_filename_and_argv',
                     'test_append_repair_also_regenerates_nonconflicted_outputs', 'test_check_command_process_is_reaped',
                     'test_recursive_globs_and_new_parent_directories', 'test_generator_and_check_use_untrusted_environment',
                     'test_append_filename_is_not_a_git_pathspec']
            self.check_failure(root / 'templates/scripts', cases)
            print(f'PASS base control: all {len(cases)} successful configured-repair assertions fail on ' + baseline)
            for case in ('published', 'unpublished'):
                mutation_caught(['/bin/bash', str(ROOT / 'tests/test_conflicts_worker.sh')],
                    env={'BUREAU_CONFLICT_TEST_SCRIPTS': str(root / 'templates/scripts'),
                         'BUREAU_CONFLICT_WORKER_CASE': case},
                    baseline_env={'BUREAU_CONFLICT_TEST_SCRIPTS': str(ROOT/'templates/scripts'),
                                  'BUREAU_CONFLICT_WORKER_CASE': case},timeout=None)
            print('PASS base control: both worker success assertions fail before the provider on ' + baseline)

    def test_bypass_regeneration_breaks_success_assertion(self):
        with tempfile.TemporaryDirectory(prefix='bureau-conflicts-mutation-') as directory:
            scripts = Path(directory).resolve() / 'scripts'; shutil.copytree(ROOT / 'templates/scripts', scripts)
            path = scripts / 'bureau-conflicts.py'; source = path.read_text()
            anchor = "            self.run(rule['command'])"
            self.assertEqual(source.count(anchor), 1)
            path.write_text(source.replace(anchor, '            pass  # private negative control: bypass regeneration'))
            self.check_failure(scripts, ['test_generated_only'])
            print('PASS negative control: bypassing regeneration leaves invalid old output; the successful-resolution assertion fails')


if __name__ == '__main__': unittest.main(verbosity=2)
