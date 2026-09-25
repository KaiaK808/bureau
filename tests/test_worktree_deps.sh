#!/bin/bash
# After reset_worktree's `clean -fdx`, an npm project gets its node_modules back — safely,
# freshly, and a failure stops the stage with 24 (environment-blocked) instead of building red.
#
# Carried over from msc-planner's scripts/worktree-deps.test.sh (EXP-1375). Runs the REAL
# restore_worktree_deps, cut from templates/scripts/bureau-config.sh, against a real main
# checkout with a linked worktree; npm and mv are shell functions where a case needs them to
# fail. The function reports its path ("Dependencies: …"), and the cases check the path, not
# just whether a node_modules exists at the end. Last, the exact call line of each of the three
# stages runs in a worktree where npm fails and must exit 24.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")" && cd .. && pwd)"
SCRIPTS="$REPO_ROOT/templates/scripts"
TMP=$(mktemp -d -t bureau-test.deps.XXXXXXXX)
trap 'rm -rf "$TMP"' EXIT

sed -n '/^restore_worktree_deps() {/,/^}/p' "$SCRIPTS/bureau-config.sh" > "$TMP/fn.sh"
grep -q 'restore_worktree_deps()' "$TMP/fn.sh" || { echo "FAIL restore_worktree_deps not found in bureau-config.sh"; exit 1; }
# shellcheck source=/dev/null
source "$TMP/fn.sh"
MAIN="$TMP/main"; WT="$TMP/wt"

setup() {  # main checkout with installed deps, plus a fresh linked worktree cleaned like reset_worktree
  rm -rf "$MAIN" "$WT"
  mkdir -p "$MAIN"
  git -C "$MAIN" init -q -b main
  git -C "$MAIN" config user.email t@t; git -C "$MAIN" config user.name t
  printf '{"name":"p","version":"1.0.0"}\n' > "$MAIN/package.json"
  printf '{"lockfileVersion":3}\n'          > "$MAIN/package-lock.json"
  printf 'node_modules/\n'                  > "$MAIN/.gitignore"
  git -C "$MAIN" add -A >/dev/null; git -C "$MAIN" commit -q -m init
  mkdir -p "$MAIN/node_modules/pkg"; printf 'real\n' > "$MAIN/node_modules/pkg/index.js"
  git -C "$MAIN" worktree add -q --detach "$WT" main
  git -C "$WT" clean -fdx --quiet
}
foreign_manifest() { printf '{"name":"other","version":"9.9.9"}\n' > "$WT/package.json"; }
path_of() { restore_worktree_deps "$1" 2>/dev/null | sed -n 's/^ *Dependencies: //p' | tail -1; }
fail() { echo "FAIL $*" >&2; exit 1; }
check() { [ "$2" = "$3" ] || fail "$1: got [$2], wanted [$3]"; }
exists() { if [ -e "$1" ] || [ -L "$1" ]; then echo yes; else echo no; fi; }

# 1 · identical manifests → clone from the main checkout
setup
check "clone path" "$(path_of "$WT")" "cloned from the main checkout"
check "cloned package present" "$(exists "$WT/node_modules/pkg/index.js")" yes
# 2 · the stamp lives in the shared .git, where no PR content can write
check "stamp outside the worktree" "$(find "$MAIN/.git/bureau-deps" -type f ! -name '*.log' | wc -l | tr -d ' ')" 1
check "no stamp inside the worktree" "$(find "$WT" -name '*bureau-deps*' | wc -l | tr -d ' ')" 0
# 3 · idempotent
check "second call skips" "$(path_of "$WT")" "unchanged, skipped"
# 4 · fresh: package.json changes, lock does not — only the manifest pair notices
foreign_manifest
npm() { return 1; }
check "changed package.json drops stale deps" "$(path_of "$WT")" "COULD NOT be restored — stopping (environment-blocked)"
check "  stale node_modules removed" "$(exists "$WT/node_modules")" no
unset -f npm
echo "PASS clone on identical manifests, stamp in .git, idempotent, and a changed manifest is noticed"

# 5 · a tracked node_modules survives clean -fdx and is never trusted. It also carries the
#     SIGPIPE trap: a `grep -q` closes the pipe at the first match, and `git ls-files` only dies
#     of SIGPIPE when its output does not fit the pipe buffer (64 KB). 3000 files (~54 KB, msc's
#     number) fit, so the trap never fired there; 6000 (~110 KB) do not.
#     And the stamp must match first: without it the function restores anyway and overwrites
#     the tracked directory, so the guard would never be what decides (the same reason as in 7).
setup
restore_worktree_deps "$WT" >/dev/null 2>&1   # a valid stamp for these manifests
rm -rf "$WT/node_modules"
mkdir -p "$WT/node_modules"; printf 'evil\n' > "$WT/node_modules/INJECTED"
for i in $(seq 1 6000); do printf 'x\n' > "$WT/node_modules/f$i"; done
git -C "$WT" add -f node_modules >/dev/null 2>&1
git -C "$WT" -c user.email=t@t -c user.name=t commit -q -m tracked
git -C "$WT" clean -fdx --quiet
check "  tracked node_modules survives clean (precondition)" "$(exists "$WT/node_modules/INJECTED")" yes
restore_worktree_deps "$WT" >/dev/null 2>&1 || true
check "tracked node_modules discarded" "$(exists "$WT/node_modules/INJECTED")" no
check "  real deps in its place" "$(exists "$WT/node_modules/pkg/index.js")" yes
# 7 · symlink attack: unchanged manifests, so the stamp matches — the `! -L` guard must carry
setup
restore_worktree_deps "$WT" >/dev/null 2>&1
mkdir -p "$TMP/foreign/pkg"; printf 'evil\n' > "$TMP/foreign/pkg/index.js"
rm -rf "$WT/node_modules"; ln -s "$TMP/foreign" "$WT/node_modules"
check "  stamp matches, symlink in place (precondition)" "$([ -L "$WT/node_modules" ] && echo yes || echo no)" yes
restore_worktree_deps "$WT" >/dev/null 2>&1 || true
check "symlinked node_modules discarded despite a matching stamp" "$([ -L "$WT/node_modules" ] && echo symlink || echo gone)" gone
check "  symlink target untouched" "$(exists "$TMP/foreign/pkg/index.js")" yes
check "  real deps instead" "$(exists "$WT/node_modules/pkg/index.js")" yes
# 9 · the main checkout is never filled — there node_modules is the clone source
restore_worktree_deps "$MAIN" >/dev/null 2>&1 || true
check "main checkout untouched" "$(exists "$MAIN/node_modules/pkg/index.js")" yes
echo "PASS a tracked or symlinked node_modules is never trusted, and the main checkout is never touched"

# 6 · foreign manifests → no clone; without npm, nothing — visible
setup; foreign_manifest
npm() { return 1; }
check "foreign manifests do not clone" "$(path_of "$WT")" "COULD NOT be restored — stopping (environment-blocked)"
check "  no node_modules, no temp left" "$(exists "$WT/node_modules")$(exists "$WT/.nm.tmp")" nono
set +e; restore_worktree_deps "$WT" >/dev/null 2>&1; rc=$?; set -e
check "failure returns 24" "$rc" 24
unset -f npm
# 8 · the npm path, positive
setup; foreign_manifest
npm() { mkdir -p node_modules/fromnpm; printf 'via npm\n' > node_modules/fromnpm/index.js; return 0; }
check "npm path runs and reports" "$(path_of "$WT")" "installed with npm ci --ignore-scripts"
check "  npm result in the worktree" "$(exists "$WT/node_modules/fromnpm/index.js")" yes
unset -f npm
# 8b · --ignore-scripts is passed, always
setup; foreign_manifest
npm() { printf '%s\n' "$*" > "$TMP/npm-args"; mkdir -p node_modules; return 0; }
restore_worktree_deps "$WT" >/dev/null 2>&1
case "$(cat "$TMP/npm-args")" in "ci --ignore-scripts"*) ;; *) fail "npm ran without --ignore-scripts: $(cat "$TMP/npm-args")" ;; esac
unset -f npm
echo "PASS different manifests go through npm ci --ignore-scripts, and a failure is 24 with nothing left behind"

# 13 · npm's diagnosis reaches the output
setup; foreign_manifest
npm() { echo "npm error ERESOLVE could not reach the registry" >&2; return 1; }
OUT=$(restore_worktree_deps "$WT" 2>&1 || true)
check "npm error text in the output" "$(printf '%s' "$OUT" | grep -c 'ERESOLVE could not reach the registry' || true)" 1
check "  named as an npm failure" "$(printf '%s' "$OUT" | grep -c 'npm ci failed' || true)" 1
unset -f npm
# 15 · exactly two npm attempts
setup; foreign_manifest
: > "$TMP/npm-count"
npm() { echo x >> "$TMP/npm-count"; return 1; }
restore_worktree_deps "$WT" >/dev/null 2>&1 || true
check "exactly two npm attempts" "$(grep -c . "$TMP/npm-count" || true)" 2
unset -f npm
# 16 · npm succeeds but mounting fails → named as mounting, not as npm
setup; foreign_manifest
npm() { mkdir -p node_modules/fromnpm; return 0; }
mv() { return 1; }
OUT=$(restore_worktree_deps "$WT" 2>&1 || true)
unset -f mv npm
check "mount failure named as mounting" "$(printf '%s' "$OUT" | grep -c 'mounting node_modules failed' || true)" 1
check "  not as npm" "$(printf '%s' "$OUT" | grep -c 'npm ci failed' || true)" 0
# 17 · mixed: attempt 1 fails at mounting, attempt 2 at npm → the message describes the last one
setup; foreign_manifest
: > "$TMP/npm-count"
npm() {
  echo x >> "$TMP/npm-count"
  if [ "$(grep -c . "$TMP/npm-count")" = 1 ]; then mkdir -p node_modules/fromnpm; return 0; fi
  echo "npm error second attempt fails" >&2; return 1
}
mv() { return 1; }
OUT=$(restore_worktree_deps "$WT" 2>&1 || true)
unset -f mv npm
check "mixed path reports the last attempt (npm)" "$(printf '%s' "$OUT" | grep -c 'npm ci failed' || true)" 1
check "  not the mounting" "$(printf '%s' "$OUT" | grep -c 'mounting node_modules failed' || true)" 0
check "  message matches the log below it" "$(printf '%s' "$OUT" | grep -c 'second attempt fails' || true)" 1
echo "PASS the diagnosis is visible, npm is tried exactly twice, and the message names the step that failed last"

# 14 · the real call line of each stage, in a worktree where npm fails, exits 24
for stage in implement-pipeline qa-pipeline code-review-pipeline; do
  line=$(grep -E '^restore_worktree_deps ' "$SCRIPTS/$stage.sh" || true)
  [ "$(printf '%s\n' "$line" | grep -c .)" = 1 ] || fail "$stage.sh: expected exactly one restore_worktree_deps call line"
  setup; foreign_manifest
  mkdir -p "$TMP/bin"; printf '#!/bin/bash\nexit 1\n' > "$TMP/bin/npm"; chmod +x "$TMP/bin/npm"
  set +e
  (cd "$WT" && PATH="$TMP/bin:$PATH" /bin/bash -c "source '$TMP/fn.sh'; $line; echo STAGE-CONTINUED") > "$TMP/stage.out" 2>&1
  rc=$?
  set -e
  check "$stage.sh call line exit" "$rc" 24
  ! grep -q STAGE-CONTINUED "$TMP/stage.out" || fail "$stage.sh carried on after the deps failed"
done
echo "PASS each stage's own call line stops with 24 when the deps cannot be restored"
