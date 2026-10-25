#!/usr/bin/env python3
"""Codex execution evidence and fail-closed configured-gate reconciliation.

Imported by bureau-provider.py. Machine recovery uses typed references; the
legacy prefix is a separate compatibility input. The store owns retry claims.
"""
import hashlib
import json
from pathlib import Path
import shlex
import time
import uuid


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def codex_schema(node):
    """Strict wire projection; optional Bureau fields become required/nullable.

    The canonical schema and its receipt hash stay unchanged. Never send an
    optional property directly to Codex's strict structured-output validator.
    """
    if isinstance(node, list): return [codex_schema(item) for item in node]
    if not isinstance(node, dict): return node
    result = {key:codex_schema(value) for key,value in node.items()}
    if node.get('type') == 'object' and isinstance(node.get('properties'), dict):
        required = node.get('required', [])
        for key,value in result['properties'].items():
            if key not in required:
                result['properties'][key] = dict(anyOf=[value, dict(type='null')])
        result['required'] = list(result['properties'])
        result.setdefault('additionalProperties', False)
    return result


def canonical_result(value, schema):
    """Omit ONLY declared optional nulls from the strict Codex wire result.

    Required nulls, unknown keys and wrong types survive to canonical validation
    and fail there; this cannot erase an explicit product/security/human hold.
    """
    if isinstance(value, dict):
        props, required = schema.get('properties', {}), schema.get('required', [])
        return {key:canonical_result(item, props.get(key, {})) for key,item in value.items()
                if not (key in props and key not in required and item is None)}
    if isinstance(value, list): return [canonical_result(item, schema.get('items', {})) for item in value]
    return value


def commands(config, repo, stage):
    configured = (config.get('repo') or {}).get('test_command')
    if isinstance(configured, str) and configured.strip(): return [configured]
    if stage != 'qa': return []
    # Keep QA's detection deterministic, shared with its shell entry point.
    if (repo / 'scripts/bureau-test.sh').is_file(): return ['bash scripts/bureau-test.sh']
    if (repo / 'package.json').is_file():
        package = json.loads((repo / 'package.json').read_text())
        scripts = package.get('scripts') if isinstance(package, dict) else None
        if isinstance(scripts, dict) and scripts.get('test') is not None and scripts.get('test') is not False:
            ci = scripts.get('test:ci')
            return ['npm run test:ci --silent' if ci is not None and ci is not False else 'npm test --silent']
    if (repo / 'Cargo.toml').is_file(): return ['cargo test --quiet']
    if any((repo / name).is_file() for name in ('pyproject.toml', 'pytest.ini', 'setup.cfg')): return ['pytest -q']
    if (repo / 'go.mod').is_file(): return ['go test ./...']
    return []


SYSTEM_PATH = '/usr/bin:/bin:/usr/sbin:/sbin'


def command_text(value):
    """Only exact absolute system-shell wrappers can stand for their command."""
    try:
        args = shlex.split(value) if isinstance(value, str) else value
        if not isinstance(args, list) or not all(isinstance(a, str) for a in args): return None
        # env must select bash through an explicit system-only PATH. Inherited
        # PATH (including an agent-owned bash) provides no executable identity.
        if len(args) == 5 and args[0] == '/usr/bin/env' and args[1].startswith('PATH=') and args[2] == 'bash':
            paths = args[1][5:].split(':')
            if paths and all(p in SYSTEM_PATH.split(':') for p in paths) and '/bin' in paths:
                args = ['/bin/bash', *args[3:]]
        shells = {Path('/bin/bash'), Path('/bin/sh')}
        targets = {shell.resolve() for shell in shells}
        if (len(args) == 3 and Path(args[0]) in shells | targets
                and Path(args[0]).resolve() in targets
                and args[1] in ('-c', '-lc')):
            return args[2]
    except (OSError, ValueError): pass
    return value if isinstance(value, str) else None


class Observer:
    """Bind live execution HEADs to one ordered rollout begin/end call pair.

    No tool request or model prose supplies proof. Raw commands/output are
    transient inputs to matching/hashing and never leave records().
    """
    def __init__(self, repo, configured, head, command_id='repo.test_command'):
        self.repo, self.configured, self.head = repo, configured, head
        self.items = {}; self.error = None; self.command_id = command_id

    def line(self, line):
        try: event = json.loads(line)
        except ValueError: return
        if not isinstance(event, dict): return
        item = event.get('item')
        if not isinstance(item, dict) or item.get('type') != 'command_execution': return
        key = item.get('id')
        if not isinstance(key, str) or not key: return
        kind = event.get('type')
        if kind == 'item.started':
            if key in self.items: self.items[key]['invalid'] = True; return
            self.items[key] = dict(attempt_id=uuid.uuid4().hex, command=item.get('command'),
                                   head=self.head(), started=time.monotonic(), call_id=item.get('call_id'))
        elif kind == 'item.completed':
            if key not in self.items:
                self.items[key] = dict(invalid=True)
                return
            attempt = self.items[key]
            if attempt.get('invalid') or 'end_head' in attempt: attempt['invalid'] = True; return
            attempt.update(end_head=self.head(), exit_code=item.get('exit_code'), status=item.get('status'),
                           output=item.get('aggregated_output'), completed_command=item.get('command'),
                           end_call_id=item.get('call_id'), duration_seconds=time.monotonic()-attempt['started'])

    def records(self, rollout=None):
        calls = {}
        if rollout:
            try:
                for index, line in enumerate(Path(rollout).read_text(errors='replace').splitlines()):
                    try: event = json.loads(line)
                    except ValueError: continue
                    if not isinstance(event, dict) or event.get('type') != 'event_msg': continue
                    payload = event.get('payload')
                    if not isinstance(payload, dict) or payload.get('type') not in ('exec_command_begin', 'exec_command_end'): continue
                    call = payload.get('call_id')
                    if isinstance(call, str) and call:
                        calls.setdefault(call, []).append((index, payload))
            except OSError: return []
        candidates = []
        for attempt in self.items.values():
            if attempt.get('invalid') or 'end_head' not in attempt: continue
            observed = command_text(attempt['command'])
            code, status, output = attempt['exit_code'], attempt['status'], attempt['output']
            if (observed not in self.configured or attempt['completed_command'] != attempt['command']
                    or not isinstance(output, str) or type(code) is not int
                    or status not in ('completed', 'failed', 'timed_out', 'cancelled')): continue
            matches = []; ambiguous = False
            for call, events in calls.items():
                if any(c is not None and c != call for c in (attempt['call_id'], attempt['end_call_id'])): continue
                # Exactly one begin and end, in that order, for THIS call ID.
                ordered = (len(events) == 2 and events[0][1]['type'] == 'exec_command_begin'
                           and events[1][1]['type'] == 'exec_command_end')
                if not ordered:
                    for _, event in events:
                        if (event['type'] == 'exec_command_begin' and command_text(event.get('command')) == observed
                                or event['type'] == 'exec_command_end' and event.get('exit_code') == code
                                and event.get('aggregated_output') == output
                                and ('command' not in event or command_text(event['command']) == observed)):
                            ambiguous = True
                    continue
                (_, begin), (_, end) = events
                if command_text(begin.get('command')) != observed: continue
                if 'command' in end and command_text(end['command']) != observed: continue
                if type(end.get('exit_code')) is not int or end['exit_code'] != code or end.get('aggregated_output') != output: continue
                cwd = begin.get('cwd')
                try: same_cwd = isinstance(cwd, str) and Path(cwd).is_absolute() and Path(cwd).resolve() == self.repo
                except (OSError, ValueError): same_cwd = False
                # Count identity candidates BEFORE checking cwd: otherwise a
                # second execution at root could lend cwd to one elsewhere.
                matches.append((call, same_cwd))
            if ambiguous or len(matches) != 1 or not matches[0][1]: continue
            call = matches[0][0]
            abort = ('timeout' if code == 124 or status == 'timed_out' else
                     'kill' if code < 0 or code in (129, 130, 131, 137, 143) or status == 'cancelled' else None)
            candidates.append((call, dict(
                attempt_id=attempt['attempt_id'], gate_id=digest(observed), command_sha256=digest(observed),
                command_id=self.command_id,
                head=attempt['head'], end_head=attempt['end_head'], normalized_exit=code,
                source='codex.command_execution', environment='sandbox', abort=abort,
                call_id_sha256=digest(call), duration_seconds=attempt['duration_seconds'],
                bytes=len(output.encode()), sha256=digest(output), stage_log=None)))
        # A single rollout call cannot be borrowed by multiple CLI items.
        return [record for call, record in candidates if sum(c == call for c, _ in candidates) == 1]


def blockers(value, stage, configured):
    """An exhaustive structured list, or the strict legacy prefix contract.

    Explicit typed blockers take precedence over the legacy prefix. A failed
    command alone cannot establish that a product/security/decision hold is gone.
    """
    if not isinstance(value, dict) or value.get('status') not in ('NEEDS_HUMAN', 'RED'): return None
    if stage == 'implement':
        notes = value.get('notes')
        items = notes.get('needs_human') if isinstance(notes, dict) else None
        if not isinstance(items, list) or not items or any(not isinstance(i, dict) for i in items): return None
        if (value.get('tasks_needs_human') != len(items) or value.get('tasks_skipped') != 0 or notes.get('skipped')): return None
        typed = any('kind' in i or 'command' in i for i in items)
        legacy = all(isinstance(i.get('reason'), str) and i['reason'].startswith('SANDBOX_GATE:') for i in items)
    elif stage == 'qa':
        typed = 'blockers' in value
        items = value.get('blockers')
        legacy = value.get('status') == 'NEEDS_HUMAN' and isinstance(value.get('coverage_notes'), str) and value['coverage_notes'].startswith('SANDBOX_GATE:')
    else: return None
    if typed:
        if not isinstance(items, list) or not items: return None
        if stage == 'implement' and (value.get('tasks_needs_human') != len(items)
                                     or value.get('tasks_skipped') != 0 or notes.get('skipped')): return None
        if any(not isinstance(i, dict) or i.get('kind') != 'gate' or i.get('command') not in configured for i in items): return None
        return dict(source='machine', commands=list(dict.fromkeys(i['command'] for i in items)))
    if legacy: return dict(source='prefix', commands=configured)
    return None


def plan(store, value, stage, configured, head, context=None):
    held = blockers(value, stage, configured)
    if held is None or not head or not configured: return dict(eligible=False, reason='non-gate blocker or missing command')
    result = dict(eligible=True, head=head, stage=stage, source=held['source'], commands=held['commands'],
                  result=value, context=context)
    if not isinstance(context, dict): return dict(eligible=False, reason='missing machine evidence')
    receipt = store.match_gate_receipt(context, stage, head, value)
    if receipt is None: return dict(eligible=False, reason='missing, mismatched or stale receipt')
    attempts = []
    for command in held['commands']:
        matches = [a for a in receipt.get('gate_attempts', []) if a.get('source') == 'codex.command_execution' and a.get('environment') == 'sandbox'
                   and a.get('command_sha256') == digest(command) and a.get('gate_id') == digest(command)
                   and a.get('head') == head and a.get('end_head') == head
                   and (a.get('abort') in ('timeout', 'kill') or type(a.get('normalized_exit')) is int and a['normalized_exit'] != 0)
                   and store.valid_execution(a)]
        if not matches: return dict(eligible=False, reason='no failed current-HEAD execution for configured gate')
        attempts.extend(a['attempt_id'] for a in matches)
    result['gate_attempt_ids'] = attempts
    return result


def machine_only(value, stage, configured, head, attempts):
    held = blockers(value, stage, configured)
    return (held is not None and held['source'] == 'machine' and bool(head)
            and all(any(a.get('source') == 'codex.command_execution' and a.get('command_sha256') == digest(command)
                        and a.get('head') == head and a.get('end_head') == head
                        and (a.get('abort') in ('timeout', 'kill') or type(a.get('normalized_exit')) is int and a['normalized_exit'] != 0)
                        for a in attempts) for command in held['commands']))


if __name__ == '__main__':
    # QA already obtained its configured command through bureau_get. Detect
    # fallbacks without loading provider settings or authenticating a model.
    import sys
    detected = commands({'repo':{'test_command':sys.stdin.read()}}, Path.cwd().resolve(), 'qa')
    print(detected[0] if detected else '')
