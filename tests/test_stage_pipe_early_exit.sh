#!/bin/bash
# No stage hands a long text to a reader that leaves early through a pipe.
#
# Under `set -o pipefail` a pipeline fails when its writer still has output after the reader
# left (`head -n 1`, `grep -q`): the writer dies of SIGPIPE (141) or, with SIGPIPE ignored,
# gets EPIPE ("write error: Broken pipe", 1). Three stage lines did that with model or API
# text, and it is certain once the text is longer than the pipe buffer plus what the reader
# took:
#   1. spec-pipeline.sh: `printf '%s' "$RESEARCH_RAW" | grep -q '<!-- bureau-research:'`
#      took a long digest with its marker near the top for "no valid output" and dropped it;
#   2. code-review-pipeline.sh: `VERDICT=$(printf '%s\n' "$_decision" | head -n 1)` ended
#      the stage (set -e) right after the paid review when the decision ran long;
#   3. upstream-port.sh: `gh api … --jq '.commit.message' 2>&1 | head -n 1` reported a long
#      commit message as "could not fetch commit message" (exit 18).
#
# Each part runs the REAL script in a sandbox, doubles only at the edges (Linear, gh, the
# model; for 2 the decision's note lines), with texts over 1 MB, under the suite's bash
# with SIGPIPE ignored and with SIGPIPE default. Negative controls put each old line back
# into the sandbox copy (CI checks out without history) and show the same run fails.
set -euo pipefail
source "$(dirname "$0")/lib/harness.sh"
unset BUREAU_CALLER_STOP

SCRIPTS="$REPO_ROOT/templates/scripts"
T=$(mktemp -d -t bureau-test.stagepipe.XXXXXXXX)
trap 'teardown || true; rm -rf "$T"' EXIT
fail() { echo "FAIL: $1" >&2; for f in "$T/out" "$T/err"; do [ ! -f "$f" ] || cut -c1-300 "$f" | tail -15 >&2; done; exit 1; }

# run_sig <ignore|default> <dir> <script> [args…]: the script under "$BASH" from <dir>, with
# SIGPIPE ignored or default, stdin closed. Sets RC; stdout in $T/out, stderr in $T/err.
run_sig() {
  local sig="$1" dir="$2"; shift 2
  set +e
  ( cd "$dir" && python3 -I -c 'import os, signal, sys
signal.signal(signal.SIGPIPE, signal.SIG_IGN if sys.argv[1] == "ignore" else signal.SIG_DFL)
os.execv(sys.argv[2], sys.argv[2:])' "$sig" "$BASH" "$@" > "$T/out" 2> "$T/err" < /dev/null )
  RC=$?
  set -e
}
# run_sig itself: SIGPIPE as named reaches the script and what it starts.
for sig in ignore default; do
  run_sig "$sig" "$T" -c 'yes | head -c 1 > /dev/null; echo "${PIPESTATUS[0]}"'
  got=$(cat "$T/out")
  if [ "$sig" = default ]; then [ "$got" = 141 ]; else [ -n "$got" ] && [ "$got" != 141 ]; fi \
    || fail "run_sig $sig: yes ended '$got' on its broken pipe"
done
# put_back <file> <new text> <old text>: a negative control's old line, in a sandbox copy only.
put_back() {
  python3 -I - "$@" <<'PY' || fail "CONTROL PATCH FAILED: $1"
import sys
path, new, old = sys.argv[1:4]
src = open(path).read()
if src.count(new) != 1:
    sys.exit('anchor found %d times in %s' % (src.count(new), path))
open(path, 'w').write(src.replace(new, old))
PY
}
# Over 1 MB of text, numbered lines.
python3 -I -c 'import sys
open(sys.argv[1], "w").write("".join("- digest line %06d: endpoint, version, gotcha\n" % i for i in range(24000)))' "$T/lines"
[ "$(wc -c < "$T/lines" | tr -d ' ')" -gt 1000000 ] || fail 'the long text is not over 1 MB'

# ── 1. spec: a long research digest with its marker on the first line ──────
{ printf '<!-- bureau-research: stripe-webhooks -->\n## Stripe Webhooks\n'; cat "$T/lines"; } > "$T/research"
# The model double: the first call (research) answers the long digest, every later one a
# filler; it notes when a later prompt carries the research block. A shell function, so
# the 1 MB prompt of the calls after research is not an exec argument.
research_double() {
  local n
  n=$(cat "$SANDBOX/research_double.n" 2>/dev/null || echo 0); n=$((n + 1))
  echo "$n" > "$SANDBOX/research_double.n"
  case "$*" in *'--- API research (auto-generated'*) echo "call $n carries the research" >> "$SANDBOX/research_seen" ;; esac
  if [ "$n" = 1 ]; then cat "$RESEARCH_FILE"; else cat "$FILLER_FILE"; fi
}
export -f research_double
NEW_SPEC="  if grep -q '<!-- bureau-research:' <<< \"\$RESEARCH_RAW\"; then"
OLD_SPEC="  if printf '%s' \"\$RESEARCH_RAW\" | grep -q '<!-- bureau-research:'; then"
spec_run() {  # <ignore|default> [old]
  sandbox_init EXP-100 test-branch
  [ -z "${2:-}" ] || put_back "$SCRIPTS_DIR/spec-pipeline.sh" "$NEW_SPEC" "$OLD_SPEC"
  export BUREAU_STUB_ISSUE_STATE=Triage BUREAU_STUB_LABELS='["needs-research","lane-2"]' BUREAU_STUB_AGENT_ENABLED=research
  export FAKE_CLAUDE_BIN=research_double RESEARCH_FILE="$T/research" FILLER_FILE="$FIXTURES_DIR/claude_filler.txt"
  run_sig "$1" "$SANDBOX" "$SCRIPTS_DIR/spec-pipeline.sh" EXP-100
}
for sig in ignore default; do
  spec_run "$sig"
  grep -q 'research complete; label stripped' "$T/out" || fail "1 $sig: the long digest was not taken (exit $RC)"
  grep -q 'no valid output' "$T/out" && fail "1 $sig: the long digest was called invalid"
  grep -q $'^post_comment\tEXP-100\t<!-- bureau-research: stripe-webhooks -->' "$SANDBOX/calls.log" || fail "1 $sig: the digest was not posted to Linear"
  grep -q $'^remove_issue_label\tEXP-100\tneeds-research' "$SANDBOX/calls.log" || fail "1 $sig: needs-research was not removed"
  grep -q '^call 2 carries the research' "$SANDBOX/research_seen" 2>/dev/null || fail "1 $sig: the specify prompt did not get the research"
  teardown
  spec_run "$sig" old
  grep -q 'research produced no valid output' "$T/out" || fail "1 negative control, $sig: the old line took the long digest, so this proves nothing (exit $RC)"
  grep -q $'^post_comment\tEXP-100\t<!-- bureau-research:' "$SANDBOX/calls.log" && fail "1 negative control, $sig: the old line posted the digest"
  teardown
done
echo 'PASS 1 spec takes a research digest over 1 MB with its marker first, SIGPIPE ignored and default (the old pipe dropped it as no valid output)'

# ── 2. review: a decision with long note lines after the verdict ───────────
NEW_REVIEW="VERDICT=\"\${_decision%%\$'\\n'*}\""
OLD_REVIEW="VERDICT=\$(printf '%s\\n' \"\$_decision\" | head -n 1)"
# The note lines: 16 rule records of 64 KB each, after whatever the real decision says.
python3 -I -c 'import sys
open(sys.argv[1], "w").write("".join("\npipe-probe-%02d\037\037" % i + "probe note " + "x" * 65536 for i in range(16)))' "$T/notes"
review_run() {  # <ignore|default> <long|plain> [old]
  sandbox_init EXP-702 test-branch
  printf 'change\n' > "$SANDBOX/change.txt"
  git -C "$SANDBOX" add change.txt && git -C "$SANDBOX" commit -q -m 'fixture change' && git -C "$SANDBOX" push -q origin test-branch
  jq -n '{repo: {test_command: "true"}}' > "$SANDBOX/.bureau.json"
  printf 'Review.\n```json\n{"verdict":"APPROVE","bugs":0,"security_issues":0,"counts":{"critical":0},"findings":[],"summary":"fixture"}\n```\n' > "$SANDBOX/verdict.txt"
  export FAKE_CLAUDE_BIN="$LIB_DIR/fake_claude.sh" FAKE_CLAUDE_FIXTURES="$SANDBOX/verdict.txt"
  export BUREAU_STUB_REVIEW_COMMENTS='[]' BUREAU_STUB_ISSUE_STATE='Build Review' GH_STUB_EXISTING_PR=99
  export BUREAU_STUB_STATE_MERGE='' BUREAU_STUB_AGENT_ENABLED='' BUREAU_NO_MERGE=1 BUREAU_STOP_REQUESTED=0
  if [ "$2" = long ]; then
    # The decision double: the real decision, then the long note lines.
    cat >> "$SCRIPTS_DIR/real-helpers.sh" <<EOF
eval "\$(declare -f decide_review_verdict | sed '1s/^decide_review_verdict /_real_decide_review_verdict /')"
decide_review_verdict() { _real_decide_review_verdict "\$@"; cat '$T/notes'; }
EOF
  fi
  [ -z "${3:-}" ] || put_back "$SCRIPTS_DIR/code-review-pipeline.sh" "$NEW_REVIEW" "$OLD_REVIEW"
  run_sig "$1" "$SANDBOX" "$SCRIPTS_DIR/code-review-pipeline.sh" EXP-702
  rm -rf "$(sed -n 's/^code-review failed .*preserved at //p' "$T/err")"
}
review_run ignore plain
PLAIN_RC=$RC
grep -q '^\*\*Verdict\*\*: APPROVE' "$SANDBOX/gh_calls.log" || fail "2: the plain review did not post its APPROVE (exit $RC)"
teardown
for sig in ignore default; do
  review_run "$sig" long
  [ "$RC" = "$PLAIN_RC" ] || fail "2 $sig: with long note lines the review ended $RC, the plain one $PLAIN_RC"
  grep -q '^\*\*Verdict\*\*: APPROVE' "$SANDBOX/gh_calls.log" || fail "2 $sig: the verdict did not reach the PR"
  grep -q '^  Verdict rule pipe-probe-15: probe note x' "$T/out" || fail "2 $sig: the last note line was not read"
  teardown
  review_run "$sig" long old
  [ "$RC" != "$PLAIN_RC" ] || fail "2 negative control, $sig: the old line ended like the plain review, so this proves nothing"
  grep -q 'Verdict\*\*: ' "$SANDBOX/gh_calls.log" 2>/dev/null && fail "2 negative control, $sig: the old line still posted a verdict"
  # 141, or 1 with the write error: bash 3.2 prints it under the default setting too,
  # because the stage has an EXIT trap.
  [ "$RC" = 141 ] || grep -q 'printf: write error: Broken pipe' "$T/err" \
    || fail "2 negative control, $sig: the old line did not end on a broken pipe (exit $RC)"
  teardown
done
echo "PASS 2 review reads its verdict from a decision with 1 MB of note lines and ends as the plain review ($PLAIN_RC), SIGPIPE ignored and default (the old pipe ended the stage)"

# ── 3. upstream-port: a commit message over 1 MB ───────────────────────────
NEW_PORT="  --jq '.commit.message' 2>&1)\" || {"
OLD_PORT="  --jq '.commit.message' 2>&1 | head -n 1)\" || {"
FULL=0123456789abcdef0123456789abcdef01234567
{ printf 'Port the widget parser to the new tokenizer\n\nWhy:\n'; cat "$T/lines"; } > "$T/message"
mkdir -p "$T/bin"
cat > "$T/bin/gh" <<EOF
#!/bin/bash
# gh double: auth is fine; the commit resolves to $FULL; its message is over 1 MB.
case "\$*" in
  'auth status') exit 0 ;;
  "api repos/example/upstream/commits/0123456 --jq .sha") echo $FULL ;;
  "api repos/example/upstream/commits/$FULL --jq .commit.message")
    [ -z "\${GH_DOUBLE_DOWN:-}" ] || { echo 'HTTP 502: Bad Gateway' >&2; exit 1; }
    cat '$T/message' ;;
  *) echo "gh double: unexpected \$*" >&2; exit 1 ;;
esac
EOF
chmod +x "$T/bin/gh"
port_run() {  # <ignore|default> [old]
  rm -rf "$T/port" "$T/porttmp"; mkdir -p "$T/port/scripts" "$T/porttmp"
  git -C "$T/port" init -q -b main
  echo '{}' > "$T/port/.bureau.json"
  cp "$SCRIPTS"/*.sh "$T/port/scripts/"
  [ -z "${2:-}" ] || put_back "$T/port/scripts/upstream-port.sh" "$NEW_PORT" "$OLD_PORT"
  git -C "$T/port" add -A && git -C "$T/port" -c user.email=t@t -c user.name=t commit -q -m init
  # The branch-collision guard right after the title stops the run there with 18.
  git -C "$T/port" branch upstream-port/0123456
  PATH="$T/bin:$PATH" TMPDIR="$T/porttmp" BUREAU_UPSTREAM_REPO=example/upstream \
    run_sig "$1" "$T/port" scripts/upstream-port.sh --sha 0123456
}
# The title section on its own, for the subject it builds.
sed -n '/^# Fetch upstream commit title/,/^SUBJECT=/p' "$SCRIPTS/upstream-port.sh" > "$T/title.sh"
grep -q '^SUBJECT=' "$T/title.sh" || fail '3: the title section is not in upstream-port.sh'
for sig in ignore default; do
  port_run "$sig"
  [ "$RC" = 18 ] && grep -q 'branch upstream-port/0123456 already exists locally' "$T/err" \
    || fail "3 $sig: the port did not get past the title to the branch guard (exit $RC)"
  grep -q 'could not fetch commit message' "$T/err" && fail "3 $sig: a long message was reported as not fetched"
  run_sig "$sig" "$T" -c 'set -euo pipefail
PATH="$1:$PATH"; UPSTREAM_REPO=example/upstream FULL_SHA="$2"
log_step() { :; }; on_failure() { echo "on_failure $*"; exit "$1"; }; EXIT_GH_FAILED=18
source "$3"; printf "%s\n" "$SUBJECT"' _ "$T/bin" "$FULL" "$T/title.sh"
  [ "$RC" = 0 ] && [ "$(cat "$T/out")" = 'port: upstream/0123456 Port the widget parser to the new tokenizer' ] \
    || fail "3 $sig: the subject is not the message's first line (exit $RC): $(cat "$T/out")"
  # A gh that fails still ends the port with 18 at the title.
  GH_DOUBLE_DOWN=1 port_run "$sig"
  [ "$RC" = 18 ] && grep -q 'could not fetch commit message' "$T/err" \
    || fail "3 $sig: a failing gh api did not end the port at the title with 18 (exit $RC)"
  port_run "$sig" old
  [ "$RC" = 18 ] && grep -q 'could not fetch commit message' "$T/err" \
    || fail "3 negative control, $sig: the old line fetched the long message, so this proves nothing (exit $RC)"
done
echo 'PASS 3 upstream-port takes the first line of a commit message over 1 MB, SIGPIPE ignored and default (the old pipe reported it as not fetched)'
