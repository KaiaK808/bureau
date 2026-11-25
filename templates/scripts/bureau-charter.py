#!/usr/bin/env python3
"""Trusted merge-charter policy and explicit, shared-Git enrollment.

Enforces policy integrity, the approved queue and path/label exceptions. Suite receipts, reports and main-CI observation remain declarations for later gates.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile
from urllib.parse import quote

SHA = re.compile(r'(?:[0-9a-f]{40}|[0-9a-f]{64})')
DEFAULTS = dict(enabled=False, approved_queue=None, exceptions=[], suite=None, red_main=None, report=False)
# Reuse Bureau's configured-hook and submodule protections. Charter policy must
# not take a hook opt-in from the checkout whose merge it is authorizing.
NO_HOOKS = ['bash', '--noprofile', '--norc', '-c',
            'source "$1" || exit; shift; _bureau_remote_git_hooks_mode() { printf "off\\n"; }; git "$@"',
            'bureau-charter', str(Path(__file__).resolve().with_name('bureau-env.sh'))]


def object_keys(value, allowed, where, required=()):
    if not isinstance(value, dict) or set(value) - set(allowed) or set(required) - set(value):
        raise ValueError(where + ' must be an object with only ' + ', '.join(allowed)
                         + ('; required: ' + ', '.join(required) if required else ''))


def text(value, where):
    if not isinstance(value, str) or not value.strip() or value != value.strip() or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise ValueError(where + ' must be a nonempty string without surrounding whitespace or control characters')
    return value


def relative_path(value, where, glob=False):
    text(value, where)
    if (value.startswith(('/', '~', ':(')) or re.match(r'^[A-Za-z]:', value) or '\\' in value
            or any(part in ('', '.', '..') for part in value.split('/'))
            or any(c in value for c in '![]{}') or (not glob and any(c in value for c in '*?'))):
        raise ValueError(where + ' must be a root-relative path without traversal, negation or unsupported glob syntax')
    return value


def strings(value, where, check=text, nonempty=False):
    if not isinstance(value, list) or (nonempty and not value):
        raise ValueError(where + ' must be ' + ('a nonempty' if nonempty else 'an') + ' array')
    for item in value:
        check(item, where)
    if len(set(value)) != len(value):
        raise ValueError(where + ' must not contain duplicates')
    return value


def policy_from_config(config):
    """Strict schema, including explicitly disabled declarations; omission is inert."""
    if not isinstance(config, dict) or not isinstance(config.get('agents', {}), dict):
        raise ValueError('charter configuration and agents must be objects')
    if 'merge_charter' not in config.get('agents', {}):
        return None
    raw = config['agents']['merge_charter']
    object_keys(raw, DEFAULTS, 'agents.merge_charter')
    policy = dict(DEFAULTS, exceptions=[]) | raw
    for key in ('enabled', 'report'):
        if type(policy[key]) is not bool:
            raise ValueError('agents.merge_charter.' + key + ' must be boolean')
    queue = policy['approved_queue']
    if queue is not None:
        object_keys(queue, ('label', 'cycle_id'), 'approved_queue')
        if len(queue) != 1:
            raise ValueError('approved_queue needs exactly one of label or cycle_id')
        text(next(iter(queue.values())), 'approved_queue selector')
        if 'cycle_id' in queue and not re.fullmatch(r'[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}', queue['cycle_id']):
            raise ValueError('approved_queue.cycle_id must be a UUID')
    entries = policy['exceptions']
    if not isinstance(entries, list):
        raise ValueError('exceptions must be an array')
    classes = set()
    for entry in entries:
        object_keys(entry, ('class', 'paths', 'labels'), 'exception', ('class',))
        name = text(entry['class'], 'exception.class')
        if name in classes:
            raise ValueError('exception class names must be unique')
        classes.add(name)
        paths = strings(entry.get('paths', []), 'exception.paths', lambda v, w: relative_path(v, w, glob=True))
        labels = strings(entry.get('labels', []), 'exception.labels')
        if not paths and not labels:
            raise ValueError('each exception needs at least one path or label selector')
    suite = policy['suite']
    if suite is not None:
        object_keys(suite, ('id', 'command', 'definition_files'), 'suite', ('id', 'command', 'definition_files'))
        text(suite['id'], 'suite.id')
        text(suite['command'], 'suite.command')
        strings(suite['definition_files'], 'suite.definition_files', relative_path, nonempty=True)
    red = policy['red_main']
    if red is not None:
        object_keys(red, ('workflows', 'poll_seconds', 'timeout_seconds'), 'red_main', ('workflows',))
        strings(red['workflows'], 'red_main.workflows', relative_path, nonempty=True)
        for path in red['workflows']:
            if not re.fullmatch(r'\.github/workflows/[^/]+\.ya?ml', path):
                raise ValueError('red_main.workflows must name literal .github/workflows/*.yml or *.yaml files')
        red = dict({'poll_seconds': 60, 'timeout_seconds': 3600}, **red)
        for key in ('poll_seconds', 'timeout_seconds'):
            if type(red[key]) is not int or red[key] < 1:
                raise ValueError('red_main.' + key + ' must be a positive integer')
        if red['poll_seconds'] > red['timeout_seconds']:
            raise ValueError('red_main.poll_seconds must not exceed timeout_seconds')
        policy['red_main'] = red
    if policy['report'] and (suite is None or red is None):
        raise ValueError('report requires suite and red_main declarations')
    return policy


def digest(policy):
    return hashlib.sha256(json.dumps(policy, sort_keys=True, separators=(',', ':'), ensure_ascii=True).encode()).hexdigest()


def read_json(raw):
    def unique(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError('duplicate JSON key')
            value[key] = item
        return value
    return json.loads(raw, object_pairs_hook=unique, parse_constant=lambda _: (_ for _ in ()).throw(ValueError('nonfinite JSON value')))


def git(repo, *args, codes=(0,), runner=None):
    env = {k: v for k, v in os.environ.items() if k not in ('GIT_DIR', 'GIT_COMMON_DIR', 'GIT_WORK_TREE', 'GIT_INDEX_FILE', 'BASH_ENV', 'ENV')}
    env.update(GIT_NO_REPLACE_OBJECTS='1', GIT_TERMINAL_PROMPT='0', GIT_LITERAL_PATHSPECS='1')
    try:
        result = subprocess.run([*(runner or ['git', '-c', 'core.hooksPath=/dev/null']), '-C', str(repo), *args],
                                capture_output=True, env=env, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        raise ValueError('charter Git read failed') from None
    if result.returncode not in codes:
        raise ValueError('charter Git operation failed: ' + args[0])
    return result


def commit(value):
    if not isinstance(value, str) or not SHA.fullmatch(value):
        raise ValueError('charter requires a full commit SHA')
    return value


def common_dir(repo):
    value = os.fsdecode(git(repo, 'rev-parse', '--git-common-dir').stdout).strip()
    if not value:
        raise ValueError('charter Git common directory is unavailable')
    return (Path(repo) / value).resolve(strict=True)


def local_common_dir(repo):
    """Resolve checkout metadata without starting Git, including linked worktrees."""
    root = Path(repo).resolve(strict=True)
    for parent in (root, *root.parents):
        marker = parent / '.git'
        try:
            marker.lstat()
        except FileNotFoundError:
            continue
        mode = marker.stat().st_mode
        if stat.S_ISDIR(mode):
            gitdir = marker.resolve(strict=True)
        elif stat.S_ISREG(mode):
            value = marker.read_text(encoding='utf-8').rstrip('\r\n')
            if not value.startswith('gitdir: ') or not value[8:] or '\n' in value or '\r' in value:
                raise ValueError('charter Git directory marker is invalid')
            gitdir = (parent / value[8:]).resolve(strict=True)
        else:
            raise ValueError('charter Git directory marker is invalid')
        mode = gitdir.stat().st_mode
        if not stat.S_ISDIR(mode) or not mode & 0o111 or not os.access(gitdir, os.X_OK):
            raise ValueError('charter Git directory is unreadable')
        shared = gitdir / 'commondir'
        try:
            shared.lstat()
        except FileNotFoundError:
            common = gitdir
        else:
            value = shared.read_text(encoding='utf-8').rstrip('\r\n')
            if not value or '\n' in value or '\r' in value:
                raise ValueError('charter Git common directory marker is invalid')
            common = (gitdir / value).resolve(strict=True)
        if not (gitdir / 'HEAD').is_file() or not all((common / name).is_dir() for name in ('objects', 'refs')):
            raise ValueError('charter Git metadata is incomplete')
        return common
    raise ValueError('charter Git common directory is unavailable')


def enrollment_path(repo, common=None):
    return (common_dir(repo) if common is None else common) / 'bureau' / 'charter' / 'enrollment.json'


def checked_enrollment_path(repo, common=None):
    """Only a missing component proves absence; inaccessible state never does."""
    path = enrollment_path(repo, common)
    for entry in (path.parent.parent.parent, path.parent.parent, path.parent, path):
        try:
            mode = entry.lstat().st_mode
        except FileNotFoundError:
            return path
        if stat.S_ISLNK(mode):
            raise ValueError('charter enrollment must not use symlinks')
        if entry != path and (not stat.S_ISDIR(mode) or not mode & 0o111 or not os.access(entry, os.X_OK)):
            raise ValueError('charter enrollment directory is unreadable')
    return path


def enrollment(repo, common=None):
    # A broken symlink or malformed record is enrolled but unusable, never absent.
    path = checked_enrollment_path(repo, common)
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return None
    except OSError:
        raise ValueError('charter enrollment is unreadable') from None
    try:
        value = read_json(raw)
        object_keys(value, ('version', 'repository', 'default_branch', 'default_commit', 'policy_digest'), 'enrollment',
                    ('version', 'repository', 'default_branch', 'default_commit', 'policy_digest'))
        if type(value['version']) is not int or value['version'] != 1:
            raise ValueError('unsupported charter enrollment version')
        commit(value['default_commit'])
        text(value['default_branch'], 'enrollment.default_branch')
        repository(value['repository'])
        if not re.fullmatch(r'[0-9a-f]{64}', value['policy_digest']):
            raise ValueError('invalid enrolled policy digest')
        return value
    except (ValueError, TypeError, UnicodeError):
        raise ValueError('charter enrollment is invalid') from None


def repository(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', value):
        raise ValueError('repository must be OWNER/REPO')
    return value


class GitHub:
    """Service boundary only; all policy and change parsing uses real Git blobs."""
    def __init__(self, repo, github):
        self.repo, self.github = repo, repository(github)

    def api(self, route):
        try:
            result = subprocess.run(['gh', 'api', '--method', 'GET', 'repos/' + self.github + route],
                                    capture_output=True, timeout=30)
            value = read_json(result.stdout)
            if result.returncode or not isinstance(value, dict) or value.get('errors') not in (None, []):
                raise ValueError()
            return value
        except (OSError, ValueError, UnicodeError, subprocess.TimeoutExpired):
            raise ValueError('charter GitHub read failed') from None

    def tip(self, branch):
        if not isinstance(branch, str) or git(self.repo, 'check-ref-format', 'refs/heads/' + branch, codes=(0, 1)).returncode:
            raise ValueError('invalid charter branch')
        return commit(self.api('/commits/' + quote(branch, safe='')).get('sha'))

    def pin(self):
        branch = self.api('').get('default_branch')
        return dict(default_branch=branch, default_commit=self.tip(branch))

    def fetch(self, sha):
        git(self.repo, 'fetch', '--no-tags', '--no-write-fetch-head', 'origin', commit(sha), runner=NO_HOOKS)
        if git(self.repo, 'rev-parse', '--verify', sha + '^{commit}').stdout.strip().decode() != sha:
            raise ValueError('fetched charter commit identity differs')

    def revalidate(self, pin):
        return self.pin() == {key: pin[key] for key in ('default_branch', 'default_commit')}


def blob_at(repo, sha, path):
    commit(sha)
    relative_path(path, 'policy input')
    rows = git(repo, 'ls-tree', '-z', sha, '--', path).stdout.split(b'\0')
    rows = [row for row in rows if row]
    if not rows:
        return None
    if len(rows) != 1 or rows[0].split(b'\t', 1)[0].split()[:2] not in ([b'100644', b'blob'], [b'100755', b'blob']):
        raise ValueError('charter input must be a regular tracked file: ' + path)
    return git(repo, 'show', sha + ':' + path).stdout


def load_policy(repo, source, pin=None, enrolling=False):
    """Never consult BUREAU_CONFIG, a worktree file, or ordinary discovery."""
    record = enrollment(repo)
    if record is None and not enrolling:
        return dict(kind='not-enrolled')
    if record is not None and record['repository'].lower() != source.github.lower():
        raise ValueError('charter enrollment belongs to another repository')
    pin = source.pin() if pin is None else pin
    object_keys(pin, ('default_branch', 'default_commit'), 'default-branch pin', ('default_branch', 'default_commit'))
    commit(pin['default_commit'])
    text(pin['default_branch'], 'default-branch pin')
    source.fetch(pin['default_commit'])
    raw = blob_at(repo, pin['default_commit'], '.bureau.json')
    if raw is None:
        raise ValueError('enrolled default-branch .bureau.json is missing')
    policy = policy_from_config(read_json(raw))
    if policy is None or not policy['enabled']:
        raise ValueError('enrolled default-branch merge_charter must be present and enabled; explicitly unenroll to deactivate')
    return dict(kind='ok', policy=policy, policy_digest=digest(policy), **pin)


def parse_changes(raw):
    """Consume the complete NUL protocol, including both rename/copy endpoints."""
    if not isinstance(raw, bytes) or (raw and not raw.endswith(b'\0')):
        raise ValueError('truncated charter change list')
    fields = raw.split(b'\0')[:-1]
    paths, index = [], 0
    while index < len(fields):
        status = fields[index].decode('ascii')
        index += 1
        if status in ('A', 'D', 'M', 'T'):
            count = 1
        elif re.fullmatch(r'[RCM][0-9]{1,3}', status) and int(status[1:]) <= 100:
            count = 2 if status[0] in 'RC' else 1
        else:
            raise ValueError('invalid charter change status')
        if index + count > len(fields):
            raise ValueError('truncated charter change endpoints')
        for field in fields[index:index + count]:
            path = field.decode('utf-8')
            if path.startswith('/') or any(part in ('', '.', '..') for part in path.split('/')):
                raise ValueError('invalid charter change path')
            paths.append(path)
        index += count
    return paths


def changed_paths(repo, base, head):
    commit(base); commit(head)
    bases = git(repo, 'merge-base', '--all', base, head).stdout.splitlines()
    if len(bases) != 1:
        raise ValueError('charter comparison needs exactly one merge base')
    ancestor = commit(bases[0].decode())
    # Copy detection includes unchanged sources; never lose a protected source
    # merely because it was copied into an otherwise unprotected directory.
    result = git(repo, 'diff', '--name-status', '-z', '--no-relative', '--no-ext-diff', '--no-textconv',
                 '--find-renames', '--find-copies', '--find-copies-harder', '--ignore-submodules=none',
                 ancestor, head, '--')
    if result.stderr:
        raise ValueError('charter diff reported incomplete or uncertain classification')
    return parse_changes(result.stdout)


def policy_change_class(repo, base, head, paths=None):
    paths = changed_paths(repo, base, head) if paths is None else paths
    return 'charter-policy' if '.bureau.json' in paths else None


def path_matches(pattern, path):
    """Root-relative, case-sensitive glob; a whole **/ may span zero directories."""
    expression, index = '', 0
    while index < len(pattern):
        if pattern[index:index + 2] == '**':
            index += 2
            if (index == 2 or pattern[index - 3] == '/') and pattern[index:index + 1] == '/':
                expression += '(?:.*/)?'
                index += 1
            else:
                expression += '.*'
        elif pattern[index] == '*':
            expression += '[^/]*'
            index += 1
        elif pattern[index] == '?':
            expression += '[^/]'
            index += 1
        else:
            expression += re.escape(pattern[index])
            index += 1
    return re.fullmatch(expression, path, re.DOTALL) is not None


def parse_issue_snapshot(raw, issue, with_cycle):
    value = read_json(raw)
    if not isinstance(value, dict) or value.get('errors') not in (None, []):
        raise ValueError('partial charter ticket snapshot')
    connection = value['data']['issues']
    nodes = connection['nodes']
    if not isinstance(nodes, list) or len(nodes) != 1 or connection['pageInfo']['hasNextPage'] is not False:
        raise ValueError('charter ticket snapshot is missing or incomplete')
    node = nodes[0]
    if node['identifier'] != issue:
        raise ValueError('charter ticket snapshot identity differs')
    labels = node['labels']
    if not isinstance(labels['nodes'], list) or labels['pageInfo']['hasNextPage'] is not False:
        raise ValueError('charter ticket labels are incomplete')
    names = [text(label['name'], 'ticket label') for label in labels['nodes']]
    cycle_id = None
    if with_cycle:
        cycle = node['cycle']  # Missing differs from an explicitly unassigned cycle.
        if cycle is not None:
            cycle_id = text(cycle['id'], 'ticket cycle')
            if not re.fullmatch(r'[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}', cycle_id):
                raise ValueError('invalid charter ticket cycle')
    return dict(labels=names, cycle_id=cycle_id)


def issue_snapshot(repo, issue, with_cycle):
    """One Linear read via the existing credential-safe helper, never a model."""
    if not isinstance(issue, str) or not re.fullmatch(r'[A-Za-z][A-Za-z0-9_]*-[1-9][0-9]*', issue):
        raise ValueError('charter requires a ticket identifier')
    key, number = issue.rsplit('-', 1)
    query = ('{ issues(first:2, filter:{team:{key:{eq:' + json.dumps(key) + '}} number:{eq:' + number + '}}) '
             '{ nodes { identifier labels(first:250) { nodes { name } pageInfo { hasNextPage } } '
             + ('cycle { id } ' if with_cycle else '') + '} pageInfo { hasNextPage } } }')
    env = {key: value for key, value in os.environ.items() if key not in ('BASH_ENV', 'ENV')}
    env.update(_BUREAU_LINEAR_SINGLE_ATTEMPT='1', _BUREAU_LINEAR_FAULT_FILE='')
    try:
        result = subprocess.run(['bash', '--noprofile', '--norc', '-c',
                                 'source "$1" >&2 || exit; bureau_load_env --export "$BUREAU_ENV_FILE" >&2 || exit; '
                                 'bureau_secret_copy API_KEY LINEAR_API_KEY || exit; linear_raw "$2" "$3"', 'bureau-charter',
                                 str(Path(__file__).resolve().with_name('bureau-config.sh')),
                                 json.dumps(dict(query=query)), '((.data.issues.nodes | type) == "array")'],
                                cwd=repo, capture_output=True, timeout=45, env=env)
        if result.returncode:
            raise ValueError('charter Linear read failed')
        return parse_issue_snapshot(result.stdout, issue, with_cycle)
    except (OSError, subprocess.TimeoutExpired, ValueError, UnicodeError, KeyError, TypeError):
        raise ValueError('charter ticket snapshot could not be read completely') from None


def selector_gates(policy, paths, read_issue):
    queue, entries = policy['approved_queue'], policy['exceptions']
    label_exceptions = any(entry.get('labels') for entry in entries)
    ticket, failed = None, False
    if queue is not None or label_exceptions:
        try:
            ticket = read_issue(queue is not None and 'cycle_id' in queue)
        except (ValueError, OSError):
            failed = True
    labels = ticket['labels'] if ticket is not None else []
    classes = []
    for entry in entries:
        path_match = any(path_matches(pattern, path) for pattern in entry.get('paths', []) for path in paths)
        label_match = any(label in labels for label in entry.get('labels', []))
        if path_match or label_match:
            classes.append(entry['class'])
    lines = []
    if classes:
        lines.append('charter-exception: classes ' + json.dumps(classes) + '; a maintainer must inspect and merge exceptions')
    if failed and label_exceptions:
        lines.append('charter-unclassified: exception labels require a complete ticket snapshot; a maintainer must inspect')
    # Exception holds always dominate an absent approval or an approval read failure.
    if lines:
        return 25, '\n'.join(lines)
    if queue is not None:
        approved = ticket is not None and (queue['label'] in labels if 'label' in queue
                                           else ticket['cycle_id'] is not None and ticket['cycle_id'].lower() == queue['cycle_id'].lower())
        if not approved:
            return 2, 'charter-queue: ' + ('ticket snapshot unavailable' if failed else 'approval outstanding') + '; waiting for the approved queue'
    return 0, ''


def gate(repo, source, pin, base_branch, head, issue=None):
    try:
        snapshot = load_policy(repo, source, pin)
        if snapshot['kind'] == 'not-enrolled':
            return 0, ''
    except (ValueError, OSError, UnicodeError) as exc:
        return 25, 'charter-unreadable: ' + str(exc)
    try:
        base = source.tip(base_branch)
        source.fetch(base); source.fetch(head)
        paths = changed_paths(repo, base, head)
        classification = policy_change_class(repo, base, head, paths)
    except (ValueError, OSError, UnicodeError) as exc:
        return 25, 'charter-unclassified: ' + str(exc)
    code, message = selector_gates(snapshot['policy'], paths, lambda cycle: issue_snapshot(repo, issue, cycle))
    if classification:
        policy_line = 'charter-policy: PR changes .bureau.json; a maintainer must merge policy changes'
        return 25, policy_line + ('\n' + message if message else '')
    if code == 25:
        return code, message
    try:
        if not source.revalidate(pin):
            return 2, 'charter-retry: default-branch policy changed during evaluation; retry with a fresh pin'
    except (ValueError, OSError):
        return 25, 'charter-unreadable: default-branch policy could not be revalidated'
    return code, message


def static_inputs(policy, read):
    errors, warnings = [], []
    for path in (policy['suite'] or {}).get('definition_files', []):
        try:
            if read(path) is None:
                errors.append('charter suite definition file is missing: ' + path)
        except (ValueError, OSError):
            errors.append('charter suite definition file is unreadable: ' + path)
    for path in (policy['red_main'] or {}).get('workflows', []):
        try:
            raw = read(path)
            if raw is None:
                errors.append('charter workflow declaration is missing: ' + path)
                continue
            body = raw.decode('utf-8')
            # Deliberately a declaration hint, not a YAML or branch-filter verdict.
            if not re.search(r'(?m)^\s*(?:push\s*:|on\s*:\s*(?:push\b|\[[^\]\n]*\bpush\b))', body):
                warnings.append('charter workflow has no statically recognizable push declaration: ' + path)
        except (ValueError, OSError, UnicodeError):
            errors.append('charter workflow declaration is unreadable: ' + path)
    if policy['red_main']:
        warnings.append('Charter workflow event/branch filters and suite coverage require maintainer review; no workflow or suite is executed by doctor')
    return errors, warnings


def enroll(repo, source):
    snapshot = load_policy(repo, source, enrolling=True)
    errors, _ = static_inputs(snapshot['policy'], lambda path: blob_at(repo, snapshot['default_commit'], path))
    if errors:
        raise ValueError('; '.join(errors))
    pin = {key: snapshot[key] for key in ('default_branch', 'default_commit')}
    if not source.revalidate(pin):
        raise ValueError('default branch changed before enrollment; retry')
    path = checked_enrollment_path(repo)
    for directory in (path.parent.parent, path.parent):
        if directory.is_symlink():
            raise ValueError('charter state directories must not be symlinks')
        directory.mkdir(mode=0o700, exist_ok=True)
    record = dict(version=1, repository=source.github, policy_digest=snapshot['policy_digest'], **pin)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix='.enrollment-', delete=False) as output:
            temporary = Path(output.name)
            output.write((json.dumps(record, sort_keys=True) + '\n').encode())
            output.flush(); os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return record


def diagnose(repo, config, details=None):
    """Offline diagnostics: current means locally fetched default-branch policy."""
    errors, warnings = [], []
    raw_agents = config.get('agents', {}) if isinstance(config, dict) else {}
    configured = isinstance(raw_agents, dict) and 'merge_charter' in raw_agents
    try:
        record = enrollment(repo)
        if not configured and record is None:
            return errors, warnings
        policy = policy_from_config(config)
        if record is None:
            if policy and policy['enabled']:
                warnings.append('Charter policy is enabled but enrollment is absent; no charter gate is active')
            else:
                return errors, warnings
        else:
            if policy is None or not policy['enabled']:
                warnings.append('Charter enrollment is present but checkout policy is absent or disabled; enrollment remains active')
            ref = git(repo, 'symbolic-ref', '--quiet', 'refs/remotes/origin/HEAD').stdout.strip().decode()
            if not ref.startswith('refs/remotes/origin/'):
                raise ValueError('locally fetched default-branch ref is unavailable')
            branch = ref[len('refs/remotes/origin/'):]
            if branch != record['default_branch']:
                warnings.append('Charter default-branch source drift: locally recorded origin/HEAD differs from enrollment')
            if git(repo, 'check-ref-format', 'refs/heads/' + branch, codes=(0, 1)).returncode:
                raise ValueError('invalid enrolled default branch')
            sha = commit(git(repo, 'rev-parse', '--verify', 'refs/remotes/origin/' + branch + '^{commit}').stdout.strip().decode())
            raw = blob_at(repo, sha, '.bureau.json')
            policy = policy_from_config(read_json(raw)) if raw is not None else None
            if policy is None or not policy['enabled']:
                raise ValueError('enrolled default-branch charter policy is absent or disabled')
            if digest(policy) != record['policy_digest']:
                warnings.append('Charter policy digest drift: locally fetched default-branch policy differs from enrollment; inspect and explicitly enroll again to acknowledge')
            warnings.append('Charter policy checked at locally fetched origin/' + branch + '; remote default-branch changes are not checked by doctor')
        if policy and policy['enabled']:
            if details is not None:
                queue = policy['approved_queue']
                details.update(enrolled=record is not None, queue_mode=next(iter(queue)) if queue else 'none',
                               approved_queue=queue, exception_classes=[entry['class'] for entry in policy['exceptions']])
            if record is not None and not policy['exceptions']:
                warnings.append('Enrolled charter has no configured exception classes; only the mandatory charter-policy class applies')
            teams = config.get('linear', {}).get('teams', [])
            if not teams or not isinstance(teams[0].get('states', {}).get('merge'), str) or not teams[0]['states']['merge'].strip():
                errors.append('Enabled charter requires a Merge state for human handoff')
            def read(path):
                if record is not None:
                    return blob_at(repo, sha, path)
                target = Path(repo) / path
                if target.is_symlink() or Path(repo).resolve() not in target.resolve().parents:
                    raise ValueError('unsafe charter input')
                return target.read_bytes() if target.exists() else None
            found_errors, found_warnings = static_inputs(policy, read)
            errors.extend(found_errors); warnings.extend(found_warnings)
        state = enrollment_path(repo).parent
        while not state.exists():
            state = state.parent
        if not state.is_dir() or not os.access(state, os.W_OK | os.X_OK) or not state.stat().st_mode & 0o222:
            errors.append('Charter shared Git state is not writable')
    except (ValueError, OSError, UnicodeError, TypeError, AttributeError) as exc:
        errors.append('Charter policy: ' + str(exc))
    return errors, warnings


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo', type=Path, default=Path('.'))
    parser.add_argument('--github', help='OWNER/REPO; required for remote policy operations')
    commands = parser.add_subparsers(dest='command', required=True)
    for name in ('enroll', 'unenroll', 'pin', 'policy'):
        commands.add_parser(name)
    status = commands.add_parser('status')
    status.add_argument('--local', action='store_true', help='Resolve shared Git metadata without running Git')
    check = commands.add_parser('gate')
    check.add_argument('--pin', required=True)
    check.add_argument('--base', required=True)
    check.add_argument('--head', required=True)
    check.add_argument('--issue')
    args = parser.parse_args(argv)
    try:
        if args.command == 'status':
            common = local_common_dir(args.repo) if args.local else None
            print('not-enrolled' if enrollment(args.repo, common) is None else 'enrolled')
            return 0
        if args.command == 'unenroll':
            path = checked_enrollment_path(args.repo)
            path.unlink(missing_ok=True)
            print('Charter enrollment removed; automatic charter checks are inactive')
            return 0
        if args.command == 'policy' and enrollment(args.repo) is None:
            print(json.dumps(dict(kind='not-enrolled')))
            return 0
        source = GitHub(args.repo, args.github)
        if args.command == 'gate':
            code, message = gate(args.repo, source, read_json(args.pin), args.base, args.head, args.issue)
            if message:
                print(message)
            return code
        if args.command == 'pin':
            value = source.pin()
        elif args.command == 'enroll':
            value = enroll(args.repo, source)
        else:
            value = load_policy(args.repo, source)
        print(json.dumps(value, sort_keys=True))
        return 0
    except (ValueError, OSError, UnicodeError, TypeError) as exc:
        print(('charter-unreadable: ' if args.command == 'gate' else 'charter: ') + str(exc))
        return 25


if __name__ == '__main__':
    sys.exit(main())
