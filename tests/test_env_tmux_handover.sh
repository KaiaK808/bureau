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
# Negative control: with bureau_env_handover printing nothing (the wrapper of the previous
# version), (i) and (ii) fail.
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
# server's (export -p); every window or typed command then runs in a fresh shell holding only
# that environment, as a pane does, with the shepherd swapped for the probe.
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
case "\$1" in
  has-session) [ -e "\$D/session.\$3" ] ;;
  new-session)
    [ -e "\$D/server.sh" ] || export -p > "\$D/server.sh"
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
elif ! grep -q "BUREAU_ENV_HANDOVER=1 BUREAU_ENV_CALLER=' BUREAU_CODEX_MODEL_DEFAULT ' BUREAU_CODEX_MODEL_DEFAULT='override-a'" "$SB/tmuxd/commands" 2>/dev/null; then
  fail "(iii) the window's command line does not carry the handover: $(cat "$SB/tmuxd/commands" 2>/dev/null)"
else echo "PASS (iii) the handover carries the list and the values, never a secret"; fi

# (iv) Every command the start scripts type into a Bureau or agent pane starts with the handover.
bad=$(grep -nE 'tmux send-keys .*"(\./scripts/|\$BENCH_RUNNER)' "$SCRIPTS"/start-agents.sh "$SCRIPTS"/start-bureau-v2.sh | grep -v '\$(bureau_env_handover)' || true)
count=$(grep -cE 'tmux send-keys .*"\$\(bureau_env_handover\)' "$SCRIPTS"/start-agents.sh "$SCRIPTS"/start-bureau-v2.sh | awk -F: '{ s += $2 } END { print s }')
grep -qx 'bureau_env_caller_export BUREAU_SESSION="$SESSION"' "$SCRIPTS/start-bureau-v2.sh" || bad="$bad start-bureau-v2 BUREAU_SESSION"
[ -z "$bad" ] && [ "$count" = 5 ] && echo "PASS (iv) start-agents.sh and start-bureau-v2.sh hand over on every Bureau or agent pane" \
  || fail "(iv) a pane command without bureau_env_handover (found $count): $bad"

# Negative control: the wrapper without the handover.
problems=$(cases 'bureau_env_handover() { :; }')
case "$problems" in *'(i) a new start without override kept'*) ;; *) fail "negative control: (i) passes without the handover: $problems" ;; esac
case "$problems" in *"(ii) a new start's override did not reach"*) ;; *) fail "negative control: (ii) passes without the handover: $problems" ;; esac
[ "$FAILS" = 0 ] && echo "PASS negative control: without the handover (i) and (ii) fail"

[ "$FAILS" = 0 ] || { echo "$FAILS check(s) failed"; exit 1; }
echo "OK test_env_tmux_handover"
