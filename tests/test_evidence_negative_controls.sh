#!/bin/bash
# Mutation controls use private copies and prove the source remains unchanged.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PYTHONDONTWRITEBYTECODE=1 python3 "$ROOT/tests/evidence_negative_controls.py"
