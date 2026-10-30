#!/bin/bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PYTHONDONTWRITEBYTECODE=1 python3 "$ROOT/tests/process_group_test.py"
PYTHONDONTWRITEBYTECODE=1 python3 "$ROOT/tests/process_group_negative_control.py" --mutations
