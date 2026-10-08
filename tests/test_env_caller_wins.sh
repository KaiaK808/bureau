#!/bin/bash
# The caller's environment wins over .env for the keys bureau_load_env reads.
#
# bureau_load_env used to assign every allow-listed key from .env with `printf -v`, so a value the
# operator set on the start line for one run (BUREAU_CODEX_MODEL_DEFAULT=<model> bash
# scripts/shepherd.sh …), which docs/configuration.md recommends for one-off experiments, was
# replaced by the .env value: an installation ran a whole stage on the model in .env. Now the keys
# the outermost Bureau script found exported (BUREAU_ENV_CALLER, handed down), and the keys a
# parent sets on purpose through bureau_env_caller_export, keep their value; .env fills the rest.
#
#   1  a start-line value beats .env, with and without --export
#   2  an empty start-line value beats .env
#   3  a key the environment lacks is filled from .env; the last entry of the file wins
#   4  loading the same .env twice in one process ends with the values of one load
#   5  the three secrets stay unexported, .env stays their only source, BUREAU_ENV_CALLER never
#      names one
#   6  parent and child: a key a parent's load took from .env, or a default the parent exported,
#      is read from .env again by the child (an edited .env reaches the next stage); a value the
#      parent sets with bureau_env_caller_export (queue-loop.sh --dry-run, the shepherd's
#      BUREAU_FORCE_ALL_AGENTS=1) and a start-line value pass through, also when the explicit
#      value equals the .env value the parent loaded and .env changes afterwards
#   7  a default bureau-config.sh exports itself (BUREAU_STOP_REQUESTED) is not the caller's
#   8  the real model path: the start-line BUREAU_MODEL_IMPLEMENT reaches the provider's argv
#      through bureau-config.sh, bureau_load_env and run_stage_for, in the stage and in a stage
#      a loading parent starts
#   9  a stop request added to .env after a run started reaches the next stage through four levels
#      (supervisor, queue loop, worker, stage, each sourcing the real bureau-config.sh and loading
#      .env the way that script does), although every level above exported BUREAU_STOP_REQUESTED=0
#   10 the call sites: every flag that sets a key for the runs a script starts goes through
#      bureau_env_caller_export
# Negative controls, against the real file with one function replaced:
#   - main's rule (`_bureau_env_from_caller` returning 1, the old unconditional `printf -v`):
#     1, 2, 6 (explicit values) and 8 must fail
#   - "every key exported at load time is the caller's" (the first version of this change):
#     9 must fail, a stop request added to .env is lost below the first level
#   - bureau_env_caller_export as a plain `export`: the explicit cases of 6 must fail
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "$0")" && cd .. && pwd)"
SCRIPTS="$REPO_ROOT/templates/scripts"
SB=$(mktemp -d -t bureau-test.callerwins.XXXXXXXX)
trap 'rm -rf "$SB"' EXIT
FAILS=0
fail() { echo "FAIL $*" >&2; FAILS=$((FAILS + 1)); }

# The old rule: the real reader with the caller check off.
OLD_RULE='_bureau_env_from_caller() { return 1; }'
# Every key exported when the load runs counts as the caller's, inherited copies and defaults too.
EXPORTED_RULE='_bureau_env_from_caller() { case $'"'"'\n'"'"'"$(compgen -e)"$'"'"'\n'"'"' in (*$'"'"'\n'"'"'"$1"$'"'"'\n'"'"'*) return 0 ;; esac; return 1; }'
# A parent's explicit value is a plain export, not marked as the caller's.
PLAIN_EXPORT_RULE='bureau_env_caller_export() { export "$@"; }'
# The variables a parent shell of this test might export, so every case starts from a known
# environment.
CLEAN=(-u BUREAU_CODEX_MODEL_DEFAULT -u BUREAU_MODEL_IMPLEMENT -u BUREAU_DRY_RUN -u BUREAU_MODEL_QA
       -u BUREAU_STOP_REQUESTED -u BUREAU_NO_MERGE -u BUREAU_CALLER_STOP -u BUREAU_ENV_CALLER -u BUREAU_FORCE_ALL_AGENTS
       -u LINEAR_API_KEY -u TELEGRAM_BOT_TOKEN -u TELEGRAM_ALERT_CHAT_ID -u API_KEY
       -u BUREAU_ENV_FILE -u BUREAU_CONFIG -u BASH_ENV -u ENV)

cat > "$SB/.env" <<'EOF'
LINEAR_API_KEY=lin_from_file_000000000001
BUREAU_CODEX_MODEL_DEFAULT=model-from-file
BUREAU_DRY_RUN=0
BUREAU_MODEL_QA=first
BUREAU_MODEL_QA=last
BUREAU_MODEL_IMPLEMENT=implement-from-file
EOF

# reader <rule> <code> [NAME=VALUE …] — a fresh /bin/bash, started with only those variables of
# the list above, that sources the real bureau-env.sh, applies <rule> and runs <code> in $SB.
reader() {
  local rule="$1" code="$2"; shift 2
  (cd "$SB" && env "${CLEAN[@]}" "$@" /bin/bash -c 'set -u; source "$1/bureau-env.sh"; '"$rule"'
'"$code" _ "$SCRIPTS" 2>&1)
}
SHOW='echo "codex=[${BUREAU_CODEX_MODEL_DEFAULT-unset}] dry=[${BUREAU_DRY_RUN-unset}] qa=[${BUREAU_MODEL_QA-unset}]"'

# case_start_line <rule> — 1 and 2; prints one line per failed expectation.
case_start_line() {
  local rule="$1" form out
  for form in --export ''; do
    out=$(reader "$rule" "bureau_load_env $form .env; $SHOW" BUREAU_CODEX_MODEL_DEFAULT=model-start-line)
    [ "$out" = 'codex=[model-start-line] dry=[0] qa=[last]' ] || echo "1 ${form:-plain}: the start-line value did not win: $out"
    out=$(reader "$rule" "bureau_load_env $form .env; $SHOW" BUREAU_CODEX_MODEL_DEFAULT= BUREAU_DRY_RUN=)
    [ "$out" = 'codex=[] dry=[] qa=[last]' ] || echo "2 ${form:-plain}: an empty start-line value did not win: $out"
  done
}

# case_parent_child <rule> — 6.
case_parent_child() {
  local rule="$1" out
  local child='/bin/bash -c '\''set -u; source "$1/bureau-env.sh"; '"$rule"'
bureau_load_env --export .env; '"$SHOW"\'' _ "$1"'
  # The parent (a queue loop, the shepherd) loads --export and starts a stage that loads again.
  out=$(reader "$rule" "bureau_load_env --export .env; $child")
  [ "$out" = 'codex=[model-from-file] dry=[0] qa=[last]' ] || echo "6 child of a loading parent: $out"
  # .env edited while the parent runs: the child reads the new value.
  cp "$SB/.env" "$SB/.env.orig"
  out=$(reader "$rule" "bureau_load_env --export .env; sed 's/^BUREAU_MODEL_QA=last\$/BUREAU_MODEL_QA=edited/' .env.orig > .env; $child")
  cp "$SB/.env.orig" "$SB/.env"
  [ "$out" = 'codex=[model-from-file] dry=[0] qa=[edited]' ] || echo "6 edited .env: the child did not read it again: $out"
  # The parent sets a key on purpose after its load (queue-loop.sh --dry-run): the child keeps it.
  out=$(reader "$rule" "bureau_load_env --export .env; bureau_env_caller_export BUREAU_DRY_RUN=1; $child")
  [ "$out" = 'codex=[model-from-file] dry=[1] qa=[last]' ] || echo "6 a value the parent set after its load was replaced: $out"
  # The explicit value equals what the parent loaded, then .env changes: the explicit value stays.
  out=$(reader "$rule" "bureau_load_env --export .env; bureau_env_caller_export BUREAU_CODEX_MODEL_DEFAULT=model-from-file; sed 's/^BUREAU_CODEX_MODEL_DEFAULT=.*/BUREAU_CODEX_MODEL_DEFAULT=model-b/' .env.orig > .env; $child")
  cp "$SB/.env.orig" "$SB/.env"
  [ "$out" = 'codex=[model-from-file] dry=[0] qa=[last]' ] || echo "6 an explicit value equal to the loaded one lost to an edited .env: $out"
  # The shepherd's BUREAU_FORCE_ALL_AGENTS=1, equal to .env, then .env switches it off: still 1.
  printf 'BUREAU_FORCE_ALL_AGENTS=1\n' >> "$SB/.env"
  out=$(reader "$rule" "bureau_load_env --export .env; bureau_env_caller_export BUREAU_FORCE_ALL_AGENTS=1; sed 's/^BUREAU_FORCE_ALL_AGENTS=1/BUREAU_FORCE_ALL_AGENTS=0/' .env > .env.new; mv .env.new .env; /bin/bash -c 'source \"\$1/bureau-env.sh\"; $rule
bureau_load_env --export .env; echo force=\$BUREAU_FORCE_ALL_AGENTS' _ \"\$1\"")
  cp "$SB/.env.orig" "$SB/.env"
  [ "$out" = 'force=1' ] || echo "6 the shepherd's BUREAU_FORCE_ALL_AGENTS=1 lost to an edited .env: $out"
  # A default the parent exports without marking it (bureau-config.sh's own defaults): .env wins.
  out=$(reader "$rule" "bureau_load_env --export .env; export BUREAU_DRY_RUN=1; $child")
  [ "$out" = 'codex=[model-from-file] dry=[0] qa=[last]' ] || echo "6 a default the parent exported shadowed .env: $out"
  # A start-line value of the parent passes through to the child.
  out=$(reader "$rule" "bureau_load_env --export .env; $child" BUREAU_CODEX_MODEL_DEFAULT=model-start-line)
  [ "$out" = 'codex=[model-start-line] dry=[0] qa=[last]' ] || echo "6 the parent's start-line value did not reach the child: $out"
}

# ── 1, 2 ──────────────────────────────────────────────────────────────────────
problems=$(case_start_line '')
[ -z "$problems" ] && echo "PASS 1 2 a start-line value, empty or not, beats .env with and without --export" \
  || while IFS= read -r line; do fail "$line"; done <<< "$problems"

# ── 3 ─────────────────────────────────────────────────────────────────────────
out=$(reader '' "bureau_load_env --export .env; $SHOW; /usr/bin/printenv BUREAU_MODEL_QA")
[ "$out" = $'codex=[model-from-file] dry=[0] qa=[last]\nlast' ] \
  && echo "PASS 3 an absent key is filled from .env (last entry wins) and exported with --export" \
  || fail "3 an absent key was not filled from .env: $out"

# ── 4 ─────────────────────────────────────────────────────────────────────────
once=$(reader '' "bureau_load_env --export .env; $SHOW; /usr/bin/env | grep '^BUREAU_' | sort" BUREAU_DRY_RUN=1)
twice=$(reader '' "bureau_load_env --export .env; bureau_load_env --export .env; $SHOW; /usr/bin/env | grep '^BUREAU_' | sort" BUREAU_DRY_RUN=1)
mixed=$(reader '' "bureau_load_env .env; bureau_load_env --export .env; bureau_load_env .env; $SHOW; /usr/bin/env | grep '^BUREAU_' | sort" BUREAU_DRY_RUN=1)
case "$once" in 'codex=[model-from-file] dry=[1] qa=[last]'*) ;; *) fail "4 a single load: $once" ;; esac
[ "$once" = "$twice" ] && [ "$once" = "$mixed" ] \
  && echo "PASS 4 loading the same .env twice (or three times, mixed forms) ends with the values of one load" \
  || fail "4 a second load changed the values: once=[$once] twice=[$twice] mixed=[$mixed]"

# ── 5 ─────────────────────────────────────────────────────────────────────────
for form in --export ''; do
  out=$(reader '' "bureau_load_env $form .env; echo \"key=\$LINEAR_API_KEY\"; /usr/bin/env | grep -c '^LINEAR_API_KEY=' || true" \
          LINEAR_API_KEY=lin_from_parent_env_0000002)
  [ "$out" = $'key=lin_from_file_000000000001\n0' ] \
    || fail "5 ${form:-plain}: the Linear key is not the file's unexported value: $(printf '%s' "$out" | sed 's/lin_[a-z_0-9]*/<key>/g' | tr '\n' ' ')"
  out=$(reader '' "bureau_load_env $form .env; bureau_load_env $form .env; /usr/bin/env | grep -c 'lin_from' || true; echo \"caller=[\$BUREAU_ENV_CALLER]\"" \
          LINEAR_API_KEY=lin_from_parent_env_0000002 TELEGRAM_BOT_TOKEN=x BUREAU_DRY_RUN=1)
  [ "$out" = $'0\ncaller=[ BUREAU_DRY_RUN ]' ] || fail "5 ${form:-plain}: a secret reached the environment or BUREAU_ENV_CALLER: $out"
done
out=$(reader '' "bureau_env_caller_export LINEAR_API_KEY=x; echo \"caller=[\$BUREAU_ENV_CALLER]\"")
[ "$out" = 'caller=[ ]' ] || fail "5 bureau_env_caller_export put a secret on BUREAU_ENV_CALLER: $out"
[ "$FAILS" = 0 ] && echo "PASS 5 the three secrets stay unexported, .env stays their only source, BUREAU_ENV_CALLER never names one"

# ── 6 ─────────────────────────────────────────────────────────────────────────
problems=$(case_parent_child '')
[ -z "$problems" ] && echo "PASS 6 a child re-reads what its parent took from .env, and keeps what the parent or the start line set" \
  || while IFS= read -r line; do fail "$line"; done <<< "$problems"

# ── 7 ─────────────────────────────────────────────────────────────────────────
mkdir -p "$SB/cfg/scripts"; cp -R "$SCRIPTS"/. "$SB/cfg/scripts/"
git -C "$SB/cfg" init -q
printf '{"linear":{"teams":[{"id":"t","key":"EXP","states":{}}],"labels":{}},"agents":{},"repo":{}}\n' > "$SB/cfg/.bureau.json"
printf 'BUREAU_STOP_REQUESTED=1\n' > "$SB/cfg/.env"
out=$(cd "$SB/cfg" && env "${CLEAN[@]}" /bin/bash -c 'source scripts/bureau-config.sh; bureau_load_env --export "$BUREAU_ENV_FILE"; echo "stop=$BUREAU_STOP_REQUESTED"' 2>&1)
[ "$out" = 'stop=1' ] && echo "PASS 7 a default bureau-config.sh exports itself does not shadow .env" \
  || fail "7 bureau-config.sh's own BUREAU_STOP_REQUESTED default hid the .env value: $out"

# ── 8  the real model path ────────────────────────────────────────────────────
mkdir -p "$SB/bin"
cat > "$SB/bin/claude" <<EOF
#!/bin/bash
if [ "\$1" = auth ]; then echo '{"loggedIn": true}'; exit 0; fi
: > "$SB/argv"
for a in "\$@"; do printf '%s\n' "\$a" >> "$SB/argv"; done
cat >/dev/null
echo '{"type":"result","subtype":"success","is_error":false,"result":"ok"}'
EOF
chmod +x "$SB/bin/claude"
cp "$SB/.env" "$SB/cfg/.env"
# model_run <rule> <nested 0|1> [NAME=VALUE …] — the model the stub claude got from
# run_stage_for implement, in a shell that loaded .env (nested 1: in a stage that a loading
# parent started).
model_run() {
  local rule="$1" nested="$2"; shift 2
  local stage='source scripts/bureau-config.sh; '"$rule"'
bureau_load_env --export "$BUREAU_ENV_FILE"; run_stage_for implement "implement this"'
  rm -f "$SB/argv"
  if [ "$nested" = 1 ]; then
    (cd "$SB/cfg" && env "${CLEAN[@]}" PATH="$SB/bin:$PATH" "$@" /bin/bash -c 'source scripts/bureau-config.sh; '"$rule"'
bureau_load_env --export "$BUREAU_ENV_FILE"; /bin/bash -c "$1"' _ "$stage" </dev/null >/dev/null 2>&1) || true
  else
    (cd "$SB/cfg" && env "${CLEAN[@]}" PATH="$SB/bin:$PATH" "$@" /bin/bash -c "$stage" </dev/null >/dev/null 2>&1) || true
  fi
  [ -f "$SB/argv" ] && awk 'prev == "--model" { print; exit } { prev = $0 }' "$SB/argv" || echo 'claude-not-started'
}
case_model() {
  local rule="$1" got
  got=$(model_run "$rule" 0 BUREAU_MODEL_IMPLEMENT=implement-start-line)
  [ "$got" = implement-start-line ] || echo "8 stage: the provider got model '$got', not the start-line value"
  got=$(model_run "$rule" 1 BUREAU_MODEL_IMPLEMENT=implement-start-line)
  [ "$got" = implement-start-line ] || echo "8 stage under a loading parent: the provider got model '$got', not the start-line value"
}
got=$(model_run '' 0)
[ "$got" = implement-from-file ] || fail "8 without a start-line value the provider got '$got', not the .env model"
problems=$(case_model '')
[ -z "$problems" ] && echo "PASS 8 the start-line model reaches the provider through the real load and run_stage_for, also under a loading parent" \
  || while IFS= read -r line; do fail "$line"; done <<< "$problems"

# ── 9  a stop request added to .env reaches the next stage ────────────────────
# Each level sources the real bureau-config.sh and loads .env as that script does (supervisor and
# queue loop: `bureau_load_env --export "${BUREAU_ENV_FILE:-…}"`, worker: only when the file
# exists), then starts the next. The operator added the stop request after the supervisor and
# the queue loop had started.
case_chain() {
  local rule="$1"
  printf 'LINEAR_API_KEY=lin_x\n' > "$SB/cfg/.env"
  (cd "$SB/cfg" && env "${CLEAN[@]}" /bin/bash -c '
    level() { printf "%s\n" "source scripts/bureau-config.sh; $RULE"; }
    export RULE="$1"
    SUPERVISOR="$(level)
bureau_load_env --export \"\${BUREAU_ENV_FILE}\"
/bin/bash -c \"\$QUEUE\""
    export QUEUE="$(level)
bureau_load_env --export \"\${BUREAU_ENV_FILE}\"
printf \"BUREAU_STOP_REQUESTED=1\\n\" >> \"\$BUREAU_ENV_FILE\"
/bin/bash -c \"\$WORKER\""
    export WORKER="$(level)
if [ -f \"\${BUREAU_ENV_FILE:-}\" ]; then bureau_load_env --export \"\$BUREAU_ENV_FILE\" || true; fi
/bin/bash -c \"\$STAGE\""
    export STAGE="$(level)
if [ -f \"\$BUREAU_ENV_FILE\" ]; then bureau_load_env --export \"\$BUREAU_ENV_FILE\"; fi
if bureau_stop_requested; then echo stage=stops; else echo stage=merges; fi"
    /bin/bash -c "$SUPERVISOR"' _ "$rule" 2>&1)
  cp "$SB/.env" "$SB/cfg/.env"   # the .env of 8, which the controls below run again
}
out=$(case_chain '')
[ "$out" = 'stage=stops' ] && echo "PASS 9 a stop request added to .env after start reaches the stage through supervisor, queue loop and worker" \
  || fail "9 the stop request added to .env did not reach the stage: $out"

# ── 10  the call sites ────────────────────────────────────────────────────────
sites=$(grep -nE '^[[:space:]]*(--dry-run\)|--no-merge\)|--allow-merge\)|export BUREAU_FORCE_ALL_AGENTS|\[ "\$NO_MERGE" = 1 \])|^[[:space:]]*bureau_env_caller_export' \
          "$SCRIPTS"/queue-loop.sh "$SCRIPTS"/shepherd.sh "$SCRIPTS"/bureau-tick.sh | grep -E 'BUREAU_[A-Z_]+=' || true)
bad=$(printf '%s\n' "$sites" | grep -v 'bureau_env_caller_export' || true)
count=$(printf '%s\n' "$sites" | grep -c 'bureau_env_caller_export' || true)
grep -qx '  bureau_env_caller_export BUREAU_FORCE_ALL_AGENTS=1' "$SCRIPTS/shepherd.sh" || bad="$bad shepherd BUREAU_FORCE_ALL_AGENTS"
[ -z "$bad" ] && [ "$count" = 7 ] && echo "PASS 10 every flag that sets a key for the runs a script starts marks it with bureau_env_caller_export" \
  || fail "10 a call site exports a key for its children without bureau_env_caller_export (found $count): $bad"

# ── negative controls ─────────────────────────────────────────────────────────
for c in case_start_line case_parent_child case_model; do
  problems=$("$c" "$OLD_RULE")
  [ -n "$problems" ] || fail "negative control: $c passes with the old printf -v rule, so it proves nothing"
done
# What the old rule got wrong, exactly: the start-line value lost, the parent's explicit value lost.
case "$(case_start_line "$OLD_RULE")" in *'1 --export: the start-line value did not win: codex=[model-from-file]'*) ;;
  *) fail "negative control: the old rule did not lose the start-line value to .env" ;; esac
case "$(case_parent_child "$OLD_RULE")" in *'6 a value the parent set after its load was replaced: codex=[model-from-file] dry=[0]'*) ;;
  *) fail "negative control: the old rule did not replace the parent's explicit value" ;; esac
case "$(case_model "$OLD_RULE")" in *"8 stage: the provider got model 'implement-from-file'"*) ;;
  *) fail "negative control: the old rule did not run the stage on the .env model" ;; esac
# Every exported key counted as the caller's: the parents' exported 0 hides the stop request.
[ "$(case_chain "$EXPORTED_RULE")" = 'stage=merges' ] \
  || fail "negative control: with every exported key counted as the caller's the stop request still reached the stage: $(case_chain "$EXPORTED_RULE")"
[ "$(case_chain "$OLD_RULE")" = 'stage=stops' ] || fail "control: main's rule should let the stop request through: $(case_chain "$OLD_RULE")"
# The explicit export as a plain export: both equal-value cases lose to the edited .env.
problems=$(case_parent_child "$PLAIN_EXPORT_RULE")
case "$problems" in *'6 an explicit value equal to the loaded one lost to an edited .env'*) ;;
  *) fail "negative control: a plain export kept the equal explicit value: $problems" ;; esac
case "$problems" in *"6 the shepherd's BUREAU_FORCE_ALL_AGENTS=1 lost to an edited .env: force=0"*) ;;
  *) fail "negative control: a plain export kept the shepherd's value: $problems" ;; esac
[ "$FAILS" = 0 ] && echo "PASS negative controls: main's rule fails 1, 2, 6 and 8; counting every exported key fails 9; a plain export fails the explicit cases of 6"

[ "$FAILS" = 0 ] || { echo "$FAILS check(s) failed"; exit 1; }
echo "OK test_env_caller_wins"
