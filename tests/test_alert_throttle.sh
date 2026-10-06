#!/bin/bash
# Alert throttling is per repository, and the alert names the repository (v3.1).
#
# Runs the REAL alert_telegram and throttle helpers from templates/scripts/bureau-config.sh
# under /bin/bash in two sandbox repositories (one path with a space, one name with an
# underscore); only `curl` is a double, recording every Telegram post. The same issue,
# pipeline and exit code alert once per hour in each repository — before, one
# /tmp/bureau-alerts.log for every installation on the host let one repository silence the
# other. A worktree shares its repository's log; BUREAU_ALERT_THROTTLE_FILE names another
# file; without any git directory the log stays in /tmp and the key carries the path (read,
# never written here). The test never writes the operator's /tmp/bureau-alerts.log. The
# negative control runs the v3.0.2 helpers, with their path patched into the sandbox: the
# second repository's alert is swallowed, and no alert names its repository.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")" && cd .. && pwd)"
SCRIPTS="$REPO_ROOT/templates/scripts"
SB=$(cd "$(mktemp -d -t bureau-test.throttle.XXXXXXXX)" && pwd -P)
trap 'rm -rf "$SB"' EXIT
unset BUREAU_CONFIG BUREAU_DRY_RUN BUREAU_ALERT_THROTTLE_FILE 2>/dev/null || true
fail() { echo "FAIL $*" >&2; [ ! -f "$SB/posts.log" ] || sed 's/^/  | /' "$SB/posts.log" >&2; exit 1; }

# A key no other run on this host uses: the test must never reach the shared /tmp file.
ISSUE="EXP-$$$RANDOM"

mkdir -p "$SB/bin"
cat > "$SB/bin/curl" <<EOF
#!/bin/bash
prev=""; config=""
for a in "\$@"; do
  [ "\$prev" = --data-urlencode ] && case "\$a" in text=*) printf '%s\n----\n' "\${a#text=}" >> "$SB/posts.log" ;; esac
  [ "\$prev" = -K ] && [ "\$a" = - ] && config=\$(cat)
  prev="\$a"
done
# The alert text comes on stdin as a curl config line (curl -K -), with \n for a newline.
text=\$(printf '%s\n' "\$config" | sed -n 's/^data-urlencode = "text=\(.*\)"\$/\1/p')
[ -z "\$text" ] || printf '%s\n----\n' "\$text" >> "$SB/posts.log"
EOF
chmod +x "$SB/bin/curl"

new_repo() {  # <dir>
  mkdir -p "$1"
  git -C "$1" init -q
  git -C "$1" -c user.name=t -c user.email=t@t commit -q --allow-empty -m init
  cat > "$1/.bureau.json" <<'EOF'
{
  "linear": {
    "teams": [{"id": "t", "key": "EXP", "name": "T",
      "states": {"triage": "s1", "spec": "s2", "spec_review": "s3", "design": "s4",
                 "build": "s5", "build_review": "s6", "done": "s7"}}],
    "labels": {"lane2": {"id": "l1", "name": "lane-2"}, "needs_human": {"id": "l2", "name": "needs-human"},
               "needs_ux": {"id": "l3", "name": "needs-ux"}, "ai_implementable": {"id": "l4", "name": "ai-implementable"}},
    "projects": []
  },
  "agents": {"poll_interval_minutes": 30, "max_review_cycles": 3},
  "repo": {"branch_prefix": "feat", "specs_dir": "specs"}
}
EOF
}
A="$SB/repo a"; B="$SB/repo_b"
new_repo "$A"; new_repo "$B"

# run_in <dir> <snippet> [<config>] — the snippet with the real config sourced, from <dir>.
run_in() {
  (cd "$1" && PATH="$SB/bin:$PATH" TELEGRAM_BOT_TOKEN=t TELEGRAM_ALERT_CHAT_ID=c /bin/bash -c "
    set -euo pipefail
    source '${3:-$SCRIPTS/bureau-config.sh}'
    $2")
}
posts() { local n=0; [ ! -f "$SB/posts.log" ] || n=$(grep -c '^----$' "$SB/posts.log" || true); echo "${n:-0}"; }
ALERT="alert_telegram '$ISSUE' code-review-pipeline.sh 25 'needs a human'"

# --- one repository: once per hour -------------------------------------------------------
run_in "$A" "$ALERT"
run_in "$A" "$ALERT"
[ "$(posts)" = 1 ] || fail "the same alert twice in one repository posted $(posts) times, wanted 1"
grep -q "^alert|$ISSUE|code-review-pipeline.sh|25	[0-9]*$" "$A/.git/bureau/alert-throttle.log" \
  || fail "the key is not in the repository's own throttle log"
grep -qF 'Repo: `repo a`' "$SB/posts.log" || fail "the alert does not name the repository (as code)"
# A worktree of the same repository shares its log (and its name).
git -C "$A" worktree add -q "$SB/a-worktree" 2>/dev/null
run_in "$SB/a-worktree" "$ALERT"
[ "$(posts)" = 1 ] || fail "a worktree of the same repository alerted again"
# A worktree with its own copy of .bureau.json (some installations copy it) still names the
# main checkout, and still shares its log.
cp "$A/.bureau.json" "$SB/a-worktree/"
run_in "$SB/a-worktree" "alert_telegram '$ISSUE' qa-pipeline.sh 14 'build failed'"
[ "$(posts)" = 2 ] || fail "another pipeline was throttled"
grep -qF 'Repo: `repo a`' <<< "$(tail -n 8 "$SB/posts.log")" || fail "a worktree's alert does not name its main checkout"
run_in "$SB/a-worktree" "alert_telegram '$ISSUE' qa-pipeline.sh 14 'build failed'"
[ "$(posts)" = 2 ] || fail "a worktree with its own .bureau.json does not share the repository's log"
echo "PASS one repository alerts once per hour and key, its worktrees share the log, the alert names the main checkout"

# --- two repositories: each alerts -----------------------------------------------------------
run_in "$B" "$ALERT"
[ "$(posts)" = 3 ] || fail "the same key in a second repository was swallowed ($(posts) posts, wanted 3)"
grep -qF 'Repo: `repo_b`' "$SB/posts.log" || fail "the second repository is not named"
grep -q "^alert|$ISSUE|code-review-pipeline.sh|25	" "$B/.git/bureau/alert-throttle.log" || fail "the second repository has no log of its own"
run_in "$B" "$ALERT"
[ "$(posts)" = 3 ] || fail "the second repository alerted twice"
# The comment throttle of merge_origin_main_or_abort uses the same log.
run_in "$A" "_throttle_record 'merge-conflict|$ISSUE'"
run_in "$A" "_throttle_should_suppress 'merge-conflict|$ISSUE' 3600" || fail "a recorded key was not suppressed in its repository"
if run_in "$B" "_throttle_should_suppress 'merge-conflict|$ISSUE' 3600"; then fail "a key recorded in one repository suppressed another"; fi
echo "PASS two repositories alert and throttle independently, the merge-conflict comment too"

# --- the override, and no git directory at all ----------------------------------------------
run_in "$A" "export BUREAU_ALERT_THROTTLE_FILE='$SB/own file.log'; alert_telegram '$ISSUE' spec-pipeline.sh 13 x"
grep -q "^alert|$ISSUE|spec-pipeline.sh|13	" "$SB/own file.log" || fail "BUREAU_ALERT_THROTTLE_FILE was not used"
grep -q "spec-pipeline.sh" "$A/.git/bureau/alert-throttle.log" && fail "the override also wrote the repository's log"
# A key with a backslash (a repository path in the /tmp fallback may carry one) is matched
# as written, and a log that cannot be written never ends the caller: the alert still goes out.
run_in "$A" "export BUREAU_ALERT_THROTTLE_FILE='$SB/own file.log'; _throttle_record 'C:\\new|x'; _throttle_should_suppress 'C:\\new|x' 3600" \
  || fail "a key with a backslash was not found again"
: > "$SB/a file"
OUT=$(run_in "$A" "export BUREAU_ALERT_THROTTLE_FILE='$SB/a file/throttle.log'; alert_telegram '$ISSUE' ux-pipeline.sh 22 x; echo CONTINUED" 2>&1) \
  || fail "an unwritable throttle log ended the caller: $OUT"
case "$OUT" in *CONTINUED*) ;; *) fail "an unwritable throttle log stopped the caller" ;; esac
grep -q "Pipeline: ux-pipeline.sh" "$SB/posts.log" || fail "an unwritable throttle log swallowed the alert"
NOGIT="$SB/no git"; mkdir -p "$NOGIT"; cp "$A/.bureau.json" "$NOGIT/"
if git -C "$NOGIT" rev-parse --git-dir >/dev/null 2>&1; then
  echo "SKIP no-git fallback: the sandbox sits inside a git repository"
else
  WHERE=$(run_in "$NOGIT" "_throttle_where; printf '%s\n%s\n' \"\$_throttle_file\" \"\$_throttle_prefix\"")
  [ "$(printf '%s\n' "$WHERE" | sed -n 1p)" = /tmp/bureau-alerts.log ] || fail "without git the log is not /tmp/bureau-alerts.log: $WHERE"
  [ "$(printf '%s\n' "$WHERE" | sed -n 2p)" = "$NOGIT|" ] || fail "without git the key does not start with the repository path: $WHERE"
  # The fallback at work, with its /tmp path patched into the sandbox (everything else is the
  # real code): the key carries the path on both sides, so one directory without git alerts
  # once per hour and another one alerts on its own.
  mkdir -p "$SB/fb"; cp "$SCRIPTS/bureau-config.sh" "$SCRIPTS/bureau-env.sh" "$SB/fb/"
  python3 - "$SB/fb/bureau-config.sh" "$SB/fallback.log" <<'PY_EOF' || fail "could not patch the fallback path"
import pathlib, sys
p = pathlib.Path(sys.argv[1]); t = p.read_text(); old = '    _throttle_file="/tmp/bureau-alerts.log"\n'
if t.count(old) != 1: sys.exit(1)
p.write_text(t.replace(old, '    _throttle_file="%s"\n' % sys.argv[2]))
PY_EOF
  NOGIT2="$SB/no git 2"; mkdir -p "$NOGIT2"; cp "$A/.bureau.json" "$NOGIT2/"
  BEFORE=$(posts)
  run_in "$NOGIT" "$ALERT" "$SB/fb/bureau-config.sh"
  run_in "$NOGIT" "$ALERT" "$SB/fb/bureau-config.sh"
  [ "$(posts)" = $((BEFORE + 1)) ] || fail "without git the same alert posted twice"
  run_in "$NOGIT2" "$ALERT" "$SB/fb/bureau-config.sh"
  [ "$(posts)" = $((BEFORE + 2)) ] || fail "without git a second directory's alert was swallowed"
  grep -qF "$NOGIT|alert|$ISSUE|code-review-pipeline.sh|25	" "$SB/fallback.log" \
    && grep -qF "$NOGIT2|alert|$ISSUE|code-review-pipeline.sh|25	" "$SB/fallback.log" \
    || fail "without git the fallback log does not key by directory"
  echo "PASS BUREAU_ALERT_THROTTLE_FILE names the log (a backslash in a key is kept, an unwritable log costs nothing but the throttle); without git the key carries the repository path, so directories alert apart"
fi
if [ -f /tmp/bureau-alerts.log ] && grep -q "$ISSUE" /tmp/bureau-alerts.log; then fail "the test wrote the shared /tmp/bureau-alerts.log"; fi

# --- negative control: the v3.0.2 helpers, path patched into the sandbox ----------------------
# Verbatim but for the log path ($OLD_THROTTLE_LOG instead of /tmp/bureau-alerts.log) and the
# trim, which a test with a handful of lines never reaches.
mkdir -p "$SB/old"; OLD="$SB/old/bureau-config.sh"
cp "$SCRIPTS/bureau-config.sh" "$SCRIPTS/bureau-env.sh" "$SB/old/"
cat >> "$OLD" <<'OLD_EOF'
_throttle_should_suppress() {
  local key="$1" window_sec="${2:-3600}"
  local throttle_log="$OLD_THROTTLE_LOG"
  [ ! -f "$throttle_log" ] && return 1
  local last now delta
  last=$(awk -F'\t' -v k="$key" '$1==k{print $2}' "$throttle_log" | tail -1)
  [ -z "$last" ] && return 1
  now=$(date +%s)
  delta=$((now - last))
  [ "$delta" -lt "$window_sec" ]
}
_throttle_record() {
  local key="$1"
  local throttle_log="$OLD_THROTTLE_LOG"
  local now
  now=$(date +%s)
  printf '%s\t%s\n' "$key" "$now" >> "$throttle_log"
}
alert_telegram() {
  local issue="$1" pipeline="$2" exit_code="$3" message="$4" log_tail="${5:-}"
  if [ "${BUREAU_DRY_RUN:-0}" = "1" ]; then
    echo "[DRY_RUN] alert_telegram $issue $pipeline exit=$exit_code: $message" >&2
    return 0
  fi
  local token="${TELEGRAM_BOT_TOKEN:-}"
  local chat="${TELEGRAM_ALERT_CHAT_ID:-}"
  [ -z "$token" ] && return 0
  [ -z "$chat" ] && return 0

  local throttle_key="alert|$issue|$pipeline|$exit_code"
  _throttle_should_suppress "$throttle_key" 3600 && return 0
  _throttle_record "$throttle_key"

  local body
  body=$(printf '🚨 Bureau pipeline alert\n\nIssue: %s\nPipeline: %s\nExit: %s\n\n%s' \
    "$issue" "$pipeline" "$exit_code" "$message")
  if [ -n "$log_tail" ]; then
    body=$(printf '%s\n\nLog tail:\n```\n%s\n```' "$body" "$log_tail")
  fi
  curl -s -X POST "https://api.telegram.org/bot${token}/sendMessage" \
    --data-urlencode "chat_id=${chat}" \
    --data-urlencode "parse_mode=Markdown" \
    --data-urlencode "text=${body}" >/dev/null 2>&1 || true
}
OLD_EOF
rm -f "$SB/posts.log"
export OLD_THROTTLE_LOG="$SB/shared.log"
run_in "$A" "$ALERT" "$OLD"
run_in "$B" "$ALERT" "$OLD"
[ "$(posts)" = 1 ] || fail "negative control: the shared log no longer swallows the second repository's alert, so this proves nothing"
grep -q 'Repo:' "$SB/posts.log" && fail "negative control: the old alert names a repository, so this proves nothing"
echo "PASS negative control: with one shared log the second repository's alert is swallowed, and no alert names its repository"
