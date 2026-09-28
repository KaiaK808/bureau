#!/bin/bash
# The merge message Bureau writes carries no CI suppressor, and changes nothing else.
#
# Runs the REAL sanitize_ci_markers and build_merge_body from templates/scripts/merge-body.sh
# against every suppressor GitHub honours, in several spellings. The wiring into
# merge-pipeline.sh is covered by tests/test_merge_pipeline_correctness.sh.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")" && cd .. && pwd)"
# shellcheck source=templates/scripts/merge-body.sh
source "$REPO_ROOT/templates/scripts/merge-body.sh"

fail() { echo "FAIL $*" >&2; exit 1; }

# Any form GitHub reads as "do not run CI", case-insensitive.
has_marker() {
  printf '%s' "$1" | grep -qiE '\[(skip ci|ci skip|no ci|skip actions|actions skip)\]|skip-checks[[:space:]]*:|\*\*\*no_ci\*\*\*'
}

# The negative control for has_marker itself: it must see every form, or G1 below proves nothing.
for m in '[skip ci]' '[ci skip]' '[no ci]' '[skip actions]' '[actions skip]' 'skip-checks: true' \
         'skip-checks:true' '[SKIP CI]' '[Ci Skip]' 'Skip-Checks: true' '***NO_CI***'; do
  has_marker "text $m text" || fail "the test's own detector misses '$m'"
  out=$(sanitize_ci_markers "before $m after")
  has_marker "$out" && fail "G1: '$m' survived as '$out'"
  case "$out" in "before "*" after") ;; *) fail "G4: text around '$m' changed: '$out'" ;; esac
done
echo "PASS G1/G4 every suppressor GitHub honours is defanged, in any case, and only the token changes"

multi=$'Fix the thing [skip ci]\n\nBody line\nskip-checks: true\n[CI SKIP] at the end\n'
once=$(sanitize_ci_markers "$multi")
has_marker "$once" && fail "G1: a marker survived in a multi-line message"
twice=$(sanitize_ci_markers "$once")
[ "$once" = "$twice" ] || fail "G2: sanitising twice changed the text"
echo "PASS G2 sanitising is idempotent, over several lines"

clean=$'Subject line\n\nA body with [brackets], skip-checking prose and ***stars***\n\n'
# Captured with a sentinel so trailing newlines are compared too.
got=$(sanitize_ci_markers "$clean"; printf x); got=${got%x}
[ "$got" = "$clean" ] || fail "G3: text without a marker changed (argument path)"
got=$(printf '%s' "$clean" | sanitize_ci_markers; printf x); got=${got%x}
[ "$got" = "$clean" ] || fail "G3: text without a marker changed (stdin path)"
echo "PASS G3 text without a marker comes back byte-identical, trailing newlines included"

[ "$(build_merge_body 'EXP-1: title [skip ci]' $'\n  \t\n')" = "EXP-1: title (skip ci)" ] \
  || fail "G5/G7: a whitespace-only body did not fall back to the sanitised title"
[ "$(build_merge_body 'EXP-1: title' 'Summary [no ci]')" = "Summary (no ci)" ] \
  || fail "the PR body was not used, or not sanitised"
echo "PASS G5/G7 an empty body falls back to the sanitised title; a real body is used and sanitised"
