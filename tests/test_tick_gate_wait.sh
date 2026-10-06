#!/bin/bash
# The bounded tick moves on when its only pick in a stage waits on its merge gate (v3.2).
#
# bureau-tick.sh runs one stage per tick. Since v3.2 the review and merge pickers put a ticket
# whose merge gate was not yet decided after every other ticket of their stage, and still take
# it when nothing else of that stage can be picked (merge_gate_waits). The tick with
# --allow-merge took such a lone ticket: the stage ended 2 and the tick ended, so the stages
# after it never ran while the gate stayed undecided. Now the tick passes over that stage and
# comes back to it only when no other stage has a ticket to run.
#
#   A  --allow-merge, a held Build Review ticket alone in its stage, a Build ticket: implement runs
#   B  the same with a held Merge ticket (merge stage on), with the rebase stage off and on: implement
#      runs; the rebase stage shares the merge picker but does not take the ticket passed over
#   C  a held ticket and no other work anywhere (review; merge with the rebase stage on): the held
#      ticket is taken, in its own stage
#   D  the hold has run out: the ticket is taken in its stage's turn, nothing is passed over
#   E  --stage code_review: there is no other stage, the held ticket is taken as before
#   F  the default --no-merge tick: unchanged, its review boundary check skips the held ticket
#   G  a held and a fresh ticket in Build Review, and a Build ticket: the review of the fresh one runs
#   H  held tickets in Merge and in Build Review, nothing else; the Merge ticket leaves Merge before
#      the tick comes back: the review ticket is taken
#   I  held tickets in Merge and in Build Review, nothing else: the tick comes back in stage order and
#      takes the Merge ticket
#   J  merge stage off, rebase stage on: rebase still takes a Merge ticket the tick did not pass over
#
# Runs the REAL bureau-tick.sh, bureau-config.sh (picker, merge_gate_waits), bureau-supervision.py
# (the records are written by its real `stop --merge-gate-wait` and `merge-wait`, as the review
# and merge stages write them) and bureau-worker.sh in its dry-run mode, which names the ticket
# and stage it was given and starts nothing. Doubles: Linear (curl) and GitHub (gh). The sandbox
# has its own HOME, git config and bare origin. Negative control: against 0153ccf A and B take
# the held ticket, and C lacks the tick's lines; against 25c66da (the first version of this
# change) the rebase stage takes the Merge ticket passed over in B and C, and H prints an
# overstated come-back line.
set -uo pipefail
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SB=$(mktemp -d -t bureau-test.tick-gate-wait.XXXXXXXX)
SB=$(cd "$SB" && pwd -P)
trap 'rm -rf "$SB"' EXIT
export HOME="$SB/home" GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_NOSYSTEM=1 GIT_TERMINAL_PROMPT=0
mkdir -p "$HOME" "$SB/bin"
FAILS=0
fail() { echo "FAIL $*" >&2; sed 's/^/  | /' "$SB/tick.err" 2>/dev/null | tail -25 >&2; FAILS=$((FAILS + 1)); }

# Linear: the tickets of $SB/tickets.json ({"ID": "state id"}) by state (the picker) and by number
# (state, branch, detail reads). "s8>s7" is a ticket in s8 that has moved to s7 once a pick saw it
# (it leaves its state during the tick). The tick under test writes nothing to Linear: a mutation fails.
cat > "$SB/bin/curl" <<'PY'
#!/usr/bin/env python3
import json, os, re, sys
args = sys.argv[1:]
query = json.loads(args[args.index('-d') + 1])['query']
tickets = json.load(open(os.path.join(os.environ['TICK_SB'], 'tickets.json')))
names = {'s5': 'Build', 's6': 'Build Review', 's8': 'Merge', 's7': 'Done'}
def node(ident, state):
    return {'id': 'id-' + ident, 'identifier': ident, 'title': 'Ticket ' + ident, 'description': 'd', 'priority': 1,
            'createdAt': '2026-01-0' + ident[-1], 'state': {'id': state, 'name': names.get(state, state)},
            'labels': {'nodes': [{'name': 'lane-2'}, {'name': 'ai-implementable'}]}, 'inverseRelations': {'nodes': []},
            'branchName': 'b-' + ident, 'comments': {'nodes': []}, 'project': None}
number = re.search(r'number: \{ eq: (\d+)', query)
state = re.search(r'state: \{ id: \{ eq: "([^"]+)"', query)
now = {i: s.split('>')[0] for i, s in tickets.items()}
if 'viewer' in query:
    data = {'viewer': {'id': 'viewer'}}
elif 'mutation' not in query and number:
    data = {'issues': {'nodes': [node(i, s) for i, s in now.items() if i == 'VFY-' + number.group(1)]}}
elif 'mutation' not in query and state:
    data = {'issues': {'nodes': [node(i, s) for i, s in now.items() if s == state.group(1)]}}
    moved = {i: (s.split('>', 1)[1] if '>' in s and now[i] == state.group(1) else s) for i, s in tickets.items()}
    if moved != tickets:
        json.dump(moved, open(os.path.join(os.environ['TICK_SB'], 'tickets.json'), 'w'))
else:
    sys.exit(9)
print(json.dumps({'data': data}))
PY
# GitHub: the open PR #7 against main (the --no-merge review boundary check asks for it).
printf '#!/bin/sh\ncase "$*" in "pr view 7 --json state,baseRefName") echo %s ;; *) exit 9 ;; esac\n' \
  "'{\"state\":\"OPEN\",\"baseRefName\":\"main\"}'" > "$SB/bin/gh"
chmod +x "$SB/bin/curl" "$SB/bin/gh"

DETAIL='{"identifier":"VFY-901","title":"Ticket VFY-901","description":"d","labels":["lane-2","ai-implementable"]}'
# new_repo <merge agent> [<rebase agent>] (true|false): the sandbox repository with the real
# scripts, a bare origin holding main and the branch b-VFY-901 at $HEAD_SHA, and no records yet.
new_repo() {
  rm -rf "$SB/repo" "$SB/origin.git"
  R="$SB/repo"
  git init -q -b main "$R"; git -C "$R" config user.email t@bureau; git -C "$R" config user.name t
  git -C "$R" commit -q --allow-empty -m init
  git init -q --bare "$SB/origin.git"; git -C "$R" remote add origin "$SB/origin.git"; git -C "$R" push -q origin main
  HEAD_SHA=$(head_of VFY-901)
  BASE_SHA=$(git -C "$R" rev-parse main)
  mkdir -p "$R/scripts"; cp -R "$REPO_ROOT/templates/scripts/." "$R/scripts/"
  printf 'scripts/\nlogs/\n.bureau.json\n' >> "$R/.git/info/exclude"
  jq -n --argjson merge "$1" --argjson rebase "${2:-false}" '{linear: {teams: [{id: "team-id", key: "VFY", name: "Verify",
      states: {triage: "s1", spec: "s2", spec_review: "s3", design: "s4", build: "s5", build_review: "s6", merge: "s8", done: "s7"}}],
    labels: {lane2: {id: "l1", name: "lane-2"}, needs_human: {id: "l2", name: "needs-human"},
             needs_ux: {id: "l3", name: "needs-ux"}, ai_implementable: {id: "l4", name: "ai-implementable"}}, projects: []},
    agents: {poll_interval_minutes: 30, code_review: true, implement: true, merge: $merge, rebase: $rebase}, repo: {}}' > "$R/.bureau.json"
}
# head_of <ID>: the head of the branch b-<ID> on origin, created on first use.
head_of() {
  local head
  head=$(git -C "$R" ls-remote origin "refs/heads/b-$1" | cut -f1)
  if [ -z "$head" ]; then
    head=$(git -C "$R" commit-tree 'main^{tree}' -p main -m "work $1")
    git -C "$R" push -q origin "$head:refs/heads/b-$1"
  fi
  printf '%s' "$head"
}
tickets() { printf '%s' "$1" > "$SB/tickets.json"; }
sup() { (cd "$R" && python3 -I scripts/bureau-supervision.py --repo "$R" "$@"); }
# The review stage's record after its inline merge found the gate not yet decided.
review_wait() {
  printf '%s' "$DETAIL" | sup stop VFY-901 --branch b-VFY-901 --state 'Build Review' --head "$HEAD_SHA" --base "$BASE_SHA" \
    --base-ref main --reviewed-head "$HEAD_SHA" --pr 7 --verdict APPROVE --merge-gate-wait --gate-waits 1 >/dev/null
}
# The merge stage's mark after a gate that was not yet decided ([ID], default VFY-901).
merge_wait() { sup merge-wait "${1:-VFY-901}" --branch "b-${1:-VFY-901}" --head "$(head_of "${1:-VFY-901}")" --outcome not-yet >/dev/null; }
# tick [args]: one real tick, the worker in its dry-run mode; sets RC, RESULT and $SB/tick.err.
tick() {
  rm -f "$R/logs/bureau-tick.json"
  (cd "$R" && env -u BUREAU_QUEUE_POLL_SECONDS -u BUREAU_ACTIVE_ENTRY -u BUREAU_RUN_ID -u BUREAU_CURRENT_ISSUE \
    PATH="$SB/bin:$PATH" TICK_SB="$SB" LINEAR_API_KEY=fake BUREAU_LINEAR_RETRIES=0 BUREAU_DRY_RUN=1 \
    BUREAU_CONFIG="$R/.bureau.json" /bin/bash scripts/bureau-tick.sh "$@" >"$SB/tick.out" 2>"$SB/tick.err")
  RC=$?
  RESULT=$(jq -c '{outcome, issue, stage, exit_code, skipped_reviews}' "$R/logs/bureau-tick.json" 2>/dev/null || echo none)
}
# ran <ID> <pipeline>: the worker was given exactly this ticket and stage, and nothing else.
ran() { [ "$(grep -c '^\[DRY_RUN\] ' "$SB/tick.err")" = 1 ] && grep -q "^\[DRY_RUN\] $1 $2 " "$SB/tick.err"; }
has() { grep -qF -- "$1" "$SB/tick.err"; }
# line <text>: the whole line is in the tick's output.
line() { grep -qxF -- "$1" "$SB/tick.err"; }
passed() { printf 'tick: %s waits on its merge gate and is the only %s ticket that can be picked — passed over while another stage has work' "$1" "$2"; }
PASSED_REVIEW=$(passed VFY-901 code_review)
PASSED_MERGE=$(passed VFY-901 merge)
BACK_REVIEW='tick: no other stage has work — back to code_review'
BACK_MERGE='tick: no other stage has work — back to merge'
TAKEN='pick: VFY-901 taken although it waits on its merge gate — no other ticket of the stage can be picked'

# ── A  a held Build Review ticket alone in its stage, and a Build ticket ──────────
new_repo false; tickets '{"VFY-901": "s6", "VFY-902": "s5"}'; review_wait
for n in 1 2; do
  tick --allow-merge
  if [ "$RC" = 0 ] && ran VFY-902 implement-pipeline.sh && [ "$RESULT" = '{"outcome":"waiting","issue":"VFY-902","stage":"implement","exit_code":0,"skipped_reviews":[]}' ] \
     && has "$PASSED_REVIEW" && ! has "$TAKEN" && ! has 'tick: no other stage'; then :
  else fail "A.$n: the tick did not pass over the held review ticket for the Build ticket (rc $RC, $RESULT)"; fi
done
grep -q '^pick: VFY-901 waits on its merge gate (not yet) at the unchanged head' "$SB/tick.err" || fail 'A: the picker did not hold VFY-901, so A proves nothing'
[ "$(sup status | jq -r '.review_stops["VFY-901"].merge_gate_wait')" = true ] || fail 'A: passing over the ticket changed its record'
[ "$FAILS" = 0 ] && echo 'PASS A --allow-merge: a lone held Build Review ticket is passed over, implement runs (twice in a row)'

# ── B  the same at Merge ────────────────────────────────────────────────────────────
F0=$FAILS
for rebase in false true; do
  new_repo true "$rebase"; tickets '{"VFY-901": "s8", "VFY-902": "s5"}'; merge_wait
  tick --allow-merge
  if [ "$RC" = 0 ] && ran VFY-902 implement-pipeline.sh && [ "$(jq -r '"\(.issue) \(.stage)"' <<< "$RESULT")" = 'VFY-902 implement' ] \
     && line "$PASSED_MERGE" && ! has "$TAKEN"; then :
  else fail "B (rebase stage $rebase): the tick did not pass over the held Merge ticket for the Build ticket (rc $RC, $RESULT)"; fi
done
[ "$FAILS" = "$F0" ] && echo 'PASS B --allow-merge: a lone held Merge ticket is passed over, also by the rebase stage; implement runs'

# ── C  a held ticket and no other work anywhere: taken ──────────────────────────────
F0=$FAILS
new_repo false; tickets '{"VFY-901": "s6"}'; review_wait
tick --allow-merge
if [ "$RC" = 0 ] && ran VFY-901 code-review-pipeline.sh && [ "$(jq -r '"\(.outcome) \(.issue) \(.stage)"' <<< "$RESULT")" = 'waiting VFY-901 code_review' ]; then :
else fail "C: the held review ticket was not taken when no other stage has work (rc $RC, $RESULT)"; fi
line "$PASSED_REVIEW" && line "$BACK_REVIEW" && has "$TAKEN" || fail 'C: the tick did not say that it passed over code_review and came back to it'
# In this order: passed over, back to the stage, taken by the picker, then the worker.
order=""
for line in "$PASSED_REVIEW" "$BACK_REVIEW" "$TAKEN" '[DRY_RUN] VFY-901 '; do order="$order $(grep -nF -m 1 -- "$line" "$SB/tick.err" | cut -d: -f1)"; done
[ "$(wc -w <<< "$order")" -eq 4 ] && [ "$order" = " $(tr ' ' '\n' <<< "$order" | sed '/^$/d' | sort -n | tr '\n' ' ' | sed 's/ $//')" ] \
  || fail "C: the lines are not in the order passed over, back, taken, worker (lines$order)"
# At Merge with the rebase stage on: the ticket is taken by the merge stage when the tick comes back.
new_repo true true; tickets '{"VFY-901": "s8"}'; merge_wait
tick --allow-merge
if [ "$RC" = 0 ] && ran VFY-901 merge-pipeline.sh && [ "$(jq -r '"\(.issue) \(.stage)"' <<< "$RESULT")" = 'VFY-901 merge' ]; then :
else fail "C: the held Merge ticket was not taken by the merge stage when no other stage has work (rc $RC, $RESULT)"; fi
line "$PASSED_MERGE" && line "$BACK_MERGE" && has "$TAKEN" || fail 'C: the tick did not say that it passed over merge and came back to it'
[ "$FAILS" = "$F0" ] && echo 'PASS C --allow-merge: with no other work anywhere the held ticket is taken in its own stage (Build Review; Merge with the rebase stage on)'

# ── D  the hold has run out: taken in its stage's turn ──────────────────────────────
F0=$FAILS
new_repo false; tickets '{"VFY-901": "s6", "VFY-902": "s5"}'; review_wait
# Two poll intervals of 30 min (3600 s) after the first "not yet": two hours ago is past it.
(cd "$R" && python3 -I - "$(git rev-parse --git-common-dir)/bureau/review-stops.json" <<'PY'
import json, sys, time
path = sys.argv[1]; stops = json.load(open(path)); stops['VFY-901']['stopped_at'] = time.time() - 7200
json.dump(stops, open(path, 'w'))
PY
)
tick --allow-merge
if [ "$RC" = 0 ] && ran VFY-901 code-review-pipeline.sh && [ "$(jq -r '"\(.issue) \(.stage)"' <<< "$RESULT")" = 'VFY-901 code_review' ] \
   && ! has 'waits on its merge gate'; then :
else fail "D: a ticket whose hold ran out was not taken in its stage's turn (rc $RC, $RESULT)"; fi
[ "$FAILS" = "$F0" ] && echo 'PASS D --allow-merge: once the hold has run out the ticket is taken in its turn, before implement'

# ── E  --stage given: no other stage to move to, taken as before ───────────────────
F0=$FAILS
new_repo false; tickets '{"VFY-901": "s6", "VFY-902": "s5"}'; review_wait
tick --allow-merge --stage code_review
if [ "$RC" = 0 ] && ran VFY-901 code-review-pipeline.sh && has "$TAKEN" && ! has 'tick: '; then :
else fail "E: --stage code_review did not take the held ticket as before (rc $RC, $RESULT)"; fi
[ "$FAILS" = "$F0" ] && echo 'PASS E --allow-merge --stage code_review: the held ticket is taken as before'

# ── F  the default --no-merge tick: unchanged ───────────────────────────────────────
# Its review boundary check (bureau-supervision.py check) finds the approval at the unchanged
# PR head and base, skips the ticket (skipped_reviews) and the tick goes on to implement.
F0=$FAILS
new_repo false; tickets '{"VFY-901": "s6", "VFY-902": "s5"}'; review_wait
tick
if [ "$RC" = 0 ] && ran VFY-902 implement-pipeline.sh \
   && [ "$(jq -c '[.issue, .stage, .skipped_reviews]' <<< "$RESULT")" = '["VFY-902","implement",["VFY-901"]]' ] && ! has 'tick: '; then :
else fail "F: the --no-merge tick changed (rc $RC, $RESULT)"; fi
[ "$FAILS" = "$F0" ] && echo 'PASS F --no-merge: unchanged, the review boundary check skips the held ticket and implement runs'

# ── G  a held and a fresh ticket in Build Review, and a Build ticket ─────────────────
# Only a pick that waits on its gate is passed over: the fresh ticket is reviewed.
F0=$FAILS
new_repo false; tickets '{"VFY-901": "s6", "VFY-903": "s6", "VFY-902": "s5"}'; review_wait
tick --allow-merge
if [ "$RC" = 0 ] && ran VFY-903 code-review-pipeline.sh && [ "$(jq -r '"\(.issue) \(.stage)"' <<< "$RESULT")" = 'VFY-903 code_review' ] \
   && ! has 'tick: '; then :
else fail "G: the review of the fresh Build Review ticket did not run (rc $RC, $RESULT)"; fi
[ "$FAILS" = "$F0" ] && echo 'PASS G --allow-merge: a fresh ticket next to a held one in Build Review is reviewed, nothing is passed over'

# ── H  two stages passed over; the Merge ticket leaves Merge before the tick comes back ─
F0=$FAILS
new_repo true; tickets '{"VFY-904": "s8>s7", "VFY-901": "s6"}'; merge_wait VFY-904; review_wait
tick --allow-merge
if [ "$RC" = 0 ] && ran VFY-901 code-review-pipeline.sh && [ "$(jq -r '"\(.issue) \(.stage)"' <<< "$RESULT")" = 'VFY-901 code_review' ]; then :
else fail "H: the held review ticket was not taken after the Merge ticket left (rc $RC, $RESULT)"; fi
line "$(passed VFY-904 merge)" && line "$PASSED_REVIEW" && line "$BACK_MERGE" && line "$BACK_REVIEW" \
  || fail 'H: the tick did not pass over both stages and come back to both'
[ "$FAILS" = "$F0" ] && echo 'PASS H --allow-merge: two stages passed over; back to Merge finds nothing, back to Build Review takes the held ticket'

# ── I  two stages passed over, both tickets stay: back in stage order ────────────────
F0=$FAILS
new_repo true; tickets '{"VFY-904": "s8", "VFY-901": "s6"}'; merge_wait VFY-904; review_wait
tick --allow-merge
if [ "$RC" = 0 ] && ran VFY-904 merge-pipeline.sh && [ "$(jq -r '"\(.issue) \(.stage)"' <<< "$RESULT")" = 'VFY-904 merge' ] \
   && line "$BACK_MERGE" && ! has "$BACK_REVIEW"; then :
else fail "I: the tick did not come back to Merge first (rc $RC, $RESULT)"; fi
[ "$FAILS" = "$F0" ] && echo 'PASS I --allow-merge: two stages passed over, the tick comes back in stage order and takes the Merge ticket'

# ── J  merge stage off, rebase stage on: rebase takes a Merge ticket it was not told to skip ─
F0=$FAILS
new_repo false true; tickets '{"VFY-901": "s8", "VFY-902": "s5"}'; merge_wait
tick --allow-merge
if [ "$RC" = 0 ] && ran VFY-901 rebase-pipeline.sh && [ "$(jq -r '"\(.issue) \(.stage)"' <<< "$RESULT")" = 'VFY-901 rebase' ] && ! has 'tick: '; then :
else fail "J: the rebase stage did not take the Merge ticket (rc $RC, $RESULT)"; fi
[ "$FAILS" = "$F0" ] && echo 'PASS J --allow-merge: with the merge stage off, the rebase stage still takes the Merge ticket'

[ "$FAILS" = 0 ] || { echo "$FAILS check(s) failed" >&2; exit 1; }
echo 'OK test_tick_gate_wait'
