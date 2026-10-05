#!/bin/bash
# pr5-interrupt.sh — the sandbox of the interrupted-run and ownership-halt tests
# (v3.1.0-rc.2): tests/test_interrupted_run_resume.sh, tests/test_ownership_halt_trace.sh.
#
# The REAL shepherd.sh, bureau-worker.sh, bureau-config.sh, bureau-env.sh and
# bureau-runtime.py run in a repository whose path has a space, with a bare origin. Only the
# edges are doubles: `curl` plays Linear (answers by query shape, only with the sandbox key;
# labels and comments per ticket; every call and write recorded) and Telegram (the alert text
# is recorded). The spec
# stage is a probe for the provider work: it enters through the real bureau_stage_enter, so
# a shepherd run nests three runtime wrappers of one run as in the field (shepherd → worker
# → stage); it creates the stage's feature branch (never pushed) and a file, then blocks
# until a signal ends it. $SB/probe-mode changes that: finish moves the ticket to Done, idle
# blocks before it writes anything, commit commits its work first, ignore ignores SIGTERM and
# Ctrl-C, slowexit spends 7 s in an EXIT trap (like implement's deferred push) and then
# writes $SB/pushed, ignorehup ignores SIGHUP and writes $SB/stopped in its EXIT trap,
# ignoreall ignores SIGHUP, SIGTERM and Ctrl-C, retry ends with 10 after its work (the
# shepherd waits 60 s and retries) (tests/test_hangup_stop.sh). Each release of a label also
# records whether it was a single attempt in $SB/release-single.log. Before it marks that it
# runs, the probe writes its own process and its ancestors up to the first one outside the
# sandbox to $SB/run-procs ("pid pgid command" per line): a test that signals one process group
# of the run takes the group from there, so it can never name a process of another run.
#
# pr5_setup                    — $SB (physical path), the fake Linear/Telegram, and a repo
# pr5_teardown                 — the tests' EXIT trap: stops every process group still running a
#                                command of the sandbox (a failed case can leave a shepherd chain
#                                behind, whose probe stage never ends), then removes the sandbox;
#                                after a test that passed, a leftover process fails it
# pr5_new_repo                 — a fresh repository: $REPO, $WT (the shepherd worktree), $COMMON
# pr5_ticket <n> <labels-json> — ticket EXP-<n>'s labels (the state is shared: $SB/state)
# pr5_shepherd [args…]         — run the shepherd on EXP-7 to its end: RC, OUT, ERR
# pr5_shepherd_start [args…]   — start it in the background (SHEP_PID) and wait for the probe
# pr5_interrupt                — SIGTERM the shepherd, as the pilot did; RC, ERR
# pr5_worker <issue> <wt>      — run the real worker for the spec stage, as queue-loop does
# pr5_writes <n>               — the Linear writes on EXP-<n>, one per line
# pr5_comments <n>             — the number of comments on EXP-<n>

PR5_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
PR5_SCRIPTS="$PR5_ROOT/templates/scripts"

pr5_setup() {
  # Physical path: the runtime hands the stages resolved paths (macOS /var → /private/var).
  SB=$(cd "$(mktemp -d -t bureau-test.pr5.XXXXXXXX)" && pwd -P)
  unset BUREAU_CONFIG BUREAU_DRY_RUN BUREAU_NO_MERGE BUREAU_STOP_REQUESTED BUREAU_ACTIVE_ENTRY BUREAU_RUN_ID \
        BUREAU_CURRENT_ISSUE BUREAU_WORKSPACE_MODE BUREAU_ALERT_THROTTLE_FILE TELEGRAM_BOT_TOKEN \
        TELEGRAM_ALERT_CHAT_ID TMUX BUREAU_CALLER_STOP 2>/dev/null || true
  mkdir -p "$SB/bin" "$SB/tmp" "$SB/labels"
  printf s1 > "$SB/state"
  : > "$SB/linear.log"; : > "$SB/comments.jsonl"; : > "$SB/alerts.log"
  cat > "$SB/bin/curl" <<EOF
#!/bin/bash
sb="$SB"
prev=""; payload=""; text=""; url=""; config=""
for a in "\$@"; do
  case "\$prev" in
    -d) payload="\$a" ;;
    --data-urlencode) case "\$a" in text=*) text="\${a#text=}" ;; esac ;;
    -K) [ "\$a" != - ] || config=\$(cat) ;;
  esac
  case "\$a" in https://*) url="\$a" ;; esac
  prev="\$a"
done
case "\$config" in *'url = "https://api.telegram.org/'*) url=https://api.telegram.org/ ;; esac
config_text=\$(printf '%s\n' "\$config" | sed -n 's/^data-urlencode = "text=\(.*\)"\$/\1/p')
[ -z "\$config_text" ] || text="\$config_text"
case "\$url" in *api.telegram.org*) printf '%s\n' "\$text" >> "\$sb/alerts.log"; exit 0 ;; esac
# Linear answers only a request that carries the key from the sandbox .env.
if ! printf '%s\n' "\$config" | grep -qx 'header = "Authorization: k"'; then
  echo unauthorized >> "\$sb/linear.log"
  printf '%s' '{"errors":[{"message":"Authentication required"}]}'
  "$PR5_ROOT/tests/lib/curl-writeout.sh" 401 "\$@"
  exit 0
fi
query=\$(printf '%s' "\$payload" | jq -r '.query // ""' 2>/dev/null || true)
num=\$(printf '%s' "\$query" | sed -n 's/.*number: { eq: \([0-9][0-9]*\) }.*/\1/p')
id=\$(printf '%s' "\$payload" | jq -r '.variables.id // ""' 2>/dev/null || true)
lfile() { local f="\$sb/labels/\$1.json"; [ -f "\$f" ] || printf '["lane-2"]' > "\$f"; printf '%s' "\$f"; }
case "\$query" in
  *commentCreate*)
    body=\$(printf '%s' "\$payload" | jq -r .variables.body)
    jq -nc --arg i "\${id#U}" --arg b "\$body" '{issue: \$i, body: \$b}' >> "\$sb/comments.jsonl"
    echo "comment \${id#U}" >> "\$sb/linear.log"; b='{"data":{"commentCreate":{"success":true}}}' ;;
  *issueAddLabel*|*issueRemoveLabel*)
    lid=\$(printf '%s' "\$payload" | jq -r .variables.lid); name="\${lid#L:}"; f=\$(lfile "\${id#U}")
    case "\$query" in
      *issueAddLabel*)
        echo "add-label \${id#U} \$name" >> "\$sb/linear.log"
        jq -c --arg n "\$name" 'if index(\$n) then . else . + [\$n] end' "\$f" > "\$f.new" && mv "\$f.new" "\$f"
        b='{"data":{"issueAddLabel":{"success":true}}}' ;;
      *)
        echo "remove-label \${id#U} \$name" >> "\$sb/linear.log"
        echo "\${_BUREAU_LINEAR_SINGLE_ATTEMPT:-0}" >> "\$sb/release-single.log"
        jq -c --arg n "\$name" 'map(select(. != \$n))' "\$f" > "\$f.new" && mv "\$f.new" "\$f"
        b='{"data":{"issueRemoveLabel":{"success":true}}}' ;;
    esac ;;
  *issueUpdate*)
    sid=\$(printf '%s' "\$payload" | jq -r .variables.sid)
    echo "move \${id#U} \$sid" >> "\$sb/linear.log"; printf '%s' "\$sid" > "\$sb/state"
    b='{"data":{"issueUpdate":{"success":true}}}' ;;
  *issueLabels*)
    name=\$(printf '%s' "\$query" | sed -n 's/.*name: { eq: "\([^"]*\)" }.*/\1/p')
    b=\$(jq -nc --arg n "\$name" '{data:{issueLabels:{nodes:[{id:("L:" + \$n),team:null}]}}}') ;;
  *viewer*) b='{"data":{"viewer":{"id":"V"}}}' ;;
  *'nodes { identifier title description project'*)
    b=\$(jq -nc --slurpfile l "\$(lfile "\$num")" --arg n "\$num" '{data:{issues:{nodes:[{identifier:("EXP-" + \$n),title:"T",description:"D",project:null,labels:{nodes:(\$l[0] | map({name: .}))}}]}}}') ;;
  *'nodes { id identifier title description state'*)
    b=\$(jq -nc --slurpfile l "\$(lfile "\$num")" --arg n "\$num" --arg s "\$(cat "\$sb/state")" '{data:{issues:{nodes:[{id:("U" + \$n),identifier:("EXP-" + \$n),title:"T",description:"D",state:{id:\$s,name:"?"},labels:{nodes:(\$l[0] | map({name: .}))}}]}}}') ;;
  *'nodes { branchName'*|*'nodes { comments'*)
    b=\$(jq -sc --arg n "\$num" '{data:{issues:{nodes:[{branchName:("exp-" + \$n + "-x"),comments:{nodes:[.[] | select(.issue == \$n) | {body, createdAt: "2026-09-30T00:00:00Z"}]}}]}}}' "\$sb/comments.jsonl") ;;
  *'nodes { id } }'*) b=\$(jq -nc --arg n "\$num" '{data:{issues:{nodes:[{id:("U" + \$n)}]}}}') ;;
  *) echo "unmatched \$query" >> "\$sb/linear.log"; b='{}' ;;
esac
printf '%s' "\$b"
"$PR5_ROOT/tests/lib/curl-writeout.sh" 200 "\$@"
EOF
  chmod +x "$SB/bin/curl"
}

pr5_new_repo() {
  REPO=$(mktemp -d "$SB/repo XXXXXXXX")
  local origin; origin=$(mktemp -d "$SB/origin-XXXXXXXX")
  git init -q --bare "$origin"
  mkdir -p "$REPO/scripts"
  git -C "$REPO" init -q -b main
  git -C "$REPO" config user.name t; git -C "$REPO" config user.email t@t
  printf '.env\n.bureau.json\n.worktrees/\n' > "$REPO/.gitignore"
  git -C "$REPO" add .gitignore; git -C "$REPO" commit -q -m init
  git -C "$REPO" remote add origin "$origin"; git -C "$REPO" push -q origin main
  local f
  for f in "$PR5_SCRIPTS"/*; do [ ! -f "$f" ] || cp "$f" "$REPO/scripts/"; done
  cat > "$REPO/scripts/spec-pipeline.sh" <<PROBE
#!/bin/bash
# Probe for the spec stage's provider work (tests/lib/pr5-interrupt.sh).
set -euo pipefail
source "\$(dirname "\$0")/bureau-config.sh"
if [ -f "\$BUREAU_ENV_FILE" ]; then bureau_load_env --export "\$BUREAU_ENV_FILE"; fi
bureau_stage_enter "\$1" "\$@"
if [ "\$(cat "$SB/probe-mode" 2>/dev/null || echo block)" = idle ]; then : > "$SB/probe-started"; while :; do sleep 1; done; fi
git checkout -q -b 145-probe-feature
echo 'spec draft' > probe-work.txt
case "\$(cat "$SB/probe-mode" 2>/dev/null || echo block)" in
  finish) echo "\$1" >> "$SB/finished.log"; printf s8 > "$SB/state"; exit 0 ;;
  commit) git add probe-work.txt; git -c user.name=t -c user.email=t@t commit -q -m 'spec draft' ;;
  ignore) trap '' TERM INT ;;
  slowexit) trap 'sleep 7; echo pushed > "$SB/pushed"' EXIT ;;
  ignorehup) trap '' HUP; trap 'echo stopped > "$SB/stopped"' EXIT ;;
  ignoreall) trap '' TERM INT HUP ;;
  retry) exit 10 ;;
esac
p=\$\$; : > "$SB/run-procs.new"
while [ -n "\$p" ] && [ "\$p" -gt 1 ]; do
  line=\$(ps -ww -o pid=,pgid=,args= -p "\$p" 2>/dev/null) || break
  case "\$line" in *"$SB/"*) printf '%s\\n' "\$line" >> "$SB/run-procs.new" ;; *) break ;; esac
  p=\$(ps -o ppid= -p "\$p" 2>/dev/null | tr -d ' ') || p=""
done
mv "$SB/run-procs.new" "$SB/run-procs"
: > "$SB/probe-started"
while :; do sleep 1; done
PROBE
  jq -n '{
    linear: {teams: [{id: "t", key: "EXP", name: "T",
      states: {triage: "s1", spec: "s2", spec_review: "s3", design: "s4", build: "s5", build_review: "s6", merge: "s7", done: "s8"}}],
      labels: {lane2: {id: "l1", name: "lane-2"}, needs_human: {id: "l2", name: "needs-human"},
               needs_ux: {id: "l3", name: "needs-ux"}, ai_implementable: {id: "l4", name: "ai-implementable"}},
      projects: []},
    agents: {poll_interval_minutes: 30, max_review_cycles: 3},
    repo: {branch_prefix: "feat", specs_dir: "specs"}}' > "$REPO/.bureau.json"
  printf 'LINEAR_API_KEY=k\nTELEGRAM_BOT_TOKEN=t\nTELEGRAM_ALERT_CHAT_ID=c\n' > "$REPO/.env"
  WT="$REPO/.worktrees/shepherd-EXP-7"
  COMMON="$REPO/.git"
  printf s1 > "$SB/state"; rm -f "$SB/labels/"*.json "$SB/probe-mode" "$SB/probe-started" "$SB/finished.log" "$SB/pushed" "$SB/stopped" "$SB/run-procs"
  : > "$SB/linear.log"; : > "$SB/comments.jsonl"; : > "$SB/alerts.log"; rm -f "$SB/release-single.log"
}

pr5_ticket() { printf '%s' "$2" > "$SB/labels/$1.json"; }

_pr5_env() {
  env PATH="$SB/bin:$PATH" TMPDIR="$SB/tmp" BUREAU_LINEAR_RETRIES=0 BUREAU_SHEPHERD_CONFIRM_SECONDS=0 \
    BUREAU_DISABLE_THROTTLE=1 "$@"
}

pr5_shepherd() {
  set +e
  (cd "$REPO" && _pr5_env bash scripts/shepherd.sh --no-tmux --worktree .worktrees/shepherd-EXP-7 "$@" EXP-7 > "$SB/out" 2> "$SB/err")
  RC=$?
  set -e
  OUT=$(cat "$SB/out"); ERR=$(cat "$SB/err")
}

pr5_shepherd_start() {
  rm -f "$SB/probe-started"
  (cd "$REPO" && exec env PATH="$SB/bin:$PATH" TMPDIR="$SB/tmp" BUREAU_LINEAR_RETRIES=0 BUREAU_SHEPHERD_CONFIRM_SECONDS=0 \
     BUREAU_DISABLE_THROTTLE=1 bash scripts/shepherd.sh --no-tmux --worktree .worktrees/shepherd-EXP-7 "$@" EXP-7 > "$SB/out" 2> "$SB/err") &
  SHEP_PID=$!
  local waited=0
  while [ ! -f "$SB/probe-started" ] && [ "$waited" -lt 300 ]; do sleep 0.1; waited=$((waited + 1)); done
  [ -f "$SB/probe-started" ] || { kill -KILL "$SHEP_PID" 2>/dev/null || true; return 1; }
}

pr5_interrupt() {
  kill -TERM "$SHEP_PID"
  set +e
  wait "$SHEP_PID"; RC=$?
  set -e
  OUT=$(cat "$SB/out"); ERR=$(cat "$SB/err")
}

pr5_worker() {
  set +e
  (cd "$REPO" && _pr5_env bash scripts/bureau-worker.sh "$1" spec-pipeline.sh "$2" > "$SB/out" 2> "$SB/err")
  RC=$?
  set -e
  OUT=$(cat "$SB/out"); ERR=$(cat "$SB/err")
}

pr5_writes() { grep -E "^(add-label|remove-label|comment|move) $1( |\$)" "$SB/linear.log" 2>/dev/null || true; }
pr5_comments() { jq -s --arg n "$1" '[.[] | select(.issue == $n)] | length' "$SB/comments.jsonl"; }
pr5_comment() { jq -rs --arg n "$1" '[.[] | select(.issue == $n)] | last | .body // ""' "$SB/comments.jsonl"; }
pr5_run_id() { jq -r 'to_entries[] | select(.key | startswith("issue:")) | .value.run_id' "$COMMON/bureau/leases.json" 2>/dev/null | head -1; }

# _pr5_leftovers — "pid pgid command" of every process whose command names the sandbox.
_pr5_leftovers() { ps -A -o pid=,pgid=,args= | grep -F "$SB/" | grep -v -e 'grep -F' -e 'ps -A' || true; }

# _pr5_stop_leftovers — SIGKILL each leftover's process group, or only the process when it shares
# the test's own group (a job the test put in the background without a session of its own).
_pr5_stop_leftovers() {
  local own pid pgid rest i=0
  own=$(ps -o pgid= -p $$ | tr -d ' ')
  while [ -n "$(_pr5_leftovers)" ] && [ "$i" -lt 25 ]; do
    while read -r pid pgid rest; do
      [ -n "$pid" ] || continue
      if [ "$pgid" = "$own" ]; then kill -KILL "$pid" 2>/dev/null || true
      else kill -KILL -- "-$pgid" 2>/dev/null || true; fi
    done <<< "$(_pr5_leftovers)"
    sleep 0.2; i=$((i + 1))
  done
}

pr5_teardown() {
  local rc=$? left
  left=$(_pr5_leftovers)
  if [ -n "$left" ]; then
    _pr5_stop_leftovers
    if [ "$rc" = 0 ]; then
      echo "FAIL the test left processes behind (stopped now):" >&2
      printf '%s\n' "$left" | sed "s#$SB#<sandbox>#g; s/^/  | /" >&2
      rc=1
    fi
  fi
  rm -rf "$SB"
  exit "$rc"
}
