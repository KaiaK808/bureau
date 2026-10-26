"""Disable only activity expiry in a private adapter; real early-stop guard fails."""
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

ROOT=Path(__file__).resolve().parents[1]
SCRIPTS=ROOT/'templates/scripts'
before={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in SCRIPTS.iterdir() if p.is_file()}
with tempfile.TemporaryDirectory(prefix='bureau-1723-expiry-control-') as scratch:
    target=Path(scratch)/'scripts'; shutil.copytree(SCRIPTS,target)
    adapter=target/'bureau-provider.py'
    source=adapter.read_text(); anchor='                            if activity.expired(owned):'
    assert source.count(anchor)==1
    adapter.write_text(source.replace(anchor,anchor.replace('if activity.expired(owned)', 'if False and activity.expired(owned)')))
    result=subprocess.run([sys.executable,str(ROOT/'tests/silence_watchdog_test.py'),'Watchdog.test_initial_silence'],
        env={**os.environ,'BUREAU_TEST_PROVIDER_SCRIPT':str(adapter)},capture_output=True,text=True,timeout=15)
    assert result.returncode!=0 and 'silent provider missed early-stop deadline' in result.stdout+result.stderr, result.stdout+result.stderr
    print('PASS negative control: disabled activity expiry misses the silent-provider early-stop deadline')
assert before=={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in SCRIPTS.iterdir() if p.is_file()}
print('PASS negative control: runtime source hashes unchanged')
