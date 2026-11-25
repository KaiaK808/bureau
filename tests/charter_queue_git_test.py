#!/usr/bin/env python3
"""Real Git fixtures; only Linear and GitHub transport answers are fake."""
import importlib.util
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('foundation', ROOT / 'tests/charter_git_test.py')
foundation = importlib.util.module_from_spec(spec)
spec.loader.exec_module(foundation)
charter, git, SCRIPTS = foundation.charter, foundation.git, foundation.SCRIPTS
ISSUE = 'TEST-' + str(1)
CYCLE = '12345678-1234-1234-1234-123456789abc'


def queue_policy(case):
    value = dict(enabled=True, approved_queue=dict(label='approved-for-merge'), exceptions=[
        dict({'class': 'security'}, paths=['auth/**']), dict({'class': 'data'}, labels=['schema-change'])])
    if case in ('wrong-cycle', 'cycle', 'unassigned-cycle', 'missing-cycle'):
        value['approved_queue'] = dict(cycle_id=CYCLE)
    if case == 'approval-read-failed':
        value['exceptions'] = [dict({'class': 'security'}, paths=['auth/**'])]
    return value


def build_history(repo, case):
    foundation.build_history(repo)
    git(repo, 'checkout', 'main')
    config = json.loads((repo / '.bureau.json').read_text())
    config['agents']['merge_charter'] = queue_policy(case)
    (repo / '.bureau.json').write_text(json.dumps(config))
    protected = repo / 'auth/entry.txt'
    protected.parent.mkdir()
    protected.write_text(''.join('protected fixture line %d\n' % n for n in range(40)))
    git(repo, 'add', '.bureau.json', 'auth/entry.txt'); git(repo, 'commit', '-m', 'chore: queue policy')
    default = git(repo, 'rev-parse', 'HEAD')
    git(repo, 'checkout', '-B', 'feat/test', default)
    (repo / 'ordinary.txt').write_text('queue candidate\n')
    if case in ('path', 'multiple', 'precedence', 'stale-exception'):
        protected.write_text(protected.read_text() + 'protected change\n')
    elif case == 'deleted':
        protected.unlink()
    elif case in ('renamed', 'copied'):
        target = repo / 'public/entry.txt'; target.parent.mkdir()
        if case == 'renamed': protected.rename(target)
        else: shutil.copyfile(protected, target)
    git(repo, 'add', '-A', '--', '.bureau.json', 'ordinary.txt', 'auth', *(['public'] if case in ('renamed', 'copied') else []))
    git(repo, 'commit', '-m', 'feat: queue candidate')
    head = git(repo, 'rev-parse', 'HEAD')
    if case == 'ambiguous':
        tree = git(repo, 'rev-parse', default + '^{tree}')
        left = git(repo, 'commit-tree', tree, '-p', default, '-m', 'left')
        right = git(repo, 'commit-tree', tree, '-p', default, '-m', 'right')
        default = git(repo, 'commit-tree', tree, '-p', left, '-p', right, '-m', 'base merge')
        head = git(repo, 'commit-tree', tree, '-p', right, '-p', left, '-m', 'head merge')
        git(repo, 'update-ref', 'refs/heads/main', default)
        git(repo, 'checkout', '-B', 'feat/test', head)
    git(repo, 'push', 'origin', 'main', 'feat/test', '--force')
    git(repo, 'update-ref', 'refs/remotes/origin/main', default)
    return default, head


def linear_value(case, second=False):
    labels = ['approved-for-merge']
    if case in ('label-missing', 'precedence', 'stale-exception') or (case == 'jit-approval' and second):
        labels = []
    if case in ('label', 'multiple', 'unenrolled') or (case == 'jit-exception' and second):
        labels.append('schema-change')
    cycle = dict(id=CYCLE)
    if case == 'wrong-cycle': cycle = dict(id='0' + CYCLE[1:])
    if case == 'unassigned-cycle': cycle = None
    node = dict(identifier=ISSUE, cycle=cycle, labels=dict(nodes=[dict(name=name) for name in labels], pageInfo=dict(hasNextPage=False)))
    if case == 'partial-labels': node['labels'].pop('nodes')
    if case == 'paginated-labels': node['labels']['pageInfo']['hasNextPage'] = True
    if case == 'missing-cycle': node.pop('cycle')
    value = dict(data=dict(issues=dict(nodes=[node], pageInfo=dict(hasNextPage=False))))
    if case == 'partial-error': value['errors'] = [dict(message='partial response')]
    return value


def linear_transport(args):
    state = Path(os.environ['STUB_DIR'])
    if 'Authorization: stub' not in sys.stdin.read():
        raise AssertionError('charter did not load its Linear credentials through the safe reader')
    count_path = state / 'linear-reads'
    count = int(count_path.read_text()) + 1 if count_path.exists() else 1
    count_path.write_text(str(count))
    query = json.loads(args[args.index('-d') + 1])['query']
    expected_cycle = (state / 'queue-case').read_text() in ('cycle', 'wrong-cycle', 'unassigned-cycle', 'missing-cycle')
    if ('cycle { id }' in query) != expected_cycle or 'pageInfo { hasNextPage }' not in query or 'identifier labels' not in query:
        raise AssertionError('incomplete or unexpected charter query')
    if (state / 'linear-failed').exists():
        return 52
    path = state / ('linear-2.json' if count > 1 else 'linear.json')
    print(path.read_text(), end='')
    print(args[args.index('-w') + 1].replace('%{http_code}', '200').replace('\\n', '\n'), end='')
    return 0


def github_transport(args):
    route = 'repos/test-owner/test-repo/commits/release'
    if args[:1] != ['api'] or route not in args:
        return 3
    state = Path(os.environ['STUB_DIR']); repo = state.parent
    case = (state / 'queue-case').read_text()
    if case not in ('unreadable-input', 'truncated-input'):
        raise AssertionError('unexpected target read')
    # Damage the actual Git input only after trusted policy has loaded. The
    # next operation is classification, so a policy-loader failure cannot
    # accidentally stand in for the unclassified assertion.
    tree = (state / 'head-tree').read_text()
    for store in (repo / '.git', repo.parent / (repo.name + '-origin.git')):
        path = store / 'objects' / tree[:2] / tree[2:]
        if case == 'unreadable-input': path.unlink(missing_ok=True)
        elif path.exists(): path.write_bytes(path.read_bytes()[:8])
    print(json.dumps(dict(sha=(state / 'default_commit').read_text().strip())))
    return 0


def inline_entry(repo):
    """Run the actual APPROVE merge caller and result arms without model work."""
    source = (SCRIPTS / 'code-review-pipeline.sh').read_text()
    release = source[source.index('_review_release_worktree() {'):source.index('\nprecondition_linear', source.index('_review_release_worktree() {'))]
    start = source.index('      GATE_REPORT="$REVIEW_TMP/merge-gate.report"')
    end = source.index('\n    fi\n    ;;\n  REQUEST_CHANGES)', start)
    body = source[start:end]
    header = '''#!/bin/bash
set -euo pipefail
source scripts/bureau-config.sh
ISSUE="$1"
SCRIPT_REPO="$PWD"
REVIEW_TMP="$PWD/stub_data"
REVIEW_HEAD=$(git rev-parse HEAD)
REVIEW_BASE=$(cat "$STUB_DIR/default_commit")
BRANCH=feat/test
PR_BASE_REF=$(jq -r .baseRefName "$STUB_DIR/pr_view.json")
PR_NUMBER=42
ACTUAL_STATE='Build Review'
ISSUE_DETAIL='{}'
REVIEW_POLICY=fixture
REVIEW_ROUND_MARKER=''
REUSED_GATE_WAITS=0
REUSED_GATE_WAIT=0
BUILD_PASSED=0
STAGE_EXIT=0
post_review_comment() { post_comment "$@"; }
'''
    (repo / 'inline.sh').write_text(header + release + '\n' + body + '\nexit "$STAGE_EXIT"\n')


def prepare(repo, case, mutation=''):
    # Reuse the foundation's real merge gates and GitHub fake, not replacements
    # for approval parsing, CI, base freshness or charter enforcement.
    foundation.prepare_pipeline(repo, 'unenrolled' if case == 'unenrolled' else 'clean',
                                history=lambda root, _: build_history(root, case))
    state = repo / 'stub_data'
    (state / 'queue-case').write_text(case)
    (state / 'linear.json').write_text(json.dumps(linear_value(case)))
    (state / 'linear-2.json').write_text(json.dumps(linear_value(case, True)))
    if case in ('approval-read-failed', 'exception-read-failed'):
        (state / 'linear-failed').touch()
    curl = repo / 'bin/curl'
    curl.write_text('#!/bin/sh\nexec python3 -I -B ' + shlex.quote(str(Path(__file__).resolve())) + ' --linear "$@"\n')
    curl.chmod(0o755)
    # The inline handoff uses the production shared state writer and release
    # helper. Its only substituted publications are the Linear boundary.
    for script in SCRIPTS.iterdir():
        target = repo / 'scripts' / script.name
        if script.is_file() and not target.exists(): shutil.copyfile(script, target)
    (repo / 'logs').mkdir(exist_ok=True)
    data = json.loads((state / 'pr_view.json').read_text())
    if case in ('unreadable-input', 'truncated-input'):
        data['baseRefName'] = 'release'
        (state / 'head-tree').write_text(git(repo, 'rev-parse', 'HEAD^{tree}'))
        original = (repo / 'bin/gh').rename(repo / 'bin/gh.foundation')
        wrapper = repo / 'bin/gh'
        wrapper.write_text('#!/bin/bash\npython3 -I -B ' + shlex.quote(str(Path(__file__).resolve()))
                           + ' --github-transport "$@"\nrc=$?\n[ "$rc" = 3 ] || exit "$rc"\nexec '
                           + shlex.quote(str(original)) + ' "$@"\n')
        wrapper.chmod(0o755)
    for comment in data['comments']:
        comment['body'] = comment['body'].replace('TEAM-' + str(1), ISSUE)
        if case in ('unreadable-input', 'truncated-input'):
            comment['body'] = comment['body'].replace('`main` at', '`release` at')
        if case == 'stale-exception':
            comment['body'] = comment['body'].replace(data['headRefOid'], data['baseRefOid'])
    (state / 'pr_view.json').write_text(json.dumps(data))
    if case == 'worktree-only':
        (repo / 'auth/entry.txt').write_text('uncommitted protected change\n')
        config = json.loads((repo / '.bureau.json').read_text())
        config['agents']['merge_charter'] = dict(enabled=False)
        (repo / '.bureau.json').write_text(json.dumps(config))
    inline_entry(repo)
    if mutation:
        target = repo / 'scripts/bureau-charter.py'
        old, new = {
            'queue': ('if not approved:', 'if False:'),
            'path': ('if path_match or label_match:', 'if label_match:'),
            'label': ('if path_match or label_match:', 'if path_match:'),
            'classes': ('json.dumps(classes)', 'json.dumps(classes[:1])'),
            'deleted': ('paths.append(path)', "if status != 'D': paths.append(path)"),
            'rename': ("count = 2 if status[0] in 'RC' else 1", 'count = 1'),
            'read': ('if failed and label_exceptions:', 'if False:'),
            'unclassified': ("return 25, 'charter-unclassified: ' + str(exc)", "return 0, ''"),
            'ambiguous': ('if len(bases) != 1:', 'if not bases:'),
            'priority': ('if lines:', 'if False:'),
            'credentials': ('bureau_load_env --export "$BUREAU_ENV_FILE" >&2 || exit; ', ''),
        }.get(mutation, ('', ''))
        if mutation == 'stale':
            target = repo / 'scripts/merge-pipeline.sh'
            old = 'charter-policy:*|charter-unreadable:*|charter-unclassified:*|charter-exception:*|*$\'\\n\'charter-policy:*|*$\'\\n\'charter-unreadable:*|*$\'\\n\'charter-unclassified:*|*$\'\\n\'charter-exception:*) return 0 ;;'
            new = ':) return 0 ;;'
        elif mutation == 'inline-handoff':
            target = repo / 'inline.sh'
            old, new = ('if mark_needs_human "$ISSUE" code-review; then', 'if false; then')
        elif mutation in ('outcome', 'dedicated-handoff', 'ticket', 'enrollment'):
            target = repo / 'scripts/merge-pipeline.sh'
            old, new = {
                'outcome': ('"charter-retry: "*|"charter-queue: "*) ;;', '"charter-retry: "*) ;;'),
                'dedicated-handoff': ('mark_needs_human "$ISSUE" merge 25 || true ;;', ': ;;'),
                'ticket': (' --issue "$ISSUE"', ''),
                'enrollment': ('if charter_enrolled; then', 'if true; then'),
            }[mutation]
        source = target.read_text()
        if not old or source.count(old) != (2 if mutation == 'enrollment' else 1):
            raise AssertionError('missing or ambiguous queue mutation: ' + mutation)
        target.write_text(source.replace(old, new))


class SelectorGitTests(unittest.TestCase):
    def test_changed_paths_are_root_relative_under_relative_diff_configuration(self):
        with tempfile.TemporaryDirectory(prefix='bureau-queue-relative-', dir=tempfile.gettempdir()) as scratch:
            repo = Path(scratch) / 'checkout'
            default, head = build_history(repo, 'path')
            nested = repo / 'nested'; nested.mkdir()
            def observe(module):
                with patch.dict(os.environ, GIT_CONFIG_COUNT='1', GIT_CONFIG_KEY_0='diff.relative', GIT_CONFIG_VALUE_0='true'):
                    self.assertIn('auth/entry.txt', module.changed_paths(nested, default, head))
            observe(charter)
            with self.assertRaises(AssertionError):
                observe(foundation.mutant("'--no-relative', ", ''))

    def test_live_target_tip_is_used_instead_of_default_or_cached_ref(self):
        with tempfile.TemporaryDirectory(prefix='bureau-queue-target-', dir=tempfile.gettempdir()) as scratch:
            repo = Path(scratch) / 'checkout'
            default, _ = build_history(repo, 'clean')
            git(repo, 'checkout', '-b', 'release', default)
            (repo / 'auth/entry.txt').write_text('already present on the target branch\n')
            git(repo, 'add', 'auth/entry.txt'); git(repo, 'commit', '-m', 'feat: target change')
            base = git(repo, 'rev-parse', 'HEAD')
            git(repo, 'checkout', '-B', 'feat/test', base)
            (repo / 'ordinary.txt').write_text('candidate based on live target\n')
            git(repo, 'add', 'ordinary.txt'); git(repo, 'commit', '-m', 'feat: target candidate')
            head = git(repo, 'rev-parse', 'HEAD')
            git(repo, 'push', 'origin', 'release', 'feat/test', '--force')
            git(repo, 'update-ref', 'refs/remotes/origin/release', default)
            class TargetSource(foundation.LocalGitHub):
                def api(self, route):
                    if route == '/commits/release': return dict(sha=base)
                    return super().api(route)
            source = TargetSource(repo, default)
            pin = source.pin(); charter.enroll(repo, source)
            def observe(module):
                with patch.object(module, 'issue_snapshot', return_value=dict(labels=['approved-for-merge'], cycle_id=None)):
                    self.assertEqual(module.gate(repo, source, pin, 'release', head, ISSUE), (0, ''))
            observe(charter)
            with self.assertRaises(AssertionError):
                observe(foundation.mutant('base = source.tip(base_branch)', "base = snapshot['default_commit']"))

    def test_local_objects_do_not_substitute_for_fetching_each_pinned_commit(self):
        for endpoint in ('base', 'head'):
            with self.subTest(endpoint=endpoint), tempfile.TemporaryDirectory(prefix='bureau-queue-fetch-', dir=tempfile.gettempdir()) as scratch:
                repo = Path(scratch) / 'checkout'
                default, head = build_history(repo, 'clean')
                if endpoint == 'base': git(repo, 'checkout', '-b', 'release', default)
                (repo / 'ordinary.txt').write_text('unpublished local commit\n')
                git(repo, 'add', 'ordinary.txt'); git(repo, 'commit', '-m', 'feat: unpublished candidate')
                unpublished = git(repo, 'rev-parse', 'HEAD')
                class MissingSource(foundation.LocalGitHub):
                    def api(self, route):
                        if route == '/commits/release': return dict(sha=unpublished)
                        return super().api(route)
                source = MissingSource(repo, default)
                pin = source.pin(); charter.enroll(repo, source)
                branch = 'release' if endpoint == 'base' else 'main'
                head = unpublished if endpoint == 'head' else head
                def observe(module):
                    with patch.object(module, 'issue_snapshot', return_value=dict(labels=['approved-for-merge'], cycle_id=None)):
                        code, line = module.gate(repo, source, pin, branch, head, ISSUE)
                    self.assertEqual(code, 25)
                    self.assertIn('charter-unclassified:', line)
                observe(charter)
                old = 'source.fetch(base); source.fetch(head)'
                new = 'source.fetch(head)' if endpoint == 'base' else 'source.fetch(base)'
                with self.assertRaises(AssertionError):
                    observe(foundation.mutant(old, new))

    def test_real_changes_and_guard_removal(self):
        for case, mutation in [('path', 'path'), ('renamed', 'rename'), ('copied', 'rename'), ('deleted', 'deleted'),
                               ('ambiguous', 'ambiguous')]:
            with self.subTest(case=case), tempfile.TemporaryDirectory(prefix='bureau-queue-', dir=tempfile.gettempdir()) as scratch:
                repo = Path(scratch) / 'checkout with spaces'
                default, head = build_history(repo, case)
                source = foundation.LocalGitHub(repo, default)
                pin = source.pin(); charter.enroll(repo, source)
                def observe(module):
                    code, line = module.gate(repo, source, pin, 'main', head, ISSUE)
                    self.assertEqual(code, 25)
                    self.assertIn('unclassified' if case == 'ambiguous' else 'security', line)
                # Only Linear is substituted; real Git fetch, merge-base, diff,
                # policy loading and all classification execute unchanged.
                with patch.object(charter, 'issue_snapshot', return_value=dict(labels=['approved-for-merge'], cycle_id=None)):
                    observe(charter)
                old, new = {'path': ('if path_match or label_match:', 'if label_match:'),
                            'rename': ("count = 2 if status[0] in 'RC' else 1", 'count = 1'),
                            'deleted': ('paths.append(path)', "if status != 'D': paths.append(path)"),
                            'ambiguous': ('if len(bases) != 1:', 'if not bases:')}[mutation]
                weakened = foundation.mutant(old, new)
                with patch.object(weakened, 'issue_snapshot', return_value=dict(labels=['approved-for-merge'], cycle_id=None)):
                    with self.assertRaises(AssertionError): observe(weakened)

    def test_doctor_reports_trusted_queue_classes_and_empty_list_warning(self):
        with tempfile.TemporaryDirectory(prefix='bureau-queue-doctor-', dir=tempfile.gettempdir()) as scratch:
            repo = Path(scratch) / 'checkout'
            default, _ = build_history(repo, 'cycle')
            config = json.loads((repo / '.bureau.json').read_text())
            source = foundation.LocalGitHub(repo, default)
            charter.enroll(repo, source)
            details = {}; errors, warnings = charter.diagnose(repo, config, details)
            self.assertEqual(errors, [])
            self.assertEqual(details['queue_mode'], 'cycle_id')
            self.assertEqual(details['exception_classes'], ['security', 'data'])
            self.assertFalse(any('no configured exception' in value for value in warnings))
            def observe_summary(module):
                details = {}; module.diagnose(repo, config, details)
                self.assertEqual(details['queue_mode'], 'cycle_id')
                self.assertEqual(details['exception_classes'], ['security', 'data'])
            observe_summary(charter)
            with self.assertRaises((AssertionError, KeyError)):
                observe_summary(foundation.mutant('if details is not None:', 'if False:'))
            git(repo, 'checkout', 'main')
            config['agents']['merge_charter']['exceptions'] = []
            (repo / '.bureau.json').write_text(json.dumps(config))
            git(repo, 'add', '.bureau.json'); git(repo, 'commit', '-m', 'chore: empty exceptions')
            git(repo, 'update-ref', 'refs/remotes/origin/main', git(repo, 'rev-parse', 'HEAD'))
            def observe_warning(module):
                self.assertTrue(any('no configured exception' in value for value in module.diagnose(repo, config)[1]))
            observe_warning(charter)
            with self.assertRaises(AssertionError):
                observe_warning(foundation.mutant("if record is not None and not policy['exceptions']:", 'if False:'))
            charter.enrollment_path(repo).unlink()
            def observe_unenrolled(module):
                self.assertFalse(any('no configured exception' in value for value in module.diagnose(repo, config)[1]))
            observe_unenrolled(charter)
            with self.assertRaises(AssertionError):
                observe_unenrolled(foundation.mutant("if record is not None and not policy['exceptions']:", "if not policy['exceptions']:"))


if __name__ == '__main__':
    os.environ.update(GIT_ALLOW_PROTOCOL='file', GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL=os.devnull,
                      PYTHONDONTWRITEBYTECODE='1')
    if sys.argv[1:2] == ['--linear']:
        sys.exit(linear_transport(sys.argv[2:]))
    if sys.argv[1:2] == ['--github-transport']:
        sys.exit(github_transport(sys.argv[2:]))
    if sys.argv[1:2] == ['--prepare']:
        prepare(Path(sys.argv[2]), *sys.argv[3:])
    else:
        unittest.main()
