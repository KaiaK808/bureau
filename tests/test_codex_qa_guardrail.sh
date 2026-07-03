#!/bin/bash
# Guardrail regression: claude_cmd_for_stage in the REAL templates/scripts/bureau-config.sh
# must WARN (stderr only) when a build/test stage (qa, implement, …) is routed to
# runner=codex — Codex's exec sandbox can't run real suites, so it false-halts.
# Review stages on codex must NOT warn. The warning must NEVER leak to stdout
# (stdout is the command string callers eval — corrupting it breaks every stage).
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "$0")" && cd .. && pwd)"
CONFIG_SH="$REPO_ROOT/templates/scripts/bureau-config.sh"

pass=0; fail=0; fail_msgs=()
assert() { if [ "${2:-0}" -eq 1 ]; then pass=$((pass + 1)); else fail=$((fail + 1)); fail_msgs+=("$1"); fi; }

SANDBOX=$(mktemp -d -t bureau-test.codex-guardrail.XXXXXXXX)
trap 'rm -rf "$SANDBOX"' EXIT
cd "$SANDBOX"
cat > .bureau.json <<'EOF'
{ "linear": { "teams": [{ "id": "t1", "key": "EXP", "name": "T", "states": { "triage": "s1" } }] },
  "agents": {}, "repo": { "branch_prefix": "feat", "specs_dir": "specs" } }
EOF

ERRF="$SANDBOX/err"
# run_cmd <stage> <prelude-exports> : prints stdout; stderr captured to $ERRF.
# Runs under bash (bureau-config.sh uses bash-only ${!var}); source noise silenced.
run_cmd() {
  bash -c "set -uo pipefail; export BUREAU_CONFIG='$SANDBOX/.bureau.json'; $2 source '$CONFIG_SH' >/dev/null 2>&1; claude_cmd_for_stage '$1'" 2>"$ERRF"
}

# 1. qa → codex : stdout = codex command, stderr = warning, stdout stays clean
OUT=$(run_cmd qa "export BUREAU_RUNNER_QA=codex;"); ERR=$(cat "$ERRF")
[[ "$OUT" == *codex-stage-runner.sh* ]] && r=1 || r=0; assert "qa->codex stdout is the codex runner command" "$r"
[[ "$OUT" != *warning:* ]]              && r=1 || r=0; assert "qa->codex stdout NOT polluted by the warning"  "$r"
[[ "$ERR" == *warning:*codex* ]]        && r=1 || r=0; assert "qa->codex emits a stderr warning naming codex" "$r"

# 2. code_review → codex : read-only sandbox, NO warning
OUT=$(run_cmd code_review "export BUREAU_RUNNER_CODE_REVIEW=codex;"); ERR=$(cat "$ERRF")
[[ "$OUT" == *codex-stage-runner.sh*read-only* ]] && r=1 || r=0; assert "code_review->codex is the read-only codex runner" "$r"
[[ -z "$ERR" ]]                                   && r=1 || r=0; assert "code_review->codex does NOT warn"               "$r"

# 3. default qa (no runner env) : stays on claude, no warning
OUT=$(run_cmd qa ""); ERR=$(cat "$ERRF")
[[ "$OUT" != *codex-stage-runner.sh* ]] && r=1 || r=0; assert "qa default stays on claude (not codex)" "$r"
[[ -z "$ERR" ]]                         && r=1 || r=0; assert "qa default does NOT warn"               "$r"

echo "codex-qa-guardrail: $pass passed, $fail failed"
if [ "$fail" -ne 0 ]; then printf '  FAIL: %s\n' "${fail_msgs[@]}"; exit 1; fi
exit 0
