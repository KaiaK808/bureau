#!/bin/bash
# bureau-env.sh — read the pipeline's keys from a .env file without ever
# executing it, and run code the branch controls without them. A pure library:
# sourcing it defines functions and does nothing else. One of them is named
# `git`: in every script that sources this file, git commands run without the
# secrets (see the end of the file). The three .env readers read no file on
# their own and need no .bureau.json, no jq and no other external program;
# bureau_untrusted_env (at the end) reads repo.untrusted_env through bureau_get
# or jq when it is called.
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
#     every key it sets except the three secrets (bureau_env_key_secret): those
#     stay shell variables of the reading script, with or without --export, and
#     lose the export attribute a parent shell gave them (v3.2). Writes nothing
#     to stdout and never prints a line or a value; the only name it prints is
#     that of a numeric key it dropped (L13).
#   bureau_env_key_allowed <name>
#     0 for a name on the key list, 1 otherwise. Silent.
#   bureau_env_key_secret <name>
#     0 for LINEAR_API_KEY, TELEGRAM_BOT_TOKEN and TELEGRAM_ALERT_CHAT_ID, 1
#     otherwise. Silent.
#   bureau_env_key_numeric <name>
#     0 for a key whose value later lands in bash arithmetic, 1 otherwise.
#     Silent.
#   bureau_secret_set <name>
#     0 when the variable <name> holds a non-empty value, 1 otherwise; a
#     running `set -x` is off while the value is looked at (v3.2). Silent.
#   bureau_secret_copy [--optional] <target> <source>
#     Sets the shell variable <target> to the value of <source> with a running
#     `set -x` off, and never exports <target> (v3.2): the stages' copy of the
#     Linear key (API_KEY). Without --optional an empty or unset <source> ends
#     the script with 1 and "<script>: <source>: Set <source> in .env" on
#     stderr, as ${source:?Set <source> in .env} did.
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
#   - allexport (`set -a`, also from an exported SHELLOPTS) is switched off
#     and left off (v3.2): with it on, every later assignment of the reading
#     script, the API_KEY copy of the Linear key among them, would be exported
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

# The secrets among them (v3.2). An exported variable is in the environment of
# every process the script starts — its gh, jq, date and Python helpers — and
# any process of the same user (a test server a branch left running) reads
# that through /proc/<pid>/environ on Linux and `ps -E` on macOS (for
# executables that are not Apple platform binaries). The stages' own Linear
# and Telegram requests read these as shell variables and hand them to curl on
# stdin (_bureau_linear_fetch and alert_telegram in bureau-config.sh), and
# every Bureau script that needs one reads .env itself, so bureau_load_env
# never exports them. A secret only the environment holds (the operator's shell
# exported it, .env does not define it) is left as it is: Bureau does not change
# values from the operator's shell, and it is already in the environment of
# every process below that shell (SECURITY.md: keep the keys only in .env).
bureau_env_key_secret() {
  case "$1" in
    LINEAR_API_KEY | TELEGRAM_BOT_TOKEN | TELEGRAM_ALERT_CHAT_ID) return 0 ;;
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
  # v3.2: an operator shell that ran `set -a` and exported SHELLOPTS starts every Bureau bash with
  # allexport on, and then each assignment the reading script makes after this load would be
  # exported to the processes it starts, the stages' API_KEY copy of the Linear key included. Off
  # here, and left off: the shell that holds the secrets exports only what it names.
  set +a
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
    if bureau_env_key_secret "$_be_name"; then
      export -n "$_be_name"
    elif [ "$_be_export" = 1 ]; then
      export "$_be_name"
    fi
  done < "$_be_file"

  if [ "$_be_trace" = 1 ]; then set -x; fi
  return 0
}

# bureau_secret_set and bureau_secret_copy (v3.2) — a stage run under `bash -x`, or with xtrace in
# an exported SHELLOPTS, prints every command it runs with its words expanded: `[ -n "$KEY" ]`
# and `API_KEY="$LINEAR_API_KEY"` would put the key on stderr, which the queue loop writes to its
# log. These two look at and copy a secret with the trace off, and restore it.
bureau_secret_set() {
  case $- in
    (*x*) set +x; local _bss_trace=1 ;;
    (*) local _bss_trace=0 ;;
  esac
  local _bss_rc=1
  [ -z "${!1:-}" ] || _bss_rc=0
  if [ "$_bss_trace" = 1 ]; then set -x; fi
  return "$_bss_rc"
}

bureau_secret_copy() {
  case $- in
    (*x*) set +x; local _bsc_trace=1 ;;
    (*) local _bsc_trace=0 ;;
  esac
  local _bsc_optional=0
  if [ "${1:-}" = --optional ]; then _bsc_optional=1; shift; fi
  if [ "$_bsc_optional" = 0 ] && [ -z "${!2:-}" ]; then
    echo "$0: $2: Set $2 in .env" >&2
    exit 1
  fi
  printf -v "$1" '%s' "${!2:-}"
  export -n "$1"
  if [ "$_bsc_trace" = 1 ]; then set -x; fi
  return 0
}

# ── Code from the branch runs without Bureau secrets ──────────────────────────
# "The seven": the Bureau-owned secrets from .env (LINEAR_API_KEY,
# TELEGRAM_BOT_TOKEN, TELEGRAM_ALERT_CHAT_ID) and the GitHub token variables
# (GH_TOKEN, GITHUB_TOKEN, GH_ENTERPRISE_TOKEN, GITHUB_ENTERPRISE_TOKEN).
# Removing a name also removes every other exported variable that carries its
# value: a value of 6 characters or more anywhere inside another value (the
# stages' API_KEY copy of the Linear key, a token inside a remote URL or an
# Authorization header), a shorter one only when it is the whole value — inside
# matching of two to five characters would take out every variable that
# happens to contain them. BASH_ENV and ENV go as well: a bash child sources
# the file BASH_ENV names at startup (a relative name from its working
# directory, which can be the branch's worktree), and pointed at .env it would
# read the keys back in. Keep the lists and the 6 here equal to
# UNTRUSTED_REMOVE, UNTRUSTED_STARTUP, COPY_MIN and UNTRUSTED_KEEP in
# bureau-provider.py; tests/test_untrusted_env.sh compares the implementations
# and pins both.
# Every function below leaves the calling shell alone (the stage keeps its keys
# for its own Linear, GitHub and Telegram calls), calls env by its absolute path
# /usr/bin/env (a PATH entry such as node_modules/.bin cannot stand in for it),
# and switches a running `set -x` off while values are compared (L1 above); the
# traced command line shows names, never a value.
#
# bureau_untrusted_env [--check] [NAME=VALUE ...] <command> [argument ...]
#   Runs code the branch controls: the review build check
#   (code-review-pipeline.sh), the three QA test runs (qa-pipeline.sh),
#   repo.post_implement_command and the Codex completion test
#   (implement-pipeline.sh), the app's `test` action (bureau-app.sh) and the
#   build and test commands of upstream-port.sh. The agent processes get the
#   same reduction from bureau-provider.py (untrusted_env). The call sites start
#   their bash child with --noprofile --norc.
#   repo.untrusted_env in .bureau.json selects it:
#     absent, null or "default" — the calling environment minus the seven,
#       their copies, BASH_ENV and ENV. Everything else stays, so test commands
#       keep their toolchain variables (cargo, nvm, pyenv, a virtualenv).
#     "clean" — `env -i` with only PATH HOME USER LOGNAME SHELL TMPDIR TEMP
#       TMP LANG LC_ALL LC_CTYPE TERM TZ CI (those that are exported and carry
#       no secret's value), plus the NAME=VALUE pairs given before the command
#       (the hook's BUREAU_ISSUE and BUREAU_BRANCH).
#   Any other value, a .bureau.json jq cannot read, or no command: the command
#   is not run, a message goes to stderr, and the function EXITS the calling
#   shell with 24 (environment-blocked), so a stage never judges code it did
#   not run. Call it in the stage's own shell: inside $(…), ( … ) or a
#   pipeline the exit ends only that subshell. With --check it only validates
#   and RETURNS 0 or 24: for a call site whose stderr goes into a log (check
#   first, so the message reaches the stage output) and for callers that map
#   the failure to a code of their own. Returns the command's exit status.
#
# bureau_without_secrets [NAME=VALUE ...] <command> [argument ...]
#   Runs a command of Bureau's own that needs none of the seven in the default
#   reduction, whatever repo.untrusted_env says ("clean" would drop what they
#   need): the provider (run_stage_for, precondition_runner) and the executor's
#   inline Python. No command: exit 24. The Linear and Telegram requests drop
#   the same variables in their own subshell (_bureau_drop_secrets).
#
# gh
#   A function in every script that sources this file: gh runs git itself (to
#   find the repository, to push for `gh pr create`), so it starts without the
#   three .env keys, their copies, BASH_ENV and ENV; it keeps the GitHub token
#   variables it authenticates with.
#
# git
#   A function in every script that sources this file. Bureau's own git
#   commands in a stage worktree run the repository's hooks and filters, and
#   those can come from the branch (core.hooksPath into the tree, the
#   pre-commit framework's .pre-commit-config.yaml, lefthook.yml, a filter
#   picked in .gitattributes). Every git command therefore runs in the default
#   reduction; those that talk to a remote (push, fetch, pull, ls-remote,
#   clone, remote, submodule) keep the GitHub token variables a credential
#   helper may read and lose the three .env keys and their copies. Local
#   commands keep running hooks and filters, only without the keys. The remote
#   ones run without hooks (v3.2): `-c core.hooksPath=/dev/null`, because a
#   hook during them — pre-push on a push, reference-transaction on every ref
#   update a fetch or push makes, the hooks of a pull's merge — would see the
#   GitHub tokens. repo.remote_git_runs_hooks: true in .bureau.json (the JSON
#   value true, nothing else) runs them with hooks again, as v3.1 did. The
#   setting covers hooks only: a filter, an fsmonitor or a credential helper
#   the configuration names still runs (SECURITY.md).
#
# bureau_exec_runtime <command> [argument ...]
#   Replaces the shell with the runtime wrapper (python3 bureau-runtime.py
#   … exec …) without BASH_ENV and ENV — every bash below it (the relaunched
#   worker or stage, the stage the worker starts after changing into the
#   branch's worktree) would source a relative BASH_ENV from there — and
#   without those of the three .env keys (and their copies) that the relaunched
#   script reads back from its .env file: set now and defined in
#   BUREAU_ENV_FILE, the only .env a stage reads (v3.2; before, a stage read
#   ./.env first). The runtime is an ancestor of every stage and runs under a
#   Python whose environment `ps -E` can read on macOS; it needs none of the keys. A
#   key that exists only in the calling environment, and the GitHub token
#   variables the stages' gh calls use, pass on unchanged.

# _bureau_env_build <mode> <names> <startup> — sets the array _BUREAU_ENV_ARGV
# to the options /usr/bin/env needs: mode default|clean, names "seven",
# "dotenv" or a list of names, startup 1 to remove BASH_ENV and ENV. Silent.
_bureau_env_build() {
  local IFS=$' \t\n'
  local _beb_mode="$1" _beb_names="$2" _beb_startup="$3" _beb_name _beb_value _beb_exported
  local -a _beb_long _beb_short
  _beb_long=(); _beb_short=(); _BUREAU_ENV_ARGV=()
  case "$_beb_names" in
    seven) _beb_names='LINEAR_API_KEY TELEGRAM_BOT_TOKEN TELEGRAM_ALERT_CHAT_ID GH_TOKEN GITHUB_TOKEN GH_ENTERPRISE_TOKEN GITHUB_ENTERPRISE_TOKEN' ;;
    dotenv) _beb_names='LINEAR_API_KEY TELEGRAM_BOT_TOKEN TELEGRAM_ALERT_CHAT_ID' ;;
  esac
  for _beb_name in $_beb_names; do
    _beb_value="${!_beb_name:-}"
    if [ "${#_beb_value}" -ge 6 ]; then _beb_long+=("$_beb_value")
    elif [ -n "$_beb_value" ]; then _beb_short+=("$_beb_value"); fi
    _BUREAU_ENV_ARGV+=(-u "$_beb_name")
  done
  if [ "$_beb_startup" = 1 ]; then _BUREAU_ENV_ARGV+=(-u BASH_ENV -u ENV); fi
  _beb_exported=$(compgen -e 2>/dev/null || true)
  if [ "$_beb_mode" = clean ]; then
    _BUREAU_ENV_ARGV=(-i)
    for _beb_name in PATH HOME USER LOGNAME SHELL TMPDIR TEMP TMP LANG LC_ALL LC_CTYPE TERM TZ CI; do
      case $'\n'"$_beb_exported"$'\n' in (*$'\n'"$_beb_name"$'\n'*) ;; (*) continue ;; esac
      [ -n "${!_beb_name+x}" ] || continue
      _beb_value="${!_beb_name}"
      _bureau_env_carries "$_beb_value" || _BUREAU_ENV_ARGV+=("$_beb_name=$_beb_value")
    done
  else
    for _beb_name in $_beb_exported; do
      _beb_value="${!_beb_name:-}"
      [ -n "$_beb_value" ] || continue
      if _bureau_env_carries "$_beb_value"; then _BUREAU_ENV_ARGV+=(-u "$_beb_name"); fi
    done
  fi
  return 0
}

# _bureau_env_carries <value> — 0 when <value> carries one of the secrets
# _bureau_env_build collected (its _beb_long and _beb_short, by bash's dynamic
# scope): a long one anywhere inside, a short one as the whole value.
_bureau_env_carries() {
  local _bec_secret
  for _bec_secret in ${_beb_long[@]+"${_beb_long[@]}"}; do
    case "$1" in (*"$_bec_secret"*) return 0 ;; esac
  done
  for _bec_secret in ${_beb_short[@]+"${_beb_short[@]}"}; do
    if [ "$1" = "$_bec_secret" ]; then return 0; fi
  done
  return 1
}

_bureau_untrusted_env_mode() {
  local _bue_filter='.repo.untrusted_env | if . == null then "default" elif . == "default" or . == "clean" then . else "invalid: " + tojson end'
  if declare -F bureau_get >/dev/null 2>&1; then
    bureau_get "$_bue_filter"
  elif [ -n "${BUREAU_CONFIG:-}" ]; then
    jq -r "$_bue_filter" "$BUREAU_CONFIG"
  else
    printf 'default\n'
  fi
}

bureau_untrusted_env() {
  case $- in
    (*x*) set +x; local _bue_trace=1 ;;
    (*) local _bue_trace=0 ;;
  esac
  local _bue_check=0 _bue_mode
  local _bue_assign_re='^[A-Za-z_][A-Za-z0-9_]*='
  local -a _bue_assign
  _bue_assign=()
  if [ "${1:-}" = --check ]; then _bue_check=1; shift; fi

  if ! _bue_mode=$(_bureau_untrusted_env_mode 2>&1) \
     || { [ "$_bue_mode" != default ] && [ "$_bue_mode" != clean ]; }; then
    echo "bureau_untrusted_env: repo.untrusted_env must be absent, \"default\" or \"clean\" (read: ${_bue_mode:-nothing}); the command was not run (24, environment-blocked)" >&2
    if [ "$_bue_trace" = 1 ]; then set -x; fi
    if [ "$_bue_check" = 1 ]; then return 24; fi
    exit 24
  fi
  if [ "$_bue_check" = 1 ]; then
    if [ "$_bue_trace" = 1 ]; then set -x; fi
    return 0
  fi

  while [ "$#" -gt 0 ] && [[ $1 =~ $_bue_assign_re ]]; do
    _bue_assign+=("$1"); shift
  done
  if [ "$#" = 0 ]; then
    echo "bureau_untrusted_env: no command given; nothing was run (24, environment-blocked)" >&2
    if [ "$_bue_trace" = 1 ]; then set -x; fi
    exit 24
  fi

  _bureau_env_build "$_bue_mode" seven 1
  if [ "$_bue_trace" = 1 ]; then set -x; fi
  /usr/bin/env "${_BUREAU_ENV_ARGV[@]}" ${_bue_assign[@]+"${_bue_assign[@]}"} "$@"
}

bureau_without_secrets() {
  case $- in
    (*x*) set +x; local _bws_trace=1 ;;
    (*) local _bws_trace=0 ;;
  esac
  local _bws_assign_re='^[A-Za-z_][A-Za-z0-9_]*='
  local -a _bws_assign
  _bws_assign=()
  while [ "$#" -gt 0 ] && [[ $1 =~ $_bws_assign_re ]]; do
    _bws_assign+=("$1"); shift
  done
  if [ "$#" = 0 ]; then
    echo "bureau_without_secrets: no command given; nothing was run (24, environment-blocked)" >&2
    if [ "$_bws_trace" = 1 ]; then set -x; fi
    exit 24
  fi
  _bureau_env_build default seven 1
  if [ "$_bws_trace" = 1 ]; then set -x; fi
  /usr/bin/env "${_BUREAU_ENV_ARGV[@]}" ${_bws_assign[@]+"${_bws_assign[@]}"} "$@"
}

# _bureau_remote_git_runs_hooks — 0 when repo.remote_git_runs_hooks is the JSON
# value true, 1 otherwise: absent, null, false, any other value, no
# BUREAU_CONFIG, or a .bureau.json jq cannot read (the hooks stay off). Read on
# every call, like repo.untrusted_env; bureau-doctor.py warns on a value that
# is not a JSON boolean.
_bureau_remote_git_runs_hooks() {
  local _brh_filter='if (.repo | type) == "object" and .repo.remote_git_runs_hooks == true then "on" else "off" end' _brh_value
  if declare -F bureau_get >/dev/null 2>&1; then
    _brh_value=$(bureau_get "$_brh_filter" 2>/dev/null) || return 1
  elif [ -n "${BUREAU_CONFIG:-}" ]; then
    _brh_value=$(jq -r "$_brh_filter" "$BUREAU_CONFIG" 2>/dev/null) || return 1
  else
    return 1
  fi
  [ "$_brh_value" = on ]
}

# The hook events git knows (hook-list.h of git 2.55, generated from githooks(5)).
_BUREAU_GIT_HOOK_EVENTS='applypatch-msg commit-msg fsmonitor-watchman p4-changelist p4-post-changelist p4-pre-submit p4-prepare-changelist post-applypatch post-checkout post-commit post-index-change post-merge post-receive post-rewrite post-update pre-applypatch pre-auto-gc pre-commit pre-merge-commit pre-push pre-rebase pre-receive prepare-commit-msg proc-receive push-to-checkout reference-transaction sendemail-validate update'

# _bureau_git_hooks_off [git's own options of the command] — sets the array
# _BUREAU_GIT_HOOKS_OFF to the options that run a git command without any hook
# (v3.2), and _BUREAU_GIT_HOOKS_ENV to the variables they need; uses the
# _BUREAU_ENV_ARGV that _bureau_env_build set for the command:
#   - -c core.hooksPath=/dev/null: no hook from a hooks directory;
#   - -c hook.<event>.enabled=false for every event git knows: no hook the
#     configuration defines (hook.<name>.command and hook.<name>.event, git 2.54
#     and later) for that event, from git 2.55 on;
#   - -c hook.<name>.enabled=false for every <name> with a hook.<name>.command
#     or hook.<name>.event that `git config` lists for the command (with its
#     own -C, -c and --git-dir): git 2.54 knows only this per-name switch, and
#     git 2.55 treats hook.<event>.enabled as per-name too when a hook named
#     like the event exists (hook.pre-push.command). A name with `=` in it
#     cannot follow -c; it goes through --config-env and the variable
#     BUREAU_GIT_HOOK_OFF=false.
# git before 2.54 runs no hook from the configuration and ignores the hook.*
# options.
_bureau_git_hooks_off() {
  local _bgo_event _bgo_keys _bgo_key _bgo_name _bgo_seen=$'\n'
  _BUREAU_GIT_HOOKS_OFF=(-c core.hooksPath=/dev/null)
  _BUREAU_GIT_HOOKS_ENV=()
  for _bgo_event in $_BUREAU_GIT_HOOK_EVENTS; do
    _BUREAU_GIT_HOOKS_OFF+=(-c "hook.$_bgo_event.enabled=false")
  done
  # Key names only (a configuration key holds no newline); no process substitution, so the file
  # still parses in a bash that runs in POSIX mode.
  _bgo_keys=$(/usr/bin/env "${_BUREAU_ENV_ARGV[@]}" git "$@" config --name-only -z --get-regexp '^hook\..+\.(command|event)$' 2>/dev/null | tr '\000' '\n') || true
  while IFS= read -r _bgo_key; do
    [ -n "$_bgo_key" ] || continue
    _bgo_name="${_bgo_key#hook.}"; _bgo_name="${_bgo_name%.*}"
    case "$_bgo_seen" in (*$'\n'"$_bgo_name"$'\n'*) continue ;; esac
    _bgo_seen="$_bgo_seen$_bgo_name"$'\n'
    case "$_bgo_name" in
      *=*) _BUREAU_GIT_HOOKS_OFF+=("--config-env=hook.$_bgo_name.enabled=BUREAU_GIT_HOOK_OFF")
           _BUREAU_GIT_HOOKS_ENV=(BUREAU_GIT_HOOK_OFF=false) ;;
      *) _BUREAU_GIT_HOOKS_OFF+=(-c "hook.$_bgo_name.enabled=false") ;;
    esac
  done <<< "$_bgo_keys"
  return 0
}

git() {
  case $- in
    (*x*) set +x; local _bg_trace=1 ;;
    (*) local _bg_trace=0 ;;
  esac
  local _bg_arg _bg_sub="" _bg_skip=0 _bg_names=seven _bg_lead=0
  local -a _bg_hooks _bg_hooks_env
  _bg_hooks=(); _bg_hooks_env=()
  # The subcommand is the first word after git's own options; -C, -c,
  # --git-dir, --work-tree, --namespace, --super-prefix and --config-env
  # take the next word as their value.
  for _bg_arg in "$@"; do
    if [ "$_bg_skip" = 1 ]; then _bg_skip=0; _bg_lead=$((_bg_lead + 1)); continue; fi
    case "$_bg_arg" in
      -C|-c|--git-dir|--work-tree|--namespace|--super-prefix|--config-env) _bg_skip=1 ;;
      -*) ;;
      *) _bg_sub="$_bg_arg"; break ;;
    esac
    _bg_lead=$((_bg_lead + 1))
  done
  case "$_bg_sub" in
    push|fetch|pull|ls-remote|clone|remote|submodule) _bg_names=dotenv ;;
  esac
  _bureau_env_build default "$_bg_names" 1
  if [ "$_bg_names" = dotenv ] && ! _bureau_remote_git_runs_hooks; then
    # Placed before the caller's own options; git passes them on to the git
    # processes it starts itself (a pull's fetch and merge, submodules).
    _bureau_git_hooks_off "${@:1:$_bg_lead}"
    _bg_hooks=("${_BUREAU_GIT_HOOKS_OFF[@]}")
    _bg_hooks_env=(${_BUREAU_GIT_HOOKS_ENV[@]+"${_BUREAU_GIT_HOOKS_ENV[@]}"})
  fi
  if [ "$_bg_trace" = 1 ]; then set -x; fi
  /usr/bin/env "${_BUREAU_ENV_ARGV[@]}" ${_bg_hooks_env[@]+"${_bg_hooks_env[@]}"} git ${_bg_hooks[@]+"${_bg_hooks[@]}"} "$@"
}

# _bureau_drop_secrets — unsets the seven, their copies, BASH_ENV and ENV in the
# CURRENT shell. Call it only inside a subshell, ( … ) or $( … ): Bureau's own
# Linear and Telegram requests use it right before curl, so curl starts
# without them while a curl defined as a shell function (a test stub) still
# answers.
_bureau_drop_secrets() {
  local _bds_i=0 _bds_n
  _bureau_env_build default seven 1
  _bds_n=${#_BUREAU_ENV_ARGV[@]}
  while [ "$_bds_i" -lt "$_bds_n" ]; do
    if [ "${_BUREAU_ENV_ARGV[$_bds_i]}" = -u ]; then
      unset "${_BUREAU_ENV_ARGV[$((_bds_i + 1))]}" 2>/dev/null || true
      _bds_i=$((_bds_i + 2))
    else
      _bds_i=$((_bds_i + 1))
    fi
  done
  return 0
}

gh() {
  case $- in
    (*x*) set +x; local _bgh_trace=1 ;;
    (*) local _bgh_trace=0 ;;
  esac
  _bureau_env_build default dotenv 1
  if [ "$_bgh_trace" = 1 ]; then set -x; fi
  /usr/bin/env "${_BUREAU_ENV_ARGV[@]}" gh "$@"
}

# _bureau_env_file_defines <file> <name> — 0 when bureau_load_env sets <name>
# from <file>. Runs the real reader in a subshell; prints nothing.
_bureau_env_file_defines() {
  [ -n "$1" ] && [ -f "$1" ] || return 1
  ( unset "$2"; bureau_load_env "$1" >/dev/null 2>&1 && [ -n "${!2:-}" ] )
}

bureau_exec_runtime() {
  case $- in
    (*x*) set +x; local _ber_trace=1 ;;
    (*) local _ber_trace=0 ;;
  esac
  local _ber_names="" _ber_name
  for _ber_name in LINEAR_API_KEY TELEGRAM_BOT_TOKEN TELEGRAM_ALERT_CHAT_ID; do
    [ -n "${!_ber_name:-}" ] || continue
    _bureau_env_file_defines "${BUREAU_ENV_FILE:-}" "$_ber_name" || continue
    _ber_names="$_ber_names $_ber_name"
  done
  _bureau_env_build default "$_ber_names" 1
  if [ "$_ber_trace" = 1 ]; then set -x; fi
  exec /usr/bin/env "${_BUREAU_ENV_ARGV[@]}" "$@"
}
