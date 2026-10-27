#!/bin/bash
# Repaired merges through the real worker, implementer and failing provider.
set -euo pipefail
source "$(dirname "$0")/lib/merge-worker.sh"
PR5_SCRIPTS="${BUREAU_CONFLICT_TEST_SCRIPTS:-$PR5_SCRIPTS}"
merge_setup_tools
trap pr5_teardown EXIT

conflict_fixture() {
  merge_fixture
  printf 'base output\n' > "$REPO/generated.txt"
  printf '# Changes\n## Unreleased\n\n## Released\nold\n' > "$REPO/CHANGELOG.md"
  git -C "$REPO" add generated.txt CHANGELOG.md
  git -C "$REPO" commit -qm 'conflict foundation'
  git -C "$REPO" checkout -q "$BRANCH"
  git -C "$REPO" merge -q --no-edit main
  printf 'ours output\n' > "$REPO/generated.txt"
  awk '{print} /^## Unreleased$/{print "- ours"}' "$REPO/CHANGELOG.md" > "$SB/changelog"
  cp "$SB/changelog" "$REPO/CHANGELOG.md"
  git -C "$REPO" add generated.txt CHANGELOG.md
  git -C "$REPO" commit -qm 'feature changes'
  git -C "$REPO" push -q origin "$BRANCH"
  ISSUE_HEAD=$(git -C "$REPO" rev-parse HEAD)
  git -C "$REPO" checkout -q main
  printf 'theirs output\n' > "$REPO/generated.txt"
  awk '{print} /^## Unreleased$/{print "- theirs"}' "$REPO/CHANGELOG.md" > "$SB/changelog"
  cp "$SB/changelog" "$REPO/CHANGELOG.md"
  git -C "$REPO" add generated.txt CHANGELOG.md
  git -C "$REPO" commit -qm 'main changes'
  git -C "$REPO" push -q origin main
  BASE_HEAD=$(git -C "$REPO" rev-parse HEAD)
  jq '.repo.test_command = "cmp CHANGELOG.md generated.txt" | .repo.conflict_resolution = {
    generated:[{globs:["generated.txt"],command:["cp","CHANGELOG.md","generated.txt"]}],
    append_only_paths:["CHANGELOG.md"]}' "$REPO/.bureau.json" > "$SB/config"
  cp "$SB/config" "$REPO/.bureau.json"
  : > "$SB/git-calls"
}

if [ "${BUREAU_CONFLICT_WORKER_CASE:-all}" != unpublished ]; then
conflict_fixture
merge_tick
merge_eq 22 "$RC" 'repair succeeds before genuine provider failure'
MERGE_HEAD=$(merge_head)
merge_eq "$MERGE_HEAD" "$(merge_remote)" 'ordinary failure push publishes repaired merge'
merge_has 'ours' "$(cat "$IWT/generated.txt")" 'generated output keeps ours'
merge_has 'theirs' "$(cat "$IWT/generated.txt")" 'generated output keeps theirs'
merge_no_halt
merge_tick
merge_eq 22 "$RC" 'on-origin repaired merge reaches provider on retry'
merge_eq "$MERGE_HEAD" "$(merge_head)" 'retry preserves repaired contents'
merge_eq 2 "$(wc -l < "$SB/provider-heads" | tr -d ' ')" 'two genuine provider calls'
merge_no_halt
merge_pass 'repaired merge survives provider failure, normal push and on-origin worker reuse'
fi

if [ "${BUREAU_CONFLICT_WORKER_CASE:-all}" != published ]; then
conflict_fixture
: > "$SB/deny-push"
merge_tick
merge_eq 22 "$RC" 'refused push retains provider exit'
MERGE_HEAD=$(merge_head)
merge_eq "$ISSUE_HEAD" "$(merge_remote)" 'remote stays unchanged'
merge_eq '' "$(git -C "$IWT" status --porcelain)" 'preserved repaired checkout is clean'
[ ! -f "$COMMON/bureau/workers/$(merge_key)" ] || merge_fail 'repaired unpublished worker stayed registered'
merge_eq 22 "$(jq .exit_code "$COMMON/bureau/preserved/$(merge_key).json")" 'failure saved'
SAVED_HEAD="$MERGE_HEAD"
merge_refused 'unpublished repaired merge'
merge_has 'ours' "$(cat "$IWT/generated.txt")" 'ownership refusal retains ours'
merge_has 'theirs' "$(cat "$IWT/generated.txt")" 'ownership refusal retains theirs'
merge_pass 'unpublished repaired merge is preserved and conservatively refused with 21 on retry'
fi
echo "OK $CASES conflict worker cases"
