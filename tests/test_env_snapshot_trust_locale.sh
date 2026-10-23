#!/bin/bash
# The snapshot interpreter must precede branch PATH lookup; its length rule
# must agree with the running Bash even behind caller-local locale variables.
# Optional script tree and section let each finding run as a negative control.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
python3 - "${1:-$ROOT/templates/scripts}" "${2:-all}" <<'PY'
import locale
import os
from pathlib import Path
import subprocess
import sys
import tempfile

scripts = Path(sys.argv[1]).resolve()
section = sys.argv[2]
assert section in ('all', 'trust', 'locale', 'empty')
checks, failures = 0, []
key = 'lin_api_trusted_snapshot_0123456789'

def check(ok, label, result=None):
    global checks
    checks += 1
    if not ok:
        failures.append(label)
        # Never echo the environment (including the fake interpreter's input).
        print('FAIL', label, 'rc=' + str(result.returncode) if result else '')

def run(tree, code, env):
    return subprocess.run(['/bin/bash', '-uc', code, '_', str(tree)], env=env,
                          capture_output=True, timeout=15)

def entries(output):
    return dict(entry.split(b'=', 1) for entry in output.split(b'\0') if b'=' in entry)

with tempfile.TemporaryDirectory(prefix='bureau-test.snapshot-trust-locale.') as directory:
    temp = Path(directory)
    bindir = temp / 'branch-bin'
    bindir.mkdir()
    fake = bindir / 'python3'
    captured = temp / 'intercepted-input'
    fake.write_text('#!/bin/bash\n/bin/cat > "$PROBE_CAPTURE"\n'
                    'printf "BUREAU_ENV_V1 END\\n"\n')
    fake.chmod(0o755)
    base = dict(PATH=os.environ['PATH'], HOME=directory, LC_ALL='C',
                PROBE_FAKE=str(fake), PROBE_CAPTURE=str(captured), INNOCENT='stays')

    if section in ('all', 'trust'):
        # A valid forged frame could preserve the carrier while removing no
        # secrets. Check both the fake's stdin and the actual child's decision.
        setups = {
            'path-before-load': 'PATH="${PROBE_FAKE%/*}:$PATH"; source "$1/bureau-env.sh";',
            'path-after-load': 'source "$1/bureau-env.sh"; PATH="${PROBE_FAKE%/*}:$PATH";',
            'hash-before-load': 'hash -p "$PROBE_FAKE" python3; source "$1/bureau-env.sh";',
            'function-before-load': 'python3() { "$PROBE_FAKE" "$@"; }; source "$1/bureau-env.sh";',
        }
        for label, setup in setups.items():
            for mode in ('default', 'clean'):
                captured.unlink(missing_ok=True)
                code = (setup + ' LINEAR_API_KEY="' + key + '"; export -n LINEAR_API_KEY; '
                        'bureau_get() { printf "%s" ' + mode + '; }; '
                        'bureau_untrusted_env /usr/bin/env -0')
                result = run(scripts, code, dict(base, CARRIER='prefix-' + key + '-suffix',
                             TZ='prefix-' + key + '-suffix', _BUREAU_ENV_PYTHON=str(fake)))
                child = entries(result.stdout)
                if captured.exists():
                    print('DETAIL', label, mode, 'fake received secret:', key.encode() in captured.read_bytes())
                check(not captured.exists(), label + '/' + mode + ': fake never receives input', result)
                check(result.returncode == 0 and key.encode() not in result.stdout
                      and b'TZ' not in child and b'CARRIER' not in child,
                      label + '/' + mode + ': forged decision cannot preserve carrier', result)
                check(child.get(b'HOME') == directory.encode(), label + '/' + mode + ': child ran', result)

    # Prefer the reviewer's exact locale; keep the test portable to Linux
    # installations that only provide C.UTF-8. Bash itself verifies support.
    utf8 = None
    for candidate in ('en_US.UTF-8', 'C.UTF-8', 'UTF-8'):
        result = run(scripts, 'p=$\'\\303\\244\'; printf "%s" "${#p}"', dict(base, LC_ALL=candidate))
        if result.returncode == 0 and result.stdout == b'1':
            utf8 = candidate
            break

    def parity(tree, env, shadow, mode, label, hidden=False):
        # This is the original Bash length + exact/embedded comparison, in the
        # same scope and effective locale as the wrapper call. Do not infer an
        # expected result from locale names or the helper's implementation.
        code = ('source "$1/bureau-env.sh"; '
                'bureau_get() { printf "%s" ' + mode + '; }; '
                '_probe_key=$LINEAR_API_KEY; /usr/bin/true; '
                '_probe() { ' + ('local ' + shadow + '; ' if shadow else '')
                + ('local LINEAR_API_KEY; ' if hidden else '')
                + '_probe_keep=1; '
                'if [ -n "$_probe_key" ] && { [ "$TZ" = "$_probe_key" ] || '
                '{ [ "${#_probe_key}" -ge 6 ] && [[ $TZ == *"$_probe_key"* ]]; }; }; '
                'then _probe_keep=0; fi; '
                'printf "%s %s\\n" "${#_probe_key}" "$_probe_keep"; '
                'bureau_untrusted_env /usr/bin/env -0; }; _probe')
        result = run(tree, code, env)
        header, _, output = result.stdout.partition(b'\n')
        child = entries(output)
        expected_keep = header.split()[-1:] == [b'1']
        check(result.returncode == 0 and header.split()[-1:] in ([b'0'], [b'1'])
              and ((b'TZ' in child) == expected_keep)
              and (not expected_keep or child[b'TZ'] == env['TZ'].encode('utf-8', 'surrogateescape')),
              label + ': TZ agrees with Bash', result)
        if mode == 'default':
            check((b'CARRIER' in child) == expected_keep,
                  label + ': carrier agrees with Bash', result)
        check(b'LINEAR_API_KEY' not in child and b'GH_TOKEN' not in child
              and child.get(b'HOME') == directory.encode(), label + ': named secrets removed, child ran', result)

    if section in ('all', 'locale'):
        if utf8 is None:
            print('SKIP no installed UTF-8 locale for caller-shadow parity')
        else:
            for exported in ('LC_ALL', 'LC_CTYPE', 'LANG'):
                env = {k: v for k, v in base.items() if k not in ('LC_ALL', 'LC_CTYPE', 'LANG')}
                env[exported] = utf8
                for shadow in ('LC_ALL', 'LC_CTYPE', 'LANG', 'LC_ALL LC_CTYPE LANG'):
                    for secret, exact in (('äääää', False), ('ääääää', False),
                                          ('äääää', True), ('abcde', False)):
                        carrier = secret if exact else 'prefix-' + secret + '-suffix'
                        for mode in ('default', 'clean'):
                            for hidden in (False, True):
                                label = f'{exported}={utf8}/local={shadow}/{len(secret)}/exact={exact}/{mode}/hidden={hidden}'
                                parity(scripts, dict(env, LINEAR_API_KEY=secret, TZ=carrier, CARRIER=carrier),
                                       shadow, mode, label, hidden)
            print('CHECKED UTF-8 Bash parity, including exported LC_ALL and all three caller-local names:', utf8)

        # libc and Python codec tables can also disagree on valid legacy
        # multibyte sequences. Keep these installed-locale controls for hidden
        # exports, where the helper cannot reuse a visible secret's length.
        legacy_seen = 0
        for name, raw in (('zh_CN.GB2312', b'\xa4\xa2' * 5),
                          ('ja_JP.SJIS', b'\x82\xa0' * 5),
                          ('ja_JP.eucJP', b'\xc3\xa4' * 5),
                          ('ko_KR.CP949', b'\x82\xa0' * 5)):
            try:
                locale.setlocale(locale.LC_CTYPE, name)
            except locale.Error:
                continue
            secret = os.fsdecode(raw)
            carrier = 'prefix-' + secret + '-suffix'
            for mode in ('default', 'clean'):
                parity(scripts, dict(base, LC_ALL=name, LINEAR_API_KEY=secret,
                                     TZ=carrier, CARRIER=carrier), 'LC_ALL LC_CTYPE LANG', mode,
                       'legacy ' + name + '/' + mode, hidden=True)
            legacy_seen += 1
        if legacy_seen:
            print('CHECKED legacy multibyte Bash parity:', legacy_seen, 'installed locales')
        else:
            print('SKIP no installed legacy multibyte locales for additional parity controls')

    if section in ('all', 'empty'):
        empty_locale = None
        available = subprocess.check_output(['/usr/bin/locale', '-a'], text=True).splitlines()
        for candidate in ['fa_IR'] + available:
            try:
                locale.setlocale(locale.LC_CTYPE, candidate)
                if not locale.nl_langinfo(locale.CODESET):
                    empty_locale = candidate
                    break
            except locale.Error:
                pass
        if empty_locale is not None:
            for mode in ('default', 'clean'):
                for secret in ('ascii_secret_012345', 'äääää'):
                    carrier = 'prefix-' + secret + '-suffix'
                    parity(scripts, dict(base, LC_ALL=empty_locale, GH_TOKEN='ascii_token_012345',
                                         LINEAR_API_KEY=secret, TZ=carrier, CARRIER=carrier),
                           '', mode, 'installed empty CODESET ' + empty_locale + '/' + mode)
            print('CHECKED installed locale with empty CODESET:', empty_locale)
        else:
            print('SKIP no installed locale with empty CODESET; simulation below is mandatory')

        # Exercise the fallback even on hosts with no such installed locale,
        # and in both byte-counting and UTF-8 shells. Only the libc result is
        # injected; run the same real framing, parser and wrapper decisions.
        simulated = temp / 'empty-codeset'
        simulated.mkdir()
        source = (scripts / 'bureau-env.sh').read_text()
        query = 'import locale\n'
        assert source.count(query) == 1, 'locale import changed'
        (simulated / 'bureau-env.sh').write_text(source.replace(
            query, query + 'locale.nl_langinfo = lambda item: ""\n'))
        for name in ('C', utf8):
            if name is None:
                continue
            for mode in ('default', 'clean'):
                parity(simulated, dict(base, LC_ALL=name, GH_TOKEN='ascii_token_012345',
                                      LINEAR_API_KEY='ascii_secret_012345',
                                      TZ='innocent', CARRIER='innocent'), '', mode,
                       'simulated empty CODESET ' + name + '/' + mode)
        print('CHECKED simulated empty CODESET with an exported ASCII GH_TOKEN')

print(f'{checks} snapshot trust/locale checks, {len(failures)} failed')
sys.exit(bool(failures))
PY
