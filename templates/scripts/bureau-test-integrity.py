#!/usr/bin/env python3
"""Advisory static test integrity, reading only pinned Git objects."""
import argparse
import ast
from collections import Counter, defaultdict
from contextlib import contextmanager
import importlib.util
import json
import os
from pathlib import Path
import posixpath
import re
import signal
import subprocess
import sys
import time

sys.dont_write_bytecode = True
spec = importlib.util.spec_from_file_location('integrity_acceptance', Path(__file__).with_name('bureau-acceptance.py'))
a = importlib.util.module_from_spec(spec); spec.loader.exec_module(a)
p, e = a.p, a.e
safe_path = e.integrity_path

MAX_FILES = 200
MAX_FILE_BYTES = 1024 * 1024
BUDGET_SECONDS = 60
JS_EXTENSIONS = ('.js', '.jsx', '.ts', '.tsx', '.mjs', '.cjs', '.json')
GIT_CONTEXT = ('GIT_INDEX_FILE', 'GIT_DIR', 'GIT_WORK_TREE', 'GIT_OBJECT_DIRECTORY',
               'GIT_ALTERNATE_OBJECT_DIRECTORIES', 'GIT_COMMON_DIR', 'GIT_NAMESPACE',
               'GIT_PREFIX', 'GIT_CEILING_DIRECTORIES')
RECOGNISERS = {
    'python': dict(assertions=(), skips=('pytest.mark.skip', 'pytest.mark.skipif', 'pytest.mark.xfail',
                    'pytest.skip', 'unittest.skip', 'unittest.skipIf', 'unittest.skipUnless', 'unittest.expectedFailure')),
    'javascript': dict(assertions=(r'\bexpect\s*\(', r'\bassert\s*\(', r'\bassert\s*\.\s*\w+\s*\(', r'\.should\.'),
                       skips=(r'\.(skip|only|todo)(?![\w$])', r'(?<![\w$])(xit|xdescribe|xtest)(?![\w$])')),
    'go': dict(assertions=(r'\bt\.(?:Errorf?|Fatalf?|FailNow|Fail)\s*\(',
                           r'\b(?:assert|require)\.[A-Z]\w*\s*\('),
               skips=(r'\bt\.(Skip|Skipf|SkipNow)\b',)),
    'rust': dict(assertions=(r'\b(?:debug_)?assert(?:_eq|_ne)?\s*!', r'\bpanic\s*!', r'#\s*\[\s*should_panic\b'),
                 skips=(r'#\s*\[\s*(ignore)\b',)),
    'shell': dict(assertions=(r'(?<![\w$.-])(?:assert\w*|fail)\b',
                              r'\|\|\s*(?:exit|return)\s+([+-]?[0-9]+)\b', r'\|\|[ \t]*\{(?=[^\n]*\bFAIL\b)'),
                  skips=(r'\b(?:exit|return)\s+0\b',)),
}
JS_IMPORT = re.compile(
    r'\b(?:import|export)\s*(?:type\s+)?'
    r'(?:\*(?:\s+as\s+[\w$]+)?|[\w$]+(?:\s*,\s*(?:\{[^}]*\}|\*\s+as\s+[\w$]+))?|\{[^}]*\})\s*\bfrom\s*'
    r'(?P<from>["\'])(?P<spec_from>(?:\\.|[^\\])*?)(?P=from)'
    r'|\b(?:require|import)\s*\(\s*(?P<call>["\'])(?P<spec_call>(?:\\.|[^\\])*?)(?P=call)\s*\)'
    r'|\bimport\s*(?P<side>["\'])(?P<spec_side>(?:\\.|[^\\])*?)(?P=side)', re.S)


class Interrupted(Exception):
    pass


class FileUnavailable(Exception):
    pass


@contextmanager
def git_context():
    inherited = {name: os.environ.pop(name) for name in GIT_CONTEXT if name in os.environ}
    try: yield
    finally: os.environ.update(inherited)


@contextmanager
def budget(started):
    # Also bound AST/regex work, not just Git children. Never run PR code.
    def expire(signum, frame): raise TimeoutError('budget exhausted')
    def stop(signum, frame): raise Interrupted()
    previous = {sig: signal.signal(sig, stop) for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP)}
    previous[signal.SIGALRM] = signal.signal(signal.SIGALRM, expire)
    entered = time.monotonic()
    timer = signal.setitimer(signal.ITIMER_REAL, max(.001, started + BUDGET_SECONDS - entered))
    try: yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        for sig, handler in previous.items(): signal.signal(sig, handler)
        if timer[0]: signal.setitimer(signal.ITIMER_REAL, max(.001, timer[0] - (time.monotonic() - entered)), timer[1])


class Git:
    def __init__(self, repo, started):
        self.repo, self.deadline = repo, started + BUDGET_SECONDS

    def prepare(self, command):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0: raise TimeoutError('budget exhausted')
        env = dict(p.process_env(command), GIT_NO_LAZY_FETCH='1', GIT_OPTIONAL_LOCKS='0')
        return subprocess.check_output(command, cwd=self.repo, stdin=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                       env=env, timeout=remaining)

    def __call__(self, *args):
        return self.prepare(['git', '-C', str(self.repo), '--literal-pathspecs', *args])

    def contents(self, revision, path):
        if not safe_path(path): raise FileUnavailable('unsafe path')
        obj = revision + ':' + path
        try:
            if int(self('cat-file', '-s', obj)) > MAX_FILE_BYTES:
                raise FileUnavailable('file size limit exceeded')
            return self('show', '--no-ext-diff', '--no-textconv', obj, '--')
        except (OSError, ValueError, subprocess.CalledProcessError): raise FileUnavailable('file unavailable')


def language(path):
    suffix = Path(path).suffix.lower()
    if suffix == '.py': return 'python'
    if suffix in JS_EXTENSIONS[:-1]: return 'javascript'
    if path.endswith('_test.go'): return 'go'
    if suffix == '.rs': return 'rust'
    if suffix in ('.sh', '.bash'): return 'shell'
    return None


def dotted(node):
    if isinstance(node, ast.Name): return node.id
    if isinstance(node, ast.Attribute):
        prefix = dotted(node.value)
        return prefix + '.' + node.attr if prefix else ''
    return ''


def lexical(source, family):
    # Rust attributes are assertion/skip tokens, not comments.
    prefixes = r'//|/\*|\*|#(?![ \t]*\[)' if family == 'rust' else r'//|#|/\*|\*'
    return re.sub(r'(?m)^[ \t]*(?:' + prefixes + r')[^\n]*', '', source)


def analyse(raw, family):
    source = raw.decode('utf-8', 'replace')
    if family == 'python':
        try: tree = ast.parse(raw)
        except (SyntaxError, ValueError, RecursionError): raise FileUnavailable('Python parse error')
        assertions = 0; markers = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Assert): assertions += 1
            if isinstance(node, ast.Call):
                method = node.func.attr if isinstance(node.func, ast.Attribute) else ''
                if method.startswith('assert') or method == 'fail' or dotted(node.func) in ('pytest.raises', 'pytest.warns'):
                    assertions += 1
            if isinstance(node, ast.Attribute):
                name = dotted(node)
                if name in RECOGNISERS[family]['skips']:
                    markers.append((name, node.lineno))
        return assertions, markers, source, tree
    code = lexical(source, family)
    rules = RECOGNISERS[family]
    if family == 'shell':
        command, failure, group = rules['assertions']
        assertions = len(list(re.finditer(command, code)))
        assertions += sum(int(match.group(1)) != 0 for match in re.finditer(failure, code))
        assertions += len(list(re.finditer(group, code)))
        markers = []; previous = ''
        for number, (raw_line, line) in enumerate(zip(source.splitlines(), code.splitlines()), 1):
            for match in re.finditer(rules['skips'][0], line):
                if re.search(r'\bskip\b', raw_line[:match.start()] + ' ' + previous, re.I):
                    markers.append(('shell skip', number))
            if raw_line.strip(): previous = raw_line
    else:
        assertions = sum(len(list(re.finditer(pattern, code))) for pattern in rules['assertions'])
        markers = [(match.group(1), code.count('\n', 0, match.start()) + 1)
                   for pattern in rules['skips'] for match in re.finditer(pattern, code)]
    return assertions, sorted(markers, key=lambda item: item[1]), source, None


def added_markers(before, after):
    old, new = Counter(key for key, _ in before[1]), Counter(key for key, _ in after[1])
    old_lines, new_lines = before[2].splitlines(), after[2].splitlines()
    unchanged = Counter((key, old_lines[line - 1]) for key, line in before[1])
    candidates = defaultdict(list)
    for key, line in after[1]:
        identity = (key, new_lines[line - 1])
        if unchanged[identity]: unchanged[identity] -= 1
        else: candidates[key].append(line)
    return sorted(line for key, count in new.items() for line in candidates[key][:max(0, count - old[key])])


def python_imports(tree):
    def guarded(node):
        if isinstance(node, (ast.Tuple, ast.List)): return any(guarded(item) for item in node.elts)
        return dotted(node).rsplit('.', 1)[-1] in ('ImportError', 'ModuleNotFoundError')
    def walk(node):
        if isinstance(node, ast.Try) and any(guarded(handler.type) for handler in node.handlers):
            for child in [*node.handlers, *node.orelse, *node.finalbody]: yield from walk(child)
        elif isinstance(node, ast.If) and dotted(node.test) in ('TYPE_CHECKING', 'typing.TYPE_CHECKING'):
            for child in node.orelse: yield from walk(child)
        elif isinstance(node, (ast.Import, ast.ImportFrom)): yield node
        else:
            for child in ast.iter_child_nodes(node): yield from walk(child)
    yield from walk(tree)


def import_findings(path, family, analysis, files, directories):
    unresolved = []; external = 0
    def python_exists(target):
        return target + '.py' in files or target in directories or posixpath.join(target, '__init__.py') in files
    if family == 'python':
        parent = posixpath.dirname(path)
        roots = ['']
        while parent:
            roots.append(parent); parent = posixpath.dirname(parent)
        for node in python_imports(analysis[3]):
            names = [alias.name for alias in node.names] if isinstance(node, ast.Import) else [node.module or '']
            for name in names:
                if isinstance(node, ast.ImportFrom) and node.level:
                    parts = path.split('/')[:-1]
                    level = node.level - 1
                    if level > len(parts): external += 1; continue
                    target = '/'.join(parts[:len(parts) - level] + (name.split('.') if name else []))
                    found = python_exists(target)
                else:
                    first = name.split('.')[0]
                    owned = [root for root in roots if python_exists(posixpath.join(root, first))]
                    if not owned: external += 1; continue
                    target = posixpath.join(owned[-1], name.replace('.', '/'))
                    found = any(python_exists(posixpath.join(root, name.replace('.', '/'))) for root in owned)
                if not found: unresolved.append((node.lineno, target if safe_path(target) else None))
    else:
        code = lexical(analysis[2], 'javascript')
        for match in JS_IMPORT.finditer(code):
            name = next(match.group(key) for key in ('spec_from', 'spec_call', 'spec_side') if match.group(key) is not None)
            if '\\' in name or not name.startswith(('./', '../')): external += 1; continue
            target = posixpath.normpath(posixpath.join(posixpath.dirname(path), name))
            if target == '..' or target.startswith('../'): external += 1; continue
            if target == '.': found = any('index' + ext in files for ext in JS_EXTENSIONS)
            else:
                found = safe_path(target) and (target in files or any(
                    target + ext in files or target + '/index' + ext in files for ext in JS_EXTENSIONS))
            if not found: unresolved.append((code.count('\n', 0, match.start()) + 1, target if safe_path(target) else None))
    return unresolved, external


def measurement_record(head):
    return dict(base=None, head=head, recorded_at=time.time(), verdict='pass', checks={
        'assertions':dict(status='pass', files=0, removed=0, unverifiable=0, reason=None),
        'skips':dict(status='pass', files=0, added=0, unverifiable=0, reason=None),
        'imports':dict(status='pass', files=0, unresolved=0, external=0, unverifiable=0, reason=None)},
        findings=[], omitted=0, unverifiable_files=0, reason=None)


class Measurement:
    def __init__(self, head):
        self.record = measurement_record(head)
        self.unknown = {key:set() for key in e.INTEGRITY_CHECKS}
        self.findings = []

    def unverified(self, checks, reason, path=None):
        if self.record['reason'] is None: self.record['reason'] = reason
        for check in checks:
            row = self.record['checks'][check]
            if row['status'] != 'flagged': row['status'] = 'unverifiable'
            if row['reason'] is None: row['reason'] = reason
            if path is not None: self.unknown[check].add(path)

    def finding(self, check, path, kind, line=None, base_count=None, head_count=None, module=None):
        if not safe_path(path): self.record['omitted'] += 1; return
        row = dict(check=check, path=path, line=line, kind=kind)
        if check == 'assertions': row.update(base_count=base_count, head_count=head_count)
        if check == 'imports': row['module'] = module
        self.findings.append(row)
        self.findings.sort(key=lambda item: (item['path'], item['line'] or 0, item['check'], item['kind']))
        if len(self.findings) > 40:
            self.findings.pop(); self.record['omitted'] += 1

    def finish(self):
        for check, paths in self.unknown.items(): self.record['checks'][check]['unverifiable'] = len(paths)
        self.record['unverifiable_files'] = len(set().union(*self.unknown.values()))
        self.record['verdict'] = e.integrity_verdict([row['status'] for row in self.record['checks'].values()])
        self.record['findings'] = self.findings
        return self.record


def changed_files(data):
    fields = data.split(b'\0'); rows = []; index = 0
    while index < len(fields) - 1:
        status = fields[index].decode('ascii'); index += 1
        old = fields[index].decode('utf-8', 'surrogateescape'); index += 1
        new = old
        if status.startswith('R'):
            new = fields[index].decode('utf-8', 'surrogateescape'); index += 1
        rows.append((None if status == 'A' else old, None if status == 'D' else new))
    return sorted(rows, key=lambda row: row[1] or row[0])


@git_context()
def measure(repo, pinned, config, started=None, head=None, measurement=None):
    started = time.monotonic() if started is None else started
    git = Git(repo, started)
    if head is None: head = git('rev-parse', '--verify', 'HEAD').decode().strip()
    m = measurement or Measurement(head)
    reason = 'diff unavailable'; pending = {check:set() for check in e.INTEGRITY_CHECKS}
    with budget(started):
        try:
            base = git('merge-base', pinned, head).decode().strip(); m.record['base'] = base
            reason = 'invalid test patterns'; patterns = a.risk_settings(config)['test_patterns']
            reason = 'diff unavailable'
            rows = changed_files(git('diff', '--no-ext-diff', '--no-textconv', '--find-renames',
                                     '--diff-filter=AMDRT', '--name-status', '-z', base, head, '--'))
            if len(rows) > MAX_FILES:
                for old, new in rows:
                    path = new or old
                    for check in e.INTEGRITY_CHECKS:
                        m.record['checks'][check]['files'] += 1
                    m.unverified(e.INTEGRITY_CHECKS, 'changed file limit exceeded', path)
                return m.finish()
            scoped = []
            for old, new in rows:
                path = new or old
                test = any(value is not None and a.matches(value, patterns) for value in (old, new))
                family = language(path)
                checks = (['assertions', 'skips'] if test else []) + (['imports'] if new and family in ('python', 'javascript') else [])
                for check in checks:
                    m.record['checks'][check]['files'] += 1; pending[check].add(path)
                scoped.append((old, new, path, test, family, checks))
            def done(checks, path):
                for check in checks: pending[check].discard(path)
            reason = 'tree unavailable'
            files = {path.decode('utf-8', 'surrogateescape') for path in
                     git('ls-tree', '-r', '--name-only', '-z', head, '--').split(b'\0') if path}
            directories = {parent for path in files for parent in
                           (str(item) for item in Path(path).parents) if parent != '.'}
            totals = defaultdict(lambda: [0, 0]); reductions = defaultdict(list); incomplete = set()
            for old, new, path, test, family, checks in scoped:
                if not checks: continue
                if not all(safe_path(value) for value in (old, new) if value is not None):
                    m.unverified(checks, 'unsafe path', path)
                    if test: incomplete.add(family)
                    m.finding(checks[0], path, 'unsafe path'); done(checks, path); continue
                if test and family is None:
                    m.unverified(['assertions', 'skips'], 'no recogniser for this file type', path)
                    m.finding('assertions', path, 'no recogniser for this file type'); done(checks, path); continue
                analyses = []
                for revision, name in ((base, old), (head, new)):
                    # Imports need only HEAD; assertion and skip deltas need both.
                    if not test and revision == base: analyses.append(None); continue
                    try:
                        raw = git.contents(revision, name) if name else b''
                        analyses.append(analyse(raw, family))
                    except FileUnavailable as exc:
                        why = str(exc)
                        affected = ['assertions', 'skips'] if test else []
                        if revision == head and 'imports' in checks: affected.append('imports')
                        m.unverified(affected, why, path)
                        if test: incomplete.add(family)
                        if affected: m.finding(affected[0], path, why)
                        analyses.append(None)
                before, after = analyses
                if test and before is not None and after is not None:
                    totals[family][0] += before[0]; totals[family][1] += after[0]
                    if before[0] > after[0]: reductions[family].append((path, new is None, before[0], after[0]))
                    for line in added_markers(before, after):
                        m.record['checks']['skips']['added'] += 1
                        m.record['checks']['skips']['status'] = 'flagged'
                        m.finding('skips', path, 'new skip or disabled test', line)
                if new and 'imports' in checks and after is not None:
                    missing, external = import_findings(path, family, after, files, directories)
                    m.record['checks']['imports']['external'] += external
                    m.record['checks']['imports']['unresolved'] += len(missing)
                    if missing: m.record['checks']['imports']['status'] = 'flagged'
                    for line, module in missing: m.finding('imports', path, 'unresolved first-party import', line, module=module)
                done(checks, path)
            # Suppress per-file losses only with complete family totals: a split
            # or move is not a loss. Incomplete families cannot establish either.
            for family, (before, after) in totals.items():
                if family in incomplete or before <= after: continue
                m.record['checks']['assertions']['removed'] += before - after
                m.record['checks']['assertions']['status'] = 'flagged'
                for path, deleted, old_count, new_count in reductions[family]:
                    kind = 'test file deleted' if deleted else ('assertions removed' if family == 'python' else
                                                               'assertion-like sites went down')
                    m.finding('assertions', path, kind,
                              base_count=old_count, head_count=new_count)
        except Interrupted:
            m.unverified(e.INTEGRITY_CHECKS, 'interrupted'); m.record['reason'] = 'interrupted'
        except (TimeoutError, subprocess.TimeoutExpired): m.unverified(e.INTEGRITY_CHECKS, 'budget exhausted')
        except (OSError, ValueError, TypeError, IndexError, subprocess.SubprocessError): m.unverified(e.INTEGRITY_CHECKS, reason)
    for check, paths in pending.items(): m.unknown[check].update(paths)
    return m.finish()


def setting(config):
    value = config.get('repo', {}).get('test_integrity_check', False)
    if type(value) is not bool:
        print('  Test integrity: not measured (invalid setting).', flush=True)
        return False
    return value


@git_context()
def main():
    started = time.monotonic()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo', required=True); parser.add_argument('--base', required=True)
    parser.add_argument('--branch')
    args = parser.parse_args(); repo = Path(args.repo).resolve()
    m = None; code = 0
    try:
        git = Git(repo, started)
        with budget(started):
            pinned = git('rev-parse', '--verify', '--end-of-options', (args.base or 'refs/remotes/origin/main') + '^{commit}').decode().strip()
            head = git('rev-parse', '--verify', 'HEAD').decode().strip()
            try: config = json.loads(git.contents(pinned, '.bureau.json'))
            except FileUnavailable: return 0
            if not setting(config): return 0
            m = Measurement(head)
            common = (repo / os.fsdecode(git('rev-parse', '--git-common-dir')).strip()).resolve()
            # Like fail-before-pass, opt in locally before resolving a provider-
            # free run's actual PR base. Only already-fetched refs are read.
            try:
                if not args.base and args.branch:
                    number = git.prepare(['gh', 'pr', 'list', '--head', args.branch, '--json', 'number', '--jq', '.[0].number']).decode().strip()
                    if number and number != 'null':
                        if not re.fullmatch(r'[1-9][0-9]*', number): raise ValueError('invalid PR')
                        refs = json.loads(git.prepare(['gh', 'pr', 'view', number, '--json', 'state,baseRefName,headRefName']))
                        base_ref = refs['baseRefName']
                        if refs['state'] != 'OPEN' or refs['headRefName'] != args.branch or base_ref.startswith('-'):
                            raise ValueError('invalid PR base')
                        git('check-ref-format', 'refs/heads/' + base_ref)
                        pinned = git('rev-parse', '--verify', 'refs/remotes/origin/' + base_ref + '^{commit}').decode().strip()
                        config = json.loads(git.contents(pinned, '.bureau.json'))
                        if not setting(config): return 0
                record = measure(repo, pinned, config, started, head, m)
            except Interrupted:
                m.unverified(e.INTEGRITY_CHECKS, 'interrupted'); record = m.finish(); code = 130
            except (TimeoutError, subprocess.TimeoutExpired):
                m.unverified(e.INTEGRITY_CHECKS, 'budget exhausted'); record = m.finish()
            except (OSError, ValueError, KeyError, TypeError, FileUnavailable, subprocess.SubprocessError):
                m.unverified(e.INTEGRITY_CHECKS, 'diff unavailable'); record = m.finish()
        if record['reason'] == 'interrupted': code = 130
        run, attempt = os.environ.get('BUREAU_RUN_ID'), os.environ.get('BUREAU_STAGE_ATTEMPT_ID')
        if run and attempt:
            if e.observe(e.Store(common / 'bureau').test_integrity, run, attempt, record) is None:
                print('  Test integrity: not measured (evidence unavailable).', flush=True)
                return code
        checks = record['checks']
        print(f'  Test integrity: {record["verdict"]}; removed assertions={checks["assertions"]["removed"]}; '
              f'new skips={checks["skips"]["added"]}; unresolved imports={checks["imports"]["unresolved"]}; '
              f'unverifiable files={record["unverifiable_files"]}; omitted={record["omitted"]}' +
              (f'; {record["reason"]}' if record['reason'] else ''), flush=True)
        return code
    except Interrupted:
        print('  Test integrity: not measured (interrupted).', flush=True); return 130
    except Exception:
        print('  Test integrity: not measured.', flush=True); return code


if __name__ == '__main__': sys.exit(main())
