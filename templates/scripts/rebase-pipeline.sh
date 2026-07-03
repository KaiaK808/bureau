#!/bin/bash
# rebase-pipeline.sh — opt-in helper that rebases bureau-only DIRTY PRs.
#
# OFF BY DEFAULT — this script force-pushes, which mutates shared remote state.
# Enable with `agents.rebase: true` in .bureau.json.
#
# Picks issues from BUREAU_STATE_MERGE (same waiting room as merge-pipeline.sh)
# and rebases the underlying PR onto origin/main only if EVERY gate passes:
#
#   1. mergeStateStatus == DIRTY                 (actual merge conflict, not BEHIND
#                                                 — squash-merge tolerates BEHIND)
#   2. Every commit ahead of origin/main carries a `Co-authored-by: Claude`
#      trailer. If a human commit is in the divergence, refuse and post.
#      Humans rebase their own branches.
#
# Successful rebase: force-push with --force-with-lease, then move the issue
# back to Build Review so code-review-pipeline.sh re-reviews against the new
# base. (We do NOT trigger code-review by gaming pick_issue — the state move
# is the trigger.)
#
# Conflict on rebase: `git rebase --abort`, leave the PR alone, post one
# comment explaining the conflict, label needs-human.
#
# --dry-run: print the gate verdicts and the action that would be taken,
# never call git rebase / git push / linear mutations.

set -euo pipefail
unset CLAUDECODE 2>/dev/null || true

REPO_DIR="$(pwd)"
SCRIPT_REPO="$(cd "$(dirname "$0")/.." && pwd)"
source "$(dirname "$0")/bureau-config.sh"

if [ -f .env ]; then source .env
elif [ -f "$SCRIPT_REPO/.env" ]; then source "$SCRIPT_REPO/.env"
else echo "ERROR: No .env found"; exit 1; fi

API_KEY="${LINEAR_API_KEY:?Set LINEAR_API_KEY in .env}"

DRY_RUN=false
POSITIONAL=()
for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY_RUN=true ;;
    *) POSITIONAL+=("$arg") ;;
  esac
done
# Re-set positional params from the filtered list. Guarded form because
# `set -- "${arr[@]:-}"` injects an empty "$1" when the array is empty.
if [ "${#POSITIONAL[@]}" -gt 0 ]; then
  set -- "${POSITIONAL[@]}"
else
  set --
fi

precondition_linear

if [ -z "${BUREAU_STATE_MERGE:-}" ]; then
  echo "rebase-pipeline: linear.teams[0].states.merge not configured. Queue empty."
  exit 2
fi

if [ -n "${1:-}" ]; then
  ISSUE="$1"
  echo "Using specified issue: $ISSUE"
else
  echo "Picking next Merge issue (rebase candidate)..."
  ISSUE=$(pipeline_pick_next "$(basename "$0")")
  if [ -z "$ISSUE" ] || [[ ! "$ISSUE" =~ ^[A-Z]+-[0-9]+$ ]]; then
    echo "No qualifying issues found. Queue empty."
    exit 2
  fi
  echo "Picked: $ISSUE"
fi

echo ""
echo "═══════════════════════════════════════"
echo "  Rebase Pipeline: $ISSUE$([ "$DRY_RUN" = true ] && echo ' (dry-run)')"
echo "═══════════════════════════════════════"

BRANCH=$(get_issue_branch "$ISSUE")
if [ -z "$BRANCH" ] || [[ "$BRANCH" == *" "* ]]; then
  echo "  ERROR: no bureau-branch marker found for $ISSUE."
  [ "$DRY_RUN" = false ] && post_comment "$ISSUE" "❌ Rebase aborted — no bureau-branch marker."
  exit 12
fi
echo "  Branch: $BRANCH"

PR_NUMBER=$(gh pr list --head "$BRANCH" --json number --jq '.[0].number' 2>/dev/null || echo "")
if [ -z "$PR_NUMBER" ]; then
  echo "  ERROR: no PR found for branch $BRANCH"
  [ "$DRY_RUN" = false ] && post_comment "$ISSUE" "❌ Rebase aborted — no PR for \`$BRANCH\`."
  exit 15
fi

MERGE_STATE=$(gh pr view "$PR_NUMBER" --json mergeStateStatus --jq .mergeStateStatus)
echo "  PR: #$PR_NUMBER / mergeStateStatus: $MERGE_STATE"
if [ "$MERGE_STATE" != "DIRTY" ]; then
  echo "  Not DIRTY — nothing to rebase. Queue empty."
  exit 2
fi

# Bureau-only divergence check — single source of truth lives in
# bureau-config.sh:branch_is_bureau_only. merge-pipeline.sh uses the same
# helper to decide whether to label `rebase-needed` on DIRTY PRs.
git -C "$REPO_DIR" fetch origin --quiet
if ! branch_is_bureau_only "$BRANCH"; then
  HUMAN_COMMITS=$(cd "$REPO_DIR" && _bureau_human_commits "$BRANCH")
  echo "  Human commits in divergence — refusing to rebase:"
  echo "$HUMAN_COMMITS" | sed 's/^/    /'
  if [ "$DRY_RUN" = false ]; then
    post_comment "$ISSUE" "🛑 Rebase skipped — human commits in divergence on \`$BRANCH\`. A human should rebase this branch (the bureau only auto-rebases its own commits)."
  fi
  exit 0
fi

echo "  Divergence is bureau-only — safe to rebase."

if [ "$DRY_RUN" = true ]; then
  echo "  [dry-run] would: git rebase origin/main && git push --force-with-lease origin HEAD:$BRANCH"
  echo "  [dry-run] would move $ISSUE back to Build Review for re-review."
  exit 0
fi

# queue-loop.sh's reset_worktree has already checked out $BRANCH for us
# (rebase-pipeline.sh is registered alongside code-review-pipeline.sh in the
# branch-checkout case). Verify before rebasing — if we're not on $BRANCH,
# fail loud rather than rebasing the wrong ref.
CUR=$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo "")
if [ "$CUR" != "$BRANCH" ]; then
  echo "  ERROR: worktree HEAD is on '$CUR', expected '$BRANCH'. Aborting."
  post_comment "$ISSUE" "❌ Rebase aborted — worktree HEAD was not on \`$BRANCH\`. Pipeline state mismatch."
  exit 12
fi

echo "  Rebasing $BRANCH onto origin/main..."
if git rebase origin/main; then
  echo "  Rebase succeeded — force-pushing with lease."
  if git push --force-with-lease origin "HEAD:$BRANCH"; then
    post_comment "$ISSUE" "🔄 Rebased \`$BRANCH\` onto \`main\` and force-pushed (\`--force-with-lease\`). Routing back to Build Review for re-check."
    move_issue "$ISSUE" "$BUREAU_STATE_BUILD_REVIEW"
    echo "  Pushed. Issue moved back to Build Review."
  else
    echo "  Force-push rejected (lease lost — branch moved underneath us)."
    post_comment "$ISSUE" "❌ Rebase succeeded locally but \`--force-with-lease\` was rejected: \`$BRANCH\` moved on origin between fetch and push. Needs human."
    add_issue_label "$ISSUE" "needs-human" \
      || echo "  WARN: failed to add 'needs-human' label to $ISSUE; will retry on next tick" >&2
    exit 19
  fi
else
  echo "  Rebase produced conflicts — aborting."
  git rebase --abort 2>/dev/null || true
  post_comment "$ISSUE" "🛑 Rebase produced conflicts on \`$BRANCH\` against \`main\`. Needs human resolution."
  add_issue_label "$ISSUE" "needs-human" \
    || echo "  WARN: failed to add 'needs-human' label to $ISSUE; will retry on next tick" >&2
  exit 0
fi

echo ""
echo "═══════════════════════════════════════"
echo "  Rebase complete: $ISSUE"
echo "═══════════════════════════════════════"
