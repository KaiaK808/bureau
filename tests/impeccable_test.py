"""Pinned local verification and real stage wiring; no services or downloads."""
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.dont_write_bytecode = True
import git_maintenance

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / 'templates/scripts'
FIXTURES = ROOT / 'tests/fixtures/impeccable'
spec = importlib.util.spec_from_file_location('impeccable', SCRIPTS / 'bureau-impeccable.py')
helper = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helper)
GIT_ENV = {**{k: v for k, v in os.environ.items() if not k.startswith('IMPECCABLE_')},
           **git_maintenance.OFF_ENV, 'GIT_CONFIG_GLOBAL': os.devnull, 'GIT_CONFIG_NOSYSTEM': '1',
           'GIT_AUTHOR_NAME': 'Test', 'GIT_AUTHOR_EMAIL': 'test@example.invalid',
           'GIT_COMMITTER_NAME': 'Test', 'GIT_COMMITTER_EMAIL': 'test@example.invalid',
           'GIT_AUTHOR_DATE': '2000-01-01T00:00:00Z', 'GIT_COMMITTER_DATE': '2000-01-01T00:00:00Z'}


def installation(root, scripts):
    skill = root / 'installed skill'
    for name in helper.REQUIRED:
        path = skill / name; path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('Fixture ' + name + '\n')
    (skill / 'SKILL.md').write_text('---\nversion: 4.9.0\n---\nFixture guidance.\n')
    (skill / 'scripts/VERSION').write_text('0.1.23\n')
    system = {'Darwin': 'darwin', 'Linux': 'linux'}[helper.platform.system()]
    arch = {'arm64': 'arm64', 'aarch64': 'arm64', 'x86_64': 'x64', 'AMD64': 'x64'}[helper.platform.machine()]
    binary = skill / 'scripts/bin' / (system + '-' + arch) / 'impeccable'
    binary.parent.mkdir(parents=True)
    binary.write_bytes((FIXTURES / 'detector.py').read_bytes()); binary.chmod(0o755)
    (skill / 'scripts/engine-checksums.txt').write_text('0.1.23 impeccable-' + system + '-' + arch + ' ' + helper.sha256(binary.read_bytes()) + '\n')
    pins = {**json.loads((SCRIPTS / 'bureau-impeccable-pins.json').read_text()), 'skill_tree_sha256': helper.tree_digest(skill)}
    (scripts / 'bureau-impeccable-pins.json').write_text(json.dumps(pins))
    return skill, binary


class ImpeccableTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='.bureau-impeccable-'); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        temp_patch = patch.object(tempfile, 'tempdir', str(self.root))
        temp_patch.start(); self.addCleanup(temp_patch.stop)
        self.repo = self.root / 'repo'; self.repo.mkdir()
        self.scripts = self.root / 'scripts'; self.scripts.mkdir()
        self.skill, self.binary = installation(self.root, self.scripts)
        self.pin_patch = patch.object(helper, 'PINS', self.scripts / 'bureau-impeccable-pins.json')
        self.pin_patch.start(); self.addCleanup(self.pin_patch.stop)
        self.config = {'design': {'impeccable': {'enabled': True, 'skill_version': '4.9.0', 'engine_version': '0.1.23',
                                               'skill_path': str(self.skill)}}}
        self.calls = self.root / 'detector-calls.jsonl'
        self.env_patch = patch.dict(os.environ, {**GIT_ENV, 'TMPDIR': str(self.root), 'FAKE_DETECTOR_CALLS': str(self.calls)}, clear=True)
        self.env_patch.start(); self.addCleanup(self.env_patch.stop)
        self.git('init', '-q', '-b', 'main')
        (self.repo / 'README.md').write_text('Fixture\n')
        self.git('add', '.'); self.git('commit', '-qm', 'test: base')
        self.base = self.git('rev-parse', 'HEAD')
        shutil.copy(FIXTURES / 'page.html', self.repo / 'page.html')

    def git(self, *args):
        return subprocess.check_output(['git', '-C', str(self.repo), *args], env=GIT_ENV,
                                       stderr=subprocess.STDOUT, text=True, timeout=10).strip()

    def verify(self):
        return helper.verify(self.repo, self.config)

    def detect(self, **args):
        return helper.detect(self.repo, self.verify(), self.base, **args)

    def test_off_and_verified_paths(self):
        self.assertEqual(helper.verify(self.repo, {})['status'], 'disabled')
        self.assertEqual(helper.ux_context(helper.verify(self.repo, {})), '')
        self.assertFalse(self.calls.exists())
        verified = self.verify(); self.assertEqual(verified['status'], 'available', verified)
        context = helper.ux_context(verified)
        for name in ('shape', 'critique', 'craft-floor', 'init', 'document'):
            self.assertIn(str(self.skill / ('reference/' + name + '.md')), context)
        self.assertIn('PRODUCT.md', context); self.assertIn('DESIGN.md', context)
        self.assertIn('win over all floor defaults and bans', context)
        self.assertEqual(helper.verify(self.repo, self.config, str(self.binary))['status'], 'available')

    def test_old_configured_pins_require_upgrade_only_when_enabled(self):
        options = self.config['design']['impeccable']
        options.update(skill_version='4.8.0', engine_version='0.1.18')
        result = self.verify(); self.assertEqual(result['status'], 'unavailable', result)
        self.assertEqual(result['reason'], 'unsupported pinned skill_version')
        self.assertEqual(self.detect()['status'], 'not measured'); self.assertFalse(self.calls.exists())
        options['enabled'] = False
        self.assertEqual(self.verify()['status'], 'disabled')
        options['enabled'] = True
        del options['skill_version'], options['engine_version']
        self.assertEqual(self.verify()['status'], 'available')

    def test_bad_engine_skill_missing_files_and_versions_are_unavailable(self):
        for target, changed, expected in [(self.binary, b'wrong', 'engine digest'),
                (self.skill / 'reference/shape.md', b'wrong', 'skill-tree digest'),
                (self.skill / 'reference/shape.md', None, 'missing skill file'), (self.binary, None, 'binary is missing')]:
            with self.subTest(expected=expected):
                original = target.read_bytes(); mode = target.stat().st_mode
                if changed is None: target.unlink()
                else: target.write_bytes(changed)
                result = self.verify(); self.assertEqual(result['status'], 'unavailable', result)
                self.assertIn(expected, result['reason'])
                report = self.detect(); self.assertEqual(report['status'], 'not measured')
                self.assertEqual(report['availability'], 'unavailable')
                target.write_bytes(original); target.chmod(mode)
        self.config['design']['impeccable']['engine_version'] = '999'
        self.assertIn('unsupported pinned', self.verify()['reason'])
        self.assertFalse(self.calls.exists(), 'verification must not execute a launcher or download')

    def test_tree_manifest_and_symlinks(self):
        before = helper.tree_digest(self.skill)
        self.binary.write_text('different binary'); self.assertEqual(helper.tree_digest(self.skill), before)
        (self.skill / 'extra.md').write_text('Unexpected file'); self.assertNotEqual(helper.tree_digest(self.skill), before)
        (self.skill / 'extra.md').unlink()
        (self.skill / 'reference/link.md').symlink_to(self.skill / 'reference/shape.md')
        self.assertIn('symlink inside skill', self.verify()['reason'])

    def test_two_tiers_telemetry_argv_and_negative_control(self):
        off = helper.detect(self.repo, helper.verify(self.repo, {}), self.base)
        self.assertEqual(off['status'], 'not measured'); self.assertFalse(self.calls.exists())
        mechanical = self.detect(tier='mechanical')
        self.assertEqual([f['antipattern'] for f in mechanical['findings']], ['low-contrast'])
        full = self.detect(); self.assertEqual(full['status'], 'findings')
        self.assertEqual({f['antipattern'] for f in full['findings']}, {'low-contrast', 'ai-color-palette'})
        for row in map(json.loads, self.calls.read_text().splitlines()):
            self.assertEqual(row['argv'], ['detect', '--json', '--no-config', './page.html'])
            self.assertEqual(row['telemetry'], ['1', '1']); self.assertEqual(row['binary'], str(self.binary))

    def test_engine_resolves_symlinked_root_and_finding_paths(self):
        linked = self.root / 'linked repo'; linked.symlink_to(self.repo, target_is_directory=True)
        for env in ({}, {'FAKE_DETECTOR_FILE': str(linked / 'page.html')}, {'FAKE_DETECTOR_FILE': './page.html'}):
            with self.subTest(env=env), patch.dict(os.environ, env):
                rows = helper.run_engine(self.verify(), linked, ['page.html'], 5)
                self.assertEqual({row['file'] for row in rows}, {'page.html'})
                self.assertEqual({row['antipattern'] for row in rows}, {'low-contrast', 'ai-color-palette'})

    def test_outside_findings_are_unattributable_and_not_measured(self):
        outside = self.root / 'outside.html'; shutil.copy(FIXTURES / 'page.html', outside)
        linked = self.repo / 'outside-link'; linked.symlink_to(outside)
        cache = self.root / 'outside-cache.json'
        for name in (str(outside), '../outside.html', str(linked)):
            for tier in ('mechanical', 'all'):
                with self.subTest(file=name, tier=tier), patch.dict(os.environ, {'FAKE_DETECTOR_FILE': name}):
                    report = self.detect(tier=tier, cache=cache)
                    self.assertEqual(report['status'], 'not measured', report)
                    self.assertIn('outside repository', report['reason'])
                    self.assertEqual({row['file'] for row in report['findings']}, {outside.as_posix()})
                    self.assertTrue(all(row['attributable'] is False for row in report['findings']))
                    self.assertFalse(cache.exists())

    def test_not_measured_exit_timeout_malformed_and_shape(self):
        for mode, reason in [('exit1', 'exit 1'), ('timeout', 'timed out'), ('malformed', 'Expecting'),
                             ('bad-shape', 'malformed detector finding')]:
            with self.subTest(mode=mode), patch.dict(os.environ, {'FAKE_DETECTOR_MODE': mode, 'FAKE_DETECTOR_PID': str(self.root / 'pid')}):
                result = self.detect(timeout=0.15 if mode == 'timeout' else 5)
                self.assertEqual(result['status'], 'not measured', result); self.assertIn(reason, result['reason'])
                gate = subprocess.check_output(['bash', '-c', 'source "$GATE_HELPER"; bureau_impeccable() { printf "%s" "$REPORT"; }; '
                    'bureau_impeccable_check all; printf "blocked=%s" "$IMPECCABLE_GATE_BLOCKED"'], text=True,
                    env={**os.environ, 'GATE_HELPER': str(SCRIPTS / 'bureau-impeccable.sh'),
                         'IMPLEMENT_GATE_TMP': str(self.root), 'REPORT': json.dumps(result)}, timeout=5)
                self.assertIn('blocked=1', gate)
                if mode == 'timeout':
                    with self.assertRaises(ProcessLookupError): os.kill(int((self.root / 'pid').read_text()), 0)

    def test_cancellation_during_detection_joins_engine_and_removes_snapshot(self):
        (self.repo / '.bureau.json').write_text(json.dumps(self.config))
        script = self.scripts / 'bureau-impeccable.py'; shutil.copy(SCRIPTS / script.name, script)
        pid = self.root / 'engine.pid'
        process = subprocess.Popen([sys.executable, '-B', str(script), 'detect', '--repo', str(self.repo),
                                    '--base', self.base, '--timeout', '30'], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   env={**os.environ, 'FAKE_DETECTOR_MODE': 'timeout', 'FAKE_DETECTOR_PID': str(pid)})
        engine = None
        try:
            deadline = time.monotonic() + 5
            while process.poll() is None and time.monotonic() < deadline:
                if pid.exists() and pid.read_text().isdigit():
                    engine = int(pid.read_text())
                    break
                time.sleep(.01)
            self.assertIsNotNone(engine, 'engine did not start')
            snapshot = Path(json.loads(self.calls.read_text().splitlines()[-1])['directory'])
            self.assertTrue((snapshot / 'page.html').is_file())
            process.terminate()
            stdout, stderr = process.communicate(timeout=5)
            self.assertEqual(process.returncode, 130, stdout + stderr)
            with self.assertRaises(ProcessLookupError): os.kill(engine, 0)
            self.assertFalse(snapshot.exists(), 'cancelled scan left its snapshot behind')
        finally:
            if process.poll() is None: process.terminate()
            try: process.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill(); process.communicate(timeout=5)
            if engine is not None:
                try: os.killpg(engine, signal.SIGKILL)
                except ProcessLookupError: pass

    def test_cancellation_at_launch_boundary_joins_engine_and_removes_snapshot(self):
        popen = subprocess.Popen
        handlers = {signal.SIGTERM: helper.cancel, signal.SIGINT: signal.default_int_handler}
        previous = {signum: signal.signal(signum, handler) for signum, handler in handlers.items()}

        try:
            for signum in handlers:
                with self.subTest(signal=signum):
                    launched = []

                    def cancel_after_spawn(args, **kwargs):
                        process = popen(args, **kwargs)
                        if args[0] == str(self.binary):
                            launched.append((process, Path(kwargs['cwd'])))
                            os.kill(os.getpid(), signum)
                        return process

                    try:
                        with patch.object(subprocess, 'Popen', side_effect=cancel_after_spawn), \
                                patch.dict(os.environ, {'FAKE_DETECTOR_MODE': 'timeout'}):
                            with self.assertRaises(SystemExit) as cancelled:
                                self.detect()
                        self.assertEqual(cancelled.exception.code, 130)
                        for sig, handler in handlers.items():
                            self.assertIs(signal.getsignal(sig), handler)
                        self.assertEqual(len(launched), 1)
                        process, snapshot = launched[0]
                        self.assertEqual(process.returncode, -signal.SIGKILL)
                        with self.assertRaises(ChildProcessError): os.waitpid(process.pid, os.WNOHANG)
                        self.assertFalse(snapshot.exists(), 'cancelled launch left its snapshot behind')
                    finally:
                        for process, _ in launched:
                            if process.poll() is None:
                                try: os.killpg(process.pid, signal.SIGKILL)
                                except ProcessLookupError: pass
                            process.communicate(timeout=5)
        finally:
            for signum, handler in previous.items():
                signal.signal(signum, handler)

    def test_ignores(self):
        path = self.repo / helper.IGNORE_FILE; path.parent.mkdir()
        narrow = dict(file='page.html', antipattern='ai-color-palette', reason='Approved visual direction in DESIGN.md')
        path.write_text(json.dumps([narrow])); report = self.detect()
        self.assertEqual([f['antipattern'] for f in report['findings']], ['low-contrast'])
        self.assertEqual(report['ignored'][0]['reason'], narrow['reason'])
        for row in [{**narrow, 'file': '**/*.html'}, {**narrow, 'antipattern': '*'}, {**narrow, 'reason': ''},
                    {**narrow, 'file': '../page.html'}, {'antipattern': 'ai-color-palette', 'reason': 'all files'},
                    {**narrow, 'reason': None}]:
            with self.subTest(row=row):
                path.write_text(json.dumps([row])); self.assertEqual(self.detect()['status'], 'not measured')

    def test_cache_invalidates_on_source_and_ignore_changes(self):
        cache = self.root / 'cached.json'
        self.detect(cache=cache); self.detect(cache=cache)
        self.assertEqual(len(self.calls.read_text().splitlines()), 1)
        (self.repo / 'page.html').write_text('<p>Plain interface</p>')
        self.assertEqual(self.detect(cache=cache)['status'], 'clean')
        path = self.repo / helper.IGNORE_FILE; path.parent.mkdir(); path.write_text('[]')
        self.assertEqual(self.detect(cache=cache)['status'], 'clean')
        self.assertEqual(len(self.calls.read_text().splitlines()), 2)
        path.write_text(json.dumps([dict(file='page.html', antipattern='ai-color-palette', reason='Approved')]))
        self.detect(cache=cache); self.assertEqual(len(self.calls.read_text().splitlines()), 3)

    def test_pinned_review_snapshot_and_no_ui(self):
        self.git('add', '.'); self.git('commit', '-qm', 'test: planted page'); head = self.git('rev-parse', 'HEAD')
        (self.repo / 'page.html').write_text('<p>Different validation checkout</p>')
        self.assertEqual(self.detect(head=head)['status'], 'findings'); self.assertEqual(self.detect()['status'], 'clean')
        self.assertEqual(helper.detect(self.repo, self.verify(), head, head=head)['status'], 'not applicable')

    def test_installer_reports_unavailable_without_downloading(self):
        config = json.loads(json.dumps(self.config)); config['design']['impeccable']['skill_path'] = 'absent'
        (self.repo / '.bureau.json').write_text(json.dumps(config))
        proc = subprocess.run([sys.executable, '-B', '-I', str(ROOT / 'scripts/bureau_install.py'),
                               'assets', '--repo', str(self.repo)], capture_output=True, text=True, env=GIT_ENV, timeout=20)
        self.assertEqual(proc.returncode, 0, proc.stderr); self.assertIn('Impeccable unavailable', proc.stderr)
        self.assertFalse((self.repo / '.claude').exists()); self.assertFalse(self.calls.exists())
        source = self.root / 'source'; scripts = source / 'templates/scripts'; scripts.mkdir(parents=True)
        shutil.copy(SCRIPTS / 'bureau-impeccable.py', scripts)
        shutil.copy(self.scripts / 'bureau-impeccable-pins.json', scripts)
        spec = importlib.util.spec_from_file_location('fixture_installer', ROOT / 'scripts/bureau_install.py')
        installer = importlib.util.module_from_spec(spec); spec.loader.exec_module(installer)
        (self.repo / '.bureau.json').write_text(json.dumps(self.config))
        with patch.object(installer, 'ROOT', source), patch.dict(os.environ, {'IMPECCABLE_BIN': str(self.binary)}):
            self.assertEqual(installer.impeccable_status(self.repo)['status'], 'available')
        self.assertFalse(self.calls.exists())

    def test_late_supplement_cannot_erase_independent_findings_or_holds(self):
        finding = dict(file='page.html', line=1, **{'class': 'CRITICAL'}, msg='Original defect')
        original = dict(specialist='security', counts=dict(critical=3, bug=0, minor=0, skip=0), findings=[finding],
                        holds=['Unresolved decision'], summary='Independent judgement')
        late = dict(specialist='security', counts=dict(critical=0, bug=0, minor=0, skip=0), findings=[], holds=[], summary='No additions')
        for suffix, value in [('independent', original), ('late', late)]:
            (self.root / ('security.' + suffix + '.txt')).write_text(json.dumps(value))
        result = helper.supplement(self.root, 'security', None)
        self.assertEqual(result['findings'], [finding]); self.assertEqual(result['counts']['critical'], 3)
        self.assertEqual(result['holds'], ['Unresolved decision'])


STAGE_RUNNER = r'''
set -euo pipefail
source tests/lib/harness.sh
sandbox_init TEST-1 001-task
export BUREAU_CONFIG="$SANDBOX/.bureau.json"
python3 -B "$STAGE_PREPARE" "$SANDBOX"
export FAKE_CLAUDE_BIN="$STAGE_PROVIDER" BUREAU_STUB_LABELS='["needs-ux"]'
export BUREAU_STUB_ISSUE_STATE="$STAGE_STATE" BUREAU_IMPL_MAX_ITER=3
export FAKE_DETECTOR_CALLS="$SANDBOX/detector-calls.jsonl"
export GH_STUB_EXISTING_PR=99 BUREAU_NO_MERGE=1 BUREAU_STOP_REQUESTED=1
run_pipeline "$STAGE_SCRIPT" TEST-1
python3 -B "$STAGE_COLLECT" "$SANDBOX"
'''

STAGE_PROVIDER = r'''import json, os
from pathlib import Path
import subprocess, sys, tempfile
root = Path(os.environ['SANDBOX'])
prompt = sys.argv[-1]
phase = os.environ.get('BUREAU_PROVIDER_PHASE', os.environ['STAGE_STATE'])
role = phase.removesuffix('_late')
late = 'separate late evidence step' in prompt
capture = root / 'captures'; capture.mkdir(exist_ok=True)
with tempfile.NamedTemporaryFile(mode='w', dir=capture, suffix='.json', delete=False) as out:
    json.dump(dict(phase=phase, late=late, prompt=prompt), out)
if role in ('correctness', 'security', 'performance'):
    value = dict(specialist=role, counts=dict(critical=0, bug=0, minor=0, skip=0), findings=[], holds=[], summary='Independent judgement')
    if late:
        assert len([p for p in capture.glob('*.json') if not json.loads(p.read_text())['late']]) == 3
        value['summary'] = 'Late evidence assessed'
elif phase == 'Design':
    value = dict(design_status='COMPLETE', summary='Brief ready')
else:
    n = len(list(capture.glob('*.json')))
    if os.environ.get('STAGE_FIX') == '1' and n > 1: (root / 'page.html').write_text('<p>Plain interface</p>\n')
    (root / 'progress.txt').write_text('Pass ' + str(n))
    subprocess.run(['git', 'add', 'page.html', 'progress.txt'], check=True)
    subprocess.run(['git', 'commit', '-qm', 'test: implementation pass'], check=True)
    value = dict(status='COMPLETE', tasks_done=1, tasks_skipped=0, tasks_needs_human=0, fixed_review_items=[],
                 notes=dict(needs_human=[], skipped=[], deviations=[]), prose_notes='')
print(json.dumps(value))
'''


class StageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='.bureau-impeccable-stages-'); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.scratch = self.root / 'scratch'; self.scratch.mkdir()
        self.provider = self.root / 'provider'
        self.provider.write_text('#!' + sys.executable + '\n' + STAGE_PROVIDER); self.provider.chmod(0o755)
        self.prepare = self.root / 'prepare.py'; self.collect = self.root / 'collect.py'
        self.collect.write_text("""import json, os, pathlib, re, shutil, sys
preserved=[pathlib.Path(name).resolve() for name in re.findall(r'Specialist outputs preserved at ([^\\n]+)',os.environ['LAST_STDERR'])]
try:
    p=pathlib.Path(sys.argv[1])
    captures=[json.loads(f.read_text()) for f in sorted((p/'captures').glob('*.json'))]
    calls=[json.loads(s) for s in (p/'detector-calls.jsonl').read_text().splitlines()] if (p/'detector-calls.jsonl').exists() else []
    artifacts={}
    for artifact in preserved:
        artifacts={f.name:f.read_text() for f in artifact.iterdir() if f.name in ('correctness.txt','security.txt','performance.txt','merge-metadata.json','impeccable.json','correctness.independent.txt')}
    pathlib.Path(os.environ['STAGE_RESULT']).write_text(json.dumps(dict(captures=captures, calls=calls,
        stdout=os.environ['LAST_STDOUT'], stderr=os.environ['LAST_STDERR'], code=os.environ['LAST_RC'],
        helpers=(p/'calls.log').read_text(), sandbox=str(p), artifacts=artifacts)))
finally:
    for artifact in preserved: shutil.rmtree(artifact)
""")

    def run_stage(self, stage, enabled, fix=False, ui=True, unavailable=False, deferred=False, mutate_prompt=False):
        state, script = {'ux': ('Design', 'ux-pipeline.sh'), 'implement': ('Build', 'implement-pipeline.sh'),
                         'review': ('Build Review', 'code-review-pipeline.sh')}[stage]
        self.prepare.write_text('''import importlib.util, json, os, pathlib, shutil, subprocess, sys
sys.dont_write_bytecode=True
repo=pathlib.Path(sys.argv[1]); root=pathlib.Path(os.environ['TEST_SOURCE'])
spec=importlib.util.spec_from_file_location('fixture', root/'tests/impeccable_test.py')
f=importlib.util.module_from_spec(spec); spec.loader.exec_module(f)
skill,binary=f.installation(repo, repo/'scripts')
config=dict(repo=dict(test_command='true'), design=dict(impeccable=dict(enabled=os.environ['STAGE_ENABLED']=='1', skill_path=str(skill))))
if os.environ.get('STAGE_UNAVAILABLE')=='1': config['design']['impeccable']['skill_path']='absent'
(repo/'.bureau.json').write_text(json.dumps(config))
(repo/'.gitignore').write_text('.bureau.json\\n.env\\n.fake-origin.git/\\nscripts/\\nlogs/\\ncaptures/\\ninstalled skill/\\n*-calls.jsonl\\nstderr.log\\ncalls.log\\n')
shutil.copy(root/'tests/fixtures/impeccable/page.html',repo/'page.html')
if os.environ.get('STAGE_DEFERRED')=='1': (repo/'page.html').write_text((repo/'page.html').read_text().replace('#aaa','#111'))
if os.environ.get('STAGE_MUTATE_PROMPT')=='1':
    path=repo/'scripts'/os.environ['STAGE_SCRIPT']
    source=path.read_text()
    needle={'ux-pipeline.sh':'You are the UX/UI design agent', 'implement-pipeline.sh':'Implement tasks from',
            'code-review-pipeline.sh':'You are a CORRECTNESS specialist reviewing'}[path.name]
    assert source.count(needle)==1
    path.write_text(source.replace(needle, needle+' (unexpected prompt change)', 1))
subprocess.run(['git','-C',str(repo),'add','.gitignore','page.html','specs'],check=True)
subprocess.run(['git','-C',str(repo),'commit','-qm','test: UI fixture'],check=True)
subprocess.run(['git','-C',str(repo),'push','-q','origin','001-task'],check=True)
''')
        result = self.root / 'result.json'
        env = {**GIT_ENV, 'TMPDIR': str(self.scratch), 'TEST_SOURCE': str(ROOT), 'PYTHONPATH': str(ROOT / 'tests'),
               'STAGE_PREPARE': str(self.prepare), 'STAGE_COLLECT': str(self.collect), 'STAGE_PROVIDER': str(self.provider),
               'STAGE_STATE': state, 'STAGE_SCRIPT': script, 'STAGE_ENABLED': '1' if enabled else '0',
               'STAGE_FIX': '1' if fix else '0', 'STAGE_UNAVAILABLE': '1' if unavailable else '0',
               'STAGE_DEFERRED': '1' if deferred else '0',
               'STAGE_MUTATE_PROMPT': '1' if mutate_prompt else '0', 'STAGE_RESULT': str(result)}
        runner = STAGE_RUNNER if ui else STAGE_RUNNER.replace("BUREAU_STUB_LABELS='[\"needs-ux\"]'", "BUREAU_STUB_LABELS='[]'")
        process = subprocess.Popen(['bash', '-c', runner], cwd=ROOT, env=env, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, text=True, start_new_session=True)
        try:
            stdout, stderr = process.communicate(timeout=60)
        except BaseException:
            try: os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError: pass
            process.communicate()
            raise
        proc = subprocess.CompletedProcess(process.args, process.returncode, stdout, stderr)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        value = json.loads(result.read_text())
        self.assertIn(value['code'], ('0', '20'), value['stdout'] + value['stderr'] + repr(value['artifacts']))
        remaining = list(self.scratch.iterdir())
        self.assertEqual([p for p in remaining if p.name != 'xcrun_db'], [], proc.stderr)
        for path in remaining: path.unlink()
        return value

    def assert_disabled_prompts(self, stage, value):
        actual = sorted((c['phase'], re.sub(r'\b[a-f0-9]{40}\b', '<commit>',
                        c['prompt'].replace(value['sandbox'], '<repo>').replace('TEST-1', '<issue>')))
                        for c in value['captures'])
        golden = json.loads((FIXTURES / 'disabled-prompts.json').read_text())[stage]
        self.assertEqual(actual, [(c['phase'], c['prompt']) for c in golden])

    def test_off_prompts_are_byte_identical_to_golden(self):
        for stage in ('ux', 'implement', 'review'):
            with self.subTest(stage=stage):
                result = self.run_stage(stage, False)
                self.assert_disabled_prompts(stage, result); self.assertEqual(result['calls'], [])

    def test_disabled_prompt_mutations_fail_golden_comparison(self):
        for stage in ('ux', 'implement', 'review'):
            with self.subTest(stage=stage):
                result = self.run_stage(stage, False, mutate_prompt=True)
                with self.assertRaises(AssertionError): self.assert_disabled_prompts(stage, result)

    def test_ux_references_and_non_ui_is_unchanged(self):
        result = self.run_stage('ux', True); prompt = result['captures'][0]['prompt']
        for name in ('shape', 'critique', 'craft-floor', 'init', 'document'):
            self.assertIn('/reference/' + name + '.md', prompt)
        self.assertIn('PRODUCT.md', prompt); self.assertIn('DESIGN.md', prompt)
        self.assertEqual(self.run_stage('implement', True, ui=False)['calls'], [])

    def test_implement_findings_feed_next_pass_and_are_checked_at_end(self):
        result = self.run_stage('implement', True, fix=True); prompts = result['captures']
        self.assertEqual(len(prompts), 2, result['stdout'] + result['stderr'])
        self.assertTrue(any('low-contrast' in c['prompt'] for c in prompts))
        self.assertTrue(all('Bureau owns Impeccable engine execution' in c['prompt'] for c in prompts))
        self.assertTrue(any('.impeccable/bureau-ignore.json' in c['prompt'] for c in prompts))
        self.assertIn('Status: Build Review (COMPLETE)', result['stdout'])
        self.assertEqual(len(result['calls']), 3)

    def test_unresolved_deferred_findings_hold_and_return_as_feedback(self):
        result = self.run_stage('implement', True)
        self.assertIn('FIXES_NEEDED', result['helpers']); self.assertIn('ai-color-palette', result['helpers'])
        self.assertIn('NEEDS_HUMAN', result['stdout'])

    def test_deferred_findings_feed_next_pass(self):
        result = self.run_stage('implement', True, fix=True, deferred=True)
        self.assertEqual(len(result['captures']), 2)
        self.assertTrue(any('ai-color-palette' in c['prompt'] for c in result['captures']))
        self.assertIn('Status: Build Review (COMPLETE)', result['stdout'])

    def test_missing_optional_tool_does_not_fail_stage(self):
        result = self.run_stage('implement', True, unavailable=True)
        self.assertEqual(result['calls'], []); self.assertIn('not measured', result['stdout'])
        self.assertIn('COMPLETE', result['stdout'])

    def test_review_receives_machine_evidence_only_after_judgement(self):
        result = self.run_stage('review', True)
        independent = [c for c in result['captures'] if not c['late']]; late = [c for c in result['captures'] if c['late']]
        self.assertEqual(len(independent), 3); self.assertEqual(len(late), 3)
        self.assertEqual({c['phase'] for c in independent}, {'correctness', 'security', 'performance'})
        self.assertEqual({c['phase'] for c in late}, {'correctness_late', 'security_late', 'performance_late'})
        self.assertTrue(all('ai-color-palette' not in c['prompt'] for c in independent))
        self.assertTrue(all('Do not invoke Impeccable commands' in c['prompt'] for c in independent))
        self.assertTrue(all('ai-color-palette' in c['prompt'] and 'Independent judgement' in c['prompt'] for c in late))
        self.assertIn('Impeccable machine evidence (late)', result['stdout']); self.assertEqual(len(result['calls']), 1)
        self.assertEqual(json.loads(result['artifacts']['impeccable.json'])['status'], 'findings')
        self.assertEqual(json.loads(result['artifacts']['correctness.independent.txt'])['summary'], 'Independent judgement')


class RealEngineTests(unittest.TestCase):
    @unittest.skipUnless(os.environ.get('IMPECCABLE_BIN'), 'IMPECCABLE_BIN is not set')
    def test_operator_supplied_engine(self):
        binary = Path(os.environ['IMPECCABLE_BIN']).resolve()
        with tempfile.TemporaryDirectory(prefix='.bureau-impeccable-real-') as tmp:
            shutil.copy(FIXTURES / 'page.html', Path(tmp) / 'page.html')
            rows = helper.run_engine(dict(binary=str(binary), skill=str(binary.parent)), Path(tmp), ['page.html'], 10)
            self.assertIn('ai-color-palette', {r['antipattern'] for r in rows})


if __name__ == '__main__':
    unittest.main()
