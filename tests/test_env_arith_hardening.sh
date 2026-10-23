#!/bin/bash
# Consumer-side numeric guards: inherited environment, JSON and real arithmetic
# sites. tests/run.sh discovers test_*.sh automatically. An optional script tree
# argument runs the identical probes against a pristine snapshot as a control.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SCRIPTS="${1:-$ROOT/templates/scripts}"
python3 - "$SCRIPTS" <<'PY'
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile

scripts = Path(sys.argv[1]).resolve()
checks, failures, sentinels = 0, [], 0

def check(condition, label):
    global checks
    checks += 1
    if not condition:
        failures.append(label)
        print('FAIL ' + label)

def lines(file, pattern):
    if file == 'queue-loop-supervised.sh' and pattern in ('^MAX_CRASHES=', '^STABILITY_WINDOW='):
        key = 'BUREAU_SUPERVISOR_' + ('MAX_CRASHES' if pattern == '^MAX_CRASHES=' else 'STABILITY_WINDOW')
        text = (scripts / file).read_text()
        return re.search(r'^if \[ "\$\{' + key + r'\+x\}" = x \]; then\n.*?^fi$', text, re.M | re.S).group()
    found = [line for line in (scripts / file).read_text().splitlines()
             if re.search(pattern, line)]
    if not found:
        raise AssertionError('consumer missing: ' + file + ' / ' + pattern)
    return '\n'.join(found)

def function(name):
    text = (scripts / 'bureau-config.sh').read_text()
    match = re.search(r'^' + re.escape(name) + r'\(\) \{\n.*?^\}', text, re.M | re.S)
    return match.group() if match else ''

# These are production assignments and arithmetic sites, not copies of the
# validator or its expressions. No provider, Linear, network or sleep is needed.
consumers = [
    ('BUREAU_IMPL_MAX_ITER', 3, 1, 1000, 'implement-pipeline.sh', r'^MAX_ITER=',
     lines('implement-pipeline.sh', r'^for \(\( i=1;') + '\nbreak; done\nprintf "%s" "$MAX_ITER"'),
    ('BUREAU_IMPL_ITER_TIMEOUT', 1800, 1, 86400, 'implement-pipeline.sh', r'^ITER_TIMEOUT=',
     'THIS_TIMEOUT=$ITER_TIMEOUT; REMAINING=999999; [ "$THIS_TIMEOUT" -gt "$REMAINING" ] || :; printf "%s" "$THIS_TIMEOUT"'),
    ('BUREAU_IMPL_TOTAL_TIMEOUT', 5400, 1, 604800, 'implement-pipeline.sh', r'^TOTAL_TIMEOUT=',
     'ELAPSED=0\n' + lines('implement-pipeline.sh', r'^  REMAINING=') + '\nprintf "%s" "$REMAINING"'),
    ('BUREAU_POST_IMPLEMENT_TIMEOUT', 900, 1, 604800, 'implement-pipeline.sh', r'^  limit=|^  \[\[ "\$limit"',
     'TOTAL_TIMEOUT=604800; [ "$limit" -le "$TOTAL_TIMEOUT" ]; printf "%s" "$limit"'),
    ('BUREAU_REVIEW_MERGE_CAP_KB', 60, 1, 1024, 'code-review-pipeline.sh', r'^_rv_cap=',
     'printf "%s" "$((_rv_cap / 1024))"'),
    ('BUREAU_SUPERVISOR_MAX_CRASHES', 5, 1, 1000, 'queue-loop-supervised.sh', r'^MAX_CRASHES=',
     '[ 0 -ge "$MAX_CRASHES" ] || :; printf "%s" "$MAX_CRASHES"'),
    ('BUREAU_SUPERVISOR_STABILITY_WINDOW', 3600, 0, 604800, 'queue-loop-supervised.sh', r'^STABILITY_WINDOW=',
     '[ 1 -gt "$STABILITY_WINDOW" ] || :; printf "%s" "$STABILITY_WINDOW"'),
    ('BUREAU_MAX_CONCURRENT', 3, 1, 1000, 'orchestrate.sh', r'^MAX_CONCURRENT=',
     '[ 0 -ge "$MAX_CONCURRENT" ] || :; printf "%s" "$MAX_CONCURRENT"'),
]

with tempfile.TemporaryDirectory(prefix='bureau-test.arith.') as directory:
    temp = Path(directory)
    marker = temp / 'SENTINEL'
    config = temp / '.bureau.json'
    base = dict(PATH=os.environ['PATH'], HOME=directory, BUREAU_CONFIG=str(config),
                SENTINEL=str(marker), LC_ALL='C')
    config.write_text('{}')
    bootstrap = '_BUREAU_SCRIPTS_DIR=$1\nbureau_get() { jq -r "$1" "$BUREAU_CONFIG"; }\n' + function('_bureau_config_number') + '\n'

    def run(code, env=None):
        global sentinels
        marker.unlink(missing_ok=True)
        result = subprocess.run(['/bin/bash', '-uc', 'source "$1/bureau-env.sh"\na=(0); x=(0)\n' + bootstrap + code,
                                 '_', str(scripts)], env=dict(base, **(env or {})),
                                capture_output=True, text=True, timeout=10)
        if marker.exists():
            sentinels += 1
        check(not marker.exists(), 'arithmetic payload did not create SENTINEL')
        return result

    def cases(default, minimum, maximum):
        return [('a[$(touch "$SENTINEL")]', default, True),
                ('1+$(touch "$SENTINEL")', default, True),
                ('x[$(touch "$SENTINEL")]', default, True),
                ('0', 0 if minimum == 0 else default, minimum != 0),
                ('-1', default, True), ('010', 10, False),
                ('99999999999999999999', default, True), ('', default, True),
                (' ', default, True), (' 10', default, True), ('10 ', default, True),
                ('\t10', default, True), ('١٠', default, True), ('１０', default, True),
                ('0x10', default, True), ('1e3', default, True),
                (str(default), default, False), (str(maximum), maximum, False),
                (str(maximum + 1), default, True)]

    def verify(result, key, raw, expected, warn):
        check(result.returncode == 0 and result.stdout == str(expected),
              key + ': accepted/default decimal result for ' + repr(raw))
        warnings = result.stderr.splitlines()
        check(len(warnings) == (1 if warn else 0), key + ': exactly one warning when invalid')
        if warn:
            check(bool(warnings) and key in warnings[0], key + ': warning names the key')
            check(not any(token in result.stderr for token in ('touch', 'SENTINEL', '99999999999999999999')),
                  key + ': warning contains no rejected value')

    for key, default, minimum, maximum, file, pattern, arithmetic in consumers:
        code = lines(file, pattern) + '\n' + arithmetic
        for raw, expected, warn in cases(default, minimum, maximum):
            verify(run(code, {key: raw}), key, raw, expected, warn)
        verify(run(code), key, '<unset>', default, False)

    # A positional interval bypasses the config guard. Removing ONLY the
    # queue consumer guard must fail these probes at the real multiplication.
    queue = lines('queue-loop.sh', r'^INTERVAL_(MINUTES|SECONDS)=')
    for raw, expected, warn in cases(30, 1, 10080):
        # Empty $2 uses the configured default by the existing ${2:-...} rule.
        if raw == '': expected, warn = 30, False
        code = 'BUREAU_POLL_INTERVAL=30; set -- all "$POSITIONAL_INTERVAL"\n' + queue
        code += '\nprintf "%s" "$INTERVAL_SECONDS"'
        verify(run(code, {'POSITIONAL_INTERVAL': raw}), 'agents.poll_interval_minutes', raw, expected * 60, warn)

    # Real JSON parser + the production count assignment/bound/accumulation.
    # A schema-valid unsigned-64 maximum used to make 1 + count wrap to 0.
    task_code = function('parse_claude_json') + '\nTASKS_DONE_TOTAL=1\n'
    task_code += lines('implement-pipeline.sh', r'^    TASKS_DONE=\$\(parse_claude_json|^  TASKS_DONE=\$\(bureau_number|^  TASKS_DONE_TOTAL=\$\(\(')
    task_code += '\nprintf "%s" "$TASKS_DONE_TOTAL"'
    for raw, expected, warn in [(18446744073709551615, 1, True), (100001, 1, True),
                                (-1, 1, True), (100000, 100001, False), (10, 11, False)]:
        verify(run(task_code, {'RESULT': json.dumps({'tasks_done': raw})}), 'tasks_done', raw, expected, warn)
    # /goal assigns its result directly rather than accumulating TASKS_DONE.
    # Extract that separate production path: removing only its bound must
    # fail this assertion even while the ordinary/timeout guards remain.
    goal_code = function('parse_claude_json') + '\n'
    goal_code += lines('implement-pipeline.sh', r'^  TASKS_DONE_TOTAL=\$\(parse_claude_json|^  TASKS_DONE_TOTAL=\$\(bureau_number')
    goal_code += '\nprintf "%s" "$TASKS_DONE_TOTAL"'
    for raw, expected, warn in [(18446744073709551615, 0, True), (100000, 100000, False)]:
        verify(run(goal_code, {'RESULT': json.dumps({'tasks_done': raw})}), 'tasks_done', '/goal ' + str(raw), expected, warn)
    schema = json.loads((scripts / 'bureau-implement.schema.json').read_text())
    task_schema = schema['properties']['tasks_done']
    # Claude rejects numeric constraint keywords; the shared schema describes
    # the range and the shell enforces it for both providers and legacy prose.
    check('0 through 100000' in task_schema.get('description', '') and
          'minimum' not in task_schema and 'maximum' not in task_schema,
          'tasks_done schema describes 0..100000 without Claude-unsupported bounds')

    # The actual file reader and decision, not a substitute for awk/tail.
    throttle_file = temp / 'alert-throttle.log'
    throttle_code = function('_throttle_where') + '\n' + function('_throttle_should_suppress') + '''
date() { printf '1800000000'; }
if _throttle_should_suppress fixture 3600; then printf suppress; else printf allow; fi
'''
    for raw, expected, warn in [(raw, 'allow', True) for raw, _, _ in cases(1, 1, 9999999999)
                                if raw not in ('', '010', '1', '9999999999', '0')]:
        throttle_file.write_text('fixture\t1800000000\nfixture\t' + str(raw) + '\n')
        result = run(throttle_code, {'BUREAU_ALERT_THROTTLE_FILE': str(throttle_file)})
        # An empty record (including a newline-only timestamp) is an absent
        # timestamp and returns before the warning-producing validator.
        verify(result, 'alert.last_epoch', raw, expected, bool(str(raw).split('\t')[0].strip('\n')))
    for raw, expected in [('1800000000', 'suppress'), ('1799990000', 'allow'),
                          ('1800000100', 'suppress'), ('', 'allow'), ('0', 'allow')]:
        throttle_file.write_text('fixture\t' + raw + '\n')
        result = run(throttle_code, {'BUREAU_ALERT_THROTTLE_FILE': str(throttle_file)})
        check(result.returncode == 0 and result.stdout == expected, 'throttle valid/absent record ' + repr(raw))

    # Supervisor source selection: env beats JSON beats default; an invalid
    # selected value uses the default rather than falling through to JSON.
    for variable, path, default, minimum, maximum, pattern, output in [
        ('BUREAU_SUPERVISOR_MAX_CRASHES', '.supervisor.max_crashes', 5, 1, 1000, '^MAX_CRASHES=', 'MAX_CRASHES'),
        ('BUREAU_SUPERVISOR_STABILITY_WINDOW', '.supervisor.stability_window', 3600, 0, 604800, '^STABILITY_WINDOW=', 'STABILITY_WINDOW'),
    ]:
        field = path.split('.')[-1]
        code = lines('queue-loop-supervised.sh', pattern) + '\nprintf "%s" "$' + output + '"'
        for raw, expected, warn in cases(default, minimum, maximum) + [('1\x00', default, True), ('1\n', default, True), (None, default, False)]:
            config.write_text(json.dumps({'supervisor': {field: raw}}))
            verify(run(code), path, raw, expected, warn)
        config.write_text(json.dumps({'supervisor': {field: 17}}))
        for raw, expected, warn in [('010', 10, False), ('', default, True), ('0x10', default, True)]:
            verify(run(code, {variable: raw}), variable, raw, expected, warn)
    config.write_text('{}')

    # Core's long valid budgets must survive exactly; they never pass via .env.
    for key, raw in [('BUREAU_IMPL_ITER_TIMEOUT', '5400'), ('BUREAU_IMPL_TOTAL_TIMEOUT', '10800')]:
        consumer = next(row for row in consumers if row[0] == key)
        verify(run(lines(consumer[4], consumer[5]) + '\n' + consumer[6], {key: raw}), key, raw, int(raw), False)

    # Keep the actual .env reader and its precedence: accepted file entries
    # replace the environment, rejected entries leave it alone. Range checks
    # still happen at the consumer, after that precedence has resolved.
    dotenv = temp / '.env'
    assignment = lines('implement-pipeline.sh', r'^TOTAL_TIMEOUT=')
    for contents, inherited, expected, warning in [
        ('BUREAU_IMPL_TOTAL_TIMEOUT=10800\n', '5400', 10800, False),
        ('', '10800', 10800, False),
        ('BUREAU_IMPL_TOTAL_TIMEOUT=010\n', '10800', 10800, True),
        ('BUREAU_IMPL_TOTAL_TIMEOUT=0\n', '10800', 5400, True),
        ('BUREAU_IMPL_TOTAL_TIMEOUT=99999999999999999999\n', '10800', 5400, True),
    ]:
        dotenv.write_text(contents)
        code = 'bureau_load_env --export "$HOME/.env"\n' + assignment + '\nprintf "%s" "$TOTAL_TIMEOUT"'
        verify(run(code, {'BUREAU_IMPL_TOTAL_TIMEOUT': inherited}), 'BUREAU_IMPL_TOTAL_TIMEOUT', contents, expected, warning)

    # The real config guard and real assignments, with JSON payloads (including
    # NUL/newline), before poll/pane/spec/review/throttle arithmetic consumes them.
    setting = function('_bureau_config_number')
    bootstrap = '_BUREAU_SCRIPTS_DIR=$1\nbureau_get() { jq -r "$1" "$BUREAU_CONFIG"; }\n' + setting + '\n'
    settings = [
        ('.agents.poll_interval_minutes', 'BUREAU_POLL_INTERVAL', 30, 1, 10080),
        ('.agents.workbench_panes', 'BUREAU_WORKBENCH_PANES', 2, 0, 32),
        ('.agents.max_review_cycles', 'BUREAU_MAX_REVIEW_CYCLES', 3, 0, 1000),
        ('.agents.code_review_sampling_threshold', 'BUREAU_CODE_REVIEW_SAMPLING_THRESHOLD', 500, 0, 10000000),
        ('.agents.max_concurrent_issues', 'BUREAU_MAX_CONCURRENT_ISSUES', 0, 0, 1000),
    ]
    # Preserve each actual consumer's evaluation: the simple [ builtin rejects
    # expressions without executing them, unlike (( )). Do not manufacture a
    # more dangerous arithmetic site merely to make the control create files.
    spec_consumer = function('spec_admission_gate')
    if spec_consumer:
        # PR 2 moved this consumer into a helper. Run it with a real-shaped empty
        # capacity snapshot, so this remains an arithmetic guard, without Linear.
        spec_consumer += '\nspec_admission_snapshot() { printf \'{"count":0,"issues":[]}\'; }\n'
        spec_consumer += 'spec_admission_gate >/dev/null\nprintf "%s" "$BUREAU_MAX_CONCURRENT_ISSUES"'
    else:
        # Keep the optional pristine-main negative control exercising its inline site.
        spec_consumer = lines('spec-pipeline.sh', r'^if \[ "\$\{BUREAU_MAX_CONCURRENT_ISSUES') + '\n:; fi\nprintf "%s" "$BUREAU_MAX_CONCURRENT_ISSUES"'
    config_consumers = {
        'BUREAU_POLL_INTERVAL': lines('queue-loop.sh', r'^INTERVAL_(MINUTES|SECONDS)=') + '\nprintf "%s" "$INTERVAL_MINUTES"',
        'BUREAU_WORKBENCH_PANES': lines('start-bureau-v2.sh', r'^BENCH_PANES=') + '\n'
            + lines('start-bureau-v2.sh', r'^if \[ "\$BENCH_PANES" -gt') + '\n'
            + lines('start-bureau-v2.sh', r'^  for \(\(p=1;') + '\n:; done; fi\nprintf "%s" "$BENCH_PANES"',
        'BUREAU_MAX_REVIEW_CYCLES': function('_review_count') + '\n_review_count "$BUREAU_MAX_REVIEW_CYCLES" >/dev/null || :\nprintf "%s" "$BUREAU_MAX_REVIEW_CYCLES"',
        'BUREAU_CODE_REVIEW_SAMPLING_THRESHOLD': lines('code-review-pipeline.sh', r'^if \[ "\$\{DIFF_TOTAL')
            + '\n:; fi\nprintf "%s" "$BUREAU_CODE_REVIEW_SAMPLING_THRESHOLD"',
        'BUREAU_MAX_CONCURRENT_ISSUES': spec_consumer,
    }
    for path, variable, default, minimum, maximum in settings:
        code = bootstrap + lines('bureau-config.sh', '^' + variable + '=') + '\n' + config_consumers[variable]
        matrix = cases(default, minimum, maximum) + [('1\x00', default, True), ('1\n', default, True),
                                                  (False, default, True), ({}, default, True), (None, default, False)]
        for raw, expected, warn in matrix:
            section, field = path.strip('.').split('.')
            config.write_text(json.dumps({section: {field: raw}}))
            verify(run(code), path, raw, expected, warn)

    # Internal config variables must not be overwritten after their JSON
    # check: the real .env allowlist rejects these names, even though they
    # start with BUREAU_. Verify that boundary rather than assuming all such
    # names can be set by .env.
    for path, variable, default, minimum, maximum in (settings[3], settings[4]):
        config.write_text('{}')
        code = bootstrap + lines('bureau-config.sh', '^' + variable + '=')
        code += '\nbureau_load_env --export "$HOME/.env"\n' + config_consumers[variable]
        for raw, expected, warn in cases(default, minimum, maximum):
            dotenv.write_text(variable + "='" + raw + "'\n")
            verify(run(code), path, raw, default, False)

    # Polling supplied by an inherited environment overrides config only after
    # validation. Exercise the real merge-gate hold, with external work faked.
    gate = function('_merge_gate_number') + '\n' + function('merge_gate_waits')
    (temp / 'bureau').mkdir()
    (temp / 'bureau' / 'merge-gate-waits.json').write_text('{}')
    for raw, expected, warn in cases(1800, 1, 604800):
        config.write_text('{}')
        code = bootstrap + gate + '\nbureau_common_dir() { printf "%s" "$HOME"; }\n' + '''
python3() { while [ "$#" -gt 0 ]; do if [ "$1" = --first ]; then printf '%s' "$2" >&3; fi; shift; done; printf '{}'; }
merge_gate_waits merge 3>&1 >/dev/null
'''
        verify(run(code, {'BUREAU_QUEUE_POLL_SECONDS': raw}), 'BUREAU_QUEUE_POLL_SECONDS', raw, max(300, expected * 2), warn)

    # The usage threshold and timestamps were also numeric inputs into shell
    # arithmetic. A fractional percentage retains the original floor behavior.
    throttle = function('_throttle_decide')
    for key, parameter, default, minimum, maximum in [
        ('session.usage_threshold_pct', 3, 80, 0, 100),
        ('usage.reset_epoch', 1, 0, 0, 9999999999),
        ('usage.updated_epoch', 2, 0, 0, 9999999999),
    ]:
        for raw, expected, warn in cases(default, minimum, maximum):
            args = ['82.5', '0', '0', '80', 'false', '1800000000']
            args[parameter] = raw
            code = throttle + '\n_throttle_decide "$TEST_PCT" "$TEST_RESET" "$TEST_UPD" "$TEST_THRESHOLD" false 1800000000'
            result = run(code, dict(zip(['TEST_PCT', 'TEST_RESET', 'TEST_UPD', 'TEST_THRESHOLD'], args[:4])))
            check(result.returncode == 0, key + ': throttle still decides')
            check(len(result.stderr.splitlines()) == (1 if warn else 0), key + ': warning count')
            check('touch' not in result.stderr and 'SENTINEL' not in result.stderr, key + ': no payload in warning')

print(f'{checks} checks, {len(failures)} failed; {sentinels} SENTINEL file(s) created')
sys.exit(1 if failures else 0)
PY

# Whole stages with the existing harness: fake providers/Linear/GitHub, real
# stage control flow, real .env reader, and malicious inherited overrides.
# The optional snapshot control above is intentionally limited to the probes.
if [ "$SCRIPTS" = "$ROOT/templates/scripts" ]; then
  source "$ROOT/tests/lib/harness.sh"
  sandbox_init EXP-710 test-branch
  export SENTINEL="$SANDBOX/SENTINEL"
  export BUREAU_IMPL_MAX_ITER='a[$(touch "$SENTINEL")]'
  export BUREAU_IMPL_ITER_TIMEOUT='1+$(touch "$SENTINEL")'
  export BUREAU_IMPL_TOTAL_TIMEOUT='x[$(touch "$SENTINEL")]'
  export FAKE_CLAUDE_FIXTURES="$FIXTURES_DIR/claude_complete.txt" FAKE_CLAUDE_COMMIT_ON_ITERS=1
  run_implement_pipeline
  assert_eq 0 "$LAST_RC" 'implement completes with invalid inherited limits'
  assert_match 'iter 1: status=COMPLETE' "$LAST_STDOUT" 'implement reached the real loop'
  assert_eq 3 "$(printf '%s\n' "$LAST_STDERR" | grep -c '^warning: BUREAU_IMPL_')" 'one warning per implement key'
  [ ! -e "$SENTINEL" ] || { echo 'FAIL implement created SENTINEL'; exit 1; }
  teardown
  unset BUREAU_IMPL_MAX_ITER BUREAU_IMPL_ITER_TIMEOUT BUREAU_IMPL_TOTAL_TIMEOUT FAKE_CLAUDE_COMMIT_ON_ITERS

  sandbox_init EXP-711 test-branch
  export SENTINEL="$SANDBOX/SENTINEL" BUREAU_REVIEW_MERGE_CAP_KB='a[$(touch "$SENTINEL")]'
  printf 'change\n' > "$SANDBOX/change.txt"
  git -C "$SANDBOX" add change.txt
  git -C "$SANDBOX" commit -q -m 'fixture change'
  git -C "$SANDBOX" push -q origin test-branch
  printf 'Review checked.\n```json\n{"verdict":"APPROVE","bugs":0,"security_issues":0,"findings":[],"summary":"fixture"}\n```\n' > "$SANDBOX/verdict.txt"
  export FAKE_CLAUDE_FIXTURES="$SANDBOX/verdict.txt" BUREAU_STUB_ISSUE_STATE='Build Review' GH_STUB_EXISTING_PR=99
  export BUREAU_NO_MERGE=1
  run_pipeline code-review-pipeline.sh EXP-711
  assert_eq 20 "$LAST_RC" 'review stops before merge with invalid inherited cap'
  assert_calls_include 'post_comment.*Code review' 'review reached the merger and posted its result'
  assert_eq 1 "$(printf '%s\n' "$LAST_STDERR" | grep -c '^warning: BUREAU_REVIEW_MERGE_CAP_KB')" 'one warning for review cap'
  [ ! -e "$SENTINEL" ] || { echo 'FAIL review created SENTINEL'; exit 1; }
  echo 'PASS 8 whole-stage assertions (implement and code review)'
fi
