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
  post_comment "$ISSUE" "❌ Spec pipeline failed (\`$klass\`, exit \`$rc\`). Routing back to Triage; matching completed phase checkpoints can resume." || true
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
SPEC_COMMENTS=$(get_issue_comments "$ISSUE")
# Reuse the latest substantive digest when the publication stripped its label.
# Publication itself is not feedback; the digest is still a specify input.
RESEARCH_RAW=$(printf '%s' "$SPEC_COMMENTS" | jq -r '[.[] | .body // "" | select(contains("<!-- bureau-research:"))][0] // ""')
RESEARCH_CONTEXT=""
if [ -n "$RESEARCH_RAW" ]; then
  RESEARCH_CONTEXT="
--- API research (auto-generated; treat as authoritative for API shapes) ---
$RESEARCH_RAW
--- End research ---
"
fi
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
    if post_comment "$ISSUE" "$(bureau_comment_projection "$RESEARCH_RAW")"; then
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
git fetch --prune --no-tags origin '+refs/heads/*:refs/remotes/origin/*' || exit 18
RESERVATION=$(python3 -I "$SCRIPT_REPO/scripts/bureau-supervision.py" --repo "$REPO_DIR" \
  reserve-spec-number "$ISSUE" --specs-dir "$BUREAU_SPECS_DIR" --base "$SPEC_BASE") || {
  RESERVATION_RC=$?
  [ "$RESERVATION_RC" != 124 ] || exit 124
  exit 18
}
SPEC_NUMBER=$(printf '%s' "$RESERVATION" | jq -er '.number')
FEATURE_HELPER=.specify/scripts/bash/create-new-feature.sh
if [ ! -f "$FEATURE_HELPER" ]; then
  echo 'ERROR: pinned Spec Kit feature helper is missing; resync Spec Kit 1.2.2.' >&2
  exit 24
fi
# The issue key is a stable short name, including when its title changes on retry.
# --allow-existing-branch reuses an exact directory without replacing spec.md.
FEATURE_ARGS=(--json --number "$SPEC_NUMBER" --short-name "$ISSUE" --allow-existing-branch "$ISSUE_TITLE")
FEATURE_JSON=$(bureau_untrusted_env bash "$FEATURE_HELPER" --dry-run "${FEATURE_ARGS[@]}") || exit 11
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
# Resolve branch ownership BEFORE specify/plan/tasks or checkpoint reuse. A
# changed reservation must not write new artifacts onto the old feature branch.
FEATURE_BRANCH="$ALLOCATED_NAME"
CUR_BRANCH=$(git branch --show-current)
if [ -z "$CUR_BRANCH" ] || [ "$CUR_BRANCH" = "$SPEC_BASE" ]; then
  if git show-ref --verify --quiet "refs/heads/$FEATURE_BRANCH"; then
    free_branch_from_other_worktrees "$FEATURE_BRANCH" "$REPO_DIR" || {
      FEATURE_OWNERSHIP_RC=$?
      # The worker's reset trap is already cleared. Keep this late refusal on
      # the same needs-human/local-hold path so Triage recovery cannot loop.
      BUREAU_RESET_REFUSAL_WORKTREE="$REPO_DIR"
      bureau_reset_refusal_trace "$ISSUE" spec || true
      exit "$FEATURE_OWNERSHIP_RC"
    }
    git checkout "$FEATURE_BRANCH"
  elif git show-ref --verify --quiet "refs/remotes/origin/$FEATURE_BRANCH"; then
    git checkout -b "$FEATURE_BRANCH" --track "origin/$FEATURE_BRANCH"
  else
    echo "  Creating branch $FEATURE_BRANCH from current HEAD"
    git checkout -b "$FEATURE_BRANCH"
  fi
elif [ "$CUR_BRANCH" = "$FEATURE_BRANCH" ]; then
  echo "  Already on $FEATURE_BRANCH (idempotent — speckit hook fired, or this is a retry)"
else
  echo "ERROR: on branch '$CUR_BRANCH', but the reservation requires '$FEATURE_BRANCH'. Reconcile the reservation/branch through ownership recovery before retrying; no spec phase was run." >&2
  exit 11
fi
# A rejected checkpoint means fresh phases, not permission to rebuild on stale
# local history. Keep published commits and refuse genuine divergence once,
# before specify/plan/tasks. Local-only completed commits can stay ahead.
FEATURE_REMOTE="refs/remotes/origin/$FEATURE_BRANCH"
if git show-ref --verify --quiet "$FEATURE_REMOTE"; then
  if git merge-base --is-ancestor HEAD "$FEATURE_REMOTE"; then
    git merge --ff-only "$FEATURE_REMOTE" || exit 18
  elif ! git merge-base --is-ancestor "$FEATURE_REMOTE" HEAD; then
    FEATURE_CONFLICT="Local branch $FEATURE_BRANCH and fetched origin/$FEATURE_BRANCH have diverged. A maintainer must reconcile both histories before retrying; no spec phase was run and Bureau will not force-push."
    echo "ERROR: needs-human: $FEATURE_CONFLICT" >&2
    post_comment "$ISSUE" "🛑 Spec needs human attention: $FEATURE_CONFLICT" || true
    mark_needs_human "$ISSUE" spec 25 technical "$FEATURE_CONFLICT" maintainer || true
    exit 25
  fi
fi
echo "  branch ready: $(git branch --show-current)"

if [ ! -d "$WANTED_DIR" ]; then
  CREATED_JSON=$(bureau_untrusted_env bash "$FEATURE_HELPER" "${FEATURE_ARGS[@]}") || exit 11
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

# Ticket text and review/human feedback are inputs. Generated digests, recovery
# comments and research publication are bookkeeping, not new ticket decisions.
SPEC_COMMENTS=$(get_issue_comments "$ISSUE")
# Stream all three inputs: research and its published comment can exceed the
# host's argv limit. JSON cannot contain a literal NUL, so separators are safe.
SPEC_INPUT=$(printf '%s\0%s\0%s' "$ISSUE_DETAIL" "$SPEC_COMMENTS" "$RESEARCH_CONTEXT" | jq -Rs '
  split("\u0000") as $inputs |
  ($inputs[0] | fromjson) as $detail | ($inputs[1] | fromjson) as $comments |
  $inputs[2] as $research |
  {ticket: ($detail | {title, description, project: (.project | {name, description})}),
   research: $research,
   feedback: [$comments[] | .body // "" |
     select((contains("<!-- bureau-research:") or test("^(<!-- bureau-branch:|❌ Spec pipeline|✅ Spec review)")) | not)]}')
SPEC_FEEDBACK=$(printf '%s' "$SPEC_INPUT" | jq -r '.feedback | join("\n\n")')
SPEC_CHECKPOINTS='[]'
if [ "${BUREAU_SPEC_CHECKPOINTS_DISABLED:-0}" != 1 ] && SPEC_CHECKPOINTS=$(printf '%s' "$SPEC_INPUT" | bureau_without_secrets python3 -I "$SCRIPT_REPO/scripts/bureau-spec.py" \
  --repo "$REPO_DIR" begin "$ISSUE" --specs-dir "$BUREAU_SPECS_DIR" --directory "$ALLOCATED_DIR" --number "$SPEC_NUMBER"); then
  :
else
  checkpoint_rc=$?
  if [ "${BUREAU_SPEC_CHECKPOINTS_DISABLED:-0}" != 1 ] && [ "$checkpoint_rc" != 2 ]; then exit 24; fi
  echo 'WARN: spec checkpoints unavailable; running all phases without reuse.' >&2
  SPEC_CHECKPOINTS='[]'
  export BUREAU_SPEC_CHECKPOINTS_DISABLED=1
fi
_spec_checkpoint_complete() {
  local checkpoint_rc phase_commit
  # Commit even when optional checkpoint storage is unavailable. Only this
  # reserved feature directory is included; provider-staged unrelated files stay out.
  phase_commit=$(bureau_without_secrets python3 -I "$SCRIPT_REPO/scripts/bureau-spec.py" --repo "$REPO_DIR" commit-phase "$ISSUE" "$1" \
    --specs-dir "$BUREAU_SPECS_DIR" --directory "$ALLOCATED_DIR") || return 24
  [ "${BUREAU_SPEC_CHECKPOINTS_DISABLED:-0}" != 1 ] || return 0
  if bureau_without_secrets python3 -I "$SCRIPT_REPO/scripts/bureau-spec.py" --repo "$REPO_DIR" complete "$ISSUE" "$1" --commit "$phase_commit"; then
    return 0
  else
    checkpoint_rc=$?
    [ "$checkpoint_rc" = 2 ] || return 24
    echo 'WARN: spec checkpoint write failed; checkpoint reuse disabled.' >&2
    SPEC_CHECKPOINTS='[]'
    export BUREAU_SPEC_CHECKPOINTS_DISABLED=1
  fi
}

if ! printf '%s' "$SPEC_CHECKPOINTS" | jq -e 'index("specify") != null' >/dev/null; then
BUREAU_PROVIDER_PHASE=specify "${CLAUDE[@]}" "Read the file $SPECKIT_DIR/speckit-specify/SKILL.md and follow its instructions exactly.

No measurements, probes, benchmarks or full test-gate runs in specify or plan. Use existing evidence and document unknowns for implementation/QA.
In spec.md, place ## Governance and security with Decision: and Rationale: lines at column 0, in plain text, before any code block or HTML block; a leading YAML front matter block is allowed.
Use Decision: required or Decision: not required and explain applicability in Rationale:; a justified not required is valid, while empty text, TBD, placeholders and unexplained N/A fail review.

Populate the already allocated feature in $ALLOCATED_DIR.
The feature helper has already run with --number $SPEC_NUMBER. Do not run create-new-feature again,
do not allocate another number, and do not change .specify/feature.json.
Use SPECIFY_FEATURE=$ALLOCATED_NAME and SPECIFY_FEATURE_DIRECTORY=$ALLOCATED_DIR for helpers.

Use this as input:

Feature name: $ISSUE_TITLE
Source: Linear issue $ISSUE
$PROJECT_CONTEXT
$ISSUE_DESC
--- Review and human feedback ---
$SPEC_FEEDBACK
$RESEARCH_CONTEXT
$LESSONS_CONTEXT"
else
  echo "  Resuming: specify checkpoint matches inputs and artifact hashes"
fi

echo ""
echo "  specify complete"

echo ""
echo "Phase 1.5: validate the reserved feature pointer"
# The branch was acquired before specify. An agent must not replace the pointer
# with a different ticket directory while populating the reserved feature.
FEATURE_DIR=$(jq -er '.feature_directory | select(type == "string" and length > 0)' .specify/feature.json 2>/dev/null) || FEATURE_DIR=""
if [ -z "$FEATURE_DIR" ] || [ ! -d "$FEATURE_DIR" ] || [ "$(cd "$FEATURE_DIR" && pwd -P)" != "$ALLOCATED_DIR" ]; then
  echo 'ERROR: feature.json no longer names the reserved directory; refusing to plan another feature.' >&2
  exit 11
fi
# The spec directory of this run, with the trailing slash the other stages use.
# Every later step reads this one directory instead of "the newest spec": in a
# fresh worktree all spec directories have the same age, and installs that
# reuse one `NNN-` prefix have many candidates.
SPEC_DIR="${FEATURE_DIR%/}/"
CUR_BRANCH=$(git branch --show-current)
if [ "$CUR_BRANCH" != "$FEATURE_BRANCH" ]; then
  echo "ERROR: specify left branch '$CUR_BRANCH', expected '$FEATURE_BRANCH'; refusing to plan." >&2
  exit 11
fi
if ! printf '%s' "$SPEC_CHECKPOINTS" | jq -e 'index("specify") != null' >/dev/null; then
  _spec_checkpoint_complete specify
fi

echo ""
echo "Phase 2/5: plan"
if ! printf '%s' "$SPEC_CHECKPOINTS" | jq -e 'index("plan") != null' >/dev/null; then
BUREAU_PROVIDER_PHASE=plan "${CLAUDE[@]}" "Read the file $SPECKIT_DIR/speckit-plan/SKILL.md and follow its instructions exactly.
No measurements, probes, benchmarks or full test-gate runs in specify or plan. Use existing evidence and document unknowns for implementation/QA.
Review and human feedback:
$SPEC_FEEDBACK
Work on the spec in $SPEC_DIR (and no other directory under $BUREAU_SPECS_DIR/)."
  _spec_checkpoint_complete plan
else
  echo "  Resuming: plan checkpoint matches inputs and artifact hashes"
fi
echo ""
echo "  plan complete"

echo ""
echo "Phase 3/5: tasks"
if ! printf '%s' "$SPEC_CHECKPOINTS" | jq -e 'index("tasks") != null' >/dev/null; then
BUREAU_PROVIDER_PHASE=tasks "${CLAUDE[@]}" "Read the file $SPECKIT_DIR/speckit-tasks/SKILL.md and follow its instructions exactly.
Review and human feedback:
$SPEC_FEEDBACK
Work on the spec in $SPEC_DIR (and no other directory under $BUREAU_SPECS_DIR/)."
  _spec_checkpoint_complete tasks
else
  echo "  Resuming: tasks checkpoint matches inputs and artifact hashes"
fi
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
git commit 2>/dev/null \
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
  SPEC_DIGEST=$(BUREAU_PROVIDER_PHASE=digest "${CLAUDE[@]}" "Read the spec artifacts in $SPEC_DIR and write a digest for a Linear comment.

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

$(bureau_comment_projection "$SPEC_DIGEST")

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
