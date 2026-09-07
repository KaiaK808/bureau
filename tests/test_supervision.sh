#!/bin/bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TMP=$(mktemp -d); trap 'rm -rf "$TMP"' EXIT
mkdir -p "$TMP/scripts"
cp "$ROOT/templates/scripts/bureau-tick.sh" "$ROOT/templates/scripts/bureau-supervision.py" "$TMP/scripts/"
git -C "$TMP" init -q
cat > "$TMP/scripts/bureau-config.sh" <<'STUB'
BUREAU_ENV_FILE=/nonexistent
bureau_is_paused() { [ "${PAUSED:-0}" = 1 ]; }
precondition_linear() { return "${AUTH_RC:-0}"; }
agent_enabled() { return 0; }
pipeline_pick_next() { [ "${EMPTY:-0}" = 1 ] || echo T-1; }
get_issue_state() { if [ -f state ]; then cat state; else echo 'Build Review'; fi; }
get_issue_branch() { echo 001-task; }
get_issue_detail() { jq -n --argjson labels "$(if [ -f labels ]; then cat labels; else echo '[]'; fi)" '{identifier:"T-1",title:"Task",description:"Work",labels:$labels}'; }
bureau_get() { echo "${HUMAN_NAME:-needs-human}"; }
session_throttle_guard() { return "${QUOTA_RC:-0}"; }
STUB
cat > "$TMP/scripts/bureau-worker.sh" <<'STUB'
#!/bin/bash
echo "$*" >> calls
[ -z "${PARK_LABEL:-}" ] || jq -n --arg name "$PARK_LABEL" '[$name]' > labels
[ -z "${NEXT:-}" ] || echo "$NEXT" > state
exit "${WORKER_RC:-0}"
STUB
cd "$TMP"
bash scripts/bureau-tick.sh > /dev/null
[ "$(wc -l < calls | tr -d ' ')" = 1 ]
jq -e '.outcome == "waiting" and .stage == "rebase"' logs/bureau-tick.json
rm calls
EMPTY=1 bash scripts/bureau-tick.sh >/dev/null
[ ! -f calls ]
PAUSED=1 bash scripts/bureau-tick.sh >/dev/null
jq -e '.outcome == "paused"' logs/bureau-tick.json
[ ! -f calls ]
rc=0; AUTH_RC=10 bash scripts/bureau-tick.sh >/dev/null || rc=$?
[ "$rc" = 10 ]; jq -e '.outcome == "failed"' logs/bureau-tick.json
rc=0; WORKER_RC=20 bash scripts/bureau-tick.sh --stage code_review >/dev/null || rc=$?
[ "$rc" = 20 ]; jq -e '.outcome == "stopped_for_review"' logs/bureau-tick.json
NEXT=Done bash scripts/bureau-tick.sh --stage merge --allow-merge >/dev/null
jq -e '.outcome == "completed"' logs/bureau-tick.json
rm state calls
rc=0; PARK_LABEL=decision-needed HUMAN_NAME=decision-needed bash scripts/bureau-tick.sh --stage qa >/dev/null || rc=$?
[ "$rc" = 25 ]; jq -e '.outcome == "blocked" and .before == .after and .exit_code == 25' logs/bureau-tick.json
rm calls labels
QUOTA_RC=23 bash scripts/bureau-tick.sh --stage implement >/dev/null
[ ! -f calls ]; jq -e '.outcome == "waiting" and .exit_code == 23' logs/bureau-tick.json
# Read the actual production usage parser and report with provider separation.
echo '{"agents":{"runner":"codex"},"session":{"cost_tracking":true}}' > .bureau.json
source "$ROOT/templates/scripts/bureau-config.sh" >/dev/null 2>&1
printf '{"pct":99,"updated_epoch":%s}' "$(date +%s)" > claude-usage.json
export BUREAU_USAGE_FILE="$TMP/claude-usage.json"
[ -z "$(_session_usage_signal codex)" ]
export BUREAU_CODEX_USAGE_FILE="$TMP/codex-usage.json"
cp claude-usage.json codex-usage.json
rc=0; BUREAU_THROTTLE_ONCE=1 session_throttle_guard implement || rc=$?
[ "$rc" = 23 ]
export BUREAU_COST_DIR="$TMP/cost"
record_stage_cost '{"provider":"codex","usage":{"input_tokens":1,"output_tokens":2},"total_cost_usd":null}' T-1 implement
jq -e '.cost_usd == null and .provider == "codex"' "$BUREAU_COST_DIR/T-1.jsonl"
report_costs | grep -q unavailable
python3 - "$ROOT" <<'PY'
import importlib.util, pathlib, sys
sys.dont_write_bytecode=True
p=pathlib.Path(sys.argv[1])/'templates/scripts/bureau-monitor.py'
spec=importlib.util.spec_from_file_location('monitor',p); m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
result={'outcome':'completed','issue':'T-1','exit_code':0}
assert m.compare(result,None)['notify']
assert not m.compare(result,result)['notify']
assert not m.compare({'outcome':'waiting'},result)['notify']
assert m.compare({'outcome':'failed'},result)['notify']
PY
echo 'PASS bounded tick, provider quota separation, unavailable costs and quiet monitor'
