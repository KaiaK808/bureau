#!/usr/bin/env python3
"""Offline policy/GraphQL transport shared by merge drivers; project requested fields."""
import base64
import json
import os
from pathlib import Path
import re
import sys
import time


def project(value, selection):
    if isinstance(value, list):
        return [project(v, selection) for v in value]
    if value is None:
        return None
    return {key: project(value.get(key), nested) if nested else value.get(key)
            for key, nested in selection.items()}


def selection(query, name):
    # Parse selection sets, rather than silently supplying unrequested metadata.
    tail = query[query.index(name) + len(name):]
    tail = tail[tail.index('{'):]
    tokens = iter(re.findall(r'[A-Za-z_][A-Za-z_0-9]*|[{}]', tail))
    def parse():
        result, last = {}, None
        for token in tokens:
            if token == '}':
                return result
            if token == '{':
                result[last] = parse()
            else:
                last = token
                result[token] = None
        raise AssertionError('unclosed selection')
    assert next(tokens) == '{'
    return parse()


def main():
    args = sys.argv[1:]
    if args[:1] != ['api']:
        return 3
    route = args[1]
    method = args[args.index('--method') + 1] if '--method' in args else ('POST' if '-f' in args or '-F' in args else 'GET')
    if method != ('POST' if route == 'graphql' else 'GET'):
        return 1
    data = Path(os.environ.get('PR2_GH') or os.environ['STUB_DIR'])
    casefile = data / 'publisher-case.json'
    case = json.loads(casefile.read_text()) if casefile.exists() else {}
    fields = dict(a.split('=', 1) for a in args if '=' in a)
    policy = data / 'default-policy.json'
    if route == 'user':
        return 1  # Installation and Actions tokens need no /user endpoint.
    if route == 'repos/test-owner/test-repo':
        if case.get('policy_read_failure'):
            return 1
        value = dict(default_branch='main')
    elif route.endswith('/commits/main'):
        value = dict(sha='c' * 40)
    elif '/git/trees/' in route:
        value = dict(truncated=False, tree=[] if case.get('missing_file') else [dict(path='.bureau.json')])
    elif route.endswith('/contents/.bureau.json'):
        assert fields.get('ref') == 'c' * 40, 'policy was not pinned to a commit'
        tick = data / 'policy-reads'
        tick.write_text(str(int(tick.read_text()) + 1) if tick.exists() else '1')
        raw = policy.read_bytes() if policy.exists() else b'{"agents":{"merge_publishers":["user:fixture"]}}'
        value = dict(encoding='base64', content=base64.b64encode(raw).decode())
        if case.get('change_policy_after_read'):
            policy.write_text('{"agents":{"merge_publishers":[]}}')
    elif route.endswith('/check-runs') and case.get('red_ci_after_review'):
        red = (data / 'review-reads').exists() and int((data / 'review-reads').read_text()) >= 2
        value = dict(check_runs=[dict(name='ci', status='completed', conclusion='failure' if red else 'success')])
    elif route.endswith('/status') and 'page' in fields:
        if (data / 'fail_status').exists():
            return 1
        value = json.loads((data / 'status.json').read_text())
        value = dict(value, total_count=value.get('total_count', len(value['statuses'])),
                     statuses=[dict(v, id=v.get('id', i), context=v.get('context', 'ci')) for i, v in enumerate(value['statuses'])])
        if case.get('status-truncated'):
            value['total_count'] = 1
        if case.get('status-errors'):
            value['errors'] = [dict(message='partial response')]
    elif route == 'graphql':
        query = fields['query']
        if 'comments(' in query:
            if (data / 'fail_comments_read').exists():
                return 1
            counter = data / 'review-reads'
            tick = int(counter.read_text()) + 1 if counter.exists() else 1
            counter.write_text(str(tick))
            if case.get('jit_only') and tick == 1:
                case = {}
            if 'fail_page' in case and case['fail_page'] == fields.get('cursor'):
                return 1
            if case.get('slow_page'):
                time.sleep(.15)
            pages = case.get('pages')
            cursor = fields.get('cursor')
            if pages:
                page = pages[int(cursor or 0)]
            else:
                source = data / 'comments.json'
                nodes = json.loads(source.read_text()) if source.exists() else json.loads((data / 'pr_view.json').read_text())['comments']
                nodes = case.get('jit_comments', nodes)
                nodes = [dict(v, isMinimized=v.get('isMinimized', False), author=v.get('author', dict(__typename='User', login='fixture'))) if v is not None else None for v in nodes]
                if 'author_override' in case:
                    nodes = [dict(v, author=case['author_override']) if v is not None else None for v in nodes]
                page = dict(totalCount=len(nodes), nodes=nodes, pageInfo=dict(hasNextPage=False, endCursor=None))
            page = dict(page, nodes=[dict(v, isMinimized=v.get('isMinimized', False)) if v is not None else None for v in page['nodes']])
            value = dict(data=dict(repository=dict(pullRequest=dict(comments=project(page, selection(query, 'comments('))))))
            if case.get('errors'):
                value['errors'] = [dict(message='partial response')]
        elif 'node(id:$id)' in query:
            if (data / 'fail_comment_metadata').exists():
                return 1
            source = data / 'comments.json'
            nodes = json.loads(source.read_text()) if source.exists() else json.loads((data / 'pr_view.json').read_text())['comments']
            if case.get('jit_only') and (data / 'review-reads').read_text() == '1':
                case = {}
            nodes = case.get('jit_comments', nodes)
            selected = next((v for v in nodes if v and v['id'] == fields['id']), None)
            if selected:
                selected = dict(selected, isMinimized=selected.get('isMinimized', False), author=selected.get('author', dict(__typename='User', login='fixture')))
            if 'reread' in case:
                selected = case['reread']
            if selected is not None:
                selected = dict(selected, isMinimized=selected.get('isMinimized', False))
            value = dict(data=dict(node=project(selected, selection(query, 'IssueComment'))))
        elif 'reviewThreads(' in query:
            if (data / 'fail_threads_read').exists():
                return 1
            source = data / ('threads.json' if (data / 'threads.json').exists() else 'review_threads.json')
            page = json.loads(source.read_text())['data']['repository']['pullRequest']['reviewThreads']
            page = dict(page, totalCount=page.get('totalCount', len(page['nodes'])),
                        pageInfo=page.get('pageInfo', dict(hasNextPage=False, endCursor=None)),
                        nodes=[dict(v, id=v.get('id', 'thread-' + str(i))) for i, v in enumerate(page['nodes'])])
            if case.get('threads-truncated'):
                page['totalCount'] += 1
            if case.get('threads-null'):
                page['nodes'] = [None]
            value = dict(data=dict(repository=dict(pullRequest=dict(reviewThreads=project(page, selection(query, 'reviewThreads('))))))
            if case.get('threads-errors'):
                value['errors'] = [dict(message='partial response')]
        else:
            return 3
    else:
        return 3
    print(json.dumps(value))
    return 0


if __name__ == '__main__':
    sys.exit(main())
