"""Synthetic baseline evidence, real Git/read helper, fake GitHub and Linear."""
from datetime import datetime, timedelta, timezone
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
from unittest.mock import patch

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
PROGRAM = ROOT / 'scripts/bureau-baseline.py'


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


b = load('baseline', PROGRAM)
e = load('fixture_evidence', ROOT / 'templates/scripts/bureau-evidence.py')


def issue(number):
    return '-'.join(('TEST', str(number)))


def tree_bytes(root):
    """Include ignored files, Git metadata, modes and directory inventory."""
    result = {}
    for path in sorted(root.rglob('*')):
        relative = str(path.relative_to(root))
        if path.is_symlink():
            result[relative] = ('link', os.readlink(path))
        elif path.is_file():
            result[relative] = (path.stat().st_mode, hashlib.sha256(path.read_bytes()).hexdigest())
        else:
            result[relative] = ('directory', path.stat().st_mode)
    return result


class BaselineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='bureau-baseline-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.repo = self.root / 'fixture repository'
        self.repo.mkdir()
        self.bin = self.root / 'bin'
        self.bin.mkdir()
        self.now = datetime(2027, 1, 3, 23, 59, 59, tzinfo=timezone.utc)
        self.monday = (self.now - timedelta(days=self.now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
        self.start = self.monday - timedelta(weeks=1)
        self.clock = (self.start + timedelta(days=1)).timestamp()
        self.git('init', '-q')
        self.git('config', 'user.name', 'Fixture')
        self.git('config', 'user.email', 'fixture')
        self.git('config', 'maintenance.auto', 'false')
        (self.repo / 'intent').write_text('fixture\n')
        self.git('add', '.')
        self.git('commit', '-qm', 'fixture')
        self.head = self.git('rev-parse', 'HEAD')
        self.store = e.Store(self.repo / '.git/bureau', clock=lambda: self.clock)
        self.prs = []
        self.snapshots = {}
        self.counter = 0
        self.providers = self.root / 'provider originals'
        self.providers.mkdir()
        # No literal target identities, URLs, credentials or local paths in fixtures.
        (self.repo / '.bureau.json').write_text(json.dumps(dict(linear=dict(teams=[dict(states={})]))))
        self.env = {k: v for k, v in os.environ.items() if not k.startswith(('BUREAU_', 'LINEAR_', '_BUREAU_'))}
        self.env.update(PATH=str(self.bin) + os.pathsep + os.environ['PATH'],
                        LINEAR_API_KEY='synthetic', BUREAU_COST_DIR=str(self.root / 'cost'),
                        BASELINE_FIXTURE=str(self.root), PYTHONDONTWRITEBYTECODE='1')
        self.linear_key = 'synthetic'
        self.executable('gh', '''import json, os, sys
from pathlib import Path
args = sys.argv[1:]
assert 'GH_REPO' not in os.environ
assert args == ['pr', 'list', '--state', 'merged', '--limit', '1000', '--json',
                'number,mergedAt,headRefName,baseRefName,title,body,commits'], args
print((Path(os.environ['BASELINE_FIXTURE']) / 'prs.json').read_text())
''')
        self.executable('curl', '''import json, os, re, sys
from pathlib import Path
args = sys.argv[1:]
root = Path(os.environ['BASELINE_FIXTURE'])
assert sys.stdin.read() == 'header = "Authorization: ' + (root / 'linear-key.txt').read_text() + '"' + chr(10)
assert 'API_KEY' not in os.environ
assert 'LINEAR_API_KEY' not in os.environ
payload = json.loads(args[args.index('-d') + 1])
query = payload['query']
assert 'mutation' not in query.lower()
assert 'completedAt canceledAt state { id name }' in query
key = re.search(r'key: { eq: "([A-Z]+)"', query).group(1)
number = re.search(r'number: { eq: ([0-9]+)', query).group(1)
rows = json.loads((Path(os.environ['BASELINE_FIXTURE']) / 'linear.json').read_text())
row = rows.get('-'.join((key, number)))
print(json.dumps({'data': {'issues': {'nodes': [row] if row else []}}}))
print('__BUREAU_HTTP_STATUS__:200')
''')

    def executable(self, name, code):
        path = self.bin / name
        path.write_text('#!' + sys.executable + '\n' + code)
        path.chmod(0o755)

    def git(self, *args):
        return subprocess.check_output(['git', '-C', str(self.repo), *args], text=True, stderr=subprocess.DEVNULL).strip()

    def identity(self):
        self.counter += 1
        return format(self.counter, '032x')

    def sync(self):
        (self.root / 'prs.json').write_text(json.dumps(self.prs))
        (self.root / 'linear.json').write_text(json.dumps(self.snapshots))
        (self.root / 'linear-key.txt').write_text(self.linear_key)

    def snapshot(self, ticket, state='Done', completed=None, cancelled=None):
        self.snapshots[ticket] = dict(id=self.identity(), identifier=ticket,
            completedAt=completed, canceledAt=cancelled, state=dict(id=self.identity(), name=state), labels=dict(nodes=[]))

    def run_manifest(self, ticket, pr=None):
        run = self.identity()
        self.store.begin_run(run, ticket, self.repo, self.head)
        if pr:
            attempt = self.store.begin_attempt(run, ticket, 'implement', 'stage', self.repo, self.head)
            self.store.set_pr(run, attempt, pr)
        return run

    def attempt(self, run, ticket, stage, code=0):
        attempt = self.store.begin_attempt(run, ticket, stage, 'stage', self.repo, self.head)
        self.clock += 60
        self.store.end_attempt(run, attempt, code, self.head)
        return attempt

    def receipt(self, run, attempt, ticket, cost, phase='implement', stage='implement', **changes):
        receipt = dict(run_id=run, stage_attempt_id=attempt, provider_attempt_id=self.identity(), issue=ticket,
            stage=stage, phase=phase, provider='codex', provider_exit=0, normalized_exit=0, outcome='complete',
            started_at=self.clock - 1, ended_at=self.clock, provider_logs=str(self.providers), transcript=None,
            structured_result=None, schema_sha256=None, start_head=self.head, end_head=self.head,
            usage=dict(input_tokens=10, output_tokens=5), total_cost_usd=cost)
        receipt.update(changes)
        self.store.put_receipt(receipt)
        return receipt

    def resolver_validation(self, run, attempt, ticket, operation='merge'):
        # Match bureau-conflicts.py:evidence, including absent timing/usage.
        validation = dict(run_id=run, provider_attempt_id=self.identity(), stage_attempt_id=attempt,
            issue=ticket, stage='conflict_resolver', phase='validation', provider='codex', normalized_exit=0,
            start_head=self.head, end_head=self.head, outcome='complete', conflict_resolution=dict(
                attempt=1, base=self.head, head=self.head, operation=operation, provider='codex',
                touched_path_digest='a' * 64, check_outcome='passed'))
        return validation, self.store.put_receipt(validation)

    def pr(self, number, ticket=None, title='feat: fixture', body='', merged=None, marked=True):
        value = dict(number=number, mergedAt=(merged or (self.start + timedelta(days=4))).isoformat(),
            headRefName='feature' + ('-' + ticket.lower() if ticket else ''), baseRefName='main', title=title, body=body,
            commits=[dict(oid=self.head, messageHeadline='fixture', messageBody='Bureau-Generated: true' if marked else '')])
        self.prs.append(value)
        return value

    def populated(self):
        first, second, abandoned, held = map(issue, range(1, 5))
        self.run = self.run_manifest(first, 1)
        self.attempt(self.run, first, 'spec')
        a = self.attempt(self.run, first, 'implement')
        self.call = self.receipt(self.run, a, first, 2.0,
            structured_result=dict(review_dispositions=[dict(disposition='applied', severity='BUG', path='intent')]))
        for number in (1, 2):
            a = self.attempt(self.run, first, 'code_review')
            self.receipt(self.run, a, first, .5, phase='correctness', stage='code_review')
            self.store.review_round(self.run, a, number, 'BUG', final_verdict='APPROVE')
        self.pr(1, first)
        self.snapshot(first, completed=self.prs[-1]['mergedAt'])
        other = self.run_manifest(second, 2)
        self.attempt(other, second, 'spec')
        a = self.attempt(other, second, 'code_review')
        self.receipt(other, a, second, 1.0, phase='correctness', stage='code_review')
        self.store.review_round(other, a, 1, 'BUG', final_verdict='APPROVE')
        self.pr(2, second, marked=False)
        self.snapshot(second, completed=self.prs[-1]['mergedAt'])
        run = self.run_manifest(abandoned)
        self.attempt(run, abandoned, 'spec', 26)
        self.snapshot(abandoned, 'Cancelled', cancelled=(self.start + timedelta(days=3)).isoformat())
        run = self.run_manifest(held)
        self.attempt(run, held, 'code_review', 25)
        self.snapshot(held, 'Build Review')
        self.pr(3, title=issue(6) + ': fix: regression', body='Regression in ' + first + '.', merged=self.start + timedelta(days=5))
        # A mention in a documentation PR is not a defect signal.
        self.pr(4, title='docs: references', body='See ' + second, merged=self.start + timedelta(days=6))
        self.pr(5, title='fix: prior change', body='Repairs #2.', merged=self.start + timedelta(days=6))
        self.sync()

    def cli(self, fmt='json', repo=None, program=None):
        # Keep argparse, isolated mode and a child process, but share the fixture
        # clock even if the real clock crosses Monday between setup and launch.
        launcher = '''import runpy, sys
from datetime import datetime
program = sys.argv.pop(1)
now = datetime.fromisoformat(sys.argv.pop(1))
sys.argv[0] = program
runpy.run_path(program)['main'](now=now)
'''
        return subprocess.run([sys.executable, '-I', '-c', launcher, str(program or PROGRAM), self.now.isoformat(), '--repo', str(repo or self.repo),
            '--weeks', '2', '--format', fmt], env=self.env, text=True, capture_output=True, timeout=40)

    def report(self):
        result = self.cli()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, '')
        return json.loads(result.stdout)

    def metrics(self):
        return self.report()['weeks'][0]['metrics']

    def copy_program(self, source):
        overlay = self.root / 'tool'
        if not overlay.exists():
            shutil.copytree(ROOT / 'templates', overlay / 'templates')
            (overlay / 'scripts').mkdir()
        program = overlay / 'scripts' / PROGRAM.name
        program.write_text(source)
        return program

    def test_metrics_and_read_only_in_both_formats(self):
        self.populated()
        before = tree_bytes(self.root)
        report = self.report()
        self.assertEqual(report['generated_at'], self.now.isoformat())
        values = report['weeks'][0]['metrics']
        self.assertEqual(values['tickets_eligible']['status'], 'missing')
        self.assertIsNone(values['tickets_eligible']['value'])
        self.assertEqual(values['tickets_eligible']['observed_spec_starts'], 3)
        self.assertEqual(values['tickets_completed']['value'], 2)
        self.assertEqual(values['tickets_abandoned']['value'], 1)
        self.assertEqual(values['merged_without_human_edit']['value'], 1)
        self.assertEqual(values['review_rounds_per_ticket']['value'], 1.5)
        self.assertEqual(values['cost_per_merged_ticket']['value'], 2.0)
        self.assertEqual(values['cost_per_merged_ticket']['per_ticket'][issue(1)]['value'], 3.0)
        self.assertEqual(values['escaped_defects']['value'], 2)
        self.assertEqual({d['fix_pr'] for d in values['escaped_defects']['links']}, {3, 5})
        self.assertEqual(values['triage_to_merge']['status'], 'missing')
        self.assertEqual(values['triage_to_merge']['first_observed_to_merge'][issue(1)]['value'], 3 * 86400)
        self.assertEqual(report['weeks'][0]['observations']['review_dispositions'], {'applied': 1})
        self.assertEqual(report['weeks'][0]['observations']['stage_outcomes']['25'], 1)
        for metric in values.values():
            self.assertIn(metric['status'], ('exact', 'estimated', 'missing'))
            self.assertTrue(metric['sources'])
            self.assertTrue(metric['reason'])
        text = self.cli('md')
        self.assertEqual(text.returncode, 0, text.stderr)
        self.assertIn('triage_to_merge | missing | missing', text.stdout)
        self.assertIn('stage-outcomes.jsonl', text.stdout)
        self.assertEqual(tree_bytes(self.root), before)

    def test_linear_auth_binds_loaded_key_over_inherited_alias(self):
        self.populated()
        self.env['API_KEY'] = 'synthetic-unrelated-key'
        for use_env_file in (False, True):
            with self.subTest(env_file=use_env_file):
                if use_env_file:
                    self.linear_key = 'synthetic-dotenv-key'
                    (self.repo / '.env').write_text('LINEAR_API_KEY=' + self.linear_key + '\n')
                    self.sync()
                before = tree_bytes(self.root)
                report = self.report()
                self.assertTrue(report['sources']['linear']['available'])
                values = report['weeks'][0]['metrics']
                self.assertEqual(values['tickets_completed']['value'], 2)
                self.assertEqual(values['tickets_abandoned']['value'], 1)
                self.assertEqual(tree_bytes(self.root), before)
        # An alias alone cannot authenticate, even with the expected value.
        (self.repo / '.env').unlink()
        self.env.pop('LINEAR_API_KEY')
        self.env['API_KEY'] = self.linear_key
        report = self.report()
        self.assertFalse(report['sources']['linear']['available'])
        self.assertEqual(report['weeks'][0]['metrics']['tickets_abandoned']['status'], 'missing')

    def test_linear_reads_the_resolved_secrets_file(self):
        self.populated()
        self.env.pop('LINEAR_API_KEY')
        config_path = self.repo / '.bureau.json'
        config = json.loads(config_path.read_text())
        external = self.root / 'operator.env'
        config['repo'] = dict(env_file=str(external))
        config_path.write_text(json.dumps(config))
        (self.repo / '.env').write_text('LINEAR_API_KEY=wrong-fixture-key\n')
        for name, path in (('setting', external), ('override', self.repo / 'override.env')):
            with self.subTest(source=name):
                self.linear_key = 'synthetic-' + name + '-key'
                path.write_text('LINEAR_API_KEY=' + self.linear_key + '\n'); path.chmod(0o600)
                if name == 'override': self.env['BUREAU_ENV_FILE'] = path.name
                self.sync()
                before = tree_bytes(self.root)
                report = self.report()
                self.assertTrue(report['sources']['linear']['available'])
                self.assertEqual(tree_bytes(self.root), before)

    def test_ticketless_provider_runs_preserve_ticket_measurements(self):
        self.populated()
        expected = self.report()['weeks'][0]
        # Use the actual manifest/receipt producer, including its null issue and
        # emitted stage outcomes. Standalone port/summary calls own no ticket.
        for stage in ('implement', 'upstream_summary'):
            run = self.run_manifest(None, 1)
            attempt = self.attempt(run, None, stage)
            self.receipt(run, attempt, None, 100, stage=stage, phase=stage,
                structured_result=dict(review_dispositions=[dict(disposition='applied', severity='BUG', path='intent')]))
        before = tree_bytes(self.root)
        report = self.report()
        self.assertEqual(report['weeks'][0], expected)
        self.assertTrue(report['sources']['pr_links']['available'])
        for name in ('manifests', 'outcomes', 'receipts'):
            self.assertTrue(report['sources'][name]['available'])
            self.assertEqual(report['sources'][name]['ticketless_records'], 2)
        self.assertTrue(report['sources']['dispositions']['available'])
        self.assertEqual(report['sources']['dispositions']['ticketless_records'], 1)
        self.assertNotIn('null', report['sources']['receipts']['per_ticket'])
        self.assertEqual(report['sources']['receipts']['unassigned_invalid_records'], 0)
        self.assertEqual(tree_bytes(self.root), before)
        source = PROGRAM.read_text()
        old = "return (valid_issue(row) and IDENTITY.fullmatch(row.get('run_id', ''))"
        self.assertEqual(source.count(old), 1)
        result = self.cli(program=self.copy_program(source.replace(old,
            "return (issue_id(row.get('issue')) and IDENTITY.fullmatch(row.get('run_id', ''))")))
        self.assertEqual(result.returncode, 0, result.stderr)
        regressed = json.loads(result.stdout)
        self.assertFalse(regressed['sources']['manifests']['available'])
        self.assertEqual(regressed['weeks'][0]['metrics']['cost_per_merged_ticket']['status'], 'missing')
        # Explicit null is valid; a missing/invalid issue or damaged attempt is not.
        path = self.store.manifest_path(run)
        original = json.loads(path.read_text())
        missing_issue = dict(original)
        missing_issue.pop('issue')
        for damaged in (missing_issue, dict(original, issue=''), dict(original, attempts=[{}])):
            with self.subTest(damaged=damaged):
                path.write_text(json.dumps(damaged))
                self.assertFalse(self.report()['sources']['manifests']['available'])
        path.write_text(json.dumps(original))
        # Null on a receipt in a ticket-owned run is still an identity mismatch.
        receipt_path = self.store.receipt_path(self.run, self.call['provider_attempt_id'])
        receipt = json.loads(receipt_path.read_text())
        receipt_path.write_text(json.dumps(dict(receipt, issue=None)))
        costs = self.metrics()['cost_per_merged_ticket']['per_ticket']
        self.assertEqual(costs[issue(1)]['status'], 'missing')
        self.assertEqual(costs[issue(2)]['value'], 1)

    def test_base_merges_require_verified_integration_and_no_extra_edits(self):
        self.populated()
        self.git('checkout', '-qb', 'fixture-feature')
        (self.repo / 'feature').write_text('generated change\n')
        self.git('add', 'feature')
        self.git('commit', '-qm', 'feat: fixture\n\nBureau-Generated: true')
        feature = self.git('rev-parse', 'HEAD')
        self.git('checkout', '-qb', 'fixture-base', self.head)
        (self.repo / 'base').write_text('base change\n')
        self.git('add', 'base')
        self.git('commit', '-qm', 'base update')
        base = self.git('rev-parse', 'HEAD')
        self.git('update-ref', 'refs/remotes/origin/main', base)
        self.git('checkout', '-q', 'fixture-feature')
        # This is the runtime's ordinary merge command, with no attribution trailer.
        self.git('merge', '--no-ff', '--no-edit', base)
        merge = self.git('rev-parse', 'HEAD')
        commits = self.prs[0]['commits'] = [dict(oid=oid,
            messageHeadline=self.git('show', '-s', '--format=%s', oid),
            messageBody=self.git('show', '-s', '--format=%b', oid)) for oid in (feature, merge)]
        self.assertNotIn('Bureau-Generated:', commits[1]['messageBody'])
        self.sync()
        before = tree_bytes(self.root)
        values = self.metrics()['merged_without_human_edit']
        self.assertEqual(values['value'], 1)
        self.assertIs(values['per_pr']['1'], True)
        self.assertEqual(tree_bytes(self.root), before)
        source = PROGRAM.read_text()
        old = 'or verified_integration_merge(repo, pr, c)'
        self.assertEqual(source.count(old), 1)
        result = self.cli(program=self.copy_program(source.replace(old, 'or False')))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['weeks'][0]['metrics']['merged_without_human_edit']['value'], 0)
        old = "'merge-tree', '--write-tree', first, second"
        self.assertEqual(source.count(old), 1)
        result = self.cli(program=self.copy_program(source.replace(old,
            "'unsupported-merge-tree', '--write-tree', first, second")))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['weeks'][0]['metrics']['merged_without_human_edit']['status'], 'missing')
        # Unavailable objects/base refs cannot be guessed from a merge title.
        self.git('update-ref', '-d', 'refs/remotes/origin/main')
        self.assertEqual(self.metrics()['merged_without_human_edit']['status'], 'missing')
        self.git('update-ref', 'refs/remotes/origin/main', base)
        commits[1]['oid'] = 'f' * 40
        self.sync()
        self.assertIsNone(self.metrics()['merged_without_human_edit']['per_pr']['1'])
        commits[1]['oid'] = merge
        self.sync()
        # A second parent outside the base history is not a base integration.
        self.git('update-ref', 'refs/remotes/origin/main', self.head)
        self.assertEqual(self.metrics()['merged_without_human_edit']['value'], 0)
        self.git('update-ref', 'refs/remotes/origin/main', base)
        # Even a two-parent base merge with the original title must have exactly
        # the automatic merge tree; a human edit during integration disqualifies it.
        (self.repo / 'manual-edit').write_text('extra change\n')
        self.git('add', 'manual-edit')
        self.git('commit', '--amend', '--no-edit', '-q')
        commits[1]['oid'] = self.git('rev-parse', 'HEAD')
        self.sync()
        before = tree_bytes(self.root)
        values = self.metrics()['merged_without_human_edit']
        self.assertEqual(values['value'], 0)
        self.assertIs(values['per_pr']['1'], False)
        self.assertEqual(tree_bytes(self.root), before)

    def test_defects_reference_every_earlier_owned_pr(self):
        self.populated()
        self.run_manifest(issue(1), 6)
        self.pr(6, issue(1), merged=self.start + timedelta(days=5))
        fix = self.pr(7, title='fix: follow-up', body='Repairs #6.', merged=self.start + timedelta(days=6))
        self.sync()
        before = tree_bytes(self.root)
        defects = self.metrics()['escaped_defects']
        self.assertEqual(defects['value'], 3)
        self.assertEqual([d for d in defects['links'] if d['fix_pr'] == 7],
                         [dict(fix_pr=7, original_pr=6, ticket=issue(1))])
        self.assertEqual(tree_bytes(self.root), before)
        source = PROGRAM.read_text()
        old = "for original in prs:\n                issue = owners.get(original['number'])"
        self.assertEqual(source.count(old), 1)
        result = self.cli(program=self.copy_program(source.replace(old,
            "for issue, original in origins.items():\n                issue = owners.get(original['number'])")))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['weeks'][0]['metrics']['escaped_defects']['value'], 2)
        # Multiple references to the same ticket/PR still count a fix only once.
        fix['body'] = 'Repairs #1 and #6; regression in ' + issue(1) + '.'
        self.sync()
        defects = self.metrics()['escaped_defects']
        self.assertEqual(defects['value'], 3)
        self.assertEqual({d['original_pr'] for d in defects['links'] if d['fix_pr'] == 7}, {1, 6})
        # Equal-time/future merges and PRs without ownership cannot be origins.
        fix['body'] = 'Repairs #6.'
        for merged in (self.prs[-2]['mergedAt'], (self.start + timedelta(days=4)).isoformat()):
            fix['mergedAt'] = merged
            self.sync()
            self.assertEqual(self.metrics()['escaped_defects']['value'], 2)
        fix['mergedAt'] = (self.start + timedelta(days=6, hours=1)).isoformat()
        fix['body'] = 'Repairs #4.'
        self.sync()
        self.assertEqual(self.metrics()['escaped_defects']['value'], 2)

    def test_fix_titles_accept_only_the_optional_ticket_prefix(self):
        self.populated()
        fix = self.prs[2]
        for kind in ('fix', 'bugfix', 'hotfix', 'revert', 'FIX(parser)!'):
            for prefix in ('', issue(6) + ': '):
                with self.subTest(kind=kind, prefix=bool(prefix)):
                    fix['title'] = prefix + kind + ': regression'
                    self.sync()
                    self.assertEqual(self.metrics()['escaped_defects']['value'], 2)
        for title in (issue(6) + ': docs: references', 'feat: fix: later', 'release: fix: later'):
            with self.subTest(nonfix=title):
                fix['title'] = title
                self.sync()
                self.assertEqual(self.metrics()['escaped_defects']['value'], 1)
        fix['title'] = issue(6) + ': fix: unrelated'
        fix['body'] = ''
        self.sync()
        self.assertEqual(self.metrics()['escaped_defects']['value'], 1)

    def test_approval_reuse_and_gate_polls_do_not_consume_rounds(self):
        self.populated()
        for code in (0, 2, 2):
            self.attempt(self.run, issue(1), 'code_review', code)
        rounds = self.metrics()['review_rounds_per_ticket']
        self.assertEqual(rounds['value'], 1.5)
        self.assertEqual(rounds['per_ticket'][issue(1)]['value'], 2)
        self.assertEqual(rounds['per_ticket'][issue(2)]['value'], 1)
        # A reserved but interrupted round counts before any provider receipt.
        a = self.attempt(self.run, issue(1), 'code_review', 22)
        self.store.review_round(self.run, a, 3, 'BUG')
        rounds = self.metrics()['review_rounds_per_ticket']
        self.assertEqual(rounds['value'], 2)
        self.assertEqual(rounds['per_ticket'][issue(1)]['value'], 3)
        # Provider activity without its round record must still make coverage missing.
        a = self.attempt(self.run, issue(1), 'code_review', 22)
        self.receipt(self.run, a, issue(1), .5, phase='correctness', stage='code_review')
        rounds = self.metrics()['review_rounds_per_ticket']
        self.assertEqual(rounds['status'], 'missing')
        self.assertEqual(rounds['per_ticket'][issue(1)]['status'], 'missing')

    def test_orphan_review_receipts_prevent_provider_free_reuse(self):
        self.populated()
        attempt = self.attempt(self.run, issue(1), 'code_review', 22)
        # The producer keeps receipts even when the optional manifest link fails.
        with patch.object(self.store, '_link_receipt', side_effect=OSError), patch.object(e, 'warn'):
            receipt = self.receipt(self.run, attempt, issue(1), .5, phase='correctness', stage='code_review')
        path = self.store.receipt_path(self.run, receipt['provider_attempt_id'])
        manifest = json.loads(self.store.manifest_path(self.run).read_text())
        self.assertTrue(path.is_file())
        self.assertEqual(next(a for a in manifest['attempts'] if a['attempt_id'] == attempt)['receipts'], [])
        # A receipt for another ticket/run/attempt cannot turn a provider-free
        # attempt into an unrecorded round merely because one identity matches.
        original = json.loads(path.read_text())
        for key, value in (('issue', issue(2)), ('stage_attempt_id', self.identity())):
            with self.subTest(foreign=key):
                path.write_text(json.dumps(dict(original, **{key: value})))
                self.assertEqual(self.metrics()['review_rounds_per_ticket']['value'], 1.5)
        path.unlink()
        foreign_run = self.identity()
        foreign_path = self.store.receipt_path(foreign_run, receipt['provider_attempt_id'])
        foreign_path.parent.mkdir(parents=True)
        foreign_path.write_text(json.dumps(dict(original, run_id=foreign_run)))
        self.assertEqual(self.metrics()['review_rounds_per_ticket']['value'], 1.5)
        foreign_path.unlink()
        path.write_text(json.dumps(original))
        before = tree_bytes(self.root)
        rounds = self.metrics()['review_rounds_per_ticket']
        self.assertEqual(rounds['status'], 'missing')
        self.assertEqual(rounds['per_ticket'][issue(1)]['status'], 'missing')
        self.assertEqual(rounds['per_ticket'][issue(2)]['value'], 1)
        self.assertIn('receipts', rounds['per_ticket'][issue(1)]['sources'])
        self.assertEqual(tree_bytes(self.root), before)
        source = PROGRAM.read_text()
        lookup = "or (i, run, a['attempt_id']) in receipt_attempts"
        self.assertEqual(source.count(lookup), 1)
        result = self.cli(program=self.copy_program(source.replace(lookup, '')))
        self.assertEqual(result.returncode, 0, result.stderr)
        regressed = json.loads(result.stdout)['weeks'][0]['metrics']['review_rounds_per_ticket']
        self.assertEqual(regressed['status'], 'estimated')
        self.assertEqual(regressed['per_ticket'][issue(1)]['value'], 2)
        self.assertEqual(regressed['value'], 1.5)
        # Adding only the optional link cannot change the measurement.
        self.store._link_receipt(receipt, path)
        self.assertEqual(self.metrics()['review_rounds_per_ticket'], rounds)
        self.store.review_round(self.run, attempt, 3, 'BUG')
        self.assertEqual(self.metrics()['review_rounds_per_ticket']['per_ticket'][issue(1)]['value'], 3)

    def test_resolver_receipts_keep_costs_and_validation_is_not_a_call(self):
        self.populated()
        attempt = self.attempt(self.run, issue(1), 'rebase')
        call = self.receipt(self.run, attempt, issue(1), .75,
                            phase='conflict_resolver', stage='conflict_resolver')
        validation, path = self.resolver_validation(self.run, attempt, issue(1), 'rebase')
        observation = json.loads(path.read_text())
        self.assertIsNone(observation['started_at'])
        self.assertIsNone(observation['ended_at'])
        self.assertIsNone(observation['usage']['total_cost_usd'])
        self.assertIn('conflict_resolution', observation)
        before = tree_bytes(self.root)
        report = self.report()
        self.assertTrue(report['sources']['receipts']['available'])
        costs = report['weeks'][0]['metrics']['cost_per_merged_ticket']
        self.assertEqual(costs['status'], 'estimated')
        self.assertEqual(costs['value'], 2.375)
        self.assertEqual(costs['per_ticket'][issue(1)]['value'], 3.75)
        self.assertEqual(costs['per_ticket'][issue(1)]['attempts'], 4)
        self.assertEqual(costs['per_ticket'][issue(2)]['value'], 1)
        self.assertEqual(tree_bytes(self.root), before)
        source = PROGRAM.read_text()
        controls = (
            ("or attempt['stage'] in NESTED_RECEIPT_STAGES.get(row.get('stage'), ())",
             'or False', 'linked receipts'),
            ("        if receipt.get('stage') == 'conflict_resolver' and receipt.get('phase') == 'validation':\n"
             "            continue\n", '', 'no completion timestamp'),
        )
        for old, new, reason in controls:
            with self.subTest(regression=old.strip()):
                self.assertEqual(source.count(old), 1)
                result = self.cli(program=self.copy_program(source.replace(old, new)))
                self.assertEqual(result.returncode, 0, result.stderr)
                regressed = json.loads(result.stdout)
                self.assertTrue(regressed['sources']['receipts']['available'])
                values = regressed['weeks'][0]['metrics']['cost_per_merged_ticket']
                self.assertEqual(values['status'], 'missing')
                self.assertEqual(values['per_ticket'][issue(1)]['status'], 'missing')
                self.assertEqual(values['per_ticket'][issue(2)]['value'], 1)
                self.assertIn(reason, values['per_ticket'][issue(1)]['reason'])
        # Missing timing/usage for the actual resolver call is still missing,
        # and the validation exception must not accept unrelated stages/IDs.
        call_path = self.store.receipt_path(self.run, call['provider_attempt_id'])
        original = json.loads(call_path.read_text())
        for change in (dict(ended_at=None), dict(usage={})):
            with self.subTest(provider_gap=change):
                call_path.write_text(json.dumps(dict(original, **change)))
                values = self.metrics()['cost_per_merged_ticket']['per_ticket']
                self.assertEqual(values[issue(1)]['status'], 'missing')
                self.assertEqual(values[issue(2)]['value'], 1)
        for change in (dict(stage='qa'), dict(issue=issue(2)), dict(stage_attempt_id=self.identity())):
            with self.subTest(mismatch=change):
                call_path.write_text(json.dumps(dict(original, **change)))
                report = self.report()
                self.assertTrue(report['sources']['receipts']['available'])
                self.assertFalse(report['sources']['receipts']['per_ticket'][issue(1)]['available'])
                self.assertEqual(report['weeks'][0]['metrics']['cost_per_merged_ticket']['per_ticket'][issue(2)]['value'], 1)
        call_path.write_text(json.dumps(original))
        # Validation alone cannot supply evidence of zero provider cost.
        ticket = issue(7)
        run = self.run_manifest(ticket, 7)
        attempt = self.attempt(run, ticket, 'rebase')
        self.store.put_receipt(dict(validation, run_id=run, stage_attempt_id=attempt,
                                    provider_attempt_id=self.identity(), issue=ticket))
        self.pr(7, ticket)
        self.snapshot(ticket, completed=self.prs[-1]['mergedAt'])
        self.sync()
        costs = self.metrics()['cost_per_merged_ticket']
        self.assertEqual(costs['status'], 'missing')
        self.assertIsNone(costs['per_ticket'][ticket]['value'])
        self.assertEqual(costs['per_ticket'][issue(1)]['value'], 3.75)

    def test_all_documented_nested_receipt_producers_preserve_costs(self):
        self.populated()
        # Enumerated from run_stage_for callers and both put_receipt writers:
        # spec-pipeline.sh research; merge_origin_main_or_abort in six pipelines;
        # rebase-pipeline.sh; and bureau-conflicts.py validation in each resolver.
        relationships = (
            ('spec', 'research'), ('ux', 'conflict_resolver'), ('copy', 'conflict_resolver'),
            ('implement', 'conflict_resolver'), ('qa', 'conflict_resolver'),
            ('code_review', 'conflict_resolver'), ('merge', 'conflict_resolver'), ('rebase', 'conflict_resolver'),
        )
        total = 3.0
        for number, (enclosing, nested) in enumerate(relationships, 1):
            with self.subTest(enclosing=enclosing, producer=nested):
                attempt = self.attempt(self.run, issue(1), enclosing)
                self.receipt(self.run, attempt, issue(1), .25, stage=nested, phase=nested)
                if nested == 'conflict_resolver':
                    self.resolver_validation(self.run, attempt, issue(1), 'rebase' if enclosing == 'rebase' else 'merge')
                total += .25
                before = tree_bytes(self.root)
                report = self.report()
                self.assertTrue(report['sources']['receipts']['available'])
                self.assertTrue(report['sources']['receipts']['per_ticket'][issue(1)]['available'])
                costs = report['weeks'][0]['metrics']['cost_per_merged_ticket']
                self.assertEqual(costs['status'], 'estimated')
                self.assertEqual(costs['value'], (total + 1) / 2)
                self.assertEqual(costs['per_ticket'][issue(1)]['value'], total)
                self.assertEqual(costs['per_ticket'][issue(1)]['attempts'], 3 + number)
                self.assertEqual(costs['per_ticket'][issue(2)]['value'], 1)
                self.assertEqual(tree_bytes(self.root), before)
        # Reproduce the research regression without removing the resolver fix.
        source = PROGRAM.read_text()
        old = "'research': frozenset(('spec',)),"
        self.assertEqual(source.count(old), 1)
        result = self.cli(program=self.copy_program(source.replace(old, "'research': frozenset(),")))
        self.assertEqual(result.returncode, 0, result.stderr)
        costs = json.loads(result.stdout)['weeks'][0]['metrics']['cost_per_merged_ticket']
        self.assertEqual(costs['per_ticket'][issue(1)]['status'], 'missing')
        self.assertEqual(costs['per_ticket'][issue(2)]['value'], 1)
        with self.assertRaises(AssertionError):
            self.assertEqual(costs['value'], (total + 1) / 2)

    def test_receipt_failures_are_scoped_to_the_owning_ticket(self):
        self.populated()
        attempt = self.attempt(self.run, issue(1), 'spec')
        receipt = self.receipt(self.run, attempt, issue(1), .25, stage='research', phase='research')
        path = self.store.receipt_path(self.run, receipt['provider_attempt_id'])
        original = json.loads(path.read_text())
        manifest_path = self.store.manifest_path(self.run)
        manifest_text = manifest_path.read_text()
        changes = (dict(issue=issue(2)), dict(run_id=self.identity()), dict(stage_attempt_id=self.identity()),
                   dict(provider_attempt_id=self.identity()), dict(stage='qa'), dict(stage='conflict_resolver'))
        for linked in (True, False):
            manifest = json.loads(manifest_text)
            if not linked:
                next(a for a in manifest['attempts'] if a['attempt_id'] == attempt)['receipts'] = []
            manifest_path.write_text(json.dumps(manifest))
            damaged = [json.dumps(dict(original, **change)) for change in changes] + ['invalid', '', '[]', '{}']
            for raw in damaged:
                with self.subTest(linked=linked, damaged=raw):
                    path.write_text(raw)
                    before = tree_bytes(self.root)
                    report = self.report()
                    source = report['sources']['receipts']
                    self.assertTrue(source['available'])
                    self.assertFalse(source['per_ticket'][issue(1)]['available'])
                    self.assertTrue(source['per_ticket'][issue(2)]['available'])
                    costs = report['weeks'][0]['metrics']['cost_per_merged_ticket']
                    self.assertEqual(costs['status'], 'missing')
                    self.assertEqual(costs['per_ticket'][issue(1)]['status'], 'missing')
                    self.assertEqual(costs['per_ticket'][issue(2)]['value'], 1)
                    self.assertEqual(tree_bytes(self.root), before)
        # Research is only a child of spec, even with fully matching identities.
        path.write_text(json.dumps(original))
        for parent in ('implement', 'rebase', 'spec_review'):
            with self.subTest(undocumented_parent=parent):
                manifest = json.loads(manifest_text)
                next(a for a in manifest['attempts'] if a['attempt_id'] == attempt)['stage'] = parent
                manifest_path.write_text(json.dumps(manifest))
                costs = self.metrics()['cost_per_merged_ticket']['per_ticket']
                self.assertEqual(costs[issue(1)]['status'], 'missing')
                self.assertEqual(costs[issue(2)]['value'], 1)
        # A missing link target is also local; restoring the old global gate
        # demonstrably suppresses the unrelated ticket's otherwise complete cost.
        manifest_path.write_text(manifest_text)
        path.unlink()
        report = self.report()
        self.assertTrue(report['sources']['receipts']['available'])
        self.assertEqual(report['weeks'][0]['metrics']['cost_per_merged_ticket']['per_ticket'][issue(2)]['value'], 1)
        source = PROGRAM.read_text()
        old = "coverage = inputs.sources['receipts']['per_ticket'].get(issue, inputs.sources['receipts'])"
        new = ("coverage = next((v for v in inputs.sources['receipts']['per_ticket'].values() "
               "if not v['available']), inputs.sources['receipts'])")
        self.assertEqual(source.count(old), 1)
        result = self.cli(program=self.copy_program(source.replace(old, new)))
        self.assertEqual(result.returncode, 0, result.stderr)
        costs = json.loads(result.stdout)['weeks'][0]['metrics']['cost_per_merged_ticket']['per_ticket']
        self.assertEqual(costs[issue(2)]['status'], 'missing')
        with self.assertRaises(AssertionError):
            self.assertEqual(costs[issue(2)]['value'], 1)

    def test_receipt_errors_outside_the_merged_cohort_preserve_its_mean(self):
        self.populated()
        # An unmerged ticket's failed receipt cannot disable either merged
        # ticket's cost, or turn their complete weekly mean into missing.
        run = self.run_manifest(issue(4))
        attempt = self.attempt(run, issue(4), 'spec')
        receipt = self.receipt(run, attempt, issue(4), .25, stage='research', phase='research')
        self.store.receipt_path(run, receipt['provider_attempt_id']).write_text('invalid')
        for unassigned in (0, 1):
            with self.subTest(unassigned=unassigned):
                if unassigned:
                    # No manifest or usable issue field: report the unassigned
                    # invalid record explicitly without assigning it globally.
                    path = self.store.receipt_path(self.identity(), self.identity())
                    path.parent.mkdir(parents=True)
                    path.write_text('invalid')
                before = tree_bytes(self.root)
                report = self.report()
                source = report['sources']['receipts']
                self.assertTrue(source['available'])
                self.assertFalse(source['per_ticket'][issue(4)]['available'])
                self.assertEqual(source['unassigned_invalid_records'], unassigned)
                costs = report['weeks'][0]['metrics']['cost_per_merged_ticket']
                self.assertEqual(costs['status'], 'estimated')
                self.assertEqual(costs['value'], 2.0)
                self.assertEqual(costs['per_ticket'][issue(1)]['value'], 3.0)
                self.assertEqual(costs['per_ticket'][issue(2)]['value'], 1.0)
                self.assertEqual(tree_bytes(self.root), before)

    def test_absent_sources_do_not_become_zero(self):
        self.executable('gh', 'raise SystemExit(1)\n')
        self.sync()
        report = self.report()
        for week in report['weeks']:
            for metric in week['metrics'].values():
                self.assertEqual(metric['status'], 'missing')
                self.assertIsNone(metric['value'])
                self.assertTrue(metric['reason'])

    def test_failed_reads_not_empty_results_and_holds_not_abandoned(self):
        self.populated()
        self.snapshots.pop(issue(3))
        self.sync()
        self.assertEqual(self.metrics()['tickets_abandoned']['status'], 'missing')
        self.snapshot(issue(3), 'Build Review')
        self.sync()
        self.assertEqual(self.metrics()['tickets_abandoned']['value'], 0)
        self.executable('gh', 'print("{}")\n')
        self.assertEqual(self.metrics()['tickets_completed']['status'], 'missing')
        self.assertEqual(self.metrics()['escaped_defects']['status'], 'missing')

    def test_cost_identity_fallback_no_double_count_and_unknown_not_zero(self):
        self.populated()
        cost = self.root / 'cost'
        cost.mkdir()
        log = cost / (issue(1) + '.jsonl')
        log.write_text(json.dumps(dict(issue=issue(1), stage='implement', cost_usd=999)) + '\n')
        self.assertEqual(self.metrics()['cost_per_merged_ticket']['value'], 2.0)
        path = self.store.receipt_path(self.run, self.call['provider_attempt_id'])
        receipt = json.loads(path.read_text())
        receipt['usage']['total_cost_usd'] = None
        path.write_text(json.dumps(receipt))
        self.assertEqual(self.metrics()['cost_per_merged_ticket']['status'], 'missing')
        # A unique exact call identity may fill a missing receipt cost.
        log.write_text(json.dumps(dict(issue=issue(1), stage='implement', estimated_cost_usd=2,
                                     provider_attempt_id=self.call['provider_attempt_id'])) + '\n')
        self.assertEqual(self.metrics()['cost_per_merged_ticket']['value'], 2.0)
        log.write_text(log.read_text() * 2)
        self.assertEqual(self.metrics()['cost_per_merged_ticket']['status'], 'missing')

    def test_untimed_legacy_only_log_and_empty_commit_list_are_missing(self):
        self.populated()
        for path in self.store.root.glob('runs/*/receipts/*.json'):
            path.unlink()
        cost = self.root / 'cost'
        cost.mkdir()
        (cost / (issue(1) + '.jsonl')).write_text(json.dumps(dict(issue=issue(1), cost_usd=9)) + '\n')
        self.prs[0]['commits'] = []
        self.sync()
        values = self.metrics()
        self.assertEqual(values['cost_per_merged_ticket']['status'], 'missing')
        self.assertIn('Legacy log', values['cost_per_merged_ticket']['per_ticket'][issue(1)]['reason'])
        self.assertEqual(values['merged_without_human_edit']['status'], 'missing')

    def test_linked_worktree_shares_evidence_and_no_runtime_writes(self):
        self.populated()
        linked = self.root / 'linked checkout'
        self.git('worktree', 'add', '-q', '-b', 'fixture-linked', str(linked))
        # This also checks primary-checkout config fallback and .git file resolution.
        before = tree_bytes(self.root)
        result = self.cli(repo=linked)
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report['weeks'][0]['metrics']['tickets_completed']['value'], 2)
        self.assertEqual(report['weeks'][0]['metrics']['tickets_abandoned']['value'], 1)
        self.assertEqual(tree_bytes(self.root), before)

    def test_iso_year_boundary_and_current_week_is_partial(self):
        self.sync()
        with patch.dict(os.environ, self.env, clear=True):
            report = b.build(b.Inputs(self.repo), 2, datetime(2027, 1, 4, 12, tzinfo=timezone.utc))
        self.assertEqual([w['week'] for w in report['weeks']], ['2026-W53', '2027-W01'])
        self.assertIn('current week is partial', report['week_basis'])

    def test_cli_shares_fixture_clock_across_monday(self):
        self.populated()
        after_midnight = self.now + timedelta(seconds=1)
        original = PROGRAM.read_text()
        fallback = 'now = now or datetime.now(UTC)'
        self.assertEqual(original.count(fallback), 1)
        # Simulate the CLI launching after Monday while setup ran on Sunday.
        source = original.replace(fallback, 'now = now or datetime.fromisoformat(' + repr(after_midnight.isoformat()) + ')')
        result = self.cli(program=self.copy_program(source))
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report['generated_at'], self.now.isoformat())
        self.assertEqual([w['week'] for w in report['weeks']], ['2026-W52', '2026-W53'])
        self.assertEqual(report['weeks'][0]['metrics']['tickets_completed']['value'], 2)
        # Dropping clock injection reproduces the original one-week shift.
        call = 'report = build(Inputs(args.repo.resolve()), args.weeks, now=now)'
        self.assertEqual(source.count(call), 1)
        source = source.replace(call, 'report = build(Inputs(args.repo.resolve()), args.weeks)')
        result = self.cli(program=self.copy_program(source))
        self.assertEqual(result.returncode, 0, result.stderr)
        shifted = json.loads(result.stdout)
        self.assertEqual([w['week'] for w in shifted['weeks']], ['2026-W53', '2027-W01'])
        with self.assertRaises(AssertionError):
            self.assertEqual(shifted['weeks'][0]['metrics']['tickets_completed']['value'], 2)

    def test_custom_terminal_states_and_done_without_pr(self):
        self.populated()
        self.snapshots[issue(3)]['state']['name'] = 'Dropped'
        extra = issue(5)
        self.run_manifest(extra)
        self.snapshot(extra, 'Finished', completed=(self.start + timedelta(days=3)).isoformat())
        (self.repo / '.bureau.json').write_text(json.dumps(dict(linear=dict(teams=[dict(states=dict(
            cancelled=self.snapshots[issue(3)]['state']['id'], done=self.snapshots[extra]['state']['id']))]))))
        self.sync()
        values = self.metrics()
        self.assertEqual(values['tickets_completed']['value'], 3)
        self.assertEqual(values['tickets_completed']['linear_only_completed'], [extra])
        self.assertEqual(values['tickets_abandoned']['value'], 1)
        self.snapshots[issue(1)]['completedAt'] = self.now.isoformat()
        self.sync()
        report = self.report()
        self.assertEqual(report['weeks'][0]['metrics']['tickets_completed']['value'], 3)
        self.assertEqual(report['weeks'][1]['metrics']['tickets_completed']['value'], 0)

    def test_corrupt_sources_and_missing_linked_receipt(self):
        self.populated()
        outcomes = self.store.root / 'stage-outcomes.jsonl'
        outcomes.write_text(outcomes.read_text() + 'invalid\n')
        before = tree_bytes(self.root)
        self.assertIsNone(self.report()['weeks'][0]['observations']['stage_outcomes'])
        self.assertEqual(tree_bytes(self.root), before)  # No quarantine/repair.
        self.store.receipt_path(self.run, self.call['provider_attempt_id']).unlink()
        values = self.metrics()
        self.assertEqual(values['cost_per_merged_ticket']['status'], 'missing')
        self.assertIn('linked receipts', values['cost_per_merged_ticket']['per_ticket'][issue(1)]['reason'])
        manifest = self.store.manifest_path(self.run)
        row = json.loads(manifest.read_text())
        row['pr'] = []
        manifest.write_text(json.dumps(row))
        self.assertEqual(self.metrics()['review_rounds_per_ticket']['status'], 'missing')

    def test_caps_future_references_and_repository_override(self):
        self.populated()
        self.env['GH_REPO'] = 'unrelated'
        self.pr(6, title='fix: earlier', body=issue(1), merged=self.start + timedelta(days=2))
        self.sync()
        self.assertEqual(self.metrics()['escaped_defects']['value'], 2)
        self.prs[0]['commits'] *= 100
        self.sync()
        self.assertEqual(self.metrics()['merged_without_human_edit']['status'], 'missing')
        template = self.prs[-1]
        self.prs = [dict(template, number=n, mergedAt=(self.start - timedelta(weeks=1)).isoformat()) for n in range(1, 1001)]
        self.sync()
        values = self.metrics()
        self.assertEqual(values['tickets_completed']['status'], 'missing')
        self.assertEqual(values['escaped_defects']['status'], 'missing')
        self.assertEqual(values['cost_per_merged_ticket']['status'], 'missing')
        self.assertEqual(values['review_rounds_per_ticket']['status'], 'missing')

    def test_conflicting_pr_links_and_missing_rounds_do_not_understate_metrics(self):
        self.populated()
        path = self.store.manifest_path(self.run)
        row = json.loads(path.read_text())
        for attempt in row['attempts']:
            attempt.pop('review_round', None)
        path.write_text(json.dumps(row))
        self.assertEqual(self.metrics()['review_rounds_per_ticket']['status'], 'missing')
        self.run_manifest(issue(6), 1)
        self.snapshot(issue(6), 'Build')
        self.sync()
        values = self.metrics()
        for name in ('tickets_completed', 'cost_per_merged_ticket', 'review_rounds_per_ticket',
                     'merged_without_human_edit', 'escaped_defects'):
            self.assertEqual(values[name]['status'], 'missing')

    def test_read_only_check_negative_control(self):
        self.populated()
        # Prove the same snapshot assertion rejects a write, not just plausible output.
        original = PROGRAM.read_text()
        program = self.copy_program(original)
        before = tree_bytes(self.repo)
        self.assertEqual(self.cli(program=program).returncode, 0)
        self.assertEqual(tree_bytes(self.repo), before)
        call = 'report = build(Inputs(args.repo.resolve()), args.weeks, now=now)'
        self.assertEqual(original.count(call), 1)
        program.write_text(original.replace(call,
            "(args.repo / 'negative-control-write').write_text('changed')\n        " + call))
        self.assertEqual(self.cli(program=program).returncode, 0)
        with self.assertRaises(AssertionError):
            self.assertEqual(tree_bytes(self.repo), before)

    def test_review_finding_negative_controls(self):
        self.populated()
        self.env['API_KEY'] = 'synthetic-unrelated-key'
        self.attempt(self.run, issue(1), 'code_review', 2)
        original = PROGRAM.read_text()
        controls = (
            ('auth-alias', 'bureau_secret_copy API_KEY LINEAR_API_KEY || exit\n', '',
             'tickets_abandoned', 1, None),
            ('prefixed-fix', "rf'^(?:{ISSUE.pattern}:[ \\t]*)?", "r'^",
             'escaped_defects', 2, 1),
            ('provider-free-review', "and ('review_round' in a or a['receipts'] or (i, run, a['attempt_id']) in receipt_attempts)", 'and True',
             'review_rounds_per_ticket', 1.5, None),
        )
        for name, old, new, metric, expected, regressed in controls:
            with self.subTest(control=name):
                self.assertEqual(original.count(old), 1)
                result = self.cli(program=self.copy_program(original.replace(old, new)))
                self.assertEqual(result.returncode, 0, result.stderr)
                measured = json.loads(result.stdout)['weeks'][0]['metrics'][metric]
                self.assertEqual(measured['value'], regressed)
                self.assertEqual(measured['status'], 'missing' if regressed is None else 'estimated')
                with self.assertRaises(AssertionError):
                    self.assertEqual(measured['value'], expected)


if __name__ == '__main__':
    unittest.main()
