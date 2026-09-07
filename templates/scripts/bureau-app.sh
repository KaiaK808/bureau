#!/bin/bash
# Small terminal actions for an adopting project; no nested model invocation.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
case "${1:-status}" in
  doctor) exec python3 "$SCRIPT_DIR/bureau-doctor.py" ;;
  status) exec python3 "$SCRIPT_DIR/bureau-runtime.py" status ;;
  setup) exec python3 "$SCRIPT_DIR/bureau-runtime.py" setup ;;
  check)
    source "$SCRIPT_DIR/bureau-config.sh"
    jq -e '(.linear.teams | type == "array" and length > 0) and (.repo | type == "object")' "$BUREAU_CONFIG" >/dev/null
    for file in "$SCRIPT_DIR"/*.sh; do bash -n "$file"; done
    echo 'Bureau config and shell syntax checked.'
    ;;
  test)
    source "$SCRIPT_DIR/bureau-config.sh"
    TEST_COMMAND=$(jq -r '.repo.test_command // empty' "$BUREAU_CONFIG")
    [ -n "$TEST_COMMAND" ] || { echo 'Configure repo.test_command before using the tests action.' >&2; exit 1; }
    exec bash -c "$TEST_COMMAND"
    ;;
  *) echo 'Usage: bureau-app.sh setup|status|doctor|check|test' >&2; exit 1 ;;
esac
