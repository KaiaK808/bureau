#!/bin/bash
# A run started through tmux sees exactly the overrides of the start that opened its pane.
#
# A tmux pane gets the environment of the tmux server (and session), not that of the script that
# runs `tmux new-window`. The server keeps the environment of the start that created it, so before
# bureau_env_handover a shepherd window inherited an earlier start's override and its
# BUREAU_ENV_CALLER list, and a new start-line override never reached the window. The shepherd's
# tmux wrapper now puts `/usr/bin/env BUREAU_ENV_HANDOVER=1 BUREAU_ENV_CALLER='…' NAME='value' …` on
# the window's command line, and the first Bureau script in the window drops every inherited key
# that list does not name.
#
# The real shepherd.sh runs up to its tmux wrapper against a stub `tmux` that, like a real server,
# keeps the environment of the start that created it and runs each window's command in it, with
# the shepherd replaced by a probe that loads .env as the shepherd does and prints the model.
#   (i)   a session created with override A, .env changed to B, a new start without override → B
#   (ii)  a session created without override, a new start with override C → C, also when C holds
#         spaces, quotes and `=`
#   (iii) no secret is put on the window's command line
#   (iv)  start-agents.sh and start-bureau-v2.sh hand over on every command they type into a pane
#         that runs Bureau or an agent
#   (v)   a forged BUREAU_ENV_CALLER (code from a branch can write the environment) whose entries
#         are subscripts with command substitutions, `NAME[0]`, `NAME;cmd`, `NAME$(cmd)` or a
#         name with a newline: nothing runs (a sentinel file stays absent), no secret reaches
#         stderr, one line reports the dropped entries without printing them, and a plain listed
#         name still counts; inherited as a list and arriving through a hand-over
#   (vi)  a list, a key and the hand-over flag a branch wrote into the tmux server's environment:
#         the shepherd's start clears them, its window and a window opened by hand afterwards
#         read .env
#   (vii) BUREAU_CALLER_STOP, which bureau-config.sh derives from BUREAU_NO_MERGE, holds for its
#         own start only: an earlier no-merge start does not stop a later one, and the reverse
# Negative controls: without the hand-over and the tmux clear (the wrapper of an earlier version)
# (i) and (ii) fail; a reader copy without the name check runs the forged entries of (v); without
# the tmux clear (vi) fails; with no derived keys (vii) fails. Against the previous head, (v), (vi)
# and (vii) fail as well.
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "$0")" && cd .. && pwd)"
SCRIPTS="$REPO_ROOT/templates/scripts"
SB=$(mktemp -d -t bureau-test.tmuxhandover.XXXXXXXX)
trap 'rm -rf "$SB"' EXIT
FAILS=0
fail() { echo "FAIL $*" >&2; FAILS=$((FAILS + 1)); }

CLEAN=(-u BUREAU_CODEX_MODEL_DEFAULT -u BUREAU_MODEL_IMPLEMENT -u BUREAU_DRY_RUN -u BUREAU_STOP_REQUESTED
       -u BUREAU_NO_MERGE -u BUREAU_CALLER_STOP -u BUREAU_ENV_CALLER -u BUREAU_ENV_HANDOVER
       -u BUREAU_FORCE_ALL_AGENTS -u BUREAU_SESSION -u BUREAU_SESSION_NAME -u LINEAR_API_KEY
       -u TELEGRAM_BOT_TOKEN -u TELEGRAM_ALERT_CHAT_ID -u API_KEY -u BUREAU_ENV_FILE -u BUREAU_CONFIG
       -u BASH_ENV -u ENV -u TMUX)

# The stub tmux. The first call that starts a session records the caller's environment as the
# server's (plain exports: tmux keeps no readonly variables); show-environment and
# set-environment read and change it (one store for the global and every session environment);
# every window or typed command then runs in a fresh shell holding only that environment, as a
# pane does, with the shepherd swapped for the probe.
mkdir -p "$SB/bin"
cat > "$SB/bin/tmux" <<EOF
#!/bin/bash
D="$SB/tmuxd"; mkdir -p "\$D"
run_pane() {
  local cmd="\$1"
  cmd="\${cmd//scripts\/shepherd.sh/scripts/probe.sh}"
  printf '%s\n' "\$cmd" >> "\$D/commands"
  (cd "$SB/repo" && env -i /bin/bash -c 'source "\$1"; exec /bin/sh -c "\$2"' _ "\$D/server.sh" "\$cmd")
}
start_server() {
  [ ! -e "\$D/server.sh" ] || return 0
  : > "\$D/server.sh"
  while IFS= read -r -d '' kv; do
    n="\${kv%%=*}"
    [[ \$n =~ ^[A-Za-z_][A-Za-z0-9_]*\$ ]] || continue
    printf 'export %s=%q\n' "\$n" "\${kv#*=}" >> "\$D/server.sh"
  done < <(/usr/bin/env -0)
}
case "\$1" in
  has-session) [ -e "\$D/session.\$3" ] ;;
  show-environment)
    [ -e "\$D/server.sh" ] || exit 1
    env -i /bin/bash -c 'source "\$1"; /usr/bin/env' _ "\$D/server.sh" ;;
  set-environment)
    shift; unset_it=0
    while [ "\$#" -gt 0 ]; do case "\$1" in -g) shift ;; -t) shift 2 ;; -t*) shift ;; -u) unset_it=1; shift ;; *) break ;; esac; done
    start_server
    if [ "\$unset_it" = 1 ]; then printf 'unset %s\n' "\$1" >> "\$D/server.sh"
    else printf 'export %s=%q\n' "\$1" "\$2" >> "\$D/server.sh"; fi ;;
  new-session)
    start_server
    shift; name=""; cmd=""
    while [ "\$#" -gt 0 ]; do case "\$1" in -s) name="\$2"; shift 2 ;; -c|-n|-x|-y|-t) shift 2 ;; -d) shift ;; *) cmd="\$1"; shift ;; esac; done
    : > "\$D/session.\$name"
    [ -z "\$cmd" ] || run_pane "\$cmd" ;;
  new-window)
    shift; cmd=""
    while [ "\$#" -gt 0 ]; do case "\$1" in -c|-n|-t) shift 2 ;; *) cmd="\$1"; shift ;; esac; done
    [ -z "\$cmd" ] || run_pane "\$cmd" ;;
  send-keys) run_pane "\$4" ;;
  *) exit 0 ;;
esac
EOF
chmod +x "$SB/bin/tmux"

mkdir -p "$SB/repo/scripts"
cp -R "$SCRIPTS"/. "$SB/repo/scripts/"
git -C "$SB/repo" init -q
printf '{"linear":{"teams":[{"id":"t","key":"TEAM","states":{}}],"labels":{}},"agents":{},"repo":{}}\n' > "$SB/repo/.bureau.json"
# The probe stands in for the shepherd inside the window: the same config, the same load.
cat > "$SB/repo/scripts/probe.sh" <<'EOF'
#!/bin/bash
source "$(dirname "$0")/bureau-config.sh"
bureau_load_env --export "$BUREAU_ENV_FILE"
printf '%s\n' "${BUREAU_CODEX_MODEL_DEFAULT-unset}" > "$(dirname "$0")/../probe.out"
if bureau_stop_requested; then echo stops; else echo merges; fi > "$(dirname "$0")/../probe.stop"
EOF
chmod +x "$SB/repo/scripts/probe.sh"
cp "$SB/repo/scripts/bureau-config.sh" "$SB/repo/scripts/bureau-config.sh.orig"

# start <rule> [NAME=VALUE …] — the real shepherd from a fresh shell with those variables; prints
# the model the probe saw in the window.
start() {
  local rule="$1"; shift
  cp "$SB/repo/scripts/bureau-config.sh.orig" "$SB/repo/scripts/bureau-config.sh"
  [ -z "$rule" ] || printf '%s\n' "$rule" >> "$SB/repo/scripts/bureau-config.sh"
  rm -f "$SB/repo/probe.out"
  (cd "$SB/repo" && env "${CLEAN[@]}" PATH="$SB/bin:$PATH" "$@" /bin/bash scripts/shepherd.sh TEAM-1 </dev/null >/dev/null 2>"$SB/shepherd.err") \
    || { echo "shepherd-failed: $(tr '\n' ' ' < "$SB/shepherd.err" | cut -c1-200)"; return; }
  cat "$SB/repo/probe.out" 2>/dev/null || echo 'window-did-not-run'
}
env_model() { printf 'LINEAR_API_KEY=lin_file_secret_0001\nBUREAU_CODEX_MODEL_DEFAULT=%s\n' "$1" > "$SB/repo/.env"; }
fresh_server() { rm -rf "$SB/tmuxd"; }

ODD="model c \"q\" 'x' k=v"
# cases <rule> — prints one line per failed expectation.
cases() {
  local rule="$1" got
  fresh_server; env_model model-a
  got=$(start "$rule" BUREAU_CODEX_MODEL_DEFAULT=override-a)
  [ "$got" = override-a ] || echo "setup: the first window did not get its own override: $got"
  env_model model-b
  got=$(start "$rule")
  [ "$got" = model-b ] || echo "(i) a new start without override kept an earlier start's override: $got"
  fresh_server; env_model model-a
  got=$(start "$rule")
  [ "$got" = model-a ] || echo "setup: the first window did not read .env: $got"
  got=$(start "$rule" BUREAU_CODEX_MODEL_DEFAULT=override-c)
  [ "$got" = override-c ] || echo "(ii) a new start's override did not reach the existing session: $got"
  got=$(start "$rule" BUREAU_CODEX_MODEL_DEFAULT="$ODD")
  [ "$got" = "$ODD" ] || echo "(ii) an override with spaces, quotes and = changed on the way: $got"
}

problems=$(cases '')
[ -z "$problems" ] && echo "PASS (i) (ii) a tmux window sees the overrides of its own start, not those the tmux server kept" \
  || while IFS= read -r line; do fail "$line"; done <<< "$problems"

# (iii) The secret the start shell exports never reaches the window's command line.
fresh_server; env_model model-a; rm -f "$SB/tmuxd/commands"
start '' LINEAR_API_KEY=lin_exported_secret_0002 BUREAU_CODEX_MODEL_DEFAULT=override-a >/dev/null
if grep -q 'lin_' "$SB/tmuxd/commands" 2>/dev/null; then fail "(iii) a secret is on the window's command line"
elif ! grep -qE "BUREAU_ENV_HANDOVER=1 BUREAU_ENV_CALLER=' BUREAU_CODEX_MODEL_DEFAULT ' BUREAU_CONFIG='[^']*/repo/.bureau.json' BUREAU_ENV_FILE='[^']*/repo/.env' BUREAU_CODEX_MODEL_DEFAULT='override-a'" "$SB/tmuxd/commands" 2>/dev/null; then
  fail "(iii) the window's command line does not carry the handover: $(cat "$SB/tmuxd/commands" 2>/dev/null)"
else echo "PASS (iii) the handover carries the list and the values, never a secret"; fi

# (iv) Every command the start scripts type into a Bureau or agent pane starts with the handover.
bad=$(grep -nE 'tmux send-keys .*"(\./scripts/|\$BENCH_RUNNER)' "$SCRIPTS"/start-agents.sh "$SCRIPTS"/start-bureau-v2.sh | grep -v '\$(bureau_env_handover)' || true)
count=$(grep -cE 'tmux send-keys .*"\$\(bureau_env_handover\)' "$SCRIPTS"/start-agents.sh "$SCRIPTS"/start-bureau-v2.sh | awk -F: '{ s += $2 } END { print s }')
grep -qx 'bureau_env_caller_export BUREAU_SESSION="$SESSION"' "$SCRIPTS/start-bureau-v2.sh" || bad="$bad start-bureau-v2 BUREAU_SESSION"
[ -z "$bad" ] && [ "$count" = 5 ] && echo "PASS (iv) start-agents.sh and start-bureau-v2.sh hand over on every Bureau or agent pane" \
  || fail "(iv) a pane command without bureau_env_handover (found $count): $bad"

# (v) A forged BUREAU_ENV_CALLER: every entry that is not a plain name on the key list is dropped
# before anything expands it. Each case runs in a fresh bash that sources the reader, loads .env,
# prints the hand-over and exports a key, with the forged list and BUREAU_MODEL_A=model inherited,
# once as an inherited list and once arriving through a hand-over.
SENT="$SB/sentinel"
FORGED=(
  'BUREAU_MODEL_A[$(printf${IFS}INJECTED>&2)]'
  'BUREAU_MODEL_A[$LINEAR_API_KEY]'
  'BUREAU_MODEL_A[$(printf${IFS}%s${IFS}"$LINEAR_API_KEY">&2)]'
  'BUREAU_MODEL_A[$(touch${IFS}'"$SENT"')]'
  'BUREAU_MODEL_A[0]'
  'BUREAU_MODEL_A;touch${IFS}'"$SENT"
  'BUREAU_MODEL_A$(touch${IFS}'"$SENT"')'
  $'BUREAU_MODEL_A\n$(touch${IFS}'"$SENT"')'
  'BUREAU_MODEL_A[$(touch '"$SENT"')]'
)
printf 'LINEAR_API_KEY=lin_file_secret_0003\nBUREAU_MODEL_A=from-file\n' > "$SB/forged.env"
forged_case() {  # <reader dir> <entry> <handover 0|1> — prints "stderr|value of BUREAU_MODEL_A"
  local dir="$1" entry="$2" handover="$3" out
  local -a extra=()
  [ "$handover" = 0 ] || extra=(BUREAU_ENV_HANDOVER=1)
  out=$(cd "$SB" && env "${CLEAN[@]}" ${extra[@]+"${extra[@]}"} BUREAU_MODEL_A=model "BUREAU_ENV_CALLER= $entry BUREAU_MODEL_A " \
          /bin/bash -c 'source "$1/bureau-env.sh"; bureau_load_env forged.env; bureau_env_handover >/dev/null
            bureau_env_caller_export BUREAU_DRY_RUN=1; printf "|%s" "$BUREAU_MODEL_A"' _ "$dir" 2>&1)
  printf '%s' "$out"
}
forged_problems() {  # <reader dir> — one line per entry that ran something, leaked or was not reported
  local dir="$1" entry handover out
  for entry in "${FORGED[@]}"; do
    for handover in 0 1; do
      rm -f "$SENT"
      out=$(forged_case "$dir" "$entry" "$handover")
      [ ! -e "$SENT" ] || echo "(v) a forged entry ran a command (handover=$handover): $(printf '%q' "$entry")"
      case "$out" in *INJECTED*) echo "(v) a forged entry printed its command's output (handover=$handover)" ;; esac
      case "$out" in *lin_file_secret*) echo "(v) a forged entry put the secret on stderr (handover=$handover)" ;; esac
      case "$out" in *"BUREAU_MODEL_A["*|*'$('*|*';touch'*) echo "(v) the warning printed the forged entry (handover=$handover)" ;; esac
      [ "$(grep -c 'entries that are not keys on the .env key list were ignored' <<< "$out")" = 1 ] \
        || echo "(v) the dropped entry was not reported exactly once (handover=$handover): $(printf '%q' "$out" | cut -c1-200)"
      case "$out" in *'|model') ;; *) echo "(v) the plain listed key no longer won (handover=$handover): $(printf '%q' "$out" | cut -c1-200)" ;; esac
    done
  done
}
problems=$(forged_problems "$SCRIPTS")
[ -z "$problems" ] && echo "PASS (v) forged entries in BUREAU_ENV_CALLER are dropped unexpanded, reported once, and leak nothing" \
  || while IFS= read -r line; do fail "$line"; done <<< "$problems"

# (vi) Code from a branch wrote a list, a key and the hand-over flag into the tmux server's
# environment. A start through the shepherd clears them, so a window the operator opens by hand
# afterwards, and runs a Bureau script in without any hand-over, reads .env.
manual_pane() {  # prints the model a hand-typed probe sees in a new pane of the server
  rm -f "$SB/repo/probe.out"
  (cd "$SB/repo" && env "${CLEAN[@]}" PATH="$SB/bin:$PATH" tmux new-window -t x: "scripts/probe.sh" >/dev/null 2>&1)
  cat "$SB/repo/probe.out" 2>/dev/null || echo 'window-did-not-run'
}
case_tmux_forged() {
  local rule="$1" got
  fresh_server; env_model model-a
  got=$(start "$rule")
  [ "$got" = model-a ] || echo "setup: the first window did not read .env: $got"
  PATH="$SB/bin:$PATH" tmux set-environment -g BUREAU_ENV_CALLER ' BUREAU_CODEX_MODEL_DEFAULT '
  PATH="$SB/bin:$PATH" tmux set-environment -g BUREAU_CODEX_MODEL_DEFAULT forged-by-branch
  PATH="$SB/bin:$PATH" tmux set-environment -g BUREAU_ENV_HANDOVER 1
  got=$(start "$rule")
  [ "$got" = model-a ] || echo "(vi) the shepherd's window took the forged value: $got"
  got=$(manual_pane)
  [ "$got" = model-a ] || echo "(vi) a window opened by hand after the start took the forged value: $got"
  if grep -qE '^(BUREAU_ENV_CALLER|BUREAU_ENV_HANDOVER|BUREAU_CODEX_MODEL_DEFAULT)=' <<< "$(PATH="$SB/bin:$PATH" tmux show-environment -g)"; then
    echo "(vi) the tmux environment still holds the forged list, flag or key"
  fi
}
problems=$(case_tmux_forged '')
[ -z "$problems" ] && echo "PASS (vi) a start clears what a branch wrote into the tmux environment; a window opened by hand later reads .env" \
  || while IFS= read -r line; do fail "$line"; done <<< "$problems"

# (vii) The merge-stop boundary is the start's own: BUREAU_CALLER_STOP, which bureau-config.sh
# derives from BUREAU_NO_MERGE and BUREAU_STOP_REQUESTED, does not outlive the start that set it.
stop_of() { cat "$SB/repo/probe.stop" 2>/dev/null || echo 'window-did-not-run'; }
case_stop() {
  local rule="$1"
  fresh_server; env_model model-a
  start "$rule" BUREAU_NO_MERGE=1 >/dev/null
  [ "$(stop_of)" = stops ] || echo "setup: a start with BUREAU_NO_MERGE=1 does not stop: $(stop_of)"
  start "$rule" BUREAU_NO_MERGE=0 BUREAU_STOP_REQUESTED=0 >/dev/null
  [ "$(stop_of)" = merges ] || echo "(vii) a start with BUREAU_NO_MERGE=0 kept the earlier start's stop: $(stop_of)"
  start "$rule" >/dev/null
  [ "$(stop_of)" = merges ] || echo "(vii) a start without the flag kept the earlier start's stop: $(stop_of)"
  fresh_server
  start "$rule" >/dev/null
  [ "$(stop_of)" = merges ] || echo "setup: a start without the flag stops: $(stop_of)"
  start "$rule" BUREAU_NO_MERGE=1 >/dev/null
  [ "$(stop_of)" = stops ] || echo "(vii) a start with BUREAU_NO_MERGE=1 into an existing session does not stop: $(stop_of)"
}
problems=$(case_stop '')
[ -z "$problems" ] && echo "PASS (vii) a merge stop holds for its own start only, in both directions" \
  || while IFS= read -r line; do fail "$line"; done <<< "$problems"

# Negative control: the wrapper without the handover.
problems=$(cases 'bureau_env_handover() { :; }; bureau_env_tmux_clear() { :; }')
case "$problems" in *'(i) a new start without override kept'*) ;; *) fail "negative control: (i) passes without the handover: $problems" ;; esac
case "$problems" in *"(ii) a new start's override did not reach"*) ;; *) fail "negative control: (ii) passes without the handover: $problems" ;; esac
# The reader without the name check and with the list taken as it is: (v) runs the forged entries.
OLD="$SB/old-reader"; mkdir -p "$OLD"; cp "$SCRIPTS/bureau-env.sh" "$OLD/"
sed -i.bak -e 's/^  \[\[ \$1 =~ \$_bka_re \]\] || return 1$/  :/' \
  -e 's/^_bureau_env_clean_list() {$/_bureau_env_clean_list() { printf "%s" " $1 "; return 0/' "$OLD/bureau-env.sh"
grep -q '^_bureau_env_clean_list() { printf' "$OLD/bureau-env.sh" || fail "negative control: the reader copy was not changed"
problems=$(forged_problems "$OLD")
case "$problems" in *'(v) a forged entry ran a command'*) ;; *) fail "negative control: (v) passes without the name check: $(head -3 <<< "$problems")" ;; esac
# Without the tmux clear, the forged list and key reach the window opened by hand.
problems=$(case_tmux_forged 'bureau_env_tmux_clear() { :; }')
case "$problems" in *'(vi) a window opened by hand after the start took the forged value'*) ;; *) fail "negative control: (vi) passes without the tmux clear: $problems" ;; esac
# Without the derived keys (BUREAU_CALLER_STOP neither dropped by the hand-over nor cleared from
# tmux), the earlier start's stop holds.
cp "$SB/repo/scripts/bureau-env.sh" "$SB/bureau-env.sh.keep"
sed -i.bak "s/^_BUREAU_ENV_DERIVED='BUREAU_CALLER_STOP'\$/_BUREAU_ENV_DERIVED=''/" "$SB/repo/scripts/bureau-env.sh"
grep -q "^_BUREAU_ENV_DERIVED=''\$" "$SB/repo/scripts/bureau-env.sh" || fail "negative control: the derived list was not emptied"
problems=$(case_stop '')
cp "$SB/bureau-env.sh.keep" "$SB/repo/scripts/bureau-env.sh"
case "$problems" in *"(vii) a start with BUREAU_NO_MERGE=0 kept the earlier start's stop"*) ;; *) fail "negative control: (vii) passes without the derived keys: $problems" ;; esac
[ "$FAILS" = 0 ] && echo "PASS negative controls: without the handover and the tmux clear (i) and (ii) fail; without the name check (v) runs the forged entries; without the tmux clear (vi) fails; without the derived keys (vii) fails"

[ "$FAILS" = 0 ] || { echo "$FAILS check(s) failed"; exit 1; }
echo "OK test_env_tmux_handover"
