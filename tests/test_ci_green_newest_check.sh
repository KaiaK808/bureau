#!/bin/bash
# The real CI helper and merge-stage classifiers, with paginated fake gh replies.
# BUREAU_TEST_CI_CONFIG can supply the preceding helper for a negative control.
set -uo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
T=$(mktemp -d -t bureau-test.ci-newest.XXXXXXXX)
trap 'rm -rf "$T"' EXIT
CI_FIXTURES="$T"
export CI_FIXTURES
mkdir -p "$T/bin"
cat > "$T/bin/gh" <<'GH'
#!/bin/bash
set -uo pipefail
printf '%s\n' "$*" >> "$CI_FIXTURES/calls.log"
case "$*" in
  'repo view '*) [ ! -e "$CI_FIXTURES/no_repo" ] || exit 1; echo test-owner/test-repo ;;
  'pr view '*) [ ! -e "$CI_FIXTURES/no_head" ] || exit 1; echo HEAD_SHA ;;
  *'/check-runs '*) [ ! -e "$CI_FIXTURES/no_checks" ] || exit 1; cat "$CI_FIXTURES/checks.json" ;;
  *'/status '*) [ ! -e "$CI_FIXTURES/no_status" ] || exit 1; cat "$CI_FIXTURES/statuses.json" ;;
  *'/git/commits/'*) echo 2020-01-01T00:00:00Z ;;
  *) echo "unexpected gh call: $*" >&2; exit 1 ;;
esac
GH
chmod +x "$T/bin/gh"
PATH="$T/bin:$PATH"
export PATH

# Load the shipped implementations, including numeric validation and the exact
# merge-pipeline.sh case patterns and comment-key normalization.
source "$ROOT/templates/scripts/bureau-env.sh"
sed -n -e '/^pr_ci_is_green() {/,/^}/p' -e '/^_merge_gate_number() {/,/^}/p' \
  -e '/^_pr_head_commit_age() {/,/^}/p' \
  "${BUREAU_TEST_CI_CONFIG:-$ROOT/templates/scripts/bureau-config.sh}" > "$T/helpers.sh"
sed -n '/^_bureau_gh_owner_repo() {/,/^}/p' "$ROOT/templates/scripts/bureau-config.sh" >> "$T/helpers.sh"
sed -n -e '/^merge_gate_outcome() {/,/^}/p' -e '/^merge_gate_key() {/,/^}/p' \
  "$ROOT/templates/scripts/merge-pipeline.sh" >> "$T/helpers.sh"
source "$T/helpers.sh"
bureau_get() { jq -r "$1" "$T/config.json"; }

pass=0 fail=0
check() {  # <description> <command...> — keep running to show all regressions
  local label="$1"; shift
  if "$@"; then
    echo "PASS $label"; pass=$((pass + 1))
  else
    echo "FAIL $label (rc=${RC:-?}, line=${LINE:-})" >&2; fail=$((fail + 1))
  fi
}
reset_case() {
  echo '{"agents":{}}' > "$T/config.json"
  echo '[]' > "$T/statuses.json"
  : > "$T/calls.log"
  rm -f "$T/no_repo" "$T/no_head" "$T/no_checks" "$T/no_status"
}
minimum() { printf '{"agents":{"merge_min_required_checks":%s}}\n' "$1" > "$T/config.json"; }
runs() { jq -n --argjson runs "$1" '{check_runs: $runs}' > "$T/checks.json"; }
gate() {
  RC=0
  LINE=$(pr_ci_is_green 42 2>&1) || RC=$?
}
line_is() { [ "$RC" = 1 ] && [ "$LINE" = "$1" ]; }
green() { [ "$RC" = 0 ] && [ -z "$LINE" ]; }
outcome_is() { [ "$(printf 'ci_green: %s\n' "$LINE" | merge_gate_outcome)" = "$1" ]; }
no_age_read() { ! grep -q '/git/commits/' "$T/calls.log"; }

for conclusion in skipped neutral; do
  reset_case
  runs "[{\"name\":\"ci\",\"app\":{\"id\":1},\"id\":20,\"started_at\":\"2020-01-01T00:00:00Z\",\"status\":\"completed\",\"conclusion\":\"$conclusion\"}]"
  gate
  check "all $conclusion, past start grace: only 0 completed" line_is 'ci: only 0 completed check(s) on HEAD_SHA (require >= 1)'
  check "all $conclusion: raw runs prevent the no-CI-started line" no_age_read
  check "all $conclusion: merge stage classifies not-yet" outcome_is not-yet
  minimum 0; gate
  check "all $conclusion: explicit minimum 0 permits merge" green
done

# The newer result precedes the older one across pages, as GitHub may return it.
reset_case
cat > "$T/checks.json" <<'JSON'
{"check_runs":[{"name":"ci","app":{"id":1},"id":20,"started_at":"2026-01-02T00:00:00Z","status":"completed","conclusion":"success"}]}
{"check_runs":[{"name":"ci","app":{"id":1},"id":10,"started_at":"2026-01-01T00:00:00Z","status":"completed","conclusion":"failure"}]}
JSON
gate
check 'newest success supersedes failure across pages' green
minimum 2
jq -s 'map(.check_runs) | add | map(.conclusion = "success") | {check_runs: .}' "$T/checks.json" > "$T/changed.json"
mv "$T/changed.json" "$T/checks.json"
gate
check 'successful reruns of one check count once' line_is 'ci: only 1 completed check(s) on HEAD_SHA (require >= 2)'
minimum 1
jq '.check_runs[0].conclusion = "skipped"' "$T/checks.json" > "$T/changed.json"
mv "$T/changed.json" "$T/checks.json"
gate
check 'newest skipped run supersedes an older success' line_is 'ci: only 0 completed check(s) on HEAD_SHA (require >= 1)'

reset_case
runs '[{"name":"ci","id":10,"status":"completed","conclusion":"success"},
       {"name":"docs","id":20,"status":"completed","conclusion":"skipped"},
       {"name":"lint","id":30,"status":"completed","conclusion":"neutral"}]'
gate
check 'one success with skipped and neutral checks meets default minimum' green
minimum 2; gate
check 'skipped and neutral checks add nothing to the count' line_is 'ci: only 1 completed check(s) on HEAD_SHA (require >= 2)'
check 'success count line stays not-yet' outcome_is not-yet
key_one=$(merge_gate_key not-yet "- ci_green: $LINE")
key_zero=$(merge_gate_key not-yet '- ci_green: ci: only 0 completed check(s) on HEAD_SHA (require >= 2)')
check 'merge comment key normalizes the success count' test "$key_one" = "$key_zero"
echo '[{"context":"legacy","state":"success"}]' > "$T/statuses.json"
gate
check 'a successful legacy status adds to the success count' green
minimum 3; gate
check 'count is successful newest runs plus successful statuses' line_is 'ci: only 2 completed check(s) on HEAD_SHA (require >= 3)'

reset_case
runs '[{"name":"ci","app":{"id":1},"id":10,"started_at":"2026-01-01T00:00:00Z","status":"completed","conclusion":"success"},
       {"name":"ci","app":{"id":1},"id":20,"started_at":"2026-01-02T00:00:00Z","status":"in_progress","conclusion":null}]'
gate
check 'newest pending run supersedes older success' line_is 'ci: 1 check(s) still pending on HEAD_SHA'
check 'pending line stays not-yet' outcome_is not-yet
minimum 0; gate
check 'newest pending run still waits at minimum 0' line_is 'ci: 1 check(s) still pending on HEAD_SHA'

reset_case
runs '[{"name":"ci","app":{"id":1},"id":10,"started_at":"2020-01-01T00:00:00Z","status":"queued","conclusion":null},
       {"name":"ci","app":{"id":1},"id":20,"started_at":"2026-01-02T00:00:00Z","status":"completed","conclusion":"success"}]'
gate
check 'superseded queued run past grace leaves no queued line' green

reset_case
runs '[{"name":"ci","app":{"id":1},"id":10,"started_at":"2026-01-01T00:00:00Z","status":"completed","conclusion":"failure"},
       {"name":"ci","app":{"id":2},"id":20,"started_at":"2026-01-02T00:00:00Z","status":"completed","conclusion":"success"}]'
gate
check 'same name from two apps preserves the failing app' line_is 'ci: failing check(s) on HEAD_SHA: ci'
check 'failing line stays blocked' outcome_is blocked
jq '.check_runs[0].conclusion = "success"' "$T/checks.json" > "$T/changed.json"
mv "$T/changed.json" "$T/checks.json"
minimum 2; gate
check 'two successful apps with the same name count separately' green

# Dates take precedence over ids; equal dates use numeric ids, independent of order.
reset_case
runs '[{"name":"ci","id":9,"started_at":"2026-01-02T00:00:00Z","status":"completed","conclusion":"success"},
       {"name":"ci","id":10,"started_at":"2026-01-01T00:00:00Z","status":"completed","conclusion":"failure"}]'
gate
check 'latest readable started_at takes precedence over id' green
jq '.check_runs[].started_at = "2026-01-02T00:00:00Z" | .check_runs |= reverse' "$T/checks.json" > "$T/changed.json"
mv "$T/changed.json" "$T/checks.json"
gate
check 'equal started_at uses highest numeric id' line_is 'ci: failing check(s) on HEAD_SHA: ci'

# Newer failures have lower ids, so losing any timestamp precision lets them pass.
while IFS='|' read -r label newer older; do
  reset_case
  runs "[{\"name\":\"ci\",\"app\":{\"id\":1},\"id\":10,\"started_at\":\"$newer\",\"status\":\"completed\",\"conclusion\":\"failure\"},
         {\"name\":\"ci\",\"app\":{\"id\":1},\"id\":20,\"started_at\":\"$older\",\"status\":\"completed\",\"conclusion\":\"success\"}]"
  gate
  check "$label" line_is 'ci: failing check(s) on HEAD_SHA: ci'
done <<'TIMESTAMPS'
newer fractional failure supersedes older whole-second success|2026-01-02T00:00:00.123Z|2026-01-01T00:00:00Z
fractional second supersedes whole second with opposing ids|2026-01-02T00:00:00.123Z|2026-01-02T00:00:00Z
larger fraction supersedes smaller fraction with opposing ids|2026-01-02T00:00:00.5Z|2026-01-02T00:00:00.123Z
fractional ordering preserves digits beyond floating-point precision|2026-01-02T00:00:00.0000000000000000002Z|2026-01-02T00:00:00.0000000000000000001Z
numeric zero offset preserves fractional ordering|2026-01-02T00:00:00.123+00:00|2026-01-02T00:00:00Z
positive numeric offset orders by UTC time|2026-01-02T00:00:00.123Z|2026-01-02T02:00:00+02:00
negative numeric offset orders by UTC time|2026-01-01T22:00:00.123-02:00|2026-01-02T00:00:00Z
TIMESTAMPS

# Equivalent fractional forms still defer to the highest id.
reset_case
runs '[{"name":"ci","app":{"id":1},"id":10,"started_at":"2026-01-02T00:00:00.123000Z","status":"completed","conclusion":"failure"},
       {"name":"ci","app":{"id":1},"id":20,"started_at":"2026-01-02T00:00:00.123Z","status":"completed","conclusion":"success"}]'
gate
check 'equal fractions with trailing zeros use highest id' green
jq '.check_runs[0].started_at = "2026-01-02T00:00:00.000Z" | .check_runs[1].started_at = "2026-01-02T00:00:00Z"' \
  "$T/checks.json" > "$T/changed.json"
mv "$T/changed.json" "$T/checks.json"
gate
check 'zero fraction ties with whole second and uses highest id' green

reset_case
runs '[{"name":"ci","app":{"id":1},"id":10,"started_at":"2026-01-02T00:00:00.123Z","status":"completed","conclusion":"failure"},
       {"name":"ci","app":{"id":1},"id":20,"started_at":"2026-01-01T00:00:00Z","status":"completed","conclusion":"success"},
       {"name":"ci","app":{"id":1},"id":5,"started_at":"garbage","status":"completed","conclusion":"failure"}]'
gate
check 'one garbage timestamp makes the entire group use ids alone' green

for at in null '"unreadable"'; do
  reset_case
  runs "[{\"name\":\"ci\",\"id\":10,\"started_at\":$at,\"status\":\"completed\",\"conclusion\":\"success\"},
         {\"name\":\"ci\",\"id\":9,\"started_at\":$at,\"status\":\"completed\",\"conclusion\":\"failure\"}]"
  gate
  check "started_at $at: ids alone order reruns" green
  jq '.check_runs[1].started_at = "2026-01-01T00:00:00Z" | .check_runs[0].status = "in_progress" | .check_runs[0].conclusion = null' \
    "$T/checks.json" > "$T/changed.json"
  mv "$T/changed.json" "$T/checks.json"
  gate
  check "started_at $at next to a readable date: newest id remains pending" line_is 'ci: 1 check(s) still pending on HEAD_SHA'
done

# Legacy contexts retain their own pending/failure rules, even when the minimum is 0.
reset_case
runs '[]'
echo '[{"context":"legacy","state":"success"}]' > "$T/statuses.json"
gate
check 'legacy success alone meets the default minimum' green
minimum 0
echo '[{"context":"legacy","state":"pending"}]' > "$T/statuses.json"
gate
check 'legacy pending still waits at minimum 0' line_is 'ci: 1 check(s) still pending on HEAD_SHA'
for state in failure error; do
  printf '[{"context":"legacy","state":"%s"}]\n' "$state" > "$T/statuses.json"
  gate
  check "legacy $state still fails at minimum 0" line_is 'ci: failing check(s) on HEAD_SHA: legacy'
done

# Classify the remaining diagnostic families directly from the real helper.
reset_case
runs '[]'; gate
check 'empty raw lists past grace keep the no-CI-started line' line_is 'ci: no check run and no status on HEAD_SHA, and its commit is older than the CI start grace (agents.merge_ci_start_grace_seconds: 1800) — no CI started for this head'
check 'no-CI-started stays blocked' outcome_is blocked
touch "$T/no_status"; gate
check 'unreadable statuses cannot claim no CI started' line_is 'ci: only 0 completed check(s) on HEAD_SHA (require >= 1)'
reset_case
runs '[{"name":"ci","id":10,"started_at":"2020-01-01T00:00:00Z","status":"queued","conclusion":null}]'
gate
check 'newest queued run past grace keeps its diagnostic' grep -Eq '^ci: check ci queued for [0-9]+ s on HEAD_SHA, past the CI queue grace ' <<< "$LINE"
check 'queued-past-grace stays blocked' outcome_is blocked
key_old=$(merge_gate_key blocked "- ci_green: $LINE")
key_later=$(merge_gate_key blocked "- ci_green: $(printf '%s' "$LINE" | sed -E 's/queued for [0-9]+ s/queued for 999999999 s/')")
check 'merge comment key still normalizes queue age' test "$key_old" = "$key_later"
touch "$T/no_checks"; gate
check 'check-runs read failure keeps its diagnostic' line_is 'ci: gh check-runs query failed for HEAD_SHA'
check 'check-runs read failure stays not-yet' outcome_is not-yet
reset_case
touch "$T/no_repo"; gate
check 'owner/repo read failure keeps its diagnostic' line_is 'ci: cannot resolve owner/repo'
check 'owner/repo read failure stays not-yet' outcome_is not-yet
reset_case
touch "$T/no_head"; gate
check 'head read failure keeps its diagnostic' line_is 'ci: cannot resolve head SHA for #42'
check 'head read failure stays not-yet' outcome_is not-yet

printf 'Results: %s passed, %s failed\n' "$pass" "$fail"
[ "$fail" = 0 ]
