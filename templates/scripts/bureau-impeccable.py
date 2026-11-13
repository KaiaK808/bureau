#!/usr/bin/env python3
"""Verify operator-installed Impeccable and collect offline design evidence."""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path, PurePosixPath
import platform
import re
import signal
import subprocess
import sys
import tempfile

PINS = Path(__file__).with_name('bureau-impeccable-pins.json')
IGNORE_FILE = '.impeccable/bureau-ignore.json'
UI_EXTENSIONS = {'.html', '.css', '.scss', '.sass', '.less', '.tsx', '.jsx', '.vue', '.svelte', '.astro', '.ts', '.js'}
MECHANICAL = {'gradient-text', 'dark-glow', 'radial-halo', 'radial-spotlight-glow',
              'oversized-h1', 'extreme-negative-tracking'}
REQUIRED = ('SKILL.md', 'reference/shape.md', 'reference/critique.md', 'reference/craft-floor.md',
            'reference/init.md', 'reference/document.md', 'scripts/impeccable',
            'scripts/VERSION', 'scripts/engine-checksums.txt')


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def tree_digest(skill):
    rows = []
    for path in sorted(skill.rglob('*'), key=lambda p: p.relative_to(skill).as_posix()):
        name = path.relative_to(skill).as_posix()
        if name == '.git' or name.startswith(('.git/', 'scripts/bin/')):
            continue
        if path.is_symlink():
            raise ValueError('symlink inside skill tree: ' + name)
        if path.is_file():
            rows.append(name + '\t' + sha256(path.read_bytes()) + '\n')
    return sha256(''.join(rows).encode('utf-8'))


def verify(repo, config, binary=None):
    design = config.get('design', {})
    if not isinstance(design, dict):
        return dict(status='unavailable', reason='design must be an object')
    options = design.get('impeccable', {})
    if not isinstance(options, dict) or options.get('enabled') is not True:
        return dict(status='disabled', reason='opt-in is off')
    try:
        pins = json.loads(PINS.read_text())
        for key in ('skill_version', 'engine_version'):
            if options.get(key, pins[key]) != pins[key]:
                raise ValueError('unsupported pinned ' + key)
        configured = options.get('skill_path')
        if configured is not None and (not isinstance(configured, str) or not configured.strip()):
            raise ValueError('skill_path must be a nonempty path')
        candidates = [Path(configured)] if configured else [Path('.claude/skills/impeccable'), Path('.agents/skills/impeccable')]
        skill = next(((p if p.is_absolute() else repo / p).resolve() for p in candidates
                      if (p if p.is_absolute() else repo / p).is_dir()), None)
        if skill is None:
            raise ValueError('installed skill directory is missing')
        for name in REQUIRED:
            if not (skill / name).is_file():
                raise ValueError('missing skill file: ' + name)
        if not re.search(r'^version: ' + re.escape(pins['skill_version']) + r'\s*$',
                         (skill / 'SKILL.md').read_text(), re.M):
            raise ValueError('skill version mismatch')
        if (skill / 'scripts/VERSION').read_text().strip() != pins['engine_version']:
            raise ValueError('engine version mismatch')
        if tree_digest(skill) != pins['skill_tree_sha256']:
            raise ValueError('skill-tree digest mismatch')
        system = {'Darwin': 'darwin', 'Linux': 'linux'}.get(platform.system())
        arch = {'arm64': 'arm64', 'aarch64': 'arm64', 'x86_64': 'x64', 'AMD64': 'x64'}.get(platform.machine())
        if system is None or arch is None:
            raise ValueError('unsupported engine platform')
        asset = 'impeccable-' + system + '-' + arch
        sums = [line.split() for line in (skill / 'scripts/engine-checksums.txt').read_text().splitlines()]
        expected = [row[2] for row in sums if len(row) == 3 and row[:2] == [pins['engine_version'], asset]]
        if len(expected) != 1 or not re.fullmatch(r'[a-f0-9]{64}', expected[0]):
            raise ValueError('missing pinned engine checksum')
        engine = Path(binary) if binary else skill / 'scripts/bin' / (system + '-' + arch) / 'impeccable'
        engine = engine.resolve()
        if not engine.is_file():
            raise ValueError('engine binary is missing')
        if sha256(engine.read_bytes()) != expected[0]:
            raise ValueError('engine digest mismatch')
        if not os.access(engine, os.X_OK):
            raise ValueError('engine binary is not executable')
        return dict(status='available', reason='verified pinned skill and engine', skill=str(skill), binary=str(engine))
    except (OSError, ValueError, TypeError) as exc:
        return dict(status='unavailable', reason=str(exc))


def relative_path(value):
    if not isinstance(value, str) or not value or any(ord(c) < 32 for c in value) or '\\' in value or ':' in value:
        raise ValueError('expected a local repository-relative file')
    path = PurePosixPath(value)
    if path.is_absolute() or any(p in ('', '.', '..', '.git') for p in value.split('/')):
        raise ValueError('expected a local repository-relative file')
    return value


def ignores(repo):
    path = repo / IGNORE_FILE
    if not path.exists():
        return []
    rows = json.loads(path.read_text())
    if not isinstance(rows, list):
        raise ValueError('ignore file must be an array')
    for row in rows:
        if not isinstance(row, dict) or set(row) != {'file', 'antipattern', 'reason'}:
            raise ValueError('ignore needs file, antipattern and a named reason')
        name = relative_path(row['file'])
        if any(c in name for c in '*?[]{}') or Path(name).suffix not in UI_EXTENSIONS:
            raise ValueError('broad ignore rejected: use an exact UI file')
        if not isinstance(row['antipattern'], str) or not re.fullmatch(r'[a-z0-9]+(?:-[a-z0-9]+)*', row['antipattern']):
            raise ValueError('broad ignore rejected: use one antipattern')
        if not isinstance(row['reason'], str) or not row['reason'].strip():
            raise ValueError('ignore needs a named reason')
    return rows


def git(repo, *args):
    return subprocess.check_output(['git', '-C', str(repo), *args], stderr=subprocess.DEVNULL, timeout=10)


def changed_files(repo, base, head=None):
    target = [base + '...' + head] if head else [base]
    names = git(repo, 'diff', '--name-only', '-z', '--diff-filter=ACMR', *target, '--').split(b'\0')
    if not head:
        names += git(repo, 'ls-files', '--others', '--exclude-standard', '-z').split(b'\0')
    return sorted({relative_path(os.fsdecode(name)) for name in names if name
                   and Path(os.fsdecode(name)).suffix in UI_EXTENSIONS})


def run_engine(installation, directory, files, timeout):
    directory = directory.resolve()
    env = {**os.environ, 'DO_NOT_TRACK': '1', 'IMPECCABLE_NO_TELEMETRY': '1',
           'IMPECCABLE_BIN': installation['binary'], 'IMPECCABLE_SKILL_DIR': installation['skill']}
    cancelled = False

    def latch_cancel(signum, frame):
        nonlocal cancelled
        cancelled = True

    previous = {signum: signal.signal(signum, latch_cancel)
                for signum in (signal.SIGTERM, signal.SIGINT)}
    try:
        # Direct binary execution cannot enter the launcher's download fallback.
        process = subprocess.Popen([installation['binary'], 'detect', '--json', '--no-config',
                                    *('./' + name for name in files)], cwd=directory, env=env,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
        try:
            # Defer launch cancellation until the child handle has cleanup protection.
            for signum in previous:
                signal.signal(signum, cancel)
            if cancelled:
                cancel(signal.SIGTERM, None)
            out, _ = process.communicate(timeout=timeout)
        except BaseException:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.communicate()
            raise
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)
    if process.returncode not in (0, 2):
        raise ValueError('detector exit ' + str(process.returncode))
    rows = json.loads(out)
    if not isinstance(rows, list):
        raise ValueError('malformed detector JSON: expected an array')
    for row in rows:
        if (not isinstance(row, dict) or any(not isinstance(row.get(k), str) or not row[k]
                for k in ('antipattern', 'name', 'description', 'severity', 'category', 'file'))
                or not isinstance(row.get('snippet'), str) or type(row.get('line')) is not int or row['line'] < 0):
            raise ValueError('malformed detector finding')
        path = Path(row['file'])
        path = (path if path.is_absolute() else directory / path).resolve()
        try:
            row['file'] = path.relative_to(directory).as_posix()
        except ValueError:
            row['file'] = path.as_posix()
            row['attributable'] = False
            continue
        relative_path(row['file'])
        if row['file'] not in files:
            raise ValueError('detector finding outside changed UI files')
    if process.returncode == 2 and not rows:
        raise ValueError('findings exit without findings')
    return rows


def detect(repo, installation, base, head=None, tier='all', timeout=30, cache=None):
    if installation['status'] != 'available':
        return dict(status='not measured', reason=installation['reason'], availability=installation['status'])
    try:
        exclusions = ignores(repo)
        files = changed_files(repo, base, head)
        files = [name for name in files if not (repo / name).resolve().is_relative_to(Path(installation['skill']))]
        if not files:
            return dict(status='not applicable', reason='no changed UI files', files=[], findings=[], ignored=[])
        # Review reads the pinned head, never the local validation merge.
        with tempfile.TemporaryDirectory(prefix='bureau-impeccable-') as tmp:
            snapshot = Path(tmp).resolve()
            inputs = []
            for name in files:
                source = repo / name
                if not head and (source.is_symlink() or not source.resolve().is_relative_to(repo)):
                    raise ValueError('UI source symlink rejected')
                data = git(repo, 'show', head + ':' + name) if head else source.read_bytes()
                inputs.append([name, sha256(data)])
                target = snapshot / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data)
            identity = sha256(json.dumps([inputs, exclusions], sort_keys=True).encode())
            if tier == 'all' and cache and cache.is_file():
                prior = json.loads(cache.read_text())
                if prior.get('input_sha256') == identity and prior.get('status') in ('clean', 'findings'):
                    return prior
            rows = run_engine(installation, snapshot, files, timeout)
        if any(row.get('attributable') is False for row in rows):
            return dict(status='not measured', reason='detector finding resolves outside repository', findings=rows)
        if tier == 'mechanical':
            rows = [r for r in rows if r['category'] == 'quality' or r['antipattern'] in MECHANICAL]
        ignored, findings = [], []
        for row in rows:
            match = next((e for e in exclusions if (e['file'], e['antipattern']) == (row['file'], row['antipattern'])), None)
            if match:
                ignored.append({**row, 'reason': match['reason']})
            else:
                findings.append(row)
        report = dict(status='findings' if findings else 'clean', tier=tier, files=files,
                      findings=findings, ignored=ignored, input_sha256=identity)
        if tier == 'all' and cache:
            cache.write_text(json.dumps(report))
        return report
    except subprocess.TimeoutExpired:
        return dict(status='not measured', reason='detector or source snapshot timed out')
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        return dict(status='not measured', reason=str(exc))


def ux_context(installation):
    if installation['status'] != 'available':
        return ''
    skill = Path(installation['skill'])
    return ('\nImpeccable UX guidance (verified, offline):\n'
            f'- Read shape: {skill / "reference/shape.md"}\n'
            f'- Read critique: {skill / "reference/critique.md"}\n'
            f'- Read the craft floor: {skill / "reference/craft-floor.md"}\n'
            '- Read PRODUCT.md and DESIGN.md at the repository root. When missing, create PRODUCT.md using '
            f'{skill / "reference/init.md"} and DESIGN.md using {skill / "reference/document.md"}. '
            'These two root documents are exceptions to the spec-directory-only write boundary; preserve existing content.\n'
            '- Project rules and pinned product/design decisions win over all floor defaults and bans. '
            'Record applicable overrides and their reasons in DESIGN.md; do not invent missing decisions. '
            'Return unresolved decisions through the existing NEEDS_HUMAN/Open Questions contract.\n'
            '- Use the referenced guidance as local reading only. Bureau owns detector execution. '
            'Do not invoke Impeccable commands, hooks, agents, network or browser workflows.\n')


def supplement(directory, role, refs):
    spec = importlib.util.spec_from_file_location('impeccable_review', Path(__file__).with_name('bureau-review.py'))
    review = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(review)
    original = review.load_specialist((directory / (role + '.independent.txt')).read_text(), role, refs)
    late = review.load_specialist((directory / (role + '.late.txt')).read_text(), role, refs)
    return combine_reports(original, late)


def combine_reports(original, late):
    findings = list(original['findings'])
    for finding in late['findings']:
        if finding not in findings:
            findings.append(finding)
    result = {**original, 'findings': findings, 'summary': original['summary'] + ' Late evidence: ' + late['summary']}
    result['counts'] = {s.lower(): sum(f['class'] == s for f in findings) for s in ('CRITICAL', 'BUG', 'MINOR', 'SKIP')}
    result['counts']['critical'] = max(result['counts']['critical'], original['counts']['critical'], late['counts']['critical'])
    for key in ('holds', 'acceptance_gaps', 'questions'):
        if key in original or key in late:
            result[key] = list(dict.fromkeys(original.get(key, []) + late.get(key, [])))
    return result


def cancel(signum, frame):
    raise SystemExit(130)


def main():
    # Unwind engine and snapshot cleanup when the stage is cancelled.
    signal.signal(signal.SIGTERM, cancel)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('verify', 'ux', 'detect', 'supplement'))
    parser.add_argument('--repo', type=Path, default=Path.cwd())
    parser.add_argument('--config', type=Path)
    parser.add_argument('--base', default='origin/main')
    parser.add_argument('--head')
    parser.add_argument('--tier', choices=('mechanical', 'all'), default='all')
    parser.add_argument('--timeout', type=float, default=30)
    parser.add_argument('--cache', type=Path)
    parser.add_argument('--directory', type=Path)
    parser.add_argument('--role', choices=('correctness', 'security', 'performance'))
    parser.add_argument('--refs', default='null')
    args = parser.parse_args()
    if args.command == 'supplement':
        print(json.dumps(supplement(args.directory, args.role, json.loads(args.refs))))
        return
    repo = args.repo.resolve()
    config_path = args.config or repo / '.bureau.json'
    try:
        config = json.loads(config_path.read_text())
        installation = verify(config_path.resolve().parent, config, os.environ.get('IMPECCABLE_BIN'))
    except (OSError, ValueError, TypeError, AttributeError) as exc:
        installation = dict(status='unavailable', reason=str(exc))
    if args.command == 'ux':
        print(ux_context(installation), end='')
    elif args.command == 'verify':
        print(json.dumps(installation))
    else:
        print(json.dumps(detect(repo, installation, args.base, args.head, args.tier, args.timeout, args.cache)))


if __name__ == '__main__':
    sys.dont_write_bytecode = True
    main()
