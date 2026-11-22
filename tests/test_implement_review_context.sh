#!/bin/bash
# Real merged review collector, implement stage and provider adapter; fake APIs/CLIs.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
export GIT_CEILING_DIRECTORIES="$(python3 -I -B -c 'from pathlib import Path; import tempfile; print(Path(tempfile.gettempdir()).resolve())')"
PYTHONDONTWRITEBYTECODE=1 python3 "$ROOT/tests/implement_review_context_test.py" "$@"
