#!/bin/bash
# Real merged review collector, implement stage and provider adapter; fake APIs/CLIs.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PYTHONDONTWRITEBYTECODE=1 python3 "$ROOT/tests/implement_review_context_test.py" "$@"
