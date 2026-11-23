"""Embedded offline responses for stateful worker and dispatcher fixtures."""
import base64
import json
import re


def project_review(value, fields):
    if isinstance(value, list):
        return [project_review(v, fields) for v in value]
    if value is None:
        return None
    return {k: project_review(value.get(k), nested) if nested else value.get(k)
            for k, nested in fields.items()}


def review_fields(query, name):
    tail = query[query.index(name) + len(name):]
    tokens = iter(re.findall(r'[A-Za-z_][A-Za-z_0-9]*|[{}]', tail[tail.index('{'):]))
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
        raise AssertionError('unclosed GraphQL selection')
    assert next(tokens) == '{'
    return parse()


def review_transport(args, commit, default_config, comments):
    """Return (handled, response); never read policy from a worker checkout.

    comments(number) returns stored comments with their actual author metadata;
    comments(None) returns all comments for an exact-node reread.
    """
    if args[:1] != ['api']:
        return False, None
    route = args[1]
    fields = dict(a.split('=', 1) for a in args[2:] if '=' in a)
    flags = {a.split('=', 1)[0]: flag for flag, a in zip(args[2:], args[3:])
             if flag in ('-f', '-F', '--raw-field', '--field') and '=' in a}
    method = args[args.index('--method') + 1] if '--method' in args else ('POST' if fields else 'GET')
    if route == 'repos/test/repo':
        value = dict(default_branch='main')
    elif route == 'repos/test/repo/commits/main':
        value = dict(sha=commit)
    elif route == 'repos/test/repo/git/trees/' + commit:
        value = dict(truncated=False, tree=[dict(path='.bureau.json')])
    elif route == 'repos/test/repo/contents/.bureau.json':
        assert fields == dict(ref=commit), 'publisher policy must use the pinned default-branch commit'
        value = dict(encoding='base64', content=base64.b64encode(json.dumps(default_config).encode()).decode())
    elif route.endswith('/status'):
        value = dict(total_count=0, statuses=[])
    elif route == 'graphql':
        query = fields['query']
        if 'node(id:$id)' in query:
            node = next((c for c in comments(None) if c['id'] == fields['id']), None)
            value = dict(data=dict(node=project_review(node, review_fields(query, 'IssueComment'))))
        else:
            # gh sends -f values as strings, which GraphQL rejects for $num:Int!.
            assert flags.get('num') in ('-F', '--field'), 'GraphQL Int variable must be a typed field'
            if 'comments(' in query:
                name = 'comments'
                nodes = comments(int(fields['num']))
            elif 'reviewThreads(' in query:
                name, nodes = 'reviewThreads', []
            else:
                raise AssertionError('unexpected GraphQL query')
            assert not fields.get('cursor'), 'fixture has only one page'
            page = dict(totalCount=len(nodes), nodes=nodes, pageInfo=dict(hasNextPage=False, endCursor=None))
            value = dict(data=dict(repository=dict(pullRequest={name: project_review(page, review_fields(query, name + '('))})))
    else:
        return False, None
    assert method == ('POST' if route == 'graphql' else 'GET'), 'fake must honor gh field/method behavior'
    return True, value
