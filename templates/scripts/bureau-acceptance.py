#!/usr/bin/env python3
"""Render metadata-only acceptance briefs; retry ticket and PR delivery independently."""
import argparse
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
from urllib.parse import quote

sys.dont_write_bytecode = True


def sibling(name):
    spec = importlib.util.spec_from_file_location(name.replace('-', '_'), Path(__file__).with_name(name + '.py'))
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    return module


e = sibling('bureau-evidence')
p = sibling('bureau-provider')


def git(repo, *args):
    return git_bytes(repo, *args).decode().strip()


def git_bytes(repo, *args):
    command = ['git', '-C', str(repo), *args]
    return subprocess.check_output(command, stderr=subprocess.DEVNULL, timeout=10, env=p.process_env(command))


def bounded(command, repo, body=None):
    """Kill the whole API-helper group on timeout, including a surviving curl."""
    # The trusted Linear shell helper needs its credentials. Git/gh follow
    # the shared Python policy, including remote GitHub authentication.
    environ = p.process_env(command) if Path(command[0]).name == 'gh' else dict(os.environ)
    for name in ('BASH_ENV', 'ENV'): environ.pop(name, None)
    child = subprocess.Popen(command, cwd=repo, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                             stderr=subprocess.DEVNULL, text=True, start_new_session=True, env=environ)
    try:
        output, _ = child.communicate(body, timeout=45)
        return child.returncode, output
    finally:
        try: os.killpg(child.pid, signal.SIGKILL)
        except ProcessLookupError: pass
        child.communicate()


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


def render(store, repo, issue, pr, head, url, spec=None, base=None, verdict=None, review_head=None):
    """Only projected metadata crosses into Markdown; never copy evidence bytes."""
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
    try:
        base = e.head(base) or git(repo, 'merge-base', 'origin/main', head)
        changed = git(repo, 'diff', '--name-only', base, head).splitlines()
        count = int(git(repo, 'rev-list', '--count', base + '..' + head))
        lines += [f'- Change: {len(changed)} files, {count} commits; [diff]({repository}/compare/{base}...{head}).']
    except (ValueError, OSError, subprocess.SubprocessError): lines += ['- Change summary/base: missing.']
    manifests = []
    for path, record in store._records('runs/*/manifest.json', 'manifest', quarantine=False):
        if record['issue'] == issue and (e.pr_number(record.get('pr')) == pr or any(e.pr_number(a.get('pr')) == pr for a in record['attempts'])):
            manifests.append((path, record))
    manifests.sort(key=lambda pair: (pair[1]['started_at'], pair[1]['run_id']))
    lines += ['', '**Test evidence**']
    gates = []; receipts = []; linked = []
    for path, record in manifests:
        for attempt in record['attempts']:
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
    for path, attempt, index, gate in gates[-8:]:
        checked = e.head(gate['head']) or 'missing'
        status = 'matches brief HEAD' if checked == head and gate['end_head'] == head else 'other/missing HEAD'
        code = gate['normalized_exit'] if type(gate['normalized_exit']) is int else 'missing'
        lines += [f'- {gate["command_id"]}: exit={code}, HEAD={checked}, end_HEAD={e.head(gate["end_head"]) or "missing"} ({status}), sha256={gate["sha256"]}; ' +
                  evidence_ref(store, path) + f', attempt `{attempt["attempt_id"]}`, gate {index}']
    if not gates: lines += ['- Gate records (exit/digest): missing.']
    if len(gates) > 8: lines += [f'- {len(gates) - 8} earlier gates omitted; see manifests.']
    lines += ['', '**Review evidence**',
              f'- Final verdict: {verdict if verdict in ("APPROVE", "REQUEST_CHANGES", "BLOCK") else "missing"}; REVIEW_HEAD={e.head(review_head) or "missing"}.']
    review = None
    for path, receipt in sorted(receipts, key=lambda pair: pair[1].get('ended_at') or 0, reverse=True):
        if (receipt.get('stage') == 'code_review' and receipt.get('phase') in ('verdict', 'code_review') and
                review_matches(repo, receipt, head, base) and receipt.get('normalized_exit') == 0 and receipt.get('provider_exit') == 0):
            review = (path, receipt); break
    points = []
    if review:
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
    lines += ['', '**Open points**'] + points
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
            except InterruptedError: raise
            except Exception: ok = False
            record[destination] = 'delivered' if ok else 'pending'
            # A lost acknowledgment is repaired by the remote marker lookup.
            try:
                with store.guard(): store._write(path, record)
            except Exception: e.warn()
        return record


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo', type=Path, required=True); parser.add_argument('--issue', required=True)
    parser.add_argument('--pr', type=int); parser.add_argument('--head'); parser.add_argument('--url')
    parser.add_argument('--branch'); parser.add_argument('--spec'); parser.add_argument('--base')
    parser.add_argument('--verdict'); parser.add_argument('--review-head'); parser.add_argument('--config-script', type=Path)
    parser.add_argument('--publish', action='store_true')
    args = parser.parse_args()
    if os.environ.get('BUREAU_DRY_RUN') == '1': return
    def interrupted(signum, frame): raise InterruptedError('acceptance publication interrupted')
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
        build = lambda: render(store, repo, issue, args.pr, args.head, args.url, args.spec, args.base, args.verdict, args.review_head)
        if not args.publish:
            print(build(), end=''); return
        def deliver(destination, body):
            marker = body.splitlines()[0]
            if destination == 'ticket':
                # All Linear CRUD stays in the existing helper. No provider call.
                setup = ('source "$1"; '
                         'if [ -f "${BUREAU_ENV_FILE:-}" ]; then bureau_load_env "$BUREAU_ENV_FILE" || exit 1; fi; '
                         'if declare -F bureau_secret_copy >/dev/null; then bureau_secret_copy --optional API_KEY LINEAR_API_KEY; fi; ')
                lookup = ['bash', '-c', setup + 'get_issue_comments "$2"',
                          'bureau-acceptance-delivery', str(args.config_script.resolve()), issue]
                command = ['bash', '-c', setup + 'body=$(cat); post_comment "$2" "$body"',
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
            code, _ = bounded(command, repo, body)
            return code == 0
        result = publish(store, issue, args.pr, args.head, build, deliver)
        if 'pending' in (result['ticket'], result['pull_request']): e.warn()
    except Exception:
        e.warn()


if __name__ == '__main__': main()
