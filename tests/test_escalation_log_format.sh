#!/bin/bash
# Verifies the REAL log_escalation helper in templates/scripts/bureau-config.sh
# produces a line matching the operator-monitor acceptance regex. This test
# does NOT use the main harness's stub config — it sources production
# bureau-config.sh against a minimal .bureau.json so any drift between the
# stub's mirror and the real helper is caught.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")" && cd .. && pwd)"
SANDBOX=$(mktemp -d -t bureau-test.escalation.XXXXXXXX)
trap 'rm -rf "$SANDBOX"' EXIT

# Minimal .bureau.json so bureau-config.sh's source-time jq reads succeed.
cat > "$SANDBOX/.bureau.json" <<'EOF'
{
  "linear": {
    "teams": [{
      "id": "team-id", "key": "EXP", "name": "Test",
      "states": {
        "triage": "s1", "spec": "s2", "spec_review": "s3", "design": "s4",
        "build": "s5", "build_review": "s6", "done": "s7"
      }
    }],
    "labels": {
      "lane2":            { "id": "l1", "name": "lane-2" },
      "needs_human":      { "id": "l2", "name": "needs-human" },
      "needs_ux":         { "id": "l3", "name": "needs-ux" },
      "ai_implementable": { "id": "l4", "name": "ai-implementable" }
    },
    "projects": []
  },
  "agents": { "poll_interval_minutes": 30, "max_review_cycles": 3 },
  "repo": { "branch_prefix": "feat", "specs_dir": "specs" }
}
EOF

cd "$SANDBOX"
# shellcheck disable=SC1091
source "$REPO_ROOT/templates/scripts/bureau-config.sh"

# Call with a reason that contains the kinds of characters that would break
# a naive line format: spaces, an embedded double-quote, an equals sign.
log_escalation \
  "EXP-402" \
  "code-review" \
  3 \
  'REQUEST_CHANGES exceeded "max_review_cycles=3"' \
  56 \
  "049-parliament-debate"

LOG_FILE="$SANDBOX/logs/escalations.log"
if [ ! -s "$LOG_FILE" ]; then
  echo "FAIL: $LOG_FILE missing or empty" >&2
  exit 1
fi

LINE=$(tail -n 1 "$LOG_FILE")

# Acceptance regex from SKILL.md / plan. Uses POSIX [[:space:]] so it works
# under both GNU grep and BSD grep (macOS default).
REGEX='^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z[[:space:]]+ESCALATED[[:space:]]+EXP-402[[:space:]]+code-review[[:space:]]+cycle=3[[:space:]]+reason="REQUEST_CHANGES exceeded .max_review_cycles=3."[[:space:]]+pr=56[[:space:]]+branch=049-parliament-debate$'
if ! grep -qE "$REGEX" <<< "$LINE"; then
  echo "FAIL: line does not match acceptance regex" >&2
  echo "Got:      $LINE" >&2
  echo "Expected: $REGEX" >&2
  exit 1
fi

# Embedded double quotes must be scrubbed to single quotes.
if grep -q '"max_review_cycles=3"' <<< "$LINE"; then
  echo "FAIL: embedded double quotes not scrubbed" >&2
  exit 1
fi

# JSONL companion event must also have been emitted.
if [ ! -s "$SANDBOX/logs/events.jsonl" ]; then
  echo "FAIL: events.jsonl missing — JSONL sink not firing alongside TSV" >&2
  exit 1
fi
EVENT=$(tail -n 1 "$SANDBOX/logs/events.jsonl")
if ! grep -q '"event":"escalation"' <<< "$EVENT"; then
  echo "FAIL: events.jsonl line is not an escalation event: $EVENT" >&2
  exit 1
fi

echo "OK test_escalation_log_format"
