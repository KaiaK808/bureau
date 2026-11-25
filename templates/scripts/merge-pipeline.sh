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
#   5. The newest authorised exact-heading review is unedited and approves this head
#      and the current base branch (the reviewed base SHA may have advanced)
#   6. No `needs-human`, `blocked`, or `wip` label on the PR
#   7. Zero unresolved review threads (GraphQL: pullRequest.reviewThreads)
#   8. Explicitly enrolled charter: readable pinned default-branch policy;
#      approved Linear queue (exit 2); path/label exceptions and .bureau.json
#      changes require a human (exit 25). Suite, report and red-main gates follow.
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
# sanitised --subject/--body for squash and merge (merge-body.sh). Re-read the
# base immediately before the merge and require the approved Target. Deliberately
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
# threads, a PR that is not open). A stale approval instead returns the ticket
# to Build Review with exit 20 and a `refreshed` report, without needs-human.
# A shepherd that sets
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
# Charter enrollment is a separate maintainer command (bureau-charter.py),
# stored in the Git common dir. Checkout configuration cannot unenroll it.
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
# BUREAU_ENV_FILE only, never ./.env: in a stage worktree that is a file the branch controls.
if [ -f "$BUREAU_ENV_FILE" ]; then bureau_load_env --export "$BUREAU_ENV_FILE"
else echo "ERROR: No .env found"; exit 1; fi

bureau_secret_copy API_KEY LINEAR_API_KEY

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
    bureau_evidence evidence-pr --number "$MERGED_PR" || true
    bureau_evidence wait-resolve "$ISSUE" --terminal --basis authoritative-pr-merge --merged-at "$MERGED_AT" || true
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
if [ "$DRY_RUN" != true ]; then
  bureau_evidence evidence-pr --number "$PR_NUMBER" --url "$PR_URL" || true
  bureau_evidence wait-resolve "$ISSUE" --key merge-queue --basis observed-merge-stage-resume || true
fi
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

# Resolve publisher authority once from a pinned default-branch commit. The same
# snapshot is used by both evaluations, including the inline merge caller.
MERGE_PUBLISHER_POLICY=$(python3 -I -B "$(dirname "$0")/bureau-merge-review.py" policy "$(_bureau_gh_owner_repo)") \
  || MERGE_PUBLISHER_POLICY='{"kind":"read","message":"publisher policy could not be read"}'

# Probe only shared local state. Unenrolled installations do not load policy,
# contact a new service or create any charter files, even with an enabled block.
charter_enrolled() {
  # 0 enrolled, 1 confirmed absent; every other result is indeterminate.
  local state
  state=$(python3 -I -B "$(dirname "$0")/bureau-charter.py" --repo "$PWD" status 2>/dev/null) || return 25
  case "$state" in
    enrolled) return 0 ;;
    not-enrolled) return 1 ;;
    *) return 25 ;;
  esac
}
MERGE_CHARTER_PIN='{}'
MERGE_CHARTER_PROBE_RC=0
if charter_enrolled; then
  # Keep D across both evaluations; neither a JIT default-branch advance nor a
  # PR-controlled enabled:false can replace that authority mid-run.
  MERGE_CHARTER_PIN=$(python3 -I -B "$(dirname "$0")/bureau-charter.py" --repo "$PWD" \
    --github "$(_bureau_gh_owner_repo)" pin) || MERGE_CHARTER_PIN='{}'
else
  MERGE_CHARTER_PROBE_RC=$?
fi

# Keep the mismatch diagnostic identical across gate passes and the live base
# check so refresh_stale_approval recognizes an unchanged blocker.
approval_target_mismatch() {
  printf 'approved Target branch %s differs from current base branch %s (reviewed head %s)' "$1" "$2" "$3"
}

# evaluate_merge_gates: run every gate against fresh GitHub data. On success,
# print the approved Target branch. On failure, print the "gate: message" rows.
# Called twice: once at the top of the pipeline (to render the gate report
# and decide whether to attempt the merge), and again just before the merge
# call and its final live base check.
#
# Output rows are stable so the bot's idempotent-comment logic can diff them.
evaluate_merge_gates() {
  # <head-sha>: the head commit this evaluation is about; CI is judged on it and the
  # merge is pinned to it (--match-head-commit). An empty one cannot be pinned: not yet.
  local pr="$1" head="${2:-}"
  local _pr_data _pr_state _merge_state _labels_csv _base_ref _base_oid _pr_read=ok
  local _approved_base=""
  # A read that fails is not a verdict about the PR: it becomes a *_read line,
  # which merge_gate_outcome counts as "not yet", never as "PR not open" or
  # "no APPROVE".
  if ! _pr_data=$(gh pr view "$pr" --json state,mergeStateStatus,labels,baseRefName,baseRefOid 2>/dev/null) \
     || ! _pr_state=$(printf '%s' "$_pr_data" | jq -er '.state | strings'); then
    _pr_read=failed; _pr_data='{}'; _pr_state=""
  fi
  # A missing base name prevents binding and freshness checks. Keep the state,
  # merge status and labels so the gate can still report their blockers.
  _base_ref=$(printf '%s' "$_pr_data" | jq -er '.baseRefName | strings | select(length > 0)') || _base_ref=""
  _merge_state=$(printf '%s' "$_pr_data" | jq -r '.mergeStateStatus // ""' 2>/dev/null || true)
  _labels_csv=$(printf '%s' "$_pr_data" | jq -r '[.labels[]?.name] | join(",")' 2>/dev/null || true)

  local owner_repo unresolved verdict _review_body _review_result _review_kind _review_message _threads
  local _threads_read=ok
  owner_repo=$(_bureau_gh_owner_repo)
  _threads=$(python3 -I -B "$(dirname "$0")/bureau-merge-review.py" threads "$owner_repo" "$pr") || _threads='{}'
  unresolved=$(printf '%s' "$_threads" | jq -er 'select(.kind == "ok") | .unresolved') \
    || { _threads_read=failed; unresolved=0; }
  _review_result=$(python3 -I -B "$(dirname "$0")/bureau-merge-review.py" review "$owner_repo" "$pr" "$ISSUE" "$MERGE_PUBLISHER_POLICY") \
    || _review_result='{"kind":"read","message":"review snapshot could not be read"}'
  _review_kind=$(printf '%s' "$_review_result" | jq -r '.kind // "read"')
  _review_message=$(printf '%s' "$_review_result" | jq -r '.message // "review snapshot could not be read"')
  verdict=$(printf '%s' "$_review_result" | jq -r '.verdict // ""')
  _review_body=$(printf '%s' "$_review_result" | jq -r '.header // ""')

  local _block_label=""
  local _l
  for _l in needs-human blocked wip; do
    if grep -q ",$_l," <<< ",$_labels_csv,"; then
      _block_label="$_l"; break
    fi
  done

  local _blockers=()
  if [ "${MERGE_CHARTER_PROBE_RC:-1}" != 0 ] && [ "${MERGE_CHARTER_PROBE_RC:-1}" != 1 ]; then
    _blockers+=("charter-policy: enrollment state could not be confirmed; a maintainer must inspect shared Git state")
  elif charter_enrolled; then
    local _charter_lines _charter_rc=0
    _charter_lines=$(python3 -I -B "$(dirname "$0")/bureau-charter.py" --repo "$PWD" \
      --github "$owner_repo" gate --pin "$MERGE_CHARTER_PIN" --base "$_base_ref" --head "$head" --issue "$ISSUE") || _charter_rc=$?
    if [ "$_charter_rc" != 0 ]; then
      case "$_charter_lines" in
        charter-*:*) _blockers+=("$_charter_lines") ;;
        *) _blockers+=("charter-unreadable: enrolled policy helper failed") ;;
      esac
    fi
  else
    local _charter_probe_rc=$?
    if [ "$_charter_probe_rc" != 1 ]; then
      _blockers+=("charter-policy: enrollment state could not be confirmed; a maintainer must inspect shared Git state")
    fi
  fi
  [ -n "$head" ] || _blockers+=("head_read: the PR's head commit could not be read")
  if [ "$_pr_read" = failed ]; then
    _blockers+=("pr_read: the PR's state, mergeStateStatus and labels could not be read")
  else
    [ -n "$_base_ref" ] || _blockers+=("pr_read: the PR's base branch could not be read")
    [ "$_pr_state" = "OPEN" ]      || _blockers+=("pr_state: PR state=$_pr_state (need OPEN)")
    if [ "$_merge_state" = "DIRTY" ] && merge_dirty_is_rebasable; then
      _blockers+=("merge_state: mergeStateStatus=DIRTY (need CLEAN; bureau-only divergence, the rebase stage resolves it)")
    elif [ "$_merge_state" != "CLEAN" ]; then
      _blockers+=("merge_state: mergeStateStatus=$_merge_state (need CLEAN)")
    fi
    [ -z "$_block_label" ]       || _blockers+=("labels: hold label '$_block_label' on PR")
  fi
  if [ "$_review_kind" = read ]; then
    _blockers+=("verdict_read: $_review_message")
  elif [ "$_review_kind" = edited ]; then
    _blockers+=("approval_binding: $_review_message")
  elif [ "$_review_kind" != ok ]; then
    _blockers+=("verdict: $_review_message")
  elif [ -n "$head" ] && [ -n "$_base_ref" ]; then
    local _head_line _target_line _approved_head _binding_error=""
    local _head_pattern='^\*\*Reviewed head\*\*: `([0-9a-f]{40})`$'
    # Git branch names exclude ASCII spaces but can contain backticks. The
    # fixed SHA suffix identifies the closing backtick of the branch name.
    local _target_pattern='^\*\*Target\*\*: `([^ ]+)` at `([0-9a-f]{40})`$'
    # Whole-line matching rejects missing, malformed and duplicate lines,
    # including one valid line next to a second malformed candidate.
    _head_line=$(printf '%s\n' "$_review_body" | sed -n '/^\*\*Reviewed head\*\*/p')
    _target_line=$(printf '%s\n' "$_review_body" | sed -n '/^\*\*Target\*\*/p')
    if [[ "$_head_line" =~ $_head_pattern ]]; then
      _approved_head="${BASH_REMATCH[1]}"
    else
      _binding_error="Reviewed head line missing, malformed or duplicated (current head $head)"
    fi
    if [[ "$_target_line" != *$'\n'* && "$_target_line" =~ $_target_pattern ]]; then
      _approved_base="${BASH_REMATCH[1]}"
    else
      _binding_error="${_binding_error:+$_binding_error; }Target line missing, malformed or duplicated (current head $head, base $_base_ref)"
    fi
    if [ -z "$_binding_error" ]; then
      if [ "$_approved_head" != "$head" ]; then
        _binding_error="approved head $_approved_head differs from current head $head"
      elif [ "$_approved_base" != "$_base_ref" ]; then
        _binding_error=$(approval_target_mismatch "$_approved_base" "$_base_ref" "$head")
      fi
    fi
    [ -z "$_binding_error" ] || _blockers+=("approval_binding: $_binding_error; fresh Build Review required")
  fi
  if [ "$_review_kind" = ok ] && ! [[ "$verdict" =~ ^(APPROVE|AUTO_APPROVE)$ ]]; then
    _blockers+=("verdict: latest authorised Code Review v2 verdict=${verdict:-none}; check agents.merge_publishers")
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
  if [ "$_require_uptodate" != "false" ] && [ -n "$_base_ref" ]; then
    _base_oid=$(printf '%s' "$_pr_data" | jq -er '.baseRefOid | strings | select(length > 0)') || _base_oid=""
    _err=$(pr_base_is_current "$pr" "$_base_ref" "$_base_oid" 2>&1 >/dev/null) \
      || _blockers+=("base_current: $_err")
  fi

  if [ "${#_blockers[@]}" -gt 0 ]; then
    printf '%s\n' "${_blockers[@]}"
    return 1
  fi
  printf '%s\n' "$_approved_base"
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
      "charter-retry: "*|"charter-queue: "*) ;;
      "charter-policy: "*|"charter-unreadable: "*|"charter-unclassified: "*|"charter-exception: "*) outcome=blocked ;;
      "ci_green: ci: "*" still pending on "*) ;;
      "ci_green: ci: only "*" completed check(s) on "*) ;;
      "ci_green: ci: cannot resolve "*|"ci_green: ci: gh check-runs query failed"*) ;;
      "ci_green: ci: gh commit-status query failed for "*) ;;
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
  # Dedicated Merge workers need the same human handoff as inline review.
  # Keep this limited to charter exceptions; existing gate routing is unchanged.
  if [ "$code" = 25 ] && [ "${DRY_RUN:-false}" != true ] && [ "${BUREAU_INLINE_MERGE:-0}" != 1 ]; then
    case "$lines" in
      charter-*|*$'\n'charter-*) mark_needs_human "$ISSUE" merge 25 || true ;;
    esac
  fi
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
    python3 -I -B "$SCRIPT_REPO/scripts/bureau-supervision.py" --repo "$PWD" merge-wait "$ISSUE" --clear >/dev/null 2>&1 || true
    return 0
  fi
  [ -n "${GATE_HEAD:-}" ] || return 0
  python3 -I -B "$SCRIPT_REPO/scripts/bureau-supervision.py" --repo "$PWD" merge-wait "$ISSUE" \
      --branch "$BRANCH" --head "$GATE_HEAD" --outcome "$1" >/dev/null \
    || echo "  WARN: could not mark the merge gate wait of $ISSUE; the merge picker takes it again on its next poll" >&2
}

# A stale approval is ordinary fresh-review work, like our own base refresh.
# Reuse the gate comment key, so repeated runs at this head do not post again.
# Both gate passes and the final base re-read use this path.
# The final check runs in _merge_pr's if condition, which suspends errexit.
refresh_stale_approval() {
  local gates="$1" lines body last_body last_key current_key
  # A policy exception must not be softened into routine re-review (exit 20).
  case "$gates" in
    charter-policy:*|charter-unreadable:*|charter-unclassified:*|charter-exception:*|*$'\n'charter-policy:*|*$'\n'charter-unreadable:*|*$'\n'charter-unclassified:*|*$'\n'charter-exception:*) return 0 ;;
  esac
  lines=$(printf '%s\n' "$gates" | sed -n 's/^approval_binding: /- /p') || exit $?
  [ -n "$lines" ] || return 0
  printf '  %s\n' "$gates"
  if [ "$DRY_RUN" = true ]; then
    echo '  [dry-run] would return the ticket to Build Review for a fresh review (exit 20).'
    exit 0
  fi
  body="🛑 **Bureau merge gate** — PR #$PR_NUMBER needs a fresh Code Review.

Outcome: refreshed — fresh Build Review required

$lines

The approval does not bind the current PR head and target. Nothing was merged; a fresh review is needed."
  # A failed deduplication read can repeat the notice, but the handoff still runs.
  last_body=$(gh pr view "$PR_NUMBER" --json comments \
    --jq '[.comments[] | select(.body | test("Bureau merge gate"))] | sort_by(.createdAt) | last | .body // ""') || last_body=""
  last_key=$(merge_gate_key "$(sed -n 's/^Outcome: \([a-z-]*\).*/\1/p' <<< "$last_body" | sed -n 1p)" "$last_body") || exit $?
  current_key=$(merge_gate_key refreshed "$lines") || exit $?
  bureau_ticket_checkpoint "$ISSUE" strict
  move_issue "$ISSUE" "$BUREAU_STATE_BUILD_REVIEW" || exit $?
  if [ "$current_key" != "$last_key" ]; then
    gh pr comment "$PR_NUMBER" --body "$body" || true
  else
    echo '  Stale approval unchanged since last bot comment — skipping post.'
  fi
  merge_gate_mark clear
  if [ -n "${BUREAU_MERGE_GATE_REPORT:-}" ]; then
    printf 'refreshed\n%s\n' "$gates" > "$BUREAU_MERGE_GATE_REPORT" || exit $?
  fi
  echo '  Stale approval — fresh Build Review required (exit 20).'
  exit 20
}

# pr_head_sha <pr>: the PR's current head commit, or nothing when it cannot be read.
pr_head_sha() {
  local sha
  sha=$(gh pr view "$1" --json headRefOid --jq .headRefOid 2>/dev/null) || sha=""
  [ "$sha" = null ] && sha=""
  printf '%s' "$sha"
}

# Retain both existing gate evaluations, bound to the PR HEAD they checked.
# Observational only: their routing and the just-in-time merge gate stay intact.
merge_record_gate() {
  [ "$DRY_RUN" != true ] && [ -n "${BUREAU_RUN_ID:-}" ] && [ -n "${BUREAU_STAGE_ATTEMPT_ID:-}" ] || return 0
  local file rc=0 kind=gate
  file=$(mktemp) || { ( echo 'bureau: WARN: merge evidence log unavailable' >&2 ) || true; return 0; }
  # Bash can itself emit a redirection error. Contain its SIGPIPE as well as
  # printf's when the optional capture fails with a closed stderr reader.
  if ! ( printf '%s\n' "${2:-all gates PASS}" > "$file" ); then
    rm -f "$file" || true
    return 0
  fi
  if [ -n "$2" ]; then
    if [ "$(printf '%s\n' "$2" | merge_gate_outcome)" = not-yet ]; then rc=2; else rc=25; fi
    case "$2" in "approval_binding: "*|*$'\n'"approval_binding: "*) rc=20 ;; esac
    case "$2" in charter-policy:*|charter-unreadable:*|charter-unclassified:*|charter-exception:*|*$'\n'charter-policy:*|*$'\n'charter-unreadable:*|*$'\n'charter-unclassified:*|*$'\n'charter-exception:*) rc=25 ;; esac
  fi
  [[ "$1" =~ ^[0-9a-f]{40,64}$ ]] || kind=artifact
  bureau_evidence evidence-gate --kind "$kind" --path "$file" --head "$1" --end-head "$1" \
    --command "evaluate_merge_gates $PR_NUMBER $1" --exit-code "$rc" || true
  rm -f "$file" || true
}

# A dispatcher-held PR cannot use the independent queue rebaser. Merge forward
# in this owned worker, publish normally, and require a fresh review instead.
# At most two refreshes per lane; review round accounting is never reset.
refresh_owned_merge_base() {
  local gates="$1" row data remote_head base_ref base_head before refresh_rc=0
  [ "${BUREAU_DISPATCH_LANE:-0}" = 1 ] && [ "${BUREAU_HELD_BY_SHEPHERD:-0}" = 1 ] \
    && [ "$DRY_RUN" != true ] || return 0
  [ "$(bureau_number merge.refreshes "${BUREAU_LANE_BASE_REFRESHES:-0}" 2 0 2)" -lt 2 ] || return 0
  case "$gates" in *"base_current: base: PR "*|*"mergeStateStatus=DIRTY "*) ;; *) return 0 ;; esac
  # Existing security/review/hold/thread failures win over recovery work.
  while IFS= read -r row; do
    case "$row" in base_current:*|merge_state:*|ci_green:*|'') ;; *) return 0 ;; esac
  done <<< "$gates"
  bureau_ticket_checkpoint "$ISSUE" strict
  data=$(gh pr view "$PR_NUMBER" --json state,headRefOid,headRefName,baseRefName) || return 0
  [ "$(printf '%s' "$data" | jq -r '.state')" = OPEN ] || return 0
  [ "$(printf '%s' "$data" | jq -r '.headRefName')" = "$BRANCH" ] || return 0
  remote_head=$(printf '%s' "$data" | jq -er '.headRefOid') || return 0
  base_ref=$(printf '%s' "$data" | jq -er '.baseRefName') || return 0
  git check-ref-format "refs/heads/$base_ref" >/dev/null 2>&1 || return 0
  [ -z "$(git status --porcelain)" ] || return 0
  git fetch origin --quiet || exit 18
  [ "$(git rev-parse "refs/remotes/origin/$BRANCH")" = "$remote_head" ] \
    || merge_gate_exit not-yet 'head_read: the PR branch changed before base refresh'
  git merge-base --is-ancestor "$remote_head" HEAD || return 0
  base_head=$(git rev-parse "refs/remotes/origin/$base_ref^{commit}") || return 0
  git merge-base --is-ancestor "$base_head" "$remote_head" && return 0
  before=$(git rev-parse HEAD)
  merge_origin_main_or_abort "$ISSUE" 'Merge base refresh' "$base_head" || refresh_rc=$?
  case "$refresh_rc" in 10|16|18|21|25|26|27|130) exit "$refresh_rc" ;; esac
  bureau_ticket_checkpoint "$ISSUE" strict
  if [ "$refresh_rc" != 0 ]; then
    ( mark_needs_human "$ISSUE" merge 17 ) || true
    exit 17
  fi
  bureau_ticket_checkpoint "$ISSUE" strict
  git push origin "HEAD:refs/heads/$BRANCH" || exit 18
  bureau_ticket_checkpoint "$ISSUE" strict
  move_issue "$ISSUE" "$BUREAU_STATE_BUILD_REVIEW"
  post_comment "$ISSUE" "Merged the current PR base into the branch with a normal push. Nothing was merged to the base; the changed head requires fresh Build Review."
  if [ -n "${BUREAU_MERGE_GATE_REPORT:-}" ]; then
    printf 'refreshed\nbase_refresh: %s -> %s (base %s)\n' "$before" "$(git rev-parse HEAD)" "$base_head" > "$BUREAU_MERGE_GATE_REPORT"
  fi
  echo '  Base refreshed — fresh Build Review required (exit 20).'
  exit 20
}

# Initial gate evaluation (renders report, may post blocker comment).
GATE_HEAD=$(pr_head_sha "$PR_NUMBER")
if GATE_OUT=$(evaluate_merge_gates "$PR_NUMBER" "$GATE_HEAD"); then
  GATE_OUT=""
fi
merge_record_gate "$GATE_HEAD" "$GATE_OUT" || true
refresh_stale_approval "$GATE_OUT"
refresh_owned_merge_base "$GATE_OUT"
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
    bureau_ticket_checkpoint "$ISSUE"
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

# Prepare the merge message before the final gates, including with scanners off.
MERGE_MESSAGE_READY=false
prepare_merge_message() {
  [ "$MERGE_MESSAGE_READY" != true ] || return 0
  local _json _title _body
  _json=$(gh pr view "$PR_NUMBER" --json title,body) || return 1
  _title=$(printf '%s' "$_json" | jq -r '.title') || return 1
  _body=$(printf '%s' "$_json" | jq -j '.body // ""' && printf x) || return 1
  _body=${_body%x}
  MERGE_SUBJECT=$(sanitize_ci_markers "$_title")
  MERGE_BODY=$(build_merge_body "$_title" "$_body" && printf x) || return 1
  MERGE_BODY=${MERGE_BODY%x}
  MERGE_MESSAGE_READY=true
}
case "$BUREAU_MERGE_STRATEGY" in
  squash|merge)
    if ! prepare_merge_message; then
      post_comment "$ISSUE" "❌ PR #$PR_NUMBER merge message could not be read. Needs human."
      mark_needs_human "$ISSUE" merge 18 || true
      exit 18
    fi
    ;;
esac

# Scan, finish network preparation, then run the just-in-time gates. Only one
# cheap base-name/tip read is allowed between those gates and the pinned merge.
SCAN_RECHECK=false
SCAN_TOOLS=()
if [ "$(bureau_get 'has("review")')" = true ]; then
  source "$(dirname "$0")/bureau-semgrep.sh"
  SEMGREP_SETTINGS=$(bureau_semgrep settings) || exit 24
  if [ "$(printf '%s' "$SEMGREP_SETTINGS" | jq -r '.enabled and .mode == "blocking"')" = true ]; then SCAN_TOOLS+=(semgrep); fi
fi
if [ "$(bureau_get '.qa | type == "object" and has("trivy")')" = true ]; then
  source "$(dirname "$0")/bureau-trivy.sh"
  TRIVY_SETTINGS=$(bureau_trivy settings) || exit 24
  if [ "$(printf '%s' "$TRIVY_SETTINGS" | jq -r '.enabled and .mode == "blocking"')" = true ]; then SCAN_TOOLS+=(trivy); fi
fi
scanner_merge_retry() {
  GATE_HEAD="${_scan_head:-}"
  echo "  Gate regressed between initial check and merge — aborting (will re-evaluate next tick):"
  printf '    %s\n' "$1"
  merge_gate_exit not-yet "$1"
}
scanner_merge_hold() {
  GATE_HEAD="${_scan_head:-}"
  printf '    %s\n' "$1"
  merge_gate_exit blocked "$1"
}
for _scan_tool in "${SCAN_TOOLS[@]+"${SCAN_TOOLS[@]}"}"; do
  _scan_head=$(pr_head_sha "$PR_NUMBER")
  [ -n "$_scan_head" ] || scanner_merge_hold "$_scan_tool: current PR head unavailable"
  _scan_refs=$(gh pr view "$PR_NUMBER" --json baseRefName,headRefName) \
    || scanner_merge_hold "$_scan_tool: current PR base unavailable"
  _scan_base_ref=$(printf '%s' "$_scan_refs" | jq -er '.baseRefName | select(type == "string" and length > 0)') \
    || scanner_merge_hold "$_scan_tool: current PR base unavailable"
  if [[ "$_scan_base_ref" == -* ]] || ! git check-ref-format "refs/heads/$_scan_base_ref" >/dev/null 2>&1; then
    scanner_merge_hold "$_scan_tool: unsafe PR base ref"
  fi
  git fetch --no-tags origin "+refs/heads/$_scan_base_ref:refs/remotes/origin/$_scan_base_ref" \
    "+refs/heads/$BRANCH:refs/remotes/origin/$BRANCH" \
    || scanner_merge_hold "$_scan_tool: pinned source unavailable"
  _scan_base=$(git rev-parse --verify "refs/remotes/origin/$_scan_base_ref^{commit}") \
    || scanner_merge_hold "$_scan_tool: pinned base unavailable"
  if [ "$SCAN_RECHECK" = true ] && { [ "$_scan_head" != "$SCANNED_HEAD" ] || [ "$_scan_base_ref" != "$SCANNED_BASE_REF" ] || [ "$_scan_base" != "$SCANNED_BASE" ]; }; then
    scanner_merge_retry "$_scan_tool: scanner inputs changed between measurements"
  fi
  _scanner_rc=0
  SCANNER_RESULT=$("bureau_$_scan_tool" scan --base "$_scan_base" --head "$_scan_head" --pr "$PR_NUMBER" --reuse) || _scanner_rc=$?
  [ "$_scanner_rc" != 130 ] || exit 130

  # A stale failure must retry just like a stale pass: do not hold a fixed PR
  # for an older head's findings, unavailable tooling or failed receipt write.
  # This last fetch must precede the JIT gates, including their CI/hold reads.
  case "$BUREAU_MERGE_STRATEGY" in
    squash|merge) prepare_merge_message || scanner_merge_retry "$_scan_tool: merge message unavailable before gate recheck" ;;
  esac
  git fetch --no-tags origin "+refs/heads/$_scan_base_ref:refs/remotes/origin/$_scan_base_ref" \
    || scanner_merge_retry "$_scan_tool: current base unavailable after scan"
  _current_base=$(git rev-parse --verify "refs/remotes/origin/$_scan_base_ref^{commit}") \
    || scanner_merge_retry "$_scan_tool: current base commit unavailable after scan"
  [ "$_current_base" = "$_scan_base" ] \
    || scanner_merge_retry "$_scan_tool: PR base commit changed during scan"
  _current_head=$(pr_head_sha "$PR_NUMBER")
  [ "$_current_head" = "$_scan_head" ] \
    || scanner_merge_retry "$_scan_tool: PR head changed during scan"
  _current_base_ref=$(gh pr view "$PR_NUMBER" --json baseRefName --jq .baseRefName) \
    || scanner_merge_retry "$_scan_tool: current PR base unavailable after scan"
  [ "$_current_base_ref" = "$_scan_base_ref" ] \
    || scanner_merge_retry "$_scan_tool: PR base ref changed during scan"

  [ "$_scanner_rc" = 0 ] || scanner_merge_hold "$_scan_tool: durable scanner receipt unavailable"
  if [ "$(printf '%s' "$SCANNER_RESULT" | jq -r '.receipt.blocking')" != false ]; then
    scanner_merge_hold "$_scan_tool: $(printf '%s' "$SCANNER_RESULT" | jq -c '.projection')"
  fi
  SCAN_RECHECK=true
  SCANNED_HEAD="$_scan_head"
  SCANNED_BASE_REF="$_scan_base_ref"
  SCANNED_BASE="$_scan_base"
done

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
if JIT_GATE_OUT=$(evaluate_merge_gates "$PR_NUMBER" "$MERGE_HEAD"); then
  APPROVED_BASE="$JIT_GATE_OUT"
  JIT_GATE_OUT=""
fi
merge_record_gate "$MERGE_HEAD" "$JIT_GATE_OUT" || true
refresh_stale_approval "$JIT_GATE_OUT"
refresh_owned_merge_base "$JIT_GATE_OUT"
if [ -n "$JIT_GATE_OUT" ]; then
  echo "  Gate regressed between initial check and merge — aborting (will re-evaluate next tick):"
  printf '    %s\n' "$JIT_GATE_OUT"
  merge_gate_exit "$(printf '%s\n' "$JIT_GATE_OUT" | merge_gate_outcome)" "$JIT_GATE_OUT"
fi

# Blocking scans used the prepared message. Verify the checked head here and
# the scanned base in the same final read as the approval target check.
if [ "$SCAN_RECHECK" = true ]; then
  [ "$MERGE_HEAD" = "$SCANNED_HEAD" ] \
    || scanner_merge_retry "$_scan_tool: PR head changed during scan"
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
  local _merge_base_json _merge_base_ref _merge_base_oid _merge_output
  local message_args=()
  case "$BUREAU_MERGE_STRATEGY" in
    squash|merge)
      prepare_merge_message || return 1
      message_args=(--subject "$MERGE_SUBJECT" --body "$MERGE_BODY")
      ;;
  esac
  # GitHub pins the head, but offers no expected-base option.
  if [ "$SCAN_RECHECK" = true ]; then
    # baseRef.target is the live branch tip; the PR's stored baseRefOid can lag.
    _merge_base_json=$(gh api graphql \
      -f query='query($url:URI!){resource(url:$url){... on PullRequest{baseRefName baseRef{target{oid}}}}}' \
      -f url="$PR_URL") \
      || scanner_merge_retry "$_scan_tool: current PR base unavailable after gate recheck"
    _merge_base_ref=$(printf '%s' "$_merge_base_json" | jq -er 'select((.errors // []) == []) | .data.resource.baseRefName | select(type == "string" and length > 0)') \
      || scanner_merge_retry "$_scan_tool: current PR base ref unavailable after gate recheck"
  else
    if ! _merge_base_ref=$(gh pr view "$PR_NUMBER" --json baseRefName --jq '.baseRefName | strings | select(length > 0)' 2>/dev/null) \
       || [ -z "$_merge_base_ref" ]; then
      merge_gate_exit not-yet "pr_read: the PR's base branch could not be read before merge"
    fi
  fi
  if [ "$_merge_base_ref" != "$APPROVED_BASE" ]; then
    refresh_stale_approval "approval_binding: $(approval_target_mismatch "$APPROVED_BASE" "$_merge_base_ref" "$MERGE_HEAD"); fresh Build Review required"
  fi
  if [ "$SCAN_RECHECK" = true ]; then
    [ "$_merge_base_ref" = "$SCANNED_BASE_REF" ] \
      || scanner_merge_retry "$_scan_tool: PR base ref changed during scan or gate recheck"
    _merge_base_oid=$(printf '%s' "$_merge_base_json" | jq -er '.data.resource.baseRef.target.oid | select(type == "string" and test("^[0-9a-f]{40,64}$"))') \
      || scanner_merge_retry "$_scan_tool: current PR base commit unavailable after gate recheck"
    [ "$_merge_base_oid" = "$SCANNED_BASE" ] \
      || scanner_merge_retry "$_scan_tool: PR base commit changed during scan or gate recheck"
  fi
  bureau_ticket_checkpoint "$ISSUE"
  if _merge_output=$(gh pr merge "$PR_NUMBER" "--$BUREAU_MERGE_STRATEGY" --match-head-commit "$MERGE_HEAD" ${message_args[@]+"${message_args[@]}"} 2>&1); then
    [ -z "$_merge_output" ] || printf '%s\n' "$_merge_output"
    return 0
  fi
  [ -z "$_merge_output" ] || printf '%s\n' "$_merge_output" >&2
  # Only GitHub's own expected-head refusal proves this call did not merge.
  # A transport failure followed by a moved head can still be an accepted merge.
  case "$_merge_output" in
    'GraphQL: Head branch was modified. Review and try the merge again. (mergePullRequest)') MERGE_HEAD_REFUSED=true ;;
  esac
  return 1
}

# This is observational, not an atomic destination precondition. A retarget
# after the final read can still change the merge's destination. Read the full
# result twice if uncertain, including after a lost merge acknowledgement;
# never issue a second merge to reconcile it.
read_merge_result() {
  local result
  result=$(gh pr view "$PR_NUMBER" --json state,mergeCommit,baseRefName) || return 1
  printf '%s' "$result" | jq -e '
    (.state == "OPEN" or .state == "CLOSED" or .state == "MERGED") and
    (.baseRefName | type == "string" and length > 0) and
    (if .state == "MERGED" then (.mergeCommit.oid | strings | test("^[0-9a-f]{40,64}$")) else .mergeCommit == null end)
  ' >/dev/null || return 1
  MERGE_RESULT="$result"
}
merge_result_hold() {
  local message="merge_result: $1"
  echo "  ERROR: $message" >&2
  post_comment "$ISSUE" "🛑 PR #$PR_NUMBER merge outcome needs human reconciliation. $1 No rollback or automatic merge retry will run." || true
  mark_needs_human "$ISSUE" merge 25 || true
  merge_gate_exit blocked "$message"
}

bureau_stop_requested && exit 20
# The line names the head the merge is pinned to, so the log shows which commit
# the gates passed and GitHub was asked to merge (v3.1.0-rc.2).
echo "  Merging PR #$PR_NUMBER ($BUREAU_MERGE_STRATEGY) at $MERGE_HEAD..."
MERGE_CALL_OK=false
MERGE_HEAD_REFUSED=false
if _merge_pr; then MERGE_CALL_OK=true; fi
MERGE_RESULT=""
if ! read_merge_result || [ "$(printf '%s' "$MERGE_RESULT" | jq -r .state)" != MERGED ]; then
  echo '  WARN: merge result uncertain; reconciling with a fresh PR read' >&2
  read_merge_result || merge_result_hold "Merged state, merge commit and target could not be confirmed for approved Target \`$APPROVED_BASE\`. The merge may already have happened."
fi
if [ "$(printf '%s' "$MERGE_RESULT" | jq -r .state)" = MERGED ]; then
  MERGED_TARGET=$(printf '%s' "$MERGE_RESULT" | jq -r .baseRefName)
  MERGED_COMMIT=$(printf '%s' "$MERGE_RESULT" | jq -r .mergeCommit.oid)
  [ "$MERGED_TARGET" = "$APPROVED_BASE" ] \
    || merge_result_hold "PR merged as \`$MERGED_COMMIT\` into \`$MERGED_TARGET\`, differing from approved Target \`$APPROVED_BASE\`."
  merge_gate_mark clear
  post_comment "$ISSUE" "✅ Merge gates passed. PR #$PR_NUMBER merged (\`--$BUREAU_MERGE_STRATEGY\`). Moving to Done."
  move_issue "$ISSUE" "$BUREAU_STATE_DONE"
  echo "  Merged. Issue moved to Done."
else
  [ "$MERGE_CALL_OK" != true ] \
    || merge_result_hold "Merge call succeeded but PR is not confirmed merged into approved Target \`$APPROVED_BASE\`."
  if [ "$MERGE_HEAD_REFUSED" = true ]; then
    # This read is diagnostic only: the merge call's refusal determines exit 2,
    # even when the later head read is stale or unavailable.
    NOW_HEAD=$(pr_head_sha "$PR_NUMBER") || NOW_HEAD=""
    GATE_HEAD=""   # the new head has not been judged: no mark, the next poll takes it
    if [ -n "$NOW_HEAD" ] && [ "$NOW_HEAD" != "$MERGE_HEAD" ]; then
      echo "  The PR head moved from $MERGE_HEAD to $NOW_HEAD during the merge — nothing was merged."
      merge_gate_exit not-yet "merge_head: the PR head moved from $MERGE_HEAD to $NOW_HEAD after the gate checked it; GitHub refused --match-head-commit; nothing was merged"
    fi
    merge_gate_exit not-yet "merge_head: GitHub refused --match-head-commit $MERGE_HEAD because the PR head changed; nothing was merged"
  fi
  merge_result_hold "Merge call failed without a confirmed head-mismatch refusal, and PR is not confirmed merged into approved Target \`$APPROVED_BASE\`. The merge may already have happened."
fi

echo ""
echo "═══════════════════════════════════════"
echo "  Merge complete: $ISSUE / PR #$PR_NUMBER"
echo "═══════════════════════════════════════"
