#!/bin/bash
# Code the branch controls runs without the Bureau secrets (repo.untrusted_env).
#
# LINEAR_API_KEY, TELEGRAM_BOT_TOKEN, TELEGRAM_ALERT_CHAT_ID, GH_TOKEN, GITHUB_TOKEN,
# GH_ENTERPRISE_TOKEN and GITHUB_ENTERPRISE_TOKEN — and any variable carrying one of their
# values, such as the stage's API_KEY — must not reach a command the branch controls,
# while the stage's own Linear and GitHub calls keep them. Every exec site is run through
# its REAL stage script (tests/lib/harness.sh: stub Linear, stub gh, fake agent) or, for
# upstream-port.sh, through the real section cut from it; each command writes the
# environment it got to a marker, and the recording gh and Linear doubles
# (tests/lib/pr1-untrusted-env.sh) show the stage's own calls after it still carry the keys.
#
#   1  bureau_untrusted_env itself (templates/scripts/bureau-env.sh): default, clean,
#      invalid values, no command, xtrace, exit status, and the same result as
#      bureau-provider.py's untrusted_env for the same environment, without BUREAU_ENV_FILE
#      and BUREAU_CONFIG; a copy of either that keeps them fails (negative control)
#   2  review build check (code-review-pipeline.sh): default, clean, invalid → 24 before
#      the command and before any verdict, negative control
#   3  QA: all three test runs (initial, retry, final), invalid → 24, negative control
#   4  repo.post_implement_command: default, clean, invalid → not run, halt 14 with the
#      work pushed, negative control
#   5  Codex implement end to end through the real run_stage_for and bureau-provider.py:
#      the agent process, the hook and the completion test, default and clean, control
#   6  bureau-app.sh test (real bureau-config.sh): default, exit status, control
#   7  upstream-port.sh build and test (real section): default, invalid → 18 before the
#      build, control
set -uo pipefail
source "$(dirname "$0")/lib/harness.sh"
source "$(dirname "$0")/lib/pr1-untrusted-env.sh"
unset BUREAU_CALLER_STOP API_KEY
ENV_SH="$REPO_ROOT/templates/scripts/bureau-env.sh"
PROVIDER="$REPO_ROOT/templates/scripts/bureau-provider.py"
TMPD=$(mktemp -d -t bureau-test.untrusted.XXXXXXXX)
trap 'teardown 2>/dev/null || true; rm -rf "$TMPD"' EXIT
fail() { pr1_fail "$@"; }
config() { printf '%s\n' "$2" > "$TMPD/$1.json"; printf '%s' "$TMPD/$1.json"; }

# ── 1  the helper ─────────────────────────────────────────────────────────────
DEFAULT_CFG=$(config default '{"repo":{}}')
CLEAN_CFG=$(config clean '{"repo":{"untrusted_env":"clean"}}')
# run_helper <config> <script>: the real bureau-env.sh in a fresh /bin/bash with the probes
# exported and API_KEY exported with the Linear key's value (the stage's copy).
run_helper() {
  env LINEAR_API_KEY="$PR1_LINEAR" TELEGRAM_BOT_TOKEN="$PR1_TG_TOKEN" TELEGRAM_ALERT_CHAT_ID="$PR1_TG_CHAT" \
    GH_TOKEN="$PR1_GH" GITHUB_TOKEN="$PR1_GITHUB" GH_ENTERPRISE_TOKEN="$PR1_GHE" GITHUB_ENTERPRISE_TOKEN="$PR1_GITHUBE" \
    API_KEY="$PR1_LINEAR" CARGO_ALIAS="$PR1_GH" REMOTE_URL="https://x-access-token:$PR1_GITHUB@github.com/owner/repo.git" \
    OPERATOR_TOOL_VAR="$PR1_OPERATOR" BUREAU_CONFIG="$1" BUREAU_ENV_FILE="$TMPD/operator.env" \
    /bin/bash -c 'set -euo pipefail; source "$1"; '"$2" _ "${ENV_SH_UNDER_TEST:-$ENV_SH}"
}
H="$TMPD/helper.env"
rm -f "$H"
out=$(run_helper "$DEFAULT_CFG" "bureau_untrusted_env HOOK_VAR=from-caller sh -c '{ echo \"--- run\"; env; } > \"$H\"; exit 7' || echo \"rc=\$?\"; echo \"after=\${LINEAR_API_KEY:-gone} \${GH_TOKEN:-gone}\"")
pr1_check_env "$H" "1 default" default 1 HOOK_VAR=from-caller
if grep -q '^CARGO_ALIAS=' "$H"; then fail "1 default: a GitHub token under another name reached the command"; fi
if grep -q '^REMOTE_URL=' "$H"; then fail "1 default: a value with a GitHub token inside reached the command"; fi
case "$out" in *"rc=7"*) ;; *) fail "1 default: the command's exit status was not passed on: $out" ;; esac
case "$out" in *"after=$PR1_LINEAR $PR1_GH"*) ;; *) fail "1 default: the calling shell lost its keys" ;; esac
rm -f "$H"
run_helper "$CLEAN_CFG" "export TZ=\"\$LINEAR_API_KEY\"; bureau_untrusted_env HOOK_VAR=from-caller sh -c '{ echo \"--- run\"; env; } > \"$H\"'" >/dev/null
pr1_check_env "$H" "1 clean" clean 1 HOOK_VAR=from-caller
stray=$(sed -n 's/=.*//p' "$H" | grep -vxE -- '--- run|PATH|HOME|USER|LOGNAME|SHELL|TMPDIR|TEMP|TMP|LANG|LC_ALL|LC_CTYPE|TERM|TZ|CI|HOOK_VAR|PWD|SHLVL|_|OLDPWD|__CF_USER_TEXT_ENCODING' | tr '\n' ' ')
[ -z "$stray" ] || fail "1 clean: variables outside the clean list: $stray"
if grep -q '^TZ=' "$H"; then fail "1 clean: a kept name carrying a secret's value was passed on"; fi
# The clean list, pinned: every allowed name is set (to a harmless value) next to what an
# operator's shell also carries (an SSH agent, cloud and registry tokens). The command must
# see exactly the allowed names, so adding a name to the list, or dropping one, fails here
# on any runner.
rm -f "$H"
run_helper "$CLEAN_CFG" "export PATH HOME USER=u LOGNAME=u SHELL=/bin/sh TMPDIR=/tmp TEMP=/tmp TMP=/tmp LANG=C LC_ALL=C LC_CTYPE=C TERM=dumb TZ=UTC CI=true \
  SSH_AUTH_SOCK=/tmp/agent.sock AWS_SECRET_ACCESS_KEY=aws-probe NPM_TOKEN=npm-probe HTTPS_PROXY=http://proxy.invalid SSL_CERT_FILE=/tmp/ca.pem; \
  bureau_untrusted_env HOOK_VAR=from-caller /usr/bin/env > \"$H\"" >/dev/null
got=$(sed -n 's/=.*//p' "$H" | grep -vxE 'PWD|SHLVL|_|OLDPWD|__CF_USER_TEXT_ENCODING' | LC_ALL=C sort | tr '\n' ' ')
want="CI HOME HOOK_VAR LANG LC_ALL LC_CTYPE LOGNAME PATH SHELL TEMP TERM TMP TMPDIR TZ USER "
[ "$got" = "$want" ] || fail "1 clean list: got [$got], wanted [$want]"
pr1_pass "1 default drops the secrets and their copies, clean keeps only its list, the caller keeps its keys"

# A secret of 6 characters or more goes wherever it appears inside a value (a 6-character key
# in a copied Authorization header); a shorter one only as the whole value (inside matching of
# a 5-character chat id would take out every variable that happens to contain it).
rm -f "$H"
run_helper "$DEFAULT_CFG" "export LINEAR_API_KEY=abc123 COPIED_HEADER='Authorization: abc123' \
  TELEGRAM_ALERT_CHAT_ID=12345 CHAT_COPY=12345 CHAT_INSIDE=x12345y; \
  bureau_untrusted_env /usr/bin/env > \"$H\"" >/dev/null
if grep -q '^COPIED_HEADER=' "$H"; then fail "1 short: a 6-character secret inside another value reached the command"; fi
if grep -q '^CHAT_COPY=' "$H"; then fail "1 short: a whole-value copy of a short secret reached the command"; fi
grep -qx 'CHAT_INSIDE=x12345y' "$H" || fail "1 short: a value that only contains a 5-character secret was removed"
if grep -q '^TELEGRAM_ALERT_CHAT_ID=' "$H"; then fail "1 short: the short secret itself reached the command"; fi

# BASH_ENV: a bash child sources the file it names before running its -c string, so a
# BASH_ENV pointing at a file that exports a key would hand the key back to branch code.
# Through the start the call sites use (bash --noprofile --norc -c, which does not stop it).
printf 'export LINEAR_API_KEY=%s BASHENV_RAN=yes\n' "$PR1_LINEAR" > "$TMPD/bashenv"
rm -f "$H"
run_helper "$DEFAULT_CFG" "export BASH_ENV='$TMPD/bashenv' ENV='$TMPD/bashenv'; bureau_untrusted_env bash --noprofile --norc -c 'env > \"$H\"'" >/dev/null
if grep -q '^BASHENV_RAN=' "$H"; then fail "1 BASH_ENV: the child bash sourced BASH_ENV"; fi
if grep -qE '^(BASH_ENV|ENV)=' "$H"; then fail "1 BASH_ENV: BASH_ENV or ENV reached the command"; fi
if grep -qF "$PR1_LINEAR" "$H"; then fail "1 BASH_ENV: the key came back through BASH_ENV"; fi
rm -f "$H"
run_helper "$DEFAULT_CFG" "export BASH_ENV='$TMPD/bashenv'; /usr/bin/env -u LINEAR_API_KEY bash --noprofile --norc -c 'env > \"$H\"'" >/dev/null
grep -q '^BASHENV_RAN=yes' "$H" || fail "1 BASH_ENV control: a child bash with BASH_ENV set should have sourced it"

# env by its absolute path: a fake env first on PATH (a branch's node_modules/.bin) is never run.
mkdir -p "$TMPD/fakebin"
printf '#!/bin/sh\necho fake-env-ran > "%s/fake-env"\nexec /usr/bin/env "$@"\n' "$TMPD" > "$TMPD/fakebin/env"
chmod +x "$TMPD/fakebin/env"; rm -f "$TMPD/fake-env" "$H"
run_helper "$DEFAULT_CFG" "PATH='$TMPD/fakebin':\$PATH; bureau_untrusted_env sh -c 'echo ran > \"$H\"'; bureau_without_secrets true" >/dev/null
[ ! -e "$TMPD/fake-env" ] || fail "1 env: the helper ran an env found on PATH"
[ -f "$H" ] || fail "1 env: the command did not run"
pr1_pass "1 short copies go as whole values, BASH_ENV and ENV go, env is /usr/bin/env"

for bad in '"cleen"' '""' 'true' '1' '["clean"]' '"clean\n"'; do
  BAD_CFG=$(config bad "{\"repo\":{\"untrusted_env\":$bad}}")
  rm -f "$TMPD/ran"
  err=$(run_helper "$BAD_CFG" "bureau_untrusted_env touch '$TMPD/ran'; echo reached" 2>&1); rc=$?
  [ "$rc" = 24 ] || fail "1 invalid $bad: exit $rc, expected 24"
  [ ! -e "$TMPD/ran" ] || fail "1 invalid $bad: the command ran"
  case "$err" in *reached*) fail "1 invalid $bad: the caller went on after the refusal" ;; esac
  case "$err" in *'repo.untrusted_env must be'*) ;; *) fail "1 invalid $bad: no message: $err" ;; esac
  # In `|| …` (errexit off, as at the Codex completion test and the hook) the refusal must
  # still end the shell with 24, never come back as the command's own failure.
  out=$(run_helper "$BAD_CFG" "bureau_untrusted_env touch '$TMPD/ran' || echo \"judged=\$?\"; echo reached" 2>/dev/null); rc=$?
  [ "$rc" = 24 ] && [ -z "$out" ] || fail "1 invalid $bad: under || the refusal returned instead of exiting (exit $rc, output: $out)"
  out=$(run_helper "$BAD_CFG" 'bureau_untrusted_env --check || echo "check=$?"; echo reached' 2>/dev/null)
  [ "$out" = "$(printf 'check=24\nreached')" ] || fail "1 invalid $bad: --check must return 24 and not exit: $out"
done
for broken in '{"repo":"a string"}' 'not json'; do
  BAD_CFG=$(config broken "$broken")
  rm -f "$TMPD/ran"
  run_helper "$BAD_CFG" "bureau_untrusted_env touch '$TMPD/ran'" >/dev/null 2>&1; rc=$?
  [ "$rc" = 24 ] && [ ! -e "$TMPD/ran" ] || fail "1 unreadable config '$broken': exit $rc, command ran: $([ -e "$TMPD/ran" ] && echo yes || echo no)"
done
out=$(run_helper "$DEFAULT_CFG" 'bureau_untrusted_env A=1 B=2; echo reached' 2>&1); rc=$?
[ "$rc" = 24 ] || fail "1 no command: exit $rc, expected 24"
case "$out" in *reached*|*"LINEAR_API_KEY"*) fail "1 no command: went on or printed the environment" ;; esac
out=$(run_helper "$DEFAULT_CFG" 'bureau_untrusted_env --check && echo check=0' 2>/dev/null)
[ "$out" = check=0 ] || fail "1 valid --check: $out"
pr1_pass "1 an invalid or unreadable repo.untrusted_env or a missing command: nothing runs, exit 24; --check returns 24"

trace=$(run_helper "$DEFAULT_CFG" 'set -x; bureau_untrusted_env true' 2>&1)
case "$trace" in *"$PR1_LINEAR"*|*"$PR1_GH"*) fail "1 xtrace: a secret value reached the trace" ;; esac
case "$trace" in *'/usr/bin/env -u LINEAR_API_KEY'*) ;; *) fail "1 xtrace: the command line was not traced: $(printf '%s' "$trace" | tail -2)" ;; esac
pr1_pass "1 under set -x the trace names the removed variables, never a value"

# Parity: the shell helper and bureau-provider.py reduce the same environment alike.
# parity <bureau-env.sh> <bureau-provider.py> <mode> — 0 when both give the same environment;
# the differences go to stderr. The shell's result stays in $TMPD/shell.<mode>.
parity() {
  local cfg="$DEFAULT_CFG" mode="$3"
  [ "$mode" = clean ] && cfg="$CLEAN_CFG"
  env -i PATH="$PATH" HOME="$HOME" LANG=C CI=12345 TZ="UTC$PR1_GH" USER=u \
    LINEAR_API_KEY="$PR1_LINEAR" GH_TOKEN="$PR1_GH" TELEGRAM_ALERT_CHAT_ID=12345 SHORT_COPY=12345 SHORT_INSIDE=x12345 \
    TELEGRAM_BOT_TOKEN=abc123 HEADER6="Authorization: abc123" \
    BASH_ENV=/dev/null ENV=/dev/null SSH_AUTH_SOCK=/tmp/agent.sock \
    API_KEY="$PR1_LINEAR" CARGO_ALIAS="$PR1_GH" OPERATOR_TOOL_VAR="$PR1_OPERATOR" BUREAU_CONFIG="$cfg" \
    BUREAU_ENV_FILE="$TMPD/operator.env" \
    REMOTE_URL="https://x-access-token:$PR1_GH@github.com/owner/repo.git" LANGUAGE="x${PR1_LINEAR}y" \
    /bin/bash -c 'source "$1"; bureau_untrusted_env env -0 > "$2"; env -0 > "$3"' _ "$1" "$TMPD/shell.$mode" "$TMPD/input.$mode"
  python3 - "$2" "$mode" "$TMPD/shell.$mode" "$TMPD/input.$mode" <<'PY'
import importlib.util, sys
spec = importlib.util.spec_from_file_location('provider', sys.argv[1]); p = importlib.util.module_from_spec(spec); spec.loader.exec_module(p)
def read(path):
    raw = open(path, 'rb').read().decode()
    return dict(item.split('=', 1) for item in raw.split('\0') if item)
ignore = {'PWD', 'SHLVL', '_', 'OLDPWD', '__CF_USER_TEXT_ENCODING'}
shell = {k: v for k, v in read(sys.argv[3]).items() if k not in ignore}
given = {k: v for k, v in read(sys.argv[4]).items() if k not in ignore}
python = p.untrusted_env(given, sys.argv[2])
if shell != python:
    print('shell only:', sorted(set(shell.items()) - set(python.items())), file=sys.stderr)
    print('python only:', sorted(set(python.items()) - set(shell.items())), file=sys.stderr)
    sys.exit(1)
PY
}
for mode in default clean; do
  parity "$ENV_SH" "$PROVIDER" "$mode" || fail "1 parity $mode: shell and provider differ"
  for name in BUREAU_ENV_FILE BUREAU_CONFIG; do
    if tr '\0' '\n' < "$TMPD/shell.$mode" | grep -q "^$name="; then fail "1 parity $mode: $name reached the command"; fi
  done
done
# The two lists name the same two variables, in the same order.
shell_paths=$(/bin/bash -c 'source "$1"; printf "%s" "$_BUREAU_UNTRUSTED_PATHS"' _ "$ENV_SH")
python_paths=$(python3 -c 'import importlib.util, sys
spec = importlib.util.spec_from_file_location("provider", sys.argv[1]); p = importlib.util.module_from_spec(spec); spec.loader.exec_module(p)
print(" ".join(p.UNTRUSTED_PATHS))' "$PROVIDER")
[ "$shell_paths" = "BUREAU_ENV_FILE BUREAU_CONFIG" ] || fail "1 paths: the shell list is [$shell_paths]"
[ "$shell_paths" = "$python_paths" ] || fail "1 paths: shell [$shell_paths] and provider [$python_paths] differ"
pr1_pass "1 the shell helper and bureau-provider.py give the same environment (default and clean), without BUREAU_ENV_FILE and BUREAU_CONFIG"

# Negative controls: a copy of either side that keeps BUREAU_ENV_FILE and BUREAU_CONFIG in the
# default mode fails the parity check against the other side, and the shell copy's command
# fails pr1_check_env; the two copies agree with each other, so only the two names differ.
mkdir -p "$TMPD/keep"
sed '/for _bue_name in \$_BUREAU_UNTRUSTED_PATHS/d' "$ENV_SH" > "$TMPD/keep/bureau-env.sh"
sed 's/ and name not in UNTRUSTED_PATHS$//' "$PROVIDER" > "$TMPD/keep/bureau-provider.py"
if cmp -s "$ENV_SH" "$TMPD/keep/bureau-env.sh"; then fail "1 control: the shell copy is unchanged (the sed found nothing)"; fi
if cmp -s "$PROVIDER" "$TMPD/keep/bureau-provider.py"; then fail "1 control: the provider copy is unchanged (the sed found nothing)"; fi
if parity "$TMPD/keep/bureau-env.sh" "$PROVIDER" default 2>/dev/null; then fail "1 control: a shell copy that keeps the paths passed the parity check"; fi
if parity "$ENV_SH" "$TMPD/keep/bureau-provider.py" default 2>/dev/null; then fail "1 control: a provider copy that keeps the paths passed the parity check"; fi
parity "$TMPD/keep/bureau-env.sh" "$TMPD/keep/bureau-provider.py" default || fail "1 control: the two copies differ in more than the paths"
rm -f "$H"
ENV_SH_UNDER_TEST="$TMPD/keep/bureau-env.sh" run_helper "$DEFAULT_CFG" "bureau_untrusted_env sh -c '{ echo \"--- run\"; env; } > \"$H\"'" >/dev/null
control_fails=$PR1_FAILS
pr1_check_env "$H" "1 control" default 2>/dev/null
if [ "$PR1_FAILS" = "$control_fails" ]; then fail "1 control: pr1_check_env passed a command that saw BUREAU_ENV_FILE and BUREAU_CONFIG"
else PR1_FAILS=$control_fails; fi
pr1_pass "1 control: a copy that keeps BUREAU_ENV_FILE and BUREAU_CONFIG fails the parity and environment checks"

# ── 2  review build check ────────────────────────────────────────────────────
# review_run <untrusted_env JSON or ''> [control|-] [bashenv]
#   bashenv: the stage runs with BASH_ENV naming a file that leaves a mark when a bash
#   sources it for the build check's command string (the stage's own bash does not match).
review_run() {
  sandbox_init EXP-801 test-branch
  pr1_setup
  printf 'change\n' > "$SANDBOX/change.txt"
  git -C "$SANDBOX" add change.txt && git -C "$SANDBOX" commit -q -m 'fixture change' && git -C "$SANDBOX" push -q origin test-branch
  local canary=""
  if [ "${3:-}" = bashenv ]; then
    canary="; : PR1_BASHENV_CANARY"
    printf 'case "${BASH_EXECUTION_STRING:-}" in *PR1_BASHENV_CANARY*) echo sourced >> "%s/bashenv.mark" ;; esac\n' "$PR1_MARKS" > "$PR1_MARKS/bashenv.sh"
    export BASH_ENV="$PR1_MARKS/bashenv.sh"
  fi
  jq -n --arg c "$(pr1_dump review)$canary" --argjson u "${1:-null}" \
    '{repo: ({test_command: $c} + (if $u == null then {} else {untrusted_env: $u} end))}' > "$SANDBOX/.bureau.json"
  [ "${2:-}" != control ] || pr1_passthrough "$SCRIPTS_DIR"
  printf 'Review checked.\n```json\n{"verdict":"APPROVE","bugs":0,"security_issues":0,"findings":[],"summary":"fixture"}\n```\n' > "$SANDBOX/verdict.txt"
  export FAKE_CLAUDE_FIXTURES="$SANDBOX/verdict.txt" BUREAU_STUB_ISSUE_STATE='Build Review' GH_STUB_EXISTING_PR=99
  export BUREAU_NO_MERGE=1 BUREAU_STOP_REQUESTED=0 BUREAU_STUB_STATE_MERGE='' BUREAU_STUB_AGENT_ENABLED=''
  run_pipeline code-review-pipeline.sh EXP-801 </dev/null; set +e  # run_pipeline leaves errexit on
  unset BASH_ENV
  rm -rf "$(printf '%s\n' "$LAST_STDERR" | sed -n 's/^code-review failed .*preserved at //p')"
}
review_run ''
pr1_check_env "$PR1_MARKS/review.env" "2 review default" default
[ "$LAST_RC" = 20 ] || fail "2 review default: expected 20 (APPROVE, stop before merge), got $LAST_RC"
grep -q '^\*\*Build\*\*: Passed' "$SANDBOX/gh_calls.log" || fail "2 review default: the review does not say the build passed"
pr1_check_bureau_calls "2 review default" gh
teardown
review_run '"clean"'
pr1_check_env "$PR1_MARKS/review.env" "2 review clean" clean
[ "$LAST_RC" = 20 ] || fail "2 review clean: expected 20, got $LAST_RC"
teardown
review_run '"cleen"'
[ "$LAST_RC" = 24 ] || fail "2 review invalid: expected 24, got $LAST_RC"
[ ! -e "$PR1_MARKS/review.env" ] || fail "2 review invalid: the build check ran"
case "$LAST_STDERR" in *'repo.untrusted_env must be'*) ;; *) fail "2 review invalid: the reason is not in the stage output" ;; esac
if grep -q 'pr comment' "$SANDBOX/gh_calls.log" 2>/dev/null; then fail "2 review invalid: a verdict was posted"; fi
assert_calls_exclude 'move_issue' '2 review invalid: the ticket moved' || PR1_FAILS=$((PR1_FAILS + 1))
teardown
review_run '' control
grep -qF "LINEAR_API_KEY=$PR1_LINEAR" "$PR1_MARKS/review.env" 2>/dev/null && grep -qF "GH_TOKEN=$PR1_GH" "$PR1_MARKS/review.env" \
  || fail "2 control: with the v3.0.2 behaviour the build check should have seen the secrets"
teardown
review_run '' - bashenv
pr1_check_env "$PR1_MARKS/review.env" "2 review BASH_ENV" default
[ ! -e "$PR1_MARKS/bashenv.mark" ] || fail "2 review BASH_ENV: the build check's bash sourced BASH_ENV"
teardown
review_run '' control bashenv
[ -e "$PR1_MARKS/bashenv.mark" ] || fail "2 BASH_ENV control: with the v3.0.2 behaviour the build check's bash should have sourced BASH_ENV"
teardown
unset BUREAU_NO_MERGE BUREAU_STOP_REQUESTED GH_STUB_EXISTING_PR BUREAU_STUB_ISSUE_STATE
pr1_pass "2 the review build check runs without the secrets, the review's gh calls keep the token"

# ── 3  QA: initial run, retry, final run ────────────────────────────────────
qa_run() {
  sandbox_init EXP-802 test-branch
  pr1_setup
  jq -n --arg c "$(pr1_dump qa); false" --argjson u "${1:-null}" \
    '{repo: ({test_command: $c} + (if $u == null then {} else {untrusted_env: $u} end))}' > "$SANDBOX/.bureau.json"
  [ "${2:-}" != control ] || pr1_passthrough "$SCRIPTS_DIR"
  printf '```json\n{"status":"RED","tests_added":0,"tests_failing":1,"coverage_notes":"fixture"}\n```\n' > "$SANDBOX/qa.txt"
  export FAKE_CLAUDE_FIXTURES="$SANDBOX/qa.txt" BUREAU_STUB_STATE_QA=state-qa BUREAU_STUB_ISSUE_STATE=QA
  run_pipeline qa-pipeline.sh EXP-802 </dev/null; set +e
  unset BUREAU_STUB_STATE_QA
}
qa_run ''
pr1_check_env "$PR1_MARKS/qa.env" "3 qa" default 3
grep -q 'Phase 3/3' <<< "$LAST_STDOUT" || fail "3 qa: the final run was not reached (exit $LAST_RC)"
pr1_check_bureau_calls "3 qa" linear
teardown
qa_run '"cleen"'
[ "$LAST_RC" = 24 ] || fail "3 qa invalid: expected 24, got $LAST_RC"
[ ! -e "$PR1_MARKS/qa.env" ] || fail "3 qa invalid: a test run happened"
case "$LAST_STDERR" in *'repo.untrusted_env must be'*) ;; *) fail "3 qa invalid: the reason is not in the stage output" ;; esac
teardown
qa_run '' control
[ "$(grep -cF "LINEAR_API_KEY=$PR1_LINEAR" "$PR1_MARKS/qa.env" 2>/dev/null)" = 3 ] \
  || fail "3 control: with the v3.0.2 behaviour every QA run should have seen the Linear key"
teardown
pr1_pass "3 all three QA test runs run without the secrets, QA's Linear calls keep the key"

# ── 4  repo.post_implement_command ───────────────────────────────────────────
hook_run() {
  sandbox_init EXP-803 test-branch
  pr1_setup
  jq -n --arg c "$(pr1_dump hook)" --argjson u "${1:-null}" \
    '{repo: ({post_implement_command: $c} + (if $u == null then {} else {untrusted_env: $u} end))}' > "$SANDBOX/.bureau.json"
  [ "${2:-}" != control ] || pr1_passthrough "$SCRIPTS_DIR"
  export FAKE_CLAUDE_FIXTURES="$FIXTURES_DIR/claude_complete.txt" FAKE_CLAUDE_COMMIT_ON_ITERS=1 BUREAU_DRY_RUN=0 BUREAU_IMPL_MAX_ITER=3
  unset BUREAU_STUB_ISSUE_STATE
  run_implement_pipeline </dev/null; set +e
}
hook_run ''
pr1_check_env "$PR1_MARKS/hook.env" "4 hook default" default 1 BUREAU_ISSUE=EXP-803 BUREAU_BRANCH=test-branch
[ "$LAST_RC" = 0 ] || fail "4 hook default: expected 0, got $LAST_RC"
pr1_check_bureau_calls "4 hook default" gh linear
teardown
hook_run '"clean"'
pr1_check_env "$PR1_MARKS/hook.env" "4 hook clean" clean 1 BUREAU_ISSUE=EXP-803 BUREAU_BRANCH=test-branch
[ "$LAST_RC" = 0 ] || fail "4 hook clean: expected 0, got $LAST_RC"
teardown
hook_run '"cleen"'
[ ! -e "$PR1_MARKS/hook.env" ] || fail "4 hook invalid: the hook ran"
[ "$LAST_RC" = 14 ] || fail "4 hook invalid: expected the hook halt 14, got $LAST_RC"
grep -q 'repo.post_implement_command was not run: repo.untrusted_env' "$SANDBOX/calls.log" \
  || fail "4 hook invalid: the halt report does not say why"
[ "$(git -C "$SANDBOX/.fake-origin.git" rev-parse test-branch)" = "$(git -C "$SANDBOX" rev-parse HEAD)" ] \
  || fail "4 hook invalid: the run's commits did not reach origin"
teardown
hook_run '' control
grep -qF "LINEAR_API_KEY=$PR1_LINEAR" "$PR1_MARKS/hook.env" 2>/dev/null || fail "4 control: with the v3.0.2 behaviour the hook should have seen the Linear key"
teardown
pr1_pass "4 repo.post_implement_command runs without the secrets and keeps BUREAU_ISSUE/BUREAU_BRANCH"

# ── 5  Codex implement end to end: agent process, hook, completion test ────────
codex_run() {
  sandbox_init EXP-804 test-branch
  pr1_setup
  printf '.env\n.bureau.json\n.fake-origin.git/\nscripts/\nlogs/\n*.log\nfake_claude_counter\n' > "$SANDBOX/.gitignore"
  jq -n --arg t "$(pr1_dump completion); python3 -c 'from feature import add; assert add(2, 3) == 5'" \
        --arg h "$(pr1_dump hook)" --argjson u "${1:-null}" \
    '{agents: {runner: "codex", use_goal_loop: true},
      repo: ({test_command: $t, post_implement_command: $h} + (if $u == null then {} else {untrusted_env: $u} end))}' > "$SANDBOX/.bureau.json"
  export BUREAU_CONFIG="$SANDBOX/.bureau.json" BUREAU_STUB_RUNNER=codex BUREAU_USE_GOAL_LOOP=1
  export OPENAI_API_KEY=sk-openai-PROBE-agent-login BUREAU_PROVIDER_LOG_DIR="$SANDBOX/logs/provider-runs"
  # The real run_stage_for and bureau-provider.py, as tests/test_codex_implement_pipeline.sh wires them.
  sed -n '/^run_stage_for() {/,/^# Build the model invocation/p' "$REPO_ROOT/templates/scripts/bureau-config.sh" >> "$SCRIPTS_DIR/bureau-config.sh"
  printf 'BUREAU_RUNTIME="$(dirname "$0")/bureau-runtime.py"\n' >> "$SCRIPTS_DIR/bureau-config.sh"
  # Control: the v3.0.2 launch of the provider too (before the fix round it inherited everything).
  [ "${2:-}" != control ] || sed -i.bak 's/bureau_without_secrets python3 -I "\$(dirname/python3 "$(dirname/' "$SCRIPTS_DIR/bureau-config.sh"
  # Control: the v3.0.2 behaviour at every site, the adapter's login check and
  # child environment included (the child gets a copy of everything; run()
  # builds it as child_env so a Codex child can get its own TMPDIR).
  [ "${2:-}" != control ] || { pr1_passthrough "$SCRIPTS_DIR"
    sed -i.bak -e "s/env=untrusted_env(os.environ, options.get('untrusted_env', 'default'), runner)/env=None/" \
      -e "s/child_env = untrusted_env(os.environ, options.get('untrusted_env', 'default'), runner)/child_env = dict(os.environ)/" \
      "$SCRIPTS_DIR/bureau-provider.py"
    { [ "$(grep -c 'env=None' "$SCRIPTS_DIR/bureau-provider.py")" = 1 ] \
      && [ "$(grep -c 'child_env = dict(os.environ)' "$SCRIPTS_DIR/bureau-provider.py")" = 1 ]; } \
      || fail "5 control: could not restore the inherited environment in the adapter copy"; }
  mkdir -p "$PR1_MARKS/agent-bin"
  cat > "$PR1_MARKS/agent-bin/codex" <<EOF
#!/usr/bin/env python3
import json, os, pathlib, sys
if sys.argv[1] == 'login': sys.exit(0)
with open('$PR1_MARKS/agent.env', 'a') as out:
    out.write('--- run\n' + ''.join(k + '=' + v + '\n' for k, v in os.environ.items()))
with open('$PR1_MARKS/seq.log', 'a') as seq: seq.write('untrusted agent\n')
sys.stdin.read()
pathlib.Path('feature.py').write_text('def add(a, b):\n    return a + b\n')
for path in pathlib.Path('specs').glob('*/tasks.md'): path.write_text(path.read_text().replace('[ ]', '[X]'))
value = {'status': 'COMPLETE', 'tasks_done': 3, 'tasks_skipped': 0, 'tasks_needs_human': 0, 'fixed_review_items': [],
         'notes': {'needs_human': [], 'skipped': [], 'deviations': []}, 'prose_notes': 'fixture'}
pathlib.Path(sys.argv[sys.argv.index('-o') + 1]).write_text(json.dumps(value))
print(json.dumps({'type': 'turn.completed', 'usage': {}}))
EOF
  chmod +x "$PR1_MARKS/agent-bin/codex"
  PATH="$PR1_MARKS/agent-bin:$PATH" run_implement_pipeline EXP-804 </dev/null; set +e
  unset BUREAU_CONFIG BUREAU_STUB_RUNNER BUREAU_USE_GOAL_LOOP OPENAI_API_KEY BUREAU_PROVIDER_LOG_DIR
}
codex_run ''
[ "$LAST_RC" = 0 ] || { fail "5 codex default: expected 0, got $LAST_RC"; printf '%s\n' "$LAST_STDERR" | tail -8 >&2; }
pr1_check_env "$PR1_MARKS/agent.env" "5 codex agent default" default 1 OPENAI_API_KEY=sk-openai-PROBE-agent-login
pr1_check_env "$PR1_MARKS/hook.env" "5 codex hook default" default 1 BUREAU_ISSUE=EXP-804 BUREAU_BRANCH=test-branch
pr1_check_env "$PR1_MARKS/completion.env" "5 codex completion test default" default
pr1_check_bureau_calls "5 codex default" gh linear
teardown
codex_run '"clean"'
[ "$LAST_RC" = 0 ] || { fail "5 codex clean: expected 0, got $LAST_RC"; printf '%s\n' "$LAST_STDERR" | tail -8 >&2; }
pr1_check_env "$PR1_MARKS/agent.env" "5 codex agent clean" clean 1 OPENAI_API_KEY=sk-openai-PROBE-agent-login
pr1_check_env "$PR1_MARKS/completion.env" "5 codex completion test clean" clean
if grep -q '^OPENAI_API_KEY=' "$PR1_MARKS/completion.env"; then fail "5 codex clean: the agent's login reached the completion test"; fi
teardown
codex_run '' control
grep -qF "LINEAR_API_KEY=$PR1_LINEAR" "$PR1_MARKS/agent.env" 2>/dev/null \
  && grep -qF "LINEAR_API_KEY=$PR1_LINEAR" "$PR1_MARKS/completion.env" \
  || fail "5 control: with the v3.0.2 behaviour the agent and the completion test should have seen the Linear key"
teardown
pr1_pass "5 Codex implement: the agent, the hook and the completion test run without the secrets"

# ── 6  bureau-app.sh test ────────────────────────────────────────────────────
app_run() {
  rm -rf "$TMPD/app"; mkdir -p "$TMPD/app"
  PR1_MARKS="$TMPD/app"
  jq -n --arg c "$(pr1_dump app); exit 7" '{repo: {test_command: $c}}' > "$TMPD/app/.bureau.json"
  (cd "$TMPD/app" && env LINEAR_API_KEY="$PR1_LINEAR" TELEGRAM_BOT_TOKEN="$PR1_TG_TOKEN" TELEGRAM_ALERT_CHAT_ID="$PR1_TG_CHAT" \
     GH_TOKEN="$PR1_GH" GITHUB_TOKEN="$PR1_GITHUB" GH_ENTERPRISE_TOKEN="$PR1_GHE" GITHUB_ENTERPRISE_TOKEN="$PR1_GITHUBE" \
     OPERATOR_TOOL_VAR="$PR1_OPERATOR" BUREAU_CONFIG="$TMPD/app/.bureau.json" bash "$1" test) >/dev/null 2>&1
}
app_run "$REPO_ROOT/templates/scripts/bureau-app.sh"; rc=$?
[ "$rc" = 7 ] || fail "6 app: the test command's exit 7 came back as $rc"
pr1_check_env "$TMPD/app/app.env" "6 app test" default
mkdir -p "$TMPD/app-old/scripts"; cp "$REPO_ROOT/templates/scripts/"*.sh "$TMPD/app-old/scripts/"
pr1_passthrough "$TMPD/app-old/scripts"
app_run "$TMPD/app-old/scripts/bureau-app.sh"
grep -qF "GH_TOKEN=$PR1_GH" "$TMPD/app/app.env" 2>/dev/null || fail "6 control: with the v3.0.2 behaviour the app test should have seen GH_TOKEN"
pr1_pass "6 bureau-app.sh test runs without the secrets and keeps the exit status"

# ── 7  upstream-port.sh build and test (the real section) ─────────────────────
SECTION="$TMPD/upstream-section.sh"
sed -n '/^# Step 9.75 — resolve configurable build/,/^# Step 12 — commit the port/p' "$REPO_ROOT/templates/scripts/upstream-port.sh" > "$SECTION"
grep -q 'PORT_TEST_CMD' "$SECTION" || fail "7: build/test section not found in upstream-port.sh"
port_run() {  # <untrusted_env JSON or ''> [control]
  rm -rf "$TMPD/port"; mkdir -p "$TMPD/port/work"
  PR1_MARKS="$TMPD/port"
  jq -n --arg b "$(pr1_dump build)" --arg t "$(pr1_dump test)" --arg w "$TMPD/port/work" --argjson u "${1:-null}" \
    '{repo: ({upstream_port: {build_cmd: $b, test_cmd: $t, work_dir: $w}} + (if $u == null then {} else {untrusted_env: $u} end))}' > "$TMPD/port/.bureau.json"
  env LINEAR_API_KEY="$PR1_LINEAR" TELEGRAM_BOT_TOKEN="$PR1_TG_TOKEN" TELEGRAM_ALERT_CHAT_ID="$PR1_TG_CHAT" \
    GH_TOKEN="$PR1_GH" GITHUB_TOKEN="$PR1_GITHUB" GH_ENTERPRISE_TOKEN="$PR1_GHE" GITHUB_ENTERPRISE_TOKEN="$PR1_GITHUBE" \
    OPERATOR_TOOL_VAR="$PR1_OPERATOR" BUREAU_CONFIG="$TMPD/port/.bureau.json" CONTROL="${2:-}" \
    /bin/bash -c 'set -euo pipefail
      source "$1"
      if [ "$CONTROL" = control ]; then bureau_untrusted_env() { [ "${1:-}" = --check ] && return 0; env "$@"; }; fi
      SCRIPT_DIR=/nonexistent; TMP_BUILD_LOG="$2/build.log"; TMP_TEST_LOG="$2/test.log"
      EXIT_BUILD_FAILED=14; EXIT_TEST_FAILED=15; EXIT_GH_FAILED=18
      log_step() { :; }
      on_failure() { echo "on_failure $1 $2"; exit "$1"; }
      bureau_get() { jq -r "$1" "$BUREAU_CONFIG"; }
      source "$3"
      echo port-ok' _ "$ENV_SH" "$TMPD/port" "$SECTION" > "$TMPD/port/out" 2>&1
}
port_run ''; rc=$?
[ "$rc" = 0 ] || fail "7 upstream-port: expected 0, got $rc: $(tail -3 "$TMPD/port/out")"
pr1_check_env "$TMPD/port/build.env" "7 upstream-port build" default
pr1_check_env "$TMPD/port/test.env" "7 upstream-port test" default
port_run '"cleen"'; rc=$?
[ "$rc" = 18 ] || fail "7 upstream-port invalid: expected the pre-flight 18, got $rc"
[ ! -e "$TMPD/port/build.env" ] || fail "7 upstream-port invalid: the build ran"
port_run '' control
grep -qF "LINEAR_API_KEY=$PR1_LINEAR" "$TMPD/port/build.env" 2>/dev/null || fail "7 control: with the v3.0.2 behaviour the build should have seen the Linear key"
pr1_pass "7 upstream-port.sh builds and tests the ported code without the secrets"

if [ "$PR1_FAILS" != 0 ]; then echo "$PR1_FAILS check(s) failed" >&2; exit 1; fi
echo "OK test_untrusted_env"
