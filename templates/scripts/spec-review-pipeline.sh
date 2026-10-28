#!/bin/bash
# Spec Review pipeline: Pick Spec Review issue → validate → route to Build/Design
set -euo pipefail

unset CLAUDECODE 2>/dev/null || true

REPO_DIR="$(pwd)"
SCRIPT_REPO="$(cd "$(dirname "$0")/.." && pwd)"
source "$(dirname "$0")/bureau-config.sh"

BUREAU_ENV_FILE="${BUREAU_ENV_FILE:-$SCRIPT_REPO/.env}"
# BUREAU_ENV_FILE only, never ./.env: in a stage worktree that is a file the branch controls.
if [ -f "$BUREAU_ENV_FILE" ]; then bureau_load_env --export "$BUREAU_ENV_FILE"
else bureau_secret_set LINEAR_API_KEY || { echo "ERROR: Set LINEAR_API_KEY"; exit 1; }; fi

CLAUDE=(run_stage_for spec_review)
bureau_secret_copy API_KEY LINEAR_API_KEY

precondition_linear
precondition_runner spec_review

if [ -n "${1:-}" ]; then
  ISSUE="$1"
  echo "Using specified issue: $ISSUE"
else
  echo "Picking next Spec Review issue..."
  ISSUE=$(pipeline_pick_next "$(basename "$0")")

  if [ -z "$ISSUE" ] || [[ ! "$ISSUE" =~ ^[A-Z]+-[0-9]+$ ]]; then
    echo "No qualifying issues found. Queue empty."
    exit 2
  fi
  echo "Picked: $ISSUE"
fi

# State guard runs unconditionally — see implement-pipeline.sh for rationale.
bureau_stage_enter "$ISSUE" "$@"

ACTUAL_STATE=$(get_issue_state "$ISSUE")
bureau_ticket_checkpoint "$ISSUE" latched
if [ "$ACTUAL_STATE" != "Spec Review" ]; then
  echo "  WARNING: $ISSUE is in '$ACTUAL_STATE', not 'Spec Review'. Skipping."
  exit 2
fi

echo ""
echo "═══════════════════════════════════════"
echo "  Spec Review Pipeline: $ISSUE"
echo "═══════════════════════════════════════"
echo ""

echo "→ Fetching issue details..."
ISSUE_DETAIL=$(get_issue_detail "$ISSUE")
ISSUE_TITLE=$(echo "$ISSUE_DETAIL" | jq -r '.title // empty')
ISSUE_DESC=$(echo "$ISSUE_DETAIL" | jq -r '.description // empty')
PROJECT_NAME=$(echo "$ISSUE_DETAIL" | jq -r '.project.name // empty')
echo "  $ISSUE: $ISSUE_TITLE"

echo "→ Finding spec branch..."
BRANCH=$(get_issue_branch "$ISSUE")

if [ -z "$BRANCH" ] || [[ "$BRANCH" == *" "* ]]; then
  echo "  ERROR: No spec branch found for $ISSUE."
  post_comment "$ISSUE" "❌ Spec review aborted — no bureau-branch marker on this issue. Routing back to Triage so spec pipeline can produce a branch."
  move_issue "$ISSUE" "$BUREAU_STATE_TRIAGE"
  exit 12
fi

echo "  Branch: $BRANCH"
git fetch origin

# Fail loud: branch must exist. No silent fresh-from-main fallback.
# Also detach any other worktree that currently holds $BRANCH — two worktrees
# can't hold the same branch, and this pipeline may run back-to-back with
# spec-pipeline on the same branch.
free_branch_from_other_worktrees "$BRANCH" "$(pwd)"
if git rev-parse --verify "origin/$BRANCH" >/dev/null 2>&1; then
  git checkout -B "$BRANCH" "origin/$BRANCH"
elif git rev-parse --verify "$BRANCH" >/dev/null 2>&1; then
  git checkout "$BRANCH"
else
  echo "  ERROR: branch '$BRANCH' not found locally or on origin."
  post_comment "$ISSUE" "❌ Spec review aborted — bureau-branch marker points at \`$BRANCH\` but the branch does not exist. Routing back to Triage."
  move_issue "$ISSUE" "$BUREAU_STATE_TRIAGE"
  exit 12
fi

# Only readiness bound to the current validated contract skips another review.
if bureau_overlap_resume "$ISSUE" "$BRANCH" spec_review; then exit 0
else OVERLAP_RC=$?; [ "$OVERLAP_RC" = 2 ] || exit "$OVERLAP_RC"; fi

echo "→ Locating spec artifacts..."
# No "newest directory" fallback: in a fresh worktree every directory has the
# same age, so it picked another ticket's spec. No match aborts below.
SPEC_DIR=$(bureau_spec_dir_for_branch "$BRANCH")

SPEC_CANDIDATES=$(bureau_spec_dir_candidates "$BRANCH")
if [ -z "$SPEC_DIR" ] && [ -n "$SPEC_CANDIDATES" ]; then
  echo "  ERROR: branch '$BRANCH' fits more than one spec directory: $SPEC_CANDIDATES"
  post_comment "$ISSUE" "❌ Spec review aborted — branch \`$BRANCH\` fits more than one spec directory ($SPEC_CANDIDATES), and the stages do not guess. Rename or remove the stray directory so that exactly one matches the branch (troubleshooting: exit 13), then remove \`needs-human\`. Moving back to Spec."
  move_issue "$ISSUE" "$BUREAU_STATE_SPEC"
  mark_needs_human "$ISSUE" spec_review 13 || true
  exit 13
fi
# No directory matches the branch at all: a name mismatch that re-running Spec
# does not repair by itself, so a human decides (needs-human). A matched
# directory without tasks.md goes back to Spec for /speckit-tasks, unlabelled.
if [ -z "$SPEC_DIR" ]; then
  echo "  ERROR: no spec directory under ${BUREAU_SPECS_DIR%/}/ matches branch '$BRANCH'."
  post_comment "$ISSUE" "❌ Spec review aborted — no spec directory under \`${BUREAU_SPECS_DIR%/}/\` matches branch \`$BRANCH\`, and the stages do not guess. Rename the spec directory or the branch so that they match, or send the ticket back to Triage for a fresh spec (troubleshooting: exit 13), then remove \`needs-human\`. Moving back to Spec."
  move_issue "$ISSUE" "$BUREAU_STATE_SPEC"
  mark_needs_human "$ISSUE" spec_review 13 || true
  exit 13
fi
if [ ! -f "${SPEC_DIR}tasks.md" ]; then
  bureau_without_secrets python3 -I "$SCRIPT_REPO/scripts/bureau-spec.py" --repo "$REPO_DIR" invalidate "$ISSUE" tasks --reason missing-tasks \
    || echo 'WARN: checkpoint invalidation unavailable; missing tasks still require re-work.' >&2
  echo "  ERROR: No spec artifacts found"
  post_comment "$ISSUE" "❌ Spec review aborted — no tasks.md found on branch \`$BRANCH\`. Moving back to Spec for re-work."
  move_issue "$ISSUE" "$BUREAU_STATE_SPEC"
  exit 13
fi
echo "  Spec dir: $SPEC_DIR"

echo ""
echo "Phase 1/2: review specs against codebase"

SPEC_CONTEXT=$(build_spec_context "$SPEC_DIR")

invalid_review_result() {
  bureau_ticket_checkpoint "$ISSUE"
  ( mark_needs_human "$ISSUE" spec_review 22 technical 'Invalid spec review result; inspect provider evidence and preserved edits' maintainer ) || true
  bureau_ticket_checkpoint "$ISSUE" latched
  bureau_outcome_signal human_hold || true
  echo "  Invalid spec review result — work preserved; human review required." >&2
  exit 22
}

REVIEW_RC=0
REVIEW_RESULT=$("${CLAUDE[@]}" --schema "$SCRIPT_REPO/scripts/bureau-spec-review.schema.json" "You are the spec reviewer for $ISSUE ($ISSUE_TITLE). Validate, don't rewrite.

$SPEC_CONTEXT

Artifacts live under $SPEC_DIR (spec.md, plan.md, tasks.md, research.md if present).
In spec.md, place ## Governance and security with Decision: and Rationale: lines at column 0, in plain text, before any code block or HTML block; a leading YAML front matter block is allowed.
Require an explicit Decision: required/not required and a Rationale: explaining applicability (German labels/decisions are valid too). Justified not required is valid. The shared validator catches common authoring and placeholder/deferral mistakes, including bare restatements of the decision. You must judge substantive adequacy and whether the section is real: reject circular reasoning, vague assertions and explanations that do not justify applicability or address the relevant access, logging and retention risks. Passing the validator does not establish adequacy. Return review_status FAIL for missing or inadequate governance and name the concrete gaps in summary, even when the deterministic check passes. Do not invent the decision or rationale for the author.

Do this, in order:
1. Read the artifacts. Compare tasks.md file paths against the real repo tree (ls, grep, etc).
2. Fix these classes of issue IN PLACE (small edits, preserve structure):
   - Wrong file paths → correct them.
   - Task names that contradict existing conventions (read adjacent source files first).
   - Missing acceptance criteria that spec.md clearly implies.
3. Commit each fix as '$ISSUE: spec-review: <what>'. One concern per commit so the fix is auditable.
4. Determine ui_work_needed: true if tasks.md or spec.md mentions pages, components, views, forms, dialogs, modals, routes, or any other user-facing surface. Otherwise false.

Do NOT:
- Rewrite tasks.md from scratch. If the spec is fundamentally broken, set review_status to FAIL and list the issues — let Spec pipeline rework it.
- Touch files outside $SPEC_DIR.
- Re-litigate decisions pinned in SPEC.md, plan.md, or research.md — skip and cite the pin in summary if you considered flagging them.

End your response with a single fenced json block (the shell parses it):

\`\`\`json
{\"review_status\":\"PASS|FAIL\",\"ui_work_needed\":true,\"summary\":\"2-3 sentences, including remaining issues\"}
\`\`\`") || REVIEW_RC=$?

bureau_ticket_checkpoint "$ISSUE" latched
if [ "$REVIEW_RC" = 22 ]; then invalid_review_result; fi
[ "$REVIEW_RC" = 0 ] || exit "$REVIEW_RC"
echo "$REVIEW_RESULT"

echo ""
echo "Phase 2/2: route issue"

REVIEW_STATUS=$(parse_claude_json "$REVIEW_RESULT" '.review_status | select(type == "string")')
UI_NEEDED_RAW=$(parse_claude_json "$REVIEW_RESULT" '.ui_work_needed | select(type == "boolean" or type == "string")')

# Reject invalid results before governance can turn a valid PASS into FAIL.
case "$REVIEW_STATUS" in
  PASS|FAIL) ;;
  *) invalid_review_result ;;
esac
# The model cannot waive the document contract. Check after its allowed edits.
GOVERNANCE_ERROR=""
if ! GOVERNANCE_ERROR=$(bureau_without_secrets python3 -I "$SCRIPT_REPO/scripts/bureau-spec.py" validate "${SPEC_DIR}spec.md" 2>&1); then
  REVIEW_STATUS="FAIL"
fi

# Normalise ui_work_needed. Downstream `case` expects YES/NO strings.
case "$UI_NEEDED_RAW" in
  true|TRUE|yes|YES) UI_NEEDED="YES" ;;
  false|FALSE|no|NO) UI_NEEDED="NO" ;;
  *) invalid_review_result ;;
esac

commit_stage_changes spec_review "$ISSUE"
if [ "${BUREAU_DRY_RUN:-0}" = "1" ]; then
  echo "  [DRY_RUN] would: git push origin HEAD ($BRANCH)"
else
  bureau_ticket_checkpoint "$ISSUE"
  git push origin HEAD || exit 18
fi

REVIEW_SUMMARY=$(parse_claude_json "$REVIEW_RESULT" '.summary // "no summary"')
if [ -n "$GOVERNANCE_ERROR" ]; then REVIEW_SUMMARY="$REVIEW_SUMMARY

$GOVERNANCE_ERROR"; fi
REVIEW_SUMMARY=$(bureau_comment_projection "$REVIEW_SUMMARY")
# An unreadable model summary can suppress the whole projection. The validator's
# trusted failure reason must still reach the human and the next spec attempt.
if [ -n "$GOVERNANCE_ERROR" ] && [[ "$REVIEW_SUMMARY" != *"$GOVERNANCE_ERROR"* ]]; then
  REVIEW_SUMMARY="$REVIEW_SUMMARY

$GOVERNANCE_ERROR"
fi

case "$REVIEW_STATUS" in
  FAIL)
    bureau_without_secrets python3 -I "$SCRIPT_REPO/scripts/bureau-spec.py" --repo "$REPO_DIR" invalidate "$ISSUE" specify --reason spec-review-failed \
      || echo 'WARN: checkpoint invalidation unavailable; FAIL feedback still requires fresh specify.' >&2
    echo "  Spec review FAILED — moving back to Spec"
    post_comment "$ISSUE" "❌ Spec review: **FAIL**

$REVIEW_SUMMARY

Routing back to Spec for re-work."
    move_issue "$ISSUE" "$BUREAU_STATE_SPEC"
    NEXT_STATE="Spec (rework)"
    ;;
  PASS)
    if [ "$UI_NEEDED" = "YES" ]; then
      add_issue_label "$ISSUE" "needs-ux" \
        || echo "  WARN: failed to add 'needs-ux' label to $ISSUE; will retry on next tick" >&2
      post_comment "$ISSUE" "✅ Spec review **PASSED**. UI work detected — routing to Design.

$REVIEW_SUMMARY"
      move_issue "$ISSUE" "$BUREAU_STATE_DESIGN"
      echo "  Spec review PASSED — routing to Design"
      NEXT_STATE="Design"
    else
      bureau_overlap_ready "$ISSUE" "$BRANCH" spec_review --status "$REVIEW_STATUS"
      bureau_overlap_admit "$ISSUE" "$BRANCH" --local
      post_comment "$ISSUE" "✅ Spec review **PASSED**. Ready for implementation.

$REVIEW_SUMMARY"
      move_issue "$ISSUE" "$BUREAU_STATE_BUILD"
      echo "  Spec review PASSED — moving to Build"
      NEXT_STATE="Build"
    fi
    ;;
  *) invalid_review_result ;;
esac

echo ""
echo "═══════════════════════════════════════"
echo "  Spec Review complete: $ISSUE"
echo "  Branch: $BRANCH"
echo "  Result: ${REVIEW_STATUS:-UNKNOWN}"
echo "  Next: $NEXT_STATE"
echo "═══════════════════════════════════════"
