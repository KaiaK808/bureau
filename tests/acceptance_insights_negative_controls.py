"""Prove PR 7 assertions detect deliberate defects in private copies."""
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / 'templates/scripts'
names = ('bureau-acceptance.py', 'bureau-evidence.py', 'code-review-pipeline.sh', 'bureau-config.sh')
before = {name: hashlib.sha256((SCRIPTS/name).read_bytes()).hexdigest() for name in names}


def rejected(command, expected, env=None):
    result = subprocess.run(command, capture_output=True, text=True, env={**os.environ, **(env or {})}, timeout=90)
    output = result.stdout + result.stderr
    assert result.returncode != 0 and expected in output, output
    print('PASS negative control: ' + expected)


with tempfile.TemporaryDirectory(prefix='bureau-pr7-negative-') as temp:
    root = Path(temp)
    shutil.copytree(SCRIPTS, root/'templates/scripts')
    shutil.copytree(ROOT/'tests/lib', root/'tests/lib')
    (root/'tests/fixtures').mkdir()
    shutil.copy(ROOT/'tests/fixtures/speckit-1.2.2.zip', root/'tests/fixtures')
    shutil.copy(ROOT/'tests/test_merge_mode.sh', root/'tests')
    review = root/'templates/scripts/code-review-pipeline.sh'
    hook = '      bureau_acceptance_brief "$ISSUE" "$BRANCH" --pr "$PR_NUMBER" --head "$REVIEW_HEAD" --url "$PR_URL" --base "$REVIEW_BASE" --verdict "$VERDICT" --review-head "$REVIEW_HEAD" || true\n'
    assert review.read_text().count(hook) == 2
    review.write_text(review.read_text().replace(hook, '', 1))
    rejected(['bash', str(root/'tests/test_merge_mode.sh')], 'manual boundary ticket brief missing')
    # Restore the earlier control so each new mutation has exactly one cause.
    review.write_text((SCRIPTS/review.name).read_text())

    shutil.copy(ROOT/'tests/test_insight_comment_privacy.sh', root/'tests')
    config = root/'templates/scripts/bureau-config.sh'; config_original = config.read_text()
    projection = config_original.index('bureau_comment_projection() {')
    projection_end = config_original.index('\n}\n', projection) + 3
    unsafe_projection = config_original[:projection] + 'bureau_comment_projection() { printf "%s" "$1"; }\n' + config_original[projection_end:]
    config.write_text(unsafe_projection)
    review.write_text(review.read_text().replace('del(.comment, .insights)', 'del(.comment)'))
    transport_env = dict(PR7_STAGES='review', PR7_VERDICTS='REQUEST_CHANGES', PR7_FORMS='object')
    rejected(['bash', str(root/'tests/test_insight_comment_privacy.sh')],
             'insight leaked into actual transport payload: review/REQUEST_CHANGES/object', transport_env)
    # Even a correct primary formatter must not use raw legacy/fallback output.
    review.write_text((SCRIPTS/review.name).read_text())
    rejected(['bash', str(root/'tests/test_insight_comment_privacy.sh')],
             'insight leaked into actual transport payload: review/REQUEST_CHANGES/fenced',
             {**transport_env, 'PR7_FORMS':'fenced'})
    config.write_text(config_original)

    # Comment outages must not turn a human hold into QA's default GREEN.
    parse_local = '  local raw="$1" filter="$2" text block\n'
    assert config_original.count(parse_local) == 1
    config.write_text(config_original.replace(parse_local, parse_local + '  raw=$(bureau_comment_projection "$raw")\n'))
    rejected(['bash', str(root/'tests/test_insight_comment_privacy.sh')], 'QA routing changed',
             dict(PR7_STAGES='qa', PR7_VERDICTS='NEEDS_HUMAN', PR7_FORMS='projector-failure'))
    config.write_text(config_original)

    evidence = root/'templates/scripts/bureau-evidence.py'; original = evidence.read_text()
    anchor = '        observe(self.collect_insights, receipt)\n'
    assert original.count(anchor) == 1
    evidence.write_text(original.replace(anchor, ''))
    env = {'BUREAU_TEST_ACCEPTANCE_SCRIPT': str(root/'templates/scripts/bureau-acceptance.py')}
    rejected([sys.executable, str(ROOT/'tests/acceptance_insights_test.py'),
              'AcceptanceInsightsTests.test_receipt_collects_once_and_keeps_metadata_private'],
             'valid emitted observation absent', env)
    evidence.write_text(original)

    provider_env = {'BUREAU_TEST_PROVIDER_SCRIPT':str(root/'templates/scripts/bureau-provider.py')}
    provider_test = [sys.executable, str(ROOT/'tests/provider_adapter_test.py'),
                     'ProviderTests.test_insights_content_filter_preserves_provider_success']
    # Restore each independently: quoted name, split token, inline log marker.
    quoted = r'[\"\x27]?'
    assert original.count(quoted) == 1
    split_line = next(line for line in original.splitlines(True) if line.startswith('SPLIT_OPAQUE_INSIGHT = '))
    log_start = original.index('LOG_INSIGHT = re.compile(')
    log_end = original.index('\n\n\nclass CapacityError', log_start)
    mutations = (
        ('quoted credential names', original.replace(quoted, '')),
        ('split opaque tokens', original.replace(split_line, 'SPLIT_OPAQUE_INSIGHT = re.compile(r"(?!)")\n')),
        ('inline log markers', original[:log_start] + 'LOG_INSIGHT = re.compile(r"(?!)")' + original[log_end:]),
    )
    for label, mutation in mutations:
        evidence.write_text(mutation)
        rejected(provider_test, 'FAIL: test_insights_content_filter_preserves_provider_success', provider_env)
        print('PASS filter negative control: ' + label)
    evidence.write_text(original)

    acceptance = root/'templates/scripts/bureau-acceptance.py'
    anchor = "    return '\\n'.join(lines) + '\\n'\n"
    assert acceptance.read_text().count(anchor) == 1
    acceptance.write_text(acceptance.read_text().replace(anchor,
                          "    return '\\n'.join(lines) + '\\n' + (repo/'gate.log').read_text()\n"))
    rejected([sys.executable, str(ROOT/'tests/acceptance_insights_test.py'),
              'AcceptanceInsightsTests.test_render_links_metadata_review_and_privacy'],
             'brief privacy leaked raw source bytes', env)

assert before == {name: hashlib.sha256((SCRIPTS/name).read_bytes()).hexdigest() for name in names}
print('PASS negative controls changed no source files')
