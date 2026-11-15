#!/usr/bin/env python3
"""Pinned, offline Semgrep CE evidence for immutable PR inputs."""
import argparse
from collections import Counter
import importlib.util
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys
import tempfile

sys.dont_write_bytecode = True
SCRIPTS = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('semgrep_scan', SCRIPTS / 'bureau-scan.py')
scan = importlib.util.module_from_spec(spec)
spec.loader.exec_module(scan)
VERSION = '1.183.0'
# The Python CLI whose options were reviewed. 'python -m semgrep' only prints a
# deprecation and exits 2, and the 'semgrep' launcher execs the native osemgrep CLI.
ENTRY = 'semgrep.console_scripts.pysemgrep'
EXTENSIONS = {'.py', '.pyi', '.js', '.jsx', '.mjs', '.cjs', '.ts', '.tsx', '.mts', '.cts'}
IGNORE = '.bureau/semgrep-ignore.json'
SWITCHES = dict(SEMGREP_SEND_METRICS='off', SEMGREP_ENABLE_VERSION_CHECK='0', DO_NOT_TRACK='1')
FLAGS = ['scan', '--oss-only', '--metrics', 'off', '--disable-version-check', '--novcs',
         '--project-root', '.', '--no-git-ignore', '--x-ignore-semgrepignore-files', '--disable-nosem',
         '--no-rewrite-rule-ids']


class ArtifactUnavailable(Exception):
    pass


def settings(config):
    if not isinstance(config, dict): raise ValueError('configuration must be an object')
    review = config.get('review', {})
    if not isinstance(review, dict): raise ValueError('review must be an object')
    raw = review.get('semgrep', {})
    defaults = dict(enabled=False, mode='advisory', block_severity='ERROR', extra_rules=None)
    if not isinstance(raw, dict) or set(raw) - set(defaults):
        raise ValueError('review.semgrep supports only enabled, mode, block_severity and extra_rules')
    options = {**defaults, **raw}
    if type(options['enabled']) is not bool: raise ValueError('review.semgrep.enabled must be boolean')
    if options['mode'] not in ('advisory', 'blocking'): raise ValueError('review.semgrep.mode must be advisory or blocking')
    if not isinstance(options['block_severity'], str) or options['block_severity'] not in scan.SEVERITY:
        raise ValueError('review.semgrep.block_severity must be INFO, WARNING or ERROR')
    path = options['extra_rules']
    if path is not None and (not scan.relative(path) or PurePosixPath(path).suffix not in ('.yml', '.yaml')):
        raise ValueError('review.semgrep.extra_rules must be null or one repository-relative YAML path')
    return options


def git(repo, *args):
    return subprocess.check_output(['git', '--no-replace-objects', '-C', str(repo), *args],
        stdin=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15,
        env={**scan.module('runtime').process_env(['git']), 'GIT_NO_LAZY_FETCH': '1', 'GIT_OPTIONAL_LOCKS': '0'})


def tree(repo, commit):
    if not re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}', commit): raise ValueError('full pinned commits required')
    entries = {}
    for row in git(repo, 'ls-tree', '-rz', commit).split(b'\0'):
        if row:
            metadata, name = row.split(b'\t', 1)
            entries[name.decode('utf-8')] = metadata.decode('ascii').split()
    return entries


def blob(repo, entries, name, limit=1000000):
    item = entries.get(name)
    if item is None: return None
    if not scan.relative(name) or item[0] not in ('100644', '100755') or item[1] != 'blob':
        raise ValueError('non-regular scan or policy input')
    if int(git(repo, 'cat-file', '-s', item[2])) > limit:
        raise ValueError('scan or policy input exceeds size limit')
    return git(repo, 'cat-file', 'blob', item[2])


def suppressions(data):
    rows = json.loads(data) if data is not None else []
    if not isinstance(rows, list): raise ValueError('base semgrep-ignore must be an array')
    for row in rows:
        if (not isinstance(row, dict) or set(row) != {'file', 'rule_id', 'reason'}
                or not scan.relative(row['file']) or any(c in row['file'] for c in '*?[')
                or not isinstance(row['rule_id'], str) or not re.fullmatch(r'[A-Za-z0-9_.-]+', row['rule_id'])
                or not isinstance(row['reason'], str) or not row['reason'].strip()):
            raise ValueError('base semgrep-ignore requires exact file, rule_id and a named nonempty reason')
    return rows


def native(name):
    parts = PurePosixPath(name).parts
    return any(p in ('.semgrepignore', '.semgrep.yml', '.semgrep.yaml', '.semgrep') for p in parts)


def policy(repo, base_tree, options):
    rules = [('bureau-semgrep-rules.yml', (SCRIPTS / 'bureau-semgrep-rules.yml').read_bytes())]
    if options['extra_rules'] is not None:
        data = blob(repo, base_tree, options['extra_rules'])
        if data is None: raise ValueError('extra_rules absent from pinned base')
        rules.append((options['extra_rules'], data))
    ignores = blob(repo, base_tree, IGNORE)
    # The digest binds exact base-controlled bytes, including an invalid policy.
    bound = dict(rules=[(name, scan.sha(data)) for name, data in rules],
                 suppressions=None if ignores is None else scan.sha(ignores))
    return rules, ignores, scan.digest(bound)


def snapshots(repo, base, head, trees, root):
    changed = git(repo, 'diff', '--no-renames', '--name-only', '-z', base + '...' + head, '--')
    names = sorted({name.decode('utf-8') for name in changed.split(b'\0') if name
                    and PurePosixPath(name.decode('utf-8')).suffix in EXTENSIONS})
    names = [n for n in names if not native(n)]
    result, manifest = {}, {}
    for side in ('base', 'head'):
        directory = root / side
        directory.mkdir(mode=0o700)
        files, inputs = [], []
        for name in names:
            data = blob(repo, trees[side], name)
            if data is None: continue
            target = directory / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            files.append(name)
            inputs.append(dict(file=name, sha256=scan.sha(data)))
        # Native configuration is never copied, including nested config files.
        manifest[side] = dict(files=inputs, removed_native_config=sorted(n for n in trees[side] if native(n)))
        result[side] = (directory, files)
    return result, manifest


def parse_report(value, files, snapshot):
    if (not isinstance(value, dict) or value.get('errors') != [] or not isinstance(value.get('results'), list)
            or not isinstance(value.get('paths'), dict) or not isinstance(value['paths'].get('scanned'), list)):
        raise ValueError('Semgrep errors or malformed JSON contract')
    def path_name(name):
        if not isinstance(name, str): raise ValueError('invalid Semgrep target')
        while name.startswith('./'): name = name[2:]
        if not scan.relative(name) or name not in files: raise ValueError('Semgrep target outside explicit input set')
        return name
    scanned = [path_name(p) for p in value['paths']['scanned']]
    if set(scanned) != set(files): raise ValueError('Semgrep skipped requested targets')
    if value['paths'].get('skipped'): raise ValueError('Semgrep skipped requested targets')
    findings = []
    for raw in value['results']:
        name = path_name(raw['path'])
        rule, severity = raw['check_id'], raw['extra']['severity']
        start, end = raw['start']['line'], raw['end']['line']
        lines = (snapshot / name).read_bytes().split(b'\n')
        if (not isinstance(rule, str) or not re.fullmatch(r'[A-Za-z0-9_.-]+', rule)
                or severity not in scan.SEVERITY or type(start) is not int or type(end) is not int
                or not 1 <= start <= end <= len(lines)):
            raise ValueError('malformed Semgrep finding')
        match = '\n'.join(line.decode('utf-8').strip() for line in lines[start - 1:end])
        findings.append(dict(tool='semgrep', rule_id=rule, file=name, line=start, severity=severity,
                             match_sha256=scan.sha(match.encode())))
    return findings


def compare(base, head, ignores):
    def key(row): return row['rule_id'], row['file'], row['match_sha256']
    old = Counter(map(key, base))
    current = {}
    for row in head: current.setdefault(key(row), []).append(row)
    suppressed_keys = {(r['file'], r['rule_id']) for r in ignores}
    new, existing, suppressed, findings, severities = 0, 0, 0, [], []
    for identity, rows in sorted(current.items()):
        count = len(rows)
        if (identity[1], identity[0]) in suppressed_keys:
            suppressed += count
            continue
        prior = old[identity]
        existing += min(prior, count)
        surplus = max(0, count - prior)
        new += surplus
        if surplus:
            # Show all occurrences: an insertion's particular line is unknowable.
            note = 'count rose from ' + str(prior) + ' to ' + str(count)
            findings += [{**r, 'count_rose': note} for r in rows]
            severities += [r['severity'] for r in rows]
    findings.sort(key=lambda r: (-scan.SEVERITY[r['severity']], r['file'], r['line'], r['rule_id']))
    return dict(new=new, existing=existing, suppressed=suppressed, findings=findings, severities=sorted(set(severities)))


def run_side(venv, prefix, root, env, side, directory, files, rules):
    if not files: return dict(errors=[], results=[], paths=dict(scanned=[]))
    output = root / (side + '.json')
    args = [*FLAGS]
    for path in rules: args += ['--config', str(path)]
    args += ['--json', '--output', str(output), '--timeout', '30', '--max-target-bytes', '1000000', '--', *files]
    proc = scan.run_process([*prefix, *scan.python_argv(venv, root / (side + '-cache'), '-m', ENTRY, *args)],
                            cwd=directory, env=env, timeout=120)
    raw = output.read_text() if output.is_file() else ''
    try: value = json.loads(raw)
    except ValueError: value = dict(unparsed_output=raw)
    return dict(exit_code=proc.returncode, json=value)


def measure(repo, config, base, head, *, reuse=False, pr=None, open_prs=None,
            _state=None, _venv=None, _manifests=None, _prefix=None, _probe=None):
    """Private keyword injections are unit-test-only; the CLI exposes none of them."""
    options = settings(config)
    if not options['enabled']: return None
    settings_digest = scan.digest(config.get('review', {}).get('semgrep', {}))
    state = _state if _state is not None else scan.state_dir(repo)
    manifests = _manifests if _manifests is not None else SCRIPTS
    # Missing inputs still get an identity and durable, explicitly unavailable receipt.
    pin = dict(version=VERSION, lock=scan.file_sha(SCRIPTS / 'bureau-semgrep-lock.txt'))
    # Even an unavailable interpreter/isolation must invalidate an advisory
    # approval when the installed pin assets change; no unverified probe is needed.
    pin['manifests'] = []
    for path in sorted(manifests.glob('bureau-semgrep-manifest-*.json')):
        try: value = scan.file_sha(path)
        except OSError: value = 'unreadable'
        pin['manifests'].append((path.name, value))
    policy_digest = scan.digest(dict(unreadable_policy=True))
    status, reason, report = 'not_measured', 'scan inputs unavailable', {}
    counts = dict(new=0, existing=0, suppressed=0, findings=[], severities=[])
    try:
        trees = dict(base=tree(repo, base), head=tree(repo, head))
        rules, ignore_bytes, policy_digest = policy(repo, trees['base'], options)
        ignores = suppressions(ignore_bytes)
        with tempfile.TemporaryDirectory(prefix='bureau-semgrep-') as tmp:
            root = Path(tmp).resolve()
            sides, manifest = snapshots(repo, base, head, trees, root)
            report['snapshots'] = manifest
            # Applicability needs only source/policy and the shipped pin hashes.
            # Never check or execute scanner prerequisites for an empty input set.
            if not any(files for _, files in sides.values()):
                bound = scan.identity('semgrep', head, base, scan.digest(pin), settings_digest, policy_digest)
                prior = scan.find_receipt(state, bound, options) if reuse else None
                if prior: return prior
                status, reason = 'not_applicable', 'no changed eligible Python, JavaScript or TypeScript files'
            else:
                venv = Path(_venv or os.environ.get('BUREAU_SEMGREP_VENV') or state / 'tools/semgrep/venv').expanduser().resolve()
                env = scan.environment(root, SWITCHES)
                prefix = _prefix if _prefix is not None else scan.network_prefix()
                probe = _probe or scan.probe_network
                if not (venv / 'bin/python').is_file():
                    status, reason = 'unavailable', 'Semgrep venv absent'
                elif not probe(prefix, root, env):
                    status, reason = 'not_measured', 'network isolation unavailable'
                else:
                    system, arch, abi = scan.interpreter(venv, root, env, prefix)
                    manifest_path = manifests / ('bureau-semgrep-manifest-' + system + '-' + arch + '-' + abi + '.json')
                    pin['manifest'] = scan.file_sha(manifest_path) if manifest_path.is_file() else 'missing:' + manifest_path.name
                    try:
                        scan.verify_venv(venv, manifest_path, 'semgrep', VERSION, system, arch, abi,
                                         manifest_sha=pin['manifest'])
                    except (OSError, ValueError, TypeError, KeyError) as exc:
                        status, reason = 'unavailable', str(exc) if isinstance(exc, ValueError) else 'runtime manifest unreadable'
                    else:
                        bound = scan.identity('semgrep', head, base, scan.digest(pin), settings_digest, policy_digest)
                        prior = scan.find_receipt(state, bound, options) if reuse else None
                        if prior: return prior
                        config_dir = root / 'rules'; config_dir.mkdir(mode=0o700)
                        paths = []
                        for index, (_, data) in enumerate(rules):
                            path = config_dir / ('rules-' + str(index) + '.yml')
                            path.write_bytes(data); paths.append(path)
                        findings = {}
                        for side, (directory, files) in sides.items():
                            if files:
                                try:
                                    scan.verify_venv(venv, manifest_path, 'semgrep', VERSION, system, arch, abi,
                                                     manifest_sha=pin['manifest'])
                                except (OSError, ValueError, TypeError, KeyError) as exc:
                                    raise ArtifactUnavailable('runtime artifact changed before ' + side + ' scan') from exc
                            raw = run_side(venv, prefix, root, env, side, directory, files, paths)
                            report[side] = raw
                            if not files: findings[side] = []; continue
                            if raw['exit_code'] != 0: raise ValueError('Semgrep non-zero exit on ' + side)
                            findings[side] = parse_report(raw['json'], files, directory)
                        counts = compare(findings['base'], findings['head'], ignores)
                        status, reason = 'measured', None
    except ArtifactUnavailable as exc:
        status, reason = 'unavailable', str(exc)
    except subprocess.TimeoutExpired:
        status, reason = 'not_measured', 'Semgrep or source snapshot timed out'
    except (OSError, ValueError, TypeError, KeyError, UnicodeError, subprocess.SubprocessError):
        # Do not project raw tool stderr, source, policy content or filesystem errors.
        status, reason = 'not_measured', 'invalid policy, source snapshot or incomplete Semgrep result'
    bound = scan.identity('semgrep', head, base, scan.digest(pin), settings_digest, policy_digest)
    return scan.persist(state, bound, options, status, reason, report, pr=pr, open_prs=open_prs, **counts)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('settings', 'scan'))
    parser.add_argument('--repo', default='.')
    parser.add_argument('--config', required=True)
    parser.add_argument('--base')
    parser.add_argument('--head')
    parser.add_argument('--reuse', action='store_true')
    parser.add_argument('--pr', type=int)
    parser.add_argument('--open-prs', default='null')
    args = parser.parse_args()
    try:
        config = json.loads(Path(args.config).read_text())
        options = settings(config)
        if args.command == 'settings': value = options
        else:
            value = measure(Path(args.repo).resolve(), config, args.base, args.head, reuse=args.reuse,
                            pr=args.pr, open_prs=json.loads(args.open_prs))
        print(scan.encoded(value))
        return 0
    except KeyboardInterrupt: return 130
    except (OSError, ValueError, TypeError, subprocess.SubprocessError):
        print('Semgrep configuration or durable receipt unavailable', file=sys.stderr)
        return 24


if __name__ == '__main__':
    sys.exit(main())
