#!/bin/bash
set -euo pipefail
PYTHONDONTWRITEBYTECODE=1 python3 -B "$(dirname "$0")/semgrep_pipeline_test.py" "$@"
