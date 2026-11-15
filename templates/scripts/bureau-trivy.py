#!/usr/bin/env python3
"""Verified Trivy binary, operator-refreshed cache and offline final-head QA evidence."""
import argparse
from collections import Counter
from datetime import datetime, timezone
import importlib.util
import json
import os
from pathlib import Path, PurePosixPath
import platform
import re
import shutil
import subprocess
import sys
import tempfile

try:
    import tomllib
except ImportError:  # Python 3.9/3.10: the conservative name reader below.
    tomllib = None

sys.dont_write_bytecode = True
SCRIPTS = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('trivy_scan', SCRIPTS / 'bureau-scan.py')
scan = importlib.util.module_from_spec(spec)
spec.loader.exec_module(scan)
# The generic object reader uses the same boundary as the merged Semgrep reader.
source = scan
VERSION = '0.76.1'
PINS = SCRIPTS / 'bureau-trivy-pins.json'
EXCEPTIONS = '.bureau/trivy-exceptions.json'
CACHE_RECORD = 'bureau-trivy-cache.json'
LEVELS = dict(UNKNOWN=0, LOW=1, MEDIUM=2, HIGH=3, CRITICAL=4)
# All release-dependent flags live here, also used by the real-binary qualifier.
FLAGS = dict(
    scan=['--offline-scan', '--skip-db-update', '--skip-java-db-update', '--skip-check-update',
          '--skip-vex-repo-update', '--skip-version-check', '--disable-telemetry'],
    refresh=['--skip-version-check', '--disable-telemetry'],
    config='--config', ignore='--ignorefile', cache='--cache-dir', format=['--format', 'json'],
    output='--output', scanners=['--scanners', 'vuln,misconfig'], timeout=['--timeout', '5m'],
    db='--download-db-only', java='--download-java-db-only')
NATIVE = {'trivy.yaml', 'trivy.yml', '.trivyignore', '.trivyignore.yaml', '.trivyignore.yml'}
LOCKS = {'package-lock.json', 'npm-shrinkwrap.json', 'pnpm-lock.yaml', 'yarn.lock', 'uv.lock', 'poetry.lock', 'go.sum'}
OTHER = {'Cargo.toml', 'Cargo.lock', 'Gemfile', 'Gemfile.lock', 'composer.json', 'composer.lock',
         'pom.xml', 'build.gradle', 'build.gradle.kts', 'packages.lock.json'}


class Unavailable(ValueError):
    pass


class NotMeasured(ValueError):
    pass


def settings(config, repo=None):
    if not isinstance(config, dict): raise ValueError('configuration must be an object')
    qa = config.get('qa', {})
    if not isinstance(qa, dict): raise ValueError('qa must be an object')
    raw = qa.get('trivy', {})
    defaults = dict(enabled=False, mode='advisory', cache_dir=None, max_db_age_hours=48,
                    block_severity='HIGH')
    if not isinstance(raw, dict) or set(raw) - set(defaults):
        raise ValueError('qa.trivy contains unknown keys or is not an object')
    options = {**defaults, **raw}
    if type(options['enabled']) is not bool: raise ValueError('qa.trivy.enabled must be boolean')
    if options['mode'] not in ('advisory', 'blocking'): raise ValueError('qa.trivy.mode must be advisory or blocking')
    if not isinstance(options['block_severity'], str) or options['block_severity'] not in LEVELS:
        raise ValueError('qa.trivy.block_severity must be UNKNOWN, LOW, MEDIUM, HIGH or CRITICAL')
    age = options['max_db_age_hours']
    if type(age) not in (int, float) or not 0 < age < float('inf'):
        raise ValueError('qa.trivy.max_db_age_hours must be a positive finite number')
    path = options['cache_dir']
    if path is not None and (not isinstance(path, str) or not path or '\0' in path or not Path(path).expanduser().is_absolute()):
        raise ValueError('qa.trivy.cache_dir must be null or an absolute path outside the repository')
    if options['enabled'] and path is None: raise ValueError('qa.trivy.cache_dir is required when enabled')
    if path is not None and repo is not None:
        cache = Path(path).expanduser().resolve()
        if cache == repo.resolve() or repo.resolve() in cache.parents:
            raise ValueError('qa.trivy.cache_dir must be outside the repository')
    return options


def platform_key():
    system = {'Linux':'linux', 'Darwin':'darwin'}.get(platform.system(), platform.system().lower())
    arch = {'aarch64':'arm64', 'AMD64':'x86_64'}.get(platform.machine(), platform.machine())
    return system + '-' + arch


def pin_data(path):
    value = json.loads(path.read_bytes())
    if not isinstance(value, dict) or value.get('version') != VERSION or value.get('tool') != 'trivy' or value.get('schema_version') != 1:
        raise Unavailable('Trivy pin metadata mismatch')
    key = platform_key()
    platforms = value.get('platforms')
    if not isinstance(platforms, dict): raise Unavailable('invalid Trivy platform pins')
    pin = platforms.get(key)
    if pin is None: raise Unavailable('missing Trivy pin for ' + key)
    if (not isinstance(pin, dict) or not isinstance(pin.get('binary_sha256'), str)
            or not re.fullmatch(r'[0-9a-f]{64}', pin['binary_sha256'])
            or type(pin.get('binary_size')) is not int or pin['binary_size'] < 1):
        raise Unavailable('invalid Trivy binary pin')
    return pin


def verify_copy(binary, root, pins):
    try:
        pin = pin_data(pins)
        return scan.verified_binary(binary, root / 'bin', pin, 'trivy'), pin
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise Unavailable(str(exc) if isinstance(exc, ValueError) else 'Trivy binary absent or not regular') from exc


def timestamp(value):
    if not isinstance(value, str): raise NotMeasured('invalid database metadata')
    try:
        result = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if result.tzinfo is None: raise ValueError()
        return result.astimezone(timezone.utc)
    except ValueError as exc: raise NotMeasured('invalid database timestamp') from exc


def metadata(cache, java=False):
    result = {}
    for name in ('db', 'java-db') if java else ('db',):
        try:
            value = json.loads((cache / name / 'metadata.json').read_bytes())
            row = {k:value[k] for k in ('Version', 'UpdatedAt', 'NextUpdate', 'DownloadedAt')}
            if type(row['Version']) is not int or row['Version'] < 1: raise ValueError()
            for key in ('UpdatedAt', 'NextUpdate', 'DownloadedAt'): timestamp(row[key])
            if not (cache / name / ('trivy.db' if name == 'db' else 'trivy-java.db')).is_file(): raise ValueError()
            result[name] = row
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise NotMeasured('database metadata or content unavailable: ' + name) from exc
    return result


def cache_snapshot(cache, root, max_age, now=None):
    """Check the bytes actually copied, including metadata and external bundles."""
    files, current = None, None
    try:
        # Read the small record with the same regular-file boundary as cache data.
        record_path = root / 'cache-record.json'
        scan.copy_regular(cache / CACHE_RECORD, record_path)
        record = json.loads(record_path.read_bytes())
        if not isinstance(record, dict): raise NotMeasured('invalid cache record')
        private = root / 'db-cache'
        files = scan.directory_files(cache, private, exclude=(CACHE_RECORD,))
        if (record.get('schema_version') != 1 or record.get('version') != VERSION or record.get('files') != files
                or record.get('digest') != scan.digest(files)):
            raise NotMeasured('cache digest mismatch')
        current = metadata(private, java=(private / 'java-db').exists())
        if record.get('metadata') != current: raise NotMeasured('cache metadata mismatch')
        current_time = now or datetime.now(timezone.utc)
        for row in current.values():
            age = (current_time - timestamp(row['UpdatedAt'])).total_seconds() / 3600
            if age > max_age: raise NotMeasured('stale database')
            if age < -1: raise NotMeasured('database UpdatedAt is in the future')
        # D2: embedded checks only. Whitelist the two database directories, so
        # an external checks bundle cannot hide under a different cache path.
        removed = []
        for entry in private.iterdir():
            if entry.name in ('db', 'java-db'): continue
            removed.append(entry.name)
            if entry.is_dir(): shutil.rmtree(entry)
            else: entry.unlink()
        return private, record['digest'], current['db']['UpdatedAt'], sorted(removed)
    except NotMeasured as exc:
        exc.db_digest = scan.digest(files) if files is not None else None
        exc.db_updated_at = current['db']['UpdatedAt'] if current is not None else None
        raise
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise NotMeasured('cache record or regular cache files unavailable') from exc


def require_binary(binary, pin):
    if scan.copy_regular(binary) != (pin['binary_sha256'], pin['binary_size']):
        raise Unavailable('private Trivy binary changed before execution')


def refresh(cache, *, java=False, repo=None, _binary=None, _pins=None):
    """The sole networked operation. Never called by a pipeline or measure()."""
    cache = Path(cache).expanduser().absolute()
    if repo is not None and (cache.resolve() == repo.resolve() or repo.resolve() in cache.resolve().parents):
        raise ValueError('refresh cache must be outside the repository')
    if cache.is_symlink() or (cache.exists() and (not cache.is_dir()
            or (any(cache.iterdir()) and not (cache / CACHE_RECORD).is_file()))):
        raise ValueError('refresh destination must be empty or a Bureau-managed cache')
    state = scan.state_dir(repo or Path.cwd())
    binary_path = Path(_binary or os.environ.get('BUREAU_TRIVY_BIN') or state / 'tools/trivy/trivy').expanduser().absolute()
    with tempfile.TemporaryDirectory(prefix='bureau-trivy-refresh-') as temporary:
        root = Path(temporary).resolve()
        binary, pin = verify_copy(binary_path, root, _pins or PINS)
        env = scan.environment(root)
        empty = root / 'empty.yaml'; empty.write_text('')
        # Download into a fresh private cache; failed refreshes leave the last
        # operator cache untouched. No launcher or config comes from the repo.
        download = root / 'download'; download.mkdir(mode=0o700)
        for flag in (FLAGS['db'], FLAGS['java']) if java else (FLAGS['db'],):
            require_binary(binary, pin)
            proc = scan.run_process([str(binary), 'image', *FLAGS['refresh'], FLAGS['config'], str(empty),
                                     FLAGS['cache'], str(download), flag], cwd=root, env=env, timeout=600)
            if proc.returncode: raise NotMeasured('Trivy database refresh failed')
        values = metadata(download, java=java)
        files = scan.directory_files(download)
        record = dict(schema_version=1, version=VERSION, metadata=values, files=files, digest=scan.digest(files))
        scan.write_json(download / CACHE_RECORD, record)
        cache.parent.mkdir(parents=True, exist_ok=True)
        # Cache updates are explicit operator operations; concurrent scans copy
        # and compare the whole cache and fail closed on partial replacement.
        if cache.is_symlink() or (cache.exists() and not cache.is_dir()): raise ValueError('cache must be a real directory')
        if cache.exists(): shutil.rmtree(cache)
        shutil.copytree(download, cache)
        return record


def native(name):
    return PurePosixPath(name).name in NATIVE


def iac(name):
    path = PurePosixPath(name); lower = path.name.lower()
    return (lower.startswith('dockerfile') or lower.endswith('.dockerfile')
            or path.suffix.lower() in ('.tf', '.yaml', '.yml', '.json', '.hcl', '.bicep', '.template'))


def remove_inline_ignores(data):
    lines, removed = data.split(b'\n'), []
    for index, line in enumerate(lines):
        if b'trivy:ignore:' not in line: continue
        # Ignore markers are comments, including trailing IaC comments. Keep
        # code before a comment and retain its LF slot so cause lines stay exact.
        match = re.search(rb'#|//|/\*|<!--', line)
        if match and b'trivy:ignore:' in line[match.start():]:
            lines[index] = line[:match.start()].rstrip()
            removed.append(index + 1)
    return b'\n'.join(lines), removed


def snapshots(repo, trees, root):
    sides, manifests = {}, {}
    for side in ('base', 'head'):
        directory = root / side; directory.mkdir(mode=0o700)
        manifest = dict(files=[], removed_native_config=[], removed_inline_ignores=[], skipped_nonregular=[])
        for name, entry in sorted(trees[side].items()):
            if native(name): manifest['removed_native_config'].append(name); continue
            if not scan.relative(name): raise ValueError('unsafe snapshot path')
            if entry[0] not in ('100644', '100755') or entry[1] != 'blob':
                manifest['skipped_nonregular'].append(name); continue
            data = source.blob(repo, trees[side], name, limit=32 * 1024 * 1024)
            if iac(name):
                data, removed = remove_inline_ignores(data)
                manifest['removed_inline_ignores'] += [dict(file=name, line=line) for line in removed]
            target = directory / name; target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            target.write_bytes(data)
            if entry[0] == '100755': target.chmod(0o700)
            manifest['files'].append(dict(file=name, sha256=scan.sha(data)))
        sides[side], manifests[side] = directory, manifest
    return sides, manifests


def normalized(name):
    return re.sub(r'[-_.]+', '-', name).lower()


def requirement_name(value):
    match = re.match(r'^\s*([A-Za-z0-9][A-Za-z0-9._-]*)(?:\[[^\]]*\])?(?=\s|[<>=!~;@]|$)', value)
    if not match: raise ValueError('unsupported dependency declaration')
    return normalized(match[1])


def fallback_toml_names(text, lock=False):
    """Dependency names only, also on Python 3.9/3.10 without a TOML dependency.

    Unsupported relevant syntax fails coverage closed. This is not a TOML
    interpreter: versions are deliberately not compared in v1.
    """
    def clean(value):
        # Comments and array brackets inside quoted requirement extras do not
        # terminate a dependency array. Reject multiline strings in this subset.
        output, quote, escaped, depth = [], None, False, 0
        for char in value:
            if quote:
                output.append(char)
                if escaped: escaped = False
                elif char == '\\' and quote == '"': escaped = True
                elif char == quote: quote = None
            elif char == '#': break
            else:
                output.append(char)
                if char in ('"', "'"): quote = char
                elif char == '[': depth += 1
                elif char == ']': depth -= 1
        if quote: raise ValueError('unsupported TOML string or array')
        return ''.join(output), depth

    def strings(value):
        value = value.strip()
        if not value.startswith('[') or not value.endswith(']'): raise ValueError('unsupported dependency array')
        token = re.compile(r'''"(?:\\.|[^"\\])*"|'[^']*' ''', re.X)
        result, tail = [], value[1:-1]
        while tail.strip():
            tail = tail.lstrip()
            match = token.match(tail)
            if not match: raise ValueError('unsupported dependency string')
            item = match[0]
            result.append(json.loads(item) if item.startswith('"') else item[1:-1])
            tail = tail[match.end():].lstrip()
            if not tail: break
            if not tail.startswith(','): raise ValueError('unsupported dependency array separator')
            tail = tail[1:]
        return result

    def poetry_group_dependencies(section):
        # A group is one TOML key. Deeper metadata tables are not dependencies.
        return re.fullmatch(r'''tool\.poetry\.group\.(?:[A-Za-z0-9_-]+|"[^"]+"|'[^']+')\.dependencies''', section)

    result, section, pending, balance = set(), '', '', 0
    for raw in text.split('\n'):
        line, depth = clean(raw)
        line = line.strip()
        if not line or line.startswith('#'): continue
        if pending:
            pending += '\n' + line
            balance += depth
            if balance < 0: raise ValueError('unsupported dependency array')
            if balance: continue
            line, pending = pending, ''
        table = re.fullmatch(r'\[\[([^\[\]]+)\]\]|\[([^\[\]]+)\]', line)
        if table:
            section = (table[1] or table[2]).strip()
            array_table = table[1] is not None
            if array_table and section.startswith(('project', 'tool.poetry')):
                raise ValueError('unsupported dependency array table')
            if lock and section == 'package' and not array_table:
                raise ValueError('lock packages must be array tables')
            if (('"' in section or "'" in section) and not poetry_group_dependencies(section)):
                raise ValueError('unsupported quoted TOML table')
            if (section.startswith(('tool.poetry.dependencies.', 'tool.poetry.dev-dependencies.'))
                    or section.startswith(('project.dependencies', 'project.optional-dependencies.'))
                    or (section.startswith('tool.poetry.group.') and '.dependencies.' in section)):
                raise ValueError('unsupported nested dependency table')
            continue
        if line.startswith('['): raise ValueError('unsupported TOML table')
        if '=' in line:
            key = line.split('=', 1)[0].strip()
            if (key.startswith(('"', "'")) or (section == '' and key.startswith(('project', 'tool')))
                    or (section == 'tool' and key.startswith('poetry'))
                    or (section == 'tool.poetry' and key.startswith(('dependencies', 'dev-dependencies', 'group')))
                    or (section == 'project' and key.startswith(('dependencies.', 'optional-dependencies')))
                    or (section == 'tool.poetry.group')
                    or (section.startswith('tool.poetry.group.') and not section.endswith('.dependencies') and key != 'optional')):
                raise ValueError('unsupported TOML dependency key or inline table')
        if lock:
            if section == 'package' and re.match(r'name\s*=', line):
                match = re.match(r'''name\s*=\s*["']([^"']+)["']''', line)
                if not match: raise ValueError('unsupported lock package name')
                result.add(normalized(match[1]))
            continue
        if section == 'project' or section == 'project.optional-dependencies':
            if '=' not in line: continue
            key, value = line.split('=', 1)
            if section == 'project' and key.strip() not in ('dependencies', 'dynamic'): continue
            _, balance = clean(value)
            if balance:
                pending = line; continue
            items = strings(value)
            if key.strip() == 'dynamic':
                if 'dependencies' in items: raise ValueError('dynamic dependencies')
            else:
                result.update(requirement_name(item) for item in items)
        elif section in ('tool.poetry.dependencies', 'tool.poetry.dev-dependencies') or poetry_group_dependencies(section):
            match = re.match(r'''([A-Za-z0-9_.-]+|"[^"]+"|'[^']+')\s*=''', line)
            if not match: raise ValueError('unsupported Poetry dependency')
            name = match[1].strip('"\'')
            if name.lower() != 'python': result.add(normalized(name))
    if pending: raise ValueError('incomplete dependency array')
    return result


def toml_names(text, lock=False):
    if tomllib is None: return fallback_toml_names(text, lock)
    value = tomllib.loads(text)
    if lock:
        packages = value.get('package', [])
        if not isinstance(packages, list) or any(not isinstance(row, dict) or not isinstance(row.get('name'), str) for row in packages):
            raise ValueError('invalid lock package array')
        return {normalized(row['name']) for row in packages}
    result = set()
    project = value.get('project', {})
    if not isinstance(project, dict): raise ValueError('invalid project table')
    if 'dependencies' in project.get('dynamic', []): raise ValueError('dynamic dependencies')
    optional = project.get('optional-dependencies', {})
    if not isinstance(optional, dict): raise ValueError('invalid optional dependencies')
    groups = [project.get('dependencies', []), *optional.values()]
    for rows in groups:
        if not isinstance(rows, list): raise ValueError('invalid dependency array')
        result.update(requirement_name(row) for row in rows)
    tools = value.get('tool', {})
    if not isinstance(tools, dict): raise ValueError('invalid tool table')
    poetry = tools.get('poetry', {})
    if not isinstance(poetry, dict): raise ValueError('invalid Poetry table')
    groups = [poetry.get('dependencies', {}), poetry.get('dev-dependencies', {})]
    poetry_groups = poetry.get('group', {})
    if not isinstance(poetry_groups, dict) or any(not isinstance(row, dict) for row in poetry_groups.values()):
        raise ValueError('invalid Poetry groups')
    groups += [row.get('dependencies', {}) for row in poetry_groups.values()]
    for rows in groups:
        if not isinstance(rows, dict): raise ValueError('invalid Poetry dependencies')
        result.update(normalized(name) for name in rows if name.lower() != 'python')
    return result


def js_names(value):
    if not isinstance(value, dict): raise ValueError('invalid package manifest')
    result = set()
    for key in ('dependencies', 'devDependencies', 'optionalDependencies'):
        rows = value.get(key, {})
        if not isinstance(rows, dict): raise ValueError('invalid package dependency table')
        if any(not re.fullmatch(r'(?:@[A-Za-z0-9_.-]+/)?[A-Za-z0-9_.-]+', name) for name in rows):
            raise ValueError('invalid dependency name')
        result.update(rows)
    return result


def pnpm_names(text):
    rows = text.split('\n'); result = set(); importer_indent = root_indent = table_indent = None
    # Only the root importer can prove direct dependency consistency.
    in_importers = False
    for raw in rows:
        line = raw.strip(); indent = len(raw) - len(raw.lstrip(' '))
        if not line or line.startswith('#'): continue
        if indent == 0:
            in_importers = line == 'importers:'; importer_indent = root_indent = table_indent = None; continue
        if not in_importers: continue
        if importer_indent is None: importer_indent = indent
        if indent <= importer_indent:
            root_indent = table_indent = None
            if indent == importer_indent and re.fullmatch(r'''(?:\.|"\."|'\.'):''', line): root_indent = indent
            continue
        if root_indent is None: continue
        if table_indent is not None and indent <= table_indent: table_indent = None
        if indent == root_indent + 2 and line in ('dependencies:', 'devDependencies:', 'optionalDependencies:'):
            table_indent = indent; continue
        if table_indent is not None and indent == table_indent + 2:
            match = re.match(r'''(?:'([^']+)'|"([^"]+)"|([^:]+)):\s*(?:#.*)?$''', line)
            if not match: raise ValueError('unsupported pnpm dependency key')
            result.add(next(item for item in match.groups() if item is not None))
    return result


def lock_names(path):
    text = path.read_text()
    if path.name in ('package-lock.json', 'npm-shrinkwrap.json'):
        value = json.loads(text)
        if not isinstance(value, dict) or not isinstance(value.get('packages'), dict):
            raise ValueError('invalid lock packages table')
        root = value['packages'].get('')
        if root is None: raise ValueError('lockfile lacks root packages entry')
        return js_names(root)
    if path.name == 'pnpm-lock.yaml': return pnpm_names(text)
    if path.name == 'yarn.lock':
        result = set()
        for line in text.split('\n'):
            if line and not line[0].isspace() and not line.startswith('#') and line.endswith(':'):
                for match in re.finditer(r'(?:^|,\s*)["\']?(@?[^@,"\s]+(?:/[^@,"\s]+)?)@', line): result.add(match[1])
        return result
    return toml_names(text, lock=True)


def inventory_name(name):
    path = PurePosixPath(name); leaf = path.name; lower = leaf.lower()
    return (leaf in LOCKS | OTHER | {'package.json', 'pyproject.toml', 'go.mod'}
            or bool(re.fullmatch(r'requirements[^/]*\.txt', leaf))
            or lower.startswith('dockerfile') or lower.endswith('.dockerfile')
            or bool(re.fullmatch(r'(?:docker-)?compose(?:[.-].+)?\.ya?ml', lower)) or path.suffix == '.tf')


def inventory(directory, skipped=()):
    names = {p.relative_to(directory).as_posix() for p in directory.rglob('*') if p.is_file()}
    all_names = names | set(skipped)
    targets, issues, unchecked = set(), [], []
    def issue(name, reason): issues.append(dict(file=name, reason=reason))
    for name in sorted(all_names):
        path = PurePosixPath(name); leaf = path.name; parent = path.parent
        if leaf in OTHER:
            unchecked.append(name)
            targets.add(name)
        if name in skipped:
            if inventory_name(name):
                issue(name, 'non-regular inventory input')
            continue
        if leaf in LOCKS:
            targets.add(name)
        if leaf == 'package.json' or leaf == 'pyproject.toml':
            candidates = ('package-lock.json', 'npm-shrinkwrap.json', 'pnpm-lock.yaml', 'yarn.lock') if leaf == 'package.json' else ('uv.lock', 'poetry.lock')
            locks = [str(parent / candidate) for candidate in candidates if str(parent / candidate) in names]
            if not locks: issue(name, 'manifest without lockfile'); continue
            try:
                declared = js_names(json.loads((directory / name).read_text())) if leaf == 'package.json' else toml_names((directory / name).read_text())
                for lock in locks:
                    for missing in sorted(declared - lock_names(directory / lock)):
                        issue(name, 'declared dependency missing from ' + lock + ': ' + missing)
            except (OSError, ValueError, KeyError, TypeError, UnicodeError): issue(name, 'lockfile consistency not measured')
        elif leaf == 'go.mod':
            targets.add(name)
            if str(parent / 'go.sum') not in names: issue(name, 'manifest without lockfile')
            unchecked.append(name)
        elif re.fullmatch(r'requirements[^/]*\.txt', leaf):
            targets.add(name)
            for raw in (directory / name).read_text().split('\n'):
                raw = raw.strip()
                if not raw or raw.startswith('#') or raw.startswith('--hash='): continue
                if not re.match(r'^[A-Za-z0-9][A-Za-z0-9._-]*(?:\[[^\]]+\])?\s*==\s*[^\s*;,]+(?:\s|;|$)', raw):
                    issue(name, 'requirements dependency is not exactly pinned'); break
        elif (leaf.lower().startswith('dockerfile') or leaf.lower().endswith('.dockerfile')
              or re.fullmatch(r'(?:docker-)?compose(?:[.-].+)?\.ya?ml', leaf.lower())
              or path.suffix == '.tf'):
            targets.add(name)
    return dict(targets=sorted(targets), issues=issues, consistency_unchecked=sorted(unchecked), applicable=bool(targets or issues or unchecked))


def exceptions(data):
    rows = json.loads(data) if data is not None else []
    if not isinstance(rows, list): raise ValueError('base trivy exceptions must be an array')
    for row in rows:
        if (not isinstance(row, dict) or set(row) - {'kind', 'id', 'package', 'file', 'owner', 'reason', 'expires'}
                or row.get('kind', 'finding') not in ('finding', 'coverage')
                or any(not isinstance(row.get(k), str) or not row[k].strip() for k in ('owner', 'reason', 'expires'))):
            raise ValueError('exceptions require owner, reason, expires and exact selectors')
        selectors = [key for key in ('id', 'package', 'file') if key in row]
        if not selectors or (row.get('kind') == 'coverage' and selectors != ['file']): raise ValueError('invalid exception selectors')
        for key in selectors:
            value = row[key]
            if not isinstance(value, str) or not value or any(c in value for c in '*?[') or any(ord(c) < 32 for c in value):
                raise ValueError('exception selectors must be exact')
            if key == 'file' and not scan.relative(value): raise ValueError('invalid exception file')
        # An ISO date expires at the start of that UTC date.
        timestamp(row['expires'] + 'T00:00:00Z' if re.fullmatch(r'\d{4}-\d{2}-\d{2}', row['expires']) else row['expires'])
    return rows


def active_exceptions(rows, now):
    return [row for row in rows if timestamp(row['expires'] + 'T00:00:00Z' if len(row['expires']) == 10 else row['expires']) > now]


def report_findings(value, directory, inv):
    if not isinstance(value, dict) or value.get('SchemaVersion') != 2 or not isinstance(value.get('Results'), list):
        raise ValueError('invalid Trivy JSON contract')
    found, seen, missing_resource = [], set(), []
    for result in value['Results']:
        if not isinstance(result, dict): raise ValueError('invalid Trivy result')
        name = result.get('Target')
        if isinstance(name, str) and name.startswith(str(directory) + '/'):
            name = name[len(str(directory)) + 1:]
        if isinstance(name, str) and name.startswith('./'): name = name[2:]
        # Filesystem targets must be files in this snapshot.
        if not scan.relative(name) or not (directory / name).is_file(): raise ValueError('Trivy target outside snapshot')
        seen.add(name)
        for item in result.get('Vulnerabilities') or []:
            rule, package, version, severity = (item[k] for k in ('VulnerabilityID', 'PkgName', 'InstalledVersion', 'Severity'))
            if (any(not isinstance(x, str) or not x or len(x) > 512 or any(ord(c) < 32 for c in x) for x in (rule, package, version))
                    or severity not in LEVELS): raise ValueError('invalid vulnerability identity')
            found.append(dict(tool='trivy', kind='vulnerability', rule_id=rule, file=name, line=1, severity=severity,
                              package=package, version=version, identity=[name, rule, package, version]))
        for item in result.get('Misconfigurations') or []:
            rule, severity = item['ID'], item['Severity']
            # Only failed checks are findings; passes and exceptions are not reported as defects.
            if item.get('Status', 'FAIL') != 'FAIL': continue
            cause = item.get('CauseMetadata', {})
            if not isinstance(cause, dict): raise ValueError('invalid misconfiguration identity')
            start, end, resource = cause.get('StartLine'), cause.get('EndLine'), cause.get('Resource')
            if resource is None: resource = ''
            if (not isinstance(rule, str) or not re.fullmatch(r'[A-Za-z0-9_.:/-]{1,512}', rule) or severity not in LEVELS
                    or not isinstance(resource, str)):
                raise ValueError('invalid misconfiguration identity')
            if start is None and end is None:
                # File-level checks (a missing HEALTHCHECK, for example) carry no cause lines in the
                # pinned release; their identity is the file, check and resource with a fixed marker.
                start, cause_hash = 0, scan.sha(b'bureau-trivy:file-level')
            else:
                lines = (directory / name).read_bytes().split(b'\n')
                if type(start) is not int or type(end) is not int or not 1 <= start <= end <= len(lines):
                    raise ValueError('invalid misconfiguration identity')
                normalized_lines = '\n'.join(line.decode('utf-8').strip() for line in lines[start - 1:end])
                cause_hash = scan.sha(normalized_lines.encode())
            if not resource: missing_resource.append(dict(file=name, rule_id=rule))
            found.append(dict(tool='trivy', kind='misconfiguration', rule_id=rule, file=name, line=start, severity=severity,
                              resource=resource, cause_sha256=cause_hash, identity=[name, rule, resource, cause_hash]))
    missing = [dict(file=name, reason='inventory target absent from Trivy results') for name in inv['targets'] if name not in seen]
    return found, missing, missing_resource


def compare(base, head, ignores):
    old, current = Counter(tuple(row['identity']) for row in base), {}
    for row in head: current.setdefault(tuple(row['identity']), []).append(row)
    findings, severities, new, existing, suppressed = [], [], 0, 0, 0
    for identity, rows in sorted(current.items()):
        applicable = any(row.get('kind', 'finding') == 'finding' and all(
            rows[0].get({'id':'rule_id', 'file':'file', 'package':'package'}[key]) == row[key]
            for key in ('id', 'file', 'package') if key in row) for row in ignores)
        if applicable: suppressed += len(rows); continue
        prior, count = old[identity], len(rows)
        existing += min(prior, count); new += max(0, count - prior)
        if count > prior:
            findings += [{**row, 'count_rose': 'count rose from ' + str(prior) + ' to ' + str(count)} for row in rows]
            severities += [row['severity'] for row in rows]
    findings.sort(key=lambda row: (-LEVELS[row['severity']], row['file'], row['line'], row['rule_id']))
    return dict(findings=findings, severities=sorted(set(severities)), new=new, existing=existing, suppressed=suppressed)


def scan_argv(binary, root, cache, side, target):
    return [str(binary), 'fs', *FLAGS['scan'], FLAGS['config'], str(root / 'empty.yaml'),
            FLAGS['ignore'], str(root / 'empty.ignore'), FLAGS['cache'], str(cache), *FLAGS['scanners'],
            *FLAGS['format'], FLAGS['output'], str(root / (side + '.json')), *FLAGS['timeout'], str(target)]


def run_side(binary, pin, prefix, root, cache, env, side, directory):
    require_binary(binary, pin)
    output = root / (side + '.json')
    proc = scan.run_process([*prefix, *scan_argv(binary, root, cache, side, directory)],
                            cwd=root, env=env, timeout=330)
    raw = output.read_text() if output.is_file() else ''
    try: value = json.loads(raw)
    except ValueError: value = dict(unparsed_output=raw)
    return dict(exit_code=proc.returncode, json=value)


def cache_guard(cache, expected):
    # Revalidate the private databases between scans and drop Trivy's own
    # non-DB analysis cache so it cannot reintroduce external checks.
    for entry in cache.iterdir():
        if entry.name in ('db', 'java-db'): continue
        if entry.is_symlink(): entry.unlink()
        elif entry.is_dir(): shutil.rmtree(entry)
        else: entry.unlink()
    if scan.directory_files(cache) != expected: raise NotMeasured('private cache digest mismatch')


def measure(repo, config, base, head, *, reuse=False, pr=None, open_prs=None,
            _state=None, _binary=None, _pins=None, _prefix=None, _probe=None, _now=None):
    options = settings(config, repo)
    if not options['enabled']: return None
    state = _state if _state is not None else scan.state_dir(repo)
    pins = _pins or PINS
    try: pin_digest = scan.digest(dict(pins=scan.file_sha(pins), execution='private-binary-v1', platform=platform_key()))
    except OSError: pin_digest = scan.digest(dict(pins='missing', execution='private-binary-v1', platform=platform_key()))
    settings_digest = scan.digest(config.get('qa', {}).get('trivy', {}))
    policy_digest = scan.digest(dict(unreadable_policy=True))
    db_digest = db_updated_at = None
    status, reason, report = 'not_measured', 'scan inputs unavailable', {}
    counts = dict(new=0, existing=0, suppressed=0, findings=[], severities=[])
    coverage, reduced_blocks = [], True
    extra = dict(qualifications=['image scanning is not part of this release', 'version-range consistency not checked',
                                'missing misconfiguration resource uses cause-lines hash'],
                 removed_native_config={}, removed_inline_ignores={})
    now = _now or datetime.now(timezone.utc)
    def bound():
        return scan.identity('trivy', head, base, pin_digest, settings_digest, policy_digest,
                             db_digest=db_digest, db_updated_at=db_updated_at)
    try:
        trees = dict(base=source.tree(repo, base), head=source.tree(repo, head))
        policy_bytes = source.blob(repo, trees['base'], EXCEPTIONS)
        policy_digest = scan.digest(dict(exceptions=None if policy_bytes is None else scan.sha(policy_bytes),
                                         checks='embedded', inventory='v1', flags=FLAGS))
        with tempfile.TemporaryDirectory(prefix='bureau-trivy-') as temporary:
            root = Path(temporary).resolve()
            if any(inventory_name(name) and not native(name) for entries in trees.values() for name in entries):
                sides, manifests = snapshots(repo, trees, root)
            else:
                # An unrelated large or non-regular source file must not turn
                # an empty eligible inventory into a scanner prerequisite error.
                sides, manifests = {}, {}
                for side in ('base', 'head'):
                    sides[side] = root / side; sides[side].mkdir(mode=0o700)
                    manifests[side] = dict(files=[], removed_native_config=sorted(n for n in trees[side] if native(n)),
                                           removed_inline_ignores=[], skipped_nonregular=[])
            report['snapshots'] = manifests
            inventories = {side:inventory(path, manifests[side]['skipped_nonregular']) for side, path in sides.items()}
            report['inventory'] = inventories
            for key in ('removed_native_config', 'removed_inline_ignores'):
                extra[key] = {side:manifests[side][key] for side in sides}
            if not any(inv['applicable'] for inv in inventories.values()):
                status, reason = 'not_applicable', 'no eligible dependency or configuration inventory'
            else:
                ignores = active_exceptions(exceptions(policy_bytes), now)
                binary_path = Path(_binary or os.environ.get('BUREAU_TRIVY_BIN') or state / 'tools/trivy/trivy').expanduser().absolute()
                binary, pin = verify_copy(binary_path, root, pins)
                cache, db_digest, db_updated_at, removed_checks = cache_snapshot(Path(options['cache_dir']).expanduser().absolute(), root,
                                                                                options['max_db_age_hours'], now)
                cache_files = scan.directory_files(cache)
                report['removed_external_checks'] = removed_checks
                env = scan.environment(root)
                prefix = _prefix if _prefix is not None else scan.network_prefix()
                if not (_probe or scan.probe_network)(prefix, root, env): raise NotMeasured('network isolation unavailable')
                (root / 'empty.yaml').write_text(''); (root / 'empty.ignore').write_text('')
                prior = scan.find_receipt(state, bound(), options, pr, severity_map=LEVELS) if reuse else None
                if prior:
                    # Expiry changes are time-dependent even when policy bytes
                    # are unchanged. Reuse only within the same active policy set.
                    old_report = json.loads(Path(prior['receipt']['report_path']).read_text())
                    if old_report.get('active_exceptions') == ignores: return prior
                report['active_exceptions'] = ignores
                findings, missing_resources = {}, {}
                for side in ('base', 'head'):
                    inv = inventories[side]
                    coverage += [{**row, 'side':side} for row in inv['issues']]
                    if inv['consistency_unchecked']:
                        extra.setdefault('consistency_unchecked', {})[side] = inv['consistency_unchecked'][:20]
                        extra.setdefault('consistency_unchecked_omitted', {})[side] = max(0, len(inv['consistency_unchecked']) - 20)
                    if not inv['targets']: findings[side] = []; continue
                    try:
                        cache_guard(cache, cache_files)
                        raw = run_side(binary, pin, prefix, root, cache, env, side, sides[side]); report[side] = raw
                        if raw['exit_code']: raise ValueError('non-zero Trivy exit')
                        findings[side], missing, missing_resources[side] = report_findings(raw['json'], sides[side], inv)
                        coverage += [{**row, 'side':side} for row in missing]
                    except Unavailable: raise
                    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
                        if side == 'head': raise NotMeasured('head not measured')
                        findings['base'] = None
                        coverage += [dict(file=name, reason='base not measured', side='base') for name in inv['targets']]
                counts = compare(findings['base'] or [], findings['head'], ignores)
                if findings['base'] is None:
                    extra['findings_unclassified'] = len(findings['head']) - counts['suppressed']
                    counts.update(new=0, existing=0, severities=[])
                    for row in counts['findings']: row.pop('count_rose', None)
                for row in coverage:
                    row['excepted'] = any(item.get('kind') == 'coverage' and item['file'] == row['file'] for item in ignores)
                reduced_blocks = any(not row['excepted'] for row in coverage)
                extra['coverage'] = coverage[:20]; extra['coverage_omitted'] = max(0, len(coverage) - 20)
                extra['missing_resources'] = {side:rows[:20] for side, rows in missing_resources.items()}
                report['coverage'] = coverage
                status, reason = ('reduced_coverage', '; '.join(row['reason'] + ': ' + row['file'] for row in coverage[:5])) if coverage else ('measured', None)
    except Unavailable as exc: status, reason = 'unavailable', str(exc)
    except NotMeasured as exc:
        status, reason = 'not_measured', str(exc)
        db_digest = getattr(exc, 'db_digest', db_digest)
        db_updated_at = getattr(exc, 'db_updated_at', db_updated_at)
    except subprocess.TimeoutExpired: status, reason = 'not_measured', 'Trivy or source snapshot timed out'
    except (OSError, ValueError, TypeError, KeyError, UnicodeError, subprocess.SubprocessError):
        status, reason = 'not_measured', 'invalid policy, source snapshot or incomplete Trivy result'
    for key in ('removed_native_config', 'removed_inline_ignores'):
        extra[key + '_omitted'] = {side:max(0, len(rows) - 20) for side, rows in extra[key].items()}
        extra[key] = {side:rows[:20] for side, rows in extra[key].items()}
    return scan.persist(state, bound(), options, status, reason, report, pr=pr, open_prs=open_prs,
                        severity_map=LEVELS, reduced_blocks=reduced_blocks, projection_extra=extra, **counts)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('settings', 'scan', 'refresh'))
    parser.add_argument('--repo', default='.')
    parser.add_argument('--config')
    parser.add_argument('--cache-dir')
    parser.add_argument('--java', action='store_true')
    parser.add_argument('--base'); parser.add_argument('--head')
    parser.add_argument('--reuse', action='store_true')
    parser.add_argument('--pr', type=int); parser.add_argument('--open-prs', default='null')
    args = parser.parse_args()
    try:
        repo = Path(args.repo).resolve()
        if args.command == 'refresh':
            if not args.cache_dir: raise ValueError('refresh requires --cache-dir')
            value = refresh(Path(args.cache_dir), java=args.java, repo=repo)
        else:
            if not args.config: raise ValueError('settings and scan require --config')
            config = json.loads(Path(args.config).read_bytes())
            value = settings(config, repo) if args.command == 'settings' else measure(repo, config, args.base, args.head,
                reuse=args.reuse, pr=args.pr, open_prs=json.loads(args.open_prs))
        print(scan.encoded(value)); return 0
    except KeyboardInterrupt: return 130
    except (OSError, ValueError, TypeError, subprocess.SubprocessError):
        print('Trivy configuration, refresh or durable receipt unavailable', file=sys.stderr); return 24


if __name__ == '__main__': sys.exit(main())
