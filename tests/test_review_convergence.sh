#!/bin/bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
python3 "$ROOT/tests/review_convergence_test.py"
python3 "$ROOT/tests/review_convergence_negative_controls.py"
