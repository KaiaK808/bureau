#!/usr/bin/env python3
"""Read-only Bureau diagnostics and explicit additive configuration migration."""
import argparse
import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import sys
import subprocess
import uuid

sys.dont_write_bytecode = True
SCRIPTS = Path(__file__).resolve().parent
STAGES = ('spec', 'spec_review', 'research', 'ux', 'copy', 'implement', 'qa', 'code_review', 'merge', 'rebase')


def module(name):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / ('bureau-' + name + '.py'))
    value = importlib.util.module_from_spec(spec); spec.loader.exec_module(value)
    return value


def validate(config):
    errors = []
    if not isinstance(config, dict): return ['Configuration must be an object']
    if type(config.get('version', 1)) is not int or config.get('version', 1) not in (1, 2): errors.append('Supported config versions: 1 and 2')
    for name in ('linear', 'agents', 'repo'):
        if not isinstance(config.get(name), dict): errors.append(name + ' must be an object')
    for name in ('session', 'supervisor'):
        if name in config and not isinstance(config[name], dict): errors.append(name + ' must be an object')
    if errors: return errors
    teams = config['linear'].get('teams')
    if not isinstance(teams, list) or not teams: errors.append('linear.teams must contain at least one team')
    else:
        for team in teams:
            if not isinstance(team, dict) or not all(isinstance(team.get(k), str) and team[k] for k in ('id', 'key')):
                errors.append('Each team needs nonempty id and key'); continue
            states = team.get('states')
            if not isinstance(states, dict) or not states or any(not isinstance(v, str) for v in states.values()):
                errors.append('Team states must map names to string IDs')
    agents = config['agents']
    if not isinstance(agents.get('providers', {}), dict): errors.append('agents.providers must be an object')
    elif any(not isinstance(v, dict) for v in agents.get('providers', {}).values()): errors.append('Each provider configuration must be an object')
    for stage in STAGES:
        value = agents.get(stage, False)
        if not isinstance(value, (bool, str, dict)): errors.append('agents.' + stage + ' must be a boolean, legacy string or object')
        elif isinstance(value, dict) and type(value.get('enabled', True)) is not bool: errors.append(stage + '.enabled must be boolean')
    if errors: return errors
    provider = module('provider')
    for stage in STAGES:
        try: provider.configuration(stage, config, {})
        except (ValueError, TypeError, AttributeError) as exc: errors.append(stage + ': ' + str(exc))
    return errors


def migrate(config):
    errors = validate(config)
    if errors: raise ValueError('; '.join(errors))
    result = copy.deepcopy(config)
    result['version'] = 2
    result['agents'].setdefault('runner', 'claude')
    if config.get('version', 1) == 1:
        result['agents'].setdefault('model_compatibility', 'v1')
    return result


def migration(path, apply=False, backup_root=None):
    original = path.read_bytes()
    before = json.loads(original); after = migrate(before)
    changes = []
    if before.get('version') != 2: changes.append('Set version to 2')
    if 'runner' not in before['agents']: changes.append('Make existing Claude default explicit')
    if before.get('version', 1) == 1 and 'model_compatibility' not in before['agents']:
        changes.append('Preserve legacy provider model selection')
    result = dict(config=str(path), changed=bool(changes), applied=False, changes=changes)
    if apply and changes:
        if path.is_symlink(): raise ValueError('Refusing to replace a symlinked config')
        if path.read_bytes() != original: raise ValueError('Config changed during migration')
        backup_root = backup_root or module('runtime').common_for(module('runtime').root_for(path.parent)) / 'bureau' / 'config-backups'
        backup_root.mkdir(parents=True, exist_ok=True)
        backup = backup_root / (path.name + '.pre-v2.' + uuid.uuid4().hex + '.bak')
        with backup.open('xb') as out: os.chmod(backup, 0o600); out.write(original)
        temporary = path.with_name(path.name + '.' + uuid.uuid4().hex)
        try:
            with temporary.open('x') as out: os.chmod(temporary, 0o600); out.write(json.dumps(after, indent=2) + '\n')
            if path.read_bytes() != original: raise ValueError('Config changed during migration')
            temporary.replace(path)
        finally: temporary.unlink(missing_ok=True)
        result.update(applied=True, backup=str(backup))
    return result


def diagnose(repo, mode):
    runtime = module('runtime'); provider = module('provider')
    repo = runtime.root_for(repo); path = runtime.config_for(repo); config = json.loads(path.read_text())
    errors = validate(config); warnings = []; effective = {}
    if not errors:
        for stage in STAGES:
            if stage in ('merge', 'rebase') or not runtime.enabled(config, stage): continue
            try: effective[stage] = provider.configuration(stage, config, os.environ)
            except (ValueError, TypeError, AttributeError) as exc: errors.append(stage + ': ' + str(exc))
    required = ['git', 'python3', 'jq', 'curl']
    if mode == 'background':
        required += sorted({v['runner'] for v in effective.values()})
        if not errors and any(runtime.enabled(config, stage) for stage in ('implement', 'code_review', 'merge', 'rebase')):
            required.append('gh')
    missing = [name for name in required if not shutil.which(name)]
    errors.extend('Missing executable: ' + name for name in missing)
    if not isinstance(config, dict): return dict(ok=False, errors=errors)
    if errors: return dict(ok=False, workspace=str(repo), config=str(path), errors=errors)
    if not config.get('repo', {}).get('test_command'): warnings.append('repo.test_command is missing; required for Codex background implementation')
    if (path.parent / '.env').is_file(): warnings.append('Doctor resolves JSON and process environment only; it does not execute .env. Source trusted overrides before running doctor for matching effective settings.')
    active = runtime.read(repo / '.specify/integration.json', {})
    manifest = runtime.read(repo / '.bureau-install.json', {})
    drift = []
    for name, entry in manifest.get('files', {}).items():
        target = repo / name
        if Path(name).is_absolute() or '..' in Path(name).parts:
            errors.append('Unsafe manifest path: ' + name); continue
        expected = entry.get('sha256') if isinstance(entry, dict) else entry
        content = target.read_bytes() if target.is_file() else b''
        if name in ('AGENTS.md', 'CLAUDE.md'):
            begin = b'<!-- bureau-init:begin -->'; end = b'<!-- bureau-init:end -->'
            if begin in content and end in content:
                content = content[content.index(begin):content.index(end) + len(end)] + b'\n'
        if not target.is_file() or hashlib.sha256(content).hexdigest() != expected: drift.append(name)
    if drift: warnings.append('Installed files have drift; review before resync')
    interfaces = {}
    for name in ('AGENTS.md', 'CLAUDE.md', '.agents/skills/bureau/SKILL.md', '.claude/commands/linear-to-spec.md'):
        target = repo / name
        found = target.is_file()
        if found and name in ('AGENTS.md', 'CLAUDE.md'):
            content = target.read_text()
            begin = '<!-- bureau-init:begin -->'; end = '<!-- bureau-init:end -->'
            start = content.find(begin); stop = content.find(end, start + len(begin))
            found = start >= 0 and stop > start and bool(content[start + len(begin):stop].strip())
        interfaces[name] = found
    if not any(interfaces.values()): errors.append('No Bureau instruction or command interfaces found; install interfaces')
    if not active.get('integration'): warnings.append('Spec Kit active integration is missing; initialize with the pinned installer')
    for name in ('bureau-runtime.py', 'bureau-provider.py', 'bureau-stage.md'):
        if not (repo / 'scripts' / name).is_file(): errors.append('Missing runtime asset: scripts/' + name)
    if len(config.get('linear', {}).get('teams', [])) > 1: warnings.append('Runtime routes through the first configured team; review other-team tickets manually')
    return dict(ok=not errors, mode=mode, workspace=str(repo), config=str(path), version=config.get('version', 1),
                interfaces=interfaces, active_integration=active.get('integration'), effective_stages=effective,
                drift=drift, errors=errors, warnings=warnings,
                authentication='not checked', live_model_acceptance='not checked')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo', default='.')
    parser.add_argument('--mode', choices=('app', 'background'), default='app')
    parser.add_argument('--migrate', action='store_true'); parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    try:
        runtime = module('runtime'); repo = runtime.root_for(Path(args.repo).resolve())
        if args.apply and not args.migrate: raise ValueError('--apply requires --migrate')
        result = migration(runtime.config_for(repo), args.apply, runtime.common_for(repo) / 'bureau' / 'config-backups') if args.migrate else diagnose(repo, args.mode)
        print(json.dumps(result, indent=2)); return 0 if result.get('ok', True) else 1
    except (ValueError, OSError, TypeError, AttributeError, KeyError, subprocess.CalledProcessError) as exc:
        print(json.dumps({'ok':False, 'errors':[str(exc)]})); return 1


if __name__ == '__main__': sys.exit(main())
