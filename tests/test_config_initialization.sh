#!/bin/bash
# Source-time framing, defaults and process count, using the real jq parser.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SCRIPTS="${1:-$ROOT/templates/scripts}"
python3 - "$SCRIPTS" <<'PY'
import copy
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

scripts = Path(sys.argv[1])
numbers = {
    'poll_interval_minutes': ('BUREAU_POLL_INTERVAL', 30, 1, 10080),
    'workbench_panes': ('BUREAU_WORKBENCH_PANES', 2, 0, 32),
    'parallel_lanes': ('BUREAU_PARALLEL_LANES', 2, 1, 1000),
    'max_review_cycles': ('BUREAU_MAX_REVIEW_CYCLES', 3, 0, 1000),
    'max_review_rounds': ('BUREAU_MAX_REVIEW_ROUNDS', 4, 1, 1001),
    'code_review_sampling_threshold': ('BUREAU_CODE_REVIEW_SAMPLING_THRESHOLD', 500, 0, 10000000),
    'max_concurrent_issues': ('BUREAU_MAX_CONCURRENT_ISSUES', 0, 0, 1000),
}
base = {'linear': {'teams': [{'key': 'TEAM', 'name': 'Team', 'states': {}}], 'labels': {}},
        'agents': {}, 'repo': {}}
probe = '''set -euo pipefail
source "$1/bureau-config.sh"
shift
for name do printf '%s\\0' "${!name}"; done
'''

with tempfile.TemporaryDirectory(prefix='bureau-config-test-') as scratch:
    root = Path(scratch); (root / 'bin').mkdir()
    config, counter = root / '.bureau.json', root / 'count'
    shim = root / 'bin/jq'
    shim.write_text('#!/bin/bash\nprintf ".\\n" >> "$JQ_COUNTER"\nexec "$REAL_JQ" "$@"\n')
    shim.chmod(0o755)
    env = dict(os.environ, BUREAU_CONFIG=str(config), TMPDIR=scratch, REAL_JQ=shutil.which('jq'),
               JQ_COUNTER=str(counter), PATH=str(root / 'bin') + os.pathsep + os.environ['PATH'])
    env.pop('BUREAU_ENV_FILE', None)

    def run(value, names=(), code=probe, extra=()):
        config.write_text(json.dumps(value)); counter.write_text('')
        result = subprocess.run(['/bin/bash', '-c', code, 'probe', str(scripts), *names, *extra],
                                cwd=root, env=env, capture_output=True, timeout=10)
        assert not (root / 'executed').exists(), 'configuration executed shell text'
        return result, len(counter.read_text().splitlines())

    result, count = run(base)
    assert result.returncode == 0 and not result.stderr, result.stderr
    assert count == 1, ('source-time jq upper bound', count)
    result, negative = run(base, code=probe + '\njq -n null >/dev/null\n')
    assert result.returncode == 0 and negative > 1, 'counter missed an extra jq start'
    print('PASS source starts one jq; extra-start negative control exceeds the bound')

    env_file = root / 'external secrets.env\n\n'; env_file.write_text('')
    value = copy.deepcopy(base); value['repo']['env_file'] = str(env_file)
    result, count = run(value, ['BUREAU_ENV_FILE'])
    assert result.returncode == 0 and not result.stderr, result
    assert result.stdout == os.fsencode(env_file) + b'\0', result
    assert count == 1, ('external env file source-time jq upper bound', count)
    print('PASS external env file preserves trailing newlines with one jq start')

    defaults = {var: str(default) for var, default, _, _ in numbers.values()}
    defaults.update(BUREAU_TEAM_KEY='TEAM', BUREAU_TEAM_NAME='Team', BUREAU_STATE_TRIAGE='null',
                    BUREAU_STATE_SPEC='null', BUREAU_STATE_SPEC_REVIEW='null', BUREAU_STATE_DESIGN='null',
                    BUREAU_STATE_BUILD='null', BUREAU_STATE_BUILD_REVIEW='null', BUREAU_STATE_DONE='null',
                    BUREAU_STATE_QA='', BUREAU_STATE_COPY='', BUREAU_STATE_MERGE='', BUREAU_LABEL_LANE2='null',
                    BUREAU_LABEL_LANE2_NAME='null', BUREAU_LABEL_NEEDS_COPY_NAME='', BUREAU_COPY_VOICE_FILE='',
                    BUREAU_REVIEW_MIN_SEVERITY='BUG', BUREAU_MERGE_STRATEGY='squash', BUREAU_MERGE_MODE='auto',
                    BUREAU_BRANCH_PREFIX='feat', BUREAU_SPECS_DIR='specs', BUREAU_PROJECTS='')
    result, _ = run(base, defaults)
    assert result.returncode == 0 and result.stdout.split(b'\0')[:-1] == [v.encode() for v in defaults.values()], result

    hostile = 'line\n"quoted" $(touch executed) `touch executed` \\ slash\0\n\n'
    value = copy.deepcopy(base)
    value['linear']['teams'][0]['name'] = hostile
    value['linear']['teams'][0]['states'] = dict.fromkeys(('triage', 'spec', 'spec_review', 'design', 'build', 'build_review', 'done', 'qa', 'copy', 'merge'), hostile)
    value['linear']['labels'] = {'lane2': {'id': hostile, 'name': hostile}, 'needs_copy': {'name': hostile}}
    value['repo'] = dict.fromkeys(('branch_prefix', 'specs_dir', 'copy_voice_file'), hostile)
    value['linear']['projects'] = ['first', hostile, 'last']
    expected = {k: hostile.replace('\0', '').rstrip('\n') for k in defaults if k.startswith('BUREAU_STATE_')}
    expected.update(dict.fromkeys(('BUREAU_TEAM_NAME', 'BUREAU_LABEL_LANE2', 'BUREAU_LABEL_LANE2_NAME',
                                  'BUREAU_LABEL_NEEDS_COPY_NAME', 'BUREAU_COPY_VOICE_FILE', 'BUREAU_BRANCH_PREFIX',
                                  'BUREAU_SPECS_DIR'), hostile.replace('\0', '').rstrip('\n')))
    expected['BUREAU_PROJECTS'] = 'first,' + hostile.replace('\0', '') + ',last'
    result, _ = run(value, expected)
    assert result.returncode == 0 and result.stdout.split(b'\0')[:-1] == [v.encode() for v in expected.values()], result
    print('PASS defaults and hostile text preserve raw-reader values without execution')

    for key, (var, default, minimum, maximum) in numbers.items():
        cases = [(None, default, False), ('010', 10, False), (str(minimum), minimum, False),
                 (maximum, maximum, False), (maximum + 1, default, True)]
        cases += [(v, default, True) for v in ('', False, {}, [], -1, 1.5, '1\n', '1\0', hostile, 'default', '9' * 40)]
        for raw, expected, warns in cases:
            value = copy.deepcopy(base); value['agents'][key] = raw
            result, _ = run(value, [var])
            warning = f'warning: .agents.{key} ignored: expected digits in the accepted range; using the default\n' if warns else ''
            assert (result.returncode, result.stdout, result.stderr) == (0, str(expected).encode() + b'\0', warning.encode()), (key, raw, result)
    value = copy.deepcopy(base); value['agents']['max_review_cycles'] = '000'
    result, _ = run(value, ['BUREAU_MAX_REVIEW_CYCLES', 'BUREAU_MAX_REVIEW_ROUNDS'])
    assert result.stdout == b'0\0' + b'1\0' and not result.stderr, result
    print('PASS every numeric bound and warning, including dependent round defaults')

    for key, var, invalid, expected in (
            ('merge_strategy', 'BUREAU_MERGE_STRATEGY', hostile, 'squash'),
            ('merge_mode', 'BUREAU_MERGE_MODE', 'auto\n', 'manual'),
            ('review_min_severity', 'BUREAU_REVIEW_MIN_SEVERITY', 'BUG\n', 'MINOR')):
        value = copy.deepcopy(base); value['agents'][key] = invalid
        result, _ = run(value, [var])
        assert result.returncode == 0 and result.stdout == expected.encode() + b'\0', result
        assert bool(result.stderr) == (key != 'review_min_severity'), result

    for malformed in ('{', '', '{} {}', '[]'):
        config.write_text(malformed)
        result = subprocess.run(['/bin/bash', '-c', probe, 'probe', str(scripts)],
                                cwd=root, env=env, capture_output=True, timeout=10)
        assert result.returncode != 0 and result.stderr, (malformed, result)
    print('PASS malformed configuration fails before consumers run')

    for risk, expected in (({}, 0), (False, 24), ({'low_max_files': 'invalid'}, 24)):
        value = copy.deepcopy(base); value['agents']['risk_tier'] = risk
        result, count = run(value)
        assert result.returncode == expected and count == 1, result
        assert result.stderr == (b'ERROR: invalid agents.risk_tier configuration\n' if expected else b''), result
    print('PASS advisory risk validation keeps its exit code and diagnostic')

    changed = copy.deepcopy(base)
    changed['agents'] = {'implement': {'enabled': False, 'runner': 'codex', 'model': 'changed-model'}, 'poll_interval_minutes': 7}
    changed['repo']['test_command'] = 'changed-command'
    replacement = root / 'changed.json'; replacement.write_text(json.dumps(changed))
    code = '''set -euo pipefail
source "$1/bureau-config.sh"
cp "$2" "$BUREAU_CONFIG"
resolve_runner_for_stage implement
bureau_get_agent_model implement
bureau_get '.repo.test_command'
_bureau_config_number .agents.poll_interval_minutes 30 1 10080
printf '\\n'
if agent_enabled implement; then exit 1; fi
'''
    result, _ = run(base, code=code, extra=[str(replacement)])
    assert result.returncode == 0 and result.stdout == b'codex\nchanged-model\nchanged-command\n7\n', result
    print('PASS runner, model, stage toggle, command and numeric readers stay live')
PY
