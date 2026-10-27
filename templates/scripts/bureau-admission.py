#!/usr/bin/env python3
"""Atomic, opt-in file scope admission in the repository's common Git directory."""
import argparse
from contextlib import contextmanager
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys

sys.dont_write_bytecode = True


def module(name, file):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(file))
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


r = module('admission_runtime', 'bureau-runtime.py')
p = module('admission_paths', 'bureau-paths.py')


class Stop(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


class SnapshotMoved(Stop):
    """A complete, usable snapshot changed while admission was checking it."""


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def git(repo, *args):
    command = ['git', '-C', str(repo), *args]
    return subprocess.check_output(command, env=r.process_env(command), stderr=subprocess.DEVNULL)


def gh(repo, endpoint):
    command = ['gh', 'api', '--paginate', '--slurp', endpoint]
    try:
        pages = json.loads(subprocess.check_output(command, cwd=repo, env=r.process_env(command), text=True))
        if not isinstance(pages, list) or not pages:
            raise ValueError('missing pages')
        return pages
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        raise Stop(18, 'GitHub snapshot unavailable: ' + endpoint) from exc


def linear(repo, query, variables):
    try:
        result = json.loads(r.shell(repo, 'linear_raw', json.dumps(dict(query=query, variables=variables))))
        if not isinstance(result, dict) or result.get('errors') or not isinstance(result.get('data'), dict):
            raise ValueError('unusable Linear response')
        return result['data']
    except subprocess.CalledProcessError as exc:
        raise Stop(exc.returncode if exc.returncode in (10, 27, 130) else 27, 'Linear snapshot unavailable') from exc
    except (OSError, ValueError) as exc:
        raise Stop(27, 'Linear snapshot incomplete') from exc


def connection(repo, query, variables, select):
    result, cursors = [], set()
    variables = dict(variables, after=None)
    while True:
        try:
            value = select(linear(repo, query, variables))
            nodes, info = value['nodes'], value['pageInfo']
            if not isinstance(nodes, list) or type(info['hasNextPage']) is not bool:
                raise ValueError('invalid connection')
            result.extend(nodes)
            if not info['hasNextPage']:
                return result
            cursor = info['endCursor']
            if not isinstance(cursor, str) or not cursor or cursor in cursors:
                raise ValueError('invalid pagination cursor')
            cursors.add(cursor)
            variables['after'] = cursor
        except (KeyError, TypeError, ValueError) as exc:
            raise Stop(27, 'Linear snapshot pagination incomplete') from exc


ISSUES = '''query($team:String!, $after:String) {
  issues(filter:{team:{key:{eq:$team}}}, first:100, after:$after, orderBy:createdAt) {
    nodes { id identifier title description project { id } state { id name type } labels { nodes { name } } }
    pageInfo { hasNextPage endCursor }
  }
}'''
COMMENTS = '''query($id:String!, $after:String) {
  issue(id:$id) { comments(first:100, after:$after) {
    nodes { body createdAt } pageInfo { hasNextPage endCursor }
  } }
}'''
LABELS = '''query($id:String!, $after:String) {
  issue(id:$id) { labels(first:100, after:$after) {
    nodes { name } pageInfo { hasNextPage endCursor }
  } }
}'''


def issues(repo, config):
    projects = config['linear'].get('projects')
    if projects is None: projects = []
    if not isinstance(projects, list) or any(not isinstance(v, str) or not v for v in projects):
        raise Stop(24, 'linear.projects must be an array of project IDs')
    values = connection(repo, ISSUES, dict(team=config['linear']['teams'][0]['key']), lambda v: v['issues'])
    result = {}
    for v in values:
        try:
            name = v['identifier']
            if (not re.fullmatch(r'[A-Z][A-Z0-9]*-\d+', name) or not v['id']
                    or not isinstance(v['state']['id'], str) or not v['state']['id']
                    or not isinstance(v['state']['type'], str) or name in result
                    or not isinstance(v['labels']['nodes'], list)):
                raise ValueError('invalid issue')
            result[name] = v
            if projects and ('project' not in v or (v['project'] is not None and
                    (not isinstance(v['project'], dict) or not isinstance(v['project'].get('id'), str) or not v['project']['id']))):
                raise ValueError('project identity missing')
        except (KeyError, TypeError, ValueError) as exc:
            raise Stop(27, 'Linear issue snapshot is incomplete or duplicated') from exc
    return result


def branches(repo, snapshot):
    result = {}
    for name, item in snapshot.items():
        comments = connection(repo, COMMENTS, dict(id=item['id']), lambda v: v['issue']['comments'])
        if any(not isinstance(c, dict) or not isinstance(c.get('body'), str) or not isinstance(c.get('createdAt'), str) for c in comments):
            raise Stop(27, 'Linear comments snapshot incomplete for ' + name)
        result[name] = r.canonical_branch(sorted(comments, key=lambda v: v['createdAt'], reverse=True))
    return result


def pull_identity(pr):
    try:
        value = (pr['number'], pr['head']['ref'], pr['head']['sha'], pr['base']['sha'])
        if (type(value[0]) is not int or not isinstance(value[1], str) or not value[1]
                or not all(re.fullmatch(r'[0-9a-f]{40}', sha) for sha in value[2:])):
            raise ValueError('invalid identity')
        return value
    except (KeyError, TypeError, ValueError) as exc:
        raise Stop(18, 'GitHub PR identity incomplete') from exc


def pulls(repo):
    pages = gh(repo, 'repos/{owner}/{repo}/pulls?state=open&per_page=100')
    if any(not isinstance(page, list) for page in pages):
        raise Stop(18, 'GitHub PR pagination incomplete')
    result = [pr for page in pages for pr in page]
    ids = [pull_identity(pr)[0] for pr in result]
    if len(set(ids)) != len(ids):
        raise Stop(18, 'GitHub PR pagination duplicated')
    return result


def ensure_commit(repo, sha):
    try:
        git(repo, 'cat-file', '-e', sha + '^{commit}')
    except subprocess.CalledProcessError:
        r.shell(repo, 'git', 'fetch', '--no-tags', 'origin', sha)
        git(repo, 'cat-file', '-e', sha + '^{commit}')


def changed_paths(repo, base, head):
    """Read both sides of a rename; -z preserves whitespace and odd filenames."""
    raw = git(repo, 'diff', '--no-ext-diff', '--no-textconv', '--name-status', '-z', '--find-renames', base, head)
    fields = raw.decode('utf-8').rstrip('\0').split('\0') if raw else []
    paths, i = set(), 0
    while i < len(fields):
        status = fields[i]; i += 1
        count = 2 if status.startswith(('R', 'C')) else 1
        if not re.fullmatch(r'[ACDMRTUXB][0-9]*', status) or i + count > len(fields):
            raise ValueError('invalid Git name-status output')
        for name in fields[i:i + count]:
            paths.add(p.normalize(name))
        i += count
    return sorted(paths)


def pr_scopes(repo, listed, canonical, inventories):
    known = {b for b in canonical.values() if b} | {v['branch'] for v in inventories.values() if v.get('branch')}
    result = []
    for pr in listed:
        number, branch, head, base = pull_identity(pr)
        body = pr.get('body') or ''
        if not isinstance(body, str):
            raise Stop(18, 'GitHub PR provenance incomplete: #' + str(number))
        legacy = ('Generated with [Claude Code]' in body and
                  ('Implemented from tasks.md spec.' in body or 'Draft PR — see Linear issue for status.' in body))
        # Canonical branch markers identify parked tickets too. Also retain old
        # numbered spec branches and explicitly Bureau-marked orphan PRs.
        if branch not in known and not re.match(r'^(?:[^/]+/)*\d+-', branch) and 'bureau' not in body.lower() and not legacy:
            continue
        try:
            for sha in (base, head): ensure_commit(repo, sha)
            merge_base = git(repo, 'merge-base', base, head).decode().strip()
            paths = changed_paths(repo, merge_base, head)
            fresh = gh(repo, 'repos/{owner}/{repo}/pulls/' + str(number))
            if len(fresh) != 1 or pull_identity(fresh[0]) != pull_identity(pr) or fresh[0].get('state') != 'open':
                raise Stop(18, 'PR changed while pinning diff: #' + str(number))
        except (OSError, ValueError, UnicodeError, subprocess.CalledProcessError) as exc:
            raise Stop(18, 'PR diff unavailable: #' + str(number)) from exc
        head_repo = pr['head'].get('repo')
        base_repo = pr['base'].get('repo')
        head_repo = head_repo.get('id') if isinstance(head_repo, dict) else None
        base_repo = base_repo.get('id') if isinstance(base_repo, dict) else None
        same_repo = type(head_repo) is int and type(base_repo) is int and head_repo == base_repo
        result.append(dict(holder='PR #' + str(number), branch=branch, paths=paths, base=base, head=head, same_repo=same_repo))
    return result


class Admission:
    def __init__(self, repo, config):
        self.repo, self.config = repo, config
        self.runtime = r.Store(repo)
        self.root = self.runtime.root / 'admission'
        self.path = self.root / 'v1.json'

    @contextmanager
    def guard(self):
        self.root.mkdir(parents=True, exist_ok=True)
        with (self.root / 'guard').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            value = r.read(self.path, dict(version=1, inventories={}, reservations={}, migration={}, ready={}, waiters={}))
            if value.get('version') != 1 or any(not isinstance(value.get(k), dict) for k in ('inventories', 'reservations', 'migration', 'ready', 'waiters')):
                raise Stop(24, 'Invalid authoritative admission store; reconcile before retry')
            yield value

    def save(self, state):
        r.save(self.path, state)
        # Authorization must survive a process restart and ordered disk writes.
        with self.path.open('rb') as saved:
            os.fsync(saved.fileno())
        directory = os.open(self.root, os.O_RDONLY)
        try: os.fsync(directory)
        finally: os.close(directory)

    def scope(self, branch, local=False, tasks=None):
        if not branch:
            return dict(branch=None, tasks=None, paths=None, unknown=['canonical branch missing'])
        try:
            git(self.repo, 'check-ref-format', '--branch', branch)
            if local:
                if git(self.repo, 'branch', '--show-current').decode().strip() != branch:
                    raise Stop(21, 'Checkout does not match canonical branch ' + branch)
                head = git(self.repo, 'rev-parse', 'HEAD').decode().strip()
            else:
                ref = 'refs/bureau/admission/' + hashlib.sha256(branch.encode()).hexdigest()
                try:
                    r.shell(self.repo, 'git', 'fetch', '--quiet', '--no-tags', 'origin', '+refs/heads/' + branch + ':' + ref)
                except subprocess.CalledProcessError as exc:
                    raise Stop(18, 'Canonical branch snapshot unavailable: ' + branch) from exc
                head = git(self.repo, 'rev-parse', ref).decode().strip()
            if tasks is None:
                tree = git(self.repo, 'ls-tree', '-r', '--name-only', '-z', head).decode().rstrip('\0').split('\0')
                spec_root = self.config.get('repo', {}).get('specs_dir', 'specs').rstrip('/')
                if Path(spec_root).is_absolute():
                    spec_root = str(Path(spec_root).relative_to(self.repo))
                candidates = [name for name in tree if name.startswith(spec_root + '/') and name.endswith('/tasks.md')]
                short = branch.split('/')[-1]
                exact = [name for name in candidates if Path(name).parent.name == short]
                matched = exact or [name for name in candidates if short.startswith(Path(name).parent.name + '-')]
                tasks = matched[0] if len(matched) == 1 else None
            prefix = os.environ.get('BUREAU_PATH_PREFIX_STRIP', self.config.get('repo', {}).get('path_prefix_strip', ''))
            value = p.inventory(self.repo, tasks, prefix, head, git)
            value['branch'] = branch
            if local and tasks and (self.repo / tasks).read_bytes() != git(self.repo, 'show', head + ':' + tasks):
                value.update(paths=None, unknown=['uncommitted tasks inventory'])
            return value
        except Stop:
            raise
        except (OSError, ValueError, UnicodeError, subprocess.CalledProcessError):
            return dict(branch=branch, tasks=tasks, paths=None, unknown=['canonical branch/tasks inventory unavailable'])

    def refresh(self, state, issue, scope):
        before = state['inventories'].get(issue)
        if before and (before.get('tasks_digest'), before.get('branch'), before.get('paths')) != (scope.get('tasks_digest'), scope.get('branch'), scope.get('paths')):
            state['ready'].pop(issue, None)
        state['inventories'][issue] = scope

    def writer_proof(self, issue, scope, leases):
        """Only a proven spec-only, unowned branch may migrate into the queue."""
        if 'issue:' + issue in leases:
            return 'execution ownership exists'
        if not scope.get('tasks') or not scope.get('head'):
            return 'unknown writer ownership or inventory'
        try:
            # Any preserved workspace needs explicit recovery, even after its
            # process stopped. Never reset/detach a user checkout to elect.
            for note in (self.runtime.root / 'preserved').glob('*.json'):
                if r.read(note).get('issue') == issue:
                    return 'preserved work requires reconciliation'
            base = self.config.get('repo', {}).get('base_branch', 'main')
            try:
                r.shell(self.repo, 'git', 'fetch', '--quiet', '--no-tags', 'origin', '+refs/heads/' + base + ':refs/bureau/admission/base')
            except subprocess.CalledProcessError as exc:
                raise Stop(18, 'Migration base snapshot unavailable: ' + base) from exc
            ancestor = git(self.repo, 'merge-base', 'refs/bureau/admission/base', scope['head']).decode().strip()
            changed = changed_paths(self.repo, ancestor, scope['head'])
            folder = str(Path(scope['tasks']).parent) + '/'
            if any(not name.startswith(folder) and name != '.specify/feature.json' for name in changed):
                return 'implementation changes already exist'
            records = git(self.repo, 'worktree', 'list', '--porcelain', '-z').decode().split('\0\0')
            for record in records:
                fields = record.split('\0')
                if 'branch refs/heads/' + scope['branch'] in fields:
                    workspace = Path(fields[0].removeprefix('worktree '))
                    if git(workspace, 'status', '--porcelain', '--untracked-files=all').strip():
                        return 'attached checkout contains unfinished work'
            return None
        except (OSError, ValueError, subprocess.CalledProcessError):
            return 'writer ownership could not be established'

    def admit(self, issue, branch, local=False, continuation=False):
        def checkpoint(snapshot=None):
            try:
                r.ticket_checkpoint(self.repo, issue, snapshot)
            except r.AdmissionStop as exc:
                raise Stop(exc.code, 'Candidate withdrawn: ' + issue) from exc

        # Withdrawal wins over admission, branch errors and overlap waits. Use
        # the same run latch as the eventual worker, before ownership/reset.
        checkpoint()
        with self.guard() as state:
            snapshot = issues(self.repo, self.config)
            if issue not in snapshot:
                raise Stop(27, 'Candidate missing from complete Linear snapshot')
            current = snapshot[issue]
            checkpoint(current)
            canonical = branches(self.repo, snapshot)
            if not branch or canonical[issue] != branch:
                raise Stop(12, 'Candidate canonical branch missing or changed')
            label_rows = connection(self.repo, LABELS, dict(id=current['id']), lambda v: v['issue']['labels'])
            if any(not isinstance(v, dict) or not isinstance(v.get('name'), str) for v in label_rows):
                raise Stop(27, 'Candidate labels snapshot incomplete')
            labels = {v['name'] for v in label_rows}
            human = self.config['linear'].get('labels', {}).get('needs_human', {}).get('name', 'needs-human')
            if labels & {human, 'needs-human', 'blocked', 'wip'}:
                raise Stop(25, 'Candidate held by human: ' + issue)
            with self.runtime.guard():
                leases = r.read(self.runtime.leases, {})
            lease = leases.get('issue:' + issue)
            if lease and lease.get('run_id') != os.environ.get('BUREAU_RUN_ID'):
                raise Stop(21, 'Candidate execution ownership held by another run: ' + issue)
            states = self.config['linear']['teams'][0]['states']
            holding = {states[k] for k in ('build', 'build_review', 'merge', 'qa', 'copy') if states.get(k)}
            candidate = self.scope(branch, local)
            self.refresh(state, issue, candidate)
            own = state['reservations'].get(issue)
            rework = current['state']['id'] in {states.get(k) for k in ('build', 'build_review', 'qa', 'merge')}
            same_scope = own and (own.get('paths') is None or (candidate['paths'] is not None and set(candidate['paths']) <= set(own['paths'])))
            if continuation and rework and same_scope and own.get('phase') == 'admitted':
                # A rework route retains an existing admission. The next writer
                # still performs the full PR/holder check before it can start.
                checkpoint()
                self.save(state)
                print('File overlap gate: retained rework reservation ' + issue)
                return
            holders = []
            projects = self.config['linear'].get('projects') or []
            classes = {}
            for name, item in snapshot.items():
                reservation = state['reservations'].get(name)
                project = item.get('project')
                project = project.get('id') if isinstance(project, dict) else None
                if projects and project not in projects and name != issue and not reservation and 'issue:' + name not in leases:
                    continue
                state_id = item['state']['id']
                if state_id not in classes:
                    classes[state_id] = r.shell(self.repo, 'bureau_ticket_state_class', state_id)
                withdrawn = classes[state_id] != 'active'
                if withdrawn and 'issue:' + name not in leases:
                    state['reservations'].pop(name, None)
                    state['migration'].pop(name, None)
                    state['waiters'].pop(name, None)
                    # A configured holding state can itself be inactive. Do
                    # not immediately re-elect a reservation just released.
                    continue
                withdrawn_writer = withdrawn and 'issue:' + name in leases
                if state_id not in holding and not reservation and not withdrawn_writer:
                    continue
                scope = candidate if name == issue else self.scope(canonical[name])
                self.refresh(state, name, scope)
                if not reservation:
                    election_leases = {k: v for k, v in leases.items() if k != 'issue:' + issue
                                       and not (k == 'issue:' + name and v.get('run_id') == state['waiters'].get(name))}
                    proof = self.writer_proof(name, scope, election_leases)
                    # Review/Merge/QA have already started. Copy and Build can
                    # be legacy queued work, but only the locked proof elects.
                    downstream = item['state']['id'] in {states.get(k) for k in ('build_review', 'merge', 'qa')}
                    if proof or downstream:
                        reservation = dict(scope, phase='legacy-holder', reason=proof or 'downstream work')
                        state['reservations'][name] = reservation
                        state['migration'][name] = 'holder'
                    else:
                        state['migration'][name] = 'queued'
                if reservation:
                    # Inventories may describe a pending expansion. Only the
                    # granted reservation holds other tickets; publishing that
                    # expansion before admission can make two grantees wait on
                    # each other. Keep the grant unchanged until the final save.
                    if name != issue:
                        holders.append(dict(holder=name, paths=reservation['paths'], unknown=reservation['unknown']))
            for name in state['reservations'].keys() - snapshot.keys():
                if name != issue:
                    holders.append(dict(holder=name, paths=None, unknown=['reserved ticket missing from service snapshot']))
            listed = pulls(self.repo)
            for pr in pr_scopes(self.repo, listed, canonical, state['inventories']):
                if pr['branch'] != branch or not pr['same_repo']:
                    holders.append(pr)
            effective = dict(candidate)
            if issue in state['reservations']:
                # Never shrink a grant. Its union with the requested inventory
                # is pending until all holder/PR checks pass under this lock.
                granted = state['reservations'][issue]['paths']
                requested = candidate['paths']
                effective['paths'] = None if granted is None or requested is None else sorted(set(granted + requested))
            # Rework retains its reservation but still rechecks external PRs
            # and writers. An earlier admission cannot waive a new conflict.
            conflicts = [dict(v, overlap=p.intersection(effective['paths'], v['paths'])) for v in holders
                         if p.intersection(effective['paths'], v['paths'])]
            latest = issues(self.repo, self.config)
            if issue in latest:
                checkpoint(latest[issue])
            if latest != snapshot:
                raise SnapshotMoved(27, 'Linear snapshot changed during admission; retry')
            if sorted(map(pull_identity, pulls(self.repo))) != sorted(map(pull_identity, listed)):
                raise SnapshotMoved(18, 'Open PR snapshot changed during admission; retry')
            checkpoint()
            if conflicts:
                if state['migration'].get(issue) == 'queued' and issue not in state['reservations'] and os.environ.get('BUREAU_RUN_ID'):
                    # A newly launched shepherd may own the ticket while only
                    # waiting. Its known non-writing run is not a legacy writer.
                    state['waiters'][issue] = os.environ['BUREAU_RUN_ID']
                self.save(state)
                lines = ['File overlap gate: ' + issue + ' waits']
                for conflict in conflicts:
                    lines.append('  ' + conflict['holder'] + ': ' + ', '.join(conflict['overlap'])
                                 + ('; ' + '; '.join(candidate['unknown'] + conflict.get('unknown', [])) if 'unknown scope (repository-wide)' in conflict['overlap'] else ''))
                reason = '\n'.join(lines)
                r.e.observe(r.e.Store(self.runtime.root).open_wait, issue, 'file-overlap', 'resource', reason,
                            'repository-maintainer', 'implement', 29, os.environ.get('BUREAU_RUN_ID'))
                raise Stop(29, reason)
            state['reservations'][issue] = dict(effective, phase='admitted')
            state['waiters'].pop(issue, None)
            self.save(state)  # Reservation is durable BEFORE the Build move or writer.
            r.e.observe(r.e.Store(self.runtime.root).resolve_waits, issue, keys=['file-overlap'], basis='observed-resume')
            print('File overlap gate: admitted ' + issue)

    def ready(self, issue, branch, stage, write=False):
        with self.guard() as state:
            scope = self.scope(branch, local=True)
            self.refresh(state, issue, scope)
            snapshot = r.issue_snapshot(self.repo, issue)
            value = dict(branch=branch, stage=stage, head=scope.get('head'),
                         tasks_digest=scope.get('tasks_digest'), config=digest(self.config), input=digest(snapshot))
            if write:
                state['ready'][issue] = value
            valid = state['ready'].get(issue) == value
            self.save(state)
            return 0 if valid else 2


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo', default='.')
    sub = parser.add_subparsers(dest='action', required=True)
    for action in ('record', 'admit', 'ready', 'ready-check'):
        part = sub.add_parser(action)
        part.add_argument('issue'); part.add_argument('branch')
        if action == 'record': part.add_argument('tasks')
        if action == 'admit':
            part.add_argument('--local', action='store_true')
            part.add_argument('--continuation', action='store_true')
        if action in ('ready', 'ready-check'): part.add_argument('stage')
    args = parser.parse_args()
    try:
        repo = r.root_for(Path(args.repo).resolve())
        config = r.read(r.config_for(repo))
        enabled = config.get('agents', {}).get('file_overlap_gate', False)
        if type(enabled) is not bool:
            raise Stop(24, 'agents.file_overlap_gate must be a boolean')
        if not enabled:
            return 2 if args.action == 'ready-check' else 0
        if not re.fullmatch(r'[A-Z][A-Z0-9]*-\d+', args.issue):
            raise Stop(24, 'Invalid admission issue identifier')
        admission = Admission(repo, config)
        if args.action == 'record':
            tasks = str(Path(args.tasks).resolve().relative_to(repo))
            with admission.guard() as state:
                admission.refresh(state, args.issue, admission.scope(args.branch, True, tasks))
                admission.save(state)
        elif args.action == 'admit':
            # Another lane may finish while this one is collecting snapshots.
            # Retry only this typed race, never a transport/shape/ownership
            # failure. Every attempt reruns the same locked gate and checkpoint.
            tries = 3 if os.environ.get('BUREAU_DISPATCH_LANE') == '1' else 1
            for attempt in range(tries):
                try:
                    admission.admit(args.issue, args.branch, args.local, args.continuation)
                    break
                except SnapshotMoved:
                    if attempt + 1 == tries: raise
        else:
            return admission.ready(args.issue, args.branch, args.stage, args.action == 'ready')
        return 0
    except Stop as exc:
        print(str(exc), file=sys.stderr)
        return exc.code
    except subprocess.CalledProcessError as exc:
        print('Admission helper failed (exit ' + str(exc.returncode) + ')', file=sys.stderr)
        return exc.returncode if exc.returncode in (10, 12, 18, 21, 25, 26, 27, 130) else 24
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        print('Admission store/config unavailable: ' + str(exc), file=sys.stderr)
        return 24


if __name__ == '__main__':
    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, lambda *_: sys.exit(130))
    sys.exit(main())
