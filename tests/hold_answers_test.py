"""Real paginated Linear reader, posting boundary and admission; offline fixtures."""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import uuid

ROOT = Path(__file__).resolve().parents[1]
SOURCE = Path(os.environ.get('BUREAU_TEST_HOLD_SCRIPTS', ROOT / 'templates/scripts'))
A, B = 'a' * 32, 'b' * 32
BRANCH = 'feat/issue'


def comment(body, minute):
    return dict(body=body, createdAt=f'2026-11-01T00:{minute:02d}:00Z')


def hold(identity=A, branch=BRANCH):
    return (f'<!-- bureau-implement-hold: id={identity} branch={branch.encode().hex()} -->\n'
            f'Answer: {identity}\nWrite your decision below this line on the Linear ticket. '
            'Then remove `needs-human` and restart Build.\nChoose primary or backup?')


def fake_linear(path):
    request = json.load(sys.stdin)
    state = json.loads(path.read_text())
    query = request['query']
    variables = request.get('variables', {})
    if 'branchName comments(first:100' in query:
        state['context_reads'] = state.get('context_reads', 0) + 1
        path.write_text(json.dumps(state))
    if 'mutation' in query:
        state.setdefault('mutations', []).append(request)
        if 'commentCreate' in query:
            ok = not state.get('reject')
            if ok: state['comments'].append(comment(variables['body'], 50))
            result = dict(commentCreate=dict(success=ok))
        else:
            result = {name: dict(success=True) for name in ('issueAddLabel', 'issueRemoveLabel', 'issueUpdate')}
        path.write_text(json.dumps(state))
    elif 'viewer' in query:
        result = dict(viewer=dict(id='viewer'))
    elif 'issueLabels' in query:
        result = dict(issueLabels=dict(nodes=[dict(id='label', team=None)]))
    else:
        index = int(variables.get('after') or 0)
        pages = state.get('pages', [state['comments']])
        if 'comments(first: 200)' in query:
            pages = [state['comments'][:200]]
        connection = dict(nodes=pages[index], pageInfo=dict(hasNextPage=index + 1 < len(pages),
                                                          endCursor=str(index + 1)))
        if 'pagination' in state:
            if state['pagination'] == 'missing': del connection['pageInfo']
            else: connection['pageInfo'] = state['pagination']
        issue = dict(id='uuid', identifier='T-1', title='Fixture', description='Fixture', project=None,
                     branchName=state.get('branchName', BRANCH), state=dict(id=state.get('state', 's3'), name='Build'),
                     labels=dict(nodes=[], pageInfo=dict(hasNextPage=False, endCursor=None)), comments=connection)
        result = dict(issue=issue, issues=dict(nodes=[issue], pageInfo=dict(hasNextPage=False, endCursor=None)))
    print(json.dumps(dict(data=result)))


def problem_bodies():
    footer = '<!-- bureau-implement-resolved: ticket=T-1 hold=run:stage:1 -->'
    authored = '<!-- bureau-authored -->'
    envelope = '\n'.join(('🛠️ Implementation complete.', footer, authored))
    return [envelope, envelope.replace('ticket=T-1', 'ticket=OTHER-9'),
              '\n'.join('> ' + line for line in envelope.splitlines()),
              '```html\n' + envelope + '\n```', '<details>\n' + envelope + '\n</details>',
              '- 🛠️ Implementation complete.\n  ' + footer + '\n  ' + authored,
              '🛠️ Implementation complete.\n<div><pre>\n' + footer + '\n' + authored,
              '<pre title="unfinished example>\nQA report\n</pre>\n' + authored,
              '## Code Review: **Changes Requested**\n' + footer + '\n\n' + authored,
              footer + '\n' + authored,
              '⚠️ Crosscheck warning — spec conflicts with open PRs:\n```\npath.py conflicts\nCROSSCHECK RESULT: conflicts 1\n```',
              '⚠️ Crosscheck incomplete — this spec was NOT fully checked against open PRs (exit code 4).\n```\nCannot read PR list.\n```',
              '🛑 Shepherd halt at QA: the merge gate is blocked. Nothing was merged.',
              '🛑 Shepherd halt at maybe use option B instead: the merge gate is blocked. Nothing was merged.',
              '✅ PR #42 already merged. Moving to Done.\nUse the existing API.',
              'Use option B.\n> 🛠️ Implementation complete.\n' + footer,
              'Use option B.\n<!-- bureau-implement-hold: status=NEEDS_HUMAN id=other:stage:2 -->',
              'review_dispositions: []\nChoose option B.']


class HoldAnswersTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='bureau hold answers ')
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.repo = self.base / 'repo'
        self.repo.mkdir()
        self.scripts = self.repo / 'scripts'
        shutil.copytree(SOURCE, self.scripts)
        self.fixture = self.base / 'linear.json'
        self.config = dict(linear=dict(teams=[dict(key='T', id='team', states=dict(
            triage='s1', spec='s2', spec_review='s2r', build='s3', qa='s4', build_review='s5', done='s6'))]),
            agents=dict(implement=True, file_overlap_gate=False), repo={})
        self.write_config()
        (self.repo / '.env').write_text('LINEAR_API_KEY=fixture\n')
        binary = self.base / 'bin'
        binary.mkdir()
        for name in ('curl', 'wget', 'gh', 'claude', 'codex', 'ssh'):
            path = binary / name
            path.write_text('#!/bin/bash\necho unexpected-external-command >> "$BUREAU_HOLD_FIXTURE.forbidden"\nexit 88\n')
            path.chmod(0o755)
        self.env = {**os.environ, 'BUREAU_CONFIG': str(self.repo / '.bureau.json'),
                    'BUREAU_ENV_FILE': str(self.repo / '.env'), 'BUREAU_HOLD_FIXTURE': str(self.fixture),
                    'BUREAU_HOLD_TEST_DRIVER': str(Path(__file__).resolve()),
                    'GIT_CEILING_DIRECTORIES': str(self.base), 'TMPDIR': str(self.base),
                    'GIT_ALLOW_PROTOCOL': 'file',
                    'PATH': str(binary) + os.pathsep + os.environ['PATH'],
                    'PYTHONDONTWRITEBYTECODE': '1', 'BUREAU_DISABLE_THROTTLE': '1'}
        for name in list(self.env):
            if name.startswith(('BUREAU_RUN_', 'BUREAU_ACTIVE_', 'BUREAU_TICKET_CONTROL_', 'BUREAU_DISPATCH_')):
                self.env.pop(name)
        for args in (('init', '-q', '-b', 'main'), ('config', 'user.name', 'Fixture'),
                     ('config', 'user.email', 'fixture@example.invalid'), ('config', 'maintenance.auto', 'false'),
                     ('commit', '-qm', 'init', '--allow-empty'), ('switch', '-qc', BRANCH)):
            self.run_command(['git', *args], 0)
        with (self.scripts / 'bureau-config.sh').open('a') as out:
            out.write('''
_bureau_linear_fetch() { printf '%s' "$1" | python3 -I -B "$BUREAU_HOLD_TEST_DRIVER" --linear "$BUREAU_HOLD_FIXTURE"; }
precondition_runner() { echo provider >> "$BUREAU_HOLD_FIXTURE.forbidden"; return 88; }
reset_worktree() { echo reset >> "$BUREAU_HOLD_FIXTURE.forbidden"; return 88; }
''')
        self.history([comment(hold(), 1)])

    def write_config(self):
        (self.repo / '.bureau.json').write_text(json.dumps(self.config))

    def history(self, comments, **extra):
        self.fixture.write_text(json.dumps(dict(comments=comments, **extra)))

    def run_command(self, command, code=None, input=None):
        result = subprocess.run(command, cwd=self.repo, env=self.env, input=input,
                                text=True, capture_output=True, timeout=40)
        if code is not None: self.assertEqual(result.returncode, code, result.stdout + result.stderr)
        return result

    def shell(self, command, code=None, *args):
        return self.run_command(['bash', '-e', '-c', 'source scripts/bureau-config.sh\n' + command,
                                 'fixture', *args], code)

    def context(self, code=0, expected=''):
        result = self.shell('bureau_implementation_context T-1 "$1"', code, expected)
        return json.loads(result.stdout) if code == 0 else result

    def assert_no_work(self):
        self.assertFalse(Path(str(self.fixture) + '.forbidden').exists())
        self.assertFalse(Path(str(self.fixture) + '.claims').exists(), 'runtime claimed ownership')
        self.assertFalse(json.loads(self.fixture.read_text()).get('mutations'))
        leases = self.repo / '.git/bureau/leases.json'
        self.assertFalse(leases.exists() and json.loads(leases.read_text()))

    def test_marked_answers_order_retries_and_new_hold(self):
        answers = [comment(f'Answer: {A}\nUse primary.', 2), comment(f'Answer: {A}\nKeep fallback.', 3)]
        self.history([comment(hold(), 1), *answers, comment(hold(), 4)])
        first = self.context()
        records = [json.loads(line) for line in first['text'].splitlines() if line.startswith('{')]
        self.assertEqual([row['body'] for row in records[1:]], ['Use primary.', 'Keep fallback.'])
        self.assertEqual([row['createdAt'] for row in records[1:]], [c['createdAt'] for c in answers])
        self.assertIn('Choose primary or backup?', records[0]['body'])
        self.assertEqual(first, self.context())  # no consume-once receipt
        expected = json.dumps([BRANCH, A])
        self.history([comment(hold(), 1), *answers, comment(hold(B), 5)])
        self.context(25, expected)
        self.history([comment(hold(), 1), *answers, comment(hold(B), 5), comment(f'Answer: {B}\nNew decision.', 6)])
        self.context(24, expected)  # answered B still was not in A's prompt

    def test_blank_old_wrong_id_branch_and_unmarked(self):
        for reply, minute in ((f'Answer: {A}\n \t', 2), (f'Answer: {A}', 2),
                              (f'Answer: {A}\nToo old.', 0), (f'Answer: {B}\nWrong ID.', 2),
                              ('Use primary.', 2), ('Unrelated update.', 2)):
            with self.subTest(reply=reply):
                self.history([comment(hold(), 1), comment(reply, minute)])
                self.context(25)
        self.history([comment(hold(A, 'other-branch'), 1), comment(f'Answer: {A}\nOld branch.', 2)])
        self.assertEqual(self.context()['text'], '')
        self.history([comment(hold(A, 'other-branch'), 1), comment(hold(B), 2),
                      comment(f'Answer: {A}\nOld branch.', 3)])
        self.context(25)

    def test_crlf_and_cr_answers_preserve_body_and_exact_marker(self):
        body = 'Use primary.\r\nKeep fallback.\nLast line.\r'
        for ending in ('\r\n', '\r'):
            with self.subTest(ending=repr(ending)):
                self.history([comment(hold(), 1), comment(f'Answer: {A}' + ending + body, 2)])
                rows = [json.loads(line) for line in self.context()['text'].splitlines() if line.startswith('{')]
                self.assertEqual(rows[-1]['body'], body)
        for marker in (f' Answer: {A}', f'Answer: {A} ', f'Answer: {A}\t'):
            self.history([comment(hold(), 1), comment(marker + '\r\n' + body, 2)])
            self.context(25)

    def test_full_problem_bodies_only_markers_release(self):
        for body in problem_bodies():
            with self.subTest(body=body):
                self.history([comment(hold(), 1), comment(body, 2)])
                self.context(25)
                self.history([comment(hold(), 1), comment(f'Answer: {A}\n' + body, 2)])
                rows = [json.loads(line) for line in self.context()['text'].splitlines() if line.startswith('{')]
                self.assertEqual(rows[-1]['body'], body)

    def test_pagination_and_context_cap(self):
        self.history([], pages=[[comment('ordinary', 0)], [comment(hold(), 1)]])
        self.context(25)
        for invalid in ('missing', None, dict(hasNextPage=None), dict(hasNextPage=True, endCursor='')):
            self.history([], pagination=invalid)
            self.context(24)
        self.history([comment(hold(), 1), comment(f'Answer: {A}\n' + 'ä' * 40000, 2)])
        self.assertIn('shorter marked reply', self.context(24).stderr)

    def test_history_reader_follows_cursors_and_rejects_short_unpaged_reads(self):
        self.history([], pages=[[comment('ordinary', 0)], [comment(hold(), 1)]])
        result = self.shell('get_issue_branch_and_comments T-1', 0)
        self.assertIn(hold(), [row['body'] for row in json.loads(result.stdout)['comments']])

    def test_short_pages_require_page_info(self):
        for invalid in ('missing', None):
            with self.subTest(pageInfo=invalid):
                self.history([], pagination=invalid)
                self.shell('get_issue_branch_and_comments T-1', 24)

    def test_publication_retries_projection_and_failure(self):
        self.history([], reject=True)
        publish = 'post_implementation_hold T-1 feat/issue "$(bureau_comment_projection "$1")" attempt'
        self.shell(publish, 24, 'review_dispositions: []\nChoose a backend.')
        record_path = self.repo / '.git/bureau/implementation-holds/T-1.json'
        original = json.loads(record_path.read_text())
        self.context(24)
        self.history([])
        posted = self.shell(publish, 0, 'changed retry body').stdout
        self.assertEqual(posted, original['body'])
        self.assertIn('Answer: ' + original['id'], posted)
        self.assertIn('Write your decision below this line', posted)
        self.shell(publish, 0, 'another retry')
        self.assertEqual([c['body'] for c in json.loads(self.fixture.read_text())['comments']], [posted, posted])
        self.shell('BUREAU_COMMENT_MAX_BYTES=800 post_implementation_hold T-1 feat/issue "$1" oversized', 0, 'ä' * 40000)
        large = json.loads(record_path.read_text())['body']
        self.assertLessEqual(len(large.encode()), 800)
        self.assertIn('Then remove `needs-human` and restart Build.', large)
        self.shell('BUREAU_COMMENT_MAX_BYTES=150 post_implementation_hold T-1 feat/issue q tiny', 24)
        self.context(24)

    def test_guards_precede_claim_mutation_git_and_provider(self):
        commands = [ ['bash', 'scripts/implement-pipeline.sh', 'T-1'],
                     ['bash', 'scripts/bureau-worker.sh', 'T-1', 'implement-pipeline.sh', str(self.base / 'worker')],
                     ['bash', 'scripts/shepherd.sh', '--no-tmux', '--from-stage', 'build', 'T-1'],
                     [sys.executable, '-I', '-B', 'scripts/bureau-runtime.py', 'prepare', 'T-1', 'implement'] ]
        for overlap in (False, True):
            self.config['agents']['file_overlap_gate'] = overlap
            self.write_config()
            for bad, code in ((False, 25), (True, 24)):
                self.history([comment(hold(), 1)], state='s4', **({'pagination': None} if bad else {}))
                for command in commands:
                    with self.subTest(command=command, overlap=overlap, code=code):
                        # app prepare requires Build; Shepherd explicitly restarts from QA.
                        state = json.loads(self.fixture.read_text())
                        state['state'] = 's4' if 'scripts/shepherd.sh' in command else 's3'
                        self.fixture.write_text(json.dumps(state))
                        self.run_command(command, code)
                        self.assert_no_work()
        self.config['agents']['implement'] = False
        self.write_config()
        self.run_command(['bash', 'scripts/shepherd.sh', '--no-tmux', '--respect-config', '--from-stage', 'build', 'T-1'], 20)
        self.assert_no_work()

    def test_app_context_finish_and_safe_posting_boundary(self):
        self.history([comment(hold(), 1), comment(f'Answer: {A}\nUse primary.', 2)])
        prepared = self.run_command([sys.executable, '-I', '-B', 'scripts/bureau-runtime.py', 'prepare', 'T-1', 'implement'], 0)
        run = json.loads(prepared.stdout)
        self.assertIn('Use primary.', run['implementation_answers']['text'])
        head = self.run_command(['git', 'rev-parse', 'HEAD'], 0).stdout.strip()
        result = dict(run_id=run['run_id'], stage='implement', outcome='blocked', head=head,
                      summary='Answer: ' + A + '\nGate only: choose how to restore the harness.')
        path = self.base / 'result.json'
        path.write_text(json.dumps(result))
        self.run_command([sys.executable, '-I', '-B', 'scripts/bureau-runtime.py', 'finish', run['run_id'], '--result', str(path)], 0)
        self.context(25)  # app blocked output creates a fresh decision hold
        self.shell('post_comment T-1 "$1"', 0, f'Answer: {A}\nAn app parent summary.')
        self.shell('post_comment T-1 "$1"', 0, hold())
        comments = json.loads(self.fixture.read_text())['comments']
        for posted in comments[-2:]: self.assertTrue(posted['body'].startswith('## Bureau update\n'))
        self.context(25)

    def test_queue_skips_unanswered_for_rest_of_poll(self):
        source = (self.scripts / 'queue-loop.sh').read_text()
        # Execute the real loop functions; polling and process creation stay outside this fixture.
        functions = source[source.index('preselect_issue() {'):source.index('\nwhile true; do\n  if bureau_is_paused')]
        setup = '''REPO_DIR="$PWD"; LOG_FILE="$TMPDIR/queue.log"; MODE=implement
pipeline_pick_next() { case "$2" in *T-1*) return 0 ;; *) echo T-1 ;; esac; }
get_issue_branch() { echo feat/issue; }
'''
        result = self.shell(setup + functions + '\nrun_one implement-pipeline.sh Build "$TMPDIR/worker"', 25)
        self.assertEqual(result.stdout.count('candidate: T-1'), 1)
        self.assert_no_work()

    def test_queue_prepares_branch_from_complete_admission_history(self):
        ordinary = [comment('ordinary', 0) for _ in range(200)]
        tail = [comment(f'<!-- bureau-branch: {BRANCH} -->', 1), comment(hold(), 2),
                comment(f'Answer: {A}\nUse primary.', 3)]
        self.history(ordinary + tail, pages=[ordinary[:100], ordinary[100:], tail], branchName='linear-generated')
        self.assertEqual(self.context()['branch'], BRANCH)
        # Keep the real worker up to the preparation boundary; the fake reset
        # models an absent generated branch and stops before doing Git work.
        with (self.scripts / 'bureau-config.sh').open('a') as out:
            out.write('''
reset_worktree() {
  printf '%s' "$3" > "$BUREAU_HOLD_FIXTURE.prepared-branch"
  [ "$3" = feat/issue ] || return 12
  return 2
}
''')
        source = (self.scripts / 'queue-loop.sh').read_text()
        functions = source[source.index('preselect_issue() {'):source.index('\nwhile true; do\n  if bureau_is_paused')]
        setup = '''REPO_DIR="$PWD"; LOG_FILE="$TMPDIR/queue.log"; MODE=implement
pipeline_pick_next() { echo T-1; }
'''
        self.shell(setup + functions + '\nrun_script implement-pipeline.sh Build "$TMPDIR/worker"', 2)
        self.assertEqual(Path(str(self.fixture) + '.prepared-branch').read_text(), BRANCH)
        self.assertFalse(Path(str(self.fixture) + '.forbidden').exists())

    def dispatch_lane(self, code, command=None):
        attempt = uuid.uuid4().hex
        workspace = self.repo / '.worktrees' / ('dispatch-T-1-' + attempt)
        root = self.repo / '.git/bureau'
        journal = root / 'dispatch-lanes' / (attempt + '.json')
        journal.parent.mkdir(parents=True, exist_ok=True)
        journal.write_text(json.dumps(dict(attempt=attempt, issue='T-1', stage='implement',
                                           workspace=str(workspace), code=None)))
        shepherd = str(self.scripts / 'shepherd.sh')
        ready = root / 'dispatch-ready' / attempt
        ready.parent.mkdir(parents=True, exist_ok=True)
        command = command if command is not None else ['bash', shepherd, '--no-tmux', '--respect-config',
                                                       '--worktree', str(workspace), 'T-1']
        with patch.dict(self.env, BUREAU_DISPATCH_LANE='1', BUREAU_DISPATCH_ATTEMPT_ID=attempt,
                        BUREAU_DISPATCH_READY_FILE=str(ready)):
            self.run_command([sys.executable, '-I', '-B', str(Path(__file__).resolve()), '--runtime-probe',
                              str(self.scripts / 'bureau-runtime.py'), '--repo', str(self.repo),
                              'exec', '--issue', 'T-1', '--workspace', str(workspace), '--entry', shepherd,
                              '--', *command], code)
        return json.loads(journal.read_text())

    def test_dispatcher_shepherd_checks_hold_before_claim_or_labels(self):
        for overlap in (False, True):
            self.config['agents']['file_overlap_gate'] = overlap
            self.write_config()
            for bad, code in ((False, 25), (True, 24)):
                with self.subTest(overlap=overlap, code=code):
                    self.history([comment(hold(), 1)], **({'pagination': None} if bad else {}))
                    Path(str(self.fixture) + '.claims').unlink(missing_ok=True)
                    result = self.dispatch_lane(code)
                    state = json.loads(self.fixture.read_text())
                    with self.subTest(invariant='no Linear mutation'):
                        self.assertFalse(state.get('mutations'))
                    with self.subTest(invariant='no ownership claim'):
                        self.assert_no_work()
                    self.assertEqual(result.get('decision_wait'), code == 25)
                    self.assertEqual(state['context_reads'], 1, 'startup admission ran twice')

    def test_dispatcher_shepherd_disabled_implementation_keeps_handoff(self):
        self.config['agents']['implement'] = False
        self.write_config()
        self.history([comment(hold(), 1)], pagination=None)
        self.dispatch_lane(20)
        self.assert_no_work()
        self.assertEqual(json.loads(self.fixture.read_text()).get('context_reads', 0), 0)

    def test_dispatcher_shepherd_does_not_gate_unrelated_stage(self):
        self.history([comment(hold(), 1)], state='s4', pagination=None)
        self.dispatch_lane(20)  # QA is disabled; its ordinary handoff still runs.
        self.assertEqual(json.loads(self.fixture.read_text()).get('context_reads', 0), 0)
        self.assertFalse(Path(str(self.fixture) + '.forbidden').exists())

    def test_dispatcher_distinguishes_child_decision_wait_from_other_25(self):
        # Start outside Build, then exercise the real reader inside the child.
        self.history([comment(hold(), 1)], state='s4')
        result = self.dispatch_lane(25, ['bash', '-ec', 'source "$1"; bureau_implementation_context T-1',
                                         'fixture', str(self.scripts / 'bureau-config.sh')])
        self.assertTrue(result.get('decision_wait'))
        self.assertTrue(Path(str(self.fixture) + '.claims').exists())
        _, dispatcher = self.dispatcher()
        path = dispatcher.journal / (result['attempt'] + '.json')
        ready = dispatcher.store.root / 'dispatch-ready' / result['attempt']
        self.assertTrue(ready.with_suffix('.decision-wait').exists())
        dispatcher.reconcile_lane(path)
        self.assertFalse(ready.with_suffix('.decision-wait').exists())
        # Matching prose is not authority for another permanent 25.
        result = self.dispatch_lane(25, ['bash', '-c', 'echo "Implementation held: post Answer: current"; exit 25'])
        self.assertFalse(result.get('decision_wait'))

    def dispatcher(self):
        spec = importlib.util.spec_from_file_location('hold_dispatch', self.scripts / 'bureau-dispatch.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        dispatcher = module.Dispatcher(self.repo, argparse.Namespace(poll_seconds=.05, max_concurrent=1, dry_run=False), -1)
        dispatcher.recheck = 1
        return module, dispatcher

    def defer_answer_wait(self, dispatcher):
        attempt = uuid.uuid4().hex
        path = dispatcher.journal / (attempt + '.json')
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(dict(attempt=attempt, issue='T-1', stage='implement', code=25,
            workspace=str(self.repo / '.worktrees' / ('dispatch-T-1-' + attempt)), decision_wait=True)))
        dispatcher.reconcile_lane(path)

    def test_dispatcher_answer_wait_releases_on_next_poll(self):
        module, dispatcher = self.dispatcher()
        dispatcher.defer('T-2', 25)  # A different, permanent human hold.
        picks = []

        def helper(body, *args):
            if body != module.PICK: return '1 1 1 0'
            excluded = set(args[0].split(','))
            picks.append(excluded)
            self.assertIn('T-2', excluded)
            if len(picks) == 1:
                self.context(25)
                self.assertNotIn('T-1', excluded)
                return 'T-1 implement'
            if len(picks) == 2:
                self.assertIn('T-1', excluded)  # Still the same poll.
                self.assertIn('T-1', dispatcher.exclusions())
                self.history([comment(hold(), 1), comment(f'Answer: {A}\nUse primary.', 2)])
                return ''
            self.assertEqual(len(picks), 3)
            self.assertIn('Use primary.', self.context()['text'])
            self.assertNotIn('T-1', excluded, 'decision wait persisted into the next poll')
            dispatcher.stopping = True
            return ''

        with patch.object(dispatcher.helpers, 'call', side_effect=helper), \
                patch.object(dispatcher, 'launch', side_effect=lambda *_: self.defer_answer_wait(dispatcher)), \
                patch.object(module.signal, 'signal'):
            dispatcher.run()
        self.assertEqual(len(picks), 3)
        self.assert_no_work()

    def test_dispatcher_answer_wait_releases_after_restart(self):
        _, dispatcher = self.dispatcher()
        self.context(25)
        self.defer_answer_wait(dispatcher)
        dispatcher.defer('T-2', 25)
        self.assertTrue({'T-1', 'T-2'} <= dispatcher.exclusions())
        self.history([comment(hold(), 1), comment(f'Answer: {A}\nUse primary.', 2)])
        _, restarted = self.dispatcher()
        self.assertIn('Use primary.', self.context()['text'])
        self.assertNotIn('T-1', restarted.exclusions())
        self.assertIn('T-2', restarted.exclusions())
        self.assert_no_work()

    def test_required_pr_mirror_retains_exit_18(self):
        self.shell('''gh() { if [ "$2" = list ]; then echo 42; else return 1; fi; }
comment_on_branch_pr feat/issue report required''', 18)

    def test_ordinary_posting_guard(self):
        for body in (f'Answer: {A}\nApp summary.', hold()):
            self.shell('post_comment T-1 "$1"', 0, body)
            posted = json.loads(self.fixture.read_text())['comments'][-1]['body']
            self.assertEqual(posted, '## Bureau update\n\n' + body)


if __name__ == '__main__':
    if sys.argv[1:2] == ['--linear']: fake_linear(Path(sys.argv[2]))
    elif sys.argv[1:2] == ['--runtime-probe']:
        # Observe real ownership even if an erroneous claim is later released.
        spec = importlib.util.spec_from_file_location('hold_runtime', sys.argv[2])
        runtime = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(runtime)
        claim = runtime.Store.claim
        def observed_claim(self, *args, **kwargs):
            Path(os.environ['BUREAU_HOLD_FIXTURE'] + '.claims').touch()
            return claim(self, *args, **kwargs)
        runtime.Store.claim = observed_claim
        sys.argv = sys.argv[2:]
        sys.exit(runtime.main())
    else: unittest.main(verbosity=2)
