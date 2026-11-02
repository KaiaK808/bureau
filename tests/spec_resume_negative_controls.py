"""Mutate private script copies; every broken guard must fail a real assertion."""
import hashlib
from pathlib import Path
import shutil
import sys
import tempfile

from mutation_checks import mutation_caught, snapshot_files

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT/'templates/scripts'
names = ('bureau-runtime.py', 'bureau-evidence.py', 'bureau-spec.py', 'bureau-provider.py',
         'bureau-config.sh', 'spec-pipeline.sh', 'spec-review-pipeline.sh')
before = {name: hashlib.sha256((SCRIPTS/name).read_bytes()).hexdigest() for name in names}


def rejected(script, case, env, expected):
    mutation_caught([sys.executable, str(ROOT/'tests'/script), case], env=env,
                    originals=originals, timeout=900, expected=expected)


with tempfile.TemporaryDirectory(prefix='bureau spec controls ') as scratch:
    root = Path(scratch).resolve()/'scripts'; shutil.copytree(SCRIPTS, root)
    originals = snapshot_files(root)
    spec = root/'bureau-spec.py'; original = spec.read_text()
    env = {'BUREAU_TEST_SPEC_SCRIPTS': str(root)}

    anchor = "        cutoff_line = re.sub(r'^(?:> ?)+', '', line)\n        cutoff_raw = re.sub(r'^(?:> ?)+', '', raw_lines[index])"
    assert original.count(anchor) == 1
    spec.write_text(original.replace(anchor, '        cutoff_line = line\n        cutoff_raw = raw_lines[index]'))
    rejected('spec_resume_test.py', 'GovernanceTests.test_quoted_code_and_html_cutoffs',
             env, 'AssertionError:')
    print('PASS final follow-up control: disabling blockquote stripping fails quoted code/HTML cutoff regressions')
    spec.write_text(original)

    anchor = '        if len(parts) > 1:'
    assert original.count(anchor) == 1
    spec.write_text(original.replace(anchor, '        if False:  # Broken: disable composite placeholder recognition'))
    rejected('spec_resume_test.py', 'GovernanceTests.test_common_template_placeholders',
             env, 'AssertionError: unexpectedly None')
    print('PASS final follow-up control: disabling the composite rule fails placeholder regressions')
    spec.write_text(original)

    anchor = next(line for line in original.splitlines() if 'line.translate(str.maketrans' in line)
    assert original.count(anchor) == 1
    assert '[]' in anchor
    spec.write_text(original.replace(anchor, anchor.replace('[]', '')))
    rejected('spec_resume_test.py', 'GovernanceTests.test_wrapped_placeholders_and_indented_code',
             env, 'AssertionError: unexpectedly None')
    print('PASS round9 control: removing bracket stripping fails the placeholder regression')
    spec.write_text(original)

    start = original.index('def _governance_controls(value):')
    end = original.index('\n\ndef _only_governance_placeholder', start)
    broken = original[:start] + 'def _governance_controls(value):\n    return False\n' + original[end:]
    spec.write_text(broken.replace(".split('\\n')", '.splitlines()'))
    rejected('spec_resume_test.py', 'GovernanceTests.test_round8_honest_mistakes_and_valid_documents',
             env, 'AssertionError: unexpectedly None')
    print('PASS round8 item 1 control: restoring splitlines and allowing controls fails the validator regression')
    spec.write_text(original)

    start = original.index('def _normalize_governance(value):')
    end = original.index('\n\ndef _governance_controls', start)
    broken = original[:start] + 'def _normalize_governance(value):\n    return value\n' + original[end:]
    spec.write_text(broken.replace('    if not any(char.isalnum() for char in value): return True\n', ''))
    rejected('spec_resume_test.py', 'GovernanceTests.test_round8_honest_mistakes_and_valid_documents',
             env, 'AssertionError: unexpectedly None')
    print('PASS round8 item 2 control: removing normalization and the alphanumeric minimum fails the validator regression')
    spec.write_text(original)

    anchor = '        e.warn()\n        return 2'
    assert original.count(anchor) == 1
    spec.write_text(original.replace(anchor,"        print('checkpoint storage unavailable',file=sys.stderr)\n        return 2"))
    rejected('spec_resume_test.py','CheckpointTests.test_checkpoint_io_with_closed_stderr_keeps_optional_status',
             env,'AssertionError:')
    print('PASS restack control: buffered optional-checkpoint diagnostics break closed-stderr status 2; the CLI guard detected it')
    spec.write_text(original)

    pipeline = root/'spec-pipeline.sh'; pipeline_original = pipeline.read_text()
    start = pipeline_original.index('    free_branch_from_other_worktrees "$FEATURE_BRANCH" "$REPO_DIR" || {')
    end = pipeline_original.index('    git checkout "$FEATURE_BRANCH"',start)
    pipeline.write_text(pipeline_original[:start] + '    free_branch_from_other_worktrees "$FEATURE_BRANCH" "$REPO_DIR"\n' + pipeline_original[end:])
    rejected('spec_resume_test.py','SpecWorkerTests.test_late_held_feature_halts_and_actual_picker_skips_ticket',
             env,"'TEAM-1' != 'TEAM-2'")
    print('PASS round6 control: reverting the late ownership halt let the actual picker select the refused ticket again')
    pipeline.write_text(pipeline_original)

    anchor = "            break\n        lines.append(line)"
    assert original.count(anchor) == 1
    spec.write_text(original.replace(anchor, "            pass  # Broken: allow governance after the first fence-like line\n        lines.append(line)"))
    rejected('spec_resume_test.py','GovernanceTests.test_governance_precedes_first_fence',
             env,'AssertionError:')
    rejected('shared_runtime_test.py','RuntimeTests.test_real_provider_pass_obeys_governance_placement',
             {'BUREAU_TEST_RUNTIME_SCRIPT':str(root/'bureau-runtime.py')},'ValueError not raised')
    print('PASS round7 control: allowing governance after the first fence-like line fails validator and real-provider app spec/spec_review guards')
    spec.write_text(original)

    anchor = next(line for line in original.splitlines() if "text = re.sub(r'<!--" in line)
    assert original.count(anchor) == 1
    spec.write_text(original.replace(anchor, '    # Broken: ignore HTML comment boundaries and count their contents'))
    rejected('spec_resume_test.py','GovernanceTests.test_governance_comments_and_column_zero_lines',
             env,'AssertionError: unexpectedly None')
    rejected('shared_runtime_test.py','RuntimeTests.test_real_provider_pass_obeys_governance_placement',
             {'BUREAU_TEST_RUNTIME_SCRIPT':str(root/'bureau-runtime.py')},'ValueError not raised')
    print('PASS round7 control: ignoring HTML comments fails validator and real-provider app spec/spec_review guards')
    spec.write_text(original)

    evidence = root/'bureau-evidence.py'; evidence_original = evidence.read_text()
    start = evidence_original.index('        # Checkpoints are optional, including')
    end = evidence_original.index("        files = [p for p in self.root.rglob('*')", start)
    old_prune = '''        for path in (self.root / 'spec-checkpoints').glob('*.json'):
            try: value = read(path)
            except ValueError: continue  # Invalid optional checkpoints cannot stop a stage.
            if not isinstance(value, dict) or not isinstance(value.get('updated_at'), (int, float)) or not isinstance(value.get('issue'), str): continue
            if now - value['updated_at'] > RETENTION_SECONDS and value['issue'] not in active_issues:
                path.unlink()
'''
    evidence.write_text(evidence_original[:start]+old_prune+evidence_original[end:])
    for case in ('test_optional_checkpoint_read_failures_do_not_abort_provider',
                 'test_optional_checkpoint_prune_failures_do_not_abort_provider'):
        rejected('provider_adapter_test.py','ProviderTests.'+case,
                 {'BUREAU_TEST_PROVIDER_SCRIPT':str(root/'bureau-provider.py')},'22 != 0')
    print('PASS round5 control: reverting optional pruning containment changed real adapter success to 22/24 before the provider; read and unlink guards detected it')
    evidence.write_text(evidence_original)

    pipeline = root/'spec-pipeline.sh'; pipeline_original = pipeline.read_text()
    start = pipeline_original.index('# A rejected checkpoint means fresh phases')
    end = pipeline_original.index('echo "  branch ready:',start)
    stale = pipeline_original[:start]+pipeline_original[end:]
    anchor = '''  elif git show-ref --verify --quiet "refs/remotes/origin/$FEATURE_BRANCH"; then
    git checkout -b "$FEATURE_BRANCH" --track "origin/$FEATURE_BRANCH"
'''
    assert stale.count(anchor)==1
    pipeline.write_text(stale.replace(anchor,''))
    rejected('spec_resume_test.py','SpecWorkerTests.test_remote_feature_advance_fast_forwards_stale_local_on_one_retry',
             env,'non-fast-forward')
    rejected('spec_resume_test.py','SpecWorkerTests.test_diverged_feature_halts_once_needs_human_before_provider',
             env,'1 != 25')
    print('PASS round5 control: reverting feature reconciliation broke the one-retry push and spent provider work on divergence; both guards detected it')
    pipeline.write_text(pipeline_original)

    anchor = "                if record['artifacts'] != self.committed_artifacts(directory, phase, commit): break"
    assert original.count(anchor) == 1
    spec.write_text(original.replace(anchor, '                if False: break  # Broken: trust the commit ID without hashing its blobs'))
    rejected('spec_resume_test.py', 'SpecWorkerTests.test_tampered_commit_cannot_supply_checkpoint_artifacts',
             env, "['plan', 'tasks', 'digest'] != ['specify', 'plan', 'tasks', 'digest']")
    print('PASS control: removing committed hash verification accepted the tampered commit and skipped specify; real reset guard detected it')
    spec.write_text(original)

    first = "value.get('input_sha256') == fingerprint"
    second = "                if record['input_sha256'] != fingerprint: break"
    assert original.count(first) == original.count(second) == 1
    spec.write_text(original.replace(first, 'True').replace(second, '                if False: break  # Broken: ignore new feedback'))
    rejected('spec_resume_test.py', 'SpecWorkerTests.test_changed_feedback_requires_fresh_specify',
             env, "['plan', 'tasks', 'digest'] != ['specify', 'plan', 'tasks', 'digest']")
    print('PASS control: ignoring the fingerprint skipped specify after new feedback; real worker call-count guard detected it')
    spec.write_text(original)

    anchor = '            return prefix\n'
    assert original.count(anchor) == 1
    spec.write_text(original.replace(anchor, '            return []  # Broken: never resume\n'))
    rejected('spec_resume_test.py', 'SpecWorkerTests.test_plan_timeout_resumes_plan_and_tasks_through_real_reset',
             env, "['specify', 'plan', 'specify', 'plan', 'tasks', 'digest']")
    print('PASS control: disabling reuse spent specify again after the plan timeout; guard detected it')
    spec.write_text(original)

    anchor = "    try: text = Path(path).read_bytes().decode('utf-8')"
    assert original.count(anchor) == 1
    spec.write_text(original.replace(anchor, '    return None  # Broken: trust model PASS\n' + anchor))
    rejected('spec_resume_test.py', 'SpecWorkerTests.test_model_pass_cannot_bypass_governance_validator', env, "'Build' != 'Spec'")
    rejected('shared_runtime_test.py', 'RuntimeTests.test_app_spec_and_review_completion_reject_invalid_governance_even_with_pass',
             {'BUREAU_TEST_RUNTIME_SCRIPT': str(root/'bureau-runtime.py')}, 'ValueError not raised')
    print('PASS control: disabling governance validation admitted model PASS in background and app completion; both guards detected it')
    spec.write_text(original)

    anchor = "    body = '\\n'.join(section)\n"
    assert original.count(anchor) == 1
    spec.write_text(original.replace(anchor, anchor + '    return None  # Broken: any named governance section qualifies\n'))
    rejected('spec_resume_test.py', 'GovernanceTests.test_reviewer_placeholder_and_german_decision_matrix',
             env, 'AssertionError: unexpectedly None')
    print('PASS control: skipping structural checks admitted placeholders; governance matrix detected it')
    spec.write_text(original)

    anchor = "    body = '\\n'.join(section)\n"
    assert original.count(anchor) == 1
    spec.write_text(original.replace(anchor, anchor + "    if 'wird definiert' in body or any(line.startswith(('Rationale:', 'Begründung:')) and not line.endswith('.') for line in body.splitlines()):\n        return 'Broken: punctuation/word heuristic'\n"))
    rejected('spec_resume_test.py', 'GovernanceTests.test_reviewer_placeholder_and_german_decision_matrix', env, 'AssertionError:')
    print('PASS control: punctuation/global-word heuristics rejected legitimate rationales; acceptance matrix detected it')
    spec.write_text(original)

    runtime = root/'bureau-runtime.py'; runtime_original = runtime.read_text()
    anchor = "        if spec_files[0].is_symlink() or spec_files[0].resolve() != expected / 'spec.md':"
    assert runtime_original.count(anchor) == 1
    runtime.write_text(runtime_original.replace(anchor, '        if False:  # Broken: another issue can supply governance'))
    rejected('shared_runtime_test.py', 'RuntimeTests.test_app_completion_rejects_another_tickets_valid_spec',
             {'BUREAU_TEST_RUNTIME_SCRIPT': str(runtime)}, 'ValueError not raised')
    print('PASS control: removing app issue binding admitted another ticket; guard detected it')
    runtime.write_text(runtime_original)

    review = root/'spec-review-pipeline.sh'; review_original = review.read_text()
    for anchor, case, expected in (
        ("|| echo 'WARN: checkpoint invalidation unavailable; FAIL feedback still requires fresh specify.' >&2",
         'test_review_invalidation_io_error_cannot_publish_pass_or_skip_rework', '24 != 0'),
        ("|| echo 'WARN: checkpoint invalidation unavailable; missing tasks still require re-work.' >&2",
         'test_missing_tasks_routes_to_spec_even_when_invalidation_fails', '24 != 13'),
    ):
        assert review_original.count(anchor) == 1
        review.write_text(review_original.replace(anchor, '|| exit 24 # Broken: optional invalidation changes routing'))
        rejected('spec_resume_test.py', 'SpecWorkerTests.' + case, env, expected)
        print('PASS control: fatal invalidation I/O broke existing recovery routing; guard detected it')
        review.write_text(review_original)

    anchor = 'case "$REVIEW_STATUS" in\n  FAIL)\n'
    assert review_original.count(anchor) == 1
    review.write_text(review_original.replace(anchor, 'case "${REVIEW_STATUS/FAIL/PASS}" in\n  FAIL)\n'))
    rejected('spec_resume_test.py', 'SpecWorkerTests.test_model_rejects_structurally_valid_but_inadequate_rationale', env, "'Build' != 'Spec'")
    print('PASS control: ignoring model FAIL admitted a structurally valid circular rationale; guard detected it')
    review.write_text(review_original)

    pipeline = root/'spec-pipeline.sh'; pipeline_original = pipeline.read_text()
    anchor = '   research: $research,'
    assert pipeline_original.count(anchor) == 1
    pipeline.write_text(pipeline_original.replace(anchor, ''))
    rejected('spec_resume_test.py', 'SpecWorkerTests.test_new_research_invalidates_specify_but_publication_does_not',
             env, "['tasks'] != ['specify', 'plan', 'tasks']")
    print('PASS control: omitting substantive research skipped specify/plan after new API evidence; guard detected it')
    pipeline.write_text(pipeline_original)

assert before == {name: hashlib.sha256((SCRIPTS/name).read_bytes()).hexdigest() for name in names}
print('Results: 22 mutation controls detected (27 guard runs); source hashes unchanged')
