#!/bin/bash
set -euo pipefail
PYTHONDONTWRITEBYTECODE=1 python3 -B "$(dirname "$0")/trivy_pipeline_test.py" "$@"
