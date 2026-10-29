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
GATE_IDS = ('repo.test_command', 'qa.test', 'review.build')


def completed_work_turn(code):
    # Success and a completed red build are work. Waits, holds, infrastructure
    # faults and incomplete provider results do not spend the progress allowance.
    return code in (0, 14)


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
                    or gate['command_id'] not in GATE_IDS
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


def token_count(usage, provider, partial=False):
    """Claude caches are disjoint input; Codex cached input is already included."""
    fields = ('input_tokens', 'output_tokens')
    if provider == 'claude': fields += ('cache_read_input_tokens', 'cache_creation_input_tokens')
    counts = [usage[k] for k in fields if isinstance(usage, dict) and type(usage.get(k)) is int and usage[k] >= 0]
    if not partial and len(counts) != len(fields): return None
    return sum(counts)


def reported_usage(provider, directory, complete=True):
    """Read actual provider output even on invalid results, timeouts and errors."""
    try:
        if not complete: return None
        raw = (Path(directory) / 'stdout.log').read_text()
        events = []
        try: events = [json.loads(raw)]
        except ValueError:
            for line in raw.splitlines():
                try: events.append(json.loads(line))
                except ValueError:
                    if line.strip(): return None
        if provider == 'claude':
            results = [v for v in events if isinstance(v, dict) and v.get('type') in (None, 'result')]
            if (not results or events[-1] is not results[-1]
                    or any(token_count(v.get('usage'), provider) is None for v in results)):
                return None
            return results[-1]['usage']
        turns, pending = [], False
        for event in events:
            if not isinstance(event, dict): return None
            kind = event.get('type')
            if (not isinstance(kind, str) or kind == 'error'
                    or kind.startswith('turn.') and kind not in ('turn.started', 'turn.completed')): return None
            if kind == 'turn.started':
                if pending: return None
                pending = True
            elif isinstance(kind, str) and kind.startswith('item.'):
                pending = True
            elif kind == 'turn.completed':
                turns.append(event.get('usage')); pending = False
        if pending: return None
        if not turns or any(token_count(v, provider) is None for v in turns): return None
        return {key: sum(v[key] for v in turns) for key in ('input_tokens', 'output_tokens')}
    except (OSError, ValueError, TypeError): return None


def reported_lower_bound(provider, directory):
    """Retain measured turns even when a later turn makes the total unknown."""
    try:
        raw = (Path(directory) / 'stdout.log').read_text()
        try: events = [json.loads(raw)]
        except ValueError:
            events = []
            for line in raw.splitlines():
                try: events.append(json.loads(line))
                except ValueError: pass
        counts = [token_count(v.get('usage'), provider, partial=True) for v in events if isinstance(v, dict)
                  and (v.get('type') == 'turn.completed' if provider == 'codex' else v.get('type') in (None, 'result'))]
        known = [count for count in counts if count is not None]
        return sum(known) if provider == 'codex' else max(known, default=0)
    except (OSError, ValueError, TypeError): return 0


class Ledger:
    def __init__(self, root, issue, config, clock=time.time):
        if not re.fullmatch(r'[A-Z][A-Z0-9_]*-\d+', issue): raise ValueError('invalid ticket identity')
        self.root, self.issue, self.clock = Path(root) / 'tickets', issue, clock
        self.config = config
        self.budget, self.progress = settings(config)
        self.contract_digest = hashlib.sha256(json.dumps(self.progress['contract'],sort_keys=True).encode()).hexdigest()
        self.required = any(self.budget[k] for k in ('tokens', 'provider_calls', 'wall_seconds')) or self.progress['enabled']
        self.path = self.root / (issue + '.json')
        self.history_path = self.root / (issue + '.attempts.json')
        self.witness_path = self.root.parent / 'ticket-witnesses' / (issue + '.json')

    def fresh(self):
        return dict(version=1, issue=self.issue, attempts={}, facts={}, turns={}, non_improving_turns=0,
                    progress_seen=[], terminal_reason=None, last_stop=None)

    def mark_witness(self):
        # Runtime state, outside provider-writable worktrees and prunable evidence.
        self.witness_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        atomic(self.witness_path, dict(version=1, issue=self.issue))

    def history(self):
        if not self.history_path.exists(): return dict(attempts=[], reset_baseline=[])
        value = json.loads(self.history_path.read_text())
        if (not isinstance(value, dict) or set(value) != {'attempts', 'reset_baseline'}
                or any(not isinstance(value[key], list) or any(not isinstance(v, str) for v in value[key])
                       for key in ('attempts', 'reset_baseline'))):
            raise ValueError('invalid ticket accounting history')
        return value

    @contextmanager
    def guard(self, reset=False):
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        with os.fdopen(os.open(str(self.root / (self.issue + '.guard')), os.O_CREAT | os.O_RDWR, 0o600), 'a') as lock:
            deadline = time.monotonic() + 5
            while True:
                try: fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB); break
                except BlockingIOError:
                    if time.monotonic() >= deadline: raise OSError('ticket accounting lock unavailable')
                    time.sleep(.01)
            if reset:
                yield self.fresh()
                return
            witnessed = self.required and self.witness_path.exists()
            history = self.history() if witnessed else dict(attempts=[], reset_baseline=[])
            missing = False
            try:
                value = json.loads(self.path.read_text())
            except FileNotFoundError:
                value = self.fresh(); missing = True
            except (OSError, ValueError):
                if not witnessed: raise
                value = self.fresh(); missing = True
            if value == {} and witnessed: value = self.fresh(); missing = True
            if (not isinstance(value, dict) or value.get('version') != 1 or value.get('issue') != self.issue
                    or not isinstance(value.get('attempts'), dict)
                    or value.get('terminal_reason') not in (None, *STOP_CODES)):
                raise ValueError('invalid ticket ledger; inspect before resuming')
            prior = value.get('last_stop')
            if prior is not None:
                if (not isinstance(prior, dict) or prior.get('terminal_reason') not in STOP_CODES
                        or prior['terminal_reason'] != value['terminal_reason'] or type(prior.get('code')) is not int):
                    raise ValueError('invalid ticket ledger terminal reason')
                stop_code(prior['terminal_reason'], prior.get('code'))
            if witnessed and (missing or set(history['attempts']) - set(value['attempts'])): value['accounting_lost'] = True
            if type(value.get('accounting_lost', False)) is not bool:
                raise ValueError('invalid ticket accounting status')
            for row in value['attempts'].values():
                if (not isinstance(row, dict) or type(row.get('started_at')) not in (int, float)
                        or not math.isfinite(row['started_at']) or row['started_at'] < 0
                        or row.get('ended_at') is not None and (type(row['ended_at']) not in (int, float)
                            or not math.isfinite(row['ended_at']) or row['ended_at'] < row['started_at'])
                        or row.get('tokens') is not None and (type(row['tokens']) is not int or row['tokens'] < 0)):
                    raise ValueError('invalid ticket attempt measurements')
                lower = row.get('tokens_known', row.get('tokens') or 0)
                if (type(lower) is not int or lower < 0
                        or row.get('tokens') is not None and lower != row['tokens']):
                    raise ValueError('invalid ticket token lower bound')
            if type(value.get('non_improving_turns')) is not int or value['non_improving_turns'] < 0:
                raise ValueError('invalid progress counter')
            if self.required and not witnessed:
                # Start witnessing only this ledger, never receipts from an
                # earlier release or non-paid validation receipt identities.
                atomic(self.history_path, dict(attempts=sorted(value['attempts']), reset_baseline=[]))
                self.mark_witness()
                # A crash after the marker but before this write fails closed.
                if missing: atomic(self.path, value)
            yield value

    def call(self, method, *args, **kwargs):
        # With both policies off, accounting I/O is optional at every boundary.
        try: return getattr(self, method)(*args, **kwargs)
        except OSError:
            if self.required: raise
            diagnostic('WARN: optional accounting unavailable')
            return None

    def save_stop(self, value, reason, code, run=None, detail=None):
        self._stop(value, reason, code, run, detail)
        try: atomic(self.path, value)
        except OSError:
            if not value.get('accounting_lost'): raise
            diagnostic('accounting lost; cannot persist stop, history still holds the ticket')

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
        known = sum(v.get('tokens_known', v['tokens'] or 0) for v in attempts)
        return dict(provider_calls=len(attempts), tokens=None if any(v['tokens'] is None for v in attempts) else known, tokens_known=known,
                    unknown_usage=unknown, wall_seconds=sum(max(0, (v['ended_at'] if v['ended_at'] is not None else now) - v['started_at']) for v in attempts))

    def decision(self, value):
        totals = self.totals(value)
        if self.required and value.get('accounting_lost'):
            return dict(terminal_reason='budget', code=25, dimension='accounting', accounting_lost=True,
                        detail='accounting lost; explicit reset required', totals=totals)
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
        with self.guard() as value:
            decision = self.decision(value)
            if decision:
                self.save_stop(value, decision['terminal_reason'], decision['code'], run, decision)
            return decision

    def begin(self, identity, provider):
        if provider not in ('claude', 'codex'): raise ValueError('invalid provider')
        with self.guard() as value:
            decision = self.decision(value)
            if decision:
                self.save_stop(value, decision['terminal_reason'], 25, identity['run_id'], decision)
                return decision
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
            history = self.history()
            history['attempts'] = sorted(set(history['attempts']) | set(value['attempts']))
            atomic(self.history_path, history)
            return None

    def reset(self):
        """Operator-only lifetime reset; never an implicit retry or file deletion."""
        if os.environ.get('BUREAU_RUN_ID') or os.environ.get('BUREAU_STAGE_ATTEMPT_ID'):
            raise ValueError('stop drivers and leave the run environment before resetting accounting')
        with self.guard(reset=True) as value:
            # Serialize the reset with runtime claim admission as well as calls.
            with (self.root.parent / 'guard').open('a') as ownership:
                fcntl.flock(ownership, fcntl.LOCK_EX | fcntl.LOCK_NB)
                leases_path = self.root.parent / 'leases.json'
                leases = json.loads(leases_path.read_text()) if leases_path.exists() else {}
                if not isinstance(leases, dict) or 'issue:' + self.issue in leases:
                    raise ValueError('reconcile ticket ownership before resetting accounting')
                if self.required and not self.witness_path.exists(): self.mark_witness()
                # A crash before clearing history still fails closed against
                # the previous admissions. Only this explicit command forgives it.
                atomic(self.path, value)
                atomic(self.history_path, dict(attempts=[], reset_baseline=[]))

    def finish(self, attempt, usage, code, tokens_known=0):
        with self.guard() as value:
            if attempt not in value['attempts'] and not self.required:
                diagnostic('WARN: optional attempt was not recorded')
                return
            row = value['attempts'][attempt]
            if row['ended_at'] is None:
                measured = token_count(usage, row['provider'])
                if type(tokens_known) is not int or tokens_known < 0: raise ValueError('invalid token lower bound')
                tokens_known = max(tokens_known, token_count(usage, row['provider'], partial=True))
                row.update(ended_at=self.clock(), tokens=measured,
                           tokens_known=measured if measured is not None else tokens_known, normalized_exit=code)
                atomic(self.path, value)

    def note_stop(self, reason, code, run=None):
        with self.guard() as value:
            # An originating cap/watchdog is more specific than its later generic hold.
            prior = value['last_stop']
            if prior and prior['run_id'] == run and prior['code'] == code and reason in ('human_hold', prior['terminal_reason']): return
            self.save_stop(value, reason, code, run)

    def terminal(self, code, run=None):
        if code not in (0, 25, 26, 124): return None
        with self.guard() as value:
            prior = value['last_stop']
            if prior and prior['run_id'] == run and prior['code'] == code: return prior['terminal_reason']
            reason = ('budget' if code == 25 and self.required and value.get('accounting_lost') else
                      {124: 'timeout', 25: 'human_hold', 26: 'ticket_withdrawn'}.get(code))
            if reason:
                self.save_stop(value, reason, code, run)
            return reason

    def _record_gate(self, run, turn, command_id, command, code, start, end, path):
        if not self.progress['enabled'] or not run or not turn or code != 0 or not start or start != end: return
        # Internal to executors that actually ran the canonical command. There
        # is deliberately no CLI that accepts caller-supplied gate proof.
        proof = hashlib.sha256(Path(path).read_bytes()).hexdigest()
        with self.guard() as value:
            if value.get('accounting_lost'): raise ValueError('accounting lost; gate cannot establish progress')
            for kind, entries in self.progress['contract'].items():
                for key, expected in entries.items():
                    if expected == dict(command_id=command_id, command=command):
                        fact = self.contract_digest + ':' + kind + ':' + key
                        value['facts'].setdefault(fact, dict(run_id=run, stage_attempt_id=turn, head=end, sha256=proof,
                                                           contract_sha256=self.contract_digest))
            atomic(self.path, value)

    def execute_gate(self, repo, run, turn, command_id, command, path):
        import importlib.util
        spec = importlib.util.spec_from_file_location('ticket_provider', Path(__file__).resolve().with_name('bureau-provider.py'))
        provider = importlib.util.module_from_spec(spec); spec.loader.exec_module(provider)
        repo = Path(repo).resolve()
        stage = 'code_review' if command_id == 'review.build' else ('qa' if command_id == 'qa.test' else 'implement')
        configured = provider.g.commands(self.config, repo, stage)
        if command_id == 'review.build' and not configured:
            if (repo / 'scripts/bureau-test.sh').is_file(): configured = ['bash scripts/bureau-test.sh']
            elif (repo / 'package.json').is_file(): configured = ['npm run build']
        if command_id not in GATE_IDS or configured != [command]:
            raise ValueError('gate must execute the exact configured command')
        start = provider.current_head(repo)
        options = provider.configuration(stage, self.config, os.environ)
        try: options['untrusted_env'] = provider.untrusted_env_mode(self.config)
        except provider.UntrustedEnvError as exc: raise ValueError(str(exc)) from exc
        code, abort = provider.outside_gate(options, repo, command, Path(path))
        end = provider.current_head(repo)
        if not abort:
            self._record_gate(run, turn, command_id, command, code, start, end, path)
        return 128 - code if code < 0 else code

    def end_turn(self, turn, run):
        if not self.progress['enabled']: return None
        with self.guard() as value:
            if value.get('accounting_lost'): return self.decision(value)
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
    reset = sub.add_parser('reset')
    reset.add_argument('--acknowledge-accounting-loss', action='store_true', required=True)
    gate = sub.add_parser('execute-gate')
    for name in ('path', 'command', 'command-id'): gate.add_argument('--' + name, required=True)
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
        elif args.action == 'reset': ledger.reset()
        elif args.action == 'execute-gate':
            return ledger.execute_gate(repo, run, os.environ.get('BUREAU_STAGE_ATTEMPT_ID'), args.command_id,
                                       args.command, args.path)
        return 0
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as exc:
        diagnostic(str(exc)); return 24


if __name__ == '__main__': sys.exit(main())
