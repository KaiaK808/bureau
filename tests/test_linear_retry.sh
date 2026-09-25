#!/bin/bash
# A Linear answer that cannot be used is retried, and if it stays unusable the stage stops
# with 27 (linear-unusable) instead of deciding on an empty result.
#
# Runs the REAL helpers from templates/scripts/bureau-config.sh under /bin/bash against a
# stubbed `curl` that plays a queue of answers and a stubbed `sleep` that only records.
# Covers: the fault class of every broken answer form, the retry ladder and its settings,
# the halt-path single attempt, that no answer text or key reaches a message, and that
# every Linear caller hands 27 on — mutators without sending anything after the failed read.
# The negative control puts the old unchecked fetch back and shows the silent empty success
# this replaces. Stage wiring: tests/test_linear_stages.sh; shepherd: tests/test_shepherd.sh.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")" && cd .. && pwd)"
SCRIPTS="$REPO_ROOT/templates/scripts"
SB=$(mktemp -d -t bureau-test.linear.XXXXXXXX)
trap 'rm -rf "$SB"' EXIT

fail() { echo "FAIL $*" >&2; printf '  | stdout: %s\n  | stderr: %s\n' "${OUT:-}" "${ERR:-}" >&2; exit 1; }

write_config() {  # $1 = extra jq filter applied to the base config
  jq "${1:-.}" > "$SB/.bureau.json" <<'EOF'
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
}
write_config

# --- stubs ------------------------------------------------------------------------
mkdir -p "$SB/bin" "$SB/forms"
HEALTHY='{"data":{"viewer":{"id":"V1"},"issues":{"nodes":[{"id":"UUID-1","identifier":"EXP-1","title":"T","description":"D","branchName":"feat/x","state":{"id":"s5","name":"Build"},"labels":{"nodes":[]},"comments":{"nodes":[]},"inverseRelations":{"nodes":[]},"priority":1,"createdAt":"2026-09-01T00:00:00Z"}]},"issueLabels":{"nodes":[{"id":"L1"}]},"commentCreate":{"success":true},"issueUpdate":{"success":true},"issueAddLabel":{"success":true},"issueRemoveLabel":{"success":true}}}'
printf '%s' "$HEALTHY"                                                  > "$SB/forms/healthy"
printf '%s' '{"data":{"issues":{"nodes":[]}}}'                          > "$SB/forms/nothing"
printf '%s' '{"errors":[{"message":"CANARY-ANSWER boom"}],"data":null}' > "$SB/forms/errors"
printf '%s' '<html>CANARY-ANSWER 502 Bad Gateway</html>'               > "$SB/forms/html"
printf '%s' ''                                                          > "$SB/forms/empty"
printf '%s' '   '                                                       > "$SB/forms/blank"
printf '%s' '{"data":null,"note":"CANARY-ANSWER"}'                      > "$SB/forms/datanull"
printf '%s' '["CANARY-ANSWER"]'                                         > "$SB/forms/array"
printf '%s' '{"data":{}} {"data":{}}'                                   > "$SB/forms/twojson"

# curl: plays $SB/queue, one form per call; the last line repeats. Logs each payload.
cat > "$SB/bin/curl" <<EOF
#!/bin/bash
q="$SB/queue"
form=\$(head -1 "\$q")
if [ "\$(wc -l < "\$q")" -gt 1 ]; then tail -n +2 "\$q" > "\$q.tmp" && mv "\$q.tmp" "\$q"; fi
prev=""; payload=""
for a in "\$@"; do [ "\$prev" = "-d" ] && payload="\$a"; prev="\$a"; done
printf '%s\n' "\$payload" | tr '\n' ' ' >> "$SB/curl.log"; echo >> "$SB/curl.log"
[ "\$form" = connfail ] && exit 7
cat "$SB/forms/\$form"
EOF
printf '#!/bin/bash\nprintf "%%s\\n" "$1" >> "%s/sleeps.log"\n' "$SB" > "$SB/bin/sleep"
chmod +x "$SB/bin/curl" "$SB/bin/sleep"

queue() { printf '%s\n' "$@" > "$SB/queue"; }

# helper <snippet>: runs the snippet with the real config sourced, under /bin/bash and
# `set -uo pipefail`; sets OUT, ERR, RC, CALLS (curl calls) and SLEEPS (waits, space-separated).
helper() {
  rm -f "$SB/curl.log" "$SB/sleeps.log" "$SB/fault"
  set +e
  OUT=$(cd "$SB" && PATH="$SB/bin:$PATH" LINEAR_API_KEY="lin_api_KEYCANARY" \
    _BUREAU_LINEAR_FAULT_FILE="$SB/fault" /bin/bash -c "
      set -uo pipefail
      source '$SCRIPTS/bureau-config.sh'
      $1" 2>"$SB/err")
  RC=$?
  set -e
  ERR=$(cat "$SB/err")
  CALLS=0; [ -f "$SB/curl.log" ] && CALLS=$(wc -l < "$SB/curl.log" | tr -d ' ')
  SLEEPS=""; [ -f "$SB/sleeps.log" ] && SLEEPS=$(tr '\n' ' ' < "$SB/sleeps.log" | sed 's/ $//')
  case "$OUT$ERR" in *CANARY-ANSWER*|*KEYCANARY*) fail "answer text or the key reached a message" ;; esac
}
expect() {  # rc calls sleeps label
  [ "$RC" = "$1" ] || fail "$4: exit $RC, wanted $1"
  [ "$CALLS" = "$2" ] || fail "$4: $CALLS fetches, wanted $2"
  [ "$SLEEPS" = "$3" ] || fail "$4: waits '$SLEEPS', wanted '$3'"
}
FETCH='linear_query "{ viewer { id } }"'

# --- classification: every broken form gets its own name ------------------------------
for pair in connfail:no-response empty:no-response blank:no-response html:not-json twojson:not-json \
            errors:graphql-errors datanull:no-data array:no-data; do
  queue "${pair%%:*}"
  BUREAU_LINEAR_RETRIES=0 helper "$FETCH"
  expect 27 1 "" "form ${pair%%:*}"
  [ -z "$OUT" ] || fail "form ${pair%%:*}: printed an answer"
  case "$ERR" in *"unusable answer (${pair##*:}) after 1 attempt(s)"*) ;; *) fail "form ${pair%%:*} not classified as ${pair##*:}" ;; esac
  [ "$(cat "$SB/fault" 2>/dev/null)" = "${pair##*:}" ] || fail "form ${pair%%:*}: fault file does not name ${pair##*:}"
done
echo "PASS every broken answer form is classified by name, printed nowhere, and recorded"

# --- the ladder ------------------------------------------------------------------------
queue healthy
helper "$FETCH"
expect 0 1 "" "healthy"
[ "$OUT" = "$HEALTHY" ] && [ -z "$ERR" ] || fail "a healthy answer was changed or produced a message"
queue html healthy
helper "$FETCH"
expect 0 2 "10" "one outage"
[ "$OUT" = "$HEALTHY" ] || fail "one outage: answer after the retry differs"
queue errors empty healthy
helper "$FETCH"
expect 0 3 "10 30" "two outages"
queue errors
helper "$FETCH"
expect 27 4 "10 30 60" "persistent"
queue nothing
helper '_resolve_issue_uuid EXP-9'
expect 0 1 "" "usable but empty"
[ -z "$OUT" ] || fail "an empty result is not empty"
echo "PASS healthy is untouched, outages are bridged 10/30/60, a persistent fault stops with 27, an empty result is not retried"

# --- settings --------------------------------------------------------------------------
queue errors
BUREAU_LINEAR_RETRIES=0 helper "$FETCH";            expect 27 1 "" "RETRIES=0"
BUREAU_LINEAR_RETRIES=2 helper "$FETCH";            expect 27 3 "10 30" "RETRIES=2"
BUREAU_LINEAR_RETRIES=5 helper "$FETCH";            expect 27 6 "10 30 60 60 60" "RETRIES=5"
BUREAU_LINEAR_RETRIES=010 helper "$FETCH";          expect 27 11 "10 30 60 60 60 60 60 60 60 60" "RETRIES=010 (not octal)"
case "$ERR" in *"attempt 1 of 11 — retrying in 10s"*) ;; *) fail "RETRIES=010: the retry line does not count 11 attempts (bash reads 010 as octal 8)" ;; esac
BUREAU_LINEAR_RETRY_WAIT_1=5 BUREAU_LINEAR_RETRY_WAIT_2=7 helper "$FETCH"; expect 27 4 "5 7 60" "WAIT_1/WAIT_2"
BUREAU_LINEAR_RETRY_WAIT_1=0 BUREAU_LINEAR_RETRY_WAIT_2=0 BUREAU_LINEAR_RETRY_WAIT_3=0 helper "$FETCH"; expect 27 4 "0 0 0" "waits of 0"
BUREAU_LINEAR_RETRIES="" helper "$FETCH";           expect 27 4 "10 30 60" "empty value"
[ -z "$(printf '%s' "$ERR" | grep warning)" ] || fail "an empty value warned"
BUREAU_LINEAR_RETRIES="x[\$(touch $SB/pwned)]" helper "$FETCH"
expect 27 4 "10 30 60" "hostile value"
[ ! -e "$SB/pwned" ] || fail "a retry setting was executed"
case "$ERR" in *"warning: BUREAU_LINEAR_RETRIES ignored"*) ;; *) fail "the hostile value did not warn by key" ;; esac
case "$ERR" in *touch*|*pwned*) fail "the warning repeats the value" ;; esac
write_config '.linear.retry = {retries: 1, wait_1: 4}'
helper "$FETCH";                                    expect 27 2 "4" ".bureau.json retry"
write_config '.linear.retry = {retries: "0\u0000"}'
helper "$FETCH";                                    expect 27 4 "10 30 60" "NUL in .bureau.json"
case "$ERR" in *"warning: .linear.retry.retries ignored"*) ;; *) fail "a value with a NUL byte did not warn" ;; esac
write_config
echo "PASS retries and waits are set from env or .bureau.json, 0 is valid, 010 is ten, anything else warns by name and never runs"

_BUREAU_LINEAR_SINGLE_ATTEMPT=1 helper "$FETCH"
expect 27 1 "" "halt path"
echo "PASS a halt path makes one attempt and waits for nothing"

# --- every caller hands 27 on ----------------------------------------------------------
queue errors
for call in '_resolve_issue_uuid EXP-1' 'get_issue_state EXP-1' 'get_issue_detail EXP-1' \
            'get_issue_comments EXP-1' 'get_issue_branch_and_comments EXP-1' 'get_issue_branch EXP-1' \
            'bureau_issue_snapshot EXP-1' 'count_in_flight_issues' 'pick_issue s5 ""' \
            'post_comment EXP-1 hello' 'move_issue EXP-1 s6' \
            'add_issue_label EXP-1 needs-human' 'remove_issue_label EXP-1 needs-human'; do
  BUREAU_LINEAR_RETRIES=0 helper "$call"
  [ "$RC" = 27 ] || fail "'$call' returned $RC, wanted 27"
  [ "$CALLS" = 1 ] || fail "'$call' fetched $CALLS times after the first read failed"
  case "$(cat "$SB/curl.log")" in *mutation*) fail "'$call' sent a mutation after a failed read" ;; esac
done
echo "PASS every Linear caller hands 27 on, and no mutator sends anything after its read failed"

queue healthy
helper 'move_issue EXP-1 s6 && add_issue_label EXP-1 needs-human && post_comment EXP-1 hi && get_issue_state EXP-1'
[ "$RC" = 0 ] && [ "$OUT" = "Build" ] || fail "the healthy path through the callers changed"
echo "PASS the healthy path through the callers is unchanged"

# --- negative control: the fetch this replaces -------------------------------------------
OLD_FETCH='
linear_query() { curl -s -X POST https://api.linear.app/graphql -H "Content-Type: application/json" -H "Authorization: $LINEAR_API_KEY" -d "{\"query\": \"$1\"}"; }
linear_raw()   { curl -s -X POST https://api.linear.app/graphql -H "Content-Type: application/json" -H "Authorization: $LINEAR_API_KEY" -d "$1"; }'
queue errors
helper "$OLD_FETCH
get_issue_state EXP-1"
[ "$RC" = 0 ] && [ -z "$OUT" ] || fail "negative control: the old fetch no longer reads a broken answer as an empty success, so this test proves nothing"
echo "PASS negative control: the old fetch turns a broken answer into an empty success (exit 0)"
