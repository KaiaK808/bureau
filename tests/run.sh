#!/bin/bash
# Test runner: discover tests/test_*.sh, run each in isolation, report pass/fail.
# BUREAU_TEST_SHARD=i/n (1-based) selects a balanced share; unset runs all tests.
# Sharding uses Python 3 and shard-weights.tsv; --list prints files without running.
# BUREAU_TEST_FILE_TIMEOUT overrides weighted deadlines; BUREAU_TEST_TIMINGS saves TSV.
# Runs locally and in CI (.github/workflows/test.yml).
# Spec admission/number allocation coverage is registered by discovery below:
# test_in_flight_count.sh, test_shepherd.sh, test_spec_number_alloc.sh,
# test_spec_feature_json.sh and test_spec_capacity_consumers.sh.
# Evidence/receipt/wait mutation controls: test_evidence_negative_controls.sh.
set -uo pipefail

if [ -n "${BUREAU_TEST_TIMINGS:-}" ]; then
  case "$BUREAU_TEST_TIMINGS" in
    /*) ;;
    *) export BUREAU_TEST_TIMINGS="$PWD/$BUREAU_TEST_TIMINGS" ;;
  esac
fi
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
from pathlib import Path
import re
import sys

from run_test_files import expected_seconds, read_weights, runner_os

selector = re.fullmatch(r"([1-9][0-9]*)/([1-9][0-9]*)", sys.argv[1])
if selector is None:
    sys.exit("BUREAU_TEST_SHARD must be i/n with positive integers (1-based)")
index, total = map(int, selector.groups())
files = sorted(p.name for p in Path('.').glob('test_*.sh') if p.is_file())
if not 1 <= index <= total <= len(files):
    sys.exit(f"BUREAU_TEST_SHARD requires 1 <= i <= n <= {len(files)} test files")

weights = read_weights(Path('shard-weights.tsv'))
os_name = runner_os()


# Longest first, then filename; equal loads go to the lowest shard number.
loads = [0] * total
shards = [[] for _ in range(total)]
for name in sorted(files, key=lambda name: (-expected_seconds(name, weights, os_name), name)):
    shard = min(range(total), key=lambda i: (loads[i], i))
    shards[shard].append(name)
    loads[shard] += expected_seconds(name, weights, os_name)
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

exec python3 -I run_test_files.py "${tests[@]}"
