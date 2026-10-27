"""Prove the new assertions reject independent missing-behaviour mutations."""
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
with tempfile.TemporaryDirectory(prefix='bureau-1753-negative-') as temp:
    scripts = Path(temp) / 'templates/scripts'; shutil.copytree(ROOT / 'templates/scripts', scripts)
    env = {**os.environ, 'BUREAU_TEST_INSIGHTS_SCRIPTS':str(scripts), 'PYTHONDONTWRITEBYTECODE':'1'}
    def reject(label, case, expected):
        p = subprocess.run([sys.executable, str(ROOT / 'tests/failure_insights_test.py'), case],
                           env=env, capture_output=True, text=True, timeout=90)
        assert p.returncode != 0 and expected in p.stdout + p.stderr, (label, p.returncode, p.stdout, p.stderr)
        print('PASS negative control: ' + label)
    evidence = scripts / 'bureau-evidence.py'; original = evidence.read_text()
    needle = "        if not items or receipt.get('stage') not in STAGES:\n"
    assert original.count(needle) == 1
    evidence.write_text(original.replace(needle, "        if receipt.get('normalized_exit') != 0 or not items or receipt.get('stage') not in STAGES:\n"))
    reject('success-only collector', 'StoreTests.test_failed_insights_for_every_stage_and_all_causes', 'missing learning ledger')
    evidence.write_text(original)
    needle = "        observe(self.collect_dispositions, receipt)\n"; assert original.count(needle) == 1
    evidence.write_text(original.replace(needle, ''))
    reject('missing dispositions', 'StoreTests.test_dispositions_project_severity_path_and_no_finding_prose', 'missing learning ledger')
    evidence.write_text(original)
    anchor = "        cause = insight_cause(receipt.get('normalized_exit'), receipt.get('outcome'), receipt.get('timeout_kind'))\n"
    assert original.count(anchor) == 1
    evidence.write_text(original.replace(anchor, anchor.replace('cause = ', "cause = (receipt.get('structured_result') or {}).get('cause_class') or ")))
    reject('model cause claim trusted', 'StoreTests.test_failed_insights_for_every_stage_and_all_causes', 'MODEL_CAUSE_CANARY')
    evidence.write_text(original)
    provider = scripts / 'bureau-provider.py'; provider_original = provider.read_text()
    anchor = "    if code != 0: receipt['insights_result'] = e.observe(receipt_result, receipt)\n"
    assert provider_original.count(anchor) == 1
    provider.write_text(provider_original.replace(anchor, ''))
    reject('failed provider observation dropped', 'ProviderRecordTests.test_real_failed_provider_captures_only_filtered_advisory_text', 'missing failed provider observation')
    provider.write_text(provider_original)
    shell = scripts / 'bureau-insights.sh'; shell_original = shell.read_text()
    shell.write_text(shell_original.replace("printf '%s\\n' timeout", "printf '%s\\n' human_hold"))
    reject('silence softened to ordinary hold', 'StoreTests.test_silence_is_timeout_without_result_and_prose_cannot_claim_it', "'human_hold' != 'timeout'")
    shell.write_text(shell_original)
    shepherd = scripts / 'shepherd.sh'; shepherd_original = shepherd.read_text()
    anchor = '  ( emit_event "event=stage_end" "mode=shepherd"'
    assert shepherd_original.count(anchor) == 1
    shepherd.write_text(shepherd_original.replace(anchor, '  ( : "event=stage_end" "mode=shepherd"'))
    reject('missing shepherd outcome', 'PipelineTests.test_shepherd_emits_exact_worker_outcome_before_checkpoint', 'missing or duplicated shepherd outcome')
