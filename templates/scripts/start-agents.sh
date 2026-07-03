#!/bin/bash
# Start pipeline agents in tmux — reads enabled agents from .bureau.json
set -euo pipefail

cd "$(dirname "$0")/.."
source scripts/bureau-config.sh

INTERVAL="${1:-$BUREAU_POLL_INTERVAL}"
SESSION="bureau"

if ! command -v tmux &>/dev/null; then
  echo "ERROR: tmux required. Install with: brew install tmux"
  exit 1
fi

if ! command -v claude &>/dev/null; then
  echo "ERROR: claude CLI required."
  exit 1
fi

tmux kill-session -t "$SESSION" 2>/dev/null || true

echo "Starting Bureau agents..."
echo "  Team: $BUREAU_TEAM_NAME ($BUREAU_TEAM_KEY)"
echo "  Interval: ${INTERVAL}m"
echo ""

FIRST=true
WIN_NUM=0

add_agent() {
  local name="$1"
  local mode="$2"

  if [ "$FIRST" = true ]; then
    tmux new-session -d -s "$SESSION" -n "$name" -x 200 -y 50
    FIRST=false
  else
    tmux new-window -t "$SESSION" -n "$name"
  fi
  tmux send-keys -t "$SESSION:$name" "./scripts/queue-loop.sh $mode $INTERVAL" Enter
  ((WIN_NUM++))
}

agent_enabled "spec" && add_agent "spec" "spec"
agent_enabled "spec_review" && add_agent "spec-review" "spec-review"
agent_enabled "ux" && add_agent "ux" "ux"
agent_enabled "implement" && add_agent "build" "implement"

agent_enabled "code_review" && add_agent "review" "code-review"

if [ "$FIRST" = true ]; then
  echo "No agents enabled in .bureau.json"
  exit 1
fi

echo "Bureau started with $WIN_NUM agents."
echo ""
echo "  Attach:   tmux attach -t $SESSION"
echo "  Navigate: Ctrl+B n/p or Ctrl+B 0-$((WIN_NUM-1))"
echo "  Detach:   Ctrl+B d"
echo "  Stop:     tmux kill-session -t $SESSION"
