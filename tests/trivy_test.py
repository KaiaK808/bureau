"""Offline Trivy qualification: real snapshots and receipts, fake pinned executable."""
import copy
from datetime import datetime, timedelta, timezone
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

import git_maintenance

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / 'templates/scripts'


def load(name, path=None):
    spec = importlib.util.spec_from_file_location('trivy_test_' + name, path or SCRIPTS / ('bureau-' + name + '.py'))
    value = importlib.util.module_from_spec(spec); spec.loader.exec_module(value)
    return value


FAKE = r'''
import json, os, pathlib, shutil, subprocess, sys, time
sys.dont_write_bytecode = True
args = sys.argv[1:]
options = json.loads(pathlib.Path(SETTINGS).read_text())
record = dict(argv=args, env=dict(os.environ), cwd=os.getcwd(), executable=sys.argv[0],
              entries=sorted(os.listdir(pathlib.Path(sys.argv[0]).parent)),
              run_mode=pathlib.Path(sys.argv[0]).parent.parent.stat().st_mode & 0o777,
              bin_mode=pathlib.Path(sys.argv[0]).parent.stat().st_mode & 0o777,
              helper=shutil.which('trivy-helper'), adjacent=(pathlib.Path(sys.argv[0]).parent/'trivy-helper').exists())
cache=pathlib.Path(args[args.index('--cache-dir')+1])
record['cache_files']=sorted(str(p.relative_to(cache)) for p in cache.rglob('*') if p.is_file())
for key in ('--config','--ignorefile'):
    if key in args: record[key]=pathlib.Path(args[args.index(key)+1]).read_text()
if '--download-db-only' in args or '--download-java-db-only' in args:
    kind='java-db' if '--download-java-db-only' in args else 'db'
    folder=cache/kind;folder.mkdir(exist_ok=True)
    (folder/'metadata.json').write_text(json.dumps(options['metadata']))
    (folder/('trivy-java.db' if kind=='java-db' else 'trivy.db')).write_bytes(b'verified database fixture')
    record['refresh']=True
else:
    directory=pathlib.Path(args[-1]);side=pathlib.Path(args[args.index('--output')+1]).stem
    record['side']=side
    rows=[]
    if '--input' in args:
        rows=[dict(Target='fixture image', Vulnerabilities=[dict(VulnerabilityID='CVE-FIXTURE',PkgName='pkg',InstalledVersion='1',Severity='CRITICAL')])]
    else:
        record['files']={str(p.relative_to(directory)):p.read_text(errors='replace') for p in directory.rglob('*') if p.is_file()}
        for name,text in record['files'].items():
            leaf=pathlib.Path(name).name
            if leaf in ('package-lock.json','npm-shrinkwrap.json','pnpm-lock.yaml','yarn.lock','uv.lock','poetry.lock','go.mod','go.sum') or leaf.startswith('requirements') or leaf.lower().startswith('dockerfile') or leaf.endswith(('.tf','.dockerfile')) or 'compose' in leaf:
                result=dict(Target=name, Vulnerabilities=[], Misconfigurations=[])
                if leaf.startswith('requirements'):
                    for raw in text.split('\n'):
                        if 'badpkg==' in raw:
                            result['Vulnerabilities'].append(dict(VulnerabilityID='CVE-FIXTURE',PkgName='badpkg',InstalledVersion=raw.split('==')[1].strip(),Severity=options.get('severity','HIGH'),Description='private-source'))
                if leaf=='Dockerfile' or leaf.endswith('.tf'):
                    for line,raw in enumerate(text.split('\n'),1):
                        if 'UNSAFE' in raw:
                            result['Misconfigurations'].append(dict(ID='AVD-FIXTURE',Severity='HIGH',CauseMetadata=dict(StartLine=line,EndLine=line,Resource='' if options.get('no_resource') else raw.split('UNSAFE')[-1].strip()),Message='private-source'))
                    if 'NOHEALTH' in text:
                        # The real pinned release reports file-level checks without cause lines.
                        result['Misconfigurations'].append(dict(ID='DS-0026',Severity='LOW',Status='FAIL',CauseMetadata=dict(Provider='Dockerfile',Service='general')))
                        result['Misconfigurations'].append(dict(ID='DS-PASS',Severity='HIGH',Status='PASS',CauseMetadata=dict(Provider='Dockerfile',Service='general')))
                if name!=options.get('missing'):rows.append(result)
    value=dict(SchemaVersion=2,Results=rows)
    if side==options.get('bad_side'): value=dict(SchemaVersion=2)
    pathlib.Path(args[args.index('--output')+1]).write_text('bad json' if options.get('bad_json') else json.dumps(value))
with open(CALLS,'a') as out:out.write(json.dumps(record)+'\n')
if options.get('hook') and not pathlib.Path(options['hook']+'.done').exists():
    pathlib.Path(options['hook']+'.done').write_text('once')
    proc=subprocess.run([PYTHON,'-I','-B',options['hook']],capture_output=True)
    pathlib.Path(options['hook']+'.log').write_bytes(proc.stdout+proc.stderr)
    if proc.returncode:sys.exit(proc.returncode)
if options.get('sleep'):time.sleep(options['sleep'])
if options.get('fork'):
    child=subprocess.Popen(['/bin/sleep','30']);pathlib.Path(options['fork']).write_text(str(child.pid))
sys.exit(options.get('exit',0) if record.get('side')==options.get('exit_side','head') else 0)
'''


def fake_install(root, scripts=None):
    scripts = scripts or root
    calls, settings, binary = root/'trivy-calls.jsonl', root/'trivy-settings.json', root/'operator-bin/trivy'
    settings.write_text('{}'); binary.parent.mkdir(exist_ok=True)
    binary.write_text('#!'+sys.executable+' -I\nSETTINGS='+repr(str(settings))+'\nCALLS='+repr(str(calls))+
                      '\nPYTHON='+repr(sys.executable)+'\n'+FAKE); binary.chmod(0o700)
    pins=scripts/'bureau-trivy-pins.json'; m=load('trivy')
    value=json.loads((SCRIPTS/'bureau-trivy-pins.json').read_text())
    value['platforms'][m.platform_key()]=dict(archive='fixture.tar.gz',archive_sha256='0'*64,
                                            binary_sha256=m.scan.file_sha(binary),binary_size=binary.stat().st_size)
    pins.write_text(json.dumps(value))
    wrapper=root/'network-wrapper.py'
    wrapper.write_text('import json,os,sys\nwith open('+repr(str(root/'network-calls.jsonl'))+
                       ',"a") as out:out.write(json.dumps(sys.argv[1:])+"\\n")\nos.execv(sys.argv[1],sys.argv[1:])\n')
    cache=root/'operator-cache'; cache.mkdir(exist_ok=True)
    make_cache(cache)
    return dict(binary=binary,pins=pins,settings=settings,calls=calls,cache=cache,
                prefix=[sys.executable,'-I','-B',str(wrapper)])


def make_cache(cache, age=0, java=False):
    now=datetime.now(timezone.utc)
    meta=dict(Version=2,UpdatedAt=(now-timedelta(hours=age)).isoformat(),
              NextUpdate=(now+timedelta(hours=6)).isoformat(),DownloadedAt=now.isoformat())
    for kind in ('db','java-db') if java else ('db',):
        folder=cache/kind;folder.mkdir(exist_ok=True)
        (folder/'metadata.json').write_text(json.dumps(meta))
        (folder/('trivy-java.db' if kind=='java-db' else 'trivy.db')).write_bytes(b'fixture database')
    refresh_record(cache, java)
    return meta


def refresh_record(cache, java=False):
    m=load('trivy'); files=m.scan.directory_files(cache,exclude=(m.CACHE_RECORD,))
    record=dict(schema_version=1,version=m.VERSION,metadata=m.metadata(cache,java),files=files,digest=m.scan.digest(files))
    (cache/m.CACHE_RECORD).write_text(json.dumps(record))
    return record


def inject_isolation(scripts, fixture, available=True):
    path=scripts/'bureau-scan.py'
    path.write_text(path.read_text()+'\n\ndef network_prefix(system=None): return '+repr(fixture['prefix'])+
                    '\n\ndef probe_network(prefix, root, env): return '+repr(available)+'\n')


def image_tar(path):
    with tarfile.open(path,'w') as out:
        for name,data in {'manifest.json':json.dumps([dict(Config='config.json',RepoTags=['fixture:latest'],Layers=[])]).encode(),
                          'config.json':b'{"architecture":"amd64","os":"linux","rootfs":{"type":"layers","diff_ids":[]},"config":{}}'}.items():
            member=tarfile.TarInfo(name);member.size=len(data);out.addfile(member,io.BytesIO(data))


class TrivyTests(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory(prefix='bureau-trivy-test-');self.addCleanup(temporary.cleanup)
        self.root=Path(temporary.name).resolve();self.repo=self.root/'repo';self.repo.mkdir()
        self.m=load('trivy');self.s=self.m.scan;self.install=fake_install(self.root)
        self.config=dict(qa=dict(trivy=dict(enabled=True,cache_dir=str(self.install['cache']))))
        self.env={**os.environ,**git_maintenance.OFF_ENV,'GIT_CONFIG_GLOBAL':os.devnull,'GIT_CONFIG_NOSYSTEM':'1',
                  'GIT_AUTHOR_NAME':'Test','GIT_COMMITTER_NAME':'Test','GIT_AUTHOR_EMAIL':'test@example.invalid',
                  'GIT_COMMITTER_EMAIL':'test@example.invalid','PYTHONDONTWRITEBYTECODE':'1'}
        self.git('init','-qb','main')
        self.write('requirements.txt','safe==1.0\n');self.write('Dockerfile','FROM scratch\nUSER 1000\n')
        self.commit();self.base=self.head
        self.write('requirements.txt','safe==1.0\nbadpkg==1.0\n');self.commit()

    def git(self,*args):
        return subprocess.check_output(['git','-C',str(self.repo),*args],env=self.env,text=True,stderr=subprocess.DEVNULL,timeout=15).strip()

    def write(self,name,data):
        path=self.repo/name;path.parent.mkdir(exist_ok=True,parents=True)
        path.write_bytes(data) if isinstance(data,bytes) else path.write_text(data)

    def commit(self):
        self.git('add','.');self.git('commit','-qm','test: scanner fixture');self.head=self.git('rev-parse','HEAD')

    def measure(self,**kw):
        return self.m.measure(self.repo,self.config,self.base,self.head,_state=self.root/'state',
                              _binary=self.install['binary'],_pins=self.install['pins'],
                              _prefix=self.install['prefix'],_probe=lambda *args:True,**kw)

    def calls(self):
        return [json.loads(row) for row in self.install['calls'].read_text().splitlines()] if self.install['calls'].exists() else []

    def mode(self,**values): self.install['settings'].write_text(json.dumps(values))

    def blocking(self): self.config['qa']['trivy']['mode']='blocking'

    def test_off_no_operation_and_applicability_precedes_prerequisites(self):
        self.config={};self.assertIsNone(self.measure());self.assertFalse((self.root/'state').exists())
        self.config=dict(qa=dict(trivy=dict(enabled=True,mode='blocking',cache_dir=str(self.install['cache']))))
        self.git('rm','requirements.txt','Dockerfile');self.commit();self.base=self.head
        self.install['binary'].unlink();shutil.rmtree(self.install['cache'])
        with patch.object(self.s,'probe_network',side_effect=AssertionError('probe must not run')), \
             patch.object(self.m,'verify_copy',side_effect=AssertionError('binary must not be checked')), \
             patch.object(self.m,'snapshots',side_effect=AssertionError('empty inventory needs no snapshot copy')):
            value=self.measure()
        self.assertEqual(value['receipt']['status'],'not_applicable');self.assertFalse(value['receipt']['blocking']);self.assertEqual(self.calls(),[])

    def test_digest_size_nonregular_and_missing_platform_never_execute(self):
        original=self.install['binary'].read_bytes()
        for kind in ('bytes','size','symlink','directory','fifo','platform'):
            with self.subTest(kind=kind):
                target=self.install['binary']
                if target.is_dir():target.rmdir()
                elif target.exists() or target.is_symlink():target.unlink()
                target.write_bytes(original);target.chmod(0o700)
                saved=self.install['pins'].read_bytes()
                if kind=='bytes':target.write_bytes(original+b'\n# tampered\n')
                elif kind=='size':
                    p=json.loads(saved);p['platforms'][self.m.platform_key()]['binary_size']+=1;self.install['pins'].write_text(json.dumps(p))
                elif kind=='symlink':
                    target.unlink();other=self.root/'other';other.write_bytes(original);target.symlink_to(other)
                elif kind=='directory':target.unlink();target.mkdir()
                elif kind=='fifo':target.unlink();os.mkfifo(target)
                else:
                    p=json.loads(saved);p['platforms']={};self.install['pins'].write_text(json.dumps(p))
                value=self.measure();self.assertEqual(value['receipt']['status'],'unavailable',value)
                self.assertEqual(self.calls(),[]);self.install['pins'].write_bytes(saved)
                if kind=='platform':self.assertIn('missing Trivy pin',value['receipt']['reason'])

    def test_exact_env_flags_private_binary_and_every_call_wrapped(self):
        (self.install['cache']/'policy/content').mkdir(parents=True)
        (self.install['cache']/'policy/content/checks.rego').write_text('hostile checks')
        refresh_record(self.install['cache'])
        extra={key:'private-key' for key in ('TRIVY_SKIP_FILES','TRIVY_CONFIG','GH_TOKEN','OPENAI_API_KEY','NVIDIA_INFERENCE_KEY','BUREAU_SECRET','DOCKER_HOST')}
        (self.install['binary'].parent/'trivy-helper').write_text('must not execute')
        with patch.dict(os.environ,extra),patch.object(self.s,'run_process',wraps=self.s.run_process) as launch:
            value=self.measure()
        self.assertEqual(value['receipt']['status'],'measured',value)
        calls=self.calls();self.assertEqual(len(calls),2)
        for call,invocation in zip(calls,launch.call_args_list):
            env=call['env'];run=Path(env['HOME']).parent
            expected=dict(PATH='/usr/bin:/bin',LANG='C.UTF-8',TZ='UTC',HOME=str(run/'home'),XDG_CONFIG_HOME=str(run/'config'),XDG_CACHE_HOME=str(run/'cache'),TMPDIR=str(run/'tmp'))
            self.assertEqual(invocation.kwargs['env'],expected)
            observed=dict(env);observed.pop('__CF_USER_TEXT_ENCODING',None);self.assertEqual(observed,expected)
            self.assertEqual(invocation.args[0][:len(self.install['prefix'])],self.install['prefix'])
            self.assertEqual(call['entries'],['trivy']);self.assertEqual((call['run_mode'],call['bin_mode']),(0o700,0o700))
            self.assertNotEqual(call['executable'],str(self.install['binary']));self.assertIsNone(call['helper']);self.assertFalse(call['adjacent'])
            self.assertEqual(call['--config'],'');self.assertEqual(call['--ignorefile'],'')
            for flag in self.m.FLAGS['scan']:self.assertIn(flag,call['argv'])
            self.assertNotIn('policy/content/checks.rego',call['cache_files']);self.assertNotIn('--download-db-only',call['argv'])
            self.assertEqual(call['argv'],self.m.scan_argv(Path(call['executable']),run,run/'db-cache',call['side'],run/call['side'])[1:])
        self.assertNotIn('private-key',json.dumps(value));self.assertNotIn('private-source',json.dumps(value['projection']))

    def test_operator_swap_after_copy_has_no_effect_private_tampering_is_unavailable(self):
        original=self.m.cache_snapshot
        def swap(*args,**kwargs):
            self.install['binary'].write_text('#!/bin/sh\nexit 97\n')
            return original(*args,**kwargs)
        with patch.object(self.m,'cache_snapshot',side_effect=swap):value=self.measure()
        self.assertEqual(value['receipt']['status'],'measured');self.assertEqual(len(self.calls()),2)
        # Fresh fixture, then mutate the private executable after the base call.
        self.install['binary'].unlink();self.install=fake_install(self.root)
        run=self.m.run_side
        def tamper(binary,*args,**kwargs):
            result=run(binary,*args,**kwargs)
            if args[5]=='base':binary.write_text('#!/bin/sh\nexit 98\n')
            return result
        with patch.object(self.m,'run_side',side_effect=tamper):value=self.measure()
        self.assertEqual(value['receipt']['status'],'unavailable',value)

    def test_cache_digest_stale_updated_at_and_record_metadata_tamper(self):
        cache=self.install['cache'];(cache/'db/trivy.db').write_bytes(b'changed')
        value=self.measure();self.assertEqual(value['receipt']['reason'],'cache digest mismatch');self.assertEqual(self.calls(),[])
        make_cache(cache,age=72)
        value=self.measure();self.assertEqual(value['receipt']['reason'],'stale database');self.assertEqual(self.calls(),[])
        self.assertIsNotNone(value['receipt']['db_digest']);self.assertIsNotNone(value['receipt']['db_updated_at'])
        make_cache(cache);record=json.loads((cache/self.m.CACHE_RECORD).read_text());record['metadata']['db']['UpdatedAt']='2099-01-01T00:00:00Z'
        (cache/self.m.CACHE_RECORD).write_text(json.dumps(record));self.assertEqual(self.measure()['receipt']['reason'],'cache metadata mismatch')

    def test_isolation_unavailable_never_executes(self):
        self.blocking()
        with patch.object(self.m.scan,'network_prefix',return_value=None),patch.object(self.m.scan,'probe_network',return_value=False):
            value=self.m.measure(self.repo,self.config,self.base,self.head,_binary=self.install['binary'],_pins=self.install['pins'])
        self.assertEqual(value['receipt']['reason'],'network isolation unavailable');self.assertTrue(value['receipt']['blocking']);self.assertEqual(self.calls(),[])

    def test_refresh_is_only_networked_operation_and_checks_private_bytes(self):
        self.mode(metadata=make_cache(self.install['cache']))
        with patch.object(self.s,'run_process',wraps=self.s.run_process) as launch:
            record=self.m.refresh(self.root/'refreshed',java=True,repo=self.repo,_binary=self.install['binary'],_pins=self.install['pins'])
        self.assertEqual(len(launch.call_args_list),2)
        for call in launch.call_args_list:
            argv=call.args[0];self.assertEqual(argv[1],'image');self.assertNotIn('--offline-scan',argv)
            self.assertIn('--skip-version-check',argv);self.assertIn('--disable-telemetry',argv)
            self.assertEqual(call.kwargs['env']['PATH'],'/usr/bin:/bin');self.assertNotEqual(argv[0],str(self.install['binary']))
        self.assertEqual(set(record['metadata']),{'db','java-db'})
        with patch.object(self.m,'refresh',side_effect=AssertionError('stage must never refresh')):self.measure()

    def test_coverage_missing_lock_missing_result_base_failure_and_exception(self):
        self.blocking();self.write('package.json',json.dumps(dict(dependencies=dict(dep='^1'))));self.commit()
        value=self.measure();self.assertEqual(value['receipt']['status'],'reduced_coverage');self.assertTrue(value['receipt']['blocking'])
        self.assertIn('manifest without lockfile',value['receipt']['reason'])
        self.write('package-lock.json',json.dumps(dict(packages={'':dict(dependencies=dict(dep='^1'))})));self.commit()
        self.mode(missing='package-lock.json');value=self.measure();self.assertIn('inventory target absent',value['receipt']['reason'])
        self.mode(bad_side='base');value=self.measure();self.assertEqual(value['receipt']['status'],'reduced_coverage');self.assertIn('base not measured',value['receipt']['reason'])
        self.mode();self.git('rm','package-lock.json');self.write('requirements.txt','safe==1.0\n')
        exception=[dict(kind='coverage',file='package.json',owner='team',reason='temporary fixture',expires='2099-01-01')]
        self.write(self.m.EXCEPTIONS,json.dumps(exception));self.commit();self.base=self.head
        self.commit_empty();value=self.measure();self.assertEqual(value['receipt']['status'],'reduced_coverage');self.assertFalse(value['receipt']['blocking'])
        self.write('requirements.txt','badpkg==1.0\n');self.commit();self.assertTrue(self.measure()['receipt']['blocking'],'coverage exception must not suppress new high findings')

    def commit_empty(self):
        self.git('commit','--allow-empty','-qm','test: head');self.head=self.git('rev-parse','HEAD')

    def test_hostile_native_files_inline_comments_and_head_exception_have_no_effect(self):
        for name in ('trivy.yaml','nested/.trivyignore','nested/.trivyignore.yaml','nested/trivy.yml'):
            self.write(name,'ignore everything\n')
        self.write('Dockerfile','# trivy:ignore:AVD-FIXTURE\nFROM scratch\n# UNSAFE resource-a\n')
        self.write(self.m.EXCEPTIONS,json.dumps([dict(id='CVE-FIXTURE',owner='team',reason='head-only',expires='2099-01-01')]))
        self.commit();value=self.measure();self.assertEqual(value['receipt']['findings_suppressed'],0)
        head=self.calls()[-1]
        for name in ('trivy.yaml','nested/.trivyignore','nested/.trivyignore.yaml','nested/trivy.yml'):self.assertNotIn(name,head['files'])
        self.assertNotIn('trivy:ignore:',head['files']['Dockerfile']);self.assertIn(dict(file='Dockerfile',line=1),value['projection']['removed_inline_ignores']['head'])
        self.assertEqual(value['receipt']['findings_new'],2)

    def test_file_level_misconfiguration_without_cause_lines_is_measured(self):
        # Shape seen from the real pinned 0.76.1: DS-0026 has only Provider and Service.
        self.write('Dockerfile','FROM scratch\n');self.commit();self.base=self.head
        self.write('Dockerfile','FROM scratch\n# NOHEALTH\n');self.commit()
        value=self.measure();self.assertEqual(value['receipt']['status'],'measured',value['projection'])
        self.assertEqual(value['receipt']['findings_new'],1,'the PASS entry must not count');existing=value['receipt']['findings_existing']
        row=value['projection']['findings'][0];self.assertEqual((row['rule_id'],row['line']),('DS-0026',0))
        self.base=self.head;self.write('Dockerfile','FROM scratch\n# NOHEALTH\nRUN true\n');self.commit()
        value=self.measure();self.assertEqual(value['receipt']['findings_new'],0);self.assertEqual(value['receipt']['findings_existing'],existing+1)

    def test_occurrence_counts_target_version_resource_and_lf_only_cause_hash(self):
        self.write('Dockerfile',b'FROM scratch\n# UNSAFE a\x0b\xe2\x80\xa8x\n');self.commit();self.base=self.head
        self.write('Dockerfile',b'FROM scratch\n# UNSAFE a\x0b\xe2\x80\xa8x\n# UNSAFE a\x0b\xe2\x80\xa8x\n');self.commit()
        value=self.measure();self.assertEqual(value['receipt']['findings_new'],1);self.assertEqual(value['receipt']['findings_existing'],2)
        report=json.loads(Path(value['receipt']['report_path']).read_text());directory=self.root/'lines';directory.mkdir()
        (directory/'Dockerfile').write_bytes(b'FROM scratch\n# UNSAFE a\x0b\xe2\x80\xa8x\n# UNSAFE a\x0b\xe2\x80\xa8x\n')
        raw={**report['head']['json'],'Results':[r for r in report['head']['json']['Results'] if r['Target']=='Dockerfile']}
        findings,_,_=self.m.report_findings(raw,directory,dict(targets=['Dockerfile']))
        self.assertEqual(findings[0]['cause_sha256'],self.s.sha(b'# UNSAFE a\x0b\xe2\x80\xa8x'))
        self.write('Dockerfile','FROM scratch\n# UNSAFE b\n');self.write('other/requirements.txt','badpkg==1.0\n');self.commit()
        value=self.measure();self.assertEqual(value['receipt']['findings_new'],2)
        self.write('requirements.txt','badpkg==2.0\n');self.commit();self.assertEqual(self.measure()['receipt']['findings_new'],3)

    def test_base_only_expired_exceptions_and_reuse_expiry(self):
        now=datetime.now(timezone.utc)
        rows=[dict(id='CVE-FIXTURE',package='badpkg',file='requirements.txt',owner='team',reason='accepted',expires=(now+timedelta(hours=1)).isoformat())]
        self.write('requirements.txt','safe==1.0\n');self.write(self.m.EXCEPTIONS,json.dumps(rows));self.commit();self.base=self.head
        self.write('requirements.txt','badpkg==1.0\n');self.commit();self.blocking()
        value=self.measure(reuse=True,pr=1);self.assertEqual(value['receipt']['findings_suppressed'],1);self.assertFalse(value['receipt']['blocking'])
        reused=self.measure(reuse=True,pr=2);self.assertEqual(value['receipt_path'],reused['receipt_path'])
        self.assertTrue((Path(value['receipt_path']).parent/'refs/pr-2').is_file())
        expired=self.measure(reuse=True,_now=now+timedelta(hours=2));self.assertNotEqual(value['receipt_path'],expired['receipt_path'])
        self.assertEqual(expired['receipt']['status'],'measured');self.assertEqual(expired['receipt']['findings_new'],1)
        self.assertTrue(expired['receipt']['blocking']);self.assertEqual(expired['receipt']['findings_suppressed'],0)

    def test_image_archive_digest_config_binding_and_advisory_only(self):
        self.write('requirements.txt','safe==1.0\n');self.commit();self.blocking()
        self.assertEqual(self.measure()['projection']['image']['reason'],'image scan: not configured')
        archive=self.root/'fixture.tar';image_tar(archive)
        import shlex
        self.config['qa']['trivy']['image_archive_command']='cp '+shlex.quote(str(archive))+' "$BUREAU_TRIVY_IMAGE_ARCHIVE"'
        value=self.measure();self.assertEqual(value['receipt']['image_digest'],self.s.file_sha(archive))
        self.assertTrue(value['receipt']['image_config_digest'].startswith('sha256:'));self.assertFalse(value['receipt']['blocking'])
        self.assertEqual(value['projection']['image']['findings_total'],1)
        call=self.calls()[-3];self.assertIn('--input',call['argv']);self.assertNotIn('DOCKER_HOST',call['env'])

    def test_inventory_ecosystems_stale_names_unpinned_and_outside_v1(self):
        directory=self.root/'inventory';directory.mkdir()
        fixtures=[('package.json',json.dumps(dict(dependencies={'@scope/a':'^1','missing':'^2'}))),
                  ('pnpm-lock.yaml',"lockfileVersion: '9.0'\nimporters:\n  .:\n    dependencies:\n      '@scope/a':\n        specifier: ^1\n        version: 1.0\n"),
                  ('pyproject.toml','[project]\ndependencies = [\n "Requests>=2",\n "missing-py==1",\n]\n[tool.poetry.group.dev.dependencies]\npytest = "*"\n'),
                  ('uv.lock','version = 1\n[[package]]\nname = "requests"\n[[package]]\nname = "pytest"\n'),
                  ('requirements-extra.txt','unpinned>=1\n'),('go.mod','module fixture\n'),('Cargo.lock','version = 4\n')]
        for name,text in fixtures:(directory/name).write_text(text)
        inv=self.m.inventory(directory);reasons=json.dumps(inv)
        self.assertIn('missing-py',reasons);self.assertIn('missing',reasons);self.assertIn('not exactly pinned',reasons)
        self.assertIn('Cargo.lock',inv['consistency_unchecked']);self.assertIn('manifest without lockfile',reasons)
        (directory/'yarn.lock').write_text('"@scope/a@^1", "@scope/a@^2":\n  version "1.0"\nmissing@^2:\n  version "2.0"\n')
        self.assertEqual(self.m.lock_names(directory/'yarn.lock'),{'@scope/a','missing'})
        (directory/'poetry.lock').write_text('[[package]]\nname = "missing-py"\n')
        self.assertEqual(self.m.lock_names(directory/'poetry.lock'),{'missing-py'})

    def test_python39_toml_extras_multiline_optional_and_poetry_groups(self):
        text='[project]\ndependencies = [\n "requests[security]>=2", # inline comment\n "second>=1",\n]\n'
        text+='[project.optional-dependencies]\ntest = ["pytest==8"]\n[tool.poetry.group."test.unit".dependencies]\ncoverage = "*"\n'
        expected={'requests','second','pytest','coverage'}
        self.assertEqual(self.m.toml_names(text),expected)
        with patch.object(self.m,'tomllib',None):
            self.assertEqual(self.m.toml_names(text),expected)
            with self.assertRaises(ValueError):self.m.toml_names('[project]\ndependencies = [broken]\n')

    def test_malformed_shapes_and_unsupported_toml_fail_closed(self):
        for text in ('["project"]\ndependencies = ["missing==1"]\n',
                     'project = { dependencies = ["missing==1"] }\n',
                     '[project]\n"dependencies" = ["missing==1"]\n',
                     '[tool.poetry.dependencies.missing]\nversion = "1"\n'):
            with patch.object(self.m,'tomllib',None):
                with self.assertRaises(ValueError):self.m.toml_names(text)
        for name,data in (('package.json','[]'),('pyproject.toml','project = 1')):
            with self.subTest(name=name):
                self.write(name,data)
                lock='package-lock.json' if name=='package.json' else 'uv.lock'
                self.write(lock,'{}' if name=='package.json' else 'version = 1\n');self.commit()
                value=self.measure();self.assertEqual(value['receipt']['status'],'reduced_coverage')
                self.git('rm',name,lock);self.commit()
        (self.install['cache']/self.m.CACHE_RECORD).write_text('[]')
        value=self.measure();self.assertEqual(value['receipt']['status'],'not_measured');self.assertEqual(value['receipt']['reason'],'invalid cache record')
        self.install['pins'].write_text('[]')
        value=self.measure();self.assertEqual(value['receipt']['status'],'unavailable')

    def test_base_failure_never_labels_head_findings_as_new(self):
        self.blocking();self.mode(bad_side='base')
        value=self.measure();self.assertEqual(value['receipt']['status'],'reduced_coverage')
        self.assertTrue(value['receipt']['blocking']);self.assertEqual(value['receipt']['findings_new'],0)
        self.assertEqual(value['projection']['findings_unclassified'],1)
        self.assertTrue(all('count_rose' not in row for row in value['projection']['findings']))

    def test_private_cache_mutation_is_not_measured_operator_mutation_cannot_affect_it(self):
        run=self.m.run_side
        def mutate(binary,pin,prefix,root,cache,env,side,directory,**kwargs):
            result=run(binary,pin,prefix,root,cache,env,side,directory,**kwargs)
            if side=='base':(self.install['cache']/'db/trivy.db').write_bytes(b'operator replacement')
            return result
        with patch.object(self.m,'run_side',side_effect=mutate):value=self.measure()
        self.assertEqual(value['receipt']['status'],'measured');self.assertEqual(value['receipt']['findings_new'],1)
        make_cache(self.install['cache'])
        def private(binary,pin,prefix,root,cache,env,side,directory,**kwargs):
            result=run(binary,pin,prefix,root,cache,env,side,directory,**kwargs)
            if side=='base':(cache/'db/trivy.db').write_bytes(b'private replacement')
            return result
        with patch.object(self.m,'run_side',side_effect=private):value=self.measure()
        self.assertEqual(value['receipt']['status'],'not_measured');self.assertEqual(value['receipt']['reason'],'head not measured')

    def test_image_and_fs_findings_share_one_bound(self):
        self.write('requirements.txt','\n'.join('badpkg=='+str(i) for i in range(30))+'\n');self.commit()
        archive=self.root/'image.tar';image_tar(archive)
        import shlex
        self.config['qa']['trivy']['image_archive_command']='cp '+shlex.quote(str(archive))+' "$BUREAU_TRIVY_IMAGE_ARCHIVE"'
        value=self.measure();self.assertEqual(len(value['projection']['findings']),20)
        self.assertEqual(value['projection']['image']['findings'],[]);self.assertEqual(value['projection']['image']['findings_omitted'],1)

    def test_missing_resource_and_trailing_ignore_keep_physical_lines(self):
        self.write('infra.tf','resource "example" "a" { # trivy:ignore:AVD-FIXTURE\n# UNSAFE resource\n}\n');self.commit()
        self.mode(no_resource=True);value=self.measure()
        head=self.calls()[-1]['files']['infra.tf']
        self.assertEqual(head.split('\n')[0],'resource "example" "a" {');self.assertNotIn('trivy:ignore:',head)
        self.assertIn(dict(file='infra.tf',line=1),value['projection']['removed_inline_ignores']['head'])
        self.assertIn(dict(file='infra.tf',rule_id='AVD-FIXTURE'),value['projection']['missing_resources']['head'])

    def test_installer_copies_single_file_trivy_assets(self):
        (self.repo/'.bureau.json').write_text(json.dumps(self.config))
        p=subprocess.run([sys.executable,'-I','-B',str(ROOT/'scripts/bureau_install.py'),'assets','--repo',str(self.repo),
                          '--scope','scripts','--apply'],env=self.env,capture_output=True,text=True,timeout=30)
        self.assertEqual(p.returncode,0,p.stdout+p.stderr)
        for name in ('bureau-trivy.py','bureau-trivy.sh','bureau-trivy-pins.json','bureau-scan.py','bureau-scan-nonet.sb'):
            self.assertEqual((self.repo/'scripts'/name).read_bytes(),(SCRIPTS/name).read_bytes())

    def test_durable_attempts_reuse_invalidation_projection_bounds(self):
        self.write('requirements.txt','\n'.join('badpkg=='+str(i) for i in range(30))+'\n');self.commit()
        value=self.measure(pr=3);self.assertEqual(len(value['projection']['findings']),20);self.assertEqual(value['projection']['findings_omitted'],10)
        receipt=Path(value['receipt_path']);before=receipt.read_bytes();again=self.measure(reuse=True,pr=4)
        self.assertEqual(again['receipt_path'],str(receipt));self.assertEqual(receipt.read_bytes(),before)
        make_cache(self.install['cache']);changed=self.measure(reuse=True);self.assertNotEqual(changed['receipt_path'],str(receipt))
        self.assertEqual(receipt.read_bytes(),before)
        self.config['qa']['trivy']['block_severity']='CRITICAL';self.assertNotEqual(self.measure(reuse=True)['receipt_path'],changed['receipt_path'])

    def test_invalid_settings_doctor_and_inside_repo_cache(self):
        doctor=load('doctor');base=dict(linear=dict(teams=[dict(id='team',key='TEAM',states=dict(build='b'))]),agents={},repo={})
        for raw in (None,[],dict(enabled='true'),dict(mode='other'),dict(block_severity='ERROR'),dict(unknown=True),
                    dict(max_db_age_hours=0),dict(max_db_age_hours=True),dict(cache_dir='relative'),dict(enabled=True),dict(image_archive_command='')):
            config={**base,'qa':dict(trivy=raw)}
            with self.subTest(raw=raw):
                with self.assertRaises(ValueError):self.m.settings(config)
                self.assertTrue(any('trivy' in error for error in doctor.validate(config)))
        self.config['qa']['trivy']['cache_dir']=str(self.repo/'cache')
        with self.assertRaisesRegex(ValueError,'outside'):self.measure()

    def test_negative_control_missing_guard_executes_tampered_binary(self):
        target=self.install['binary'];target.write_bytes(target.read_bytes()+b'\n# changed bytes\n')
        self.assertEqual(self.measure()['receipt']['status'],'unavailable');self.assertEqual(self.calls(),[])
        def unverified(source,directory,pin,name):
            directory.mkdir();result=directory/'trivy';shutil.copyfile(source,result);result.chmod(0o700);return result
        with patch.object(self.s,'verified_binary',side_effect=unverified),patch.object(self.m,'require_binary'):
            self.assertEqual(self.measure()['receipt']['status'],'measured')
        self.assertEqual(len(self.calls()),2,'negative control must expose execution without the guard')


@unittest.skipUnless(os.environ.get('BUREAU_TRIVY_REAL_TEST')=='1','optional maintainer/hosted CI binary qualification')
class RealBinaryTests(unittest.TestCase):
    def test_pinned_offline_flags_lockfile_dockerfile_and_image_input(self):
        m=load('trivy')
        with tempfile.TemporaryDirectory(prefix='bureau-trivy-real-') as temporary:
            root=Path(temporary).resolve();repo=root/'repo';repo.mkdir()
            env={**os.environ,**git_maintenance.OFF_ENV,'GIT_CONFIG_GLOBAL':os.devnull,'GIT_CONFIG_NOSYSTEM':'1',
                 'GIT_AUTHOR_NAME':'Test','GIT_COMMITTER_NAME':'Test','GIT_AUTHOR_EMAIL':'test@example.invalid','GIT_COMMITTER_EMAIL':'test@example.invalid'}
            def git(*args):return subprocess.check_output(['git','-C',str(repo),*args],env=env,text=True).strip()
            git('init','-qb','main')
            (repo/'requirements.txt').write_text('requests==2.19.1\n')
            (repo/'Dockerfile').write_text('FROM alpine:3.20\nUSER root\nRUN echo fixture\n')
            git('add','.');git('commit','-qm','test: real binary fixture');base=git('rev-parse','HEAD')
            git('commit','--allow-empty','-qm','test: final head');head=git('rev-parse','HEAD')
            config=dict(qa=dict(trivy=dict(enabled=True,cache_dir=os.environ['BUREAU_TRIVY_CACHE'])))
            value=m.measure(repo,config,base,head,_state=root/'state')
            self.assertEqual(value['receipt']['status'],'measured',value['projection'])
            report=json.loads(Path(value['receipt']['report_path']).read_text())
            for side in ('base','head'):
                self.assertEqual(report[side]['exit_code'],0)
                targets={r['Target'] for r in report[side]['json']['Results']}
                self.assertTrue({'requirements.txt','Dockerfile'} <= targets,targets)
            # Exercise image --input even though an empty-layer archive may have
            # no OS packages. Every flag uses the same invocation builder.
            private=root/'private';private.mkdir(mode=0o700)
            binary,pin=m.verify_copy(Path(os.environ['BUREAU_TRIVY_BIN']),private,m.PINS)
            cache,_,_,_=m.cache_snapshot(Path(os.environ['BUREAU_TRIVY_CACHE']),private,48)
            (private/'empty.yaml').write_text('');(private/'empty.ignore').write_text('')
            archive=private/'image.tar';image_tar(archive)
            prefix=m.scan.network_prefix();allowed=m.scan.environment(private)
            self.assertTrue(m.scan.probe_network(prefix,private,allowed))
            raw=m.run_side(binary,pin,prefix,private,cache,allowed,'image',archive,image=True)
            self.assertEqual(raw['exit_code'],0,raw);self.assertEqual(raw['json']['SchemaVersion'],2)


if __name__=='__main__':unittest.main()
