"""Review regressions: real parsing, Git, adapter flags and evidence stores."""
import json
import os
import re
import subprocess
import sys
import unittest
from unittest.mock import patch

import review_merge_test as fixture

SCRIPTS = fixture.SCRIPTS


class MergeRegressions(unittest.TestCase):
    def setUp(self):
        self.f = fixture.MergeTests(); self.f.setUp(); self.addCleanup(self.f.doCleanups)
        self.m = self.f.m

    def finalize(self, grounding='off', **changes):
        _, _, metadata = self.f.merge()
        (self.f.outputs / 'merge-metadata.json').write_text(self.m.encoded(metadata))
        config = self.f.root / 'config.json'; config.write_text(json.dumps(dict(agents=dict(review_grounding=grounding))))
        result = dict(verdict='APPROVE', bugs=0, security_issues=0, missing_acceptance=[], fixes_needed=[],
                      summary='Empty fallback', comment='Empty fallback', findings=[])
        result.update(changes)
        output = self.f.outputs / 'fallback.txt'; output.write_text(json.dumps(result))
        args = ['bureau-review.py', 'fallback', '--directory', str(self.f.outputs), '--config', str(config),
                '--base', self.f.base, '--head', self.f.head, '--result', str(output)]
        with patch.object(sys, 'argv', args): self.assertEqual(self.m.main(), 0)
        return json.loads((self.f.outputs / 'merged.json').read_text()), json.loads((self.f.outputs / 'merge-metadata.json').read_text())

    def test_structured_holds_survive_convergence(self):
        for signal in ({'holds':['Acceptance needs a maintainer decision.']},
                       {'holds':['A safety decision remains.'], 'summary':'All checks completed.'}):
            with self.subTest(signal=signal):
                self.f.values['correctness'] = fixture.specialist('correctness')
                self.f.values['correctness'].update(signal)
                result, _, metadata = self.f.merge()
                self.assertIsNotNone(result)
                self.assertEqual(result['verdict'], 'BLOCK')
                self.assertGreater(metadata['human_holds'], 0)
                self.assertIn('## Human Review Holds', result['comment'])
                # The existing convergence path must not soften this candidate.
                source = (SCRIPTS / 'bureau-config.sh').read_text()
                helpers = '\n'.join(re.search(r'^' + name + r'\(\) \{\n.*?^\}', source, re.M | re.S).group(0)
                                    for name in ('parse_claude_json', 'review_convergence'))
                verdict = subprocess.check_output(['/bin/bash', '-c', helpers + '\nreview_convergence "$(cat)" BUG'],
                                                  input=json.dumps(result), text=True)
                self.assertEqual(verdict.strip(), 'BLOCK')

    def test_fallback_keeps_every_readable_role_and_acceptance_gap(self):
        self.f.values['correctness'] = fixture.specialist('correctness', [fixture.finding('Correctness BUG')],
                                                         acceptance_gaps=['Required behavior absent.'])
        self.f.values['security'] = fixture.specialist('security', [fixture.finding('Security MINOR', 'MINOR')])
        self.f.values['performance'] = fixture.specialist('performance', [fixture.finding('Readable malformed BUG')],
                                                        acceptance_gaps=['Performance acceptance absent.'])
        self.f.values['performance']['unknown'] = 'Schema fault must not erase the report.'
        result, metadata = self.finalize()
        self.assertEqual({f['msg'] for f in result['findings']},
                         {'Correctness BUG', 'Security MINOR', 'Readable malformed BUG'})
        self.assertEqual(result['bugs'], 2); self.assertEqual(result['security_issues'], 1)
        self.assertEqual(set(result['missing_acceptance']), {'Required behavior absent.', 'Performance acceptance absent.'})
        for f in result['findings']: self.assertIn(f['msg'], result['comment'])
        self.assertEqual(metadata['missing_acceptance_count'], 2)

    def test_count_only_severities_survive_unrelated_schema_faults(self):
        for role in self.m.ROLES:
            for severity in ('critical', 'bug', 'minor'):
                with self.subTest(role=role, severity=severity):
                    self.f.values = {r: fixture.specialist(r) for r in self.m.ROLES}
                    self.f.values[role]['counts'][severity] = 2
                    self.f.values[role]['unknown'] = True
                    _, _, metadata = self.f.merge()
                    self.assertGreaterEqual(metadata['severity_counts'][severity], 2)
                    if severity == 'critical': self.assertGreaterEqual(metadata['critical_floor'], 2)

    def assert_summary_fallback(self, summary, role='correctness', reason='summary_signal'):
        self.f.values = {r: fixture.specialist(r) for r in self.m.ROLES}
        self.f.values[role]['summary'] = summary
        result, findings, metadata = self.f.merge()
        self.assertIsNone(result)
        self.assertEqual(findings, [])
        self.assertEqual(metadata['mode'], 'model_fallback')
        self.assertEqual(metadata['malformed_specialists'], [])
        self.assertEqual(metadata['fallback_reasons'], {role:reason})
        self.assertFalse(any(metadata['severity_counts'].values()))
        self.assertEqual((metadata['critical_floor'], metadata['security_floor'], metadata['human_holds']), (0, 0, 0))
        fixture.load('evidence').review_merge_shape(metadata)

    def test_summary_severities_use_merger_without_inferred_floors(self):
        for role in self.m.ROLES:
            for severity in self.m.SEVERITY:
                with self.subTest(role=role, severity=severity):
                    self.assert_summary_fallback(severity + ': the recovery path needs assessment.', role)
        for summary in ('The recovery path has a critical defect.', 'No proof the CRITICAL defect is fixed.',
                        '_CRITICAL_: the recovery path deletes user data.', '__CRITICAL__: data loss.'):
            with self.subTest(summary=summary): self.assert_summary_fallback(summary)

    def test_summary_signals_use_merger_even_when_negated(self):
        summaries = ('No human review required.', 'No architectural disagreement remains.', 'Nothing blocks.',
                     'Human review is not required.', 'Human approval is no longer necessary.',
                     'No CRITICAL findings.', 'No BUG or MINOR defects remain.', 'No blocking issues remain.',
                     'Nothing is blocking this PR.', 'No unresolved architectural disagreement remains.',
                     'Not blocked.', 'This change is not blocked.', 'Nothing is blocked.',
                     'There is no need for human review.', 'No human review is required for this PR.',
                     '_No human review required._',
                     'No human review required, and no architectural disagreement remains.',
                     'No human review required. No architectural disagreement remains. Nothing blocks.',
                     'Human review required.', 'Architectural disagreement remains.', 'Something blocks.',
                     'A blocking defect remains in recovery.',
                     '__BLOCK__: the safety contract is unresolved.', '_NEEDS_HUMAN_: acceptance is unresolved.',
                     'Human review is required.', 'Human approval is still necessary.',
                     'No human review can resolve this ambiguity.', 'Human review is not clearly unnecessary.',
                     'No human review required unless the missing safety condition is met.',
                     'Not blocked unless the safety condition fails.', 'There is no proof that human review is unnecessary.',
                     'No human review required, but architectural disagreement remains.',
                     'No architectural disagreement remains, and human review is required.',
                     'Nothing blocks; however, human review is required.',
                     'No architectural disagreement remains. BLOCK: acceptance is unresolved.',
                     'A human-decision is pending.',
                     'NEEDS_HUMAN: acceptance is unclear.', 'The acceptance criteria are ambiguous.',
                     'Human **judgment** is required.', 'Architectural_disagreement remains.')
        for summary in summaries:
            with self.subTest(summary=summary): self.assert_summary_fallback(summary)
        # Typed holds and explicit legacy BLOCK verdicts remain authoritative
        # when the merger approves a summary that negates a hold.
        for fields in ({'holds':['Unresolved maintainer decision.']}, {'verdict':'BLOCK'}):
            self.f.values['correctness'] = fixture.specialist('correctness', **fields)
            self.f.values['correctness']['summary'] = summaries[0]
            result, metadata = self.finalize()
            self.assertEqual(result['verdict'], 'BLOCK')
            self.assertEqual(metadata['verdict_floor'], 'BLOCK')

    def test_unicode_case_summaries_require_merger(self):
        for summary in ('CR\u0130TICAL: the recovery path silently deletes persisted user data.',
                        'Human \u0130NPUT is required before this acceptance decision.'):
            for role in self.m.ROLES:
                with self.subTest(summary=summary, role=role):
                    self.assert_summary_fallback(summary, role, 'summary_non_ascii')
                    result, metadata = self.finalize(verdict='BLOCK')
                    self.assertEqual(result['verdict'], 'BLOCK')
                    self.assertEqual(metadata['fallback_reasons'], {role:'summary_non_ascii'})

    def test_summaries_outside_printable_ascii_require_merger(self):
        summaries = ['All checks passed.' + c for c in ('\x00', '\x1f', '\t', '\n', '\r', '\x7f',
                                                       '\u00a0', '\u200b', '\uff41', '\U0001f642')]
        summaries += ['Acceptance needs human\njudgement.', 'ＣＲＩＴＩＣＡＬ: recovery needs assessment.']
        for summary in summaries:
            with self.subTest(summary=summary):
                self.assert_summary_fallback(summary, reason='summary_non_ascii')

    def test_zero_severity_tallies_use_merger_without_floors(self):
        for summary in ('CRITICAL: 0; BUG: 0; MINOR: 0; SKIP: 0. All checks passed.',
                        'critical=0, bugs=0; no human review required.',
                        'No CRITICAL findings. No BUG findings. No architectural disagreement remains.'):
            for role in self.m.ROLES:
                with self.subTest(summary=summary, role=role):
                    self.assert_summary_fallback(summary, role)
                    result, metadata = self.finalize(summary=summary)
                    self.assertEqual(result['verdict'], 'APPROVE')
                    self.assertFalse(any(metadata['severity_counts'].values()))
                    self.assertEqual(metadata['critical_floor'], 0)
                    self.assertEqual(metadata['human_holds'], 0)

    def test_prose_around_json_never_uses_deterministic_merge(self):
        report = json.dumps(fixture.specialist('correctness', holds=[]))
        signals = ('BLOCK: ambiguous acceptance criteria require human judgment.',
                   'CRITICAL: recovery silently deletes persisted user data.')
        for signal in signals:
            for text in (signal + '\n' + report, signal + '\n```json\n' + report + '\n```',
                         '```json\n' + report + '\n```\n' + signal, report + '\n' + signal):
                with self.subTest(text=text):
                    self.f.values['correctness'] = text
                    result, _, metadata = self.f.merge()
                    self.assertIsNone(result)
                    self.assertEqual(metadata['mode'], 'model_fallback')
                    self.assertEqual(metadata['fallback_reasons'], {'correctness':'non_json_report'})

    def test_prose_fallback_retains_readable_claims_counts_gaps_and_holds(self):
        report = fixture.specialist('correctness', [fixture.finding('Persisted data is lost.', 'CRITICAL')],
                                    acceptance_gaps=['Safety behavior is missing.'], holds=['Maintainer decision needed.'])
        self.f.values['correctness'] = 'BLOCK: review the safety contract.\n```json\n' + json.dumps(report) + '\n```'
        result, metadata = self.finalize()
        self.assertEqual(result['verdict'], 'BLOCK')
        self.assertEqual(result['findings'][0]['msg'], 'Persisted data is lost.')
        self.assertEqual(result['missing_acceptance'], ['Safety behavior is missing.'])
        self.assertEqual(metadata['critical_floor'], 1)
        self.assertEqual(metadata['human_holds'], 1)
        self.assertEqual(metadata['fallback_reasons'], {'correctness':'non_json_report'})

    def test_cost_envelopes_preserve_report_purity(self):
        report = json.dumps(fixture.specialist('correctness'))
        self.f.values['correctness'] = dict(result=' \n' + report + '\n', usage=dict(input_tokens=1))
        result, _, metadata = self.f.merge()
        self.assertEqual(result['verdict'], 'APPROVE')
        self.assertEqual(metadata['mode'], 'deterministic')
        self.assertEqual(metadata['fallback_reasons'], {})
        for text in ('BLOCK: human review required.\n```json\n' + report + '\n```',
                     '```json\n' + report + '\n```'):
            with self.subTest(text=text):
                self.f.values['correctness']['result'] = text
                result, _, metadata = self.f.merge()
                self.assertIsNone(result)
                self.assertEqual(metadata['fallback_reasons'], {'correctness':'non_json_report'})

    def test_clean_pure_json_approves_deterministically(self):
        for summary in ('All checks passed. ~', ''.join(chr(c) for c in range(32, 127)), ''):
            with self.subTest(summary=summary):
                self.f.values = {r: dict(fixture.specialist(r, holds=[]), summary=summary) for r in self.m.ROLES}
                self.f.values['correctness'] = ' \n' + json.dumps(self.f.values['correctness']) + '\n'
                result, _, metadata = self.f.merge()
                self.assertEqual(result['verdict'], 'APPROVE')
                self.assertEqual(metadata['mode'], 'deterministic')
                self.assertEqual(metadata['fallback_reasons'], {})
                self.assertFalse(any(metadata['severity_counts'].values()))

    def test_fallback_summary_does_not_manufacture_a_verdict_floor(self):
        self.assert_summary_fallback('Human review required.')
        for severity in ('CRITICAL', 'BUG'):
            with self.subTest(severity=severity):
                result, metadata = self.finalize(summary=severity + ': 0 findings.', verdict='APPROVE')
                self.assertEqual(result['verdict'], 'APPROVE')
                self.assertEqual(metadata['severity_counts'][severity.lower()], 0)
                self.assertEqual(metadata['critical_floor'], 0)
        result, _ = self.finalize(verdict='BLOCK')
        self.assertEqual(result['verdict'], 'BLOCK')

    def test_fallback_reason_metadata_rejects_prose_and_unknown_roles(self):
        self.assert_summary_fallback('Human review required.')
        metadata = self.f.merge()[2]
        for reason in ({'correctness':'PRIVATE_PROSE'}, {'unknown':'summary_signal'}, ['summary_signal']):
            with self.subTest(reason=reason):
                with self.assertRaises(ValueError):
                    fixture.load('evidence').review_merge_shape(dict(metadata, fallback_reasons=reason))

    def test_completely_unreadable_specialist_cannot_fall_back_to_approval(self):
        self.f.values['performance'] = 'Unreadable specialist output'
        result, metadata = self.finalize()
        self.assertEqual(metadata['verdict_floor'], 'REQUEST_CHANGES')
        self.assertEqual(result['verdict'], 'REQUEST_CHANGES')

    def test_critical_class_survives_an_unreadable_message(self):
        for message in (None, [], '', '   '):
            with self.subTest(message=message):
                self.f.values['correctness'] = fixture.specialist('correctness',
                    [fixture.finding(message, 'CRITICAL')])
                self.f.values['correctness']['counts']['critical'] = 0
                _, metadata = self.finalize()
                self.assertEqual(metadata['critical_floor'], 1)

    def test_retained_minor_cannot_soften_an_already_blocked_fallback(self):
        self.f.values['correctness'] = fixture.specialist('correctness', [fixture.finding('Deferred claim', 'MINOR')])
        self.f.values['performance'] = 'Malformed specialist'
        result, _ = self.finalize(fixes_needed=['Deferred claim'])
        self.assertEqual(result['verdict'], 'BLOCK')

    def test_malformed_locations_are_counted_even_when_claim_is_unreadable(self):
        for fields in ({'line':'1'}, {'line':True}, {'line':-1}, {'line_end':'2'}, {'file':[]},
                       {'line':1, 'line_end':0}, {'line':'1', 'msg':None}):
            with self.subTest(fields=fields):
                self.f.values['correctness'] = fixture.specialist('correctness',
                    [fixture.finding(anchor={'side':'invalid'}, **fields)])
                result, _, metadata = self.f.merge()
                self.assertIsNone(result)
                self.assertEqual(metadata['invalid_anchors'], 1)
                self.assertEqual(metadata['anchor_checks'][0]['status'], 'location')
                _, metadata = self.finalize('required')
                self.assertEqual(metadata['invalid_anchors'], 1)

    def test_dedup_preserves_per_variant_security_actionability(self):
        self.f.values['correctness'] = fixture.specialist('correctness', [fixture.finding('Same claim', 'MINOR')])
        self.f.values['security'] = fixture.specialist('security', [fixture.finding('Same claim', 'SKIP')])
        result, findings, metadata = self.f.merge()
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0]['specialists'], ['correctness', 'security'])
        self.assertEqual(result['security_issues'], 0)
        self.assertEqual(metadata['security_floor'], 0)
        # A real actionable security variant still raises its own floor.
        self.f.values['security'] = fixture.specialist('security', [fixture.finding('Same claim', 'MINOR')])
        self.assertEqual(self.f.merge()[0]['security_issues'], 1)

    def test_shadow_output_cannot_rewrite_verdict_or_merge_floors(self):
        self.f.values['security'] = fixture.specialist('security', [fixture.finding('Critical claim', 'CRITICAL', anchor=self.f.anchor())])
        result, findings, metadata = self.f.merge()
        self.m.prepare_verifier(self.f.outputs, findings, self.f.base, self.f.head, [], metadata)
        metadata_path = self.f.outputs / 'merge-metadata.json'; metadata_path.write_text(self.m.encoded(metadata))
        merged = self.f.outputs / 'merged.json'; merged.write_text(self.m.encoded(result)); before = merged.read_bytes()
        expected = metadata['verifier'][0]
        output = self.f.outputs / 'shadow.txt'
        output.write_text(json.dumps(dict(finding_id=expected['finding_id'], anchor_sha256=expected['anchor_sha256'],
                                          outcome='refuted', verdict='APPROVE', critical_floor=0, human_holds=0)))
        with patch.object(sys, 'argv', ['bureau-review.py', 'verifier-result', '--directory', str(self.f.outputs),
                          '--request', str(self.f.outputs / 'verifier-0.prompt'), '--result', str(output)]):
            self.assertEqual(self.m.main(), 0)
        after = json.loads(metadata_path.read_text())
        self.assertEqual(after['verifier'][0]['outcome'], 'unverifiable')
        self.assertEqual({k:v for k,v in after.items() if k != 'verifier'}, {k:v for k,v in metadata.items() if k != 'verifier'})
        self.assertEqual(merged.read_bytes(), before)


class VerifierRegressions(unittest.TestCase):
    def setUp(self):
        # Use the real adapter and its fake CLIs, including the real argv parser.
        with patch.dict(os.environ, BUREAU_TEST_PROVIDER_SCRIPT=str(SCRIPTS / 'bureau-provider.py')):
            import provider_adapter_test as adapter
        self.f = adapter.ProviderTests(); self.f.setUp(); self.addCleanup(self.f.doCleanups)

    def test_claude_shadow_has_no_builtin_mcp_or_skill_tools(self):
        self.f.config.write_text(json.dumps(dict(agents=dict(runner='claude', headroom_wrap=True))))
        wrapper = self.f.bin / 'headroom'
        wrapper.write_text('#!/bin/sh\nshift 3\nexec claude "$@"\n'); wrapper.chmod(0o755)
        self.f.run_provider('--stage', 'code_review', '--phase', 'finding_verifier')
        argv = json.loads((self.f.root / 'argv.json').read_text())
        self.assertIn('--tools', argv); self.assertEqual(argv[argv.index('--tools') + 1], '')
        self.assertIn('--strict-mcp-config', argv)
        self.assertEqual(json.loads(argv[argv.index('--mcp-config') + 1]), {'mcpServers':{}})
        self.assertIn('--disable-slash-commands', argv)
        self.assertEqual(argv[argv.index('--setting-sources') + 1], '')
        self.assertEqual(json.loads(argv[argv.index('--settings') + 1]), {'disableAllHooks':True})
        self.assertNotIn('--dangerously-skip-permissions', argv)

    def test_codex_shadow_fails_closed_before_launching_tools(self):
        result = self.f.run_provider('--stage', 'code_review', '--phase', 'finding_verifier', code=22)
        self.assertIn('tool-free finding verifier is unavailable', result.stderr)
        self.assertFalse((self.f.root / 'argv.json').exists())


class AcceptanceRegressions(unittest.TestCase):
    def setUp(self):
        with patch.dict(os.environ, BUREAU_TEST_ACCEPTANCE_SCRIPT=str(SCRIPTS / 'bureau-acceptance.py')):
            import acceptance_insights_test as acceptance
        self.f = acceptance.AcceptanceInsightsTests(); self.f.setUp(); self.addCleanup(self.f.doCleanups)

    def deterministic(self, bad_role=None):
        e = self.f.store
        for role in fixture.load('review').ROLES:
            # Real specialists have no verdict schema and therefore no result digest.
            receipt = self.f.receipt(phase=role, structured_result=None, schema_sha256=None)
            if role == bad_role: receipt['end_head'] = self.f.base
            e.put_receipt(receipt)
        merge = dict(mode='deterministic', malformed_specialists=[], grounding='off', invalid_anchors=0,
                     anchor_checks=[], verifier=[], verifier_omitted=0, critical_floor=0, security_floor=0, adr_refs=[],
                     human_holds=0, verdict_floor='APPROVE', severity_counts=dict(critical=0, bug=1, minor=0, skip=0),
                     result_sha256='f' * 64, missing_acceptance_count=2, fixes_needed_count=1)
        return e.review_round(self.f.run, self.f.attempt, 1, 'BUG', 'APPROVE', 'REQUEST_CHANGES', merge)

    def test_deterministic_acceptance_uses_receipts_and_round_blockers(self):
        self.deterministic()
        brief = self.f.render(verdict='REQUEST_CHANGES', review_head=self.f.head)
        self.assertIn('Review receipt:', brief); self.assertIn('deterministic round 1', brief)
        self.assertIn('missing_acceptance: 2', brief); self.assertIn('fixes_needed: 1', brief)
        self.assertIn('Recorded final verdict: REQUEST_CHANGES', brief)
        self.assertNotIn('Review blockers/known limits: missing', brief)
        self.assertNotIn('PRIVATE_REVIEW_SENTINEL', brief)

    def test_deterministic_acceptance_rejects_uncorrelated_or_missing_receipts(self):
        self.deterministic(bad_role='performance')
        self.assertIn('Review receipt/verdict for this HEAD: missing', self.f.render())


class PipelineRegressions(unittest.TestCase):
    def setUp(self):
        self.f = fixture.PipelineTests(); self.f.setUp(); self.addCleanup(self.f.doCleanups)

    def assert_model_fallback(self, record, reason='summary_signal'):
        self.assertEqual(record['calls'], 4)
        self.assertEqual(record['merge']['mode'], 'model_fallback')
        self.assertEqual(record['merge']['fallback_reasons'], {'correctness':reason})
        self.assertEqual(record['merge']['critical_floor'], 0)
        self.assertEqual(record['merge']['security_floor'], 0)

    def test_human_summary_goes_to_merger_and_keeps_its_block(self):
        summary = 'BLOCK: ambiguous acceptance criteria require human judgment.'
        self.f.values['correctness']['summary'] = summary
        self.f.values['verdict'].update(verdict='BLOCK', comment=summary)
        record, body, capture = self.f.run_review(25)
        self.assert_model_fallback(record)
        self.assertEqual(record['final_verdict'], 'BLOCK')
        self.assertIn(summary, (capture / 'verdict.txt').read_text())
        self.assertIn(summary, body)

    def test_summary_only_critical_goes_to_merger(self):
        summary = 'CRITICAL: the recovery path silently deletes persisted user data.'
        self.f.values['correctness'].update(summary=summary, holds=[])
        self.f.values['verdict'].update(verdict='BLOCK', comment=summary)
        record, body, capture = self.f.run_review(25)
        self.assert_model_fallback(record)
        self.assertEqual(record['final_verdict'], 'BLOCK')
        self.assertIn(summary, (capture / 'verdict.txt').read_text())
        self.assertIn('**Verdict**: BLOCK', body)

    def test_summary_only_bug_goes_to_merger(self):
        self.f.values['correctness']['summary'] = 'BUG: an unresolved recovery defect.'
        self.f.values['verdict']['findings'] = [fixture.finding('Unresolved recovery defect.', 'BUG', file='task1.txt')]
        self.f.values['verdict']['bugs'] = 1
        record, _, _ = self.f.run_review(0)
        self.assert_model_fallback(record)
        self.assertEqual(record['final_verdict'], 'REQUEST_CHANGES')

    def test_summary_minor_merger_findings_respect_the_configured_floor(self):
        self.f.values['correctness']['summary'] = 'MINOR: an unresolved recovery defect.'
        self.f.values['verdict']['findings'] = [fixture.finding('Unresolved recovery defect.', 'MINOR', file='task1.txt')]
        record, _, _ = self.f.run_review(20)
        self.assert_model_fallback(record)
        self.assertEqual(record['final_verdict'], 'APPROVE')
        self.f.config['agents']['review_min_severity'] = 'MINOR'
        record, _, _ = self.f.run_review(0)
        self.assert_model_fallback(record)
        self.assertEqual(record['final_verdict'], 'REQUEST_CHANGES')

    def test_negated_holds_use_merger_and_allow_its_approval(self):
        self.f.values['correctness'].update(summary='No human review required.', holds=[])
        record, body, capture = self.f.run_review(20)
        self.assert_model_fallback(record)
        self.assertEqual(record['final_verdict'], 'APPROVE')
        self.assertEqual(record['merge']['human_holds'], 0)
        self.assertIn('No human review required.', (capture / 'verdict.txt').read_text())
        self.assertIn('**Verdict**: APPROVE', body)

    def test_prose_block_and_critical_before_json_are_sent_to_merger(self):
        report = json.dumps(self.f.values['correctness'])
        for signal in ('BLOCK: ambiguous acceptance criteria require human judgment.',
                       'CRITICAL: recovery silently deletes persisted user data.'):
            with self.subTest(signal=signal):
                self.f.values['correctness'] = signal + '\n```json\n' + report + '\n```'
                self.f.values['verdict'].update(verdict='BLOCK', comment=signal)
                record, body, capture = self.f.run_review(25)
                self.assert_model_fallback(record, 'non_json_report')
                self.assertEqual(record['final_verdict'], 'BLOCK')
                self.assertIn(signal, (capture / 'verdict.txt').read_text())
                self.assertIn('**Verdict**: BLOCK', body)

    def test_zero_severity_tallies_use_merger_and_allow_its_approval(self):
        self.f.values['correctness']['summary'] = 'CRITICAL: 0; BUG: 0; MINOR: 0; SKIP: 0. All checks passed.'
        record, body, capture = self.f.run_review(20)
        self.assert_model_fallback(record)
        self.assertFalse(any(record['merge']['severity_counts'].values()))
        self.assertEqual(record['merge']['human_holds'], 0)
        self.assertIn(self.f.values['correctness']['summary'], (capture / 'verdict.txt').read_text())
        self.assertEqual(record['final_verdict'], 'APPROVE')
        self.assertIn('**Verdict**: APPROVE', body)

    def test_unreadable_specialist_requires_changes_after_empty_fallback(self):
        self.f.values['performance'] = 'Not a structured report'
        record, body, _ = self.f.run_review(0)
        self.assertEqual(record['final_verdict'], 'REQUEST_CHANGES')
        self.assertIn('**Verdict**: REQUEST_CHANGES', body)

    def test_fallback_does_not_clamp_an_unreadable_security_count_to_zero(self):
        self.f.values['performance'] = 'Malformed specialist'
        self.f.values['verdict']['security_issues'] = -1
        record, _, _ = self.f.run_review(25)
        self.assertEqual(record['final_verdict'], 'BLOCK')

    def test_fallback_count_only_critical_cannot_approve(self):
        self.f.values['correctness']['counts']['critical'] = 1
        self.f.values['correctness']['unknown'] = True
        record, _, _ = self.f.run_review(25)
        self.assertEqual(record['merge']['critical_floor'], 1); self.assertEqual(record['final_verdict'], 'BLOCK')

    def test_critical_without_readable_message_cannot_approve(self):
        self.f.values['correctness']['findings'] = [dict(self.f.claim('CRITICAL'), msg=None)]
        record, _, _ = self.f.run_review(25)
        self.assertEqual(record['merge']['critical_floor'], 1); self.assertEqual(record['final_verdict'], 'BLOCK')

    def test_fallback_bug_gap_and_malformed_location_cannot_approve(self):
        self.f.config['agents']['review_grounding'] = 'required'
        claim = self.f.claim('BUG', invalid=True); claim['line'] = '1'
        self.f.values['correctness'] = fixture.specialist('correctness', [claim], acceptance_gaps=['Missing acceptance.'])
        record, body, _ = self.f.run_review(25)
        self.assertEqual(record['merge']['invalid_anchors'], 1); self.assertEqual(record['final_verdict'], 'BLOCK')
        self.assertIn('Missing acceptance.', body)

    def test_security_skip_does_not_promote_deferred_correctness_minor(self):
        self.f.values['correctness'] = fixture.specialist('correctness', [self.f.claim('MINOR')])
        self.f.values['security'] = fixture.specialist('security', [self.f.claim('SKIP')])
        record, body, _ = self.f.run_review(20)
        self.assertEqual(record['merge']['security_floor'], 0)
        self.assertIn('**Verdict**: APPROVE', body)

    def test_real_deterministic_worker_has_acceptance_evidence_without_verdict_receipt(self):
        record, _, _ = self.f.run_review(20)
        acceptance = fixture.load('acceptance')
        store = acceptance.e.Store(self.f.repo / '.git/bureau')
        receipts = [r for _, r in store._records('runs/*/receipts/*.json', 'receipt', quarantine=False)]
        self.assertEqual({r['phase'] for r in receipts}, set(fixture.load('review').ROLES))
        self.assertTrue(all(r['result_sha256'] is None for r in receipts))
        inputs = receipts[0]['review_inputs']
        brief = acceptance.render(store, self.f.repo, 'T-1', 1, inputs['head'], 'https://example.invalid/pull/1',
                                  base=inputs['base'], verdict=record['final_verdict'], review_head=inputs['head'])
        self.assertIn('Review receipt:', brief)
        self.assertIn('deterministic round 1', brief)
        self.assertIn('missing_acceptance: 0', brief)
        self.assertIn('fixes_needed: 0', brief)
        self.assertNotIn('Review receipt/verdict for this HEAD: missing', brief)


if __name__ == '__main__': unittest.main()
