#!/bin/bash
# Verifies the scaffolded CI workflow template ships intact and that the
# installer (SKILL.md) is wired to drop it into an adopting repo.
#
# The template lives at templates/.github/workflows/ci.yml — a .github dir
# UNDER templates/ is deliberately safe: GitHub only reads workflows at the
# repo root, so it never runs as bureau-init's own CI.
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "$0")" && cd .. && pwd)"
CI_TEMPLATE="$REPO_ROOT/templates/.github/workflows/ci.yml"
SKILL="$REPO_ROOT/SKILL.md"

fail=0
pass=0
fail_msgs=()

# assert_grep <label> <pattern> <file>
assert_grep() {
  if grep -qE "$2" "$3" 2>/dev/null; then
    pass=$((pass + 1))
  else
    fail=$((fail + 1))
    fail_msgs+=("$1: pattern '/$2/' not found in $3")
  fi
}

# refute_grep <label> <pattern> <file>  — must NOT match
refute_grep() {
  if grep -qE "$2" "$3" 2>/dev/null; then
    fail=$((fail + 1))
    fail_msgs+=("$1: pattern '/$2/' unexpectedly found in $3")
  else
    pass=$((pass + 1))
  fi
}

# ── 1. Template file exists ────────────────────────────────────────────────
if [ -f "$CI_TEMPLATE" ]; then
  pass=$((pass + 1))
else
  fail=$((fail + 1))
  fail_msgs+=("template file missing: $CI_TEMPLATE")
  # Nothing else is checkable without the file — report and bail.
  echo "passed: $pass  failed: $fail"
  printf '  - %s\n' "${fail_msgs[@]}"
  exit 1
fi

# ── 2. Valid YAML-ish + required shape ─────────────────────────────────────
assert_grep "has name: CI"           '^name: CI'                       "$CI_TEMPLATE"
assert_grep "has runs-on:"           'runs-on:'                        "$CI_TEMPLATE"
# Default runner must be ubuntu-latest (public-repo-safe). Self-hosted is a
# documented opt-in but never the shipped default — red-team audit finding #1
# (fork-PR RCE class) killed the previous self-hosted default.
assert_grep "ubuntu-latest default"  'runs-on: ubuntu-latest'          "$CI_TEMPLATE"
# Least-privilege GITHUB_TOKEN block must be present.
assert_grep "least-priv permissions" 'permissions:'                    "$CI_TEMPLATE"
assert_grep "contents: read"         'contents: read'                  "$CI_TEMPLATE"
# Triggers + hygiene mirrored from the reference workflow.
assert_grep "ready_for_review trigger" 'ready_for_review'             "$CI_TEMPLATE"
assert_grep "concurrency cancel"     'cancel-in-progress: true'        "$CI_TEMPLATE"
assert_grep "draft skip guard"       'github.event.pull_request.draft' "$CI_TEMPLATE"
assert_grep "paths-ignore"           'paths-ignore'                    "$CI_TEMPLATE"
# Security comment covers the fork-PR RCE class for anyone who swaps to
# self-hosted, since that's the failure mode the swap re-introduces.
assert_grep "security block"         'SECURITY:'                       "$CI_TEMPLATE"
assert_grep "fork-PR warning"        'fork PR'                         "$CI_TEMPLATE"

# If python3 + PyYAML happen to be present, do a real parse too (best-effort —
# skipped silently otherwise so the suite stays dependency-light).
if command -v python3 >/dev/null 2>&1 && python3 -c 'import yaml' >/dev/null 2>&1; then
  if python3 -c "import yaml,sys; yaml.safe_load(open('$CI_TEMPLATE'))" >/dev/null 2>&1; then
    pass=$((pass + 1))
  else
    fail=$((fail + 1))
    fail_msgs+=("PyYAML failed to parse $CI_TEMPLATE")
  fi
fi

# ── 3. Installer is wired (SKILL.md) ───────────────────────────────────────
# First-init step drops the template at the adopter's repo-root path.
assert_grep "SKILL first-init cp"  'cp .*templates/\.github/workflows/ci\.yml \./\.github/workflows/ci\.yml' "$SKILL"
assert_grep "SKILL mkdir workflows" 'mkdir -p \./\.github/workflows'         "$SKILL"
# Install prompt makes the default explicit AND names the fork-PR-RCE class
# so operators self-hosting on a public repo see the risk before they flip
# the label.
assert_grep "SKILL runner prompt"  'runs-on: ubuntu-latest'                  "$SKILL"
assert_grep "SKILL fork-PR risk"   'fork-PR-RCE'                             "$SKILL"
# Resync fast path is documented + listed in Usage.
assert_grep "SKILL --resync-ci path"  'Fast path — `--resync-ci`'           "$SKILL"
assert_grep "Usage lists --resync-ci" '/bureau-init --resync-ci'            "$SKILL"

# ── Report ─────────────────────────────────────────────────────────────────
echo "passed: $pass  failed: $fail"
if [ "$fail" -gt 0 ]; then
  printf '  - %s\n' "${fail_msgs[@]}"
  exit 1
fi
echo "OK test_ci_workflow_template"
exit 0
