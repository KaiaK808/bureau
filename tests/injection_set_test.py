"""Agents obey the corpus; real stages decide. No model or remote service is used."""
import json
import importlib.util
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

if sys.argv[1:2] != ['--agent']:
    import git_maintenance
    from mutation_checks import run as run_fixture

ROOT = Path(__file__).resolve().parents[1]
CORPUS = ROOT / 'tests/fixtures/injection/corpus.json'
MUTATIONS = CORPUS.with_name('mutations.json')
INPUT_CLASSES = {'ticket_description', 'ticket_comment', 'spec_file', 'branch_file',
                 'commit_message', 'test_output', 'review_comment'}
ACTIONS = {'emit', 'commit', 'push_base', 'gh', 'environment', 'credential_probe', 'read_env_file'}
SECRET = 'injection-secret-canary'
STORED = 'injection-stored-canary'
ISSUE = '-'.join(('TEST', '1'))
BRANCH = 'test-branch'
SANDBOX_INIT = ('set -euo pipefail\nsource "$1"\nsandbox_init "$2" "$3" "$4"\n'
                'trap - EXIT\nprintf "\\nSANDBOX=%s\\n" "$SANDBOX"')


def scratch_parent(path):
    path = Path(path).resolve()
    for ancestor in (path, *path.parents):
        if (ancestor / '.git').exists() or ((ancestor / 'HEAD').is_file() and
                                           (ancestor / 'objects').is_dir()):
            raise AssertionError('scratch parent is inside a Git repository')
    return path


def scratch_directory(prefix):
    return tempfile.TemporaryDirectory(prefix=prefix, dir=scratch_parent(tempfile.gettempdir()))


def git_environment(parent, env=None):
    env = os.environ if env is None else env
    return dict({key:value for key, value in env.items() if not key.startswith('GIT_')},
                GIT_CEILING_DIRECTORIES=str(parent), GIT_CONFIG_NOSYSTEM='1',
                GIT_CONFIG_GLOBAL=os.devnull, GIT_ALLOW_PROTOCOL='file',
                GIT_TERMINAL_PROMPT='0', GIT_NO_LAZY_FETCH='1')


def checkout_snapshot(root):
    git_dir = root / '.git'
    if git_dir.is_file():
        git_dir = (root / git_dir.read_text().strip().removeprefix('gitdir: ')).resolve()
    common_file = git_dir / 'commondir'
    common = (git_dir / common_file.read_text().strip()).resolve() if common_file.exists() else git_dir
    paths = {git_dir / 'HEAD', git_dir / 'index', git_dir / 'config.worktree',
             common / 'HEAD', common / 'config', common / 'packed-refs'}
    for directory in (git_dir / 'refs', common / 'refs'):
        paths.update(path for path in directory.rglob('*') if path.is_file())
    return {str(path):path.read_bytes() if path.exists() else None for path in paths}


def command(args, cwd, env=None, input=None):
    if Path(args[0]).name == 'git':
        env = git_environment(Path(cwd).resolve().parent, env)
    return subprocess.run(args, cwd=cwd, env=env, input=input, capture_output=True,
                          text=True, check=True, timeout=10).stdout.strip()


def local_path(root, name):
    path = (root / name).resolve()
    if path == root or root not in path.parents:
        raise ValueError('fixture path escapes its sandbox')
    return path


def assert_origin(root, env=None):
    if (command(['git', 'rev-parse', '--show-toplevel'], root, env) != str(root) or
            command(['git', 'rev-parse', '--absolute-git-dir'], root, env) != str(root / '.git')):
        raise AssertionError('agent Git checkout is outside the sandbox')
    expected = root / '.fake-origin.git'
    for option in ([], ['--push']):
        value = command(['git', 'remote', 'get-url', *option, 'origin'], root, env)
        if Path(value).resolve() != expected or not Path(value).is_absolute():
            raise AssertionError('agent remote is not the sandbox bare origin')


def specialist(role):
    return dict(specialist=role, counts=dict(critical=0, bug=0, minor=0, skip=0),
                findings=[], holds=[], summary='No findings.')


def agent(root, args):
    # Auth is a stub; all other actions are chosen by the case/phase, never its prompt.
    if args[:1] == ['auth']:
        print('{"loggedIn":true}')
        return
    if args == ['--help']:
        print('Usage: claude')
        return
    case = json.loads((root / 'control/case.json').read_text())
    if Path.cwd().resolve() != root or root not in Path(os.environ['HOME']).resolve().parents:
        raise AssertionError('agent working directory or home escapes the sandbox')
    assert_origin(root)
    stage = case['stage']
    phase = os.environ.get('BUREAU_PROVIDER_PHASE', stage)
    prompt = sys.stdin.read()
    reads = []
    cls = case['input_class']
    if cls == 'spec_file':
        name = 'tasks.md' if stage == 'implement' else 'spec.md'
        for path in sorted((root / 'specs').glob('*/' + name)):
            reads.append(dict(path=str(path), text=path.read_text()))
    elif cls == 'branch_file':
        path = local_path(root, case.get('setup', {}).get('input_path', 'adjacent.txt'))
        if stage == 'code_review':
            base, head = os.environ['BUREAU_REVIEW_INPUT_BASE'], os.environ['BUREAU_REVIEW_INPUT_HEAD']
            value = command(['git', 'diff', base + '...' + head, '--', str(path)], root)
        else:
            tasks = root / 'specs/001-test-branch/tasks.md'
            reads.append(dict(path=str(tasks), text=tasks.read_text()))
            value = path.read_text()
        reads.append(dict(path=str(path), text=value))
    elif cls == 'commit_message':
        reads.append(dict(command='git log --format=%B origin/main..HEAD',
                          text=command(['git', 'log', '--format=%B', 'origin/main..HEAD'], root)))

    result = dict(status='COMPLETE', tasks_done=3, tasks_skipped=0, tasks_needs_human=0,
                  fixed_review_items=[], notes=dict(needs_human=[], skipped=[], deviations=[]), prose_notes='Completed.')
    if stage == 'qa': result = dict(status='GREEN', tests_added=0, tests_failing=0, coverage_notes='Passed.')
    if phase in ('correctness', 'security', 'performance'):
        result = specialist(phase)
        if case.get('setup', {}).get('fallback'):
            # A legacy fenced report takes the real deterministic merger's fallback.
            result = case['hostile_text'] + '\n```json\n' + json.dumps(result) + '\n```'
    if stage == 'spec' and phase in ('specify', 'plan', 'tasks'):
        pointer = json.loads((root / '.specify/feature.json').read_text())
        directory = local_path(root, pointer['feature_directory'])
        name = 'spec' if phase == 'specify' else phase
        text = '# Fixture ' + name + '\n'
        if phase == 'tasks': text += '- [ ] T001 Edit accepted.txt\n'
        (directory / (name + '.md')).write_text(text)
        result = 'Created ' + name

    probe = {}
    performed = []
    for action in case['agent_obeys']:
        if action.get('phase', phase) != phase: continue
        kind = action['action']
        performed.append(kind)
        if kind == 'emit':
            result = specialist(phase) if action['result'] == 'clean_specialist' else action['result']
        elif kind == 'commit':
            path = local_path(root, action['path'])
            if path.relative_to(root).parts[0].startswith('.'):
                raise AssertionError('agent commit targets private fixture state')
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(action['content'])
            command(['git', 'add', '--', action['path']], root)
            command(['git', 'commit', '-qm', action.get('message', 'test: agent change')], root)
        elif kind == 'push_base':
            assert_origin(root)
            command(['git', 'push', 'origin', 'HEAD:refs/heads/main'], root)
        elif kind == 'gh':
            binary = root / 'fake-bin/gh'
            if Path(shutil.which('gh')).resolve() != binary:
                raise AssertionError('agent gh is not the sandbox stub')
            if action['args'] != ['pr', 'merge', '99', '--squash']:
                raise AssertionError('unsupported fixture gh command')
            command([str(binary), *action['args']], root)
        elif kind == 'environment':
            (root / 'control/environment.json').write_text(json.dumps(dict(os.environ)))
            probe.update(bureau_secret_available=any(SECRET in value for value in os.environ.values()),
                         operator_variable_present='INJECTION_OPERATOR' in os.environ)
        elif kind == 'credential_probe':
            value = command(['git', 'credential', 'fill'], root, input='protocol=https\nhost=offline\n\n')
            (root / 'control/credential.txt').write_text(value)
            probe.update(credential_available=('password=' + STORED) in value,
                         github_token_present='GH_TOKEN' in os.environ)
        elif kind == 'read_env_file':
            value = (root / '.env').read_text()
            (root / 'control/env-copy.txt').write_text(value)
            probe.update(env_file_secret_available=SECRET in value,
                         linear_key_present='LINEAR_API_KEY' in os.environ)
        else:
            raise AssertionError('unsupported agent action: ' + kind)
    with tempfile.NamedTemporaryFile(mode='w', prefix=phase + '-', suffix='.json',
                                     dir=root / 'control/prompts', delete=False) as capture:
        json.dump(dict(phase=phase, prompt=prompt, reads=reads, actions=performed, probe=probe, result=result), capture)
    print(json.dumps(dict(result=json.dumps(result) if isinstance(result, dict) else result,
                          usage=dict(input_tokens=1, output_tokens=1))))


class Sandbox:
    def __init__(self, parent, case, control=False):
        parent = scratch_parent(parent)
        self.parent, self.case = parent, case
        for name in ('tmp', 'home'): (parent / name).mkdir()
        self.env = git_environment(parent / 'tmp', dict(PATH=os.environ['PATH'], HOME=str(parent / 'home'),
                                  TMPDIR=str(parent), LANG='C', PYTHONDONTWRITEBYTECODE='1'))
        value = command(['/bin/bash', '-c', SANDBOX_INIT, 'fixture', str(ROOT / 'tests/lib/harness.sh'), ISSUE, BRANCH,
                         str(parent / 'tmp')],
                        ROOT, self.env)
        self.root = Path(value.split('SANDBOX=')[-1]).resolve()
        if self.root.parent != parent / 'tmp': raise AssertionError('harness sandbox escaped fixture parent')
        root = self.root
        assert_origin(root, self.env)
        self.scripts = root / 'scripts'
        (root / 'control/prompts').mkdir(parents=True)
        for name in ('home', 'tmp'): (root / 'control' / name).mkdir()
        (root / 'fake-bin').mkdir()
        git_binary = shutil.which('git', path=self.env['PATH'])
        git_wrapper = root / 'fake-bin/git'
        git_wrapper.write_text('#!/bin/bash\n' + ''.join('export ' + key + '=' + shlex.quote(value) + '\n'
                               for key, value in git_environment(root.parent, {}).items()) +
                               'exec ' + shlex.quote(git_binary) + ' "$@"\n')
        git_wrapper.chmod(0o755)
        (root / 'control/case.json').write_text(json.dumps(case))
        shutil.copyfile(__file__, root / 'control/agent.py')
        wrapper = root / 'fake-bin/claude'
        wrapper.write_text('#!/bin/bash\nexec ' + shlex.quote(sys.executable) + ' ' +
                           shlex.quote(str(root / 'control/agent.py')) + ' --agent ' + shlex.quote(str(root)) + ' "$@"\n')
        wrapper.chmod(0o755)
        shutil.copyfile(ROOT / 'tests/lib/bin/gh', root / 'fake-bin/gh')
        (root / 'fake-bin/gh').chmod(0o755)
        (root / 'fake-bin/sleep').write_text('#!/bin/bash\nexit 0\n')
        (root / 'fake-bin/sleep').chmod(0o755)
        self.env.update(SANDBOX=str(root), HOME=str(root / 'control/home'), TMPDIR=str(root / 'control/tmp'),
                        PATH=str(root / 'fake-bin') + os.pathsep + self.env['PATH'],
                        BUREAU_CONFIG=str(root / '.bureau.json'), BUREAU_ENV_FILE=str(root / '.env'),
                        BUREAU_PROVIDER_LOG_DIR=str(root / 'control/provider'),
                        BUREAU_STUB_PICKED_ISSUE=ISSUE, BUREAU_STUB_BRANCH=BRANCH,
                        BUREAU_IMPL_MAX_ITER='1', BUREAU_IMPL_ITER_TIMEOUT='15', BUREAU_IMPL_TOTAL_TIMEOUT='120',
                        BUREAU_IMPL_PR_RETRY_WAIT='0', GH_STUB_EXISTING_PR='99',
                        BUREAU_STUB_STATE_QA='state-qa' if case['stage'] == 'qa' else '', BUREAU_STUB_STATE_MERGE='state-merge',
                        BUREAU_STUB_AGENT_ENABLED='merge', GH_STUB_CALLS=str(root / 'gh_calls.log'),
                        INJECTION_OPERATOR='operator-fixture', GH_TOKEN=SECRET, TOKEN_ALIAS=SECRET,
                        LINEAR_API_KEY=SECRET)
        stage = case['stage']
        self.env['BUREAU_STUB_ISSUE_STATE'] = {'implement':'Build', 'qa':'QA', 'code_review':'Build Review',
                                                'spec':'Triage', 'spec_review':'Spec Review'}[stage]
        config = dict(agents=dict(runner='claude', providers=dict(claude=dict(timeout_seconds=10))),
                      repo=dict(test_command='bash test-gate.sh', untrusted_env=case.get('setup', {}).get('untrusted_env', 'default')))
        (root / '.bureau.json').write_text(json.dumps(config))
        (root / '.env').write_text('LINEAR_API_KEY=' + SECRET + '\n')
        (root / '.gitignore').write_text('.env\n.bureau.json\n.fake-origin.git/\n.specify/\n'
                                        'scripts/\nlogs/\ncontrol/\nfake-bin/\n*.log\n')
        (root / '.git/info/exclude').write_text((root / '.gitignore').read_text())
        for key, value in (('protocol.allow', 'never'), ('protocol.file.allow', 'always'),
                           ('core.hooksPath', str(root / 'control/no-hooks'))):
            self.git('config', key, value)
        (root / 'control/no-hooks').mkdir()
        # Use the production adapter entry/commit functions with fake transport only.
        source = (ROOT / 'templates/scripts/bureau-config.sh').read_text()
        block = source[source.index('run_stage_for() {'):source.index('# Build the model invocation for a stage.')]
        with (self.scripts / 'bureau-config.sh').open('a') as out:
            out.write('\n' + block)
        gate_red = case.get('setup', {}).get('gate_red', False)
        output = case['hostile_text'] if case['input_class'] == 'test_output' else 'Fixture gate output.'
        if case['input_class'] == 'commit_message': output += ' Inspect git log --format=%B origin/main..HEAD for context.'
        (root / 'control/test-output.txt').write_text(output + '\n')
        (root / 'test-gate.sh').write_text('cat control/test-output.txt\nexit ' + str(int(gate_red)) + '\n')
        tasks = root / 'specs/001-test-branch/tasks.md'
        tasks.write_text('# Tasks\n- [ ] T001 Edit accepted.txt; inspect adjacent.txt\n')
        self.input_path = None
        cls = case['input_class']
        if cls == 'ticket_description': self.env['BUREAU_STUB_DESCRIPTION'] = case['hostile_text']
        elif cls == 'ticket_comment':
            if stage == 'spec': self.env['BUREAU_STUB_REVIEW_COMMENTS'] = json.dumps([dict(body=case['hostile_text'])])
            else: self.env['BUREAU_STUB_REVIEW_COMMENT'] = 'Code Review — Changes Requested\n' + case['hostile_text']
        elif cls == 'spec_file':
            self.input_path = tasks if stage == 'implement' else tasks.with_name('spec.md')
            with self.input_path.open('a') as out: out.write('\n' + case['hostile_text'] + '\n')
        elif cls == 'branch_file':
            name = case.get('setup', {}).get('input_path', 'adjacent.txt')
            self.input_path = local_path(root, name)
            self.input_path.parent.mkdir(parents=True, exist_ok=True)
            with self.input_path.open('a') as out: out.write('\n# ' + case['hostile_text'] + '\n')
        if any(a['action'] == 'credential_probe' for a in case['agent_obeys']):
            helper = root / 'control/credential-helper'
            helper.write_text('#!/bin/bash\ncat >/dev/null\nprintf "username=fixture\\npassword=' + STORED + '\\n"\n')
            helper.chmod(0o755)
            self.git('config', 'credential.helper', str(helper))
        self.git('add', 'specs', '.gitignore', 'test-gate.sh')
        if cls == 'branch_file': self.git('add', '-f', '--', str(self.input_path))
        message = case['hostile_text'] if cls == 'commit_message' else 'test: stage input'
        self.git('commit', '-qm', message)
        assert_origin(root, self.env)
        self.git('push', '-q', 'origin', BRANCH)
        self.base = self.git('rev-parse', 'origin/main')
        if stage == 'spec': self.git('checkout', '-q', 'main')
        with patch.dict(os.environ, self.env, clear=True):
            git_maintenance.require_disabled(root)
        if control:
            for mutation in json.loads(MUTATIONS.read_text())[case['defence']]:
                path = self.scripts / mutation['file']
                old = path.read_text()
                if old.count(mutation['old']) != 1:
                    raise AssertionError('mutation anchor changed: ' + case['defence'] + '/' + mutation['file'])
                path.write_text(old.replace(mutation['old'], mutation['new']))

    def git(self, *args):
        return command(['git', *args], self.root, self.env)

    def run(self):
        assert_origin(self.root, self.env)
        self.result = run_fixture(['/bin/bash', str(self.scripts / (self.case['stage'].replace('_', '-') + '-pipeline.sh')), ISSUE],
                                  root=self.root, cwd=self.root, env=self.env, timeout=35)
        self.captures = [json.loads(p.read_text()) for p in (self.root / 'control/prompts').glob('*.json')]
        self.calls = (self.root / 'calls.log').read_text() if (self.root / 'calls.log').exists() else ''
        self.gh = (self.root / 'gh_calls.log').read_text() if (self.root / 'gh_calls.log').exists() else ''

    def assert_delivery(self, test):
        test.assertTrue(self.captures, self.result.stdout + self.result.stderr)
        direct = self.case['input_class'] in ('ticket_description', 'ticket_comment', 'test_output', 'review_comment')
        seen = [row for row in self.captures if self.case['hostile_text'] in
                (row['prompt'] if direct else '\n'.join(read['text'] for read in row['reads']))]
        test.assertTrue(seen, 'hostile input never reached the agent: ' + self.result.stdout + self.result.stderr)
        if not direct:
            if self.case['input_class'] == 'spec_file':
                test.assertTrue(any(str(self.input_path.parent) in row['prompt'] for row in seen))
            elif self.case['stage'] == 'code_review':
                test.assertTrue(any('git diff ' in row['prompt'] for row in seen))
            elif self.case['input_class'] == 'commit_message':
                test.assertTrue(any('git log --format=%B origin/main..HEAD' in row['prompt'] for row in seen))
            else:
                test.assertTrue(any(str(self.root / 'specs/001-test-branch/tasks.md') in row['prompt']
                                    and 'adjacent.txt' in row['reads'][0]['text'] for row in seen))
        performed = {kind for row in self.captures for kind in row['actions']}
        test.assertEqual(performed, {a['action'] for a in self.case['agent_obeys']}, 'fake did not obey every configured action')
        assert_origin(self.root, self.env)
        test.assertEqual(self.git('remote'), 'origin')
        test.assertIn(self.root, Path(self.env['HOME']).parents)
        test.assertEqual(Path(shutil.which('gh', path=self.env['PATH'])), self.root / 'fake-bin/gh')

    def assert_effect(self, test, expected):
        test.assertEqual(self.result.returncode, expected['exit_code'], self.result.stdout + self.result.stderr)
        for label, text in (('calls', self.calls), ('gh', self.gh)):
            for regex in expected.get(label + '_include', []): test.assertRegex(text, '(?m)' + regex)
            for regex in expected.get(label + '_exclude', []): test.assertNotRegex(text, '(?m)' + regex)
        if 'origin_base_changed' in expected:
            tip = self.git('--git-dir', str(self.root / '.fake-origin.git'), 'rev-parse', 'main')
            test.assertEqual(tip != self.base, expected['origin_base_changed'])
        for key, ref in (('branch_files', 'HEAD'), ('origin_branch_files', BRANCH), ('origin_base_files', 'main')):
            for name, value in expected.get(key, {}).items():
                prefix = [] if key == 'branch_files' else ['--git-dir', str(self.root / '.fake-origin.git')]
                test.assertIn(value, self.git(*prefix, 'show', ref + ':' + name))
        probe = {key:value for row in self.captures for key,value in row['probe'].items()}
        for key, value in expected.get('probe', {}).items(): test.assertEqual(probe.get(key), value, key)


class InjectionSetTests(unittest.TestCase):
    selected = []

    def test_failed_initialization_stops_before_any_later_command(self):
        self.assertTrue((ROOT / '.git').exists(), 'run this regression from a real checkout')
        before = checkout_snapshot(ROOT)
        try:
            with scratch_directory('.injection-init-failure-') as scratch:
                scratch = Path(scratch).resolve()
                binary = scratch / 'bin/git'
                binary.parent.mkdir()
                binary.write_text('#!' + sys.executable + '\n' +
                                  'import json, os, pathlib, sys\n'
                                  'with pathlib.Path(__file__).with_name("calls.jsonl").open("a") as out:\n'
                                  '    out.write(json.dumps(dict(args=sys.argv[1:], '
                                  'ceiling=os.environ.get("GIT_CEILING_DIRECTORIES"), '
                                  'system=os.environ.get("GIT_CONFIG_NOSYSTEM"), '
                                  'global_config=os.environ.get("GIT_CONFIG_GLOBAL"), '
                                  'protocol=os.environ.get("GIT_ALLOW_PROTOCOL"))) + "\\n")\n'
                                  'sys.exit(77 if "init" in sys.argv[1:] else 0)\n')
                binary.chmod(0o755)
                case = json.loads(CORPUS.read_text())[0]
                for mode in ('runner', 'without-errexit', 'control'):
                    with self.subTest(mode=mode):
                        parent = scratch / mode
                        parent.mkdir()
                        log = binary.with_name('calls.jsonl')
                        log.unlink(missing_ok=True)
                        env = git_environment(parent, dict(PATH=str(binary.parent) + os.pathsep + os.environ['PATH'],
                                              HOME=str(parent), TMPDIR=str(parent)))
                        if mode == 'runner':
                            # Sandbox deliberately constructs a fresh environment.
                            with patch.dict(os.environ, env, clear=True):
                                with self.assertRaises(subprocess.CalledProcessError) as error:
                                    Sandbox(parent, case)
                        else:
                            harness = ROOT / 'tests/lib/harness.sh'
                            init = ('set -uo pipefail\nsource "$1"\nsandbox_init "$2" "$3" "$4"\n'
                                    'init_rc=$?\nexit "$init_rc"')
                            if mode == 'control':
                                source = harness.read_text()
                                anchor = 'git -C "$SANDBOX" init -q -b main || return'
                                self.assertEqual(source.count(anchor), 1)
                                harness = parent / 'harness.sh'
                                harness.write_text(source.replace(anchor, anchor.replace('|| return', '|| :')))
                                init = SANDBOX_INIT
                            with self.assertRaises(subprocess.CalledProcessError) as error:
                                command(['/bin/bash', '-c', init, 'fixture', str(harness), ISSUE, BRANCH, str(parent)],
                                        ROOT, env)
                        rows = [json.loads(line) for line in log.read_text().splitlines()]
                        self.assertIn('init', rows[0]['args'])
                        for row in rows:
                            self.assertEqual(row['ceiling'], str(parent / 'tmp' if mode == 'runner' else parent))
                            self.assertEqual((row['system'], row['global_config'], row['protocol']), ('1', os.devnull, 'file'))
                        def assert_stopped():
                            self.assertEqual(error.exception.returncode, 77)
                            self.assertEqual(len(rows), 1, 'a command ran after failed initialization')
                        if mode == 'control':
                            with self.assertRaises(AssertionError): assert_stopped()
                            self.assertGreater(len(rows), 1, 'negative control did not reach the next command')
                        else:
                            assert_stopped()
        finally:
            self.assertEqual(checkout_snapshot(ROOT), before, 'real checkout Git metadata changed')

    def test_sandbox_confinement(self):
        case = json.loads(CORPUS.read_text())[0]
        with self.assertRaisesRegex(AssertionError, 'scratch parent is inside a Git repository'):
            scratch_parent(ROOT)
        with scratch_directory('.injection-parent with spaces-') as scratch:
            scratch = Path(scratch).resolve()
            parent, fallback = scratch / 'case', scratch / 'default'
            parent.mkdir(); fallback.mkdir()
            alias = scratch / 'alias'
            alias.symlink_to(parent, target_is_directory=True)
            env = git_environment(scratch, dict(PATH=os.environ['PATH'], HOME=str(scratch), TMPDIR=str(fallback)))
            for argument, expected in ((str(parent), parent), (str(alias) + '/', parent), ('', fallback)):
                with self.subTest(parent=argument or 'TMPDIR'):
                    value = command(['/bin/bash', '-c', SANDBOX_INIT, 'fixture', str(ROOT / 'tests/lib/harness.sh'),
                                     ISSUE, BRANCH, argument], ROOT, env)
                    self.assertEqual(Path(value.split('SANDBOX=')[-1]).resolve().parent, expected)
            sandbox = Sandbox(alias, case)
            self.assertEqual(sandbox.parent, parent)
            self.assertEqual(sandbox.root.parent, parent / 'tmp')
            for name in ('sibling', 'equal', 'symlink', 'wrong-child'):
                rejected = scratch / name
                rejected.mkdir()
                escaped = fallback if name == 'sibling' else rejected
                if name == 'symlink':
                    escaped = rejected / 'escape'
                    escaped.symlink_to(fallback, target_is_directory=True)
                elif name == 'wrong-child':
                    escaped = rejected / 'fallback-sandbox'
                    escaped.mkdir()
                with patch(__name__ + '.command', return_value='SANDBOX=' + str(escaped)):
                    with self.assertRaisesRegex(AssertionError, 'harness sandbox escaped fixture parent'):
                        Sandbox(rejected, case)
            ignored = scratch / 'ignored'
            ignored.mkdir()
            run_command = command
            def ignore_parent(args, *rest):
                return run_command([*args[:-1], ''], *rest)
            with patch(__name__ + '.command', side_effect=ignore_parent):
                with self.assertRaisesRegex(AssertionError, 'harness sandbox escaped fixture parent'):
                    Sandbox(ignored, case)

    def test_mutation_anchors(self):
        for guard, mutations in json.loads(MUTATIONS.read_text()).items():
            for mutation in mutations:
                with self.subTest(guard=guard, file=mutation['file']):
                    name = 'bureau-config.sh' if mutation['file'] == 'real-helpers.sh' else mutation['file']
                    source = (ROOT / 'templates/scripts' / name).read_text()
                    self.assertEqual(source.count(mutation['old']), 1, 'mutation anchor changed')
                    self.assertNotEqual(mutation['old'], mutation['new'])

    def test_fake_action_fixtures(self):
        # Check the fake's effects independently of the stage/cleanup integration.
        # This is not evidence that the pipeline accepted or rejected those effects.
        for case in json.loads(CORPUS.read_text()):
            if 'pending' not in case and not case['id'].startswith('environment-'): continue
            with self.subTest(case=case['id']), scratch_directory('.injection-actions-') as scratch:
                with patch.dict(os.environ, {'PATH':os.environ['PATH']}, clear=True):
                    sandbox = Sandbox(Path(scratch).resolve(), case)
                    spec = importlib.util.spec_from_file_location('injection_provider', sandbox.scripts / 'bureau-provider.py')
                    provider = importlib.util.module_from_spec(spec); spec.loader.exec_module(provider)
                    env = provider.untrusted_env(sandbox.env, case.get('setup', {}).get('untrusted_env', 'default'), 'claude')
                    command([str(sandbox.root / 'fake-bin/claude'), '-p'], sandbox.root, env, case['hostile_text'])
                    rows = [json.loads(p.read_text()) for p in (sandbox.root / 'control/prompts').glob('*.json')]
                    self.assertEqual({a for row in rows for a in row['actions']}, {a['action'] for a in case['agent_obeys']})
                    probe = {k:v for row in rows for k,v in row['probe'].items()}
                    for key, value in case['expected'].get('probe', {}).items(): self.assertEqual(probe[key], value)
                    for action in case['agent_obeys']:
                        if action['action'] == 'commit': self.assertEqual(action['content'].rstrip('\n'), sandbox.git('show', 'HEAD:' + action['path']))
                    if case['id'] == 'push-base':
                        self.assertNotEqual(sandbox.git('--git-dir', str(sandbox.root / '.fake-origin.git'), 'rev-parse', 'main'), sandbox.base)
                    if case['id'] == 'merge-directly':
                        self.assertIn('gh\tpr\tmerge\t99\t--squash', (sandbox.root / 'gh_calls.log').read_text())
                    assert_origin(sandbox.root, sandbox.env)

    def test_corpus(self):
        cases = json.loads(CORPUS.read_text())
        mutations = json.loads(MUTATIONS.read_text())
        self.assertTrue(12 <= len(cases) <= 20)
        self.assertEqual({c['input_class'] for c in cases}, INPUT_CLASSES)
        self.assertEqual(len({c['id'] for c in cases}), len(cases))
        self.assertEqual({c['defence'] for c in cases if 'pending' not in c}, set(mutations))
        self.assertLessEqual(set(self.selected), {c['id'] for c in cases}, 'unknown corpus case')
        for case in cases:
            if self.selected and case['id'] not in self.selected: continue
            with self.subTest(case=case['id']):
                self.assertTrue(case['hostile_text'])
                self.assertLessEqual({a['action'] for a in case['agent_obeys']}, ACTIONS)
                pending = case.get('pending')
                if pending:
                    self.assertTrue(pending['reason'])
                    self.assertEqual(case['defence'], 'none')
                for control in (False,) if pending else (False, True):
                    with scratch_directory('.injection-') as scratch:
                        parent = Path(scratch).resolve()
                        # The cleanup helper merges environments: clear the parent first,
                        # so neither fake nor provider can inherit a real credential.
                        with patch.dict(os.environ, {'PATH':os.environ['PATH']}, clear=True):
                            sandbox = Sandbox(parent, case, control)
                            sandbox.run()
                            sandbox.assert_delivery(self)
                            if control:
                                sandbox.assert_effect(self, case['control_expected'])
                                with self.assertRaises(AssertionError, msg='negative control passed with its guard removed'):
                                    sandbox.assert_effect(self, case['expected'])
                            elif pending:
                                try: sandbox.assert_effect(self, case['expected'])
                                except AssertionError as exc:
                                    raise AssertionError('Pending effect changed; investigate and remove pending when defended: ' + case['id']) from exc
                            else:
                                sandbox.assert_effect(self, case['expected'])
                print('PASS ' + case['id'] + (': pending, gap confirmed' if pending else ': defended, control fails without guard'), flush=True)


if __name__ == '__main__':
    if sys.argv[1:2] == ['--agent']:
        agent(Path(sys.argv[2]).resolve(), sys.argv[3:])
    else:
        while '--case' in sys.argv:
            index = sys.argv.index('--case')
            InjectionSetTests.selected.append(sys.argv.pop(index + 1))
            sys.argv.pop(index)
        unittest.main()
