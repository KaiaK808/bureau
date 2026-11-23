#!/usr/bin/env python3
"""Authenticate visible review snapshots against pinned default-branch policy."""
import base64
from datetime import datetime
import json
import re
import subprocess
import sys
import time
from urllib.parse import quote

LOGIN = r'[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*'
SHA = re.compile(r'[0-9a-fA-F]{40}')
FIELDS = 'id body createdAt lastEditedAt isMinimized author{__typename login}'
RESERVED = re.compile(r'^\*\*(Verdict|Build|Reviewed head|Target)\*\*')


def merge_publishers(config):
    agents = config.get('agents') if isinstance(config, dict) else None
    entries = agents.get('merge_publishers') if isinstance(agents, dict) else None
    if not isinstance(entries, list) or len(entries) > 20 or any(
            not isinstance(v, str) or not re.fullmatch(r'(user|app):' + LOGIN, v)
            or not 1 <= len(v.split(':')[1]) <= 39 for v in entries):
        raise ValueError('agents.merge_publishers must be an array of at most 20 typed user:<login> or app:<slug> entries')
    normalized = [v.lower() for v in entries]
    if len(set(normalized)) != len(normalized):
        raise ValueError('agents.merge_publishers must not contain duplicate entries')
    return normalized


class ReadError(Exception):
    pass


class Reader:
    def __init__(self, repo):
        self.repo = repo
        self.deadline = time.monotonic() + 90

    def api(self, route, fields=None):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise ReadError('snapshot deadline exceeded')
        args = ['gh', 'api', route, '--method', 'POST' if route == 'graphql' else 'GET']
        for key, value in (fields or {}).items():
            args += ['-F' if type(value) is int else '-f', key + '=' + str(value)]
        try:
            proc = subprocess.run(args, capture_output=True, text=True, timeout=min(30, remaining))
            value = json.loads(proc.stdout)
            if proc.returncode or not isinstance(value, dict) or value.get('errors') not in (None, []):
                raise ValueError()
            return value
        except (OSError, ValueError, UnicodeError, subprocess.TimeoutExpired):
            raise ReadError('GitHub response failed or was unreadable') from None

    def policy(self):
        repository = self.api('repos/' + self.repo)
        branch = required(repository, 'default_branch', str)
        commit = required(self.api('repos/' + self.repo + '/commits/' + quote(branch, safe='')), 'sha', str)
        if not SHA.fullmatch(commit):
            raise ReadError('default branch commit is unreadable')
        # A directory listing proves absence; a failed contents read stays unknown.
        tree = self.api('repos/' + self.repo + '/git/trees/' + commit)
        if tree.get('truncated') is not False or not isinstance(tree.get('tree'), list):
            raise ReadError('default branch tree is incomplete')
        if not any(isinstance(v, dict) and v.get('path') == '.bureau.json' for v in tree['tree']):
            return dict(kind='policy', message='agents.merge_publishers is missing on the default branch')
        contents = self.api('repos/' + self.repo + '/contents/.bureau.json', {'ref': commit})
        try:
            if contents['encoding'] != 'base64':
                raise ValueError()
            raw = base64.b64decode(''.join(contents['content'].split()), validate=True)
        except (KeyError, TypeError, AttributeError, ValueError):
            raise ReadError('default branch configuration is unreadable') from None
        try:
            entries = merge_publishers(json.loads(raw))
        except (ValueError, UnicodeError) as exc:
            return dict(kind='policy', message='agents.merge_publishers: invalid default-branch policy: ' + str(exc))
        return dict(kind='ok', entries=entries, commit=commit)

    def connection(self, number, name, fields):
        nodes, ids, cursors, cursor, count = [], set(), set(), None, None
        for _ in range(100):
            query = ('query($owner:String!,$repo:String!,$num:Int!,$cursor:String){repository(owner:$owner,name:$repo){'
                     'pullRequest(number:$num){' + name + '(first:100,after:$cursor){totalCount nodes{' + fields +
                     '} pageInfo{hasNextPage endCursor}}}}}')
            variables = dict(query=query, owner=self.repo.split('/')[0], repo=self.repo.split('/')[1], num=number)
            if cursor is not None:
                variables['cursor'] = cursor
            try:
                page = self.api('graphql', variables)['data']['repository']['pullRequest'][name]
                total = required(page, 'totalCount', int)
                batch = required(page, 'nodes', list)
                info = required(page, 'pageInfo', dict)
                more = required(info, 'hasNextPage', bool)
                end = info['endCursor']
                if total < 0 or len(batch) > 100 or (count is not None and total != count):
                    raise ReadError('inconsistent page count')
                count = total
                for node in batch:
                    identifier = required(node, 'id', str)
                    if identifier in ids:
                        raise ReadError('duplicate node ID')
                    ids.add(identifier)
                nodes += batch
                if len(nodes) > count or (end is not None and (not isinstance(end, str) or not end or end in cursors)):
                    raise ReadError('inconsistent pagination cursor or count')
                if not more:
                    if len(nodes) != count:
                        raise ReadError('truncated snapshot')
                    return nodes
                if not batch or not end or len(nodes) >= count:
                    raise ReadError('incomplete pagination')
                cursors.add(end)
                cursor = end
            except (KeyError, TypeError):
                raise ReadError('missing connection metadata') from None
        raise ReadError('snapshot exceeds 100 pages')

    def threads(self, number):
        nodes = self.connection(number, 'reviewThreads', 'id isResolved')
        return dict(kind='ok', unresolved=sum(not required(v, 'isResolved', bool) for v in nodes))

    def statuses(self, head):
        nodes, ids, count = [], set(), None
        for page in range(1, 101):
            value = self.api('repos/' + self.repo + '/commits/' + head + '/status',
                             dict(per_page=100, page=page))
            total = required(value, 'total_count', int)
            batch = required(value, 'statuses', list)
            if total < 0 or len(batch) > 100 or (count is not None and count != total):
                raise ReadError('inconsistent status count')
            count = total
            for node in batch:
                identifier = required(node, 'id', int)
                if identifier in ids or required(node, 'state', str) not in ('success', 'pending', 'failure', 'error'):
                    raise ReadError('invalid or duplicate status')
                required(node, 'context', str)
                ids.add(identifier)
            nodes += batch
            if len(nodes) == count:
                return nodes
            if len(batch) != 100 or len(nodes) > count:
                raise ReadError('truncated status snapshot')
        raise ReadError('status snapshot exceeds 100 pages')

    def review(self, number, issue, policy):
        if policy['kind'] != 'ok':
            return policy
        candidates = []
        heading = '## Code Review v2 — ' + issue
        for node in self.connection(number, 'comments', FIELDS):
            body = required(node, 'body', str, empty=True)
            if required(node, 'isMinimized', bool):
                raise ReadError('comment snapshot contains a hidden entry')
            lines = [v.removesuffix('\r') for v in body.split('\n')]
            if not lines or lines[0] != heading:
                continue
            # Unknown authors of review-shaped entries may hide a newer verdict.
            actor = account(node.get('author'))
            if actor in policy['entries']:
                candidates.append((timestamp(required(node, 'createdAt', str)), node))
        if not candidates:
            return dict(kind='verdict', message='no authorised review; check agents.merge_publishers')
        candidates.sort(key=lambda v: v[0])
        if len(candidates) > 1 and candidates[-1][0] == candidates[-2][0]:
            raise ReadError('ambiguous newest review')
        selected = candidates[-1][1]
        required(selected, 'lastEditedAt', (str, type(None)))
        query = 'query($id:ID!){node(id:$id){... on IssueComment{' + FIELDS + '}}}'
        try:
            reread = self.api('graphql', dict(query=query, id=selected['id']))['data']['node']
        except (KeyError, TypeError):
            raise ReadError('selected review disappeared') from None
        for key in ('id', 'body', 'createdAt'):
            required(reread, key, str, empty=(key == 'body'))
        required(reread, 'lastEditedAt', (str, type(None)))
        if required(reread, 'isMinimized', bool):
            raise ReadError('selected review is hidden')
        actor = account(reread.get('author'))
        if reread['id'] != selected['id'] or actor != account(selected['author']):
            raise ReadError('selected review identity changed')
        if selected['lastEditedAt'] is not None or reread['lastEditedAt'] is not None or reread != selected:
            return dict(kind='edited', message='selected review was edited; fresh Build Review required')
        # Only contiguous reserved fields own the header, never provider prose.
        lines = [v.removesuffix('\r') for v in selected['body'].split('\n')]
        header, seen = [], set()
        if len(lines) < 2 or lines[1] != '':
            return dict(kind='verdict', message='invalid review header; check agents.merge_publishers')
        for line in lines[2:]:
            match = RESERVED.match(line)
            if not match:
                break
            key = match.group(1)
            if key in seen and key not in ('Reviewed head', 'Target'):
                return dict(kind='verdict', message='duplicate review header field ' + key + '; check agents.merge_publishers')
            seen.add(key)
            header.append(line)
        verdicts = [re.fullmatch(r'\*\*Verdict\*\*: (APPROVE|AUTO_APPROVE|BLOCK|REQUEST_CHANGES)', v) for v in header]
        verdict = next((v.group(1) for v in verdicts if v), None)
        if verdict is None:
            return dict(kind='verdict', message='invalid review verdict; check agents.merge_publishers')
        return dict(kind='ok', verdict=verdict, header='\n'.join(header))


def required(value, key, kind, empty=False):
    found = value.get(key) if isinstance(value, dict) else None
    kinds = kind if isinstance(kind, tuple) else (kind,)
    if not isinstance(value, dict) or key not in value or type(found) not in kinds or (found == '' and not empty):
        raise ReadError('missing or invalid ' + key)
    return found


def account(author):
    kind = required(author, '__typename', str)
    if kind not in ('User', 'Bot'):
        raise ReadError('unknown review author type')
    login = required(author, 'login', str).lower()
    if kind == 'User':
        prefix = 'user:'
    elif kind == 'Bot':
        prefix = 'app:'
        login = login.removeprefix('app/')
        if login.endswith('[bot]'):
            login = login[:-5]
    if not 1 <= len(login) <= 39 or not re.fullmatch(LOGIN, login):
        raise ReadError('invalid review author login')
    return prefix + login


def timestamp(value):
    try:
        return datetime.strptime(value, '%Y-%m-%dT%H:%M:%SZ')
    except ValueError:
        raise ReadError('unreadable review creation time') from None


def main():
    args = sys.argv[1:]
    usage = 'usage: bureau-merge-review.py policy OWNER/REPO | threads OWNER/REPO PR | statuses OWNER/REPO HEAD | review OWNER/REPO PR ISSUE POLICY_JSON'
    if args in (['--help'], ['-h']):
        print(usage)
        return 0
    try:
        counts = dict(policy=2, threads=3, statuses=3, review=5)
        if not args or args[0] not in counts or len(args) != counts[args[0]]:
            raise ValueError('expected policy, threads, statuses or review with the required arguments; use --help')
        action, repo = args[:2]
        if not re.fullmatch(r'[A-Za-z0-9-]+/[A-Za-z0-9_.-]+', repo):
            raise ValueError('repository must be OWNER/REPO')
        if action in ('threads', 'review'):
            if not re.fullmatch(r'[1-9][0-9]*', args[2]):
                raise ValueError('PR must be a positive integer')
            number = int(args[2])
            if number > 2147483647:
                raise ValueError('PR exceeds the GraphQL Int range')
        if action == 'statuses' and not SHA.fullmatch(args[2]):
            raise ValueError('HEAD must be a 40-character hexadecimal commit')
        if action == 'review':
            if not args[3] or any(c.isspace() or ord(c) < 32 for c in args[3]):
                raise ValueError('ISSUE must be a nonempty single-line identifier')
            policy = json.loads(args[4])
            if not isinstance(policy, dict) or policy.get('kind') not in ('ok', 'policy', 'read'):
                raise ValueError('POLICY_JSON must be a publisher policy result')
            if policy['kind'] == 'ok':
                merge_publishers(dict(agents=dict(merge_publishers=policy.get('entries'))))
                if not isinstance(policy.get('commit'), str) or not SHA.fullmatch(policy['commit']):
                    raise ValueError('POLICY_JSON must pin a default-branch commit')
            elif not isinstance(policy.get('message'), str) or not policy['message']:
                raise ValueError('POLICY_JSON must include its failure message')
    except (ValueError, RecursionError):
        # Callers turn any nonzero helper exit into an unreadable gate (2),
        # never a decided verdict or the stale-approval handoff (20).
        print('bureau-merge-review: invalid arguments; use --help', file=sys.stderr)
        return 2
    try:
        reader = Reader(repo)
        if action == 'policy':
            result = reader.policy()
        elif action == 'threads':
            result = reader.threads(number)
        elif action == 'statuses':
            result = reader.statuses(args[2])
        else:
            result = reader.review(number, args[3], policy)
    except ReadError as exc:
        if action == 'statuses':
            print(str(exc), file=sys.stderr)
            return 1
        result = dict(kind='read', message=str(exc))
    print(json.dumps(result))
    return 0


if __name__ == '__main__':
    sys.exit(main())
