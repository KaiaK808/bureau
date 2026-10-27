#!/bin/bash
# Caller locals must not hide exported secret carriers from any filter.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
python3 - "$ROOT" "${1:-$ROOT/templates/scripts}" <<'PY'
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import tempfile

root, scripts = map(Path, sys.argv[1:])
key = 'lin_api_fake_0123456789'
functions = {}
for path in sorted((root / 'templates/scripts').glob('*.sh')):
    for match in re.finditer(r'^([A-Za-z_]\w*)\(\) \{\n(.*?)^\}', path.read_text(), re.M | re.S):
        # Ignore prose comments; match shell calls conservatively (also git/gh
        # wrappers), then follow their callers so enclosing scopes are covered.
        body = '\n'.join(line for line in match[2].splitlines() if not line.lstrip().startswith('#'))
        body = body.replace('\\\n', ' ')
        names = set()
        for declaration in re.finditer(r'(?:^\s*|;\s*)local\s+([^;\n]+)', body, re.M):
            for token in shlex.split(declaration[1]):
                name = token.split('=', 1)[0]
                if re.fullmatch(r'[A-Za-z_]\w*', name):
                    names.add(name)
        functions[match[1]] = (path.name, body, names)

callers = {'_bureau_env_build', 'bureau_without_secrets', 'bureau_untrusted_env',
           'bureau_exec_runtime', '_bureau_drop_secrets', 'git', 'gh'}
while True:
    pattern = r'(?<![\w/-])(?:' + '|'.join(map(re.escape, callers)) + r')\s'
    found = {name for name, (_, body, _) in functions.items() if re.search(pattern, body)}
    if found <= callers:
        break
    callers |= found
assert functions['run_stage_for'][2] == {'stage', 'temp', 'rc', 'system', 'schema', 'gate_context', 'args'}
failures, count = [], 0
with tempfile.TemporaryDirectory(prefix='bureau-test.shadowed-callers.') as directory:
    temp = Path(directory)
    (temp / 'operator.env').write_text('LINEAR_API_KEY=' + key + '\n')
    (temp / 'bin').mkdir()
    for name in ('git', 'gh'):
        command = temp / 'bin' / name
        command.write_text('#!/bin/sh\nexec /usr/bin/env -0\n')
        command.chmod(0o755)
    base = dict(PATH=str(temp / 'bin') + ':' + os.environ['PATH'], HOME=directory,
                BUREAU_ENV_FILE=str(temp / 'operator.env'), INNOCENT='stays',
                TMPDIR=os.environ.get('TMPDIR', directory), LC_ALL='C')
    calls = ['bureau_without_secrets /usr/bin/env', 'bureau_untrusted_env /usr/bin/env',
             'bureau_exec_runtime /usr/bin/env', '( _bureau_drop_secrets; /usr/bin/env )',
             'git status', 'gh api']
    for caller in sorted(callers & functions.keys()):
        file, _, names = functions[caller]
        if not names:
            continue
        # Every discovered local is shadowed as an uninitialized binding,
        # the Bash 3.2 case where the inherited value survives in a child.
        for call in calls:
            env = dict(base, **{name: 'prefix-' + key + '-suffix' for name in names})
            code = ('source "$1/bureau-env.sh"; shift; LINEAR_API_KEY="' + key + '"; '
                    # Warm Bash 3.2's export cache before introducing locals:
                    # this is how a long-running stage retains the old value.
                    '/usr/bin/true; _probe() { local "$@"; ' + call + '; }; _probe "$@"')
            result = subprocess.run(['/bin/bash', '-uc', code, '_', str(scripts), *sorted(names)],
                                    env=env, capture_output=True, text=True, timeout=15)
            count += len(names)
            if result.returncode or key in result.stdout + result.stderr or 'INNOCENT=stays' not in result.stdout:
                failures.append(file + ':' + caller + ' via ' + call)
                print('DETAIL', result.returncode, [line.split('=', 1)[0] for line in result.stdout.splitlines() if key in line],
                      result.stderr[:200])
        print(f'CALLER {file}:{caller}: ' + ', '.join(sorted(names)))

    # Execute run_stage_for itself as well: real locals, prompt files and
    # provider invocation; the fake provider only reads its environment.
    config = (scripts / 'bureau-config.sh').read_text()
    stage = re.search(r'^run_stage_for\(\) \{\n.*?^\}', config, re.M | re.S).group()
    provider = temp / 'bureau-provider.py'
    provider.write_text('import os, sys\nsys.stdout.write("\\n".join(k+"="+v for k,v in os.environ.items()))\n')
    for name in sorted(functions['run_stage_for'][2]):
        # One at a time: assigning another exported local (such as temp)
        # would invalidate Bash's cache and mask the uninitialized rc case.
        for options in (None, 'braceexpand:hashall:interactive-comments', 'allexport:braceexpand:hashall:interactive-comments'):
            env = dict(base, BUREAU_RUNTIME=str(temp / 'bureau-runtime.py'), BUREAU_CONFIG=str(temp / '.bureau.json'),
                       **{name: 'prefix-' + key + '-suffix'})
            if options:
                env['SHELLOPTS'] = options
            result = subprocess.run(['/bin/bash', '-uc', 'source "$1/bureau-env.sh"\n'
                                     + stage + '\nLINEAR_API_KEY="' + key + '"\n/usr/bin/true\nrun_stage_for implement fixture',
                                     '_', str(scripts)], env=env, capture_output=True, text=True, timeout=15)
            if result.returncode or key in result.stdout + result.stderr or 'INNOCENT=stays' not in result.stdout:
                failures.append('actual run_stage_for: ' + name + ' leaked or failed; SHELLOPTS=' + str(options))

    result = subprocess.run(['/bin/bash', '-uc', 'source "$1/bureau-env.sh"; bureau_without_secrets /usr/bin/env',
                             '_', str(scripts)], env=dict(base, rc='ordinary-rc'),
                            capture_output=True, text=True, timeout=15)
    if result.returncode or 'rc=ordinary-rc' not in result.stdout:
        failures.append('ordinary application rc was reserved or lost')

    # env -u removes a malformed host name literally; Bash unset would
    # evaluate its subscript. The in-shell scrub must refuse before that.
    sentinel = temp / 'host-name-sentinel'
    evil_name = 'HERDR_PROBE[$(touch "' + str(sentinel) + '")]'
    for call in calls:
        sentinel.unlink(missing_ok=True)
        result = subprocess.run(['/bin/bash', '-uc', 'source "$1/bureau-env.sh"; LINEAR_API_KEY="' + key + '"; '
                                 + call, '_', str(scripts)], env=dict(base, **{evil_name: 'x'}),
                                capture_output=True, text=True, timeout=15)
        expected = 24 if '_bureau_drop_secrets' in call else 0
        if result.returncode != expected or sentinel.exists() or 'HERDR_PROBE' in result.stdout:
            failures.append('malformed host name executed or survived via ' + call)

    # Literal NUL framing preserves newlines, including a secret on the next
    # line of a shadowed carrier; clean mode must use the real exported value.
    for mode in ('default', 'clean'):
        env = dict(base, LANG='prefix\n' + key + '\nsuffix', ORDINARY='line one\nline two')
        code = ('source "$1/bureau-env.sh"; LINEAR_API_KEY="' + key + '"; '
                'bureau_get() { printf "%s" "' + mode + '"; }; '
                '/usr/bin/true; _probe() { local LANG; bureau_untrusted_env /usr/bin/env; }; _probe')
        result = subprocess.run(['/bin/bash', '-uc', code, '_', str(scripts)], env=env,
                                capture_output=True, text=True, timeout=15)
        if result.returncode or key in result.stdout + result.stderr:
            failures.append(mode + ': multiline shadowed LANG leaked')
        if mode == 'default' and 'ORDINARY=line one\nline two' not in result.stdout:
            failures.append('multiline innocent value changed')

    # Model the command-substitution export-cache rebuild on Bash 3.2 too.
    # Bash 5.2 refreshes exported SHELLOPTS when entering $(...), even without
    # errexit; changing pipefail inside the producer causes the same mismatch
    # here. Compare real child environments, never ${!name} or export -p.
    modeled = temp / 'cache-rebuild'
    modeled.mkdir()
    source = (scripts / 'bureau-env.sh').read_text()
    anchor = '_beb_snapshot=$({\n'
    assert source.count(anchor) == 1, 'snapshot boundary changed'
    (modeled / 'bureau-env.sh').write_text(source.replace(anchor, anchor + '    set -o pipefail\n'))
    local_names = sorted(set().union(*(functions[caller][2] for caller in callers & functions.keys())))
    internal = set(re.search(r"^_BUREAU_ENV_INTERNAL_NAMES='(.*?)'", source, re.M | re.S)[1].split())
    modeled_checks = 0
    for options in (None, 'braceexpand:hashall:interactive-comments', 'allexport:braceexpand:hashall:interactive-comments'):
        for secret in (False, True):
            for call in calls:
                env = dict(base, **{name: 'prefix-' + key + '-suffix' if secret else 'ordinary-' + name
                                    for name in local_names if name not in internal})
                if options:
                    env['SHELLOPTS'] = options
                # exec itself rebuilds Bash 3.2's environment. Match the
                # runtime wrapper's launch shape in its unfiltered control.
                raw_call = '(exec /usr/bin/env -0)' if call.startswith('bureau_exec_runtime ') else '/usr/bin/env -0'
                code = ('source "$1/bureau-env.sh"; shift; LINEAR_API_KEY="' + key + '"; '
                        '/usr/bin/true; _probe() { local "$@"; '
                        'printf "RAW\\0"; ' + raw_call + '; printf "FILTERED\\0"; '
                        + call.replace('/usr/bin/env', '/usr/bin/env -0') + '; }; _probe "$@"')
                result = subprocess.run(['/bin/bash', '-uc', code, '_', str(modeled), *local_names],
                                        env=env, capture_output=True, timeout=15)
                raw, separator, filtered = result.stdout.partition(b'FILTERED\0')
                before = dict(entry.split(b'=', 1) for entry in raw.removeprefix(b'RAW\0').split(b'\0') if b'=' in entry)
                after = dict(entry.split(b'=', 1) for entry in filtered.split(b'\0') if b'=' in entry)
                modeled_checks += 1
                label = f'cache rebuild: secret={secret}/{call}/SHELLOPTS={options}'
                if result.returncode or not separator or b'INNOCENT=stays' not in filtered or key.encode() in filtered + result.stderr:
                    failures.append(label)
                if not call.startswith('bureau_exec_runtime ') and before.get(b'args') != env['args'].encode():
                    failures.append(label + ': unfiltered child did not inherit args')
                if not secret:
                    for name in local_names:
                        value = ('ordinary-' + name).encode()
                        if before.get(name.encode()) == value and name not in internal and after.get(name.encode()) != value:
                            failures.append(label + ': innocent ' + name + ' lost')
    print(f'{modeled_checks} deterministic child-environment cache-rebuild probes across {len(local_names)} caller-local names')

print(f'{count} caller-local/filter combinations plus 21 actual run_stage_for, ordinary rc and multiline probes; {len(failures)} failed')
for failure in failures:
    print('FAIL ' + failure)
sys.exit(bool(failures))
PY
