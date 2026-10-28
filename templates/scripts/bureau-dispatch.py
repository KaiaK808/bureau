#!/usr/bin/env python3
"""Continuously refill owned ticket lanes using Bureau's trusted shell policy."""
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
import time
import uuid

sys.dont_write_bytecode = True
SCRIPTS = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('dispatch_runtime', SCRIPTS / 'bureau-runtime.py')
r = importlib.util.module_from_spec(spec)
spec.loader.exec_module(r)

# Every policy read uses the installed runtime, never a ticket's scripts.
SOURCE = '''source "$1/bureau-config.sh"
if [ -f "${BUREAU_ENV_FILE:-}" ]; then bureau_load_env --export "$BUREAU_ENV_FILE"; fi
unset BUREAU_FORCE_ALL_AGENTS
shift
bureau_secret_copy --optional API_KEY LINEAR_API_KEY
'''
PICK = '''skip="$1"; fallback=""
for row in merge:merge code_review:code-review qa:qa implement:implement copy:copy ux:ux spec_review:spec-review spec:spec; do
  stage="${row%%:*}"; pipeline="${row#*:}-pipeline.sh"
  agent_enabled "$stage" || continue
  picked=$(pipeline_pick_next "$pipeline" "$skip" mark-held) || exit $?
  [ -n "$picked" ] || continue
  case "$picked" in
    *' held') [ -n "$fallback" ] || fallback="${picked% held} $stage" ;;
    *) printf '%s %s' "$picked" "$stage"; exit 0 ;;
  esac
done
printf '%s' "$fallback"
'''
PREFLIGHT = '''issue="$1"; stage="$2"
bureau_ticket_checkpoint "$issue" strict
if bureau_is_paused; then printf paused; exit 25; fi
agent_enabled "$stage" || exit 20
held=$(bureau_human_hold "$issue") || exit $?
[ -z "$held" ] || exit 25
case "$stage" in
  spec) spec_admission_gate >&2 ;;
  merge|rebase) ;;
  *) BUREAU_THROTTLE_ONCE=1 session_throttle_guard "$stage" ;;
esac
if [ "$stage" = spec ]; then BUREAU_THROTTLE_ONCE=1 session_throttle_guard spec; fi
bureau_ticket_authorize "$issue" strict
'''


class Stop(Exception):
    def __init__(self, code, output=''):
        self.code = code
        self.output = output


class Helpers:
    """Track only handles created here; cancellation never reads stored PIDs."""
    def __init__(self, repo):
        self.repo = repo
        self.stopping = False
        self.child = None

    def stop(self):
        self.stopping = True
        if self.child is not None and self.child.poll() is None:
            r.signal_group(self.child, signal.SIGTERM)

    def call(self, body, *args, env=None):
        if self.stopping:
            raise Stop(130)
        environ = dict(os.environ if env is None else env)
        for key in ('BASH_ENV', 'ENV'):
            environ.pop(key, None)
        command = ['bash', '-e', '-c', SOURCE + body, 'bureau-dispatch', str(SCRIPTS), *args]
        child = subprocess.Popen(command, cwd=self.repo, env=environ, text=True,
                                 stdout=subprocess.PIPE, start_new_session=True)
        self.child = child
        # A signal can arrive in Popen before its handle has been assigned.
        if self.stopping:
            self.stop()
        try:
            deadline = None
            while True:
                try:
                    output, _ = child.communicate(timeout=.2)
                    break
                except subprocess.TimeoutExpired:
                    if self.stopping:
                        deadline = deadline or time.monotonic() + 5
                        if time.monotonic() >= deadline:
                            r.signal_group(child, signal.SIGKILL)
        finally:
            self.child = None
        if self.stopping:
            raise Stop(130)
        if child.returncode:
            raise Stop(130 if child.returncode < 0 else child.returncode, output.strip())
        return output.strip()


@contextmanager
def locked(path, shared=False, nonblocking=False, cancelled=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a') as handle:
        mode = fcntl.LOCK_SH if shared else fcntl.LOCK_EX
        while True:
            if cancelled and cancelled(): raise Stop(130)
            try:
                fcntl.flock(handle, mode | (fcntl.LOCK_NB if nonblocking or cancelled else 0))
                break
            except BlockingIOError:
                if nonblocking: raise
                time.sleep(.05)
        yield handle


def spec_start(repo, issue):
    """Serialize count + reservation + Spec move, including lagging snapshots.

    Reservations count until Linear reflects the move or execution ownership
    ends. Interrupted leases continue counting until explicit reconciliation.
    This lock is separate from runtime/admission locks; no runtime lock is
    held across a shell helper or Linear call.
    """
    store = r.Store(repo)
    helpers = Helpers(repo)
    signal.signal(signal.SIGTERM, lambda *_: helpers.stop())
    signal.signal(signal.SIGINT, lambda *_: helpers.stop())
    signal.signal(signal.SIGHUP, lambda *_: helpers.stop())
    path = store.root / 'spec-admissions.json'
    with locked(store.root / 'spec-admissions.guard', cancelled=lambda: helpers.stopping):
        if helpers.call('bureau_ticket_authorize "$1" strict', issue) != 'authorized':
            return 0
        cap = int(helpers.call('printf %s "$BUREAU_MAX_CONCURRENT_ISSUES"'))
        pending = r.read(path, {})
        with store.guard():
            leases = r.read(store.leases, {})
        snapshot = json.loads(helpers.call('spec_admission_snapshot')) if cap else dict(issues=[])
        counted = {row['identifier'] for row in snapshot['issues']}
        pending = {key: run for key, run in pending.items()
                   if key not in counted and leases.get('issue:' + key, {}).get('run_id') == run}
        if cap and issue not in counted and issue not in pending and len(counted | pending.keys()) >= cap:
            r.save(path, pending)
            print('Spec gate: atomic admission waits for capacity', file=sys.stderr)
            return 28
        run = os.environ.get('BUREAU_RUN_ID')
        if cap:
            if not run or leases.get('issue:' + issue, {}).get('run_id') != run:
                return 21
            pending[issue] = run
            r.save(path, pending)  # durable before the mutation, not an evidence record
        environ = {**os.environ, 'BUREAU_SPEC_ADMISSION_LOCKED': '1'}
        helpers.call('bureau_ticket_checkpoint "$1" strict\n'
                     'if bureau_is_paused; then\n'
                     '  [ -z "${BUREAU_DISPATCH_READY_FILE:-}" ] || : > "${BUREAU_DISPATCH_READY_FILE}.paused"\n'
                     '  exit 25\nfi\n'
                     'move_issue "$1" "$BUREAU_STATE_SPEC"', issue, env=environ)
        return 0


class Dispatcher:
    def __init__(self, repo, args, lock_fd):
        self.repo, self.args = repo, args
        self.lock_fd = lock_fd
        self.store = r.Store(repo)
        self.helpers = Helpers(repo)
        self.lanes = {}
        self.ready = {}
        self.path = self.store.root / 'dispatch-skips.json'
        self.journal = self.store.root / 'dispatch-lanes'
        self.skips = r.read(self.path, {})
        self.stopping = False
        self.poll = args.poll_seconds

    def stop(self, *_):
        self.stopping = True
        self.helpers.stop()
        for child, _, _ in list(self.lanes.values()):
            if child.poll() is None:
                try: child.send_signal(signal.SIGTERM)
                except ProcessLookupError: pass

    def defer(self, issue, code, paused=False, attempt=None):
        prior = self.skips.get(issue, {})
        count = min(prior.get('attempts', 0) + 1, 16)
        quiet = code in (2, 10, 16, 23, 28, 29)
        due = time.time() + min(self.poll * 2 ** (count - 1), self.recheck) if quiet or paused else None
        self.skips[issue] = dict(code=code, attempts=count, until=due)
        if attempt is not None: self.skips[issue]['attempt'] = attempt
        r.save(self.path, self.skips)
        if code not in (0, 2, 10, 16, 20, 23, 25, 26, 28, 29, 130):
            try:
                self.helpers.call('alert_telegram "$1" bureau-dispatch.py "$2" "dispatcher lane halted" || true\n'
                                  'if [ "$2" != 21 ]; then mark_needs_human "$1" dispatcher "$2" || true; fi', issue, str(code))
            except Stop:
                if self.stopping: raise
        print(f'dispatch: {issue} exit={code}; ' + ('deferred' if due is not None else 'held (dispatch-skips.json)'), flush=True)

    def reconcile_lane(self, path):
        row = r.read(path)
        attempt, issue = row['attempt'], row['issue']
        if path.stem != attempt or not re.fullmatch(r'[a-f0-9]{32}', attempt) or not re.fullmatch(r'[A-Z][A-Z0-9]*-\d+', issue):
            raise ValueError('invalid dispatch launch identity: ' + str(path))
        workspace = (self.repo / '.worktrees' / ('dispatch-' + issue + '-' + attempt)).resolve()
        if row['workspace'] != str(workspace): raise ValueError('dispatch workspace identity changed: ' + str(path))
        code = row.get('code')
        # An exclusive dispatch lock means no inherited lane is still running.
        # A missing terminal outcome is unfinished work, never permission to retry.
        if code is None: code = 130
        if type(code) is not int or not 0 <= code <= 255: raise ValueError('invalid dispatch outcome: ' + str(path))
        if self.skips.get(issue, {}).get('attempt') != attempt:
            self.defer(issue, code, paused=code == 25 and row.get('paused') is True, attempt=attempt)
        ready = self.store.root / 'dispatch-ready' / attempt
        ready.unlink(missing_ok=True)
        ready.with_suffix('.paused').unlink(missing_ok=True)
        if code != 130: self.clean_worktree(workspace)
        # Saving the skip precedes acknowledgment; a crash between the two is
        # reconciled idempotently by the attempt identity in dispatch-skips.json.
        path.unlink()

    def exclusions(self):
        with self.store.guard():
            leases = r.read(self.store.leases, {})
        held = {key[6:] for key in leases if key.startswith('issue:')}
        # A preview must also skip unreconciled launches without acknowledging
        # them or mutating the skip store. Active lanes are already excluded.
        pending = {r.read(path)['issue'] for path in self.journal.glob('*.json')}
        now = time.time()
        return held | pending | set(self.lanes) | {issue for issue, row in self.skips.items()
                                         if row['until'] is None or row['until'] > now}

    def clean_worktree(self, workspace):
        # Keep every interruption or unfinished checkpoint. Only a clean,
        # detached, identity-matching disposable worker may be removed.
        if not workspace.exists(): return
        key = hashlib.sha256(str(workspace.resolve()).encode()).hexdigest()
        registry = self.store.root / 'workers' / key
        if not registry.is_file() or (self.store.root / 'preserved' / (key + '.json')).exists(): return
        with self.store.guard():
            if 'workspace:' + str(workspace) in r.read(self.store.leases, {}): return
        try:
            if r.git(workspace, 'rev-parse', '--absolute-git-dir') != registry.read_text().strip(): return
            if r.git(workspace, 'status', '--porcelain') or r.git(workspace, 'branch', '--show-current'): return
            r.git(self.repo, 'worktree', 'remove', str(workspace))
            registry.unlink(missing_ok=True)
        except (OSError, subprocess.CalledProcessError):
            print('dispatch: retaining worktree ' + str(workspace), file=sys.stderr)

    def reap(self):
        for issue, (child, workspace, output) in list(self.lanes.items()):
            code = child.poll()
            if code is None: continue
            child.wait()
            output.close()
            del self.lanes[issue]
            ready = self.ready.pop(issue)
            self.reconcile_lane(self.journal / (ready.name + '.json'))

    def launch(self, issue, stage):
        # One final authoritative shell read, then runtime admission and atomic
        # ownership precede *all* worker creation/reset. Done's exit 0 is not
        # authorization: only the literal acknowledgment permits a launch.
        if self.helpers.call(PREFLIGHT, issue, stage) != 'authorized':
            self.defer(issue, 0)
            return
        if self.stopping or (self.store.root / 'paused').is_file(): return
        if issue in self.exclusions(): return
        attempt = uuid.uuid4().hex
        workspace = (self.repo / '.worktrees' / ('dispatch-' + issue + '-' + attempt)).resolve()
        logs = self.store.root / 'dispatch-logs'
        logs.mkdir(parents=True, exist_ok=True)
        output = (logs / (issue + '-' + attempt + '.log')).open('a')
        env = dict(os.environ)
        for key in ('BUREAU_RUN_ID', 'BUREAU_RUN_DEPTH', 'BUREAU_ACTIVE_ENTRY', 'BUREAU_STAGE_ATTEMPT_ID',
                    'BUREAU_CURRENT_ISSUE', 'BUREAU_TICKET_CONTROL_DIR', 'BUREAU_FORCE_ALL_AGENTS',
                    'BUREAU_LANE_BASE_REFRESHES', 'BASH_ENV', 'ENV'):
            env.pop(key, None)
        env.update(BUREAU_DISPATCH_LANE='1', BUREAU_THROTTLE_ONCE='1')
        ready = self.store.root / 'dispatch-ready' / attempt
        ready.parent.mkdir(parents=True, exist_ok=True)
        env['BUREAU_DISPATCH_READY_FILE'] = str(ready)
        env['BUREAU_DISPATCH_ATTEMPT_ID'] = attempt
        # The runtime updates this launch with its own run identity and outcome,
        # even if this dispatcher disappears before it can reap the lane.
        r.save(self.journal / (attempt + '.json'), dict(attempt=attempt, issue=issue, stage=stage,
                                                     workspace=str(workspace), code=None))
        shepherd = SCRIPTS / 'shepherd.sh'
        command = [sys.executable, '-I', str(SCRIPTS / 'bureau-runtime.py'), '--repo', str(self.repo),
                   'exec', '--issue', issue, '--workspace', str(workspace), '--entry', str(shepherd),
                   '--', 'bash', str(shepherd), '--no-tmux', '--respect-config', '--worktree', str(workspace), issue]
        try:
            child = subprocess.Popen(command, cwd=self.repo, env=env, stdout=output,
                                     stderr=subprocess.STDOUT, start_new_session=True, pass_fds=(self.lock_fd,))
        except BaseException:
            output.close()
            raise
        self.lanes[issue] = child, workspace, output
        self.ready[issue] = ready
        if self.stopping: self.stop()
        print(f'dispatch: launched {issue} ({stage}), {len(self.lanes)}/{self.limit} lanes', flush=True)

    def run(self, cancelled=None):
        for signum in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP): signal.signal(signum, self.stop)
        # Consume startup_stop after installing the replacement handlers. A
        # signal during Store/common-directory construction must not be lost.
        if cancelled and cancelled(): self.stop()
        if self.stopping: return 130
        settings = self.helpers.call('''if [ "$(bureau_get '.agents.file_overlap_gate | tojson')" != true ]; then
  echo 'dispatch: agents.file_overlap_gate must be explicitly true, including with one lane' >&2
  exit 24
fi
printf "%s %s %s %s" "$BUREAU_PARALLEL_LANES" "$BUREAU_POLL_INTERVAL" '''
                                     '"$(_merge_gate_number merge_gate_recheck_seconds 3600)" "${BUREAU_DRY_RUN:-0}"').split()
        raw = self.args.max_concurrent if self.args.max_concurrent is not None else os.environ.get('BUREAU_MAX_CONCURRENT', settings[0])
        self.limit = int(raw)
        if not 1 <= self.limit <= 1000: raise ValueError('lane override must be between 1 and 1000')
        self.poll = self.poll if self.poll is not None else int(settings[1]) * 60
        self.recheck = max(self.poll, int(settings[2]))
        if self.args.dry_run or settings[3] == '1':
            picked = self.helpers.call(PICK, ','.join(sorted(self.exclusions())))
            print('[DRY_RUN] dispatch candidate: ' + (picked or 'queue empty'), flush=True)
            return 0
        try:
            for path in sorted(self.journal.glob('*.json')):
                self.reconcile_lane(path)
            while not self.stopping:
                self.reap()
                if (self.store.root / 'paused').is_file():
                    time.sleep(min(self.poll, .2))
                    continue
                attempted = self.exclusions()
                while len(self.lanes) < self.limit and not self.stopping:
                    # Settle startup (including shepherd-focused and the first
                    # worker's admission) before another candidate snapshots
                    # the queue. Paid work still runs concurrently.
                    if any(not self.ready[issue].exists() for issue in self.lanes): break
                    try:
                        picked = self.helpers.call(PICK, ','.join(sorted(attempted)))
                    except Stop as exc:
                        if exc.code == 130: raise
                        self.helpers.call('alert_telegram none bureau-dispatch.py "$1" "dispatcher pick failed" || true', str(exc.code))
                        break
                    if not picked: break
                    issue, stage = picked.split()
                    if not re.fullmatch(r'[A-Z][A-Z0-9]*-\d+', issue): raise ValueError('invalid picker identifier')
                    if issue in attempted: break  # defensive: even a broken picker cannot duplicate a lane
                    attempted.add(issue)
                    try:
                        self.launch(issue, stage)
                    except Stop as exc:
                        if exc.code == 130: raise
                        self.defer(issue, exc.code, paused=exc.code == 25 and exc.output == 'paused')
                # Reap and refill as soon as a lane ends, independently of the
                # idle poll interval. Idle arrivals are discovered next poll.
                deadline = time.monotonic() + self.poll
                awaiting = [path for path in self.ready.values() if not path.exists()]
                while not self.stopping and time.monotonic() < deadline:
                    if any(child.poll() is not None for child, _, _ in self.lanes.values()): break
                    if any(path.exists() for path in awaiting): break
                    time.sleep(min(.1, max(0, deadline - time.monotonic())))
        finally:
            self.stop()
            # Runtime owns nested shutdown/grace and preservation. Do not kill
            # its wrapper early or consult process records from an old run.
            for child, _, output in self.lanes.values():
                child.wait()
                output.close()
        return 130


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo', default='.')
    sub = parser.add_subparsers(dest='action', required=True)
    run = sub.add_parser('run')
    run.add_argument('--poll-seconds', type=float)
    run.add_argument('--max-concurrent', type=int, help='compatibility override of agents.parallel_lanes')
    run.add_argument('--dry-run', action='store_true', help='print one eligible candidate without launching')
    sub.add_parser('wait', help='wait cancellably for the dispatcher/poller lock without launching work')
    start = sub.add_parser('spec-start'); start.add_argument('issue')
    poller = sub.add_parser('poller'); poller.add_argument('command', nargs=argparse.REMAINDER)
    args = parser.parse_args()
    stopped = False
    def startup_stop(*_):
        nonlocal stopped
        stopped = True
    for signum in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP): signal.signal(signum, startup_stop)
    try:
        repo = r.root_for(Path(args.repo).resolve())
        if stopped: return 130
        if args.action == 'spec-start': return spec_start(repo, args.issue)
        if args.action == 'run':
            for key in ('BUREAU_RUN_ID', 'BUREAU_RUN_DEPTH', 'BUREAU_ACTIVE_ENTRY', 'BUREAU_STAGE_ATTEMPT_ID',
                        'BUREAU_CURRENT_ISSUE', 'BUREAU_TICKET_CONTROL_DIR', 'BUREAU_FORCE_ALL_AGENTS'):
                os.environ.pop(key, None)
        if args.action == 'run' and args.poll_seconds is not None and not .05 <= args.poll_seconds <= 604800:
            raise ValueError('poll seconds must be between .05 and 604800')
        # Legacy stage pollers may coexist with one another, but never with a
        # continuous dispatcher. The FD is held across exec, not a PID marker.
        with locked(r.common_for(repo) / 'bureau' / 'dispatch.lock', shared=args.action == 'poller',
                    nonblocking=args.action != 'wait', cancelled=lambda: stopped) as lock:
            if stopped: return 130
            if args.action == 'wait': return 0
            if args.action == 'poller':
                command = args.command[1:] if args.command[:1] == ['--'] else args.command
                if not command: raise ValueError('poller requires a command')
                os.set_inheritable(lock.fileno(), True)
                os.execvpe(command[0], command, {**os.environ, 'BUREAU_POLLER_LOCKED': '1'})
            return Dispatcher(repo, args, lock.fileno()).run(cancelled=lambda: stopped)
    except Stop as exc:
        return exc.code
    except BlockingIOError:
        print('dispatch: another dispatcher or stage poller owns this common Git directory', file=sys.stderr)
        return 21
    except (ValueError, OSError, KeyError, subprocess.CalledProcessError) as exc:
        print('dispatch: ' + str(exc), file=sys.stderr)
        return 24


if __name__ == '__main__':
    sys.exit(main())
