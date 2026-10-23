#!/bin/bash
# A snapshot/transport error must stop every wrapper before its child starts,
# regardless of errexit or the conditional context suppressing it. The optional
# script tree supports negative controls without modifying production files.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
python3 - "${1:-$ROOT/templates/scripts}" <<'PY'
import os
from pathlib import Path
import subprocess
import sys
import tempfile

scripts = Path(sys.argv[1]).resolve()
checks, failures = 0, []
key = 'lin_api_snapshot_probe_0123456789'
with tempfile.TemporaryDirectory(prefix='bureau-test.snapshot.') as directory:
    temp = Path(directory)
    proxy = temp / 'snapshot-proxy.py'
    # Run the actual isolated parser, mutating its input or output at the
    # boundary. No replacement parser can accidentally accept bad framing.
    proxy.write_text('''import subprocess, sys
fault = sys.argv[1]
data = sys.stdin.buffer.read()
if fault == 'helper_nonzero':
    sys.exit(7)
if fault == 'input_truncated':
    data = data[:-1]
elif fault == 'input_malformed':
    data = data[:-1] + b'NOT_AN_ENV_ENTRY\\0\\0'
result = subprocess.run([sys.executable, *sys.argv[2:]], input=data, capture_output=True)
output = result.stdout
if fault == 'output_truncated':
    output = output.removesuffix(b' END\\n')
elif fault == 'output_malformed':
    output = b'BUREAU_ENV_V1 u\\\\xzz END\\n'
elif fault == 'output_nul':
    output = b'BUREAU_ENV_V1 u\\\\x00 END\\n'
elif fault == 'output_raw_nul':
    output = output.replace(b' END', b'\\0 END')
elif fault == 'output_bad_name':
    output = b'BUREAU_ENV_V1 u\\\\x3d END\\n'
elif fault == 'output_bad_entry':
    output = b'BUREAU_ENV_V1 e' + b''.join(b'\\\\x%02x' % byte for byte in b'NOT_AN_ASSIGNMENT') + b' END\\n'
sys.stdout.buffer.write(output)
sys.stderr.buffer.write(result.stderr)
sys.exit(7 if fault == 'complete_then_nonzero' else result.returncode)
''')
    source = (scripts / 'bureau-env.sh').read_text()
    injected = temp / 'injected'
    missing = temp / 'missing'
    injected.mkdir()
    missing.mkdir()
    invocation = '} | LC_ALL=C "$_BUREAU_ENV_PYTHON" -I -c'
    assert source.count(invocation) == 1, 'snapshot helper invocation changed'
    # Inject at the absolute invocation boundary, keeping the actual parser.
    # A python3 shell function/PATH double must no longer intercept secrets.
    (injected / 'bureau-env.sh').write_text(source.replace(
        invocation, '} | LC_ALL=C "$PROBE_PYTHON" "$SNAPSHOT_PROXY" "$SNAPSHOT_FAULT" -I -c'))
    resolver = 'builtin type -P python3'
    assert source.count(resolver) == 1, 'trusted interpreter resolver changed'
    (missing / 'bureau-env.sh').write_text(source.replace(resolver, 'builtin false'))
    (temp / 'operator.env').write_text('LINEAR_API_KEY=' + key + '\n')
    (temp / 'empty-path').mkdir()
    base = dict(PATH=os.environ['PATH'], HOME=directory, LC_ALL='C',
                BUREAU_ENV_FILE=str(temp / 'operator.env'), LINEAR_API_KEY=key,
                CARRIER='prefix-' + key + '-suffix', INNOCENT='stays',
                SNAPSHOT_PROXY=str(proxy), PROBE_PYTHON=sys.executable)
    child = '/bin/bash --noprofile --norc -c \'printf CHILD_RAN\''
    calls = {
        'without': 'bureau_without_secrets ' + child,
        'untrusted-default': 'bureau_untrusted_env ' + child,
        'untrusted-clean': 'bureau_untrusted_env ' + child,
        'runtime': 'bureau_exec_runtime ' + child,
        'git': 'git status',
        'gh': 'gh api',
        'curl-scrub': '( _bureau_drop_secrets; ' + child + ' )',
    }
    # git/gh stubs are reached only if the filter wrongly launches its child.
    bindir = temp / 'bin'
    bindir.mkdir()
    for name in ('git', 'gh'):
        path = bindir / name
        path.write_text('#!/bin/bash\nprintf CHILD_RAN\n')
        path.chmod(0o755)
    base['PATH'] = str(bindir) + ':' + base['PATH']
    contexts = {
        'direct': '{call}',
        'if': 'if {call}; then :; else :; fi',
        'or': '{call} || :',
        'subshell': '( {call} ); exit $?',
    }
    faults = ('helper_nonzero', 'complete_then_nonzero', 'input_truncated',
              'input_malformed', 'output_truncated', 'output_malformed',
              'output_nul', 'output_raw_nul', 'producer_write', 'consumer_read',
              'consumer_write', 'decode_write', 'missing_python', 'pipe_unavailable',
              'output_bad_name', 'output_bad_entry')
    for fault in faults:
        for name, call in calls.items():
            mode = 'clean' if name == 'untrusted-clean' else 'default'
            setup = 'source "$1/bureau-env.sh"\nbureau_get() { builtin printf "%s" ' + mode + '; }\n'
            target = scripts
            if fault == 'missing_python':
                target = missing
            elif fault == 'pipe_unavailable':
                # Only descriptors 0..3 may remain: the snapshot cannot obtain
                # the descriptors for its command-substitution/producer pipe.
                setup += 'ulimit -n 4\n'
            elif fault == 'consumer_read':
                setup += 'read() { return 2; }\n'
            elif fault in ('producer_write', 'consumer_write', 'decode_write'):
                condition = {'producer_write': '[ "$1" = \'%s\\0\' ]',
                             'consumer_write': '[ "$1" = \'%s\' ]',
                             'decode_write': '[ "$1" = -v ]'}[fault]
                setup += 'printf() { if ' + condition + '; then return 1; fi; builtin printf "$@"; }\n'
            else:
                target = injected
            for context, pattern in contexts.items():
                for errexit in (False, True):
                    command = pattern.format(call=call)
                    # A scrub necessarily exits only its own subshell. Preserve
                    # that status in conditional callers while still probing
                    # the same errexit suppression as real curl call sites.
                    if name == 'curl-scrub' and context in ('if', 'or'):
                        command = ('if ' + call + '; then :; else exit $?; fi' if context == 'if'
                                   else call + ' || exit $?')
                    code = setup + ('set -e\n' if errexit else 'set +e\n') + command
                    result = subprocess.run(['/bin/bash', '-uc', code, '_', str(target)],
                                            env=dict(base, SNAPSHOT_FAULT=fault),
                                            capture_output=True, timeout=10)
                    label = f'{fault}/{name}/{context}/errexit={errexit}'
                    checks += 1
                    if result.returncode != 24 or b'CHILD_RAN' in result.stdout + result.stderr:
                        failures.append(label)
                        print('FAIL', label, 'rc=', result.returncode, result.stdout[:100], result.stderr[:200])
                    if key.encode() in result.stdout + result.stderr or key.encode().hex().encode() in result.stdout + result.stderr:
                        failures.append(label + ': secret printed')
        print('CHECKED', fault)

    # Successful snapshots need no writable TMPDIR; retain child exit status,
    # multiline bytes and ordinary variables beside removal of the carrier.
    for mode in ('default', 'clean'):
        code = ('source "$1/bureau-env.sh"\nbureau_get() { builtin printf "%s" ' + mode + '; }\n'
                'bureau_untrusted_env /usr/bin/env')
        result = subprocess.run(['/bin/bash', '-uc', code, '_', str(scripts)],
                                env=dict(base, TMPDIR=str(temp / 'does-not-exist'),
                                         LANG='line one\nline two\n'), capture_output=True, timeout=10)
        checks += 1
        if result.returncode or key.encode() in result.stdout + result.stderr or b'LANG=line one\nline two\n' not in result.stdout:
            failures.append('no-tempfile success: ' + mode)

print(f'{checks} snapshot checks, {len(failures)} failed')
sys.exit(bool(failures))
PY
