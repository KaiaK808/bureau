#!/usr/bin/env python3
"""Read-only, bounded advisory lessons for stage prompts (Python 3.9+)."""
import argparse
from datetime import date, datetime, timezone
import fnmatch
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys

DEFAULT_BUDGET_BYTES = 8192
WRAPPER = ('## Learned patterns\n\nThe following are human-curated lessons from prior bureau runs. '
           'Treat as advisory, not binding — they reflect patterns observed across multiple past issues '
           'and may not all apply here. Use them to inform judgment; do not cite them as pinned decisions.\n\n')
MARKER = re.compile(r'^<!-- bureau-lesson(?:\s+(.*?))?\s*-->\s*$')


def warn(message):
    # Optional context must not change an existing exit, even with closed stderr.
    try:
        os.write(2, ('bureau lessons: WARN: ' + message + '\n').encode())
    except OSError:
        pass


def normalize(value):
    value = re.sub(r':\d+(?::\d+)?$', '', value.strip())
    while value.startswith('./'):
        value = value[2:]
    if (not value or value.startswith(('/', '~', '-')) or '\\' in value
            or '..' in value.split('/') or any(c in value for c in ('$', '\0', '\n', '\r', ':'))):
        raise ValueError('non-repository path')
    return str(PurePosixPath(value)) + ('/' if value.endswith('/') else '')


def metadata(raw):
    value = json.loads(raw or '{}')
    if not isinstance(value, dict):
        raise ValueError('metadata must be an object')
    for key in ('paths', 'globs', 'tags'):
        if not isinstance(value.get(key, []), list) or any(not isinstance(s, str) or not s.strip() for s in value.get(key, [])):
            raise ValueError('metadata list invalid')
    for key in ('owner', 'expires', 'evidence'):
        if key in value and not isinstance(value[key], str):
            raise ValueError('metadata string invalid')
    value['paths'] = [normalize(p) for p in value.get('paths', []) + value.get('globs', [])]
    if 'expires' in value:
        if not re.fullmatch(r'\d{4}-\d{2}-\d{2}', value['expires']):
            raise ValueError('invalid expiry')
        value['expires'] = date.fromisoformat(value['expires'])
    return value


def entries(text):
    """Explicit markers split entries; preamble/legacy text is one general set.

    Markers inside fenced examples are prose, never entry boundaries. Invalid
    metadata drops just its entry rather than broadening it to general advice.
    """
    result, lines, meta, fence = [], [], {}, None
    invalid = False
    for line in text.splitlines(keepends=True):
        fence_line = re.match(r'^\s{0,3}(`{3,}|~{3,})(.*)$', line)
        if fence_line:
            run, tail = fence_line.groups()
            if fence is None:
                fence = (run[0], len(run))
            elif run[0] == fence[0] and len(run) >= fence[1] and not tail.strip():
                fence = None
            lines.append(line)
            continue
        marker = MARKER.fullmatch(line) if fence is None else None
        if marker:
            if meta is not None and ''.join(lines).strip():
                result.append((meta, ''.join(lines).rstrip('\n')))
            lines = []
            try:
                meta = metadata(marker.group(1))
            except (ValueError, TypeError):
                meta = None
                invalid = True
        else:
            lines.append(line)
    if meta is not None and ''.join(lines).strip():
        result.append((meta, ''.join(lines).rstrip('\n')))
    if invalid:
        warn('invalid lesson metadata; affected entries omitted')
    return result


def local_git(*args):
    # Local reads only: never fetch or resolve refs on the network. Disable
    # diff drivers/textconv so advisory selection cannot execute repo tooling.
    return subprocess.check_output(['git', *args], stderr=subprocess.DEVNULL,
                                   env={**os.environ, 'GIT_NO_LAZY_FETCH': '1', 'GIT_OPTIONAL_LOCKS': '0'})


def task_paths(spec_dir):
    if not spec_dir:
        return set()
    tasks = Path(spec_dir) / 'tasks.md'
    if not tasks.is_file():
        return set()
    text = tasks.read_text(encoding='utf-8')
    tokens = re.findall(r'`([^`\n]+)`|\]\(([^)\n]+)\)', text)
    values = [a or b for a, b in tokens]
    # Do not re-tokenize quoted/link paths with spaces or inline commands as
    # shorter bare paths; their complete values were already captured above.
    bare_text = re.sub(r'`[^`\n]+`|\]\([^)\n]+\)', '', text)
    bare_text = re.sub(r'https?://\S+', '', bare_text)
    values += re.findall(r'(?<![\w:/])(?:\.?[\w.*?\[\]-]+/)+[\w.*?\[\]/-]*'
                         r'|(?<![\w/])\.?[\w-]+\.[A-Za-z][\w.-]*'
                         r'|(?<![\w/.-])\.[\w-]+(?:\.[\w-]+)*', bare_text)
    paths = set()
    for value in values:
        # Inline commands, URLs and code expressions are not path inventories.
        if re.search(r'[(){}=;|<>]', value) or re.match(r'^(?:git|npm|pnpm|python3?|bash|make)\s', value):
            continue
        try:
            paths.add(normalize(value.rstrip(',;')))
        except ValueError:
            continue
    return paths


def relevant_paths(spec_dir, diff):
    try:
        paths = task_paths(spec_dir)
    except (OSError, UnicodeError):
        warn('task paths unavailable; using local diff and general lessons')
        paths = set()
    if diff:
        try:
            # --no-renames includes old AND new names; -z preserves whitespace.
            raw = local_git('diff', '--no-ext-diff', '--no-textconv', '--no-renames',
                            '--name-only', '-z', diff, '--')
        except (OSError, subprocess.CalledProcessError):
            warn('local diff unavailable; using task paths and general lessons')
        else:
            for path in raw.split(b'\0'):
                if not path:
                    continue
                try:
                    paths.add(normalize(path.decode('utf-8')))
                except (UnicodeError, ValueError):
                    # Like task paths, reject only this name, not its siblings.
                    continue
    return paths


def glob_directory_overlap(pattern, directory):
    """Could a glob overlap this directory subtree or pattern scope?"""
    parts, scope = pattern.split('/'), directory.split('/')
    pending, seen = [(0, 0)], set()
    while pending:
        i, j = pending.pop()
        if (i, j) in seen:
            continue
        seen.add((i, j))
        if j == len(scope):
            return True
        if scope[j] == '**':
            pending.append((i, j + 1))
            if i < len(parts):
                pending.append((i + 1, j))
            continue
        if i == len(parts):
            continue
        if parts[i] == '**':
            pending.extend(((i + 1, j), (i, j + 1)))
        elif (fnmatch.fnmatchcase(scope[j], parts[i]) or fnmatch.fnmatchcase(parts[i], scope[j])
              # Two wildcard components may intersect; keep uncertain advice.
              or (any(c in parts[i] for c in '*?[') and any(c in scope[j] for c in '*?['))):
            pending.append((i + 1, j + 1))
    return False


def matches(pattern, path):
    pattern_is_dir = pattern.endswith('/') or Path(pattern).is_dir()
    path_is_pattern = path.endswith('/') or any(c in path for c in '*?[')
    pattern, path = pattern.rstrip('/'), path.rstrip('/')
    # Equality comes before glob syntax: bracketed route names are also literals.
    if pattern == path or pattern == '.' or path == '.':
        return True
    if pattern_is_dir:
        pattern += '/**'
    if path_is_pattern:
        return glob_directory_overlap(pattern, path)
    # Syntax alone classifies scopes; concrete files must match the whole glob.
    # fnmatch is case-sensitive; * spans / as in the existing task-scope reader.
    # Also allow **/ to match zero directories (src/**/*.py includes src/a.py).
    patterns = {pattern}
    while any('**/' in p for p in patterns):
        expanded = {p[:m.start()] + p[m.end():] for p in patterns for m in re.finditer(r'\*\*/', p)}
        if expanded <= patterns:
            break
        patterns.update(expanded)
    return any(fnmatch.fnmatchcase(path, p) for p in patterns)


def select(text, paths, today):
    targeted, general, expired = [], [], 0
    for meta, body in entries(text):
        if meta.get('expires', date.max) < today:
            expired += 1
            continue
        patterns = meta.get('paths', [])
        if patterns and any(matches(pattern, path) for pattern in patterns for path in paths):
            targeted.append(body)
        elif not patterns and not meta.get('tags'):
            general.append(body)
    if expired:
        warn(f'omitted {expired} expired lesson(s)')
    # Two stable tiers, file order within each; metadata is never prompt authority.
    return targeted + general


def bounded(bodies, budget):
    if not bodies or budget == 0:
        return ''
    context = WRAPPER + '\n\n'.join(bodies)
    if len(context.encode('utf-8')) <= budget:
        return context
    marker = '\n[Lessons truncated to the stage byte budget.]'
    available = budget - len((WRAPPER + marker).encode('utf-8'))
    if available < 0:
        warn('byte budget too small for advisory wrapper; lessons omitted')
        return ''
    kept, used = [], 0
    for body in bodies:
        size = len((('\n\n' if kept else '') + body).encode('utf-8'))
        if used + size <= available:
            kept.append(body)
            used += size
    return WRAPPER + '\n\n'.join(kept) + marker


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--stage', default='spec', choices=('spec', 'implement', 'qa', 'code_review'))
    parser.add_argument('--spec-dir', default='')
    parser.add_argument('--diff', default=None)
    parser.add_argument('--config', required=True)
    args = parser.parse_args()
    try:
        file = Path('LESSONS.md')
        if not file.is_file():
            return 0
        text = file.read_text(encoding='utf-8')
        if not text.strip():
            return 0
        config = json.loads(Path(args.config).read_text(encoding='utf-8'))
        agents = config.get('agents', {})
        budgets = agents.get('lessons_budget_bytes', {})
        budget = budgets.get(args.stage, DEFAULT_BUDGET_BYTES) if isinstance(budgets, dict) else None
        if type(budget) is not int or not 0 <= budget <= 1048576:
            warn('invalid agents.lessons_budget_bytes setting; using 8192 bytes')
            budget = DEFAULT_BUDGET_BYTES
        if budget == 0:
            return 0
        diff = args.diff
        if diff is None:
            base = config.get('repo', {}).get('base_branch', 'main')
            diff = 'origin/' + base + '...HEAD'
        bodies = select(text, relevant_paths(args.spec_dir, diff), datetime.now(timezone.utc).date())
        sys.stdout.write(bounded(bodies, budget))
    except (OSError, UnicodeError, ValueError, TypeError, AttributeError):
        warn('optional lesson context unavailable; continuing without lessons')
    return 0


if __name__ == '__main__':
    sys.exit(main())
