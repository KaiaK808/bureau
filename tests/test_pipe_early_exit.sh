#!/bin/bash
# No test pipes text into a reader that can leave before it has read all of it.
#
# Under `set -o pipefail` a pipeline fails when any member fails. A reader that leaves early
# (`awk … exit`, `head`, `grep -q`/`-m`/`-l`, `sed … q`) closes the pipe while the writer may
# still have output, and the writer then dies of SIGPIPE (141) or, with SIGPIPE ignored as on
# the GitHub Actions runner, gets EPIPE ("printf: write error: Broken pipe", status 1). So
# `x=$(printf … | awk … exit)` ends a `set -e` test although awk found its lines, and
# `if printf … | grep -q x; then fail; fi` passes although x was there. It is certain once the
# writer has more to write than the reader took plus the pipe buffer, and a race below that:
# test_needs_human_hold.sh's cut() failed three times on ubuntu CI with
# "line 250: printf: write error: Broken pipe", each time green on the rerun.
#
#   1. the REAL shared helpers, read out of the test files (cut, has/hasnt in four files,
#      has_marker, has_ci_marker, pr5_run_id), on 1 MB inputs they match in the first lines,
#      under this suite's bash with set -euo pipefail, SIGPIPE ignored and SIGPIPE default:
#      each gives its answer. Negative control: the same helpers in their pipe form before this
#      change (reconstructed here, CI checks out without history) fail on the same inputs,
#      under both settings, every time.
#   2. no shell file under tests/ pipes into such a reader: a static scan, itself checked
#      against forms it must and must not report.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
T=$(mktemp -d -t bureau-test.pipe.XXXXXXXX)
trap 'rm -rf "$T"' EXIT
fail() { echo "FAIL: $1" >&2; exit 1; }

# 1 MB of numbered lines, and a lease file whose run ids come to about 400 KB of jq output.
python3 -I -c 'import sys
open(sys.argv[1], "w").write("".join("line %07d\n" % i for i in range(80000)))' "$T/big"
mkdir -p "$T/common/bureau"
python3 -I -c 'import json, sys
json.dump({"issue:EXP-%d" % i: {"run_id": "run-%06d" % i} for i in range(40000)}, open(sys.argv[1], "w"))' "$T/common/bureau/leases.json"
[ "$(wc -c < "$T/big" | tr -d ' ')" -ge 1000000 ] || fail 'the 1 MB input is smaller than 1 MB'
# A CI marker built at run time, so this file does not carry one.
MARK="[skip"; MARK="$MARK ci]"
export T MARK COMMON="$T/common"

# definition <file under tests/> <name>: the function as the file defines it.
definition() {
  awk -v n="$2() {" 'index($0, n) == 1 { f = 1 } f { print } f && (/^}/ || /; }$/) { exit }' "$ROOT/tests/$1"
}
# probe <ignore|default> <definitions> <check>: definitions + check in a fresh "$BASH" (the
# bash that runs this suite) under set -euo pipefail, SIGPIPE ignored or default. Sets PRC, POUT.
probe() {
  set +e
  POUT=$(python3 -I -c 'import os, signal, sys
signal.signal(signal.SIGPIPE, signal.SIG_IGN if sys.argv[1] == "ignore" else signal.SIG_DFL)
os.execv(sys.argv[2], [sys.argv[2], "-c", "set -euo pipefail\n" + sys.argv[3] + "\n" + sys.argv[4], "probe"])' \
    "$1" "$BASH" "fail() { echo \"FAIL \$*\" >&2; exit 1; }; report() { :; }
$2" "$3" 2>&1)
  PRC=$?
  set -e
}

CHECK_CUT='X=$(cut "$(cat "$T/big")" "^line 0000001$" "^line 0000003$")
[ "$X" = "$(printf "line %07d\n" 1 2 3)" ] || { echo "WRONG CUT: ${X:0:80}"; exit 3; }; echo FOUND'
CHECK_HAS='has "^line 0000000$" "$(cat "$T/big")" probe; echo FOUND'
CHECK_HASNT='hasnt "^line 0000000$" "$(cat "$T/big")" probe; echo MISSED'
CHECK_MARKER='has_marker "$MARK
$(cat "$T/big")"; echo FOUND'
CHECK_CI_MARKER='has_ci_marker "$MARK
$(cat "$T/big")"; echo FOUND'
CHECK_RUN_ID='R=$(pr5_run_id); [ "$R" = run-000000 ] || { echo "WRONG RUN ID: $R"; exit 3; }; echo FOUND'

# expect_found <label> <definitions> <check>: under both settings the check prints FOUND.
expect_found() {
  [ -n "$2" ] || fail "$1: definition not found"
  for sig in ignore default; do
    probe "$sig" "$2" "$3"
    [ "$PRC" = 0 ] && [ "$POUT" = FOUND ] || fail "$1, SIGPIPE $sig: exit $PRC: $POUT"
  done
}
# expect_seen <label> <definitions>: hasnt fails the test on the 1 MB input that holds its pattern.
expect_seen() {
  [ -n "$2" ] || fail "$1: definition not found"
  for sig in ignore default; do
    probe "$sig" "$2" "$CHECK_HASNT"
    [ "$PRC" = 1 ] && [ "$POUT" = 'FAIL probe (unexpected /^line 0000000$/)' ] || fail "$1, SIGPIPE $sig: exit $PRC: $POUT"
  done
}

# ── 1. the real helpers on 1 MB inputs ─────────────────────────────────────
expect_found "cut (test_needs_human_hold.sh)" "$(definition test_needs_human_hold.sh cut)" "$CHECK_CUT"
for f in test_implement_marker_in_loop.sh test_implement_push_open_pr.sh test_post_implement_command.sh test_post_implement_dirty.sh; do
  expect_found "has ($f)" "$(definition "$f" has)" "$CHECK_HAS"
  expect_seen "hasnt ($f)" "$(definition "$f" hasnt)"
done
expect_found "has_marker (test_merge_body.sh)" "$(definition test_merge_body.sh has_marker)" "$CHECK_MARKER"
expect_found "has_ci_marker (test_merge_pipeline_correctness.sh)" "$(definition test_merge_pipeline_correctness.sh has_ci_marker)" "$CHECK_CI_MARKER"
expect_found "pr5_run_id (lib/pr5-interrupt.sh)" "$(definition lib/pr5-interrupt.sh pr5_run_id)" "$CHECK_RUN_ID"
echo 'PASS 1 cut, has/hasnt (four files), has_marker, has_ci_marker and pr5_run_id answer right on 1 MB inputs, SIGPIPE ignored and default'

# Negative control: the pipe forms these helpers had before.
OLD_CUT=$(cat <<'EOF'
cut() {  # $1 = file content, $2 = start regex, $3 = end regex (awk)
  printf '%s\n' "$1" | awk -v s="$2" -v e="$3" '$0 ~ s { f = 1 } f { print } f && $0 ~ e { exit }'
}
EOF
)
OLD_HAS=$(cat <<'EOF'
has() { printf '%s' "$2" | grep -qE -- "$1" || fail "$3 (no match for /$1/)"; }
hasnt() { if printf '%s' "$2" | grep -qE -- "$1"; then fail "$3 (unexpected /$1/)"; fi; }
EOF
)
OLD_MARKERS=$(cat <<'EOF'
has_marker() {
  printf '%s' "$1" | grep -qiE '\[(skip ci|ci skip|no ci|skip actions|actions skip)\]|skip-checks[[:space:]]*:|\*\*\*no_ci\*\*\*'
}
has_ci_marker() {
  printf '%s' "$1" | grep -qiE '\[(skip ci|ci skip|no ci|skip actions|actions skip)\]|skip-checks[[:space:]]*:'
}
EOF
)
OLD_RUN_ID=$(cat <<'EOF'
pr5_run_id() { jq -r 'to_entries[] | select(.key | startswith("issue:")) | .value.run_id' "$COMMON/bureau/leases.json" 2>/dev/null | head -1; }
EOF
)
for sig in ignore default; do
  probe "$sig" "$OLD_CUT" "$CHECK_CUT"
  [ "$PRC" != 0 ] || fail "negative control, SIGPIPE $sig: the old cut() passed on 1 MB, so this proves nothing: $POUT"
  if [ "$sig" = ignore ]; then
    case "$POUT" in *'printf: write error: Broken pipe'*) ;; *) fail "negative control: the old cut() did not fail as on CI (exit $PRC): $POUT" ;; esac
  else
    [ "$PRC" = 141 ] || fail "negative control: the old cut() did not die of SIGPIPE (exit $PRC): $POUT"
  fi
  probe "$sig" "$OLD_HAS" "$CHECK_HAS"
  [ "$PRC" = 1 ] && case "$POUT" in *'FAIL probe (no match for'*) ;; *) false ;; esac \
    || fail "negative control, SIGPIPE $sig: the old has() found the first line of 1 MB (exit $PRC): $POUT"
  probe "$sig" "$OLD_HAS" "$CHECK_HASNT"
  [ "$PRC" = 0 ] && [ "${POUT##*$'\n'}" = MISSED ] \
    || fail "negative control, SIGPIPE $sig: the old hasnt() saw the first line of 1 MB (exit $PRC): $POUT"
  probe "$sig" "$OLD_MARKERS" "$CHECK_MARKER"
  [ "$PRC" != 0 ] || fail "negative control, SIGPIPE $sig: the old has_marker found the marker in 1 MB: $POUT"
  probe "$sig" "$OLD_MARKERS" "$CHECK_CI_MARKER"
  [ "$PRC" != 0 ] || fail "negative control, SIGPIPE $sig: the old has_ci_marker found the marker in 1 MB: $POUT"
  probe "$sig" "$OLD_RUN_ID" "$CHECK_RUN_ID"
  [ "$PRC" != 0 ] || fail "negative control, SIGPIPE $sig: the old pr5_run_id read 400 KB of run ids: $POUT"
done
echo 'PASS negative control: the pipe forms fail on the same inputs (CI line with SIGPIPE ignored, 141 without), has() misses and hasnt() hides the match'

# ── 2. no pipe into an early-exiting reader under tests/ ───────────────────
# Kept on purpose: test_crosscheck.sh holds spec-pipeline.sh's old Phase 4 verbatim as its
# negative control, and this file holds the old helpers above.
cat > "$T/scan.py" <<'PY'
import re, sys
PIPE = re.compile(r'(?<![|>])\|(?!\|)&?')
GREP_EARLY = set('qlLm')
GREP_LONG = ('--quiet', '--silent', '--max-count', '--files-with-matches', '--files-without-match')
def command(rest):
    """The command after a pipe, up to the next unquoted | ; & or )."""
    out, quote = [], None
    for ch in rest.lstrip():
        if quote:
            quote = None if ch == quote else quote
        elif ch in '\'"':
            quote = ch
        elif ch in '|;&)':
            break
        out.append(ch)
    return ''.join(out)
def early(cmd):
    words = cmd.split()
    if not words:
        return None
    if words[0] == 'head':
        return 'head'
    if words[0] in ('grep', 'egrep', 'fgrep'):
        for o in words[1:]:
            if o == '--' or not o.startswith('-'):
                return None
            if o.startswith('--'):
                if o.split('=')[0] in GREP_LONG:
                    return 'grep ' + o
            elif GREP_EARLY & set(o[1:]):
                return 'grep ' + o
        return None
    if words[0] in ('awk', 'gawk', 'mawk', 'nawk') and re.search(r'\bexit\b', cmd):
        return 'awk ... exit'
    if words[0] == 'sed' and re.search(r"(?:^|[\s;'\"{0-9/$])q(?=$|[\s;'\"}0-9])", cmd[3:]):
        return 'sed ... q'
    return None
allow = set(l for l in open(sys.argv[1]).read().split('\n') if l)
hits = 0
for path in sys.argv[2:]:
    for n, line in enumerate(open(path, encoding='utf-8', errors='replace'), 1):
        line = line.rstrip('\n')
        if line.lstrip().startswith('#'):
            continue
        for m in PIPE.finditer(line):
            what = early(command(line[m.end():]))
            if what and '%s\t%s' % (path.rsplit('tests/', 1)[-1], line.strip()) not in allow:
                print('%s:%d: %s: %s' % (path, n, what, line.strip()))
                hits += 1
sys.exit(1 if hits else 0)
PY
# The scan's own check: every form here must be reported, none of the second set.
cat > "$T/flag.sh" <<'EOF'
printf '%s\n' "$1" | awk -v s="$2" '$0 ~ s { print; exit }'
x=$(cmd | head -1)
cmd | head -n 1 | cut -c1-3
cmd |head -c 80
printf '%s' "$2" | grep -qE -- "$1" || fail x
cmd | grep -q x
cmd | grep -m1 -oE x
cmd | grep -l x
cmd | grep --quiet x
cmd | egrep -qi x
cmd |& grep -q x
cmd | sed -n '1p;q'
cmd | sed 1q
EOF
cat > "$T/clean.sh" <<'EOF'
awk -v s="$2" '$0 ~ s { print; exit }' <<< "$1"
grep -qE -- "$1" <<< "$2" || fail x
x=$(cmd | sed -n 1p)
cmd | grep -c x
cmd | grep -oE x | sed -n 1p
cmd | tail -n +2
cmd || head -1
grep -q x file
cmd | sed 's/q//'
cmd | grep -v -- -q
cmd | awk '{ print }'
cmd | awk '{ print $1 }' || exit 1
# cmd | head -1
EOF
: > "$T/none"
set +e
FLAGGED=$(python3 -I "$T/scan.py" "$T/none" "$T/flag.sh"); FRC=$?
CLEAN=$(python3 -I "$T/scan.py" "$T/none" "$T/clean.sh"); CRC=$?
set -e
[ "$FRC" = 1 ] && [ "$(cut -d: -f2 <<< "$FLAGGED" | tr '\n' ' ')" = "$(seq 1 "$(grep -c . "$T/flag.sh")" | tr '\n' ' ')" ] \
  || fail "2: the scan did not report each form it must, once (exit $FRC): $FLAGGED"
[ "$CRC" = 0 ] && [ -z "$CLEAN" ] || fail "2: the scan reported a form that cannot break a pipe (exit $CRC): $CLEAN"
printf '%s\t%s\n' \
  test_crosscheck.sh 'SPEC_TASKS=$(ls -td "$BUREAU_SPECS_DIR"/*/tasks.md 2>/dev/null | head -1 || true)' \
  test_crosscheck.sh 'if echo "$CROSSCHECK_OUTPUT" | grep -q "conflicts detected"; then' > "$T/allow"
FILES=()
for f in "$ROOT"/tests/*.sh "$ROOT"/tests/lib/*.sh "$ROOT"/tests/lib/bin/*; do
  [ "$f" = "$ROOT/tests/test_pipe_early_exit.sh" ] || FILES+=("$f")
done
[ "${#FILES[@]}" -gt 50 ] || fail "2: only ${#FILES[@]} shell files found under tests/"
set +e
HITS=$(python3 -I "$T/scan.py" "$T/allow" "${FILES[@]}"); HRC=$?
set -e
[ "$HRC" = 0 ] || fail "2: a test pipes into a reader that can leave early; feed it a here-string or let it read to the end:
$HITS"
echo "PASS 2 none of ${#FILES[@]} shell files under tests/ pipes into a reader that can leave early (scan checked on $(grep -c . "$T/flag.sh") forms it must report and $(grep -c . "$T/clean.sh") it must not)"
