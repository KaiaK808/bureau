#!/bin/bash
# Fake-wrapper negative coverage; no live slot or service calls.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PYTHONDONTWRITEBYTECODE=1 python3 "$ROOT/tests/test_wrapper_test.py" --negative
