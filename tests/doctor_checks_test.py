"""Doctor checks added in v3.1 and the provider's default stage timeout, run against real
temporary Git repositories: the CI gate without pull-request workflows, merge gate switches
and agents.implement.push_each_iteration that are not booleans, short provider timeouts,
repo.worktree_links judged in the main checkout from a linked worktree, and .env* links; since
v3.2 also linked directories that hold a .env* name or cannot be searched, the
repo.test_command warning only where implement runs on Codex, and that doctor reads the .env file
the nine stages read (BUREAU_ENV_FILE only, never a .env of the checkout it runs in)."""
import copy
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import git_maintenance

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('doctor', ROOT / 'templates/scripts/bureau-doctor.py')
d = importlib.util.module_from_spec(spec); spec.loader.exec_module(d)
PROVIDER = ROOT / 'templates/scripts/bureau-provider.py'
REAL_RUN = subprocess.run
INSTALLER = ROOT / 'scripts/bureau_install.py'
# No detached git maintenance may still write into a test repository while its temp dir is deleted (git_maintenance.py).
GIT_ENV = {**os.environ, 'GIT_CONFIG_GLOBAL': os.devnull, 'GIT_CONFIG_NOSYSTEM': '1', **git_maintenance.OFF_ENV,
           'GIT_AUTHOR_NAME': 'Bureau Test', 'GIT_AUTHOR_EMAIL': 'test@example.invalid',
           'GIT_COMMITTER_NAME': 'Bureau Test', 'GIT_COMMITTER_EMAIL': 'test@example.invalid'}
BASE = {'version': 2,
        'linear': {'teams': [{'id': 'team-id', 'key': 'TEAM', 'states': {'build': 'state-id', 'merge': 'merge-id'}}]},
        'agents': {'runner': 'claude', 'spec': True, 'implement': True, 'code_review': True},
        'repo': {'test_command': 'python3 test.py'}}
LONG = ('spec', 'spec_review', 'ux', 'qa', 'code_review')


def git(cwd, *args, check=True):
    return subprocess.run(['git', '-C', str(cwd), *args], check=check, capture_output=True, text=True, env=GIT_ENV)


class Repo(unittest.TestCase):
    """A repository with Bureau's interfaces and scripts installed and committed."""
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='bureau doctor checks '); self.addCleanup(temp.cleanup)
        self.base = Path(temp.name).resolve()
        self.repo = self.base / 'main checkout'
        git(self.base, 'init', '-q', str(self.repo))
        self.write_config(BASE)
        subprocess.run([sys.executable, str(INSTALLER), 'assets', '--repo', str(self.repo), '--target', 'codex',
                        '--scope', 'interfaces', '--scope', 'scripts', '--apply'], check=True, stdout=subprocess.DEVNULL)
        # .bureau.json stays untracked, so a linked worktree has none and doctor there finds the main checkout's.
        with (self.repo / '.git/info/exclude').open('a') as out: out.write('.bureau.json\n')
        git(self.repo, 'add', '-A'); git(self.repo, 'commit', '-qm', 'install')
        # Doctor finds .bureau.json itself; an operator's BUREAU_CONFIG or timeout must not leak in.
        env = patch.dict(os.environ, {}); env.start(); self.addCleanup(env.stop)
        for name in ('BUREAU_CONFIG', 'BUREAU_STAGE_TIMEOUT'): os.environ.pop(name, None)

    def write_config(self, config):
        (self.repo / '.bureau.json').write_text(json.dumps(config))

    def config(self, **agents):
        config = copy.deepcopy(BASE); config['agents'].update(agents); return config

    def diagnose(self, config=None, repo=None):
        if config is not None: self.write_config(config)
        return d.diagnose(repo or self.repo, 'app')

    def workflow(self, name, text):
        path = self.repo / '.github/workflows' / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(text if isinstance(text, bytes) else text.encode())
        return path


class GitMaintenanceTests(Repo):
    def test_commits_through_the_git_helper_start_no_git_maintenance(self):
        trace = self.base / 'trace2.json'
        (self.repo / 'traced.txt').write_text('traced\n')
        with patch.dict(GIT_ENV, {'GIT_TRACE2_EVENT': str(trace)}):
            git(self.repo, 'add', 'traced.txt'); git(self.repo, 'commit', '-qm', 'traced')
        self.assertTrue(any('commit' in argv for argv in git_maintenance.commands(trace)))  # the commit was traced
        self.assertEqual(git_maintenance.maintenance_started(trace), [])


class StageTimeoutTests(Repo):
    def describe(self, stage, config, **env):
        path = self.base / 'describe.json'; path.write_text(json.dumps(config))
        proc = subprocess.run([sys.executable, str(PROVIDER), '--stage', stage, '--config', str(path), '--describe'],
                              capture_output=True, text=True, env={**os.environ, **env})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return json.loads(proc.stdout)['timeout']

    def test_default_is_one_hour_per_call_and_every_override_still_wins(self):
        for stage in LONG + ('implement', 'copy', 'research'):
            with self.subTest(stage=stage):
                self.assertEqual(self.describe(stage, BASE), 3600.0)
                for runner in ('claude', 'codex'):
                    config = self.config(runner=runner, providers={runner: {'timeout_seconds': 1200}})
                    self.assertEqual(self.describe(stage, config), 1200.0)
                    config['agents'][stage] = {'enabled': True, 'timeout_seconds': 5400}
                    self.assertEqual(self.describe(stage, config), 5400.0)
                    self.assertEqual(self.describe(stage, config, BUREAU_STAGE_TIMEOUT='600'), 600.0)

    def warning(self, result):
        found = [w for w in result['warnings'] if w.startswith('Provider timeout below')]
        self.assertLessEqual(len(found), 1, found)
        return found[0] if found else None

    def test_doctor_warns_for_enabled_long_stages_below_1800_seconds(self):
        stages = {stage: True for stage in LONG}
        result = self.diagnose(self.config(**stages))
        self.assertTrue(result['ok'], result); self.assertIsNone(self.warning(result))
        self.assertEqual({s: result['effective_stages'][s]['timeout'] for s in LONG}, dict.fromkeys(LONG, 3600.0))
        # A provider default of 900 s, as installations set it before v3.1, names every long stage.
        config = self.config(**stages, providers={'claude': {'timeout_seconds': 900}})
        text = self.warning(self.diagnose(config))
        self.assertIsNotNone(text)
        for stage in LONG: self.assertIn(stage + ' 900 s', text)
        self.assertNotIn('implement', text); self.assertNotIn('BUREAU_STAGE_TIMEOUT', text)
        # The boundary: 1800 s is enough, 1799 s is not; implement brings its own limits.
        config['agents'].update(spec={'enabled': True, 'timeout_seconds': 1800}, qa={'enabled': True, 'timeout_seconds': 1799},
                                implement={'enabled': True, 'timeout_seconds': 60})
        text = self.warning(self.diagnose(config))
        self.assertNotIn('spec 1800', text); self.assertIn('qa 1799 s', text); self.assertIn('code_review 900 s', text)
        self.assertNotIn('implement', text)
        # A disabled stage is not judged.
        config = self.config(ux={'enabled': False, 'timeout_seconds': 60}, code_review={'enabled': True, 'timeout_seconds': 3600})
        self.assertIsNone(self.warning(self.diagnose(config)))
        # The process environment wins over the configuration, and the warning says so.
        os.environ['BUREAU_STAGE_TIMEOUT'] = '600'
        text = self.warning(self.diagnose(self.config()))
        self.assertIn('spec 600 s', text); self.assertIn('code_review 600 s', text); self.assertIn('BUREAU_STAGE_TIMEOUT', text)


class CiGateTests(Repo):
    GATE = 'agents.merge_require_green_ci: automatic merges need'

    def gate(self, result):
        return [w for w in result['warnings'] if w.startswith(self.GATE)]

    def assertGateWarning(self, config, expected, contains=None):
        result = self.diagnose(config)
        self.assertTrue(result['ok'], result)
        found = self.gate(result)
        self.assertEqual(len(found), 1 if expected else 0, found)
        if contains: self.assertIn(contains, found[0])
        return result

    def test_warns_when_no_workflow_runs_on_pull_requests(self):
        self.assertGateWarning(self.config(), True, 'the repository has no workflow in .github/workflows')
        self.workflow('deploy.yml', 'name: deploy\non:\n  push:\n    branches: [main]\njobs:\n  x:\n    if: github.event.pull_request.number\n    runs-on: ubuntu-latest\n')
        self.workflow('notes.txt', 'on: pull_request\n')  # not a workflow file
        found = 'no workflow in .github/workflows runs on pull requests or on pushes to every branch'
        result = self.assertGateWarning(self.config(), True, found + '; push is limited by branches or to tags in deploy.yml (checked: deploy.yml)')
        self.assertIn('Unless those filters take in the pull request\'s branch', self.gate(result)[0])
        (self.repo / '.github/workflows/deploy.yml').unlink()
        self.workflow('nightly.yaml', "# on: pull_request (disabled)\n'on':\n  schedule:\n    - cron: '0 3 * * *'\n  # pull_request:\n")
        self.workflow('release.yml', 'on:\n  push:\n    tags: [v*]\n')
        result = self.assertGateWarning(self.config(), True, found + '; push is limited by branches or to tags in release.yml (checked: nightly.yaml, release.yml)')
        (self.repo / '.github/workflows/release.yml').unlink()
        result = self.assertGateWarning(self.config(), True, found + ' (checked: nightly.yaml)')
        self.assertIn('Unless another CI reports checks or statuses to GitHub for the pull request\'s head commit', self.gate(result)[0])

    def test_any_pull_request_trigger_satisfies_the_check(self):
        for text in ('on: pull_request\n', 'on: [push, pull_request]\n', 'on:\n  pull_request:\n    types: [opened]\n',
                     '"on":\n  - push\n  - pull_request\n', "'on': [pull_request]\n", 'on:\n  pull_request_target:\n', 'on: {pull_request: {}}\n',
                     'name: ci\non:  # triggers\n  push:\n  pull_request:\njobs: {}\n',
                     'on:\n- push\n- pull_request\njobs:\n  test:\n    runs-on: ubuntu-latest\n',
                     b'\xef\xbb\xbfon: pull_request\n', b'\xef\xbb\xbfname: ci\non:\n  pull_request:\n',
                     # flow collections over several lines, quoted keys and quoted list items
                     'on: [\n  push,\n  pull_request\n]\njobs: {}\n', 'on: [\npush,\npull_request\n]\n',
                     'on: {\n  pull_request: {}\n}\njobs: {}\n', 'on:\n  "pull_request":\n',
                     "on:\n  'pull_request':\n    types: [opened]\n", "on:\n  - 'pull_request'\n", 'on:\n- "pull_request"\n'):
            with self.subTest(text=text):
                path = self.workflow('ci.yaml', text)
                self.assertGateWarning(self.config(), False)
                path.unlink()
        # The workflow the installer scaffolds.
        subprocess.run([sys.executable, str(INSTALLER), 'assets', '--repo', str(self.repo), '--scope', 'ci', '--apply'],
                       check=True, stdout=subprocess.DEVNULL)
        self.assertGateWarning(self.config(), False)

    def test_a_push_trigger_for_every_branch_satisfies_the_check(self):
        # The gate counts check runs on the head commit, whatever event started them, and a push
        # to the pull request's branch starts a push workflow unless branches or tags limit it.
        for text in ('on: push\n', 'on: [push]\n', 'on:\n  push:\n', 'on:\n  push:\n    branches-ignore: [main]\n',
                     'on:\n  push:\n    paths: [src/**]\n', 'on:\n  push:\n    tags: [v*]\n    branches-ignore: [gh-pages]\n',
                     'on:\n- push\njobs: {}\n', b'\xef\xbb\xbfon: push\n',
                     'on:\n  workflow_dispatch:\n  push:\n  schedule:\n    - cron: x\n',
                     'on: [\n  push\n]\n', 'on: {\n  push: {}\n}\n', 'on:\n  "push":\n', "on:\n  'push':\n", "on:\n  - 'push'\n", 'on:\n- "push"\n'):
            with self.subTest(text=text):
                path = self.workflow('ci.yml', text)
                self.assertGateWarning(self.config(), False)
                path.unlink()
        for text in ('on:\n  push:\n    branches: [main]\n', 'on:\n  push:\n    branches:\n      - main\n  workflow_dispatch:\n',
                     'on:\n  push:\n    tags:\n      - v*\n', 'on: {push: {branches: [main]}}\n',
                     'on:\n  workflow_run:\n    workflows: [push]\n',
                     'on: {\n  push: {branches: [main]}\n}\n', "on:\n  'push':\n    branches: [main]\n",
                     # a job named push after the on: block is no trigger
                     'on:\n  schedule:\n    - cron: x\njobs:\n  push:\n    runs-on: ubuntu-latest\n'):
            with self.subTest(text=text):
                path = self.workflow('ci.yml', text)
                self.assertGateWarning(self.config(), True)
                path.unlink()

    def test_only_a_gate_that_needs_checks_and_merges_automatically_is_judged(self):
        # (agents settings, warning expected)
        for agents, expected in (({}, True),
                                 ({'merge_require_green_ci': False}, False),
                                 ({'merge_require_green_ci': True}, True),
                                 ({'merge_require_green_ci': None}, True),
                                 ({'merge_min_required_checks': 0}, False),
                                 ({'merge_min_required_checks': 2}, True),
                                 ({'merge_min_required_checks': False}, True),
                                 ({'merge_mode': 'manual'}, False),
                                 ({'merge_mode': 'Auto'}, False),
                                 ({'code_review': False}, False),
                                 ({'code_review': False, 'merge': True}, True),
                                 ({'code_review': 'false', 'merge': {'enabled': False}}, False),
                                 ({'merge_require_up_to_date': False}, True)):
            with self.subTest(agents=agents):
                result = self.assertGateWarning(self.config(**agents), expected)
                if agents.get('merge_min_required_checks') == 2: self.assertIn('at least 2 completed', self.gate(result)[0])

    def test_the_grace_is_read_by_the_gate_rule(self):
        for value, used, warned in (('600', 600, True), (-5, 1800, True), (900, 900, False), (None, 1800, False)):
            with self.subTest(value=value):
                result = self.diagnose(self.config(merge_ci_start_grace_seconds=value))
                found = [w for w in result['warnings'] if w.startswith('agents.merge_ci_start_grace_seconds ')]
                self.assertEqual(found, ['agents.merge_ci_start_grace_seconds ' + json.dumps(value)
                                         + ' should be a whole number of at least 0; the merge gate uses %d' % used] if warned else [])

    def test_the_queue_grace_and_the_review_recheck_are_read_by_the_gate_rule(self):
        # v3.2: agents.merge_ci_queued_grace_seconds (pr_ci_is_green) and
        # agents.merge_gate_recheck_seconds (review_gate_waits) follow the same rule, default 3600.
        for key in ('merge_ci_queued_grace_seconds', 'merge_gate_recheck_seconds'):
            for value, used, warned in (('600', 600, True), (-5, 3600, True), ('abc', 3600, True), (1.5, 2, True),
                                        (True, 3600, True), (0, 0, False), (900, 900, False), (None, 3600, False)):
                with self.subTest(key=key, value=value):
                    result = self.diagnose(self.config(**{key: value}))
                    found = [w for w in result['warnings'] if w.startswith('agents.' + key + ' ')]
                    self.assertEqual(found, ['agents.' + key + ' ' + json.dumps(value)
                                             + ' should be a whole number of at least 0; the merge gate uses %d' % used] if warned else [])

    def test_the_minimum_is_read_by_the_gate_rule(self):
        # The merge gate's own reading (gate_number, _merge_gate_number in bureau-config.sh):
        # "2" needs 2, -1 and "abc" need the default 1, 1.5 needs 2; each warns once.
        for value, needed in (('2', 2), (-1, 1), ('abc', 1), (1.5, 2)):
            with self.subTest(value=value):
                result = self.assertGateWarning(self.config(merge_min_required_checks=value), True)
                self.assertIn('at least %d completed' % needed, self.gate(result)[0])
                found = [w for w in result['warnings'] if w.startswith('agents.merge_min_required_checks ')]
                self.assertEqual(found, ['agents.merge_min_required_checks ' + json.dumps(value)
                                         + ' should be a whole number of at least 0; the merge gate uses %d' % needed])

    def test_gate_switches_that_are_not_booleans_count_as_required(self):
        for key in ('merge_require_green_ci', 'merge_require_up_to_date'):
            for value in ('false', 0, 'no', [], {}):
                with self.subTest(key=key, value=value):
                    result = self.diagnose(self.config(**{key: value}))
                    found = [w for w in result['warnings'] if w.startswith('agents.' + key + ' ')]
                    self.assertEqual(len(found), 1, result['warnings'])
                    self.assertIn('is not a JSON boolean', found[0]); self.assertIn(json.dumps(value), found[0])
                    # A green-CI switch that is not false still needs checks.
                    self.assertEqual(len(self.gate(result)), 1, result['warnings'])
            for value in (True, False, None):
                result = self.diagnose(self.config(**{key: value}))
                self.assertFalse([w for w in result['warnings'] if 'is not a JSON boolean' in w], result['warnings'])


class PushEachIterationTests(Repo):
    def test_only_a_boolean_or_null_is_accepted(self):
        key = 'agents.implement.push_each_iteration'
        for implement, warned in ((True, False), ('true', False), ({'enabled': True}, False),
                                  ({'push_each_iteration': False}, False), ({'push_each_iteration': True}, False),
                                  ({'push_each_iteration': None}, False), ({'push_each_iteration': 'false'}, True),
                                  ({'push_each_iteration': 0}, True), ({'enabled': True, 'push_each_iteration': []}, True)):
            with self.subTest(implement=implement):
                result = self.diagnose(self.config(implement=implement))
                found = [w for w in result['warnings'] if w.startswith(key)]
                self.assertEqual(len(found), int(warned), result['warnings'])
                if warned: self.assertIn('pushes after every iteration', found[0])
                self.assertTrue(result['ok'], result)


class TestCommandWarningTests(Repo):
    """repo.test_command is required only where implement runs on Codex: its completion runs the
    command as an independent check and stops with 24 without one (implement-pipeline.sh). v3.1
    warned on every installation. Each case first runs the REAL lines of implement-pipeline.sh that
    decide it, in a bash that sourced the real bureau-config.sh: the .env loading at the top and the
    Codex completion gate (resolve_runner_for_stage implement, then `exit 24` on an empty
    repo.test_command). Doctor must warn exactly where that gate stops the stage. The stage runs as it
    does under the worker: in a linked worktree without a .env, with BUREAU_CONFIG naming the main
    checkout's .bureau.json, as the tick exports it. LOAD and GATE are cut from implement-pipeline.sh
    and must equal LOAD_TEXT and GATE_TEXT exactly: a change to those lines fails every test here
    until doctor's reading (stage_env_file, stage_env_value, implement_runner in bureau-doctor.py) is
    checked against it and the copies are updated (a deliberate tripwire). v3.2 (S1b): the stage reads
    BUREAU_ENV_FILE only, never ./.env."""
    WARNING = 'repo.test_command is missing; required for Codex background implementation'
    CONFIG_SH = ROOT / 'templates/scripts/bureau-config.sh'
    IMPLEMENT = (ROOT / 'templates/scripts/implement-pipeline.sh').read_text().splitlines()
    LOAD_FIRST = 'BUREAU_ENV_FILE="${BUREAU_ENV_FILE:-$SCRIPT_REPO/.env}"'
    LOAD_TEXT = '\n'.join((
        LOAD_FIRST,
        '# BUREAU_ENV_FILE only, never ./.env: in a stage worktree that is a file the branch controls.',
        'if [ -f "$BUREAU_ENV_FILE" ]; then bureau_load_env --export "$BUREAU_ENV_FILE"',
        'else bureau_secret_set LINEAR_API_KEY || { echo "ERROR: Set LINEAR_API_KEY"; exit 1; }; fi'))
    GATE_FIRST = 'if [ "$STATUS" = "COMPLETE" ] && [ "$(resolve_runner_for_stage implement)" = codex ]; then'
    GATE_TEXT = '\n'.join((
        GATE_FIRST,
        "  TEST_COMMAND=$(bureau_get '.repo.test_command // empty')",
        "  [ -n \"$TEST_COMMAND\" ] || { echo 'Codex completion needs repo.test_command for independent verification.' >&2; exit 24; }",
        'fi'))
    _load = IMPLEMENT.index(LOAD_FIRST) if LOAD_FIRST in IMPLEMENT else None
    LOAD = None if _load is None else '\n'.join(IMPLEMENT[_load:_load + LOAD_TEXT.count('\n') + 1])
    _gate = IMPLEMENT.index(GATE_FIRST) if GATE_FIRST in IMPLEMENT else None
    GATE = None if _gate is None else '\n'.join(IMPLEMENT[_gate:_gate + GATE_TEXT.count('\n')] + ['fi'])
    STAGE = ('cd "$WORKTREE" && [ ! -e .env ] || exit 97\n'
             'source "$CONFIG_SH" >/dev/null 2>&1 || exit 99; SCRIPT_REPO="$REPO"; eval "$LOAD" >/dev/null 2>&1 || exit 98\n'
             'runner=$(resolve_runner_for_stage implement 2>/dev/null) || runner=unresolved\n'
             'STATUS=COMPLETE; rc=0; ( eval "$GATE" ) >/dev/null 2>&1 || rc=$?; printf "%s %s" "$runner" "$rc"')

    def setUp(self):
        super().setUp()
        drifted = 'implement-pipeline.sh changed the lines this test copies; check bureau-doctor.py against them, then update '
        self.assertEqual(self.LOAD, self.LOAD_TEXT, drifted + 'LOAD_TEXT')
        self.assertEqual(self.GATE, self.GATE_TEXT, drifted + 'GATE_TEXT')
        for name in [name for name in os.environ if name.startswith('BUREAU_RUNNER_')] + ['BUREAU_ENV_FILE']: os.environ.pop(name, None)
        with (self.repo / '.git/info/exclude').open('a') as out: out.write('.env\n')
        self.worktree = self.base / 'implement worktree'
        git(self.repo, 'worktree', 'add', '-q', '--detach', str(self.worktree))

    def stage(self, env):
        """(runner implement resolves, whether the Codex completion gate stops the stage with 24)."""
        proc = subprocess.run(['/bin/bash', '-c', self.STAGE], capture_output=True, text=True,
                              env={**os.environ, 'REPO': str(self.repo), 'WORKTREE': str(self.worktree), 'CONFIG_SH': str(self.CONFIG_SH), 'LOAD': self.LOAD, 'GATE': self.GATE,
                                   'BUREAU_CONFIG': str(self.repo / '.bureau.json'), 'LINEAR_API_KEY': 'lin_test', **env})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        runner, rc = proc.stdout.split()
        self.assertIn(rc, ('0', '24'), proc.stdout)
        return runner, rc == '24'

    def check(self, agents, env=None, test_command=None, env_file=None, at=None):
        """Doctor warns exactly where the stage's gate stops it. Returns the stage's runner."""
        config = copy.deepcopy(BASE); config['agents'] = {'spec': True, 'code_review': True, **agents}
        if test_command is None: del config['repo']['test_command']
        else: config['repo']['test_command'] = test_command
        self.write_config(config)
        target = at or (self.repo / '.env')
        if env_file is None: target.unlink(missing_ok=True)
        else: target.write_bytes(env_file.encode())
        runner, halts = self.stage(env or {})
        with patch.dict(os.environ, env or {}):
            result = self.diagnose(config)
        self.assertTrue(result['ok'], result)
        self.assertEqual(result['warnings'].count(self.WARNING), int(halts), (runner, result['warnings']))
        return runner, halts

    def test_only_a_codex_implement_needs_the_test_command(self):
        cases = [  # agents, environment, the runner implement resolves
            ({'runner': 'claude', 'implement': True}, {}, 'claude'),
            ({'implement': True}, {}, 'claude'),                                          # no agents.runner: claude
            ({'runner': 'codex', 'implement': True}, {}, 'codex'),                        # a boolean takes the default runner
            ({'runner': 'claude', 'implement': 'true'}, {}, 'claude'),                    # so does a legacy string
            ({'runner': 'claude', 'implement': {'enabled': True, 'runner': 'codex'}}, {}, 'codex'),
            ({'runner': 'codex', 'implement': {'enabled': True, 'runner': 'claude'}}, {}, 'claude'),
            ({'runner': 'codex', 'implement': {'enabled': True}}, {}, 'codex'),          # an object without a runner
            ({'runner': 'codex', 'implement': {'enabled': True, 'runner': ''}}, {}, 'codex'),
            ({'runner': 'claude', 'implement': {'enabled': False, 'runner': 'codex'}}, {}, 'codex'),  # off: the shepherd still runs it
            ({'runner': 'codex', 'implement': False}, {}, 'codex'),
            ({'runner': 'claude', 'implement': True, 'qa': {'enabled': True, 'runner': 'codex'},
              'providers': {'codex': {'model': 'gpt-test', 'timeout_seconds': 3600}}}, {}, 'claude'),  # Codex for other stages only
            ({'runner': 'claude', 'implement': True}, {'BUREAU_RUNNER_IMPLEMENT': 'codex'}, 'codex'),
            ({'runner': 'codex', 'implement': {'enabled': True, 'runner': 'codex'}}, {'BUREAU_RUNNER_IMPLEMENT': 'claude'}, 'claude'),
        ]
        warned = 0
        for agents, env, runner in cases:
            with self.subTest(agents=agents, env=env):
                self.assertEqual(self.check(agents, env), (runner, runner == 'codex'))
                warned += runner == 'codex'
                # A configured command satisfies it.
                self.assertEqual(self.check(agents, env, test_command='python3 test.py'), (runner, False))
        self.assertEqual(warned, 7)

    def test_an_empty_test_command_is_a_missing_one(self):
        # The gate reads `.repo.test_command // empty` and stops on an empty result: "", false and
        # null stop a Codex implement with 24, a command of blanks runs (and passes) as a command.
        # On Claude an empty command stops nothing (v3.1 warned there too).
        codex = {'runner': 'codex', 'implement': True}
        for value, halts in (('', True), (False, True), ('  ', False), ('true', False)):
            with self.subTest(test_command=value):
                self.assertEqual(self.check(codex, test_command=value), ('codex', halts))
        for value in ('', False):
            with self.subTest(test_command=value, runner='claude'):
                self.assertEqual(self.check({'runner': 'claude', 'implement': True}, test_command=value), ('claude', False))
        config = copy.deepcopy(BASE); config['agents'].update(codex); config['repo']['test_command'] = None
        self.write_config(config)
        self.assertEqual(self.stage({}), ('codex', True))
        self.assertIn(self.WARNING, self.diagnose(config)['warnings'])

    def test_a_codex_implement_set_in_the_env_file_counts(self):
        # BUREAU_RUNNER_IMPLEMENT is one of the keys the stages load from .env (bureau-env.sh), and a
        # key the file sets replaces the environment's. Doctor reads it with the stages' own reader;
        # nothing in the file runs.
        claude = {'runner': 'claude', 'implement': True}
        mark = self.base / 'executed'
        cases = [  # .env text, environment, runner, stops
            ('BUREAU_RUNNER_IMPLEMENT=codex\n', {}, 'codex', True),
            ("  export BUREAU_RUNNER_IMPLEMENT='codex'   # set by hand\r\n", {}, 'codex', True),
            ('LINEAR_API_KEY=lin_test\nBUREAU_RUNNER_IMPLEMENT=claude\nBUREAU_RUNNER_IMPLEMENT="codex"\n', {}, 'codex', True),  # the last entry wins
            ('# BUREAU_RUNNER_IMPLEMENT=codex\n', {}, 'claude', False),
            ('BUREAU_RUNNER_IMPLEMENT = codex\n', {}, 'claude', False),                  # not a NAME=VALUE line
            ('BUREAU_RUNNER_IMPLEMENT=claude\n', {'BUREAU_RUNNER_IMPLEMENT': 'codex'}, 'claude', False),  # the file wins
            ('LINEAR_API_KEY=lin_test\n', {'BUREAU_RUNNER_IMPLEMENT': 'codex'}, 'codex', True),       # the environment stays
            (f"BUREAU_RUNNER_IMPLEMENT=codex; touch '{mark}'\ntouch '{mark}'\n$(touch '{mark}')\n", {}, 'unresolved', False),
        ]
        for text, env, runner, halts in cases:
            with self.subTest(env_file=text, env=env):
                self.assertEqual(self.check(claude, env, env_file=text), (runner, halts))
        self.assertFalse(mark.exists(), 'a line of .env ran')
        # The reader's bash starts without BASH_ENV, which a non-interactive bash would source first.
        hook = self.base / 'bash-env.sh'; hook.write_text(f"touch '{mark}'\nBUREAU_RUNNER_IMPLEMENT=codex\n")
        config = copy.deepcopy(BASE); del config['repo']['test_command']
        (self.repo / '.env').write_text('LINEAR_API_KEY=lin_test\n')
        with patch.dict(os.environ, {'BASH_ENV': str(hook)}):
            self.assertNotIn(self.WARNING, self.diagnose(config)['warnings'])
        self.assertFalse(mark.exists(), 'doctor ran BASH_ENV')
        # A configured Codex default that the file turns back to Claude.
        self.assertEqual(self.check({'runner': 'codex', 'implement': True}, env_file='BUREAU_RUNNER_IMPLEMENT=claude\n'), ('claude', False))
        # No .env next to .bureau.json: the stages read BUREAU_ENV_FILE.
        elsewhere = self.base / 'secrets' / 'bureau.env'; elsewhere.parent.mkdir()
        (self.repo / '.env').unlink(missing_ok=True)
        self.assertEqual(self.check(claude, {'BUREAU_ENV_FILE': str(elsewhere)}, env_file='BUREAU_RUNNER_IMPLEMENT=codex\n', at=elsewhere), ('codex', True))

    def test_bureau_env_file_wins_over_the_checkout_doctor_runs_in(self):
        # The stage's worktree has no .env, so the stage reads BUREAU_ENV_FILE and never the main
        # checkout's .env; doctor, run in the main checkout, must read the same file.
        claude = {'runner': 'claude', 'implement': True}
        elsewhere = self.base / 'secrets' / 'bureau.env'; elsewhere.parent.mkdir()
        cases = [  # BUREAU_ENV_FILE's text (None: unset), the main checkout's .env, runner, stops
            ('BUREAU_RUNNER_IMPLEMENT=codex\n', 'LINEAR_API_KEY=lin_test\n', 'codex', True),
            ('LINEAR_API_KEY=lin_test\n', 'BUREAU_RUNNER_IMPLEMENT=codex\n', 'claude', False),
            (None, 'BUREAU_RUNNER_IMPLEMENT=codex\n', 'codex', True),   # unset: both read the .env next to .bureau.json
        ]
        for other, main, runner, halts in cases:
            with self.subTest(bureau_env_file=other, main_env=main):
                env = {}
                if other is not None: elsewhere.write_text(other); env['BUREAU_ENV_FILE'] = str(elsewhere)
                self.assertEqual(self.check(claude, env, env_file=main), (runner, halts))

    # The nine stages' own .env loading, each cut from its script: from LOAD_FIRST through the line
    # that closes its `if` (implement's is LOAD above; merge and rebase stop without the file).
    STAGES = ('implement', 'spec', 'spec-review', 'ux', 'copy', 'qa', 'code-review', 'merge', 'rebase')
    PROBE = ('cd "$PLACE" || exit 97\n'
             'source "$CONFIG_SH" >/dev/null 2>&1 || exit 99; SCRIPT_REPO="$REPO"\n'
             'for block in "$@"; do ( eval "$block" >/dev/null 2>&1; printf "%s\\n" "${LINEAR_API_KEY-}" ) || echo stopped; done')

    @classmethod
    def loader(cls, stage):
        lines = (ROOT / 'templates/scripts' / (stage + '-pipeline.sh')).read_text().splitlines()
        start = lines.index(cls.LOAD_FIRST)
        end = next(i for i in range(start, len(lines)) if lines[i].rstrip().endswith('fi'))
        return '\n'.join(lines[start:end + 1])

    def test_doctor_reads_the_env_file_every_stage_reads(self):
        # Doctor and the stages must take BUREAU_RUNNER_IMPLEMENT (and every other .env key) from
        # the same file: BUREAU_ENV_FILE, by default the .env next to .bureau.json, a relative value
        # counted from there, never a .env of the checkout they run in, which in a stage worktree
        # the branch controls. Every source holds its own Linear key, so the stages' loaders (cut
        # from the nine scripts, run in a bash that sourced the real bureau-config.sh) name the
        # source they read. Doctor (the real diagnose, with the process in that checkout) names it
        # through its repo.test_command warning: it warns when only that source sets codex and does
        # not when every other source does; its .env note appears exactly when the file exists.
        # The worktree's branch tracks a .env and a conf/bureau.env of its own. BUREAU_CONFIG is
        # exported (as the drivers and the runtime do), found from the checkout, or names a link to
        # the main checkout's .bureau.json in another directory: the stages take the directory of
        # the link (dirname), so doctor must not resolve it.
        config = self.config(runner='claude', implement=True); del config['repo']['test_command']; self.write_config(config)
        with (self.repo / '.git/info/exclude').open('a') as out: out.write('conf/\n')
        elsewhere = self.base / 'secrets' / 'bureau.env'; elsewhere.parent.mkdir()
        linked = self.base / 'linked config'; linked.mkdir(); (linked / '.bureau.json').symlink_to(self.repo / '.bureau.json')
        files = {'main': self.repo / '.env', 'main-conf': self.repo / 'conf/bureau.env', 'elsewhere': elsewhere,
                 'branch': self.worktree / '.env', 'branch-conf': self.worktree / 'conf/bureau.env',
                 'link': linked / '.env', 'link-conf': linked / 'conf/bureau.env'}
        for path in files.values(): path.parent.mkdir(parents=True, exist_ok=True); path.write_text('LINEAR_API_KEY=x\n')
        git(self.worktree, 'add', '-f', '.env', 'conf/bureau.env'); git(self.worktree, 'commit', '-qm', 'the branch tracks .env files')
        self.assertEqual(git(self.worktree, 'ls-files', '.env', 'conf/bureau.env').stdout.split(), ['.env', 'conf/bureau.env'])
        loaders = [self.loader(stage) for stage in self.STAGES]
        self.assertEqual(loaders[0], self.LOAD)
        for stage, block in zip(self.STAGES, loaders):
            self.assertIn('bureau_load_env --export "$BUREAU_ENV_FILE"', block, stage)

        def write(codex):  # every source gets its own Linear key; `codex` decides the runner
            for name, path in files.items():
                if path.exists(): path.write_text('LINEAR_API_KEY=lin_' + name + '\nBUREAU_RUNNER_IMPLEMENT=' + ('codex' if codex(name) else 'claude') + '\n')
            return {'LINEAR_API_KEY': 'lin_env', 'BUREAU_RUNNER_IMPLEMENT': 'codex' if codex('env') else 'claude'}

        def doctor(place, env):
            before = os.getcwd(); os.chdir(place)
            try:
                with patch.dict(os.environ, env):
                    for name in [n for n in ('BUREAU_ENV_FILE', 'BUREAU_CONFIG') if n not in env]: os.environ.pop(name, None)
                    result = d.diagnose(place, 'app')
            finally: os.chdir(before)
            self.assertTrue(result['ok'], result)
            return self.WARNING in result['warnings'], any(w.startswith('Doctor resolves JSON') for w in result['warnings'])

        cases = [  # label, BUREAU_ENV_FILE (None: unset), sources removed, the source the stages read
            ('unset', None, (), 'main'),
            ('empty', '', (), 'main'),
            ('absolute', str(elsewhere), (), 'elsewhere'),
            ('absolute, missing', str(self.base / 'secrets/missing.env'), (), 'env'),
            ('relative .env', '.env', (), 'main'),
            ('relative conf/bureau.env', 'conf/bureau.env', (), 'main-conf'),
            ('relative, missing next to .bureau.json', 'conf/bureau.env', ('main-conf',), 'env'),
            ('unset, no .env next to .bureau.json', None, ('main',), 'env'),
        ]
        configs = {'exported': self.repo / '.bureau.json', 'found': None, 'link': linked / '.bureau.json'}
        for label, value, removed, main_source in cases:
            case_env = {} if value is None else {'BUREAU_ENV_FILE': value}
            for place in (self.repo, self.worktree):
                for mode, named in configs.items():
                    # Through the link the directory of .bureau.json is the link's: its .env.
                    to_link = {'main': 'link', 'main-conf': 'link-conf'} if mode == 'link' else {}
                    source = to_link.get(main_source, main_source)
                    for path in files.values(): path.write_text('LINEAR_API_KEY=x\n')
                    for name in removed: files[to_link.get(name, name)].unlink()
                    with self.subTest(case=label, place=place.name, bureau_config=mode):
                        env = {**case_env, **({'BUREAU_CONFIG': str(named)} if named else {})}
                        stage_env = {**os.environ, **write(lambda name: False), **env, 'REPO': str(self.repo), 'PLACE': str(place), 'CONFIG_SH': str(self.CONFIG_SH)}
                        for name in [n for n in ('BUREAU_ENV_FILE', 'BUREAU_CONFIG') if n not in env]: stage_env.pop(name, None)
                        proc = subprocess.run(['/bin/bash', '-c', self.PROBE, 'probe', *loaders], capture_output=True, text=True, env=stage_env)
                        self.assertEqual(proc.returncode, 0, proc.stderr)
                        read = dict(zip(self.STAGES, proc.stdout.split()))
                        expected = {stage: 'stopped' if source == 'env' and stage in ('merge', 'rebase') else 'lin_' + source for stage in self.STAGES}
                        self.assertEqual(read, expected)
                        warned, note = doctor(place, {**env, **write(lambda name: name == source)})
                        if not warned:
                            used = [name for name in list(files) + ['env'] if (name == 'env' or files[name].exists())
                                    and doctor(place, {**env, **write(lambda other: other == name)})[0]]
                            self.fail('doctor did not read ' + source + ', the source every stage read; it read ' + (', '.join(used) or 'none'))
                        self.assertEqual(note, source != 'env')
                        self.assertEqual(doctor(place, {**env, **write(lambda name: name != source)}), (False, source != 'env'))

    def test_an_implement_runner_that_does_not_resolve_is_no_codex(self):
        # Implement off and an unknown runner in the environment: the stage would stop on it,
        # doctor (which reports it for an enabled stage only) gives no test_command warning.
        config = copy.deepcopy(BASE); config['agents']['implement'] = False; del config['repo']['test_command']
        with patch.dict(os.environ, {'BUREAU_RUNNER_IMPLEMENT': 'other'}):
            result = self.diagnose(config)
        self.assertTrue(result['ok'], result); self.assertNotIn(self.WARNING, result['warnings'])


class WorktreeLinkTests(Repo):
    def setUp(self):
        super().setUp()
        (self.repo / '.gitignore').write_text('.venv\n.env*\nsecrets\n')
        git(self.repo, 'add', '.gitignore'); git(self.repo, 'commit', '-qm', 'ignore')

    def links(self, *entries, repo=None):
        config = copy.deepcopy(BASE); config['repo']['worktree_links'] = list(entries)
        return self.diagnose(config, repo)

    def statuses(self, result):
        return {entry['path']: entry['status'] for entry in result['worktree_links']}

    def test_a_linked_worktree_is_judged_against_the_main_checkout(self):
        (self.repo / '.venv/bin').mkdir(parents=True)
        worktree = self.base / 'stage worktree'
        git(self.repo, 'worktree', 'add', '-q', '--detach', str(worktree))
        self.assertFalse((worktree / '.bureau.json').exists())
        self.assertFalse((worktree / '.venv').exists())
        from_main = self.links('.venv')
        from_worktree = self.links('.venv', repo=worktree)
        for result in (from_main, from_worktree):
            self.assertEqual(self.statuses(result), {'.venv': 'ok'}, result)
        for result in (from_main, from_worktree):
            self.assertEqual(result['main_checkout'], str(self.repo)); self.assertTrue(result['ok'], result)
            self.assertFalse([w for w in result['warnings'] if 'worktree_links' in w], result['warnings'])
        self.assertEqual(from_worktree['workspace'], str(worktree))
        # Without it in the main checkout it is missing, even when the worktree has one.
        (self.repo / '.venv/bin').rmdir(); (self.repo / '.venv').rmdir(); (worktree / '.venv').mkdir()
        result = self.links('.venv', repo=worktree)
        self.assertEqual(self.statuses(result), {'.venv': 'missing in the main checkout'})
        # Tracking is the main checkout's too.
        (self.repo / 'tools').mkdir(); (self.repo / 'tools/cfg').write_text('x\n')
        git(self.repo, 'add', 'tools/cfg'); git(self.repo, 'commit', '-qm', 'tools')
        self.assertFalse((worktree / 'tools').exists())
        self.assertEqual(self.statuses(self.links('tools', repo=worktree)), {'tools': 'tracked in the main checkout'})

    def test_env_files_are_rejected(self):
        for name in ('.env', '.env.local', '.envrc', '.ENV.production'):
            (self.repo / name).write_text('LINEAR_API_KEY=probe\n')
        (self.repo / 'config').mkdir(); (self.repo / 'config/.env.test').write_text('x\n')
        (self.repo / '.env.d').mkdir(); (self.repo / '.env.d/app').write_text('x\n')  # a path through a .env* directory
        (self.repo / 'secrets').symlink_to('.env.local')  # a link whose target is a .env file
        (self.repo / '.venv').mkdir()
        for entry, status in (('.env', 'env file'), ('.env.local', 'env file'), ('.envrc', 'env file'),
                              ('.ENV.production', 'env file'), ('config/.env.test', 'env file'), ('.env.d/app', 'env file'),
                              ('.env.missing', 'env file'),
                              ('secrets', 'env file'), ('.venv', 'ok')):
            with self.subTest(entry=entry):
                result = self.links(entry)
                self.assertEqual(self.statuses(result), {entry: status}, result)
                found = [e for e in result['errors'] if 'worktree_links' in e]
                self.assertEqual(len(found), int(status == 'env file'), result['errors'])
                self.assertEqual(result['ok'], status != 'env file', result)
                if found: self.assertIn('is a .env file', found[0]); self.assertIn(json.dumps(entry), found[0])
        # One bad entry does not hide the others.
        result = self.links('.venv', '.env')
        self.assertEqual(self.statuses(result), {'.venv': 'ok', '.env': 'env file'}); self.assertFalse(result['ok'])

    def test_directories_holding_env_files_are_rejected(self):
        # v3.2: a directory entry is searched the way the stages search it before they link it
        # (find -L, links followed, no depth limit): a .env* name anywhere below it, or a search
        # that does not finish, is an error. v3.1 reported every one of these as `ok`.
        (self.repo / 'settings').mkdir(); (self.repo / 'settings/.env').write_text('LINEAR_API_KEY=probe\n')
        (self.repo / 'deeper/a/b').mkdir(parents=True); (self.repo / 'deeper/a/b/.Env.Local').write_text('x\n')
        (self.repo / 'store/.envdir').mkdir(parents=True)                       # a .env* directory below, empty
        (self.repo / 'via').mkdir(); (self.repo / 'via/conf').symlink_to('../settings')   # a link inside, followed
        (self.repo / 'alias').symlink_to('settings')                            # the entry itself is a link
        (self.repo / '.venv/bin').mkdir(parents=True); (self.repo / '.venv/bin/python').write_text('')
        (self.repo / 'tools/envs').mkdir(parents=True); (self.repo / 'tools/my.env').write_text('x\n')  # names that only contain env
        (self.repo / 'notes.txt').write_text('x\n')                             # a file: nothing to search
        for inner in ('a', 'b'):                                                 # two hits: the search stops at the first
            (self.repo / 'twice' / inner).mkdir(parents=True); (self.repo / 'twice' / inner / '.env').write_text('x\n')
        with (self.repo / '.gitignore').open('a') as out:
            out.write('settings\ndeeper\nstore\nvia\nalias\ntools\nnotes.txt\nlocked\ntwice\n')
        expected = {'settings': ('holds an env file', 'settings/.env'), 'deeper': ('holds an env file', 'deeper/a/b/.Env.Local'),
                    'store': ('holds an env file', 'store/.envdir'), 'via': ('holds an env file', 'via/conf/.env'),
                    'alias': ('holds an env file', 'alias/.env'), 'twice': ('holds an env file', ('twice/a/.env', 'twice/b/.env')),
                    '.venv': ('ok', None), 'tools': ('ok', None), 'notes.txt': ('ok', None)}
        if os.getuid() != 0:  # as root an unreadable directory is readable
            (self.repo / 'locked/inner').mkdir(parents=True); (self.repo / 'locked/inner').chmod(0o000)
            self.addCleanup((self.repo / 'locked/inner').chmod, 0o700)
            expected['locked'] = ('not searched completely', None)
        for entry, (status, hit) in expected.items():
            with self.subTest(entry=entry):
                result = self.links(entry)
                self.assertEqual(self.statuses(result), {entry: status}, result)
                found = [e for e in result['errors'] if 'worktree_links' in e]
                self.assertEqual(len(found), int(status != 'ok'), result['errors'])
                self.assertEqual(result['ok'], status == 'ok', result)
                if hit:
                    named = [h for h in ((hit,) if isinstance(hit, str) else hit)
                             if json.dumps(entry) + ' is a directory that holds a .env file (' + h + '):' in found[0]]
                    self.assertEqual(len(named), 1, found[0])
                if status == 'not searched completely': self.assertIn('could not be searched completely', found[0])
        # Each entry is judged on its own; a trailing slash names the same directory.
        result = self.links('.venv', 'settings/', 'tools')
        self.assertEqual(self.statuses(result), {'.venv': 'ok', 'settings': 'holds an env file', 'tools': 'ok'})
        self.assertEqual(len(result['errors']), 1, result['errors'])

    def test_a_search_that_fails_is_an_error(self):
        # Fail closed as the stages do: a find that fails is no answer, even when it printed a hit
        # first; a missing find too. A file entry is not searched at all.
        (self.repo / '.venv/bin').mkdir(parents=True); (self.repo / 'notes.txt').write_text('x\n')
        with (self.repo / '.gitignore').open('a') as out: out.write('notes.txt\n')
        fake = self.base / 'fake find'; fake.mkdir()
        (fake / 'find').write_text('#!/bin/sh\necho "$2/.env"\nexit 1\n'); (fake / 'find').chmod(0o755)
        os.environ['PATH'] = str(fake) + os.pathsep + os.environ['PATH']
        result = self.links('.venv', 'notes.txt')
        self.assertEqual(self.statuses(result), {'.venv': 'not searched completely', 'notes.txt': 'ok'}, result)
        self.assertFalse(result['ok'])
        # No find on PATH at all: the search cannot run, the directory is not linked.
        with patch.object(d.subprocess, 'run', side_effect=lambda command, *a, **k: (_ for _ in ()).throw(FileNotFoundError(command[0]))
                          if command[0] == 'find' else REAL_RUN(command, *a, **k)):
            result = self.links('.venv')
        self.assertEqual(self.statuses(result), {'.venv': 'not searched completely'}, result)

    def test_no_main_checkout_means_no_links(self):
        # A git directory kept outside the checkout: the stages make no links.
        separate = self.base / 'separate checkout'
        git(self.base, 'init', '-q', '--separate-git-dir', str(self.base / 'elsewhere.git'), str(separate))
        (separate / '.venv').mkdir()
        repo, self.repo = self.repo, separate
        self.write_config(BASE)
        subprocess.run([sys.executable, str(INSTALLER), 'assets', '--repo', str(separate), '--target', 'codex',
                        '--scope', 'interfaces', '--scope', 'scripts', '--apply'], check=True, stdout=subprocess.DEVNULL)
        result = self.links('.venv', '.env')
        self.assertIsNone(result['main_checkout'])
        self.assertEqual(self.statuses(result), {'.venv': 'no main checkout', '.env': 'env file'})
        found = [w for w in result['warnings'] if 'worktree_links' in w]
        self.assertEqual(len(found), 1, found); self.assertIn('--separate-git-dir', found[0]); self.assertIn('stages make no links', found[0])
        self.assertIsNone(self.links()['main_checkout'])
        self.assertFalse([w for w in self.links()['warnings'] if 'worktree_links' in w])
        # A worktree of a bare repository has no main checkout either.
        self.repo = repo
        bare = self.base / 'bare.git'
        git(self.base, 'clone', '-q', '--bare', str(repo), str(bare))
        worktree = self.base / 'bare worktree'
        git(bare, 'worktree', 'add', '-q', '--detach', str(worktree))
        self.repo = worktree; self.write_config(BASE); (worktree / '.venv').mkdir()
        result = self.links('.venv')
        self.assertIsNone(result['main_checkout'])
        self.assertEqual(self.statuses(result), {'.venv': 'no main checkout'})
        self.assertTrue(any('is bare' in w for w in result['warnings']), result['warnings'])


if __name__ == '__main__': unittest.main()
