#!/bin/bash
# Exercise the real harness with cheap test bodies, including an unweighted future test.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
python3 -I - "$ROOT" <<'PY'
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

root = Path(sys.argv[1])
expected = sorted(p.name for p in (root / 'tests').glob('test_*.sh') if p.is_file())
future_test = 'test_unmeasured_future.sh'
assert future_test not in expected
expected = sorted([*expected, future_test])

with tempfile.TemporaryDirectory(prefix='bureau-test-shards. ') as directory:
    fixture = Path(directory)
    shutil.copy2(root / 'tests/run.sh', fixture / 'run.sh')
    shutil.copy2(root / 'tests/shard-weights.tsv', fixture / 'shard-weights.tsv')
    executed = fixture / 'executed'
    for name in expected:
        (fixture / name).write_text('printf "%s\\n" "${0##*/}" >> "$SHARD_TEST_EXECUTED"\n')
    # A test reading stdin must not consume the harness's remaining file list.
    future_path = fixture / future_test
    future_path.write_text('cat >/dev/null\n' + future_path.read_text())

    def run_harness(selector=None, runner_os='Linux', listing=True):
        env = os.environ.copy()
        env.pop('BUREAU_TEST_SHARD', None)
        if selector is not None:
            env['BUREAU_TEST_SHARD'] = selector
        env['RUNNER_OS'] = runner_os
        env['LC_ALL'] = 'C'
        env['SHARD_TEST_EXECUTED'] = str(executed)
        executed.unlink(missing_ok=True)
        return subprocess.run(
            ['/bin/bash', str(fixture / 'run.sh'), *(['--list'] if listing else [])],
            env=env, input='', text=True, capture_output=True, timeout=30,
        )

    def shard_files(index, total, runner_os):
        result = run_harness(f'{index}/{total}', runner_os)
        assert result.returncode == 0, result.stderr
        return result.stdout.splitlines()

    def assert_partition(select_shard, total, runner_os):
        flattened = []
        for index in range(1, total + 1):
            files = select_shard(index, total, runner_os)
            assert files, f'empty shard {index}/{total}'
            assert files == sorted(files), f'unsorted shard {index}/{total}'
            flattened.extend(files)
        missing = set(expected) - set(flattened)
        extra = set(flattened) - set(expected)
        assert not missing and not extra, f'missing={sorted(missing)}, extra={sorted(extra)}'
        assert len(flattened) == len(set(flattened)), 'shards overlap or duplicate files'

    for runner_os in ('Linux', 'macOS'):
        for total in range(1, 9):
            assert_partition(shard_files, total, runner_os)
            print(f'PASS {runner_os} n={total}: union is all {len(expected)} files, shards disjoint')

    result = run_harness()
    assert result.returncode == 0 and result.stdout.splitlines() == expected, result.stderr
    print(f'PASS unset selector lists all {len(expected)} files')
    result = run_harness(listing=False)
    assert result.returncode == 0, result.stdout + result.stderr
    assert executed.read_text().splitlines() == expected
    assert f'Results: {len(expected)} passed, 0 failed' in result.stdout
    print(f'PASS unset selector executes all {len(expected)} files')

    # Prove the selected list reaches the execution loop, rather than only --list.
    for runner_os, total in (('Linux', 4), ('macOS', 6)):
        for index in range(1, total + 1):
            files = shard_files(index, total, runner_os)
            result = run_harness(f'{index}/{total}', runner_os, listing=False)
            assert result.returncode == 0, result.stdout + result.stderr
            assert executed.read_text().splitlines() == files
            assert f'Results: {len(files)} passed, 0 failed' in result.stdout
        print(f'PASS {runner_os}: all {total} shards execute exactly their selected files')

    # Evaluate the measured cost of the actual assignment, without reproducing its algorithm.
    costs = {}
    for line in (fixture / 'shard-weights.tsv').read_text().splitlines():
        if not line.strip() or line.startswith('#'):
            continue
        name, ubuntu, macos = line.split()
        costs[name] = tuple(None if value == '-' else int(value) for value in (ubuntu, macos))
    for runner_os, total in (('Linux', 4), ('macOS', 6)):
        estimates = {}
        for name in expected:
            ubuntu, macos = costs.get(name, (None, None))
            linux_cost = ubuntu if ubuntu is not None else ((macos + 1) // 2 if macos else 60)
            estimates[name] = linux_cost if runner_os == 'Linux' else (macos or 2 * linux_cost)
        loads = [sum(estimates[name] for name in shard_files(i, total, runner_os))
                 for i in range(1, total + 1)]
        assert max(loads) <= 1.1 * sum(loads) / total, f'{runner_os} unbalanced: {loads}'
        assert max(loads) < 3600, f'{runner_os} over an hour: {loads}'
        print(f'PASS {runner_os} balance: {loads} seconds, max within 10% of average and under 60m')

    malformed = (
        '', '0/1', '1/0', '2/1', '1', '1/', '/2', '1/2/3', '-1/2', '1/-2',
        '1.0/2', ' 1/2', '1/2 ', '01/2', '1/02', 'x/y', '1/2\n', '1/\n2',
        f'1/{len(expected) + 1}', f'{len(expected) + 1}/{len(expected) + 1}',
        '1/' + '9' * 64,
    )
    for selector in malformed:
        result = run_harness(selector, listing=False)
        assert result.returncode != 0, f'accepted malformed selector {selector!r}'
        assert 'BUREAU_TEST_SHARD' in result.stderr, f'no diagnostic for {selector!r}'
        assert not executed.exists(), f'malformed selector {selector!r} executed tests'
    print(f'PASS {len(malformed)} malformed selectors fail loudly before executing any test')

    def drop_one_file(index, total, runner_os):
        return [name for name in shard_files(index, total, runner_os) if name != expected[0]]

    try:
        assert_partition(drop_one_file, 4, 'Linux')
    except AssertionError as error:
        assert expected[0] in str(error), f'negative control failed for the wrong reason: {error}'
    else:
        raise AssertionError('negative control: dropping one file passed the partition test')
    print('PASS negative control: dropping one file fails the same partition assertion')

print('OK test_run_shards: 16 partitions (72 shard listings), 2 unset checks, '
      '10 shard executions, 2 balance checks, 21 malformed selectors, 1 negative control')
PY
