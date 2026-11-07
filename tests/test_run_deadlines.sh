#!/bin/bash
set -euo pipefail
PYTHONDONTWRITEBYTECODE=1 python3 "$(dirname "$0")/run_deadlines_test.py"
