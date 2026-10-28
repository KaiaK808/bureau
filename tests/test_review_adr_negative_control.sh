#!/bin/bash
# Verify enabled-mode assertions fail against local released runtime objects.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PYTHONDONTWRITEBYTECODE=1 python3 "$ROOT/tests/review_adr_negative_control.py"
