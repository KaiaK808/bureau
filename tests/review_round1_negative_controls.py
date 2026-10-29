"""Review behavior must fail unchanged positive assertions in private copies."""
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
CONTROLS = (
    ('human-hold-lost', 'bureau-review.py', 'holds.extend(human_holds(raw))', 'holds.extend([])',
     'MergeRegressions.test_structured_holds_survive_convergence'),
    ('fallback-drops-claims', 'bureau-review.py', "[('retained', f) for f in retained['findings']] +",
     '[] +', 'MergeRegressions.test_fallback_keeps_every_readable_role_and_acceptance_gap'),
    ('count-only-critical-lost', 'bureau-review.py', "reported = raw.get('counts', {})",
     "reported = raw.get('counts', {}) if role not in failures else {}",
     'MergeRegressions.test_count_only_severities_survive_unrelated_schema_faults'),
    ('unreadable-can-approve', 'bureau-review.py', 'if failures or uncertain_claims else',
     'if uncertain_claims else', 'MergeRegressions.test_completely_unreadable_specialist_cannot_fall_back_to_approval'),
    ('critical-message-lost', 'bureau-review.py', "isinstance(f, dict) and f.get('class') == severity.upper()",
     "isinstance(f, dict) and f.get('class') == severity.upper() and isinstance(f.get('msg'), str) and bool(f['msg'].strip())",
     'MergeRegressions.test_critical_class_survives_an_unreadable_message'),
    ('malformed-location-skipped', 'bureau-review.py',
     "status, _ = anchor_check(finding, base, head) if isinstance(finding, dict) else ('location', None)",
     "status, _ = anchor_check(finding, base, head) if isinstance(finding, dict) and location_valid(finding) else ('valid', None)",
     'MergeRegressions.test_malformed_locations_are_counted_even_when_claim_is_unreadable'),
    ('skip-promoted-to-security', 'bureau-review.py', "sum('security' in f['actionable_specialists'] for f in findings)",
     "sum('security' in f['specialists'] for f in findings)",
     'MergeRegressions.test_dedup_preserves_per_variant_security_actionability'),
    ('shadow-tools-enabled', 'bureau-provider.py',
     "shadow = options['stage'] == 'code_review' and options.get('phase') == 'finding_verifier'",
     'shadow = False', 'VerifierRegressions.test_claude_shadow_has_no_builtin_mcp_or_skill_tools'),
    ('acceptance-ignores-round', 'bureau-acceptance.py',
     'completed = completed_review(repo, receipts, rounds, head, base)', 'completed = None',
     'AcceptanceRegressions.test_deterministic_acceptance_uses_receipts_and_round_blockers'),
    ('summary-routing-disabled', 'bureau-review.py',
     "if role in values and summary_requires_merger(values[role]['summary']):", 'if False:',
     'MergeRegressions.test_summary_severities_use_merger_without_inferred_floors'),
    ('negation-routes-deterministically', 'bureau-review.py', 'return any(word in summary for word in (',
     "return not summary.startswith(('no ', 'nothing ', 'not ')) and any(word in summary for word in (",
     'MergeRegressions.test_summary_signals_use_merger_even_when_negated'),
    ('summary-manufactures-critical-floor', 'bureau-review.py', 'holds.extend(human_holds(raw))',
     "holds.extend(human_holds(raw))\n        counts['critical'] = max(counts['critical'], int('critical' in raw.get('summary', '').lower()))",
     'MergeRegressions.test_zero_severity_tallies_use_merger_without_floors'),
    ('summary-manufactures-human-hold', 'bureau-review.py', 'holds.extend(human_holds(raw))',
     "holds.extend(human_holds(raw))\n        if 'human review' in raw.get('summary', '').lower(): holds.append('Inferred hold.')",
     'MergeRegressions.test_summary_signals_use_merger_even_when_negated'),
    ('prose-routes-deterministically', 'bureau-review.py', 'if not pure_report(text):', 'if not structured(text):',
     'MergeRegressions.test_prose_around_json_never_uses_deterministic_merge'),
    ('envelope-prose-routes-deterministically', 'bureau-review.py', "value = json.loads(value['result'])",
     "value = structured(value['result'])", 'MergeRegressions.test_cost_envelopes_preserve_report_purity'),
    ('fallback-reason-lost', 'bureau-review.py', 'fallback_reasons=fallback_reasons,', 'fallback_reasons={},',
     'MergeRegressions.test_summary_severities_use_merger_without_inferred_floors'),
    ('shadow-memory-enabled', 'bureau-provider.py', "if shadow: child_env['CLAUDE_CODE_DISABLE_AUTO_MEMORY'] = '1'",
     "if shadow: child_env['CLAUDE_CODE_DISABLE_AUTO_MEMORY'] = '0'",
     'ProviderTests.test_shadow_disables_automatic_memory_only_for_verification'),
    ('acceptance-prefers-old-deterministic', 'bureau-acceptance.py',
     "merge.get('mode') not in ('deterministic', 'model_fallback')", "merge.get('mode') not in ('deterministic',)",
     'AcceptanceInsightsTests.test_latest_completed_round_uses_retained_counts_across_merge_modes'),
)

for name, filename, old, new, test in CONTROLS:
    with tempfile.TemporaryDirectory(prefix='bureau-review-round1-control-') as scratch:
        scripts = Path(scratch).resolve() / 'scripts'
        shutil.copytree(ROOT / 'templates/scripts', scripts, ignore=shutil.ignore_patterns('__pycache__'))
        path = scripts / filename; source = path.read_text()
        if source.count(old) != 1: sys.exit('CONTROL PATCH FAILED: ' + name)
        path.write_text(source.replace(old, new, 1))
        program = 'review_round1_test.py'
        env = {**os.environ, 'BUREAU_TEST_REVIEW_SCRIPTS':str(scripts), 'PYTHONDONTWRITEBYTECODE':'1'}
        if test.startswith('ProviderTests.'):
            program = 'provider_adapter_test.py'; env['BUREAU_TEST_PROVIDER_SCRIPT'] = str(scripts / filename)
        elif test.startswith('AcceptanceInsightsTests.'):
            program = 'acceptance_insights_test.py'; env['BUREAU_TEST_ACCEPTANCE_SCRIPT'] = str(scripts / filename)
        completed = subprocess.run([sys.executable, str(ROOT / 'tests' / program), test],
                                   cwd=ROOT, env=env,
                                   capture_output=True, text=True, timeout=45)
        if completed.returncode != 1 or 'AssertionError' not in completed.stderr or 'FAILED (failures=' not in completed.stderr:
            sys.exit('CONTROL DID NOT FAIL ITS ASSERTION: ' + name + '\n' + completed.stdout + completed.stderr)
        print('PASS negative control: ' + name + ' rejected by unchanged positive test')
print(str(len(CONTROLS)) + ' review negative controls passed')
