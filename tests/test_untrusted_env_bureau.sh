#!/bin/bash
# Bureau's own processes in a stage do not hand the Bureau secrets to code from the branch.
#
# tests/test_untrusted_env.sh covers the commands that run branch code on purpose. This file
# covers the places where branch code gets in through Bureau's own work, each through the REAL
# templates/scripts/bureau-config.sh (and bureau-env.sh it sources) in a sandbox repository:
#
#   A  inline Python runs with -I: a `subprocess.py`, `pathlib.py` or `hashlib.py` the branch
#      commits to its root is not imported by commit_stage_changes or the worker's cleanup
#   B  git hooks and filters: with core.hooksPath pointing at a tracked directory and a
#      .gitattributes filter, Bureau's own add/commit/checkout run them without the seven;
#      a pre-push hook sees the GitHub token variables (push needs them) but not the .env keys
#   C  the provider (run_stage_for, precondition_runner) starts without the seven
#   D  the runtime wrapper (bureau_stage_enter, bureau-worker.sh, shepherd.sh) starts without
#      the .env keys the relaunched script reads back, and keeps a key .env does not define
#   E  the Linear and Telegram requests carry the key, the token and the alert text on stdin,
#      never in curl's argument list, and curl starts without the seven
#   F  the git and gh processes Bureau's Python starts (bureau-supervision.py, bureau-runtime.py,
#      bureau-doctor.py): a branch fsmonitor hook sees no secret, a credential helper started
#      by `git ls-remote` sees the GitHub tokens but not the .env keys; the same as git()/gh()
# Every git in this file runs with GIT_CONFIG_GLOBAL=/dev/null and GIT_CONFIG_NOSYSTEM=1, so the
# machine's own credential helper and configuration are never used; all values are fake.
# Each section has a control that runs the v3.0.2 form of the same code on the same fixture
# and shows the secret arriving, so no assertion can pass on a fixture that never carried one.
set -uo pipefail
export GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_NOSYSTEM=1 GIT_TERMINAL_PROMPT=0
source "$(dirname "$0")/lib/pr1-untrusted-env.sh"
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SCRIPTS="$REPO_ROOT/templates/scripts"
TMP=$(mktemp -d -t bureau-test.untrusted-bureau.XXXXXXXX)
TMP=$(cd "$TMP" && pwd -P)
trap '[ -z "${SERVER:-}" ] || kill "$SERVER" 2>/dev/null; rm -rf "$TMP"' EXIT
fail() { pr1_fail "$@"; }
MARKS="$TMP/marks"; mkdir -p "$MARKS"
PROBES=(LINEAR_API_KEY="$PR1_LINEAR" TELEGRAM_BOT_TOKEN="$PR1_TG_TOKEN" TELEGRAM_ALERT_CHAT_ID="$PR1_TG_CHAT"
        GH_TOKEN="$PR1_GH" GITHUB_TOKEN="$PR1_GITHUB" GH_ENTERPRISE_TOKEN="$PR1_GHE" GITHUB_ENTERPRISE_TOKEN="$PR1_GITHUBE"
        API_KEY="$PR1_LINEAR" OPERATOR_TOOL_VAR="$PR1_OPERATOR" BASH_ENV=/dev/null)
# no_secret <file> <label>: none of the seven, their values or BASH_ENV in an env dump.
no_secret() {
  local v
  [ -s "$1" ] || { fail "$2: nothing ran (no $1)"; return; }
  for v in "$PR1_LINEAR" "$PR1_TG_TOKEN" "$PR1_TG_CHAT" "$PR1_GH" "$PR1_GITHUB" "$PR1_GHE" "$PR1_GITHUBE"; do
    if grep -qF -- "$v" "$1"; then fail "$2: a secret reached it, as $(grep -F -- "$v" "$1" | cut -d= -f1 | sort -u | tr '\n' ' ')"; fi
  done
  if grep -q '^BASH_ENV=' "$1"; then fail "$2: BASH_ENV reached it"; fi
  grep -qx "OPERATOR_TOOL_VAR=$PR1_OPERATOR" "$1" || fail "$2: an operator variable was dropped"
}
# repo <dir>: a repository whose main branch holds harmless hooks, a filter script and a
# .gitattributes; the branch "feat" (the pull request) makes every hook and the filter dump
# its environment and adds Python modules that shadow the standard library. core.hooksPath
# and the filter are then configured, as an operator does once.
repo() {
  local r="$1" h
  rm -rf "$r" "$r.origin"; mkdir -p "$r"
  git -C "$r" init -q -b main; git -C "$r" config user.email t@t; git -C "$r" config user.name t
  mkdir -p "$r/.githooks" "$r/.gitfilter"
  for h in pre-commit commit-msg post-checkout pre-push fsmonitor cred; do printf '#!/bin/sh\nexit 0\n' > "$r/.githooks/$h"; chmod +x "$r/.githooks/$h"; done
  printf '#!/bin/sh\ncat\n' > "$r/.gitfilter/clean.sh"
  printf '*.txt filter=probe\n' > "$r/.gitattributes"
  printf '.bureau.json\n.env\n' > "$r/.git/info/exclude"
  printf '{"repo":{}}\n' > "$r/.bureau.json"
  git -C "$r" add -A; git -C "$r" commit -q -m init
  git init -q --bare "$r.origin"; git -C "$r" remote add origin "$r.origin"; git -C "$r" push -q origin main
  git -C "$r" checkout -q -b feat
  for h in pre-commit commit-msg post-checkout pre-push; do
    printf '#!/bin/sh\n{ echo "--- run"; env; } >> "%s/%s.env"\ncat >/dev/null 2>&1 || true\nexit 0\n' "$MARKS" "$h" > "$r/.githooks/$h"
  done
  printf '#!/bin/sh\n{ echo "--- run"; env; } >> "%s/filter.env"\ncat\n' "$MARKS" > "$r/.gitfilter/clean.sh"
  # An fsmonitor hook (git runs it to learn what changed) and a credential helper, both files
  # the branch controls, once the operator's config names them.
  printf '#!/bin/sh\n{ echo "--- run"; env; } >> "%s/fsmonitor.env"\nexit 1\n' "$MARKS" > "$r/.githooks/fsmonitor"
  printf '#!/bin/sh\n{ echo "--- run $1"; env; } >> "%s/cred.env"\n[ "$1" != get ] || printf "username=probe\\npassword=fake\\n"\n' "$MARKS" > "$r/.githooks/cred"
  for h in subprocess pathlib hashlib; do
    printf 'import os\nopen(%s, "a").write("%s " + os.environ.get("LINEAR_API_KEY", "none") + "\\n")\nraise SystemExit("shadow module imported")\n' \
      "'$MARKS/shadow.log'" "$h" > "$r/$h.py"
  done
  git -C "$r" add -A; git -C "$r" commit -q -m 'the pull request'
  git -C "$r" config core.hooksPath .githooks
  git -C "$r" config filter.probe.clean 'sh .gitfilter/clean.sh'
  git -C "$r" config core.fsmonitor "$r/.githooks/fsmonitor"
  git -C "$r" config credential.helper "$r/.githooks/cred"
  rm -f "$MARKS"/*
}
# stage <scripts dir> <script>: a stage shell in the sandbox repository with the probes
# exported, the real bureau-config.sh of <scripts dir> sourced, then <script>.
stage() {
  (cd "$R" && env "${PROBES[@]}" BUREAU_CONFIG="$R/.bureau.json" /bin/bash -c 'set -uo pipefail; source "$1/bureau-config.sh"; export API_KEY; '"$2" _ "$1")
}

# ── A + B  executor commit, hooks, filter, checkout, push ─────────────────────
R="$TMP/repo"; repo "$R"
printf 'work\n' > "$R/work.txt"
mkdir -p "$TMP/ghbin"
printf '#!/bin/sh\nenv > "%s/gh.env"\necho https://github.com/test/repo/pull/5\n' "$MARKS" > "$TMP/ghbin/gh"; chmod +x "$TMP/ghbin/gh"
out=$(PATH="$TMP/ghbin:$PATH" stage "$SCRIPTS" 'commit_stage_changes qa EXP-2 >/dev/null 2>&1; echo "commit=$?"; git checkout -q -b other; git -C "$PWD" push -q origin HEAD 2>/dev/null; echo "push=$?"; gh pr create --draft --title t --body b >/dev/null; echo "caller=${LINEAR_API_KEY:-gone}"')
case "$out" in *commit=0*) ;; *) fail "A: commit_stage_changes failed: $out" ;; esac
case "$out" in *push=0*) ;; *) fail "B: the push failed: $out" ;; esac
case "$out" in *"caller=$PR1_LINEAR"*) ;; *) fail "B: the stage shell lost its key" ;; esac
[ "$(git -C "$R" log -1 --format=%s)" = 'EXP-2: Bureau qa changes' ] || fail "A: the executor's commit is missing"
[ ! -e "$MARKS/shadow.log" ] || fail "A: the branch's Python module ran inside commit_stage_changes: $(cat "$MARKS/shadow.log")"
for h in pre-commit commit-msg post-checkout filter fsmonitor; do no_secret "$MARKS/$h.env" "B $h"; done
# gh runs git itself: it starts without the .env keys and keeps the GitHub token it logs in with.
if [ -s "$MARKS/gh.env" ]; then
  grep -qx "GH_TOKEN=$PR1_GH" "$MARKS/gh.env" || fail "B gh: GH_TOKEN (gh's login) was removed"
  for v in "$PR1_LINEAR" "$PR1_TG_TOKEN" "$PR1_TG_CHAT"; do
    if grep -qF -- "$v" "$MARKS/gh.env"; then fail "B gh: a .env key reached gh"; fi
  done
  if grep -q '^BASH_ENV=' "$MARKS/gh.env"; then fail "B gh: BASH_ENV reached gh"; fi
else
  fail "B gh: gh did not run"
fi
if [ -s "$MARKS/pre-push.env" ]; then
  grep -qx "GH_TOKEN=$PR1_GH" "$MARKS/pre-push.env" || fail "B pre-push: GH_TOKEN (push credentials) was removed"
  for v in "$PR1_LINEAR" "$PR1_TG_TOKEN" "$PR1_TG_CHAT"; do
    if grep -qF -- "$v" "$MARKS/pre-push.env"; then fail "B pre-push: a .env key reached the hook"; fi
  done
else
  fail "B pre-push: the hook did not run"
fi
[ "$(git -C "$R.origin" rev-parse other)" = "$(git -C "$R" rev-parse HEAD)" ] || fail "B: the pushed commit is not on origin"
# The worker's cleanup line, as it runs: in the stage worktree, next to the branch's modules.
line=$(grep -F "key=\$(python3" "$SCRIPTS/bureau-worker.sh" | sed 's/^ *//')
case "$line" in *'python3 -I -c'*) ;; *) fail "A: the worker's registry key line is not found or not isolated: $line" ;; esac
key=$(cd "$R" && WORKTREE="$R" && env "${PROBES[@]}" /bin/bash -c "$line"' && echo "$key"')
[[ "$key" =~ ^[0-9a-f]{64}$ ]] || fail "A: the worker's registry key was not computed: $key"
[ ! -e "$MARKS/shadow.log" ] || fail "A: the branch's Python module ran in the worker's cleanup line"
# Every inline Python in the templates is isolated (-I): the two cases above are the ones that
# run in a stage worktree today; this keeps a new one from coming back without the flag.
stray=$(grep -rnE 'python3? +(-[A-HJ-Za-z]+ +)*(-c|-)( |$)|python3? +<<' "$SCRIPTS" | grep -v -- ' -I ' | grep -vE ':[0-9]+: *#' || true)
[ -z "$stray" ] || fail "A: inline Python without -I: $stray"
pr1_pass "A+B: branch hooks, filter and Python modules run without the secrets; push keeps the GitHub tokens"

# Control: the v3.0.2 forms (no git function, python3 - without -I and without the reduction).
repo "$R"; printf 'work\n' > "$R/work.txt"
OLD="$TMP/old-scripts"; rm -rf "$OLD"; cp -R "$SCRIPTS" "$OLD"
sed -i.bak -e 's/bureau_without_secrets python3 -I - /python3 - /' "$OLD/bureau-config.sh"
out=$(stage "$OLD" 'unset -f git; commit_stage_changes qa EXP-2 >/dev/null 2>&1; echo "commit=$?"')
grep -q "$PR1_LINEAR" "$MARKS/shadow.log" 2>/dev/null || fail "A control: without -I the branch's subprocess.py should have run with the key"
repo "$R"; printf 'work\n' > "$R/work.txt"
out=$(stage "$SCRIPTS" 'unset -f git; commit_stage_changes qa EXP-2 >/dev/null 2>&1; echo "commit=$?"')
grep -qF "LINEAR_API_KEY=$PR1_LINEAR" "$MARKS/pre-commit.env" 2>/dev/null || fail "B control: without the git function the pre-commit hook should have seen the key"
grep -qF "LINEAR_API_KEY=$PR1_LINEAR" "$MARKS/filter.env" 2>/dev/null || fail "B control: without the git function the filter should have seen the key"
grep -qF "LINEAR_API_KEY=$PR1_LINEAR" "$MARKS/fsmonitor.env" 2>/dev/null || fail "B control: without the git function the fsmonitor hook should have seen the key"
pr1_pass "A+B control: the v3.0.2 forms run the branch's module, hook and filter with the key"

# ── C  the provider starts without the seven ──────────────────────────────────
PROV="$TMP/prov-scripts"; rm -rf "$PROV"; cp -R "$SCRIPTS" "$PROV"
cat > "$PROV/bureau-provider.py" <<EOF
import os, sys
with open('$MARKS/provider.env', 'a') as out:
    out.write('--- run\n' + ''.join(k + '=' + v + '\n' for k, v in os.environ.items()))
print('provider-ok')
EOF
repo "$R"
out=$(stage "$PROV" 'run_stage_for spec "a prompt"; precondition_runner spec; echo "check-ok"')
case "$out" in *provider-ok*check-ok*) ;; *) fail "C: run_stage_for or precondition_runner did not complete: $out" ;; esac
# The compatibility entry codex-stage-runner.sh, with a prompt argument and on stdin.
(cd "$R" && env "${PROBES[@]}" BUREAU_CONFIG="$R/.bureau.json" bash "$PROV/codex-stage-runner.sh" "a prompt" >/dev/null 2>&1
 printf 'a prompt' | env "${PROBES[@]}" BUREAU_CONFIG="$R/.bureau.json" bash "$PROV/codex-stage-runner.sh" >/dev/null 2>&1)
[ "$(grep -c '^--- run$' "$MARKS/provider.env" 2>/dev/null)" = 4 ] || fail "C: expected four provider starts (run_stage_for, precondition_runner, codex-stage-runner.sh twice)"
no_secret "$MARKS/provider.env" "C provider"
sed -i.bak -e 's/bureau_without_secrets python3 -I "\$(dirname/python3 "$(dirname/' "$PROV/bureau-config.sh"
rm -f "$MARKS/provider.env"
stage "$PROV" 'run_stage_for spec "a prompt"' >/dev/null
grep -qF "LINEAR_API_KEY=$PR1_LINEAR" "$MARKS/provider.env" 2>/dev/null || fail "C control: the v3.0.2 launch should have handed the provider the key"
pr1_pass "C: the provider starts without the seven (control: the old launch hands it the key)"

# ── D  the runtime wrapper starts without the .env keys ───────────────────────
RT="$TMP/rt-scripts"; rm -rf "$RT"; cp -R "$SCRIPTS" "$RT"
# The probe records its environment and then does what the worker does after the runtime has
# relaunched it: change into the branch's worktree and start a bash (bureau-worker.sh).
cat > "$RT/bureau-runtime.py" <<EOF
import os, subprocess
with open('$MARKS/runtime.env', 'a') as out:
    out.write('--- run\n' + ''.join(k + '=' + v + '\n' for k, v in os.environ.items()))
if os.environ.get('PR1_WT'):
    subprocess.run(['bash', '-c', ':'], cwd=os.environ['PR1_WT'])
EOF
repo "$R"
rm -rf "$R/scripts"; cp -R "$RT" "$R/scripts"
# .env defines the Linear key and the bot token; the chat id comes only from the environment.
printf 'LINEAR_API_KEY=%s\nTELEGRAM_BOT_TOKEN=%s\n' "$PR1_LINEAR" "$PR1_TG_TOKEN" > "$R/.env"
check_runtime() {  # <label>
  local f="$MARKS/runtime.env"
  [ -s "$f" ] || { fail "$1: the runtime did not start"; return; }
  if grep -qF -- "$PR1_LINEAR" "$f"; then fail "$1: the Linear key (or its API_KEY copy) reached the runtime"; fi
  if grep -qF -- "$PR1_TG_TOKEN" "$f"; then fail "$1: the bot token reached the runtime"; fi
  grep -qx "TELEGRAM_ALERT_CHAT_ID=$PR1_TG_CHAT" "$f" || fail "$1: a key .env does not define was dropped"
  grep -qx "GH_TOKEN=$PR1_GH" "$f" || fail "$1: GH_TOKEN (the stages' gh calls) was dropped"
  rm -f "$f"
}
(cd "$R" && env "${PROBES[@]}" BUREAU_CONFIG="$R/.bureau.json" BUREAU_WORKSPACE_MODE=disposable \
   /bin/bash -c 'source scripts/bureau-config.sh; export API_KEY; bureau_stage_enter EXP-1' "$R/scripts/qa-pipeline.sh") >/dev/null 2>&1
check_runtime "D bureau_stage_enter"
(cd "$R" && env "${PROBES[@]}" BUREAU_CONFIG="$R/.bureau.json" bash scripts/bureau-worker.sh EXP-1 qa-pipeline.sh "$R/.wt" feat) >/dev/null 2>&1
check_runtime "D bureau-worker.sh"
(cd "$R" && env "${PROBES[@]}" BUREAU_CONFIG="$R/.bureau.json" bash scripts/shepherd.sh --no-tmux EXP-1) >/dev/null 2>&1
check_runtime "D shepherd.sh"
# A key that exists only in the environment (no .env line for it) passes on: the stages need it.
printf 'TELEGRAM_BOT_TOKEN=%s\n' "$PR1_TG_TOKEN" > "$R/.env"
(cd "$R" && env "${PROBES[@]}" BUREAU_CONFIG="$R/.bureau.json" bash scripts/bureau-worker.sh EXP-1 qa-pipeline.sh "$R/.wt" feat) >/dev/null 2>&1
grep -qx "LINEAR_API_KEY=$PR1_LINEAR" "$MARKS/runtime.env" 2>/dev/null || fail "D: a Linear key that .env does not define must pass on to the stages"
rm -f "$MARKS/runtime.env"
# The same from a directory without its own .env: only BUREAU_ENV_FILE decides, and it lacks the key.
mkdir -p "$R/noenv"
(cd "$R/noenv" && env "${PROBES[@]}" BUREAU_CONFIG="$R/.bureau.json" BUREAU_WORKSPACE_MODE=disposable \
   /bin/bash -c 'source ../scripts/bureau-config.sh; bureau_stage_enter EXP-1' "$R/scripts/qa-pipeline.sh") >/dev/null 2>&1
grep -qx "LINEAR_API_KEY=$PR1_LINEAR" "$MARKS/runtime.env" 2>/dev/null || fail "D: a Linear key that BUREAU_ENV_FILE does not define must pass on (no ./.env)"
if grep -qF -- "$PR1_TG_TOKEN" "$MARKS/runtime.env" 2>/dev/null; then fail "D: the bot token BUREAU_ENV_FILE defines reached the runtime (no ./.env)"; fi
rm -f "$MARKS/runtime.env"
# A stage reads ./.env first: when that exists and lacks the key, the key must pass on even
# though BUREAU_ENV_FILE defines it (the relaunched stage would not find it again).
printf 'LINEAR_API_KEY=%s\nTELEGRAM_BOT_TOKEN=%s\n' "$PR1_LINEAR" "$PR1_TG_TOKEN" > "$R/.env"
mkdir -p "$R/sub"; printf 'TELEGRAM_BOT_TOKEN=%s\n' "$PR1_TG_TOKEN" > "$R/sub/.env"
(cd "$R/sub" && env "${PROBES[@]}" BUREAU_CONFIG="$R/.bureau.json" BUREAU_WORKSPACE_MODE=disposable \
   /bin/bash -c 'source ../scripts/bureau-config.sh; bureau_stage_enter EXP-1' "$R/scripts/qa-pipeline.sh") >/dev/null 2>&1
grep -qx "LINEAR_API_KEY=$PR1_LINEAR" "$MARKS/runtime.env" 2>/dev/null || fail "D: a key that ./.env does not define must pass on, whatever BUREAU_ENV_FILE holds"
if grep -qF -- "$PR1_TG_TOKEN" "$MARKS/runtime.env" 2>/dev/null; then fail "D: the bot token that both .env files define reached the runtime"; fi
rm -f "$MARKS/runtime.env"
# A relative BASH_ENV names a script the branch committed to its worktree: the bash the worker
# starts there must not source it (the runtime drops BASH_ENV and ENV).
mkdir -p "$R/wt"
printf '{ echo "--- sourced"; env; } >> "%s/bashenv.env"\n' "$MARKS" > "$R/wt/evil.sh"
(cd "$R" && env "${PROBES[@]}" BASH_ENV=evil.sh ENV=evil.sh PR1_WT="$R/wt" BUREAU_CONFIG="$R/.bureau.json" \
   bash scripts/bureau-worker.sh EXP-1 qa-pipeline.sh "$R/.wt" feat) >/dev/null 2>&1
[ -s "$MARKS/runtime.env" ] || fail "D relative BASH_ENV: the runtime did not start"
if grep -qE '^(BASH_ENV|ENV)=' "$MARKS/runtime.env" 2>/dev/null; then fail "D: BASH_ENV or ENV reached the runtime"; fi
[ ! -e "$MARKS/bashenv.env" ] || fail "D: the worker's bash in the branch worktree sourced a relative BASH_ENV from the branch"
rm -f "$MARKS/runtime.env"
# Control: the v3.0.2 launch.
printf 'LINEAR_API_KEY=%s\n' "$PR1_LINEAR" > "$R/.env"
sed -i.bak -e 's/bureau_exec_runtime python3 -I /exec python3 /' "$R/scripts/bureau-worker.sh"
(cd "$R" && env "${PROBES[@]}" BUREAU_CONFIG="$R/.bureau.json" bash scripts/bureau-worker.sh EXP-1 qa-pipeline.sh "$R/.wt" feat) >/dev/null 2>&1
grep -qF "LINEAR_API_KEY=$PR1_LINEAR" "$MARKS/runtime.env" 2>/dev/null || fail "D control: the v3.0.2 launch should have handed the runtime the key"
rm -f "$MARKS/runtime.env"
(cd "$R" && env "${PROBES[@]}" BASH_ENV=evil.sh PR1_WT="$R/wt" BUREAU_CONFIG="$R/.bureau.json" \
   bash scripts/bureau-worker.sh EXP-1 qa-pipeline.sh "$R/.wt" feat) >/dev/null 2>&1
grep -q '^--- sourced' "$MARKS/bashenv.env" 2>/dev/null || fail "D control: with the v3.0.2 launch the worker's bash should have sourced the branch's BASH_ENV"
pr1_pass "D: the runtime starts without the .env keys the stage reads back (stage enter, worker, shepherd)"

# ── E  Linear and Telegram requests: secrets on stdin, not in argv or curl's environment ──
BIN="$TMP/curlbin"; mkdir -p "$BIN"
cat > "$BIN/curl" <<EOF
#!/bin/bash
n=\$(( \$(cat '$MARKS/curl.n' 2>/dev/null || echo 0) + 1 )); echo "\$n" > '$MARKS/curl.n'
printf '%s\n' "\$@" > '$MARKS/curl.'\$n'.argv'
env > '$MARKS/curl.'\$n'.env'
if [ -t 0 ]; then : > '$MARKS/curl.'\$n'.stdin'; else cat > '$MARKS/curl.'\$n'.stdin'; fi
printf '{"data":{"viewer":{"id":"u1"}}}'
bash '$REPO_ROOT/tests/lib/curl-writeout.sh' 200 "\$@"
EOF
chmod +x "$BIN/curl"
repo "$R"
out=$(cd "$R" && PATH="$BIN:$PATH" env "${PROBES[@]}" BUREAU_CONFIG="$R/.bureau.json" BUREAU_DRY_RUN=0 /bin/bash -c '
  source "$1/bureau-config.sh"
  _throttle_should_suppress() { return 1; }; _throttle_record() { :; }
  _bureau_linear_fetch "{\"query\":\"{ viewer { id } }\"}"; echo
  alert_telegram EXP-1 qa 13 "probe alert" "tool output: token=FAKE_LEAKED_IN_LOG_0011
second line"' _ "$SCRIPTS" 2>&1)
case "$out" in *'"id":"u1"'*) ;; *) fail "E: the Linear fetch did not return the answer: $out" ;; esac
for n in 1 2; do
  [ -f "$MARKS/curl.$n.argv" ] || { fail "E: request $n was not made"; continue; }
  for v in "$PR1_LINEAR" "$PR1_TG_TOKEN" "$PR1_TG_CHAT"; do
    if grep -qF -- "$v" "$MARKS/curl.$n.argv"; then fail "E request $n: a secret is in curl's argument list"; fi
  done
  no_secret "$MARKS/curl.$n.env" "E request $n (curl's environment)"
done
grep -qxF "header = \"Authorization: $PR1_LINEAR\"" "$MARKS/curl.1.stdin" || fail "E: the Linear key did not reach curl on stdin"
grep -qxF "url = \"https://api.telegram.org/bot$PR1_TG_TOKEN/sendMessage\"" "$MARKS/curl.2.stdin" || fail "E: the Telegram URL did not reach curl on stdin"
grep -qxF "data-urlencode = \"chat_id=$PR1_TG_CHAT\"" "$MARKS/curl.2.stdin" || fail "E: the chat id did not reach curl on stdin"
if grep -qF FAKE_LEAKED_IN_LOG_0011 "$MARKS/curl.2.argv"; then fail "E: the alert text (log tail) is in curl's argument list"; fi
grep -q '^data-urlencode = "text=.*FAKE_LEAKED_IN_LOG_0011\\nsecond line' "$MARKS/curl.2.stdin" || fail "E: the alert text did not reach curl on stdin, newline escaped"
grep -qx -- '-K' "$MARKS/curl.1.argv" && grep -qx -- '-K' "$MARKS/curl.2.argv" || fail "E: curl was not told to read its config from stdin"
# Control: the recorder sees a key passed the v3.0.2 way (-H on the command line).
rm -f "$MARKS"/curl.*
PATH="$BIN:$PATH" curl -s -H "Authorization: $PR1_LINEAR" https://api.linear.app/graphql </dev/null >/dev/null
grep -qF -- "$PR1_LINEAR" "$MARKS/curl.1.argv" || fail "E control: the curl recorder does not see its argument list"
pr1_pass "E: the Linear key and the Telegram token go to curl on stdin; curl's argv and environment hold no secret"

# ── F  git and gh started by Bureau's Python ─────────────────────────────────
repo "$R"
# F1: the git() helpers of bureau-supervision.py and bureau-runtime.py: `git status` runs the
# branch's fsmonitor hook (the workspace and checkpoint checks run it in a stage worktree).
py_git_status() {  # <scripts dir>
  (cd "$R" && env "${PROBES[@]}" python3 -I - "$1" "$R" <<'PY'
import importlib.util, sys
from pathlib import Path
for name in ('supervision', 'runtime'):
    spec = importlib.util.spec_from_file_location(name, Path(sys.argv[1]) / ('bureau-' + name + '.py'))
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    module.git(Path(sys.argv[2]), 'status', '--porcelain')
PY
  ) >/dev/null 2>&1
}
py_git_status "$SCRIPTS"
[ "$(grep -c '^--- run$' "$MARKS/fsmonitor.env" 2>/dev/null)" -ge 2 ] || fail "F: the fsmonitor hook did not run for both helpers"
no_secret "$MARKS/fsmonitor.env" "F fsmonitor (supervision and runtime git)"
# F2: a stopped review's check (bureau-supervision.py check, called by the review stage with its
# .env exported) asks gh for the PR and reads the remote tips with `git ls-remote`; the remote
# asks for credentials, so git runs the credential helper the operator's config names.
cat > "$TMP/authserver.py" <<'PY'
import http.server, sys
class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(401); self.send_header('WWW-Authenticate', 'Basic realm="probe"')
        self.send_header('Content-Length', '0'); self.end_headers()
    def log_message(self, *args): pass
server = http.server.HTTPServer(('127.0.0.1', 0), Handler)
open(sys.argv[1], 'w').write(str(server.server_port)); server.serve_forever()
PY
rm -f "$TMP/port"; python3 "$TMP/authserver.py" "$TMP/port" & SERVER=$!
for _ in 1 2 3 4 5 6 7 8 9 10; do [ -s "$TMP/port" ] && break; sleep 0.3; done
git -C "$R" remote set-url origin "http://127.0.0.1:$(cat "$TMP/port")/repo.git"
mkdir -p "$TMP/supgh"
printf '#!/bin/sh\nenv > "%s/gh-supervision.env"\necho %s\n' "$MARKS" "'{\"state\":\"OPEN\",\"baseRefName\":\"main\"}'" > "$TMP/supgh/gh"; chmod +x "$TMP/supgh/gh"
DETAIL='{"identifier":"EXP-1","title":"t","description":"d","labels":[]}'
HEAD_SHA=$(git -C "$R" rev-parse HEAD)
supervise() {  # <scripts dir>
  (cd "$R" && printf '%s' "$DETAIL" | env "${PROBES[@]}" python3 "$1/bureau-supervision.py" --repo "$R" stop EXP-1 --branch feat \
     --state 'Build Review' --head "$HEAD_SHA" --base "$HEAD_SHA" --reviewed-head "$HEAD_SHA" --pr 5 >/dev/null 2>&1
   printf '%s' "$DETAIL" | PATH="$TMP/supgh:$PATH" env "${PROBES[@]}" python3 "$1/bureau-supervision.py" --repo "$R" check EXP-1 \
     --branch feat --state 'Build Review' >/dev/null 2>&1)
}
rm -f "$MARKS/cred.env" "$MARKS/gh-supervision.env"
supervise "$SCRIPTS"
if [ -s "$MARKS/cred.env" ]; then
  grep -qx "GH_TOKEN=$PR1_GH" "$MARKS/cred.env" || fail "F credential helper: GH_TOKEN (the remote's credentials) was removed"
  for v in "$PR1_LINEAR" "$PR1_TG_TOKEN" "$PR1_TG_CHAT"; do
    if grep -qF -- "$v" "$MARKS/cred.env"; then fail "F credential helper: a .env key reached it"; fi
  done
  if grep -q '^BASH_ENV=' "$MARKS/cred.env"; then fail "F credential helper: BASH_ENV reached it"; fi
else
  fail "F: git ls-remote did not run the credential helper"
fi
if [ -s "$MARKS/gh-supervision.env" ]; then
  grep -qx "GH_TOKEN=$PR1_GH" "$MARKS/gh-supervision.env" || fail "F gh: GH_TOKEN was removed"
  if grep -qF -- "$PR1_LINEAR" "$MARKS/gh-supervision.env"; then fail "F gh: the Linear key reached gh"; fi
else
  fail "F: the check did not ask gh"
fi
# Control: the v3.0.2 supervision (no env= on its git and gh calls) hands both hooks the key.
OLDSUP="$TMP/old-sup"; rm -rf "$OLDSUP"; cp -R "$SCRIPTS" "$OLDSUP"
sed -i.bak -e 's/, env=process_env(command))/)/' -e 's/ env=process_env(command)//' "$OLDSUP/bureau-supervision.py" "$OLDSUP/bureau-runtime.py"
rm -f "$MARKS/cred.env" "$MARKS/fsmonitor.env"
supervise "$OLDSUP"; py_git_status "$OLDSUP"
grep -qF "LINEAR_API_KEY=$PR1_LINEAR" "$MARKS/cred.env" 2>/dev/null || fail "F control: the old ls-remote should have handed the credential helper the key"
grep -qF "LINEAR_API_KEY=$PR1_LINEAR" "$MARKS/fsmonitor.env" 2>/dev/null || fail "F control: the old git() should have handed the fsmonitor hook the key"
kill "$SERVER" 2>/dev/null; wait "$SERVER" 2>/dev/null
# F3: the same environment as the shell's git() and gh(), for a local and a remote command.
mkdir -p "$TMP/dumpbin"
for tool in git gh; do printf '#!/bin/sh\n/usr/bin/env -0 > "%s/%s.dump"\n' "$TMP" "$tool" > "$TMP/dumpbin/$tool"; chmod +x "$TMP/dumpbin/$tool"; done
for cmd in 'git status' 'git -C . push' 'gh pr view 5'; do
  tool=${cmd%% *}
  env -i PATH="$TMP/dumpbin:$PATH" HOME="$HOME" LINEAR_API_KEY="$PR1_LINEAR" TELEGRAM_BOT_TOKEN=abc123 TELEGRAM_ALERT_CHAT_ID=12345 \
    GH_TOKEN="$PR1_GH" API_KEY="$PR1_LINEAR" HEADER="Authorization: abc123" CHAT=12345 CHAT_IN=x12345 URL="https://t:$PR1_GH@h" \
    BASH_ENV=/dev/null OPERATOR_TOOL_VAR="$PR1_OPERATOR" \
    /bin/bash -c 'source "$1"; env -0 > "$2.input"; '"$cmd" _ "$SCRIPTS/bureau-env.sh" "$TMP/$tool"
  python3 - "$SCRIPTS" "$TMP/$tool.dump" "$TMP/$tool.input" $cmd <<'PY' || fail "F parity '$cmd': the shell and a Python process_env differ"
import importlib.util, sys
from pathlib import Path
def read(path): return dict(item.split('=', 1) for item in open(path, 'rb').read().decode().split('\0') if item)
ignore = {'PWD', 'SHLVL', '_', 'OLDPWD', '__CF_USER_TEXT_ENCODING'}
shell = {k: v for k, v in read(sys.argv[2]).items() if k not in ignore}
given = {k: v for k, v in read(sys.argv[3]).items() if k not in ignore}
failed = False
for name in ('provider', 'supervision', 'runtime'):
    spec = importlib.util.spec_from_file_location(name, Path(sys.argv[1]) / ('bureau-' + name + '.py'))
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    python = module.process_env(sys.argv[4:], given)
    if shell != python:
        failed = True
        print(name, 'shell only:', sorted(set(shell) - set(python)), 'python only:', sorted(set(python) - set(shell)), file=sys.stderr)
sys.exit(1 if failed else 0)
PY
done
# F4: every git and gh process the three Python scripts start is given an environment.
python3 - "$SCRIPTS" <<'PY' || fail "F: a git or gh call in the Python scripts starts without env="
import ast, sys
from pathlib import Path
bad = []
for name in ('bureau-supervision.py', 'bureau-runtime.py', 'bureau-doctor.py'):
    tree = ast.parse((Path(sys.argv[1]) / name).read_text())
    commands = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.List) and node.value.elts \
           and isinstance(node.value.elts[0], ast.Constant) and node.value.elts[0].value in ('git', 'gh'):
            for target in node.targets:
                if isinstance(target, ast.Name): commands[target.id] = True
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in ('run', 'check_output', 'Popen', 'call') \
           and isinstance(node.func.value, ast.Name) and node.func.value.id == 'subprocess' and node.args:
            first = node.args[0]
            starts = (isinstance(first, ast.List) and first.elts and isinstance(first.elts[0], ast.Constant) and first.elts[0].value in ('git', 'gh')) \
                     or (isinstance(first, ast.Name) and first.id in commands)
            if starts and not any(k.arg == 'env' for k in node.keywords): bad.append('%s:%d' % (name, node.lineno))
if bad: print('without env=:', bad, file=sys.stderr); sys.exit(1)
PY
pr1_pass "F: git and gh from Bureau's Python run without the .env keys (fsmonitor, credential helper, gh), as git()/gh() do"

if [ "$PR1_FAILS" != 0 ]; then echo "$PR1_FAILS check(s) failed" >&2; exit 1; fi
echo "OK test_untrusted_env_bureau"
