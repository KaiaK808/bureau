#!/bin/bash
# Linear and GitHub comments are fitted to about 60 KB, so a long comment never fails a stage.
#
# Linear refuses a comment body over its limit with an error answer; the transport retried
# it and ended with 27 "Linear unusable", so one long model answer halted a stage as if
# Linear were down. GitHub refuses a PR comment over 65,536 characters, and the review
# stage swallowed that. bureau_cap_comment keeps the beginning and the end of a long text
# (the first line: a branch marker or the "Code Review … Changes Requested" header; the
# last: the fenced JSON verdict) and says in the middle how much it left out.
#
#   1. the REAL bureau_cap_comment: a text that fits is unchanged; a longer one is at most
#      the limit, valid UTF-8, keeps its first line and its closing JSON block, names the cut
#   2. the REAL post_comment against a curl double that refuses bodies over 65,536 bytes as
#      Linear does: a 200 KB body is posted, fitted
#   3. the REAL review stage: a 150 KB merger comment reaches the PR fitted, with its header
# Negative controls: post_comment without the cap sends the whole body and fails with 27;
# the review without it posts more than GitHub accepts.
set -euo pipefail
source "$(dirname "$0")/lib/harness.sh"
source "$(dirname "$0")/lib/pr2-gate.sh"
unset BUREAU_CALLER_STOP
T=$(mktemp -d -t bureau-test.cap.XXXXXXXX)
trap 'teardown || true; rm -rf "$T"' EXIT
fail() { echo "FAIL: $1" >&2; exit 1; }
REAL="$REPO_ROOT/templates/scripts/bureau-config.sh"

sed -n -e '/^BUREAU_COMMENT_MAX_BYTES=/p' -e '/^bureau_cap_comment() {/,/^}/p' "$REAL" > "$T/cap.sh"
grep -q '^bureau_cap_comment() {' "$T/cap.sh" || fail 'bureau_cap_comment not found in bureau-config.sh'
# shellcheck source=/dev/null
source "$T/cap.sh"
utf8_ok() { python3 -c 'import sys; sys.stdin.buffer.read().decode("utf-8")' 2>/dev/null; }
bytes() { LC_ALL=C wc -c | tr -d ' '; }

# A long body shaped like the stages' own: marker first, fenced JSON verdict last,
# multi-byte text in between.
make_long() {  # <approx bytes> <file>
  python3 - "$1" "$2" <<'PY'
import json, sys
n, path = int(sys.argv[1]), sys.argv[2]
line = "- `src/ä.py:12` — Grüße 😀 der Fehler liegt hier, 42 € Schaden.\n"
body = "<!-- bureau-branch: feat/cap-test -->\n🔄 Code Review: **Changes Requested** (cycle 1/3)\n\n"
while len(body.encode()) < n:
    body += line
body += "\n```json\n" + json.dumps({"verdict": "REQUEST_CHANGES", "bugs": 1, "security_issues": 0}) + "\n```"
open(path, "w").write(body)
PY
}

# ── 1. the helper ──────────────────────────────────────────────────────────
printf 'short comment\nwith two lines ✓' > "$T/short"
bureau_cap_comment "$(cat "$T/short")" > "$T/short.out"
cmp -s "$T/short" "$T/short.out" || fail '1: a short comment was changed'
python3 -c 'import sys; sys.stdout.write("a" * 60000)' > "$T/exact"
[ "$(bureau_cap_comment "$(cat "$T/exact")" | bytes)" = 60000 ] || fail '1: a comment of exactly the limit was cut'
python3 -c 'import sys; sys.stdout.write("a" * 60001)' > "$T/over"
[ "$(bureau_cap_comment "$(cat "$T/over")" | bytes)" -le 60000 ] || fail '1: one byte over the limit was not cut'
make_long 200000 "$T/long"
bureau_cap_comment "$(cat "$T/long")" > "$T/long.out"
[ "$(bytes < "$T/long.out")" -le 60000 ] || fail "1: fitted comment is $(bytes < "$T/long.out") bytes"
[ "$(bytes < "$T/long.out")" -ge 55000 ] || fail "1: fitted comment is only $(bytes < "$T/long.out") bytes"
utf8_ok < "$T/long.out" || fail '1: the fitted comment is not valid UTF-8'
[ "$(head -n 1 "$T/long.out")" = '<!-- bureau-branch: feat/cap-test -->' ] || fail '1: the first line was lost'
sed -n 2p "$T/long.out" | grep -q 'Code Review.*Changes Requested' || fail "1: the review header was lost"
tail -n 3 "$T/long.out" | sed -n '2p' | jq -e '.verdict == "REQUEST_CHANGES"' >/dev/null || fail '1: the closing JSON verdict was lost'
[ "$(tail -n 1 "$T/long.out")" = '```' ] || fail '1: the closing fence was lost'
cut=$(grep -oE '\[… [0-9]+ bytes cut from the middle' "$T/long.out" | grep -oE '[0-9]+') || fail '1: no note about the cut'
note_len=$(grep -oE '\[… [0-9]+ bytes cut from the middle[^]]*\]' "$T/long.out" | tr -d '\n' | bytes)
[ "$(( $(bytes < "$T/long") - cut ))" = "$(( $(bytes < "$T/long.out") - note_len - 4 ))" ] \
  || fail "1: the note's count ($cut) does not match what was left out"
# Both cuts fall on line breaks: every line but the note is a whole line of the original.
python3 - "$T/long" "$T/long.out" <<'PY' || fail '1: a cut split a line'
import sys
orig = set(open(sys.argv[1], encoding='utf-8').read().split('\n'))
for line in open(sys.argv[2], encoding='utf-8').read().split('\n'):
    if line and not line.startswith('[… ') and line not in orig:
        sys.exit('not a whole line of the original: %r' % line[:80])
PY
# Invalid UTF-8 in, valid UTF-8 out; a single line without breaks is cut too.
printf 'bad \377\376 bytes' > "$T/bad"
bureau_cap_comment "$(cat "$T/bad")" | utf8_ok || fail '1: invalid UTF-8 passed through'
python3 -c 'import sys; sys.stdout.write("é" * 50000)' > "$T/oneline"
bureau_cap_comment "$(cat "$T/oneline")" > "$T/oneline.out"
[ "$(bytes < "$T/oneline.out")" -le 60000 ] && utf8_ok < "$T/oneline.out" || fail '1: a single long line was not fitted cleanly'
[ "$(bureau_cap_comment "$(cat "$T/long")" 1000 | bytes)" -le 1000 ] || fail '1: a smaller limit was not honoured'
echo 'PASS 1 bureau_cap_comment: unchanged when it fits; otherwise at most the limit, valid UTF-8, head and verdict kept, cut named'

# ── 2. the real post_comment ───────────────────────────────────────────────
mkdir -p "$T/linear/bin" "$T/linear/scripts" "$T/linear/payloads"
cp "$REAL" "$REPO_ROOT/templates/scripts/bureau-env.sh" "$T/linear/scripts/"
cat > "$T/linear/.bureau.json" <<'EOF'
{"linear":{"teams":[{"id":"team-id","key":"EXP","name":"Test","states":{"triage":"s1","spec":"s2","spec_review":"s3","design":"s4","build":"s5","build_review":"s6","done":"s7"}}],
 "labels":{"lane2":{"id":"l1","name":"lane-2"},"needs_human":{"id":"l2","name":"needs-human"},"needs_ux":{"id":"l3","name":"needs-ux"},"ai_implementable":{"id":"l4","name":"ai-implementable"}},"projects":[]},
 "agents":{},"repo":{"branch_prefix":"feat","specs_dir":"specs"}}
EOF
# curl: every payload in its own file; a comment body over 65,536 bytes gets the error
# answer Linear gives, anything else a healthy answer.
cat > "$T/linear/bin/curl" <<EOF
#!/bin/bash
prev=""; payload=""
for a in "\$@"; do [ "\$prev" = -d ] && payload="\$a"; prev="\$a"; done
n=\$(ls "$T/linear/payloads" | wc -l | tr -d ' ')
printf '%s' "\$payload" > "$T/linear/payloads/\$n.json"
if [ "\$(printf '%s' "\$payload" | jq -r '.variables.body // ""' | LC_ALL=C wc -c | tr -d ' ')" -gt 65536 ]; then
  printf '%s' '{"errors":[{"message":"Body too long"}],"data":null}'
else
  printf '%s' '{"data":{"issues":{"nodes":[{"id":"UUID-1","identifier":"EXP-1","title":"T","description":"D","branchName":"feat/x","state":{"id":"s5","name":"Build"},"labels":{"nodes":[]},"comments":{"nodes":[]}}]},"commentCreate":{"success":true}}}'
fi
"$REPO_ROOT/tests/lib/curl-writeout.sh" 200 "\$@"
EOF
chmod +x "$T/linear/bin/curl"
post() {  # <body file> — the real post_comment; sets PRC and BODY (the posted body)
  rm -f "$T/linear/payloads/"*
  set +e
  ( cd "$T/linear" && PATH="$T/linear/bin:$PATH" LINEAR_API_KEY=lin_api_test BUREAU_LINEAR_RETRIES=0 /bin/bash -c '
      set -uo pipefail
      source scripts/bureau-config.sh
      post_comment EXP-1 "$(cat "$1")"' _ "$1" ) > "$T/post.out" 2> "$T/post.err"
  PRC=$?
  set -e
  BODY=$(for f in "$T/linear/payloads/"*.json; do jq -r 'select(.query | test("commentCreate")) | .variables.body' "$f"; done)
}
post "$T/long"
[ "$PRC" = 0 ] || fail "2: posting a 200 KB comment failed ($PRC): $(cat "$T/post.err")"
[ "$(printf '%s' "$BODY" | bytes)" -le 60000 ] || fail "2: posted $(printf '%s' "$BODY" | bytes) bytes"
[ "$(printf '%s\n' "$BODY" | head -n 1)" = '<!-- bureau-branch: feat/cap-test -->' ] || fail '2: the branch marker is not the first line any more'
printf '%s\n' "$BODY" | tail -n 2 | head -n 1 | jq -e .verdict >/dev/null || fail '2: the JSON verdict is not the end any more'
post "$T/short"
[ "$PRC" = 0 ] && [ "$BODY" = "$(cat "$T/short")" ] || fail '2: a short comment was not posted unchanged'
echo 'PASS 2 post_comment posts a 200 KB comment fitted; a short one unchanged'

# Negative control: post_comment without the cap sends the whole body, and Linear's
# refusal ends the stage with 27.
python3 - "$T/linear/scripts/bureau-config.sh" <<'PY'
import sys
p = sys.argv[1]; src = open(p).read()
old = '  if capped=$(bureau_cap_comment "$body"); then\n'
assert src.count(old) == 1, 'cap call not found'
open(p, 'w').write(src.replace(old, '  if false; then\n'))
PY
post "$T/long"
[ "$PRC" = 27 ] || fail "negative control: without the cap a 200 KB comment should fail with 27, got $PRC"
[ "$(printf '%s' "$BODY" | bytes)" -gt 65536 ] || fail 'negative control: without the cap the whole body should be sent'
echo 'PASS negative control: without the cap the long comment is refused and the stage gets 27'

# ── 3. the review stage's PR comment ───────────────────────────────────────
review_long() {  # [nocap]
  teardown || true
  sandbox_init EXP-804 test-branch
  printf 'change\n' > "$SANDBOX/change.txt"
  git -C "$SANDBOX" add change.txt && git -C "$SANDBOX" commit -q -m 'fixture change' && git -C "$SANDBOX" push -q origin test-branch
  printf 'Specialist prose.\n```json\n{"specialist":"x","counts":{"critical":0,"bug":0,"minor":0,"skip":0},"findings":[],"summary":""}\n```\n' > "$SANDBOX/specialist.txt"
  make_long 150000 "$T/merger-comment"
  jq -n --rawfile c "$T/merger-comment" '{verdict:"REQUEST_CHANGES",bugs:1,security_issues:0,missing_acceptance:[],fixes_needed:["x"],summary:"s",comment:$c,findings:[]}' > "$SANDBOX/merger.txt"
  export FAKE_CLAUDE_FIXTURES="$SANDBOX/specialist.txt" FAKE_CLAUDE_MERGE_FIXTURE="$SANDBOX/merger.txt"
  export BUREAU_STUB_ISSUE_STATE='Build Review' BUREAU_STUB_STATE_MERGE=state-merge BUREAU_STUB_AGENT_ENABLED=merge
  export BUREAU_NO_MERGE=0 BUREAU_STOP_REQUESTED=0
  pr2_gate_setup
  if [ "${1:-}" = nocap ]; then
    python3 - "$SCRIPTS_DIR/code-review-pipeline.sh" <<'PY'
import sys
p = sys.argv[1]; src = open(p).read()
old = 'REVIEW_COMMENT_BODY=$(bureau_cap_comment "$REVIEW_COMMENT") || REVIEW_COMMENT_BODY="$REVIEW_COMMENT"\n'
assert src.count(old) == 1, 'PR comment cap not found'
open(p, 'w').write(src.replace(old, 'REVIEW_COMMENT_BODY="$REVIEW_COMMENT"\n'))
PY
  fi
  run_pipeline code-review-pipeline.sh EXP-804
  jq -j '[.[] | select(.body | test("Code Review v2"))] | last | .body // ""' "$PR2_GH/comments.json" > "$T/pr-comment"
}
review_long
[ "$LAST_RC" = 0 ] || fail "3: the review ended $LAST_RC"
[ "$(bytes < "$T/pr-comment")" -le 60000 ] || fail "3: the PR comment is $(bytes < "$T/pr-comment") bytes"
utf8_ok < "$T/pr-comment" || fail '3: the PR comment is not valid UTF-8'
[ "$(head -n 1 "$T/pr-comment")" = '## Code Review v2 — EXP-804' ] || fail '3: the PR comment lost its header'
grep -q '^\*\*Verdict\*\*: REQUEST_CHANGES$' "$T/pr-comment" || fail '3: the PR comment lost the verdict line the gate reads'
[ "$(tail -n 1 "$T/pr-comment")" = '*Automated review by Bureau pipeline*' ] || fail '3: the PR comment lost its end'
grep -q 'bytes cut from the middle' "$T/pr-comment" || fail '3: the PR comment does not say it was cut'
echo 'PASS 3 the review PR comment is fitted and keeps its header, verdict and end'

review_long nocap
[ "$(python3 -c 'import sys; print(len(sys.stdin.buffer.read().decode()))' < "$T/pr-comment")" -gt 65536 ] \
  || fail 'negative control: without the cap the PR comment should exceed what GitHub accepts'
echo 'PASS negative control: without the cap the review posts more than 65,536 characters'

echo 'OK test_comment_cap'
