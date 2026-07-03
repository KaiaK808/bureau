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

  cp "$REAL_BUREAU_CONFIG"  "$sb/scripts/bureau-config.sh"
  cp "$REAL_MERGE_PIPELINE" "$sb/scripts/merge-pipeline.sh"

  # Append test overrides for Linear glue. These come AFTER the real
  # definitions so they win at function-resolution time. pr_ci_is_green
  # and pr_base_is_current (the helpers under test) stay as the real
  # implementations.
  cat >> "$sb/scripts/bureau-config.sh" <<'OVERRIDES'

# ── TEST OVERRIDES ─────────────────────────────────────────────────
precondition_linear()      { return 0; }
precondition_claude_auth() { return 0; }
post_comment()             { echo "post_comment $1 :: $2" >> "$STUB_DIR/comments_posted.log"; }
move_issue()               { echo "move_issue $1 -> $2"   >> "$STUB_DIR/state_changes.log"; }
add_issue_label()          { echo "+$1 $2" >> "$STUB_DIR/labels.log"; }
remove_issue_label()       { echo "-$1 $2" >> "$STUB_DIR/labels.log"; }
get_issue_branch()         { echo "feat/test"; }
pipeline_pick_next()       { echo "EXP-1"; }
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
      view) cat "$STUB_DIR/pr_view.json" | project_and_filter ;;
      merge)
        echo "gh pr merge ${3:-?}" >> "$STUB_DIR/merge_calls.log"
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
      graphql) cat "$STUB_DIR/review_threads.json" | apply_jq ;;
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

# ── Run all ───────────────────────────────────────────────────────
FAILS=0
for scenario in test_happy_path test_stale_base test_ci_red test_ci_pending test_jit_race test_ghost_merge test_ghost_merge_bare_branch test_ghost_merge_branch_mismatch; do
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
