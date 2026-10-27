#!/bin/bash
# Shared reservations use real Git refs/worktrees and real processes; no providers/network.
set -euo pipefail
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
python3 - "$REPO_ROOT" <<'PY'
import concurrent.futures, hashlib, json, os, pathlib, runpy, subprocess, sys, tempfile, time, zipfile
root = pathlib.Path(sys.argv[1])
script = root / 'templates/scripts/bureau-supervision.py'
os.environ.pop('BUREAU_SPEC_LOCK_TIMEOUT', None)
module = runpy.run_path(str(script))
assert module['spec_lock_timeout']() == 300
for value, expected in [('00001', 1), ('86400', 86400), ('0', 300), ('86401', 300), ('x', 300), ('9' * 5000, 300)]:
    os.environ['BUREAU_SPEC_LOCK_TIMEOUT'] = value
    assert module['spec_lock_timeout']() == expected
os.environ.pop('BUREAU_SPEC_LOCK_TIMEOUT', None)
print('PASS allocator lock defaults to 300 s; bounded ASCII settings, decimal zeros and invalid fallback')

with zipfile.ZipFile(root / 'tests/fixtures/speckit-1.2.2.zip') as fixture:
    for name, digest in {
        'scripts/bash/create-new-feature.sh': '972f03ed8ca5ee17b7baad6d56d821d46269d77abc337496beafb0fe85a725f2',
        'scripts/bash/common.sh': '53cb1c23e349db4586c272436ce35c6e2f36a9c0b5351a85e0b216d9887a26b7',
        'LICENSE': '2510b446bc1f0cf9702453075d20cd88631e20e5642658edb7325d9c1eb534f7',
    }.items():
        assert hashlib.sha256(fixture.read(name)).hexdigest() == digest, name
print('PASS pinned upstream fixture SHA-256: feature helper, common helper and license unchanged')

def run(*args, cwd=None):
    return subprocess.check_output(args, cwd=cwd, text=True, stderr=subprocess.PIPE).strip()

with tempfile.TemporaryDirectory(prefix='bureau-test.spec-numbers.') as tmp:
    tmp = pathlib.Path(tmp)
    repo = tmp / 'repo'; repo.mkdir()
    def git(*args): return run('git', '-C', str(repo), *args)
    git('init', '-q', '-b', 'main'); git('config', 'user.email', 'test@bureau'); git('config', 'user.name', 'test')
    (repo / 'specs/050-main').mkdir(parents=True); (repo / 'specs/050-main/spec.md').write_text('main')
    git('add', '.'); git('commit', '-qm', '050 on main')
    run('git', 'init', '-q', '--bare', str(tmp / 'origin.git'))
    git('remote', 'add', 'origin', str(tmp / 'origin.git')); git('push', '-q', 'origin', 'main')
    git('checkout', '-qb', 'feat/KEY-99-unmerged')
    (repo / 'specs/051-unmerged').mkdir(); (repo / 'specs/051-unmerged/spec.md').write_text('unmerged')
    git('add', '.'); git('commit', '-qm', '051 on issue branch'); git('push', '-q', 'origin', 'HEAD')
    git('checkout', '-q', 'main'); git('branch', '-D', 'feat/KEY-99-unmerged')
    git('fetch', '-q', 'origin', '+refs/heads/*:refs/remotes/origin/*')
    registry = repo / '.git/bureau/spec-numbers.json'
    def allocate(issue, *, program=script, cwd=repo, extra=()):
        return json.loads(run(sys.executable, '-I', str(program), '--repo', str(cwd),
                              'reserve-spec-number', issue, *extra))['number']
    assert allocate('KEY-100') == 52
    assert allocate('KEY-100') == 52
    print('PASS main 050 + origin feat/KEY branch containing 051 -> 052; retry -> 052')
    registry.unlink()
    with concurrent.futures.ThreadPoolExecutor(2) as pool:
        values = sorted(pool.map(allocate, ('KEY-101', 'KEY-102')))
    assert values == [52, 53], values
    assert sorted(json.loads(registry.read_text()).values()) == [52, 53]
    print('PASS two simultaneous processes reserve 052/053 with both records intact')

    worktree = tmp / 'other checkout'
    git('worktree', 'add', '-q', '--detach', str(worktree), 'main')
    (worktree / 'specs/060-uncommitted').mkdir()
    assert allocate('KEY-103', cwd=worktree) == 61
    assert allocate('KEY-103') == 61
    print('PASS uncommitted worktree directory counts, shared reservation/retry across worktrees')
    git('branch', '062-local')
    assert allocate('KEY-104') == 63
    git('branch', '-D', '062-local')
    (worktree / 'specs/070-detached').mkdir(); (worktree / 'specs/070-detached/spec.md').write_text('detached')
    run('git', '-C', str(worktree), 'add', 'specs'); run('git', '-C', str(worktree), 'commit', '-qm', 'detached')
    # Keep only the committed tree as evidence, removing its working directory.
    import shutil
    shutil.rmtree(worktree / 'specs/070-detached')
    assert allocate('KEY-105') == 71
    print('PASS numeric local branch and committed detached-worktree trees count')
    git('worktree', 'remove', '--force', str(worktree))

    # Origin-tree inspection negative control. Only origin/main (050) is local.
    registry.unlink()
    old = tmp / 'no-origin.py'
    text = script.read_text()
    shutil.copy(script.with_name('bureau-evidence.py'), tmp / 'bureau-evidence.py')
    before = "'refs/remotes/origin/', 'refs/heads/'"
    assert text.count(before) == 1
    old.write_text(text.replace(before, "'refs/heads/'"))
    assert allocate('KEY-106', program=old) == 51
    print('PASS NEGATIVE CONTROL omit origin-branch inspection -> duplicate 051')
    registry.unlink()

    # Force the two unlocked allocators to reach the persistence boundary together.
    # Only the sandbox copy loses its lock; the shipped code has no testing hooks.
    old = tmp / 'no-lock.py'
    begin = text.index('def reserve_spec_number('); end = text.index('\ndef fingerprint(', begin)
    region = text[begin:end]
    lock_call = 'fcntl.flock(guard, fcntl.LOCK_EX | fcntl.LOCK_NB)'
    assert region.count(lock_call) == 1
    region = region.replace(lock_call, 'pass  # negative control: no lock')
    region = region.replace('        numbers[issue] = number', '''        (root / (issue + '.ready')).touch()
        until = time.monotonic() + 10
        while len(list(root.glob('*.ready'))) < 2:
            if time.monotonic() > until:
                raise ValueError('negative control barrier timed out')
            time.sleep(0.01)
        numbers[issue] = number''')
    old.write_text(text[:begin] + region + text[end:])
    with concurrent.futures.ThreadPoolExecutor(2) as pool:
        values = sorted(pool.map(lambda issue: allocate(issue, program=old), ('KEY-107', 'KEY-108')))
    assert values == [52, 52], values
    assert len(json.loads(registry.read_text())) == 1
    print('PASS NEGATIVE CONTROL omit lock -> 052/052 collision and one lost reservation')
    registry.unlink()
    assert allocate('KEY-109') == 52
    assert allocate('KEY-110') == 53
    print('PASS restored allocator -> 052/053')

    # A real holder remains alive past the deadline: no reservation is changed.
    holder_code = '''import fcntl, sys
with open(sys.argv[1], 'a') as guard:
    fcntl.flock(guard, fcntl.LOCK_EX)
    print('locked', flush=True)
    sys.stdin.read()
'''
    guard = registry.with_name('spec-numbers.guard')
    with subprocess.Popen([sys.executable, '-c', holder_code, str(guard)],
                          stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True) as holder:
        try:
            assert holder.stdout.readline().strip() == 'locked'
            before = registry.read_bytes()
            env = dict(os.environ, BUREAU_SPEC_LOCK_TIMEOUT='1')
            command = [sys.executable, '-I', str(script), '--repo', str(repo), 'reserve-spec-number', 'KEY-113']
            started = time.monotonic()
            result = subprocess.run(command, env=env, text=True, capture_output=True, timeout=5)
            elapsed = time.monotonic() - started
            assert result.returncode == 124, result
            assert 'timed out after 1s waiting for spec allocator lock' in result.stderr, result.stderr
            assert 0.9 <= elapsed < 5, elapsed
            assert not result.stdout and registry.read_bytes() == before
            print('PASS held allocator lock -> 124 after 1 s, clear diagnostic, registry unchanged')

            old = tmp / 'blocking-lock.py'
            begin = text.index('def reserve_spec_number('); end = text.index('\ndef fingerprint(', begin)
            assert text[begin:end].count(lock_call) == 1
            old.write_text(text[:begin] + text[begin:end].replace(lock_call, 'fcntl.flock(guard, fcntl.LOCK_EX)') + text[end:])
            command[2] = str(old)
            try:
                subprocess.run(command, env=env, text=True, capture_output=True, timeout=2)
            except subprocess.TimeoutExpired:
                print('PASS NEGATIVE CONTROL blocking flock hangs past configured 1 s (killed at 2 s)')
            else:
                raise AssertionError('blocking-lock control unexpectedly finished')
            assert registry.read_bytes() == before
        finally:
            holder.communicate()
    assert allocate('KEY-113') == 54
    print('PASS allocation proceeds after real holder releases the lock')

    # Configured directory/base, timestamp exclusion, >999 and fail-closed records.
    (repo / 'features/999-custom').mkdir(parents=True)
    (repo / 'features/20261023-120000-timestamp').mkdir()
    (repo / 'features/999-custom/spec.md').write_text('custom')
    git('add', 'features'); git('commit', '-qm', 'custom specs'); git('push', '-q', 'origin', 'HEAD:release/base')
    assert allocate('KEY-111', extra=('--base', 'release/base', '--specs-dir', 'features')) == 1000
    registry.write_text('{broken')
    try: allocate('KEY-112')
    except subprocess.CalledProcessError: pass
    else: raise AssertionError('corrupt reservations failed open')
    assert registry.read_text() == '{broken'
    print('PASS configured base/directory, timestamp exclusion, 1000; corrupt registry refuses unchanged')
print('OK test_spec_number_alloc (13 scenarios; 3 include negative controls)')
PY
