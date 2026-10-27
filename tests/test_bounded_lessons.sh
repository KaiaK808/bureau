#!/bin/bash
# Real shell entry, task paths, local PR diffs, expiry and UTF-8 byte budgets.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PYTHONDONTWRITEBYTECODE=1 python3 "$ROOT/tests/bounded_lessons_test.py"
