"""Cheap private-repository acceptance cases for the local suite orchestrator."""
import json
import io
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
sys.path.insert(0, str(Path(__file__).resolve().parent))
import run_suite as suite
import run_test_files as runner
from process_helpers import process_group_running

ROOT = Path(__file__).resolve().parents[1]


class SuiteTests(unittest.TestCase):
    def setUp(self):
        temp_parent = Path(tempfile.gettempdir()).resolve()
        self.assertFalse(any((p / '.git').exists() for p in (temp_parent, *temp_parent.parents)),
                         'fixture temporary parent must be outside any Git repository')
        self.temp = tempfile.TemporaryDirectory(prefix='bureau-suite. ')
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.repo, self.out, self.state = (self.base / n for n in ('repo', 'out', 'state'))
        self.tests = self.repo / 'tests'
        self.tests.mkdir(parents=True)
        for name in ('run.sh', 'run_test_files.py', 'process_helpers.py', 'run_suite.py'):
            shutil.copy2(ROOT / 'tests' / name, self.tests / name)
        for name in ('home', 'tmp'):
            (self.base / name).mkdir()
        self.env = {k: v for k, v in os.environ.items() if not k.startswith(('BUREAU_', 'GIT_'))
                    and k not in ('CI', 'GITHUB_ACTIONS', 'XDG_STATE_HOME', 'PYTHONPATH')}
        self.env.update(PYTHONDONTWRITEBYTECODE='1', HOME=str(self.base / 'home'),
                        XDG_STATE_HOME=str(self.base / 'state-home'), TMPDIR=str(self.base / 'tmp'),
                        GIT_CEILING_DIRECTORIES=str(self.base), GIT_CONFIG_NOSYSTEM='1',
                        GIT_CONFIG_GLOBAL=os.devnull, RUNNER_OS='Linux',
                        SUITE_EVENTS=str(self.base / 'events'), SUITE_ONCE=str(self.base / 'once'))
        self.git('init', '-q')
        self.git('config', 'user.name', 'Fixture')
        self.git('config', 'user.email', 'fixture@example.invalid')
        self.plant('test_a.sh', 'exit 0\n')
        self.plant('test_b.sh', 'exit 0\n')
        self.weights()
        self.children = []
        self.addCleanup(self.stop_children)

    def git(self, *args):
        return subprocess.check_output(['git', '-C', str(self.repo), *args], env=self.env,
                                       text=True, stderr=subprocess.STDOUT, timeout=5).strip()

    def plant(self, name, body):
        (self.tests / name).write_text(body)

    def weights(self, **values):
        (self.tests / 'shard-weights.tsv').write_text(''.join(
            f'{p.name}\t{values.get(p.name, 1)}\t{values.get(p.name, 1)}\n'
            for p in sorted(self.tests.glob('test_*.sh'))))

    def commit(self):
        self.git('add', '.')
        self.git('commit', '-qm', 'fixture')

    def command(self, tag='sample', *extra):
        return [sys.executable, '-B', str(self.tests / 'run_suite.py'), '--out', str(self.out),
                '--tag', tag, '--project', 'sample', '--state-dir', str(self.state), *extra]

    def run_suite(self, tag='sample', *, env=None, extra=()):
        child = subprocess.run(self.command(tag, *extra), cwd=self.repo, env={**self.env, **(env or {})},
                               capture_output=True, text=True, timeout=20)
        path = self.out / f'suite-{tag}-result.json'
        return child, json.loads(path.read_text()) if path.exists() else None

    def assert_state(self, child, result, state, code):
        self.assertEqual(child.returncode, code, child.stdout + child.stderr + repr(result))
        self.assertEqual(result['state'], state, result)

    def fail_once(self, name='test_a.sh'):
        self.plant(name, 'if mkdir "$SUITE_ONCE-${0##*/}" 2>/dev/null; then exit 1; fi\n')

    def assert_no_rerun(self, result):
        self.assertEqual(result['rerun_logs'], {}, result)
        self.assertFalse(list(self.out.glob('*-rerun-*.log')))

    def test_fail_once_is_observed_file_flake(self):
        self.fail_once()
        self.commit()
        child, result = self.run_suite()
        self.assert_state(child, result, 'passed_after_serial_rerun', 0)
        self.assertEqual(result['recovered'], {'test_a.sh': 'failed_in_parallel_passed_serially'})
        self.assertEqual(result['first_pass_failures'], ['test_a.sh'])
        self.assertEqual([a['outcome'] for a in result['attempts']['test_a.sh']], ['FAIL', 'PASS'])
        self.assertEqual(len(result['attempts']['test_b.sh']), 1)
        row, = [json.loads(line) for line in (self.state / 'flakes.jsonl').read_text().splitlines()]
        self.assertEqual((row['file'], row['test_name'], row['identity_scope']), ('test_a.sh', 'test_a.sh', 'file'))
        self.assertEqual(row['sha'], self.git('rev-parse', 'HEAD'))
        self.assertEqual(result['not_attempted'], [])

    def test_always_failing_stops_after_one_rerun(self):
        self.plant('test_a.sh', 'exit 1\n')
        self.plant('test_b.sh', 'exit 1\n')
        self.commit()
        child, result = self.run_suite()
        self.assert_state(child, result, 'failed', 1)
        self.assertEqual(len(result['attempts']['test_a.sh']), 2)
        self.assertEqual(len(result['attempts']['test_b.sh']), 1)
        self.assertEqual(result['not_attempted'], ['test_b.sh'])

    def test_all_shards_finish_before_serial_reruns(self):
        for name in ('test_a.sh', 'test_b.sh'):
            self.plant(name, '''name=${0##*/}
if mkdir "$SUITE_ONCE-$name" 2>/dev/null; then
  echo "parallel-start $name" >> "$SUITE_EVENTS"
  sleep .2
  echo "parallel-end $name" >> "$SUITE_EVENTS"
  exit 1
fi
mkdir "$SUITE_ONCE-active" || exit 7
echo "serial-start $name" >> "$SUITE_EVENTS"
sleep .1
echo "serial-end $name" >> "$SUITE_EVENTS"
rmdir "$SUITE_ONCE-active"
''')
        self.commit()
        child, result = self.run_suite()
        self.assert_state(child, result, 'passed_after_serial_rerun', 0)
        events = (self.base / 'events').read_text().splitlines()
        first_serial = next(i for i, e in enumerate(events) if e.startswith('serial-start'))
        self.assertEqual(sum(e.startswith('parallel-end') for e in events[:first_serial]), 2)
        self.assertEqual([e.split()[0] for e in events[first_serial:]],
                         ['serial-start', 'serial-end', 'serial-start', 'serial-end'])

    def test_clean_timeout_is_rerun_with_original_deadline(self):
        self.plant('test_a.sh', '''if mkdir "$SUITE_ONCE" 2>/dev/null; then
  exec python3 -c 'import time; time.sleep(30)'
fi
''')
        self.commit()
        child, result = self.run_suite(env={'BUREAU_TEST_FILE_TIMEOUT': '1'})
        self.assert_state(child, result, 'passed_after_serial_rerun', 0)
        attempts = result['attempts']['test_a.sh']
        self.assertEqual([a['outcome'] for a in attempts], ['TIMEOUT', 'PASS'])
        self.assertEqual([a['deadline'] for a in attempts], [1, 1])
        self.assertTrue(all(a['cleanup_ok'] for a in attempts))

    def test_crashed_shard_never_reruns(self):
        path = self.tests / 'run_test_files.py'
        path.write_text(path.read_text().replace('override = timeout_override()', 'os._exit(7)'))
        self.commit()
        child, result = self.run_suite()
        self.assert_state(child, result, 'invalid', 2)
        self.assert_no_rerun(result)

    def test_nonzero_exit_without_failed_file_never_reruns(self):
        path = self.tests / 'run_test_files.py'
        path.write_text(path.read_text().replace('return int(bool(failed))', 'return 1'))
        self.commit()
        child, result = self.run_suite()
        self.assert_state(child, result, 'invalid', 2)
        self.assert_no_rerun(result)

    def test_bad_records_and_failed_cleanup_never_rerun(self):
        self.fail_once()
        path = self.tests / 'run_test_files.py'
        original = path.read_text()
        mutations = {
            'malformed': ("output.write(json.dumps(dict(file=name", "output.write('broken\\n')\n            output.write(json.dumps(dict(file=name"),
            'missing': ("output.write(json.dumps(dict(file=name", "return\n            output.write(json.dumps(dict(file=name"),
            'cleanup': ("evidence['cleanup_ok'] = True", "evidence['cleanup_ok'] = False"),
            'duplicate': ("output.flush()", "output.flush()\n            output.write(json.dumps(dict(file=name, outcome=outcome, elapsed_seconds=elapsed, deadline=timeout, **evidence)) + '\\n')"),
        }
        for tag, (old, new) in mutations.items():
            with self.subTest(fault=tag):
                path.write_text(original.replace(old, new))
                self.commit()
                child, result = self.run_suite(tag)
                self.assert_state(child, result, 'invalid', 2)
                self.assert_no_rerun(result)

    def test_vanished_selected_file_is_reported_missing(self):
        path = self.tests / 'run_test_files.py'
        path.write_text(path.read_text().replace('for name in names:',
                        "for name in names:\n            Path(name).unlink()"))
        self.commit()
        child, result = self.run_suite()
        self.assert_state(child, result, 'invalid', 2)
        self.assert_no_rerun(result)
        # Also exercise the opt-in MISSING record without Git integrity masking it.
        records = self.base / 'missing.jsonl'
        # Use the unchanged runner for a genuinely already-missing selection.
        shutil.copy2(ROOT / 'tests/run_test_files.py', self.tests / 'run_test_files.py')
        subprocess.run([sys.executable, '-I', str(self.tests / 'run_test_files.py'), 'test_gone.sh'],
                       cwd=self.tests, env={**self.env, 'BUREAU_TEST_RESULTS': str(records)}, check=False,
                       capture_output=True, timeout=5)
        row, = [json.loads(line) for line in records.read_text().splitlines()]
        self.assertEqual(row['outcome'], 'MISSING')

    def test_dirty_worktree_is_invalid(self):
        self.commit()
        self.plant('test_a.sh', 'exit 1\n')
        child, result = self.run_suite()
        self.assert_state(child, result, 'invalid', 2)
        self.assertEqual(result['shards'], [])

    def test_moved_head_is_invalid(self):
        self.plant('test_a.sh', 'git -c core.hooksPath=/dev/null commit --allow-empty -qm moved\n')
        self.commit()
        child, result = self.run_suite()
        self.assert_state(child, result, 'invalid', 2)
        self.assert_no_rerun(result)

    def test_rerun_budget_uses_expected_seconds(self):
        self.fail_once()
        self.weights(**{'test_a.sh': 2193})
        self.commit()
        child, result = self.run_suite()
        self.assert_state(child, result, 'passed_after_serial_rerun', 0)
        self.assertEqual(result['attempts']['test_a.sh'][1]['deadline'], 6579)
        # A different, over-budget file set never gets a serial attempt.
        self.plant('test_a.sh', 'exit 1\n')
        self.weights(**{'test_a.sh': 3601})
        self.commit()
        child, result = self.run_suite('over')
        self.assert_state(child, result, 'failed', 1)
        self.assertIn('budget', result['reason'])
        self.assert_no_rerun_for_tag(result, 'over')

    def assert_no_rerun_for_tag(self, result, tag):
        self.assertEqual(result['rerun_logs'], {})
        self.assertFalse(list(self.out.glob(f'suite-{tag}-rerun-*.log')))

    def test_more_than_five_failures_never_reruns(self):
        for letter in 'abcdef':
            self.plant(f'test_{letter}.sh', 'exit 1\n')
        self.weights()
        self.commit()
        child, result = self.run_suite()
        self.assert_state(child, result, 'failed', 1)
        self.assertEqual(len(result['not_attempted']), 6)
        self.assert_no_rerun(result)

    def test_refuses_ci_and_partial_selection(self):
        self.commit()
        for key in ('CI', 'GITHUB_ACTIONS', 'BUREAU_TEST_SHARD', 'BUREAU_TEST_MANIFEST'):
            with self.subTest(key=key):
                child, result = self.run_suite(env={key: ''})
                self.assertEqual(child.returncode, 2, child.stderr)
                self.assertIsNone(result)
        child, _ = self.run_suite(extra=('--out', str(self.repo / 'logs')))
        self.assertEqual(child.returncode, 2)
        child, _ = self.run_suite(extra=('--state-dir', str(self.repo / 'state')))
        self.assertEqual(child.returncode, 2)

    def test_nested_runner_cannot_write_suite_records(self):
        self.plant('test_a.sh', 'python3 -I run_test_files.py test_b.sh\n')
        self.commit()
        child, result = self.run_suite()
        self.assert_state(child, result, 'passed', 0)
        self.assertEqual(len(result['attempts']['test_b.sh']), 1, result)

    def test_direct_run_sh_never_retries(self):
        self.fail_once()
        self.commit()
        child = subprocess.run(['bash', 'tests/run.sh'], cwd=self.repo, env=self.env,
                               capture_output=True, text=True, timeout=10)
        self.assertEqual(child.returncode, 1, child.stdout + child.stderr)
        self.assertIn('Results: 1 passed, 1 failed', child.stdout)
        self.assertNotIn('rerun', child.stdout)

    def observation(self, sha, tag=None):
        return dict(sha=sha, tag=tag or sha, recovered={'test_a.sh': 'failed_in_parallel_passed_serially'})

    def test_ledger_three_shas_replay_and_open_proposal(self):
        for sha in ('a', 'a', 'b', 'c'):
            ids = suite.ledger(self.state, self.observation(sha), now=1000000)
        self.assertEqual(len(ids), 1)
        suite.ledger(self.state, self.observation('c'), now=1000001)
        suite.ledger(self.state, self.observation('d'), now=1000002)
        self.assertEqual(len(list((self.state / 'proposals').glob('*.json'))), 1)
        rows = (self.state / 'flakes.jsonl').read_text().splitlines()
        self.assertEqual(len(rows), 4)
        proposal = json.loads(next((self.state / 'proposals').glob('*.json')).read_text())
        self.assertEqual(proposal['shas'], ['a', 'b', 'c'])
        self.assertEqual(proposal['run_tags'], ['a', 'b', 'c'])

    def test_ledger_window_boundary_and_same_sha_does_not_refresh(self):
        now = 1000000
        suite.ledger(self.state, self.observation('old'), now=now - 604801)
        suite.ledger(self.state, self.observation('edge'), now=now - 604800)
        suite.ledger(self.state, self.observation('old', 'repeat'), now=now)
        suite.ledger(self.state, self.observation('second'), now=now)
        self.assertEqual(list((self.state / 'proposals').glob('*.json')), [])
        suite.ledger(self.state, self.observation('third'), now=now)
        proposal = json.loads(next((self.state / 'proposals').glob('*.json')).read_text())
        self.assertEqual(proposal['shas'], ['edge', 'second', 'third'])

    def test_resolved_proposal_starts_a_new_episode(self):
        for sha in 'abc':
            ids = suite.ledger(self.state, self.observation(sha), now=1000000)
        suite.ledger(self.state, resolve=ids[0], now=1000001)
        suite.ledger(self.state, self.observation('d'), now=1000001)
        for sha in 'ab':
            self.assertEqual(suite.ledger(self.state, self.observation(sha, 'new-' + sha), now=1000002), [])
        suite.ledger(self.state, self.observation('c', 'new-c'), now=1000002)
        proposals = [json.loads(p.read_text()) for p in (self.state / 'proposals').glob('*.json')]
        self.assertEqual(sorted(p['status'] for p in proposals), ['open', 'resolved'])
        self.assertEqual(next(p for p in proposals if p['status'] == 'open')['shas'], ['a', 'b', 'c'])

    def test_proposal_reconciliation_after_write_failure(self):
        for sha in 'ab':
            suite.ledger(self.state, self.observation(sha), now=1000000)
        with patch.object(suite, 'atomic_json', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                suite.ledger(self.state, self.observation('c'), now=1000000)
        suite.ledger(self.state, self.observation('c'), now=1000001)
        self.assertEqual(len((self.state / 'flakes.jsonl').read_text().splitlines()), 3)
        self.assertEqual(len(list((self.state / 'proposals').glob('*.json'))), 1)

    def test_failed_ledger_write_prevents_green(self):
        self.fail_once()
        path = self.tests / 'run_suite.py'
        source = path.read_text()
        needle = "with path.open('a') as output:"
        self.assertEqual(source.count(needle), 1)
        path.write_text(source.replace(needle, "raise OSError('ledger write failed')\n                " + needle))
        self.commit()
        child, result = self.run_suite()
        self.assert_state(child, result, 'invalid', 2)
        self.assertEqual(result['attempts']['test_a.sh'][1]['outcome'], 'PASS')

    def test_passed_state_and_private_default_state_paths(self):
        self.fail_once()
        self.commit()
        for tag, xdg in (('xdg', True), ('fallback', False)):
            command = self.command(tag)
            del command[-2:]  # Exercise defaults without ever using the real home/state.
            env = self.env.copy()
            if not xdg:
                env.pop('XDG_STATE_HOME')
                (self.base / 'once-test_a.sh').rmdir()
            child = subprocess.run(command, cwd=self.repo, env=env, capture_output=True, text=True, timeout=10)
            result = json.loads((self.out / f'suite-{tag}-result.json').read_text())
            self.assert_state(child, result, 'passed_after_serial_rerun', 0)
            state = self.base / ('state-home' if xdg else 'home/.local/state')
            self.assertTrue((state / 'bureau/suites/sample/flakes.jsonl').is_file())
        child, result = self.run_suite('clean')
        self.assert_state(child, result, 'passed', 0)
        self.assertEqual(result['recovered'], {})
        self.assert_no_rerun_for_tag(result, 'clean')

    def test_selection_must_be_exhaustive(self):
        script = self.tests / 'run.sh'
        script.write_text(script.read_text().replace("print('\\n'.join(sorted(shards[index - 1])))",
                                                   "print('test_a.sh')"))
        self.commit()
        child, result = self.run_suite()
        self.assert_state(child, result, 'invalid', 2)
        self.assertEqual(result['shards'], [])

    def signal_case(self, phase):
        self.plant('test_a.sh', '''if [ "$SUITE_SIGNAL_PHASE" = serial ] && mkdir "$SUITE_ONCE" 2>/dev/null; then exit 1; fi
exec python3 - <<'PY'
import os, signal, subprocess, sys, time
from pathlib import Path
child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])
def stop(signum, frame):
    child.terminate()
    child.wait(timeout=2)
    sys.exit(0)
signal.signal(signal.SIGTERM, stop)
Path(os.environ['SUITE_EVENTS']).write_text(str(os.getpid()))
time.sleep(30)
PY
''')
        self.commit()
        child = subprocess.Popen(self.command(), cwd=self.repo,
                                 env={**self.env, 'SUITE_SIGNAL_PHASE': phase},
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.children.append(child)
        end = time.monotonic() + 8
        while not (self.base / 'events').exists():
            self.assertIsNone(child.poll())
            self.assertLess(time.monotonic(), end, 'fixture never started')
            time.sleep(.02)
        group = int((self.base / 'events').read_text())
        child.send_signal(signal.SIGHUP if phase == 'serial' else signal.SIGINT)
        self.assertEqual(child.wait(timeout=10), 130)
        result = json.loads((self.out / 'suite-sample-result.json').read_text())
        self.assertEqual(result['state'], 'cancelled')
        self.assertFalse(process_group_running(group), f'test group {group} survived or inspection unavailable')
        self.assertEqual(bool(result['rerun_logs']), phase == 'serial')

    def stop_children(self):
        for child in self.children:
            if child.poll() is None:
                child.terminate()
                child.wait(timeout=10)

    def test_signal_during_shard_cleans_test_group(self):
        self.signal_case('parallel')

    def test_signal_during_rerun_cleans_test_group(self):
        self.signal_case('serial')

    def test_opt_in_records_and_legacy_missing_skip(self):
        self.commit()
        for opt in (False, True):
            records = self.base / 'records.jsonl'
            env = {**self.env, **({'BUREAU_TEST_RESULTS': str(records)} if opt else {})}
            child = subprocess.run([sys.executable, '-I', str(self.tests / 'run_test_files.py'),
                                    'test_gone.sh', 'test_a.sh'], cwd=self.tests, env=env,
                                   text=True, capture_output=True, timeout=5)
            self.assertEqual(child.returncode, int(opt), child.stdout + child.stderr)
            if opt:
                rows = [json.loads(line) for line in records.read_text().splitlines()]
                self.assertEqual([r['outcome'] for r in rows], ['MISSING', 'PASS'])
                self.assertEqual(rows[1]['deadline'], 600)
            else:
                self.assertFalse(records.exists())
                self.assertIn('Results: 1 passed, 0 failed', child.stdout)
        self.assertFalse(list(self.base.rglob('__pycache__')))

    def test_pending_launch_signal_still_cleans_group(self):
        child = Mock(pid=12345)
        events = []

        def mask(how, signals):
            events.append(how)
            if how == signal.SIG_SETMASK:
                raise KeyboardInterrupt  # Pending signal is delivered at unmask.
            return set()

        with patch.object(runner.signal, 'pthread_sigmask', side_effect=mask), \
                patch.object(runner.subprocess, 'Popen', return_value=child), \
                patch.object(runner, 'stop_group') as stop:
            with self.assertRaises(KeyboardInterrupt):
                runner.run_file('test_a.sh', 600, io.StringIO(), {})
        self.assertEqual(events, [signal.SIG_BLOCK, signal.SIG_SETMASK])
        stop.assert_called_once_with(child)
        child.wait.assert_not_called()


if __name__ == '__main__':
    unittest.main()
