#!/bin/bash
# Test-only replacement for templates/scripts/bureau-config.sh.
#
# The real bureau-config.sh reads .bureau.json, talks to Linear over GraphQL,
# and provides the helpers every pipeline calls (post_comment, move_issue,
# add_issue_label, …). The pipeline does `source "$(dirname "$0")/bureau-config.sh"`
# at startup, so tests can substitute behaviour by placing this stub at that path
# inside a sandbox.
#
# Contract:
#   - Every Linear/git/gh helper records its call into $SANDBOX/calls.log
#     (tab-separated: helper_name<TAB>arg1<TAB>arg2<TAB>…) and returns 0.
#   - parse_claude_json is the REAL implementation — copied verbatim from
#     bureau-config.sh — so tests exercise the JSON-parsing path used in
#     production.
#   - claude_cmd_for_stage returns $FAKE_CLAUDE_BIN so the pipeline invokes
#     a fixture-driven stub instead of real Claude.
#   - All BUREAU_* env vars used by the implement pipeline are pre-set.
#   - Per-call behaviour can be tuned via env vars:
#       BUREAU_STUB_ADD_LABEL_RC=<n>   → add_issue_label returns <n> instead of 0
#       BUREAU_STUB_ISSUE_STATE=<name> → get_issue_state returns this (default Build)
#       BUREAU_STUB_BRANCH=<name>      → branch returned by get_issue_branch_and_comments
#       BUREAU_STUB_LABELS=<json>      → JSON array put into get_issue_detail.labels
#                                        (default '[]'). Used by tests that need
#                                        a label-driven branch to fire.
#       BUREAU_STUB_AGENT_ENABLED=<csv> → colon-separated list of stage names
#                                        for which agent_enabled returns 0
#                                        (true). Default: empty → always 1 (false).
set -uo pipefail

_calls_log="${SANDBOX:?SANDBOX must be set by harness}/calls.log"
: > "$_calls_log"

_record() {
  local IFS=$'\t'
  printf '%s\n' "$*" >> "$_calls_log"
}

# ── Constants ────────────────────────────────────────────────────────────
BUREAU_TEAM_KEY="EXP"
BUREAU_TEAM_ID="00000000-0000-0000-0000-000000000001"
BUREAU_TEAM_NAME="Experimental"

BUREAU_STATE_TRIAGE="state-triage"
BUREAU_STATE_SPEC="state-spec"
BUREAU_STATE_SPEC_REVIEW="state-spec-review"
BUREAU_STATE_DESIGN="state-design"
BUREAU_STATE_BUILD="state-build"
BUREAU_STATE_BUILD_REVIEW="state-build-review"
BUREAU_STATE_DONE="state-done"
BUREAU_STATE_QA="${BUREAU_STUB_STATE_QA:-}"        # empty by default → Build Review
BUREAU_STATE_COPY="${BUREAU_STUB_STATE_COPY:-}"
BUREAU_STATE_MERGE="${BUREAU_STUB_STATE_MERGE:-}"  # set to "state-merge" to enable merge/rebase pipelines

BUREAU_LABEL_LANE2="label-lane2"
BUREAU_LABEL_LANE2_NAME="lane-2"
BUREAU_LABEL_NEEDS_HUMAN="label-needs-human"
BUREAU_LABEL_NEEDS_UX="label-needs-ux"
BUREAU_LABEL_AI_IMPL="label-ai-impl"
BUREAU_LABEL_NEEDS_COPY_NAME=""

BUREAU_COPY_VOICE_FILE=""
BUREAU_POLL_INTERVAL=30
BUREAU_WORKBENCH_PANES=2
BUREAU_MAX_REVIEW_CYCLES=3
BUREAU_CODE_REVIEW_SAMPLING_THRESHOLD=500
BUREAU_MAX_CONCURRENT_ISSUES=0
BUREAU_MERGE_STRATEGY="squash"

BUREAU_BRANCH_PREFIX="feat"
BUREAU_COMMIT_PREFIX=""
BUREAU_SPECS_DIR="${SANDBOX}/specs"

BUREAU_PROJECTS=""

BUREAU_MODEL_DEFAULT=""
BUREAU_MODEL_IMPLEMENT=""

# ── Helpers ──────────────────────────────────────────────────────────────

claude_cmd_for_stage() {
  echo "${FAKE_CLAUDE_BIN:?FAKE_CLAUDE_BIN must be set by harness}"
}

# EXP-671 — the pipeline calls this after each claude invocation; no-op in the
# stub (the real one records token usage only when cost tracking is enabled).
record_stage_cost() { return 0; }

# Token-efficiency flag helpers. Defaults match the real bureau-config.sh:
# read BUREAU_* env first, otherwise off. Tests opt into the /goal path by
# exporting BUREAU_USE_GOAL_LOOP=1 before invoking the pipeline.
use_goal_loop_enabled() { [ "${BUREAU_USE_GOAL_LOOP:-}" = "1" ]; }
headroom_wrap_enabled() { [ "${BUREAU_HEADROOM_WRAP:-}" = "1" ]; }
caveman_level() { printf '%s' "${BUREAU_CAVEMAN_LEVEL:-off}"; }

agent_enabled() {
  local stage="$1"
  local enabled="${BUREAU_STUB_AGENT_ENABLED:-}"
  case ":$enabled:" in
    *":$stage:"*) return 0 ;;
    *) return 1 ;;
  esac
}

precondition_linear() { _record "precondition_linear"; return 0; }
precondition_claude_auth() { _record "precondition_claude_auth"; return 0; }

pipeline_pick_next() {
  _record "pipeline_pick_next" "$1"
  echo "${BUREAU_STUB_PICKED_ISSUE:-EXP-1}"
}

pick_issue() { pipeline_pick_next "$@"; }

get_issue_state() {
  _record "get_issue_state" "$1"
  echo "${BUREAU_STUB_ISSUE_STATE:-Build}"
}

get_issue_detail() {
  _record "get_issue_detail" "$1"
  local labels_json="${BUREAU_STUB_LABELS:-[]}"
  jq -n --arg id "$1" --argjson labels "$labels_json" \
    '{identifier:$id, title:"Test issue", description:"A test issue.",
      project:{name:"Test project", description:""}, labels:$labels}'
}

get_issue_branch_and_comments() {
  _record "get_issue_branch_and_comments" "$1"
  local branch="${BUREAU_STUB_BRANCH:-test-branch}"
  jq -n --arg b "$branch" \
    '{branch:$b, comments:[]}'
}

get_issue_branch() {
  _record "get_issue_branch" "$1"
  echo "${BUREAU_STUB_BRANCH:-test-branch}"
}

get_issue_comments() {
  _record "get_issue_comments" "$1"
  echo "[]"
}

free_branch_from_other_worktrees() { _record "free_branch_from_other_worktrees" "$1" "$2"; return 0; }
merge_origin_main_or_abort() { _record "merge_origin_main_or_abort" "$1" "$2"; return 0; }

add_issue_label() {
  _record "add_issue_label" "$1" "$2"
  return "${BUREAU_STUB_ADD_LABEL_RC:-0}"
}

remove_issue_label() {
  _record "remove_issue_label" "$1" "$2"
  return "${BUREAU_STUB_REMOVE_LABEL_RC:-0}"
}

# branch_is_bureau_only: stub returns whatever BUREAU_STUB_BUREAU_ONLY_RC
# is set to (default 0 = bureau-only). Tests that want to exercise the
# "human commits in divergence" path set this to 1.
branch_is_bureau_only() {
  _record "branch_is_bureau_only" "$1"
  return "${BUREAU_STUB_BUREAU_ONLY_RC:-0}"
}

post_comment() { _record "post_comment" "$1" "$2"; return 0; }

move_issue() { _record "move_issue" "$1" "$2"; return 0; }

build_spec_context() { echo ""; }
build_negative_constraints() { echo ""; }

emit_event() { _record "emit_event" "$@"; return 0; }

# log_escalation: stub that records the call AND writes the real TSV line to
# $SANDBOX/logs/escalations.log, so tests can assert against both the call
# sequence and the file format. Mirrors the production helper.
log_escalation() {
  local issue="$1" pipeline="$2" cycle="$3" reason="$4" pr="$5" branch="$6"
  _record "log_escalation" "$issue" "$pipeline" "$cycle" "$reason" "${pr:-0}" "${branch:--}"
  local log_file="${SANDBOX}/logs/escalations.log"
  mkdir -p "$(dirname "$log_file")" 2>/dev/null || return 0
  local ts; ts=$(date -u '+%Y-%m-%dT%H:%M:%SZ')
  local reason_clean
  reason_clean=$(printf '%s' "$reason" | tr '"' "'")
  printf '%s\tESCALATED\t%s\t%s\tcycle=%s\treason="%s"\tpr=%s\tbranch=%s\n' \
    "$ts" "$issue" "$pipeline" "$cycle" "$reason_clean" "${pr:-0}" "${branch:--}" \
    >> "$log_file" 2>/dev/null || true
  return 0
}

# parse_claude_json — REAL implementation, copied verbatim from
# templates/scripts/bureau-config.sh:921. Tests rely on the production parser
# behaviour, not a re-implementation.
parse_claude_json() {
  local raw="$1" filter="$2"
  local block
  block=$(printf '%s' "$raw" \
    | awk 'BEGIN{b=""; in_block=0}
      /^```json[[:space:]]*$/ { in_block=1; b=""; next }
      /^```[[:space:]]*$/       { if (in_block) { saved=b; in_block=0 } next }
      { if (in_block) b = b $0 "\n" }
      END { print saved }')
  [ -z "$block" ] && return 0
  printf '%s' "$block" | jq -r "$filter" 2>/dev/null || true
}
