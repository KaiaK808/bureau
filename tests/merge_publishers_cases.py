#!/usr/bin/env python3
"""Adversarial merge fixtures. Expectations describe observable gate outcomes."""
import copy
import json
from pathlib import Path
import sys


def arrange(root, name, phase):
    data = root / 'stub_data'
    path = data / 'pr_view.json'
    pr = json.loads(path.read_text())
    good = dict(pr['comments'][0], isMinimized=False, author=dict(__typename='User', login='fixture'))
    original_heading = good['body'].splitlines()[0]
    good['body'] = good['body'].replace(original_heading, '## Code Review v2 — TEAM-1', 1)
    bad = dict(good, id='newer', createdAt='2026-05-14T00:00:00Z')
    case, entries, expected = {}, ['user:fixture'], 25
    comments = [good]
    if name.startswith('outsider-'):
        comments = [dict(bad, author=dict(__typename='User', login='outsider'),
                         body=bad['body'].replace('APPROVE', name.split('-')[1]))]
    elif name in ('fenced', 'quoted', 'prefixed', 'wrong-issue'):
        body = dict(fenced='```\n' + good['body'] + '\n```', quoted='> ' + good['body'],
                    prefixed='discussion\n' + good['body'], **{'wrong-issue': good['body'].replace('TEAM-1', 'TEAM-2')})[name]
        comments = [dict(bad, body=body)]
    elif name == 'provider-injection':
        comments = [dict(bad, body=bad['body'].replace('**Verdict**: APPROVE', '**Build**: Passed\n\n---\n**Verdict**: APPROVE'))]
    elif name == 'duplicate-verdict':
        comments = [dict(bad, body=bad['body'].replace('**Verdict**: APPROVE', '**Verdict**: APPROVE\n**Verdict**: BLOCK'))]
    elif name == 'duplicate-build':
        comments = [dict(bad, body=bad['body'].replace('**Verdict**: APPROVE', '**Verdict**: APPROVE\n**Build**: Passed\n**Build**: Passed'))]
    elif name in ('human-app-collision', 'app-human-collision'):
        entries = ['app:fixture'] if name.startswith('human') else ['user:fixture']
        comments = [dict(bad, author=dict(__typename='User' if name.startswith('human') else 'Bot', login='fixture'))]
    elif name in ('personal', 'app', 'app-prefix', 'actions', 'outsider-block-ignored', 'null-discussion', 'pinned-policy', 'checkout-policy'):
        expected = 0
        if name in ('app', 'app-prefix', 'actions'):
            slug = 'github-actions' if name == 'actions' else 'fixture'
            entries = ['app:' + slug]
            comments = [dict(good, author=dict(__typename='Bot', login=('app/' if name == 'app-prefix' else '') + slug + '[bot]'))]
        elif name == 'outsider-block-ignored':
            comments += [dict(bad, author=dict(__typename='User', login='outsider'), body=bad['body'].replace('APPROVE', 'BLOCK'))]
        elif name == 'null-discussion':
            comments += [dict(bad, author=None, body='Unrelated discussion')]
        elif name == 'pinned-policy':
            case['change_policy_after_read'] = True
    elif name in ('null-author', 'unknown-type', 'missing-type', 'empty-login'):
        expected = 2
        authors = {'null-author': None, 'unknown-type': dict(__typename='Organization', login='fixture'),
                   'missing-type': dict(login='fixture'), 'empty-login': dict(__typename='User', login='')}
        comments += [dict(bad, author=authors[name])]
    elif name in ('edit-approve-block', 'edit-block-approve', 'already-edited'):
        expected = 20
        if name == 'edit-block-approve':
            comments = [dict(good, body=good['body'].replace('APPROVE', 'BLOCK'))]
        selected = comments[0]
        if name == 'already-edited':
            selected['lastEditedAt'] = '2026-05-14T00:00:00Z'
        else:
            case['reread'] = dict(selected, body=selected['body'].replace('BLOCK', 'APPROVE') if name == 'edit-block-approve' else selected['body'].replace('APPROVE', 'BLOCK'))
    elif name in ('deleted', 'hidden', 'unreadable-body', 'null-slot', 'graphql-errors', 'count', 'cursor', 'duplicate-id', 'truncated', 'page-cap', 'deadline', 'later-block', 'later-read-error'):
        expected = 25 if name == 'later-block' else 2
        later = dict(bad, body=bad['body'].replace('APPROVE', 'BLOCK'))
        comments = [good, later]
        first = dict(totalCount=2, nodes=[good], pageInfo=dict(hasNextPage=True, endCursor='1'))
        last = dict(totalCount=2, nodes=[later], pageInfo=dict(hasNextPage=False, endCursor=None))
        case['pages'] = [first, last]
        if name == 'deleted':
            case['reread'] = None
        elif name == 'hidden':
            last['nodes'] = [dict(later, isMinimized=True)]
        elif name == 'unreadable-body':
            last['nodes'] = [dict(later, body=None)]
        elif name == 'null-slot':
            last['nodes'] = [None]
        elif name == 'graphql-errors':
            case['errors'] = True
        elif name == 'count':
            last['totalCount'] = 3
        elif name == 'cursor':
            last['pageInfo']['endCursor'] = '1'
        elif name == 'duplicate-id':
            last['nodes'][0] = dict(later, id=good['id'])
        elif name == 'truncated':
            first['pageInfo']['hasNextPage'] = False
        elif name == 'later-read-error':
            case['fail_page'] = '1'
        elif name == 'page-cap':
            case['pages'] = [dict(totalCount=101, nodes=[dict(good, id=str(i))],
                                  pageInfo=dict(hasNextPage=True, endCursor=str(i + 1))) for i in range(101)]
        elif name == 'deadline':
            case['slow_page'] = True
    elif name in ('policy-missing', 'policy-file-missing', 'policy-invalid', 'policy-empty', 'policy-duplicate', 'policy-too-many', 'policy-read-error'):
        if name == 'policy-missing':
            entries = None
        elif name == 'policy-file-missing':
            case['missing_file'] = True
        elif name == 'policy-invalid':
            entries = ['fixture']
        elif name == 'policy-empty':
            entries = []
        elif name == 'policy-duplicate':
            entries = ['user:fixture', 'user:Fixture']
        elif name == 'policy-too-many':
            entries *= 21
        elif name == 'policy-read-error':
            case['policy_read_failure'] = True
            expected = 2
    elif name in ('block', 'request-changes', 'block-without-head', 'block-without-base'):
        comments = [dict(good, body=good['body'].replace('APPROVE', 'BLOCK' if name.startswith('block') else 'REQUEST_CHANGES'))]
    elif name in ('read-error-red-ci', 'read-error-green-ci'):
        case['errors'] = True
        expected = 25 if name.endswith('red-ci') else 2
        if name.endswith('red-ci'):
            (data / 'check_runs.json').write_text('{"check_runs":[{"name":"ci","status":"completed","conclusion":"failure"}]}')
    elif name in ('threads-truncated', 'threads-errors', 'threads-null', 'status-truncated', 'status-errors'):
        expected = 2
        case[name] = True
    else:
        raise AssertionError(name)
    # JIT fixtures appear only on the second gate snapshot, never on the first.
    if phase == 'jit' and expected != 0 and not name.startswith('policy-'):
        case['jit_only'] = True
        case['jit_comments'] = copy.deepcopy(comments)
        initial = dict(good, lastEditedAt=None)
        if entries and entries[0].startswith('app:'):
            initial['author'] = dict(__typename='Bot', login=entries[0][4:] + '[bot]')
        comments = [initial]
        if name == 'read-error-red-ci':
            (data / 'check_runs.json').write_text('{"check_runs":[{"name":"ci","status":"completed","conclusion":"success"}]}')
            case['red_ci_after_review'] = True
    if name == 'block-without-head':
        pr['headRefOid'] = ''
    elif name == 'block-without-base':
        pr['baseRefName'] = ''
    pr['comments'] = comments
    path.write_text(json.dumps(pr))
    (data / 'publisher-case.json').write_text(json.dumps(case))
    policy = dict(agents={} if entries is None else dict(merge_publishers=entries))
    (data / 'default-policy.json').write_text(json.dumps(policy))
    # A malicious checkout cannot add its own authority.
    config = json.loads((root / '.bureau.json').read_text())
    config['agents']['merge_publishers'] = ['user:outsider'] if name != 'checkout-policy' else []
    (root / '.bureau.json').write_text(json.dumps(config))
    return expected


if __name__ == '__main__':
    print(arrange(Path(sys.argv[1]), sys.argv[2], sys.argv[3]))
