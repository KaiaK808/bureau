"""Offline acceptance-declaration checks; fixtures never initialize Git repositories."""
import copy
import json
import math
import os
from pathlib import Path
import runpy
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'templates/scripts/bureau-acceptance-contract.py'
EXAMPLES = ROOT / 'references/acceptance-contract-examples'
MODULE = runpy.run_path(str(SCRIPT))
SPEC = '- **FR-001**: The sample MUST produce a result.\n'
TASKS = '- [ ] T001 Create the sample.\n'
REGRESSION = {'kind': 'fails_on_base_passes_on_candidate', 'base': 'a' * 40,
              'fixture': {'commit': 'b' * 40, 'sha256': 'c' * 64}}
DROP = object()


def contract():
    return {'schema_version': 1, 'contract_id': 'sample-contract', 'revision': 1,
            'retired_requirement_ids': [], 'requirements': [
                {'requirement_id': 'FR-001', 'spec_ref': 'FR-001', 'task_ids': ['T001'],
                 'checks': [{'verifier_id': 'sample-tests', 'expect': {'kind': 'pass'}}]}]}


def changed(path, value):
    result = contract()
    node = result
    for key in path[:-1]:
        node = node[key]
    if value is DROP:
        del node[path[-1]]
    else:
        node[path[-1]] = copy.deepcopy(value)
    return result


class ContractTests(unittest.TestCase):
    def setUp(self):
        temporary_root = Path(tempfile.gettempdir()).resolve()
        if any((parent / '.git').exists() for parent in (temporary_root, *temporary_root.parents)):
            raise RuntimeError('temporary root must be outside every Git repository')
        self.temp = tempfile.TemporaryDirectory(prefix='bureau-contract-', dir=temporary_root)
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name).resolve()
        self.paths = {name: self.directory / name for name in ('contract', 'spec', 'tasks')}
        self.write_inputs()

    def write_inputs(self, value=None, spec=SPEC, tasks=TASKS, raw=None):
        self.paths['contract'].write_bytes(raw if raw is not None else json.dumps(
            contract() if value is None else value).encode('utf-8'))
        self.paths['spec'].write_text(spec, encoding='utf-8')
        self.paths['tasks'].write_text(tasks, encoding='utf-8')

    def cli(self, *, arguments=None, paths=None, code=0):
        paths = paths or self.paths
        args = arguments if arguments is not None else [
            'validate', str(paths['contract']), '--spec', str(paths['spec']), '--tasks', str(paths['tasks'])]
        result = subprocess.run([sys.executable, '-I', '-B', str(SCRIPT), *args],
                                cwd=self.directory, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, code, result.stdout + result.stderr)
        self.assertEqual(result.stderr, '')
        self.assertEqual(len(result.stdout.splitlines()), 1, result.stdout)
        report = json.loads(result.stdout)
        self.assertEqual(set(report), {'well_formed', 'enforcement_active', 'scope', 'errors'})
        self.assertIs(report['well_formed'], code == 0)
        self.assertIs(report['enforcement_active'], False)
        self.assertIn('only whether the declaration is well formed', report['scope'])
        self.assertIn('does not prove requirements are complete', report['scope'])
        self.assertIn('any verifier exists or passed', report['scope'])
        self.assertIn('or that the product is accepted', report['scope'])
        self.assertIn('Enforcement is not active', report['scope'])
        self.assertEqual(bool(report['errors']), code != 0)
        for error in report['errors']:
            self.assertEqual(set(error), {'location', 'message'})
            self.assertTrue(error['location'] == '' or error['location'].startswith('/'))
        return report

    def errors(self, value, spec=SPEC, tasks=TASKS):
        validator = MODULE['Validator']()
        validator.validate(value, spec, tasks)
        return validator.errors

    def test_valid_contract(self):
        value = contract()
        value['contract_id'] = 'opaque identifier with spaces'
        value['revision'] = 2
        value['retired_requirement_ids'] = ['FR-099']
        value['requirements'][0]['checks'] += [
            {'verifier_id': 'sample-artifact', 'expect': {'kind': 'present', 'path': 'out/result.txt'}},
            {'verifier_id': 'sample-artifact', 'expect': {'kind': 'nonempty', 'path': 'out/result.txt'}},
            {'verifier_id': 'sample-artifact', 'expect': {
                'kind': 'digest', 'path': 'out/result.txt', 'sha256': 'A' * 64}},
            {'verifier_id': 'sample-regression', 'expect': copy.deepcopy(REGRESSION)}]
        value['requirements'].append({'requirement_id': 'SC-1000', 'spec_ref': 'SC-1000',
                                      'task_ids': [], 'mode': 'human'})
        self.write_inputs(value, spec=SPEC + '* **SC-1000**: Review the sample.\n')
        self.assertEqual(self.cli()['errors'], [])
        self.assertFalse((self.directory / 'out').exists(), 'artifact contents must not be inspected')

    def test_rejections(self):
        req = ('requirements', 0)
        check = (*req, 'checks', 0)
        expect = (*check, 'expect')
        cases = []

        def case(name, path, value, location=None):
            at = location if location is not None else '/' + '/'.join(map(str, path))
            cases.append((name, changed(path, value), at))

        for path in (('command',), (*req, 'command'), (*check, 'command'), (*expect, 'command')):
            case('unknown field at ' + str(path), path, 'synthetic-command')
        case('unknown fixture field', expect, {**REGRESSION, 'fixture': {
            **REGRESSION['fixture'], 'command': 'synthetic-command'}},
             '/requirements/0/checks/0/expect/fixture/command')
        for field in contract():
            case('missing root field ' + field, (field,), DROP)
        for version in (True, False, '1', 1.0, 0, 2, None):
            case('version ' + repr(version), ('schema_version',), version)
        for revision in (True, False, '1', 1.0, 0, -1, None):
            case('revision ' + repr(revision), ('revision',), revision)
        for identifier in ('', ' \t', None, True, [], {}):
            case('contract ID ' + repr(identifier), ('contract_id',), identifier)
        for field, values in (
                ('requirements', ({}, None, True)),
                ('retired_requirement_ids', ({}, None, True, [True], ['FR-01'], ['FR-099', 'FR-099']))):
            for value in values:
                case(field + repr(value), (field,), value, '/' + field)
        case('nonobject requirement', req, False)
        for field in ('requirement_id', 'spec_ref', 'task_ids'):
            case('missing requirement field ' + field, (*req, field), DROP)
        for field in ('requirement_id', 'spec_ref'):
            for value in (True, None, [], 'FR-01', 'FR-001\n'):
                case(field + repr(value), (*req, field), value)
        for value in (None, {}, True, [True], ['T01'], ['T001', 'T001']):
            case('task IDs ' + repr(value), (*req, 'task_ids'), value, '/requirements/0/task_ids')
        for value in (None, {}, True, [], 'pass'):
            case('checks ' + repr(value), (*req, 'checks'), value)
        case('both checks and human', (*req, 'mode'), 'human', '/requirements/0')
        case('neither checks nor human', (*req, 'checks'), DROP, '/requirements/0')
        case('nonobject check', check, [])
        for field in ('verifier_id', 'expect'):
            case('missing check field ' + field, (*check, field), DROP)
        for value in (None, False, '', ' \t'):
            case('verifier ID ' + repr(value), (*check, 'verifier_id'), value)
        for value in (None, [], False, 'pass'):
            case('nonobject expect ' + repr(value), expect, value)
        for value in (None, False, [], 'unknown', 'human'):
            case('kind ' + repr(value), (*expect, 'kind'), value)
        case('missing kind', (*expect, 'kind'), DROP)
        case('test cannot carry artifact fields', expect, {'kind': 'pass', 'path': 'out/file'},
             '/requirements/0/checks/0/expect/path')
        case('artifact cannot carry regression fields', expect, {'kind': 'present', 'path': 'out/file',
             'base': 'a' * 40}, '/requirements/0/checks/0/expect/base')
        for kind in ('present', 'nonempty', 'digest'):
            for path in ('/file', 'C:/file', 'C:file', '../file', 'a/../file', 'a\\file',
                         'a\x00b', 'a\nb', 'a\x1fb', 'a\x7fb', 'a\x85b', '', '.', './', True):
                value = {'kind': kind, 'path': path}
                if kind == 'digest':
                    value['sha256'] = 'a' * 64
                case('unsafe ' + kind + repr(path), expect, value, '/requirements/0/checks/0/expect/path')
            case('missing artifact path ' + kind, expect, {'kind': kind},
                 '/requirements/0/checks/0/expect/path')
        for field, length in (('base', 40), ('commit', 40), ('sha256', 64)):
            for value in ('a' * (length - 1), 'a' * (length + 1), 'g' * length, True, None):
                expectation = copy.deepcopy(REGRESSION)
                node = expectation if field == 'base' else expectation['fixture']
                node[field] = value
                suffix = field if field == 'base' else 'fixture/' + field
                case('regression hash ' + field + repr(value), expect, expectation,
                     '/requirements/0/checks/0/expect/' + suffix)
        for value in ('a' * 63, 'a' * 65, 'g' * 64, True, None):
            case('digest hash ' + repr(value), expect, {'kind': 'digest', 'path': 'out/file', 'sha256': value},
                 '/requirements/0/checks/0/expect/sha256')
        for field in ('base', 'fixture'):
            value = copy.deepcopy(REGRESSION)
            del value[field]
            case('missing regression ' + field, expect, value, '/requirements/0/checks/0/expect/' + field)
        for field in ('commit', 'sha256'):
            value = copy.deepcopy(REGRESSION)
            del value['fixture'][field]
            case('missing fixture ' + field, expect, value, '/requirements/0/checks/0/expect/fixture/' + field)
        for value in (None, [], False):
            case('fixture type ' + repr(value), expect, {**REGRESSION, 'fixture': value},
                 '/requirements/0/checks/0/expect/fixture')
        for mode in (False, None, [], 'automatic'):
            value = changed((*req, 'checks'), DROP)
            value['requirements'][0]['mode'] = mode
            cases.append(('mode ' + repr(mode), value, '/requirements/0/mode'))
        duplicate = contract()
        duplicate['requirements'].append(copy.deepcopy(duplicate['requirements'][0]))
        cases.append(('duplicate requirement', duplicate, '/requirements/1/requirement_id'))
        case('retired ID in use', ('retired_requirement_ids',), ['FR-001'], '/requirements/0/requirement_id')
        duplicate = contract()
        duplicate['requirements'][0]['checks'].append({'expect': {'kind': 'pass'}, 'verifier_id': 'sample-tests'})
        cases.append(('duplicate check', duplicate, '/requirements/0/checks/1'))
        cases.append(('nonobject root', [], ''))
        for name, value, location in cases:
            with self.subTest(name=name):
                errors = self.errors(value)
                self.assertTrue(errors)
                self.assertTrue(any(error['location'] == location or error['location'].startswith(location + '/')
                                    for error in errors), (location, errors))

    def test_duplicate_json_keys(self):
        valid = json.dumps(contract())
        cases = (
            (valid.replace('"schema_version": 1', '"schema_version": 1, "schema_version": 1'), '/schema_version'),
            (valid.replace('"task_ids":', '"task_ids": [], "task_ids":'), '/requirements/0/task_ids'),
            (valid.replace('"verifier_id":', '"verifier_id": "first", "verifier_id":'),
             '/requirements/0/checks/0/verifier_id'),
            (valid.replace('"kind": "pass"', '"kind": "pass", "k\\u0069nd": "pass"'),
             '/requirements/0/checks/0/expect/kind'),
            (valid.replace('"kind": "pass"', '"kind": "pass", "~/": 1, "~/": 2'),
             '/requirements/0/checks/0/expect/~0~1'),
        )
        regression = changed(('requirements', 0, 'checks', 0, 'expect'), REGRESSION)
        raw = json.dumps(regression).replace('"commit":', '"commit": "' + 'a' * 40 + '", "commit":')
        for raw, location in (*cases, (raw, '/requirements/0/checks/0/expect/fixture/commit')):
            with self.subTest(location=location):
                self.write_inputs(raw=raw.encode('utf-8'))
                self.assertIn({'location': location, 'message': 'duplicate JSON key'}, self.cli(code=22)['errors'])

    def test_spec_kit_declarations(self):
        validator = MODULE['Validator']()
        spec = '''# Declarations
  - **FR-001**: First.
* **SC-1000** : Second.
- **FR-01**: Too short.
- FR-002: Missing bold.
FR-003 in prose.
```markdown
- **FR-001**: Ignored duplicate.
- **FR-004**: Ignored.
~~~
```
~~~~ example
- **SC-001**: Ignored.
~~~
- **SC-002**: Still ignored.
~~~~
- **SC-003**: Visible.
````
```
- **FR-005**: Still fenced.
````
'''
        tasks = '''- [ ] T001 Create.
  - [x] T003 [P] Configure.
- [X] T1000 Complete.
* [ ] T002 Wrong bullet.
- [] T004 Missing space.
- [ ] T01 Short.
- [ ] T005suffix Not a word boundary.
T006 in prose.
```text
- [ ] T001 Ignored duplicate.
```
~~~
- [ ] T007 Ignored.
~~~
'''
        self.assertEqual(set(validator.declarations(spec, MODULE['SPEC_DECLARATION'], '/spec')),
                         {'FR-001', 'SC-1000', 'SC-003'})
        self.assertEqual(set(validator.declarations(tasks, MODULE['TASK_DECLARATION'], '/tasks')),
                         {'T001', 'T003', 'T1000'})
        self.assertEqual(validator.errors, [])

    def test_duplicate_declarations(self):
        for spec, tasks, location in ((SPEC + SPEC, TASKS, '/spec/lines/2'),
                                     (SPEC, TASKS + '- [X] T001 Duplicate.\n', '/tasks/lines/2')):
            with self.subTest(location=location):
                self.write_inputs(spec=spec, tasks=tasks)
                self.assertIn(location, [error['location'] for error in self.cli(code=22)['errors']])

    def test_broken_references(self):
        for value, spec, tasks, location in (
                (changed(('requirements', 0, 'spec_ref'), 'FR-999'), SPEC, TASKS, '/requirements/0/spec_ref'),
                (changed(('requirements', 0, 'task_ids'), ['T999']), SPEC, TASKS, '/requirements/0/task_ids/0'),
                (contract(), SPEC + '- **SC-001**: Unmapped.\n', TASKS, '/requirements'),
                (contract(), 'FR-001 is merely mentioned.\n', TASKS, '/requirements/0/spec_ref'),
                (contract(), SPEC, 'T001 is merely mentioned.\n', '/requirements/0/task_ids/0'),
                (changed(('requirements', 0, 'spec_ref'), 'SC-001'),
                 SPEC + '- **SC-001**: Different ID.\n', TASKS, '/requirements/0/spec_ref')):
            with self.subTest(location=location, spec=spec, tasks=tasks):
                self.write_inputs(value, spec=spec, tasks=tasks)
                self.assertIn(location, [error['location'] for error in self.cli(code=22)['errors']])
        value = contract()
        value['retired_requirement_ids'] = ['SC-001']
        self.write_inputs(value, spec=SPEC + '- **SC-001**: Retired.\n')
        self.cli()

    def test_examples(self):
        examples = sorted(EXAMPLES.glob('*.json'))
        self.assertEqual({path.name for path in examples}, {'complete.json', 'human.json'})
        for source in examples:
            with self.subTest(example=source.name):
                for name, fixture in (('contract', source), ('spec', EXAMPLES / 'spec.md'),
                                      ('tasks', EXAMPLES / 'tasks.md')):
                    shutil.copyfile(fixture, self.paths[name])
                self.assertEqual(self.cli()['errors'], [])

    def test_contract_size_boundary(self):
        value = contract()
        value['contract_id'] = 'synthetic-\u00e9'
        raw = json.dumps(value, ensure_ascii=False).encode('utf-8')
        limit = MODULE['MAX_BYTES']
        for size in (limit - 1, limit, limit + 1):
            with self.subTest(bytes=size):
                self.write_inputs(raw=raw + b' ' * (size - len(raw)))
                self.assertEqual(self.paths['contract'].stat().st_size, size)
                report = self.cli(code=0 if size <= limit else 22)
                if size > limit:
                    self.assertIn('exceeds 256 KiB', report['errors'][0]['message'])
        self.write_inputs(raw=b'!' * (limit + 1))
        self.assertEqual(self.cli(code=22)['errors'], [{'location': '', 'message': 'contract exceeds 256 KiB'}])

    def test_requirement_limit_boundary(self):
        limit = MODULE['MAX_REQUIREMENTS']
        for count in (limit - 1, limit, limit + 1):
            with self.subTest(requirements=count):
                ids = ['FR-' + str(i).zfill(3) for i in range(1, count + 1)]
                value = contract()
                value['requirements'] = [{'requirement_id': rid, 'spec_ref': rid, 'task_ids': [], 'mode': 'human'}
                                         for rid in ids]
                self.write_inputs(value, spec=''.join('- **' + rid + '**: Sample.\n' for rid in ids))
                report = self.cli(code=0 if count <= limit else 22)
                if count > limit:
                    self.assertTrue(any('128 requirements' in e['message'] for e in report['errors']))

    def test_check_limit_boundary(self):
        limit = MODULE['MAX_CHECKS']
        for count, split in ((limit - 1, False), (limit, False), (limit, True), (limit + 1, True)):
            with self.subTest(checks=count, split=split):
                value = contract()
                checks = [{'verifier_id': 'synthetic-check-' + str(i), 'expect': {'kind': 'pass'}}
                          for i in range(count)]
                value['requirements'][0]['checks'] = checks[:count // 2] if split else checks
                spec = SPEC
                if split:
                    value['requirements'].append({'requirement_id': 'SC-001', 'spec_ref': 'SC-001',
                                                  'task_ids': [], 'checks': checks[count // 2:]})
                    spec += '- **SC-001**: Sample.\n'
                self.write_inputs(value, spec=spec)
                report = self.cli(code=0 if count <= limit else 22)
                if count > limit:
                    self.assertTrue(any('256 checks' in e['message'] for e in report['errors']))

    def test_symlinked_inputs(self):
        for name in self.paths:
            with self.subTest(input=name):
                link = self.directory / (name + '-link')
                link.symlink_to(self.paths[name])
                self.cli(paths={**self.paths, name: link}, code=2)
        parent = self.directory / 'linked-parent'
        parent.symlink_to(self.directory, target_is_directory=True)
        self.cli(paths={name: parent / path.name for name, path in self.paths.items()}, code=0)
        dangling = self.directory / 'dangling'
        dangling.symlink_to(self.directory / 'absent')
        self.cli(paths={**self.paths, 'contract': dangling}, code=2)

    def test_nonregular_and_unreadable_inputs(self):
        missing = self.directory / 'missing'
        directory = self.directory / 'directory'
        directory.mkdir()
        fifo = self.directory / 'fifo'
        os.mkfifo(fifo)
        for name in self.paths:
            for path in (missing, directory, fifo):
                with self.subTest(input=name, kind=path.name):
                    report = self.cli(paths={**self.paths, name: path}, code=2)
                    self.assertEqual(report['errors'][0]['location'], '' if name == 'contract' else '/' + name)
        for name in ('spec', 'tasks'):
            self.write_inputs()
            self.paths[name].write_bytes(b'\xff')
            self.cli(code=2)

    def test_stable_error_order(self):
        value = contract()
        value.update({'schema_version': True, 'revision': 0, 'bad~/field': 'unknown'})
        value['requirements'][0]['task_ids'] = [False, 'T999']
        value['requirements'][0]['checks'][0]['expect'] = {'kind': 'unknown', 'z': 1, 'a': 2}
        self.write_inputs(value, spec=SPEC + '- **SC-001**: Unmapped.\n')
        first = self.cli(code=22)
        self.assertIn('/bad~0~1field', [error['location'] for error in first['errors']])
        self.assertIn('/requirements/0/task_ids/1', [error['location'] for error in first['errors']])
        self.assertEqual(first['errors'], sorted(first['errors'], key=lambda e: (e['location'], e['message'])))

        def reverse(item):
            if isinstance(item, dict):
                return {key: reverse(child) for key, child in reversed(list(item.items()))}
            return [reverse(child) for child in item] if isinstance(item, list) else item

        self.write_inputs(reverse(value), spec=SPEC + '- **SC-001**: Unmapped.\n')
        self.assertEqual(self.cli(code=22), first)

    def test_cli_exit_codes(self):
        self.cli(code=0)
        self.write_inputs(raw=b'{')
        self.cli(code=22)
        self.write_inputs(tasks='')
        self.cli(code=22)
        for arguments in ([], ['validate'], ['unknown'], ['--help'],
                          ['validate', str(self.paths['contract']), '--spec', str(self.paths['spec'])],
                          ['validate', str(self.paths['contract']), '--spec', str(self.paths['spec']),
                           '--tasks', str(self.paths['tasks']), '--unknown']):
            with self.subTest(arguments=arguments):
                self.cli(arguments=arguments, code=2)
        self.paths['spec'].unlink()
        self.cli(code=2)
        self.write_inputs(raw=b'!' * (MODULE['MAX_BYTES'] + 1))
        self.paths['tasks'].unlink()
        self.cli(code=2)

    def test_malformed_json(self):
        for raw in (b'', b'{', b'{} trailing', b'\xff', b'{"schema_version":NaN}',
                    b'{"schema_version":Infinity}', b'{"schema_version":-Infinity}',
                    b'[' * 1200 + b'0' + b']' * 1200):
            with self.subTest(raw=raw[:40]):
                self.write_inputs(raw=raw)
                self.assertTrue(any('malformed contract' in e['message'] for e in self.cli(code=22)['errors']))

    def test_empty_mapping_and_retirement(self):
        value = contract()
        value['requirements'] = []
        self.write_inputs(value, spec='No requirements declared.\n', tasks='No tasks declared.\n')
        self.cli()
        value['retired_requirement_ids'] = ['FR-001']
        self.write_inputs(value)
        self.cli()

    def test_no_stage_integration(self):
        mentions = []
        for directory in (ROOT / 'scripts', ROOT / 'templates/scripts'):
            for path in directory.rglob('*'):
                if path.is_file() and path != SCRIPT and b'bureau-acceptance-contract' in path.read_bytes():
                    mentions.append(str(path.relative_to(ROOT)))
        self.assertEqual(mentions, [], 'standalone validator must have no script callers or imports')

    def test_shard_registration_and_limits(self):
        weights = {}
        for line in (ROOT / 'tests/shard-weights.tsv').read_text().splitlines():
            if not line.strip() or line.startswith('#'):
                continue
            name, linux, macos = line.split()
            self.assertNotIn(name, weights)
            weights[name] = tuple(None if cell == '-' else int(cell) for cell in (linux, macos))
        self.assertIn('test_acceptance_contract.sh', weights)
        for cell in weights['test_acceptance_contract.sh']:
            self.assertTrue(cell is None or cell > 0)
        files = sorted(path.name for path in (ROOT / 'tests').glob('test_*.sh'))
        files.append('test_unmeasured_future.sh')
        for platform, total in (('Linux', 4), ('macOS', 7)):
            costs = {}
            for name in files:
                linux, macos = weights.get(name, (None, None))
                linux = linux if linux is not None else (math.ceil(macos / 2) if macos is not None else 60)
                costs[name] = linux if platform == 'Linux' else (macos if macos is not None else 2 * linux)
            loads = [0] * total
            for name in sorted(files, key=lambda item: (-costs[item], item)):
                index = min(range(total), key=lambda i: (loads[i], i))
                loads[index] += costs[name]
            self.assertLessEqual(max(loads), 1.1 * sum(loads) / total, (platform, loads))
            self.assertLess(max(loads), 3600, (platform, loads))
        manifest = (ROOT / 'tests/macos-manifest.txt').read_text()
        self.assertNotIn('test_acceptance_contract.sh', manifest)
        self.assertTrue((ROOT / 'tests/test_acceptance_contract.sh').is_file())
        self.assertRegex((ROOT / 'tests/test_acceptance_contract.sh').read_text(),
                         r'PYTHONDONTWRITEBYTECODE=1 python3 .*acceptance_contract_test\.py')


if __name__ == '__main__':
    unittest.main()
