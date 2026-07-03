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

# Resolve the fixture for this call.
IFS=':' read -ra fixtures <<< "${FAKE_CLAUDE_FIXTURES:?must list at least one fixture}"
idx=$((n - 1))
[ "$idx" -ge "${#fixtures[@]}" ] && idx=$((${#fixtures[@]} - 1))
fixture="${fixtures[$idx]}"

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
      git -C "$SANDBOX" commit -q -m "fake-claude iter $n progress" >/dev/null 2>&1 || true
      break
    fi
  done
fi

cat "$fixture"

if [ -n "${FAKE_CLAUDE_SLEEP:-}" ]; then
  sleep "$FAKE_CLAUDE_SLEEP"
fi
