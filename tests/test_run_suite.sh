#!/bin/bash
# Private full-selection fixtures; never run the repository's real suite here.
set -euo pipefail
PYTHONDONTWRITEBYTECODE=1 python3 "$(dirname "$0")/run_suite_test.py"
