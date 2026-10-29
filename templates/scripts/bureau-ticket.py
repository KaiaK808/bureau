#!/usr/bin/env python3
"""Authoritative ticket accounting and trusted progress; never provider prose."""
import argparse
from contextlib import contextmanager
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time

sys.dont_write_bytecode = True

# Closed terminal vocabulary. These are refinements of existing exits, not new exits.
STOP_CODES = dict(budget=25, timeout=124, watchdog=25, review_capped=25,
                  human_hold=25, ticket_withdrawn=25, stalled=25)
PROGRESS_KINDS = ('fulfilled_requirements', 'qualified_gates', 'confirmed_findings')


def diagnostic(message):
    try: os.write(2, ('bureau ticket: ' + message + '\n').encode())
    except OSError: pass  # A lost log reader cannot change the exit protocol.


def stop_code(reason, code=None):
    expected = STOP_CODES[reason]
    code = expected if code is None else code
    if type(code) is not int or code not in ((0, 25, 26) if reason == 'ticket_withdrawn' else (expected,)):
        raise ValueError('terminal reason does not match exit')
    return code


def settings(config):
    if not isinstance(config, dict) or not isinstance(config.get('agents', {}), dict):
        raise ValueError('agents must be an object')
    agents = config.get('agents', {})
    budget = agents.get('ticket_budget')
    progress = agents.get('ticket_progress')
    if budget is None: budget = {}
    if progress is None: progress = {}
    if not isinstance(budget, dict) or set(budget) - {'tokens', 'provider_calls', 'wall_seconds', 'allow_unknown_usage'}:
        raise ValueError('invalid agents.ticket_budget')
    limits = {}
    for key in ('tokens', 'provider_calls', 'wall_seconds'):
        value = budget.get(key)
        if value is not None and (type(value) not in (int, float) or type(value) is float and not math.isfinite(value) or value < 0
                                  or key != 'wall_seconds' and type(value) is not int):
            raise ValueError('invalid ticket budget ' + key)
        limits[key] = value or None
    if type(budget.get('allow_unknown_usage', False)) is not bool:
        raise ValueError('allow_unknown_usage must be boolean')
    limits['allow_unknown_usage'] = budget.get('allow_unknown_usage', False)
    if not isinstance(progress, dict) or set(progress) - {'enabled', 'max_non_improving_turns', 'contract'}:
        raise ValueError('invalid agents.ticket_progress')
    enabled, maximum = progress.get('enabled', False), progress.get('max_non_improving_turns', 2)
    if type(enabled) is not bool or type(maximum) is not int or not 1 <= maximum <= 100000:
        raise ValueError('invalid ticket progress policy')
    contract = progress.get('contract', {})
    if not isinstance(contract, dict) or set(contract) - set(PROGRESS_KINDS):
        raise ValueError('invalid ticket progress contract')
    for kind, facts in contract.items():
        if not isinstance(facts, dict): raise ValueError('invalid progress facts')
        for key, gate in facts.items():
            if not re.fullmatch(r'[A-Za-z0-9_.:-]{1,128}', key): raise ValueError('invalid progress identity')
            if (not isinstance(gate, dict) or set(gate) != {'command_id', 'command'}
                    or gate['command_id'] not in ('repo.test_command', 'qa.test')
                    or not isinstance(gate['command'], str) or not gate['command'].strip()):
                raise ValueError('progress needs an exact trusted gate command')
    return limits, dict(enabled=enabled, max_non_improving_turns=maximum, contract=contract)


def root_for(repo):
    # No credentials are needed by this local Git query, including its hooks.
    env = {k: v for k, v in os.environ.items() if k not in
           ('LINEAR_API_KEY', 'API_KEY', 'GH_TOKEN', 'GITHUB_TOKEN', 'GH_ENTERPRISE_TOKEN',
            'GITHUB_ENTERPRISE_TOKEN', 'TELEGRAM_BOT_TOKEN', 'TELEGRAM_ALERT_CHAT_ID', 'BASH_ENV', 'ENV')}
    value = subprocess.check_output(['git', '-C', str(repo), 'rev-parse', '--git-common-dir'],
                                    text=True, env=env).strip()
    return (Path(repo) / value).resolve() / 'bureau'


def read_config(repo):
    path = Path(os.environ['BUREAU_CONFIG']) if os.environ.get('BUREAU_CONFIG') else root_for(repo).parent.parent / '.bureau.json'
    return json.loads(path.read_text()) if path.exists() else {}


def atomic(path, value):
    fd, name = tempfile.mkstemp(prefix='.' + path.name, dir=str(path.parent))
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(value, stream, sort_keys=True, allow_nan=False)
            stream.flush(); os.fsync(stream.fileno())
        os.replace(name, path)
        directory = os.open(str(path.parent), os.O_RDONLY)
        try: os.fsync(directory)
        finally: os.close(directory)
    finally:
        if os.path.exists(name): os.unlink(name)


def token_count(usage, provider):
    """Claude caches are disjoint input; Codex cached input is already included."""
    fields = ('input_tokens', 'output_tokens')
    if provider == 'claude': fields += ('cache_read_input_tokens', 'cache_creation_input_tokens')
    if not isinstance(usage, dict) or any(type(usage.get(k)) is not int or usage[k] < 0 for k in fields):
        return None
    return sum(usage[k] for k in fields)


def reported_usage(provider, directory):
    """Read actual provider output even on invalid results, timeouts and errors."""
    try:
        raw = (Path(directory) / 'stdout.log').read_text()
        events = []
        try: events = [json.loads(raw)]
        except ValueError:
            for line in raw.splitlines():
                try: events.append(json.loads(line))
                except ValueError: pass
        if provider == 'claude':
            results = [v for v in events if isinstance(v, dict) and v.get('type') in (None, 'result') and 'usage' in v]
            return results[-1].get('usage') if results else None
        turns = [v.get('usage') for v in events if isinstance(v, dict) and v.get('type') == 'turn.completed']
        if not turns or any(token_count(v, provider) is None for v in turns): return None
        return {key: sum(v[key] for v in turns) for key in ('input_tokens', 'output_tokens')}
    except (OSError, ValueError, TypeError): return None


class Ledger:
    def __init__(self, root, issue, config, clock=time.time):
        if not re.fullmatch(r'[A-Z][A-Z0-9_]*-\d+', issue): raise ValueError('invalid ticket identity')
        self.root, self.issue, self.clock = Path(root) / 'tickets', issue, clock
        self.budget, self.progress = settings(config)
        self.contract_digest = hashlib.sha256(json.dumps(self.progress['contract'],sort_keys=True).encode()).hexdigest()
        self.required = any(self.budget[k] for k in ('tokens', 'provider_calls', 'wall_seconds')) or self.progress['enabled']
        self.path = self.root / (issue + '.json')

    @contextmanager
    def guard(self):
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        with os.fdopen(os.open(str(self.root / (self.issue + '.guard')), os.O_CREAT | os.O_RDWR, 0o600), 'a') as lock:
            deadline = time.monotonic() + 5
            while True:
                try: fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB); break
                except BlockingIOError:
                    if time.monotonic() >= deadline: raise OSError('ticket accounting lock unavailable')
                    time.sleep(.01)
            value = json.loads(self.path.read_text()) if self.path.exists() else dict(
                version=1, issue=self.issue, attempts={}, facts={}, turns={}, non_improving_turns=0,
                progress_seen=[], terminal_reason=None, last_stop=None)
            if (value.get('version') != 1 or value.get('issue') != self.issue
                    or not isinstance(value.get('attempts'), dict)
                    or value.get('terminal_reason') not in (None, *STOP_CODES)):
                raise ValueError('invalid ticket ledger; inspect before resuming')
            for row in value['attempts'].values():
                if (not isinstance(row, dict) or type(row.get('started_at')) not in (int, float)
                        or not math.isfinite(row['started_at']) or row['started_at'] < 0
                        or row.get('ended_at') is not None and (type(row['ended_at']) not in (int, float)
                            or not math.isfinite(row['ended_at']) or row['ended_at'] < row['started_at'])
                        or row.get('tokens') is not None and (type(row['tokens']) is not int or row['tokens'] < 0)):
                    raise ValueError('invalid ticket attempt measurements')
            if type(value.get('non_improving_turns')) is not int or value['non_improving_turns'] < 0:
                raise ValueError('invalid progress counter')
            yield value

    def call(self, method, *args, **kwargs):
        # Disabled policies preserve the existing optional-evidence behavior.
        try: return getattr(self, method)(*args, **kwargs)
        except (OSError, ValueError, KeyError, TypeError):
            if self.required: raise
            diagnostic('WARN: optional accounting unavailable')
            return None

    def totals(self, value):
        now = self.clock()
        attempts = list(value['attempts'].values())
        # Open calls have not reported usage yet. Only a live adapter with its
        # recorded birth identity may defer that unknown to its completion.
        live = {}
        if any(v['ended_at'] is None for v in attempts):
            import importlib.util
            spec = importlib.util.spec_from_file_location('ticket_activity', Path(__file__).resolve().with_name('bureau-activity.py'))
            activity = importlib.util.module_from_spec(spec); spec.loader.exec_module(activity)
            live = activity.ProcessTable().snapshot()
        def pending(row):
            process = live.get(row.get('pid'))
            return row['ended_at'] is None and process and not process[2] and str(process[1]) == row.get('birth')
        unknown = sum(v['tokens'] is None and not pending(v) for v in attempts)
        known = sum(v['tokens'] for v in attempts if v['tokens'] is not None)
        return dict(provider_calls=len(attempts), tokens=None if any(v['tokens'] is None for v in attempts) else known, tokens_known=known,
                    unknown_usage=unknown, wall_seconds=sum(max(0, (v['ended_at'] if v['ended_at'] is not None else now) - v['started_at']) for v in attempts))

    def decision(self, value):
        totals = self.totals(value)
        for key in ('tokens', 'provider_calls', 'wall_seconds'):
            limit = self.budget[key]
            measured = totals['tokens_known'] if key == 'tokens' else totals[key]
            if limit and (measured >= limit or key == 'tokens' and totals['unknown_usage'] and not self.budget['allow_unknown_usage']):
                unknown = key == 'tokens' and totals['unknown_usage'] and not self.budget['allow_unknown_usage']
                return dict(terminal_reason='budget', code=25, dimension=key, usage_unknown=bool(unknown), totals=totals)
        if self.progress['enabled'] and value['non_improving_turns'] >= self.progress['max_non_improving_turns']:
            return dict(terminal_reason='stalled', code=25, non_improving_turns=value['non_improving_turns'], totals=totals)
        return None

    def _stop(self, value, reason, code, run=None, detail=None):
        stop_code(reason, code)
        value.update(terminal_reason=reason, last_stop=dict(terminal_reason=reason, code=code, run_id=run,
                                                          at=self.clock(), detail=detail))

    def check(self, run=None):
        if not self.required: return None
        with self.guard() as value:
            decision = self.decision(value)
            if decision:
                self._stop(value, decision['terminal_reason'], decision['code'], run, decision)
                atomic(self.path, value)
            return decision

    def begin(self, identity, provider):
        if provider not in ('claude', 'codex'): raise ValueError('invalid provider')
        with self.guard() as value:
            decision = self.decision(value)
            if decision:
                self._stop(value, decision['terminal_reason'], 25, identity['run_id'], decision)
                atomic(self.path, value); return decision
            attempt = identity['provider_attempt_id']
            if attempt in value['attempts']: raise ValueError('provider attempt reused')
            # Birth identity is used only to recognize live accounting, never to signal a PID.
            import importlib.util
            spec = importlib.util.spec_from_file_location('ticket_activity', Path(__file__).resolve().with_name('bureau-activity.py'))
            activity = importlib.util.module_from_spec(spec); spec.loader.exec_module(activity)
            process = activity.ProcessTable().snapshot().get(os.getpid())
            value['attempts'][attempt] = dict(**identity, provider=provider, started_at=self.clock(), ended_at=None,
                                             tokens=None, normalized_exit=None, pid=os.getpid(), birth=str(process[1]) if process else None)
            atomic(self.path, value)
            return None

    def finish(self, attempt, usage, code):
        with self.guard() as value:
            row = value['attempts'][attempt]
            if row['ended_at'] is None:
                row.update(ended_at=self.clock(), tokens=token_count(usage, row['provider']), normalized_exit=code)
                atomic(self.path, value)

    def note_stop(self, reason, code, run=None):
        with self.guard() as value:
            # An originating cap/watchdog is more specific than its later generic hold.
            prior = value['last_stop']
            if prior and prior['run_id'] == run and prior['code'] == code and reason in ('human_hold', prior['terminal_reason']): return
            self._stop(value, reason, code, run)
            atomic(self.path, value)

    def terminal(self, code, run=None):
        if code not in (0, 25, 26, 124): return None
        with self.guard() as value:
            prior = value['last_stop']
            if prior and prior['run_id'] == run and prior['code'] == code: return prior['terminal_reason']
            reason = {124: 'timeout', 25: 'human_hold', 26: 'ticket_withdrawn'}.get(code)
            if reason:
                self._stop(value, reason, code, run)
                atomic(self.path, value)
            return reason

    def gate(self, run, turn, command_id, command, code, start, end, path):
        if not self.progress['enabled'] or not run or not turn or code != 0 or not start or start != end: return
        # This API is called only by the trusted outer shell / outside executor.
        # A provider's receipt, declared status, checkbox or commit never calls it.
        proof = hashlib.sha256(Path(path).read_bytes()).hexdigest()
        with self.guard() as value:
            for kind, entries in self.progress['contract'].items():
                for key, expected in entries.items():
                    if expected == dict(command_id=command_id, command=command):
                        fact = self.contract_digest + ':' + kind + ':' + key
                        value['facts'].setdefault(fact, dict(run_id=run, stage_attempt_id=turn, head=end, sha256=proof,
                                                           contract_sha256=self.contract_digest))
            atomic(self.path, value)

    def end_turn(self, turn, run):
        if not self.progress['enabled']: return None
        with self.guard() as value:
            if turn in value['turns']: return self.decision(value)
            # Stable contract identities count once, even if a new HEAD or a
            # fresh provider replays the same successful gate indefinitely.
            facts = {key for key, fact in value['facts'].items() if fact['contract_sha256'] == self.contract_digest}
            improving = bool(facts - set(value['progress_seen']))
            value['non_improving_turns'] = 0 if improving else value['non_improving_turns'] + 1
            value['progress_seen'] = sorted(facts)
            value['turns'][turn] = dict(run_id=run, improving=improving, at=self.clock())
            decision = self.decision(value)
            if decision: self._stop(value, decision['terminal_reason'], 25, run, decision)
            atomic(self.path, value)
            return decision


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo', default='.')
    parser.add_argument('--issue', required=True)
    sub = parser.add_subparsers(dest='action', required=True)
    sub.add_parser('check'); sub.add_parser('status')
    stop = sub.add_parser('stop'); stop.add_argument('reason', choices=STOP_CODES); stop.add_argument('code', type=int)
    gate = sub.add_parser('gate')
    for name in ('path', 'head', 'end-head', 'command', 'command-id'): gate.add_argument('--' + name, required=True)
    gate.add_argument('--exit-code', type=int, required=True)
    args = parser.parse_args()
    try:
        repo = Path(args.repo).resolve(); ledger = Ledger(root_for(repo), args.issue, read_config(repo))
        run = os.environ.get('BUREAU_RUN_ID')
        if args.action == 'check':
            decision = ledger.call('check', run)
            if decision: print(json.dumps(decision)); return decision['code']
        elif args.action == 'status':
            with ledger.guard() as value: print(json.dumps(dict(value, totals=ledger.totals(value))))
        elif args.action == 'stop': ledger.call('note_stop', args.reason, args.code, run)
        elif args.action == 'gate':
            ledger.call('gate', run, os.environ.get('BUREAU_STAGE_ATTEMPT_ID'), args.command_id, args.command,
                        args.exit_code, args.head, args.end_head, args.path)
        return 0
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as exc:
        diagnostic(str(exc)); return 24


if __name__ == '__main__': sys.exit(main())
