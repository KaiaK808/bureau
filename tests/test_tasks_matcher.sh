#!/bin/bash
# Regression test for bureau_spec_dir_for_branch, the branch → spec directory
# matcher every stage uses (bureau-config.sh), and bureau_spec_dir_candidates.
#
# The bug it guards against: installations that mint "001-<slug>" for every ticket
# (and installations that repeat a number now and then) share one numeric prefix
# across many spec directories. The old numeric-prefix-first matcher handed every
# such branch the FIRST-ALPHABETICAL 001-*/tasks.md, i.e. another ticket's tasks;
# the loose substring match the other stages used picked `001-foo` for `001-foo-bar`.
# Now: the branch loses a `prefix/` and an issue key; then exact directory name >
# the one slug that fits (equal; the branch slug as its start up to a `-`; or the
# directory slug as whole words inside the branch slug) > among several fits, the
# one carrying the branch's number. A number alone never selects. Ambiguous → nothing.
#
# The functions are EXTRACTED from the shipped template (not re-typed), so this
# fails loudly if the logic regresses or a resync re-introduces an old matcher.
# The matching order goes back to a fix from June 2026 (3f034a0) that one
# installation carried by hand.
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "$0")" && cd .. && pwd)"
SRC="$REPO_ROOT/templates/scripts/bureau-config.sh"

MATCHER=$(sed -n '/^# ── Spec directory of a branch/,/^# ── End of spec directory of a branch/p' "$SRC")
case "$MATCHER" in
  *'bureau_spec_dir_for_branch() {'*'bureau_spec_dir_candidates() {'*) : ;;
  *) echo "FAIL: could not extract the spec-directory matcher from $SRC" >&2; exit 1 ;;
esac
eval "$MATCHER"

pass=0; fail=0; fail_msgs=()
assert() {  # <label> <expected> <actual>
  if [ "$2" = "$3" ]; then
    pass=$((pass + 1))
  else
    fail=$((fail + 1)); fail_msgs+=("$1: expected '$2' got '$3'")
  fi
}

ROOT=$(mktemp -d -t bureau-test.matcher.XXXXXX)
trap 'rm -rf "$ROOT"' EXIT

# mkspecs <set> <dir>... — a specs directory holding these spec dirs, each with a tasks.md.
mkspecs() {
  local set="$1" d; shift
  mkdir -p "$ROOT/$set"
  for d in "$@"; do mkdir -p "$ROOT/$set/$d"; : > "$ROOT/$set/$d/tasks.md"; done
}

# resolve <set> <branch> → the matched directory's basename, or "<none>".
resolve() {
  local out
  out=$(BUREAU_SPECS_DIR="$ROOT/$1" bureau_spec_dir_for_branch "$2")
  if [ -n "$out" ]; then basename "$out"; else echo "<none>"; fi
}
# candidates <set> <branch> → the tied directories as the stages print them ("`a`, `b`"),
# sorted here because the glob order follows the locale; or "<none>".
candidates() {
  local out
  out=$(BUREAU_SPECS_DIR="$ROOT/$1" bureau_spec_dir_candidates "$2")
  if [ -n "$out" ]; then printf '%s\n' "${out//, /$'\n'}" | LC_ALL=C sort | paste -sd, - | sed 's/,/, /g'; else echo "<none>"; fi
}

# A — ten all-"001-" dirs, a 091 truncation pair, and a long name for truncation.
mkspecs a 001-audit-trail 001-retry-queue 001-cache-warmup 001-data-export \
          001-email-digest 001-feature-flags 001-graph-view 001-health-check \
          001-invoice-pdf 001-job-scheduler 091-wire-mcp-tool-metadata \
          001-long-feature-name-here
assert "A1 own dir, not first-alphabetical (retry-queue)" "001-retry-queue" "$(resolve a 001-retry-queue)"
assert "A2 absent branch → none, not first-alphabetical" "<none>"          "$(resolve a 001-ghost-ticket)"
assert "A3 truncated branch → unique slug-prefix dir"     "091-wire-mcp-tool-metadata" "$(resolve a 091-wire-mcp-tool)"
assert "A4 own dir (graph-view)"                           "001-graph-view"   "$(resolve a 001-graph-view)"
assert "A5 own dir (long name, sorts last among 001)"      "001-long-feature-name-here" "$(resolve a 001-long-feature-name-here)"
assert "A6 raw output keeps the specs path and trailing slash" \
  "$ROOT/a/001-retry-queue/" "$(BUREAU_SPECS_DIR="$ROOT/a" bureau_spec_dir_for_branch 001-retry-queue)"
assert "A7 truncated at a word among all-001 dirs → slug prefix" \
  "001-long-feature-name-here" "$(resolve a 001-long-feature)"
assert "A8 truncated inside a word → none (the prefix must end at '-')" "<none>" "$(resolve a 001-long-feature-na)"

# B — prefix siblings: the exact directory wins over a slug-prefix sibling, both ways.
mkspecs b 001-foo 001-foo-bar
assert "B1 exact 001-foo beats sibling 001-foo-bar"     "001-foo"     "$(resolve b 001-foo)"
assert "B2 exact 001-foo-bar beats sibling 001-foo"     "001-foo-bar" "$(resolve b 001-foo-bar)"
assert "B3 a branch with more words fits both → none"   "<none>"      "$(resolve b 001-foo-bar-baz)"

# C — numbers: they only decide between slug fits, never alone.
mkspecs c 001-export 002-export 010-reporting
assert "C1 exact 002-export"                                "002-export"  "$(resolve c 002-export)"
assert "C2 002-export-csv fits both → the one numbered 002" "002-export"  "$(resolve c 002-export-csv)"
assert "C3 slug fits both, neither numbered 003 → none"     "<none>"      "$(resolve c 003-export)"
assert "C4 no slug fit, unique number → none (a number alone never selects)" "<none>" "$(resolve c 010-weekly-numbers)"
assert "C5 no slug fit, number shared → none"               "<none>"      "$(resolve a 001-renamed-ticket)"
assert "C6 ambiguous slug fits never fall to another slug's number" "<none>" "$(resolve c 010-export)"
mkspecs c7 009-widget-attrs 110-other-feature
assert "C7 a single slug fit wins over a directory carrying the branch's number" \
  "009-widget-attrs" "$(resolve c7 110-widget-attrs)"

# D — branches without a number, spec dirs without a number (hand-named, legacy).
mkspecs d 001-feature-login 001-other legacy-auth 004-CSV-Import 004-other
assert "D1 branch without number → slug match"          "001-feature-login" "$(resolve d feature-login)"
assert "D2 numbered branch → legacy dir by slug"        "legacy-auth" "$(resolve d 003-legacy-auth)"
assert "D3 exact legacy name"                           "legacy-auth" "$(resolve d legacy-auth)"
assert "D4 slug compares case-insensitively (number 004 is shared)" "004-CSV-Import" "$(resolve d 004-csv-import)"
assert "D5 unrelated branch → none"                     "<none>"      "$(resolve d someone/exp-12-billing)"

# F — branch names with a `prefix/` and an issue key (Linear branch names, codex/…).
mkspecs f 142-request-routing 141-backup-runbook 001-integration-tests 006-score-merge \
          009-default-attrs 001-other-thing
assert "F1 codex/<key>-<slug> → the slug's dir"         "142-request-routing"   "$(resolve f codex/exp-1444-request-routing)"
assert "F2 user/<key>-<task>-<slug>-<more> → dir slug as whole words" \
  "001-integration-tests" "$(resolve f someone/exp-404-t1-integration-tests-unit-fixtures)"
assert "F3 exp/<key>-<slug>"                            "006-score-merge"       "$(resolve f exp/exp-94-score-merge)"
assert "F4 exp/<other number>-<slug> → the slug's dir"  "009-default-attrs"     "$(resolve f exp/110-default-attrs)"
assert "F5 upper-case key without a prefix"             "142-request-routing"   "$(resolve f EXP-7-request-routing)"
assert "F6 prefix, key and a truncated slug"            "142-request-routing"   "$(resolve f codex/exp-1444-request)"
assert "F7 exact name after the prefix"                 "141-backup-runbook"    "$(resolve f team/141-backup-runbook)"
assert "F8 prefix and a numbered, truncated slug"       "142-request-routing"   "$(resolve f codex/142-request)"
assert "F9 exact name after the prefix beats a slug-prefix sibling" "001-foo" "$(resolve b team/001-foo)"
mkspecs f10 007-k8s-1-upgrade-notes
assert "F10 only letters form an issue key: k8s-1- stays part of the slug" "007-k8s-1-upgrade-notes" "$(resolve f10 k8s-1-upgrade)"

# G — word boundaries: a fit starts or ends at a '-', in both directions.
mkspecs g1 001-refund-rate 001-rate-limit
assert "G1 001-rate → start of 001-rate-limit, not the middle of 001-refund-rate" "001-rate-limit" "$(resolve g1 001-rate)"
mkspecs g2 001-fund 001-other
assert "G2 a directory slug inside a word of the branch → none" "<none>" "$(resolve g2 001-refund-rate)"
mkspecs g3 001-re 001-other
assert "G3 a directory slug that is part of a branch word → none" "<none>" "$(resolve g3 001-refund-rate)"
mkspecs g4 002-a.b '003-[ab]'
assert "G4 003-a → none: 'a' is not a word of 'a.b', and '[ab]' is literal" "<none>" "$(resolve g4 003-a)"
mkspecs g5 001-refund 045-rate-of-refund
assert "G5 whole leading words of the branch → that dir" "001-refund" "$(resolve g5 001-refund-rate)"

# H — the candidates the stages name when there is no single directory.
assert "H1 tied dirs are named"                         "\`001-foo-bar\`, \`001-foo\`" "$(candidates b 001-foo-bar-baz)"
assert "H2 tied dirs, none with the branch's number"    "\`001-export\`, \`002-export\`" "$(candidates c 003-export)"
assert "H3 a unique fit has no candidates"              "<none>"      "$(candidates b 001-foo)"
assert "H4 no fit has no candidates"                    "<none>"      "$(candidates a 001-ghost-ticket)"
assert "H5 narrowed by number → no candidates"          "<none>"      "$(candidates c 002-export-csv)"

# E — edges: nothing to match, and safe under set -euo pipefail.
mkdir -p "$ROOT/empty"
assert "E1 empty specs dir → none"                      "<none>"      "$(resolve empty 001-foo)"
assert "E2 missing specs dir → none"                    "<none>"      "$(resolve missing 001-foo)"
assert "E3 empty branch → none"                         "<none>"      "$(resolve a '')"
rc=$(bash -c 'set -euo pipefail; eval "$1"; BUREAU_SPECS_DIR="$2" bureau_spec_dir_for_branch 001-ghost >/dev/null; BUREAU_SPECS_DIR="$2" bureau_spec_dir_candidates 001-ghost >/dev/null; echo ok' _ "$MATCHER" "$ROOT/a")
assert "E4 no match returns 0 under set -euo pipefail"  "ok"          "$rc"
# A file (not a directory) under specs/ is never a candidate, not even by exact name.
mkspecs e 001-alpha
: > "$ROOT/e/001-alpha-x"
assert "E5 files under specs are ignored"               "001-alpha"   "$(resolve e 001-alpha-x)"

echo "passed: $pass  failed: $fail"
if [ "$fail" -gt 0 ]; then
  printf '  - %s\n' "${fail_msgs[@]}"
  exit 1
fi
echo "OK test_tasks_matcher"
exit 0
