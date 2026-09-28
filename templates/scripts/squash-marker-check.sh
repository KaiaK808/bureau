#!/bin/bash
# squash-marker-check.sh — read the message of every commit in the squash range
# and report each one that carries an entry of scripts/ci-skip-markers.txt.
#
# Carried over from installation A (EXP-1465). A squash merge without an
# explicit body writes the full message of every branch commit — subject and
# body — into the merge commit on main, and GitHub reads a CI suppressor there
# again: the run on main stays away. The stages no longer append one, and
# merge-pipeline.sh sets its own sanitised body (merge-body.sh); what remains
# is a task title or a body the agent wrote itself, a rebase merge, and a merge
# done by hand in the GitHub UI. This reads the commits that were actually made.
#
# The list sits next to this script and is found via ${BASH_SOURCE[0]} — not
# via the working directory and not via $0 — so a stage running in a worktree
# reads the list of the checkout this script came from. Its second reader is
# tests/test_squash_marker.sh. Format: one entry per line and nothing else;
# empty lines are skipped. This script carries no list of its own.
#
# The range is <base>..HEAD, base defaulting to origin/main: every commit
# reachable from HEAD and not from main — what the squash carries, the same
# list GitHub shows on the PR. Not origin/<branch>..HEAD: that misses every
# commit the agent already pushed and reports commits that came in from main
# through a merge. Not the three-dot form: for a branch behind main it would
# take main's own commits along.
#
# Read only: rev-parse, rev-list, cat-file. No file, no ref, no comment is
# written, which also makes the script harmless in a dry run.
#
# Exit codes: 0 clean (also for an empty range), 3 at least one finding,
# 2 not checked (list missing, unreadable or empty; base not resolvable; a
# read of the range or of a commit failed). Findings are 3 and not 1 because
# bash itself exits 1 when it trips — an unset variable under set -u — and the
# caller reads every code other than 0 and 3 as "not checked", so a crash can
# pass neither for a finding list nor for clean.
#
# Usage: bash scripts/squash-marker-check.sh [<base>]
set -uo pipefail

LIST="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/ci-skip-markers.txt"
BASE="${1:-origin/main}"
RANGE="$BASE..HEAD"
NL=$'\n'

not_checked() {
  echo "squash-marker-check: NOT CHECKED — $1"
  exit 2
}

# The list. The number of entries is checked before the array is read: in
# bash 3.2 an empty array under set -u is an unbound variable and a crash.
entries=()
n_entries=0
[ -f "$LIST" ] && [ -r "$LIST" ] || not_checked "the list $LIST is missing or unreadable"
while IFS= read -r entry || [ -n "$entry" ]; do
  [ -n "$entry" ] || continue
  entries[$n_entries]="$entry"
  n_entries=$((n_entries + 1))
done < "$LIST"
[ "$n_entries" -gt 0 ] || not_checked "the list $LIST has no entry"

git rev-parse --verify --quiet "$BASE^{commit}" >/dev/null \
  || not_checked "base '$BASE' does not resolve to a commit"

# One call for the whole range, oldest first. Its code is read in the if form;
# a failed listing is "not checked", never an empty range.
if commits=$(git rev-list --reverse "$RANGE"); then
  :
else
  not_checked "listing $RANGE failed (exit code $?)"
fi

count=0
n_findings=0
report=""
for sha in $commits; do
  count=$((count + 1))
  if raw=$(git cat-file commit "$sha"); then
    :
  else
    not_checked "reading commit $sha failed (exit code $?)"
  fi

  # The raw object: header lines, one truly empty line, the message. Header
  # lines of signed commits continue over lines that start with a space, so the
  # first empty line is a reliable separator. A commit without a message
  # has no separator left once $(…) stripped the trailing newlines.
  case "$raw" in
    *"$NL$NL"*) msg="${raw#*"$NL$NL"}" ;;
    *) msg="" ;;
  esac
  subject="${msg%%"$NL"*}"
  case "$msg" in
    *"$NL"*) body="${msg#*"$NL"}" ;;
    *) body="" ;;
  esac

  # Verbatim match, as GitHub reads it: the entry stays in quotes. Unquoted it
  # would turn into a bracket expression and hit nearly every message.
  # Several hits of one entry in one place are one finding.
  short=""
  i=0
  while [ "$i" -lt "$n_entries" ]; do
    entry="${entries[$i]}"
    for place in subject body; do
      if [ "$place" = "subject" ]; then text="$subject"; else text="$body"; fi
      case "$text" in
        *"$entry"*)
          [ -n "$short" ] || short=$(git rev-parse --short "$sha" 2>/dev/null) || short="$sha"
          report+="  $short  $place  $entry  $subject$NL"
          n_findings=$((n_findings + 1))
          ;;
      esac
    done
    i=$((i + 1))
  done
done

if [ "$n_findings" -eq 0 ]; then
  echo "squash-marker-check: $RANGE, $count commit(s) read, none carries an entry of scripts/ci-skip-markers.txt"
  exit 0
fi

echo "squash-marker-check: $n_findings finding(s) in $RANGE ($count commit(s) read) — a CI suppressor from scripts/ci-skip-markers.txt:"
printf '%s' "$report"
echo "Reword these messages before the merge: the squash carries every one of them into the body of the merge commit on main, and the run there stays away. This check rewrites nothing."
exit 3
