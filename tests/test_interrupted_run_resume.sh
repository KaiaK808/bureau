#!/bin/bash
# After an interrupted run the operator is told the whole way back (v3.1.0-rc.2).
#
# A run cancelled by SIGTERM (exit 130) keeps its leases and its worktree, and the worktree
# loses its disposable-worker registration so that no later reset erases the work. The
# message said only "work preserved; inspect processes and explicitly release ownership",
# three times (once per nested wrapper). Releasing alone and rerunning ended in exit 21
# ("refusing to reset unregistered worktree"), and the spec stage's feature branch, never
# pushed, stayed behind to block the next spec run (observed in two pilot runs).
#
# Runs the REAL shepherd.sh → bureau-worker.sh → probe stage chain (tests/lib/pr5-interrupt.sh)
# with the real runtime and config; Linear and Telegram are the curl double.
#   1. SIGTERM in the stage: exit 130; one message, from the run's owner, that names the run,
#      the ticket and the worktree, and gives the release command, the removal of the
#      worktree and of its unpushed local branch, the --worktree alternative and the rerun;
#      nothing written to Linear but the shepherd's own claim label; the work is still there
#   2. the steps, run exactly as printed, resume: the rerun goes through to Done
#   3. the steps follow the checkout: `git branch -D` only for a branch never pushed and without
#      commits of its own; commits that are on no remote, or not on origin/<branch>, are
#      pushed first, never deleted; a pushed branch needs no deletion; the main checkout, a missing
#      worktree and a directory Git does not know are never offered to `git worktree remove`
#      (bureau-runtime.py's resume_steps on real repos)
#   4. a run interrupted before its stage wrote anything: the worker leaves the clean worktree
#      and the runtime records who preserved it (the record the reset's refusal reads); no
#      branch to delete, the worktree still to drop
# Negative control: against v3.1.0-rc.1 (5184cf8) the message has no release command and no
# worktree, and case 1 fails.
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

# settle — wait until no process of the sandbox repository is left (the nested wrappers and
# the worker finish after the shepherd's own runtime has returned).
settle() {
  local i=0
  while ps -A -o args= | grep -F "$REPO/" | grep -v grep >/dev/null && [ "$i" -lt 100 ]; do
    sleep 0.1; i=$((i + 1))
  done
}

# ── 1. SIGTERM in the stage ────────────────────────────────────────────────────────────
pr5_new_repo
pr5_ticket 7 '["lane-2"]'
pr5_shepherd_start || fail "1: the probe stage did not start"
RUN=$(pr5_run_id)
[ -n "$RUN" ] || fail "1: no run holds EXP-7"
pr5_interrupt
settle
ERR=$(cat "$SB/err")
[ "$RC" = 130 ] || fail "1: an interrupted shepherd ended $RC, wanted 130"
[ "$(grep -c '^Interrupted Bureau run ' <<< "$ERR")" = 1 ] || fail "1: the interrupt message is not printed exactly once"
MSG=$(sed -n '/^Interrupted Bureau run /,$p' <<< "$ERR")
grep -q "^Interrupted Bureau run $RUN (EXP-7): work preserved" <<< "$MSG" || fail "1: the message does not name the run and the ticket"
grep -qF "Worktree: $WT (" <<< "$MSG" || fail "1: the message does not name the worktree"
grep -qxF "         python3 scripts/bureau-runtime.py release $RUN" <<< "$MSG" || fail "1: no release command for $RUN"
grep -qxF "         git worktree remove --force '$WT'" <<< "$MSG" || fail "1: no (quoted) removal of the preserved worktree"
grep -qxF "         git branch -D 145-probe-feature" <<< "$MSG" || fail "1: the unpushed local branch is not named for deletion"
grep -q 'local branch 145-probe-feature, which was never pushed and has no commits of its own' <<< "$MSG" || fail "1: the message does not say why the branch may go"
grep -qF 'shepherd.sh --worktree DIR' <<< "$MSG" || fail "1: the --worktree alternative is missing"
grep -q 'stops with exit 21 until it is dropped' <<< "$MSG" || fail "1: the message does not say a rerun on it stops with 21"
grep -q '3\. Rerun the shepherd or the stage\.' <<< "$MSG" || fail "1: the rerun step is missing"
[ "$(pr5_writes 7)" = "$(printf 'add-label 7 shepherd-focused\nremove-label 7 shepherd-focused')" ] \
  || fail "1: the cancelled run wrote to Linear: $(pr5_writes 7 | tr '\n' ';')"
[ ! -s "$SB/alerts.log" ] || fail "1: the cancelled run sent an alert"
[ "$(git -C "$WT" branch --show-current)" = 145-probe-feature ] || fail "1: the worktree was taken off the branch it was building"
[ "$(cat "$WT/probe-work.txt")" = 'spec draft' ] || fail "1: the stage's work is gone"
jq -e --arg r "$RUN" '.run_id == $r and .issue == "EXP-7" and .reason == "interrupted" and .branch == "145-probe-feature"' \
  "$COMMON"/bureau/preserved/*.json >/dev/null || fail "1: no record of who preserved the worktree"
echo "PASS 1 an interrupted run prints the full way back once, with its run and worktree, and writes nothing"

# ── 2. the printed steps, run as printed, resume ───────────────────────────────────────
STEPS=$(sed -n 's/^         //p' <<< "$MSG")
[ "$(grep -c . <<< "$STEPS")" = 3 ] || fail "2: expected three commands in the message, got: $STEPS"
while IFS= read -r step; do
  (cd "$REPO" && PATH="$SB/bin:$PATH" bash -c "$step" >/dev/null 2>"$SB/step.err") || { ERR=$(cat "$SB/step.err"); fail "2: the printed step failed: $step"; }
done <<< "$STEPS"
printf finish > "$SB/probe-mode"
pr5_shepherd
[ "$RC" = 0 ] || fail "2: the rerun after the printed steps ended $RC, wanted 0"
grep -qx EXP-7 "$SB/finished.log" || fail "2: the rerun did not run the stage"
[ "$(cat "$SB/state")" = s8 ] || fail "2: the ticket did not reach Done"
[ ! -e "$COMMON/bureau/preserved" ] || [ -z "$(ls "$COMMON/bureau/preserved")" ] || fail "2: the preserved record outlived the reset"
echo "PASS 2 the printed steps, run exactly as printed, resume the ticket to Done"

# ── 3. the steps follow the checkout ───────────────────────────────────────────────────
PYTHONDONTWRITEBYTECODE=1 python3 - "$PR5_SCRIPTS/bureau-runtime.py" "$SB/steps repo" <<'PY' || fail "3: resume_steps"
import importlib.util, subprocess, sys
from pathlib import Path
spec = importlib.util.spec_from_file_location('runtime', sys.argv[1]); r = importlib.util.module_from_spec(spec); spec.loader.exec_module(r)
base = Path(sys.argv[2]); base.mkdir()
origin, repo = base / 'origin.git', base / 'main checkout'
def git(cwd, *a): subprocess.run(['git', '-C', str(cwd), *a], check=True, capture_output=True)
git(base, 'init', '-q', '--bare', str(origin)); git(base, 'init', '-q', '-b', 'main', str(repo))
git(repo, 'config', 'user.name', 't'); git(repo, 'config', 'user.email', 't@t')
git(repo, 'commit', '-q', '--allow-empty', '-m', 'init'); git(repo, 'remote', 'add', 'origin', str(origin)); git(repo, 'push', '-q', 'origin', 'main')
store = r.Store(repo)
def steps(ws): return r.render_steps(r.resume_steps(store, repo, 'a' * 32, ws, 'Rerun.'))
pushed = base / 'pushed wt'; git(repo, 'worktree', 'add', '-q', '-b', 'feat/pushed', str(pushed), 'main'); git(pushed, 'push', '-q', 'origin', 'feat/pushed')
text = steps(str(pushed))
assert "git worktree remove --force '" + str(pushed) + "'" in text, text
assert 'git branch -D' not in text and 'git push' not in text, 'a pushed, even branch gets a push or a deletion:\n' + text
assert 'Its branch feat/pushed is on origin; deleting it is not needed' in text, text
# Finished work that did not reach origin (an implement stage whose final push failed):
# pushed, never deleted.
git(pushed, 'commit', '-q', '--allow-empty', '-m', 'one'); git(pushed, 'commit', '-q', '--allow-empty', '-m', 'two')
text = steps(str(pushed))
assert 'git branch -D' not in text, 'commits that are not on origin are offered for deletion:\n' + text
assert '         git push origin feat/pushed\n' in text and '2 commit(s) that are not on origin/feat/pushed' in text, text
assert text.index('git push origin') < text.index('git worktree remove'), 'the worktree goes before its commits are pushed:\n' + text
assert 'Deleting feat/pushed is not needed' in text, text
# A branch that was never pushed: deleted only while it has no commits of its own.
fresh = base / 'fresh wt'; git(repo, 'worktree', 'add', '-q', '-b', 'feat/fresh', str(fresh), 'main')
text = steps(str(fresh))
assert 'git branch -D feat/fresh' in text and 'never pushed and has no commits of its own' in text, text
git(fresh, 'commit', '-q', '--allow-empty', '-m', 'local work')
text = steps(str(fresh))
assert 'git branch -D' not in text, 'a never-pushed branch with commits is offered for deletion:\n' + text
assert 'git push -u origin feat/fresh' in text and '1 commit(s) that are on no remote' in text, text
assert '         git branch -m feat/fresh feat/fresh-saved' in text, 'the rename is not a command of the step:\n' + text
assert text.index('git push -u origin') < text.index('git branch -m'), 'the branch is renamed before it is pushed:\n' + text
text = steps(str(repo))
assert 'worktree remove' not in text and 'branch -D' not in text, 'the main checkout is offered for removal:\n' + text
assert 'release ' + 'a' * 32 in text, text
text = steps(str(base / 'never created'))
assert 'worktree remove' not in text, 'a missing worktree is offered for removal:\n' + text
for plain in (base / 'plain dir', repo / '.worktrees' / 'plain dir', pushed / 'sub dir'):
    plain.mkdir(parents=True)
    text = steps(str(plain))
    assert 'worktree remove' not in text, 'a directory Git does not know is offered to git worktree remove:\n' + text
PY
echo "PASS 3 the steps follow the checkout: pushed, ahead, main checkout, missing worktree, plain directory"

# ── 4. interrupted before the stage wrote anything ─────────────────────────────────────
pr5_new_repo
printf idle > "$SB/probe-mode"
pr5_shepherd_start || fail "4: the probe stage did not start"
RUN=$(pr5_run_id)
pr5_interrupt
settle
ERR=$(cat "$SB/err")
[ "$RC" = 130 ] || fail "4: the interrupted shepherd ended $RC"
MSG=$(sed -n '/^Interrupted Bureau run /,$p' <<< "$ERR")
grep -qxF "         git worktree remove --force '$WT'" <<< "$MSG" || fail "4: the clean worktree is not offered for removal"
grep -q 'git branch -D' <<< "$MSG" && fail "4: a branch is offered for deletion although none was made"
jq -e --arg r "$RUN" '.run_id == $r and .issue == "EXP-7" and .reason == "interrupted"' "$COMMON"/bureau/preserved/*.json >/dev/null \
  || fail "4: the runtime did not record who preserved the clean worktree"
echo "PASS 4 a run interrupted before any write: the worktree to drop, no branch, and the record of its run"
