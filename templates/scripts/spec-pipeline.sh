#!/bin/bash
# Spec pipeline: Pick issue → specify → plan → tasks → push
set -euo pipefail

unset CLAUDECODE 2>/dev/null || true

REPO_DIR="$(pwd)"
SCRIPT_REPO="$(cd "$(dirname "$0")/.." && pwd)"
source "$(dirname "$0")/bureau-config.sh"

BUREAU_ENV_FILE="${BUREAU_ENV_FILE:-$SCRIPT_REPO/.env}"
# BUREAU_ENV_FILE only, never ./.env: in a stage worktree that is a file the branch controls.
if [ -f "$BUREAU_ENV_FILE" ]; then bureau_load_env --export "$BUREAU_ENV_FILE"
else bureau_secret_set LINEAR_API_KEY || { echo "ERROR: Set LINEAR_API_KEY"; exit 1; }; fi

CLAUDE=(run_stage_for spec)
bureau_secret_copy API_KEY LINEAR_API_KEY

precondition_linear

# single-flight / bounded-parallelism gate. When
# BUREAU_MAX_CONCURRENT_ISSUES is non-zero, refuse to pick new Triage work
# until existing in-flight issues drain below the cap. Spec is the only
# stage that admits new work into the pipeline — gating here is sufficient.
# Downstream stages (spec-review, ux, copy, implement, qa, code-review,
# rebase, merge) keep operating on whatever's already in-flight, so a cap
# of 1 still drains the current issue without deadlock.
spec_admission_gate
precondition_runner spec

# Step 0: Pick or resolve issue
if [ -n "${1:-}" ]; then
  ISSUE="$1"
  echo "Using specified issue: $ISSUE"
else
  echo "Picking next $BUREAU_LABEL_LANE2_NAME Triage issue..."
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
if [ "$ACTUAL_STATE" != "Triage" ]; then
  echo "  WARNING: $ISSUE is in '$ACTUAL_STATE', not 'Triage'. Skipping."
  exit 2
fi

echo ""
echo "═══════════════════════════════════════"
echo "  Spec Pipeline: $ISSUE"
echo "═══════════════════════════════════════"
echo ""

echo "→ Moving $ISSUE to Spec..."
move_issue "$ISSUE" "$BUREAU_STATE_SPEC"
echo "  Done."

# trap failure and route back to Triage. Installed right after
# the Triage→Spec move so any crash in speckit phases doesn't strand the issue.
# Cleared before the final move_issue → Spec Review so success doesn't fire it.
_spec_recovery() {
  local rc=$?
  [ "$rc" = 0 ] && return 0
  local klass="speckit-failed"
  case "$rc" in
    10) klass="linear-down" ;;
    11) klass="worktree-dirty" ;;
    # Linear just failed every retry: the rollback's own writes get one
    # attempt each, so the halt is not delayed by another full retry ladder.
    27) klass="linear-unusable"; export _BUREAU_LINEAR_SINGLE_ATTEMPT=1 ;;
    16) klass="claude-unauth" ;;
    *)  klass="speckit-failed" ;;
  esac
  echo "RECOVERY: spec-pipeline failed (exit $rc / $klass). Routing $ISSUE back to Triage." >&2
  post_comment "$ISSUE" "❌ Spec pipeline failed (\`$klass\`, exit \`$rc\`). Routing back to Triage for a fresh attempt." || true
  move_issue "$ISSUE" "$BUREAU_STATE_TRIAGE" || true
  alert_telegram "$ISSUE" "spec-pipeline" "$rc" "Spec phase crashed after Triage→Spec move ($klass)" || true
}
trap _spec_recovery EXIT

echo "→ Fetching issue details..."
ISSUE_DETAIL=$(get_issue_detail "$ISSUE")
ISSUE_TITLE=$(echo "$ISSUE_DETAIL" | jq -r '.title // empty')
ISSUE_DESC=$(echo "$ISSUE_DETAIL" | jq -r '.description // empty')
PROJECT_NAME=$(echo "$ISSUE_DETAIL" | jq -r '.project.name // empty')
PROJECT_DESC=$(echo "$ISSUE_DETAIL" | jq -r '.project.description // empty')
echo "  $ISSUE: $ISSUE_TITLE"
[ -n "$PROJECT_NAME" ] && echo "  Project: $PROJECT_NAME"

# Pre-spec research (label-gated, same-tick, best-effort). When the issue
# carries `needs-research` AND `.agents.research` is configured in
# .bureau.json, spawn a $CLAUDE pass with WebFetch/WebSearch to compile
# API/library notes from official docs. The output is injected into
# specify's prompt as authoritative API reference and posted to Linear so
# spec-review can see it.
#
# Failure handling: research is BEST-EFFORT, not a gate. Any non-zero
# exit, or output missing the `<!-- bureau-research: -->` marker, is
# swallowed and the pipeline proceeds to specify without research
# context. This keeps the exit-code protocol (0/2/10–16) untouched and
# degrades to today's behavior on failure. The label is stripped only
# after a successful Linear comment post, so a crashed research run can
# be retried by re-applying the label.
RESEARCH_CONTEXT=""
if agent_enabled research \
   && printf '%s' "$ISSUE_DETAIL" | jq -e '.labels | index("needs-research")' >/dev/null 2>&1; then
  echo ""
  echo "Phase 0/5: research (needs-research label present)"
  CLAUDE_RESEARCH=(run_stage_for research)
  RESEARCH_RAW=$("${CLAUDE_RESEARCH[@]}" "You are doing pre-spec research for a Linear issue. The spec/build agents that come after you have stale training data, so your job is to ground them in current docs.

Issue: $ISSUE_TITLE
Body:
$ISSUE_DESC

Task:
1. Identify every external API, SDK, library, or service the implementation will touch.
2. For each, use WebFetch on the official docs URL (or WebSearch to find it) and read the relevant sections. Prefer primary sources over blog posts.
3. Compile a single markdown digest containing, per dependency: current version, the specific endpoints / method signatures / config keys this task will use, breaking changes in the last ~18 months, and known gotchas.

Do NOT speculate. If docs are unreachable for a dependency, say so explicitly for that dependency rather than guessing.

OUTPUT FORMAT — read this last; this is what the parser checks.

Your output MUST contain this exact line somewhere (preferably the first non-empty line):

    <!-- bureau-research: api1,api2,api3 -->

Where api1,api2,api3 is a comma-separated list of the APIs you researched (no spaces around commas). Examples:
    <!-- bureau-research: aws-bedrock-converse,aws-sigv4 -->
    <!-- bureau-research: stripe-checkout,stripe-webhooks -->

If the issue has no external API surface, emit:

    <!-- bureau-research:  -->
    No external API surface identified.

After the marker, write a concise markdown body with inline doc URLs. No preamble, no closing summary. Be terse." 2>&1 || echo "")

  # Extract the marker from anywhere in the output (not just line 1).
  # Headless claude -p subprocesses regularly prepend markdown headers,
  # wrap in code fences, or add an intro sentence — all valid research
  # content that the strict line-1 check used to discard. The marker
  # itself is what signals "real research happened"; its position is
  # cosmetic. A here-string, not a pipe: grep -q leaves at the marker, and under
  # pipefail the writer's broken pipe would drop a long digest as "no valid output".
  if grep -q '<!-- bureau-research:' <<< "$RESEARCH_RAW"; then
    RESEARCH_CONTEXT="
--- API research (auto-generated; treat as authoritative for API shapes) ---
$RESEARCH_RAW
--- End research ---
"
    if post_comment "$ISSUE" "$RESEARCH_RAW"; then
      remove_issue_label "$ISSUE" "needs-research" || true
      echo "  research complete; label stripped"
    else
      echo "  warning: research comment post failed; label preserved for retry"
    fi
  else
    echo "  research produced no valid output (missing <!-- bureau-research: --> marker); proceeding without it"
  fi
fi

echo ""
echo "Phase 1/5: specify"
PROJECT_CONTEXT=""
[ -n "$PROJECT_DESC" ] && PROJECT_CONTEXT="
--- Project context: $PROJECT_NAME ---
$PROJECT_DESC
--- End project context ---
"

# logs→memory: include human-curated LESSONS.md if present. Empty when absent.
LESSONS_CONTEXT=$(build_lessons_context)
SPECKIT_DIR=.claude/skills
if [ "$(resolve_runner_for_stage spec)" = codex ]; then SPECKIT_DIR=.agents/skills; fi

# Refresh every origin head even in a single-branch clone. The reservation lock
# covers inspection and persistence; fetch uses Bureau's normal git wrapper.
SPEC_BASE=$(bureau_get '.repo.base_branch // "main"')
git fetch --no-tags origin '+refs/heads/*:refs/remotes/origin/*' || exit 18
RESERVATION=$(python3 -I "$SCRIPT_REPO/scripts/bureau-supervision.py" --repo "$REPO_DIR" \
  reserve-spec-number "$ISSUE" --specs-dir "$BUREAU_SPECS_DIR" --base "$SPEC_BASE") || exit 18
SPEC_NUMBER=$(printf '%s' "$RESERVATION" | jq -er '.number')
FEATURE_HELPER=.specify/scripts/bash/create-new-feature.sh
if [ ! -f "$FEATURE_HELPER" ]; then
  echo 'ERROR: pinned Spec Kit feature helper is missing; resync Spec Kit 1.2.2.' >&2
  exit 24
fi
# The issue key is a stable short name, including when its title changes on retry.
# --allow-existing-branch reuses an exact directory without replacing spec.md.
FEATURE_ARGS=(--json --number "$SPEC_NUMBER" --short-name "$ISSUE" --allow-existing-branch "$ISSUE_TITLE")
FEATURE_JSON=$(bash "$FEATURE_HELPER" --dry-run "${FEATURE_ARGS[@]}") || exit 11
if ! printf '%s' "$FEATURE_JSON" | jq -e --argjson number "$SPEC_NUMBER" \
    '(.FEATURE_NUM | tonumber) == $number and (.SPEC_FILE | type) == "string"' >/dev/null; then
  echo 'ERROR: Spec Kit did not honor the reserved feature number; refusing to specify.' >&2
  exit 11
fi
ALLOCATED_DIR=$(dirname "$(printf '%s' "$FEATURE_JSON" | jq -er '.SPEC_FILE')")
ALLOCATED_NAME=$(basename "$ALLOCATED_DIR")
# 1.2.2 creates under specs/ (no directory option). A dry-run validates the
# number first. Reuse an existing target without creating/deleting a template
# elsewhere; for a new custom target, relocate the helper's actual output.
WANTED_DIR="${BUREAU_SPECS_DIR%/}/$ALLOCATED_NAME"
if [ ! -d "$WANTED_DIR" ]; then
  CREATED_JSON=$(bash "$FEATURE_HELPER" "${FEATURE_ARGS[@]}") || exit 11
  if [ "$CREATED_JSON" != "$(printf '%s' "$FEATURE_JSON" | jq -c 'del(.DRY_RUN)')" ]; then
    echo 'ERROR: Spec Kit output changed after its dry-run; refusing to specify.' >&2
    exit 11
  fi
  mkdir -p "$BUREAU_SPECS_DIR"
  if [ "$(cd "$(dirname "$ALLOCATED_DIR")" && pwd -P)/$ALLOCATED_NAME" != "$(cd "$BUREAU_SPECS_DIR" && pwd -P)/$ALLOCATED_NAME" ]; then
    mv "$ALLOCATED_DIR" "$WANTED_DIR"
  fi
fi
ALLOCATED_DIR=$(cd "$WANTED_DIR" && pwd -P)
jq -n --arg dir "$ALLOCATED_DIR" '{feature_directory:$dir}' > .specify/feature.json
export SPECIFY_FEATURE="$ALLOCATED_NAME" SPECIFY_FEATURE_DIRECTORY="$ALLOCATED_DIR"

"${CLAUDE[@]}" "Read the file $SPECKIT_DIR/speckit-specify/SKILL.md and follow its instructions exactly.

Populate the already allocated feature in $ALLOCATED_DIR.
The feature helper has already run with --number $SPEC_NUMBER. Do not run create-new-feature again,
do not allocate another number, and do not change .specify/feature.json.
Use SPECIFY_FEATURE=$ALLOCATED_NAME and SPECIFY_FEATURE_DIRECTORY=$ALLOCATED_DIR for helpers.

Use this as input:

Feature name: $ISSUE_TITLE
Source: Linear issue $ISSUE
$PROJECT_CONTEXT
$ISSUE_DESC
$RESEARCH_CONTEXT
$LESSONS_CONTEXT"

echo ""
echo "  specify complete"

echo ""
echo "Phase 1.5: ensure feature branch (speckit v0.7.5+ hook-skip workaround)"
# Spec Kit 1.2.2's create-new-feature.sh creates the feature directory and
# writes .specify/feature.json, but does not create a Git branch. Git-extension
# hooks may create one, but headless agents can skip prompt-driven hooks.
# Read the feature pointer and name the branch after its directory when needed;
# `git checkout -b` carries the untracked spec files onto the new branch.
#
# Upstream tracking: github/spec-kit (issue filed 2026-05-15)
FEATURE_DIR=$(jq -er '.feature_directory | select(type == "string" and length > 0)' .specify/feature.json 2>/dev/null) || FEATURE_DIR=""
if [ -z "$FEATURE_DIR" ] || [ ! -d "$FEATURE_DIR" ] || [ "$(cd "$FEATURE_DIR" && pwd -P)" != "$ALLOCATED_DIR" ]; then
  echo 'ERROR: feature.json no longer names the reserved directory; refusing to plan another feature.' >&2
  exit 11
fi
FEATURE_BRANCH=$(basename "$FEATURE_DIR")
# The spec directory of this run, with the trailing slash the other stages use.
# Every later step reads this one directory instead of "the newest spec": in a
# fresh worktree all spec directories have the same age, and installs that
# reuse one `NNN-` prefix have many candidates.
SPEC_DIR="${FEATURE_DIR%/}/"
CUR_BRANCH=$(git branch --show-current)
if [ -z "$CUR_BRANCH" ] || [ "$CUR_BRANCH" = "$SPEC_BASE" ]; then
  echo "  Creating branch $FEATURE_BRANCH from current HEAD"
  git checkout -b "$FEATURE_BRANCH"
elif [ "$CUR_BRANCH" = "$FEATURE_BRANCH" ]; then
  echo "  Already on $FEATURE_BRANCH (idempotent — speckit hook fired, or this is a retry)"
else
  echo "  WARN: on unexpected branch '$CUR_BRANCH', expected '$FEATURE_BRANCH'. Continuing on current."
fi
echo "  branch ready: $(git branch --show-current)"

echo ""
echo "Phase 2/5: plan"
"${CLAUDE[@]}" "Read the file $SPECKIT_DIR/speckit-plan/SKILL.md and follow its instructions exactly.
Work on the spec in $SPEC_DIR (and no other directory under $BUREAU_SPECS_DIR/)."
echo ""
echo "  plan complete"

echo ""
echo "Phase 3/5: tasks"
"${CLAUDE[@]}" "Read the file $SPECKIT_DIR/speckit-tasks/SKILL.md and follow its instructions exactly.
Work on the spec in $SPEC_DIR (and no other directory under $BUREAU_SPECS_DIR/)."
echo ""
echo "  tasks complete"

echo ""
echo "Phase 4/5: crosscheck"
SPEC_TASKS=""
[ -f "${SPEC_DIR}tasks.md" ] && SPEC_TASKS="${SPEC_DIR}tasks.md"
if [ -n "$SPEC_TASKS" ]; then
  crosscheck_open_prs "$ISSUE" "$SPEC_TASKS"
else
  echo "  No tasks.md found — skipping crosscheck"
fi

echo ""
echo "Phase 5/5: push + update Linear"
BRANCH=$(git branch --show-current)
if [ "$BRANCH" = "main" ] || [ -z "$BRANCH" ]; then
  trap - EXIT
  echo "ERROR: Not on a feature branch. Moving $ISSUE back to Triage."
  post_comment "$ISSUE" "❌ Spec pipeline aborted — not on a feature branch after speckit phases. Routing back to Triage." || true
  move_issue "$ISSUE" "$BUREAU_STATE_TRIAGE" || true
  alert_telegram "$ISSUE" "spec-pipeline" "11" "Not on a feature branch after speckit phases" || true
  exit 11
fi

git add -A
# Trailer makes the commit recognizable to branch_is_bureau_only without
# relying on the subject-line regex fallback.
git commit --allow-empty 2>/dev/null \
  -m "$ISSUE: spec artifacts" \
  -m "Bureau-Generated: true" \
  || true
if [ "${BUREAU_DRY_RUN:-0}" = "1" ]; then
  echo "  [DRY_RUN] would: git push -u origin HEAD ($BRANCH)"
else
  git push -u origin HEAD
  echo "  Pushed branch: $BRANCH"
fi

# The spec directory for this issue is $SPEC_DIR, read from .specify/feature.json above.

# Build a digest from the spec artifacts (creative work — Claude).
# Idempotency: pull the most recent existing spec-digest comment so Claude can
# emit `SKIP` if the new digest would be byte-identical. This avoids
# comment-storms when the spec pipeline re-runs (e.g. after a Triage retry)
# without any artifact changes.
PREV_DIGEST=$(get_issue_comments "$ISSUE" \
  | jq -r '[.[] | select(.body | test("\\*\\*Spec Artifacts —"))][0].body // ""' 2>/dev/null || echo "")
SPEC_DIGEST=""
if [ -n "$SPEC_DIR" ]; then
  SPEC_DIGEST=$("${CLAUDE[@]}" "Read the spec artifacts in $SPEC_DIR and write a digest for a Linear comment.

Previous digest on this issue (may be empty if this is the first spec run):
---PREV---
$PREV_DIGEST
---/PREV---

If the digest you would write is byte-identical to the previous one (no spec.md / plan.md / tasks.md changes since), output the literal string \`SKIP\` and nothing else. Otherwise produce the new digest.

Sections (markdown, in this order):
1. **Purpose** — 1-2 sentences from spec.md §Overview. Do not reframe or embellish.
2. **Key Requirements** — top 5 functional requirements from spec.md as a bulleted list. Keep the original wording.
3. **Tech Approach** — 2-3 sentences from plan.md. Name the stack, libraries, and the key design decision.
4. **Risks / Assumptions** — anything plan.md or research.md flagged as risky, unresolved, or assumed. Bullet list. If none are flagged, write 'None flagged.'
5. **Tasks** — copy-paste the full task list from tasks.md verbatim. Preserve checkboxes, IDs, dependency markers, and any [P] tags. Do NOT rephrase or renumber — downstream implementers match against these exactly.

End with:
---
Branch: \`$BRANCH\`
Spec dir: \`$SPEC_DIR\`

Constraints: do not invent requirements. If a section has no source material, write 'Not specified.' Keep sections 1-4 short; section 5 must be complete." 2>&1 || echo "Spec digest generation failed")
fi

# Clear the recovery trap — we're about to succeed.
trap - EXIT

# Idempotency: if Claude detected no artifact change, post no comment. The
# bureau-branch marker is already on the issue from the previous spec run.
SPEC_DIGEST_TRIMMED=$(printf '%s' "$SPEC_DIGEST" | sed 's/^[[:space:]]*//; s/[[:space:]]*$//')
if [ "$SPEC_DIGEST_TRIMMED" = "SKIP" ]; then
  echo "  Spec digest unchanged — no new comment posted."
else
  # Post the combined digest + bureau-branch marker (deterministic
  # branch discovery). The marker is an HTML comment so it renders invisibly
  # in Linear but can be parsed by get_issue_branch() in implement/spec-review/ux.
  DIGEST_BODY="<!-- bureau-branch: $BRANCH -->
**Spec Artifacts — $ISSUE**

$SPEC_DIGEST

---
*Spec branch \`$BRANCH\` pushed. Ready for review.*"
  post_comment "$ISSUE" "$DIGEST_BODY"
fi

move_issue "$ISSUE" "$BUREAU_STATE_SPEC_REVIEW"

echo "  Moved $ISSUE to Spec Review"

echo ""
echo "═══════════════════════════════════════"
echo "  Spec pipeline complete: $ISSUE"
echo "  Branch: $BRANCH"
echo "  Status: Spec Review"
echo "═══════════════════════════════════════"
