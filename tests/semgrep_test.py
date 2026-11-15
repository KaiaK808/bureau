"""Offline scanner boundary and Semgrep receipts; no installed scanner required."""
import copy
import importlib.util
import json
import os
from pathlib import Path
import platform
import re
import py_compile
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import venv
import zipfile

import git_maintenance

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / 'templates/scripts'


def load(name, path=None):
    spec = importlib.util.spec_from_file_location('semgrep_test_' + name, path or SCRIPTS / ('bureau-' + name + '.py'))
    value = importlib.util.module_from_spec(spec); spec.loader.exec_module(value)
    return value


FAKE = r'''
import json, os, pathlib, shutil, site, subprocess, sys, time
settings = json.loads(pathlib.Path(SETTINGS).read_text())
# Model a system site directory without writing to the host's Python install.
# CPython's effective venv configuration decides whether that prefix is enabled.
if settings.get('system_site') and sys.base_prefix in site.PREFIXES:
    site.addsitedir(settings['system_site'])
    site.execsitecustomize()
args = sys.argv[1:]
files = args[args.index('--')+1:]
record = dict(env=dict(os.environ), argv=args, python=sys.orig_argv if hasattr(sys, 'orig_argv') else [],
              files={p:pathlib.Path(p).read_text() for p in files}, cwd=os.getcwd(), marker='source',
              configs=[pathlib.Path(args[i+1]).read_text() for i,x in enumerate(args[:-1]) if x=='--config'],
              native=[str(p) for p in pathlib.Path('.').rglob('*') if '.semgrep' in str(p)],
              cache_prefix=sys.pycache_prefix, cache_empty=not any(pathlib.Path(sys.pycache_prefix).rglob('*')),
              prefix=sys.prefix, executable=sys.executable, base_prefix=sys.base_prefix,
              executable_entries=sorted(os.listdir(os.path.dirname(sys.executable))),
              site_paths=[p for p in sys.path if p.endswith('/site-packages')], site_prefixes=site.PREFIXES,
              shadow_cfg=(pathlib.Path(sys.prefix)/'pyvenv.cfg').read_text(),
              python_target=str(pathlib.Path(sys.executable).resolve()),
              site_targets={p:str(pathlib.Path(p).resolve()) for p in sys.path if p.endswith('/site-packages')})
if settings.get('lookup_helpers'):
    record['helpers'] = {}
    for name in ('semgrep-core-proprietary', 'semgrep', 'pysemgrep'):
        adjacent = pathlib.Path(sys.executable).parent/name
        found = dict(path=shutil.which(name), adjacent=str(adjacent) if adjacent.is_file() else None)
        record['helpers'][name] = found
        for binary in found.values():
            if binary: subprocess.run([binary], check=True)
with open(CALLS, 'a') as out: out.write(json.dumps(record)+'\n')
if settings.get('sleep'): time.sleep(settings['sleep'])
rows=[]
for name in files:
    for line,raw in enumerate(pathlib.Path(name).read_bytes().split(b'\n'),1):
        text = raw.decode('utf-8').rstrip('\r')
        if text.lstrip().startswith('#'): continue
        if 'eval(' in text or 'UNSAFE(' in text or 'os.system(' in text:
            rows.append(dict(check_id='fake.rule',path=name,start=dict(line=line),end=dict(line=line),
                             extra=dict(severity=settings.get('severity','ERROR'),lines=text,message='private '+text)))
value=dict(errors=[],results=rows,paths=dict(scanned=files))
mode=settings.get('mode')
if mode=='skipped': value['paths']['scanned']=files[:-1]
if mode=='errors': value['errors']=[dict(message='private error')]
if mode=='outside': value['paths']['scanned']=['../outside.py']
if mode=='bad-result': value['results']=[{}]
output=pathlib.Path(args[args.index('--output')+1])
output.write_text('bad json' if mode=='json' else json.dumps(value))
if settings.get('hook'):
    hook = subprocess.run([sys.executable, '-I', '-B', settings['hook']], capture_output=True)
    pathlib.Path(settings['hook'] + '.log').write_bytes(hook.stdout + hook.stderr)
    if hook.returncode: sys.exit(hook.returncode)
sys.exit(settings.get('exit',0))
'''


def fake_install(root, scripts):
    env = root / 'verified venv'
    venv.EnvBuilder(with_pip=False, symlinks=True).create(env)
    site = env / 'lib' / ('python%d.%d' % sys.version_info[:2]) / 'site-packages'
    package = site / 'semgrep'; package.mkdir()
    (package / '__init__.py').write_text('')
    calls, settings = root / 'scanner-calls.jsonl', root / 'scanner-settings.json'
    settings.write_text('{}')
    (package / 'console_scripts').mkdir(); (package / 'console_scripts/__init__.py').write_text('')
    (package / 'console_scripts/pysemgrep.py').write_text('SETTINGS=' + repr(str(settings)) + '\nCALLS=' + repr(str(calls)) + '\n' + FAKE)
    (package / 'bin').mkdir(); (package / 'bin/semgrep-core').write_bytes(b'fixture core')
    metadata = site / 'semgrep-1.183.0.dist-info'; metadata.mkdir()
    (metadata / 'METADATA').write_text('Version: 1.183.0\n')
    (metadata / 'RECORD').write_text('untrusted record\n')
    system = {'Darwin':'darwin','Linux':'linux'}[platform.system()]
    arch = {'arm64':'arm64','aarch64':'arm64','x86_64':'x86_64'}[platform.machine()]
    abi = 'cp%d%d' % sys.version_info[:2]
    manifest = scripts / ('bureau-semgrep-manifest-' + system + '-' + arch + '-' + abi + '.json')
    refresh_manifest(env, manifest, system, arch, abi)
    wrapper = root / 'network-wrapper.py'
    wrapper.write_text('import json,os,sys\nwith open(' + repr(str(root / 'wrapper-calls.jsonl'))
                       + ',"a") as out: out.write(json.dumps(sys.argv[1:])+"\\n")\nos.execv(sys.argv[1],sys.argv[1:])\n')
    return dict(venv=env, site=site, manifest=manifest, calls=calls, settings=settings,
                prefix=[sys.executable, '-I', '-B', str(wrapper)], system=system, arch=arch, abi=abi)


def refresh_manifest(env, path, system=None, arch=None, abi=None):
    scan = load('scan')
    before = json.loads(path.read_text()) if path.exists() else {}
    site = next((env / 'lib').glob('python*/site-packages'))
    value = dict(schema_version=1, tool='semgrep', version='1.183.0', os=system or before['os'],
                 arch=arch or before['arch'], python=abi or before['python'], files=scan.site_files(site))
    path.write_text(json.dumps(value))


def inject_isolation(scripts, fixture, available=True):
    # Patch only a trusted, temporary installed script. Production has no env or
    # repository configuration override for its probe or manifest directory.
    path = scripts / 'bureau-scan.py'
    path.write_text(path.read_text() + '\n\ndef network_prefix(system=None): return ' + repr(fixture['prefix'])
                    + '\n\ndef probe_network(prefix, root, env): return ' + repr(available) + '\n')


class SemgrepTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='bureau-semgrep-test-'); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.repo = self.root / 'repo'; self.repo.mkdir()
        self.m = load('semgrep'); self.s = self.m.scan
        self.fixture = fake_install(self.root, self.root)
        self.config = dict(review=dict(semgrep=dict(enabled=True)))
        self.env = {**os.environ, **git_maintenance.OFF_ENV, 'GIT_CONFIG_GLOBAL':os.devnull, 'GIT_CONFIG_NOSYSTEM':'1',
                    'GIT_AUTHOR_NAME':'Test', 'GIT_AUTHOR_EMAIL':'test@example.invalid',
                    'GIT_COMMITTER_NAME':'Test', 'GIT_COMMITTER_EMAIL':'test@example.invalid'}
        self.git('init', '-qb', 'main')
        (self.repo / 'a.js').write_text('eval(userText);\nconst value = 1;\n')
        self.commit(); self.base = self.git('rev-parse', 'HEAD')
        (self.repo / 'a.js').write_text('eval(userText);\nconst value = 2;\n')
        self.commit(); self.head = self.git('rev-parse', 'HEAD')

    def git(self, *args):
        return subprocess.check_output(['git','-C',str(self.repo),*args], env=self.env,
                                       stderr=subprocess.STDOUT, text=True).strip()

    def commit(self):
        self.git('add', '.'); self.git('commit', '-qm', 'test: scanner fixture')
        self.head = self.git('rev-parse', 'HEAD')

    def measure(self, **kwargs):
        return self.m.measure(self.repo, self.config, self.base, self.head, _state=self.root / 'state',
                              _venv=self.fixture['venv'], _manifests=self.root,
                              _prefix=self.fixture['prefix'], _probe=lambda *args: True, **kwargs)

    def calls(self):
        return [json.loads(s) for s in self.fixture['calls'].read_text().splitlines()] if self.fixture['calls'].exists() else []

    def mode(self, **values):
        self.fixture['settings'].write_text(json.dumps(values))

    def test_off_is_no_operation(self):
        self.config = {}
        self.assertIsNone(self.measure()); self.assertEqual(self.calls(), [])
        self.assertFalse((self.root / 'state').exists())

    def test_exact_environment_flags_private_snapshot_and_network_every_call(self):
        secrets = {k:'private-secret' for k in ('OPENAI_API_KEY','NVIDIA_INFERENCE_KEY','GH_TOKEN','BUREAU_SECRET',
                                              'SKILLSPECTOR_API_KEY','PYTHONPATH','SEMGREP_APP_TOKEN','SEMGREP_RULES')}
        with patch.dict(os.environ, secrets), patch.object(self.s, 'run_process', wraps=self.s.run_process) as launch:
            value = self.measure()
        self.assertEqual(value['receipt']['status'], 'measured', value)
        calls = self.calls(); self.assertEqual(len(calls), 2)
        for side, call in zip(('base','head'), calls):
            env = call['env']; root = Path(env['HOME']).parent
            expected = dict(PATH='/usr/bin:/bin', LANG='C.UTF-8', TZ='UTC',
                            HOME=str(root/'home'), XDG_CONFIG_HOME=str(root/'config'),
                            XDG_CACHE_HOME=str(root/'cache'), TMPDIR=str(root/'tmp'), **self.m.SWITCHES)
            # macOS's system interpreter inserts this after exec, even under -I -S.
            # Assert the actual exec environment separately; no caller value passes.
            observed = dict(env)
            if sys.platform == 'darwin': observed.pop('__CF_USER_TEXT_ENCODING', None)
            self.assertEqual(observed, expected)
            for invocation in launch.call_args_list:
                self.assertEqual(invocation.kwargs['env'], expected)
            for key in secrets: self.assertNotIn(key, env)
            expected_flags = ['scan','--oss-only','--metrics','off','--disable-version-check','--novcs',
                              '--project-root','.','--no-git-ignore','--x-ignore-semgrepignore-files',
                              '--disable-nosem','--no-rewrite-rule-ids']
            self.assertEqual(call['argv'][:len(expected_flags)], expected_flags)
            for flag, argument in (('--timeout','30'),('--max-target-bytes','1000000'),('--project-root','.')):
                self.assertEqual(call['argv'][call['argv'].index(flag)+1], argument)
            self.assertEqual(call['argv'][call['argv'].index('--')+1:], ['a.js'])
            self.assertNotIn('--error', call['argv']); self.assertNotIn('--baseline-commit', call['argv'])
            self.assertEqual(Path(call['cwd']).name, side); self.assertNotIn('.git', call['files'])
            self.assertTrue(call['cache_empty']); self.assertEqual(call['marker'], 'source')
            self.assertFalse(root.exists(), 'temporary boundary survived scan')
            if sys.version_info >= (3, 10):
                for flag in ('-I','-B','-X','-m'): self.assertIn(flag, call['python'])
        wrapped = [json.loads(s) for s in (self.root / 'wrapper-calls.jsonl').read_text().splitlines()]
        self.assertEqual(len(wrapped), 3)  # isolated interpreter probe plus both scan sides
        self.assertIn('-S',wrapped[0])
        self.assertEqual(len({args[0] for args in wrapped}), 3, 'invocations shared a shadow')
        for args in wrapped:
            binary = Path(args[0])
            self.assertEqual(binary.parts[-3:], ('shadow', 'bin', 'python'))
            self.assertEqual(binary.parents[3], root)
        for args in wrapped[1:]:
            for flag in ('-I','-B','-X','-m'): self.assertIn(flag, args)

    def test_unverified_venv_helpers_are_neither_found_nor_executed(self):
        markers = []
        for name in ('semgrep-core-proprietary', 'semgrep', 'pysemgrep'):
            marker = self.root / (name + '-executed'); markers.append(marker)
            binary = self.fixture['venv'] / 'bin' / name
            binary.write_text('#!' + sys.executable + '\nfrom pathlib import Path\nPath(' + repr(str(marker)) + ').touch()\n')
            binary.chmod(0o755)
        self.mode(lookup_helpers=True)
        value = self.measure()
        self.assertEqual(value['receipt']['status'], 'measured', value)
        self.assertEqual(len(self.calls()), 2)
        self.assertFalse(any(marker.exists() for marker in markers), 'unverified venv helper executed')
        for call in self.calls():
            self.assertEqual(call['helpers'], {name:dict(path=None, adjacent=None)
                for name in ('semgrep-core-proprietary', 'semgrep', 'pysemgrep')})

    def test_shadow_prefix_has_only_python_and_the_verified_site(self):
        value = self.measure()
        self.assertEqual(value['receipt']['status'], 'measured', value)
        for call in self.calls():
            shadow = Path(call['prefix'])
            site = shadow/'lib'/self.fixture['site'].parent.name/'site-packages'
            self.assertEqual(shadow.name, 'shadow')
            self.assertEqual(Path(call['executable']), shadow/'bin/python')
            self.assertEqual(call['executable_entries'], ['python'])
            self.assertEqual(call['site_paths'], [str(site)])
            self.assertEqual(call['site_targets'], {str(site):str(self.fixture['site'])})
            base = (self.fixture['venv']/'bin/python').resolve()
            self.assertEqual(call['python_target'], str(base))
            self.assertEqual(call['shadow_cfg'], 'home = '+str(base.parent)+'\ninclude-system-site-packages = false\n')

    def test_receipts_from_operator_venv_execution_are_not_reused(self):
        digest = self.s.digest
        def legacy_digest(value):
            if isinstance(value, dict) and 'version' in value and 'lock' in value:
                value = {k:v for k,v in value.items() if k != 'execution'}
            return digest(value)
        with patch.object(self.s, 'digest', side_effect=legacy_digest):
            prior = self.measure()
        current = self.measure(reuse=True)
        self.assertNotEqual(prior['receipt']['pin_digest'], current['receipt']['pin_digest'])
        self.assertNotEqual(prior['receipt_path'], current['receipt_path'])
        self.assertEqual(len(self.calls()), 4, 'receipt from the vulnerable boundary was reused')

    def test_duplicate_operator_cfg_cannot_enable_system_startup(self):
        system = self.root/'system-site-packages'; system.mkdir()
        markers = []
        for name in ('sitecustomize.py', 'system.pth'):
            marker = self.root/(name+'-executed'); markers.append(marker)
            (system/name).write_text('import pathlib; pathlib.Path('+repr(str(marker))+').touch()\n')
        cfg = self.fixture['venv']/'pyvenv.cfg'
        cfg.write_text(cfg.read_text()+'include-system-site-packages = true\n')
        self.mode(system_site=str(system))
        value = self.measure()
        self.assertEqual(value['receipt']['status'], 'measured', value)
        self.assertEqual(len(self.calls()), 2)
        self.assertFalse(any(p.exists() for p in markers), 'unverified system startup executed')
        for call in self.calls():
            self.assertNotIn(call['base_prefix'], call['site_prefixes'])
        # Execution also works without an operator config at all.
        cfg.unlink()
        self.assertEqual(self.measure()['receipt']['status'], 'measured')

    def test_shadow_rejects_invalid_base_and_ambiguous_or_wrong_library_minor(self):
        for kind in ('non-executable', 'directory'):
            with self.subTest(kind=kind):
                base = self.root/kind
                if kind == 'directory': base.mkdir()
                else: base.write_text('not executable'); base.chmod(0o600)
                python = self.fixture['venv']/'bin/python'
                original = python.readlink(); python.unlink(); python.symlink_to(base)
                try:
                    with self.assertRaisesRegex(ValueError, 'regular executable'):
                        self.s.python_argv(self.fixture['venv'], self.root/('invalid-'+kind), '-c', 'pass')
                finally:
                    python.unlink(); python.symlink_to(original)
        extra = self.fixture['venv']/'lib/python3.0/site-packages'; extra.mkdir(parents=True)
        with patch.object(self.s, 'run_process', wraps=self.s.run_process) as run:
            value = self.measure()
        self.assertEqual(value['receipt']['status'], 'unavailable', value)
        self.assertFalse(run.called)
        shutil.rmtree(extra.parent)
        with patch.object(self.s, 'interpreter', return_value=(self.fixture['system'], self.fixture['arch'], 'cp300')):
            value = self.measure()
        self.assertEqual(value['receipt']['status'], 'unavailable', value)
        self.assertEqual(self.calls(), [])

    def test_network_probe_available_and_unavailable_without_host_dependency(self):
        for system in ('Linux','Darwin','unsupported'):
            with patch.object(self.s.Path, 'is_file', return_value=True): prefix = self.s.network_prefix(system)
            if system == 'Linux': self.assertEqual(prefix, ['/usr/bin/unshare','-rn','--'])
            elif system == 'Darwin': self.assertEqual(prefix[:2], ['/usr/bin/sandbox-exec','-f'])
            else: self.assertIsNone(prefix)
        env_root = self.root / 'probe'; env_root.mkdir()
        with patch.object(self.s, 'run_process', return_value=subprocess.CompletedProcess([],0,b'',b'')) as run:
            self.assertTrue(self.s.probe_network(['wrapper'], env_root, {}))
            self.assertEqual(run.call_args.args[0], ['wrapper','/usr/bin/true'])
        with patch.object(self.s, 'run_process', return_value=subprocess.CompletedProcess([],1,b'',b'')):
            self.assertFalse(self.s.probe_network(['wrapper'], env_root, {}))
        self.config['review']['semgrep']['mode'] = 'blocking'
        result = self.m.measure(self.repo, self.config, self.base, self.head, _state=self.root / 'state',
            _venv=self.fixture['venv'], _manifests=self.root, _prefix=['never-execute'], _probe=lambda *a: False)
        self.assertEqual(result['receipt']['status'], 'not_measured')
        self.assertEqual(result['receipt']['reason'], 'network isolation unavailable')
        self.assertTrue(result['receipt']['blocking']); self.assertEqual(self.calls(), [])
        profile = (SCRIPTS / 'bureau-scan-nonet.sb').read_text()
        self.assertIn('(allow default)', profile); self.assertIn('(deny network*)', profile)

    def test_unavailable_isolation_still_binds_changed_pin_assets(self):
        def measure():
            return self.m.measure(self.repo,self.config,self.base,self.head,_state=self.root/'state',
                _venv=self.fixture['venv'],_manifests=self.root,_prefix=['unused'],_probe=lambda *a:False)['receipt']
        first=measure()
        path=self.fixture['manifest'];path.write_text(path.read_text()+'\n')
        second=measure()
        self.assertEqual(first['status'],'not_measured');self.assertEqual(second['status'],'not_measured')
        self.assertNotEqual(first['pin_digest'],second['pin_digest']);self.assertEqual(self.calls(),[])

    def test_artifact_modified_missing_added_metadata_and_manifest_minor(self):
        source = self.fixture['site'] / 'semgrep/console_scripts/pysemgrep.py'
        original = source.read_bytes()
        for change in ('modified','missing','additional'):
            with self.subTest(change=change):
                extra = self.fixture['site'] / 'intruder.py'
                if change == 'modified': source.write_bytes(original + b'\n# changed\n')
                elif change == 'missing': source.unlink()
                else: extra.write_text('raise RuntimeError("untrusted")')
                # A forged RECORD cannot confer authority.
                (self.fixture['site'] / 'semgrep-1.183.0.dist-info/RECORD').write_text('updated to match tampering')
                with patch.object(self.s, 'run_process', wraps=self.s.run_process) as run:
                    value = self.measure()
                self.assertEqual(value['receipt']['status'], 'unavailable', value)
                self.assertFalse(run.called, 'interpreter probe preceded manifest verification')
                self.assertEqual(self.calls(), [])
                source.write_bytes(original); extra.unlink(missing_ok=True)
        marker=self.root/'unverified-startup-executed'
        for name in ('sitecustomize.py','unverified.pth'):
            path=self.fixture['site']/name
            path.write_text('import pathlib; pathlib.Path('+repr(str(marker))+').write_text("executed")\n')
            self.assertEqual(self.measure()['receipt']['status'],'unavailable')
            self.assertFalse(marker.exists(), 'unverified site startup ran in the interpreter probe')
            path.unlink()
        manifest = self.fixture['manifest']; original_manifest = manifest.read_text()
        manifest.unlink(); value = self.measure()
        self.assertEqual(value['receipt']['status'], 'unavailable'); self.assertIn(manifest.name, value['receipt']['reason'])
        manifest.write_text(original_manifest)
        data = json.loads(original_manifest); data['python'] = 'cp300'; manifest.write_text(json.dumps(data))
        self.assertEqual(self.measure()['receipt']['status'], 'unavailable'); self.assertEqual(self.calls(), [])

    def test_artifact_reverified_before_each_side(self):
        original=self.m.run_side
        def tamper(venv,prefix,root,env,side,directory,files,rules):
            result=original(venv,prefix,root,env,side,directory,files,rules)
            if side=='base': (self.fixture['site']/'semgrep/__init__.py').write_text('# changed between scan sides\n')
            return result
        with patch.object(self.m,'run_side',side_effect=tamper): value=self.measure()
        self.assertEqual(value['receipt']['status'],'unavailable')
        self.assertEqual([Path(c['cwd']).name for c in self.calls()],['base'])

    def test_tampered_valid_header_adjacent_bytecode_never_executes(self):
        source = self.fixture['site'] / 'semgrep/console_scripts/pysemgrep.py'
        original = source.read_bytes(); before = source.stat()
        poison = b'raise RuntimeError("cached poison executed")\n'
        source.write_bytes(poison + b'#' * (len(original)-len(poison)))
        os.utime(source, (before.st_atime, before.st_mtime))
        py_compile.compile(str(source), doraise=True, invalidation_mode=py_compile.PycInvalidationMode.TIMESTAMP)
        source.write_bytes(original); os.utime(source, (before.st_atime, before.st_mtime))
        self.assertEqual(self.measure()['receipt']['status'], 'measured')
        self.assertEqual([c['marker'] for c in self.calls()], ['source','source'])

    def test_occurrence_aware_new_existing_and_moved(self):
        value = self.measure()['receipt']; self.assertEqual((value['findings_new'],value['findings_existing']), (0,1))
        (self.repo / 'a.js').write_text('eval(userText);\n   eval(userText);   \nconst value = 2;\n')
        self.commit(); value = self.measure()
        self.assertEqual((value['receipt']['findings_new'],value['receipt']['findings_existing']), (1,1))
        self.assertEqual(len(value['projection']['findings']), 2)
        self.assertEqual({r['count_rose'] for r in value['projection']['findings']}, {'count rose from 1 to 2'})
        self.git('mv','a.js','other.js'); self.commit(); value = self.measure()['receipt']
        self.assertEqual((value['findings_new'],value['findings_existing']), (2,0))

    def test_comment_separators_do_not_shift_physical_finding_lines(self):
        for separator in (b'\x0b', '\u2028'.encode('utf-8')):
            with self.subTest(separator=separator):
                source = self.repo / 'physical.py'
                source.write_bytes(b'# header\nos.system(old_input)\n')
                self.commit(); self.base = self.head
                source.write_bytes(b'#' + separator + b'os.system(old_input)\nos.system(new_input)\n')
                self.commit()
                value = self.measure(); receipt = value['receipt']
                self.assertEqual(receipt['status'], 'measured', value)
                self.assertEqual((receipt['findings_new'], receipt['findings_existing']), (1,0))
                self.assertEqual([row['line'] for row in value['projection']['findings']], [2])
                report = json.loads(Path(receipt['report_path']).read_text())
                self.assertEqual([row['start']['line'] for row in report['head']['json']['results']], [2])

    def test_physical_lines_keep_crlf_normalization_and_bounds_check(self):
        snapshot = self.root / 'physical'; snapshot.mkdir()
        (snapshot / 'a.py').write_bytes(b'#\x0bdecoy\n  os.system(input)  \r\n')
        row = dict(check_id='fake.rule', path='a.py', start=dict(line=2), end=dict(line=2),
                   extra=dict(severity='ERROR'))
        report = dict(errors=[], results=[row], paths=dict(scanned=['a.py']))
        findings = self.m.parse_report(report, ['a.py'], snapshot)
        self.assertEqual(findings[0]['match_sha256'], self.s.sha(b'os.system(input)'))
        row['end']['line'] = 4
        with self.assertRaisesRegex(ValueError, 'malformed Semgrep finding'):
            self.m.parse_report(report, ['a.py'], snapshot)

    def test_empty_eligible_set_no_semgrep_call(self):
        self.head = self.base
        value = self.measure()['receipt']; self.assertEqual(value['status'], 'not_applicable')
        self.assertEqual(self.calls(), [])

    def test_empty_eligible_set_needs_no_venv_isolation_or_runtime_verification(self):
        self.base = self.head
        (self.repo / 'README.md').write_text('Only documentation changed.\n'); self.commit()
        self.config['review']['semgrep']['mode'] = 'blocking'
        for unavailable in ('venv', 'isolation'):
            with self.subTest(unavailable=unavailable), \
                 patch.object(self.s, 'environment') as environment, \
                 patch.object(self.s, 'network_prefix') as prefix, \
                 patch.object(self.s, 'probe_network', return_value=False) as probe, \
                 patch.object(self.s, 'interpreter') as interpreter, \
                 patch.object(self.s, 'verify_venv') as verify, \
                 patch.object(self.s, 'run_process') as process, \
                 patch.object(self.m, 'run_side') as scanner:
                value = self.m.measure(self.repo, self.config, self.base, self.head, _state=self.root/'state',
                    _venv=self.root/'absent-venv' if unavailable == 'venv' else self.fixture['venv'],
                    _manifests=self.root)
                receipt = value['receipt']
                self.assertEqual(receipt['status'], 'not_applicable', value)
                self.assertFalse(receipt['blocking'])
                for mock in (environment, prefix, probe, interpreter, verify, process, scanner):
                    mock.assert_not_called()
                pin = dict(version=self.m.VERSION, lock=self.s.file_sha(SCRIPTS/'bureau-semgrep-lock.txt'), execution='shadow-venv-v1',
                           manifests=[(p.name, self.s.file_sha(p)) for p in sorted(self.root.glob('bureau-semgrep-manifest-*.json'))])
                self.assertEqual(receipt['pin_digest'], self.s.digest(pin))
                self.assertEqual(self.calls(), [])
                self.assertFalse((self.root/'wrapper-calls.jsonl').exists())
                if unavailable == 'venv':
                    first_pin = receipt['pin_digest']
                    path = self.fixture['manifest']; path.write_text(path.read_text() + '\n')
                else:
                    self.assertNotEqual(receipt['pin_digest'], first_pin)

    def test_incomplete_output_nonzero_json_errors_and_skipped_are_not_measured(self):
        for mode in ('skipped','errors','outside','bad-result','json','exit'):
            self.mode(mode=mode, exit=1 if mode == 'exit' else 0)
            with self.subTest(mode=mode):
                value = self.measure(); self.assertEqual(value['receipt']['status'], 'not_measured', value)
                self.assertNotIn('private', json.dumps(value['projection']))
        with patch.object(self.s, 'run_process', side_effect=subprocess.TimeoutExpired('scan', 120)):
            self.assertEqual(self.measure()['receipt']['status'], 'not_measured')

    def test_hostile_head_policy_native_configs_nosem_and_base_suppression(self):
        suppression = [dict(file='new.js', rule_id='fake.rule', reason='Approved exact exception')]
        for name, data in {'.semgrepignore':'*\n', '.semgrep.yml':'rules: []\n', '.semgrep/rules.yml':'rules: []\n',
                           '.bureau/semgrep-ignore.json':json.dumps(suppression), 'new.js':'eval(userText); // nosemgrep\n'}.items():
            path = self.repo / name; path.parent.mkdir(parents=True, exist_ok=True); path.write_text(data)
        self.commit(); value = self.measure()
        self.assertEqual(value['receipt']['findings_new'], 1)
        self.assertEqual(value['receipt']['findings_suppressed'], 0)
        self.assertTrue(all(not c['native'] for c in self.calls()))
        report = json.loads(Path(value['receipt']['report_path']).read_text())
        self.assertIn('.semgrepignore', report['snapshots']['head']['removed_native_config'])
        self.git('checkout', '-q', self.base)
        path = self.repo / self.m.IGNORE; path.parent.mkdir(exist_ok=True); path.write_text(json.dumps(suppression))
        self.commit(); self.base = self.head
        (self.repo / 'new.js').write_text('eval(userText); // nosemgrep\n'); self.commit()
        value = self.measure()['receipt']
        self.assertEqual((value['findings_new'], value['findings_suppressed']), (0,1))

    def test_extra_rules_and_suppressions_only_from_base(self):
        (self.repo / 'custom.yml').write_text('rules: []\n# base authority\n'); self.commit(); self.base = self.head
        self.config['review']['semgrep']['extra_rules'] = 'custom.yml'
        (self.repo / 'custom.yml').write_text('rules: []\n# hostile head\n')
        (self.repo / 'a.js').write_text('eval(userText);\n// changed\n'); self.commit()
        self.assertEqual(self.measure()['receipt']['status'], 'measured')
        self.assertTrue(all('base authority' in c['configs'][1] and 'hostile' not in c['configs'][1] for c in self.calls()))
        for row in (dict(file='*',rule_id='fake.rule',reason='broad'),dict(file='a.js',rule_id='fake.rule',reason='')):
            with self.assertRaises(ValueError): self.m.suppressions(json.dumps([row]))

    def test_settings_digest_is_canonical_raw_configuration_block(self):
        value=self.measure()['receipt']
        self.assertEqual(value['settings_digest'],self.s.digest(self.config['review']['semgrep']))
        self.config['review']['semgrep']['mode']='advisory'
        self.assertNotEqual(value['settings_digest'],self.measure()['receipt']['settings_digest'])

    def test_modes_severity_and_status_semantics(self):
        for mode in ('advisory','blocking'):
            for status in self.s.STATUSES:
                value = self.s.blocking(dict(mode=mode,block_severity='ERROR'), status, ['WARNING'])
                self.assertEqual(value, mode=='blocking' and status in ('unavailable','not_measured','reduced_coverage'))
        for floor in ('INFO','WARNING','ERROR'):
            self.assertEqual(self.s.blocking(dict(mode='blocking',block_severity=floor),'measured',['WARNING']), floor!='ERROR')

    def test_durable_write_once_identity_validation_reuse_and_retention(self):
        a = self.measure(pr=1); b = self.measure(pr=1)
        self.assertNotEqual(a['receipt_path'], b['receipt_path'])
        self.assertEqual(a['receipt'], b['receipt'] | {k:a['receipt'][k] for k in ('report_path','report_sha256','projection_path','projection_sha256')})
        bound = {k:a['receipt'][k] for k in self.s.BOUND}; options = self.m.settings(self.config)
        self.assertIsNotNone(self.s.validate_receipt(Path(a['receipt_path']),bound,options))
        before = len(self.calls()); reused = self.measure(reuse=True)
        self.assertEqual(len(self.calls()),before); self.assertEqual(reused['receipt_path'],b['receipt_path'])
        for key in self.s.BOUND:
            changed = {**bound,key:'other'}
            self.assertIsNone(self.s.validate_receipt(Path(a['receipt_path']),changed,options))
        report = Path(b['receipt']['report_path']); report.write_text('tampered')
        self.assertIsNone(self.s.validate_receipt(Path(b['receipt_path']),bound,options))
        for n in range(22):
            self.s.persist(self.root/'retention', bound, options, 'measured', None, {}, pr=7 if n==0 else None, open_prs=[7])
        attempts = list((self.root/'retention/scans/semgrep').glob('*/*/receipt.json'))
        self.assertEqual(len(attempts),21)
        with self.s.locked(self.root/'retention/scans/semgrep'):
            self.s.retain(self.root/'retention/scans/semgrep', open_prs=[])
        self.assertEqual(len(list((self.root/'retention/scans/semgrep').glob('*/*/receipt.json'))),20)

    def test_projection_bounded_and_never_contains_matches(self):
        (self.repo/'new.js').write_text('eval(secretValue);\n'*25); self.commit()
        value = self.measure(); projected = value['projection']
        self.assertEqual(len(projected['findings']),20); self.assertEqual(projected['findings_omitted'],5)
        self.assertNotIn('secretValue',json.dumps(projected)); self.assertNotIn('message',json.dumps(projected))
        self.assertEqual(set(projected['findings'][0]), {'tool','rule_id','file','line','severity','count_rose'})

    def test_state_dir_reuses_common_git_root_and_operator_venv_default(self):
        self.assertEqual(self.s.state_dir(self.repo), self.repo/'.git/bureau')
        with patch.dict(os.environ, {'BUREAU_SEMGREP_VENV':str(self.fixture['venv'])}):
            value = self.m.measure(self.repo,self.config,self.base,self.head,_manifests=self.root,
                _prefix=self.fixture['prefix'],_probe=lambda *a:True)
        self.assertEqual(value['receipt']['status'],'measured')
        self.assertTrue(Path(value['receipt_path']).is_relative_to(self.repo/'.git/bureau/scans'))

    def test_installer_copies_all_immediate_scanner_assets_without_maintainer_tool(self):
        (self.repo/'.bureau.json').write_text(json.dumps(self.config))
        process=subprocess.run([sys.executable,'-I','-B',str(ROOT/'scripts/bureau_install.py'),'assets',
            '--repo',str(self.repo),'--scope','scripts','--apply'],env=self.env,capture_output=True,text=True,timeout=30)
        self.assertEqual(process.returncode,0,process.stdout+process.stderr)
        for name in ('bureau-scan.py','bureau-scan-nonet.sb','bureau-semgrep.py','bureau-semgrep.sh',
                     'bureau-semgrep-lock.txt','bureau-semgrep-rules.yml'):
            self.assertEqual((self.repo/'scripts'/name).read_bytes(),(SCRIPTS/name).read_bytes())
        self.assertFalse((self.repo/'scripts/semgrep_pin.py').exists())

    def test_config_defaults_invalid_even_when_disabled_and_doctor(self):
        self.assertEqual(self.m.settings({}),dict(enabled=False,mode='advisory',block_severity='ERROR',extra_rules=None))
        doctor = load('doctor')
        base = dict(linear=dict(teams=[dict(id='team',key='TEAM',states=dict(build='build'))]),agents={},repo={})
        for raw in (dict(enabled='true'),dict(mode='audit'),dict(block_severity='critical'),dict(extra_rules='../bad.yml'),
                    dict(extra_rules='/outside.yml'),dict(extra_rules='a.json'),dict(version='latest'),None):
            config = {**base,'review':dict(semgrep=raw)}
            with self.subTest(raw=raw):
                with self.assertRaises(ValueError): self.m.settings(config)
                self.assertTrue(any('semgrep' in e for e in doctor.validate(config)))


class PinTests(unittest.TestCase):
    def test_synthetic_wheels_hash_anchor_mapping_exclusions_and_refusal(self):
        pin = load('pin', ROOT/'scripts/semgrep_pin.py')
        with tempfile.TemporaryDirectory(prefix='bureau-semgrep-pin-') as tmp:
            root = Path(tmp); wheels = root/'wheels'; wheels.mkdir()
            wheel = wheels/'semgrep-1.183.0-py3-none-any.whl'
            with zipfile.ZipFile(wheel,'w') as archive:
                for name,data in {'semgrep/__init__.py':'source','semgrep/bin/semgrep-core':'binary',
                                  'semgrep-1.183.0.dist-info/METADATA':'metadata',
                                  'semgrep-1.183.0.dist-info/RECORD':'ignored','semgrep-1.183.0.dist-info/INSTALLER':'ignored',
                                  'semgrep-1.183.0.dist-info/direct_url.json':'ignored','semgrep-1.183.0.dist-info/REQUESTED':'ignored',
                                  'semgrep/__pycache__/poison.pyc':'ignored',
                                  'semgrep-1.183.0.data/platlib/native.so':'native'}.items(): archive.writestr(name,data)
            lock=root/'lock.txt';lock.write_text('semgrep==1.183.0 --hash=sha256:'+pin.scan.file_sha(wheel)+'\n')
            value=pin.manifest(wheels,lock,'linux','x86_64','cp312')
            self.assertEqual(set(value['files']),{'semgrep/__init__.py','semgrep/bin/semgrep-core',
                                                  'semgrep-1.183.0.dist-info/METADATA','native.so'})
            lock.write_text('# --hash=sha256:'+pin.scan.file_sha(wheel)+'\n')
            with self.assertRaisesRegex(ValueError,'SHA-256 absent'): pin.manifest(wheels,lock,'linux','x86_64','cp312')
            lock.write_text('semgrep==1.183.0 --hash=sha256:'+pin.scan.file_sha(wheel)+'\n')
            with zipfile.ZipFile(wheel,'a') as archive: archive.writestr('tampered.py','changed')
            with self.assertRaisesRegex(ValueError,'SHA-256 absent'): pin.manifest(wheels,lock,'linux','x86_64','cp312')


class RealBinaryTests(unittest.TestCase):
    @unittest.skipUnless(os.environ.get('BUREAU_SEMGREP_VENV'), 'BUREAU_SEMGREP_VENV is not set')
    @unittest.skipUnless(sys.platform == 'linux' and os.environ.get('GITHUB_ACTIONS') == 'true' and os.environ.get('RUNNER_ENVIRONMENT') == 'github-hosted', 'real scanner runs on GitHub-hosted Ubuntu only')
    def test_rules_and_one_isolated_scan(self):
        m=load('semgrep'); s=m.scan; env_path=Path(os.environ['BUREAU_SEMGREP_VENV']).resolve()
        with tempfile.TemporaryDirectory(prefix='bureau-semgrep-real-') as tmp:
            root=Path(tmp); env=s.environment(root,m.SWITCHES); prefix=s.network_prefix()
            self.assertTrue(s.probe_network(prefix,root,env),'network isolation unavailable')
            system,arch,abi=s.interpreter(env_path,root,env,prefix)
            manifest=SCRIPTS/('bureau-semgrep-manifest-'+system+'-'+arch+'-'+abi+'.json')
            s.verify_venv(env_path,manifest,'semgrep',m.VERSION,system,arch,abi)
            # Rule fixtures run through the production invocation: every ruleid line needs the
            # rule on the next line, every ok line forbids it, and nothing else may match.
            fixtures=ROOT/'tests/fixtures/semgrep'; names=sorted(p.name for p in fixtures.iterdir())
            raw=m.run_side(env_path,prefix,root,env,'fixtures',fixtures,names,[SCRIPTS/'bureau-semgrep-rules.yml'])
            self.assertEqual(raw['exit_code'],0,raw)
            got={(r['file'],r['line'],r['rule_id']) for r in m.parse_report(raw['json'],names,fixtures)}
            want,forbid=set(),set()
            for name in names:
                for number,line in enumerate((fixtures/name).read_text().splitlines(),1):
                    match=re.search(r'(?:#|//)\s*(ruleid|ok):\s*([A-Za-z0-9_.-]+)',line)
                    if match: (want if match.group(1)=='ruleid' else forbid).add((name,number+1,match.group(2)))
            self.assertTrue(want and forbid); self.assertEqual(got,want); self.assertFalse(got & forbid)
            snapshot=root/'head';snapshot.mkdir();(snapshot/'sample.py').write_text('import os\nos.system(user_input)\n')
            s.verify_venv(env_path,manifest,'semgrep',m.VERSION,system,arch,abi)
            raw=m.run_side(env_path,prefix,root,env,'head',snapshot,['sample.py'],[SCRIPTS/'bureau-semgrep-rules.yml'])
            self.assertEqual(raw['exit_code'],0,raw)
            self.assertTrue(m.parse_report(raw['json'],['sample.py'],snapshot))


if __name__ == '__main__': unittest.main()
