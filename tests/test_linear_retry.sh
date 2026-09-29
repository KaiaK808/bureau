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
# EXP-1482: an HTTP error status, `{"data":{}}` and null root fields, NUL bytes, the
# per-request time limit and its settings, and the shapes the issue readers
# depend on — with a negative control against the transport this replaces, and the same
# checks with the REAL curl against a local server (status line, NUL byte, time limit).
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
HEALTHY='{"data":{"viewer":{"id":"V1"},"issues":{"nodes":[{"id":"UUID-1","identifier":"EXP-1","title":"T","description":"D","branchName":"feat/x","state":{"id":"s5","name":"Build"},"labels":{"nodes":[]},"comments":{"nodes":[]},"inverseRelations":{"nodes":[]},"priority":1,"createdAt":"2026-09-01T00:00:00Z"}]},"issueLabels":{"nodes":[{"id":"L1","team":{"key":"EXP"}}]},"commentCreate":{"success":true},"issueUpdate":{"success":true},"issueAddLabel":{"success":true},"issueRemoveLabel":{"success":true}}}'
printf '%s' "$HEALTHY"                                                  > "$SB/forms/healthy"
printf '%s' '{"data":{"issues":{"nodes":[]}}}'                          > "$SB/forms/nothing"
printf '%s' '{"errors":[{"message":"CANARY-ANSWER boom"}],"data":null}' > "$SB/forms/errors"
printf '%s' '<html>CANARY-ANSWER 502 Bad Gateway</html>'               > "$SB/forms/html"
printf '%s' ''                                                          > "$SB/forms/empty"
printf '%s' '   '                                                       > "$SB/forms/blank"
printf '%s' '{"data":null,"note":"CANARY-ANSWER"}'                      > "$SB/forms/datanull"
printf '%s' '["CANARY-ANSWER"]'                                         > "$SB/forms/array"
printf '%s' '{"data":{}} {"data":{}}'                                   > "$SB/forms/twojson"

# EXP-1482: an HTTP error page with a JSON body, a missing root field, a NUL byte in the
# body, a node without the list a reader depends on, and a hanging request.
printf '%s' '{"data":{}}'                                               > "$SB/forms/dataempty"
printf '%s' '{"data":{"issues":null}}'                                  > "$SB/forms/rootnull"
printf '%s' '{"data":{"issue":null}}'                                   > "$SB/forms/issuenull"
printf '{"data":{"viewer":{"id":"V1"}}}\000'                           > "$SB/forms/nul"
printf '{"data":{"viewer":{"id":"V\000CANARY-ANSWER"}}}'               > "$SB/forms/nulinside"
NODE='"id":"UUID-1","identifier":"EXP-1","title":"T","description":"D","project":null,"branchName":"","state":{"id":"s5","name":"Build"}'
printf '{"data":{"issues":{"nodes":[{%s,"labels":{"nodes":[]},"comments":{"nodes":[]}}]}}}' "$NODE" > "$SB/forms/bare"
printf '{"data":{"issues":{"nodes":[{%s,"labels":null,"comments":{"nodes":[]}}]}}}'         "$NODE" > "$SB/forms/labelsnull"
printf '{"data":{"issues":{"nodes":[{%s,"labels":{"nodes":[]}}]}}}'                          "$NODE" > "$SB/forms/nocomments"
printf '{"data":{"issues":{"nodes":[{"id":"UUID-1","identifier":"EXP-1","labels":{"nodes":[]},"comments":{"nodes":[]}}]}}}' > "$SB/forms/nostate"

# curl: plays $SB/queue, one entry per call; the last line repeats. An entry is a form name,
# optionally with the HTTP status after '@' (default 200; '@none' prints no status line).
# 'connfail' fails the connection (7), 'hang' is a request cut off by --max-time (28),
# 'partial' prints a healthy body and then fails (18). Logs
# each payload and the time limits curl was given. The status line comes from
# tests/lib/curl-writeout.sh, as real curl prints it for `-w`.
cat > "$SB/bin/curl" <<EOF
#!/bin/bash
q="$SB/queue"
entry=\$(head -1 "\$q")
if [ "\$(wc -l < "\$q")" -gt 1 ]; then tail -n +2 "\$q" > "\$q.tmp" && mv "\$q.tmp" "\$q"; fi
form="\${entry%@*}"; status=200
case "\$entry" in *@*) status="\${entry##*@}" ;; esac
prev=""; payload=""; mt="-"; ct="-"
for a in "\$@"; do
  case "\$prev" in -d) payload="\$a" ;; --max-time) mt="\$a" ;; --connect-timeout) ct="\$a" ;; esac
  prev="\$a"
done
printf '%s\n' "\$payload" | tr '\n' ' ' >> "$SB/curl.log"; echo >> "$SB/curl.log"
printf '%s %s\n' "\$mt" "\$ct" >> "$SB/limits.log"
[ "\$form" = connfail ] && exit 7
if [ "\$form" = hang ]; then "$REPO_ROOT/tests/lib/curl-writeout.sh" 000 "\$@"; exit 28; fi
# 'partial': a healthy-looking body, then curl fails (18: transfer cut short).
if [ "\$form" = partial ]; then cat "$SB/forms/healthy"; "$REPO_ROOT/tests/lib/curl-writeout.sh" 200 "\$@"; exit 18; fi
cat "$SB/forms/\$form"
[ "\$status" = none ] || "$REPO_ROOT/tests/lib/curl-writeout.sh" "\$status" "\$@"
EOF
printf '#!/bin/bash\nprintf "%%s\\n" "$1" >> "%s/sleeps.log"\n' "$SB" > "$SB/bin/sleep"
chmod +x "$SB/bin/curl" "$SB/bin/sleep"

queue() { printf '%s\n' "$@" > "$SB/queue"; }

# helper <snippet>: runs the snippet with the real config sourced, under /bin/bash and
# `set -uo pipefail`; sets OUT, ERR, RC, CALLS (curl calls) and SLEEPS (waits, space-separated).
helper() {
  rm -f "$SB/curl.log" "$SB/sleeps.log" "$SB/fault" "$SB/limits.log"
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
linear_raw()   { curl -s -X POST https://api.linear.app/graphql -H "Content-Type: application/json" -H "Authorization: $LINEAR_API_KEY" -d "$1"; }
linear_issue_query() { linear_query "$1"; }'
queue errors
helper "$OLD_FETCH
get_issue_state EXP-1"
[ "$RC" = 0 ] && [ -z "$OUT" ] || fail "negative control: the old fetch no longer reads a broken answer as an empty success, so this test proves nothing"
echo "PASS negative control: the old fetch turns a broken answer into an empty success (exit 0)"

# --- EXP-1482: status, time limit, NUL bytes, missing nodes ------------------------------
for pair in dataempty:no-data rootnull:no-data issuenull:no-data nul:not-json nulinside:not-json \
            healthy@500:no-response healthy@404:no-response \
            errors@400:graphql-errors html@502:not-json hang:no-response; do
  queue "${pair%%:*}"
  BUREAU_LINEAR_RETRIES=0 helper "$FETCH"
  expect 27 1 "" "form ${pair%%:*}"
  [ -z "$OUT" ] || fail "form ${pair%%:*}: printed an answer"
  case "$ERR" in *"unusable answer (${pair##*:}) after 1 attempt(s)"*) ;; *) fail "form ${pair%%:*} not classified as ${pair##*:}" ;; esac
done
queue hang
BUREAU_LINEAR_RETRIES=0 helper "$FETCH"
case "$ERR" in *"no answer within 30s (curl timed out)"*) ;; *) fail "a timed-out request is not named as one" ;; esac
queue healthy@201
helper "$FETCH"
expect 0 1 "" "HTTP 201"
[ "$OUT" = "$HEALTHY" ] || fail "a 2xx answer was changed"
queue healthy@none
helper "$FETCH"
expect 0 1 "" "no status line (a curl double)"
[ "$OUT" = "$HEALTHY" ] || fail "an answer without a status line was changed"
echo "PASS an HTTP error status, a missing or null root field, a NUL byte and a timeout are unusable; 2xx passes; a curl double without a status line is judged by its body"

queue healthy
helper "$FETCH";                                                         [ "$(cat "$SB/limits.log")" = "30 10" ] || fail "default limits: $(cat "$SB/limits.log")"
BUREAU_LINEAR_MAX_TIME=5 BUREAU_LINEAR_CONNECT_TIMEOUT=2 helper "$FETCH"; [ "$(cat "$SB/limits.log")" = "5 2" ] || fail "env limits: $(cat "$SB/limits.log")"
write_config '.linear.request = {max_time: 7, connect_timeout: 3}'
helper "$FETCH";                                                         [ "$(cat "$SB/limits.log")" = "7 3" ] || fail ".bureau.json limits: $(cat "$SB/limits.log")"
write_config
BUREAU_LINEAR_MAX_TIME=0 BUREAU_LINEAR_CONNECT_TIMEOUT=0 helper "$FETCH"
[ "$RC" = 0 ] && [ "$(cat "$SB/limits.log")" = "30 10" ] || fail "a limit of 0 was passed on: $(cat "$SB/limits.log")"
case "$ERR" in *"warning: BUREAU_LINEAR_MAX_TIME ignored: not a whole number of 1 to 300"*"warning: BUREAU_LINEAR_CONNECT_TIMEOUT ignored: not a whole number of 1 to 60"*) ;; *) fail "a limit of 0 did not warn by key with its range" ;; esac
# An invalid env value falls back to .bureau.json first, then to the default.
write_config '.linear.request = {max_time: 60, connect_timeout: 5}'
BUREAU_LINEAR_MAX_TIME=0 BUREAU_LINEAR_CONNECT_TIMEOUT=junk helper "$FETCH"
[ "$(cat "$SB/limits.log")" = "60 5" ] || fail "an invalid env limit skipped .bureau.json: $(cat "$SB/limits.log")"
write_config '.linear.request = {max_time: 0}'
helper "$FETCH"
[ "$(cat "$SB/limits.log")" = "30 10" ] || fail "a limit of 0 in .bureau.json was passed on: $(cat "$SB/limits.log")"
case "$ERR" in *"warning: .linear.request.max_time ignored: not a whole number of 1 to 300"*) ;; *) fail "a limit of 0 in .bureau.json did not warn by path" ;; esac
write_config
BUREAU_LINEAR_MAX_TIME=301 BUREAU_LINEAR_CONNECT_TIMEOUT="x[\$(touch $SB/pwned)]" helper "$FETCH"
[ "$(cat "$SB/limits.log")" = "30 10" ] || fail "an invalid limit was passed on"
[ ! -e "$SB/pwned" ] || fail "a limit setting was executed"
case "$ERR" in *"warning: BUREAU_LINEAR_MAX_TIME ignored: not a whole number of 1 to 300"*"warning: BUREAU_LINEAR_CONNECT_TIMEOUT ignored: not a whole number of 1 to 60"*) ;; *) fail "invalid limits did not warn by key with their range" ;; esac
case "$ERR" in *touch*|*pwned*) fail "the warning repeats the value" ;; esac
queue hang
helper "$FETCH"
expect 27 4 "10 30 60" "persistent timeout"
[ "$(sort -u "$SB/limits.log")" = "30 10" ] || fail "a retry ran without the time limit"
echo "PASS every request carries --max-time/--connect-timeout (default 30/10, env, .bureau.json); 0 and junk warn by key and fall back to .bureau.json, then the default; a hanging Linear gives up after 4 x 30 s + 100 s"

queue bare
helper 'get_issue_detail EXP-1 | jq -c "[.identifier, .labels]"'
[ "$RC" = 0 ] && [ "$OUT" = '["EXP-1",[]]' ] || fail "a ticket without labels is no longer 'no labels'"
helper 'printf "[%s]" "$(get_issue_branch EXP-1)"'
[ "$RC" = 0 ] && [ "$OUT" = "[]" ] || fail "an absent branch marker is no longer an empty branch with exit 0"
helper 'get_issue_state EXP-1'
[ "$RC" = 0 ] && [ "$OUT" = "Build" ] || fail "the state of a bare ticket changed"
queue nothing
helper 'printf "[%s]" "$(get_issue_state EXP-9)"; get_issue_detail EXP-9 | jq -c .identifier'
[ "$RC" = 0 ] && [ "$OUT" = "[]null" ] || fail "a ticket that does not exist is no longer a usable empty answer: $OUT"
for pair in 'labelsnull:get_issue_detail EXP-1' 'nostate:get_issue_state EXP-1' 'nostate:bureau_issue_snapshot EXP-1' \
            'labelsnull:bureau_issue_snapshot EXP-1' 'nocomments:get_issue_branch EXP-1' \
            'nocomments:get_issue_branch_and_comments EXP-1' 'nocomments:get_issue_comments EXP-1' \
            'dataempty:get_issue_state EXP-1' 'dataempty:get_issue_detail EXP-1'; do
  queue "${pair%%:*}"
  BUREAU_LINEAR_RETRIES=1 helper "${pair#*:}"
  [ "$RC" = 27 ] && [ "$CALLS" = 2 ] && [ -z "$OUT" ] || fail "'${pair#*:}' on ${pair%%:*}: exit $RC after $CALLS fetches, output '$OUT'"
done
echo "PASS a reader whose list is missing retries and gives up with 27; no labels, no branch marker and no such ticket stay usable answers"

# curl's own exit code decides even for a caller without pipefail (tr sits in the pipe).
queue partial
BUREAU_LINEAR_RETRIES=0 helper "set +o pipefail; $FETCH"
expect 27 1 "" "curl failed after a healthy-looking body, caller without pipefail"
case "$ERR" in *"(no-response)"*) ;; *) fail "a failed transfer was not no-response" ;; esac
# A NUL byte stays unusable with a jq that lets control bytes through (jq 1.6 and older are
# lenient; 1.7 rejects them on its own, which is why this shim is needed to reach the check).
mkdir -p "$SB/lenient"
cat > "$SB/lenient/jq" <<EOF
#!/bin/bash
for a in "\$@"; do [ "\$a" = -n ] || [ -f "\$a" ] && exec "$(command -v jq)" "\$@"; done
LC_ALL=C tr -d '\000\001' | exec "$(command -v jq)" "\$@"
EOF
chmod +x "$SB/lenient/jq"
for form in nul nulinside; do
  queue "$form"
  BUREAU_LINEAR_RETRIES=0 helper "PATH='$SB/lenient':\$PATH; $FETCH"
  expect 27 1 "" "form $form with a lenient jq"
done
queue healthy
BUREAU_LINEAR_RETRIES=0 helper "PATH='$SB/lenient':\$PATH; $FETCH"
expect 0 1 "" "healthy with a lenient jq"
# The two new keys come through .env like the retry settings.
printf 'BUREAU_LINEAR_MAX_TIME=5\nBUREAU_LINEAR_CONNECT_TIMEOUT=2\n' > "$SB/.env"
queue healthy
helper "bureau_load_env '$SB/.env'; $FETCH"
[ "$RC" = 0 ] && [ "$(cat "$SB/limits.log")" = "5 2" ] || fail ".env limits did not reach curl: $(cat "$SB/limits.log" 2>/dev/null)"
rm -f "$SB/.env"
echo "PASS a failed transfer is unusable without pipefail, a NUL byte is unusable even with a lenient jq, and the limits load from .env"

# --- positive control: every caller works on answers with only the fields it asked for --------
# The fixtures above carry every field on every node, so a shape stricter than its query (the
# state read demanding comments, say) would pass them and fail against real Linear. Here each
# query gets exactly the fields it asks for, on a ticket without labels and without comments,
# as Linear would send them. An unknown query gets '{}' (no-data), so a new query shows up here.
mkdir -p "$SB/exactbin"
cat > "$SB/exactbin/curl" <<EOF
#!/bin/bash
prev=""; p=""
for a in "\$@"; do [ "\$prev" = -d ] && p="\$a"; prev="\$a"; done
case "\$p" in
  *'nodes { branchName comments'*) b='{"data":{"issues":{"nodes":[{"branchName":"exp-1-x","comments":{"nodes":[]}}]}}}' ;;
  *'nodes { comments'*)            b='{"data":{"issues":{"nodes":[{"comments":{"nodes":[]}}]}}}' ;;
  *'nodes { identifier title description project'*)
                                   b='{"data":{"issues":{"nodes":[{"identifier":"EXP-1","title":"T","description":null,"project":null,"labels":{"nodes":[]}}]}}}' ;;
  *'nodes { id identifier title description state'*)
                                   b='{"data":{"issues":{"nodes":[{"id":"U","identifier":"EXP-1","title":"T","description":null,"state":{"id":"s5","name":"Build"},"labels":{"nodes":[]}}]}}}' ;;
  # Before the id lookup below: the count's query ends in children(first: 1) { nodes { id } }.
  *'children(first: 1)'*)          b='{"data":{"issues":{"nodes":[{"labels":{"nodes":[]},"children":{"nodes":[]}}]}}}' ;;
  *'nodes { id } }'*)              b='{"data":{"issues":{"nodes":[{"id":"U"}]}}}' ;;
  *issueLabels*)                   b='{"data":{"issueLabels":{"nodes":[{"id":"L","team":null}]}}}' ;;
  *issueUpdate*)                   b='{"data":{"issueUpdate":{"success":true}}}' ;;
  *commentCreate*)                 b='{"data":{"commentCreate":{"success":true}}}' ;;
  *issueAddLabel*)                 b='{"data":{"issueAddLabel":{"success":true}}}' ;;
  *issueRemoveLabel*)              b='{"data":{"issueRemoveLabel":{"success":true}}}' ;;
  *viewer*)                        b='{"data":{"viewer":{"id":"V"}}}' ;;
  *inverseRelations*)              b='{"data":{"issues":{"nodes":[{"identifier":"EXP-1","priority":0,"createdAt":"2026-01-01","labels":{"nodes":[]},"inverseRelations":{"nodes":[]}}]}}}' ;;
  *)                               echo "UNMATCHED QUERY" >&2; b='{}' ;;
esac
printf '%s' "\$b"
"$REPO_ROOT/tests/lib/curl-writeout.sh" 200 "\$@"
EOF
chmod +x "$SB/exactbin/curl"
for call in '_resolve_issue_uuid EXP-1' 'get_issue_state EXP-1' 'get_issue_detail EXP-1' \
            'get_issue_comments EXP-1' 'get_issue_branch_and_comments EXP-1' 'get_issue_branch EXP-1' \
            'bureau_issue_snapshot EXP-1' 'count_in_flight_issues' 'pick_issue s5 ""' \
            'post_comment EXP-1 hello' 'move_issue EXP-1 s6' \
            'add_issue_label EXP-1 needs-human' 'remove_issue_label EXP-1 needs-human'; do
  BUREAU_LINEAR_RETRIES=0 helper "PATH='$SB/exactbin':\$PATH; $call >/dev/null"
  [ "$RC" = 0 ] || fail "'$call' failed ($RC) on an answer with exactly the fields it asked for: $ERR"
  case "$ERR" in *UNMATCHED*|*unusable*) fail "'$call' saw an unmatched or unusable answer: $ERR" ;; esac
done
echo "PASS every Linear caller returns 0 on answers carrying only the fields its query asks for"

# --- negative control for EXP-1482: the transport this replaces ----------------------------
OLD_TRANSPORT='
_bureau_linear_fetch() {
  local answer code=0 finding
  answer=$(curl -s -X POST https://api.linear.app/graphql -H "Content-Type: application/json" -H "Authorization: $LINEAR_API_KEY" -d "$1") || code=$?
  [ "$code" = 0 ] || return 27
  finding=$(printf "%s" "$answer" | jq -s -r "if length != 1 then \"x\" elif (.[0].data | type) != \"object\" then \"x\" else \"\" end" 2>/dev/null) || return 27
  [ -z "$finding" ] || return 27
  printf "%s" "$answer"
}'
queue dataempty
helper "$OLD_TRANSPORT
printf '[%s]' \"\$(get_issue_state EXP-1)\""
[ "$RC" = 0 ] && [ "$OUT" = "[]" ] || fail "negative control: the old transport no longer reads {\"data\":{}} as a ticket without a state"
queue healthy@500
helper "$OLD_TRANSPORT
linear_query '{ viewer { id } }' >/dev/null"
[ "$RC" = 0 ] || fail "negative control: the old transport no longer takes an HTTP 500 for a success"
queue nul
helper "$OLD_TRANSPORT
linear_query '{ viewer { id } }' >/dev/null"
[ "$RC" = 0 ] || fail "negative control: the old transport no longer lets a NUL byte through"
queue labelsnull
helper "$OLD_TRANSPORT
get_issue_detail EXP-1 | jq -c .labels"
[ "$RC" = 0 ] && [ "$OUT" = "[]" ] || fail "negative control: the old transport no longer reads a missing label list as 'no labels'"
queue healthy
helper "$OLD_TRANSPORT
linear_query '{ viewer { id } }' >/dev/null"
[ "$(cat "$SB/limits.log")" = "- -" ] || fail "negative control: the old transport passed a time limit"
echo "PASS negative control: the old transport takes {\"data\":{}}, an HTTP 500, a NUL byte and a missing label list for success and runs without a time limit"

# --- real curl against a local server: the write-out, the status and the time limit --------
REAL_CURL=$(command -v curl)
PORTFILE="$SB/port"
# Started from a subshell that exits at once, so the test shell never reports the server as
# a terminated job (bash would print "Terminated: 15" and the whole heredoc).
( python3 - "$PORTFILE" > "$SB/server.log" 2>&1 <<'PY' &
import http.server, socketserver, sys, time
BODY = b'{"data":{"viewer":{"id":"V1"}}}'
class H(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length", 0)))
        mode = self.path.strip("/")
        if mode == "hang":
            time.sleep(5)
        body = BODY + b"\x00" if mode == "nul" else BODY
        self.send_response(500 if mode == "status500" else 200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
class S(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    def server_bind(self):
        # HTTPServer.server_bind resolves the host name (socket.getfqdn), which can hang
        # for a long time on macOS CI runners; the name is never used here.
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = self.server_address[:2]
s = S(("127.0.0.1", 0), H)
open(sys.argv[1], "w").write(str(s.server_address[1]))
s.serve_forever()
PY
echo $! > "$SB/server.pid" )
SERVER=$(cat "$SB/server.pid")
trap 'kill "$SERVER" 2>/dev/null; rm -rf "$SB"' EXIT
# A cold CI runner can take several seconds to start Python; wait up to 30 s.
waited=0
while [ ! -s "$PORTFILE" ] && [ "$waited" -lt 100 ] && kill -0 "$SERVER" 2>/dev/null; do
  /bin/sleep 0.3; waited=$((waited + 1))
done
[ -s "$PORTFILE" ] || fail "the local test server did not start (alive: $(kill -0 "$SERVER" 2>/dev/null && echo yes || echo no)): $(cat "$SB/server.log")"
mkdir -p "$SB/realbin"
cat > "$SB/realbin/curl" <<EOF
#!/bin/bash
# The real curl, pointed at the local server; every other argument unchanged.
args=()
for a in "\$@"; do
  [ "\$a" = https://api.linear.app/graphql ] && a="http://127.0.0.1:$(cat "$PORTFILE")/\$(cat "$SB/mode")"
  args+=("\$a")
done
exec "$REAL_CURL" "\${args[@]}"
EOF
chmod +x "$SB/realbin/curl"
real() {  # $1 = server mode, $2 = snippet; sets OUT ERR RC ELAPSED
  printf '%s' "$1" > "$SB/mode"
  local start=$SECONDS
  set +e
  OUT=$(cd "$SB" && PATH="$SB/realbin:$PATH" LINEAR_API_KEY=k BUREAU_LINEAR_RETRIES=0 /bin/bash -c "
    set -uo pipefail
    source '$SCRIPTS/bureau-config.sh'
    $2" 2>"$SB/err")
  RC=$?
  set -e
  ELAPSED=$((SECONDS - start))
  ERR=$(cat "$SB/err")
}
real ok "$FETCH"
[ "$RC" = 0 ] && [ "$OUT" = '{"data":{"viewer":{"id":"V1"}}}' ] || fail "real curl: a healthy answer came back as '$OUT' (exit $RC)"
real status500 "$FETCH"
[ "$RC" = 27 ] && case "$ERR" in *"(no-response)"*) true ;; *) false ;; esac || fail "real curl: HTTP 500 was not unusable (exit $RC)"
real nul "$FETCH"
[ "$RC" = 27 ] && case "$ERR" in *"(not-json)"*) true ;; *) false ;; esac || fail "real curl: a NUL byte was not unusable (exit $RC)"
BUREAU_LINEAR_MAX_TIME=1 real hang "$FETCH"
[ "$RC" = 27 ] && [ "$ELAPSED" -lt 4 ] || fail "real curl: a hanging server was not cut off at 1 s (exit $RC after ${ELAPSED}s)"
real hang "$OLD_TRANSPORT
$FETCH >/dev/null"
[ "$RC" = 0 ] && [ "$ELAPSED" -ge 4 ] || fail "negative control: the old transport no longer waits for a hanging server (exit $RC after ${ELAPSED}s)"
echo "PASS real curl: the status line is read and stripped, HTTP 500 and a NUL byte are unusable, a hanging server is cut off at the limit (the old transport waited ${ELAPSED}s)"
