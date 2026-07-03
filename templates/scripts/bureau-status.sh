#!/bin/bash
# Bureau status dashboard — live view of all pipeline agents.
# Reads agent config from .bureau.json.
#
# Usage:
#   bureau-status.sh             — live tmux-pane dashboard (refresh every 5s)
#   bureau-status.sh --config    — one-shot dump of the effective resolved
#                                  config (json + env + defaults), with source
#                                  annotation. Useful when "where did this
#                                  value come from?" is the question.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
source "$(dirname "$0")/bureau-config.sh"

# Source .env if present so --config can report LINEAR_API_KEY / TELEGRAM_*
# accurately. The pipeline scripts do this themselves and exit 1 if .env is
# missing because they need LINEAR_API_KEY to function; bureau-status.sh just
# *reports* on the environment, so missing .env is non-fatal — the report
# will show those secrets as UNSET, which is the truth in that case.
if [ -f .env ]; then
  set -a; source .env; set +a
elif [ -f "$REPO_DIR/.env" ]; then
  set -a; source "$REPO_DIR/.env"; set +a
fi

LOG_DIR="$REPO_DIR/logs"
# Resolution order, mirroring start-bureau-v2.sh:11-13 so a fresh-shell
# invocation of this script finds the same session start-bureau-v2.sh created:
#   1. BUREAU_SESSION — exported by start-bureau-v2.sh for its child windows
#      (so panes inside the tmux session always resolve correctly).
#   2. BUREAU_SESSION_NAME — user-facing override consulted by start-bureau-v2.sh
#      when constructing the session name.
#   3. bureau-v2-<repo-slug> — same default formula as start-bureau-v2.sh:11-12.
SESSION="${BUREAU_SESSION:-${BUREAU_SESSION_NAME:-bureau-v2-$(basename "$REPO_DIR")}}"

RESET="\033[0m"
BOLD="\033[1m"
DIM="\033[2m"
GREEN="\033[32m"
RED="\033[31m"
CYAN="\033[36m"
YELLOW="\033[33m"

# show_effective_config: dump JSON-backed + env-only + env-override knobs
# with their resolved value and source annotation. Source legend:
#   json  — value comes from .bureau.json
#   env   — value comes from an environment variable (marked with *)
#   def   — value is the in-code default (env unset, no .bureau.json key)
show_effective_config() {
  local row_label row_value row_source

  # _row: <category> <name> <source> <value>
  _row() {
    local source_tag="$3"
    local color=""
    case "$source_tag" in
      "env *") color="$YELLOW" ;;
      "json")  color="" ;;
      "def")   color="$DIM" ;;
    esac
    printf "    %-34s ${color}%-7s${RESET} %s\n" "$2" "$source_tag" "$4"
  }

  # Resolve a knob that lives in BOTH .bureau.json and an env var. If the env
  # var is set (even to empty), it wins and we mark "env *". Otherwise pull
  # from json, falling back to the default if the json key is absent.
  _resolve_env_over_json() {
    local env_var="$1" json_key="$2" default="$3"
    if [ -n "${!env_var+x}" ] && [ -n "${!env_var}" ]; then
      row_value="${!env_var}"; row_source="env *"
    else
      local v; v=$(bureau_get "$json_key // empty")
      if [ -n "$v" ]; then
        row_value="$v"; row_source="json"
      else
        row_value="$default"; row_source="def"
      fi
    fi
  }

  # Resolve an env-only knob: env if set, default otherwise.
  _resolve_env_only() {
    local env_var="$1" default="$2"
    if [ -n "${!env_var+x}" ] && [ -n "${!env_var}" ]; then
      row_value="${!env_var}"; row_source="env *"
    else
      row_value="$default"; row_source="def"
    fi
  }

  echo -e "${BOLD}══════════════════════════════════════════════════════════${RESET}"
  echo -e "${BOLD}  BUREAU EFFECTIVE CONFIG${RESET}"
  echo -e "${BOLD}══════════════════════════════════════════════════════════${RESET}"
  echo ""
  echo -e "  Source legend:  ${BOLD}json${RESET} = .bureau.json    ${YELLOW}env *${RESET} = env overrides    ${DIM}def${RESET} = in-code default"
  echo ""

  echo -e "  ${BOLD}TEAM${RESET}"
  _row team BUREAU_TEAM_KEY     "json" "$BUREAU_TEAM_KEY"
  _row team BUREAU_TEAM_NAME    "json" "$BUREAU_TEAM_NAME"
  _row team BUREAU_BRANCH_PREFIX "json" "$BUREAU_BRANCH_PREFIX"
  _row team BUREAU_SPECS_DIR    "json" "$BUREAU_SPECS_DIR"
  echo ""

  echo -e "  ${BOLD}AGENTS${RESET}  (on/off toggles)"
  local a
  for a in spec spec_review ux copy implement qa code_review merge rebase; do
    local v; v=$(bureau_get ".agents.$a // false")
    if [ "$v" = "false" ] || [ "$v" = "null" ]; then
      _row agents "$a" "json" "off"
    else
      _row agents "$a" "json" "ON"
    fi
  done
  echo ""

  echo -e "  ${BOLD}TUNING${RESET}"
  _row tuning agents.poll_interval_minutes        "json" "$BUREAU_POLL_INTERVAL"
  _row tuning agents.max_review_cycles            "json" "$BUREAU_MAX_REVIEW_CYCLES"
  _row tuning agents.max_concurrent_issues        "json" "$BUREAU_MAX_CONCURRENT_ISSUES"
  _row tuning agents.code_review_sampling_threshold "json" "$BUREAU_CODE_REVIEW_SAMPLING_THRESHOLD"
  _row tuning agents.merge_strategy               "json" "$BUREAU_MERGE_STRATEGY"
  _row tuning agents.workbench_panes              "json" "$BUREAU_WORKBENCH_PANES"
  echo ""

  echo -e "  ${BOLD}IMPLEMENT RETRY LOOP${RESET}  (env-only)"
  _resolve_env_only BUREAU_IMPL_MAX_ITER      3
  _row impl BUREAU_IMPL_MAX_ITER      "$row_source" "$row_value"
  _resolve_env_only BUREAU_IMPL_ITER_TIMEOUT  1800
  _row impl BUREAU_IMPL_ITER_TIMEOUT  "$row_source" "$row_value"
  _resolve_env_only BUREAU_IMPL_TOTAL_TIMEOUT 5400
  _row impl BUREAU_IMPL_TOTAL_TIMEOUT "$row_source" "$row_value"
  echo ""

  echo -e "  ${BOLD}SUPERVISOR${RESET}"
  _resolve_env_over_json BUREAU_SUPERVISOR_MAX_CRASHES      ".supervisor.max_crashes"      5
  _row supervisor max_crashes      "$row_source" "$row_value"
  _resolve_env_over_json BUREAU_SUPERVISOR_STABILITY_WINDOW ".supervisor.stability_window" 3600
  _row supervisor stability_window "$row_source" "$row_value"
  echo ""

  echo -e "  ${BOLD}MODELS${RESET}  (per-stage; env shortcut overrides .bureau.json)"
  _resolve_env_over_json BUREAU_MODEL_DEFAULT     ".agents.model"             "(claude CLI default)"
  _row model agents.model         "$row_source" "$row_value"
  local stage
  for stage in spec spec_review ux copy implement qa code_review merge; do
    local upper; upper=$(printf '%s' "$stage" | tr '[:lower:]' '[:upper:]')
    local env_var="BUREAU_MODEL_${upper}"
    if [ -n "${!env_var+x}" ] && [ -n "${!env_var}" ]; then
      row_value="${!env_var}"; row_source="env *"
    else
      local v; v=$(bureau_get_agent_model "$stage")
      if [ -n "$v" ]; then row_value="$v"; row_source="json"
      else row_value="(inherits default)"; row_source="def"; fi
    fi
    _row model "agents.${stage}.model" "$row_source" "$row_value"
  done
  echo ""

  echo -e "  ${BOLD}RUNTIME${RESET}"
  _resolve_env_only BUREAU_DRY_RUN      0
  _row runtime BUREAU_DRY_RUN      "$row_source" "$row_value"
  _resolve_env_only BUREAU_SESSION_NAME "(bureau-v2-$(basename "$REPO_DIR"))"
  _row runtime BUREAU_SESSION_NAME "$row_source" "$row_value"
  # Secrets: never show the actual value — just whether it's set.
  if [ -n "${LINEAR_API_KEY:-}" ]; then
    _row runtime LINEAR_API_KEY      "env *" "set"
  else
    _row runtime LINEAR_API_KEY      "def" "${RED}UNSET${RESET}  (required)"
  fi
  if [ -n "${TELEGRAM_BOT_TOKEN:-}" ]; then
    _row runtime TELEGRAM_BOT_TOKEN  "env *" "set"
  else
    _row runtime TELEGRAM_BOT_TOKEN  "def" "unset (alerts disabled)"
  fi
  if [ -n "${TELEGRAM_ALERT_CHAT_ID:-}" ]; then
    _row runtime TELEGRAM_ALERT_CHAT_ID "env *" "set"
  else
    _row runtime TELEGRAM_ALERT_CHAT_ID "def" "unset"
  fi
  echo ""

  # Activity rollup. No `claude usage` CLI exists today; this reads the local
  # Claude Code stats cache (~/.claude/stats-cache.json) — a snapshot of
  # session/message/token activity, NOT an Anthropic credit-pool balance.
  # When/if the CLI ships a real credit-balance query, swap the data source.
  echo -e "  ${BOLD}ACTIVITY${RESET}  (~/.claude/stats-cache.json — local activity proxy, not credit balance)"
  local stats_file="$HOME/.claude/stats-cache.json"
  if [ ! -f "$stats_file" ] || ! command -v jq >/dev/null 2>&1; then
    _row activity "stats-cache" "def" "unavailable (no file or jq missing)"
  else
    local seven_days_ago
    seven_days_ago=$(date -v -7d +%Y-%m-%d 2>/dev/null || date -d "-7 days" +%Y-%m-%d 2>/dev/null || echo "")
    local last_computed
    last_computed=$(jq -r '.lastComputedDate // "unknown"' "$stats_file" 2>/dev/null || echo "unknown")
    _row activity "lastComputedDate" "stats" "$last_computed"

    if [ -n "$seven_days_ago" ]; then
      local rollup
      rollup=$(jq -r --arg since "$seven_days_ago" '
        [.dailyActivity[]? | select(.date >= $since)]
        | "\(map(.messageCount) | add // 0)|\(map(.sessionCount) | add // 0)|\(map(.toolCallCount) | add // 0)"
      ' "$stats_file" 2>/dev/null || echo "0|0|0")
      local msgs sessions tools
      IFS='|' read -r msgs sessions tools <<<"$rollup"
      _row activity "7d messages"   "stats" "$msgs"
      _row activity "7d sessions"   "stats" "$sessions"
      _row activity "7d tool calls" "stats" "$tools"

      local tokens_by_model
      tokens_by_model=$(jq -r --arg since "$seven_days_ago" '
        [.dailyModelTokens[]? | select(.date >= $since)]
        | map(.tokensByModel // {}) | add // {}
        | to_entries | sort_by(-.value)
        | .[] | "\(.key)|\(.value)"
      ' "$stats_file" 2>/dev/null || true)
      if [ -n "$tokens_by_model" ]; then
        while IFS='|' read -r model tokens; do
          [ -z "$model" ] && continue
          local human
          if [ "$tokens" -ge 1000000 ] 2>/dev/null; then
            human=$(awk -v t="$tokens" 'BEGIN{printf "%.1fM", t/1000000}')
          elif [ "$tokens" -ge 1000 ] 2>/dev/null; then
            human=$(awk -v t="$tokens" 'BEGIN{printf "%dK", t/1000}')
          else
            human="$tokens"
          fi
          _row activity "7d tokens ($model)" "stats" "$human"
        done <<<"$tokens_by_model"
      fi
    fi
  fi
  echo ""
  echo -e "${BOLD}══════════════════════════════════════════════════════════${RESET}"
}

if [ "${1:-}" = "--config" ]; then
  show_effective_config
  exit 0
fi

# EXP-671 — per-issue / per-stage cost report (opt-in cost tracking).
if [ "${1:-}" = "--cost" ]; then
  report_costs
  exit 0
fi

status_color() {
  case "$1" in
    working)  echo -e "${GREEN}working${RESET}" ;;
    idle)     echo -e "${DIM}idle${RESET}" ;;
    sleeping) echo -e "${DIM}sleeping${RESET}" ;;
    done)     echo -e "${CYAN}done${RESET}" ;;
    error)    echo -e "${RED}error${RESET}" ;;
    *)        echo -e "${DIM}---${RESET}" ;;
  esac
}

parse_status() {
  local line="$1"
  if echo "$line" | grep -q "queue empty"; then echo "idle"
  elif echo "$line" | grep -q "error (exit"; then echo "error"
  elif echo "$line" | grep -qi "done\\."; then echo "done"
  elif echo "$line" | grep -q "\\.\\.\\."; then echo "working"
  elif echo "$line" | grep -q "Next check"; then echo "sleeping"
  elif echo "$line" | grep -q "Queue Worker Started"; then echo "idle"
  else echo "---"; fi
}

parse_issue() {
  echo "$1" | grep -oE '[A-Z]+-[0-9]+' | head -1 || echo ""
}

# Build agent list from config
AGENTS=()
LABELS=()
LOG_NAMES=()
WINDOWS=()
WIN=1

add_if_enabled() {
  local config_key="$1" label="$2" log_name="$3"
  local val
  val=$(bureau_get ".agents.$config_key // false")
  if [ "$val" != "false" ] && [ "$val" != "null" ]; then
    AGENTS+=("$config_key")
    LABELS+=("$label")
    LOG_NAMES+=("$log_name")
    WINDOWS+=("$WIN")
    ((WIN++))
  fi
}

add_if_enabled "spec" "spec" "spec"
add_if_enabled "spec_review" "spec-review" "spec-review"
add_if_enabled "ux" "ux" "ux"
add_if_enabled "copy" "copy" "copy"
add_if_enabled "implement" "build" "implement"
add_if_enabled "qa" "qa" "qa"

add_if_enabled "code_review" "review" "code-review"
add_if_enabled "rebase" "rebase" "rebase"
add_if_enabled "merge" "merge" "merge"

while true; do
  clear
  NOW=$(date '+%Y-%m-%d %H:%M:%S')

  echo -e "${BOLD}==============================================================${RESET}"
  echo -e "${BOLD}  BUREAU PIPELINE${RESET}                                  ${DIM}$NOW${RESET}"
  echo -e "${BOLD}==============================================================${RESET}"
  echo ""
  echo -e "  ${BOLD}PIPELINE AGENTS${RESET}  (team: $BUREAU_TEAM_NAME)"
  echo ""
  printf "  ${BOLD}%-14s %-12s %-10s %s${RESET}\n" "AGENT" "STATUS" "ISSUE" "LAST ACTIVITY"
  printf "  %-14s %-12s %-10s %s\n" "-------------" "-----------" "---------" "------------------------------"

  for i in "${!AGENTS[@]}"; do
    label="${LABELS[$i]}"
    log_name="${LOG_NAMES[$i]}"
    win="${WINDOWS[$i]}"
    log="$LOG_DIR/queue-$log_name.log"

    if [ -f "$log" ]; then
      last=$(grep -v '^---$' "$log" | grep -v '^$' | tail -1 || echo "")
      status=$(parse_status "$last")
      issue=$(parse_issue "$last")
      activity=$(echo "$last" | sed 's/\[.*\] //' | cut -c1-35)
    else
      status="---"
      issue=""
      activity="no log file"
    fi

    colored_status=$(status_color "$status")
    printf "  %-14s %-23s %-10s %s\n" "$label [$win]" "$colored_status" "${issue:----}" "${activity:----}"
  done

  echo ""
  echo -e "  ${BOLD}WORKBENCH${RESET} [$WIN]"
  echo ""

  if tmux list-panes -t "$SESSION:bench" 2>/dev/null | head -2 | while read -r pane; do true; done; then
    PANE_COUNT=$(tmux list-panes -t "$SESSION:bench" 2>/dev/null | wc -l | tr -d ' ')
    for p in $(seq 0 $((PANE_COUNT - 1))); do
      pane_cmd=$(tmux display-message -t "$SESSION:bench.$p" -p '#{pane_current_command}' 2>/dev/null || echo "---")
      pane_pid=$(tmux display-message -t "$SESSION:bench.$p" -p '#{pane_pid}' 2>/dev/null || echo "---")
      printf "  pane %-2s  %-20s ${DIM}pid %s${RESET}\n" "$p" "$pane_cmd" "$pane_pid"
    done
  else
    echo -e "  ${DIM}workbench not running${RESET}"
  fi

  echo ""
  echo -e "  ${BOLD}NAVIGATION${RESET}"
  echo -e "  ${CYAN}Ctrl+B 0${RESET} dashboard    ${CYAN}Ctrl+B 1-$((WIN-1))${RESET} agent windows"
  echo -e "  ${CYAN}Ctrl+B $WIN${RESET} workbench    ${CYAN}Ctrl+B d${RESET}   detach"
  echo ""
  echo -e "  ${BOLD}COMMANDS${RESET}"
  echo -e "  ${DIM}Stop all:${RESET}  tmux kill-session -t $SESSION"
  echo -e "  ${DIM}Tail logs:${RESET} tail -F $LOG_DIR/queue-*.log"
  echo -e "${BOLD}==============================================================${RESET}"

  sleep 5
done
