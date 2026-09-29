#!/bin/bash
# Doubles for the implement-loop tests (push_each_iteration, the squash-range check inside
# the loop, the post-implement hook's dirty-file check). Source after tests/lib/harness.sh;
# call the pr3_* functions after sandbox_init. Everything they write lives in the sandbox
# (under .pr3-bin/ and .pr3-*), so teardown removes it.
#
#   pr3_count_pushes      git on PATH stays the real git; a pass-through in front of it
#                         appends every `git push` to $SANDBOX/.pr3-pushes and, for the
#                         order against gh, to $SANDBOX/gh_calls.log.
#   pr3_count_receives    a post-receive hook in the bare origin appends one line per push
#                         that reached it ("<old> <new> <ref>") to $SANDBOX/.pr3-receives.
#   pr3_gh_pr_list <mode> gh answers `gh pr list` with the literal `null` (mode null) or
#                         fails with nothing on stdout (mode fail); every other call, and
#                         every call in mode stub, goes to the shared tests/lib/bin/gh.
#   pr3_fake_claude       the stage's model becomes a wrapper around tests/lib/fake_claude.sh
#                         that, after the shared fake has answered, also
#                           commits PR3_MARKER_FILE with message PR3_MARKER_MSG on call
#                           PR3_MARKER_ON (a commit whose message carries a CI suppressor);
#                           renames PR3_RENAME ("<from>:<to>") with `git mv`, staged and
#                           not committed;
#                           appends a line to each path in PR3_DIRTY (colon-separated,
#                           relative to the worktree) without committing it.
#   pr3_pushes / pr3_receives   the counts so far (0 when nothing was recorded).
#   pr3_run_implement     run the stage with these doubles (the gh and git ones only work
#                         through it).
#   pr3_ignore_harness_files   list the harness's own files in the sandbox's
#                         .git/info/exclude (the bare origin, the call logs, the fake
#                         model's counter, stderr.log, these doubles), so that `git status`
#                         in the stage shows only what the agent and the hook left: a
#                         write into the bare origin or a log while the hook runs is not
#                         the hook's doing.

pr3_bin() { mkdir -p "$SANDBOX/.pr3-bin"; }

# pr3_run_implement <args>: run_implement_pipeline with the git and gh doubles first on PATH
# for that run only (a PATH left pointing into a removed sandbox would break the next
# case's git calls through bash's command hash).
pr3_run_implement() { PATH="$SANDBOX/.pr3-bin:$PATH" run_implement_pipeline "$@"; }

pr3_ignore_harness_files() {
  printf '%s\n' '/.fake-origin.git/' '/calls.log' '/gh_calls.log' '/stderr.log' \
    '/fake_claude_counter' '/.pr3-*' >> "$SANDBOX/.git/info/exclude"
}

pr3_count_pushes() {
  local real_git
  real_git=$(command -v git)
  pr3_bin
  cat > "$SANDBOX/.pr3-bin/git" <<SHIM
#!/bin/bash
if [ "\${1:-}" = push ]; then
  printf 'push\n' >> "$SANDBOX/.pr3-pushes"
  { printf 'git'; for a in "\$@"; do printf '\t%s' "\$a"; done; printf '\n'; } >> "$SANDBOX/gh_calls.log"
fi
exec "$real_git" "\$@"
SHIM
  chmod +x "$SANDBOX/.pr3-bin/git"
}

pr3_count_receives() {
  local hook="$SANDBOX/.fake-origin.git/hooks/post-receive"
  cat > "$hook" <<HOOK
#!/bin/sh
while read -r old new ref; do echo "\$old \$new \$ref" >> "$SANDBOX/.pr3-receives"; done
HOOK
  chmod +x "$hook"
}

pr3_pushes() { if [ -f "$SANDBOX/.pr3-pushes" ]; then wc -l < "$SANDBOX/.pr3-pushes" | tr -d ' '; else echo 0; fi; }
pr3_receives() { if [ -f "$SANDBOX/.pr3-receives" ]; then wc -l < "$SANDBOX/.pr3-receives" | tr -d ' '; else echo 0; fi; }

pr3_gh_pr_list() {
  local mode="$1"
  pr3_bin
  cat > "$SANDBOX/.pr3-bin/gh" <<GH
#!/bin/bash
if [ "\${1:-}" = pr ] && [ "\${2:-}" = list ] && [ "$mode" != stub ]; then
  { printf 'gh'; for a in "\$@"; do printf '\t%s' "\$a"; done; printf '\n'; } >> "$SANDBOX/gh_calls.log"
  case "$mode" in
    null) echo null; exit 0 ;;
    fail) echo "HTTP 502: Bad Gateway" >&2; exit 1 ;;
  esac
fi
exec "$LIB_DIR/bin/gh" "\$@"
GH
  chmod +x "$SANDBOX/.pr3-bin/gh"
}

pr3_fake_claude() {
  cat > "$SANDBOX/.pr3-bin-claude" <<'CLAUDE'
#!/bin/bash
set -uo pipefail
"$PR3_SHARED_FAKE_CLAUDE" "$@"
rc=$?
n=$(cat "$SANDBOX/fake_claude_counter" 2>/dev/null || echo 0)
if [ -n "${PR3_MARKER_ON:-}" ] && [ "$PR3_MARKER_ON" = "$n" ]; then
  f="${PR3_MARKER_FILE:-pr3-marker.txt}"
  date > "$SANDBOX/$f"
  git -C "$SANDBOX" add -- "$f" >/dev/null 2>&1
  git -C "$SANDBOX" commit -q -m "${PR3_MARKER_MSG:?PR3_MARKER_MSG must be set}" >/dev/null 2>&1
fi
if [ -n "${PR3_RENAME:-}" ]; then
  git -C "$SANDBOX" mv -- "${PR3_RENAME%%:*}" "${PR3_RENAME#*:}" >/dev/null 2>&1
fi
if [ -n "${PR3_DIRTY:-}" ]; then
  IFS=':' read -ra dirty <<< "$PR3_DIRTY"
  for p in "${dirty[@]}"; do echo "left by the agent in call $n" >> "$SANDBOX/$p"; done
fi
exit "$rc"
CLAUDE
  chmod +x "$SANDBOX/.pr3-bin-claude"
  export PR3_SHARED_FAKE_CLAUDE="$LIB_DIR/fake_claude.sh"
  export FAKE_CLAUDE_BIN="$SANDBOX/.pr3-bin-claude"
}
