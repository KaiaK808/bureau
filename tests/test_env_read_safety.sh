#!/bin/bash
# A value in .env must never run as a command.
#
# Every script under templates/scripts/ used to read its .env with `source` (most of them
# wrapped in `set -a`). Under `source`, a line like `KEY= value` executes `value`, and bash
# echoes it in the "command not found" message — into a log that reaches Linear or GitHub.
# `bureau_load_env` in templates/scripts/bureau-env.sh parses instead of executing.
#
# The test runs the REAL reader against a hostile .env, and then runs the OLD form against
# the same file as a control. Without that control the assertions below could pass on a file
# that was harmless to begin with.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")" && cd .. && pwd)"
SANDBOX=$(mktemp -d -t bureau-test.envread.XXXXXXXX)
trap 'rm -rf "$SANDBOX"' EXIT

fail() { echo "FAIL $*" >&2; exit 1; }

MARKER="$SANDBOX/executed"
cat > "$SANDBOX/.env" <<EOF
LINEAR_API_KEY=lin_api_value
BUREAU_LINEAR_RETRIES=3
HOSTILE= \$(touch "$MARKER")
BUREAU_MAX_REVIEW_CYCLES=08
NOT_ON_THE_LIST=whatever
EOF

cd "$SANDBOX"

# --- the reader ------------------------------------------------------------
out=$(/bin/bash -c '
  source "$1/templates/scripts/bureau-env.sh"
  bureau_load_env --export .env || exit 9
  printf "key=%s retries=%s cycles=%s other=%s\n" \
    "${LINEAR_API_KEY:-}" "${BUREAU_LINEAR_RETRIES:-}" \
    "${BUREAU_MAX_REVIEW_CYCLES:-unset}" "${NOT_ON_THE_LIST:-unset}"
' _ "$REPO_ROOT" 2>/dev/null)

[ ! -e "$MARKER" ] || fail "bureau_load_env executed a value from .env"
case "$out" in
  *"key=lin_api_value"*) ;;
  *) fail "bureau_load_env did not read an allow-listed key: $out" ;;
esac
case "$out" in
  *"retries=3"*) ;;
  *) fail "bureau_load_env dropped a valid numeric key: $out" ;;
esac
case "$out" in
  *"cycles=unset"*) ;;
  *) fail "bureau_load_env took 08 for a numeric key — bash reads it as octal: $out" ;;
esac
case "$out" in
  *"other=unset"*) ;;
  *) fail "bureau_load_env set a key that is not on the allow list: $out" ;;
esac
echo "PASS bureau_load_env reads the allow list and executes nothing"

# --- the control: the form this replaced -----------------------------------
# If this does NOT fire, the hostile line above stopped being hostile and every
# assertion further up became decoration.
rm -f "$MARKER"
/bin/bash -c 'set -a; source .env; set +a' >/dev/null 2>&1 || true
[ -e "$MARKER" ] || fail "the control did not execute either — the hostile .env line no longer bites, fix the fixture"
echo "PASS the control proves the fixture is still hostile under the old form"

# --- no script may go back to sourcing .env --------------------------------
offenders=$(grep -lE '(^|[^a-z_])(source|\.)[[:space:]]+[^[:space:]]*\.env' \
  "$REPO_ROOT"/templates/scripts/*.sh 2>/dev/null || true)
[ -z "$offenders" ] || fail "these scripts source .env instead of reading it: $offenders"
echo "PASS no script under templates/scripts sources its .env"
