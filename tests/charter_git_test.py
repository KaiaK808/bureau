#!/usr/bin/env python3
"""Real-Git charter fixtures and offline service transport for the merge driver."""
import base64
import copy
import importlib.util
import io
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import types
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / 'templates/scripts'
spec = importlib.util.spec_from_file_location('charter', SCRIPTS / 'bureau-charter.py')
charter = importlib.util.module_from_spec(spec)
spec.loader.exec_module(charter)


def git(repo, *args):
    result = subprocess.run(['git', '-c', 'core.hooksPath=/dev/null', '-c', 'user.name=Fixture',
                             '-c', 'user.email=fixture@example.invalid', '-C', str(repo), *args],
                            capture_output=True, text=True, check=True)
    return result.stdout.strip()


def base_config():
    return dict(linear=dict(teams=[dict(id='team', key='TEAM', states=dict(merge='merge'))]),
                agents=dict(merge_mode='manual', merge_charter=dict(enabled=True)), repo={})


def build_history(repo, case='clean'):
    """Every policy/head/base identity used by the production loader is real."""
    repo.mkdir(parents=True, exist_ok=True)
    git(repo, 'init', '--initial-branch=main')
    config = base_config() if not (repo / '.bureau.json').exists() else json.loads((repo / '.bureau.json').read_text())
    config['agents']['merge_charter'] = dict(enabled=True)
    (repo / '.bureau.json').write_text(json.dumps(config))
    (repo / 'ordinary.txt').write_text('base\n')
    git(repo, 'add', '.bureau.json', 'ordinary.txt'); git(repo, 'commit', '-m', 'chore: trusted policy')
    default = git(repo, 'rev-parse', 'HEAD')
    git(repo, 'checkout', '-b', 'feat/test')
    if case in ('policy', 'disabled-pr', 'checkout-policy', 'unenrolled', 'disabled-unenrolled', 'stale-policy'):
        config['agents']['merge_charter']['enabled'] = False
        if case in ('checkout-policy', 'unenrolled'):
            config['agents']['merge_charter'] = dict(enabled=True, exceptions=[{'class': 'checkout-only', 'labels': ['sensitive']}])
        (repo / '.bureau.json').write_text(json.dumps(config))
    (repo / 'ordinary.txt').write_text('head\n')
    git(repo, 'add', '.bureau.json', 'ordinary.txt'); git(repo, 'commit', '-m', 'feat: ordinary change')
    head = git(repo, 'rev-parse', 'HEAD')
    git(repo, 'update-ref', 'refs/remotes/origin/main', default)
    git(repo, 'symbolic-ref', 'refs/remotes/origin/HEAD', 'refs/remotes/origin/main')
    # A local bare transport exercises real fetch and object identity checks.
    origin = repo.parent / (repo.name + '-origin.git')
    subprocess.run(['git', 'clone', '--bare', '--no-hardlinks', str(repo), str(origin)], capture_output=True, check=True)
    git(repo, 'remote', 'add', 'origin', str(origin))
    return default, head


class LocalGitHub(charter.GitHub):
    """Fake only GitHub answers; Git, policy bytes, parsers and fetch remain real."""
    def __init__(self, repo, default):
        super().__init__(repo, 'test-owner/test-repo')
        self.default = default
        self.calls = []

    def api(self, route):
        self.calls.append(route)
        if route == '':
            return dict(default_branch='main')
        if route == '/commits/main':
            return dict(sha=self.default)
        raise ValueError('unexpected fake GitHub route')


def mutant(old, new):
    source = (SCRIPTS / 'bureau-charter.py').read_text()
    if source.count(old) != 1:
        raise AssertionError('mutation anchor missing or ambiguous: ' + old)
    module = types.ModuleType('charter_mutant')
    module.__file__ = str(SCRIPTS / 'bureau-charter.py')
    exec(compile(source.replace(old, new), '<charter mutation>', 'exec'), module.__dict__)
    return module


class ProbeLayoutTests(unittest.TestCase):
    """Repository authority comes from real Git output, never fixture discovery."""
    def setUp(self):
        scratch = tempfile.TemporaryDirectory(prefix='bureau-charter-layout-', dir=tempfile.gettempdir())
        self.addCleanup(scratch.cleanup)
        self.root = Path(scratch.name)

    def command(self, repo, common=None):
        arguments = ['--repo', str(repo), 'status']
        if common is not None:
            arguments += ['--common-dir', common]
        output = io.StringIO()
        with redirect_stdout(output):
            code = charter.main(arguments)
        return code, output.getvalue().strip()

    def record(self, repo):
        path = charter.enrollment_path(repo)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(dict(version=1, repository='owner/repository', default_branch='main',
                                       default_commit='a' * 40, policy_digest='b' * 64)))
        return path

    def test_git_output_drives_checkout_worktree_separate_bare_and_submodule_probes(self):
        checkout = self.root / 'checkout with spaces'
        checkout.mkdir()
        git(checkout, 'init', '--initial-branch=main')
        git(checkout, 'commit', '--allow-empty', '-m', 'chore: layout fixture')
        nested = checkout / 'nested directory'
        nested.mkdir()
        linked = self.root / 'linked worktree'
        git(checkout, 'worktree', 'add', '--detach', str(linked), 'HEAD')
        separate = self.root / 'separate checkout'
        separate.mkdir()
        git(separate, 'init', '--separate-git-dir', str(self.root / 'separate metadata'), '--initial-branch=main')
        bare = self.root / 'standalone bare.git'
        git(self.root, 'init', '--bare', '--initial-branch=main', str(bare))
        nested_bare = checkout / 'nested bare.git'
        git(checkout, 'init', '--bare', '--initial-branch=main', str(nested_bare))
        superproject = self.root / 'superproject'
        superproject.mkdir()
        git(superproject, 'init', '--initial-branch=main')
        git(superproject, '-c', 'protocol.file.allow=always', 'submodule', 'add', '--', str(checkout), 'module with spaces')
        module = superproject / 'module with spaces'
        for repo in (checkout, nested, linked, separate, bare, bare / 'objects', nested_bare, module):
            with self.subTest(layout=repo.relative_to(self.root)):
                raw = git(repo, 'rev-parse', '--git-common-dir')
                self.assertEqual(charter.common_dir(repo), (repo / raw).resolve())
                self.assertEqual(self.command(repo), (0, 'not-enrolled'))
                with patch.object(charter.subprocess, 'run', side_effect=AssertionError('unexpected process')):
                    self.assertEqual(self.command(repo, raw), (0, 'not-enrolled'))
                path = self.record(repo)
                try:
                    self.assertEqual(self.command(repo), (0, 'enrolled'))
                    with patch.object(charter.subprocess, 'run', side_effect=AssertionError('unexpected process')):
                        self.assertEqual(self.command(repo, raw), (0, 'enrolled'))
                finally:
                    path.unlink()
        self.assertIsNone(charter.enrollment(checkout))

    def test_ordinary_content_named_like_git_metadata_keeps_git_authority(self):
        checkout = self.root / 'checkout'
        checkout.mkdir()
        git(checkout, 'init', '--initial-branch=main')
        self.record(checkout)
        content = checkout / 'fixtures'
        content.mkdir()
        (content / 'HEAD').write_text('notes\n')
        for name in ('objects', 'refs'):
            (content / name).mkdir()
        raw = git(content, 'rev-parse', '--git-common-dir')
        self.assertEqual((content / raw).resolve(), charter.common_dir(checkout))
        self.assertEqual(self.command(content, raw), (0, 'enrolled'))

    def test_git_failure_never_confirms_absence(self):
        bare = self.root / 'broken bare.git'
        git(self.root, 'init', '--bare', '--initial-branch=main', str(bare))
        self.record(bare)
        (bare / 'HEAD').unlink()
        code, message = self.command(bare)
        self.assertEqual(code, 25)
        self.assertIn('Git operation failed', message)


class GitTests(unittest.TestCase):
    def setUp(self):
        self.scratch = tempfile.TemporaryDirectory(prefix='bureau-charter-', dir=tempfile.gettempdir())
        self.addCleanup(self.scratch.cleanup)
        self.repo = Path(self.scratch.name) / 'checkout with spaces'
        self.default, self.head = build_history(self.repo, 'checkout-policy')
        self.source = LocalGitHub(self.repo, self.default)
        self.pin = self.source.pin()

    def enrolled(self):
        return charter.enroll(self.repo, self.source)

    def test_not_enrolled_does_not_read_policy_or_create_state(self):
        self.source.calls.clear()
        self.assertEqual(charter.load_policy(self.repo, self.source), dict(kind='not-enrolled'))
        self.assertEqual(self.source.calls, [])
        self.assertFalse(charter.enrollment_path(self.repo).parent.exists())

    def test_trusted_policy_is_read_at_D_and_not_checkout_or_override(self):
        self.enrolled()
        with patch.dict(os.environ, BUREAU_CONFIG=str(self.repo / '.bureau.json')):
            def observation(module):
                snapshot = module.load_policy(self.repo, self.source, self.pin)
                self.assertEqual(snapshot['default_commit'], self.default)
                self.assertEqual(snapshot['policy']['exceptions'], [])
                self.assertEqual(snapshot['policy_digest'], charter.digest(charter.policy_from_config(base_config())))
            observation(charter)
            weakened = mutant("raw = blob_at(repo, pin['default_commit'], '.bureau.json')",
                              "raw = (Path(repo) / '.bureau.json').read_bytes()")
            with self.assertRaises(AssertionError):
                observation(weakened)

    def test_policy_change_and_false_in_PR_remain_enrolled(self):
        self.enrolled()
        for disabled in (False, True):
            current = json.loads((self.repo / '.bureau.json').read_text())
            current['agents']['merge_charter']['enabled'] = not disabled
            (self.repo / '.bureau.json').write_text(json.dumps(current))
            git(self.repo, 'add', '.bureau.json'); git(self.repo, 'commit', '--allow-empty', '-m', 'chore: policy edit')
            head = git(self.repo, 'rev-parse', 'HEAD')
            git(self.repo, 'push', 'origin', 'HEAD:refs/heads/feat/test')
            def observation(module):
                code, line = module.gate(self.repo, self.source, self.pin, 'main', head)
                self.assertEqual(code, 25)
                self.assertTrue(line.startswith('charter-policy:'), line)
                self.assertIsNotNone(charter.enrollment(self.repo))
            observation(charter)
            with self.assertRaises(AssertionError):
                observation(mutant('if classification:', 'if False:'))

    def test_unreadable_enrolled_policy_is_an_exception(self):
        self.enrolled()
        git(self.repo, 'checkout', 'main')
        (self.repo / '.bureau.json').write_text('{broken')
        git(self.repo, 'add', '.bureau.json'); git(self.repo, 'commit', '-m', 'chore: invalid policy')
        broken = git(self.repo, 'rev-parse', 'HEAD')
        self.source.default = broken
        git(self.repo, 'push', 'origin', 'main')
        pin = self.source.pin()
        def observation(module):
            code, line = module.gate(self.repo, self.source, pin, 'main', self.head)
            self.assertEqual(code, 25)
            self.assertTrue(line.startswith('charter-unreadable:'), line)
        observation(charter)
        with self.assertRaises(AssertionError):
            observation(mutant("return 25, 'charter-unreadable: ' + str(exc)", "return 0, ''"))

    def test_deleted_renamed_and_type_changed_policy_are_classified(self):
        for operation in ('delete', 'rename', 'type'):
            git(self.repo, 'checkout', '-B', 'policy-case', self.default)
            if operation == 'delete':
                (self.repo / '.bureau.json').unlink()
            elif operation == 'rename':
                (self.repo / '.bureau.json').rename(self.repo / 'policy.json')
            else:
                (self.repo / '.bureau.json').unlink()
                (self.repo / '.bureau.json').symlink_to('ordinary.txt')
            git(self.repo, 'add', '-A'); git(self.repo, 'commit', '-m', 'chore: change policy path')
            self.assertEqual(charter.policy_change_class(self.repo, self.default, git(self.repo, 'rev-parse', 'HEAD')), 'charter-policy')

    def test_revalidate_and_clean_policy_change_control(self):
        self.enrolled()
        self.assertTrue(self.source.revalidate(self.pin))
        self.assertTrue(self.source.revalidate(charter.load_policy(self.repo, self.source)))
        self.source.default = self.head
        self.assertFalse(self.source.revalidate(self.pin))
        self.assertIsNone(charter.policy_change_class(self.repo, self.default, self.default))
        def observation(module):
            code, line = module.gate(self.repo, self.source, self.pin, 'main', self.default)
            self.assertEqual(code, 2)
            self.assertTrue(line.startswith('charter-retry:'), line)
        observation(charter)
        old = "return 2, 'charter-retry: default-branch policy changed during evaluation; retry with a fresh pin'"
        with self.assertRaises(AssertionError):
            observation(mutant(old, "return 0, ''"))

    def test_doctor_static_inputs_handoff_and_shared_state(self):
        config = base_config()
        config['agents']['merge_charter'].update(
            suite=dict(id='full', command='true', definition_files=['missing.sh']),
            red_main=dict(workflows=['.github/workflows/ci.yml']), report=True)
        errors, _ = charter.diagnose(self.repo, config)
        self.assertTrue(any('suite definition file is missing' in error for error in errors), errors)
        self.assertTrue(any('workflow declaration is missing' in error for error in errors), errors)
        config['agents']['merge_charter']['suite']['definition_files'] = ['ordinary.txt']
        workflow = self.repo / '.github/workflows/ci.yml'
        workflow.parent.mkdir(parents=True)
        workflow.write_text('on: [push, pull_request]\njobs: {}\n')
        errors, warnings = charter.diagnose(self.repo, config)
        self.assertEqual(errors, [])
        self.assertFalse(any('no statically recognizable push' in warning for warning in warnings), warnings)
        workflow.write_text('on: pull_request\njobs: {}\n')
        self.assertTrue(any('no statically recognizable push' in warning for warning in charter.diagnose(self.repo, config)[1]))
        config['linear']['teams'][0]['states'] = {}
        self.assertIn('Enabled charter requires a Merge state for human handoff', charter.diagnose(self.repo, config)[0])
        common = charter.common_dir(self.repo)
        mode = common.stat().st_mode & 0o777
        try:
            common.chmod(0o500)
            self.assertIn('Charter shared Git state is not writable', charter.diagnose(self.repo, config)[0])
        finally:
            common.chmod(mode)

    def test_enrollment_is_atomic_shared_and_explicitly_reversible(self):
        record = self.enrolled()
        path = charter.enrollment_path(self.repo)
        self.assertEqual(record['default_commit'], self.default)
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(list(path.parent.glob('.enrollment-*')), [])
        worktree = Path(self.scratch.name) / 'linked'
        git(self.repo, 'worktree', 'add', '--detach', str(worktree), self.default)
        self.assertEqual(charter.enrollment(worktree), record)
        self.assertEqual(charter.enrollment_path(worktree), path)
        self.assertEqual(charter.main(['--repo', str(worktree), 'unenroll']), 0)
        self.assertIsNone(charter.enrollment(self.repo))
        self.assertFalse((worktree / 'enrollment.json').exists())

    def test_corrupt_enrollment_does_not_unenroll(self):
        self.enrolled()
        path = charter.enrollment_path(self.repo)
        for raw in (b'{bad', b'{}', b'{"version":false}'):
            path.write_bytes(raw)
            code, line = charter.gate(self.repo, self.source, self.pin, 'main', self.head)
            self.assertEqual(code, 25)
            self.assertTrue(line.startswith('charter-unreadable:'), line)

    def test_unenroll_refuses_state_symlinked_to_another_repository(self):
        other = Path(self.scratch.name) / 'other checkout'
        default, _ = build_history(other)
        charter.enroll(other, LocalGitHub(other, default))
        target = charter.enrollment_path(other)
        original = target.read_bytes()
        path = charter.enrollment_path(self.repo)
        path.parent.parent.mkdir()
        path.parent.symlink_to(target.parent, target_is_directory=True)
        self.assertEqual(charter.main(['--repo', str(self.repo), 'unenroll']), 25)
        self.assertEqual(target.read_bytes(), original)
        self.assertTrue(path.parent.is_symlink())

    def test_doctor_drift_and_disabled_compatibility(self):
        config = base_config()
        absent = copy.deepcopy(config); absent['agents'].pop('merge_charter')
        self.assertEqual(charter.diagnose(self.repo, absent), ([], []))
        disabled = copy.deepcopy(config); disabled['agents']['merge_charter']['enabled'] = False
        self.assertEqual(charter.diagnose(self.repo, disabled), ([], []))
        self.assertTrue(any('enrollment is absent' in warning for warning in charter.diagnose(self.repo, config)[1]))
        self.enrolled()
        git(self.repo, 'update-ref', 'refs/remotes/origin/main', self.head)
        def observation(module):
            errors, warnings = module.diagnose(self.repo, config)
            self.assertEqual(errors, [])
            self.assertTrue(any('digest drift' in warning for warning in warnings), warnings)
        observation(charter)
        with self.assertRaises(AssertionError):
            observation(mutant("if digest(policy) != record['policy_digest']:", 'if False:'))
        doctor_spec = importlib.util.spec_from_file_location('doctor', SCRIPTS / 'bureau-doctor.py')
        doctor = importlib.util.module_from_spec(doctor_spec); doctor_spec.loader.exec_module(doctor)
        result = doctor.diagnose(self.repo, 'app')
        self.assertTrue(any('digest drift' in warning for warning in result.get('warnings', [])), result)


def service_transport(args):
    """Only GitHub API payloads are faked; unknown requests use the existing fake."""
    if args[:1] != ['api']:
        return 3
    route = next((v for v in args[1:] if v.startswith('repos/')), '')
    state = Path(os.environ['STUB_DIR'])
    default = (state / 'default_commit').read_text().strip()
    if '--method' not in args or args[args.index('--method') + 1] != 'GET':
        return 3
    if route == 'repos/test-owner/test-repo':
        value = dict(default_branch='main')
    elif route == 'repos/test-owner/test-repo/commits/main':
        value = dict(sha=default)
    elif '/git/trees/' in route:
        value = dict(truncated=False, tree=[dict(path='.bureau.json')])
    elif route.endswith('/contents/.bureau.json'):
        if 'ref=' + default not in args:
            raise AssertionError('publisher policy did not use the pinned default commit')
        value = dict(encoding='base64', content=base64.b64encode(b'{"agents":{"merge_publishers":["user:fixture"]}}').decode())
    else:
        return 3
    with (state / 'charter_api.log').open('a') as log:
        log.write(route + '\n')
    print(json.dumps(value))
    return 0


def prepare_pipeline(repo, case, mutation=''):
    default, head = build_history(repo, case)
    state = repo / 'stub_data'
    (state / 'default_commit').write_text(default)
    original = (repo / 'bin/gh').rename(repo / 'bin/gh.base')
    wrapper = repo / 'bin/gh'
    wrapper.write_text('#!/bin/bash\npython3 -I -B ' + shlex.quote(str(Path(__file__).resolve()))
                       + ' --transport "$@"\nrc=$?\n[ "$rc" = 3 ] || exit "$rc"\nexec '
                       + shlex.quote(str(original)) + ' "$@"\n')
    wrapper.chmod(0o755)
    shutil.copy(SCRIPTS / 'bureau-charter.py', repo / 'scripts/bureau-charter.py')
    if case not in ('unenrolled', 'disabled-unenrolled', 'absent'):
        charter.enroll(repo, LocalGitHub(repo, default))
    if case == 'absent':
        config = json.loads((repo / '.bureau.json').read_text())
        config['agents'].pop('merge_charter'); (repo / '.bureau.json').write_text(json.dumps(config))
    if case == 'unreadable':
        git(repo, 'checkout', 'main')
        (repo / '.bureau.json').write_text('{bad')
        git(repo, 'add', '.bureau.json'); git(repo, 'commit', '-m', 'chore: invalid policy')
        default = git(repo, 'rev-parse', 'HEAD')
        git(repo, 'push', 'origin', 'main')
        (state / 'default_commit').write_text(default)
        git(repo, 'checkout', 'feat/test')
    data = json.loads((state / 'pr_view.json').read_text())
    data.update(headRefOid=head, baseRefOid=default)
    data['title'] = 'Fixture change'
    for comment in data['comments']:
        old_issue = comment['body'].splitlines()[0].split(' — ')[-1]
        comment['body'] = comment['body'].replace('a' * 40, head).replace('b' * 40, default).replace(old_issue, 'TEAM-1')
        if case == 'stale-policy':
            comment['body'] = comment['body'].replace(head, default)
    (state / 'pr_view.json').write_text(json.dumps(data))
    (state / 'branch_main.json').write_text(json.dumps(dict(commit=dict(sha=default))))
    with (repo / 'scripts/bureau-config.sh').open('a') as output:
        output.write('\nmark_needs_human() { printf "needs-human %s %s %s\\n" "$@" >> "$STUB_DIR/handoff.log"; }\n')
    if case == 'non-searchable':
        charter.enrollment_path(repo).parent.chmod(0o600)
    if case == 'common-dir-failed':
        # A failed Git lookup must block rather than confirm absence.
        (repo / '.git/commondir').write_text('missing-common-directory\n')
    if mutation:
        target = repo / ('scripts/merge-pipeline.sh' if mutation in ('wiring', 'unconditional', 'handoff', 'priority') else 'scripts/bureau-charter.py')
        old, new = {
            'wiring': ('if charter_enrolled; then', 'if false; then'),
            'unconditional': ('if charter_enrolled; then', 'if true; then'),
            'classification': ('if classification:', 'if False:'),
            'unreadable': ("return 25, 'charter-unreadable: ' + str(exc)", "return 0, ''"),
            'handoff': ('mark_needs_human "$ISSUE" merge 25 || true ;;', ': ;;'),
            'priority': ('charter-policy:*|charter-unreadable:*|charter-unclassified:*|*$\'\\n\'charter-policy:*|*$\'\\n\'charter-unreadable:*|*$\'\\n\'charter-unclassified:*) return 0 ;;', ':) return 0 ;;'),
        }[mutation]
        source = target.read_text()
        if old not in source:
            raise AssertionError('mutation anchor missing: ' + old)
        target.write_text(source.replace(old, new))


if __name__ == '__main__':
    # Fixture stores and mutation copies live only in the caller's temp root.
    os.environ['GIT_ALLOW_PROTOCOL'] = 'file'
    os.environ['GIT_CONFIG_NOSYSTEM'] = '1'
    os.environ['GIT_CONFIG_GLOBAL'] = os.devnull
    if sys.argv[1:2] == ['--transport']:
        sys.exit(service_transport(sys.argv[2:]))
    if sys.argv[1:2] == ['--prepare']:
        prepare_pipeline(Path(sys.argv[2]), *sys.argv[3:])
    else:
        unittest.main()
