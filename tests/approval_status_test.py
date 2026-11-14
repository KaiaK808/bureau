"""Exact-head approval decisions, installation and diagnostics, without network."""
import ast
import copy
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import git_maintenance

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'templates/scripts/bureau-approval-status.py'
WORKFLOW = ROOT / 'templates/.github/workflows/bureau-approval.yml'
INSTALLER = ROOT / 'scripts/bureau_install.py'
FIXTURES = ROOT / 'tests/fixtures/approval-status'
HEAD = 'a' * 40
NEW_HEAD = 'b' * 40
CONTEXT = 'bureau/human-approval'
BASE = dict(version=2, linear={'teams': [{'id': 'team', 'key': 'TEAM', 'states': {'merge': 'merge'}}]},
            agents={'runner': 'claude', 'merge_approvers': ['merge-person']}, repo={})


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    value = importlib.util.module_from_spec(spec); spec.loader.exec_module(value)
    return value


def workflow_yaml(text):
    """Parse the template's restricted YAML subset, with no optional YAML dependency.

    Only block maps, sequences of maps, plain/JSON scalars and scalar flow lists are
    accepted. Reject unsupported syntax rather than guessing what GitHub would read.
    """
    tokens = []
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith('#'): continue
        assert '\t' not in line and not any(ord(c) < 32 for c in line)
        indent = len(line) - len(line.lstrip(' ')); value = line[indent:]
        assert indent % 2 == 0
        if value.startswith('- '): tokens.extend(((indent, '-'), (indent + 2, value[2:])))
        else: tokens.append((indent, value))

    def scalar(value):
        if value.startswith('['):
            assert value.endswith(']')
            return [scalar(part.strip()) for part in value[1:-1].split(',')]
        if value.startswith('"'): return json.loads(value)
        assert value and value[0] not in "-?:,[]{}#&*!|>'%@`"
        assert not re.search(r':\s|\s#', value) and not value.endswith(':')
        if value in ('true', 'false'): return value == 'true'
        if value.isdigit(): return int(value)
        return value

    def block(index, indent):
        sequence = tokens[index] == (indent, '-')
        result = [] if sequence else {}
        while index < len(tokens) and tokens[index][0] >= indent:
            depth, value = tokens[index]; assert depth == indent
            index += 1
            if sequence:
                assert value == '-'
                child, index = block(index, indent + 2); result.append(child)
            else:
                key, sep, value = value.partition(':')
                assert sep and re.fullmatch(r'[A-Za-z_][A-Za-z0-9_-]*', key) and key not in result
                if value.strip(): result[key] = scalar(value.strip())
                else:
                    assert index < len(tokens) and tokens[index][0] == indent + 2
                    result[key], index = block(index, indent + 2)
        return result, index

    result, end = block(0, 0); assert end == len(tokens)
    return result


class Fixture(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='bureau approval ', dir=ROOT)
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.bin = self.root / 'bin'; self.bin.mkdir()
        shutil.copyfile(FIXTURES / 'gh', self.bin / 'gh'); (self.bin / 'gh').chmod(0o755)
        self.env = {**os.environ, **git_maintenance.OFF_ENV, 'PATH': str(self.bin) + os.pathsep + os.environ['PATH'],
                    'APPROVAL_FIXTURE': str(self.root), 'PYTHONDONTWRITEBYTECODE': '1',
                    'GIT_CONFIG_GLOBAL': os.devnull, 'GIT_CONFIG_NOSYSTEM': '1', 'GH_TOKEN': 'approval-test-secret'}
        for name in ('BUREAU_CONFIG', 'BUREAU_ENV_FILE', 'BASH_ENV', 'ENV', 'GIT_DIR', 'GIT_WORK_TREE', 'GIT_INDEX_FILE', 'GIT_COMMON_DIR'):
            self.env.pop(name, None)
        self.state = dict(repository={'default_branch': 'stable'}, default_config=copy.deepcopy(BASE),
                          head_config={'agents': {'merge_approvers': ['branch-author']}},
                          pull_request={'number': 42, 'state': 'open', 'merged': False, 'head': {'sha': HEAD}},
                          login='runtime-agent')

    def fixture(self, name):
        return json.loads((FIXTURES / (name + '.json')).read_text())

    def save(self, reset=True):
        (self.root / 'answers.json').write_text(json.dumps(self.state))
        if reset:
            for name in ('calls.jsonl', 'posted-statuses.json'):
                (self.root / name).unlink(missing_ok=True)

    def calls(self):
        self.assertFalse((self.root / 'unknown.json').exists(), 'gh double received an unknown route')
        path = self.root / 'calls.jsonl'
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def writes(self):
        return [call for call in self.calls() if call['method'] == 'POST']

    def run_event(self, event='issue_comment', payload=None, code=0, reset=True):
        self.save(reset)
        if payload is None: payload = self.fixture('comment')
        path = self.root / 'event.json'; path.write_text(json.dumps(payload))
        proc = subprocess.run([sys.executable, str(SCRIPT), '--event-name', event, '--event-path', str(path),
                               '--repo', 'example/project'], env=self.env, cwd=self.root, capture_output=True,
                              text=True, timeout=10)
        self.assertEqual(proc.returncode, code, proc.stdout + proc.stderr)
        self.assertEqual(len(proc.stdout.splitlines()), 1, proc.stdout)
        self.assertEqual(proc.stderr, '')
        self.assertNotIn('approval-test-secret', proc.stdout)
        self.calls()
        return proc.stdout

    def assert_status(self, state, head=HEAD, url=None, description=None):
        fields = dict(context=CONTEXT, state=state)
        if url is not None: fields['target_url'] = url
        if description is not None: fields['description'] = description
        writes = self.writes(); self.assertEqual(len(writes), 1, writes)
        self.assertEqual(writes[0]['route'], 'repos/example/project/statuses/' + head)
        for key, value in fields.items(): self.assertEqual(writes[0]['fields'][key], value)
        self.assertEqual(writes[0]['query'], None)

    def git_repo(self, name='target repo'):
        repo = self.root / name
        subprocess.run(['git', 'init', '-q', str(repo)], check=True, env=self.env, capture_output=True, timeout=5)
        git_maintenance.disable(repo)
        return repo

    def install(self, repo, *args):
        proc = subprocess.run([sys.executable, str(INSTALLER), 'assets', '--repo', str(repo), *args],
                              env=self.env, capture_output=True, text=True, timeout=20)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        return json.loads(proc.stdout)


class ApprovalDecisions(Fixture):
    def test_command_writes_success_on_exact_head(self):
        payload = self.fixture('comment'); self.run_event(payload=payload)
        self.assert_status('success', url=payload['comment']['html_url'], description='approved by merge-person')
        self.assertEqual([c['route'] for c in self.calls()], ['repos/example/project',
                         'repos/example/project/contents/.bureau.json', 'repos/example/project/pulls/42',
                         'repos/example/project/statuses/' + HEAD])

    def test_old_command_fails_and_names_both_short_shas(self):
        payload = self.fixture('comment'); payload['comment']['body'] = '/bureau approve ' + NEW_HEAD
        out = self.run_event(payload=payload, code=1)
        self.assertIn(HEAD[:7], out); self.assertIn(NEW_HEAD[:7], out)
        self.assertEqual(self.writes(), [])

    def test_unlisted_command_is_refused(self):
        payload = self.fixture('comment')
        payload['comment']['user']['login'] = payload['sender']['login'] = 'branch-author'
        self.run_event(payload=payload, code=1)
        self.assertEqual(self.writes(), [])

    def test_outsider_command_leaves_existing_approval_unchanged(self):
        self.run_event()
        payload = self.fixture('comment')
        payload['comment']['user']['login'] = payload['sender']['login'] = 'branch-author'
        self.run_event(payload=payload, code=1, reset=False)
        self.assert_status('success')
        self.assertEqual(json.loads((self.root / 'posted-statuses.json').read_text()),
                         {HEAD: [self.writes()[0]['fields']]})

    def test_stale_command_leaves_existing_approval_unchanged(self):
        self.run_event()
        payload = self.fixture('comment'); payload['comment']['body'] = '/bureau approve ' + NEW_HEAD
        self.run_event(payload=payload, code=1, reset=False)
        self.assert_status('success')
        self.assertEqual(json.loads((self.root / 'posted-statuses.json').read_text()),
                         {HEAD: [self.writes()[0]['fields']]})

    def test_full_sha_command_accepts_surrounding_spaces_and_later_lines(self):
        payload = self.fixture('comment')
        for body in ('/bureau approve ' + HEAD, '  /bureau approve ' + HEAD.upper() + ' \t\nFurther explanation approval-test-secret',
                     '/bureau approve ' + HEAD + '\r\nFurther explanation'):
            with self.subTest(body=body):
                payload['comment']['body'] = body; self.run_event('issue_comment', payload)
                self.assert_status('success', url=payload['comment']['html_url'], description='approved by merge-person')

    def test_command_ignores_short_sha_extra_words_edits_and_non_pr_issues(self):
        for body in ('', '/bureau approve ' + HEAD[:7], '/bureau approve ' + HEAD + ' please',
                     'Please /bureau approve ' + HEAD, 'Explanation\n/bureau approve ' + HEAD,
                     '/bureau approve ' + HEAD + '\vextra words', '/bureau approve ' + HEAD + '\u2028extra words',
                     '/bureau approve ' + 'g' * 40):
            with self.subTest(body=body):
                payload = self.fixture('comment'); payload['comment']['body'] = body
                self.run_event('issue_comment', payload); self.assertEqual(self.writes(), [])
        payload = self.fixture('comment'); payload['action'] = 'edited'
        self.run_event('issue_comment', payload); self.assertEqual(self.writes(), [])
        payload = self.fixture('comment'); del payload['issue']['pull_request']
        self.run_event('issue_comment', payload); self.assertEqual(self.writes(), [])

    def test_bots_and_different_senders_never_approve(self):
        for mutation in ('author-bot', 'sender-bot', 'bot-suffix', 'different-sender'):
            with self.subTest(mutation=mutation):
                payload = self.fixture('comment')
                if mutation == 'author-bot': payload['comment']['user']['type'] = 'Bot'
                elif mutation == 'sender-bot': payload['sender']['type'] = 'Bot'
                elif mutation == 'bot-suffix':
                    payload['comment']['user']['login'] = payload['sender']['login'] = 'merge-person[bot]'
                else: payload['sender']['login'] = 'other-person'
                self.run_event(payload=payload, code=1); self.assertEqual(self.writes(), [])

    def test_closed_and_merged_prs_change_nothing(self):
        for state, merged in (('closed', False), ('closed', True), ('open', True)):
            with self.subTest(state=state, merged=merged):
                self.state['pull_request'].update(state=state, merged=merged)
                self.run_event(); self.assertEqual(self.writes(), [])

    def test_command_must_match_fresh_head(self):
        self.state['pull_request']['head']['sha'] = NEW_HEAD
        self.run_event(code=1)
        self.assertEqual(self.writes(), [])

    def test_policy_comes_from_default_branch_not_checkout_or_pr_head(self):
        (self.root / '.bureau.json').write_text(json.dumps(self.state['head_config']))
        payload = self.fixture('comment'); payload['sender']['login'] = payload['comment']['user']['login'] = 'branch-author'
        self.run_event(payload=payload, code=1); self.assertEqual(self.writes(), [])
        reads = [c for c in self.calls() if c['route'].endswith('/contents/.bureau.json')]
        self.assertEqual(len(reads), 1); self.assertEqual(reads[0]['method'], 'GET')
        self.assertEqual(reads[0]['fields'], {'ref': 'stable'})

    def test_login_comparison_is_case_insensitive(self):
        self.state['default_config']['agents']['merge_approvers'] = ['MERGE-PERSON']
        payload = self.fixture('comment'); payload['sender']['login'] = 'Merge-Person'
        self.run_event(payload=payload); self.assert_status('success')

    def test_missing_or_empty_setting_writes_nothing_for_any_event(self):
        for config in ({}, {'agents': {}}, {'agents': {'merge_approvers': []}}):
            self.state['default_config'] = config
            for event in ('issue_comment', 'other'):
                with self.subTest(config=config, event=event):
                    self.run_event(event); self.assertEqual(self.writes(), [])
                    self.assertEqual(len(self.calls()), 2)

    def test_invalid_settings_refuse_every_event_without_writing(self):
        invalid = (None, 'merge-person', {}, [1], [''], ['has space'], ['has_underscore'], ['-leading'],
                   ['trailing-'], ['two--hyphens'], ['x' * 40], ['person[bot]'], ['café'], ['person'] * 21)
        for value in invalid:
            self.state['default_config']['agents']['merge_approvers'] = value
            for event in ('issue_comment', 'other'):
                with self.subTest(value=value, event=event):
                    self.run_event(event, code=2); self.assertEqual(self.writes(), [])

    def test_missing_required_fields_never_approve(self):
        cases = (('issue_comment', 'comment', ('action',)), ('issue_comment', 'comment', ('issue', 'number')),
                 ('issue_comment', 'comment', ('comment',)), ('issue_comment', 'comment', ('sender',)),
                 ('issue_comment', 'comment', ('comment', 'user', 'type')), ('issue_comment', 'comment', ('comment', 'user', 'login')),
                 ('issue_comment', 'comment', ('comment', 'body')), ('issue_comment', 'comment', ('comment', 'html_url')),
                 ('issue_comment', 'comment', ('comment', 'user')), ('issue_comment', 'comment', ('sender', 'type')))
        for event, name, keys in cases:
            with self.subTest(event=event, keys=keys):
                payload = self.fixture(name); parent = payload
                for key in keys[:-1]: parent = parent[key]
                del parent[keys[-1]]
                self.run_event(event, payload, code=2); self.assertEqual(self.writes(), [])
        for key in ('head', 'state', 'merged'):
            current = self.state['pull_request'].pop(key)
            self.run_event(code=2); self.assertEqual(self.writes(), [])
            self.state['pull_request'][key] = current

    def test_non_comment_events_do_nothing(self):
        payload = dict(action='synchronize', pull_request={'number': 42, 'head': {'sha': HEAD}})
        for event in ('pull_request', 'pull_request_review', 'pull_request_target', 'other'):
            with self.subTest(event=event):
                self.run_event(event, payload); self.assertEqual(self.writes(), [])
                self.assertEqual([c['route'] for c in self.calls()],
                                 ['repos/example/project', 'repos/example/project/contents/.bureau.json'])

    def test_failed_api_calls_and_unreadable_responses_never_report_success(self):
        for route in ('repos/example/project', 'repos/example/project/contents/.bureau.json',
                      'repos/example/project/pulls/42', 'repos/example/project/statuses/' + HEAD):
            with self.subTest(route=route):
                self.state['fail_route'] = route
                out = self.run_event(code=2)
                self.assertIn('GitHub API call failed', out); self.assertNotIn('approved:', out)
                if '/statuses/' not in route: self.assertEqual(self.writes(), [])
        del self.state['fail_route']
        for response in ('not JSON', '{}', '{"encoding":"base64","content":"invalid!"}',
                         '{"encoding":"base64","content":"bm90IEpTT04="}'):
            self.state['responses'] = {'repos/example/project/contents/.bureau.json': response}
            self.run_event(code=2); self.assertEqual(self.writes(), [])

    def test_usage_and_unreadable_input_have_one_safe_line(self):
        self.save()
        for args in ([], ['--event-name', 'issue_comment', '--event-path', str(self.root / 'missing'), '--repo', 'example/project']):
            proc = subprocess.run([sys.executable, str(SCRIPT), *args], env=self.env, capture_output=True, text=True, timeout=5)
            self.assertEqual(proc.returncode, 2); self.assertEqual(len(proc.stdout.splitlines()), 1)
            self.assertEqual(proc.stderr, ''); self.assertEqual(self.writes(), [])
        path = self.root / 'event.json'
        for data in ('invalid JSON', '[]'):
            path.write_text(data)
            proc = subprocess.run([sys.executable, str(SCRIPT), '--event-name', 'issue_comment',
                                   '--event-path', str(path), '--repo', 'example/project'], env=self.env,
                                  capture_output=True, text=True, timeout=5)
            self.assertEqual(proc.returncode, 2); self.assertEqual(self.writes(), [])

    def test_transport_errors_are_fixed_messages_and_calls_are_bounded(self):
        approval = load('approval_status', SCRIPT)
        for error in (FileNotFoundError('approval-test-secret'), subprocess.TimeoutExpired('gh', 30),
                      UnicodeDecodeError('utf-8', b'\xff', 0, 1, 'approval-test-secret')):
            with patch.object(approval.subprocess, 'run', side_effect=error) as run:
                with self.assertRaisesRegex(ValueError, '^GitHub API call failed$'):
                    approval.api('example/project')
                self.assertEqual(run.call_args.kwargs['timeout'], 30)


class ApprovalIntegration(Fixture):
    def test_scope_installs_exactly_workflow_and_preserves_existing_scopes(self):
        repo = self.git_repo(); preview = self.install(repo, '--scope', 'approval')
        self.assertEqual(preview['files'], [{'path': '.github/workflows/bureau-approval.yml', 'action': 'install'}])
        self.assertFalse((repo / '.github').exists())
        self.install(repo, '--scope', 'approval', '--apply')
        files = {str(p.relative_to(repo)) for p in repo.rglob('*') if p.is_file() and '.git' not in p.relative_to(repo).parts}
        self.assertEqual(files, {'.bureau-install.json', '.gitignore', '.github/workflows/bureau-approval.yml'})
        self.assertEqual((repo / '.gitignore').read_text().split(), ['.bureau-install.json'])
        self.assertEqual((repo / '.github/workflows/bureau-approval.yml').read_bytes(), WORKFLOW.read_bytes())
        manifest = json.loads((repo / '.bureau-install.json').read_text())
        self.assertEqual(set(manifest['files']), {'.github/workflows/bureau-approval.yml'})
        self.assertEqual(set(manifest['sources']), {'approval'})
        for scope in (None, 'interfaces', 'scripts', 'workflows', 'ci'):
            with self.subTest(scope=scope):
                target = self.git_repo('scope ' + str(scope))
                args = ['--target', 'both', '--apply'] + (['--scope', scope] if scope else [])
                self.install(target, *args)
                self.assertFalse((target / '.github/workflows/bureau-approval.yml').exists())
                if scope == 'scripts': self.assertEqual((target / 'scripts/bureau-approval-status.py').read_bytes(), SCRIPT.read_bytes())
                if scope == 'ci':
                    self.assertEqual((target / '.github/workflows/ci.yml').read_bytes(), (ROOT / 'templates/.github/workflows/ci.yml').read_bytes())

    def test_doctor_rejects_invalid_lists_and_accepts_login_boundaries(self):
        doctor = load('approval_doctor', ROOT / 'templates/scripts/bureau-doctor.py')
        for value in (None, {}, 'person', [False], ['-person'], ['person-'], ['per--son'], ['a_b'], ['x' * 40], ['person'] * 21):
            config = copy.deepcopy(BASE); config['agents']['merge_approvers'] = value
            self.assertTrue(any('agents.merge_approvers' in error for error in doctor.validate(config)))
        for value in ([], ['a', 'x' * 39, 'person-2'], ['person' + str(n) for n in range(20)]):
            config = copy.deepcopy(BASE); config['agents']['merge_approvers'] = value
            self.assertEqual(doctor.validate(config), [])

    def test_doctor_without_approvers_does_not_need_approval_script(self):
        repo = self.git_repo(); config = copy.deepcopy(BASE); del config['agents']['merge_approvers']
        (repo / '.bureau.json').write_text(json.dumps(config))
        self.install(repo, '--target', 'codex', '--scope', 'interfaces', '--apply')
        shutil.copytree(ROOT / 'templates/scripts', repo / 'scripts')
        command = [sys.executable, str(repo / 'scripts/bureau-doctor.py'), '--repo', str(repo)]
        before = subprocess.run(command, env=self.env, capture_output=True, text=True, timeout=10)
        self.assertEqual(before.returncode, 0, before.stdout + before.stderr)
        (repo / 'scripts/bureau-approval-status.py').unlink()
        after = subprocess.run(command, env=self.env, capture_output=True, text=True, timeout=10)
        self.assertEqual(after.returncode, 0, after.stdout + after.stderr)
        self.assertEqual(after.stderr, '')
        self.assertEqual(json.loads(after.stdout), json.loads(before.stdout))
        self.assertEqual(self.calls(), [])

    def test_doctor_with_approvers_reports_missing_approval_script(self):
        repo = self.git_repo(); (repo / '.bureau.json').write_text(json.dumps(BASE))
        self.install(repo, '--target', 'codex', '--scope', 'interfaces', '--apply')
        shutil.copytree(ROOT / 'templates/scripts', repo / 'scripts', ignore=shutil.ignore_patterns('bureau-approval-status.py'))
        proc = subprocess.run([sys.executable, str(repo / 'scripts/bureau-doctor.py'), '--repo', str(repo)],
                              env=self.env, capture_output=True, text=True, timeout=10)
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertEqual(proc.stderr, '')
        result = json.loads(proc.stdout)
        self.assertFalse(result['ok'])
        self.assertEqual(result['errors'], ['Missing runtime asset: scripts/bureau-approval-status.py; resync scripts'])
        self.assertEqual(self.calls(), [])

    def test_doctor_warns_about_workflow_protection_and_shared_identity(self):
        repo = self.git_repo(); (repo / '.bureau.json').write_text(json.dumps(BASE))
        self.install(repo, '--target', 'codex', '--scope', 'interfaces', '--scope', 'scripts', '--apply')
        doctor = load('approval_doctor', ROOT / 'templates/scripts/bureau-doctor.py')
        self.state['login'] = 'MERGE-PERSON'; self.save()
        with patch.dict(os.environ, self.env, clear=True): result = doctor.diagnose(repo, 'app')
        self.assertTrue(result['ok'], result)
        warnings = '\n'.join(result['warnings'])
        self.assertIn('workflow is not installed', warnings); self.assertIn('assets --scope approval', warnings)
        self.assertIn('Branch protection must require the context bureau/human-approval', warnings)
        self.assertIn('accident guard against approval carried to a later push', warnings)
        self.assertIn('does not stop deliberate bypass', warnings)
        self.assertIn('a pull request workflow can write the same status', warnings)
        self.assertIn("agents using an approver's login can approve themselves", warnings)
        self.assertIn('An unforgeable approval requires a separate Bureau identity.', warnings)
        self.assertIn('Bureau and its agents act with the login MERGE-PERSON and could then approve on their own behalf.', warnings)
        self.assertIn('A separate account or token without that login is needed for the approval to mean a person.', warnings)
        self.assertEqual(self.calls(), [dict(method='GET', route='user', fields={}, query='.login')])
        self.assertEqual(self.writes(), [])
        self.install(repo, '--scope', 'approval', '--apply'); self.state['login'] = 'runtime-agent'; self.save()
        with patch.dict(os.environ, self.env, clear=True): warnings = doctor.approval_warnings(repo, BASE)
        self.assertEqual(len(warnings), 2); self.assertIn(CONTEXT, warnings[0]); self.calls()

    def test_doctor_quietly_skips_unreadable_login_and_off_means_no_calls(self):
        doctor = load('approval_doctor', ROOT / 'templates/scripts/bureau-doctor.py')
        self.state['fail_route'] = 'user'; self.save()
        with patch.dict(os.environ, self.env, clear=True): warnings = doctor.approval_warnings(self.root, BASE)
        self.assertEqual(len(warnings), 3); self.assertFalse(any('own behalf' in w for w in warnings)); self.calls()
        for error in (FileNotFoundError(), subprocess.TimeoutExpired('gh', 10), UnicodeDecodeError('utf-8', b'\xff', 0, 1, 'invalid')):
            with patch.object(doctor.subprocess, 'run', side_effect=error):
                self.assertEqual(len(doctor.approval_warnings(self.root, BASE)), 3)
        for config in ({'agents': {}}, {'agents': {'merge_approvers': []}}):
            self.save()
            with patch.dict(os.environ, self.env, clear=True): self.assertEqual(doctor.approval_warnings(self.root, config), [])
            self.assertEqual(self.calls(), [])


class ApprovalStructure(unittest.TestCase):
    def assert_trusted_workflow(self, text):
        workflow = workflow_yaml(text)
        self.assertEqual(workflow['on'], dict(issue_comment={'types': ['created']}))
        self.assertEqual(workflow['permissions'], {'statuses': 'write', 'pull-requests': 'read', 'contents': 'read'})
        self.assertEqual(workflow['concurrency']['group'], 'bureau-approval-${{ github.event.issue.number }}')
        self.assertFalse(workflow['concurrency']['cancel-in-progress'])
        self.assertEqual(set(workflow['jobs']), {'approval'})
        job = workflow['jobs']['approval']; self.assertNotIn('permissions', job)
        self.assertEqual(job['if'], 'github.event.issue.pull_request')
        self.assertEqual(job['runs-on'], 'ubuntu-latest')
        checkout, run = job['steps']
        self.assertEqual(checkout['uses'], 'actions/checkout@v4')
        self.assertEqual(checkout['with'], {'ref': '${{ github.event.repository.default_branch }}', 'persist-credentials': False})
        self.assertNotIn('${{ github.event', run['run'])
        self.assertEqual(run['run'], 'python3 scripts/bureau-approval-status.py --event-name "$BUREAU_EVENT_NAME" --event-path "$BUREAU_EVENT_PATH" --repo "$BUREAU_REPOSITORY"')
        self.assertEqual(run['env'], {'GH_TOKEN': '${{ github.token }}', 'BUREAU_EVENT_NAME': '${{ github.event_name }}',
                                     'BUREAU_EVENT_PATH': '${{ github.event_path }}', 'BUREAU_REPOSITORY': '${{ github.repository }}'})

    def test_workflow_yaml_and_trusted_execution(self):
        self.assert_trusted_workflow(WORKFLOW.read_text())

    def test_workflow_rejects_unsafe_mutations(self):
        text = WORKFLOW.read_text()
        for trigger in ('pull_request', 'pull_request_review', 'pull_request_target'):
            with self.subTest(trigger=trigger):
                for variant in (text.replace('issue_comment:', trigger + ':'),
                                text.replace('permissions:', '  ' + trigger + ':\n    types: [opened]\npermissions:', 1)):
                    with self.assertRaises(AssertionError): self.assert_trusted_workflow(variant)
        for old, new in (('github.event.issue.pull_request', 'true'),
                         ('${{ github.event.repository.default_branch }}', '${{ github.event.pull_request.head.sha }}'),
                         ('${{ github.event.repository.default_branch }}', '${{ github.ref }}'),
                         ('--repo "$BUREAU_REPOSITORY"', '--repo "${{ github.event.comment.body }}"'),
                         ('contents: read', 'contents: write')):
            with self.subTest(mutation=new):
                self.assertIn(old, text)
                with self.assertRaises(AssertionError): self.assert_trusted_workflow(text.replace(old, new))

    def test_status_writer_can_only_send_success(self):
        states = [node.value for node in ast.walk(ast.parse(SCRIPT.read_text()))
                  if isinstance(node, ast.keyword) and node.arg == 'state']
        self.assertEqual(len(states), 1)
        self.assertIsInstance(states[0], ast.Constant)
        self.assertEqual(states[0].value, 'success')

    def test_only_workflow_script_can_write_statuses_and_no_pipeline_calls_it(self):
        for path in (ROOT / 'templates/scripts').rglob('*'):
            if not path.is_file() or path == SCRIPT: continue
            text = path.read_text()
            if path.name.endswith('-pipeline.sh'): self.assertNotIn(SCRIPT.name, text, str(path))
            self.assertNotRegex(text, r'/statuses(?:/|[\s\x27\"])', str(path))


if __name__ == '__main__':
    unittest.main()
