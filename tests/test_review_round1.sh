#!/bin/bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PYTHONDONTWRITEBYTECODE=1 python3 "$ROOT/tests/review_round1_test.py"
PYTHONDONTWRITEBYTECODE=1 python3 "$ROOT/tests/review_round1_negative_controls.py"
