"""Private-copy mutations and explicit-base qualification of semantic repair."""
import os
from pathlib import Path
import re
import shutil
import sys
import tempfile
import unittest

from mutation_checks import historical_scripts, mutation_caught, snapshot_files

ROOT=Path(__file__).resolve().parents[1]
TIMEOUT = float(os.environ.get('BUREAU_MODEL_CONFLICT_CONTROL_TIMEOUT', '300'))


class ModelConflictControls(unittest.TestCase):
    def run_cases(self,scripts,module,*cases,originals):
        return mutation_caught([sys.executable,str(ROOT/'tests'/module),*cases],
            env={'BUREAU_MODEL_CONFLICT_SCRIPTS':str(scripts)},
            originals=originals,timeout=TIMEOUT)

    def test_bypassing_external_checks_accepts_bad_resolution(self):
        with tempfile.TemporaryDirectory(prefix='bureau-1750-no-check-') as name:
            scripts=Path(name).resolve()/'scripts';shutil.copytree(ROOT/'templates/scripts',scripts)
            originals=snapshot_files(scripts)
            path=scripts/'bureau-conflicts.py';code=path.read_text()
            needle='    def validate(self, check, scratch):\n'
            self.assertEqual(code.count(needle),1)
            path.write_text(code.replace(needle,needle+'        return  # private negative control\n',1))
            self.run_cases(scripts,'model_conflicts_test.py','ModelConflictTests.test_red_check_rolls_back_and_is_human_17',
                           originals=originals)
            print('PASS negative control: bypassing the external check incorrectly accepted failing source (0 instead of 17)')

    @unittest.skipUnless(os.environ.get('BUREAU_MODEL_CONFLICT_BASELINE'),'explicit baseline required for build qualification')
    def test_every_new_case_detects_missing_base_behavior(self):
        with historical_scripts(ROOT, os.environ['BUREAU_MODEL_CONFLICT_BASELINE']) as (scripts, originals):
            total=0
            for module in ('model_conflicts_test.py','model_conflict_pipeline_test.py'):
                group='ModelConflictTests' if module=='model_conflicts_test.py' else 'ConflictPipelineTests'
                cases=re.findall(r'^    def (test_\w+)\(', (ROOT/'tests'/module).read_text(),re.M)
                output=''
                for case in cases:
                    result=self.run_cases(scripts,module,group+'.'+case,originals=originals)
                    output+=result.stdout+result.stderr
                (scripts.parent/(module+'.log')).write_text(output)
                total+=len(cases)
            print('PASS baseline: every one of '+str(total)+' new behavior cases failed or errored on the specified base')

if __name__=='__main__': unittest.main()
