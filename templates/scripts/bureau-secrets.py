#!/usr/bin/env python3
"""Offline, masked secret findings from added lines of immutable branch inputs."""
import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import math
from pathlib import Path
import re
import sys

sys.dont_write_bytecode = True
spec = importlib.util.spec_from_file_location('secret_scan', Path(__file__).resolve().with_name('bureau-scan.py'))
scan = importlib.util.module_from_spec(spec)
spec.loader.exec_module(scan)

# Local fallback, rather than extending Trivy: bureau-trivy.py FLAGS selects
# vuln,misconfig and measure() requires an operator-refreshed database cache.
# RealBinaryTests qualifies lockfiles/Dockerfiles, not offline secret-only scans
# with the pinned 0.76.1 binary. This check needs neither a binary nor a database.
EXCEPTIONS = '.bureau/secret-exceptions.json'
MASK = '[REDACTED]'
MAX_FILES, MAX_BYTES, MAX_FINDINGS = 200, 1024 * 1024, 20


def token(pattern):
    return re.compile(r'(?<![A-Za-z0-9_])(?:' + pattern + r')(?![A-Za-z0-9_-])')


# Public reusable rule table: (stable rule ID, compiled pattern, entropy floor).
# A future published-text scanner can import SECRET_RULES and rule_ids(). Only
# masked values and Git blob/path locators leave this module. Format checks are
# narrow; no credential validation, decoding, history or cross-line reconstruction.
SECRET_RULES = (
    ('aws-access-key-id', token(r'(?:AKIA|ASIA)[A-Z0-9]{16}'), None),
    ('github-token', token(r'gh[pousr]_[A-Za-z0-9]{36}|github_pat_[A-Za-z0-9]{22}_[A-Za-z0-9]{59}'), None),
    ('anthropic-api-key', token(r'sk-ant-api[0-9]{2}-[A-Za-z0-9_-]{80,120}'), None),
    ('openai-api-key', token(r'sk-(?:proj-|svcacct-)[A-Za-z0-9_-]{48,200}|sk-[A-Za-z0-9]{48}'), None),
    ('slack-token', token(r'xox[baprs]-(?:[0-9]{10,13}-){2,3}[A-Za-z0-9]{20,64}|xapp-[0-9]-[A-Z0-9]{10,13}-[0-9]{10,13}-[a-f0-9]{64}'), None),
    ('stripe-live-key', token(r'(?:sk|rk)_live_[A-Za-z0-9]{24,200}'), None),
    ('google-api-key', token(r'AIza[A-Za-z0-9_-]{35}'), None),
    ('private-key-header', re.compile(r'-----BEGIN (?:RSA |EC |DSA |OPENSSH |ENCRYPTED )?PRIVATE KEY-----'), None),
    ('generic-secret-assignment', re.compile(
        r'''(?i)(?<![A-Za-z0-9_])(?:password|secret)[ \x09]*=[ \x09]*["']?(?P<value>[A-Za-z0-9_+/!@#$%^&*.=-]{24,256})(?![A-Za-z0-9_+/!@#$%^&*.=-])'''), 4.0),
)


def rule_matches(text):
    """Yield every qualifying span, using the same thresholds for all consumers."""
    for rule, pattern, floor in SECRET_RULES:
        for match in pattern.finditer(text):
            if floor is not None:
                value = match['value']
                # Require length, entropy and mixed character classes. Ordinary
                # words, repeated placeholders and hex digests are not enough.
                classes = sum(bool(re.search(group, value)) for group in (r'[a-z]', r'[A-Z]', r'[0-9]', r'[^A-Za-z0-9]'))
                entropy = -sum((n / len(value)) * math.log2(n / len(value)) for n in Counter(value).values())
                if classes < 3 or entropy < floor:
                    continue
            yield rule, match


def rule_ids(text, *, values=None):
    """Return distinct rule IDs, optionally collecting every matched value."""
    rules = {}
    for rule, match in rule_matches(text):
        if values is not None:
            values.add(match.groupdict().get('value', match.group()))
        rules[rule] = None
    return list(rules)


def metadata_matches(text):
    """Apply metadata rules per component so assignments never consume a slash."""
    offset = 0
    for component in text.split('/'):
        for rule, match in rule_matches(component):
            yield rule, match, offset
        offset += len(component) + 1


def mask_metadata(text, values=()):
    """Redact the union of rule spans and exact known values, without boundaries."""
    values = tuple(value for value in values if value and value != MASK)
    while True:
        spans = [(offset + match.start(), offset + match.end()) for _, match, offset in metadata_matches(text)]
        for value in values:
            start = text.find(value)
            while start >= 0:
                spans.append((start, start + len(value)))
                start = text.find(value, start + 1)
        pieces, end = [], 0
        for start, stop in sorted(spans):
            if start >= end:
                pieces.extend((text[end:start], MASK))
            end = max(end, stop)
        pieces.append(text[end:])
        masked = ''.join(pieces)
        if masked == text:
            return masked
        # Rule redaction can create a known masked form. Repeat until stable;
        # detected values and nontrivial masked forms are longer than MASK.
        text = masked


def lines(data):
    return data.split(b'\n')[:-1] if data.endswith(b'\n') else data.split(b'\n') if data else []


def added_lines(patch, before, after):
    """Validate every hunk against both blobs, including every unchanged gap."""
    old, new = lines(before), lines(after)
    old_pos = new_pos = 0
    remaining_old = remaining_new = 0
    in_hunk = False
    for raw in patch.split(b'\n')[:-1]:
        hunk = re.fullmatch(rb'@@ -([0-9]+)(?:,([0-9]+))? \+([0-9]+)(?:,([0-9]+))? @@.*', raw)
        if hunk:
            if remaining_old or remaining_new:
                raise ValueError('incomplete diff hunk')
            start_old, remaining_old = int(hunk[1]), int(hunk[2] or 1)
            start_new, remaining_new = int(hunk[3]), int(hunk[4] or 1)
            index_old, index_new = start_old - bool(remaining_old), start_new - bool(remaining_new)
            if (not old_pos <= index_old <= len(old) or not new_pos <= index_new <= len(new)
                    or old[old_pos:index_old] != new[new_pos:index_new]):
                raise ValueError('diff gap does not match blobs')
            old_pos, new_pos, in_hunk = index_old, index_new, True
        elif raw == b'\\ No newline at end of file':
            if not in_hunk:
                raise ValueError('unexpected diff marker')
        elif in_hunk:
            prefix, content = raw[:1], raw[1:]
            if prefix not in (b'+', b'-', b' '):
                raise ValueError('invalid diff line')
            if prefix in (b'-', b' '):
                if remaining_old < 1 or old_pos >= len(old) or old[old_pos] != content:
                    raise ValueError('old diff line does not match blob')
                remaining_old -= 1
                old_pos += 1
            if prefix in (b'+', b' '):
                if remaining_new < 1 or new_pos >= len(new) or new[new_pos] != content:
                    raise ValueError('new diff line does not match blob')
                remaining_new -= 1
                new_pos += 1
                if prefix == b'+':
                    yield new_pos, content
        elif not raw.startswith((b'diff --git ', b'index ', b'--- ', b'+++ ', b'new file mode ',
                                 b'deleted file mode ', b'old mode ', b'new mode ')):
            raise ValueError('invalid diff header')
    if (remaining_old or remaining_new or old[old_pos:] != new[new_pos:]
            or (patch and not patch.endswith(b'\n'))):
        raise ValueError('incomplete diff')


def measure(repo, base, head, *, now=None):
    result = dict(tool='secrets', status='not_measured', reason='secret scan inputs or result unavailable',
                  base=base, head=head, findings=[], findings_count=0, findings_omitted=0,
                  findings_suppressed=0, blocking=True)
    try:
        # Full pinned commit IDs, raw Git objects only; never inspect the checkout
        # or invoke a repository diff driver, textconv, build or remote operation.
        base_tree = scan.tree(repo, base)
        head_tree = scan.tree(repo, head)
        merge_base = scan.git(repo, 'merge-base', base, head).decode('ascii').strip()
        old_tree = scan.tree(repo, merge_base)
        policy = scan.module('trivy')
        rows = policy.exceptions(scan.blob(repo, base_tree, EXCEPTIONS))
        if any('package' in row or row.get('kind', 'finding') != 'finding' for row in rows):
            raise ValueError('secret exceptions support finding IDs and files only')
        ignores = policy.active_exceptions(rows, now or datetime.now(timezone.utc))
        flags = ('--no-ext-diff', '--no-textconv', '--no-renames', '--no-color', '--text', '--ignore-submodules=none')
        # The complete pinned head tree proves deleted paths have no additions.
        # Exclude them before path budgets, content limits or old blob reads.
        # Every remaining changed blob must produce a fully validated patch.
        names = [name for name in head_tree if old_tree.get(name) != head_tree[name]]
        if len(names) > MAX_FILES or any(not scan.relative(name) for name in names):
            raise ValueError('unsupported diff paths or size')
        found, count, suppressed, values = [], 0, 0, set()
        for name in sorted(names):
            values.update(match.groupdict().get('value', match.group()) for _, match, _ in metadata_matches(name))
            before = scan.blob(repo, old_tree, name, limit=MAX_BYTES) or b''
            after = scan.blob(repo, head_tree, name, limit=MAX_BYTES) or b''
            patch = scan.git(repo, 'diff', *flags, '--unified=0', '--inter-hunk-context=0',
                             '--output-indicator-new=+', '--output-indicator-old=-', '--output-indicator-context= ',
                             merge_base, head, '--', ':(literal)' + name)
            for line, content in added_lines(patch, before, after):
                for rule in rule_ids(content.decode('utf-8', errors='replace'), values=values):
                    values.update(match.groupdict().get('value', match.group()) for _, match, _ in metadata_matches(rule))
                    if any(all(row[key] == {'id': rule, 'file': name}[key] for key in ('id', 'file') if key in row)
                           for row in ignores):
                        suppressed += 1
                    else:
                        count += 1
                        if len(found) < MAX_FINDINGS:
                            found.append(dict(file=name, line=line, rule_id=rule, value=MASK,
                                              locator=dict(blob=head_tree[name][2],
                                                           path_sha256=hashlib.sha256(name.encode('utf-8')).hexdigest()[:16])))
        # Redact only after every addition has been validated and scanned. Later,
        # suppressed and omitted detections must protect earlier metadata too.
        values.update(mask_metadata(value) for value in tuple(values))
        for finding in found:
            for key in ('file', 'rule_id'):
                finding[key] = mask_metadata(finding[key], values)
        result.update(status='measured', reason=None, findings=found, findings_count=count,
                      findings_omitted=max(0, count - MAX_FINDINGS), findings_suppressed=suppressed, blocking=bool(count))
    except Exception:
        # Scanner, redaction and locator failures all leave the default held result.
        # Never render an exception, patch, source line or matched value, including
        # when an earlier file already produced findings before a later failure.
        pass
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo', default='.')
    parser.add_argument('--base', required=True)
    parser.add_argument('--head', required=True)
    args = parser.parse_args()
    try:
        print(json.dumps(measure(Path(args.repo).resolve(), args.base, args.head), sort_keys=True))
        return 0
    except KeyboardInterrupt:
        return 130


if __name__ == '__main__':
    sys.exit(main())
