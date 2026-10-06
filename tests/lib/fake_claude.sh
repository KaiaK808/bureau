#!/bin/bash
# Fake $CLAUDE binary. The implement pipeline invokes claude_cmd_for_stage to
# get a command line and then calls it with a prompt argument. The stub
# config returns the path to this script as the resolved command, so the
# pipeline ends up running: fake_claude.sh <prompt-text>.
#
# Behaviour driven by env vars set in the test:
#   FAKE_CLAUDE_FIXTURES   — colon-separated list of fixture file paths,
#                            one per Claude call. Beyond the last entry the
#                            stub repeats the final fixture.
#   FAKE_CLAUDE_COMMIT_ON_ITERS — colon-separated list of iter numbers (1-based)
#                            where the stub should make a real git commit
#                            before printing the fixture. Used to exercise the
#                            COMMITS_THIS_ITER signal in the stuck detector.
#   FAKE_CLAUDE_SLEEP      — seconds to sleep AFTER printing output but before
#                            exit. With `timeout` wrapping us, only matters if
#                            the iter timeout is shorter than the sleep.
#   FAKE_CLAUDE_LOG        — file to append "iter N invoked" lines to.
#   FAKE_CLAUDE_COMMIT_MSG — message for those commits (default "fake-claude iter N progress").
#   FAKE_CLAUDE_CHECK_TASKS_ON_ITERS — colon-separated iter numbers on which the stub marks
#                            every open task in the sandbox's specs/*/tasks.md done
#                            ("- [ ]" → "- [X]") and stages the file, so a commit on the
#                            same iter carries the marks.
#   FAKE_CLAUDE_TIMEOUT_ON_ITERS — colon-separated iter numbers that end like a provider
#                            pass that hit its time limit: after the commit (if any), no
#                            output and exit 124, as bureau-provider.py returns a timeout.
#                            FAKE_CLAUDE_TIMEOUT_SLEEP seconds pass before that exit.
#   FAKE_CLAUDE_PROMPT_DIR — directory that receives each call's arguments (the prompt)
#                            as prompt-N.txt.
#
# The pipeline's $CLAUDE is unquoted on call, so this script receives the
# prompt as its arguments. We ignore them — the prompt is irrelevant to the
# test; only the response shape matters.
set -uo pipefail

counter_file="${SANDBOX:?SANDBOX must be set}/fake_claude_counter"
n=$(cat "$counter_file" 2>/dev/null || echo 0)
n=$((n + 1))
echo "$n" > "$counter_file"

[ -n "${FAKE_CLAUDE_LOG:-}" ] && echo "iter $n invoked" >> "$FAKE_CLAUDE_LOG"
# FAKE_CLAUDE_PROMPT_LOG — file to append every prompt to, so a test can check
# what a stage told the agent (for example which spec directory).
[ -n "${FAKE_CLAUDE_PROMPT_LOG:-}" ] && printf '%s\n' "$*" >> "$FAKE_CLAUDE_PROMPT_LOG"
[ -z "${FAKE_CLAUDE_PROMPT_DIR:-}" ] || printf '%s\n' "$*" > "$FAKE_CLAUDE_PROMPT_DIR/prompt-$n.txt"

# in_iters <n> <colon-separated list>: whether call n is on the list.
in_iters() {
  local item items
  IFS=':' read -ra items <<< "$2"
  for item in "${items[@]}"; do [ "$item" = "$1" ] && return 0; done
  return 1
}

# Resolve the fixture for this call.
IFS=':' read -ra fixtures <<< "${FAKE_CLAUDE_FIXTURES:?must list at least one fixture}"
idx=$((n - 1))
[ "$idx" -ge "${#fixtures[@]}" ] && idx=$((${#fixtures[@]} - 1))
fixture="${fixtures[$idx]}"
# Role fixtures for the review stage, chosen by the prompt instead of the call order (the
# three specialists run in parallel): FAKE_CLAUDE_SECURITY_FIXTURE answers the security
# specialist, FAKE_CLAUDE_MERGE_FIXTURE the merger.
case "$*" in
  *"You are a SECURITY specialist"*)             [ -z "${FAKE_CLAUDE_SECURITY_FIXTURE:-}" ] || fixture="$FAKE_CLAUDE_SECURITY_FIXTURE" ;;
  *"Merge these three specialist reviews"*)      [ -z "${FAKE_CLAUDE_MERGE_FIXTURE:-}" ] || fixture="$FAKE_CLAUDE_MERGE_FIXTURE" ;;
esac

if [ -n "${FAKE_CLAUDE_CHECK_TASKS_ON_ITERS:-}" ] && in_iters "$n" "$FAKE_CLAUDE_CHECK_TASKS_ON_ITERS"; then
  for tasks_file in "$SANDBOX"/specs/*/tasks.md; do
    [ -f "$tasks_file" ] || continue
    sed 's/^- \[ \]/- [X]/' "$tasks_file" > "$tasks_file.tmp" && mv "$tasks_file.tmp" "$tasks_file"
    git -C "$SANDBOX" add -- "${tasks_file#"$SANDBOX"/}" >/dev/null 2>&1 || true
  done
fi

# Optionally make a git commit before emitting output. The pipeline's stuck
# detector uses commit count between HEAD_BEFORE and HEAD_AFTER as a signal;
# fixtures that claim "PARTIAL with progress" need a real commit to be
# believable.
if [ -n "${FAKE_CLAUDE_COMMIT_ON_ITERS:-}" ]; then
  IFS=':' read -ra commit_iters <<< "$FAKE_CLAUDE_COMMIT_ON_ITERS"
  for ci in "${commit_iters[@]}"; do
    if [ "$ci" = "$n" ]; then
      progress_file="$SANDBOX/iter_${n}_progress.txt"
      date > "$progress_file"
      git -C "$SANDBOX" add "$(basename "$progress_file")" >/dev/null 2>&1 || true
      git -C "$SANDBOX" commit -q -m "${FAKE_CLAUDE_COMMIT_MSG:-fake-claude iter $n progress}" >/dev/null 2>&1 || true
      break
    fi
  done
fi

if [ -n "${FAKE_CLAUDE_TIMEOUT_ON_ITERS:-}" ] && in_iters "$n" "$FAKE_CLAUDE_TIMEOUT_ON_ITERS"; then
  [ -z "${FAKE_CLAUDE_TIMEOUT_SLEEP:-}" ] || sleep "$FAKE_CLAUDE_TIMEOUT_SLEEP"
  exit 124
fi

cat "$fixture"

if [ -n "${FAKE_CLAUDE_SLEEP:-}" ]; then
  sleep "$FAKE_CLAUDE_SLEEP"
fi
