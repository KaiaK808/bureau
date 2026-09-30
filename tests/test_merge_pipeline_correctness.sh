#!/bin/bash
# Regression test for merge-pipeline.sh's NRSR-enforcement gates.
#
# Scenarios:
#   happy        — all gates pass; gh pr merge IS called, ticket → Done
#   stale_base   — PR baseRefOid != main HEAD; merge REFUSED, blocker surfaces
#                  the "N commits behind" message
#   ci_red       — one check-run has conclusion=failure; merge REFUSED, blocker
#                  surfaces the failing check name
#   ci_pending   — one check-run has status=in_progress; merge REFUSED
#   jit_race     — initial gate eval is clean but the JIT recheck sees main has
#                  moved between the two evaluations; merge REFUSED
#
# Harness shape mirrors test_shepherd.sh: each scenario builds a fresh sandbox,
# layers a stub `gh` on PATH that reads scripted JSON from $STUB_DIR, runs the
# REAL merge-pipeline.sh against a REAL bureau-config.sh (whose Linear-glue
# functions are overridden by appended test stubs).
#
# Each gh call appends to gh_invocations.log. Merges write to merge_calls.log.
# Tests assert on those logs + pipeline stdout.

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "$0")" && cd .. && pwd)"
REAL_BUREAU_CONFIG="$REPO_ROOT/templates/scripts/bureau-config.sh"
REAL_MERGE_PIPELINE="$REPO_ROOT/templates/scripts/merge-pipeline.sh"
SANDBOX_ROOT=$(mktemp -d -t bureau-test.merge-corr.XXXXXXXX)
trap 'rm -rf "$SANDBOX_ROOT"' EXIT

# ── Sandbox construction ──────────────────────────────────────────
make_sandbox() {
  local name="$1"
  local sb="$SANDBOX_ROOT/$name"
  mkdir -p "$sb/scripts" "$sb/bin" "$sb/stub_data"

  echo "LINEAR_API_KEY=stub" > "$sb/.env"

  cat > "$sb/.bureau.json" <<'EOF'
{
  "linear": {
    "teams": [{
      "id":"t","key":"EXP","name":"T",
      "states": {
        "triage":"s1","spec":"s2","spec_review":"s3","design":"s4",
        "build":"s5","build_review":"s6","merge":"s7","done":"s8"
      }
    }],
    "labels": {
      "lane2":{"id":"l1","name":"lane-2"},
      "needs_human":{"id":"l2","name":"needs-human"},
      "needs_ux":{"id":"l3","name":"needs-ux"},
      "ai_implementable":{"id":"l4","name":"ai-implementable"}
    },
    "projects": []
  },
  "agents": {
    "merge": true,
    "merge_strategy": "squash",
    "poll_interval_minutes": 30,
    "max_review_cycles": 3
  },
  "repo": {"branch_prefix": "feat", "specs_dir": "specs"}
}
EOF

  # The real config sources bureau-env.sh next to itself; the pipelines read their
  # .env through it instead of sourcing the file (tests/test_env_read_safety.sh).
  cp "$REPO_ROOT/templates/scripts/bureau-env.sh" "$sb/scripts/"
  # merge-pipeline.sh sources merge-body.sh next to itself for the merge message.
  cp "$REPO_ROOT/templates/scripts/merge-body.sh" "$sb/scripts/"
  cp "$REAL_BUREAU_CONFIG"  "$sb/scripts/bureau-config.sh"
  cp "$REAL_MERGE_PIPELINE" "$sb/scripts/merge-pipeline.sh"

  # Append test overrides for Linear glue. These come AFTER the real
  # definitions so they win at function-resolution time. pr_ci_is_green
  # and pr_base_is_current (the helpers under test) stay as the real
  # implementations.
  cat >> "$sb/scripts/bureau-config.sh" <<'OVERRIDES'

# ── TEST OVERRIDES ─────────────────────────────────────────────────
precondition_linear()      { echo precondition_linear >> "$STUB_DIR/linear.log"; return 0; }
bureau_stage_enter()       { :; } # ownership covered by runtime tests
precondition_claude_auth() { return 0; }
post_comment()             { echo "post_comment $1 :: $2" >> "$STUB_DIR/comments_posted.log"; }
move_issue()               { echo "move_issue $1 -> $2"   >> "$STUB_DIR/state_changes.log"; }
add_issue_label()          { echo "+$1 $2" >> "$STUB_DIR/labels.log"; }
remove_issue_label()       { echo "-$1 $2" >> "$STUB_DIR/labels.log"; }
get_issue_branch()         { echo get_issue_branch >> "$STUB_DIR/linear.log"; echo "feat/test"; }
pipeline_pick_next()       { echo pipeline_pick_next >> "$STUB_DIR/linear.log"; echo "EXP-1"; }
alert_telegram()           { :; }
_bureau_gh_owner_repo()    { echo "test-owner/test-repo"; }
OVERRIDES

  # gh stub. Routes by argv prefix, returns canned JSON from $STUB_DIR, and
  # honors gh's --json/--jq projection. Records every call.
  cat > "$sb/bin/gh" <<'GHEOF'
#!/bin/bash
set -uo pipefail

{ printf 'gh'; for a in "$@"; do printf ' %q' "$a"; done; printf '\n'; } \
  >> "${INVOCATIONS_LOG:-/dev/null}"

# Extract --jq and --json values from argv (linear scan; bash 3.2 friendly).
JQ_FILTER=""
JSON_FIELDS=""
prev=""
for a in "$@"; do
  case "$prev" in
    --jq)   JQ_FILTER="$a";   prev=""; continue ;;
    --json) JSON_FIELDS="$a"; prev=""; continue ;;
  esac
  prev="$a"
done

apply_jq() {
  if [ -n "$JQ_FILTER" ]; then jq -r "$JQ_FILTER"; else cat; fi
}

project_and_filter() {
  if [ -z "$JSON_FIELDS" ]; then
    apply_jq
    return
  fi
  local proj="{" first=1 f
  local OLDIFS="$IFS"; IFS=,
  for f in $JSON_FIELDS; do
    [ $first -eq 0 ] && proj+=","
    proj+="\"$f\":.$f"
    first=0
  done
  IFS="$OLDIFS"
  proj+="}"
  jq "$proj" | apply_jq
}

case "${1:-}" in
  repo)
    case "${2:-}" in
      view) echo '{"nameWithOwner":"test-owner/test-repo"}' | apply_jq ;;
    esac
    ;;
  pr)
    case "${2:-}" in
      list)
        # Branch on argv: the ghost-merge path passes `--state merged`, the
        # default open-PR lookup does not. Per-scenario JSON fixtures override
        # the defaults so most scenarios can ignore this stub entirely.
        is_merged_query=0
        for a in "$@"; do
          case "$a" in
            merged) is_merged_query=1 ;;
          esac
        done
        if [ "$is_merged_query" -eq 1 ]; then
          if [ -f "$STUB_DIR/pr_list_merged.json" ]; then
            cat "$STUB_DIR/pr_list_merged.json" | apply_jq
          else
            echo '[]' | apply_jq
          fi
        else
          if [ -f "$STUB_DIR/pr_list_open.json" ]; then
            cat "$STUB_DIR/pr_list_open.json" | apply_jq
          else
            echo '[{"number":42}]' | apply_jq
          fi
        fi
        ;;
      view)
        # The gate's own reads can be made to fail one at a time (v3.0.1).
        if [ "$JSON_FIELDS" = "state,mergeStateStatus,labels" ] && [ -f "$STUB_DIR/fail_pr_gate_read" ]; then
          echo "HTTP 502" >&2; exit 1
        fi
        if [ "$JSON_FIELDS" = "comments" ] && [ -f "$STUB_DIR/fail_comments_read" ]; then
          echo "HTTP 502" >&2; exit 1
        fi
        # The merge message read (--json title,body) can be made to fail on its own.
        if [ "$JSON_FIELDS" = "title,body" ] && [ -f "$STUB_DIR/fail_title_body" ]; then
          echo "HTTP 502" >&2
          exit 1
        fi
        cat "$STUB_DIR/pr_view.json" | project_and_filter
        ;;
      merge)
        echo "gh pr merge ${3:-?}" >> "$STUB_DIR/merge_calls.log"
        # Full argv, NUL-separated: the body is multi-line.
        printf '%s\0' "$@" > "$STUB_DIR/merge_argv"
        # Flip PR state to MERGED so any further view sees the new world.
        if [ -f "$STUB_DIR/pr_view.json" ]; then
          jq '.state = "MERGED"' "$STUB_DIR/pr_view.json" > "$STUB_DIR/pr_view.json.tmp" \
            && mv "$STUB_DIR/pr_view.json.tmp" "$STUB_DIR/pr_view.json"
        fi
        exit 0
        ;;
      comment)
        # Capture --body value
        prev=""
        for a in "$@"; do
          if [ "$prev" = "--body" ]; then
            echo "$a" >> "$STUB_DIR/comments_posted.log"
            break
          fi
          prev="$a"
        done
        ;;
    esac
    ;;
  api)
    path_arg="${2:-}"
    case "$path_arg" in
      graphql) if [ -f "$STUB_DIR/fail_threads_read" ]; then echo "HTTP 502" >&2; exit 1; fi
               cat "$STUB_DIR/review_threads.json" | apply_jq ;;
      */commits/*/check-runs) cat "$STUB_DIR/check_runs.json" | apply_jq ;;
      */commits/*/status)     cat "$STUB_DIR/status.json"     | apply_jq ;;
      */branches/*)
        # Counter-based fixture selection so a scenario can flip the answer
        # between the first eval (initial gate) and the second (JIT recheck).
        tick_file="$STUB_DIR/branch_tick"
        tick=$(cat "$tick_file" 2>/dev/null || echo 0)
        tick=$((tick + 1))
        echo "$tick" > "$tick_file"
        if [ -f "$STUB_DIR/branch_main_${tick}.json" ]; then
          cat "$STUB_DIR/branch_main_${tick}.json" | apply_jq
        else
          cat "$STUB_DIR/branch_main.json" | apply_jq
        fi
        ;;
      */compare/*) cat "$STUB_DIR/compare.json" | apply_jq ;;
    esac
    ;;
esac
GHEOF
  chmod +x "$sb/bin/gh"
  echo "$sb"
}

# ── Default fixture data (a "would-merge" PR) ────────────────────
populate_happy_fixtures() {
  local sd="$1/stub_data"
  cat > "$sd/pr_view.json" <<'EOF'
{
  "state":"OPEN",
  "title":"EXP-1: test PR",
  "body":"Summary of the change.",
  "mergeStateStatus":"CLEAN",
  "labels":[],
  "url":"https://github.com/test-owner/test-repo/pull/42",
  "headRefName":"feat/test",
  "headRefOid":"HEAD_SHA",
  "baseRefName":"main",
  "baseRefOid":"MAIN_SHA",
  "comments":[
    {"createdAt":"2026-05-13T00:00:00Z","body":"## Code Review v2 — EXP-1\n**Verdict**: APPROVE"}
  ]
}
EOF
  cat > "$sd/check_runs.json" <<'EOF'
{"check_runs":[{"name":"ci","status":"completed","conclusion":"success"}]}
EOF
  echo '{"statuses":[]}'                                       > "$sd/status.json"
  echo '{"commit":{"sha":"MAIN_SHA"}}'                         > "$sd/branch_main.json"
  echo '{"ahead_by":0}'                                        > "$sd/compare.json"
  echo '{"data":{"repository":{"pullRequest":{"reviewThreads":{"nodes":[]}}}}}' > "$sd/review_threads.json"
}

run_pipeline() {
  local sb="$1"; shift
  STUB_DIR="$sb/stub_data" \
    INVOCATIONS_LOG="$sb/gh_invocations.log" \
    PATH="$sb/bin:$PATH" \
    bash "$sb/scripts/merge-pipeline.sh" "$@" \
    > "$sb/pipeline.out" 2> "$sb/pipeline.err"
}

# ── Scenarios ─────────────────────────────────────────────────────

test_happy_path() {
  local sb; sb=$(make_sandbox happy)
  populate_happy_fixtures "$sb"
  run_pipeline "$sb"

  if [ ! -s "$sb/stub_data/merge_calls.log" ]; then
    echo "FAIL happy: gh pr merge was NOT called" >&2
    sed 's/^/  | /' "$sb/pipeline.out" >&2
    return 1
  fi
  if ! grep -q "move_issue EXP-1 -> s8" "$sb/stub_data/state_changes.log" 2>/dev/null; then
    echo "FAIL happy: ticket did not move to Done (state s8)" >&2
    return 1
  fi
  return 0
}

test_stale_base() {
  local sb; sb=$(make_sandbox stale_base)
  populate_happy_fixtures "$sb"
  echo '{"commit":{"sha":"MAIN_SHA_NEW"}}' > "$sb/stub_data/branch_main.json"
  echo '{"ahead_by":3}'                    > "$sb/stub_data/compare.json"
  run_pipeline "$sb"

  if [ -s "$sb/stub_data/merge_calls.log" ]; then
    echo "FAIL stale_base: gh pr merge WAS called despite stale base" >&2
    return 1
  fi
  if ! grep -q "behind main" "$sb/pipeline.out"; then
    echo "FAIL stale_base: 'behind main' blocker not surfaced in pipeline output" >&2
    sed 's/^/  | /' "$sb/pipeline.out" >&2
    return 1
  fi
  return 0
}

test_ci_red() {
  local sb; sb=$(make_sandbox ci_red)
  populate_happy_fixtures "$sb"
  cat > "$sb/stub_data/check_runs.json" <<'EOF'
{"check_runs":[
  {"name":"build","status":"completed","conclusion":"success"},
  {"name":"test (e2e)","status":"completed","conclusion":"failure"}
]}
EOF
  run_pipeline "$sb"

  if [ -s "$sb/stub_data/merge_calls.log" ]; then
    echo "FAIL ci_red: gh pr merge WAS called despite red CI" >&2
    return 1
  fi
  if ! grep -q "test (e2e)" "$sb/pipeline.out"; then
    echo "FAIL ci_red: failing check name 'test (e2e)' not surfaced" >&2
    sed 's/^/  | /' "$sb/pipeline.out" >&2
    return 1
  fi
  return 0
}

test_ci_pending() {
  local sb; sb=$(make_sandbox ci_pending)
  populate_happy_fixtures "$sb"
  cat > "$sb/stub_data/check_runs.json" <<'EOF'
{"check_runs":[
  {"name":"build","status":"completed","conclusion":"success"},
  {"name":"test","status":"in_progress","conclusion":null}
]}
EOF
  run_pipeline "$sb"

  if [ -s "$sb/stub_data/merge_calls.log" ]; then
    echo "FAIL ci_pending: gh pr merge WAS called despite pending CI" >&2
    return 1
  fi
  if ! grep -q "pending" "$sb/pipeline.out"; then
    echo "FAIL ci_pending: 'pending' blocker not surfaced" >&2
    sed 's/^/  | /' "$sb/pipeline.out" >&2
    return 1
  fi
  return 0
}

test_ghost_merge() {
  local sb; sb=$(make_sandbox ghost_merge)
  populate_happy_fixtures "$sb"
  # No open PR for the bureau-tracked branch; a merged PR exists matching
  # both the issue ID and headRefName. Pipeline must bump to Done, exit 0,
  # post a recovery comment, and skip gh pr merge.
  echo '[]' > "$sb/stub_data/pr_list_open.json"
  cat > "$sb/stub_data/pr_list_merged.json" <<'EOF'
[
  {"number":42,"headRefName":"feat/test","mergedAt":"2026-05-12T10:00:00Z","mergeCommit":{"oid":"deadbeef"}}
]
EOF
  run_pipeline "$sb"

  if [ -s "$sb/stub_data/merge_calls.log" ]; then
    echo "FAIL ghost_merge: gh pr merge WAS called for an already-merged PR" >&2
    return 1
  fi
  if ! grep -q "move_issue EXP-1 -> s8" "$sb/stub_data/state_changes.log" 2>/dev/null; then
    echo "FAIL ghost_merge: ticket did not move to Done (state s8)" >&2
    sed 's/^/  | /' "$sb/pipeline.out" >&2
    return 1
  fi
  if ! grep -q "was merged at" "$sb/stub_data/comments_posted.log" 2>/dev/null; then
    echo "FAIL ghost_merge: recovery comment not posted" >&2
    sed 's/^/  | /' "$sb/stub_data/comments_posted.log" >&2
    return 1
  fi
  return 0
}

test_ghost_merge_bare_branch() {
  local sb; sb=$(make_sandbox ghost_bare)
  populate_happy_fixtures "$sb"
  # No open PR AND no merged PR matching the issue. This is the genuine
  # "bare branch, no PR ever existed" case — must still exit 15 and NOT
  # move the ticket.
  echo '[]' > "$sb/stub_data/pr_list_open.json"
  echo '[]' > "$sb/stub_data/pr_list_merged.json"
  set +e
  run_pipeline "$sb"
  rc=$?
  set -e

  if [ "$rc" -ne 15 ]; then
    echo "FAIL ghost_bare: expected exit 15, got $rc" >&2
    sed 's/^/  | /' "$sb/pipeline.out" >&2
    return 1
  fi
  if [ -s "$sb/stub_data/merge_calls.log" ]; then
    echo "FAIL ghost_bare: gh pr merge WAS called for a bare branch" >&2
    return 1
  fi
  if grep -q "move_issue EXP-1 -> s8" "$sb/stub_data/state_changes.log" 2>/dev/null; then
    echo "FAIL ghost_bare: ticket was wrongly moved to Done" >&2
    return 1
  fi
  return 0
}

test_ghost_merge_branch_mismatch() {
  local sb; sb=$(make_sandbox ghost_mismatch)
  populate_happy_fixtures "$sb"
  # A merged PR mentions the issue (cross-reference) but its headRefName
  # does NOT match the bureau-tracked branch. Must NOT auto-promote.
  echo '[]' > "$sb/stub_data/pr_list_open.json"
  cat > "$sb/stub_data/pr_list_merged.json" <<'EOF'
[
  {"number":99,"headRefName":"feat/some-other-branch","mergedAt":"2026-05-12T10:00:00Z","mergeCommit":{"oid":"cafef00d"}}
]
EOF
  set +e
  run_pipeline "$sb"
  rc=$?
  set -e

  if [ "$rc" -ne 15 ]; then
    echo "FAIL ghost_mismatch: expected exit 15, got $rc" >&2
    sed 's/^/  | /' "$sb/pipeline.out" >&2
    return 1
  fi
  if grep -q "move_issue EXP-1 -> s8" "$sb/stub_data/state_changes.log" 2>/dev/null; then
    echo "FAIL ghost_mismatch: ticket wrongly promoted on cross-reference match" >&2
    return 1
  fi
  return 0
}

test_jit_race() {
  local sb; sb=$(make_sandbox jit_race)
  populate_happy_fixtures "$sb"
  # First /branches/main call (initial gate) reports the matching SHA;
  # second call (JIT recheck) reports a different SHA — main moved.
  echo '{"commit":{"sha":"MAIN_SHA"}}'             > "$sb/stub_data/branch_main_1.json"
  echo '{"commit":{"sha":"MAIN_SHA_AFTER_RACE"}}'  > "$sb/stub_data/branch_main_2.json"
  echo '{"ahead_by":1}'                            > "$sb/stub_data/compare.json"
  run_pipeline "$sb"

  if [ -s "$sb/stub_data/merge_calls.log" ]; then
    echo "FAIL jit_race: gh pr merge WAS called despite mid-run race" >&2
    sed 's/^/  | /' "$sb/pipeline.out" >&2
    return 1
  fi
  if ! grep -q "Gate regressed" "$sb/pipeline.out"; then
    echo "FAIL jit_race: 'Gate regressed' diagnostic missing" >&2
    sed 's/^/  | /' "$sb/pipeline.out" >&2
    return 1
  fi
  return 0
}

# ── Merge message: subject and body are set, and carry no CI suppressor ──
# merge_arg <sb> <flag>: the value that followed <flag> in the recorded `gh pr merge` argv.
merge_arg() {
  local want="$2" prev="" a
  [ -f "$1/stub_data/merge_argv" ] || return 1
  while IFS= read -r -d '' a; do
    if [ "$prev" = "$want" ]; then printf '%s' "$a"; return 0; fi
    prev="$a"
  done < "$1/stub_data/merge_argv"
  return 1
}
has_ci_marker() {
  printf '%s' "$1" | grep -qiE '\[(skip ci|ci skip|no ci|skip actions|actions skip)\]|skip-checks[[:space:]]*:'
}
# merge_message_is_clean <sb>: 0 when the merge carried a marker-free --subject and --body.
merge_message_is_clean() {
  local subject body
  subject=$(merge_arg "$1" --subject) || { echo "no --subject passed to gh pr merge" >&2; return 1; }
  body=$(merge_arg "$1" --body) || { echo "no --body passed to gh pr merge" >&2; return 1; }
  if has_ci_marker "$subject" || has_ci_marker "$body"; then
    echo "a CI suppressor reached the merge message: subject='$subject' body='$body'" >&2
    return 1
  fi
  [ "$subject" = "EXP-1: fix the thing (skip ci)" ] || { echo "unexpected subject '$subject'" >&2; return 1; }
  case "$body" in *"(CI SKIP)"*"skip checks: true"*) ;; *) echo "unexpected body '$body'" >&2; return 1 ;; esac
}
populate_marker_fixtures() {
  populate_happy_fixtures "$1"
  jq '.title = "EXP-1: fix the thing [skip ci]" | .body = "Summary [CI SKIP]\n\nskip-checks: true\n"' \
    "$1/stub_data/pr_view.json" > "$1/stub_data/pr_view.json.tmp" \
    && mv "$1/stub_data/pr_view.json.tmp" "$1/stub_data/pr_view.json"
}

test_merge_message_defanged() {
  local sb; sb=$(make_sandbox merge_message)
  populate_marker_fixtures "$sb"
  run_pipeline "$sb"
  if ! merge_message_is_clean "$sb"; then
    echo "FAIL merge_message: the merge did not carry a clean subject and body" >&2
    sed 's/^/  | /' "$sb/pipeline.out" >&2
    return 1
  fi
  # Negative control: the same fixture through the merge call this change replaced.
  local old; old=$(make_sandbox merge_message_old)
  populate_marker_fixtures "$old"
  perl -0pi -e 's/if _merge_pr; then/if gh pr merge "\$PR_NUMBER" "--\$BUREAU_MERGE_STRATEGY"; then/' \
    "$old/scripts/merge-pipeline.sh"
  grep -q 'if gh pr merge "$PR_NUMBER" "--$BUREAU_MERGE_STRATEGY"; then' "$old/scripts/merge-pipeline.sh" \
    || { echo "FAIL merge_message: could not build the negative control" >&2; return 1; }
  run_pipeline "$old"
  [ -s "$old/stub_data/merge_calls.log" ] || { echo "FAIL merge_message: negative control did not merge at all" >&2; return 1; }
  if merge_message_is_clean "$old" 2>/dev/null; then
    echo "FAIL merge_message: the check also passes the old merge call, so it proves nothing" >&2
    return 1
  fi
  return 0
}

test_merge_message_read_fails() {
  local sb; sb=$(make_sandbox merge_read_fails)
  populate_happy_fixtures "$sb"
  touch "$sb/stub_data/fail_title_body"
  run_pipeline "$sb"
  local rc=$?
  if [ -s "$sb/stub_data/merge_calls.log" ]; then
    echo "FAIL merge_read_fails: merged with GitHub's default message after the title/body read failed" >&2
    return 1
  fi
  [ "$rc" -eq 18 ] || { echo "FAIL merge_read_fails: exit $rc, wanted 18" >&2; return 1; }
  grep -q '+EXP-1 needs-human' "$sb/stub_data/labels.log" 2>/dev/null \
    || { echo "FAIL merge_read_fails: needs-human was not set" >&2; return 1; }
  return 0
}

test_merge_rebase_stays_plain() {
  local sb; sb=$(make_sandbox merge_rebase)
  populate_marker_fixtures "$sb"
  jq '.agents.merge_strategy = "rebase"' "$sb/.bureau.json" > "$sb/.bureau.json.tmp" && mv "$sb/.bureau.json.tmp" "$sb/.bureau.json"
  run_pipeline "$sb"
  [ -s "$sb/stub_data/merge_calls.log" ] || { echo "FAIL merge_rebase: no merge" >&2; sed 's/^/  | /' "$sb/pipeline.out" >&2; return 1; }
  if merge_arg "$sb" --body >/dev/null; then
    echo "FAIL merge_rebase: --body passed to a rebase merge, which gh rejects" >&2
    return 1
  fi
  return 0
}

# ── agents.merge_mode ─────────────────────────────────────────────
set_merge_mode() {  # $1 = sandbox, $2 = a JSON value for .agents.merge_mode
  jq --argjson v "$2" '.agents.merge_mode = $v' "$1/.bureau.json" > "$1/.bureau.json.tmp" && mv "$1/.bureau.json.tmp" "$1/.bureau.json"
}

# Nothing ran: no gh call at all, no Linear helper, no state, label or comment.
assert_untouched() {  # $1 = sandbox, $2 = label
  local sb="$1" f
  if [ -s "$sb/gh_invocations.log" ]; then
    echo "FAIL $2: gh was called:" >&2; sed 's/^/  | /' "$sb/gh_invocations.log" >&2; return 1
  fi
  for f in linear.log state_changes.log labels.log comments_posted.log merge_calls.log; do
    if [ -s "$sb/stub_data/$f" ]; then
      echo "FAIL $2: $f is not empty:" >&2; sed 's/^/  | /' "$sb/stub_data/$f" >&2; return 1
    fi
  done
}

# manual: the queue, a named ticket and the review stage's inline call all refuse with
# exit 2 before .env, Linear, gh or git — on a PR every gate would let through.
test_merge_mode_manual() {
  local how sb rc
  for how in queue named inline; do
    sb=$(make_sandbox "manual_$how")
    populate_happy_fixtures "$sb"
    set_merge_mode "$sb" '"manual"'
    case "$how" in
      queue)  run_pipeline "$sb" ;;
      named)  run_pipeline "$sb" EXP-1 ;;
      inline) BUREAU_INLINE_MERGE=1 run_pipeline "$sb" EXP-1 ;;
    esac
    rc=$?
    [ "$rc" -eq 2 ] || { echo "FAIL manual_$how: exit $rc, wanted 2" >&2; sed 's/^/  | /' "$sb/pipeline.out" "$sb/pipeline.err" >&2; return 1; }
    assert_untouched "$sb" "manual_$how" || return 1
    grep -q 'merge_mode is manual' "$sb/pipeline.out" || { echo "FAIL manual_$how: no reason printed" >&2; return 1; }
  done
  return 0
}

# An unknown value falls closed: no merge, and a warning names the value.
test_merge_mode_invalid() {
  local value sb rc
  for value in '"Manual"' '"yes"' 'false' 'true' '0' '{}'; do
    sb=$(make_sandbox "invalid_mode")
    rm -rf "$sb/stub_data"/* "$sb/gh_invocations.log"
    populate_happy_fixtures "$sb"
    set_merge_mode "$sb" "$value"
    run_pipeline "$sb"; rc=$?
    [ "$rc" -eq 2 ] || { echo "FAIL invalid $value: exit $rc, wanted 2" >&2; return 1; }
    assert_untouched "$sb" "invalid $value" || return 1
    grep -q 'merge_mode.*falling closed to manual' "$sb/pipeline.err" || { echo "FAIL invalid $value: no warning" >&2; sed 's/^/  | /' "$sb/pipeline.err" >&2; return 1; }
  done
  return 0
}

# Negative control: "auto" and null (as absent, test_happy_path) still merge the same PR.
test_merge_mode_auto_merges() {
  local value sb
  for value in '"auto"' 'null'; do
    sb=$(make_sandbox "auto_mode")
    rm -rf "$sb/stub_data"/* "$sb/gh_invocations.log"
    populate_happy_fixtures "$sb"
    set_merge_mode "$sb" "$value"
    run_pipeline "$sb"
    [ -s "$sb/stub_data/merge_calls.log" ] || { echo "FAIL auto $value: gh pr merge was NOT called" >&2; sed 's/^/  | /' "$sb/pipeline.out" >&2; return 1; }
    if grep -q 'falling closed' "$sb/pipeline.err"; then echo "FAIL auto $value: warned about a valid value" >&2; return 1; fi
  done
  return 0
}

# ── Gate outcome (v3.0.1): the merge stage tells its caller why it did not merge ──
# It used to end with 0 whether it merged or not; a shepherd then took the
# unchanged Merge state for an unseen move and ran into its stuck detector
# (pilot run EXP-1534). Now: 2 = not yet (pending, not started, still computing),
# 25 = blocked; with BUREAU_MERGE_GATE_REPORT the outcome and gate lines land in
# that file. Inline merges and --dry-run keep their 0.
run_gate() {  # <sb> [args…] — sets GRC (exit code) and GREP (report file content)
  local sb="$1"; shift
  rm -f "$sb/gate.report"
  STUB_DIR="$sb/stub_data" INVOCATIONS_LOG="$sb/gh_invocations.log" PATH="$sb/bin:$PATH" \
    BUREAU_MERGE_GATE_REPORT="$sb/gate.report" \
    env ${GATE_ENV:-} bash "$sb/scripts/merge-pipeline.sh" "$@" > "$sb/pipeline.out" 2> "$sb/pipeline.err"
  GRC=$?
  GREP=$(cat "$sb/gate.report" 2>/dev/null || true)
}
gate_case() {  # <sb> <label> <want rc> <want outcome|-> [pattern in the report]
  local sb="$1" label="$2" want_rc="$3" want_out="$4" pat="${5:-}"
  if [ "$GRC" != "$want_rc" ]; then
    echo "FAIL gate $label: exit $GRC, wanted $want_rc" >&2; sed 's/^/  | /' "$sb/pipeline.out" >&2; return 1
  fi
  if [ "$want_out" = - ]; then
    [ -z "$GREP" ] || { echo "FAIL gate $label: wrote a report although it merged: $GREP" >&2; return 1; }
  else
    [ "$(printf '%s\n' "$GREP" | head -n 1)" = "$want_out" ] \
      || { echo "FAIL gate $label: report outcome '$(printf '%s\n' "$GREP" | head -n 1)', wanted '$want_out'" >&2; return 1; }
    [ -z "$pat" ] || printf '%s\n' "$GREP" | sed -n '2,$p' | grep -q -- "$pat" \
      || { echo "FAIL gate $label: report lacks '$pat': $GREP" >&2; return 1; }
    [ ! -s "$sb/stub_data/merge_calls.log" ] || { echo "FAIL gate $label: gh pr merge was called" >&2; return 1; }
  fi
  return 0
}
set_pr_field() {  # <sb> <jq assignment>
  jq "$2" "$1/stub_data/pr_view.json" > "$1/stub_data/pr_view.json.tmp" && mv "$1/stub_data/pr_view.json.tmp" "$1/stub_data/pr_view.json"
}

test_gate_outcome() {
  local sb
  # Checks still running (GitHub shows UNSTABLE meanwhile): not yet.
  sb=$(make_sandbox gate_pending); populate_happy_fixtures "$sb"; set_pr_field "$sb" '.mergeStateStatus="UNSTABLE"'
  echo '{"check_runs":[{"name":"ci","status":"in_progress","conclusion":null}]}' > "$sb/stub_data/check_runs.json"
  run_gate "$sb"; gate_case "$sb" pending 2 not-yet 'still pending' || return 1
  # No check has started yet: not yet.
  sb=$(make_sandbox gate_notstarted); populate_happy_fixtures "$sb"
  echo '{"check_runs":[]}' > "$sb/stub_data/check_runs.json"
  run_gate "$sb"; gate_case "$sb" not-started 2 not-yet 'only 0 completed' || return 1
  # GitHub still computing mergeStateStatus, everything else green: not yet.
  sb=$(make_sandbox gate_unknown); populate_happy_fixtures "$sb"; set_pr_field "$sb" '.mergeStateStatus="UNKNOWN"'
  run_gate "$sb"; gate_case "$sb" unknown 2 not-yet 'UNKNOWN' || return 1
  # A gate read that failed is not a verdict: not yet (the check-runs query, the base).
  sb=$(make_sandbox gate_ciread); populate_happy_fixtures "$sb"; rm -f "$sb/stub_data/check_runs.json"
  run_gate "$sb"; gate_case "$sb" ci-read-failed 2 not-yet 'check-runs query failed' || return 1
  sb=$(make_sandbox gate_baseread); populate_happy_fixtures "$sb"; rm -f "$sb/stub_data/branch_main.json"
  run_gate "$sb"; gate_case "$sb" base-read-failed 2 not-yet 'base: cannot resolve main HEAD' || return 1
  # The PR's own gate read and the verdict read failing (a 502) are not verdicts: not
  # yet, even next to a pending check — they used to read as "PR state= (need OPEN)"
  # and "verdict=none" and end blocked. So is an unreadable thread list, which used to
  # count as zero unresolved threads.
  local r
  for r in pr_gate_read comments_read threads_read; do
    sb=$(make_sandbox "gate_fail_$r"); populate_happy_fixtures "$sb"; touch "$sb/stub_data/fail_$r"
    echo '{"check_runs":[{"name":"ci","status":"in_progress","conclusion":null}]}' > "$sb/stub_data/check_runs.json"
    run_gate "$sb"
    case "$r" in
      pr_gate_read)  gate_case "$sb" "$r" 2 not-yet 'pr_read: ' || return 1 ;;
      comments_read) gate_case "$sb" "$r" 2 not-yet 'verdict_read: ' || return 1 ;;
      threads_read)  gate_case "$sb" "$r" 2 not-yet 'threads_read: ' || return 1 ;;
    esac
    printf '%s\n' "$GREP" | grep -qE '^(pr_state|verdict|unresolved_threads):' \
      && { echo "FAIL gate $r: a failed read still reads as a verdict: $GREP" >&2; return 1; }
  done
  # A hold label a human put on the PR: not yet (the queue stays quiet until they remove it).
  sb=$(make_sandbox gate_hold); populate_happy_fixtures "$sb"; set_pr_field "$sb" '.labels=[{"name":"wip"}]'
  run_gate "$sb"; gate_case "$sb" hold-label 2 not-yet "hold label 'wip'" || return 1
  # Conflicts the rebase stage resolves (agents.rebase on, bureau-only divergence): not
  # yet. With a human commit in the divergence, or the rebase agent off: blocked.
  sb=$(make_sandbox gate_dirty_rebase); populate_happy_fixtures "$sb"; set_pr_field "$sb" '.mergeStateStatus="DIRTY"'
  jq '.agents.rebase = true' "$sb/.bureau.json" > "$sb/.bureau.json.tmp" && mv "$sb/.bureau.json.tmp" "$sb/.bureau.json"
  echo 'branch_is_bureau_only() { return 0; }' >> "$sb/scripts/bureau-config.sh"
  run_gate "$sb"; gate_case "$sb" dirty-rebasable 2 not-yet 'the rebase stage resolves it' || return 1
  echo 'branch_is_bureau_only() { return 1; }' >> "$sb/scripts/bureau-config.sh"
  run_gate "$sb"; gate_case "$sb" dirty-human-commits 25 blocked 'DIRTY (need CLEAN)' || return 1
  # The shepherd forces every agent on; the rebase stage still counts only when the
  # repo turned it on (the shepherd never runs it).
  sb=$(make_sandbox gate_dirty_forced); populate_happy_fixtures "$sb"; set_pr_field "$sb" '.mergeStateStatus="DIRTY"'
  echo 'branch_is_bureau_only() { return 0; }' >> "$sb/scripts/bureau-config.sh"
  GATE_ENV="BUREAU_FORCE_ALL_AGENTS=1" run_gate "$sb"; gate_case "$sb" dirty-forced 25 blocked 'DIRTY (need CLEAN)' || return 1
  # Under a shepherd (BUREAU_HELD_BY_SHEPHERD=1) nothing rebases the ticket: blocked.
  sb=$(make_sandbox gate_dirty_held); populate_happy_fixtures "$sb"; set_pr_field "$sb" '.mergeStateStatus="DIRTY"'
  jq '.agents.rebase = true' "$sb/.bureau.json" > "$sb/.bureau.json.tmp" && mv "$sb/.bureau.json.tmp" "$sb/.bureau.json"
  echo 'branch_is_bureau_only() { return 0; }' >> "$sb/scripts/bureau-config.sh"
  GATE_ENV="BUREAU_HELD_BY_SHEPHERD=1" run_gate "$sb"; gate_case "$sb" dirty-held 25 blocked 'DIRTY (need CLEAN)' || return 1
  # A failing check (the EXP-1534 case): blocked, whatever GitHub's state says.
  sb=$(make_sandbox gate_red); populate_happy_fixtures "$sb"; set_pr_field "$sb" '.mergeStateStatus="UNSTABLE"'
  echo '{"check_runs":[{"name":"build + test","status":"completed","conclusion":"failure"}]}' > "$sb/stub_data/check_runs.json"
  run_gate "$sb"; gate_case "$sb" red 25 blocked 'failing check(s) on HEAD_SHA: build + test' || return 1
  # Conflicts, a stale base, no APPROVE: blocked.
  sb=$(make_sandbox gate_dirty); populate_happy_fixtures "$sb"; set_pr_field "$sb" '.mergeStateStatus="DIRTY"'
  run_gate "$sb"; gate_case "$sb" dirty 25 blocked 'DIRTY' || return 1
  sb=$(make_sandbox gate_behind); populate_happy_fixtures "$sb"
  echo '{"commit":{"sha":"MAIN_SHA_NEW"}}' > "$sb/stub_data/branch_main.json"; echo '{"ahead_by":3}' > "$sb/stub_data/compare.json"
  run_gate "$sb"; gate_case "$sb" behind 25 blocked 'behind main' || return 1
  sb=$(make_sandbox gate_noverdict); populate_happy_fixtures "$sb"; set_pr_field "$sb" '.comments=[]'
  run_gate "$sb"; gate_case "$sb" no-verdict 25 blocked 'verdict=none' || return 1
  # Pending CI next to a missing APPROVE is blocked: waiting cannot bring the APPROVE.
  sb=$(make_sandbox gate_mixed); populate_happy_fixtures "$sb"; set_pr_field "$sb" '.comments=[]'
  echo '{"check_runs":[{"name":"ci","status":"queued","conclusion":null}]}' > "$sb/stub_data/check_runs.json"
  run_gate "$sb"; gate_case "$sb" pending+no-verdict 25 blocked 'still pending' || return 1
  # The just-in-time recheck reports like the initial gate (main moved in between).
  sb=$(make_sandbox gate_jit); populate_happy_fixtures "$sb"
  echo '{"commit":{"sha":"MAIN_SHA"}}' > "$sb/stub_data/branch_main_1.json"
  echo '{"commit":{"sha":"MAIN_SHA_AFTER_RACE"}}' > "$sb/stub_data/branch_main_2.json"; echo '{"ahead_by":1}' > "$sb/stub_data/compare.json"
  run_gate "$sb"; gate_case "$sb" jit 25 blocked 'behind main' || return 1
  grep -q 'Gate regressed' "$sb/pipeline.out" || { echo "FAIL gate jit: the recheck did not run" >&2; return 1; }
  # A merge that goes through: 0, no report.
  sb=$(make_sandbox gate_green); populate_happy_fixtures "$sb"
  run_gate "$sb"; gate_case "$sb" green 0 - || return 1
  [ -s "$sb/stub_data/merge_calls.log" ] || { echo "FAIL gate green: gh pr merge was not called" >&2; return 1; }
  # The review stage's inline merge ends with the same codes (v3.1; it used to end with 0
  # and the review reported Done). tests/test_inline_merge_result.sh covers the review side.
  sb=$(make_sandbox gate_inline); populate_happy_fixtures "$sb"
  echo '{"check_runs":[{"name":"ci","status":"completed","conclusion":"failure"}]}' > "$sb/stub_data/check_runs.json"
  GATE_ENV="BUREAU_INLINE_MERGE=1" run_gate "$sb" EXP-1
  gate_case "$sb" inline-blocked 25 blocked 'failing check(s)' || return 1
  sb=$(make_sandbox gate_inline_pending); populate_happy_fixtures "$sb"
  echo '{"check_runs":[{"name":"ci","status":"in_progress","conclusion":null}]}' > "$sb/stub_data/check_runs.json"
  GATE_ENV="BUREAU_INLINE_MERGE=1" run_gate "$sb" EXP-1
  gate_case "$sb" inline-not-yet 2 not-yet 'still pending' || return 1
  # --dry-run stays an audit: 0, and it names the outcome.
  sb=$(make_sandbox gate_dry); populate_happy_fixtures "$sb"
  echo '{"check_runs":[{"name":"ci","status":"in_progress","conclusion":null}]}' > "$sb/stub_data/check_runs.json"
  run_gate "$sb" --dry-run
  [ "$GRC" = 0 ] && [ -z "$GREP" ] || { echo "FAIL gate dry-run: exit $GRC, report '$GREP'" >&2; return 1; }
  grep -q 'gate outcome: not-yet' "$sb/pipeline.out" || { echo "FAIL gate dry-run: outcome not named" >&2; return 1; }
  # Without a report file (the queue loop) the codes are the same.
  sb=$(make_sandbox gate_nofile); populate_happy_fixtures "$sb"
  echo '{"check_runs":[{"name":"ci","status":"completed","conclusion":"failure"}]}' > "$sb/stub_data/check_runs.json"
  STUB_DIR="$sb/stub_data" INVOCATIONS_LOG="$sb/gh_invocations.log" PATH="$sb/bin:$PATH" \
    bash "$sb/scripts/merge-pipeline.sh" > "$sb/pipeline.out" 2> "$sb/pipeline.err"
  [ "$?" = 25 ] || { echo "FAIL gate without a report file: not 25" >&2; return 1; }

  # Negative control: v3.0.0's ending (exit 0 after the blocker comment).
  sb=$(make_sandbox gate_old); populate_happy_fixtures "$sb"
  echo '{"check_runs":[{"name":"ci","status":"in_progress","conclusion":null}]}' > "$sb/stub_data/check_runs.json"
  python3 - "$sb/scripts/merge-pipeline.sh" <<'NEG_EOF' || return 1
import pathlib, sys
p = pathlib.Path(sys.argv[1]); t = p.read_text()
old = '  merge_gate_exit "$GATE_OUTCOME" "$GATE_OUT"\n'
if t.count(old) != 1: sys.exit("negative control: the gate exit line was not found")
p.write_text(t.replace(old, '  exit 0\n'))
NEG_EOF
  run_gate "$sb"
  [ "$GRC" = 0 ] && [ -z "$GREP" ] || { echo "FAIL negative control: the old ending should end 0 without a report (exit $GRC)" >&2; return 1; }
  return 0
}

# The gate comment is posted again only when the outcome or a blocker changes: one
# check more or less still running is no change, pending → failing is, and so is an
# old-format comment (no "Outcome:" line).
post_back() {  # <sb> — the text in stub_data/last_body becomes the PR's latest bot comment
  jq --rawfile b "$1/stub_data/last_body" '.comments += [{"createdAt":"2026-09-29T09:00:00Z","body":$b}]' \
    "$1/stub_data/pr_view.json" > "$1/stub_data/pr_view.json.tmp" && mv "$1/stub_data/pr_view.json.tmp" "$1/stub_data/pr_view.json"
}
test_gate_comment_key() {
  local sb n
  sb=$(make_sandbox gate_key); populate_happy_fixtures "$sb"
  echo '{"check_runs":[{"name":"a","status":"in_progress","conclusion":null},{"name":"b","status":"queued","conclusion":null}]}' > "$sb/stub_data/check_runs.json"
  run_gate "$sb"
  n=$(grep -c 'Bureau merge gate' "$sb/stub_data/comments_posted.log" 2>/dev/null || echo 0)
  [ "$n" = 1 ] || { echo "FAIL key: first pass posted $n comments" >&2; return 1; }
  awk '/Bureau merge gate/{f=1} f' "$sb/stub_data/comments_posted.log" > "$sb/stub_data/last_body"; post_back "$sb"
  grep -q '^Outcome: not yet' "$sb/stub_data/last_body" || { echo "FAIL key: the comment does not state the outcome" >&2; return 1; }
  echo '{"check_runs":[{"name":"a","status":"completed","conclusion":"success"},{"name":"b","status":"in_progress","conclusion":null}]}' > "$sb/stub_data/check_runs.json"
  run_gate "$sb"
  grep -q 'Blockers unchanged' "$sb/pipeline.out" || { echo "FAIL key: 2 → 1 running check posted a new comment" >&2; return 1; }
  echo '{"check_runs":[{"name":"a","status":"completed","conclusion":"success"},{"name":"b","status":"completed","conclusion":"failure"}]}' > "$sb/stub_data/check_runs.json"
  run_gate "$sb"
  grep -q 'Posted blocker comment' "$sb/pipeline.out" || { echo "FAIL key: pending → failing did not post" >&2; return 1; }
  # An old-format comment (v3.0.0, no "Outcome:" line) with the same blocker line posts once.
  sb=$(make_sandbox gate_key_old); populate_happy_fixtures "$sb"
  echo '{"check_runs":[{"name":"a","status":"in_progress","conclusion":null}]}' > "$sb/stub_data/check_runs.json"
  printf '🛑 **Bureau merge gate** — PR #42 is not eligible to merge.\n\n- ci: 1 check(s) still pending on HEAD_SHA\n' > "$sb/stub_data/last_body"; post_back "$sb"
  run_gate "$sb"
  grep -q 'Posted blocker comment' "$sb/pipeline.out" || { echo "FAIL key: an old-format comment did not get the outcome" >&2; return 1; }
  return 0
}

# End to end: the real shepherd, worker, runtime and merge stage against the stubbed
# gh and Linear. A DIRTY PR with only bureau commits and agents.rebase on halts at
# once with 25 and the gate report — the queue's rebase picker skips a ticket the
# shepherd holds, so waiting could only run out. Negative control: e243165's stage,
# which classified that DIRTY as "not yet" also under the shepherd, waits.
run_shepherd_on_merge() {  # <sb> [env…] — sets SRC (exit code) and SWAITS (recorded waits)
  local sb="$1"; shift
  mkdir -p "$sb/tmp"
  set +e
  ( cd "$sb" && env STUB_DIR="$sb/stub_data" INVOCATIONS_LOG="$sb/gh_invocations.log" \
      PATH="$sb/bin:$PATH" TMPDIR="$sb/tmp" BUREAU_SHEPHERD_CONFIRM_SECONDS=0 "$@" \
      bash "$sb/scripts/shepherd.sh" --no-tmux --worktree "$sb/.worktrees/shepherd" EXP-1 \
      > "$sb/shepherd.out" 2> "$sb/shepherd.err" )
  SRC=$?
  set -e
  SWAITS=$(grep -c . "$sb/sleeps.log" 2>/dev/null || true); SWAITS=${SWAITS:-0}
}
make_shepherd_merge_sandbox() {  # <name> — the merge sandbox plus the shepherd's own scripts
  local sb; sb=$(make_sandbox "$1"); populate_happy_fixtures "$sb"; set_pr_field "$sb" '.mergeStateStatus="DIRTY"'
  jq '.agents.rebase = true' "$sb/.bureau.json" > "$sb/.bureau.json.tmp" && mv "$sb/.bureau.json.tmp" "$sb/.bureau.json"
  cp "$REPO_ROOT/templates/scripts/shepherd.sh" "$REPO_ROOT/templates/scripts/bureau-worker.sh" \
     "$REPO_ROOT/templates/scripts/bureau-runtime.py" "$REPO_ROOT/templates/scripts/bureau-supervision.py" "$sb/scripts/"
  git -C "$sb" init -q && git -C "$sb" -c user.name=t -c user.email=t@t commit -q --allow-empty -m init
  echo Merge > "$sb/stub_data/ticket_state"
  cat >> "$sb/scripts/bureau-config.sh" <<'SHEP_EOF'
get_issue_state()   { cat "$STUB_DIR/ticket_state"; }
move_issue()        { echo "move_issue $1 -> $2" >> "$STUB_DIR/state_changes.log"; [ "$2" = s8 ] && echo Done > "$STUB_DIR/ticket_state"; return 0; }
get_issue_detail()  { printf '%s' '{"identifier":"EXP-1","labels":[]}'; }
reset_worktree()    { mkdir -p "$1"; }
free_branch_from_other_worktrees() { :; }
session_throttle_guard() { return 0; }
branch_is_bureau_only() { return 0; }
alert_telegram()    { echo "$4" >> "$STUB_DIR/alerts.log"; }
SHEP_EOF
  printf '#!/bin/bash\nprintf "%%s\\n" "$1" >> "%s/sleeps.log"\n[ "$(wc -l < "%s/sleeps.log")" -ge 20 ] && kill -KILL "$PPID"\nexit 0\n' "$sb" "$sb" > "$sb/bin/sleep"
  chmod +x "$sb/bin/sleep"
  echo "$sb"
}
test_gate_dirty_under_shepherd() {
  local sb
  sb=$(make_shepherd_merge_sandbox shep_dirty)
  run_shepherd_on_merge "$sb"
  [ "$SRC" = 25 ] || { echo "FAIL shepherd dirty: exit $SRC, wanted 25" >&2; tail -8 "$sb/shepherd.out" "$sb/shepherd.err" >&2; return 1; }
  [ "$SWAITS" = 0 ] || { echo "FAIL shepherd dirty: waited $SWAITS time(s) for a rebase nothing runs" >&2; return 1; }
  grep -q 'the merge gate is blocked' "$sb/stub_data/comments_posted.log" && grep -q 'DIRTY (need CLEAN)' "$sb/stub_data/comments_posted.log" \
    || { echo "FAIL shepherd dirty: the halt comment lacks the gate report" >&2; cat "$sb/stub_data/comments_posted.log" >&2; return 1; }
  grep -q '+EXP-1 needs-human' "$sb/stub_data/labels.log" || { echo "FAIL shepherd dirty: no needs-human" >&2; return 1; }
  [ ! -s "$sb/stub_data/merge_calls.log" ] || { echo "FAIL shepherd dirty: merged a DIRTY PR" >&2; return 1; }
  # The queue (no shepherd) keeps "not yet" for the same PR: the rebase stage takes it.
  run_gate "$sb"; gate_case "$sb" dirty-queue 2 not-yet 'the rebase stage resolves it' || return 1

  # Negative control: e243165's stage (no BUREAU_HELD_BY_SHEPHERD check) — the shepherd waits.
  sb=$(make_shepherd_merge_sandbox shep_dirty_old)
  python3 - "$sb/scripts/merge-pipeline.sh" <<'NEG_EOF' || return 1
import pathlib, sys
p = pathlib.Path(sys.argv[1]); t = p.read_text()
old = '  [ "${BUREAU_HELD_BY_SHEPHERD:-0}" = 1 ] && return 1\n'
if t.count(old) != 1: sys.exit("negative control: the shepherd check was not found")
p.write_text(t.replace(old, ''))
NEG_EOF
  run_shepherd_on_merge "$sb" BUREAU_SHEPHERD_MERGE_WAIT_SECONDS=120 BUREAU_SHEPHERD_MERGE_POLL_SECONDS=60
  [ "$SWAITS" -ge 1 ] || { echo "FAIL negative control: the old stage should make the shepherd wait (exit $SRC, $SWAITS waits)" >&2; tail -5 "$sb/shepherd.out" >&2; return 1; }
  return 0
}

# After a reused approval (PR #26) the ticket reaches Merge like after any APPROVE, and
# the gate reads the verdict from the review comment. The comment and the reuse text are
# cut from the real review stage, so a reuse that stopped posting "**Verdict**: APPROVE"
# would turn the gate into "blocked (verdict)" here.
test_gate_after_reused_approval() {
  local review="$REPO_ROOT/templates/scripts/code-review-pipeline.sh" body sb
  # The two assignments, each from its first line to the line that closes its string.
  local cut; cut=$(python3 - "$review" <<'CUT_EOF'
import sys
lines = open(sys.argv[1]).read().split("\n")
def block(start, end):
    i = [k for k, l in enumerate(lines) if l.startswith(start)]
    if len(i) != 1: sys.exit("cannot find %r" % start)
    j = next(k for k in range(i[0], len(lines)) if lines[k].endswith(end))
    return "\n".join(lines[i[0]:j + 1])
print(block('  MERGED_REVIEW="Reused the approval', '\\`\\`\\`"'))
print(block('REVIEW_COMMENT="## Code Review v2', 'Automated review by Bureau pipeline*"'))
CUT_EOF
  ) || { echo "FAIL reuse: $cut" >&2; return 1; }
  body=$(ISSUE=EXP-1 VERDICT=APPROVE BUILD_STATUS=Passed REVIEW_HEAD=HEAD_SHA PR_BASE_REF=main REVIEW_BASE=MAIN_SHA \
    PR_NUMBER=42 REUSED_AT=2026-09-29T08:00:00Z /bin/bash -euc 'eval "$1"; printf "%s" "$REVIEW_COMMENT"' _ "$cut") \
    || { echo "FAIL reuse: the cut assignments do not run" >&2; return 1; }
  case "$body" in *"**Verdict**: APPROVE"*"Reused the approval recorded 2026-09-29T08:00:00Z"*) ;;
    *) echo "FAIL reuse: could not cut the review comment and the reuse text from code-review-pipeline.sh: $body" >&2; return 1 ;; esac
  for ci in pending green; do
    sb=$(make_sandbox "gate_reuse_$ci"); populate_happy_fixtures "$sb"
    jq --arg b "$body" '.comments = [{"createdAt":"2026-09-29T08:01:00Z","body":$b}]' "$sb/stub_data/pr_view.json" > "$sb/stub_data/pr_view.json.tmp" \
      && mv "$sb/stub_data/pr_view.json.tmp" "$sb/stub_data/pr_view.json"
    [ "$ci" = green ] || echo '{"check_runs":[{"name":"ci","status":"in_progress","conclusion":null}]}' > "$sb/stub_data/check_runs.json"
    run_gate "$sb"
    case "$ci" in
      pending) gate_case "$sb" reuse-pending 2 not-yet 'still pending' || return 1
               printf '%s\n' "$GREP" | grep -q '^verdict:' && { echo "FAIL reuse: the gate did not read the reused APPROVE" >&2; return 1; } ;;
      green)   gate_case "$sb" reuse-green 0 - || return 1
               [ -s "$sb/stub_data/merge_calls.log" ] || { echo "FAIL reuse: no merge after a reused approval" >&2; return 1; } ;;
    esac
  done
  return 0
}

# The queue loop's real run_script: a merge that is not yet eligible stays quiet
# ("queue empty"), a blocked one alerts (throttled per ticket and class).
test_gate_codes_in_queue_loop() {
  local q; q="$SANDBOX_ROOT/queue"; mkdir -p "$q/scripts"
  printf '#!/bin/bash\nexit "${STUB_RC:-0}"\n' > "$q/scripts/bureau-worker.sh"
  {
    sed -n '/^exit_class() {/,/^}/p' "$REAL_BUREAU_CONFIG"
    sed -n '/^run_script() {/,/^}/p' "$REPO_ROOT/templates/scripts/queue-loop.sh"
  } > "$q/queue.sh"
  grep -q '^run_script() {' "$q/queue.sh" || { echo "FAIL queue: run_script not found in queue-loop.sh" >&2; return 1; }
  local rc
  for rc in 2 25; do
    rm -f "$q/alerts" "$q/log"
    Q="$q" STUB_RC="$rc" /bin/bash -c '
      source "$Q/queue.sh"
      REPO_DIR="$Q"; LOG_FILE="$Q/log"; MODE=merge
      preselect_issue() { echo EXP-9; }
      get_issue_branch() { echo feat/x; }
      emit_event() { :; }
      stop_before_merge_was_asked() { return 1; }
      alert_telegram() { echo "$4" >> "$Q/alerts"; }
      run_script merge-pipeline.sh "Merge" "$Q"' >/dev/null 2>&1
    case "$rc" in
      2)  [ ! -s "$q/alerts" ] && grep -q 'queue empty' "$q/log" \
            || { echo "FAIL queue: a merge that is not yet eligible should be quiet" >&2; cat "$q/alerts" "$q/log" >&2; return 1; } ;;
      25) grep -q 'needs-human-or-paused' "$q/alerts" \
            || { echo "FAIL queue: a blocked merge should alert" >&2; cat "$q/log" >&2; return 1; } ;;
    esac
  done
  return 0
}

# ── Run all ───────────────────────────────────────────────────────
FAILS=0
for scenario in test_happy_path test_stale_base test_ci_red test_ci_pending test_jit_race test_ghost_merge test_ghost_merge_bare_branch test_ghost_merge_branch_mismatch test_merge_message_defanged test_merge_message_read_fails test_merge_rebase_stays_plain test_merge_mode_manual test_merge_mode_invalid test_merge_mode_auto_merges test_gate_outcome test_gate_comment_key test_gate_dirty_under_shepherd test_gate_after_reused_approval test_gate_codes_in_queue_loop; do
  if "$scenario"; then
    echo "  ok   $scenario"
  else
    echo "  FAIL $scenario"
    FAILS=$((FAILS + 1))
  fi
done

if [ "$FAILS" -eq 0 ]; then
  echo "OK test_merge_pipeline_correctness"
  exit 0
else
  echo "FAIL test_merge_pipeline_correctness ($FAILS scenario(s) failed)"
  exit 1
fi
