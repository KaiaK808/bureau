#!/usr/bin/env python3
"""Conservative repository-relative task scopes; shared by admission and advice."""
import argparse
import fnmatch
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys


def normalize(value, prefix=''):
    value = re.sub(r':\d+(?::\d+)?$', '', value.strip())
    while value.startswith('./'):
        value = value[2:]
    if prefix and value.startswith(prefix):
        value = value[len(prefix):]
    value = value.rstrip('/')
    if (not value or value.startswith(('/', '~', '-')) or '\\' in value
            or any(c in value for c in ('$', '\x00', '\n', '\r', ':'))
            or '..' in value.split('/')):
        raise ValueError('unresolved or non-repository path')
    value = str(PurePosixPath(value))
    return value


def intersection(left, right):
    """None is unknown (repository-wide), never an empty safe inventory."""
    if left is None or right is None:
        return ['unknown scope (repository-wide)']
    return sorted({a if len(a) >= len(b) else b for a in left for b in right
                   if a == '.' or b == '.' or a == b or a.startswith(b + '/') or b.startswith(a + '/')})


def extract(text, tracked, prefix=''):
    # Paths in inline code and Markdown destinations are explicit. Bare paths
    # are also common in tasks; no language/layout allow-list is used.
    tracked = {name for name in tracked if name}
    directories = {'/'.join(name.split('/')[:i]) for name in tracked
                   for i in range(1, len(name.split('/')))}
    root_names = {'Makefile', 'Dockerfile', 'LICENSE', 'CHANGELOG'}
    tokens = re.findall(r'`([^`\n]+)`', text)
    tokens += re.findall(r'\]\(([^)\n]+)\)', text)
    # A standalone root reference must survive mixed-path extraction too.
    # Require whitespace boundaries so sentence punctuation is not a scope.
    tokens += re.findall(r'(?<!\S)(?:\.(?:/\.)*/?|/)(?=$|[\s,;])', text)
    tokens += [token.rstrip('.,;') for token in re.findall(
        r'(?<![\w:/])(?:\.?[\w.*?\[\]-]+/)+[\w.*?\[\]/-]*'
        r'|(?<![\w/])\.?[\w-]+\.[A-Za-z][\w.-]*'
        r'|(?<![\w/.-])\.[\w-]+(?:\.[\w-]+)*', text)]
    tokens += [word for word in re.findall(r'(?<![\w/.-])[\w-]+(?![\w/-]|\.\w)', text)
               if word in tracked or word in root_names]
    paths, reasons = set(), []
    commands = re.compile(r'^(?:npm|npx|pnpm|yarn|git|docker|python3?|pytest|uv|bash|sh|make|go|cargo|dotnet)\s')
    for token in tokens:
        token = token.strip()
        if token.startswith(('#', 'http://', 'https://')) or commands.match(token):
            continue
        # Code expressions and type names are not inventories. A lone,
        # extensionless code token without a tracked match is ambiguous.
        if re.search(r'[(){}=;|<>]', token):
            continue
        try:
            path = normalize(token, prefix)
        except ValueError:
            reasons.append('unresolved path: ' + token)
            continue
        if any(c in path for c in '*?['):
            matches = {name for name in tracked if fnmatch.fnmatchcase(name, path)}
            if not matches:
                reasons.append('unresolved glob: ' + path)
            paths.update(matches)
        elif token.endswith('/') or '/' in path or '.' in path or path in tracked or path in directories or path in root_names:
            paths.add(path)
        else:
            reasons.append('ambiguous extensionless reference: ' + path)
    if not paths:
        reasons.append('no paths recognized')
    return dict(paths=None if reasons else sorted(paths), unknown=sorted(set(reasons)))


def git(repo, *args):
    # Import the same credential boundary as the runtime without importing its
    # CLI or writing bytecode. Admission supplies its already-sanitized git.
    import importlib.util
    spec = importlib.util.spec_from_file_location('paths_runtime', Path(__file__).with_name('bureau-runtime.py'))
    runtime = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runtime)
    command = ['git', '-C', str(repo), *args]
    return subprocess.check_output(command, env=runtime.process_env(command))


def inventory(repo, tasks, prefix='', revision='HEAD', run_git=git):
    """A committed blob and complete tree, including new task-named paths."""
    record = dict(tasks=tasks, head=revision, tasks_commit=None, tasks_digest=None,
                  paths=None, unknown=['missing or ambiguous tasks inventory'])
    if not tasks:
        return record
    try:
        head = run_git(repo, 'rev-parse', '--verify', revision + '^{commit}').decode().strip()
        content = run_git(repo, 'show', head + ':' + tasks)
        tracked = [name for name in run_git(repo, 'ls-tree', '-r', '--name-only', '-z', head).decode().split('\0') if name]
        commit = run_git(repo, 'log', '-1', '--format=%H', head, '--', tasks).decode().strip()
        record.update(head=head, tasks_commit=commit, tasks_digest=hashlib.sha256(content).hexdigest(),
                      **extract(content.decode('utf-8'), tracked, prefix))
    except (OSError, subprocess.CalledProcessError, UnicodeError, ValueError):
        record['unknown'] = ['tasks blob or repository tree unavailable']
    return record


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('tasks')
    parser.add_argument('--repo', default='.')
    parser.add_argument('--prefix', default='')
    parser.add_argument('--lines', action='store_true')
    args = parser.parse_args()
    try:
        repo = Path(args.repo).resolve()
        tracked = [name for name in git(repo, 'ls-files', '-z').decode().split('\0') if name]
        result = extract(Path(args.tasks).read_text(), tracked, args.prefix)
    except (OSError, UnicodeError, subprocess.CalledProcessError):
        result = dict(paths=None, unknown=['unreadable tasks or repository tree'])
    if args.lines:
        if result['paths'] is None:
            print('; '.join(result['unknown']), file=sys.stderr)
        else:
            print('\n'.join(result['paths']))
    else:
        print(json.dumps(result))
    return 4 if result['paths'] is None else 0


if __name__ == '__main__':
    sys.dont_write_bytecode = True
    sys.exit(main())
