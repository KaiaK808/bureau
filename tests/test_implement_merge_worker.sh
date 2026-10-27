#!/bin/bash
# Two ticks through the real worker and failing provider, plus conservative
# re-acceptance and executable negative controls. No provider/network calls.
set -euo pipefail
source "$(dirname "$0")/lib/merge-worker.sh"
merge_setup_tools
trap pr5_teardown EXIT

# Main already advanced by two ordinary commits, then implement cleanly merged
# it. Even a refused default failure push must not wedge the second tick.
merge_preserved
: > "$SB/forbid-rewrite"
merge_tick
merge_eq 22 "$RC" 'second tick reaches failing provider'
merge_eq "$MERGE_HEAD" "$(merge_head)" 'worker and implement retain merge HEAD'
merge_eq "$MERGE_HEAD" "$(tail -1 "$SB/provider-heads")" 'provider sees retained HEAD'
merge_eq 2 "$(wc -l < "$SB/provider-heads" | tr -d ' ')" 'two provider calls'
merge_has 'Re-accepted the clean preserved worktree' "$ERR" 're-acceptance'
merge_has 'Retaining verified base-merge HEAD' "$OUT" 'stage checkout retains HEAD'
merge_lacks 'forbidden-rewrite' "$ERR" 'neither resetting checkout runs'
merge_no_halt
merge_pass 'default refused push: ordinary main commits plus clean merge reused on tick two'

# A failed pass still publishes the clean merge once. The registered worker
# can then reset without loss on the next tick.
for code in 22 23; do
  merge_fixture
  if [ "$code" = 23 ]; then echo quota > "$SB/provider-error"; fi
  merge_tick
  merge_eq "$code" "$RC" 'failure exit retained'
  MERGE_HEAD=$(merge_head)
  merge_eq 1 "$(merge_pushes)" 'one failure push for the merge-only state'
  merge_eq "$MERGE_HEAD" "$(merge_remote)" 'failure pushes merge to origin'
  merge_tick
  merge_eq "$code" "$RC" 'on-origin retry reaches provider'
  merge_eq 1 "$(merge_pushes)" 'retry has nothing new to push'
  merge_eq "$MERGE_HEAD" "$(tail -1 "$SB/provider-heads")" 'retry sees pushed merge'
  merge_no_halt
  merge_pass "failure $code pushes the merge-only state once, then retries without another push"
done

merge_preserved 23
: > "$SB/forbid-rewrite"
merge_tick
merge_eq 23 "$RC" 'quota retry reaches provider'
merge_eq 1 "$(merge_pushes)" 'quota retry attempts the ordinary failure push'
merge_eq "$ISSUE_HEAD" "$(merge_remote)" 'refused quota push leaves origin unchanged'
merge_eq "$MERGE_HEAD" "$(merge_head)" 'quota retains HEAD'
merge_no_halt
merge_pass 'ordinary quota failure 23 with a refused push also reuses the worktree'

merge_preserved
git -C "$IWT" commit -q --allow-empty -m 'own ordinary commit'
SAVED_HEAD=$(merge_head)
merge_refused ordinary
merge_pass 'own ordinary commit still rejected with 21, needs-human and unchanged HEAD'

merge_preserved
echo extra >> "$IWT/feature.txt"
git -C "$IWT" commit -qam 'extra in merge' --amend
SAVED_HEAD=$(merge_head)
merge_refused extra-change
merge_pass 'merge with an extra tree change rejected'

merge_preserved
echo dirty >> "$IWT/feature.txt"
SAVED_HEAD=$(merge_head)
merge_refused tracked-dirt
merge_has dirty "$(cat "$IWT/feature.txt")" 'tracked dirt retained'
merge_pass 'tracked dirt rejected and retained'

merge_preserved
echo notes > "$IWT/notes.txt"
SAVED_HEAD=$(merge_head)
merge_refused untracked-dirt
merge_eq notes "$(cat "$IWT/notes.txt")" 'untracked file retained'
merge_pass 'untracked dirt rejected and retained'

for fault in merge-tree fetch; do
  merge_preserved
  echo "$fault" > "$SB/git-fault"
  SAVED_HEAD=$(merge_head)
  merge_refused "$fault unavailable"
  merge_pass "$fault unavailable: fail closed without resetting"
done

# Multiple clean base merges, with ordinary main advancement between them.
merge_preserved
echo third >> "$REPO/base.txt"
git -C "$REPO" commit -qam 'ordinary third base commit'; git -C "$REPO" push -q origin main
git -C "$IWT" fetch -q origin
git -C "$IWT" merge -q --no-ff --no-edit origin/main
MERGE_HEAD=$(merge_head)
: > "$SB/forbid-rewrite"
merge_tick
merge_eq 22 "$RC" 'two clean merges accepted'
merge_eq "$MERGE_HEAD" "$(tail -1 "$SB/provider-heads")" 'both merges retained'
merge_no_halt
merge_pass 'a chain of two clean base merges is accepted'

# Construct an actual unresolved-parent merge, with the branch parent already
# on origin, so rejecting an ordinary local commit cannot mask this guard.
merge_preserved
rm -f "$SB/deny-push"
echo branch > "$IWT/shared.txt"; git -C "$IWT" commit -qam 'branch edits shared file'
git -C "$IWT" push -q origin "$BRANCH"
echo base > "$REPO/shared.txt"; git -C "$REPO" commit -qam 'base edits shared file'
git -C "$REPO" push -q origin main; git -C "$IWT" fetch -q origin
if git -C "$IWT" merge --no-ff --no-edit origin/main > "$SB/conflict" 2>&1; then merge_fail 'fixture merge did not conflict'; fi
echo resolution > "$IWT/shared.txt"
git -C "$IWT" add shared.txt; git -C "$IWT" commit -qm 'custom resolution'
SAVED_HEAD=$(merge_head)
merge_refused conflict-resolution
merge_pass 'actual conflicting parents with a committed resolution are rejected'

# Negative control: keep the parent/ancestry checks but accept any resulting
# merge tree. The same conflict-resolution case must now be wrongly accepted.
python3 - "$REPO/scripts/bureau-config.sh" <<'PY'
from pathlib import Path
import sys
p = Path(sys.argv[1]); text = p.read_text()
old = 'merged=$(git -C "$wt" merge-tree --write-tree "${parent_row[1]}" "${parent_row[2]}" 2>/dev/null) || return 1'
assert old in text
p.write_text(text.replace(old, 'merged=$(git -C "$wt" rev-parse --verify "$commit^{tree}") || return 1', 1))
PY
merge_tick
merge_eq 22 "$RC" 'control accepts resolved merge and reaches provider'
merge_has 'Re-accepted the clean preserved worktree' "$ERR" 'control wrongly accepts resolution'
merge_pass 'NEGATIVE CONTROL accept any merge: conflict-resolution wrongly accepted'

merge_preserved
python3 - "$REPO/scripts/bureau-config.sh" <<'PY'
from pathlib import Path
import sys
p = Path(sys.argv[1]); text = p.read_text()
old = 'rev-list "$issue_head..$head" --not "$base_head"'
assert old in text
p.write_text(text.replace(old, 'rev-list "$issue_head..$head"', 1))
PY
SAVED_HEAD=$(merge_head)
merge_refused 'control without base exclusion'
merge_pass 'NEGATIVE CONTROL drop --not base: ordinary main advancement wrongly rejected'

merge_preserved
echo head-race > "$SB/git-fault"
merge_tick
merge_eq 21 "$RC" 'writer during classification rejected'
merge_eq 'late writer commit' "$(git -C "$IWT" log -1 --format=%s)" 'late commit retained'
merge_eq 1 "$(wc -l < "$SB/provider-heads" | tr -d ' ')" 'no second provider after race'
merge_pass 'HEAD changed during classification: reject and preserve the new writer commit'

# A writer arrives after worker classification, immediately before stage
# startup. The retained-head marker must never fall back to checkout -B.
merge_preserved
python3 - "$REPO/scripts/bureau-config.sh" <<'PY'
from pathlib import Path
import sys
p = Path(sys.argv[1]); text = p.read_text()
old = '    export BUREAU_RETAINED_MERGE_HEAD="$retain_head"'
assert old in text
p.write_text(text.replace(old, old + '\n    git -C "$wt" commit -q --allow-empty -m "writer after re-acceptance"', 1))
PY
: > "$SB/forbid-rewrite"
merge_tick
merge_eq 21 "$RC" 'writer after worker classification rejected at stage startup'
merge_eq 'writer after re-acceptance' "$(git -C "$IWT" log -1 --format=%s)" 'writer commit retained'
merge_eq 1 "$(wc -l < "$SB/provider-heads" | tr -d ' ')" 'no second provider after startup race'
merge_lacks 'forbidden-rewrite' "$ERR" 'no fallback resetting checkout'
merge_pass 'HEAD changes after worker classification: stage refuses and preserves the new commit'

merge_preserved
python3 - "$REPO/scripts/bureau-config.sh" "$SB" <<'PY'
from pathlib import Path
import sys
p = Path(sys.argv[1]); text = p.read_text()
old = '    export BUREAU_RETAINED_MERGE_HEAD="$retain_head"'
assert old in text
hook = '\n    if [ -f "' + sys.argv[2] + '/fail-preparation" ]; then rm -f "' + sys.argv[2] + '/fail-preparation"; return 24; fi'
p.write_text(text.replace(old, old + hook, 1))
PY
: > "$SB/forbid-rewrite"; : > "$SB/fail-preparation"
merge_tick
merge_eq 24 "$RC" 'preparation failure after restoring registration'
[ -f "$COMMON/bureau/workers/$(merge_key)" ] || merge_fail 'fixture did not restore registration'
merge_tick
merge_eq 22 "$RC" 'next preparation revalidates despite restored registration'
merge_eq "$MERGE_HEAD" "$(tail -1 "$SB/provider-heads")" 'preparation failure never authorizes reset'
merge_lacks 'forbidden-rewrite' "$ERR" 'retry after preparation failure does not reset'
merge_no_halt
merge_pass 'failure between re-acceptance and cleanup cannot authorize a reset on the next tick'

for code in 124 130 null; do
  merge_preserved
  note="$COMMON/bureau/preserved/$(merge_key).json"
  jq --argjson code "$code" '.exit_code = $code | if $code == 130 then .reason = "interrupted" else . end' "$note" > "$SB/note"
  cp "$SB/note" "$note"
  SAVED_HEAD=$(merge_head)
  merge_refused "failure record $code"
  merge_pass "failure record $code never enters the ordinary-failure merge resume"
done

echo "OK test_implement_merge_worker: $CASES scenarios (including 2 negative controls)"
