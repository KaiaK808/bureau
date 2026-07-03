#!/bin/bash
# Cross-check spec task files against open PR branches to detect file conflicts.
set -euo pipefail

SCRIPT_REPO="$(cd "$(dirname "$0")/.." && pwd)"
source "$(dirname "$0")/bureau-config.sh"

if [ -f .env ]; then source .env
elif [ -f "$SCRIPT_REPO/.env" ]; then source "$SCRIPT_REPO/.env"; fi

echo "Scanning open PRs for changed files..."

declare -A PR_FILES_MAP
declare -A PR_LABELS

while IFS=$'\t' read -r pr_number pr_branch pr_title; do
  [ -z "$pr_branch" ] && continue
  PR_LABELS["$pr_branch"]="#${pr_number}: ${pr_title}"
  changed=$(git diff --name-only "origin/main...origin/${pr_branch}" 2>/dev/null || true)
  [ -n "$changed" ] && PR_FILES_MAP["$pr_branch"]="$changed"
done < <(gh pr list --state open --json number,headRefName,title --jq '.[] | [.number, .headRefName, .title] | @tsv' 2>/dev/null || true)

PR_COUNT=${#PR_LABELS[@]}
echo "  Found $PR_COUNT open PRs"

if [ "$PR_COUNT" -eq 0 ]; then
  echo "No open PRs — nothing to cross-check."
  exit 0
fi

if [ -n "${1:-}" ]; then
  TASK_FILES=("$1")
else
  TASK_FILES=()
  while IFS= read -r f; do TASK_FILES+=("$f"); done < <(find $BUREAU_SPECS_DIR/ -name "tasks.md" -type f 2>/dev/null | sort)
fi

if [ ${#TASK_FILES[@]} -eq 0 ]; then
  echo "No tasks.md files found."
  exit 0
fi

echo "  Found ${#TASK_FILES[@]} spec task file(s)"
echo ""

extract_paths() {
  local file="$1"
  grep -oE '`[^`]*`' "$file" 2>/dev/null \
    | sed 's/`//g' \
    | grep -E '(src/|experiments/|scripts/|specs/|design-tokens/|public/|\.tsx?$|\.jsx?$|\.css$|\.json$)' \
    | grep -vE '^\$|^#|npm |docker |git ' \
    | sort -u || true
}

CONFLICTS_FOUND=0
REPORT=""

for tasks_file in "${TASK_FILES[@]}"; do
  spec_dir=$(dirname "$tasks_file")
  spec_name=$(basename "$spec_dir")
  planned_paths=$(extract_paths "$tasks_file")
  [ -z "$planned_paths" ] && continue

  spec_conflicts=""
  for branch in "${!PR_FILES_MAP[@]}"; do
    pr_files="${PR_FILES_MAP[$branch]}"
    pr_label="${PR_LABELS[$branch]}"
    matched_files=""
    while IFS= read -r planned; do
      [ -z "$planned" ] && continue
      # Strip the universal `./` plus any repo-specific path prefix configured
      # in .repo.path_prefix_strip (env override: BUREAU_PATH_PREFIX_STRIP).
      # Useful when specs reference paths with a repo-dir prefix that doesn't
      # appear in PR file lists — e.g. brainhuggers-cli's `brainhuggers-bureau/`.
      # Default empty → no extra stripping. Resolved per-iteration so a wrong
      # config is loud (sed will refuse a malformed pattern).
      _prefix="${BUREAU_PATH_PREFIX_STRIP:-$(jq -r '.repo.path_prefix_strip // empty' "${BUREAU_CONFIG:-.bureau.json}" 2>/dev/null)}"
      if [ -n "$_prefix" ]; then
        clean_planned=$(echo "$planned" | sed -e 's|^\./||' -e "s|^${_prefix}||")
      else
        clean_planned=$(echo "$planned" | sed 's|^\./||')
      fi
      while IFS= read -r pr_file; do
        [ -z "$pr_file" ] && continue
        if [[ "$pr_file" == *"$clean_planned"* ]] || [[ "$clean_planned" == *"$pr_file"* ]]; then
          matched_files="${matched_files}\n    - ${pr_file}"
        fi
      done <<< "$pr_files"
    done <<< "$planned_paths"
    if [ -n "$matched_files" ]; then
      CONFLICTS_FOUND=1
      spec_conflicts="${spec_conflicts}\n  **PR ${pr_label}** (branch: \`${branch}\`):$(echo -e "$matched_files")"
    fi
  done
  if [ -n "$spec_conflicts" ]; then
    REPORT="${REPORT}\n### ${spec_name}\nPlanned files overlap with:$(echo -e "$spec_conflicts")\n"
  fi
done

echo "═══════════════════════════════════════"
echo "  Spec ↔ PR Cross-Check Report"
echo "═══════════════════════════════════════"
echo ""

if [ "$CONFLICTS_FOUND" -eq 0 ]; then
  echo "No conflicts found."
  exit 0
fi

echo -e "File conflicts detected\n"
echo -e "$REPORT"
echo "---"
echo "Coordinate before implementing to avoid merge conflicts."
exit 0
