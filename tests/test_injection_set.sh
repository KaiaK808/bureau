#!/bin/bash
set -euo pipefail
PYTHONDONTWRITEBYTECODE=1 python3 "$(dirname "$0")/injection_set_test.py" "$@"
