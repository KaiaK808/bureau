#!/usr/bin/env python3
"""Persist review boundaries shared by bounded ticks in a Git repository."""
import argparse
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
import uuid


def git(repo, *args):
    return subprocess.check_output(['git', '-C', str(repo), *args], text=True).strip()


def directory(repo):
    root = Path(git(repo, 'rev-parse', '--show-toplevel')).resolve()
    return root, (root / git(root, 'rev-parse', '--git-common-dir')).resolve() / 'bureau'


def read(path):
    value = json.loads(path.read_text()) if path.exists() else {}
    if not isinstance(value, dict):
        raise ValueError('Review stops must be an object')
    return value


@contextmanager
def locked(root):
    root.mkdir(parents=True, exist_ok=True)
    with (root / 'review-stops.guard').open('a') as guard:
        fcntl.flock(guard, fcntl.LOCK_EX)
        yield root / 'review-stops.json'


def save(path, value):
    fd, name = tempfile.mkstemp(prefix='.review-stops-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as out:
            json.dump(value, out, indent=2)
            out.write('\n')
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def fingerprint(detail, issue):
    if not isinstance(detail, dict) or detail.get('identifier') != issue:
        raise ValueError('Issue detail is missing its matching identifier')
    labels = detail.get('labels')
    if not isinstance(labels, list) or any(not isinstance(label, str) for label in labels):
        raise ValueError('Issue detail must include label names')
    # Bot digest comments do not constitute new work. A changed title,
    # description, or labels does; same-head review requests can explicitly resume.
    material = {key: detail.get(key) for key in ('identifier', 'title', 'description')}
    material['labels'] = sorted(set(labels) - {'shepherd-focused'})
    return hashlib.sha256(json.dumps(material, sort_keys=True).encode()).hexdigest()


def resume(root, issue, expected=None):
    with locked(root) as path:
        stops = read(path)
        if expected is not None and stops.get(issue) != expected:
            return False
        removed = stops.pop(issue, None) is not None
        if removed:
            save(path, stops)
        return removed


def check(repo, root, issue, branch, state, detail):
    record = read(root / 'review-stops.json').get(issue)
    if not record:
        return {'stopped': False}
    same = (record['branch'] == branch and record['state'] == state
            and record['ticket_hash'] == fingerprint(detail, issue))
    if same:
        # Query the PR itself so a push, closure, or merge can invalidate the
        # stopped boundary without changing or resetting any local checkout.
        raw = subprocess.check_output(
            ['gh', 'pr', 'view', str(record['pr']), '--json', 'state,baseRefName'],
            cwd=repo, text=True, timeout=30)
        current = json.loads(raw)
        if not isinstance(current, dict) or current.get('state') not in ('OPEN', 'CLOSED', 'MERGED'):
            raise ValueError('GitHub did not return the PR state')
        same = current['state'] == 'OPEN'
        if same:
            base_ref = current.get('baseRefName')
            if (not isinstance(base_ref, str) or not base_ref or base_ref.startswith('-')
                    or subprocess.run(['git', 'check-ref-format', 'refs/heads/' + base_ref],
                                      cwd=repo, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode):
                raise ValueError('GitHub did not return a valid PR base branch')
            # Older boundaries were always recorded against main. A retarget
            # invalidates approval even when both base refs point at one commit.
            same = base_ref == record.get('base_ref', 'main')
        if same:
            # GitHub's PR snapshot can lag a branch update. Read both remote
            # refs directly; never equate a cached baseRefOid with its current tip.
            refs = subprocess.check_output(
                ['git', 'ls-remote', '--exit-code', 'origin', 'refs/heads/' + branch, 'refs/heads/' + base_ref],
                cwd=repo, text=True, timeout=30)
            tips = dict((ref, sha) for sha, ref in (line.split() for line in refs.splitlines()))
            if 'refs/heads/' + base_ref not in tips:
                raise ValueError('The current remote base is unavailable')
            same = tips.get('refs/heads/' + branch) == record['head'] and tips['refs/heads/' + base_ref] == record['base']
    if not same:
        resume(root, issue, record)
    return {'stopped': same, 'issue': issue, 'head': record['head'], 'pr': record['pr']}


def checkpoint(repo, root, issue):
    record = read(root / 'review-stops.json').get(issue)
    head = git(repo, 'rev-parse', 'HEAD')
    if (not record or record.get('workspace') != str(repo) or record['reviewed_head'] != head
            or git(repo, 'branch', '--show-current') or git(repo, 'status', '--porcelain')):
        raise ValueError('Only this completed clean detached review may be checkpointed')
    key = hashlib.sha256(str(repo).encode()).hexdigest()
    path = root / 'review-checkpoints' / (key + '.json')
    path.parent.mkdir(parents=True, exist_ok=True)
    save(path, {'head': head, 'git_dir': git(repo, 'rev-parse', '--absolute-git-dir')})
    return {'checkpoint': str(repo), 'head': head}


def workspace(repo, root, issue, stage):
    preferred = repo / '.worktrees' / ('tick-' + stage + '-' + issue)
    if stage != 'code_review':
        return preferred
    candidates = [preferred] + sorted(preferred.parent.glob(preferred.name + '.*'))
    for candidate in candidates:
        key = hashlib.sha256(str(candidate.resolve()).encode()).hexdigest()
        if not candidate.exists() or (root / 'workers' / key).is_file():
            return candidate
        # Rotate only around a verified completed review checkpoint. Failed,
        # dirty, replaced, and app-owned checkouts retain the worker's refusal.
        prior = read(root / 'review-checkpoints' / (key + '.json'))
        if (not prior or not (candidate / '.git').exists()
                or git(candidate, 'rev-parse', '--absolute-git-dir') != prior['git_dir']
                or git(candidate, 'rev-parse', 'HEAD') != prior['head']
                or git(candidate, 'branch', '--show-current') or git(candidate, 'status', '--porcelain')):
            return candidate
    # Never pre-create this directory: the worker claims and registers it before
    # its first reset. Prior completed checkpoints remain available for inspection.
    return preferred.with_name(preferred.name + '.' + uuid.uuid4().hex)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo', default='.')
    commands = parser.add_subparsers(dest='action', required=True)
    commands.add_parser('status')
    saved = commands.add_parser('checkpoint'); saved.add_argument('issue')
    work = commands.add_parser('workspace'); work.add_argument('issue')
    work.add_argument('--stage', required=True, choices=('spec','spec_review','ux','copy','implement','qa','code_review','merge','rebase'))
    again = commands.add_parser('resume'); again.add_argument('issue')
    for action in ('stop', 'check'):
        command = commands.add_parser(action)
        command.add_argument('issue'); command.add_argument('--branch', required=True)
        command.add_argument('--state', required=True)
        if action == 'stop':
            command.add_argument('--head', required=True); command.add_argument('--base', required=True)
            command.add_argument('--base-ref', default='main')
            command.add_argument('--reviewed-head', required=True); command.add_argument('--pr', type=int, required=True)
    args = parser.parse_args()
    if getattr(args, 'issue', None) and not re.fullmatch(r'[A-Z][A-Z0-9]*-[0-9]+', args.issue):
        parser.error('issue must be an identifier such as TEAM-123')
    try:
        repo, root = directory(Path(args.repo).resolve())
        if args.action == 'status':
            result = {'review_stops': read(root / 'review-stops.json')}
        elif args.action == 'checkpoint':
            result = checkpoint(repo, root, args.issue)
        elif args.action == 'workspace':
            result = {'workspace': str(workspace(repo, root, args.issue, args.stage))}
        elif args.action == 'resume':
            result = {'issue': args.issue, 'resumed': resume(root, args.issue)}
        else:
            detail = json.load(sys.stdin)
            ticket_hash = fingerprint(detail, args.issue)
            if args.action == 'check':
                result = check(repo, root, args.issue, args.branch, args.state, detail)
            else:
                if any(not re.fullmatch(r'[0-9a-f]{40,64}', value) for value in (args.head, args.base, args.reviewed_head)) or args.pr <= 0:
                    raise ValueError('A review stop requires a commit SHA and PR number')
                if (args.base_ref.startswith('-') or subprocess.run(['git', 'check-ref-format', 'refs/heads/' + args.base_ref],
                        cwd=repo, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode):
                    raise ValueError('A review stop requires a valid base branch')
                record = dict(workspace=str(repo), branch=args.branch, state=args.state, head=args.head, base=args.base, base_ref=args.base_ref, reviewed_head=args.reviewed_head, pr=args.pr,
                              ticket_hash=ticket_hash, stopped_at=time.time(), revision=uuid.uuid4().hex)
                with locked(root) as path:
                    stops = read(path); stops[args.issue] = record; save(path, stops)
                result = {'stopped': True, 'issue': args.issue, 'head': args.head, 'pr': args.pr}
        print(json.dumps(result))
        return 0
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as exc:
        print('bureau supervision: ' + str(exc), file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
