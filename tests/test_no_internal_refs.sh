#!/bin/bash
# Keep private ticket provenance out of public templates, prose and test comments.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
T=$(mktemp -d -t bureau-test.internal-refs.XXXXXXXX)
trap 'rm -rf "$T"' EXIT
fail() { echo "FAIL: $*" >&2; exit 1; }

cat > "$T/scan.py" <<'PY'
from pathlib import Path
import re
import subprocess
import sys
import tempfile

KEY = re.compile(r'EXP-[0-9]')


def matches(line, comments_only):
    return bool(KEY.search(line)) and (not comments_only or line.lstrip().startswith('#'))


if sys.argv[1] == '--self-check':
    key = 'EXP-'  # Build synthetic matcher fixtures without literal private keys.
    lines = [
        '# provenance ' + key + str(1),
        '  # indented comment ' + key + str(7),
        '\t# Python comment ' + key + str(19),
        'ISSUE="' + key + str(7) + '"',
        '{"identifier":"' + key + str(910) + '"}',
        '# example TEAM-123',
        'echo "TEAM-123"',
        '# EXP- is a prefix; EXP-example has no digit',
    ]
    for comments_only, expected in ((False, [1, 2, 3, 4, 5]), (True, [1, 2, 3])):
        found = [n for n, line in enumerate(lines, 1) if matches(line, comments_only)]
        if found != expected:
            print(f'matcher self-check: comments_only={comments_only}, expected {expected}, got {found}')
            sys.exit(1)
    with tempfile.TemporaryDirectory(prefix='bureau-internal-refs-') as scratch:
        root = Path(scratch).resolve()
        subprocess.run(['git', 'init', '-q', str(root)], check=True)
        (root/'.gitignore').write_text('__pycache__/\n*.pyc\nignored-tracked.txt\n')
        tracked = root/'templates/ignored-tracked.txt'
        tracked.parent.mkdir()
        tracked.write_text('clean baseline\n')
        subprocess.run(['git', '-C', str(root), 'add', '-f', '.gitignore',
                        str(tracked.relative_to(root))], check=True)

        def scan(mode='all', scope='templates', expected=None):
            result = subprocess.run([sys.executable, '-I', __file__, mode, str(root), str(root/scope)],
                                    capture_output=True, text=True)
            assert result.returncode == (1 if expected else 0), result.stdout + result.stderr
            if expected:
                assert str(expected) in result.stdout, result.stdout + result.stderr

        cache = root/'templates/__pycache__/x.pyc'
        cache.parent.mkdir()
        cache.write_bytes((key + str(1234)).encode())
        scan()
        print('PASS reference inventory skips ignored bytecode')

        untracked = root/'templates/new file\nwith spaces.txt'
        untracked.write_text(key + str(1234))
        scan(expected=untracked)
        untracked.unlink()
        print('PASS reference inventory catches untracked non-ignored files (NUL-safe names)')

        tracked.write_text(key + str(1234))
        scan(expected=tracked)
        tracked.write_text('clean baseline\n')
        print('PASS reference inventory scans tracked files even when ignored')

        prose = root/'docs/new.md'; prose.parent.mkdir()
        prose.write_text(key + str(1234))
        scan(scope='docs', expected=prose)
        test = root/'tests/new.py'; test.parent.mkdir()
        test.write_text('ISSUE="' + key + str(1234) + '"\n')
        scan('comments', 'tests')
        test.write_text('# ' + key + str(1234) + '\n')
        scan('comments', 'tests', expected=test)
        print('PASS reference inventory preserves public-prose and test-comment scopes')
    sys.exit(0)

comments_only = sys.argv[1] == 'comments'
root = Path(sys.argv[2]).resolve()
scopes = [Path(name).resolve() for name in sys.argv[3:]]
# Cached files stay covered even if now ignored; new files are included only
# when Git does not ignore them. NUL separation preserves unusual filenames.
inventory = subprocess.check_output(['git', '-C', str(root), 'ls-files',
                                    '--cached', '--others', '--exclude-standard', '-z'])
hits = 0
for name in sorted(set(inventory.decode('utf-8', errors='surrogateescape').split('\0')) - {''}):
    file = root/name
    if not file.is_file() or not any(file == path or path in file.parents for path in scopes):
        continue
    for n, line in enumerate(file.read_text(encoding='utf-8', errors='replace').splitlines(), 1):
        if matches(line, comments_only):
            print(f'{file}:{n}: {line}')
            hits += 1
sys.exit(1 if hits else 0)
PY

python3 -I "$T/scan.py" --self-check || fail 'reference matcher self-check'
echo 'PASS reference matcher reports private keys and allows fixture data in tests and neutral examples'

python3 -I "$T/scan.py" all "$ROOT" "$ROOT/templates" || fail 'private ticket reference under templates/'
echo 'PASS no private ticket references under templates/'

PROSE=("$ROOT/CHANGELOG.md" "$ROOT/AGENTS.md" "$ROOT/README.md" "$ROOT/SECURITY.md" "$ROOT/CONTRIBUTING.md"
       "$ROOT"/docs/*.md "$ROOT"/references/*.md)
python3 -I "$T/scan.py" all "$ROOT" "${PROSE[@]}" || fail 'private ticket reference in public prose'
echo 'PASS no private ticket references in public prose'

python3 -I "$T/scan.py" comments "$ROOT" "$ROOT/tests" || fail 'private ticket reference in a test comment'
echo 'PASS no private ticket references in test comments'

[ ! -e "$ROOT/docs/2026-09-25-drift-inventar.md" ] || fail 'internal drift inventory still exists'
echo 'PASS internal drift inventory is absent'
