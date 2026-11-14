"""The agent processes run without the Bureau secrets (repo.untrusted_env).

The agents run the branch's tests (Claude with --dangerously-skip-permissions) and
load the branch's own agent settings, so bureau-provider.py starts them, and the
login check before them, in the environment bureau_untrusted_env in bureau-env.sh
gives every other command the branch controls. These tests run the REAL adapter as a
subprocess with fake `claude` and `codex` executables that write the environment they
received to a file; the environment the adapter itself is started with carries the
secrets, as it does under a stage. A pass-through control (the v3.0.2 adapter
behaviour, env inherited) shows that the same fakes see every secret, so the
assertions below cannot pass on a fixture that never carried one.
"""
import importlib.util
import json
import os
import shlex
import shutil
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'templates/scripts/bureau-provider.py'
spec = importlib.util.spec_from_file_location('provider', SCRIPT)
p = importlib.util.module_from_spec(spec); spec.loader.exec_module(p)

SECRETS = {
    'LINEAR_API_KEY': 'lin_api_PROBE_linear_0001',
    'TELEGRAM_BOT_TOKEN': 'PROBE_telegram_bot_0002',
    'TELEGRAM_ALERT_CHAT_ID': '-100PROBE0003',
    'GH_TOKEN': 'ghp_PROBE_gh_token_0004',
    'GITHUB_TOKEN': 'ghs_PROBE_github_token_0005',
    'GH_ENTERPRISE_TOKEN': 'PROBE_gh_enterprise_0006',
    'GITHUB_ENTERPRISE_TOKEN': 'PROBE_github_enterprise_0007',
}
# API_KEY is where the stages copy the Linear key; CARGO_ALIAS holds a GitHub token under
# an unrelated name, REMOTE_URL has one inside. All three must go by value.
ALIASES = {'API_KEY': SECRETS['LINEAR_API_KEY'], 'CARGO_ALIAS': SECRETS['GH_TOKEN'],
           'REMOTE_URL': 'https://x-access-token:' + SECRETS['GITHUB_TOKEN'] + '@github.com/owner/repo.git'}
OPERATOR = {'OPERATOR_TOOL_VAR': 'keep_me_operator_var_0008', 'MY_HERDR_NOTE': 'keep_this_note'}
HOST = {'HERDR_PANE_ID': 'operator-pane', 'HERDR_SOCKET_PATH': '/tmp/herdr-probe.sock',
        'HERDR_ENV': 'pane', 'HERDR_BIN_PATH': '/tmp/herdr-probe', 'HERDR_TAB_ID': 'operator-tab',
        'HERDR_WORKSPACE_ID': 'operator-workspace', 'HERDR_FUTURE_VAR': 'future', 'HERDR_EMPTY': ''}
# No secrets, but the absolute paths to the operator's .env and .bureau.json: Bureau's own
# scripts read them, the agent gets neither.
PATHS = {'BUREAU_ENV_FILE': '/operator/checkout/.env', 'BUREAU_CONFIG': '/operator/checkout/.bureau.json'}
AGENT = {'ANTHROPIC_API_KEY': 'sk-ant-PROBE-agent-login', 'CLAUDE_CODE_OAUTH_TOKEN': 'PROBE_claude_oauth',
         'OPENAI_API_KEY': 'sk-openai-PROBE-agent-login', 'CODEX_HOME': '/tmp/codex-home-probe',
         'HTTPS_PROXY': 'http://proxy.invalid:3128'}
GIT_OFF = {'GIT_CONFIG_COUNT': '1', 'GIT_CONFIG_KEY_0': 'credential.helper', 'GIT_CONFIG_VALUE_0': '',
           'GIT_TERMINAL_PROMPT': '0'}

FAKE = '#!' + sys.executable + '''
import json, os, pathlib, sys
name = pathlib.Path(sys.argv[0]).name
root = pathlib.Path(os.environ.get('PR1_FAKE_ROOT') or pathlib.Path(sys.argv[0]).resolve().parent.parent)
phase = 'auth' if sys.argv[1] in ('auth', 'login') else 'run'
(root / (name + '.' + phase + '.json')).write_text(json.dumps(dict(os.environ)))
if phase == 'auth':
    print(json.dumps({'loggedIn': True})); sys.exit(0)
sys.stdin.read()
if name == 'codex':
    pathlib.Path(sys.argv[sys.argv.index('-o') + 1]).write_text('final response')
    print(json.dumps({'type': 'turn.completed', 'usage': {}}))
else:
    print(json.dumps({'result': 'final response', 'usage': {}}))
'''


class UntrustedEnvProviderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='bureau untrusted env '); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name); self.bin = self.root / 'bin'; self.bin.mkdir()
        subprocess.run(['git', '-C', str(self.root), 'init', '-q'], check=True)
        subprocess.run(['git', '-C', str(self.root), 'config', 'maintenance.auto', 'false'], check=True)
        for name in ('claude', 'codex'):
            path = self.bin / name; path.write_text(FAKE); path.chmod(0o755)
        self.config = self.root / 'config.json'
        self.prompt = self.root / 'prompt.txt'; self.prompt.write_text('fixture prompt')
        base = {k: v for k, v in os.environ.items() if not k.startswith(('BUREAU_RUNNER_', 'BUREAU_MODEL_', 'BUREAU_CODEX_MODEL_', 'BUREAU_STAGE_TIMEOUT'))}
        for name in list(base):
            if name in SECRETS or name in AGENT or name.endswith('_PROXY') or name.endswith('_proxy'): base.pop(name)
        self.env = {**base, **SECRETS, **ALIASES, **OPERATOR, **AGENT, **PATHS, **HOST, 'BASH_ENV': '/dev/null', 'ENV': '/dev/null',
                    'LANG': 'C',
                    'PATH': str(self.bin) + os.pathsep + os.environ['PATH'],
                    'BUREAU_PROVIDER_LOG_DIR': str(self.root / 'evidence')}

    def provider(self, runner, untrusted=None, *extra, code=0, env=None, credentials=None):
        config = {'agents': {'runner': runner}}
        if untrusted is not None: config['repo'] = {'untrusted_env': untrusted}
        if credentials is not None: config.setdefault('repo', {})['untrusted_git_credentials'] = credentials
        self.config.write_text(json.dumps(config))
        command = [sys.executable, str(SCRIPT), '--stage', 'implement', '--config', str(self.config),
                   '--repo', str(self.root), '--prompt-file', str(self.prompt), *extra]
        result = subprocess.run(command, capture_output=True, text=True, env=env or self.env, timeout=30)
        self.assertEqual(result.returncode, code, result.stdout + result.stderr)
        return result

    def seen(self, runner, phase):
        path = self.root / (runner + '.' + phase + '.json')
        self.assertTrue(path.exists(), runner + ' ' + phase + ' did not run')
        return json.loads(path.read_text())

    def test_git_credentials_in_the_login_check_and_agent(self):
        include = self.root / 'inherited.config'; include.write_text('')
        inherited = {'GIT_CONFIG_COUNT': '1', 'GIT_CONFIG_KEY_0': 'credential.helper',
                     'GIT_CONFIG_VALUE_0': 'inherited-helper', 'GIT_TERMINAL_PROMPT': '1',
                     'GIT_CONFIG_PARAMETERS': shlex.quote('include.path=' + str(include))}
        for runner in ('claude', 'codex'):
            for mode in ('default', 'clean'):
                for setting in (None, 'off', 'keep'):
                    with self.subTest(runner=runner, mode=mode, setting=setting):
                        self.provider(runner, mode, credentials=setting, env={**self.env, **inherited})
                        expected = dict(inherited) if mode == 'default' else {}
                        if setting != 'keep':
                            expected.pop('GIT_CONFIG_PARAMETERS', None)
                            index = '1' if mode == 'default' else '0'
                            expected.update(GIT_CONFIG_COUNT=str(int(index) + 1), GIT_TERMINAL_PROMPT='0')
                            expected.update({'GIT_CONFIG_KEY_' + index: 'credential.helper', 'GIT_CONFIG_VALUE_' + index: ''})
                        for phase in ('auth', 'run'):
                            seen = self.seen(runner, phase)
                            self.assertEqual({k:v for k,v in seen.items() if k.startswith(('GIT_CONFIG_KEY_', 'GIT_CONFIG_VALUE_'))
                                              or k in ('GIT_CONFIG_COUNT', 'GIT_CONFIG_PARAMETERS', 'GIT_TERMINAL_PROMPT')}, expected)

    def test_invalid_git_credentials_refuse_before_the_login_check(self):
        for bad in ('', 'OFF', True, 1, ['off'], {'mode': 'off'}):
            for mode in ('default', 'clean'):
                with self.subTest(bad=bad, mode=mode):
                    result = self.provider('claude', mode, credentials=bad, code=24)
                    self.assertIn('repo.untrusted_git_credentials', result.stderr)
                    self.assertFalse((self.root / 'claude.auth.json').exists())

    def test_malformed_git_config_count_resets_before_the_login_check(self):
        for runner in ('claude', 'codex'):
            for mode in ('default', 'clean'):
                with self.subTest(runner=runner, mode=mode):
                    self.provider(runner, mode, env={**self.env, 'GIT_CONFIG_COUNT': '',
                                                   'GIT_CONFIG_KEY_9': 'credential.helper', 'GIT_CONFIG_VALUE_9': 'inherited-helper'})
                    for phase in ('auth', 'run'):
                        seen = self.seen(runner, phase)
                        self.assertEqual({k:v for k,v in seen.items() if k.startswith(('GIT_CONFIG_KEY_', 'GIT_CONFIG_VALUE_'))
                                          or k in GIT_OFF}, GIT_OFF)

    def assert_no_secret(self, seen, label):
        # assertTrue/assertFalse, never assertIn on the environment: a failure must name the
        # variable, not print the whole environment (it holds the runner's real variables).
        for name, value in {**SECRETS, **ALIASES}.items():
            self.assertFalse(name in seen, label + ': ' + name + ' reached the agent')
            carriers = sorted(n for n, v in seen.items() if value in v)
            self.assertEqual(carriers, [], label + ': the value of ' + name + ' reached the agent')

    def test_default_removes_the_secrets_and_their_copies_from_login_check_and_agent(self):
        for runner in ('claude', 'codex'):
            for mode in (None, 'default'):
                with self.subTest(runner=runner, mode=mode):
                    self.provider(runner, mode)
                    for phase in ('auth', 'run'):
                        seen = self.seen(runner, phase)
                        self.assert_no_secret(seen, runner + ' ' + phase)
                        self.assertFalse('BASH_ENV' in seen or 'ENV' in seen, runner + ' ' + phase + ' got BASH_ENV or ENV')
                        for name in PATHS: self.assertFalse(name in seen, runner + ' ' + phase + ' got ' + name)
                        for name in HOST: self.assertFalse(name in seen, runner + ' ' + phase + ' got ' + name)
                        # Everything else stays: the operator's tool variables and the agent login.
                        for name, value in {**OPERATOR, **AGENT}.items():
                            self.assertEqual(seen.get(name), value, runner + ' ' + phase + ' lost ' + name)
                        self.assertEqual(seen['PATH'], self.env['PATH'])
                        self.assertEqual(seen['LANG'], 'C')

    def test_clean_keeps_only_the_base_list_and_the_agents_own_login(self):
        own = {'claude': ('ANTHROPIC_API_KEY', 'CLAUDE_CODE_OAUTH_TOKEN'), 'codex': ('OPENAI_API_KEY', 'CODEX_HOME')}
        for runner in ('claude', 'codex'):
            with self.subTest(runner=runner):
                self.provider(runner, 'clean')
                for phase in ('auth', 'run'):
                    seen = self.seen(runner, phase)
                    self.assert_no_secret(seen, runner + ' ' + phase)
                    other = 'codex' if runner == 'claude' else 'claude'
                    for name in own[runner]: self.assertEqual(seen.get(name), AGENT[name], runner + ' lost its login ' + name)
                    for name in own[other]: self.assertFalse(name in seen, runner + ' got the other agent\'s ' + name)
                    self.assertEqual(seen.get('HTTPS_PROXY'), AGENT['HTTPS_PROXY'])
                    self.assertFalse('OPERATOR_TOOL_VAR' in seen, 'clean kept an operator variable')
                    self.assertFalse('BUREAU_PROVIDER_LOG_DIR' in seen, 'clean kept a Bureau variable')
                    for name in PATHS: self.assertFalse(name in seen, 'clean kept ' + name)
                    for name in HOST: self.assertFalse(name in seen, 'clean kept ' + name)
                    allowed = set(p.UNTRUSTED_KEEP) | set(p.AGENT_KEEP) | set(GIT_OFF)
                    stray = [n for n in seen if n not in allowed and not n.startswith(p.AGENT_KEEP_PREFIXES[runner])
                             and not n.startswith('__CF') and n not in ('LC_CTYPE',)]
                    self.assertEqual(stray, [], runner + ' ' + phase + ' got variables outside the clean list')
                    self.assertEqual(seen['PATH'], self.env['PATH'])
                    self.assertEqual(seen['LANG'], 'C')

    def test_host_prefix_is_removed_in_both_modes_without_changing_other_variables(self):
        environ = {**HOST, **OPERATOR, 'LANG': 'C', 'PATH': '/bin'}
        before = dict(environ)
        for mode in ('default', 'clean'):
            for runner in (None, 'claude', 'codex'):
                with self.subTest(mode=mode, runner=runner):
                    reduced = p.untrusted_env(environ, mode, runner)
                    expected = {'LANG': 'C', 'PATH': '/bin', **GIT_OFF}
                    if mode == 'default': expected.update(OPERATOR)
                    self.assertEqual(reduced, expected)
        self.assertEqual(environ, before)

    def test_clean_agent_keep_prefixes_cannot_readmit_host_variables(self):
        for runner, prefix in (('claude', 'CLAUDE_'), ('codex', 'CODEX_')):
            with self.subTest(runner=runner):
                note = prefix + 'MY_HERDR_NOTE'
                environ = {**HOST, note: 'keep_this_note', 'PATH': '/bin', 'LANG': 'C'}
                # Even an overlapping agent allowlist must not override the host exclusion.
                with patch.dict(p.AGENT_KEEP_PREFIXES, {runner: p.AGENT_KEEP_PREFIXES[runner] + ('HERDR_',)}):
                    self.assertEqual(p.untrusted_env(environ, 'clean', runner),
                                     {note: 'keep_this_note', 'PATH': '/bin', 'LANG': 'C', **GIT_OFF})

    def test_invalid_mode_refuses_before_any_agent_or_login_check(self):
        for bad in ('cleen', '', True, 1, ['clean'], {'mode': 'clean'}, 'clean\n'):
            with self.subTest(bad=bad):
                for extra in ((), ('--check',)):
                    result = self.provider('claude', bad, *extra, code=24)
                    self.assertIn('repo.untrusted_env', result.stderr)
                    self.assertFalse((self.root / 'claude.auth.json').exists(), 'login check ran on an invalid mode')
                    self.assertFalse((self.root / 'claude.run.json').exists(), 'agent ran on an invalid mode')
        self.config.write_text(json.dumps({'agents': {'runner': 'claude'}, 'repo': 'not an object'}))
        result = subprocess.run([sys.executable, str(SCRIPT), '--stage', 'implement', '--config', str(self.config), '--check'],
                                capture_output=True, text=True, env=self.env, timeout=30)
        self.assertEqual(result.returncode, 24, result.stderr)
        # --describe reports the stage settings and stays usable for bureau-status.sh.
        self.provider('claude', 'cleen', '--describe')

    def test_rules_the_adapter_environment_is_left_alone_and_short_values_are_not_copies(self):
        environ = {**self.env}
        before = dict(environ)
        reduced = p.untrusted_env(environ, 'default', 'claude')
        self.assertEqual(environ, before)
        self.assertFalse('LINEAR_API_KEY' in reduced)
        with self.assertRaises(p.UntrustedEnvError): p.untrusted_env(environ, 'strict', 'claude')
        self.assertEqual(p.untrusted_env_mode({}), 'default')
        self.assertEqual(p.untrusted_env_mode({'repo': {}}), 'default')
        self.assertEqual(p.untrusted_env_mode({'repo': None}), 'default')
        self.assertEqual(p.untrusted_env_mode({'repo': {'untrusted_env': None}}), 'default')
        self.assertEqual(p.untrusted_env_mode({'repo': {'untrusted_env': 'clean'}}), 'clean')
        # A secret of 6 characters or more goes wherever it appears inside a value; a shorter one
        # only as the whole value (CI=12345 goes with a chat id 12345, x12345 stays).
        short = p.untrusted_env({'TELEGRAM_ALERT_CHAT_ID': '12345', 'CI': '12345', 'Z': 'x12345',
                                 'LINEAR_API_KEY': 'abc123', 'H': 'Authorization: abc123',
                                 'GH_TOKEN': '12345678', 'X': '12345678', 'Y': 'Bearer 12345678 end',
                                 'BASH_ENV': '/tmp/env.sh', 'ENV': '/tmp/env.sh', 'PATH': '/bin'}, 'default')
        self.assertEqual(short, {'Z': 'x12345', 'PATH': '/bin', **GIT_OFF})
        self.assertEqual(p.COPY_MIN, 6)

    def test_bureau_git_and_gh_processes(self):
        # process_env: the environment bureau-supervision.py, bureau-runtime.py and bureau-doctor.py
        # give the git and gh processes they start (the git() and gh() functions of bureau-env.sh).
        environ = {**SECRETS, **ALIASES, **OPERATOR, **HOST, 'BASH_ENV': '/dev/null', 'ENV': '/dev/null', 'PATH': '/bin'}
        local = p.process_env(['git', '-C', '/repo', '--literal-pathspecs', 'status', '--porcelain'], environ)
        self.assertEqual(local, {**OPERATOR, 'PATH': '/bin'})
        for command in (['git', '-C', '/repo', 'ls-remote', 'origin'], ['git', 'push'], ['git', '-c', 'x=y', 'fetch'],
                        ['gh', 'pr', 'create'], ['/usr/local/bin/gh', 'pr', 'merge']):
            with self.subTest(command=command):
                remote = p.process_env(command, environ)
                for name in ('LINEAR_API_KEY', 'TELEGRAM_BOT_TOKEN', 'TELEGRAM_ALERT_CHAT_ID', 'API_KEY', 'BASH_ENV', 'ENV'):
                    self.assertFalse(name in remote, name + ' reached ' + command[0])
                for name in HOST: self.assertFalse(name in remote, name + ' reached ' + command[0])
                for name, value in OPERATOR.items(): self.assertEqual(remote.get(name), value)
                self.assertEqual(remote.get('PATH'), '/bin')
                for name in ('GH_TOKEN', 'GITHUB_TOKEN', 'GH_ENTERPRISE_TOKEN', 'GITHUB_ENTERPRISE_TOKEN', 'CARGO_ALIAS', 'REMOTE_URL'):
                    self.assertEqual(remote.get(name), {**SECRETS, **ALIASES}[name], name + ' was taken from ' + command[0])
        self.assertEqual(p.git_subcommand(['-C', 'push', 'status']), 'status')
        # git 2.55's other options with a separate value: the value is no subcommand.
        self.assertEqual(p.git_subcommand(['--attr-source', 'HEAD', 'push', 'origin']), 'push')
        self.assertEqual(p.git_subcommand(['--shallow-file', 'x', 'fetch']), 'fetch')

    def test_lists_are_pinned(self):
        # Literal sets: widening the clean list (an SSH agent, a cloud key) or shortening the
        # removal list fails here on any runner, whatever its own environment holds.
        self.assertEqual(p.UNTRUSTED_REMOVE, ('LINEAR_API_KEY', 'TELEGRAM_BOT_TOKEN', 'TELEGRAM_ALERT_CHAT_ID',
                                              'GH_TOKEN', 'GITHUB_TOKEN', 'GH_ENTERPRISE_TOKEN', 'GITHUB_ENTERPRISE_TOKEN'))
        self.assertEqual(p.UNTRUSTED_STARTUP, ('BASH_ENV', 'ENV'))
        self.assertEqual(p.UNTRUSTED_PATHS, ('BUREAU_ENV_FILE', 'BUREAU_CONFIG'))
        self.assertEqual(p.UNTRUSTED_HOST_PREFIXES, ('HERDR_',))
        self.assertEqual(p.UNTRUSTED_KEEP, ('PATH', 'HOME', 'USER', 'LOGNAME', 'SHELL', 'TMPDIR', 'TEMP', 'TMP',
                                            'LANG', 'LC_ALL', 'LC_CTYPE', 'TERM', 'TZ', 'CI'))
        self.assertEqual(p.AGENT_KEEP_PREFIXES, {'claude': ('ANTHROPIC_', 'CLAUDE_', 'HEADROOM_'), 'codex': ('OPENAI_', 'CODEX_')})
        self.assertEqual(p.AGENT_KEEP, ('HTTP_PROXY', 'HTTPS_PROXY', 'NO_PROXY', 'ALL_PROXY', 'http_proxy', 'https_proxy',
                                        'no_proxy', 'all_proxy', 'SSL_CERT_FILE', 'SSL_CERT_DIR', 'NODE_EXTRA_CA_CERTS',
                                        'XDG_CONFIG_HOME'))

    def test_each_name_goes_by_name_even_where_value_matching_cannot_see_it(self):
        # Each secret with its own short value that no other variable carries: only the name
        # list can remove it, so value matching cannot hide a name missing from the list.
        environ = {name: 'v%d' % i for i, name in enumerate(p.UNTRUSTED_REMOVE)}
        environ.update(PATH='/bin', OTHER='v0-and-more')
        for mode, runner in (('default', None), ('default', 'claude'), ('clean', 'claude'), ('clean', 'codex')):
            with self.subTest(mode=mode, runner=runner):
                reduced = p.untrusted_env(environ, mode, runner)
                for name in p.UNTRUSTED_REMOVE: self.assertFalse(name in reduced, name + ' was kept')
        self.assertEqual(p.untrusted_env(environ, 'default'), {'PATH': '/bin', 'OTHER': 'v0-and-more', **GIT_OFF})

    def test_the_operator_paths_go_in_both_modes_and_stay_for_bureau_processes(self):
        environ = {**PATHS, **OPERATOR, 'PATH': '/bin'}
        for mode, runner in (('default', None), ('default', 'claude'), ('default', 'codex'), ('clean', 'claude'), ('clean', None)):
            with self.subTest(mode=mode, runner=runner):
                reduced = p.untrusted_env(environ, mode, runner)
                for name in PATHS: self.assertFalse(name in reduced, name + ' was kept')
        self.assertEqual(p.untrusted_env(environ, 'default'), {**OPERATOR, 'PATH': '/bin', **GIT_OFF})
        # Bureau's own git and gh processes are not code the branch controls in this sense: they keep
        # them, as before (process_env is unchanged).
        for command in (['git', 'status'], ['git', 'push'], ['gh', 'pr', 'view']):
            self.assertEqual({n: v for n, v in p.process_env(command, environ).items() if n in PATHS}, PATHS)

    def test_control_a_provider_that_keeps_the_paths_fails_the_path_assertions(self):
        # The same assertion against a copy of the adapter whose default mode keeps the two paths
        # (the v3.3.0 behaviour): it must see them, so the assertions above are not vacuous.
        source = SCRIPT.read_text()
        kept = source.replace(' and name not in UNTRUSTED_PATHS\n', '\n')
        self.assertNotEqual(kept, source, 'the control could not remove the UNTRUSTED_PATHS check')
        copy = self.root / 'bureau-provider-keep.py'; copy.write_text(kept)
        shutil.copy(SCRIPT.with_name('bureau-evidence.py'), self.root / 'bureau-evidence.py')
        shutil.copy(SCRIPT.with_name('bureau-gates.py'), self.root / 'bureau-gates.py')
        shutil.copy(SCRIPT.with_name('bureau-activity.py'), self.root / 'bureau-activity.py')
        shutil.copy(SCRIPT.with_name('bureau-ticket.py'), self.root / 'bureau-ticket.py')
        shutil.copy(SCRIPT.with_name('bureau-process.py'), self.root / 'bureau-process.py')
        control_spec = importlib.util.spec_from_file_location('provider_keep', copy)
        control = importlib.util.module_from_spec(control_spec); control_spec.loader.exec_module(control)
        reduced = control.untrusted_env({**PATHS, 'PATH': '/bin'}, 'default', 'claude')
        self.assertEqual({n: v for n, v in reduced.items() if n in PATHS}, PATHS)

    def test_control_an_inherited_environment_shows_every_secret(self):
        # The v3.0.2 adapter started the agent with the inherited environment. Same fakes,
        # started that way: every probe is there, so the assertions above are not vacuous.
        subprocess.run([str(self.bin / 'claude'), 'auth', 'status'], env={**self.env, 'PR1_FAKE_ROOT': str(self.root)},
                       capture_output=True, check=True, timeout=30)
        seen = self.seen('claude', 'auth')
        for name, value in {**SECRETS, **ALIASES}.items(): self.assertEqual(seen.get(name), value)


class GitCredentialEnvTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='bureau git credentials '); self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.config = self.root / 'config.json'
        self.env = dict(PATH=os.environ['PATH'], HOME=str(self.root), TMPDIR=str(self.root), LANG='C',
                        GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL=os.devnull)
        subprocess.run(['git', 'init', '-q', str(self.root)], env=self.env, check=True, timeout=10)

    def configure(self, mode, setting=None):
        config = {'repo': {'untrusted_env': mode, 'untrusted_git_credentials': setting}}
        self.config.write_text(json.dumps(config))
        return config

    def shell(self, environ, code=0, command='bureau_untrusted_env /usr/bin/env -0'):
        result = subprocess.run(['/bin/bash', '-c', 'source "$1"; ' + command, 'fixture',
                                 str(SCRIPT.with_name('bureau-env.sh'))],
                                env={**environ, 'BUREAU_CONFIG': str(self.config)}, capture_output=True, timeout=10)
        self.assertEqual(result.returncode, code, result.stderr.decode())
        return result

    def builders(self, mode, setting=None, **inherited):
        config = self.configure(mode, setting)
        environ = {**self.env, **inherited}
        reduced = p.untrusted_env(environ, mode, git_credentials=p.untrusted_git_credentials_mode(config))
        raw = self.shell(environ).stdout.decode()
        shell = dict(item.split('=', 1) for item in raw.split('\0') if item)
        ignore = {'PWD', 'SHLVL', '_', 'OLDPWD', '__CF_USER_TEXT_ENCODING'}
        self.assertEqual({k:v for k,v in shell.items() if k not in ignore}, reduced)
        return (reduced, shell)

    def test_ticket_gates_honor_git_credentials_setting(self):
        spec = importlib.util.spec_from_file_location('ticket', SCRIPT.with_name('bureau-ticket.py'))
        ticket = importlib.util.module_from_spec(spec); spec.loader.exec_module(ticket)
        helper = self.root / '.git/helper'
        helper.write_text('#!/bin/sh\ncat >/dev/null\nprintf "username=fixture\\npassword=stored-fixture\\n"\n')
        helper.chmod(0o755)
        subprocess.run(['git', '-C', str(self.root), 'config', 'credential.helper', '!' + shlex.quote(str(helper))],
                       env=self.env, check=True, timeout=10)
        command = "printf 'protocol=https\\nhost=offline\\n\\n' | git credential fill"
        for command_id in ('review.build', 'qa.test'):
            for mode in ('default', 'clean'):
                for setting in (None, 'off', 'keep', 'invalid'):
                    with self.subTest(command_id=command_id, mode=mode, setting=setting):
                        config = self.configure(mode, setting)
                        if setting is None: config['repo'].pop('untrusted_git_credentials')
                        config['repo']['test_command'] = command
                        ledger = ticket.Ledger.__new__(ticket.Ledger); ledger.config = config
                        ledger.progress = dict(enabled=False)
                        log = self.root / (command_id + '-' + mode + '-' + str(setting) + '.log')
                        argv = ['bureau-ticket', '--repo', str(self.root), '--issue', 'fixture', 'execute-gate',
                                '--command-id', command_id, '--command', command, '--path', str(log)]
                        if command_id == 'qa.test': argv.append('--qa-final')
                        with patch.dict(os.environ, {**self.env, 'GIT_TERMINAL_PROMPT': '0'}, clear=True), \
                                patch.object(ticket, 'Ledger', return_value=ledger), \
                                patch.object(ticket, 'diagnostic'), patch.object(sys, 'argv', argv):
                            code = ticket.main()
                        self.assertEqual(code, 24 if setting == 'invalid' else 0 if setting == 'keep' else 128)
                        self.assertEqual(log.exists(), setting != 'invalid')
                        if log.exists(): self.assertEqual('password=stored-fixture' in log.read_text(), setting == 'keep')
                        self.assertEqual(list((self.root / '.git/bureau/process-holds').glob('*')), [])

    def test_gate_runs_with_a_reset_for_malformed_counts_and_leaves_no_hold(self):
        store = p.e.Store(self.root / '.git/bureau')
        for mode in ('default', 'clean'):
            with self.subTest(mode=mode):
                log = self.root / ('gate-' + mode + '.log')
                # Resolve the trusted evidence root before inheriting Git's unusable count.
                with patch.dict(os.environ, {**self.env, 'GIT_CONFIG_COUNT': '',
                                            'GIT_CONFIG_KEY_9': 'credential.helper', 'GIT_CONFIG_VALUE_9': 'inherited-helper'}, clear=True), \
                        patch.object(p, 'evidence_store', return_value=store):
                    self.assertEqual(p.outside_gate(dict(timeout=10, untrusted_env=mode), self.root, '/usr/bin/env -0', log), (0, None))
                seen = dict(item.split('=', 1) for item in log.read_text().split('\0') if item)
                self.assertEqual({k:v for k,v in seen.items() if k.startswith(('GIT_CONFIG_KEY_', 'GIT_CONFIG_VALUE_'))
                                  or k in GIT_OFF}, GIT_OFF)
                self.assertEqual(list((self.root / '.git/bureau/process-holds').glob('*')), [])

    def test_malformed_counts_in_both_builders(self):
        pairs = {'GIT_CONFIG_KEY_0': 'credential.helper', 'GIT_CONFIG_VALUE_0': 'inherited-helper',
                 'GIT_CONFIG_KEY_9': 'example.value', 'GIT_CONFIG_VALUE_9': 'stale-entry'}
        for mode in ('default', 'clean'):
            self.configure(mode, 'off')
            for count in ('', 'nonnumeric', '-1', '1\n', '2147483647', '9' * 5000):
                for inherited in ({}, pairs):
                    environ = {**self.env, **inherited, 'GIT_CONFIG_COUNT': count, 'GIT_TERMINAL_PROMPT': '1'}
                    for implementation in ('python', 'shell'):
                        with self.subTest(mode=mode, count=count[:12], pairs=bool(inherited), implementation=implementation):
                            if implementation == 'python': seen = p.untrusted_env(environ, mode)
                            else: seen = dict(item.split('=', 1) for item in self.shell(environ).stdout.decode().split('\0') if item)
                            self.assertEqual({k:v for k,v in seen.items() if k.startswith(('GIT_CONFIG_KEY_', 'GIT_CONFIG_VALUE_'))
                                              or k in GIT_OFF}, GIT_OFF)
                            if implementation == 'shell': self.shell(environ, command='bureau_untrusted_env --check')

    def test_conflict_entries_reject_invalid_settings_before_git(self):
        spec = importlib.util.spec_from_file_location('conflicts', SCRIPT.with_name('bureau-conflicts.py'))
        conflicts = importlib.util.module_from_spec(spec); spec.loader.exec_module(conflicts)
        config = self.configure('default', 'invalid')
        transaction = conflicts.Transaction(self.root, config, 'base', self.config)
        with patch.object(transaction, 'git') as git:
            with self.assertRaises(conflicts.provider.UntrustedEnvError): transaction.execute()
        git.assert_not_called()
        command = '''source "$1/bureau-config.sh"
git() { printf GIT_RAN; return 1; }
merge_origin_main_or_abort fixture Test
'''
        result = subprocess.run(['/bin/bash', '-c', command, 'fixture', str(SCRIPT.parent)],
                                env={**self.env, 'BUREAU_CONFIG': str(self.config)}, capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 24, result.stderr.decode())
        self.assertNotIn(b'GIT_RAN', result.stdout)

    def test_stage_preconditions_reject_invalid_git_credentials(self):
        self.configure('default', 'invalid')
        for stage in ('implement', 'qa', 'code_review'):
            with self.subTest(stage=stage):
                script = SCRIPT.with_name(stage.replace('_', '-') + '-pipeline.sh').read_text()
                self.assertLess(script.index('\nprecondition_runner ' + stage + '\n'), script.index('\nbureau_stage_enter '))
                result = self.shell(self.env, code=24, command='source "' + str(SCRIPT.with_name('bureau-config.sh')) +
                                    '"; precondition_runner ' + stage + '; printf GATE_REACHED')
                self.assertIn(b'repo.untrusted_git_credentials', result.stderr)
                self.assertEqual(result.stdout, b'')
        config = self.configure('default', 'invalid'); config['agents'] = dict(merge_mode='auto')
        self.config.write_text(json.dumps(config))
        env_file = self.root / 'operator.env'; env_file.write_text('LINEAR_API_KEY=fixture\n')
        script = SCRIPT.with_name('rebase-pipeline.sh')
        prefix = script.read_text().split('\nprecondition_linear\n', 1)[0]
        result = subprocess.run(['/bin/bash', '-c', prefix + '\nprintf GATE_REACHED', str(script)],
                                cwd=self.root, env={**self.env, 'BUREAU_CONFIG': str(self.config), 'BUREAU_ENV_FILE': str(env_file)},
                                capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 24, result.stderr.decode())
        self.assertIn(b'repo.untrusted_git_credentials', result.stderr)
        self.assertNotIn(b'GATE_REACHED', result.stdout)

    def test_inherited_pairs_and_parameters_in_both_builders(self):
        value = "spaces, an apostrophe ' and !"
        parameters = "'credential.helper=parameter-helper' 'credential.https://offline.helper=scoped-helper' " + "'example.value=" + value.replace("'", "'\\''") + "'"
        for mode in ('default', 'clean'):
            for env in self.builders(mode, GIT_CONFIG_COUNT='0001', GIT_CONFIG_KEY_0='credential.helper',
                                     GIT_CONFIG_VALUE_0='inherited-helper', GIT_CONFIG_PARAMETERS=parameters):
                index = '1' if mode == 'default' else '0'
                self.assertEqual(env['GIT_CONFIG_COUNT'], str(int(index) + 1))
                self.assertEqual(env['GIT_CONFIG_KEY_' + index], 'credential.helper')
                self.assertEqual(env['GIT_CONFIG_VALUE_' + index], '')
                self.assertEqual(env['GIT_TERMINAL_PROMPT'], '0')
                self.assertNotIn('GIT_CONFIG_PARAMETERS', env)
                if mode == 'default':
                    self.assertEqual(env['GIT_CONFIG_VALUE_0'], 'inherited-helper')
                else:
                    self.assertNotIn('GIT_CONFIG_KEY_1', env)
            for env in self.builders(mode, 'keep', GIT_CONFIG_PARAMETERS=parameters):
                if mode == 'default': self.assertEqual(env['GIT_CONFIG_PARAMETERS'], parameters)
                else: self.assertNotIn('GIT_CONFIG_PARAMETERS', env)

    def test_real_git_resets_inherited_includes_in_both_builders(self):
        helper = self.root / 'helper'
        helper.write_text('#!/bin/sh\ncat >/dev/null\nprintf "username=fixture\\npassword=stored-fixture\\n"\n')
        helper.chmod(0o755)
        include = self.root / 'included.config'
        subprocess.run(['git', 'config', '--file', str(include), 'credential.helper', '!' + shlex.quote(str(helper))],
                       env=self.env, check=True, timeout=10)
        sources = ({'GIT_CONFIG_PARAMETERS': shlex.quote('include.path=' + str(include))},
                   {'GIT_CONFIG_COUNT': '1', 'GIT_CONFIG_KEY_0': 'include.path', 'GIT_CONFIG_VALUE_0': str(include)})
        for inherited in sources:
            for mode in ('default', 'clean'):
                for setting in (None, 'off', 'keep'):
                    with self.subTest(source=next(iter(inherited)), mode=mode, setting=setting):
                        for env in self.builders(mode, setting, **inherited):
                            if mode == 'default' and 'GIT_CONFIG_COUNT' in inherited:
                                self.assertEqual(env['GIT_CONFIG_KEY_0'], 'include.path')
                                self.assertEqual(env['GIT_CONFIG_VALUE_0'], str(include))
                                self.assertEqual(env['GIT_CONFIG_COUNT'], '1' if setting == 'keep' else '2')
                            env = {**env, 'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_GLOBAL': os.devnull,
                                   'GIT_TERMINAL_PROMPT': '0'}
                            result = subprocess.run(['git', '-C', str(self.root), 'credential', 'fill'], env=env,
                                                    input='protocol=https\nhost=offline\n\n', capture_output=True, text=True, timeout=10)
                            available = mode == 'default' and setting == 'keep'
                            self.assertEqual(result.returncode, 0 if available else 128, result.stderr)
                            self.assertEqual('password=stored-fixture' in result.stdout, available)

    def test_real_git_resets_system_global_and_repository_helpers(self):
        helper = self.root / 'helper'
        helper.write_text('#!/bin/sh\ncat >/dev/null\nprintf "username=fixture\\npassword=stored-fixture\\n"\n')
        helper.chmod(0o755)
        configs = [self.root / 'system.config', self.root / '.gitconfig', self.root / '.git/config']
        for path in configs:
            subprocess.run(['git', 'config', '--file', str(path), 'credential.helper', '!' + shlex.quote(str(helper))],
                           env=self.env, check=True, timeout=10)
        baseline = {**self.env, 'GIT_CONFIG_SYSTEM': str(configs[0]), 'GIT_CONFIG_GLOBAL': str(configs[1]),
                    'GIT_CONFIG_NOSYSTEM': '0'}
        origins = subprocess.run(['git', '-C', str(self.root), 'config', '--show-origin', '--get-all', 'credential.helper'],
                                 env=baseline, capture_output=True, text=True, check=True, timeout=10).stdout
        for path in configs: self.assertIn('file:' + str(path.relative_to(self.root) if path == configs[2] else path), origins)
        for mode in ('default', 'clean'):
            for setting in (None, 'off', 'keep'):
                for env in self.builders(mode, setting):
                    # Reintroduce only fixture config paths so clean mode also reads all three scopes.
                    env = {**env, 'GIT_CONFIG_SYSTEM': str(configs[0]), 'GIT_CONFIG_GLOBAL': str(configs[1]),
                           'GIT_CONFIG_NOSYSTEM': '0'}
                    result = subprocess.run(['git', '-C', str(self.root), 'credential', 'fill'], env=env,
                                            input='protocol=https\nhost=offline\n\n', capture_output=True, text=True, timeout=10)
                    self.assertEqual(result.returncode, 0 if setting == 'keep' else 128)
                    self.assertEqual('password=stored-fixture' in result.stdout, setting == 'keep')
        # Bureau's own environment and shell git wrapper still use the stored helper.
        self.assertEqual(p.process_env(['git', 'credential', 'fill'], self.env), self.env)
        self.configure('default')
        result = self.shell(self.env, command="printf 'protocol=https\\nhost=offline\\n\\n' | git -C \"$HOME\" credential fill")
        self.assertIn(b'password=stored-fixture', result.stdout)

    def test_invalid_shell_settings_refuse_including_check(self):
        for mode in ('default', 'clean'):
            for setting in ('', 'OFF', True, 1, ['off'], {'mode': 'off'}):
                self.configure(mode, setting)
                for command in ('bureau_untrusted_env /usr/bin/env', 'bureau_untrusted_env --check'):
                    result = self.shell(self.env, code=24, command=command)
                    self.assertIn(b'repo.untrusted_git_credentials', result.stderr)
                    self.assertEqual(result.stdout, b'')
        for mode in ('default', 'clean'):
            inherited = {'GIT_CONFIG_COUNT': 'malformed', 'GIT_CONFIG_KEY_9': 'example.value',
                         'GIT_CONFIG_VALUE_9': 'inherited-entry', 'GIT_TERMINAL_PROMPT': '1'}
            for env in self.builders(mode, 'keep', **inherited):
                self.assertEqual({k:v for k,v in env.items() if k.startswith(('GIT_CONFIG_KEY_', 'GIT_CONFIG_VALUE_'))
                                  or k in GIT_OFF}, inherited if mode == 'default' else {})


if __name__ == '__main__':
    unittest.main(verbosity=1)
