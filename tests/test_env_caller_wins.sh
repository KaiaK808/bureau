#!/bin/bash
# The caller's environment wins over .env for the keys bureau_load_env reads.
#
# bureau_load_env used to assign every allow-listed key from .env with `printf -v`, so a value the
# operator set on the start line for one run (BUREAU_CODEX_MODEL_DEFAULT=<model> bash
# scripts/shepherd.sh …), which docs/configuration.md recommends for one-off experiments, was
# replaced by the .env value: an installation ran a whole stage on the model in .env. Now a key
# the process got from its caller, set or empty, keeps its value; .env fills only absent keys.
#
#   1  a start-line value beats .env, with and without --export
#   2  an empty start-line value beats .env
#   3  a key the environment lacks is filled from .env; the last entry of the file wins
#   4  loading the same .env twice in one process ends with the values of one load
#   5  the three secrets stay unexported, and .env stays their only source
#   6  parent and child: a key a parent's load took from .env is read from .env again by the
#      child (an edited .env reaches the next stage), a value the parent set afterwards
#      (queue-loop.sh --dry-run) and a start-line value pass through
#   7  a default bureau-config.sh exports itself (BUREAU_STOP_REQUESTED) is not the caller's
#   8  the real model path: the start-line BUREAU_MODEL_IMPLEMENT reaches the provider's argv
#      through bureau-config.sh, bureau_load_env and run_stage_for, in the stage and in a stage
#      a loading parent starts
# Negative control: every case that the old rule breaks (1, 2, 6 and 8) runs again against the
# real file with the caller check switched off (`_bureau_env_from_caller` returning 1, which is
# the old unconditional `printf -v`) and must fail there.
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "$0")" && cd .. && pwd)"
SCRIPTS="$REPO_ROOT/templates/scripts"
SB=$(mktemp -d -t bureau-test.callerwins.XXXXXXXX)
trap 'rm -rf "$SB"' EXIT
FAILS=0
fail() { echo "FAIL $*" >&2; FAILS=$((FAILS + 1)); }

# The old rule: the real reader with the caller check off.
OLD_RULE='_bureau_env_from_caller() { return 1; }'
# The variables a parent shell of this test might export, so every case starts from a known
# environment.
CLEAN=(-u BUREAU_CODEX_MODEL_DEFAULT -u BUREAU_MODEL_IMPLEMENT -u BUREAU_DRY_RUN -u BUREAU_MODEL_QA
       -u BUREAU_STOP_REQUESTED -u BUREAU_NO_MERGE -u BUREAU_CALLER_STOP -u BUREAU_ENV_FILLED
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
  # The parent sets a key after its load (queue-loop.sh --dry-run): the child keeps it.
  out=$(reader "$rule" "bureau_load_env --export .env; export BUREAU_DRY_RUN=1; $child")
  [ "$out" = 'codex=[model-from-file] dry=[1] qa=[last]' ] || echo "6 a value the parent set after its load was replaced: $out"
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
once=$(reader '' "bureau_load_env --export .env; $SHOW; /usr/bin/env | grep '^BUREAU_' | grep -v '^BUREAU_ENV_FILLED=' | sort" BUREAU_DRY_RUN=1)
twice=$(reader '' "bureau_load_env --export .env; bureau_load_env --export .env; $SHOW; /usr/bin/env | grep '^BUREAU_' | grep -v '^BUREAU_ENV_FILLED=' | sort" BUREAU_DRY_RUN=1)
mixed=$(reader '' "bureau_load_env .env; bureau_load_env --export .env; bureau_load_env .env; $SHOW; /usr/bin/env | grep '^BUREAU_' | grep -v '^BUREAU_ENV_FILLED=' | sort" BUREAU_DRY_RUN=1)
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
  out=$(reader '' "bureau_load_env $form .env; bureau_load_env $form .env; /usr/bin/env | grep -c 'lin_from' || true; printf '%s' \"\${BUREAU_ENV_FILLED:-}\" | grep -c lin_from || true")
  [ "$out" = $'0\n0' ] || fail "5 ${form:-plain}: a secret reached the environment or BUREAU_ENV_FILLED: $out"
done
[ "$FAILS" = 0 ] && echo "PASS 5 the three secrets stay unexported, .env stays their only source, BUREAU_ENV_FILLED never holds one"

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

# ── negative control: the old rule ────────────────────────────────────────────
for c in case_start_line case_parent_child case_model; do
  problems=$("$c" "$OLD_RULE")
  [ -n "$problems" ] || fail "negative control: $c passes with the old printf -v rule, so it proves nothing"
done
# What the old rule got wrong, exactly: the start-line value lost, the parent's later value lost.
case "$(case_start_line "$OLD_RULE")" in *'1 --export: the start-line value did not win: codex=[model-from-file]'*) ;;
  *) fail "negative control: the old rule did not lose the start-line value to .env" ;; esac
case "$(case_parent_child "$OLD_RULE")" in *'6 a value the parent set after its load was replaced: codex=[model-from-file] dry=[0]'*) ;;
  *) fail "negative control: the old rule did not replace the parent's later value" ;; esac
case "$(case_model "$OLD_RULE")" in *"8 stage: the provider got model 'implement-from-file'"*) ;;
  *) fail "negative control: the old rule did not run the stage on the .env model" ;; esac
[ "$FAILS" = 0 ] && echo "PASS negative control: with the old printf -v rule the start-line, empty, parent and model cases fail"

[ "$FAILS" = 0 ] || { echo "$FAILS check(s) failed"; exit 1; }
echo "OK test_env_caller_wins"
