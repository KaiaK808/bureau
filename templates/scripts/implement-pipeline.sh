#!/bin/bash
# Implement pipeline: Pick Build issue → checkout branch → execute tasks via a
# bounded retry loop → push → PR. Each tick attempts up to MAX_ITER passes
# through Claude, parsing the JSON status block emitted at the end of each
# iteration to decide whether to continue. Terminal states:
#   COMPLETE                  → push + mark PR ready + move to QA / Build Review
#   NEEDS_HUMAN/STUCK/        → push + draft PR + needs-human label + summary
#   CAP_TIME/PARTIAL            comment + no state move (issue stays in Build)
set -euo pipefail

unset CLAUDECODE 2>/dev/null || true

REPO_DIR="$(pwd)"
SCRIPT_REPO="$(cd "$(dirname "$0")/.." && pwd)"
source "$(dirname "$0")/bureau-config.sh"

BUREAU_ENV_FILE="${BUREAU_ENV_FILE:-$SCRIPT_REPO/.env}"
# BUREAU_ENV_FILE only, never ./.env: in a stage worktree that is a file the branch controls.
if [ -f "$BUREAU_ENV_FILE" ]; then bureau_load_env --export "$BUREAU_ENV_FILE"
else [ -n "${LINEAR_API_KEY:-}" ] || { echo "ERROR: Set LINEAR_API_KEY"; exit 1; }; fi

CLAUDE=(run_stage_for implement)
API_KEY="${LINEAR_API_KEY:?Set LINEAR_API_KEY in .env}"

# Retry-loop bounds. MAX_ITER caps the number of Claude passes per tick.
# ITER_TIMEOUT caps wall-time per pass; TOTAL_TIMEOUT caps cumulative wall-time
# so a single tick can't burn unbounded compute even if every iteration is
# productive. Defaults give ≤90 min worst case before the issue is parked for
# human review.
MAX_ITER="${BUREAU_IMPL_MAX_ITER:-3}"
ITER_TIMEOUT="${BUREAU_IMPL_ITER_TIMEOUT:-1800}"
TOTAL_TIMEOUT="${BUREAU_IMPL_TOTAL_TIMEOUT:-5400}"

# The provider adapter enforces per-pass timeouts on both macOS and Linux.

# refresh_review_context: pull the latest "Code Review … Changes Requested"
# comment for $1 and emit the prompt block the implement loop interpolates.
# Returns empty if there's nothing relevant. Called once per iteration so a
# human comment posted mid-run is seen by the next pass.
refresh_review_context() {
  local issue="$1"
  local blob feedback
  # No fallback to '{}': a failed read would drop the reviewer's requested
  # fixes from the prompt without a word. The caller's `$(…)` ends the stage.
  blob=$(get_issue_branch_and_comments "$issue") || return $?
  feedback=$(printf '%s' "$blob" \
    | jq -r '[.comments[] | select(.body | test("Code Review.*Changes Requested|FIXES_NEEDED|(?m)^VERDICT: REQUEST_CHANGES[[:space:]]*$"))][0].body // empty' 2>/dev/null || echo "")
  if [ -n "$feedback" ] && [ "${#feedback}" -gt 20 ]; then
    printf '\n--- Code Review Feedback (PRIORITY) ---\n%s\nAddress ALL fixes before remaining tasks.\n--- End feedback ---\n' "$feedback"
  fi
}

# open_or_update_pr_draft: ensure a draft PR exists for $BRANCH; emit its URL.
# Used during intermediate iterations and on non-COMPLETE terminal states so
# reviewers can see in-flight work without QA/code-review picking it up.
# push_branch_loud <label> [status] [rev]: push $BRANCH to origin; on failure
# say so loudly, and carry on — or, with `status`, return git's exit code so
# the caller can decide. Only the end-of-run push uses it: after it the ticket
# is handed on, and a hand-off of work that is not on origin must not happen.
# rev (default HEAD) is what gets pushed; push_deferred passes the commit a
# hook started from when the hook moved HEAD.
#
# Carried over from installation A (EXP-1462). Every push here used to end
# in `|| true`, so a failed push left no trace: whether the branch was out
# could only be learned by diffing origin against the worktree. Now a failure
# names branch, exit code and git's own output on stderr, distinct from
# progress noise. It stays non-fatal on purpose — a flaky network must not end
# a ticket mid-flight; the end-of-run push retries.
#
# The trap: this script runs under `set -euo pipefail`, and
#     push_out=$(git push …); rc=$?          # WRONG
# aborts the run the moment the push fails. Only a command in an `if`
# condition is exempt from `set -e`, and only in the else-branch of the
# un-negated form is `$?` git's own code (`if ! …` has already turned it to 0).
#
# The target is HEAD:refs/heads/$BRANCH. Plain HEAD has no target when HEAD is
# detached (installation A's EXP-1420 log shows two such pushes swallowed while
# the run walked on to QA). Installation A's HEAD:"$BRANCH" fixes that only while
# the branch already exists on origin: for a new one git cannot tell that the
# name is meant as a branch and refuses ("not a full refname").
push_branch_loud() {
  local label="$1" mode="${2:-}" rev="${3:-HEAD}" push_out rc
  if [ "${BUREAU_DRY_RUN:-0}" = "1" ]; then
    echo "  [DRY_RUN] would: git push -u origin $rev:refs/heads/$BRANCH ($label)"
    return 0
  fi
  if push_out=$(git push -u origin "$rev":refs/heads/"$BRANCH" 2>&1); then
    :
  else
    rc=$?
    if [ "$mode" = status ]; then
      # The caller compares HEAD with origin/$BRANCH and says what is missing.
      echo "  ✗✗ PUSH FAILED ($label): branch '$BRANCH' — git exit $rc" >&2
      printf '%s\n' "$push_out" | sed 's/^/       git: /' >&2
      return "$rc"
    fi
    echo "  ✗✗ PUSH FAILED ($label): branch '$BRANCH' is NOT on origin — git exit $rc" >&2
    printf '%s\n' "$push_out" | sed 's/^/       git: /' >&2
    echo "  ✗✗ the work is only in this worktree until a later push succeeds" >&2
  fi
  return 0
}

# run_post_implement_command: the optional repo.post_implement_command hook
# (.bureau.json), for a repo that has to derive files from an implementation
# run — regenerate generated docs or contracts — before anyone sees the branch.
#
# Runs wherever the stage releases the work for review: when the terminal
# status is COMPLETE (hand-off to QA or Build Review), and when it is PARTIAL
# with commits (the PR is marked ready so CI runs, EXP-622). COMPLETE already
# implies commits beyond origin/main — both paths turn a COMPLETE on an empty
# branch into STUCK (EXP-573) before this point. A failing hook turns either
# into the POST_IMPLEMENT_FAILED halt, whose PR stays a draft.
# It does not depend on whether THIS run committed: after a
# halt on the hook, a human fixes it and removes needs-human, and the next run
# typically finds the tasks done and commits nothing — the hook must still run
# before that run hands the ticket on. So the command must be idempotent and
# exit 0 when there is nothing to commit (a bare `git commit` exits 1 then).
# Never runs in a dry run. It runs after the loop and before the squash-range
# check and the final push, in the implement worktree via `bash -o pipefail -c`
# (the review build check's convention), with no stdin, with BUREAU_ISSUE and
# BUREAU_BRANCH set, without the Bureau secrets (bureau_untrusted_env,
# repo.untrusted_env), and bounded by BUREAU_POST_IMPLEMENT_TIMEOUT (default
# 900 s, never above the stage's TOTAL_TIMEOUT). On a timeout the runner sends
# SIGTERM to the hook's whole process group and, after a 5 s grace, SIGKILL to
# whatever is left of it. The hook commits its own output: its commits go
# through the squash-range check (a CI suppressor in its message halts like any
# other) and are pushed with the rest (AHEAD_OF_ORIGIN is read after the hook).
#
# It fails — POST_IMPLEMENT_FAILED=1, and the stage halts with 14 after the
# usual halt bookkeeping — when it exits non-zero or times out, when it leaves
# changes it did not commit (new entries in `git status` compared with before
# it ran, or new content in a path that was already uncommitted before it ran
# and still is; nothing is deleted, so the worker keeps the worktree for a
# human to look at), or when HEAD is no longer a descendant of the commit it
# started from (then the final push is skipped: pushing would publish
# rewritten history).
POST_IMPLEMENT_FAILED=0
POST_IMPLEMENT_HEAD_REWRITTEN=0
POST_IMPLEMENT_REPORT=""
_POST_IMPLEMENT_RUNNER='
import os, signal, subprocess, sys, time
limit, cmd, status_file = int(sys.argv[1]), sys.argv[2], sys.argv[3]
child = subprocess.Popen(["bash", "--noprofile", "--norc", "-o", "pipefail", "-c", cmd], stdin=subprocess.DEVNULL, start_new_session=True)
def stop(code, why):
    with open(status_file, "w") as f:
        f.write(why)
    pgid = child.pid
    try:
        os.killpg(pgid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        child.poll()
        try:
            os.killpg(pgid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.1)
    try:
        os.killpg(pgid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    child.wait()
    sys.exit(code)
for sig, code in ((signal.SIGINT, 130), (signal.SIGTERM, 143), (signal.SIGHUP, 129)):
    signal.signal(sig, lambda *_, c=code: stop(c, "signal"))
try:
    rc = child.wait(timeout=limit)
except subprocess.TimeoutExpired:
    stop(124, "timeout")
sys.exit(128 - rc if rc < 0 else rc)
'
# A path that is already uncommitted when the hook starts (` M a.txt`, an
# untracked file) keeps the same `git status` line however much the hook adds
# to it, so the line comparison alone cannot see the hook change it. The
# helpers below record the content of every such path before the hook and
# name the ones that are still uncommitted afterwards with other content.
# Paths come from `git status -z` (any byte in a name is safe), with renames
# split into a deletion and an addition.
#
# _post_implement_hash_paths <path>...: one line per path, in order: the
# content hash of a regular file, "link:" and the hash of the target text for
# a symlink (what git stores for it; the file it points to may change and be
# committed while the link stays as it was), "absent" for anything else
# (deleted, a directory). All regular files go through one `git hash-object`
# call (xargs splits a long list); one call per path only when that fails (a
# file vanished).
_post_implement_hash_paths() {
  local p out h i=0 n=0
  local -a hashes
  for p in "$@"; do
    if [ -f "$p" ] && [ ! -L "$p" ]; then n=$((n + 1)); fi
  done
  out=""
  if [ "$n" -gt 0 ]; then
    out=$(for p in "$@"; do if [ -f "$p" ] && [ ! -L "$p" ]; then printf '%s\0' "$p"; fi; done \
      | bureau_without_secrets xargs -0 git hash-object --no-filters -- 2>/dev/null) || out=""
  fi
  hashes=()
  if [ -n "$out" ]; then
    while IFS= read -r h; do hashes+=("$h"); done <<< "$out"
  fi
  for p in "$@"; do
    if [ -L "$p" ]; then
      echo "link:$(readlink "./$p" | git hash-object --stdin 2>/dev/null)"
    elif [ ! -f "$p" ]; then
      echo absent
    elif [ "${#hashes[@]}" -eq "$n" ]; then
      echo "${hashes[$i]}"
      i=$((i + 1))
    else
      git hash-object --no-filters -- "$p" 2>/dev/null || echo absent
    fi
  done
}
# _post_implement_dirty_snapshot <file>: write "<hash><TAB><path>\0" for
# every path `git status` lists now.
_post_implement_dirty_snapshot() {
  local entry h i=0
  local -a paths
  paths=()
  while IFS= read -r -d '' entry; do
    paths+=("${entry:3}")
  done < <(git status --porcelain -z --no-renames --untracked-files=all)
  : > "$1"
  [ "${#paths[@]}" -gt 0 ] || return 0
  while IFS= read -r h; do
    printf '%s\t%s\0' "$h" "${paths[$i]}" >> "$1"
    i=$((i + 1))
  done < <(_post_implement_hash_paths "${paths[@]}")
}
# _post_implement_changed_dirty <file>: print, one per line, each path of the
# snapshot whose content changed and that `git status` still lists, unless it
# changes again within _POST_IMPLEMENT_SETTLE seconds after the command has
# ended. Such a path is being written by something that is still running,
# typically a process the agent left behind (the provider stops its process
# group only on a timeout or an interrupt, not after a normal turn); it is
# named on stderr and not counted. A writer slower than that can still be
# counted against the command. The wait happens only when there is a path to
# blame.
_POST_IMPLEMENT_SETTLE=2
_post_implement_changed_dirty() {
  local record now again i=0 j=0
  local -a paths hashes blamed blamed_now
  paths=()
  hashes=()
  blamed=()
  blamed_now=()
  while IFS= read -r -d '' record; do
    hashes+=("${record%%$'\t'*}")
    paths+=("${record#*$'\t'}")
  done < "$1"
  [ "${#paths[@]}" -gt 0 ] || return 0
  while IFS= read -r now; do
    if [ "$now" != "${hashes[$i]}" ] \
      && [ -n "$(git --literal-pathspecs status --porcelain --untracked-files=all -- "${paths[$i]}")" ]; then
      blamed+=("${paths[$i]}")
      blamed_now+=("$now")
    fi
    i=$((i + 1))
  done < <(_post_implement_hash_paths "${paths[@]}")
  [ "${#blamed[@]}" -gt 0 ] || return 0
  sleep "$_POST_IMPLEMENT_SETTLE"
  while IFS= read -r again; do
    if [ "$again" = "${blamed_now[$j]}" ]; then
      printf '%s\n' "${blamed[$j]}"
    else
      echo "  repo.post_implement_command: not counted, still changing after the command ended: ${blamed[$j]}" >&2
    fi
    j=$((j + 1))
  done < <(_post_implement_hash_paths "${blamed[@]}")
}
run_post_implement_command() {
  local cmd limit before before_status after_status new_dirty changed_dirty dirty_snapshot rc log status_file why reason hook_commits
  cmd=$(bureau_get '.repo.post_implement_command // empty')
  [ -n "$cmd" ] || return 0
  if [ "$STATUS" != COMPLETE ] && ! { [ "$STATUS" = PARTIAL ] && [ "$COMMITS_TOTAL" -gt 0 ]; }; then
    echo "  repo.post_implement_command: skipped (status $STATUS does not release the work for review)"
    return 0
  fi
  if [ "${BUREAU_DRY_RUN:-0}" = "1" ]; then
    echo "  [DRY_RUN] would run repo.post_implement_command: $cmd"
    return 0
  fi
  # Branch code: it runs without the Bureau secrets (bureau_untrusted_env,
  # bureau-env.sh). When that environment cannot be built the hook is not run
  # and counts as failed — the halt below still pushes this run's commits,
  # where an exit 24 here would leave them unpushed.
  if ! bureau_untrusted_env --check; then
    POST_IMPLEMENT_FAILED=1
    POST_IMPLEMENT_REPORT="repo.post_implement_command was not run: repo.untrusted_env is not absent, \"default\" or \"clean\", so the environment without the Bureau secrets could not be built.
command: ${cmd}"
    echo "  ✗✗ repo.post_implement_command not run: repo.untrusted_env is invalid" >&2
    return 0
  fi
  limit="${BUREAU_POST_IMPLEMENT_TIMEOUT:-900}"
  [[ "$limit" =~ ^[1-9][0-9]*$ ]] || limit=900
  [ "$limit" -le "$TOTAL_TIMEOUT" ] || limit="$TOTAL_TIMEOUT"
  before=$(git rev-parse HEAD)
  before_status=$(git status --porcelain --untracked-files=all | LC_ALL=C sort)
  dirty_snapshot=$(mktemp "${TMPDIR:-/tmp}/bureau-post-implement-dirty.XXXXXX")
  _post_implement_dirty_snapshot "$dirty_snapshot"
  log=$(mktemp "${TMPDIR:-/tmp}/bureau-post-implement.XXXXXX")
  status_file="$log.why"
  echo "  Running repo.post_implement_command (limit ${limit}s): $cmd"
  if bureau_untrusted_env BUREAU_ISSUE="$ISSUE" BUREAU_BRANCH="$BRANCH" python3 -I -c "$_POST_IMPLEMENT_RUNNER" "$limit" "$cmd" "$status_file" >"$log" 2>&1; then
    rc=0
  else
    rc=$?
  fi
  why=$(cat "$status_file" 2>/dev/null || true)
  rm -f "$status_file"
  tail -20 "$log" | sed 's/^/    /'
  reason=""
  if [ "$why" = timeout ]; then
    reason="timed out after ${limit}s"
  elif [ "$why" = signal ]; then
    reason="was interrupted by a signal (exit $rc)"
  elif [ "$rc" != 0 ]; then
    reason="exited $rc"
  fi
  if git merge-base --is-ancestor "$before" HEAD 2>/dev/null; then
    hook_commits=$(git rev-list --count "$before..HEAD")
  else
    hook_commits=0
    POST_IMPLEMENT_HEAD_REWRITTEN=1
    reason="${reason:+$reason; }moved HEAD off the commit it started from (${before:0:12})"
  fi
  after_status=$(git status --porcelain --untracked-files=all | LC_ALL=C sort)
  new_dirty=$(LC_ALL=C comm -13 <(printf '%s\n' "$before_status") <(printf '%s\n' "$after_status") | sed '/^$/d')
  changed_dirty=$(_post_implement_changed_dirty "$dirty_snapshot")
  rm -f "$dirty_snapshot"
  if [ -n "$new_dirty" ] || [ -n "$changed_dirty" ]; then
    reason="${reason:+$reason; }left changes it did not commit"
  fi
  if [ -z "$reason" ]; then
    echo "  repo.post_implement_command: ok, ${hook_commits} commit(s)"
    rm -f "$log"
    return 0
  fi
  POST_IMPLEMENT_FAILED=1
  POST_IMPLEMENT_REPORT="repo.post_implement_command ${reason}.
command: ${cmd}
last lines of its output:
$(tail -20 "$log")"
  if [ -n "$new_dirty" ]; then
    POST_IMPLEMENT_REPORT="${POST_IMPLEMENT_REPORT}
uncommitted changes it left (kept in the worktree):
${new_dirty}"
  fi
  if [ -n "$changed_dirty" ]; then
    POST_IMPLEMENT_REPORT="${POST_IMPLEMENT_REPORT}
files that were uncommitted before it ran and that it changed further (kept in the worktree):
${changed_dirty}"
  fi
  rm -f "$log"
  echo "  ✗✗ repo.post_implement_command ${reason}" >&2
  return 0
}

open_or_update_pr_draft() {
  local issue="$1" title="$2"
  if [ "${BUREAU_DRY_RUN:-0}" = "1" ]; then
    echo "<dry-run: would create/update draft PR>"
    return 0
  fi
  local existing
  existing=$(gh pr list --head "$BRANCH" --json number --jq '.[0].number' 2>/dev/null || echo "")
  if [ -n "$existing" ]; then
    gh pr view "$existing" --json url --jq '.url'
  else
    gh pr create --draft \
      --title "$issue: $title" \
      --body "## Summary
Implementation of $issue: $title (in progress).

Draft PR — see Linear issue for status.

Generated with [Claude Code](https://claude.com/claude-code)"
  fi
}

# open_or_update_pr_ready: ensure a ready-for-review PR exists for $BRANCH;
# if it was previously draft, mark it ready. Used on COMPLETE.
open_or_update_pr_ready() {
  local issue="$1" title="$2"
  if [ "${BUREAU_DRY_RUN:-0}" = "1" ]; then
    echo "<dry-run: would create or mark-ready PR>"
    return 0
  fi
  local existing
  existing=$(gh pr list --head "$BRANCH" --json number --jq '.[0].number' 2>/dev/null || echo "")
  if [ -n "$existing" ]; then
    # `gh pr ready` is a no-op if the PR is already ready; both are exit 0.
    # A real non-zero exit (transient API error, missing permission, deleted
    # PR) must surface so a human can finish the flip — FR-007.
    if ! gh pr ready "$existing" >/dev/null 2>&1; then
      echo "  WARN: failed to mark PR #$existing as ready-for-review; please flip manually" >&2
    fi
    gh pr view "$existing" --json url --jq '.url'
  else
    gh pr create \
      --title "$issue: $title" \
      --body "## Summary
Implementation of $issue: $title

Implemented from tasks.md spec.
See Linear issue for full context.

Generated with [Claude Code](https://claude.com/claude-code)"
  fi
}

# build_summary_comment: format the consolidated Linear comment posted at the
# end of the pipeline, regardless of terminal status. Takes status, total
# tasks done across iterations, per-iteration log, and PR URL.
build_summary_comment() {
  local status="$1" total_tasks="$2" iter_log="$3" pr_url="$4"
  local header
  case "$status" in
    COMPLETE)    header="🛠️ Implementation complete." ;;
    NEEDS_HUMAN) header="🚧 Implementation halted: NEEDS_HUMAN — Claude flagged tasks requiring human judgment." ;;
    STUCK)       header="🚧 Implementation stuck — no progress in last iteration (no commits, no [X] marks, no review fixes)." ;;
    CAP_TIME)    header="🚧 Implementation hit total time cap (${TOTAL_TIMEOUT}s) before completing." ;;
    PARTIAL)     header="🚧 Implementation made partial progress but exhausted iteration cap (${MAX_ITER}) without COMPLETE." ;;
    CI_MARKER)   header="🚧 Halted before hand-off: a commit in the squash range carries an entry of scripts/ci-skip-markers.txt, or the range could not be checked. Nothing went to QA or Build Review. Reword the message(s) named below, then remove needs-human." ;;
    POST_IMPLEMENT_FAILED) header="🚧 Halted before the work was released for review: repo.post_implement_command failed. Nothing went to QA or Build Review, and the PR stays a draft. Fix the command or the files it derives from, then remove needs-human: the next run executes the command again before it releases the work, even when it has nothing else to commit, so the command must be idempotent and exit 0 when there is nothing to commit." ;;
    *)           header="🚧 Implementation ended with status=$status." ;;
  esac
  printf '%s\n\n**Total tasks done across iterations:** %s\n**Branch:** `%s`\n**PR:** %s\n\nIteration log:\n```\n%s```\n' \
    "$header" "$total_tasks" "$BRANCH" "$pr_url" "$iter_log"
  if [ "$status" = "CI_MARKER" ]; then
    printf '\nSquash-range check:\n```\n%s\n```\n' "$SQUASH_REPORT"
  fi
  if [ "$POST_IMPLEMENT_FAILED" = 1 ]; then
    printf '\nPost-implement command:\n```\n%s\n```\n' "$POST_IMPLEMENT_REPORT"
  fi
}

precondition_linear
precondition_runner implement

if [ -n "${1:-}" ]; then
  ISSUE="$1"
  echo "Using specified issue: $ISSUE"
else
  echo "Picking next Build issue..."
  ISSUE=$(pipeline_pick_next "$(basename "$0")")

  if [ -z "$ISSUE" ] || [[ ! "$ISSUE" =~ ^[A-Z]+-[0-9]+$ ]]; then
    echo "No qualifying issues found. Queue empty."
    exit 2
  fi
  echo "Picked: $ISSUE"
fi

# State guard runs unconditionally — queue-loop preselects an issue and then
# spawns the pipeline seconds later; in that window the state can change
# (parallel rebase agent, human intervention). Confirm the issue is still in
# Build before doing any work.
bureau_stage_enter "$ISSUE" "$@"

ACTUAL_STATE=$(get_issue_state "$ISSUE")
if [ "$ACTUAL_STATE" != "Build" ]; then
  echo "  WARNING: $ISSUE is in '$ACTUAL_STATE', not 'Build'. Skipping."
  exit 2
fi

echo ""
echo "═══════════════════════════════════════"
echo "  Implement Pipeline: $ISSUE"
echo "═══════════════════════════════════════"
echo ""

echo "→ Fetching issue details..."
ISSUE_DETAIL=$(get_issue_detail "$ISSUE")
ISSUE_TITLE=$(echo "$ISSUE_DETAIL" | jq -r '.title // empty')
ISSUE_DESC=$(echo "$ISSUE_DETAIL" | jq -r '.description // empty')
PROJECT_NAME=$(echo "$ISSUE_DETAIL" | jq -r '.project.name // empty')
PROJECT_DESC=$(echo "$ISSUE_DETAIL" | jq -r '.project.description // empty')
echo "  $ISSUE: $ISSUE_TITLE"

echo "→ Finding branch and prior review feedback..."
# Single GraphQL roundtrip for both branch resolution and comment scan; the
# comment array is reused below to extract any prior 'Changes Requested' block.
ISSUE_BLOB=$(get_issue_branch_and_comments "$ISSUE")
BRANCH=$(printf '%s' "$ISSUE_BLOB" | jq -r '.branch // empty')

# EXP-413: fail loud — no silent fresh-from-main fallback. Spec pipeline must
# have produced a branch with artifacts before implement can run. The legacy
# behaviour silently created a new branch from main on lookup failure and
# burned Claude tokens on implementations with zero spec context.
if [ -z "$BRANCH" ] || [[ "$BRANCH" == *" "* ]] || [[ ${#BRANCH} -gt 200 ]]; then
  echo "  ERROR: no bureau-branch marker found for $ISSUE."
  post_comment "$ISSUE" "❌ Implement pipeline cannot start: no bureau-branch marker on this issue. The spec pipeline must produce a branch before implement can run. Routing back to Triage."
  move_issue "$ISSUE" "$BUREAU_STATE_TRIAGE"
  exit 12
fi

echo "  Found branch: $BRANCH"
git fetch origin

if ! git rev-parse --verify "$BRANCH" >/dev/null 2>&1 \
  && ! git rev-parse --verify "origin/$BRANCH" >/dev/null 2>&1; then
  echo "  ERROR: branch '$BRANCH' not found locally or on origin."
  post_comment "$ISSUE" "❌ Implement pipeline cannot start: bureau-branch marker points at \`$BRANCH\` but the branch does not exist. Routing back to Triage — spec pipeline must produce the branch."
  move_issue "$ISSUE" "$BUREAU_STATE_TRIAGE"
  exit 12
fi

# Release the branch from any other worktree before attaching here.
free_branch_from_other_worktrees "$BRANCH" "$(pwd)"
if git rev-parse --verify "origin/$BRANCH" >/dev/null 2>&1; then
  git checkout -B "$BRANCH" "origin/$BRANCH"
else
  git checkout "$BRANCH"
fi

# Implement runs against possibly-stale code if the spec branch was cut before
# recent merges. Conflict here means the spec branch and main have diverged in
# overlapping files — Claude shouldn't try to resolve that. Issue is already in
# Build, so label needs-human and exit; the picker excludes needs-human, so the
# issue stays out of the queue until a human rebases.
if ! merge_origin_main_or_abort "$ISSUE" "Implement"; then
  # If labelling fails, mark_needs_human holds the ticket locally so the
  # picker skips it and retries the label; Linear unusable ends with 27.
  # Otherwise exit 17 either way so the alert classifies as rebase-needed.
  mark_needs_human "$ISSUE" implement 17 || true
  exit 17
fi

# After the checkout AND the merge of origin/main — both can change the
# manifests. Idempotent: does nothing when node_modules already matches. A
# failure here is infrastructure (registry, disk), not the PR: stop with 24
# (environment-blocked) instead of building red and judging someone's code.
restore_worktree_deps "$(pwd)" || exit 24

echo ""
echo "Phase 1/2: execute tasks (bounded retry loop, MAX_ITER=$MAX_ITER)"

# The ticket's spec directory, matched on the branch the same way every stage
# does it (bureau_spec_dir_for_branch: exact name, else the one slug that fits).
# No match, or more than one candidate, leaves TASKS_FILE empty and routes back
# to Spec below. There is deliberately no "take the only or the
# first tasks.md" fallback: it fed another ticket's tasks.md to this issue.
SPEC_DIR_MATCH=$(bureau_spec_dir_for_branch "$BRANCH")
TASKS_FILE=""
if [ -n "$SPEC_DIR_MATCH" ] && [ -f "${SPEC_DIR_MATCH}tasks.md" ]; then
  TASKS_FILE="${SPEC_DIR_MATCH}tasks.md"
fi

PROJECT_CONTEXT=""
[ -n "$PROJECT_DESC" ] && PROJECT_CONTEXT="
--- Project context: $PROJECT_NAME ---
$PROJECT_DESC
--- End project context ---
"

DESIGN_CONTEXT=""
if [ -n "$TASKS_FILE" ]; then
  DESIGN_FILE="$(dirname "$TASKS_FILE")/design.md"
  if [ -f "$DESIGN_FILE" ]; then
    echo "  Found design: $DESIGN_FILE"
    DESIGN_CONTEXT="
--- UX/UI Design Artifacts ---
Read $DESIGN_FILE before implementing UI tasks.
--- End design context ---
"
  fi
fi

# build_spec_context gets the directory matched above only when it holds the
# tasks.md; without one the stage stops below anyway.
[ -n "$TASKS_FILE" ] || SPEC_DIR_MATCH=""
SPEC_CONTEXT=$(build_spec_context "$SPEC_DIR_MATCH")

# tasks.md is guaranteed by the time Build state is reached: spec-pipeline
# produced it via /speckit-tasks and spec-review aborts if it's missing. If we
# get here without one, the state machine is broken — fail loud and route the
# issue back to Spec for re-tasks.
# Two directories that fit the branch equally are a different fault: running
# Spec again cannot resolve it (it may add a third), so a human decides.
SPEC_CANDIDATES=$(bureau_spec_dir_candidates "$BRANCH")
if [ -z "$TASKS_FILE" ] && [ -n "$SPEC_CANDIDATES" ]; then
  echo "  ERROR: branch '$BRANCH' fits more than one spec directory: $SPEC_CANDIDATES"
  post_comment "$ISSUE" "❌ Implement cannot run: branch \`$BRANCH\` fits more than one spec directory ($SPEC_CANDIDATES), and the stages do not guess. Rename or remove the stray directory so that exactly one matches the branch (troubleshooting: exit 13), then remove \`needs-human\`. Routing back to Spec."
  move_issue "$ISSUE" "$BUREAU_STATE_SPEC"
  mark_needs_human "$ISSUE" implement 13 || true
  exit 13
fi
# No directory matches the branch at all (as opposed to a matched directory
# without tasks.md, below): a name mismatch that re-running Spec does not repair
# by itself, so a human decides.
if [ -z "$TASKS_FILE" ] && [ -z "$(bureau_spec_dir_for_branch "$BRANCH")" ]; then
  echo "  ERROR: no spec directory under ${BUREAU_SPECS_DIR%/}/ matches branch '$BRANCH'."
  post_comment "$ISSUE" "❌ Implement cannot run: no spec directory under \`${BUREAU_SPECS_DIR%/}/\` matches branch \`$BRANCH\`, and the stages do not guess. Rename the spec directory or the branch so that they match, or send the ticket back to Triage for a fresh spec (troubleshooting: exit 13), then remove \`needs-human\`. Routing back to Spec."
  move_issue "$ISSUE" "$BUREAU_STATE_SPEC"
  mark_needs_human "$ISSUE" implement 13 || true
  exit 13
fi
if [ -z "$TASKS_FILE" ]; then
  echo "  ERROR: no tasks.md found on branch '$BRANCH' despite valid bureau-branch marker."
  post_comment "$ISSUE" "❌ Implement cannot run: \`tasks.md\` is missing on \`$BRANCH\` despite a valid bureau-branch marker. Routing back to Spec so /speckit-tasks can run again."
  move_issue "$ISSUE" "$BUREAU_STATE_SPEC"
  exit 13
fi

NEGATIVE_CONSTRAINTS=$(build_negative_constraints)

echo "  Found tasks: $TASKS_FILE"

# ─── retry loop ───────────────────────────────────────────────────────────
START_TS=$(date +%s)
RESULT=""
STATUS=""
TASKS_DONE_TOTAL=0
COMMITS_TOTAL=0
ITER_LOG=""
i=0
CLAUDE_EXIT=0

# agents.implement.push_each_iteration. true (the default): the branch is
# pushed after every iteration and after the /goal run, as it always was.
# false: while a PR is open for the branch when this run starts, those pushes
# wait, and the end-of-run push below carries all of them at once. Each push
# to a branch with an open PR is a `pull_request: synchronize`, so CI then runs
# once over the finished state instead of once per iteration. Without an open
# PR the pushes stay, because they cost no PR run and they keep the work on
# origin should the stage die before its end.
#
# Read from .agents.implement only when that is an object
# ({"enabled": true, "push_each_iteration": false}); `agents.implement: true`
# and an absent or null key mean the default. Any other value, and a
# configuration jq cannot read, push each iteration with a warning: a push too
# many costs one CI run, a push too few can cost the work of a whole run.
PUSH_EACH_ITERATION=$(bureau_get '.agents.implement | if type == "object" then .push_each_iteration else null end | if . == null then true elif type == "boolean" then . else tojson end' 2>/dev/null) || PUSH_EACH_ITERATION="(unreadable)"
case "$PUSH_EACH_ITERATION" in
  true|false) ;;
  *)
    echo "WARN: .agents.implement.push_each_iteration = $PUSH_EACH_ITERATION is not a JSON boolean — pushing after every iteration (the default)." >&2
    PUSH_EACH_ITERATION=true
    ;;
esac
# Asked once, here, before both paths, so the /goal run and every iteration
# see the same answer. Only `false` asks. Anything but a PR number means no
# open PR: the empty answer, the literal "null" `--jq '.[0].number'` prints
# when nothing matches, and a gh that fails.
OPEN_PR_AT_START=""
if [ "$PUSH_EACH_ITERATION" = false ]; then
  OPEN_PR_AT_START=$(gh pr list --head "$BRANCH" --state open --json number --jq '.[0].number' 2>/dev/null || echo "")
  case "$OPEN_PR_AT_START" in "null"|*[!0-9]*) OPEN_PR_AT_START="" ;; esac
  if [ -n "$OPEN_PR_AT_START" ]; then
    echo "  PR #$OPEN_PR_AT_START is open for $BRANCH and push_each_iteration is false: pushing once, at the end of the run"
  fi
fi

# push_iteration <label>: the push after an iteration or after the /goal run.
DEFERRED_PUSH_PENDING=0
HEAD_BEFORE_HOOK=""
push_iteration() {
  if [ -n "$OPEN_PR_AT_START" ]; then
    DEFERRED_PUSH_PENDING=1
    echo "  push deferred ($1): PR #$OPEN_PR_AT_START is open, the end-of-run push carries the work"
  else
    push_branch_loud "$1"
  fi
}

# push_deferred <label> [rev]: push what push_iteration held back (rev, HEAD by
# default), once, loud and best effort, on a way out that does not reach the
# end-of-run push. Nothing held back, nothing to do; it never fails.
push_deferred() {
  [ "$DEFERRED_PUSH_PENDING" = 1 ] || return 0
  DEFERRED_PUSH_PENDING=0
  echo "  pushing the deferred commits of $BRANCH ($1)" >&2
  push_branch_loud "$1" "" "${2:-HEAD}"
}
# Every way out before the end-of-run push goes through this EXIT trap while
# pushes are deferred: a provider that fails or times out, an unusable Linear,
# a crash under set -e, SIGTERM or SIGHUP (bash runs the EXIT trap for those;
# `$?` in it is then not the signal's code, so the pending flag decides). The
# exit code stays what it was. Without it the held-back iterations would be
# only in the worktree: the worker keeps that worktree, but the next run on
# the branch stops at it, and a run outside the worker checks the branch out
# again from origin. Once repo.post_implement_command is about to run, the
# trap pushes the commit the hook starts from, never HEAD: a stage ended
# during or after the hook must not publish what the hook did to HEAD.
_push_deferred_on_exit() {
  push_deferred "the stage ends before its end-of-run push" "${HEAD_BEFORE_HOOK:-HEAD}"
}
if [ -n "$OPEN_PR_AT_START" ]; then
  trap _push_deferred_on_exit EXIT
fi

# EXP-token-efficiency — /goal-driven path. Closes the EXP-573 / EXP-571 /
# EXP-624 / EXP-627 stuck-detector lineage: instead of bash counting commits
# and parsing self-reported status per iter, delegate completion-evaluation
# to Haiku via Claude Code's `/goal` slash command. Haiku reads the
# transcript after every turn and decides whether the goal is met; the
# single $CLAUDE invocation only returns when Haiku says yes OR Claude
# stops after $MAX_ITER turns.
#
# The full work instructions (spec / project / design / review contexts +
# rules of engagement + JSON schema) go into --append-system-prompt; the
# goal condition itself stays under the documented 4000-char limit and
# describes only the verifiable end-state. parse_claude_json finds the last
# fenced JSON block in the combined transcript — same parse path the iter
# loop used, so the downstream PR / state-move / EXP-622 ready-flip logic
# is unchanged.
#
# Opt-in via .agents.use_goal_loop in .bureau.json (or BUREAU_USE_GOAL_LOOP=1).
# When off, the bash for-loop below runs verbatim — rollback is one flag flip.
if use_goal_loop_enabled; then
  echo "  /goal-driven implementation (use_goal_loop=true; iter loop disabled)"
  HEAD_BEFORE_RUN=$(git rev-parse HEAD)

  REVIEW_CONTEXT=$(refresh_review_context "$ISSUE")

  IMPL_SYSTEM="You are implementing $ISSUE on branch $BRANCH for $ISSUE_TITLE.
$SPEC_CONTEXT
$PROJECT_CONTEXT
$DESIGN_CONTEXT
$REVIEW_CONTEXT
Parent issue: $ISSUE — $ISSUE_TITLE
$ISSUE_DESC
Rules of engagement:
1. Read $TASKS_FILE for the full task list. If review feedback is present above, address those fixes BEFORE remaining tasks.
2. For each task in dependency order:
   a. Read adjacent files before writing new ones — match existing code style.
   b. Implement the smallest change that satisfies the task.
   c. If the task references tests, update or add them. Otherwise do not touch tests — the QA stage handles that.
   d. Commit: '$ISSUE: <task title>'. One task per commit.
   e. Mark the task [X] in $TASKS_FILE.
3. If a task is tagged 'needs-human' or '[Human]', skip and add a comment block at the intended location:
     // needs-human: <task-id> — <why this needs human judgement>

Stop conditions — halt and report status NEEDS_HUMAN when:
- A task requires information that isn't in the spec and cannot be derived from the code.
- A task conflicts with a pinned decision in SPEC.md / plan.md / CLAUDE.md.
- A task can't be implemented without breaking an existing test.
$NEGATIVE_CONSTRAINTS

End every turn with a fenced JSON status block — Haiku reads it to decide if the goal is met:
\`\`\`json
{
  \"status\": \"COMPLETE|PARTIAL|NEEDS_HUMAN|STUCK\",
  \"tasks_done\": 0,
  \"tasks_skipped\": 0,
  \"tasks_needs_human\": 0,
  \"fixed_review_items\": [],
  \"notes\": {
    \"needs_human\": [{\"task_id\": \"\", \"reason\": \"\"}],
    \"skipped\":     [{\"task_id\": \"\", \"reason\": \"\"}],
    \"deviations\":  [{\"task_id\": \"\", \"what\": \"\", \"why\": \"\"}]
  },
  \"prose_notes\": \"\"
}
\`\`\`
Do NOT emit COMPLETE without commits to back it — the bash post-check (and the goal evaluator) will catch lying-COMPLETE."

  GOAL_CONDITION="every '[ ]' checkbox in $TASKS_FILE has become '[X]' AND a fenced JSON block at the end of the turn reports status=COMPLETE with tasks_done > 0. Report status=PARTIAL+commit-summary if you got real work done but couldn't finish; status=NEEDS_HUMAN if a task requires info not in the spec; status=STUCK if no progress is possible. Stop after $MAX_ITER turns regardless."

  set +e
  RESULT=$(BUREAU_STAGE_TIMEOUT="$TOTAL_TIMEOUT" "${CLAUDE[@]}" --append-system-prompt "$IMPL_SYSTEM" "/goal $GOAL_CONDITION")
  CLAUDE_EXIT=$?
  set -e
  [ "$CLAUDE_EXIT" = 0 ] || exit "$CLAUDE_EXIT"

  record_stage_cost "$RESULT" "$ISSUE" "implement"

  STATUS=$(parse_claude_json "$RESULT" '.status // "PARTIAL"')
  [ -z "$STATUS" ] && STATUS="PARTIAL"
  TASKS_DONE_TOTAL=$(parse_claude_json "$RESULT" '.tasks_done // 0')
  [[ "$TASKS_DONE_TOTAL" =~ ^[0-9]+$ ]] || TASKS_DONE_TOTAL=0
  HEAD_AFTER_RUN=$(git rev-parse HEAD)
  COMMITS_TOTAL=$(git rev-list --count "$HEAD_BEFORE_RUN..$HEAD_AFTER_RUN" 2>/dev/null || echo 0)

  ITER_LOG="  /goal: status=$STATUS tasks_done=$TASKS_DONE_TOTAL commits=$COMMITS_TOTAL"
  [ "$CLAUDE_EXIT" = 124 ] && ITER_LOG+=" (timed out at TOTAL_TIMEOUT=${TOTAL_TIMEOUT}s)"
  ITER_LOG+=$'\n'
  echo "$ITER_LOG"

  push_iteration "/goal run"

  # Lying-COMPLETE backstop (same belt-and-suspenders the iter-loop path
  # carries via the post-loop EXP-571/EXP-624 check). Haiku is good but not
  # infallible; verify against the actual branch state.
  BRANCH_COMMITS_AHEAD=$(git rev-list --count "origin/main..HEAD" 2>/dev/null || echo 0)
  if [ "$STATUS" = "COMPLETE" ] && [ "$BRANCH_COMMITS_AHEAD" -eq 0 ]; then
    echo "  WARN: /goal reported COMPLETE but branch has no commits beyond origin/main — overriding to STUCK."
    STATUS="STUCK"
  fi
fi

if ! use_goal_loop_enabled; then
for (( i=1; i<=MAX_ITER; i++ )); do
  ELAPSED=$(( $(date +%s) - START_TS ))
  REMAINING=$(( TOTAL_TIMEOUT - ELAPSED ))
  if [ "$REMAINING" -le 60 ]; then
    echo "  Total wall-time cap exhausted (${ELAPSED}s elapsed of ${TOTAL_TIMEOUT}s). Stopping."
    STATUS="CAP_TIME"
    break
  fi

  THIS_TIMEOUT=$ITER_TIMEOUT
  [ "$THIS_TIMEOUT" -gt "$REMAINING" ] && THIS_TIMEOUT=$REMAINING

  echo ""
  echo "  → iter $i (per-iter timeout ${THIS_TIMEOUT}s)"

  # Re-fetch review feedback so mid-run human comments are seen by the next pass.
  REVIEW_CONTEXT=$(refresh_review_context "$ISSUE")

  HEAD_BEFORE=$(git rev-parse HEAD)

  # `set +e` around the Claude invocation: timeout-on-iter is normal flow, not
  # an error to bail on. We capture the exit code and decide. No EXIT trap
  # routes the ticket anywhere — a hard crash bails via `set -e` at the outer
  # scope, queue-loop sees non-zero, alert fires, issue stays in Build, next
  # tick re-picks. The retry loop preserves that. The only EXIT trap
  # (_push_deferred_on_exit, set only while pushes are deferred) pushes and
  # changes nothing else.
  PROMPT="Implement tasks from $TASKS_FILE for $ISSUE ($ISSUE_TITLE) on branch $BRANCH.

$SPEC_CONTEXT
$PROJECT_CONTEXT
$DESIGN_CONTEXT
$REVIEW_CONTEXT

Parent issue: $ISSUE — $ISSUE_TITLE
$ISSUE_DESC

Rules of engagement:
1. Read $TASKS_FILE for the full task list. If review feedback is present above, address those fixes BEFORE remaining tasks.
2. For each task in dependency order:
   a. Read adjacent files before writing new ones — match existing code style.
   b. Implement the smallest change that satisfies the task.
   c. If the task references tests, update or add them. Otherwise do not touch tests — the QA stage handles that.
   d. Commit: '$ISSUE: <task title>'. One task per commit.
   e. Mark the task [X] in $TASKS_FILE.
3. If a task is tagged 'needs-human' or '[Human]', skip the implementation and add a comment block at the intended location:
     // needs-human: <task-id> — <why this needs human judgement>

Stop conditions — do NOT guess; halt and report status NEEDS_HUMAN when:
- A task requires information that isn't in the spec and cannot be derived from the code.
- A task conflicts with a pinned decision in SPEC.md / plan.md / CLAUDE.md.
- A task can't be implemented without breaking an existing test.

$NEGATIVE_CONSTRAINTS

At the end of your work, emit a single fenced json block so the shell can summarise. Structured fields (needs_human / skipped / deviations) are auditable; \`prose_notes\` is for things that fit none of those buckets. \`fixed_review_items\` MUST list the specific fix-item IDs you addressed in this run (empty array if no review feedback was present).

\`\`\`json
{
  \"status\": \"COMPLETE|PARTIAL|NEEDS_HUMAN\",
  \"tasks_done\": 0,
  \"tasks_skipped\": 0,
  \"tasks_needs_human\": 0,
  \"fixed_review_items\": [],
  \"notes\": {
    \"needs_human\": [{\"task_id\": \"\", \"reason\": \"\"}],
    \"skipped\":     [{\"task_id\": \"\", \"reason\": \"\"}],
    \"deviations\":  [{\"task_id\": \"\", \"what\": \"\", \"why\": \"\"}]
  },
  \"prose_notes\": \"\"
}
\`\`\`"

  set +e
  RESULT=$(BUREAU_STAGE_TIMEOUT="$THIS_TIMEOUT" "${CLAUDE[@]}" --schema "$SCRIPT_REPO/scripts/bureau-implement.schema.json" "$PROMPT")
  CLAUDE_EXIT=$?
  set -e
  commit_codex_changes implement "$ISSUE"
  if [ "$CLAUDE_EXIT" != 0 ]; then
    echo "Provider pass failed with exit $CLAUDE_EXIT; preserved any changes. See provider evidence." >&2
    push_deferred "provider exit $CLAUDE_EXIT"
    exit "$CLAUDE_EXIT"
  fi

  # EXP-671 — record this iteration's token usage + est. $ (no-op unless cost
  # tracking is enabled and the output carries a usage envelope).
  record_stage_cost "$RESULT" "$ISSUE" "implement"

  # How much this iter produced; the stuck detector, the iter log and
  # COMMITS_TOTAL all read it.
  #
  # No commit message is touched here. This block used to amend `[skip ci]`
  # onto every iteration commit to save CI runs on an open PR — and a squash
  # merge without an explicit body carried that marker into the merge commit
  # on main, where GitHub then ran no CI at all. merge-body.sh and
  # check_squash_range are the two guards against a marker from any source.
  HEAD_AFTER=$(git rev-parse HEAD)
  COMMITS_THIS_ITER=$(git rev-list --count "$HEAD_BEFORE..$HEAD_AFTER" 2>/dev/null || echo 0)

  STATUS=$(parse_claude_json "$RESULT" '.status // "PARTIAL"')
  [ -z "$STATUS" ] && STATUS="PARTIAL"
  TASKS_DONE=$(parse_claude_json "$RESULT" '.tasks_done // 0')
  [[ "$TASKS_DONE" =~ ^[0-9]+$ ]] || TASKS_DONE=0
  FIXED_REVIEW=$(parse_claude_json "$RESULT" '.fixed_review_items // [] | length')
  [[ "$FIXED_REVIEW" =~ ^[0-9]+$ ]] || FIXED_REVIEW=0
  TASKS_DONE_TOTAL=$(( TASKS_DONE_TOTAL + TASKS_DONE ))

  LINE="iter $i: status=$STATUS tasks_done=$TASKS_DONE commits=$COMMITS_THIS_ITER"
  [ "$CLAUDE_EXIT" = 124 ] && LINE+=" (timed out)"
  echo "    $LINE"
  ITER_LOG+="  $LINE"$'\n'

  COMMITS_TOTAL=$(( COMMITS_TOTAL + COMMITS_THIS_ITER ))

  # The squash-range check after every iteration, before its push: a commit
  # message in origin/main..HEAD that carries a CI suppressor, or a range that
  # cannot be read, ends the loop after the iteration that wrote it instead of
  # paying for the remaining ones. The check before the hand-off below still
  # runs and writes the report, and the end-of-run push still carries the work.
  check_squash_range origin/main
  if [ "$SQUASH_CHECK" != "clean" ]; then
    echo "  ✗✗ squash-range check after iter $i is not clean: stopping the loop" >&2
    echo "$SQUASH_REPORT" >&2
    ITER_LOG+="  iter $i: squash-range check not clean, loop stopped:"$'\n'
    ITER_LOG+="$(printf '%s\n' "$SQUASH_REPORT" | sed 's/^/    /')"$'\n'
    STATUS="CI_MARKER"
    break
  fi

  # Push every iteration, unless push_iteration defers it to the end of the
  # run (agents.implement.push_each_iteration false, PR open). queue-loop's
  # reset_worktree hard-resets to origin between picks (CLAUDE.md invariant 5),
  # so commits that never reach origin are wiped.
  push_iteration "iter $i"

  # Single-strike stuck detector (EXP-573). Runs BEFORE the status-based
  # break so a model that self-reports PARTIAL with zero commits and zero
  # tasks done can't loop forever — force-park instead. Commits are the
  # load-bearing signal: self-reported tasks_done and fixed_review_items
  # are unverifiable hot air without a commit to back them up.
  #
  # COMPLETE skipped (EXP-571, brainhuggers-cli PR #109). status=COMPLETE
  # means "task list is done, no further work needed" — typical when
  # qa-pipeline bounced the ticket to Build after writing tests and
  # ticking them itself, and implement re-runs to find the production
  # code already present. Flagging that as STUCK is a false positive that
  # parks a mergeable ticket. This previously inverted EXP-573's "model
  # lies about COMPLETE" carve-out; the post-loop COMMITS_TOTAL==0 check
  # below remains as belt-and-suspenders for the lying case.
  #
  # NEEDS_HUMAN deliberately NOT in this case — an honest "I can't do this"
  # with zero work is the correct termination and we want it to flow
  # through cleanly rather than being mislabelled STUCK.
  #
  # FIXED_REVIEW also deliberately dropped from the check (was an AND in
  # the previous rule): models would self-report "I considered review
  # items" without committing anything, which let them through. The
  # commit/task floor is enough.
  if [ "$COMMITS_THIS_ITER" -eq 0 ] && [ "$TASKS_DONE" -eq 0 ]; then
    case "$STATUS" in
      PARTIAL)
        # EXP-627: only force-park to STUCK when no iter has committed.
        # A productive-then-exhausted run (commits early, dry late) is not
        # stuck — it's done with what was achievable. Let the loop exit
        # naturally at MAX_ITER with terminal status=PARTIAL so EXP-622's
        # ready-flip logic can take it from there. Promoting to STUCK
        # here would suppress that flip (it only matches PARTIAL) and
        # the PR would stay draft despite real commits landing.
        if [ "$COMMITS_TOTAL" -gt 0 ]; then
          echo "  No work this iteration, but prior iters produced ${COMMITS_TOTAL} commit(s) — letting loop continue toward MAX_ITER."
        else
          echo "  No work evidence this iteration and no prior commits — overriding status=$STATUS → STUCK."
          STATUS="STUCK"
          break
        fi
        ;;
    esac
  fi

  case "$STATUS" in
    COMPLETE|NEEDS_HUMAN) break ;;
  esac
done

# If the loop ran to completion without hitting a terminal break, status is
# either COMPLETE (rare — loop would have broken) or PARTIAL with progress.
# Normalise to PARTIAL so the case below handles it.
if [ "$i" -gt "$MAX_ITER" ] && [ "$STATUS" != "COMPLETE" ] && [ "$STATUS" != "NEEDS_HUMAN" ] && [ "$STATUS" != "STUCK" ] && [ "$STATUS" != "CAP_TIME" ]; then
  STATUS="PARTIAL"
fi

# Belt-and-suspenders for EXP-573: if the loop ended with COMPLETE but the
# branch has no commits beyond origin/main, the model is lying — override
# to STUCK so the issue is parked, not shipped.
#
# Branch-wide check, not COMMITS_TOTAL this tick (EXP-571 / EXP-624). The
# legit case the per-iter exemption above admits — qa-pipeline bounced the
# ticket to Build after writing tests, implement re-runs and sees nothing
# left to do — produces COMMITS_TOTAL=0 in THIS tick but the branch
# still has the prior implement-run's commits. Trusting branch state
# instead of this-tick state lets that path through while still parking
# a truly empty branch claimed as COMPLETE.
BRANCH_COMMITS_AHEAD=$(git rev-list --count origin/main..HEAD 2>/dev/null || echo 0)
if [ "$STATUS" = "COMPLETE" ] && [ "$BRANCH_COMMITS_AHEAD" -eq 0 ]; then
  echo "  WARN: terminal status=COMPLETE but branch has no commits beyond origin/main — overriding to STUCK."
  STATUS="STUCK"
fi
fi  # end of `if ! use_goal_loop_enabled` wrapper around iter-loop + post-loop overrides

# The squash-range check over the finished state, for both paths, before
# anything is handed on (check_squash_range in bureau-config.sh). Clean is one
# extra line. Not clean — a commit message in origin/main..HEAD carries a CI
# suppressor, or the range could not be read — overrides every status,
# COMPLETE included: the stage ends in CI_MARKER, and the halt branch below
# keeps the PR a draft, labels needs-human and puts the report on the PR.
# The optional post-implement hook runs first, so its commits are inside the
# range the check reads and inside the final push (run_post_implement_command).
HEAD_BEFORE_HOOK=$(git rev-parse HEAD)
run_post_implement_command
if [ "$POST_IMPLEMENT_FAILED" = 1 ]; then
  STATUS="POST_IMPLEMENT_FAILED"
fi

check_squash_range origin/main
if [ "$SQUASH_CHECK" = "clean" ]; then
  echo "  $SQUASH_REPORT"
else
  echo "$SQUASH_REPORT" >&2
  STATUS="CI_MARKER"
fi

echo ""
echo "Phase 2/2: terminal status=$STATUS (after $i iter(s))"

# One push over the finished state, before the PR is opened or marked ready
# below. The per-iter pushes above are non-fatal, so this is the retry for any
# that failed; with agents.implement.push_each_iteration false and a PR open
# at the start (push_iteration), it is the run's only push. It used to be an
# empty "CI re-trigger" commit, paired with the `[skip ci]` amend the iter loop
# no longer makes; with no suppressed pushes there is nothing to re-trigger,
# so no commit is written here.
# Pushes when the run committed or when this worktree holds anything origin
# does not (a merge of origin/main before the loop is counted by neither
# COMMITS_TOTAL nor the iter log). An unreadable comparison counts as ahead.
AHEAD_OF_ORIGIN=$(git rev-list --count "origin/$BRANCH..HEAD" 2>/dev/null || echo 1)
# A hook that moved HEAD off the commit it started from gets no push below.
# With deferred pushes origin then lacks the run's own commits: push the commit
# the hook started from, which is what the per-iteration pushes would have put
# there. From here on the end-of-run push owns the push, and the EXIT trap has
# nothing left to do.
if [ "$POST_IMPLEMENT_HEAD_REWRITTEN" = 1 ]; then
  push_deferred "the run's commits, without the hook's rewrite" "$HEAD_BEFORE_HOOK"
fi
DEFERRED_PUSH_PENDING=0
# A failed push here is retried once after a short wait. If it still fails,
# the branch is fetched from origin (a rejected push does not update
# origin/$BRANCH, and someone may have rewritten it) and HEAD is compared with
# it. If HEAD holds commits origin does not, the stage ends with 18 here,
# before any PR is marked ready or the ticket moves: nothing is handed on that
# origin does not have, and the worker keeps the worktree because it is ahead
# of origin. If origin already has every commit — the usual case, the per-iter
# pushes went through and this one had nothing to send — a failed push changes
# nothing and the stage goes on. A failed fetch or an unreadable comparison
# counts as missing commits.
if [ "$COMMITS_TOTAL" -gt 0 ] || [ "$AHEAD_OF_ORIGIN" -gt 0 ]; then
  if [ "$POST_IMPLEMENT_HEAD_REWRITTEN" = 1 ]; then
    echo "  ✗✗ not pushing: repo.post_implement_command moved HEAD off the commit it started from" >&2
  elif ! push_branch_loud "end of run" status; then
    sleep 3
    if ! push_branch_loud "end of run, retry" status; then
      # Explicit refspec: a plain `git fetch origin "$BRANCH"` writes only
      # FETCH_HEAD when remote.origin.fetch does not cover the branch (a
      # --single-branch clone, a narrowed refspec), and the comparison below
      # would read a stale ref.
      if git fetch -q origin "+refs/heads/$BRANCH:refs/remotes/origin/$BRANCH" >/dev/null 2>&1; then
        UNPUSHED=$(git rev-list --count "origin/$BRANCH..HEAD" 2>/dev/null || echo unreadable)
      else
        UNPUSHED=unreadable
      fi
      if [ "$UNPUSHED" = unreadable ]; then
        UNPUSHED_TEXT="origin could not be read to compare"
      else
        UNPUSHED_TEXT="${UNPUSHED} commit(s) missing on origin"
      fi
      if [ "$UNPUSHED" = 0 ]; then
        echo "  the final push failed twice, but origin/$BRANCH already has every commit of HEAD; going on" >&2
      else
        echo "  ✗✗ ${UNPUSHED_TEXT}; nothing is handed on" >&2
        PUSH_FAIL_COMMENT="❌ Implement stopped before hand-off: the final push of \`$BRANCH\` to origin failed twice (${UNPUSHED_TEXT}). Nothing went to QA or Build Review and the ticket stays in Build. The commits origin lacks are only in the implement worktree, which the worker keeps. Push the branch once origin accepts it, or re-run the stage."
        if [ "$POST_IMPLEMENT_FAILED" = 1 ]; then
          PUSH_FAIL_COMMENT="${PUSH_FAIL_COMMENT}

repo.post_implement_command had failed before the push, too:
\`\`\`
${POST_IMPLEMENT_REPORT}
\`\`\`"
        fi
        post_comment "$ISSUE" "$PUSH_FAIL_COMMENT" || true
        exit 18
      fi
    fi
  fi
fi

if [ "$STATUS" = "COMPLETE" ] && [ "$(resolve_runner_for_stage implement)" = codex ]; then
  TEST_COMMAND=$(bureau_get '.repo.test_command // empty')
  [ -n "$TEST_COMMAND" ] || { echo 'Codex completion needs repo.test_command for independent verification.' >&2; exit 24; }
  # Branch code: without the Bureau secrets (bureau_untrusted_env, bureau-env.sh).
  bureau_untrusted_env bash --noprofile --norc -c "$TEST_COMMAND" || exit 14
fi

PR_URL=""
NEEDS_HUMAN_UNMARKED=0
case "$STATUS" in
  COMPLETE)
    # When QA is configured (opt-in), the implement pipeline routes through QA
    # instead of going straight to code review. QA runs the test suite and
    # writes missing tests before a reviewer sees the PR.
    if [ -n "${BUREAU_STATE_QA:-}" ]; then
      NEXT_STATE_LABEL="QA"
      NEXT_STATE_ID="$BUREAU_STATE_QA"
    else
      NEXT_STATE_LABEL="Build Review"
      NEXT_STATE_ID="$BUREAU_STATE_BUILD_REVIEW"
    fi

    PR_URL=$(open_or_update_pr_ready "$ISSUE" "$ISSUE_TITLE")
    post_comment "$ISSUE" "$(build_summary_comment COMPLETE "$TASKS_DONE_TOTAL" "$ITER_LOG" "$PR_URL")"
    move_issue "$ISSUE" "$NEXT_STATE_ID"
    echo "  Moved $ISSUE to $NEXT_STATE_LABEL"
    ;;

  NEEDS_HUMAN|STUCK|CAP_TIME|PARTIAL|CI_MARKER|POST_IMPLEMENT_FAILED)
    # PARTIAL with real commits proceeds to downstream gates as ready-for-review
    # so CI fires on the ready_for_review transition (EXP-622 / FR-001). Every
    # other halt status — and PARTIAL with zero commits — stays draft (FR-002,
    # FR-003). The summary comment, needs-human label, escalation log, and
    # operator status report below are unchanged (FR-005).
    # CI_MARKER stays draft like every other halt status: a ready PR would be
    # exactly the hand-off the squash-range check refuses. Its report also goes
    # on the PR, once the PR is sure to exist.
    if [ "$STATUS" = "PARTIAL" ] && [ "$COMMITS_TOTAL" -gt 0 ]; then
      PR_URL=$(open_or_update_pr_ready "$ISSUE" "$ISSUE_TITLE")
    else
      PR_URL=$(open_or_update_pr_draft "$ISSUE" "$ISSUE_TITLE")
    fi
    if [ "$STATUS" = "CI_MARKER" ]; then
      comment_on_branch_pr "$BRANCH" "$SQUASH_REPORT"
    fi
    PR_NUMBER=$(gh pr list --head "$BRANCH" --json number --jq '.[0].number' 2>/dev/null || echo "")
    if mark_needs_human "$ISSUE" implement; then
      log_escalation "$ISSUE" "implement" "$i" \
        "$STATUS: $TASKS_DONE_TOTAL tasks done across $i iter(s)" \
        "${PR_NUMBER:-0}" "$BRANCH"
    else
      NEEDS_HUMAN_UNMARKED=1
    fi
    post_comment "$ISSUE" "$(build_summary_comment "$STATUS" "$TASKS_DONE_TOTAL" "$ITER_LOG" "$PR_URL")"
    NEXT_STATE_LABEL="Build (needs-human)"
    ;;
esac

echo ""
echo "═══════════════════════════════════════"
echo "  Implement pipeline complete: $ISSUE"
echo "  Branch: $BRANCH"
echo "  PR: ${PR_URL:-existing}"
echo "  Status: $NEXT_STATE_LABEL ($STATUS)"
echo "═══════════════════════════════════════"

# A needs-human escalation whose label could not be written must not read as
# success to the driver (EXP-1516): the local hold keeps the queue away, the
# non-zero exit stops a shepherd.
if [ "$NEEDS_HUMAN_UNMARKED" = 1 ]; then exit 25; fi

# A failed post-implement hook has been handled like every halt above (draft
# PR, needs-human, escalation log, summary comment with its report); the stage
# still ends non-zero so the shepherd halts and the queue alerts it as
# build-failed (14) — the hook is the repo's own build step for derived files.
# When its needs-human label could not be written either, the 25 above wins:
# the hold is what keeps the queue away.
if [ "$POST_IMPLEMENT_FAILED" = 1 ]; then
  exit 14
fi
