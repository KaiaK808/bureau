#!/bin/bash
# Test doubles for the review stage's inline merge and the merge gate (v3.1, review and
# merge outcomes). Source after tests/lib/harness.sh and call pr2_gate_setup after
# sandbox_init.
#
# What runs for real on top of the harness: the review stage, merge-pipeline.sh (inline
# or on its own), bureau-supervision.py, and the gate helpers cut from the real
# bureau-config.sh (pr_ci_is_green with its CI start grace, pr_base_is_current). The
# edges are doubles: Linear (the harness stub), the models (fake_claude.sh) and GitHub,
# played by the `gh` below from files in $PR2_GH:
#   pr.json         the PR (number, state, mergeStateStatus, labels, head/base refs and OIDs)
#   comments.json   the PR's comments; `gh pr comment` appends to it, so the gate reads
#                   the review comment the stage just posted
#   check_runs.json, status.json, branch.json, compare.json, threads.json, commit.json
#   fail_status     present: the legacy status read fails
# Every call is logged to $SANDBOX/gh_calls.log (tab-separated argv, like the harness gh);
# `gh pr merge` also writes merge_calls.log and flips the PR to MERGED.
# `date +%s` answers $PR2_NOW (set here), so the CI start grace can be tested to the second;
# every other `date` call is the real one.

pr2_gate_setup() {
  PR2_GH="$SANDBOX/.pr2-gh"
  export PR2_GH
  mkdir -p "$PR2_GH/bin"
  cat > "$PR2_GH/bin/gh" <<'GH'
#!/bin/bash
set -uo pipefail
D="${PR2_GH:?}"
{ printf 'gh'; for a in "$@"; do printf '\t%s' "$a"; done; printf '\n'; } >> "${SANDBOX:?}/gh_calls.log"
JQ="" JSON="" BODY="" prev=""
for a in "$@"; do
  case "$prev" in --jq) JQ="$a" ;; --json) JSON="$a" ;; --body) BODY="$a" ;; esac
  prev="$a"
done
out() { if [ -n "$JQ" ]; then jq -r "$JQ"; else cat; fi; }
pr_doc() { jq --slurpfile c "$D/comments.json" '. + {comments: $c[0]}' "$D/pr.json"; }
case "${1:-}:${2:-}" in
  repo:view) echo '{"nameWithOwner":"test-owner/test-repo"}' | out ;;
  pr:list)
    case " $* " in
      *" merged "*) echo '[]' | out ;;
      *) jq 'if .state == "OPEN" then [{number: .number}] else [] end' "$D/pr.json" | out ;;
    esac ;;
  pr:view)
    if [ -n "$JSON" ]; then
      pr_doc | jq --arg f "$JSON" '. as $pr | reduce ($f | split(",")[]) as $k ({}; .[$k] = $pr[$k])' | out
    else
      jq -r .url "$D/pr.json"
    fi ;;
  pr:comment)
    jq --arg b "$BODY" --arg t "2026-09-29T10:$(printf '%02d' "$(jq length "$D/comments.json")"):00Z" \
      '. + [{createdAt: $t, body: $b}]' "$D/comments.json" > "$D/comments.json.tmp" && mv "$D/comments.json.tmp" "$D/comments.json" ;;
  pr:merge)
    echo "gh pr merge ${3:-?}" >> "$D/merge_calls.log"
    jq '.state = "MERGED"' "$D/pr.json" > "$D/pr.json.tmp" && mv "$D/pr.json.tmp" "$D/pr.json" ;;
  api:*)
    case "$2" in
      graphql) out < "$D/threads.json" ;;
      */commits/*/check-runs) cat "$D/check_runs.json" ;;
      */commits/*/status) [ ! -e "$D/fail_status" ] || { echo "HTTP 502" >&2; exit 1; }; out < "$D/status.json" ;;
      */git/commits/*) [ -s "$D/commit.json" ] || exit 1; out < "$D/commit.json" ;;
      */branches/*) out < "$D/branch.json" ;;
      */compare/*) out < "$D/compare.json" ;;
      *) echo "pr2 gh double: unknown api path $2" >&2; exit 1 ;;
    esac ;;
  *) echo "pr2 gh double: unknown call $*" >&2; exit 1 ;;
esac
GH
  chmod +x "$PR2_GH/bin/gh"
  cat > "$PR2_GH/bin/date" <<'DATE'
#!/bin/bash
if [ "$#" = 1 ] && [ "$1" = +%s ] && [ -n "${PR2_NOW:-}" ]; then echo "$PR2_NOW"; else exec /bin/date "$@"; fi
DATE
  chmod +x "$PR2_GH/bin/date"
  PR2_NOW=$(/bin/date +%s)
  export PR2_NOW
  export PATH="$PR2_GH/bin:$PATH"

  # The gate helpers from the real config; the owner/repo lookup is the only gh read
  # replaced here, because the real one caches a `gh repo view` per process.
  {
    sed -n -e '/^pr_ci_is_green() {/,/^}/p' -e '/^_merge_gate_number() {/,/^}/p' \
      -e '/^_pr_head_commit_age() {/,/^}/p' -e '/^pr_base_is_current() {/,/^}/p' \
      "$REPO_ROOT/templates/scripts/bureau-config.sh"
    echo '_bureau_gh_owner_repo() { printf "test-owner/test-repo"; }'
  } >> "$SCRIPTS_DIR/real-helpers.sh"
  local fn
  for fn in pr_ci_is_green _merge_gate_number _pr_head_commit_age pr_base_is_current; do
    grep -q "^$fn() {" "$SCRIPTS_DIR/real-helpers.sh" || { echo "pr2-gate: $fn not found in bureau-config.sh" >&2; return 1; }
  done

  local head base
  head=$(git -C "$SANDBOX" rev-parse "refs/remotes/origin/${BUREAU_STUB_BRANCH:-test-branch}" 2>/dev/null \
    || git -C "$SANDBOX" rev-parse "${BUREAU_STUB_BRANCH:-test-branch}")
  base=$(git -C "$SANDBOX" rev-parse origin/main 2>/dev/null || git -C "$SANDBOX" rev-parse main)
  jq -n --arg head "$head" --arg base "$base" --arg branch "${BUREAU_STUB_BRANCH:-test-branch}" \
    '{number: 99, state: "OPEN", url: "https://github.com/test-owner/test-repo/pull/99",
      title: "EXP: test PR", body: "Summary.", mergeStateStatus: "CLEAN", labels: [],
      headRefName: $branch, headRefOid: $head, baseRefName: "main", baseRefOid: $base}' > "$PR2_GH/pr.json"
  echo '[]' > "$PR2_GH/comments.json"
  echo '{"statuses":[]}' > "$PR2_GH/status.json"
  jq -n --arg base "$base" '{commit: {sha: $base}}' > "$PR2_GH/branch.json"
  echo '{"ahead_by":0}' > "$PR2_GH/compare.json"
  echo '{"data":{"repository":{"pullRequest":{"reviewThreads":{"nodes":[]}}}}}' > "$PR2_GH/threads.json"
  pr2_checks green
  pr2_head_age 60
}

# pr2_checks none|pending|green|red — the check runs on the PR head.
pr2_checks() {
  case "$1" in
    none)    echo '{"check_runs":[]}' ;;
    pending) echo '{"check_runs":[{"name":"ci","status":"in_progress","conclusion":null}]}' ;;
    green)   echo '{"check_runs":[{"name":"ci","status":"completed","conclusion":"success"}]}' ;;
    red)     echo '{"check_runs":[{"name":"ci","status":"completed","conclusion":"failure"}]}' ;;
  esac > "$PR2_GH/check_runs.json"
}

# pr2_head_age <seconds>|unreadable|empty — the head commit's committer time, that long ago;
# unreadable: the read fails; empty: GitHub answers without a time.
pr2_head_age() {
  if [ "$1" = unreadable ]; then : > "$PR2_GH/commit.json"; return; fi
  if [ "$1" = empty ]; then echo '{"committer":{"date":""}}' > "$PR2_GH/commit.json"; return; fi
  jq -n --argjson t "$(( PR2_NOW - $1 ))" '{committer: {date: ($t | todate)}}' > "$PR2_GH/commit.json"
}

# pr2_config <jq filter> — edit the sandbox .bureau.json (none reads as {}).
pr2_config() {
  [ -f "$SANDBOX/.bureau.json" ] || echo '{}' > "$SANDBOX/.bureau.json"
  jq "$1" "$SANDBOX/.bureau.json" > "$SANDBOX/.bureau.json.tmp" && mv "$SANDBOX/.bureau.json.tmp" "$SANDBOX/.bureau.json"
}

# Counters for assertions.
pr2_model_calls() { cat "$SANDBOX/fake_claude_counter" 2>/dev/null || echo 0; }
pr2_review_comments() { jq '[.[] | select(.body | test("Code Review v2"))] | length' "$PR2_GH/comments.json"; }
pr2_merged() { [ -s "$PR2_GH/merge_calls.log" ]; }
pr2_gate_reads() { grep -c 'check-runs' "$SANDBOX/gh_calls.log" 2>/dev/null || true; }
