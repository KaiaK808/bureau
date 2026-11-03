#!/bin/bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd -P)"
PYTHONDONTWRITEBYTECODE=1 python3 "$ROOT/tests/shared_runtime_test.py" \
  RuntimeTests.test_config_change_names_release_and_reprepare_and_recovery_works
