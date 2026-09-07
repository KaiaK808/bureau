#!/usr/bin/env python3
"""Provider adapter: bounded subprocess, stdin prompts, separate evidence and results."""
import argparse
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
import time
import uuid


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
    timeout = float(env.get('BUREAU_STAGE_TIMEOUT') or item.get('timeout_seconds') or provider.get('timeout_seconds', 900))
    if not math.isfinite(timeout) or timeout <= 0 or timeout > 86400: raise ValueError('timeout_seconds must be within (0, 86400]')
    return dict(stage=stage, runner=runner, model=model, sandbox=sandbox, reasoning=reasoning, timeout=timeout,
                headroom=runner == 'claude' and env.get('BUREAU_HEADROOM_WRAP', str(agents.get('headroom_wrap', False))).lower() in ('1', 'true'),
                cost_tracking=env.get('BUREAU_COST_TRACKING', str(config.get('session', {}).get('cost_tracking', False))).lower() in ('1', 'true'))


class AuthError(Exception):
    pass


def auth(options):
    runner = options['runner']
    if not shutil.which(runner): raise AuthError(runner + ' executable is missing')
    cmd = ['claude', 'auth', 'status', '--json'] if runner == 'claude' else ['codex', 'login', 'status']
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
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


def validate(value, schema):
    # Small schema validator for the adapter's object/array/scalar contracts.
    kind = schema.get('type')
    checks = {'object': lambda x:isinstance(x, dict), 'array': lambda x:isinstance(x, list),
              'string': lambda x:isinstance(x, str), 'integer': lambda x:type(x) is int,
              'number': lambda x:type(x) in (int,float), 'boolean':lambda x:type(x) is bool}
    if kind in checks and not checks[kind](value): raise ValueError('result type must be ' + kind)
    if 'enum' in schema and value not in schema['enum']: raise ValueError('unrecognized result value')
    if isinstance(value, dict):
        if any(k not in value for k in schema.get('required', [])): raise ValueError('result missing required fields')
        props = schema.get('properties', {})
        if schema.get('additionalProperties') is False and set(value) - set(props): raise ValueError('unexpected result fields')
        for k, v in value.items():
            if k in props: validate(v, props[k])
    if isinstance(value, list):
        for v in value: validate(v, schema.get('items', {}))


def run(options, prompt, system, repo, evidence, schema=None):
    runner = options['runner']
    final = evidence / 'final.txt'
    if runner == 'codex':
        command = ['codex', 'exec', '--json', '-C', str(repo), '-s', options['sandbox'], '-o', str(final)]
        if options['reasoning']: command += ['-c', 'model_reasoning_effort=' + json.dumps(options['reasoning'])]
        if schema: command += ['--output-schema', str(schema)]
        # Codex edits files; Git metadata writes remain the shell executor's job.
        prompt = system + '\n\n' + prompt + '\n\nDo not run git commit or git push. Leave changes for the Bureau shell executor. Mark completed tasks truthfully. If tests are denied by the environment, report the permission blocker separately from code failures.'
    else:
        command = ['claude', '-p', '--output-format', 'json']
        if options['stage'] == 'upstream_summary': command += ['--tools', '']
        else: command += ['--dangerously-skip-permissions']
        if system:
            (evidence/'system.txt').write_text(system)
            command += ['--append-system-prompt-file', str(evidence/'system.txt')]
        if schema: command += ['--json-schema', schema.read_text()]
        if options['headroom']:
            if not shutil.which('headroom'): raise ValueError('headroom executable is missing')
            command = ['headroom', 'wrap', 'claude', '--', *command[1:]]
    if options['model']: command += ['--model', options['model']]
    if runner == 'codex': command.append('-')
    interrupted = None
    child = None
    def kill(signum):
        if child:
            try: os.killpg(child.pid, signum)
            except ProcessLookupError: pass
    def stop(signum, frame):
        nonlocal interrupted
        interrupted = signum; kill(signum)
        signal.setitimer(signal.ITIMER_REAL, 5)
    signal.signal(signal.SIGTERM, stop); signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGALRM, lambda signum, frame: kill(signal.SIGKILL))
    started = time.monotonic()
    with (evidence/'prompt.txt').open('w') as out: out.write(prompt)
    with (evidence/'stdout.log').open('wb') as stdout, (evidence/'stderr.log').open('wb') as stderr:
        child = subprocess.Popen(command, cwd=repo, stdin=subprocess.PIPE, stdout=stdout, stderr=stderr, start_new_session=True)
        if interrupted: kill(interrupted)
        timed_out = False
        try:
            child.communicate(prompt.encode(), timeout=options['timeout'])
        except subprocess.TimeoutExpired:
            timed_out = True
            kill(signal.SIGTERM)
            try: child.communicate(timeout=5)
            except subprocess.TimeoutExpired: pass
        finally:
            try:
                if timed_out or interrupted:
                    # The leader can exit while descendants ignore the signal.
                    # Finish the entire group before returning to the owner.
                    kill(signal.SIGKILL)
                    child.communicate()
            finally:
                signal.setitimer(signal.ITIMER_REAL, 0)
        if timed_out:
            return 124, '', dict(outcome='timeout', duration_seconds=time.monotonic()-started)
    if interrupted: return 130, '', dict(outcome='cancelled')
    stderr = (evidence/'stderr.log').read_text(errors='replace')
    stdout = (evidence/'stdout.log').read_text(errors='replace')
    if child.returncode:
        text = stderr + stdout
        outcome = 'provider-error'; code = 22
        if re.search(r'not logged in|unauthorized|authentication', text, re.I): outcome='auth'; code=16
        elif re.search(r'quota|rate.limit|usage.limit', text, re.I): outcome='quota'; code=23
        elif re.search(r'permission denied|operation not permitted|sandbox', text, re.I): outcome='environment'; code=24
        return code, '', dict(outcome=outcome, provider_exit=child.returncode)
    usage = {}; cost = None
    if runner == 'codex':
        text = final.read_text() if final.exists() else ''
        for line in stdout.splitlines():
            try: event = json.loads(line)
            except ValueError: continue
            if not isinstance(event, dict): continue
            if event.get('type') == 'turn.completed': usage = event.get('usage', {})
            if event.get('type') in ('turn.failed', 'error'):
                return 22, '', dict(outcome='provider-error')
    else:
        try: envelope = json.loads(stdout)
        except ValueError: raise ValueError('Claude did not return a JSON envelope')
        if not isinstance(envelope, dict): raise ValueError('Claude envelope must be an object')
        if envelope.get('is_error'): return 22, '', dict(outcome='provider-error')
        value = envelope.get('structured_output')
        text = json.dumps(value) if isinstance(value, dict) else envelope.get('result', '')
        usage = envelope.get('usage', {}); cost = envelope.get('total_cost_usd')
    if not isinstance(text, str) or not text.strip(): raise ValueError('provider returned no final message')
    if schema:
        value = structured(text)
        validate(value, json.loads(schema.read_text()))
        text = json.dumps(value)
    value = structured(text)
    if value and value.get('status') == 'NEEDS_HUMAN' and re.search(r'operation not permitted|permission denied|sandbox denied', text, re.I):
        return 24, '', dict(outcome='environment')
    metadata = dict(outcome='complete', provider=runner, usage=usage, total_cost_usd=cost, duration_seconds=time.monotonic()-started)
    if options['cost_tracking']:
        text = json.dumps(dict(result=text, usage=usage, total_cost_usd=cost, provider=runner))
    return 0, text, metadata


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--stage', required=True); parser.add_argument('--repo', default='.')
    parser.add_argument('--config', default=os.environ.get('BUREAU_CONFIG', '.bureau.json'))
    parser.add_argument('--prompt-file'); parser.add_argument('--system-file'); parser.add_argument('--schema')
    parser.add_argument('--check', action='store_true'); parser.add_argument('--describe', action='store_true')
    args = parser.parse_args(); evidence = None
    try:
        config_path=Path(args.config).resolve(); config=json.loads(config_path.read_text())
        options=configuration(args.stage, config, os.environ)
        if args.describe: print(json.dumps(options)); return 0
        auth(options)
        if args.check: print(json.dumps(dict(runner=options['runner'], authenticated=True))); return 0
        if not args.prompt_file: raise ValueError('--prompt-file is required')
        prompt=sys.stdin.read() if args.prompt_file=='-' else Path(args.prompt_file).read_text()
        system=Path(args.system_file).read_text() if args.system_file else ''
        repo=Path(args.repo).resolve()
        base=Path(os.environ.get('BUREAU_PROVIDER_LOG_DIR',str(config_path.parent/'logs/provider-runs')))
        evidence=base/uuid.uuid4().hex; evidence.mkdir(parents=True,mode=0o700)
        schema=Path(args.schema).resolve() if args.schema else None
        code,text,metadata=run(options,prompt,system,repo,evidence,schema)
        metadata.update(run_id=os.environ.get('BUREAU_RUN_ID'), issue=os.environ.get('BUREAU_CURRENT_ISSUE'),
                        estimated_cost_usd=metadata.get('total_cost_usd'), actual_billed_cost_usd=None,
                        account_used_percent=None, stage=args.stage, provider=options['runner'], model=options['model'], evidence=str(evidence))
        (evidence/'result.json').write_text(json.dumps(metadata,indent=2)+'\n')
        print('Bureau provider evidence: '+str(evidence),file=sys.stderr)
        if code==0: print(text)
        else: print('Bureau provider outcome: '+metadata['outcome'],file=sys.stderr)
        return code
    except AuthError as exc: print(str(exc),file=sys.stderr); return 16
    except PermissionError as exc: print(str(exc),file=sys.stderr); return 24
    except (OSError, ValueError, TypeError, subprocess.SubprocessError) as exc:
        if evidence: (evidence/'result.json').write_text(json.dumps({'outcome':'invalid-result','error':str(exc)}))
        print('Bureau provider: '+str(exc),file=sys.stderr); return 22


if __name__=='__main__': sys.exit(main())
