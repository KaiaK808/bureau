#!/bin/bash
# Deterministic conflict repair with real local Git and fake service edges.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PYTHONDONTWRITEBYTECODE=1 python3 "$ROOT/tests/conflicts_test.py"
