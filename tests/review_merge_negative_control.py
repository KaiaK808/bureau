"""Unchanged positive assertions must reject private-copy mutations."""
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
controls = [
    ('drop-performance', 'if claim: claims.append((role, claim))',
     "if claim and role != 'performance': claims.append((role, claim))",
     'MergeTests.test_each_specialist_is_preserved_and_template_is_stable'),
    ('bypass-anchors', "status, _ = anchor_check(finding, base, head) if isinstance(finding, dict) else ('location', None)",
     "status, _ = 'valid', None",
     'MergeTests.test_grounding_metadata_cannot_hide_invalid_anchor'),
    ('reload-per-finding', '@lru_cache(maxsize=None)\n', '',
     'MergeTests.test_many_findings_reuse_imports_and_still_validate_every_location'),
    ('questions-become-holds', 'questions.extend(validated_questions[role] if role in validated_questions else human_questions(raw))',
     'holds.extend(validated_questions[role] if role in validated_questions else human_questions(raw))', 'MergeTests.test_questions_are_advisory_and_stably_deduplicated'),
    ('questions-lost', 'questions.extend(validated_questions[role] if role in validated_questions else human_questions(raw))',
     'questions.extend([])', 'MergeTests.test_questions_are_advisory_and_stably_deduplicated'),
    ('late-questions-revalidated', "if (directory / (role + '.independent.txt')).is_file():\n"
     "                combined = module('impeccable').supplement(directory, role, refs)\n"
     "                validated_questions[role] = combined.get('questions', [])\n"
     "                core = dict(combined); core.pop('questions', None)\n"
     "                values[role] = load_specialist(encoded(core), role, refs)\n"
     "                if 'questions' in combined: values[role]['questions'] = combined['questions']\n"
     "            else:\n                values[role] = load_specialist(text, role, refs)", 'values[role] = load_specialist(text, role, refs)',
     'DispositionTests.test_late_questions_reconstruct_evidence_beyond_the_per_report_limit'),
    ('combined-core-unvalidated', 'values[role] = load_specialist(encoded(core), role, refs)', 'values[role] = core',
     'MergeTests.test_late_questions_do_not_bypass_combined_findings_validation'),
    ('question-render-cap-lost', 'MAX_RENDER_QUESTIONS = 32', 'MAX_RENDER_QUESTIONS = 999',
     'MergeTests.test_question_render_cap_keeps_the_full_stable_union'),
    ('fallback-report-rewritten', '### Correctness\n$CORRECTNESS_REVIEW\n\n### Security',
     '### Correctness\n{}\n\n### Security',
     'PipelineRegressions.test_repeated_critical_summary_and_fields_reach_merger_unchanged'),
    ('fallback-appended-questions-lost',
     "result['comment'] += '\\n\\n' + render(findings, retained['summaries'], result['missing_acceptance'], retained['holds'], retained['questions'])",
     "result['comment'] += '\\n\\n' + render(findings, retained['summaries'], result['missing_acceptance'], retained['holds'])",
     'MergeRegressions.test_fallback_merger_echo_is_published_as_model_output'),
]
for name, old, new, test in controls:
    with tempfile.TemporaryDirectory(prefix='bureau-review-control-') as directory:
        scripts = Path(directory).resolve() / 'scripts'
        shutil.copytree(ROOT / 'templates/scripts', scripts, ignore=shutil.ignore_patterns('__pycache__'))
        helper = scripts / ('code-review-pipeline.sh' if test.startswith('PipelineRegressions.') else 'bureau-review.py')
        text = helper.read_text()
        if text.count(old) != 1: sys.exit('Mutation target changed: ' + name)
        helper.write_text(text.replace(old, new, 1))
        program = ('review_merge_dispositions_test.py' if test.startswith('DispositionTests.') else
                   'review_round1_test.py' if test.startswith(('MergeRegressions.', 'PipelineRegressions.')) else 'review_merge_test.py')
        completed = subprocess.run([sys.executable, str(ROOT / 'tests' / program), test], cwd=ROOT,
                                   env={**os.environ, 'BUREAU_TEST_REVIEW_SCRIPTS':str(scripts), 'PYTHONDONTWRITEBYTECODE':'1'},
                                   capture_output=True, text=True, timeout=90 if test.startswith('PipelineRegressions.') else 30)
        if completed.returncode == 0 or 'AssertionError' not in completed.stderr:
            sys.exit(name + ' did not fail its positive assertion:\n' + completed.stdout + completed.stderr)
        print('PASS negative control: ' + name + ' rejected by unchanged positive test')
print(str(len(controls)) + ' negative controls passed')
