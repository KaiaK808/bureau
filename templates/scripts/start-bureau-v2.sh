#!/bin/bash
# Bureau v2: All agents + interactive workbench + dashboard in one tmux session.
# Reads agent config from .bureau.json.
set -euo pipefail

cd "$(dirname "$0")/.."
source scripts/bureau-config.sh

INTERVAL="${1:-$BUREAU_POLL_INTERVAL}"
BENCH_PANES="${2:-$BUREAU_WORKBENCH_PANES}"
REPO_SLUG="$(basename "$PWD")"
SESSION="${BUREAU_SESSION_NAME:-bureau-v2-$REPO_SLUG}"
export BUREAU_SESSION="$SESSION"

if ! command -v tmux &>/dev/null; then
  echo "ERROR: tmux required. Install with: brew install tmux"
  exit 1
fi

if ! command -v claude &>/dev/null; then
  echo "ERROR: claude CLI required."
  exit 1
fi

# Drift check: warn (non-blocking) if repo scripts differ from or are missing
# relative to the skill template. Never mutates anything.
check_script_drift() {
  local template_dir="$HOME/.claude/skills/bureau-init/templates/scripts"
  [ -d "$template_dir" ] || return 0
  local drift=0 missing=0
  # Repo scripts whose template counterpart differs
  for repo_script in scripts/*.sh; do
    [ -f "$repo_script" ] || continue
    local name template
    name="$(basename "$repo_script")"
    template="$template_dir/$name"
    if [ -f "$template" ] && ! cmp -s "$repo_script" "$template"; then
      drift=$((drift + 1))
    fi
  done
  # Template scripts not yet present in the repo
  for tmpl in "$template_dir"/*.sh; do
    [ -f "$tmpl" ] || continue
    local tname
    tname="$(basename "$tmpl")"
    [ -f "scripts/$tname" ] || missing=$((missing + 1))
  done
  if [ "$drift" -gt 0 ] || [ "$missing" -gt 0 ]; then
    echo "⚠  bureau-init template drift: $drift differ, $missing new"
    echo "   → resync with:  claude /bureau-init --resync-scripts"
    echo ""
  fi
}
check_script_drift

if tmux has-session -t bureau 2>/dev/null; then
  echo "WARNING: Old 'bureau' session is still running."
  echo "  Kill it with: tmux kill-session -t bureau"
  echo ""
fi

tmux kill-session -t "$SESSION" 2>/dev/null || true

echo "Starting Bureau v2..."
echo "  Team: $BUREAU_TEAM_NAME ($BUREAU_TEAM_KEY)"
echo "  Interval: ${INTERVAL}m"
echo "  Bench panes: $BENCH_PANES"
echo ""

mkdir -p logs

# Window 0: Status dashboard
tmux new-session -d -s "$SESSION" -n status -x 200 -y 50
tmux send-keys -t "$SESSION:status" "BUREAU_SESSION=$SESSION ./scripts/bureau-status.sh" Enter

WIN_NUM=1

add_agent_window() {
  local name="$1"
  local mode="$2"
  tmux new-window -t "$SESSION" -n "$name"
  tmux send-keys -t "$SESSION:$name" "./scripts/queue-loop-supervised.sh $mode $INTERVAL" Enter
  ((WIN_NUM++))
}

agent_enabled "spec" && add_agent_window "spec" "spec"
agent_enabled "spec_review" && add_agent_window "spec-review" "spec-review"
agent_enabled "ux" && add_agent_window "ux" "ux"
agent_enabled "copy" && add_agent_window "copy" "copy"
agent_enabled "implement" && add_agent_window "build" "implement"
agent_enabled "qa" && add_agent_window "qa" "qa"

agent_enabled "code_review" && add_agent_window "review" "code-review"
agent_enabled "rebase" && add_agent_window "rebase" "rebase"
agent_enabled "merge" && add_agent_window "merge" "merge"

# Workbench window
BENCH_WIN=$WIN_NUM
tmux new-window -t "$SESSION" -n bench
tmux send-keys -t "$SESSION:bench" "echo '── Bench pane 0 ──' && claude" Enter

for ((p=1; p<BENCH_PANES; p++)); do
  tmux split-window -h -t "$SESSION:bench"
  tmux send-keys -t "$SESSION:bench.$p" "echo '── Bench pane $p ──' && claude" Enter
done

tmux select-layout -t "$SESSION:bench" even-horizontal
((WIN_NUM++))

# ── Pipeline overview window: all agent logs tailed + workbench panes ──
OVERVIEW_WIN=$WIN_NUM
tmux new-window -t "$SESSION" -n overview

# Collect enabled agent log names for the overview panes
OVERVIEW_AGENTS=()
agent_enabled "spec" && OVERVIEW_AGENTS+=("spec")
agent_enabled "spec_review" && OVERVIEW_AGENTS+=("spec-review")
agent_enabled "ux" && OVERVIEW_AGENTS+=("ux")
agent_enabled "copy" && OVERVIEW_AGENTS+=("copy")
agent_enabled "implement" && OVERVIEW_AGENTS+=("implement")
agent_enabled "qa" && OVERVIEW_AGENTS+=("qa")
agent_enabled "code_review" && OVERVIEW_AGENTS+=("code-review")
agent_enabled "rebase" && OVERVIEW_AGENTS+=("rebase")
agent_enabled "merge" && OVERVIEW_AGENTS+=("merge")

# First pane: first agent log
# tail -F (capital) follows by NAME and retries on missing/rotated, so the
# overview survives a log file being deleted, truncated, or git-rm'd while
# the session is up. tail -f follows by inode and strands silently on the
# old fd if the pipeline's `tee -a` recreates the file at a new inode.
# tail -F also handles missing-at-startup natively, so no fallback needed.
if [ ${#OVERVIEW_AGENTS[@]} -gt 0 ]; then
  tmux send-keys -t "$SESSION:overview" "echo '── ${OVERVIEW_AGENTS[0]} ──' && tail -F logs/queue-${OVERVIEW_AGENTS[0]}.log" Enter

  # Remaining agent log panes. Re-tile after each split so the active pane
  # doesn't shrink to <2 rows and trigger "no space for new pane" — which
  # happens at ~5 vertical splits in a 50-row session, well below the 8
  # agents an opt-in repo can have enabled.
  for ((a=1; a<${#OVERVIEW_AGENTS[@]}; a++)); do
    tmux split-window -v -t "$SESSION:overview"
    tmux select-layout -t "$SESSION:overview" tiled >/dev/null
    tmux send-keys -t "$SESSION:overview" "echo '── ${OVERVIEW_AGENTS[$a]} ──' && tail -F logs/queue-${OVERVIEW_AGENTS[$a]}.log" Enter
  done
fi

# Final tile pass for symmetry (panes are already tiled from the loop above).
# Workbench panes intentionally excluded — the dedicated bench window already
# hosts interactive claude sessions, and duplicating them in overview spawns
# extra processes that compete for the same auth/session state.
tmux select-layout -t "$SESSION:overview" tiled
((WIN_NUM++))

tmux select-window -t "$SESSION:status"

AGENT_COUNT=$((BENCH_WIN - 1))
echo "Bureau v2 started with $AGENT_COUNT agents + $BENCH_PANES bench panes."
echo ""
echo "  Attach:      tmux attach -t $SESSION"
echo ""
echo "  Dashboard:   Ctrl+B 0"
echo "  Agents:      Ctrl+B 1-$AGENT_COUNT"
echo "  Workbench:   Ctrl+B $BENCH_WIN  ($BENCH_PANES interactive Claude sessions)"
echo "  Overview:    Ctrl+B $OVERVIEW_WIN  (all agent logs tiled in one view)"
echo "  Detach:      Ctrl+B d"
echo "  Stop all:    tmux kill-session -t $SESSION"
