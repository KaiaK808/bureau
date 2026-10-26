#!/bin/bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PYTHONDONTWRITEBYTECODE=1 python3 "$ROOT/tests/file_overlap_test.py"
PYTHONDONTWRITEBYTECODE=1 python3 "$ROOT/tests/file_overlap_negative_control.py"
