#!/bin/bash
# Discovered by tests/run.sh; read-only, Bash 3.2 + Python 3.9, no services.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PYTHONDONTWRITEBYTECODE=1 python3 - "$ROOT" <<'PY'
import datetime
import os
from pathlib import Path, PurePosixPath
import re
import sys

root = Path(sys.argv[1]).resolve()
directory = root / 'docs/adr'
sections = ['Context', 'Options considered', 'Decision', 'Rationale',
            'Consequences', 'Known limits', 'Reversal condition', 'Evidence',
            'Open points']
fields = ['Status', 'Date', 'Decided by', 'Sources']
status_pattern = re.compile(r'accepted \(derived\)|proposed \(v3\.5, PR #[1-9][0-9]*\)|superseded by (?!0000)[0-9]{4}')
template_status = 'accepted (derived) | proposed (v3.5, PR #n) | superseded by NNNN'
email_pattern = re.compile(r"[A-Z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Z0-9-]+(?:\.[A-Z0-9-]+)+", re.I)
ticket_quote = re.compile(r'''\b[A-Z][A-Z0-9]*-[0-9]+\b(?:[^\n]{0,160}|[^\n]{0,160}\n[ \t]*(?:>[ \t]*)?)(?:["“„«]|(?<!\w)['‘])''')
errors = []


def fail(path, line, message):
    # Never echo matched prose, addresses or private deny-list entries.
    errors.append(f'{path.relative_to(root)}:{line}: {message}')


def existing_reference(ref, document, line):
    match = re.fullmatch(r'([^:]+?)(?::([1-9][0-9]*))?', ref)
    if not match:
        fail(document, line, 'invalid repository file reference')
        return
    relative = PurePosixPath(match[1])
    if relative.is_absolute() or '..' in relative.parts:
        fail(document, line, 'reference must stay inside the repository')
        return
    target = root / str(relative)
    try:
        target.resolve().relative_to(root)
        if not target.is_file():
            raise ValueError('missing file')
        if match[2] and int(match[2]) > len(target.read_text(encoding='utf-8').splitlines()):
            raise ValueError('line outside file')
    except (OSError, UnicodeError, ValueError):
        fail(document, line, 'referenced file or source line does not exist inside the repository')


def layout_and_privacy(path, text):
    previous = 'blank'
    fence = None
    for number, line in enumerate(text.splitlines(), 1):
        if line.endswith('  ') or line.endswith('\\'):
            fail(path, number, 'hard line break; keep each paragraph on one physical line')
        marker = re.match(r'^ {0,3}(`{3,}|~{3,})', line)
        if marker:
            if fence is None:
                fence = (marker[1][0], len(marker[1]))
            elif marker[1][0] == fence[0] and len(marker[1]) >= fence[1]:
                fence = None
            previous = 'structure'
            continue
        if fence:
            continue
        if not line.strip():
            kind = 'blank'
        elif re.match(r'^\s*(?:[-*+] |[0-9]+[.)] )', line):
            kind = 'list'
        elif re.match(r'^(?:#{1,6} |\s*\||\s*>)', line):
            kind = 'structure'
        else:
            kind = 'paragraph'
        if kind == 'paragraph' and previous in ('paragraph', 'list'):
            fail(path, number, 'hard-wrapped paragraph or list item')
        previous = kind
    for pattern, message in ((email_pattern, 'mail address is forbidden'),
                             (ticket_quote, 'ticket identifier followed by quoted prose is forbidden')):
        for match in pattern.finditer(text):
            fail(path, text.count('\n', 0, match.start()) + 1, message)


documents = sorted(directory.glob('*.md'))
texts = {}
for path in documents:
    try:
        texts[path] = path.read_text(encoding='utf-8')
    except (OSError, UnicodeError):
        fail(path, 1, 'document must be readable UTF-8')
        continue
    layout_and_privacy(path, texts[path])

template = directory / '0000-template.md'
readme = directory / 'README.md'
for required in (template, readme):
    if required not in texts:
        fail(required, 1, 'required ADR document is missing')

records = {}
for path, text in texts.items():
    if path == readme:
        continue
    is_template = path == template
    filename = re.fullmatch(r'([0-9]{4})-[a-z0-9]+(?:-[a-z0-9]+)*\.md', path.name)
    if not filename or (filename[1] == '0000' and not is_template):
        fail(path, 1, 'expected NNNN-lowercase-title.md')
        continue
    title = re.fullmatch(r'# (NNNN|[0-9]{4}) (.+)', text.splitlines()[0] if text else '')
    if not title or title[1] != ('NNNN' if is_template else filename[1]):
        fail(path, 1, 'title number must match the ADR filename or template placeholder')
        continue
    headings = list(re.finditer(r'^## (.+)$', text, re.M))
    if [match[1] for match in headings] != sections:
        fail(path, 1, 'required sections must occur exactly once in the required order')
        continue
    preamble = text[:headings[0].start()]
    metadata = list(re.finditer(r'^- \*\*(Status|Date|Decided by|Sources)\*\*: (.+)$', preamble, re.M))
    if [match[1] for match in metadata] != fields:
        fail(path, 1, 'required metadata fields must occur once in the required order')
        continue
    values = {match[1]: match[2] for match in metadata}
    valid_status = (values['Status'] == template_status if is_template
                    else bool(status_pattern.fullmatch(values['Status'])))
    if not valid_status:
        fail(path, 3, 'invalid status value')
    if values['Decided by'] != 'Kai Ebert':
        fail(path, 5, 'Decided by must use the required maintainer attribution')
    if is_template:
        if values['Date'] != 'YYYY-MM-DD':
            fail(path, 4, 'template date must use YYYY-MM-DD')
    else:
        try:
            datetime.date.fromisoformat(values['Date'])
            if not re.fullmatch(r'[0-9]{4}-[0-9]{2}-[0-9]{2}', values['Date']):
                raise ValueError('date shape')
        except ValueError:
            fail(path, 4, 'date must be a valid YYYY-MM-DD date')
    sources = re.findall(r'`([^`]+)`', values['Sources'])
    if not sources or len(sources) != len(set(sources)):
        fail(path, 6, 'Sources must contain distinct cited references in backticks')
    if not is_template:
        for source in sources:
            if re.fullmatch(r'[0-9a-f]{7,40}|v[0-9]+\.[0-9]+\.[0-9]+(?:-[a-z0-9.]+)?', source):
                continue  # Historical objects need not be present in shallow CI checkouts.
            if not re.fullmatch(r'[^:]+:[1-9][0-9]*', source):
                fail(path, 6, 'file sources must include a physical line number')
            existing_reference(source, path, 6)
    bodies = {}
    for index, heading in enumerate(headings):
        end = headings[index + 1].start() if index + 1 < len(headings) else len(text)
        bodies[heading[1]] = text[heading.end():end].strip()
        if not bodies[heading[1]]:
            fail(path, text.count('\n', 0, heading.start()) + 1, 'required section must not be empty')
    if not is_template:
        for line in bodies['Decision'].splitlines():
            if line.strip() and not set(re.findall(r'`([^`]+)`', line)).intersection(sources):
                fail(path, text.count('\n', 0, headings[2].end()) + 1, 'each Decision line needs a reference listed in Sources')
    evidence = re.findall(r'`([^`]+)`', bodies['Evidence'])
    if not evidence or not any(ref.startswith('tests/') for ref in evidence):
        fail(path, 1, 'Evidence must name an existing test file')
    for ref in evidence:
        if not ref.startswith(('tests/', '.github/workflows/')):
            fail(path, 1, 'Evidence references must name test files or CI workflows')
        existing_reference(ref, path, 1)
    if not is_template:
        number = filename[1]
        if number in records:
            fail(path, 1, 'duplicate ADR number')
        records[number] = (path, title[2], values['Status'])

if not records:
    fail(directory, 1, 'no numbered ADRs found')
elif sorted(int(number) for number in records) != list(range(1, len(records) + 1)):
    fail(directory, 1, 'ADR numbers must be contiguous starting at 0001')
for number, (path, title, status) in records.items():
    if status.startswith('superseded by '):
        target = status.rsplit(' ', 1)[1]
        if target not in records or target == number:
            fail(path, 3, 'supersession must name another existing ADR')

if readme in texts:
    rows = re.findall(r'^\| \[([0-9]{4})\]\(([^)]+)\) \| ([^|]+) \| ([^|]+) \| ([^|]+) \|$', texts[readme], re.M)
    if len(rows) != len(records) or len({row[0] for row in rows}) != len(rows):
        fail(readme, 1, 'index must contain exactly one row for each ADR')
    for number, filename, title, status, scope in rows:
        expected = records.get(number)
        if not expected or (filename, title.strip(), status.strip()) != (expected[0].name, expected[1], expected[2]) or not scope.strip():
            fail(readme, 1, 'index number, title, status or one-line scope does not match the ADR')

if 'BUREAU_ADR_DENYLIST' not in os.environ:
    print('SKIP ADR deny-list scan (BUREAU_ADR_DENYLIST unset)')
else:
    try:
        raw = os.environ['BUREAU_ADR_DENYLIST']
        if not raw.strip():
            raise ValueError('empty path')
        denylist = Path(raw).expanduser().resolve()
        try:
            denylist.relative_to(root)
        except ValueError:
            pass
        else:
            raise ValueError('inside repository')
        entries = [(number, line.strip()) for number, line in enumerate(denylist.read_text(encoding='utf-8').splitlines(), 1)
                   if line.strip() and not line.lstrip().startswith('#')]
        for path, text in texts.items():
            # Case folding can change string length; scan physical lines so
            # diagnostics retain correct line numbers without revealing text.
            for line_number, line in enumerate(text.splitlines(), 1):
                for entry_number, entry in entries:
                    if entry.casefold() in line.casefold():
                        fail(path, line_number, f'external deny-list entry {entry_number} matched')
        print(f'CHECK ADR external deny-list ({len(entries)} entries; contents not printed)')
    except (OSError, UnicodeError, ValueError):
        fail(directory, 1, 'BUREAU_ADR_DENYLIST must name a readable UTF-8 file outside the repository')

if errors:
    print('\n'.join('FAIL ' + error for error in errors), file=sys.stderr)
    sys.exit(1)
print(f'PASS ADR contract: {len(records)} records, template, index, references, paragraph layout and privacy checks')
PY
