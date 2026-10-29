"""Replay the new fault injections against a locally available source release."""
import argparse
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]


def reject(label, script, selection, variable, source, signatures):
    result = subprocess.run([sys.executable, str(ROOT / 'tests' / script), '-v', *selection], cwd=ROOT,
                            env={**os.environ, 'PYTHONDONTWRITEBYTECODE': '1', variable: str(source)},
                            capture_output=True, text=True, timeout=90)
    output = result.stdout + result.stderr
    summary = re.search(r'^FAILED \([^\n]+\)$', output, re.M)
    if result.returncode != 1 or summary is None or not any(value in output for value in signatures):
        sys.exit(label + ': expected regression was absent\n' + output)
    print(label + ': rejected (' + summary[0] + ')')


def mutations():
    cases = [
        ('owned zombie reaping', 'bureau-process.py',
         'reap=child.poll', 'reap=None',
         'test_signal_reprobe_reaps_owned_zombie_before_deadline', 'TerminationUncertain'),
        ('asynchronous exit without grace', 'bureau-process.py',
         '_signal_group(group, 0, wait_for_exit=True)', '_signal_group(group, 0)',
         'test_successful_probe_reprobe_esrch_clears_gate_hold', 'GateExecutionError'),
        ('zombie reaping without re-probe', 'bureau-process.py',
         '        if exc.errno != errno.EPERM: raise\n', '        raise\n',
         'test_zombie_eperm_reprobe_esrch_clears_gate_hold', 'GateExecutionError'),
        ('EPERM never green', 'bureau-process.py',
         "raise TerminationUncertain('Process group termination uncertain: ' + str(exc)) from exc", 'return',
         'test_outside_group_eperm_at_signal_timeout_and_cleanup', 'GateExecutionError not raised'),
        ('ownership retained', 'bureau-process.py', '    held = []\n', '    return []\n    held = []\n',
         'test_uncertain_gate_keeps_exact_ownership_and_worktree', 'Conflict not raised'),
        ('registration ownership', 'bureau-runtime.py', 'self.failed_registration = run', 'pass',
         'test_registration_failure_with_unreaped_child_keeps_leases', 'Items in the second set'),
        ('hold survives restart and failed later writes', 'bureau-dispatch.py',
         'hold.close(stopped=child is not None and self.termination_failure is None)', 'hold.close(stopped=True)',
         'test_dispatch_escalation_eperm_retains_cancellation_and_clears_handle', 'is not true'),
        ('ESRCH stays clean', 'bureau-process.py',
         'try: os.killpg(group, signum)\n    except ProcessLookupError: return True',
         "try: os.killpg(group, signum)\n    except ProcessLookupError as exc: raise TerminationUncertain('injected ESRCH rejection') from exc",
         'test_outside_esrch_preserves_success_timeout_and_cancellation_and_clears_hold', 'GateExecutionError'),
        ('worker worktree preserved', 'bureau-worker.sh',
         '  python3 -I "$SCRIPT_DIR/bureau-process.py" --root "$common/bureau" --run "${BUREAU_RUN_ID:-}" || exit "$rc"',
         '  :', 'test_worker_cleanup_keeps_uncertain_gate_branch_and_registration', "'' != 'fixture-work'"),
    ]
    for label, name, old, new, test, signature in cases:
        with tempfile.TemporaryDirectory(prefix='bureau process mutation ') as directory:
            scripts = Path(directory).resolve() / 'scripts'
            shutil.copytree(ROOT / 'templates/scripts', scripts)
            file = scripts / name
            source = file.read_text()
            if source.count(old) != 1: sys.exit(label + ': mutation anchor changed')
            file.write_text(source.replace(old, new, 1))
            reject(label, 'process_group_test.py', ['ProcessGroupTests.' + test],
                   'BUREAU_TEST_GROUP_SCRIPTS', scripts, (signature,))
    with tempfile.TemporaryDirectory(prefix='bureau conflict hold mutation ') as directory:
        scripts = Path(directory).resolve() / 'scripts'
        shutil.copytree(ROOT / 'templates/scripts', scripts)
        file = scripts / 'bureau-conflicts.py'
        file.write_text(file.read_text().replace('if self.termination_uncertain:', 'if False:', 1))
        reject('uncertain conflict checkout preserved', 'conflicts_test.py',
               ['ConflictTests.test_uncertain_validation_keeps_merge_index_and_worktree'],
               'BUREAU_CONFLICT_TEST_SCRIPTS', scripts, ('MERGE_HEAD',))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--ref', default='v3.5.0')
    parser.add_argument('--mutations', action='store_true')
    args = parser.parse_args()
    if args.mutations:
        mutations()
        return
    commit = subprocess.check_output(['git', 'rev-parse', '--verify', args.ref + '^{commit}'], cwd=ROOT, text=True).strip()
    files = subprocess.check_output(['git', 'ls-tree', '-r', '--name-only', commit, 'templates/scripts'], cwd=ROOT, text=True).splitlines()
    with tempfile.TemporaryDirectory(prefix='bureau process group control ') as directory:
        scripts = Path(directory).resolve()
        for name in files:
            target = scripts / Path(name).relative_to('templates/scripts')
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(subprocess.check_output(['git', 'show', commit + ':' + name], cwd=ROOT))
        cases = [
            ('gate responses', 'provider_adapter_test.py',
             ['ProviderTests.test_machine_gate_eperm_never_passes_even_with_empty_inspection'],
             'BUREAU_TEST_PROVIDER_SCRIPT', scripts / 'bureau-provider.py', ('True is not false', 'outside gate termination uncertain', '0 != 25', '22 != 25')),
        ]
        for label, script, selection, variable, source, signature in cases:
            signatures = (signature,) if isinstance(signature, str) else signature
            reject(label + ' against ' + args.ref + ' (' + commit[:12] + ')', script, selection, variable, source, signatures)


if __name__ == '__main__': main()
