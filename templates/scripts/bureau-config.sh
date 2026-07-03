#!/bin/bash
# bureau-config.sh — reads .bureau.json for pipeline scripts
# Source this file: source "$(dirname "$0")/bureau-config.sh"

BUREAU_CONFIG=""

_find_config() {
  if [ -f ".bureau.json" ]; then
    BUREAU_CONFIG=".bureau.json"
  elif [ -f "$(cd "$(dirname "$0")/.." && pwd)/.bureau.json" ]; then
    BUREAU_CONFIG="$(cd "$(dirname "$0")/.." && pwd)/.bureau.json"
  else
    echo "ERROR: .bureau.json not found. Run /bureau-init to set up."
    exit 1
  fi
}

_find_config

bureau_get() { jq -r "$1" "$BUREAU_CONFIG"; }

# Linear config
BUREAU_TEAM_KEY=$(bureau_get '.linear.teams[0].key')
BUREAU_TEAM_ID=$(bureau_get '.linear.teams[0].id')
BUREAU_TEAM_NAME=$(bureau_get '.linear.teams[0].name')

# States
BUREAU_STATE_TRIAGE=$(bureau_get '.linear.teams[0].states.triage')
BUREAU_STATE_SPEC=$(bureau_get '.linear.teams[0].states.spec')
BUREAU_STATE_SPEC_REVIEW=$(bureau_get '.linear.teams[0].states.spec_review')
BUREAU_STATE_DESIGN=$(bureau_get '.linear.teams[0].states.design')
BUREAU_STATE_BUILD=$(bureau_get '.linear.teams[0].states.build')
BUREAU_STATE_BUILD_REVIEW=$(bureau_get '.linear.teams[0].states.build_review')
BUREAU_STATE_DONE=$(bureau_get '.linear.teams[0].states.done')
# Optional states (empty string when not configured in .bureau.json).
# qa slots between Build and Build Review. copy slots between Design and Build
# (or between Spec Review and Build if no Design stage). merge slots between
# Build Review and Done — opt-in waiting room for the gated merge agent.
BUREAU_STATE_QA=$(bureau_get '.linear.teams[0].states.qa // empty')
BUREAU_STATE_COPY=$(bureau_get '.linear.teams[0].states.copy // empty')
BUREAU_STATE_MERGE=$(bureau_get '.linear.teams[0].states.merge // empty')

# Labels
BUREAU_LABEL_LANE2=$(bureau_get '.linear.labels.lane2.id')
BUREAU_LABEL_LANE2_NAME=$(bureau_get '.linear.labels.lane2.name')
BUREAU_LABEL_NEEDS_HUMAN=$(bureau_get '.linear.labels.needs_human.id')
BUREAU_LABEL_NEEDS_UX=$(bureau_get '.linear.labels.needs_ux.id')
BUREAU_LABEL_AI_IMPL=$(bureau_get '.linear.labels.ai_implementable.id')
# Optional: copywriter gate. Absence → copy pipeline is disabled for this repo.
BUREAU_LABEL_NEEDS_COPY_NAME=$(bureau_get '.linear.labels.needs_copy.name // empty')

# Optional: path to a copy voice guide (markdown). Read by copy-pipeline.sh.
BUREAU_COPY_VOICE_FILE=$(bureau_get '.repo.copy_voice_file // empty')

# Agent config
BUREAU_POLL_INTERVAL=$(bureau_get '.agents.poll_interval_minutes // 30')
BUREAU_WORKBENCH_PANES=$(bureau_get '.agents.workbench_panes // 2')
BUREAU_MAX_REVIEW_CYCLES=$(bureau_get '.agents.max_review_cycles // 3')
# Code-review diff size at which specialists switch from exhaustive to
# critical-path sampling. Repos with mature CI / type-safety can review
# bigger diffs exhaustively; legacy repos cap lower. Configurable per repo.
BUREAU_CODE_REVIEW_SAMPLING_THRESHOLD=$(bureau_get '.agents.code_review_sampling_threshold // 500')
# How merge-pipeline closes the PR. Valid: squash | merge | rebase. Default
# squash so shipped main never carries the in-development merge commits the
# pipelines accumulate. Repos that want explicit merge-commit history (or
# strict linear via rebase) opt out per-repo.
# Per-stage model override (EXP-490). Resolution is performed live by
# resolve_model_for_stage / claude_cmd_for_stage — see below for the
# precedence contract. Example .bureau.json shape:
#   {"agents": {"model": "claude-sonnet-4-6",
#               "spec": {"model": "claude-opus-4-7"},
#               "code_review": {"model": "claude-haiku-4-5-20251001"}}}
#
# bureau_get_agent_model: type-safe lookup for `.agents.<stage>.model`. An
# agent toggle may be a boolean (`true`/`false`) or a string (`"v2"`); plain
# `.agents.<stage>.model` errors with "Cannot index boolean with string". This
# helper guards on type and returns empty unless `.agents.<stage>` is actually
# an object holding a `.model` field.
bureau_get_agent_model() {
  jq -r ".agents.$1 | if type==\"object\" then .model // empty else empty end" "$BUREAU_CONFIG"
}

# NOTE: We deliberately do NOT pre-load BUREAU_MODEL_DEFAULT or
# BUREAU_MODEL_<STAGE> from .bureau.json here. The previous source-time
# pre-load had two failure modes:
#   (a) an operator's `BUREAU_MODEL_<STAGE>=…` env override got CLOBBERED
#       by the JSON read, so the documented "env beats per-stage JSON"
#       contract silently broke;
#   (b) downstream consumers (bureau-status.sh --config, claude_cmd_for_stage)
#       couldn't tell whether `BUREAU_MODEL_<STAGE>` came from an operator or
#       from the JSON pre-load, so resolution precedence was indeterminate.
# Resolution now happens live via resolve_model_for_stage on every call.

# Cap on concurrent in-flight issues (EXP-491). 0 = unlimited (current default).
# 1 = single-flight (drain one issue end-to-end before another enters Spec).
# Higher values bound parallelism without forbidding it. Only spec-pipeline
# honours this; downstream stages keep operating on whatever's already in
# flight so a cap of 1 doesn't deadlock the loop.
BUREAU_MAX_CONCURRENT_ISSUES=$(bureau_get '.agents.max_concurrent_issues // 0')
BUREAU_MERGE_STRATEGY=$(bureau_get '.agents.merge_strategy // "squash"')
case "$BUREAU_MERGE_STRATEGY" in
  squash|merge|rebase) ;;
  *)
    echo "WARN: .agents.merge_strategy = '$BUREAU_MERGE_STRATEGY' is not one of {squash, merge, rebase} — falling back to squash." >&2
    BUREAU_MERGE_STRATEGY="squash"
    ;;
esac

# Repo config
BUREAU_BRANCH_PREFIX=$(bureau_get '.repo.branch_prefix // "feat"')
BUREAU_COMMIT_PREFIX=$(bureau_get '.repo.commit_prefix // ""')
BUREAU_SPECS_DIR=$(bureau_get '.repo.specs_dir // "specs"')

# Projects filter (comma-separated UUIDs; empty = all projects in the team)
BUREAU_PROJECTS=$(bureau_get '.linear.projects // [] | join(",")')

# Helper: query Linear GraphQL
linear_query() {
  curl -s -X POST https://api.linear.app/graphql \
    -H "Content-Type: application/json" \
    -H "Authorization: ${API_KEY:-$LINEAR_API_KEY}" \
    -d "{\"query\": \"$1\"}"
}

# Helper: run a raw GraphQL payload (for mutations that need variables).
linear_raw() {
  curl -s -X POST https://api.linear.app/graphql \
    -H "Content-Type: application/json" \
    -H "Authorization: ${API_KEY:-$LINEAR_API_KEY}" \
    -d "$1"
}

# EXP-490: per-stage model resolution. Resolution order (first non-empty
# wins):
#   1. BUREAU_MODEL_<STAGE> env  (operator override, e.g. ad-hoc shell var)
#   2. .agents.<stage>.model     (per-stage JSON)
#   3. .agents.model             (workspace JSON default)
#   4. BUREAU_MODEL_DEFAULT env  (workspace env fallback, typically .env)
#   5. empty → no --model flag emitted (claude CLI's own default applies)
#
# Reads JSON live every call. Does NOT depend on any source-time JSON→env
# pre-load (see the note above bureau_get_agent_model for why). Single source
# of truth: both claude_cmd_for_stage and the bureau-status.sh --config
# display call this.
#
# Stage names match the .bureau.json agents.* keys: spec, spec_review, ux,
# copy, implement, qa, code_review, merge, research.
#
# Stdout: the resolved model identifier, or empty if none configured.
resolve_model_for_stage() {
  local stage="$1"
  # bash 3.2 (macOS default) lacks ${var^^} uppercase expansion, so use tr.
  # See similar bash-3.2 caveat in pick_issue (parallel arrays vs `declare -A`).
  local upper
  upper=$(printf '%s' "$stage" | tr '[:lower:]' '[:upper:]')
  local var="BUREAU_MODEL_${upper}"
  local model

  # (1) per-stage env (operator override)
  model="${!var:-}"

  # (2) per-stage JSON
  if [ -z "$model" ]; then
    model=$(bureau_get_agent_model "$stage")
  fi

  # (3) workspace JSON default
  if [ -z "$model" ]; then
    model=$(bureau_get '.agents.model // empty')
  fi

  # (4) workspace env fallback
  if [ -z "$model" ]; then
    model="${BUREAU_MODEL_DEFAULT:-}"
  fi

  printf '%s' "$model"
}

# Resolve which model RUNNER backs a stage: "claude" (default) or "codex".
# Same precedence ladder as resolve_model_for_stage:
#   (1) per-stage env   BUREAU_RUNNER_<STAGE>   (e.g. BUREAU_RUNNER_QA=codex)
#   (2) per-stage JSON  .agents.<stage>.runner
#   (3) workspace JSON  .agents.runner
#   (4) hard default    "claude"
# Anything other than "codex" resolves to "claude" — opt-in, never silent.
resolve_runner_for_stage() {
  local stage="$1"
  local upper
  upper=$(printf '%s' "$stage" | tr '[:lower:]' '[:upper:]')
  local var="BUREAU_RUNNER_${upper}"
  local runner
  runner="${!var:-}"
  if [ -z "$runner" ]; then
    runner=$(jq -r ".agents.$stage | if type==\"object\" then .runner // empty else empty end" "$BUREAU_CONFIG" 2>/dev/null)
  fi
  if [ -z "$runner" ]; then
    runner=$(bureau_get '.agents.runner // empty')
  fi
  [ "$runner" = "codex" ] && { echo "codex"; return; }
  echo "claude"
}

# Build the model invocation for a stage. Default backend is `claude -p`;
# when the stage's runner resolves to "codex", emit a codex-stage-runner.sh
# invocation that presents the SAME `cmd "PROMPT"` → verdict-on-stdout contract
# (see that script's header). Emits `--model <m>` only when
# resolve_model_for_stage finds one; otherwise the CLI's own default applies.
#
# Pipelines call this once into a local CLAUDE variable:
#   CLAUDE=$(claude_cmd_for_stage "implement")
claude_cmd_for_stage() {
  local stage="$1"
  local model runner
  model=$(resolve_model_for_stage "$stage")
  runner=$(resolve_runner_for_stage "$stage")

  if [ "$runner" = "codex" ]; then
    # Read-only for review (it must not mutate the tree); workspace-write for
    # QA (it commits tests). The stage name decides the safe default.
    local sandbox="workspace-write"
    case "$stage" in
      code_review|spec_review|research) sandbox="read-only" ;;
      *)
        # Guardrail (stderr ONLY — stdout is the command string callers eval):
        # Codex's exec sandbox has no network listeners, trust-store, or
        # git-metadata writes, so stages that run the project's build/test suite
        # (qa, implement) fail spuriously there and false-halt needs-human.
        # Route ONLY review-type stages to Codex; keep qa/implement/spec on Claude.
        echo "warning: stage '$stage' resolved to runner=codex, but Codex's sandbox can't run most build/test suites — expect spurious failures / needs-human halts. Route only code_review (diff-reading) to Codex; keep '$stage' on Claude." >&2
        ;;
    esac
    # The `model` resolved above is a CLAUDE model id — must NOT be forwarded to
    # codex. Codex's model comes from a separate codex-specific source so the
    # two never cross-contaminate:
    #   (1) per-stage env  BUREAU_CODEX_MODEL_<STAGE>
    #   (2) workspace env  BUREAU_CODEX_MODEL_DEFAULT
    #   (3) omit → codex CLI's own configured default applies
    local cupper cvar cmodel
    cupper=$(printf '%s' "$stage" | tr '[:lower:]' '[:upper:]')
    cvar="BUREAU_CODEX_MODEL_${cupper}"
    cmodel="${!cvar:-${BUREAU_CODEX_MODEL_DEFAULT:-}}"
    local runner_path="${BUREAU_SCRIPT_DIR:-scripts}/codex-stage-runner.sh"
    if [ -n "$cmodel" ]; then
      echo "bash $runner_path --model $cmodel --sandbox $sandbox --"
    else
      echo "bash $runner_path --sandbox $sandbox --"
    fi
    return
  fi

  # EXP-671 — cost tracking swaps `--print` (text) for `--output-format json`
  # (envelope with usage). Default OFF → `--print`, byte-identical. parse_claude_json
  # unwraps the envelope transparently, so consumers are unaffected either way.
  local out_flag="--print"
  cost_tracking_enabled && out_flag="--output-format json"

  # EXP-token-efficiency — optional headroom wrap. When .agents.headroom_wrap
  # is true, prefix the claude invocation with `headroom wrap ` so headroom's
  # compression pipeline sits between this script and the Anthropic API.
  # Reversible (CCR): claude can call `headroom_retrieve` to fetch originals
  # if a summary is too lossy for the current task. Scoped to the claude
  # backend only — the codex path above is left alone (codex has its own
  # sandboxing layer that doesn't compose cleanly with headroom wrap).
  # Headroom must be on PATH; misconfiguration surfaces immediately on the
  # first stage invocation. See docs/configuration.md for the full schema.
  # `headroom wrap claude` is the launcher; `--` separates headroom's OWN flags
  # (it has -p/--port) from claude's args. WITHOUT `--`, headroom parses claude's
  # `-p` (print) as its --port and dies ("'--print' is not a valid integer").
  # headroom's own help documents exactly this form: `headroom wrap claude -- -p`.
  local launcher="claude" sep=""
  if headroom_wrap_enabled; then
    launcher="headroom wrap claude"
    sep="-- "
  fi

  if [ -n "$model" ]; then
    echo "${launcher} ${sep}-p $out_flag --dangerously-skip-permissions --model $model"
  else
    echo "${launcher} ${sep}-p $out_flag --dangerously-skip-permissions"
  fi
}

# EXP-token-efficiency — opt-in toggles for the three token-efficiency layers.
# Same precedence ladder as cost_tracking_enabled: env var first, then JSON
# (.agents.<flag>), default off. Live read on every call so flipping the flag
# mid-flight doesn't require a queue-loop restart.

# headroom_wrap_enabled: prefix `headroom wrap ` on every claude invocation.
# Used by claude_cmd_for_stage above.
headroom_wrap_enabled() {
  [ "${BUREAU_HEADROOM_WRAP:-}" = "1" ] && return 0
  command -v jq >/dev/null 2>&1 || return 1
  [ "$(jq -r '.agents.headroom_wrap // false' "${BUREAU_CONFIG:-.bureau.json}" 2>/dev/null)" = "true" ]
}

# use_goal_loop_enabled: implement-pipeline.sh drives via `claude -p "/goal …"`
# instead of the bash for-loop when this is true. Closes the EXP-573 / EXP-571
# / EXP-624 / EXP-627 stuck-detector tangle structurally.
use_goal_loop_enabled() {
  [ "${BUREAU_USE_GOAL_LOOP:-}" = "1" ] && return 0
  command -v jq >/dev/null 2>&1 || return 1
  [ "$(jq -r '.agents.use_goal_loop // false' "${BUREAU_CONFIG:-.bureau.json}" 2>/dev/null)" = "true" ]
}

# caveman_level: returns one of off | lite | full | ultra | wenyan. Read by
# SKILL.md Phase 5.5 at /bureau-init time AND by per-stage scripts that may
# prefix `/caveman <level>` to a prompt (review prose only — never commit
# messages or PR bodies). Default off.
caveman_level() {
  if [ -n "${BUREAU_CAVEMAN_LEVEL:-}" ]; then
    printf '%s' "$BUREAU_CAVEMAN_LEVEL"
    return
  fi
  command -v jq >/dev/null 2>&1 || { printf 'off'; return; }
  local level
  level=$(jq -r '.agents.caveman_level // "off"' "${BUREAU_CONFIG:-.bureau.json}" 2>/dev/null)
  case "$level" in
    off|lite|full|ultra|wenyan) printf '%s' "$level" ;;
    *) printf 'off' ;;
  esac
}

# EXP-671 — opt-in per-stage cost/token tracking. Default OFF (the pipeline is
# byte-identical). Enable via BUREAU_COST_TRACKING=1 or .bureau.json
# session.cost_tracking=true.
cost_tracking_enabled() {
  [ "${BUREAU_COST_TRACKING:-}" = "1" ] && return 0
  command -v jq >/dev/null 2>&1 || return 1
  [ "$(jq -r '.session.cost_tracking // false' "${BUREAU_CONFIG:-.bureau.json}" 2>/dev/null)" = "true" ]
}

# EXP-671 — append one stage's token usage + est. $ to a per-issue cost log.
# Pipelines call this once after each claude invocation:
#   record_stage_cost "$RESULT" "$ISSUE" "implement"
# No-op when cost tracking is off, jq is missing, or the output carries no usage
# envelope (codex / --print) — so it's safe to call unconditionally.
# Log: $BUREAU_COST_DIR (default ~/.bureau/cost)/<issue>.jsonl. The legacy
# default ~/.brainhuggers/bureau-cost still works if set explicitly via the env.
record_stage_cost() {
  cost_tracking_enabled || return 0
  command -v jq >/dev/null 2>&1 || return 0
  local raw="$1" issue="$2" stage="$3"
  local usage
  usage=$(printf '%s' "$raw" | jq -c 'if type=="object" and has("usage") then .usage else empty end' 2>/dev/null)
  [ -z "$usage" ] && return 0
  local in out cost dir
  in=$(printf '%s' "$usage" | jq -r '.input_tokens // 0' 2>/dev/null)
  out=$(printf '%s' "$usage" | jq -r '.output_tokens // 0' 2>/dev/null)
  cost=$(printf '%s' "$raw" | jq -r '.total_cost_usd // 0' 2>/dev/null)
  dir="${BUREAU_COST_DIR:-$HOME/.bureau/cost}"
  mkdir -p "$dir" 2>/dev/null || return 0
  printf '{"issue":"%s","stage":"%s","input_tokens":%s,"output_tokens":%s,"cost_usd":%s}\n' \
    "$issue" "$stage" "${in:-0}" "${out:-0}" "${cost:-0}" >> "$dir/$issue.jsonl"
}

# EXP-671 — aggregate the per-issue cost logs into a report. Used by
# `bureau-status.sh --cost`. Prints a per-issue / per-stage token + $ summary.
report_costs() {
  local dir="${BUREAU_COST_DIR:-$HOME/.bureau/cost}"
  if [ ! -d "$dir" ] || [ -z "$(ls -A "$dir" 2>/dev/null)" ]; then
    echo "No cost data yet. Enable with session.cost_tracking=true (or BUREAU_COST_TRACKING=1) and run the pipeline."
    return 0
  fi
  command -v jq >/dev/null 2>&1 || { echo "jq required for the cost report"; return 1; }
  echo "Bureau cost report (per issue → per stage)"
  echo "──────────────────────────────────────────"
  local f issue
  for f in "$dir"/*.jsonl; do
    [ -f "$f" ] || continue
    issue=$(basename "$f" .jsonl)
    jq -rs --arg issue "$issue" '
      (group_by(.stage) | map({stage: .[0].stage,
         in: (map(.input_tokens) | add), out: (map(.output_tokens) | add),
         cost: (map(.cost_usd) | add)})) as $byStage
      | "\($issue):",
        ($byStage[] | "  \(.stage): \(.in) in · \(.out) out · $\(.cost * 1000 | round / 1000)"),
        "  TOTAL: $\((($byStage | map(.cost) | add) * 1000 | round / 1000))"
    ' "$f"
  done
}

# Helper: check if agent is enabled.
# Shepherd (and other end-to-end drivers) export BUREAU_FORCE_ALL_AGENTS=1 to
# force every stage on regardless of `.agents.<stage>` toggles — for "really
# end to end" runs that want to route through every configured state.
# Conditional state-presence checks (e.g. `[ -n "$BUREAU_STATE_QA" ]`) still
# apply: force-all only overrides the agent toggle, not state configuration.
agent_enabled() {
  [ "${BUREAU_FORCE_ALL_AGENTS:-0}" = "1" ] && return 0
  local val
  val=$(bureau_get ".agents.$1 // false")
  [ "$val" != "false" ] && [ "$val" != "null" ]
}

# ── Linear glue helpers (EXP-412) ──────────────────────────────────
# These replace the legacy pattern of spawning `claude -p` + remote Linear MCP
# to perform routine CRUD. Remote MCP uses short-lived OAuth tokens that can't
# be refreshed from headless subprocesses — the first cron tick worked, every
# subsequent tick failed silently. Direct GraphQL + LINEAR_API_KEY is stable,
# fast, and free of hidden Claude token burn.

# Resolve "EXP-123" → Linear UUID. Caches nothing — re-queried per call.
_resolve_issue_uuid() {
  local ref="$1"
  if [[ "$ref" =~ ^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$ ]]; then
    printf '%s' "$ref"
    return 0
  fi
  local team_key="${ref%%-*}"
  local number="${ref##*-}"
  linear_query "{ issues(filter: { team: { key: { eq: \\\"$team_key\\\" } }, number: { eq: $number } }) { nodes { id } } }" \
    | jq -r '.data.issues.nodes[0].id // empty'
}

# Move an issue to a new state.
# Usage: move_issue <issue-id-or-key> <state-uuid>
#
# Dry-run: BUREAU_DRY_RUN=1 logs the intent and returns 0 without hitting
# Linear. Same shape on post_comment, add_issue_label, alert_telegram so a
# developer can point a fresh checkout at a real Linear team and watch the
# pipeline without polluting state.
move_issue() {
  local ref="$1" state_id="$2"
  if [ "${BUREAU_DRY_RUN:-0}" = "1" ]; then
    echo "[DRY_RUN] move_issue $ref → $state_id" >&2
    return 0
  fi
  local uuid
  uuid=$(_resolve_issue_uuid "$ref")
  if [ -z "$uuid" ]; then
    echo "move_issue: could not resolve $ref to UUID" >&2
    return 1
  fi
  local payload
  payload=$(jq -n --arg id "$uuid" --arg sid "$state_id" \
    '{query: "mutation($id: String!, $sid: String!) { issueUpdate(id: $id, input: { stateId: $sid }) { success } }",
      variables: {id: $id, sid: $sid}}')
  local result
  result=$(linear_raw "$payload")
  local ok
  ok=$(printf '%s' "$result" | jq -r '.data.issueUpdate.success // false')
  if [ "$ok" != "true" ]; then
    echo "move_issue: $ref → $state_id failed: $result" >&2
    return 1
  fi
}

# Post a markdown comment to an issue.
# Usage: post_comment <issue-id-or-key> <body>
post_comment() {
  local ref="$1" body="$2"
  if [ "${BUREAU_DRY_RUN:-0}" = "1" ]; then
    echo "[DRY_RUN] post_comment $ref ($(printf '%s' "$body" | head -c 80 | tr '\n' ' ')...)" >&2
    return 0
  fi
  local uuid
  uuid=$(_resolve_issue_uuid "$ref")
  if [ -z "$uuid" ]; then
    echo "post_comment: could not resolve $ref to UUID" >&2
    return 1
  fi
  local payload
  payload=$(jq -n --arg id "$uuid" --arg body "$body" \
    '{query: "mutation($id: String!, $body: String!) { commentCreate(input: { issueId: $id, body: $body }) { success } }",
      variables: {id: $id, body: $body}}')
  local result
  result=$(linear_raw "$payload")
  local ok
  ok=$(printf '%s' "$result" | jq -r '.data.commentCreate.success // false')
  if [ "$ok" != "true" ]; then
    echo "post_comment: $ref failed: $result" >&2
    return 1
  fi
}

# Resolve the working branch for an issue.
# Lookup order:
#   1. A bureau-branch marker comment posted by the spec pipeline. The marker
#      MUST be the first line of the comment body:
#        <!-- bureau-branch: 001-automated-tests -->
#        **Spec Artifacts — EXP-123**
#        ...
#      Newest wins if multiple marker comments exist. Anchoring to the first
#      line avoids false positives from documentation/review comments that
#      quote the marker pattern in prose or code blocks.
#   2. Fallback to Linear's auto-generated branchName (rarely matches the
#      sequential spec-number branches the pipeline uses, but better than
#      empty).
# Output: branch name on stdout, empty if nothing found.
get_issue_branch() {
  local ref="$1"
  local team_key="${ref%%-*}"
  local number="${ref##*-}"
  local data
  # first: 200 — covers virtually every long-running issue (REQUEST_CHANGES
  # cycles + bot pings rarely exceed this). The marker is posted once by the
  # spec pipeline near the top of the comment list; if it falls off the page,
  # downstream pipelines silently fall back to Linear's branchName which never
  # matches the sequential spec branch numbers.
  data=$(linear_query "{ issues(filter: { team: { key: { eq: \\\"$team_key\\\" } }, number: { eq: $number } }) { nodes { branchName comments(first: 200) { nodes { body createdAt } } } } }")
  local marker
  marker=$(printf '%s' "$data" \
    | jq -r '
      (.data.issues.nodes[0].comments.nodes // [])
      | sort_by(.createdAt) | reverse
      | map(.body | split("\n")[0])
      | map(select(test("^<!-- bureau-branch: [^ ]+ -->[[:space:]]*$")))
      | .[0] // ""
    ' \
    | sed -E 's/^<!-- bureau-branch: //; s/ -->[[:space:]]*$//')
  if [ -n "$marker" ]; then
    printf '%s' "$marker"
    return 0
  fi
  printf '%s' "$data" | jq -r '.data.issues.nodes[0].branchName // empty'
}

# Combined fetch: returns { branch, comments } in one GraphQL roundtrip.
# Use when a caller needs both pieces close together (e.g. implement-pipeline
# resolves the branch then scans for review feedback). The branch is resolved
# the same way get_issue_branch resolves it (marker comment, newest wins,
# fallback to Linear's branchName). Comments are sorted newest-first to match
# get_issue_comments.
#
# Output: { "branch": "<resolved>", "comments": [{body, createdAt}, ...] }
# Both fields populated even if the issue has no comments.
get_issue_branch_and_comments() {
  local ref="$1"
  local team_key="${ref%%-*}"
  local number="${ref##*-}"
  linear_query "{ issues(filter: { team: { key: { eq: \\\"$team_key\\\" } }, number: { eq: $number } }) { nodes { branchName comments(first: 200) { nodes { body createdAt } } } } }" \
    | jq '
      (.data.issues.nodes[0] // {}) as $issue
      | (($issue.comments.nodes // []) | sort_by(.createdAt) | reverse) as $comments
      | (
          $comments
          | map(.body | split("\n")[0])
          | map(select(test("^<!-- bureau-branch: [^ ]+ -->[[:space:]]*$")))
          | .[0] // ""
          | sub("^<!-- bureau-branch: "; "")
          | sub(" -->[[:space:]]*$"; "")
        ) as $marker
      | { branch: (if $marker != "" then $marker else ($issue.branchName // "") end),
          comments: $comments }
    '
}

# Return issue comments newest-first as a JSON array of objects {body, createdAt}.
get_issue_comments() {
  local ref="$1"
  local team_key="${ref%%-*}"
  local number="${ref##*-}"
  linear_query "{ issues(filter: { team: { key: { eq: \\\"$team_key\\\" } }, number: { eq: $number } }) { nodes { comments(first: 50) { nodes { body createdAt } } } } }" \
    | jq '(.data.issues.nodes[0].comments.nodes // []) | sort_by(.createdAt) | reverse'
}

# Return full issue detail as JSON: {identifier, title, description, project:{name,description}, labels:[names]}.
get_issue_detail() {
  local ref="$1"
  local team_key="${ref%%-*}"
  local number="${ref##*-}"
  linear_query "{ issues(filter: { team: { key: { eq: \\\"$team_key\\\" } }, number: { eq: $number } }) { nodes { identifier title description project { name description } labels { nodes { name } } } } }" \
    | jq '(.data.issues.nodes[0] // {}) | {identifier, title, description, project: (.project // {name: null, description: null}), labels: ((.labels.nodes // []) | map(.name))}'
}

# Return issue state name as plain string (used for "is it still in X?" guards).
get_issue_state() {
  local ref="$1"
  local team_key="${ref%%-*}"
  local number="${ref##*-}"
  linear_query "{ issues(filter: { team: { key: { eq: \\\"$team_key\\\" } }, number: { eq: $number } }) { nodes { state { name } } } }" \
    | jq -r '.data.issues.nodes[0].state.name // empty'
}

# Add a label (by name) to an issue.
# Usage: add_issue_label <issue-id-or-key> <label-name>
add_issue_label() {
  local ref="$1" name="$2"
  if [ "${BUREAU_DRY_RUN:-0}" = "1" ]; then
    echo "[DRY_RUN] add_issue_label $ref += '$name'" >&2
    return 0
  fi
  local uuid
  uuid=$(_resolve_issue_uuid "$ref")
  if [ -z "$uuid" ]; then
    echo "add_issue_label: could not resolve $ref" >&2
    return 1
  fi
  local label_id
  label_id=$(linear_query "{ issueLabels(filter: { name: { eq: \\\"$name\\\" } }, first: 1) { nodes { id } } }" \
    | jq -r '.data.issueLabels.nodes[0].id // empty')
  if [ -z "$label_id" ]; then
    echo "add_issue_label: no label named '$name'" >&2
    return 1
  fi
  local payload
  payload=$(jq -n --arg id "$uuid" --arg lid "$label_id" \
    '{query: "mutation($id: String!, $lid: String!) { issueAddLabel(id: $id, labelId: $lid) { success } }",
      variables: {id: $id, lid: $lid}}')
  local ok
  ok=$(linear_raw "$payload" | jq -r '.data.issueAddLabel.success // false')
  [ "$ok" = "true" ]
}

# Remove a label (by name) from an issue. Mirrors add_issue_label.
# Idempotent on both ends — Linear's issueRemoveLabel no-ops if the label
# isn't currently applied; we also return success (without calling Linear) if
# the label name doesn't exist in the workspace at all, because the caller
# wants the label absent and it definitionally is.
# Usage: remove_issue_label <issue-id-or-key> <label-name>
remove_issue_label() {
  local ref="$1" name="$2"
  if [ "${BUREAU_DRY_RUN:-0}" = "1" ]; then
    echo "[DRY_RUN] remove_issue_label $ref -= '$name'" >&2
    return 0
  fi
  local uuid
  uuid=$(_resolve_issue_uuid "$ref")
  if [ -z "$uuid" ]; then
    echo "remove_issue_label: could not resolve $ref" >&2
    return 1
  fi
  local label_id
  label_id=$(linear_query "{ issueLabels(filter: { name: { eq: \\\"$name\\\" } }, first: 1) { nodes { id } } }" \
    | jq -r '.data.issueLabels.nodes[0].id // empty')
  [ -z "$label_id" ] && return 0
  local payload
  payload=$(jq -n --arg id "$uuid" --arg lid "$label_id" \
    '{query: "mutation($id: String!, $lid: String!) { issueRemoveLabel(id: $id, labelId: $lid) { success } }",
      variables: {id: $id, lid: $lid}}')
  local ok
  ok=$(linear_raw "$payload" | jq -r '.data.issueRemoveLabel.success // false')
  [ "$ok" = "true" ]
}

# branch_is_bureau_only: returns 0 if every commit in
# `origin/main..origin/<branch>` is bureau-generated, 1 if even one
# human-authored commit is in the divergence. Used by rebase-pipeline
# (refuse-to-rebase guard) and merge-pipeline (decide whether to label
# `rebase-needed` on DIRTY PRs).
#
# A commit counts as bureau-generated if ANY of these hold:
#   1. Carries a `Co-authored-by: …Claude…` trailer (implementation commits).
#   2. Is a merge commit (2+ parents) — merge_origin_main_or_abort produces
#      these and they integrate content rather than author it.
#   3. Subject matches `^[A-Z]+-[0-9]+: spec artifacts$` — spec-pipeline.sh
#      autonomous commit (legacy; newer spec commits also carry the trailer).
#
# Caller is responsible for `git fetch origin` beforehand — keeping the fetch
# out lets callers batch it with their own fetches.
#
# Usage: if branch_is_bureau_only "$BRANCH"; then …; fi
branch_is_bureau_only() {
  local branch="$1"
  local human_commits
  human_commits=$(_bureau_human_commits "$branch")
  [ -z "$human_commits" ]
}

# Shared helper: prints SHAs of every commit in origin/main..origin/<branch>
# that does NOT match any of the three bureau-safe categories above. Used by
# branch_is_bureau_only (existence check) and rebase-pipeline.sh (diagnostic
# print when the predicate fails). Keeping the awk in one place ensures the
# refusal message lists exactly the commits the predicate considered human.
_bureau_human_commits() {
  local branch="$1"
  # tolower() rather than gawk-only IGNORECASE so the helper works under BSD
  # awk (macOS) and gawk (Linux CI) alike.
  git log "origin/main..origin/$branch" \
    --format='%H|%P|%s|%(trailers:key=Co-authored-by,valueonly,separator=,)' 2>/dev/null \
    | awk -F'|' '
        {
          n = split($2, parents, " ")
          is_merge   = (n > 1)
          is_spec    = (tolower($3) ~ /^[a-z]+-[0-9]+: spec artifacts$/)
          has_claude = (tolower($4) ~ /claude/)
          if (!is_merge && !is_spec && !has_claude) print $1
        }'
}

# ── Pre-merge correctness gates (Not-Rocket-Science Rule) ─────────
# `mergeStateStatus == CLEAN` is GitHub's heuristic — async-cached and lax
# when branch protection isn't configured. CLEAN passes when no required
# checks have *completed*, including the case where CI hasn't started yet.
# Bureau enforces green-CI and up-to-date-base independently of GitHub's
# status, so the merge gate doesn't depend on per-repo branch protection.

# Cache the gh-resolved owner/repo for the script lifetime (multiple helpers
# below call it; the underlying `gh` call hits the network).
_bureau_gh_owner_repo() {
  if [ -z "${_BUREAU_OWNER_REPO_CACHE:-}" ]; then
    _BUREAU_OWNER_REPO_CACHE=$(gh repo view --json nameWithOwner --jq .nameWithOwner 2>/dev/null || echo "")
  fi
  printf '%s' "$_BUREAU_OWNER_REPO_CACHE"
}

# pr_ci_is_green <pr-number>
# Returns 0 if every check-run AND legacy status context on the PR's CURRENT
# head SHA is completed and successful (success/skipped/neutral). Returns 1
# with a stderr diagnostic if any check is pending, in-progress, or failing.
#
# Independent of `mergeStateStatus`. Treats "no checks completed" as failure
# unless .agents.merge_min_required_checks is set to 0 (default 1 — repos
# without CI should opt out via .agents.merge_require_green_ci=false rather
# than via this knob).
pr_ci_is_green() {
  local pr="$1"
  local owner_repo head_sha
  owner_repo=$(_bureau_gh_owner_repo)
  [ -z "$owner_repo" ] && { echo "ci: cannot resolve owner/repo" >&2; return 1; }
  head_sha=$(gh pr view "$pr" --json headRefOid --jq .headRefOid 2>/dev/null || echo "")
  [ -z "$head_sha" ] && { echo "ci: cannot resolve head SHA for #$pr" >&2; return 1; }

  # check-runs: paginate-and-slurp; `--paginate --jq` returns per-page
  # filtered output which loses the aggregation. The `-s` jq slurps all
  # response bodies into one array, then we concat their check_runs.
  local checks
  checks=$(gh api "repos/$owner_repo/commits/$head_sha/check-runs" --paginate 2>/dev/null \
            | jq -s 'map(.check_runs // []) | add // []') || {
    echo "ci: gh check-runs query failed for $head_sha" >&2
    return 1
  }

  # Legacy commit status (for status contexts not registered as check-runs,
  # e.g. some third-party CI integrations). The endpoint returns a flat
  # `statuses` array per-context with state ∈ {success,pending,failure,error}.
  local statuses
  statuses=$(gh api "repos/$owner_repo/commits/$head_sha/status" --jq '.statuses // []' 2>/dev/null || echo '[]')

  local pending failed completed pending_legacy failed_legacy
  pending=$(echo "$checks"   | jq '[.[] | select(.status != "completed")] | length')
  completed=$(echo "$checks" | jq '[.[] | select(.status == "completed")] | length')
  failed=$(echo "$checks"    | jq '[.[]
    | select(.status == "completed")
    | select((.conclusion // "") as $c
        | $c != "success" and $c != "skipped" and $c != "neutral")
    ] | length')
  pending_legacy=$(echo "$statuses" | jq '[.[] | select(.state == "pending")] | length')
  failed_legacy=$(echo "$statuses"  | jq '[.[] | select(.state == "failure" or .state == "error")] | length')

  if [ "$pending" -gt 0 ] || [ "$pending_legacy" -gt 0 ]; then
    echo "ci: $((pending + pending_legacy)) check(s) still pending on $head_sha" >&2
    return 1
  fi
  if [ "$failed" -gt 0 ] || [ "$failed_legacy" -gt 0 ]; then
    local fail_names legacy_names
    fail_names=$(echo "$checks"      | jq -r '[.[]
      | select(.status == "completed")
      | select((.conclusion // "") as $c
          | $c != "success" and $c != "skipped" and $c != "neutral")
      | .name] | join(", ")')
    legacy_names=$(echo "$statuses"  | jq -r '[.[] | select(.state == "failure" or .state == "error") | .context] | join(", ")')
    local all=""
    [ -n "$fail_names"   ] && all="$fail_names"
    [ -n "$legacy_names" ] && all="${all:+$all, }$legacy_names"
    echo "ci: failing check(s) on $head_sha: $all" >&2
    return 1
  fi
  local min_required
  min_required=$(bureau_get '.agents.merge_min_required_checks // 1')
  local total_completed=$((completed))
  # Count completed legacy statuses too (any non-pending state counts).
  total_completed=$((total_completed + $(echo "$statuses" | jq '[.[] | select(.state != "pending")] | length')))
  if [ "$total_completed" -lt "$min_required" ]; then
    echo "ci: only $total_completed completed check(s) on $head_sha (require >= $min_required)" >&2
    return 1
  fi
  return 0
}

# pr_base_is_current <pr-number>
# Returns 0 iff the PR's base ref OID equals the current HEAD of its base
# branch on origin. Returns 1 with a stderr diagnostic and the "behind by N"
# count if not. Catches the stale-base race that mergeStateStatus's async
# cache misses.
pr_base_is_current() {
  local pr="$1"
  local owner_repo base_ref base_pr_sha base_head_sha behind
  owner_repo=$(_bureau_gh_owner_repo)
  [ -z "$owner_repo" ] && { echo "base: cannot resolve owner/repo" >&2; return 1; }
  base_ref=$(gh pr view "$pr" --json baseRefName --jq .baseRefName 2>/dev/null || echo "")
  base_pr_sha=$(gh pr view "$pr" --json baseRefOid --jq .baseRefOid 2>/dev/null || echo "")
  [ -z "$base_ref" ] || [ -z "$base_pr_sha" ] && {
    echo "base: cannot resolve baseRefName/baseRefOid for #$pr" >&2
    return 1
  }
  base_head_sha=$(gh api "repos/$owner_repo/branches/$base_ref" --jq .commit.sha 2>/dev/null || echo "")
  [ -z "$base_head_sha" ] && {
    echo "base: cannot resolve $base_ref HEAD on origin" >&2
    return 1
  }
  if [ "$base_pr_sha" = "$base_head_sha" ]; then
    return 0
  fi
  behind=$(gh api "repos/$owner_repo/compare/$base_pr_sha...$base_head_sha" --jq .ahead_by 2>/dev/null || echo "?")
  echo "base: PR #$pr is $behind commit(s) behind $base_ref (PR base=$base_pr_sha, $base_ref=$base_head_sha)" >&2
  return 1
}

# ── Observability helpers (EXP-414) ────────────────────────────────
# Shared throttle: returns 0 if the event for $key fired within the last
# $window_sec seconds (caller should suppress), 1 if not seen recently
# (caller should fire AND will record). On the "fire" path, the caller calls
# _throttle_record. Two-step so callers can decide what to log on suppression.
#
# Used by alert_telegram and merge_origin_main_or_abort to keep retry loops
# from spamming Telegram or Linear.
_throttle_should_suppress() {
  local key="$1" window_sec="${2:-3600}"
  local throttle_log="/tmp/bureau-alerts.log"
  [ ! -f "$throttle_log" ] && return 1
  local last now delta
  last=$(awk -F'\t' -v k="$key" '$1==k{print $2}' "$throttle_log" | tail -1)
  [ -z "$last" ] && return 1
  now=$(date +%s)
  delta=$((now - last))
  [ "$delta" -lt "$window_sec" ]
}

_throttle_record() {
  local key="$1"
  local throttle_log="/tmp/bureau-alerts.log"
  local now
  now=$(date +%s)
  printf '%s\t%s\n' "$key" "$now" >> "$throttle_log"
  # Cap log at 1000 lines so a long-running session doesn't leave an unbounded
  # file in /tmp. Trim is cheap and runs at most once per fired event.
  local lines
  lines=$(wc -l < "$throttle_log" 2>/dev/null | tr -d ' ' || echo 0)
  if [ "${lines:-0}" -gt 1000 ]; then
    tail -n 500 "$throttle_log" > "${throttle_log}.tmp" 2>/dev/null \
      && mv "${throttle_log}.tmp" "$throttle_log"
  fi
}

# ── Session-usage throttling (EXP-670) ──────────────────────────────
# Pause before a work unit when session usage is near the limit, so unattended
# executor/shepherd runs don't exhaust quota mid-build. GRACEFULLY NO-OPS when no
# usage signal is available — never block work just because the signal is missing
# (adapt to the host project's signals, don't hard-depend on ClaudeWatch).
#
# Signal file contract (JSON): { "pct": <0-100>, "reset_epoch": <unix>,
# "updated_epoch": <unix> }. Read from $BUREAU_USAGE_FILE
# (default ~/.bureau/session-usage.json), then ClaudeWatch
# (~/.claude/claudewatch-usage.json) parsed leniently as a fallback. The
# legacy $BRAINHUGGERS_USAGE_FILE env name is still honoured as a third
# fallback for existing operators. Wire a producer (ClaudeWatch, or a host
# UsageTracker → the usage file) to make it live.
#
# Config (.bureau.json): session.usage_threshold_pct (default 80),
# session.pause_on_stale_data (default false). Staleness window: 5 min.

# Portable "HH:MM for an epoch" (BSD `date -r` / GNU `date -d @`).
_epoch_hm() {
  date -r "$1" +%H:%M 2>/dev/null || date -d "@$1" +%H:%M 2>/dev/null || echo "?"
}

# Echo "pct|reset_epoch|updated_epoch" from the first available signal, else
# nothing. Lenient field aliases cover our file + ClaudeWatch-ish shapes.
_session_usage_signal() {
  command -v jq >/dev/null 2>&1 || return 0
  local f
  for f in "${BUREAU_USAGE_FILE:-$HOME/.bureau/session-usage.json}" \
           "$HOME/.claude/claudewatch-usage.json" \
           "${BRAINHUGGERS_USAGE_FILE:-$HOME/.brainhuggers/session-usage.json}"; do
    [ -f "$f" ] || continue
    local out
    out=$(jq -r '
      ( .pct // .usage_pct // .percent // .used_pct // empty ) as $p
      | ( .reset_epoch // .reset_at_epoch // .resets_at_epoch // 0 ) as $r
      | ( .updated_epoch // .timestamp // .updated_at_epoch // 0 ) as $u
      | if $p == null then empty else "\($p)|\($r)|\($u)" end
    ' "$f" 2>/dev/null)
    [ -n "$out" ] && { echo "$out"; return 0; }
  done
  return 0
}

# Pure decision (no sleep, no I/O) — testable in isolation. Echoes one of:
#   "proceed"      below threshold, or stale-and-not-configured-to-pause
#   "pause <sec>"  over threshold; <sec> until reset (bounded to 1h per sleep)
# Args: pct reset_epoch updated_epoch threshold stale_pause now
_throttle_decide() {
  local pct="$1" reset="$2" upd="$3" threshold="$4" stale_pause="$5" now="$6"
  if [ "${upd:-0}" -gt 0 ] && [ "$((now - upd))" -gt 300 ] && [ "$stale_pause" != "true" ]; then
    echo "proceed"; return 0
  fi
  if ! [ "${pct%%.*}" -ge "$threshold" ] 2>/dev/null; then
    echo "proceed"; return 0
  fi
  local sec=300
  [ "${reset:-0}" -gt "$now" ] && sec=$((reset - now + 5))
  [ "$sec" -gt 3600 ] && sec=3600
  echo "pause $sec"
}

# The guard wired before each work unit. Loops decide→sleep until under
# threshold (bounded so a never-clearing signal can't hang the pipeline forever).
session_throttle_guard() {
  [ "${BUREAU_DISABLE_THROTTLE:-0}" = "1" ] && return 0
  command -v jq >/dev/null 2>&1 || return 0
  local cfg="${BUREAU_CONFIG:-.bureau.json}"
  local threshold stale_pause
  threshold=$(jq -r '.session.usage_threshold_pct // 80' "$cfg" 2>/dev/null || echo 80)
  stale_pause=$(jq -r '.session.pause_on_stale_data // false' "$cfg" 2>/dev/null || echo false)

  local iters=0
  while :; do
    local sig
    sig=$(_session_usage_signal)
    if [ -z "$sig" ]; then
      if [ -z "${_THROTTLE_NOSIGNAL_LOGGED:-}" ]; then
        echo "[throttle] no usage signal — proceeding (set up ClaudeWatch or the usage-file hook to enable pausing)" >&2
        _THROTTLE_NOSIGNAL_LOGGED=1
      fi
      return 0
    fi
    local pct rest reset upd now decision
    pct="${sig%%|*}"; rest="${sig#*|}"; reset="${rest%%|*}"; upd="${rest##*|}"
    now=$(date +%s)
    decision=$(_throttle_decide "$pct" "$reset" "$upd" "$threshold" "$stale_pause" "$now")
    case "$decision" in
      proceed) return 0 ;;
      pause\ *)
        local sec="${decision#pause }"
        echo "[throttle] usage ${pct}% ≥ ${threshold}% — pausing ${sec}s until ~$(_epoch_hm "$((now + sec))")" >&2
        sleep "$sec"
        ;;
    esac
    iters=$((iters + 1))
    if [ "$iters" -ge 24 ]; then
      echo "[throttle] WARN paused 24× without clearing — proceeding to avoid a stuck pipeline" >&2
      return 0
    fi
  done
}

# alert_telegram: best-effort push to a Telegram chat for failure signals.
# Throttled per (issue, pipeline, exit_code) via _throttle_should_suppress.
# Requires TELEGRAM_BOT_TOKEN and TELEGRAM_ALERT_CHAT_ID in .env. Silently
# no-ops if either is missing (so dev environments don't break).
#
# Usage: alert_telegram <issue> <pipeline> <exit_code> <message> [log_tail]
alert_telegram() {
  local issue="$1" pipeline="$2" exit_code="$3" message="$4" log_tail="${5:-}"
  if [ "${BUREAU_DRY_RUN:-0}" = "1" ]; then
    echo "[DRY_RUN] alert_telegram $issue $pipeline exit=$exit_code: $message" >&2
    return 0
  fi
  local token="${TELEGRAM_BOT_TOKEN:-}"
  local chat="${TELEGRAM_ALERT_CHAT_ID:-}"
  [ -z "$token" ] && return 0
  [ -z "$chat" ] && return 0

  local throttle_key="alert|$issue|$pipeline|$exit_code"
  _throttle_should_suppress "$throttle_key" 3600 && return 0
  _throttle_record "$throttle_key"

  local body
  body=$(printf '🚨 Bureau pipeline alert\n\nIssue: %s\nPipeline: %s\nExit: %s\n\n%s' \
    "$issue" "$pipeline" "$exit_code" "$message")
  if [ -n "$log_tail" ]; then
    body=$(printf '%s\n\nLog tail:\n```\n%s\n```' "$body" "$log_tail")
  fi
  curl -s -X POST "https://api.telegram.org/bot${token}/sendMessage" \
    --data-urlencode "chat_id=${chat}" \
    --data-urlencode "parse_mode=Markdown" \
    --data-urlencode "text=${body}" >/dev/null 2>&1 || true
}

# emit_event: append one structured JSONL line to logs/events.jsonl. Auto-
# injects an ISO-8601 UTC "ts" field. Callers pass any number of key=value
# pairs; empty values are dropped so callers can pass "branch=$maybe_branch"
# unconditionally. Values matching /^-?[0-9]+$/ are stored as JSON numbers so
# exit_code / duration_s can be range-queried by /bureau-learnings.
#
# Silent-on-failure contract: any error (jq missing, disk full, perms) logs to
# stderr and returns 0. Event logging is observability, not correctness — it
# must never wedge a cron-driven pipeline.
#
# Usage:
#   emit_event "event=stage_start" "mode=$MODE" "stage=$script" "issue=$picked"
#   emit_event "event=stage_end"   "mode=$MODE" "stage=$script" "issue=$picked" \
#              "branch=$branch" "exit_code=$ec" "class=$klass" "duration_s=$dur"
emit_event() {
  local repo_dir
  if [ -n "${BUREAU_CONFIG:-}" ] && [ "${BUREAU_CONFIG:0:1}" = "/" ]; then
    repo_dir=$(dirname "$BUREAU_CONFIG")
  else
    repo_dir="$(pwd)"
  fi
  local events_log="$repo_dir/logs/events.jsonl"
  mkdir -p "$(dirname "$events_log")" 2>/dev/null || {
    echo "emit_event: cannot create $(dirname "$events_log")" >&2
    return 0
  }

  local ts
  ts=$(date -u '+%Y-%m-%dT%H:%M:%SZ')

  local args=("--arg" "ts" "$ts")
  local jq_expr='{ts:$ts'
  local pair key val
  for pair in "$@"; do
    key="${pair%%=*}"
    val="${pair#*=}"
    [ -z "$val" ] && continue
    if [[ "$val" =~ ^-?[0-9]+$ ]]; then
      args+=("--argjson" "$key" "$val")
    else
      args+=("--arg" "$key" "$val")
    fi
    jq_expr+=", ${key}:\$${key}"
  done
  jq_expr+='}'

  local json
  json=$(jq -nc "${args[@]}" "$jq_expr" 2>/dev/null) || {
    echo "emit_event: jq construct failed (args: $*)" >&2
    return 0
  }
  printf '%s\n' "$json" >> "$events_log" 2>/dev/null || {
    echo "emit_event: append failed to $events_log" >&2
    return 0
  }
  return 0
}

# log_escalation: append one tab-separated line to logs/escalations.log AND
# emit a matching JSON event via emit_event. Two sinks: the TSV file is
# regex-friendly for operator monitors (tail | grep), the JSONL firehose is
# queryable by /bureau-learnings.
#
# Line format (verbatim, tab-separated):
#   <ts>\tESCALATED\t<issue>\t<pipeline>\tcycle=<n>\treason="<text>"\tpr=<n>\tbranch=<name>
#
# Required acceptance regex:
#   ^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z\s+ESCALATED\s+([A-Z]+-\d+)\s+(\S+)\s+cycle=(\d+)\s+reason="([^"]+)"\s+pr=(\d+)\s+branch=(\S+)$
#
# Append-only, silent-on-failure, returns 0 — observability never wedges a
# cron pipeline. Callers should invoke ONLY after the Linear mutation
# (add_issue_label, comment) succeeded, so a phantom escalation isn't logged
# when the API errored.
#
# Usage: log_escalation <issue> <pipeline> <cycle> <reason> <pr> <branch>
#   cycle:  integer (0 if N/A)
#   reason: free text; embedded double quotes are collapsed to single quotes
#           so the line stays regex-matchable
#   pr:     PR number (0 if no PR)
#   branch: branch name (or "-" if N/A)
log_escalation() {
  local issue="$1" pipeline="$2" cycle="$3" reason="$4" pr="$5" branch="$6"
  local repo_dir
  if [ -n "${BUREAU_CONFIG:-}" ] && [ "${BUREAU_CONFIG:0:1}" = "/" ]; then
    repo_dir=$(dirname "$BUREAU_CONFIG")
  else
    repo_dir="$(pwd)"
  fi
  local log_file="$repo_dir/logs/escalations.log"
  mkdir -p "$(dirname "$log_file")" 2>/dev/null || return 0

  local ts
  ts=$(date -u '+%Y-%m-%dT%H:%M:%SZ')
  # Scrub embedded double quotes so they don't break the regex contract.
  # bash 3.2 (macOS default) mangles ${var//\"/\'}; tr is portable.
  local reason_clean
  reason_clean=$(printf '%s' "$reason" | tr '"' "'")

  printf '%s\tESCALATED\t%s\t%s\tcycle=%s\treason="%s"\tpr=%s\tbranch=%s\n' \
    "$ts" "$issue" "$pipeline" "$cycle" "$reason_clean" "${pr:-0}" "${branch:--}" \
    >> "$log_file" 2>/dev/null || true

  emit_event "event=escalation" "stage=$pipeline" "issue=$issue" \
    "cycle=$cycle" "reason=$reason_clean" "pr=${pr:-0}" "branch=${branch:--}"
  return 0
}

# Merge origin/main into HEAD so the current pipeline tests against current
# main, not whatever main looked like when the branch was cut. Without this,
# branches cut before recent merges produce phantom-revert diffs in the GitHub
# PR view (the diff shows reverts of commits that landed on main after the
# branch was cut) and may compile/test against a stale base — false-failing
# real fixes. The merge stays local to the pipeline's worktree; pushing it is
# benign (and harmless if the pipeline pushes later) — the PR author still
# rebases before final merge.
#
# Returns 0 if the branch is already up to date with origin/main, or the merge
# succeeded. Returns 1 on conflict — the merge is aborted and a comment is
# posted to the issue. The caller decides routing on conflict, since the
# desired next state varies (qa/code-review → Build; implement is already in
# Build → caller labels needs-human and stays).
#
# Caller must already be checked out on the branch. Helper fetches origin
# itself so its contract is self-contained — earlier callers relied on
# queue-loop having pre-fetched, which masked silent staleness when the
# pre-fetch failed (`|| true` in reset_worktree).
#
# Usage:
#   if ! merge_origin_main_or_abort "$ISSUE" "QA"; then
#     move_issue "$ISSUE" "$BUREAU_STATE_BUILD"
#     exit 17
#   fi
merge_origin_main_or_abort() {
  local issue="$1" stage_label="$2"
  git fetch origin --quiet || true
  if git merge-base --is-ancestor origin/main HEAD 2>/dev/null; then
    echo "  Branch is up to date with origin/main."
    return 0
  fi
  echo "  Branch is behind origin/main — merging origin/main..."
  if git merge --no-ff --no-edit origin/main; then
    return 0
  fi
  # Trivial-conflict auto-resolver. Legacy bureau branches predate the
  # `.gitattributes` merge drivers (`merge=ours` for `.specify/feature.json`,
  # `merge=union` for `CLAUDE.md`), so they hit the same conflicts every cycle
  # even though the resolutions are mechanical. Apply the same rules in-line:
  #   - .specify/feature.json    → keep ours (branch's feature_directory)
  #   - CLAUDE.md                → strip conflict markers (union)
  #   - .gitignore               → strip conflict markers (union)
  #   - rust/Cargo.lock          → take theirs (regenerated on next build)
  #   - logs/queue-*.log         → git rm (runtime artifacts that shouldn't be tracked)
  # If, after applying these rules, no unmerged paths remain, complete the
  # merge and return 0. Otherwise abort and fall through to the conflict-comment
  # path below — preserving the existing throttled-comment behavior for real
  # source-code conflicts.
  local conflict_files
  conflict_files=$(git diff --name-only --diff-filter=U 2>/dev/null)
  if [ -n "$conflict_files" ]; then
    local trivial_only=true
    while IFS= read -r f; do
      [ -z "$f" ] && continue
      case "$f" in
        .specify/feature.json) git checkout --ours "$f" 2>/dev/null && git add "$f" ;;
        CLAUDE.md|.gitignore)
          sed -i.bak '/^<<<<<<< HEAD$/d; /^=======$/d; /^>>>>>>> origin\/main$/d' "$f" 2>/dev/null \
            && rm -f "$f.bak" && git add "$f" ;;
        rust/Cargo.lock) git checkout --theirs "$f" 2>/dev/null && git add "$f" ;;
        logs/queue-*.log) git rm "$f" >/dev/null 2>&1 ;;
        *) trivial_only=false ;;
      esac
    done <<<"$conflict_files"
    if $trivial_only && [ -z "$(git diff --name-only --diff-filter=U 2>/dev/null)" ]; then
      git -c core.editor=true commit --no-edit >/dev/null 2>&1
      echo "  Auto-resolved trivial conflicts (feature.json/CLAUDE.md/.gitignore/Cargo.lock/logs)."
      return 0
    fi
  fi
  echo "  ERROR: merge of origin/main has conflicts. Aborting $stage_label."
  git merge --abort 2>/dev/null || true
  # Throttle the conflict comment to once per hour per issue. Without this,
  # an issue parked at needs-human (implement) re-runs the helper every tick
  # and Linear gets a comment-storm. The merge still aborts and returns 1
  # either way — only the user-facing comment is suppressed.
  local throttle_key="merge-conflict|$issue"
  if _throttle_should_suppress "$throttle_key" 3600; then
    echo "  (conflict comment suppressed — already posted within the last hour)"
  else
    _throttle_record "$throttle_key"
    post_comment "$issue" "❌ $stage_label pipeline cannot proceed — branch has conflicts with \`origin/main\`. Resolve them and re-run."
  fi
  return 1
}

# EXP-491: count issues currently in-flight between Spec (inclusive) and Done
# (exclusive). Used by spec-pipeline as a gate before picking new Triage work
# when BUREAU_MAX_CONCURRENT_ISSUES is non-zero. Issues with parking labels
# (needs-human, blocked, wip) are excluded from the count — they're already
# stalled, holding up the cap on them too would deadlock the loop.
#
# Output: integer count on stdout, "0" on any query failure (fail-open so a
# Linear hiccup doesn't block work).
count_in_flight_issues() {
  # Build a comma-separated list of in-flight state UUIDs. Optional states
  # (qa, copy, merge) are only included when configured.
  local state_ids=""
  for sid in "$BUREAU_STATE_SPEC" "$BUREAU_STATE_SPEC_REVIEW" \
             "$BUREAU_STATE_DESIGN" "$BUREAU_STATE_BUILD" \
             "$BUREAU_STATE_BUILD_REVIEW" \
             "${BUREAU_STATE_QA:-}" "${BUREAU_STATE_COPY:-}" \
             "${BUREAU_STATE_MERGE:-}"; do
    [ -n "$sid" ] && state_ids+="\"$sid\","
  done
  state_ids="${state_ids%,}"  # strip trailing comma
  [ -z "$state_ids" ] && { echo "0"; return 0; }

  local query
  query=$(printf '{ issues(filter: { team: { key: { eq: "%s" } }, state: { id: { in: [%s] } }, parent: { null: true } }, first: 250) { nodes { labels { nodes { name } } } } }' \
    "$BUREAU_TEAM_KEY" "$state_ids")

  local payload
  payload=$(jq -n --arg q "$query" '{query: $q}')

  curl -s -X POST https://api.linear.app/graphql \
    -H "Content-Type: application/json" \
    -H "Authorization: ${API_KEY:-$LINEAR_API_KEY}" \
    -d "$payload" 2>/dev/null \
  | jq '
    [(.data.issues.nodes // [])[]
     | select(
         ([(.labels.nodes // [])[].name]
          | map(select(. == "needs-human" or . == "blocked" or . == "wip"))
          | length) == 0
       )]
    | length
  ' 2>/dev/null || echo "0"
}

# Detach any worktree (other than $keep_wt) that currently holds $branch.
# Git refuses to attach the same branch to two worktrees, so when two
# pipelines touch the same spec branch back-to-back (e.g. code-review →
# rework → implement, or spec → spec-review), the later pick would fail
# with exit 128 unless the earlier worktree has released the branch. This
# releases it by switching the other worktree to detached HEAD at the same
# commit — no work is lost, the ref still points at the same sha.
#
# Usage: free_branch_from_other_worktrees <branch> <keep-worktree-path>
# Safe to call from any worktree inside the repo (git worktree list is
# repo-scoped, not cwd-scoped).
#
# Called twice per cron tick by design — once in queue-loop.sh's
# reset_worktree (protects the cron path) and once in each pipeline script
# (protects the manual-invocation path: e.g. `./implement-pipeline.sh ABC-1`
# from a workbench pane, which never touches queue-loop). Both calls are
# idempotent: detach if held, no-op if not. Removing either path's call
# would regress one of the two invocation modes — keep both.
free_branch_from_other_worktrees() {
  local branch="$1"
  local keep_wt="$2"
  [ -z "$branch" ] && return 0
  git worktree list --porcelain 2>/dev/null \
    | awk -v b="refs/heads/$branch" -v keep="$keep_wt" '
      /^worktree / { wt=$2; next }
      /^branch / {
        if ($2 == b && wt != keep) print wt
      }
    ' \
    | while read -r other; do
        [ -n "$other" ] && [ -d "$other" ] \
          && git -C "$other" checkout --detach --quiet 2>/dev/null || true
      done
}

# Hard-reset a worktree to a known state before invoking a pipeline.
# Originally in queue-loop.sh; relocated so shepherd.sh (and future single-
# shot drivers) can reuse it without sourcing the loop.
#
# Pipelines that start from main (spec-pipeline.sh) reset to origin/main.
# Pipelines that build on an existing spec branch checkout that branch.
# Either way, clean -fdx to strip any carryover.
#
# Reads $REPO_DIR from the caller's scope (every pipeline + queue-loop sets
# it before sourcing this file).
reset_worktree() {
  local wt="$1"
  local target_script="$2"
  local target_branch="${3:-}"

  if [ ! -d "$wt" ]; then
    git -C "$REPO_DIR" fetch --quiet || true
    git -C "$REPO_DIR" worktree add --detach "$wt" origin/main --quiet || true
  fi

  git -C "$wt" fetch origin --prune --quiet || true

  case "$target_script" in
    spec-pipeline.sh)
      git -C "$wt" reset --hard origin/main --quiet || true
      git -C "$wt" clean -fdx --quiet || true
      git -C "$wt" checkout --detach origin/main --quiet || true
      ;;
    spec-review-pipeline.sh|implement-pipeline.sh|code-review-pipeline.sh|ux-pipeline.sh|qa-pipeline.sh|copy-pipeline.sh|merge-pipeline.sh|rebase-pipeline.sh)
      if [ -n "$target_branch" ] \
        && git -C "$wt" rev-parse --verify "origin/$target_branch" >/dev/null 2>&1; then
        free_branch_from_other_worktrees "$target_branch" "$wt"
        git -C "$wt" checkout -B "$target_branch" "origin/$target_branch" --quiet || true
        git -C "$wt" reset --hard "origin/$target_branch" --quiet || true
      else
        git -C "$wt" reset --hard origin/main --quiet || true
        git -C "$wt" checkout --detach origin/main --quiet || true
      fi
      git -C "$wt" clean -fdx --quiet || true
      ;;
  esac
}

# Map pipeline exit code → human-readable error class (for alerts, logs,
# and shepherd's halt-classifier). Originally in queue-loop.sh; relocated
# so single-shot drivers can reuse the same exit-code protocol.
exit_class() {
  case "$1" in
    0)   echo "ok" ;;
    2)   echo "queue-empty" ;;
    10)  echo "linear-down" ;;
    11)  echo "worktree-dirty" ;;
    12)  echo "no-branch" ;;
    13)  echo "no-tasks" ;;
    14)  echo "build-failed" ;;
    15)  echo "no-pr" ;;
    16)  echo "claude-unauth" ;;
    17)  echo "rebase-needed" ;;
    18)  echo "gh-failed" ;;
    19)  echo "rebase-rejected" ;;
    *)   echo "error-$1" ;;
  esac
}

# Precondition: verify LINEAR_API_KEY works. Exit 10 on failure.
precondition_linear() {
  local out
  out=$(linear_query "{ viewer { id } }" 2>/dev/null || true)
  local id
  id=$(printf '%s' "$out" | jq -r '.data.viewer.id // empty' 2>/dev/null || true)
  if [ -z "$id" ]; then
    echo "ERROR: LINEAR_API_KEY is missing or invalid (viewer query returned no id)" >&2
    exit 10
  fi
}

# Precondition: claude CLI is authenticated. Exit 16 on failure.
# Probes claude -p with a trivial prompt to detect "Not logged in" before any
# state mutation — prevents stranding issues in Spec with zero work done.
precondition_claude_auth() {
  local out
  out=$(claude -p --print --dangerously-skip-permissions "reply with ok" 2>&1 | head -5 || true)
  if printf "%s" "$out" | grep -qi "Not logged in\|Please run /login\|authentication\|unauthorized"; then
    echo "ERROR: claude CLI is not authenticated (run /login)" >&2
    exit 16
  fi
}

# Precondition: worktree is clean (no uncommitted changes). Exit 11 on failure.
precondition_clean_worktree() {
  local dirty
  dirty=$(git status --porcelain 2>/dev/null || true)
  if [ -n "$dirty" ]; then
    echo "ERROR: worktree has uncommitted changes — pipeline refuses to run" >&2
    echo "$dirty" >&2
    exit 11
  fi
}

# Helper: pick next issue from a queue via direct Linear GraphQL.
#
# Replaces the old "spawn claude -p and ask it to pick" picker, which was
# unreliable because headless claude subprocesses can't re-auth remote MCPs
# and Linear's MCP OAuth tokens expire after ~1h.
#
# Usage:
#   pick_issue <state-uuid> <required-label-names-csv> [exclude-label-names-csv]
#
# Filters: team=$BUREAU_TEAM_KEY, state by UUID, at least one required label,
#          project ∈ $BUREAU_PROJECTS — all listed projects (if set), parent is null.
# Exclude: drops issues that carry any excluded label (filtered client-side in jq).
# Sort:    priority ASC (1=Urgent first; 0=None treated as last), then createdAt ASC.
# Output:  issue identifier on stdout, empty string if queue empty or every
#          candidate has an open blocker.
#
# Dependency awareness (EXP-437): each candidate's Linear inverseRelations are
# inspected. A candidate is skipped when any relation of type "blocks" points
# from an issue whose state.type is neither "completed" nor "canceled". The
# picker walks the sorted list and returns the first unblocked candidate. Deep
# chains fall out naturally — A blocked by B blocked by C only unlocks B once
# C is Done, then unlocks A once B is Done. Skipped candidates are logged to
# stderr with the blocker identifier(s) so stuck queues are diagnosable.
#
# Requires LINEAR_API_KEY in .env (picked up as $API_KEY by calling scripts).
pick_issue() {
  local state_id="$1"
  local required_csv="$2"
  local exclude_csv="${3:-}"

  local required_gql
  required_gql=$(printf '%s' "$required_csv" | awk -F',' '
    BEGIN{printf "["}
    {for(i=1;i<=NF;i++) if($i!="") printf "%s\"%s\"", (i>1?",":""), $i}
    END{printf "]"}
  ')

  # Project filter: every UUID in $BUREAU_PROJECTS (comma-separated) →
  # GraphQL `project: { id: { in: [...] } }`. Empty $BUREAU_PROJECTS = no
  # clause = all team projects. Same CSV→JSON-array pattern as required_gql
  # and exclude_json. Previously this used `cut -d',' -f1` which silently
  # dropped projects[1:] when an operator selected >1 project in Phase 1b.
  local project_clause=""
  if [ -n "${BUREAU_PROJECTS:-}" ]; then
    local projects_gql
    projects_gql=$(printf '%s' "$BUREAU_PROJECTS" | awk -F',' '
      BEGIN{printf "["}
      {for(i=1;i<=NF;i++) if($i!="") printf "%s\"%s\"", (i>1?",":""), $i}
      END{printf "]"}
    ')
    [ "$projects_gql" != "[]" ] && project_clause=$(printf ', project: { id: { in: %s } }' "$projects_gql")
  fi

  local query
  # first: 200 (issues) — was 50; under heavy load with many same-state issues
  # the urgent oldest ones could fall off the page before the client-side
  # priority sort ran. Bumping to 200 covers practical queue depths without
  # paginating. inverseRelations stays at 50 — practical blocker chains are short.
  query=$(printf '{ issues(filter: { team: { key: { eq: "%s" } }, state: { id: { eq: "%s" } }, labels: { some: { name: { in: %s } } }%s, parent: { null: true } }, orderBy: updatedAt, first: 200) { nodes { identifier priority createdAt labels { nodes { name } } inverseRelations(first: 50) { nodes { type issue { identifier state { type } } } } } } }' \
    "$BUREAU_TEAM_KEY" "$state_id" "$required_gql" "$project_clause")

  local payload
  payload=$(jq -n --arg q "$query" '{query: $q}')

  local exclude_json
  exclude_json=$(printf '%s' "$exclude_csv" | awk -F',' '
    BEGIN{printf "["}
    {for(i=1;i<=NF;i++) if($i!="") printf "%s\"%s\"", (i>1?",":""), $i}
    END{printf "]"}
  ')

  # Sorted candidate list, one per line: <identifier>\t<open-blockers-csv>
  # The blockers column is empty when nothing blocks the candidate.
  local candidates
  candidates=$(curl -s -X POST https://api.linear.app/graphql \
    -H "Content-Type: application/json" \
    -H "Authorization: ${API_KEY:-$LINEAR_API_KEY}" \
    -d "$payload" \
  | jq -r --argjson excl "$exclude_json" '
    (.data.issues.nodes // [])
    | map(select(
        ([(.labels.nodes // [])[].name] | map(select(. as $n | $excl | index($n))) | length) == 0
      ))
    | map(. + {_pri: (if .priority == 0 then 5 else .priority end)})
    | sort_by(._pri, .createdAt)
    | .[]
    | [ .identifier,
        ([(.inverseRelations.nodes // [])[]
          | select(.type == "blocks")
          | .issue
          | select(.state.type != "completed" and .state.type != "canceled")
          | .identifier
         ] | join(","))
      ]
    | @tsv
  ')

  # Walk in priority order; log every blocked skip and emit the first that is unblocked.
  while IFS=$'\t' read -r ident blockers; do
    [ -z "$ident" ] && continue
    if [ -n "$blockers" ]; then
      echo "pick_issue: skip $ident (open blockers: $blockers)" >&2
      continue
    fi
    printf '%s' "$ident"
    return 0
  done <<<"$candidates"
}

# ── Pipeline picker registry ───────────────────────────────────────
# Single source of truth for "which Linear queue does each pipeline drain?".
# Both queue-loop.sh's preselect (so it can resolve the spec branch and reset
# the worktree before invoking the pipeline) AND each pipeline's own picker
# call read from here. Adding a new pipeline → one row added below; the two
# call sites stay in sync automatically.
#
# Output format on stdout: <state-uuid>|<required-label-csv>|<exclude-label-csv>
#   Empty stdout = pipeline is opt-in and not configured for this repo (the
#   underlying state UUID is missing from .bureau.json). Both consumers treat
#   that as "queue empty".
#
# Per-pipeline config has lived inline at the call sites (qa/copy/merge gated
# on optional state UUIDs). Centralising it eliminates the "forgot to update
# both places" failure mode that bit the merge agent during initial wiring.
pipeline_picker_args() {
  case "$1" in
    spec-pipeline.sh)
      echo "$BUREAU_STATE_TRIAGE|$BUREAU_LABEL_LANE2_NAME|"
      ;;
    spec-review-pipeline.sh)
      echo "$BUREAU_STATE_SPEC_REVIEW|$BUREAU_LABEL_LANE2_NAME|"
      ;;
    ux-pipeline.sh)
      echo "$BUREAU_STATE_DESIGN|needs-ux,$BUREAU_LABEL_LANE2_NAME|"
      ;;
    copy-pipeline.sh)
      [ -n "${BUREAU_STATE_COPY:-}" ] && [ -n "${BUREAU_LABEL_NEEDS_COPY_NAME:-}" ] \
        && echo "$BUREAU_STATE_COPY|$BUREAU_LABEL_NEEDS_COPY_NAME,$BUREAU_LABEL_LANE2_NAME|needs-human"
      ;;
    implement-pipeline.sh)
      echo "$BUREAU_STATE_BUILD|$BUREAU_LABEL_LANE2_NAME,ai-implementable|needs-human"
      ;;
    qa-pipeline.sh)
      [ -n "${BUREAU_STATE_QA:-}" ] \
        && echo "$BUREAU_STATE_QA|$BUREAU_LABEL_LANE2_NAME,ai-implementable|needs-human"
      ;;
    code-review-pipeline.sh)
      echo "$BUREAU_STATE_BUILD_REVIEW|$BUREAU_LABEL_LANE2_NAME,ai-implementable|needs-human"
      ;;
    merge-pipeline.sh|rebase-pipeline.sh)
      [ -n "${BUREAU_STATE_MERGE:-}" ] \
        && echo "$BUREAU_STATE_MERGE|$BUREAU_LABEL_LANE2_NAME,ai-implementable|needs-human,blocked,wip"
      ;;
  esac
}

# pipeline_pick_next <script-name>
#   Reads the registry above, dispatches to pick_issue with the right args.
#   Returns the picked issue identifier on stdout, empty on queue-empty or
#   when an opt-in pipeline isn't configured for this repo.
#
# Usage in pipelines:
#   ISSUE=$(pipeline_pick_next "$(basename "$0")")
# Usage in queue-loop.sh's preselect:
#   pipeline_pick_next "$script_name"
pipeline_pick_next() {
  local args
  args=$(pipeline_picker_args "$1")
  [ -z "$args" ] && return 0
  local state required exclude
  IFS='|' read -r state required exclude <<<"$args"
  # Universal: never pick a ticket currently being driven by shepherd.sh.
  # Shepherd applies `shepherd-focused` on entry and removes it on EXIT/INT/
  # TERM — while it's set, the cron queue stays out of the way.
  if [ -n "$exclude" ]; then
    exclude="${exclude},shepherd-focused"
  else
    exclude="shepherd-focused"
  fi
  pick_issue "$state" "$required" "$exclude"
}

# ── Shared prompt helpers ──────────────────────────────────────────
# build_spec_context: assemble the "pinned decisions win" grounding that every
# stage prompt should carry. Previously only code-review-pipeline.sh built this
# inline (see EXP notes on SKIP classification). Exposed here so spec-review,
# ux, implement, qa, copy can reuse it — the discipline applies everywhere.
#
# Usage: ctx=$(build_spec_context "$SPEC_DIR")
#   $SPEC_DIR may be empty. Files that don't exist are silently omitted.
build_spec_context() {
  local spec_dir="${1:-}"
  local ctx="SCOPE DISCIPLINE — READ BEFORE ACTING:"
  [ -f "SPEC.md" ]   && ctx+=$'\n- SPEC.md (repo root) — project source of truth.'
  [ -f "CLAUDE.md" ] && ctx+=$'\n- CLAUDE.md (repo root) — conventions and non-goals.'
  if [ -n "$spec_dir" ]; then
    local f
    for f in spec.md plan.md research.md tasks.md design.md; do
      [ -f "${spec_dir}${f}" ] && ctx+=$'\n- '"${spec_dir}${f}"
    done
  fi
  ctx+=$'\n\nA proposal that contradicts a pinned decision is NOT an improvement — it is out of scope. When declining, cite the pin ("skipped: plan.md pins Go 1.22"). Do NOT resurface findings that a prior review cycle on this issue already deferred.'
  printf '%s' "$ctx"
}

# build_lessons_context: read LESSONS.md from cwd (the worktree root) and wrap
# it for inclusion in a stage prompt. Returns empty string when the file is
# absent OR contains only whitespace — so consumers can splice it
# unconditionally without producing an empty "## Learned patterns" section.
#
# /bureau-learnings writes the draft; the human curates and commits. Only the
# committed file ever reaches a pipeline prompt, because worktrees reset to
# origin/<branch> before every pick.
#
# Usage: lessons=$(build_lessons_context)
build_lessons_context() {
  local file="LESSONS.md"
  [ ! -f "$file" ] && return 0
  # Whitespace-only check: tr -d, then test empty.
  local trimmed
  trimmed=$(tr -d '[:space:]' < "$file" 2>/dev/null || true)
  [ -z "$trimmed" ] && return 0
  printf '## Learned patterns\n\nThe following are human-curated lessons from prior bureau runs. Treat as advisory, not binding — they reflect patterns observed across multiple past issues and may not all apply here. Use them to inform judgment; do not cite them as pinned decisions.\n\n%s' "$(cat "$file")"
}

# build_negative_constraints: shared "Do NOT" block injected into prompts that
# write code (implement, ux, copy, qa). Centralising avoids drift — adding a
# new constraint only touches one place. Each prompt may add its own role-
# specific constraints below this block.
build_negative_constraints() {
  cat <<'EOF'
NEGATIVE CONSTRAINTS — DO NOT:
- Edit package.json versions, lockfiles, CI config files, or .env.
- Refactor code outside the task's explicit scope.
- Delete or suppress failing tests to make the build pass.
- Re-introduce patterns that a prior review cycle on this issue declined.
- Create new top-level directories without an explicit task saying so.
EOF
}

# parse_claude_json: extract the LAST fenced ```json ... ``` block from Claude's
# stdout and pass it to jq. Every stage whose output the shell parses should
# emit such a block so the shell side is regex-free.
#
# Usage: value=$(parse_claude_json "$OUTPUT" '.verdict')
#   Returns empty string on parse failure — caller decides the fallback.
parse_claude_json() {
  local raw="$1" filter="$2"
  # EXP-671 — cost-tracking mode wraps the agent text in a `claude --output-format
  # json` envelope { "result": "<text>", "usage": {...} }. Unwrap to the inner
  # text first; plain `--print` output and codex verdicts fall through unchanged
  # (jq fails / no .result → empty → raw kept). Backward-compatible.
  local inner
  inner=$(printf '%s' "$raw" | jq -r 'if type=="object" and has("result") then .result else empty end' 2>/dev/null)
  [ -n "$inner" ] && raw="$inner"
  local block
  # awk extracts the last ```json...``` block; sed strips the fences.
  block=$(printf '%s' "$raw" \
    | awk 'BEGIN{b=""; in_block=0}
      /^```json[[:space:]]*$/ { in_block=1; b=""; next }
      /^```[[:space:]]*$/       { if (in_block) { saved=b; in_block=0 } next }
      { if (in_block) b = b $0 "\n" }
      END { print saved }')
  [ -z "$block" ] && return 0
  printf '%s' "$block" | jq -r "$filter" 2>/dev/null || true
}
