#!/bin/bash
# test_cost_tracking.sh — EXP-671 opt-in per-stage cost tracking. Verifies the
# toggle, the --print↔--output-format-json flag, the backward-compatible
# envelope unwrap in parse_claude_json, the usage capture, and the report.
# Default-OFF must be byte-identical (no log, --print, raw parse unchanged).

set -uo pipefail
TESTS_DIR="$(cd "$(dirname "$0")" && pwd)"
BC="$TESTS_DIR/../templates/scripts/bureau-config.sh"
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
cd "$TMP"
echo '{"session":{"cost_tracking":false}}' > .bureau.json
# shellcheck disable=SC1090
source "$BC" >/dev/null 2>&1
set +e

fail=0
check() { if eval "$2"; then echo "ok   - $1"; else echo "FAIL - $1 (got: ${3:-})"; fail=1; fi; }

# ── toggle ───────────────────────────────────────────────────────────────────
check "off by default" '! cost_tracking_enabled'
check "on via BUREAU_COST_TRACKING=1" '( export BUREAU_COST_TRACKING=1; cost_tracking_enabled )'
echo '{"session":{"cost_tracking":true}}' > on.json
check "on via .bureau.json session.cost_tracking" '( export BUREAU_CONFIG="'"$TMP"'/on.json"; cost_tracking_enabled )'

# ── invocation flag ──────────────────────────────────────────────────────────
check "cmd uses --print when off" 'claude_cmd_for_stage implement | grep -q -- "--print"'
check "cmd uses --output-format json when on" \
  '( export BUREAU_COST_TRACKING=1; claude_cmd_for_stage implement ) | grep -q -- "--output-format json"'
check "cmd is NOT json when off" '! claude_cmd_for_stage implement | grep -q -- "--output-format json"'

# ── parse_claude_json: backward-compatible envelope unwrap ────────────────────
RAW=$'preamble text\n```json\n{"status":"DONE","tasks_done":3}\n```\ntrailer'
check "parses raw verdict (cost-off shape)" '[ "$(parse_claude_json "$RAW" ".status")" = "DONE" ]' \
  "$(parse_claude_json "$RAW" ".status")"
ENV=$(jq -n --arg r "$RAW" '{result:$r, usage:{input_tokens:100,output_tokens:50,cache_read_input_tokens:0,cache_creation_input_tokens:0}, total_cost_usd:0.012}')
check "parses envelope-wrapped verdict (cost-on shape)" '[ "$(parse_claude_json "$ENV" ".status")" = "DONE" ]' \
  "$(parse_claude_json "$ENV" ".status")"
check "envelope unwrap reads tasks_done too" '[ "$(parse_claude_json "$ENV" ".tasks_done")" = "3" ]'

# ── usage capture ────────────────────────────────────────────────────────────
record_stage_cost "$ENV" "EXP-1" "implement"   # cost OFF → no-op
check "record no-op when tracking off" '[ ! -f "'"$TMP"'/cost/EXP-1.jsonl" ]'

( export BUREAU_COST_TRACKING=1 BUREAU_COST_DIR="$TMP/cost"; record_stage_cost "$ENV" "EXP-1" "implement" )
check "record writes log when on" '[ -f "'"$TMP"'/cost/EXP-1.jsonl" ]'
check "log captures input_tokens" 'grep -q "\"input_tokens\":100" "'"$TMP"'/cost/EXP-1.jsonl"'
check "log captures cost_usd" 'grep -q "\"cost_usd\":0.012" "'"$TMP"'/cost/EXP-1.jsonl"'

( export BUREAU_COST_TRACKING=1 BUREAU_COST_DIR="$TMP/cost2"; record_stage_cost "$RAW" "EXP-2" "qa" )
check "record no-op when no usage envelope (codex/--print)" '[ ! -f "'"$TMP"'/cost2/EXP-2.jsonl" ]'

# ── report ───────────────────────────────────────────────────────────────────
report=$( export BUREAU_COST_DIR="$TMP/cost"; report_costs )
check "report names the issue" 'echo "$report" | grep -q "EXP-1"'
check "report shows the implement stage + tokens" 'echo "$report" | grep -q "implement: 100 in"'
empty=$( export BUREAU_COST_DIR="$TMP/empty"; report_costs )
check "report graceful when no data" 'echo "$empty" | grep -qi "No cost data"'

[ "$fail" -eq 0 ] && echo "PASS — cost tracking (opt-in, backward-compatible)"
exit "$fail"
