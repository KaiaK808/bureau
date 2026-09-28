#!/bin/bash
# A red build must never soften a BLOCK verdict.
#
# The review stage folds the build result into the verdict. That fold used to be one
# unconditional line — `[ "$BUILD_OK" = false ] && VERDICT="REQUEST_CHANGES"` — so a build
# that was red for an environmental reason (missing dependencies, no network, a broken stub)
# rewrote a BLOCK carrying a security finding into ordinary rework and skipped `needs-human`.
#
# This test runs the REAL helper from templates/scripts/bureau-config.sh and the REAL fold
# block cut out of templates/scripts/code-review-pipeline.sh. It does not re-implement either:
# a rewrite that moves the block makes the cut fail loudly instead of leaving an assertion
# that quietly tests nothing.
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

# --- the fold block in the stage -------------------------------------------
# Cut from `if [ "$BUILD_OK" = false ]; then` to the next `fi` at column 1.
BLOCK=$(awk '/^if \[ "\$BUILD_OK" = false \]; then$/{f=1} f{print} f&&/^fi$/{exit}' \
  "$SCRIPTS/code-review-pipeline.sh")
[ -n "$BLOCK" ] || fail "the build-fold block is no longer in code-review-pipeline.sh"
case "$BLOCK" in
  *'apply_build_failure'*) ;;
  *) fail "the fold block no longer runs the verdict through apply_build_failure" ;;
esac

run_fold() {  # $1 = BUILD_OK, $2 = incoming verdict; echoes "<verdict>|<appended text>"
  BUILD_OK="$1" VERDICT="$2" bash -c "
    set -euo pipefail
    $(declare -f apply_build_failure)
    MERGED_REVIEW='BEFORE'
    $BLOCK
    printf '%s|%s' \"\$VERDICT\" \"\${MERGED_REVIEW#BEFORE}\"
  "
}

NOTE=$'\n\nBUILD FAILURE: Must be fixed.'
for pair in "BLOCK:BLOCK" "APPROVE:REQUEST_CHANGES" "REQUEST_CHANGES:REQUEST_CHANGES" "NONSENSE:BLOCK"; do
  input="${pair%%:*}"; want="${pair##*:}"
  out=$(run_fold false "$input")
  [ "${out%%|*}" = "$want" ] || fail "red build turned '$input' into '${out%%|*}', wanted '$want'"
  [ "${out#*|}" = "$NOTE" ] || fail "the build-failure note changed or is missing for '$input'"
done
echo "PASS the real fold block holds the floor on a red build"

for input in BLOCK APPROVE REQUEST_CHANGES; do
  out=$(run_fold true "$input")
  [ "${out%%|*}" = "$input" ] || fail "green build changed '$input' into '${out%%|*}'"
  [ -z "${out#*|}" ] || fail "green build appended a build-failure note to '$input'"
done
echo "PASS a green build leaves verdict and review text untouched"
