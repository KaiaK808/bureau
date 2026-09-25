#!/bin/bash
# The review stage's exit code comes from the verdict alone, and the shepherd halts on every
# code it does not explicitly know.
#
# Runs the REAL resolve_verdict_exit and shepherd_rc_action from bureau-config.sh, and the REAL
# last line of code-review-pipeline.sh cut out of the script. The shepherd's reaction to a
# BLOCK is covered end to end in tests/test_shepherd.sh (test_block_halts).
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")" && cd .. && pwd)"
SCRIPTS="$REPO_ROOT/templates/scripts"
TMP=$(mktemp -d -t bureau-test.gate.XXXXXXXX)
trap 'rm -rf "$TMP"' EXIT
fail() { echo "FAIL $*" >&2; exit 1; }

sed -n -e '/^resolve_verdict_exit() {/,/^}/p' -e '/^shepherd_rc_action() {/,/^}/p' \
  "$SCRIPTS/bureau-config.sh" > "$TMP/fn.sh"
# shellcheck source=/dev/null
source "$TMP/fn.sh"

for pair in APPROVE:0 REQUEST_CHANGES:0 BLOCK:25 :25 approve:25 NONSENSE:25; do
  got=$(resolve_verdict_exit "${pair%%:*}")
  [ "$got" = "${pair##*:}" ] || fail "resolve_verdict_exit '${pair%%:*}' gave $got, wanted ${pair##*:}"
done
echo "PASS the verdict alone sets the exit code; BLOCK and anything unknown are 25"

for pair in 0:ok 2:ok 10:retry 16:retry 11:halt 17:halt 20:halt 21:halt 22:halt 23:halt 24:halt 25:halt 26:halt 27:halt 124:halt 130:halt 99:halt :halt; do
  got=$(shepherd_rc_action "${pair%%:*}")
  [ "$got" = "${pair##*:}" ] || fail "shepherd_rc_action '${pair%%:*}' gave $got, wanted ${pair##*:}"
done
echo "PASS the shepherd halts on every code but 0, 2, 10 and 16, unknown codes included"

LAST=$(grep -E '^exit "\$\(resolve_verdict_exit ' "$SCRIPTS/code-review-pipeline.sh" || true)
[ "$(printf '%s\n' "$LAST" | grep -c .)" = 1 ] || fail "code-review-pipeline.sh no longer ends with one resolve_verdict_exit line"
[ "$(tail -n 1 "$SCRIPTS/code-review-pipeline.sh")" = "$LAST" ] || fail "the verdict exit is not the stage's last line"
for pair in APPROVE:0 REQUEST_CHANGES:0 BLOCK:25 :25; do
  set +e
  VERDICT="${pair%%:*}" /bin/bash -c "source '$TMP/fn.sh'; $LAST"
  rc=$?
  set -e
  [ "$rc" = "${pair##*:}" ] || fail "the stage's last line exits $rc for '${pair%%:*}', wanted ${pair##*:}"
done
echo "PASS the review stage's real last line exits by the verdict"
