"""Exercise the actual shell evidence helpers under errexit and failing I/O."""
import os
from pathlib import Path
import re
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / 'templates/scripts'
MERGE = Path(os.environ.get('BUREAU_TEST_MERGE_SCRIPT', SCRIPTS / 'merge-pipeline.sh'))
CONFIG = Path(os.environ.get('BUREAU_TEST_CONFIG_SCRIPT', SCRIPTS / 'bureau-config.sh'))
REVIEW = Path(os.environ.get('BUREAU_TEST_REVIEW_SCRIPT', SCRIPTS / 'code-review-pipeline.sh'))


def function(path, name):
    match = re.search(r'^' + name + r'\(\) \{\n.*?^\}', path.read_text(), re.M | re.S)
    if match is None: raise AssertionError('missing production function ' + name)
    return match.group()


class CaptureTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='bureau capture ')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def shell(self, source, closed_stderr=False, **env):
        writer = None
        if closed_stderr:
            reader, writer = os.pipe()
            os.close(reader)
        try:
            return subprocess.run(['/bin/bash', '-c', 'set -euo pipefail\n' + source],
                                  env={**os.environ, 'SCRATCH': str(self.root), **env},
                                  stdout=subprocess.PIPE, stderr=writer if closed_stderr else subprocess.PIPE,
                                  text=True, timeout=5)
        finally:
            if writer is not None: os.close(writer)

    def test_optional_shell_warnings_preserve_exits_and_routing_with_closed_stderr(self):
        for name, path, setup, call in (
                ('bureau_evidence', CONFIG, '''BUREAU_RUNTIME=unused
unset _BUREAU_EVIDENCE_WARNED
git() { return 1; }
bureau_without_secrets() { echo injected-evidence-failure >&2; return 1; }
''', 'bureau_evidence evidence-pr; bureau_evidence evidence-pr'),
                ('merge_record_gate', MERGE, '''DRY_RUN=false
BUREAU_RUN_ID=a; BUREAU_STAGE_ATTEMPT_ID=b
mktemp() { return 1; }
''', 'merge_record_gate "" ""')):
            helper = function(path, name)
            for code in (0, 14, 25, 124):
                for closed in (False, True):
                    with self.subTest(helper=name, code=code, closed_stderr=closed):
                        result = self.shell(setup + helper + '\n' + call + '\necho stage-routed\nexit ' + str(code),
                                            closed_stderr=closed, BUREAU_DRY_RUN='0')
                        self.assertEqual(result.returncode, code, 'optional shell warning changed stage exit: ' + str(result.stderr))
                        self.assertEqual(result.stdout.strip(), 'stage-routed', 'optional shell warning skipped routing')
                        if not closed:
                            self.assertEqual(result.stderr.count('WARN:'), 1, 'warning was missing or repeated')

    def test_merge_capture_cleanup_and_sink_failures_preserve_block_exit(self):
        helper = function(MERGE, 'merge_record_gate')
        for fault in ('capture', 'cleanup', 'sink'):
            with self.subTest(fault=fault):
                setup = '''DRY_RUN=false; BUREAU_RUN_ID=a; BUREAU_STAGE_ATTEMPT_ID=b; PR_NUMBER=1
merge_gate_outcome() { echo blocked; }
bureau_evidence() { return 0; }
'''
                if fault == 'capture': setup += 'mktemp() { echo "$SCRATCH"; }\n'
                if fault == 'cleanup': setup += 'rm() { return 1; }\n'
                if fault == 'sink': setup += 'bureau_evidence() { return 1; }\n'
                result = self.shell(setup + helper + '\nmerge_record_gate ' + 'a' * 40 + ' "gate BLOCK"\necho routed-block\nexit 25')
                self.assertEqual(result.returncode, 25, 'optional merge capture changed exit 25: ' + result.stderr)
                self.assertEqual(result.stdout.strip(), 'routed-block')

    def test_merge_capture_redirection_preserves_exits_and_routing_with_closed_stderr(self):
        helper = function(MERGE, 'merge_record_gate')
        for code in (0, 14, 25, 124):
            for closed in (False, True):
                for optional in (False, True):
                    with self.subTest(code=code, closed_stderr=closed, optional=optional):
                        result = self.shell('''DRY_RUN=false; BUREAU_RUN_ID=a; BUREAU_STAGE_ATTEMPT_ID=b; PR_NUMBER=1
# A directory cannot be opened for capture. Bash itself emits the diagnostic.
mktemp() { echo "$SCRATCH"; }
merge_gate_outcome() { echo blocked; }
bureau_evidence() { echo unexpected-evidence; }
''' + helper + '\nmerge_record_gate ' + 'a' * 40 + ' "gate BLOCK"' + (' || true' if optional else '')
                            + '\necho stage-routed\nexit ' + str(code), closed_stderr=closed)
                        self.assertEqual(result.returncode, code,
                                         'optional merge redirection changed stage exit: ' + str(result.stderr))
                        self.assertEqual(result.stdout.strip(), 'stage-routed', 'optional merge redirection skipped routing')

    def test_qa_identity_and_evidence_failures_preserve_test_exit(self):
        helper = function(SCRIPTS / 'qa-pipeline.sh', 'qa_run_gate')
        for code in (0, 14):
            with self.subTest(code=code):
                result = self.shell('''git() { return 1; }
bureau_record_gate() { return 1; }
bureau_untrusted_env() { "$@"; }
TEST_CMD='echo test-ran; exit '"$TEST_EXIT"
''' + helper + '\nqa_run_gate "$SCRATCH/qa.log"\n', TEST_EXIT=str(code))
                self.assertEqual(result.returncode, code, result.stderr)
                self.assertEqual((self.root / 'qa.log').read_text().strip(), 'test-ran')

    def test_review_and_qa_cleanup_failures_preserve_original_exit(self):
        for script, name in ((REVIEW, '_review_cleanup'), (SCRIPTS / 'qa-pipeline.sh', '_qa_cleanup')):
            helper = function(script, name)
            for code in (0, 2, 14, 25, 124):
                for closed in (False, True):
                    with self.subTest(script=script, code=code, closed_stderr=closed):
                        result = self.shell('''REVIEW_TMP="$SCRATCH"; QA_TMP="$SCRATCH"; QA_LOG_PATH="$SCRATCH/log"
touch "$QA_LOG_PATH"
bureau_archive_evidence() { return 1; }
bureau_archive_evidence_dir_log() { return 1; }
rm() { return 1; }
''' + helper + "\ntrap '" + name + " || true; echo cleanup-routed' EXIT\nexit " + str(code), closed_stderr=closed)
                        self.assertEqual(result.returncode, code, result.stderr)
                        self.assertEqual(result.stdout.strip(), 'cleanup-routed', 'optional cleanup warning skipped routing')

    def test_common_identity_pr_and_archive_helpers_are_observational(self):
        helpers = '\n'.join(function(SCRIPTS / 'bureau-config.sh', name) for name in
                            ('bureau_record_gate', 'bureau_archive_evidence', 'bureau_record_pr_url',
                             'bureau_record_branch_pr', 'bureau_observe_waits', 'bureau_sweep_waits'))
        result = self.shell('''BUREAU_RUN_ID=a; BUREAU_STAGE_ATTEMPT_ID=b
git() { return 1; }
gh() { return 1; }
bureau_evidence() { return 1; }
bureau_common_dir() { echo "$SCRATCH"; }
mkdir -p "$SCRATCH/bureau/evidence/v1/waits"
echo private > "$SCRATCH/artifact"
''' + helpers + '''
bureau_record_gate "$SCRATCH/artifact" '' 'test' 14
bureau_archive_evidence "$SCRATCH" 'archive'
bureau_record_pr_url 'https://example.invalid/pull/1'
bureau_record_branch_pr 'feature'
bureau_observe_waits TEAM-1 '{}'
bureau_sweep_waits
echo stage-routed
exit 14
''')
        self.assertEqual(result.returncode, 14, result.stderr)
        self.assertEqual(result.stdout.strip(), 'stage-routed')

    def test_dispatcher_rotates_without_persisting_cursors(self):
        config=self.root/'.bureau.json'; config.write_text('{"linear":{"teams":[]},"agents":{},"repo":{}}')
        result=self.shell('''date() { echo 1000; }
source "$CONFIG_SCRIPT"
bureau_common_dir() { echo "$SCRATCH"; }
bureau_evidence() { printf '%s\\n' "$*"; return 1; }
mkdir -p "$SCRATCH/bureau/evidence/v1/waits"
for n in 1 2 3 4; do bureau_sweep_waits; done
''',CONFIG_SCRIPT=str(CONFIG),BUREAU_CONFIG=str(config))
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(result.stdout.splitlines(),['observe-open --rotation '+str(n) for n in range(4)])


if __name__ == '__main__': unittest.main()
