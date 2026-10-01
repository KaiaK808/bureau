#!/bin/bash
# An approved review waiting on an undecided merge gate no longer holds the review queue
# (v3.2, O2).
#
# When the review stage approves and its inline merge finds the gate not yet decided, it
# records the APPROVE and ends with 2. The picker handed out that ticket again on every
# poll, so the other Build Review tickets waited behind it for as long as the gate stayed
# undecided, and every pick ran the review build check again on a head that had not
# changed.
#
#   build  the review stage: a gate-wait approval keeps the build check that passed, with
#          its command, and the count of "not yet" answers in a row; the next run on the
#          same head, base and command does not run the build check again (the gate runs
#          again, no model call); a changed command runs it; a push means a new review;
#          an approval kept after a blocked gate (not a gate wait) runs it as before
#   pick   the REAL pipeline_pick_next: a ticket with a fresh gate wait at an unchanged
#          head is skipped for the review stage (and only for it), so the next ticket is
#          reviewed; the backoff doubles with each "not yet" (300 s, 600 s, …) up to
#          agents.merge_gate_recheck_seconds (default 3600, 0 = off, read by the shared
#          number rule); a push, a record that is not a gate wait, an origin that cannot
#          be read end or skip the hold, and none of it fails a pick
#
# Runs the REAL code-review-pipeline.sh, merge-pipeline.sh, bureau-supervision.py and the
# gate helpers in the harness sandbox (GitHub: the pr2 gh double; Linear: the stub config;
# the models: fake_claude.sh), and the REAL bureau-config.sh for the picker against a curl
# double for Linear and the sandbox's own bare origin. TEST_SECTIONS="build pick" (any
# subset) runs only those sections, so each can be shown to fail against v3.1.0.
set -euo pipefail
source "$(dirname "$0")/lib/harness.sh"
source "$(dirname "$0")/lib/pr2-gate.sh"
unset BUREAU_CALLER_STOP
ISSUE=EXP-801
OTHER=EXP-802
trap 'teardown || true' EXIT
fail() { echo "FAIL: $1" >&2; printf '%s\n--- stderr ---\n%s\n' "${LAST_STDOUT:-}" "${LAST_STDERR:-}" | tail -40 >&2; exit 1; }
want() { case " ${TEST_SECTIONS:-build pick} " in *" $1 "*) return 0 ;; *) return 1 ;; esac; }

new_sandbox() {
  teardown || true
  sandbox_init "$ISSUE" test-branch
  printf 'change\n' > "$SANDBOX/change.txt"
  git -C "$SANDBOX" add change.txt
  git -C "$SANDBOX" commit -q -m 'fixture change'
  git -C "$SANDBOX" push -q origin test-branch
  cat > "$SANDBOX/approve.txt" <<'EOF'
Review checked.
```json
{"verdict":"APPROVE","bugs":0,"security_issues":0,"findings":[],"summary":"Checks passed"}
```
EOF
  export FAKE_CLAUDE_FIXTURES="$SANDBOX/approve.txt"
  export BUREAU_STUB_ISSUE_STATE='Build Review' BUREAU_STUB_STATE_MERGE='' BUREAU_STUB_AGENT_ENABLED=''
  export BUREAU_NO_MERGE=0 BUREAU_STOP_REQUESTED=0
  pr2_gate_setup
  COMMON="$(git -C "$SANDBOX" rev-parse --path-format=absolute --git-common-dir)"
  STOPS="$COMMON/bureau/review-stops.json"
  BUILDS="$COMMON/build-runs"
  # The build check counts its runs outside the worktree.
  build_command "echo run >> '$BUILDS'"
}
build_command() { jq --arg c "$1" '.repo.test_command = $c' "$SANDBOX/.bureau.json" > "$SANDBOX/.bureau.json.tmp" 2>/dev/null \
  || jq -n --arg c "$1" '{repo: {test_command: $c}}' > "$SANDBOX/.bureau.json.tmp"; mv "$SANDBOX/.bureau.json.tmp" "$SANDBOX/.bureau.json"; }
review() { rm -f "$SANDBOX/fake_claude_counter"; : > "$SANDBOX/gh_calls.log"; : > "$SANDBOX/calls.log"; run_pipeline code-review-pipeline.sh "$ISSUE"; }
builds() { grep -c run "$BUILDS" 2>/dev/null || true; }
record() { jq -c --arg i "$ISSUE" '.[$i] // empty' "$STOPS" 2>/dev/null || true; }
field() { record | jq -r ".$1 // \"none\""; }
push_head() {  # a new commit on the branch at origin, and the PR follows it
  local bump
  bump=$(git -C "$SANDBOX" commit-tree "origin/test-branch^{tree}" -p origin/test-branch -m "${1:-pushed}")
  git -C "$SANDBOX" push -q origin "$bump:refs/heads/test-branch"
  git -C "$SANDBOX" fetch -q origin
  jq --arg h "$bump" '.headRefOid = $h' "$PR2_GH/pr.json" > "$PR2_GH/pr.tmp" && mv "$PR2_GH/pr.tmp" "$PR2_GH/pr.json"
}

# ── build: the review stage keeps a passed build check with a gate wait ────
if want build; then
new_sandbox
pr2_checks pending
review
[ "$LAST_RC" = 2 ] || fail "build 1: not yet ended $LAST_RC, wanted 2"
[ "$(builds)" = 1 ] || fail "build 1: the first review ran the build check $(builds) time(s), wanted 1"
RECORD_1=$(record)

review
[ "$LAST_RC" = 2 ] || fail "build 2: the retry ended $LAST_RC, wanted 2"
[ "$(builds)" = 1 ] || fail "build 2: the retry on an unchanged head ran the build check again ($(builds) runs)"
[ "$(jq -r '"\(.merge_gate_wait) \(.gate_waits) \(.build_check)"' <<< "$RECORD_1")" = 'true 1 passed' ] \
  || fail "build 1: the gate wait did not record the passed build check and the count: $RECORD_1"
[ "$(jq -r .build_command <<< "$RECORD_1")" = "echo run >> '$BUILDS'" ] || fail "build 1: the record does not name the command: $RECORD_1"
grep -q "Build check not run again: \`echo run >> '$BUILDS'\` passed for head" <<< "$LAST_STDOUT" || fail 'build 2: the log does not say why the build check was skipped'
[ "$(pr2_model_calls)" = 0 ] || fail "build 2: the retry paid $(pr2_model_calls) model call(s)"
[ "$(pr2_gate_reads)" -ge 1 ] || fail 'build 2: the retry did not read the gate again'
[ "$(grep -c $'^post_comment\t' "$SANDBOX/calls.log" || true)" = 0 ] || fail 'build 2: the retry commented on the ticket'
[ "$(field gate_waits) $(field build_check)" = '2 passed' ] || fail "build 2: the retry did not count on or keep the pass: $(record)"

# Another command is another check: it runs, and its pass is what the record keeps.
build_command "echo run >> '$BUILDS'; true"
review
[ "$LAST_RC" = 2 ] && [ "$(builds)" = 2 ] || fail "build 3: a changed command did not run (rc $LAST_RC, $(builds) runs)"
[ "$(field build_command)" = "echo run >> '$BUILDS'; true" ] && [ "$(field gate_waits)" = 3 ] || fail "build 3: the record does not follow the new command: $(record)"

# Green: merged on the reused approval, still without the build check.
pr2_checks green
review
[ "$LAST_RC" = 0 ] && pr2_merged || fail "build 4: the green gate did not merge (rc $LAST_RC)"
[ "$(builds)" = 2 ] || fail "build 4: the merging run ran the build check again"
grep -q $'move_issue\t'"$ISSUE"$'\tstate-done' "$SANDBOX/calls.log" || fail 'build 4: the ticket did not move to Done'
[ -z "$(record)" ] || fail 'build 4: the record survived the merge'
echo 'PASS build: a gate wait keeps its passed build check; the next run on the same head, base and command skips it; another command runs; green merges'

# A push is a new head: a new review, and its build check runs.
new_sandbox
pr2_checks pending
review; [ "$LAST_RC" = 2 ] && [ "$(builds)" = 1 ] || fail "build 5: setup (rc $LAST_RC, $(builds) runs)"
push_head 'fix pushed'
review
[ "$(pr2_model_calls)" -gt 0 ] || fail 'build 5: a new head reused the old approval'
[ "$(builds)" = 2 ] || fail "build 5: the new head's review did not run the build check ($(builds) runs)"
[ "$(field gate_waits)" = 1 ] || fail "build 5: the count did not start again for the new head: $(record)"
# A red build check on a gate-wait retry (another command) is never skipped or kept: the
# reused APPROVE folds into REQUEST_CHANGES, back to Build, and no approval stays on record.
new_sandbox
pr2_checks pending
review; [ "$LAST_RC" = 2 ] && [ "$(builds)" = 1 ] || fail "build 6: setup (rc $LAST_RC, $(builds) runs)"
build_command "echo run >> '$BUILDS'; exit 1"
review
[ "$LAST_RC" = 0 ] && [ "$(builds)" = 2 ] || fail "build 6: a red build on a reused approval ended $LAST_RC after $(builds) runs, wanted 0 (REQUEST_CHANGES) after 2"
grep -q $'move_issue\t'"$ISSUE"$'\tstate-build' "$SANDBOX/calls.log" || fail 'build 6: the red build did not send the ticket back to Build'
[ -z "$(record)" ] || fail "build 6: a red build left an approval on record: $(record)"
# An approval kept after a blocked gate is not a gate wait: once unblocked it runs the build
# check again, as in v3.1.
new_sandbox
pr2_checks red
review; [ "$LAST_RC" = 25 ] && [ "$(builds)" = 1 ] || fail "build 7: setup (rc $LAST_RC, $(builds) runs)"
[ "$(field merge_gate_wait) $(field build_check)" = 'none none' ] || fail "build 7: the blocked gate recorded a gate wait: $(record)"
pr2_checks green
review
[ "$LAST_RC" = 0 ] && pr2_merged || fail "build 7: the run after the fix did not merge (rc $LAST_RC)"
[ "$(builds)" = 2 ] || fail "build 7: the run after a blocked gate skipped the build check"
echo 'PASS build: a push means a new review with its build check; a red build is never kept; an approval kept after a blocked gate runs the check again'
fi

# ── pick: the real picker leaves a fresh gate wait alone ──────────────────
if want pick; then
new_sandbox
# The real config: the Linear part for the picker, next to the stub's repo.test_command.
jq '. + {linear: {teams: [{id: "team-id", key: "EXP", name: "Test",
          states: {triage: "s1", spec: "s2", spec_review: "s3", design: "s4", build: "s5", build_review: "s6", done: "s7"}}],
        labels: {lane2: {id: "l1", name: "lane-2"}, needs_human: {id: "l2", name: "needs-human"},
                 needs_ux: {id: "l3", name: "needs-ux"}, ai_implementable: {id: "l4", name: "ai-implementable"}},
        projects: []},
      agents: {poll_interval_minutes: 30, max_review_cycles: 3}}' "$SANDBOX/.bureau.json" > "$SANDBOX/.bureau.json.tmp"
mv "$SANDBOX/.bureau.json.tmp" "$SANDBOX/.bureau.json"
PICKBIN="$SANDBOX/.pick-bin"; mkdir -p "$PICKBIN"
# Linear: EXP-801 (priority 1, older) and EXP-802 in the queried state; no blockers.
cat > "$PICKBIN/curl" <<'EOF'
#!/bin/bash
node() { printf '{"identifier":"%s","priority":%s,"createdAt":"%s","labels":{"nodes":[{"name":"lane-2"},{"name":"ai-implementable"}]},"inverseRelations":{"nodes":[]}}' "$@"; }
printf '{"data":{"issues":{"nodes":[%s,%s]}}}' "$(node EXP-801 1 2026-01-01)" "$(node EXP-802 2 2026-01-02)"
EOF
chmod +x "$PICKBIN/curl"
pick() {  # <stage script> [skip csv] — the REAL pipeline_pick_next; sets PICKED, PICK_RC, PICK_ERR
  set +e
  PICKED=$(cd "$SANDBOX" && PATH="$PICKBIN:$PATH" LINEAR_API_KEY=k BUREAU_LINEAR_RETRIES=0 BUREAU_CONFIG="$SANDBOX/.bureau.json" \
    /bin/bash -c 'set -euo pipefail; source "$1/templates/scripts/bureau-config.sh"; pipeline_pick_next "$2" "${3:-}"' \
    _ "$REPO_ROOT" "$1" "${2:-}" 2>"$SANDBOX/pick.err")
  PICK_RC=$?
  set -e
  PICK_ERR=$(cat "$SANDBOX/pick.err")
}
picks() {  # <want> <label> [stage] — the review picker hands out <want>
  pick "${3:-code-review-pipeline.sh}"
  [ "$PICK_RC" = 0 ] && [ "$PICKED" = "$1" ] || fail "pick $2: picked '$PICKED' (rc $PICK_RC), wanted '$1'; stderr: $PICK_ERR"
}
age_record() {  # <seconds> — the recorded gate wait happened that long ago (real time: the picker's clock)
  python3 - "$STOPS" "$ISSUE" "$1" <<'PY'
import json, sys, time
path, issue, age = sys.argv[1], sys.argv[2], int(sys.argv[3])
stops = json.load(open(path)); stops[issue]['stopped_at'] = time.time() - age
json.dump(stops, open(path, 'w'))
PY
}
set_waits() { jq --arg i "$ISSUE" --argjson n "$1" '.[$i].gate_waits = $n' "$STOPS" > "$STOPS.tmp" && mv "$STOPS.tmp" "$STOPS"; }

picks "$ISSUE" 'without any record'
pr2_checks pending
review; [ "$LAST_RC" = 2 ] || fail "pick: setup review ended $LAST_RC, wanted 2"
HEAD_SHA=$(git -C "$SANDBOX" rev-parse origin/test-branch)
picks "$OTHER" 'a fresh gate wait'
grep -qE "^pick: skip $ISSUE — approved, waiting on its merge gate at the unchanged head ${HEAD_SHA:0:12}; checked again in ([1-9][0-9]?|[12][0-9][0-9]|300) s \(not yet 1 time\(s\) in a row\)$" <<< "$PICK_ERR" \
  || fail "pick: the skip is not logged as it should be: $PICK_ERR"
picks "$ISSUE" 'another stage' implement-pipeline.sh
grep -q 'waiting on its merge gate' <<< "$PICK_ERR" && fail 'pick: another stage consulted the gate waits'
# The caller's own skip list still applies on top.
pick code-review-pipeline.sh "$OTHER"
[ "$PICK_RC" = 0 ] && [ -z "$PICKED" ] || fail "pick: the gate wait plus the caller's skip should leave nothing, got '$PICKED'"
# The backoff: 300 s after the first "not yet".
age_record 290; picks "$OTHER" '290 s after the first not yet'
age_record 310; picks "$ISSUE" '310 s after the first not yet'
# The next run on the unchanged head counts on: 600 s.
review; [ "$LAST_RC" = 2 ] && [ "$(field gate_waits)" = 2 ] || fail "pick: the second not yet did not count on (rc $LAST_RC, $(record))"
age_record 310; picks "$OTHER" '310 s after the second not yet'
age_record 610; picks "$ISSUE" '610 s after the second not yet'
# Doubling stops at agents.merge_gate_recheck_seconds (default 3600).
set_waits 9
age_record 3590; picks "$OTHER" 'the ninth not yet, 3590 s'
age_record 3610; picks "$ISSUE" 'the ninth not yet, 3610 s'
pr2_config '.agents.merge_gate_recheck_seconds = 600'
age_record 590; picks "$OTHER" 'a 600 s cap, 590 s'
age_record 610; picks "$ISSUE" 'a 600 s cap, 610 s'
grep -q 'should be a whole number' <<< "$PICK_ERR" && fail 'pick: warned about a valid recheck value'
# 0 switches the backoff off: the ticket is picked on every poll, as in v3.1.
pr2_config '.agents.merge_gate_recheck_seconds = 0'
age_record 1; picks "$ISSUE" 'recheck 0'
# A value that is not a plain whole number: the shared rule, with a warning.
pr2_config '.agents.merge_gate_recheck_seconds = "abc"'
age_record 1; picks "$OTHER" 'recheck "abc" (default 3600)'
grep -q 'pick: WARN: agents.merge_gate_recheck_seconds should be a whole number of at least 0; using 3600' <<< "$PICK_ERR" \
  || fail "pick: no warning for \"abc\": $PICK_ERR"
pr2_config '.agents.merge_gate_recheck_seconds = "60"'
age_record 59; picks "$OTHER" 'recheck "60", 59 s'
age_record 61; picks "$ISSUE" 'recheck "60", 61 s'
pr2_config 'del(.agents.merge_gate_recheck_seconds)'
# A count that is not a whole number from 1 counts as the first "not yet".
for waits in 0 '"abc"' null; do
  set_waits "$waits"
  age_record 290; picks "$OTHER" "count $waits, 290 s"
  age_record 310; picks "$ISSUE" "count $waits, 310 s"
done
set_waits 1
# A record time in the future counts as now: the wait is never longer than its backoff.
age_record -100000; picks "$OTHER" 'a record time in the future'
grep -qE "checked again in ([1-9][0-9]?|[12][0-9][0-9]|300) s" <<< "$PICK_ERR" || fail "pick: a future record time held the ticket longer than its backoff: $PICK_ERR"
echo 'PASS pick: a fresh gate wait at an unchanged head is skipped by the review picker only; the backoff doubles from 300 s up to agents.merge_gate_recheck_seconds; 0 switches it off'

# A push ends the wait at once.
age_record 1; picks "$OTHER" 'before the push'
push_head 'fix pushed'
picks "$ISSUE" 'a new head on origin'
# A record of the head that is on origin now holds the ticket again.
NEW_HEAD=$(git -C "$SANDBOX" rev-parse origin/test-branch)
jq --arg i "$ISSUE" --arg h "$NEW_HEAD" '.[$i].head = $h' "$STOPS" > "$STOPS.tmp" && mv "$STOPS.tmp" "$STOPS"
age_record 1; picks "$OTHER" 'a gate wait recorded for the new head'
# An origin that cannot be read holds nothing back, and says so.
git -C "$SANDBOX" remote set-url origin "$SANDBOX/.no-such-origin.git"
picks "$ISSUE" 'origin unreadable'
grep -q 'pick: WARN: the review gate waits could not be read; no ticket is held back' <<< "$PICK_ERR" || fail "pick: no warning for an unreadable origin: $PICK_ERR"
git -C "$SANDBOX" remote set-url origin "$SANDBOX/.fake-origin.git"
# A record file that is not JSON: the same.
cp "$STOPS" "$STOPS.bak"; echo 'not json' > "$STOPS"
picks "$ISSUE" 'an unreadable record file'
grep -q 'could not be read; no ticket is held back' <<< "$PICK_ERR" || fail "pick: no warning for an unreadable record file: $PICK_ERR"
mv "$STOPS.bak" "$STOPS"
age_record 1; picks "$OTHER" 'the record file is back'
# A record that is not a gate wait (a --no-merge stop, an approval kept after a blocked
# gate) is not this rule's business.
jq --arg i "$ISSUE" '.[$i] |= del(.merge_gate_wait)' "$STOPS" > "$STOPS.tmp" && mv "$STOPS.tmp" "$STOPS"
picks "$ISSUE" 'a record that is not a gate wait'
echo 'PASS pick: a push, an unreadable origin or record file, and a record that is not a gate wait hold nothing back'
fi

echo 'OK test_review_gate_waits'
