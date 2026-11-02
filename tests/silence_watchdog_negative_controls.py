"""Disable only activity expiry in a private adapter; real early-stop guard fails."""
import hashlib
from pathlib import Path
import shutil
import sys
import tempfile

from mutation_checks import mutation_caught, snapshot_files

ROOT=Path(__file__).resolve().parents[1]
SCRIPTS=ROOT/'templates/scripts'
before={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in SCRIPTS.iterdir() if p.is_file()}
with tempfile.TemporaryDirectory(prefix='bureau-1723-expiry-control-') as scratch:
    target=Path(scratch).resolve()/'scripts'; shutil.copytree(SCRIPTS,target)
    originals=snapshot_files(target)
    adapter=target/'bureau-provider.py'
    source=adapter.read_text(); anchor='                            if activity.expired(owned):'
    assert source.count(anchor)==1
    adapter.write_text(source.replace(anchor,anchor.replace('if activity.expired(owned)', 'if False and activity.expired(owned)')))
    mutation_caught([sys.executable,str(ROOT/'tests/silence_watchdog_test.py'),'Watchdog.test_initial_silence'],
        env={'BUREAU_TEST_PROVIDER_SCRIPT':str(adapter)},
        originals=originals,timeout=15)
    print('PASS negative control: disabled activity expiry misses the silent-provider early-stop deadline')
assert before=={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in SCRIPTS.iterdir() if p.is_file()}
print('PASS negative control: runtime source hashes unchanged')
