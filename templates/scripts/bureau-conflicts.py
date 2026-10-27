#!/usr/bin/env python3
"""Owned merge transaction: legacy rules, checked insertions and regeneration.

Provider edits are untrusted; approval and publication stay with the caller. The trusted shell
checkpoint latches ticket withdrawal; rollback precedes the parent's stop/exit.
Other refusals retain the existing merge hook's exit-17 routing.
"""
import argparse
import difflib
import fnmatch
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
import uuid
from contextlib import contextmanager

sys.dont_write_bytecode = True
_spec = importlib.util.spec_from_file_location('bureau_provider', Path(__file__).with_name('bureau-provider.py'))
provider = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(provider)
MARKERS = re.compile(rb'^(?:<{7,}(?: .*)?|={7,}|>{7,}(?: .*)?|\|{7,}(?: .*)?)\r?$', re.M)
SECTION_HEADING = re.compile(rb'^[ \t]*(?:#{1,6}(?:[ \t\r\n]|$)|(?:=+|-+)[ \t\r\n]*$)')


class Declined(Exception):
    pass


class ProviderStop(Declined):
    def __init__(self, code):
        super().__init__('provider or ticket stopped the transaction')
        self.code = code


def resolver_configuration(config):
    agents = config.get('agents', {})
    if not isinstance(agents, dict): raise Declined('agents must be an object')
    item = agents.get('conflict_resolver', {})
    if not isinstance(item, dict) or type(item.get('enabled', False)) is not bool:
        raise Declined('agents.conflict_resolver requires a boolean enabled')
    limit = item.get('max_attempts', 1)
    if type(limit) is not int or not 1 <= limit <= 2:
        raise Declined('conflict_resolver.max_attempts must be 1 or 2')
    return item.get('enabled', False), limit


class AttemptBudget:
    """Authoritative reservations, separate from evidence retention and HEAD."""
    def __init__(self, root, issue, base):
        provider.e.ticket(issue)
        if provider.e.head(base) is None: raise Declined('invalid immutable conflict base')
        self.root = root / 'conflict-resolver'
        key = hashlib.sha256((issue + '\0' + base).encode()).hexdigest()
        self.path = self.root / (key + '.json')
        self.issue, self.base = issue, base

    @contextmanager
    def locked(self):
        provider.e.private_dir(self.root)
        with os.fdopen(os.open(self.root / 'guard', os.O_CREAT | os.O_RDWR, 0o600), 'a') as guard:
            deadline = time.monotonic() + 2
            while True:
                try:
                    fcntl.flock(guard, fcntl.LOCK_EX | fcntl.LOCK_NB); break
                except BlockingIOError:
                    if time.monotonic() >= deadline: raise Declined('conflict budget lock unavailable')
                    time.sleep(.01)
            if self.path.exists():
                data = json.loads(self.path.read_text())
                if (not isinstance(data, dict) or data.get('issue') != self.issue or data.get('base') != self.base
                        or data.get('schema_version') != 1 or not isinstance(data.get('attempts'), list)
                        or any(not isinstance(a, dict) or a.get('attempt') != n for n, a in enumerate(data['attempts'], 1))):
                    raise Declined('conflict budget is unreadable')
            else: data = dict(schema_version=1, issue=self.issue, base=self.base, attempts=[])
            yield data

    def reserve(self, limit, head, operation, identity, paths):
        with self.locked() as data:
            if len(data['attempts']) >= limit: raise Declined('conflict attempt allowance exhausted')
            record = dict(attempt=len(data['attempts']) + 1, base=self.base, head=head, operation=operation,
                          provider=identity['runner'], model=identity.get('model'), reserved_at=time.time(),
                          allowed_path_digest=hashlib.sha256(json.dumps(sorted(paths)).encode()).hexdigest(),
                          touched_path_digest=hashlib.sha256(b'[]').hexdigest(),
                          check_outcome='pending', outcome='reserved')
            data['attempts'].append(record)
            # Atomic replace + file/directory fsync must finish before spending.
            provider.e.atomic(self.path, data)
            return record

    def finish(self, record, **updates):
        with self.locked() as data:
            value = data['attempts'][record['attempt'] - 1]
            value.update(updates)
            provider.e.atomic(self.path, data)
            record.update(value)


def relative(path):
    if not isinstance(path, str) or not path or '\0' in path or '\\' in path:
        raise Declined('paths must be nonempty repository-relative POSIX paths')
    if any(p in ('', '.', '..', '.git') for p in path.split('/')) or path.startswith('/'):
        raise Declined('unsafe path in conflict configuration')
    return path


def matches(path, pattern):
    """Root-anchored globs: * is one component; ** spans zero or more."""
    parts, globs = path.split('/'), pattern.split('/')
    def match(i, j):
        if j == len(globs): return i == len(parts)
        if globs[j] == '**': return match(i, j + 1) or i < len(parts) and match(i + 1, j)
        return i < len(parts) and fnmatch.fnmatchcase(parts[i], globs[j]) and match(i + 1, j + 1)
    return match(0, 0)


def configuration(config):
    if not isinstance(config, dict): raise Declined('configuration must be an object')
    repo = config.get('repo', {})
    if not isinstance(repo, dict): raise Declined('repo must be an object')
    rules = repo.get('conflict_resolution', {})
    if not isinstance(rules, dict) or set(rules) - {'generated', 'append_only_paths'}:
        raise Declined('invalid repo.conflict_resolution')
    generated, append = rules.get('generated', []), rules.get('append_only_paths', [])
    if not isinstance(generated, list) or not isinstance(append, list):
        raise Declined('conflict strategies must be arrays')
    for path in append: relative(path)
    for rule in generated:
        if not isinstance(rule, dict) or set(rule) != {'globs', 'command'}:
            raise Declined('generated entries require globs and command')
        if not isinstance(rule['globs'], list) or not rule['globs']:
            raise Declined('generated globs must be a nonempty array')
        for pattern in rule['globs']: relative(pattern)
        command = rule['command']
        if not isinstance(command, list) or not command or any(not isinstance(a, str) or not a or '\0' in a for a in command):
            raise Declined('generator command must be a nonempty argument array')
    return generated, set(append), repo.get('test_command')


def entry(path):
    mode = path.lstat().st_mode
    if stat.S_ISLNK(mode): return ('link', stat.S_IMODE(mode), os.readlink(path))
    if stat.S_ISDIR(mode): return ('dir', stat.S_IMODE(mode), None)
    if stat.S_ISREG(mode):
        digest = hashlib.sha256()
        with path.open('rb') as source:
            for block in iter(lambda: source.read(1024 * 1024), b''): digest.update(block)
        return ('file', stat.S_IMODE(mode), digest.hexdigest())
    return ('special', stat.S_IMODE(mode), mode)


def inventory(root, backup=None):
    """Include ignored files; never traverse directory symlinks or root .git."""
    result = {}
    def visit(directory):
        for path in directory.iterdir():
            name = path.relative_to(root).as_posix()
            if name == '.git': continue
            value = entry(path)
            if backup is not None:
                if value[0] == 'file':
                    target = backup / name; target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(path, target)
                elif value[0] == 'special': raise Declined('special file in checkout')
            result[name] = value
            if value[0] == 'dir': visit(path)
    visit(root)
    return result


def changed(before, after):
    return {p for p in before.keys() | after.keys() if before.get(p) != after.get(p)}


def directory_mode(root, path):
    # Failed removal of a replaced directory must not send later restores
    # through a generator-created symlink into another checkout.
    if path != root: directory_mode(root, path.parent)
    mode = path.lstat().st_mode
    if not stat.S_ISDIR(mode): raise NotADirectoryError()
    return stat.S_IMODE(mode)


def restore(root, before, backup, root_mode):
    """Restore only changed entries, isolating failures until Git has reset.

    Grant directory access only when an operation needs it; restore_modes
    puts those temporary permissions back after the index/tree reset.
    """
    current, failures = {}, set()
    def grant(directory, permissions):
        mode = directory_mode(root, directory)
        if mode & permissions != permissions: directory.chmod(mode | permissions)

    def visit(directory, saved_mode):
        try:
            # Listing needs read permission; inspecting the listed children
            # also needs search permission, even when listing itself succeeds.
            if directory_mode(root, directory) != saved_mode: grant(directory, 0o500)
            try: paths = list(directory.iterdir())
            except PermissionError:
                grant(directory, 0o500)
                paths = list(directory.iterdir())
        except OSError:
            failures.add(directory)
            return
        for path in paths:
            name = path.relative_to(root).as_posix()
            if name == '.git': continue
            try: current[name] = entry(path)
            except OSError: current[name] = ('unreadable', None, None)
            if current[name][0] == 'dir':
                old = before.get(name)
                visit(path, old[1] if old and old[0] == 'dir' else None)

    def attempt(path, operation):
        try:
            directory_mode(root, path.parent)
            try: operation()
            except PermissionError:
                grant(path.parent, 0o700)
                operation()
        except OSError: failures.add(path)

    visit(root, root_mode)
    modified = changed(before, current)
    for name in sorted(modified & current.keys(), key=lambda p: (p.count('/'), p), reverse=True):
        path = root / name
        if current[name][0] == 'dir':
            if name not in before or before[name][0] != 'dir': attempt(path, path.rmdir)
        else: attempt(path, path.unlink)
    for name in sorted(modified & before.keys(), key=lambda p: (p.count('/'), p)):
        path = root / name; kind, mode, value = before[name]
        # A failed removal can leave a symlink to untouched ignored data.
        # Keep that path unrestored rather than writing through the old entry.
        if path in failures: continue
        if kind == 'dir':
            if current.get(name) is None or current[name][0] != 'dir': attempt(path, path.mkdir)
        elif kind == 'link': attempt(path, lambda: path.symlink_to(value))
        else: attempt(path, lambda: shutil.copy2(backup / name, path))
    return failures


def restore_modes(root, before, root_mode):
    # Restore restrictive parents last, after all data and Git operations.
    failures = set()
    directories = [(root / name, before[name][1]) for name in
                   sorted(before, key=lambda p: (p.count('/'), p), reverse=True) if before[name][0] == 'dir']
    for path, mode in directories + [(root, root_mode)]:
        try:
            if directory_mode(root, path) != mode: path.chmod(mode)
        except OSError: failures.add(path)
    return failures


def insertion_only(base, side):
    if b'\0' in base + side or not base or not base.endswith(b'\n') or not side.endswith(b'\n'):
        raise Declined('append-only input is binary, empty or lacks a final newline')
    old, new = base.splitlines(keepends=True), side.splitlines(keepends=True)
    inserted = set()
    for kind, i, j, a, b in difflib.SequenceMatcher(None, old, new, autojunk=False).get_opcodes():
        if kind not in ('equal', 'insert'): raise Declined('append-only input removes or rewrites base content')
        # Re-inserting an existing nonblank anchor can mask moves/reordering.
        if kind == 'insert' and any(line.strip() and line in old for line in new[a:b]):
            raise Declined('ambiguous repeated append-only anchor')
        if kind == 'insert': inserted.update(line for line in new[a:b] if line.strip())
    return inserted


class Transaction:
    def __init__(self, repo, config, base, config_path, issue='', operation='merge', result_file=None):
        self.repo, self.config, self.base = repo, config, base
        self.config_path = config_path
        self.issue = issue
        self.git_env = provider.process_env(['git'])
        self.git_env['GIT_LITERAL_PATHSPECS'] = '1'
        self.child = None
        self.operation, self.semantic = operation, False
        self.reservations = []
        self.result_file = result_file

    def git(self, *args, check=True, data=None):
        result = subprocess.run(['git', '-C', str(self.repo), *args], input=data, capture_output=True, env=self.git_env)
        if check and result.returncode: raise Declined('Git operation failed: ' + args[0])
        return result

    def identity(self):
        return (self.git('rev-parse', 'HEAD').stdout, self.git('rev-parse', '--verify', 'MERGE_HEAD', check=False).stdout,
                self.git('rev-parse', '--verify', 'REBASE_HEAD', check=False).stdout,
                self.git('symbolic-ref', '-q', 'HEAD', check=False).stdout,
                self.git('ls-files', '-v', '--stage', '-z').stdout,
                self.git('ls-files', '-f', '--stage', '-z').stdout,
                self.git('rev-parse', '--verify', self.branch + '^{commit}', check=False).stdout if self.branch else b'',
                self.operation_state())

    def operation_state(self):
        result = {}
        for name in ('config', 'config.worktree', 'ORIG_HEAD', 'MERGE_MSG', 'MERGE_MODE', 'rebase-merge', 'rebase-apply'):
            path = Path(os.fsdecode(self.git('rev-parse', '--git-path', name).stdout).strip())
            if not path.is_absolute(): path = self.repo / path
            if path.is_dir(): result[name] = inventory(path)
            elif path.exists(): result[name] = entry(path)
        return result

    def complete(self):
        if self.result_file:
            if self.result_file.resolve() == self.repo or self.repo in self.result_file.resolve().parents:
                raise Declined('transaction result must be outside checkout')
            self.result_file.write_text(json.dumps(dict(semantic=self.semantic)))

    def checkpoint(self):
        if not self.issue: return
        # Use this installed runtime's PR 9 helper, never branch code or a
        # second state classifier. Secrets are read only in the trusted shell.
        # Done exits that shell 0 without the acknowledgment: still a stop.
        # A claimed transaction needs a successful refresh before repair work;
        # the stage's additional-refresh failure policy cannot authorize it.
        command = ['/bin/bash', '--noprofile', '--norc', '-e', '-o', 'pipefail', '-c',
                   'source "$1"; '
                   'if [ -f "${BUREAU_ENV_FILE:-}" ]; then bureau_load_env "$BUREAU_ENV_FILE"; fi; '
                   'bureau_secret_copy --optional API_KEY LINEAR_API_KEY; '
                   'if [ -n "${BUREAU_TICKET_CONTROL_DIR:-}" ]; then '
                   'bureau_ticket_checkpoint "$2" strict; fi; printf authorized',
                   'bureau-conflicts-checkpoint', str(Path(__file__).with_name('bureau-config.sh')), self.issue]
        if self.run(command, trusted=True) != b'authorized':
            raise Declined('ticket withdrawn before resolution command')

    def run(self, command, *, trusted=False, provider_call=False):
        """Executor-owned command outside the provider sandbox, with its env policy."""
        env = (os.environ.copy() if trusted else
               provider.untrusted_env(os.environ, provider.untrusted_env_mode(self.config)))
        owned = provider.a.OwnedProcesses() if provider_call else None
        if owned: env[provider.a.OWNER_ENV] = owned.token
        self.child = subprocess.Popen(command, cwd=self.repo, env=env, stdin=subprocess.DEVNULL,
                                      stdout=subprocess.PIPE if trusted else None, start_new_session=True)
        try:
            if owned: owned.attach(self.child.pid)
            timeout = provider.configuration('conflict_resolver', self.config, os.environ)['timeout'] + 30 if provider_call else 1800
            started = time.monotonic()
            while True:
                if owned: owned.scan()
                remaining = timeout - (time.monotonic() - started)
                if remaining <= 0: raise subprocess.TimeoutExpired(command, timeout)
                try:
                    output, _ = self.child.communicate(timeout=min(.1, remaining) if owned else remaining)
                    break
                except subprocess.TimeoutExpired:
                    if not owned: raise
            if trusted and self.child.returncode in (10, 16, 21, 25, 26, 27, 130):
                raise ProviderStop(self.child.returncode)
            if self.child.returncode: raise Declined('configured command failed')
            return output
        except subprocess.TimeoutExpired:
            raise Declined('configured command exceeded its executor bound')
        finally:
            # Reap the leader and stop any remaining children in its owned group,
            # including a command that exits after leaving a background writer.
            try:
                if owned:
                    # Scan while ancestry still exists, then stop descendants
                    # too, including CLI helpers in other sessions.
                    owned.scan(); owned.signal(signal.SIGTERM)
                    try: self.child.communicate(timeout=7)
                    except subprocess.TimeoutExpired: pass
                    owned.scan(); owned.signal(signal.SIGKILL)
            finally:
                try: os.killpg(self.child.pid, signal.SIGKILL)
                except ProcessLookupError: pass
                self.child.wait()
                if owned: owned.drain()
                self.child = None

    def shell(self, body, *args):
        return ['/bin/bash', '--noprofile', '--norc', '-e', '-o', 'pipefail', '-c',
                'source "$1"; ' + body, 'bureau-conflicts',
                str(Path(__file__).with_name('bureau-config.sh')), *map(str, args)]

    def model_repair(self, paths, scratch):
        enabled, limit = resolver_configuration(self.config)
        if not enabled: raise Declined('unsupported source conflict; model resolution is disabled')
        check = self.config.get('repo', {}).get('test_command')
        if not isinstance(check, str) or not any(line.strip() and not line.lstrip().startswith('#') for line in check.splitlines()):
            raise Declined('model resolution requires repo.test_command')
        branch = self.branch[len('refs/heads/'):]
        if not self.branch.startswith('refs/heads/') or not branch: raise Declined('model resolution requires an attached branch')
        remote = self.git('rev-parse', '--verify', 'refs/remotes/origin/' + branch + '^{commit}').stdout.strip().decode()
        self.git('merge-base', '--is-ancestor', remote, self.head)
        self.run(self.shell('branch_is_bureau_only "$2" "$3" "$4"; '
                            'branch_is_bureau_only "$2" "$5" "$4"', branch, self.head, self.base, remote), trusted=True)
        # All paths were classified before any write. Binary, add/delete,
        # symlink and mode conflicts are deliberately outside model repair.
        for name in paths:
            stages = self.conflicts()[name]
            if (set(stages) != {1, 2, 3} or len({v[0] for v in stages.values()}) != 1
                    or stages[1][0] not in (b'100644', b'100755')
                    or any(b'\0' in self.git('cat-file', 'blob', oid.decode()).stdout for _, oid in stages.values())):
                raise Declined('model repair requires regular text three-way conflicts')
        markers = {name: self.conflict_markers(name) for name in paths}
        self.checkpoint()
        identity = provider.configuration('conflict_resolver', self.config, os.environ)
        common = Path(os.fsdecode(self.git('rev-parse', '--git-common-dir').stdout).strip())
        root = (self.repo / common).resolve() / 'bureau'
        budget = AttemptBudget(root, self.issue, self.base)
        record = budget.reserve(limit, self.git('rev-parse', 'HEAD').stdout.strip().decode(), self.operation, identity, paths)
        self.reservations.append((budget, record))
        before, git_identity = inventory(self.repo), self.identity()
        prompt = scratch / 'resolver-prompt'
        prompt.write_text('Resolve only these remaining source conflicts: ' + json.dumps(sorted(paths)) +
                          '\nYou write only those files. Helpers may report to you but must touch no file. '
                          'Preserve both intended changes. Do not change any other path, mode or symlink. '
                          'Do not run Git writes, stage files, continue/abort the operation, run validation, commit or publish. '
                          'The executor checks the files and index, regenerates outputs and runs the configured tests. '
                          'Report what you changed; a resolved claim is not proof.')
        try:
            acknowledgment = b'\nbureau-conflict-provider-complete\n'
            output = self.run(self.shell(
                'if [ -f "${BUREAU_ENV_FILE:-}" ]; then bureau_load_env "$BUREAU_ENV_FILE"; fi; '
                'bureau_secret_copy --optional API_KEY LINEAR_API_KEY; '
                'export BUREAU_CURRENT_ISSUE="$2" BUREAU_PROVIDER_LOG_DIR="$4"; rc=0; '
                'run_stage_for conflict_resolver "$(cat "$3")" || rc=$?; '
                '[ "$rc" = 0 ] || exit "$rc"; printf "\\nbureau-conflict-provider-complete\\n"',
                self.issue, prompt, root / 'conflict-provider-runs'), trusted=True, provider_call=True)
            if not output.endswith(acknowledgment): raise Declined('provider stopped before completion')
            self.checkpoint()
            after = inventory(self.repo)
            touched = changed(before, after)
            budget.finish(record, touched_path_digest=hashlib.sha256(json.dumps(sorted(touched)).encode()).hexdigest())
            if self.identity() != git_identity: raise Declined('provider changed Git state or index')
            if touched - set(paths): raise Declined('provider changed an out-of-scope path')
            for name in paths:
                if (after.get(name, (None,))[0] != 'file' or before[name][:2] != after[name][:2]
                        or b'\0' in (self.repo / name).read_bytes() or markers[name].search((self.repo / name).read_bytes())):
                    raise Declined('provider left invalid files or conflict markers')
                self.git('add', '--', name)
            self.semantic = True
            budget.finish(record, outcome='resolved')
        except BaseException:
            try:
                touched = changed(before, inventory(self.repo))
                budget.finish(record, outcome='failed',
                              touched_path_digest=hashlib.sha256(json.dumps(sorted(touched)).encode()).hexdigest())
            except (OSError, ValueError, Declined): pass
            raise

    def validate(self, check, scratch):
        before, identity = inventory(self.repo), self.identity()
        self.checkpoint()
        log = scratch / ('check-' + uuid.uuid4().hex + '.log')
        options = dict(timeout=1800, untrusted_env=provider.untrusted_env_mode(self.config))
        code, abort = provider.outside_gate(options, self.repo, check, log)
        tracked = {os.fsdecode(p) for p in self.git('ls-files', '-z').stdout.split(b'\0') if p}
        valid = (code == 0 and not abort and self.identity() == identity
                 and not changed(before, inventory(self.repo)) & tracked
                 and not self.git('diff', '--name-only').stdout
                 and not self.git('ls-files', '--others', '--exclude-standard', '-z').stdout)
        for budget, record in self.reservations:
            budget.finish(record, check_outcome='passed' if valid else 'failed', check_exit=code)
        self.checkpoint()
        if code == 130: raise ProviderStop(130)
        if not valid: raise Declined('external conflict validation failed or changed the checked tree')

    def evidence(self, outcome, code=0):
        for budget, record in self.reservations:
            budget.finish(record, outcome=outcome)
            # Evidence can be pruned or unavailable; it never replenishes the
            # authoritative fsynced reservation. Keep only hashes/typed fields.
            run = os.environ.get('BUREAU_RUN_ID') or uuid.uuid4().hex
            provider.e.observe(provider.e.Store(budget.root.parent).put_receipt,
                dict(run_id=run, provider_attempt_id=uuid.uuid4().hex,
                     stage_attempt_id=os.environ.get('BUREAU_STAGE_ATTEMPT_ID'), issue=self.issue,
                     stage='conflict_resolver', phase='validation', provider=record['provider'],
                     normalized_exit=code, start_head=self.head,
                     end_head=self.git('rev-parse', 'HEAD').stdout.strip().decode(),
                     conflict_resolution={k: record[k] for k in ('attempt', 'base', 'head', 'operation', 'provider',
                         'touched_path_digest', 'check_outcome')}, outcome='complete' if outcome == 'complete' else 'invalid-result'))

    def union(self, blobs, scratch):
        paths = []
        for stage in (2, 1, 3):
            path = scratch / str(stage); path.write_bytes(blobs[stage]); paths.append(str(path))
        result = self.git('merge-file', '-p', '--union', '--', *paths)
        if MARKERS.search(result.stdout): raise Declined('union retained conflict markers')
        return result.stdout

    def conflicts(self):
        result = {}
        for row in self.git('ls-files', '--unmerged', '-z').stdout.split(b'\0'):
            if not row: continue
            metadata, path = row.split(b'\t', 1); mode, oid, stage = metadata.split()
            name = os.fsdecode(path); relative(name)
            result.setdefault(name, {})[int(stage)] = (mode, oid)
        return result

    def resolve(self, generated, append, scratch):
        conflicts = self.conflicts()
        if not conflicts: raise Declined('merge failed without resolvable entries')
        resolved_paths = set(conflicts)
        pending, configured, sources = [], False, []
        for name, stages in conflicts.items():
            owners = [r for r in generated if any(matches(name, g) for g in r['globs'])]
            if len(owners) > 1 or owners and name in append: raise Declined('overlapping conflict strategies')
            legacy = name in ('.specify/feature.json', 'CLAUDE.md', '.gitignore', 'rust/Cargo.lock') or matches(name, 'logs/queue-*.log')
            if not owners and name not in append and not legacy:
                sources.append(name); continue
            if owners or name in append:
                configured = True
                if set(stages) != {1, 2, 3} or len({v[0] for v in stages.values()}) != 1 or stages[1][0] not in (b'100644', b'100755'):
                    raise Declined('configured conflict is not a regular three-way edit')
            pending.append((name, stages, bool(owners)))
        configured = configured or bool(sources)
        if configured:
            check = self.config.get('repo', {}).get('test_command')
            if not isinstance(check, str) or not any(line.strip() and not line.lstrip().startswith('#') for line in check.split('\n')):
                raise Declined('configured repair requires repo.test_command')
        # Verify ALL inputs before running any generator. Append-only sources
        # and legacy resolutions are staged before generated outputs are built.
        for name, stages, output in pending:
            if output:
                self.git('checkout', '--ours', '--', name)
            elif name in append or name in ('CLAUDE.md', '.gitignore'):
                if not {2, 3}.issubset(stages): raise Declined('union requires both sides')
                if any(v[0] not in (b'100644', b'100755') for v in stages.values()): raise Declined('union requires regular files')
                blobs = {s: self.git('cat-file', 'blob', oid.decode()).stdout for s, (_, oid) in stages.items()}
                blobs.setdefault(1, b'')  # Legacy CLAUDE.md/.gitignore add/add union.
                if name in append:
                    ours = insertion_only(blobs[1], blobs[2])
                    theirs = insertion_only(blobs[1], blobs[3])
                    # --union aligns identical inserted headings/context even
                    # when doing so moves entries into another section. Side
                    # subsequences alone cannot prove those associations.
                    if ours & theirs: raise Declined('ambiguous shared append-only insertion anchor')
                    # A heading from just one side can also capture entries
                    # inserted by the other under the original parent section.
                    # Decline new ATX/Setext headings rather than infer intent.
                    if any(SECTION_HEADING.match(line) for line in ours | theirs):
                        raise Declined('append-only insertion changes section headings')
                merged = self.union(blobs, scratch)
                if name in append:
                    # A real union must retain both entire side sequences.
                    for side in (blobs[2], blobs[3]):
                        lines = iter(merged.splitlines(keepends=True))
                        if not all(any(line == wanted for line in lines) for wanted in side.splitlines(keepends=True)):
                            raise Declined('union lost side content')
                (self.repo / name).write_bytes(merged)
            elif name == '.specify/feature.json': self.git('checkout', '--ours', '--', name)
            elif name == 'rust/Cargo.lock': self.git('checkout', '--theirs', '--', name)
            else: self.git('rm', '--', name); continue
            if not output: self.git('add', '--', name)
        if sources: self.model_repair(sources, scratch)
        if configured:
            resolved_paths.update(self.regenerate(generated, append))
        if self.conflicts(): raise Declined('unmerged entries remain')
        touched = self.git('diff', '--cached', '--name-only', '-z', 'HEAD').stdout.split(b'\0')
        resolved_paths.update(os.fsdecode(p) for p in touched if p)
        self.reject_markers(resolved_paths)
        return configured

    def regenerate(self, generated, append):
        resolved_paths = set()
        # The merge may change either manifest, and worker clean -fdx
        # removes ignored dependencies. Reuse the stages' trusted helper
        # against the merged files before ANY generator or check runs.
        identity = self.identity()
        unstaged = self.git('diff', '--name-only', '-z').stdout
        self.checkpoint()
        self.run(['/bin/bash', '--noprofile', '--norc', '-e', '-o', 'pipefail', '-c',
                  'BUREAU_CONFIG="$2"; BUREAU_ENV_FILE=/dev/null; source "$1"; restore_worktree_deps "$PWD"',
                  'bureau-conflicts', str(Path(__file__).with_name('bureau-config.sh')), str(self.config_path)])
        if self.identity() != identity or self.git('diff', '--name-only', '-z').stdout != unstaged:
            raise Declined('dependency preparation changed the checked tree or Git state')
        for rule in generated:
            before, identity = inventory(self.repo), self.identity()
            self.checkpoint()
            self.run(rule['command'])
            after = inventory(self.repo)
            if self.identity() != identity: raise Declined('generator changed Git state')
            for name in changed(before, after):
                # Creation of parent directories is permitted only for a
                # declared output actually created by this invocation.
                if name not in before and after[name][0] == 'dir' and any(p.startswith(name + '/') and any(matches(p, g) for g in rule['globs']) for p in changed(before, after)):
                    continue
                if not any(matches(name, g) for g in rule['globs']): raise Declined('generator changed an undeclared path')
                if (name in after and after[name][0] != 'file') or (name in before and before[name][0] == 'dir'):
                    raise Declined('generated outputs must be regular files')
            outputs = {p for p in before.keys() | after.keys() if any(matches(p, g) for g in rule['globs'])
                       and before.get(p, after.get(p))[0] != 'dir'}
            for name in sorted(outputs):
                if sum(any(matches(name, g) for g in r['globs']) for r in generated) != 1 or name in append:
                    raise Declined('overlapping output ownership')
                if name in after and after[name][0] != 'file': raise Declined('generated output is not regular')
                self.git('add', '-A', '--', name)
            resolved_paths.update(outputs)
        return resolved_paths

    def conflict_markers(self, name):
        value = self.git('check-attr', '-z', 'conflict-marker-size', '--', name).stdout.split(b'\0')[2]
        number = re.fullmatch(rb'\+?([0-9]+)', value)
        digits = number[1].lstrip(b'0') if number else b''
        # Invalid/nonpositive sizes use seven. Standard markers and sizes
        # >= seven are already covered by the default detector.
        if len(digits) != 1 or not b'1' <= digits <= b'6': return MARKERS
        exact = MARKERS.pattern.replace(b'{7,}', b'{' + digits + b'}')
        return re.compile(MARKERS.pattern + b'|' + exact, re.M)

    def reject_markers(self, paths):
        for name in paths:
            path = self.repo / name
            if path.is_file() and self.conflict_markers(name).search(path.read_bytes()):
                raise Declined('resolved tree contains conflict markers')

    def commit_merge(self):
        self.checkpoint()
        checked_tree = self.git('write-tree').stdout
        args = ['-c', 'core.editor=true', 'commit', '--no-edit']
        if self.semantic: args += ['--trailer', 'Bureau-Generated: true']
        self.git(*args)
        parents = self.git('rev-list', '--parents', '-1', 'HEAD').stdout.decode().split()[1:]
        if (self.git('rev-parse', 'HEAD^{tree}').stdout != checked_tree or parents != [self.head, self.base]
                or self.git('status', '--porcelain', '--untracked-files=all').stdout):
            raise Declined('merge commit differs from the checked tree')
        print('  Auto-resolved conflicts (checked deterministic or model resolution).')

    def merge(self):
        """Retain the deterministic transaction API for installed callers."""
        return self.execute()

    def execute(self):
        generated, append, check = configuration(self.config)
        enabled, _ = resolver_configuration(self.config)
        if self.git('status', '--porcelain', '--untracked-files=all').stdout:
            raise Declined('merge transaction requires a clean owned checkout')
        head = self.git('rev-parse', 'HEAD').stdout.strip().decode()
        self.head = head
        self.branch = self.git('symbolic-ref', '-q', 'HEAD', check=False).stdout.strip().decode()
        self.base = self.git('rev-parse', '--verify', self.base + '^{commit}').stdout.strip().decode()
        if (self.git('rev-parse', '--verify', 'MERGE_HEAD', check=False).returncode == 0
                or any((self.repo / os.fsdecode(self.git('rev-parse', '--git-path', name).stdout).strip()).exists()
                       for name in ('rebase-merge', 'rebase-apply'))):
            raise Declined('merge or rebase already in progress')
        with tempfile.TemporaryDirectory(prefix='bureau-conflicts-') as directory:
            scratch = Path(directory).resolve()
            if scratch == self.repo or self.repo in scratch.parents: raise Declined('transaction scratch must be outside checkout')
            backup = scratch / 'saved'; backup.mkdir()
            index = Path(os.fsdecode(self.git('rev-parse', '--git-path', 'index').stdout).strip())
            if not index.is_absolute(): index = self.repo / index
            saved_index = scratch / 'index'; shutil.copy2(index, saved_index)
            root_mode = stat.S_IMODE(self.repo.stat().st_mode)
            saved = inventory(self.repo, backup) if generated or append or enabled else None
            try:
                self.checkpoint()
                if self.operation == 'merge':
                    result = self.git('merge', '--no-ff', '--no-edit', self.base, check=False)
                    if result.returncode == 0:
                        self.complete(); return 0
                    configured = self.resolve(generated, append, scratch)
                    if configured: self.validate(check, scratch)
                    self.commit_merge()
                else:
                    result = self.git('rebase', self.base, check=False)
                    configured = False
                    while result.returncode:
                        configured = self.resolve(generated, append, scratch) or configured
                        self.checkpoint()
                        result = self.git('-c', 'core.editor=true', 'rebase', '--continue', check=False)
                    if configured:
                        self.regenerate(generated, append)
                        paths = self.git('diff', '--name-only', '-z', self.head).stdout.split(b'\0')
                        self.reject_markers(os.fsdecode(p) for p in paths if p)
                        self.validate(check, scratch)
                        tree = self.git('write-tree').stdout
                        if self.git('diff', '--cached', '--quiet', check=False).returncode:
                            self.checkpoint()
                            self.git('commit', '-m', 'Regenerate resolved outputs\n\nBureau-Generated: true')
                        if self.git('rev-parse', 'HEAD^{tree}').stdout != tree or self.git('status', '--porcelain').stdout:
                            raise Declined('rebase differs from the checked tree')
                self.evidence('complete')
                self.complete()
                return 0
            except BaseException as failure:
                restoration_errors = set()
                try:
                    self.git(self.operation, '--abort', check=False)
                    if saved is not None: restoration_errors.update(restore(self.repo, saved, backup, root_mode))
                finally:
                    # Restoration errors must never bypass the index restore
                    # and reset, even when merge --abort could not write files.
                    try:
                        if self.branch: self.git('symbolic-ref', 'HEAD', self.branch)
                        else: self.git('update-ref', '--no-deref', 'HEAD', head)
                    finally:
                        try:
                            shutil.copy2(saved_index, index)
                            self.git('reset', '--hard', head)
                        finally:
                            try:
                                shutil.copy2(saved_index, index)
                            finally:
                                if saved is not None: restoration_errors.update(restore_modes(self.repo, saved, root_mode))
                if saved is not None:
                    if restoration_errors: raise Declined('checkout restoration could not be completed')
                    if inventory(self.repo) != saved: raise Declined('checkout restoration could not be verified')
                shutil.copy2(saved_index, index)
                try: self.evidence('failed', failure.code if isinstance(failure, ProviderStop) else 17)
                except (OSError, ValueError, Declined): pass
                raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo', required=True, type=Path)
    parser.add_argument('--config', required=True, type=Path)
    parser.add_argument('--base', required=True)
    parser.add_argument('--issue', default='')
    parser.add_argument('--operation', choices=('merge', 'rebase'), default='merge')
    parser.add_argument('--result-file', type=Path)
    args = parser.parse_args()
    def stop(signum, frame): raise ProviderStop(130)
    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP): signal.signal(sig, stop)
    try:
        transaction = Transaction(args.repo.resolve(), json.loads(args.config.read_text()), args.base, args.config.resolve(), args.issue, args.operation, args.result_file)
        result = transaction.execute()
        return result
    except ProviderStop as exc:
        if exc.code in (10, 16, 27):
            print('  Conflict resolution declined: trusted checkpoint or provider precondition refused.', file=sys.stderr)
        return exc.code
    except (Declined, OSError, ValueError, provider.UntrustedEnvError) as exc:
        # Do not copy command text, paths or child stderr into control messages.
        print('  Conflict resolution declined: ' + str(exc) if isinstance(exc, Declined) else '  Conflict resolution unavailable.', file=sys.stderr)
        return 1


if __name__ == '__main__': sys.exit(main())
