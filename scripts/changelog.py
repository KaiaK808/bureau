#!/usr/bin/env python3
"""Check, preview and release this source repository's changelog fragments."""
import argparse
from datetime import date
import os
from pathlib import Path
import re
import shutil
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
SECTIONS = ('Added', 'Changed', 'Deprecated', 'Removed', 'Fixed', 'Security', 'Documentation')
POINTER = 'Pending changes are recorded in [changes/unreleased/](changes/unreleased/).'
UNRELEASED = '## [Unreleased]\n\n' + POINTER + '\n\n'


def read_fragments(changelog):
    fragments = []
    errors = []
    for path in sorted((ROOT/'changes').glob('.release-*')):
        if path.is_dir():
            version = path.name[len('.release-'):].rsplit('-', 1)[0]
            if re.search(r'^## \['+re.escape(version)+r'\](?:\s|$)', changelog, re.M):
                recovery = (f'version {version} is already in CHANGELOG.md; '
                            'staged files are released and may be deleted')
            else:
                recovery = (f'version {version} is not in CHANGELOG.md; '
                            'move staged files back to changes/unreleased/')
            errors.append(f'{path.relative_to(ROOT)}: unfinished release staging directory; {recovery}')
    for path in sorted((ROOT/'changes/unreleased').iterdir()):
        if path.name == '.gitkeep':
            continue
        try:
            if not re.fullmatch(r'[a-z0-9-]+\.md', path.name):
                raise ValueError('expected a lowercase letters/digits/hyphens slug ending in .md')
            if path.is_symlink() or not path.is_file():
                raise ValueError('expected a regular fragment file')
            text = path.read_bytes().decode('utf-8')
            lines = text.splitlines()
            if not lines or lines[0] not in ('section: '+section for section in SECTIONS):
                raise ValueError('first line must be section: ' + ' / '.join(SECTIONS))
            if len(lines) < 2 or lines[1] != '':
                raise ValueError('section line must be followed by a blank line')
            if any(line != line.rstrip() for line in text.split('\n')) or text.endswith('\n\n'):
                raise ValueError('trailing whitespace or blank lines at end of file')
            entry = lines[2:]
            if (not entry or not entry[0].startswith('- ') or not entry[0][2:].strip()
                    or any(line and (not line.startswith('  ')
                                     or re.match(r'\s*(?:[-+*]|[0-9]+[.)])(?:\s|$)', line))
                           for line in entry[1:])):
                raise ValueError("expected one non-empty '- ' list item; indent continuation lines")
            fragments.append((path, lines[0][len('section: '):], '\n'.join(entry)))
        except (ValueError, UnicodeError) as error:
            errors.append(f'{path.relative_to(ROOT)}: {error}')
    return fragments, errors


def unreleased_bounds(text):
    headings = list(re.finditer(r'^## \[Unreleased\]\n', text, re.M))
    if len(headings) != 1:
        raise ValueError('CHANGELOG.md: expected exactly one ## [Unreleased] section')
    heading = headings[0]
    next_heading = re.search(r'^## ', text[heading.end():], re.M)
    end = heading.end() + next_heading.start() if next_heading else len(text)
    if text[heading.start():end] != UNRELEASED:
        raise ValueError('CHANGELOG.md: Unreleased must contain only this pointer line: ' + POINTER)
    return heading.start(), end


def render(heading, fragments):
    blocks = [heading]
    for section in SECTIONS:
        entries = [entry for _, name, entry in fragments if name == section]
        if entries:
            blocks.extend(['### '+section, *entries])
    return '\n\n'.join(blocks) + '\n\n'


def release_fragments(path, text, fragments, version, heading):
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', newline='\n',
                                         dir=path.parent, prefix='.changelog-', delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, path.stat().st_mode)
        staging = Path(tempfile.mkdtemp(dir=ROOT/'changes', prefix=f'.release-{version}-'))
        try:
            for fragment, _, _ in fragments:
                os.rename(fragment, staging/fragment.name)
            os.replace(temporary, path)
        except BaseException:
            if heading in path.read_bytes().decode('utf-8').splitlines():
                try:
                    shutil.rmtree(staging)
                except BaseException:
                    report_staging(staging)
                    raise
                raise
            for fragment in sorted(staging.iterdir()):
                try:
                    os.rename(fragment, ROOT/'changes/unreleased'/fragment.name)
                except BaseException as error:
                    print(f'Cannot restore {fragment}: {error}', file=sys.stderr)
            try:
                staging.rmdir()
            except BaseException:
                report_staging(staging)
            raise
        try:
            shutil.rmtree(staging)
        except BaseException:
            report_staging(staging)
            raise
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def report_staging(staging):
    print(f'Release staging remains: {staging}', file=sys.stderr)
    for path in sorted(staging.iterdir()):
        print(f'  {path.name}', file=sys.stderr)


def main():
    parser = argparse.ArgumentParser(description=__doc__, epilog='Exit codes: 0 success, '
                                     '1 validation failure, 2 usage or I/O error.')
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('check', help='validate fragments and the Unreleased pointer')
    commands.add_parser('render', help='print pending entries without changing files')
    release = commands.add_parser('release', help='date the pending entries and remove their fragments')
    release.add_argument('version')
    release.add_argument('date')
    args = parser.parse_args()
    if args.command == 'release':
        if not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9.+-]*', args.version):
            parser.error('VERSION must contain only letters, digits, dots, plus signs or hyphens')
        try:
            if not re.fullmatch(r'[0-9]{4}-[0-9]{2}-[0-9]{2}', args.date):
                raise ValueError
            date.fromisoformat(args.date)
        except ValueError:
            parser.error('DATE must be a valid date in YYYY-MM-DD form')
    try:
        changelog = ROOT/'CHANGELOG.md'
        text = changelog.read_bytes().decode('utf-8')
        fragments, errors = read_fragments(text)
        try:
            _, end = unreleased_bounds(text)
        except ValueError as error:
            errors.append(str(error))
        if errors:
            print('\n'.join(errors), file=sys.stderr)
            return 1
        if args.command == 'check':
            print(f'PASS changelog: {len(fragments)} fragments and Unreleased pointer')
        elif args.command == 'render':
            sys.stdout.write(render('## [Unreleased]', fragments))
        else:
            if re.search(r'^## \['+re.escape(args.version)+r'\](?:\s|$)', text, re.M):
                raise ValueError(f'CHANGELOG.md: version {args.version} already exists')
            if not fragments:
                raise ValueError('no changelog fragments to release')
            heading = f'## [{args.version}] - {args.date}'
            section = render(heading, fragments)
            release_fragments(changelog, text[:end] + section + text[end:], fragments, args.version, heading)
            print(f'Released {len(fragments)} fragments as {args.version}')
    except (ValueError, UnicodeError) as error:
        print(f'Changelog validation failed: {error}', file=sys.stderr)
        return 1
    except OSError as error:
        print(f'Changelog I/O failed: {error}', file=sys.stderr)
        return 2
    return 0


if __name__ == '__main__':
    sys.exit(main())
