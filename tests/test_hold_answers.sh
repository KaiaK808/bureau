#!/bin/bash
# Real hold admission and posting with fake Linear, entirely offline.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PYTHONDONTWRITEBYTECODE=1 python3 -B "$ROOT/tests/hold_answers_test.py" "$@"
