#!/usr/bin/env python3
"""Read-only weekly baseline from retained Bureau metadata (Python 3.9+).

This is a maintainer tool, never an installed runtime asset. Do not import the
runtime Store: even its readers can lock, prune, quarantine or observe records.
"""
import argparse
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
import json
import math
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import tempfile

sys.dont_write_bytecode = True
UTC = timezone.utc
ISSUE = re.compile(r'(?<![A-Za-z0-9])([A-Z][A-Z0-9]*-[1-9][0-9]*)(?![A-Za-z0-9])', re.I)
IDENTITY = re.compile(r'[a-f0-9]{32}')
EVIDENCE = '<git common dir>/bureau/evidence/v1'
RETAINED = 'Retained observations only; optional evidence is prunable (30 days, 128 completed runs and capacity limits).'
PR_FIELDS = 'number,mergedAt,headRefName,baseRefName,title,body,commits'
# bureau-provider.py:run keeps the enclosing attempt identity but the called
# provider's stage. spec-pipeline.sh calls research; merge_origin_main_or_abort
# and rebase-pipeline.sh call the resolver. bureau-conflicts.py:evidence writes
# resolver validation receipts under those same attempts. Upstream port/summary
# calls have their own standalone attempts, not a shared enclosing stage.
NESTED_RECEIPT_STAGES = {
    'research': frozenset(('spec',)),
    'conflict_resolver': frozenset(('ux', 'copy', 'implement', 'qa', 'code_review', 'merge', 'rebase')),
}


def timestamp(value):
    if type(value) in (int, float) and math.isfinite(value):
        try:
            return datetime.fromtimestamp(value, UTC)
        except (ValueError, OverflowError, OSError):
            return None
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
            return parsed.astimezone(UTC) if parsed.tzinfo else None
        except ValueError:
            pass
    return None


def issue_id(value):
    return value.upper() if isinstance(value, str) and ISSUE.fullmatch(value) else None


def valid_issue(row):
    return 'issue' in row and (row['issue'] is None or issue_id(row['issue']) is not None)


def amount(value):
    return value if type(value) in (int, float) and math.isfinite(value) and value >= 0 else None


def valid_pr(value):
    return value is None or isinstance(value, dict) and type(value.get('number')) is int and value['number'] > 0


def valid_manifest(row):
    attempts = row.get('attempts')
    return (valid_issue(row) and IDENTITY.fullmatch(row.get('run_id', ''))
            and valid_pr(row.get('pr')) and isinstance(attempts, list)
            and all(isinstance(a, dict) and IDENTITY.fullmatch(a.get('attempt_id', ''))
                    and isinstance(a.get('stage'), str) and isinstance(a.get('phase'), str)
                    and valid_pr(a.get('pr')) and isinstance(a.get('receipts'), list)
                    and all(isinstance(p, str) and re.fullmatch(r'runs/[a-f0-9]{32}/receipts/[a-f0-9]{32}\.json', p)
                            for p in a['receipts']) for a in attempts)
            and len({a['attempt_id'] for a in attempts}) == len(attempts))


def receipt_matches(row, run, attempt):
    return (all(row.get(k) == v for k, v in (
                ('issue', run['issue']), ('run_id', run['run_id']),
                ('stage_attempt_id', attempt['attempt_id'])))
            and (row.get('stage') == attempt['stage']
                 or attempt['stage'] in NESTED_RECEIPT_STAGES.get(row.get('stage'), ())))


def metric(status, value, sources, reason, **details):
    return dict(status=status, value=value, sources=sources, reason=reason, **details)


def missing(sources, reason, **details):
    return metric('missing', None, sources, reason, **details)


def command(argv, repo, env=None, timeout=20):
    """No files, caches, prompts or surviving API-helper children on timeout."""
    environ = dict(os.environ if env is None else env)
    for key in list(environ):
        if key.startswith('GIT_') or key in ('BASH_ENV', 'ENV', 'GH_DEBUG', 'GH_LOG_FILE', 'GH_REPO'):
            environ.pop(key, None)
    environ.update(GIT_OPTIONAL_LOCKS='0', GIT_NO_LAZY_FETCH='1',
                   GIT_TERMINAL_PROMPT='0', GH_PROMPT_DISABLED='1', PYTHONDONTWRITEBYTECODE='1')
    child = subprocess.Popen(argv, cwd=repo, env=environ, stdin=subprocess.DEVNULL,
                             stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                             text=True, start_new_session=True)
    try:
        output, _ = child.communicate(timeout=timeout)
        if child.returncode:
            raise ValueError('read command failed')
        return output
    finally:
        # A helper may outlive its parent even after stdout has closed.
        try:
            os.killpg(child.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        child.wait()


def git(repo, *args):
    return command(['git', '--no-replace-objects', '-C', str(repo), *args], repo).strip()


class Inputs:
    def __init__(self, repo):
        self.repo = Path(git(repo, 'rev-parse', '--show-toplevel')).resolve()
        self.common = (self.repo / git(self.repo, 'rev-parse', '--git-common-dir')).resolve()
        self.root = self.common / 'bureau' / 'evidence' / 'v1'
        self.sources = {}
        self.invalid_records = {}

    def source(self, name, location, reader, available, count=None, reason=None):
        self.sources[name] = dict(location=location, reader=reader, available=available,
                                  records=count, reason=reason)

    def records(self, name, pattern, kind, validator, lines=False):
        rows, invalid = [], 0
        failures = []
        files = sorted(self.root.glob(pattern)) if self.root.is_dir() else []
        for path in files:
            row = None
            try:
                if any(p.is_symlink() for p in (path, *path.parents)):
                    raise ValueError('linked evidence')
                raw = path.read_text(encoding='utf-8')
                if not lines and not raw.strip():
                    raise ValueError('empty record')
                values = raw.splitlines() if lines else [raw]
                for value in values:
                    if not value.strip():
                        continue
                    try:
                        row = json.loads(value)
                        if (not isinstance(row, dict) or type(row.get('schema_version')) is not int or row['schema_version'] != 1
                                or row.get('record_type') != kind or not validator(row)):
                            raise ValueError('invalid metadata')
                        if kind == 'manifest' and path.parent.name != row['run_id']:
                            raise ValueError('foreign manifest')
                        if kind == 'receipt' and (path.stem != row['provider_attempt_id'] or path.parent.parent.name != row['run_id']):
                            raise ValueError('foreign receipt')
                        rows.append(row)
                    except (ValueError, TypeError, KeyError):
                        invalid += 1
                        failures.append((path, row))
            except (OSError, UnicodeError, ValueError):
                invalid += 1
                failures.append((path, row))
        reason = ('Source absent.' if not files else
                  'Unreadable or invalid records; valid rows retained, coverage incomplete.' if invalid else None)
        self.source(name, EVIDENCE + '/' + pattern, 'bureau-evidence.py', bool(files) and not invalid,
                    len(rows) if files else None, reason)
        self.invalid_records[name] = failures
        return rows

    def local(self):
        manifests = self.records('manifests', 'runs/*/manifest.json', 'manifest', valid_manifest)
        outcomes = self.records('outcomes', 'stage-outcomes.jsonl', 'stage_outcome',
            lambda r: valid_issue(r) and IDENTITY.fullmatch(r.get('stage_attempt_id', ''))
            and timestamp(r.get('timestamp')) and type(r.get('normalized_exit')) is int, lines=True)
        receipts = self.records('receipts', 'runs/*/receipts/*.json', 'receipt',
            lambda r: valid_issue(r) and IDENTITY.fullmatch(r.get('provider_attempt_id', ''))
            and IDENTITY.fullmatch(r.get('run_id', '')) and IDENTITY.fullmatch(r.get('stage_attempt_id', ''))
            and isinstance(r.get('stage'), str))
        runs = {r['run_id']: r for r in manifests}
        coverage = {issue_id(r['issue']): dict(available=True, records=0, reason=None)
                    for r in manifests + receipts if r['issue'] is not None}
        unassigned = 0

        def receipt_failure(owner, reason):
            nonlocal unassigned
            if owner:
                coverage.setdefault(owner, dict(records=0)).update(available=False, reason=reason)
            else:
                unassigned += 1

        for path, row in self.invalid_records['receipts']:
            run = runs.get(path.parent.parent.name)
            owner = issue_id(run['issue']) if run else issue_id(row.get('issue')) if isinstance(row, dict) else None
            if owner:
                receipt_failure(owner, 'Unreadable or invalid ticket receipts; cost coverage incomplete.')
            else:
                unassigned += 1
        # Validate unlinked receipts too: linking is optional, identity is not.
        # Use the manifest's owner so a foreign issue field cannot poison another
        # ticket's cost or lend that ticket a mismatched provider call.
        accepted = []
        for row in receipts:
            run = runs.get(row['run_id'])
            if run:
                attempt = next((a for a in run['attempts'] if a['attempt_id'] == row['stage_attempt_id']), None)
                if not attempt or not receipt_matches(row, run, attempt):
                    receipt_failure(issue_id(run['issue']), 'Mismatched ticket receipt identity or stage; cost coverage incomplete.')
                    continue
            accepted.append(row)
            if row['issue'] is not None:
                coverage[issue_id(row['issue'])]['records'] += 1
        receipts = accepted
        retained = {f"runs/{r['run_id']}/receipts/{r['provider_attempt_id']}.json": r for r in receipts}
        for run in manifests:
            for attempt in run['attempts']:
                for path in attempt['receipts']:
                    row = retained.get(path)
                    if not row or not receipt_matches(row, run, attempt):
                        receipt_failure(issue_id(run['issue']),
                            'Missing or mismatched manifest-linked receipts; cost coverage incomplete.')
        for value in coverage.values():
            if value['available'] and not value['records']:
                value.update(available=False, reason='No retained receipts for this ticket.')
        incomplete = unassigned or any(not value['available'] for value in coverage.values())
        self.sources['receipts'].update(available=bool(receipts),
            records=len(receipts) if self.sources['receipts']['records'] is not None else None,
            reason=('Some ticket receipts are missing/invalid; see per_ticket coverage and unassigned_invalid_records.'
                    if incomplete else None if receipts else 'Source absent.'),
            per_ticket=coverage, unassigned_invalid_records=unassigned)
        dispositions = self.records('dispositions', 'review-dispositions.jsonl', 'review_disposition',
            lambda r: valid_issue(r) and timestamp(r.get('timestamp'))
            and r.get('disposition') in ('applied', 'declined', 'skip', 'deferred', 'human_merge'), lines=True)
        tickets = self.records('tickets', 'tickets/*.json', None,
            lambda r: issue_id(r.get('issue')) and timestamp(r.get('first_observed_at')))
        # Ticket observations have no record_type in the version-1 store.
        # Standalone provider calls legitimately have no ticket. Validate them
        # above, but keep their PRs, attempts and costs out of ticket metrics.
        ticket_rows = []
        for name, rows in (('manifests', manifests), ('outcomes', outcomes), ('receipts', receipts),
                           ('dispositions', dispositions), ('tickets', tickets)):
            self.sources[name]['ticketless_records'] = sum(r['issue'] is None for r in rows)
            ticket_rows.append([r for r in rows if r['issue'] is not None])
        return tuple(ticket_rows)

    def merged_prs(self):
        try:
            rows = json.loads(command(['gh', 'pr', 'list', '--state', 'merged', '--limit', '1000',
                                       '--json', PR_FIELDS], self.repo))
            if (not isinstance(rows, list) or any(not isinstance(r, dict) or type(r.get('number')) is not int or r['number'] <= 0
                    or not timestamp(r.get('mergedAt')) or any(not isinstance(r.get(k), str) for k in ('headRefName', 'title', 'body')) for r in rows)
                    or len({r['number'] for r in rows}) != len(rows)):
                raise ValueError('invalid PR metadata')
            # gh's default creation ordering cannot prove merge-window coverage
            # just because one old merge appears on a full page.
            complete = len(rows) < 1000
            self.source('prs', 'gh pr list --state merged --limit 1000', 'gh', complete, len(rows),
                        None if complete else 'PR limit reached; requested-window coverage is unproven.')
            return rows
        except (OSError, ValueError, subprocess.SubprocessError):
            self.source('prs', 'gh pr list --state merged --limit 1000', 'gh', False,
                        reason='gh unavailable, unauthenticated or merged-PR response invalid.')
            return []

    def linear(self, issues):
        """Use the existing pure snapshot, never get_issue_state or Store readers."""
        helper = Path(__file__).resolve().parents[1] / 'templates/scripts/bureau-config.sh'
        candidates = [self.repo / '.bureau.json', self.common.parent / '.bureau.json']
        config = next((p for p in candidates if p.is_file()), None)
        snapshots = {}
        reason = 'No repository configuration, trusted read helper or jq; Linear snapshots missing.'
        if config and helper.is_file() and shutil.which('jq'):
            try:
                state_ids = json.loads(config.read_text()).get('linear', {}).get('teams', [{}])[0].get('states', {})
                if not isinstance(state_ids, dict):
                    raise ValueError('invalid states')
            except (OSError, ValueError, IndexError, TypeError, AttributeError):
                state_ids = {}
            # Discard inherited runtime controls and fault-file paths. The helper
            # parses .env without sourcing it; all paths below are positional args.
            environ = {k: v for k, v in os.environ.items() if not k.startswith('BUREAU_')}
            if os.environ.get('BUREAU_ENV_FILE'): environ['BUREAU_ENV_FILE'] = os.environ['BUREAU_ENV_FILE']
            environ.update(BUREAU_CONFIG=str(config),
                           BUREAU_DRY_RUN='1', _BUREAU_LINEAR_SINGLE_ATTEMPT='1',
                           _BUREAU_LINEAR_FAULT_FILE='', BUREAU_LINEAR_MAX_TIME='10',
                           BUREAU_LINEAR_CONNECT_TIMEOUT='5')
            shell = '''source "$1" >/dev/null || exit
if [ -f "$BUREAU_ENV_FILE" ]; then bureau_load_env "$BUREAU_ENV_FILE" || exit; fi
[ -n "${LINEAR_API_KEY:-}" ] || exit 1
bureau_secret_copy API_KEY LINEAR_API_KEY || exit
bureau_wait_snapshot "$2"
'''
            for issue in sorted(issues):
                try:
                    value = json.loads(command(['bash', '--noprofile', '--norc', '-c', shell,
                                                 'baseline', str(helper), issue], self.repo, environ))
                    if (not isinstance(value, dict) or issue_id(value.get('identifier')) != issue
                            or not isinstance(value.get('state'), dict)
                            or not isinstance(value['state'].get('name'), str)):
                        raise ValueError('invalid snapshot')
                    for key, name in (('done', 'Done'), ('cancelled', 'Cancelled'), ('canceled', 'Canceled'), ('duplicate', 'Duplicate')):
                        if state_ids.get(key) and value['state'].get('id') == state_ids[key]:
                            value['state']['name'] = name
                    snapshots[issue] = value
                except (OSError, ValueError, subprocess.SubprocessError):
                    pass
            reason = 'Some known tickets lack a usable Linear snapshot or credentials.'
        complete = bool(issues) and len(snapshots) == len(issues)
        self.source('linear', 'bureau-config.sh:bureau_wait_snapshot (current states, completedAt, canceledAt)',
                    'trusted template read helper; no observations', complete,
                    len(snapshots) if issues else None, None if complete else reason)
        return snapshots

    def cost_log(self, issue):
        directory = Path(os.environ.get('BUREAU_COST_DIR') or Path.home() / '.bureau' / 'cost')
        try:
            rows = [json.loads(line) for line in (directory / (issue + '.jsonl')).read_text().splitlines() if line.strip()]
            if any(not isinstance(r, dict) or r.get('issue') != issue for r in rows):
                raise ValueError('invalid cost rows')
            self.sources['cost_log']['per_ticket'][issue] = dict(available=True, records=len(rows), reason=None)
            return rows, None
        except (OSError, ValueError, UnicodeError):
            self.sources['cost_log']['per_ticket'][issue] = dict(available=False, records=None,
                reason='Cost log absent, unreadable or invalid.')
            return [], 'Cost log absent, unreadable or invalid.'


def references(text):
    return {match.upper() for match in ISSUE.findall(text or '')}


def link_prs(manifests, prs, issues):
    links = defaultdict(set)
    for run in manifests:
        for record in [run] + run['attempts']:
            number = (record.get('pr') or {}).get('number')
            if type(number) is int:
                links[number].add(issue_id(run['issue']))
    result, ambiguous = {}, []
    for pr in prs:
        # A body can point back to an earlier defect; it never establishes PR ownership.
        owners = links[pr['number']] or (references(pr.get('headRefName')) & issues)
        if len(owners) == 1:
            result[pr['number']] = next(iter(owners))
        elif owners:
            ambiguous.append(pr['number'])
    return result, ambiguous


def verified_integration_merge(repo, pr, commit):
    """Verify a plain base merge without writing objects in the target repo."""
    oid = commit.get('oid')
    if repo is None or not isinstance(oid, str) or not re.fullmatch(r'(?:[a-f0-9]{40}|[a-f0-9]{64})', oid):
        return None
    try:
        row = git(repo, 'show', '-s', '--format=%H %P %T', oid).split()
        if len(row) != 4:  # Exactly two parents; ordinary edits and octopus merges do not qualify.
            return False
        _, first, second, tree = row
        if not any(c.get('oid') == first for c in pr['commits']):
            return False
        base_name = pr.get('baseRefName')
        if not isinstance(base_name, str) or not base_name:
            return None
        base_ref = 'refs/remotes/origin/' + base_name
        git(repo, 'check-ref-format', base_ref)
        base = git(repo, 'rev-parse', '--verify', base_ref + '^{commit}')
        if git(repo, 'merge-base', second, base) != second:
            return False
        objects = (Path(repo) / git(repo, 'rev-parse', '--git-path', 'objects')).resolve()
        # merge-tree writes objects. Give it a separate bare store with only
        # read access to the target objects, no templates or configured drivers.
        with tempfile.TemporaryDirectory(prefix='bureau-baseline-') as directory:
            scratch = Path(directory) / 'objects.git'
            isolated = ['env', 'GIT_CONFIG_NOSYSTEM=1', 'GIT_CONFIG_GLOBAL=' + os.devnull,
                        'git', '--no-replace-objects', '-c', 'core.attributesFile=' + os.devnull]
            command(isolated + ['init', '-q', '--bare', '--template=',
                               '--object-format=' + ('sha256' if len(oid) == 64 else 'sha1'), str(scratch)], repo)
            (scratch / 'objects/info/alternates').write_text(json.dumps(str(objects), ensure_ascii=False) + '\n')
            merged = command(isolated + ['--git-dir=' + str(scratch), 'merge-tree', '--write-tree', first, second], repo).strip()
            return merged == tree
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def attributed(pr, repo=None):
    commits = pr.get('commits')
    if not isinstance(commits, list) or not commits or len(commits) >= 100:
        return None
    for commit in commits:
        if not isinstance(commit, dict) or any(not isinstance(commit.get(k), str) for k in ('messageHeadline', 'messageBody')):
            return None
    flags = [bool(re.search(r'^Bureau-Generated:[ \t]*true[ \t]*$', c['messageBody'], re.M)
               or re.search(r'^Co-authored-by:.*\bclaude\b', c['messageBody'], re.M | re.I)
               or re.fullmatch(r'[A-Z]+-[0-9]+: spec artifacts', c['messageHeadline'], re.I))
             or verified_integration_merge(repo, pr, c) for c in commits]
    return False if False in flags else None if None in flags else True


def ticket_cost(inputs, issue, merge_time, receipts):
    source = ['receipts']
    legacy, error = inputs.cost_log(issue)
    coverage = inputs.sources['receipts']['per_ticket'].get(issue, inputs.sources['receipts'])
    if not coverage['available']:
        reason = coverage['reason']
        if legacy:
            reason += ' Legacy log alone has no repository/pre-merge correlation.'
        return missing(source + ['cost_log'], reason)
    calls = {}
    for receipt in receipts:
        if issue_id(receipt['issue']) != issue:
            continue
        # The resolver's external validation observation is not a provider call
        # and intentionally has no provider timing or usage measurements.
        if receipt.get('stage') == 'conflict_resolver' and receipt.get('phase') == 'validation':
            continue
        end = timestamp(receipt.get('ended_at'))
        start = timestamp(receipt.get('started_at'))
        if (start and start > merge_time) or (end and end > merge_time):
            continue
        if not end:
            return missing(source, 'A retained provider attempt has no completion timestamp; pre-merge cost is incomplete.')
        identity = receipt['provider_attempt_id']
        if identity in calls and calls[identity] != receipt:
            return missing(source, 'Conflicting receipts for one provider attempt.')
        calls[identity] = receipt
    if calls:
        costs = []
        for identity, receipt in calls.items():
            usage = receipt.get('usage') or {}
            cost = amount(usage.get('total_cost_usd')) if isinstance(usage, dict) else None
            # Untimed legacy rows can only fill an exact provider identity.
            matches = [r for r in legacy if r.get('provider_attempt_id') == identity and r.get('stage') == receipt.get('stage')]
            if cost is None and len(matches) == 1:
                cost = amount(matches[0].get('estimated_cost_usd', matches[0].get('cost_usd')))
                if 'cost_log' not in source:
                    source.append('cost_log')
            costs.append(cost)
        if any(c is None for c in costs):
            return missing(source, 'Some retained provider attempts have unknown cost; no zero substitution.',
                           measured_attempts=sum(c is not None for c in costs), attempts=len(costs))
        return metric('estimated', round(sum(costs), 6), source,
                      'Cumulative retained provider estimates through merge; excludes human time and billing reconciliation. ' + RETAINED,
                      attempts=len(costs), currency='USD')
    if error:
        return missing(source + ['cost_log'], error)
    # Old logs are untimed and not repository-scoped. Never bucket a lifetime
    # total into a merge week or add it on top of receipts.
    return missing(source + ['cost_log'], 'Legacy log has no correlated provider attempts/timestamps; repository and pre-merge attribution unavailable.',
                   legacy_rows=len(legacy))


def build(inputs, weeks, now=None):
    now = now or datetime.now(UTC)
    monday = (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    start = monday - timedelta(weeks=weeks - 1)
    manifests, outcomes, receipts, dispositions, tickets = inputs.local()
    issues = {issue_id(r['issue']) for r in manifests + outcomes + receipts + dispositions + tickets}
    issues.discard(None)
    prs = inputs.merged_prs()
    owners, ambiguous = link_prs(manifests, prs, issues)
    corrupt_manifests = not inputs.sources['manifests']['available'] and inputs.sources['manifests']['records'] is not None
    inputs.source('pr_links', 'retained manifest PR numbers, otherwise known-ticket branch identifiers',
                  'unambiguous local ticket-to-PR correlation', not ambiguous and not corrupt_manifests, len(owners),
                  'Conflicting PR ownership or corrupt manifests; cohort incomplete.' if ambiguous or corrupt_manifests else None)
    snapshots = inputs.linear(issues)
    inputs.source('cost_log', 'BUREAU_COST_DIR/<issue>.jsonl (default: user cost directory)',
                  'bureau-config.sh:record_stage_cost; identity-matched fallback only', None,
                  reason='Legacy log is outside the repository, untimed and not repository-scoped; inspected per merged ticket.')
    inputs.sources['cost_log']['per_ticket'] = {}
    attempts = [(issue_id(r['issue']), r['run_id'], a) for r in manifests for a in r['attempts']]
    # Manifest receipt links are best effort; retained calls still identify the
    # enclosing attempt even when the optional link update failed.
    receipt_attempts = {(issue_id(r['issue']), r['run_id'], r['stage_attempt_id']) for r in receipts}
    observed = defaultdict(list)
    for row in tickets:
        observed[issue_id(row['issue'])].append(timestamp(row['first_observed_at']))
    for run in manifests:
        if timestamp(run.get('started_at')):
            observed[issue_id(run['issue'])].append(timestamp(run['started_at']))
    origins = {}
    for pr in sorted(prs, key=lambda p: timestamp(p['mergedAt'])):
        if pr['number'] in owners:
            origins.setdefault(owners[pr['number']], pr)
    result, attributions = [], {}
    for offset in range(weeks):
        begin = start + timedelta(weeks=offset)
        end = begin + timedelta(weeks=1)
        def inside(value):
            point = timestamp(value)
            return point is not None and begin <= point < end and point <= now
        merged = [p for p in prs if p['number'] in owners and inside(p['mergedAt'])]
        merged_issues = sorted({owners[p['number']] for p in merged})
        admissions = {issue for issue, _, a in attempts if a.get('stage') == 'spec' and inside(a.get('started_at'))}
        metrics = {}
        metrics['tickets_eligible'] = missing(['manifests', 'outcomes', 'linear'],
            'No historical eligibility census or label/state-entry history; current state cannot reconstruct a week.',
            observed_spec_starts=len(admissions) if inputs.sources['manifests']['available'] else None)
        # A delayed Done transition must not count a prior week's merge twice.
        completed_merges = {i for i, p in origins.items() if inside(p['mergedAt'])}
        done = {i for i, s in snapshots.items() if i not in origins and s['state']['name'] == 'Done' and inside(s.get('completedAt'))}
        undated_done = [i for i, s in snapshots.items() if s['state']['name'] == 'Done' and not timestamp(s.get('completedAt'))]
        metrics['tickets_completed'] = (metric('estimated', len(done | completed_merges), ['prs', 'pr_links', 'linear'],
            'Distinct known tickets by first known PR merge, otherwise current Done/completedAt; delayed Done is not counted twice. ' + RETAINED,
            observed_merged_tickets=merged_issues, linear_only_completed=sorted(done))
            if inputs.sources['prs']['available'] and inputs.sources['pr_links']['available']
               and inputs.sources['linear']['available'] and issues and not undated_done
            else missing(['prs', 'pr_links', 'linear'],
                'Merged PRs, local provenance or dated Linear completion snapshots missing/incomplete.', observed_merged_tickets=merged_issues))
        cancelled = [i for i, s in snapshots.items() if s['state']['name'] in ('Cancelled', 'Canceled', 'Duplicate')
                     and inside(s.get('canceledAt'))]
        undated = [i for i, s in snapshots.items() if s['state']['name'] in ('Cancelled', 'Canceled', 'Duplicate')
                   and not timestamp(s.get('canceledAt'))]
        metrics['tickets_abandoned'] = (metric('estimated', len(cancelled), ['linear'],
            'Known tickets currently cancelled/duplicate, bucketed by canceledAt; holds and failed stages are not abandonment. ' + RETAINED)
            if inputs.sources['linear']['available'] and not undated else missing(['linear'],
                'Current terminal snapshots or cancellation timestamps missing; observed holds/exits cannot prove abandonment.',
                observed_cancelled=len(cancelled) if snapshots else None, undated_tickets=undated))
        attrs = {str(p['number']): attributed(p, inputs.repo) for p in merged}
        attributions.update(attrs)
        metrics['merged_without_human_edit'] = (metric('estimated', sum(v is True for v in attrs.values()), ['prs', 'pr_links', 'attribution'],
            'PRs whose commits carry Bureau attribution or are verified base integrations; this is a proxy, not proof of no human editing.', per_pr=attrs)
            if merged and inputs.sources['prs']['available'] and inputs.sources['pr_links']['available'] and all(v is not None for v in attrs.values())
            else missing(['prs', 'pr_links', 'attribution'], 'No complete merged cohort, or commit attribution unavailable (100-commit cap; base merges need local objects/base refs and Git 2.38+).', per_pr=attrs))
        rounds, costs, elapsed = {}, {}, {}
        for issue in merged_issues:
            merge_time = min(timestamp(p['mergedAt']) for p in merged if owners[p['number']] == issue)
            review = {(run, a.get('attempt_id')): a for i, run, a in attempts if i == issue
                      and a.get('stage') == 'code_review' and a.get('phase') == 'stage'
                      and timestamp(a.get('started_at')) and timestamp(a['started_at']) <= merge_time
                      # Approval reuse and gate polling reserve no round and
                      # invoke no review providers. Receipts without a round
                      # still indicate incomplete metadata, never zero work.
                      and ('review_round' in a or a['receipts'] or (i, run, a['attempt_id']) in receipt_attempts)}
            if not inputs.sources['manifests']['available'] or not review or any(not isinstance(a.get('review_round'), dict)
                                 or type(a['review_round'].get('number')) is not int or a['review_round']['number'] < 1 for a in review.values()):
                rounds[issue] = missing(['manifests', 'receipts'], 'Retained review-round metadata missing; absence is not zero rounds.')
            else:
                rounds[issue] = metric('estimated', len(review), ['manifests', 'receipts'],
                    'Distinct recorded review rounds through merge, including incomplete rounds; '
                    'provider-free attempts without round metadata consume no round. ' + RETAINED)
            costs[issue] = ticket_cost(inputs, issue, merge_time, receipts)
            points = [point for point in observed[issue] if point <= merge_time]
            elapsed[issue] = (metric('estimated', round((merge_time - min(points)).total_seconds(), 3), ['tickets', 'manifests', 'prs'],
                'First retained observation to PR merge; this is not a Triage-entry timestamp.', unit='seconds')
                if points else missing(['tickets', 'manifests', 'prs'], 'No retained start observation before merge.'))
        for key, values, sources in (('review_rounds_per_ticket', rounds, ['manifests', 'receipts']),
                                    ('cost_per_merged_ticket', costs, ['receipts', 'cost_log'])):
            sources = ['prs', 'pr_links'] + (sorted({s for v in values.values() for s in v['sources']}) if values else sources)
            known = [v['value'] for v in values.values() if v['status'] != 'missing']
            metrics[key] = (metric('estimated', round(sum(known) / len(known), 6), sources,
                'Mean for distinct tickets merged this week; per-ticket details carry coverage limits.', per_ticket=values)
                if values and len(known) == len(values) and inputs.sources['prs']['available'] and inputs.sources['pr_links']['available'] else missing(sources,
                    'No complete merged cohort or at least one ticket lacks measurements; no partial mean.', per_ticket=values))
        metrics['triage_to_merge'] = missing(['tickets', 'manifests', 'linear', 'prs'],
            'Bureau records first observation/spec execution, not Triage entry; current Linear snapshots have no transition history.',
            first_observed_to_merge=elapsed)
        defects = []
        for pr in prs:
            if not inside(pr['mergedAt']) or not re.match(rf'^(?:{ISSUE.pattern}:[ \t]*)?(?:fix|bugfix|hotfix|revert)(?:\([^)]*\))?!?:', pr.get('title') or '', re.I):
                continue
            refs = references(pr.get('body')) | references(pr.get('title'))
            numbers = {int(n) for n in re.findall(r'(?<![\w/])#([1-9][0-9]*)\b', pr.get('body') or '')}
            for original in prs:
                issue = owners.get(original['number'])
                if not issue:
                    continue
                if (timestamp(original['mergedAt']) < timestamp(pr['mergedAt'])
                        and (issue in refs or original['number'] in numbers)):
                    defects.append(dict(fix_pr=pr['number'], original_pr=original['number'], ticket=issue))
        metrics['escaped_defects'] = (metric('estimated', len({d['fix_pr'] for d in defects}), ['prs', 'pr_links'],
            'Merged fix/bugfix/hotfix/revert PR titles with an explicit back-reference to an earlier known Bureau ticket/PR; references do not prove causality.',
            links=defects) if inputs.sources['prs']['available'] and inputs.sources['pr_links']['available'] and origins else missing(['prs', 'pr_links'],
                'Merged-PR history or an earlier Bureau merge is unavailable; no zero-defect claim.'))
        result.append(dict(week=begin.strftime('%G-W%V'), start=begin.isoformat(), end_exclusive=end.isoformat(),
            metrics=metrics, observations=dict(
                stage_outcomes=dict(Counter(str(r['normalized_exit']) for r in outcomes if inside(r['timestamp'])))
                    if inputs.sources['outcomes']['available'] else None,
                review_dispositions=dict(Counter(r['disposition'] for r in dispositions if inside(r['timestamp'])))
                    if inputs.sources['dispositions']['available'] else None)))
    attribution_complete = bool(attributions) and all(v is not None for v in attributions.values())
    inputs.source('attribution', 'PR commit messages; local Git objects and origin PR-base refs',
                  'git show/merge-base; merge-tree in a separate temporary object store', attribution_complete, len(attributions),
                  None if attribution_complete else 'No merged cohort or some commit attribution cannot be verified.')
    cost_sources = inputs.sources['cost_log']['per_ticket']
    inputs.sources['cost_log']['available'] = bool(cost_sources) and all(v['available'] for v in cost_sources.values())
    return dict(schema_version=1, repository=inputs.common.parent.name, generated_at=now.isoformat(),
                week_basis='UTC ISO weeks, oldest first; current week is partial. Completion/review/cost cohorts use PR merge week; defects use fix merge week.',
                limits=[RETAINED, 'No fetch, target-repository writes, runtime Store readers or Linear mutations; only local metadata and gh/Linear queries.',
                        'PR history is limited to 1000 merges; commit attribution is unavailable at 100 commits. Missing sources never imply zero.'],
                sources=inputs.sources, weeks=result)


def markdown(report):
    def cell(value):
        return str(value).replace('|', '\\|').replace('\n', ' ')
    lines = [f"# Bureau baseline: {cell(report['repository'])}", '', report['week_basis'], '']
    for week in report['weeks']:
        lines += [f"## {week['week']}", '', '| Metric | Value | Quality | Sources / reason |', '|---|---:|---|---|']
        for name, value in week['metrics'].items():
            shown = 'missing' if value['status'] == 'missing' else value['value']
            lines.append('| ' + ' | '.join(map(cell, [name, shown, value['status'], ', '.join(value['sources']) + ': ' + value['reason']])) + ' |')
            details = {k: v for k, v in value.items() if k not in ('status', 'value', 'sources', 'reason')}
            if details:
                lines.append('| details | ' + cell(json.dumps(details, sort_keys=True)) + ' | | |')
        lines += ['', 'Retained stage outcomes / dispositions: ' + json.dumps(week['observations'], sort_keys=True), '']
    lines += ['## Sources and limits', '']
    for name, value in report['sources'].items():
        lines.append(f"- {name}: {value['location']}; reader: {value['reader']}; "
                     + ('available' if value['available'] else 'missing/incomplete') + ('. ' + value['reason'] if value['reason'] else '.'))
    lines += [''] + ['- ' + limit for limit in report['limits']]
    return '\n'.join(lines) + '\n'


def main(now=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo', required=True, type=Path)
    parser.add_argument('--weeks', type=int, default=4, help='ISO weeks including the current week (default: 4)')
    parser.add_argument('--format', choices=('md', 'json'), default='md')
    args = parser.parse_args()
    if not 1 <= args.weeks <= 520:
        parser.error('--weeks must be between 1 and 520')
    try:
        report = build(Inputs(args.repo.resolve()), args.weeks, now=now)
    except (OSError, ValueError, subprocess.SubprocessError):
        parser.exit(1, 'bureau baseline: repository metadata unavailable or invalid\n')
    print(json.dumps(report, indent=2, sort_keys=True, allow_nan=False) if args.format == 'json' else markdown(report), end='\n' if args.format == 'json' else '')


if __name__ == '__main__':
    main()
