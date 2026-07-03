#!/bin/bash
# Verifies the REAL branch_is_bureau_only helper in
# templates/scripts/bureau-config.sh against a real git fixture:
#
#   * one commit with a `Co-authored-by: …Claude…` trailer → bureau-only
#   * one human-authored commit (no trailer)                → not bureau-only
#
# Sources the production bureau-config.sh in a minimal sandbox so any drift
# between the stub mirror and the real helper is caught — same pattern as
# test_escalation_log_format.sh.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")" && cd .. && pwd)"
SANDBOX=$(mktemp -d -t bureau-test.bureauonly.XXXXXXXX)
trap 'rm -rf "$SANDBOX"' EXIT

# Minimal .bureau.json so bureau-config.sh's source-time jq reads succeed.
cat > "$SANDBOX/.bureau.json" <<'EOF'
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

# Build a git sandbox with a bare origin so origin/main and origin/<branch> resolve.
git -C "$SANDBOX" init -q -b main
git -C "$SANDBOX" config user.email "test@bureau"
git -C "$SANDBOX" config user.name  "Bureau Test"
git -C "$SANDBOX" commit -q --allow-empty -m "init main"

ORIGIN="$SANDBOX/.fake-origin.git"
git init -q --bare "$ORIGIN"
git -C "$SANDBOX" remote add origin "$ORIGIN"
git -C "$SANDBOX" push -q origin main

cd "$SANDBOX"

# shellcheck disable=SC1091
source "$REPO_ROOT/templates/scripts/bureau-config.sh"

# Case 1: branch with only bureau-authored commits — must return 0.
git checkout -q -b bureau-only-branch
echo "alpha" > a.txt && git add a.txt
git commit -q -m "bureau commit 1

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
echo "beta" > b.txt && git add b.txt
git commit -q -m "bureau commit 2

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
git push -q origin bureau-only-branch
git fetch origin --quiet

if ! branch_is_bureau_only bureau-only-branch; then
  echo "FAIL: bureau-only-branch should be reported bureau-only" >&2
  exit 1
fi

# Case 2: branch with a human commit mixed in — must return 1.
git checkout -q main
git checkout -q -b mixed-branch
echo "gamma" > c.txt && git add c.txt
git commit -q -m "bureau commit

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
echo "delta" > d.txt && git add d.txt
git commit -q -m "human commit — no Claude trailer"
git push -q origin mixed-branch
git fetch origin --quiet

if branch_is_bureau_only mixed-branch; then
  echo "FAIL: mixed-branch should NOT be reported bureau-only" >&2
  exit 1
fi

# Case 3: empty divergence (branch at origin/main) — vacuously bureau-only.
git checkout -q main
git checkout -q -b empty-branch
git push -q origin empty-branch
git fetch origin --quiet

if ! branch_is_bureau_only empty-branch; then
  echo "FAIL: empty divergence should be reported bureau-only (no human commits)" >&2
  exit 1
fi

# Case 4: branch with a merge commit + a spec-artifacts commit (no trailer) +
# Claude-co-authored commits. None of the three should count as human, so the
# branch must be reported bureau-only. This is the EXP-411 / PR #62 case the
# widened predicate is meant to unblock.
git checkout -q main
git checkout -q -b bureau-multi-branch
echo "bureau-multi" > marker.txt && git add marker.txt
git commit -q -m "EXP-411: spec artifacts"  # no Claude trailer (legacy format)
echo "impl" > impl.txt && git add impl.txt
git commit -q -m "implement EXP-411

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"

# Simulate the merge commit produced by merge_origin_main_or_abort: advance
# origin/main with an unrelated commit, then merge it into the branch.
git checkout -q main
echo "main-moved" > main-marker.txt && git add main-marker.txt
git commit -q -m "main moves forward"
git push -q origin main
git checkout -q bureau-multi-branch
git merge -q --no-ff -m "Merge remote-tracking branch 'origin/main' into bureau-multi-branch" main
git push -q origin bureau-multi-branch
git fetch origin --quiet

if ! branch_is_bureau_only bureau-multi-branch; then
  echo "FAIL: bureau-multi-branch (merge commit + spec-artifacts + Claude commit) should be reported bureau-only" >&2
  echo "  human commits printed by helper:" >&2
  _bureau_human_commits bureau-multi-branch | sed 's/^/    /' >&2
  exit 1
fi

# Case 5: human commit with a Claude trailer must still be classified
# correctly — i.e. case-insensitive trailer match works (the awk uses
# IGNORECASE for the trailer check).
git checkout -q main
git checkout -q -b mixed-case-trailer
echo "mc" > mc.txt && git add mc.txt
git commit -q -m "lowercase trailer

co-authored-by: claude <noreply@anthropic.com>"
git push -q origin mixed-case-trailer
git fetch origin --quiet

if ! branch_is_bureau_only mixed-case-trailer; then
  echo "FAIL: lowercase 'co-authored-by: claude' trailer should still count as bureau-authored" >&2
  exit 1
fi

echo "OK test_branch_is_bureau_only"
