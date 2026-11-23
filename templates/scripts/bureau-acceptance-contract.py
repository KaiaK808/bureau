#!/usr/bin/env python3
"""Offline declaration validation only; no verifier execution or stage integration."""
import argparse
import json
import os
import re
import stat
import sys

MAX_BYTES = 256 * 1024
MAX_REQUIREMENTS = 128
MAX_CHECKS = 256
SPEC_DECLARATION = re.compile(r'^\s*[-*]\s+\*\*((?:FR|SC)-[0-9]{3,})\*\*\s*:')
TASK_DECLARATION = re.compile(r'^\s*-\s+\[[ xX]\]\s+(T[0-9]{3,})\b')
REQUIREMENT_ID = re.compile(r'(?:FR|SC)-[0-9]{3,}')
TASK_ID = re.compile(r'T[0-9]{3,}')
FENCE = re.compile(r'^ {0,3}(`{3,}|~{3,})(.*)$')
SCOPE = ('Validation checks only whether the declaration is well formed. It does not prove '
         'requirements are complete, that any verifier exists or passed, or that the product '
         'is accepted. Enforcement is not active.')


def pointer(parent, key):
    return parent + '/' + str(key).replace('~', '~0').replace('/', '~1')


def read_regular(filename, limit=None):
    """Refuse an input that is a symlink; parents may be system links such as /tmp on macOS."""
    # The kernel walks the parent, so a parent step after a symlink keeps its real meaning.
    parent, name = os.path.split(os.fspath(filename))
    directory = os.open(parent or '.', os.O_RDONLY | os.O_DIRECTORY)
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        with os.fdopen(fd, 'rb') as source:
            info = os.fstat(source.fileno())
            if not stat.S_ISREG(info.st_mode):
                raise OSError('input must be a regular file')
            if limit is not None and info.st_size > limit:
                raise ValueError('contract exceeds 256 KiB')
            data = source.read() if limit is None else source.read(limit + 1)
            if limit is not None and len(data) > limit:
                raise ValueError('contract exceeds 256 KiB')
            return data
    finally:
        os.close(directory)


class ObjectPairs(list):
    """Distinguish JSON objects from arrays while preserving duplicate keys."""


class Validator:
    def __init__(self):
        self.errors = []

    def error(self, location, message):
        self.errors.append({'location': location, 'message': message})

    def unpack(self, value, location=''):
        if isinstance(value, ObjectPairs):
            result = {}
            for key, child in value:
                at = pointer(location, key)
                if key in result:
                    self.error(at, 'duplicate JSON key')
                result[key] = self.unpack(child, at)
            return result
        if isinstance(value, list):
            return [self.unpack(child, pointer(location, i)) for i, child in enumerate(value)]
        return value

    def shape(self, value, location, required, optional=()):
        if not isinstance(value, dict):
            self.error(location, 'expected an object')
            return False
        for key in sorted(set(required) - value.keys()):
            self.error(pointer(location, key), 'missing field')
        for key in sorted(value.keys() - set(required) - set(optional)):
            self.error(pointer(location, key), 'unknown field')
        return True

    def string(self, value, location, pattern=None):
        if not isinstance(value, str) or not value.strip():
            self.error(location, 'expected a nonblank string')
            return False
        if pattern and not pattern.fullmatch(value):
            self.error(location, 'invalid identifier or hash')
            return False
        return True

    def ids(self, value, location, pattern):
        if not isinstance(value, list):
            self.error(location, 'expected an array')
            return []
        seen, result = set(), []
        for i, item in enumerate(value):
            at = pointer(location, i)
            if self.string(item, at, pattern):
                if item in seen:
                    self.error(at, 'duplicate identifier')
                seen.add(item)
                result.append(item)
        return result

    def declarations(self, text, pattern, location):
        found, fence = {}, ''
        for number, line in enumerate(text.splitlines(), 1):
            marker = FENCE.match(line)
            if fence:
                if (marker and marker[1][0] == fence[0] and len(marker[1]) >= len(fence)
                        and not marker[2].strip()):
                    fence = ''
                continue
            if marker and (marker[1][0] != '`' or '`' not in marker[2]):
                fence = marker[1]
                continue
            match = pattern.match(line)
            if match:
                if match[1] in found:
                    self.error(location + '/lines/' + str(number), 'duplicate declaration: ' + match[1])
                found[match[1]] = number
        return found

    def expectation(self, value, location):
        fields = {'pass': (), 'present': ('path',), 'nonempty': ('path',),
                  'digest': ('path', 'sha256'),
                  'fails_on_base_passes_on_candidate': ('base', 'fixture')}
        kind = value.get('kind') if isinstance(value, dict) else None
        extra = fields.get(kind, ()) if isinstance(kind, str) else ()
        if not self.shape(value, location, ('kind', *extra)):
            return
        if not isinstance(kind, str) or kind not in fields:
            self.error(location + '/kind', 'unsupported expectation kind')
            return
        if 'path' in extra:
            path = value.get('path')
            if self.string(path, location + '/path') and (
                    path.startswith('/') or re.match(r'^[A-Za-z]:', path) or '\\' in path
                    or '..' in path.split('/') or not any(p not in ('', '.') for p in path.split('/'))
                    or any(ord(c) < 32 or 127 <= ord(c) <= 159 for c in path)):
                self.error(location + '/path', 'expected a safe relative artifact path')
        if kind == 'digest':
            self.string(value.get('sha256'), location + '/sha256', re.compile(r'[0-9a-fA-F]{64}'))
        if kind == 'fails_on_base_passes_on_candidate':
            self.string(value.get('base'), location + '/base', re.compile(r'[0-9a-fA-F]{40}'))
            fixture = value.get('fixture')
            at = location + '/fixture'
            if self.shape(fixture, at, ('commit', 'sha256')):
                for key, length in (('commit', 40), ('sha256', 64)):
                    self.string(fixture.get(key), pointer(at, key),
                                re.compile(r'[0-9a-fA-F]{' + str(length) + '}'))

    def validate(self, contract, spec, tasks):
        spec_ids = self.declarations(spec, SPEC_DECLARATION, '/spec')
        task_ids = self.declarations(tasks, TASK_DECLARATION, '/tasks')
        required = ('schema_version', 'contract_id', 'revision', 'retired_requirement_ids', 'requirements')
        if not self.shape(contract, '', required):
            return
        if type(contract.get('schema_version')) is not int or contract['schema_version'] != 1:
            self.error('/schema_version', 'expected schema version 1 as an integer')
        if type(contract.get('revision')) is not int or contract['revision'] < 1:
            self.error('/revision', 'expected an integer of at least 1')
        self.string(contract.get('contract_id'), '/contract_id')
        retired = self.ids(contract.get('retired_requirement_ids'), '/retired_requirement_ids', REQUIREMENT_ID)
        requirements = contract.get('requirements')
        if not isinstance(requirements, list):
            self.error('/requirements', 'expected an array')
            return
        if len(requirements) > MAX_REQUIREMENTS:
            self.error('/requirements', 'at most 128 requirements are allowed')
        seen, mapped, check_count = set(), set(), 0
        for i, requirement in enumerate(requirements):
            at = '/requirements/' + str(i)
            if not self.shape(requirement, at, ('requirement_id', 'spec_ref', 'task_ids'), ('checks', 'mode')):
                continue
            rid, ref = requirement.get('requirement_id'), requirement.get('spec_ref')
            if self.string(rid, at + '/requirement_id', REQUIREMENT_ID):
                if rid in seen:
                    self.error(at + '/requirement_id', 'duplicate requirement ID')
                if rid in retired:
                    self.error(at + '/requirement_id', 'retired requirement ID is still in use')
                seen.add(rid)
            if self.string(ref, at + '/spec_ref', REQUIREMENT_ID):
                if ref not in spec_ids:
                    self.error(at + '/spec_ref', 'requirement is not declared in spec')
                if ref != rid:
                    self.error(at + '/spec_ref', 'spec_ref must equal requirement_id')
                mapped.add(ref)
            task_values = requirement.get('task_ids')
            valid_tasks = self.ids(task_values, at + '/task_ids', TASK_ID)
            for j, task in enumerate(task_values if isinstance(task_values, list) else []):
                if isinstance(task, str) and task in valid_tasks and task not in task_ids:
                    self.error(at + '/task_ids/' + str(j), 'task is not declared in tasks')
            if ('checks' in requirement) == ('mode' in requirement):
                self.error(at, 'use exactly one of nonempty checks or mode human')
            if 'mode' in requirement and requirement['mode'] != 'human':
                self.error(at + '/mode', 'expected human')
            if 'checks' in requirement:
                checks = requirement['checks']
                if not isinstance(checks, list) or not checks:
                    self.error(at + '/checks', 'expected a nonempty array')
                    continue
                check_count += len(checks)
                unique = set()
                for j, check in enumerate(checks):
                    cp = at + '/checks/' + str(j)
                    identity = json.dumps(check, sort_keys=True)
                    if identity in unique:
                        self.error(cp, 'duplicate check')
                    unique.add(identity)
                    if self.shape(check, cp, ('verifier_id', 'expect')):
                        self.string(check.get('verifier_id'), cp + '/verifier_id')
                        self.expectation(check.get('expect'), cp + '/expect')
        if check_count > MAX_CHECKS:
            self.error('/requirements', 'at most 256 checks are allowed in total')
        for missing in sorted(spec_ids.keys() - mapped - set(retired)):
            self.error('/requirements', 'unmapped spec requirement: ' + missing)


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise ValueError(message)


def emit(code, validator):
    errors = sorted(validator.errors, key=lambda e: (e['location'], e['message']))
    print(json.dumps({'well_formed': code == 0, 'enforcement_active': False,
                      'scope': SCOPE, 'errors': errors}, sort_keys=True))
    return code


def main(argv=None):
    validator = Validator()
    parser = Parser(add_help=False, allow_abbrev=False)
    parser.add_argument('command', choices=['validate'])
    parser.add_argument('contract')
    parser.add_argument('--spec', required=True)
    parser.add_argument('--tasks', required=True)
    try:
        args = parser.parse_args(argv)
    except ValueError as error:
        validator.error('/cli', str(error))
        return emit(2, validator)
    inputs, code = {}, 0
    for name, location in (('contract', ''), ('spec', '/spec'), ('tasks', '/tasks')):
        try:
            inputs[name] = read_regular(getattr(args, name), MAX_BYTES if name == 'contract' else None)
            if name != 'contract':
                inputs[name] = inputs[name].decode('utf-8')
        except (OSError, UnicodeError) as error:
            validator.error(location, 'unreadable regular UTF-8 input: ' + str(error))
            code = 2
        except ValueError as error:
            validator.error(location, str(error))
            if code != 2:
                code = 22
    if code:
        return emit(code, validator)
    try:
        def invalid_constant(value):
            raise ValueError('nonstandard JSON constant: ' + value)
        contract = validator.unpack(json.loads(inputs['contract'].decode('utf-8'),
                                               object_pairs_hook=ObjectPairs, parse_constant=invalid_constant))
        validator.validate(contract, inputs['spec'], inputs['tasks'])
    except (ValueError, UnicodeError, RecursionError) as error:
        validator.error('', 'malformed contract: ' + str(error))
    return emit(22 if validator.errors else 0, validator)


if __name__ == '__main__':
    sys.exit(main())
