#!/bin/bash
set -euo pipefail
PYTHONDONTWRITEBYTECODE=1 python3 "$(dirname "$0")/review_config_test.py" "$@"
