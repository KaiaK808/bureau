#!/bin/bash
# Test runner: discover tests/test_*.sh, run each in isolation, report pass/fail.
# No external deps beyond bash + jq + git. Designed to run locally (< 30s) and
# in CI (.github/workflows/test.yml).
set -uo pipefail

cd "$(dirname "$0")"

pass=0
fail=0
failed_tests=()

for t in test_*.sh; do
  [ -f "$t" ] || continue
  log=$(mktemp -t bureau-test.XXXXXX.log)
  if bash "$t" >"$log" 2>&1; then
    echo "PASS  $t"
    pass=$((pass + 1))
  else
    echo "FAIL  $t"
    sed 's/^/      /' "$log"
    fail=$((fail + 1))
    failed_tests+=("$t")
  fi
  rm -f "$log"
done

echo
echo "─────────────────────────────"
echo "Results: $pass passed, $fail failed"
[ "$fail" -eq 0 ] && exit 0

echo "Failed tests:"
for t in "${failed_tests[@]}"; do echo "  - $t"; done
exit 1
