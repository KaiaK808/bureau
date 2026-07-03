#!/bin/bash
# test_orchestrate_executor.sh — the d&a executor (orchestrate.sh --execute) with
# a STUB shepherd injected via $SHEPHERD_BIN. No Linear, no git, no real pipeline.
# Asserts: honest exit, coverage, concurrency+cap, per-lane worktree isolation,
# serial-chain ordering. (Ported from brainhuggers-cli.)

set -uo pipefail
TESTS_DIR="$(cd "$(dirname "$0")" && pwd)"
ORCH="$TESTS_DIR/../templates/scripts/orchestrate.sh"
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT

LOG="$TMP/calls.log"; MARK="$TMP/markers"; mkdir -p "$MARK"; COUNTS="$TMP/counts.log"

STUB="$TMP/stub-shepherd.sh"
cat > "$STUB" <<'STUBEOF'
#!/bin/bash
wt=""; ticket=""
while [ $# -gt 0 ]; do
  case "$1" in
    --worktree) wt="$2"; shift 2 ;;
    --no-tmux|--no-merge|--dry-run|--respect-config) shift ;;
    --from-stage) shift 2 ;;
    *) ticket="$1"; shift ;;
  esac
done
echo "$ticket $wt" >> "$STUB_LOG"
touch "$STUB_MARK/$ticket"
ls "$STUB_MARK" | wc -l | tr -d ' ' >> "$STUB_COUNTS"
sleep 3
rm -f "$STUB_MARK/$ticket"
[ "$ticket" = "${STUB_FAIL:-}" ] && exit 17
exit 0
STUBEOF
chmod +x "$STUB"

SCHED="$TMP/sched.json"
echo '{"serialChains":[["EXP-1","EXP-2"]],"parallelSafe":["EXP-3","EXP-4","EXP-5","EXP-6"]}' > "$SCHED"

SHEPHERD_BIN="$STUB" STUB_LOG="$LOG" STUB_MARK="$MARK" STUB_COUNTS="$COUNTS" STUB_FAIL="EXP-5" \
  bash "$ORCH" --execute --max-concurrent 3 --schedule "$SCHED" >"$TMP/out.log" 2>&1
RC=$?

fail=0
check() { if eval "$2"; then echo "ok   - $1"; else echo "FAIL - $1 (got: ${3:-})"; fail=1; fi; }

check "honest exit propagates rc=17" '[ "$RC" -eq 17 ]' "$RC"
n=$(wc -l < "$LOG" | tr -d ' ')
check "all 6 tickets shepherded" '[ "$n" -eq 6 ]' "$n"
maxc=$(sort -n "$COUNTS" | tail -1)
check "concurrency overlaps (max>=2)" '[ "$maxc" -ge 2 ]' "max=$maxc"
check "concurrency cap respected (max<=3)" '[ "$maxc" -le 3 ]' "max=$maxc"
psafe_wts=$(grep -E '^EXP-[3456] ' "$LOG" | awk '{print $2}' | sort -u | wc -l | tr -d ' ')
check "4 distinct parallelSafe worktrees" '[ "$psafe_wts" -eq 4 ]' "$psafe_wts"
chain_wts=$(grep -E '^EXP-[12] ' "$LOG" | awk '{print $2}' | sort -u | wc -l | tr -d ' ')
check "serialChain shares 1 worktree" '[ "$chain_wts" -eq 1 ]' "$chain_wts"
i1=$(grep -n '^EXP-1 ' "$LOG" | head -1 | cut -d: -f1)
i2=$(grep -n '^EXP-2 ' "$LOG" | head -1 | cut -d: -f1)
check "serialChain ordered (EXP-1 before EXP-2)" '[ "$i1" -lt "$i2" ]' "$i1<$i2"

[ "$fail" -eq 0 ] && echo "PASS — orchestrate executor"
exit "$fail"
