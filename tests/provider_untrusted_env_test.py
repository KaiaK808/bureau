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
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

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
OPERATOR = {'OPERATOR_TOOL_VAR': 'keep_me_operator_var_0008'}
AGENT = {'ANTHROPIC_API_KEY': 'sk-ant-PROBE-agent-login', 'CLAUDE_CODE_OAUTH_TOKEN': 'PROBE_claude_oauth',
         'OPENAI_API_KEY': 'sk-openai-PROBE-agent-login', 'CODEX_HOME': '/tmp/codex-home-probe',
         'HTTPS_PROXY': 'http://proxy.invalid:3128'}

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
        for name in ('claude', 'codex'):
            path = self.bin / name; path.write_text(FAKE); path.chmod(0o755)
        self.config = self.root / 'config.json'
        self.prompt = self.root / 'prompt.txt'; self.prompt.write_text('fixture prompt')
        base = {k: v for k, v in os.environ.items() if not k.startswith(('BUREAU_RUNNER_', 'BUREAU_MODEL_', 'BUREAU_CODEX_MODEL_', 'BUREAU_STAGE_TIMEOUT'))}
        for name in list(base):
            if name in SECRETS or name in AGENT or name.endswith('_PROXY') or name.endswith('_proxy'): base.pop(name)
        self.env = {**base, **SECRETS, **ALIASES, **OPERATOR, **AGENT, 'BASH_ENV': '/dev/null', 'ENV': '/dev/null',
                    'PATH': str(self.bin) + os.pathsep + os.environ['PATH'],
                    'BUREAU_PROVIDER_LOG_DIR': str(self.root / 'evidence')}

    def provider(self, runner, untrusted=None, *extra, code=0, env=None):
        config = {'agents': {'runner': runner}}
        if untrusted is not None: config['repo'] = {'untrusted_env': untrusted}
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
                        # Everything else stays: the operator's tool variables and the agent login.
                        for name, value in {**OPERATOR, **AGENT}.items():
                            self.assertEqual(seen.get(name), value, runner + ' ' + phase + ' lost ' + name)
                        self.assertEqual(seen['PATH'], self.env['PATH'])

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
                    allowed = set(p.UNTRUSTED_KEEP) | set(p.AGENT_KEEP)
                    stray = [n for n in seen if n not in allowed and not n.startswith(p.AGENT_KEEP_PREFIXES[runner])
                             and not n.startswith('__CF') and n not in ('LC_CTYPE',)]
                    self.assertEqual(stray, [], runner + ' ' + phase + ' got variables outside the clean list')
                    self.assertEqual(seen['PATH'], self.env['PATH'])

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
        # A secret of 8 characters or more goes wherever it appears inside a value; a shorter one
        # only as the whole value (CI=1234567 goes with a chat id 1234567, x1234567 stays).
        short = p.untrusted_env({'TELEGRAM_ALERT_CHAT_ID': '1234567', 'CI': '1234567', 'Z': 'x1234567',
                                 'GH_TOKEN': '12345678', 'X': '12345678', 'Y': 'Bearer 12345678 end',
                                 'BASH_ENV': '/tmp/env.sh', 'ENV': '/tmp/env.sh', 'PATH': '/bin'}, 'default')
        self.assertEqual(short, {'Z': 'x1234567', 'PATH': '/bin'})

    def test_lists_are_pinned(self):
        # Literal sets: widening the clean list (an SSH agent, a cloud key) or shortening the
        # removal list fails here on any runner, whatever its own environment holds.
        self.assertEqual(p.UNTRUSTED_REMOVE, ('LINEAR_API_KEY', 'TELEGRAM_BOT_TOKEN', 'TELEGRAM_ALERT_CHAT_ID',
                                              'GH_TOKEN', 'GITHUB_TOKEN', 'GH_ENTERPRISE_TOKEN', 'GITHUB_ENTERPRISE_TOKEN'))
        self.assertEqual(p.UNTRUSTED_STARTUP, ('BASH_ENV', 'ENV'))
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
        self.assertEqual(p.untrusted_env(environ, 'default'), {'PATH': '/bin', 'OTHER': 'v0-and-more'})

    def test_control_an_inherited_environment_shows_every_secret(self):
        # The v3.0.2 adapter started the agent with the inherited environment. Same fakes,
        # started that way: every probe is there, so the assertions above are not vacuous.
        subprocess.run([str(self.bin / 'claude'), 'auth', 'status'], env={**self.env, 'PR1_FAKE_ROOT': str(self.root)},
                       capture_output=True, check=True, timeout=30)
        seen = self.seen('claude', 'auth')
        for name, value in {**SECRETS, **ALIASES}.items(): self.assertEqual(seen.get(name), value)


if __name__ == '__main__':
    unittest.main(verbosity=1)
