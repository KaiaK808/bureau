#!/usr/bin/env python3
"""Dormant test wrapper primitive. No stage routes here until a separate upgrade.

A foreground prefix must wait for the private envelope. Only the envelope's
successful native Popen proves start; prefix exit alone proves no child result.
Coordination belongs outside checkout content, including in .git. Records are protocol data,
not evidence-v1 receipts, and are not an isolation boundary against the same UID.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path
import signal
import stat
import subprocess
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass
from functools import lru_cache

sys.dont_write_bytecode = True
SCRIPTS = Path(__file__).resolve().parent
ENVELOPE_SOURCE = Path(__file__).read_bytes()
INTERPRETER = str(Path(sys.executable).resolve())
SIGNALS = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)


@lru_cache(None)
def module(name):
    spec = importlib.util.spec_from_file_location('wrapper_' + name.replace('-', '_'), SCRIPTS / ('bureau-' + name + '.py'))
    value = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = value
    spec.loader.exec_module(value)
    return value


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def validate_policy(config):
    """Shared shape validation, including explicitly disabled configurations."""
    if not isinstance(config, dict) or not isinstance(config.get('repo', {}), dict):
        raise ValueError('repo must be an object')
    repo = config.get('repo', {})
    argv = repo.get('test_wrapper', [])
    if (not isinstance(argv, list) or any(not isinstance(arg, str) or
            any(ord(c) < 32 or 127 <= ord(c) <= 159 for c in arg) for arg in argv)
            or (argv and not os.path.isabs(argv[0]))):
        raise ValueError('repo.test_wrapper must be a literal argv array with an absolute executable and no control characters')
    timeout = repo.get('test_wrapper_start_timeout_seconds', 21600)
    if not finite(timeout) or not 0 < timeout <= 86400:
        raise ValueError('repo.test_wrapper_start_timeout_seconds must be finite and within (0, 86400]')
    return tuple(argv), float(timeout)


@dataclass(frozen=True)
class Policy:
    argv: tuple
    start_timeout: float
    source: str
    executable: str | None
    identity: str
    env_mode: str
    git_credentials: str

    @property
    def enabled(self):
        return bool(self.argv)


def within(path, root):
    return path == root or root in path.parents


class PolicyUnavailable(ValueError):
    """The authoritative configuration could not be obtained."""


def physical_policy_path(selected, roots):
    """Inspect every physical component before normalizing away parent steps."""
    current = Path(selected.anchor)
    pending = list(selected.parts[1:])
    direct = '..' not in selected.parts
    links = 0
    while pending:
        part = pending.pop(0)
        if part == '..':
            current = current.parent
            continue
        current = current / part
        if current.is_symlink():
            # Only a direct path that stays lexically in that checkout is safe: it is read pinned.
            if any(within(current, root) and not (direct and within(selected, root)) for root in roots):
                raise ValueError('configuration path traverses a tested checkout symlink')
            links += 1
            if links > 40:
                raise ValueError('configuration path contains too many symlinks')
            target = Path(os.readlink(current))
            current = Path(target.anchor) if target.is_absolute() else current.parent
            pending = list(target.parts[1:] if target.is_absolute() else target.parts) + pending
    return current


def git_metadata_dir(checkout, env):
    """The checkout's genuine git directory; a symlink or look-alike directory is not one."""
    path = checkout / '.git'
    if path.is_symlink() or not path.is_dir():
        return None
    # Inherited repository selection would answer for another repository despite -C.
    env = {key: value for key, value in env.items()
           if key not in ('GIT_DIR', 'GIT_WORK_TREE', 'GIT_COMMON_DIR', 'GIT_PREFIX')}
    try:
        found = subprocess.run(['git', '-C', str(checkout), 'rev-parse', '--absolute-git-dir'],
                               capture_output=True, text=True, timeout=15, env=env)
    except (OSError, subprocess.SubprocessError):
        return None
    if found.returncode != 0 or Path(found.stdout.strip()).resolve() != path.resolve():
        return None
    return path.resolve()


def resolve_policy(repo, *, primary_config, trusted_base=None, operator_config=None, tested_checkouts=()):
    """Freeze trusted content. BUREAU_CONFIG is deliberately never consulted.

    operator_config is an explicit coordinator argument, never an auto-exported
    environment value. Any chosen config in a tested tree requires a pinned blob.
    The caller pins trusted_base before allowing a provider to change the tree.
    """
    repo = Path(repo).resolve()
    roots = tuple(dict.fromkeys((repo, *(Path(p).resolve() for p in tested_checkouts))))
    selected = Path(operator_config if operator_config is not None else primary_config).absolute()
    resolved = physical_policy_path(selected, roots)
    selected = Path(os.path.abspath(selected))
    # Check lexical and resolved paths: a checkout symlink cannot select authority.
    controlled = next((root for root in roots if within(selected, root) or within(resolved, root)), None)
    if controlled is not None:
        if not within(selected, controlled):
            raise ValueError('configuration symlink enters a tested checkout')
        if trusted_base is None:
            raise PolicyUnavailable('a pinned base commit is required')
        try:
            text = module('review-config').Blobs(trusted_base, controlled).read(selected.relative_to(controlled).as_posix())
        except (OSError, subprocess.SubprocessError) as exc:
            raise PolicyUnavailable(str(exc)) from exc
        config = json.loads(text) if text is not None else {}
        source = 'pinned-base:' + str(trusted_base) + ':' + selected.relative_to(controlled).as_posix()
    else:
        try:
            text = resolved.read_text()
        except OSError as exc:
            raise PolicyUnavailable(str(exc)) from exc
        config = json.loads(text)
        source = ('operator:' if operator_config is not None else 'primary:') + str(resolved)
    argv, timeout = validate_policy(config)
    executable = None
    if argv:
        target = Path(argv[0]).resolve(strict=True)
        if any(within(target, root) for root in roots):
            raise ValueError('wrapper executable resolves inside a tested checkout')
        if not target.is_file() or not os.access(target, os.X_OK):
            raise ValueError('wrapper executable is not an executable regular file')
        executable = str(target)
        argv = (executable, *argv[1:])
    provider = module('provider')
    mode = provider.untrusted_env_mode(config)
    credentials = provider.untrusted_git_credentials_mode(config)
    return Policy(argv, timeout, source, executable,
                  digest([argv, timeout, source, config]), mode, credentials)


def diagnose_policy(repo, *, primary_config, trusted_base=None, operator_config=None):
    """Read and resolve only; never run the prefix, including for --help."""
    report = dict(source=None, enabled=None, executable=None, routing_installed=False,
                  routing='Stage routing is not installed yet.')
    selected = Path(operator_config if operator_config is not None else primary_config).absolute()
    controlled = within(Path(os.path.abspath(selected)), Path(repo).resolve()) or within(selected.resolve(), Path(repo).resolve())
    report['source'] = ('pinned-base:' + str(trusted_base) + ':' + selected.name if controlled
                        else ('operator:' if operator_config is not None else 'primary:') + str(selected))
    try:
        policy = resolve_policy(repo, primary_config=primary_config, trusted_base=trusted_base,
                                operator_config=operator_config)
        report.update(source=policy.source, enabled=policy.enabled, executable=policy.executable,
                      start_timeout_seconds=policy.start_timeout, policy_digest=policy.identity)
    except PolicyUnavailable as exc:
        report.update(error=str(exc), authority_unavailable=True)
    except (OSError, ValueError, TypeError, subprocess.SubprocessError) as exc:
        report['error'] = str(exc)
    return report


class BudgetClock:
    """Monotonic elapsed minus the union of owned, validated queue intervals.

    Open intervals are registered by the trusted executor before prefix spawn.
    Consumers share this clock for command, stage/provider and silence budgets;
    they keep their own starting charged() readings, preserving earlier work.
    Failed proof revokes its interval rather than granting retrospective credit.
    """
    def __init__(self, run_id, attempt_id, monotonic=time.monotonic):
        if not run_id or not attempt_id:
            raise ValueError('run and stage attempt identities are required')
        self.owner = (run_id, attempt_id)
        self.monotonic = monotonic
        self.origin = monotonic()
        self.intervals = {}

    def queue(self, owner, execution_id):
        if tuple(owner) != self.owner or execution_id in self.intervals:
            raise ValueError('queue interval owner or execution mismatch')
        self.intervals[execution_id] = [self.monotonic(), None]
        return self.intervals[execution_id][0]

    def started(self, execution_id, at):
        start, end = self.intervals[execution_id]
        if end is not None or not finite(at) or not start <= at <= self.monotonic():
            raise ValueError('invalid queue end')
        self.intervals[execution_id][1] = at

    def revoke(self, execution_id):
        self.intervals.pop(execution_id, None)

    def charged(self):
        now = self.monotonic()
        merged = []
        for start, end in sorted((max(self.origin, a), min(now, b if b is not None else now))
                                 for a, b in self.intervals.values()):
            if merged and start <= merged[-1][1]:
                merged[-1][1] = max(end, merged[-1][1])
            else:
                merged.append([start, end])
        return now - self.origin - sum(max(0, b - a) for a, b in merged)


@dataclass(frozen=True)
class Result:
    code: int
    kind: str
    reason: str
    state: str
    execution_id: str
    records: str | None = None
    child_status: int | None = None
    wrapper_status: int | None = None


def command_argv(command):
    if isinstance(command, str):
        return ['/bin/bash', '--noprofile', '--norc', '-o', 'pipefail', '-c', command]
    if not isinstance(command, (list, tuple)) or not command or any(not isinstance(a, str) or '\0' in a for a in command):
        raise ValueError('command must be shell text or nonempty native argv')
    return list(command)


def compose(policy, directory, command):
    return [*policy.argv, INTERPRETER, '-I', '-B', str(directory / 'envelope.py'),
            '--envelope', str(directory), '--', *command_argv(command)]


def publish(path, record, *, replace=False):
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.pending')
    try:
        with os.fdopen(os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'w') as out:
            json.dump(record, out, allow_nan=False)
            out.flush()
            os.fsync(out.fileno())
        if replace:
            os.replace(temporary, path)
        else:
            os.link(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def read_record(path):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return None
    with os.fdopen(fd) as source:
        info = os.fstat(source.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_size > 16384:
            raise ValueError('unsafe protocol record')
        value = json.load(source)
    if not isinstance(value, dict):
        raise ValueError('protocol record must be an object')
    return value


IDENTITY = ('version', 'execution_id', 'run_id', 'attempt_id', 'command_digest', 'policy_digest', 'queued_at')


def status(code):
    return 128 - code if code < 0 else code


def proof(record, expected, now, start=None):
    if not isinstance(record, dict): raise ValueError('malformed wrapper proof')
    keys = ('version', 'execution_id', 'run_id', 'attempt_id', 'command_digest', 'policy_digest', 'queued_at')
    if any(type(record.get(k)) is not type(expected[k]) or record[k] != expected[k] for k in keys):
        raise ValueError('stale or mismatched wrapper proof')
    if type(record.get('pgid')) is not int or record['pgid'] != expected['pgid']:
        raise ValueError('wrapper group mismatch')
    if record.get('wrapper_status') is not None: raise ValueError('premature wrapper status')
    ended = 'ended_at' in record
    if ended:
        if not finite(record['ended_at']) or not expected['queued_at'] <= record['ended_at'] <= now:
            raise ValueError('invalid end time')
        error = record.get('launch_error')
        if error is not None:
            if start is not None or not isinstance(error, str) or not error or record.get('child_status') is not None:
                raise ValueError('invalid launch-error proof')
            return
        if start is None: raise ValueError('end without start proof')
        if any(record.get(k) != start[k] for k in ('started_at', 'pid', 'pgid')):
            raise ValueError('start/end proof disagreement')
        if record['ended_at'] < start['started_at'] or type(record.get('child_status')) is not int:
            raise ValueError('invalid child result')
    if (not finite(record.get('started_at')) or not expected['queued_at'] <= record['started_at'] <= now
            or type(record.get('pid')) is not int or record['pid'] <= 0):
        raise ValueError('invalid child-start proof')


def envelope(directory, command):
    """Remain in the prefix's group; close-fds wrappers need no inherited pipe."""
    request = read_record(directory / 'request.json')
    cancelled = False
    child = None
    def stop(signum, frame):
        nonlocal cancelled
        cancelled = True
        if child is not None:
            try: child.send_signal(signum)
            except ProcessLookupError: pass
    for sig in SIGNALS:
        signal.signal(sig, stop)
    base = {key: request[key] for key in IDENTITY}
    base.update(pgid=os.getpgrp(), wrapper_status=None)
    def failed(error, code):
        publish(directory / 'end.json', dict(base, started_at=None, ended_at=time.monotonic(),
                                           pid=None, child_status=None, launch_error=error))
        return code
    if digest(command) != request['command_digest']:
        return failed('command-mismatch', 24)
    try:
        with open(request['stdout'], 'ab', buffering=0) as out:
            if request['stderr'] == request['stdout']:
                err = out
            else:
                err = open(request['stderr'], 'ab', buffering=0)
            try:
                # The pre-Popen timestamp is published only after exec succeeded.
                at = time.monotonic()
                if cancelled or (directory / 'cancelled.json').exists():
                    return failed('cancelled', 130)
                child = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=out, stderr=err)
                if cancelled:
                    child.kill()
                start = dict(base, started_at=at, pid=child.pid, launch_error=None)
                publish(directory / 'start.json', start)
                code = child.wait()
                publish(directory / 'end.json', dict(start, ended_at=time.monotonic(), child_status=code))
                return status(code)
            finally:
                if err is not out: err.close()
    except OSError as exc:
        return failed(type(exc).__name__, 24)
    finally:
        if child is not None and child.poll() is None:
            child.kill()
            child.wait()


def execute(policy, command, *, repo, stdout, stderr=None, timeout=None, clock=None,
            run_id, attempt_id, coordination_root=None, environ=None, sleep=time.sleep,
            poll_seconds=.02, grace_seconds=5):
    """Execute once with proven-start timeout; return a typed, fail-closed result.

    Resolve policy at the trusted boundary first. Output paths must be fresh and
    trusted. stderr=None combines canonical streams; prefix diagnostics remain in
    the private launch directory. No existing caller invokes this entry point.
    Signal handlers require the coordinator's main thread, as in outside_gate.
    """
    execution = uuid.uuid4().hex
    state = 'QUEUED'
    directory = None
    child = hold = None
    previous = {}
    cancelled = None
    stopped = False
    start = end = None
    result = Result(24, 'launch-error', 'launch-error', state, execution)
    clock = clock or BudgetClock(run_id, attempt_id)
    provider = module('provider')
    pg = module('process')
    def stop(signum, frame):
        nonlocal cancelled
        # No signalling in the handler: latch even during Popen/hold publication.
        if cancelled is None: cancelled = signum
    try:
        if not isinstance(policy, Policy): raise ValueError('resolved policy required')
        argv = command_argv(command)
        if timeout is not None and (not finite(timeout) or timeout <= 0):
            raise ValueError('execution timeout must be positive or None')
        env = provider.untrusted_env(os.environ if environ is None else environ, policy.env_mode,
                                     git_credentials=policy.git_credentials)
        root = Path(coordination_root) if coordination_root is not None else pg.hold_root(repo, provider.process_env(['git']))
        root = root.resolve()
        checkout = Path(repo).resolve()
        if within(root, checkout):
            metadata = git_metadata_dir(checkout, provider.process_env(['git']))
            if metadata is None or not within(root, metadata):
                raise ValueError('coordination directory is inside a tested checkout')
        root.mkdir(parents=True, exist_ok=True)
        directory = Path(tempfile.mkdtemp(prefix='test-wrapper-', dir=root))
        os.chmod(directory, 0o700)
        with os.fdopen(os.open(directory / 'envelope.py', os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'wb') as out:
            out.write(ENVELOPE_SOURCE)
        out_path = Path(stdout).absolute()
        err_path = Path(stderr).absolute() if stderr is not None else out_path
        for path in dict.fromkeys((out_path, err_path)):
            with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'wb'): pass
        for sig in SIGNALS:
            previous[sig] = signal.signal(sig, stop)
        hold = pg.ProcessHold(root, 'test-wrapper', repo, run_id=run_id)
        queued = clock.queue((run_id, attempt_id), execution)
        request = dict(version=1, execution_id=execution, run_id=run_id, attempt_id=attempt_id,
                       command_digest=digest(argv), policy_digest=policy.identity, queued_at=queued,
                       stdout=str(out_path), stderr=str(err_path))
        publish(directory / 'request.json', request)
        if cancelled is not None:
            result = Result(130, 'cancelled', 'cancelled', state, execution)
        else:
            with (directory / 'prefix.stdout').open('xb') as diag_out, (directory / 'prefix.stderr').open('xb') as diag_err:
                os.chmod(diag_out.name, 0o600); os.chmod(diag_err.name, 0o600)
                child = subprocess.Popen(compose(policy, directory, command), cwd=repo, env=env,
                                         stdin=subprocess.DEVNULL, stdout=diag_out, stderr=diag_err, start_new_session=True)
                request['pgid'] = child.pid
                teardown_at = None
                while True:
                    if cancelled is not None:
                        result = Result(130, 'cancelled', 'cancelled', state, execution)
                        break
                    candidate = read_record(directory / 'start.json') if start is None else None
                    end = read_record(directory / 'end.json')
                    if end is not None and start is None and candidate is None:
                        candidate = read_record(directory / 'start.json')
                    now = clock.monotonic()
                    if start is None:
                        if candidate is not None:
                            proof(candidate, request, now)
                            clock.started(execution, candidate['started_at'])
                            start = candidate
                            state = 'RUNNING'
                    if end is not None:
                        proof(end, request, now, start)
                        if end.get('launch_error'):
                            if end['launch_error'] == 'cancelled':
                                result = Result(130, 'cancelled', 'cancelled', state, execution)
                            else:
                                result = Result(24, 'launch-error', 'exec-failed', state, execution)
                            break
                        if start is None: raise ValueError('end without start proof')
                        if teardown_at is None: teardown_at = now + 5
                    if start is not None and start['started_at'] - queued >= policy.start_timeout:
                        result = Result(124, 'timeout', 'wrapper-start-timeout', state, execution)
                        break
                    execution_end = end['ended_at'] if end is not None else now
                    if start is not None and timeout is not None and execution_end - start['started_at'] > timeout:
                        result = Result(124, 'timeout', 'execution-timeout', state, execution)
                        break
                    rc = child.poll()
                    if rc is not None:
                        # Poll again: end/start publication may race this iteration.
                        if start is None and (directory / 'start.json').exists(): continue
                        if end is None and (directory / 'end.json').exists(): continue
                        if start is None:
                            result = Result(24, 'refused', 'wrapper-refused-before-start', state, execution)
                        elif end is None:
                            raise ValueError('wrapper ended without child result')
                        elif rc != status(end['child_status']):
                            raise ValueError('wrapper/child status disagreement')
                        else:
                            state = 'FINISHED'
                            result = Result(end['child_status'], 'child-result', 'child-result', state, execution,
                                            child_status=end['child_status'], wrapper_status=rc)
                            publish(directory / 'end.json', dict(end, wrapper_status=rc), replace=True)
                        break
                    if start is None and now - queued >= policy.start_timeout:
                        result = Result(124, 'timeout', 'wrapper-start-timeout', state, execution)
                        break
                    if teardown_at is not None and now >= teardown_at:
                        raise ValueError('wrapper did not release after child completion')
                    sleep(poll_seconds)
    except (OSError, ValueError, TypeError, KeyError, subprocess.SubprocessError) as exc:
        result = Result(24, 'protocol-error' if child else 'launch-error', str(exc), state, execution)
    finally:
        if start is None: clock.revoke(execution)
        try:
            if child is not None:
                if cancelled is not None:
                    try: publish(directory / 'cancelled.json', dict(signal=cancelled))
                    except OSError: pass  # Termination and verification remain mandatory.
                    pg.signal_group(child, cancelled)
                    try: child.wait(timeout=grace_seconds)
                    except subprocess.TimeoutExpired: pass
                hold.cleanup(child)
            stopped = True
        except (OSError, subprocess.SubprocessError):
            result = Result(pg.uncertain_exit(result.code), 'cleanup-uncertain', result.reason, state, execution)
        finally:
            if hold is not None:
                try: hold.close(stopped=stopped)
                except OSError:
                    result = Result(pg.uncertain_exit(result.code), 'cleanup-uncertain', result.reason, state, execution)
            for sig, handler in previous.items(): signal.signal(sig, handler)
    if cancelled is not None:
        result = Result(130, 'cleanup-uncertain' if result.kind == 'cleanup-uncertain' else 'cancelled', 'cancelled', state, execution)
    return Result(result.code, result.kind, result.reason, result.state, execution,
                  str(directory) if directory is not None else None, result.child_status,
                  result.wrapper_status if result.wrapper_status is not None else child.returncode if child else None)


if __name__ == '__main__':
    if len(sys.argv) < 5 or sys.argv[1] != '--envelope' or sys.argv[3] != '--':
        sys.exit(24)
    sys.exit(envelope(Path(sys.argv[2]), sys.argv[4:]))
