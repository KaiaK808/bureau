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
# Only the JSON value false switches a gate off (merge_gate_required); any other
# value keeps it on with a warning. Leaving the defaults is strongly recommended;
# the toggles exist for repos without CI (docs-only) or with deliberate
# batch-merge workflows.
#
# When eligible, runs `gh pr merge N --$BUREAU_MERGE_STRATEGY` (squash by
# default; configurable via .agents.merge_strategy in .bureau.json), pinned with
# --match-head-commit to the head the just-in-time gate checked (a push in
# between makes GitHub refuse it; the stage then ends with 2), with a
# sanitised --subject/--body for squash and merge (merge-body.sh). Deliberately
# no --delete-branch and no --auto: see code-review-pipeline.sh:314-322 for the
# worktree/detached-HEAD rationale; --auto would queue the merge for later, we
# want loud immediate failure if a gate slipped between the check and the call.
#
# When NOT eligible, comments on the PR with the precise blocker — but only if
# the blocker has changed since the bot's last "Bureau merge gate" comment, so
# this script can run every poll interval without spamming — and tells its
# caller why it did not merge (merge_gate_outcome): `2` when the gates are not
# yet decided (checks pending or not started, GitHub still computing, a gate
# read that failed, a hold label a human put on the PR, conflicts the rebase
# stage resolves) and `25` when a gate is decided against the merge (a failing
# check, a check queued past agents.merge_ci_queued_grace_seconds that no runner
# took, conflicts nothing here resolves, a stale base, no APPROVE, unresolved
# threads, a PR that is not open). A shepherd that sets
# BUREAU_MERGE_GATE_REPORT gets the outcome and the gate lines in that file.
# The merge stage's own run (not the inline merge, not a dry run) also marks a gate
# that ended with 2 or 25 for the head it judged (merge_gate_mark, v3.2): while that
# head is unchanged the merge picker takes the other Merge tickets first.
# The inline merge from the review stage (BUREAU_INLINE_MERGE=1) ends with the
# same codes, and the review stage acts on them (code-review-pipeline.sh: Done
# only after a merge, needs-human on 25, a recorded approval and 2 on not yet).
# --dry-run keeps ending with 0.
#
# Opt-in via .bureau.json:
#   - agents.merge: true
#   - linear.teams[0].states.merge: "<uuid of the Merge workflow state>"
# Both must be set; missing either makes this pipeline a queue-empty no-op.
# agents.merge_mode: "manual" switches it off regardless of both (exit 2
# before anything else); a human merges.
#
# --dry-run: print the gate verdicts and the action that would be taken, but
# never call `gh pr merge` and never post comments. Use to audit before
# trusting the agent.

set -euo pipefail
unset CLAUDECODE 2>/dev/null || true

REPO_DIR="$(pwd)"
SCRIPT_REPO="$(cd "$(dirname "$0")/.." && pwd)"
source "$(dirname "$0")/bureau-config.sh"
# shellcheck source=templates/scripts/merge-body.sh
source "$(dirname "$0")/merge-body.sh"

# agents.merge_mode = manual: a human merges. Refuse before .env, Linear, gh or
# git, for the queue, a named ticket and the review stage's inline call alike.
# Exit 2 (queue-empty): the queue loop and the shepherd pass it quietly.
if bureau_merge_is_manual; then
  echo "merge-pipeline: agents.merge_mode is manual — a human merges this repo's PRs. Nothing to do."
  exit 2
fi

BUREAU_ENV_FILE="${BUREAU_ENV_FILE:-$SCRIPT_REPO/.env}"
# shellcheck disable=SC1090
if [ -f .env ]; then bureau_load_env --export .env
elif [ -f "$BUREAU_ENV_FILE" ]; then bureau_load_env --export "$BUREAU_ENV_FILE"
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

if bureau_stop_requested; then
  echo "Merge stopped by BUREAU_NO_MERGE"
  exit 20
fi
precondition_linear

if [ -z "${BUREAU_STATE_MERGE:-}" ] && [ "${BUREAU_INLINE_MERGE:-0}" != 1 ]; then
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

if [ "$DRY_RUN" = false ]; then bureau_stage_enter "$ISSUE" "$@"; fi

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
  # <head-sha>: the head commit this evaluation is about; CI is judged on it and the
  # merge is pinned to it (--match-head-commit). An empty one cannot be pinned: not yet.
  local pr="$1" head="${2:-}"
  local _pr_data _pr_state _merge_state _labels_csv _pr_read=ok
  # A read that fails is not a verdict about the PR: it becomes a *_read line,
  # which merge_gate_outcome counts as "not yet", never as "PR not open" or
  # "no APPROVE".
  if ! _pr_data=$(gh pr view "$pr" --json state,mergeStateStatus,labels 2>/dev/null) \
     || ! _pr_state=$(printf '%s' "$_pr_data" | jq -er '.state | strings') ; then
    _pr_read=failed; _pr_data='{}'; _pr_state=""
  fi
  _merge_state=$(printf '%s' "$_pr_data" | jq -r '.mergeStateStatus // ""' 2>/dev/null || true)
  _labels_csv=$(printf '%s' "$_pr_data" | jq -r '[.labels[]?.name] | join(",")' 2>/dev/null || true)

  local owner_repo owner repo unresolved verdict _review_body _threads_read=ok _verdict_read=ok
  owner_repo=$(_bureau_gh_owner_repo)
  owner="${owner_repo%/*}"
  repo="${owner_repo#*/}"
  # An unreadable thread list used to count as zero unresolved threads.
  if ! unresolved=$(gh api graphql \
      -f query='query($owner:String!,$repo:String!,$num:Int!){repository(owner:$owner,name:$repo){pullRequest(number:$num){reviewThreads(first:100){nodes{isResolved}}}}}' \
      -f owner="$owner" -f repo="$repo" -F num="$pr" 2>/dev/null \
      | jq -e '.data.repository.pullRequest.reviewThreads.nodes | [.[] | select(.isResolved == false)] | length' 2>/dev/null); then
    _threads_read=failed; unresolved=0
  fi
  if _review_body=$(gh pr view "$pr" --json comments \
      --jq '[.comments[] | select(.body | test("Code Review v2"))] | sort_by(.createdAt) | last | .body // ""' 2>/dev/null); then
    verdict=$(printf '%s\n' "$_review_body" \
      | grep -oE '\*\*Verdict\*\*[[:space:]]*:[[:space:]]*[A-Z_]+' \
      | grep -oE 'APPROVE|AUTO_APPROVE|REQUEST_CHANGES|BLOCK' \
      | sed -n 1p || true)
  else
    _verdict_read=failed; verdict=""
  fi

  local _block_label=""
  local _l
  for _l in needs-human blocked wip; do
    if grep -q ",$_l," <<< ",$_labels_csv,"; then
      _block_label="$_l"; break
    fi
  done

  local _blockers=()
  [ -n "$head" ] || _blockers+=("head_read: the PR's head commit could not be read")
  if [ "$_pr_read" = failed ]; then
    _blockers+=("pr_read: the PR's state, mergeStateStatus and labels could not be read")
  else
    [ "$_pr_state" = "OPEN" ]      || _blockers+=("pr_state: PR state=$_pr_state (need OPEN)")
    if [ "$_merge_state" = "DIRTY" ] && merge_dirty_is_rebasable; then
      _blockers+=("merge_state: mergeStateStatus=DIRTY (need CLEAN; bureau-only divergence, the rebase stage resolves it)")
    elif [ "$_merge_state" != "CLEAN" ]; then
      _blockers+=("merge_state: mergeStateStatus=$_merge_state (need CLEAN)")
    fi
    [ -z "$_block_label" ]       || _blockers+=("labels: hold label '$_block_label' on PR")
  fi
  if [ "$_verdict_read" = failed ]; then
    _blockers+=("verdict_read: the PR's review comments could not be read")
  elif ! [[ "$verdict" =~ ^(APPROVE|AUTO_APPROVE)$ ]]; then
    _blockers+=("verdict: latest Code Review v2 verdict=${verdict:-none}")
  fi
  if [ "$_threads_read" = failed ]; then
    _blockers+=("threads_read: the PR's review threads could not be read")
  elif [ "${unresolved:-0}" != "0" ]; then
    _blockers+=("unresolved_threads: $unresolved unresolved review thread(s)")
  fi

  # Bureau-enforced NRSR gates. Toggleable via .bureau.json (merge_gate_required).
  local _require_ci _require_uptodate
  _require_ci=$(merge_gate_required merge_require_green_ci)
  _require_uptodate=$(merge_gate_required merge_require_up_to_date)

  local _err _key _used
  if [ "$_require_ci" != "false" ]; then
    # Its numbers are read by one rule (_merge_gate_number in bureau-config.sh); say
    # here when a value was not a plain whole number, since pr_ci_is_green's own
    # stderr is its gate line.
    for _key in merge_min_required_checks:1 merge_ci_start_grace_seconds:1800 merge_ci_queued_grace_seconds:3600; do
      _used=$(_merge_gate_number "${_key%%:*}" "${_key#*:}") \
        || echo "  WARN: agents.${_key%%:*} should be a whole number of at least 0; using $_used" >&2
    done
    _err=$(pr_ci_is_green "$pr" "$head" 2>&1 >/dev/null) \
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

# merge_gate_required <agents key>: prints false when .agents.<key> is the JSON
# value false, true otherwise. `jq '.x // true'` used to turn a configured false
# into true, so the documented opt-out never switched a gate off. Absent or null
# keeps the gate; any other value (the string "false", 0, "no") keeps it too and
# warns on stderr: a typo must not open a merge gate.
merge_gate_required() {
  local key="$1" value
  value=$(bureau_get ".agents.$key | if . == null then \"true\" elif type == \"boolean\" then tostring else \"invalid\" end" 2>/dev/null) \
    || value=invalid
  case "$value" in
    true|false) printf '%s\n' "$value" ;;
    *) echo "  WARN: agents.$key must be true or false; keeping the gate on" >&2
       printf 'true\n' ;;
  esac
}

# merge_dirty_is_rebasable: 0 when a DIRTY PR is one the rebase stage resolves on
# its own — the rebase agent is on in .bureau.json (read without the shepherd's
# BUREAU_FORCE_ALL_AGENTS) and the divergence is bureau-only (the rebase stage
# refuses human commits). Never while a shepherd holds the ticket
# (BUREAU_HELD_BY_SHEPHERD=1, which shepherd.sh sets for the stages it runs): the
# queue's rebase picker skips a ticket labeled shepherd-focused and the shepherd
# runs only the merge stage at Merge, so nothing would rebase it — waiting would
# only use up the shepherd's budget.
merge_dirty_is_rebasable() {
  [ "${BUREAU_HELD_BY_SHEPHERD:-0}" = 1 ] && return 1
  BUREAU_FORCE_ALL_AGENTS=0 agent_enabled rebase || return 1
  git fetch origin --quiet 2>/dev/null || true
  branch_is_bureau_only "$BRANCH"
}

# merge_gate_outcome: reads gate lines ("gate: message") on stdin and prints
# "not-yet" when every blocker can clear without anyone deciding against the
# merge — checks still pending or not started, GitHub still computing
# mergeStateStatus, a gate read that failed, a hold label a human put on the PR
# (it stays until they remove it; the queue does not alert on it), conflicts the
# rebase stage resolves — and "blocked" as soon as one blocker needs someone to
# act. mergeStateStatus BLOCKED/UNSTABLE count as "not yet": they also show
# pending checks, and a failing check is decided by its own ci_green line. So is a
# check queued longer than agents.merge_ci_queued_grace_seconds ("ci: check <name>
# queued for N s on … — runner offline?", v3.2): none of the patterns below matches
# it, and it is blocked.
merge_gate_outcome() {
  local line outcome=not-yet
  while IFS= read -r line; do
    [ -n "$line" ] || continue
    case "$line" in
      "ci_green: ci: "*" still pending on "*) ;;
      "ci_green: ci: only "*" completed check(s) on "*) ;;
      "ci_green: ci: cannot resolve "*|"ci_green: ci: gh check-runs query failed"*) ;;
      "merge_state: mergeStateStatus= "*|"merge_state: mergeStateStatus=UNKNOWN "*) ;;
      "merge_state: mergeStateStatus=BLOCKED "*|"merge_state: mergeStateStatus=UNSTABLE "*) ;;
      "merge_state: mergeStateStatus=DIRTY (need CLEAN; bureau-only divergence, the rebase stage resolves it)") ;;
      "labels: hold label "*) ;;
      "pr_read: "*|"verdict_read: "*|"threads_read: "*|"head_read: "*) ;;
      "base_current: base: cannot resolve "*) ;;
      *) outcome=blocked ;;
    esac
  done
  printf '%s\n' "$outcome"
}

# merge_gate_key: the idempotent-comment key — the outcome and the blocker lines
# with the counts of running or completed checks replaced, so a PR gets a new
# gate comment when the outcome or a blocker changes, not when one more check
# finished. The age of a check queued past its grace and the number of others
# queued with it are replaced too (v3.2): the merge stage polls a blocked head on
# every run, and the age grows each time.
merge_gate_key() {
  printf 'Outcome: %s\n' "$1"
  printf '%s\n' "$2" | sed -n '/^- /p' \
    | sed -E -e 's/[0-9]+ check\(s\) still pending/N check(s) still pending/' \
             -e 's/only [0-9]+ completed check\(s\)/only N completed check(s)/' \
             -e 's/ queued for [0-9]+ s on / queued for N s on /' \
             -e 's/, [0-9]+ more check\(s\) as well — runner offline\?$/, N more check(s) as well — runner offline?/' | sort
}

# merge_gate_exit <outcome> <gate lines>: records the outcome for a caller that
# asked for it (BUREAU_MERGE_GATE_REPORT) and ends the run — 2 not yet, 25
# blocked. The review stage's inline merge ends the same way; it used to end
# with 0 here, and the review then reported Done for a PR it had not merged.
merge_gate_exit() {
  local outcome="$1" lines="$2" code=25
  [ "$outcome" = not-yet ] && code=2
  merge_gate_mark "$outcome"
  if [ -n "${BUREAU_MERGE_GATE_REPORT:-}" ]; then
    printf '%s\n%s\n' "$outcome" "$lines" > "$BUREAU_MERGE_GATE_REPORT" 2>/dev/null \
      || echo "  WARN: could not write the gate report to $BUREAU_MERGE_GATE_REPORT" >&2
  fi
  echo "  Gate outcome: $outcome — exit $code"
  exit "$code"
}

# merge_gate_mark <not-yet|blocked> / merge_gate_mark clear (v3.2): the merge stage's own
# run marks a gate that was not yet decided or blocked, for the head it judged
# (GATE_HEAD), so the merge picker puts the ticket after every other Merge ticket while
# that head is unchanged (merge_gate_waits in bureau-config.sh). Before, the stage sets
# no needs-human on either outcome and the picker handed out the same ticket on every
# poll. A merge clears the mark. Not for the review stage's inline merge (the review
# keeps its own record) and not in a dry run. Best effort: a mark that cannot be
# written only means the picker takes the ticket again on its next poll.
merge_gate_mark() {
  [ "${BUREAU_INLINE_MERGE:-0}" != 1 ] && [ "$DRY_RUN" != true ] || return 0
  if [ "$1" = clear ]; then
    python3 -I "$SCRIPT_REPO/scripts/bureau-supervision.py" --repo "$PWD" merge-wait "$ISSUE" --clear >/dev/null 2>&1 || true
    return 0
  fi
  [ -n "${GATE_HEAD:-}" ] || return 0
  python3 -I "$SCRIPT_REPO/scripts/bureau-supervision.py" --repo "$PWD" merge-wait "$ISSUE" \
      --branch "$BRANCH" --head "$GATE_HEAD" --outcome "$1" >/dev/null \
    || echo "  WARN: could not mark the merge gate wait of $ISSUE; the merge picker takes it again on its next poll" >&2
}

# pr_head_sha <pr>: the PR's current head commit, or nothing when it cannot be read.
pr_head_sha() {
  local sha
  sha=$(gh pr view "$1" --json headRefOid --jq .headRefOid 2>/dev/null) || sha=""
  [ "$sha" = null ] && sha=""
  printf '%s' "$sha"
}

# Initial gate evaluation (renders report, may post blocker comment).
GATE_HEAD=$(pr_head_sha "$PR_NUMBER")
GATE_OUT=$(evaluate_merge_gates "$PR_NUMBER" "$GATE_HEAD" || true)
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

  GATE_OUTCOME=$(printf '%s\n' "$GATE_OUT" | merge_gate_outcome)
  case "$GATE_OUTCOME" in
    not-yet) OUTCOME_TEXT="not yet — the merge stage checks again on its next run" ;;
    *)       OUTCOME_TEXT="blocked — needs someone to act" ;;
  esac
  NEW_BODY="🛑 **Bureau merge gate** — PR #$PR_NUMBER is not eligible to merge.

Outcome: $OUTCOME_TEXT

$BLOCKER_LINES"

  if [ "$DRY_RUN" = true ]; then
    echo ""
    echo "  [dry-run] would post on PR #$PR_NUMBER (if blockers changed):"
    echo "$NEW_BODY" | sed 's/^/    /'
    echo "  [dry-run] gate outcome: $GATE_OUTCOME (a real run ends with 2 for not-yet, 25 for blocked)"
    exit 0
  fi

  # Idempotent commenting: only post if the outcome or the blocker list differs
  # from the most recent "Bureau merge gate" comment on this PR (merge_gate_key:
  # the number of running checks alone is no change). The bot reposts only when
  # something actionable has changed, so the PR doesn't get a comment per tick.
  # Use sed (not grep) for the line filter — sed exits 0 when no lines match,
  # grep exits 1 which would crash the substitution under `set -o pipefail`.
  # An unreadable comment list posts again rather than ending the stage.
  LAST_BOT_BODY=$(gh pr view "$PR_NUMBER" --json comments \
    --jq '[.comments[] | select(.body | test("Bureau merge gate"))] | sort_by(.createdAt) | last | .body // ""') || LAST_BOT_BODY=""
  case "$(sed -n 's/^Outcome: \([a-z]*\).*/\1/p' <<< "$LAST_BOT_BODY" | sed -n 1p)" in
    not) LAST_OUTCOME=not-yet ;;
    blocked) LAST_OUTCOME=blocked ;;
    *) LAST_OUTCOME="" ;;
  esac
  CURRENT_KEY=$(merge_gate_key "$GATE_OUTCOME" "$BLOCKER_LINES")
  LAST_KEY=$(merge_gate_key "$LAST_OUTCOME" "$LAST_BOT_BODY")

  if [ -n "$LAST_BOT_BODY" ] && [ "$CURRENT_KEY" = "$LAST_KEY" ]; then
    echo "  Blockers unchanged since last bot comment — skipping post."
  else
    gh pr comment "$PR_NUMBER" --body "$NEW_BODY" || true
    echo "  Posted blocker comment."
  fi
  merge_gate_exit "$GATE_OUTCOME" "$GATE_OUT"
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
  # Show the subject and body the real merge would set, so a dry run can audit
  # them. Informational only: a failed read here does not end the dry run.
  case "$BUREAU_MERGE_STRATEGY" in
    squash|merge)
      if _dry_json=$(gh pr view "$PR_NUMBER" --json title,body 2>/dev/null) \
         && _dry_title=$(printf '%s' "$_dry_json" | jq -r '.title') \
         && _dry_body=$(printf '%s' "$_dry_json" | jq -r '.body // ""'); then
        echo "  [dry-run] merge subject: $(sanitize_ci_markers "$_dry_title")"
        echo "  [dry-run] merge body:    $(build_merge_body "$_dry_title" "$_dry_body")"
      else
        echo "  [dry-run] could not read PR #$PR_NUMBER title/body for the merge message preview"
      fi
      ;;
  esac
  echo "  [dry-run] would move $ISSUE to Done."
  exit 0
fi

# Just-in-time gate recheck. Closes the race between the initial gate query
# (potentially seconds-to-minutes ago) and the merge call. Most importantly
# this re-checks pr_base_is_current — the prior tick's merge of a different
# PR may have advanced main, making this PR's base stale even though the
# initial pass was clean. If any gate has flipped, abort without merging and
# report the outcome of the recheck like the initial gate does (2 or 25), so
# the next tick re-evaluates against fresh state. The head it judges is the one the
# merge is pinned to (--match-head-commit): a push between this check and the merge
# call makes GitHub refuse the merge instead of merging a head no gate has seen.
MERGE_HEAD=$(pr_head_sha "$PR_NUMBER")
GATE_HEAD="$MERGE_HEAD"
JIT_GATE_OUT=$(evaluate_merge_gates "$PR_NUMBER" "$MERGE_HEAD" || true)
if [ -n "$JIT_GATE_OUT" ]; then
  echo "  Gate regressed between initial check and merge — aborting (will re-evaluate next tick):"
  printf '    %s\n' "$JIT_GATE_OUT"
  merge_gate_exit "$(printf '%s\n' "$JIT_GATE_OUT" | merge_gate_outcome)" "$JIT_GATE_OUT"
fi

# No --delete-branch: same reason code-review-pipeline.sh dropped it (commit
# d812471). Inside .worktrees/queue-merge gh fails on detached HEAD or when
# main is held by the primary worktree. Remote branch deletion belongs to the
# repo setting `deleteBranchOnMerge: true`.
# No --auto: we want immediate merge (or immediate failure if a gate slipped
# between check and call). --auto would queue for later and silence the failure.
# Strategy is configurable via .agents.merge_strategy in .bureau.json (default
# squash). BUREAU_MERGE_STRATEGY is validated and clamped in bureau-config.sh.
#
# Subject and body are set explicitly for squash and merge (merge-body.sh):
# without --body GitHub composes the message from the branch's commit list, and
# a CI suppressor anywhere in it stops the run on main. Rebase writes no merge
# commit and gh takes no --body for it, so it merges plain — outside this
# guarantee; check_squash_range is the layer that covers it.
#
# Runs as an `if` condition, where `set -e` is suspended inside the function,
# so every read carries its own `|| return 1`: a failed `gh pr view` or `jq`
# goes to the needs-human branch below instead of falling back to GitHub's
# default message. The body is captured with a sentinel (`&& printf x`, then
# `${…%x}`) so its trailing newlines survive command substitution while the
# producer's exit code still propagates.
_merge_pr() {
  case "$BUREAU_MERGE_STRATEGY" in
    squash|merge)
      local _json _title _body _subject _mbody
      _json=$(gh pr view "$PR_NUMBER" --json title,body) || return 1
      _title=$(printf '%s' "$_json" | jq -r '.title') || return 1
      _body=$(printf '%s' "$_json" | jq -j '.body // ""' && printf x) || return 1
      _body=${_body%x}
      _subject=$(sanitize_ci_markers "$_title")
      _mbody=$(build_merge_body "$_title" "$_body" && printf x) || return 1
      _mbody=${_mbody%x}
      gh pr merge "$PR_NUMBER" "--$BUREAU_MERGE_STRATEGY" --match-head-commit "$MERGE_HEAD" --subject "$_subject" --body "$_mbody"
      ;;
    *)
      gh pr merge "$PR_NUMBER" "--$BUREAU_MERGE_STRATEGY" --match-head-commit "$MERGE_HEAD"
      ;;
  esac
}

bureau_stop_requested && exit 20
# The line names the head the merge is pinned to, so the log shows which commit
# the gates passed and GitHub was asked to merge (v3.1.0-rc.2).
echo "  Merging PR #$PR_NUMBER ($BUREAU_MERGE_STRATEGY) at $MERGE_HEAD..."
if _merge_pr; then
  merge_gate_mark clear
  post_comment "$ISSUE" "✅ Merge gates passed. PR #$PR_NUMBER merged (\`--$BUREAU_MERGE_STRATEGY\`). Moving to Done."
  move_issue "$ISSUE" "$BUREAU_STATE_DONE"
  echo "  Merged. Issue moved to Done."
else
  # A head that moved since the gate checked it: GitHub refused the pinned merge. The
  # new head has not been through the gate: not yet (2), never merged, never blocked.
  NOW_HEAD=$(pr_head_sha "$PR_NUMBER")
  if [ -n "$NOW_HEAD" ] && [ "$NOW_HEAD" != "$MERGE_HEAD" ]; then
    echo "  The PR head moved from $MERGE_HEAD to $NOW_HEAD during the merge — nothing was merged."
    GATE_HEAD=""   # the new head has not been judged: no mark, the next poll takes it
    merge_gate_exit not-yet "merge_head: the PR head moved from $MERGE_HEAD to $NOW_HEAD after the gate checked it; nothing was merged"
  fi
  echo "  Merge call failed."
  post_comment "$ISSUE" "❌ Merge attempted but \`gh pr merge\` (or its title/body read) failed despite gates passing. PR #$PR_NUMBER. Needs human."
  mark_needs_human "$ISSUE" merge 18 || true
  exit 18
fi

echo ""
echo "═══════════════════════════════════════"
echo "  Merge complete: $ISSUE / PR #$PR_NUMBER"
echo "═══════════════════════════════════════"
