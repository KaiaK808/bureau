#!/usr/bin/env python3
"""Read-only Bureau diagnostics and explicit additive configuration migration."""
import argparse
import copy
import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path
import re
import shutil
import sys
import subprocess
import tempfile
import uuid

sys.dont_write_bytecode = True
SCRIPTS = Path(__file__).resolve().parent
STAGES = ('spec', 'spec_review', 'research', 'ux', 'copy', 'implement', 'qa', 'code_review', 'merge', 'rebase')


def module(name):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / ('bureau-' + name + '.py'))
    value = importlib.util.module_from_spec(spec); spec.loader.exec_module(value)
    return value


def merge_mode(config):
    """The merge mode the shell pipelines use (bureau-config.sh): absent or null is auto;
    anything but exactly "auto" or "manual" (no whitespace) falls closed to manual.
    Returns (mode, raw)."""
    raw = config.get('agents', {}).get('merge_mode') if isinstance(config.get('agents'), dict) else None
    if raw is None: return 'auto', raw
    return (raw if raw in ('auto', 'manual') else 'manual'), raw


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


def template_source(manifest):
    """Which template revision the installed assets came from, as bureau_install.py recorded it."""
    if not manifest: return dict(status='source not recorded', reason='no installation manifest')
    sources = manifest.get('sources')
    if not isinstance(sources, dict):
        return dict(status='source not recorded', reason='the manifest predates source recording; the next asset --apply records it')
    if not sources:
        return dict(status='source not recorded', reason='no asset scope has a recorded source: the last asset --apply installed no files, or dropped a stale record; the next asset --apply that installs files records it')
    files = json.dumps(manifest.get('files', {}), sort_keys=True).encode()
    if hashlib.sha256(files).hexdigest() != manifest.get('sources_files_sha256'):
        return dict(status='stale', scopes=sources, reason='installed file hashes changed after the source was recorded (an installer that does not record its source wrote the manifest)')
    return dict(status='recorded', scopes=sources)


# Stages whose single provider calls run long: measured 15 to 30 minutes per spec review, QA or
# review call, and spec stages up to an hour over a few calls. Doctor warns below this.
LONG_CALL_STAGES = ('spec', 'spec_review', 'ux', 'qa', 'code_review')
LONG_CALL_MIN_SECONDS = 1800


def stage_env_file(config_path):
    """The .env every stage reads, as _find_config in bureau-config.sh sets it (bureau-config.sh:40-44):
    BUREAU_ENV_FILE, by default (unset or empty) the .env next to .bureau.json; a relative value counts
    from the directory of .bureau.json, never from the working directory. Never ./.env of the checkout
    doctor runs in: in a stage worktree that is a file the branch controls (v3.2). "The directory of
    .bureau.json" is the one BUREAU_CONFIG names, as the stages take it (dirname, no link resolved):
    config_for resolves an explicit BUREAU_CONFIG, so a .bureau.json that is a link would otherwise
    point doctor at the directory of the link's target."""
    explicit = os.environ.get('BUREAU_CONFIG')
    base = Path(os.path.abspath(explicit)).parent if explicit else config_path.parent
    return base / (os.environ.get('BUREAU_ENV_FILE') or '.env')


def stage_env_value(config_path, name):
    """<name> as the implement stage sees it once it has loaded .env (implement-pipeline.sh:17-20): the
    stage reads stage_env_file and nothing else, whichever checkout it runs in, and so does doctor. The
    file is read by the stages' own reader, bureau_load_env in bureau-env.sh, run here unchanged: it
    never executes the file, takes only the keys on its list, and a key the file sets replaces the
    environment's value while a key it lacks leaves the environment's. Returns the value, or None when
    neither sets it; the environment's value when there is no such file or it cannot be read."""
    env_file = stage_env_file(config_path)
    if not env_file.is_file(): return os.environ.get(name)
    script = 'source "$1" || exit 1; bureau_load_env "$2" 2>/dev/null || exit 1; n=$3; [ -z "${!n+set}" ] || printf "set:%s" "${!n}"'
    try:
        proc = subprocess.run(['bash', '--noprofile', '--norc', '-c', script, 'bureau-doctor', str(SCRIPTS / 'bureau-env.sh'), str(env_file), name],
                              stdin=subprocess.DEVNULL, capture_output=True,
                              env={key: value for key, value in os.environ.items() if key not in ('BASH_ENV', 'ENV')})
    except OSError:
        return os.environ.get(name)
    if proc.returncode != 0: return os.environ.get(name)
    out = os.fsdecode(proc.stdout)
    return out[len('set:'):] if out.startswith('set:') else None


def implement_runner(config, provider, env):
    """The runner the implement stage resolves (resolve_runner_for_stage implement in bureau-config.sh,
    the same ladder as configuration() in bureau-provider.py, which this calls): BUREAU_RUNNER_IMPLEMENT,
    then agents.implement.runner when agents.implement is an object (a boolean or legacy string has
    none), then agents.runner, then claude. `env` is the environment the stage has after loading .env
    (stage_env_value). Only a codex implement needs repo.test_command: its completion runs the command
    as an independent check and stops with 24 when it is empty (implement-pipeline.sh:1102-1104).
    Judged whether or not agents.implement is on, since the shepherd runs every stage unless
    --respect-config. None when it does not resolve (an unknown runner: the stage stops on it, and
    doctor reports it for an enabled stage)."""
    try: return provider.configuration('implement', config, env)['runner']
    except (ValueError, TypeError, AttributeError): return None


def gate_switch(agents, key, warnings):
    """A merge gate switch read by the v3.1 rule for agents.merge_require_*: absent or null is
    true (required), a JSON boolean is itself, and any other value counts as required, with a
    warning. Only an explicit false switches the gate off. The merge stage's own read
    (`// true` in merge-pipeline.sh, which takes false for true) moves to this rule in v3.1."""
    value = agents.get(key)
    if value is None: return True
    if type(value) is bool: return value
    warnings.append('agents.' + key + ' ' + json.dumps(value) + ' is not a JSON boolean; the merge gate counts it as true (required): only false switches the gate off')
    return True


GATE_NUMBER_CAP = 9999999


def gate_number(agents, key, default, warnings):
    """A number of the merge gate (agents.merge_min_required_checks,
    agents.merge_ci_start_grace_seconds, agents.merge_ci_queued_grace_seconds, and the
    review picker's agents.merge_gate_recheck_seconds), read by the one rule the gate itself uses
    (_merge_gate_number in bureau-config.sh): absent or null is <default>; a whole number
    from 0 is itself; a string that reads as a number (ASCII blanks around it and one
    leading "+" dropped, then digits with an optional fraction and exponent) is that
    number; a fraction is rounded up; a
    number above 9999999 is 9999999; a negative number, any other string, a boolean, an
    array or an object is <default>. Every case but absent, null and a plain whole number
    also warns, naming the number the gate uses."""
    value = agents.get(key)
    if value is None: return default
    number = None
    if type(value) in (int, float): number = value
    elif isinstance(value, str):
        text = value.strip(' \t\n\r\f\v')
        text = text[1:] if text.startswith('+') else text
        if re.fullmatch(r'[0-9]+(\.[0-9]+)?([eE][+-]?[0-9]+)?', text): number = float(text)
    if number is None or number < 0:
        used, plain = default, False
    else:
        used = GATE_NUMBER_CAP if number > GATE_NUMBER_CAP else min(math.ceil(number), GATE_NUMBER_CAP)
        plain = not isinstance(value, str) and number == math.floor(number) and number <= GATE_NUMBER_CAP
    if not plain:
        warnings.append('agents.' + key + ' ' + json.dumps(value) + ' should be a whole number of at least 0; the merge gate uses ' + str(used))
    return used


PULL_REQUEST_EVENTS = ('pull_request', 'pull_request_target')
REF_FILTER = re.compile(r'(?:^|[\s{,])(branches-ignore|branches|tags-ignore|tags)\s*:')


def push_reaches_every_branch(spec):
    """Whether a push trigger with these settings runs for a push to any branch. GitHub runs
    it for no branch when `branches` limits it (doctor cannot know the pull request's branch
    name) or when it names only tags; `branches-ignore` alone leaves the other branches in."""
    keys = set(REF_FILTER.findall(spec))
    if 'branches' in keys: return False
    return not (keys & {'tags', 'tags-ignore'}) or 'branches-ignore' in keys


def ci_trigger(text):
    """How a workflow's top-level `on:` can put a check on a pull request's head commit:
    'pull_request' (pull_request or pull_request_target), 'push' (a push trigger that runs
    for every branch: the gate counts check runs per commit, whatever event started them),
    'push-filtered' (a push trigger limited by branches or to tags), or None. A line reading,
    not a YAML parser: only the `on:` block counts, so a push-only workflow whose steps read
    github.event.pull_request is no pull_request workflow; a sequence may sit at column 0, a
    flow list or map may run over several lines, and event names may be quoted. The caller
    reads the file as utf-8-sig, so a byte order mark does not hide `on:`."""
    lines = [re.sub(r'(^|\s)#.*$', '', line.rstrip('\r')) for line in text.splitlines()]
    for index, line in enumerate(lines):
        match = re.match(r'''(?:on|"on"|'on')\s*:(.*)$''', line)
        if not match: continue
        inline, block = match.group(1).strip(), []
        for following in lines[index + 1:]:
            if following.strip() and not following[:1].isspace() and not re.match(r'-(\s|$)', following): break
            block.append(following)
        # A flow list or map that stays open continues until its brackets close.
        depth = sum(inline.count(c) for c in '[{') - sum(inline.count(c) for c in ']}')
        for following in lines[index + 1:]:
            if depth <= 0: break
            inline += '\n' + following
            depth += sum(following.count(c) for c in '[{') - sum(following.count(c) for c in ']}')
        if inline:
            events = {name: inline for name in re.findall(r'[\w-]+', inline)}
        else:
            # Events are the list items, or the keys at the block's first indentation, each
            # with the lines indented below it as its settings.
            events, current, indent = {}, None, None
            for following in block:
                if not following.strip(): continue
                depth = len(following) - len(following.lstrip())
                item = re.match(r'\s*-\s*["\']?([\w-]+)', following)
                key = re.match(r'\s*["\']?([\w-]+)["\']?\s*:(.*)$', following)
                if indent is None: indent = depth
                if depth <= indent and item:
                    current = item.group(1); events[current] = ''
                elif depth <= indent and key:
                    current = key.group(1); events[current] = key.group(2)
                elif current is not None:
                    events[current] += '\n' + following
        if any(name in PULL_REQUEST_EVENTS for name in events): return 'pull_request'
        pushes = [spec for name, spec in events.items() if name == 'push']
        if not pushes: return None
        return 'push' if any(push_reaches_every_branch(spec) for spec in pushes) else 'push-filtered'
    return None


def ci_gate_without_workflows(repo, minimum):
    """The warning for a merge gate that needs checks no workflow provides, or None.
    `minimum` is agents.merge_min_required_checks as the gate reads it (gate_number); a
    configured 0 lets a head without any check pass."""
    if minimum <= 0: return None
    directory = repo / '.github' / 'workflows'
    files = sorted(p for p in directory.iterdir() if p.is_file() and p.suffix in ('.yml', '.yaml')) if directory.is_dir() else []
    filtered = []
    for workflow in files:
        try: text = workflow.read_text(encoding='utf-8-sig', errors='replace')
        except OSError: continue
        trigger = ci_trigger(text)
        if trigger in ('pull_request', 'push'): return None
        if trigger == 'push-filtered': filtered.append(workflow.name)
    if not files:
        found = 'the repository has no workflow in .github/workflows'
    else:
        found = 'no workflow in .github/workflows runs on pull requests or on pushes to every branch'
        if filtered: found += '; push is limited by branches or to tags in ' + ', '.join(filtered)
        found += ' (checked: ' + ', '.join(p.name for p in files) + ')'
    unless = ('Unless those filters take in the pull request\'s branch, or another CI reports' if filtered else 'Unless another CI reports')
    return ('agents.merge_require_green_ci: automatic merges need at least ' + str(minimum) + ' completed check(s) on the pull request\'s head, but ' + found
            + '. ' + unless + ' checks or statuses to GitHub for the pull request\'s head commit, no automatic merge passes the gate. Add a workflow that runs on pull_request'
            + ' (bureau_install.py assets --scope ci scaffolds one), or set agents.merge_require_green_ci to false for a repository without CI, or agents.merge_mode to manual')


def uses_git_lfs(main):
    """True when an attributes file of the main checkout gives a pattern the Git LFS filter
    (filter=lfs, in any position among the pattern's attributes): its top-level .gitattributes, every
    other .gitattributes in it that git lists (tracked, or untracked and not ignored), and the git
    directory's info/attributes; a comment line does not count. LFS uploads its objects in its
    pre-push hook, which Bureau's remote git skips unless repo.remote_git_runs_hooks is true or
    "operator" (v3.2)."""
    files = [main / '.gitattributes', main / '.git' / 'info' / 'attributes']
    command = ['git', '-C', str(main), 'ls-files', '-z', '--cached', '--others', '--exclude-standard', '--', '*.gitattributes']
    try: listed = subprocess.run(command, capture_output=True, env=module('provider').process_env(command)).stdout
    except OSError: listed = b''
    files += [main / os.fsdecode(name) for name in listed.split(b'\0') if os.path.basename(os.fsdecode(name)) == '.gitattributes']
    for path in files:
        try: text = path.read_text(errors='replace')
        except OSError: continue
        if any(re.search(r'(^|\s)filter=lfs(\s|$)', line) for line in text.splitlines() if not line.lstrip().startswith('#')):
            return True
    return False


def operator_pre_push(main):
    """True when the hooks directory "operator" mode runs (hooks/ in the git common dir of the main
    checkout, as git() in bureau-env.sh resolves it) holds an executable pre-push hook."""
    command = ['git', '-C', str(main), 'rev-parse', '--git-common-dir']
    try: proc = subprocess.run(command, capture_output=True, text=True, env=module('provider').process_env(command))
    except OSError: return False
    common = proc.stdout.strip()
    if proc.returncode != 0 or not common: return False
    hook = (main / common).resolve() / 'hooks' / 'pre-push'
    return hook.is_file() and os.access(hook, os.X_OK)


def main_checkout(repo):
    """The checkout reset_worktree links from, resolved as bureau_link_worktree_paths does: the
    parent of the git common directory, so doctor run in a linked worktree judges the main
    checkout and not itself. Returns (path, None), or (None, reason) for a bare repository and
    for a git directory kept outside the main checkout (--separate-git-dir), where the stages
    make no links. Outside git the checkout is its own main checkout."""
    raw = subprocess.run(['git', '-C', str(repo), 'rev-parse', '--git-common-dir'], capture_output=True, text=True, env=module('provider').process_env(['git', '-C', str(repo), 'rev-parse', '--git-common-dir'])).stdout.strip()
    if not raw: return repo, None
    common = raw if os.path.isabs(raw) else os.path.join(str(repo), raw)
    bare = subprocess.run(['git', '--git-dir=' + common, 'rev-parse', '--is-bare-repository'], capture_output=True, text=True, env=module('provider').process_env(['git', '--git-dir=' + common, 'rev-parse', '--is-bare-repository'])).stdout.strip()
    if bare == 'true':
        return None, 'the repository is bare, so there is no main checkout to link from'
    # `cd "$common/.." && pwd -P`: the logical parent, then the physical path.
    main = Path(os.path.normpath(os.path.join(common, '..'))).resolve()
    if Path(common).resolve() != main / '.git':
        return None, 'the git directory is not inside the main checkout (--separate-git-dir), so there is no main checkout to link from'
    return main, None


def env_file(name):
    """The .env* family (.env, .env.local, .envrc, ...): files that hold a checkout's secrets."""
    return name.lower().startswith('.env')


def env_path(main, path):
    """A worktree_links entry that leads to a .env* name: any component of the entry, or of its fully
    resolved path relative to the main checkout (`alias/key` with `alias -> .envdir`; a target outside
    the checkout keeps the components after the common part). The stages apply the same rule
    (_bureau_link_worktree_path in bureau-config.sh)."""
    if any(env_file(part) for part in path.split('/')): return True
    if main is None: return False
    rel = os.path.relpath(os.path.realpath(os.path.join(str(main), path)), os.path.realpath(str(main)))
    return any(env_file(part) for part in rel.split(os.sep) if part not in ('', '.', '..'))


def env_inside(main, path):
    """For an entry that is a directory in the main checkout (links followed), the search the stages
    run before they link it (_bureau_link_worktree_path in bureau-config.sh), run the same way:
    `find -L <dir> -mindepth 1 -iname '.env*' -print -quit` from PATH, links followed, no depth or
    time limit, stopping at the first hit. Returns (hit, complete): the first .env* name found,
    relative to the main checkout, or None; and False when find did not finish cleanly (an
    unreadable subdirectory, a link loop where find reports one, no find at all), since what it did
    not see can hold a .env and the stages skip such a directory too. Not a directory: (None, True)."""
    target = os.path.join(str(main), path)
    if not os.path.isdir(target): return None, True
    try:
        proc = subprocess.run(['find', '-L', target, '-mindepth', '1', '-iname', '.env*', '-print', '-quit'],
                              stdin=subprocess.DEVNULL, capture_output=True)
    except OSError:
        return None, False
    if proc.returncode != 0: return None, False
    hit = os.fsdecode(proc.stdout).rstrip('\n')
    if not hit: return None, True
    prefix = str(main) + '/'
    return (hit[len(prefix):] if hit.startswith(prefix) else hit), True


def worktree_links(repo, config, checkout=None):
    """repo.worktree_links as reset_worktree applies it: (report, errors, warnings).
    Existence and tracking are judged in the main checkout the stages link from (`checkout`,
    as main_checkout returns it), also when doctor runs in a linked worktree. Ignore status is
    asked the way a stage worktree sees it: for a symlink, not for the main checkout's
    directory. A directory-only pattern like `.venv/` matches the directory here but not the
    link there, so the question runs in a temporary work tree that holds only this checkout's
    .gitignore files on the path and no file at the path itself. A .env* entry is an error:
    it would put the main checkout's secrets into every stage worktree, where pull-request
    code runs, and stage worktrees otherwise hold no .env (their reset runs git clean -fdx).
    So is a directory with a .env* name anywhere below it, and one whose search does not
    finish (env_inside): the stages skip both."""
    raw = config.get('repo', {}).get('worktree_links') if isinstance(config.get('repo'), dict) else None
    # Like the stages' `// []`: absent, null and false mean "no links".
    if raw is None or raw is False: return [], [], []
    if not isinstance(raw, list):
        return [], ['repo.worktree_links must be a list of relative paths; stages make no links'], []
    main, no_main = checkout if checkout is not None else main_checkout(repo)
    report, errors, warnings = [], [], []
    if no_main and raw: warnings.append('repo.worktree_links: ' + no_main + '; stages make no links')
    git_dir = subprocess.run(['git', '-C', str(repo), 'rev-parse', '--absolute-git-dir'], capture_output=True, text=True, env=module('provider').process_env(['git', '-C', str(repo), 'rev-parse', '--absolute-git-dir'])).stdout.strip()
    for entry in raw:
        if not isinstance(entry, str) or '\n' in entry:
            errors.append('repo.worktree_links entry ' + json.dumps(entry) + ' is not a one-line string'); continue
        path = entry.rstrip('/')
        parts = path.split('/')
        if not path or path.startswith('/') or any(part.lower() in ('', '.', '..', '.git') for part in parts):
            errors.append('repo.worktree_links entry ' + json.dumps(entry) + ' must be a plain relative path (no /, ., .., .git or empty component); stages skip it'); report.append(dict(path=entry, status='invalid')); continue
        if env_path(main, path):
            errors.append('repo.worktree_links entry ' + json.dumps(entry) + ' is a .env file: stages would link the main checkout\'s secrets into every stage worktree, where pull-request code runs; remove it (the stages read .env from the main checkout)')
            report.append(dict(path=path, status='env file')); continue
        if main is None:
            report.append(dict(path=path, status='no main checkout')); continue
        hit, complete = env_inside(main, path)
        if not complete:
            errors.append('repo.worktree_links entry ' + json.dumps(entry) + ' is a directory that could not be searched completely for .env files (an unreadable subdirectory, or a link loop where find reports one, as GNU find does): stages skip it, since what the search did not see can hold the main checkout\'s secrets; make it readable or remove the entry')
            report.append(dict(path=path, status='not searched completely')); continue
        if hit:
            errors.append('repo.worktree_links entry ' + json.dumps(entry) + ' is a directory that holds a .env file (' + hit + '): stages skip it, since a link would put the main checkout\'s secrets into every stage worktree, where pull-request code runs; move the .env file out or remove the entry')
            report.append(dict(path=path, status='holds an env file')); continue
        status = 'ok'
        tracked = subprocess.run(['git', '-C', str(main), '--literal-pathspecs', 'ls-files', '--', path], capture_output=True, text=True, env=module('provider').process_env(['git', '-C', str(main), '--literal-pathspecs', 'ls-files', '--', path])).stdout.strip() if git_dir else ''
        if tracked:
            status = 'tracked in the main checkout'
        elif not (main / path).exists():
            status = 'missing in the main checkout'
        elif git_dir:
            with tempfile.TemporaryDirectory(prefix='bureau-doctor-') as shadow:
                for depth in range(len(parts)):
                    ignore = repo.joinpath(*parts[:depth], '.gitignore')
                    if ignore.is_file():
                        target = Path(shadow).joinpath(*parts[:depth], '.gitignore')
                        target.parent.mkdir(parents=True, exist_ok=True); shutil.copyfile(ignore, target)
                probe = subprocess.run(['git', '--git-dir=' + git_dir, '--work-tree=' + shadow, '-C', shadow, 'check-ignore', '-q', '--no-index', '--', path], capture_output=True, text=True, env=module('provider').process_env(['git', '--git-dir=' + git_dir, '--work-tree=' + shadow, '-C', shadow, 'check-ignore', '-q', '--no-index', '--', path]))
            if probe.returncode != 0:
                status = 'not ignored as a symlink'
        if status == 'tracked in the main checkout':
            warnings.append('repo.worktree_links: ' + path + ' is tracked in the main checkout, so stages skip it as a path the branch tracks')
        elif status == 'missing in the main checkout':
            warnings.append('repo.worktree_links: ' + path + ' does not exist in the main checkout; stages skip it')
        elif status == 'not ignored as a symlink':
            warnings.append('repo.worktree_links: ' + path + ' is not ignored as a symlink, so stages skip it (a link there would leave the worktree dirty); a pattern with a trailing slash matches directories only, add ' + path + ' without it to .gitignore')
        report.append(dict(path=path, status=status))
    return report, errors, warnings


def claude_logged_in(provider, options, mode):
    """`claude auth status --json` in the environment a Claude stage gets (its untrusted_env mode,
    CLAUDE_CONFIG_DIR from config_dir), as auth() in bureau-provider.py asks it before every call.
    Returns (logged_in, reason)."""
    if not shutil.which('claude'): return False, 'claude executable not found on PATH'
    try:
        proc = subprocess.run(['claude', 'auth', 'status', '--json'], capture_output=True, text=True, timeout=20, stdin=subprocess.DEVNULL,
                              env=provider.claude_env(options, env=provider.untrusted_env(os.environ, mode, 'claude')))
    except (OSError, subprocess.SubprocessError) as exc: return False, 'claude auth status failed: ' + str(exc)
    try: logged = proc.returncode == 0 and json.loads(proc.stdout).get('loggedIn') is True
    except (ValueError, AttributeError): logged = False
    return logged, None if logged else 'claude auth status --json did not report loggedIn true (exit ' + str(proc.returncode) + ')'


def claude_config_dirs(config, effective, provider, warnings):
    """Per enabled Claude stage, the configuration directory its claude child gets: config_dir
    (BUREAU_CLAUDE_CONFIG_DIR, else agents.providers.claude.config_dir), with whether Claude is
    logged in there; without one, the directory the child inherits (CLAUDE_CONFIG_DIR, else
    ~/.claude), which is the operator's own setup and is not checked here."""
    try: mode = provider.untrusted_env_mode(config)
    except provider.UntrustedEnvError: mode = 'default'
    report, checked = {}, {}
    for stage, options in effective.items():
        if options['runner'] != 'claude': continue
        directory = options.get('config_dir')
        if not directory:
            inherited = os.environ.get('CLAUDE_CONFIG_DIR') or os.path.join(os.path.expanduser('~'), '.claude')
            report[stage] = dict(config_dir=inherited, configured=False, logged_in='not checked'); continue
        if directory not in checked: checked[directory] = claude_logged_in(provider, options, mode)
        logged, reason = checked[directory]
        report[stage] = dict(config_dir=directory, configured=True, logged_in=logged)
        if not logged: report[stage]['reason'] = reason
    for directory, (logged, reason) in checked.items():
        if not logged:
            stages = ', '.join(stage for stage, entry in report.items() if entry['config_dir'] == directory and entry['configured'])
            warnings.append('Claude is not logged in in the configuration directory ' + directory + ' (' + stages + '): ' + reason
                            + '; the stage ends with 16 before any call. Log in with CLAUDE_CONFIG_DIR=' + directory + ' claude')
    return report


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
    if not config.get('repo', {}).get('test_command'):
        # BUREAU_RUNNER_IMPLEMENT is a .env key the stages load; the stage's value decides.
        stage_env = {key: value for key, value in os.environ.items() if key != 'BUREAU_RUNNER_IMPLEMENT'}
        runner_override = stage_env_value(path, 'BUREAU_RUNNER_IMPLEMENT')
        if runner_override is not None: stage_env['BUREAU_RUNNER_IMPLEMENT'] = runner_override
        if implement_runner(config, provider, stage_env) == 'codex':
            warnings.append('repo.test_command is missing; required for Codex background implementation')
    short = [stage + ' ' + format(effective[stage]['timeout'], 'g') + ' s' for stage in LONG_CALL_STAGES
             if stage in effective and effective[stage]['timeout'] < LONG_CALL_MIN_SECONDS]
    if short:
        warnings.append('Provider timeout below ' + str(LONG_CALL_MIN_SECONDS) + ' s per call: ' + ', '.join(short) + '. Spec, spec review, UX, QA and review calls'
                        ' often run 15 to 30 minutes and end with 124 when cut off; raise agents.<stage>.timeout_seconds or agents.providers.<runner>.timeout_seconds (default 3600)'
                        + ('; BUREAU_STAGE_TIMEOUT in the environment wins over both' if os.environ.get('BUREAU_STAGE_TIMEOUT') else ''))
    if stage_env_file(path).is_file(): warnings.append('Doctor resolves JSON and process environment only; it does not execute .env (it reads only BUREAU_RUNNER_IMPLEMENT from it, for the repo.test_command warning). Source trusted overrides before running doctor for matching effective settings.')
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
    source = template_source(manifest)
    if source['status'] == 'stale': warnings.append('Recorded template source is stale: ' + source['reason'])
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
    merge, raw_merge = merge_mode(config)
    if not (raw_merge is None or raw_merge in ('auto', 'manual')):
        warnings.append('agents.merge_mode ' + json.dumps(raw_merge) + ' is not "auto" or "manual"; the pipelines fall closed to manual (no automatic merge or rebase)')
    if merge == 'manual' and not config['linear']['teams'][0].get('states', {}).get('merge'):
        errors.append('agents.merge_mode is manual but linear.teams[0].states.merge is not set: code review refuses with 24; configure the Merge state or set merge_mode to auto')
    require_ci = gate_switch(config['agents'], 'merge_require_green_ci', warnings)
    gate_switch(config['agents'], 'merge_require_up_to_date', warnings)
    minimum = gate_number(config['agents'], 'merge_min_required_checks', 1, warnings)
    gate_number(config['agents'], 'merge_ci_start_grace_seconds', 1800, warnings)
    gate_number(config['agents'], 'merge_ci_queued_grace_seconds', 3600, warnings)
    gate_number(config['agents'], 'merge_gate_recheck_seconds', 3600, warnings)
    if merge == 'auto' and require_ci and any(runtime.enabled(config, stage) for stage in ('code_review', 'merge')):
        ci = ci_gate_without_workflows(repo, minimum)
        if ci: warnings.append(ci)
    implement = config['agents'].get('implement')
    push = implement.get('push_each_iteration') if isinstance(implement, dict) else None
    if push is not None and type(push) is not bool:
        warnings.append('agents.implement.push_each_iteration ' + json.dumps(push) + ' is not a JSON boolean; the implement stage counts it as true and pushes after every iteration')
    repo_cfg = config.get('repo') if isinstance(config.get('repo'), dict) else {}
    hook = repo_cfg.get('post_implement_command')
    if hook is not None and hook is not False and not isinstance(hook, str):
        errors.append('repo.post_implement_command must be a string; the implement stage would run ' + json.dumps(hook) + ' as a shell command')
    # v3.2: the git function in bureau-env.sh (_bureau_remote_git_hooks_mode) runs Bureau's push, fetch and
    # other remote git commands with every hook for the JSON value true ("on"), with only the hooks in the git
    # common dir's hooks/ for the string "operator", and without hooks for anything else ("off").
    remote_hooks = repo_cfg.get('remote_git_runs_hooks')
    remote_mode = 'on' if remote_hooks is True else 'operator' if remote_hooks == 'operator' else 'off'
    if remote_hooks is not None and type(remote_hooks) is not bool and remote_hooks != 'operator':
        warnings.append('repo.remote_git_runs_hooks ' + json.dumps(remote_hooks) + ' is not a JSON boolean or "operator"; Bureau counts it as false and runs its push, fetch and other remote git commands without the repository\'s hooks: only true runs them all, "operator" only those in the main checkout\'s own hooks directory')
    checkout = main_checkout(repo); main = checkout[0]
    if remote_mode == 'off' and main is not None and uses_git_lfs(main):
        warnings.append('repo.remote_git_runs_hooks is not true, but the main checkout uses Git LFS (filter=lfs in a .gitattributes file or in info/attributes): Bureau pushes without the repository\'s hooks, so the pre-push hook of git lfs does not upload the LFS objects and the remote lacks them; set repo.remote_git_runs_hooks to true, or to "operator" when git lfs install put its hooks in the main checkout\'s .git/hooks')
    if remote_mode == 'operator' and main is not None and uses_git_lfs(main) and not operator_pre_push(main):
        warnings.append('repo.remote_git_runs_hooks is "operator" and the main checkout uses Git LFS, but the git common dir\'s hooks directory has no executable pre-push hook: Bureau runs only that directory\'s hooks, so the LFS objects are not uploaded; run git lfs install in the main checkout without core.hooksPath set, or set repo.remote_git_runs_hooks to true')
    links, link_errors, link_warnings = worktree_links(repo, config, checkout)
    errors.extend(link_errors); warnings.extend(link_warnings)
    claude_dirs = claude_config_dirs(config, effective, provider, warnings)
    return dict(ok=not errors, mode=mode, workspace=str(repo), config=str(path), version=config.get('version', 1),
                merge_mode=merge, post_implement_command=hook if isinstance(hook, str) and hook.strip() else None,
                remote_git_hooks=remote_mode,
                main_checkout=str(main) if main is not None else None, worktree_links=links, claude_config_dirs=claude_dirs,
                interfaces=interfaces, active_integration=active.get('integration'), effective_stages=effective,
                template_source=source, drift=drift, errors=errors, warnings=warnings,
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
