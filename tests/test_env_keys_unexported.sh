#!/bin/bash
# The stages keep the .env keys unexported (v3.2, S1).
#
# v3.1 removed LINEAR_API_KEY, TELEGRAM_BOT_TOKEN and TELEGRAM_ALERT_CHAT_ID from every command
# the branch controls, but the stages still exported them (`bureau_load_env --export`), so every
# other process a stage started — its jq, date, Python helpers — carried them, readable through
# /proc/<pid>/environ on Linux and `ps -E` on macOS for executables that are not Apple platform
# binaries. Now bureau_load_env sets the three keys as shell variables of the reading script and
# never exports them (it also removes the export attribute a parent shell gave them); the other
# keys keep --export. The stage's own Linear and Telegram requests read the shell variables and
# hand them to curl on stdin (tests/test_untrusted_env_bureau.sh, section E).
#
#   A  the reader (the REAL bureau-env.sh): with and without --export the keys are set and not
#      exported, the other .env keys are exported with --export; a key the environment exported
#      and .env defines loses its export; a key only the environment holds is left alone; the
#      stages' API_KEY copy is not exported even when the operator's shell exported API_KEY
#   B  the nine REAL stage scripts (tests/lib/harness.sh: stub Linear, real bureau-env.sh, and
#      the real config's `export -n API_KEY` line), each with probes in front of PATH for the
#      tools a stage starts: every process a stage starts after its .env load carries none of
#      the three keys and no API_KEY
#   C  every script that reads a .env reads it through bureau_load_env: no `source`/`.` of an
#      env file, no `set -a`, no `export` of the three names or of API_KEY, in the shell
#      templates and in the inline bash of the Python runtime
#   D  an operator shell that ran `set -a` or `set -x` and exported SHELLOPTS starts every Bureau
#      bash with allexport or xtrace on: the REAL bureau-config.sh (load, the API_KEY copy, the
#      presence check, a Linear request, a Telegram alert, curl doubled) and the nine stages of B
#      export no key and print none in their trace, and the Linear request and the alert still
#      reach curl with the key and the token
# The drivers (shepherd, worker, runtime) run for real in tests/test_env_keys_drivers.sh.
# Negative control: against v3.1.0 (9411b3b) A fails on every "exported" check and B on every
# stage ("a process the script started carries a .env key: … jq LINEAR_API_KEY …"); against
# 735496e D fails ("the trace shows a secret", "API_KEY is exported").
set -uo pipefail
source "$(dirname "$0")/lib/harness.sh"
source "$(dirname "$0")/lib/c1-env-probes.sh"
SCRIPTS="$REPO_ROOT/templates/scripts"
TMP=$(mktemp -d -t bureau-test.c1-env.XXXXXXXX)
trap 'teardown; rm -rf "$TMP"' EXIT

# ── A  the reader ─────────────────────────────────────────────────────────────
c1_env_file "$TMP/.env"
# reader <shell code> — a fresh /bin/bash with the real bureau-env.sh sourced; prints what the
# code prints. The environment holds no key unless the code's caller puts one there.
reader() { env -u LINEAR_API_KEY -u TELEGRAM_BOT_TOKEN -u TELEGRAM_ALERT_CHAT_ID -u API_KEY \
             /bin/bash -c 'set -u; source "$1/bureau-env.sh"; cd "$2"; '"$1" _ "$SCRIPTS" "$TMP" 2>&1; }
# reader_env <shell code> NAME=VALUE … — the same with those variables exported to it.
reader_env() { local code="$1"; shift; env "$@" /bin/bash -c 'set -u; source "$1/bureau-env.sh"; cd "$2"; '"$code" _ "$SCRIPTS" "$TMP" 2>&1; }
for form in '--export' ''; do
  out=$(reader "bureau_load_env $form .env; echo \"shell=\${LINEAR_API_KEY:-}|\${TELEGRAM_BOT_TOKEN:-}|\${TELEGRAM_ALERT_CHAT_ID:-}\"; /usr/bin/env")
  printf '%s\n' "$out" | grep -qxF "shell=$C1_LINEAR|$C1_TG_TOKEN|$C1_TG_CHAT" \
    || c1_fail "A ${form:-plain}: the reading shell does not hold the three keys: $(printf '%s\n' "$out" | head -1)"
  for name in LINEAR_API_KEY TELEGRAM_BOT_TOKEN TELEGRAM_ALERT_CHAT_ID; do
    if printf '%s\n' "$out" | grep -q "^$name="; then c1_fail "A ${form:-plain}: $name is exported to a child"; fi
  done
  if [ "$form" = --export ]; then
    printf '%s\n' "$out" | grep -qxF "$C1_MARK_NAME=$C1_MARK_VALUE" || c1_fail "A --export: a non-secret .env key is no longer exported"
  fi
done
# The environment exported the keys (a parent that still exports them, an operator shell) and
# .env defines them: the file's value, unexported.
for form in '--export' ''; do
  out=$(reader_env "bureau_load_env $form .env; echo \"shell=\$LINEAR_API_KEY\"; /usr/bin/env" \
          LINEAR_API_KEY=from-parent-env TELEGRAM_BOT_TOKEN=from-parent-env TELEGRAM_ALERT_CHAT_ID=from-parent-env)
  printf '%s\n' "$out" | grep -qxF "shell=$C1_LINEAR" || c1_fail "A ${form:-plain} over an exported key: the .env value does not win"
  for name in LINEAR_API_KEY TELEGRAM_BOT_TOKEN TELEGRAM_ALERT_CHAT_ID; do
    if printf '%s\n' "$out" | grep -q "^$name="; then c1_fail "A ${form:-plain} over an exported key: $name stays exported"; fi
  done
done
# A key only the environment holds (not in .env) is not the reader's: left as the shell has it.
printf 'LINEAR_API_KEY=%s\n' "$C1_LINEAR" > "$TMP/only-linear.env"
out=$(reader_env "bureau_load_env --export only-linear.env; /usr/bin/env" TELEGRAM_ALERT_CHAT_ID=env-only-chat)
printf '%s\n' "$out" | grep -qx 'TELEGRAM_ALERT_CHAT_ID=env-only-chat' || c1_fail "A: a key only the environment holds was changed or dropped"
if printf '%s\n' "$out" | grep -q '^LINEAR_API_KEY='; then c1_fail "A: the key .env defines is exported next to an environment-only key"; fi
# The stages copy the key into API_KEY after sourcing bureau-config.sh; an API_KEY the operator's
# shell exported must not carry that copy to the stage's children.
mkdir -p "$TMP/cfg/scripts"; cp "$SCRIPTS"/bureau-config.sh "$SCRIPTS"/bureau-env.sh "$TMP/cfg/scripts/"
printf '{"linear":{"teams":[{"id":"t","key":"EXP","states":{}}],"labels":{}},"agents":{},"repo":{}}\n' > "$TMP/cfg/.bureau.json"
c1_env_file "$TMP/cfg/.env"
out=$(cd "$TMP/cfg" && env API_KEY=operator-value BUREAU_CONFIG="$TMP/cfg/.bureau.json" /bin/bash -c \
  'source scripts/bureau-config.sh; bureau_load_env --export .env; API_KEY="${LINEAR_API_KEY:?}"; echo "shell=$API_KEY"; /usr/bin/env' 2>&1)
printf '%s\n' "$out" | grep -qxF "shell=$C1_LINEAR" || c1_fail "A API_KEY: the stage shell lost its copy"
if printf '%s\n' "$out" | grep -q '^API_KEY='; then c1_fail "A API_KEY: the stage's copy of the Linear key is exported (the operator's shell exported API_KEY)"; fi
[ "$C1_FAILS" = 0 ] && echo "PASS A the reader sets the three keys unexported, exports the rest with --export, and un-exports what a parent exported"

# ── B  the nine stages ────────────────────────────────────────────────────────
B_FAILS_BEFORE=$C1_FAILS
# stage_run <label> <Linear state> <script> [args …] — the real stage in a fresh harness sandbox
# with the probe .env, the operator's API_KEY exported (empty, as tests/lib/pr1-untrusted-env.sh
# does), the probes on PATH and the ticket in <Linear state>, set up so the stage gets past its
# state check (the fixtures of tests/test_spec_dir_stages.sh and the dry-run label tests).
stage_run() {
  local label="$1" state="$2" script="$3"; shift 3
  sandbox_init "EXP-321" "test-branch"
  c1_env_file "$SANDBOX/.env"
  printf '.c1/\n' >> "$SANDBOX/.git/info/exclude"
  printf '# spec\n' > "$SANDBOX/specs/001-test-branch/spec.md"
  git -C "$SANDBOX" add specs; git -C "$SANDBOX" commit -q -m 'fixture spec'; git -C "$SANDBOX" push -q origin test-branch
  printf '```json\n{"status":"GREEN","tests_added":0,"tests_failing":0,"coverage_notes":"fixture"}\n```\n' > "$SANDBOX/qa.txt"
  printf 'Review checked.\n```json\n{"verdict":"APPROVE","bugs":0,"security_issues":0,"findings":[],"summary":"fixture"}\n```\n' > "$SANDBOX/verdict.txt"
  case "$label" in
    qa) jq -n '{repo: {test_command: "true"}}' > "$SANDBOX/.bureau.json" ;;
    copy) printf 'BUREAU_LABEL_NEEDS_COPY_NAME=needs-copy\n' >> "$SCRIPTS_DIR/bureau-config.sh" ;;
  esac
  # The executor's commit lives in the real bureau-config.sh, which the stub replaces (as pr1 does).
  printf 'commit_stage_changes() { _record commit_stage_changes "$@"; return 0; }\n' >> "$SCRIPTS_DIR/bureau-config.sh"
  # The real config's top-level line that un-exports the stages' API_KEY copy, cut from it into
  # the stub as the harness cuts its other helpers.
  grep -x 'export -n API_KEY' "$SCRIPTS/bureau-config.sh" >> "$SCRIPTS_DIR/bureau-config.sh" \
    || c1_fail "B $label: bureau-config.sh no longer un-exports API_KEY"
  c1_probe_tools "$SANDBOX/.c1/bin" "$SANDBOX/.c1/probe.log"
  ( fixture="$FIXTURES_DIR/claude_complete.txt"
    case "$label" in qa) fixture="$SANDBOX/qa.txt" ;; code-review) fixture="$SANDBOX/verdict.txt" ;; spec*|ux|copy) fixture="$FIXTURES_DIR/claude_filler.txt" ;; esac
    export PATH="$SANDBOX/.c1/bin:$PATH" API_KEY="" FAKE_CLAUDE_FIXTURES="$fixture" BUREAU_DRY_RUN=0 \
      BUREAU_STUB_ISSUE_STATE="$state" BUREAU_STUB_STATE_QA=state-qa BUREAU_STUB_STATE_COPY=state-copy \
      BUREAU_STUB_STATE_MERGE=state-merge GH_STUB_EXISTING_PR=99 BUREAU_NO_MERGE=1 BUREAU_STOP_REQUESTED=0
    [ "$label" != merge ] || export BUREAU_NO_MERGE=0
    # D: the shell that starts the stage has these options on and exports SHELLOPTS.
    if [ -n "${C1_SHELLOPTS:-}" ]; then for opt in $C1_SHELLOPTS; do set -o "$opt"; done; export SHELLOPTS; fi
    run_pipeline "$script" "$@" </dev/null
    { set +x; } 2>/dev/null
    printf '%s\n' "$LAST_RC" > "$SANDBOX/.c1/rc"
    cp "$SANDBOX/stderr.log" "$SANDBOX/.c1/stage.err"
    printf '%s\n' "$LAST_STDERR" | tail -3 > "$SANDBOX/.c1/err" ) 2>/dev/null
  c1_check_log "$SANDBOX/.c1/probe.log" "${C1_LABEL:-B} $label (exit $(cat "$SANDBOX/.c1/rc" 2>/dev/null); $(tr '\n' ' ' < "$SANDBOX/.c1/err" 2>/dev/null | cut -c1-200))"
  case " ${C1_SHELLOPTS:-} " in
    *" xtrace "*)
      grep -q '^+' "$SANDBOX/.c1/stage.err" || c1_fail "${C1_LABEL:-B} $label: the stage did not trace (SHELLOPTS not taken)"
      c1_no_secret "$SANDBOX/.c1/stage.err" "${C1_LABEL:-B} $label: the trace shows a secret" ;;
  esac
  teardown
}
stage_run implement Build implement-pipeline.sh
stage_run spec Triage spec-pipeline.sh EXP-321
stage_run spec-review 'Spec Review' spec-review-pipeline.sh EXP-321
stage_run ux Design ux-pipeline.sh EXP-321
stage_run copy Copy copy-pipeline.sh EXP-321
stage_run qa QA qa-pipeline.sh EXP-321
stage_run code-review 'Build Review' code-review-pipeline.sh EXP-321
stage_run merge Merge merge-pipeline.sh EXP-321
stage_run rebase Merge rebase-pipeline.sh
[ "$C1_FAILS" = "$B_FAILS_BEFORE" ] && echo "PASS B the nine stages start every process after their .env load without the three keys"

# ── C  one way to read .env ───────────────────────────────────────────────────
C_FAILS_BEFORE=$C1_FAILS
readers=$(grep -l 'bureau_load_env' "$SCRIPTS"/*.sh | sed 's#.*/##' | sort | tr '\n' ' ')
for f in bureau-status.sh bureau-tick.sh bureau-worker.sh code-review-pipeline.sh complete-issue.sh copy-pipeline.sh \
         crosscheck-specs.sh grab-issue.sh implement-pipeline.sh merge-pipeline.sh qa-pipeline.sh queue-loop-supervised.sh \
         queue-loop.sh rebase-pipeline.sh shepherd.sh spec-pipeline.sh spec-review-pipeline.sh ux-pipeline.sh; do
  case " $readers " in *" $f "*) ;; *) c1_fail "C: $f no longer reads .env through bureau_load_env" ;; esac
done
stray=$(grep -nE '(^|[;&|{( ])(source|\.) +[^ ]*(\.env|ENV_FILE)|set -a|set -o allexport|(export|declare -x|typeset -x)( +-[a-z]+)* +[^#]*\b(LINEAR_API_KEY|TELEGRAM_BOT_TOKEN|TELEGRAM_ALERT_CHAT_ID|API_KEY)\b' \
          "$SCRIPTS"/*.sh "$SCRIPTS"/*.py | grep -vE ':[0-9]+: *#' | grep -v 'export -n API_KEY' || true)
[ -z "$stray" ] || c1_fail "C: a script reads .env past bureau_load_env or exports a key: $stray"
[ "$C1_FAILS" = "$C_FAILS_BEFORE" ] && echo "PASS C every .env reader goes through bureau_load_env, and nothing exports the keys"

# ── D  allexport and xtrace from the operator's shell ─────────────────────────
D_FAILS_BEFORE=$C1_FAILS
# c1_no_secret <file> <label> — none of the three key values in <file>.
c1_no_secret() {
  local v
  for v in "$C1_LINEAR" "$C1_TG_TOKEN" "$C1_TG_CHAT"; do
    if grep -qF -- "$v" "$1"; then c1_fail "$2: $(grep -F -- "$v" "$1" | head -2 | cut -c1-160 | tr '\n' ';')"; fi
  done
}
D="$TMP/d"; mkdir -p "$D/scripts" "$TMP/dbin"; cp "$SCRIPTS"/bureau-config.sh "$SCRIPTS"/bureau-env.sh "$D/scripts/"
git -C "$D" init -q -b main
printf '{"linear":{"teams":[{"id":"t","key":"EXP","states":{}}],"labels":{}},"agents":{},"repo":{}}\n' > "$D/.bureau.json"
c1_env_file "$D/.env"
printf '%s\n' '#!/bin/bash' "cat > \"$TMP/curl.\$\$.stdin\"" "printf '%s' '{\"data\":{\"viewer\":{\"id\":\"u\"}}}'" \
  "bash '$REPO_ROOT/tests/lib/curl-writeout.sh' 200 \"\$@\"" > "$TMP/dbin/curl"
chmod +x "$TMP/dbin/curl"
# The real config in a shell whose options come from SHELLOPTS: the load, the stages' API_KEY copy
# and presence check, a Linear request and a Telegram alert; then what that shell exports.
SECRETS_CODE='source scripts/bureau-config.sh; bureau_load_env --export .env
bureau_secret_copy API_KEY LINEAR_API_KEY
bureau_secret_set LINEAR_API_KEY && echo key-present
_bureau_linear_fetch "{\"query\":\"{ viewer { id } }\"}" >/dev/null || echo linear-failed
alert_telegram EXP-1 qa 1 "probe alert"
echo "copy-length=${#API_KEY}"; /usr/bin/env'
shellopts() {
  rm -f "$TMP"/curl.*.stdin; rm -rf "$D/.git/bureau"   # the alert throttle of the run before
  (cd "$D" && env -u LINEAR_API_KEY -u TELEGRAM_BOT_TOKEN -u TELEGRAM_ALERT_CHAT_ID -u API_KEY PATH="$TMP/dbin:$PATH" \
     BUREAU_CONFIG="$D/.bureau.json" BUREAU_LINEAR_RETRIES=0 SHELLOPTS="$1" /bin/bash -c "$SECRETS_CODE" > "$TMP/d.out" 2>&1)
  cat "$TMP"/curl.*.stdin > "$TMP/d.curl" 2>/dev/null || : > "$TMP/d.curl"
}
d_requests() {  # <label> — the Linear request carried the key and the alert the token, on curl's stdin
  grep -q '^key-present$' "$TMP/d.out" || c1_fail "$1: the shell lost the key"
  grep -qx "copy-length=${#C1_LINEAR}" "$TMP/d.out" || c1_fail "$1: the API_KEY copy is not the key"
  grep -qF "Authorization: $C1_LINEAR" "$TMP/d.curl" || c1_fail "$1: the Linear request did not carry the key on curl's stdin: $(head -c 200 "$TMP/d.curl" | tr '\n' ' ')"
  grep -qF "bot$C1_TG_TOKEN/sendMessage" "$TMP/d.curl" || c1_fail "$1: the alert did not reach curl with the token"
}
shellopts xtrace:braceexpand:hashall:interactive-comments
grep -q '^+' "$TMP/d.out" || c1_fail "D xtrace: the shell did not trace (SHELLOPTS not taken)"
d_requests "D xtrace"
c1_no_secret "$TMP/d.out" "D xtrace: the trace shows a secret"
shellopts allexport:braceexpand:hashall:interactive-comments
d_requests "D allexport"
for name in API_KEY LINEAR_API_KEY TELEGRAM_BOT_TOKEN TELEGRAM_ALERT_CHAT_ID; do
  if grep -q "^$name=" "$TMP/d.out"; then c1_fail "D allexport: $name is exported"; fi
done
c1_no_secret "$TMP/d.out" "D allexport: an exported variable carries a secret"
if grep -q '^BASH_FUNC' "$TMP/d.out"; then c1_fail "D allexport: Bureau's functions are exported: $(grep -c '^BASH_FUNC' "$TMP/d.out") (e.g. $(grep -m1 -o '^BASH_FUNC[^=]*' "$TMP/d.out"))"; fi
if grep -q '^_BUREAU_SCRIPTS_DIR=' "$TMP/d.out"; then c1_fail "D allexport: bureau-config.sh exported its own variables (allexport still on when it started)"; fi
# The reader alone (a script that sources bureau-env.sh only) under allexport: a later assignment
# of the key is not exported. The config without a .env file (the key from the environment) under
# allexport: the Linear request still carries the key (its config line is not exported, so
# _bureau_drop_secrets does not unset it before curl reads it).
out=$(cd "$D" && env SHELLOPTS=allexport:braceexpand:hashall:interactive-comments /bin/bash -c \
  'source scripts/bureau-env.sh; bureau_load_env .env; COPY="$LINEAR_API_KEY"; /usr/bin/env' 2>&1)
if printf '%s\n' "$out" | grep -q '^COPY='; then c1_fail "D allexport: the reader left allexport on (a later copy of the key is exported)"; fi
if printf '%s\n' "$out" | grep -q '^BASH_FUNC'; then c1_fail "D allexport: sourcing bureau-env.sh alone exports its functions"; fi
rm -f "$TMP"/curl.*.stdin; rm -rf "$D/.git/bureau"
(cd "$D" && env PATH="$TMP/dbin:$PATH" BUREAU_CONFIG="$D/.bureau.json" BUREAU_LINEAR_RETRIES=0 LINEAR_API_KEY="$C1_LINEAR" \
   SHELLOPTS=allexport:braceexpand:hashall:interactive-comments BUREAU_ENV_FILE="$D/missing.env" /bin/bash -c \
   'source scripts/bureau-config.sh; _bureau_linear_fetch "{\"query\":\"{ viewer { id } }\"}" >/dev/null' >/dev/null 2>&1)
cat "$TMP"/curl.*.stdin 2>/dev/null | grep -qF "Authorization: $C1_LINEAR" \
  || c1_fail "D allexport: without a .env load the Linear request lost the key (bureau-config.sh left allexport on)"
# bureau_secret_copy: an unset source ends the script with 1 and the message ${source:?} gave;
# --optional copies an empty value and goes on.
out=$(env -u NO_SUCH_KEY /bin/bash -c 'source "$1/bureau-env.sh"; bureau_secret_copy API_KEY NO_SUCH_KEY; echo continued' c1-script "$SCRIPTS" 2>&1); rc=$?
[ "$rc" = 1 ] && [ "$out" = 'c1-script: NO_SUCH_KEY: Set NO_SUCH_KEY in .env' ] || c1_fail "D: bureau_secret_copy of an unset key did not stop with 1: rc=$rc $out"
out=$(env -u NO_SUCH_KEY /bin/bash -c 'source "$1/bureau-env.sh"; API_KEY=x; bureau_secret_copy --optional API_KEY NO_SUCH_KEY; echo "continued=[$API_KEY]"' _ "$SCRIPTS" 2>&1)
[ "$out" = 'continued=[]' ] || c1_fail "D: bureau_secret_copy --optional did not copy the empty value and go on: $out"
# Static: outside bureau-env.sh a key is expanded only on the three lines that run with the trace
# off (the Linear request's config line, the alert's two config lines); everywhere else the
# scripts go through bureau_secret_set and bureau_secret_copy.
expands=$(grep -nE '\$\{?!?(LINEAR_API_KEY|TELEGRAM_BOT_TOKEN|TELEGRAM_ALERT_CHAT_ID|API_KEY)\b' "$SCRIPTS"/*.sh | grep -v '/bureau-env\.sh:' \
  | grep -vE ':[0-9]+: *#' | grep -vF 'auth_config=$(_bureau_curl_config header "Authorization: ${API_KEY:-$LINEAR_API_KEY}")' \
  | grep -vF 'config=$(_bureau_curl_config url "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage"' \
  | grep -vF '_bureau_curl_config data-urlencode "chat_id=${TELEGRAM_ALERT_CHAT_ID}"' || true)
[ -z "$expands" ] || c1_fail "D: a script expands a key where a trace would print it: $(printf '%s' "$expands" | sed "s#$SCRIPTS/##g" | tr '\n' ';')"
# The nine stages of B, started from a shell with both options on and SHELLOPTS exported.
C1_SHELLOPTS="allexport xtrace" C1_LABEL="D allexport+xtrace"
stage_run implement Build implement-pipeline.sh
stage_run spec Triage spec-pipeline.sh EXP-321
stage_run spec-review 'Spec Review' spec-review-pipeline.sh EXP-321
stage_run ux Design ux-pipeline.sh EXP-321
stage_run copy Copy copy-pipeline.sh EXP-321
stage_run qa QA qa-pipeline.sh EXP-321
stage_run code-review 'Build Review' code-review-pipeline.sh EXP-321
stage_run merge Merge merge-pipeline.sh EXP-321
stage_run rebase Merge rebase-pipeline.sh
C1_SHELLOPTS="" C1_LABEL=""
[ "$C1_FAILS" = "$D_FAILS_BEFORE" ] && echo "PASS D with allexport or xtrace from the operator's shell no key is exported or traced, and Linear and Telegram still get them"

if [ "$C1_FAILS" != 0 ]; then echo "$C1_FAILS check(s) failed" >&2; exit 1; fi
echo "OK test_env_keys_unexported"
