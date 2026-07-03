#!/bin/bash
# UX/UI Design pipeline: Pick Design issue → generate design artifacts → move to Build
set -euo pipefail

unset CLAUDECODE 2>/dev/null || true

REPO_DIR="$(pwd)"
SCRIPT_REPO="$(cd "$(dirname "$0")/.." && pwd)"
source "$(dirname "$0")/bureau-config.sh"

if [ -f .env ]; then source .env
elif [ -f "$SCRIPT_REPO/.env" ]; then source "$SCRIPT_REPO/.env"
else echo "ERROR: No .env found"; exit 1; fi

CLAUDE=$(claude_cmd_for_stage "ux")
API_KEY="${LINEAR_API_KEY:?Set LINEAR_API_KEY in .env}"

precondition_linear

if [ -n "${1:-}" ]; then
  ISSUE="$1"
  echo "Using specified issue: $ISSUE"
else
  echo "Picking next Design issue..."
  ISSUE=$(pipeline_pick_next "$(basename "$0")")

  if [ -z "$ISSUE" ] || [[ ! "$ISSUE" =~ ^[A-Z]+-[0-9]+$ ]]; then
    echo "No qualifying issues found. Queue empty."
    exit 2
  fi
  echo "Picked: $ISSUE"
fi

# State guard runs unconditionally — see implement-pipeline.sh for rationale.
# UX previously had no guard at all, so cron would happily run UX work on an
# issue that had moved out of Design between queue-loop's preselect and now.
ACTUAL_STATE=$(get_issue_state "$ISSUE")
if [ "$ACTUAL_STATE" != "Design" ]; then
  echo "  WARNING: $ISSUE is in '$ACTUAL_STATE', not 'Design'. Skipping."
  exit 2
fi

echo ""
echo "═══════════════════════════════════════"
echo "  UX/UI Design Pipeline: $ISSUE"
echo "═══════════════════════════════════════"
echo ""

echo "→ Fetching issue details..."
ISSUE_DETAIL=$(get_issue_detail "$ISSUE")
ISSUE_TITLE=$(echo "$ISSUE_DETAIL" | jq -r '.title // empty')
ISSUE_DESC=$(echo "$ISSUE_DETAIL" | jq -r '.description // empty')
PROJECT_NAME=$(echo "$ISSUE_DETAIL" | jq -r '.project.name // empty')
PROJECT_DESC=$(echo "$ISSUE_DETAIL" | jq -r '.project.description // empty')
echo "  $ISSUE: $ISSUE_TITLE"

echo "→ Finding spec branch..."
BRANCH=$(get_issue_branch "$ISSUE")

if [ -z "$BRANCH" ] || [[ "$BRANCH" == *" "* ]]; then
  echo "  ERROR: no bureau-branch marker found for $ISSUE."
  post_comment "$ISSUE" "❌ UX pipeline aborted — no bureau-branch marker. Moving back to Spec Review."
  move_issue "$ISSUE" "$BUREAU_STATE_SPEC_REVIEW"
  exit 12
fi

echo "  Branch: $BRANCH"
git fetch origin

# Release the branch from any other worktree before attaching here.
free_branch_from_other_worktrees "$BRANCH" "$(pwd)"
if git rev-parse --verify "origin/$BRANCH" >/dev/null 2>&1; then
  git checkout -B "$BRANCH" "origin/$BRANCH"
elif git rev-parse --verify "$BRANCH" >/dev/null 2>&1; then
  git checkout "$BRANCH"
else
  echo "  ERROR: branch '$BRANCH' not found locally or on origin."
  post_comment "$ISSUE" "❌ UX pipeline aborted — branch \`$BRANCH\` does not exist. Moving back to Spec Review."
  move_issue "$ISSUE" "$BUREAU_STATE_SPEC_REVIEW"
  exit 12
fi

# Same EXP-484 reasoning as implement/qa/code-review: design.md is committed
# and pushed; the PR diff is computed against origin/main, so a stale base
# pollutes the design-review with phantom-revert hunks.
if ! merge_origin_main_or_abort "$ISSUE" "UX/Design"; then
  move_issue "$ISSUE" "$BUREAU_STATE_SPEC_REVIEW"
  exit 17
fi

echo "→ Locating spec artifacts..."
SPEC_DIR=""
for d in "$BUREAU_SPECS_DIR"/*/; do
  [ -d "$d" ] || continue
  dir_name=$(basename "$d")
  slug=$(echo "$dir_name" | sed 's/^[0-9]*-//')
  if echo "$BRANCH" | grep -qi "$slug"; then
    SPEC_DIR="$d"
    break
  fi
done
[ -z "$SPEC_DIR" ] && SPEC_DIR=$(ls -td "$BUREAU_SPECS_DIR"/*/ 2>/dev/null | head -1 || true)

if [ -z "$SPEC_DIR" ]; then
  echo "  ERROR: No spec directory found for $ISSUE on branch $BRANCH."
  post_comment "$ISSUE" "❌ UX pipeline aborted — no spec directory matched branch \`$BRANCH\`. Routing back to Spec Review for spec rework."
  move_issue "$ISSUE" "$BUREAU_STATE_SPEC_REVIEW"
  exit 13
fi
echo "  Spec dir: $SPEC_DIR"

echo ""
echo "Phase 1/2: generate design artifacts"

PROJECT_CONTEXT=""
[ -n "$PROJECT_DESC" ] && PROJECT_CONTEXT="
--- Project context: $PROJECT_NAME ---
$PROJECT_DESC
--- End project context ---
"

SPEC_CONTEXT=$(build_spec_context "$SPEC_DIR")
NEGATIVE_CONSTRAINTS=$(build_negative_constraints)

UX_RESULT=$($CLAUDE "You are the UX/UI design agent for $ISSUE ($ISSUE_TITLE).

$SPEC_CONTEXT
$PROJECT_CONTEXT

Description:
$ISSUE_DESC

Inputs:
- Spec artifacts: ${SPEC_DIR}
- Existing codebase — READ before proposing. Scan for:
  - Design-system package (shadcn, radix, chakra, mui, headless-ui, custom).
  - Component directory and naming conventions.
  - Global tokens (tailwind.config.*, CSS variables, theme files).
  - Existing forms, dialogs, tables, empty-states of a similar shape to reuse.
- Linear attachments: if the description references an image URL you cannot fetch, note what it was expected to show under Open Questions — do not hallucinate content.

Produce \`${SPEC_DIR}design.md\` with this exact structure (headings verbatim):

\`\`\`markdown
# Design — $ISSUE_TITLE

## Component Hierarchy
<tree of components, top-down, noting which are reused vs new>

## Layout & Responsive Behavior
<breakpoints, grid, stacking rules — cite the repo's breakpoint tokens if any>

## Interaction Patterns
<click/keyboard/drag/focus flows — cite existing components where behavior matches>

## Visual Design
<tokens used, referencing the repo's token file paths>

## Accessibility
<keyboard nav, ARIA roles, focus order, contrast concerns>

## Existing Components to Reuse
<path → usage>

## Net-New Components
<proposed path → purpose → props sketch>

## Open Questions
<anything the spec left unresolved — flag for human review>
\`\`\`

Then update tasks.md: for each UI-touching task, append a sub-bullet linking to the design.md section it implements.

Commit: '$ISSUE: design artifacts'. One commit is fine — design is a single logical change.

Stop conditions — emit \`design_status\` accordingly:
- NEEDS_HUMAN: a required design token (color, spacing, typography, motion) is missing from the existing config AND the spec doesn't pin a fallback. Name the missing token(s) in open_questions with \`blocking: true\`.
- NEEDS_HUMAN: component-library version conflict (the spec needs a feature only available in a newer version than what's installed).
- INCOMPLETE: any blocking open question (a UX-critical decision the spec is silent on). Pipeline routes to Spec, not Build.
- COMPLETE: design.md fully populated, all open questions are non-blocking.

$NEGATIVE_CONSTRAINTS

Role-specific:
- Do NOT propose components from a library not already in package.json (or equivalent).
- Do NOT override design tokens that are already pinned in the existing config.
- Do NOT fabricate Figma links, screenshots, or URLs — cite repo file paths instead.
- Do NOT touch source files outside ${SPEC_DIR}; implementation happens later.

At the end, emit a fenced json block. \`components_net_new\` MUST include prop sketches so implementers consume them directly without re-deriving:

\`\`\`json
{
  \"design_status\": \"COMPLETE|INCOMPLETE|NEEDS_HUMAN\",
  \"components_net_new\": [{\"path\": \"\", \"props\": [{\"name\": \"\", \"type\": \"\", \"default\": null}]}],
  \"components_reused\": [\"path\"],
  \"open_questions\": [{\"q\": \"\", \"blocking\": false}],
  \"summary\": \"\"
}
\`\`\`" 2>&1)
echo "$UX_RESULT"

echo ""
echo "  design artifacts generated"

echo ""
echo "Phase 2/2: push + route"

DESIGN_STATUS=$(parse_claude_json "$UX_RESULT" '.design_status // "COMPLETE"')
DESIGN_SUMMARY=$(parse_claude_json "$UX_RESULT" '.summary // ""')

git add -A
git commit -m "$ISSUE: design artifacts" --allow-empty || true
if [ "${BUREAU_DRY_RUN:-0}" = "1" ]; then
  echo "  [DRY_RUN] would: git push origin HEAD ($BRANCH)"
else
  git push origin HEAD
fi

case "$DESIGN_STATUS" in
  NEEDS_HUMAN)
    echo "  Design flagged NEEDS_HUMAN — labelling and leaving in Design."
    add_issue_label "$ISSUE" "needs-human" \
      || echo "  WARN: failed to add 'needs-human' label to $ISSUE; will retry on next tick" >&2
    post_comment "$ISSUE" "🚫 UX flagged for human review.

$DESIGN_SUMMARY"
    NEXT_STATE_LABEL="Design (needs-human)"
    ;;
  INCOMPLETE)
    echo "  Design INCOMPLETE — routing back to Spec Review (blocking open questions)."
    post_comment "$ISSUE" "↩️ UX produced design artifacts but left blocking open questions. Routing back to Spec Review.

$DESIGN_SUMMARY"
    move_issue "$ISSUE" "$BUREAU_STATE_SPEC_REVIEW"
    NEXT_STATE_LABEL="Spec Review (rework)"
    ;;
  COMPLETE|*)
    post_comment "$ISSUE" "🎨 Design phase complete. Design artifacts committed to \`$BRANCH\`. Ready for implementation."
    move_issue "$ISSUE" "$BUREAU_STATE_BUILD"
    NEXT_STATE_LABEL="Build"
    ;;
esac

echo "  Moved $ISSUE to $NEXT_STATE_LABEL"

echo ""
echo "═══════════════════════════════════════"
echo "  UX/UI Design complete: $ISSUE"
echo "  Branch: $BRANCH"
echo "  Status: $NEXT_STATE_LABEL"
echo "═══════════════════════════════════════"
