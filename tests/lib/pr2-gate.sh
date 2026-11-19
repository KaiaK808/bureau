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
#   move_head_on_merge  a SHA: a push lands just before the merge call (the head moves)
#   move_head_after_reads  "<n> <sha>": a push lands right after the n-th read of the head
#   fail_head_read  present: reading the PR's head commit (--json headRefOid) fails
#   fail_gate_comment_read  present: only the lookup of prior merge gate comments fails
#   retarget_after_read  "<gate-snapshot|base-tip|merge-message> <n> <base> <sha>": retarget after that response
#   branch-<base>.json  overrides branch.json for the named branch
#   fail_live_base_read  present: the final standalone base name read fails
#   allow_unpinned  present: a merge without --match-head-commit goes through (v3.0.2's call)
# `gh pr merge` must carry --match-head-commit and refuses it, as GitHub does, when the
# PR's head is not that commit.
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
JQ="" JSON="" BODY="" PIN="" prev=""
for a in "$@"; do
  case "$prev" in --jq) JQ="$a" ;; --json) JSON="$a" ;; --body) BODY="$a" ;; --match-head-commit) PIN="$a" ;; esac
  prev="$a"
done
out() { if [ -n "$JQ" ]; then jq -r "$JQ"; else cat; fi; }
pr_doc() {
  # Match the CLI's PR comments projection. lastEditedAt is available only
  # through the separate IssueComment node lookup below.
  jq --slurpfile c "$D/comments.json" '. + {comments: ($c[0] | map({
    id, author: {login: "fixture", id: "fixture-user", name: ""}, authorAssociation: "NONE",
    body, createdAt, includesCreatedEdit: (.lastEditedAt != null),
    isMinimized: false, minimizedReason: "", reactionGroups: [],
    url: ("https://github.com/test-owner/test-repo/pull/99#issuecomment-" + .id), viewerDidAuthor: true
  }))}' "$D/pr.json"
}
retarget_after_read() {
  local kind after base sha reads
  [ -s "$D/retarget_after_read" ] || return 0
  read -r kind after base sha < "$D/retarget_after_read"
  [ "$kind" = "$1" ] || return 0
  reads=$(( $(cat "$D/retarget_reads" 2>/dev/null || echo 0) + 1 ))
  echo "$reads" > "$D/retarget_reads"
  [ "$reads" = "$after" ] || return 0
  jq --arg ref "$base" --arg oid "$sha" '.baseRefName = $ref | .baseRefOid = $oid' "$D/pr.json" > "$D/pr.json.tmp" \
    && mv "$D/pr.json.tmp" "$D/pr.json"
}
case "${1:-}:${2:-}" in
  repo:view) echo '{"nameWithOwner":"test-owner/test-repo"}' | out ;;
  pr:list)
    case " $* " in
      *" merged "*) echo '[]' | out ;;
      *) jq 'if .state == "OPEN" then [{number: .number}] else [] end' "$D/pr.json" | out ;;
    esac ;;
  pr:view)
    if [ "$JSON" = headRefOid ] && [ -e "$D/fail_head_read" ]; then echo "HTTP 502" >&2; exit 1; fi
    if [ "$JSON" = comments ] && [ -e "$D/fail_comments_read" ]; then echo "HTTP 502" >&2; exit 1; fi
    if [ "$JSON" = comments ] && [ -e "$D/fail_gate_comment_read" ] && [[ "$JQ" == *'Bureau merge gate'* ]]; then
      echo "HTTP 502 (gate comment lookup)" >&2; exit 1
    fi
    if [ "$JSON" = baseRefName ] && [ -e "$D/fail_live_base_read" ]; then echo "HTTP 502" >&2; exit 1; fi
    if [ -n "$JSON" ]; then
      pr_doc | jq --arg f "$JSON" '. as $pr | reduce ($f | split(",")[]) as $k ({}; .[$k] = $pr[$k])' | out
    else
      jq -r .url "$D/pr.json"
    fi
    if [ "$JSON" = headRefOid ] && [ -s "$D/move_head_after_reads" ]; then
      reads=$(( $(cat "$D/head_reads" 2>/dev/null || echo 0) + 1 )); echo "$reads" > "$D/head_reads"
      read -r after sha < "$D/move_head_after_reads"
      if [ "$reads" = "$after" ]; then
        jq --arg h "$sha" '.headRefOid = $h' "$D/pr.json" > "$D/pr.json.tmp" && mv "$D/pr.json.tmp" "$D/pr.json"
      fi
    fi
    if [ "$JSON" = headRefOid ] && [ -s "$D/after_initial_head" ]; then
      hook=$(cat "$D/after_initial_head"); rm "$D/after_initial_head"; bash "$hook" || exit $?
    fi
    case "$JSON" in
      state,mergeStateStatus,labels,baseRefName|state,mergeStateStatus,labels,baseRefName,baseRefOid)
        retarget_after_read gate-snapshot ;;
      title,body) retarget_after_read merge-message ;;
    esac ;;
  pr:comment)
    jq --arg b "$BODY" --arg t "2026-09-29T10:$(printf '%02d' "$(jq length "$D/comments.json")"):00Z" \
      '. + [{id: ("comment-" + (length | tostring)), createdAt: $t, lastEditedAt: null, body: $b}]' "$D/comments.json" > "$D/comments.json.tmp" && mv "$D/comments.json.tmp" "$D/comments.json"
    if [[ "$BODY" == *'Code Review v2'* ]] && [ -s "$D/on_review_comment" ]; then
      hook=$(cat "$D/on_review_comment"); rm "$D/on_review_comment"; bash "$hook" || exit $?
    fi ;;
  pr:merge)
    if [ -s "$D/move_head_on_merge" ]; then
      jq --arg h "$(cat "$D/move_head_on_merge")" '.headRefOid = $h' "$D/pr.json" > "$D/pr.json.tmp" && mv "$D/pr.json.tmp" "$D/pr.json"
    fi
    [ -n "$PIN" ] || [ -e "$D/allow_unpinned" ] || { echo "pr2 gh double: gh pr merge without --match-head-commit" >&2; exit 1; }
    if [ -n "$PIN" ] && [ "$PIN" != "$(jq -r .headRefOid "$D/pr.json")" ]; then
      echo "GraphQL: Head branch was modified. Review and try the merge again. (mergePullRequest)" >&2; exit 1
    fi
    echo "gh pr merge ${3:-?}" >> "$D/merge_calls.log"
    jq '.state = "MERGED"' "$D/pr.json" > "$D/pr.json.tmp" && mv "$D/pr.json.tmp" "$D/pr.json" ;;
  api:*)
    case "$2" in
      graphql)
        case " $* " in
          *'node(id:$id)'*)
            [ ! -e "$D/fail_comment_metadata" ] || { echo "HTTP 502" >&2; exit 1; }
            id=""; for a in "$@"; do case "$a" in id=*) id=${a#id=} ;; esac; done
            jq --arg id "$id" '{data: {node: ([.[] | select(.id == $id) | {body, lastEditedAt}] | first)}}' "$D/comments.json" | out ;;
          *'baseRef{target{oid}}'*)
            jq -n --slurpfile pr "$D/pr.json" --slurpfile branch "$D/branch.json" \
              '{data: {resource: {baseRefName: $pr[0].baseRefName, baseRef: {target: {oid: $branch[0].commit.sha}}}}}' | out ;;
          *) out < "$D/threads.json" ;;
        esac ;;
      */commits/*/check-runs) cat "$D/check_runs.json" ;;
      */commits/*/status) [ ! -e "$D/fail_status" ] || { echo "HTTP 502" >&2; exit 1; }; out < "$D/status.json" ;;
      */git/commits/*) [ -s "$D/commit.json" ] || exit 1; out < "$D/commit.json" ;;
      */branches/*)
        branch_file="$D/branch-${2#*/branches/}.json"
        [ -f "$branch_file" ] || branch_file="$D/branch.json"
        out < "$branch_file" || exit $?
        retarget_after_read base-tip ;;
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

# Publish exactly the binding lines emitted by the review stage, including for
# the legacy AUTO_APPROVE verdict that the merge gate still accepts.
pr2_approval() {
  jq -n --slurpfile pr "$PR2_GH/pr.json" --arg verdict "${1:-APPROVE}" \
    '[{id: "approval", createdAt: "2026-09-29T09:00:00Z", lastEditedAt: null,
       body: ("## Code Review v2\n\n**Verdict**: " + $verdict +
         "\n**Reviewed head**: `" + $pr[0].headRefOid + "`\n**Target**: `" +
         $pr[0].baseRefName + "` at `" + $pr[0].baseRefOid + "`")}]' > "$PR2_GH/comments.json"
}

# Revert only approval binding in a private runtime copy. The same assertions
# must then reject the old behaviour; no checked-in runtime is changed.
pr2_revert_approval_binding() {
  python3 - "$SCRIPTS_DIR/merge-pipeline.sh" <<'PY'
from pathlib import Path
import sys
p = Path(sys.argv[1]); source = p.read_text()
start = '  elif [ -n "$head" ] && [ "$_pr_read" = ok ]; then\n'
end = '\n  fi\n  if [ "$_threads_read" = failed ]; then'
assert source.count(start) == 1
i = source.index(start); j = source.index(end, i)
p.write_text(source[:i] + '  elif [ "$_pr_read" = ok ]; then\n    _approved_base="$_base_ref"' + source[j:])
PY
}

# Move the target after the selected gate read. Release has a distinct live tip
# so the old split reads can pass the freshness check against the wrong branch.
pr2_retarget_after_read() {
  local release
  release=$(git -C "$SANDBOX" rev-parse HEAD)
  git -C "$SANDBOX" push -q origin 'HEAD:refs/heads/release'
  jq -n --arg head "$release" '{commit: {sha: $head}}' > "$PR2_GH/branch-release.json"
  printf '%s %s release %s\n' "$1" "$2" "$release" > "$PR2_GH/retarget_after_read"
}

# Restore the split reads, drop the merge-time base check, or undo both, in
# private copies of the same helpers and merge stage the tests execute.
pr2_revert_merge_base_fix() {
  python3 - "$SCRIPTS_DIR" "$1" <<'PY'
from pathlib import Path
import sys

scripts = Path(sys.argv[1])
fix = sys.argv[2]
assert fix in ('snapshot', 'target', 'both')
if fix in ('snapshot', 'both'):
    helpers = scripts / 'real-helpers.sh'
    source = helpers.read_text()
    start = '  if [ -z "$base_ref" ] || [ -z "$base_pr_sha" ]; then\n'
    assert source.count(start) == 1
    helpers.write_text(source.replace(start, '''  base_ref=$(gh pr view "$pr" --json baseRefName --jq .baseRefName)
  base_pr_sha=$(gh pr view "$pr" --json baseRefOid --jq .baseRefOid)
''' + start))
if fix in ('target', 'both'):
    merge = scripts / 'merge-pipeline.sh'
    source = merge.read_text()
    start = '  bureau_ticket_checkpoint "$ISSUE"\n  if [ "$SCAN_RECHECK" = true ]; then\n'
    end = '\n  gh pr merge "$PR_NUMBER" '
    assert source.count(start) == 1
    first = source.index(start) + len(start.splitlines(keepends=True)[0])
    last = source.index(end, first) + 1
    merge.write_text(source[:first] + source[last:])
PY
}

# An actual normal push to the sandbox's file-only origin, then the GitHub
# double observes that remote head. Keep the published approval unchanged.
pr2_push_head() {
  local head
  git -C "$SANDBOX" commit -q --allow-empty -m 'test: next head'
  git -C "$SANDBOX" push -q origin "HEAD:refs/heads/${BUREAU_STUB_BRANCH:-test-branch}"
  head=$(git -C "$SANDBOX" rev-parse HEAD)
  jq --arg h "$head" '.headRefOid = $h' "$PR2_GH/pr.json" > "$PR2_GH/pr.json.tmp" && mv "$PR2_GH/pr.json.tmp" "$PR2_GH/pr.json"
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
