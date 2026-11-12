"""Static integrity fixtures use real Git; only the QA integration fakes services."""
import copy
from contextlib import redirect_stdout
import importlib.util
import io
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from unittest.mock import patch

import git_maintenance
import qa_changed_tests_test as changed

ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / 'templates/scripts/bureau-test-integrity.py'
spec = importlib.util.spec_from_file_location('test_integrity', HELPER)
c = importlib.util.module_from_spec(spec); spec.loader.exec_module(c)
a, e = c.a, c.e


class IntegrityTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='.qa-integrity-fixture-', dir=ROOT)
        self.addCleanup(temp.cleanup); self.root = Path(temp.name)
        self.repo = self.root / 'repo'; self.repo.mkdir()
        self.env = {**os.environ, **git_maintenance.OFF_ENV, 'TMPDIR':str(self.root),
                    'GIT_CONFIG_GLOBAL':os.devnull, 'GIT_CONFIG_NOSYSTEM':'1', 'PYTHONDONTWRITEBYTECODE':'1'}
        for key in (*c.GIT_CONTEXT, 'BUREAU_CONFIG', 'BUREAU_RUN_ID', 'BUREAU_STAGE_ATTEMPT_ID'):
            self.env.pop(key, None)
        self.git('init', '-q', '-b', 'main'); git_maintenance.disable(self.repo)
        self.git('config', 'user.email', 'fixture@example.invalid'); self.git('config', 'user.name', 'Fixture')
        self.write('pkg/__init__.py', 'VALUE = 1\n')
        self.write('src/owned/__init__.py', '')
        self.config = dict(repo=dict(test_integrity_check=True))
        self.base_files({})
        self.store = e.Store(self.repo / '.git/bureau'); self.pr = 0

    def git(self, *args):
        return subprocess.check_output(['git', '-C', str(self.repo), *args], env=self.env,
                                       stderr=subprocess.DEVNULL, text=True, timeout=10).strip()

    def write(self, path, source):
        target = self.repo / path; target.parent.mkdir(parents=True, exist_ok=True); target.write_text(source)

    def commit(self, files):
        for path, source in files.items():
            if source is None: (self.repo / path).unlink()
            else: self.write(path, source)
        self.git('add', '--all'); self.git('commit', '-qm', 'test: fixture change')
        return self.git('rev-parse', 'HEAD')

    def base_files(self, files):
        self.base = self.commit({**files, '.bureau.json':json.dumps(self.config)})

    def publish(self, files):
        self.head = self.commit(files)

    def begin(self):
        self.run = uuid.uuid4().hex; self.pr += 1
        self.store.begin_run(self.run, None, self.repo, self.head)
        self.attempt = self.store.begin_attempt(self.run, None, 'qa', 'stage', self.repo, self.head)
        self.store.set_pr(self.run, self.attempt, self.pr)
        self.env.update(BUREAU_RUN_ID=self.run, BUREAU_STAGE_ATTEMPT_ID=self.attempt)

    def brief(self):
        with patch.dict(os.environ, self.env, clear=True):
            return a.render(self.store, self.repo, None, self.pr, self.head,
                            'https://example.invalid/repo/pull/' + str(self.pr), base=self.base)

    def state(self):
        files = {str(path.relative_to(self.repo)):path.read_bytes() for path in self.repo.rglob('*')
                 if path.is_file() and '.git' not in path.relative_to(self.repo).parts}
        return (files, (self.repo / '.git/index').read_bytes(), (self.repo / '.git/HEAD').read_bytes(),
                self.git('rev-parse', 'HEAD'), self.git('worktree', 'list', '--porcelain'))

    def scan(self, expected, counts, direct=False, **kwargs):
        self.begin(); before = self.state()
        if direct:
            with patch.dict(os.environ, self.env, clear=True):
                record = c.measure(self.repo, self.base, self.config, head=self.head, **kwargs)
            self.store.test_integrity(self.run, self.attempt, record)
        else:
            result = subprocess.run([sys.executable, '-I', str(HELPER), '--repo', str(self.repo), '--base', self.base],
                                    cwd=self.repo, env=self.env, capture_output=True, text=True, timeout=70)
            self.assertEqual(0, result.returncode, result.stdout + result.stderr)
            self.output = result.stdout + result.stderr
        self.assertEqual(before, self.state(), 'measurement changed the checkout, index, HEAD or worktrees')
        record = self.store._manifest(self.run)['attempts'][0].get('test_integrity')
        self.assertIsNotNone(record, getattr(self, 'output', '')); e.test_integrity_shape(record)
        self.assertEqual((self.base, self.head, expected), (record['base'], record['head'], record['verdict']))
        actual = (record['checks']['assertions']['removed'], record['checks']['skips']['added'],
                  record['checks']['imports']['unresolved'], record['unverifiable_files'])
        self.assertEqual(counts, actual, record)
        brief = self.brief()
        self.assertIn(f'- {expected}; removed assertions={counts[0]}; new skips={counts[1]}; '
                      f'unresolved imports={counts[2]}; unverifiable files={counts[3]}; omitted={record["omitted"]}.', brief)
        self.assertLess(brief.index('**Test evidence**'), brief.index('**Test integrity**'))
        self.assertLess(brief.index('**Test integrity**'), brief.index('**Review evidence**'))
        return record, brief

    def test_removed_assertions_in_python_javascript_and_shell(self):
        cases = [('tests/test_value.py', 'assert True\nassert 1 == 1\n', 'assert True\n'),
                 ('tests/value.test.js', 'expect(true).toBe(true);\nassert.ok(1);\n', 'expect(true).toBe(true);\n'),
                 ('tests/value.sh', '    assert_eq 1 1\n    true || exit 1\n', '    assert_eq 1 1\n')]
        for path, before, after in cases:
            with self.subTest(path=path):
                self.base_files({path:before}); self.publish({path:after})
                record, brief = self.scan('flagged', (1, 0, 0, 0))
                kind = 'assertions removed' if path.endswith('.py') else 'assertion-like sites went down'
                self.assertEqual([dict(check='assertions', path=path, line=None, kind=kind,
                                       base_count=2, head_count=1)], record['findings'])
                self.assertIn(f'`{path}`: {kind}', brief)

    def test_deleted_python_test(self):
        path = 'tests/test_deleted.py'; self.base_files({path:'assert True\n'})
        self.publish({path:None})
        record, brief = self.scan('flagged', (1, 0, 0, 0))
        self.assertEqual('test file deleted', record['findings'][0]['kind'])
        self.assertIsNone(record['findings'][0]['line'])
        self.assertIn('`tests/test_deleted.py`: test file deleted', brief)

    def test_javascript_syntax_preserves_assertion_sites(self):
        cases = [r'const url = /https?:\/\//; ',
                 'const text = `https://${flag ? `nested ${value}` : "//"}`; ',
                 'const text = "// \\\" /*"; ',
                 'const result = object.return / 2; ',
                 'const text = `${(() => { expect(value).toBe(1); return `//${value}`; })()}`;']
        path = 'tests/literal.test.ts'
        for prefix in cases:
            with self.subTest(prefix=prefix):
                before = 'expect(value).toBe(1);\n'
                after = prefix + ('' if prefix == cases[-1] else before)
                self.base_files({path:before}); self.publish({path:after})
                self.assertEqual(1, c.analyse(after.encode(), 'javascript')[0])
                record, brief = self.scan('pass', (0, 0, 0, 0))
                self.assertEqual([], record['findings']); self.assertNotIn('assertions removed', brief)

    def test_shell_hashes_and_heredocs_are_counted_lexically(self):
        prefixes = [('n=${#value}; ', 1), ('n=$#; ', 1), ('word=value#suffix; ', 1),
                    ("word='# literal'; ", 1), ('word="# literal"; ', 1), ('word=value\\ #suffix; ', 1),
                    ("word=$'escaped\\\' # literal'; ", 1),
                    ("cat <<'END'\nassert_not_a_command\nEND\n", 2),
                    ('cat <<-END\n\tassert_not_a_command\n\tEND\n', 2),
                    ("cat <<ONE <<'TWO'\nassert_not_a_command\nONE\nassert_not_a_command\nTWO\n", 3)]
        path = 'tests/literal.sh'
        for prefix, count in prefixes:
            with self.subTest(prefix=prefix):
                before = prefix + 'assert_eq "$n" 3\n'; after = prefix + 'assert_eq "$n" 4\n'
                self.base_files({path:before}); self.publish({path:after})
                self.assertEqual(count, c.analyse(after.encode(), 'shell')[0])
                record, brief = self.scan('pass', (0, 0, 0, 0))
                self.assertEqual([], record['findings']); self.assertNotIn('assertion-like sites went down', brief)

    def test_multiple_sites_per_line_and_line_join_control(self):
        cases = [('tests/join.test.js', 'expect(x)', 'assert.ok(y)'),
                 ('tests/join_test.go', 't.Error("x")', 'require.Equal(t, 1, 1)'),
                 ('tests/join.rs', 'assert!(x)', 'panic!("y")'),
                 ('tests/join.sh', 'assert_eq "$x" 1', 'assert_eq "$y" 2'),
                 ('tests/group.sh', 'true || { echo FAIL; }', 'false || { echo FAIL; }')]
        for path, first, second in cases:
            with self.subTest(path=path):
                self.base_files({path:first + '\n' + second + '\n'})
                self.publish({path:first + '; ' + second + '\n'})
                record, brief = self.scan('pass', (0, 0, 0, 0))
                self.assertEqual([], record['findings']); self.assertNotIn('assertion-like sites went down', brief)
                self.base = self.head
                self.publish({path:first + '\n'})
                record, brief = self.scan('flagged', (1, 0, 0, 0))
                self.assertEqual((2, 1), (record['findings'][0]['base_count'], record['findings'][0]['head_count']))
                self.assertIn(f'`{path}`: assertion-like sites went down', brief)

    def test_whole_line_comments_do_not_count(self):
        comments = '// expect(x)\n # assert_eq 1 1\n /* assert.ok(x)\n * assert!(x)\n'
        cases = [('tests/comment.test.js', 'expect(x)\n'), ('tests/comment.sh', 'assert_eq 1 1\n')]
        for path, assertion in cases:
            with self.subTest(path=path):
                self.base_files({path:assertion}); self.publish({path:comments + assertion})
                record, brief = self.scan('pass', (0, 0, 0, 0))
                self.assertEqual([], record['findings']); self.assertNotIn('assertion-like sites went down', brief)

    def test_shell_conditional_assertion_removal_and_control(self):
        path = 'tests/conditional.sh'
        before = 'if assert_eq "$x" 1; then :; else exit 1; fi\n'
        self.base_files({path:before}); self.publish({path:':\n'})
        record, brief = self.scan('flagged', (1, 0, 0, 0))
        self.assertEqual((1, 0), (record['findings'][0]['base_count'], record['findings'][0]['head_count']))
        self.assertIn('`tests/conditional.sh`: assertion-like sites went down', brief)
        self.publish({path:before.replace('"$x" 1', '"$x" 2')})
        record, brief = self.scan('pass', (0, 0, 0, 0))
        self.assertEqual([], record['findings']); self.assertNotIn('assertion-like sites went down', brief)

    def test_shell_command_starts(self):
        starts = ['', 'if ', 'elif ', 'while ', 'until ', '! ', 'then ', 'else ', 'do ',
                  '{ ', '( ', 'true && ', 'false || ', ':; ', 'printf value | ', 'if ! ', 'command ', 'builtin ']
        before = '\n'.join(start + 'assert_eq "$x" 1' for start in starts) + '\n'
        path = 'tests/commands.sh'
        self.base_files({path:before}); self.publish({path:':\n'})
        record, brief = self.scan('flagged', (len(starts), 0, 0, 0))
        self.assertEqual(len(starts), record['findings'][0]['base_count'])
        self.assertIn('`tests/commands.sh`: assertion-like sites went down', brief)
        self.assertEqual(0, c.analyse(b'$assert_eq .assert_eq -assert_eq wordassert_eq failure\n', 'shell')[0])

    def test_literal_and_inline_comment_removal_is_a_reviewer_pointer(self):
        cases = [('tests/literal.test.js', 'const text = "expect(value)";\n', 'const text = "ordinary";\n'),
                 ('tests/template.test.ts', 'const text = `assert.ok(value)`;\n', 'const text = `ordinary`;\n'),
                 ('tests/regex.test.js', 'const pattern = /expect(ing)/;\n', 'const pattern = /ordinary/;\n'),
                 ('tests/comment.test.js', 'const value = 1; /* expect(value) */\n', 'const value = 1;\n'),
                 ('tests/literal_test.go', 'const text = `t.Fatal("literal")`\n', 'const text = `ordinary`\n'),
                 ('tests/literal.rs', 'let text = r#"assert!(false)"#;\n', 'let text = r#"ordinary"#;\n'),
                 ('tests/literal.sh', "printf '%s\\n' 'true || { echo FAIL; }'\n", "printf '%s\\n' 'ordinary'\n")]
        for path, before, after in cases:
            with self.subTest(path=path):
                self.base_files({path:before}); self.publish({path:after})
                record, brief = self.scan('flagged', (1, 0, 0, 0))
                self.assertEqual((1, 0), (record['findings'][0]['base_count'], record['findings'][0]['head_count']))
                self.assertIn(f'`{path}`: assertion-like sites went down', brief)

    def test_literal_text_does_not_mask_real_assertion_loss(self):
        cases = [('tests/loss.test.js', 'const text = "expect(value)";\n', 'expect(value);\n'),
                 ('tests/loss_test.go', 'const text = `t.Error("literal") //`\n', 't.Error("bad")\n'),
                 ('tests/loss.rs', 'let text = br#"assert!(false); //"#;\n', 'assert!(true);\n'),
                 ('tests/loss.sh', 'cat <<END\nassert_not_a_command\nEND\n', 'assert_eq 1 1\n')]
        for path, literal, assertion in cases:
            with self.subTest(path=path):
                self.base_files({path:literal + assertion}); self.publish({path:literal})
                record, brief = self.scan('flagged', (1, 0, 0, 0))
                self.assertEqual((2, 1), (record['findings'][0]['base_count'], record['findings'][0]['head_count']))
                self.assertIn(f'`{path}`: assertion-like sites went down', brief)

    def test_non_python_syntax_does_not_disable_coverage(self):
        cases = [('tests/conditional.sh', '[[ "$x" -eq 1 ]] || exit 1\n', 'assert_eq 1 1\n', 2, 1),
                 ('tests/substitution.sh', 'result="$(printf value)"\n', 'assert_eq 1 1\n', 1, 0),
                 ('tests/division.test.js', 'const result = Math.max(1, 2) / 2;\n', 'expect(result);\n', 1, 0),
                 ('tests/view.test.jsx', 'const view = <p>Hello</p>;\n', 'expect(view);\n', 1, 0),
                 ('tests/unknown_test.go', 'const text = `unterminated\n', 't.Error("bad")\n', 1, 0),
                 ('tests/unknown.rs', '/* outer /* inner */\n', 'assert!(true);\n', 1, 0)]
        for path, source, assertion, base_count, head_count in cases:
            with self.subTest(path=path):
                self.base_files({path:source + assertion}); self.publish({path:source})
                record, brief = self.scan('flagged', (1, 0, 0, 0))
                self.assertIsNone(record['reason'])
                self.assertEqual((base_count, head_count),
                                 (record['findings'][0]['base_count'], record['findings'][0]['head_count']))
                self.assertIn(f'`{path}`: assertion-like sites went down', brief)

    def test_new_skip_and_only_report_the_added_head_lines(self):
        self.base_files({'tests/test_skip.py':'@pytest.mark.skip\ndef test_old(): assert True\n',
                         'tests/skip.test.ts':'test.skip("old", () => {});\n'})
        self.publish({'tests/test_skip.py':'@pytest.mark.skip\ndef test_old(): assert True\n\n@pytest.mark.skip\ndef test_new(): assert True\n',
                      'tests/skip.test.ts':'test.skip("old", () => {});\n// test.only("comment", () => {});\ntest.only("new", () => {});\n'})
        record, brief = self.scan('flagged', (0, 2, 0, 0))
        self.assertEqual([('tests/skip.test.ts', 3), ('tests/test_skip.py', 4)],
                         [(row['path'], row['line']) for row in record['findings']])
        self.assertIn('`tests/skip.test.ts`:3: new skip or disabled test', brief)
        self.assertIn('`tests/test_skip.py`:4: new skip or disabled test', brief)

    def test_go_rust_and_shell_recognisers(self):
        self.base_files({'tests/value_test.go':'t.Error("bad")\nrequire.Equal(t, 1, 1)\n',
                         'tests/value.rs':'assert_eq!(1, 1);\ndebug_assert_ne!(1, 2);\n',
                         'tests/value.bash':'assert_ok true\nfalse || { echo "FAIL"; exit 1; }\n'})
        self.publish({'tests/value_test.go':'t.Error("bad")\nt.SkipNow()\n',
                      'tests/value.rs':'assert_eq!(1, 1);\n#[ignore]\n',
                      'tests/value.bash':'assert_ok true\n# SKIP pending\n\nreturn 0\n'})
        record, brief = self.scan('flagged', (3, 3, 0, 0))
        self.assertTrue(all(record['checks'][check]['status'] == 'flagged' for check in ('assertions', 'skips')))
        self.assertIn('`tests/value.bash`:4: new skip or disabled test', brief)

    def test_skip_marker_names_and_identifier_boundaries(self):
        cases = [('tests/test_markers.py',
                  ('pytest.mark.skip', 'pytest.mark.skipif', 'pytest.mark.xfail', 'pytest.skip',
                   'unittest.skip', 'unittest.skipIf', 'unittest.skipUnless', 'unittest.expectedFailure'),
                  'skip = {name}; skip()', '{name}()'),
                 ('tests/markers.test.js', ('test.skip', 'test.only', 'test.todo', 'xit', 'xdescribe', 'xtest'),
                  'const disabled = {name}; disabled();', '{name}();'),
                 ('tests/markers_test.go', ('t.Skip', 't.Skipf', 't.SkipNow'),
                  'skip := {name}; skip()', '{name}()'),
                 ('tests/markers.rs', ('ignore',), '#[{name}]', '#[{name} = "pending"]')]
        for path, names, reference, call in cases:
            with self.subTest(path=path):
                control = '\n'.join(reference.format(name=name + '_extra') for name in names) + '\n'
                if path.endswith('.js'):
                    control += 'const disabled = test.skip$extra; const other = $xit;\n'
                self.base_files({path:'\n'}); self.publish({path:control})
                record, brief = self.scan('pass', (0, 0, 0, 0))
                self.assertEqual([], record['findings']); self.assertNotIn('new skip or disabled test', brief)
                for template in (reference, call):
                    with self.subTest(template=template):
                        source = '\n'.join(template.format(name=name) for name in names) + '\n'
                        self.base_files({path:control + '\n'}); self.publish({path:source})
                        record, brief = self.scan('flagged', (0, len(names), 0, 0))
                        self.assertEqual([dict(check='skips', path=path, line=line, kind='new skip or disabled test')
                                          for line in range(1, len(names) + 1)], record['findings'])
                        for line in range(1, len(names) + 1):
                            self.assertIn(f'`{path}`:{line}: new skip or disabled test', brief)
                        self.base = self.head; self.publish({path:source + '\n'})
                        record, brief = self.scan('pass', (0, 0, 0, 0))
                        self.assertEqual([], record['findings']); self.assertNotIn('new skip or disabled test', brief)

    def test_assertion_calls_and_method_values_keep_the_token_contract(self):
        cases = [('tests/test_values.py',
                  ('case.assertEqual', 'case.fail', 'pytest.raises', 'pytest.warns', 'pytest.fail')),
                 ('tests/values.test.js', ('expect', 'assert', 'assert.ok')),
                 ('tests/values_test.go',
                  ('t.Error', 't.Errorf', 't.Fatal', 't.Fatalf', 't.Fail', 't.FailNow', 'assert.Equal', 'require.Equal'))]
        for path, names in cases:
            with self.subTest(path=path):
                calls = '\n'.join(name + '()' for name in names) + '\n'
                references = '\n'.join('check = ' + name + '; check()' for name in names) + '\n'
                self.base_files({path:calls}); self.publish({path:references})
                record, brief = self.scan('flagged', (len(names), 0, 0, 0))
                self.assertEqual((len(names), 0), (record['findings'][0]['base_count'], record['findings'][0]['head_count']))
                kind = 'assertions removed' if path.endswith('.py') else 'assertion-like sites went down'
                self.assertIn(f'`{path}`: {kind}', brief)
        self.assertEqual(0, c.analyse(b'let check = assert; let other = panic;\n', 'rust')[0])
        self.assertEqual(2, c.analyse(b'check=assert_ok; other=fail\n', 'shell')[0])

    def test_first_party_missing_imports_and_external_and_guarded_imports(self):
        self.publish({'app.py':'''import pkg.missing
import external_dependency
from pkg import attribute
try:
    import pkg.optional
except ImportError:
    pass
try:
    from pkg.unavailable import value
except (RuntimeError, ModuleNotFoundError):
    pass
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    import pkg.typing_only
''', 'web/app.ts':'''import value from './missing'
export {other} from "./also-missing"
const third = require("./third")
const fourth = import ("./fourth")
import external from 'external_dependency'
''', 'src/app.py':'import owned.missing\n',
                      'pkg/relative.py':'from .missing import value\nfrom . import absent\n'})
        record, brief = self.scan('flagged', (0, 0, 7, 0))
        self.assertEqual(3, record['checks']['imports']['external'])
        self.assertTrue(all(row['check'] == 'imports' for row in record['findings']))
        self.assertNotIn('external_dependency', json.dumps(record)); self.assertNotIn('external_dependency', brief)
        self.assertIn('`app.py`:1: unresolved first-party import', brief)
        self.assertIn('`web/app.ts`:4: unresolved first-party import', brief)

    def test_relative_python_imported_names_are_not_judged(self):
        path = 'pkg/use.py'
        self.write('pkg/nested/__init__.py', 'VALUE = 1\n')
        cases = [('from pkg import VALUE\n', 'from . import VALUE\n'),
                 ('from pkg import *\n', 'from . import *\n'),
                 ('from pkg.nested import VALUE\n', 'from .nested import VALUE\n'),
                 ('from pkg import missing_module\n', 'from . import missing_module\n'),
                 ('from pkg.nested import *\n', 'from .nested import *\n')]
        for before, after in cases:
            with self.subTest(after=after):
                self.base_files({path:before}); self.publish({path:after})
                record, brief = self.scan('pass', (0, 0, 0, 0))
                self.assertEqual([], record['findings']); self.assertNotIn('unresolved first-party import', brief)
        self.publish({path:'from .missing import value\n'})
        record, brief = self.scan('flagged', (0, 0, 1, 0))
        self.assertEqual('pkg/missing', record['findings'][0]['module'])
        self.assertIn('`pkg/use.py`:1: unresolved first-party import', brief)

    def test_relative_python_root_package_and_parent_at_depth_one(self):
        for path, prefix in [('use.py', '.'), ('pkg/use.py', '..')]:
            with self.subTest(path=path):
                package = 'VALUE = 1\nOTHER = 2\n'
                self.base_files({'__init__.py':package, path:f'from {prefix} import VALUE\n'})
                self.publish({path:f'from {prefix} import VALUE, OTHER\n'})
                record, brief = self.scan('pass', (0, 0, 0, 0))
                self.assertEqual([], record['findings']); self.assertNotIn('unresolved first-party import', brief)
                self.publish({'__init__.py':None})
                record, brief = self.scan('flagged', (0, 0, 1, 0))
                self.assertIsNone(record['findings'][0]['module'])
                self.assertIn(f'`{path}`:1: unresolved first-party import', brief)
                self.publish({'__init__.py':package,
                              path:f'from {prefix}. import VALUE\nfrom {prefix}.outside import VALUE\n'})
                record, brief = self.scan('pass', (0, 0, 0, 0))
                self.assertEqual(2, record['checks']['imports']['external'])
                self.assertEqual([], record['findings']); self.assertNotIn('outside', json.dumps(record))
                self.assertNotIn('unresolved first-party import', brief)

    def test_escaped_javascript_specifier_is_not_judged(self):
        path = 'web/app.js'
        self.base_files({'web/existing.js':'export default 1;\n', path:"import value from './existing.js';\n"})
        self.publish({path:"import value from './\\u0065xisting.js';\n"})
        record, brief = self.scan('pass', (0, 0, 0, 0))
        self.assertEqual(1, record['checks']['imports']['external'])
        self.assertEqual([], record['findings']); self.assertNotIn('unresolved first-party import', brief)
        self.publish({path:"import value from './missing.js';\n"})
        record, brief = self.scan('flagged', (0, 0, 1, 0))
        self.assertEqual(0, record['checks']['imports']['external'])
        self.assertEqual('web/missing.js', record['findings'][0]['module'])
        self.assertIn('`web/app.js`:1: unresolved first-party import', brief)

    def test_relative_javascript_repository_root_and_outside_targets(self):
        for path, prefix in [('tests/root.test.js', '../'), ('root.test.js', './')]:
            with self.subTest(path=path):
                self.base_files({'index.js':'module.exports = {};\n', path:f"require('{prefix}index.js');\n"})
                self.publish({path:f"require('{prefix}');\n"})
                record, brief = self.scan('pass', (0, 0, 0, 0))
                self.assertEqual(0, record['checks']['imports']['external'])
                self.assertEqual([], record['findings']); self.assertNotIn('unresolved first-party import', brief)
                self.publish({'index.js':None})
                record, brief = self.scan('flagged', (0, 0, 1, 0))
                self.assertEqual([dict(check='imports', path=path, line=1, kind='unresolved first-party import',
                                       module=None)], record['findings'])
                self.assertIn(f'`{path}`:1: unresolved first-party import', brief)
                self.publish({path:f"require('{prefix}../outside');\nrequire('{prefix}..');\n"})
                record, brief = self.scan('pass', (0, 0, 0, 0))
                self.assertEqual(2, record['checks']['imports']['external'])
                self.assertEqual([], record['findings']); self.assertNotIn('outside', json.dumps(record))
                self.assertNotIn('unresolved first-party import', brief)

    def test_import_resolution_uses_head_files_and_lexical_javascript(self):
        self.publish({'web/app.ts':'''import a from './direct';
export {b} from './folder';
const c = require('./data.json');
const d = import('./explicit.mjs');
// import fake from './missing';
const text = "require('./not-an-import')";
''', 'web/direct.tsx':'export default 1;\n', 'web/folder/index.cjs':'module.exports = {};\n',
                      'web/data.json':'{}\n', 'web/explicit.mjs':'export default 1;\n',
                      'pkg/app.py':'from . import local\nfrom pkg import attribute\nimport pkg.local\n',
                      'pkg/local.py':'VALUE = 1\n', 'src/app.py':'import owned\n'})
        record, brief = self.scan('flagged', (0, 0, 1, 0))
        self.assertEqual([dict(check='imports', path='web/app.ts', line=6, kind='unresolved first-party import',
                               module='web/not-an-import')], record['findings'])
        self.assertIn('`web/app.ts`:6: unresolved first-party import', brief)

    def test_external_and_optional_imports_alone_pass(self):
        self.publish({'pkg/app.py':'''import external_dependency
try:
    import pkg.optional
except ImportError:
    pass
if TYPE_CHECKING:
    import pkg.type_only
from pkg import attribute
''', 'web/app.js':"import external from 'external_dependency';\n"})
        record, brief = self.scan('pass', (0, 0, 0, 0))
        self.assertEqual(2, record['checks']['imports']['external'])
        self.assertEqual('pass', record['checks']['imports']['status'])
        self.assertNotIn('external_dependency', json.dumps(record)); self.assertNotIn('external_dependency', brief)

    def test_outside_and_escaped_module_names_are_not_retained(self):
        self.publish({'app.js':"require('../outside');\nimport value from './bad\\nname';\n"})
        record, brief = self.scan('pass', (0, 0, 0, 0))
        self.assertEqual(2, record['checks']['imports']['external'])
        self.assertEqual([], record['findings'])
        self.assertNotIn('outside', json.dumps(record)); self.assertNotIn('outside', brief.split('**Test integrity**')[1])

    def test_clean_split_rename_and_rewrite(self):
        self.base_files({'tests/test_split.py':'assert 1\nassert 2\n',
                         'tests/test_old.py':'assert True\n',
                         'tests/rewrite.test.js':'expect(1).toBe(1);\nassert.ok(true);\n'})
        (self.repo / 'tests/test_old.py').rename(self.repo / 'tests/test_renamed.py')
        self.publish({'tests/test_split.py':'assert 1\n', 'tests/test_split_second.py':'assert 2\n',
                      'tests/rewrite.test.js':'assert(1);\nvalue.should.equal(1);\n'})
        self.assertIn('R100', self.git('diff', '--name-status', '--find-renames', self.base, self.head, '--'))
        record, brief = self.scan('pass', (0, 0, 0, 0))
        self.assertEqual([], record['findings']); self.assertNotIn('assertions removed', brief)

    def test_lexical_assertion_splits_preserve_family_totals(self):
        cases = [('tests/split.test.js', 'tests/second.test.js', 'expect(x);\n', 'assert.ok(y);\n'),
                 ('tests/split_test.go', 'tests/second_test.go', 't.Error("x")\n', 'require.Equal(t, 1, 1)\n'),
                 ('tests/split.rs', 'tests/second.rs', 'assert!(x);\n', 'panic!("y");\n'),
                 ('tests/split.sh', 'tests/second.sh', 'assert_eq "$x" 1\n', 'assert_eq "$y" 2\n')]
        for path, second_path, first, second in cases:
            with self.subTest(path=path):
                self.base_files({path:first + second}); self.publish({path:first, second_path:second})
                record, brief = self.scan('pass', (0, 0, 0, 0))
                self.assertEqual([], record['findings']); self.assertNotIn('assertion-like sites went down', brief)

    def test_unsupported_test_and_unparseable_python_are_unverifiable(self):
        self.publish({'tests/value.custom':'check value\n', 'tests/test_broken.py':'def broken(:\n'})
        record, brief = self.scan('unverifiable', (0, 0, 0, 2))
        self.assertEqual('Python parse error', record['reason'])
        self.assertEqual(['Python parse error', 'no recogniser for this file type'],
                         [row['kind'] for row in record['findings']])
        self.assertIn('Python parse error', brief); self.assertIn('no recogniser for this file type', brief)

    def test_invalid_base_python_cannot_be_reported_as_pass(self):
        self.base_files({'tests/test_broken.py':'def broken(:\n'})
        self.publish({'tests/test_broken.py':'assert True\n'})
        record, brief = self.scan('unverifiable', (0, 0, 0, 1))
        self.assertEqual('pass', record['checks']['imports']['status'])
        self.assertIn('Python parse error', brief)

    def test_unparseable_product_does_not_hide_known_test_assertion_loss(self):
        self.base_files({'tests/test_value.py':'assert True\n'})
        self.publish({'tests/test_value.py':'pass\n', 'app.py':'def broken(:\n'})
        record, brief = self.scan('flagged', (1, 0, 0, 1))
        self.assertEqual('flagged', record['checks']['assertions']['status'])
        self.assertEqual('unverifiable', record['checks']['imports']['status'])
        self.assertIn('assertions removed', brief); self.assertIn('Python parse error', brief)

    def test_hostile_names_remain_data_and_control_characters_are_withheld(self):
        sentinel = self.root / 'unexpected'
        self.env['INTEGRITY_SENTINEL'] = str(sentinel)
        paths = ['tests/test_quote\'.py', 'tests/test_$(touch "$INTEGRITY_SENTINEL").py',
                 '-leading.test.py', 'tests/test_new\nline.py']
        source = ('from pathlib import Path\nPath(' + repr(str(sentinel)) + ').touch()\n'
                  '@pytest.mark.skip\ndef test_value(): assert True\n')
        self.publish({path:source for path in paths})
        record, brief = self.scan('flagged', (0, 3, 0, 1))
        self.assertFalse(sentinel.exists())
        self.assertEqual(1, record['omitted']); self.assertEqual('unsafe path', record['reason'])
        self.assertEqual(3, len(record['findings']))
        self.assertNotIn('test_new\nline.py', json.dumps(record))
        self.assertIn('test_quote%27.py`:3:', brief)
        self.assertIn('%24%28touch%20%22%24INTEGRITY_SENTINEL%22%29', brief)

    def test_more_than_two_hundred_changed_files_are_unverifiable(self):
        self.publish({f'files/file-{number:03d}.txt':'content\n' for number in range(201)})
        record, brief = self.scan('unverifiable', (0, 0, 0, 201))
        self.assertEqual('changed file limit exceeded', record['reason'])
        self.assertEqual([], record['findings']); self.assertNotIn('- pass;', brief)

    def test_oversize_file_is_unverifiable_and_never_read(self):
        self.publish({'tests/test_large.py':'#' + 'x' * c.MAX_FILE_BYTES})
        real = c.Git.__call__; shows = []
        def tracked(git, *args):
            if args[0] == 'show': shows.append(args)
            return real(git, *args)
        with patch.object(c.Git, '__call__', tracked):
            record, brief = self.scan('unverifiable', (0, 0, 0, 1), direct=True)
        self.assertEqual([], shows)
        self.assertEqual('file size limit exceeded', record['reason']); self.assertIn('file size limit exceeded', brief)

    def test_budget_is_shared_by_git_and_static_analysis(self):
        self.publish({'tests/test_budget.py':'assert True\n'})
        original = c.Git.prepare; timeouts = []
        def tracked(git, command):
            timeouts.append(git.deadline - time.monotonic())
            return original(git, command)
        def expire(*args):
            handler = signal.getsignal(signal.SIGALRM)
            self.assertTrue(callable(handler))
            self.assertTrue(0 < signal.getitimer(signal.ITIMER_REAL)[0] <= 60)
            handler(signal.SIGALRM, None)
        with patch.object(c.Git, 'prepare', tracked), patch.object(c, 'analyse', expire):
            record, brief = self.scan('unverifiable', (0, 0, 0, 1), direct=True)
        self.assertEqual('budget exhausted', record['reason'])
        self.assertTrue(timeouts); self.assertTrue(all(0 < timeout <= 60 for timeout in timeouts))
        self.assertNotIn('- pass;', brief)

    def test_cancellation_after_an_unverifiable_file_keeps_exit_130(self):
        self.publish({'tests/test_a.custom':'unrecognised\n', 'tests/test_b.py':'assert True\n'})
        self.begin()
        def interrupt(*args):
            handler = signal.getsignal(signal.SIGTERM)
            self.assertTrue(callable(handler))
            handler(signal.SIGTERM, None)
        output = io.StringIO()
        with patch.dict(os.environ, self.env, clear=True), patch.object(c, 'analyse', interrupt), redirect_stdout(output), \
                patch.object(sys, 'argv', ['integrity', '--repo', str(self.repo), '--base', self.base]):
            self.assertEqual(130, c.main())
        record = self.store._manifest(self.run)['attempts'][0]['test_integrity']; e.test_integrity_shape(record)
        self.assertEqual('interrupted', record['reason']); self.assertEqual('unverifiable', record['verdict'])
        self.assertEqual(2, record['unverifiable_files'])
        self.assertIn('unverifiable; removed assertions=0;', self.brief())

    def test_findings_are_sorted_and_bounded_in_record_and_brief(self):
        self.publish({f'tests/test_{number:02d}.py':'@pytest.mark.xfail\ndef test_value(): assert True\n'
                      for number in reversed(range(45))})
        record, brief = self.scan('flagged', (0, 45, 0, 0))
        self.assertEqual(40, len(record['findings'])); self.assertEqual(5, record['omitted'])
        self.assertEqual([f'tests/test_{number:02d}.py' for number in range(40)],
                         [row['path'] for row in record['findings']])
        block = brief.split('**Test integrity**')[1].split('**Review evidence**')[0]
        self.assertEqual(8, block.count(': new skip or disabled test')); self.assertIn('- 37 more.', block)

    def test_untracked_module_does_not_resolve_and_dirty_checkout_is_untouched(self):
        self.publish({'app.py':'import pkg.only_untracked\n'})
        self.write('pkg/only_untracked.py', 'VALUE = True\n')
        self.write('pkg/__init__.py', 'VALUE = 2\n'); self.git('add', 'pkg/__init__.py')
        self.write('pkg/__init__.py', 'VALUE = 3\n')
        self.write('app.py', 'import pkg\n')
        record, brief = self.scan('flagged', (0, 0, 1, 0))
        self.assertEqual('pkg/only_untracked', record['findings'][0]['module'])
        self.assertIn('`app.py`:1: unresolved first-party import', brief)

    def test_only_pinned_configuration_controls_setting_and_patterns(self):
        self.config['agents'] = dict(risk_tier=dict(test_patterns=['checks/*']))
        self.base_files({'checks/value.py':'assert True\n', 'tests/test_ignored.py':'assert True\n'})
        disabled = dict(repo=dict(test_integrity_check=False))
        self.publish({'checks/value.py':'pass\n', 'tests/test_ignored.py':'pass\n', '.bureau.json':json.dumps(disabled)})
        alternate = self.root / 'alternate.json'; alternate.write_text(json.dumps(disabled))
        self.env['BUREAU_CONFIG'] = str(alternate)
        record, brief = self.scan('flagged', (1, 0, 0, 0))
        self.assertEqual(['checks/value.py'], [row['path'] for row in record['findings']])
        self.assertIn('`checks/value.py`: assertions removed', brief)

    def test_provider_free_resolution_follows_local_opt_in_and_actual_base(self):
        main = self.base
        self.config['agents'] = dict(risk_tier=dict(test_patterns=['checks/*']))
        self.base_files({'checks/value.py':'assert True\n'})
        self.git('update-ref', 'refs/remotes/origin/main', main)
        self.git('update-ref', 'refs/remotes/origin/alternate', self.base)
        self.publish({'checks/value.py':'pass\n'})
        self.begin(); original = c.Git.prepare; calls = []
        def prepare(git, command):
            if command[0] == 'gh':
                calls.append(command)
                if command[2] == 'list': return b'7'
                return json.dumps(dict(state='OPEN', baseRefName='alternate', headRefName='feature')).encode()
            return original(git, command)
        output = io.StringIO()
        with patch.dict(os.environ, self.env, clear=True), patch.object(c.Git, 'prepare', prepare), redirect_stdout(output), \
                patch.object(sys, 'argv', ['integrity', '--repo', str(self.repo), '--base', '', '--branch', 'feature']):
            self.assertEqual(0, c.main())
        record = self.store._manifest(self.run)['attempts'][0]['test_integrity']; e.test_integrity_shape(record)
        self.assertEqual(2, len(calls)); self.assertEqual(self.base, record['base'])
        self.assertEqual('flagged', record['verdict']); self.assertEqual(1, record['checks']['assertions']['removed'])
        self.assertIn('Test integrity: flagged', output.getvalue())
        self.assertIn('`checks/value.py`: assertions removed', self.brief())
        self.config['repo']['test_integrity_check'] = False
        self.base_files({}); self.git('update-ref', 'refs/remotes/origin/main', self.base)
        self.publish({'checks/value.py':'assert True\n'}); self.begin(); before = self.brief(); calls.clear()
        with patch.dict(os.environ, self.env, clear=True), patch.object(c.Git, 'prepare', prepare), redirect_stdout(io.StringIO()), \
                patch.object(sys, 'argv', ['integrity', '--repo', str(self.repo), '--base', '', '--branch', 'feature']):
            self.assertEqual(0, c.main())
        self.assertEqual([], calls); self.assertNotIn('test_integrity', self.store._manifest(self.run)['attempts'][0])
        self.assertEqual(before, self.brief())

    def test_provider_free_preparation_timeout_records_unverifiable(self):
        self.git('update-ref', 'refs/remotes/origin/main', self.base)
        self.publish({'app.py':'import pkg\n'}); self.begin()
        original = c.Git.prepare
        def timeout(git, command):
            if command[0] == 'gh': raise subprocess.TimeoutExpired(command, 60)
            return original(git, command)
        with patch.dict(os.environ, self.env, clear=True), patch.object(c.Git, 'prepare', timeout), redirect_stdout(io.StringIO()), \
                patch.object(sys, 'argv', ['integrity', '--repo', str(self.repo), '--base', '', '--branch', 'feature']):
            self.assertEqual(0, c.main())
        record = self.store._manifest(self.run)['attempts'][0]['test_integrity']; e.test_integrity_shape(record)
        self.assertEqual('unverifiable', record['verdict']); self.assertEqual('budget exhausted', record['reason'])
        self.assertIn('unverifiable; removed assertions=0;', self.brief())

    def test_off_and_invalid_settings_make_no_record_or_brief_change(self):
        for value in ('absent', False, None, 0, 1, 'true', [], {}):
            with self.subTest(value=value):
                self.config = dict(repo={} if value == 'absent' else dict(test_integrity_check=value))
                self.base_files({})
                self.publish({'.bureau.json':json.dumps(dict(repo=dict(test_integrity_check=True))),
                              'tests/test_skip.py':'@pytest.mark.skip\ndef test_value(): assert True\n'})
                self.begin(); brief = self.brief()
                result = subprocess.run([sys.executable, '-I', str(HELPER), '--repo', str(self.repo), '--base', self.base],
                                        env=self.env, capture_output=True, text=True, timeout=15)
                self.assertEqual(0, result.returncode, result.stderr)
                self.assertNotIn('test_integrity', self.store._manifest(self.run)['attempts'][0])
                self.assertEqual(brief, self.brief())
                if value == 'absent' or value is False: self.assertEqual('', result.stdout)
                else: self.assertIn('Test integrity: not measured (invalid setting)', result.stdout)

    def test_optional_record_keeps_old_manifest_shape_and_survives_old_writes(self):
        self.publish({'app.js':"require('./missing');\n"})
        record, brief = self.scan('flagged', (0, 0, 1, 0))
        path = self.store.manifest_path(self.run); disk = json.loads(path.read_text())
        legacy = json.loads((changed.FIXTURES / 'manifest.json').read_text())
        self.assertEqual(set(legacy), set(disk)); self.assertEqual(set(legacy['attempts'][0]), set(disk['attempts'][0]))
        disk['attempts'][0]['ended_at'] = time.time(); path.write_text(json.dumps(disk))
        self.assertEqual(record, self.store._manifest(self.run)['attempts'][0]['test_integrity'])
        self.store.end_attempt(self.run, self.attempt, 0, self.head)
        self.assertNotIn('test_integrity', json.loads(path.read_text())['attempts'][0])
        self.assertEqual(record, self.store._manifest(self.run)['attempts'][0]['test_integrity'])
        sidecar = path.parent / 'test-integrity' / (self.attempt + '.json')
        sidecar.write_text(json.dumps(dict(record, verdict='pass')))
        self.assertNotIn('test_integrity', self.store._manifest(self.run)['attempts'][0])
        self.assertFalse(list((self.store.root / 'quarantine').glob('*.json')))
        self.assertNotIn('**Test integrity**', self.brief()); self.assertIn('**Test integrity**', brief)

    def test_integrity_block_follows_existing_fail_before_pass_evidence(self):
        self.publish({'app.js':"require('./missing');\n"})
        record, _ = self.scan('flagged', (0, 0, 1, 0))
        sibling = dict(base=self.base, head=self.head, recorded_at=record['recorded_at'], command_sha256='c' * 64,
                       files=[], omitted=0, reason=None)
        self.store.changed_tests(self.run, self.attempt, sibling)
        brief = self.brief()
        self.assertLess(brief.index('**Fail-before-pass**'), brief.index('**Test integrity**'))
        self.assertLess(brief.index('**Test integrity**'), brief.index('**Review evidence**'))
        self.assertEqual(record, self.store._manifest(self.run)['attempts'][0]['test_integrity'])

    def test_shape_rejects_unknown_text_counts_and_unsafe_module_metadata(self):
        self.publish({'app.js':"require('./missing');\n"})
        record, brief = self.scan('flagged', (0, 0, 1, 0))
        mutations = [dict(record, raw='must not persist'), dict(record, verdict='pass'),
                     dict(record, omitted=-1), dict(record, reason='unbounded prose')]
        wrong = copy.deepcopy(record); wrong['findings'][0]['module'] = '../outside'; mutations.append(wrong)
        wrong = copy.deepcopy(record); wrong['findings'][0]['path'] = 'bad\nname'; mutations.append(wrong)
        for value in mutations:
            with self.subTest(value=value), self.assertRaises(ValueError): e.test_integrity_shape(value)
        self.assertIn('unresolved first-party import', brief)


class QAIntegration(unittest.TestCase):
    def fixture(self):
        fixture = changed.ChangedTests('test_disabled_fixture_and_brief_match_golden')
        self.addCleanup(fixture.doCleanups); fixture.setUp()
        return fixture

    def test_disabled_brief_matches_existing_fail_before_pass_golden(self):
        fixture = self.fixture()
        fixture.test_disabled_fixture_and_brief_match_golden()
        self.assertNotIn('test_integrity', fixture.store._manifest(fixture.run)['attempts'][0])
        self.assertNotIn('**Test integrity**', fixture.brief())

    def test_flagged_integrity_preserves_qa_verdict_exit_and_gates(self):
        fixture = self.fixture()
        fixture.config['repo'].pop('changed_test_command')
        fixture.config['repo']['test_integrity_check'] = True
        fixture.write('tests/test_value.py', 'assert True\n')
        fixture.write('.bureau.json', json.dumps(fixture.config))
        fixture.git('add', '.'); fixture.git('commit', '-qm', 'test: enable static integrity')
        fixture.base = fixture.git('rev-parse', 'HEAD'); fixture.set_base(True)
        fixture.publish({'tests/test_value.py':'pass\n'})
        _, enabled = fixture.qa()
        record = fixture.store._manifest(fixture.run)['attempts'][0]['test_integrity']
        e.test_integrity_shape(record); self.assertEqual('flagged', record['verdict'])
        self.assertEqual(1, record['checks']['assertions']['removed'])
        self.assertIn('Test integrity: flagged', fixture.output)
        self.assertIn('flagged; removed assertions=1;', fixture.brief())
        _, disabled = fixture.qa(enabled=False)
        self.assertEqual((0, 'GREEN'), enabled); self.assertEqual(enabled, disabled)
        self.assertNotIn('test_integrity', fixture.store._manifest(fixture.run)['attempts'][0])
        self.assertNotIn('**Test integrity**', fixture.brief())

    def test_helper_failure_preserves_green_qa_and_reports_not_measured(self):
        fixture = self.fixture()
        fixture.config['repo'].pop('changed_test_command')
        fixture.config['repo']['test_integrity_check'] = True
        fixture.write('.bureau.json', json.dumps(fixture.config)); fixture.git('add', '.bureau.json')
        fixture.git('commit', '-qm', 'test: enable static integrity'); fixture.base = fixture.git('rev-parse', 'HEAD')
        fixture.publish({'tests/test_value.py':'assert True\n'})
        (fixture.scripts / 'bureau-test-integrity.py').write_text('raise RuntimeError("fixture helper failure")\n')
        _, outcome = fixture.qa()
        self.assertEqual((0, 'GREEN'), outcome); self.assertIn('Test integrity: not measured.', fixture.output)
        self.assertNotIn('test_integrity', fixture.store._manifest(fixture.run)['attempts'][0])
        self.assertNotIn('**Test integrity**', fixture.brief())


if __name__ == '__main__': unittest.main()
