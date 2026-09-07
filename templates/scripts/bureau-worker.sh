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
BRANCH="${4:-}"
case "$PIPELINE" in spec-pipeline.sh|spec-review-pipeline.sh|ux-pipeline.sh|copy-pipeline.sh|implement-pipeline.sh|qa-pipeline.sh|code-review-pipeline.sh|merge-pipeline.sh|rebase-pipeline.sh) ;; *) exit 1 ;; esac
if [ "${BUREAU_DRY_RUN:-0}" = 1 ]; then
  echo "[DRY_RUN] $ISSUE $PIPELINE workspace=$WORKTREE branch=$BRANCH"
  exit 0
fi
if [ "${BUREAU_ACTIVE_ENTRY:-}" != "$0" ]; then
  exec python3 "$BUREAU_RUNTIME" --repo "$REPO_DIR" exec --issue "$ISSUE" --workspace "$WORKTREE" --entry "$0" -- bash "$0" "$@"
fi
export BUREAU_WORKSPACE_MODE=disposable
reset_worktree "$WORKTREE" "$PIPELINE" "$BRANCH"
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
    key=$(python3 -c 'import hashlib, pathlib, sys; print(hashlib.sha256(str(pathlib.Path(sys.argv[1]).resolve()).encode()).hexdigest())' "$WORKTREE")
    rm -f "$common/bureau/workers/$key"
    # A successful stopped review can leave only its local validation merge.
    # Keep that clean checkpoint (HEAD and files), but release this worker's own
    # branch so new work can use a fresh checkout without resetting the checkpoint.
    if [ "$rc" = 20 ] && [ "$PIPELINE" = code-review-pipeline.sh ] && [ -z "$(git -C "$WORKTREE" status --porcelain)" ]; then
      git -C "$WORKTREE" checkout --detach --quiet || return
      python3 "$SCRIPT_DIR/bureau-supervision.py" --repo "$WORKTREE" checkpoint "$ISSUE" >/dev/null || return
      echo "Preserved stopped review checkpoint in $WORKTREE; future work uses another checkout." >&2
    else
      echo "Preserved unfinished work in $WORKTREE; disposable registration removed. Inspect/resume before reuse." >&2
    fi
    return
  fi
  git -C "$WORKTREE" checkout --detach --quiet 2>/dev/null || true
}
trap _worker_cleanup EXIT
cd "$WORKTREE"
bash "$SCRIPT_DIR/$PIPELINE" "$ISSUE"
