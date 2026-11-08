#!/bin/bash
# Exercise the real harness with cheap test bodies, including an unweighted future test.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
python3 -I - "$ROOT" <<'PY'
import json
import os
from pathlib import Path
import re
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
    shutil.copy2(root / 'tests/run_test_files.py', fixture / 'run_test_files.py')
    shutil.copy2(root / 'tests/process_helpers.py', fixture / 'process_helpers.py')
    shutil.copy2(root / 'tests/shard-weights.tsv', fixture / 'shard-weights.tsv')
    executed = fixture / 'executed'
    for name in expected:
        (fixture / name).write_text('printf "%s\\n" "${0##*/}" >> "$SHARD_TEST_EXECUTED"\n')
    # A test reading stdin must not consume the harness's remaining file list.
    future_path = fixture / future_test
    future_path.write_text('cat >/dev/null\n' + future_path.read_text())

    def run_harness(selector=None, runner_os='Linux', listing=True, manifest=None, invoking=None):
        env = os.environ.copy()
        env.pop('BUREAU_TEST_SHARD', None)
        env.pop('BUREAU_TEST_MANIFEST', None)
        env.pop('BUREAU_TEST_TIMINGS', None)
        env.pop('BUREAU_TEST_FILE_TIMEOUT', None)
        if selector is not None:
            env['BUREAU_TEST_SHARD'] = selector
        if manifest is not None:
            env['BUREAU_TEST_MANIFEST'] = str(manifest)
        env['RUNNER_OS'] = runner_os
        env['LC_ALL'] = 'C'
        env['SHARD_TEST_EXECUTED'] = str(executed)
        executed.unlink(missing_ok=True)
        return subprocess.run(
            ['/bin/bash', str(fixture / 'run.sh'), *(['--list'] if listing else [])],
            cwd=invoking or fixture, env=env, input='', text=True, capture_output=True, timeout=30,
        )

    def shard_files(index, total, runner_os):
        result = run_harness(f'{index}/{total}', runner_os)
        assert result.returncode == 0, result.stderr
        return result.stdout.splitlines()

    def assert_partition(select_shard, total, runner_os, expected_files=expected):
        flattened = []
        for index in range(1, total + 1):
            files = select_shard(index, total, runner_os)
            assert files, f'empty shard {index}/{total}'
            assert files == sorted(files), f'unsorted shard {index}/{total}'
            flattened.extend(files)
        missing = set(expected_files) - set(flattened)
        extra = set(flattened) - set(expected_files)
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

    manifest_entries = []
    for line in (root / 'tests/macos-manifest.txt').read_text().splitlines():
        name, _, reason = line.partition('#')
        if not name.strip():
            continue
        assert reason.strip(), f'manifest entry has no reason: {line}'
        assert name.strip() in expected and name.strip() != future_test, f'unknown entry: {line}'
        manifest_entries.append(name.strip())
    assert manifest_entries and len(manifest_entries) == len(set(manifest_entries))
    assert 'test_untrusted_env_bureau.sh' in manifest_entries, 'missing macOS credential-server regression'
    print(f'PASS macOS manifest: {len(manifest_entries)} existing entrypoints with reasons, no duplicates')

    invoking = fixture / 'invoking directory'; invoking.mkdir()
    manifest = invoking / 'focused tests.txt'

    def manifest_files(index, total, runner_os):
        result = run_harness(f'{index}/{total}', runner_os, manifest=manifest)
        assert result.returncode == 0, result.stderr
        return result.stdout.splitlines()

    def assert_manifest_selection():
        result = run_harness(manifest=manifest)
        assert result.returncode == 0, result.stderr
        assert result.stdout.splitlines() == selected, 'manifest selection did not restrict discovery'

    for runner_os in ('Linux', 'macOS'):
        # All four belong to one full-suite shard: filtering after sharding
        # would leave the other shard empty instead of balancing the subset.
        selected = shard_files(1, 2, runner_os)[:4]
        manifest.write_text('# planted manifest\n\n' + '\n'.join(
            '  ' + name + ' # selected fixture' for name in reversed(selected)) + '\n')
        assert_manifest_selection()
        result = run_harness(runner_os=runner_os, listing=False,
                             manifest=manifest.name, invoking=invoking)
        assert result.returncode == 0, result.stdout + result.stderr
        assert executed.read_text().splitlines() == selected
        assert_partition(manifest_files, 2, runner_os, selected)
        for index in (1, 2):
            files = manifest_files(index, 2, runner_os)
            result = run_harness(f'{index}/2', runner_os, listing=False, manifest=manifest)
            assert result.returncode == 0, result.stdout + result.stderr
            assert executed.read_text().splitlines() == files
        print(f'PASS {runner_os}: manifest lists/executes only its files and balances before sharding')

    source = (fixture / 'run.sh').read_text()
    needle = "manifest = os.environ.get('BUREAU_TEST_MANIFEST')"
    assert source.count(needle) == 1
    try:
        (fixture / 'run.sh').write_text(source.replace(needle, 'manifest = None'))
        try:
            assert_manifest_selection()
        except AssertionError as error:
            assert str(error) == 'manifest selection did not restrict discovery', str(error)
        else:
            raise AssertionError('negative control: removing manifest selection passed')
    finally:
        (fixture / 'run.sh').write_text(source)
    print('PASS negative control: removing manifest selection fails the same selector assertion')

    for content in ('test_missing.sh # unknown\n', '../test_adrs.sh\n', '# no entries\n\n'):
        manifest.write_text(content)
        result = run_harness(listing=False, manifest=manifest)
        assert result.returncode != 0 and 'BUREAU_TEST_MANIFEST' in result.stderr, result.stderr
        assert not executed.exists(), 'invalid manifest executed tests'
    manifest.unlink()
    result = run_harness(manifest=manifest)
    assert result.returncode != 0 and 'BUREAU_TEST_MANIFEST' in result.stderr, result.stderr
    result = run_harness(manifest='')
    assert result.returncode == 0 and result.stdout.splitlines() == expected, result.stderr
    print('PASS unknown/empty/missing manifests fail loudly; empty selector preserves full discovery')

    workflow = (root / '.github/workflows/test.yml').read_text()
    assert "branches: [main, 'release/**']" in workflow
    assert "tags: ['v*']" in workflow
    assert re.search(r'^  pull_request:\s*$', workflow, re.M)
    assert "schedule:\n    - cron: '17 4 * * 1'" in workflow
    assert re.search(r'^  workflow_dispatch:\s*$', workflow, re.M)
    matrix = re.search(
        r"include: >-\s*\$\{\{ fromJSON\(github\.event_name == 'pull_request' && "
        r"'(\[.*?\])' \|\| '(\[.*?\])'\) }}", workflow, re.S)
    assert matrix, 'workflow must choose the focused matrix only for pull_request'
    pr_matrix, full_matrix = (json.loads(value) for value in matrix.groups())
    assert ("BUREAU_TEST_MANIFEST: ${{ matrix.mode == 'focused' && "
            "'tests/macos-manifest.txt' || '' }}") in workflow
    for event in ('pull_request', 'push', 'schedule', 'workflow_dispatch'):
        rows = pr_matrix if event == 'pull_request' else full_matrix
        assert len(rows) == (6 if event == 'pull_request' else 10), rows
        for os_name, total, mode in (('ubuntu-latest', 4, 'full'),
                                     ('macos-latest', 2 if event == 'pull_request' else 6,
                                      'focused' if event == 'pull_request' else 'full')):
            selected_rows = [row for row in rows if row['os'] == os_name]
            assert sorted(row['shard'] for row in selected_rows) == list(range(1, total + 1)), rows
            assert all(row['total'] == total and row['mode'] == mode for row in selected_rows), rows
    lint = workflow.split('      - name: Install lint tooling\n', 1)[1].split('\n      - ', 1)[0]
    assert 'if: matrix.shard == 1' in lint and 'install' in lint and 'shellcheck' in lint
    venv = workflow.split('      - name: Install Python venv support\n', 1)[1].split('\n      - ', 1)[0]
    assert "if: runner.os == 'Linux'" in venv and 'python3-venv' in venv
    focused_seconds = sum(costs[name][1] for name in manifest_entries)
    estimate = focused_seconds / 60 + 2 * 2
    assert 60 <= estimate <= 90, f'focused macOS estimate outside 60-90 runner-minutes: {estimate:.1f}'
    assert f'{estimate:.2f} runner-minutes' in workflow, 'refresh the workflow estimate after weight/manifest changes'
    print(f'PASS workflow: full Linux, focused PR macOS ({estimate:.2f}m), full other-event macOS, scoped lint/venv')

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

print('OK test_run_shards: full-suite and manifest discovery, execution, sharding and negative controls')
PY
