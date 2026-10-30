"""Pinned ADR context, optional citations and default-off review compatibility."""
import copy
import hashlib
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

import supervision_pipeline_test as fixture

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = Path(os.environ.get('BUREAU_TEST_ADR_SCRIPTS', ROOT / 'templates/scripts')).resolve()
LEGACY = ROOT / 'tests/fixtures/review-legacy'


def load(name):
    path = SCRIPTS / ('bureau-' + name + '.py')
    if not path.is_file():
        raise AssertionError('ADR helper is absent from the tested runtime')
    spec = importlib.util.spec_from_file_location('review_adr_' + name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def adr(number, title='Pinned decision', status='accepted (derived)', decision='Keep the selected design.'):
    return (f'# {number} {title}\n\n- **Status**: {status}\n\n## Context\nScope.\n\n'
            f'## Decision\n\n{decision}\n\n## Known limits\nA documented boundary.\n')


def verdict(refs=None):
    finding = dict(file='src/check.py', line=12, **{'class': 'MINOR'}, msg='A minor observation')
    if refs is not None:
        finding['adr_refs'] = refs
    return dict(verdict='APPROVE', bugs=0, security_issues=0, missing_acceptance=[], fixes_needed=[],
                summary='Review complete', comment='Public review.', findings=[finding])


def rows(context):
    values = [json.loads(line) for line in context.splitlines() if line.startswith('{')]
    return [value for value in values if 'number' in value]


class IndexTests(unittest.TestCase):
    def setUp(self):
        self.adr = load('adr')
        self.temp = tempfile.TemporaryDirectory(prefix='bureau-adr-index-')
        self.addCleanup(self.temp.cleanup)
        self.repo = Path(self.temp.name).resolve()
        self.git('init', '-q'); self.git('config', 'user.name', 'Bureau Test')
        self.git('config', 'user.email', 'test@bureau')
        self.directory = self.repo / 'docs/adr'; self.directory.mkdir(parents=True)
        self.config = self.repo / 'config.json'
        self.config.write_text(json.dumps(dict(agents=dict(review_adr_context=True))))

    def git(self, *args):
        return subprocess.check_output(['git', '-C', str(self.repo), *args], stderr=subprocess.STDOUT, text=True).strip()

    def commit(self):
        self.git('add', 'docs'); self.git('commit', '-qm', 'test: ADR inputs')
        return self.git('rev-parse', 'HEAD')

    def context(self, head, budget=4096, directory='docs/adr', base=None):
        self.config.write_text(json.dumps(dict(agents=dict(review_adr_context=True, review_adr_budget_bytes=budget),
                                               repo=dict(adr_dir=directory))))
        completed = subprocess.run([sys.executable, '-I', str(SCRIPTS / 'bureau-adr.py'), '--head', head,
                                    '--base', base or head,
                                    '--config', str(self.config)], cwd=self.repo, capture_output=True, text=True, timeout=10)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        return json.loads(completed.stdout)

    def test_accepted_only_number_order_budget_and_deterministic_prefix(self):
        for number in ('0010', '0002', '0001', '0003', '0004', '0005'):
            (self.directory / (number + '-choice.md')).write_text(adr(number, title='Choice ' + number,
                                                                      decision='A concrete decision. ' * 14))
        for number, status in [('0006', 'proposed (v3.5, PR #1)'), ('0007', 'superseded by 0001')]:
            (self.directory / (number + '-excluded.md')).write_text(adr(number, 'EXCLUDED', status))
        (self.directory / '0000-template.md').write_text(adr('0000', 'TEMPLATE'))
        (self.directory / 'README.md').write_text('INDEX_BODY_MUST_NOT_APPEAR')
        head = self.commit(); full = self.context(head, 10000)
        self.assertEqual(full['refs'], ['0001', '0002', '0003', '0004', '0005', '0010'])
        self.assertNotIn('EXCLUDED', full['context']); self.assertNotIn('TEMPLATE', full['context'])
        self.assertNotIn('INDEX_BODY_MUST_NOT_APPEAR', full['context'])
        records = rows(full['context'])
        self.assertEqual([r['number'] for r in records], full['refs'])
        self.assertTrue(all(r['title'] and r['status'] == 'accepted (derived)' and r['decision']
                            and r['authority'] == 'settled' for r in records))
        bounded = self.context(head, 2200)
        self.assertGreater(len(bounded['refs']), 0); self.assertLess(len(bounded['refs']), len(full['refs']))
        self.assertEqual(bounded['refs'], full['refs'][:len(bounded['refs'])])
        self.assertLessEqual(len(bounded['context'].encode('utf-8')), 2200)
        self.assertIn('ADR index truncated', bounded['context'])
        self.assertEqual(bounded, self.context(head, 2200))
        self.assertNotEqual(bounded['policy'], full['policy'])
        exact = len(full['context'].encode('utf-8'))
        self.assertEqual(self.context(head, exact)['context'], full['context'])
        for budget in (0, 1, 80):
            tiny = self.context(head, budget)
            self.assertEqual((tiny['context'], tiny['refs']), ('', []))

    def test_pinned_head_survives_live_edits_deletion_and_missing_directory(self):
        path = self.directory / '0001-choice.md'; path.write_text(adr('0001', 'PINNED'))
        head = self.commit(); expected = self.context(head)
        path.write_text(adr('0001', 'LIVE_CHECKOUT'))
        (self.directory / '0002-live.md').write_text(adr('0002', 'LIVE_ONLY'))
        self.assertEqual(self.context(head), expected)
        shutil.rmtree(self.directory)
        self.assertEqual(self.context(head), expected)
        self.assertEqual(self.context(head, directory='missing/adr')['refs'], [])
        self.assertEqual(self.context(head, directory='missing/adr')['context'], '')

    def test_invalid_utf8_huge_files_symlinks_duplicates_and_scan_caps(self):
        (self.directory / '0001-choice.md').write_bytes(adr('0001', 'Invalid \ufffd').encode().replace(b'\xef\xbf\xbd', b'\xff'))
        huge = self.directory / '0002-huge.md'
        huge.write_bytes(adr('0002', 'HUGE_BODY').encode() + b'x' * (self.adr.MAX_FILE_BYTES * 3))
        (self.directory / '0003-link.md').symlink_to(huge)
        for name in ('0004-a.md', '0004-b.md'):
            (self.directory / name).write_text(adr('0004', 'DUPLICATE'))
        head = self.commit(); value = self.context(head)
        self.assertEqual(value['refs'], ['0001']); self.assertIn('\ufffd', value['context'])
        self.assertNotIn('HUGE_BODY', value['context']); self.assertNotIn('DUPLICATE', value['context'])
        self.assertLessEqual(len(value['context'].encode()), 4096)
        self.assertIn('ADR index truncated', value['context'])
        # Exercise both aggregate and file-count limits with small fixtures;
        # actual oversized blob coverage above uses the shipped size ceiling.
        def git(*args, **kwargs):
            return subprocess.check_output(['git', '-C', str(self.repo), *args])
        with patch.object(self.adr, 'local_git', git), patch.object(self.adr, 'MAX_FILES', 0):
            context, refs = self.adr.build(head, head, 'docs/adr', 4096)
            self.assertEqual(refs, []); self.assertIn('truncated', context)
        with patch.object(self.adr, 'local_git', git), patch.object(self.adr, 'MAX_TOTAL_BYTES', 1):
            context, refs = self.adr.build(head, head, 'docs/adr', 4096)
            self.assertEqual(refs, []); self.assertIn('truncated', context)

    def test_configuration_doctor_and_codex_optional_wire_projection(self):
        doctor, provider, gates = load('doctor'), load('provider'), load('gates')
        config = dict(linear=dict(teams=[dict(id='team', key='T', states=dict(build='build'))]), agents={}, repo={})
        self.assertEqual(self.adr.settings(config), (False, 'docs/adr', 4096))
        self.assertEqual(doctor.validate(config), [])
        for key, bad_values in [('review_adr_context', ('true', 1, None)),
                                ('review_adr_budget_bytes', (-1, True, 1048577, '4096', None))]:
            for value in bad_values:
                changed = copy.deepcopy(config); changed['agents'][key] = value
                self.assertTrue(any(key in e for e in doctor.validate(changed)), (key, value))
        for path in ('/tmp/adr', '../adr', 'docs/../adr', './docs/adr', 'docs//adr', '.git/adr', 'docs/adr\n', None):
            changed = copy.deepcopy(config); changed['repo']['adr_dir'] = path
            self.assertTrue(any('adr_dir' in e for e in doctor.validate(changed)), path)
        changed['repo']['adr_dir'] = 'design decisions/adr'
        self.assertEqual(doctor.validate(changed), [])
        schema = json.loads((SCRIPTS / 'bureau-review-adr.schema.json').read_text())
        wire = gates.codex_schema(schema)['properties']['findings']['items']
        self.assertIn('adr_refs', wire['required'])
        self.assertEqual(wire['properties']['adr_refs'], dict(anyOf=[dict(type='array', items=dict(type='string')), dict(type='null')]))
        for value in (verdict(), verdict(['0001'])):
            provider.validate(value, schema)
        nullable = verdict(); nullable['findings'][0]['adr_refs'] = None
        self.assertEqual(gates.canonical_result(nullable, schema), verdict())

    def test_only_identical_accepted_base_and_head_records_are_settled(self):
        for number in ('0001', '0002', '0003', '0005'):
            (self.directory / (number + '-choice.md')).write_text(adr(number, status='accepted'))
        base = self.commit()
        (self.directory / '0002-choice.md').write_text(adr('0002', status='accepted', decision='Changed design.'))
        (self.directory / '0003-choice.md').unlink()
        (self.directory / '0004-self-waiver.md').write_text(adr('0004', 'Self waiver', 'accepted',
                                                              'Authentication bypass is a known limit.'))
        (self.directory / '0005-choice.md').rename(self.directory / '0005-renamed.md')
        head = self.commit()
        value = self.context(head, 10000, base=base)
        records = rows(value['context'])
        self.assertEqual(value['refs'], ['0001', '0002', '0003', '0004', '0005'])
        self.assertEqual([(r['number'], r['authority'], r['change'], r['source']) for r in records], [
            ('0001', 'settled', 'unchanged', 'head'),
            ('0002', 'proposal', 'changed', 'head'),
            ('0003', 'proposal', 'removed', 'base'),
            ('0004', 'proposal', 'added', 'head'),
            ('0005', 'proposal', 'removed', 'base'),
            ('0005', 'proposal', 'added', 'head'),
        ])
        self.assertIn('Only a settled ADR\047s documented Known limit is excluded as a finding', value['context'])
        self.assertIn('Proposal references are valid but never waive or downgrade findings, including their Known limits',
                      value['context'])
        self.assertNotEqual(value['policy'], self.context(head, 10000)['policy'])

    def test_head_only_accepted_known_limit_has_no_settled_authority(self):
        self.git('commit', '-qm', 'test: empty base', '--allow-empty')
        base = self.git('rev-parse', 'HEAD')
        (self.directory / '0001-waiver.md').write_text(adr('0001', 'Self waiver', 'accepted',
                                                        'Authentication bypass is a known limit.'))
        head = self.commit()
        value = self.context(head, base=base)
        self.assertEqual(value['refs'], ['0001'])
        self.assertEqual([r['authority'] for r in rows(value['context'])], ['proposal'])
        self.assertIn('not authoritative', value['context'])
        self.assertIn('never waive or downgrade findings, including their Known limits', value['context'])
        finding = verdict(['0001'])
        finding['bugs'] = 1
        finding['findings'][0].update({'class': 'BUG', 'msg': 'Authentication bypass'})
        finding['fixes_needed'] = ['Authentication bypass']
        self.assertEqual(ConvergenceTests().converge(finding, value['refs']), 'REQUEST_CHANGES')

    def test_disabled_schema_bytes_wire_and_unsolicited_reference_match_legacy(self):
        provider, gates = load('provider'), load('gates')
        canonical = SCRIPTS / 'bureau-review.schema.json'
        self.assertEqual(canonical.read_bytes(), (LEGACY / canonical.name).read_bytes())
        schema = json.loads(canonical.read_bytes())
        self.assertEqual(json.dumps(gates.codex_schema(schema)).encode(), (LEGACY / 'codex-schema.json').read_bytes())
        for contract in (schema, json.loads((LEGACY / canonical.name).read_bytes())):
            provider.validate(verdict(), contract)
            with self.assertRaisesRegex(ValueError, '^unexpected result fields$'):
                provider.validate(verdict(['9999']), contract)


class ConvergenceTests(unittest.TestCase):
    def converge(self, value, index):
        text = (SCRIPTS / 'bureau-config.sh').read_text()
        helpers = '\n'.join(re.search(r'^' + name + r'\(\) \{\n.*?^\}', text, re.M | re.S)[0]
                            for name in ('parse_claude_json', 'review_convergence'))
        completed = subprocess.run(['/bin/bash', '-euo', 'pipefail', '-c', helpers + '\nraw=$(cat); review_convergence "$raw" BUG'],
                                   input=json.dumps(value), env={**os.environ, 'BUREAU_REVIEW_ADR_REFS':json.dumps(index)},
                                   capture_output=True, text=True, timeout=10)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        return completed.stdout.strip()

    def test_optional_valid_refs_and_all_malformed_refs(self):
        self.assertEqual(self.converge(verdict(), ['0001']), 'APPROVE')
        self.assertEqual(self.converge(verdict([]), ['0001']), 'APPROVE')
        self.assertEqual(self.converge(verdict(['0001']), ['0001']), 'APPROVE')
        for refs in (['9999'], ['0001', '0002'], '0001', [1], [None], {}, True):
            with self.subTest(refs=refs):
                self.assertEqual(self.converge(verdict(refs), ['0001']), 'BLOCK')
                self.assertEqual(self.converge(verdict(refs), None), 'APPROVE')
        null_refs = verdict(); null_refs['findings'][0]['adr_refs'] = None
        self.assertEqual(self.converge(null_refs, ['0001']), 'BLOCK')
        self.assertEqual(self.converge(verdict(['0001']), []), 'BLOCK')

    def test_unknown_reference_blocks(self):
        self.assertEqual(self.converge(verdict(['9999']), ['0001']), 'BLOCK')


class PipelineTests(unittest.TestCase):
    def setUp(self):
        # Use the existing real-worker fixture, with fake provider/Linear/GitHub.
        self.source_temp = tempfile.TemporaryDirectory(prefix='bureau-adr-source-')
        self.addCleanup(self.source_temp.cleanup)
        source = Path(self.source_temp.name).resolve()
        shutil.copytree(SCRIPTS, source / 'templates/scripts')
        capture = """capture = pathlib.Path(os.environ['ADR_CAPTURE']); capture.mkdir(exist_ok=True)
(capture / (os.environ.get('BUREAU_PROVIDER_PHASE', 'stage') + '.txt')).write_text(prompt)
if '--output-schema' in sys.argv:
    (capture / (os.environ.get('BUREAU_PROVIDER_PHASE', 'stage') + '.schema.json')).write_bytes(pathlib.Path(sys.argv[sys.argv.index('--output-schema')+1]).read_bytes())
"""
        codex = fixture.CODEX.replace('prompt = sys.stdin.read()\n', 'prompt = sys.stdin.read()\n' + capture)
        codex = codex.replace("pathlib.Path(sys.argv[sys.argv.index('-o')+1]).write_text(json.dumps(result))", """if 'Merge these three specialist reviews' in prompt:
    result = json.loads(os.environ['ADR_RESULT'])
else:
    result = {'counts':{'critical':0},'findings':json.loads(os.environ['ADR_RESULT'])['findings']}
pathlib.Path(sys.argv[sys.argv.index('-o')+1]).write_text(json.dumps(result))""")
        self.fixture = fixture.SupervisionPipelineTests('test_fixture_commits_and_pushes_start_no_git_maintenance')
        with patch.object(fixture, 'ROOT', source), patch.object(fixture, 'CODEX', codex):
            self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.repo, self.root, self.git = self.fixture.repo, self.fixture.root, self.fixture.git
        self.config = json.loads((self.repo / '.bureau.json').read_text())
        self.config['repo']['test_command'] = 'true'
        self.workers = 0
        self.git('checkout', '-q', '001-task')
        directory = self.repo / 'docs/adr'; directory.mkdir(parents=True)
        (directory / '0001-choice.md').write_text(adr('0001', 'PINNED_ACCEPTED', decision='PRIVATE_ADR_DECISION_TEXT'))
        (directory / '0002-proposed.md').write_text(adr('0002', 'PROPOSAL_UNDER_REVIEW', 'proposed (v3.5, PR #1)'))
        self.git('add', 'docs'); self.git('commit', '-qm', 'test: pinned ADR records')
        self.git('push', '-q', 'origin', 'HEAD'); self.git('checkout', '-q', 'main')

    def review(self, value=None, code=20, **overrides):
        self.workers += 1
        (self.repo / '.bureau.json').write_text(json.dumps(self.config))
        capture = self.root / ('capture-' + str(self.workers)); capture.mkdir()
        self.capture = capture
        completed = subprocess.run(['bash', 'scripts/bureau-worker.sh', 'T-1', 'code-review-pipeline.sh',
                                    str(self.root / ('worker-' + str(self.workers))), '001-task'],
                                   cwd=self.repo, env={**self.fixture.env, 'BUREAU_NO_MERGE':'1', 'BUREAU_STOP_REQUESTED':'1',
                                                      'ADR_CAPTURE':str(capture), 'ADR_RESULT':json.dumps(value or verdict()), **overrides},
                                   capture_output=True, text=True, timeout=60)
        # Failed reviews intentionally preserve their diagnostic temp directory.
        # Test cleanup owns only direct children of the resolved platform root.
        for name in re.findall(r'Specialist outputs preserved at ([^\n]+)', completed.stderr):
            path = Path(name).resolve()
            if path.parent == Path(tempfile.gettempdir()).resolve():
                self.addCleanup(shutil.rmtree, path, ignore_errors=True)
        self.assertEqual(completed.returncode, code, completed.stdout + completed.stderr)
        return {p.stem:p.read_bytes() for p in capture.glob('*.txt')}

    def test_enabled_valid_reference_prompts_comment_and_metadata_only_evidence(self):
        self.config['agents']['review_adr_context'] = True
        prompts = self.review(verdict(['0001']))
        for phase in ('correctness', 'security', 'performance'):
            prompt = prompts[phase].decode()
            self.assertIn('## ADR context', prompt)
            self.assertIn('PINNED_ACCEPTED', prompt); self.assertIn('PROPOSAL_UNDER_REVIEW', prompt)
            self.assertEqual(len(rows(prompt)), 2)
            self.assertTrue(all(r['authority'] == 'proposal' for r in rows(prompt)))
            self.assertIn('new evidence', prompt)
            self.assertIn('never waive or downgrade findings, including their Known limits', prompt)
        self.assertIn('proposals under review, not authoritative', prompts['verdict'].decode())
        self.assertEqual(len(rows(prompts['verdict'].decode())), 2)
        self.assertTrue(all(r['authority'] == 'proposal' for r in rows(prompts['verdict'].decode())))
        calls = [json.loads(line) for line in self.fixture.gh_log.read_text().splitlines()]
        body = next(args[args.index('--body') + 1] for args in calls if args[:2] == ['pr', 'comment'])
        self.assertIn('`src/check.py:12` — ADR 0001', body)
        self.assertNotIn('PRIVATE_ADR_DECISION_TEXT', body)
        receipts = list((self.repo / '.git/bureau/evidence/v1/runs').glob('*/receipts/*.json'))
        merger = next(json.loads(p.read_text()) for p in receipts if json.loads(p.read_text()).get('phase') == 'verdict')
        self.assertEqual(merger['adr_refs'], ['0001'])
        self.assertEqual(merger['schema_sha256'], hashlib.sha256((SCRIPTS / 'bureau-review-adr.schema.json').read_bytes()).hexdigest())
        for path in (self.repo / '.git/bureau/evidence/v1').rglob('*'):
            if path.is_file():
                self.assertNotIn(b'PRIVATE_ADR_DECISION_TEXT', path.read_bytes(), path)

    def test_enabled_unknown_reference_is_malformed_and_blocks(self):
        self.config['agents']['review_adr_context'] = True
        self.review(verdict(['9999']), code=25)
        items = json.loads(self.fixture.linear.read_text())
        self.assertIn('needs-human', [v['name'] for v in items['T-1']['labels']['nodes']])

    def test_enabling_context_invalidates_reusable_approval(self):
        self.config['agents']['merge'] = True
        self.review()
        self.config['agents']['review_adr_context'] = True
        prompts = self.review(code=0, BUREAU_NO_MERGE='0', BUREAU_STOP_REQUESTED='0')
        self.assertEqual(set(prompts), {'correctness', 'security', 'performance', 'verdict'})
        self.assertIn(b'## ADR context', prompts['correctness'])

    def test_enabled_invalid_directory_stops_before_paid_calls(self):
        self.config['agents']['review_adr_context'] = True
        self.config['repo']['adr_dir'] = '../outside'
        self.assertEqual(self.review(code=24), {})
        self.assertEqual(self.fixture.models(), [])

    def test_enabled_pipeline_uses_pinned_head_after_checkout_changes(self):
        self.config['agents']['review_adr_context'] = True
        real_git = shutil.which('git')
        wrapper = self.root / 'bin/git'
        wrapper.write_text('#!' + sys.executable + '\n' + '''import os, pathlib, sys
if os.environ.get('BUREAU_REVIEW_INPUT_HEAD') and sys.argv[1:] == ['rev-parse', 'HEAD']:
    directory = pathlib.Path('docs/adr')
    if directory.is_dir():
        (directory / '0001-choice.md').write_text('# 0001 LIVE_AFTER_PIN\\n\\n- **Status**: accepted (derived)\\n\\n## Decision\\nLIVE_DECISION\\n')
os.execv(''' + repr(real_git) + ', [' + repr(real_git) + ', *sys.argv[1:]])\n')
        wrapper.chmod(0o755)
        prompts = self.review()
        self.assertIn('LIVE_AFTER_PIN', (self.root / 'worker-1/docs/adr/0001-choice.md').read_text())
        for phase in ('correctness', 'security', 'performance'):
            self.assertIn(b'PINNED_ACCEPTED', prompts[phase]); self.assertNotIn(b'LIVE_AFTER_PIN', prompts[phase])

    def test_disabled_prompts_comments_wire_and_receipt_equal_legacy_fixtures(self):
        self.config['agents']['review_adr_context'] = False
        prompts = self.review(verdict())
        self.assertEqual(set(prompts), {'correctness', 'security', 'performance', 'verdict'})
        def normalize(raw):
            return raw.replace(self.git('rev-parse', 'origin/main').encode(), b'<BASE>').replace(
                self.git('rev-parse', 'origin/001-task').encode(), b'<HEAD>')
        for phase, prompt in prompts.items():
            self.assertEqual(normalize(prompt), (LEGACY / (phase + '.txt')).read_bytes())
        calls = [json.loads(line) for line in self.fixture.gh_log.read_text().splitlines()]
        comment = next(args[args.index('--body') + 1] for args in calls if args[:2] == ['pr', 'comment'])
        self.assertEqual(normalize(comment.encode()), (LEGACY / 'comment.txt').read_bytes())
        self.assertEqual((self.capture / 'verdict.schema.json').read_bytes(), (LEGACY / 'codex-schema.json').read_bytes())
        receipts = list((self.repo / '.git/bureau/evidence/v1/runs').glob('*/receipts/*.json'))
        merger = next(json.loads(p.read_text()) for p in receipts if json.loads(p.read_text()).get('phase') == 'verdict')
        self.assertEqual(merger['schema_sha256'], hashlib.sha256((LEGACY / 'bureau-review.schema.json').read_bytes()).hexdigest())
        self.assertTrue(all(b'## ADR context' not in prompt for prompt in prompts.values()))

    def test_disabled_unsolicited_reference_is_unexpected_at_provider_boundary(self):
        self.config['agents']['review_adr_context'] = False
        self.review(verdict(['9999']), code=22)
        metadata = list((self.repo / 'logs/provider-runs').glob('*/result.json'))
        self.assertTrue(metadata)
        self.assertTrue(any(json.loads(path.read_text()).get('error') == 'unexpected result fields' for path in metadata))


if __name__ == '__main__':
    unittest.main()
