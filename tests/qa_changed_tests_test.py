"""Real QA/Git fixtures; services are faked and all temporary trees are owned."""
import copy
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
import uuid
from unittest.mock import patch

import git_maintenance

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / 'tests/fixtures/qa-changed-tests'


def module(directory, name):
    spec = importlib.util.spec_from_file_location(name.replace('-', '_'), directory / (name + '.py'))
    result = importlib.util.module_from_spec(spec); spec.loader.exec_module(result)
    return result


c = module(ROOT / 'templates/scripts', 'bureau-changed-tests')
a, e = c.a, c.e
TEMPLATE = 'PYTHONPATH=. exec python3 {file}'
GIT_CONTEXT = ('GIT_INDEX_FILE', 'GIT_DIR', 'GIT_WORK_TREE', 'GIT_OBJECT_DIRECTORY',
               'GIT_ALTERNATE_OBJECT_DIRECTORIES', 'GIT_COMMON_DIR', 'GIT_NAMESPACE',
               'GIT_PREFIX', 'GIT_CEILING_DIRECTORIES')
PUBLISH_READY = '''def publish_ready(path, value):
    import tempfile
    with tempfile.NamedTemporaryFile(mode='w', dir=path.parent, delete=False) as output:
        output.write(value)
    Path(output.name).replace(path)
'''
CASE = '''from pathlib import Path
import unittest
from product import VALUE

class Case(unittest.TestCase):
    def test_value(self):
        {assertion}

if __name__ == '__main__': unittest.main()
'''


class MeasurementFixtures(unittest.TestCase):
    def test_git_context_is_scoped_and_restored_on_errors(self):
        inherited = {name: 'caller-value' for name in GIT_CONTEXT}
        with patch.dict(os.environ, inherited):
            before = dict(os.environ)
            with self.assertRaises(RuntimeError), c.git_context():
                self.assertFalse(set(GIT_CONTEXT) & os.environ.keys())
                with c.git_context(): self.assertFalse(set(GIT_CONTEXT) & os.environ.keys())
                raise RuntimeError('fixture')
            self.assertEqual(before, dict(os.environ))

    def test_readiness_is_published_after_the_write_closes(self):
        with tempfile.TemporaryDirectory(prefix='.qa-ready-fixture-', dir=ROOT) as directory:
            ready = Path(directory) / 'ready'
            namespace = dict(Path=Path); exec(PUBLISH_READY, namespace)
            original = tempfile.NamedTemporaryFile
            def opened(*args, **kwargs):
                output = original(*args, **kwargs); write = output.write
                def preempted(value):
                    write(value[:1]); output.flush()
                    self.assertFalse(ready.exists(), 'partial readiness was published')
                    write(value[1:])
                output.write = preempted
                return output
            with patch.object(tempfile, 'NamedTemporaryFile', side_effect=opened) as publish:
                namespace['publish_ready'](ready, '{"ready":true}')
            publish.assert_called_once()
            self.assertEqual(dict(ready=True), json.loads(ready.read_text()))
            self.assertEqual([ready], list(Path(directory).iterdir()))


class ChangedTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='.qa-changed-fixture-', dir=ROOT)
        self.addCleanup(temp.cleanup); self.root = Path(temp.name).resolve()
        self.env = {**os.environ, **git_maintenance.OFF_ENV, 'TMPDIR':str(self.root),
                    'GIT_CONFIG_GLOBAL':os.devnull, 'GIT_CONFIG_NOSYSTEM':'1', 'PYTHONDONTWRITEBYTECODE':'1'}
        command = '''set -euo pipefail
source "$1"
sandbox_init
trap - EXIT
python3 -c 'import json, os; print(json.dumps({k:v for k,v in os.environ.items() if k in ("PATH", "SANDBOX") or k.startswith(("BUREAU_", "FAKE_", "GH_STUB"))}))'
'''
        result = subprocess.run(['/bin/bash', '-c', command, 'fixture', str(ROOT / 'tests/lib/harness.sh')],
                                env=self.env, capture_output=True, text=True, timeout=20, check=True)
        self.env.update(json.loads(result.stdout)); self.repo = Path(self.env['SANDBOX'])
        self.scripts = self.repo / 'scripts'; self.issue = self.env['BUREAU_STUB_PICKED_ISSUE']
        self.env.update(BUREAU_CONFIG=str(self.repo / '.bureau.json'), BUREAU_STUB_STATE_QA='state-qa',
                        BUREAU_STUB_ISSUE_STATE='QA', BUREAU_QA_TEST_TIMEOUT='120')
        source = (ROOT / 'templates/scripts/bureau-config.sh').read_text()
        commit = source[source.index('commit_stage_changes() {'):source.index('# Build the model invocation')]
        with (self.scripts / 'bureau-config.sh').open('a') as out: out.write('\n' + commit)
        shutil.rmtree(self.repo / 'specs')
        self.write('.gitignore', '.fake-origin.git/\nscripts/\nlogs/\n.env\n*.log\n__pycache__/\nshared\n')
        self.write('shared/available', 'shared dependency\n')
        self.write('product.py', 'VALUE = 0\n')
        self.write('test_gate.py', 'import unittest\nclass Gate(unittest.TestCase):\n    def test_gate(self): self.assertTrue(True)\n')
        self.config = dict(repo=dict(test_command='python3 -m unittest -q test_gate', worktree_links=[]))
        self.write('.bureau.json', json.dumps(self.config))
        self.git('add', '.'); self.git('commit', '-qm', 'test: base without measurement')
        self.disabled_base = self.git('rev-parse', 'HEAD')
        self.config['repo']['changed_test_command'] = TEMPLATE
        self.write('.bureau.json', json.dumps(self.config))
        self.git('add', '.bureau.json'); self.git('commit', '-qm', 'test: enable measurement')
        self.base = self.git('rev-parse', 'HEAD')
        self.set_base(True)
        self.write('product.py', 'VALUE = 1\n'); self.write('new_module.py', 'ADDED = True\n')
        self.store = e.Store(self.repo / '.git/bureau'); self.pr = 0
        self.shared = (self.repo / 'shared/available').read_bytes()

    def write(self, path, value):
        target = self.repo / path; target.parent.mkdir(parents=True, exist_ok=True); target.write_text(value)

    def git(self, *args):
        return subprocess.check_output(['git', '-C', str(self.repo), *args], env=self.env,
                                       stderr=subprocess.DEVNULL, text=True, timeout=10).strip()

    def set_base(self, enabled):
        self.git('update-ref', 'refs/heads/main', self.base if enabled else self.disabled_base)
        self.git('push', '-q', '--force', 'origin', 'main')

    def publish(self, paths):
        for path, source in paths.items(): self.write(path, source)
        self.git('add', '.'); self.git('commit', '-qm', 'test: change product and tests')
        self.head = self.git('rev-parse', 'HEAD'); self.git('push', '-q', 'origin', 'HEAD')

    def wrap_gh(self, body):
        original = shutil.which('gh', path=self.env['PATH'])
        directory = self.root / 'bin'; directory.mkdir()
        wrapper = directory / 'gh'
        wrapper.write_text('#!' + sys.executable + '\nimport os, sys, time\nfrom pathlib import Path\n' + body +
                           '\nos.execv(' + repr(original) + ', [' + repr(original) + ', *sys.argv[1:]])\n')
        wrapper.chmod(0o700)
        self.env['PATH'] = str(directory) + os.pathsep + self.env['PATH']

    def begin(self):
        self.pr += 1; self.run = uuid.uuid4().hex
        self.store.begin_run(self.run, self.issue, self.repo, self.head)
        self.attempt = self.store.begin_attempt(self.run, self.issue, 'qa', 'stage', self.repo, self.head)
        self.store.set_pr(self.run, self.attempt, self.pr)
        self.env.update(BUREAU_RUN_ID=self.run, BUREAU_STAGE_ATTEMPT_ID=self.attempt)

    def state(self):
        return (self.git('rev-parse', 'HEAD'), self.git('write-tree'), self.git('status', '--porcelain', '-uall'),
                self.git('worktree', 'list', '--porcelain'))

    def contents(self):
        paths = [path for path in self.repo.rglob('*')
                 if path.relative_to(self.repo).parts[0] not in ('.git', '.fake-origin.git') and path.is_file()]
        paths += [self.repo / '.git' / name for name in ('HEAD', 'index')]
        return {str(path.relative_to(self.repo)):path.read_bytes() for path in paths}

    def git_environment(self):
        return dict(GIT_INDEX_FILE=str(self.repo / '.git/index'), GIT_DIR=str(self.repo / '.git'),
                    GIT_WORK_TREE=str(self.repo), GIT_OBJECT_DIRECTORY=str(self.repo / '.git/objects'),
                    GIT_ALTERNATE_OBJECT_DIRECTORIES=str(self.repo / '.git/objects'),
                    GIT_COMMON_DIR=str(self.repo / '.git'), GIT_NAMESPACE='caller', GIT_PREFIX='caller/',
                    GIT_CEILING_DIRECTORIES=str(self.root))

    def stop_child(self, child):
        if child.poll() is None:
            try: os.killpg(child.pid, signal.SIGTERM)
            except ProcessLookupError: pass
            try: child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(child.pid, signal.SIGKILL); child.wait(timeout=5)

    def wait_ready(self, path, child):
        deadline = time.monotonic() + 120
        while not path.exists() and child.poll() is None and time.monotonic() < deadline: time.sleep(.03)
        self.assertTrue(path.exists(), 'run never reached its interrupt point')
        return path.read_text()

    def qa(self, enabled=True, interrupt=False, expected='GREEN', runtime=False, code=0, leftover=False, held=False,
           interrupt_wait=None):
        self.set_base(enabled); self.begin()
        ready = self.repo / 'logs/changed-ready'
        ready.unlink(missing_ok=True)
        env = {**self.env, 'CHANGED_READY':str(ready)}
        command = ['/bin/bash', str(self.scripts / 'qa-pipeline.sh'), self.issue]
        if runtime:
            command = [sys.executable, '-I', str(self.scripts / 'bureau-runtime.py'), '--repo', str(self.repo),
                       'exec', '--issue', self.issue, '--entry', str(self.scripts / 'qa-pipeline.sh'), '--', *command]
        child = subprocess.Popen(command,
                                 cwd=self.repo, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                 text=True, start_new_session=True)
        try:
            if interrupt:
                owner = json.loads(self.wait_ready(ready, child)); self.interrupted_tree = Path(owner['tree'])
                if interrupt_wait:
                    os.kill(owner['parent'], signal.SIGTERM)
                    self.wait_ready(interrupt_wait, child)
                os.kill(child.pid if runtime else owner['parent'], signal.SIGTERM)
            out, err = child.communicate(timeout=180)
        finally: self.stop_child(child)
        self.output = out + err
        self.assertEqual(code, child.returncode, self.output)
        if expected is not None: self.assertIn('Status: ' + expected, out, self.output)
        else: self.assertNotIn('QA: GREEN', out, self.output)
        attempt = self.store._manifest(self.run)['attempts'][-1 if runtime else 0]
        self.attempt = attempt['attempt_id']
        self.assertEqual([0, 1, 1] if expected == 'RED' else [0, 0],
                         [gate['normalized_exit'] for gate in attempt['gates'] if gate['kind'] == 'gate'])
        self.assertEqual(self.head, self.git('rev-parse', 'HEAD'))
        self.assertEqual('', self.git('status', '--porcelain', '-uall'))
        self.assertEqual(1, self.git('worktree', 'list', '--porcelain').count('worktree '))
        if not leftover: self.assertFalse(list((self.repo / '.git/bureau/changed-tests').glob('run-*')))
        self.assertEqual(self.shared, (self.repo / 'shared/available').read_bytes())
        if code != 25 and not held: self.assertFalse(c.p.pg.process_holds(self.repo / '.git/bureau'))
        return attempt.get('changed_tests'), (child.returncode, expected)

    def brief(self, renderer=a):
        store = self.store if renderer is a else renderer.e.Store(self.repo / '.git/bureau')
        return renderer.render(store, self.repo, self.issue, self.pr, self.head,
                               'https://example.invalid/repo/pull/' + str(self.pr), base=self.base)

    def check_case(self, source, expected, path='tests/test_change.py', interrupt=False):
        self.publish({path:source})
        record, outcome = self.qa(interrupt=interrupt)
        self.assertIsNotNone(record)
        self.assertEqual((self.base, self.head, e.text_digest(self.config['repo']['changed_test_command'])),
                         (record['base'], record['head'], record['command_sha256']))
        self.assertEqual([(path, expected)], [(row['path'], row['class']) for row in record['files']], record)
        e.changed_tests_shape(record)
        brief = self.brief()
        if expected != 'proves the change': self.assertIn(': ' + expected, brief)
        else: self.assertIn('proves the change=1', brief)
        self.assertLess(brief.index('**Test evidence**'), brief.index('**Fail-before-pass**'))
        self.assertLess(brief.index('**Fail-before-pass**'), brief.index('**Review evidence**'))
        disabled, legacy_outcome = self.qa(enabled=False)
        self.assertIsNone(disabled); self.assertEqual(outcome, legacy_outcome)
        self.assertNotIn('**Fail-before-pass**', self.brief())
        return record

    def test_regression_proves_the_change(self):
        record = self.check_case(CASE.format(assertion='self.assertEqual(VALUE, 1)'), 'proves the change')
        self.assertEqual('assertion failure', record['files'][0]['base_result']['status'])

    def test_passing_base_proves_nothing_and_is_listed(self):
        self.check_case(CASE.format(assertion='self.assertIn(VALUE, (0, 1))'), 'does not prove the change')

    def test_new_import_is_not_runnable_on_base(self):
        source = CASE.format(assertion='self.assertEqual(VALUE, 1)').replace('import unittest', 'import unittest\nimport new_module')
        self.check_case(source, 'not runnable on base')

    def test_linked_inputs_cannot_write_into_the_real_checkout(self):
        self.config['repo']['worktree_links'] = ['shared']
        self.write('.bureau.json', json.dumps(self.config)); self.git('add', '.bureau.json')
        self.git('commit', '-qm', 'test: configure linked inputs')
        self.base = self.git('rev-parse', 'HEAD'); self.set_base(True)
        source = CASE.format(assertion='self.assertEqual(VALUE, 1)').replace('class Case', '''if VALUE == 0:
    Path('shared/available').write_text('must not reach the real checkout')

class Case''')
        record = self.check_case(source, 'not measured')
        self.assertEqual('repository uses worktree links', record['files'][0]['reason'])
        self.assertEqual(self.shared, (self.repo / 'shared/available').read_bytes())

    def test_linked_inputs_cannot_manufacture_assertion_proof(self):
        base_product = "from pathlib import Path\nVALUE = 0\ndef available(): return Path('shared/available').exists()\n"
        self.write('product.py', base_product)
        self.config['repo']['worktree_links'] = ['shared']
        self.write('.bureau.json', json.dumps(self.config)); self.git('add', 'product.py', '.bureau.json')
        self.git('commit', '-qm', 'test: linked dependency on base')
        self.base = self.git('rev-parse', 'HEAD'); self.set_base(True)
        self.write('product.py', 'VALUE = 1\ndef available(): return True\n')
        source = CASE.format(assertion='self.assertTrue(available())').replace('import VALUE', 'import VALUE, available')
        self.publish({'tests/test_change.py':source, 'tests/test_other.py':CASE.format(assertion='self.assertEqual(VALUE, 1)')})
        for product in (base_product, (self.repo / 'product.py').read_text()):
            subprocess.run([sys.executable, '-c', product + '\nassert available()'], cwd=self.repo,
                           env=self.env, check=True, timeout=5)
        record, outcome = self.qa()
        self.assertEqual('repository uses worktree links', record['reason'])
        self.assertEqual(['not measured', 'not measured'], [row['class'] for row in record['files']])
        self.assertTrue(all(row['reason'] == record['reason'] for row in record['files']))
        self.assertTrue(all(row['base_result']['status'] == row['head_result']['status'] == 'not measured'
                            for row in record['files']))
        self.assertIn('repository uses worktree links', self.brief())
        before = self.state()
        with patch.dict(os.environ, self.env), patch.object(c.p, 'outside_gate') as outside:
            c.measure(self.repo, self.base, self.config, TEMPLATE)
        outside.assert_not_called(); self.assertEqual(before, self.state())
        self.assertEqual((None, outcome), self.qa(enabled=False))

    def test_head_and_base_writes_stay_inside_their_trees(self):
        source = CASE.format(assertion="Path('shared').mkdir(exist_ok=True); Path('shared/available').write_text('local')")
        self.check_case(source, 'does not prove the change')
        self.assertEqual(self.shared, (self.repo / 'shared/available').read_bytes())

    def test_fails_on_both(self):
        self.check_case(CASE.format(assertion='self.assertEqual(VALUE, 2)'), 'fails on both')

    def test_hostile_filename_is_one_literal_argument(self):
        sentinel = self.root / 'unexpected'
        self.env['HOSTILE_SENTINEL'] = str(sentinel)
        path = "-odd'$(touch \"$HOSTILE_SENTINEL\").test.py"
        source = CASE.format(assertion="self.assertEqual(VALUE, 1); self.assertNotIn('LINEAR_API_KEY', __import__('os').environ)")
        record = self.check_case(source, 'proves the change', path=path)
        self.assertFalse(sentinel.exists())
        self.assertEqual(path, record['files'][0]['path'])

    def test_sigterm_during_base_run_cleans_up_and_preserves_qa(self):
        source = PUBLISH_READY + CASE.format(assertion='self.assertEqual(VALUE, 1)').replace('class Case', '''if VALUE == 0:
    import json, os, time
    publish_ready(Path(os.environ['CHANGED_READY']), json.dumps(dict(parent=os.getppid(), tree=str(Path.cwd()))))
    while True: time.sleep(1)

class Case''')
        record = self.check_case(source, 'not measured', interrupt=True)
        self.assertEqual('interrupted', record['files'][0]['reason'])
        self.assertFalse(self.interrupted_tree.exists())
        self.git('worktree', 'prune')
        self.assertNotIn(str(self.interrupted_tree), self.git('worktree', 'list', '--porcelain'))

    def test_uncertain_gate_stops_qa_and_preserves_mandatory_ownership(self):
        injection = '''def uncertain(options, tree, command, log):
    hold = p.pg.ProcessHold(p.evidence_store(tree).root.parent.parent, 'outside_gate', tree)
    hold.close(stopped=False)
    raise p.GateExecutionError(p.pg.TerminationUncertain('fixture'), (0, None), uncertain=True)
p.outside_gate = uncertain
'''
        self.check_retained_gate(injection)

    def test_failed_hold_removal_stops_qa_after_confirmed_termination(self):
        injection = '''close = p.pg.ProcessHold.close
def refuse_removal(self, stopped=False):
    if stopped:
        self.handle.close()
        raise PermissionError('fixture refuses hold removal')
    return close(self, stopped=stopped)
p.pg.ProcessHold.close = refuse_removal
outside = p.outside_gate
def record_failure(*args):
    try: return outside(*args)
    except p.GateExecutionError as exc:
        Path(os.environ['CHANGED_FAILURE']).write_text(json.dumps(dict(uncertain=exc.uncertain, code=exc.code)))
        raise
p.outside_gate = record_failure
'''
        failure = self.root / 'failure'
        self.env['CHANGED_FAILURE'] = str(failure)
        self.check_retained_gate(injection)
        self.assertEqual(dict(uncertain=False, code=22), json.loads(failure.read_text()))

    def test_retained_private_runtime_ownership_stops_qa_without_an_existing_hold(self):
        injection = '''def unavailable(options, tree, command, log):
    root = p.evidence_store(tree).root.parent.parent
    (root / 'leases.json').write_text(json.dumps({'workspace:' + str(tree): dict(run_id=os.environ['BUREAU_RUN_ID'])}))
    raise p.GateExecutionError(OSError('fixture'), (0, None))
p.outside_gate = unavailable
'''
        self.check_retained_gate(injection)
        self.assertTrue(list((self.repo / '.git/bureau/changed-tests').glob('run-*/git/bureau/leases.json')))

    def check_retained_gate(self, injection):
        self.publish({'tests/test_change.py':CASE.format(assertion='self.assertEqual(VALUE, 1)'),
                      'tests/test_later.py':CASE.format(assertion='self.assertEqual(VALUE, 1)')})
        script = self.scripts / 'bureau-changed-tests.py'
        script.write_text(script.read_text().replace("if __name__ == '__main__':", injection + "\nif __name__ == '__main__':"))
        record, _ = self.qa(runtime=True, expected=None, code=25, leftover=True)
        self.assertEqual('outside gate termination uncertain', record['reason'])
        self.assertEqual(['not measured', 'not measured'], [row['class'] for row in record['files']])
        self.assertTrue(all(row['base_result']['status'] == 'not measured' for row in record['files']))
        self.assertIn('human_hold', self.output)
        root = self.repo / '.git/bureau'
        holds = c.p.pg.process_holds(root); self.assertEqual(1, len(holds))
        runtime = module(self.scripts, 'bureau-runtime'); store = runtime.Store(self.repo)
        with self.assertRaises(runtime.Conflict): store.release(self.run)
        self.assertTrue(json.loads(store.leases.read_text()))
        # The injected failures leave no writer; explicitly clear their holds.
        for hold in holds: hold.unlink()
        store.release(self.run)
        self.assertEqual({}, json.loads(store.leases.read_text()))

    def test_certain_execution_failure_stays_advisory(self):
        self.publish({'tests/test_change.py':CASE.format(assertion='self.assertEqual(VALUE, 1)')})
        script = self.scripts / 'bureau-changed-tests.py'
        injection = '''def unavailable(*args):
    raise p.GateExecutionError(OSError('fixture'), (0, None))
p.outside_gate = unavailable
'''
        script.write_text(script.read_text().replace("if __name__ == '__main__':", injection + "\nif __name__ == '__main__':"))
        record, outcome = self.qa(runtime=True)
        self.assertEqual('execution unavailable', record['files'][0]['reason'])
        self.assertEqual({}, json.loads((self.repo / '.git/bureau/leases.json').read_text()))
        self.assertEqual((None, outcome), self.qa(enabled=False, runtime=True))

    def test_gate_errors_check_all_owned_runtime_state_and_preserve_stop_codes(self):
        self.publish({'tests/test_change.py':CASE.format(assertion='self.assertEqual(VALUE, 1)'),
                      'tests/test_later.py':CASE.format(assertion='self.assertEqual(VALUE, 1)')})
        self.begin()
        root = self.repo / '.git/bureau'
        for state, outcome in (('private lease', (22, None)), ('private process', (124, 'timeout')),
                               ('parent lease', (130, 'kill')), ('unreadable ownership', (22, None))):
            with self.subTest(state=state):
                def unavailable(options, tree, command, log):
                    control = root if state == 'parent lease' else c.p.evidence_store(tree).root.parent.parent
                    if state == 'private process':
                        path = control / 'processes' / (self.run + '.json')
                        path.parent.mkdir()
                        path.write_text(json.dumps(dict(groups=[], interrupted=True)))
                    else:
                        path = control / 'leases.json'
                        path.write_text('{' if state == 'unreadable ownership' else
                                        json.dumps({'workspace:' + str(tree): dict(run_id=self.run)}))
                    if state == 'private lease': raise RuntimeError('unwrapped execution failure')
                    raise c.p.GateExecutionError(OSError('fixture'), outcome)
                with patch.dict(os.environ, self.env), patch.object(c.p, 'outside_gate', side_effect=unavailable) as outside:
                    with self.assertRaises(c.p.GateExecutionError) as failed:
                        c.measure(self.repo, self.base, self.config, TEMPLATE)
                outside.assert_called_once()
                self.assertTrue(failed.exception.uncertain)
                self.assertEqual((c.p.pg.uncertain_exit(outcome[0]), outcome[1]),
                                 (failed.exception.code, failed.exception.abort))
                record = failed.exception.record; e.changed_tests_shape(record)
                self.assertTrue(all(row['class'] == 'not measured' for row in record['files']))
                holds = c.p.pg.process_holds(root, run=self.run); self.assertTrue(holds)
                for hold in holds: hold.unlink()
                (root / 'leases.json').unlink(missing_ok=True)
                c.sweep(root / 'changed-tests', time.monotonic() + c.CLEANUP_SECONDS)
                self.assertFalse(list((root / 'changed-tests').glob('run-*')))

    def test_runtime_cancellation_deregisters_before_slow_process_drain(self):
        source = PUBLISH_READY + CASE.format(assertion='self.assertEqual(VALUE, 1)').replace('class Case', '''if VALUE == 0:
    import json, os, time
    publish_ready(Path(os.environ['CHANGED_READY']), json.dumps(dict(parent=os.getppid(), tree=str(Path.cwd()))))
    while True: time.sleep(1)

class Case''')
        self.publish({'tests/test_change.py':source})
        draining = self.root / 'draining'
        self.env.update(BUREAU_STOP_GRACE_SECONDS='1', BUREAU_RUN_DEPTH='3', CHANGED_DRAINING=str(draining))
        helper = self.scripts / 'bureau-changed-tests.py'
        injection = PUBLISH_READY + '''outside = p.outside_gate
def slow_drain(options, *args):
    confirm = p.pg.assert_groups_stopped
    def delayed(groups):
        if options.get('_gate_cancelled'):
            publish_ready(Path(os.environ['CHANGED_DRAINING']), json.dumps(list(groups)))
            while True: time.sleep(1)
        return confirm(groups)
    p.pg.assert_groups_stopped = delayed
    try: return outside(options, *args)
    finally: p.pg.assert_groups_stopped = confirm
p.outside_gate = slow_drain
'''
        helper.write_text(helper.read_text().replace("if __name__ == '__main__':", injection + "\nif __name__ == '__main__':"))
        self.qa(runtime=True, interrupt=True, expected=None, code=130, leftover=True, held=True, interrupt_wait=draining)
        self.assertTrue(draining.exists(), 'termination confirmation never reached its slow interval')
        leftovers = list((self.repo / '.git/bureau/changed-tests').glob('run-*'))
        self.assertTrue(leftovers)
        for directory in leftovers:
            for name in ('head', 'base'): self.assertFalse((directory / name / '.git').exists())
            self.assertFalse(list((directory / 'git/worktrees').glob('*')))
        c.p.pg.assert_groups_stopped(set(json.loads(draining.read_text())))
        holds = c.p.pg.process_holds(self.repo / '.git/bureau'); self.assertTrue(holds)
        for hold in holds: hold.unlink()
        self.publish({'tests/test_change.py':CASE.format(assertion='self.assertEqual(VALUE, 1)')})
        with patch.dict(os.environ, self.env):
            record = c.measure(self.repo, self.base, self.config, TEMPLATE)
        self.assertEqual('proves the change', record['files'][0]['class'], record)
        self.assertFalse(list((self.repo / '.git/bureau/changed-tests').glob('run-*')))
        module(self.scripts, 'bureau-runtime').Store(self.repo).release(self.run)

    def test_runtime_cancellation_deregisters_before_slow_cleanup_and_sweeps(self):
        source = PUBLISH_READY + CASE.format(assertion='self.assertEqual(VALUE, 1)').replace('class Case', '''if VALUE == 0:
    import json, os, time
    publish_ready(Path(os.environ['CHANGED_READY']), json.dumps(dict(parent=os.getppid(), tree=str(Path.cwd()))))
    while True: time.sleep(1)

class Case''')
        self.publish({'tests/test_change.py':source})
        cleanup = self.root / 'cleanup-ready'
        self.env.update(BUREAU_STOP_GRACE_SECONDS='1', BUREAU_RUN_DEPTH='3', CHANGED_CLEANUP=str(cleanup))
        helper = self.scripts / 'bureau-changed-tests.py'
        original = helper.read_text()
        injection = PUBLISH_READY + '''remove = remove_directory
def paused_cleanup(directory, timeout):
    publish_ready(Path(os.environ['CHANGED_CLEANUP']), 'ready')
    return remove(directory, timeout)
remove_directory = paused_cleanup
'''
        helper.write_text(original.replace('import shutil, sys; shutil.rmtree',
                                          'import signal; signal.pause(); import shutil, sys; shutil.rmtree').replace(
                                              "if __name__ == '__main__':", injection + "\nif __name__ == '__main__':"))
        # Keep QA's shell alive past its runtime grace, after helper cleanup.
        pipeline = self.scripts / 'qa-pipeline.sh'
        pipeline.write_text(pipeline.read_text().replace('bureau_archive_evidence_dir_log() {',
                                                       'bureau_archive_evidence_dir_log() {\n  while :; do sleep 1; done'))
        record, _ = self.qa(runtime=True, interrupt=True, expected=None, code=130, leftover=True, interrupt_wait=cleanup)
        leftovers = list((self.repo / '.git/bureau/changed-tests').glob('run-*'))
        self.assertTrue(leftovers, self.output)
        for directory in leftovers:
            self.assertFalse((directory / 'base/.git').exists())
            self.assertFalse(list((directory / 'git/worktrees').glob('*')))
        helper.write_text(original)
        self.publish({'tests/test_change.py':CASE.format(assertion='self.assertEqual(VALUE, 1)')})
        with patch.dict(os.environ, self.env):
            measured = c.measure(self.repo, self.base, self.config, TEMPLATE)
        self.assertEqual('proves the change', measured['files'][0]['class'], measured)
        self.assertFalse(list((self.repo / '.git/bureau/changed-tests').glob('run-*')))
        module(self.scripts, 'bureau-runtime').Store(self.repo).release(self.run)

    def test_disabled_fixture_and_brief_match_golden(self):
        self.publish({'tests/test_change.py':CASE.format(assertion='self.assertEqual(VALUE, 1)')})
        record, outcome = self.qa(enabled=False)
        self.assertIsNone(record); expected = self.brief()
        expected = expected.replace(self.head, '{head}').replace(self.base, '{base}')
        expected = expected.replace(self.run, '{run}').replace(self.attempt, '{attempt}').replace(self.issue, '{issue}')
        for gate in self.store._manifest(self.run)['attempts'][0]['gates']:
            expected = expected.replace(gate['sha256'], '{gate-sha256}')
        self.assertEqual((FIXTURES / 'disabled-brief.md').read_text(), expected)
        calls = (self.repo / 'gh_calls.log').read_text().splitlines()
        self.assertEqual(json.loads((FIXTURES / 'disabled-requests.json').read_text()), calls)
        self.assertEqual((0, 'GREEN'), outcome)

    def test_missing_base_configuration_stays_silent_and_disabled(self):
        self.disabled_base = self.git('rev-parse', self.disabled_base + '^')
        self.publish({'tests/test_change.py':CASE.format(assertion='self.assertEqual(VALUE, 1)')})
        record, outcome = self.qa(enabled=False)
        self.assertIsNone(record); self.assertEqual((0, 'GREEN'), outcome)
        self.assertNotIn('Fail-before-pass', self.output)
        self.assertEqual(json.loads((FIXTURES / 'disabled-requests.json').read_text()),
                         (self.repo / 'gh_calls.log').read_text().splitlines())

    def test_old_manifest_format_stays_readable_and_updateable(self):
        self.publish({'tests/test_change.py':CASE.format(assertion='self.assertIn(VALUE, (0, 1))')})
        record, _ = self.qa()
        legacy = json.loads((FIXTURES / 'manifest.json').read_text())
        path = self.store.manifest_path(self.run)
        disk = json.loads(path.read_text())
        self.assertEqual(set(legacy), set(disk))
        self.assertEqual(set(legacy['attempts'][0]), set(disk['attempts'][0]))
        self.assertEqual(set(legacy['attempts'][0]['gates'][0]), set(disk['attempts'][0]['gates'][0]))
        # An old-format writer changes the manifest without touching sidecars.
        disk['attempts'][0]['gates'].append(copy.deepcopy(disk['attempts'][0]['gates'][0]))
        path.write_text(json.dumps(disk))
        attempt = self.store._manifest(self.run)['attempts'][0]
        self.assertEqual(3, len([row for row in attempt['gates'] if row['kind'] == 'gate']))
        self.assertEqual(record, attempt['changed_tests'])
        self.assertIn('does not prove the change=1', self.brief())
        path = self.store.manifest_path(self.run).parent / 'changed-tests' / (self.attempt + '.json')
        broken = copy.deepcopy(record); broken['files'][0]['class'] = 'proves the change'
        path.write_text(json.dumps(broken))
        attempt = self.store._manifest(self.run)['attempts'][0]
        self.assertNotIn('changed_tests', attempt)
        self.assertEqual(3, len([row for row in attempt['gates'] if row['kind'] == 'gate']))
        self.assertFalse(list((self.store.root / 'quarantine').glob('*.json')))

    def test_legacy_store_needs_no_optional_record(self):
        legacy = json.loads((FIXTURES / 'manifest.json').read_text())
        path = self.store.manifest_path(legacy['run_id']); path.parent.mkdir(parents=True)
        path.write_text(json.dumps(legacy))
        self.assertEqual(legacy, self.store._manifest(legacy['run_id']))

    def test_disabled_setting_does_not_refresh_the_base_after_the_gate(self):
        self.env['CHANGED_ADVANCE_BASE'] = self.base
        source = '''from pathlib import Path
import os, subprocess, unittest
class Gate(unittest.TestCase):
    def test_gate(self):
        count = Path('logs/gate-count')
        number = int(count.read_text()) + 1 if count.exists() else 1
        count.write_text(str(number))
        if number == 2:
            subprocess.run(['git', '--git-dir', '.fake-origin.git', 'update-ref', 'refs/heads/main',
                            os.environ['CHANGED_ADVANCE_BASE']], check=True, timeout=5)
'''
        self.publish({'test_gate.py':source})
        record, _ = self.qa(enabled=False)
        self.assertIsNone(record)
        self.assertEqual(self.disabled_base, self.git('rev-parse', 'origin/main'))

    def test_failed_final_gate_skips_measurement_and_keeps_qa_red(self):
        source = '''from pathlib import Path
import unittest
class Gate(unittest.TestCase):
    def test_gate(self):
        count = Path('logs/gate-count')
        number = int(count.read_text()) + 1 if count.exists() else 1
        count.write_text(str(number))
        self.assertEqual(number, 1)
'''
        self.publish({'test_gate.py':source, 'tests/test_change.py':CASE.format(assertion='self.assertEqual(VALUE, 1)')})
        record, enabled = self.qa(expected='RED')
        self.assertIsNone(record)
        (self.repo / 'logs/gate-count').unlink()
        record, disabled = self.qa(enabled=False, expected='RED')
        self.assertIsNone(record); self.assertEqual(enabled, disabled)

    def test_cap_order_and_brief_bound(self):
        self.publish({f'tests/test_{number:02d}.py':CASE.format(assertion='self.assertIn(VALUE, (0, 1))') for number in range(23)})
        record, _ = self.qa()
        self.assertEqual(3, record['omitted']); self.assertEqual(20, len(record['files']))
        self.assertEqual([f'tests/test_{number:02d}.py' for number in range(20)], [row['path'] for row in record['files']])
        block = self.brief().split('**Fail-before-pass**')[1].split('**Review evidence**')[0]
        self.assertEqual(8, block.count(': does not prove the change')); self.assertIn('12 more', block)

    def test_modified_deleted_renamed_and_custom_patterns(self):
        for path in ('checks/changed.py', 'checks/deleted.py', 'checks/old.py'):
            self.write(path, CASE.format(assertion='self.assertIn(VALUE, (0, 1))'))
        self.config['agents'] = dict(risk_tier=dict(test_patterns=['checks/*']))
        self.write('.bureau.json', json.dumps(self.config))
        self.git('add', '.'); self.git('commit', '-qm', 'test: existing tests and patterns')
        self.base = self.git('rev-parse', 'HEAD'); self.set_base(True)
        (self.repo / 'checks/deleted.py').unlink()
        (self.repo / 'checks/old.py').rename(self.repo / 'checks/new.py')
        self.publish({'checks/changed.py':CASE.format(assertion='self.assertEqual(VALUE, 1)'),
                      'tests/test_ignored.py':CASE.format(assertion='self.assertEqual(VALUE, 2)')})
        record, _ = self.qa()
        self.assertEqual(['checks/changed.py', 'checks/new.py'], [row['path'] for row in record['files']])

    def test_invalid_template_cannot_prove_and_keeps_qa_green(self):
        self.config['repo']['changed_test_command'] = 'python3 "{file}"'
        self.write('.bureau.json', json.dumps(self.config)); self.git('add', '.bureau.json')
        self.git('commit', '-qm', 'test: invalid measurement command')
        self.base = self.git('rev-parse', 'HEAD'); self.set_base(True)
        record = self.check_case(CASE.format(assertion='self.assertEqual(VALUE, 1)'), 'not measured')
        self.assertEqual('invalid command template', record['files'][0]['reason'])

    def test_worktree_failure_is_only_unmeasured(self):
        self.publish({'tests/test_change.py':CASE.format(assertion='self.assertEqual(VALUE, 1)')})
        self.begin(); before = self.state()
        real = subprocess.check_output
        with patch.dict(os.environ, self.env), patch.object(tempfile, 'tempdir', str(self.root)), \
                patch.object(c.subprocess, 'check_output', wraps=real) as git:
            def run(command, **kwargs):
                if 'worktree' in command and 'add' in command: raise OSError('fixture refuses worktree')
                return real(command, **kwargs)
            git.side_effect = run
            record = c.measure(self.repo, self.base, self.config, TEMPLATE)
        self.assertEqual('not measured', record['files'][0]['class']); self.assertEqual(before, self.state())

    def test_measurement_preserves_dirty_index_head_and_untracked_files(self):
        assertion = (f'self.assertFalse(set({GIT_CONTEXT!r}) & os.environ.keys()); '
                     "self.assertEqual(subprocess.check_output(['git', 'show', 'HEAD:product.py'], text=True).strip(), "
                     "'VALUE = ' + str(VALUE)); self.assertEqual(VALUE, 1); Path('test-output').write_text('temporary')")
        source = CASE.format(assertion=assertion).replace('import unittest', 'import unittest\nimport os, subprocess')
        self.publish({'tests/test_change.py':source})
        self.write('tests/test_change.py', source + '# staged test\n'); self.git('add', 'tests/test_change.py')
        self.write('tests/test_change.py', source + '# unstaged test\n')
        self.write('product.py', 'VALUE = 99\n'); self.git('add', 'product.py')
        self.write('product.py', 'VALUE = 100\n'); self.write('untracked', 'preserve\n')
        before = self.state(); contents = self.contents(); context = self.git_environment()
        for inherited in ({}, *({name:context[name]} for name in ('GIT_INDEX_FILE', 'GIT_DIR', 'GIT_WORK_TREE')), context):
            with self.subTest(context=sorted(inherited)), patch.dict(os.environ, {**self.env, **inherited}, clear=True):
                environment = dict(os.environ)
                record = c.measure(self.repo, self.base, self.config, TEMPLATE)
                self.assertEqual(environment, dict(os.environ))
                self.assertEqual(contents, self.contents())
                self.assertEqual('proves the change', record['files'][0]['class'], record)
                self.assertEqual(before, self.state())
                self.assertFalse(list((self.repo / '.git/bureau/changed-tests').glob('run-*')))

    def test_helper_clears_git_context_for_preparation_execution_and_cleanup(self):
        self.publish({'tests/test_change.py':CASE.format(assertion='self.assertEqual(VALUE, 1)')})
        self.begin(); before = self.state(); contents = self.contents()
        original_output, original_run = subprocess.check_output, subprocess.run
        calls = []
        def checked(original, command, **kwargs):
            if command[0] == 'git':
                self.assertFalse(set(GIT_CONTEXT) & kwargs.get('env', os.environ).keys())
                calls.append(command)
            return original(command, **kwargs)
        with patch.dict(os.environ, {**self.env, **self.git_environment()}, clear=True), \
                patch.object(sys, 'argv', ['changed-tests', '--repo', str(self.repo), '--base', self.base]), \
                patch.object(subprocess, 'check_output', side_effect=lambda command, **kw: checked(original_output, command, **kw)), \
                patch.object(subprocess, 'run', side_effect=lambda command, **kw: checked(original_run, command, **kw)):
            environment = dict(os.environ)
            self.assertEqual(0, c.main())
            self.assertEqual(environment, dict(os.environ))
        self.assertEqual(contents, self.contents()); self.assertEqual(before, self.state())
        self.assertTrue(any('show' in call for call in calls))
        self.assertTrue(any('checkout' in call for call in calls))
        self.assertTrue(any('prune' in call for call in calls))
        record = self.store._manifest(self.run)['attempts'][0]['changed_tests']
        self.assertEqual('proves the change', record['files'][0]['class'], record)

    def test_quoted_and_embedded_placeholders_are_invalid(self):
        for template in ('bash "{file}"', 'bash {file} {file}', 'bash test.py',
                         'FOO="prefix {file} suffix" bash test.py', 'bash ${x:- {file} }'):
            with self.subTest(template=template), self.assertRaises(ValueError):
                c.file_command(template, "-odd'$(touch unexpected).test.py")

    def test_budget_exhaustion_never_proves_and_keeps_qa_green(self):
        script = self.scripts / 'bureau-changed-tests.py'
        injection = '''outside = p.outside_gate
def expire_base(options, tree, *args):
    if tree.name == 'base': options['timeout'] = 1
    return outside(options, tree, *args)
p.outside_gate = expire_base
'''
        script.write_text(script.read_text().replace("if __name__ == '__main__':", injection + "\nif __name__ == '__main__':"))
        source = CASE.format(assertion='self.assertEqual(VALUE, 1)').replace('class Case',
                    'if VALUE == 0:\n    import time\n    while True: time.sleep(1)\n\nclass Case')
        record = self.check_case(source, 'not measured')
        self.assertEqual('budget exhausted', record['files'][0]['reason'])

    def test_slow_cleanup_keeps_completed_results(self):
        self.publish({'tests/test_change.py':CASE.format(assertion='self.assertEqual(VALUE, 1)')})
        before = self.state(); run = subprocess.run; operations = []
        def delayed(command, **kwargs):
            if 'worktree' in command and 'prune' in command:
                operations.append('prune')
                command = [sys.executable, '-I', '-c',
                           'import subprocess, sys, time; time.sleep(1); subprocess.run(sys.argv[1:], check=True)',
                           *command]
            elif command[:3] == [sys.executable, '-I', '-c'] and 'shutil.rmtree' in command[3]:
                operations.append('delete')
                directory = Path(command[-1])
                for name in ('head', 'base'): self.assertFalse((directory / name / '.git').exists())
                self.assertFalse(list((directory / 'git/worktrees').glob('*')))
                command = [*command[:3], 'import time; time.sleep(1); ' + command[3], *command[4:]]
            else: return run(command, **kwargs)
            self.assertEqual(120, kwargs['timeout'])
            return run(command, **kwargs)
        with patch.dict(os.environ, self.env), patch.object(subprocess, 'run', side_effect=delayed):
            record = c.measure(self.repo, self.base, self.config, TEMPLATE)
        self.assertEqual(['prune', 'delete'], operations)
        self.assertIsNone(record['reason']); self.assertEqual('proves the change', record['files'][0]['class'])
        e.changed_tests_shape(record)
        self.assertEqual(before, self.state())
        self.assertFalse(list((self.repo / '.git/bureau/changed-tests').glob('run-*')))

    def test_cleanup_failure_keeps_results_and_next_start_sweeps(self):
        self.publish({'tests/test_change.py':CASE.format(assertion='self.assertEqual(VALUE, 1)')})
        self.begin(); before = self.state(); parent = self.repo / '.git/bureau/changed-tests'
        run, monotonic, outside = subprocess.run, time.monotonic, c.p.outside_gate
        for operation in ('delete', 'prune'):
            for failure in ('timeout', 'error'):
                with self.subTest(operation=operation, failure=failure):
                    elapsed = [0]; failures = []
                    def unavailable(command, **kwargs):
                        pruning = 'worktree' in command and 'prune' in command
                        deleting = command[:3] == [sys.executable, '-I', '-c'] and 'shutil.rmtree' in command[3]
                        if pruning and operation == 'prune' or deleting and operation == 'delete':
                            failures.append(command)
                            self.assertEqual(120, kwargs['timeout'])
                            if failure == 'timeout':
                                elapsed[0] += kwargs['timeout'] + 1
                                raise subprocess.TimeoutExpired(command, kwargs['timeout'])
                            raise PermissionError('fixture refuses cleanup')
                        return run(command, **kwargs)
                    with patch.dict(os.environ, self.env), patch.object(subprocess, 'run', side_effect=unavailable), \
                            patch.object(c.time, 'monotonic', side_effect=lambda: monotonic() + elapsed[0]):
                        record = c.measure(self.repo, self.base, self.config, TEMPLATE)
                    self.assertEqual(1, len(failures))
                    self.assertEqual('cleanup failed', record['reason'])
                    row = record['files'][0]
                    self.assertEqual('proves the change', row['class']); self.assertIsNone(row['reason'])
                    self.assertEqual(dict(status='assertion failure', normalized_exit=1), row['base_result'])
                    self.assertEqual(dict(status='passed', normalized_exit=0), row['head_result'])
                    e.changed_tests_shape(record)
                    wrong = copy.deepcopy(record); wrong['reason'] = 'budget exhausted'
                    with self.assertRaises(ValueError): e.changed_tests_shape(wrong)
                    self.store.changed_tests(self.run, self.attempt, record)
                    self.assertEqual(record, self.store._manifest(self.run)['attempts'][0]['changed_tests'])
                    brief = self.brief()
                    self.assertIn('proves the change=1', brief)
                    self.assertIn('Cleanup incomplete: temporary directory retained for the next check.', brief)
                    self.assertNotIn('Not measured: cleanup failed', brief)
                    leftovers = list(parent.glob('run-*')); self.assertEqual(1, len(leftovers))
                    for name in ('head', 'base'): self.assertFalse((leftovers[0] / name / '.git').exists())
                    if operation == 'delete': self.assertFalse(list((leftovers[0] / 'git/worktrees').glob('*')))
                    self.assertEqual(before, self.state())
                    def swept(*args):
                        self.assertFalse(leftovers[0].exists(), 'the next start did not sweep its predecessor')
                        return outside(*args)
                    with patch.dict(os.environ, self.env), patch.object(c.p, 'outside_gate', side_effect=swept) as gate:
                        recovered = c.measure(self.repo, self.base, self.config, TEMPLATE)
                    self.assertEqual(2, gate.call_count)
                    self.assertIsNone(recovered['reason']); self.assertEqual(row, recovered['files'][0])
                    c.sweep(parent, time.monotonic() + c.CLEANUP_SECONDS)
                    self.assertFalse(list(parent.glob('run-*')))
                    self.assertEqual(before, self.state())

    def test_preparation_uses_the_measurement_budget(self):
        self.publish({'tests/test_change.py':CASE.format(assertion='self.assertEqual(VALUE, 1)')})
        self.begin(); before = self.state()
        self.env['BUREAU_QA_TEST_TIMEOUT'] = '1'
        with patch.dict(os.environ, self.env), patch.object(c.p, 'outside_gate') as outside:
            record = c.measure(self.repo, self.base, self.config, TEMPLATE,
                               started=time.monotonic() - 1, head=self.head)
        self.assertEqual('budget exhausted', record['reason']); outside.assert_not_called()
        self.assertEqual(before, self.state())
        self.env['BUREAU_QA_TEST_TIMEOUT'] = '120'
        output = subprocess.check_output; timeouts = []
        def budgeted(command, **kwargs):
            if command[0] == 'git' and 'timeout' in kwargs: timeouts.append(kwargs['timeout'])
            return output(command, **kwargs)
        with patch.dict(os.environ, self.env), patch.object(subprocess, 'check_output', side_effect=budgeted):
            control = c.measure(self.repo, self.base, self.config, TEMPLATE)
        self.assertEqual('proves the change', control['files'][0]['class'], control)
        self.assertTrue(timeouts); self.assertTrue(all(10 < timeout <= 120 for timeout in timeouts), timeouts)

    def test_preparation_timeout_is_advisory_and_disabled_makes_no_request(self):
        self.publish({'tests/test_change.py':CASE.format(assertion='self.assertEqual(VALUE, 1)')})
        ready = self.root / 'preparation-ready'
        self.env['PREPARATION_READY'] = str(ready)
        script = self.scripts / 'bureau-changed-tests.py'
        injection = PUBLISH_READY + '''output = subprocess.check_output
def expired(command, **kwargs):
    if command[:3] == ['gh', 'pr', 'list']:
        publish_ready(Path(os.environ['PREPARATION_READY']), json.dumps(dict(timeout=kwargs.get('timeout'))))
        raise subprocess.TimeoutExpired(command, kwargs.get('timeout'))
    return output(command, **kwargs)
subprocess.check_output = expired
'''
        script.write_text(script.read_text().replace("if __name__ == '__main__':", injection + "\nif __name__ == '__main__':"))
        record, outcome = self.qa()
        self.assertEqual('budget exhausted', record['reason'])
        timeout = json.loads(ready.read_text())['timeout']
        self.assertGreater(timeout, 10); self.assertLessEqual(timeout, 120)
        self.assertEqual([], record['files'])
        ready.unlink()
        self.assertEqual((None, outcome), self.qa(enabled=False))
        self.assertFalse(ready.exists(), 'disabled QA made the optional request')

    def test_enabled_measurement_uses_the_fetched_pr_base(self):
        alternate_config = copy.deepcopy(self.config)
        template = 'PYTHONPATH=. exec python3 -B {file}'
        alternate_config['repo']['changed_test_command'] = template
        self.write('.bureau.json', json.dumps(alternate_config))
        self.git('add', '.bureau.json'); self.git('commit', '-qm', 'test: alternate base configuration')
        alternate = self.git('rev-parse', 'HEAD')
        self.git('update-ref', 'refs/heads/alternate', alternate); self.git('push', '-q', 'origin', 'alternate')
        self.publish({'tests/test_change.py':CASE.format(assertion='self.assertEqual(VALUE, 1)')})
        self.wrap_gh('''if '--json' in sys.argv:
    fields = sys.argv[sys.argv.index('--json') + 1]
    if fields == 'number':
        print('7'); sys.exit(0)
    if fields == 'state,baseRefName,headRefName':
        import json
        print(json.dumps(dict(state='OPEN', baseRefName='alternate', headRefName=os.environ['BUREAU_STUB_BRANCH'])))
        sys.exit(0)
''')
        record, outcome = self.qa()
        self.assertEqual(alternate, record['base'])
        self.assertEqual(e.text_digest(template), record['command_sha256'])
        self.assertEqual('proves the change', record['files'][0]['class'])
        self.assertEqual((None, outcome), self.qa(enabled=False))

    def test_control_characters_are_rejected_without_execution(self):
        self.publish({'tests/test_unsafe\nname.py':'raise RuntimeError("must not run")\n',
                      'tests/test_unsafe\x01name.py':'raise RuntimeError("must not run")\n'})
        record, outcome = self.qa()
        self.assertEqual(['unsafe path', 'unsafe path'], [row['reason'] for row in record['files']])
        self.assertTrue(all(row['class'] == 'not measured' for row in record['files']))
        self.assertEqual((None, outcome), self.qa(enabled=False))

    def test_shape_rejects_false_proof_and_unknown_fields(self):
        self.publish({'tests/test_change.py':CASE.format(assertion='self.assertIn(VALUE, (0, 1))')})
        record, _ = self.qa()
        wrong = copy.deepcopy(record); wrong['files'][0]['class'] = 'proves the change'
        with self.assertRaises(ValueError): e.changed_tests_shape(wrong)
        wrong = copy.deepcopy(record); wrong['files'][0]['raw'] = 'output must not be retained'
        with self.assertRaises(ValueError): e.changed_tests_shape(wrong)


if __name__ == '__main__': unittest.main()
