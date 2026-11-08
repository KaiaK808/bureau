"""Restart admission with local Git and fake GitHub, Linear and stages."""
import json
import os
import shlex
import unittest

from local_commits_hold_test import BRANCH, SCRIPTS, LocalCommitsFixture, function, run


class ShepherdPrPreflightTests(LocalCommitsFixture):
    def setUp(self):
        super().setUp()
        self.issue = 'TEST-' + str(os.getpid())
        self.shepherd_path = self.scripts / 'shepherd.sh'
        (self.repo / '.bureau.json').write_text(json.dumps(dict(
            repo=dict(base_branch='release/next'),
            linear=dict(teams=[dict(states={key: 'state-' + key.replace('_', '-')
                                           for key in ('build', 'qa', 'build_review', 'merge', 'done')})]))))
        self.env.update(BUREAU_IMPL_PR_RETRIES='2', BUREAU_IMPL_PR_RETRY_WAIT='0',
                        BUREAU_STUB_STATE_MERGE='state-merge', BUREAU_SHEPHERD_CONFIRM_SECONDS='0')
        with (self.scripts / 'bureau-config.sh').open('a') as out:
            for name in ('bureau_ticket_state_class', 'exit_class', 'shepherd_rc_action'):
                out.write('\n' + function(self.production, name) + '\n')
            out.write('''
_record() { printf '%s\\n' "$*" >> "$SANDBOX/actions"; }
bureau_exec_runtime() {
  _record runtime_claim
  export BUREAU_ACTIVE_ENTRY="$0"
  exec bash "$0" --no-tmux "${ORIG_ARGS[@]}"
}
bureau_issue_snapshot() {
  jq -nc --arg issue "$1" --arg state "$(cat "$SANDBOX/state")" \
    '{identifier:$issue,state:{id:$state}}'
}
get_issue_state() {
  case "$(cat "$SANDBOX/state")" in
    state-build) echo Build ;; state-qa) echo QA ;;
    state-build-review) echo 'Build Review' ;; state-merge) echo Merge ;;
    state-done) echo Done ;;
  esac
}
move_issue() { _record move_issue "$@"; echo "$2" > "$SANDBOX/state"; }
session_throttle_guard() { :; }
bureau_observe_waits() { :; }
''')
        (self.repo / 'state').write_text('state-build\n')
        (self.scripts / 'bureau-worker.sh').write_text('''#!/bin/bash
exec bash "$(dirname "$0")/$2" "$1"
''')
        for stage, state in (('implement', 'state-qa'), ('qa', 'state-build-review'),
                             ('code-review', 'state-done'), ('merge', 'state-done')):
            (self.scripts / (stage + '-pipeline.sh')).write_text('''#!/bin/bash
echo "${0##*/}" >> "$SANDBOX/stages"
if [ "${0##*/}" = code-review-pipeline.sh ] && [ "${PR_MODE:-open}" = missing ]; then
  echo 'no open PR for branch' >&2
  exit 15
fi
echo ''' + state + ''' > "$SANDBOX/state"
''')
        bin_dir = self.root / 'bin'
        bin_dir.mkdir()
        gh = bin_dir / 'gh'
        gh.write_text('''#!/bin/bash
set -euo pipefail
[ "$*" = "pr list --head $BUREAU_STUB_BRANCH --json number --jq .[0].number" ] || exit 99
echo lookup >> "$SANDBOX/lookups"
case "${PR_MODE:-open}" in
  missing) ;;
  null) echo null ;;
  invalid) echo broken ;;
  auth) echo 'HTTP 401: Bad credentials' >&2; exit 1 ;;
  transient) echo 'HTTP 503: Service Unavailable' >&2; exit 1 ;;
  recover)
    if [ "$(wc -l < "$SANDBOX/lookups")" -lt 3 ]; then
      echo 'connection reset' >&2; exit 1
    fi
    echo 7 ;;
  *) echo 7 ;;
esac
''')
        gh.chmod(0o755)
        self.env['PATH'] = str(bin_dir) + os.pathsep + self.env['PATH']

    def shepherd(self, stage, mode='missing', **env):
        return run(['bash', str(self.shepherd_path), '--no-tmux', '--from-stage', stage, self.issue],
                   cwd=self.repo, env={**self.env, 'PR_MODE': mode, **env})

    def lines(self, name):
        path = self.repo / name
        return path.read_text().splitlines() if path.exists() else []

    def assert_unclaimed(self):
        actions = self.lines('actions')
        for mutation in ('runtime_claim', 'add_issue_label', 'remove_issue_label', 'move_issue', 'post_comment'):
            self.assertFalse(any(line.startswith(mutation) for line in actions), actions)
        self.assertEqual(self.lines('stages'), [])
        self.assertFalse((self.repo / '.git/bureau/leases.json').exists())

    def test_qa_without_pr_holds_before_claim_or_stage(self):
        result = self.shepherd('qa')
        self.assertEqual(result.returncode, 31, result.stdout + result.stderr)
        self.assertIn(BRANCH, result.stderr)
        self.assertIn('no open pull request', result.stderr)
        self.assertIn('gh pr create --head ' + BRANCH + ' --base release/next --draft --fill', result.stderr)
        restart = next(line.split('Restart: ', 1)[1] for line in result.stderr.splitlines() if 'Restart:' in line)
        self.assertEqual(shlex.split(restart), [str(self.shepherd_path), '--no-tmux', '--from-stage', 'qa', self.issue])
        self.assertEqual(self.lines('lookups'), ['lookup'])
        self.assert_unclaimed()

    def test_all_later_starts_require_pr(self):
        for stage in ('QA', 'build-review', 'merge', 'done'):
            with self.subTest(stage=stage):
                result = self.shepherd(stage, mode='null')
                self.assertEqual(result.returncode, 31, result.stdout + result.stderr)
                self.assert_unclaimed()

    def test_open_pr_starts_qa_and_review(self):
        for stage, expected in (('qa', ['qa-pipeline.sh', 'code-review-pipeline.sh']),
                                ('build_review', ['code-review-pipeline.sh'])):
            with self.subTest(stage=stage):
                (self.repo / 'stages').write_text('')
                result = self.shepherd(stage, mode='open')
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(self.lines('stages'), expected)
                self.assertNotIn('Open PR:', result.stdout + result.stderr)
                self.assertIn('runtime_claim', self.lines('actions'))

    def test_lookup_errors_are_not_missing_pr(self):
        for mode, count in (('auth', 1), ('transient', 3), ('invalid', 1)):
            with self.subTest(mode=mode):
                (self.repo / 'lookups').write_text('')
                result = self.shepherd('qa', mode=mode)
                self.assertEqual(result.returncode, 18, result.stdout + result.stderr)
                self.assertIn('PR lookup', result.stderr)
                self.assertNotIn('Open PR:', result.stderr)
                self.assertEqual(len(self.lines('lookups')), count)
                self.assert_unclaimed()

    def test_transient_lookup_recovery_runs_qa(self):
        result = self.shepherd('qa', mode='recover')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.lines('stages'), ['qa-pipeline.sh', 'code-review-pipeline.sh'])
        self.assertEqual(len(self.lines('lookups')), 4)  # Startup and runtime re-entry.

    def test_missing_origin_branch_holds(self):
        self.git('push', '-q', 'origin', '--delete', BRANCH)
        result = self.shepherd('qa', mode='open')
        self.assertEqual(result.returncode, 31, result.stdout + result.stderr)
        self.assertIn('branch does not exist on origin', result.stderr)
        self.assertIn('git push origin ' + BRANCH, result.stderr)
        self.assertEqual(self.lines('lookups'), [])
        self.assert_unclaimed()

    def test_origin_error_is_not_missing_branch(self):
        self.git('remote', 'set-url', 'origin', str(self.root / 'missing-origin'))
        result = self.shepherd('qa')
        self.assertEqual(result.returncode, 18, result.stdout + result.stderr)
        self.assertIn('origin lookup failed', result.stderr)
        self.assertNotIn('Open PR:', result.stderr)
        self.assert_unclaimed()

    def test_build_without_pr_still_runs_build(self):
        result = self.shepherd('build')
        self.assertEqual(result.returncode, 15, result.stdout + result.stderr)
        self.assertEqual(self.lines('stages'), ['implement-pipeline.sh', 'qa-pipeline.sh', 'code-review-pipeline.sh'])
        self.assertEqual(self.lines('lookups'), [])
        self.assertNotIn('Open PR:', result.stdout + result.stderr)

    def test_review_retains_missing_pr_exit_and_distinguishes_lookup_error(self):
        review = self.scripts / 'review-under-test.sh'
        review.write_text((SCRIPTS / 'code-review-pipeline.sh').read_text())
        for mode, code in (('missing', 15), ('auth', 18)):
            with self.subTest(mode=mode):
                (self.repo / 'state').write_text('state-build-review\n')
                (self.repo / 'actions').write_text('')
                result = run(['bash', str(review), self.issue], cwd=self.repo,
                             env={**self.env, 'PR_MODE': mode})
                self.assertEqual(result.returncode, code, result.stdout + result.stderr)
                moved = any(line.startswith('move_issue') for line in self.lines('actions'))
                self.assertEqual(moved, mode == 'missing', self.lines('actions'))
                self.assertIn('no PR found' if mode == 'missing' else 'PR lookup failed',
                              result.stdout + result.stderr)

    def test_queue_classifies_exit_31_as_human_hold(self):
        (self.scripts / 'bureau-worker.sh').write_text('#!/bin/bash\nexit 31\n')
        probe = self.root / 'queue-probe.sh'
        probe.write_text('''#!/bin/bash
source "$1/bureau-config.sh"
REPO_DIR="$SANDBOX"
LOG_FILE="$SANDBOX/queue.log"
MODE=qa
preselect_issue() { echo "$BUREAU_STUB_PICKED_ISSUE"; }
''' + function((SCRIPTS / 'queue-loop.sh').read_text(), 'run_script') + '''
run_script qa-pipeline.sh QA "$SANDBOX/worker"
''')
        result = run(['bash', str(probe), str(self.scripts)], cwd=self.repo,
                     env={**self.env, 'BUREAU_STUB_PICKED_ISSUE': self.issue})
        self.assertEqual(result.returncode, 31, result.stdout + result.stderr)
        self.assertIn('held for a human: open PR required', result.stdout)
        self.assertNotIn('error (exit', result.stdout)

    def test_missing_pr_negative_control_reaches_qa_then_review_exit_15(self):
        guard = '  qa|build_review|merge|done) _shepherd_from_stage_pr_check ;;\n'
        source = self.shepherd_path.read_text()
        self.assertEqual(source.count(guard), 1)
        self.shepherd_path.write_text(source.replace(guard, '', 1))
        result = self.shepherd('qa')
        self.assertEqual(result.returncode, 15, result.stdout + result.stderr)
        self.assertEqual(self.lines('stages'), ['qa-pipeline.sh', 'code-review-pipeline.sh'])
        self.assertTrue(any(line.startswith('add_issue_label') for line in self.lines('actions')))
        self.assertTrue(any(line.startswith('move_issue') for line in self.lines('actions')))


if __name__ == '__main__':
    unittest.main()
