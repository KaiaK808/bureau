#!/bin/bash
# Every stage's model call carries the rule for linked paths in its system text.
#
# repo.worktree_links links a path such as .venv from the main checkout into every stage
# worktree as soon as the resynced scripts are on disk. The longer rule lives in
# scripts/bureau-stage.md, which an agent reads from its worktree, i.e. only once the resynced
# file is committed to main (and to the branch). The system text comes from the main
# checkout's run_stage_for, so it carries the rule from the first reset on. This test sends a
# prompt through the real run_stage_for and bureau-provider.py and reads what a stub claude
# (system prompt file) and a stub codex (prompt on stdin) actually received.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")" && cd .. && pwd)"
SCRIPTS="$REPO_ROOT/templates/scripts"
SB=$(mktemp -d -t bureau-test.system.XXXXXXXX)
trap 'rm -rf "$SB"' EXIT
fail() { echo "FAIL $*" >&2; exit 1; }

git -C "$SB" init -q
write_config() {  # $1 = runner
  cat > "$SB/.bureau.json" <<EOF
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
  "agents": { "runner": "$1", "poll_interval_minutes": 30, "max_review_cycles": 3 },
  "repo": { "branch_prefix": "feat", "specs_dir": "specs", "test_command": "true" }
}
EOF
}

mkdir -p "$SB/bin"
# claude: the provider passes the system text as --append-system-prompt-file FILE.
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
# codex: the provider puts the system text in front of the prompt on stdin.
cat > "$SB/bin/codex" <<EOF
#!/bin/bash
if [ "\$1" = login ]; then exit 0; fi
cat > "$SB/received"
echo '{"type":"turn.completed","usage":{"input_tokens":1,"output_tokens":1}}'
EOF
chmod +x "$SB/bin/claude" "$SB/bin/codex"

RULE_START='If a path in your worktree (such as .venv) is a symlink that points outside the worktree'
RULE_KEEP='never delete, recreate or --clear it, and do not install into it unless the ticket asks'
RULE_USUAL='If it is missing or not a symlink, handle it as usual.'

for runner in claude codex; do
  write_config "$runner"
  rm -f "$SB/received"
  (cd "$SB" && PATH="$SB/bin:$PATH" /bin/bash -c "
    set -uo pipefail
    source '$SCRIPTS/bureau-config.sh'
    run_stage_for implement 'implement the tasks'" </dev/null >/dev/null 2>&1) || true
  [ -s "$SB/received" ] || fail "$runner: the stub never received the stage's text"
  for part in "$RULE_START" "$RULE_KEEP" "$RULE_USUAL"; do
    grep -qF -- "$part" "$SB/received" || fail "$runner: the system text lacks [$part]"
  done
  echo "PASS $runner receives the linked-path rule in the stage's system text"
done

echo "OK test_stage_system_text"
