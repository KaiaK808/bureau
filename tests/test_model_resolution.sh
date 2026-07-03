#!/bin/bash
# Verifies resolve_model_for_stage / claude_cmd_for_stage in the REAL
# templates/scripts/bureau-config.sh honour the documented precedence:
#
#   1. BUREAU_MODEL_<STAGE> env  (operator override)
#   2. .agents.<stage>.model     (per-stage JSON)
#   3. .agents.model             (workspace JSON default)
#   4. BUREAU_MODEL_DEFAULT env  (workspace env fallback)
#   5. empty                     (no --model flag → CLI default)
#
# Regression coverage for the silent bug where a source-time JSON→env
# pre-load clobbered operator overrides and made the workspace env default
# dominate per-stage JSON in pipelines spawned by queue-loop.sh.
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "$0")" && cd .. && pwd)"
CONFIG_SH="$REPO_ROOT/templates/scripts/bureau-config.sh"

fail=0
pass=0
fail_msgs=()

# write_config <json-for-.agents>
write_config() {
  local agents="$1"
  cat > .bureau.json <<EOF
{
  "linear": {
    "teams": [{
      "id": "t1", "key": "EXP", "name": "Test",
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
    }
  },
  "agents": $agents,
  "repo": { "branch_prefix": "feat", "specs_dir": "specs" }
}
EOF
}

# assert <label> <expected> <actual>
assert_eq() {
  if [ "$2" = "$3" ]; then
    pass=$((pass + 1))
  else
    fail=$((fail + 1))
    fail_msgs+=("$1: expected '$2', got '$3'")
  fi
}

SANDBOX=$(mktemp -d -t bureau-test.model-resolution.XXXXXXXX)
trap 'rm -rf "$SANDBOX"' EXIT
cd "$SANDBOX"

# ── Scenario A: per-stage JSON beats workspace env default ────────────────
write_config '{
  "implement": { "model": "claude-opus-4-7" },
  "qa":        { "model": "claude-sonnet-4-6" }
}'
# Run in a subshell so BUREAU_MODEL_DEFAULT export does not leak between scenarios.
out=$(
  export BUREAU_MODEL_DEFAULT=claude-sonnet-4-6
  unset BUREAU_MODEL_IMPLEMENT BUREAU_MODEL_QA
  # shellcheck disable=SC1090
  source "$CONFIG_SH"
  resolve_model_for_stage implement
)
assert_eq "A1 per-stage JSON beats env default (implement)" "claude-opus-4-7" "$out"

out=$(
  export BUREAU_MODEL_DEFAULT=claude-sonnet-4-6
  unset BUREAU_MODEL_QA
  source "$CONFIG_SH"
  resolve_model_for_stage qa
)
assert_eq "A2 per-stage JSON sonnet stays sonnet (qa)" "claude-sonnet-4-6" "$out"

# ── Scenario B: operator BUREAU_MODEL_<STAGE> beats per-stage JSON ────────
write_config '{
  "implement": { "model": "claude-opus-4-7" }
}'
out=$(
  export BUREAU_MODEL_DEFAULT=claude-sonnet-4-6
  export BUREAU_MODEL_IMPLEMENT=claude-haiku-4-5
  source "$CONFIG_SH"
  resolve_model_for_stage implement
)
assert_eq "B  operator env beats per-stage JSON" "claude-haiku-4-5" "$out"

# ── Scenario C: workspace JSON default beats env default ─────────────────
write_config '{ "model": "claude-opus-4-7" }'
out=$(
  export BUREAU_MODEL_DEFAULT=claude-sonnet-4-6
  unset BUREAU_MODEL_IMPLEMENT
  source "$CONFIG_SH"
  resolve_model_for_stage implement
)
assert_eq "C  workspace JSON default beats env default" "claude-opus-4-7" "$out"

# ── Scenario D: env default is the last-resort fallback ───────────────────
write_config '{}'
out=$(
  export BUREAU_MODEL_DEFAULT=claude-sonnet-4-6
  unset BUREAU_MODEL_IMPLEMENT
  source "$CONFIG_SH"
  resolve_model_for_stage implement
)
assert_eq "D  env default falls through when nothing in JSON" "claude-sonnet-4-6" "$out"

# ── Scenario E: nothing configured → empty (no --model flag) ──────────────
write_config '{}'
out=$(
  unset BUREAU_MODEL_DEFAULT BUREAU_MODEL_IMPLEMENT
  source "$CONFIG_SH"
  resolve_model_for_stage implement
)
assert_eq "E1 empty resolution when nothing configured" "" "$out"

out=$(
  unset BUREAU_MODEL_DEFAULT BUREAU_MODEL_IMPLEMENT
  source "$CONFIG_SH"
  claude_cmd_for_stage implement
)
assert_eq "E2 claude_cmd_for_stage omits --model flag" \
  "claude -p --print --dangerously-skip-permissions" "$out"

# ── Scenario F: claude_cmd_for_stage emits --model when resolved ─────────
write_config '{ "implement": { "model": "claude-opus-4-7" } }'
out=$(
  unset BUREAU_MODEL_DEFAULT BUREAU_MODEL_IMPLEMENT
  source "$CONFIG_SH"
  claude_cmd_for_stage implement
)
assert_eq "F  claude_cmd_for_stage emits --model" \
  "claude -p --print --dangerously-skip-permissions --model claude-opus-4-7" "$out"

# ── Scenario G: boolean .agents.<stage> does not crash resolution ─────────
# Per-stage may be a plain boolean toggle ("implement": true). The model
# lookup must not jq-error on that shape — bureau_get_agent_model guards.
write_config '{
  "implement": true,
  "model": "claude-opus-4-7"
}'
out=$(
  unset BUREAU_MODEL_DEFAULT BUREAU_MODEL_IMPLEMENT
  source "$CONFIG_SH"
  resolve_model_for_stage implement
)
assert_eq "G  boolean stage toggle falls through to workspace JSON" \
  "claude-opus-4-7" "$out"

# ── Scenario H: codex runner routing (BUREAU_RUNNER / .agents.runner) ─────
write_config '{}'
out=$(
  export BUREAU_RUNNER_QA=codex
  source "$CONFIG_SH"
  resolve_runner_for_stage qa
)
assert_eq "H1 BUREAU_RUNNER_<STAGE>=codex resolves codex" "codex" "$out"

out=$(
  unset BUREAU_RUNNER_IMPLEMENT
  source "$CONFIG_SH"
  resolve_runner_for_stage implement
)
assert_eq "H2 default runner is claude" "claude" "$out"

write_config '{ "qa": { "runner": "codex" } }'
out=$(
  unset BUREAU_RUNNER_QA
  source "$CONFIG_SH"
  resolve_runner_for_stage qa
)
assert_eq "H3 .agents.<stage>.runner=codex resolves codex" "codex" "$out"

# claude_cmd_for_stage emits the codex-stage-runner invocation; qa → workspace-write
write_config '{}'
out=$(
  export BUREAU_RUNNER_QA=codex
  unset BUREAU_CODEX_MODEL_QA BUREAU_CODEX_MODEL_DEFAULT
  source "$CONFIG_SH"
  claude_cmd_for_stage qa
)
assert_eq "H4 codex qa → workspace-write runner, no model" \
  "bash scripts/codex-stage-runner.sh --sandbox workspace-write --" "$out"

# review stages run read-only (must not mutate the tree)
out=$(
  export BUREAU_RUNNER_CODE_REVIEW=codex
  unset BUREAU_CODEX_MODEL_CODE_REVIEW BUREAU_CODEX_MODEL_DEFAULT
  source "$CONFIG_SH"
  claude_cmd_for_stage code_review
)
assert_eq "H5 codex code_review → read-only sandbox" \
  "bash scripts/codex-stage-runner.sh --sandbox read-only --" "$out"

# codex model flows from BUREAU_CODEX_MODEL_<STAGE>, never the claude model
out=$(
  export BUREAU_RUNNER_QA=codex BUREAU_CODEX_MODEL_QA=gpt-5-codex
  source "$CONFIG_SH"
  claude_cmd_for_stage qa
)
assert_eq "H6 codex model from BUREAU_CODEX_MODEL_<STAGE>" \
  "bash scripts/codex-stage-runner.sh --model gpt-5-codex --sandbox workspace-write --" "$out"

# ── Report ───────────────────────────────────────────────────────────────
echo "passed: $pass  failed: $fail"
if [ "$fail" -gt 0 ]; then
  printf '  - %s\n' "${fail_msgs[@]}"
  exit 1
fi
exit 0
