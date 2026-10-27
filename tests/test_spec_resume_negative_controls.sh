#!/bin/bash
# Mutate private copies and prove the positive guards detect unsafe reuse/PASS.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PYTHONDONTWRITEBYTECODE=1 python3 "$ROOT/tests/spec_resume_negative_controls.py"
