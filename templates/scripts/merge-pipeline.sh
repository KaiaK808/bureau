#!/bin/bash
# merge-pipeline.sh — gated PR merger.
#
# Picks issues from BUREAU_STATE_MERGE (the "approved, awaiting merge" waiting
# room populated by code-review-pipeline.sh when agents.merge is enabled) and
# merges the underlying PR only if every gate below passes:
#
#   1. PR state == OPEN                          (gh pr view --json state)
#   2. mergeStateStatus == CLEAN                 (GitHub heuristic — async, lax)
#   3. CI green on the PR's CURRENT head SHA     (NRSR: bureau-enforced, see
#                                                  pr_ci_is_green in bureau-config.sh)
#   4. PR base SHA == origin/<base-ref> HEAD     (NRSR: pr_base_is_current,
#                                                  catches mergeStateStatus's
#                                                  async-cache race)
#   5. Latest "Code Review v2" verdict on the PR was APPROVE / AUTO_APPROVE
#   6. No `needs-human`, `blocked`, or `wip` label on the PR
#   7. Zero unresolved review threads (GraphQL: pullRequest.reviewThreads)
#
# Gates 3 and 4 enforce the Not-Rocket-Science Rule independently of GitHub's
# mergeStateStatus — they catch (a) PRs that merge with red CI when no
# required-checks rule is configured, and (b) the async-cache race where
# mergeStateStatus still reads CLEAN after main has advanced.
#
# Gates 3 and 4 are toggleable via .bureau.json:
#   - agents.merge_require_green_ci   (default true)
#   - agents.merge_require_up_to_date (default true)
# Leaving the defaults is strongly recommended; the toggles exist for repos
# without CI (docs-only) or with deliberate batch-merge workflows.
#
# When eligible, runs `gh pr merge N --$BUREAU_MERGE_STRATEGY` (squash by
# default; configurable via .agents.merge_strategy in .bureau.json). Deliberately
# no --delete-branch and no --auto: see code-review-pipeline.sh:314-322 for the
# worktree/detached-HEAD rationale; --auto would queue the merge for later, we
# want loud immediate failure if a gate slipped between the check and the call.
#
# When NOT eligible, comments on the PR with the precise blocker — but only if
# the blocker has changed since the bot's last "Bureau merge gate" comment, so
# this script can run every poll interval without spamming.
#
# Opt-in via .bureau.json:
#   - agents.merge: true
#   - linear.teams[0].states.merge: "<uuid of the Merge workflow state>"
# Both must be set; missing either makes this pipeline a queue-empty no-op.
#
# --dry-run: print the gate verdicts and the action that would be taken, but
# never call `gh pr merge` and never post comments. Use to audit before
# trusting the agent.

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
# Repo-wide BUREAU_DRY_RUN env var also flips this script's existing dry-run
# path. Either source works; the CLI flag and env var converge to one
# DRY_RUN flag so downstream branches stay simple.
[ "${BUREAU_DRY_RUN:-0}" = "1" ] && DRY_RUN=true
# Re-set positional params from the filtered list. Guarded form because
# `set -- "${arr[@]:-}"` injects an empty "$1" when the array is empty.
if [ "${#POSITIONAL[@]}" -gt 0 ]; then
  set -- "${POSITIONAL[@]}"
else
  set --
fi

precondition_linear

if [ -z "${BUREAU_STATE_MERGE:-}" ]; then
  echo "merge-pipeline: linear.teams[0].states.merge not configured in .bureau.json. Queue empty."
  exit 2
fi

if [ -n "${1:-}" ]; then
  ISSUE="$1"
  echo "Using specified issue: $ISSUE"
else
  echo "Picking next Merge issue..."
  ISSUE=$(pipeline_pick_next "$(basename "$0")")
  if [ -z "$ISSUE" ] || [[ ! "$ISSUE" =~ ^[A-Z]+-[0-9]+$ ]]; then
    echo "No qualifying issues found. Queue empty."
    exit 2
  fi
  echo "Picked: $ISSUE"
fi

echo ""
echo "═══════════════════════════════════════"
echo "  Merge Pipeline: $ISSUE$([ "$DRY_RUN" = true ] && echo ' (dry-run)')"
echo "═══════════════════════════════════════"

BRANCH=$(get_issue_branch "$ISSUE")
if [ -z "$BRANCH" ] || [[ "$BRANCH" == *" "* ]]; then
  echo "  ERROR: no bureau-branch marker found for $ISSUE."
  [ "$DRY_RUN" = false ] && post_comment "$ISSUE" "❌ Merge aborted — no bureau-branch marker."
  exit 12
fi
echo "  Branch: $BRANCH"

PR_NUMBER=$(gh pr list --head "$BRANCH" --json number --jq '.[0].number' 2>/dev/null || echo "")
# `gh pr list --jq '.[0].number'` returns the literal string "null" (not empty)
# when no open PR matches the branch — treat both as "no open PR".
if [ -z "$PR_NUMBER" ] || [ "$PR_NUMBER" = "null" ]; then
  # Ghost-merge recovery: the bureau-tracked branch has no OPEN PR, but a
  # merged-then-deleted PR may already exist (squash + auto-delete done by a
  # human, an out-of-band `gh pr merge`, or a prior tick that died after the
  # merge but before move_issue). Without this, the ticket sticks in Merge
  # forever and the next tick re-stalls identically. Scope strictly: the
  # merged PR must (a) mention this issue ID in its title or body AND
  # (b) have a headRefName equal to the bureau-tracked $BRANCH. Either
  # condition alone is unsafe — cross-referenced tickets or a reused branch
  # name on a different issue would corrupt state.
  MERGED_MATCH=$(gh pr list \
    --state merged \
    --search "$ISSUE in:title,body" \
    --json number,headRefName,mergedAt,mergeCommit \
    --limit 10 \
    --jq "[.[] | select(.headRefName == \"$BRANCH\")] | .[0]" 2>/dev/null || echo "")
  if [ -n "$MERGED_MATCH" ] && [ "$MERGED_MATCH" != "null" ]; then
    MERGED_PR=$(echo "$MERGED_MATCH" | jq -r '.number')
    MERGED_AT=$(echo "$MERGED_MATCH" | jq -r '.mergedAt')
    MERGED_SHA=$(echo "$MERGED_MATCH" | jq -r '.mergeCommit.oid // "unknown"')
    echo "  → PR #$MERGED_PR (branch $BRANCH) already merged at $MERGED_AT (commit $MERGED_SHA)"
    if [ "$DRY_RUN" = true ]; then
      echo "  [dry-run] would post recovery comment and move $ISSUE to Done."
      exit 0
    fi
    post_comment "$ISSUE" "✅ PR #$MERGED_PR was merged at $MERGED_AT (commit \`$MERGED_SHA\`) outside the merge-pipeline. Bumping to Done — no further action needed."
    move_issue "$ISSUE" "$BUREAU_STATE_DONE"
    exit 0
  fi
  echo "  ERROR: no PR found for branch $BRANCH"
  [ "$DRY_RUN" = false ] && post_comment "$ISSUE" "❌ Merge aborted — no PR for branch \`$BRANCH\`."
  exit 15
fi

PR_DATA=$(gh pr view "$PR_NUMBER" --json state,mergeStateStatus,labels,url,headRefName)
PR_STATE=$(echo "$PR_DATA" | jq -r '.state')
MERGE_STATE=$(echo "$PR_DATA" | jq -r '.mergeStateStatus')
PR_URL=$(echo "$PR_DATA" | jq -r '.url')
echo "  PR: #$PR_NUMBER ($PR_URL)"
echo "  State: $PR_STATE / mergeStateStatus: $MERGE_STATE"

# If already merged: short-circuit to Done.
if [ "$PR_STATE" = "MERGED" ]; then
  echo "  PR already merged — moving issue to Done."
  if [ "$DRY_RUN" = false ]; then
    post_comment "$ISSUE" "✅ PR #$PR_NUMBER already merged. Moving to Done."
    move_issue "$ISSUE" "$BUREAU_STATE_DONE"
  fi
  exit 0
fi

# evaluate_merge_gates: run every gate against fresh GitHub data. Prints
# "FAIL: ..." lines to stdout for each failed gate, returns 0 if all pass.
# Called twice: once at the top of the pipeline (to render the gate report
# and decide whether to attempt the merge), and again immediately before
# `gh pr merge` (just-in-time recheck — closes the race between the initial
# query and the merge call).
#
# Output rows are stable so the bot's idempotent-comment logic can diff them.
evaluate_merge_gates() {
  local pr="$1"
  local _pr_data _pr_state _merge_state _labels_csv
  _pr_data=$(gh pr view "$pr" --json state,mergeStateStatus,labels 2>/dev/null || echo '{}')
  _pr_state=$(echo "$_pr_data" | jq -r '.state // ""')
  _merge_state=$(echo "$_pr_data" | jq -r '.mergeStateStatus // ""')
  _labels_csv=$(echo "$_pr_data" | jq -r '[.labels[]?.name] | join(",")')

  local owner_repo owner repo unresolved verdict
  owner_repo=$(_bureau_gh_owner_repo)
  owner="${owner_repo%/*}"
  repo="${owner_repo#*/}"
  unresolved=$(gh api graphql \
    -f query='query($owner:String!,$repo:String!,$num:Int!){repository(owner:$owner,name:$repo){pullRequest(number:$num){reviewThreads(first:100){nodes{isResolved}}}}}' \
    -f owner="$owner" -f repo="$repo" -F num="$pr" 2>/dev/null \
    | jq '[.data.repository.pullRequest.reviewThreads.nodes[]? | select(.isResolved == false)] | length' 2>/dev/null \
    || echo "0")
  verdict=$(gh pr view "$pr" --json comments \
    --jq '[.comments[] | select(.body | test("Code Review v2"))] | sort_by(.createdAt) | last | .body // ""' \
    | grep -oE '\*\*Verdict\*\*[[:space:]]*:[[:space:]]*[A-Z_]+' \
    | grep -oE 'APPROVE|AUTO_APPROVE|REQUEST_CHANGES|BLOCK' \
    | head -1 || true)

  local _block_label=""
  local _l
  for _l in needs-human blocked wip; do
    if printf ',%s,' "$_labels_csv" | grep -q ",$_l,"; then
      _block_label="$_l"; break
    fi
  done

  local _blockers=()
  [ "$_pr_state" = "OPEN" ]      || _blockers+=("pr_state: PR state=$_pr_state (need OPEN)")
  [ "$_merge_state" = "CLEAN" ]  || _blockers+=("merge_state: mergeStateStatus=$_merge_state (need CLEAN)")
  if [[ "$verdict" =~ ^(APPROVE|AUTO_APPROVE)$ ]]; then
    :
  else
    _blockers+=("verdict: latest Code Review v2 verdict=${verdict:-none}")
  fi
  [ "${unresolved:-0}" = "0" ] || _blockers+=("unresolved_threads: $unresolved unresolved review thread(s)")
  [ -z "$_block_label" ]       || _blockers+=("labels: blocking label '$_block_label' on PR")

  # Bureau-enforced NRSR gates. Toggleable via .bureau.json.
  local _require_ci _require_uptodate
  _require_ci=$(bureau_get '.agents.merge_require_green_ci // true')
  _require_uptodate=$(bureau_get '.agents.merge_require_up_to_date // true')

  local _err
  if [ "$_require_ci" != "false" ]; then
    _err=$(pr_ci_is_green "$pr" 2>&1 >/dev/null) \
      || _blockers+=("ci_green: $_err")
  fi
  if [ "$_require_uptodate" != "false" ]; then
    _err=$(pr_base_is_current "$pr" 2>&1 >/dev/null) \
      || _blockers+=("base_current: $_err")
  fi

  if [ "${#_blockers[@]}" -gt 0 ]; then
    printf '%s\n' "${_blockers[@]}"
    return 1
  fi
  return 0
}

# Initial gate evaluation (renders report, may post blocker comment).
GATE_OUT=$(evaluate_merge_gates "$PR_NUMBER" || true)
echo ""
echo "  ── Gate report ──"
if [ -z "$GATE_OUT" ]; then
  echo "  all gates PASS"
else
  printf '  %s\n' "$GATE_OUT" | sed 's|^  \([a-z_]*\):|  \1:|'
fi

ELIGIBLE=true
BLOCKERS=()
if [ -n "$GATE_OUT" ]; then
  ELIGIBLE=false
  while IFS= read -r line; do
    # Strip the gate-name prefix; surface only the message for the PR comment.
    BLOCKERS+=("${line#*: }")
  done <<<"$GATE_OUT"
fi

if [ "$ELIGIBLE" = false ]; then
  BLOCKER_LINES=$(printf -- '- %s\n' "${BLOCKERS[@]}")
  echo ""
  echo "  NOT ELIGIBLE:"
  printf '    %s\n' "${BLOCKERS[@]}"

  # Kanban surfacing: if DIRTY is among the blockers AND the divergence is
  # bureau-only (rebase agent can safely auto-fix), apply `rebase-needed` so
  # operators glancing at the board see "wedged on rebase" vs "wedged on
  # review/CI". Routing is unaffected — rebase-pipeline still polls the
  # shared Merge state. The label is operator visibility, not control flow.
  if [ "$MERGE_STATE" = "DIRTY" ]; then
    git fetch origin --quiet 2>/dev/null || true
    if branch_is_bureau_only "$BRANCH"; then
      if [ "$DRY_RUN" = true ]; then
        echo "  [dry-run] would: add 'rebase-needed' label (bureau-only divergence)"
      else
        add_issue_label "$ISSUE" "rebase-needed" \
          || echo "  WARN: failed to add 'rebase-needed' label to $ISSUE" >&2
      fi
    else
      echo "  DIRTY but human commits in divergence — no label (existing comment is enough)."
    fi
  fi

  NEW_BODY="🛑 **Bureau merge gate** — PR #$PR_NUMBER is not eligible to merge.

$BLOCKER_LINES"

  if [ "$DRY_RUN" = true ]; then
    echo ""
    echo "  [dry-run] would post on PR #$PR_NUMBER (if blockers changed):"
    echo "$NEW_BODY" | sed 's/^/    /'
    exit 0
  fi

  # Idempotent commenting: only post if the blocker list differs from the most
  # recent "Bureau merge gate" comment on this PR. The bot reposts only when
  # something actionable has changed, so the PR doesn't get a comment per tick.
  # Use sed (not grep) for the line filter — sed exits 0 when no lines match,
  # grep exits 1 which would crash the substitution under `set -o pipefail`.
  LAST_BOT_BODY=$(gh pr view "$PR_NUMBER" --json comments \
    --jq '[.comments[] | select(.body | test("Bureau merge gate"))] | sort_by(.createdAt) | last | .body // ""')
  CURRENT_KEY=$(printf '%s' "$BLOCKER_LINES" | sort)
  LAST_KEY=$(printf '%s' "$LAST_BOT_BODY" | sed -n '/^- /p' | sort)

  if [ -n "$LAST_KEY" ] && [ "$CURRENT_KEY" = "$LAST_KEY" ]; then
    echo "  Blockers unchanged since last bot comment — skipping post."
  else
    gh pr comment "$PR_NUMBER" --body "$NEW_BODY" || true
    echo "  Posted blocker comment."
  fi
  exit 0
fi

# All gates pass — merge.
echo ""
echo "  ALL GATES PASS"

# Defensive: clear `rebase-needed` if a prior tick set it and the branch has
# since been rebased (CLEAN gate passing implies it's no longer DIRTY).
# remove_issue_label no-ops when the label isn't present.
if [ "$DRY_RUN" = true ]; then
  echo "  [dry-run] would: remove 'rebase-needed' label (defensive)"
else
  remove_issue_label "$ISSUE" "rebase-needed" 2>/dev/null || true
fi

if [ "$DRY_RUN" = true ]; then
  echo "  [dry-run] would run: gh pr merge $PR_NUMBER --$BUREAU_MERGE_STRATEGY"
  echo "  [dry-run] would move $ISSUE to Done."
  exit 0
fi

echo "  Merging PR #$PR_NUMBER (squash)..."
# Just-in-time gate recheck. Closes the race between the initial gate query
# (potentially seconds-to-minutes ago) and the merge call. Most importantly
# this re-checks pr_base_is_current — the prior tick's merge of a different
# PR may have advanced main, making this PR's base stale even though the
# initial pass was clean. If any gate has flipped, abort cleanly with exit 0
# so the next tick re-evaluates against fresh state.
JIT_GATE_OUT=$(evaluate_merge_gates "$PR_NUMBER" || true)
if [ -n "$JIT_GATE_OUT" ]; then
  echo "  Gate regressed between initial check and merge — aborting (will re-evaluate next tick):"
  printf '    %s\n' "$JIT_GATE_OUT"
  exit 0
fi

# No --delete-branch: same reason code-review-pipeline.sh dropped it (commit
# d812471). Inside .worktrees/queue-merge gh fails on detached HEAD or when
# main is held by the primary worktree. Remote branch deletion belongs to the
# repo setting `deleteBranchOnMerge: true`.
# No --auto: we want immediate merge (or immediate failure if a gate slipped
# between check and call). --auto would queue for later and silence the failure.
# Strategy is configurable via .agents.merge_strategy in .bureau.json (default
# squash). BUREAU_MERGE_STRATEGY is validated and clamped in bureau-config.sh.
if gh pr merge "$PR_NUMBER" "--$BUREAU_MERGE_STRATEGY"; then
  post_comment "$ISSUE" "✅ Merge gates passed. PR #$PR_NUMBER merged (\`--$BUREAU_MERGE_STRATEGY\`). Moving to Done."
  move_issue "$ISSUE" "$BUREAU_STATE_DONE"
  echo "  Merged. Issue moved to Done."
else
  echo "  Merge call failed."
  post_comment "$ISSUE" "❌ Merge attempted but \`gh pr merge\` failed despite gates passing. PR #$PR_NUMBER. Needs human."
  add_issue_label "$ISSUE" "needs-human" \
    || echo "  WARN: failed to add 'needs-human' label to $ISSUE; will retry on next tick" >&2
  exit 18
fi

echo ""
echo "═══════════════════════════════════════"
echo "  Merge complete: $ISSUE / PR #$PR_NUMBER"
echo "═══════════════════════════════════════"
