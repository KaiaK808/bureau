#!/usr/bin/env python3
"""Check that release documentation edits left the tested asset trees unchanged."""
import argparse
import os
import subprocess
import sys

EXCLUDED = {b'scripts/release_verify.py', b'tests/test_release_verify.sh'}


def git(*args):
    return subprocess.run(['git', *args], check=True, capture_output=True, timeout=30).stdout


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('tested_ref', help='release branch head that ran the full macOS suite')
    parser.add_argument('release_ref', help='candidate or tagged release commit')
    args = parser.parse_args()
    try:
        trees = [git('rev-parse', '--verify', '--end-of-options', ref+'^{tree}').decode().strip()
                 for ref in (args.tested_ref, args.release_ref)]
        changed = git('diff', '--no-ext-diff', '--no-textconv', '--no-renames', '--no-relative',
                      '--ignore-submodules=none', '--name-only', '-z', *trees, '--')
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
        detail = str(error)
        if isinstance(error, subprocess.CalledProcessError):
            detail = error.stderr.decode(errors='replace').strip()
        print('Release verification failed: '+detail, file=sys.stderr)
        return 2
    paths = [path for path in changed.split(b'\0')
             if path.startswith((b'templates/', b'tests/', b'scripts/')) and path not in EXCLUDED]
    if paths:
        print('Release verification failed; differing paths:')
        for path in paths:
            print('  '+os.fsdecode(path))
        return 1
    print('PASS templates/, tests/ and scripts/ are tree-identical '
          '(release verifier and its test excluded)')
    return 0


if __name__ == '__main__':
    sys.exit(main())
