#!/bin/bash
# Doubles for the implement-loop tests (push_each_iteration, the squash-range check inside
# the loop, the post-implement hook's dirty-file check). Source after tests/lib/harness.sh;
# call the pr3_* functions after sandbox_init. Everything they write lives in the sandbox
# (under .pr3-bin/ and .pr3-*), so teardown removes it.
#
#   pr3_count_pushes      git on PATH stays the real git; a pass-through in front of it
#                         appends every `git push` to $SANDBOX/.pr3-pushes and, for the
#                         order against gh, to $SANDBOX/gh_calls.log (without the
#                         `-c core.hooksPath=/dev/null` Bureau's git puts first, v3.2).
#   pr3_count_receives    a post-receive hook in the bare origin appends one line per push
#                         that reached it ("<old> <new> <ref>") to $SANDBOX/.pr3-receives.
#   pr3_gh_pr_list <mode> gh answers `gh pr list` with the literal `null` (mode null), with
#                         text that is not a number and exit 0 (mode junk), or fails with
#                         nothing on stdout (mode fail); every other call, and every call in
#                         mode stub, goes to the shared tests/lib/bin/gh.
#   pr3_fake_claude       the stage's model becomes a wrapper around tests/lib/fake_claude.sh
#                         that, after the shared fake has answered, also
#                           commits PR3_MARKER_FILE with message PR3_MARKER_MSG on call
#                           PR3_MARKER_ON (a commit whose message carries a CI suppressor);
#                           renames PR3_RENAME ("<from>:<to>") with `git mv`, staged and
#                           not committed;
#                           appends a line to each path in PR3_DIRTY (colon-separated,
#                           relative to the worktree) without committing it;
#                           runs PR3_ON_CALL_<n> (a shell command, in the worktree) on
#                           call n;
#                           ends call PR3_EXIT_ON with exit code PR3_EXIT_CODE (a provider
#                           that fails, times out, runs out of quota or is interrupted);
#                           on call PR3_TERM_STAGE_ON sends SIGTERM to the implement stage
#                           itself (pr3_term_stage_script) and exits a moment later.
#   pr3_term_stage_script $SANDBOX/.pr3-term-stage, for a hook that ends its own stage.
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
  # Bureau's git function puts -c core.hooksPath=/dev/null before a remote subcommand (v3.2);
  # the shim reads the subcommand after it and logs the call without it.
  cat > "$SANDBOX/.pr3-bin/git" <<SHIM
#!/bin/bash
sub=\${1:-}; [ "\$sub" != -c ] || sub=\${3:-}
if [ "\$sub" = push ]; then
  printf 'push\n' >> "$SANDBOX/.pr3-pushes"
  args=("\$@"); [ "\${1:-}" != -c ] || args=("\${@:3}")
  { printf 'git'; for a in "\${args[@]}"; do printf '\t%s' "\$a"; done; printf '\n'; } >> "$SANDBOX/gh_calls.log"
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
    junk) echo "#7 (open)"; exit 0 ;;
    fail) echo "HTTP 502: Bad Gateway" >&2; exit 1 ;;
  esac
fi
exec "$LIB_DIR/bin/gh" "\$@"
GH
  chmod +x "$SANDBOX/.pr3-bin/gh"
}

# pr3_term_stage_script: $SANDBOX/.pr3-term-stage sends SIGTERM to the implement stage that
# runs it somewhere below. Walking up its ancestors, the first process whose command line
# is this sandbox's `bash …/scripts/implement-pipeline.sh` starts a run of such processes
# (the stage's command substitutions are forks with the same command line); the last one
# of that run is the stage. The walk stops there, so nothing above the test is touched.
pr3_term_stage_script() {
  cat > "$SANDBOX/.pr3-term-stage" <<TERM
#!/bin/bash
stage="" p=\$PPID
while [ -n "\$p" ] && [ "\$p" -gt 1 ]; do
  case "\$(ps -ww -o command= -p "\$p" 2>/dev/null)" in
    "bash $SANDBOX/scripts/implement-pipeline.sh"*) stage=\$p ;;
    *) [ -z "\$stage" ] || break ;;
  esac
  p=\$(ps -o ppid= -p "\$p" 2>/dev/null | tr -d ' ')
done
[ -z "\$stage" ] || kill -TERM "\$stage"
TERM
  chmod +x "$SANDBOX/.pr3-term-stage"
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
on_call="PR3_ON_CALL_$n"
if [ -n "${!on_call:-}" ]; then (cd "$SANDBOX" && eval "${!on_call}") >/dev/null 2>&1; fi
if [ -n "${PR3_TERM_STAGE_ON:-}" ] && [ "$PR3_TERM_STAGE_ON" = "$n" ]; then
  "$SANDBOX/.pr3-term-stage"
  sleep 2
fi
if [ -n "${PR3_EXIT_ON:-}" ] && [ "$PR3_EXIT_ON" = "$n" ]; then exit "${PR3_EXIT_CODE:?PR3_EXIT_CODE must be set}"; fi
exit "$rc"
CLAUDE
  chmod +x "$SANDBOX/.pr3-bin-claude"
  pr3_term_stage_script
  export PR3_SHARED_FAKE_CLAUDE="$LIB_DIR/fake_claude.sh"
  export FAKE_CLAUDE_BIN="$SANDBOX/.pr3-bin-claude"
}
