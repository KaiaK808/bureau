#!/bin/bash
# Every stage's model call carries the CI rule (v3.2): never wait for, poll or re-trigger CI or
# a merge gate inside a stage, never commit CI results as evidence; when the stage's work is
# done, report and stop; a missing, pending or failed CI run does not change the reported
# status. It wins over project instructions that ask the agent to wait for CI, and it names no
# Git step of its own (Codex and read-only stages leave Git to the shell). Implement passes were
# killed at their time limit while polling the checks of finished, pushed work, because an
# installation's own CLAUDE.md told the agent to wait for a missing CI run.
#
# The stages are every name a template script passes to run_stage_for. Each one sends a prompt
# through the real run_stage_for and bureau-provider.py; the test reads what a stub claude
# (system prompt file) and a stub codex (prompt on stdin) actually received. The rule is also
# in scripts/bureau-stage.md, the protocol app stages and agents read from the worktree, and
# the app implement command asks for passing tests, not passing checks.
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
  "When this stage's work is done (committed and pushed where this stage does that), report and stop: waiting on CI is the job of Bureau's merge gate and the shepherd."
  "A missing, pending or failed CI run does not change the status you report for your own work, and not merging is all that a project rule such as 'do not merge without green CI' asks of a stage."
  'This rule takes precedence over project instructions (CLAUDE.md, AGENTS.md or any other) that ask you to wait for CI.'
)
PROTOCOL_PARTS=(
  'CI and merge gates: never wait for, poll or re-trigger CI or a merge gate inside a stage'
  'and never commit CI results as evidence.'
  "When the stage's work is done (committed and pushed where the stage does that; Codex and read-only stages leave Git to the Bureau shell), report and stop."
  "Waiting on CI is the job of Bureau's merge gate and the shepherd; a check that is still running when the stage ends is not a blocker."
  'A missing, pending or failed CI run does not change the status or outcome you report for your own work, and not merging is all that a project rule such as "do not merge without green CI" asks of a stage.'
  'This rule takes precedence over project instructions (CLAUDE.md, AGENTS.md or any other) that ask you to wait for CI.'
  "Do not merge, and do not wait for the PR's checks"
)
# The rule must not push anyone to commit: Codex stages and read-only stages do not.
NOT_PARTS=('Push, report and stop')

# Every stage name a template passes to run_stage_for (code, not comments or messages).
STAGES=$(grep -hE '^[^#]*run_stage_for [a-z_]+' "$SCRIPTS"/*.sh | grep -oE 'run_stage_for [a-z_]+' \
  | awk '{print $2}' | grep -vx requires | sort -u | tr '\n' ' ')
for must in spec research spec_review ux copy implement qa code_review upstream_port upstream_summary; do
  case " $STAGES " in *" $must "*) ;; *) fail "the stage list misses $must (found: $STAGES)" ;; esac
done

# The three ways the stages call run_stage_for: a prompt alone; with a system text of the
# stage's own (--append-system-prompt, the /goal path of implement-pipeline.sh); with a
# result schema (--schema, the implement iterations, QA and the review). The stage's own
# system text must arrive next to the rule, not instead of it.
OWN_SYSTEM='STAGE-OWN-SYSTEM-TEXT-7f3a'
FORMS='plain system schema'
for runner in claude codex; do
  printf '{"agents": {"runner": "%s"}, "repo": {"test_command": "true"}}\n' "$runner" > "$SB/.bureau.json"
  for stage in $STAGES; do
    for form in $FORMS; do
      case "$form" in
        plain)  call="run_stage_for $stage 'do the stage'" ;;
        system) call="run_stage_for $stage --append-system-prompt '$OWN_SYSTEM' 'do the stage'" ;;
        schema) call="run_stage_for $stage --schema '$SCRIPTS/bureau-implement.schema.json' 'do the stage'" ;;
      esac
      rm -f "$SB/received"
      (cd "$SB" && PATH="$SB/bin:$PATH" BUREAU_PROVIDER_LOG_DIR="$SB/evidence" /bin/bash -c "
        set -uo pipefail
        source '$SCRIPTS/bureau-config.sh'
        $call" </dev/null >/dev/null 2>&1) || true
      if [ ! -s "$SB/received" ]; then fail "$runner/$stage/$form: the stub never received the stage's text"; continue; fi
      for part in "${SYSTEM_PARTS[@]}"; do
        grep -qF -- "$part" "$SB/received" || fail "$runner/$stage/$form: the system text lacks [$part]"
      done
      for part in "${NOT_PARTS[@]}"; do
        if grep -qF -- "$part" "$SB/received"; then fail "$runner/$stage/$form: the system text still says [$part]"; fi
      done
      if [ "$form" = system ] && ! grep -qF -- "$OWN_SYSTEM" "$SB/received"; then
        fail "$runner/$stage/$form: the stage's own system text did not arrive"
      fi
    done
  done
  echo "checked $runner ($FORMS): $STAGES"
done

for part in "${PROTOCOL_PARTS[@]}"; do
  grep -qF -- "$part" "$SCRIPTS/bureau-stage.md" || fail "bureau-stage.md lacks [$part]"
done
for part in "${NOT_PARTS[@]}"; do
  if grep -qF -- "$part" "$SCRIPTS/bureau-stage.md"; then fail "bureau-stage.md still says [$part]"; fi
done
# The app implement command asks for passing tests, not passing PR checks.
grep -qF 'all required tasks are done and the tests pass' "$REPO_ROOT/templates/commands/linear-implement.md" \
  || fail "linear-implement.md does not say the tests pass"
if grep -qF 'are done and checks pass' "$REPO_ROOT/templates/commands/linear-implement.md"; then
  fail "linear-implement.md still asks for passing checks"
fi

if [ "$FAILS" -gt 0 ]; then
  echo "test_stage_ci_rule: $FAILS failure(s)" >&2
  exit 1
fi
echo "OK test_stage_ci_rule"
