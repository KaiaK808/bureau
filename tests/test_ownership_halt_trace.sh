#!/bin/bash
# An exit 21 over ownership leaves a trace on the ticket (v3.1.0-rc.2).
#
# A stage or shepherd that stopped with 21 because a worktree was unregistered or foreign, or
# because another run held the ticket, wrote only a line on stderr: the ticket stayed in its
# state with lane-2 and nothing on it, and a queue picked it again on every tick.
# Now the halt sets needs-human (mark_needs_human, with its local hold) and
# posts one comment naming the worktree and the way back; a repeat sets the label again but
# posts no second comment, and a cancelled run writes nothing.
#
# Runs the REAL shepherd.sh, bureau-worker.sh, reset_worktree and runtime
# (tests/lib/pr5-interrupt.sh); Linear and Telegram are the curl double.
#   1. the interrupted run was not released: the next shepherd's claim conflicts (21);
#      needs-human and one comment on the ticket with the release command, the worktree and
#      its unpushed branch; a repeat labels again, no second comment
#   2. released but not dropped (the pilot's run 2): the reset refuses the worktree (21);
#      needs-human and one comment naming the worktree, the interrupted run and the fix; a
#      repeat after a human removed the label without the fix labels again, no second comment;
#      the fix from the comment, run as written, resumes to Done and clears the records
#   3. a halt reached by a run the runtime marked interrupted writes nothing
#   4. a live holder is working: the second shepherd ends 21 and writes nothing
#   5. a worktree another ticket's run holds (the claim) or preserved (the reset; a queue shares
#      one worktree per stage): the halt goes to that ticket, once; the tickets picked after it
#      get nothing
#   6. an unregistered checkout nobody recorded: the first ticket that hits it carries the
#      halt, the next gets nothing; the checkout is untouched, and the comment does not offer
#      `git worktree remove` for a directory Git does not know as a worktree
#   7. the ticket's branch is checked out in another worktree: needs-human and a comment that
#      names the holder and how to free the branch
#   8. a registered worker whose directory now holds another repository: needs-human and a
#      comment without git commands of this repository
#   9. finished work that is not on origin (an implement stage whose final push failed, and a
#      spec stage that committed before the interrupt): the comment pushes it, never offers
#      `git branch -D`, and its commands, run as written, keep the commit (the spec branch under
#      <branch>-saved, so the rerun can create its branch again and reaches Done)
#  10. the main checkout as the worktree: the comment asks for a worktree of its own and offers
#      neither `git worktree remove` nor `git branch -D`; shepherd.sh --worktree . is refused
#      before anything is claimed or written
#  11. a signal while the claim waits for the runtime's lock, before it conflicts: exit 130 and
#      nothing written
# Negative control: against v3.1.0-rc.1 (5184cf8) every halt above writes nothing to Linear,
# and case 1 fails.
set -euo pipefail
source "$(dirname "$0")/lib/pr5-interrupt.sh"

fail() {
  echo "FAIL $*" >&2
  printf '  | rc=%s\n' "${RC:-}" >&2
  printf '%s\n' "${ERR:-}" | tail -30 | sed 's/^/  | err: /' >&2
  sed 's/^/  | linear: /' "$SB/linear.log" >&2 2>/dev/null || true
  exit 1
}
pr5_setup
trap pr5_teardown EXIT

settle() {
  local i=0
  while ps -A -o args= | grep -F "$REPO/" | grep -v grep >/dev/null && [ "$i" -lt 100 ]; do
    sleep 0.1; i=$((i + 1))
  done
}
# interrupted — a fresh repository whose shepherd run on the fixture ticket was cancelled in the stage.
interrupted() {
  pr5_new_repo
  pr5_ticket 7 '["lane-2"]'
  pr5_shepherd_start || fail "the probe stage did not start"
  RUN=$(pr5_run_id)
  pr5_interrupt
  settle
  [ "$RC" = 130 ] || fail "the interrupted shepherd ended $RC"
  : > "$SB/linear.log"
}
labels() { cat "$SB/labels/$1.json" 2>/dev/null || echo '["lane-2"]'; }
has_human() { labels "$1" | jq -e 'index("needs-human")' >/dev/null; }
label_adds() { grep -c "^add-label $1 needs-human\$" "$SB/linear.log" || true; }

# ── 1. the claim conflicts with the interrupted run ────────────────────────────────────
interrupted
pr5_shepherd
[ "$RC" = 21 ] || fail "1: the shepherd on a ticket an interrupted run holds ended $RC, wanted 21"
has_human 7 || fail "1: needs-human is not on the ticket"
[ "$(pr5_comments 7)" = 1 ] || fail "1: $(pr5_comments 7) comments, wanted 1"
BODY=$(pr5_comment 7)
grep -q '^🛑 Bureau halt (exit 21, ownership-conflict) in `shepherd`: `issue:EXP-7` is held by run '"$RUN"', which was interrupted' <<< "$BODY" \
  || fail "1: the comment does not say which run holds the ticket: $BODY"
grep -qF "Worktree: \`$WT\`" <<< "$BODY" || fail "1: the comment does not name the worktree"
grep -qxF "   python3 scripts/bureau-runtime.py release $RUN" <<< "$BODY" || fail "1: the comment has no release command"
grep -qxF "   git worktree remove --force '$WT'" <<< "$BODY" || fail "1: the comment does not drop the worktree"
grep -qxF "   git branch -D 145-probe-feature" <<< "$BODY" || fail "1: the comment does not name the unpushed branch"
grep -qxF "python3 scripts/bureau-runtime.py release $RUN" <<< "$(sed 's/^ *//' <<< "$ERR")" || fail "1: stderr has no release command"
pr5_shepherd
[ "$RC" = 21 ] || fail "1: the repeat ended $RC"
[ "$(pr5_comments 7)" = 1 ] || fail "1: the repeat posted a second comment"
[ "$(label_adds 7)" = 2 ] || fail "1: the repeat did not set needs-human again ($(label_adds 7) label writes)"
echo "PASS 1 a claim held by an interrupted run: needs-human and one comment with the release and the worktree"

# ── 2. released, not dropped: the reset refuses the worktree ───────────────────────────
interrupted
(cd "$REPO" && python3 scripts/bureau-runtime.py release "$RUN" >/dev/null)
pr5_shepherd
[ "$RC" = 21 ] || fail "2: the rerun on the unregistered worktree ended $RC, wanted 21"
grep -qF "refusing to reset unregistered worktree $WT" <<< "$ERR" || fail "2: not the reset's refusal"
has_human 7 || fail "2: needs-human is not on the ticket"
[ "$(pr5_comments 7)" = 1 ] || fail "2: $(pr5_comments 7) comments, wanted 1"
BODY=$(pr5_comment 7)
grep -q '^🛑 Bureau halt (exit 21, ownership-conflict) in `spec`: the worktree `'"$WT"'` is not registered' <<< "$BODY" \
  || fail "2: the comment does not name the worktree: $BODY"
grep -q "It was preserved when run \`$RUN\` of EXP-7 was interrupted" <<< "$BODY" || fail "2: the comment does not name the interrupted run"
grep -qF 'drop it and its local branch `145-probe-feature`, which was never pushed and has no commits of its own' <<< "$BODY" || fail "2: the comment does not name the unpushed branch"
grep -qF 'shepherd.sh --worktree DIR' <<< "$BODY" || fail "2: the --worktree alternative is missing"
grep -qF "2. Remove \`needs-human\` from EXP-7 and rerun." <<< "$BODY" || fail "2: the rerun step is missing"
grep -qF "git worktree remove --force '$WT'" <<< "$ERR" || fail "2: stderr does not carry the fix"
pr5_ticket 7 '["lane-2"]'     # a human removes the label but not the worktree
pr5_shepherd
[ "$RC" = 21 ] || fail "2: the repeat ended $RC"
has_human 7 || fail "2: the repeat did not set needs-human again"
[ "$(pr5_comments 7)" = 1 ] || fail "2: the repeat posted a second comment"
FIX=$(sed -n '/^   ```sh$/,/^   ```$/{ /```/d; s/^   //p; }' <<< "$BODY")
[ "$(grep -c . <<< "$FIX")" = 2 ] || fail "2: expected two commands in the comment, got: $FIX"
while IFS= read -r step; do
  (cd "$REPO" && bash -c "$step" >/dev/null 2>&1) || fail "2: the comment's step failed: $step"
done <<< "$FIX"
pr5_ticket 7 '["lane-2"]'
printf finish > "$SB/probe-mode"
pr5_shepherd
[ "$RC" = 0 ] || fail "2: the rerun after the comment's fix ended $RC, wanted 0"
[ "$(cat "$SB/state")" = s8 ] || fail "2: the ticket did not reach Done"
[ -z "$(ls "$COMMON/bureau/ownership-halts" 2>/dev/null)" ] || fail "2: the halt record outlived the fix"
[ -z "$(ls "$COMMON/bureau/preserved" 2>/dev/null)" ] || fail "2: the preserved record outlived the fix"
echo "PASS 2 an unregistered worktree: needs-human, one comment with the fix, and the fix resumes"

# ── 3. a halt inside a cancelled run writes nothing ────────────────────────────────────
interrupted
# The interrupted run's own worker reaches the reset after the runtime marked the run
# interrupted (a process that outlived the cancel): the same refusal, nothing written.
set +e
(cd "$REPO" && env PATH="$SB/bin:$PATH" TMPDIR="$SB/tmp" BUREAU_LINEAR_RETRIES=0 BUREAU_RUN_ID="$RUN" BUREAU_CURRENT_ISSUE=EXP-7 \
   BUREAU_CONFIG="$REPO/.bureau.json" BUREAU_ACTIVE_ENTRY="$REPO/scripts/bureau-worker.sh" \
   bash "$REPO/scripts/bureau-worker.sh" EXP-7 spec-pipeline.sh "$WT" > "$SB/out" 2> "$SB/err")
RC=$?
set -e
ERR=$(cat "$SB/err")
[ "$RC" = 21 ] || fail "3: the refusal ended $RC"
grep -q "run $RUN was cancelled — nothing written to EXP-7" <<< "$ERR" || fail "3: no note that the cancelled run writes nothing"
[ -z "$(pr5_writes 7)" ] || fail "3: a cancelled run wrote to Linear: $(pr5_writes 7 | tr '\n' ';')"
echo "PASS 3 a halt reached inside a cancelled run writes nothing"

# ── 4. a live holder ───────────────────────────────────────────────────────────────────
pr5_new_repo
pr5_ticket 7 '["lane-2"]'
pr5_shepherd_start || fail "4: the probe stage did not start"
FIRST=$SHEP_PID
: > "$SB/linear.log"
set +e
(cd "$REPO" && _pr5_env bash scripts/shepherd.sh --no-tmux --worktree .worktrees/second EXP-7 > "$SB/out2" 2> "$SB/err2")
RC=$?
set -e
ERR=$(cat "$SB/err2")
[ "$RC" = 21 ] || fail "4: a second shepherd on a ticket a live run holds ended $RC, wanted 21"
grep -q 'which is still active: wait for it or stop it; nothing was written' <<< "$ERR" || fail "4: stderr does not say the holder is active"
[ -z "$(pr5_writes 7)" ] || fail "4: a conflict with a live holder wrote to Linear: $(pr5_writes 7 | tr '\n' ';')"
SHEP_PID=$FIRST; pr5_interrupt; settle
echo "PASS 4 a live holder: 21, and nothing written"

# ── 5. another ticket's preserved worktree, shared by a queue ──────────────────────────
interrupted
# Not released yet: the claim on the worktree conflicts, and the halt goes to the holder's ticket.
pr5_worker EXP-8 "$WT"
[ "$RC" = 21 ] || fail "5: the worker on a worktree EXP-7's interrupted run holds ended $RC, wanted 21"
grep -qF "workspace:$WT is held by run $RUN, which was interrupted" <<< "$ERR" || fail "5: stderr does not name the holder of the worktree"
has_human 7 || fail "5: needs-human is not on EXP-7, whose run holds the worktree"
[ -z "$(pr5_writes 8)" ] || fail "5: EXP-8, which only met the claim, was written to: $(pr5_writes 8 | tr '\n' ';')"
(cd "$REPO" && python3 scripts/bureau-runtime.py release "$RUN" >/dev/null)
pr5_ticket 7 '["lane-2"]'
: > "$SB/comments.jsonl"; rm -rf "$COMMON/bureau/ownership-halts"
pr5_worker EXP-8 "$WT"
[ "$RC" = 21 ] || fail "5: the worker on EXP-7's preserved worktree ended $RC, wanted 21"
has_human 7 || fail "5: needs-human is not on EXP-7, whose run preserved the worktree"
[ "$(pr5_comments 7)" = 1 ] || fail "5: EXP-7 got $(pr5_comments 7) comments, wanted 1"
[ -z "$(pr5_writes 8)" ] || fail "5: EXP-8, which only hit the worktree, was written to: $(pr5_writes 8 | tr '\n' ';')"
pr5_worker EXP-9 "$WT"
[ "$RC" = 21 ] || fail "5: the next pick ended $RC"
[ "$(pr5_comments 7)" = 1 ] || fail "5: the next pick posted a second comment on EXP-7"
[ -z "$(pr5_writes 9)" ] || fail "5: EXP-9 was written to"
echo "PASS 5 a worktree another ticket's run holds or preserved: the halt goes to that ticket, once"

# ── 6. an unregistered checkout nobody recorded ────────────────────────────────────────
pr5_new_repo
QWT="$REPO/.worktrees/queue spec"
mkdir -p "$QWT"; echo 'mine' > "$QWT/notes.txt"
pr5_worker EXP-8 "$QWT"
[ "$RC" = 21 ] || fail "6: the worker on an unrecorded checkout ended $RC, wanted 21"
has_human 8 || fail "6: needs-human is not on EXP-8"
BODY=$(pr5_comment 8)
grep -qF "the worktree \`$QWT\` is not registered" <<< "$BODY" || fail "6: the comment does not name the checkout"
grep -qF 'It is no worktree of this repository: save anything you want from it, then move it away' <<< "$BODY" || fail "6: the comment does not say how to clear a plain directory"
grep -q 'git worktree remove' <<< "$BODY" && fail "6: the comment offers git worktree remove for a directory Git does not know"
pr5_worker EXP-9 "$QWT"
[ "$RC" = 21 ] || fail "6: the next pick ended $RC"
grep -q "the halt for this worktree is on EXP-8 — nothing written to EXP-9" <<< "$ERR" || fail "6: stderr does not say where the halt is"
[ -z "$(pr5_writes 9)" ] || fail "6: EXP-9 was written to: $(pr5_writes 9 | tr '\n' ';')"
[ "$(cat "$QWT/notes.txt")" = mine ] || fail "6: the checkout was touched"
echo "PASS 6 an unrecorded checkout: one ticket carries the halt, the checkout is untouched"

# ── 7. the ticket's branch is held by another worktree ─────────────────────────────────
pr5_new_repo
git -C "$REPO" branch feat/exp-7; git -C "$REPO" push -q origin feat/exp-7
HOLDER=$(mktemp -d "$SB/holder XXXXXXXX")
git -C "$REPO" worktree add -q "$HOLDER" feat/exp-7
set +e
(cd "$REPO" && _pr5_env bash scripts/bureau-worker.sh EXP-7 implement-pipeline.sh "$REPO/.worktrees/impl" feat/exp-7 > "$SB/out" 2> "$SB/err")
RC=$?
set -e
ERR=$(cat "$SB/err")
[ "$RC" = 21 ] || fail "7: a branch held elsewhere ended $RC, wanted 21"
has_human 7 || fail "7: needs-human is not on the ticket"
BODY=$(pr5_comment 7)
grep -qF "branch \`feat/exp-7\` is checked out in another worktree, \`$HOLDER\`" <<< "$BODY" || fail "7: the comment does not name the holder: $BODY"
grep -qxF "   git -C '$HOLDER' switch --detach" <<< "$BODY" || fail "7: the comment does not say how to free the branch"
[ "$(git -C "$HOLDER" branch --show-current)" = feat/exp-7 ] || fail "7: the holder was detached"
echo "PASS 7 a branch held by another worktree: needs-human and a comment naming the holder"

# ── 8. a registered worker that now holds another repository ───────────────────────────
pr5_new_repo
printf finish > "$SB/probe-mode"
pr5_worker EXP-7 "$WT"
[ "$RC" = 0 ] || fail "8: the first run to register the worker ended $RC"
rm -rf "$WT"; git init -q "$WT"
printf s1 > "$SB/state"; : > "$SB/linear.log"
pr5_worker EXP-7 "$WT"
[ "$RC" = 21 ] || fail "8: a worker with a changed identity ended $RC, wanted 21"
grep -q 'worker identity changed' <<< "$ERR" || fail "8: not the identity refusal"
has_human 7 || fail "8: needs-human is not on the ticket"
BODY=$(pr5_comment 7)
grep -qF "the worktree \`$WT\` is registered as a Bureau worker, but it now belongs to another Git checkout" <<< "$BODY" \
  || fail "8: the comment does not say what changed"
grep -q 'git worktree remove\|git branch -D' <<< "$BODY" && fail "8: the comment runs git of this repository on another repository"
echo "PASS 8 a worker whose identity changed: needs-human and a comment"

# ── 9. finished work that is not on origin ─────────────────────────────────────────────
pr5_new_repo
git -C "$REPO" branch feat/exp-7; git -C "$REPO" push -q origin feat/exp-7
cat > "$REPO/scripts/implement-pipeline.sh" <<'PROBE'
#!/bin/bash
# Probe: an implement stage that commits its work and then fails its final push (exit 18).
set -euo pipefail
source "$(dirname "$0")/bureau-config.sh"
if [ -f "$BUREAU_ENV_FILE" ]; then bureau_load_env --export "$BUREAU_ENV_FILE"; fi
bureau_stage_enter "$1" "$@"
echo done > feature.txt; git add feature.txt
git -c user.name=t -c user.email=t@t commit -q -m 'implement iteration 1 (finished work)'
exit 18
PROBE
IWT="$REPO/.worktrees/queue-implement"
set +e
(cd "$REPO" && _pr5_env bash scripts/bureau-worker.sh EXP-7 implement-pipeline.sh "$IWT" feat/exp-7 > "$SB/out" 2> "$SB/err"); RC=$?
set -e
[ "$RC" = 18 ] || { ERR=$(cat "$SB/err"); fail "9: the implement probe ended $RC, wanted 18"; }
DONE=$(git -C "$REPO" log --all --format=%H --grep='implement iteration 1' -n 1)
set +e
(cd "$REPO" && _pr5_env bash scripts/bureau-worker.sh EXP-7 implement-pipeline.sh "$IWT" feat/exp-7 > "$SB/out" 2> "$SB/err"); RC=$?
set -e
ERR=$(cat "$SB/err")
[ "$RC" = 21 ] || fail "9: the next pick on the preserved worktree ended $RC, wanted 21"
BODY=$(pr5_comment 7)
grep -q 'git branch -D' <<< "$BODY" && fail "9: finished work that is not on origin is offered for deletion: $BODY"
grep -qF 'Its branch `feat/exp-7` has 1 commit(s) that are not on origin/feat/exp-7: push them' <<< "$BODY" || fail "9: the comment does not say the commit is not on origin"
grep -qxF '   git push origin feat/exp-7' <<< "$BODY" || fail "9: the comment does not push the branch"
grep -qF "Deleting \`feat/exp-7\` is not needed: the rerun's \`git checkout -B\` resets it to origin/feat/exp-7." <<< "$BODY" || fail "9: the comment does not say deleting is not needed"
FIX=$(sed -n '/^   ```sh$/,/^   ```$/{ /```/d; s/^   //p; }' <<< "$BODY")
while IFS= read -r step; do
  (cd "$REPO" && bash -c "$step" >/dev/null 2>&1) || fail "9: the comment's step failed: $step"
done <<< "$FIX"
grep -q 'origin/feat/exp-7' <<< "$(git -C "$REPO" branch -r --contains "$DONE")" || fail "9: the finished commit is not on origin after the comment's steps"
grep -q 'feat/exp-7' <<< "$(git -C "$REPO" branch --contains "$DONE")" || fail "9: the finished commit left its branch"
# A spec stage that committed before the interrupt: its branch was never pushed.
pr5_new_repo
printf commit > "$SB/probe-mode"
pr5_shepherd_start || fail "9: the probe stage did not start"
RUN=$(pr5_run_id); pr5_interrupt; settle
(cd "$REPO" && python3 scripts/bureau-runtime.py release "$RUN" >/dev/null)
: > "$SB/linear.log"
pr5_shepherd
[ "$RC" = 21 ] || fail "9: the rerun on the unregistered worktree ended $RC, wanted 21"
BODY=$(pr5_comment 7)
grep -q 'git branch -D' <<< "$BODY" && fail "9: a never-pushed branch with a commit is offered for deletion: $BODY"
grep -qF 'Its local branch `145-probe-feature` has 1 commit(s) that are on no remote: push them' <<< "$BODY" || fail "9: the comment does not say the commit is on no remote"
grep -qxF '   git push -u origin 145-probe-feature' <<< "$BODY" || fail "9: the comment does not push the never-pushed branch"
grep -qxF '   git branch -m 145-probe-feature 145-probe-feature-saved' <<< "$BODY" || fail "9: the comment's commands do not keep the branch out of the rerun's way"
SPEC=$(git -C "$REPO" rev-parse 145-probe-feature)
FIX=$(sed -n '/^   ```sh$/,/^   ```$/{ /```/d; s/^   //p; }' <<< "$BODY")
[ "$(grep -c . <<< "$FIX")" = 3 ] || fail "9: expected three commands in the comment, got: $FIX"
while IFS= read -r step; do
  (cd "$REPO" && bash -c "$step" >/dev/null 2>&1) || fail "9: the comment's step failed: $step"
done <<< "$FIX"
grep -q 'origin/145-probe-feature' <<< "$(git -C "$REPO" branch -r --contains "$SPEC")" || fail "9: the spec commit is not on origin after the comment's steps"
[ "$(git -C "$REPO" rev-parse 145-probe-feature-saved)" = "$SPEC" ] || fail "9: the spec commit is not kept on 145-probe-feature-saved"
pr5_ticket 7 '["lane-2"]'
printf finish > "$SB/probe-mode"; printf s1 > "$SB/state"
pr5_shepherd
[ "$RC" = 0 ] || fail "9: the rerun after the comment's steps ended $RC, wanted 0"
[ "$(cat "$SB/state")" = s8 ] || fail "9: the rerun did not reach Done"
echo "PASS 9 finished work that is not on origin: pushed, never offered for deletion, kept by the steps"

# ── 10. the main checkout as the worktree ──────────────────────────────────────────────
pr5_new_repo
git -C "$REPO" checkout -q -b local-only-work
pr5_worker EXP-8 "$REPO"
[ "$RC" = 21 ] || fail "10: the worker on the main checkout ended $RC, wanted 21"
BODY=$(pr5_comment 8)
grep -qF "the worktree \`$REPO\` is the repository's main checkout, which Bureau never resets or drops" <<< "$BODY" || fail "10: the comment does not say it is the main checkout: $BODY"
grep -q 'git worktree remove\|git branch -D' <<< "$BODY" && fail "10: the comment offers to drop the main checkout or its branch: $BODY"
grep -qF 'Rerun with a worktree of its own' <<< "$BODY" || fail "10: the comment does not ask for a worktree of its own"
[ "$(git -C "$REPO" branch --show-current)" = local-only-work ] || fail "10: the main checkout was touched"
: > "$SB/linear.log"
for arg in . "$REPO"; do
  pr5_shepherd --worktree "$arg"
  [ "$RC" = 1 ] || fail "10: shepherd.sh --worktree $arg ended $RC, wanted 1"
  grep -qF 'the shepherd needs a worktree of its own' <<< "$ERR" || fail "10: shepherd.sh --worktree $arg does not say why"
done
[ ! -s "$SB/linear.log" ] || fail "10: a refused --worktree reached Linear: $(tr '\n' ';' < "$SB/linear.log")"
[ ! -s "$COMMON/bureau/leases.json" ] || [ "$(jq length "$COMMON/bureau/leases.json")" = 0 ] || fail "10: a refused --worktree claimed something"
echo "PASS 10 the main checkout: a comment that asks for a worktree of its own, and shepherd.sh refuses it up front"

# ── 11. a signal while the claim waits, before it conflicts ────────────────────────────
interrupted
python3 - "$COMMON/bureau/guard" "$SB/lock-held" "$SB/lock-release" <<'PY' &
import fcntl, os, sys, time
with open(sys.argv[1], 'a') as lock:
    fcntl.flock(lock, fcntl.LOCK_EX)
    open(sys.argv[2], 'w').close()
    while not os.path.exists(sys.argv[3]): time.sleep(0.05)
PY
LOCKER=$!
i=0; while [ ! -f "$SB/lock-held" ] && [ "$i" -lt 100 ]; do sleep 0.1; i=$((i + 1)); done
(cd "$REPO" && exec env PATH="$SB/bin:$PATH" TMPDIR="$SB/tmp" BUREAU_LINEAR_RETRIES=0 \
   bash scripts/shepherd.sh --no-tmux --worktree .worktrees/shepherd-EXP-7 EXP-7 > "$SB/out" 2> "$SB/err") &
SECOND=$!
i=0; while ! grep -q 'bureau-runtime.py' <<< "$(ps -o args= -p "$SECOND" 2>/dev/null)" && [ "$i" -lt 100 ]; do sleep 0.1; i=$((i + 1)); done
sleep 1   # the runtime has its signal handlers and waits for the lock
kill -TERM "$SECOND"
sleep 0.5
touch "$SB/lock-release"; wait "$LOCKER" || true
set +e; wait "$SECOND"; RC=$?; set -e
ERR=$(cat "$SB/err")
[ "$RC" = 130 ] || fail "11: a signal during the claim ended $RC, wanted 130"
grep -q 'bureau conflict' <<< "$ERR" && fail "11: a cancelled claim still reported the conflict"
[ -z "$(pr5_writes 7)" ] || fail "11: a cancelled claim wrote to Linear: $(pr5_writes 7 | tr '\n' ';')"
echo "PASS 11 a signal while the claim waits: 130 and nothing written"
