#!/bin/bash
# bureau_spec_dir_for_branch when several spec directories fit the branch slug, the
# issue-key normaliser cases N4 and N7, and the Linear-fallback limitation D1-1.
#
# Tie order (v3.1): the branch's number narrows the fits first (as in v3.0.2); among
# the fits left, the one whose slug equals the branch slug wins; two or more left is
# a tie (no directory, the stages name the candidates). Every answer v3.0.2 gave
# stays; only former ties can become a pick. The function is cut out of the shipped
# bureau-config.sh, as in tests/test_tasks_matcher.sh.
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "$0")" && cd .. && pwd)"
SRC="${BUREAU_MATCHER_SRC:-$REPO_ROOT/templates/scripts/bureau-config.sh}"

MATCHER=$(sed -n '/^# ── Spec directory of a branch/,/^# ── End of spec directory of a branch/p' "$SRC")
case "$MATCHER" in
  *'bureau_spec_dir_for_branch() {'*'bureau_spec_dir_candidates() {'*) : ;;
  *) echo "FAIL: could not extract the spec-directory matcher from $SRC" >&2; exit 1 ;;
esac
eval "$MATCHER"

pass=0; fail=0; skipped=0; fail_msgs=()
assert() {  # <label> <expected> <actual>
  if [ "$2" = "$3" ]; then
    pass=$((pass + 1))
  else
    fail=$((fail + 1)); fail_msgs+=("$1: expected '$2' got '$3'")
  fi
}

ROOT=$(mktemp -d -t bureau-test.tiebreak.XXXXXX)
trap 'rm -rf "$ROOT"' EXIT

mkspecs() {  # <set> <dir>... — a specs directory holding these spec dirs, each with a tasks.md
  local set="$1" d; shift
  mkdir -p "$ROOT/$set"
  for d in "$@"; do mkdir -p "$ROOT/$set/$d"; : > "$ROOT/$set/$d/tasks.md"; done
}
resolve() {  # <set> <branch> → basename of the matched directory, or <none>
  local out
  out=$(BUREAU_SPECS_DIR="$ROOT/$1" bureau_spec_dir_for_branch "$2")
  if [ -n "$out" ]; then basename "$out"; else echo "<none>"; fi
}
candidates() {  # <set> <branch> → the tied directories, sorted, or <none>
  local out
  out=$(BUREAU_SPECS_DIR="$ROOT/$1" bureau_spec_dir_candidates "$2")
  if [ -n "$out" ]; then printf '%s\n' "${out//, /$'\n'}" | LC_ALL=C sort | paste -sd, - | sed 's/,/, /g'; else echo "<none>"; fi
}

# T — several fits.
# T1 (D1-4): an equal slug next to a leading word run, no number on the branch. v3.0.2: tie.
mkspecs t1 117-report-builder 128-report-builder-wireup
assert "T1 equal slug beats a word-run fit (prefix and key on the branch)" "128-report-builder-wireup" "$(resolve t1 codex/exp-999-report-builder-wireup)"
assert "T1 no candidates once it resolves"                     "<none>" "$(candidates t1 codex/exp-999-report-builder-wireup)"
assert "T1 the shorter directory still resolves for its own slug" "117-report-builder" "$(resolve t1 someone/exp-12-report-builder)"
# T2: an equal slug next to a forward-prefix fit. v3.0.2: tie.
mkspecs t2 003-cache-warmup 010-cache-warmup-jobs
assert "T2 equal slug beats a directory the branch slug is the start of" "003-cache-warmup" "$(resolve t2 exp-4-cache-warmup)"
assert "T2 slug equality ignores letter case"                  "003-cache-warmup" "$(resolve t2 EXP-4-Cache-Warmup)"
# T3: the number decides before equality (unchanged from v3.0.2; rejected option D9 b
# would have taken 002-auth).
mkspecs t3 002-auth 007-auth-sso
assert "T3 the branch's number beats an equal slug"            "007-auth-sso" "$(resolve t3 007-auth)"
# T4: two fits carry the number; the equal one among them wins. v3.0.2: tie.
mkspecs t4 002-auth 007-auth 007-auth-sso
assert "T4 among the numbered fits, the equal slug"            "007-auth" "$(resolve t4 codex/exp-5-007-auth)"
# T5: two fits carry the number, neither is equal; an equal slug with another number
# does not break that tie (the number already narrowed the fits).
mkspecs t5 002-auth 007-auth-sso 007-auth-x
assert "T5 an equal slug outside the numbered fits does not decide" "<none>" "$(resolve t5 007-auth)"
assert "T5 all fits are named"                                 "\`002-auth\`, \`007-auth-sso\`, \`007-auth-x\`" "$(candidates t5 007-auth)"
# T6: two equal slugs, no number on the branch.
mkspecs t6 002-auth 004-auth
assert "T6 two equal slugs are a tie"                          "<none>" "$(resolve t6 exp-3-auth)"
assert "T6 both are named"                                     "\`002-auth\`, \`004-auth\`" "$(candidates t6 exp-3-auth)"
# T7: a forward-prefix fit and a word-run fit, neither equal.
mkspecs t7 003-export 010-report-export-button
assert "T7 a prefix fit is not an equal slug"                  "<none>" "$(resolve t7 exp-3-report-export)"
# T8: two numbered fits whose names differ only in letter case, both equal. Needs a
# case-sensitive file system (macOS volumes are usually not); skipped elsewhere.
mkdir -p "$ROOT/t8/007-Auth"
if [ ! -d "$ROOT/t8/007-auth" ]; then
  mkspecs t8 007-Auth 007-auth 007-auth-sso
  assert "T8 two equal numbered fits are a tie"                "<none>" "$(resolve t8 codex/exp-5-007-auth)"
else
  skipped=$((skipped + 1))
fi

# N — the issue-key normaliser.
# N4: the key's letters may be upper or mixed case; the slug then only fits once the
# key is gone (a truncated slug: no word run of the raw name fits).
mkspecs n4 012-request-routing 013-other-thing
assert "N4 upper-case key before a truncated slug"             "012-request-routing" "$(resolve n4 EXP-7-request)"
assert "N4 mixed-case key before a truncated slug"             "012-request-routing" "$(resolve n4 Exp-7-request)"
# N7: the second word must be all digits to be a key: `fix-login-page` keeps `login`.
mkspecs n7 003-fix-login-page 004-other-thing
assert "N7 an unnumbered, keyless three-word branch keeps its words" "003-fix-login-page" "$(resolve n7 fix-login-page)"
assert "N7 same with a prefix"                                 "003-fix-login-page" "$(resolve n7 someone/fix-login-page)"

# D1-1 (known limitation, same answer as v3.0.1): without a bureau-branch marker the
# stages use Linear's generated branch name `user/<key>-<title-slug>`. A child ticket
# whose title starts with the parent ticket's slug gets the parent's directory: the
# parent's slug is a leading word run of the title, the child's own slug is not a
# contiguous run of it. The marker is canonical: restore it (troubleshooting, exit 13).
mkspecs d11 020-split-config-loader 024-config-loader-cli-flags
assert "D1-1 a Linear-fallback name can take the parent ticket's directory" "020-split-config-loader" "$(resolve d11 someone/exp-513-split-config-loader-phase-2-cli-flags)"
assert "D1-1 the marker branch resolves to the child's own directory" "024-config-loader-cli-flags" "$(resolve d11 024-config-loader-cli-flags)"

echo "passed: $pass  failed: $fail  skipped: $skipped"
if [ "$fail" -gt 0 ]; then
  printf '  - %s\n' "${fail_msgs[@]}"
  exit 1
fi
echo "OK test_spec_dir_tiebreak"
exit 0
