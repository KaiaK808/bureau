#!/bin/bash
# Every stage's model call carries the CI rule (v3.2): never wait for, poll or re-trigger CI or
# a merge gate inside a stage, never commit CI results as evidence; push, report and stop. It
# wins over project instructions that ask the agent to wait for CI. Implement passes were
# killed at their time limit while polling the checks of finished, pushed work, because an
# installation's own CLAUDE.md told the agent to wait for a missing CI run.
#
# The stages are every name a template script passes to run_stage_for. Each one sends a prompt
# through the real run_stage_for and bureau-provider.py; the test reads what a stub claude
# (system prompt file) and a stub codex (prompt on stdin) actually received. The rule is also
# in scripts/bureau-stage.md, the protocol app stages and agents read from the worktree.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")" && cd .. && pwd)"
SCRIPTS="$REPO_ROOT/templates/scripts"
SB=$(mktemp -d -t bureau-test.cirule.XXXXXXXX)
trap 'rm -rf "$SB"' EXIT
FAILS=0
fail() { echo "FAIL $*" >&2; FAILS=$((FAILS + 1)); }

git -C "$SB" init -q
mkdir -p "$SB/bin" "$SB/evidence"
cat > "$SB/bin/claude" <<EOF
#!/bin/bash
if [ "\$1" = auth ]; then echo '{"loggedIn": true}'; exit 0; fi
: > "$SB/received"
while [ "\$#" -gt 0 ]; do
  if [ "\$1" = --append-system-prompt-file ]; then cat "\$2" >> "$SB/received"; fi
  shift
done
cat >/dev/null
echo '{"type":"result","subtype":"success","is_error":false,"result":"ok"}'
EOF
cat > "$SB/bin/codex" <<EOF
#!/bin/bash
if [ "\$1" = login ]; then exit 0; fi
cat > "$SB/received"
echo '{"type":"turn.completed","usage":{"input_tokens":1,"output_tokens":1}}'
EOF
chmod +x "$SB/bin/claude" "$SB/bin/codex"

# The rule as the system text states it, in parts, so a reworded part names itself.
SYSTEM_PARTS=(
  'Never wait for, poll or re-trigger CI or a merge gate inside this stage'
  'no gh pr checks --watch, no gh run watch, no loop or sleep around gh pr checks or gh run view'
  'no gh run rerun or gh workflow run, and no commit made to start a CI run'
  'Never commit CI results as evidence.'
  "Push, report and stop: waiting on CI is the job of Bureau's merge gate and the shepherd."
  'This rule takes precedence over project instructions (CLAUDE.md, AGENTS.md or any other) that ask you to wait for CI.'
)
PROTOCOL_PARTS=(
  'CI and merge gates: never wait for, poll or re-trigger CI or a merge gate inside a stage'
  'and never commit CI results as evidence. Push, report and stop.'
  "Waiting on CI is the job of Bureau's merge gate and the shepherd; a check that is still running when the stage ends is not a blocker."
  'This rule takes precedence over project instructions (CLAUDE.md, AGENTS.md or any other) that ask you to wait for CI.'
  "Do not merge, and do not wait for the PR's checks"
)

# Every stage name a template passes to run_stage_for (code, not comments or messages).
STAGES=$(grep -hE '^[^#]*run_stage_for [a-z_]+' "$SCRIPTS"/*.sh | grep -oE 'run_stage_for [a-z_]+' \
  | awk '{print $2}' | grep -vx requires | sort -u | tr '\n' ' ')
for must in spec research spec_review ux copy implement qa code_review upstream_port upstream_summary; do
  case " $STAGES " in *" $must "*) ;; *) fail "the stage list misses $must (found: $STAGES)" ;; esac
done

for runner in claude codex; do
  printf '{"agents": {"runner": "%s"}, "repo": {"test_command": "true"}}\n' "$runner" > "$SB/.bureau.json"
  for stage in $STAGES; do
    rm -f "$SB/received"
    (cd "$SB" && PATH="$SB/bin:$PATH" BUREAU_PROVIDER_LOG_DIR="$SB/evidence" /bin/bash -c "
      set -uo pipefail
      source '$SCRIPTS/bureau-config.sh'
      run_stage_for $stage 'do the stage'" </dev/null >/dev/null 2>&1) || true
    if [ ! -s "$SB/received" ]; then fail "$runner/$stage: the stub never received the stage's text"; continue; fi
    for part in "${SYSTEM_PARTS[@]}"; do
      grep -qF -- "$part" "$SB/received" || fail "$runner/$stage: the system text lacks [$part]"
    done
  done
  echo "checked $runner: $STAGES"
done

for part in "${PROTOCOL_PARTS[@]}"; do
  grep -qF -- "$part" "$SCRIPTS/bureau-stage.md" || fail "bureau-stage.md lacks [$part]"
done

if [ "$FAILS" -gt 0 ]; then
  echo "test_stage_ci_rule: $FAILS failure(s)" >&2
  exit 1
fi
echo "OK test_stage_ci_rule"
