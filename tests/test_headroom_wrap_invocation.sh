#!/bin/bash
# Regression: claude_cmd_for_stage must emit `headroom wrap claude -- -p` (WITH
# the `--` end-of-options separator) when headroom_wrap is on. `headroom wrap`
# has its OWN `-p/--port` flag, so `headroom wrap claude -p …` makes headroom
# parse claude's `-p` (print) as the port and die: "'--print' is not a valid
# integer" (exit 2). The OFF path must stay byte-identical to plain `claude -p`.
# (Found 2026-06-23 piloting the #22 token-efficiency stack on brainhuggers-cli.)
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "$0")" && cd .. && pwd)"
CONFIG_SH="$REPO_ROOT/templates/scripts/bureau-config.sh"

pass=0; fail=0; fail_msgs=()
assert() { if [ "${2:-0}" -eq 1 ]; then pass=$((pass + 1)); else fail=$((fail + 1)); fail_msgs+=("$1"); fi; }

SANDBOX=$(mktemp -d -t bureau-test.headroom.XXXXXXXX)
trap 'rm -rf "$SANDBOX"' EXIT
cd "$SANDBOX"
cat > .bureau.json <<'EOF'
{ "linear": { "teams": [{ "id": "t1", "key": "EXP", "name": "T", "states": { "triage": "s1" } }] },
  "agents": {}, "repo": { "branch_prefix": "feat", "specs_dir": "specs" } }
EOF

# run <prelude-exports> : prints claude_cmd_for_stage implement (bash-only ${!var}).
run() { bash -c "set -uo pipefail; export BUREAU_CONFIG='$SANDBOX/.bureau.json'; $1 source '$CONFIG_SH' >/dev/null 2>&1; claude_cmd_for_stage implement" 2>/dev/null; }

OFF=$(run "")
ON=$(run "export BUREAU_HEADROOM_WRAP=1;")

[[ "$OFF" == "claude -p "* ]]                       && r=1 || r=0; assert "headroom OFF → plain 'claude -p …' (byte-identical default)" "$r"
[[ "$ON" == "headroom wrap claude -- -p "* ]]       && r=1 || r=0; assert "headroom ON → 'headroom wrap claude -- -p …' (with the -- separator)" "$r"
[[ "$ON" != *"headroom wrap claude -p"* ]]          && r=1 || r=0; assert "headroom ON must NOT emit 'headroom wrap claude -p' (the -p/--port collision)" "$r"

echo "headroom-wrap-invocation: $pass passed, $fail failed"
if [ "$fail" -ne 0 ]; then printf '  FAIL: %s\n' "${fail_msgs[@]}"; exit 1; fi
exit 0
