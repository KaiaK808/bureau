#!/bin/bash
# A failed push in the implement stage is loud, names branch and exit code, and does not end
# the run; a detached HEAD still pushes to the branch.
#
# 1. The REAL implement stage (harness) against an origin whose pre-receive hook rejects every
#    push: each push reports itself on stderr with git's own words, and the run still ends.
# 2. push_branch_loud cut from the real script, in a repo with a detached HEAD: it pushes to
#    origin/<branch>. Negative control: the old `git push -u origin HEAD` fails there.
set -euo pipefail
source "$(dirname "$0")/lib/harness.sh"
fail() { echo "FAIL $*" >&2; exit 1; }

sandbox_init "EXP-120" "test-branch"
export FAKE_CLAUDE_FIXTURES="$FIXTURES_DIR/claude_complete.txt" FAKE_CLAUDE_COMMIT_ON_ITERS=1
export BUREAU_DRY_RUN=0 BUREAU_IMPL_MAX_ITER=3
printf '#!/bin/sh\necho "rejected by the test hook"\nexit 1\n' > "$SANDBOX/.fake-origin.git/hooks/pre-receive"
chmod +x "$SANDBOX/.fake-origin.git/hooks/pre-receive"
run_implement_pipeline
assert_eq 0 "$LAST_RC" "a failed push does not end the run"
assert_match 'terminal status=COMPLETE' "$LAST_STDOUT" "the run reached its end"
assert_match "PUSH FAILED \\(iter 1\\): branch 'test-branch' is NOT on origin — git exit [1-9]" "$LAST_STDERR" "iteration push reported"
assert_match "PUSH FAILED \\(end of run\\)" "$LAST_STDERR" "end-of-run push retried and reported"
assert_match 'git: .*rejected by the test hook' "$LAST_STDERR" "git's own output is shown"
echo "PASS a rejected push is reported per attempt with branch, exit code and git's words, and the run goes on"
teardown

# --- detached HEAD --------------------------------------------------------------------------
FN=$(sed -n '/^push_branch_loud() {/,/^}/p' "$REPO_ROOT/templates/scripts/implement-pipeline.sh")
[ -n "$FN" ] || fail "push_branch_loud not found in implement-pipeline.sh"
D=$(mktemp -d -t bureau-test.push.XXXXXXXX)
trap 'rm -rf "$D"' EXIT
git init -q --bare "$D/origin.git"
git init -q -b main "$D/repo"
git -C "$D/repo" -c user.email=t@t -c user.name=t commit -q --allow-empty -m init
git -C "$D/repo" remote add origin "$D/origin.git"
git -C "$D/repo" checkout -q --detach
git -C "$D/repo" -c user.email=t@t -c user.name=t commit -q --allow-empty -m "work on a detached HEAD"
(cd "$D/repo" && BRANCH=feat/x /bin/bash -c "set -euo pipefail; $FN
push_branch_loud 'detached'") >/dev/null 2>&1
git -C "$D/origin.git" rev-parse --verify --quiet refs/heads/feat/x >/dev/null \
  || fail "push_branch_loud did not push a detached HEAD to its branch"
git -C "$D/repo" -c user.email=t@t -c user.name=t commit -q --allow-empty -m "more work"
for old in 'HEAD' 'HEAD:feat/new'; do   # the template's old form, and slidefactory's
  if (cd "$D/repo" && git push -u origin "$old" >/dev/null 2>&1); then
    fail "negative control: 'git push -u origin $old' now succeeds from a detached HEAD, so this proves nothing"
  fi
done
echo "PASS a detached HEAD is pushed to refs/heads/\$BRANCH; plain HEAD and a short HEAD:<new-branch> both fail there"
