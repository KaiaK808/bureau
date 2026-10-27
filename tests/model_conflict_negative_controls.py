"""Private-copy mutations and explicit-base qualification of semantic repair."""
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest

ROOT=Path(__file__).resolve().parents[1]


class ModelConflictControls(unittest.TestCase):
    def run_cases(self,scripts,module,*cases):
        return subprocess.run([sys.executable,str(ROOT/'tests'/module),*cases],
            env={**os.environ,'BUREAU_MODEL_CONFLICT_SCRIPTS':str(scripts),'PYTHONDONTWRITEBYTECODE':'1'},
            capture_output=True,text=True,timeout=180)

    def test_bypassing_external_checks_accepts_bad_resolution(self):
        with tempfile.TemporaryDirectory(prefix='bureau-1750-no-check-') as name:
            scripts=Path(name)/'scripts';shutil.copytree(ROOT/'templates/scripts',scripts)
            path=scripts/'bureau-conflicts.py';code=path.read_text()
            needle='    def validate(self, check, scratch):\n'
            self.assertEqual(code.count(needle),1)
            path.write_text(code.replace(needle,needle+'        return  # private negative control\n',1))
            result=self.run_cases(scripts,'model_conflicts_test.py','ModelConflictTests.test_red_check_rolls_back_and_is_human_17')
            self.assertNotEqual(result.returncode,0,result.stdout+result.stderr)
            self.assertIn('AssertionError: 0 != 17',result.stderr)
            self.assertIn('FAILED (failures=1)',result.stderr)
            self.assertNotIn('\nERROR:',result.stderr)
            print('PASS negative control: bypassing the external check incorrectly accepted failing source (0 instead of 17)')

    @unittest.skipUnless(os.environ.get('BUREAU_MODEL_CONFLICT_BASELINE'),'explicit baseline required for build qualification')
    def test_every_new_case_detects_missing_base_behavior(self):
        with tempfile.TemporaryDirectory(prefix='bureau-1750-base-') as name:
            root=Path(name);archive=root/'base.tar'
            with archive.open('wb') as out:
                subprocess.run(['git','-C',str(ROOT),'archive',os.environ['BUREAU_MODEL_CONFLICT_BASELINE'],'templates/scripts'],stdout=out,check=True)
            with tarfile.open(archive) as files: files.extractall(root)
            total=0
            for module in ('model_conflicts_test.py','model_conflict_pipeline_test.py'):
                count=len(re.findall(r'^    def test_', (ROOT/'tests'/module).read_text(),re.M))
                result=self.run_cases(root/'templates/scripts',module)
                (root/(module+'.log')).write_text(result.stdout+result.stderr)
                self.assertNotEqual(result.returncode,0,result.stdout+result.stderr)
                self.assertIn(f'Ran {count} tests',result.stderr)
                self.assertIn(f'FAILED (failures={count})',result.stderr,result.stdout+result.stderr)
                self.assertNotIn('\nERROR:',result.stderr)
                total+=count
            print('PASS baseline: every one of '+str(total)+' new behavior cases failed assertions on the specified base; no test errors')

if __name__=='__main__': unittest.main()
