"""Replay the new fault injections against a locally available source release."""
import argparse
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--ref', default='v3.5.0')
    args = parser.parse_args()
    commit = subprocess.check_output(['git', 'rev-parse', '--verify', args.ref + '^{commit}'], cwd=ROOT, text=True).strip()
    files = subprocess.check_output(['git', 'ls-tree', '-r', '--name-only', commit, 'templates/scripts'], cwd=ROOT, text=True).splitlines()
    with tempfile.TemporaryDirectory(prefix='bureau process group control ') as directory:
        scripts = Path(directory).resolve()
        for name in files:
            target = scripts / Path(name).relative_to('templates/scripts')
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(subprocess.check_output(['git', 'show', commit + ':' + name], cwd=ROOT))
        cases = [
            ('process groups', 'process_group_test.py', [], 'BUREAU_TEST_GROUP_SCRIPTS', scripts, 'PermissionError'),
            ('gate responses', 'provider_adapter_test.py',
             ['ProviderTests.test_machine_gate_exited_leader_with_live_or_uninspectable_descendants_never_passes'],
             'BUREAU_TEST_PROVIDER_SCRIPT', scripts / 'bureau-provider.py', ('True is not false', 'outside gate termination uncertain')),
        ]
        for label, script, selection, variable, source, signature in cases:
            result = subprocess.run([sys.executable, str(ROOT / 'tests' / script), '-v', *selection], cwd=ROOT,
                                    env={**os.environ, 'PYTHONDONTWRITEBYTECODE': '1', variable: str(source)},
                                    capture_output=True, text=True, timeout=60)
            output = result.stdout + result.stderr
            summary = re.search(r'^FAILED \([^\n]+\)$', output, re.M)
            signatures = (signature,) if isinstance(signature, str) else signature
            if result.returncode != 1 or summary is None or not any(value in output for value in signatures):
                sys.exit(label + ': expected regression was absent\n' + output)
            print(label + ': rejected ' + args.ref + ' (' + commit[:12] + '), ' + summary[0])


if __name__ == '__main__': main()
