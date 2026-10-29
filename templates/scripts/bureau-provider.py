#!/usr/bin/env python3
"""Provider adapter: bounded subprocess, stdin prompts, separate evidence and results."""
import argparse
import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import uuid

sys.dont_write_bytecode = True
_evidence_spec = importlib.util.spec_from_file_location('bureau_evidence', Path(__file__).resolve().with_name('bureau-evidence.py'))
e = importlib.util.module_from_spec(_evidence_spec)
_evidence_spec.loader.exec_module(e)
_gate_spec = importlib.util.spec_from_file_location('bureau_gates', Path(__file__).resolve().with_name('bureau-gates.py'))
g = importlib.util.module_from_spec(_gate_spec)
_gate_spec.loader.exec_module(g)
_activity_spec = importlib.util.spec_from_file_location('bureau_activity', Path(__file__).resolve().with_name('bureau-activity.py'))
a = importlib.util.module_from_spec(_activity_spec)
_activity_spec.loader.exec_module(a)
_ticket_spec = importlib.util.spec_from_file_location('bureau_ticket', Path(__file__).resolve().with_name('bureau-ticket.py'))
t = importlib.util.module_from_spec(_ticket_spec)
_ticket_spec.loader.exec_module(t)


# The values the Claude CLI's --effort accepts.
CLAUDE_EFFORTS = ('low', 'medium', 'high', 'xhigh', 'max')


def configuration(stage, config, env):
    if not isinstance(config, dict) or not isinstance(config.get('agents', {}), dict): raise ValueError('configuration must contain an agents object')
    agents = config.get('agents', {})
    item = agents.get(stage, {})
    item = item if isinstance(item, dict) else {}
    upper = stage.upper()
    runner = env.get('BUREAU_RUNNER_' + upper) or item.get('runner') or agents.get('runner', 'claude')
    if runner not in ('claude', 'codex'):
        raise ValueError('unknown runner: ' + str(runner))
    providers = agents.get('providers', {})
    if not isinstance(providers, dict) or not isinstance(providers.get(runner, {}), dict): raise ValueError('providers and provider entries must be objects')
    if not isinstance(config.get('session', {}), dict): raise ValueError('session must be an object')
    provider = providers.get(runner, {})
    # Legacy routing can ignore generic Claude defaults for Codex, but malformed
    # model fields must still fail validation before any provider invocation.
    for configured_model in (item.get('model'), agents.get('model'), provider.get('model')):
        if configured_model is not None and not isinstance(configured_model, str):
            raise ValueError('model must be a string')
    # Before v2 all generic model fields belonged to Claude, even when a
    # stage/default runner or environment override selected Codex. Retain that
    # meaning on resync and on migrations carrying the compatibility marker.
    compatibility = agents.get('model_compatibility', 'v1' if config.get('version', 1) == 1 else 'v2')
    if compatibility not in ('v1', 'v2'):
        raise ValueError('model_compatibility must be v1 or v2')
    legacy_codex = runner == 'codex' and compatibility == 'v1'
    # In v2 an explicitly assigned stage model belongs to that stage's runner.
    # A generic default belongs only to the configured default runner.
    default_model = agents.get('model') if runner == agents.get('runner', 'claude') else None
    model = (env.get('BUREAU_' + runner.upper() + '_MODEL_' + upper)
             or (env.get('BUREAU_MODEL_' + upper) if not legacy_codex else None)
             or (item.get('model') if not legacy_codex else None) or provider.get('model')
             or (default_model if not legacy_codex else None) or env.get('BUREAU_' + runner.upper() + '_MODEL_DEFAULT')
             or (env.get('BUREAU_MODEL_DEFAULT') if runner == 'claude' else None))
    if model is not None and not isinstance(model, str): raise ValueError('model must be a string')
    sandbox = env.get('BUREAU_SANDBOX_' + upper) or item.get('sandbox') or provider.get('sandbox') or ('read-only' if stage in ('code_review', 'research', 'upstream_summary') else 'workspace-write')
    if sandbox not in ('read-only', 'workspace-write'):
        raise ValueError('sandbox must be read-only or workspace-write; Bureau does not disable sandboxing')
    reasoning = env.get('BUREAU_REASONING_' + upper) or item.get('reasoning_effort') or provider.get('reasoning_effort')
    if reasoning and reasoning not in ('none', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max', 'ultra'):
        raise ValueError('invalid reasoning_effort')
    # The Claude CLI only warns about an --effort value it does not know and
    # runs with its own default, so the values only Codex knows fail here.
    if runner == 'claude' and reasoning and reasoning not in CLAUDE_EFFORTS:
        raise ValueError('reasoning_effort for runner claude must be one of ' + ', '.join(CLAUDE_EFFORTS))
    timeout = float(env.get('BUREAU_STAGE_TIMEOUT') or item.get('timeout_seconds') or provider.get('timeout_seconds', 3600))
    if not math.isfinite(timeout) or timeout <= 0 or timeout > 86400: raise ValueError('timeout_seconds must be within (0, 86400]')
    def silence_setting(name, default):
        value = item.get(name, provider.get(name, default))
        if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 86400:
            raise ValueError(name + ' must be a finite number within [0, 86400]')
        return value
    silence = silence_setting('silence_timeout_seconds', 0)
    silence_child = silence_setting('silence_child_timeout_seconds', silence)
    if silence_child < silence: raise ValueError('silence_child_timeout_seconds must not be shorter than silence_timeout_seconds')
    options = dict(stage=stage, runner=runner, model=model, sandbox=sandbox, reasoning=reasoning, timeout=timeout,
                   silence_timeout_seconds=silence, silence_child_timeout_seconds=silence_child,
                   headroom=runner == 'claude' and env.get('BUREAU_HEADROOM_WRAP', str(agents.get('headroom_wrap', False))).lower() in ('1', 'true'),
                   cost_tracking=env.get('BUREAU_COST_TRACKING', str(config.get('session', {}).get('cost_tracking', False))).lower() in ('1', 'true'))
    if runner == 'claude': options['config_dir'] = claude_config_dir(provider, env)
    return options


def claude_config_dir(provider, env):
    # The Claude configuration directory of this installation's Claude stages:
    # BUREAU_CLAUDE_CONFIG_DIR, else agents.providers.claude.config_dir, else
    # None (the child keeps the CLAUDE_CONFIG_DIR it inherits, or the CLI's
    # ~/.claude). An absolute path to an existing directory; a leading ~/ is
    # the home directory. Anything else fails before any provider call.
    value, name = env.get('BUREAU_CLAUDE_CONFIG_DIR'), 'BUREAU_CLAUDE_CONFIG_DIR'
    if not value: value, name = provider.get('config_dir'), 'agents.providers.claude.config_dir'
    if value is None: return None
    if not isinstance(value, str): raise ValueError(name + ' must be a string')
    if value.startswith('~/'): value = os.path.join(env.get('HOME') or os.path.expanduser('~'), value[2:])
    if not os.path.isabs(value): raise ValueError(name + ' must be an absolute path or start with ~/ (read: ' + json.dumps(value) + ')')
    if not os.path.isdir(value): raise ValueError(name + ' is not an existing directory: ' + value)
    return os.path.normpath(value)


def claude_env(options, env):
    # The environment of a claude process: CLAUDE_CONFIG_DIR is the configured
    # directory when there is one; without one `env` is returned unchanged
    # (None, as subprocess takes it, is the adapter's own environment).
    if options.get('runner') == 'claude' and options.get('config_dir'):
        return {**(os.environ if env is None else env), 'CLAUDE_CONFIG_DIR': options['config_dir']}
    return env


class AuthError(Exception):
    pass


# Untrusted code runs without Bureau secrets. The agent runs the branch's
# tests (Claude with --dangerously-skip-permissions) and loads the branch's own
# agent settings, so it starts in the environment bureau_untrusted_env in
# bureau-env.sh gives every other command the branch controls. Keep
# UNTRUSTED_REMOVE, UNTRUSTED_STARTUP, UNTRUSTED_PATHS, UNTRUSTED_HOST_PREFIXES
# and UNTRUSTED_KEEP equal to the lists there; tests/test_untrusted_env.sh
# compares the two implementations and tests/provider_untrusted_env_test.py pins these tuples.
UNTRUSTED_REMOVE = ('LINEAR_API_KEY', 'TELEGRAM_BOT_TOKEN', 'TELEGRAM_ALERT_CHAT_ID',
                    'GH_TOKEN', 'GITHUB_TOKEN', 'GH_ENTERPRISE_TOKEN', 'GITHUB_ENTERPRISE_TOKEN')
# A bash started by the agent sources the file BASH_ENV (or, as sh, ENV) names;
# pointed at .env it would read the keys back in.
UNTRUSTED_STARTUP = ('BASH_ENV', 'ENV')
# Where the operator's .env and .bureau.json are: no secret, but the absolute
# path to the file that holds them. Bureau's own scripts read both; nothing the
# branch controls needs them (until v3.3.0 the default mode passed them on).
UNTRUSTED_PATHS = ('BUREAU_ENV_FILE', 'BUREAU_CONFIG')
# A headless child must not claim the operator's terminal-multiplexer pane.
UNTRUSTED_HOST_PREFIXES = ('HERDR_',)
# A secret this long counts wherever it appears inside another value; a shorter
# one only as the whole value (inside matching would take out every variable
# that happens to contain two or three common characters).
COPY_MIN = 6
# git subcommands that talk to a remote keep the GitHub token variables their
# credential helper may read (bureau-env.sh git()); gh keeps them too.
REMOTE_GIT = ('push', 'fetch', 'pull', 'ls-remote', 'clone', 'remote', 'submodule')
GIT_VALUE_OPTIONS = ('-C', '-c', '--git-dir', '--work-tree', '--namespace', '--super-prefix', '--config-env',
                     '--attr-source', '--shallow-file')
UNTRUSTED_KEEP = ('PATH', 'HOME', 'USER', 'LOGNAME', 'SHELL', 'TMPDIR', 'TEMP', 'TMP',
                  'LANG', 'LC_ALL', 'LC_CTYPE', 'TERM', 'TZ', 'CI')
# Under "clean" the agent CLI also keeps its own login and network settings;
# without them it cannot authenticate or reach its API. Cloud-provider logins
# (Bedrock, Vertex) are not kept: stay on the default for those.
AGENT_KEEP_PREFIXES = {'claude': ('ANTHROPIC_', 'CLAUDE_', 'HEADROOM_'), 'codex': ('OPENAI_', 'CODEX_')}
AGENT_KEEP = ('HTTP_PROXY', 'HTTPS_PROXY', 'NO_PROXY', 'ALL_PROXY', 'http_proxy', 'https_proxy', 'no_proxy',
              'all_proxy', 'SSL_CERT_FILE', 'SSL_CERT_DIR', 'NODE_EXTRA_CA_CERTS', 'XDG_CONFIG_HOME')


class UntrustedEnvError(Exception):
    pass


def _carries(environ, names):
    # The test "carries one of these secrets' values": a value of COPY_MIN
    # characters or more anywhere inside, a shorter one as the whole value.
    values = [environ.get(name, '') for name in names]
    long_secrets = [value for value in values if len(value) >= COPY_MIN]
    short_secrets = {value for value in values if 0 < len(value) < COPY_MIN}
    return lambda value: value in short_secrets or any(secret in value for secret in long_secrets)


def git_subcommand(args):
    skip = False
    for arg in args:
        if skip: skip = False; continue
        if arg in GIT_VALUE_OPTIONS: skip = True
        elif not arg.startswith('-'): return arg
    return ''


def process_env(command, environ=None):
    # The environment for a git or gh process Bureau starts itself from Python
    # (bureau-doctor.py uses this one; bureau-supervision.py and
    # bureau-runtime.py carry a copy so they need no other file): the same as
    # the git() and gh() functions in bureau-env.sh give the shell's. Hooks,
    # filters, an fsmonitor or a credential helper such a process runs can come
    # from the branch. Always without the .env keys, their copies, BASH_ENV and
    # ENV or host pane variables; without the GitHub token variables too,
    # except for gh and for git commands that talk to a remote.
    environ = os.environ if environ is None else environ
    name = os.path.basename(command[0]) if command else ''
    remote = name == 'gh' or (name == 'git' and git_subcommand(command[1:]) in REMOTE_GIT)
    names = UNTRUSTED_REMOVE[:3] if remote else UNTRUSTED_REMOVE
    carries = _carries(environ, names)
    return {key: value for key, value in environ.items()
            if key not in names and key not in UNTRUSTED_STARTUP
            and not key.startswith(UNTRUSTED_HOST_PREFIXES) and not carries(value)}


def untrusted_env_mode(config):
    # Same reading as _bureau_untrusted_env_mode in bureau-env.sh: absent or
    # null is "default"; anything but "default" or "clean" refuses (24).
    repo = config.get('repo')
    if repo is None: return 'default'
    if not isinstance(repo, dict): raise UntrustedEnvError('repo must be an object')
    mode = repo.get('untrusted_env')
    if mode is None: return 'default'
    if isinstance(mode, str) and mode in ('default', 'clean'): return mode
    raise UntrustedEnvError('repo.untrusted_env must be absent, "default" or "clean" (read: ' + json.dumps(mode)
                            + '); the agent was not started (24, environment-blocked)')


def untrusted_env(environ, mode, runner=None):
    # The environment for code the branch controls. "default": everything but
    # the listed secrets, BASH_ENV and ENV, BUREAU_ENV_FILE and BUREAU_CONFIG,
    # and any variable that carries one of the secrets' values (_carries). "clean": only UNTRUSTED_KEEP, plus, for an
    # agent, its own login and network variables, again without any value that
    # carries a secret. Both modes drop host pane variables, even if an agent
    # keep prefix overlaps. The caller's environment is never changed.
    carries = _carries(environ, UNTRUSTED_REMOVE)
    if mode == 'clean':
        prefixes = AGENT_KEEP_PREFIXES.get(runner, ())
        def keep(name):
            return name in UNTRUSTED_KEEP or (runner is not None and (name in AGENT_KEEP or name.startswith(prefixes)))
    elif mode == 'default':
        def keep(name):
            return name not in UNTRUSTED_REMOVE and name not in UNTRUSTED_STARTUP and name not in UNTRUSTED_PATHS
    else:
        raise UntrustedEnvError('unknown untrusted environment mode')
    return {name: value for name, value in environ.items()
            if not name.startswith(UNTRUSTED_HOST_PREFIXES) and keep(name) and not carries(value)}


def auth(options):
    runner = options['runner']
    if not shutil.which(runner): raise AuthError(runner + ' executable is missing')
    cmd = ['claude', 'auth', 'status', '--json'] if runner == 'claude' else ['codex', 'login', 'status']
    # The login check runs in the environment the agent will get, so a
    # "clean" mode that drops the agent's login fails here, with 16.
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=20,
                          env=claude_env(options, env=untrusted_env(os.environ, options.get('untrusted_env', 'default'), runner)))
    if proc.returncode != 0: raise AuthError(runner + ' is not authenticated')
    if runner == 'claude':
        try: logged = json.loads(proc.stdout).get('loggedIn') is True
        except (ValueError, AttributeError): logged = False
        if not logged: raise AuthError('Claude auth status did not confirm login')


def structured(text):
    try:
        value = json.loads(text)
        if isinstance(value, dict): return value
    except ValueError: pass
    blocks = re.findall(r'^```json\s*\n(.*?)^```\s*$', text, re.M | re.S)
    if blocks:
        try:
            value = json.loads(blocks[-1])
            if isinstance(value, dict): return value
        except ValueError: pass
    return None


def sandbox_gate_only(value):
    # Same reading as codex_sandbox_gate_only in bureau-config.sh: at least one
    # notes.needs_human entry, and every reason a string beginning with
    # SANDBOX_GATE: (tests the Codex sandbox denies, nothing else).
    notes = value.get('notes') if isinstance(value, dict) else None
    items = notes.get('needs_human') if isinstance(notes, dict) else None
    if not isinstance(items, list) or not items: return False
    return all(isinstance(item, dict) and 'kind' not in item and 'command' not in item and isinstance(item.get('reason'), str)
               and item['reason'].startswith('SANDBOX_GATE:') for item in items)


def qa_sandbox_gate_only(value):
    # QA has no needs-human array: coverage_notes carries the sandbox diagnosis.
    return (isinstance(value, dict) and value.get('status') == 'NEEDS_HUMAN'
            and 'blockers' not in value
            and isinstance(value.get('coverage_notes'), str)
            and value['coverage_notes'].startswith('SANDBOX_GATE:'))


def environment_blocked(value, text, runner, stage):
    # A NEEDS_HUMAN that names a denied operation ends the stage with 24. Codex
    # implement and QA sandbox-only results go on to their shell gates, where
    # the suite outside the sandbox decides. Claude is read as before. Only the
    # implement schema has a notes.needs_human list, so that exception is scoped
    # to implement on purpose; QA carries its diagnosis in coverage_notes.
    if not value or value.get('status') != 'NEEDS_HUMAN': return False
    if runner == 'codex' and stage == 'implement' and sandbox_gate_only(value): return False
    if runner == 'codex' and stage == 'qa' and qa_sandbox_gate_only(value): return False
    return bool(re.search(r'operation not permitted|permission denied|sandbox denied', text, re.I))


def codex_tmpdir(runner, environ, repo):
    # Without TMPDIR, git on macOS asks for the per-user temp directory, which
    # the Codex sandbox can deny. Only the child environment gets a directory
    # of its own; run() allocates after installing stop handlers and removes
    # it after the child finishes. An explicit resolved /tmp keeps TEMP or TMP
    # from moving it into the worktree, where a commit could pick it up.
    if runner != 'codex' or environ.get('TMPDIR'): return None
    base = Path('/tmp').resolve()
    if base == Path(repo).resolve() or Path(repo).resolve() in base.parents: return None
    path = tempfile.mkdtemp(prefix='bureau-codex-', dir=str(base))
    environ['TMPDIR'] = path
    return path


def validate(value, schema):
    # Small schema validator for the adapter's object/array/scalar contracts.
    kind = schema.get('type')
    checks = {'object': lambda x:isinstance(x, dict), 'array': lambda x:isinstance(x, list),
              'string': lambda x:isinstance(x, str), 'integer': lambda x:type(x) is int,
              'number': lambda x:type(x) in (int,float), 'boolean':lambda x:type(x) is bool,
              'null': lambda x:x is None}
    if isinstance(kind, list):
        if not any(checks.get(k, lambda _:False)(value) for k in kind):
            raise ValueError('result type must be one of ' + ', '.join(kind))
    elif kind in checks and not checks[kind](value): raise ValueError('result type must be ' + kind)
    if 'enum' in schema and value not in schema['enum']: raise ValueError('unrecognized result value')
    if isinstance(value, dict):
        if any(k not in value for k in schema.get('required', [])): raise ValueError('result missing required fields')
        props = schema.get('properties', {})
        if schema.get('additionalProperties') is False and set(value) - set(props): raise ValueError('unexpected result fields')
        for k, v in value.items():
            if k in props: validate(v, props[k])
    if isinstance(value, list):
        if len(value) > schema.get('maxItems', len(value)): raise ValueError('result array too long')
        for v in value: validate(v, schema.get('items', {}))
    if isinstance(value, str) and len(value) > schema.get('maxLength', len(value)):
        raise ValueError('result string too long')


def provider_schema(schema, evidence):
    # Claude's insight contract requires an array. Codex uses g.codex_schema
    # instead, making every optional field required/nullable recursively.
    # Local validation keeps insights optional for legacy results and recovery.
    contract = json.loads(schema.read_text())
    if 'insights' not in contract.get('properties', {}): return schema
    contract['required'] = list(dict.fromkeys(contract.get('required', []) + ['insights']))
    path = evidence / 'provider-schema.json'
    path.write_text(json.dumps(contract))
    path.chmod(0o600)
    return path


def evidence_store(repo):
    command = ['git', '-C', str(repo), 'rev-parse', '--git-common-dir']
    common = subprocess.check_output(command, text=True, env=process_env(command)).strip()
    return e.Store((repo / common).resolve() / 'bureau')


def current_head(repo):
    command = ['git', '-C', str(repo), 'rev-parse', '--verify', 'HEAD']
    result = subprocess.run(command, text=True, capture_output=True, env=process_env(command))
    return result.stdout.strip() if result.returncode == 0 else None


def run(options, prompt, system, repo, evidence, schema=None):
    """Persist the attempt's normalized outcome before handing anything to main."""
    options.pop('_provider_exit', None)
    options.pop('_receipt_metadata', None)
    options.pop('_provider_cancelled', None)
    store = e.observe(evidence_store, repo)
    run_id = options.setdefault('evidence_run_id', os.environ.get('BUREAU_RUN_ID') or uuid.uuid4().hex)
    issue = os.environ.get('BUREAU_CURRENT_ISSUE') or None
    start = e.observe(current_head, repo); started = time.time()
    if store is not None: e.observe(store.begin_run, run_id, issue, repo, start)
    stage_attempt = os.environ.get('BUREAU_STAGE_ATTEMPT_ID')
    standalone = not stage_attempt
    phase = options.get('phase') or options['stage']
    if standalone and store is not None:
        stage_attempt = e.observe(store.begin_attempt, run_id, issue, options['stage'], phase, repo, start) or uuid.uuid4().hex
    stage_attempt = stage_attempt or uuid.uuid4().hex
    attempt = uuid.uuid4().hex
    identity = dict(run_id=run_id, provider_attempt_id=attempt, stage_attempt_id=stage_attempt,
                    issue=issue, stage=e.tag(options['stage']), phase=e.tag(phase),
                    schema_sha256=hashlib.sha256(schema.read_bytes()).hexdigest() if schema else None)
    ticket = None
    if issue:
        try:
            ticket = t.Ledger(t.root_for(repo), issue, options['_ticket_config'] if '_ticket_config' in options else t.read_config(repo))
            stop = ticket.call('begin', identity, options['runner'])
        except (OSError, ValueError, TypeError, KeyError) as exc:
            raise PermissionError('ticket accounting unavailable: ' + str(exc)) from exc
        if stop:
            if standalone and store is not None:
                e.observe(store.end_attempt, run_id, stage_attempt, stop['code'], start, outer=not os.environ.get('BUREAU_RUN_ID'))
            return stop['code'], '', dict(outcome=stop['terminal_reason'], **identity, **stop)
    observer = None
    if options['runner'] == 'codex' and options['stage'] in ('implement', 'qa'):
        observer = g.Observer(repo, options.get('gate_commands', []), lambda: current_head(repo), options.get('gate_command_id', 'repo.test_command'))
    options['_gate_observer'] = observer
    options['_gate_start_head'] = start
    error = None
    try:
        code, text, metadata = _run(options, prompt, system, repo, evidence, schema)
    except (OSError, ValueError, TypeError, UntrustedEnvError, subprocess.SubprocessError) as exc:
        if options.get('_provider_cancelled'):
            code, text, metadata = 130, '', dict(outcome='cancelled')
        else:
            error = exc
            code, text = (24 if isinstance(exc, (PermissionError, UntrustedEnvError)) else 22), ''
            metadata = dict(outcome='invalid-result', error=str(exc))
    if ticket is not None:
        try:
            usage = t.reported_usage(options['runner'], evidence)
            ticket.call('finish', attempt, usage, code)
            reason = 'watchdog' if metadata.get('timeout_kind') == 'silence' else ('timeout' if code == 124 else None)
            if reason: ticket.call('note_stop', reason, code, run_id)
        except (OSError, ValueError, TypeError, KeyError) as exc:
            if code == 130:
                note('Bureau ticket accounting unavailable after cancellation; usage remains unknown')
            else:
                raise PermissionError('ticket accounting unavailable: ' + str(exc)) from exc
    end = e.observe(current_head, repo)
    gate_attempts = []
    if observer and not observer.error:
        gate_attempts = e.observe(observer.records, (e.observe(transcript, options, repo, evidence) or {}).get('transcript')) or []
    result_text = json.loads(text)['result'] if code == 0 and metadata.get('cost_envelope') else text
    result = structured(result_text) if code == 0 and schema else None
    if result is not None: validate(result, json.loads(schema.read_text()))
    receipt = dict(schema_version=e.VERSION, record_type='receipt', **identity,
                   provider=options['runner'], provider_exit=metadata.get('provider_exit', options.get('_provider_exit')),
                   normalized_exit=code, outcome=metadata['outcome'], structured_result=result,
                   insights_result=e.normalize_insights(structured(result_text)) if code == 0 and not schema else None,
                   result_absence=None if result is not None else ('no-schema' if code == 0 else metadata['outcome']),
                   start_head=start, end_head=end, started_at=started, ended_at=time.time(),
                   provider_logs=str(evidence), transcript=(e.observe(transcript, options, repo, evidence) or {}).get('transcript'),
                   gate_attempts=gate_attempts, usage=metadata.get('usage'), total_cost_usd=metadata.get('total_cost_usd'))
    receipt['timeout_kind'] = metadata.get('timeout_kind')
    if options['stage'] == 'code_review':
        receipt['review_min_severity'] = os.environ.get('BUREAU_REVIEW_MIN_SEVERITY')
        receipt['review_inputs'] = dict(head=os.environ.get('BUREAU_REVIEW_INPUT_HEAD'),
                                      base=os.environ.get('BUREAU_REVIEW_INPUT_BASE'),
                                      validation_head=os.environ.get('BUREAU_REVIEW_VALIDATION_HEAD'))
    # A failed CLI may still have a final advisory observation. Read originals
    # only for optional local capture; never validate/forward/recover a success.
    if code != 0: receipt['insights_result'] = e.observe(receipt_result, receipt)
    # _run reports the actual child exit; no inferred success from commits.
    if code == 0: receipt['provider_exit'] = 0
    if code == 0:
        if store is None: raise OSError('completion receipt store unavailable')
        path = store.put_receipt(receipt)
    else:
        path = e.observe(store.put_receipt, receipt) if store is not None else None
    if standalone and store is not None: e.observe(store.end_attempt, run_id, stage_attempt, code, end, outer=not os.environ.get('BUREAU_RUN_ID'))
    metadata.update(identity, receipt=str(path) if path else None)
    if options['runner'] == 'codex' and options.get('gate_context') and code == 0 and path:
        e.observe(e.atomic, Path(options['gate_context']), {**identity, 'receipt':str(path)})
    options['_receipt_metadata'] = dict(metadata)
    if error: raise error
    return code, text, metadata


def claude_envelope(stdout):
    """One terminal result contract for legacy JSON, streaming and recovery."""
    try: envelope = json.loads(stdout)
    except ValueError:
        envelope = None
        for line in stdout.splitlines():
            try: event = json.loads(line)
            except ValueError: continue
            if isinstance(event, dict) and event.get('type') == 'result': envelope = event
    if not isinstance(envelope, dict): raise ValueError('Claude did not return a JSON envelope')
    if envelope.get('type') not in (None, 'result'): raise ValueError('Claude returned no terminal result envelope')
    return envelope


def receipt_result(receipt, contract=None):
    # Recovery reads the original provider output, then the receipt checks its
    # full structured-result digest. No result prose is copied into the store.
    directory = Path(receipt['provider_logs'])
    try:
        if receipt['provider'] == 'codex':
            value = structured((directory / 'final.txt').read_text())
            if contract is not None: value = g.canonical_result(value, contract)
            return e.normalize_insights(value)
        envelope = claude_envelope((directory / 'stdout.log').read_text())
        value = envelope.get('structured_output')
        if isinstance(value, dict): return e.normalize_insights(value)
        if receipt.get('transcript'):
            found, value = transcript_result(Path(receipt['transcript']))
            if found: return e.normalize_insights(value)
        return e.normalize_insights(structured(envelope.get('result', '')))
    except (OSError, ValueError, TypeError, AttributeError): return None


def completion_text(code, text, metadata, repo, schema):
    """Only this successful attempt can recover a lost forwarding result."""
    if code != 0 or schema is None: return text
    contract = json.loads(schema.read_text())
    try:
        candidate = json.loads(text)['result'] if metadata.get('cost_envelope') else text
        validate(structured(candidate), contract)
        return text
    except (ValueError, TypeError, KeyError):
        def load_result(receipt):
            # The original Codex file uses the nullable wire schema; receipts
            # hash the canonical result with declared optional nulls omitted.
            return receipt_result(receipt, contract)
        result = evidence_store(repo).reconcile(metadata, current_head(repo), contract, validate, load_result)
        if result is None: raise ValueError('no matching completion receipt for this attempt and HEAD')
        metadata['structured_source'] = 'receipt'
        recovered = json.dumps(result)
        if metadata.get('cost_envelope'):
            recovered = json.dumps(dict(result=recovered, usage=metadata.get('usage', {}), total_cost_usd=metadata.get('total_cost_usd'), provider=metadata.get('provider')))
        return recovered


def _run(options, prompt, system, repo, evidence, schema=None):
    runner = options['runner']
    final = evidence / 'final.txt'
    if runner == 'codex':
        command = ['codex', 'exec', '--json', '-C', str(repo), '-s', options['sandbox'], '-o', str(final)]
        if options['reasoning']: command += ['-c', 'model_reasoning_effort=' + json.dumps(options['reasoning'])]
        if schema:
            contract = json.loads(schema.read_text())
            wire = g.codex_schema(contract)
            wire_path = schema
            if wire != contract:
                wire_path = evidence / 'codex-schema.json'
                wire_path.write_text(json.dumps(wire))
                prompt += '\nOptional Bureau result fields are required but nullable in the Codex wire schema. Use null to omit an unused optional field, including kind/command for the legacy SANDBOX_GATE form or QA blockers when using that legacy form. Declare every real blocker; never omit a product, security or human-decision hold.'
            command += ['--output-schema', str(wire_path)]
        # Codex edits files; Git metadata writes remain the shell executor's job.
        prompt = system + '\n\n' + prompt + '\n\nDo not run git commit or git push. Leave changes for the Bureau shell executor. Mark completed tasks truthfully. If tests are denied by the environment, report the permission blocker separately from code failures.'
    else:
        command = ['claude', '-p', '--output-format', 'stream-json' if options.get('silence_timeout_seconds') else 'json']
        if options.get('silence_timeout_seconds'): command += ['--verbose', '--include-partial-messages']
        # The session id names the transcript Claude writes while it works
        # (transcript()); stdout.log gets the JSON envelope only at the end.
        if options.get('session_id'): command += ['--session-id', options['session_id']]
        if options['stage'] == 'upstream_summary': command += ['--tools', '']
        else:
            command += ['--dangerously-skip-permissions', '--disallowedTools',
                        'ScheduleWakeup,CronCreate,CronDelete,RemoteTrigger,Monitor']
        if system:
            (evidence/'system.txt').write_text(system)
            command += ['--append-system-prompt-file', str(evidence/'system.txt')]
        if schema: command += ['--json-schema', provider_schema(schema, evidence).read_text()]
        if options['reasoning']: command += ['--effort', options['reasoning']]
        if options['headroom']:
            if not shutil.which('headroom'): raise ValueError('headroom executable is missing')
            command = ['headroom', 'wrap', 'claude', '--', *command[1:]]
    if options['model']: command += ['--model', options['model']]
    if runner == 'codex': command.append('-')
    interrupted = None
    child = None
    owned = None
    def kill(signum):
        if child and owned:
            owned.scan(); owned.signal(signum)
        elif child:
            try: os.killpg(child.pid, signum)
            except ProcessLookupError: pass
    def stop(signum, frame):
        nonlocal interrupted
        # A hang-up stops the call like SIGTERM, and the provider's process
        # group gets SIGTERM: it runs in a session of its own, without the
        # terminal, where a hang-up can be ignored (bureau-runtime.py execute).
        if signum == signal.SIGHUP: signum = signal.SIGTERM
        interrupted = signum; options['_provider_cancelled'] = True; kill(signum)
        signal.setitimer(signal.ITIMER_REAL, 5)
    previous = {sig: signal.getsignal(sig) for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP, signal.SIGALRM)}
    signal.signal(signal.SIGTERM, stop); signal.signal(signal.SIGINT, stop)
    # Left ignored when the run started under nohup, as in the runtime.
    if signal.getsignal(signal.SIGHUP) != signal.SIG_IGN: signal.signal(signal.SIGHUP, stop)
    signal.signal(signal.SIGALRM, lambda signum, frame: kill(signal.SIGKILL))
    tempdir = None
    try:
        child_env = untrusted_env(os.environ, options.get('untrusted_env', 'default'), runner)
        child_env = claude_env(options, env=child_env)
        tempdir = codex_tmpdir(runner, child_env, repo)
        if options.get('silence_timeout_seconds') or options.get('cancel_file'):
            owned = a.OwnedProcesses()
            child_env[a.OWNER_ENV] = owned.token
        child_env.pop('BUREAU_PROVIDER_CANCEL_FILE', None)
        activity = a.Activity(runner, options.get('silence_timeout_seconds', 0), options.get('silence_child_timeout_seconds', 0))
        started = time.monotonic()
        with (evidence/'prompt.txt').open('w') as out: out.write(prompt)
        with (evidence/'stdout.log').open('wb') as stdout, (evidence/'stderr.log').open('wb') as stderr:
            observer = options.get('_gate_observer')
            child = subprocess.Popen(command, cwd=repo, stdin=subprocess.PIPE, stdout=subprocess.PIPE if observer or owned else stdout, stderr=stderr, start_new_session=True,
                                     env=child_env)
            if owned: owned.attach(child.pid)
            reader = None
            stream_error = []
            if observer or owned:
                pipe = child.stdout
                # communicate owns stdin and waiting; this reader alone owns
                # stdout, preserving the actual JSONL and sampling execution HEADs.
                child.stdout = None
                def read_events():
                    pending = b''
                    def fanout(line):
                        if observer:
                            try: observer.line(line.decode(errors='replace'))
                            except Exception: observer.error = 'execution evidence unavailable'
                        if owned: activity.line(line)
                    try:
                        while True:
                            chunk = os.read(pipe.fileno(), 65536)
                            if not chunk: break
                            stdout.write(chunk); stdout.flush()
                            pending += chunk
                            while b'\n' in pending:
                                line, pending = pending.split(b'\n', 1)
                                fanout(line)
                        if pending: fanout(pending)
                    except (OSError, ValueError, TypeError) as exc:
                        stream_error.append(str(exc))
                        if observer: observer.error = 'execution stream unavailable'
                    finally: pipe.close()
                reader = threading.Thread(target=read_events, daemon=True)
                reader.start()
            if interrupted: kill(interrupted)
            timed_out = False
            timeout_kind = None
            try:
                input_bytes = prompt.encode()
                while True:
                    try:
                        child.communicate(input_bytes, timeout=min(.1, options['timeout']) if owned else options['timeout'])
                        break
                    except subprocess.TimeoutExpired:
                        input_bytes = None
                        if not interrupted and options.get('cancel_file') and Path(options['cancel_file']).exists():
                            # Let the review parent distinguish its own sibling
                            # shutdown from an independent signal (which wins).
                            if options.get('phase') in ('correctness', 'security', 'performance'):
                                e.observe(e.atomic, Path(options['cancel_file'] + '.' + options['phase']), dict(cancelled=True))
                            stop(signal.SIGTERM, None)
                        if interrupted: break
                        if time.monotonic() - started >= options['timeout']:
                            timed_out = True; timeout_kind = 'wall-clock'; break
                        if owned:
                            owned.scan()
                            if activity.expired(owned):
                                timed_out = True; timeout_kind = 'silence'; break
            finally:
                try:
                    if timed_out or interrupted:
                        # The leader can exit while descendants ignore the signal.
                        # Finish the entire group before returning to the owner.
                        kill(signal.SIGTERM)
                        if owned:
                            owned.scan(); owned.signal(signal.SIGTERM)
                        try: child.communicate(timeout=.3 if owned else 5)
                        except subprocess.TimeoutExpired: pass
                        kill(signal.SIGKILL)
                        child.communicate()
                    if owned: owned.drain()
                finally:
                    signal.setitimer(signal.ITIMER_REAL, 0)
                    if reader:
                        reader.join(timeout=5)
                        if reader.is_alive():
                            stream_error.append('execution stream did not close')
                            if observer: observer.error = 'execution stream did not close'
            if interrupted: return 130, '', dict(outcome='cancelled', provider_exit=child.returncode)
            if timed_out:
                if timeout_kind == 'silence' and options.get('cancel_file') and options.get('phase') in ('correctness', 'security', 'performance'):
                    # A specialist shell can also return 25 for withdrawal.
                    # Give its parent evidence of an actual watchdog failure.
                    e.observe(e.atomic, Path(options['cancel_file'] + '.' + options['phase'] + '.silence'), dict(timeout_kind='silence'))
                return (25 if timeout_kind == 'silence' else 124), '', dict(outcome='timeout', timeout_kind=timeout_kind,
                            provider_exit=child.returncode, duration_seconds=time.monotonic()-started)
            # Without the watchdog this remains observational gate evidence;
            # an inherited pipe cannot invalidate a successful final result.
            if stream_error and options.get('silence_timeout_seconds'):
                raise OSError('provider execution stream unavailable')
    finally:
        # Exceptional reader/inspection/spawn paths must also finish all owned
        # writers before the receipt or the trusted shell can report a stop.
        if child and child.poll() is None:
            kill(signal.SIGKILL)
            child.communicate()
        if owned: owned.drain()
        signal.setitimer(signal.ITIMER_REAL, 0)
        for sig, handler in previous.items(): signal.signal(sig, handler)
        if tempdir:
            try:
                # A failure after Popen must also finish the child before its
                # temporary directory is removed. Normal exits already waited.
                if child and child.poll() is None:
                    kill(signal.SIGKILL)
                    child.communicate()
            finally:
                signal.setitimer(signal.ITIMER_REAL, 0)
                try: shutil.rmtree(tempdir)
                except OSError as exc: note('Bureau provider: could not remove ' + tempdir + ': ' + str(exc))
    options['_provider_exit'] = child.returncode
    if interrupted: return 130, '', dict(outcome='cancelled', provider_exit=child.returncode)
    stderr = (evidence/'stderr.log').read_text(errors='replace')
    stdout = (evidence/'stdout.log').read_text(errors='replace')
    if child.returncode:
        text = stderr + stdout
        outcome = 'provider-error'; code = 22
        if re.search(r'not logged in|unauthorized|authentication', text, re.I): outcome='auth'; code=16
        elif re.search(r'quota|rate.limit|usage.limit', text, re.I): outcome='quota'; code=23
        elif re.search(r'permission denied|operation not permitted|sandbox', text, re.I): outcome='environment'; code=24
        return code, '', dict(outcome=outcome, provider_exit=child.returncode)
    usage = {}; cost = None; recovered_errors = {}; structured_source = None
    if runner == 'codex':
        text = final.read_text() if final.exists() else ''
        pending_error = False
        for line in stdout.splitlines():
            try: event = json.loads(line)
            except ValueError: continue
            if not isinstance(event, dict): continue
            # Only a later completion recovers an error notice; turn.failed
            # remains fatal even when another completion follows it.
            if event.get('type') == 'turn.failed':
                return 22, '', dict(outcome='provider-error', provider_exit=child.returncode)
            if event.get('type') == 'turn.completed':
                usage = event.get('usage', {})
                pending_error = False
            elif event.get('type') == 'error':
                pending_error = True
                if not recovered_errors:
                    recovered_errors = dict(count=0, first_message=str(event.get('message', ''))[:200])
                recovered_errors['count'] += 1
        if pending_error: return 22, '', dict(outcome='provider-error', provider_exit=child.returncode)
    else:
        envelope = claude_envelope(stdout)
        if envelope.get('is_error'): return 22, '', dict(outcome='provider-error', provider_exit=child.returncode)
        value = envelope.get('structured_output')
        if schema and not isinstance(value, dict):
            found, recovered = claude_transcript_result(options, repo, evidence)
            if found: value = recovered; structured_source = 'transcript'
        text = json.dumps(value) if isinstance(value, dict) or structured_source else envelope.get('result', '')
        usage = envelope.get('usage', {}); cost = envelope.get('total_cost_usd')
    if not isinstance(text, str) or not text.strip(): raise ValueError('provider returned no final message')
    if schema:
        value = structured(text)
        if runner == 'codex': value = g.canonical_result(value, json.loads(schema.read_text()))
        value = e.normalize_insights(value)
        validate(value, json.loads(schema.read_text()))
        text = json.dumps(value)
    value = structured(text)
    observer = options.get('_gate_observer')
    gate_head = e.observe(current_head, repo) if observer else None
    machine_gate = (observer and not observer.error and gate_head == options.get('_gate_start_head')
                    and g.machine_only(value, options['stage'], options.get('gate_commands', []), gate_head,
                                       e.observe(observer.records, (e.observe(transcript, options, repo, evidence) or {}).get('transcript')) or []))
    if not machine_gate and environment_blocked(value, text, runner, options['stage']):
        return 24, '', dict(outcome='environment', provider_exit=child.returncode)
    metadata = dict(outcome='complete', provider=runner, provider_exit=child.returncode, usage=usage, total_cost_usd=cost, duration_seconds=time.monotonic()-started)
    if recovered_errors: metadata['recovered_errors'] = recovered_errors
    if structured_source: metadata['structured_source'] = structured_source
    if options['cost_tracking']:
        metadata['cost_envelope'] = True
        text = json.dumps(dict(result=text, usage=usage, total_cost_usd=cost, provider=runner))
    return 0, text, metadata


# The provider's own record of a call, for a pass that ends without a result.
# `claude -p --output-format json` writes stdout.log only when it finishes, so
# a call that timed out leaves it empty; Claude's transcript is written while it
# works, at <config dir>/projects/<slug>/<session id>.jsonl. The config dir is
# the stage's config_dir, else CLAUDE_CONFIG_DIR, else ~/.claude; the slug is
# claude_project_slug of the resolved working directory. Should a Claude version place it elsewhere, a
# search for the session id under projects/ finds it. `codex exec --json`
# streams its events, the first being thread.started with the thread id; its
# rollout file is
# <CODEX_HOME or ~/.codex>/sessions/YYYY/MM/DD/rollout-<time>-<thread id>.jsonl.
CODEX_THREAD = re.compile(r'^[0-9A-Za-z-]{8,64}$')
SLUG_MAX = 200
BASE36 = '0123456789abcdefghijklmnopqrstuvwxyz'


def claude_project_slug(path):
    # Claude Code's own project-directory name (functions k, tx and EQ in the
    # 2.1.286 binary): every UTF-16 code unit other than an ASCII letter or digit
    # becomes '-' (an emoji, two code units, becomes '--'); a result longer than
    # 200 characters is cut to 200 and gets '-' plus the base-36 absolute value
    # of a 32-bit hash, e = (e << 5) - e + unit, over the code units of the path.
    raw = path.encode('utf-16-le', 'surrogatepass')
    units = [int.from_bytes(raw[i:i + 2], 'little') for i in range(0, len(raw), 2)]
    slug = ''.join(chr(u) if 48 <= u <= 57 or 65 <= u <= 90 or 97 <= u <= 122 else '-' for u in units)
    if len(slug) <= SLUG_MAX: return slug
    value = 0
    for u in units: value = (value * 31 + u) & 0xFFFFFFFF
    value = abs(value - 0x100000000 if value & 0x80000000 else value)
    digits = ''
    while True:
        value, rest = divmod(value, 36); digits = BASE36[rest] + digits
        if not value: break
    return slug[:SLUG_MAX] + '-' + digits


def _found(root, pattern):
    # The first file under root matching pattern, or None. Looking for evidence
    # never fails the call: an unreadable directory is the same as no file.
    try: return next(iter(sorted(root.glob(pattern))), None)
    except OSError: return None


# How long a cached "no" holds. A "yes" holds until the binary changes or the CLI
# rejects the flag (claude_rejected_session_id); a "no" is asked again after a
# day, because behind an unchanged wrapper or shim (mise, asdf, a script of
# one's own) the CLI can be upgraded without the key below changing.
SESSION_FLAG_RECHECK = 86400


def _claude_cli(environ):
    # The claude found on PATH and the cache key of the file found there: its
    # resolved path, size and mtime. Returns (path, key, reason-if-none).
    found = shutil.which('claude', path=environ.get('PATH'))
    if not found: return None, None, 'claude executable not found on PATH'
    real = os.path.realpath(found)
    try: stat = os.stat(real)
    except OSError as exc: return None, None, 'claude executable unreadable: ' + str(exc)
    return found, [real, stat.st_size, stat.st_mtime_ns], None


def remember_session_flag(cache, key, answer, source):
    # Atomic, so parallel calls (the review's three specialists) can share the
    # file; a temporary file that could not be moved into place is removed.
    temp = cache.with_name(cache.name + '.' + str(os.getpid()))
    try:
        temp.write_text(json.dumps(dict(key=key, session_id=answer, source=source, checked=int(time.time()))) + '\n')
        os.replace(temp, cache)
    except OSError:
        try: temp.unlink()
        except OSError: pass


def claude_takes_session_id(cache, mode='default', environ=None):
    # Whether the claude CLI on PATH takes --session-id, so a CLI without the
    # flag runs as before v3.2: without it. `claude --help` is asked once per
    # binary; the answer is kept in `cache` (the evidence directory's
    # .claude-cli.json) under the binary's resolved path, size and mtime, so an
    # update asks again and an unchanged binary costs no extra process; a "no"
    # is asked again after SESSION_FLAG_RECHECK. Returns (answer, reason); a
    # failed check is not kept and leaves the flag off. The check runs in the
    # environment the agent gets (mode: repo.untrusted_env).
    environ = os.environ if environ is None else environ
    found, key, reason = _claude_cli(environ)
    if reason: return False, reason
    no = 'the claude CLI at ' + key[0] + ' does not take --session-id'
    try:
        saved = json.loads(cache.read_text())
        if isinstance(saved, dict) and saved.get('key') == key and isinstance(saved.get('session_id'), bool):
            if saved['session_id']: return True, None
            checked = saved.get('checked')
            if type(checked) is int and 0 <= time.time() - checked < SESSION_FLAG_RECHECK:
                return False, no + ' (' + str(saved.get('source') or 'claude --help') + ')'
    except (OSError, ValueError): pass
    try:
        proc = subprocess.run([found, '--help'], capture_output=True, text=True, timeout=20, stdin=subprocess.DEVNULL,
                              env=untrusted_env(environ, mode, 'claude'))
    except (OSError, subprocess.SubprocessError) as exc: return False, 'claude --help failed: ' + str(exc)
    if proc.returncode != 0: return False, 'claude --help failed with exit ' + str(proc.returncode)
    answer = '--session-id' in proc.stdout
    remember_session_flag(cache, key, answer, 'claude --help')
    return answer, None if answer else no + ' (claude --help)'


def claude_rejected_session_id(evidence):
    # The CLI refused the flag at argument parsing, before any model call:
    # commander's "error: unknown option '--session-id'", as Claude Code prints
    # it for an option it does not know.
    try: text = (evidence/'stderr.log').read_text(errors='replace')
    except OSError: return False
    return re.search(r"unknown option '--session-id'", text) is not None


def transcript(options, repo, evidence, environ=None):
    environ = os.environ if environ is None else environ
    home = Path(environ.get('HOME') or os.path.expanduser('~'))
    if options['runner'] == 'claude':
        session = options.get('session_id')
        if not session:
            missing = dict(session_id=None, transcript=None, transcript_found=False)
            if options.get('session_note'): missing['transcript_note'] = options['session_note'] + '; no session id was set'
            return missing
        projects = Path(claude_env(options, env=environ).get('CLAUDE_CONFIG_DIR') or home/'.claude')/'projects'
        expected = projects/claude_project_slug(str(repo))/(session + '.jsonl')
        found = _found(expected.parent, expected.name) or _found(projects, '*/' + session + '.jsonl')
        return dict(session_id=session, transcript=str(found or expected), transcript_found=found is not None)
    thread = None
    try: lines = (evidence/'stdout.log').read_text(errors='replace').splitlines()
    except OSError: lines = []
    for line in lines:
        try: event = json.loads(line)
        except ValueError: continue
        if isinstance(event, dict) and event.get('type') == 'thread.started':
            value = event.get('thread_id')
            if isinstance(value, str) and CODEX_THREAD.match(value): thread = value
            break
    if not thread:
        return dict(session_id=None, transcript=None, transcript_found=False,
                    transcript_note='codex reported no thread id (no thread.started event in stdout.log)')
    found = _found(Path(environ.get('CODEX_HOME') or home/'.codex')/'sessions', '*/*/*/rollout-*-' + thread + '.jsonl')
    return dict(session_id=thread, transcript=str(found) if found else None, transcript_found=found is not None)


def claude_transcript_result(options, repo, evidence):
    # A deferred turn can replace the envelope's structured_output with prose.
    # Claude JSONL stores assistant tool_use blocks in message.content. Keep
    # the last matching input even if invalid: the schema must reject it,
    # never silently accept an earlier StructuredOutput instead.
    record = transcript(options, repo, evidence)
    if not record['transcript_found']: return False, None
    return transcript_result(Path(record['transcript']))


def transcript_result(path):
    found = False; value = None
    try:
        with path.open(errors='replace') as source:
            for line in source:
                try: event = json.loads(line)
                except ValueError: continue
                if not isinstance(event, dict) or event.get('type') != 'assistant': continue
                message = event.get('message')
                content = message.get('content') if isinstance(message, dict) else None
                if not isinstance(content, list): continue
                for block in content:
                    if isinstance(block, dict) and block.get('type') == 'tool_use' and block.get('name') == 'StructuredOutput':
                        found = True; value = block.get('input')
    except OSError: return False, None
    return found, value


def note(text):
    """A line on stderr for the stage's log. After a hang-up the terminal is
    gone and the write fails: the outcome and the exit code stay (Python's last
    flush of stderr would fail again and end the adapter with 120)."""
    try:
        print(text, file=sys.stderr); sys.stderr.flush()
    except OSError:
        devnull = os.open(os.devnull, os.O_WRONLY)
        os.dup2(devnull, 2); os.close(devnull)


def outside_gate(options, repo, command, log):
    """Run a canonical command once, bounded and without Bureau credentials."""
    child = None; stopped = None; abort = None
    def stop(signum, frame):
        nonlocal stopped
        stopped = signum
        if child:
            try: os.killpg(child.pid, signal.SIGKILL)
            except ProcessLookupError: pass
    previous = {sig:signal.signal(sig, stop) for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP)}
    try:
        with os.fdopen(os.open(log, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'wb') as output:
            child = subprocess.Popen(['/bin/bash', '--noprofile', '--norc', '-o', 'pipefail', '-c', command], cwd=repo,
                                     stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.STDOUT,
                                     env=untrusted_env(os.environ, options['untrusted_env']), start_new_session=True)
            if stopped: stop(stopped, None)
            try: code = child.wait(timeout=options['timeout'])
            except subprocess.TimeoutExpired:
                abort = 'timeout'
                try: os.killpg(child.pid, signal.SIGKILL)
                except ProcessLookupError: pass
                child.wait(); code = 124
            if stopped: code, abort = 130, 'kill'
            elif code < 0 or code == 130: abort = 'kill'
        return code, abort
    finally:
        if child:
            # A successful check may leave background writers in its group.
            try: os.killpg(child.pid, signal.SIGKILL)
            except ProcessLookupError: pass
            child.wait()
        for sig, handler in previous.items(): signal.signal(sig, handler)


def gate_action(args, config, options, repo):
    configured = g.commands(config, repo, args.stage)
    if args.gate_action == 'command': return configured[0] if configured else ''
    store = evidence_store(repo); head = current_head(repo)
    raw = {} if args.gate_action == 'publish' else json.load(sys.stdin)
    if not isinstance(raw, dict):
        return json.dumps(dict(eligible=False, green=False, logs=[], reason='gate input must be one object'))
    context = raw.get('context') if args.gate_action == 'run' and isinstance(raw, dict) else None
    if args.gate_action in ('plan', 'publish'):
        value = json.loads(raw['result']) if isinstance(raw, dict) and isinstance(raw.get('result'), str) else raw
        if args.gate_context and Path(args.gate_context).is_file() and Path(args.gate_context).stat().st_size:
            context = json.loads(Path(args.gate_context).read_text())
    expected = dict(run_id=os.environ.get('BUREAU_RUN_ID') or None, stage_attempt_id=os.environ.get('BUREAU_STAGE_ATTEMPT_ID') or None,
                    issue=os.environ.get('BUREAU_CURRENT_ISSUE') or None, stage=args.stage, phase=options['phase'])
    foreign = (options['runner'] != 'codex' or context is not None and
               (not isinstance(context, dict) or any(v is not None and context.get(k) != v for k,v in expected.items())))
    if args.gate_action == 'run' and not foreign:
        for key in ('run_id', 'stage_attempt_id'):
            owner = (context or {}).get(key) or expected[key]
            if owner and raw.get(key) != owner: foreign = True
    if foreign:
        return json.dumps(dict(eligible=False, green=False, logs=[], reason='gate evidence belongs to another current stage'))
    if args.gate_action == 'publish':
        store.record_publication(context, args.stage, repo, args.publication_before, args.publication_after)
        return json.dumps(dict(recorded=True))
    if args.gate_action == 'plan':
        plan = g.plan(store, value, args.stage, configured, head, context, repo)
        if plan['eligible']:
            # Even the compatibility prefix uses the same durable reservation.
            run_id = (context or {}).get('run_id') or os.environ.get('BUREAU_RUN_ID') or uuid.uuid4().hex
            issue = os.environ.get('BUREAU_CURRENT_ISSUE') or None
            store.begin_run(run_id, issue, repo, head)
            attempt = (context or {}).get('stage_attempt_id') or os.environ.get('BUREAU_STAGE_ATTEMPT_ID')
            if not attempt: attempt = store.begin_attempt(run_id, issue, args.stage, 'gate', repo, head)
            plan.update(run_id=run_id, stage_attempt_id=attempt)
        return json.dumps(plan)
    # Re-check original evidence, config and HEAD immediately before running.
    fresh = g.plan(store, raw.get('result'), args.stage, configured, head, raw.get('context'), repo)
    if not fresh['eligible'] or raw.get('head') != head or raw.get('commands') != fresh.get('commands'):
        return json.dumps(dict(eligible=False, green=False, reason='gate evidence changed before outside execution', logs=[]))
    run_id, owner = raw['run_id'], raw['stage_attempt_id']
    logs = []; green = True; verified = True
    for command in fresh['commands']:
        reserved = e.observe(store.reserve_gate_rerun, run_id, owner, head, command, g.digest(command), fresh.get('gate_attempt_ids', []))
        if reserved is None:
            green = False; verified = False; continue
        claimed, reservation = reserved
        record = reservation.get('result')
        if reservation['gate_attempt_ids'] != fresh.get('gate_attempt_ids', []): record = None
        if claimed:
            # Original outside output belongs to normal stage logs, never the
            # metadata store. Eligibility depends on its digest, not copied bytes.
            directory = repo / 'logs' / 'outside-gates'
            e.private_dir(directory)
            log = directory / (reservation['attempt_id'] + '.log')
            started = time.monotonic()
            code, abort = outside_gate(options, repo, command, log)
            # Cancellation wins over EVERY subsequent read/write/validation.
            if code == 130: options['_gate_cancelled'] = True
            logs.append(str(log))
            def measurement():
                data = log.read_bytes()
                return dict(attempt_id=reservation['attempt_id'], gate_id=g.digest(command), command_sha256=g.digest(command),
                            command_id='repo.test_command' if (config.get('repo') or {}).get('test_command') else 'qa.test',
                            head=head, end_head=current_head(repo), normalized_exit=code,
                            source='bureau.shell', environment='outside', abort=abort, call_id_sha256=None,
                            duration_seconds=time.monotonic()-started, bytes=len(data),
                            sha256=hashlib.sha256(data).hexdigest(), stage_log=str(log))
            record = e.observe(measurement)
            if record is not None:
                record = e.observe(store.finish_gate_rerun, run_id, owner, reservation['attempt_id'], record)
        elif record and isinstance(record.get('stage_log'), str):
            logs.append(record['stage_log'])
        if not record or not store.valid_execution(record):
            green = False; verified = False; continue
        if claimed:
            issue = os.environ.get('BUREAU_CURRENT_ISSUE')
            if issue:
                ticket = t.Ledger(t.root_for(repo), issue, config)
                ticket.call('gate', run_id, owner, record['command_id'], command, record['normalized_exit'],
                            record['head'], record['end_head'], record['stage_log'])
        if record['normalized_exit'] == 130: options['_gate_cancelled'] = True
        verified = verified and record['head'] == head and record['end_head'] == head
        green = green and record['normalized_exit'] == 0 and record['head'] == head and record['end_head'] == head
    verified = verified and current_head(repo) == head
    return json.dumps(dict(eligible=verified, green=green and verified, logs=logs,
                           reason='outside gate green' if green and verified else 'outside gate red, aborted or incomplete'))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--stage', required=True); parser.add_argument('--repo', default='.')
    parser.add_argument('--phase')
    parser.add_argument('--config', default=os.environ.get('BUREAU_CONFIG', '.bureau.json'))
    parser.add_argument('--prompt-file'); parser.add_argument('--system-file'); parser.add_argument('--schema')
    parser.add_argument('--gate-context')
    parser.add_argument('--cancel-file')
    parser.add_argument('--gate-action', choices=('command', 'plan', 'run', 'publish'))
    parser.add_argument('--publication-before'); parser.add_argument('--publication-after')
    parser.add_argument('--check', action='store_true'); parser.add_argument('--describe', action='store_true')
    args = parser.parse_args(); evidence = None; options = {}
    try:
        config_path=Path(args.config).resolve(); config=json.loads(config_path.read_text())
        options=configuration(args.stage, config, os.environ)
        options['phase'] = args.phase or os.environ.get('BUREAU_PROVIDER_PHASE') or args.stage
        options['cancel_file'] = args.cancel_file
        if args.describe: print(json.dumps(options)); return 0
        options['_ticket_config'] = config
        options['untrusted_env']=untrusted_env_mode(config)
        repo=Path(args.repo).resolve()
        if args.gate_action:
            response = e.observe(gate_action, args, config, options, repo)
            print(response or json.dumps(dict(eligible=False, green=False, logs=[], reason='gate evidence unavailable')))
            return 130 if options.get('_gate_cancelled') else 0
        options['gate_commands'] = g.commands(config, repo, args.stage)
        options['gate_command_id'] = 'repo.test_command' if (config.get('repo') or {}).get('test_command') else 'qa.test'
        options['gate_context'] = args.gate_context
        auth(options)
        if args.check: print(json.dumps(dict(runner=options['runner'], authenticated=True))); return 0
        if not args.prompt_file: raise ValueError('--prompt-file is required')
        prompt=sys.stdin.read() if args.prompt_file=='-' else Path(args.prompt_file).read_text()
        system=Path(args.system_file).read_text() if args.system_file else ''
        repo=Path(args.repo).resolve()
        base=Path(os.environ.get('BUREAU_PROVIDER_LOG_DIR',str(config_path.parent/'logs/provider-runs'))).absolute()
        evidence=base/uuid.uuid4().hex; evidence.mkdir(parents=True,mode=0o700)
        schema=Path(args.schema).resolve() if args.schema else None
        if options['runner']=='claude':
            supported,options['session_note']=claude_takes_session_id(base/'.claude-cli.json',options['untrusted_env'])
            if supported: options['session_id']=str(uuid.uuid4())
        code,text,metadata=run(options,prompt,system,repo,evidence,schema)
        if options.get('session_id') and code not in (0,25,124,130) and claude_rejected_session_id(evidence):
            # A "yes" gone stale: the CLI behind an unchanged path (a wrapper, a
            # shim) no longer takes the flag. The call failed while parsing its
            # arguments, so nothing was paid: remember "no", keep the refused
            # attempt's logs, and run the call once more without the flag.
            _,key,reason=_claude_cli(os.environ)
            if not reason: remember_session_flag(base/'.claude-cli.json',key,False,'rejected by the CLI')
            for name in ('stdout.log','stderr.log'):
                if (evidence/name).exists(): (evidence/name).replace(evidence/('session-id-rejected.'+name))
            options['session_id']=None
            options['session_note']="the claude CLI rejected --session-id (unknown option), and the call ran again without it"
            code,text,metadata=run(options,prompt,system,repo,evidence,schema)
            metadata['session_id_rejected']=True
        text = completion_text(code, text, metadata, repo, schema)
        metadata.update(estimated_cost_usd=metadata.get('total_cost_usd'), actual_billed_cost_usd=None,
                        account_used_percent=None, stage=args.stage, provider=options['runner'], model=options['model'], evidence=str(evidence))
        metadata.update(e.observe(transcript,options,repo,evidence) or dict(transcript=None))
        e.observe(e.atomic,evidence/'result.json', metadata)
        note('Bureau provider evidence: '+str(evidence))
        if metadata['transcript']: note('Bureau provider transcript: '+metadata['transcript'])
        if code==0:
            try: print(text, flush=True)
            except BrokenPipeError:
                # The completion receipt is durable even if its consumer stopped.
                devnull = os.open(os.devnull, os.O_WRONLY)
                os.dup2(devnull, 1); os.close(devnull)
        else: note('Bureau provider outcome: '+metadata['outcome'])
        return code
    except AuthError as exc: print(str(exc),file=sys.stderr); return 16
    except (PermissionError, UntrustedEnvError) as exc:
        note('Bureau provider: '+str(exc)); return 130 if options.get('_gate_cancelled') else 24
    except (OSError, ValueError, TypeError, subprocess.SubprocessError) as exc:
        if evidence:
            failed={**options.get('_receipt_metadata', {}), 'outcome':'invalid-result','error':str(exc)}
            failed.update(e.observe(transcript,options,repo,evidence) or {})
            e.observe(e.atomic,evidence/'result.json', failed)
        note('Bureau provider: '+str(exc)); return 130 if options.get('_gate_cancelled') else 22


if __name__=='__main__': sys.exit(main())
