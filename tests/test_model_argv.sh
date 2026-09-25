#!/bin/bash
# A model value from .env reaches the provider as ONE argument; it can never add runner options.
#
# Every stage and upstream-port.sh call the model through run_stage_for → bureau-provider.py,
# which builds the command as a list. This test sends a hostile BUREAU_MODEL_UPSTREAM_PORT
# through the real path — bureau_load_env reads the .env, run_stage_for starts the real
# provider, and a stub `claude` on PATH records the argv it received. The negative control
# runs the legacy string form (`$(claude_cmd_for_stage …)` word-split unquoted, which
# slidefactory closed as EXP-1476) with the same value and shows the injected option arrive
# as an argument of its own.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")" && cd .. && pwd)"
SCRIPTS="$REPO_ROOT/templates/scripts"
SB=$(mktemp -d -t bureau-test.argv.XXXXXXXX)
trap 'rm -rf "$SB"' EXIT
fail() { echo "FAIL $*" >&2; exit 1; }

git -C "$SB" init -q
cat > "$SB/.bureau.json" <<'EOF'
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
HOSTILE='opus --add-dir /'
printf 'BUREAU_MODEL_UPSTREAM_PORT=%s\n' "$HOSTILE" > "$SB/.env"

mkdir -p "$SB/bin"
cat > "$SB/bin/claude" <<EOF
#!/bin/bash
# The provider checks the login first; answer that without recording it.
if [ "\$1" = auth ]; then echo '{"loggedIn": true}'; exit 0; fi
: > "$SB/argv"
for a in "\$@"; do printf '%s\n' "\$a" >> "$SB/argv"; done
cat >/dev/null
echo '{"type":"result","subtype":"success","is_error":false,"result":"ok"}'
EOF
chmod +x "$SB/bin/claude"

run() {  # $1 = snippet after the config and .env are loaded
  rm -f "$SB/argv"
  (cd "$SB" && PATH="$SB/bin:$PATH" /bin/bash -c "
    set -uo pipefail
    source '$SCRIPTS/bureau-config.sh'
    bureau_load_env --export .env
    $1" </dev/null >/dev/null 2>&1) || true
  [ -f "$SB/argv" ] || fail "the stub claude was never started"
}
after_model() { awk 'prev == "--model" { print; exit } { prev = $0 }' "$SB/argv"; }

run 'run_stage_for upstream_port "port this"'
[ "$(after_model)" = "$HOSTILE" ] || fail "the model value did not arrive as one argument: '$(after_model)'"
! grep -qx -- '--add-dir' "$SB/argv" || fail "the model value added a runner option of its own"
echo "PASS a model value from .env reaches claude as one argument through run_stage_for"

run 'claude_cmd=$(claude_cmd_for_stage upstream_port); $claude_cmd "port this"'
grep -qx -- '--add-dir' "$SB/argv" \
  || fail "negative control: the legacy string form no longer splits the value, so this test proves nothing"
echo "PASS negative control: the legacy string form turns the same value into an extra option"

# No script under templates/scripts may call the legacy string form.
if grep -nE '\$\(claude_cmd_for_stage|`claude_cmd_for_stage' "$SCRIPTS"/*.sh | grep -v '^[^:]*:[0-9]*:[[:space:]]*#'; then
  fail "a template script calls claude_cmd_for_stage; use run_stage_for"
fi
echo "PASS no template script calls the legacy string form"
