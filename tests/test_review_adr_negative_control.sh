#!/bin/bash
# Verify private runtime mutations fail unchanged enabled-mode assertions.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PYTHONDONTWRITEBYTECODE=1 python3 "$ROOT/tests/review_adr_negative_control.py"
