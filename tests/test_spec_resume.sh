#!/bin/bash
# Real worker/reset + checkpoint/governance contracts; providers/Linear faked.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PYTHONDONTWRITEBYTECODE=1 python3 "$ROOT/tests/spec_resume_test.py"
