#!/bin/bash
# A clean preserved worktree whose every commit is on origin is re-accepted, not refused with 21.
#
# A stage that fails with unfinished work keeps its worktree and loses the disposable-worker
# registration (bureau-worker.sh: "Preserved unfinished work …"). The next reset refused the
# unregistered worktree with 21, and the shepherd halted with needs-human. In an installation an
# implement pass had committed and the operator had pushed the branch: the worktree was clean and
# its HEAD equalled origin/<branch>, so nothing could be lost, yet the restart needed a manual
# `git worktree remove`. reset_worktree now registers such a worktree again and goes on.
#
# Runs the REAL bureau-worker.sh, reset_worktree and runtime (tests/lib/pr5-interrupt.sh) with an
# implement probe: its first run commits and ends 18 (the worker preserves the worktree), its
# second run records that it ran and ends 0.
#   1. preserved, clean, HEAD equal to origin/<branch> (pushed by hand), an ignored file inside:
#      re-accepted with one line that says why, the stage runs, the worker is registered again and
#      the preserved record is gone; nothing written to Linear
#   2. preserved with an uncommitted change to a tracked file: 21, the refusal as before
#   3. preserved with an untracked file: 21
#   4. preserved with a local commit origin lacks: 21
#   5. preserved, clean and pushed, but origin cannot be read: 21
#   6. the spec stage (no target branch) on a clean preserved worktree: 21
# Negative control: case 1 against a copy whose reset never re-accepts ends 21.
set -euo pipefail
source "$(dirname "$0")/lib/pr5-interrupt.sh"

fail() {
  echo "FAIL $*" >&2
  printf '  | rc=%s\n' "${RC:-}" >&2
  printf '%s\n' "${ERR:-}" | tail -30 | sed 's/^/  | err: /' >&2
  exit 1
}
pr5_setup
trap pr5_teardown EXIT

IWT=""
# preserved — a fresh repository whose implement worker committed on feat/exp-7 and failed (18):
# the worker keeps $IWT unregistered with a commit origin lacks. The probe's next run finishes.
preserved() {
  pr5_new_repo
  git -C "$REPO" branch feat/exp-7; git -C "$REPO" push -q origin feat/exp-7
  cat > "$REPO/scripts/implement-pipeline.sh" <<PROBE
#!/bin/bash
# Probe: the first run commits its work and fails (18); a later run records that it ran.
set -euo pipefail
source "\$(dirname "\$0")/bureau-config.sh"
if [ -f "\$BUREAU_ENV_FILE" ]; then bureau_load_env --export "\$BUREAU_ENV_FILE"; fi
bureau_stage_enter "\$1" "\$@"
if [ -f feature.txt ]; then echo "\$1 \$(git rev-parse HEAD)" >> "$SB/finished.log"; exit 0; fi
echo done > feature.txt; git add feature.txt
git -c user.name=t -c user.email=t@t commit -q -m 'implement iteration 1'
exit 18
PROBE
  IWT="$REPO/.worktrees/queue-implement"
  impl
  [ "$RC" = 18 ] || fail "setup: the implement probe ended $RC, wanted 18"
  grep -qF "Preserved unfinished work in $IWT" <<< "$ERR" || fail "setup: the worker did not preserve the worktree"
  [ -n "$(git -C "$IWT" rev-list origin/feat/exp-7..HEAD)" ] || fail "setup: no commit ahead of origin"
  : > "$SB/linear.log"; rm -f "$SB/finished.log"
}
impl() {
  set +e
  (cd "$REPO" && _pr5_env bash scripts/bureau-worker.sh EXP-7 implement-pipeline.sh "$IWT" feat/exp-7 > "$SB/out" 2> "$SB/err")
  RC=$?
  set -e
  OUT=$(cat "$SB/out"); ERR=$(cat "$SB/err")
}
key() { printf '%s' "$1" | shasum -a 256 | cut -d' ' -f1; }
refused() {
  [ "$RC" = 21 ] || fail "$1: the rerun ended $RC, wanted 21"
  grep -qF "refusing to reset unregistered worktree $IWT" <<< "$ERR" || fail "$1: not the reset's refusal"
  grep -q 'Re-accepted' <<< "$ERR" && fail "$1: the worktree was re-accepted"
  [ ! -s "$SB/finished.log" ] || fail "$1: the stage ran"
  [ ! -f "$COMMON/bureau/workers/$(key "$IWT")" ] || fail "$1: the worktree was registered again"
}

# ── 1. clean and pushed: re-accepted ───────────────────────────────────────────────────
preserved
git -C "$IWT" push -q origin feat/exp-7          # the operator pushes the finished commit
echo 'LOCAL=1' > "$IWT/.env"                     # ignored: does not count as a change
HEAD=$(git -C "$IWT" rev-parse HEAD)
impl
[ "$RC" = 0 ] || fail "1: the rerun on the clean preserved worktree ended $RC, wanted 0"
[ "$(grep -c '^Re-accepted the clean preserved worktree ' <<< "$ERR")" = 1 ] || fail "1: no single re-accept line"
grep -qF "Re-accepted the clean preserved worktree $IWT: no uncommitted or untracked changes and HEAD ${HEAD:0:12} is on origin/feat/exp-7" <<< "$ERR" \
  || fail "1: the re-accept line does not say why"
grep -q 'refusing to reset' <<< "$ERR" && fail "1: the reset still refused"
[ "$(cat "$SB/finished.log")" = "EXP-7 $HEAD" ] || fail "1: the stage did not run on the pushed head: $(cat "$SB/finished.log" 2>/dev/null)"
[ "$(cat "$COMMON/bureau/workers/$(key "$IWT")")" = "$(git -C "$IWT" rev-parse --absolute-git-dir)" ] || fail "1: the worker is not registered again"
[ -z "$(ls "$COMMON/bureau/preserved" 2>/dev/null)" ] || fail "1: the preserved record outlived the reset"
[ -z "$(pr5_writes 7 | grep -v shepherd-focused)" ] || fail "1: the re-accept wrote to Linear: $(pr5_writes 7 | tr '\n' ';')"
echo "PASS 1 a clean preserved worktree equal to origin is re-accepted with one line, and the stage runs"

# ── 2. an uncommitted change to a tracked file ─────────────────────────────────────────
preserved
git -C "$IWT" push -q origin feat/exp-7
echo 'edited' > "$IWT/feature.txt"
impl
refused 2
[ "$(cat "$IWT/feature.txt")" = edited ] || fail "2: the change was lost"
echo "PASS 2 an uncommitted change: 21 as before, the change kept"

# ── 3. an untracked file ───────────────────────────────────────────────────────────────
preserved
git -C "$IWT" push -q origin feat/exp-7
echo 'notes' > "$IWT/notes.txt"
impl
refused 3
[ "$(cat "$IWT/notes.txt")" = notes ] || fail "3: the untracked file was lost"
echo "PASS 3 an untracked file: 21 as before, the file kept"

# ── 4. a local commit origin lacks ─────────────────────────────────────────────────────
preserved
LOCAL=$(git -C "$IWT" rev-parse HEAD)
impl
refused 4
[ "$(git -C "$IWT" rev-parse HEAD)" = "$LOCAL" ] || fail "4: the local commit left the worktree"
echo "PASS 4 a commit origin lacks: 21 as before, the commit kept"

# ── 5. origin cannot be read ───────────────────────────────────────────────────────────
preserved
git -C "$IWT" push -q origin feat/exp-7
git -C "$REPO" remote set-url origin "$SB/no such origin"
impl
refused 5
echo "PASS 5 origin cannot be read: 21 as before"

# ── 6. the spec stage has no target branch ─────────────────────────────────────────────
preserved
git -C "$IWT" push -q origin feat/exp-7
set +e
(cd "$REPO" && _pr5_env bash scripts/bureau-worker.sh EXP-7 spec-pipeline.sh "$IWT" > "$SB/out" 2> "$SB/err"); RC=$?
set -e
ERR=$(cat "$SB/err")
[ "$RC" = 21 ] || fail "6: the spec stage on the clean preserved worktree ended $RC, wanted 21"
grep -qF "refusing to reset unregistered worktree $IWT" <<< "$ERR" || fail "6: not the reset's refusal"
echo "PASS 6 the spec stage, without a target branch: 21 as before"

# ── negative control: a reset that never re-accepts ────────────────────────────────────
preserved
git -C "$IWT" push -q origin feat/exp-7
f="$REPO/scripts/bureau-config.sh"
sed -i.bak 's/&& _bureau_reaccept_clean_worktree "\$wt" "\$target_branch" "\$common"; then/\&\& false; then/' "$f"; rm -f "$f.bak"
grep -q '_bureau_reaccept_clean_worktree "\$wt"' "$f" && fail "control: the re-accept call is still in the copy"
impl
[ "$RC" = 21 ] || fail "control: without the re-accept the clean case ended $RC, wanted 21"
grep -qF "refusing to reset unregistered worktree $IWT" <<< "$ERR" || fail "control: not the reset's refusal"
echo "PASS control: without the re-accept the clean case ends 21"
