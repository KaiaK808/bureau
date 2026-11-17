"""Exercise installed Python entry points without a model or network."""
import json
import os
from pathlib import Path
import re
import runpy
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / 'templates/scripts'
CACHE_IGNORE = 'scripts/__pycache__/'
GIT_ENV = {**os.environ, 'PYTHONDONTWRITEBYTECODE': '1',
           'GIT_CONFIG_GLOBAL': os.devnull, 'GIT_CONFIG_NOSYSTEM': '1',
           'GIT_CONFIG_COUNT': '3', 'GIT_CONFIG_KEY_0': 'maintenance.auto', 'GIT_CONFIG_VALUE_0': 'false',
           'GIT_CONFIG_KEY_1': 'commit.gpgsign', 'GIT_CONFIG_VALUE_1': 'false',
           'GIT_CONFIG_KEY_2': 'core.hooksPath', 'GIT_CONFIG_VALUE_2': os.devnull,
           'GIT_AUTHOR_NAME': 'Bureau Test', 'GIT_AUTHOR_EMAIL': 'test@example.invalid',
           'GIT_COMMITTER_NAME': 'Bureau Test', 'GIT_COMMITTER_EMAIL': 'test@example.invalid'}


class BytecodeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='bureau-bytecode-')
        self.addCleanup(self.temp.cleanup)
        self.repo = Path(self.temp.name).resolve() / 'adopting repo'
        self.repo.mkdir()
        self.scripts = self.repo / 'scripts'
        shutil.copytree(SCRIPTS, self.scripts, ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
        self.case_sensitive = not (self.repo / 'Scripts').exists()
        self.env = dict(GIT_ENV)
        for name in list(self.env):
            if name.startswith('BUREAU_') or name in ('BASH_ENV', 'ENV', 'GIT_DIR', 'GIT_WORK_TREE',
                    'GIT_INDEX_FILE', 'GIT_COMMON_DIR', 'GIT_OBJECT_DIRECTORY', 'GIT_ALTERNATE_OBJECT_DIRECTORIES',
                    'GIT_CONFIG_PARAMETERS', 'LINEAR_API_KEY', 'TELEGRAM_BOT_TOKEN', 'TELEGRAM_ALERT_CHAT_ID',
                    'GH_TOKEN', 'GITHUB_TOKEN', 'GH_ENTERPRISE_TOKEN', 'GITHUB_ENTERPRISE_TOKEN'):
                self.env.pop(name)
        self.env['BUREAU_CONFIG'] = str(self.repo / '.bureau.json')
        self.run_command(['git', 'init', '-qb', 'main'])
        config = dict(version=2, agents={'runner': 'claude', 'merge_mode': 'manual'},
                      linear={'teams': [{'id': 'team', 'key': 'T', 'states': {'merge': 'merge'}}]},
                      repo={'test_command': 'true'})
        (self.repo / '.bureau.json').write_text(json.dumps(config))
        (self.repo / 'AGENTS.md').write_text('<!-- bureau-init:begin -->\nFixture instructions.\n<!-- bureau-init:end -->\n')
        (self.repo / 'source.txt').write_text('fixture\n')
        self.run_command(['git', 'add', '.'])
        self.run_command(['git', 'commit', '-qm', 'test: fixture'])
        self.head = self.run_command(['git', 'rev-parse', 'HEAD']).stdout.strip()

    def run_command(self, command, *, status=0, env=None, input=None):
        result = subprocess.run(command, cwd=self.repo, env=env or self.env, input=input,
                                text=True, capture_output=True, timeout=30)
        self.assertEqual(result.returncode, status, result.stdout + result.stderr)
        return result

    def python(self, script, *args, **kwargs):
        # Match the pre-fix stage invocation: -I ignores PYTHONDONTWRITEBYTECODE.
        # No -B here: every entry point must also guard its own sibling imports.
        return self.run_command([sys.executable, '-I', str(self.scripts / script), *args], **kwargs)

    def assert_no_cache(self, call):
        caches = sorted(str(p.relative_to(self.repo)) for p in self.scripts.rglob('*')
                        if p.name == '__pycache__' or '__pycache__' in p.parts)
        self.assertEqual(caches, [], 'bytecode writer: ' + call + ': ' + ', '.join(caches))

    def require_filesystem(self, *, case_sensitive):
        if self.case_sensitive != case_sensitive:
            needed = 'case-sensitive' if case_sensitive else 'case-insensitive'
            actual = 'case-sensitive' if self.case_sensitive else 'case-insensitive'
            self.skipTest('requires a ' + needed + ' filesystem; fixture volume is ' + actual)

    def helper_cache(self):
        return runpy.run_path(str(self.scripts / 'bureau-paths.py'))['is_helper_cache']

    def exact_only_cache_control(self):
        path = self.scripts / 'bureau-paths.py'
        text = path.read_text()
        anchor = "return os.path.samefile(Path(repo).joinpath(*parts[:2]), Path(repo) / 'scripts/__pycache__')"
        self.assertEqual(text.count(anchor), 1)
        path.write_text(text.replace(anchor, 'return False'))

    def folded_cache_control(self):
        path = self.scripts / 'bureau-paths.py'
        text = path.read_text()
        anchor = "if parts[:2] == ['scripts', '__pycache__']:"
        self.assertEqual(text.count(anchor), 1)
        path.write_text(text.replace(anchor, "if [part.lower() for part in parts[:2]] == ['scripts', '__pycache__']:"))

    def test_missing_cache_directories_match_only_exact_components(self):
        match = self.helper_cache()
        for present in (False, True):
            with self.subTest(directory_exists=present):
                if present:
                    (self.scripts / '__pycache__').mkdir()
                self.assertTrue(match(self.repo, b'scripts/__pycache__/x.pyc'))
                self.assertTrue(match(self.repo, b'scripts/__pycache__'))
                if not present:
                    self.assertFalse(match(self.repo, b'Scripts/__PyCaChE__/x.pyc'))
                self.assertFalse(match(self.repo, b'application/__pycache__/x.pyc'))
                self.assertFalse(match(self.repo, b'scripts/__pycache__-fixture/x.pyc'))
                self.assertFalse(match(self.repo, b'scripts'))
                self.assertFalse(match(self.repo, b''))
        (self.scripts / '__pycache__').rmdir()
        self.assertTrue(match(self.repo, b'scripts/__pycache__/deleted.pyc'))
        self.assertFalse(match(self.repo, b'Scripts/__PyCaChE__/deleted.pyc'))

    def test_directory_identity_matches_on_every_filesystem_with_negative_control(self):
        # A symlinked alias exercises the samefile branch on case-sensitive
        # Linux CI too, where the case-variant tests skip.
        (self.scripts / '__pycache__').mkdir()
        (self.repo / 'helpers-alias').symlink_to('scripts', target_is_directory=True)
        self.assertTrue(self.helper_cache()(self.repo, b'helpers-alias/__pycache__/x.pyc'))
        self.assertFalse(self.helper_cache()(self.repo, b'helpers-alias/other/x.pyc'))
        self.exact_only_cache_control()
        self.assertFalse(self.helper_cache()(self.repo, b'helpers-alias/__pycache__/x.pyc'))

    def test_stage_python_entry_points_and_provider_commands(self):
        # Include every shipped Python entry point, even ones reached indirectly
        # by the provider (gates), scanner wrappers and doctor.
        inventory = set()
        for path in SCRIPTS.glob('*.sh'):
            inventory.update(re.findall(r'(bureau-[a-z-]+\.py)', path.read_text()))
        entries = {p.name for p in self.scripts.glob('*.py')}
        self.assertTrue(inventory <= entries, sorted(inventory - entries))
        for name in sorted(entries):
            with self.subTest(entry=name):
                if name == 'bureau-worktree-deps.py':
                    self.python(name, str(self.repo))
                else:
                    self.python(name, '--help')
                self.assert_no_cache(name + ' --help')
        for name, action in (('bureau-impeccable.py', 'detect'), ('bureau-semgrep.py', 'settings')):
            self.python(name, action, '--repo', str(self.repo), '--config', str(self.repo / '.bureau.json'))
            self.assert_no_cache(name + ' ' + action)
        self.python('bureau-doctor.py', '--repo', str(self.repo))
        self.assert_no_cache('bureau-doctor.py --repo')

    def test_stage_python_operations(self):
        spec = self.repo / 'spec.md'
        spec.write_text('# Feature\n\n## Governance and security\nDecision: not required\n'
                        'Rationale: This documentation change handles no credentials or personal data.\n')
        config = str(self.repo / '.bureau.json')
        commands = [
            ('bureau-runtime.py', '--repo', str(self.repo), 'status'),
            ('bureau-supervision.py', '--repo', str(self.repo), 'status'),
            ('bureau-spec.py', '--repo', str(self.repo), 'validate', str(spec)),
            ('bureau-ticket.py', '--repo', str(self.repo), '--issue', 'T-1', 'status'),
            ('bureau-review.py', 'settings', '--config', config),
            ('bureau-provider.py', '--repo', str(self.repo), '--stage', 'implement', '--config', config, '--describe'),
            ('bureau-adr.py', '--config', config, '--base', self.head, '--head', self.head),
        ]
        for command in commands:
            with self.subTest(command=command[0]):
                self.python(*command)
                self.assert_no_cache(' '.join(command))

    def test_provider_gate_imports_in_clean_environment(self):
        # A configured adopting-repository command can import a Bureau helper;
        # unlike direct execution, even a module that guards its own imports is
        # cached before its first statement runs.
        command = shlex.join([sys.executable, '-c',
            "import importlib.util; from pathlib import Path; "
            "s=importlib.util.spec_from_file_location('fixture_spec', Path('scripts/bureau-spec.py')); "
            "s.loader.exec_module(importlib.util.module_from_spec(s))"])
        code = '''import pathlib, runpy, sys
p = runpy.run_path('scripts/bureau-provider.py')
code, abort = p['outside_gate'](dict(untrusted_env='clean', timeout=10), pathlib.Path.cwd(), sys.argv[1], pathlib.Path('gate.log'))
assert code == 0 and abort is None, (code, abort)
'''
        # Do not let the test runner's policy mask a missing provider policy.
        self.run_command([sys.executable, '-I', '-B', '-c', code, command],
                         env={**self.env, 'PYTHONDONTWRITEBYTECODE': ''})
        self.assert_no_cache('provider outside_gate (clean; configured spec importer)')

    def test_provider_environment_negative_control(self):
        path = self.scripts / 'bureau-provider.py'
        text = path.read_text()
        self.assertEqual(text.count("    result['PYTHONDONTWRITEBYTECODE'] = '1'"), 1)
        path.write_text(text.replace("    result['PYTHONDONTWRITEBYTECODE'] = '1'", ''))
        with self.assertRaisesRegex(AssertionError, 'bytecode writer: provider outside_gate'):
            self.test_provider_gate_imports_in_clean_environment()
        self.assertTrue(list((self.scripts / '__pycache__').glob('bureau-spec.*.pyc')))

    def test_scanner_python_command(self):
        code = '''import pathlib, runpy, sys
repo = pathlib.Path.cwd()
scan = runpy.run_path('scripts/bureau-scan.py')
venv = repo / 'scanner-python'
(venv / 'bin').mkdir(parents=True)
(venv / 'bin/python').symlink_to(sys.executable)
(venv / 'lib' / ('python%d.%d' % sys.version_info[:2]) / 'site-packages').mkdir(parents=True)
env = scan['environment'](repo / 'scanner-environment')
assert env['PYTHONDONTWRITEBYTECODE'] == '1'
probe = "import importlib.util, pathlib, sys; assert sys.dont_write_bytecode; s=importlib.util.spec_from_file_location('scan_spec', pathlib.Path('scripts/bureau-spec.py')); s.loader.exec_module(importlib.util.module_from_spec(s))"
argv = scan['python_argv'](venv, repo / 'scan-invocation', '-c', probe)
result = scan['run_process'](argv, cwd=repo, env=env, timeout=10)
assert result.returncode == 0, result.stderr
'''
        self.run_command([sys.executable, '-I', '-B', '-c', code])
        self.assert_no_cache('scanner Python command')

    def prepare_review(self):
        directory = self.repo / 'review'
        directory.mkdir()
        reports = {}
        for role in ('correctness', 'security', 'performance'):
            reports[role] = dict(specialist=role, counts=dict(critical=0, bug=0, skip=0, minor=0),
                                 findings=[], summary=role)
            (directory / (role + '.txt')).write_text(json.dumps(reports[role]))
        self.python('bureau-review.py', 'merge', '--directory', str(directory), '--base', self.head, '--head', self.head)
        # run_path executes the evidence file as a script, so setup itself cannot
        # manufacture an import cache or inherit another module's bytecode guard.
        setup = '''import json, os, pathlib, runpy, uuid
repo = pathlib.Path.cwd()
e = runpy.run_path(str(repo / 'scripts/bureau-evidence.py'))
store = e['Store'](repo / '.git/bureau')
run = uuid.uuid4().hex
head = os.environ['TEST_HEAD']
store.begin_run(run, 'T-1', repo, head)
attempt = store.begin_attempt(run, 'T-1', 'code_review', 'stage', repo, head)
for role in ('correctness', 'security', 'performance'):
    report = json.loads((repo / 'review' / (role + '.txt')).read_text())
    logs = repo / ('provider-' + role)
    logs.mkdir()
    (logs / 'final.txt').write_text(json.dumps(report))
    store.put_receipt(dict(run_id=run, stage_attempt_id=attempt, provider_attempt_id=uuid.uuid4().hex,
        issue='T-1', stage='code_review', phase=role, provider='codex', provider_exit=0,
        normalized_exit=0, outcome='complete', start_head=head, end_head=head,
        provider_logs=str(logs), structured_result=None, insights_result=report,
        usage=dict(input_tokens=1, output_tokens=1), total_cost_usd=0))
print(json.dumps(dict(BUREAU_RUN_ID=run, BUREAU_STAGE_ATTEMPT_ID=attempt)))
'''
        result = self.run_command([sys.executable, '-I', '-B', '-c', setup], env={**self.env, 'TEST_HEAD': self.head})
        self.env.update(json.loads(result.stdout))
        self.assert_no_cache('review fixture preparation')
        return directory

    def collect_review(self, directory):
        return self.python('bureau-evidence.py', '--repo', str(self.repo), '--round', '1', '--min-severity', 'BUG',
                           '--provider-verdict', 'APPROVE', '--final-verdict', 'APPROVE',
                           '--merge-metadata', str(directory / 'merge-metadata.json'),
                           '--merge-result', str(directory / 'merged.json'))

    def test_evidence_lazy_review_import(self):
        directory = self.prepare_review()
        self.collect_review(directory)
        self.assert_no_cache('bureau-evidence.py --round (deterministic review)')

    def test_evidence_guard_negative_control(self):
        path = self.scripts / 'bureau-evidence.py'
        text = path.read_text()
        self.assertEqual(text.count('sys.dont_write_bytecode = True'), 1)
        path.write_text(text.replace('sys.dont_write_bytecode = True', ''))
        with self.assertRaisesRegex(AssertionError, 'bytecode writer: bureau-evidence.py'):
            self.test_evidence_lazy_review_import()
        self.assertTrue(list((self.scripts / '__pycache__').glob('bureau-review.*.pyc')))

    def commit_stage(self, stage='implement', *, status=0):
        text = (self.scripts / 'bureau-config.sh').read_text()
        start = text.index('commit_stage_changes() {')
        helper = text[start:text.index('\n}\n', start) + 3]
        if stage in ('spec', 'ux'):
            entry = (self.scripts / (stage + '-pipeline.sh')).read_text()
            call = re.search(r'^commit_stage_changes ' + stage + r' .+$', entry, re.M)
            self.assertIsNotNone(call, 'stage must use the filtered commit helper')
            invocation = 'ISSUE=T-1\n' + call.group()
        else:
            invocation = 'commit_stage_changes ' + stage + ' T-1'
        shell = 'set -euo pipefail\nsource scripts/bureau-env.sh\n' + helper + '\n' + invocation
        return self.run_command(['/bin/bash', '-c', shell], status=status)

    def check_stage_commits_exclude_cache(self, directory):
        cache = self.repo / directory / 'x.pyc'
        cache.parent.mkdir(parents=True)
        cache.write_bytes(b'old cache\0')
        # Preserve the supplied spelling in Git's index even on macOS.
        self.run_command(['git', '-c', 'core.ignorecase=false', 'add', '--', str(cache.relative_to(self.repo))])
        self.run_command(['git', 'commit', '-qm', 'test: previously tracked cache'])
        for stage in ('spec', 'spec_review', 'ux', 'copy', 'implement', 'qa'):
            before = self.run_command(['git', 'rev-parse', 'HEAD']).stdout.strip()
            cache.write_bytes(b'new cache\0')
            fresh = cache.with_name('new\ncache.pyc'); fresh.write_bytes(b'new\0')
            (self.repo / 'source.txt').write_text(stage + '\n')
            self.commit_stage(stage)
            changed = self.run_command(['git', 'diff', '--name-only', '-z', before, 'HEAD']).stdout.split('\0')
            self.assertIn('source.txt', changed)
            self.assertNotIn(str(cache.relative_to(self.repo)), changed, (stage, changed))
            self.assertNotIn(str(fresh.relative_to(self.repo)), changed, (stage, changed))
            self.assertEqual(self.run_command(['git', 'show', 'HEAD:' + str(cache.relative_to(self.repo))]).stdout,
                             'old cache\0')
            self.assertEqual(self.run_command(['git', 'diff', '--cached', '--name-only']).stdout, '')

    def test_stage_commits_exclude_unignored_and_already_tracked_cache(self):
        self.check_stage_commits_exclude_cache('scripts/__pycache__')

    def test_stage_commits_exclude_case_variant_cache(self):
        self.require_filesystem(case_sensitive=False)
        self.assertTrue(os.path.samefile(self.scripts, self.repo / 'Scripts'))
        self.check_stage_commits_exclude_cache('Scripts/__PyCaChE__')

    def test_ux_preserves_empty_artifact_commit(self):
        self.assertEqual(self.run_command(['git', 'status', '--porcelain']).stdout, '')
        self.commit_stage('ux')
        self.assertNotEqual(self.run_command(['git', 'rev-parse', 'HEAD']).stdout.strip(), self.head)
        self.assertEqual(self.run_command(['git', 'show', '-s', '--format=%s']).stdout.strip(), 'T-1: design artifacts')
        self.assertIn('Bureau-Generated: true', self.run_command(['git', 'show', '-s', '--format=%B']).stdout)
        self.assertEqual(self.run_command(['git', 'diff', '--name-only', self.head, 'HEAD']).stdout, '')

    def check_pre_staged_cache_refuses(self, directory):
        cache = self.repo / directory / 'x.pyc'
        cache.parent.mkdir(parents=True); cache.write_bytes(b'cache\0')
        (self.repo / 'source.txt').write_text('stage me too\n')
        self.run_command(['git', 'add', '--', 'source.txt'])
        self.run_command(['git', '-c', 'core.ignorecase=false', 'add', '--', str(cache.relative_to(self.repo))])
        self.assertIn(str(cache.relative_to(self.repo)),
                      self.run_command(['git', 'diff', '--cached', '--name-only', '-z']).stdout.split('\0'))
        before = self.run_command(['git', 'write-tree']).stdout
        result = self.commit_stage(status=24)
        self.assertIn('staged Bureau bytecode', result.stderr)
        self.assertEqual(self.run_command(['git', 'write-tree']).stdout, before)
        self.assertEqual(self.run_command(['git', 'rev-parse', 'HEAD']).stdout.strip(), self.head)

    def test_pre_staged_cache_refuses_without_mutating_index(self):
        self.check_pre_staged_cache_refuses('scripts/__pycache__')

    def test_pre_staged_case_variant_cache_refuses_without_mutating_index(self):
        self.require_filesystem(case_sensitive=False)
        self.check_pre_staged_cache_refuses('Scripts/__PyCaChE__')

    def test_pre_staged_deleted_cache_refuses_without_mutating_index(self):
        cache = self.scripts / '__pycache__/x.pyc'
        cache.parent.mkdir(); cache.write_bytes(b'old cache\0')
        self.run_command(['git', 'add', '--', 'scripts/__pycache__/x.pyc'])
        self.run_command(['git', 'commit', '-qm', 'test: previously tracked cache'])
        before_head = self.run_command(['git', 'rev-parse', 'HEAD']).stdout
        shutil.rmtree(cache.parent)
        self.run_command(['git', 'add', '-u'])
        before_index = self.run_command(['git', 'write-tree']).stdout
        self.commit_stage(status=24)
        self.assertEqual(self.run_command(['git', 'write-tree']).stdout, before_index)
        self.assertEqual(self.run_command(['git', 'rev-parse', 'HEAD']).stdout, before_head)

    def install_scripts(self, target='codex'):
        self.run_command([sys.executable, '-I', '-B', str(ROOT / 'scripts/bureau_install.py'), 'assets',
                          '--repo', str(self.repo), '--target', target, '--scope', 'scripts', '--apply'])

    def check_separate_application_cache_commits(self, *, pre_staged):
        self.require_filesystem(case_sensitive=True)
        cache = self.repo / 'Scripts/__pycache__/golden.pyc'
        cache.parent.mkdir(parents=True); cache.write_bytes(b'fixture\0')
        (self.scripts / '__pycache__').mkdir()
        self.assertFalse(os.path.samefile(self.scripts / '__pycache__', cache.parent))
        self.run_command(['git', 'add', '--', 'Scripts/__pycache__/golden.pyc', '.gitignore'])
        self.run_command(['git', 'commit', '-qm', 'test: application fixture'])
        for stage in ('spec', 'spec_review', 'ux', 'copy', 'implement', 'qa'):
            before = self.run_command(['git', 'rev-parse', 'HEAD']).stdout.strip()
            cache.write_bytes(stage.encode() + b'\0')
            fresh = cache.with_name(stage + '.pyc'); fresh.write_bytes(b'new fixture\0')
            self.run_command(['git', 'check-ignore', '-q', str(fresh.relative_to(self.repo))], status=1)
            (self.repo / 'source.txt').write_text(stage + '\n')
            if pre_staged:
                self.run_command(['git', 'add', '--', 'Scripts/__pycache__/golden.pyc'])
            self.commit_stage(stage)
            changed = self.run_command(['git', 'diff', '--name-only', '-z', before, 'HEAD']).stdout.split('\0')
            self.assertIn('Scripts/__pycache__/golden.pyc', changed, stage)
            self.assertIn(str(fresh.relative_to(self.repo)), changed, stage)
            self.assertIn('source.txt', changed, stage)
            self.assertEqual(self.run_command(['git', 'show', 'HEAD:Scripts/__pycache__/golden.pyc']).stdout,
                             stage + '\0')
            self.assertEqual(self.run_command(['git', 'diff', '--cached', '--name-only']).stdout, '')

    def test_separate_application_cache_commits_normally(self):
        self.require_filesystem(case_sensitive=True)
        self.install_scripts()
        self.check_separate_application_cache_commits(pre_staged=False)

    def test_pre_staged_separate_application_cache_commits_normally(self):
        self.require_filesystem(case_sensitive=True)
        self.install_scripts()
        self.check_separate_application_cache_commits(pre_staged=True)

    def test_separate_application_cache_filter_negative_control(self):
        self.require_filesystem(case_sensitive=True)
        # Install before mutating the copy so resync cannot repair the control.
        self.install_scripts()
        self.folded_cache_control()
        with self.assertRaises(AssertionError):
            self.check_separate_application_cache_commits(pre_staged=False)

    def test_separate_application_cache_pre_staged_negative_control(self):
        self.require_filesystem(case_sensitive=True)
        self.install_scripts()
        self.folded_cache_control()
        with self.assertRaises(AssertionError):
            self.check_separate_application_cache_commits(pre_staged=True)

    def test_spec_phase_checkpoint_commits_only_reserved_artifacts(self):
        branch = '001-t-1'
        self.run_command(['git', 'checkout', '-qb', branch])
        directory = self.repo / 'specs' / branch
        directory.mkdir(parents=True)
        (directory / 'spec.md').write_text('# Reserved feature\n')
        cache = self.scripts / '__pycache__/pre-staged.pyc'
        cache.parent.mkdir(); cache.write_bytes(b'cache\0')
        self.run_command(['git', 'add', str(cache.relative_to(self.repo))])
        self.python('bureau-spec.py', '--repo', str(self.repo), 'commit-phase', 'T-1', 'specify',
                    '--specs-dir', 'specs', '--directory', str(directory))
        paths = self.run_command(['git', 'diff-tree', '--no-commit-id', '--name-only', '-r', 'HEAD']).stdout.splitlines()
        self.assertEqual(paths, ['specs/' + branch + '/spec.md'])
        self.assertEqual(self.run_command(['git', 'diff', '--cached', '--name-only']).stdout.strip(),
                         'scripts/__pycache__/pre-staged.pyc')

    def test_commit_exclusion_negative_control(self):
        path = self.scripts / 'bureau-config.sh'
        text = path.read_text()
        anchor = "and not is_helper_cache('.', p)]"
        self.assertEqual(text.count(anchor), 1)
        path.write_text(text.replace(anchor, ']'))
        with self.assertRaises(AssertionError):
            self.test_stage_commits_exclude_unignored_and_already_tracked_cache()
        tracked = self.run_command(['git', 'ls-files', 'scripts/__pycache__/']).stdout
        self.assertIn('new', tracked)

    def test_pre_staged_guard_negative_control(self):
        path = self.scripts / 'bureau-config.sh'
        text = path.read_text()
        anchor = "if any(is_helper_cache('.', p) for p in names):"
        self.assertEqual(text.count(anchor), 1)
        path.write_text(text.replace(anchor, 'if False:'))
        with self.assertRaises(AssertionError):
            self.test_pre_staged_cache_refuses_without_mutating_index()

    def test_case_variant_commit_filter_negative_control(self):
        self.require_filesystem(case_sensitive=False)
        self.exact_only_cache_control()
        with self.assertRaises(AssertionError):
            self.test_stage_commits_exclude_case_variant_cache()
        self.assertIn('Scripts/__PyCaChE__/new\ncache.pyc', self.run_command(
            ['git', 'ls-files', '-z', '--', 'Scripts/__PyCaChE__/']).stdout.split('\0'))

    def test_case_variant_pre_staged_guard_negative_control(self):
        self.require_filesystem(case_sensitive=False)
        self.exact_only_cache_control()
        with self.assertRaises(AssertionError):
            self.test_pre_staged_case_variant_cache_refuses_without_mutating_index()
        self.assertIn('Scripts/__PyCaChE__/x.pyc',
                      self.run_command(['git', 'ls-tree', '-r', '--name-only', 'HEAD']).stdout.splitlines())

    def test_installer_resync_ignore_is_idempotent_and_has_negative_control(self):
        install = [sys.executable, '-I', '-B', str(ROOT / 'scripts/bureau_install.py'), 'assets',
                   '--repo', str(self.repo), '--target', 'codex', '--scope', 'scripts', '--apply']
        # Model an older installation that already ignores only the manifest.
        ignore = self.repo / '.gitignore'
        ignore.write_text('user-rule\n.bureau-install.json\n')
        for _ in range(2):
            self.run_command(install)
            lines = ignore.read_text().splitlines()
            self.assertEqual(lines.count(CACHE_IGNORE), 1)
            self.assertEqual(lines.count('.bureau-install.json'), 1)
            self.assertIn('user-rule', lines)
        self.run_command(['git', 'check-ignore', '-q', 'scripts/__pycache__/probe.pyc'])
        # Independent negative control: remove just the new ignore entry.
        ignore.write_text('user-rule\n.bureau-install.json\n')
        self.run_command(['git', 'check-ignore', '-q', 'scripts/__pycache__/probe.pyc'], status=1)
        # Mutation control: resync without the installer rule cannot fix the
        # same old installation, despite all the other runtime protections.
        code = '''import pathlib, sys
path = pathlib.Path(sys.argv[1])
source = path.read_text()
anchor = '(MANIFEST, "scripts/__pycache__/")'
assert source.count(anchor) == 1
sys.argv = sys.argv[1:]
exec(compile(source.replace(anchor, '(MANIFEST,)'), str(path), 'exec'),
     dict(__name__='__main__', __file__=str(path)))
'''
        self.run_command([sys.executable, '-I', '-B', '-c', code, *install[3:]])
        with self.assertRaises(AssertionError):
            self.assertIn(CACHE_IGNORE, ignore.read_text().splitlines())

    def test_installer_ignore_preserves_separate_application_cache_with_negative_control(self):
        self.require_filesystem(case_sensitive=True)
        ignore = self.repo / '.gitignore'
        ignore.write_text('user-rule\nscripts/__pycache__/\n')
        probe = 'Scripts/__pycache__/golden.pyc'
        for _ in range(2):
            self.install_scripts()
        lines = ignore.read_text().splitlines()
        self.assertEqual(lines.count(CACHE_IGNORE), 1)
        self.assertIn('user-rule', lines)
        self.run_command(['git', 'check-ignore', '-q', 'scripts/__pycache__/probe.pyc'])
        self.run_command(['git', 'check-ignore', '-q', probe], status=1)
        # Restore round 2's overbroad rule: a distinct fixture becomes hidden.
        ignore.write_text(ignore.read_text() + '[sS][cC][rR][iI][pP][tT][sS]/__[pP][yY][cC][aA][cC][hH][eE]__/\n')
        with self.assertRaises(AssertionError):
            self.run_command(['git', 'check-ignore', '-q', probe], status=1)

    def check_doctor_tracked_cache(self, directory):
        cache = self.repo / directory / 'x.pyc'
        cache.parent.mkdir(parents=True); cache.write_bytes(b'cache\0')
        command = ['bureau-doctor.py', '--repo', str(self.repo)]
        def warnings():
            return json.loads(self.python(*command).stdout)['warnings']
        self.assertFalse(any('git rm --cached' in warning for warning in warnings()))
        self.run_command(['git', '-c', 'core.ignorecase=false', 'add', '--', str(cache.relative_to(self.repo))])
        found = warnings()
        self.assertTrue(any('scripts/__pycache__/' in warning and 'git rm --cached' in warning for warning in found), found)
        # The warning is required even when a resync already added the ignore.
        (self.repo / '.gitignore').write_text('scripts/__pycache__/\n')
        self.assertTrue(any('git rm --cached' in warning for warning in warnings()))
        if directory == 'scripts/__pycache__':
            shutil.rmtree(cache.parent)
            self.assertTrue(any('git rm --cached' in warning for warning in warnings()))
            cache.parent.mkdir(); cache.write_bytes(b'cache\0')
        path = self.scripts / 'bureau-doctor.py'
        text = path.read_text(); self.assertEqual(text.count('    if any(tracked_cache):'), 1)
        path.write_text(text.replace('    if any(tracked_cache):', '    if False:'))
        with self.assertRaises(AssertionError):
            self.assertTrue(any('git rm --cached' in warning for warning in warnings()))
        warning = next(w for w in found if 'git rm --cached' in w)
        cleanup = warning.split('then run ', 1)[1].split(' once and commit', 1)[0]
        self.run_command(shlex.split(cleanup))
        self.assertTrue(cache.is_file())
        self.assertEqual(self.run_command(['git', 'ls-files', '-z', '--', ':(literal)' + directory + '/']).stdout, '')

    def test_doctor_warns_only_for_tracked_cache_with_negative_control(self):
        self.check_doctor_tracked_cache('scripts/__pycache__')

    def test_doctor_warns_for_case_variant_tracked_cache(self):
        self.require_filesystem(case_sensitive=False)
        self.check_doctor_tracked_cache('Scripts/__PyCaChE__')

    def test_doctor_case_variant_negative_control(self):
        self.require_filesystem(case_sensitive=False)
        self.exact_only_cache_control()
        with self.assertRaises(AssertionError):
            self.test_doctor_warns_for_case_variant_tracked_cache()

    def test_doctor_ignores_separate_application_cache(self):
        self.require_filesystem(case_sensitive=True)
        cache = self.repo / 'Scripts/__pycache__/golden.pyc'
        cache.parent.mkdir(parents=True); cache.write_bytes(b'fixture\0')
        (self.scripts / '__pycache__').mkdir()
        self.assertFalse(os.path.samefile(self.scripts / '__pycache__', cache.parent))
        self.run_command(['git', 'add', '--', str(cache.relative_to(self.repo))])
        self.run_command(['git', 'commit', '-qm', 'test: application fixture'])
        warnings = json.loads(self.python('bureau-doctor.py', '--repo', str(self.repo)).stdout)['warnings']
        self.assertFalse(any('git rm --cached' in w for w in warnings), warnings)

    def test_doctor_separate_application_cache_negative_control(self):
        self.require_filesystem(case_sensitive=True)
        self.folded_cache_control()
        with self.assertRaises(AssertionError):
            self.test_doctor_ignores_separate_application_cache()

    def test_shell_environment_and_isolated_python_negative_controls(self):
        importer = ("import importlib.util; from pathlib import Path; "
                    "s=importlib.util.spec_from_file_location('probe_spec', Path('scripts/bureau-spec.py')); "
                    "s.loader.exec_module(importlib.util.module_from_spec(s))")
        direct = 'source scripts/bureau-env.sh\n' + shlex.join([sys.executable, '-c', importer])
        self.run_command(['/bin/bash', '-c', direct], env={**self.env, 'PYTHONDONTWRITEBYTECODE': ''})
        self.assert_no_cache('direct Python inherits shell policy')
        for mode in ('clean', 'default'):
            config = json.loads((self.repo / '.bureau.json').read_text())
            config['repo']['untrusted_env'] = mode
            (self.repo / '.bureau.json').write_text(json.dumps(config))
            for wrapper in ('bureau_untrusted_env', 'bureau_without_secrets'):
                shell = ('source scripts/bureau-env.sh\nexport BUREAU_CONFIG="$PWD/.bureau.json"\n' + wrapper +
                         ' PYTHONDONTWRITEBYTECODE= ' + shlex.join([sys.executable, '-c', importer]))
                self.run_command(['/bin/bash', '-c', shell], env={**self.env, 'PYTHONDONTWRITEBYTECODE': ''})
                self.assert_no_cache(wrapper + ' ' + mode)
        shell = 'source scripts/bureau-env.sh\nbureau_without_secrets ' + shlex.join([sys.executable, '-I', '-B', '-c', importer])
        self.run_command(['/bin/bash', '-c', shell])
        self.assert_no_cache('isolated shell Python with -B')
        self.run_command(['/bin/bash', '-c', shell.replace(' -B ', ' ')])
        with self.assertRaisesRegex(AssertionError, 'bytecode writer'):
            self.assert_no_cache('isolated shell Python without -B')

    def test_shell_export_negative_control(self):
        path = self.scripts / 'bureau-env.sh'
        text = path.read_text()
        anchor = 'export PYTHONDONTWRITEBYTECODE=1'
        self.assertEqual(text.count(anchor), 1)
        path.write_text(text.replace(anchor, ''))
        with self.assertRaisesRegex(AssertionError, 'bytecode writer: direct Python'):
            self.test_shell_environment_and_isolated_python_negative_controls()

    def test_shell_clean_policy_negative_control(self):
        path = self.scripts / 'bureau-env.sh'
        text = path.read_text()
        line = next(line for line in text.splitlines() if line.startswith('  /usr/bin/env ') and '_bue_assign' in line)
        changed = line.replace(' PYTHONDONTWRITEBYTECODE=1', '')
        self.assertNotEqual(changed, line)
        path.write_text(text.replace(line, changed))
        with self.assertRaisesRegex(AssertionError, 'bytecode writer: bureau_untrusted_env clean'):
            self.test_shell_environment_and_isolated_python_negative_controls()

    def test_all_isolated_shell_calls_pass_B(self):
        def check(text):
            for line in text.splitlines():
                if not line.lstrip().startswith('#') and re.search(r'(?:python3|"\$_BUREAU_ENV_PYTHON") -I\b', line):
                    self.assertRegex(line, r' -I -B\b')
        for path in self.scripts.glob('*.sh'):
            check(path.read_text())
        original = (self.scripts / 'spec-pipeline.sh').read_text()
        with self.assertRaises(AssertionError):
            check(original.replace('python3 -I -B', 'python3 -I', 1))


if __name__ == '__main__':
    unittest.main()
