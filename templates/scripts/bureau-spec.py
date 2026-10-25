#!/usr/bin/env python3
"""Spec phase checkpoints and deterministic governance checks (stdlib only).

Checkpoints use bureau-evidence's versioned store, lock and atomic writer. They
are evidence of completed phases, not permission to take over a workspace.
"""
import argparse
import hashlib
import html
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import unicodedata

sys.dont_write_bytecode = True
_module = importlib.util.spec_from_file_location('bureau_evidence', Path(__file__).with_name('bureau-evidence.py'))
e = importlib.util.module_from_spec(_module)
_module.loader.exec_module(e)
PHASES = ('specify', 'plan', 'tasks')


DECISIONS = r'(?:not required|required|not applicable|applicable|n/a|nicht erforderlich|erforderlich|nicht anwendbar|anwendbar|nicht zutreffend|zutreffend)'


def _normalize_governance(value):
    value = unicodedata.normalize('NFKC', html.unescape(value))
    return ''.join(char for char in value if unicodedata.category(char) != 'Cf')


def _governance_controls(value):
    return any((ord(char) < 32 and char != '\t') or 127 <= ord(char) <= 159 for char in value)


def _only_governance_placeholder(value):
    """Recognize entire placeholder/deferral statements, never words in prose.

    Adequacy, specificity and circular reasoning belong to the spec reviewer.
    A rationale needs neither a particular verb nor terminal punctuation.
    """
    value = _normalize_governance(value)
    if not any(char.isalnum() for char in value): return True
    lines = [line.strip() for line in value.split('\n') if line.strip()]
    if not lines: return True
    for line in lines:
        line = re.sub(r'^(?:[-+]\s+|\d+[.)]\s+)?(?:\[[ xX]\]\s*)?', '', line)
        line = line.strip().rstrip('.!?;:').strip()
        if re.fullmatch(r'\{\{.*?\}\}|[\[<](?:describe|add|enter|state|write|list|explain|insert|fill|provide|justify|summarize|specify|rationale|(?:Begründung\s+)?(?:beschreiben|einfügen|ergänzen))\b[^\]>]*[\]>]', line, re.I): continue
        # Split common template alternatives, preserving N/A as one token.
        parts = re.split(r'[/,|;]|\s+(?:or|and|oder|und)\s+', re.sub(r'\bn/a\b', 'not applicable', line, flags=re.I), flags=re.I)
        if len(parts) > 1:
            if all(_only_governance_placeholder(part) for part in parts): continue
            return False
        line = line.translate(str.maketrans('', '', "[](){}<>\"'“”‘’«»*_"))
        line = line.strip(' \t.,!?;:/\\|+=~-–—…')
        if re.fullmatch(r'(?:(?:Access|Logging|Retention)\s*:\s*)*(?:Access|Logging|Retention)', line, re.I): continue
        if not line or re.fullmatch(DECISIONS + r'|none|keine|entfällt|offen|[-–—]+', line, re.I): continue
        # A suffix describes when/where the deferred answer will be supplied.
        # Keep it inside one statement: a real explanation elsewhere qualifies.
        later = r'(?:[ \t]+(?:after|before|later|following|once|nach|später|bis)\b[^.!?\n]*)?'
        if re.fullmatch(r'(?:T[.\s-]*B[.\s-]*D\.?|TODO|FIXME|tbc|placeholder)' + later, line, re.I): continue
        english = r'(?:(?:the )?(?:explanation|rationale|decision|justification|details?|assessment|access|logging|retention)(?: is| are)?\s+)?'
        # "Pending events are logged ..." describes behavior. Only a bare
        # pending marker or an explicit review/approval deferral qualifies.
        pending = r'pending(?:\s+(?:(?:a|an|the|final|further|security|privacy|legal|compliance|technical|human|team|architecture)\s+)*(?:review|approval|decision|assessment|confirmation|input|discussion|sign-off|release|implementation)(?:\s+(?:from|by|on|of|for|with)\b[^.!?\n]*)?)?'
        if re.fullmatch(english + r'(?:' + pending + r'|to[\s-]+follow|to[\s-]+be[\s-]+(?:defined|decided|determined|filled|completed|added|written|provided|confirmed|clarified|updated|done)|will[\s-]+be[\s-]+(?:added|provided|completed|documented))' + later, line, re.I): continue
        german = r'(?:(?:die|der|das)\s+)?(?:(?:Begründung|Erklärung|Entscheidung|Bewertung|Details)\s+)?'
        if re.fullmatch(german + r'(?:folgt|folgen|wird nachgereicht|wird (?:noch )?(?:geklärt|definiert|entschieden)|noch offen|noch zu klären|noch zu definieren|noch zu ergänzen|wird ergänzt|wird nachgetragen)' + later, line, re.I): continue
        return False
    return True


def governance_error(path):
    """Catch honest placement/placeholder mistakes, not deliberate obfuscation.

    Spec review owns the section's substantive reality and adequacy.
    """
    try: text = Path(path).read_bytes().decode('utf-8')
    except (OSError, UnicodeError): return 'Governance and security: spec.md is missing or unreadable'
    # Markdown has LF/CRLF lines; a bare CR or another control cannot make one.
    text = text.replace('\r\n', '\n')
    raw_lines = text.split('\n')
    # Mask comments, including unclosed ones, without moving text to column 0.
    text = re.sub(r'<!--.*?(?:-->|$)', lambda m: re.sub(r'[^\r\n]', ' ', m[0]), text, flags=re.S)
    lines = []; placement_hint = ''
    front_matter = _normalize_governance(raw_lines[0]).strip() == '---'
    for index, line in enumerate(text.split('\n')):
        if front_matter:
            if index and raw_lines[index].strip() == '---': front_matter = False
            continue
        cutoff_line = re.sub(r'^(?:> ?)+', '', line)
        cutoff_raw = re.sub(r'^(?:> ?)+', '', raw_lines[index])
        if cutoff_line.strip() and re.match(r'^(?: {4}| *\t)', cutoff_line):
            placement_hint = '; move the Governance and security section above the first code block'
            break
        if re.match(r'^[ \t]*<(?:[A-Za-z!]|/)', _normalize_governance(cutoff_raw)):
            placement_hint = '; move the Governance and security section above the first HTML block'
            break
        if re.match(r'^\s*(?:(?:[-*+]|\d+[.)]|>)\s*)*(?:`{3,}|~{3,})', line):
            placement_hint = '; move the Governance and security section above the first code block'
            break
        lines.append(line)
    section = []; level = None
    for line in lines:
        # Keep Markdown syntax physical: NFKC must not turn NBSP into the
        # required ASCII space after #, or move an indented line to column 0.
        syntax_line = ''.join(char for char in line if unicodedata.category(char) != 'Cf')
        heading = re.match(r'^(#{1,6}) +(.+?)[ \t]*#*[ \t]*$', syntax_line)
        if heading:
            if level is not None and len(heading[1]) <= level: break
            if re.fullmatch(r'governance\s+(?:and|&)\s+security', _normalize_governance(heading[2]), re.I):
                if _governance_controls(line) or _governance_controls(_normalize_governance(line)):
                    return 'Governance and security: control character in heading'
                level = len(heading[1]); continue
        if level is not None:
            if section and section[-1].strip() and re.fullmatch(r' {0,3}(?:-+|=+)[ \t]*', line):
                section.pop()  # The preceding text belongs to the setext heading.
                break
            if _governance_controls(line) or _governance_controls(_normalize_governance(line)):
                return 'Governance and security: control character in decision/rationale'
            section.append(line)
    if level is None: return 'Governance and security: required heading is missing' + placement_hint
    body = '\n'.join(section)
    # Decision/rationale labels start at column 0; risk fields may be bullets.
    # Strip decoration so it cannot hide T**B**D or T_B_D from the check.
    fields = {}; current = None
    for raw in body.split('\n'):
        line = re.sub(r'[*_]', '', _normalize_governance(raw))
        if current == 'rationale' and line.lstrip().startswith('>'): continue
        if current == 'rationale' and line.strip() and re.match(r'^(?: {4}| *\t)', raw):
            return 'Governance and security: rationale continuation must not be indented code' + placement_hint
        field = re.match(r'^[ \t]*(?:(?:[-+]|\d+[.)]|>)[ \t]*)*(?:\[[ xX]\][ \t]+)?(Decision|Entscheidung|Rationale|Begründung|Access|Logging|Retention)[ \t]*:[ \t]*(.*)$', line, re.I)
        if field:
            current = {'entscheidung': 'decision', 'begründung': 'rationale'}.get(field[1].lower(), field[1].lower())
            if current in ('decision', 'rationale') and not re.match(r'^(?:\*\*|__)?(?:Decision|Entscheidung|Rationale|Begründung)\b', _normalize_governance(raw), re.I):
                return 'Governance and security: decision and rationale must start at column 0' + placement_hint
            if current in ('decision', 'rationale') and field[2].startswith('`'):
                return 'Governance and security: decision/rationale must be plain text, not a code span' + placement_hint
            fields[current] = field[2]
        elif line.lstrip().startswith('#'): current = None
        elif current:
            if current == 'decision' and re.match(r'^\s|^(?:[-*+>]|\d+[.)])', raw): current = None
            else: fields[current] += '\n' + line.strip()
    # Existing specs can express the same explicit decision in one sentence,
    # e.g. "Not required because this changes only static documentation."
    # Do not require a template rewrite merely to add the two field labels.
    inline_lines = [_normalize_governance(line) for line in body.split('\n') if line.strip()]
    decision = fields.get('decision', inline_lines[0] if inline_lines and not fields else '')
    match = re.match(r'^(' + DECISIONS + r')(?=$|[\s,:;.!?—-])', decision, re.I)
    if not match:
        return 'Governance and security: explicit Decision: required/not required is missing' + placement_hint
    if not fields.get('rationale', '').strip():
        inline = decision[match.end():].lstrip(' \t\n,:;.—-')
        inline = re.sub(r'^(?:because|weil|da)\b[ \t]*', '', inline, flags=re.I)
        fields['rationale'] = inline or ('\n'.join(fields.get(name, '') for name in ('access', 'logging', 'retention')) if 'rationale' in fields else '')
    for name in ('access', 'logging', 'retention'):
        if name in fields and _only_governance_placeholder(fields[name]):
            return 'Governance and security: ' + name.title() + ' is empty or only a placeholder/decision' + placement_hint
    if fields.get('rationale', '').lstrip().startswith('`') or _only_governance_placeholder(fields.get('rationale', '')):
        return 'Governance and security: explain the decision in Rationale: (including not required/N/A)' + placement_hint
    return None


def git(repo, *args):
    return subprocess.check_output(['git', '-C', str(repo), *args], text=True).strip()


def sha(value):
    return hashlib.sha256(value).hexdigest()


CHECKPOINT_VERSION = 2


def disable_reuse(repo, issue):
    """A durable opt-out survives optional evidence writes and lease release."""
    common = (Path(repo) / git(repo, 'rev-parse', '--git-common-dir')).resolve()
    e.atomic(common / 'bureau' / 'spec-checkpoints-disabled' / (e.ticket(issue) + '.json'),
             dict(issue=issue, reuse_disabled=True))


class Checkpoints:
    def __init__(self, repo, store=None):
        self.repo = Path(repo).resolve()
        common = (self.repo / git(self.repo, 'rev-parse', '--git-common-dir')).resolve()
        self.store = store or e.Store(common / 'bureau')

    def path(self, issue):
        return self.store.root / 'spec-checkpoints' / (e.ticket(issue) + '.json')

    def disabled_path(self, issue):
        return self.store.root.parent.parent / 'spec-checkpoints-disabled' / (e.ticket(issue) + '.json')

    def read(self, issue):
        if self.disabled_path(issue).exists(): return None
        try: value = e.read(self.path(issue))
        except (ValueError, TypeError): return None
        if not isinstance(value, dict) or value.get('schema_version') != e.VERSION or value.get('checkpoint_version') != CHECKPOINT_VERSION:
            return None  # Pre-commit checkpoints are never migrated/inferred.
        if value.get('issue') != issue or value.get('record_type') != 'spec_checkpoint': return None
        return value

    def directory(self, specs, directory, issue):
        specs = (self.repo / specs).resolve()
        path = Path(directory)
        if not path.is_absolute(): path = self.repo / path
        resolved = path.resolve()
        if self.repo not in specs.parents or resolved.parent != specs or path.is_symlink():
            raise ValueError('spec directory must be an unlinked feature inside this workspace specs_dir')
        if not re.fullmatch(r'[0-9]+-' + re.escape(issue.lower()), resolved.name):
            raise ValueError('spec directory does not belong to this ticket reservation')
        return resolved

    def artifacts(self, directory, phase):
        required = {'specify': 'spec.md', 'plan': 'plan.md', 'tasks': 'tasks.md'}[phase]
        directory = Path(directory)
        files = list(directory.rglob('*'))
        hashes = {}
        for path in sorted(files):
            if path.is_symlink(): raise ValueError('symlink in spec artifacts')
            if path.is_file(): hashes[str(path.relative_to(directory))] = sha(path.read_bytes())
        if required not in hashes or not (directory / required).read_bytes().strip():
            raise ValueError('successful ' + phase + ' requires nonempty ' + required)
        return hashes

    def committed_artifacts(self, directory, phase, commit):
        """Hash Git blobs, never staged, ignored or uncommitted working files."""
        if not re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}', commit or ''):
            raise ValueError('invalid checkpoint commit')
        relative = directory.relative_to(self.repo).as_posix()
        listing = subprocess.check_output(['git', '-C', str(self.repo), 'ls-tree', '-rz', commit, '--', relative])
        hashes = {}
        for entry in listing.split(b'\0'):
            if not entry: continue
            meta, raw = entry.split(b'\t', 1)
            name = raw.decode()[len(relative) + 1:]
            if meta.split()[0] not in (b'100644', b'100755'): raise ValueError('non-file in committed spec artifacts')
            content = subprocess.check_output(['git', '-C', str(self.repo), 'show', commit + ':' + raw.decode()])
            hashes[name] = sha(content)
            if name == {'specify': 'spec.md', 'plan': 'plan.md', 'tasks': 'tasks.md'}[phase] and not content.strip():
                raise ValueError('empty committed phase artifact')
        if {'specify': 'spec.md', 'plan': 'plan.md', 'tasks': 'tasks.md'}[phase] not in hashes:
            raise ValueError('missing committed phase artifact')
        return hashes

    def prefix(self, value, directory, fingerprint):
        """Only a contiguous, reachable, fingerprint/hash-verified commit chain."""
        phases = value.get('phases', {})
        matched = []
        for phase in PHASES:
            try:
                record = phases[phase]
                if record['input_sha256'] != fingerprint: break
                commit = record['commit']
                if not any(subprocess.run(['git', '-C', str(self.repo), 'merge-base', '--is-ancestor', commit, ref],
                                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0
                           for ref in ('refs/heads/' + value['branch'], 'refs/remotes/origin/' + value['branch'])): break
                if record['artifacts'] != self.committed_artifacts(directory, phase, commit): break
                if matched and subprocess.run(['git', '-C', str(self.repo), 'merge-base', '--is-ancestor',
                                               phases[matched[-1]]['commit'], commit],
                                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode: break
            except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError): break
            matched.append(phase)
        return matched

    def reset_target(self, issue, specs):
        """Return data for a normal registered-worker reset, never permission."""
        with self.store.guard():
            value = self.read(issue)
            if not value: return None
            directory = self.directory(specs, value['spec_directory'], issue)
            if value['branch'] != directory.name or value['specs_dir'] != directory.parent.relative_to(self.repo).as_posix(): return None
            numbers = e.read(self.store.root.parent.parent / 'spec-numbers.json', {})
            if numbers.get(issue) != value['number'] or int(directory.name.split('-', 1)[0]) != value['number']: return None
            prefix = self.prefix(value, directory, value['input_sha256'])
            if not prefix: return None
            commit = value['phases'][prefix[-1]]['commit']
            remote = 'refs/remotes/origin/' + value['branch']
            if subprocess.run(['git', '-C', str(self.repo), 'show-ref', '--verify', '--quiet', remote]).returncode == 0:
                if subprocess.run(['git', '-C', str(self.repo), 'merge-base', '--is-ancestor', remote, commit],
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode:
                    return None  # Never rewind published work or need a force push.
            return dict(branch=value['branch'], commit=commit)

    def begin(self, issue, specs, directory, number, inputs):
        directory = self.directory(specs, directory, issue)
        identity = dict(spec_directory=directory.relative_to(self.repo).as_posix(),
                        specs_dir=directory.parent.relative_to(self.repo).as_posix(), branch=directory.name, number=number)
        fingerprint = sha(json.dumps(inputs, sort_keys=True, separators=(',', ':')).encode())
        with self.store.guard():
            value = self.read(issue)
            if value is None or any(value.get(k) != v for k, v in identity.items()):
                value = dict(schema_version=e.VERSION, checkpoint_version=CHECKPOINT_VERSION,
                             record_type='spec_checkpoint', issue=issue, phases={}, **identity)
            prefix = []
            if os.environ.get('BUREAU_SPEC_FORCE_FRESH') != '1' and value.get('input_sha256') == fingerprint:
                prefix = self.prefix(value, directory, fingerprint)
                # The worker must actually have reset to that commit. Direct
                # stage calls cannot qualify arbitrary dirty/staged files.
                if prefix and (git(self.repo, 'rev-parse', 'HEAD') != value['phases'][prefix[-1]]['commit']
                               or git(self.repo, 'status', '--porcelain', '--', directory.relative_to(self.repo).as_posix())):
                    prefix = []
            value['phases'] = {phase: value['phases'][phase] for phase in prefix}
            value.update(input_sha256=fingerprint, run_id=os.environ.get('BUREAU_RUN_ID'),
                         stage_attempt_id=os.environ.get('BUREAU_STAGE_ATTEMPT_ID'), updated_at=self.store.clock())
            self.store._write(self.path(issue), value)
            self.disabled_path(issue).unlink(missing_ok=True)
            return prefix

    def commit_phase(self, issue, specs, directory, phase):
        directory = self.directory(specs, directory, issue)
        if git(self.repo, 'branch', '--show-current') != directory.name:
            raise ValueError('phase commit requires the reserved feature branch')
        self.artifacts(directory, phase)  # A provider success still needs real artifacts.
        relative = directory.relative_to(self.repo).as_posix()
        git(self.repo, 'add', '-A', '--', relative)
        git(self.repo, 'commit', '--only', '--allow-empty', '-m', issue + ': spec ' + phase + ' checkpoint',
            '-m', 'Bureau-Generated: true', '--', relative)
        commit = git(self.repo, 'rev-parse', 'HEAD')
        self.committed_artifacts(directory, phase, commit)
        return commit

    def complete(self, issue, phase, commit):
        with self.store.guard():
            value = self.read(issue)
            if not value: raise OSError('spec checkpoint preparation unavailable')
            directory = self.directory(value['specs_dir'], value['spec_directory'], issue)
            if commit != git(self.repo, 'rev-parse', 'HEAD') or git(self.repo, 'branch', '--show-current') != value['branch']:
                raise ValueError('phase commit is not the reserved branch HEAD')
            for prior in PHASES[:PHASES.index(phase)]:
                if prior not in value['phases']: raise OSError('spec checkpoint predecessor unavailable')
            value['phases'][phase] = dict(completed_at=self.store.clock(), input_sha256=value['input_sha256'],
                                          run_id=value['run_id'], stage_attempt_id=value['stage_attempt_id'], commit=commit,
                                          artifacts=self.committed_artifacts(directory, phase, commit))
            value['updated_at'] = self.store.clock()
            self.store._write(self.path(issue), value)

    def invalidate(self, issue, phase, reason):
        with self.store.guard():
            value = self.read(issue)
            if value:
                for successor in PHASES[PHASES.index(phase):]: value['phases'].pop(successor, None)
                value.update(invalidation=dict(from_phase=phase, reason_sha256=e.text_digest(reason), at=self.store.clock()))
                self.store._write(self.path(issue), value)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo', default='.')
    sub = parser.add_subparsers(dest='action', required=True)
    validate = sub.add_parser('validate'); validate.add_argument('path')
    begin = sub.add_parser('begin'); begin.add_argument('issue'); begin.add_argument('--specs-dir', required=True)
    begin.add_argument('--directory', required=True); begin.add_argument('--number', required=True, type=int)
    target = sub.add_parser('reset-target'); target.add_argument('issue'); target.add_argument('--specs-dir', required=True)
    commit = sub.add_parser('commit-phase'); commit.add_argument('issue'); commit.add_argument('phase', choices=PHASES)
    commit.add_argument('--specs-dir', required=True); commit.add_argument('--directory', required=True)
    complete = sub.add_parser('complete'); complete.add_argument('issue'); complete.add_argument('phase', choices=PHASES); complete.add_argument('--commit', required=True)
    invalid = sub.add_parser('invalidate'); invalid.add_argument('issue'); invalid.add_argument('phase', choices=PHASES)
    invalid.add_argument('--reason', required=True)
    args = parser.parse_args()
    try:
        if args.action == 'validate':
            error = governance_error(args.path)
            if error: print(error, file=sys.stderr); return 1
        else:
            checkpoints = Checkpoints(args.repo)
            if args.action == 'begin':
                print(json.dumps(checkpoints.begin(args.issue, args.specs_dir, args.directory, args.number, json.load(sys.stdin))))
            elif args.action == 'reset-target': print(json.dumps(checkpoints.reset_target(args.issue, args.specs_dir)))
            elif args.action == 'commit-phase': print(checkpoints.commit_phase(args.issue, args.specs_dir, args.directory, args.phase))
            elif args.action == 'complete': checkpoints.complete(args.issue, args.phase, args.commit)
            else: checkpoints.invalidate(args.issue, args.phase, args.reason)
        return 0
    except OSError as exc:
        if args.action not in ('validate', 'commit-phase'):
            try: disable_reuse(args.repo, args.issue)
            except (OSError, ValueError, subprocess.SubprocessError): pass
        # Internal helper status 2 means optional storage is unavailable. The
        # stage consumes it; contract/identity errors still use 24 below.
        e.warn()
        return 2
    except (ValueError, KeyError, TypeError, subprocess.CalledProcessError) as exc:
        if args.action == 'reset-target':
            print('bureau spec: ignoring invalid checkpoint: ' + str(exc), file=sys.stderr); print('null'); return 0
        print('bureau spec: ' + str(exc), file=sys.stderr); return 24


if __name__ == '__main__':
    sys.exit(main())
