#!/bin/bash
# Copy pipeline: Pick a needs-copy issue from the Copy state → polish
# user-facing strings against a voice guide → commit → move to Build.
#
# Opt-in: no-op if .bureau.json lacks states.copy or labels.needs_copy. Slots
# between Design and Build (or between Spec Review and Build if no Design
# stage). Typically gated on repos with significant user-facing surfaces.
set -euo pipefail

unset CLAUDECODE 2>/dev/null || true

REPO_DIR="$(pwd)"
SCRIPT_REPO="$(cd "$(dirname "$0")/.." && pwd)"
source "$(dirname "$0")/bureau-config.sh"

if [ -f .env ]; then source .env
elif [ -f "$SCRIPT_REPO/.env" ]; then source "$SCRIPT_REPO/.env"
else echo "ERROR: No .env found"; exit 1; fi

CLAUDE=$(claude_cmd_for_stage "copy")
API_KEY="${LINEAR_API_KEY:?Set LINEAR_API_KEY in .env}"

precondition_linear

# Opt-in gate — no configured Copy state or label ⇒ nothing to do.
if [ -z "${BUREAU_STATE_COPY:-}" ] || [ -z "${BUREAU_LABEL_NEEDS_COPY_NAME:-}" ]; then
  echo "Copy pipeline: not configured (.linear.teams[0].states.copy and .linear.labels.needs_copy.name both required). Exiting as queue-empty."
  exit 2
fi

if [ -n "${1:-}" ]; then
  ISSUE="$1"
  echo "Using specified issue: $ISSUE"
else
  echo "Picking next Copy issue..."
  ISSUE=$(pipeline_pick_next "$(basename "$0")")

  if [ -z "$ISSUE" ] || [[ ! "$ISSUE" =~ ^[A-Z]+-[0-9]+$ ]]; then
    echo "No qualifying issues found. Queue empty."
    exit 2
  fi
  echo "Picked: $ISSUE"
fi

# State guard runs unconditionally — see implement-pipeline.sh for rationale.
ACTUAL_STATE=$(get_issue_state "$ISSUE")
if [ "$ACTUAL_STATE" != "Copy" ]; then
  echo "  WARNING: $ISSUE is in '$ACTUAL_STATE', not 'Copy'. Skipping."
  exit 2
fi

echo ""
echo "═══════════════════════════════════════"
echo "  Copy Pipeline: $ISSUE"
echo "═══════════════════════════════════════"
echo ""

ISSUE_DETAIL=$(get_issue_detail "$ISSUE")
ISSUE_TITLE=$(echo "$ISSUE_DETAIL" | jq -r '.title // empty')
echo "  $ISSUE: $ISSUE_TITLE"

echo "→ Finding branch..."
BRANCH=$(get_issue_branch "$ISSUE")
if [ -z "$BRANCH" ] || [[ "$BRANCH" == *" "* ]]; then
  echo "  ERROR: no bureau-branch marker found for $ISSUE."
  post_comment "$ISSUE" "❌ Copy pipeline cannot start — no bureau-branch marker. Moving back to Spec Review."
  move_issue "$ISSUE" "$BUREAU_STATE_SPEC_REVIEW"
  exit 12
fi
echo "  Branch: $BRANCH"

git fetch origin
free_branch_from_other_worktrees "$BRANCH" "$(pwd)"
if git rev-parse --verify "origin/$BRANCH" >/dev/null 2>&1; then
  git checkout -B "$BRANCH" "origin/$BRANCH"
elif git rev-parse --verify "$BRANCH" >/dev/null 2>&1; then
  git checkout "$BRANCH"
else
  echo "  ERROR: branch '$BRANCH' not found locally or on origin."
  post_comment "$ISSUE" "❌ Copy pipeline cannot start — branch \`$BRANCH\` does not exist."
  move_issue "$ISSUE" "$BUREAU_STATE_SPEC_REVIEW"
  exit 12
fi

# Same EXP-484 reasoning as implement/qa/code-review: copy commits land in
# user-facing strings and are pushed. The PR diff is computed against
# origin/main, so a stale base pollutes review.
if ! merge_origin_main_or_abort "$ISSUE" "Copy"; then
  move_issue "$ISSUE" "$BUREAU_STATE_SPEC_REVIEW"
  exit 17
fi

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
SPEC_CONTEXT=$(build_spec_context "$SPEC_DIR")

# Load voice guide if configured. Absence is explicitly OK — the agent falls
# back to matching existing strings in the codebase.
VOICE_BLOCK="No voice guide configured. Match tone and length of existing user-facing strings already in the codebase."
if [ -n "${BUREAU_COPY_VOICE_FILE:-}" ] && [ -f "$BUREAU_COPY_VOICE_FILE" ]; then
  VOICE_BLOCK="Voice guide: $BUREAU_COPY_VOICE_FILE (read it before editing)."
fi

echo ""
echo "Phase 1/2: polish copy"

NEGATIVE_CONSTRAINTS=$(build_negative_constraints)

$CLAUDE "You are the copywriter for $ISSUE ($ISSUE_TITLE) on branch $BRANCH.

$SPEC_CONTEXT

$VOICE_BLOCK

Scope — only user-facing strings added or changed on this branch:
- Button labels, links, page titles, headings.
- Empty states, loading states, success/error messages.
- Tooltips, form labels, helper text, placeholder text.
- Meta descriptions and og tags when editing pages.

For each string:
1. If a voice guide is configured, rewrite to match it. Otherwise infer tone from the 5 nearest existing user-facing strings (by file proximity in the codebase) and match. Only flag as \`open_questions\` when the existing nearby strings are themselves inconsistent and there's no voice guide.
2. Prefer the shortest form that retains the full meaning. Each rewrite must keep the original string's character length within ±25%, unless the original is unambiguously verbose (then explain in \`rationale\`).
3. Keep technical terms exact — do not 'translate' product names, API terms, CLI flags.

Role-specific NEVER edit:
- Strings in tests.
- Commit messages, changelog entries, or CHANGELOG files.
- Code comments or identifiers.
- Logs or debug output.
- Constants that downstream code depends on (tokens, enum values).

$NEGATIVE_CONSTRAINTS

Commit: '$ISSUE: copy polish'. One commit is fine for copy work.

Emit a fenced json block at the end. \`changes\` records each rewrite with before/after so reviewers don't need to read the diff:

\`\`\`json
{
  \"changes\": [{\"file\": \"\", \"line\": 0, \"before\": \"\", \"after\": \"\", \"rationale\": \"\"}],
  \"strings_changed\": 0,
  \"open_questions\": [],
  \"summary\": \"\"
}
\`\`\`" 2>&1

if ! git diff --quiet || ! git diff --cached --quiet; then
  git add -A
  git commit -m "$ISSUE: copy polish" --allow-empty || true
  if [ "${BUREAU_DRY_RUN:-0}" = "1" ]; then
    echo "  [DRY_RUN] would: git push origin HEAD ($BRANCH)"
  else
    git push origin HEAD || true
  fi
fi

echo ""
echo "Phase 2/2: route to Build"

post_comment "$ISSUE" "✍️ Copy pass complete on \`$BRANCH\`. Moving to Build."
move_issue "$ISSUE" "$BUREAU_STATE_BUILD"

echo "  Moved $ISSUE to Build"

echo ""
echo "═══════════════════════════════════════"
echo "  Copy pipeline complete: $ISSUE"
echo "  Branch: $BRANCH"
echo "  Next: Build"
echo "═══════════════════════════════════════"
