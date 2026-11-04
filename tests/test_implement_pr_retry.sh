#!/bin/bash
# Real implement stage/local bare origin; fake gh and provider, with no network.
# An optional pipeline source runs the same assertions against a saved baseline.
set -euo pipefail
source "$(dirname "$0")/lib/harness.sh"
finish() {
  local rc="$?"
  if [ "$rc" != 0 ]; then printf '%s\n%s\n' "${LAST_STDOUT:-}" "${LAST_STDERR:-}" >&2; fi
  if [ -n "${SANDBOX:-}" ] && [ -d "$SANDBOX" ]; then teardown; fi
}
trap finish EXIT

setup() {
  sandbox_init test-item test-branch
  if [ -n "${BUREAU_TEST_IMPLEMENT_SOURCE:-}" ]; then
    cp "$BUREAU_TEST_IMPLEMENT_SOURCE" "$SCRIPTS_DIR/implement-pipeline.sh"
  fi
  export BUREAU_DRY_RUN=0 BUREAU_IMPL_MAX_ITER=1
  export FAKE_CLAUDE_COMMIT_ON_ITERS=1 GH_STUB_EXISTING_PR=7
  export FAKE_CLAUDE_FIXTURES="$FIXTURES_DIR/claude_complete.txt"
  export PR_RETRY_VERB="$1" PR_RETRY_FAILURES="$2" PR_RETRY_ERROR='HTTP 503: Service Unavailable'
  unset BUREAU_USE_GOAL_LOOP BUREAU_IMPL_PR_RETRIES BUREAU_IMPL_PR_RETRY_WAIT
  [ "$1" != create ] || unset GH_STUB_EXISTING_PR
  case "$3" in
    partial) export FAKE_CLAUDE_FIXTURES="$FIXTURES_DIR/claude_partial_progress.txt" ;;
    draft) export FAKE_CLAUDE_FIXTURES="$FIXTURES_DIR/claude_needs_human.txt" ;;
  esac
  EXPECTED_URL=$(GH_STUB_CALLS="$SANDBOX/expected-gh.log" "$LIB_DIR/bin/gh" pr view "${GH_STUB_EXISTING_PR:-99}" --json url --jq '.url')
  mkdir -p "$SANDBOX/.pr-retry-bin"
  cat > "$SANDBOX/.pr-retry-bin/gh" <<'SH'
#!/bin/bash
set -euo pipefail
verb="${2:-}"
printf '%s\n' "$verb" >> "$SANDBOX/.pr-retry-calls"
if [ "$verb" = "$PR_RETRY_VERB" ]; then
  # This invocation must come after the push, over the exact pushed head.
  head=$(git rev-parse HEAD)
  pushed=$(git --git-dir="$SANDBOX/.fake-origin.git" rev-parse test-branch)
  [ "$head" = "$pushed" ] || { echo 'PR call before the push' >&2; exit 1; }
  [ -f "$SANDBOX/.pr-retry-pushed-head" ] || printf '%s\n' "$pushed" > "$SANDBOX/.pr-retry-pushed-head"
  count=0
  [ ! -f "$SANDBOX/.pr-retry-count" ] || count=$(cat "$SANDBOX/.pr-retry-count")
  count=$((count + 1))
  printf '%s\n' "$count" > "$SANDBOX/.pr-retry-count"
  if [ "$PR_RETRY_FAILURES" = all ] || [ "$count" -le "$PR_RETRY_FAILURES" ]; then
    [ "$verb" = list ] || echo 'discard this failed output'
    echo "$PR_RETRY_ERROR" >&2
    exit 1
  fi
fi
# A failed list followed by create would collide with the existing PR.
if [ "$verb" = create ] && [ -n "${GH_STUB_EXISTING_PR:-}" ]; then
  echo 'a pull request already exists for this branch' >&2
  exit 1
fi
exec "$PR_RETRY_GH" "$@"
SH
  cat > "$SANDBOX/.pr-retry-bin/sleep" <<'SH'
#!/bin/bash
printf '%s\n' "$1" >> "$SANDBOX/.pr-retry-waits"
SH
  chmod +x "$SANDBOX/.pr-retry-bin/gh" "$SANDBOX/.pr-retry-bin/sleep"
  export PR_RETRY_GH="$LIB_DIR/bin/gh"
}

run_case() { PATH="$SANDBOX/.pr-retry-bin:$PATH" run_implement_pipeline test-item; }
count_calls() { grep -c "^$1$" "$SANDBOX/.pr-retry-calls" || true; }
assert_pushed() {
  local pushed
  pushed=$(cat "$SANDBOX/.pr-retry-pushed-head")
  assert_eq "$pushed" "$(git -C "$SANDBOX" rev-parse HEAD)" 'local head unchanged by PR calls'
  assert_eq "$pushed" "$(git --git-dir="$SANDBOX/.fake-origin.git" rev-parse test-branch)" 'pushed head unchanged by PR calls'
}
assert_held() {
  assert_eq 18 "$LAST_RC" 'PR failure is gh-failed'
  assert_match "HOLD: branch 'test-branch' is pushed; only the PR step is incomplete.*Re-run the implement stage" "$LAST_STDERR" 'actionable recovery message'
  assert_calls_exclude 'move_issue|post_comment|add_issue_label' 'no handoff after PR failure'
  assert_pushed
}

scenario="${1:-all}"
for route in complete partial draft; do
  for verb in list ready view create; do
    [ "$route:$verb" != draft:ready ] || continue
    for failure in once persistent; do
      name="$route-$verb-$failure"
      [ "$scenario" = all ] || [ "$scenario" = "$name" ] || continue
      failures=1; [ "$failure" != persistent ] || failures=all
      setup "$verb" "$failures" "$route"
      run_case
      if [ "$failure" = once ]; then
        assert_eq 0 "$LAST_RC" "$name recovered"
        assert_match "PR: $EXPECTED_URL" "$LAST_STDOUT" "$name URL"
        attempts=2
        # Halt paths also read the number for their existing escalation log.
        if [ "$route" != complete ] && [ "$verb" = list ]; then attempts=3; fi
        assert_eq "$attempts" "$(cat "$SANDBOX/.pr-retry-count")" "$name attempts"
        assert_eq 2 "$(cat "$SANDBOX/.pr-retry-waits")" "$name backoff"
        if [ "$route" = complete ]; then
          assert_calls_include 'move_issue.*state-build-review' "$name handoff"
        else
          assert_calls_exclude 'move_issue' "$name stays in Build"
        fi
        if [ "$verb" = create ]; then
          if [ "$route" = draft ]; then
            assert_file_contains $'^gh\tpr\tcreate\t--draft' "$SANDBOX/gh_calls.log" "$name draft flag"
          elif grep -q -- --draft "$SANDBOX/gh_calls.log"; then
            echo "FAIL $name created a draft" >&2; exit 1
          fi
        fi
        assert_pushed
      else
        assert_held
        assert_eq 4 "$(cat "$SANDBOX/.pr-retry-count")" "$name bounded attempts"
        assert_eq '2 4 8 ' "$(tr '\n' ' ' < "$SANDBOX/.pr-retry-waits")" "$name default backoff"
        [ "$verb" != list ] || assert_eq 0 "$(count_calls create)" "$name no duplicate PR"
        [ "$verb" != ready ] || assert_eq 0 "$(count_calls view)" "$name no view after failed ready"
      fi
      echo "PASS $name"
      teardown
    done
  done
done

if [ "$scenario" = all ]; then
  setup ready all complete
  export PR_RETRY_ERROR='Pull request fixture#7 is already "ready for review"'
  run_case
  assert_eq 0 "$LAST_RC" 'already-ready PR can finish on rerun'
  assert_match "PR: $EXPECTED_URL" "$LAST_STDOUT" 'already-ready URL'
  assert_calls_include 'move_issue.*state-build-review' 'already-ready handoff'
  assert_eq 1 "$(cat "$SANDBOX/.pr-retry-count")" 'already-ready is not retried'
  [ ! -f "$SANDBOX/.pr-retry-waits" ]
  assert_pushed
  echo 'PASS already-ready response is idempotent'
  teardown

  setup list all complete
  export PR_RETRY_ERROR='HTTP 403: Resource not accessible by integration'
  run_case
  assert_held
  assert_eq 1 "$(cat "$SANDBOX/.pr-retry-count")" 'permanent failure is not retried'
  assert_eq 0 "$(count_calls create)" 'permanent failed lookup cannot create'
  [ ! -f "$SANDBOX/.pr-retry-waits" ]
  echo 'PASS permanent failure holds without retry'
  teardown

  setup view all complete
  printf 'BUREAU_IMPL_PR_RETRIES=2\nBUREAU_IMPL_PR_RETRY_WAIT=1\n' >> "$SANDBOX/.env"
  run_case
  assert_held
  assert_eq 3 "$(cat "$SANDBOX/.pr-retry-count")" '.env retry count'
  assert_eq '1 2 ' "$(tr '\n' ' ' < "$SANDBOX/.pr-retry-waits")" '.env initial wait doubles'
  echo 'PASS .env retry settings'
  teardown

  setup view all complete
  export BUREAU_IMPL_PR_RETRIES=0 BUREAU_IMPL_PR_RETRY_WAIT=0
  run_case
  assert_held
  assert_eq 1 "$(cat "$SANDBOX/.pr-retry-count")" 'zero retries'
  [ ! -f "$SANDBOX/.pr-retry-waits" ]
  echo 'PASS zero retries'
  teardown

  setup view all complete
  export BUREAU_IMPL_PR_RETRIES=05 BUREAU_IMPL_PR_RETRY_WAIT=040
  run_case
  assert_held
  assert_eq 6 "$(cat "$SANDBOX/.pr-retry-count")" 'maximum retries in decimal'
  assert_eq '40 60 60 60 60 ' "$(tr '\n' ' ' < "$SANDBOX/.pr-retry-waits")" 'backoff caps at 60 seconds'
  echo 'PASS bounded settings and capped backoff'
  teardown

  setup view all complete
  export BUREAU_IMPL_PR_RETRIES=6 BUREAU_IMPL_PR_RETRY_WAIT=61
  run_case
  assert_held
  assert_eq 4 "$(cat "$SANDBOX/.pr-retry-count")" 'invalid retries use default'
  assert_eq '2 4 8 ' "$(tr '\n' ' ' < "$SANDBOX/.pr-retry-waits")" 'invalid wait uses default'
  echo 'PASS out-of-range settings use defaults'
  teardown
fi

if [ "$scenario" = all ] || [ "$scenario" = transport ]; then
  for message in 'HTTP 429: Too Many Requests' 'connection reset by peer' EOF 'context deadline exceeded'; do
    setup view 1 complete
    export PR_RETRY_ERROR="$message"
    cat > "$SCRIPTS_DIR/pr-retry-probe.sh" <<'SH'
#!/bin/bash
set -euo pipefail
source "$(dirname "$0")/bureau-config.sh"
bureau_gh_pr_retry test-branch view 7 --json url --jq '.url'
SH
    PATH="$SANDBOX/.pr-retry-bin:$PATH" run_pipeline pr-retry-probe.sh
    assert_eq 0 "$LAST_RC" "$message recovered"
    assert_eq "$EXPECTED_URL" "$LAST_STDOUT" "$message clean URL"
    assert_eq 2 "$(cat "$SANDBOX/.pr-retry-count")" "$message attempts"
    assert_eq 2 "$(cat "$SANDBOX/.pr-retry-waits")" "$message wait"
    assert_pushed
    echo "PASS transient $message"
    teardown
  done
fi
