#!/bin/bash
# /linear-implement finds the feature's spec directory with the shared matcher
# (bureau_spec_dir_for_branch / bureau_spec_dir_candidates in bureau-config.sh), not by
# looking through `specs/*/tasks.md` for "the spec that matches the Linear issue".
# Installs the interfaces and scripts scopes for both targets into a temporary
# repository with the real installer, then, for the Claude command and the Codex skill
# alike, cuts the command line out of the rendered text and runs it there against a
# specs directory: an exact name, a tie and no match.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TMP=$(mktemp -d -t bureau-test.linimpl.XXXXXX)
trap 'rm -rf "$TMP"' EXIT
REPO="$TMP/target repo"
fail() { echo "FAIL $*" >&2; exit 1; }

git init -q "$REPO"
PYTHONDONTWRITEBYTECODE=1 python3 "$ROOT/scripts/bureau_install.py" assets --target both \
  --scope interfaces --scope scripts --apply --repo "$REPO" >/dev/null
printf '{"repo": {"specs_dir": "specs"}}\n' > "$REPO/.bureau.json"
for d in 001-foo 001-foo-bar 002-export 003-export; do
  mkdir -p "$REPO/specs/$d"; : > "$REPO/specs/$d/tasks.md"
done

for rendered in .claude/commands/linear-implement.md .agents/skills/linear-implement/SKILL.md; do
  f="$REPO/$rendered"
  [ -f "$f" ] || fail "$rendered was not installed"
  text=$(cat "$f")
  case "$text" in *'specs/*/tasks.md'*|*'for the spec that matches the Linear issue'*)
    fail "$rendered still describes the lookup in words" ;; esac
  case "$text" in *bureau_spec_dir_candidates*) : ;; *) fail "$rendered does not name bureau_spec_dir_candidates" ;; esac
  # The command line exactly as the agent reads it.
  cmd=$(printf '%s\n' "$text" | grep -o "bash -c 'source scripts/bureau-config.sh; bureau_spec_dir_for_branch \"\$1\"' _ BRANCH" | head -1)
  [ -n "$cmd" ] || fail "$rendered carries no matcher command line"
  run() {  # <function> <branch> — the rendered line with the branch filled in, run from the repository root
    local line="${cmd/bureau_spec_dir_for_branch/$1}"
    line="${line% BRANCH} $2"
    (cd "$REPO" && eval "$line")
  }
  [ "$(run bureau_spec_dir_for_branch 001-foo-bar)" = "specs/001-foo-bar/" ] || fail "$rendered: exact name gave '$(run bureau_spec_dir_for_branch 001-foo-bar)'"
  [ "$(run bureau_spec_dir_for_branch codex/exp-12-foo-bar)" = "specs/001-foo-bar/" ] || fail "$rendered: prefixed branch gave '$(run bureau_spec_dir_for_branch codex/exp-12-foo-bar)'"
  [ -z "$(run bureau_spec_dir_for_branch 004-export)" ] || fail "$rendered: a tie printed a directory"
  case "$(run bureau_spec_dir_candidates 004-export)" in
    *'`002-export`'*'`003-export`'*|*'`003-export`'*'`002-export`'*) : ;;
    *) fail "$rendered: the candidates line did not name both tied directories: '$(run bureau_spec_dir_candidates 004-export)'" ;;
  esac
  [ -z "$(run bureau_spec_dir_for_branch 009-unrelated)$(run bureau_spec_dir_candidates 009-unrelated)" ] || fail "$rendered: no match printed something"
  echo "PASS $rendered resolves the spec directory with the shared matcher"
done
echo "OK test_linear_implement_matcher"
