"""Dormant wrapper tests. All prefixes and commands are local fixture programs.

Drivers split policy, execution, process and mutation coverage. Named cases can
also be run individually on a shared host; no driver is needed for that mode.
"""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / 'templates/scripts'


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    value = importlib.util.module_from_spec(spec)
    sys.modules[name] = value
    spec.loader.exec_module(value)
    return value


w = load(SCRIPTS / 'bureau-test-wrapper.py', 'test_wrapper_runtime')
d = load(SCRIPTS / 'bureau-doctor.py', 'test_wrapper_doctor')
FAKE = r'''
import json, os, signal, subprocess, sys, time
from pathlib import Path
mode, event = sys.argv[1:3]
argv = sys.argv[3:]
directory = Path(argv[argv.index('--envelope') + 1])
def log(name):
    with open(event, 'a') as out:
        out.write(json.dumps(dict(event=name, at=time.monotonic(), pid=os.getpid(),
                                 pgid=os.getpgrp(), argv=argv, env=dict(os.environ), cwd=os.getcwd())) + '\n')
log('acquire')
print('prefix diagnostic stdout', flush=True)
print('prefix diagnostic stderr', file=sys.stderr, flush=True)
if mode.startswith('refuse'):
    sys.exit(int(mode[6:]))
if mode in ('queued', 'running', 'hang'):
    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP): signal.signal(sig, signal.SIG_IGN)
if mode in ('queued', 'hang'):
    log('queued')
    time.sleep(60)
if mode in ('malformed', 'stale', 'missing-start'):
    r = json.loads((directory/'request.json').read_text())
    r.update(pgid=os.getpgrp(), pid=os.getpid(), started_at=time.monotonic(), wrapper_status=None)
    if mode == 'stale': r['execution_id'] = 'old-execution'
    if mode == 'malformed': (directory/'start.json').write_text('{')
    elif mode == 'stale': (directory/'start.json').write_text(json.dumps(r))
    else:
        r.update(ended_at=time.monotonic(), child_status=0)
        (directory/'end.json').write_text(json.dumps(r))
    for p in directory.glob('*.json'): p.chmod(0o600)
    sys.exit(0)
if mode == 'serialize':
    import fcntl
    lock = open(event + '.lock', 'a')
    fcntl.flock(lock, fcntl.LOCK_EX)
if mode == 'delay': time.sleep(.08)
log('start-envelope')
rc = subprocess.call(argv, close_fds=True)
log('release')
if mode == 'serialize':
    fcntl.flock(lock, fcntl.LOCK_UN)
if mode == 'missing-end': (directory/'end.json').unlink()
if mode == 'release-hang': time.sleep(60)
sys.exit(0 if mode == 'swallow' else rc)
'''


class ProbeClock:
    def __init__(self): self.now = 0.0
    def monotonic(self): return self.now


class WrapperTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='bureau-wrapper-')
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.repo = self.base / 'checkout'
        self.repo.mkdir()
        self.control = self.base / 'coordination'
        self.wrapper = self.base / 'fake wrapper.py'
        self.wrapper.write_text(FAKE)
        self.events = self.base / 'events.jsonl'
        self.config = self.base / 'operator.json'
        # Bounded discovery; no host Git config, maintenance, credentials or hooks.
        env = {k: v for k, v in os.environ.items() if k in ('PATH', 'HOME', 'TMPDIR', 'SYSTEMROOT')}
        env.update(PYTHONDONTWRITEBYTECODE='1', GIT_CEILING_DIRECTORIES=str(self.base),
                   GIT_DISCOVERY_ACROSS_FILESYSTEM='0', LC_ALL='C', GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL=os.devnull,
                   GIT_AUTHOR_NAME='Fixture', GIT_COMMITTER_NAME='Fixture',
                   GIT_AUTHOR_EMAIL='fixture@example.invalid', GIT_COMMITTER_EMAIL='fixture@example.invalid',
                   GIT_CONFIG_COUNT='2', GIT_CONFIG_KEY_0='maintenance.auto', GIT_CONFIG_VALUE_0='false',
                   GIT_CONFIG_KEY_1='gc.auto', GIT_CONFIG_VALUE_1='0')
        clean = patch.dict(os.environ, env, clear=True)
        clean.start(); self.addCleanup(clean.stop)
        # The temporary root itself must not discover a surrounding repository.
        check = subprocess.run(['git', '-C', str(self.base), 'rev-parse', '--show-toplevel'], capture_output=True)
        self.assertNotEqual(check.returncode, 0, 'fixture root is inside a repository')
        self.counter = 0

    def policy(self, mode='run', **settings):
        config = {'repo': {'test_wrapper': [sys.executable, str(self.wrapper), mode, str(self.events)], **settings}}
        self.config.write_text(json.dumps(config))
        return w.resolve_policy(self.repo, primary_config=self.config, operator_config=self.config)

    def execute(self, command=None, mode='run', runtime=w, policy=None, **kwargs):
        self.counter += 1
        out, err = self.base / f'out-{self.counter}', self.base / f'err-{self.counter}'
        result = runtime.execute(policy or self.policy(mode), command or [sys.executable, '-I', '-B', '-c', 'print("child")'],
                                 repo=self.repo, stdout=out, stderr=err, run_id='run', attempt_id='attempt',
                                 coordination_root=self.control, grace_seconds=.1, **kwargs)
        return result, out, err

    def assert_result(self, result, code, kind):
        if result.kind == 'cleanup-uncertain':
            self.fail('process cleanup could not be verified under this sandbox; maintainer must run this case')
        self.assertEqual((result.code, result.kind), (code, kind), result)
        self.assertFalse(list(self.control.glob('process-holds/*.json')))

    def git(self, *argv):
        return subprocess.check_output(['git', '-C', str(self.repo), *argv], stderr=subprocess.DEVNULL).decode().strip()

    def pin(self, config, symlink=False):
        self.git('init', '-q')
        self.git('config', 'maintenance.auto', 'false'); self.git('config', 'gc.auto', '0')
        target = self.repo / '.bureau.json'
        if symlink: target.symlink_to(self.config)
        else: target.write_text(json.dumps(config))
        self.git('add', '.bureau.json'); self.git('commit', '-qm', 'fixture base')
        return self.git('rev-parse', 'HEAD')

    def test_policy_validation(self):
        for value in (None, 'echo test', [1], ['relative'], [''], ['/bin/echo', '\n'], ['/bin/echo', '\x7f']):
            with self.subTest(value=value), self.assertRaises(ValueError):
                w.validate_policy({'repo': {'test_wrapper': value}})
        for value in (True, None, 0, -1, 86401, float('inf'), float('nan'), '5'):
            with self.subTest(timeout=value), self.assertRaises(ValueError):
                w.validate_policy({'repo': {'test_wrapper_start_timeout_seconds': value}})
        self.assertEqual(w.validate_policy({}), ((), 21600))
        self.assertEqual(w.validate_policy({'repo': {'test_wrapper': []}}), ((), 21600))
        self.assertEqual(w.validate_policy({'repo': {'test_wrapper': ['/bin/echo', '', 'a b', '$HOME;*']}})[0][1:], ('', 'a b', '$HOME;*'))
        self.assertEqual(w.validate_policy({}), ((), 21600.0))
        self.assertEqual(w.validate_policy({'repo': {'test_wrapper': []}}), ((), 21600.0))
        for value in (None, True, False, 0, -1, 86400.1, float('nan'), float('inf'), '21600'):
            with self.subTest(timeout=value), self.assertRaises(ValueError):
                w.validate_policy({'repo': {'test_wrapper_start_timeout_seconds': value}})
        self.assertEqual(w.validate_policy({'repo': {'test_wrapper_start_timeout_seconds': 86400}})[1],86400)
        result, _, _ = self.execute(policy='invalid')
        self.assert_result(result, 24, 'launch-error')

    def test_trusted_sources(self):
        good = self.policy()
        original = json.loads(self.config.read_text())
        base = self.pin(original)
        (self.repo / '.bureau.json').write_text('{"repo":{"test_wrapper":[]}}')
        os.environ['BUREAU_CONFIG'] = str(self.repo / '.bureau.json')
        policy = w.resolve_policy(self.repo, primary_config=self.repo / '.bureau.json', trusted_base=base)
        self.assertEqual(policy.argv, good.argv)
        self.assertTrue(policy.source.startswith('pinned-base:' + base))
        self.config.write_text('{"repo":{}}')
        self.assertTrue(policy.enabled, 'policy content was not frozen')
        external = w.resolve_policy(self.repo, primary_config=self.repo / '.bureau.json', operator_config=self.config)
        self.assertFalse(external.enabled)
        with self.assertRaises(ValueError): w.resolve_policy(self.repo, primary_config=self.repo / '.bureau.json')
        self.assertEqual(w.resolve_policy(self.repo, primary_config=self.config).source, 'primary:' + str(self.config))

    def test_symlink_and_executable_trust(self):
        base = self.pin({}, symlink=True)
        with self.assertRaises(ValueError):
            w.resolve_policy(self.repo, primary_config=self.repo / '.bureau.json', trusted_base=base)
        executable = self.repo / 'prefix'
        executable.write_text('fake'); executable.chmod(0o700)
        external_link = self.base / 'prefix-link'; external_link.symlink_to(executable)
        for value in (str(executable), str(external_link), str(self.base / 'missing'), str(self.wrapper)):
            self.config.write_text(json.dumps({'repo': {'test_wrapper': [value]}}))
            with self.subTest(value=value), self.assertRaises((OSError, ValueError)):
                w.resolve_policy(self.repo, primary_config=self.config)

    def test_doctor_never_invokes_wrapper(self):
        self.policy()
        with patch.object(w.subprocess, 'Popen', side_effect=AssertionError('doctor invoked a process')):
            report = w.diagnose_policy(self.repo, primary_config=self.config, operator_config=self.config)
        self.assertTrue(report['enabled']); self.assertFalse(report['routing_installed'])
        self.assertIn('not installed yet', report['routing'])
        self.assertEqual(report['executable'], str(Path(sys.executable).resolve()))
        self.assertFalse(self.events.exists())
        with patch.object(d, 'main_checkout', return_value=(self.base, None)):
            (self.base / '.bureau.json').write_text(self.config.read_text())
            os.environ['BUREAU_CONFIG'] = str(self.repo / 'untrusted.json')
            report = d.test_wrapper_diagnostics(self.repo)
        self.assertTrue(report['enabled']); self.assertIn('primary:', report['source'])
        self.config.write_text('{"repo":{"test_wrapper":null}}')
        self.assertIn('error', w.diagnose_policy(self.repo, primary_config=self.config))
        with patch.object(d.module('test-wrapper'), 'validate_policy', side_effect=ValueError('shared validator')):
            errors = d.validate({'linear': {'teams': [{'id':'id','key':'TEAM','states':{'done':'id'}}]}, 'agents':{}, 'repo':{'test_wrapper':[]}})
        self.assertIn('shared validator', errors)

    def test_doctor_needs_no_wrapper_asset_when_unset(self):
        installed = self.base / 'installed'
        installed.mkdir()
        for path in SCRIPTS.iterdir():
            if path.is_file() and path.name != 'bureau-test-wrapper.py':
                shutil.copy2(path, installed / path.name)
        partial = load(installed / 'bureau-doctor.py', 'doctor_without_wrapper')
        config = {'linear': {'teams': [{'id':'id','key':'TEAM','states':{'done':'id'}}]}, 'agents':{}, 'repo':{}}
        self.assertFalse([e for e in partial.validate(config) if 'wrapper' in e])
        self.assertFalse(partial.test_wrapper_diagnostics(self.repo, config=config)['enabled'])
        configured = {**config, 'repo': {'test_wrapper': [sys.executable]}}
        self.assertTrue([e for e in partial.validate(configured) if 'bureau-test-wrapper.py' in e])

    def test_production_launches_stay_unrouted(self):
        # No production call site can consult or route through the new primitive.
        for name in PRODUCTION_LAUNCH_FILES:
            source = (SCRIPTS / name).read_bytes()
            self.assertNotIn(b'test-wrapper', source)
            self.assertNotIn(b'test_wrapper', source)
        installer = (ROOT / 'scripts/bureau_install.py').read_text()
        self.assertIn('template_files("templates/scripts", "*")', installer)
        weights = {}
        for line in (ROOT/'tests/shard-weights.tsv').read_text().splitlines():
            if line and not line.startswith('#'):
                name, linux, mac = line.split('\t')
                self.assertNotIn(name, weights, 'duplicate shard weight')
                weights[name] = (60 if linux == '-' else int(linux), int(mac) if mac != '-' else 120)
        names = sorted(p.name for p in (ROOT/'tests').glob('test_*.sh'))
        for group in ('policy', 'execution', 'process', 'negative'):
            self.assertIn('test_wrapper_' + group + '.sh', weights)
        for os_index, count in ((0,4),(1,7)):
            loads = [0]*count
            for name in sorted(names, key=lambda n: (-weights.get(n,(60,120))[os_index], n)):
                loads[loads.index(min(loads))] += weights.get(name,(60,120))[os_index]
            self.assertLess(max(loads),3600)
            self.assertLessEqual(max(loads),1.1*sum(loads)/count)
        manifest = [line.split('#')[0].strip() for line in (ROOT/'tests/macos-manifest.txt').read_text().splitlines()]
        seconds = sum(weights[name][1] for name in manifest if name)
        self.assertLessEqual(seconds/60+4,90)


    def test_literal_argv_environment_and_canonical_output(self):
        command = [sys.executable, '-I', '-B', '-c',
                   'import json,os,sys; print(json.dumps(dict(argv=sys.argv[1:],env=dict(os.environ),cwd=os.getcwd()))); print("child stderr",file=sys.stderr)',
                   '', 'space value', '$HOME; touch never', 'quote\'"*']
        for env_mode in ('default', 'clean'):
            env = {**os.environ, 'LINEAR_API_KEY':'fixture-secret', 'CARRIER':'fixture-secret', 'BASH_ENV':'bad', 'NORMAL':'normal'}
            policy = self.policy(untrusted_env=env_mode)
            result, out, err = self.execute(command, policy=policy, environ=env)
            self.assert_result(result, 0, 'child-result')
            record = json.loads(out.read_text())
            self.assertEqual(record['argv'], command[5:])
            self.assertEqual(record['cwd'], str(self.repo))
            reduced = w.module('provider').untrusted_env(env, env_mode, git_credentials='off')
            direct = subprocess.run(command, cwd=self.repo, env=reduced, capture_output=True, text=True, check=True)
            self.assertEqual(record['env'], json.loads(direct.stdout)['env'])
            self.assertEqual(err.read_text(), 'child stderr\n')
            self.assertIn('prefix diagnostic', (Path(result.records)/'prefix.stdout').read_text())
            self.assertNotIn('prefix', out.read_text())
            self.assertEqual(os.stat(result.records).st_mode & 0o777, 0o700)
            for path in Path(result.records).iterdir(): self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        result, out, _ = self.execute('printf "%s" "shell boundary"', mode='delay')
        self.assert_result(result, 0, 'child-result'); self.assertEqual(out.read_text(), 'shell boundary')
        policy = self.policy(test_wrapper=[])
        result, _, _ = self.execute(policy=policy)
        self.assert_result(result, 0, 'child-result')

    def test_refusal_and_real_child_same_status(self):
        for code in (0, 7):
            refused, out, _ = self.execute(mode='refuse' + str(code))
            self.assert_result(refused, 24, 'refused'); self.assertEqual(out.read_bytes(), b'')
            real, _, _ = self.execute([sys.executable, '-I', '-B', '-c', f'raise SystemExit({code})'])
            self.assert_result(real, code, 'child-result')

    def test_native_exec_failure_and_shell_status(self):
        missing, _, _ = self.execute([str(self.base / 'no-executable')])
        self.assert_result(missing, 24, 'launch-error'); self.assertEqual(missing.reason, 'exec-failed')
        shell, _, _ = self.execute('exit 127')
        self.assert_result(shell, 127, 'child-result')

    def test_broken_proof_and_status_disagreement(self):
        for mode in ('malformed', 'stale', 'missing-start', 'missing-end', 'swallow'):
            result, _, _ = self.execute([sys.executable, '-I', '-B', '-c', 'raise SystemExit(7)'], mode=mode)
            self.assert_result(result, 24, 'protocol-error')
        record = dict(version=1, execution_id='exec', run_id='run', attempt_id='attempt', command_digest='cmd',
                      policy_digest='policy', queued_at=0.0, pgid=100)
        for change in ({'started_at':-1}, {'started_at':20}, {'pid':0}, {'pgid':101}, {'command_digest':'stale'}):
            candidate = dict(record, started_at=1.0, pid=101, wrapper_status=None); candidate.update(change)
            with self.assertRaises(ValueError): w.proof(candidate, record, 10)

    def test_start_watchdog(self):
        result, out, _ = self.execute(mode='hang', policy=self.policy('hang', test_wrapper_start_timeout_seconds=.12))
        self.assert_result(result, 124, 'timeout')
        self.assertEqual(result.reason, 'wrapper-start-timeout'); self.assertEqual(out.read_bytes(), b'')

    def simulated(self, duration, runtime=w):
        probe = ProbeClock()
        clock = runtime.BudgetClock('run', 'attempt', probe.monotonic)
        policy = self.policy()
        fake = Mock(pid=54321, returncode=None)
        directory = None
        def spawn(argv, **kwargs):
            nonlocal directory
            directory = Path(argv[argv.index('--envelope') + 1])
            request = json.loads((directory/'request.json').read_text())
            self.assertTrue(list(self.control.glob('process-holds/*.json')), 'hold must precede spawn')
            self.assertEqual(clock.charged(), 0)
            probe.now = 600.0
            record = {k: request[k] for k in ('version','execution_id','run_id','attempt_id','command_digest','policy_digest','queued_at')}
            record.update(pid=54322, pgid=fake.pid, started_at=600.0, wrapper_status=None)
            runtime.publish(directory/'start.json', record)
            runtime.publish(directory/'end.json', dict(record, ended_at=600.0+duration, child_status=0))
            probe.now += duration
            fake.returncode = 0
            return fake
        fake.poll.return_value = 0
        with patch.object(runtime.subprocess, 'Popen', side_effect=spawn), patch.object(runtime.module('process').ProcessHold, 'cleanup'):
            result, _, _ = self.execute(runtime=runtime, policy=policy, clock=clock, timeout=300)
        return result, clock

    def test_queued_600_execution_299_succeeds(self):
        result, clock = self.simulated(299)
        self.assert_result(result, 0, 'child-result'); self.assertEqual(clock.charged(), 299)

    def test_execution_over_300_times_out(self):
        result, clock = self.simulated(301)
        self.assert_result(result, 124, 'timeout'); self.assertEqual(result.reason, 'execution-timeout')
        self.assertEqual(clock.charged(), 301)

    def test_live_union_clock_and_outer_consumers(self):
        probe = ProbeClock(); clock = w.BudgetClock('run', 'attempt', probe.monotonic)
        probe.now = 10
        clock.queue(('run','attempt'), 'one')
        probe.now = 20; clock.queue(('run','attempt'), 'two')
        probe.now = 610
        self.assertEqual(clock.charged(), 10, 'live intervals must pause all budget consumers')
        for consumer in ('provider', 'stage', 'silence'):
            with self.subTest(consumer=consumer): self.assertLess(clock.charged(), 300)
        clock.started('one', 600); clock.started('two', 610)
        probe.now = 899; self.assertEqual(clock.charged(), 299)
        probe.now = 901; self.assertGreater(clock.charged(), 300)
        with self.assertRaises(ValueError): clock.queue(('other','attempt'), 'foreign')
        with self.assertRaises(ValueError): clock.queue(('run','attempt'), 'one')
        with self.assertRaises(ValueError): clock.started('one', 901)

    def test_uncertain_cleanup_keeps_hold(self):
        with patch.object(w.module('process').ProcessHold, 'cleanup', side_effect=w.module('process').TerminationUncertain('injected')):
            result, _, _ = self.execute(mode='refuse0')
        self.assertEqual((result.code, result.kind), (25, 'cleanup-uncertain'))
        self.assertTrue(list(self.control.glob('process-holds/*.json')))
        self.assertEqual(w.module('process').uncertain_exit(124), 124)
        self.assertEqual(w.module('process').uncertain_exit(130), 130)

    def runner(self, mode='run', command=None):
        self.policy(mode)
        self.counter += 1
        path = self.base / f'runner-{self.counter}.py'
        output = self.base / f'result-{self.counter}.json'
        command = command or [sys.executable, '-I', '-B', '-c', 'import time; print("running",flush=True); time.sleep(60)']
        path.write_text('import importlib.util,json,sys\nfrom pathlib import Path\n' +
                       f'spec=importlib.util.spec_from_file_location("runner_wrapper", {str(SCRIPTS / "bureau-test-wrapper.py")!r})\n' +
                       'w=importlib.util.module_from_spec(spec); sys.modules[spec.name]=w; spec.loader.exec_module(w)\n' +
                       f'p=w.resolve_policy({str(self.repo)!r}, primary_config={str(self.config)!r})\n' +
                       f'r=w.execute(p, {command!r}, repo={str(self.repo)!r}, stdout={str(self.base / ("running-"+str(self.counter)))!r}, '
                       f'run_id="run", attempt_id="attempt", coordination_root={str(self.control)!r}, grace_seconds=.1)\n' +
                       f'Path({str(output)!r}).write_text(json.dumps(r.__dict__))\n')
        process = subprocess.Popen([sys.executable, '-I', '-B', str(path)], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.addCleanup(self.reap, process)
        return process, output

    @staticmethod
    def reap(process):
        if process.poll() is None: process.kill()
        process.communicate(timeout=10)

    def wait_for(self, condition):
        deadline = time.monotonic()+8
        while time.monotonic() < deadline:
            if condition(): return
            time.sleep(.02)
        self.fail('fixture handshake timed out')

    def cancel(self, signum, queued):
        process, output = self.runner('queued' if queued else 'running')
        if queued:
            self.wait_for(lambda: self.events.exists() and '"queued"' in self.events.read_text())
        else:
            self.wait_for(lambda: any(p.read_text() == 'running\n' for p in self.base.glob('running-*')))
        rows = [json.loads(line) for line in self.events.read_text().splitlines()]
        group = rows[0]['pgid']
        process.send_signal(signum)
        stdout, stderr = process.communicate(timeout=10)
        self.assertEqual(process.returncode, 0, (stdout, stderr))
        result = json.loads(output.read_text())
        self.assertEqual((result['code'], result['kind']), (130, 'cancelled'), result)
        self.assertFalse(list(self.control.glob('process-holds/*.json')))
        # Never bypass denied inspection: failure here is an unverified case.
        try:
            os.killpg(group, 0)
        except ProcessLookupError:
            pass
        except PermissionError:
            self.fail('sandbox denied group inspection; maintainer must run this case')
        else:
            self.fail('owned group survived cancellation')
        if queued:
            time.sleep(.12)
            self.assertNotIn('start-envelope', self.events.read_text())
            self.assertFalse(any(p.stat().st_size for p in self.base.glob('running-*')))

    def test_int_queued(self): self.cancel(signal.SIGINT, True)
    def test_term_queued(self): self.cancel(signal.SIGTERM, True)
    def test_hup_queued(self): self.cancel(signal.SIGHUP, True)
    def test_int_running(self): self.cancel(signal.SIGINT, False)
    def test_term_running(self): self.cancel(signal.SIGTERM, False)
    def test_hup_running(self): self.cancel(signal.SIGHUP, False)

    def test_serializes_only_execution_envelopes(self):
        command = [sys.executable, '-I', '-B', '-c', 'import time; time.sleep(.2)']
        first, first_result = self.runner('serialize', command)
        second, second_result = self.runner('serialize', command)
        for process in (first, second):
            stdout, stderr = process.communicate(timeout=10)
            self.assertEqual(process.returncode, 0, (stdout, stderr))
        for path in (first_result, second_result):
            result = json.loads(path.read_text()); self.assertEqual((result['code'],result['kind']), (0,'child-result'), result)
        rows = [json.loads(line) for line in self.events.read_text().splitlines()]
        active = 0
        for row in rows:
            if row['event'] == 'start-envelope': active += 1; self.assertEqual(active,1)
            elif row['event'] == 'release': active -= 1; self.assertEqual(active,0)
        self.assertEqual(active,0)
        self.assertEqual(sum(r['event']=='release' for r in rows),2)
        self.assertFalse(list(self.control.glob('process-holds/*.json')))

    def test_negative_controls(self):
        source = (SCRIPTS/'bureau-test-wrapper.py').read_text()
        mutations = (
            ("if start is None:\n                            result = Result(24, 'refused'", "if start is None:\n                            result = Result(0, 'child-result'", 'refusal'),
            ("execution_end - start['started_at'] > timeout", 'execution_end - queued > timeout', 'timing'),
            ("if start is None: raise ValueError('end without start proof')", 'if start is None: return', 'proof'),
        )
        for index, (before, after, kind) in enumerate(mutations):
            self.assertIn(before, source)
            target = self.base/f'mutant-{index}.py'; target.write_text(source.replace(before, after))
            mutant = load(target, f'wrapper_mutant_{index}')
            mutant.SCRIPTS = SCRIPTS
            if kind == 'refusal':
                result, _, _ = self.execute(mode='refuse0', runtime=mutant)
                with self.assertRaises(AssertionError): self.assert_result(result,24,'refused')
            elif kind == 'timing':
                result, _ = self.simulated(299, runtime=mutant)
                with self.assertRaises(AssertionError): self.assert_result(result,0,'child-result')
            else:
                expected = dict(version=1,execution_id='exec',run_id='run',attempt_id='attempt',command_digest='cmd',
                                policy_digest='policy',queued_at=0.,pgid=1)
                record = dict(expected,started_at=1.,ended_at=2.,pid=2,child_status=0,wrapper_status=None)
                def oracle(runtime):
                    with self.assertRaises(ValueError): runtime.proof(record,expected,3.)
                oracle(w)
                with self.assertRaises(AssertionError): oracle(mutant)


PRODUCTION_LAUNCH_FILES = (
    'bureau-provider.py',
    'bureau-gates.py',
    'bureau-changed-tests.py',
    'bureau-conflicts.py',
    'bureau-scan.py',
    'bureau-semgrep.py',
    'bureau-trivy.py',
    'bureau-impeccable.py',
    'bureau-worktree-deps.py',
    'bureau-ticket.py',
    'bureau-app.sh',
    'bureau-config.sh',
    'qa-pipeline.sh',
    'code-review-pipeline.sh',
    'implement-pipeline.sh',
    'upstream-port.sh',
)
POLICY_CASES = ('test_policy_validation', 'test_trusted_sources', 'test_symlink_and_executable_trust',
                'test_doctor_never_invokes_wrapper', 'test_doctor_needs_no_wrapper_asset_when_unset',
                'test_production_launches_stay_unrouted')
PROCESS_CASES = ('test_int_queued', 'test_term_queued', 'test_hup_queued', 'test_int_running', 'test_term_running',
                 'test_hup_running', 'test_serializes_only_execution_envelopes')
if __name__ == '__main__':
    if len(sys.argv) == 2 and sys.argv[1] in ('--policy', '--execution', '--process', '--negative'):
        group = sys.argv.pop()
        names = unittest.defaultTestLoader.getTestCaseNames(WrapperTests)
        selected = [name for name in names if (name in POLICY_CASES if group == '--policy' else
                    name in PROCESS_CASES if group == '--process' else name == 'test_negative_controls' if group == '--negative' else
                    name not in (*POLICY_CASES, *PROCESS_CASES, 'test_negative_controls'))]
        unittest.main(defaultTest=[f'WrapperTests.{name}' for name in selected])
    else:
        unittest.main()
