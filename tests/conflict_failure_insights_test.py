"""Terminal resolver causes cross both real worker hooks, without changing routing."""
import json
import os
from pathlib import Path
import shlex
import unittest
from unittest.mock import patch

import model_conflict_pipeline_test as conflict

SCRIPTS = Path(os.environ.get('BUREAU_TEST_INSIGHTS_SCRIPTS', conflict.SCRIPTS))


class ConflictFailureInsightsTests(unittest.TestCase):
    def setUp(self):
        self.fixture = conflict.ConflictPipelineTests()
        with patch.object(conflict, 'SCRIPTS', SCRIPTS): self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.repo, self.root = self.fixture.repo, self.fixture.root
        self.config = json.loads((self.repo / '.bureau.json').read_text())
        self.config['agents']['merge'] = True
        self.provider_pids = self.root / 'resolver-pids'
        self.fixture.env['RESOLVER_PIDS'] = str(self.provider_pids)
        binary = self.root / 'bin/codex'
        source = binary.read_text()
        needle = "if 'Resolve only these remaining source conflicts:' in prompt:\n"
        self.assertEqual(source.count(needle), 1)
        binary.write_text(source.replace(needle, needle + '''    with open(os.environ['RESOLVER_PIDS'], 'a') as out: out.write(str(os.getpid())+'\\n')
    mode = os.environ.get('RESOLVER_FAILURE', '')
    if mode in ('wall-clock', 'silence'):
        import time
        time.sleep(10)
    if mode == 'provider': sys.exit(3)
    if mode == 'unresolved':
        pathlib.Path(sys.argv[sys.argv.index('-o')+1]).write_text('Claimed resolved.')
        print(json.dumps({'type':'turn.completed','usage':{'input_tokens':1,'output_tokens':1}}))
        sys.exit(0)
'''))
        self.addCleanup(self.assert_providers_stopped)

    def assert_providers_stopped(self):
        if self.provider_pids.exists():
            for raw in self.provider_pids.read_text().splitlines():
                with self.assertRaises(ProcessLookupError, msg='resolver survived: ' + raw): os.kill(int(raw), 0)

    def run_hook(self, hook, number=1):
        (self.repo / '.bureau.json').write_text(json.dumps(self.config))
        if hook == 'rebase': self.fixture.prepare_rebase()
        self.fixture.worker = self.root / ('worker-' + str(number))
        stage = 'rebase-pipeline.sh' if hook == 'rebase' else 'code-review-pipeline.sh'
        result = self.fixture.run_worker(stage)
        path = self.repo / '.git/bureau/evidence/v1/stage-outcomes.jsonl'
        self.assertTrue(path.exists(), result.stdout + result.stderr)
        name = 'rebase' if hook == 'rebase' else 'code_review'
        rows = [json.loads(line) for line in path.read_text().splitlines()]
        row = [row for row in rows if row['stage'] == name][-1]
        self.assertEqual(row['normalized_exit'], result.returncode, result.stdout + result.stderr)
        self.assert_providers_stopped()
        return result, row

    def release_hold(self):
        items = json.loads(self.fixture.f.linear.read_text())
        items['T-1']['state'] = {'id':'build_review', 'name':'Build Review'}
        items['T-1']['labels']['nodes'] = [{'name':'lane-2'}, {'name':'ai-implementable'}]
        self.fixture.f.linear.write_text(json.dumps(items))
        (self.repo / '.git/bureau/needs-human-held/T-1').unlink(missing_ok=True)

    def failure_case(self, hook, mode, cause):
        resolver = self.config['agents']['conflict_resolver']
        if mode == 'disabled': resolver['enabled'] = False
        elif mode == 'missing-check': self.config['repo']['test_command'] = '# no external check'
        elif mode == 'red-check': self.config['repo']['test_command'] = 'exit 1'
        elif mode == 'wall-clock': resolver['timeout_seconds'] = .4
        elif mode == 'silence':
            resolver.update(silence_timeout_seconds=.3, silence_child_timeout_seconds=.3)
        self.fixture.env['RESOLVER_FAILURE'] = 'unresolved' if mode == 'exhausted' else mode
        if mode == 'exhausted':
            first, _ = self.run_hook(hook)
            self.assertEqual(first.returncode, 17, first.stdout + first.stderr)
            before = self.provider_pids.read_text()
            self.release_hold()
        result, row = self.run_hook(hook, 2)
        expected_exit = 25 if mode == 'silence' else 17
        self.assertEqual(result.returncode, expected_exit, result.stdout + result.stderr)
        self.assertEqual(self.fixture.git('ls-remote', 'origin', 'refs/heads/001-task').split()[0], self.fixture.old_head)
        self.assertEqual(self.fixture.git('-C', str(self.fixture.worker), 'rev-parse', 'HEAD'), self.fixture.old_head)
        self.assertEqual(self.fixture.git('-C', str(self.fixture.worker), 'status', '--porcelain'), '')
        self.assertEqual(self.fixture.git('-C', str(self.fixture.worker), 'ls-files', '-u'), '')
        labels = json.loads(self.fixture.f.linear.read_text())['T-1']['labels']['nodes']
        self.assertIn('needs-human', [label['name'] for label in labels])
        if mode == 'exhausted': self.assertEqual(self.provider_pids.read_text(), before)
        self.assertEqual(row['cause_class'], cause)
        if cause == 'timeout': self.assertEqual(row['timeout_kind'], mode)

    def successful_repair_case(self, hook):
        # First prove the failure signal is live; then allow another repair in
        # a fresh stage and require its later failure to keep its own cause.
        self.config['agents']['conflict_resolver']['max_attempts'] = 2
        self.fixture.env['RESOLVER_FAILURE'] = 'unresolved'
        first, failed = self.run_hook(hook)
        self.assertEqual(first.returncode, 17, first.stdout + first.stderr)
        self.release_hold()
        self.fixture.env['RESOLVER_FAILURE'] = ''
        if hook == 'rebase':
            later = self.fixture.git('commit-tree', self.fixture.old_head + '^{tree}', '-p', self.fixture.old_head, '-m', 'Concurrent remote human')
            self.fixture.git('push', '-q', 'origin', later + ':refs/heads/remote-writer')
            self.fixture.env['CONFLICT_REMOTE_ADVANCE'] = later
            expected = (19, 'other')
        else:
            checked = shlex.quote(str(self.root / 'external-check-passed'))
            self.config['repo']['test_command'] = 'if [ -f ' + checked + ' ]; then exit 1; fi; touch ' + checked
            expected = (0, 'gate_red')
        result, later_row = self.run_hook(hook, 2)
        self.assertEqual(result.returncode, expected[0], result.stdout + result.stderr)
        if hook == 'rebase':
            self.assertIn('Rebase succeeded', result.stdout)
            self.assertEqual(self.fixture.git('ls-remote', 'origin', 'refs/heads/001-task').split()[0], later)
        else:
            self.assertEqual(json.loads(self.fixture.f.linear.read_text())['T-1']['state']['id'], 'build')
            self.assertEqual(len(list(self.fixture.probe.glob('*.json'))), 3, 'fresh specialist review did not run')
            self.assertNotEqual(self.fixture.git('rev-parse', 'origin/001-task'), self.fixture.old_head)
        self.assertEqual((failed['cause_class'], later_row['cause_class']), ('human_hold', expected[1]))


def failure_test(hook, mode, cause):
    def test(self): self.failure_case(hook, mode, cause)
    return test


def successful_repair_test(hook):
    def test(self): self.successful_repair_case(hook)
    return test


for hook in ('rebase', 'merge'):
    for mode, cause in (('disabled', 'human_hold'), ('unresolved', 'human_hold'), ('exhausted', 'human_hold'),
                        ('red-check', 'gate_red'), ('missing-check', 'gate_red'), ('provider', 'provider_error'),
                        ('wall-clock', 'timeout'), ('silence', 'timeout')):
        setattr(ConflictFailureInsightsTests, 'test_' + hook + '_' + mode.replace('-', '_'), failure_test(hook, mode, cause))
    setattr(ConflictFailureInsightsTests, 'test_' + hook + '_successful_repair_later_failure', successful_repair_test(hook))


if __name__ == '__main__': unittest.main()
