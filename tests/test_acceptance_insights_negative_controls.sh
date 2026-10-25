#!/bin/bash
# Mutation tests operate only on private temporary source copies.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PYTHONDONTWRITEBYTECODE=1 python3 "$ROOT/tests/acceptance_insights_negative_controls.py"
