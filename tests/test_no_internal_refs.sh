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
import sys

KEY = re.compile(r'EXP-[0-9]')


def matches(line, comments_only):
    return bool(KEY.search(line)) and (not comments_only or line.lstrip().startswith('#'))


if sys.argv[1] == '--self-check':
    lines = [
        '# provenance EXP-1',
        '  # indented comment EXP-7',
        '\t# Python comment EXP-19',
        'ISSUE="EXP-7"',
        '{"identifier":"EXP-910"}',
        '# example TEAM-123',
        'echo "TEAM-123"',
        '# EXP- is a prefix; EXP-example has no digit',
    ]
    for comments_only, expected in ((False, [1, 2, 3, 4, 5]), (True, [1, 2, 3])):
        found = [n for n, line in enumerate(lines, 1) if matches(line, comments_only)]
        if found != expected:
            print(f'matcher self-check: comments_only={comments_only}, expected {expected}, got {found}')
            sys.exit(1)
    sys.exit(0)

comments_only = sys.argv[1] == 'comments'
hits = 0
for name in sys.argv[2:]:
    path = Path(name)
    files = sorted(p for p in path.rglob('*') if p.is_file()) if path.is_dir() else [path]
    for file in files:
        for n, line in enumerate(file.read_text(encoding='utf-8', errors='replace').splitlines(), 1):
            if matches(line, comments_only):
                print(f'{file}:{n}: {line}')
                hits += 1
sys.exit(1 if hits else 0)
PY

python3 -I "$T/scan.py" --self-check || fail 'reference matcher self-check'
echo 'PASS reference matcher reports private keys and allows fixture data in tests and neutral examples'

python3 -I "$T/scan.py" all "$ROOT/templates" || fail 'private ticket reference under templates/'
echo 'PASS no private ticket references under templates/'

PROSE=("$ROOT/CHANGELOG.md" "$ROOT/AGENTS.md" "$ROOT/README.md" "$ROOT/SECURITY.md" "$ROOT/CONTRIBUTING.md"
       "$ROOT"/docs/*.md "$ROOT"/references/*.md)
python3 -I "$T/scan.py" all "${PROSE[@]}" || fail 'private ticket reference in public prose'
echo 'PASS no private ticket references in public prose'

python3 -I "$T/scan.py" comments "$ROOT/tests" || fail 'private ticket reference in a test comment'
echo 'PASS no private ticket references in test comments'

[ ! -e "$ROOT/docs/2026-09-25-drift-inventar.md" ] || fail 'internal drift inventory still exists'
echo 'PASS internal drift inventory is absent'
