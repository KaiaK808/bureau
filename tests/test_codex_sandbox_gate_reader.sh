#!/bin/bash
# The production reader alone: no harness, stage, provider or network calls.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
T=$(mktemp -d -t bureau-test.sandbox-reader.XXXXXXXX)
trap 'rm -rf "$T"' EXIT
sed -n -e '/^codex_sandbox_gate_only() {/,/^}/p' -e '/^codex_qa_sandbox_gate_only() {/,/^}/p' \
  "$ROOT/templates/scripts/bureau-config.sh" > "$T/reader.sh"
source "$T/reader.sh"

expect() {
  local expected="$1" raw="$2" label="$3" actual=false
  if codex_sandbox_gate_only "$raw"; then actual=true; fi
  [ "$actual" = "$expected" ] || { echo "FAIL [$label]: expected $expected, got $actual" >&2; exit 1; }
}

GOOD='{"notes":{"needs_human":[{"reason":"SANDBOX_GATE: Operation not permitted on bind"}]}}'
expect true "$GOOD" 'one reason object'
expect true '{"notes":{"needs_human":[{"reason":"SANDBOX_GATE: bind"},{"reason":"SANDBOX_GATE: network"}]}}' 'all reasons have the prefix'
for raw in 'null' '[]' '42' '"text"' '{}' '{"notes":[]}' '{"notes":null}' '{"notes":"text"}'; do
  expect false "$raw" 'object and notes are required'
done
for items in '[]' 'null' '{}' '"SANDBOX_GATE:"' '{"reason":"SANDBOX_GATE: bind"}' \
  '[null]' '[42]' '[[]]' '["SANDBOX_GATE:"]' '[{}]' '[{"reason":null}]' \
  '[{"reason":42}]' '[{"reason":[]}]' '[{"reason":{}}]' '[{"reason":""}]' \
  '[{"reason":"other blocker"}]' '[{"reason":"SANDBOX_GATE: bind"},{"reason":"other blocker"}]'; do
  expect false "{\"notes\":{\"needs_human\":$items}}" 'nonempty array of reason objects only'
done
ENVELOPE=$(jq -nc --arg result "$GOOD" '{result:$result,provider:"codex",total_cost_usd:null}')
expect true "$ENVELOPE" 'cost envelope'
expect false '{"result":{ "notes":{"needs_human":[{"reason":"SANDBOX_GATE: bind"}]}}}' 'envelope result must be a string'
expect false "$(jq -nc --arg result '[]' '{result:$result}')" 'envelope result must hold an object'
expect false "$(jq -nc --arg result "$GOOD"$'\n'"$GOOD" '{result:$result}')" 'two inner values'
expect false "$(jq -nc --arg result "$GOOD"$'\n''garbage' '{result:$result}')" 'inner trailing garbage'
FENCED=$'A result in prose:\n```json\n'"$GOOD"$'\n```'
expect false "$FENCED" 'fenced JSON in prose'
expect false "$(jq -nc --arg result "$FENCED" '{result:$result}')" 'fenced JSON inside envelope'
expect false "$GOOD"$'\n''garbage' 'valid object followed by garbage'
expect false "$GOOD"$'\n'"$GOOD" 'two objects'
expect false "$ENVELOPE"$'\n'"$GOOD" 'envelope followed by an object'
expect false '' 'empty input'

expect_qa() {
  local expected="$1" raw="$2" label="$3" actual=false
  if codex_qa_sandbox_gate_only "$raw"; then actual=true; fi
  [ "$actual" = "$expected" ] || { echo "FAIL [QA $label]: expected $expected, got $actual" >&2; exit 1; }
}

QA_GOOD='{"status":"NEEDS_HUMAN","tests_added":0,"tests_failing":0,"coverage_notes":"SANDBOX_GATE: socket test: Operation not permitted on bind"}'
expect_qa true "$QA_GOOD" 'one result object'
QA_ENVELOPE=$(jq -nc --arg result "$QA_GOOD" '{result:$result,provider:"codex",total_cost_usd:null}')
expect_qa true "$QA_ENVELOPE" 'cost envelope'
for raw in 'null' '[]' '42' '"text"' '{}' \
  '{"coverage_notes":"SANDBOX_GATE: bind"}' \
  '{"status":"GREEN","coverage_notes":"SANDBOX_GATE: bind"}' \
  '{"status":"RED","coverage_notes":"SANDBOX_GATE: bind"}' \
  '{"status":42,"coverage_notes":"SANDBOX_GATE: bind"}' \
  '{"status":["NEEDS_HUMAN"],"coverage_notes":"SANDBOX_GATE: bind"}' \
  '{"status":"NEEDS_HUMAN"}'; do
  expect_qa false "$raw" 'object with NEEDS_HUMAN status and coverage notes required'
done
for notes in 'null' '42' '[]' '{}' 'false' '""' '"other blocker"' \
  '" SANDBOX_GATE: bind"' '"other blocker; SANDBOX_GATE: bind"'; do
  expect_qa false "{\"status\":\"NEEDS_HUMAN\",\"coverage_notes\":$notes}" 'string prefix at the start required'
done
expect_qa false "$(jq -nc --argjson result "$QA_GOOD" '{result:$result}')" 'envelope result must be a string'
expect_qa false "$(jq -nc --arg result '[]' '{result:$result}')" 'envelope result must hold an object'
expect_qa false "$(jq -nc --arg result "$QA_GOOD"$'\n'"$QA_GOOD" '{result:$result}')" 'two inner values'
expect_qa false "$(jq -nc --arg result "$QA_GOOD"$'\n''garbage' '{result:$result}')" 'inner trailing garbage'
QA_FENCED=$'```json\n'"$QA_GOOD"$'\n```'
expect_qa false "$QA_FENCED" 'fenced JSON'
expect_qa false "$(jq -nc --arg result "$QA_FENCED" '{result:$result}')" 'fenced JSON inside envelope'
expect_qa false "$QA_GOOD"$'\n''garbage' 'valid object followed by garbage'
expect_qa false "$QA_GOOD"$'\n'"$QA_GOOD" 'two objects'
expect_qa false "$QA_ENVELOPE"$'\n'"$QA_GOOD" 'envelope followed by an object'
expect_qa false '' 'empty input'
echo 'OK test_codex_sandbox_gate_reader'
