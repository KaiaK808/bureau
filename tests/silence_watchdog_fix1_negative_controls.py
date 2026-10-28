"""Restore each review defect privately and require its real regression to fail."""
import hashlib
from pathlib import Path
import shutil
import sys
import tempfile

from mutation_checks import mutation_caught, snapshot_files

ROOT=Path(__file__).resolve().parents[1]
SCRIPTS=ROOT/'templates/scripts'


def replace_once(source, before, after):
    assert source.count(before)==1, 'control anchor changed: '+before
    return source.replace(before,after,1)


def argv_only(source):
    start=source.index('    def tool_alive(')
    end=source.index('    def tool_finished(',start)
    return source[:start]+'''    def tool_alive(self, command, key):
        return any(pid != self.root and not row[2] and command_matches(command, self.table.args_env(pid)[0])
                   for pid,row in self.current.items())

'''+source[end:]


controls=[
    ('argv-only liveness','bureau-activity.py',argv_only,
     'ProcessIdentity.test_exec_and_observed_descendants_keep_the_same_tool_identity',
     'exec lost the identified tool process'),
    ('background result closes ownership','bureau-activity.py',
     lambda source:replace_once(source,"if block['tool_use_id'] not in self.background:", 'if True:'),
     'ProviderRegressions.test_background_taskoutput_keeps_child_allowance',
     'background Bash lost its tool allowance'),
    ('watchdog-off stream failure discards success','bureau-provider.py',
     lambda source:replace_once(source,"if stream_error and options.get('silence_timeout_seconds'):", 'if stream_error:'),
     'ProviderRegressions.test_watchdog_off_inherited_stdout_preserves_success',
     'watchdog-off pipe observation discarded success'),
    ('withdrawal latch cancels siblings','code-review-pipeline.sh',
     lambda source:replace_once(source,'    # Withdrawal waits for every current call, including inactive\'s exit 25.',
         '''    if [ -n "${BUREAU_TICKET_CONTROL_DIR:-}" ]; then
      for rc in 0 25 26; do
        [ ! -f "$BUREAU_TICKET_CONTROL_DIR/stop-$rc" ] || : > "$REVIEW_TMP/cancel-specialists"
      done
    fi
    # Withdrawal waits for every current call, including inactive's exit 25.'''),
     'ReviewWithdrawal.test_done_waits_for_current_calls_with_watchdog_off',
     'withdrawal cancelled an in-flight security call'),
    ('inactive exit 25 is mistaken for silence','code-review-pipeline.sh',
     lambda source:replace_once(source,' && [ -f "$REVIEW_TMP/cancel-specialists.${phases[$i]}.silence" ]',''),
     'ReviewWithdrawal.test_inactive_waits_for_current_calls_with_watchdog_off',
     'withdrawal cancelled an in-flight security call'),
]

before={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in SCRIPTS.iterdir() if p.is_file()}
for name, filename, mutate, test, expected in controls:
    with tempfile.TemporaryDirectory(prefix='bureau-1723-fix1-control-') as scratch:
        target=Path(scratch).resolve()/'scripts'; shutil.copytree(SCRIPTS,target)
        originals=snapshot_files(target)
        source=target/filename; source.write_text(mutate(source.read_text()))
        mutation_caught([sys.executable,str(ROOT/'tests/silence_watchdog_fix1_test.py'),test],
            env={'BUREAU_TEST_PROVIDER_SCRIPT':str(target/'bureau-provider.py'),
                 'BUREAU_TEST_SILENCE_SCRIPTS':str(target)},
            originals=originals,timeout=65,expected=expected)
        print('PASS negative control: '+name,flush=True)
assert before=={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in SCRIPTS.iterdir() if p.is_file()}
print('PASS negative controls: runtime source hashes unchanged')
