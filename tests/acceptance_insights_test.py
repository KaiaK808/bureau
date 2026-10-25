"""Real Git/evidence fixtures; publication and providers never reach the network."""
import concurrent.futures
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
import uuid
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
PROGRAM = Path(os.environ.get('BUREAU_TEST_ACCEPTANCE_SCRIPT', ROOT / 'templates/scripts/bureau-acceptance.py'))
spec = importlib.util.spec_from_file_location('acceptance', PROGRAM)
a = importlib.util.module_from_spec(spec); spec.loader.exec_module(a)
e = a.e


def append_observation(args):
    directory, receipt = args
    e.Store(directory).put_receipt(receipt)


class AcceptanceInsightsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='bureau-pr7-fixture-'); self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name) / 'Users/canary-private-operator'
        self.repo = self.home / 'canary-private-client-repository'; self.spec = self.repo / 'specs/001-brief/spec.md'
        self.spec.parent.mkdir(parents=True); self.spec.write_text('# Intent\nPRIVATE_SPEC_SENTINEL\n')
        self.git('init', '-q', '-b', 'main'); self.git('config', 'user.name', 'Test')
        self.git('config', 'user.email', 'test@example.invalid'); self.git('config', 'maintenance.auto', 'false')
        self.git('add', '.'); self.git('commit', '-qm', 'intent')
        self.base = self.git('rev-parse', 'HEAD')
        self.git('update-ref', 'refs/remotes/origin/main', self.base)
        (self.repo / 'change').write_text('changed'); self.git('add', '.'); self.git('commit', '-qm', 'change')
        self.head = self.git('rev-parse', 'HEAD'); self.issue = 'TEAM-12'; self.pr = 7
        self.url = 'https://github.com/test/repo/pull/7'
        self.store = e.Store(self.repo / '.git/bureau')
        self.run = uuid.uuid4().hex
        self.store.begin_run(self.run, self.issue, self.repo, self.head)
        self.attempt = self.store.begin_attempt(self.run, self.issue, 'code_review', 'stage', self.repo, self.head)
        self.raw = self.repo / 'gate.log'; self.raw.write_text('RAW_GATE_SENTINEL secret=canary\n')
        self.store.gate(self.run, self.attempt, self.raw, self.head, self.head, 'PRIVATE_COMMAND_SENTINEL', 0, pr=self.pr, command_id='review.build')

    def git(self, *args): return a.git(self.repo, *args)

    def render(self, **kwargs):
        return a.render(self.store, self.repo, self.issue, self.pr, self.head, self.url,
                        spec=self.spec, base=self.base, **kwargs)

    def receipt(self, observation='The runner cap required splitting a test pass.', **changes):
        original = self.repo / ('original-' + uuid.uuid4().hex); original.mkdir()
        result = dict(verdict='APPROVE', missing_acceptance=[], fixes_needed=[], summary='PRIVATE_REVIEW_SENTINEL', insights=[observation])
        (original / 'final.txt').write_text(json.dumps(result))
        receipt = dict(run_id=self.run, issue=self.issue, stage='code_review', phase='verdict', provider='codex',
                    provider_attempt_id=uuid.uuid4().hex, stage_attempt_id=self.attempt,
                    started_at=1, ended_at=2, normalized_exit=0, provider_exit=0, outcome='complete',
                    provider_logs=str(original), transcript=None, structured_result=result,
                    start_head=self.head, end_head=self.head, schema_sha256='a' * 64)
        receipt.update(changes)
        return receipt

    def cli_fixture(self):
        self.bin = self.repo/'bin'; self.bin.mkdir()
        gh = self.bin/'gh'
        gh.write_text('#!'+sys.executable+'''\nimport json, os, pathlib, sys
root=pathlib.Path(os.environ['PR7_ROOT']); stored=root/'pr-comment'
(root/'gh-env.json').write_text(json.dumps(dict(os.environ)))
if sys.argv[2]=='view':
    print(json.dumps({'comments':[{'body':stored.read_text()}] if stored.exists() else []}))
else:
    body=sys.stdin.read()
    with stored.open('a') as out: out.write(body)
'''); gh.chmod(0o755)
        self.config = self.repo/'config.sh'
        self.config.write_text('''get_issue_comments() {
  if [ "${PR7_BAD_TICKET_LOOKUP:-0}" = 1 ]; then echo invalid; return 0; fi
  python3 -c 'import json, os, pathlib; p=pathlib.Path(os.environ["PR7_ROOT"])/"ticket-comment"; print(json.dumps([{"body":p.read_text()}] if p.exists() else []))'
}
post_comment() { printf '%s\\n' "$2" >> "$PR7_ROOT/ticket-comment"; }
''')

    def cli(self, repo=None, **env):
        command = [sys.executable, '-I', str(PROGRAM), '--repo', str(repo or self.repo), '--issue', self.issue,
                   '--pr', str(self.pr), '--head', self.head, '--url', self.url, '--spec', str(self.spec),
                   '--base', self.base, '--config-script', str(self.config), '--publish']
        result = subprocess.run(command, capture_output=True, text=True, timeout=10,
                                env={**os.environ, 'PATH':str(self.bin)+os.pathsep+os.environ['PATH'], 'PR7_ROOT':str(self.repo),
                                     'BUREAU_ENV_FILE':'', 'BUREAU_CONFIG':'', **env})
        self.assertEqual(result.returncode, 0, result.stderr)
        return result

    def test_render_links_metadata_review_and_privacy(self):
        observation = 'A narrow runner cap required dividing the private canary pass.'
        path = self.store.put_receipt(self.receipt(observation=observation))
        self.assertIn(observation, (self.store.root/'insights.jsonl').read_text())
        wait = self.store.open_wait(self.issue, 'approval', 'decision', 'private reason', 'maintainer', 'code_review', 20)
        with patch.dict(os.environ, HOME=str(self.home)):
            brief = self.render(verdict='APPROVE', review_head=self.head)
        for private in ('file:', str(self.repo), str(self.repo.resolve()), str(self.home), os.environ['HOME'],
                        '$HOME', self.repo.name, self.home.name):
            self.assertNotIn(private, brief, 'brief leaked local identity or absolute path')
        self.assertNotRegex(brief, r'(?<![:/\w])/(?:Users|home|private|var|tmp)/')
        self.assertIn(f'/blob/{self.head}/specs/001-brief/spec.md', brief)
        self.assertIn(f'/compare/{self.base}...{self.head}', brief)
        self.assertIn('receipt ' + path.stem, brief)
        self.assertIn('run ' + self.run + '/manifest', brief)
        self.assertIn('wait ' + wait['wait_id'], brief)
        self.assertIn('`<git common dir>/bureau/evidence/v1`', brief)
        self.assertIn('exit=0', brief); self.assertIn('REVIEW_HEAD=' + self.head, brief)
        self.assertIn(hashlib.sha256(self.raw.read_bytes()).hexdigest(), brief)
        self.assertIn('Final verdict: APPROVE', brief); self.assertIn('missing_acceptance: 0', brief)
        for secret in ('RAW_GATE_SENTINEL', 'canary', 'PRIVATE_SPEC_SENTINEL', 'PRIVATE_REVIEW_SENTINEL', 'PRIVATE_COMMAND_SENTINEL', observation):
            self.assertNotIn(secret, brief, 'brief privacy leaked raw source bytes')

    def validation_merge(self):
        self.git('checkout', '-qb', 'advanced-base', self.base)
        (self.repo / 'base-only').write_text('base advanced')
        self.git('add', 'base-only'); self.git('commit', '-qm', 'advance base')
        self.base = self.git('rev-parse', 'HEAD')
        self.git('update-ref', 'refs/remotes/origin/main', self.base)
        self.git('checkout', '-q', 'main')
        self.git('merge', '--no-ff', '--no-edit', self.base)
        validation = self.git('rev-parse', 'HEAD')
        self.assertNotEqual(validation, self.head)
        self.assertEqual(self.git('rev-list', '--parents', '-n', '1', validation).split(), [validation, self.head, self.base])
        return validation

    def test_review_receipt_matches_actual_diverged_base_validation_merge(self):
        validation = self.validation_merge()
        inputs = dict(head=self.head, base=self.base, validation_head=validation)
        path = self.store.put_receipt(self.receipt(start_head=validation, end_head=validation, review_inputs=inputs))
        brief = self.render(verdict='APPROVE', review_head=self.head)
        self.assertIn('receipt ' + path.stem, brief)
        self.assertIn('Validation merge: ' + validation, brief)
        self.assertIn('verified parents', brief)
        self.assertIn('Provider verdict: APPROVE', brief)

    def test_receipt_keeps_gate_attempts_with_review_only_inputs(self):
        inputs = dict(head=self.head, base=self.base, validation_head=self.head)
        execution = dict(attempt_id=uuid.uuid4().hex, gate_id='1'*64, command_id='repo.test_command',
                         command_sha256='1'*64, head=self.head, end_head=self.head, normalized_exit=1,
                         source='codex.command_execution', environment='sandbox', abort=None,
                         call_id_sha256='2'*64, duration_seconds=.1, bytes=3, sha256='3'*64, stage_log=None)
        for stage in ('code_review', 'implement', 'qa'):
            with self.subTest(stage=stage):
                path = self.store.put_receipt(self.receipt(stage=stage, gate_attempts=[execution], review_inputs=inputs))
                stored = json.loads(path.read_text())
                self.assertEqual(stored['gate_attempts'], [execution])
                self.assertEqual(stored.get('review_inputs'), inputs if stage == 'code_review' else None)

    def test_review_receipt_rejects_uncorrelated_validation_heads(self):
        validation = self.validation_merge()
        inputs = dict(head=self.head, base=self.base, validation_head=validation)
        (self.repo / 'after-review').write_text('unreviewed')
        self.git('add', 'after-review'); self.git('commit', '-qm', 'after validation')
        later = self.git('rev-parse', 'HEAD')
        for changes in ({'review_inputs': None},
                        {'review_inputs': {**inputs, 'head': self.base}},
                        {'review_inputs': {**inputs, 'base': self.head}},
                        {'review_inputs': {**inputs, 'validation_head': self.head}},
                        {'start_head': self.head}, {'end_head': later},
                        {'start_head': later, 'end_head': later, 'review_inputs': {**inputs, 'validation_head': later}}):
            with self.subTest(changes=changes):
                receipt = self.receipt(start_head=validation, end_head=validation, review_inputs=inputs)
                receipt.update(changes); path = self.store.put_receipt(receipt)
                self.assertIn('Review receipt/verdict for this HEAD: missing.', self.render())
                path.unlink()

    def test_absent_evidence_is_explicit(self):
        brief = a.render(self.store, self.repo, 'OTHER-1', 9, self.head, self.url, spec=None)
        for value in ('Spec intent: missing', 'Gate records (exit/digest): missing', 'Final verdict: missing',
                      'Review receipt/verdict for this HEAD: missing', 'Run manifest: missing', 'Wait records: missing'):
            self.assertIn(value, brief)

    def test_old_head_gate_is_not_current(self):
        (self.repo / 'change').write_text('next'); self.git('add', '.'); self.git('commit', '-qm', 'next')
        self.head = self.git('rev-parse', 'HEAD')
        self.assertIn('other/missing HEAD', self.render())

    def test_wait_and_missing_original(self):
        receipt = self.receipt(); self.store.put_receipt(receipt)
        Path(receipt['provider_logs'], 'final.txt').unlink()
        wait = self.store.open_wait(self.issue, 'approval', 'decision', 'private reason', 'maintainer', 'code_review', 20, self.run, self.head, self.pr)
        brief = self.render()
        self.assertIn('original result unavailable', brief)
        self.assertIn('Wait: decision', brief)
        self.assertNotIn('private reason', brief)

    def test_invalid_receipt_and_wait_metadata_do_not_echo_source_text(self):
        path = self.store.put_receipt(self.receipt())
        value = json.loads(path.read_text()); value['result_sha256'] = 'RAW_GATE_SENTINEL'
        value.pop('provider_logs'); path.write_text(json.dumps(value))
        wait = self.store.open_wait(self.issue, 'approval', 'decision', 'private reason', 'maintainer', 'code_review', 20)
        wait_path = self.store.root/'waits'/(wait['wait_id']+'.json')
        value = json.loads(wait_path.read_text()); value.update(stage='RAW_GATE_SENTINEL',exit_code='RAW_GATE_SENTINEL')
        wait_path.write_text(json.dumps(value))
        brief = self.render()
        self.assertNotIn('RAW_GATE_SENTINEL', brief)
        self.assertIn('result sha256=missing', brief)
        self.assertIn('exit=missing', brief)

    def test_publish_independent_retry_and_changed_head(self):
        calls = []
        def delivery(destination, body):
            calls.append((destination, body)); return destination == 'ticket'
        record = a.publish(self.store, self.issue, self.pr, self.head, self.render, delivery)
        self.assertEqual((record['ticket'], record['pull_request']), ('delivered', 'pending'))
        self.assertEqual([c[0] for c in calls], ['ticket', 'pull_request'])
        calls.clear()
        record = a.publish(self.store, self.issue, self.pr, self.head, lambda: 'different content', lambda d, b: calls.append((d, b)) or True)
        self.assertEqual([c[0] for c in calls], ['pull_request'])
        self.assertEqual(calls[0][1], record['body'])
        calls.clear()
        a.publish(self.store, self.issue, self.pr, self.head, self.render, lambda d, b: calls.append(d) or True)
        self.assertEqual(calls, [])
        a.publish(self.store, self.issue, self.pr, 'b' * 40, lambda: 'new HEAD', lambda d, b: calls.append(d) or True)
        self.assertEqual(calls, ['ticket', 'pull_request'])

    def test_reverse_failure_and_exception_are_pending(self):
        calls = []
        def fail(destination, body):
            calls.append(destination)
            if destination == 'ticket': raise ValueError('API malformed JSON')
            return True
        a.publish(self.store, self.issue, self.pr, self.head, self.render, fail)
        calls.clear()
        a.publish(self.store, self.issue, self.pr, self.head, self.render, lambda d, b: calls.append(d) or True)
        self.assertEqual(calls, ['ticket'])

    def test_pre_fix_pending_body_is_rendered_without_private_paths(self):
        unsafe = '[manifest](file:///Users/canary-private-operator/private-client/.git/manifest.json)'
        a.publish(self.store, self.issue, self.pr, self.head, lambda: unsafe, lambda d, b: d == 'ticket')
        calls = []
        record = a.publish(self.store, self.issue, self.pr, self.head, self.render,
                           lambda d, b: calls.append((d, b)) or True)
        self.assertEqual([d for d, _ in calls], ['pull_request'])
        self.assertEqual(calls[0][1], record['body'])
        self.assertNotIn('file:', record['body'])
        self.assertNotIn('canary-private-operator', record['body'])
        self.assertEqual(record['body_sha256'], hashlib.sha256(record['body'].encode()).hexdigest())

    def test_cli_remote_markers_recover_lost_acknowledgments(self):
        observation = 'A narrow runner cap required dividing the private canary pass.'
        self.store.put_receipt(self.receipt(observation=observation))
        self.assertIn(observation, (self.store.root/'insights.jsonl').read_text())
        self.cli_fixture(); self.cli()
        path = self.store.root/'acceptance'/f'{self.pr}-{self.head}.json'
        record = json.loads(path.read_text()); record.update(ticket='pending',pull_request='pending')
        path.write_text(json.dumps(record))
        self.cli()
        for name in ('ticket-comment', 'pr-comment'):
            self.assertEqual((self.repo/name).read_text().count('<!-- bureau-acceptance:'), 1)
            self.assertNotIn(observation, (self.repo/name).read_text(), 'brief transport leaked local insight')
        self.assertEqual(json.loads(path.read_text())['ticket'], 'delivered')

    def test_cli_bad_ticket_response_does_not_prevent_pr_delivery(self):
        self.cli_fixture(); self.cli(PR7_BAD_TICKET_LOOKUP='1')
        self.assertFalse((self.repo/'ticket-comment').exists())
        self.assertTrue((self.repo/'pr-comment').exists())
        self.cli()
        for name in ('ticket-comment', 'pr-comment'):
            self.assertEqual((self.repo/name).read_text().count('<!-- bureau-acceptance:'), 1)

    def test_cli_retry_keeps_explicit_saved_head_when_resolving_url(self):
        self.cli_fixture(); self.cli()
        receipt = self.store.root/'acceptance'/f'{self.pr}-{self.head}.json'
        value = json.loads(receipt.read_text()); value['ticket'] = 'pending'; receipt.write_text(json.dumps(value))
        gh = self.bin/'gh'
        source = gh.read_text().replace("if sys.argv[2]=='view':", """if 'number,headRefOid,baseRefOid,url' in sys.argv:
    print(json.dumps({'number':7, 'headRefOid':'b'*40, 'baseRefOid':'c'*40, 'url':'https://github.com/test/repo/pull/7'}))
elif sys.argv[2]=='view':""")
        gh.write_text(source)
        command = [sys.executable, '-I', str(PROGRAM), '--repo', str(self.repo), '--issue', self.issue,
                   '--pr', str(self.pr), '--head', self.head, '--config-script', str(self.config), '--publish']
        result = subprocess.run(command, capture_output=True, text=True, timeout=10,
                                env={**os.environ, 'PATH':str(self.bin)+os.pathsep+os.environ['PATH'], 'PR7_ROOT':str(self.repo),
                                     'BUREAU_ENV_FILE':'', 'BUREAU_CONFIG':''})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(list(receipt.parent.glob('*.json'))), 1)
        self.assertEqual(json.loads(receipt.read_text())['ticket'], 'delivered')

    def test_git_and_gh_filter_secrets_and_shell_startup(self):
        self.cli_fixture()
        original_git = shutil.which('git')
        shim = self.bin/'git'
        shim.write_text('#!'+sys.executable+'\nimport json,os,pathlib,sys\n'
                        '(pathlib.Path(os.environ["PR7_ROOT"])/"git-env.json").write_text(json.dumps(dict(os.environ)))\n'
                        'os.execv('+repr(original_git)+', ['+repr(original_git)+']+sys.argv[1:])\n')
        shim.chmod(0o755)
        startup = self.repo/'startup.sh'; marker = self.repo/'startup-ran'
        startup.write_text('touch "'+str(marker)+'"\n')
        probes = dict(LINEAR_API_KEY='linear-probe-123456',TELEGRAM_BOT_TOKEN='telegram-probe-123456',
                      COPY_OF_KEY='prefix-linear-probe-123456-suffix',GH_TOKEN='github-probe-123456',
                      HERDR_PANE_ID='operator-pane',BASH_ENV=str(startup),ENV=str(startup))
        # The trusted shell API callback must still have the Linear credential.
        self.config.write_text(self.config.read_text().replace('post_comment() {',
                               'post_comment() { [ "$LINEAR_API_KEY" = linear-probe-123456 ] || return 1;'))
        self.cli(**probes)
        for tool in ('git','gh'):
            environ = json.loads((self.repo/(tool+'-env.json')).read_text())
            for key in ('LINEAR_API_KEY','TELEGRAM_BOT_TOKEN','COPY_OF_KEY','HERDR_PANE_ID','BASH_ENV','ENV'):
                self.assertNotIn(key,environ.keys())
            self.assertEqual(environ.get('GH_TOKEN'),probes['GH_TOKEN'] if tool=='gh' else None)
        self.assertFalse(marker.exists())
        self.assertTrue((self.repo/'ticket-comment').exists())

    def test_ticket_callback_reloads_private_env(self):
        self.cli_fixture()
        envfile=self.repo/'.env'; envfile.write_text('LINEAR_API_KEY=dotenv-only-probe\n')
        source='source "'+str(ROOT/'templates/scripts/bureau-env.sh')+'"\n'
        config=self.config.read_text().replace('post_comment() {',
                    'post_comment() { [ "$LINEAR_API_KEY" = dotenv-only-probe ] && [ "$API_KEY" = dotenv-only-probe ] || return 1;')
        self.config.write_text(source+config)
        self.cli(BUREAU_ENV_FILE=str(envfile), LINEAR_API_KEY='')
        self.assertTrue((self.repo/'ticket-comment').exists())
        record=json.loads(next((self.store.root/'acceptance').glob('*.json')).read_text())
        self.assertEqual(record['ticket'],'delivered')

    def test_linked_worktree_shares_delivery_and_insights(self):
        self.cli_fixture(); self.cli()
        linked = self.repo/'linked-worktree'
        self.git('worktree', 'add', '--quiet', '--detach', str(linked), self.head)
        self.cli(repo=linked)
        for name in ('ticket-comment', 'pr-comment'):
            self.assertEqual((self.repo/name).read_text().count('<!-- bureau-acceptance:'), 1)
        common = a.git(linked, 'rev-parse', '--git-common-dir')
        shared = e.Store((linked/Path(common)).resolve()/'bureau')
        receipt = self.receipt(); shared.put_receipt(receipt); self.store.put_receipt(receipt)
        self.assertEqual(shared.root.resolve(), self.store.root.resolve())
        self.assertEqual(len((shared.root/'insights.jsonl').read_text().splitlines()), 1)

    def test_receipt_collects_once_and_keeps_metadata_private(self):
        receipt = self.receipt(); path = self.store.put_receipt(receipt)
        self.store.put_receipt(receipt)
        self.assertTrue((self.store.root / 'insights.jsonl').exists(), 'valid emitted observation absent')
        rows = [json.loads(line) for line in (self.store.root / 'insights.jsonl').read_text().splitlines()]
        self.assertEqual(len(rows), 1, 'valid emitted observation absent or duplicated')
        self.assertEqual(rows[0]['observation'], receipt['structured_result']['insights'][0])
        for key in ('issue', 'stage', 'run_id', 'provider_attempt_id', 'stage_attempt_id'):
            self.assertEqual(rows[0][key], receipt[key])
        self.assertIsInstance(rows[0]['timestamp'], (float, int))
        self.assertNotIn('PRIVATE_REVIEW_SENTINEL', path.read_text())
        self.assertEqual((self.store.root / 'insights.jsonl').stat().st_mode & 0o777, 0o600)

    def test_old_empty_and_malformed_insights(self):
        for items in (None, [], {}, ['good', 4], 'text', ['x' * 281], ['x'] * 4, ['a\nb']):
            receipt = self.receipt()
            if items is None: receipt['structured_result'].pop('insights')
            else: receipt['structured_result']['insights'] = items
            self.store.put_receipt(receipt)
        self.assertFalse((self.store.root / 'insights.jsonl').exists())

    def test_insights_filter_credentials_paths_and_raw_output_before_persistence(self):
        canaries = [name + '=CANARY_VALUE' for name in ('TOKEN', 'API_KEY', 'client_secret', 'PASSWORD', 'AUTH', 'COOKIE', 'SESSION')]
        canaries += [prefix + 'CANARY_VALUE' for prefix in ('ghp_', 'gho_', 'github_pat_', 'sk-', 'lin_api_', 'xoxb-', 'AKIA')]
        canaries += ['-----BEGIN PRIVATE KEY-----', '-----BEGIN CERTIFICATE-----', 'a' * 32,
                    'ABCDEF0123456789' * 2, 'QWxhZGRpbjpvcGVuIHNlc2FtZUNhbmFyeQ==',
                    'https://canary-user:canary-pass@example.invalid/api', 'ssh://canary-user@example.invalid/repo',
                    '/Users/canary-private-operator/project', '/home/canary-user/project', '/root/.config',
                    r'C:\Users\canary-user\project', str(self.home / 'private-file'),
                    '[INFO] CANARY_RAW_OUTPUT', 'INFO: CANARY_RAW_OUTPUT', '2026-10-24T12:30:00Z CANARY_RAW_OUTPUT',
                    '12:30:00 CANARY_RAW_OUTPUT', 'npm ERR! CANARY_RAW_OUTPUT', '$ printenv CANARY_RAW_OUTPUT',
                    '> node CANARY_RAW_OUTPUT', '+ export CANARY_RAW_OUTPUT', '{"raw":"CANARY_RAW_OUTPUT"}',
                    'total 42', '-rw-r--r-- 1 canary staff 42 file', 'Traceback (most recent call last):',
                    'ok 1 - CANARY_RAW_OUTPUT', '2 passed in 0.4s']
        safe = 'The runner cap required splitting a test pass.'
        for canary in canaries:
            with self.subTest(canary=canary), patch.object(e.os, 'write') as warning:
                receipt = self.receipt()
                receipt['structured_result']['insights'] = [canary, safe]
                path = self.store.put_receipt(receipt)
                self.assertTrue(path.exists(), 'advisory filtering must preserve completion')
                self.assertEqual(json.loads(path.read_text())['normalized_exit'], 0)
                rows = [json.loads(line) for line in (self.store.root / 'insights.jsonl').read_text().splitlines()]
                attempt_rows = [r for r in rows if r['provider_attempt_id'] == receipt['provider_attempt_id']]
                self.assertEqual([r['observation'] for r in attempt_rows], [safe])
                self.assertTrue(warning.called, 'filtered observation needs a content-free warning')
                self.assertNotIn(canary.encode(), b''.join(c.args[1] for c in warning.call_args_list))

    def test_insights_multiline_canaries_preserve_success_without_persistence(self):
        for canary in ('CANARY_OUTPUT\nnext line', 'CANARY_OUTPUT\rnext line', 'CANARY_OUTPUT\u2028next line', '\x1b[31mCANARY_OUTPUT'):
            with self.subTest(canary=canary), patch.object(e.os, 'write') as warning:
                receipt = self.receipt(observation=canary)
                path = self.store.put_receipt(receipt)
                self.assertEqual(json.loads(path.read_text())['normalized_exit'], 0)
                self.assertFalse((self.store.root / 'insights.jsonl').exists())
                self.assertTrue(warning.called)
                self.assertNotIn(canary.encode(), b''.join(c.args[1] for c in warning.call_args_list))

    def test_concurrent_append(self):
        receipts = [self.receipt(observation='Observation ' + str(i) + '.') for i in range(12)]
        with concurrent.futures.ProcessPoolExecutor(max_workers=2) as pool:
            list(pool.map(append_observation, [(self.repo / '.git/bureau', receipt) for receipt in receipts] * 2))
        rows = [json.loads(line) for line in (self.store.root / 'insights.jsonl').read_text().splitlines()]
        self.assertEqual(len(rows), 12)
        self.assertEqual(len({r['provider_attempt_id'] for r in rows}), 12)

    def test_corrupt_brief_does_not_block_receipts(self):
        path = self.store.root/'acceptance'/'invalid.json'; path.parent.mkdir()
        path.write_text('RAW_CORRUPTION_SENTINEL')
        self.store.put_receipt(self.receipt())
        self.assertFalse(path.exists())
        for record in (self.store.root/'quarantine').glob('*.json'):
            self.assertNotIn('RAW_CORRUPTION_SENTINEL', record.read_text())


if __name__ == '__main__': unittest.main()
