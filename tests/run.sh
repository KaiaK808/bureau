#!/bin/bash
# Test runner: discover tests/test_*.sh, run each in isolation, report pass/fail.
# BUREAU_TEST_SHARD=i/n (1-based) selects a balanced share; unset runs all tests.
# Sharding uses Python 3 and shard-weights.tsv; --list prints files without running.
# Runs locally and in CI (.github/workflows/test.yml).
# Spec admission/number allocation coverage is registered by discovery below:
# test_in_flight_count.sh, test_shepherd.sh, test_spec_number_alloc.sh,
# test_spec_feature_json.sh and test_spec_capacity_consumers.sh.
# Evidence/receipt/wait mutation controls: test_evidence_negative_controls.sh.
set -uo pipefail

cd "$(dirname "$0")" || exit 1

select_tests() {
  if [ "${BUREAU_TEST_SHARD+x}" != x ]; then
    for t in test_*.sh; do
      [ -f "$t" ] || continue
      printf '%s\n' "$t"
    done
    return 0
  fi

  python3 - "$BUREAU_TEST_SHARD" <<'PY'
import os
from pathlib import Path
import re
import sys

selector = re.fullmatch(r"([1-9][0-9]*)/([1-9][0-9]*)", sys.argv[1])
if selector is None:
    sys.exit("BUREAU_TEST_SHARD must be i/n with positive integers (1-based)")
index, total = map(int, selector.groups())
files = sorted(p.name for p in Path('.').glob('test_*.sh') if p.is_file())
if not 1 <= index <= total <= len(files):
    sys.exit(f"BUREAU_TEST_SHARD requires 1 <= i <= n <= {len(files)} test files")

weights = {}
for line in Path('shard-weights.tsv').read_text().splitlines():
    if not line.strip() or line.startswith('#'):
        continue
    name, ubuntu, macos = line.split()
    weights[name] = tuple(None if value == '-' else int(value) for value in (ubuntu, macos))
is_macos = os.environ.get('RUNNER_OS', sys.platform) in ('macOS', 'darwin')


def expected_seconds(name):
    ubuntu, macos = weights.get(name, (None, None))
    # Round the observed Ubuntu mean (~45s) up to 60s for unmeasured files.
    # The common measured files take ~1.8x on macOS; use 2x when estimating.
    ubuntu = ubuntu if ubuntu is not None else ((macos + 1) // 2 if macos is not None else 60)
    return (macos if macos is not None else 2 * ubuntu) if is_macos else ubuntu


# Longest first, then filename; equal loads go to the lowest shard number.
loads = [0] * total
shards = [[] for _ in range(total)]
for name in sorted(files, key=lambda name: (-expected_seconds(name), name)):
    shard = min(range(total), key=lambda i: (loads[i], i))
    shards[shard].append(name)
    loads[shard] += expected_seconds(name)
print('\n'.join(sorted(shards[index - 1])))
PY
}

if ! test_files=$(select_tests); then
  exit 1
fi
if [ "${1:-}" = --list ]; then
  [ -z "$test_files" ] || printf '%s\n' "$test_files"
  exit 0
fi

# Read the selection before running children so their stdin stays unchanged.
tests=()
while IFS= read -r t; do
  tests+=("$t")
done <<< "$test_files"

pass=0
fail=0
failed_tests=()

for t in "${tests[@]}"; do
  [ -f "$t" ] || continue
  log=$(mktemp -t bureau-test.XXXXXX.log)
  if bash "$t" >"$log" 2>&1; then
    echo "PASS  $t"
    # A test that skips a part says so in a line starting with SKIP; show it.
    grep '^SKIP' "$log" | sed 's/^/      /' || true
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
