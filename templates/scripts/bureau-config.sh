#!/bin/bash
# bureau-config.sh — reads .bureau.json for pipeline scripts
# Source this file: source "$(dirname "$0")/bureau-config.sh"

# bureau_load_env: every script under scripts/ reads its .env through this instead of
# sourcing it, so no value in that file can run as a command. `KEY= value` under `source`
# executes `value` and bash echoes it in the "command not found" message — into a log that
# reaches Linear or GitHub. The reader parses instead of executing, restricts assignment to
# an allow-list, disables a running `set -x` before the first expansion, and accepts the
# arithmetic-bound keys only as plain digits (bash re-evaluates variable *content* inside
# `$(( ))`). Written for bash 3.2.
#
# _BUREAU_SCRIPTS_DIR is this file's own directory, resolved once at source time: helpers
# that run a sibling script must take it from the checkout this config came from, never
# from ./scripts/ relative to wherever the stage has cd'd to.
_BUREAU_SCRIPTS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=templates/scripts/bureau-env.sh
source "$_BUREAU_SCRIPTS_DIR/bureau-env.sh"
# The scripts that source this file copy the Linear key into API_KEY once they
# have read .env. bureau_load_env never exports the key itself (v3.2); an
# API_KEY the operator's shell happens to export would still carry the copy to
# every process the script starts, so it loses the export attribute here.
export -n API_KEY

_find_config() {
  local common primary candidate
  if [ -n "${BUREAU_CONFIG:-}" ]; then
    [ -f "$BUREAU_CONFIG" ] || { echo "ERROR: explicit BUREAU_CONFIG missing: $BUREAU_CONFIG" >&2; exit 1; }
  else
    common=$(git rev-parse --git-common-dir 2>/dev/null || true)
    primary=""
    [ -n "$common" ] && primary="$(cd "$common/.." && pwd)"
    for candidate in "$PWD/.bureau.json" "${primary:-$PWD}/.bureau.json" "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/.bureau.json"; do
      if [ -f "$candidate" ]; then BUREAU_CONFIG="$candidate"; break; fi
    done
    [ -n "${BUREAU_CONFIG:-}" ] || { echo "ERROR: .bureau.json not found. Run bureau-init." >&2; exit 1; }
  fi
  BUREAU_CONFIG="$(cd "$(dirname "$BUREAU_CONFIG")" && pwd)/$(basename "$BUREAU_CONFIG")"
  export BUREAU_CONFIG
  BUREAU_ENV_FILE="${BUREAU_ENV_FILE:-$(dirname "$BUREAU_CONFIG")/.env}"
  # The .env every script reads (v3.2: never ./.env, which in a stage worktree is a file the
  # branch controls). A relative value counts from the directory of .bureau.json, not from the
  # working directory.
  case "$BUREAU_ENV_FILE" in /*) ;; *) BUREAU_ENV_FILE="$(dirname "$BUREAU_CONFIG")/$BUREAU_ENV_FILE" ;; esac
}
_find_config
# Capture the caller boundary separately from user-facing .env settings. An
# older file may assign those settings again (even to the same value).
if [ "${BUREAU_CALLER_STOP:-0}" = 1 ] || [ "${BUREAU_STOP_REQUESTED:-0}" = 1 ] || [ "${BUREAU_NO_MERGE:-0}" = 1 ]; then
  if [ "${BUREAU_CALLER_STOP:-0}" != 1 ]; then BUREAU_CALLER_STOP=1; fi
  export BUREAU_CALLER_STOP
  readonly BUREAU_CALLER_STOP
fi
export BUREAU_STOP_REQUESTED="${BUREAU_STOP_REQUESTED:-${BUREAU_NO_MERGE:-0}}"
bureau_stop_requested() {
  [ "${BUREAU_CALLER_STOP:-0}" = 1 ] || [ "${BUREAU_STOP_REQUESTED:-0}" = 1 ] || [ "${BUREAU_NO_MERGE:-0}" = 1 ]
}
BUREAU_RUNTIME="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/bureau-runtime.py"

# Re-enter after claiming the issue and checkout. The Python parent owns cleanup,
# leaving each pipeline's EXIT traps intact. Nested stages reuse the driver's run.
bureau_stage_enter() {
  local issue="$1"; shift
  if [ "${BUREAU_ACTIVE_ENTRY:-}" = "$0" ]; then
    BUREAU_EXPECTED_STATE_ID=$(bureau_issue_snapshot "$issue" | jq -r '.state.id // empty')
    [ -n "$BUREAU_EXPECTED_STATE_ID" ] || { echo "ERROR: missing entry state" >&2; exit 10; }
    return 0
  fi
  if [ "${BUREAU_WORKSPACE_MODE:-current}" != disposable ] && [ "$(basename "$0")" != merge-pipeline.sh ]; then
    echo "ERROR: background stages require a disposable Bureau worker. For the current app checkout use bureau-runtime.py prepare/finish." >&2
    exit 21
  fi
  [ "${BUREAU_DRY_RUN:-0}" = 1 ] && { echo "[DRY_RUN] stage $(basename "$0") issue=$issue"; exit 0; }
  if [ "$#" = 0 ]; then set -- "$issue"; fi
  # Without the .env keys the relaunched stage reads back itself (bureau-env.sh).
  bureau_exec_runtime python3 -I "$BUREAU_RUNTIME" --repo "$PWD" exec --issue "$issue" --entry "$0" -- bash "$0" "$@"
}

bureau_get() { jq -r "$1" "$BUREAU_CONFIG"; }

# Linear config
BUREAU_TEAM_KEY=$(bureau_get '.linear.teams[0].key')
BUREAU_TEAM_ID=$(bureau_get '.linear.teams[0].id')
BUREAU_TEAM_NAME=$(bureau_get '.linear.teams[0].name')

# States
BUREAU_STATE_TRIAGE=$(bureau_get '.linear.teams[0].states.triage')
BUREAU_STATE_SPEC=$(bureau_get '.linear.teams[0].states.spec')
BUREAU_STATE_SPEC_REVIEW=$(bureau_get '.linear.teams[0].states.spec_review')
BUREAU_STATE_DESIGN=$(bureau_get '.linear.teams[0].states.design')
BUREAU_STATE_BUILD=$(bureau_get '.linear.teams[0].states.build')
BUREAU_STATE_BUILD_REVIEW=$(bureau_get '.linear.teams[0].states.build_review')
BUREAU_STATE_DONE=$(bureau_get '.linear.teams[0].states.done')
# Optional states (empty string when not configured in .bureau.json).
# qa slots between Build and Build Review. copy slots between Design and Build
# (or between Spec Review and Build if no Design stage). merge slots between
# Build Review and Done — opt-in waiting room for the gated merge agent.
BUREAU_STATE_QA=$(bureau_get '.linear.teams[0].states.qa // empty')
BUREAU_STATE_COPY=$(bureau_get '.linear.teams[0].states.copy // empty')
BUREAU_STATE_MERGE=$(bureau_get '.linear.teams[0].states.merge // empty')

# Labels
BUREAU_LABEL_LANE2=$(bureau_get '.linear.labels.lane2.id')
BUREAU_LABEL_LANE2_NAME=$(bureau_get '.linear.labels.lane2.name')
BUREAU_LABEL_NEEDS_HUMAN=$(bureau_get '.linear.labels.needs_human.id')
BUREAU_LABEL_NEEDS_UX=$(bureau_get '.linear.labels.needs_ux.id')
BUREAU_LABEL_AI_IMPL=$(bureau_get '.linear.labels.ai_implementable.id')
# Optional: copywriter gate. Absence → copy pipeline is disabled for this repo.
BUREAU_LABEL_NEEDS_COPY_NAME=$(bureau_get '.linear.labels.needs_copy.name // empty')

# Optional: path to a copy voice guide (markdown). Read by copy-pipeline.sh.
BUREAU_COPY_VOICE_FILE=$(bureau_get '.repo.copy_voice_file // empty')

# Agent config
BUREAU_POLL_INTERVAL=$(bureau_get '.agents.poll_interval_minutes // 30')
BUREAU_WORKBENCH_PANES=$(bureau_get '.agents.workbench_panes // 2')
BUREAU_MAX_REVIEW_CYCLES=$(bureau_get '.agents.max_review_cycles // 3')
# Code-review diff size at which specialists switch from exhaustive to
# critical-path sampling. Repos with mature CI / type-safety can review
# bigger diffs exhaustively; legacy repos cap lower. Configurable per repo.
BUREAU_CODE_REVIEW_SAMPLING_THRESHOLD=$(bureau_get '.agents.code_review_sampling_threshold // 500')
# How merge-pipeline closes the PR. Valid: squash | merge | rebase. Default
# squash so shipped main never carries the in-development merge commits the
# pipelines accumulate. Repos that want explicit merge-commit history (or
# strict linear via rebase) opt out per-repo.
# Per-stage model override (EXP-490). Resolution is performed live by
# resolve_model_for_stage / claude_cmd_for_stage — see below for the
# precedence contract. Example .bureau.json shape:
#   {"agents": {"model": "claude-sonnet-4-6",
#               "spec": {"model": "claude-opus-4-7"},
#               "code_review": {"model": "claude-haiku-4-5-20251001"}}}
#
# bureau_get_agent_model: type-safe lookup for `.agents.<stage>.model`. An
# agent toggle may be a boolean (`true`/`false`) or a string (`"v2"`); plain
# `.agents.<stage>.model` errors with "Cannot index boolean with string". This
# helper guards on type and returns empty unless `.agents.<stage>` is actually
# an object holding a `.model` field.
bureau_get_agent_model() {
  jq -r ".agents.$1 | if type==\"object\" then .model // empty else empty end" "$BUREAU_CONFIG"
}

# NOTE: We deliberately do NOT pre-load BUREAU_MODEL_DEFAULT or
# BUREAU_MODEL_<STAGE> from .bureau.json here. The previous source-time
# pre-load had two failure modes:
#   (a) an operator's `BUREAU_MODEL_<STAGE>=…` env override got CLOBBERED
#       by the JSON read, so the documented "env beats per-stage JSON"
#       contract silently broke;
#   (b) downstream consumers (bureau-status.sh --config, claude_cmd_for_stage)
#       couldn't tell whether `BUREAU_MODEL_<STAGE>` came from an operator or
#       from the JSON pre-load, so resolution precedence was indeterminate.
# Resolution now happens live via resolve_model_for_stage on every call.

# Cap on concurrent in-flight issues (EXP-491). 0 = unlimited (current default).
# 1 = single-flight (drain one issue end-to-end before another enters Spec).
# Higher values bound parallelism without forbidding it. Only spec-pipeline
# honours this; downstream stages keep operating on whatever's already in
# flight so a cap of 1 doesn't deadlock the loop.
BUREAU_MAX_CONCURRENT_ISSUES=$(bureau_get '.agents.max_concurrent_issues // 0')
BUREAU_MERGE_STRATEGY=$(bureau_get '.agents.merge_strategy // "squash"')
case "$BUREAU_MERGE_STRATEGY" in
  squash|merge|rebase) ;;
  *)
    echo "WARN: .agents.merge_strategy = '$BUREAU_MERGE_STRATEGY' is not one of {squash, merge, rebase} — falling back to squash." >&2
    BUREAU_MERGE_STRATEGY="squash"
    ;;
esac

# ── Merge policy (agents.merge_mode) ──────────────────────────────────
# Who merges an approved PR. "auto" (default): the pipeline, through
# merge-pipeline.sh. "manual": a human. Then the merge and rebase agents are off
# (agent_enabled), merge-pipeline.sh and rebase-pipeline.sh refuse before any
# Linear, gh or git call, and the review stage parks an approved ticket in the
# Merge state instead of merging it. manual needs that Merge state: without it
# the review stage refuses at its start (exit 24) — there is nowhere to park.
# The policy holds regardless of agents.merge / agents.rebase and
# BUREAU_FORCE_ALL_AGENTS (the shepherd forces every stage on), which is why it
# is its own key. It replaces the local kill-switches installs carried at the
# top of both scripts.
#
# Absent (or null) is auto. Every other value but exactly "auto" or "manual" —
# a typo, "auto\n", or `false` meant as "don't merge" — falls closed to manual:
# it must never switch automatic merging on. jq compares the raw value, so the
# shell never sees a trailing newline it could strip. Read from .bureau.json
# only, no env override. bureau-doctor.py applies the same rule.
BUREAU_MERGE_MODE=$(bureau_get '.agents.merge_mode | if . == null then "auto" elif . == "auto" or . == "manual" then . else tojson end')
case "$BUREAU_MERGE_MODE" in
  auto|manual) ;;
  *)
    echo "WARN: .agents.merge_mode = '$BUREAU_MERGE_MODE' is not one of {auto, manual} — falling closed to manual (no automatic merge or rebase)." >&2
    BUREAU_MERGE_MODE="manual"
    ;;
esac

# bureau_merge_is_manual — true unless the merge mode is exactly "auto", so an
# unset value also counts as manual.
bureau_merge_is_manual() {
  [ "${BUREAU_MERGE_MODE:-}" != auto ]
}

# merge_mode_lacks_merge_state — true when a human merges but the first team has
# no Merge state to park an approved ticket in. The review stage refuses then.
merge_mode_lacks_merge_state() {
  bureau_merge_is_manual && [ -z "${BUREAU_STATE_MERGE:-}" ]
}

# stop_before_merge_was_asked <exit-code> — true when the code is 20
# (stopped-before-merge) and the caller asked for that stop (--no-merge /
# BUREAU_NO_MERGE). That 20 is the requested end of the automated run, so the
# shepherd and the queue loop stop quietly instead of alerting. Any other 20
# still alerts; under merge_mode manual no template stage exits 20.
stop_before_merge_was_asked() {
  [ "${1:-}" = 20 ] && bureau_stop_requested
}
# ── End of merge policy ───────────────────────────────────────────────

# Repo config
BUREAU_BRANCH_PREFIX=$(bureau_get '.repo.branch_prefix // "feat"')
BUREAU_COMMIT_PREFIX=$(bureau_get '.repo.commit_prefix // ""')
BUREAU_SPECS_DIR=$(bureau_get '.repo.specs_dir // "specs"')

# Projects filter (comma-separated UUIDs; empty = all projects in the team)
BUREAU_PROJECTS=$(bureau_get '.linear.projects // [] | join(",")')

# ── Linear fetches: check the answer, retry, else stop with our own code ──
# Carried over from installation A (EXP-1478), where every fetch used to be
# passed on unchecked: an error page ended at `jq` with exit 5, while an answer
# carrying `errors`, an empty answer and a failed connection all came back as
# SUCCESS with an empty result — and the stage then decided on that empty
# result (it moved a ticket from Build Review back to Build and reported a
# missing branch marker).
#
# A fetch now counts as successful only when curl exited 0 within its time
# limit, the server answered with an HTTP 2xx status, the body as received (NUL
# bytes included) is ONE JSON object, that object carries a non-empty object
# `data` in which no requested root field is null, it has no non-empty `errors`,
# and — where the caller names one — the answer has the shape the caller reads.
# Any other answer is unusable, gets exactly one fault class from the fixed list
# (no-response, not-json, graphql-errors, no-data) and is retried after a wait.
# If it stays unusable the fetch prints NOTHING and returns
# $BUREAU_EXIT_LINEAR_UNUSABLE; no answer text and no key travels in a message.
#
# EXP-1482 (carried over from installation A's follow-up): curl used to run
# with neither a status check nor a time limit. An error page whose body was
# `{"data":{}}` counted as a success, every reader then answered "nothing"
# (no state, no labels) with exit 0 and the shepherd slept forever or walked
# past needs-human; a hanging request never reached the retry at all. And the
# shell drops NUL bytes while it captures, so a body that jq would reject
# reached the check already cleaned. Now every NUL byte is turned into a
# control byte that no JSON text may contain before the shell sees the body,
# and the status and the time limit are read from curl itself.
#
# A root field that is null without `errors` is a broken answer here because
# every root field this template asks for (`issues`, `issueLabels`, `viewer`
# and the mutation payloads) is non-null in Linear's schema. A future query of
# a nullable root field (e.g. `issueVcsBranchSearch`) must not go through
# these helpers unchanged. An issue query that matches no issue (`nodes: []`)
# is a usable answer: "no such ticket" is not "Linear unusable".
#
# The code is 27 (`linear-unusable` in exit_class), not installation A's 20: in
# this template 20 is `stopped-before-merge`. 10 (`linear-down`) stays the
# precondition code for a missing or invalid key.
#
# Settings (first usable wins): environment (and therefore .env) →
# .bureau.json `.linear.retry.*` → default. Defaults: three retries, waiting
# 10, 30 and 60 seconds. Zero retries and a wait of 0 are valid.
# The time limit of one request comes the same way from
# BUREAU_LINEAR_MAX_TIME / `.linear.request.max_time` (1 to 300 s, default 30)
# and BUREAU_LINEAR_CONNECT_TIMEOUT / `.linear.request.connect_timeout` (1 to
# 60 s, default 10). With the defaults a fetch that stays unusable gives up
# after at most 4 attempts × 30 s + 10 + 30 + 60 s of waiting = 220 s; with
# every setting at its maximum after 11 × 300 s + 10 × 600 s = 9,300 s.
BUREAU_EXIT_LINEAR_UNUSABLE=27

# The write-out curl appends after the body: a marker line and the HTTP status.
# Real curl prints it on every run, 000 when no answer came. An output without
# it can only come from a curl double in a test, whose answer is then judged by
# its body alone — the status check itself is held by tests that print one.
_BUREAU_LINEAR_STATUS_MARK='__BUREAU_HTTP_STATUS__:'

# _bureau_linear_classify <curl-exit> <answer> [<http-status>] [<shape>] —
# prints the fault class, or nothing when the answer is usable. Reads no value
# out of the answer.
#   <http-status>: the status curl reported; any status outside 2xx is
#     unusable. Empty or left out, the status is not judged.
#   <shape>: a jq condition on the whole answer that must hold (a constant
#     from this file, never text from outside); a broken shape is no-data.
_bureau_linear_classify() {
  local code="$1" answer="$2" status="${3:-}" shape="${4:-true}"
  [ "$code" = 0 ] || { printf 'no-response'; return 0; }
  # A NUL byte from the wire arrives here as \001 (see _bureau_linear_fetch);
  # no JSON text may contain either byte unescaped.
  case "$answer" in
    *$'\001'*) printf 'not-json'; return 0 ;;
  esac
  case "$answer" in
    *[![:space:]]*) ;;
    *) printf 'no-response'; return 0 ;;
  esac
  local finding
  # -s so that two JSON values in one body ("{} {}") are not read as one.
  finding=$(printf '%s' "$answer" | jq -s -r '
    if length != 1 then "not-json"
    elif (.[0] | type) != "object" then "no-data"
    elif (.[0].errors != null) and (.[0].errors != []) then "graphql-errors"
    elif (.[0].data | type) != "object" then "no-data"
    elif (.[0].data | length) == 0 then "no-data"
    elif ([.[0].data[] | select(. == null)] | length) > 0 then "no-data"
    elif (.[0] | '"$shape"') != true then "no-data"
    else "" end' 2>/dev/null) || finding="not-json"
  case "$finding" in
    '' | not-json | no-data | graphql-errors) ;;
    *) finding="not-json" ;;
  esac
  if [ -z "$finding" ] && [ -n "$status" ]; then
    case "$status" in
      2[0-9][0-9]) ;;
      *) finding="no-response" ;;
    esac
  fi
  printf '%s' "$finding"
}

# _bureau_linear_number <value> <max> [<min>] — prints <value> as a number
# without leading zeros, or nothing (exit 1) when it is not a whole number from
# <min> (default 0) to <max>. Digits only: no whitespace, no sign, no dot, no second word. The value
# never reaches an arithmetic context before it has passed this check.
_bureau_linear_number() {
  local value="$1" max="$2" min="${3:-0}"
  case "$value" in
    '' | *[!0123456789]*) return 1 ;;
  esac
  [ "${#value}" -gt 4 ] && return 1
  value="${value#"${value%%[!0]*}"}"
  [ -z "$value" ] && value=0
  [ "$value" -le "$max" ] && [ "$value" -ge "$min" ] || return 1
  printf '%s' "$value"
}

# _bureau_linear_setting <key> <value-from-env> <json-path> <default> <max> [<min>]
# An empty value counts as "not set", silently. Any other invalid value is
# dropped: the next source applies, and one warning on stderr names the key or
# the JSON path and NEVER the value.
_bureau_linear_setting() {
  local name="$1" from_env="$2" path="$3" default="$4" max="$5" min="${6:-0}"
  local number
  if [ -n "$from_env" ]; then
    if number=$(_bureau_linear_number "$from_env" "$max" "$min"); then
      printf '%s' "$number"
      return 0
    fi
    echo "warning: $name ignored: not a whole number of $min to $max written in digits only; the next source applies" >&2
  fi
  local from_json
  # Two guards, because a command substitution does not hand on what jq wrote.
  # First: the value has to be digits only ALREADY INSIDE jq. A shell drops every
  # embedded NUL byte while capturing (bash 3.2 and 5), so a `"0\u0000"` in the
  # JSON would otherwise reach the check as a clean `0` and pass it — the retry
  # would be off without a word. What is not digits only leaves jq as the word
  # `invalid`, which _bureau_linear_number rejects like any other non-number, so
  # the one place that decides what a number is stays the one place.
  # Second: the trailing '#' is a marker, not part of the value, because the
  # capture also strips every trailing newline — with the marker a `"0\n"`
  # survives as far as the check. Only the last '#' is removed, so a value
  # ending in '#' stays invalid as well.
  from_json=$(jq -r "
    try ($path) catch null
    | if . == null then \"\" else (if type == \"string\" then . else tojson end) end
    | (if (explode | all(. >= 48 and . <= 57)) then . else \"invalid\" end) + \"#\"" \
    "$BUREAU_CONFIG" 2>/dev/null) || from_json="#"
  from_json=${from_json%\#}
  if [ -n "$from_json" ]; then
    if number=$(_bureau_linear_number "$from_json" "$max" "$min"); then
      printf '%s' "$number"
      return 0
    fi
    echo "warning: $path ignored: not a whole number of $min to $max written in digits only; the default applies" >&2
  fi
  printf '%s' "$default"
}

# _bureau_linear_record <fault-class> — remember the fault class of the LAST
# attempt; shepherd.sh reads the file after a stage exited with
# $BUREAU_EXIT_LINEAR_UNUSABLE. Writes only a name from the fixed list, never a
# byte of the answer. Silent on every failure: a halt must not depend on a
# writable file.
_bureau_linear_record() {
  local file="${_BUREAU_LINEAR_FAULT_FILE:-}"
  [ -n "$file" ] || return 0
  printf '%s\n' "$1" > "$file" 2>/dev/null || true
  return 0
}

# _bureau_linear_request_limits — prints "<max-time> <connect-timeout>" for one
# request. Read on every fetch (lazily, like the retry settings) because a stage
# loads .env only after it has sourced this file. The minimum is 1: 0 would
# mean "no limit" to curl, so it is invalid like any other bad value — the
# warning names the key and the next source (.bureau.json, then the default)
# applies.
_bureau_linear_request_limits() {
  local max_time connect
  max_time=$(_bureau_linear_setting BUREAU_LINEAR_MAX_TIME "${BUREAU_LINEAR_MAX_TIME:-}" '.linear.request.max_time' 30 300 1)
  connect=$(_bureau_linear_setting BUREAU_LINEAR_CONNECT_TIMEOUT "${BUREAU_LINEAR_CONNECT_TIMEOUT:-}" '.linear.request.connect_timeout' 10 60 1)
  printf '%s %s' "$max_time" "$connect"
}

# _bureau_linear_fetch <payload> [<shape>] — one fetch, retried while the
# answer is unusable. <shape> is passed on to _bureau_linear_classify.
# stdout: the usable answer, otherwise nothing. Exit: 0 or
# $BUREAU_EXIT_LINEAR_UNUSABLE.
#
# The retry settings are read only once an answer is unusable: a healthy fetch
# waits not at all, retries not at all and prints no extra line. On a halt path
# (_BUREAU_LINEAR_SINGLE_ATTEMPT=1, set by shepherd.sh and by the spec stage's
# rollback trap) a single attempt is made without any retry wait. Each attempt
# can still take up to the request time limit, so a halt with N writes takes at
# most N × max-time (30 s by default) — bounded, but it grows with the writes.
# _bureau_curl_config <option> <value> — one line of a curl config file
# (curl -K -) with <value> quoted and backslash, quote, newline, carriage
# return and tab escaped: a secret, or text that may hold one (an alert's log
# tail), goes to curl on stdin this way instead of on its argument list.
_bureau_curl_config() {
  local value="$2"
  value="${value//\\/\\\\}"
  value="${value//\"/\\\"}"
  value="${value//$'\n'/\\n}"
  value="${value//$'\r'/\\r}"
  value="${value//$'\t'/\\t}"
  printf '%s = "%s"\n' "$1" "$value"
}

_bureau_linear_fetch() {
  local payload="$1" shape="${2:-true}"
  local attempt=1 code raw answer status fault wait limits max_time connect
  local planned=0 retries=0 w1=10 w2=30 w3=60
  limits=$(_bureau_linear_request_limits)
  max_time="${limits% *}"
  connect="${limits#* }"
  while : ; do
    code=0
    # tr runs before the shell captures anything: a NUL byte would otherwise be
    # dropped silently and the rest could pass as clean JSON. The exit code is
    # curl's own, not tr's, with or without the caller's pipefail.
    # The key reaches curl on stdin as a config line (-K -), never in its
    # argument list, which `ps` shows to every process (on Linux to every
    # user), and curl itself starts without the secrets in its environment
    # (dropped in this subshell after the config line is built). A running
    # `set -x` is off inside the subshell, so the key is not traced.
    raw=$({ set +x; } 2>/dev/null
      auth_config=$(_bureau_curl_config header "Authorization: ${API_KEY:-$LINEAR_API_KEY}")
      _bureau_drop_secrets
      curl -s -X POST https://api.linear.app/graphql \
      --connect-timeout "$connect" --max-time "$max_time" \
      -w "\\n${_BUREAU_LINEAR_STATUS_MARK}%{http_code}" \
      -H "Content-Type: application/json" \
      -K - -d "$payload" <<< "$auth_config" \
      | LC_ALL=C tr '\000' '\001'; exit "${PIPESTATUS[0]}") || code=$?
    case "$raw" in
      *"$_BUREAU_LINEAR_STATUS_MARK"*)
        status="${raw##*"$_BUREAU_LINEAR_STATUS_MARK"}"
        answer="${raw%$'\n'"$_BUREAU_LINEAR_STATUS_MARK"*}"
        ;;
      *)
        status=""
        answer="$raw"
        ;;
    esac
    [ "$code" = 28 ] && echo "linear: no answer within ${max_time}s (curl timed out)" >&2
    fault=$(_bureau_linear_classify "$code" "$answer" "$status" "$shape")
    if [ -z "$fault" ]; then
      printf '%s' "$answer"
      return 0
    fi
    if [ "${_BUREAU_LINEAR_SINGLE_ATTEMPT:-0}" = 1 ]; then
      echo "linear: unusable answer ($fault) on a halt path — one attempt only, giving up" >&2
      return "$BUREAU_EXIT_LINEAR_UNUSABLE"
    fi
    if [ "$planned" = 0 ]; then
      retries=$(_bureau_linear_setting BUREAU_LINEAR_RETRIES "${BUREAU_LINEAR_RETRIES:-}" '.linear.retry.retries' 3 10)
      w1=$(_bureau_linear_setting BUREAU_LINEAR_RETRY_WAIT_1 "${BUREAU_LINEAR_RETRY_WAIT_1:-}" '.linear.retry.wait_1' 10 600)
      w2=$(_bureau_linear_setting BUREAU_LINEAR_RETRY_WAIT_2 "${BUREAU_LINEAR_RETRY_WAIT_2:-}" '.linear.retry.wait_2' 30 600)
      w3=$(_bureau_linear_setting BUREAU_LINEAR_RETRY_WAIT_3 "${BUREAU_LINEAR_RETRY_WAIT_3:-}" '.linear.retry.wait_3' 60 600)
      planned=1
    fi
    if [ "$attempt" -gt "$retries" ]; then
      echo "linear: unusable answer ($fault) after $attempt attempt(s) — giving up with exit $BUREAU_EXIT_LINEAR_UNUSABLE" >&2
      _bureau_linear_record "$fault"
      return "$BUREAU_EXIT_LINEAR_UNUSABLE"
    fi
    case "$attempt" in
      1) wait="$w1" ;;
      2) wait="$w2" ;;
      *) wait="$w3" ;;
    esac
    echo "linear: unusable answer ($fault), attempt $attempt of $((retries + 1)) — retrying in ${wait}s" >&2
    sleep "$wait"
    attempt=$((attempt + 1))
  done
}

# halt_if_linear_unusable <exit-code> — for the few call sites that CATCH a
# helper's exit code (`if add_issue_label …; then`). Ends the stage when the
# code says "Linear stayed unusable"; returns 1 for every other non-zero code,
# so the caller's own error branch runs exactly as before.
halt_if_linear_unusable() {
  if [ "${1:-0}" = "$BUREAU_EXIT_LINEAR_UNUSABLE" ]; then
    echo "linear: the stage cannot decide without this answer — giving up with exit $BUREAU_EXIT_LINEAR_UNUSABLE" >&2
    exit "$BUREAU_EXIT_LINEAR_UNUSABLE"
  fi
  return 1
}

# Helper: query Linear GraphQL. Every caller captures the answer first and
# carries `|| return $?`: piped straight into jq, the fetch's exit code is lost.
linear_query() {
  _bureau_linear_fetch "{\"query\": \"$1\"}"
}

# Helper: run a raw GraphQL payload (for mutations that need variables).
# linear_raw <payload> [<shape>] — a reader passes the shape its jq depends on
# (see the _BUREAU_SHAPE_* constants below), as linear_issue_query does.
linear_raw() {
  _bureau_linear_fetch "$1" "${2:-}"
}

# The shapes the issue readers below depend on. A reader that fills a missing
# list with `// []` cannot tell "this ticket has no labels" from "the answer
# carried no label list", so the list has to be there before the answer counts
# as usable — and a broken one goes through the retry ladder like any other
# unusable answer. An issue query that matches nothing (`nodes: []`) passes:
# every condition over no nodes holds.
_BUREAU_SHAPE_ISSUES='((.data.issues.nodes | type) == "array")'
_BUREAU_SHAPE_ISSUE_LABELS="$_BUREAU_SHAPE_ISSUES"' and all(.data.issues.nodes[]; (.labels.nodes | type) == "array")'
_BUREAU_SHAPE_ISSUE_STATE="$_BUREAU_SHAPE_ISSUE_LABELS"' and all(.data.issues.nodes[]; (.state.id | type) == "string")'
_BUREAU_SHAPE_ISSUE_COMMENTS="$_BUREAU_SHAPE_ISSUES"' and all(.data.issues.nodes[]; (.comments.nodes | type) == "array")'

# linear_issue_query <query> <shape> — linear_query for a reader that depends
# on <shape> (one of the _BUREAU_SHAPE_* constants).
linear_issue_query() {
  _bureau_linear_fetch "{\"query\": \"$1\"}" "$2"
}

# EXP-490: per-stage model resolution. Resolution order (first non-empty
# wins):
#   1. BUREAU_MODEL_<STAGE> env  (operator override, e.g. ad-hoc shell var)
#   2. .agents.<stage>.model     (per-stage JSON)
#   3. .agents.model             (workspace JSON default)
#   4. BUREAU_MODEL_DEFAULT env  (workspace env fallback, typically .env)
#   5. empty → no --model flag emitted (claude CLI's own default applies)
#
# Reads JSON live every call. Does NOT depend on any source-time JSON→env
# pre-load (see the note above bureau_get_agent_model for why). Single source
# of truth: both claude_cmd_for_stage and the bureau-status.sh --config
# display call this.
#
# Stage names match the .bureau.json agents.* keys: spec, spec_review, ux,
# copy, implement, qa, code_review, merge, research.
#
# Stdout: the resolved model identifier, or empty if none configured.
resolve_model_for_stage() {
  local stage="$1"
  # bash 3.2 (macOS default) lacks ${var^^} uppercase expansion, so use tr.
  # See similar bash-3.2 caveat in pick_issue (parallel arrays vs `declare -A`).
  local upper
  upper=$(printf '%s' "$stage" | tr '[:lower:]' '[:upper:]')
  local var="BUREAU_MODEL_${upper}"
  local model

  # (1) per-stage env (operator override)
  model="${!var:-}"

  # (2) per-stage JSON
  if [ -z "$model" ]; then
    model=$(bureau_get_agent_model "$stage")
  fi

  # (3) workspace JSON default
  if [ -z "$model" ]; then
    model=$(bureau_get '.agents.model // empty')
  fi

  # (4) workspace env fallback
  if [ -z "$model" ]; then
    model="${BUREAU_MODEL_DEFAULT:-}"
  fi

  printf '%s' "$model"
}

# Resolve which model RUNNER backs a stage: "claude" (default) or "codex".
# Same precedence ladder as resolve_model_for_stage:
#   (1) per-stage env   BUREAU_RUNNER_<STAGE>   (e.g. BUREAU_RUNNER_QA=codex)
#   (2) per-stage JSON  .agents.<stage>.runner
#   (3) workspace JSON  .agents.runner
#   (4) hard default    "claude"
# Anything other than "codex" resolves to "claude" — opt-in, never silent.
resolve_runner_for_stage() {
  local stage="$1"
  local upper
  upper=$(printf '%s' "$stage" | tr '[:lower:]' '[:upper:]')
  local var="BUREAU_RUNNER_${upper}"
  local runner
  runner="${!var:-}"
  if [ -z "$runner" ]; then
    runner=$(jq -r ".agents.$stage | if type==\"object\" then .runner // empty else empty end" "$BUREAU_CONFIG" 2>/dev/null)
  fi
  if [ -z "$runner" ]; then
    runner=$(bureau_get '.agents.runner // empty')
  fi
  runner="${runner:-claude}"
  case "$runner" in claude|codex) echo "$runner" ;; *) echo "ERROR: unknown runner $runner" >&2; return 22 ;; esac
}

# Execute a creative pass without shell command strings or ARG_MAX-sized argv.
run_stage_for() {
  local stage="$1"; shift
  local temp rc system="" schema=""
  while [ "$#" -gt 1 ]; do
    case "$1" in
      --append-system-prompt) system="$2"; shift 2 ;;
      --schema) schema="$2"; shift 2 ;;
      *) break ;;
    esac
  done
  [ "$#" = 1 ] || { echo 'run_stage_for requires one prompt' >&2; return 22; }
  temp=$(mktemp -d)
  printf '%s' "$1" > "$temp/prompt"
  printf '%s\n' "You are a creative worker in an already claimed Bureau background stage ($stage). Do not invoke prepare/finish, queue workers, or Linear mutations. Follow project instructions and stage boundaries in scripts/bureau-stage.md. Include Bureau-Generated: true on authored commits when Git writes are permitted. If a path in your worktree (such as .venv) is a symlink that points outside the worktree, it is the main checkout's shared environment: never delete, recreate or --clear it, and do not install into it unless the ticket asks. If it is missing or not a symlink, handle it as usual." "$system" > "$temp/system"
  local args=(--stage "$stage" --repo "$PWD" --config "$BUREAU_CONFIG" --prompt-file "$temp/prompt" --system-file "$temp/system")
  [ -n "$schema" ] && args+=(--schema "$schema")
  # The provider needs none of the Bureau secrets: it starts without them, so
  # the agent's parent process does not hold them either (bureau-env.sh).
  if bureau_without_secrets python3 -I "$(dirname "$BUREAU_RUNTIME")/bureau-provider.py" "${args[@]}"; then rc=0; else rc=$?; fi
  rm -rf "$temp"
  return "$rc"
}

precondition_runner() {
  bureau_without_secrets python3 -I "$(dirname "$BUREAU_RUNTIME")/bureau-provider.py" --stage "$1" --config "$BUREAU_CONFIG" --check >/dev/null || exit $?
}

commit_codex_changes() {
  [ "$(resolve_runner_for_stage "$1")" = codex ] || return 0
  commit_stage_changes "$@"
}

# Include newly created files as well as tracked changes. Keep local config,
# credentials and evidence out of executor-authored commits.
commit_stage_changes() {
  [ -n "$(git status --porcelain)" ] || return 0
  # An earlier tool may have staged private files already. Excluding them from
  # our `git add` list alone would still include them in the final commit.
  # Refuse without changing the index, leaving the operator's staged work intact.
  bureau_without_secrets python3 -I - <<'PY_STAGED' || return $?
import subprocess, sys
names = subprocess.check_output(['git', 'diff', '--cached', '--name-only', '--no-renames', '-z']).split(b'\0')
private = [p for p in names if p in (b'.env', b'.bureau.json', b'.bureau-install.json') or p.startswith(b'logs/')]
if private:
    print('Refusing to commit staged private Bureau files; inspect and unstage them before retrying.', file=sys.stderr)
    sys.exit(24)
PY_STAGED
  local paths
  paths=$(mktemp)
  bureau_without_secrets python3 -I - "$paths" <<'PY_PATHS'
import subprocess, sys
from pathlib import Path
names = subprocess.check_output(['git', 'ls-files', '-z', '--modified', '--deleted', '--others', '--exclude-standard']).split(b'\0')
keep = [p for p in names if p and p not in (b'.env', b'.bureau.json', b'.bureau-install.json') and not p.startswith(b'logs/')]
Path(sys.argv[1]).write_bytes(b'\0'.join(keep) + (b'\0' if keep else b''))
PY_PATHS
  if [ -s "$paths" ]; then
    GIT_LITERAL_PATHSPECS=1 git add -A --pathspec-from-file="$paths" --pathspec-file-nul
  fi
  rm -f "$paths"
  git diff --cached --quiet || git commit -m "$2: Bureau $1 changes" -m "Bureau-Generated: true"
}

# Build the model invocation for a stage. Default backend is `claude -p`;
# when the stage's runner resolves to "codex", emit a codex-stage-runner.sh
# invocation that presents the SAME `cmd "PROMPT"` → verdict-on-stdout contract
# (see that script's header). Emits `--model <m>` only when
# resolve_model_for_stage finds one; otherwise the CLI's own default applies.
#
# Legacy: no template script calls this any more — every stage and
# upstream-port.sh go through run_stage_for, which starts the runner through
# bureau-provider.py as an argument list. Kept for installations' own scripts
# and tests/test_model_resolution.sh. Do NOT word-split its output
# (`$(claude_cmd_for_stage …)` unquoted): a model value from .env then adds
# runner options of its own (installation A EXP-1476, tests/test_model_argv.sh).
claude_cmd_for_stage() {
  local stage="$1"
  local model runner
  model=$(resolve_model_for_stage "$stage")
  runner=$(resolve_runner_for_stage "$stage")

  if [ "$runner" = "codex" ]; then
    # Read-only for review (it must not mutate the tree); workspace-write for
    # QA (it commits tests). The stage name decides the safe default.
    local sandbox="workspace-write"
    case "$stage" in
      code_review|research) sandbox="read-only" ;;
      *) sandbox="workspace-write" ;;
    esac
    # The `model` resolved above is a CLAUDE model id — must NOT be forwarded to
    # codex. Codex's model comes from a separate codex-specific source so the
    # two never cross-contaminate:
    #   (1) per-stage env  BUREAU_CODEX_MODEL_<STAGE>
    #   (2) workspace env  BUREAU_CODEX_MODEL_DEFAULT
    #   (3) omit → codex CLI's own configured default applies
    local cupper cvar cmodel
    cupper=$(printf '%s' "$stage" | tr '[:lower:]' '[:upper:]')
    cvar="BUREAU_CODEX_MODEL_${cupper}"
    cmodel="${!cvar:-${BUREAU_CODEX_MODEL_DEFAULT:-}}"
    local runner_path="${BUREAU_SCRIPT_DIR:-scripts}/codex-stage-runner.sh"
    if [ -n "$cmodel" ]; then
      echo "bash $runner_path --model $cmodel --sandbox $sandbox --"
    else
      echo "bash $runner_path --sandbox $sandbox --"
    fi
    return
  fi

  # EXP-671 — cost tracking swaps `--print` (text) for `--output-format json`
  # (envelope with usage). Default OFF → `--print`, byte-identical. parse_claude_json
  # unwraps the envelope transparently, so consumers are unaffected either way.
  local out_flag="--print"
  cost_tracking_enabled && out_flag="--output-format json"

  # EXP-token-efficiency — optional headroom wrap. When .agents.headroom_wrap
  # is true, prefix the claude invocation with `headroom wrap ` so headroom's
  # compression pipeline sits between this script and the Anthropic API.
  # Reversible (CCR): claude can call `headroom_retrieve` to fetch originals
  # if a summary is too lossy for the current task. Scoped to the claude
  # backend only — the codex path above is left alone (codex has its own
  # sandboxing layer that doesn't compose cleanly with headroom wrap).
  # Headroom must be on PATH; misconfiguration surfaces immediately on the
  # first stage invocation. See docs/configuration.md for the full schema.
  # `headroom wrap claude` is the launcher; `--` separates headroom's OWN flags
  # (it has -p/--port) from claude's args. WITHOUT `--`, headroom parses claude's
  # `-p` (print) as its --port and dies ("'--print' is not a valid integer").
  # headroom's own help documents exactly this form: `headroom wrap claude -- -p`.
  local launcher="claude" sep=""
  if headroom_wrap_enabled; then
    launcher="headroom wrap claude"
    sep="-- "
  fi

  if [ -n "$model" ]; then
    echo "${launcher} ${sep}-p $out_flag --dangerously-skip-permissions --model $model"
  else
    echo "${launcher} ${sep}-p $out_flag --dangerously-skip-permissions"
  fi
}

# EXP-token-efficiency — opt-in toggles for the three token-efficiency layers.
# Same precedence ladder as cost_tracking_enabled: env var first, then JSON
# (.agents.<flag>), default off. Live read on every call so flipping the flag
# mid-flight doesn't require a queue-loop restart.

# headroom_wrap_enabled: prefix `headroom wrap ` on every claude invocation.
# Used by claude_cmd_for_stage above.
headroom_wrap_enabled() {
  [ "${BUREAU_HEADROOM_WRAP:-}" = "1" ] && return 0
  command -v jq >/dev/null 2>&1 || return 1
  [ "$(jq -r '.agents.headroom_wrap // false' "${BUREAU_CONFIG:-.bureau.json}" 2>/dev/null)" = "true" ]
}

# use_goal_loop_enabled: implement-pipeline.sh drives via `claude -p "/goal …"`
# instead of the bash for-loop when this is true. Closes the EXP-573 / EXP-571
# / EXP-624 / EXP-627 stuck-detector tangle structurally.
use_goal_loop_enabled() {
  [ "$(resolve_runner_for_stage implement)" = claude ] || return 1
  [ "${BUREAU_USE_GOAL_LOOP:-}" = "1" ] && return 0
  command -v jq >/dev/null 2>&1 || return 1
  [ "$(jq -r '.agents.use_goal_loop // false' "${BUREAU_CONFIG:-.bureau.json}" 2>/dev/null)" = "true" ]
}

# caveman_level: returns one of off | lite | full | ultra | wenyan. Read by
# SKILL.md Phase 5.5 at /bureau-init time AND by per-stage scripts that may
# prefix `/caveman <level>` to a prompt (review prose only — never commit
# messages or PR bodies). Default off.
caveman_level() {
  if [ -n "${BUREAU_CAVEMAN_LEVEL:-}" ]; then
    printf '%s' "$BUREAU_CAVEMAN_LEVEL"
    return
  fi
  command -v jq >/dev/null 2>&1 || { printf 'off'; return; }
  local level
  level=$(jq -r '.agents.caveman_level // "off"' "${BUREAU_CONFIG:-.bureau.json}" 2>/dev/null)
  case "$level" in
    off|lite|full|ultra|wenyan) printf '%s' "$level" ;;
    *) printf 'off' ;;
  esac
}

# EXP-671 — opt-in per-stage cost/token tracking. Default OFF (the pipeline is
# byte-identical). Enable via BUREAU_COST_TRACKING=1 or .bureau.json
# session.cost_tracking=true.
cost_tracking_enabled() {
  [ "${BUREAU_COST_TRACKING:-}" = "1" ] && return 0
  command -v jq >/dev/null 2>&1 || return 1
  [ "$(jq -r '.session.cost_tracking // false' "${BUREAU_CONFIG:-.bureau.json}" 2>/dev/null)" = "true" ]
}

# EXP-671 — append one stage's token usage + est. $ to a per-issue cost log.
# Pipelines call this once after each claude invocation:
#   record_stage_cost "$RESULT" "$ISSUE" "implement"
# No-op when cost tracking is off, jq is missing, or the output carries no usage
# envelope (codex / --print) — so it's safe to call unconditionally.
# Log: $BUREAU_COST_DIR (default ~/.bureau/cost)/<issue>.jsonl. The legacy
# default ~/.brainhuggers/bureau-cost still works if set explicitly via the env.
record_stage_cost() {
  cost_tracking_enabled || return 0
  command -v jq >/dev/null 2>&1 || return 0
  local raw="$1" issue="$2" stage="$3"
  local usage
  usage=$(printf '%s' "$raw" | jq -c 'if type=="object" and has("usage") then .usage else empty end' 2>/dev/null)
  [ -z "$usage" ] && return 0
  local in out cost dir
  in=$(printf '%s' "$usage" | jq -r '.input_tokens // 0' 2>/dev/null)
  out=$(printf '%s' "$usage" | jq -r '.output_tokens // 0' 2>/dev/null)
  cost=$(printf '%s' "$raw" | jq -r '.total_cost_usd // null' 2>/dev/null)
  dir="${BUREAU_COST_DIR:-$HOME/.bureau/cost}"
  mkdir -p "$dir" 2>/dev/null || return 0
  jq -cn --arg issue "$issue" --arg stage "$stage" \
    --arg provider "$(printf '%s' "$raw" | jq -r '.provider // "claude"')" \
    --argjson input "${in:-0}" --argjson output "${out:-0}" --argjson cost "${cost:-null}" \
    '{issue:$issue,stage:$stage,provider:$provider,input_tokens:$input,output_tokens:$output,
      cost_usd:$cost,estimated_cost_usd:$cost,actual_billed_cost_usd:null}' >> "$dir/$issue.jsonl"
}

# EXP-671 — aggregate the per-issue cost logs into a report. Used by
# `bureau-status.sh --cost`. Prints a per-issue / per-stage token + $ summary.
report_costs() {
  local dir="${BUREAU_COST_DIR:-$HOME/.bureau/cost}"
  if [ ! -d "$dir" ] || [ -z "$(ls -A "$dir" 2>/dev/null)" ]; then
    echo "No cost data yet. Enable with session.cost_tracking=true (or BUREAU_COST_TRACKING=1) and run the pipeline."
    return 0
  fi
  command -v jq >/dev/null 2>&1 || { echo "jq required for the cost report"; return 1; }
  echo "Bureau cost report (per issue → per stage)"
  echo "──────────────────────────────────────────"
  local f issue
  for f in "$dir"/*.jsonl; do
    [ -f "$f" ] || continue
    issue=$(basename "$f" .jsonl)
    jq -rs --arg issue "$issue" '
      (group_by(.stage) | map({stage: .[0].stage,
         in: (map(.input_tokens) | add), out: (map(.output_tokens) | add),
         cost: (if any(.cost_usd == null) then null else map(.cost_usd) | add end)})) as $byStage
      | "\($issue):",
        ($byStage[] | "  \(.stage): \(.in) in · \(.out) out · $\(if .cost == null then "unavailable" else .cost * 1000 | round / 1000 end)"),
        "  TOTAL: $\(if any($byStage[]; .cost == null) then "unavailable" else ($byStage | map(.cost) | add) * 1000 | round / 1000 end)"
    ' "$f"
  done
}

# Helper: check if agent is enabled.
# Shepherd (and other end-to-end drivers) export BUREAU_FORCE_ALL_AGENTS=1 to
# force every stage on regardless of `.agents.<stage>` toggles — for "really
# end to end" runs that want to route through every configured state.
# Conditional state-presence checks (e.g. `[ -n "$BUREAU_STATE_QA" ]`) still
# apply: force-all only overrides the agent toggle, not state configuration.
agent_enabled() {
  # merge_mode manual switches the merge and rebase agents off, forced or not:
  # no dispatcher (queue loop, bounded tick, tmux windows) spends a tick on a
  # stage that would only refuse.
  case "${1:-}" in merge|rebase) if bureau_merge_is_manual; then return 1; fi ;; esac
  [ "${BUREAU_FORCE_ALL_AGENTS:-0}" = "1" ] && return 0
  local val
  val=$(jq -r --arg stage "$1" '.agents[$stage] | if type == "object" then
      if has("enabled") then .enabled else true end else . // false end' "$BUREAU_CONFIG")
  [ "$val" != "false" ] && [ "$val" != "null" ]
}

# ── Linear glue helpers (EXP-412) ──────────────────────────────────
# These replace the legacy pattern of spawning `claude -p` + remote Linear MCP
# to perform routine CRUD. Remote MCP uses short-lived OAuth tokens that can't
# be refreshed from headless subprocesses — the first cron tick worked, every
# subsequent tick failed silently. Direct GraphQL + LINEAR_API_KEY is stable,
# fast, and free of hidden Claude token burn.

# Resolve "EXP-123" → Linear UUID. Caches nothing — re-queried per call.
_resolve_issue_uuid() {
  local ref="$1"
  if [[ "$ref" =~ ^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$ ]]; then
    printf '%s' "$ref"
    return 0
  fi
  local team_key="${ref%%-*}"
  local number="${ref##*-}"
  local answer
  answer=$(linear_query "{ issues(filter: { team: { key: { eq: \\\"$team_key\\\" } }, number: { eq: $number } }) { nodes { id } } }") || return $?
  printf '%s' "$answer" | jq -r '.data.issues.nodes[0].id // empty'
}

# Move an issue to a new state.
# Usage: move_issue <issue-id-or-key> <state-uuid>
#
# Dry-run: BUREAU_DRY_RUN=1 logs the intent and returns 0 without hitting
# Linear. Same shape on post_comment, add_issue_label, alert_telegram so a
# developer can point a fresh checkout at a real Linear team and watch the
# pipeline without polluting state.
move_issue() {
  local ref="$1" state_id="$2"
  if [ "${BUREAU_DRY_RUN:-0}" = "1" ]; then
    echo "[DRY_RUN] move_issue $ref → $state_id" >&2
    return 0
  fi
  if [ -n "${BUREAU_EXPECTED_STATE_ID:-}" ] && [ "$ref" = "${BUREAU_CURRENT_ISSUE:-}" ]; then
    local current_snapshot current_state
    current_snapshot=$(bureau_issue_snapshot "$ref") || return $?
    current_state=$(printf '%s' "$current_snapshot" | jq -r '.state.id // empty')
    if [ "$current_state" != "$BUREAU_EXPECTED_STATE_ID" ]; then
      echo "ERROR: $ref state changed during this stage; refusing stale transition" >&2
      return 21
    fi
  fi
  local uuid
  uuid=$(_resolve_issue_uuid "$ref") || return $?
  if [ -z "$uuid" ]; then
    echo "move_issue: could not resolve $ref to UUID" >&2
    return 1
  fi
  local payload
  payload=$(jq -n --arg id "$uuid" --arg sid "$state_id" \
    '{query: "mutation($id: String!, $sid: String!) { issueUpdate(id: $id, input: { stateId: $sid }) { success } }",
      variables: {id: $id, sid: $sid}}')
  local result
  result=$(linear_raw "$payload") || return $?
  local ok
  ok=$(printf '%s' "$result" | jq -r '.data.issueUpdate.success // false')
  if [ "$ok" != "true" ]; then
    echo "move_issue: $ref → $state_id failed: $result" >&2
    return 1
  fi
  if [ -n "${BUREAU_EXPECTED_STATE_ID:-}" ] && [ "$ref" = "${BUREAU_CURRENT_ISSUE:-}" ]; then
    BUREAU_EXPECTED_STATE_ID="$state_id"
  fi
}


# bureau_cap_comment <text> [<max-bytes>] — prints <text> fitted to a comment size
# limit, by default BUREAU_COMMENT_MAX_BYTES (60000 bytes of UTF-8).
#
# Linear refuses a comment body over its limit with an error answer, which the
# transport retries (10 + 30 + 60 s) and then reports as 27 "Linear unusable", so
# one long model answer halted a stage as if Linear were down; GitHub refuses a PR
# comment over 65,536 characters, and the review stage swallowed that failure. A
# text that fits is printed unchanged (bytes that are not UTF-8 become U+FFFD). A
# longer one keeps its beginning and its end, and a note in the middle says how
# many bytes were left out: the first line (a branch marker, the "Code Review …
# Changes Requested" header implement looks for) survives. The last fenced JSON
# block (the review's verdict) is kept whole, with everything after it, whenever it
# fits in the limit with 4 KB of beginning to spare. Each cut moves to a line break
# only when one is within 4 KB, else it falls between two characters: never inside
# a UTF-8 character, never a collapse to a few bytes. The result is at most
# <max-bytes> bytes.
BUREAU_COMMENT_MAX_BYTES=60000
bureau_cap_comment() {
  printf '%s' "$1" | python3 -I -c '
import sys
limit = int(sys.argv[1])
data = sys.stdin.buffer.read().decode("utf-8", "replace").encode("utf-8")
if len(data) <= limit:
    sys.stdout.buffer.write(data)
    sys.exit(0)
def note(cut):
    return ("\n\n[… %d bytes cut from the middle of this comment: it was longer than the %d bytes"
            " Bureau posts to Linear and GitHub. The stage output has the full text. …]\n\n" % (cut, limit)).encode("utf-8")
# The number of cut bytes has at most as many digits as the whole length.
room = limit - len(note(len(data)))
if room < 2:
    sys.stdout.buffer.write(data[:limit].decode("utf-8", "ignore").encode("utf-8"))
    sys.exit(0)
NEAR = 4096
tail_start = len(data) - (room - room // 2)
block = data.rfind(b"\n```json\n") + 1
if 0 < block < tail_start and len(data) - block <= room - NEAR:
    tail = data[block:]
else:
    tail = data[tail_start:]
    start_at = tail.find(b"\n", 0, NEAR)
    if start_at >= 0:
        tail = tail[start_at + 1:]
head = data[:room - len(tail)]
cut_at = head.rfind(b"\n", max(0, len(head) - NEAR))
if cut_at >= 0:
    head = head[:cut_at]
# A cut between two lines far from any break can split a character; drop its bytes.
head = head.decode("utf-8", "ignore").encode("utf-8")
tail = tail.decode("utf-8", "ignore").encode("utf-8")
sys.stdout.buffer.write(head + note(len(data) - len(head) - len(tail)) + tail)
' "${2:-$BUREAU_COMMENT_MAX_BYTES}"
}

# Post a markdown comment to an issue. The body is fitted to the comment size
# limit first (bureau_cap_comment), so a long comment never fails the stage.
# Usage: post_comment <issue-id-or-key> <body>
post_comment() {
  local ref="$1" body="$2" capped
  if [ "${BUREAU_DRY_RUN:-0}" = "1" ]; then
    echo "[DRY_RUN] post_comment $ref ($(printf '%s' "$body" | head -c 80 | tr '\n' ' ')...)" >&2
    return 0
  fi
  if capped=$(bureau_cap_comment "$body"); then
    body="$capped"
  else
    echo "post_comment: could not fit the comment for $ref to the size limit; posting it unchanged" >&2
  fi
  local uuid
  uuid=$(_resolve_issue_uuid "$ref") || return $?
  if [ -z "$uuid" ]; then
    echo "post_comment: could not resolve $ref to UUID" >&2
    return 1
  fi
  local payload
  payload=$(jq -n --arg id "$uuid" --arg body "$body" \
    '{query: "mutation($id: String!, $body: String!) { commentCreate(input: { issueId: $id, body: $body }) { success } }",
      variables: {id: $id, body: $body}}')
  local result
  result=$(linear_raw "$payload") || return $?
  local ok
  ok=$(printf '%s' "$result" | jq -r '.data.commentCreate.success // false')
  if [ "$ok" != "true" ]; then
    echo "post_comment: $ref failed: $result" >&2
    return 1
  fi
}

# crosscheck_open_prs: cross-check <tasks-file> against the open PRs and report
# the outcome on <issue>. Always returns 0.
#
# Carried over from installation A (EXP-1469). "No file conflicts" is only
# said after an explicit success: exit code 0 AND a last non-empty output line
# "CROSSCHECK RESULT: clean …". Exit 3 with "conflicts" posts the conflict
# warning as before. Every other pairing — an abort (bash itself exits 1 or 2),
# 4 from the script, 127 for a missing script, empty output, a code that
# disagrees with the word — is "incomplete" and posts exactly one warning. The
# spec stage used to run the script with `|| true` and grep for "conflicts
# detected", so an abort read as "No file conflicts with open PRs" on every run.
#
# The last non-empty line counts, never the first match: a PR title in the
# report can itself read like a result line.
#
# Sets CROSSCHECK_RESULT to clean, conflicts or incomplete.
#
# The trap: the spec stage runs under `set -euo pipefail`, and
#     out=$(bash …/crosscheck-specs.sh …); rc=$?          # WRONG
# ends the stage at exit 3 or 4 before `rc=$?` is ever reached — and the
# stage's `trap _spec_recovery EXIT` then routes the issue back to Triage. Only
# a command in an `if` condition is exempt from `set -e`.
#
# The script is found via $_BUREAU_SCRIPTS_DIR, never ./scripts/: the result
# line is a contract between script and evaluation, and both have to come from
# the same checkout.
#
# Dry-run: post_comment logs the intent and writes nothing; no branch here.
#
# Usage: crosscheck_open_prs <issue> <tasks-file>
crosscheck_open_prs() {
  local issue="$1" tasks="$2" out rc line last="" pattern
  local word="" compared="" paths="" unchecked="" reason heading body comment posted
  if out=$(bash "$_BUREAU_SCRIPTS_DIR/crosscheck-specs.sh" "$tasks" 2>&1); then rc=0; else rc=$?; fi
  printf '%s\n' "$out"

  while IFS= read -r line; do
    if [ -n "$line" ]; then
      last="$line"
    fi
  done <<< "$out"
  pattern='^CROSSCHECK RESULT: (clean|conflicts|incomplete) open=([0-9]+) compared=([0-9]+) paths=([0-9]+) unchecked=(-|#[0-9]+(,#[0-9]+)*)$'
  if [[ $last =~ $pattern ]]; then
    word="${BASH_REMATCH[1]}"
    compared="${BASH_REMATCH[3]}"
    paths="${BASH_REMATCH[4]}"
    unchecked="${BASH_REMATCH[5]}"
  fi

  if [ "$rc" -eq 0 ] && [ "$word" = "clean" ]; then
    CROSSCHECK_RESULT="clean"
  elif [ "$rc" -eq 3 ] && [ "$word" = "conflicts" ]; then
    CROSSCHECK_RESULT="conflicts"
  else
    CROSSCHECK_RESULT="incomplete"
  fi

  if [ "$CROSSCHECK_RESULT" = "clean" ]; then
    echo "  No file conflicts with open PRs ($compared PRs compared, $paths planned paths)"
    return 0
  fi

  if [ "$CROSSCHECK_RESULT" = "conflicts" ]; then
    comment="⚠️ Crosscheck warning — spec conflicts with open PRs:

\`\`\`
$out
\`\`\`"
    if post_comment "$issue" "$comment"; then
      posted="warning posted to $issue"
    else
      posted="warning could NOT be posted to $issue"
    fi
    echo "  File conflicts with open PRs — $posted"
    return 0
  fi

  if [ -n "$unchecked" ] && [ "$unchecked" != "-" ]; then
    reason="Not checked: ${unchecked//,/, } — their changed files could not be read."
  elif [ -z "$word" ]; then
    reason="The cross-check ended without a result line, so it did not run to completion."
  else
    reason="The cross-check could not read all of its inputs — see its output below."
  fi
  if [ -n "$word" ]; then
    heading="Cross-check output:"
    body="$out"
  else
    heading="Last 20 lines of the cross-check output:"
    body=$(printf '%s\n' "$out" | tail -n 20)
  fi
  if [ -z "$body" ]; then
    body="(no output)"
  fi
  comment="⚠️ Crosscheck incomplete — this spec was NOT fully checked against open PRs (exit code $rc).

$reason

$heading
\`\`\`
$body
\`\`\`"
  if post_comment "$issue" "$comment"; then
    posted="warning posted to $issue"
  else
    posted="warning could NOT be posted to $issue"
  fi
  echo "  WARNING: crosscheck incomplete (exit $rc) — open PRs were NOT fully checked against this spec; $posted"
  return 0
}

# check_squash_range: does any commit in the squash range carry a CI
# suppressor? Runs squash-marker-check.sh against <base>..HEAD and leaves the
# answer in two globals; the caller decides what a halt means.
#
# Carried over from installation A (EXP-1465). The second layer behind
# merge-body.sh: that one defangs the message merge-pipeline.sh writes, this one
# reads the commits themselves, which is what reaches main on a rebase merge or
# a merge done by hand. The implement stage calls it before the hand-off, the
# QA stage before it routes.
#
# Sets:
#   SQUASH_CHECK   clean | found | unchecked
#   SQUASH_REPORT  the script's one line (clean), its report (found), or a line
#                  saying the range could not be checked followed by whatever
#                  the script printed (unchecked)
#
# Exit 3 is a finding. Every other non-zero code — 2 from the script itself,
# 1 from a bash that tripped, 127 for a missing script — is "unchecked", and
# unchecked halts like a finding: a guard that waves through what it could not
# read is the silent failure it exists to prevent.
#
# Always returns 0. The trap: the stages run under `set -euo pipefail`, and
#     out=$(bash …/squash-marker-check.sh …); rc=$?          # WRONG
# ends the stage at the first finding, before `rc=$?` is ever reached. Only a
# command in an `if` condition is exempt from `set -e`, and in the else-branch
# of the un-negated form `$?` is the script's own code.
#
# The script is found via $_BUREAU_SCRIPTS_DIR, never relative to the working
# directory: stages run from worktrees.
#
# Usage: check_squash_range [<base>]   (base defaults to origin/main)
check_squash_range() {
  local basis="${1:-origin/main}" out rc
  if out=$(bash "$_BUREAU_SCRIPTS_DIR/squash-marker-check.sh" "$basis" 2>&1); then
    SQUASH_CHECK="clean"
    SQUASH_REPORT="$out"
  else
    rc=$?
    if [ "$rc" -eq 3 ]; then
      SQUASH_CHECK="found"
      SQUASH_REPORT="$out"
    else
      SQUASH_CHECK="unchecked"
      SQUASH_REPORT="Squash range $basis..HEAD could not be checked (exit code $rc) — halting rather than passing it on unchecked.
$out"
    fi
  fi
  return 0
}

# comment_on_branch_pr: post <text> as a comment on the open PR of <branch>,
# if there is one. Loud on failure, never fatal; always returns 0.
#
# Carried over from installation A (EXP-1465). A halt for a CI suppressor has
# to show where the merge happens, not only in Linear. It is a comment and not
# a flip back to draft on purpose: no stage makes that transition today.
#
# "No PR" covers three answers of `gh pr list --jq '.[0].number'`: empty, the
# literal string "null" it prints when nothing matches, and gh failing —
# whatever it printed then is not an answer. The text goes in on stdin
# (--body-file -), so a multi-line report arrives unmangled.
#
# Dry-run: BUREAU_DRY_RUN=1 logs the intent and returns 0 without calling gh.
#
# Usage: comment_on_branch_pr <branch> <text>
comment_on_branch_pr() {
  local branch="$1" text="$2" pr out rc
  if [ "${BUREAU_DRY_RUN:-0}" = "1" ]; then
    echo "[DRY_RUN] comment_on_branch_pr $branch ($(printf '%s' "$text" | head -c 80 | tr '\n' ' ')...)" >&2
    return 0
  fi
  if pr=$(gh pr list --head "$branch" --json number --jq '.[0].number' 2>/dev/null); then
    :
  else
    pr=""
  fi
  if [ -z "$pr" ] || [ "$pr" = "null" ]; then
    echo "  no open PR for $branch — the finding stands in Linear and in logs/escalations.log"
    return 0
  fi
  if out=$(printf '%s\n' "$text" | gh pr comment "$pr" --body-file - 2>&1); then
    :
  else
    rc=$?
    echo "  ✗ could not comment on PR #$pr (gh exit code $rc) — the finding stands in Linear and in logs/escalations.log" >&2
    echo "$out" | sed 's/^/       gh: /' >&2
  fi
  return 0
}

# Resolve the working branch for an issue.
# Lookup order:
#   1. A bureau-branch marker comment posted by the spec pipeline. The marker
#      MUST be the first line of the comment body:
#        <!-- bureau-branch: 001-automated-tests -->
#        **Spec Artifacts — EXP-123**
#        ...
#      Newest wins if multiple marker comments exist. Anchoring to the first
#      line avoids false positives from documentation/review comments that
#      quote the marker pattern in prose or code blocks.
#      A comment with an empty or null body has no first line and is skipped.
#   2. Fallback to Linear's auto-generated branchName (rarely matches the
#      sequential spec-number branches the pipeline uses, but better than
#      empty).
# Output: branch name on stdout, empty if nothing found.
get_issue_branch() {
  local ref="$1"
  local team_key="${ref%%-*}"
  local number="${ref##*-}"
  local data
  # first: 200 — covers virtually every long-running issue (REQUEST_CHANGES
  # cycles + bot pings rarely exceed this). The marker is posted once by the
  # spec pipeline near the top of the comment list; if it falls off the page,
  # downstream pipelines silently fall back to Linear's branchName which never
  # matches the sequential spec branch numbers.
  data=$(linear_issue_query "{ issues(filter: { team: { key: { eq: \\\"$team_key\\\" } }, number: { eq: $number } }) { nodes { branchName comments(first: 200) { nodes { body createdAt } } } } }" "$_BUREAU_SHAPE_ISSUE_COMMENTS") || return $?
  local marker
  marker=$(printf '%s' "$data" \
    | jq -r '
      (.data.issues.nodes[0].comments.nodes // [])
      | sort_by(.createdAt) | reverse
      | map((.body // "") | split("\n")[0] // "")
      | map(select(test("^<!-- bureau-branch: [^ ]+ -->[[:space:]]*$")))
      | .[0] // ""
    ' \
    | sed -E 's/^<!-- bureau-branch: //; s/ -->[[:space:]]*$//')
  if [ -n "$marker" ]; then
    printf '%s' "$marker"
    return 0
  fi
  printf '%s' "$data" | jq -r '.data.issues.nodes[0].branchName // empty'
}

# Combined fetch: returns { branch, comments } in one GraphQL roundtrip.
# Use when a caller needs both pieces close together (e.g. implement-pipeline
# resolves the branch then scans for review feedback). The branch is resolved
# the same way get_issue_branch resolves it (marker comment, newest wins,
# fallback to Linear's branchName). Comments are sorted newest-first to match
# get_issue_comments.
#
# Output: { "branch": "<resolved>", "comments": [{body, createdAt}, ...] }
# Both fields populated even if the issue has no comments.
get_issue_branch_and_comments() {
  local ref="$1"
  local team_key="${ref%%-*}"
  local number="${ref##*-}"
  local answer
  answer=$(linear_issue_query "{ issues(filter: { team: { key: { eq: \\\"$team_key\\\" } }, number: { eq: $number } }) { nodes { branchName comments(first: 200) { nodes { body createdAt } } } } }" "$_BUREAU_SHAPE_ISSUE_COMMENTS") || return $?
  printf '%s' "$answer" \
    | jq '
      (.data.issues.nodes[0] // {}) as $issue
      | (($issue.comments.nodes // []) | sort_by(.createdAt) | reverse) as $comments
      | (
          $comments
          | map((.body // "") | split("\n")[0] // "")
          | map(select(test("^<!-- bureau-branch: [^ ]+ -->[[:space:]]*$")))
          | .[0] // ""
          | sub("^<!-- bureau-branch: "; "")
          | sub(" -->[[:space:]]*$"; "")
        ) as $marker
      | { branch: (if $marker != "" then $marker else ($issue.branchName // "") end),
          comments: $comments }
    '
}

# Return issue comments newest-first as a JSON array of objects {body, createdAt}.
get_issue_comments() {
  local ref="$1"
  local team_key="${ref%%-*}"
  local number="${ref##*-}"
  local answer
  answer=$(linear_issue_query "{ issues(filter: { team: { key: { eq: \\\"$team_key\\\" } }, number: { eq: $number } }) { nodes { comments(first: 200) { nodes { body createdAt } } } } }" "$_BUREAU_SHAPE_ISSUE_COMMENTS") || return $?
  printf '%s' "$answer" \
    | jq '(.data.issues.nodes[0].comments.nodes // []) | sort_by(.createdAt) | reverse'
}

# Return full issue detail as JSON: {identifier, title, description, project:{name,description}, labels:[names]}.
get_issue_detail() {
  local ref="$1"
  local team_key="${ref%%-*}"
  local number="${ref##*-}"
  local answer
  answer=$(linear_issue_query "{ issues(filter: { team: { key: { eq: \\\"$team_key\\\" } }, number: { eq: $number } }) { nodes { identifier title description project { name description } labels { nodes { name } } } } }" "$_BUREAU_SHAPE_ISSUE_LABELS") || return $?
  printf '%s' "$answer" \
    | jq '(.data.issues.nodes[0] // {}) | {identifier, title, description, project: (.project // {name: null, description: null}), labels: ((.labels.nodes // []) | map(.name))}'
}

# Raw identity/state snapshot for optimistic stage completion checks.
bureau_issue_snapshot() {
  local ref="$1" team_key="${1%%-*}" number="${1##*-}"
  local answer
  answer=$(linear_issue_query "{ issues(filter: { team: { key: { eq: \\\"$team_key\\\" } }, number: { eq: $number } }) { nodes { id identifier title description state { id name } labels { nodes { name } } } } }" "$_BUREAU_SHAPE_ISSUE_STATE") || return $?
  printf '%s' "$answer" \
    | jq '.data.issues.nodes[0] // {}'
}

# Canonical names for existing stage guards, resolved from configured UUIDs.
get_issue_state() {
  local snapshot id key
  snapshot=$(bureau_issue_snapshot "$1") || return $?
  id=$(printf '%s' "$snapshot" | jq -r '.state.id // empty')
  key=$(jq -r --arg id "$id" '.linear.teams[0].states | to_entries[] | select(.value == $id and $id != "") | .key' "$BUREAU_CONFIG" | head -1)
  case "$key" in
    triage) echo Triage ;; spec) echo Spec ;; spec_review) echo 'Spec Review' ;;
    design) echo Design ;; copy) echo Copy ;; build) echo Build ;; qa) echo QA ;;
    build_review) echo 'Build Review' ;; merge) echo Merge ;; done) echo Done ;;
    *) printf '%s' "$snapshot" | jq -r '.state.name // empty' ;;
  esac
}

# _resolve_label_id <label-name> <issue-ref> — the UUID of the label <name> that
# applies to the issue's team. stdout: the UUID, or empty for a well-formed
# "no such label". Exit: 0; $BUREAU_EXIT_LINEAR_UNUSABLE when Linear stayed
# unusable; 2 when the answer was usable but a matching label could not be
# classified.
#
# Carried over from installation B (EXP-1340). The name-only lookup with
# `first: 1` returned whichever label of that name the server listed first. In
# a workspace where two teams both have `needs-human` (or `shepherd-focused`),
# that was deterministically the other team's label, which cannot attach to
# this team's issue — every attach failed, and remove_issue_label reported an
# idempotent success while the label stayed on. So the name is queried across
# the workspace, each candidate carrying its team, and chosen in strict order:
#   1. the label owned by the issue's own team
#   2. else a workspace-level label (team == null, valid for every team)
#   3. else nothing
# No server-side team filter on purpose: it would drop the workspace-level
# labels of tier 2 before they reach the choice.
#
# A matching node without classifiable team metadata or without a usable id is
# never skipped into "not found": that is exit 2, unless a usable winner exists
# anyway. Empty output with exit 0 is reserved for a clean no-match.
#
# The team comes from an identifier like EXP-123; for a UUID reference the
# configured team key applies.
_resolve_label_id() {
  local name="$1" ref="$2" team_key answer selection
  case "$ref" in
    [A-Z]*-[0-9]*) team_key="${ref%%-*}" ;;
    *) team_key="$BUREAU_TEAM_KEY" ;;
  esac
  answer=$(linear_query "{ issueLabels(filter: { name: { eq: \\\"$name\\\" } }, first: 20) { nodes { id team { key } } } }") || return $?
  selection=$(printf '%s' "$answer" | jq -r --arg tk "$team_key" '
    def usable_node:
      ( has("team")
        and ( (.team == null)
              or ( ((.team | type) == "object")
                   and (.team | has("key"))
                   and ((.team.key | type) == "string")
                   and ((.team.key | length) > 0) ) ) )
      and ((.id | type) == "string") and ((.id | length) > 0);
    if (.data.issueLabels.nodes | type) != "array" then "malformed"
    else
      .data.issueLabels.nodes as $nodes
      | ($nodes | map(select(usable_node)))    as $good
      | (($good | length) < ($nodes | length)) as $malformed
      | ( ([ $good[] | select(.team.key == $tk) ] | .[0])
          // ([ $good[] | select(.team == null) ] | .[0]) ) as $win
      | if $win != null then "id " + $win.id
        elif $malformed then "malformed"
        else "none" end
    end') || selection="malformed"
  case "$selection" in
    "id "*) printf '%s' "${selection#id }" ;;
    none) : ;;
    *)
      echo "_resolve_label_id: a label named '$name' could not be classified for team '$team_key'" >&2
      return 2 ;;
  esac
}

# Add a label (by name) to an issue.
# Usage: add_issue_label <issue-id-or-key> <label-name>
add_issue_label() {
  local ref="$1" name="$2"
  if [ "${BUREAU_DRY_RUN:-0}" = "1" ]; then
    echo "[DRY_RUN] add_issue_label $ref += '$name'" >&2
    return 0
  fi
  local uuid
  uuid=$(_resolve_issue_uuid "$ref") || return $?
  if [ -z "$uuid" ]; then
    echo "add_issue_label: could not resolve $ref" >&2
    return 1
  fi
  local label_id status=0
  label_id=$(_resolve_label_id "$name" "$ref") || status=$?
  if [ "$status" = "$BUREAU_EXIT_LINEAR_UNUSABLE" ]; then
    return "$status"
  elif [ "$status" != 0 ]; then
    echo "add_issue_label: label lookup failed for '$name'" >&2
    return 1
  fi
  if [ -z "$label_id" ]; then
    echo "add_issue_label: no label named '$name' for this team or the workspace" >&2
    return 1
  fi
  local payload
  payload=$(jq -n --arg id "$uuid" --arg lid "$label_id" \
    '{query: "mutation($id: String!, $lid: String!) { issueAddLabel(id: $id, labelId: $lid) { success } }",
      variables: {id: $id, lid: $lid}}')
  local ok
  local label_result
  label_result=$(linear_raw "$payload") || return $?
  ok=$(printf '%s' "$label_result" | jq -r '.data.issueAddLabel.success // false')
  [ "$ok" = "true" ]
}

# Remove a label (by name) from an issue. Mirrors add_issue_label.
# Idempotent on both ends — Linear's issueRemoveLabel no-ops if the label
# isn't currently applied; we also return success (without calling Linear) if
# no label of that name exists for the issue's team or the workspace, because
# the caller wants the label absent and it definitionally is.
# Usage: remove_issue_label <issue-id-or-key> <label-name>
remove_issue_label() {
  local ref="$1" name="$2"
  if [ "${BUREAU_DRY_RUN:-0}" = "1" ]; then
    echo "[DRY_RUN] remove_issue_label $ref -= '$name'" >&2
    return 0
  fi
  local uuid
  uuid=$(_resolve_issue_uuid "$ref") || return $?
  if [ -z "$uuid" ]; then
    echo "remove_issue_label: could not resolve $ref" >&2
    return 1
  fi
  local label_id status=0
  label_id=$(_resolve_label_id "$name" "$ref") || status=$?
  # A lookup failure is never an idempotent success: that would acknowledge a
  # release while the label may still be attached. Only a clean "no such label
  # for this team or the workspace" stays idempotent.
  if [ "$status" = "$BUREAU_EXIT_LINEAR_UNUSABLE" ]; then
    return "$status"
  elif [ "$status" != 0 ]; then
    echo "remove_issue_label: label lookup failed for '$name'" >&2
    return 1
  fi
  [ -z "$label_id" ] && return 0
  local payload
  payload=$(jq -n --arg id "$uuid" --arg lid "$label_id" \
    '{query: "mutation($id: String!, $lid: String!) { issueRemoveLabel(id: $id, labelId: $lid) { success } }",
      variables: {id: $id, lid: $lid}}')
  local ok
  local label_result
  label_result=$(linear_raw "$payload") || return $?
  ok=$(printf '%s' "$label_result" | jq -r '.data.issueRemoveLabel.success // false')
  [ "$ok" = "true" ]
}

# ── needs-human hold (EXP-1516) ─────────────────────────────────────────
# A stage that hands a ticket to a human adds the needs-human label, and the
# picker excludes that label: that is what keeps the paid stage from running the
# same ticket again. When the label write fails, the escalation must not live
# only in a comment. mark_needs_human then records the ticket in a local hold
# under the shared git directory ($(git rev-parse --git-common-dir)/bureau/
# needs-human-held/<ISSUE>, one file per ticket, visible from every worktree).
# pipeline_pick_next skips held tickets and tries the label again on every
# pick; once the label is on the ticket the hold ends, the label keeps the
# ticket out from there, and a human releases it the usual way, by removing the
# label. To release a held ticket without the label, delete its file.
# shepherd.sh, which runs a named ticket past the picker, reads the same holds
# through bureau_human_hold and refuses a held ticket before its claim.
#
# mark_needs_human <issue> <stage> [<stage exit>]
#   0  the label is on the ticket (any hold for it is cleared)
#   27 Linear stayed unusable: the ticket is held, the stage ends with 27 here
#   1  any other failure: the ticket is held, an alert goes out, and the caller
#      still posts its comment but must not end with 0 (it ends with 25 where it
#      would have ended with 0), so a driver halts instead of reading success.
#      <stage exit> is the code the caller will end with (default 25); the
#      alert names it.
#
# The directory comes from the repository that holds .bureau.json, not from the
# current directory, so every stage and picker of one repo sees the same holds
# wherever it runs from (bureau_common_dir).
bureau_common_dir() {
  local base="." common
  [ -n "${BUREAU_CONFIG:-}" ] && base=$(dirname "$BUREAU_CONFIG")
  if ! common=$(git -C "$base" rev-parse --git-common-dir 2>/dev/null); then
    [ "$base" != . ] || return 1
    # An explicit BUREAU_CONFIG outside any git repository: fall back to the
    # current directory's repository, as before, and say so.
    echo "bureau: $BUREAU_CONFIG is not inside a git repository — holds and the pause marker use the current directory's repository" >&2
    base=.
    common=$(git rev-parse --git-common-dir 2>/dev/null) || return 1
  fi
  [ -n "$common" ] || return 1
  case "$common" in /*) ;; *) common="$(cd "$base" && pwd)/$common" ;; esac
  printf '%s' "$common"
}

_needs_human_hold_dir() {
  local common
  common=$(bureau_common_dir) || return 1
  printf '%s/bureau/needs-human-held' "$common"
}

# bureau_human_hold <issue> — whether a human holds <issue>, for a driver that
# runs a named ticket past the picker (shepherd.sh). Prints nothing when nobody
# does, else one line: "hold <file>" when the ticket is held locally (checked
# first; it needs no Linear), or "label <name>" for the first of needs-human,
# the configured linear.labels.needs_human.name, blocked and wip on the ticket.
# Exit: 0, or the label read's own code (27 = Linear stayed unusable); an answer
# without a readable label list fails instead of counting as "no label".
bureau_human_hold() {
  local issue="$1" dir detail human
  if [[ "$issue" =~ ^[A-Z][A-Z0-9_]*-[0-9]+$ ]] && dir=$(_needs_human_hold_dir 2>/dev/null) \
     && [ -f "$dir/$issue" ]; then
    printf 'hold %s\n' "$dir/$issue"
    return 0
  fi
  detail=$(get_issue_detail "$issue") || return $?
  human=$(bureau_get '.linear.labels.needs_human.name // "needs-human"') || return $?
  printf '%s' "$detail" | jq -rn --arg human "$human" '
    input | .labels as $on
    | [ "needs-human", $human, "blocked", "wip" | select(. as $l | $on | any(.[]; . == $l)) ]
    | if length > 0 then "label " + .[0] else empty end'
}

mark_needs_human() {
  local issue="$1" stage="$2" stage_exit="${3:-25}" rc=0 dir="" held_at=""
  add_issue_label "$issue" "needs-human" || rc=$?
  if [ "$rc" = 0 ]; then
    if [ "${BUREAU_DRY_RUN:-0}" != 1 ] && dir=$(_needs_human_hold_dir); then
      rm -f "$dir/$issue"
    fi
    return 0
  fi
  # Only a ticket identifier becomes a file name: nothing else can escape the directory.
  if [[ "$issue" =~ ^[A-Z][A-Z0-9_]*-[0-9]+$ ]] && dir=$(_needs_human_hold_dir) \
     && mkdir -p "$dir" \
     && printf 'stage=%s\texit=%s\tat=%s\n' "$stage" "$rc" "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" > "$dir/.$issue.$$" \
     && mv -f "$dir/.$issue.$$" "$dir/$issue"; then
    held_at="$dir/$issue"
  fi
  if [ "$rc" = "$BUREAU_EXIT_LINEAR_UNUSABLE" ]; then
    if [ -n "$held_at" ]; then
      echo "  needs-human: Linear is unusable — $issue is held in $held_at; the queue skips it and sets the label once Linear answers" >&2
    fi
    halt_if_linear_unusable "$rc"
  fi
  if [ -n "$held_at" ]; then
    echo "  ✗ could not add 'needs-human' to $issue (exit $rc) — held in $held_at: the queue skips the ticket and tries the label again on every pick; delete that file to release it without the label" >&2
  else
    echo "  ✗ could not add 'needs-human' to $issue (exit $rc), and it could not be held locally — the queue may pick it again" >&2
  fi
  alert_telegram "$issue" "$stage" "$stage_exit" "needs-human could not be set (exit $rc)${held_at:+; the ticket is held in $held_at}" || true
  return 1
}

# needs_human_holds_flush: try the label again for every held ticket. Prints the
# tickets still held, comma-separated, on stdout; a ticket that now carries the
# label is released. A label that still cannot be written keeps its ticket held
# and never fails the pick: one ticket must not stop a queue. Each retry is a
# single attempt without waits (the next pick tries again), and after a 27 the
# remaining holds are not tried in this pick; if Linear is unusable for the pick
# as well, the pick's own read ends with 27. A dry run only reads the holds.
needs_human_holds_flush() {
  local dir f id rc held="" tried=1
  dir=$(_needs_human_hold_dir) || return 0
  [ -d "$dir" ] || return 0
  for f in "$dir"/*; do
    [ -f "$f" ] || continue
    id=${f##*/}
    [[ "$id" =~ ^[A-Z][A-Z0-9_]*-[0-9]+$ ]] || continue
    if [ "${BUREAU_DRY_RUN:-0}" != 1 ] && [ "$tried" = 1 ]; then
      rc=0
      _BUREAU_LINEAR_SINGLE_ATTEMPT=1 add_issue_label "$id" "needs-human" >&2 || rc=$?
      if [ "$rc" = 0 ]; then
        rm -f "$f"
        echo "needs-human: $id now carries the label — its local hold is released" >&2
        continue
      fi
      if [ "$rc" = "$BUREAU_EXIT_LINEAR_UNUSABLE" ]; then
        echo "needs-human: Linear gave up on the label for $id — every held ticket stays held and skipped in this pick" >&2
        tried=0
      fi
    fi
    held="${held:+$held,}$id"
  done
  printf '%s' "$held"
}
# ── End of needs-human hold ─────────────────────────────────────────────

# branch_is_bureau_only: returns 0 if every commit in
# `origin/main..origin/<branch>` is bureau-generated, 1 if even one
# human-authored commit is in the divergence. Used by rebase-pipeline
# (refuse-to-rebase guard) and merge-pipeline (decide whether to label
# `rebase-needed` on DIRTY PRs).
#
# A commit counts as bureau-generated if ANY of these hold:
#   1. Carries a `Co-authored-by: …Claude…` trailer (implementation commits).
#   2. Is a merge commit (2+ parents) — merge_origin_main_or_abort produces
#      these and they integrate content rather than author it.
#   3. Subject matches `^[A-Z]+-[0-9]+: spec artifacts$` — spec-pipeline.sh
#      autonomous commit (legacy; newer spec commits also carry the trailer).
#
# Caller is responsible for `git fetch origin` beforehand — keeping the fetch
# out lets callers batch it with their own fetches.
#
# Usage: if branch_is_bureau_only "$BRANCH"; then …; fi
branch_is_bureau_only() {
  local branch="$1"
  local human_commits
  human_commits=$(_bureau_human_commits "$branch") || return 1
  [ -z "$human_commits" ]
}

# Shared helper: prints SHAs of every commit in origin/main..origin/<branch>
# that does NOT match any of the three bureau-safe categories above. Used by
# branch_is_bureau_only (existence check) and rebase-pipeline.sh (diagnostic
# print when the predicate fails). Keeping the awk in one place ensures the
# refusal message lists exactly the commits the predicate considered human.
_bureau_human_commits() {
  local branch="$1"
  # tolower() rather than gawk-only IGNORECASE so the helper works under BSD
  # awk (macOS) and gawk (Linux CI) alike.
  local commits
  commits=$(git log "origin/main..origin/$branch" \
    --format='%H|%P|%s|%(trailers:key=Co-authored-by,valueonly,separator=,)|%(trailers:key=Bureau-Generated,valueonly,separator=,)' 2>/dev/null) || return 1
  printf '%s\n' "$commits" | awk -F'|' '
        {
          n = split($2, parents, " ")
          is_merge   = (n > 1)
          is_spec    = (tolower($3) ~ /^[a-z]+-[0-9]+: spec artifacts$/)
          has_claude = (tolower($4) ~ /claude/)
          has_bureau = ($5 == "true")
          if (!is_merge && !is_spec && !has_claude && !has_bureau) print $1
        }'
}

# ── Pre-merge correctness gates (Not-Rocket-Science Rule) ─────────
# `mergeStateStatus == CLEAN` is GitHub's heuristic — async-cached and lax
# when branch protection isn't configured. CLEAN passes when no required
# checks have *completed*, including the case where CI hasn't started yet.
# Bureau enforces green-CI and up-to-date-base independently of GitHub's
# status, so the merge gate doesn't depend on per-repo branch protection.

# Cache the gh-resolved owner/repo for the script lifetime (multiple helpers
# below call it; the underlying `gh` call hits the network).
_bureau_gh_owner_repo() {
  if [ -z "${_BUREAU_OWNER_REPO_CACHE:-}" ]; then
    _BUREAU_OWNER_REPO_CACHE=$(gh repo view --json nameWithOwner --jq .nameWithOwner 2>/dev/null || echo "")
  fi
  printf '%s' "$_BUREAU_OWNER_REPO_CACHE"
}

# pr_ci_is_green <pr-number>
# Returns 0 if every check-run AND legacy status context on the PR's CURRENT
# head SHA is completed and successful (success/skipped/neutral). Returns 1
# with a stderr diagnostic if any check is pending, in-progress, or failing.
#
# Independent of `mergeStateStatus`. Treats "no checks completed" as failure
# unless .agents.merge_min_required_checks is set to 0 (default 1 — repos
# without CI should opt out via .agents.merge_require_green_ci=false rather
# than via this knob).
#
# "No checks completed" is normally a check that has not started yet, which the
# merge stage reads as "not yet". A head that carries nothing at all — no check
# run, not even a queued one, and no status — when its commit is older than
# .agents.merge_ci_start_grace_seconds (default 1800) will not get CI any more
# (no workflow, a trigger that does not match): that is reported as its own
# line, "ci: no check run and no status on …", which the merge stage reads as
# blocked. Without the grace the gate stayed "not yet" forever, without an alert.
pr_ci_is_green() {
  # <head-sha>, when given, is the commit to judge (the merge stage pins its merge to it);
  # without it the PR's current head is read here.
  local pr="$1"
  local owner_repo head_sha="${2:-}"
  owner_repo=$(_bureau_gh_owner_repo)
  [ -z "$owner_repo" ] && { echo "ci: cannot resolve owner/repo" >&2; return 1; }
  [ -n "$head_sha" ] || head_sha=$(gh pr view "$pr" --json headRefOid --jq .headRefOid 2>/dev/null || echo "")
  [ -z "$head_sha" ] && { echo "ci: cannot resolve head SHA for #$pr" >&2; return 1; }

  # check-runs: paginate-and-slurp; `--paginate --jq` returns per-page
  # filtered output which loses the aggregation. The `-s` jq slurps all
  # response bodies into one array, then we concat their check_runs.
  local checks
  checks=$(gh api "repos/$owner_repo/commits/$head_sha/check-runs" --paginate 2>/dev/null \
            | jq -s 'map(.check_runs // []) | add // []') || {
    echo "ci: gh check-runs query failed for $head_sha" >&2
    return 1
  }

  # Legacy commit status (for status contexts not registered as check-runs,
  # e.g. some third-party CI integrations). The endpoint returns a flat
  # `statuses` array per-context with state ∈ {success,pending,failure,error}.
  local statuses statuses_read=ok
  statuses=$(gh api "repos/$owner_repo/commits/$head_sha/status" --jq '.statuses // []' 2>/dev/null) \
    || { statuses='[]'; statuses_read=failed; }

  local pending failed completed pending_legacy failed_legacy
  pending=$(echo "$checks"   | jq '[.[] | select(.status != "completed")] | length')
  completed=$(echo "$checks" | jq '[.[] | select(.status == "completed")] | length')
  failed=$(echo "$checks"    | jq '[.[]
    | select(.status == "completed")
    | select((.conclusion // "") as $c
        | $c != "success" and $c != "skipped" and $c != "neutral")
    ] | length')
  pending_legacy=$(echo "$statuses" | jq '[.[] | select(.state == "pending")] | length')
  failed_legacy=$(echo "$statuses"  | jq '[.[] | select(.state == "failure" or .state == "error")] | length')

  if [ "$pending" -gt 0 ] || [ "$pending_legacy" -gt 0 ]; then
    echo "ci: $((pending + pending_legacy)) check(s) still pending on $head_sha" >&2
    return 1
  fi
  if [ "$failed" -gt 0 ] || [ "$failed_legacy" -gt 0 ]; then
    local fail_names legacy_names
    fail_names=$(echo "$checks"      | jq -r '[.[]
      | select(.status == "completed")
      | select((.conclusion // "") as $c
          | $c != "success" and $c != "skipped" and $c != "neutral")
      | .name] | join(", ")')
    legacy_names=$(echo "$statuses"  | jq -r '[.[] | select(.state == "failure" or .state == "error") | .context] | join(", ")')
    local all=""
    [ -n "$fail_names"   ] && all="$fail_names"
    [ -n "$legacy_names" ] && all="${all:+$all, }$legacy_names"
    echo "ci: failing check(s) on $head_sha: $all" >&2
    return 1
  fi
  # A value the test below cannot compare ("abc", 1.5) used to make the test fail and
  # the function fall through to "green" with no check at all; it counts as 1 now.
  local min_required
  min_required=$(_merge_gate_number merge_min_required_checks 1) || true
  local total_completed=$((completed))
  # Count completed legacy statuses too (any non-pending state counts).
  total_completed=$((total_completed + $(echo "$statuses" | jq '[.[] | select(.state != "pending")] | length')))
  if [ "$total_completed" -lt "$min_required" ]; then
    # Past the pending check above, zero completed means nothing at all on the head.
    # Both lists must have been read; a head commit time that cannot be read keeps
    # the "only 0 completed" line (not yet).
    local grace age
    if [ "$total_completed" = 0 ] && [ "$statuses_read" = ok ]; then
      grace=$(_merge_gate_number merge_ci_start_grace_seconds 1800) || true
      if age=$(_pr_head_commit_age "$owner_repo" "$head_sha") && [ "$age" -ge "$grace" ]; then
        # No age in the line: the merge stage posts a new PR comment when a line changes.
        echo "ci: no check run and no status on $head_sha, and its commit is older than the CI start grace (agents.merge_ci_start_grace_seconds: $grace) — no CI started for this head" >&2
        return 1
      fi
    fi
    echo "ci: only $total_completed completed check(s) on $head_sha (require >= $min_required)" >&2
    return 1
  fi
  return 0
}

# _merge_gate_number <agents key> <default>: prints the whole number the merge gate uses
# for .agents.<key>, and returns 1 when the value was not a plain whole number, so a
# caller can warn. One rule, which bureau-doctor.py (gate_number) applies the same way:
#   absent or null                  → <default>
#   a whole number from 0           → itself
#   a string that reads as a number → that number (warn): ASCII blanks around it and one
#                                     leading "+" are dropped, then it must be digits
#                                     with an optional fraction and exponent (" 2",
#                                     "+2", "2.0", "1e3")
#   a fraction (1.5)                → rounded up, never below what was written (warn)
#   above 9999999                   → 9999999, still never below a count (warn)
#   negative, any other string, a boolean, an array or an object → <default> (warn)
# Before, a string made the gate's count test fail and let the CI check pass with no
# check at all, and a negative number counted as "no check needed". pr_ci_is_green
# cannot warn itself: its stderr is its gate line (merge-pipeline.sh warns before it
# runs the gate). The cap keeps the number inside shell arithmetic.
_merge_gate_number() {
  local filter out value flag
  filter='.agents.KEY as $v
    | ($v | if type == "number" then .
            elif type == "string" then
              (sub("\\A[ \\t\\n\\r\\f\\x0b]+"; "") | sub("[ \\t\\n\\r\\f\\x0b]+\\z"; "") | ltrimstr("+")
               | if test("\\A[0-9]+(\\.[0-9]+)?([eE][+-]?[0-9]+)?\\z") then tonumber else null end)
            else null end) as $n
    | if $v == null then "default ok"
      elif $n == null or $n < 0 then "default warn"
      else ([($n | ceil), 9999999] | min | if . == 0 then 0 else . end | tostring)
           + (if ($v | type) == "string" or $n != ($n | floor) or $n > 9999999 then " warn" else " ok" end)
      end'
  out=$(bureau_get "${filter//KEY/$1}" 2>/dev/null) || out="default warn"
  value=${out% *}; flag=${out##* }
  case "$value" in
    default) value="$2" ;;
    ''|*[!0-9]*) value="$2"; flag=warn ;;
  esac
  printf '%s' "$((10#$value))"
  [ "$flag" = ok ]
}

# _pr_head_commit_age <owner/repo> <sha>: seconds since the commit's committer
# time on GitHub (negative for a time in the future). Fails (prints nothing) when
# the time cannot be read.
_pr_head_commit_age() {
  local when
  when=$(gh api "repos/$1/git/commits/$2" --jq '.committer.date' 2>/dev/null) || return 1
  when=$(printf '%s' "$when" | jq -Rr 'fromdateiso8601 | floor' 2>/dev/null) || return 1
  case "$when" in ''|*[!0-9]*) return 1 ;; esac
  printf '%s' "$(( $(date +%s) - when ))"
}

# pr_base_is_current <pr-number>
# Returns 0 iff the PR's base ref OID equals the current HEAD of its base
# branch on origin. Returns 1 with a stderr diagnostic and the "behind by N"
# count if not. Catches the stale-base race that mergeStateStatus's async
# cache misses.
pr_base_is_current() {
  local pr="$1"
  local owner_repo base_ref base_pr_sha base_head_sha behind
  owner_repo=$(_bureau_gh_owner_repo)
  [ -z "$owner_repo" ] && { echo "base: cannot resolve owner/repo" >&2; return 1; }
  base_ref=$(gh pr view "$pr" --json baseRefName --jq .baseRefName 2>/dev/null || echo "")
  base_pr_sha=$(gh pr view "$pr" --json baseRefOid --jq .baseRefOid 2>/dev/null || echo "")
  [ -z "$base_ref" ] || [ -z "$base_pr_sha" ] && {
    echo "base: cannot resolve baseRefName/baseRefOid for #$pr" >&2
    return 1
  }
  base_head_sha=$(gh api "repos/$owner_repo/branches/$base_ref" --jq .commit.sha 2>/dev/null || echo "")
  [ -z "$base_head_sha" ] && {
    echo "base: cannot resolve $base_ref HEAD on origin" >&2
    return 1
  }
  if [ "$base_pr_sha" = "$base_head_sha" ]; then
    return 0
  fi
  behind=$(gh api "repos/$owner_repo/compare/$base_pr_sha...$base_head_sha" --jq .ahead_by 2>/dev/null || echo "?")
  echo "base: PR #$pr is $behind commit(s) behind $base_ref (PR base=$base_pr_sha, $base_ref=$base_head_sha)" >&2
  return 1
}

# ── Observability helpers (EXP-414) ────────────────────────────────
# Shared throttle: returns 0 if the event for $key fired within the last
# $window_sec seconds (caller should suppress), 1 if not seen recently
# (caller should fire AND will record). On the "fire" path, the caller calls
# _throttle_record. Two-step so callers can decide what to log on suppression.
#
# Used by alert_telegram and merge_origin_main_or_abort to keep retry loops
# from spamming Telegram or Linear.
#
# The log belongs to one repository: <git common dir>/bureau/alert-throttle.log
# of the repository that holds .bureau.json (bureau_common_dir, as for the
# needs-human holds), shared by its worktrees. One /tmp/bureau-alerts.log for
# every installation on the host let the same key (an issue, a pipeline and a
# code, or `none` for a failed pick) in one repository silence the alert of
# another for an hour. BUREAU_ALERT_THROTTLE_FILE names another file (tests).
# Only when no git directory resolves does the log stay in /tmp, and the key
# then starts with the repository's path.
#
# _throttle_where — sets _throttle_file and _throttle_prefix, which the caller
# declares local.
_throttle_where() {
  local common base
  _throttle_prefix=""
  if [ -n "${BUREAU_ALERT_THROTTLE_FILE:-}" ]; then
    _throttle_file="$BUREAU_ALERT_THROTTLE_FILE"
  elif common=$(bureau_common_dir 2>/dev/null); then
    _throttle_file="$common/bureau/alert-throttle.log"
  else
    _throttle_file="/tmp/bureau-alerts.log"
    base="$PWD"
    [ -n "${BUREAU_CONFIG:-}" ] && base=$(dirname "$BUREAU_CONFIG")
    _throttle_prefix="$(cd "$base" 2>/dev/null && pwd || printf '%s' "$base")|"
  fi
}

_throttle_should_suppress() {
  local key="$1" window_sec="${2:-3600}" _throttle_file _throttle_prefix
  _throttle_where
  key="$_throttle_prefix$key"
  [ ! -f "$_throttle_file" ] && return 1
  local last now delta
  # The key goes in through the environment: awk -v would expand backslashes.
  last=$(_THROTTLE_KEY="$key" awk -F'\t' '$1==ENVIRON["_THROTTLE_KEY"]{print $2}' "$_throttle_file" | tail -1)
  [ -z "$last" ] && return 1
  now=$(date +%s)
  delta=$((now - last))
  [ "$delta" -lt "$window_sec" ]
}

_throttle_record() {
  local key="$1" _throttle_file _throttle_prefix
  _throttle_where
  key="$_throttle_prefix$key"
  local now
  now=$(date +%s)
  # Best effort: when the log cannot be written the next event fires again and
  # the caller carries on (under set -e a failed append would end the stage).
  mkdir -p "$(dirname "$_throttle_file")" 2>/dev/null || true
  printf '%s\t%s\n' "$key" "$now" 2>/dev/null >> "$_throttle_file" || return 0
  # Cap log at 1000 lines so a long-running session doesn't leave an unbounded
  # file behind. Trim is cheap and runs at most once per fired event.
  local lines
  lines=$(wc -l < "$_throttle_file" 2>/dev/null | tr -d ' ' || echo 0)
  if [ "${lines:-0}" -gt 1000 ]; then
    tail -n 500 "$_throttle_file" > "${_throttle_file}.tmp" 2>/dev/null \
      && mv "${_throttle_file}.tmp" "$_throttle_file"
  fi
}

# _bureau_repo_name — the directory name of the main checkout of the repository
# that holds .bureau.json, so an alert says which installation sent it.
_bureau_repo_name() {
  local common
  if common=$(bureau_common_dir 2>/dev/null) && [ "${common##*/}" = .git ]; then
    common="${common%/.git}"
    printf '%s' "${common##*/}"
  elif [ -n "${BUREAU_CONFIG:-}" ]; then
    basename "$(dirname "$BUREAU_CONFIG")"
  else
    basename "$PWD"
  fi
}

# ── Session-usage throttling (EXP-670) ──────────────────────────────
# Pause before a work unit when session usage is near the limit, so unattended
# executor/shepherd runs don't exhaust quota mid-build. GRACEFULLY NO-OPS when no
# usage signal is available — never block work just because the signal is missing
# (adapt to the host project's signals, don't hard-depend on ClaudeWatch).
#
# Signal file contract (JSON): { "pct": <0-100>, "reset_epoch": <unix>,
# "updated_epoch": <unix> }. Read from $BUREAU_USAGE_FILE
# (default ~/.bureau/session-usage.json), then ClaudeWatch
# (~/.claude/claudewatch-usage.json) parsed leniently as a fallback. The
# legacy $BRAINHUGGERS_USAGE_FILE env name is still honoured as a third
# fallback for existing operators. Wire a producer (ClaudeWatch, or a host
# UsageTracker → the usage file) to make it live.
#
# Config (.bureau.json): session.usage_threshold_pct (default 80),
# session.pause_on_stale_data (default false). Staleness window: 5 min.

# Portable "HH:MM for an epoch" (BSD `date -r` / GNU `date -d @`).
_epoch_hm() {
  date -r "$1" +%H:%M 2>/dev/null || date -d "@$1" +%H:%M 2>/dev/null || echo "?"
}

# Echo "pct|reset_epoch|updated_epoch" from the first available signal, else
# nothing. Lenient field aliases cover our file + ClaudeWatch-ish shapes.
bureau_is_paused() {
  local common
  common=$(bureau_common_dir) || return 1
  [ -f "$common/bureau/paused" ]
}

_session_usage_signal() {
  command -v jq >/dev/null 2>&1 || return 0
  local provider="${1:-claude}" f
  local files=()
  if [ "$provider" = codex ]; then
    files=("${BUREAU_CODEX_USAGE_FILE:-}" "${BUREAU_USAGE_FILE:-}")
  else
    files=("${BUREAU_USAGE_FILE:-$HOME/.bureau/session-usage.json}"
           "$HOME/.claude/claudewatch-usage.json"
           "${BRAINHUGGERS_USAGE_FILE:-$HOME/.brainhuggers/session-usage.json}")
  fi
  for f in "${files[@]}"; do
    [ -f "$f" ] || continue
    if [ "$provider" = codex ] && [ "$f" != "${BUREAU_CODEX_USAGE_FILE:-}" ]; then
      jq -e '.provider == "codex"' "$f" >/dev/null 2>&1 || continue
    elif [ "$provider" = claude ]; then
      jq -e '(.provider // "claude") == "claude"' "$f" >/dev/null 2>&1 || continue
    fi
    local out
    out=$(jq -r '
      ( .pct // .usage_pct // .percent // .used_pct // empty ) as $p
      | ( .reset_epoch // .reset_at_epoch // .resets_at_epoch // 0 ) as $r
      | ( .updated_epoch // .timestamp // .updated_at_epoch // 0 ) as $u
      | if $p == null then empty else "\($p)|\($r)|\($u)" end
    ' "$f" 2>/dev/null)
    [ -n "$out" ] && { echo "$out"; return 0; }
  done
  return 0
}

# Pure decision (no sleep, no I/O) — testable in isolation. Echoes one of:
#   "proceed"      below threshold, or stale-and-not-configured-to-pause
#   "pause <sec>"  over threshold; <sec> until reset (bounded to 1h per sleep)
# Args: pct reset_epoch updated_epoch threshold stale_pause now
_throttle_decide() {
  local pct="$1" reset="$2" upd="$3" threshold="$4" stale_pause="$5" now="$6"
  if [ "${upd:-0}" -gt 0 ] && [ "$((now - upd))" -gt 300 ] && [ "$stale_pause" != "true" ]; then
    echo "proceed"; return 0
  fi
  if ! [ "${pct%%.*}" -ge "$threshold" ] 2>/dev/null; then
    echo "proceed"; return 0
  fi
  local sec=300
  [ "${reset:-0}" -gt "$now" ] && sec=$((reset - now + 5))
  [ "$sec" -gt 3600 ] && sec=3600
  echo "pause $sec"
}

# The guard wired before each work unit. Loops decide→sleep until under
# threshold (bounded so a never-clearing signal can't hang the pipeline forever).
session_throttle_guard() {
  [ "${BUREAU_DISABLE_THROTTLE:-0}" = "1" ] && return 0
  command -v jq >/dev/null 2>&1 || return 0
  local cfg="${BUREAU_CONFIG:-.bureau.json}"
  local threshold stale_pause
  threshold=$(jq -r '.session.usage_threshold_pct // 80' "$cfg" 2>/dev/null || echo 80)
  stale_pause=$(jq -r '.session.pause_on_stale_data // false' "$cfg" 2>/dev/null || echo false)

  local provider
  provider=$(resolve_runner_for_stage "${1:-implement}") || return $?
  local iters=0
  while :; do
    local sig
    sig=$(_session_usage_signal "$provider")
    if [ -z "$sig" ]; then
      if [ -z "${_THROTTLE_NOSIGNAL_LOGGED:-}" ]; then
        echo "[throttle] no usage signal — proceeding (configure a usage file for this provider to enable pausing)" >&2
        _THROTTLE_NOSIGNAL_LOGGED=1
      fi
      return 0
    fi
    local pct rest reset upd now decision
    pct="${sig%%|*}"; rest="${sig#*|}"; reset="${rest%%|*}"; upd="${rest##*|}"
    now=$(date +%s)
    decision=$(_throttle_decide "$pct" "$reset" "$upd" "$threshold" "$stale_pause" "$now")
    case "$decision" in
      proceed) return 0 ;;
      pause\ *)
        [ "${BUREAU_THROTTLE_ONCE:-0}" != 1 ] || return 23
        local sec="${decision#pause }"
        echo "[throttle] usage ${pct}% ≥ ${threshold}% — pausing ${sec}s until ~$(_epoch_hm "$((now + sec))")" >&2
        sleep "$sec"
        ;;
    esac
    iters=$((iters + 1))
    if [ "$iters" -ge 24 ]; then
      echo "[throttle] WARN paused 24× without clearing — proceeding to avoid a stuck pipeline" >&2
      return 0
    fi
  done
}

# alert_telegram: best-effort push to a Telegram chat for failure signals.
# Throttled per (issue, pipeline, exit_code) and repository via
# _throttle_should_suppress; the message names the repository (its main
# checkout's directory name, as code so an underscore cannot break Telegram's
# Markdown and drop the alert).
# Requires TELEGRAM_BOT_TOKEN and TELEGRAM_ALERT_CHAT_ID in .env. Silently
# no-ops if either is missing (so dev environments don't break).
#
# Usage: alert_telegram <issue> <pipeline> <exit_code> <message> [log_tail]
alert_telegram() {
  local issue="$1" pipeline="$2" exit_code="$3" message="$4" log_tail="${5:-}"
  if [ "${BUREAU_DRY_RUN:-0}" = "1" ]; then
    echo "[DRY_RUN] alert_telegram $issue $pipeline exit=$exit_code: $message" >&2
    return 0
  fi
  local token="${TELEGRAM_BOT_TOKEN:-}"
  local chat="${TELEGRAM_ALERT_CHAT_ID:-}"
  [ -z "$token" ] && return 0
  [ -z "$chat" ] && return 0

  local throttle_key="alert|$issue|$pipeline|$exit_code"
  _throttle_should_suppress "$throttle_key" 3600 && return 0
  _throttle_record "$throttle_key"

  local body
  body=$(printf '🚨 Bureau pipeline alert\n\nRepo: `%s`\nIssue: %s\nPipeline: %s\nExit: %s\n\n%s' \
    "$(_bureau_repo_name)" "$issue" "$pipeline" "$exit_code" "$message")
  if [ -n "$log_tail" ]; then
    body=$(printf '%s\n\nLog tail:\n```\n%s\n```' "$body" "$log_tail")
  fi
  # The token (it is in the URL), the chat id and the text (its log tail can
  # hold whatever a failing tool printed) reach curl on stdin (-K -), never in
  # its argument list, which `ps` shows; curl starts without the secrets in its
  # environment, and a running `set -x` is off in the subshell.
  ( { set +x; } 2>/dev/null
    config=$(_bureau_curl_config url "https://api.telegram.org/bot${token}/sendMessage"
             _bureau_curl_config data-urlencode "chat_id=${chat}"
             _bureau_curl_config data-urlencode "text=${body}")
    token=""; chat=""
    _bureau_drop_secrets
    curl -s -X POST -K - --data-urlencode "parse_mode=Markdown" <<< "$config"
  ) >/dev/null 2>&1 || true
}

# emit_event: append one structured JSONL line to logs/events.jsonl. Auto-
# injects an ISO-8601 UTC "ts" field. Callers pass any number of key=value
# pairs; empty values are dropped so callers can pass "branch=$maybe_branch"
# unconditionally. Values matching /^-?[0-9]+$/ are stored as JSON numbers so
# exit_code / duration_s can be range-queried by /bureau-learnings.
#
# Silent-on-failure contract: any error (jq missing, disk full, perms) logs to
# stderr and returns 0. Event logging is observability, not correctness — it
# must never wedge a cron-driven pipeline.
#
# Usage:
#   emit_event "event=stage_start" "mode=$MODE" "stage=$script" "issue=$picked"
#   emit_event "event=stage_end"   "mode=$MODE" "stage=$script" "issue=$picked" \
#              "branch=$branch" "exit_code=$ec" "class=$klass" "duration_s=$dur"
emit_event() {
  local repo_dir
  if [ -n "${BUREAU_CONFIG:-}" ] && [ "${BUREAU_CONFIG:0:1}" = "/" ]; then
    repo_dir=$(dirname "$BUREAU_CONFIG")
  else
    repo_dir="$(pwd)"
  fi
  local events_log="$repo_dir/logs/events.jsonl"
  mkdir -p "$(dirname "$events_log")" 2>/dev/null || {
    echo "emit_event: cannot create $(dirname "$events_log")" >&2
    return 0
  }

  local ts
  ts=$(date -u '+%Y-%m-%dT%H:%M:%SZ')

  local args=("--arg" "ts" "$ts")
  local jq_expr='{ts:$ts'
  local pair key val
  for pair in "$@"; do
    key="${pair%%=*}"
    val="${pair#*=}"
    [ -z "$val" ] && continue
    if [[ "$val" =~ ^-?[0-9]+$ ]]; then
      args+=("--argjson" "$key" "$val")
    else
      args+=("--arg" "$key" "$val")
    fi
    jq_expr+=", ${key}:\$${key}"
  done
  jq_expr+='}'

  local json
  json=$(jq -nc "${args[@]}" "$jq_expr" 2>/dev/null) || {
    echo "emit_event: jq construct failed (args: $*)" >&2
    return 0
  }
  printf '%s\n' "$json" >> "$events_log" 2>/dev/null || {
    echo "emit_event: append failed to $events_log" >&2
    return 0
  }
  return 0
}

# log_escalation: append one tab-separated line to logs/escalations.log AND
# emit a matching JSON event via emit_event. Two sinks: the TSV file is
# regex-friendly for operator monitors (tail | grep), the JSONL firehose is
# queryable by /bureau-learnings.
#
# Line format (verbatim, tab-separated):
#   <ts>\tESCALATED\t<issue>\t<pipeline>\tcycle=<n>\treason="<text>"\tpr=<n>\tbranch=<name>
#
# Required acceptance regex:
#   ^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z\s+ESCALATED\s+([A-Z]+-\d+)\s+(\S+)\s+cycle=(\d+)\s+reason="([^"]+)"\s+pr=(\d+)\s+branch=(\S+)$
#
# Append-only, silent-on-failure, returns 0 — observability never wedges a
# cron pipeline. Callers should invoke ONLY after the Linear mutation
# (add_issue_label, comment) succeeded, so a phantom escalation isn't logged
# when the API errored.
#
# Usage: log_escalation <issue> <pipeline> <cycle> <reason> <pr> <branch>
#   cycle:  integer (0 if N/A)
#   reason: free text; embedded double quotes are collapsed to single quotes
#           so the line stays regex-matchable
#   pr:     PR number (0 if no PR)
#   branch: branch name (or "-" if N/A)
#
# A dry run (BUREAU_DRY_RUN=1) writes neither record: add_issue_label only
# logs there and returns 0, so a caller that logs after a "successful" label
# would otherwise record an escalation that never happened. It prints the
# intent on stderr instead, as alert_telegram does.
log_escalation() {
  local issue="$1" pipeline="$2" cycle="$3" reason="$4" pr="$5" branch="$6"
  if [ "${BUREAU_DRY_RUN:-0}" = "1" ]; then
    echo "[DRY_RUN] log_escalation $issue $pipeline cycle=$cycle pr=${pr:-0} branch=${branch:--}: $reason" >&2
    return 0
  fi
  local repo_dir
  if [ -n "${BUREAU_CONFIG:-}" ] && [ "${BUREAU_CONFIG:0:1}" = "/" ]; then
    repo_dir=$(dirname "$BUREAU_CONFIG")
  else
    repo_dir="$(pwd)"
  fi
  local log_file="$repo_dir/logs/escalations.log"
  mkdir -p "$(dirname "$log_file")" 2>/dev/null || return 0

  local ts
  ts=$(date -u '+%Y-%m-%dT%H:%M:%SZ')
  # Scrub embedded double quotes so they don't break the regex contract.
  # bash 3.2 (macOS default) mangles ${var//\"/\'}; tr is portable.
  local reason_clean
  reason_clean=$(printf '%s' "$reason" | tr '"' "'")

  printf '%s\tESCALATED\t%s\t%s\tcycle=%s\treason="%s"\tpr=%s\tbranch=%s\n' \
    "$ts" "$issue" "$pipeline" "$cycle" "$reason_clean" "${pr:-0}" "${branch:--}" \
    >> "$log_file" 2>/dev/null || true

  emit_event "event=escalation" "stage=$pipeline" "issue=$issue" \
    "cycle=$cycle" "reason=$reason_clean" "pr=${pr:-0}" "branch=${branch:--}"
  return 0
}

# Merge origin/main into HEAD so the current pipeline tests against current
# main, not whatever main looked like when the branch was cut. Without this,
# branches cut before recent merges produce phantom-revert diffs in the GitHub
# PR view (the diff shows reverts of commits that landed on main after the
# branch was cut) and may compile/test against a stale base — false-failing
# real fixes. The merge stays local to the pipeline's worktree; pushing it is
# benign (and harmless if the pipeline pushes later) — the PR author still
# rebases before final merge.
#
# Returns 0 if the branch is already up to date with origin/main, or the merge
# succeeded. Returns 1 on conflict — the merge is aborted and a comment is
# posted to the issue. The caller decides routing on conflict, since the
# desired next state varies (qa/code-review → Build; implement is already in
# Build → caller labels needs-human and stays).
#
# Caller must already be checked out on the branch. By default the helper fetches origin
# itself so its contract is self-contained — earlier callers relied on
# queue-loop having pre-fetched, which masked silent staleness when the
# pre-fetch failed (`|| true` in reset_worktree).
# Code review may pass a third argument containing an already fetched commit
# SHA for the PR's actual base. That immutable input is never refetched here.
#
# Usage:
#   if ! merge_origin_main_or_abort "$ISSUE" "QA"; then
#     move_issue "$ISSUE" "$BUREAU_STATE_BUILD"
#     exit 17
#   fi
merge_origin_main_or_abort() {
  local issue="$1" stage_label="$2" base_ref="${3:-origin/main}"
  if [ "$#" -ge 3 ]; then
    [[ "$base_ref" =~ ^[0-9a-f]{40,64}$ ]] && git cat-file -e "$base_ref^{commit}" || return 1
  else
    git fetch origin --quiet || true
  fi
  if git merge-base --is-ancestor "$base_ref" HEAD 2>/dev/null; then
    echo "  Branch is up to date with $base_ref."
    return 0
  fi
  echo "  Branch is behind $base_ref — merging $base_ref..."
  if git merge --no-ff --no-edit "$base_ref"; then
    return 0
  fi
  # Trivial-conflict auto-resolver. Legacy bureau branches predate the
  # `.gitattributes` merge drivers (`merge=ours` for `.specify/feature.json`,
  # `merge=union` for `CLAUDE.md`), so they hit the same conflicts every cycle
  # even though the resolutions are mechanical. Apply the same rules in-line:
  #   - .specify/feature.json    → keep ours (branch's feature_directory)
  #   - CLAUDE.md                → strip conflict markers (union)
  #   - .gitignore               → strip conflict markers (union)
  #   - rust/Cargo.lock          → take theirs (regenerated on next build)
  #   - logs/queue-*.log         → git rm (runtime artifacts that shouldn't be tracked)
  # If, after applying these rules, no unmerged paths remain, complete the
  # merge and return 0. Otherwise abort and fall through to the conflict-comment
  # path below — preserving the existing throttled-comment behavior for real
  # source-code conflicts.
  local conflict_files
  conflict_files=$(git diff --name-only --diff-filter=U 2>/dev/null)
  if [ -n "$conflict_files" ]; then
    local trivial_only=true
    while IFS= read -r f; do
      [ -z "$f" ] && continue
      case "$f" in
        .specify/feature.json) git checkout --ours "$f" 2>/dev/null && git add "$f" ;;
        CLAUDE.md|.gitignore)
          sed -i.bak "/^<<<<<<< HEAD$/d; /^=======$/d; /^>>>>>>> ${base_ref//\//\\/}$/d" "$f" 2>/dev/null \
            && rm -f "$f.bak" && git add "$f" ;;
        rust/Cargo.lock) git checkout --theirs "$f" 2>/dev/null && git add "$f" ;;
        logs/queue-*.log) git rm "$f" >/dev/null 2>&1 ;;
        *) trivial_only=false ;;
      esac
    done <<<"$conflict_files"
    if $trivial_only && [ -z "$(git diff --name-only --diff-filter=U 2>/dev/null)" ]; then
      git -c core.editor=true commit --no-edit >/dev/null 2>&1
      echo "  Auto-resolved trivial conflicts (feature.json/CLAUDE.md/.gitignore/Cargo.lock/logs)."
      return 0
    fi
  fi
  echo "  ERROR: merge of $base_ref has conflicts. Aborting $stage_label."
  git merge --abort 2>/dev/null || true
  # Throttle the conflict comment to once per hour per issue. Without this,
  # an issue parked at needs-human (implement) re-runs the helper every tick
  # and Linear gets a comment-storm. The merge still aborts and returns 1
  # either way — only the user-facing comment is suppressed.
  local throttle_key="merge-conflict|$issue"
  if _throttle_should_suppress "$throttle_key" 3600; then
    echo "  (conflict comment suppressed — already posted within the last hour)"
  else
    _throttle_record "$throttle_key"
    post_comment "$issue" "❌ $stage_label pipeline cannot proceed — branch has conflicts with \`$base_ref\`. Resolve them and re-run."
  fi
  return 1
}

# EXP-491: count issues currently in-flight between Spec (inclusive) and Done
# (exclusive). Used by spec-pipeline as a gate before picking new Triage work
# when BUREAU_MAX_CONCURRENT_ISSUES is non-zero. Issues with parking labels
# (needs-human, the configured linear.labels.needs_human.name as in
# pipeline_pick_next, blocked, wip) are excluded from the count — they're
# already stalled, holding up the cap on them too would deadlock the loop.
#
# What counts is work, not tickets (carried over from installation A,
# EXP-1462): only issues of the configured projects (.linear.projects, as in
# pick_issue), and only issues without children — an epic is a bracket, not
# work, and one on Spec used to hold every new run. Sub-issues count: the old
# `parent: { null: true }` filter counted epics and skipped the work under
# them, and without the project filter the cap counted other projects'
# tickets (16 foreign ones held every spec stage in installation A).
#
# Output: integer count on stdout. A Linear answer that stays unusable returns
# $BUREAU_EXIT_LINEAR_UNUSABLE instead of "0": the count used to fail open, so
# the cap let new work in exactly while Linear was failing. (installation A keeps
# it fail-open; the retry ladder bridges short outages here.)
count_in_flight_issues() {
  # Build a comma-separated list of in-flight state UUIDs. Optional states
  # (qa, copy, merge) are only included when configured.
  local state_ids=""
  for sid in "$BUREAU_STATE_SPEC" "$BUREAU_STATE_SPEC_REVIEW" \
             "$BUREAU_STATE_DESIGN" "$BUREAU_STATE_BUILD" \
             "$BUREAU_STATE_BUILD_REVIEW" \
             "${BUREAU_STATE_QA:-}" "${BUREAU_STATE_COPY:-}" \
             "${BUREAU_STATE_MERGE:-}"; do
    [ -n "$sid" ] && state_ids+="\"$sid\","
  done
  state_ids="${state_ids%,}"  # strip trailing comma
  [ -z "$state_ids" ] && { echo "0"; return 0; }

  local project_clause="" projects_gql
  if [ -n "${BUREAU_PROJECTS:-}" ]; then
    projects_gql=$(printf '%s' "$BUREAU_PROJECTS" | awk -F',' '
      BEGIN{printf "["}
      {for(i=1;i<=NF;i++) if($i!="") printf "%s\"%s\"", (i>1?",":""), $i}
      END{printf "]"}
    ')
    [ "$projects_gql" != "[]" ] && project_clause=$(printf ', project: { id: { in: %s } }' "$projects_gql")
  fi

  # `children(first: 1)`: the rule only asks WHETHER an issue has children.
  local query
  query=$(printf '{ issues(filter: { team: { key: { eq: "%s" } }, state: { id: { in: [%s] } }%s }, first: 250) { nodes { labels { nodes { name } } children(first: 1) { nodes { id } } } } }' \
    "$BUREAU_TEAM_KEY" "$state_ids" "$project_clause")

  local payload
  payload=$(jq -n --arg q "$query" '{query: $q}')

  # Through linear_raw, and no fallback to 0: a count read from an unusable
  # answer used to come out as "0 in flight", which let the spec stage take a
  # new ticket past the cap exactly while Linear was failing.
  # The answer has to carry every list the count reads (the issues, and each
  # node's labels and children): read as `[]`, a missing list counted a parked
  # ticket as work, a missing issue list as "0 in flight". Such an answer is
  # unusable like any other: retried, then 27.
  local answer human
  answer=$(linear_raw "$payload" "$_BUREAU_SHAPE_ISSUE_LABELS"' and all(.data.issues.nodes[]; (.children.nodes | type) == "array")') || return $?
  human=$(bureau_get '.linear.labels.needs_human.name // "needs-human"') || return $?
  printf '%s' "$answer" \
  | jq --arg human "$human" '
    [.data.issues.nodes[]
     | select(
         ([.labels.nodes[].name]
          | map(select(. == "needs-human" or . == $human or . == "blocked" or . == "wip"))
          | length) == 0
       )
     | select((.children.nodes | length) == 0)]
    | length
  '
}

# Why the last reset_worktree refused its worktree over ownership (v3.1.0-rc.2):
# BUREAU_RESET_REFUSAL is unregistered, identity, held-branch or not-owner;
# BUREAU_RESET_REFUSAL_WORKTREE is the worktree the reset was for, and for a held
# branch BUREAU_RESET_REFUSAL_HOLDER / _BRANCH name the checkout that holds it.
# Empty after a reset that went through and after a failure that is no ownership
# refusal (a git command that failed, a signal). bureau-worker.sh reads them to
# leave the halt on the ticket (bureau_reset_refusal_trace below).
BUREAU_RESET_REFUSAL=""
BUREAU_RESET_REFUSAL_WORKTREE=""
BUREAU_RESET_REFUSAL_HOLDER=""
BUREAU_RESET_REFUSAL_BRANCH=""

# A held branch is an ownership conflict. Never detach another checkout.
free_branch_from_other_worktrees() {
  local branch="$1" keep_wt="$2" other
  [ -z "$branch" ] && return 0
  # Git lists physical paths; callers can use a symlinked checkout (including
  # macOS /tmp). Resolve even a not-yet-created worker before comparing owners.
  keep_wt=$(python3 -I -c 'from pathlib import Path; import sys; print(Path(sys.argv[1]).resolve())' "$keep_wt") || return 21
  other=$(git worktree list --porcelain | awk -v b="refs/heads/$branch" -v keep="$keep_wt" '
    /^worktree / { wt=substr($0, 10); next }
    /^branch / { if (substr($0, 8) == b && wt != keep) print wt }')
  if [ -n "$other" ]; then
    BUREAU_RESET_REFUSAL="held-branch"; BUREAU_RESET_REFUSAL_HOLDER="$other"; BUREAU_RESET_REFUSAL_BRANCH="$branch"
    echo "ERROR: branch $branch is held by $other; release or hand off that checkout explicitly." >&2
    return 21
  fi
}

# Only a worker created and registered by Bureau may be reset. Merely residing
# under .worktrees is not ownership; pre-existing directories are rejected.
reset_worktree() {
  local wt="$1" target_script="$2" target_branch="${3:-}" common registry key ref
  BUREAU_RESET_REFUSAL=""; BUREAU_RESET_REFUSAL_HOLDER=""; BUREAU_RESET_REFUSAL_BRANCH=""
  [ "${BUREAU_WORKSPACE_MODE:-current}" = disposable ] || { echo "ERROR: reset requires disposable worker mode" >&2; return 21; }
  [ -n "${BUREAU_RUN_ID:-}" ] || { echo "ERROR: reset requires an ownership claim" >&2; return 21; }
  wt=$(python3 -I -c 'from pathlib import Path; import sys; print(Path(sys.argv[1]).resolve())' "$wt")
  BUREAU_RESET_REFUSAL_WORKTREE="$wt"
  # A check that a signal ended is no refusal: it leaves no trace on the ticket.
  python3 "$BUREAU_RUNTIME" --repo "$REPO_DIR" assert-owner --issue "${BUREAU_CURRENT_ISSUE:?missing issue claim}" --workspace "$wt" --run "$BUREAU_RUN_ID" \
    || { [ "$?" -gt 128 ] || BUREAU_RESET_REFUSAL=not-owner; return 21; }
  common=$(git -C "$REPO_DIR" rev-parse --git-common-dir)
  case "$common" in /*) ;; *) common="$REPO_DIR/$common" ;; esac
  registry="$common/bureau/workers"
  key=$(printf '%s' "$wt" | shasum -a 256 | cut -d' ' -f1)
  if [ -e "$wt" ] && [ ! -f "$registry/$key" ]; then
    BUREAU_RESET_REFUSAL=unregistered
    echo "ERROR: refusing to reset unregistered worktree $wt" >&2; return 21
  fi
  if [ -d "$wt" ]; then
    [ "$(git -C "$wt" rev-parse --absolute-git-dir)" = "$(cat "$registry/$key")" ] || { BUREAU_RESET_REFUSAL=identity; echo "ERROR: worker identity changed" >&2; return 21; }
  fi
  git -C "$REPO_DIR" fetch origin --prune --quiet || return 18
  ref=origin/main
  if [ "$target_script" != spec-pipeline.sh ]; then
    [ -n "$target_branch" ] || return 12
    ref="origin/$target_branch"
    git -C "$REPO_DIR" rev-parse --verify "$ref" >/dev/null || return 12
    free_branch_from_other_worktrees "$target_branch" "$wt" || return $?
  fi
  if [ ! -d "$wt" ]; then
    git -C "$REPO_DIR" worktree add --detach "$wt" "$ref" --quiet || return 21
    mkdir -p "$registry"
    git -C "$wt" rev-parse --absolute-git-dir > "$registry/$key"
  fi
  git -C "$wt" checkout --detach --force "$ref" --quiet || return 21
  git -C "$wt" reset --hard "$ref" --quiet || return 21
  git -C "$wt" clean -fdx --quiet || return 21
  if [ "$target_script" != spec-pipeline.sh ]; then
    git -C "$wt" checkout -B "$target_branch" "$ref" --quiet || return 21
  fi
  # A registered, reset worker: whatever preserved it before and any halt left
  # for it are settled (bureau_preserve_note, bureau_ownership_trace).
  rm -f "$common/bureau/preserved/$key.json" "$common/bureau/ownership-halts/"*".$key" 2>/dev/null || true
  # `clean -fdx` above removed every ignored path; put the configured links back.
  bureau_link_worktree_paths "$wt"
}

# ── Ownership halts leave a trace (v3.1.0-rc.2) ─────────────────────────
# An exit 21 over ownership used to end the run with a line on stderr and
# nothing on the ticket: the ticket stayed in its state with lane-2, and a queue
# picked it again on every tick (pilot EXP-1545: an interrupted run's worktree,
# released but not dropped). The halt now sets needs-human (mark_needs_human,
# with its local hold when the label cannot be written) and posts one comment
# that names the worktree and the way back. A cancelled run writes nothing.

# bureau_preserve_note <worktree> <issue> <reason> — record why <worktree> lost
# its disposable-worker registration (reason: interrupted, unfinished,
# review-checkpoint): the run, the ticket, its branch. reset_worktree's refusal
# reads it to name the owner; a reset that registers the worktree again removes
# it. bureau-runtime.py writes the same record for an interrupted run.
bureau_preserve_note() {
  local wt="$1" issue="$2" reason="$3" common key dir branch
  wt=$(python3 -I -c 'from pathlib import Path; import sys; print(Path(sys.argv[1]).resolve())' "$wt") || return 1
  common=$(bureau_common_dir) || return 1
  key=$(printf '%s' "$wt" | shasum -a 256 | cut -d' ' -f1)
  dir="$common/bureau/preserved"
  branch=$(git -C "$wt" branch --show-current 2>/dev/null || true)
  mkdir -p "$dir" || return 1
  jq -n --arg run "${BUREAU_RUN_ID:-}" --arg issue "$issue" --arg ws "$wt" --arg branch "$branch" --arg reason "$reason" \
    '{run_id: $run, issue: $issue, workspace: $ws, branch: $branch, reason: $reason}' > "$dir/.$key.$$" \
    && mv -f "$dir/.$key.$$" "$dir/$key.json"
}

# _bureau_physical <dir> — <dir> with symlinks resolved; empty when it is empty or gone.
_bureau_physical() {
  [ -n "${1:-}" ] || return 0
  (cd "$1" 2>/dev/null && pwd -P) || true
}

# _bureau_shq <word> — <word> quoted for a shell command line in a comment.
_bureau_shq() {
  case "$1" in
    *"'"*) printf '%q' "$1" ;;
    *[!A-Za-z0-9_./:@%+=-]*|'') printf "'%s'" "$1" ;;
    *) printf '%s' "$1" ;;
  esac
}

# bureau_ownership_trace <issue> <stage> <worktree> <body> — needs-human on
# <issue> (mark_needs_human: the label, or a local hold and an alert when it
# cannot be written) and <body> as one comment. The comment goes out once per
# ticket and worktree ($common/bureau/ownership-halts/<issue>.<key>, removed
# when reset_worktree registers that worktree again): a label a human removed
# without the fix is set again on the next pick, the comment is not repeated.
# Writes nothing for a run the runtime marked interrupted. Returns 0 unless the
# ticket identifier is invalid or the git directory cannot be found.
bureau_ownership_trace() {
  local issue="$1" stage="$2" wt="$3" body="$4" common key dir label_rc=0
  [[ "$issue" =~ ^[A-Z][A-Z0-9_]*-[0-9]+$ ]] || { echo "bureau: not a ticket identifier: $issue" >&2; return 1; }
  common=$(bureau_common_dir) || return 1
  if [ -n "${BUREAU_RUN_ID:-}" ] \
     && jq -e '.interrupted == true' "$common/bureau/processes/$BUREAU_RUN_ID.json" >/dev/null 2>&1; then
    echo "  run $BUREAU_RUN_ID was cancelled — nothing written to $issue" >&2
    return 0
  fi
  key=$(printf '%s' "$wt" | shasum -a 256 | cut -d' ' -f1)
  dir="$common/bureau/ownership-halts"
  # A subshell: a Linear that stayed unusable ends mark_needs_human with 27
  # (the hold is on disk by then), and the halt keeps its own 21.
  ( mark_needs_human "$issue" "$stage" 21 ) || label_rc=$?
  if [ -f "$dir/$issue.$key" ]; then
    echo "  the halt comment for $wt is already on $issue — not posted again" >&2
    return 0
  fi
  if ( [ "$label_rc" != "$BUREAU_EXIT_LINEAR_UNUSABLE" ] || export _BUREAU_LINEAR_SINGLE_ATTEMPT=1
       post_comment "$issue" "<!-- bureau-ownership-halt: $key -->
$body" ); then
    [ "${BUREAU_DRY_RUN:-0}" = 1 ] || { mkdir -p "$dir" && : > "$dir/$issue.$key"; } || true
  else
    echo "  could not post the halt comment on $issue — the next halt tries again" >&2
  fi
  return 0
}

# bureau_reset_refusal_trace <issue> <stage> — after reset_worktree refused its
# worktree over ownership (BUREAU_RESET_REFUSAL): print why and the way back,
# and leave both on a ticket (bureau_ownership_trace). The ticket is the one
# whose run preserved the worktree when bureau_preserve_note recorded it, else
# <issue>. Without a record the trace goes to one ticket per worktree: a queue
# that shares its worktree between tickets labels the first ticket it hit it
# with, not every ticket it picks after.
bureau_reset_refusal_trace() {
  local issue="$1" stage="$2" wt="$BUREAU_RESET_REFUSAL_WORKTREE" repo="${REPO_DIR:-$PWD}" \
        common key note="" owner="" run="" reason="" branch="" target why steps drop marker traced \
        kind wt_common wt_gitdir own_common own text cmds alt hint
  [ -n "$BUREAU_RESET_REFUSAL" ] && [ -n "$wt" ] || return 0
  common=$(bureau_common_dir) || return 1
  key=$(printf '%s' "$wt" | shasum -a 256 | cut -d' ' -f1)
  [ ! -f "$common/bureau/preserved/$key.json" ] || note="$common/bureau/preserved/$key.json"
  if [ -n "$note" ] && [ "$BUREAU_RESET_REFUSAL" = unregistered ]; then
    owner=$(jq -r '.issue // empty' "$note" 2>/dev/null || true)
    run=$(jq -r '.run_id // empty' "$note" 2>/dev/null || true)
    reason=$(jq -r '.reason // empty' "$note" 2>/dev/null || true)
    [[ "$owner" =~ ^[A-Z][A-Z0-9_]*-[0-9]+$ ]] || owner=""
  fi
  target="${owner:-$issue}"
  steps="To resume, from \`$repo\`:"
  case "$BUREAU_RESET_REFUSAL" in
    unregistered|identity)
      # Where the worktree stands: a linked worktree of this repository (dropped
      # with git), the main checkout (never reset, never dropped), or anything
      # else (a plain directory, another repository). A plain directory under
      # .worktrees/ answers for the main checkout around it, hence the toplevel.
      kind=other
      if [ "$(_bureau_physical "$(git -C "$wt" rev-parse --show-toplevel 2>/dev/null)")" = "$(_bureau_physical "$wt")" ]; then
        wt_common=$(_bureau_physical "$(git -C "$wt" rev-parse --path-format=absolute --git-common-dir 2>/dev/null)")
        wt_gitdir=$(_bureau_physical "$(git -C "$wt" rev-parse --absolute-git-dir 2>/dev/null)")
        own_common=$(_bureau_physical "$(git -C "$repo" rev-parse --path-format=absolute --git-common-dir 2>/dev/null)")
        if [ -n "$wt_common" ] && [ "$wt_common" = "$own_common" ]; then
          if [ "$wt_gitdir" = "$wt_common" ]; then kind=main; else kind=linked; fi
        fi
      fi
      if [ "$kind" = main ]; then
        why="the worktree \`$wt\` is the repository's main checkout, which Bureau never resets or drops."
      elif [ "$BUREAU_RESET_REFUSAL" = unregistered ]; then
        why="the worktree \`$wt\` is not registered as a disposable Bureau worker, so Bureau will not reset it: it may hold work nobody saved."
        case "$reason" in
          interrupted) why="$why It was preserved when run \`$run\` of ${owner:-its ticket} was interrupted." ;;
          unfinished) why="$why It was preserved with the unfinished work of run \`$run\` of ${owner:-its ticket}." ;;
          review-checkpoint) why="$why It holds the checkpoint of a review of ${owner:-its ticket} that stopped before merge (run \`$run\`)." ;;
        esac
      else
        why="the worktree \`$wt\` is registered as a Bureau worker, but it now belongs to another Git checkout, so Bureau will not reset it."
      fi
      alt="Or keep the worktree and rerun with a new one instead (\`shepherd.sh --worktree DIR\`): a rerun on this worktree stops with exit 21 until it is dropped."
      case "$kind" in
        main)
          steps="$steps
1. Rerun with a worktree of its own (\`shepherd.sh --worktree DIR\` with DIR under \`.worktrees/\`, or no \`--worktree\` at all)." ;;
        other)
          steps="$steps
1. It is no worktree of this repository: save anything you want from it, then move it away, or delete it once nothing in it is needed.
   $alt" ;;
        linked)
          # The branch: `git branch -D` only for one that was never pushed and
          # has no commits of its own (the spec stage's fresh branch, whose name
          # a rerun needs again). Commits on no remote are pushed, never deleted
          # (an implement stage whose final push failed leaves finished work);
          # a branch origin has needs no deletion (the rerun's checkout -B).
          branch=$(git -C "$wt" branch --show-current 2>/dev/null || true)
          drop="git worktree remove --force $(_bureau_shq "$wt")"
          text="Save anything you want from the worktree, then drop it:"; cmds="$drop"; hint="$alt"
          if [ -z "$branch" ] || [ "$branch" = main ] || [ "$branch" = master ]; then
            :
          elif git -C "$wt" rev-parse --verify --quiet "refs/remotes/origin/$branch" >/dev/null 2>&1; then
            own=$(git -C "$wt" rev-list --count "refs/remotes/origin/$branch..HEAD" 2>/dev/null || echo 1)
            if [ "$own" = 0 ]; then
              hint="Its branch \`$branch\` is on origin; deleting it is not needed: the rerun's \`git checkout -B\` resets it. $alt"
            else
              text="Its branch \`$branch\` has $own commit(s) that are not on origin/$branch: push them (or keep them on another branch), then save anything else you want from the worktree and drop it:"
              cmds="git push origin $(_bureau_shq "$branch")
$drop"
              hint="Deleting \`$branch\` is not needed: the rerun's \`git checkout -B\` resets it to origin/$branch. $alt"
            fi
          else
            own=$(git -C "$wt" rev-list --count HEAD --not --remotes 2>/dev/null || echo 1)
            if [ "$own" = 0 ]; then
              text="Save anything you want from the worktree, then drop it and its local branch \`$branch\`, which was never pushed and has no commits of its own:"
              cmds="$drop
git branch -D $(_bureau_shq "$branch")"
            else
              # The rerun creates a branch of this name again: kept, renamed.
              text="Its local branch \`$branch\` has $own commit(s) that are on no remote: push them, save anything else you want from the worktree and drop it, then keep the branch as \`$branch-saved\`, since the rerun creates \`$branch\` again:"
              cmds="git push -u origin $(_bureau_shq "$branch")
$drop
git branch -m $(_bureau_shq "$branch") $(_bureau_shq "$branch-saved")"
            fi
          fi
          steps="$steps
1. $text
   \`\`\`sh
$(printf '%s\n' "$cmds" | sed 's/^/   /')
   \`\`\`
   $hint" ;;
      esac
      ;;
    held-branch)
      why="branch \`$BUREAU_RESET_REFUSAL_BRANCH\` is checked out in another worktree, \`$BUREAU_RESET_REFUSAL_HOLDER\`, so Bureau will not take it for \`$wt\`."
      steps="$steps
1. Finish or save the work in \`$BUREAU_RESET_REFUSAL_HOLDER\`, then free the branch there (or drop that checkout once nothing in it is needed):
   \`\`\`sh
   git -C $(_bureau_shq "$BUREAU_RESET_REFUSAL_HOLDER") switch --detach
   \`\`\`"
      ;;
    not-owner)
      why="this run (\`${BUREAU_RUN_ID:-}\`) no longer owns $issue and the worktree \`$wt\`, so it stopped before touching the worktree."
      steps="$steps
1. Find the run that holds them (\`python3 scripts/bureau-runtime.py status\`); release it only once it has stopped."
      ;;
    *) return 0 ;;
  esac
  steps="$steps
2. Remove \`needs-human\` from $target and rerun."
  printf '%s\n%s\n' "$why" "$steps" | sed 's/^/  /' >&2
  # Without a record, one ticket per worktree carries the halt.
  if [ -z "$owner" ] && [ "$BUREAU_RESET_REFUSAL" != held-branch ]; then
    for marker in "$common/bureau/ownership-halts/"*".$key"; do
      [ -f "$marker" ] || continue
      traced=${marker##*/}; traced=${traced%".$key"}
      if [ "$traced" != "$target" ]; then
        echo "  the halt for this worktree is on $traced — nothing written to $target" >&2
        return 0
      fi
    done
  fi
  bureau_ownership_trace "$target" "$stage" "$wt" "🛑 Bureau halt (exit 21, ownership-conflict) in \`$stage\`: $why

$steps"
}
# ── End of ownership halts ──────────────────────────────────────────────

# restore_worktree_deps <worktree> — put node_modules back after reset_worktree's
# `clean -fdx`, for an npm project. Returns 0 when there is nothing to do or the
# dependencies are in place, 24 (environment-blocked) when they could not be
# restored. The stages call it after their own checkout and merge of
# origin/main (both can change the manifests) as `|| exit 24`.
#
# Carried over from installation B (EXP-1375). `clean -fdx` removes ignored files,
# node_modules included, and nothing installed them again: the review stage's
# build check ran without dependencies every time, the build was red, and the
# red build turned four unanimous APPROVEs into REQUEST_CHANGES. Only for npm
# (package.json AND package-lock.json); every other project returns 0 at once.
#
# Security, each point a review finding in installation B:
#   - `npm ci --ignore-scripts`, never without: otherwise the lifecycle scripts
#     of the packages a PR lists run before anyone has reviewed the PR, on a
#     machine with .env access.
#   - A node_modules the branch tracks, or one that is a symlink, is discarded:
#     `clean -fdx` does not remove tracked files, and a symlink could point the
#     build at PR-supplied binaries. The `! -L` in the stamp check is the guard
#     that carries; the removals are depth.
#   - The stamp (SHA-256 of package.json + package-lock.json) lives in the
#     shared .git, where no PR content can write, and so does the npm log.
#
# Correctness:
#   - Fresh, not just present: the stamp is compared on every call, so a call
#     after the final checkout picks up a lock file the branch changed.
#   - Clone only on identical manifests (from the main checkout, copy-on-write
#     where APFS allows); otherwise npm ci. Never a symlink to the main
#     checkout's node_modules (Turbopack rejects it).
#   - Mounted atomically: built next to the target, then renamed. A half-filled
#     node_modules made builds fail with internal errors instead of a clear one.
#   - Never in the main checkout itself: there node_modules is the clone source.
#   - Paths come from `git rev-parse --git-common-dir`, not $REPO_DIR: a stage
#     sets REPO_DIR to its own worktree.
#   - Why 24 and not 0: with 0 the stage ran on, built red, and a registry
#     outage became a REQUEST_CHANGES on code that was never the problem.
restore_worktree_deps() {
  local wt="$1"
  local common main_repo stampdir stamp tmp want have src_want tracked log attempt reason

  [ -f "$wt/package.json" ] || return 0
  [ -f "$wt/package-lock.json" ] || return 0
  command -v shasum >/dev/null 2>&1 || return 0

  common=$(git -C "$wt" rev-parse --git-common-dir 2>/dev/null) || return 0
  case "$common" in
    /*) : ;;
    *)  common=$(cd "$wt" && cd "$common" 2>/dev/null && pwd) || return 0 ;;
  esac
  [ -d "$common" ] || return 0
  main_repo=$(dirname "$common")
  [ "$main_repo" = "$wt" ] && return 0
  stampdir="$common/bureau-deps"
  stamp="$stampdir/$(printf "%s" "$wt" | shasum -a 256 | cut -d" " -f1)"
  tmp="$wt/.nm.tmp"

  # Both manifests: package.json carries overrides and resolutions the lock
  # file does not show.
  want=$(cat "$wt/package.json" "$wt/package-lock.json" 2>/dev/null | shasum -a 256 | cut -d" " -f1)
  [ -n "$want" ] || return 0

  # `grep -c`, not `grep -q`: -q closes the pipe early, `git ls-files` gets
  # SIGPIPE, and under pipefail the condition is always false. `$` is needed
  # for a tracked symlink, which ls-files lists without a slash. Case-insensitive
  # because macOS folds NODE_MODULES onto the same path.
  tracked=$(git -C "$wt" ls-files 2>/dev/null | grep -ciE '^node_modules(/|$)' || true)
  if [ "${tracked:-0}" -gt 0 ]; then
    echo "  WARNING: node_modules is tracked in the branch — discarded (not trusted)."
    rm -rf "$wt/node_modules"
  fi
  mkdir -p "$stampdir" 2>/dev/null
  if [ -L "$wt/node_modules" ]; then
    echo "  WARNING: node_modules is a symlink — discarded (not trusted)."
    rm -f "$wt/node_modules"
  fi

  if [ -d "$wt/node_modules" ] && [ ! -L "$wt/node_modules" ] && [ -f "$stamp" ]; then
    have=$(cat "$stamp" 2>/dev/null)
    if [ "$have" = "$want" ]; then
      echo "  Dependencies: unchanged, skipped"
      return 0
    fi
  fi

  rm -rf "$tmp"

  if [ -d "$main_repo/node_modules" ] && [ -f "$main_repo/package.json" ] && [ -f "$main_repo/package-lock.json" ]; then
    src_want=$(cat "$main_repo/package.json" "$main_repo/package-lock.json" 2>/dev/null | shasum -a 256 | cut -d" " -f1)
    if [ "$src_want" = "$want" ]; then
      if cp -Rc "$main_repo/node_modules" "$tmp" 2>/dev/null || cp -R "$main_repo/node_modules" "$tmp" 2>/dev/null; then
        rm -rf "$wt/node_modules"
        if mv "$tmp" "$wt/node_modules" 2>/dev/null; then
          printf "%s" "$want" > "$stamp" 2>/dev/null
          echo "  Dependencies: cloned from the main checkout"
          return 0
        fi
      fi
      rm -rf "$tmp"
    fi
  fi

  if command -v npm >/dev/null 2>&1; then
    # `npm ci` reads package.json from its working directory; a --prefix alone
    # does not do that.
    mkdir -p "$tmp"
    cp "$wt/package.json" "$wt/package-lock.json" "$tmp/" 2>/dev/null
    log="$stamp.npm-ci.log"
    attempt=1
    while [ "$attempt" -le 2 ]; do
      # Reset per attempt, so message and log describe the same, last attempt.
      reason="npm ci"
      if ( cd "$tmp" && npm ci --ignore-scripts --no-audit --no-fund ) >"$log" 2>&1 \
         && [ -d "$tmp/node_modules" ]; then
        rm -rf "$wt/node_modules"
        if mv "$tmp/node_modules" "$wt/node_modules" 2>>"$log"; then
          printf "%s" "$want" > "$stamp" 2>/dev/null
          rm -rf "$tmp"
          echo "  Dependencies: installed with npm ci --ignore-scripts"
          return 0
        fi
        reason="mounting node_modules"
      fi
      # One retry: the usual cause is a registry hiccup, gone the second time.
      attempt=$((attempt + 1))
    done
    echo "  $reason failed ($((attempt - 1)) attempts) — last lines:"
    tail -n 20 "$log" 2>/dev/null | sed "s/^/    | /"
    rm -rf "$tmp"
  fi

  # A node_modules left here is stale by the stamp: remove it rather than build
  # green on the wrong dependencies.
  rm -rf "$wt/node_modules"
  echo "  Dependencies: COULD NOT be restored — stopping (environment-blocked)"
  return 24
}

# ── Worktree links (repo.worktree_links) ──────────────────────────────
# bureau_link_worktree_paths <worktree> — after reset_worktree's `clean -fdx`,
# symlink each path listed in `repo.worktree_links` (for example `[".venv"]`)
# from the main checkout into the stage worktree. `clean -fdx` removes every
# ignored path, a Python virtualenv included, and the agents' own commands
# (`.venv/bin/python -m pytest` from a spec) then fail in every stage. This is
# the Python counterpart of restore_worktree_deps, carried over from
# installation A, which linked `.venv` by hand after the reset (EXP-799).
# Nothing is configured by default, and then nothing happens.
#
# A link is only made when it cannot hurt; otherwise the path is skipped with
# one warning line, and the stage runs on as it would without the link:
#   - the entry is a plain relative path: not absolute, no `.`, `..` or empty
#     component, not inside `.git`;
#   - it is not a .env file: no component starts with `.env` (any case), of the
#     entry or of its resolved path in the main checkout (the doctor's
#     env_path), and, for a directory, no name anywhere below it does — a search
#     that fails refuses the link;
#   - the branch tracks nothing at that path (a tracked path is the PR's own);
#   - its parent directory exists in the worktree and resolves inside it (a
#     tracked symlink as parent would put the link outside the worktree);
#   - the path exists in the main checkout;
#   - the worktree's gitignore ignores it AS A SYMLINK: an untracked link makes
#     the worktree dirty, the worker then preserves it and the next reset stops
#     with 21. A directory-only pattern (`.venv/`) does not match a symlink;
#     list `.venv` without the slash. Asked before the link exists, git answers
#     for a non-directory, which is what the link will be;
#   - nothing but an older link sits at that path: a link is replaced, a real
#     file or directory is never touched.
# The next `clean -fdx` removes the link, never its target. The link points at
# the main checkout's own copy, so what an agent installs into it changes the
# main checkout too. Never in the main checkout itself. Always returns 0.
bureau_link_worktree_paths() {
  local wt="$1" cfg="${BUREAU_CONFIG:-}" list line common main wt_phys
  [ -n "$cfg" ] && [ -f "$cfg" ] || return 0
  # One line per entry: "=path" for a usable string, "!…" for anything else.
  list=$(jq -r '
    (.repo.worktree_links // []) as $l
    | if ($l | type) != "array" then "!type"
      else $l[] | if type != "string" then "!entry"
                  elif test("\n") then "!entry"
                  else "=" + . end
      end' "$cfg" 2>/dev/null) || {
    echo "  WARNING: repo.worktree_links could not be read — no links made."
    return 0
  }
  [ -n "$list" ] || return 0
  wt_phys=$(cd "$wt" 2>/dev/null && pwd -P) || return 0
  common=$(git -C "$wt" rev-parse --git-common-dir 2>/dev/null) || return 0
  case "$common" in /*) ;; *) common="$wt/$common" ;; esac
  # A bare repository has no main checkout; its parent directory is not one.
  if [ "$(git --git-dir="$common" rev-parse --is-bare-repository 2>/dev/null)" = true ]; then
    echo "  WARNING: repo.worktree_links: the repository is bare, so there is no main checkout to link from — no links made."
    return 0
  fi
  main=$(cd "$common/.." 2>/dev/null && pwd -P) || return 0
  # A git directory kept elsewhere (--separate-git-dir): its parent is not the main checkout.
  if [ "$(cd "$common" 2>/dev/null && pwd -P)" != "$main/.git" ]; then
    echo "  WARNING: repo.worktree_links: the git directory is not inside the main checkout (--separate-git-dir), so there is no main checkout to link from — no links made."
    return 0
  fi
  [ "$main" = "$wt_phys" ] && return 0
  while IFS= read -r line; do
    case "$line" in
      '!type')  echo "  WARNING: repo.worktree_links must be a list of paths — no links made."; return 0 ;;
      '!entry') echo "  WARNING: repo.worktree_links: an entry is not a one-line string — skipped." ;;
      =*)       _bureau_link_worktree_path "$wt_phys" "$main" "${line#=}" ;;
    esac
  done <<EOF
$list
EOF
  return 0
}

_bureau_link_worktree_path() {
  local wt="$1" main="$2" p="$3" parent parent_phys tracked target envfile
  while :; do case "$p" in */) p="${p%/}" ;; *) break ;; esac; done
  case "$p" in
    ''|/*) echo "  WARNING: worktree link '$3' skipped: not a relative path."; return 0 ;;
  esac
  case "/$(printf '%s' "$p" | tr '[:upper:]' '[:lower:]')/" in
    */../*|*/./*|*//*|*/.git/*)
      echo "  WARNING: worktree link '$3' skipped: '.', '..', '.git' and empty components are not allowed."; return 0 ;;
  esac
  # Never a .env file: any component starting with `.env` in any case (.env,
  # .env.local, .envrc) — of the entry, or of its fully resolved path in the
  # main checkout (`alias/key` with `alias -> .envdir` resolves through one).
  # The link would put the main checkout's secrets into every stage worktree,
  # where code from the branch runs; the reset keeps them out otherwise, and the
  # stages read .env from the main checkout. Same rule as env_path in
  # bureau-doctor.py, which reports such an entry as an error.
  case "/$(printf '%s' "$p" | tr '[:upper:]' '[:lower:]')" in
    */.env*) echo "  WARNING: worktree link '$p' skipped: a .env file holds the main checkout's secrets and must not reach a stage worktree."; return 0 ;;
  esac
  # The resolved path, relative to the main checkout (a target outside it keeps
  # the components after the common part); prints its first .env* component.
  target=$(python3 -I -c '
import os, sys
main = os.path.realpath(sys.argv[1])
rel = os.path.relpath(os.path.realpath(os.path.join(sys.argv[1], sys.argv[2])), main)
print(next((c for c in rel.split(os.sep) if c not in ("", ".", "..") and c.lower().startswith(".env")), ""))' "$main" "$p" 2>/dev/null) || {
    echo "  WARNING: worktree link '$p' skipped: its target in the main checkout could not be resolved."; return 0
  }
  if [ -n "$target" ]; then
    echo "  WARNING: worktree link '$p' skipped: it resolves through a .env file or directory ($target) in the main checkout, whose secrets must not reach a stage worktree."; return 0
  fi
  # Nor a directory that holds one anywhere below it: a .env* name (any case),
  # links followed; the search stops at the first hit. A search that fails (an
  # unreadable subdirectory, a link loop) refuses the link: what it did not see
  # can hold a .env.
  if [ -d "$main/$p" ]; then
    if ! envfile=$(find -L "$main/$p" -mindepth 1 -iname '.env*' -print -quit 2>/dev/null); then
      echo "  WARNING: worktree link '$p' skipped: the directory could not be searched completely for .env files."; return 0
    fi
    if [ -n "$envfile" ]; then
      echo "  WARNING: worktree link '$p' skipped: the directory holds a .env file (${envfile#"$main/"}) whose secrets must not reach a stage worktree."; return 0
    fi
  fi
  # `grep -c`, not `grep -q`: -q closes the pipe early and pipefail turns the
  # SIGPIPE of ls-files into a false "not tracked".
  tracked=$(git -C "$wt" --literal-pathspecs ls-files -- "$p" 2>/dev/null | grep -c . || true)
  if [ "${tracked:-0}" -gt 0 ]; then
    echo "  WARNING: worktree link '$p' skipped: the branch tracks that path."; return 0
  fi
  # The parent by string slicing, not `dirname`: an entry may start with `-`,
  # which `dirname` reads as an option (and then fails the worker under set -e).
  case "$p" in */*) parent="${p%/*}" ;; *) parent=. ;; esac
  parent_phys=$(cd "$wt/$parent" 2>/dev/null && pwd -P) || {
    echo "  WARNING: worktree link '$p' skipped: '$parent' does not exist in the worktree."; return 0
  }
  case "$parent_phys/" in
    "$wt"/*) ;;
    *) echo "  WARNING: worktree link '$p' skipped: '$parent' leads outside the worktree."; return 0 ;;
  esac
  # Inside the worktree but through a symlink: git refuses to look beyond it.
  if [ "$parent" != . ] && [ "$parent_phys" != "$wt/$parent" ]; then
    echo "  WARNING: worktree link '$p' skipped: '$parent' runs through a symlink in the worktree."; return 0
  fi
  if [ ! -e "$main/$p" ]; then
    echo "  WARNING: worktree link '$p' skipped: it does not exist in the main checkout."; return 0
  fi
  # --no-index: tracking is checked above with a literal path; without it,
  # check-ignore reads the argument as a pathspec against the index, and `[ab]`
  # would count as tracked because the branch tracks `a`.
  if ! git -C "$wt" check-ignore -q --no-index -- "$p" 2>/dev/null; then
    echo "  WARNING: worktree link '$p' skipped: not ignored as a symlink (it would leave the worktree dirty). A pattern with a trailing slash matches directories only; add '$p' without it to .gitignore."
    return 0
  fi
  if [ -e "$wt/$p" ] && [ ! -L "$wt/$p" ]; then
    echo "  WARNING: worktree link '$p' skipped: a real file or directory is in the way; it is left alone."; return 0
  fi
  if ln -sfn -- "$main/$p" "$wt/$p" 2>/dev/null && [ "$(readlink "$wt/$p")" = "$main/$p" ]; then
    echo "  Linked $p from the main checkout"
  else
    echo "  WARNING: worktree link '$p' could not be made."
  fi
  return 0
}
# ── End of worktree links ─────────────────────────────────────────────

# A red build pulls the verdict down — but it NEVER softens a BLOCK.
#
# The review stage used to fold the build result in with one unconditional line:
# `[ "$BUILD_OK" = false ] && VERDICT="REQUEST_CHANGES"`. A build is also red for
# reasons that have nothing to do with the code — missing dependencies in the
# worktree, no network, a broken stub — and in that case a BLOCK lost its
# escalation: the finding stayed in the review text while the ticket went into
# ordinary rework without `needs-human`. That happened in a live installation on
# 2026-08-11 and was logged there as CRITICAL.
#
# The table is fail-closed: what it does not know becomes BLOCK. An unknown verdict
# is a fault in the caller, and a fault in the caller must not reach a merge.
apply_build_failure() {
  case "${1:-}" in
    BLOCK)                   echo "BLOCK" ;;
    APPROVE|REQUEST_CHANGES) echo "REQUEST_CHANGES" ;;
    *)                       echo "BLOCK" ;;
  esac
}

# decide_review_verdict <verdict> <security_issues> <security_critical> <build_ok> <cycles> <max_cycles>
#   → the final verdict on the first line, then one line per rule that changed or
#     qualified it. At most one line carries a reason: the rule that turned the verdict
#     into BLOCK (every escalating rule runs only while the verdict is not BLOCK yet), so a
#     BLOCK the merger gave itself keeps its own reason. Line format: "<rule><US><reason for the escalation log><US><text for the review>", fields
#     split by the ASCII unit separator (\037): a tab is whitespace to `read`, and two
#     tabs around an empty reason would collapse into one.
#
# The review stage's verdict rules as one ordered decision. The order is the behaviour:
#   1. verdict   anything but APPROVE, REQUEST_CHANGES or BLOCK is BLOCK.
#   2. security  the merged review's security_issues must be a count (a non-negative
#                integer). Missing, negative or anything else is BLOCK: an unreadable
#                count is not "none" (EXP-1518 in installation A: it used to read as 0,
#                so the floor did nothing exactly when the review was unreliable).
#   3. critical  the security specialist's own count of CRITICAL findings above 0 is BLOCK,
#                whatever verdict the merger chose. The merger's rules already say "any
#                CRITICAL security finding → BLOCK", and a merger once gave a CRITICAL
#                option injection a REQUEST_CHANGES header; this holds the rule without
#                trusting the merger. The specialist's JSON is unvalidated free text, so an
#                unreadable count is noted in the review, not escalated.
#   4. floor     any security finding means never APPROVE: APPROVE → REQUEST_CHANGES. A
#                non-critical security bug goes into rework like any other bug.
#   5. build     a build that is not green folds through apply_build_failure (BLOCK stays
#                BLOCK). The review text says the pipeline cannot tell a red build caused by
#                the code from one caused by the environment.
#   6. cap       last, so it also sees a REQUEST_CHANGES the build fold produced: at or past
#                max_review_cycles it escalates to BLOCK. It used to run before the fold, so
#                "reviewers approve, build red" never met the cap and went round forever
#                (EXP-1514 in installation A). A cycle count or cap that is not a count
#                escalates too.
decide_review_verdict() {
  local verdict="${1:-}" sec="${2:-}" crit="${3:-}" build_ok="${4:-}" cycles="${5:-}" max="${6:-}"
  local notes="" sep=$'\037' before sec_count="" crit_count cycles_count max_count reason

  case "$verdict" in
    APPROVE|REQUEST_CHANGES|BLOCK) ;;
    *) notes+="verdict${sep}review verdict unreadable${sep}VERDICT UNREADABLE: the review gave '$(_review_shown "$verdict")', not APPROVE, REQUEST_CHANGES or BLOCK. Treated as BLOCK."$'\n'
       verdict="BLOCK" ;;
  esac

  if sec_count=$(_review_count "$sec"); then :; else
    sec_count=""
    # The reason names the escalation only when this rule caused it: a BLOCK the merger
    # gave itself stays the logged reason.
    reason="security_issues unreadable"; [ "$verdict" != "BLOCK" ] || reason=""
    notes+="security-unreadable${sep}${reason}${sep}SECURITY COUNT UNREADABLE: the merged review reported security_issues as '$(_review_shown "${sec:-missing}")', not a count. Treated as BLOCK: an unreadable count is not \"none\"."$'\n'
    verdict="BLOCK"
  fi

  if crit_count=$(_review_count "$crit"); then
    if [ "$crit_count" -gt 0 ] && [ "$verdict" != "BLOCK" ]; then
      notes+="security-critical${sep}security specialist reported ${crit_count} CRITICAL finding(s)${sep}SECURITY: the security specialist reported ${crit_count} CRITICAL finding(s); a CRITICAL security finding always needs a human, whatever the merged verdict said. Raised from ${verdict} to BLOCK."$'\n'
      verdict="BLOCK"
    fi
  elif [[ "$crit" =~ ^[0-9]+$ ]]; then
    # Digits only but too long to be a real count: it is still a positive CRITICAL count.
    if [ "$verdict" != "BLOCK" ]; then
      notes+="security-critical${sep}security specialist reported an implausible CRITICAL count${sep}SECURITY: the security specialist reported '$(_review_shown "$crit")' CRITICAL findings, not a plausible count but above 0. Raised from ${verdict} to BLOCK."$'\n'
      verdict="BLOCK"
    fi
  else
    notes+="security-critical-unknown${sep}${sep}Note: the security specialist's own count of CRITICAL findings could not be read; only the merged security_issues count was checked."$'\n'
  fi

  if [ -n "$sec_count" ] && [ "$sec_count" -gt 0 ] && [ "$verdict" = "APPROVE" ]; then
    notes+="security-floor${sep}${sep}SECURITY FLOOR: ${sec_count} security finding(s) reported, so this cannot be approved. Downgraded APPROVE to REQUEST_CHANGES."$'\n'
    verdict="REQUEST_CHANGES"
  fi

  before="$verdict"
  if [ "$build_ok" != "true" ]; then
    verdict=$(apply_build_failure "$verdict")
    if [ "$before" = "APPROVE" ]; then
      notes+="build${sep}${sep}BUILD FAILURE: Must be fixed. The reviewers approved the code and only the build check failed; the pipeline cannot tell a failure caused by the code from one caused by the environment (dependencies, network, services)."$'\n'
    else
      notes+="build${sep}${sep}BUILD FAILURE: Must be fixed. The pipeline cannot tell a failure caused by the code from one caused by the environment (dependencies, network, services)."$'\n'
    fi
  fi

  if [ "$verdict" = "REQUEST_CHANGES" ]; then
    if ! cycles_count=$(_review_count "$cycles") || ! max_count=$(_review_count "$max"); then
      notes+="cycle-cap${sep}review cycle count unreadable${sep}**ESCALATED:** the review cycle count ('$(_review_shown "$cycles")') or max_review_cycles ('$(_review_shown "$max")') is not a count, so the loop cannot be bounded. Needs human intervention."$'\n'
      verdict="BLOCK"
    elif [ "$cycles_count" -ge "$max_count" ]; then
      if [ "$before" = "APPROVE" ]; then
        notes+="cycle-cap${sep}REQUEST_CHANGES exceeded max_review_cycles=${max_count} (reviewers approved, build red)${sep}**ESCALATED:** ${cycles_count} review cycles (max ${max_count}). The reviewers approved and only the build check stayed red, so rework alone does not end this loop. Needs human intervention."$'\n'
      else
        notes+="cycle-cap${sep}REQUEST_CHANGES exceeded max_review_cycles=${max_count}${sep}**ESCALATED:** ${cycles_count} review cycles (max ${max_count}). Needs human intervention."$'\n'
      fi
      verdict="BLOCK"
    fi
  fi

  printf '%s\n' "$verdict"
  printf '%s' "$notes"
}

# _review_count <value> → the value as a plain decimal count, or exit 1. Only digits,
# leading zeros dropped, at most nine digits: a longer number (2^63 and up) would make
# `[ … -gt … ]` fail with an error, and an error in an `if` reads as false.
_review_count() {
  local v="${1:-}"
  [[ "$v" =~ ^[0-9]+$ ]] || return 1
  v="${v#"${v%%[!0]*}"}"
  [ -n "$v" ] || v=0
  [ "${#v}" -le 9 ] || return 1
  printf '%s\n' "$v"
}

# _review_shown <value> → model output echoed into the review only as short, printable text.
_review_shown() {
  printf '%s' "${1:-}" | tr -cd '[:alnum:]_ .-' | cut -c1-40
}

# review_verdict_from_text <review> → APPROVE | REQUEST_CHANGES | BLOCK, or nothing.
#
# The legacy text form of the verdict, for a merger that dropped its json verdict:
# `REVIEW_VERDICT: X` or a `## REVIEW_VERDICT` heading with X on the next non-empty
# line. X must be exactly one verdict word (bold, backticks and a trailing period
# stripped); anything else gives nothing, which the stage reads as BLOCK. The old form
# took the first verdict word anywhere on those lines, so "NOT_APPROVED — BLOCK" read
# as APPROVE (EXP-1513 in installation A).
review_verdict_from_text() {
  local line
  line=$(printf '%s\n' "${1:-}" | sed 's/\*\*//g' | awk '
    grab && NF { print; exit }
    /^#+[[:space:]]*REVIEW_VERDICT[[:space:]]*$/ { grab = 1; next }
    /^REVIEW_VERDICT:/ { sub(/^REVIEW_VERDICT:[[:space:]]*/, ""); if (NF) { print; exit } grab = 1 }
  ')
  line=$(printf '%s' "$line" | sed -E 's/^[[:space:]]+//; s/[[:space:]]+$//; s/\.$//; s/^`(.*)`$/\1/')
  case "$line" in
    APPROVE|REQUEST_CHANGES|BLOCK) printf '%s\n' "$line" ;;
  esac
}

# Map pipeline exit code → human-readable error class (for alerts, logs,
# and shepherd's halt-classifier). Originally in queue-loop.sh; relocated
# so single-shot drivers can reuse the same exit-code protocol.
# resolve_verdict_exit <verdict> → 0 | 25 — the review stage's exit code,
# taken from the verdict alone. APPROVE and REQUEST_CHANGES end the stage
# cleanly (their routing is done); BLOCK and anything unknown end with 25
# (needs-human-or-paused), the same fail-closed direction as the stage's
# `VERDICT="${VERDICT:-BLOCK}"`.
#
# Carried over from installation B (EXP-1322), with this template's code: installation B
# ends a BLOCK with 20, which here means stopped-before-merge. A BLOCK used to
# label, comment and exit 0, indistinguishable from an approved review. The
# queue picker skips the needs-human ticket, but a shepherd saw 0, found the
# ticket still in Build Review and ran the review again on the same commit —
# in installation B such a second run flipped BLOCK to APPROVE with no code change and
# merged. With 25 the shepherd halts (shepherd_rc_action below).
resolve_verdict_exit() {
  case "${1:-}" in
    APPROVE|REQUEST_CHANGES) echo 0 ;;
    *)                       echo 25 ;;
  esac
}

# shepherd_rc_action <exit-code> → ok | retry | halt — how shepherd.sh answers a
# stage's exit code, as a pure table.
#
# Carried over from installation B. The shepherd used to list its halt codes one
# by one and send everything else to an "unexpected exit" that stopped without
# an alert — so every code added later (22 to 26 here) halted silently. Now
# halt is the default and only the exceptions are listed:
#   ok    0 success · 2 queue-empty
#   retry 10 linear-down · 16 provider-unauth (transient, throttled retry)
# Everything else halts with an alert, unknown codes included.
shepherd_rc_action() {
  case "${1:-}" in
    0|2)   echo "ok" ;;
    10|16) echo "retry" ;;
    *)     echo "halt" ;;
  esac
}

exit_class() {
  case "$1" in
    0)   echo "ok" ;;
    2)   echo "queue-empty" ;;
    10)  echo "linear-down" ;;
    11)  echo "worktree-dirty" ;;
    12)  echo "no-branch" ;;
    13)  echo "no-tasks" ;;
    14)  echo "build-failed" ;;
    15)  echo "no-pr" ;;
    16)  echo "provider-unauth" ;;
    17)  echo "rebase-needed" ;;
    18)  echo "gh-failed" ;;
    19)  echo "rebase-rejected" ;;
    20)  echo "stopped-before-merge" ;;
    21)  echo "ownership-conflict" ;;
    22)  echo "provider-or-result-error" ;;
    23)  echo "quota-wait" ;;
    24)  echo "environment-blocked" ;;
    25)  echo "needs-human-or-paused" ;;
    26)  echo "cancelled-ticket" ;;
    27)  echo "linear-unusable" ;;
    124) echo "timeout" ;;
    130) echo "cancelled-run" ;;
    *)   echo "error-$1" ;;
  esac
}

# Precondition: verify LINEAR_API_KEY works. Exit 10 on failure.
precondition_linear() {
  local out
  # No `2>/dev/null`: the retry lines of the fetch belong in the stage log.
  # Exit 10 stays — it is the contract with shepherd and queue-loop.
  out=$(linear_query "{ viewer { id } }" || true)
  local id
  id=$(printf '%s' "$out" | jq -r '.data.viewer.id // empty' 2>/dev/null || true)
  if [ -z "$id" ]; then
    echo "ERROR: LINEAR_API_KEY is missing or invalid (viewer query returned no id)" >&2
    exit 10
  fi
}

# Precondition: claude CLI is authenticated. Exit 16 on failure.
# Probes claude -p with a trivial prompt to detect "Not logged in" before any
# state mutation — prevents stranding issues in Spec with zero work done.
precondition_claude_auth() {
  precondition_runner "${1:-spec}"
}

# Precondition: worktree is clean (no uncommitted changes). Exit 11 on failure.
precondition_clean_worktree() {
  local dirty
  dirty=$(git status --porcelain 2>/dev/null || true)
  if [ -n "$dirty" ]; then
    echo "ERROR: worktree has uncommitted changes — pipeline refuses to run" >&2
    echo "$dirty" >&2
    exit 11
  fi
}

# Helper: pick next issue from a queue via direct Linear GraphQL.
#
# Replaces the old "spawn claude -p and ask it to pick" picker, which was
# unreliable because headless claude subprocesses can't re-auth remote MCPs
# and Linear's MCP OAuth tokens expire after ~1h.
#
# Usage:
#   pick_issue <state-uuid> <required-label-names-csv> [exclude-label-names-csv] [skip-issue-ids-csv]
#
# Filters: team=$BUREAU_TEAM_KEY, state by UUID, at least one required label,
#          project ∈ $BUREAU_PROJECTS — all listed projects (if set), parent is null.
# Exclude: drops issues that carry any excluded label (filtered client-side in jq).
# Sort:    priority ASC (1=Urgent first; 0=None treated as last), then createdAt ASC.
# Output:  issue identifier on stdout, empty string if queue empty or every
#          candidate has an open blocker.
#
# Dependency awareness (EXP-437): each candidate's Linear inverseRelations are
# inspected. A candidate is skipped when any relation of type "blocks" points
# from an issue whose state.type is neither "completed" nor "canceled". The
# picker walks the sorted list and returns the first unblocked candidate. Deep
# chains fall out naturally — A blocked by B blocked by C only unlocks B once
# C is Done, then unlocks A once B is Done. Skipped candidates are logged to
# stderr with the blocker identifier(s) so stuck queues are diagnosable.
#
# Requires LINEAR_API_KEY in .env (picked up as $API_KEY by calling scripts).
pick_issue() {
  local state_id="$1"
  local required_csv="$2"
  local exclude_csv="${3:-}"
  local skip_csv="${4:-}"

  local required_gql
  required_gql=$(printf '%s' "$required_csv" | awk -F',' '
    BEGIN{printf "["}
    {for(i=1;i<=NF;i++) if($i!="") printf "%s\"%s\"", (i>1?",":""), $i}
    END{printf "]"}
  ')

  # Project filter: every UUID in $BUREAU_PROJECTS (comma-separated) →
  # GraphQL `project: { id: { in: [...] } }`. Empty $BUREAU_PROJECTS = no
  # clause = all team projects. Same CSV→JSON-array pattern as required_gql
  # and exclude_json. Previously this used `cut -d',' -f1` which silently
  # dropped projects[1:] when an operator selected >1 project in Phase 1b.
  local project_clause=""
  if [ -n "${BUREAU_PROJECTS:-}" ]; then
    local projects_gql
    projects_gql=$(printf '%s' "$BUREAU_PROJECTS" | awk -F',' '
      BEGIN{printf "["}
      {for(i=1;i<=NF;i++) if($i!="") printf "%s\"%s\"", (i>1?",":""), $i}
      END{printf "]"}
    ')
    [ "$projects_gql" != "[]" ] && project_clause=$(printf ', project: { id: { in: %s } }' "$projects_gql")
  fi

  local query
  # first: 200 (issues) — was 50; under heavy load with many same-state issues
  # the urgent oldest ones could fall off the page before the client-side
  # priority sort ran. Bumping to 200 covers practical queue depths without
  # paginating. inverseRelations stays at 50 — practical blocker chains are short.
  query=$(printf '{ issues(filter: { team: { key: { eq: "%s" } }, state: { id: { eq: "%s" } }, labels: { some: { name: { in: %s } } }%s, parent: { null: true } }, orderBy: updatedAt, first: 200) { nodes { identifier priority createdAt labels { nodes { name } } inverseRelations(first: 50) { nodes { type issue { identifier state { type } } } } } } }' \
    "$BUREAU_TEAM_KEY" "$state_id" "$required_gql" "$project_clause")

  local payload
  payload=$(jq -n --arg q "$query" '{query: $q}')

  local exclude_json
  exclude_json=$(printf '%s' "$exclude_csv" | awk -F',' '
    BEGIN{printf "["}
    {for(i=1;i<=NF;i++) if($i!="") printf "%s\"%s\"", (i>1?",":""), $i}
    END{printf "]"}
  ')

  # Sorted candidate list, one per line: <identifier>\t<open-blockers-csv>
  # The blockers column is empty when nothing blocks the candidate.
  # Through linear_raw, so an unusable answer is retried and then ends the
  # stage with $BUREAU_EXIT_LINEAR_UNUSABLE instead of reading as "queue
  # empty" (exit 2). The answer has to carry every list the pick reads (the
  # issues, and each node's labels and blockers): read as `[]`, a node without
  # its labels passed the needs-human exclusion and one without its relations
  # passed as unblocked.
  local answer
  answer=$(linear_raw "$payload" "$_BUREAU_SHAPE_ISSUE_LABELS"' and all(.data.issues.nodes[]; (.inverseRelations.nodes | type) == "array")') || return $?
  local candidates
  candidates=$(printf '%s' "$answer" \
  | jq -r --argjson excl "$exclude_json" --arg skip "$skip_csv" '
    .data.issues.nodes
    | map(select(.identifier as $id | ($skip | split(",") | index($id)) == null))
    | map(select(
        ([.labels.nodes[].name] | map(select(. as $n | $excl | index($n))) | length) == 0
      ))
    | map(. + {_pri: (if .priority == 0 then 5 else .priority end)})
    | sort_by(._pri, .createdAt)
    | .[]
    | [ .identifier,
        ([.inverseRelations.nodes[]
          | select(.type == "blocks")
          | .issue
          | select(.state.type != "completed" and .state.type != "canceled")
          | .identifier
         ] | join(","))
      ]
    | @tsv
  ')

  # Walk in priority order; log every blocked skip and emit the first that is unblocked.
  while IFS=$'\t' read -r ident blockers; do
    [ -z "$ident" ] && continue
    if [ -n "$blockers" ]; then
      echo "pick_issue: skip $ident (open blockers: $blockers)" >&2
      continue
    fi
    printf '%s' "$ident"
    return 0
  done <<<"$candidates"
}

# ── Pipeline picker registry ───────────────────────────────────────
# Single source of truth for "which Linear queue does each pipeline drain?".
# Both queue-loop.sh's preselect (so it can resolve the spec branch and reset
# the worktree before invoking the pipeline) AND each pipeline's own picker
# call read from here. Adding a new pipeline → one row added below; the two
# call sites stay in sync automatically.
#
# Output format on stdout: <state-uuid>|<required-label-csv>|<exclude-label-csv>
#   Empty stdout = pipeline is opt-in and not configured for this repo (the
#   underlying state UUID is missing from .bureau.json). Both consumers treat
#   that as "queue empty".
#
# Per-pipeline config has lived inline at the call sites (qa/copy/merge gated
# on optional state UUIDs). Centralising it eliminates the "forgot to update
# both places" failure mode that bit the merge agent during initial wiring.
pipeline_picker_args() {
  case "$1" in
    spec-pipeline.sh)
      echo "$BUREAU_STATE_TRIAGE|$BUREAU_LABEL_LANE2_NAME|"
      ;;
    spec-review-pipeline.sh)
      echo "$BUREAU_STATE_SPEC_REVIEW|$BUREAU_LABEL_LANE2_NAME|"
      ;;
    ux-pipeline.sh)
      echo "$BUREAU_STATE_DESIGN|needs-ux,$BUREAU_LABEL_LANE2_NAME|"
      ;;
    copy-pipeline.sh)
      [ -n "${BUREAU_STATE_COPY:-}" ] && [ -n "${BUREAU_LABEL_NEEDS_COPY_NAME:-}" ] \
        && echo "$BUREAU_STATE_COPY|$BUREAU_LABEL_NEEDS_COPY_NAME,$BUREAU_LABEL_LANE2_NAME|needs-human"
      ;;
    implement-pipeline.sh)
      echo "$BUREAU_STATE_BUILD|$BUREAU_LABEL_LANE2_NAME,ai-implementable|needs-human"
      ;;
    qa-pipeline.sh)
      [ -n "${BUREAU_STATE_QA:-}" ] \
        && echo "$BUREAU_STATE_QA|$BUREAU_LABEL_LANE2_NAME,ai-implementable|needs-human"
      ;;
    code-review-pipeline.sh)
      echo "$BUREAU_STATE_BUILD_REVIEW|$BUREAU_LABEL_LANE2_NAME,ai-implementable|needs-human"
      ;;
    merge-pipeline.sh|rebase-pipeline.sh)
      [ -n "${BUREAU_STATE_MERGE:-}" ] \
        && echo "$BUREAU_STATE_MERGE|$BUREAU_LABEL_LANE2_NAME,ai-implementable|needs-human,blocked,wip"
      ;;
  esac
}

# pipeline_pick_next <script-name> [skip-issue-ids-csv]
#   Reads the registry above, dispatches to pick_issue with the right args.
#   Returns the picked issue identifier on stdout, empty on queue-empty or
#   when an opt-in pipeline isn't configured for this repo.
#
# Usage in pipelines:
#   ISSUE=$(pipeline_pick_next "$(basename "$0")")
# Usage in queue-loop.sh's preselect:
#   pipeline_pick_next "$script_name"
pipeline_pick_next() {
  local args
  args=$(pipeline_picker_args "$1")
  [ -z "$args" ] && return 0
  local state required exclude
  IFS='|' read -r state required exclude <<<"$args"
  # Universal: never pick a ticket currently being driven by shepherd.sh.
  # Shepherd applies `shepherd-focused` on entry and removes it on EXIT/INT/
  # TERM — while it's set, the cron queue stays out of the way.
  if [ -n "$exclude" ]; then
    exclude="${exclude},shepherd-focused"
  else
    exclude="shepherd-focused"
  fi
  # App blocked results use the configured human gate in every stage.
  local human_label
  human_label=$(bureau_get '.linear.labels.needs_human.name // "needs-human"')
  exclude="${exclude},needs-human,${human_label}"
  # A ticket whose needs-human label could not be written is held locally
  # (mark_needs_human): skip it like a labelled one, and try the label again.
  local held skip="${2:-}"
  held=$(needs_human_holds_flush)
  if [ -n "$held" ]; then
    echo "pick: skipping ticket(s) held for a human whose needs-human label is not written yet: $held" >&2
    skip="${skip:+$skip,}$held"
  fi
  if [ -n "$skip" ]; then
    pick_issue "$state" "$required" "$exclude" "$skip"
  else
    pick_issue "$state" "$required" "$exclude"
  fi
}

# ── Spec directory of a branch ─────────────────────────────────────────
# bureau_spec_dir_for_branch <branch>: print the spec directory that belongs to
# <branch> as "$BUREAU_SPECS_DIR/<name>/", or nothing when no directory can be
# told apart. Every stage that needs the ticket's spec asks here, so they all
# agree on one directory. bureau_spec_dir_candidates <branch> prints the
# directories that fit equally, as "`a`, `b`", when that is why there is none.
#
# Spec dirs and branches share the form `NNN-<slug>`, but the number is not
# unique: some installs mint `001-<slug>` for every ticket, others repeat a
# number now and then, and hand-made branches reuse the number of an unrelated
# spec. Matching on the number handed such a branch another ticket's tasks.md;
# matching the slug as a loose substring did the same for `001-refund-rate`
# next to `001-refund`. The branch is normalised first: everything up to its
# last `/` goes (`codex/`, `exp/`, `user/`), then a leading issue key such as
# `exp-1444-` (letters, `-`, digits, `-`, any case). Then, highest first:
#   1. a directory named exactly like the branch or its last segment;
#   2. the one directory whose slug (name without `NNN-`) fits the branch
#      slug: equal; or the branch slug is its start up to a `-` (a truncated
#      branch: `091-wire-mcp-tool` for `091-wire-mcp-tool-metadata`); or it is
#      a run of whole `-`-separated words inside the branch slug (a branch with
#      extra words: `t1-automated-tests-unit` for `001-automated-tests`). When
#      several fit, the branch's number decides first: the fits that carry it,
#      if any, are the only ones left. Among those left, the one whose slug
#      equals the branch slug (`128-report-builder-wireup` over `117-report-builder`
#      for a branch `…-report-builder-wireup`). Two or more left is a tie.
# Nothing else: a number alone never selects a directory, and a single slug
# fit wins even when another directory carries the branch's number. Anything
# ambiguous prints nothing, and the stage works without a spec directory or
# stops instead of guessing. Slugs compare case-insensitively. bash 3.2 safe.
bureau_spec_dir_for_branch() {
  _bureau_spec_scan "${1:-}"
  if [ -n "$_bsd_result" ]; then printf '%s\n' "$_bsd_result"; fi
  return 0
}
bureau_spec_dir_candidates() {
  _bureau_spec_scan "${1:-}"
  if [ -n "$_bsd_candidates" ]; then printf '%s\n' "$_bsd_candidates"; fi
  return 0
}
# _bureau_spec_scan <branch>: sets $_bsd_result (the directory, or empty) and
# $_bsd_candidates (the tied directories as "`a`, `b`", or empty).
_bureau_spec_scan() {
  local branch="${1:-}" specs="${BUREAU_SPECS_DIR:-specs}"
  local last b_num b_slug d name num slug hit
  local slug_n=0 slug_hit="" slug_all="" slugnum_n=0 slugnum_hit=""
  local eq_n=0 eq_hit="" numeq_n=0 numeq_hit=""
  _bsd_result=""; _bsd_candidates=""
  [ -n "$branch" ] || return 0
  last="${branch##*/}"
  _bureau_spec_split "$(_bureau_spec_unkey "$last")"; b_num="$_bsd_num"; b_slug="$_bsd_slug"
  for d in "$specs"/*/; do
    [ -d "$d" ] || continue
    name="${d%/}"; name="${name##*/}"
    if [ "$name" = "$branch" ] || [ "$name" = "$last" ]; then
      _bsd_result="$d"
      return 0
    fi
    _bureau_spec_split "$name"; num="$_bsd_num"; slug="$_bsd_slug"
    hit=""
    if [ -n "$slug" ] && [ -n "$b_slug" ]; then
      case "$slug" in "$b_slug"|"$b_slug"-*) hit=1 ;; esac
      case "-$b_slug-" in *"-$slug-"*) hit=1 ;; esac
    fi
    if [ -n "$hit" ]; then
      slug_n=$((slug_n + 1)); slug_hit="$d"
      slug_all="${slug_all:+$slug_all, }\`$name\`"
      if [ -n "$num" ] && [ "$num" = "$b_num" ]; then
        slugnum_n=$((slugnum_n + 1)); slugnum_hit="$d"
      fi
      if [ "$slug" = "$b_slug" ]; then
        eq_n=$((eq_n + 1)); eq_hit="$d"
        if [ -n "$num" ] && [ "$num" = "$b_num" ]; then
          numeq_n=$((numeq_n + 1)); numeq_hit="$d"
        fi
      fi
    fi
  done
  # Several fits: the branch's number narrows them first; among what is left, a
  # slug equal to the branch slug decides; anything else is a tie.
  if [ "$slug_n" -eq 1 ]; then
    _bsd_result="$slug_hit"
  elif [ "$slug_n" -gt 1 ]; then
    if [ "$slugnum_n" -eq 1 ]; then _bsd_result="$slugnum_hit"
    elif [ "$slugnum_n" -gt 1 ] && [ "$numeq_n" -eq 1 ]; then _bsd_result="$numeq_hit"
    elif [ "$slugnum_n" -eq 0 ] && [ "$eq_n" -eq 1 ]; then _bsd_result="$eq_hit"
    else _bsd_candidates="$slug_all"; fi
  fi
  return 0
}
# _bureau_spec_unkey <segment>: the segment without a leading issue key
# (`exp-1444-provider-routing` → `provider-routing`).
_bureau_spec_unkey() {
  local n="${1:-}" key rest digits
  key="${n%%-*}"; rest="${n#*-}"
  case "$n" in *-*-*) ;; *) printf '%s' "$n"; return 0 ;; esac
  digits="${rest%%-*}"
  case "$key" in ''|*[!A-Za-z]*) printf '%s' "$n"; return 0 ;; esac
  case "$digits" in ''|*[!0-9]*) printf '%s' "$n"; return 0 ;; esac
  printf '%s' "${rest#*-}"
}
# _bureau_spec_split <name>: split `NNN-<slug>` into $_bsd_num (digits before
# the first dash, empty when there are none) and $_bsd_slug (the rest, lower
# case). A name without a leading `NNN-` is all slug.
_bureau_spec_split() {
  local n="${1:-}" head
  _bsd_num=""; _bsd_slug="$n"
  case "$n" in
    *-*)
      head="${n%%-*}"
      case "$head" in
        *[!0-9]*) ;;
        *) _bsd_num="$head"; _bsd_slug="${n#*-}" ;;
      esac ;;
  esac
  _bsd_slug=$(printf '%s' "$_bsd_slug" | tr '[:upper:]' '[:lower:]')
}
# ── End of spec directory of a branch ──────────────────────────────────

# ── Shared prompt helpers ──────────────────────────────────────────
# build_spec_context: assemble the "pinned decisions win" grounding that every
# stage prompt should carry. Previously only code-review-pipeline.sh built this
# inline (see EXP notes on SKIP classification). Exposed here so spec-review,
# ux, implement, qa, copy can reuse it — the discipline applies everywhere.
#
# Usage: ctx=$(build_spec_context "$SPEC_DIR")
#   $SPEC_DIR may be empty. Files that don't exist are silently omitted.
build_spec_context() {
  local spec_dir="${1:-}"
  local ctx="SCOPE DISCIPLINE — READ BEFORE ACTING:"
  ctx+=$'\n- scripts/bureau-stage.md — shared stage boundaries and evidence contract.'
  [ -f "AGENTS.md" ] && ctx+=$'\n- AGENTS.md (repo root) — project instructions.'
  [ -f "SPEC.md" ]   && ctx+=$'\n- SPEC.md (repo root) — project source of truth.'
  [ -f "CLAUDE.md" ] && ctx+=$'\n- CLAUDE.md (repo root) — conventions and non-goals.'
  if [ -n "$spec_dir" ]; then
    local f
    for f in spec.md plan.md research.md tasks.md design.md; do
      [ -f "${spec_dir}${f}" ] && ctx+=$'\n- '"${spec_dir}${f}"
    done
  fi
  ctx+=$'\n\nA proposal that contradicts a pinned decision is NOT an improvement — it is out of scope. When declining, cite the pin ("skipped: plan.md pins Go 1.22"). Do NOT resurface findings that a prior review cycle on this issue already deferred.'
  printf '%s' "$ctx"
}

# build_lessons_context: read LESSONS.md from cwd (the worktree root) and wrap
# it for inclusion in a stage prompt. Returns empty string when the file is
# absent OR contains only whitespace — so consumers can splice it
# unconditionally without producing an empty "## Learned patterns" section.
#
# /bureau-learnings writes the draft; the human curates and commits. Only the
# committed file ever reaches a pipeline prompt, because worktrees reset to
# origin/<branch> before every pick.
#
# Usage: lessons=$(build_lessons_context)
build_lessons_context() {
  local file="LESSONS.md"
  [ ! -f "$file" ] && return 0
  # Whitespace-only check: tr -d, then test empty.
  local trimmed
  trimmed=$(tr -d '[:space:]' < "$file" 2>/dev/null || true)
  [ -z "$trimmed" ] && return 0
  printf '## Learned patterns\n\nThe following are human-curated lessons from prior bureau runs. Treat as advisory, not binding — they reflect patterns observed across multiple past issues and may not all apply here. Use them to inform judgment; do not cite them as pinned decisions.\n\n%s' "$(cat "$file")"
}

# build_negative_constraints: shared "Do NOT" block injected into prompts that
# write code (implement, ux, copy, qa). Centralising avoids drift — adding a
# new constraint only touches one place. Each prompt may add its own role-
# specific constraints below this block.
build_negative_constraints() {
  cat <<'EOF'
NEGATIVE CONSTRAINTS — DO NOT:
- Edit package.json versions, lockfiles, CI config files, or .env.
- Refactor code outside the task's explicit scope.
- Delete or suppress failing tests to make the build pass.
- Re-introduce patterns that a prior review cycle on this issue declined.
- Create new top-level directories without an explicit task saying so.
EOF
}

# parse_claude_json: extract the LAST fenced ```json ... ``` block from Claude's
# stdout and pass it to jq. Every stage whose output the shell parses should
# emit such a block so the shell side is regex-free.
#
# Usage: value=$(parse_claude_json "$OUTPUT" '.verdict')
#   Returns empty string on parse failure — caller decides the fallback.
parse_claude_json() {
  local raw="$1" filter="$2" text block
  text=$(printf '%s' "$raw" | jq -r 'if type=="object" and (.result | type)=="string" then .result else empty end' 2>/dev/null || true)
  [ -n "$text" ] || text="$raw"
  if printf '%s' "$text" | jq -e 'type == "object"' >/dev/null 2>&1; then
    printf '%s' "$text" | jq -r "$filter" 2>/dev/null || true
    return 0
  fi
  block=$(printf '%s' "$text" | awk 'BEGIN{b=""; in_block=0}
    /^```json[[:space:]]*$/ {in_block=1; b=""; next}
    /^```[[:space:]]*$/ {if(in_block){saved=b; in_block=0}; next}
    {if(in_block)b=b $0 "\n"} END{print saved}')
  [ -n "$block" ] && printf '%s' "$block" | jq -r "$filter" 2>/dev/null || true
}
