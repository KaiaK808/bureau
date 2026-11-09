#!/usr/bin/env python3
"""Execution evidence, assertion-only QA retries and configured-gate reconciliation.

Imported by bureau-provider.py. Machine recovery uses typed references; the
legacy prefix is a separate compatibility input. The store owns retry claims.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import sys
import time
import uuid


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def qa_timeout():
    value = os.environ.get('BUREAU_QA_TEST_TIMEOUT', '900')
    return int(value) if re.fullmatch(r'[0-9]{1,5}', value) and 1 <= int(value) <= 86400 else 900


def qa_test_output(path):
    """Recognize complete assertion summaries; unfamiliar output fails closed."""
    try:
        if Path(path).stat().st_size > 8 * 1024 * 1024: return None
        output = Path(path).read_text(encoding='utf-8')
    except (OSError, UnicodeError): return None
    output = re.sub(r'\x1b\[[0-9;]*m', '', output)
    # Runner case labels and traceback frames are identities, not failure diagnostics.
    diagnostics = re.sub(r'^\S+ \([^\n]+\) \.\.\. (?:ok|FAIL|skipped .*)$|'
                         r'^\S+ (?:PASSED|FAILED|SKIPPED)(?:[ \t]+\[[ \t]*\d+%\])?[ \t]*$|'
                         r'^FAIL: \S+ \([^\n]+\)$|^_+ \S+ _+$|'
                         r'^[ \t]+File "[^\n]+", line \d+(?:, in [^\n]+)?$', '', output, flags=re.M)
    diagnostics = re.sub(r'^\S+\.py:\d+:(?: in \S+[ \t]*$|[ \t]*)|'
                         r'^[ \t>]*(?:async[ \t]+)?def[ \t]+\w+(?=[ \t]*\()', '', diagnostics, flags=re.M)
    diagnostics = re.sub(r'^FAILED \S+(?: - |$)', '', diagnostics, flags=re.M)
    if re.search(r'operation not permitted|permission denied|read-only file system|sandbox|\b(?:EPERM|EACCES|EROFS|ENOSPC)\b|'
                 r'\b(?:EADDRINUSE|EADDRNOTAVAIL|ECONNREFUSED|ENETUNREACH|EAI_AGAIN)\b|'
                 r'address already in use|connection refused|network is unreachable|name resolution|'
                 r'no module named|command not found|no such file or directory|'
                 r'timed? out|timeout|cleanup (?:failed|error)|teardown|tearDown|segmentation fault|'
                 r'core dumped|fatal error|interrupted', diagnostics, re.I): return None
    # unittest's terminal summary distinguishes assertion failures from errors.
    summary = re.search(r'\nRan (\d+) tests? in [\d.]+s\s*\n\s*(OK(?: \([^\n]+\))?|FAILED \([^\n]+\))\s*\Z', output)
    if summary:
        counts = dict((key, int(value)) for key, value in re.findall(r'(\w+)=(\d+)', summary[2]))
        if set(counts) - {'failures', 'skipped'}: return None
        if summary[2].startswith('FAILED') != bool(counts.get('failures', 0)): return None
        names = re.findall(r'^FAIL: (\S+) \(([^\n]+)\)$', output, re.M)
        names = [owner if owner.endswith('.' + name) else owner + '.' + name for name, owner in names]
        result = dict(runner='unittest', total=int(summary[1]), failed=counts.get('failures', 0),
                      skipped=counts.get('skipped', 0), names=names)
        cases = re.findall(r'^(\S+) \(([^\n]+)\) \.\.\. (?:ok|FAIL|skipped .*)$', output, re.M)
        cases = [owner if owner.endswith('.' + name) else owner + '.' + name for name, owner in cases]
    else:
        summary = re.search(r'^(?:=+ )?((?:\d+ (?:failed|passed|skipped|deselected|errors?|warnings?|xfailed|xpassed)(?:, )?)+) in [\d.]+s(?: .*?)?\s*\Z', output, re.M)
        if not summary: return None
        counts = dict((key, int(value)) for value, key in re.findall(r'(\d+) (\w+)', summary[1]))
        if set(counts) - {'failed', 'passed', 'skipped', 'deselected', 'warning', 'warnings'}: return None
        failures = re.findall(r'^FAILED (\S+)(?: - ([^\n]*)|)$', output, re.M)
        if any(not re.match(r'(?:AssertionError(?::|$)|assert\b)', reason) for _, reason in failures): return None
        names = [name for name, _ in failures]
        result = dict(runner='pytest', total=sum(counts.get(key, 0) for key in ('failed', 'passed', 'skipped')), failed=counts.get('failed', 0),
                      skipped=counts.get('skipped', 0), names=names)
        cases = re.findall(r'^(\S+) (?:PASSED|FAILED|SKIPPED)(?:\s+\[\s*\d+%\])?\s*$', output, re.M)
    if (result['total'] <= 0 or result['total'] <= result['skipped']
            or result['failed'] + result['skipped'] > result['total']
            or result['failed'] != len(set(result['names'])) or len(result['names']) != len(set(result['names']))): return None
    result['cases'] = cases if (len(cases) == len(set(cases)) == result['total']
                               and set(result['names']) <= set(cases)) else None
    return result


def qa_retry_command(command, evidence):
    """Select named cases only for simple, unambiguous native runner commands."""
    try: args = shlex.split(command)
    except ValueError: return command, False
    if not args or any(re.search(r'[;&|<>`$\n]', arg) for arg in args): return command, False
    prefix = 1 if Path(args[0]).name in ('pytest', 'pytest.exe') else 0
    if len(args) >= 3 and re.fullmatch(r'python(?:3(?:\.\d+)?)?', Path(args[0]).name) and args[1] == '-m':
        if args[2] == evidence['runner']: prefix = 3
    if not prefix: return command, False
    runner = Path(args[0]).name.removesuffix('.exe') if prefix == 1 else args[2]
    if runner != evidence['runner']: return command, False
    flags = {'-q', '-v', '-vv', '--disable-warnings', '--no-header'} if runner == 'pytest' else {'-q', '-v', '-b'}
    tail = args[prefix:]
    if any(arg.startswith('-') and arg not in flags for arg in tail): return command, False
    if runner == 'unittest' and 'discover' in tail: return command, False
    names = evidence['names']
    if any(name.startswith('-') for name in names) or sum(map(len, names)) > 32768: return command, False
    if runner == 'unittest' and any(not re.fullmatch(r'[\w.]+', name) for name in names): return command, False
    return shlex.join(args[:prefix] + [arg for arg in tail if arg in flags] + names), True


def qa_run_gate(execute, command, log, timeout, head, record):
    """One visible retry, only after a completed run with named assertion failures."""
    checked = head(); started = time.monotonic()
    code, abort = execute(command, log)
    elapsed = time.monotonic() - started
    result = dict(logs=[str(log)], flaky=None)
    record(log, command, code, checked, head(), None)
    first = qa_test_output(log)
    if (abort or code <= 0 or code >= 124 or elapsed >= timeout or not checked or checked != head()
            or not first or not first['failed']): return code, abort, result
    try: first_digest = hashlib.sha256(Path(log).read_bytes()).hexdigest()
    except OSError: return code, abort, result
    retry_command, selected = qa_retry_command(command, first)
    retry_log = Path(log).with_name(Path(log).stem + '.retry.log')
    print('QA: assertion failures; rerunning failed cases once.' if selected else
          'QA: assertion failures; rerunning the whole gate once.', file=sys.stderr)
    started = time.monotonic()
    code, abort = execute(retry_command, retry_log)
    elapsed = time.monotonic() - started
    result['logs'].append(str(retry_log))
    second = qa_test_output(retry_log)
    if code == 0 and not abort:
        try: unchanged = hashlib.sha256(Path(log).read_bytes()).hexdigest() == first_digest
        except OSError: unchanged = False
        covered = (second and second['runner'] == first['runner'] and not second['failed']
                   and (second['total'] == first['failed'] if selected else
                        first['cases'] and second['cases'] and set(first['cases']) <= set(second['cases']))
                   and second['skipped'] == 0
                   and elapsed < timeout and checked == head() and unchanged)
        if covered:
            result['flaky'] = dict(failed_tests=[name[:200] for name in first['names'][:20]],
                                   omitted=max(0, len(first['names']) - 20),
                                   first_sha256=first_digest)
        else:
            code = 1
            with retry_log.open('a') as output: output.write('\nQA: rerun did not prove coverage of the first run.\n')
    record(retry_log, retry_command, code, checked, head(), result['flaky'])
    return code, abort, result


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


def plan(store, value, stage, configured, head, context=None, repo=None):
    held = blockers(value, stage, configured)
    if held is None or not head or not configured: return dict(eligible=False, reason='non-gate blocker or missing command')
    result = dict(eligible=True, head=head, stage=stage, source=held['source'], commands=held['commands'],
                  result=value, context=context)
    if not isinstance(context, dict): return dict(eligible=False, reason='missing machine evidence')
    receipt = store.match_gate_receipt(context, stage, head, value, repo)
    if receipt is None: return dict(eligible=False, reason='missing, mismatched or stale receipt')
    execution_head = receipt['end_head']
    attempts = []
    for command in held['commands']:
        matches = [a for a in receipt.get('gate_attempts', []) if a.get('source') == 'codex.command_execution' and a.get('environment') == 'sandbox'
                   and a.get('command_sha256') == digest(command) and a.get('gate_id') == digest(command)
                   and a.get('head') == execution_head and a.get('end_head') == execution_head
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
