#!/bin/bash
# The bureau-branch marker readers skip comments whose body is empty or null.
# get_issue_branch and get_issue_branch_and_comments are cut out of the shipped
# bureau-config.sh and run on a stubbed Linear answer; jq used to stop with 5 on such a
# comment ("cannot be matched" / "cannot be split"). get_issue_branch then fell back to
# Linear's generated branch name although a marker existed (the jq error ends in a pipe),
# and get_issue_branch_and_comments failed with 5.
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "$0")" && cd .. && pwd)"
SRC="$REPO_ROOT/templates/scripts/bureau-config.sh"
FNS=$(sed -n -e '/^get_issue_branch() {/,/^}/p' -e '/^get_issue_branch_and_comments() {/,/^}/p' "$SRC")
case "$FNS" in
  *'get_issue_branch() {'*'get_issue_branch_and_comments() {'*) : ;;
  *) echo "FAIL: could not cut the marker readers out of $SRC" >&2; exit 1 ;;
esac
eval "$FNS"
_BUREAU_SHAPE_ISSUE_COMMENTS=comments
# linear_issue_query stub: prints $ANSWER.
linear_issue_query() { printf '%s' "$ANSWER"; }

fail=0
check() {  # <label> <expected> <actual> <rc>
  if [ "$2" = "$3" ] && [ "$4" = 0 ]; then echo "PASS $1"; else echo "FAIL $1: expected '$2' rc 0, got '$3' rc $4" >&2; fail=1; fi
}
answer() {  # <comments JSON array> → a Linear answer with branchName fallback-branch
  printf '{"data":{"issues":{"nodes":[{"branchName":"someone/exp-7-fallback-branch","comments":{"nodes":%s}}]}}}' "$1"
}
MARKER='{"body":"<!-- bureau-branch: 004-own-feature -->\n**Spec Artifacts**","createdAt":"2026-01-02T00:00:00Z"}'
EMPTY='{"body":"","createdAt":"2026-01-04T00:00:00Z"}'
NULL='{"body":null,"createdAt":"2026-01-03T00:00:00Z"}'

for case in "an empty body|[$EMPTY,$MARKER]" "a null body|[$NULL,$MARKER]" "an empty and a null body|[$EMPTY,$NULL,$MARKER]"; do
  ANSWER=$(answer "${case#*|}")
  out=$(get_issue_branch EXP-7); rc=$?
  check "get_issue_branch reads the marker behind ${case%%|*}" 004-own-feature "$out" "$rc"
  out=$(get_issue_branch_and_comments EXP-7 | jq -r '.branch'); rc=$?
  check "get_issue_branch_and_comments reads the marker behind ${case%%|*}" 004-own-feature "$out" "$rc"
done
ANSWER=$(answer "[$EMPTY,$NULL]")
out=$(get_issue_branch EXP-7); rc=$?
check "no marker among empty bodies → Linear's branch name" someone/exp-7-fallback-branch "$out" "$rc"
out=$(get_issue_branch_and_comments EXP-7 | jq -r '.branch + " " + (.comments | length | tostring)'); rc=$?
check "same for the combined read, which keeps every comment" "someone/exp-7-fallback-branch 2" "$out" "$rc"

[ "$fail" = 0 ] || exit 1
echo "OK test_marker_empty_body"
