#!/bin/bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PYTHONDONTWRITEBYTECODE=1 python3 "$ROOT/tests/silence_watchdog_negative_controls.py"
PYTHONDONTWRITEBYTECODE=1 python3 "$ROOT/tests/silence_watchdog_fix1_negative_controls.py"
