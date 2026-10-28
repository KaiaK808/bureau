"""Run the real judged stages and provider adapter against offline CLI fixtures."""
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import unittest

import git_maintenance

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / 'templates/scripts'
BRANCH = '001-test-branch'
ISSUE = 'TEAM-1'
VALID_SPEC = '''# Fixture
## Governance and security
Decision: not required
Rationale: This change only edits local documentation and introduces no new data, access, logging or retention behavior.
'''
BAD_VALUES = [('missing', None), ('error', 'ERROR'), ('unknown', 'UNKNOWN'),
              ('lowercase', 'fail'), ('number', 7), ('object', {'value': 'PASS'}),
              ('null', None), ('boolean', True), ('schema-invalid', 'valid')]


def function(source, name):
    return re.search(r'^' + re.escape(name) + r'\(\) \{.*?^\}', source, re.M | re.S)[0]


class JudgedStageTests(unittest.TestCase):
    def setUp(self):
        self.git_env = {**os.environ, **git_maintenance.OFF_ENV}
        # Reuse the stage harness's real parser, hold helpers and fake Linear.
        setup = subprocess.run(['bash', '-euc', '''
source "$1/tests/lib/harness.sh"
sandbox_init TEAM-1 001-test-branch
trap - EXIT
printf '%s' "$SANDBOX"
''', 'fixture', str(ROOT)], text=True, capture_output=True, check=True, env=self.git_env)
        self.repo = Path(setup.stdout)
        self.addCleanup(shutil.rmtree, self.repo)
        self.scripts = self.repo / 'scripts'
        self.git(*git_maintenance.OFF_CONFIG)
        self.git('--git-dir=' + str(self.repo / '.fake-origin.git'), *git_maintenance.OFF_CONFIG)
        alternative = os.environ.get('BUREAU_TEST_JUDGED_STAGE_SOURCE')
        if alternative:
            for name in ('spec-review-pipeline.sh', 'ux-pipeline.sh'):
                shutil.copy(Path(alternative) / name, self.scripts / name)
        self.spec = self.repo / 'specs' / BRANCH / 'spec.md'
        self.edit = self.spec.with_name('provider-edit.md')
        self.spec.write_text(VALID_SPEC)
        (self.repo / '.gitignore').write_text('/*\n!/.gitignore\n!/specs/\n')
        self.git('add', '.gitignore', 'specs')
        self.git('commit', '-qm', 'fixture')
        self.git('push', '-q', 'origin', BRANCH)
        self.head = self.git('rev-parse', 'HEAD').strip()
        self.bin = self.repo / 'bin'; self.bin.mkdir()
        real_git = shutil.which('git')
        (self.bin / 'git').write_text('''#!/bin/bash
for arg in "$@"; do
  if [ "$arg" = push ]; then printf '%s\n' "$*" >> "$SANDBOX/pushes"; break; fi
done
exec "''' + real_git + '''" "$@"
''')
        (self.bin / 'git').chmod(0o755)
        # This CLI never forks or starts a server. Every invocation is joined by
        # the real adapter, and subprocess.run also joins each complete stage.
        fake = '#!' + sys.executable + '''
import json, os, pathlib, sys
root = pathlib.Path(os.environ['SANDBOX'])
runner = pathlib.Path(sys.argv[0]).name
if sys.argv[1] in ('auth', 'login'):
    print('{"loggedIn":true}'); sys.exit(0)
if sys.argv[1] == '--help':
    print('--session-id'); sys.exit(0)
schema = None
if '--output-schema' in sys.argv:
    schema = json.loads(pathlib.Path(sys.argv[sys.argv.index('--output-schema')+1]).read_text())
elif '--json-schema' in sys.argv:
    schema = json.loads(sys.argv[sys.argv.index('--json-schema')+1])
(root/'wire.json').write_text(json.dumps(schema))
(root/'prompt.txt').write_text(sys.stdin.read())
(root/'specs/001-test-branch/provider-edit.md').write_text('Preserved provider work')
stop = os.environ.get('JUDGED_WITHDRAWAL')
if stop: (root/'withdrawal').write_text(stop)
value = json.loads((root/'provider-result.json').read_text())
if runner == 'codex':
    if schema and 'advisory' in schema['properties']: value['advisory'] = None
    pathlib.Path(sys.argv[sys.argv.index('-o')+1]).write_text(json.dumps(value))
    print('{"type":"turn.completed","usage":{"input_tokens":1,"output_tokens":1}}')
else:
    print(json.dumps({'structured_output':value, 'result':json.dumps(value), 'usage':{}}))
'''
        for runner in ('claude', 'codex'):
            path = self.bin / runner; path.write_text(fake); path.chmod(0o755)
        production = (SCRIPTS / 'bureau-config.sh').read_text()
        with (self.scripts / 'bureau-config.sh').open('a') as out:
            out.write('\n' + function(production, 'commit_stage_changes') + '\n'
                      + function(production, 'run_stage_for') + '''
bureau_silence_hold() { :; }
bureau_ticket_checkpoint() {
  if [ -f "$SANDBOX/withdrawal" ]; then exit "$(cat "$SANDBOX/withdrawal")"; fi
}
''')
        self.env = {k:v for k,v in self.git_env.items()
                    if not k.startswith(('BUREAU_', 'FAKE_CLAUDE_', 'JUDGED_'))}
        self.env.update(SANDBOX=str(self.repo), BUREAU_STUB_BRANCH=BRANCH,
                        PATH=str(self.bin) + os.pathsep + os.environ['PATH'],
                        BUREAU_CONFIG=str(self.repo / '.bureau.json'),
                        BUREAU_PROVIDER_LOG_DIR=str(self.repo / 'provider-runs'),
                        CODEX_HOME=str(self.repo / 'codex-home'),
                        CLAUDE_CONFIG_DIR=str(self.repo / 'claude-home'))
        for name in ('codex-home', 'claude-home'): (self.repo / name).mkdir()
        spec = importlib.util.spec_from_file_location('judged_gates', SCRIPTS / 'bureau-gates.py')
        self.gates = importlib.util.module_from_spec(spec); spec.loader.exec_module(self.gates)

    def git(self, *args):
        return subprocess.run(['git', '-C', str(self.repo), *args], check=True,
                              capture_output=True, text=True, env=self.git_env).stdout

    def run_stage(self, stage, runner, value, label_rc=0, withdrawal=None):
        self.git('reset', '--hard', self.head)
        self.git('--git-dir=' + str(self.repo / '.fake-origin.git'),
                 'update-ref', 'refs/heads/' + BRANCH, self.head)
        for name in ('pushes', 'wire.json', 'withdrawal'):
            (self.repo / name).unlink(missing_ok=True)
        self.edit.unlink(missing_ok=True)
        (self.repo / '.git/bureau/needs-human-held' / ISSUE).unlink(missing_ok=True)
        (self.repo / 'provider-result.json').write_text(json.dumps(value))
        (self.repo / '.bureau.json').write_text(json.dumps(dict(agents=dict(runner=runner))))
        env = dict(self.env, BUREAU_STUB_ISSUE_STATE='Spec Review' if stage == 'spec-review' else 'Design',
                   BUREAU_STUB_ADD_LABEL_RC=str(label_rc))
        if withdrawal: env['JUDGED_WITHDRAWAL'] = str(withdrawal)
        result = subprocess.run(['bash', str(self.scripts / (stage + '-pipeline.sh')), ISSUE],
                                cwd=self.repo, env=env, capture_output=True, text=True, timeout=30)
        self.calls = (self.repo / 'calls.log').read_text()
        self.moves = [line.split('\t')[-1] for line in self.calls.splitlines()
                      if line.startswith('move_issue\t')]
        self.pushes = (self.repo / 'pushes').read_text().splitlines() if (self.repo / 'pushes').exists() else []
        self.held = (self.repo / '.git/bureau/needs-human-held' / ISSUE).is_file()
        return result

    def value(self, stage, status=None, ui=False):
        return (dict(review_status=status or 'PASS', ui_work_needed=ui, summary='Fixture summary')
                if stage == 'spec-review' else dict(design_status=status or 'COMPLETE', summary='Fixture summary'))

    def bad_value(self, stage, name, status):
        value = self.value(stage)
        field = 'review_status' if stage == 'spec-review' else 'design_status'
        if name == 'missing': value.pop(field)
        elif name == 'schema-invalid': value['summary'] = {'unexpected': 'object'}
        else: value[field] = status
        return value

    def assert_invalid(self, result, label_rc):
        observed = dict(exit=result.returncode, edits=self.edit.read_text() if self.edit.exists() else None,
                        hold=('add_issue_label\t' + ISSUE + '\tneeds-human') in self.calls,
                        local_hold=self.held, moves=self.moves, pushes=self.pushes,
                        success_comment=bool(re.search(r'PASSED|Design phase complete', self.calls)))
        expected = dict(exit=22, edits='Preserved provider work', hold=True,
                        local_hold=label_rc != 0, moves=[], pushes=[], success_comment=False)
        self.assertEqual(expected, observed, result.stdout + result.stderr)

    def test_invalid_results_hold_and_preserve_without_publishing(self):
        for stage in ('spec-review', 'ux'):
            for runner in ('claude', 'codex'):
                for name, status in BAD_VALUES:
                    for label_rc in (0, 1, 27):
                        with self.subTest(stage=stage, runner=runner, status=name, label_rc=label_rc):
                            self.assert_invalid(self.run_stage(stage, runner,
                                                self.bad_value(stage, name, status), label_rc), label_rc)

    def test_shell_routing_rejects_invalid_status_even_if_adapter_is_bypassed(self):
        # Independent routing defense: keep the production JSON parser while a
        # deliberately broken adapter returns bad data with success.
        with (self.scripts / 'bureau-config.sh').open('a') as out:
            out.write('''
run_stage_for() {
  printf 'Preserved provider work' > "$SANDBOX/specs/001-test-branch/provider-edit.md"
  cat "$SANDBOX/provider-result.json"
}
''')
        for stage in ('spec-review', 'ux'):
            for name, status in BAD_VALUES:
                if name == 'schema-invalid': continue  # full schema validation belongs to the adapter
                with self.subTest(stage=stage, status=name):
                    self.assert_invalid(self.run_stage(stage, 'claude', self.bad_value(stage, name, status)), 0)

    def test_valid_status_routes(self):
        cases = [('spec-review', 'PASS', False, ['state-build']),
                 ('spec-review', 'PASS', True, ['state-design']),
                 ('spec-review', 'FAIL', False, ['state-spec']),
                 ('ux', 'COMPLETE', False, ['state-build']),
                 ('ux', 'INCOMPLETE', False, ['state-spec-review']),
                 ('ux', 'NEEDS_HUMAN', False, [])]
        for runner in ('claude', 'codex'):
            for stage, status, ui, moves in cases:
                with self.subTest(stage=stage, runner=runner, status=status, ui=ui):
                    result = self.run_stage(stage, runner, self.value(stage, status, ui))
                    self.assertEqual(0, result.returncode, result.stdout + result.stderr)
                    self.assertEqual(moves, self.moves)
                    self.assertEqual(1, len(self.pushes))
                    self.assertEqual(status == 'NEEDS_HUMAN', 'needs-human' in self.calls)
                    self.assertTrue(self.edit.exists())

    def test_required_fields_and_types(self):
        for stage in ('spec-review', 'ux'):
            for runner in ('claude', 'codex'):
                for field in self.value(stage):
                    for change in ('missing', 'wrong-type'):
                        with self.subTest(stage=stage, runner=runner, field=field, change=change):
                            value = self.value(stage)
                            if change == 'missing': value.pop(field)
                            else: value[field] = 'false' if field == 'ui_work_needed' else []
                            self.assert_invalid(self.run_stage(stage, runner, value), 0)

    def test_governance_still_forces_fail(self):
        self.spec.write_text('# Missing governance\n')
        self.git('add', 'specs'); self.git('commit', '-qm', 'inadequate governance')
        self.git('push', '-q', 'origin', BRANCH)
        self.head = self.git('rev-parse', 'HEAD').strip()
        for runner in ('claude', 'codex'):
            with self.subTest(runner=runner):
                result = self.run_stage('spec-review', runner, self.value('spec-review'))
                self.assertEqual(0, result.returncode, result.stdout + result.stderr)
                self.assertEqual(['state-spec'], self.moves)
                self.assertIn('Spec review: **FAIL**', self.calls)
                self.assertNotIn('PASSED', self.calls)
                self.assertEqual(1, len(self.pushes))
                self.assert_invalid(self.run_stage('spec-review', runner,
                                    self.bad_value('spec-review', 'error', 'ERROR')), 0)

    def test_withdrawal_precedes_invalid_results(self):
        for stage in ('spec-review', 'ux'):
            for runner in ('claude', 'codex'):
                for code in (25, 26):
                    with self.subTest(stage=stage, runner=runner, code=code):
                        result = self.run_stage(stage, runner, self.bad_value(stage, 'error', 'ERROR'), withdrawal=code)
                        self.assertEqual(code, result.returncode, result.stdout + result.stderr)
                        self.assertTrue(self.edit.exists())
                        self.assertEqual([], self.moves)
                        self.assertEqual([], self.pushes)
                        self.assertNotIn('needs-human', self.calls)

    def test_provider_wire_projection_and_optional_nulls(self):
        for stage in ('spec-review', 'ux'):
            schema = self.scripts / ('bureau-' + stage + '.schema.json')
            canonical = json.loads(schema.read_text())
            canonical['properties']['advisory'] = dict(type='array', maxItems=3, items=dict(type='string'))
            schema.write_text(json.dumps(canonical))
            for runner in ('claude', 'codex'):
                with self.subTest(stage=stage, runner=runner):
                    result = self.run_stage(stage, runner, self.value(stage))
                    self.assertEqual(0, result.returncode, result.stdout + result.stderr)
                    wire = json.loads((self.repo / 'wire.json').read_text())
                    self.assertEqual(self.gates.codex_schema(canonical) if runner == 'codex' else canonical, wire)
                    self.assertEqual(['state-build'], self.moves)


if __name__ == '__main__': unittest.main()
