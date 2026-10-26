#!/usr/bin/env python3
"""Owned merge transaction: legacy rules, checked insertions and regeneration.

No provider, Linear, approval or publication authority lives here. Return 1 to
the existing merge hook on refusal; its callers retain their exit-17 routing.
"""
import argparse
import difflib
import fnmatch
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

sys.dont_write_bytecode = True
_spec = importlib.util.spec_from_file_location('bureau_provider', Path(__file__).with_name('bureau-provider.py'))
provider = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(provider)
MARKERS = re.compile(rb'^(?:<{7,}(?: .*)?|={7,}|>{7,}(?: .*)?|\|{7,}(?: .*)?)\r?$', re.M)
SECTION_HEADING = re.compile(rb'^[ \t]*(?:#{1,6}(?:[ \t\r\n]|$)|(?:=+|-+)[ \t\r\n]*$)')


class Declined(Exception):
    pass


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
    def __init__(self, repo, config, base, config_path):
        self.repo, self.config, self.base = repo, config, base
        self.config_path = config_path
        self.git_env = provider.process_env(['git'])
        self.git_env['GIT_LITERAL_PATHSPECS'] = '1'
        self.child = None

    def git(self, *args, check=True, data=None):
        result = subprocess.run(['git', '-C', str(self.repo), *args], input=data, capture_output=True, env=self.git_env)
        if check and result.returncode: raise Declined('Git operation failed: ' + args[0])
        return result

    def identity(self):
        return (self.git('rev-parse', 'HEAD').stdout, self.git('rev-parse', '--verify', 'MERGE_HEAD').stdout,
                self.git('symbolic-ref', '-q', 'HEAD', check=False).stdout, self.git('ls-files', '--stage', '-z').stdout)

    def run(self, command):
        """Executor-owned command outside the provider sandbox, with its env policy."""
        env = provider.untrusted_env(os.environ, provider.untrusted_env_mode(self.config))
        self.child = subprocess.Popen(command, cwd=self.repo, env=env, stdin=subprocess.DEVNULL, start_new_session=True)
        try:
            code = self.child.wait(timeout=1800)
            if code: raise Declined('configured command failed')
        except subprocess.TimeoutExpired:
            raise Declined('configured command exceeded 1800 seconds')
        finally:
            # Reap the leader and stop any remaining children in its owned group,
            # including a command that exits after leaving a background writer.
            try: os.killpg(self.child.pid, signal.SIGKILL)
            except ProcessLookupError: pass
            self.child.wait(); self.child = None

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
        pending, configured = [], False
        for name, stages in conflicts.items():
            owners = [r for r in generated if any(matches(name, g) for g in r['globs'])]
            if len(owners) > 1 or owners and name in append: raise Declined('overlapping conflict strategies')
            legacy = name in ('.specify/feature.json', 'CLAUDE.md', '.gitignore', 'rust/Cargo.lock') or matches(name, 'logs/queue-*.log')
            if not owners and name not in append and not legacy: raise Declined('unsupported source conflict')
            if owners or name in append:
                configured = True
                if set(stages) != {1, 2, 3} or len({v[0] for v in stages.values()}) != 1 or stages[1][0] not in (b'100644', b'100755'):
                    raise Declined('configured conflict is not a regular three-way edit')
            pending.append((name, stages, bool(owners)))
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
        if configured:
            # The merge may change either manifest, and worker clean -fdx
            # removes ignored dependencies. Reuse the stages' trusted helper
            # against the merged files before ANY generator or check runs.
            identity = self.identity()
            unstaged = self.git('diff', '--name-only', '-z').stdout
            self.run(['/bin/bash', '--noprofile', '--norc', '-e', '-o', 'pipefail', '-c',
                      'BUREAU_CONFIG="$2"; BUREAU_ENV_FILE=/dev/null; source "$1"; restore_worktree_deps "$PWD"',
                      'bureau-conflicts', str(Path(__file__).with_name('bureau-config.sh')), str(self.config_path)])
            if self.identity() != identity or self.git('diff', '--name-only', '-z').stdout != unstaged:
                raise Declined('dependency preparation changed the checked tree or Git state')
            for rule in generated:
                before, identity = inventory(self.repo), self.identity()
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
        if self.conflicts(): raise Declined('unmerged entries remain')
        touched = self.git('diff', '--cached', '--name-only', '-z', 'HEAD').stdout.split(b'\0')
        resolved_paths.update(os.fsdecode(p) for p in touched if p)
        for name in resolved_paths:
            if (self.repo / name).is_file() and MARKERS.search((self.repo / name).read_bytes()):
                raise Declined('resolved tree contains conflict markers')
        if configured:
            before, identity = inventory(self.repo), self.identity()
            self.run(['/bin/bash', '--noprofile', '--norc', '-o', 'pipefail', '-c', check])
            tracked = {os.fsdecode(p) for p in self.git('ls-files', '-z').stdout.split(b'\0') if p}
            if self.identity() != identity or changed(before, inventory(self.repo)) & tracked or self.git('diff', '--name-only').stdout:
                raise Declined('validation changed the checked tree or Git state')
            # Ignored caches/logs from tests are permitted; tracked changes and
            # nonignored untracked output cannot enter the accepted transaction.
            if self.git('ls-files', '--others', '--exclude-standard', '-z').stdout:
                raise Declined('validation left untracked output')
        checked_tree = self.git('write-tree').stdout
        self.git('-c', 'core.editor=true', 'commit', '--no-edit')
        parents = self.git('rev-list', '--parents', '-1', 'HEAD').stdout.decode().split()[1:]
        if (self.git('rev-parse', 'HEAD^{tree}').stdout != checked_tree or parents != [self.head, self.base]
                or self.git('status', '--porcelain', '--untracked-files=all').stdout):
            raise Declined('merge commit differs from the checked tree')
        print('  Auto-resolved conflicts (checked generated/append-only or legacy rules).')

    def merge(self):
        generated, append, _ = configuration(self.config)
        if self.git('status', '--porcelain', '--untracked-files=all').stdout:
            raise Declined('merge transaction requires a clean owned checkout')
        head = self.git('rev-parse', 'HEAD').stdout.strip().decode()
        self.head = head
        self.branch = self.git('symbolic-ref', '-q', 'HEAD', check=False).stdout.strip().decode()
        self.base = self.git('rev-parse', '--verify', self.base + '^{commit}').stdout.strip().decode()
        if self.git('rev-parse', '--verify', 'MERGE_HEAD', check=False).returncode == 0:
            raise Declined('merge already in progress')
        with tempfile.TemporaryDirectory(prefix='bureau-conflicts-') as directory:
            scratch = Path(directory).resolve()
            if scratch == self.repo or self.repo in scratch.parents: raise Declined('transaction scratch must be outside checkout')
            backup = scratch / 'saved'; backup.mkdir()
            index = Path(os.fsdecode(self.git('rev-parse', '--git-path', 'index').stdout).strip())
            if not index.is_absolute(): index = self.repo / index
            saved_index = scratch / 'index'; shutil.copy2(index, saved_index)
            root_mode = stat.S_IMODE(self.repo.stat().st_mode)
            saved = inventory(self.repo, backup) if generated or append else None
            try:
                result = self.git('merge', '--no-ff', '--no-edit', self.base, check=False)
                if result.returncode == 0: return 0
                self.resolve(generated, append, scratch)
                return 0
            except BaseException:
                restoration_errors = set()
                try:
                    self.git('merge', '--abort', check=False)
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
                raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo', required=True, type=Path)
    parser.add_argument('--config', required=True, type=Path)
    parser.add_argument('--base', required=True)
    args = parser.parse_args()
    def stop(signum, frame): raise Declined('merge transaction interrupted')
    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP): signal.signal(sig, stop)
    try:
        return Transaction(args.repo.resolve(), json.loads(args.config.read_text()), args.base, args.config.resolve()).merge()
    except (Declined, OSError, ValueError, provider.UntrustedEnvError) as exc:
        # Do not copy command text, paths or child stderr into control messages.
        print('  Conflict resolution declined: ' + str(exc) if isinstance(exc, Declined) else '  Conflict resolution unavailable.', file=sys.stderr)
        return 1


if __name__ == '__main__': sys.exit(main())
