#!/usr/bin/env python3
"""Shared local-scanner boundary: isolation, verified wheels and durable receipts.

The operator's system interpreter is trusted. Package RECORD files, entry-point
launchers, repository configuration and adjacent bytecode are never trust anchors.
"""
from contextlib import contextmanager
from datetime import datetime, timezone
from functools import lru_cache
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path, PurePosixPath
import platform
import re
import shutil
import signal
import subprocess
import sys
import uuid

sys.dont_write_bytecode = True
SCRIPTS = Path(__file__).resolve().parent
STATUSES = {'measured', 'not_measured', 'unavailable', 'not_applicable', 'reduced_coverage'}
SEVERITY = {'INFO': 0, 'WARNING': 1, 'ERROR': 2}
# Per-install metadata inside *.dist-info, never executed. uv adds uv_cache.json.
METADATA = {'RECORD', 'INSTALLER', 'direct_url.json', 'REQUESTED', 'uv_cache.json'}
BOUND = ('tool', 'head', 'base', 'pin_digest', 'settings_digest', 'policy_digest')


@lru_cache(maxsize=None)
def module(name):
    spec = importlib.util.spec_from_file_location('scan_' + name, SCRIPTS / ('bureau-' + name + '.py'))
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


def encoded(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(',', ':'))


def sha(data):
    return hashlib.sha256(data).hexdigest()


def digest(value):
    return sha(encoded(value).encode())


def file_sha(path):
    result = hashlib.sha256()
    with Path(path).open('rb') as source:
        for block in iter(lambda: source.read(65536), b''):
            result.update(block)
    return result.hexdigest()


def relative(path):
    return (isinstance(path, str) and bool(path) and not path.startswith(('/', '~', '-'))
            and '\\' not in path and ':' not in path and not any(ord(c) < 32 for c in path)
            and all(p not in ('', '.', '..', '.git') for p in path.split('/')))


def state_dir(repo):
    # The same shared-Git directory as runtime.Store, including linked worktrees.
    return module('runtime').common_for(Path(repo)) / 'bureau'


def environment(root, tool_dir, switches=None):
    values = dict(PATH=str(tool_dir) + ':/usr/bin:/bin', LANG='C.UTF-8', TZ='UTC')
    for key, name in (('HOME', 'home'), ('XDG_CONFIG_HOME', 'config'),
                      ('XDG_CACHE_HOME', 'cache'), ('TMPDIR', 'tmp')):
        path = root / name
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        values[key] = str(path)
    values.update(switches or {})
    return values


def run_process(argv, *, cwd, env, timeout=120):
    """Join the entire scanner group on timeout/cancellation, including its core."""
    process = None
    previous = {}

    def cancel(signum, frame):
        raise KeyboardInterrupt

    try:
        for signum in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
            previous[signum] = signal.signal(signum, cancel)
        # Delay signal delivery until the new process has a recorded group ID.
        blocked = signal.pthread_sigmask(signal.SIG_BLOCK, set(previous))
        try:
            process = subprocess.Popen(argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
        finally:
            signal.pthread_sigmask(signal.SIG_SETMASK, blocked)
        stdout, stderr = process.communicate(timeout=timeout)
        return subprocess.CompletedProcess(argv, process.returncode, stdout, stderr)
    finally:
        if process is not None:
            # A core/child can outlive the Python leader even on a normal exit.
            try: os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError: pass
            process.communicate()
        for signum, handler in previous.items():
            signal.signal(signum, handler)


def network_prefix(system=None):
    system = system or platform.system()
    if system == 'Linux':
        binary = next((p for p in ('/usr/bin/unshare', '/bin/unshare') if Path(p).is_file()), None)
        return [binary, '-rn', '--'] if binary else None
    if system == 'Darwin' and Path('/usr/bin/sandbox-exec').is_file():
        return ['/usr/bin/sandbox-exec', '-f', str(SCRIPTS / 'bureau-scan-nonet.sb')]
    return None


def probe_network(prefix, root, env):
    if not prefix: return False
    try:
        return run_process([*prefix, '/usr/bin/true'], cwd=root, env=env, timeout=10).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def python_argv(venv, cache, *args, no_site=False):
    cache.mkdir(mode=0o700)  # fresh, empty, never shared with another invocation
    return [str(venv / 'bin/python'), '-I', '-B', '-X', 'pycache_prefix=' + str(cache),
            *(['-S'] if no_site else []), *args]


def interpreter(venv, root, env, prefix):
    # -S avoids executing any .pth or sitecustomize before package verification.
    code = ('import json,platform,sys; assert sys.implementation.name == "cpython"; print(json.dumps([platform.system(),'
            'platform.machine(),"cp%d%d" % sys.version_info[:2]]))')
    proc = run_process([*prefix, *python_argv(venv, root / 'interpreter-cache', '-c', code, no_site=True)],
                       cwd=root, env=env, timeout=10)
    if proc.returncode: raise ValueError('system interpreter probe failed')
    system, arch, abi = json.loads(proc.stdout)
    system = {'Darwin': 'darwin', 'Linux': 'linux'}.get(system)
    arch = {'aarch64': 'arm64', 'arm64': 'arm64', 'x86_64': 'x86_64', 'AMD64': 'x86_64'}.get(arch)
    if not system or not arch or not re.fullmatch(r'cp3[0-9]+', abi):
        raise ValueError('unsupported scanner platform or CPython interpreter')
    return system, arch, abi


def excluded(name):
    parts = PurePosixPath(name).parts
    return ('__pycache__' in parts or
            (parts[-1] in METADATA and len(parts) > 1 and parts[-2].endswith('.dist-info')))


def site_files(site):
    result = {}
    for path in sorted(site.rglob('*')):
        name = path.relative_to(site).as_posix()
        if path.is_symlink(): raise ValueError('symlink in site-packages')
        if path.is_file() and not excluded(name): result[name] = file_sha(path)
        elif not path.is_dir() and not path.is_file(): raise ValueError('non-regular site-packages entry')
    return result


def verify_venv(venv, manifest_path, tool, version, system, arch, abi, manifest_sha=None):
    """Never execute the launcher or consult an installed RECORD file."""
    if not manifest_path.is_file(): raise ValueError('missing manifest ' + manifest_path.name)
    if manifest_sha is not None and file_sha(manifest_path) != manifest_sha:
        raise ValueError('runtime manifest changed during scan')
    manifest = json.loads(manifest_path.read_text())
    if any(manifest.get(k) != v for k, v in dict(schema_version=1, tool=tool, version=version,
                                                 os=system, arch=arch, python=abi).items()):
        raise ValueError('runtime manifest platform, interpreter or pin mismatch')
    files = manifest.get('files')
    if (not isinstance(files, dict) or not files or any(not relative(k) or excluded(k)
            or not isinstance(v, str) or not re.fullmatch(r'[0-9a-f]{64}', v) for k, v in files.items())):
        raise ValueError('invalid runtime manifest file set')
    cfg = (venv / 'pyvenv.cfg').read_text()
    if not re.search(r'^include-system-site-packages\s*=\s*false\s*$', cfg, re.M | re.I):
        raise ValueError('venv must disable system site-packages')
    # A second library directory could silently become an additional import path.
    sites = list((venv / 'lib').glob('python*/site-packages'))
    minor = abi[3:]
    expected = venv / 'lib' / ('python3.' + minor) / 'site-packages'
    if sites != [expected] or expected.is_symlink() or expected.parent.is_symlink():
        raise ValueError('venv site-packages does not match its interpreter')
    actual = site_files(expected)
    if actual != files: raise ValueError('site-packages manifest mismatch (changed, missing or additional file)')
    if tool == 'semgrep' and not any(PurePosixPath(p).name == 'semgrep-core' for p in files):
        raise ValueError('manifest is missing semgrep-core')
    return manifest


def identity(tool, head, base, pin_digest, settings_digest, policy_digest, **extra):
    result = dict(tool=tool, head=head, base=base, pin_digest=pin_digest,
                  settings_digest=settings_digest, policy_digest=policy_digest, **extra)
    if not relative(tool) or '/' in tool: raise ValueError('invalid tool name')
    if any(not re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}', result[k]) for k in ('head', 'base')):
        raise ValueError('scan requires pinned full head and base commits')
    return result


def blocking(settings, status, severities=(), reduced_blocks=True):
    if settings['mode'] != 'blocking': return False
    if status in ('not_measured', 'unavailable'): return True
    if status == 'reduced_coverage': return reduced_blocks
    return status == 'measured' and any(SEVERITY[s] >= SEVERITY[settings['block_severity']] for s in severities)


def projection(receipt, findings):
    # Explicit field selection: matched text, messages, metadata and secrets stay local.
    rows = []
    for finding in findings[:20]:
        row = {k: finding[k] for k in ('tool', 'rule_id', 'file', 'line', 'severity')}
        if 'count_rose' in finding: row['count_rose'] = finding['count_rose']
        rows.append(row)
    return dict(tool=receipt['tool'], status=receipt['status'], reason=receipt.get('reason'),
                findings_new=receipt['findings_new'], findings_existing=receipt['findings_existing'],
                findings_suppressed=receipt['findings_suppressed'], blocking=receipt['blocking'],
                findings=rows, findings_omitted=max(0, len(findings) - len(rows)))


@contextmanager
def locked(root):
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    with (root / '.lock').open('a') as lock:
        os.chmod(lock.name, 0o600)
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def write_json(path, value):
    with path.open('x', encoding='utf-8') as out:
        os.chmod(path, 0o600)
        out.write(encoded(value) + '\n')


def retain(root, open_prs=None):
    attempts = sorted((p for p in root.glob('*/*') if p.is_dir() and not p.is_symlink()),
                      key=lambda p: p.name, reverse=True)
    for path in attempts[20:]:
        try:
            receipt = json.loads((path / 'receipt.json').read_text())
            pr = receipt.get('pr')
            # Unknown open-PR inventory fails conservatively: keep referenced evidence.
            if pr is not None and (open_prs is None or pr in open_prs): continue
            shutil.rmtree(path)
        except (OSError, ValueError):
            continue


def persist(state, bound, settings, status, reason, report, findings=(), *, new=0, existing=0,
            suppressed=0, severities=(), pr=None, open_prs=None):
    if status not in STATUSES or (status != 'measured' and not reason):
        raise ValueError('scan status requires a named reason')
    root = state / 'scans' / bound['tool']
    key = digest(bound)
    with locked(root):
        parent = root / key
        parent.mkdir(mode=0o700, exist_ok=True)
        attempt = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ-') + uuid.uuid4().hex
        target = parent / attempt
        target.mkdir(mode=0o700)  # exclusive, never reuse an earlier attempt
        report_path = target / 'report.json'
        write_json(report_path, report)
        receipt = dict(schema_version=1, **bound, identity_digest=key, status=status,
                       findings_new=new, findings_existing=existing, findings_suppressed=suppressed,
                       new_severities=list(severities), blocking=blocking(settings, status, severities),
                       report_sha256=file_sha(report_path), report_path=str(report_path), pr=pr)
        if status != 'measured': receipt['reason'] = reason
        projected = projection(receipt, list(findings))
        projection_path = target / 'projection.json'
        write_json(projection_path, projected)
        receipt.update(projection_path=str(projection_path), projection_sha256=file_sha(projection_path))
        write_json(target / 'receipt.json', receipt)  # commit marker, written last
        retain(root, open_prs)
    return result(target, receipt, projected)


def result(target, receipt, projected):
    return dict(receipt=receipt, receipt_path=str(target / 'receipt.json'),
                receipt_sha256=file_sha(target / 'receipt.json'), projection=projected)


def validate_receipt(path, bound, settings):
    try:
        if path.is_symlink() or path.parent.is_symlink(): return None
        receipt = json.loads(path.read_text())
        key = digest(bound)
        if (receipt.get('schema_version') != 1 or receipt.get('identity_digest') != key
                or path.parent.parent.name != key or any(receipt.get(k) != v for k, v in bound.items())
                or receipt.get('status') not in STATUSES
                or (receipt['status'] != 'measured' and not receipt.get('reason'))): return None
        for field in ('findings_new', 'findings_existing', 'findings_suppressed'):
            if type(receipt.get(field)) is not int or receipt[field] < 0: return None
        severities = receipt.get('new_severities')
        if not isinstance(severities, list) or any(s not in SEVERITY for s in severities): return None
        if type(receipt.get('blocking')) is not bool or receipt['blocking'] != blocking(settings, receipt['status'], severities):
            return None
        for stem in ('report', 'projection'):
            target = path.with_name(stem + '.json')
            if (target.is_symlink() or receipt.get(stem + '_path') != str(target)
                    or receipt.get(stem + '_sha256') != file_sha(target)): return None
        projected = json.loads(path.with_name('projection.json').read_text())
        return result(path.parent, receipt, projected)
    except (OSError, ValueError, TypeError, KeyError):
        return None


def find_receipt(state, bound, settings):
    root = state / 'scans' / bound['tool'] / digest(bound)
    for path in sorted(root.glob('*/receipt.json'), reverse=True):
        value = validate_receipt(path, bound, settings)
        if value and value['receipt']['status'] in ('measured', 'not_applicable'):
            return value
    return None
