#!/bin/bash
# The scripts read the operator's .env, never a .env the branch tracks (v3.2, with S1).
#
# The worker starts a stage with the branch's worktree as working directory, and every stage
# read `./.env` before BUREAU_ENV_FILE (the .env next to .bureau.json in the main checkout). The
# worktree reset (`git clean -fdx`) removes only untracked files, so a `.env` the branch commits
# was read: its LINEAR_API_KEY, Telegram bot and chat, and settings such as BUREAU_RUNNER_*,
# BUREAU_NO_MERGE or BUREAU_DRY_RUN steered the stage. Every script now reads BUREAU_ENV_FILE
# only (a relative value counts from the directory of .bureau.json), and the runtime launch
# decides which keys to drop by that file alone.
#
# The REAL scripts run from the main checkout's scripts/ with a linked worktree as working
# directory, as bureau-worker.sh starts them; curl records the Linear key it gets on stdin and
# answers 401, so each script stops at its first Linear request.
#   1  the nine stages: their first Linear request carries the main checkout's key, never the
#      key of the .env the branch tracks
#   2  shepherd.sh, grab-issue.sh and complete-issue.sh started in that worktree: the same
#   3  crosscheck-specs.sh (a stage runs it in its worktree): gh sees the main checkout's
#      settings, not the branch's
#   4  a relative BUREAU_ENV_FILE counts from the directory of .bureau.json
#   5  no script reads a .env relative to its working directory, and every reader outside
#      bureau-env.sh reads BUREAU_ENV_FILE
# Negative control: against v3.1.0 (9411b3b) 1 to 4 fail ("1 implement: Linear got the key of the
# .env the branch tracks") and 5 names the sixteen scripts that read ./.env (fourteen loaders, the
# runtime launch in bureau-env.sh and bureau-status.sh).
set -uo pipefail
export GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_NOSYSTEM=1 GIT_TERMINAL_PROMPT=0
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SCRIPTS="$REPO_ROOT/templates/scripts"
SB=$(mktemp -d -t bureau-test.env-source.XXXXXXXX)
SB=$(cd "$SB" && pwd -P)
trap 'rm -rf "$SB"' EXIT
FAILS=0
fail() { echo "FAIL $*" >&2; FAILS=$((FAILS + 1)); }
section() { if [ "$FAILS" = "$1" ]; then echo "PASS $2"; else echo "FAILED $2" >&2; fi; }

MAIN_KEY=lin_api_main_checkout_key_0001
BRANCH_KEY=lin_api_branch_tracked_key_0002
M="$SB/main checkout"; WT="$M/.worktrees/wt"
mkdir -p "$M"
git -C "$M" init -q -b main; git -C "$M" config user.email t@t; git -C "$M" config user.name t
printf '.env\n.bureau.json\n.worktrees/\nscripts/\n' > "$M/.gitignore"
git -C "$M" add .gitignore; git -C "$M" commit -q -m init
cp -R "$SCRIPTS" "$M/scripts"
jq -n '{linear: {teams: [{id: "t", key: "EXP", name: "T", states: {triage: "s1", spec: "s2", spec_review: "s3", design: "s4", build: "s5", qa: "s9", build_review: "s6", merge: "s7", done: "s8"}}],
  labels: {lane2: {id: "l1", name: "lane-2"}, needs_human: {id: "l2", name: "needs-human"}, needs_ux: {id: "l3", name: "needs-ux"}, ai_implementable: {id: "l4", name: "ai-implementable"}}},
  agents: {}, repo: {}}' > "$M/.bureau.json"
printf 'LINEAR_API_KEY=%s\nBUREAU_PATH_PREFIX_STRIP=from-main/\n' "$MAIN_KEY" > "$M/.env"
# The pull request's branch commits a .env of its own (git add -f past .gitignore).
git -C "$M" worktree add -q "$WT" -b feat
printf 'LINEAR_API_KEY=%s\nTELEGRAM_BOT_TOKEN=branch-bot\nTELEGRAM_ALERT_CHAT_ID=branch-chat\nBUREAU_NO_MERGE=0\nBUREAU_PATH_PREFIX_STRIP=from-branch/\n' "$BRANCH_KEY" > "$WT/.env"
git -C "$WT" add -f .env; git -C "$WT" commit -q -m 'the branch tracks .env'
git -C "$WT" ls-files --error-unmatch .env >/dev/null 2>&1 || fail "setup: the branch does not track .env"

mkdir -p "$SB/bin"
cat > "$SB/bin/curl" <<EOF
#!/bin/bash
cat 2>/dev/null | sed -n 's/^header = "Authorization: \(.*\)"\$/\1/p' >> "$SB/linear.log"
printf '%s' '{"errors":[{"message":"Authentication required"}]}'
bash '$REPO_ROOT/tests/lib/curl-writeout.sh' 401 "\$@"
EOF
cat > "$SB/bin/gh" <<EOF
#!/bin/bash
echo "gh prefix=\${BUREAU_PATH_PREFIX_STRIP:-none}" >> "$SB/gh.log"
EOF
chmod +x "$SB/bin/curl" "$SB/bin/gh"
# run <dir> <script> [args …] — a script from the main checkout's scripts/, started in <dir>
# with the main checkout's config; prints the keys its Linear requests carried, one per line.
run() {
  local dir="$1" script="$2"; shift 2
  : > "$SB/linear.log"
  (cd "$dir" && env -u LINEAR_API_KEY -u TELEGRAM_BOT_TOKEN -u TELEGRAM_ALERT_CHAT_ID -u BUREAU_ENV_FILE \
     PATH="$SB/bin:$PATH" BUREAU_CONFIG="$M/.bureau.json" BUREAU_LINEAR_RETRIES=0 BUREAU_WORKSPACE_MODE=disposable \
     BUREAU_DISABLE_THROTTLE=1 ${RUN_ENV:-} bash "$M/scripts/$script" "$@" >/dev/null 2>&1 </dev/null)
  sort -u "$SB/linear.log"
}
# check <label> <keys seen> — the main checkout's key and nothing else.
check() {
  case "$2" in
    "$MAIN_KEY") ;;
    *"$BRANCH_KEY"*) fail "$1: Linear got the key of the .env the branch tracks" ;;
    '') fail "$1: no Linear request was made" ;;
    *) fail "$1: Linear got $(printf '%s' "$2" | tr '\n' ' ')" ;;
  esac
}

# ── 1  the nine stages ────────────────────────────────────────────────────────
F=$FAILS
for stage in implement spec spec-review ux copy qa code-review merge rebase; do
  check "1 $stage" "$(run "$WT" "$stage-pipeline.sh" EXP-1)"
done
section "$F" "1 the nine stages read the main checkout's .env, not the one the branch tracks"

# ── 2  shepherd, grab-issue, complete-issue in the worktree ───────────────────
F=$FAILS
check "2 shepherd.sh" "$(run "$WT" shepherd.sh --no-tmux EXP-1)"
check "2 grab-issue.sh" "$(run "$WT" grab-issue.sh)"
check "2 complete-issue.sh" "$(run "$WT" complete-issue.sh EXP-1)"
section "$F" "2 shepherd, grab-issue and complete-issue started in the worktree read the main checkout's .env"

# ── 3  crosscheck-specs.sh ────────────────────────────────────────────────────
F=$FAILS
: > "$SB/gh.log"
run "$WT" crosscheck-specs.sh >/dev/null
case "$(cat "$SB/gh.log")" in
  'gh prefix=from-main/') ;;
  *from-branch*) fail "3 crosscheck-specs.sh: it read the settings of the .env the branch tracks" ;;
  *) fail "3 crosscheck-specs.sh: gh saw $(tr '\n' ' ' < "$SB/gh.log")" ;;
esac
section "$F" "3 crosscheck-specs.sh in the worktree reads the main checkout's settings"

# ── 4  a relative BUREAU_ENV_FILE ─────────────────────────────────────────────
F=$FAILS
check "4 relative BUREAU_ENV_FILE" "$(RUN_ENV='BUREAU_ENV_FILE=.env' run "$WT" implement-pipeline.sh EXP-1)"
section "$F" "4 a relative BUREAU_ENV_FILE counts from the directory of .bureau.json"

# ── 5  no .env relative to the working directory ──────────────────────────────
F=$FAILS
stray=$(grep -nE '\[ -f \.env \]|bureau_load_env( --export)? +\.env\b|_bureau_env_file_defines +\.env\b' "$SCRIPTS"/*.sh "$SCRIPTS"/*.py \
          | grep -vE ':[0-9]+: *#' || true)
[ -z "$stray" ] || fail "5: a script reads .env relative to its working directory: $(printf '%s' "$stray" | sed "s#$SCRIPTS/##g" | tr '\n' ';')"
# … and every reader outside the library reads BUREAU_ENV_FILE. Doctor's reader takes its file as
# an argument from stage_env_file (bureau-doctor.py); tests/doctor_checks_test.py runs it next to the
# nine stages' loaders and checks that both read the same file.
loose=$(grep -nE 'bureau_load_env( --export)? +["$.~/]' "$SCRIPTS"/*.sh "$SCRIPTS"/*.py | grep -v '/bureau-env\.sh:' | grep -vE ':[0-9]+: *#' \
          | grep -vE 'bureau_load_env( --export)? +"\$(\{)?BUREAU_ENV_FILE' | grep -vE '/bureau-doctor\.py:[0-9]+:.*bureau_load_env "\$2" ' || true)
[ -z "$loose" ] || fail "5: a script reads a .env other than BUREAU_ENV_FILE: $(printf '%s' "$loose" | sed "s#$SCRIPTS/##g" | tr '\n' ';')"
section "$F" "5 no script reads a .env relative to its working directory"

if [ "$FAILS" != 0 ]; then echo "$FAILS check(s) failed" >&2; exit 1; fi
echo "OK test_env_file_source"
