#!/bin/bash
# repo.worktree_links: after reset_worktree's `clean -fdx`, the listed paths (a Python
# `.venv`) come back into the stage worktree as symlinks to the main checkout — and only
# where a link cannot hurt. Every guard has its own case, and each case checks the effect
# (what is on disk, what git sees), not only the warning text.
#
# Runs the REAL bureau_link_worktree_paths, cut from templates/scripts/bureau-config.sh,
# against a real main checkout (with a space in its path) and a linked worktree cleaned the
# way reset_worktree does it. The end-to-end run through reset_worktree itself is in
# shared_runtime_test.py.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")" && cd .. && pwd)"
SCRIPTS="$REPO_ROOT/templates/scripts"
TMP=$(mktemp -d -t bureau-test.links.XXXXXXXX)
TMP=$(cd "$TMP" && pwd -P)
trap 'rm -rf "$TMP"' EXIT

sed -n '/^# ── Worktree links (repo.worktree_links)/,/^# ── End of worktree links/p' "$SCRIPTS/bureau-config.sh" > "$TMP/fn.sh"
grep -q '^bureau_link_worktree_paths() {' "$TMP/fn.sh" || { echo "FAIL bureau_link_worktree_paths not found in bureau-config.sh"; exit 1; }
# shellcheck source=/dev/null
source "$TMP/fn.sh"

MAIN="$TMP/main checkout"; WT="$TMP/stage worktree"; OUTSIDE="$TMP/outside"
export BUREAU_CONFIG="$TMP/bureau config.json"

fail() { echo "FAIL $*" >&2; exit 1; }
check() { [ "$2" = "$3" ] || fail "$1: got [$2], wanted [$3]"; }
kind() {  # link:<target> | dir | file | none
  if [ -L "$1" ]; then echo "link:$(readlink "$1")"
  elif [ -d "$1" ]; then echo dir
  elif [ -e "$1" ]; then echo file
  else echo none; fi
}
links() { printf '{"repo":{"worktree_links":%s}}\n' "$1" > "$BUREAU_CONFIG"; }
dirty() { git -C "$WT" status --porcelain --untracked-files=all | tr '\n' ' '; }
run() { OUT=$(bureau_link_worktree_paths "$WT"); }
warned() { case "$OUT" in *"$1"*) : ;; *) fail "$2: expected a warning containing [$1], got [$OUT]" ;; esac; }

# setup <gitignore line>… — main checkout with a venv, a branch, and a fresh worktree on it.
setup() {
  rm -rf "$MAIN" "$WT" "$OUTSIDE"
  mkdir -p "$MAIN" "$OUTSIDE"
  git -C "$MAIN" init -q -b main
  git -C "$MAIN" config user.email t@t; git -C "$MAIN" config user.name t
  : > "$MAIN/.gitignore"
  local line
  for line in "$@"; do printf '%s\n' "$line" >> "$MAIN/.gitignore"; done
  mkdir -p "$MAIN/tools"; printf 'tool\n' > "$MAIN/tools/readme.txt"
  git -C "$MAIN" add -A >/dev/null; git -C "$MAIN" commit -q -m init
  mkdir -p "$MAIN/.venv/bin" "$MAIN/my env/bin" "$MAIN/tools/.venv/bin"
  printf '#!/bin/sh\necho venv-python\n' > "$MAIN/.venv/bin/python"; chmod +x "$MAIN/.venv/bin/python"
  printf 'x\n' > "$MAIN/my env/bin/python"; printf 'x\n' > "$MAIN/tools/.venv/bin/python"
  git -C "$MAIN" branch feat
  git -C "$MAIN" worktree add -q "$WT" feat
  git -C "$WT" clean -fdx --quiet
  unset OUT
}
# commit_in_branch <message> — commit what is staged in the worktree's branch.
commit_in_branch() { git -C "$WT" commit -q -m "$1"; }

# 0 · negative control: nothing configured → nothing happens, silently (the v3.0.1 behaviour).
setup .venv
printf '{"repo":{}}\n' > "$BUREAU_CONFIG"; run
check "default: no link" "$(kind "$WT/.venv")" none
check "default: silent" "$OUT" ""

# 1 · a listed, ignored venv is linked; the worktree stays clean; the venv works through it.
setup .venv
links '[".venv"]'; run
check "linked" "$(kind "$WT/.venv")" "link:$MAIN/.venv"
check "worktree clean" "$(dirty)" ""
check "venv usable" "$("$WT/.venv/bin/python")" venv-python
echo "PASS a listed venv is linked from the main checkout and the worktree stays clean"

# 2 · the next reset's clean -fdx removes the link only, never the target.
git -C "$WT" clean -fdx --quiet
check "link gone after clean" "$(kind "$WT/.venv")" none
check "target survives clean" "$(kind "$MAIN/.venv/bin/python")" file
run
check "relinked after the next reset" "$(kind "$WT/.venv")" "link:$MAIN/.venv"
echo "PASS clean -fdx removes the link, not the main checkout's venv"

# 3 · an older link is replaced; a real directory in the way is never touched.
ln -sfn "$OUTSIDE" "$WT/.venv"; run
check "old link replaced" "$(kind "$WT/.venv")" "link:$MAIN/.venv"
check "nothing written into the old link's target" "$(ls -A "$OUTSIDE")" ""
rm "$WT/.venv"; mkdir -p "$WT/.venv"; printf 'keep\n' > "$WT/.venv/own"; run
warned "real file or directory is in the way" "real dir"
check "real dir kept" "$(kind "$WT/.venv")" dir
check "real dir content kept" "$(cat "$WT/.venv/own")" keep
check "no link inside the real dir" "$(ls -A "$WT/.venv")" own
echo "PASS an older link is replaced, a real directory is left alone"

# 4 · a path the branch tracks belongs to the PR: skipped, untouched.
setup .venv
mkdir -p "$WT/.venv"; printf 'home = pr\n' > "$WT/.venv/pyvenv.cfg"
git -C "$WT" add -f .venv/pyvenv.cfg; commit_in_branch "track a venv file"
links '[".venv"]'; run
warned "the branch tracks that path" "tracked"
check "tracked dir untouched" "$(cat "$WT/.venv/pyvenv.cfg")" "home = pr"
check "tracked: no link" "$(kind "$WT/.venv")" dir
echo "PASS a path the branch tracks is skipped"

# 5 · literal path: glob characters in an entry are not a pattern for the tracked check.
setup '\[ab\]'
mkdir -p "$MAIN/[ab]"; printf 'x\n' > "$MAIN/[ab]/f"
printf 'tracked a\n' > "$WT/a"; git -C "$WT" add a; commit_in_branch "track a"
links '["[ab]"]'; run
check "glob entry linked, not mistaken for tracked 'a'" "$(kind "$WT/[ab]")" "link:$MAIN/[ab]"
echo "PASS an entry is a literal path, not a pathspec"

# 6 · not ignored as a symlink: a trailing-slash pattern, and no pattern at all.
setup '.venv/'
links '[".venv"]'; run
warned "not ignored as a symlink" "trailing slash"
check "trailing-slash pattern: no link" "$(kind "$WT/.venv")" none
check "trailing-slash pattern: worktree clean" "$(dirty)" ""
setup
links '[".venv"]'; run
warned "not ignored as a symlink" "no pattern"
check "no pattern: no link" "$(kind "$WT/.venv")" none
check "no pattern: worktree clean" "$(dirty)" ""
echo "PASS a path not ignored as a symlink is skipped and the worktree stays clean"

# 7 · missing in the main checkout.
setup .venv venv
links '["venv"]'; run
warned "does not exist in the main checkout" "missing"
check "missing: no dangling link" "$(kind "$WT/venv")" none
echo "PASS a path missing in the main checkout is skipped"

# 8 · entries that are not plain relative paths. Later guards would stop most of them too
# (git refuses paths outside the repository), so each case names the guard that must fire.
setup .venv
ABS="$MAIN/.venv"
for pair in "$ABS|not a relative path" "/etc|not a relative path" "|not a relative path" \
            "../outside|components are not allowed" "tools/../.venv|components are not allowed" \
            "./.venv|components are not allowed" "tools//.venv|components are not allowed" \
            ".git|components are not allowed" ".git/hooks|components are not allowed" \
            ".GIT|components are not allowed" "tools/.Git|components are not allowed"; do
  entry="${pair%%|*}"; reason="${pair#*|}"
  links "[$(printf '%s' "$entry" | jq -Rs .)]"; run
  warned "$reason" "entry [$entry]"
done
check "absolute: nothing at /etc in the worktree" "$(kind "$WT/etc")" none
check "dotdot: nothing next to the worktree" "$(kind "$TMP/outside/outside")" none
check "no .git link" "$(kind "$WT/.git")" file
check "nothing linked by any refused entry" "$(find "$WT" -type l | wc -l | tr -d ' ')" 0
echo "PASS absolute paths, '.', '..', '.git' and empty components are refused"

# 9 · the parent must exist inside the worktree; a tracked symlink as parent is refused.
setup .venv
links '["nowhere/.venv"]'; run
warned "'nowhere' does not exist in the worktree" "missing parent"
check "missing parent not created" "$(kind "$WT/nowhere")" none
git -C "$WT" rm -rq tools; ln -s "$OUTSIDE" "$WT/tools"; git -C "$WT" add tools; commit_in_branch "tools becomes a symlink"
links '["tools/.venv"]'; run
warned "leads outside the worktree" "symlinked parent"
check "nothing written outside the worktree" "$(ls -A "$OUTSIDE")" ""
echo "PASS a parent that is missing or leads outside the worktree is refused"

# 10 · nested paths and names with spaces, a trailing slash in the entry, and several entries.
setup .venv 'my env'
links '["my env/", ".venv", 7, "tools/.venv"]'; run
check "space in name linked" "$(kind "$WT/my env")" "link:$MAIN/my env"
check "second entry linked" "$(kind "$WT/.venv")" "link:$MAIN/.venv"
check "nested entry linked" "$(kind "$WT/tools/.venv")" "link:$MAIN/tools/.venv"
warned "not a one-line string" "non-string entry"
check "several links: worktree clean" "$(dirty)" ""
echo "PASS names with spaces, nested paths, a trailing slash and several entries"

# 11 · the key is not a list: one warning, no link.
setup .venv
printf '{"repo":{"worktree_links":".venv"}}\n' > "$BUREAU_CONFIG"; run
warned "must be a list" "not a list"
check "not a list: no link" "$(kind "$WT/.venv")" none
echo "PASS a value that is not a list makes no links"

# 12 · never in the main checkout itself.
setup .venv
links '[".venv"]'
OUT=$(bureau_link_worktree_paths "$MAIN")
check "main checkout: silent" "$OUT" ""
check "main checkout: venv is still the real directory" "$(kind "$MAIN/.venv")" dir
check "main checkout: no link inside the venv" "$(find "$MAIN/.venv" -type l | wc -l | tr -d ' ')" 0
echo "PASS the main checkout itself is never linked"

# 13 · an entry starting with '-' is a name, not an option — also under the worker's set -euo pipefail.
setup .venv -venv
mkdir -p "$MAIN/-venv/bin"; printf 'x\n' > "$MAIN/-venv/bin/python"
links '["-venv", ".venv"]'
OUT=$(bash -c 'set -euo pipefail; source "$1"; bureau_link_worktree_paths "$2"; echo "rc-ok"' _ "$TMP/fn.sh" "$WT" 2>&1) || true
case "$OUT" in *rc-ok*) : ;; *) fail "dash entry under set -euo pipefail: [$OUT]" ;; esac
check "dash entry linked" "$(kind "$WT/-venv")" "link:$MAIN/-venv"
check "entry after the dash entry linked" "$(kind "$WT/.venv")" "link:$MAIN/.venv"
check "dash entry: worktree clean" "$(dirty)" ""
echo "PASS an entry starting with '-' is linked, and the worker survives it under set -e"

# 14 · a parent that runs through a symlink inside the worktree gets its own message.
setup .venv
mkdir -p "$WT/real"; printf 'r\n' > "$WT/real/keep"; ln -s real "$WT/alias"
git -C "$WT" add real alias; commit_in_branch "alias -> real"
mkdir -p "$MAIN/alias/.venv"
links '["alias/.venv"]'; run
warned "'alias' runs through a symlink in the worktree" "symlink parent inside"
check "nothing linked through the symlink" "$(ls -A "$WT/real")" keep
echo "PASS a parent that runs through a symlink inside the worktree is refused with its own reason"

# 15 · a bare repository has no main checkout: nothing is linked from its parent directory.
rm -rf "$TMP/bare proj"; mkdir -p "$TMP/bare proj"
git -C "$MAIN" clone -q --bare "$MAIN" "$TMP/bare proj/repo.git"
git -C "$TMP/bare proj/repo.git" worktree add -q "$TMP/bare proj/wt" main 2>/dev/null
mkdir -p "$TMP/bare proj/.venv"
links '[".venv"]'
OUT=$(bureau_link_worktree_paths "$TMP/bare proj/wt")
warned "the repository is bare" "bare"
check "bare: no link" "$(kind "$TMP/bare proj/wt/.venv")" none
echo "PASS a worktree of a bare repository gets no links"

echo "OK test_worktree_links"
