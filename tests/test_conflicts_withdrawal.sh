#!/bin/bash
# Real PR 9 checkpoint/Linear transport and owned conflict-repair workers.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PYTHONDONTWRITEBYTECODE=1 python3 "$ROOT/tests/conflicts_withdrawal_test.py"
