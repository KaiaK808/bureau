#!/bin/bash
# Verify that classification and its rechecks pin the same chosen base, while
# the actual worker/implement/provider preserve and reuse the merge commit.
set -euo pipefail
source "$(dirname "$0")/lib/merge-worker.sh"
merge_setup_tools
trap pr5_teardown EXIT

base_retry() {
  : > "$SB/forbid-rewrite"
  merge_tick
  merge_eq 22 "$RC" 'chosen base reaches provider on retry'
  merge_eq "$MERGE_HEAD" "$(merge_head)" 'chosen base retains incoming merge'
  merge_eq "$MERGE_HEAD" "$(tail -1 "$SB/provider-heads")" 'provider sees retained merge'
  merge_eq 1 "$(merge_pushes)" 'retry attempts the ordinary failure push'
  merge_has "clean merges of origin/$1" "$ERR" 'resolved base reported'
  merge_no_halt
}

merge_preserved
git -C "$REPO" push -q origin "$BASE_HEAD:refs/heads/release/custom" "$ISSUE_HEAD:refs/heads/trunk"
git -C "$REPO" push -q --force origin "$ISSUE_HEAD:refs/heads/main"
git -C "$REPO" symbolic-ref refs/remotes/origin/HEAD refs/remotes/origin/trunk
jq '.repo.base_branch = "release/custom"' "$REPO/.bureau.json" > "$SB/config"
cp "$SB/config" "$REPO/.bureau.json"
base_retry release/custom
merge_pass 'configured custom base wins over different origin default and main, consistently through preparation and stage startup'

for setting in null '"../invalid"' '"main\n"' 42; do
  merge_preserved
  git -C "$REPO" push -q origin "$BASE_HEAD:refs/heads/trunk"
  git -C "$REPO" push -q --force origin "$ISSUE_HEAD:refs/heads/main"
  git -C "$REPO" symbolic-ref refs/remotes/origin/HEAD refs/remotes/origin/trunk
  jq --argjson setting "$setting" '.repo.base_branch = $setting' \
    "$REPO/.bureau.json" > "$SB/config"
  cp "$SB/config" "$REPO/.bureau.json"
  base_retry trunk
  merge_pass "origin/HEAD base used consistently when configured base is $setting"
done

for missing in configured default fallback; do
  merge_preserved
  case "$missing" in
    configured)
      jq '.repo.base_branch = "missing-base"' "$REPO/.bureau.json" > "$SB/config"
      cp "$SB/config" "$REPO/.bureau.json" ;;
    default)
      git -C "$ORIGIN" symbolic-ref HEAD refs/heads/missing-default
      git -C "$REPO" push -q origin --delete main
      git -C "$REPO" symbolic-ref refs/remotes/origin/HEAD refs/remotes/origin/missing-default ;;
    fallback)
      git -C "$ORIGIN" symbolic-ref HEAD refs/heads/unborn
      git -C "$REPO" push -q origin --delete main ;;
  esac
  SAVED_HEAD=$(merge_head)
  : > "$SB/forbid-rewrite"
  merge_refused "unresolvable $missing base"
  merge_lacks 'forbidden-rewrite' "$ERR" 'unresolved base never resets retained work'
  merge_pass "unresolvable $missing base disables merge-only acceptance and preserves HEAD"
done

echo "OK test_merge_reuse_base: $CASES scenarios"
