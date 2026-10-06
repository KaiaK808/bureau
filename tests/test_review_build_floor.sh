#!/bin/bash
# A red build must never soften a BLOCK verdict.
#
# The review stage folds the build result into the verdict. That fold used to be one
# unconditional line — `[ "$BUILD_OK" = false ] && VERDICT="REQUEST_CHANGES"` — so a build
# that was red for an environmental reason (missing dependencies, no network, a broken stub)
# rewrote a BLOCK carrying a security finding into ordinary rework and skipped `needs-human`.
#
# This test runs the REAL helpers from templates/scripts/bureau-config.sh (apply_build_failure
# and decide_review_verdict, which folds the build for the stage) and checks that the stage
# calls the decision instead of folding on its own. It does not re-implement either.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")" && cd .. && pwd)"
SCRIPTS="$REPO_ROOT/templates/scripts"
SANDBOX=$(mktemp -d -t bureau-test.buildfloor.XXXXXXXX)
trap 'rm -rf "$SANDBOX"' EXIT

fail() { echo "FAIL $*" >&2; exit 1; }

# --- the table itself -------------------------------------------------------
# Sourced from the real config; the minimal .bureau.json keeps its source-time jq reads happy.
cat > "$SANDBOX/.bureau.json" <<'EOF'
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

cd "$SANDBOX"
# shellcheck disable=SC1091
source "$SCRIPTS/bureau-config.sh"

for pair in "BLOCK:BLOCK" "APPROVE:REQUEST_CHANGES" "REQUEST_CHANGES:REQUEST_CHANGES" \
            ":BLOCK" "approve:BLOCK" "UNKNOWN:BLOCK"; do
  input="${pair%%:*}"; want="${pair##*:}"
  got=$(apply_build_failure "$input")
  [ "$got" = "$want" ] || fail "apply_build_failure '$input' gave '$got', wanted '$want'"
done
echo "PASS apply_build_failure keeps a BLOCK and falls closed on anything unknown"

# --- the fold inside the stage's verdict decision ----------------------------
# The fold now runs inside decide_review_verdict (security floor before it, cycle cap
# after it; the order is held by tests/test_review_verdict_order.sh). The stage must call
# that decision and no longer fold on its own.
grep -q 'decide_review_verdict "$VERDICT"' "$SCRIPTS/code-review-pipeline.sh" \
  || fail "the review stage no longer runs its verdict through decide_review_verdict"
if grep -q 'VERDICT=$(apply_build_failure' "$SCRIPTS/code-review-pipeline.sh"; then
  fail "the review stage folds the build on its own again, outside the ordered decision"
fi

run_fold() {  # $1 = build_ok, $2 = incoming verdict; echoes "<verdict>|<review text>"
  local out
  out=$(decide_review_verdict "$2" 0 0 "$1" 0 3)
  printf '%s|%s' "$(sed -n 1p <<< "$out")" \
    "$(printf '%s\n' "$out" | tail -n +2 | awk -F'\037' '$1 == "build" {print $3}')"
}

for pair in "BLOCK:BLOCK" "APPROVE:REQUEST_CHANGES" "REQUEST_CHANGES:REQUEST_CHANGES" "NONSENSE:BLOCK"; do
  input="${pair%%:*}"; want="${pair##*:}"
  out=$(run_fold false "$input")
  [ "${out%%|*}" = "$want" ] || fail "red build turned '$input' into '${out%%|*}', wanted '$want'"
  case "${out#*|}" in
    'BUILD FAILURE: Must be fixed.'*) ;;
    *) fail "the build-failure note changed or is missing for '$input': '${out#*|}'" ;;
  esac
done
echo "PASS the stage's decision holds the floor on a red build"

for input in BLOCK APPROVE REQUEST_CHANGES; do
  out=$(run_fold true "$input")
  [ "${out%%|*}" = "$input" ] || fail "green build changed '$input' into '${out%%|*}'"
  [ -z "${out#*|}" ] || fail "green build appended a build-failure note to '$input'"
done
echo "PASS a green build leaves verdict and review text untouched"
