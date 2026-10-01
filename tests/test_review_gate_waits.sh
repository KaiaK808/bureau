#!/bin/bash
# A ticket waiting on an undecided merge gate no longer holds its queue (v3.2, O2).
#
# When the review stage approves and its inline merge finds the gate not yet decided, it
# records the APPROVE and ends with 2; the merge stage ends with 2 or 25 and sets no
# needs-human. The pickers handed out that ticket again on every poll, so the other
# tickets of the state waited behind it for as long as the gate stayed so, and every
# review pick ran the build check again on a head that had not changed.
#
#   build  the review stage: a gate-wait approval keeps the build check that passed, with
#          its command and a key over the command, repo.untrusted_env and
#          repo.worktree_links, and the count of "not yet" answers in a row; the next run
#          on the same head, base and key does not run the build check again (the gate
#          runs again, no model call); a changed command or setting runs it; a push means
#          a new review; an approval kept after a blocked gate (not a gate wait) runs it
#   pick   the REAL pipeline_pick_next: a ticket with a fresh gate wait at an unchanged
#          head goes after every other candidate of the review stage (and only of it),
#          and is taken when nothing else can be; the hold starts at two poll intervals
#          (at least 300 s; the queue loop's own interval wins over the configured one),
#          doubles with each "not yet" up to agents.merge_gate_recheck_seconds (default
#          3600, 0 = off, read by the shared number rule); a push, a deleted branch, a
#          record from the future, a record that is not a gate wait, an origin or record
#          file that cannot be read (one warning line) end or skip the hold, and none of
#          it fails a pick
#   merge  the REAL merge stage marks a not-yet or blocked gate (count per head, cleared
#          by a merge; nothing in a dry run or the review's inline merge); the merge
#          picker, and neither the rebase nor the review picker, holds it the same way
#
# Runs the REAL code-review-pipeline.sh, merge-pipeline.sh, bureau-supervision.py and the
# gate helpers in the harness sandbox (GitHub: the pr2 gh double; Linear: the stub config;
# the models: fake_claude.sh), and the REAL bureau-config.sh for the picker against a curl
# double for Linear and the sandbox's own bare origin. TEST_SECTIONS="build pick merge"
# (any subset) runs only those sections, so each can be shown to fail against v3.1.0.
set -euo pipefail
source "$(dirname "$0")/lib/harness.sh"
source "$(dirname "$0")/lib/pr2-gate.sh"
unset BUREAU_CALLER_STOP
ISSUE=EXP-801
OTHER=EXP-802
trap 'teardown || true' EXIT
fail() { echo "FAIL: $1" >&2; printf '%s\n--- stderr ---\n%s\n' "${LAST_STDOUT:-}" "${LAST_STDERR:-}" | tail -40 >&2; exit 1; }
want() { case " ${TEST_SECTIONS:-build pick merge} " in *" $1 "*) return 0 ;; *) return 1 ;; esac; }

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

# The environment settings are part of the key: another repo.untrusted_env or another
# repo.worktree_links runs the check again, and a run on the same key skips it again.
jq '.repo.untrusted_env = "clean"' "$SANDBOX/.bureau.json" > "$SANDBOX/.bureau.json.tmp" && mv "$SANDBOX/.bureau.json.tmp" "$SANDBOX/.bureau.json"
review
[ "$LAST_RC" = 2 ] && [ "$(builds)" = 3 ] || fail "build 3: another repo.untrusted_env did not run the check (rc $LAST_RC, $(builds) runs)"
review
[ "$LAST_RC" = 2 ] && [ "$(builds)" = 3 ] || fail "build 3: the same key ran the check again (rc $LAST_RC, $(builds) runs)"
jq '.repo.worktree_links = [".venv"]' "$SANDBOX/.bureau.json" > "$SANDBOX/.bureau.json.tmp" && mv "$SANDBOX/.bureau.json.tmp" "$SANDBOX/.bureau.json"
review
[ "$LAST_RC" = 2 ] && [ "$(builds)" = 4 ] || fail "build 3: another repo.worktree_links did not run the check (rc $LAST_RC, $(builds) runs)"
[ "$(field build_key)" != none ] && [ "$(field gate_waits)" = 6 ] || fail "build 3: the record lost its key or its count: $(record)"
BUILDS_BEFORE_GREEN=$(builds)

# Green: merged on the reused approval, still without the build check.
pr2_checks green
review
[ "$LAST_RC" = 0 ] && pr2_merged || fail "build 4: the green gate did not merge (rc $LAST_RC)"
[ "$(builds)" = "$BUILDS_BEFORE_GREEN" ] || fail "build 4: the merging run ran the build check again"
grep -q $'move_issue\t'"$ISSUE"$'\tstate-done' "$SANDBOX/calls.log" || fail 'build 4: the ticket did not move to Done'
[ -z "$(record)" ] || fail 'build 4: the record survived the merge'
echo 'PASS build: a gate wait keeps its passed build check; the next run on the same head, base and key skips it; another command or environment setting runs it; green merges'

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

# ── the real picker, shared by the pick and merge sections ────────────────
real_picker() {  # the Linear part of the real config next to the stub's settings, a curl and a python3 double
  jq '. + {linear: {teams: [{id: "team-id", key: "EXP", name: "Test",
            states: {triage: "s1", spec: "s2", spec_review: "s3", design: "s4", build: "s5", build_review: "s6", merge: "s8", done: "s7"}}],
          labels: {lane2: {id: "l1", name: "lane-2"}, needs_human: {id: "l2", name: "needs-human"},
                   needs_ux: {id: "l3", name: "needs-ux"}, ai_implementable: {id: "l4", name: "ai-implementable"}},
          projects: []}}
      | .agents = ((.agents // {}) + {poll_interval_minutes: 1, max_review_cycles: 3})' "$SANDBOX/.bureau.json" > "$SANDBOX/.bureau.json.tmp"
  mv "$SANDBOX/.bureau.json.tmp" "$SANDBOX/.bureau.json"
  PICKBIN="$SANDBOX/.pick-bin"; mkdir -p "$PICKBIN"
  # Linear: the tickets in $PICKBIN/issues (default EXP-801, priority 1 and older, then
  # EXP-802) in whatever state is asked for; no blockers.
  echo "$ISSUE $OTHER" > "$PICKBIN/issues"
  cat > "$PICKBIN/curl" <<EOF
#!/bin/bash
node() { printf '{"identifier":"%s","priority":%s,"createdAt":"2026-01-0%s","labels":{"nodes":[{"name":"lane-2"},{"name":"ai-implementable"}]},"inverseRelations":{"nodes":[]}}' "\$1" "\$2" "\$2"; }
n=0; nodes=""
for id in \$(cat "$PICKBIN/issues"); do n=\$((n + 1)); nodes="\${nodes:+\$nodes,}\$(node "\$id" "\$n")"; done
printf '{"data":{"issues":{"nodes":[%s]}}}' "\$nodes"
EOF
  # python3 passes through and counts the picker's gate-waits calls.
  cat > "$PICKBIN/python3" <<EOF
#!/bin/bash
case " \$* " in *" gate-waits "*) echo call >> "$PICKBIN/gate-waits.log" ;; esac
exec "$(command -v python3)" "\$@"
EOF
  chmod +x "$PICKBIN/curl" "$PICKBIN/python3"
}
gate_wait_calls() { if [ -f "$PICKBIN/gate-waits.log" ]; then grep -c call "$PICKBIN/gate-waits.log"; else echo 0; fi; }
pick() {  # <stage script> [skip csv] — the REAL pipeline_pick_next; sets PICKED, PICK_RC, PICK_ERR
  set +e
  PICKED=$(cd "$SANDBOX" && PATH="$PICKBIN:$PATH" LINEAR_API_KEY=k BUREAU_LINEAR_RETRIES=0 BUREAU_CONFIG="$SANDBOX/.bureau.json" \
    /bin/bash -c 'set -euo pipefail; source "$1/templates/scripts/bureau-config.sh"; pipeline_pick_next "$2" "${3:-}"' \
    _ "$REPO_ROOT" "$1" "${2:-}" 2>"$SANDBOX/pick.err")
  PICK_RC=$?
  set -e
  PICK_ERR=$(cat "$SANDBOX/pick.err")
}
picks() {  # <want> <label> [stage] — the picker of <stage> (default: review) hands out <want>
  pick "${3:-code-review-pipeline.sh}"
  [ "$PICK_RC" = 0 ] && [ "$PICKED" = "$1" ] || fail "pick $2: picked '$PICKED' (rc $PICK_RC), wanted '$1'; stderr: $PICK_ERR"
}
age_record() {  # <seconds> [file] — the record of $ISSUE was written that long ago (real time: the picker's clock)
  python3 - "${2:-$STOPS}" "$ISSUE" "$1" <<'PYAGE'
import json, sys, time
path, issue, age = sys.argv[1], sys.argv[2], int(sys.argv[3])
stops = json.load(open(path)); stops[issue]['stopped_at'] = time.time() - age
json.dump(stops, open(path, 'w'))
PYAGE
}
set_waits() { local f="${2:-$STOPS}"; jq --arg i "$ISSUE" --argjson n "$1" '.[$i].gate_waits = $n' "$f" > "$f.tmp" && mv "$f.tmp" "$f"; }
held_line() {  # <outcome> <count> — the picker's line for a held $ISSUE at HEAD_SHA, as an ERE
  printf '^pick: %s waits on its merge gate \\(%s\\) at the unchanged head %s — after every other ticket for [0-9]+ s more \\(%s time\\(s\\) in a row\\)$' "$ISSUE" "$1" "${HEAD_SHA:0:12}" "$2"
}

# ── pick: the review picker puts a fresh gate wait after the other tickets ─
if want pick; then
new_sandbox
real_picker
picks "$ISSUE" 'without any record'
[ "$(gate_wait_calls)" = 0 ] || fail 'pick: without a record file the picker still started the gate-waits read'
pr2_checks pending
review; [ "$LAST_RC" = 2 ] || fail "pick: setup review ended $LAST_RC, wanted 2"
HEAD_SHA=$(git -C "$SANDBOX" rev-parse origin/test-branch)
picks "$OTHER" 'a fresh gate wait'
[ "$(gate_wait_calls)" -ge 1 ] || fail 'pick: the python3 counter saw no gate-waits call, so it proves nothing'
grep -qE "$(held_line 'not yet' 1)" <<< "$PICK_ERR" || fail "pick: the hold is not logged as it should be: $PICK_ERR"
# Only the review picker: the merge, rebase and implement pickers take EXP-801 first.
for stage in merge-pipeline.sh rebase-pipeline.sh implement-pipeline.sh; do
  picks "$ISSUE" "the review record seen by $stage" "$stage"
  grep -q 'waits on its merge gate' <<< "$PICK_ERR" && fail "pick: $stage consulted the review gate waits"
done
# No other ticket can be picked (the caller skips it, or it is the only one): the waiting
# ticket is taken anyway, so a hold never delays a merge while nothing else waits.
pick code-review-pipeline.sh "$OTHER"
[ "$PICK_RC" = 0 ] && [ "$PICKED" = "$ISSUE" ] || fail "pick: alone with its hold, the waiting ticket must be taken, got '$PICKED'"
grep -q "^pick: $ISSUE taken although it waits on its merge gate — no other ticket of the stage can be picked$" <<< "$PICK_ERR" || fail "pick: taking it anyway is not logged: $PICK_ERR"
echo "$ISSUE" > "$PICKBIN/issues"; picks "$ISSUE" 'the only ticket in Build Review'; echo "$ISSUE $OTHER" > "$PICKBIN/issues"
# The hold: two poll intervals, at least 300 s (agents.poll_interval_minutes is 1 here),
# after the first "not yet".
age_record 270; picks "$OTHER" '270 s after the first not yet'
age_record 330; picks "$ISSUE" '330 s after the first not yet'
# The next run on the unchanged head counts on: 600 s.
review; [ "$LAST_RC" = 2 ] && [ "$(field gate_waits)" = 2 ] || fail "pick: the second not yet did not count on (rc $LAST_RC, $(record))"
age_record 330; picks "$OTHER" '330 s after the second not yet'
age_record 630; picks "$ISSUE" '630 s after the second not yet'
set_waits 1
# Two poll intervals: from the configuration (10 min: 1200 s) …
pr2_config '.agents.poll_interval_minutes = 10'
age_record 1140; picks "$OTHER" 'poll interval 10 min, 1140 s'
age_record 1260; picks "$ISSUE" 'poll interval 10 min, 1260 s'
pr2_config '.agents.poll_interval_minutes = 1'
# … or the queue loop's own interval, which wins (900 s: 1800 s). queue-loop.sh hands
# its interval on (its real lines, cut up to LOG_DIR, run with `queue-loop.sh all 15` and
# with no interval argument).
for row in '15:900' ':1800'; do
  qenv=$(QL="$REPO_ROOT/templates/scripts/queue-loop.sh" ARG="${row%%:*}" BUREAU_POLL_INTERVAL=30 /bin/bash -c '
    code=$(sed -n "/^INTERVAL_MINUTES=/,/^LOG_DIR=/p" "$QL"); set -- all $ARG; eval "$code"; env' | grep '^BUREAU_QUEUE_POLL_SECONDS=' || true)
  [ "$qenv" = "BUREAU_QUEUE_POLL_SECONDS=${row#*:}" ] || fail "pick: queue-loop.sh ${row%%:*} did not hand on its interval (${row#*:} s): '$qenv'"
done
export BUREAU_QUEUE_POLL_SECONDS=900
age_record 1740; picks "$OTHER" 'loop interval 900 s, 1740 s'
age_record 1860; picks "$ISSUE" 'loop interval 900 s, 1860 s'
unset BUREAU_QUEUE_POLL_SECONDS
# Doubling stops at agents.merge_gate_recheck_seconds (default 3600).
set_waits 9
age_record 3540; picks "$OTHER" 'the ninth not yet, 3540 s'
age_record 3660; picks "$ISSUE" 'the ninth not yet, 3660 s'
pr2_config '.agents.merge_gate_recheck_seconds = 600'
age_record 540; picks "$OTHER" 'a 600 s cap, 540 s'
age_record 660; picks "$ISSUE" 'a 600 s cap, 660 s'
grep -q 'should be a whole number' <<< "$PICK_ERR" && fail 'pick: warned about a valid recheck value'
# 0 switches the hold off: the ticket is picked on every poll, as in v3.1.
pr2_config '.agents.merge_gate_recheck_seconds = 0'
calls=$(gate_wait_calls)
age_record 1; picks "$ISSUE" 'recheck 0'
[ "$(gate_wait_calls)" = "$calls" ] || fail 'pick: recheck 0 still started the gate-waits read'
# A value that is not a plain whole number: the shared rule, with a warning.
pr2_config '.agents.merge_gate_recheck_seconds = "abc"'
age_record 1; picks "$OTHER" 'recheck "abc" (default 3600)'
grep -q 'pick: WARN: agents.merge_gate_recheck_seconds should be a whole number of at least 0; using 3600' <<< "$PICK_ERR" \
  || fail "pick: no warning for \"abc\": $PICK_ERR"
pr2_config '.agents.merge_gate_recheck_seconds = "60"'
age_record 45; picks "$OTHER" 'recheck "60", 45 s'
age_record 75; picks "$ISSUE" 'recheck "60", 75 s'
pr2_config 'del(.agents.merge_gate_recheck_seconds)'
# A count that is not a whole number from 1 counts as the first "not yet".
for waits in 0 '"abc"' null; do
  set_waits "$waits"
  age_record 270; picks "$OTHER" "count $waits, 270 s"
  age_record 330; picks "$ISSUE" "count $waits, 330 s"
done
set_waits 1
# A record time in the future (a clock that stepped back) holds nothing; the same record
# written in the present holds until its time is up.
age_record -100000; picks "$ISSUE" 'a record time a day in the future'
age_record -30;     picks "$ISSUE" 'a record time 30 s in the future'
grep -q 'waits on its merge gate' <<< "$PICK_ERR" && fail "pick: a record from the future still printed a hold: $PICK_ERR"
age_record 0;   picks "$OTHER" 'the same record, written now'
age_record 330; picks "$ISSUE" 'the same record, 330 s later'
echo 'PASS pick: a fresh gate wait at an unchanged head goes after the other tickets of the review picker only, and is taken when it is alone; the hold starts at two poll intervals (at least 300 s) and doubles up to agents.merge_gate_recheck_seconds; 0 switches it off; a record from the future holds nothing'

# A push ends the hold at once.
age_record 1; picks "$OTHER" 'before the push'
push_head 'fix pushed'
picks "$ISSUE" 'a new head on origin'
# A record of the head that is on origin now holds the ticket again.
NEW_HEAD=$(git -C "$SANDBOX" rev-parse origin/test-branch)
jq --arg i "$ISSUE" --arg h "$NEW_HEAD" '.[$i].head = $h' "$STOPS" > "$STOPS.tmp" && mv "$STOPS.tmp" "$STOPS"
age_record 1; picks "$OTHER" 'a gate wait recorded for the new head'
# A branch gone from origin ends it too.
git -C "$SANDBOX" push -q origin --delete test-branch
picks "$ISSUE" 'the branch deleted on origin'
git -C "$SANDBOX" push -q origin "$NEW_HEAD:refs/heads/test-branch"
picks "$OTHER" 'the branch back on origin'
# An origin that cannot be read holds nothing back, and says so in one line.
git -C "$SANDBOX" remote set-url origin "$SANDBOX/.no-such-origin.git"
picks "$ISSUE" 'origin unreadable'
[ "$(printf '%s\n' "$PICK_ERR" | grep -c .)" = 1 ] \
  && grep -qE '^pick: WARN: the merge gate waits could not be read \(bureau supervision: git ls-remote origin failed: fatal: .*\); no ticket is held back$' <<< "$PICK_ERR" \
  || fail "pick: an unreadable origin should leave exactly one warning line: $PICK_ERR"
git -C "$SANDBOX" remote set-url origin "$SANDBOX/.fake-origin.git"
# A record file that is not JSON: the same.
cp "$STOPS" "$STOPS.bak"; echo 'not json' > "$STOPS"
picks "$ISSUE" 'an unreadable record file'
[ "$(printf '%s\n' "$PICK_ERR" | grep -c .)" = 1 ] && grep -q '^pick: WARN: the merge gate waits could not be read (bureau supervision: .*); no ticket is held back$' <<< "$PICK_ERR" \
  || fail "pick: an unreadable record file should leave exactly one warning line: $PICK_ERR"
mv "$STOPS.bak" "$STOPS"
age_record 1; picks "$OTHER" 'the record file is back'
# A record that is not a gate wait (a --no-merge stop, an approval kept after a blocked
# gate) is not this rule's business.
jq --arg i "$ISSUE" '.[$i] |= del(.merge_gate_wait)' "$STOPS" > "$STOPS.tmp" && mv "$STOPS.tmp" "$STOPS"
picks "$ISSUE" 'a record that is not a gate wait'
echo 'PASS pick: a push, a deleted branch, an unreadable origin or record file (one warning line), and a record that is not a gate wait hold nothing back'
fi

# ── merge: the merge stage marks its gate, and the merge picker holds it ───
if want merge; then
new_sandbox
export BUREAU_STUB_STATE_MERGE=state-merge BUREAU_STUB_AGENT_ENABLED=merge
real_picker
MARKS="$COMMON/bureau/merge-gate-waits.json"
mark() { jq -c --arg i "$ISSUE" '.[$i] // empty' "$MARKS" 2>/dev/null || true; }
merge_run() { : > "$SANDBOX/calls.log"; rm -f "$PR2_GH/merge_calls.log"; run_pipeline merge-pipeline.sh "$@" "$ISSUE"; }
# The review's APPROVE the gate reads.
jq -n '[{createdAt: "2026-09-29T09:00:00Z", body: "## Code Review v2 — EXP-801\n\n**Verdict**: APPROVE"}]' > "$PR2_GH/comments.json"
HEAD_SHA=$(jq -r .headRefOid "$PR2_GH/pr.json")
calls=$(gate_wait_calls)
picks "$ISSUE" 'the merge picker without any mark' merge-pipeline.sh
[ "$(gate_wait_calls)" = "$calls" ] || fail 'merge: without a mark file the merge picker still started the gate-waits read'
pr2_checks pending
merge_run
[ "$LAST_RC" = 2 ] || fail "merge: a pending gate ended the merge stage $LAST_RC, wanted 2"
picks "$OTHER" 'the merge picker, a fresh not-yet gate' merge-pipeline.sh
[ "$(mark | jq -r '"\(.outcome) \(.gate_waits) \(.head) \(.branch)"')" = "not-yet 1 $HEAD_SHA test-branch" ] || fail "merge: no mark for the not-yet gate: $(mark)"
grep -qE "$(held_line 'not yet' 1)" <<< "$PICK_ERR" || fail "merge: the hold is not logged as it should be: $PICK_ERR"
# Not for the rebase stage (it resolves a conflicted gate) and not for the review picker.
picks "$ISSUE" 'the rebase picker with a merge mark' rebase-pipeline.sh
picks "$ISSUE" 'the review picker with a merge mark' code-review-pipeline.sh
grep -q 'waits on its merge gate' <<< "$PICK_ERR" && fail 'merge: the review picker consulted the merge marks'
# Alone in Merge: taken anyway.
echo "$ISSUE" > "$PICKBIN/issues"; picks "$ISSUE" 'the only Merge ticket' merge-pipeline.sh; echo "$ISSUE $OTHER" > "$PICKBIN/issues"
# A blocked gate (the merge stage sets no needs-human) is held the same way, and counts on.
pr2_checks red
merge_run
[ "$LAST_RC" = 25 ] || fail "merge: a red gate ended $LAST_RC, wanted 25"
[ "$(mark | jq -r '"\(.outcome) \(.gate_waits)"')" = 'blocked 2' ] || fail "merge: the blocked gate did not count on: $(mark)"
picks "$OTHER" 'the merge picker, a blocked mark' merge-pipeline.sh
grep -qE "$(held_line blocked 2)" <<< "$PICK_ERR" || fail "merge: the blocked hold is not logged: $PICK_ERR"
age_record 570 "$MARKS"; picks "$OTHER" 'the second answer, 570 s' merge-pipeline.sh
age_record 630 "$MARKS"; picks "$ISSUE" 'the second answer, 630 s' merge-pipeline.sh
# A new head: the mark of the old head holds nothing, and the count starts again.
push_head 'fix pushed'
HEAD_SHA=$(jq -r .headRefOid "$PR2_GH/pr.json")
age_record 1 "$MARKS"; picks "$ISSUE" 'a new head before the next merge run' merge-pipeline.sh
pr2_checks pending
merge_run
[ "$LAST_RC" = 2 ] && [ "$(mark | jq -r '"\(.gate_waits) \(.head)"')" = "1 $HEAD_SHA" ] || fail "merge: a new head did not start the count again (rc $LAST_RC, $(mark))"
# Green: merged, and the mark is gone.
pr2_checks green
merge_run
[ "$LAST_RC" = 0 ] && pr2_merged || fail "merge: the green gate did not merge (rc $LAST_RC)"
[ -z "$(mark)" ] || fail "merge: the mark survived the merge: $(mark)"
# A dry run marks nothing.
pr2_checks pending
jq '.state = "OPEN"' "$PR2_GH/pr.json" > "$PR2_GH/pr.tmp" && mv "$PR2_GH/pr.tmp" "$PR2_GH/pr.json"
merge_run --dry-run
[ "$LAST_RC" = 0 ] && [ -z "$(mark)" ] || fail "merge: a dry run marked the gate (rc $LAST_RC, $(mark))"
# The review stage's inline merge keeps its own record and writes no merge mark.
export BUREAU_STUB_STATE_MERGE='' BUREAU_STUB_AGENT_ENABLED=''
review
[ "$LAST_RC" = 2 ] && [ "$(field merge_gate_wait)" = true ] && [ -z "$(mark)" ] || fail "merge: the inline merge wrote a merge mark (rc $LAST_RC, $(mark))"
echo 'PASS merge: a not-yet or blocked merge gate marks the ticket; the merge picker (only it) puts it after the other Merge tickets and takes it when alone; the count follows the head; a merge clears the mark; a dry run and the inline merge mark nothing'
fi

echo 'OK test_review_gate_waits'
