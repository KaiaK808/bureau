#!/usr/bin/env python3
"""Advisory task completion evidence, reading only pinned Git objects."""
import argparse
from contextlib import contextmanager
import importlib.util
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import time

sys.dont_write_bytecode = True
spec = importlib.util.spec_from_file_location('completion_acceptance', Path(__file__).with_name('bureau-acceptance.py'))
a = importlib.util.module_from_spec(spec); spec.loader.exec_module(a)
p, e = a.p, a.e

MAX_FILES = 200
MAX_FILE_BYTES = 1024 * 1024
BUDGET_SECONDS = 60
GIT_CONTEXT = ('GIT_INDEX_FILE', 'GIT_DIR', 'GIT_WORK_TREE', 'GIT_OBJECT_DIRECTORY',
               'GIT_ALTERNATE_OBJECT_DIRECTORIES', 'GIT_COMMON_DIR', 'GIT_NAMESPACE',
               'GIT_PREFIX', 'GIT_CEILING_DIRECTORIES')
TASK = re.compile(r'^\s*(?:[-+*]|\d+[.)])\s+\[([ xX])\](?:\s+|$)(.*)$')


class Interrupted(Exception):
    pass


class FileUnavailable(Exception):
    pass


@contextmanager
def git_context():
    inherited = {name: os.environ.pop(name) for name in GIT_CONTEXT if name in os.environ}
    try: yield
    finally: os.environ.update(inherited)


@contextmanager
def budget(started):
    def expire(signum, frame): raise TimeoutError('budget exhausted')
    def stop(signum, frame): raise Interrupted()
    previous = {sig: signal.signal(sig, stop) for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP)}
    previous[signal.SIGALRM] = signal.signal(signal.SIGALRM, expire)
    entered = time.monotonic()
    timer = signal.setitimer(signal.ITIMER_REAL, max(.001, started + BUDGET_SECONDS - entered))
    try: yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        for sig, handler in previous.items(): signal.signal(sig, handler)
        if timer[0]: signal.setitimer(signal.ITIMER_REAL, max(.001, timer[0] - (time.monotonic() - entered)), timer[1])


class Git:
    def __init__(self, repo, started):
        self.repo, self.deadline = repo, started + BUDGET_SECONDS

    def prepare(self, command):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0: raise TimeoutError('budget exhausted')
        env = dict(p.process_env(command), GIT_NO_LAZY_FETCH='1', GIT_OPTIONAL_LOCKS='0')
        return subprocess.check_output(command, cwd=self.repo, stdin=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                       env=env, timeout=remaining)

    def __call__(self, *args):
        return self.prepare(['git', '-C', str(self.repo), '--literal-pathspecs', *args])

    def contents(self, revision, path):
        if not e.integrity_path(path): raise FileUnavailable('unsafe path')
        obj = revision + ':' + path
        try:
            if int(self('cat-file', '-s', obj)) > MAX_FILE_BYTES:
                raise FileUnavailable('file size limit exceeded')
            return self('show', '--no-ext-diff', '--no-textconv', obj, '--')
        except (OSError, ValueError, subprocess.CalledProcessError): raise FileUnavailable('file unavailable')


class Measurement:
    def __init__(self, head, tasks):
        self.record = dict(base=None, head=head, tasks=tasks if e.integrity_path(tasks) else None,
                           recorded_at=time.time(), verdict='unverifiable',
                           counts=dict(verified=0, flagged=0, unverifiable=0, unchecked=0),
                           findings=[], omitted=0, reason=None)

    def finding(self, line, verdict, rule, paths=()):
        self.record['counts'][verdict] += 1
        if len(self.record['findings']) < 40:
            self.record['findings'].append(dict(line=line, verdict=verdict, rule=rule,
                                               paths=list(paths[:20]), omitted_paths=max(0, len(paths) - 20)))
        else: self.record['omitted'] += 1

    def finish(self):
        self.record['verdict'] = e.task_completion_verdict(self.record['counts'])
        return self.record


def paths_changed(paths, changed):
    return any(path == name or name.startswith(path + '/') for path in paths for name in changed)


def tasks_path(repo, tasks):
    if not tasks: return tasks
    try: return a.paths.normalize(os.path.relpath(tasks, repo) if os.path.isabs(tasks) else tasks)
    except ValueError: return ''


@git_context()
def measure(repo, pinned, config, tasks, started=None, head=None, measurement=None):
    started = time.monotonic() if started is None else started
    git = Git(repo, started)
    if head is None: head = git('rev-parse', '--verify', 'HEAD').decode().strip()
    tasks = tasks_path(repo, tasks)
    m = measurement or Measurement(head, tasks)
    pending = []; reason = 'diff unavailable'
    with budget(started):
        try:
            base = git('merge-base', pinned, head).decode().strip(); m.record['base'] = base
            reason = 'file unavailable'
            source = git.contents(head, tasks).decode('utf-8')
            for number, line in enumerate(source.splitlines(), 1):
                task = TASK.match(line)
                if not task: continue
                if task[1] == ' ': m.record['counts']['unchecked'] += 1
                else: pending.append((number, task[2]))
            reason = 'invalid test patterns'; patterns = a.risk_settings(config)['test_patterns']
            reason = 'invalid path prefix'; prefix = config.get('repo', {}).get('path_prefix_strip', '')
            if not isinstance(prefix, str): raise ValueError(reason)
            reason = 'diff unavailable'
            fields = git('diff', '--no-ext-diff', '--no-textconv', '--no-renames', '--diff-filter=AMDT',
                         '--name-status', '-z', base, head, '--').split(b'\0')
            rows = [(fields[i].decode('ascii'), fields[i + 1].decode('utf-8', 'surrogateescape'))
                    for i in range(0, len(fields) - 1, 2)]
            if len(rows) > MAX_FILES: raise FileUnavailable('changed file limit exceeded')
            changed = {path for _, path in rows}
            test_changed = any(status in ('A', 'M') and a.matches(path, patterns) for status, path in rows)
            reason = 'tree unavailable'
            tracked = {path.decode('utf-8', 'surrogateescape') for revision in (base, head) for path in
                       git('ls-tree', '-r', '--name-only', '-z', revision, '--').split(b'\0') if path}
            reason = 'budget exhausted'
            for number, text in pending:
                scope = a.paths.extract(text, tracked, prefix)
                paths = scope['paths'] or []
                rule = 'ambiguous paths' if scope['paths'] is None and scope['unknown'] != ['no paths recognized'] else 'no paths recognized'
                verdict = 'unverifiable'
                if paths and not all(e.integrity_path(path) for path in paths):
                    paths = []; rule = 'unsafe path'
                if re.search(r'\btests?\b', text, re.I) and not test_changed:
                    verdict, rule = 'flagged', 'no test file changed'
                elif paths:
                    verdict, rule = ('verified', 'named path changed') if paths_changed(paths, changed) else (
                        'flagged', 'no named path changed')
                m.finding(number, verdict, rule, paths)
            pending = []
        except Interrupted: m.record['reason'] = 'interrupted'
        except (TimeoutError, subprocess.TimeoutExpired): m.record['reason'] = 'budget exhausted'
        except FileUnavailable as exc: m.record['reason'] = str(exc)
        except (OSError, ValueError, TypeError, IndexError, subprocess.SubprocessError): m.record['reason'] = reason
    completed = m.record['counts']['verified'] + m.record['counts']['flagged'] + m.record['counts']['unverifiable']
    for number, _ in pending[completed:]: m.finding(number, 'unverifiable', m.record['reason'])
    return m.finish()


def setting(config):
    value = config.get('repo', {}).get('task_completion_check', False)
    if type(value) is not bool:
        print('  Task completion: not measured (invalid setting).', flush=True)
        return False
    return value


@git_context()
def main():
    started = time.monotonic()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo', required=True); parser.add_argument('--base', required=True)
    parser.add_argument('--branch'); parser.add_argument('--tasks', required=True)
    args = parser.parse_args(); repo = Path(args.repo).resolve()
    args.tasks = tasks_path(repo, args.tasks)
    m = None; code = 0
    try:
        git = Git(repo, started)
        with budget(started):
            pinned = git('rev-parse', '--verify', '--end-of-options', (args.base or 'refs/remotes/origin/main') + '^{commit}').decode().strip()
            head = git('rev-parse', '--verify', 'HEAD').decode().strip()
            try: config = json.loads(git.contents(pinned, '.bureau.json'))
            except FileUnavailable: return 0
            if not setting(config): return 0
            m = Measurement(head, args.tasks)
            common = (repo / os.fsdecode(git('rev-parse', '--git-common-dir')).strip()).resolve()
            try:
                if not args.base and args.branch:
                    number = git.prepare(['gh', 'pr', 'list', '--head', args.branch, '--json', 'number', '--jq', '.[0].number']).decode().strip()
                    if number and number != 'null':
                        if not re.fullmatch(r'[1-9][0-9]*', number): raise ValueError('invalid PR')
                        refs = json.loads(git.prepare(['gh', 'pr', 'view', number, '--json', 'state,baseRefName,headRefName']))
                        base_ref = refs['baseRefName']
                        if refs['state'] != 'OPEN' or refs['headRefName'] != args.branch or base_ref.startswith('-'):
                            raise ValueError('invalid PR base')
                        git('check-ref-format', 'refs/heads/' + base_ref)
                        pinned = git('rev-parse', '--verify', 'refs/remotes/origin/' + base_ref + '^{commit}').decode().strip()
                        config = json.loads(git.contents(pinned, '.bureau.json'))
                        if not setting(config): return 0
                record = measure(repo, pinned, config, args.tasks, started, head, m)
            except Interrupted:
                m.record['reason'] = 'interrupted'; record = m.finish(); code = 130
            except (TimeoutError, subprocess.TimeoutExpired):
                m.record['reason'] = 'budget exhausted'; record = m.finish()
            except (OSError, ValueError, KeyError, TypeError, FileUnavailable, subprocess.SubprocessError):
                m.record['reason'] = 'diff unavailable'; record = m.finish()
        if record['reason'] == 'interrupted': code = 130
        run, attempt = os.environ.get('BUREAU_RUN_ID'), os.environ.get('BUREAU_STAGE_ATTEMPT_ID')
        if run and attempt:
            if e.observe(e.Store(common / 'bureau').task_completion, run, attempt, record) is None:
                print('  Task completion: not measured (evidence unavailable).', flush=True)
                return code
        counts = record['counts']
        print(f'  Task completion: {record["verdict"]}; verified={counts["verified"]}; flagged={counts["flagged"]}; '
              f'unverifiable={counts["unverifiable"]}; unchecked={counts["unchecked"]}; omitted={record["omitted"]}' +
              (f'; {record["reason"]}' if record['reason'] else ''), flush=True)
        return code
    except Interrupted:
        print('  Task completion: not measured (interrupted).', flush=True); return 130
    except Exception:
        print('  Task completion: not measured.', flush=True); return code


if __name__ == '__main__': sys.exit(main())
