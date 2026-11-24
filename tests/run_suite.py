"""Local full-suite barrier, bounded serial file reruns and file-level flake ledger."""
import argparse
from datetime import datetime, timezone
import fcntl
import json
import math
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import tempfile
import time
import uuid

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent))
from run_test_files import expected_seconds, read_weights, runner_os, timeout_override

ROOT = Path(__file__).resolve().parents[1]
GREEN = ('passed', 'passed_after_serial_rerun')
CANCEL_SIGNALS = {signal.SIGINT, signal.SIGTERM, signal.SIGHUP}


def atomic_json(path, value, *, before_replace=None):
    name = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', dir=path.parent, delete=False) as output:
            name = output.name
            json.dump(value, output, indent=2, allow_nan=False)
            output.write('\n')
            output.flush()
            os.fsync(output.fileno())
        if before_replace is not None:
            before_replace()
        os.replace(name, path)
        name = None  # The replace is the commit; do no fallible cleanup after it.
    finally:
        if name and os.path.exists(name):
            os.unlink(name)


def utc(seconds):
    return datetime.fromtimestamp(seconds, timezone.utc).isoformat()


def read_proposal(path):
    proposal = json.loads(path.read_text())
    if (not isinstance(proposal, dict) or not isinstance(proposal.get('id'), str)
            or re.fullmatch(r'[a-f0-9]{32}', proposal['id']) is None
            or proposal['id'] != path.stem or not isinstance(proposal.get('file'), str)
            or proposal.get('status') not in ('open', 'resolved')
            or any(not isinstance(proposal.get(key), list) or not proposal[key]
                   or not all(isinstance(value, str) for value in proposal[key])
                   for key in ('shas', 'run_tags'))
            or (proposal['status'] == 'open' and 'resolved_at' in proposal)):
        raise ValueError(f'malformed proposal: {path.name}')
    try:
        datetime.fromisoformat(proposal['created_at']).timestamp()
        if proposal['status'] == 'resolved':
            datetime.fromisoformat(proposal['resolved_at']).timestamp()
    except (KeyError, TypeError, ValueError, OverflowError) as error:
        raise ValueError(f'malformed proposal timestamp: {path.name}') from error
    return proposal


def ledger(state, result=None, *, resolve=None, now=None):
    """Serialize append/replay/reconciliation and explicit proposal resolution."""
    now = time.time() if now is None else now
    state.mkdir(parents=True, exist_ok=True)
    with (state / 'flakes.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        path = state / 'flakes.jsonl'
        data = path.read_bytes() if path.exists() else b''
        if data and not data.endswith(b'\n'):
            raise ValueError('malformed flake ledger: missing trailing newline')
        rows = [json.loads(line) for line in data.decode().splitlines()]
        for row in rows:
            if (row['identity_scope'] != 'file' or row['test_name'] != row['file']
                    or not all(isinstance(row[k], str) for k in ('file', 'sha', 'run_tag'))):
                raise ValueError('malformed flake ledger')
            datetime.fromisoformat(row['timestamp']).timestamp()
        proposals_dir = state / 'proposals'
        proposals = [read_proposal(p) for p in proposals_dir.glob('*.json')]
        proposals_dir.mkdir(exist_ok=True)
        if resolve is not None:
            proposal = next((p for p in proposals if p['id'] == resolve), None)
            if proposal is None:
                raise ValueError('unknown proposal')
            if proposal['status'] == 'open':
                proposal.update(status='resolved', resolved_at=utc(now))
                atomic_json(proposals_dir / (resolve + '.json'), proposal)
            return []
        ids = []
        for name in result['recovered']:
            if not any((r['file'], r['sha'], r['run_tag']) == (name, result['sha'], result['tag']) for r in rows):
                row = dict(file=name, test_name=name, identity_scope='file', sha=result['sha'],
                           timestamp=utc(now), run_tag=result['tag'])
                with path.open('a') as output:
                    output.write(json.dumps(row) + '\n')
                    output.flush()
                    os.fsync(output.fileno())
                rows.append(row)
            previous = [p for p in proposals if p['file'] == name]
            opened = next((p for p in previous if p['status'] == 'open'), None)
            if opened:
                ids.append(opened['id'])
                continue
            resolved = max((datetime.fromisoformat(p['resolved_at']).timestamp() for p in previous), default=-math.inf)
            earliest = {}
            for row in rows:
                stamp = datetime.fromisoformat(row['timestamp']).timestamp()
                if row['file'] == name and resolved < stamp <= now:
                    earliest[row['sha']] = min(stamp, earliest.get(row['sha'], stamp))
            shas = sorted(sha for sha, stamp in earliest.items() if now - 604800 <= stamp)
            if len(shas) >= 3:
                proposal = dict(id=uuid.uuid4().hex, status='open', file=name, shas=shas,
                                run_tags=sorted({r['run_tag'] for r in rows if r['file'] == name
                                    and r['sha'] in shas and resolved < datetime.fromisoformat(r['timestamp']).timestamp() <= now}),
                                created_at=utc(now))
                atomic_json(proposals_dir / (proposal['id'] + '.json'), proposal)
                proposals.append(proposal)
                ids.append(proposal['id'])
        return ids


def git(*args):
    return subprocess.check_output(['git', '-C', str(ROOT), *args], text=True, timeout=15).strip()


def integrity(sha):
    if git('rev-parse', 'HEAD') != sha or git('status', '--porcelain'):
        raise ValueError('HEAD changed or worktree is dirty')


def read_records(path, names, deadlines, code):
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    if len(rows) != len(names) or sorted(r['file'] for r in rows) != sorted(names):
        raise ValueError('incomplete, duplicate or unexpected file records')
    for row in rows:
        if (row['outcome'] not in ('PASS', 'FAIL', 'TIMEOUT') or row['cleanup_ok'] is not True
                or type(row['deadline']) is not int or row['deadline'] != deadlines[row['file']]
                or type(row['elapsed_seconds']) not in (int, float)
                or not math.isfinite(row['elapsed_seconds']) or row['elapsed_seconds'] < 0):
            raise ValueError('invalid outcome, deadline or cleanup evidence')
    if code not in (0, 1) or (code == 0) != all(r['outcome'] == 'PASS' for r in rows):
        raise ValueError('shard crashed, interrupted or exit disagrees with file records')
    return rows


class Suite:
    def __init__(self, args):
        self.args = args
        self.cancelled = False
        self.active = []
        self.result = dict(schema_version=1, state='invalid', sha=None, tag=args.tag,
                           project=args.project, shards=[], attempts={}, first_pass_failures=[],
                           rerun_logs={}, not_attempted=[], recovered={})

    def signal(self, signum, frame):
        self.cancelled = True

    def check_cancelled(self):
        if self.cancelled:
            raise KeyboardInterrupt

    def cancellation_pending(self):
        return self.cancelled or bool(signal.sigpending() & CANCEL_SIGNALS)

    def check_publication_cancelled(self):
        if self.cancellation_pending():
            raise KeyboardInterrupt

    def stop(self):
        # Signal runners, not their sessions: test groups belong to the runners.
        for child in self.active:
            if child.poll() is None:
                child.terminate()
        # stop_group's 1s/3s grace periods plus up to 20s inspection overrun
        # each total 44s; allow its masked cleanup to finish before killing it.
        end = time.monotonic() + 50
        for child in self.active:
            try:
                child.wait(timeout=max(.01, end - time.monotonic()))
            except subprocess.TimeoutExpired:
                child.kill()
                try:
                    child.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    self.result.update(state='cancelled' if self.cancelled else 'invalid',
                                       reason='runner did not terminate after kill')

    def launch(self, command, log, env, cwd):
        self.check_cancelled()
        with log.open('x') as output:
            child = subprocess.Popen(command, cwd=cwd, env=env, stdout=output,
                                     stderr=subprocess.STDOUT, start_new_session=True)
        self.active.append(child)
        return child

    def wait(self):
        while any(child.poll() is None for child in self.active):
            self.check_cancelled()
            time.sleep(.02)
        self.check_cancelled()

    def execute(self, scratch):
        result, args = self.result, self.args
        result['sha'] = git('rev-parse', 'HEAD')
        integrity(result['sha'])
        env = {**os.environ, 'PYTHONDONTWRITEBYTECODE': '1'}
        env.pop('BUREAU_TEST_RESULTS', None)
        env.pop('BUREAU_TEST_TIMINGS', None)

        def listing(extra):
            self.check_cancelled()
            return subprocess.check_output(['bash', 'tests/run.sh', '--list'], cwd=ROOT,
                                           env={**env, **extra}, text=True, timeout=30).splitlines()

        full = listing({})
        shards = [listing(dict(BUREAU_TEST_SHARD=f'{i}/{args.shards}')) for i in range(1, args.shards + 1)]
        flat = [name for names in shards for name in names]
        if (not full or not all(shards) or len(flat) != len(set(flat)) or sorted(flat) != sorted(full)
                or any(re.fullmatch(r'test_[A-Za-z0-9_.-]+\.sh', n) is None for n in full)):
            raise ValueError('shards are not a disjoint non-empty exhaustive partition')
        weights, os_name, override = read_weights(ROOT / 'tests/shard-weights.tsv'), runner_os(), timeout_override()
        estimates = {n: expected_seconds(n, weights, os_name) for n in full}
        deadlines = {n: override if override is not None else max(600, 3 * estimates[n]) for n in full}
        runners = []
        for i, names in enumerate(shards, 1):
            manifest, records = scratch / f's{i}.txt', scratch / f's{i}.jsonl'
            manifest.write_text('\n'.join(names) + '\n')
            log = args.out / f'suite-{args.tag}-s{i}.log'
            result['shards'].append(dict(files=names, log=str(log)))
            child = self.launch(['bash', 'tests/run.sh'], log,
                                {**env, 'BUREAU_TEST_MANIFEST': str(manifest), 'BUREAU_TEST_RESULTS': str(records)}, ROOT)
            runners.append((child, records, names))
        self.wait()  # Every shard has ended before validation or any serial rerun.
        integrity(result['sha'])
        for info, (child, records, names) in zip(result['shards'], runners):
            info['exit_code'] = child.returncode
            for row in read_records(records, names, deadlines, child.returncode):
                result['attempts'][row['file']] = [{**row, 'log': info['log'], 'phase': 'parallel'}]
        failures = sorted(n for n, attempts in result['attempts'].items() if attempts[0]['outcome'] != 'PASS')
        result['first_pass_failures'] = failures
        result['not_attempted'] = failures.copy()
        if len(failures) > 5 or sum(estimates[n] for n in failures) > 3600:
            result.update(state='failed', reason='serial rerun budget exceeded (5 files / 3600 expected seconds)')
            return
        for index, name in enumerate(failures):
            self.check_cancelled()
            integrity(result['sha'])
            records = scratch / f'rerun-{index}.jsonl'
            log = args.out / f'suite-{args.tag}-rerun-{name}.log'
            result['rerun_logs'][name] = str(log)
            child = self.launch([sys.executable, '-I', 'run_test_files.py', name], log,
                                {**env, 'BUREAU_TEST_RESULTS': str(records)}, ROOT / 'tests')
            result['not_attempted'].remove(name)
            self.wait()
            integrity(result['sha'])
            row, = read_records(records, [name], deadlines, child.returncode)
            result['attempts'][name].append({**row, 'log': str(log), 'phase': 'serial'})
            if row['outcome'] != 'PASS':
                result.update(state='failed', reason='serial rerun did not pass')
                return
            result['recovered'][name] = 'failed_in_parallel_passed_serially'
        self.check_cancelled()
        integrity(result['sha'])
        result['state'] = 'passed_after_serial_rerun' if failures else 'passed'

    def run(self):
        handlers = {s: signal.signal(s, self.signal) for s in CANCEL_SIGNALS}
        try:
            with tempfile.TemporaryDirectory(prefix=f'suite-{self.args.tag}-', dir=self.args.out) as directory:
                self.execute(Path(directory))
                self.check_cancelled()
                if self.result['recovered']:
                    integrity(self.result['sha'])
                    self.result['proposal_ids'] = ledger(self.args.state_dir, self.result)
        except KeyboardInterrupt:
            self.result.update(state='cancelled', reason='signal received')
        except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as error:
            self.result.update(state='invalid', reason=str(error))
        finally:
            self.stop()
        mask = signal.pthread_sigmask(signal.SIG_BLOCK, CANCEL_SIGNALS)
        path = self.args.out / f'suite-{self.args.tag}-result.json'
        try:
            try:
                self.check_publication_cancelled()
                if self.result['state'] in GREEN:
                    integrity(self.result['sha'])
                atomic_json(path, self.result, before_replace=self.check_publication_cancelled)
            except (KeyboardInterrupt, OSError, ValueError, TypeError, subprocess.SubprocessError) as error:
                cancelled = isinstance(error, KeyboardInterrupt) or self.cancellation_pending()
                self.result.update(state='cancelled' if cancelled else 'invalid',
                                   reason='signal received' if cancelled else str(error))
                try:
                    path.unlink(missing_ok=True)  # No stale green may survive a failed publication.
                    atomic_json(path, self.result)
                except (OSError, ValueError, TypeError) as failure:
                    try:
                        path.unlink(missing_ok=True)
                    except OSError as removal_error:
                        print(f'Cannot remove failed result: {removal_error}', file=sys.stderr)
                    print(f'Cannot publish suite result: {failure}', file=sys.stderr)
            # Successful replacement is final, including when unmasking delivers
            # a signal held across that commit. Exit only from the published state.
            code = (0 if self.result['state'] in GREEN else 130 if self.result['state'] == 'cancelled'
                    else 1 if self.result['state'] == 'failed' else 2)
        finally:
            signal.pthread_sigmask(signal.SIG_SETMASK, mask)
            for signum, handler in handlers.items():
                signal.signal(signum, handler)
        return code


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=Path)
    parser.add_argument('--tag')
    parser.add_argument('--project', required=True)
    parser.add_argument('--shards', type=int, default=2)
    parser.add_argument('--state-dir', type=Path)
    parser.add_argument('--resolve-proposal')
    args = parser.parse_args()
    if any(k in os.environ for k in ('CI', 'GITHUB_ACTIONS', 'BUREAU_TEST_SHARD', 'BUREAU_TEST_MANIFEST')):
        parser.error('local full suite only: CI, GITHUB_ACTIONS, shard and manifest must be unset')
    if re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*', args.project) is None:
        parser.error('project must be a plain key')
    base = Path(os.environ.get('XDG_STATE_HOME', str(Path.home() / '.local/state')))
    args.state_dir = (args.state_dir or base / 'bureau/suites' / args.project).resolve()
    if args.state_dir == ROOT or ROOT in args.state_dir.parents:
        parser.error('state directory must be outside the worktree')
    if args.resolve_proposal:
        if re.fullmatch(r'[a-f0-9]{32}', args.resolve_proposal) is None:
            parser.error('invalid proposal id')
        ledger(args.state_dir, resolve=args.resolve_proposal)
        return 0
    if args.out is None or not args.tag or args.shards < 1:
        parser.error('--out, --tag and positive --shards are required')
    if re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*', args.tag) is None:
        parser.error('tag must be a plain name')
    args.out = args.out.resolve()
    if args.out == ROOT or ROOT in args.out.parents:
        parser.error('--out must be outside the worktree')
    args.out.mkdir(parents=True, exist_ok=True)
    if list(args.out.glob(f'suite-{args.tag}-*')):
        parser.error('tag already used in this output directory')
    return Suite(args).run()


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as error:
        print(error, file=sys.stderr)
        sys.exit(2)
