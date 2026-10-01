#!/bin/bash
# A check run that no runner takes is blocked after a grace (v3.2, O3).
#
# A check run on the PR head that stays "queued" (an offline self-hosted runner, a runner
# label nothing serves) counted as pending for ever, so the merge
# gate stayed "not yet" for ever: the queue picked the ticket on every poll without an
# alert, and the shepherd waited out its budget. Once a check run has been queued longer
# than agents.merge_ci_queued_grace_seconds (default 3600), measured from its started_at
# (which GitHub sets when it queues the run), the gate reports it as its own line, which
# the merge stage reads as blocked (25).
#
#   merge   the merge stage on its own: the line, the boundaries of the grace, the
#           statuses that do not count ("in_progress", "waiting", "pending", "requested"),
#           a time that cannot be read, the configured grace by the shared number rule
#           (with the stage's warning), several queued checks, one PR comment however often
#           a blocked head is polled, and the merge once the runner is back
#   review  the review stage's inline merge: 25, needs-human, the line on the ticket, the
#           approval kept; once the runner is back the next run merges without a model call
#   queue   queue-loop.sh's real run_script around the real merge stage: the blocked gate
#           alerts once per throttle key (the real alert_telegram and throttle), not once
#           per poll
#
# Runs the REAL merge-pipeline.sh, code-review-pipeline.sh, bureau-supervision.py and the
# gate helpers from bureau-config.sh in the harness sandbox; GitHub is the pr2 gh double
# (its check_runs.json carries the started_at times), Linear the stub config, the models
# fake_claude.sh, Telegram a curl double. TEST_SECTIONS="merge review queue" (any subset)
# runs only those sections, so each can be shown to fail against v3.1.0 on its own.
set -euo pipefail
source "$(dirname "$0")/lib/harness.sh"
source "$(dirname "$0")/lib/pr2-gate.sh"
unset BUREAU_CALLER_STOP
ISSUE=EXP-803
trap 'teardown || true' EXIT
fail() { echo "FAIL: $1" >&2; printf '%s\n--- stderr ---\n%s\n' "${LAST_STDOUT:-}" "${LAST_STDERR:-}" | tail -40 >&2; exit 1; }
want() { case " ${TEST_SECTIONS:-merge review queue} " in *" $1 "*) return 0 ;; *) return 1 ;; esac; }

# checks <status>:<age>[:<name>] … — the check runs on the PR head; <age> is how long
# ago started_at lies (null: none, bad: a time GitHub never sends, frac: with fractions).
checks() {
  local spec status age name rest out='[]' at
  for spec in "$@"; do
    status=${spec%%:*}; rest=${spec#*:}; age=${rest%%:*}; name=ci
    [ "$rest" = "$age" ] || name=${rest#*:}
    case "$age" in
      null) at=null ;;
      bad)  at='"yesterday"' ;;
      frac) at="\"$(jq -rn --argjson t "$(( PR2_NOW - 7200 ))" '$t | todate | sub("Z$"; ".250Z")')\"" ;;
      *)    at="\"$(jq -rn --argjson t "$(( PR2_NOW - age ))" '$t | todate')\"" ;;
    esac
    case "$status" in
      success) out=$(jq --arg n "$name" --argjson at "$at" '. + [{name: $n, status: "completed", conclusion: "success", started_at: $at}]' <<< "$out") ;;
      *)       out=$(jq --arg n "$name" --arg s "$status" --argjson at "$at" '. + [{name: $n, status: $s, conclusion: null, started_at: $at}]' <<< "$out") ;;
    esac
  done
  jq -n --argjson runs "$out" '{check_runs: $runs}' > "$PR2_GH/check_runs.json"
}

new_sandbox() {
  teardown || true
  sandbox_init "$ISSUE" test-branch
  printf 'change\n' > "$SANDBOX/change.txt"
  git -C "$SANDBOX" add change.txt
  git -C "$SANDBOX" commit -q -m 'fixture change'
  git -C "$SANDBOX" push -q origin test-branch
  export BUREAU_NO_MERGE=0 BUREAU_STOP_REQUESTED=0
  pr2_gate_setup
  HEAD_SHA=$(jq -r .headRefOid "$PR2_GH/pr.json")
}
merge_sandbox() {
  new_sandbox
  export BUREAU_STUB_STATE_MERGE=state-merge BUREAU_STUB_AGENT_ENABLED=merge
  # The review's APPROVE the gate reads.
  jq -n '[{createdAt: "2026-09-29T09:00:00Z", body: "## Code Review v2 — EXP-803\n\n**Verdict**: APPROVE"}]' > "$PR2_GH/comments.json"
}
gate() {  # runs the merge stage; sets LAST_RC, OUTCOME and LINES
  export BUREAU_MERGE_GATE_REPORT="$SANDBOX/gate.report"
  rm -f "$BUREAU_MERGE_GATE_REPORT" "$PR2_GH/merge_calls.log"
  run_pipeline merge-pipeline.sh "$ISSUE"
  unset BUREAU_MERGE_GATE_REPORT
  OUTCOME=$(head -n 1 "$SANDBOX/gate.report" 2>/dev/null || true)
  LINES=$(sed -n '2,$p' "$SANDBOX/gate.report" 2>/dev/null || true)
}
case_is() {  # <label> <want rc> <want outcome> <pattern> — after gate
  [ "$LAST_RC" = "$2" ] && [ "$OUTCOME" = "$3" ] && grep -qE -- "$4" <<< "$LINES" \
    || fail "$1: rc $LAST_RC outcome '$OUTCOME', wanted $2 $3 /$4/ in: $LINES"
  pr2_merged && fail "$1: merged"
  return 0
}
queued_line() {  # <name> <age> <grace> [<more>] — the exact gate line for HEAD_SHA, as an ERE
  local more=""
  [ -z "${4:-}" ] || more=", $4 more check\\(s\\) as well"
  printf '^ci_green: ci: check %s queued for %s s on %s, past the CI queue grace \\(agents\\.merge_ci_queued_grace_seconds: %s\\)%s — runner offline\\?$' "$1" "$2" "$HEAD_SHA" "$3" "$more"
}
gate_comments() { jq '[.[] | select(.body | test("Bureau merge gate"))] | length' "$PR2_GH/comments.json"; }

# ── merge: the merge stage on its own ─────────────────────────────────────
if want merge; then
merge_sandbox
checks queued:3700
gate; case_is 'queued 3700 s' 25 blocked "$(queued_line ci 3700 3600)"
[ "$(printf '%s\n' "$LINES" | grep -c .)" = 1 ] || fail "merge: the queued check should be the only gate line: $LINES"
[ "$(jq -r '[.[] | select(.body | test("Bureau merge gate"))] | last | .body' "$PR2_GH/comments.json" | sed -n 's/^Outcome: //p')" = 'blocked — needs someone to act' ] \
  || fail 'merge: the PR gate comment does not say blocked'
grep -q 'should be a whole number' <<< "$LAST_STDERR" && fail 'merge: warned about the default grace'
checks queued:3599;  gate; case_is 'one second inside the grace' 2 not-yet '^ci_green: ci: 1 check\(s\) still pending on '
checks queued:3600;  gate; case_is 'at the grace'                25 blocked "$(queued_line ci 3600 3600)"
checks queued:-600;  gate; case_is 'queued in the future'         2 not-yet 'still pending'
# Only "queued" counts: a running check, a deployment approval, a concurrency wait and a
# requested run stay pending however old.
for status in in_progress waiting pending requested; do
  checks "$status:99999"; gate; case_is "status $status" 2 not-yet 'still pending'
done
# A time that cannot be read leaves the run pending, and does not hide another one.
for age in null bad frac; do
  checks "queued:$age"; gate; case_is "started_at $age" 2 not-yet 'still pending'
  checks "queued:$age:odd" queued:3700:build; gate; case_is "started_at $age next to one past the grace" 25 blocked "$(queued_line build 3700 3600)"
done
# A name with a tab or a line break stays one line.
checks "queued:3700:lint"$'\n'"fast"$'\t'"x"; gate; case_is 'a name with a line break' 25 blocked "$(queued_line 'lint fast x' 3700 3600)"
# A pending commit status (the legacy API) stays pending: it is not a check run.
checks; echo '{"statuses":[{"context":"ext","state":"pending","created_at":"2020-01-01T00:00:00Z"}]}' > "$PR2_GH/status.json"
gate; case_is 'a pending status' 2 not-yet 'still pending'
# … and does not hide a check run queued past the grace next to it.
checks queued:3700
gate; case_is 'a pending status next to a check queued past the grace' 25 blocked "$(queued_line ci 3700 3600)"
echo '{"statuses":[]}' > "$PR2_GH/status.json"
# Several: the longest-queued one is named, the others counted; a running or a green
# check beside them changes nothing.
checks queued:4000:lint queued:5000:build 'queued:100:docs' in_progress:60:test success:60:fmt
gate; case_is 'two past the grace' 25 blocked "$(queued_line build 5000 3600 1)"
echo 'PASS merge: a check queued for the grace or longer is blocked with its own line; within it, any other status and an unreadable time stay not yet'

# The grace is read by the gate's number rule; the merge stage warns about a value that is
# not a plain whole number and names the number it uses.
checks queued:3700
pr2_config '.agents.merge_ci_queued_grace_seconds = 7200'; gate; case_is 'grace 7200' 2 not-yet 'still pending'
pr2_config '.agents.merge_ci_queued_grace_seconds = 0'; checks queued:1; gate; case_is 'grace 0' 25 blocked "$(queued_line ci 1 0)"
grep -q 'should be a whole number' <<< "$LAST_STDERR" && fail 'merge: warned about a valid grace'
checks queued:3700
# value : grace used : outcome for a check queued 3700 s ago
for row in '"600":600:blocked' '1.5:2:blocked' '-5:3600:blocked' '"abc":3600:blocked' 'true:3600:blocked' '"7200":7200:not-yet'; do
  value=${row%%:*}; rest=${row#*:}; used=${rest%%:*}; outcome=${rest#*:}
  pr2_config ".agents.merge_ci_queued_grace_seconds = $value"
  gate
  if [ "$outcome" = blocked ]; then case_is "grace $value" 25 blocked "$(queued_line ci 3700 "$used")"
  else case_is "grace $value" 2 not-yet 'still pending'; fi
  grep -q "agents.merge_ci_queued_grace_seconds should be a whole number of at least 0; using $used\$" <<< "$LAST_STDERR" \
    || fail "merge: no warning naming $used for the grace $value: $LAST_STDERR"
done
pr2_config 'del(.agents.merge_ci_queued_grace_seconds)'
echo 'PASS merge: the queue grace follows the shared number rule, with a warning for a value that is not a plain whole number'

# Polled while it stays blocked (the merge stage sets no needs-human, so the queue runs it
# again on every poll) and the age grows: one gate comment on the PR, not one per poll.
merge_sandbox
for age in 3700 3760 3820 3880; do checks "queued:$age"; gate; done
[ "$LAST_RC" = 25 ] || fail "merge: the polled head ended $LAST_RC, wanted 25"
[ "$(gate_comments)" = 1 ] || fail "merge: four polls of the same queued check posted $(gate_comments) gate comments, wanted 1"
for age in 3940 4000; do checks queued:5000:build "queued:$age"; gate; done
[ "$(gate_comments)" = 2 ] || fail "merge: a second check past the grace should post one more comment, posted $(( $(gate_comments) - 1 ))"
# The oldest one ages and a third joins: the same blocker, no new comment.
checks queued:5100:build queued:4100 queued:3700:lint; gate
case_is 'three past the grace' 25 blocked "$(queued_line build 5100 3600 2)"
[ "$(gate_comments)" = 2 ] || fail "merge: an older age and one more queued check posted another comment ($(gate_comments) in all)"
# The runner is back: the check runs (not yet), then passes (merged).
checks in_progress:10; gate; case_is 'the check started' 2 not-yet 'still pending'
checks success:10; gate
[ "$LAST_RC" = 0 ] && pr2_merged || fail "merge: the green head did not merge (rc $LAST_RC: $LINES)"
echo 'PASS merge: polling a blocked head posts one comment however the age grows; once the runner is back the PR merges'
fi

# ── review: the inline merge after an APPROVE ─────────────────────────────
if want review; then
new_sandbox
cat > "$SANDBOX/approve.txt" <<'EOF'
Review checked.
```json
{"verdict":"APPROVE","bugs":0,"security_issues":0,"findings":[],"summary":"Checks passed"}
```
EOF
export FAKE_CLAUDE_FIXTURES="$SANDBOX/approve.txt"
export BUREAU_STUB_ISSUE_STATE='Build Review' BUREAU_STUB_STATE_MERGE='' BUREAU_STUB_AGENT_ENABLED=''
STOPS="$(git -C "$SANDBOX" rev-parse --path-format=absolute --git-common-dir)/bureau/review-stops.json"
checks queued:3700
rm -f "$SANDBOX/fake_claude_counter"; run_pipeline code-review-pipeline.sh "$ISSUE"
[ "$LAST_RC" = 25 ] || fail "review: a check queued past the grace ended the review $LAST_RC, wanted 25"
grep -q $'add_issue_label\t'"$ISSUE"$'\tneeds-human' "$SANDBOX/calls.log" || fail 'review: no needs-human'
grep -q 'merge gate is blocked' "$SANDBOX/calls.log" && grep -q "check ci queued for 3700 s on $HEAD_SHA" "$SANDBOX/calls.log" \
  || fail 'review: the ticket comment does not carry the queued-check line'
pr2_merged && fail 'review: merged with a check that never ran'
[ "$(jq -r --arg i "$ISSUE" '.[$i] | .verdict + " " + ((.merge_gate_wait // false) | tostring)' "$STOPS")" = 'APPROVE false' ] \
  || fail "review: the approval was not kept for after the fix: $(cat "$STOPS")"
# A human brings the runner back; the check passes; needs-human is removed: the next run
# reuses the approval (no model call) and merges.
checks success:10
: > "$SANDBOX/calls.log"; rm -f "$SANDBOX/fake_claude_counter"
run_pipeline code-review-pipeline.sh "$ISSUE"
[ "$LAST_RC" = 0 ] && pr2_merged || fail "review: the run after the fix did not merge (rc $LAST_RC)"
[ "$(pr2_model_calls)" = 0 ] || fail "review: the run after the fix paid $(pr2_model_calls) model call(s)"
echo 'PASS review: a check queued past the grace blocks the inline merge (25, needs-human, the line on the ticket); after the fix the kept approval merges'
fi

# ── queue: one alert per throttle key ─────────────────────────────────────
if want queue; then
merge_sandbox
checks queued:3700
Q="$SANDBOX/.queue"; mkdir -p "$Q/scripts" "$Q/bin"
# The worker runs the real merge stage in the harness sandbox.
cat > "$Q/scripts/bureau-worker.sh" <<EOF
#!/bin/bash
cd "$SANDBOX" && exec bash "$SCRIPTS_DIR/merge-pipeline.sh" "\$1"
EOF
cat > "$Q/bin/curl" <<EOF
#!/bin/bash
cat > /dev/null
echo post >> "$Q/telegram.log"
EOF
chmod +x "$Q/bin/curl"
{
  echo "source '$REPO_ROOT/templates/scripts/bureau-env.sh'"
  sed -n -e '/^_bureau_curl_config() {/,/^}/p' -e '/^bureau_common_dir() {/,/^}/p' \
    -e '/^_throttle_where() {/,/^}/p' -e '/^_throttle_should_suppress() {/,/^}/p' -e '/^_throttle_record() {/,/^}/p' \
    -e '/^_bureau_repo_name() {/,/^}/p' -e '/^alert_telegram() {/,/^}/p' -e '/^exit_class() {/,/^}/p' \
    "$REPO_ROOT/templates/scripts/bureau-config.sh"
  sed -n '/^run_script() {/,/^}/p' "$REPO_ROOT/templates/scripts/queue-loop.sh"
} > "$Q/queue.sh"
for fn in alert_telegram _throttle_should_suppress exit_class run_script; do
  grep -q "^$fn() {" "$Q/queue.sh" || fail "queue: $fn not found in the real scripts"
done
export Q ISSUE
poll() {
  ( cd "$SANDBOX" && PATH="$Q/bin:$PATH" TELEGRAM_BOT_TOKEN=123456:test TELEGRAM_ALERT_CHAT_ID=42 \
      BUREAU_ALERT_THROTTLE_FILE="$SANDBOX/.queue/throttle.log" /bin/bash -c '
    source "$Q/queue.sh"
    REPO_DIR="$Q"; LOG_FILE="$Q/queue.log"; MODE=merge
    preselect_issue() { echo "$ISSUE"; }
    get_issue_branch() { echo test-branch; }
    emit_event() { :; }
    bureau_merge_is_manual() { return 1; }
    stop_before_merge_was_asked() { return 1; }
    run_script merge-pipeline.sh "Merge" "$Q/wt"' ) > /dev/null 2>&1 || true
}
poll; poll; poll
grep -c 'Gate outcome: blocked — exit 25' "$Q/queue.log" | grep -qx 3 || fail "queue: the merge stage did not end blocked on all three polls: $(tail -5 "$Q/queue.log")"
grep -q 'error (exit 25 / needs-human-or-paused)' "$Q/queue.log" || fail 'queue: the queue log does not name the blocked gate'
posts=$(grep -c post "$Q/telegram.log" 2>/dev/null || true)
[ "$posts" = 1 ] || fail "queue: three polls of the blocked gate sent $posts alert(s), wanted 1"
echo 'PASS queue: a check queued past the grace makes the merge stage end 25, and the queue alerts once per throttle key'
fi

echo 'OK test_ci_queued_grace'
