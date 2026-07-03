#!/bin/bash
# Spec Review pipeline: Pick Spec Review issue → validate → route to Build/Design
set -euo pipefail

unset CLAUDECODE 2>/dev/null || true

REPO_DIR="$(pwd)"
SCRIPT_REPO="$(cd "$(dirname "$0")/.." && pwd)"
source "$(dirname "$0")/bureau-config.sh"

if [ -f .env ]; then source .env
elif [ -f "$SCRIPT_REPO/.env" ]; then source "$SCRIPT_REPO/.env"
else echo "ERROR: No .env found"; exit 1; fi

CLAUDE=$(claude_cmd_for_stage "spec_review")
API_KEY="${LINEAR_API_KEY:?Set LINEAR_API_KEY in .env}"

precondition_linear

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
ACTUAL_STATE=$(get_issue_state "$ISSUE")
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

# Fail loud: branch must exist. No silent fresh-from-main fallback (EXP-413).
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

echo "→ Locating spec artifacts..."
SPEC_DIR=""
for d in $BUREAU_SPECS_DIR/*/; do
  [ -d "$d" ] || continue
  dir_name=$(basename "$d")
  if echo "$BRANCH" | grep -qi "$(echo "$dir_name" | sed 's/^[0-9]*-//')" 2>/dev/null; then
    SPEC_DIR="$d"
    break
  fi
done
[ -z "$SPEC_DIR" ] && SPEC_DIR=$(ls -td $BUREAU_SPECS_DIR/*/ 2>/dev/null | head -1 || true)

if [ -z "$SPEC_DIR" ] || [ ! -f "${SPEC_DIR}tasks.md" ]; then
  echo "  ERROR: No spec artifacts found"
  post_comment "$ISSUE" "❌ Spec review aborted — no tasks.md found on branch \`$BRANCH\`. Moving back to Spec for re-work."
  move_issue "$ISSUE" "$BUREAU_STATE_SPEC"
  exit 13
fi
echo "  Spec dir: $SPEC_DIR"

echo ""
echo "Phase 1/2: review specs against codebase"

SPEC_CONTEXT=$(build_spec_context "$SPEC_DIR")

REVIEW_RESULT=$($CLAUDE "You are the spec reviewer for $ISSUE ($ISSUE_TITLE). Validate, don't rewrite.

$SPEC_CONTEXT

Artifacts live under $SPEC_DIR (spec.md, plan.md, tasks.md, research.md if present).

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
- Re-litigate decisions pinned in SPEC.md, plan.md, or research.md — skip and cite the pin in remaining_issues if you considered flagging them.

End your response with a single fenced json block (the shell parses it):

\`\`\`json
{\"review_status\":\"PASS|FAIL\",\"ui_work_needed\":true,\"issues_found\":0,\"issues_fixed\":0,\"remaining_issues\":[],\"summary\":\"2-3 sentences\"}
\`\`\`" 2>&1)

echo "$REVIEW_RESULT"

echo ""
echo "Phase 2/2: route issue"

REVIEW_STATUS=$(parse_claude_json "$REVIEW_RESULT" '.review_status // empty')
UI_NEEDED_RAW=$(parse_claude_json "$REVIEW_RESULT" '.ui_work_needed // empty')

# Parse failure → treat as FAIL and route back to Spec so a human (or a fresh
# spec run) can figure out what went wrong. Safer than silently routing to
# Build with a half-reviewed spec.
if [ -z "$REVIEW_STATUS" ]; then
  REVIEW_STATUS="FAIL"
fi

# Normalise ui_work_needed. Downstream `case` expects YES/NO strings.
case "$UI_NEEDED_RAW" in
  true|TRUE|yes|YES) UI_NEEDED="YES" ;;
  *) UI_NEEDED="NO" ;;
esac

if ! git diff --quiet || ! git diff --cached --quiet; then
  git add -A
  git commit -m "$ISSUE: spec-review adjustments" --allow-empty || true
  if [ "${BUREAU_DRY_RUN:-0}" = "1" ]; then
    echo "  [DRY_RUN] would: git push origin HEAD ($BRANCH)"
  else
    git push origin HEAD || true
  fi
fi

REVIEW_SUMMARY=$(parse_claude_json "$REVIEW_RESULT" '.summary // "no summary"')

if [ "$REVIEW_STATUS" = "FAIL" ]; then
  echo "  Spec review FAILED — moving back to Spec"
  post_comment "$ISSUE" "❌ Spec review: **FAIL**

$REVIEW_SUMMARY

Routing back to Spec for re-work."
  move_issue "$ISSUE" "$BUREAU_STATE_SPEC"
  NEXT_STATE="Spec (rework)"
elif [ "$UI_NEEDED" = "YES" ]; then
  add_issue_label "$ISSUE" "needs-ux" \
    || echo "  WARN: failed to add 'needs-ux' label to $ISSUE; will retry on next tick" >&2
  post_comment "$ISSUE" "✅ Spec review **PASSED**. UI work detected — routing to Design.

$REVIEW_SUMMARY"
  move_issue "$ISSUE" "$BUREAU_STATE_DESIGN"
  echo "  Spec review PASSED — routing to Design"
  NEXT_STATE="Design"
else
  post_comment "$ISSUE" "✅ Spec review **PASSED**. Ready for implementation.

$REVIEW_SUMMARY"
  move_issue "$ISSUE" "$BUREAU_STATE_BUILD"
  echo "  Spec review PASSED — moving to Build"
  NEXT_STATE="Build"
fi

echo ""
echo "═══════════════════════════════════════"
echo "  Spec Review complete: $ISSUE"
echo "  Branch: $BRANCH"
echo "  Result: ${REVIEW_STATUS:-UNKNOWN}"
echo "  Next: $NEXT_STATE"
echo "═══════════════════════════════════════"
