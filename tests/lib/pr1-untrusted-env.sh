#!/bin/bash
# Doubles for tests/test_untrusted_env.sh (code the branch controls runs without the
# Bureau secrets). Source after tests/lib/harness.sh; call pr1_setup after sandbox_init.
#
#   pr1_setup                  — writes the sandbox .env with the Linear and Telegram
#                                probes (the stage exports them, as under the queue),
#                                exports the four GitHub token probes, an empty exported
#                                API_KEY (the stage copies the Linear key into it), a
#                                remote URL with a token inside and an operator variable; puts a recording gh in front of the
#                                stub gh and appends recording Linear doubles to the
#                                sandbox bureau-config.sh.
#   pr1_dump <name>            — a shell command that appends one "--- run" block with
#                                its environment to $PR1_MARKS/<name>.env and a line to
#                                the sequence log (absolute paths: it must work in the
#                                "clean" environment too).
#   pr1_check_env <file> <label> <default|clean> [runs] [NAME=VALUE ...]
#                              — no probe value and no secret name in any run, PATH and
#                                HOME kept, the operator variable kept (default) or gone
#                                (clean), each NAME=VALUE present.
#   pr1_check_bureau_calls <label> [gh|linear ...]
#                              — every recorded gh call saw GH_TOKEN and every recorded
#                                Linear call saw LINEAR_API_KEY and API_KEY, and each
#                                named kind was recorded AFTER the last untrusted run.
#   pr1_passthrough <scripts dir> — negative control: bureau_untrusted_env becomes the
#                                v3.0.2 behaviour (the command inherits everything).
# Failures go through pr1_fail, which counts them in PR1_FAILS.

PR1_LINEAR=lin_api_PROBE_linear_0001
PR1_TG_TOKEN=PROBE_telegram_bot_0002
PR1_TG_CHAT=-100PROBE0003
PR1_GH=ghp_PROBE_gh_token_0004
PR1_GITHUB=ghs_PROBE_github_token_0005
PR1_GHE=PROBE_gh_enterprise_0006
PR1_GITHUBE=PROBE_github_enterprise_0007
PR1_OPERATOR=keep_me_operator_var_0008
PR1_SECRET_NAMES="LINEAR_API_KEY TELEGRAM_BOT_TOKEN TELEGRAM_ALERT_CHAT_ID GH_TOKEN GITHUB_TOKEN GH_ENTERPRISE_TOKEN GITHUB_ENTERPRISE_TOKEN API_KEY REMOTE_URL"
PR1_FAILS=${PR1_FAILS:-0}

pr1_fail() { echo "FAIL $*" >&2; PR1_FAILS=$((PR1_FAILS + 1)); }
# pr1_pass <message>: PASS when the section since the last pr1_pass added no failure.
PR1_SECTION_FAILS=0
pr1_pass() {
  if [ "$PR1_FAILS" = "$PR1_SECTION_FAILS" ]; then echo "PASS $*"; else echo "FAILED $*" >&2; fi
  PR1_SECTION_FAILS=$PR1_FAILS
}

pr1_setup() {
  PR1_MARKS="$SANDBOX/.pr1"
  mkdir -p "$PR1_MARKS/bin"
  printf '.pr1/\n' >> "$SANDBOX/.git/info/exclude"
  cat > "$SANDBOX/.env" <<EOF
LINEAR_API_KEY=$PR1_LINEAR
TELEGRAM_BOT_TOKEN=$PR1_TG_TOKEN
TELEGRAM_ALERT_CHAT_ID=$PR1_TG_CHAT
EOF
  export GH_TOKEN="$PR1_GH" GITHUB_TOKEN="$PR1_GITHUB" \
    GH_ENTERPRISE_TOKEN="$PR1_GHE" GITHUB_ENTERPRISE_TOKEN="$PR1_GITHUBE" \
    OPERATOR_TOOL_VAR="$PR1_OPERATOR" API_KEY="" \
    REMOTE_URL="https://x-access-token:$PR1_GITHUB@github.com/owner/repo.git"
  # gh: record the token it was started with, then answer as the harness stub.
  cat > "$PR1_MARKS/bin/gh" <<EOF
#!/bin/bash
case "\${GH_TOKEN:-}" in
  '$PR1_GH') printf 'gh ok %s\n' "\$*" >> '$PR1_MARKS/seq.log' ;;
  '') printf 'gh missing %s\n' "\$*" >> '$PR1_MARKS/seq.log' ;;
  *) printf 'gh other %s\n' "\$*" >> '$PR1_MARKS/seq.log' ;;
esac
exec '$LIB_DIR/bin/gh' "\$@"
EOF
  chmod +x "$PR1_MARKS/bin/gh"
  export PATH="$PR1_MARKS/bin:$PATH"
  # Linear: the stage's own calls run in the stage shell, where the real helpers read
  # API_KEY / LINEAR_API_KEY. Record what they would send; answer as the stub does.
  cat >> "$SCRIPTS_DIR/bureau-config.sh" <<EOF

# ── pr1 doubles (tests/lib/pr1-untrusted-env.sh) ──
pr1_linear_seen() {
  local state=ok
  [ "\${LINEAR_API_KEY:-}" = '$PR1_LINEAR' ] && [ "\${API_KEY:-\$LINEAR_API_KEY}" = '$PR1_LINEAR' ] || state=missing
  printf 'linear %s %s\n' "\$state" "\$1" >> '$PR1_MARKS/seq.log'
}
post_comment() { pr1_linear_seen post_comment; _record "post_comment" "\$1" "\$2"; return 0; }
move_issue() { pr1_linear_seen move_issue; _record "move_issue" "\$1" "\$2"; return 0; }
add_issue_label() { pr1_linear_seen add_issue_label; _record "add_issue_label" "\$1" "\$2"; return "\${BUREAU_STUB_ADD_LABEL_RC:-0}"; }
# QA's executor commit (the real one lives in bureau-config.sh, which the stub replaces).
commit_stage_changes() { _record "commit_stage_changes" "\$@"; return 0; }
EOF
}

pr1_dump() {
  printf "{ echo '--- run'; env; } >> '%s/%s.env'; echo 'untrusted %s' >> '%s/seq.log'" "$PR1_MARKS" "$1" "$1" "$PR1_MARKS"
}

pr1_check_env() {
  local file="$1" label="$2" mode="$3" runs="${4:-1}" value name runs_seen pair
  shift 4 2>/dev/null || shift $#
  if [ ! -f "$file" ]; then pr1_fail "$label: the command never ran (no $file)"; return; fi
  runs_seen=$(grep -c '^--- run$' "$file" || true)
  [ "$runs_seen" = "$runs" ] || pr1_fail "$label: expected $runs run(s), saw $runs_seen"
  for value in "$PR1_LINEAR" "$PR1_TG_TOKEN" "$PR1_TG_CHAT" "$PR1_GH" "$PR1_GITHUB" "$PR1_GHE" "$PR1_GITHUBE"; do
    if grep -qF -- "$value" "$file"; then
      pr1_fail "$label: a Bureau secret reached the command, as $(grep -F -- "$value" "$file" | cut -d= -f1 | sort -u | tr '\n' ' ')"
    fi
  done
  for name in $PR1_SECRET_NAMES; do
    if grep -q "^$name=." "$file"; then pr1_fail "$label: $name is set for the command"; fi
  done
  [ "$(grep -c '^PATH=' "$file" || true)" = "$runs" ] || pr1_fail "$label: PATH did not reach every run"
  [ "$(grep -c '^HOME=' "$file" || true)" = "$runs" ] || pr1_fail "$label: HOME did not reach every run"
  if [ "$mode" = clean ]; then
    if grep -q '^OPERATOR_TOOL_VAR=' "$file"; then pr1_fail "$label: clean kept an operator variable"; fi
  else
    [ "$(grep -cx "OPERATOR_TOOL_VAR=$PR1_OPERATOR" "$file" || true)" = "$runs" ] \
      || pr1_fail "$label: the default mode dropped an operator variable"
  fi
  for pair in "$@"; do
    grep -qxF -- "$pair" "$file" || pr1_fail "$label: $pair missing"
  done
}

pr1_check_bureau_calls() {
  local label="$1" kind last tail_lines
  shift
  if [ ! -s "$PR1_MARKS/seq.log" ]; then pr1_fail "$label: nothing recorded"; return; fi
  if grep -Eq '^(gh|linear) (missing|other) ' "$PR1_MARKS/seq.log"; then
    pr1_fail "$label: a Bureau call ran without its key: $(grep -E '^(gh|linear) (missing|other) ' "$PR1_MARKS/seq.log" | head -3 | tr '\n' ';')"
  fi
  last=$(grep -n '^untrusted ' "$PR1_MARKS/seq.log" | tail -1 | cut -d: -f1)
  [ -n "$last" ] || { pr1_fail "$label: no untrusted run recorded"; return; }
  tail_lines=$(sed -n "$((last + 1)),\$p" "$PR1_MARKS/seq.log")
  for kind in "$@"; do
    printf '%s\n' "$tail_lines" | grep -q "^$kind ok " \
      || pr1_fail "$label: no $kind call with its key after the last untrusted run"
  done
}

pr1_passthrough() {
  cat >> "$1/bureau-config.sh" <<'EOF'
# ── pr1 negative control: the v3.0.2 behaviour, the command inherits everything ──
bureau_untrusted_env() { [ "${1:-}" = --check ] && return 0; env "$@"; }
EOF
}
