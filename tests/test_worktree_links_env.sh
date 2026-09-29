#!/bin/bash
# repo.worktree_links never brings a .env file into a stage worktree.
#
# A stage worktree holds no .env after its reset (`git clean -fdx`), so code from the branch
# that the review build check, QA or an agent runs there finds no secrets on disk next to it;
# the stages read .env from the main checkout. A `.env*` entry in repo.worktree_links would
# link the main checkout's secrets right back in. The stages skip such an entry with one
# warning line: any path component that starts with `.env` in any case, or an entry whose real
# target in the main checkout is such a file (the rule env_file in bureau-doctor.py reports as
# an error), and, beyond the doctor, a directory with such a name among the entries of its top
# two levels (a .env three levels down is not searched: `deeper` below is linked, by design, so
# a virtualenv is checked quickly). Names that only contain "env" are linked as before.
#
# Runs the REAL bureau_link_worktree_paths, cut from templates/scripts/bureau-config.sh, as
# tests/test_worktree_links.sh does, against a real main checkout (with a space in its path)
# and a linked worktree cleaned the way reset_worktree cleans it. Every entry below is ignored
# by .gitignore and exists in the main checkout, so without the guard every one of them is
# linked: the last case shows that on the same fixture with the guard cut out.
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "$0")" && cd .. && pwd)"
SCRIPTS="$REPO_ROOT/templates/scripts"
TMP=$(mktemp -d -t bureau-test.links-env.XXXXXXXX)
TMP=$(cd "$TMP" && pwd -P)
trap 'rm -rf "$TMP"' EXIT
FAILS=0
fail() { echo "FAIL $*" >&2; FAILS=$((FAILS + 1)); }

cut_fn() {  # <bureau-config.sh> <out>
  sed -n '/^# ── Worktree links (repo.worktree_links)/,/^# ── End of worktree links/p' "$1" > "$2"
  grep -q '^bureau_link_worktree_paths() {' "$2" || { echo "FAIL bureau_link_worktree_paths not found in $1"; exit 1; }
}
cut_fn "$SCRIPTS/bureau-config.sh" "$TMP/fn.sh"

MAIN="$TMP/main checkout"; WT="$TMP/stage worktree"
export BUREAU_CONFIG="$TMP/bureau config.json"
# key_readable: the Linear probe can be read through anything in the worktree, links followed.
key_readable() { find -L "$WT" -name .git -prune -o -type f -exec grep -l 'lin_api_PROBE_linear_0001' {} + 2>/dev/null | grep -q .; }
kind() { if [ -L "$1" ]; then echo "link:$(readlink "$1")"; elif [ -e "$1" ]; then echo present; else echo none; fi; }

setup() {
  rm -rf "$MAIN" "$WT"
  mkdir -p "$MAIN"
  git -C "$MAIN" init -q -b main
  git -C "$MAIN" config user.email t@t; git -C "$MAIN" config user.name t
  printf '%s\n' .env '.env.*' .envrc .ENV.Local 'config/.env.production' secrets conf upper '.Envs/prod' \
    'tools/.Envs/key' settings deep deeper .venv my.env env > "$MAIN/.gitignore"
  mkdir -p "$MAIN/config" "$MAIN/.Envs" "$MAIN/tools/.Envs"; printf 'config\n' > "$MAIN/config/readme.txt"
  printf 'tracked\n' > "$MAIN/.Envs/README"          # a tracked directory with a .env* name
  printf 'tracked\n' > "$MAIN/tools/.Envs/README"    # the same, below the first path component
  git -C "$MAIN" add -A >/dev/null; git -C "$MAIN" commit -q -m init
  printf 'LINEAR_API_KEY=lin_api_PROBE_linear_0001\n' > "$MAIN/.env"
  printf 'X=1\n' > "$MAIN/.env.local"; printf 'X=1\n' > "$MAIN/.envrc"; printf 'X=1\n' > "$MAIN/.ENV.Local"
  printf 'X=1\n' > "$MAIN/config/.env.production"
  ln -s .env "$MAIN/secrets"                       # a harmless name that leads to .env
  mkdir -p "$MAIN/.envdir"; ln -s .envdir "$MAIN/conf"   # a directory link to a .env* directory
  ln -s .ENV.Local "$MAIN/upper"                   # a link to an upper-case .env* file
  printf 'X=1\n' > "$MAIN/.Envs/prod"               # a file inside a .env* directory
  printf 'X=1\n' > "$MAIN/tools/.Envs/key"          # ... not the first component
  mkdir -p "$MAIN/settings" "$MAIN/deep/sub" "$MAIN/deeper/a/b"
  printf 'LINEAR_API_KEY=lin_api_PROBE_linear_0001\n' > "$MAIN/settings/.env"   # a directory holding .env
  printf 'X=1\n' > "$MAIN/deep/sub/.Env.Local"      # ... one level further down
  printf 'X=1\n' > "$MAIN/deeper/a/b/.env"         # ... below the two levels searched (linked)
  mkdir -p "$MAIN/.venv/bin" "$MAIN/env"; printf 'x\n' > "$MAIN/my.env"
  git -C "$MAIN" branch feat
  git -C "$MAIN" worktree add -q "$WT" feat
  git -C "$WT" clean -fdx --quiet
}

ENV_ENTRIES='.env .env.local .envrc .ENV.Local config/.env.production secrets conf upper .Envs/prod tools/.Envs/key settings deep'
OTHER_ENTRIES='.venv my.env env deeper'
LIST='[".env", ".env.local", ".envrc", ".ENV.Local", "config/.env.production", "secrets", "conf", "upper", ".Envs/prod", "tools/.Envs/key", "settings", "deep", ".venv", "my.env", "env", "deeper"]'

# run_with <fn file> — the configured list through the given copy of the function.
run_with() {
  setup
  printf '{"repo":{"worktree_links":%s}}\n' "$LIST" > "$BUREAU_CONFIG"
  OUT=$(/bin/bash -c 'set -euo pipefail; source "$1"; bureau_link_worktree_paths "$2"' _ "$1" "$WT" 2>&1); RC=$?
}

# 1 · the real function: no .env* entry is linked, each is named in one warning line, the
#     other entries are linked as before, and the worktree stays clean.
run_with "$TMP/fn.sh"
[ "$RC" = 0 ] || fail "1: bureau_link_worktree_paths returned $RC"
for e in $ENV_ENTRIES; do
  [ "$(kind "$WT/$e")" = none ] || fail "1: '$e' was linked into the stage worktree ($(kind "$WT/$e"))"
  [ "$(printf '%s\n' "$OUT" | grep -c "worktree link '$e' skipped: .*\.env file")" = 1 ] \
    || fail "1: no single .env warning for '$e': $OUT"
done
for e in $OTHER_ENTRIES; do
  [ "$(kind "$WT/$e")" = "link:$MAIN/$e" ] || fail "1: '$e' is not linked ($(kind "$WT/$e")): $OUT"
done
if key_readable; then fail "1: the main checkout's Linear key is readable in the stage worktree"; fi
[ -z "$(git -C "$WT" status --porcelain --untracked-files=all)" ] || fail "1: the worktree is dirty"
[ "$FAILS" = 0 ] && echo "PASS .env* entries are skipped with a warning, other entries are linked, the worktree has no secrets"

# 2 · negative control: the same fixture with the guard cut out of the same function links
#     every .env* entry (the v3.0.2 behaviour), so case 1 cannot pass on a fixture that never
#     would have been linked.
python3 - "$TMP/fn.sh" "$TMP/fn-old.sh" <<'PY'
import re, sys
src = open(sys.argv[1]).read()
start = src.index('  # Never a .env file:')
end = src.index('  # `grep -c`, not `grep -q`')
assert start < end
open(sys.argv[2], 'w').write(src[:start] + src[end:])
PY
before=$FAILS
run_with "$TMP/fn-old.sh"
for e in $ENV_ENTRIES; do
  case "$(kind "$WT/$e")" in link:*) ;; *) fail "2 control: without the guard '$e' should have been linked" ;; esac
done
key_readable || fail "2 control: without the guard the Linear key should be readable through the link"
[ "$FAILS" = "$before" ] && echo "PASS control: without the guard the same entries are linked and the key is readable"

# 3 · fail-closed: when the target cannot be resolved (no working python3), nothing is linked.
mkdir -p "$TMP/nopy"; printf '#!/bin/sh\nexit 1\n' > "$TMP/nopy/python3"; chmod +x "$TMP/nopy/python3"
before=$FAILS
PATH="$TMP/nopy:$PATH" run_with "$TMP/fn.sh"
[ "$(kind "$WT/.venv")" = none ] || fail "3: .venv was linked although its target could not be resolved"
case "$OUT" in *"worktree link '.venv' skipped: its target in the main checkout could not be resolved"*) ;; *) fail "3: no warning for the unresolved target: $OUT" ;; esac
[ "$FAILS" = "$before" ] && echo "PASS an entry whose target cannot be resolved is skipped, not linked"

# 4 · the stages and bureau-doctor.py agree: every entry the doctor reports as `env file` is
#     skipped by the stages, and the stages skip nothing else except the directories that hold a
#     .env* entry (their own, deeper check).
before=$FAILS
setup
printf '{"repo":{"worktree_links":%s}}\n' "$LIST" > "$BUREAU_CONFIG"
doctor_env=$(python3 - "$SCRIPTS/bureau-doctor.py" "$MAIN" "$BUREAU_CONFIG" <<'PY'
import importlib.util, json, sys
from pathlib import Path
spec = importlib.util.spec_from_file_location('doctor', sys.argv[1]); d = importlib.util.module_from_spec(spec); spec.loader.exec_module(d)
report, errors, warnings = d.worktree_links(Path(sys.argv[2]), json.loads(open(sys.argv[3]).read()))
print(' '.join(sorted(entry['path'] for entry in report if entry.get('status') == 'env file')))
PY
)
stage_skipped=$(for e in $ENV_ENTRIES; do case "$e" in (settings|deep) ;; (*) printf '%s\n' "$e" ;; esac; done | LC_ALL=C sort | tr '\n' ' ' | sed 's/ $//')
doctor_sorted=$(printf '%s\n' $doctor_env | LC_ALL=C sort | tr '\n' ' ' | sed 's/ $//')
[ "$doctor_sorted" = "$stage_skipped" ] || fail "4: the doctor reports [$doctor_sorted] as env file, the stages skip [$stage_skipped] by the same rule"
[ "$FAILS" = "$before" ] && echo "PASS the stages skip exactly what bureau-doctor.py reports as a .env file, plus directories holding one"

if [ "$FAILS" != 0 ]; then echo "$FAILS check(s) failed" >&2; exit 1; fi
echo "OK test_worktree_links_env"
