#!/bin/bash
# PR 7 renderer, delivery and receipt collector contract; all services are faked.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PYTHONDONTWRITEBYTECODE=1 python3 "$ROOT/tests/acceptance_insights_test.py"
