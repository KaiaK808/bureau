"""Run evidence guards against deliberately broken private copies, never the source."""
import hashlib
from pathlib import Path
import shutil
import sys
import tempfile

from mutation_checks import mutation_caught, snapshot_files

ROOT=Path(__file__).resolve().parents[1]
SCRIPTS=ROOT/'templates/scripts'
names=('bureau-provider.py','bureau-runtime.py','bureau-evidence.py','bureau-gates.py','bureau-activity.py','bureau-spec.py','bureau-supervision.py','bureau-tick.sh',
       'merge-pipeline.sh','bureau-config.sh','bureau-env.sh','bureau-insights.sh','code-review-pipeline.sh')
before={name:hashlib.sha256((SCRIPTS/name).read_bytes()).hexdigest() for name in names}


def rejected(test, cases, env, expected):
    mutation_caught([sys.executable,str(ROOT/'tests'/test),*cases],env=env,
                    originals=originals,
                    timeout=30,expected=expected)


with tempfile.TemporaryDirectory(prefix='bureau evidence controls ') as scratch:
    root=Path(scratch).resolve()
    for name in names: shutil.copy(SCRIPTS/name,root/name)
    originals=snapshot_files(root)
    provider=root/'bureau-provider.py'; original=provider.read_text()
    anchor='        path = store.put_receipt(receipt)\n'
    assert original.count(anchor)==1
    delayed=original.replace(anchor,"        path = store.receipt_path(run_id, attempt)\n        options['_delayed_receipt'] = (store, receipt)\n")
    anchor="        else: note('Bureau provider outcome: '+metadata['outcome'])"
    assert delayed.count(anchor)==1
    delayed=delayed.replace(anchor,"            if options.get('_delayed_receipt'):\n                saved_store, saved_receipt = options['_delayed_receipt']\n                saved_store.put_receipt(saved_receipt)\n"+anchor)
    provider.write_text(delayed)
    env={'BUREAU_TEST_PROVIDER_SCRIPT':str(provider)}
    rejected('provider_adapter_test.py',['ProviderTests.test_completion_receipt_is_durable_before_stdout'],env,
             'result forwarded before receipt')
    rejected('provider_adapter_test.py',['ProviderTests.test_prose_or_missing_forwarded_result_recovers_only_own_receipt'],env,
             'no matching completion receipt for this attempt')
    print('PASS control: receipt after stdout brings back the lost result and fails the ordering guard')
    provider.write_text(original)

    evidence=root/'bureau-evidence.py'; original_evidence=evidence.read_text()
    anchor="        value = read(path, {})\n        keys = ('run_id'"
    assert original_evidence.count(anchor)==1
    mixed=original_evidence.replace(anchor,"        if not path.exists(): path = next((self.root / 'runs').glob('*/receipts/*.json'))\n        value = read(path, {})\n        keys = ('run_id'",1)
    anchor="        if value.get('schema_version') != VERSION or any(value.get(k) != identity.get(k) for k in keys): return None"
    assert mixed.count(anchor)==1
    mixed=mixed.replace(anchor,'        # Broken: accept any ticket or attempt receipt.')
    evidence.write_text(mixed)
    rejected('provider_adapter_test.py',['ProviderTests.test_foreign_attempt_in_another_path_is_never_used'],env,'AssertionError: 0 != 22')
    print('PASS control: accepting any receipt mixes up tickets and the foreign-attempt guard fails')
    evidence.write_text(original_evidence)

    anchor="value.update(last_observed_at=now, reason_sha256=text_digest(reason),"
    assert original_evidence.count(anchor)==1
    evidence.write_text(original_evidence.replace(anchor,anchor.replace('last_observed_at=now','last_observed_at=now, reached_at=now')))
    rejected('supervision_pipeline_test.py',['WaitingRegisterTests.test_repeated_stops_preserve_first_reached_across_restart'],
             {'BUREAU_TEST_EVIDENCE_SCRIPT':str(evidence)},'160 != 100')
    print('PASS control: resetting reached on every tick fails the first-reached guard')
    evidence.write_text(original_evidence)

    anchor="            value['attempts'].append(attempt); self._save_manifest(value)"
    assert original_evidence.count(anchor)==1
    evidence.write_text(original_evidence.replace(anchor,'            self._save_manifest(value)  # Broken: omit stage attempts'))
    rejected('shared_runtime_test.py',['RuntimeTests.test_nested_execute_records_parent_and_every_stage_exit'],
             {'BUREAU_TEST_RUNTIME_SCRIPT':str(root/'bureau-runtime.py')},'None != 22')
    print('PASS control: omitting stage attempts fails the complete-run guard')
    evidence.write_text(original_evidence)

    anchor='                     structured_result=None, result_sha256='
    assert original_evidence.count(anchor)==1
    evidence.write_text(original_evidence.replace(anchor,"                     structured_result=receipt.get('structured_result'), result_sha256="))
    rejected('provider_adapter_test.py',['ProviderTests.test_entire_store_contains_only_metadata_and_digests'],
             env,'PRIVACY_SENTINEL')
    print('PASS fix 1 control: retaining result prose fails the whole-store byte scan')
    evidence.write_text(original_evidence)

    gate_evidence=root/'bureau-evidence.py'; gate_original=gate_evidence.read_text()
    anchor="        value.update(start_head=head(receipt.get('start_head')), end_head=head(receipt.get('end_head')))"
    assert gate_original.count(anchor)==1
    gate_evidence.write_text(gate_original.replace(anchor,anchor+"\n        value['raw_gate_output'] = (Path(receipt['provider_logs'])/'stdout.log').read_text()"))
    rejected('provider_adapter_test.py',['ProviderTests.test_gate_evidence_store_contains_only_metadata_and_digests'],
             {'BUREAU_TEST_PROVIDER_SCRIPT':str(provider)},'gate output leaked into evidence')
    print('PASS PR6 privacy control: storing raw gate output fails the whole-store scan')
    gate_evidence.write_text(gate_original)

    runtime=root/'bureau-runtime.py'; original_runtime=runtime.read_text()
    anchor='        begun = e.observe(evidence.begin_run, run, args.issue, repo, evidence_head(repo))'
    assert original_runtime.count(anchor)==1
    runtime.write_text(original_runtime.replace(anchor,'        begun = evidence.begin_run(run, args.issue, repo, evidence_head(repo))'))
    rejected('shared_runtime_test.py',['RuntimeTests.test_enospc_manifest_attempt_gate_wait_and_pruning_are_observational'],
             {'BUREAU_TEST_RUNTIME_SCRIPT':str(runtime)},'injected full device')
    print('PASS fix 2 control: mandatory manifest startup stops the stage on ENOSPC')
    runtime.write_text(original_runtime)

    anchor='        child = store.spawn(run, command, repo, env='
    assert original_runtime.count(anchor)==1
    runtime.write_text(original_runtime.replace(anchor,"        e.observe(evidence.resolve_waits, args.issue, ['ownership'], 'observed-ownership-resume')\n"+anchor))
    rejected('shared_runtime_test.py',['RuntimeTests.test_repeated_execute_ownership_refusals_keep_one_wait_until_stage_succeeds'],
             {'BUREAU_TEST_RUNTIME_SCRIPT':str(runtime)},'ownership refusal reopened the wait')
    print('PASS fix 3 control: premature lease-only resolution resets repeated execute waits')
    runtime.write_text(original_runtime)

    supervision=root/'bureau-supervision.py'; original_supervision=supervision.read_text()
    anchor='               resolved_at=merged_at, terminal=terminal)'
    assert original_supervision.count(anchor)==1
    supervision.write_text(original_supervision.replace(anchor,anchor+"\n        if terminal: wait_evidence(root, 'resolve_waits', issue, terminal=True)"))
    rejected('supervision_pipeline_test.py',['SupervisionPipelineTests.test_stale_pr_observation_cannot_resolve_replacement_boundary'],
             {'BUREAU_TEST_SUPERVISION_SCRIPT':str(supervision)},'stale PR observation resolved a newer wait')
    print('PASS fix 4 control: unconditional old-PR resolution closes the replacement generation')
    supervision.write_text(original_supervision)

    anchor="                    or (resolved_at is not None and resolved_at < value['reached_at'])):"
    assert original_evidence.count(anchor)==1
    evidence.write_text(original_evidence.replace(anchor,'                    ):'))
    rejected('supervision_pipeline_test.py',['WaitingRegisterTests.test_unknown_or_impossible_resolution_time_is_never_invented'],
             {'BUREAU_TEST_EVIDENCE_SCRIPT':str(evidence)},'stale resolution timestamp closed wait')
    print('PASS fix 4 time control: accepting a predating timestamp fails the stale-time guard')
    evidence.write_text(original_evidence)

    tick=root/'bureau-tick.sh'; original_tick=tick.read_text()
    anchor='\nif declare -F bureau_sweep_waits >/dev/null; then bureau_sweep_waits || true; fi\n'
    assert original_tick.count(anchor)==1
    tick.write_text(original_tick.replace(anchor,'\n'))
    rejected('supervision_pipeline_test.py',['SupervisionPipelineTests.test_bounded_wait_sweep_closes_operator_completion_in_both_adopter_profiles'],
             {'BUREAU_TEST_TICK_SCRIPT':str(tick)},'wait sweep did not run')
    print('PASS fix 5 control: omitting the tick sweep leaves operator-completed waits open in both profiles')
    tick.write_text(original_tick)

    anchor="        files = [p for p in self.root.rglob('*') if p.is_file() and p.name != 'guard']"
    assert original_evidence.count(anchor)==1
    evidence.write_text(original_evidence.replace(anchor,'        return  # Broken: no global capacity enforcement\n'+anchor))
    rejected('shared_runtime_test.py',['RuntimeTests.test_global_record_and_size_caps_evict_oldest_completed_and_protect_live_data'],
             {'BUREAU_TEST_RUNTIME_SCRIPT':str(runtime)},'global record cap exceeded')
    print('PASS cap control: count/age-only retention fails the global-budget guard')
    evidence.write_text(original_evidence)

    merge=root/'merge-pipeline.sh'; original_merge=merge.read_text()
    anchor='  if ! ( printf \'%s\\n\' "${2:-all gates PASS}" > "$file" ); then\n    rm -f "$file" || true\n    return 0\n  fi'
    assert original_merge.count(anchor)==1
    merge.write_text(original_merge.replace(anchor,'  printf \'%s\\n\' "${2:-all gates PASS}" > "$file"'))
    rejected('evidence_capture_test.py',['CaptureTests.test_merge_capture_cleanup_and_sink_failures_preserve_block_exit'],
             {'BUREAU_TEST_MERGE_SCRIPT':str(merge)},'optional merge capture changed exit 25')
    print('PASS round 2 finding 1 capture control: unguarded capture replaces blocked exit 25')
    merge.write_text(original_merge)

    anchor='    end = e.observe(current_head, repo)'
    assert original.count(anchor)==1
    provider.write_text(original.replace(anchor,'    end = current_head(repo)'))
    rejected('provider_adapter_test.py',['ProviderTests.test_evidence_identity_lookup_failures_preserve_provider_outcomes'],
             env,'22 != 124')
    print('PASS round 2 finding 1 identity control: post-provider HEAD failure replaces timeout 124')
    provider.write_text(original)

    anchor="        except Exception as exc:\n            raise CapacityError('evidence capacity could not be established') from exc"
    assert original_evidence.count(anchor)==1
    evidence.write_text(original_evidence.replace(anchor,'        except Exception: warn()  # Broken: write without confirmed capacity.'))
    rejected('shared_runtime_test.py',['RuntimeTests.test_undeletable_completed_run_cannot_bypass_capacity'],
             {'BUREAU_TEST_RUNTIME_SCRIPT':str(runtime)},'housekeeping failure bypassed capacity')
    print('PASS round 2 finding 2 control: swallowed prune failure bypasses the global cap')
    evidence.write_text(original_evidence)

    anchor="            return [(issue, [v for v in result if v['issue'] == issue]) for issue in issues]"
    assert original_evidence.count(anchor)==1
    evidence.write_text(original_evidence.replace(anchor,"            for value in result:\n                value['last_checked_at'] = self.clock()\n                self._write(self.root / 'waits' / (value['wait_id'] + '.json'), value)\n"+anchor))
    anchor="                e.observe(evidence.observation_attempted, issue, [v['wait_id'] for v in records])"
    assert original_runtime.count(anchor)==1
    runtime.write_text(original_runtime.replace(anchor,'                pass  # Broken: selection already advanced the entire batch.'))
    rejected('shared_runtime_test.py',['RuntimeTests.test_slow_ticket_does_not_advance_unvisited_observation_cursors'],
             {'BUREAU_TEST_RUNTIME_SCRIPT':str(runtime)},'open waits starved across four sweeps')
    print('PASS round 2 finding 3 control: selection-only cursor advancement starves TEAM-2..4 for four sweeps')
    evidence.write_text(original_evidence)
    runtime.write_text(original_runtime)

    anchor="        try: os.write(2, b'bureau evidence: WARN: optional evidence unavailable or invalid; work continues\\n')"
    assert original_evidence.count(anchor)==1
    evidence.write_text(original_evidence.replace(anchor,"        try: print('bureau evidence: WARN: optional evidence unavailable or invalid; work continues', file=sys.stderr)"))
    rejected('shared_runtime_test.py',['RuntimeTests.test_closed_stderr_finalization_preserves_stage_exits'],
             {'BUREAU_TEST_RUNTIME_SCRIPT':str(runtime)},'broken stderr changed stage exit')
    print('PASS round 3 finding 1 control: buffered warning changes closed-stderr finalization exits to 120')
    evidence.write_text(original_evidence)

    anchor='        try: self._prune(len(encoded(value)), 1, protect, path)'
    assert original_evidence.count(anchor)==1
    evidence.write_text(original_evidence.replace(anchor,
        "        old_size = path.stat().st_size if path.exists() else 0\n        try: self._prune(len(encoded(value)) - old_size, int(not path.exists()), protect, path)"))
    rejected('shared_runtime_test.py',['RuntimeTests.test_atomic_staging_and_failed_cleanup_stay_within_budget'],
             {'BUREAU_TEST_RUNTIME_SCRIPT':str(runtime)},'atomic staging exceeded budget')
    print('PASS round 3 finding 2 control: final-size-only budgeting exceeds the one-file cap during atomic staging')
    evidence.write_text(original_evidence)

    anchor='        self._observation_attempts[issue] = self._observation_turn'
    assert original_evidence.count(anchor)==1
    evidence.write_text(original_evidence.replace(anchor,'        pass  # Broken: rely only on refused persistent cursor writes.'))
    rejected('shared_runtime_test.py',['RuntimeTests.test_refused_cursors_do_not_starve_open_waits'],
             {'BUREAU_TEST_RUNTIME_SCRIPT':str(runtime)},'refused cursors starved open waits across four sweeps')
    print('PASS round 3 finding 3 control: losing in-memory attempts starves TEAM-2..4 when cursor writes fail')
    evidence.write_text(original_evidence)

    config=root/'bureau-config.sh'; original_config=config.read_text()
    review=root/'code-review-pipeline.sh'; original_review=review.read_text()
    for path,source,diagnostic in (
            (config,original_config,"echo 'bureau evidence: WARN: optional evidence unavailable or invalid; work continues' >&2"),
            (merge,original_merge,"echo 'bureau: WARN: merge evidence log unavailable' >&2"),
            (review,original_review,'echo "code-review failed (exit $rc). Specialist outputs preserved at $REVIEW_TMP" >&2')):
        anchor='( '+diagnostic+' ) || true'
        assert source.count(anchor)==1
        path.write_text(source.replace(anchor,diagnostic+' || true'))
    rejected('evidence_capture_test.py',[
        'CaptureTests.test_optional_shell_warnings_preserve_exits_and_routing_with_closed_stderr',
        'CaptureTests.test_review_and_qa_cleanup_failures_preserve_original_exit'],
        {'BUREAU_TEST_CONFIG_SCRIPT':str(config),'BUREAU_TEST_MERGE_SCRIPT':str(merge),'BUREAU_TEST_REVIEW_SCRIPT':str(review)},
        'optional shell warning changed stage exit')
    print('PASS round 4 finding 1 control: unprotected Bash diagnostics lose stage exits/routing on closed stderr')
    config.write_text(original_config); merge.write_text(original_merge); review.write_text(original_review)

    anchor='            if not persisted:\n'
    assert original_evidence.count(anchor)==1
    evidence.write_text(original_evidence.replace(anchor,'            if rotation is not None or not persisted:\n'))
    anchor='_BUREAU_WAIT_SWEEP_TURN=$((_BUREAU_WAIT_SWEEP_TURN / (60 * BUREAU_POLL_INTERVAL)))'
    assert original_config.count(anchor)==1
    config.write_text(original_config.replace(anchor,'_BUREAU_WAIT_SWEEP_TURN=$((_BUREAU_WAIT_SWEEP_TURN / 10))'))
    rejected('shared_runtime_test.py',['RuntimeTests.test_periodic_ticks_preserve_cursors_and_progress_when_writes_are_refused'],
             {'BUREAU_TEST_RUNTIME_SCRIPT':str(runtime),'BUREAU_TEST_CONFIG_SCRIPT':str(config)},
             ('rotation overrode writable persisted cursor order','periodic ticks starved open waits across N ticks'))
    config.write_text(original_config)
    anchor='                    self._prune(cursor_capacity, 1, writing=path)\n'
    assert original_evidence.count(anchor)==1
    evidence.write_text(original_evidence.replace(anchor,''))
    rejected('shared_runtime_test.py',['RuntimeTests.test_periodic_ticks_preserve_cursors_and_progress_when_writes_are_refused'],
             {'BUREAU_TEST_RUNTIME_SCRIPT':str(runtime),'BUREAU_TEST_CONFIG_SCRIPT':str(config)},
             'periodic ticks starved open waits across N ticks')
    print('PASS round 4 finding 2 control: overriding persisted order/ten-second seeds/unchanged-size-only probes fail periodic cursor guards')
    evidence.write_text(original_evidence)

    anchor='  if ! ( printf \'%s\\n\' "${2:-all gates PASS}" > "$file" ); then'
    assert original_merge.count(anchor)==1
    merge.write_text(original_merge.replace(anchor,'  if ! printf \'%s\\n\' "${2:-all gates PASS}" > "$file"; then'))
    rejected('evidence_capture_test.py',[
        'CaptureTests.test_merge_capture_redirection_preserves_exits_and_routing_with_closed_stderr'],
        {'BUREAU_TEST_MERGE_SCRIPT':str(merge)},'optional merge redirection changed stage exit')
    print('PASS round 5 finding 1 control: unprotected capture redirection loses exits/routing to SIGPIPE, including || true')
    merge.write_text(original_merge)

    anchor='                    try: self._write(path, value)\n                    except Exception: warn()'
    assert original_evidence.count(anchor)==1
    evidence.write_text(original_evidence.replace(anchor,'                    self._write(path, value)'))
    rejected('shared_runtime_test.py',['RuntimeTests.test_differently_sized_waits_do_not_starve_readable_tickets'],
             {'BUREAU_TEST_RUNTIME_SCRIPT':str(runtime)},
             'differently sized waits starved readable tickets across N sweeps')
    print('PASS round 5 finding 2 control: aborting on the larger wait starves readable tickets in retained/fresh stores')
    evidence.write_text(original_evidence)

assert before=={name:hashlib.sha256((SCRIPTS/name).read_bytes()).hexdigest() for name in names}
print('Results: 23 negative controls detected; source hashes unchanged')
