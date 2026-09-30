#!/bin/bash
# The agent processes run without the Bureau secrets: tests/provider_untrusted_env_test.py.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PYTHONDONTWRITEBYTECODE=1 python3 "$ROOT/tests/provider_untrusted_env_test.py"
