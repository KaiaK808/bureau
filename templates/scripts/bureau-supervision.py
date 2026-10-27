#!/usr/bin/env python3
"""Persist review boundaries and spec reservations shared by Git worktrees."""
import argparse
from datetime import datetime
from contextlib import contextmanager
import fcntl
import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
import uuid

sys.dont_write_bytecode = True
_evidence_spec = importlib.util.spec_from_file_location('bureau_evidence', Path(__file__).resolve().with_name('bureau-evidence.py'))
e = importlib.util.module_from_spec(_evidence_spec)
_evidence_spec.loader.exec_module(e)


def wait_evidence(root, method, *args, **kwargs):
    return e.observe(lambda: getattr(e.Store(root), method)(*args, **kwargs))

# Keep equal to bureau-provider.py and bureau-env.sh (standalone script).
UNTRUSTED_HOST_PREFIXES = ('HERDR_',)

def process_env(command, environ=None):
    # Bureau's own git and gh calls run without the Bureau secrets: hooks,
    # filters, an fsmonitor or a credential helper they start can come from the
    # branch. The same rule as process_env in bureau-provider.py and the git()
    # and gh() functions in bureau-env.sh (tests/test_untrusted_env_bureau.sh
    # compares them): never the three .env keys, their copies (6 characters or
    # more anywhere inside a value, a shorter one as the whole value), BASH_ENV
    # or ENV, nor host pane variables; the GitHub token variables only for gh
    # and for git commands that talk to a remote. Kept here so this script
    # needs no other file.
    environ = os.environ if environ is None else environ
    names = ['LINEAR_API_KEY', 'TELEGRAM_BOT_TOKEN', 'TELEGRAM_ALERT_CHAT_ID']
    sub, skip = '', False
    for arg in command[1:]:
        if skip: skip = False
        elif arg in ('-C', '-c', '--git-dir', '--work-tree', '--namespace', '--super-prefix', '--config-env', '--attr-source', '--shallow-file'): skip = True
        elif not arg.startswith('-'): sub = arg; break
    if os.path.basename(command[0]) != 'gh' and sub not in ('push', 'fetch', 'pull', 'ls-remote', 'clone', 'remote', 'submodule'):
        names += ['GH_TOKEN', 'GITHUB_TOKEN', 'GH_ENTERPRISE_TOKEN', 'GITHUB_ENTERPRISE_TOKEN']
    values = [environ.get(name, '') for name in names]
    long_secrets = [value for value in values if len(value) >= 6]
    short_secrets = {value for value in values if 0 < len(value) < 6}
    return {key: value for key, value in environ.items()
            if key not in names and key not in ('BASH_ENV', 'ENV')
            and not key.startswith(UNTRUSTED_HOST_PREFIXES) and value not in short_secrets
            and not any(secret in value for secret in long_secrets)}


# Bureau's own git commands that talk to a remote run without the repository's hooks (v3.2): the
# switches the git() function in bureau-env.sh adds (_bureau_git_hooks_off), since such a command
# keeps the GitHub token variables and a hook from the branch would see them: no hooks directory,
# and hook.<event>.enabled=false for every event git knows, which turns off the hooks the
# configuration defines (git 2.55). The one command this script runs, ls-remote, updates no ref and
# fires no hook event in any git version, so the switches change nothing there today, git() adds
# the per-name switches of git 2.54 only where an event can fire, and repo.remote_git_runs_hooks
# (true or "operator") has nothing to bring back; they keep the rule the same at every remote
# command Bureau starts.
HOOK_EVENTS = ('applypatch-msg', 'commit-msg', 'fsmonitor-watchman', 'p4-changelist', 'p4-post-changelist',
               'p4-pre-submit', 'p4-prepare-changelist', 'post-applypatch', 'post-checkout', 'post-commit',
               'post-index-change', 'post-merge', 'post-receive', 'post-rewrite', 'post-update', 'pre-applypatch',
               'pre-auto-gc', 'pre-commit', 'pre-merge-commit', 'pre-push', 'pre-rebase', 'pre-receive',
               'prepare-commit-msg', 'proc-receive', 'push-to-checkout', 'reference-transaction',
               'sendemail-validate', 'update')
NO_HOOKS = ['-c', 'core.hooksPath=/dev/null'] + [part for event in HOOK_EVENTS for part in ('-c', 'hook.' + event + '.enabled=false')]


def git(repo, *args):
    command = ['git', '-C', str(repo), *args]
    return subprocess.check_output(command, text=True, env=process_env(command)).strip()


def directory(repo):
    root = Path(git(repo, 'rev-parse', '--show-toplevel')).resolve()
    return root, (root / git(root, 'rev-parse', '--git-common-dir')).resolve() / 'bureau'


def read(path):
    value = json.loads(path.read_text()) if path.exists() else {}
    if not isinstance(value, dict):
        raise ValueError('Review stops must be an object')
    return value


@contextmanager
def locked(root, timeout=None):
    root.mkdir(parents=True, exist_ok=True)
    with (root / 'review-stops.guard').open('a') as guard:
        if timeout is None:
            fcntl.flock(guard, fcntl.LOCK_EX)
        else:
            deadline = time.monotonic() + timeout
            while True:
                try:
                    fcntl.flock(guard, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline: raise TimeoutError('review boundary lock unavailable')
                    time.sleep(.01)
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


def spec_lock_timeout():
    value = os.environ.get('BUREAU_SPEC_LOCK_TIMEOUT', '300')
    if re.fullmatch(r'[0-9]{1,10}', value) and 1 <= int(value) <= 86400:
        return int(value)
    print('WARNING: invalid BUREAU_SPEC_LOCK_TIMEOUT; using 300 seconds', file=sys.stderr)
    return 300


def reserve_spec_number(repo, root, issue, specs_dir, base):
    """Allocate once per issue across every checkout sharing this Git common dir.

    The caller fetches all origin heads first. Inspect trees, not just branch
    names: feat/TEAM-123-... can contain 051 even though its name has no number.
    Reservations intentionally outlive failed runs; gaps are harmless.
    """
    specs = (repo / specs_dir).resolve().relative_to(repo).as_posix()
    if specs == '.' or specs.startswith('.git/') or specs == '.git':
        raise ValueError('specs_dir must name a directory inside the repository')
    git(repo, 'rev-parse', '--verify', 'refs/remotes/origin/' + base + '^{commit}')
    root.mkdir(parents=True, exist_ok=True)
    with (root / 'spec-numbers.guard').open('a') as guard:
        timeout = spec_lock_timeout()
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(guard, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError(f'timed out after {timeout}s waiting for spec allocator lock {guard.name}; stop or release its holder before retrying')
                time.sleep(min(0.1, remaining))
        path = root / 'spec-numbers.json'
        numbers = read(path)
        if any(not re.fullmatch(r'[A-Z][A-Z0-9]*-[0-9]+', key)
               or type(value) is not int or not 0 < value < 9223372036854775807
               for key, value in numbers.items()) or len(set(numbers.values())) != len(numbers):
            raise ValueError('Invalid spec number reservations; refusing to allocate')
        if issue in numbers:
            return {'issue': issue, 'number': numbers[issue]}

        used = list(numbers.values())

        def consider(name):
            # Match Spec Kit's sequential prefixes, excluding timestamp names.
            name = name.rsplit('/', 1)[-1]
            match = re.match(r'^([0-9]{3,})-', name)
            if match and not re.match(r'^[0-9]{8}-[0-9]{6}-', name):
                used.append(int(match[1]))

        refs = git(repo, 'for-each-ref', '--format=%(refname)',
                   'refs/remotes/origin/', 'refs/heads/').splitlines()
        refs.append('HEAD')
        worktrees = git(repo, 'worktree', 'list', '--porcelain', '-z')
        for field in worktrees.split('\0'):
            if field.startswith('HEAD '):
                refs.append(field[5:])  # Detached worktree commits count too.
            elif field.startswith('worktree '):
                directory = Path(field[9:]) / specs
                if directory.is_dir():
                    for entry in directory.iterdir():
                        if entry.is_dir():
                            consider(entry.name)  # Includes uncommitted work.
        for ref in sorted(set(refs)):
            consider(ref)
            for entry in git(repo, 'ls-tree', '-z', '-d', '--name-only', ref,
                             '--', specs + '/').split('\0'):
                consider(entry)
        number = max(used, default=0) + 1
        if number >= 9223372036854775807:
            raise ValueError('Spec number range exhausted')
        numbers[issue] = number
        save(path, numbers)
        return {'issue': issue, 'number': number}


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


def boundary_generation(record):
    return record.get('revision') or e.digest(record)


def resume(root, issue, expected=None, basis='observed-resume', resolved_at=None, terminal=False, lock_timeout=None):
    with locked(root, timeout=lock_timeout) as path:
        stops = read(path)
        if expected is not None and stops.get(issue) != expected:
            return False
        record = stops.pop(issue, None)
        removed = record is not None
        if removed:
            save(path, stops)
        # Keep boundary replacement and its evidence resolution serialized. An
        # older observation must not resolve a wait opened by a newer review.
        if removed:
            wait_evidence(root, 'resolve_waits', issue, ['approval', 'merge-gate'], basis, resolved_at,
                          terminal, generation_id=boundary_generation(record))
    return removed


def check(repo, root, issue, branch, state, detail):
    record = read(root / 'review-stops.json').get(issue)
    if not record:
        return {'stopped': False}
    same = (record['branch'] == branch and record['state'] == state
            and record['ticket_hash'] == fingerprint(detail, issue))
    terminal = False; merged_at = None
    if same:
        # Query the PR itself so a push, closure, or merge can invalidate the
        # stopped boundary without changing or resetting any local checkout.
        command = ['gh', 'pr', 'view', str(record['pr']), '--json', 'state,baseRefName,mergedAt']
        raw = subprocess.check_output(command, cwd=repo, text=True, timeout=30, env=process_env(command))
        current = json.loads(raw)
        if not isinstance(current, dict) or current.get('state') not in ('OPEN', 'CLOSED', 'MERGED'):
            raise ValueError('GitHub did not return the PR state')
        same = current['state'] == 'OPEN'
        terminal = current['state'] == 'MERGED'
        if terminal and current.get('mergedAt'):
            try: merged_at = datetime.fromisoformat(current['mergedAt'].replace('Z', '+00:00')).timestamp()
            except (ValueError, TypeError): pass
        if same:
            base_ref = current.get('baseRefName')
            if (not isinstance(base_ref, str) or not base_ref or base_ref.startswith('-')
                    or subprocess.run(['git', 'check-ref-format', 'refs/heads/' + base_ref], env=process_env(['git', 'check-ref-format']),
                                      cwd=repo, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode):
                raise ValueError('GitHub did not return a valid PR base branch')
            # Older boundaries were always recorded against main. A retarget
            # invalidates approval even when both base refs point at one commit.
            same = base_ref == record.get('base_ref', 'main')
        if same:
            # GitHub's PR snapshot can lag a branch update. Read both remote
            # refs directly; never equate a cached baseRefOid with its current tip.
            command = ['git', *NO_HOOKS, 'ls-remote', '--exit-code', 'origin', 'refs/heads/' + branch, 'refs/heads/' + base_ref]
            refs = subprocess.check_output(command, cwd=repo, text=True, timeout=30, env=process_env(command))
            tips = dict((ref, sha) for sha, ref in (line.split() for line in refs.splitlines()))
            if 'refs/heads/' + base_ref not in tips:
                raise ValueError('The current remote base is unavailable')
            same = tips.get('refs/heads/' + branch) == record['head'] and tips['refs/heads/' + base_ref] == record['base']
    if not same:
        removed = resume(root, issue, record,
               basis=('authoritative-pr-merge' if merged_at is not None else 'observed-pr-merge') if terminal else 'observed-approval-invalidated',
               resolved_at=merged_at, terminal=terminal)
        if not removed:
            return {'stopped': True, 'issue': issue, 'head': record['head'], 'pr': record['pr']}
    return {'stopped': same, 'issue': issue, 'head': record['head'], 'pr': record['pr']}


def reuse(root, issue, branch, state, head, base, base_ref, pr, raw_detail):
    """Consume a recorded approval for exactly these review inputs.

    A review that stopped before merge recorded its inputs. When the stage runs
    again without a stop, an approval of the same head and base, for the same PR
    and base branch, the same ticket text and state, is still the answer a new
    paid review would start from. Any difference, a record without a recorded
    APPROVE (written before verdicts were recorded), or a malformed record means
    a normal review; the record is removed either way, so it is used at most once
    and never outlives the inputs it describes. That includes a ticket detail that
    cannot be read or fingerprinted: it is judged under the lock like any other
    input, so it removes the record too instead of leaving it for a later run.

    The review stage writes the same record when an APPROVE's inline merge found its
    gate not yet decided (`stop --merge-gate-wait`); the answer says so, and the stage
    then retries the gate without posting its comments again. Such a record also keeps
    the build check that passed for its inputs (`--build-check passed` with its
    command and key) and the count of "not yet" answers in a row (`--gate-waits`): the stage
    does not run that build check again, and gate_waits() below spaces the rechecks.
    """
    with locked(root) as path:
        stops = read(path)
        record = stops.get(issue)
        if record is None:
            return {'reuse': False, 'reason': 'no recorded approval'}
        try:
            ticket_hash = fingerprint(json.loads(raw_detail), issue)
        except ValueError as exc:
            ticket_hash, mismatch = None, 'ticket detail unreadable (' + str(exc) + ')'
        else:
            wanted = (('verdict', 'APPROVE'), ('branch', branch), ('state', state), ('head', head),
                      ('base', base), ('base_ref', base_ref), ('pr', pr), ('ticket_hash', ticket_hash))
            mismatch = 'record is not an object' if not isinstance(record, dict) else next(
                (key for key, value in wanted if record.get(key) != value), None)
        del stops[issue]
        save(path, stops)
    if not record.get('merge_gate_wait'):
        wait_evidence(root, 'resolve_waits', issue, ['approval'], 'observed-review-resume',
                      generation_id=boundary_generation(record))
    if mismatch:
        prefix = '' if ticket_hash is None else 'recorded approval does not match: '
        return {'reuse': False, 'reason': prefix + mismatch}
    waits = record.get('gate_waits')
    return {'reuse': True, 'head': head, 'base': base, 'pr': pr, 'stopped_at': record.get('stopped_at'),
            'merge_gate_wait': record.get('merge_gate_wait') is True,
            # v3.2: the build check that passed for these inputs (gate waits only), and how
            # many times in a row the gate was not yet decided for this head.
            'build_check': 'passed' if record.get('build_check') == 'passed' else None,
            'build_command': record.get('build_command') if isinstance(record.get('build_command'), str) else None,
            'build_key': record.get('build_key') if isinstance(record.get('build_key'), str) else None,
            'gate_waits': waits if type(waits) is int and waits >= 1 else 1}


def gate_waits(repo, root, stage, first, cap, now=None):
    """The tickets a picker puts after all others for now (v3.2): they wait on their merge gate.

    stage "review": the review stage's APPROVE kept while the gate was not yet decided
    (review-stops.json, `merge_gate_wait`); stage "merge": the merge stage's mark after a
    gate that was not yet decided or blocked (merge-gate-waits.json, merge_wait()). A
    record holds its ticket for `first` seconds after the first answer in a row at the
    same head, doubling with each further one (`gate_waits`), never longer than `cap`
    seconds, counted from `stopped_at`. A record time in the future (a clock that stepped
    back) holds nothing, like a malformed record. Only while the branch on origin is
    still at the recorded head: one `git ls-remote` for all such branches; a branch that
    moved or is gone ends the hold. Returns {"waiting": [{"issue", "head", "due_in",
    "waits", "outcome"}]}; an unreadable file or origin raises (one line), and the caller
    then holds nothing back.
    """
    now = time.time() if now is None else now
    name = 'review-stops.json' if stage == 'review' else 'merge-gate-waits.json'
    held = []
    for issue, record in sorted(read(root / name).items()):
        if not isinstance(record, dict) or (stage == 'review' and record.get('merge_gate_wait') is not True):
            continue
        branch, head, at = record.get('branch'), record.get('head'), record.get('stopped_at')
        if (not re.fullmatch(r'[A-Z][A-Z0-9]*-[0-9]+', issue) or not isinstance(branch, str) or not branch
                or branch.startswith('-') or not isinstance(head, str) or not re.fullmatch(r'[0-9a-f]{40,64}', head)
                or type(at) not in (int, float) or not math.isfinite(at) or at > now):
            continue
        waits = record.get('gate_waits')
        waits = waits if type(waits) is int and waits >= 1 else 1
        wait = min(first * 2 ** (min(waits, 16) - 1), cap)
        left = at + wait - now
        if left > 0:
            outcome = record.get('outcome') if record.get('outcome') in ('not-yet', 'blocked') else 'not-yet'
            held.append((issue, branch, head, int(math.ceil(left)), waits, outcome.replace('-', ' ')))
    if not held:
        return {'waiting': []}
    command = ['git', *NO_HOOKS, 'ls-remote', 'origin'] + sorted({'refs/heads/' + branch for _, branch, _, _, _, _ in held})
    answer = subprocess.run(command, cwd=repo, text=True, timeout=30, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            env=process_env(command))
    if answer.returncode:
        # git's own reason in one line: its first fatal line, else its last line.
        lines = [line.strip() for line in answer.stderr.splitlines() if line.strip()]
        reason = next((line for line in lines if line.startswith('fatal:')), lines[-1] if lines else 'exit %d' % answer.returncode)
        raise ValueError('git ls-remote origin failed: ' + reason)
    tips = {ref: sha for sha, ref in (line.split('\t', 1) for line in answer.stdout.splitlines() if '\t' in line)}
    return {'waiting': [{'issue': issue, 'head': head, 'due_in': left, 'waits': waits, 'outcome': outcome}
                        for issue, branch, head, left, waits, outcome in held if tips.get('refs/heads/' + branch) == head]}


def merge_wait(root, issue, branch=None, head=None, outcome=None, clear=False):
    """The merge stage's mark of a gate that was not yet decided or blocked (v3.2).

    Counts the answers in a row at the same branch and head (`gate_waits`), so the
    merge picker's hold doubles; `clear` removes the mark after a merge.
    """
    with locked(root) as stops_path:
        path = stops_path.parent / 'merge-gate-waits.json'
        marks = read(path)
        if clear:
            removed = marks.pop(issue, None) is not None
            if removed:
                save(path, marks)
            wait_evidence(root, 'resolve_waits', issue, ['merge-gate'], 'observed-merge-wait-cleared')
            return {'issue': issue, 'cleared': removed}
        prior = marks.get(issue)
        waits = 1
        if isinstance(prior, dict) and prior.get('branch') == branch and prior.get('head') == head \
                and type(prior.get('gate_waits')) is int and prior['gate_waits'] >= 1:
            waits = prior['gate_waits'] + 1
        marks[issue] = {'branch': branch, 'head': head, 'outcome': outcome, 'stopped_at': time.time(), 'gate_waits': waits}
        save(path, marks)
    wait_evidence(root, 'open_wait', issue, 'merge-gate', 'dependency' if outcome == 'not-yet' else 'technical',
                  'Merge gate is not yet decided' if outcome == 'not-yet' else 'Merge gate is blocked',
                  'ci-or-gate-owner' if outcome == 'not-yet' else 'repository-maintainer', 'merge', 2 if outcome == 'not-yet' else 25,
                  os.environ.get('BUREAU_RUN_ID'))
    return {'issue': issue, 'head': head, 'gate_waits': waits}


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
    allocate = commands.add_parser('reserve-spec-number'); allocate.add_argument('issue')
    allocate.add_argument('--specs-dir', default='specs'); allocate.add_argument('--base', default='main')
    waits = commands.add_parser('gate-waits'); waits.add_argument('--cap', type=int, required=True)
    waits.add_argument('--first', type=int, required=True); waits.add_argument('--stage', required=True, choices=('review', 'merge'))
    mark = commands.add_parser('merge-wait'); mark.add_argument('issue'); mark.add_argument('--clear', action='store_true')
    mark.add_argument('--branch'); mark.add_argument('--head'); mark.add_argument('--outcome', choices=('not-yet', 'blocked'))
    for action in ('stop', 'check', 'reuse'):
        command = commands.add_parser(action)
        command.add_argument('issue'); command.add_argument('--branch', required=True)
        command.add_argument('--state', required=True)
        if action in ('stop', 'reuse'):
            command.add_argument('--head', required=True); command.add_argument('--base', required=True)
            command.add_argument('--base-ref', required=action == 'reuse', default='main')
            command.add_argument('--pr', type=int, required=True)
        if action == 'stop':
            command.add_argument('--reviewed-head', required=True)
            # The review's verdict; only an APPROVE can be reused (see reuse()).
            command.add_argument('--verdict', choices=('APPROVE',))
            # Written by the review stage when the inline merge's gate was not yet decided.
            command.add_argument('--merge-gate-wait', action='store_true')
            # v3.2, with --merge-gate-wait: the build check passed for these inputs (and its
            # command), and how many times in a row the gate was not yet decided.
            command.add_argument('--build-check', choices=('passed',))
            command.add_argument('--build-command'); command.add_argument('--build-key')
            command.add_argument('--gate-waits', type=int)
    args = parser.parse_args()
    if getattr(args, 'issue', None) and not re.fullmatch(r'[A-Z][A-Z0-9]*-[0-9]+', args.issue):
        parser.error('issue must be an identifier such as TEAM-123')
    try:
        repo, root = directory(Path(args.repo).resolve())
        if args.action == 'status':
            result = {'review_stops': read(root / 'review-stops.json')}
        elif args.action == 'reserve-spec-number':
            result = reserve_spec_number(repo, root, args.issue, args.specs_dir, args.base)
        elif args.action == 'checkpoint':
            result = checkpoint(repo, root, args.issue)
        elif args.action == 'workspace':
            result = {'workspace': str(workspace(repo, root, args.issue, args.stage))}
        elif args.action == 'resume':
            result = {'issue': args.issue, 'resumed': resume(root, args.issue)}
        elif args.action == 'gate-waits':
            if args.cap < 0 or args.first < 1:
                raise ValueError('--cap must be a whole number of seconds from 0, --first from 1')
            result = gate_waits(repo, root, args.stage, args.first, args.cap)
        elif args.action == 'merge-wait':
            if not args.clear and (not args.branch or args.branch.startswith('-') or not args.outcome
                                   or not re.fullmatch(r'[0-9a-f]{40,64}', args.head or '')):
                raise ValueError('merge-wait needs --branch, a commit SHA as --head and --outcome, or --clear')
            result = merge_wait(root, args.issue, args.branch, args.head, args.outcome, args.clear)
        elif args.action == 'reuse':
            # reuse() reads the ticket detail itself, under the lock, so an unreadable
            # detail still removes the recorded approval.
            result = reuse(root, args.issue, args.branch, args.state, args.head, args.base,
                           args.base_ref, args.pr, sys.stdin.read())
        else:
            detail = json.load(sys.stdin)
            ticket_hash = fingerprint(detail, args.issue)
            if args.action == 'check':
                result = check(repo, root, args.issue, args.branch, args.state, detail)
            else:
                if any(not re.fullmatch(r'[0-9a-f]{40,64}', value) for value in (args.head, args.base, args.reviewed_head)) or args.pr <= 0:
                    raise ValueError('A review stop requires a commit SHA and PR number')
                if (args.base_ref.startswith('-') or subprocess.run(['git', 'check-ref-format', 'refs/heads/' + args.base_ref], env=process_env(['git', 'check-ref-format']),
                        cwd=repo, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode):
                    raise ValueError('A review stop requires a valid base branch')
                record = dict(workspace=str(repo), branch=args.branch, state=args.state, head=args.head, base=args.base, base_ref=args.base_ref, reviewed_head=args.reviewed_head, pr=args.pr,
                              ticket_hash=ticket_hash, stopped_at=time.time(), revision=uuid.uuid4().hex)
                if args.verdict:
                    record['verdict'] = args.verdict
                if args.merge_gate_wait:
                    record['merge_gate_wait'] = True
                    record['gate_waits'] = args.gate_waits if args.gate_waits and args.gate_waits >= 1 else 1
                    if args.build_check == 'passed' and args.build_command and re.fullmatch(r'[0-9a-f]{64}', args.build_key or ''):
                        record['build_check'] = 'passed'
                        record['build_command'] = args.build_command
                        record['build_key'] = args.build_key
                with locked(root) as path:
                    stops = read(path); stops[args.issue] = record; save(path, stops)
                wait_evidence(root, 'open_wait', args.issue, 'merge-gate' if args.merge_gate_wait else 'approval',
                              'dependency' if args.merge_gate_wait else 'decision',
                              'Merge gate is not yet decided' if args.merge_gate_wait else 'Approved work awaits merge authorization',
                              'ci-or-gate-owner' if args.merge_gate_wait else 'maintainer', 'code_review',
                              2 if args.merge_gate_wait else 20, os.environ.get('BUREAU_RUN_ID'),
                              generation_id=record['revision'], pr=args.pr)
                result = {'stopped': True, 'issue': args.issue, 'head': args.head, 'pr': args.pr}
        print(json.dumps(result))
        return 0
    except TimeoutError as exc:
        print('bureau supervision: ' + str(exc), file=sys.stderr)
        return 124
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as exc:
        print('bureau supervision: ' + str(exc), file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
