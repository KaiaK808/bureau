#!/usr/bin/env python3
"""Local Bureau ownership and resumable stage protocol (no model subprocesses)."""
import argparse
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
import re
from pathlib import Path
import shlex
import signal
import subprocess
import sys
import time
import uuid

SCRIPTS = Path(__file__).resolve().parent
STAGES = {
    'spec': ('triage', 'spec_review'), 'spec_review': ('spec_review', 'build'),
    'ux': ('design', 'build'), 'copy': ('copy', 'build'),
    'implement': ('build', 'build_review'), 'qa': ('qa', 'build_review'),
    'code_review': ('build_review', None),
}


class Conflict(Exception):
    pass


def process_env(command, environ=None):
    # Bureau's own git and gh calls run without the Bureau secrets: hooks,
    # filters, an fsmonitor or a credential helper they start can come from the
    # branch. The same rule as process_env in bureau-provider.py and the git()
    # and gh() functions in bureau-env.sh (tests/test_untrusted_env_bureau.sh
    # compares them): never the three .env keys, their copies (6 characters or
    # more anywhere inside a value, a shorter one as the whole value), BASH_ENV
    # or ENV; the GitHub token variables only for gh and for git commands that
    # talk to a remote. Kept here so this script needs no other file.
    environ = os.environ if environ is None else environ
    names = ['LINEAR_API_KEY', 'TELEGRAM_BOT_TOKEN', 'TELEGRAM_ALERT_CHAT_ID']
    sub, skip = '', False
    for arg in command[1:]:
        if skip: skip = False
        elif arg in ('-C', '-c', '--git-dir', '--work-tree', '--namespace', '--super-prefix', '--config-env'): skip = True
        elif not arg.startswith('-'): sub = arg; break
    if os.path.basename(command[0]) != 'gh' and sub not in ('push', 'fetch', 'pull', 'ls-remote', 'clone', 'remote', 'submodule'):
        names += ['GH_TOKEN', 'GITHUB_TOKEN', 'GH_ENTERPRISE_TOKEN', 'GITHUB_ENTERPRISE_TOKEN']
    values = [environ.get(name, '') for name in names]
    long_secrets = [value for value in values if len(value) >= 6]
    short_secrets = {value for value in values if 0 < len(value) < 6}
    return {key: value for key, value in environ.items()
            if key not in names and key not in ('BASH_ENV', 'ENV') and value not in short_secrets
            and not any(secret in value for secret in long_secrets)}


def git(repo, *args):
    command = ['git', '-C', str(repo), *args]
    return subprocess.check_output(command, text=True, env=process_env(command)).strip()


def root_for(repo):
    return Path(git(repo, 'rev-parse', '--show-toplevel')).resolve()


def common_for(repo):
    return (repo / git(repo, 'rev-parse', '--git-common-dir')).resolve()


def config_for(repo):
    explicit = os.environ.get('BUREAU_CONFIG')
    if explicit:
        path = Path(explicit).resolve()
        if not path.is_file():
            raise ValueError('BUREAU_CONFIG does not exist: ' + str(path))
        return path
    for path in (repo / '.bureau.json', common_for(repo).parent / '.bureau.json'):
        if path.is_file():
            return path
    raise ValueError('No .bureau.json; run bureau-init or workspace setup')


def read(path, default=None):
    return json.loads(path.read_text()) if path.exists() else default


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + '.' + uuid.uuid4().hex)
    try:
        temp.write_text(json.dumps(value, indent=2) + '\n')
        os.chmod(temp, 0o600)
        temp.replace(path)
    finally:
        temp.unlink(missing_ok=True)


class Store:
    def __init__(self, repo):
        self.root = common_for(repo) / 'bureau'
        self.leases = self.root / 'leases.json'

    @contextmanager
    def guard(self):
        self.root.mkdir(parents=True, exist_ok=True)
        with (self.root / 'guard').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            yield

    def claim(self, issue, workspace, run, owner, pid=None):
        resources = ['issue:' + issue, 'workspace:' + str(workspace.resolve())]
        with self.guard():
            leases = read(self.leases, {})
            for resource in resources:
                prior = leases.get(resource)
                if prior and prior['run_id'] != run:
                    # App leases deliberately have no TTL: a quiet task is not dead.
                    raise Conflict(json.dumps({'resource': resource, 'owner': prior}))
            added = []
            for resource in resources:
                if resource not in leases:
                    leases[resource] = dict(run_id=run, owner=owner, pid=pid, started=time.time())
                    added.append(resource)
            save(self.leases, leases)
            return added

    def release(self, run, resources=None):
        with self.guard():
            leases = read(self.leases, {})
            leases = {k: v for k, v in leases.items()
                      if v['run_id'] != run or (resources is not None and k not in resources)}
            save(self.leases, leases)
            if not any(lease['run_id'] == run for lease in leases.values()):
                self.process_path(run).unlink(missing_ok=True)

    def process_path(self, run):
        return self.root / 'processes' / (run + '.json')

    def spawn(self, run, command, repo, env):
        # Registration and launch share the lock with cancellation. A nested
        # wrapper cannot launch an unrecorded group after its owner is stopped.
        with self.guard():
            path = self.process_path(run)
            record = read(path, {'groups': [], 'interrupted': False})
            if record['interrupted']:
                raise Conflict('Run was interrupted; inspect and release it before continuing')
            child = subprocess.Popen(command, cwd=repo, start_new_session=True, env=env)
            record['groups'].append(child.pid)
            try:
                save(path, record)
            except BaseException:
                try: os.killpg(child.pid, signal.SIGKILL)
                except ProcessLookupError: pass
                child.wait()
                raise
            return child

    def finish_execution(self, run, resources, interrupted, repo=None, report=True):
        with self.guard():
            path = self.process_path(run)
            record = read(path, {'groups': [], 'interrupted': False})
            if interrupted or record['interrupted']:
                record['interrupted'] = True
                save(path, record)
                leases = read(self.leases, {})
                issue = held_issue(leases, run)
                workspaces = []
                for key, lease in leases.items():
                    if lease['run_id'] == run:
                        lease['interrupted'] = True
                        if key.startswith('workspace:'):
                            workspace = key[len('workspace:'):]
                            workspaces.append(workspace)
                            worker = hashlib.sha256(workspace.encode()).hexdigest()
                            registration = self.root / 'workers' / worker
                            if registration.exists():
                                registration.unlink(missing_ok=True)
                                self.preserve(worker, run, issue, workspace, 'interrupted')
                save(self.leases, leases)
            else:
                record = None
        if record is None:
            self.release(run, resources)
            return
        # Nested wrappers of one run (shepherd → worker → stage) share it; only
        # the one that claimed it prints the steps, once, after the others.
        if report:
            workspace = workspaces[0] if workspaces else None
            try:
                message = interrupted_message(self, repo or self.root.parent.parent, run, issue, workspace)
            except Exception:  # the exit code is the protocol; the steps are help
                message = ('Interrupted Bureau run ' + run + ': work preserved; release it (bureau-runtime.py release '
                           + run + '), then drop or replace its worktree before a rerun.')
            print(message, file=sys.stderr)

    def preserve(self, key, run, issue, workspace, reason):
        # The record reset_worktree reads when it refuses the unregistered
        # worktree: whose run left it, on which branch, and why.
        branch = branch_state(workspace)[0]
        save(self.root / 'preserved' / (key + '.json'),
             dict(run_id=run, issue=issue, workspace=workspace, branch=branch or '', reason=reason, at=time.time()))

    def assert_stopped(self, run):
        # Reused live group IDs conservatively prevent release. Never signal
        # a stored ID here: it may now belong to an unrelated process.
        groups = set(read(self.process_path(run), {}).get('groups', []))
        live = set()
        for group in groups:
            try: os.killpg(group, 0)
            except ProcessLookupError: continue
            live.add(group)
        if live:
            # killpg(0) includes zombies, which cannot write. Fail closed if the
            # host cannot inspect process state rather than guessing it is safe.
            try:
                output = subprocess.check_output(['ps', '-axo', 'pgid=,stat='], text=True, stderr=subprocess.DEVNULL)
            except (OSError, subprocess.CalledProcessError):
                raise Conflict('Cannot confirm interrupted process groups stopped; inspect them before release')
            active = {int(parts[0]) for line in output.splitlines()
                      if len(parts := line.split()) >= 2 and not parts[1].startswith('Z')}
            if live & active:
                raise Conflict('Background process group still alive; stop it before releasing')

    def assert_owner(self, issue, workspace, run):
        leases = read(self.leases, {})
        for key in ('issue:' + issue, 'workspace:' + str(workspace.resolve())):
            if leases.get(key, {}).get('run_id') != run:
                raise Conflict('Run no longer owns ' + key)


def held_issue(leases, run):
    return next((key[len('issue:'):] for key, lease in leases.items()
                 if lease.get('run_id') == run and key.startswith('issue:')), None)


def held_workspace(leases, run):
    return next((key[len('workspace:'):] for key, lease in leases.items()
                 if lease.get('run_id') == run and key.startswith('workspace:')), None)


def quiet_git(repo, *args):
    # For the steps after an interrupt: a checkout that is gone or no checkout
    # at all is an answer here, not an error for the operator's terminal.
    command = ['git', '-C', str(repo), *args]
    return subprocess.check_output(command, text=True, env=process_env(command), stderr=subprocess.DEVNULL).strip()


def branch_state(workspace):
    """(branch, pushed, own) of a checkout: whether origin has the branch, and
    how many of its commits are not there (not on origin/<branch> when pushed,
    on no remote at all when not). ('', False, 0) when detached or unreadable."""
    try:
        branch = quiet_git(workspace, 'branch', '--show-current')
    except (OSError, subprocess.CalledProcessError):
        return '', False, 0
    if not branch:
        return '', False, 0
    try:
        quiet_git(workspace, 'rev-parse', '--verify', '--quiet', 'refs/remotes/origin/' + branch)
        pushed, count = True, ['rev-list', '--count', 'refs/remotes/origin/' + branch + '..HEAD']
    except (OSError, subprocess.CalledProcessError):
        pushed, count = False, ['rev-list', '--count', 'HEAD', '--not', '--remotes']
    try:
        return branch, pushed, int(quiet_git(workspace, *count))
    except (OSError, subprocess.CalledProcessError, ValueError):
        return branch, pushed, 1


def branch_step(path, alternative):
    """The step that drops a preserved worktree, and what it says about its
    branch. `git branch -D` only for a branch that was never pushed and has no
    commits of its own (the spec stage's fresh branch, whose name a rerun needs
    again). Commits that are on no remote are pushed first, never deleted: an
    implement stage whose final push failed leaves finished work there. A branch
    origin has needs no deletion: the rerun's `git checkout -B` resets it."""
    branch, pushed, own = branch_state(path)
    remove = 'git worktree remove --force ' + shlex.quote(str(path))
    if not branch or branch in ('main', 'master'):
        return ('Save anything you want from the worktree, then drop it:', [remove], alternative)
    quoted = shlex.quote(branch)
    if not pushed and not own:
        return ('Save anything you want from the worktree, then drop it and its local branch ' + branch
                + ', which was never pushed and has no commits of its own:', [remove, 'git branch -D ' + quoted], alternative)
    if not pushed:
        return ('Its local branch ' + branch + ' has ' + str(own) + ' commit(s) that are on no remote: push them (or keep the branch), '
                'then save anything else you want from the worktree and drop it:', ['git push -u origin ' + quoted, remove],
                'A rerun that creates a branch of this name needs it out of the way: keep it as `git branch -m ' + quoted + ' '
                + shlex.quote(branch + '-saved') + '` instead of deleting it. ' + alternative)
    if own:
        return ('Its branch ' + branch + ' has ' + str(own) + ' commit(s) that are not on origin/' + branch + ': push them (or keep them '
                'on another branch), then save anything else you want from the worktree and drop it:', ['git push origin ' + quoted, remove],
                'Deleting ' + branch + ' is not needed: the rerun\'s `git checkout -B` resets it to origin/' + branch + '. ' + alternative)
    return ('Save anything you want from the worktree, then drop it:', [remove],
            'Its branch ' + branch + ' is on origin; deleting it is not needed: the rerun\'s `git checkout -B` resets it. ' + alternative)


def linked_worktree(store, path):
    """Whether <path> is a linked worktree of this repository, the only kind the
    steps drop with `git worktree remove`: never the main checkout, nothing
    where nothing exists, and no directory Git does not know as a worktree."""
    if not path or not path.is_dir():
        return False
    try:
        top = Path(quiet_git(path, 'rev-parse', '--show-toplevel')).resolve()
        common = (path / quiet_git(path, 'rev-parse', '--git-common-dir')).resolve()
        own = Path(quiet_git(path, 'rev-parse', '--absolute-git-dir')).resolve()
    except (OSError, subprocess.CalledProcessError):
        return False
    # A plain directory under .worktrees/ answers for the main checkout around it.
    return top == path.resolve() and common == store.root.parent.resolve() and own != common


def resume_steps(store, repo, run, workspace, rerun):
    """The operator's way back after an interrupted run, as (text, [commands],
    note) steps: release the run, drop or replace its preserved worktree, rerun.
    Releasing alone is not enough: the worktree lost its disposable-worker
    registration, and reset_worktree refuses it with exit 21."""
    runtime = os.path.relpath(SCRIPTS / 'bureau-runtime.py', repo)
    if runtime.startswith('..'):
        runtime = str(SCRIPTS / 'bureau-runtime.py')
    runtime = 'python3 ' + shlex.quote(runtime)
    steps = []
    if run:
        steps.append(('Check that no process of run ' + run + ' is left (`' + runtime + ' status`), then release its ownership:',
                      [runtime + ' release ' + run], ''))
    path = Path(workspace) if workspace else None
    if linked_worktree(store, path):
        steps.append(branch_step(path, 'Or keep the worktree and rerun with a new one instead (`shepherd.sh --worktree DIR`): '
                                 'a rerun on this worktree stops with exit 21 until it is dropped.'))
    steps.append((rerun, [], ''))
    return steps


def render_steps(steps, markdown=False):
    lines = []
    for number, (text, commands, note) in enumerate(steps, 1):
        lines.append(('' if markdown else '    ') + str(number) + '. ' + text)
        if commands and markdown:
            lines += ['   ```sh'] + ['   ' + command for command in commands] + ['   ```']
        elif commands:
            lines += ['         ' + command for command in commands]
        if note:
            lines.append(('   ' if markdown else '       ') + note)
    return '\n'.join(lines)


def interrupted_message(store, repo, run, issue, workspace):
    head = 'Interrupted Bureau run ' + run + (' (' + issue + ')' if issue else '') + ': work preserved, nothing was reset or deleted.'
    if workspace and linked_worktree(store, Path(workspace)):
        head += ('\n  Worktree: ' + workspace + ' (its disposable-worker registration was removed, so no rerun resets it)')
    steps = resume_steps(store, repo, run, workspace, 'Rerun the shepherd or the stage.')
    return head + '\n  To resume, from ' + str(repo) + ':\n' + render_steps(steps)


def report_conflict(repo, store, args, exc):
    """A claim that another run holds. A live holder is working: say who and
    write nothing. A holder that is gone (interrupted, or its process no longer
    runs) leaves the ticket stuck until a human releases it: print the steps and
    leave them on the holder's ticket with needs-human, once (the queue skips a
    labelled ticket instead of hitting the same conflict on every pick)."""
    try:
        detail = json.loads(str(exc))
    except ValueError:
        return
    prior, resource = detail.get('owner') or {}, detail.get('resource', '')
    holder = prior.get('run_id')
    if not holder:
        return
    gone = None
    if prior.get('interrupted'):
        gone = 'was interrupted'
    elif prior.get('pid'):
        try: os.kill(prior['pid'], 0)
        except ProcessLookupError: gone = 'lost its process (pid ' + str(prior['pid']) + ')'
        except PermissionError: pass
    if not gone:
        print(resource + ' is held by run ' + holder + ' (' + str(prior.get('owner')) + '), which is still active: '
              'wait for it or stop it; nothing was written.', file=sys.stderr)
        return
    leases = read(store.leases, {})
    issue, workspace = held_issue(leases, holder), held_workspace(leases, holder)
    rerun = 'Remove `needs-human` from ' + (issue or 'the ticket') + ' and rerun.'
    steps = resume_steps(store, repo, holder, workspace, rerun)
    why = resource + ' is held by run ' + holder + ', which ' + gone + '; no new run can start until it is released.'
    print(why + '\n  To resume, from ' + str(repo) + ':\n' + render_steps(steps), file=sys.stderr)
    if not issue:
        return
    stage = Path(args.entry or 'exec').name
    stage = stage[:-len('-pipeline.sh')] if stage.endswith('-pipeline.sh') else stage[:-3] if stage.endswith('.sh') else stage
    body = ('🛑 Bureau halt (exit 21, ownership-conflict) in `' + stage + '`: ' + why.replace(resource, '`' + resource + '`', 1)
            + ('\n\nWorktree: `' + workspace + '`' if workspace else '')
            + '\n\nTo resume, from `' + str(repo) + '`:\n\n' + render_steps(steps, markdown=True))
    try:
        shell(repo, 'bureau_ownership_trace', issue, stage, workspace or '', body)
    except (OSError, subprocess.CalledProcessError) as error:
        print('bureau: could not leave the halt on ' + issue + ' (' + str(error) + ')', file=sys.stderr)


def shell(repo, function, *args):
    # Fixed helper names and positional arguments; never interpolate issue prose into shell code.
    script = '''source "$1/bureau-config.sh"
if [ -f "${BUREAU_ENV_FILE:-}" ]; then set -a; source "$BUREAU_ENV_FILE"; set +a; fi
shift
"$@"
'''
    return subprocess.check_output(['bash', '-e', '-c', script, 'bureau', str(SCRIPTS), function, *args], cwd=repo, text=True).strip()


def issue_snapshot(repo, issue):
    value = json.loads(shell(repo, 'bureau_issue_snapshot', issue))
    if not value.get('id') or not value.get('state', {}).get('id'):
        raise ValueError('Linear issue snapshot is missing identity/state')
    return value


def enabled(config, stage):
    value = config.get('agents', {}).get(stage, False)
    if isinstance(value, str): return value not in ('false', 'null')
    return value.get('enabled', True) if isinstance(value, dict) else bool(value)


def canonical_branch(comments):
    for comment in comments:
        first = comment.get('body', '').split('\n')[0]
        match = re.fullmatch(r'<!-- bureau-branch: ([^ ]+) -->\s*', first)
        if match: return match.group(1)
    return None


def stage_context(repo, stage, issue, run, config):
    paths = ['AGENTS.md', 'CLAUDE.md', 'SPEC.md', '.specify/memory/constitution.md', 'LESSONS.md']
    return dict(version=1, run_id=run, issue=issue['identifier'], stage=stage,
                workspace=str(repo), head=git(repo, 'rev-parse', 'HEAD'),
                branch=git(repo, 'branch', '--show-current'), state=issue['state'],
                issue_detail=issue, config=str(config_for(repo)),
                context=[p for p in paths if (repo / p).is_file()],
                specs_dir=config.get('repo', {}).get('specs_dir', 'specs'),
                protocol=str(SCRIPTS / 'bureau-stage.md'))


def prepare(repo, args, store):
    config = read(config_for(repo))
    snapshot = issue_snapshot(repo, args.issue)
    states = config['linear']['teams'][0]['states']
    expected = states.get(STAGES[args.stage][0])
    if not expected or snapshot['state']['id'] != expected:
        raise Conflict('Issue is not in the configured entry state for ' + args.stage)
    branch = git(repo, 'branch', '--show-current')
    canonical = canonical_branch(json.loads(shell(repo, 'get_issue_comments', args.issue)))
    if canonical and branch != canonical:
        raise Conflict('Attach the canonical issue branch before preparing: ' + canonical)
    if args.stage != 'spec' and (not branch or branch in ('main', 'master')):
        raise Conflict('Attach the issue branch before preparing this stage')
    run = uuid.uuid4().hex
    store.claim(args.issue, repo, run, args.owner)
    try:
        # A checkout adopted by an app task ceases to be a disposable worker.
        key = hashlib.sha256(str(repo).encode()).hexdigest()
        (store.root / 'workers' / key).unlink(missing_ok=True)
        context = stage_context(repo, args.stage, snapshot, run, config)
        context.update(canonical_branch=canonical, status='prepared', allow_merge=args.allow_merge, owner=args.owner,
                       config_hash=hashlib.sha256(config_for(repo).read_bytes()).hexdigest())
        save(store.root / 'runs' / (run + '.json'), context)
    except BaseException:
        store.release(run)
        raise
    print(json.dumps(context, indent=2))


def validate_result(repo, record, result):
    for key in ('run_id', 'stage'):
        if result.get(key) != record[key]:
            raise ValueError('result ' + key + ' does not match the prepared stage')
    if result.get('outcome') not in ('complete', 'partial', 'blocked') or not isinstance(result.get('summary'), str):
        raise ValueError('result requires outcome complete|partial|blocked and summary')
    if result.get('head') != git(repo, 'rev-parse', 'HEAD'):
        raise Conflict('Result head is stale')
    if record['stage'] == 'code_review' and result['head'] != record['head']:
        raise Conflict('Review head changed after prepare; prepare a fresh review')
    if result['outcome'] != 'complete':
        return
    artifacts = result.get('artifacts', [])
    if not isinstance(artifacts, list):
        raise ValueError('artifacts must be paths')
    for relative in artifacts:
        path = (repo / relative).resolve()
        if repo not in path.parents or not path.is_file():
            raise ValueError('artifact must exist inside the workspace: ' + relative)
    if record['stage'] == 'spec':
        if not {'spec.md', 'plan.md', 'tasks.md'} <= {Path(p).name for p in artifacts}:
            raise ValueError('spec completion requires spec.md, plan.md, tasks.md')
        specs = (repo / record['specs_dir']).resolve()
        folders = {(repo / p).resolve().parent for p in artifacts if Path(p).name in ('spec.md','plan.md','tasks.md')}
        if len(folders) != 1 or specs not in next(iter(folders)).parents:
            raise ValueError('spec artifacts must share one feature folder inside specs_dir')
    if record['stage'] in ('implement', 'qa'):
        tests = result.get('tests')
        if not isinstance(tests, list) or not tests or any(
                not isinstance(t, dict) or not t.get('command') or type(t.get('exit_code')) is not int or t['exit_code'] != 0 for t in tests):
            raise ValueError('completion requires successful test evidence')
    if record['stage'] == 'code_review' and result.get('verdict') not in ('APPROVE', 'REQUEST_CHANGES', 'BLOCK'):
        raise ValueError('review requires a recognized verdict')


def finish(repo, args, store):
    path = store.root / 'runs' / (args.run + '.json')
    record = read(path)
    if not record or record['workspace'] != str(repo):
        raise ValueError('unknown run or wrong workspace')
    result = read(Path(args.result))
    if not isinstance(result, dict):
        raise ValueError('result must be a JSON object')
    digest = hashlib.sha256(json.dumps(result, sort_keys=True).encode()).hexdigest()
    if record['status'] == 'finished':
        if record['result_hash'] != digest:
            raise Conflict('Run already finished with a different result')
        store.release(args.run)
        print(json.dumps(record, indent=2)); return
    if record.get('pending_hash') and record['pending_hash'] != digest:
        raise Conflict('Run already has a pending finish with a different result')
    store.assert_owner(record['issue'], repo, args.run)
    validate_result(repo, record, result)
    config = read(config_for(repo))
    if record['config_hash'] != hashlib.sha256(config_for(repo).read_bytes()).hexdigest():
        raise Conflict('Configuration changed since prepare; prepare a fresh stage')
    states = config['linear']['teams'][0]['states']
    current = issue_snapshot(repo, record['issue'])
    target = STAGES[record['stage']][1]
    if record['stage'] == 'spec_review':
        labels = {item['name'] for item in current.get('labels', {}).get('nodes', [])}
        configured_labels = config['linear'].get('labels', {})
        ux_label = configured_labels.get('needs_ux', {}).get('name', 'needs-ux')
        copy_label = configured_labels.get('needs_copy', {}).get('name', 'needs-copy')
        if ux_label in labels and enabled(config, 'ux') and states.get('design'):
            target = 'design'
        elif copy_label in labels and enabled(config, 'copy') and states.get('copy'):
            target = 'copy'
    if record['stage'] == 'implement' and enabled(config, 'qa') and states.get('qa'):
        target = 'qa'
    if record['stage'] == 'code_review':
        target = 'build' if result.get('verdict') == 'REQUEST_CHANGES' else None
        if result.get('verdict') == 'APPROVE' and record['allow_merge'] and states.get('merge'):
            target = 'merge'
    if result['outcome'] != 'complete':
        target = None
    target_id = record.get('target_state') if record.get('pending_hash') else (states.get(target) if target else record['state']['id'])
    if not target_id:
        raise ValueError('Missing configured target state: ' + str(target))
    if current['state']['id'] != record['state']['id']:
        if record.get('pending_hash') != digest or current['state']['id'] != target_id:
            raise Conflict('Issue state changed since prepare; result cannot advance it')
    branch = git(repo, 'branch', '--show-current')
    comments = json.loads(shell(repo, 'get_issue_comments', record['issue']))
    canonical = canonical_branch(comments)
    if canonical and canonical != branch:
        raise Conflict('Canonical issue branch changed or does not match this checkout')
    if record.get('canonical_branch') and canonical != record.get('canonical_branch'):
        raise Conflict('Canonical issue branch changed since prepare')
    if record['stage'] != 'spec' and branch != record['branch']:
        raise Conflict('Checkout branch changed since prepare')
    if result['outcome'] == 'complete' and (not branch or branch in ('main', 'master')):
        raise Conflict('Create or resume the issue branch before finishing a stage')
    record.update(pending_hash=digest, target_state=target_id)
    save(path, record)
    marker = '<!-- bureau-run: ' + args.run + ' -->'
    if result['outcome'] == 'blocked' or (record['stage'] == 'code_review' and result.get('verdict') == 'BLOCK'):
        label = config['linear'].get('labels', {}).get('needs_human', {}).get('name', 'needs-human')
        if label not in {item['name'] for item in current.get('labels', {}).get('nodes', [])}:
            shell(repo, 'add_issue_label', record['issue'], label)
    if not any(marker in c.get('body', '') for c in comments):
        body = (('<!-- bureau-branch: ' + branch + ' -->\n') if branch and branch not in ('main','master') else '') + marker + '\n' + result['summary']
        if result.get('verdict'):
            body += '\nVERDICT: ' + result['verdict']
        shell(repo, 'post_comment', record['issue'], body)
    if target_id != current['state']['id']:
        shell(repo, 'move_issue', record['issue'], target_id)
    record.update(status='finished', result_hash=digest, result=result)
    save(path, record)
    store.release(args.run)
    print(json.dumps(record, indent=2))


def stop_grace(depth):
    """Seconds a runtime waits for its child after forwarding a signal, before
    it kills the child's process group. The wrappers of one run nest (shepherd
    → worker → stage), and an outer kill of the inner wrapper's group also takes
    the inner runtime with it, before that one killed its own child: a stage
    that ignored the signal then ran on. So each level waits one step (5 s,
    BUREAU_STOP_GRACE_SECONDS) longer than the level inside it: 20 s at the
    top, 15, 10, then 5 s from the fourth level on."""
    unit = os.environ.get('BUREAU_STOP_GRACE_SECONDS', '')
    unit = int(unit) if unit.isdigit() and 1 <= int(unit) <= 600 else 5
    return unit * (4 - min(depth, 3))


def execute(repo, args, store):
    inherited = bool(os.environ.get('BUREAU_RUN_ID'))
    depth = os.environ.get('BUREAU_RUN_DEPTH', '')
    depth = int(depth) if inherited and depth.isdigit() else 0
    run = os.environ.get('BUREAU_RUN_ID') or uuid.uuid4().hex
    if not re.fullmatch(r'[a-f0-9]{32}', run):
        raise ValueError('BUREAU_RUN_ID must be a 32-character run ID')
    workspace = Path(args.workspace or repo).resolve()
    command = args.command[1:] if args.command[:1] == ['--'] else args.command
    if not command:
        raise ValueError('exec requires a command')
    child = None
    interrupted = None
    resources = []

    def signal_child(signum):
        if child is not None:
            try: os.killpg(child.pid, signum)
            except ProcessLookupError: pass

    def stop(signum, frame):
        nonlocal interrupted
        # Record cancellation even if it arrives during Popen, before the child
        # handle is assigned. Raising here can orphan that just-created process.
        interrupted = signum
        signal_child(signum)

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        try:
            resources = store.claim(args.issue, workspace, run, 'background', os.getpid())
        except Conflict as exc:
            # A cancelled run writes nothing, not even the halt of a conflict.
            if interrupted: return 130
            print('bureau conflict: ' + str(exc), file=sys.stderr)
            try:
                report_conflict(repo, store, args, exc)
            except Exception as error:  # the exit code is the protocol; the halt is help
                print('bureau: could not report the conflict (' + repr(error) + ')', file=sys.stderr)
            return 21
        if interrupted: return 130
        child = store.spawn(run, command, repo, env={**os.environ, 'BUREAU_RUN_ID': run, 'BUREAU_RUN_DEPTH': str(depth + 1), 'BUREAU_ACTIVE_ENTRY': args.entry or '',
                 'BUREAU_CURRENT_ISSUE': args.issue, 'BUREAU_CONFIG': str(config_for(repo))})
        if interrupted: signal_child(interrupted)
        while True:
            try:
                code = child.wait(timeout=.2)
                return 130 if interrupted else code
            except subprocess.TimeoutExpired:
                if interrupted:
                    try: child.wait(timeout=stop_grace(depth))
                    except subprocess.TimeoutExpired: signal_child(signal.SIGKILL); child.wait()
                    return 130
    finally:
        store.finish_execution(run, resources, interrupted, repo, report=bool(resources) or not inherited)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo', default='.')
    sub = parser.add_subparsers(dest='action', required=True)
    status = sub.add_parser('status')
    status.add_argument('--run')
    resume = sub.add_parser('resume'); resume.add_argument('run')
    claim = sub.add_parser('prepare')
    claim.add_argument('issue'); claim.add_argument('stage', choices=STAGES)
    claim.add_argument('--owner', default='app'); claim.add_argument('--allow-merge', action='store_true')
    done = sub.add_parser('finish'); done.add_argument('run'); done.add_argument('--result', required=True)
    release = sub.add_parser('release'); release.add_argument('run')
    runner = sub.add_parser('exec'); runner.add_argument('--issue', required=True)
    runner.add_argument('--workspace'); runner.add_argument('--entry'); runner.add_argument('command', nargs=argparse.REMAINDER)
    verify = sub.add_parser('assert-owner'); verify.add_argument('--issue', required=True)
    verify.add_argument('--workspace', required=True); verify.add_argument('--run', required=True)
    sub.add_parser('setup')
    sub.add_parser('pause'); sub.add_parser('unpause')
    args = parser.parse_args()
    if getattr(args, 'run', None) and not re.fullmatch(r'[a-f0-9]{32}', args.run):
        parser.error('run must be the 32-character ID returned by prepare/status')
    if getattr(args, 'issue', None) and not re.fullmatch(r'[A-Z][A-Z0-9]*-[0-9]+', args.issue):
        parser.error('issue must be an identifier such as TEAM-123')
    try:
        repo = root_for(Path(args.repo).resolve()); store = Store(repo)
        if args.action in ('pause', 'unpause'):
            with store.guard():
                marker = store.root / 'paused'
                if args.action == 'pause': marker.touch()
                else: marker.unlink(missing_ok=True)
            print(json.dumps({'paused': marker.exists()}))
        elif args.action == 'status':
            if args.run:
                print(json.dumps(read(store.root / 'runs' / (args.run + '.json')), indent=2))
            else:
                print(json.dumps({'workspace': str(repo), 'config': str(config_for(repo)),
                    'paused': (store.root / 'paused').exists(), 'leases': read(store.leases, {}),
                    'runs': [{k: v for k, v in read(p).items() if k in ('run_id','issue','stage','workspace','status')}
                             for p in sorted((store.root / 'runs').glob('*.json'))]}, indent=2))
        elif args.action == 'resume':
            record = read(store.root / 'runs' / (args.run + '.json'))
            if not record: raise ValueError('unknown run')
            snapshot = issue_snapshot(repo, record['issue'])
            print(json.dumps({'run': record, 'current_state': snapshot['state'],
                'current_head': git(repo, 'rev-parse', 'HEAD'), 'workspace_matches': record['workspace'] == str(repo),
                'leases': read(store.leases, {})}, indent=2))
        elif args.action == 'prepare': prepare(repo, args, store)
        elif args.action == 'finish':
            # Serialize retries for this run without blocking other issues.
            directory = store.root / 'runs'
            if not directory.is_dir(): raise ValueError('unknown run')
            with (directory / (args.run + '.guard')).open('a') as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                finish(repo, args, store)
        elif args.action == 'release':
            leases = read(store.leases, {})
            for lease in leases.values():
                if lease['run_id'] == args.run and lease.get('pid'):
                    try: os.kill(lease['pid'], 0)
                    except ProcessLookupError: pass
                    else: raise Conflict('Background owner still alive; stop it before releasing')
            store.assert_stopped(args.run)
            store.release(args.run)
            print(json.dumps({'released': args.run}))
        elif args.action == 'assert-owner': store.assert_owner(args.issue, Path(args.workspace), args.run)
        elif args.action == 'exec': return execute(repo, args, store)
        elif args.action == 'setup':
            source = config_for(repo).parent
            for name in ('.bureau.json', '.env'):
                target = repo / name
                if not target.exists() and (source / name).is_file():
                    with target.open('xb') as out:
                        os.chmod(target, 0o600); out.write((source / name).read_bytes())
            print(json.dumps({'workspace': str(repo), 'config': str(config_for(repo))}))
        return 0
    except Conflict as exc:
        print('bureau conflict: ' + str(exc), file=sys.stderr); return 21
    except (ValueError, OSError, subprocess.CalledProcessError, KeyError) as exc:
        print('bureau: ' + str(exc), file=sys.stderr); return 1


if __name__ == '__main__':
    sys.exit(main())
