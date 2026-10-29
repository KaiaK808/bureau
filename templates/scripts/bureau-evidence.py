#!/usr/bin/env python3
"""Metadata-only local evidence. Never an authorization or stage prerequisite."""
from contextlib import contextmanager
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
import uuid

VERSION = 1
MAX_RUNS = 128
RETENTION_SECONDS = 30 * 86400
MAX_BYTES = 16 * 1024 * 1024
MAX_RECORDS = 4096  # All files except the lock, including atomic staging leftovers.
LOCK_TIMEOUT_SECONDS = .25
KINDS = ('decision', 'dependency', 'compliance', 'resource', 'technical')
TAGS = frozenset(('spec', 'spec_review', 'ux', 'copy', 'implement', 'qa', 'code_review', 'conflict_resolver', 'validation',
                  'merge', 'rebase', 'research', 'upstream_summary', 'specify', 'plan', 'tasks',
                  'correctness', 'security', 'performance', 'verdict', 'finding_verifier', 'worker', 'stage', 'driver'))
ACTORS = frozenset(('maintainer', 'workspace-owner', 'ci-or-gate-owner', 'repository-maintainer',
                    'merge-worker', 'operator'))
COMMAND_IDS = frozenset(('configured-command', 'repo.test_command', 'review.build', 'qa.test',
                        'review.artifact', 'qa.summary'))
BASES = frozenset(('observed-resume', 'observed-stage-resume', 'observed-review-resume',
                   'observed-approval-invalidated', 'observed-merge-wait-cleared', 'observed-merge-stage-resume',
                   'observed-cleared-hold', 'observed-terminal-transition', 'observed-pr-merge',
                   'authoritative-pr-merge', 'authoritative-linear-completion', 'observed-superseded'))
_warned = False
MAX_INSIGHTS = 3
MAX_INSIGHT_LENGTH = 280
MAX_DISPOSITIONS = 32
CAUSE_CLASSES = frozenset(('timeout', 'gate_red', 'review_capped', 'ownership', 'provider_error',
                           'quota', 'human_hold', 'other'))
STAGES = frozenset(('spec', 'spec_review', 'ux', 'copy', 'implement', 'qa', 'code_review', 'merge', 'rebase',
                    'research', 'upstream_summary'))
UNSAFE_INSIGHT = re.compile(
    r'\b[\w.-]*(?:token|key|secret|password|auth|cookie|session)[\w.-]*[\"\x27]?\s*[=:]\s*\S+'
    r'|\b(?:ghp_|gho_|github_pat_|sk-|lin_api_|xox|AKIA)[\w-]*'
    r'|-----BEGIN [^-\r\n]+-----'
    r'|[A-Za-z0-9+/_-]{32,}={0,2}'
    r'|\b[a-z][a-z0-9+.-]*://[^\s/]+@'
    r'|/(?:Users|home|root)/[^\s]+'
    r'|[a-z]:[\\/](?:Users|Documents and Settings)[\\/]', re.I)
# Scan split runs separately: a short benign run must not consume a following
# credential assignment before UNSAFE_INSIGHT can inspect it.
SPLIT_OPAQUE_INSIGHT = re.compile(r'(?:[A-Za-z0-9+/_-]{8,}[\s\"\x27]+)+[A-Za-z0-9+/_-]{8,}={0,2}')
LOG_INSIGHT = re.compile(
    r'\b(?:TRACE|DEBUG|INFO|WARN(?:ING)?|ERROR|FATAL|Traceback|stderr)\b|\bexit\s+code\b'
    r'|\bFile [\"\x27][^\"\x27]+[\"\x27], line \d+'
    r'|\bat\s+\S+\s+\([^\r\n)]+:\d+(?::\d+)?\)'
    r'|^\s*at\s+\S+:\d+(?::\d+)?\b'
    r'|^\s*(?:[\[{]|[$>+]\s|(?:PASS|FAIL)\b'
    r'|\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}'
    r'|\d{2}:\d{2}:\d{2}\b|(?:npm|yarn|pnpm)\s+(?:ERR|WARN|error|run)\b'
    r'|Traceback\b|File ".*", line \d+|total \d+\b|[d-][rwx-]{9}\s'
    r'|(?:ok|not ok) \d+\b|\d+ (?:passed|failed|errors?|warnings?)\b)', re.I)


class CapacityError(OSError):
    pass


def warn():
    # One warning per process, with no exception/path/content that could leak a secret.
    global _warned
    if not _warned:
        _warned = True
        # Never buffer this diagnostic: a failed buffered flush can make Python
        # replace the stage's exit with 120 during interpreter shutdown.
        try: os.write(2, b'bureau evidence: WARN: optional evidence unavailable or invalid; work continues\n')
        except OSError: pass


def observe(call, *args, **kwargs):
    """Every observational caller uses this boundary; only success receipts bypass it."""
    try: return call(*args, **kwargs)
    except Exception:
        warn()
        return None


USAGE_FIELDS = ('input_tokens', 'output_tokens', 'cached_input_tokens', 'cache_creation_input_tokens', 'total_cost_usd')
REVIEW_PHASES = frozenset(('correctness', 'security', 'performance', 'verdict'))


def usage_metadata(metadata):
    """Numeric allowlist; absent, invalid and unreported measurements stay null."""
    usage = metadata.get('usage')
    usage = usage if isinstance(usage, dict) else {}
    values = {key: usage.get(key) for key in USAGE_FIELDS}
    values['cached_input_tokens'] = usage.get('cached_input_tokens', usage.get('cache_read_input_tokens'))
    values['cache_creation_input_tokens'] = usage.get('cache_creation_input_tokens')
    values['total_cost_usd'] = metadata.get('total_cost_usd', usage.get('total_cost_usd'))
    return {key: value if (type(value) in ((int, float) if key == 'total_cost_usd' else (int,))
                          and 0 <= value <= 10**15 and math.isfinite(value)) else None for key, value in values.items()}


def review_round_shape(value):
    shape({k:v for k,v in value.items() if k != 'merge'}, 'number min_severity provider_verdict final_verdict provider_attempt_ids calls usage')
    if 'merge' in value: review_merge_shape(value['merge'])
    if type(value['number']) is not int or not 1 <= value['number'] <= 999999: raise ValueError('invalid review round')
    if value['min_severity'] not in ('BUG', 'MINOR'): raise ValueError('invalid review floor')
    if any(value[k] not in (None, 'APPROVE', 'REQUEST_CHANGES', 'BLOCK') for k in ('provider_verdict', 'final_verdict')):
        raise ValueError('invalid review decision')
    ids = value['provider_attempt_ids']
    if not isinstance(ids, list) or len(ids) != len(set(ids)): raise ValueError('invalid review calls')
    for item in ids: identifier(item)
    if type(value['calls']) is not int or value['calls'] != len(ids): raise ValueError('invalid review call count')
    shape(value['usage'], ' '.join(USAGE_FIELDS))
    for key, item in value['usage'].items():
        if item is not None and (type(item) not in ((int, float) if key == 'total_cost_usd' else (int,))
                                 or not math.isfinite(item) or item < 0): raise ValueError('invalid review usage')


def review_merge_shape(value):
    """Allowlist identities, counts and enums, never claim/source prose."""
    optional = {'human_holds', 'verdict_floor', 'severity_counts', 'result_sha256', 'missing_acceptance_count', 'fixes_needed_count', 'fallback_reasons'}
    shape({k:v for k,v in value.items() if k not in optional}, 'mode malformed_specialists grounding invalid_anchors anchor_checks verifier verifier_omitted critical_floor security_floor adr_refs')
    for key in ('human_holds', 'missing_acceptance_count', 'fixes_needed_count'):
        if key in value and (type(value[key]) is not int or not 0 <= value[key] <= 3999999999): raise ValueError('invalid blocker count')
    if 'verdict_floor' in value and value['verdict_floor'] not in ('APPROVE', 'REQUEST_CHANGES', 'BLOCK'):
        raise ValueError('invalid specialist verdict floor')
    if 'result_sha256' in value and not re.fullmatch(r'[a-f0-9]{64}', value['result_sha256']): raise ValueError('invalid review digest')
    if 'severity_counts' in value:
        shape(value['severity_counts'], 'critical bug minor skip')
        if any(type(n) is not int or not 0 <= n <= 3999999999 for n in value['severity_counts'].values()):
            raise ValueError('invalid severity counts')
    if not isinstance(value['adr_refs'], list) or len(value['adr_refs']) > 1024 or any(not isinstance(r, str) or not re.fullmatch(r'[0-9]{4}', r) for r in value['adr_refs']):
        raise ValueError('invalid merge ADR references')
    if value['mode'] not in ('deterministic', 'model_fallback') or value['grounding'] not in ('off', 'required'):
        raise ValueError('invalid review merge mode')
    roles = ('correctness', 'security', 'performance')
    reasons = value.get('fallback_reasons', {})
    if (not isinstance(reasons, dict) or any(role not in roles for role in reasons)
            or any(reason not in ('non_json_report', 'invalid_specialist', 'summary_signal') for reason in reasons.values())):
        raise ValueError('invalid fallback reason')
    if not isinstance(value['malformed_specialists'], list) or any(r not in roles for r in value['malformed_specialists']):
        raise ValueError('invalid fallback specialists')
    for key in ('invalid_anchors', 'verifier_omitted', 'critical_floor', 'security_floor'):
        if type(value[key]) is not int or not 0 <= value[key] <= 3999999999: raise ValueError('invalid merge count')
    if not isinstance(value['anchor_checks'], list) or len(value['anchor_checks']) > 4096: raise ValueError('invalid anchor checks')
    for row in value['anchor_checks']:
        shape(row, 'finding_id status')
        if not re.fullmatch(r'[a-f0-9]{64}', row['finding_id']): raise ValueError('invalid finding identity')
        if row['status'] not in ('valid', 'missing', 'shape', 'side', 'path', 'blob', 'digest', 'absent', 'foreign_blob', 'range', 'stale_digest', 'location'):
            raise ValueError('invalid anchor status')
    if not isinstance(value['verifier'], list) or len(value['verifier']) > 8: raise ValueError('invalid verifier count')
    for row in value['verifier']:
        shape(row, 'finding_id anchor_sha256 outcome basis')
        if any(not re.fullmatch(r'[a-f0-9]{64}', row[k]) for k in ('finding_id', 'anchor_sha256')):
            raise ValueError('invalid verification identity')
        if row['outcome'] not in ('confirmed', 'refuted', 'unverifiable') or row['basis'] not in ('invalid_anchor', 'budget', 'pending', 'model', 'invalid_result'):
            raise ValueError('invalid verification outcome')


def disposition_path(value):
    """Bounded repository-relative metadata, with the same best-effort privacy filter."""
    return (isinstance(value, str) and 0 < len(value) <= MAX_INSIGHT_LENGTH
            and re.fullmatch(r'[A-Za-z0-9_.@+ /-]+', value) is not None
            and value == value.strip() and not value.startswith(('/', '~', '-'))
            and all(part not in ('', '.', '..') for part in value.split('/'))
            and not unsafe_insight(value) and not LOG_INSIGHT.search(value))


def normalize_dispositions(value):
    if not isinstance(value, dict) or 'review_dispositions' not in value: return value
    items = value['review_dispositions']
    if (not isinstance(items, list) or len(items) > MAX_DISPOSITIONS or any(
            not isinstance(r, dict) or set(r) != {'disposition', 'severity', 'path'}
            or r['disposition'] not in ('applied', 'declined') or r['severity'] not in ('CRITICAL', 'BUG', 'MINOR', 'SKIP')
            or not disposition_path(r['path']) for r in items)):
        warn()
        return {k: v for k, v in value.items() if k != 'review_dispositions'}
    return value


def insight_cause(code, outcome=None, timeout_kind=None, signal=None, withdrawn=False):
    """The shell owns the mapping; inputs here are runtime metadata, never result fields."""
    cause = subprocess.check_output(['/bin/bash', str(Path(__file__).with_name('bureau-insights.sh')),
                                     str(code), outcome or '', timeout_kind or '', signal or '',
                                     '1' if withdrawn else '0'], text=True, timeout=2).strip()
    if cause not in CAUSE_CLASSES: raise ValueError('invalid insight cause')
    return cause


def normalize_insights(value):
    """Optional observations must never turn an otherwise valid result red."""
    value = normalize_dispositions(value)
    if not isinstance(value, dict) or 'insights' not in value: return value
    items = value['insights']
    if (not isinstance(items, list) or len(items) > MAX_INSIGHTS or
            any(not isinstance(s, str) or not s.strip() or len(s.splitlines()) != 1 or len(s) > MAX_INSIGHT_LENGTH or
                any(ord(c) < 32 or ord(c) == 127 for c in s) for s in items)):
        try: os.write(2, b'bureau insights: WARN: malformed optional insights dropped\n')
        except OSError: pass
        return {k: v for k, v in value.items() if k != 'insights'}
    safe = [s.strip() for s in items if not unsafe_insight(s) and not LOG_INSIGHT.search(s)
            and not (os.environ.get('HOME', '').startswith('/') and os.environ['HOME'].rstrip('/') + '/' in s)]
    if len(safe) != len(items):
        try: os.write(2, b'bureau insights: WARN: unsafe optional observations dropped (best-effort filter)\n')
        except OSError: pass
    return {**value, 'insights': list(dict.fromkeys(safe))}


def unsafe_insight(text):
    return bool(UNSAFE_INSIGHT.search(text)) or any(
        len(re.sub(r'[^A-Za-z0-9+/_-]', '', match[0])) >= 32
        for match in SPLIT_OPAQUE_INSIGHT.finditer(text))


def read(path, default=None):
    return json.loads(path.read_text()) if path.exists() else default


def encoded(value):
    return (json.dumps(value, indent=2, allow_nan=False) + '\n').encode()


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def text_digest(value):
    return hashlib.sha256(str(value).encode()).hexdigest()


def tag(value):
    if value in TAGS or re.fullmatch(r'iteration-[0-9]{1,6}', value or ''): return value
    if re.fullmatch(r'sha256:[a-f0-9]{64}', value or ''): return value
    if isinstance(value, str) and value.replace('-', '_') in TAGS: return value.replace('-', '_')
    return 'sha256:' + text_digest(value) if value is not None else None


def wait_key(value):
    if value in ('approval', 'ownership', 'merge-gate', 'merge-queue'): return value
    if isinstance(value, str) and value.startswith('needs-human:'): return 'needs-human:' + tag(value[12:])
    if re.fullmatch(r'sha256:[a-f0-9]{64}', value or ''): return value
    return 'sha256:' + text_digest(value)


def head(value):
    return value if isinstance(value, str) and re.fullmatch(r'[a-f0-9]{40,64}', value) else None


def private_dir(path):
    if not path.parent.exists(): private_dir(path.parent)
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path, 0o700)


def atomic(path, value):
    atomic_bytes(path, encoded(value))


def atomic_bytes(path, payload):
    private_dir(path.parent)
    temp = path.with_name('.' + path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        with os.fdopen(os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'wb') as out:
            out.write(payload); out.flush(); os.fsync(out.fileno())
        os.replace(temp, path)
        fd = os.open(path.parent, os.O_RDONLY)
        try: os.fsync(fd)
        finally: os.close(fd)
    finally:
        try: temp.unlink(missing_ok=True)
        except OSError:
            # A .tmp is never a committed record. If unlink is refused, try to
            # discard its payload; pruning can retry removal under the lock.
            # Its full size and file slot were reserved before atomic() ran.
            try:
                with temp.open('r+b') as out: out.truncate(0)
            except OSError: pass
            raise


def identifier(value):
    if not re.fullmatch(r'[a-f0-9]{32}', value or ''):
        raise ValueError('evidence IDs must be 32 lowercase hexadecimal characters')
    return value


def ticket(value):
    if not re.fullmatch(r'[A-Z][A-Z0-9_]*-[0-9]+', value or ''):
        raise ValueError('evidence issue must be a ticket identifier')
    return value


def generation(value):
    if value is not None and not re.fullmatch(r'[a-f0-9]{32,64}', value):
        raise ValueError('wait generation must be a digest or revision ID')
    return value


def pr_number(value):
    if isinstance(value, dict): value = value.get('number')
    return value if type(value) is int and value > 0 else None


def shape(value, fields):
    if not isinstance(value, dict) or set(value) != set(fields.split()): raise ValueError('invalid evidence fields')


def times(value, names):
    for name in names.split():
        at = value[name]
        if at is not None and (type(at) not in (int, float) or not math.isfinite(at)):
            raise ValueError('invalid evidence time')


EXECUTION_FIELDS = ('attempt_id gate_id command_id command_sha256 head end_head normalized_exit source '
                    'environment abort call_id_sha256 duration_seconds bytes sha256 stage_log')


def execution_metadata(record):
    """An explicit allowlist; never copy tool payloads into receipts/reservations."""
    value = {key: record.get(key) for key in EXECUTION_FIELDS.split()}
    execution_shape(value)
    return value


def execution_shape(value):
    shape(value, EXECUTION_FIELDS)
    identifier(value['attempt_id'])
    if value['command_id'] not in COMMAND_IDS: raise ValueError('invalid execution command role')
    for key in ('gate_id', 'command_sha256', 'sha256'):
        if not re.fullmatch(r'[a-f0-9]{64}', value[key] or ''): raise ValueError('invalid execution digest')
    if value['gate_id'] != value['command_sha256']: raise ValueError('invalid gate identity')
    if not head(value['head']) or not head(value['end_head']): raise ValueError('invalid execution HEAD')
    if type(value['normalized_exit']) is not int or type(value['bytes']) is not int or value['bytes'] < 0: raise ValueError('invalid execution measurement')
    if value['abort'] not in (None, 'kill', 'timeout'): raise ValueError('invalid execution abort')
    duration = value['duration_seconds']
    if type(duration) not in (int, float) or not math.isfinite(duration) or duration < 0: raise ValueError('invalid execution duration')
    if (value['source'], value['environment']) == ('codex.command_execution', 'sandbox'):
        if not re.fullmatch(r'[a-f0-9]{64}', value['call_id_sha256'] or '') or value['stage_log'] is not None:
            raise ValueError('invalid sandbox provenance')
    elif (value['source'], value['environment']) == ('bureau.shell', 'outside'):
        if value['call_id_sha256'] is not None or not isinstance(value['stage_log'], str) or not Path(value['stage_log']).is_absolute():
            raise ValueError('invalid stage log pointer')
    else: raise ValueError('invalid execution source')


def rerun_shape(value):
    shape(value, 'attempt_id head command_sha256 gate_id gate_attempt_ids started_at result')
    identifier(value['attempt_id'])
    if not head(value['head']) or value['gate_id'] != value['command_sha256'] or not re.fullmatch(r'[a-f0-9]{64}', value['gate_id'] or ''):
        raise ValueError('invalid rerun identity')
    if not isinstance(value['gate_attempt_ids'], list): raise ValueError('invalid rerun sources')
    for source in value['gate_attempt_ids']: identifier(source)
    times(value, 'started_at')
    if value['result'] is not None:
        execution_shape(value['result'])
        if (value['result']['source'] != 'bureau.shell' or value['result']['environment'] != 'outside'
                or any(value['result'][k] != value[k] for k in ('attempt_id', 'head', 'gate_id', 'command_sha256'))):
            raise ValueError('mismatched rerun completion')


def publication_shape(value):
    shape(value, 'provider_attempt_id stage_attempt_id pre_head head parent_head bureau_generated recorded_at')
    for key in ('provider_attempt_id', 'stage_attempt_id'): identifier(value[key])
    if (not head(value['pre_head']) or not head(value['head']) or value['pre_head'] == value['head']
            or value['parent_head'] != value['pre_head'] or value['bureau_generated'] is not True):
        raise ValueError('invalid executor publication')
    times(value, 'recorded_at')
    if value['recorded_at'] is None: raise ValueError('missing publication time')


def verify_publication(repo, before, after):
    """Read Git metadata transiently; never retain the commit message."""
    if not head(before) or not head(after) or before == after: return False
    def git(*args):
        return subprocess.check_output(['git', '--no-replace-objects', '-C', str(repo), *args],
                                       text=True, stderr=subprocess.DEVNULL).strip()
    try:
        return (git('rev-parse', '--verify', 'HEAD') == after
                and git('rev-list', '--parents', '-n', '1', after).split() == [after, before]
                and git('log', '-1', '--format=%(trailers:key=Bureau-Generated,valueonly)', after) == 'true')
    except (OSError, ValueError, subprocess.SubprocessError): return False


def manifest_shape(value):
    shape(value, 'schema_version record_type run_id issue workspace started_at ended_at start_head final_head normalized_exit pr attempts')
    if not isinstance(value['attempts'], list): raise ValueError('invalid attempts')
    for attempt in value['attempts']:
        if 'gate_reruns' in attempt:
            if not isinstance(attempt['gate_reruns'], list): raise ValueError('invalid gate reservations')
            for rerun in attempt['gate_reruns']: rerun_shape(rerun)
        if 'publications' in attempt:
            if not isinstance(attempt['publications'], list): raise ValueError('invalid publications')
            for publication in attempt['publications']:
                publication_shape(publication)
                if publication['stage_attempt_id'] != attempt['attempt_id']: raise ValueError('foreign publication')
        if 'review_round' in attempt:
            if attempt['stage'] != 'code_review': raise ValueError('foreign review round')
            review_round_shape(attempt['review_round'])
        if 'outcome_signals' in attempt:
            if (not isinstance(attempt['outcome_signals'], list)
                    or any(s not in ('timeout', 'provider_error', 'review_capped', 'gate_red', 'human_hold') for s in attempt['outcome_signals'])):
                raise ValueError('invalid outcome signals')
        shape({k:v for k,v in attempt.items() if k not in ('gate_reruns', 'publications', 'review_round', 'outcome_signals')}, 'attempt_id parent_attempt_id stage phase workspace started_at ended_at start_head final_head normalized_exit receipts gates pr')
        identifier(attempt['attempt_id'])
        if attempt['parent_attempt_id'] is not None: identifier(attempt['parent_attempt_id'])
        times(attempt, 'started_at ended_at')
        if not isinstance(attempt['receipts'], list) or any(not isinstance(p, str) or not re.fullmatch(r'runs/[a-f0-9]{32}/receipts/[a-f0-9]{32}\.json', p) for p in attempt['receipts']):
            raise ValueError('invalid receipt references')
        if not isinstance(attempt['gates'], list): raise ValueError('invalid gates')
        for gate in attempt['gates']:
            shape(gate, 'kind head end_head command_id command_sha256 normalized_exit recorded_at bytes sha256')
            if gate['kind'] not in ('gate', 'artifact') or gate['command_id'] not in COMMAND_IDS: raise ValueError('invalid gate')
            if any(not re.fullmatch(r'[a-f0-9]{64}', gate[k]) for k in ('command_sha256', 'sha256')): raise ValueError('invalid gate digest')
            if type(gate['bytes']) is not int or gate['bytes'] < 0: raise ValueError('invalid gate size')
            times(gate, 'recorded_at')


class Store:
    def __init__(self, bureau_dir, clock=None):
        self.root = Path(bureau_dir) / 'evidence' / 'v1'
        self.clock = clock or time.time
        self._observation_attempts = {}
        self._observation_turn = 0

    @contextmanager
    def guard(self):
        private_dir(self.root.parent); private_dir(self.root)
        with os.fdopen(os.open(self.root / 'guard', os.O_CREAT | os.O_RDWR, 0o600), 'a') as lock:
            deadline = time.monotonic() + LOCK_TIMEOUT_SECONDS
            while True:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline: raise TimeoutError('evidence lock unavailable')
                    time.sleep(.01)
            yield

    def manifest_path(self, run):
        return self.root / 'runs' / identifier(run) / 'manifest.json'

    def _quarantine(self, path):
        # Isolate corruption without retaining its potentially sensitive bytes.
        warn()
        try:
            raw = path.read_bytes()
            value = dict(schema_version=VERSION, record_type='quarantine', at=self.clock(),
                         bytes=len(raw), sha256=hashlib.sha256(raw).hexdigest())
            path.unlink()
            observe(self._write, self.root / 'quarantine' / (uuid.uuid4().hex + '.json'), value)
        except OSError: pass

    def _load(self, path, kind, quarantine=True):
        try:
            value = read(path)
            if value is None: return None
            if not isinstance(value, dict) or value.get('schema_version') != VERSION:
                raise ValueError('invalid evidence record')
            if kind != 'ticket' and value.get('record_type') != kind: raise ValueError('invalid record type')
            if kind in ('manifest', 'wait', 'ticket', 'acceptance') and value.get('issue') is not None: ticket(value['issue'])
            if kind == 'manifest':
                manifest_shape(value)
                identifier(value['run_id'])
                if value['run_id'] != path.parent.name or not isinstance(value['attempts'], list): raise ValueError('invalid manifest')
                for key in ('started_at', 'ended_at'):
                    if key not in value or (value[key] is not None and (type(value[key]) not in (int, float) or not math.isfinite(value[key]))):
                        raise ValueError('invalid run time')
                if value['ended_at'] is not None and type(value.get('normalized_exit')) is not int: raise ValueError('invalid run exit')
            elif kind == 'wait':
                shape(value, 'schema_version record_type wait_id issue key kind reason_sha256 responsible_actor stage exit_code run_id generation pr state reached_at last_observed_at last_checked_at resolved_at resolution_observed_at resolution_basis events')
                identifier(value['wait_id'])
                if value['state'] not in ('open', 'resolved') or value.get('issue') is None: raise ValueError('invalid wait')
                for key in ('reached_at', 'last_observed_at'):
                    if type(value[key]) not in (int, float) or not math.isfinite(value[key]): raise ValueError('invalid wait time')
                if not isinstance(value['key'], str) or not isinstance(value['events'], list): raise ValueError('invalid wait fields')
                if wait_key(value['key']) != value['key'] or value['kind'] not in KINDS or value['responsible_actor'] not in ACTORS:
                    raise ValueError('invalid wait metadata')
                if not re.fullmatch(r'[a-f0-9]{64}', value['reason_sha256']): raise ValueError('invalid wait digest')
                if value['run_id'] is not None: identifier(value['run_id'])
                generation(value['generation'])
                times(value, 'last_checked_at resolved_at resolution_observed_at')
                if value['state'] == 'resolved' and type(value['resolution_observed_at']) not in (int, float): raise ValueError('invalid resolution')
            elif kind == 'ticket':
                shape(value, 'schema_version issue first_observed_at terminal_observed_at last_observed_at')
                times(value, 'first_observed_at last_observed_at terminal_observed_at')
                if any(type(value[k]) not in (int, float) for k in ('first_observed_at', 'last_observed_at')): raise ValueError('invalid ticket time')
            elif kind == 'acceptance':
                shape(value, 'schema_version record_type issue pr head created_at body body_sha256 ticket pull_request')
                times(value, 'created_at')
                if type(value['created_at']) not in (int, float) or not pr_number(value['pr']) or not head(value['head']):
                    raise ValueError('invalid acceptance identity')
                if any(value[k] not in ('pending', 'delivered') for k in ('ticket', 'pull_request')):
                    raise ValueError('invalid acceptance delivery')
                if not isinstance(value['body'], str) or text_digest(value['body']) != value['body_sha256']:
                    raise ValueError('invalid acceptance digest')
            return value
        except (ValueError, KeyError, TypeError, AttributeError):
            if quarantine: self._quarantine(path)
            else: warn()
        except OSError:
            warn()
        return None

    def _records(self, pattern, kind, quarantine=True):
        for path in sorted(self.root.glob(pattern)):
            value = self._load(path, kind, quarantine)
            if value is not None: yield path, value

    def _manifest(self, run):
        value = self._load(self.manifest_path(run), 'manifest')
        if value is None: raise ValueError('unknown evidence run')
        return value

    def _prune(self, extra_bytes=0, extra_records=0, protect=None, writing=None):
        now = self.clock()
        # The caller holds guard(), so no live writer owns a staging file.
        # Recognize the old pre-.tmp name too. Failed deletion refuses writes;
        # it must never be treated as newly available capacity.
        for path in self.root.rglob('*'):
            if path.is_file() and (path.suffix == '.tmp' or re.fullmatch(r'\..*\.json\.[a-f0-9]{32}', path.name)):
                path.unlink()
        waits = list(self._records('waits/*.json', 'wait'))
        open_waits = [v for _, v in waits if v['state'] == 'open']
        protected_runs = {v.get('run_id') for v in open_waits} | {protect}
        active_issues = {v['issue'] for v in open_waits}
        completed = []
        for path, value in self._records('runs/*/manifest.json', 'manifest'):
            if value['ended_at'] is None or value.get('normalized_exit') == 130:
                active_issues.add(value['issue'])
            elif value['run_id'] not in protected_runs:
                completed.append((value['ended_at'], path.parent))
        completed.sort()
        for index, (ended, path) in enumerate(completed):
            if index < max(0, len(completed) - MAX_RUNS) or now - ended > RETENTION_SECONDS:
                shutil.rmtree(path)
        candidates = [(ended, path) for ended, path in completed if path.exists()]
        for path, value in waits:
            if value['state'] == 'resolved':
                if path == writing: continue
                if now - value['resolution_observed_at'] > RETENTION_SECONDS: path.unlink(missing_ok=True)
                else: candidates.append((value['resolution_observed_at'], path))
        for path, value in self._records('tickets/*.json', 'ticket'):
            if path == writing: continue
            if value['issue'] not in active_issues:
                if now - value['last_observed_at'] > RETENTION_SECONDS: path.unlink(missing_ok=True)
                else: candidates.append((value['last_observed_at'], path))
        for path, value in self._records('acceptance/*.json', 'acceptance'):
            if path == writing: continue
            if value.get('issue') not in active_issues:
                if now - value['created_at'] > RETENTION_SECONDS: path.unlink(missing_ok=True)
                elif value.get('ticket') == value.get('pull_request') == 'delivered':
                    candidates.append((value['created_at'], path))
        candidates += [(p.stat().st_mtime, p) for p in (self.root / 'quarantine').glob('*.json')]
        # Checkpoints are optional, including when another ticket's record is
        # pruned before the real provider starts. Only a success receipt is mandatory.
        try: paths = list((self.root / 'spec-checkpoints').glob('*.json'))
        except OSError:
            warn()
            paths = []
        for path in paths:
            if path == writing: continue
            try:
                value = read(path)
                if not isinstance(value, dict) or not isinstance(value.get('updated_at'), (int, float)) or not isinstance(value.get('issue'), str): continue
                if now - value['updated_at'] > RETENTION_SECONDS and value['issue'] not in active_issues:
                    path.unlink()
            except (OSError, ValueError, TypeError):
                warn()
                issue = observe(ticket, path.stem)
                if issue is None: continue
                observe(os.write, 2, ('bureau evidence: WARN: checkpoint reuse disabled for ' + issue + '\n').encode())
                try:
                    atomic(self.root.parent.parent / 'spec-checkpoints-disabled' / (issue + '.json'),
                           dict(issue=issue, reuse_disabled=True))
                except (OSError, ValueError, TypeError):
                    observe(os.write, 2, ('bureau evidence: WARN: checkpoint opt-out unavailable for ' + issue + '\n').encode())
        files = [p for p in self.root.rglob('*') if p.is_file() and p.name != 'guard']
        size = sum(p.stat().st_size for p in files); count = len(files)
        for _, path in sorted(candidates, key=lambda item: (item[0], str(item[1]))):
            if size + extra_bytes <= MAX_BYTES and count + extra_records <= MAX_RECORDS: break
            if path == writing: continue
            contents = [p for p in path.rglob('*') if p.is_file()] if path.is_dir() else [path]
            size -= sum(p.stat().st_size for p in contents); count -= len(contents)
            if path.is_dir(): shutil.rmtree(path)
            else: path.unlink(missing_ok=True)
        if size + extra_bytes > MAX_BYTES or count + extra_records > MAX_RECORDS:
            raise CapacityError('evidence capacity occupied by protected records')

    def _write(self, path, value):
        # A write cannot evict its own run. Reject staging when only protected
        # records remain: optional writes warn/skip; a SUCCESS receipt fails closed.
        protect = path.relative_to(self.root).parts[1] if path.relative_to(self.root).parts[0] == 'runs' else None
        # Every write needs a confirmed budget, including when housekeeping
        # cannot inspect or delete an eligible record. Observational callers
        # catch the failure and skip; the mandatory success receipt propagates it.
        # Replacement needs a full second file until rename succeeds, even for
        # an unchanged or shrinking record. Reserve both its bytes and slot.
        try: self._prune(len(encoded(value)), 1, protect, path)
        except CapacityError: raise
        except Exception as exc:
            raise CapacityError('evidence capacity could not be established') from exc
        atomic(path, value)

    def _save_manifest(self, value):
        self._write(self.manifest_path(value['run_id']), value)

    def _observe_ticket(self, issue, at, reopened=False):
        if issue is None: return
        path = self.root / 'tickets' / (ticket(issue) + '.json')
        value = self._load(path, 'ticket') or dict(schema_version=VERSION, issue=issue, first_observed_at=at,
                                                  terminal_observed_at=None)
        value['last_observed_at'] = at
        if reopened: value['terminal_observed_at'] = None
        self._write(path, value)

    def begin_run(self, run, issue, workspace, start_head):
        with self.guard():
            path = self.manifest_path(run)
            if path.exists():
                value = self._manifest(run)
                if value['issue'] != issue: raise ValueError('evidence run belongs to another ticket')
                return value
            if issue is not None: ticket(issue)
            now = self.clock()
            value = dict(schema_version=VERSION, record_type='manifest', run_id=run, issue=issue,
                         workspace=str(workspace), started_at=now, ended_at=None,
                         start_head=head(start_head), final_head=None, normalized_exit=None, pr=None, attempts=[])
            self._save_manifest(value); self._observe_ticket(issue, now)
            return value

    def begin_attempt(self, run, issue, stage, phase, workspace, start_head, parent=None):
        with self.guard():
            value = self._manifest(run)
            if value['issue'] != issue: raise ValueError('evidence attempt belongs to another ticket')
            attempt = dict(attempt_id=uuid.uuid4().hex, parent_attempt_id=identifier(parent) if parent else None,
                           stage=tag(stage), phase=tag(phase), workspace=str(workspace), started_at=self.clock(), ended_at=None,
                           start_head=head(start_head), final_head=None, normalized_exit=None, receipts=[], gates=[], pr=None)
            value.update(ended_at=None, normalized_exit=None, final_head=None)
            value['attempts'].append(attempt); self._save_manifest(value)
            return attempt['attempt_id']

    def end_attempt(self, run, attempt_id, code, final_head, outer=False):
        with self.guard():
            value = self._manifest(run)
            attempt = next(a for a in value['attempts'] if a['attempt_id'] == attempt_id)
            attempt.update(ended_at=self.clock(), normalized_exit=code, final_head=head(final_head))
            if outer:
                children = [a for a in value['attempts'] if a['attempt_id'] != attempt_id and a['final_head']]
                final = max(children, key=lambda a: a['ended_at'] or 0)['final_head'] if children else head(final_head)
                value.update(ended_at=self.clock(), normalized_exit=code, final_head=final)
            self._save_manifest(value)
        observe(self.collect_stage_outcome, value, attempt)

    def receipt_path(self, run, attempt):
        return self.manifest_path(run).parent / 'receipts' / (identifier(attempt) + '.json')

    def put_receipt(self, receipt):
        # Explicit projection, even if a caller accidentally supplies result prose
        # or provider diagnostics. The original provider files remain pointers only.
        fields = ('run_id', 'provider_attempt_id', 'stage_attempt_id', 'issue', 'schema_sha256',
                  'provider_exit', 'normalized_exit', 'started_at', 'ended_at', 'provider_logs', 'transcript')
        value = {k: receipt.get(k) for k in fields}
        value.update(schema_version=VERSION, record_type='receipt', stage=tag(receipt.get('stage')),
                     phase=tag(receipt.get('phase')), provider=receipt.get('provider') if receipt.get('provider') in ('codex', 'claude') else None,
                     outcome=receipt.get('outcome') if receipt.get('outcome') in ('complete', 'invalid-result', 'provider-error', 'timeout', 'cancelled', 'environment', 'quota') else None,
                     structured_result=None, result_sha256=digest(receipt['structured_result']) if receipt.get('structured_result') is not None else None)
        if value['issue'] is not None: ticket(value['issue'])
        if value['stage_attempt_id']: identifier(value['stage_attempt_id'])
        if value['schema_sha256'] is not None and not re.fullmatch(r'[a-f0-9]{64}', value['schema_sha256']):
            raise ValueError('invalid receipt schema digest')
        for key in ('provider_exit', 'normalized_exit'):
            if value[key] is not None and type(value[key]) is not int: raise ValueError('invalid receipt exit')
        for key in ('started_at', 'ended_at'):
            if value[key] is not None and (type(value[key]) not in (int, float) or not math.isfinite(value[key])):
                raise ValueError('invalid receipt time')
        for key in ('provider_logs', 'transcript'):
            if value[key] is not None and (not isinstance(value[key], str) or not Path(value[key]).is_absolute()):
                raise ValueError('receipt needs an absolute provider pointer')
        value.update(start_head=head(receipt.get('start_head')), end_head=head(receipt.get('end_head')))
        # Additive metadata only; legacy receipts have no timeout_kind. Never
        # infer silence from an interrupted review or an absent result/usage.
        if receipt.get('timeout_kind') in ('silence', 'wall-clock'):
            value['timeout_kind'] = receipt['timeout_kind']
        attempts = observe(lambda: [execution_metadata(a) for a in receipt.get('gate_attempts', [])])
        if attempts: value['gate_attempts'] = attempts
        inputs = receipt.get('review_inputs')
        if value['stage'] == 'code_review' and isinstance(inputs, dict):
            value['review_inputs'] = {key: head(inputs.get(key)) for key in ('head', 'base', 'validation_head')}
        refs = receipt.get('adr_refs')
        if value['stage'] == 'code_review' and isinstance(refs, list):
            value['adr_refs'] = sorted({ref for ref in refs if isinstance(ref, str) and re.fullmatch(r'[0-9]{4}', ref)})[:1024]
        conflict = receipt.get('conflict_resolution')
        if value['stage'] == 'conflict_resolver' and isinstance(conflict, dict):
            if (type(conflict.get('attempt')) is int and 1 <= conflict['attempt'] <= 2
                    and head(conflict.get('base')) and head(conflict.get('head'))
                    and conflict.get('operation') in ('merge', 'rebase')
                    and conflict.get('provider') in ('claude', 'codex')
                    and re.fullmatch(r'[a-f0-9]{64}', conflict.get('touched_path_digest', ''))
                    and conflict.get('check_outcome') in ('pending', 'passed', 'failed')):
                value['conflict_resolution'] = {key: conflict[key] for key in
                    ('attempt', 'base', 'head', 'operation', 'provider', 'touched_path_digest', 'check_outcome')}
        value['usage'] = usage_metadata(receipt)
        with self.guard():
            path = self.receipt_path(value['run_id'], value['provider_attempt_id'])
            self._write(path, value)
        # The receipt does not depend on the optional manifest existing or linking.
        observe(self._link_receipt, value, path)
        observe(self.collect_insights, receipt)
        observe(self.collect_dispositions, receipt)
        return path

    def collect_insights(self, receipt):
        """The sole free-text exception: bounded, advisory stage observations."""
        result = normalize_insights(receipt.get('structured_result') or receipt.get('insights_result'))
        items = result.get('insights', []) if isinstance(result, dict) else []
        if not items or receipt.get('stage') not in STAGES:
            return
        issue = ticket(receipt['issue']) if receipt.get('issue') else None
        run = identifier(receipt['run_id']); attempt = identifier(receipt['provider_attempt_id'])
        stage_attempt = identifier(receipt['stage_attempt_id'])
        cause = insight_cause(receipt.get('normalized_exit'), receipt.get('outcome'), receipt.get('timeout_kind'))
        rows = [dict(schema_version=VERSION, issue=issue, stage=receipt['stage'], run_id=run,
                     stage_attempt_id=stage_attempt, provider_attempt_id=attempt,
                     timestamp=self.clock(), observation=s, normalized_exit=receipt.get('normalized_exit'),
                     cause_class=cause) for s in items]
        self._append_learning('insights.jsonl', rows, ('provider_attempt_id',), run)

    def _append_learning(self, name, incoming, identity, run):
        """All learning ledgers share PR 7's lock, atomic budget and bounded retention."""
        if not incoming: return
        path = self.root / name
        with self.guard():
            rows = [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []
            now = self.clock()
            rows = [r for r in rows if now - r['timestamp'] <= RETENTION_SECONDS]
            if not any(all(r.get(k) == incoming[0].get(k) for k in identity) for r in rows): rows += incoming
            rows = rows[-2048:]
            payload = ''.join(json.dumps(r, ensure_ascii=True, separators=(',', ':')) + '\n' for r in rows).encode()
            self._prune(len(payload), 1, run, path)
            atomic_bytes(path, payload)

    def outcome_signal(self, run, stage_attempt, signal):
        if signal not in ('timeout', 'provider_error', 'review_capped', 'gate_red', 'human_hold'): raise ValueError('invalid outcome signal')
        with self.guard():
            value = self._manifest(run)
            attempt = next(a for a in value['attempts'] if a['attempt_id'] == identifier(stage_attempt))
            if signal == 'review_capped' and attempt['stage'] != 'code_review': raise ValueError('not a review stage')
            attempt['outcome_signals'] = sorted(set(attempt.get('outcome_signals', [])) | {signal})
            self._save_manifest(value)

    def collect_stage_outcome(self, manifest, attempt):
        # Drivers are not stages. Only a worker around its own stage is a
        # duplicate; a stage invoking another stage keeps its own outcome.
        if attempt['stage'] not in STAGES: return
        if attempt['phase'] == 'worker' and any(a['parent_attempt_id'] == attempt['attempt_id']
                and a['stage'] == attempt['stage'] and a['phase'] == 'stage' for a in manifest['attempts']): return
        code = attempt['normalized_exit']; run = manifest['run_id']
        receipts = [read(self.root / p, {}) for p in attempt['receipts']]
        # Recency is not a fatal decision: ignored phases (such as pre-spec
        # research) and recovered failures cannot explain an unrelated stop.
        # Parallel silence still matches its own 25 after sibling cancellation.
        failed = [r for r in receipts if code != 0 and r.get('stage') == attempt['stage']
                  and r.get('normalized_exit') == code]
        timeout = 'silence' if any(r.get('timeout_kind') == 'silence' and r.get('normalized_exit') == 25 for r in failed) else None
        outcome = next((s for s in ('quota', 'provider-error', 'invalid-result') if any(r.get('outcome') == s for r in failed)), None)
        signals = attempt.get('outcome_signals', [])
        gates = [g for g in attempt['gates'] if g.get('kind') == 'gate']
        red = code != 0 and gates and gates[-1].get('normalized_exit') not in (None, 0)
        signal = next((s for s in ('timeout', 'provider_error', 'review_capped', 'gate_red', 'human_hold') if s in signals), 'gate_red' if red else None)
        if signal == 'timeout' and code == 25: timeout = 'silence'
        control = self.root.parent.parent / 'ticket-control' / run
        withdrawn = any((control / ('stop-' + str(c))).exists() for c in (0, 25, 26))
        cause = insight_cause(code, outcome, timeout, signal, withdrawn)
        row = dict(schema_version=VERSION, record_type='stage_outcome', issue=manifest['issue'], stage=attempt['stage'],
                   run_id=run, stage_attempt_id=attempt['attempt_id'], provider_attempt_id=None,
                   phase=attempt['phase'], timestamp=self.clock(), normalized_exit=code, cause_class=cause)
        if code == 124 or (cause == 'timeout' and signal == 'timeout' and code != 25): row['timeout_kind'] = 'wall-clock'
        elif cause == 'timeout' and timeout: row['timeout_kind'] = timeout
        self._append_learning('stage-outcomes.jsonl', [row], ('stage_attempt_id',), run)

    def collect_dispositions(self, receipt):
        result = normalize_dispositions(receipt.get('structured_result') or receipt.get('insights_result'))
        if not isinstance(result, dict): return
        items = []
        if receipt.get('stage') == 'implement': items = result.get('review_dispositions', [])
        if receipt.get('stage') == 'code_review' and receipt.get('phase') == 'verdict' and receipt.get('normalized_exit') == 0:
            for finding in result.get('findings', []):
                severity = finding.get('class') if isinstance(finding, dict) else None
                disposition = 'skip' if severity == 'SKIP' else ('deferred' if severity == 'MINOR' and receipt.get('review_min_severity') == 'BUG' else None)
                if disposition and disposition_path(finding.get('file')):
                    items.append(dict(disposition=disposition, severity=severity, path=finding['file']))
        items = list({(r['disposition'], r['severity'], r['path']): r for r in items}.values())[:MAX_DISPOSITIONS]
        if not items: return
        issue = ticket(receipt['issue']) if receipt.get('issue') else None
        run = identifier(receipt['run_id']); provider = identifier(receipt['provider_attempt_id'])
        attempt = identifier(receipt['stage_attempt_id'])
        rows = [dict(schema_version=VERSION, record_type='review_disposition', issue=issue, stage=receipt['stage'],
                     run_id=run, stage_attempt_id=attempt, provider_attempt_id=provider, timestamp=self.clock(), **item) for item in items]
        self._append_learning('review-dispositions.jsonl', rows, ('provider_attempt_id',), run)

    def _link_receipt(self, receipt, path):
        with self.guard():
            value = self._manifest(receipt['run_id'])
            if value['issue'] != receipt['issue']: raise ValueError('receipt belongs to another ticket')
            attempt = next(a for a in value['attempts'] if a['attempt_id'] == receipt['stage_attempt_id'])
            relative = str(path.relative_to(self.root))
            if relative not in attempt['receipts']: attempt['receipts'].append(relative)
            self._save_manifest(value)

    def review_round(self, run, stage_attempt, number, min_severity, provider_verdict=None, final_verdict=None, merge=None):
        """One round per stage attempt, with deduplicated provider-call measurements.

        A partial set of phases or any unknown measurement makes that aggregate
        unknown. Retries have distinct provider attempt IDs and are counted once.
        Neither approval reuse nor gate polling calls this collector.
        """
        with self.guard():
            value = self._manifest(run)
            attempt = next(a for a in value['attempts'] if a['attempt_id'] == identifier(stage_attempt))
            if attempt['stage'] != 'code_review': raise ValueError('not a review stage')
            prior = attempt.get('review_round')
            if prior and (prior['number'] != number or prior['min_severity'] != min_severity):
                raise ValueError('review round already assigned')
            calls = {}
            for relative in attempt['receipts']:
                receipt = read(self.root / relative, {})
                if (receipt.get('run_id') == run and receipt.get('stage_attempt_id') == stage_attempt
                        and receipt.get('issue') == value['issue'] and receipt.get('stage') == 'code_review'
                        and receipt.get('phase') in REVIEW_PHASES | {'finding_verifier'} and receipt.get('record_type') == 'receipt'
                        and receipt.get('schema_version') == VERSION):
                    call_id = identifier(receipt['provider_attempt_id'])
                    if self.receipt_path(run, call_id) == self.root / relative: calls[call_id] = receipt
            required = REVIEW_PHASES - {'verdict'} if merge and merge.get('mode') == 'deterministic' else REVIEW_PHASES
            complete = required <= {r['phase'] for r in calls.values()}
            planned_checks = sum(r['basis'] in ('pending', 'model', 'invalid_result') for r in merge['verifier']) if merge else 0
            complete = complete and sum(r['phase'] == 'finding_verifier' for r in calls.values()) >= planned_checks
            measurements = [usage_metadata(r) for r in calls.values()]
            usage = {key: sum(r[key] for r in measurements) if complete and all(r[key] is not None for r in measurements)
                     else None for key in USAGE_FIELDS}
            record = dict(number=number, min_severity=min_severity, provider_verdict=provider_verdict,
                          final_verdict=final_verdict, provider_attempt_ids=sorted(calls), calls=len(calls), usage=usage)
            if merge is not None: record['merge'] = merge
            review_round_shape(record)
            attempt['review_round'] = record
            self._save_manifest(value)
            return record

    def reconcile(self, identity, current_head, schema, validate, load_result):
        path = self.receipt_path(identity['run_id'], identity['provider_attempt_id'])
        value = read(path, {})
        keys = ('run_id', 'provider_attempt_id', 'stage_attempt_id', 'issue', 'stage', 'phase', 'schema_sha256')
        if value.get('schema_version') != VERSION or any(value.get(k) != identity.get(k) for k in keys): return None
        if value.get('normalized_exit') != 0 or value.get('provider_exit') != 0 or not current_head or value.get('end_head') != current_head: return None
        result = load_result(value)
        if not isinstance(result, dict) or digest(result) != value.get('result_sha256'): return None
        validate(result, schema)
        return result

    def gate(self, run, attempt_id, path, checked_head, end_head, command, code, kind='gate', pr=None, command_id='configured-command'):
        if kind not in ('gate', 'artifact'): raise ValueError('unknown evidence kind')
        if kind == 'gate' and not head(checked_head): raise ValueError('cannot record a gate without its checked HEAD')
        if kind == 'artifact': checked_head, end_head, code = None, None, None
        stream_digest = hashlib.sha256(); size = 0
        with Path(path).open('rb') as source:
            while True:
                block = source.read(65536)
                if not block: break
                size += len(block); stream_digest.update(block)
        with self.guard():
            value = self._manifest(run)
            attempt = next(a for a in value['attempts'] if a['attempt_id'] == attempt_id)
            record = dict(kind=kind, head=head(checked_head), end_head=head(end_head),
                          command_id=command_id if command_id in COMMAND_IDS else 'configured-command',
                          command_sha256=text_digest(command), normalized_exit=code, recorded_at=self.clock(),
                          bytes=size, sha256=stream_digest.hexdigest())
            attempt['gates'].append(record)
            if pr is not None: attempt['pr'] = dict(number=pr_number(pr)); value['pr'] = attempt['pr']
            self._save_manifest(value)
            return record

    def valid_execution(self, record):
        try:
            execution_shape(record)
            return True
        except (ValueError, KeyError, TypeError): return False

    def record_publication(self, identity, stage, repo, before, after):
        # Only the executor calls this after its own successful git commit.
        # Verification and metadata publication share the evidence lock.
        with self.guard():
            receipt = read(self.receipt_path(identity['run_id'], identity['provider_attempt_id']), {})
            keys = ('run_id', 'provider_attempt_id', 'stage_attempt_id', 'issue', 'stage', 'phase', 'schema_sha256')
            if (stage not in ('implement', 'qa') or receipt.get('provider') != 'codex'
                    or receipt.get('schema_version') != VERSION or receipt.get('record_type') != 'receipt'
                    or any(receipt.get(k) != identity.get(k) for k in keys) or receipt.get('stage') != stage
                    or receipt.get('start_head') != before or receipt.get('end_head') != before
                    or not verify_publication(repo, before, after)):
                raise ValueError('unverified executor publication')
            value = self._manifest(identity['run_id'])
            attempt = next(a for a in value['attempts'] if a['attempt_id'] == identity['stage_attempt_id'])
            if (value['issue'] != identity['issue'] or attempt['stage'] != stage
                    or Path(attempt['workspace']).resolve() != Path(repo).resolve()):
                raise ValueError('publication belongs to another stage')
            record = dict(provider_attempt_id=identity['provider_attempt_id'], stage_attempt_id=identity['stage_attempt_id'],
                          pre_head=before, head=after, parent_head=before, bureau_generated=True, recorded_at=self.clock())
            publication_shape(record)
            publications = attempt.setdefault('publications', [])
            if any(p['provider_attempt_id'] == identity['provider_attempt_id'] for p in publications):
                raise ValueError('provider attempt already published')
            publications.append(record); self._save_manifest(value)
            return record

    def _publication_matches(self, identity, stage, repo, before, after, receipt):
        if repo is None: return False
        with self.guard():
            value = self._manifest(identity['run_id'])
            attempt = next(a for a in value['attempts'] if a['attempt_id'] == identity['stage_attempt_id'])
            if (value['issue'] != identity['issue'] or attempt['stage'] != stage
                    or Path(attempt['workspace']).resolve() != Path(repo).resolve()): return False
            matches = [p for p in attempt.get('publications', [])
                       if p['provider_attempt_id'] == identity['provider_attempt_id']]
            return (len(matches) == 1 and matches[0]['stage_attempt_id'] == identity['stage_attempt_id']
                    and matches[0]['pre_head'] == before and matches[0]['head'] == after
                    and type(receipt.get('ended_at')) in (int, float)
                    and receipt['ended_at'] <= matches[0]['recorded_at'] <= self.clock()
                    and verify_publication(repo, before, after))

    def match_gate_receipt(self, identity, stage, checked_head, result, repo=None):
        # Reconcile digests/identities, never retained logs or result prose.
        value = read(self.receipt_path(identity['run_id'], identity['provider_attempt_id']), {})
        keys = ('run_id', 'provider_attempt_id', 'stage_attempt_id', 'issue', 'stage', 'phase', 'schema_sha256')
        if (value.get('schema_version') != VERSION or value.get('record_type') != 'receipt' or value.get('provider') != 'codex'
                or any(value.get(k) != identity.get(k) for k in keys)
                or value.get('stage') != stage or value.get('normalized_exit') != 0 or value.get('provider_exit') != 0
                or not head(checked_head) or not head(value.get('start_head')) or value.get('start_head') != value.get('end_head')
                or value.get('result_sha256') != digest(result)): return None
        receipt_head = value['end_head']
        if checked_head == receipt_head:
            # A known publication binds this attempt to B even if someone
            # rewinds HEAD to A; only an unpublished attempt uses equality.
            with self.guard():
                manifest = self._load(self.manifest_path(identity['run_id']), 'manifest')
                if manifest and any(p['provider_attempt_id'] == identity['provider_attempt_id']
                                    for a in manifest['attempts'] if a['attempt_id'] == identity['stage_attempt_id']
                                    for p in a.get('publications', [])): return None
        if checked_head != receipt_head and not self._publication_matches(identity, stage, repo, receipt_head, checked_head, value):
            return None
        return value

    def reserve_gate_rerun(self, run, attempt_id, checked_head, command, gate_id, sources):
        # Durable before spawn: an incomplete reservation never permits a retry.
        with self.guard():
            value = self._manifest(run)
            attempt = next(a for a in value['attempts'] if a['attempt_id'] == attempt_id)
            reruns = attempt.setdefault('gate_reruns', [])
            old = next((a for a in reruns if a['head'] == checked_head and a['gate_id'] == gate_id), None)
            if old is not None: return False, old
            record = dict(attempt_id=uuid.uuid4().hex, head=checked_head, command_sha256=text_digest(command),
                          gate_id=gate_id, gate_attempt_ids=list(sources), started_at=self.clock(), result=None)
            rerun_shape(record)
            reruns.append(record); self._save_manifest(value)
            return True, record

    def finish_gate_rerun(self, run, attempt_id, reservation, result):
        with self.guard():
            value = self._manifest(run)
            attempt = next(a for a in value['attempts'] if a['attempt_id'] == attempt_id)
            record = next(a for a in attempt['gate_reruns'] if a['attempt_id'] == reservation)
            record['result'] = execution_metadata(result)
            if record['result']['attempt_id'] != reservation: raise ValueError('invalid rerun completion')
            rerun_shape(record)
            self._save_manifest(value)
            return record['result']

    def set_pr(self, run, attempt_id, pr):
        with self.guard():
            value = self._manifest(run)
            record = dict(number=pr_number(pr))
            if record['number'] is None: raise ValueError('invalid PR number')
            next(a for a in value['attempts'] if a['attempt_id'] == attempt_id)['pr'] = record
            value['pr'] = record; self._save_manifest(value)

    def open_wait(self, issue, key, kind, reason, actor, stage=None, code=None, run=None, generation_id=None, pr=None):
        ticket(issue); generation(generation_id)
        if kind not in KINDS: raise ValueError('unknown wait kind')
        key = wait_key(key)
        with self.guard():
            now = self.clock(); self._observe_ticket(issue, now, reopened=True)
            for path, value in self._records('waits/*.json', 'wait'):
                if (value['issue'] == issue and value['key'] == key and value['state'] == 'open'
                        and generation_id is not None and value.get('generation') != generation_id):
                    value.update(state='resolved', resolved_at=None, resolution_observed_at=now, resolution_basis='observed-superseded')
                    value['events'].append(dict(event='resolved', at=None, observed_at=now, basis='observed-superseded'))
                    self._write(path, value)
                    continue
                if value['issue'] == issue and value['key'] == key and value['state'] == 'open' and (generation_id is None or value.get('generation') == generation_id):
                    if code is None: code = value.get('exit_code')
                    value.update(last_observed_at=now, reason_sha256=text_digest(reason),
                                 responsible_actor=actor if actor in ACTORS else 'operator', exit_code=code, kind=kind)
                    self._write(path, value)
                    return value
            wait = uuid.uuid4().hex
            value = dict(schema_version=VERSION, record_type='wait', wait_id=wait, issue=issue,
                         key=key, kind=kind, reason_sha256=text_digest(reason), responsible_actor=actor if actor in ACTORS else 'operator',
                         stage=tag(stage), exit_code=code, run_id=identifier(run) if run else None,
                         generation=generation_id, pr=pr_number(pr), state='open', reached_at=now, last_observed_at=now,
                         last_checked_at=now, resolved_at=None, resolution_observed_at=None, resolution_basis=None,
                         events=[dict(event='reached', at=now, run_id=run)])
            self._write(self.root / 'waits' / (wait + '.json'), value)
            return value

    def resolve_waits(self, issue, keys=None, basis='observed-resume', resolved_at=None, terminal=False,
                      generation_id=None, wait_ids=None):
        ticket(issue); generation(generation_id)
        keys = None if keys is None else {wait_key(k) for k in keys}
        with self.guard():
            now = self.clock()
            # Reject stale/future authoritative observations, rather than silently
            # downgrading them into unknown-time resolutions.
            if resolved_at is not None and (type(resolved_at) not in (int, float) or not math.isfinite(resolved_at) or resolved_at > now): return 0
            changed = 0; remaining = False
            for path, value in self._records('waits/*.json', 'wait'):
                if value['issue'] != issue or value['state'] != 'open': continue
                if ((keys is not None and value['key'] not in keys)
                    or (generation_id is not None and value.get('generation') != generation_id)
                    or (wait_ids is not None and value['wait_id'] not in wait_ids)
                    or (resolved_at is not None and resolved_at < value['reached_at'])):
                    remaining = True; continue
                safe_basis = basis if basis in BASES else 'sha256:' + text_digest(basis)
                value.update(state='resolved', resolved_at=resolved_at, resolution_observed_at=now, resolution_basis=safe_basis)
                value['events'].append(dict(event='resolved', at=resolved_at, observed_at=now, basis=safe_basis))
                self._write(path, value); changed += 1
            self._observe_ticket(issue, now)
            if terminal and not remaining and changed:
                path = self.root / 'tickets' / (issue + '.json'); value = self._load(path, 'ticket')
                value['terminal_observed_at'] = now; self._write(path, value)
            return changed

    def open_waits(self):
        return [v for _, v in self._records('waits/*.json', 'wait') if v['state'] == 'open']

    def observation_batch(self, limit=4, rotation=None):
        # Selection never advances cursors. Check that the oldest cursor can
        # round-trip unchanged before trusting persisted order across processes.
        with self.guard():
            values = self.open_waits()
            issues = {v['issue'] for v in values}
            self._observation_attempts = {i: turn for i, turn in self._observation_attempts.items() if i in issues}
            issues = sorted(issues, key=lambda i: (
                min(v['last_checked_at'] for v in values if v['issue'] == i), i))
            persisted = True
            if issues:
                cursor = min((v for v in values if v['issue'] == issues[0]), key=lambda v: v['last_checked_at'])
                path = self.root / 'waits' / (cursor['wait_id'] + '.json')
                try:
                    # An unchanged cursor can fit when its next timestamp
                    # cannot. Reserve the longest finite-float JSON encoding
                    # as well, without advancing an unvisited ticket's cursor.
                    cursor_capacity = max(len(encoded(cursor)),
                                          len(encoded(dict(cursor, last_checked_at=-sys.float_info.max))))
                    self._prune(cursor_capacity, 1, writing=path)
                    self._write(path, cursor)
                    if self._load(path, 'wait', quarantine=False) != cursor:
                        raise OSError('observation cursor did not round-trip')
                except Exception:
                    warn()
                    persisted = False
            if not persisted:
                if rotation is None:
                    issues = sorted(issues, key=lambda i: (self._observation_attempts.get(i, 0), i))
                else:
                    # A shell dispatcher retains its turn across fresh stores.
                    issues = sorted(issues)
                    start = rotation % len(issues)
                    issues = issues[start:] + issues[:start]
            issues = issues[:limit]
            result = [v for v in values if v['issue'] in issues]
            return [(issue, [v for v in result if v['issue'] == issue]) for issue in issues]

    def observation_attempted(self, issue, wait_ids):
        # Remember the attempt before any lock/write that can fail. Selection
        # itself still leaves unvisited tickets untouched.
        self._observation_turn += 1
        self._observation_attempts[issue] = self._observation_turn
        with self.guard():
            for path, value in self._records('waits/*.json', 'wait'):
                if value['issue'] == issue and value['state'] == 'open' and value['wait_id'] in wait_ids:
                    value['last_checked_at'] = self.clock()
                    # A larger wait may not fit even though the oldest cursor
                    # passed the probe. Still advance every writable cursor.
                    try: self._write(path, value)
                    except Exception: warn()

    def waits(self, issue=None):
        now = self.clock()
        records = [v for _, v in self._records('waits/*.json', 'wait', quarantine=False)]
        issues = {v['issue'] for v in records} if issue is None else {ticket(issue)}
        result = []
        for key in sorted(issues):
            values = [v for v in records if v['issue'] == key]
            period = self._load(self.root / 'tickets' / (key + '.json'), 'ticket', quarantine=False) or {}
            start = max(now - RETENTION_SECONDS, period.get('first_observed_at', min((v['reached_at'] for v in values), default=now)))
            end = period.get('terminal_observed_at') or now
            intervals = sorted((max(start, v['reached_at']), min(end, v['resolved_at'] or v['resolution_observed_at'] or now)) for v in values)
            seconds = 0; last = start
            for left, right in intervals:
                seconds += max(0, right - max(left, last)); last = max(last, right)
            result.append(dict(issue=key, observed_from=start, observed_until=end, waiting_seconds=seconds,
                               waiting_share=seconds / (end - start) if end > start else 0,
                               resolution_times_unknown=sum(v['state'] == 'resolved' and v['resolved_at'] is None for v in values),
                               open_waits=[v for v in values if v['state'] == 'open']))
        return dict(schema_version=VERSION, measurement='observed intervals; unknown ends use first observation (upper bound)', tickets=result)


def review_round_main():
    """Observational shell bridge; successful provider receipts remain mandatory."""
    import argparse
    parser = argparse.ArgumentParser(description='Record metadata for the current review round')
    parser.add_argument('--repo', type=Path, required=True)
    parser.add_argument('--round', type=int)
    parser.add_argument('--min-severity', choices=('BUG', 'MINOR'))
    parser.add_argument('--signal', choices=('timeout', 'provider_error', 'review_capped', 'gate_red', 'human_hold'))
    parser.add_argument('--provider-verdict')
    parser.add_argument('--final-verdict')
    parser.add_argument('--merge-metadata', type=Path)
    args = parser.parse_args()
    run = os.environ.get('BUREAU_RUN_ID'); attempt = os.environ.get('BUREAU_STAGE_ATTEMPT_ID')
    if not run or not attempt: return
    common = subprocess.check_output(['git', '-C', str(args.repo), 'rev-parse', '--git-common-dir'], text=True).strip()
    store = Store((args.repo / common).resolve() / 'bureau')
    if args.signal:
        store.outcome_signal(run, attempt, args.signal)
        return
    store.review_round(
        run, attempt, args.round, args.min_severity,
        args.provider_verdict if args.provider_verdict in ('APPROVE', 'REQUEST_CHANGES', 'BLOCK') else None,
        args.final_verdict if args.final_verdict in ('APPROVE', 'REQUEST_CHANGES', 'BLOCK') else None,
        json.loads(args.merge_metadata.read_text()) if args.merge_metadata else None)


if __name__ == '__main__':
    observe(review_round_main)
