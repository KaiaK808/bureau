#!/bin/bash
# One disposable-worker stage, with issue/worktree ownership before any reset.
set -euo pipefail
REPO_DIR="$(pwd)"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SCRIPT_REPO="$(dirname "$SCRIPT_DIR")"
source "$SCRIPT_DIR/bureau-config.sh"
ISSUE="${1:?issue required}"
PIPELINE="${2:?pipeline required}"
WORKTREE="${3:?worktree required}"
# A relative worktree is relative to the directory the worker starts in; made
# absolute here, since the worker changes into the worktree and its EXIT
# cleanup and registry key must still name it from there.
case "$WORKTREE" in /*) ;; *) WORKTREE="$REPO_DIR/$WORKTREE" ;; esac
BRANCH="${4:-}"
case "$PIPELINE" in spec-pipeline.sh|spec-review-pipeline.sh|ux-pipeline.sh|copy-pipeline.sh|implement-pipeline.sh|qa-pipeline.sh|code-review-pipeline.sh|merge-pipeline.sh|rebase-pipeline.sh) ;; *) exit 1 ;; esac
if [ "${BUREAU_DRY_RUN:-0}" = 1 ]; then
  echo "[DRY_RUN] $ISSUE $PIPELINE workspace=$WORKTREE branch=$BRANCH"
  exit 0
fi
if [ "${BUREAU_ACTIVE_ENTRY:-}" != "$0" ]; then
  bureau_exec_runtime python3 -I "$BUREAU_RUNTIME" --repo "$REPO_DIR" exec --issue "$ISSUE" --workspace "$WORKTREE" --entry "$0" -- bash "$0" "$@"
fi
export BUREAU_WORKSPACE_MODE=disposable
# A signal (the runtime forwards Ctrl-C and SIGTERM to this process group, and
# SIGTERM after a hang-up; a SIGHUP sent to the group counts the same) ends the
# worker with 130 once the command in flight returns. Untrapped, bash hands
# its EXIT trap $? = 0 after a signal: the cleanup below took a cancelled stage
# for a finished one and detached the worktree from the branch it was building,
# and the resume steps could no longer name that branch.
trap 'exit 130' INT TERM HUP
# A reset that refuses the worktree over ownership (exit 21: an unregistered or
# foreign worktree, the branch held by another checkout, a lost claim) leaves the
# halt on the ticket: needs-human and one comment naming the worktree and the way
# back (bureau_reset_refusal_trace in bureau-config.sh). An EXIT trap keeps
# reset_worktree under errexit; it ends with the reset's own code, since bash 3.2
# ends a script with its EXIT trap's last status. A signal during the reset ends
# with 130 and writes nothing.
_worker_reset_refused() {
  local rc=$?
  if [ "$rc" = 21 ] && [ -n "${BUREAU_RESET_REFUSAL:-}" ]; then
    # The runtime above runs without the .env keys; read them back as a stage does.
    if ! bureau_secret_set LINEAR_API_KEY; then
      if [ -f "${BUREAU_ENV_FILE:-}" ]; then bureau_load_env --export "$BUREAU_ENV_FILE" || true; fi
    fi
    bureau_reset_refusal_trace "$ISSUE" "${PIPELINE%-pipeline.sh}" || true
  fi
  exit "$rc"
}
trap _worker_reset_refused EXIT
reset_worktree "$WORKTREE" "$PIPELINE" "$BRANCH"
trap - EXIT
# Release only this registered worker's branch after the stage, so the next
# worker can acquire it without touching an app/user checkout.
_worker_cleanup() {
  local rc=$? branch ahead=0 common key
  branch=$(git -C "$WORKTREE" branch --show-current)
  if [ -n "$branch" ]; then
    ahead=$(git -C "$WORKTREE" rev-list --count "origin/$branch..HEAD" 2>/dev/null || echo 1)
  fi
  if [ "$rc" != 0 ] && { [ -n "$(git -C "$WORKTREE" status --porcelain)" ] || [ "$ahead" != 0 ]; }; then
    common=$(git -C "$REPO_DIR" rev-parse --git-common-dir)
    case "$common" in /*) ;; *) common="$REPO_DIR/$common" ;; esac
    key=$(python3 -I -c 'import hashlib, pathlib, sys; print(hashlib.sha256(str(pathlib.Path(sys.argv[1]).resolve()).encode()).hexdigest())' "$WORKTREE")
    rm -f "$common/bureau/workers/$key"
    # A successful stopped review can leave only its local validation merge.
    # Keep that clean checkpoint (HEAD and files), but release this worker's own
    # branch so new work can use a fresh checkout without resetting the checkpoint.
    # Either way the record names the run and ticket for the reset that later
    # refuses this worktree (bureau_preserve_note).
    if [ "$rc" = 20 ] && [ "$PIPELINE" = code-review-pipeline.sh ] && [ -z "$(git -C "$WORKTREE" status --porcelain)" ]; then
      bureau_preserve_note "$WORKTREE" "$ISSUE" review-checkpoint || true
      git -C "$WORKTREE" checkout --detach --quiet || return
      python3 -I "$SCRIPT_DIR/bureau-supervision.py" --repo "$WORKTREE" checkpoint "$ISSUE" >/dev/null || return
      echo "Preserved stopped review checkpoint in $WORKTREE; future work uses another checkout." >&2
    else
      if [ "$rc" = 130 ]; then bureau_preserve_note "$WORKTREE" "$ISSUE" interrupted || true
      else bureau_preserve_note "$WORKTREE" "$ISSUE" unfinished || true; fi
      echo "Preserved unfinished work in $WORKTREE; disposable registration removed. Inspect/resume before reuse." >&2
    fi
    return
  fi
  git -C "$WORKTREE" checkout --detach --quiet 2>/dev/null || true
}
trap _worker_cleanup EXIT
cd "$WORKTREE"
bash "$SCRIPT_DIR/$PIPELINE" "$ISSUE"
