#!/bin/bash
# Real worker/runtime/implement/config/provider adapter, fake Linear/gh/Claude,
# real local Git and bare origin. Shared by the clean-base-merge re-acceptance tests.
source "$(dirname "${BASH_SOURCE[0]}")/pr5-interrupt.sh"

merge_setup_tools() {
  pr5_setup
  MERGE_REAL_GIT=$(command -v git)
  cat > "$SB/bin/claude" <<EOF
#!/bin/bash
if [ "\${1:-}" = auth ]; then echo '{"loggedIn":true}'; exit 0; fi
if [ "\${1:-}" = --help ]; then echo '--json-schema --disallowedTools'; exit 0; fi
if [ "\${1:-}" = --version ]; then echo '2.0.0'; exit 0; fi
git rev-parse HEAD >> "$SB/provider-heads"
echo "\$(cat "$SB/provider-error")" >&2
exit 1
EOF
  cat > "$SB/bin/gh" <<'EOF'
#!/bin/bash
if [ "${1:-}" = pr ] && [ "${2:-}" = list ]; then echo 7; exit 0; fi
exit 0
EOF
  cat > "$SB/bin/git" <<EOF
#!/bin/bash
args=("\$@")
dir="\$PWD"
while :; do
  case "\${args[0]:-}" in
    -C) dir="\${args[1]}"; args=("\${args[@]:2}") ;;
    -c) args=("\${args[@]:2}") ;;
    --config-env=*) args=("\${args[@]:1}") ;;
    *) break ;;
  esac
done
sub="\${args[0]:-}"
printf '%s\n' "\$sub" >> "$SB/git-calls"
if [ -f "$SB/forbid-rewrite" ]; then
  case "\$sub" in
    reset|clean) echo forbidden-rewrite >&2; exit 96 ;;
    checkout) for a in "\${args[@]}"; do case "\$a" in -B|--force) echo forbidden-rewrite >&2; exit 96 ;; esac; done ;;
  esac
fi
fault=\$(cat "$SB/git-fault" 2>/dev/null || true)
if [ "\$sub" = fetch ] && [ "\$fault" = fetch ]; then exit 1; fi
if [ "\$sub" = merge-tree ]; then
  [ "\$fault" != merge-tree ] || exit 129
  if [ "\$fault" = head-race ]; then
    rm -f "$SB/git-fault"
    "$MERGE_REAL_GIT" "\$@"
    rc=\$?
    "$MERGE_REAL_GIT" -C "\$dir" commit -q --allow-empty -m 'late writer commit'
    exit "\$rc"
  fi
fi
exec "$MERGE_REAL_GIT" "\$@"
EOF
  chmod +x "$SB/bin/claude" "$SB/bin/gh" "$SB/bin/git"
  CASES=0
}

merge_fixture() {
  pr5_new_repo
  rm -f "$SB/provider-heads" "$SB/git-fault" "$SB/forbid-rewrite" "$SB/deny-push"
  echo 'provider unavailable' > "$SB/provider-error"
  printf 'logs/\n' >> "$REPO/.gitignore"
  echo original > "$REPO/shared.txt"
  git -C "$REPO" add .gitignore shared.txt
  git -C "$REPO" commit -qm foundation
  git -C "$REPO" push -q origin main
  BRANCH=007-merge-retry
  git -C "$REPO" checkout -qb "$BRANCH"
  mkdir -p "$REPO/specs/$BRANCH"
  printf '# Tasks\n- [ ] T001 Implement\n' > "$REPO/specs/$BRANCH/tasks.md"
  echo feature > "$REPO/feature.txt"
  git -C "$REPO" add specs feature.txt
  git -C "$REPO" commit -qm feature
  git -C "$REPO" push -q origin "$BRANCH"
  ISSUE_HEAD=$(git -C "$REPO" rev-parse HEAD)
  git -C "$REPO" checkout -q main
  echo 'ordinary base commit one' > "$REPO/base.txt"
  git -C "$REPO" add base.txt; git -C "$REPO" commit -qm 'base advances by ordinary commit'
  echo 'ordinary base commit two' >> "$REPO/base.txt"
  git -C "$REPO" commit -qam 'base advances again'
  git -C "$REPO" push -q origin main
  BASE_HEAD=$(git -C "$REPO" rev-parse HEAD)
  ORIGIN=$(git -C "$REPO" remote get-url origin)
  jq '.agents.implement = {enabled:true, runner:"claude", push_each_iteration:false}' "$REPO/.bureau.json" > "$SB/config"
  cp "$SB/config" "$REPO/.bureau.json"
  printf s5 > "$SB/state"
  jq -nc --arg b "<!-- bureau-branch: $BRANCH -->" '{issue:"7", body:$b}' > "$SB/comments.jsonl"
  cat > "$ORIGIN/hooks/pre-receive" <<EOF
#!/bin/sh
while read -r old new ref; do
  if [ -f "$SB/deny-push" ] && [ "\$ref" = "refs/heads/$BRANCH" ]; then echo 'test rejects push' >&2; exit 1; fi
done
EOF
  chmod +x "$ORIGIN/hooks/pre-receive"
  IWT="$REPO/.worktrees/queue-implement"
  : > "$SB/git-calls"
}

merge_tick() {
  set +e
  (cd "$REPO" && _pr5_env bash scripts/bureau-worker.sh EXP-7 implement-pipeline.sh "$IWT" "$BRANCH" > "$SB/out" 2> "$SB/err")
  RC=$?
  set -e
  OUT=$(cat "$SB/out"); ERR=$(cat "$SB/err")
}

merge_fail() {
  echo "FAIL $* (rc=${RC:-unknown})" >&2
  tail -30 "$SB/err" 2>/dev/null >&2 || true
  exit 1
}
merge_eq() { [ "$1" = "$2" ] || merge_fail "$3: expected $1, got $2"; }
merge_has() { grep -qF "$1" <<< "$2" || merge_fail "$3: missing $1"; }
merge_lacks() { if grep -qF "$1" <<< "$2"; then merge_fail "$3: unexpected $1"; fi; }
merge_pass() { CASES=$((CASES + 1)); echo "PASS $CASES $*"; }
merge_head() { git -C "$IWT" rev-parse HEAD; }
merge_key() { printf '%s' "$IWT" | shasum -a 256 | cut -d' ' -f1; }
merge_pushes() { grep -c '^push$' "$SB/git-calls" || true; }
merge_remote() { git -C "$ORIGIN" rev-parse "$BRANCH"; }
merge_no_halt() {
  merge_lacks 'needs-human' "$(pr5_writes 7)" 'no human halt'
  merge_eq '{}' "$(jq -c . "$COMMON/bureau/leases.json")" 'ordinary failure releases leases'
}

# First tick failed with its clean merge still local. This is genuine provider
# exit 22/23 via the real adapter, with the push refused by a real receive hook.
merge_preserved() {
  merge_fixture
  local code="${1:-22}"
  if [ "$code" = 23 ]; then echo quota > "$SB/provider-error"; fi
  : > "$SB/deny-push"
  merge_tick
  merge_eq "$code" "$RC" 'first tick provider failure'
  merge_eq 1 "$(merge_pushes)" 'first tick attempts the default failure push'
  MERGE_HEAD=$(merge_head)
  merge_eq "$ISSUE_HEAD" "$(merge_remote)" 'merge stays local'
  [ ! -f "$COMMON/bureau/workers/$(merge_key)" ] || merge_fail 'worker stayed registered'
  merge_eq "$code" "$(jq .exit_code "$COMMON/bureau/preserved/$(merge_key).json")" 'failure recorded'
  merge_no_halt
  : > "$SB/git-calls"
}

merge_refused() {
  local expected="$1"
  merge_tick
  merge_eq 21 "$RC" "$expected rejected"
  merge_eq "$SAVED_HEAD" "$(merge_head)" "$expected HEAD preserved"
  merge_eq 1 "$(wc -l < "$SB/provider-heads" | tr -d ' ')" "$expected never reaches provider again"
  merge_has 'needs-human' "$(pr5_writes 7)" "$expected ownership halt"
}
