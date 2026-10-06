#!/bin/bash
# repo.post_implement_command and the fatal end-of-run push, on the real
# implement stage (tests/lib/harness.sh: stubbed Linear/gh/model, real git with
# a bare origin).
#
#   1  hook unset → today's behaviour (COMPLETE, Build Review, exit 0)
#   2  hook set, run made commits → runs once, in the worktree, with
#      BUREAU_ISSUE/BUREAU_BRANCH; its commit counts, is pushed, hand-off as usual
#   3  hook set, status does not release the work (NEEDS_HUMAN) → not run
#  3d  PARTIAL with commits (PR marked ready, EXP-622) → the hook runs first,
#      its file reaches origin, the PR is ready
#  3e  PARTIAL with commits and a failing hook → exit 14, the PR stays a draft
#  3f  PARTIAL without commits (goal path; the PR stays a draft) → not run
#  3c  after a halt on the hook: the next run commits nothing, the hook runs
#      anyway before the hand-off, and its file reaches origin
#   4  hook exits non-zero → halt (needs-human, draft, report), exit 14, no hand-off
#   5  hook leaves uncommitted changes → halt, exit 14, files kept and named
#   6  hook exceeds BUREAU_POST_IMPLEMENT_TIMEOUT → killed, halt, exit 14
#  6b  a child that ignores SIGTERM does not survive the timeout
#  6c  a hook that exits 124 by itself is reported as "exited 124", not a timeout
#  6d  the limit never exceeds BUREAU_IMPL_TOTAL_TIMEOUT
#  6e  BUREAU_POST_IMPLEMENT_TIMEOUT is read from .env
#   7  hook moves HEAD off the run's commits → halt, exit 14, final push skipped
#   8  dry run → hook not run
#   9  hook commit carries a CI suppressor → squash-range check halts (CI_MARKER)
#  10  every push rejected → retried once, then 18 before any hand-off
#  11  only the first push rejected → per-iter push stays non-fatal, exit 0
#  11b the end-of-run push fails twice but origin already has every commit → 0,
#      hand-off
#  11c hook fails and the push fails → 18, and the comment carries the hook's report
#  11d the pushes fail and origin/<branch>..HEAD cannot be read → 18
#  11e origin rewritten, final pushes rejected (fetch first) → fetched, 18
#  11f the pushes and the fetch fail at transport level → unreadable, 18
#  11g as 11e with a fetch refspec that covers only main → still 18
#  12  goal-loop path runs the hook too
#  14  the hook gets no stdin
#  15  hook fails and the label write fails → 25 (the hold) wins over 14
#  13  negative control: the pre-change stage (hook call removed, final push
#      non-fatal) shows the old behaviour for 2 and 10
set -euo pipefail
source "$(dirname "$0")/lib/harness.sh"

FAILS=0
fail() { echo "FAIL $*" >&2; FAILS=$((FAILS + 1)); }
check_eq() { [ "$1" = "$2" ] || fail "$3: expected '$1', got '$2'"; }
has() { printf '%s' "$2" | grep -qE -- "$1" || fail "$3 (no match for /$1/)"; }
hasnt() { if printf '%s' "$2" | grep -qE -- "$1"; then fail "$3 (unexpected /$1/)"; fi; }
calls() { cat "$SANDBOX/calls.log" 2>/dev/null || true; }

MARK_DIR=$(mktemp -d -t bureau-postimpl-mark.XXXXXX)
export MARK_DIR

setup() {  # setup <case-name> [hook-command]
  sandbox_init "EXP-100" "test-branch"
  export FAKE_CLAUDE_FIXTURES="$FIXTURES_DIR/claude_complete.txt"
  export FAKE_CLAUDE_COMMIT_ON_ITERS="1" BUREAU_DRY_RUN=0 BUREAU_IMPL_MAX_ITER=3
  unset BUREAU_USE_GOAL_LOOP BUREAU_POST_IMPLEMENT_TIMEOUT BUREAU_IMPL_TOTAL_TIMEOUT FAKE_CLAUDE_COMMIT_MSG
  export MARK="$MARK_DIR/$1"
  rm -f "$MARK"
  if [ -n "${2:-}" ]; then
    jq -n --arg c "$2" '{repo: {post_implement_command: $c}}' > "$SANDBOX/.bureau.json"
  fi
}
reject_pushes() {  # reject_pushes all|first
  local hook="$SANDBOX/.fake-origin.git/hooks/pre-receive"
  if [ "$1" = all ]; then
    printf '#!/bin/sh\necho "rejected by test" >&2\nexit 1\n' > "$hook"
  else
    printf '#!/bin/sh\nf="%s/.rejected-once"\n[ -f "$f" ] && exit 0\n: > "$f"\necho "rejected once by test" >&2\nexit 1\n' "$SANDBOX" > "$hook"
  fi
  chmod +x "$hook"
}
origin_subjects() { git -C "$SANDBOX/.fake-origin.git" log --format=%s test-branch 2>/dev/null || true; }
# The pre-change stage: no hook call, end-of-run push non-fatal again.
use_old_stage() {
  local f="$SCRIPTS_DIR/implement-pipeline.sh"
  grep -q '^run_post_implement_command$' "$f" || { fail "negative control: hook call line not found"; return; }
  grep -q 'elif ! push_branch_loud "end of run" status; then' "$f" || { fail "negative control: end-of-run push line not found"; return; }
  sed -i.bak -e '/^run_post_implement_command$/d' \
    -e 's/elif ! push_branch_loud "end of run" status; then/elif ! push_branch_loud "end of run"; then/' "$f"
}

HOOK_OK='printf "%s %s %s\n" "$BUREAU_ISSUE" "$BUREAU_BRANCH" "$(pwd -P)" >> "$MARK"; date > generated.txt; git add generated.txt; git commit -qm "chore: regenerate derived files"'

# 1 — unset
setup c1
run_implement_pipeline
check_eq 0 "$LAST_RC" "1 exit"
has 'status=COMPLETE' "$LAST_STDOUT" "1 COMPLETE"
hasnt 'post_implement' "$LAST_STDOUT$LAST_STDERR" "1 no hook output"
has 'move_issue.*state-build-review' "$(calls)" "1 hand-off"
teardown

# 2 — runs once in the worktree, commit counted and pushed
setup c2 "$HOOK_OK"
run_implement_pipeline
check_eq 0 "$LAST_RC" "2 exit"
check_eq 1 "$(wc -l < "$MARK" 2>/dev/null | tr -d ' ' || echo 0)" "2 hook ran exactly once"
check_eq "EXP-100 test-branch $(cd "$SANDBOX" && pwd -P)" "$(cat "$MARK" 2>/dev/null)" "2 env and working directory"
has 'repo.post_implement_command: ok, 1 commit' "$LAST_STDOUT" "2 hook commit counted"
has 'chore: regenerate derived files' "$(origin_subjects)" "2 hook commit on origin"
has 'move_issue.*state-build-review' "$(calls)" "2 hand-off"
hasnt 'needs-human' "$(calls)" "2 no needs-human"
teardown

# 3 — not a hand-off → not run
setup c3 "$HOOK_OK"
export FAKE_CLAUDE_FIXTURES="$FIXTURES_DIR/claude_needs_human.txt"
run_implement_pipeline
[ ! -e "$MARK" ] || fail "3 hook ran although the status was not COMPLETE"
has 'post_implement_command: skipped \(status NEEDS_HUMAN does not release the work for review\)' "$LAST_STDOUT" "3 skip line"
teardown

# 3d — PARTIAL with commits marks the PR ready (EXP-622): the hook runs before that
setup c3d "$HOOK_OK"
export FAKE_CLAUDE_FIXTURES="$FIXTURES_DIR/claude_partial_progress.txt" FAKE_CLAUDE_COMMIT_ON_ITERS="1:2:3"
run_implement_pipeline
has 'terminal status=PARTIAL' "$LAST_STDOUT" "3d PARTIAL"
check_eq 1 "$(wc -l < "$MARK" 2>/dev/null | tr -d ' ' || echo 0)" "3d hook ran once"
check_eq 1 "$(git -C "$SANDBOX/.fake-origin.git" ls-tree --name-only test-branch | grep -c '^generated.txt$' || true)" "3d generated.txt on origin"
has $'^gh\tpr\tcreate\t--title' "$(cat "$SANDBOX/gh_calls.log" 2>/dev/null)" "3d PR opened ready"
teardown

# 3e — PARTIAL with commits and a failing hook: halt, the PR stays a draft
setup c3e 'echo "regen broke"; exit 3'
export FAKE_CLAUDE_FIXTURES="$FIXTURES_DIR/claude_partial_progress.txt" FAKE_CLAUDE_COMMIT_ON_ITERS="1:2:3"
run_implement_pipeline
check_eq 14 "$LAST_RC" "3e exit"
has $'^gh\tpr\tcreate\t--draft' "$(cat "$SANDBOX/gh_calls.log" 2>/dev/null)" "3e PR opened as a draft"
hasnt $'^gh\tpr\t(ready|create\t--title)' "$(cat "$SANDBOX/gh_calls.log" 2>/dev/null)" "3e PR never marked ready"
teardown

# 3f — PARTIAL without commits keeps a draft PR, so the hook does not run
setup c3f "$HOOK_OK"
export FAKE_CLAUDE_FIXTURES="$FIXTURES_DIR/claude_partial_progress.txt" FAKE_CLAUDE_COMMIT_ON_ITERS="" BUREAU_USE_GOAL_LOOP=1
run_implement_pipeline
has 'terminal status=PARTIAL' "$LAST_STDOUT" "3f PARTIAL"
[ ! -e "$MARK" ] || fail "3f hook ran for a PARTIAL without commits"
teardown

# 3c — after a halt on the hook, the next run commits nothing but still runs the hook
setup c3c 'echo "regen broke"; exit 3'
run_implement_pipeline
check_eq 14 "$LAST_RC" "3c first run halts on the hook"
: > "$SANDBOX/calls.log"
jq -n --arg c "$HOOK_OK" '{repo: {post_implement_command: $c}}' > "$SANDBOX/.bureau.json"
export FAKE_CLAUDE_COMMIT_ON_ITERS=""
run_implement_pipeline
check_eq 0 "$LAST_RC" "3c second run exit"
check_eq 1 "$(wc -l < "$MARK" 2>/dev/null | tr -d ' ' || echo 0)" "3c hook ran in the second run"
check_eq 1 "$(git -C "$SANDBOX/.fake-origin.git" ls-tree --name-only test-branch | grep -c '^generated.txt$' || true)" "3c generated.txt on origin"
has 'move_issue.*state-build-review' "$(calls)" "3c hand-off after the hook"
teardown

# 4 — non-zero exit
setup c4 'echo "regen broke on purpose"; exit 3'
run_implement_pipeline
check_eq 14 "$LAST_RC" "4 exit"
hasnt 'move_issue' "$(calls)" "4 no state move"
has 'add_issue_label.*needs-human' "$(calls)" "4 needs-human"
has 'post_comment.*repo.post_implement_command failed' "$(calls)" "4 halt comment"
has 'repo.post_implement_command exited 3' "$(calls)" "4 report names the exit"
has 'regen broke on purpose' "$(calls)" "4 report carries the output"
has 'fake-claude iter 1 progress' "$(origin_subjects)" "4 run's commits still pushed"
teardown

# 5 — uncommitted changes
setup c5 'echo derived > left-behind.txt; echo more >> iter_1_progress.txt'
run_implement_pipeline
check_eq 14 "$LAST_RC" "5 exit"
has 'left changes it did not commit' "$(calls)" "5 reason"
has 'left-behind.txt' "$(calls)" "5 untracked file named"
has 'iter_1_progress.txt' "$(calls)" "5 modified file named"
[ -f "$SANDBOX/left-behind.txt" ] || fail "5 the hook's file was deleted"
hasnt 'move_issue' "$(calls)" "5 no state move"
teardown

# 6 — timeout kills the hook's process group
setup c6 'echo $$ > "$MARK.pid"; sleep 60 & echo $! >> "$MARK.pid"; wait'
export BUREAU_POST_IMPLEMENT_TIMEOUT=2
t0=$(date +%s)
run_implement_pipeline
t1=$(date +%s)
check_eq 14 "$LAST_RC" "6 exit"
has 'timed out after 2s' "$(calls)" "6 reason"
[ $((t1 - t0)) -lt 30 ] || fail "6 stage waited $((t1 - t0))s for the hook"
if [ -f "$MARK.pid" ]; then
  while read -r pid; do
    if kill -0 "$pid" 2>/dev/null; then fail "6 hook process $pid survived the timeout"; kill "$pid" 2>/dev/null || true; fi
  done < "$MARK.pid"
else
  fail "6 hook never started"
fi
rm -f "$MARK.pid"
teardown

# 6b — a child that ignores SIGTERM is killed with the group after the grace period
setup c6b '(trap "" TERM; exec sleep 60) & echo $! >> "$MARK.pid"; wait'
export BUREAU_POST_IMPLEMENT_TIMEOUT=2
run_implement_pipeline
check_eq 14 "$LAST_RC" "6b exit"
has 'timed out after 2s' "$(calls)" "6b reason"
if [ -s "$MARK.pid" ]; then
  while read -r pid; do
    if kill -0 "$pid" 2>/dev/null; then fail "6b a child ignoring SIGTERM ($pid) survived the timeout"; kill -9 "$pid" 2>/dev/null || true; fi
  done < "$MARK.pid"
else
  fail "6b hook never started its child"
fi
rm -f "$MARK.pid"
teardown

# 6c — the hook's own exit 124 is not a timeout
setup c6c 'exit 124'
run_implement_pipeline
check_eq 14 "$LAST_RC" "6c exit"
has 'repo.post_implement_command exited 124' "$(calls)" "6c reason names the exit"
hasnt 'timed out' "$(calls)" "6c not reported as a timeout"
teardown

# 6d — the limit is capped at the stage's total time
setup c6d 'sleep 30'
# goal path: the iteration loop needs more than 60 s of budget to start a pass
export BUREAU_USE_GOAL_LOOP=1 BUREAU_IMPL_TOTAL_TIMEOUT=4 BUREAU_POST_IMPLEMENT_TIMEOUT=60
run_implement_pipeline
unset BUREAU_IMPL_TOTAL_TIMEOUT
check_eq 14 "$LAST_RC" "6d exit"
has 'timed out after 4s' "$(calls)" "6d limit capped at BUREAU_IMPL_TOTAL_TIMEOUT"
teardown

# 6e — the limit can come from .env (allow-list in bureau-env.sh)
setup c6e 'sleep 30'
printf 'BUREAU_POST_IMPLEMENT_TIMEOUT=2\n' >> "$SANDBOX/.env"
run_implement_pipeline
check_eq 14 "$LAST_RC" "6e exit"
has 'timed out after 2s' "$(calls)" "6e limit read from .env"
teardown

# 7 — HEAD moved off the run's commits: no push of rewritten history
setup c7 'git reset -q --hard HEAD~1'
run_implement_pipeline
check_eq 14 "$LAST_RC" "7 exit"
has "moved HEAD off the commit it started from" "$(calls)" "7 reason"
has 'not pushing: repo.post_implement_command moved HEAD' "$LAST_STDERR" "7 final push skipped"
has 'fake-claude iter 1 progress' "$(origin_subjects)" "7 origin keeps the run's commit"
teardown

# 8 — dry run
setup c8 "$HOOK_OK"
export BUREAU_DRY_RUN=1
run_implement_pipeline
[ ! -e "$MARK" ] || fail "8 hook ran in a dry run"
has '\[DRY_RUN\] would run repo.post_implement_command' "$LAST_STDOUT" "8 dry-run line"
teardown

# 9 — a CI suppressor in the hook's commit is caught by the squash-range check
setup c9 'date > generated.txt; git add generated.txt; git commit -qm "chore: regenerate [skip ci]"'
run_implement_pipeline
hasnt 'move_issue' "$(calls)" "9 no hand-off"
has 'post_comment.*Halted before hand-off: a commit in the squash range' "$(calls)" "9 CI_MARKER halt"
teardown

# 10 — every push rejected: 18, nothing handed on
setup c10
reject_pushes all
run_implement_pipeline
check_eq 18 "$LAST_RC" "10 exit"
hasnt 'move_issue' "$(calls)" "10 no state move"
hasnt 'Implementation complete' "$(calls)" "10 no completion comment"
has 'post_comment.*final push of `test-branch` to origin failed twice' "$(calls)" "10 comment"
has 'PUSH FAILED \(end of run, retry\)' "$LAST_STDERR" "10 push retried once"
has 'PUSH FAILED \(end of run\)' "$LAST_STDERR" "10 loud push line"
teardown

# 11 — a failed per-iter push stays non-fatal; the end-of-run push retries it
setup c11
reject_pushes first
run_implement_pipeline
check_eq 0 "$LAST_RC" "11 exit"
has 'PUSH FAILED \(iter 1\)' "$LAST_STDERR" "11 first push failed"
has 'move_issue.*state-build-review' "$(calls)" "11 hand-off"
has 'fake-claude iter 1 progress' "$(origin_subjects)" "11 commit on origin"
teardown

# 11b — the end-of-run push fails at transport level, but origin already has every commit
setup c11b
REAL_GIT=$(command -v git)
mkdir -p "$SANDBOX/.shim"
printf '#!/bin/bash\na=("$@"); while :; do case "${a[0]:-}" in -c) a=("${a[@]:2}") ;; --config-env=*) a=("${a[@]:1}") ;; *) break ;; esac; done; s=${a[0]:-}\nif [ "$s" = push ]; then n=$(cat "%s/.pushes" 2>/dev/null || echo 0); n=$((n+1)); echo "$n" > "%s/.pushes"; if [ "$n" -ge 2 ]; then echo "fatal: unable to access origin: Could not resolve host" >&2; exit 128; fi; fi\nexec "%s" "$@"\n' "$SANDBOX" "$SANDBOX" "$REAL_GIT" > "$SANDBOX/.shim/git"
chmod +x "$SANDBOX/.shim/git"
PATH="$SANDBOX/.shim:$PATH" run_implement_pipeline
check_eq 0 "$LAST_RC" "11b exit"
check_eq 3 "$(cat "$SANDBOX/.pushes")" "11b iteration push, end-of-run push and one retry"
has 'origin/test-branch already has every commit of HEAD; going on' "$LAST_STDERR" "11b says why it goes on"
has 'move_issue.*state-build-review' "$(calls)" "11b hand-off"
hasnt 'final push' "$(calls)" "11b no push-failure comment"
teardown

# 11d — the pushes fail and origin/<branch>..HEAD cannot be read: counts as missing commits → 18
setup c11d
REAL_GIT=$(command -v git)
mkdir -p "$SANDBOX/.shim"
cat > "$SANDBOX/.shim/git" <<SHIM
#!/bin/bash
n=\$(cat "$SANDBOX/.pushes" 2>/dev/null || echo 0)
a=("\$@"); while :; do case "\${a[0]:-}" in -c) a=("\${a[@]:2}") ;; --config-env=*) a=("\${a[@]:1}") ;; *) break ;; esac; done; s=\${a[0]:-}
if [ "\$s" = push ]; then n=\$((n+1)); echo "\$n" > "$SANDBOX/.pushes"; if [ "\$n" -ge 2 ]; then echo "fatal: unable to access origin" >&2; exit 128; fi; fi
if [ "\$n" -ge 2 ] && [ "\${1:-}" = rev-list ] && [ "\${3:-}" = "origin/test-branch..HEAD" ]; then echo "fatal: bad revision" >&2; exit 128; fi
exec "$REAL_GIT" "\$@"
SHIM
chmod +x "$SANDBOX/.shim/git"
PATH="$SANDBOX/.shim:$PATH" run_implement_pipeline
check_eq 18 "$LAST_RC" "11d an unreadable comparison counts as commits origin lacks"
has 'post_comment.*origin could not be read to compare' "$(calls)" "11d comment wording"
has 'post_comment.*final push of `test-branch` to origin failed twice' "$(calls)" "11d comment"
hasnt 'move_issue' "$(calls)" "11d no hand-off"
teardown

# 11e — origin's branch was rewritten after the iteration push; the final pushes are rejected
# (fetch first). A rejected push does not update origin/<branch>, so the stage must fetch
# before comparing: HEAD's commit is missing on origin → 18, no hand-off.
setup c11e
REAL_GIT=$(command -v git)
mkdir -p "$SANDBOX/.shim"
cat > "$SANDBOX/.shim/git" <<SHIM
#!/bin/bash
a=("\$@"); while :; do case "\${a[0]:-}" in -c) a=("\${a[@]:2}") ;; --config-env=*) a=("\${a[@]:1}") ;; *) break ;; esac; done; s=\${a[0]:-}
if [ "\$s" = push ]; then
  n=\$(cat "$SANDBOX/.pushes" 2>/dev/null || echo 0); n=\$((n+1)); echo "\$n" > "$SANDBOX/.pushes"
  if [ "\$n" = 2 ]; then
    o="$SANDBOX/.fake-origin.git"
    x=\$(GIT_AUTHOR_NAME=other GIT_AUTHOR_EMAIL=other@test GIT_COMMITTER_NAME=other GIT_COMMITTER_EMAIL=other@test "$REAL_GIT" -C "\$o" commit-tree "\$("$REAL_GIT" -C "\$o" rev-parse 'main^{tree}')" -p main -m "someone else's rewrite")
    "$REAL_GIT" -C "\$o" update-ref refs/heads/test-branch "\$x"
  fi
fi
exec "$REAL_GIT" "\$@"
SHIM
chmod +x "$SANDBOX/.shim/git"
PATH="$SANDBOX/.shim:$PATH" run_implement_pipeline
# precondition: the shim really rewrote origin (it needs a git identity, which CI has only from the env above)
check_eq "someone else's rewrite" "$(git -C "$SANDBOX/.fake-origin.git" log -1 --format=%s test-branch)" "11e origin was rewritten"
check_eq 18 "$LAST_RC" "11e rejected pushes against a rewritten origin"
hasnt 'already has every commit' "$LAST_STDERR" "11e does not claim origin is complete"
hasnt 'move_issue' "$(calls)" "11e no hand-off"
has 'post_comment.*commit\(s\) missing on origin' "$(calls)" "11e comment names the missing commits"
teardown

# 11g — as 11e, in a clone whose fetch refspec covers only main (a --single-branch clone), with the
# local origin/<branch> still at HEAD: a plain `git fetch origin <branch>` writes only FETCH_HEAD, so
# the stale ref would claim origin has everything. The explicit refspec updates it → 18.
setup c11g
git -C "$SANDBOX" config remote.origin.fetch "+refs/heads/main:refs/remotes/origin/main"
REAL_GIT=$(command -v git)
mkdir -p "$SANDBOX/.shim"
cat > "$SANDBOX/.shim/git" <<SHIM
#!/bin/bash
a=("\$@"); while :; do case "\${a[0]:-}" in -c) a=("\${a[@]:2}") ;; --config-env=*) a=("\${a[@]:1}") ;; *) break ;; esac; done; s=\${a[0]:-}
if [ "\$s" = push ]; then
  n=\$(cat "$SANDBOX/.pushes" 2>/dev/null || echo 0); n=\$((n+1)); echo "\$n" > "$SANDBOX/.pushes"
  if [ "\$n" = 2 ]; then
    o="$SANDBOX/.fake-origin.git"
    x=\$(GIT_AUTHOR_NAME=other GIT_AUTHOR_EMAIL=other@test GIT_COMMITTER_NAME=other GIT_COMMITTER_EMAIL=other@test "$REAL_GIT" -C "\$o" commit-tree "\$("$REAL_GIT" -C "\$o" rev-parse 'main^{tree}')" -p main -m "someone else's rewrite")
    "$REAL_GIT" -C "\$o" update-ref refs/heads/test-branch "\$x"
    # the local tracking ref still says origin has HEAD (as a push with a full refspec left it)
    "$REAL_GIT" -C "$SANDBOX" update-ref refs/remotes/origin/test-branch HEAD
  fi
fi
exec "$REAL_GIT" "\$@"
SHIM
chmod +x "$SANDBOX/.shim/git"
PATH="$SANDBOX/.shim:$PATH" run_implement_pipeline
# precondition: the shim really rewrote origin (it needs a git identity, which CI has only from the env above)
check_eq "someone else's rewrite" "$(git -C "$SANDBOX/.fake-origin.git" log -1 --format=%s test-branch)" "11g origin was rewritten"
check_eq 18 "$LAST_RC" "11g rejected pushes against a rewritten origin"
hasnt 'already has every commit' "$LAST_STDERR" "11g does not claim origin is complete"
hasnt 'move_issue' "$(calls)" "11g no hand-off"
has 'post_comment.*commit\(s\) missing on origin' "$(calls)" "11g comment names the missing commits"
teardown

# 11f — pushes and the fetch fail at transport level: origin cannot be read → 18
setup c11f
REAL_GIT=$(command -v git)
mkdir -p "$SANDBOX/.shim"
cat > "$SANDBOX/.shim/git" <<SHIM
#!/bin/bash
n=\$(cat "$SANDBOX/.pushes" 2>/dev/null || echo 0)
a=("\$@"); while :; do case "\${a[0]:-}" in -c) a=("\${a[@]:2}") ;; --config-env=*) a=("\${a[@]:1}") ;; *) break ;; esac; done; s=\${a[0]:-}
if [ "\$s" = push ]; then n=\$((n+1)); echo "\$n" > "$SANDBOX/.pushes"; fi
if [ "\$n" -ge 2 ] && { [ "\$s" = push ] || [ "\$s" = fetch ]; }; then echo "fatal: unable to access origin: Could not resolve host" >&2; exit 128; fi
exec "$REAL_GIT" "\$@"
SHIM
chmod +x "$SANDBOX/.shim/git"
PATH="$SANDBOX/.shim:$PATH" run_implement_pipeline
check_eq 18 "$LAST_RC" "11f a failed fetch counts as unreadable"
has 'post_comment.*origin could not be read to compare' "$(calls)" "11f comment wording"
hasnt 'move_issue' "$(calls)" "11f no hand-off"
teardown

# 11c — hook failed and the push failed: 18, and the hook's report is not lost
setup c11c 'echo "regen broke on purpose"; exit 3'
reject_pushes all
run_implement_pipeline
check_eq 18 "$LAST_RC" "11c exit"
has 'post_comment.*final push of `test-branch` to origin failed twice' "$(calls)" "11c push comment"
has 'repo.post_implement_command exited 3' "$(calls)" "11c hook report in the comment"
has 'regen broke on purpose' "$(calls)" "11c hook output in the comment"
teardown

# 12 — the goal-loop path runs the hook too
setup c12 "$HOOK_OK"
export BUREAU_USE_GOAL_LOOP=1
run_implement_pipeline
check_eq 1 "$(wc -l < "$MARK" 2>/dev/null | tr -d ' ' || echo 0)" "12 hook ran on the goal path"
has 'chore: regenerate derived files' "$(origin_subjects)" "12 hook commit on origin"
teardown

# 14 — the hook gets no stdin, even when the stage has one
setup c14 'if read -r line; then echo "stdin:$line" >> "$MARK"; fi; echo done >> "$MARK"; date > generated.txt; git add generated.txt; git commit -qm "chore: regenerate derived files"'
( cd "$SANDBOX" && printf 'from-the-stage-stdin\n' | bash "$SCRIPTS_DIR/implement-pipeline.sh" >/dev/null 2>&1 ) || true
check_eq done "$(cat "$MARK" 2>/dev/null)" "14 hook read nothing from stdin"
teardown

# 15 — hook fails and the needs-human label cannot be written: the hold's 25 wins over 14
setup c15 'exit 3'
export BUREAU_STUB_ADD_LABEL_RC=1
run_implement_pipeline
unset BUREAU_STUB_ADD_LABEL_RC
check_eq 25 "$LAST_RC" "15 exit when the escalation could not be labelled"
has 'repo.post_implement_command exited 3' "$(calls)" "15 report still posted"
teardown

# 13 — negative control: the pre-change stage
setup c13a "$HOOK_OK"
use_old_stage
run_implement_pipeline
[ ! -e "$MARK" ] || fail "13 old stage ran the hook (control is not the old stage)"
teardown
setup c13b
reject_pushes all
use_old_stage
run_implement_pipeline
check_eq 0 "$LAST_RC" "13 old stage ends 0 on a failed final push"
has 'move_issue.*state-build-review' "$(calls)" "13 old stage hands on work that is not on origin"
teardown

rm -rf "$MARK_DIR"
trap - EXIT  # every case tore its own sandbox down; the harness trap would fail on the missing one under set -e
if [ "$FAILS" -gt 0 ]; then
  echo "test_post_implement_command: $FAILS failure(s)" >&2
  exit 1
fi
echo "OK test_post_implement_command"
