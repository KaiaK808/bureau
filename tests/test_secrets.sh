#!/bin/bash
set -euo pipefail
export PYTHONDONTWRITEBYTECODE=1
python3 -B "$(dirname "$0")/secret_rules_test.py" "$@"
python3 -B "$(dirname "$0")/secret_scan_test.py" "$@"
python3 -B "$(dirname "$0")/secret_scan_pipeline_test.py" "$@"
