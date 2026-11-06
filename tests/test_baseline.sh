#!/bin/bash
# Read-only maintainer baseline; local Git/evidence and fake API readers only.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PYTHONDONTWRITEBYTECODE=1 python3 "$ROOT/tests/baseline_test.py"
