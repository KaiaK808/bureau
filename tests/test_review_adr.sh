#!/bin/bash
# Real local Git/workers; providers, Linear and GitHub are fixture processes.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PYTHONDONTWRITEBYTECODE=1 python3 "$ROOT/tests/review_adr_test.py"
