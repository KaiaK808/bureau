#!/bin/bash
# bureau-env.sh — read the pipeline's keys from a .env file without ever
# executing it. A pure library: sourcing it defines three functions and does
# nothing else. It reads no file on its own and needs no .bureau.json, no jq
# and no other external program.
#
# EXP-1469. Sixteen scripts under scripts/ used to `source` their .env, so the
# file ran as shell code. A single space after `=` in a future entry
# (`KEY= value`) would have run `value` as a command, and bash prints it in its
# "command not found" message — straight into a log that ends up in Linear or
# GitHub.
#
#   bureau_load_env [--export] <file>
#     Reads <file> line by line and sets only the keys bureau_env_key_allowed
#     accepts. Returns 0 when the file was read, even if no key was taken, and
#     1 with the single line "bureau_load_env: cannot read <file>" on stderr
#     when it is missing, not a regular file or unreadable. --export exports
#     every key it sets. Writes nothing to stdout and never prints a line or a
#     value; the only name it prints is that of a numeric key it dropped (L13).
#   bureau_env_key_allowed <name>
#     0 for a name on the key list, 1 otherwise. Silent.
#   bureau_env_key_numeric <name>
#     0 for a key whose value later lands in bash arithmetic, 1 otherwise.
#     Silent.
#
# Reading rules (specs/023-crosscheck-env-read/contracts/env-read.md, L1–L13):
#   - only `NAME=VALUE` lines count, optionally indented and prefixed with
#     `export`; comments, blank lines and anything else are skipped silently
#   - a trailing carriage return is dropped; the value is everything after the
#     FIRST `=`
#   - a value in "…" or '…' is the text between the quotes, taken literally; an
#     unquoted value loses a whitespace-separated `# comment` — cut on the raw
#     value, before any trimming — and then its surrounding whitespace
#   - values stay text: no $-expansion, no command substitution, no escapes
#   - the last entry wins; a key from the file replaces the environment's
#     value, and a key missing from the file leaves the environment alone
#   - a running `set -x` is switched off first and restored before returning,
#     so no value reaches the trace
#   - a key whose value later lands in bash arithmetic is taken only as `0` or
#     as digits without a leading zero (bureau_env_key_numeric); any other
#     value — `08` and `010` among them, which bash reads as octal — is dropped
#     with a warning on stderr that names the key, never the value
#
# Must run under bash 3.2 (/bin/bash on macOS): there is no `local -` and no
# `declare -g`, so keys are assigned with `printf -v`, and every local name
# starts with _be_ so it can never shadow a key.

# The key list: every name with prefix BUREAU_, LINEAR_, TELEGRAM_ or
# BRAINHUGGERS_ that one of the 16 readers or bureau-config.sh reads and that
# bureau-config.sh does not set itself when sourced, plus the per-stage names
# bureau-config.sh builds at run time (research R-15). The inventory guard I-1
# in tests/unit/test_pipeline_env_read.py fails when a script starts reading a
# name that is on neither side.
#
# EXP-1478 added the four BUREAU_LINEAR_RETRY* keys (retry count and the three
# waits of a Linear fetch). They are NOT on the numeric list: their values stay
# digits with a possible leading zero, and _bureau_linear_number in
# bureau-config.sh checks each one before it can reach an arithmetic context.
# EXP-1482 added BUREAU_LINEAR_MAX_TIME and BUREAU_LINEAR_CONNECT_TIMEOUT (the
# time limit of one Linear request) on the same terms.
# BUREAU_POST_IMPLEMENT_TIMEOUT (the limit of repo.post_implement_command) sits
# with the implement timeouts, on the numeric list too; implement-pipeline.sh
# also checks it against ^[1-9][0-9]*$ before use.
bureau_env_key_allowed() {
  case "$1" in
    LINEAR_API_KEY | TELEGRAM_BOT_TOKEN | TELEGRAM_ALERT_CHAT_ID | \
    BUREAU_MODEL_DEFAULT | BUREAU_CODEX_MODEL_DEFAULT | \
    BUREAU_DRY_RUN | BUREAU_FORCE_ALL_AGENTS | BUREAU_HEADROOM_WRAP | BUREAU_USE_GOAL_LOOP | \
    BUREAU_CAVEMAN_LEVEL | BUREAU_COST_TRACKING | BUREAU_COST_DIR | \
    BUREAU_USAGE_FILE | BRAINHUGGERS_USAGE_FILE | BUREAU_DISABLE_THROTTLE | \
    BUREAU_PATH_PREFIX_STRIP | BUREAU_REVIEW_MERGE_CAP_KB | \
    BUREAU_IMPL_MAX_ITER | BUREAU_IMPL_ITER_TIMEOUT | BUREAU_IMPL_TOTAL_TIMEOUT | \
    BUREAU_POST_IMPLEMENT_TIMEOUT | \
    BUREAU_SUPERVISOR_MAX_CRASHES | BUREAU_SUPERVISOR_STABILITY_WINDOW | \
    BUREAU_SESSION | BUREAU_SESSION_NAME | \
    BUREAU_NO_MERGE | BUREAU_STOP_REQUESTED | \
    BUREAU_LINEAR_RETRIES | BUREAU_LINEAR_RETRY_WAIT_1 | \
    BUREAU_LINEAR_RETRY_WAIT_2 | BUREAU_LINEAR_RETRY_WAIT_3 | \
    BUREAU_LINEAR_MAX_TIME | BUREAU_LINEAR_CONNECT_TIMEOUT | \
    BUREAU_MODEL_[A-Z]* | BUREAU_RUNNER_[A-Z]* | BUREAU_CODEX_MODEL_[A-Z]*)
      return 0
      ;;
  esac
  return 1
}

# The numbers among them. Bash evaluates the CONTENT of a variable a second time
# inside $(( … )), (( … )) and [[ … -lt … ]], so a value of the form
# x[$(command)] runs command at the reading script's arithmetic, not here — the
# one way left for a .env value to become a command (review of 79a9825). These
# keys are therefore taken only as `0` or as digits without a leading zero
# (L13); anything else is dropped, so the reading script's own default holds.
# Evaluated a second time today:
# implement-pipeline.sh:513 (BUREAU_IMPL_MAX_ITER) and :515
# (BUREAU_IMPL_TOTAL_TIMEOUT), code-review-pipeline.sh:279
# (BUREAU_REVIEW_MERGE_CAP_KB). The other three reach only the `[` builtin,
# which does not re-evaluate its arguments (implement-pipeline.sh:523,
# queue-loop-supervised.sh:89 and :98) — they are the numeric knobs of the very
# same loops, and one edit from `[` to `[[` would evaluate them, so they carry
# the same rule. No other listed key reaches arithmetic, a numeric [[ … ]] or
# let.
bureau_env_key_numeric() {
  case "$1" in
    BUREAU_IMPL_MAX_ITER | BUREAU_IMPL_ITER_TIMEOUT | BUREAU_IMPL_TOTAL_TIMEOUT | \
    BUREAU_POST_IMPLEMENT_TIMEOUT | BUREAU_REVIEW_MERGE_CAP_KB | \
    BUREAU_SUPERVISOR_MAX_CRASHES | BUREAU_SUPERVISOR_STABILITY_WINDOW)
      return 0
      ;;
  esac
  return 1
}

bureau_load_env() {
  # L1: the very first statement turns a running trace off, before any line or
  # value is expanded; every return below restores it.
  case $- in
    (*x*) set +x; local _be_trace=1 ;;
    (*) local _be_trace=0 ;;
  esac
  local _be_export=0
  if [ "${1:-}" = "--export" ]; then
    _be_export=1
    shift
  fi
  local _be_file="${1:-}"
  if [ -z "$_be_file" ] || [ ! -f "$_be_file" ] || [ ! -r "$_be_file" ]; then
    echo "bureau_load_env: cannot read $_be_file" >&2
    if [ "$_be_trace" = 1 ]; then set -x; fi
    return 1
  fi

  local _be_assign='^[[:space:]]*(export[[:space:]]+)?([A-Za-z_][A-Za-z0-9_]*)=(.*)$'
  local _be_tail='^[[:space:]]*(#.*)?$'
  local _be_number='^(0|[1-9][0-9]*)$'
  local _be_line _be_name _be_raw _be_value _be_quote _be_rest _be_inner _be_after
  while IFS= read -r _be_line || [ -n "$_be_line" ]; do
    _be_line="${_be_line%$'\r'}"
    [[ $_be_line =~ $_be_assign ]] || continue
    _be_name="${BASH_REMATCH[2]}"
    _be_raw="${BASH_REMATCH[3]}"
    bureau_env_key_allowed "$_be_name" || continue

    _be_value="${_be_raw#"${_be_raw%%[![:space:]]*}"}"
    _be_quote="${_be_value:0:1}"
    if [ "$_be_quote" = '"' ] || [ "$_be_quote" = "'" ]; then
      _be_rest="${_be_value:1}"
      _be_inner="${_be_rest%%"$_be_quote"*}"
      _be_after="${_be_rest#*"$_be_quote"}"
      if [ "$_be_inner" != "$_be_rest" ] && [[ $_be_after =~ $_be_tail ]]; then
        _be_value="$_be_inner"
      else
        _be_value="${_be_value%"${_be_value##*[![:space:]]}"}"
      fi
    else
      # Cut the comment on the RAW value: trimming first would turn
      # `KEY= # comment` into "# comment" (research R-10).
      _be_value="${_be_raw%%[[:space:]]#*}"
      _be_value="${_be_value#"${_be_value%%[![:space:]]*}"}"
      _be_value="${_be_value%"${_be_value##*[![:space:]]}"}"
    fi

    # L13: a key that later lands in arithmetic is taken only as `0` or digits
    # without a leading zero. A digits-only test was not enough: bash reads a
    # leading zero as octal, so `08` aborts the arithmetic it reaches
    # (implement-pipeline.sh:515, exit 1) and `010` silently becomes 8 — the
    # guard promised more than it gave (review of 3d168a0). Both fall back to
    # the reading script's own default now. The warning names the key and never
    # the value (L11 holds for the value).
    if bureau_env_key_numeric "$_be_name" && ! [[ $_be_value =~ $_be_number ]]; then
      echo "bureau_load_env: $_be_name ignored, value is not a plain number" >&2
      continue
    fi

    printf -v "$_be_name" '%s' "$_be_value"
    if [ "$_be_export" = 1 ]; then
      export "$_be_name"
    fi
  done < "$_be_file"

  if [ "$_be_trace" = 1 ]; then set -x; fi
  return 0
}
