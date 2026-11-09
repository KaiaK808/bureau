#!/usr/bin/env python3
"""Render metadata-only acceptance briefs; retry ticket and PR delivery independently."""
import argparse
from collections import Counter
import fcntl
import fnmatch
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
from urllib.parse import quote

sys.dont_write_bytecode = True


class PublicationInterrupted(Exception):
    """Separate from InterruptedError, which subprocess/select may retry."""


def sibling(name):
    spec = importlib.util.spec_from_file_location(name.replace('-', '_'), Path(__file__).with_name(name + '.py'))
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    return module


e = sibling('bureau-evidence')
p = sibling('bureau-provider')
paths = sibling('bureau-paths')

# Slashless globs also match basenames. Sensitive defaults cannot be removed by
# configuration; additional patterns can cover repository-specific conventions.
SENSITIVE_PATHS = (
    '*.json', '*.toml', '*.yaml', '*.yml', '*.ini', '*.cfg', '*.config', '*.config.*', '*.properties',
    '.editorconfig', '.gitignore', '.gitattributes', '.gitmodules',
    'config/*', '**/config/*', '.config/*', '**/.config/*', '.*rc', '.*rc.*',
    'Dockerfile*', 'Makefile', 'package.json', 'package-lock.json', 'npm-shrinkwrap.json',
    'yarn.lock', 'pnpm-lock.yaml', 'bun.lock*', 'requirements*.txt', 'requirements*.in', 'setup.py', 'Pipfile*',
    'poetry.lock', 'uv.lock', 'Cargo.toml', 'Cargo.lock', 'go.mod', 'go.sum', 'go.work*',
    'Gemfile*', 'composer.json', 'composer.lock', '*.csproj', 'pom.xml',
    'build.gradle*', 'gradle.lockfile', 'mix.exs', 'mix.lock', 'Package.swift',
    'Package.resolved', '*.lock', '.github/workflows/*', '**/.github/workflows/*',
    '.gitlab-ci*', 'Jenkinsfile', '.circleci/*', '**/.circleci/*', 'azure-pipelines*',
    '.env', '.env.*', '*.env', '*.pem', '*.key', '*.p12', '*.pfx', '*credentials*', '*secrets*',
    'CLAUDE*.md', 'AGENTS*.md', '.claude/*', '**/.claude/*', '.codex/*', '**/.codex/*',
    '.agents/*', '**/.agents/*', '.mcp.json', 'scripts/bureau-stage.md', '**/scripts/bureau-stage.md',
)
RISK_DEFAULTS = dict(low_max_changed_lines=100, low_max_files=5, high_min_changed_lines=1000,
                     docs_patterns=['docs/*', '**/docs/*', '*.md', '*.rst'],
                     test_patterns=['tests/*', '**/tests/*', 'test/*', '**/test/*',
                                    'test_*.py', '*_test.py', '*.test.*', '*.spec.*'],
                     sensitive_patterns=[])


class ConfigError(ValueError):
    pass


def risk_settings(config):
    value = config.get('agents', {}).get('risk_tier', {})
    if not isinstance(value, dict) or set(value) - set(RISK_DEFAULTS):
        raise ConfigError('invalid agents.risk_tier')
    result = {**RISK_DEFAULTS, **value}
    for key in ('low_max_changed_lines', 'low_max_files', 'high_min_changed_lines'):
        if type(result[key]) is not int or not 0 <= result[key] <= 10**9:
            raise ConfigError('invalid agents.risk_tier thresholds')
    if result['low_max_changed_lines'] >= result['high_min_changed_lines']:
        raise ConfigError('invalid agents.risk_tier threshold order')
    for key in ('docs_patterns', 'test_patterns', 'sensitive_patterns'):
        patterns = result[key]
        if (not isinstance(patterns, list) or len(patterns) > 128 or any(
                not isinstance(v, str) or not 0 < len(v) <= 280 or v != v.strip()
                or v.startswith(('/', '~', '-')) or '..' in v.split('/')
                or re.search(r'[\x00-\x20\x7f\\]', v) for v in patterns)):
            raise ConfigError('invalid agents.risk_tier patterns')
    return result


def configuration(repo):
    explicit = os.environ.get('BUREAU_CONFIG')
    candidates = [Path(explicit)] if explicit else [repo / '.bureau.json',
                        (repo / git(repo, 'rev-parse', '--git-common-dir')).resolve().parent / '.bureau.json']
    for path in candidates:
        if path.is_file(): return json.loads(path.read_text())
    return {}


def matches(path, patterns):
    return any(fnmatch.fnmatchcase(path, pattern) or
               ('/' not in pattern and fnmatch.fnmatchcase(path.rsplit('/', 1)[-1], pattern)) for pattern in patterns)


def diff_size(repo, base, head):
    entries = git_bytes(repo, 'diff', '--no-ext-diff', '--no-textconv', '--no-renames',
                        '--numstat', '-z', base, head, '--').split(b'\0')
    changed = []; insertions = deletions = binary = 0
    for entry in filter(None, entries):
        added, removed, path = entry.split(b'\t', 2)
        changed.append(path.decode('utf-8'))
        if added == removed == b'-': binary += 1
        else: insertions += int(added); deletions += int(removed)
    return dict(paths=changed, files=len(changed), insertions=insertions, deletions=deletions, binary=binary)


def path_list(items):
    safe = [path for path in items if e.disposition_path(path)]
    shown = ', '.join('`' + path + '`' for path in safe[:20]) or 'none'
    if len(safe) > 20: shown += f'; {len(safe) - 20} additional paths'
    if len(items) > len(safe): shown += f'; {len(items) - len(safe)} paths withheld (unsafe metadata)'
    return shown


def risk_tier(size, settings):
    if size is None: return 'normal', 'diff metadata: missing; low cannot be established'
    changed = size['paths']; lines = size['insertions'] + size['deletions']
    sensitive = [path for path in changed if matches(path, SENSITIVE_PATHS + tuple(settings['sensitive_patterns']))]
    facts = f'{size["files"]} files, {lines} changed lines, {size["binary"]} binary files'
    if sensitive or lines >= settings['high_min_changed_lines']:
        if sensitive: facts += '; sensitive paths: ' + path_list(sensitive)
        if lines >= settings['high_min_changed_lines']: facts += f'; changed lines >= {settings["high_min_changed_lines"]}'
        return 'high', facts
    doc_tests = sum(matches(path, settings['docs_patterns'] + settings['test_patterns']) for path in changed)
    facts += (f'; docs/test paths={doc_tests}/{len(changed)}, sensitive paths=0; '
              f'low limits: files <= {settings["low_max_files"]}, changed lines <= {settings["low_max_changed_lines"]}; '
              f'high threshold: {settings["high_min_changed_lines"]} changed lines')
    low = (bool(changed) and doc_tests == len(changed) and size['binary'] == 0
           and size['files'] <= settings['low_max_files'] and lines <= settings['low_max_changed_lines'])
    return ('low' if low else 'normal'), facts


def ci_status(repo, url, head):
    """Query the immutable commit, never a possibly newer PR head. No prose."""
    host, owner, name = url.removeprefix('https://').split('/')[:3]
    try:
        code, output = bounded(['gh', 'api', '--hostname', host, '--paginate', '--slurp',
                                f'repos/{owner}/{name}/commits/{head}/check-runs?per_page=100'], repo)
        if code: return None
        pages = json.loads(output)
        if not isinstance(pages, list) or not pages: return None
        counts = Counter()
        for page in pages:
            if not isinstance(page, dict) or not isinstance(page.get('check_runs'), list): return None
            for check in page['check_runs']:
                if not isinstance(check, dict) or check.get('head_sha') != head: return None
                conclusion = check.get('conclusion')
                if conclusion is None and check.get('status') in ('queued', 'in_progress', 'waiting', 'requested', 'pending'):
                    conclusion = 'pending'
                if conclusion not in ('success', 'failure', 'neutral', 'cancelled', 'skipped', 'timed_out',
                                      'action_required', 'stale', 'startup_failure', 'pending'): return None
                counts[conclusion] += 1
        return ', '.join(f'{key}={counts[key]}' for key in sorted(counts)) or '0 check runs'
    except (OSError, ValueError, TypeError, subprocess.SubprocessError): return None


def spending(repo, issue, config, attempts, receipts, head, base):
    enabled = os.environ.get('BUREAU_COST_TRACKING') == '1' or config.get('session', {}).get('cost_tracking') is True
    if not enabled: return ['- Tokens spent: missing (cost tracking off).']
    calls = {}
    completed = {(run, attempt['attempt_id']) for attempt, run in attempts if attempt.get('final_head') == head}
    for _, receipt in receipts:  # Already linked to this PR's stage attempts.
        identity = receipt.get('provider_attempt_id'); stage = receipt.get('stage')
        if (identity and stage in e.STAGES and
                (review_matches(repo, receipt, head, base) if stage == 'code_review' else
                 receipt.get('end_head') == head or (receipt.get('run_id'), receipt.get('stage_attempt_id')) in completed)):
            calls[identity] = receipt
    legacy = []
    try:
        directory = Path(os.environ.get('BUREAU_COST_DIR') or Path.home() / '.bureau/cost')
        for line in (directory / (issue + '.jsonl')).read_text().splitlines():
            row = json.loads(line)
            if isinstance(row, dict) and row.get('issue') == issue and row.get('stage') in e.STAGES:
                legacy.append((row, e.usage_metadata(dict(usage=row,
                    total_cost_usd=row.get('estimated_cost_usd', row.get('cost_usd'))))))
    except (OSError, ValueError): legacy = []
    rows = []; source = 'PR/HEAD receipts; one entry per provider attempt'
    metrics = ('input_tokens', 'output_tokens', 'total_cost_usd'); fallback = 0
    for identity, receipt in calls.items():
        usage = e.usage_metadata(receipt)
        # Legacy rows normally lack identity. Never guess a match by stage.
        matching = [value for row, value in legacy if row.get('provider_attempt_id') == identity
                    and row['stage'] == receipt['stage']]
        if all(usage[key] is None for key in metrics) and len(matching) == 1:
            usage = matching[0]; fallback += 1
        rows.append((receipt['stage'], usage))
    if fallback: source += f'; legacy cost log fallback for {fallback} attempt' + ('s' if fallback != 1 else '')
    if not calls:
        rows = [(row['stage'], usage) for row, usage in legacy]
        source = 'legacy per-issue cost log fallback; PR/HEAD correlation unavailable'
    if not rows: return ['- Tokens spent: missing.']
    result = [f'- Tokens spent ({source}):']
    for stage in sorted({stage for stage, _ in rows}) + ['total']:
        selected = [usage for tag, usage in rows if stage == 'total' or tag == stage]
        values = {}
        for key in metrics:
            values[key] = sum(row[key] for row in selected) if all(row[key] is not None for row in selected) else None
        measured = sum(all(row[key] is not None for key in metrics) for row in selected)
        coverage = f'; partial: {measured} of {len(selected)} attempts measured' if measured < len(selected) else ''
        cost = f'${values["total_cost_usd"]:.6f}' if values['total_cost_usd'] is not None else 'missing'
        result.append(f'  - {stage}: input={values["input_tokens"] if values["input_tokens"] is not None else "missing"}, '
                      f'output={values["output_tokens"] if values["output_tokens"] is not None else "missing"}, estimated cost={cost}{coverage}.')
    return result


def sibling_open_points(repo, receipts, head):
    outside = set(); ancestors = {head: True}
    for _, receipt in receipts:
        if receipt.get('stage') != 'implement' or receipt.get('normalized_exit') != 0 or receipt.get('provider_exit') != 0: continue
        reported = receipt.get('sibling_out_of_scope_paths'); checked = e.head(receipt.get('end_head'))
        if (not isinstance(reported, list) or len(reported) > 1024 or not checked
                or not all(e.disposition_path(path) for path in reported)): continue
        if checked not in ancestors:
            try: git(repo, 'merge-base', '--is-ancestor', checked, head); ancestors[checked] = True
            except (OSError, subprocess.SubprocessError): ancestors[checked] = False
        if ancestors[checked]: outside.update(reported)
    if not outside: return []
    return ['- Reported out-of-scope sibling sites: ' + path_list(sorted(outside)) +
            '. Advisory open points, not revalidated; ticket scope and overlap claims are unchanged.']


def git(repo, *args):
    return git_bytes(repo, *args).decode().strip()


def git_bytes(repo, *args):
    command = ['git', '-C', str(repo), *args]
    return subprocess.check_output(command, stderr=subprocess.DEVNULL, timeout=10, env=p.process_env(command))


def bounded(command, repo, body=None, diagnostics=False):
    """Kill the whole API-helper group on timeout, including a surviving curl."""
    # The trusted Linear shell helper needs its credentials. Git/gh follow
    # the shared Python policy, including remote GitHub authentication.
    environ = p.process_env(command) if Path(command[0]).name == 'gh' else dict(os.environ)
    for name in ('BASH_ENV', 'ENV'): environ.pop(name, None)
    child = subprocess.Popen(command, cwd=repo, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                             stderr=None if diagnostics else subprocess.DEVNULL, text=True, start_new_session=True, env=environ)
    try:
        output, _ = child.communicate(body, timeout=45)
        if child.returncode == 130 or child.returncode < 0:
            raise PublicationInterrupted('acceptance helper interrupted')
        return child.returncode, output
    finally:
        pending = sys.exc_info()[1]
        try:
            p.signal_group(child, signal.SIGKILL)
            child.communicate(timeout=1)
        except (OSError, subprocess.TimeoutExpired) as exc:
            if not isinstance(pending, (PublicationInterrupted, subprocess.TimeoutExpired)):
                raise
            print('acceptance helper termination uncertain: ' + str(exc), file=sys.stderr)
        finally:
            if child.stdout is not None: child.stdout.close()


def evidence_ref(store, path):
    """Only store-relative, validated identifiers may leave the local store."""
    relative = path.resolve().relative_to(store.root.resolve()).as_posix()
    match = re.fullmatch(r'runs/([a-f0-9]{32})/manifest.json', relative)
    if match: return '`run ' + match[1] + '/manifest`'
    match = re.fullmatch(r'runs/([a-f0-9]{32})/receipts/([a-f0-9]{32}).json', relative)
    if match: return '`receipt ' + match[2] + '` (`run ' + match[1] + '`)'
    match = re.fullmatch(r'waits/([a-f0-9]{32}).json', relative)
    if match: return '`wait ' + match[1] + '`'
    raise ValueError('unrecognized evidence identifier')


def review_matches(repo, receipt, head, base):
    """A local validation merge must prove both pinned inputs and its parents."""
    if receipt.get('end_head') == head: return True  # Legacy, unmerged reviews.
    inputs = receipt.get('review_inputs')
    if not isinstance(inputs, dict): return False
    validation = e.head(inputs.get('validation_head'))
    if (inputs.get('head') != head or not e.head(base) or inputs.get('base') != base or not validation or
            receipt.get('start_head') != validation or receipt.get('end_head') != validation):
        return False
    try:
        return git(repo, 'rev-list', '--parents', '-n', '1', validation).split() == [validation, head, base]
    except (OSError, subprocess.SubprocessError): return False


def completed_review(repo, receipts, rounds, head, base):
    """Select the latest completed matching round, independently of merge mode.

    Round counts describe the deterministic union, including dedup and holds;
    summing specialist counts would lose those semantics. Shadow results have
    no authority over these counts or the final verdict.
    """
    candidates = []
    for attempt, run in rounds:
        record = attempt['review_round']; merge = record.get('merge', {})
        if (merge.get('mode') not in ('deterministic', 'model_fallback') or record.get('final_verdict') not in ('APPROVE', 'REQUEST_CHANGES', 'BLOCK')
                or not all(k in merge for k in ('result_sha256', 'missing_acceptance_count', 'fixes_needed_count', 'human_holds'))):
            continue
        phases = {'correctness', 'security', 'performance'}
        if merge['mode'] == 'model_fallback': phases.add('verdict')
        selected = [(path, r) for path, r in receipts if r.get('provider_attempt_id') in record['provider_attempt_ids']
                    and r.get('run_id') == run and r.get('stage_attempt_id') == attempt['attempt_id']
                    and r.get('stage') == 'code_review' and r.get('phase') in phases]
        if {r['phase'] for _, r in selected} != phases: continue
        if any(r.get('normalized_exit') != 0 or r.get('provider_exit') != 0 or r.get('start_head') != r.get('end_head')
               or not review_matches(repo, r, head, base) for _, r in selected): continue
        # Specialists run without a verdict schema so malformed output can reach
        # the conservative fallback. Their receipts have no structured-result
        # digest. The completed round owns the retained merged digest/counts;
        # a fallback provider's original may omit retained specialist blockers.
        completed_at = max(r.get('ended_at') or 0 for _, r in selected)
        candidates.append(((completed_at, record['number'], attempt['started_at'], run, attempt['attempt_id']), selected, record))
    return max(candidates, key=lambda candidate: candidate[0])[1:] if candidates else None


def stage_results(repo, attempts, receipts, head, base):
    lines = []; present = set()
    for attempt, run in attempts:
        stage = attempt['stage']
        if stage not in e.STAGES or attempt['phase'] != 'stage': continue
        linked = [r for _, r in receipts if r.get('run_id') == run and r.get('stage_attempt_id') == attempt['attempt_id'] and r.get('stage') == stage]
        matching = (attempt.get('final_head') == head or any(
            review_matches(repo, r, head, base) if stage == 'code_review' else r.get('end_head') == head for r in linked))
        if not matching: continue
        code = attempt.get('normalized_exit')
        verdict = attempt.get('review_round', {}).get('final_verdict')
        result = verdict if verdict in ('APPROVE', 'REQUEST_CHANGES', 'BLOCK') else 'missing'
        if type(code) is int: result += f'; exit={code}'
        # Exit 0 is a final stage status, not an inferred provider approval.
        if type(code) is int and verdict not in ('APPROVE', 'REQUEST_CHANGES', 'BLOCK'):
            result = 'ok' if code == 0 else 'failed'
            result += f'; exit={code}'
        lines += [f'- {stage}: {result}; attempt `{attempt["attempt_id"]}`.']; present.add(stage)
    for stage in ('spec', 'spec_review', 'ux', 'copy', 'implement', 'qa', 'code_review', 'merge', 'rebase'):
        if stage not in present: lines += [f'- {stage}: missing.']
    return lines


def render(store, repo, issue, pr, head, url, spec=None, base=None, verdict=None, review_head=None, ci=None):
    """Only projected metadata crosses into Markdown; never copy evidence bytes."""
    config = configuration(repo); settings = risk_settings(config)
    marker = f'<!-- bureau-acceptance: pr={pr} head={head} -->'
    lines = [marker, f'## Acceptance brief — {issue} / PR #{pr}',
             'Awaiting human decision. This brief does not establish acceptance, merge or freshness.',
             '', '**Intent**']
    repository = url.rsplit('/pull/', 1)[0]
    spec_found = False
    if spec:
        try:
            relative = Path(spec).resolve().relative_to(repo.resolve()).as_posix()
            source = git_bytes(repo, 'show', head + ':' + relative)
            lines += [f'- Spec: [intent at HEAD]({repository}/blob/{head}/{quote(relative)}) sha256={hashlib.sha256(source).hexdigest()}']
            spec_found = True
        except (ValueError, OSError, subprocess.SubprocessError): pass
    if not spec_found: lines += ['- Spec intent: missing.']
    lines += ['', '**Result**', f'- HEAD: [{head}]({repository}/commit/{head})']
    size = None
    try:
        base = e.head(base) or git(repo, 'merge-base', 'origin/main', head)
        size = diff_size(repo, base, head)
        count = int(git(repo, 'rev-list', '--count', base + '..' + head))
        lines += [f'- Change: {size["files"]} files, {count} commits; [diff]({repository}/compare/{base}...{head}).',
                  f'- Diff size: {size["files"]} files, {size["insertions"]} insertions, {size["deletions"]} deletions, {size["binary"]} binary files (base..HEAD; renames counted as delete/add).']
    except (ValueError, OSError, subprocess.SubprocessError):
        lines += ['- Change summary/base: missing.', '- Diff size: missing.']
    declared = None
    if spec:
        try:
            tasks = (Path(spec).resolve().parent / 'tasks.md').relative_to(repo.resolve()).as_posix()
            prefix = os.environ.get('BUREAU_PATH_PREFIX_STRIP', config.get('repo', {}).get('path_prefix_strip', ''))
            declared = paths.inventory(repo, tasks, prefix, head, git_bytes)['paths']
        except (ValueError, OSError, subprocess.SubprocessError): pass
    if declared is None: lines += ['- declared files: missing.']
    elif size is None: lines += ['- Files outside declared scope: missing (diff unavailable).']
    else:
        outside = [path for path in size['paths'] if not paths.intersection([path], declared)]
        lines += [f'- Files outside declared scope: {len(outside)}; ' + path_list(outside) + '.']
    tier, facts = risk_tier(size, settings)
    lines += [f'- Risk tier: {tier}; {facts}.',
              '- Risk tier is advisory only: it changes no routing, holds, verdicts or merges. A human merges every PR.',
              '- CI status: ' + (ci or 'unavailable') + f' (HEAD={head}).']
    manifests = []
    for path, record in store._records('runs/*/manifest.json', 'manifest', quarantine=False):
        if record['issue'] == issue and (e.pr_number(record.get('pr')) == pr or any(e.pr_number(a.get('pr')) == pr for a in record['attempts'])):
            manifests.append((path, record))
    manifests.sort(key=lambda pair: (pair[1]['started_at'], pair[1]['run_id']))
    gates = []; receipts = []; linked = []; rounds = []; attempts = []
    for path, record in manifests:
        for attempt in record['attempts']:
            if e.pr_number(attempt.get('pr') or record.get('pr')) != pr: continue
            attempts.append((attempt, record['run_id']))
            if attempt['stage'] == 'code_review' and 'review_round' in attempt: rounds.append((attempt, record['run_id']))
            for index, gate in enumerate(attempt['gates']):
                if gate['kind'] == 'gate': gates.append((path, attempt, index, gate))
            for relative in attempt['receipts']:
                receipt_path = store.root / relative
                try: receipt = e.read(receipt_path, {})
                except (OSError, ValueError): continue
                if not isinstance(receipt, dict): continue
                if (receipt.get('issue') == issue and receipt.get('run_id') == record['run_id'] and
                        receipt.get('stage_attempt_id') == attempt['attempt_id']):
                    receipts.append((receipt_path, receipt))
        linked.append(evidence_ref(store, path))
    lines += ['', '**Stage verdicts**'] + stage_results(repo, attempts, receipts, head, base)
    lines += ['', '**Test evidence**']
    for path, attempt, index, gate in gates[-8:]:
        checked = e.head(gate['head']) or 'missing'
        status = 'matches brief HEAD' if checked == head and gate['end_head'] == head else 'other/missing HEAD'
        code = gate['normalized_exit'] if type(gate['normalized_exit']) is int else 'missing'
        lines += [f'- {gate["command_id"]}: exit={code}, HEAD={checked}, end_HEAD={e.head(gate["end_head"]) or "missing"} ({status}), sha256={gate["sha256"]}; ' +
                  evidence_ref(store, path) + f', attempt `{attempt["attempt_id"]}`, gate {index}']
    latest_flake = next((item for item in reversed(gates) if item[3].get('flaky')), None)
    if latest_flake:
        path, attempt, index, gate = latest_flake
        flake = gate['flaky']
        lines += ['- Flaky: passed on one rerun; failed tests: ' + ', '.join(flake['failed_tests']) +
                  (f'; {flake["omitted"]} more omitted' if flake['omitted'] else '') + '; ' +
                  evidence_ref(store, path) + f', attempt `{attempt["attempt_id"]}`, gate {index}.']
    if not gates: lines += ['- Gate records (exit/digest): missing.']
    if len(gates) > 8: lines += [f'- {len(gates) - 8} earlier gates omitted; see manifests.']
    lines += ['', '**Review evidence**',
              f'- Final verdict: {verdict if verdict in ("APPROVE", "REQUEST_CHANGES", "BLOCK") else "missing"}; REVIEW_HEAD={e.head(review_head) or "missing"}.']
    agents = config.get('agents', {})
    def configured_number(key, default, minimum, maximum):
        # Match _bureau_config_number/bureau_number, including digit strings.
        raw = agents.get(key, default)
        raw = str(raw) if type(raw) in (int, str) else ''
        return int(raw) if re.fullmatch(r'[0-9]{1,10}', raw) and minimum <= int(raw) <= maximum else default
    cycles = configured_number('max_review_cycles', 3, 0, 1000)
    cap = configured_number('max_review_rounds', cycles + 1, 1, 1001)
    lines += [f'- Review rounds used: {len(rounds)} recorded for this PR; cap={cap} (effective max_review_rounds).']
    review = None
    for path, receipt in sorted(receipts, key=lambda pair: pair[1].get('ended_at') or 0, reverse=True):
        if (receipt.get('stage') == 'code_review' and receipt.get('phase') in ('verdict', 'code_review') and
                review_matches(repo, receipt, head, base) and receipt.get('normalized_exit') == 0 and receipt.get('provider_exit') == 0):
            review = (path, receipt); break
    points = []
    completed = completed_review(repo, receipts, rounds, head, base)
    if completed:
        selected, record = completed; merge = record['merge']; mode = merge['mode']
        lines += ['- Review receipt: ' + ', '.join(evidence_ref(store, path) for path, _ in selected) +
                  f'; {mode} round {record["number"]}, REVIEW_HEAD={head}, result sha256={merge["result_sha256"]}.',
                  f'- Recorded final verdict: {record["final_verdict"]}.']
        validation = selected[0][1]['end_head']
        if validation != head:
            lines += [f'- Validation merge: {validation}; pinned input HEAD={head}, base={base} (verified parents).']
        for key in ('missing_acceptance', 'fixes_needed'):
            points += [f'- {key}: {merge[key + "_count"]} ({mode} review metadata).']
        points += [f'- Human review holds: {merge["human_holds"]}.']
        points += [f'- Critical floor: {merge["critical_floor"]}; security floor: {merge["security_floor"]}; '
                   f'invalid anchors: {merge["invalid_anchors"]} (grounding {merge["grounding"]}).']
    elif review:
        path, receipt = review
        result_digest = receipt.get('result_sha256')
        if not isinstance(result_digest, str) or not re.fullmatch(r'[a-f0-9]{64}', result_digest): result_digest = None
        lines += [f'- Review receipt: {evidence_ref(store, path)}; REVIEW_HEAD={head}, end_HEAD={e.head(receipt.get("end_head")) or "missing"}, result sha256={result_digest or "missing"}.']
        if receipt.get('end_head') != head:
            lines += [f'- Validation merge: {receipt["end_head"]}; pinned input HEAD={head}, base={base} (verified parents).']
        try: original = p.receipt_result(receipt) if result_digest else None
        except (KeyError, TypeError, ValueError, OSError): original = None
        if isinstance(original, dict) and e.digest(original) == receipt.get('result_sha256'):
            provider_verdict = original.get('verdict')
            lines += [f'- Provider verdict: {provider_verdict if provider_verdict in ("APPROVE", "REQUEST_CHANGES", "BLOCK") else "missing"}.']
            for key in ('missing_acceptance', 'fixes_needed'):
                items = original.get(key)
                points += [f'- {key}: {len(items)} (details in original review).' if isinstance(items, list) else f'- {key}: missing.']
        else:
            points += ['- Review blockers/known limits: missing (original result unavailable or digest mismatch).']
    else:
        lines += ['- Review receipt/verdict for this HEAD: missing.']
        points += ['- Review blockers/known limits: missing.']
    lines += ['', '**Token usage**'] + spending(repo, issue, config, attempts, receipts, head, base)
    lines += ['', '**Open points**'] + points + sibling_open_points(repo, receipts, head)
    waits = [v for _, v in store._records('waits/*.json', 'wait', quarantine=False) if v['issue'] == issue and v['state'] == 'open']
    for wait in waits[:6]:
        stage = e.tag(wait['stage']) if isinstance(wait['stage'], str) else 'missing'
        code = wait['exit_code'] if type(wait['exit_code']) is int else 'missing'
        lines += [f'- Wait: {wait["kind"]}, actor={wait["responsible_actor"]}, stage={stage}, exit={code}; ' +
                  evidence_ref(store, store.root / 'waits' / (wait['wait_id'] + '.json'))]
    if not waits: lines += ['- Wait records: missing (absence is not proof of no waits).']
    if len(waits) > 6: lines += [f'- {len(waits) - 6} additional open waits in the local register.']
    lines += ['- Other blockers/known limits: missing; confirm with the owner.', '', '**Evidence index**']
    lines += ['- ' + link for link in linked[-4:]] if linked else ['- Run manifest: missing.']
    lines += ['- Operator store location: `<git common dir>/bureau/evidence/v1`.']
    lines += ['- Local evidence is observational, may expire, and is not accessible from GitHub without the repository evidence store.']
    return '\n'.join(lines) + '\n'


def publish(store, issue, pr, head, build, deliver):
    """Separate publication lock: remote calls never hold the evidence-store lock."""
    lock_path = store.root.parent.parent / 'acceptance.guard'
    e.private_dir(lock_path.parent)
    with os.fdopen(os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600), 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        path = store.root / 'acceptance' / (str(pr) + '-' + head + '.json')
        with store.guard(): record = store._load(path, 'acceptance')
        if record is None:
            record = dict(schema_version=1, record_type='acceptance', issue=issue, pr=pr, head=head, created_at=store.clock(),
                          body=build(), ticket='pending', pull_request='pending')
            record['body_sha256'] = hashlib.sha256(record['body'].encode()).hexdigest()
            with store.guard(): store._write(path, record)
        if record['issue'] != issue or record['head'] != head or record['pr'] != pr:
            raise ValueError('acceptance identity mismatch')
        # Pre-fix pending ledgers may contain private file: links. Re-render
        # before retrying; retain delivery states so successful posts stay deduplicated.
        if re.search(r'file:', record['body'], re.I):
            record['body'] = build()
            record['body_sha256'] = hashlib.sha256(record['body'].encode()).hexdigest()
            with store.guard(): store._write(path, record)
        for destination in ('ticket', 'pull_request'):
            if record[destination] == 'delivered': continue
            try: ok = deliver(destination, record['body'])
            except PublicationInterrupted: raise
            except Exception: ok = False
            record[destination] = 'delivered' if ok else 'pending'
            # A lost acknowledgment is repaired by the remote marker lookup.
            try:
                with store.guard(): store._write(path, record)
            except Exception: e.warn()
        return record


def main():
    if len(sys.argv) == 3 and sys.argv[1] == '--validate-config':
        try: risk_settings(json.loads(Path(sys.argv[2]).read_text()))
        except (OSError, ValueError, TypeError, AttributeError):
            print('ERROR: invalid agents.risk_tier configuration', file=sys.stderr)
            raise SystemExit(24)
        return
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo', type=Path, required=True); parser.add_argument('--issue', required=True)
    parser.add_argument('--pr', type=int); parser.add_argument('--head'); parser.add_argument('--url')
    parser.add_argument('--branch'); parser.add_argument('--spec'); parser.add_argument('--base')
    parser.add_argument('--verdict'); parser.add_argument('--review-head'); parser.add_argument('--config-script', type=Path)
    parser.add_argument('--publish', action='store_true')
    args = parser.parse_args()
    if os.environ.get('BUREAU_DRY_RUN') == '1': return
    def interrupted(signum, frame): raise PublicationInterrupted('acceptance publication interrupted')
    for name in ('SIGTERM', 'SIGINT', 'SIGHUP'):
        signal.signal(getattr(signal, name), interrupted)
    try:
        repo = args.repo.resolve(); issue = e.ticket(args.issue)
        common = Path(git(repo, 'rev-parse', '--git-common-dir'))
        store = e.Store((repo / common).resolve() / 'bureau')
        def gh(*argv):
            code, output = bounded(['gh', *argv], repo)
            if code: raise OSError('PR pointer unavailable')
            return output.strip()
        if args.pr is None or not args.head or not args.url:
            pointer = json.loads(gh('pr', 'view', str(args.pr) if args.pr else args.branch, '--json', 'number,headRefOid,baseRefOid,url'))
            args.pr = args.pr or pointer['number']
            args.head = args.head or pointer['headRefOid']
            args.url = args.url or pointer['url']
            args.base = args.base or pointer.get('baseRefOid')
        if not e.pr_number(args.pr) or not e.head(args.head): raise ValueError('missing PR/HEAD')
        if not re.fullmatch(r'https://[A-Za-z0-9.-]+/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/pull/' + str(args.pr), args.url):
            raise ValueError('invalid PR pointer')
        def build():
            # publish() invokes build outside store.guard(), and freezes this
            # snapshot once. Delivery retries retain that same PR/HEAD packet.
            ci = ci_status(repo, args.url, args.head) if args.publish else None
            return render(store, repo, issue, args.pr, args.head, args.url, args.spec, args.base, args.verdict, args.review_head, ci)
        if not args.publish:
            print(build(), end=''); return
        def deliver(destination, body):
            marker = body.splitlines()[0]
            setup = ('source "$1"; '
                     'if [ -f "${BUREAU_ENV_FILE:-}" ]; then bureau_load_env "$BUREAU_ENV_FILE" || exit 1; fi; '
                     'if declare -F bureau_secret_copy >/dev/null; then bureau_secret_copy --optional API_KEY LINEAR_API_KEY; fi; ')
            # Use the same trusted shell checkpoint at each publication edge.
            # Done exits 0 without the sentinel; it is a stop, not delivery.
            check = ['bash', '-c', setup + 'bureau_ticket_checkpoint "$2" && printf authorized',
                     'bureau-acceptance-delivery', str(args.config_script.resolve()), issue]
            code, allowed = bounded(check, repo, diagnostics=True)
            if code or allowed != 'authorized': return False
            if destination == 'ticket':
                # All Linear CRUD stays in the existing helper. No provider call.
                lookup = ['bash', '-c', setup + 'get_issue_comments "$2"',
                          'bureau-acceptance-delivery', str(args.config_script.resolve()), issue]
                # post_comment can cooperatively exit this shell 0 without
                # posting. Only a successful return reaches the acknowledgment.
                command = ['bash', '-c', setup + 'body=$(cat); post_comment "$2" "$body" >/dev/null && printf published',
                           'bureau-acceptance-delivery', str(args.config_script.resolve()), issue]
            else:
                lookup = ['gh', 'pr', 'view', str(args.pr), '--json', 'comments']
                command = ['gh', 'pr', 'comment', str(args.pr), '--body-file', '-']
            code, answer = bounded(lookup, repo)
            if code: return False
            comments = json.loads(answer)
            if destination == 'pull_request': comments = comments['comments']
            if not isinstance(comments, list): return False
            if any(isinstance(c, dict) and c.get('body', '').splitlines()[:1] == [marker] for c in comments): return True
            code, allowed = bounded(check, repo, diagnostics=True)
            if code or allowed != 'authorized': return False
            code, completed = bounded(command, repo, body)
            return code == 0 and (destination != 'ticket' or completed == 'published')
        result = publish(store, issue, args.pr, args.head, build, deliver)
        if 'pending' in (result['ticket'], result['pull_request']): e.warn()
    except PublicationInterrupted:
        raise SystemExit(130)
    except ConfigError:
        print('ERROR: invalid agents.risk_tier configuration', file=sys.stderr)
        raise SystemExit(24)
    except Exception:
        e.warn()


if __name__ == '__main__': main()
