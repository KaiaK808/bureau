#!/bin/bash
# Cross-check spec task files against open PR branches to detect file conflicts,
# and say whether that check was complete.
#
# Carried over from slidefactory-core (EXP-1469). Three outcomes, each visible
# in the exit code AND in the result line, which is always the last line on
# stdout:
#   0  clean       the PR list was read, every open PR was compared, no overlap
#   3  conflicts   as clean, but a planned file overlaps a PR's changed files
#   4  incomplete  the PR list, a PR's changed files or the planned paths could
#                  not be read; overlaps found anyway are still reported
#
#   CROSSCHECK RESULT: <clean|conflicts|incomplete> open=<n> compared=<k> paths=<m> unchecked=<#a,#b|->
#
# Any other exit code is an abort without a result line (bash itself exits 1
# or 2, a missing script 127), and crosscheck_open_prs in bureau-config.sh
# treats it as incomplete. The previous version opened with `declare -A`,
# died on macOS's bash 3.2 right there, and the spec stage — which ran it with
# `|| true` and grepped for "conflicts detected" — reported "No file conflicts
# with open PRs" every time.
#
# It changes nothing in the working tree, no local branch, nothing on GitHub
# or Linear. Its only write: a PR branch missing locally is fetched once into
# refs/remotes/origin/<branch>.
#
# Must run under bash 3.2 (/bin/bash on macOS): no associative arrays, and no
# "${arr[@]}" on an array that can be empty under set -u. The records live in
# index arrays under one counter (tests/test_crosscheck.sh runs this file
# under /bin/bash).
#
# Usage: crosscheck-specs.sh [<tasks.md>]
#        Without an argument: every tasks.md under $BUREAU_SPECS_DIR.
set -euo pipefail

SCRIPT_REPO="$(cd "$(dirname "$0")/.." && pwd)"
source "$(dirname "$0")/bureau-config.sh"

if [ -f .env ]; then bureau_load_env --export .env
elif [ -f "$SCRIPT_REPO/.env" ]; then bureau_load_env --export "$SCRIPT_REPO/.env"; fi

extract_paths() {
  local file="$1"
  grep -oE '`[^`]*`' "$file" 2>/dev/null \
    | sed 's/`//g' \
    | grep -E '(src/|experiments/|scripts/|specs/|design-tokens/|public/|\.tsx?$|\.jsx?$|\.css$|\.json$)' \
    | grep -vE '^\$|^#|npm |docker |git ' \
    | sort -u || true
}

# ── Planned paths — read before the PR list, so every outcome can count them ──
TASK_FILES=()
TASK_PATHS=()
n_tasks=0
n_paths=0
planned_error=""
NL=$'\n'
if [ -n "${1:-}" ]; then
  if [ -f "$1" ] && [ -r "$1" ]; then
    TASK_FILES[0]="$1"
    n_tasks=1
  else
    planned_error="Planned paths could not be read: $1"
  fi
else
  found=$(find "$BUREAU_SPECS_DIR" -name tasks.md -type f 2>/dev/null | sort) || found=""
  while IFS= read -r f; do
    if [ -n "$f" ]; then
      TASK_FILES[$n_tasks]="$f"
      n_tasks=$((n_tasks + 1))
    fi
  done <<< "$found"
  if [ "$n_tasks" -eq 0 ]; then
    planned_error="Planned paths could not be read: no tasks.md under $BUREAU_SPECS_DIR"
  fi
fi

t=0
while [ "$t" -lt "$n_tasks" ]; do
  # Checked right before reading: extract_paths swallows a read error, so a
  # tasks.md found by find but unreadable (or gone since) would count as zero
  # planned paths and let the run end clean.
  if [ ! -r "${TASK_FILES[$t]}" ]; then
    planned_error="${planned_error:+$planned_error$NL}Planned paths could not be read: ${TASK_FILES[$t]}"
    TASK_PATHS[$t]=""
    t=$((t + 1))
    continue
  fi
  planned=$(extract_paths "${TASK_FILES[$t]}")
  TASK_PATHS[$t]="$planned"
  if [ -n "$planned" ]; then
    n_paths=$((n_paths + $(printf '%s\n' "$planned" | wc -l)))
  fi
  t=$((t + 1))
done

# ── Open PRs — one record per PR, in the order of the PR list ─────────────────
echo "Scanning open PRs for changed files..."

PR_NUM=()
PR_BRANCH=()
PR_TITLE=()
PR_FILES=()
PR_READ=()
n_prs=0
n_compared=0
unchecked=""
list_error=""
gh_err=$(mktemp "${TMPDIR:-/tmp}/crosscheck-gh.XXXXXX")
trap 'rm -f "$gh_err"' EXIT

# The PR list is read in an `if`, with its stderr kept apart: a failing gh is
# "nothing was compared", never "no open PRs", and none of its messages may
# turn into a PR line.
if pr_tsv=$(gh pr list --state open --json number,headRefName,title --jq '.[] | [.number, .headRefName, .title] | @tsv' 2>"$gh_err"); then
  while IFS=$'\t' read -r pr_number pr_branch pr_title; do
    if [ -z "$pr_branch" ]; then
      continue
    fi
    PR_NUM[$n_prs]="$pr_number"
    PR_BRANCH[$n_prs]="$pr_branch"
    PR_TITLE[$n_prs]="$pr_title"
    PR_FILES[$n_prs]=""
    PR_READ[$n_prs]=0
    n_prs=$((n_prs + 1))
  done <<< "$pr_tsv"
else
  gh_rc=$?
  list_error="Open PRs could not be listed (gh exit $gh_rc) — nothing was compared."
fi

echo "  Found $n_prs open PRs"
echo "  Found $n_tasks spec task file(s)"
echo ""

i=0
while [ "$i" -lt "$n_prs" ]; do
  branch="${PR_BRANCH[$i]}"
  if ! git rev-parse --verify --quiet "refs/remotes/origin/${branch}^{commit}" >/dev/null 2>&1; then
    # Opened after the stage fetched origin: fetch exactly this branch, once.
    # Its exit code is not evaluated — the diff below says whether it worked.
    if git fetch --quiet origin "+refs/heads/${branch}:refs/remotes/origin/${branch}" </dev/null >/dev/null 2>&1; then
      :
    fi
  fi
  if files=$(git diff --name-only "origin/main...origin/${branch}" 2>/dev/null); then
    PR_FILES[$i]="$files"
    PR_READ[$i]=1
    n_compared=$((n_compared + 1))
  else
    unchecked="${unchecked:+$unchecked,}#${PR_NUM[$i]}"
  fi
  i=$((i + 1))
done

# ── Planned paths against changed files — the matching itself is unchanged;
#    it walks the record index of every PR that could be read ────────────────
CONFLICTS_FOUND=0
REPORT=""

t=0
while [ "$t" -lt "$n_tasks" ]; do
  tasks_file="${TASK_FILES[$t]}"
  planned_paths="${TASK_PATHS[$t]}"
  t=$((t + 1))
  spec_dir=$(dirname "$tasks_file")
  spec_name=$(basename "$spec_dir")
  if [ -z "$planned_paths" ]; then
    continue
  fi

  spec_conflicts=""
  i=0
  while [ "$i" -lt "$n_prs" ]; do
    if [ "${PR_READ[$i]}" != 1 ]; then
      i=$((i + 1))
      continue
    fi
    branch="${PR_BRANCH[$i]}"
    pr_files="${PR_FILES[$i]}"
    pr_label="#${PR_NUM[$i]}: ${PR_TITLE[$i]}"
    i=$((i + 1))
    matched_files=""
    while IFS= read -r planned; do
      [ -z "$planned" ] && continue
      # Strip the universal `./` plus any repo-specific path prefix configured
      # in .repo.path_prefix_strip (env override: BUREAU_PATH_PREFIX_STRIP).
      # Useful when specs reference paths with a repo-dir prefix that doesn't
      # appear in PR file lists — e.g. brainhuggers-cli's `brainhuggers-bureau/`.
      # Default empty → no extra stripping. Resolved per-iteration, as before.
      _prefix="${BUREAU_PATH_PREFIX_STRIP:-$(jq -r '.repo.path_prefix_strip // empty' "${BUREAU_CONFIG:-.bureau.json}" 2>/dev/null)}"
      # Both cuts are parameter expansion, and the prefix is quoted inside it:
      # it is removed as literal text, never read as a pattern, and an empty
      # prefix removes nothing. The prefix used to go unquoted into a sed
      # program (`sed -e "s|^${_prefix}||"`), where a value of the form
      # `|<command>; #|e;s|` runs <command> under GNU sed's `e` flag, which
      # every Linux runner has. No external program sees the value now, so
      # nothing here needs escaping.
      clean_planned="${planned#./}"
      clean_planned="${clean_planned#"$_prefix"}"
      while IFS= read -r pr_file; do
        [ -z "$pr_file" ] && continue
        if [[ "$pr_file" == *"$clean_planned"* ]] || [[ "$clean_planned" == *"$pr_file"* ]]; then
          matched_files="${matched_files}${NL}    - ${pr_file}"
        fi
      done <<< "$pr_files"
    done <<< "$planned_paths"
    # Literal newlines instead of `echo -e`: a PR title is text and is never
    # interpreted, not even its backslashes.
    if [ -n "$matched_files" ]; then
      CONFLICTS_FOUND=1
      spec_conflicts="${spec_conflicts}${NL}  **PR ${pr_label}** (branch: \`${branch}\`):${matched_files}"
    fi
  done
  if [ -n "$spec_conflicts" ]; then
    REPORT="${REPORT}${NL}### ${spec_name}${NL}Planned files overlap with:${spec_conflicts}${NL}"
  fi
done

# ── Report, result line and exit code: one place, at the very end — never in
#    a trap, so an abort leaves no result line behind ──────────────────────────
finish() {
  local result code n
  if [ -n "$list_error" ] || [ -n "$planned_error" ] || [ -n "$unchecked" ]; then
    result="incomplete"
    code=4
  elif [ "$CONFLICTS_FOUND" -eq 1 ]; then
    result="conflicts"
    code=3
  else
    result="clean"
    code=0
  fi

  echo "═══════════════════════════════════════"
  echo "  Spec ↔ PR Cross-Check Report"
  echo "═══════════════════════════════════════"
  echo ""

  if [ "$CONFLICTS_FOUND" -eq 1 ]; then
    echo "File conflicts detected"
    echo ""
    printf '%s\n' "$REPORT"
    echo "---"
    echo "Coordinate before implementing to avoid merge conflicts."
  fi
  if [ -n "$unchecked" ]; then
    echo "Not checked — changed files could not be read:"
    n=0
    while [ "$n" -lt "$n_prs" ]; do
      if [ "${PR_READ[$n]}" != 1 ]; then
        echo "  PR #${PR_NUM[$n]}: ${PR_TITLE[$n]} (branch: \`${PR_BRANCH[$n]}\`)"
      fi
      n=$((n + 1))
    done
  fi
  if [ -n "$list_error" ]; then
    echo "$list_error"
    head -n 3 "$gh_err" | sed 's/^/  gh: /'
  fi
  if [ -n "$planned_error" ]; then
    echo "$planned_error"
  fi
  if [ "$result" = "clean" ]; then
    echo "No conflicts found ($n_compared open PRs compared against $n_paths planned paths)."
    if [ "$n_paths" -eq 0 ]; then
      echo "No planned paths were recognised in the tasks file — nothing was compared."
    fi
  fi
  echo ""
  echo "CROSSCHECK RESULT: $result open=$n_prs compared=$n_compared paths=$n_paths unchecked=${unchecked:--}"
  exit "$code"
}

finish
