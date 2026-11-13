#!/usr/bin/env python3
"""Advisory fail-before-pass evidence, using only pinned configuration and trees."""
import argparse
from collections import Counter
from contextlib import contextmanager
import fcntl
import importlib.util
import json
import os
from pathlib import Path
import re
import shlex
import signal
import subprocess
import sys
import tempfile
import time
import unicodedata

sys.dont_write_bytecode = True
spec = importlib.util.spec_from_file_location('changed_acceptance', Path(__file__).with_name('bureau-acceptance.py'))
a = importlib.util.module_from_spec(spec); spec.loader.exec_module(a)
p, e, g = a.p, a.e, a.p.g


GIT_CONTEXT = ('GIT_INDEX_FILE', 'GIT_DIR', 'GIT_WORK_TREE', 'GIT_OBJECT_DIRECTORY',
               'GIT_ALTERNATE_OBJECT_DIRECTORIES', 'GIT_COMMON_DIR', 'GIT_NAMESPACE',
               'GIT_PREFIX', 'GIT_CEILING_DIRECTORIES')


@contextmanager
def git_context():
    # Both Git probes and test commands must resolve their own measurement tree.
    inherited = {name: os.environ.pop(name) for name in GIT_CONTEXT if name in os.environ}
    try: yield
    finally: os.environ.update(inherited)


class Interrupted(Exception):
    pass


def safe_path(path):
    return (0 < len(path) <= 4096 and not path.startswith('/')
            and all(part.lower() not in ('', '.', '..', '.git') for part in path.split('/'))
            and not any(unicodedata.category(char).startswith('C') for char in path))


def file_command(template, path):
    # Quoting a replacement inside existing quotes is unsafe. Require one
    # complete, unquoted shell word, then quote the entire relative filename.
    if not isinstance(template, str) or not template.strip() or len(template) > 32768:
        raise ValueError('invalid command template')
    lexer = shlex.shlex(template, posix=False, punctuation_chars=True)
    lexer.whitespace_split = True
    words = list(lexer)
    if (template.count('{file}') != 1 or words.count('{file}') != 1 or shlex.split(template).count('{file}') != 1
            or re.search(r'[\x00-\x1f\x7f]', template)
            or any(re.search(r'[;&|<>`$(){}]', word) for word in words
                   if word != '{file}' and not word.startswith(('"', "'")))):
        raise ValueError('invalid command template')
    if not safe_path(path): raise ValueError('unsafe path')
    return template.replace('{file}', shlex.quote('./' + path))


def unmeasured(row, reason):
    row.update({'class': 'not measured', 'reason': reason})
    return row


def measurement_record(head, template):
    return dict(base=None, head=head, command_sha256=e.text_digest(template) if isinstance(template, str) else e.digest(template),
                recorded_at=time.time(), files=[], omitted=0, reason=None)


CLEANUP_SECONDS = 120


def remove_directory(directory, timeout):
    # Recursive deletion is expendable; the next invocation sweeps leftovers.
    command = [sys.executable, '-I', '-c', 'import shutil, sys; shutil.rmtree(sys.argv[1])', str(directory)]
    subprocess.run(command, check=True, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                   stderr=subprocess.DEVNULL, env=p.process_env(command), timeout=timeout)


@git_context()
def deregister(directory, timeout=CLEANUP_SECONDS):
    # Remove the two back-pointers before any recursive deletion. Pruning this
    # private Git store cannot touch registrations of real checkouts.
    for name in ('head', 'base'):
        (directory / name / '.git').unlink(missing_ok=True)
    common = directory / 'git'
    if common.is_dir():
        command = ['git', '--git-dir', str(common), 'worktree', 'prune', '--expire=now']
        subprocess.run(command, check=True, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, env=p.process_env(command), timeout=timeout)


def runtime_ownership(root, run=None):
    try: leases = json.loads((root / 'leases.json').read_text())
    except FileNotFoundError: leases = {}
    if not isinstance(leases, dict): raise ValueError('invalid runtime ownership')
    owned = {key: value for key, value in leases.items() if run is None or value.get('run_id') == run}
    paths = [root / 'processes' / (run + '.json')] if run else (root / 'processes').glob('*.json')
    processes = {}
    for path in paths:
        try: processes[path.name] = json.loads(path.read_text())
        except FileNotFoundError: pass
    return owned, processes


def sweep(parent, deadline):
    if not parent.is_dir(): return
    for directory in sorted(parent.glob('run-*')):
        remaining = deadline - time.monotonic()
        if remaining <= 0: break
        try:
            with (directory / 'lock').open('a') as lock:
                try: fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError: continue
                if p.pg.process_holds(directory / 'git' / 'bureau'): continue
                deregister(directory, min(CLEANUP_SECONDS, remaining))
                remaining = deadline - time.monotonic()
                if remaining <= 0: break
                remove_directory(directory, min(CLEANUP_SECONDS, remaining))
        except (OSError, subprocess.SubprocessError): pass


@git_context()
def measure(repo, pinned, config, template, started=None, head=None):
    deadline = (time.monotonic() if started is None else started) + g.qa_timeout()
    if head is None:
        remaining = deadline - time.monotonic()
        if remaining <= 0: raise TimeoutError('budget exhausted')
        command = ['git', '-C', str(repo), 'rev-parse', '--verify', 'HEAD']
        head = subprocess.check_output(command, env=p.process_env(command),
                                       timeout=remaining).decode().strip()
    record = measurement_record(head, template)
    previous = {}; trees = []; directory = None; lock = None; options = {}; interrupted = False; failure = None

    def cancel():
        if directory: deregister(directory)

    def stop(signum, frame):
        try: cancel()
        finally: raise Interrupted()

    def git(where, *args):
        remaining = deadline - time.monotonic()
        if remaining <= 0: raise TimeoutError('budget exhausted')
        command = ['git', '-C', str(where), '--literal-pathspecs', *args]
        return subprocess.check_output(command, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
                                       env=p.process_env(command), timeout=remaining)

    def execute(tree, command, log):
        remaining = deadline - time.monotonic()
        if remaining <= 0: raise TimeoutError('budget exhausted')
        options['timeout'] = remaining
        run = os.environ.get('BUREAU_RUN_ID')
        # The active QA parent's ownership is expected; measurement must leave
        # neither new ownership nor any private runtime registration behind.
        before = runtime_ownership(root, run)
        try: code, abort = p.outside_gate(options, tree, command, log)
        except Exception as exc:
            held = True
            try:
                held = (bool(p.pg.process_holds(root, run=run)) or runtime_ownership(root, run) != before
                        or any(runtime_ownership(directory / 'git/bureau')))
            except (OSError, ValueError, AttributeError): pass
            if getattr(exc, 'uncertain', False) or held:
                try:
                    if not p.pg.process_holds(root, run=run):
                        hold = p.pg.ProcessHold(root, 'outside_gate', tree)
                        hold.close(stopped=False)
                except OSError: pass
                outcome = (exc.code, exc.abort) if isinstance(exc, p.GateExecutionError) else (
                    (130, 'kill') if options.get('_gate_cancelled') else (22, None))
                raise p.GateExecutionError(exc, outcome, uncertain=True) from exc
            if options.get('_gate_cancelled'): raise Interrupted()
            raise OSError('execution unavailable')
        if options.get('_gate_cancelled') or abort == 'kill': raise Interrupted()
        if abort or code >= 124 or code < 0:
            raise TimeoutError('budget exhausted') if abort == 'timeout' or code == 124 else OSError('execution unavailable')
        output = g.qa_test_output(log) if code else None
        return dict(status='passed' if code == 0 else 'assertion failure' if output and output['failed'] else 'failed',
                    normalized_exit=code)

    reason = 'diff unavailable'
    try:
        for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP): previous[sig] = signal.signal(sig, stop)
        base = git(repo, 'merge-base', pinned, head).decode().strip()
        record['base'] = base
        reason = 'invalid test patterns'
        patterns = a.risk_settings(config)['test_patterns']
        reason = 'diff unavailable'
        paths = sorted(path.decode('utf-8', 'surrogateescape') for path in
                       git(repo, 'diff', '--no-ext-diff', '--no-textconv', '--no-renames', '--diff-filter=AM',
                           '--name-only', '-z', base, head, '--').split(b'\0') if path)
        paths = [path for path in paths if a.matches(path, patterns)]
        record['omitted'] = max(0, len(paths) - 20)
        for path in paths[:20]:
            record['files'].append(dict(path=path, base_result=dict(status='not measured', normalized_exit=None),
                                        head_result=dict(status='not measured', normalized_exit=None),
                                        **{'class': 'not measured', 'reason': 'worktree unavailable'}))
        if config.get('repo', {}).get('worktree_links'):
            record['reason'] = 'repository uses worktree links'
            return record
        reason = 'invalid command template'
        file_command(template, 'test-file')
        for row in record['files']:
            if not safe_path(row['path']): unmeasured(row, 'unsafe path')
        usable = [row for row in record['files'] if row['reason'] != 'unsafe path']
        if not usable: return record
        reason = 'worktree unavailable'
        options['untrusted_env'] = p.untrusted_env_mode(config)
        options['untrusted_git_credentials'] = p.untrusted_git_credentials_mode(config)
        options['_gate_on_cancel'] = cancel
        common = (repo / os.fsdecode(git(repo, 'rev-parse', '--git-common-dir')).strip()).resolve()
        root = common / 'bureau'
        parent = root / 'changed-tests'
        sweep(parent, min(deadline, time.monotonic() + CLEANUP_SECONDS))
        parent.mkdir(parents=True, exist_ok=True)
        directory = Path(tempfile.mkdtemp(prefix='run-', dir=parent))
        lock = (directory / 'lock').open('x'); fcntl.flock(lock, fcntl.LOCK_EX)
        # Worktree registrations and optional evidence are private. Mandatory
        # holds still use normal coordination, including if this helper dies.
        private = directory / 'git'
        git(repo, 'init', '--bare', str(private))
        git(repo, '--git-dir', str(private), 'config', 'maintenance.auto', 'false')
        git(repo, '--git-dir', str(private), 'config', 'receive.autogc', 'false')
        (private / 'objects/info/alternates').write_text(str(common / 'objects') + '\n')
        (root / 'process-holds').mkdir(exist_ok=True)
        (private / 'bureau').mkdir()
        (private / 'bureau/process-holds').symlink_to(root / 'process-holds', target_is_directory=True)
        for name, revision in (('head', head), ('base', base)):
            tree = directory / name; trees.append(tree)
            # Disable checkout hooks for these disposable measurement trees.
            git(repo, '--git-dir', str(private), '-c', 'core.hooksPath=' + os.devnull,
                'worktree', 'add', '--detach', str(tree), revision)
        head_tree, base_tree = trees
        transplant = [path for path in paths if safe_path(path)]
        for offset in range(0, len(transplant), 20):
            git(base_tree, '-c', 'core.hooksPath=' + os.devnull, 'checkout', head, '--', *transplant[offset:offset + 20])
        reason = 'execution unavailable'
        for index, row in enumerate(usable):
            if interrupted:
                unmeasured(row, 'interrupted'); continue
            if time.monotonic() >= deadline:
                unmeasured(row, 'budget exhausted'); continue
            path = row['path']
            if any((tree / path).is_symlink() or not (tree / path).is_file() for tree in trees):
                unmeasured(row, 'test is not a regular file'); continue
            command = file_command(template, path)
            try:
                row['head_result'] = execute(head_tree, command, directory / (str(index) + '-head.log'))
                row['base_result'] = execute(base_tree, command, directory / (str(index) + '-base.log'))
                row['class'] = e.changed_test_class(row['base_result'], row['head_result'])
                row['reason'] = 'head did not pass' if row['class'] == 'not measured' else None
            except Interrupted:
                interrupted = True; unmeasured(row, 'interrupted')
            except p.GateExecutionError: raise
            except TimeoutError: unmeasured(row, 'budget exhausted')
            except (OSError, ValueError, subprocess.SubprocessError): unmeasured(row, 'execution unavailable')
    except p.GateExecutionError as exc:
        failure = exc; record['reason'] = exc.reason
    except Interrupted: reason = 'interrupted'; record['reason'] = reason
    except (TimeoutError, subprocess.TimeoutExpired): reason = 'budget exhausted'; record['reason'] = reason
    except (OSError, ValueError, TypeError, subprocess.SubprocessError): record['reason'] = reason
    finally:
        # Keep essential deregistration ahead of bounded, retryable deletion.
        for sig in previous: signal.signal(sig, signal.SIG_IGN)
        cleanup_failed = False
        try:
            if directory:
                deregister(directory)
                if failure is None:
                    try: remove_directory(directory, CLEANUP_SECONDS)
                    except (OSError, subprocess.SubprocessError): cleanup_failed = True
        except (OSError, subprocess.SubprocessError):
            cleanup_failed = True
        finally:
            if lock: lock.close()
            if directory:
                try: directory.parent.rmdir()
                except OSError: pass
        if record['reason']:
            for row in record['files']:
                row['base_result'] = dict(status='not measured', normalized_exit=None)
                row['head_result'] = dict(status='not measured', normalized_exit=None)
                unmeasured(row, record['reason'])
        # Filesystem cleanup cannot invalidate completed measurements.
        if cleanup_failed and not record['reason']: record['reason'] = 'cleanup failed'
        for sig, handler in previous.items(): signal.signal(sig, handler)
    if failure:
        failure.record = record
        raise failure
    return record


@git_context()
def main():
    started = time.monotonic()
    code = 0
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo', required=True); parser.add_argument('--base', required=True)
    parser.add_argument('--branch')
    args = parser.parse_args(); repo = Path(args.repo).resolve()
    try:
        # Never consult the PR checkout or BUREAU_CONFIG for this opt-in.
        deadline = started + g.qa_timeout()
        def prepare(command):
            remaining = deadline - time.monotonic()
            if remaining <= 0: raise TimeoutError('budget exhausted')
            return subprocess.check_output(command, cwd=repo, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
                                           env=p.process_env(command), timeout=remaining)
        def git(*args): return prepare(['git', '-C', str(repo), *args])
        pinned = git('rev-parse', '--verify', (args.base or 'refs/remotes/origin/main') + '^{commit}').decode().strip()
        head = git('rev-parse', '--verify', 'HEAD').decode().strip()
        try: config = json.loads(git('show', pinned + ':.bureau.json'))
        except subprocess.CalledProcessError: return 0
        template = config.get('repo', {}).get('changed_test_command')
        if template is None: return 0
        try:
            # Resolve a provider-free run's actual PR base only after the local
            # trusted opt-in. These requests share the measurement deadline.
            if not args.base and args.branch:
                number = prepare(['gh', 'pr', 'list', '--head', args.branch, '--json', 'number', '--jq', '.[0].number']).decode().strip()
                if number and number != 'null':
                    if not re.fullmatch(r'[1-9][0-9]*', number): raise ValueError('invalid PR')
                    refs = json.loads(prepare(['gh', 'pr', 'view', number, '--json', 'state,baseRefName,headRefName']))
                    base_ref = refs['baseRefName']
                    if refs['state'] != 'OPEN' or refs['headRefName'] != args.branch or base_ref.startswith('-'):
                        raise ValueError('invalid PR base')
                    git('check-ref-format', 'refs/heads/' + base_ref)
                    pinned = git('rev-parse', '--verify', 'refs/remotes/origin/' + base_ref + '^{commit}').decode().strip()
                    config = json.loads(git('show', pinned + ':.bureau.json'))
                    template = config.get('repo', {}).get('changed_test_command')
                    if template is None: return 0
            record = measure(repo, pinned, config, template, started, head)
        except p.GateExecutionError as exc:
            record = exc.record; code = exc.code
            p.note('bureau: human_hold: ' + exc.reason)
        except (TimeoutError, subprocess.TimeoutExpired):
            record = measurement_record(head, template); record['reason'] = 'budget exhausted'
        except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
            record = measurement_record(head, template); record['reason'] = 'diff unavailable'
        run, attempt = os.environ.get('BUREAU_RUN_ID'), os.environ.get('BUREAU_STAGE_ATTEMPT_ID')
        if run and attempt: e.observe(p.evidence_store(repo).changed_tests, run, attempt, record)
        counts = Counter(row['class'] for row in record['files'])
        summary = ('  Fail-before-pass: ' + '; '.join(f'{name}={counts[name]}' for name in e.CHANGED_TEST_CLASSES) +
                   f'; omitted={record["omitted"]}' + (f'; {record["reason"]}' if record['reason'] else ''))
        if code: p.note(summary)
        else: print(summary, flush=True)
        return code
    except p.UntrustedEnvError as exc:
        p.note('  Fail-before-pass: ' + str(exc))
        return 24
    except (OSError, ValueError, TypeError, subprocess.SubprocessError):
        p.note('  Fail-before-pass: not measured (evidence unavailable).')
        return code


if __name__ == '__main__': sys.exit(main())
