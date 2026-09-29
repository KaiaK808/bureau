#!/bin/bash
# Fake agent for the spec stage (tests/test_spec_feature_json.sh). On the
# speckit-specify call it does to .specify/feature.json (in the stage's working
# directory) what PR4_SPECIFY_ACTION says, then answers like tests/lib/fake_claude.sh,
# which also counts every call (fake_claude_counter) and logs every prompt.
#   leave      — touch nothing (the file stays as it was, or absent)
#   remove     — delete the file
#   write      — create $PR4_SPECIFY_DIR and write {"feature_directory": "$PR4_SPECIFY_DIR"}
#   mkdir      — create $PR4_SPECIFY_DIR, leave the file as it was
#   raw        — write $PR4_SPECIFY_RAW verbatim into the file
# PR4_SPECIFY_KEEP_MTIME=1 sets the file's modification time back to 2020-01-01 00:00
# after writing it (the time the test's setup gives the file), so that only its bytes
# show the write.
set -uo pipefail
case "$*" in
  *speckit-specify/SKILL.md*)
    case "${PR4_SPECIFY_ACTION:-leave}" in
      leave) ;;
      remove) rm -f .specify/feature.json ;;
      write)
        mkdir -p .specify "${PR4_SPECIFY_DIR:?}"
        printf '{"feature_directory":"%s"}\n' "$PR4_SPECIFY_DIR" > .specify/feature.json ;;
      mkdir) mkdir -p "${PR4_SPECIFY_DIR:?}" ;;
      raw) mkdir -p .specify; printf '%s' "${PR4_SPECIFY_RAW?}" > .specify/feature.json ;;
      *) echo "pr4-fake-specify: unknown PR4_SPECIFY_ACTION '$PR4_SPECIFY_ACTION'" >&2; exit 97 ;;
    esac
    if [ "${PR4_SPECIFY_KEEP_MTIME:-}" = 1 ] && [ -f .specify/feature.json ]; then
      touch -t 202001010000 .specify/feature.json
    fi ;;
esac
exec "$(dirname "$0")/fake_claude.sh" "$@"
