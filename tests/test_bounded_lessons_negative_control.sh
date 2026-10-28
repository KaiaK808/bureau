#!/bin/bash
# The real budget assertion must detect a selector whose cap is disabled.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TMP=$(mktemp -d -t bureau-lessons-control.XXXXXXXX)
trap 'rm -rf "$TMP"' EXIT
cp -R "$ROOT/templates/scripts" "$TMP/scripts"
python3 - "$TMP/scripts/bureau-lessons.py" <<'PY'
from pathlib import Path
import sys
path = Path(sys.argv[1]); text = path.read_text()
needle = "if len(context.encode('utf-8')) <= budget:"
assert text.count(needle) == 1
path.write_text(text.replace(needle, 'if True:  # negative control: no byte cap'))
PY
PYTHONDONTWRITEBYTECODE=1 python3 - "$ROOT" "$TMP/scripts" <<'PY'
from pathlib import Path
import sys
root = Path(sys.argv[1]).resolve()
sys.path.insert(0, str(root/'tests'))
from mutation_checks import mutation_caught
mutation_caught([sys.executable, str(root/'tests/bounded_lessons_test.py'),
                 'LessonsTests.test_default_8192_bytes_and_legacy_is_bounded'],
                env={'BUREAU_TEST_LESSONS_SCRIPTS':str(Path(sys.argv[2]).resolve())},
                baseline_env={'BUREAU_TEST_LESSONS_SCRIPTS':str(root/'templates/scripts')})
PY
echo 'PASS negative control: disabling the byte cap fails the actual byte-budget assertion'
