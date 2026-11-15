#!/usr/bin/env python3
"""Build one reviewed Semgrep runtime manifest from hash-locked wheels, offline."""
import argparse
import hashlib
import importlib.util
import json
from pathlib import Path, PurePosixPath
import re
import stat
import sys
import zipfile

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('pin_scan', ROOT / 'templates/scripts/bureau-scan.py')
scan = importlib.util.module_from_spec(spec); spec.loader.exec_module(scan)
VERSION = '1.183.0'


def manifest(wheels, lock, system, arch, abi):
    if system not in ('darwin', 'linux') or arch not in ('arm64', 'x86_64') or not re.fullmatch(r'cp3[0-9]+', abi):
        raise ValueError('expected supported OS, architecture and CPython minor (for example cp312)')
    # Hashes in comments confer no authority. Requirements may span backslash lines.
    text = '\n'.join(line.split('#', 1)[0] for line in lock.read_text().splitlines())
    hashes = set(re.findall(r'--hash=sha256:([0-9a-f]{64})(?![0-9a-f])', text))
    files, packages = {}, set()
    sources = sorted(wheels.iterdir())
    if not sources or any(p.suffix != '.whl' or not p.is_file() for p in sources):
        raise ValueError('wheel directory must contain only locked wheels for one platform')
    for wheel in sources:
        if scan.file_sha(wheel) not in hashes: raise ValueError('wheel SHA-256 absent from lock: ' + wheel.name)
        package = wheel.name.split('-', 1)[0].lower().replace('_', '-')
        if package in packages: raise ValueError('multiple wheels for one distribution')
        if package == 'semgrep' and wheel.name.split('-')[1] != VERSION:
            raise ValueError('Semgrep wheel version must be ' + VERSION)
        packages.add(package)
        with zipfile.ZipFile(wheel) as archive:
            for item in archive.infolist():
                if item.is_dir(): continue
                name = item.filename
                if not scan.relative(name): raise ValueError('unsafe wheel path')
                if stat.S_ISLNK(item.external_attr >> 16): raise ValueError('wheel symlink rejected')
                parts = PurePosixPath(name).parts
                if parts[0].endswith('.data'):
                    if len(parts) < 3 or parts[1] not in ('purelib', 'platlib'):
                        raise ValueError('unsupported wheel installation scheme: ' + name)
                    name = '/'.join(parts[2:])
                if scan.excluded(name): continue
                if name in files: raise ValueError('colliding wheel path: ' + name)
                files[name] = hashlib.sha256(archive.read(item)).hexdigest()
    if 'semgrep' not in packages or not any(PurePosixPath(p).name == 'semgrep-core' for p in files):
        raise ValueError('wheel set must include Semgrep and semgrep-core')
    return dict(schema_version=1, tool='semgrep', version=VERSION, os=system, arch=arch, python=abi,
                files=dict(sorted(files.items())))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('wheels', type=Path)
    parser.add_argument('--lock', type=Path, default=ROOT / 'templates/scripts/bureau-semgrep-lock.txt')
    parser.add_argument('--os', required=True, choices=('darwin', 'linux'))
    parser.add_argument('--arch', required=True, choices=('arm64', 'x86_64'))
    parser.add_argument('--python', required=True, help='CPython minor key, such as cp312')
    parser.add_argument('--output-dir', type=Path, default=ROOT / 'templates/scripts')
    args = parser.parse_args()
    try:
        value = manifest(args.wheels, args.lock, args.os, args.arch, args.python)
        output = args.output_dir / ('bureau-semgrep-manifest-' + args.os + '-' + args.arch + '-' + args.python + '.json')
        output.write_text(json.dumps(value, indent=2, sort_keys=True) + '\n')
        print(output.name)
    except (OSError, ValueError, zipfile.BadZipFile) as exc:
        parser.exit(1, str(exc) + '\n')


if __name__ == '__main__': main()
