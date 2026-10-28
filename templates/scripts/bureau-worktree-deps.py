#!/usr/bin/env python3
"""Restore pnpm/Yarn Classic worktree dependencies without registry access."""
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import signal
import subprocess
import sys

INSTALL_TIMEOUT = 120
VERSION_TIMEOUT = 20


def git(repo, *args):
    return subprocess.check_output(['git', '-C', str(repo), *args], timeout=20)


def remove(path):
    if path.is_symlink():
        path.unlink()
    elif path.is_dir():
        shutil.rmtree(path)
    elif path.exists():
        path.unlink()


def modules(repo):
    """Do not traverse links, Git metadata or a dependency tree."""
    found = []
    for parent, dirs, files in os.walk(repo, followlinks=False):
        for name in list(dirs) + files:
            if name.lower() == 'node_modules':
                found.append(Path(parent) / name)
        dirs[:] = [name for name in dirs if name.lower() not in ('.git', 'node_modules')]
    return found


def clean(repo):
    for path in modules(repo):
        remove(path)


def yarn_pnp_enabled(repo, manifest):
    """Classic also supports PnP; version detection alone cannot rule it out."""
    install_config = manifest.get('installConfig', {})
    if isinstance(install_config, dict) and install_config.get('pnp'):
        return True
    if any((repo / name).exists() or (repo / name).is_symlink()
           for name in ('.pnp.js', '.pnp.cjs', '.pnp.loader.mjs', '.pnp.data.json')):
        return True

    def enabled(value):
        # Only an absent/empty or explicit false switch is known to be off;
        # other strings can be truthy to a package manager's option parser.
        return str(value) not in ('', 'false')

    if any(enabled(os.environ.get(name, ''))
           for name in ('YARN_PNP', 'YARN_ENABLE_PNP', 'YARN_PLUGNPLAY')):
        return True
    if '.pnp.' in os.environ.get('NODE_OPTIONS', ''):
        return True
    # Yarn reads rc files above the project and in the operator's home too.
    directories = {repo, *repo.parents, Path.home()}
    pnp_keys = {'pnp', 'enable-pnp', 'plugnplay', 'install.pnp',
                'install.enable-pnp', 'install.plugnplay'}
    for directory in sorted(directories):
        for name in ('.yarnrc', '.yarnrc.yml'):
            path = directory / name
            if not path.is_file():
                continue
            for line in path.read_text().splitlines():
                parts = shlex.split(line, comments=True)
                if not parts:
                    continue
                key = parts[0].lstrip('-').rstrip(':')
                value = parts[1] if len(parts) > 1 else 'true'
                if (key in pnp_keys and enabled(value)) or (key == 'nodeLinker' and value == 'pnp'):
                    return True
    return False


def fingerprint(repo, manager, lockfile):
    # Include workspace manifests and manager configuration, not only the root
    # package/lock pair. Ignored dependency trees and caches are not inputs.
    names = git(repo, 'ls-files', '--cached', '--others', '--exclude-standard', '-z').split(b'\0')
    inputs = {'package.json', lockfile}
    config_names = {'package.json', 'pnpm-workspace.yaml', '.npmrc', '.yarnrc', '.yarnrc.yml'}
    for raw in names:
        if not raw:
            continue
        name = os.fsdecode(raw)
        parts = Path(name).parts
        if not any(p.lower() in ('node_modules', '.git') for p in parts) and parts[-1] in config_names:
            inputs.add(name)
    digest = hashlib.sha256(manager.encode())
    needs_modules = False
    for name in sorted(inputs):
        path = repo / name
        if path.is_symlink() or repo not in path.resolve().parents:
            raise ValueError('dependency input must be an unlinked worktree file: ' + name)
        content = path.read_bytes()
        if path.name == 'package.json':
            manifest = json.loads(content)
            if not isinstance(manifest, dict):
                raise ValueError('package.json must be an object: ' + name)
            needs_modules |= any(manifest.get(key) for key in ('dependencies', 'devDependencies', 'optionalDependencies'))
        digest.update(os.fsencode(name) + b'\0' + str(len(content)).encode() + b'\0' + content)
    return {'sha256': digest.hexdigest(), 'needs_modules': needs_modules}


def command(argv, repo, env, log, timeout):
    """Bound each attempt and drain its own process group, including on signals."""
    with log.open('wb') as output:
        proc = subprocess.Popen(argv, cwd=repo, env=env, stdin=subprocess.DEVNULL,
                                stdout=output, stderr=subprocess.STDOUT, start_new_session=True)
        try:
            try:
                return proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                output.write(b'\nDependency command timed out\n')
                return 124
        finally:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            proc.wait()


def restore(repo):
    common = Path(os.fsdecode(git(repo, 'rev-parse', '--git-common-dir')).strip())
    if not common.is_absolute():
        common = repo / common
    common = common.resolve()
    private = Path(os.fsdecode(git(repo, 'rev-parse', '--git-dir')).strip())
    if not private.is_absolute():
        private = repo / private
    if private.resolve() == common:
        return 0  # Never modify the main checkout.
    locks = [name for name in ('pnpm-lock.yaml', 'yarn.lock', 'package-lock.json') if (repo / name).exists()]
    if len(locks) != 1:
        raise ValueError('multiple package-manager lockfiles; keep the intended one')
    lockfile = locks[0]
    manager = 'pnpm' if lockfile == 'pnpm-lock.yaml' else 'yarn'
    manifest = json.loads((repo / 'package.json').read_text())
    package_manager = manifest.get('packageManager')
    if package_manager and (not isinstance(package_manager, str) or not package_manager.startswith(manager + '@')):
        raise ValueError('packageManager disagrees with ' + lockfile)
    if manager == 'yarn' and yarn_pnp_enabled(repo, manifest):
        clean(repo)
        raise ValueError('Yarn PnP is unsupported, including Classic; use a separate dependency setup')
    want = fingerprint(repo, manager, lockfile)
    directory = common / 'bureau-deps'
    directory.mkdir(parents=True, exist_ok=True)
    stamp = directory / (hashlib.sha256(os.fsencode(repo)).hexdigest() + '.json')
    log = stamp.with_suffix('.' + manager + '-install.log')
    env = dict(os.environ, COREPACK_ENABLE_NETWORK='0', COREPACK_ENABLE_DOWNLOAD_PROMPT='0',
               YARN_IGNORE_PATH='1')
    installed = shutil.which(manager)
    if not installed:
        clean(repo)
        raise ValueError(manager + ' is not installed; provision it and its offline cache before running workers')
    if manager == 'yarn':
        # Classic and modern Yarn have different immutable-install/script/plugin
        # contracts. Never guess the dialect and silently lose a safety flag.
        if command([installed, '--version'], repo, env, log, VERSION_TIMEOUT) != 0 or not re.fullmatch(r'1\.[0-9]+\.[0-9]+[^\n]*\n?', log.read_text()):
            clean(repo)
            raise ValueError('Yarn Classic 1.x is required; modern Yarn/PnP needs a separate dependency setup')
    tracked = {os.fsdecode(n) for n in git(repo, 'ls-files', '-z').split(b'\0') if n}
    tracked_modules = any('node_modules' in (p.lower() for p in Path(n).parts) for n in tracked)
    current = modules(repo)
    if tracked_modules or any(p.is_symlink() for p in current):
        print('  WARNING: tracked or symlinked node_modules discarded (not trusted).')
        clean(repo)
    else:
        try:
            saved = json.loads(stamp.read_text())
        except (OSError, ValueError):
            saved = {}
        expected = sorted(str(p.relative_to(repo)) for p in current if p.is_dir())
        if saved == {'fingerprint': want, 'modules': expected} and (expected or not want['needs_modules']):
            print('  Dependencies: unchanged, skipped')
            return 0
    clean(repo)
    stamp.unlink(missing_ok=True)
    argv = [installed, 'install', '--frozen-lockfile', '--offline', '--ignore-scripts']
    if manager == 'pnpm':
        argv += ['--prefer-offline', '--ignore-pnpmfile']
    else:
        argv += ['--non-interactive']
    try:
        for attempt in (1, 2):
            code = command(argv, repo, env, log, INSTALL_TIMEOUT)
            current = modules(repo)
            if code == 0 and (current or not want['needs_modules']) and all(p.is_dir() and not p.is_symlink() for p in current):
                # Frozen must mean unchanged, even with an unexpected tool version.
                if fingerprint(repo, manager, lockfile) != want:
                    raise ValueError('dependency installation changed a manifest or lockfile')
                stamp.write_text(json.dumps({'fingerprint': want, 'modules': sorted(str(p.relative_to(repo)) for p in current)}))
                print('  Dependencies: installed with ' + manager + ' install --frozen-lockfile --offline --ignore-scripts')
                return 0
            clean(repo)
        print('  ' + manager + ' install failed (2 attempts) — last lines:')
        for line in log.read_text(errors='replace').splitlines()[-20:]:
            print('    | ' + line)
        return 24
    finally:
        if not stamp.exists():
            clean(repo)


def interrupted(signum, frame):
    raise KeyboardInterrupt


def main():
    for signum in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        signal.signal(signum, interrupted)
    try:
        return restore(Path(sys.argv[1]).resolve())
    except KeyboardInterrupt:
        return 130
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        print('  Dependencies: ' + str(exc), file=sys.stderr)
        return 24


if __name__ == '__main__':
    sys.exit(main())
