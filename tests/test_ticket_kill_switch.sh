#!/bin/bash
# Fake Linear/providers, real runtime, ownership and Git; includes a mutant.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PYTHONDONTWRITEBYTECODE=1 python3 "$ROOT/tests/ticket_kill_switch_test.py"
