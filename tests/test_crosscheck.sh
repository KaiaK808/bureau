#!/bin/bash
# The spec stage's cross-check says how it ended, and only an explicit success reads as
# "no file conflicts".
#
# The old script opened with `declare -A`, which /bin/bash 3.2 on macOS rejects; the stage
# ran it with `|| true` and grepped for "conflicts detected", so every abort read as "No file
# conflicts with open PRs". This test runs the REAL crosscheck-specs.sh under /bin/bash
# against a real git fixture and a stubbed `gh`, the REAL crosscheck_open_prs from
# bureau-config.sh, and the REAL Phase 4 block cut out of spec-pipeline.sh. The negative
# control runs the old Phase 4 block against the same aborting script and shows that it
# says "No file conflicts" — so this test would have caught the bug it guards.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")" && cd .. && pwd)"
SCRIPTS="$REPO_ROOT/templates/scripts"
SB=$(mktemp -d -t bureau-test.crosscheck.XXXXXXXX)
trap 'rm -rf "$SB"' EXIT
REPO="$SB/repo"

fail() { echo "FAIL $*" >&2; [ -n "${OUT:-}" ] && printf '%s\n' "$OUT" | sed 's/^/  | /' >&2; exit 1; }

# --- fixture: a repo with an origin and four PR branches ---------------------
git init -q --bare "$SB/origin.git"
git init -q -b main "$REPO"
git -C "$REPO" config user.email test@bureau
git -C "$REPO" config user.name "Bureau Test"
git -C "$REPO" remote add origin "$SB/origin.git"
mkdir -p "$REPO/src" "$REPO/docs"
echo a > "$REPO/src/konflikt.py"; echo b > "$REPO/src/zwei.py"; echo c > "$REPO/docs/notes.md"
git -C "$REPO" add -A && git -C "$REPO" commit -q -m init && git -C "$REPO" push -q origin main
branch() {  # $1 = branch, $2 = file it changes
  git -C "$REPO" checkout -q -b "$1" main
  echo "change on $1" >> "$REPO/$2"
  git -C "$REPO" commit -q -am "change $2" && git -C "$REPO" push -q origin "$1"
  git -C "$REPO" checkout -q main
}
branch feat/eins src/konflikt.py
branch feat/zwei src/zwei.py
branch feat/drei/mit-slash docs/notes.md
branch feat/spaet src/konflikt.py
# Opened after the stage fetched: on origin, but no remote-tracking ref here.
git -C "$REPO" update-ref -d refs/remotes/origin/feat/spaet

cat > "$REPO/.bureau.json" <<'EOF'
{
  "linear": {
    "teams": [{
      "id": "team-id", "key": "EXP", "name": "Test",
      "states": {
        "triage": "s1", "spec": "s2", "spec_review": "s3", "design": "s4",
        "build": "s5", "build_review": "s6", "done": "s7"
      }
    }],
    "labels": {
      "lane2":            { "id": "l1", "name": "lane-2" },
      "needs_human":      { "id": "l2", "name": "needs-human" },
      "needs_ux":         { "id": "l3", "name": "needs-ux" },
      "ai_implementable": { "id": "l4", "name": "ai-implementable" }
    },
    "projects": []
  },
  "agents": { "poll_interval_minutes": 30, "max_review_cycles": 3 },
  "repo": { "branch_prefix": "feat", "specs_dir": "specs" }
}
EOF
echo "LINEAR_API_KEY=test-key" > "$REPO/.env"
mkdir -p "$REPO/specs/001-probe"
TASKS="$REPO/specs/001-probe/tasks.md"
printf '%s\n' '# Tasks' '- [ ] T001 Change `src/konflikt.py`' > "$TASKS"

# --- stubbed gh: prints $SB/prs.tsv, or fails with GH_STUB_RC and GH_STUB_ERR --
mkdir -p "$SB/bin"
cat > "$SB/bin/gh" <<'EOF'
#!/bin/bash
[ -z "${GH_STUB_ERR:-}" ] || printf '%s\n' "$GH_STUB_ERR" >&2
[ "${GH_STUB_RC:-0}" -eq 0 ] || exit "$GH_STUB_RC"
cat "$GH_STUB_TSV"
EOF
chmod +x "$SB/bin/gh"
export GH_STUB_TSV="$SB/prs.tsv"
prs() { if [ "$#" -eq 0 ]; then : > "$GH_STUB_TSV"; else printf '%s\n' "$@" > "$GH_STUB_TSV"; fi; }

# --- runners -----------------------------------------------------------------
xcheck() {  # run the real script under /bin/bash in the fixture; sets OUT and RC
  set +e
  OUT=$(cd "$REPO" && PATH="$SB/bin:$PATH" /bin/bash "$SCRIPTS/crosscheck-specs.sh" "$@" 2>&1)
  RC=$?
  set -e
}
last_line() { printf '%s\n' "$OUT" | awk 'NF { l = $0 } END { print l }'; }
expect() {  # $1 = rc, $2 = result line, $3 = label
  [ "$RC" = "$1" ] || fail "$3: exit $RC, wanted $1"
  [ "$(last_line)" = "$2" ] || fail "$3: last line '$(last_line)', wanted '$2'"
}

# Runs a snippet the way the spec stage does: /bin/bash, set -euo pipefail, the real config
# sourced, post_comment recording into $SB/posts (one line per call) and $SB/post-body.
stage() {
  rm -f "$SB/posts" "$SB/post-body"
  set +e
  OUT=$(cd "$REPO" && PATH="$SB/bin:$PATH" /bin/bash -c "
    set -euo pipefail
    source '$SCRIPTS/bureau-config.sh'
    post_comment() { printf '%s\n' \"\$1\" >> '$SB/posts'; printf '%s\n' \"\$2\" > '$SB/post-body'; return \${POST_RC:-0}; }
    ISSUE=EXP-1
    $1
    echo \"STAGE CONTINUES result=\${CROSSCHECK_RESULT:-unset}\"
  " 2>&1)
  RC=$?
  set -e
}
posts() { if [ -f "$SB/posts" ]; then wc -l < "$SB/posts" | tr -d ' '; else echo 0; fi; }
fake_script() {  # $1 = body; installs $SB/fake/crosscheck-specs.sh for the evaluation
  mkdir -p "$SB/fake"
  printf '#!/bin/bash\n%s\n' "$1" > "$SB/fake/crosscheck-specs.sh"
}

# --- the script ----------------------------------------------------------------
prs $'11\tfeat/eins\tEins'
xcheck "$TASKS"
expect 3 "CROSSCHECK RESULT: conflicts open=1 compared=1 paths=1 unchecked=-" "conflict"
case "$OUT" in *"PR #11: Eins"*'`feat/eins`'*"- src/konflikt.py"*) ;; *) fail "conflict report lacks PR, branch or file" ;; esac
echo "PASS a conflict names PR, branch and file, exit 3"

prs $'12\tfeat/zwei\tZwei' $'13\tfeat/drei/mit-slash\tDrei' $'14\tfeat/zwei\tZwei again'
xcheck "$TASKS"
expect 0 "CROSSCHECK RESULT: clean open=3 compared=3 paths=1 unchecked=-" "three clean PRs"
echo "PASS three open PRs without overlap run through under /bin/bash and count as compared"

prs
xcheck "$TASKS"
expect 0 "CROSSCHECK RESULT: clean open=0 compared=0 paths=1 unchecked=-" "no PRs"
echo "PASS no open PR is clean, and the planned paths were still read"

GH_STUB_RC=1 GH_STUB_ERR="HTTP 401: Bad credentials" xcheck "$TASKS"
expect 4 "CROSSCHECK RESULT: incomplete open=0 compared=0 paths=1 unchecked=-" "gh fails"
case "$OUT" in *"could not be listed"*"gh: HTTP 401"*) ;; *) fail "gh failure is not reported with its message" ;; esac
case "$OUT" in *"PR #"*) fail "a gh error message turned into a PR line" ;; esac
echo "PASS an unreadable PR list is incomplete, never zero PRs"

prs $'31\tfeat/nirgends\tGibt es nicht' $'32\tfeat/eins\tEins'
xcheck "$TASKS"
expect 4 "CROSSCHECK RESULT: incomplete open=2 compared=1 paths=1 unchecked=#31" "one PR unreadable"
case "$OUT" in *"PR #32: Eins"*"src/konflikt.py"*"Not checked"*"PR #31"*) ;; *) fail "the readable conflict or the unchecked PR is missing" ;; esac
echo "PASS an unreadable PR is named and the others still report"

prs $'41\tfeat/spaet\tSpaet'
xcheck "$TASKS"
expect 3 "CROSSCHECK RESULT: conflicts open=1 compared=1 paths=1 unchecked=-" "branch fetched late"
git -C "$REPO" rev-parse --verify --quiet refs/remotes/origin/feat/spaet >/dev/null \
  || fail "the missing PR branch was not fetched"
echo "PASS a PR branch missing locally is fetched and compared"

# shellcheck disable=SC2016  # the title must stay literal: it is the payload
prs $'51\tfeat/eins\t$(touch '"$SB"$'/pwned) `x` \\n'
xcheck "$TASKS"
expect 3 "CROSSCHECK RESULT: conflicts open=1 compared=1 paths=1 unchecked=-" "hostile title"
[ ! -e "$SB/pwned" ] || fail "a PR title was executed"
case "$OUT" in *'$(touch '*'\n'*) ;; *) fail "the PR title was not reported literally" ;; esac
echo "PASS a PR title is text: reported literally, never evaluated"

xcheck "$SB/does-not-exist.md"
expect 4 "CROSSCHECK RESULT: incomplete open=1 compared=1 paths=0 unchecked=-" "missing tasks file"
echo "PASS a missing tasks file is incomplete"

# --- the evaluation: crosscheck_open_prs --------------------------------------
prs $'11\tfeat/eins\tEins'
stage 'crosscheck_open_prs "$ISSUE" "'"$TASKS"'"'
[ "$RC" = 0 ] || fail "evaluation ended the stage on a conflict (exit $RC)"
case "$OUT" in *"STAGE CONTINUES result=conflicts"*) ;; *) fail "conflict not classified as conflicts" ;; esac
[ "$(posts)" = 1 ] && grep -q "Crosscheck warning" "$SB/post-body" || fail "conflict did not post exactly one warning"
echo "PASS a real conflict reaches the ticket as the warning, and the stage continues"

prs $'12\tfeat/zwei\tZwei'
stage 'crosscheck_open_prs "$ISSUE" "'"$TASKS"'"'
case "$OUT" in *"No file conflicts with open PRs (1 PRs compared, 1 planned paths)"*"result=clean"*) ;; *) fail "clean run not reported with counts" ;; esac
[ "$(posts)" = 0 ] || fail "a clean run posted a comment"
echo "PASS an explicit clean says so with counts and posts nothing"

check_incomplete() {  # $1 = fake script body, $2 = label
  fake_script "$1"
  stage '_BUREAU_SCRIPTS_DIR='"$SB"'/fake; crosscheck_open_prs "$ISSUE" "'"$TASKS"'"'
  [ "$RC" = 0 ] || fail "$2: evaluation ended the stage (exit $RC)"
  case "$OUT" in *"STAGE CONTINUES result=incomplete"*) ;; *) fail "$2: not classified as incomplete" ;; esac
  case "$OUT" in *"No file conflicts"*) fail "$2: said 'No file conflicts'" ;; esac
  [ "$(posts)" = 1 ] && grep -q "Crosscheck incomplete" "$SB/post-body" || fail "$2: did not post exactly one incomplete warning"
}
check_incomplete 'echo "declare: -A: invalid option"; exit 2' "abort without result line"
check_incomplete 'exit 0' "empty output with exit 0"
check_incomplete 'echo "CROSSCHECK RESULT: conflicts open=1 compared=1 paths=1 unchecked=-"; exit 0' "exit 0 with the word conflicts"
check_incomplete 'echo "CROSSCHECK RESULT: clean open=1 compared=1 paths=1 unchecked=-"; exit 7' "unknown exit code with the word clean"
check_incomplete 'echo "CROSSCHECK RESULT: clean open=1 compared=1 paths=1 unchecked=-"; echo "trailing"; exit 0' "result line not last"
echo "PASS every pairing other than 0+clean and 3+conflicts is incomplete, posts once, and never says no conflicts"

rm -f "$SB/fake/crosscheck-specs.sh"
stage '_BUREAU_SCRIPTS_DIR='"$SB"'/fake; crosscheck_open_prs "$ISSUE" "'"$TASKS"'"'
case "$OUT" in *"exit 127"*"result=incomplete"*) ;; *) fail "a missing script is not incomplete with exit 127" ;; esac
echo "PASS a missing script is incomplete with exit 127"

POST_RC=1 stage 'crosscheck_open_prs "$ISSUE" "'"$SB"'/does-not-exist.md"'
[ "$RC" = 0 ] || fail "a failing post_comment ended the stage (exit $RC)"
case "$OUT" in *"could NOT be posted"*"STAGE CONTINUES result=incomplete"*) ;; *) fail "a failing comment is not reported" ;; esac
echo "PASS a failing comment is reported and does not end the stage"

# --- the stage: the real Phase 4 block, and the old one as negative control ---
cut_phase4() {  # stdin = a spec-pipeline.sh; prints Phase 4 from its SPEC_TASKS line to its closing fi
  awk '/^echo "Phase 4\/5: crosscheck"$/ { f = 1; next } f { print } f && /^fi$/ { exit }'
}
NEW_BLOCK=$(cut_phase4 < "$SCRIPTS/spec-pipeline.sh")
case "$NEW_BLOCK" in
  *'SPEC_TASKS='*'crosscheck_open_prs "$ISSUE" "$SPEC_TASKS"'*'fi') ;;
  *) fail "Phase 4 in spec-pipeline.sh no longer has the expected shape: $NEW_BLOCK" ;;
esac
# The form this change replaced, kept verbatim as the broken version.
OLD_BLOCK=$(cut_phase4 <<'EOF'
echo "Phase 4/5: crosscheck"
SPEC_TASKS=$(ls -td "$BUREAU_SPECS_DIR"/*/tasks.md 2>/dev/null | head -1 || true)
if [ -n "$SPEC_TASKS" ]; then
  CROSSCHECK_OUTPUT=$(./scripts/crosscheck-specs.sh "$SPEC_TASKS" 2>&1 || true)
  echo "$CROSSCHECK_OUTPUT"
  if echo "$CROSSCHECK_OUTPUT" | grep -q "conflicts detected"; then
    post_comment "$ISSUE" "⚠️ Crosscheck warning — spec conflicts with open PRs:

\`\`\`
$CROSSCHECK_OUTPUT
\`\`\`"
  else
    echo "  No file conflicts with open PRs"
  fi
else
  echo "  No tasks.md found — skipping crosscheck"
fi
EOF
)

# The abort the old script produced on /bin/bash 3.2, as the script both blocks will run.
ABORT='echo "crosscheck-specs.sh: line 13: declare: -A: invalid option"; exit 2'
fake_script "$ABORT"
mkdir -p "$REPO/scripts"
cp "$SB/fake/crosscheck-specs.sh" "$REPO/scripts/crosscheck-specs.sh"
chmod +x "$REPO/scripts/crosscheck-specs.sh"

stage '_BUREAU_SCRIPTS_DIR='"$SB"'/fake
'"$NEW_BLOCK"
[ "$RC" = 0 ] || fail "the real Phase 4 block ended the stage on an abort (exit $RC)"
case "$OUT" in *"WARNING: crosscheck incomplete (exit 2)"*"STAGE CONTINUES"*) ;; *) fail "the real Phase 4 block did not warn on an abort" ;; esac
case "$OUT" in *"No file conflicts"*) fail "the real Phase 4 block said 'No file conflicts' after an abort" ;; esac
[ "$(posts)" = 1 ] || fail "the real Phase 4 block did not post the incomplete warning"
echo "PASS the real Phase 4 block turns an abort into one warning and the stage goes on"

stage "$OLD_BLOCK"
case "$OUT" in *"No file conflicts with open PRs"*) ;; *) fail "negative control: the old block no longer shows the bug, so this test proves nothing" ;; esac
[ "$(posts)" = 0 ] || fail "negative control: the old block posted something"
echo "PASS negative control: the old Phase 4 block reports 'No file conflicts' after the same abort"

# The new block against a real conflict, end to end: script, evaluation and stage.
prs $'11\tfeat/eins\tEins'
stage "$NEW_BLOCK"
case "$OUT" in *"CROSSCHECK RESULT: conflicts"*"File conflicts with open PRs — warning posted to EXP-1"*) ;; *) fail "the real Phase 4 block missed a real conflict" ;; esac
echo "PASS the real Phase 4 block reports a real conflict end to end"
