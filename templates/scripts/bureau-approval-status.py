#!/usr/bin/env python3
"""Record exact-commit human approvals from GitHub events, using default-branch policy."""
import argparse
import base64
import json
from pathlib import Path
import re
import subprocess
import sys

CONTEXT = 'bureau/human-approval'
LOGIN = re.compile(r'[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*')
SHA = re.compile(r'[0-9a-fA-F]{40}')
COMMAND = re.compile(r'[ \t]*/bureau approve ([0-9a-fA-F]{40})[ \t]*')


def merge_approvers(config):
    if not isinstance(config, dict) or not isinstance(config.get('agents', {}), dict):
        raise ValueError('invalid approval configuration')
    value = config.get('agents', {}).get('merge_approvers', [])
    if not isinstance(value, list) or len(value) > 20 or any(
            not isinstance(login, str) or not 1 <= len(login) <= 39 or not LOGIN.fullmatch(login)
            for login in value):
        raise ValueError('agents.merge_approvers must be an array of at most 20 GitHub logins')
    return {login.lower() for login in value}


def api(repo, route='', fields=None, method='GET'):
    command = ['gh', 'api', '--method', method, 'repos/' + repo + route]
    for key, value in (fields or {}).items():
        command += ['-f', key + '=' + value]
    try:
        proc = subprocess.run(command, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired, UnicodeError):
        raise ValueError('GitHub API call failed') from None
    if proc.returncode != 0:
        raise ValueError('GitHub API call failed')
    if method == 'POST': return None
    try: return json.loads(proc.stdout)
    except ValueError: raise ValueError('unreadable GitHub API response') from None


def trusted_approvers(repo):
    repository = api(repo)
    branch = field(repository, 'default_branch', str)
    # Explicit GET: gh otherwise turns a request with fields into a POST.
    contents = api(repo, '/contents/.bureau.json', {'ref': branch})
    try:
        if contents['encoding'] != 'base64': raise ValueError()
        content = base64.b64decode(''.join(contents['content'].split()), validate=True)
        config = json.loads(content)
    except (ValueError, KeyError, TypeError, AttributeError):
        raise ValueError('unreadable default-branch approval configuration') from None
    return merge_approvers(config)


def field(value, key, kind):
    found = value.get(key) if isinstance(value, dict) else None
    if type(found) is not kind or (kind is str and not found):
        raise ValueError('event or GitHub response lacks a required field')
    return found


def commit(value):
    if not isinstance(value, str) or not SHA.fullmatch(value):
        raise ValueError('event or GitHub response lacks a full commit SHA')
    return value.lower()


def account(value):
    login = field(value, 'login', str)
    kind = field(value, 'type', str)
    plain = login[:-5] if login.lower().endswith('[bot]') else login
    if not 1 <= len(plain) <= 39 or not LOGIN.fullmatch(plain):
        raise ValueError('event lacks a valid GitHub login')
    return login, kind


def status(repo, head, description, url=None):
    fields = dict(context=CONTEXT, state='success', description=description)
    if url is not None: fields['target_url'] = url
    api(repo, '/statuses/' + head, fields, method='POST')


def decide(event, payload, repo):
    approvers = trusted_approvers(repo)
    if not approvers: return 0, 'ignored: agents.merge_approvers is absent or empty'
    action = field(payload, 'action', str)
    if event == 'issue_comment':
        if action != 'created': return 0, 'ignored: comment was not created'
        issue = field(payload, 'issue', dict)
        if not isinstance(issue.get('pull_request'), dict): return 0, 'ignored: comment is not on a pull request'
        source = field(payload, 'comment', dict)
        body = source.get('body')
        if not isinstance(body, str): raise ValueError('event lacks a comment body')
        first_line = re.split(r'\r\n?|\n', body, maxsplit=1)[0]
        match = COMMAND.fullmatch(first_line)
        if not match: return 0, 'ignored: comment has no exact approval command'
        candidate = commit(match.group(1))
        number = field(issue, 'number', int)
    else:
        return 0, 'ignored: event needs no status'
    if number <= 0: raise ValueError('event lacks a valid pull request number')

    login, kind = account(field(source, 'user', dict))
    sender, sender_kind = account(field(payload, 'sender', dict))
    url = field(source, 'html_url', str)
    if not url.startswith('https://') or any(c.isspace() for c in url):
        raise ValueError('event lacks a valid approval URL')

    current = api(repo, '/pulls/' + str(number))
    head = commit(field(field(current, 'head', dict), 'sha', str))
    state = field(current, 'state', str)
    merged = field(current, 'merged', bool)
    if state != 'open' or merged: return 0, 'refused: pull request is closed or merged'
    if (kind != 'User' or sender_kind != 'User' or login.lower().endswith('[bot]')
            or sender.lower().endswith('[bot]') or sender.lower() != login.lower() or login.lower() not in approvers):
        return 1, 'refused: ' + login + ' is not an allowed approval actor'
    if candidate != head:
        return 1, 'refused: approval for ' + candidate[:7] + ' does not match current head ' + head[:7]
    status(repo, head, 'approved by ' + login, url)
    return 0, 'approved: ' + login + ' approved ' + head[:7]


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise ValueError('usage: --event-name NAME --event-path PATH --repo OWNER/REPO')


def main(argv=None):
    parser = Parser(description=__doc__)
    parser.add_argument('--event-name', required=True)
    parser.add_argument('--event-path', required=True)
    parser.add_argument('--repo', required=True)
    try:
        args = parser.parse_args(argv)
        if not re.fullmatch(r'[A-Za-z0-9-]+/[A-Za-z0-9_.-]+', args.repo):
            raise ValueError('repository must be OWNER/REPO')
        try: payload = json.loads(Path(args.event_path).read_text())
        except (OSError, ValueError): raise ValueError('unreadable event payload') from None
        if not isinstance(payload, dict): raise ValueError('event payload must be an object')
        code, message = decide(args.event_name, payload, args.repo)
    except ValueError as exc:
        code, message = 2, 'refused: ' + str(exc)
    print(message)
    return code


if __name__ == '__main__':
    sys.exit(main())
