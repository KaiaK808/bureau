"""Real stage checkouts and worker resets, with local Git and fake Linear."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

import git_maintenance
from process_helpers import communicate_until

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / 'templates/scripts'
BRANCH = 'test-branch'


def function(source, name):
    return re.search(r'^' + re.escape(name) + r'\(\) \{.*?^\}', source, re.M | re.S)[0]


def run(command, *, cwd, env):
    with subprocess.Popen(command, cwd=cwd, env=env, text=True, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, start_new_session=True) as child:
        try:
            out, err = communicate_until(child, time.monotonic() + 30)
        except BaseException:
            try:
                os.killpg(child.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            child.communicate(timeout=5)
            raise
        return subprocess.CompletedProcess(command, child.returncode, out, err)


class LocalCommitsHoldTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='bureau-local-commits-', dir=ROOT)
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.env = {k: v for k, v in os.environ.items()
                    if not k.startswith(('BUREAU_', 'FAKE_CLAUDE_', 'GH_STUB_', 'GIT_'))}
        self.env.update(git_maintenance.OFF_ENV, TMPDIR=str(self.root), PYTHONDONTWRITEBYTECODE='1')
        setup = run(['bash', '-euc', '''
source "$1/tests/lib/harness.sh"
sandbox_init fixture test-branch
trap - EXIT
printf '%s' "$SANDBOX"
''', 'fixture', str(ROOT)], cwd=ROOT, env=self.env)
        self.assertEqual(setup.returncode, 0, setup.stderr)
        self.repo = Path(setup.stdout).resolve()
        self.scripts = self.repo / 'scripts'
        self.git(*git_maintenance.OFF_CONFIG)
        self.git('--git-dir=' + str(self.repo / '.fake-origin.git'), *git_maintenance.OFF_CONFIG)
        (self.repo / '.gitignore').write_text('/*\n!/.gitignore\n!/specs/\n')
        self.git('add', '.gitignore', 'specs')
        self.git('commit', '-qm', 'fixture baseline')
        self.git('push', '-q', 'origin', BRANCH)
        self.origin = self.git('rev-parse', 'HEAD')
        (self.repo / '.bureau.json').write_text('{}')
        self.env.update(SANDBOX=str(self.repo), BUREAU_CONFIG=str(self.repo / '.bureau.json'),
                        BUREAU_STUB_BRANCH=BRANCH, BUREAU_STUB_STATE_QA='state-qa',
                        BUREAU_STUB_STATE_COPY='state-copy', BUREAU_ENV_FILE=str(self.repo / '.env'))
        self.production = (SCRIPTS / 'bureau-config.sh').read_text()
        with (self.scripts / 'bureau-config.sh').open('a') as out:
            out.write('\n' + function(self.production, 'emit_event') + '''
BUREAU_LABEL_NEEDS_COPY_NAME=needs-copy
merge_origin_main_or_abort() {
  git rev-parse HEAD > "$SANDBOX/checkout-head"
  echo 'checkout finished'
  exit 0
}
bureau_overlap_resume() { merge_origin_main_or_abort; }
''')

    def git(self, *args, repo=None):
        result = run(['git', '-C', str(repo or self.repo), *args], cwd=self.root, env=self.env)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.strip()

    def commit(self, name):
        path = self.repo / 'specs' / '001-test-branch' / (name + '.md')
        path.write_text(name + '\n')
        self.git('add', str(path))
        self.git('commit', '-qm', name)
        return self.git('rev-parse', 'HEAD')

    def branch_only(self):
        commit = self.commit('local branch fix')
        self.git('checkout', '-q', '--detach', self.origin)
        return commit

    def stage(self, stage='qa', **env):
        state = dict(qa='QA', copy='Copy', spec_review='Spec Review', ux='Design', implement='Build')[stage]
        return run(['bash', str(self.scripts / (stage.replace('_', '-') + '-pipeline.sh')), 'fixture'],
                   cwd=self.repo, env={**self.env, 'BUREAU_STUB_ISSUE_STATE': state, **env})

    def assert_held(self, result, commits):
        self.assertEqual(result.returncode, 30, 'expected a local-commit hold: ' + result.stdout + result.stderr)
        for commit in commits:
            self.assertIn(commit[:12], result.stderr)
            self.assertIn(self.git('show', '-s', '--format=%s', commit), result.stderr)
        self.assertIn('Push these commits to origin/' + BRANCH, result.stderr)
        self.assertIn('BUREAU_DROP_LOCAL_COMMITS=1', result.stderr)
        self.assertNotIn('checkout finished', result.stdout)
        self.assertFalse((self.repo / 'checkout-head').exists())
        calls = (self.repo / 'calls.log').read_text()
        self.assertIn('add_issue_label\tfixture\tneeds-human', calls)
        self.assertNotIn('move_issue', calls)

    def test_qa_local_branch_ref_holds_without_moving_refs_or_files(self):
        commit = self.branch_only()
        before = self.git('status', '--porcelain')
        self.assert_held(self.stage(), [commit])
        self.assertEqual(self.git('rev-parse', 'refs/heads/' + BRANCH), commit)
        self.assertEqual(self.git('rev-parse', 'HEAD'), self.origin)
        self.assertEqual(self.git('branch', '--show-current'), '')
        self.assertEqual(self.git('status', '--porcelain'), before)

    def test_qa_detached_head_holds_when_branch_equals_origin(self):
        self.git('checkout', '-q', '--detach')
        commit = self.commit('detached fix')
        self.assert_held(self.stage(), [commit])
        self.assertEqual(self.git('rev-parse', 'HEAD'), commit)
        self.assertEqual(self.git('rev-parse', 'refs/heads/' + BRANCH), self.origin)
        self.assertEqual(self.git('branch', '--show-current'), '')
        self.assertTrue((self.repo / 'specs/001-test-branch/detached fix.md').exists())

    def test_both_tips_are_reported_once(self):
        branch_commit = self.branch_only()
        head_commit = self.commit('detached fix')
        result = self.stage()
        self.assert_held(result, [branch_commit, head_commit])
        for commit in (branch_commit, head_commit):
            self.assertEqual(result.stderr.count(commit[:12]), 1)

    def test_equal_origin_proceeds_without_new_output_or_event(self):
        result = self.stage()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('checkout finished', result.stdout)
        self.assertNotIn('local-only', result.stdout + result.stderr)
        self.assertFalse((self.repo / 'logs/events.jsonl').exists())
        self.assertEqual((self.repo / 'checkout-head').read_text().strip(), self.origin)
        self.assertEqual(self.git('branch', '--show-current'), BRANCH)

    def test_missing_local_branch_proceeds(self):
        self.git('checkout', '-q', '--detach')
        self.git('branch', '-D', BRANCH)
        result = self.stage()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.git('rev-parse', 'refs/heads/' + BRANCH), self.origin)

    def test_main_ahead_no_hold(self):
        self.git('checkout', '-q', 'main')
        self.git('commit', '-q', '--allow-empty', '-m', 'unrelated main change')
        head = self.git('rev-parse', 'HEAD')
        result = run(['bash', '-euc', '''
source "$1/bureau-config.sh"
bureau_guard_branch_reset "$2"
''', str(self.scripts / 'fixture'), str(self.scripts), BRANCH], cwd=self.repo, env=self.env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout + result.stderr, '')
        self.assertEqual(self.git('rev-parse', 'HEAD'), head)
        self.assertEqual(self.git('branch', '--show-current'), 'main')
        self.assertEqual(self.git('rev-parse', 'refs/heads/' + BRANCH), self.origin)

    def test_override_resets_both_tips_and_records_full_shas(self):
        branch_commit = self.branch_only()
        head_commit = self.commit('detached fix')
        result = self.stage(BUREAU_DROP_LOCAL_COMMITS='1')
        self.assertEqual(result.returncode, 0, result.stderr)
        events = [json.loads(line) for line in (self.repo / 'logs/events.jsonl').read_text().splitlines()]
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]['event'], 'local_commits_dropped')
        self.assertEqual(set(events[0]['commits'].split(',')), {branch_commit, head_commit})
        self.assertEqual(events[0]['stage'], 'qa-pipeline.sh')
        for commit in (branch_commit, head_commit):
            self.assertIn(commit, result.stderr)
        self.assertEqual(self.git('rev-parse', 'refs/heads/' + BRANCH), self.origin)
        self.assertEqual(self.git('rev-parse', 'HEAD'), self.origin)
        self.assertEqual((self.repo / 'checkout-head').read_text().strip(), self.origin)

    def test_other_checkouts_use_the_guard(self):
        commit = self.branch_only()
        for stage in ('copy', 'spec_review', 'ux', 'implement'):
            with self.subTest(stage=stage):
                self.assert_held(self.stage(stage), [commit])
                self.assertEqual(self.git('rev-parse', 'refs/heads/' + BRANCH), commit)

    def test_qa_plain_checkout_negative_control(self):
        commit = self.branch_only()
        qa = self.scripts / 'qa-pipeline.sh'
        guard = '  bureau_guard_branch_reset "$BRANCH" || exit $?\n'
        self.assertEqual(qa.read_text().count(guard), 1)
        qa.write_text(qa.read_text().replace(guard, '', 1))
        result = self.stage()
        with self.assertRaisesRegex(AssertionError, 'expected a local-commit hold'):
            self.assert_held(result, [commit])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.git('rev-parse', 'refs/heads/' + BRANCH), self.origin)
        self.assertIn('checkout finished', result.stdout)

    def worker(self, wt=None):
        self.git('checkout', '-q', '--detach', self.origin)
        wt = wt or self.root / 'worker'
        self.git('worktree', 'add', '-q', '--detach', str(wt), self.origin)
        common = Path(self.git('rev-parse', '--absolute-git-dir'))
        registry = common / 'bureau/workers'
        registry.mkdir(parents=True)
        key = hashlib.sha256(str(wt.resolve()).encode()).hexdigest()
        (registry / key).write_text(self.git('rev-parse', '--absolute-git-dir', repo=wt) + '\n')
        # Ownership is orthogonal; keep the real reset and Git commands, fake
        # only the runtime's already-tested lease assertion.
        runtime = self.root / 'owner.py'
        runtime.write_text('import sys\nassert "assert-owner" in sys.argv\n')
        with (self.scripts / 'bureau-config.sh').open('a') as out:
            for name in ('reset_worktree', 'free_branch_from_other_worktrees', 'bureau_preserve_note',
                         '_bureau_reaccept_clean_worktree', '_bureau_worktree_reuse_state',
                         '_bureau_reuse_unchanged', '_bureau_reuse_base_ref', '_bureau_physical'):
                out.write('\n' + function(self.production, name) + '\n')
            out.write('bureau_link_worktree_paths() { :; }\n')
        self.env.update(BUREAU_WORKSPACE_MODE='disposable', BUREAU_RUN_ID='fixture',
                        BUREAU_CURRENT_ISSUE='fixture')
        self.runtime = runtime
        return wt

    def reset(self, wt, **env):
        return run(['bash', '-euc', '''
source "$1/bureau-config.sh"
REPO_DIR="$2"
BUREAU_RUNTIME="$4"
reset_worktree "$3" qa-pipeline.sh test-branch
''', str(self.scripts / 'fixture'), str(self.scripts), str(self.repo), str(wt), str(self.runtime)],
                   cwd=self.repo, env={**self.env, **env})

    def test_worker_reset_holds_branch_ref_before_cleaning(self):
        commit = self.branch_only()
        wt = self.worker()
        (wt / 'untracked').write_text('keep')
        result = self.reset(wt)
        self.assert_held(result, [commit])
        self.assertEqual(self.git('rev-parse', 'refs/heads/' + BRANCH), commit)
        self.assertEqual((wt / 'untracked').read_text(), 'keep')
        self.assertEqual(self.git('rev-parse', 'HEAD', repo=wt), self.origin)

    def test_worker_reset_holds_detached_head_until_pushed(self):
        wt = self.worker()
        self.git('commit', '-q', '--allow-empty', '-m', 'detached worker fix', repo=wt)
        commit = self.git('rev-parse', 'HEAD', repo=wt)
        self.assert_held(self.reset(wt), [commit])
        self.assertEqual(self.git('rev-parse', 'HEAD', repo=wt), commit)
        result = self.reset(wt)
        self.assertEqual(result.returncode, 21, result.stderr)
        self.assertIn('refusing to reset unregistered worktree', result.stderr)
        self.assertEqual(self.git('rev-parse', 'HEAD', repo=wt), commit)
        self.git('push', '-q', 'origin', commit + ':refs/heads/' + BRANCH)
        result = self.reset(wt)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('Re-accepted the clean preserved worktree', result.stderr)
        self.assertEqual(self.git('rev-parse', 'HEAD', repo=wt), commit)
        key = hashlib.sha256(str(wt.resolve()).encode()).hexdigest()
        common = Path(self.git('rev-parse', '--absolute-git-dir'))
        self.assertFalse((common / 'bureau/preserved' / (key + '.json')).exists())
        self.assertTrue((common / 'bureau/workers' / key).is_file())

    def worker_override_resets_and_logs_drop(self, branch_only=False):
        if branch_only:
            commit = self.branch_only()
        wt = self.worker()
        if not branch_only:
            self.git('commit', '-q', '--allow-empty', '-m', 'detached worker fix', repo=wt)
            commit = self.git('rev-parse', 'HEAD', repo=wt)
        self.assert_held(self.reset(wt), [commit])
        result = self.reset(wt, BUREAU_DROP_LOCAL_COMMITS='1')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.git('rev-parse', 'HEAD', repo=wt), self.origin)
        self.assertEqual(self.git('rev-parse', 'refs/heads/' + BRANCH), self.origin)
        events = [json.loads(line) for line in (self.repo / 'logs/events.jsonl').read_text().splitlines()]
        self.assertEqual(len(events), 1)
        self.assertEqual((events[0]['event'], events[0]['commits']), ('local_commits_dropped', commit))
        self.assertIn(commit, result.stderr)
        key = hashlib.sha256(str(wt.resolve()).encode()).hexdigest()
        common = Path(self.git('rev-parse', '--absolute-git-dir'))
        self.assertFalse((common / 'bureau/preserved' / (key + '.json')).exists())
        self.assertEqual((common / 'bureau/workers' / key).read_text().strip(),
                         self.git('rev-parse', '--absolute-git-dir', repo=wt))

    def test_worker_override_resets_detached_head_and_logs_drop(self):
        self.worker_override_resets_and_logs_drop()

    def test_worker_override_resets_branch_ref_and_logs_drop(self):
        self.worker_override_resets_and_logs_drop(branch_only=True)

    def worker_hold_override_requires_matching_preservation_record(self, branch_only=False):
        if branch_only:
            commit = self.branch_only()
        wt = self.worker()
        if not branch_only:
            self.git('commit', '-q', '--allow-empty', '-m', 'detached worker fix', repo=wt)
            commit = self.git('rev-parse', 'HEAD', repo=wt)
        self.assert_held(self.reset(wt), [commit])
        head = self.git('rev-parse', 'HEAD', repo=wt)
        branch_head = self.git('rev-parse', 'refs/heads/' + BRANCH)
        ignored = wt / 'ignored-fixture'
        ignored.write_text('keep')
        key = hashlib.sha256(str(wt.resolve()).encode()).hexdigest()
        common = Path(self.git('rev-parse', '--absolute-git-dir'))
        note = common / 'bureau/preserved' / (key + '.json')
        original = json.loads(note.read_text())
        for field, value in (('issue', 'other'), ('workspace', str(self.root / 'other')),
                             ('reset_branch', 'other-branch'), ('git_dir', str(common)),
                             ('git_dir', None), ('reason', 'interrupted'), ('exit_code', 22)):
            for missing in (False, True):
                with self.subTest(field=field, value=value, missing=missing):
                    record = {**original, field: value}
                    if missing:
                        record.pop(field)
                    note.write_text(json.dumps(record))
                    result = self.reset(wt, BUREAU_DROP_LOCAL_COMMITS='1')
                    self.assertEqual(result.returncode, 21, result.stderr)
                    self.assertEqual(self.git('rev-parse', 'HEAD', repo=wt), head)
                    self.assertEqual(self.git('rev-parse', 'refs/heads/' + BRANCH), branch_head)
                    self.assertEqual(ignored.read_text(), 'keep')
                    self.assertEqual(json.loads(note.read_text()), record)
                    self.assertFalse((common / 'bureau/workers' / key).exists())
                    self.assertFalse((self.repo / 'logs/events.jsonl').exists())

    def test_worker_hold_override_requires_matching_preservation_record(self):
        self.worker_hold_override_requires_matching_preservation_record()

    def test_worker_branch_ref_hold_override_requires_matching_preservation_record(self):
        self.worker_hold_override_requires_matching_preservation_record(branch_only=True)

    def test_worker_hold_override_requires_clean_worktree(self):
        wt = self.worker()
        self.git('commit', '-q', '--allow-empty', '-m', 'detached worker fix', repo=wt)
        commit = self.git('rev-parse', 'HEAD', repo=wt)
        self.assert_held(self.reset(wt), [commit])
        tracked = wt / 'specs/001-test-branch/tasks.md'
        original = tracked.read_text()
        for path in (tracked, wt / 'specs/001-test-branch/untracked.md'):
            with self.subTest(path=path.name):
                path.write_text('keep this change\n')
                result = self.reset(wt, BUREAU_DROP_LOCAL_COMMITS='1')
                self.assertEqual(result.returncode, 21, result.stderr)
                self.assertEqual(path.read_text(), 'keep this change\n')
                self.assertEqual(self.git('rev-parse', 'HEAD', repo=wt), commit)
                self.assertFalse((self.repo / 'logs/events.jsonl').exists())
                if path == tracked:
                    path.write_text(original)
                else:
                    path.unlink()

    def test_worker_hold_override_requires_ownership(self):
        wt = self.worker()
        self.git('commit', '-q', '--allow-empty', '-m', 'detached worker fix', repo=wt)
        commit = self.git('rev-parse', 'HEAD', repo=wt)
        self.assert_held(self.reset(wt), [commit])
        self.runtime.write_text('raise SystemExit(21)\n')
        result = self.reset(wt, BUREAU_DROP_LOCAL_COMMITS='1')
        self.assertEqual(result.returncode, 21, result.stderr)
        self.assertEqual(self.git('rev-parse', 'HEAD', repo=wt), commit)
        self.assertFalse((self.repo / 'logs/events.jsonl').exists())

    def test_reused_pushed_head_no_hold(self):
        wt = self.worker()
        self.git('commit', '-q', '--allow-empty', '-m', 'previous ticket change', repo=wt)
        commit = self.git('rev-parse', 'HEAD', repo=wt)
        self.git('push', '-q', 'origin', commit + ':refs/heads/previous-branch')
        result = self.reset(wt)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn('local-only', result.stdout + result.stderr)
        self.assertEqual(self.git('rev-parse', 'HEAD', repo=wt), self.origin)
        self.assertEqual(self.git('rev-parse', 'refs/remotes/origin/previous-branch'), commit)

    def test_other_branch_unpushed_no_hold(self):
        wt = self.worker()
        self.git('commit', '-q', '--allow-empty', '-m', 'previous ticket local change', repo=wt)
        commit = self.git('rev-parse', 'HEAD', repo=wt)
        self.git('branch', 'previous-branch', commit)
        result = self.reset(wt)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn('local-only', result.stdout + result.stderr)
        self.assertEqual(self.git('rev-parse', 'HEAD', repo=wt), self.origin)
        self.assertEqual(self.git('rev-parse', 'refs/heads/previous-branch'), commit)

    def test_detached_commit_after_ref_move_holds(self):
        wt = self.worker()
        self.git('checkout', '-q', BRANCH, repo=wt)
        self.git('commit', '-q', '--allow-empty', '-m', 'detached worker fix', repo=wt)
        commit = self.git('rev-parse', 'HEAD', repo=wt)
        self.git('checkout', '-q', '--detach', repo=wt)
        self.git('update-ref', 'refs/heads/' + BRANCH, self.origin, commit)
        self.assert_held(self.reset(wt), [commit])
        self.assertEqual(self.git('rev-parse', 'HEAD', repo=wt), commit)
        self.assertEqual(self.git('rev-parse', 'refs/heads/' + BRANCH), self.origin)

    def test_new_worker_checks_branch_without_using_driver_head(self):
        commit = self.branch_only()
        wt = self.worker()
        self.git('worktree', 'remove', str(wt))
        self.assert_held(self.reset(wt), [commit])
        self.assertFalse(wt.exists())
        self.git('branch', '-D', BRANCH)
        self.git('commit', '-q', '--allow-empty', '-m', 'unrelated driver commit')
        result = self.reset(wt)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.git('rev-parse', 'HEAD', repo=wt), self.origin)

    def dispatcher(self):
        spec = importlib.util.spec_from_file_location('local_hold_dispatch', SCRIPTS / 'bureau-dispatch.py')
        dispatch = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(dispatch)

        class Helpers:
            def call(self, *_):
                raise AssertionError('a known hold must not take the unknown-crash path')

        driver = object.__new__(dispatch.Dispatcher)
        driver.skips = {}
        driver.poll = driver.recheck = 1
        driver.path = self.root / 'dispatch-skips.json'
        driver.helpers = Helpers()
        driver.repo = self.repo
        driver.store = dispatch.r.Store(self.repo)
        driver.journal = driver.store.root / 'dispatch-lanes'
        return dispatch, driver

    def test_dispatcher_reconciliation_preserves_detached_commit(self):
        dispatch, driver = self.dispatcher()
        attempt = 'a' * 32
        issue = 'TEST-' + str(os.getpid())
        wt = self.worker(self.repo / '.worktrees' / ('dispatch-' + issue + '-' + attempt))
        self.git('commit', '-q', '--allow-empty', '-m', 'detached worker fix', repo=wt)
        commit = self.git('rev-parse', 'HEAD', repo=wt)
        reflog = self.git('reflog', 'show', 'HEAD', repo=wt)
        result = self.reset(wt, BUREAU_CURRENT_ISSUE=issue)
        self.assertEqual(result.returncode, 30, result.stderr)
        path = driver.journal / (attempt + '.json')
        dispatch.r.save(path, dict(attempt=attempt, issue=issue, workspace=str(wt), code=30))
        with patch.object(dispatch.r.t, 'Ledger') as ledger:
            ledger.return_value.call.return_value = None
            driver.reconcile_lane(path)
        self.assertFalse(path.exists())
        self.assertTrue(wt.is_dir(), 'dispatcher removed the held detached worktree')
        self.assertEqual(self.git('rev-parse', 'HEAD', repo=wt), commit)
        self.assertEqual(self.git('reflog', 'show', 'HEAD', repo=wt), reflog)
        key = hashlib.sha256(str(wt.resolve()).encode()).hexdigest()
        note = json.loads((driver.store.root / 'preserved' / (key + '.json')).read_text())
        self.assertEqual((note['issue'], note['reason'], note['exit_code']), (issue, 'unfinished', 30))
        self.assertFalse((driver.store.root / 'workers' / key).exists())
        self.assertIsNone(driver.skips[issue]['until'])

    def test_tick_reports_blocked_and_preserves_exit_30(self):
        with (self.scripts / 'bureau-config.sh').open('a') as out:
            out.write('\n' + function(self.production, 'bureau_is_paused') + '\n'
                      + 'session_throttle_guard() { return 0; }\n')
        (self.scripts / 'bureau-worker.sh').write_text('#!/bin/bash\nexit 30\n')
        result = run(['bash', str(self.scripts / 'bureau-tick.sh'), '--stage', 'qa'], cwd=self.repo,
                     env={**self.env, 'BUREAU_STUB_AGENT_ENABLED': 'qa',
                          'BUREAU_STUB_PICKED_ISSUE': 'TEST-' + str(os.getpid()),
                          'BUREAU_STUB_ISSUE_STATE': 'QA'})
        self.assertEqual(result.returncode, 30, result.stdout + result.stderr)
        record = json.loads((self.repo / 'logs/bureau-tick.json').read_text())
        self.assertEqual((record['outcome'], record['exit_code'], record['stage']), ('blocked', 30, 'qa'))
        self.assertEqual(json.loads(result.stdout), record)

    def test_hold_classification(self):
        with (self.scripts / 'bureau-config.sh').open('a') as out:
            out.write('\n' + function(self.production, 'exit_class') + '\n'
                      + function(self.production, 'shepherd_rc_action') + '\n')
        result = run(['bash', '-euc', '''
source "$1/bureau-config.sh"
exit_class 30
shepherd_rc_action 30
bash "$1/bureau-insights.sh" 30
''', str(self.scripts / 'fixture'), str(self.scripts)], cwd=self.repo, env=self.env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines(), ['local-commits-held', 'halt', 'human_hold'])
        dispatch, driver = self.dispatcher()
        with patch.object(dispatch.r.t, 'Ledger') as ledger:
            ledger.return_value.call.return_value = None
            driver.defer('fixture', 30, terminal_reason=None)
        self.assertIsNone(driver.skips['fixture']['until'])


if __name__ == '__main__':
    unittest.main()
