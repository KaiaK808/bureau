#!/bin/bash
# Exercise complete tick and queue dispatchers with a capacity-waiting worker.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SB=$(mktemp -d -t bureau-test.capacity.XXXXXXXX)
trap 'rm -rf "$SB"' EXIT
mkdir -p "$SB/scripts" "$SB/bin"
git -C "$SB" init -q -b main
git -C "$SB" -c user.email=t@bureau -c user.name=test commit -qm init --allow-empty
cp "$ROOT/templates/scripts/bureau-tick.sh" "$ROOT/templates/scripts/queue-loop.sh" "$ROOT/templates/scripts/bureau-supervision.py" "$ROOT/templates/scripts/bureau-env.sh" "$ROOT/templates/scripts/bureau-evidence.py" "$SB/scripts/"
cat > "$SB/scripts/bureau-config.sh" <<'CFG'
source "$(dirname "${BASH_SOURCE[0]}")/bureau-env.sh"
BUREAU_ENV_FILE=/dev/null
BUREAU_POLL_INTERVAL=1
BUREAU_TEAM_NAME=Test
BUREAU_TEAM_KEY=EXP
bureau_load_env() { :; }
bureau_is_paused() { return 1; }
precondition_linear() { :; }
agent_enabled() { [ "$1" = spec ]; }
pipeline_pick_next() { echo EXP-1638; }
preselect_issue() { echo EXP-1638; }
get_issue_state() { echo Triage; }
get_issue_branch() { echo ''; }
session_throttle_guard() { :; }
emit_event() { printf '%s\n' "$*" >> "$REPO_DIR/events"; }
alert_telegram() { echo ALERT >> "$REPO_DIR/alerts"; }
CFG
sed -n '/^exit_class() {/,/^}/p' "$ROOT/templates/scripts/bureau-config.sh" >> "$SB/scripts/bureau-config.sh"
cat > "$SB/scripts/bureau-worker.sh" <<'WORKER'
#!/bin/bash
printf 'Spec gate: waiting\n  EXP-1621 — Merge\n'
exit 28
WORKER
for n in 1 2 3; do
  rc=0
  bash "$SB/scripts/bureau-tick.sh" --stage spec > "$SB/tick.out" 2> "$SB/tick.err" || rc=$?
  [ "$rc" = 28 ]
  jq -e '.outcome == "waiting" and .exit_code == 28 and .issue == "EXP-1638" and .stage == "spec"' "$SB/logs/bureau-tick.json" >/dev/null
 done
echo 'PASS three bounded ticks: process 28, outcome waiting, JSON retains exit_code 28'
# Extract the real queue function rather than starting an unbounded poll loop.
sed -n '/^run_script() {/,/^}/p' "$ROOT/templates/scripts/queue-loop.sh" > "$SB/run-queue.sh"
cat > "$SB/queue-test.sh" <<'QUEUE'
#!/bin/bash
set -uo pipefail
REPO_DIR="$1"; source "$REPO_DIR/scripts/bureau-config.sh"
LOG_FILE="$REPO_DIR/queue.log"; MODE=spec
source "$REPO_DIR/run-queue.sh"
rc=0; run_script spec-pipeline.sh Spec "$REPO_DIR/worker" || rc=$?
[ "$rc" = 28 ] && [ "$CAPACITY_WAIT" = true ]
QUEUE
bash "$SB/queue-test.sh" "$SB" > "$SB/queue.out"
grep -q 'waiting for spec capacity' "$SB/queue.out"
grep -q 'class=spec-capacity-wait' "$SB/events"
[ ! -s "$SB/alerts" ]
! grep -q 'error (exit 28\|queue empty' "$SB/queue.out"
echo 'PASS queue reports waiting, records spec-capacity-wait and sends no alert'
python3 - "$ROOT" <<'PY'
import json, pathlib, subprocess, sys
text = (pathlib.Path(sys.argv[1]) / 'templates/commands/bureau-learnings.md').read_text()
query = text.split("jq -sc 'group_by(.issue)", 1)[1].split("' \"$BUREAU_LEARN_TMP/events-window.jsonl\"", 1)[0]
query = 'group_by(.issue)' + query
events = [{'event': 'stage_end', 'issue': 'EXP-1', 'class': c}
          for c in ['spec-capacity-wait'] * 3 + ['ok', 'queue-empty']]
events.append({'event': 'stage_end', 'issue': 'EXP-2', 'class': 'no-tasks'})
result = json.loads(subprocess.check_output(['jq', '-sc', query], text=True,
                    input='\n'.join(json.dumps(event) for event in events)))
assert len(result) == 1 and result[0]['issue'] == 'EXP-2' and len(result[0]['failures']) == 1, result
print('PASS actual bureau-learnings jq excludes three capacity waits and retains a real failure')
PY
echo 'OK test_spec_capacity_consumers (5 dispatch/filter checks)'
