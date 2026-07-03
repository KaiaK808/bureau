#!/usr/bin/env bash
# upstream-port.sh — fast-path cherry-pick from a configured upstream repo.
#
# Skips the full shepherd ceremony (spec / spec-review / ux / implement × N /
# qa / code-review × 3) for trivial upstream ports where the referenced code
# already exists on this fork. The script is intentionally mechanical;
# loud, classified failure is the design contract.
#
# CONFIGURATION
#   `.bureau.json` keys (all optional — defaults preserve the original
#   brainhuggers-cli setup for backwards compatibility):
#     repo.upstream                  — upstream repo as `owner/name`
#                                      (default: ultraworkers/claw-code).
#                                      Env override: BUREAU_UPSTREAM_REPO.
#     repo.upstream_port.build_cmd   — shell command for the release-build
#                                      gate (default: brainhuggers-cli cargo
#                                      build). Env: BUREAU_UPSTREAM_PORT_BUILD.
#     repo.upstream_port.test_cmd    — shell command for the workspace tests
#                                      (default: cargo test --workspace).
#                                      Env: BUREAU_UPSTREAM_PORT_TEST.
#     repo.upstream_port.work_dir    — working directory the build + test
#                                      commands run inside (default:
#                                      ${SCRIPT_DIR}/../rust). Env:
#                                      BUREAU_UPSTREAM_PORT_WORK_DIR.
#
# SYNOPSIS
#   scripts/upstream-port.sh --sha <upstream-sha> [--with-llm [--yes]]
#   scripts/upstream-port.sh --pr  <upstream-pr-number> [--with-llm [--yes]]
#   scripts/upstream-port.sh --help
#
# Exactly one of --sha / --pr is required. --help is mutually exclusive.
#
# ARGUMENTS
#   --sha <upstream-sha>   7-to-40-char lowercase hex SHA reachable on the
#                          configured upstream via `gh api .../commits/<sha>`.
#                          Validated against ^[0-9a-f]{7,40}$.
#   --pr  <pr-number>      Positive integer of a *merged* PR on the configured
#                          upstream. Resolved to its merge-commit OID via
#                          `gh pr view --json mergeCommit`.
#   --with-llm             On `git apply --3way` conflict, invoke Claude ONCE
#                          to resolve. Off by default (exit 17 as before).
#                          Prints a token estimate and prompts before calling
#                          (TTY) or requires --yes to proceed (non-TTY).
#                          PR title gets a "(LLM-assisted)" marker. Uses
#                          claude_cmd_for_stage "upstream_port" — pin a model
#                          via .agents.upstream_port.model in .bureau.json
#                          (haiku is a good default — translation is
#                          mechanical, not creative).
#   --yes, -y              Skip the cost-gate confirmation prompt. Required
#                          when --with-llm is used non-interactively.
#   --help                 Print this usage and exit 0.
#
# EXIT CODES (subset of scripts/bureau-config.sh::exit_class — kept inline
# because bash has no enum; numeric literals are the integration contract,
# the comment header is the cross-reference)
#   0   ok               port succeeded; PR URL on stdout
#   14  build-failed     configured build_cmd exited non-zero (default:
#                        `cargo build --release -p brainhuggers-cli`)
#   15  no-pr            configured test_cmd exited non-zero (default:
#                        `cargo test --workspace --no-fail-fast`)
#   17  rebase-needed    `git apply --3way` produced conflicts
#   18  gh-failed        gh API call OR pre-flight guard (args, dirty tree,
#                        gh auth, branch exists, push, PR create) failed
#
# EXAMPLE
#   ./scripts/upstream-port.sh --sha 53953a8
#   ./scripts/upstream-port.sh --pr  3024
#
# Cross-reference: scripts/bureau-config.sh::exit_class — single source of
# truth for the {14, 15, 17, 18} mapping; do not invent new codes here.

set -euo pipefail

readonly EXIT_OK=0
readonly EXIT_BUILD_FAILED=14
readonly EXIT_TEST_FAILED=15
readonly EXIT_CONFLICT=17
readonly EXIT_GH_FAILED=18

# Upstream repo resolution. Order: BUREAU_UPSTREAM_REPO env > .repo.upstream
# JSON > legacy default (preserves the brainhuggers-cli setup). Resolved once
# at startup — operators who flip it mid-run should expect the next invocation
# to pick up the change.
UPSTREAM_REPO="${BUREAU_UPSTREAM_REPO:-$(jq -r '.repo.upstream // empty' "${BUREAU_CONFIG:-.bureau.json}" 2>/dev/null)}"
[ -z "$UPSTREAM_REPO" ] && UPSTREAM_REPO="ultraworkers/claw-code"
readonly UPSTREAM_REPO
readonly BRANCH_PREFIX="upstream-port"
readonly PIPELINE_NAME="upstream-port"

# Source bureau-config.sh for exit_class + optional alert_telegram. The helper
# defines alert_telegram as a silent no-op when TELEGRAM_BOT_TOKEN /
# TELEGRAM_ALERT_CHAT_ID are absent, so wiring it is always safe.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly SCRIPT_DIR
BUREAU_CONFIG_SH="${SCRIPT_DIR}/bureau-config.sh"
if [ ! -f "$BUREAU_CONFIG_SH" ]; then
  echo "ERROR: required helper ${BUREAU_CONFIG_SH} is missing" >&2
  exit "$EXIT_GH_FAILED"
fi
# shellcheck source=scripts/bureau-config.sh
# shellcheck disable=SC1091
source "$BUREAU_CONFIG_SH"

# Ephemeral tempfiles. Registered for cleanup on EXIT regardless of which path
# the script exits through (success, guard failure, conflict, build/test fail).
TMP_DIFF="$(mktemp -t upstream-port-diff.XXXXXX)"
TMP_BUILD_LOG="$(mktemp -t upstream-port-build.XXXXXX)"
TMP_TEST_LOG="$(mktemp -t upstream-port-test.XXXXXX)"
readonly TMP_DIFF TMP_BUILD_LOG TMP_TEST_LOG
trap 'rm -f "$TMP_DIFF" "$TMP_BUILD_LOG" "$TMP_TEST_LOG"' EXIT

log_step() {
  printf '==> %s\n' "$*" >&2
}

# Single funnel for every non-zero exit path. Fires the throttled Telegram
# alert when configured (alert_telegram is a no-op without env wiring per
# bureau-config.sh) and exits with the requested code.
on_failure() {
  local code="$1" step_label="$2"
  if declare -F alert_telegram >/dev/null 2>&1; then
    alert_telegram "upstream-port" "$PIPELINE_NAME" "$code" \
      "step=${step_label} sha=${FULL_SHA:-unresolved}" "" || true
  fi
  exit "$code"
}

# llm_resolve_or_abort: invoked when git apply --3way produced conflicts AND
# --with-llm was passed. One-shot Claude call to translate upstream intent
# against local code. On success: stages resolved files, sets LLM_ASSISTED=1,
# returns 0 — caller continues into build/test/commit. On any failure: hard-
# resets the worktree to HEAD and exits EXIT_CONFLICT via on_failure (so a
# failed LLM run is indistinguishable from "didn't pass --with-llm" from the
# caller's perspective — same exit code, same Telegram alert class).
#
# Security note: this path uses `claude_cmd_for_stage upstream_port`, which
# resolves to `claude -p --print --dangerously-skip-permissions ...`. The
# operator's explicit --with-llm opt-in covers the implication that an
# upstream-controlled commit could direct Claude to use its Edit/Write tools.
# Don't pass --with-llm on upstream commits you wouldn't trust to merge.
#
# Globals it reads: $TMP_DIFF, $FULL_SHA, $UPSTREAM_TITLE, $UPSTREAM_REPO,
# $SKIP_CONFIRM. Globals it sets: LLM_ASSISTED.
llm_resolve_or_abort() {
  local conflicts
  conflicts="$(git diff --name-only --diff-filter=U 2>/dev/null || true)"
  if [ -z "$conflicts" ]; then
    # Defensive: caller already verified conflicts exist. If they don't now,
    # apply must have failed for some other reason — bail loud.
    echo "ERROR: --with-llm: git apply failed but no unmerged paths found" >&2
    git reset --hard HEAD >/dev/null 2>&1 || true
    on_failure "$EXIT_CONFLICT" "llm no unmerged paths"
  fi

  local conflicts_listing
  conflicts_listing="$(printf '%s\n' "$conflicts" | sed 's/^/  - /')"

  local upstream_diff
  upstream_diff="$(cat "$TMP_DIFF")"

  # Build the prompt. Claude reads the conflicted files via its Read tool —
  # we don't inline file contents (would double the token budget). The full
  # upstream diff is the source-of-truth for upstream intent; we DO inline it.
  local prompt
  prompt="You are translating an upstream commit into local code. The upstream and local repos share lineage but have diverged. \`git apply --3way --index\` applied what it could and left conflict markers in the files listed below.

Upstream commit: ${FULL_SHA}
Upstream title: ${UPSTREAM_TITLE}
Upstream repo: ${UPSTREAM_REPO}

Conflicted files (work ONLY on these):
${conflicts_listing}

Your task:
1. Read each conflicted file. It currently contains <<<<<<<, =======, >>>>>>> markers.
2. Read the upstream diff below. Understand what each upstream hunk was trying to achieve.
3. Edit each conflicted file (Edit tool) to remove ALL conflict markers and apply the upstream intent to the local code, preserving local code's intentional divergences (renames, omitted files, refactored APIs).
4. Do NOT create new files. Do NOT touch files outside the conflicted list. Do NOT run build or test commands — the calling script will do that.
5. If you cannot resolve a file confidently — the upstream change has no sensible local analog — LEAVE its markers in place. The caller will detect that and abort. Do not guess.

When done, no file in the conflicted list should contain <<<<<<<, =======, or >>>>>>> on a line of its own.

Upstream diff:
${upstream_diff}

Begin."

  local prompt_chars estimated_tokens
  prompt_chars=$(printf '%s' "$prompt" | wc -c | tr -d ' ')
  estimated_tokens=$((prompt_chars / 4))
  local conflict_count
  conflict_count=$(printf '%s\n' "$conflicts" | wc -l | tr -d ' ')

  echo "==> --with-llm: ${conflict_count} conflicted file(s), estimated prompt ~${estimated_tokens} tokens (upper bound)" >&2

  if [ "$SKIP_CONFIRM" = "0" ]; then
    if [ ! -t 0 ]; then
      echo "ERROR: --with-llm with non-interactive stdin requires --yes (refuse to spend silently)" >&2
      git reset --hard HEAD >/dev/null 2>&1 || true
      on_failure "$EXIT_CONFLICT" "llm cost-gate non-tty"
    fi
    local resp=""
    read -rp "Proceed with LLM-assisted resolution? [y/N] " resp || resp=""
    case "$resp" in
      y|Y|yes|YES) ;;
      *)
        echo "==> aborting per operator response" >&2
        git reset --hard HEAD >/dev/null 2>&1 || true
        on_failure "$EXIT_CONFLICT" "llm declined by operator"
        ;;
    esac
  fi

  local claude_cmd
  claude_cmd="$(claude_cmd_for_stage upstream_port)"
  if [ -z "$claude_cmd" ]; then
    echo "ERROR: --with-llm: claude_cmd_for_stage returned empty (no model resolved and no fallback)" >&2
    git reset --hard HEAD >/dev/null 2>&1 || true
    on_failure "$EXIT_CONFLICT" "llm no claude command"
  fi

  log_step "invoking ${claude_cmd%% *} for conflict resolution (5min cap)"
  local timeout_bin=""
  if command -v timeout >/dev/null 2>&1; then
    timeout_bin="timeout 300s"
  elif command -v gtimeout >/dev/null 2>&1; then
    timeout_bin="gtimeout 300s"
  fi
  # shellcheck disable=SC2086
  if ! $timeout_bin $claude_cmd "$prompt" >/dev/null 2>&1; then
    echo "==> claude invocation failed or timed out — reverting" >&2
    git reset --hard HEAD >/dev/null 2>&1 || true
    on_failure "$EXIT_CONFLICT" "llm invocation failed"
  fi

  # Verify resolution: no <<<<<<<, =======, or >>>>>>> on a line of its own
  # in any conflicted file. Anchored to line-start so source code that
  # legitimately contains the substring (e.g. a string literal) doesn't trip.
  local remaining=""
  local f
  for f in $conflicts; do
    if [ -f "$f" ] && grep -qE '^(<<<<<<<|=======|>>>>>>>)( |$)' "$f"; then
      remaining="${remaining}${f}"$'\n'
    fi
  done
  if [ -n "$remaining" ]; then
    echo "==> LLM left conflict markers in:" >&2
    printf '  %s\n' $remaining >&2
    echo "==> reverting" >&2
    git reset --hard HEAD >/dev/null 2>&1 || true
    on_failure "$EXIT_CONFLICT" "llm did not resolve all conflicts"
  fi

  # Stage the LLM's resolutions. `git add -u` picks up modifications to
  # tracked files but won't add new ones — matches the prompt's "do not
  # create new files" instruction.
  if ! git add -u >/dev/null 2>&1; then
    git add -u >&2 || true
    git reset --hard HEAD >/dev/null 2>&1 || true
    on_failure "$EXIT_GH_FAILED" "git add -u after llm"
  fi

  # Final check: index has nothing unmerged.
  local still_unmerged
  still_unmerged="$(git diff --name-only --diff-filter=U 2>/dev/null || true)"
  if [ -n "$still_unmerged" ]; then
    echo "==> still unmerged after git add -u:" >&2
    printf '  %s\n' $still_unmerged >&2
    git reset --hard HEAD >/dev/null 2>&1 || true
    on_failure "$EXIT_CONFLICT" "llm post-stage still unmerged"
  fi

  LLM_ASSISTED=1
  log_step "LLM-assisted resolution complete (${conflict_count} file(s) resolved)"
}

# --------------------------------------------------------------------------
# Usage block — kept here so --help can print it. Mirrors the header comment
# above and contracts/upstream-port-cli.md (which is canonical per FR-015).
# --------------------------------------------------------------------------
print_usage() {
  cat <<'EOF'
upstream-port.sh — fast-path cherry-pick from ultraworkers/claw-code.

USAGE
  scripts/upstream-port.sh --sha <upstream-sha> [--with-llm [--yes]]
  scripts/upstream-port.sh --pr  <upstream-pr-number> [--with-llm [--yes]]
  scripts/upstream-port.sh --help

Exactly one of --sha / --pr is required.

ARGUMENTS
  --sha <upstream-sha>   7-to-40-char lowercase hex SHA on
                         ultraworkers/claw-code. Validated against
                         ^[0-9a-f]{7,40}$.
  --pr  <pr-number>      Positive integer of a *merged* PR on
                         ultraworkers/claw-code. Resolved to its merge-commit
                         OID via `gh pr view --json mergeCommit`.
  --with-llm             On 3-way conflict, invoke Claude ONCE to resolve
                         instead of exiting 17. Off by default. Prints a
                         token estimate and prompts before calling (TTY)
                         or requires --yes (non-TTY). Resulting PR title
                         carries a "(LLM-assisted)" marker.
  --yes, -y              Skip the cost-gate confirm. Required for non-TTY
                         --with-llm runs.
  --help                 Print this usage and exit 0.

EXIT CODES
  0   ok               port succeeded; PR URL on stdout
  14  build-failed     cargo build --release -p brainhuggers-cli failed
  15  no-pr            cargo test --workspace --no-fail-fast failed
  17  rebase-needed    git apply --3way produced conflicts (and either
                       --with-llm was off, or LLM failed to resolve)
  18  gh-failed        gh API call OR pre-flight guard failed

EXAMPLE
  ./scripts/upstream-port.sh --sha 53953a8
  ./scripts/upstream-port.sh --pr  3024
  ./scripts/upstream-port.sh --sha 53953a8 --with-llm
  ./scripts/upstream-port.sh --pr  3024 --with-llm --yes
EOF
}

# --------------------------------------------------------------------------
# argv parsing — mutually exclusive --sha / --pr, --help short-circuits.
# --------------------------------------------------------------------------
SHA_ARG=""
PR_ARG=""
SHOW_HELP=0
WITH_LLM=0
SKIP_CONFIRM=0
LLM_ASSISTED=0

while [ "$#" -gt 0 ]; do
  case "$1" in
    --sha)
      [ -n "${2:-}" ] || { echo "ERROR: --sha requires a value" >&2; print_usage >&2; exit "$EXIT_GH_FAILED"; }
      SHA_ARG="$2"
      shift 2
      ;;
    --pr)
      [ -n "${2:-}" ] || { echo "ERROR: --pr requires a value" >&2; print_usage >&2; exit "$EXIT_GH_FAILED"; }
      PR_ARG="$2"
      shift 2
      ;;
    --with-llm)
      WITH_LLM=1
      shift
      ;;
    --yes|-y)
      SKIP_CONFIRM=1
      shift
      ;;
    --help|-h)
      SHOW_HELP=1
      shift
      ;;
    *)
      echo "ERROR: unknown argument: $1" >&2
      print_usage >&2
      exit "$EXIT_GH_FAILED"
      ;;
  esac
done

if [ "$SHOW_HELP" = "1" ]; then
  print_usage
  exit "$EXIT_OK"
fi

if [ -n "$SHA_ARG" ] && [ -n "$PR_ARG" ]; then
  echo "ERROR: --sha and --pr are mutually exclusive" >&2
  print_usage >&2
  exit "$EXIT_GH_FAILED"
fi

if [ -z "$SHA_ARG" ] && [ -z "$PR_ARG" ]; then
  echo "ERROR: exactly one of --sha or --pr is required" >&2
  print_usage >&2
  exit "$EXIT_GH_FAILED"
fi

# Forward-declare FULL_SHA so on_failure can reference it before resolution.
FULL_SHA=""

# --------------------------------------------------------------------------
# Step 1 — gh auth guard.
# --------------------------------------------------------------------------
log_step "validating gh auth"
if ! gh auth status >/dev/null 2>&1; then
  gh auth status >&2 || true
  echo "ERROR: gh CLI is not authenticated — run 'gh auth login'" >&2
  on_failure "$EXIT_GH_FAILED" "gh auth"
fi
log_step "gh auth ok"

# --------------------------------------------------------------------------
# Step 2 — worktree-clean guard.
# --------------------------------------------------------------------------
log_step "checking working tree"
DIRTY_FILES="$(git status --porcelain 2>/dev/null || true)"
if [ -n "$DIRTY_FILES" ]; then
  echo "ERROR: working tree has uncommitted changes — refuse to proceed" >&2
  printf '%s\n' "$DIRTY_FILES" >&2
  on_failure "$EXIT_GH_FAILED" "working tree dirty"
fi
log_step "working tree clean"

# --------------------------------------------------------------------------
# Step 3 / 4 — resolve --pr → SHA, or validate + canonicalise --sha.
# --------------------------------------------------------------------------
if [ -n "$PR_ARG" ]; then
  if ! [[ "$PR_ARG" =~ ^[1-9][0-9]*$ ]]; then
    echo "ERROR: --pr must be a positive integer; got \"$PR_ARG\"" >&2
    on_failure "$EXIT_GH_FAILED" "invalid --pr format"
  fi
  log_step "resolving upstream PR #${PR_ARG} → merge commit"
  PR_MERGE_OID="$(gh pr view "$PR_ARG" --repo "$UPSTREAM_REPO" \
    --json mergeCommit --jq '.mergeCommit.oid // empty' 2>&1)" || {
    echo "ERROR: gh pr view failed for PR #${PR_ARG}" >&2
    printf '%s\n' "$PR_MERGE_OID" >&2
    on_failure "$EXIT_GH_FAILED" "gh pr view"
  }
  if [ -z "$PR_MERGE_OID" ] || [ "$PR_MERGE_OID" = "null" ]; then
    echo "ERROR: PR #${PR_ARG} on ${UPSTREAM_REPO} is not merged" >&2
    on_failure "$EXIT_GH_FAILED" "pr ${PR_ARG} not merged"
  fi
  FULL_SHA="$PR_MERGE_OID"
else
  if ! [[ "$SHA_ARG" =~ ^[0-9a-f]{7,40}$ ]]; then
    echo "ERROR: --sha must match ^[0-9a-f]{7,40}$; got \"$SHA_ARG\"" >&2
    on_failure "$EXIT_GH_FAILED" "invalid --sha format"
  fi
  log_step "resolving upstream/${SHA_ARG} → canonical SHA"
  if ! FULL_SHA="$(gh api "repos/${UPSTREAM_REPO}/commits/${SHA_ARG}" \
    --jq '.sha' 2>&1)"; then
    echo "ERROR: gh api could not resolve upstream/${SHA_ARG}" >&2
    printf '%s\n' "$FULL_SHA" >&2
    FULL_SHA=""
    on_failure "$EXIT_GH_FAILED" "gh api commits/${SHA_ARG}"
  fi
fi

# At this point FULL_SHA must be a 40-char lowercase hex string.
if ! [[ "$FULL_SHA" =~ ^[0-9a-f]{40}$ ]]; then
  echo "ERROR: resolved canonical SHA is not 40 hex chars: \"$FULL_SHA\"" >&2
  on_failure "$EXIT_GH_FAILED" "canonical sha invalid"
fi

# Fetch upstream commit title (first line of commit message). Truncate to a
# ~80-char budget so the commit subject + "port: upstream/<short_sha> " prefix
# stays legible in PR-list views and `git log --oneline`.
log_step "fetching upstream commit title"
UPSTREAM_TITLE="$(gh api "repos/${UPSTREAM_REPO}/commits/${FULL_SHA}" \
  --jq '.commit.message' 2>&1 | head -n 1)" || {
  echo "ERROR: gh api could not fetch commit message for ${FULL_SHA}" >&2
  on_failure "$EXIT_GH_FAILED" "gh api commit message"
}
if [ -z "$UPSTREAM_TITLE" ]; then
  echo "ERROR: upstream commit ${FULL_SHA} has an empty title" >&2
  on_failure "$EXIT_GH_FAILED" "empty upstream title"
fi
SHORT_SHA="${FULL_SHA:0:7}"
# Subject budget: 80 chars total. `port: upstream/<7-char-sha> ` = 22 chars.
# Leaves 58 chars for the upstream title; truncate with `…` if needed.
SUBJECT_PREFIX="port: upstream/${SHORT_SHA} "
TITLE_BUDGET=$((80 - ${#SUBJECT_PREFIX}))
if [ "${#UPSTREAM_TITLE}" -gt "$TITLE_BUDGET" ]; then
  UPSTREAM_TITLE_TRUNCATED="${UPSTREAM_TITLE:0:$((TITLE_BUDGET - 1))}…"
else
  UPSTREAM_TITLE_TRUNCATED="$UPSTREAM_TITLE"
fi
SUBJECT="${SUBJECT_PREFIX}${UPSTREAM_TITLE_TRUNCATED}"

# --------------------------------------------------------------------------
# Step 5 — branch-collision guard.
# --------------------------------------------------------------------------
BRANCH="${BRANCH_PREFIX}/${SHORT_SHA}"
log_step "checking ${BRANCH} is absent locally and on origin"
if git rev-parse --verify "refs/heads/${BRANCH}" >/dev/null 2>&1; then
  echo "ERROR: branch ${BRANCH} already exists locally — delete it first:" >&2
  echo "  git branch -D ${BRANCH}" >&2
  on_failure "$EXIT_GH_FAILED" "branch ${BRANCH} exists locally"
fi
if git ls-remote --exit-code origin "refs/heads/${BRANCH}" >/dev/null 2>&1; then
  echo "ERROR: branch ${BRANCH} already exists on origin — delete it first:" >&2
  echo "  git push origin --delete ${BRANCH}" >&2
  on_failure "$EXIT_GH_FAILED" "branch ${BRANCH} exists on origin"
fi

# --------------------------------------------------------------------------
# Steps 6–7 — fresh base, branch off origin/main.
# --------------------------------------------------------------------------
log_step "git fetch origin --prune"
if ! git fetch origin --prune >/dev/null 2>&1; then
  git fetch origin --prune >&2 || true
  on_failure "$EXIT_GH_FAILED" "git fetch origin"
fi
log_step "git checkout -b ${BRANCH} origin/main"
if ! git checkout -b "$BRANCH" origin/main >/dev/null 2>&1; then
  git checkout -b "$BRANCH" origin/main >&2 || true
  on_failure "$EXIT_GH_FAILED" "git checkout -b"
fi

# --------------------------------------------------------------------------
# Step 8 — fetch upstream diff via gh api.
# --------------------------------------------------------------------------
log_step "fetching upstream diff for ${FULL_SHA}"
if ! gh api -H "Accept: application/vnd.github.v3.diff" \
  "repos/${UPSTREAM_REPO}/commits/${FULL_SHA}" >"$TMP_DIFF" 2>/tmp/upstream-port-gh-err.$$; then
  cat /tmp/upstream-port-gh-err.$$ >&2 || true
  rm -f /tmp/upstream-port-gh-err.$$
  on_failure "$EXIT_GH_FAILED" "gh api diff"
fi
rm -f /tmp/upstream-port-gh-err.$$

# --------------------------------------------------------------------------
# Step 8.5 — optional path translation (EXP-629).
# --------------------------------------------------------------------------
# If <repo>/.bureau-port-map.json exists, rewrite the fetched diff before
# applying. Lets a downstream repo absorb upstream renames (e.g.
# `claw-cli` ↔ `rusty-claude-cli`) and drop upstream-only files
# (e.g. ROADMAP.md) without manual diff editing every port.
#
# Schema:
#   { "version": 1,
#     "paths":      { "upstream/prefix/": "local/prefix/" },
#     "drop_paths": [ "exact/file", "prefix/to/drop/" ] }
#
# `paths`: prefix substitution applied only to header lines (diff --git,
# --- a/, +++ b/, rename from/to, copy from/to). Literal substring match —
# no regex semantics, no escaping required for the operator. Iteration is
# jq's insertion order; first-match wins per line.
#
# `drop_paths`: file sections whose a/ OR b/ path matches are stripped from
# the diff. Trailing `/` = prefix match; otherwise exact match.
#
# Missing/empty file is a no-op — behaviour identical to before this step.
PORT_MAP="${SCRIPT_DIR}/../.bureau-port-map.json"
if [ -f "$PORT_MAP" ]; then
  if ! jq -e . "$PORT_MAP" >/dev/null 2>&1; then
    echo "ERROR: $PORT_MAP is not valid JSON" >&2
    on_failure "$EXIT_GH_FAILED" "invalid .bureau-port-map.json"
  fi
  log_step "applying .bureau-port-map.json transforms"

  # Path substitutions — read each "from\tto" pair into the loop.
  while IFS=$'\t' read -r from to; do
    [ -z "$from" ] && continue
    [ -z "$to" ] && continue
    hits=$(awk -v from="$from" '
      /^diff --git a\// {
        n_parts = split($0, parts, " ")
        if (n_parts >= 4) {
          a = substr(parts[3], 3); b = substr(parts[4], 3)
          if (substr(a, 1, length(from)) == from || substr(b, 1, length(from)) == from) hits++
        }
      }
      END { print hits+0 }
    ' "$TMP_DIFF")
    echo "    paths: ${from} → ${to} (${hits} file section(s))"
    awk -v from="$from" -v to="$to" '
      function repl(s) {
        if (substr(s, 1, length(from)) == from) return to substr(s, length(from)+1)
        return s
      }
      /^diff --git a\// {
        n_parts = split($0, parts, " ")
        if (n_parts >= 4) {
          a = substr(parts[3], 3); b = substr(parts[4], 3)
          print "diff --git a/" repl(a) " b/" repl(b)
          next
        }
      }
      /^--- a\// { print "--- a/" repl(substr($0, 7)); next }
      /^\+\+\+ b\// { print "+++ b/" repl(substr($0, 7)); next }
      /^rename from / { print "rename from " repl(substr($0, 13)); next }
      /^rename to / { print "rename to " repl(substr($0, 11)); next }
      /^copy from / { print "copy from " repl(substr($0, 11)); next }
      /^copy to / { print "copy to " repl(substr($0, 9)); next }
      { print }
    ' "$TMP_DIFF" > "${TMP_DIFF}.new"
    mv "${TMP_DIFF}.new" "$TMP_DIFF"
  done < <(jq -r '.paths // {} | to_entries[] | "\(.key)\t\(.value)"' "$PORT_MAP")

  # Drop paths — strip entire file sections.
  while IFS= read -r drop; do
    [ -z "$drop" ] && continue
    is_prefix=0
    [ -z "${drop##*/}" ] && is_prefix=1
    hits=$(awk -v drop="$drop" -v is_prefix="$is_prefix" '
      /^diff --git a\// {
        n_parts = split($0, parts, " ")
        if (n_parts >= 4) {
          a = substr(parts[3], 3); b = substr(parts[4], 3)
          if (is_prefix == 1) {
            if (substr(a, 1, length(drop)) == drop || substr(b, 1, length(drop)) == drop) hits++
          } else {
            if (a == drop || b == drop) hits++
          }
        }
      }
      END { print hits+0 }
    ' "$TMP_DIFF")
    echo "    drop:  ${drop} (${hits} file section(s))"
    awk -v drop="$drop" -v is_prefix="$is_prefix" '
      /^diff --git a\// {
        in_drop = 0
        n_parts = split($0, parts, " ")
        if (n_parts >= 4) {
          a = substr(parts[3], 3); b = substr(parts[4], 3)
          if (is_prefix == 1) {
            if (substr(a, 1, length(drop)) == drop || substr(b, 1, length(drop)) == drop) in_drop = 1
          } else {
            if (a == drop || b == drop) in_drop = 1
          }
        }
      }
      !in_drop { print }
    ' "$TMP_DIFF" > "${TMP_DIFF}.new"
    mv "${TMP_DIFF}.new" "$TMP_DIFF"
  done < <(jq -r '.drop_paths // [] | .[]' "$PORT_MAP")
fi

# --------------------------------------------------------------------------
# Step 9 — apply diff with git apply --3way.
# --------------------------------------------------------------------------
log_step "git apply --3way --index"
if ! git apply --3way --index "$TMP_DIFF" 2>/tmp/upstream-port-apply-err.$$; then
  cat /tmp/upstream-port-apply-err.$$ >&2 || true
  rm -f /tmp/upstream-port-apply-err.$$
  echo "==> git apply --3way produced conflicts" >&2
  CONFLICTS="$(git diff --name-only --diff-filter=U 2>/dev/null || true)"
  if [ -n "$CONFLICTS" ]; then
    echo "conflicted files:" >&2
    printf '  %s\n' $CONFLICTS >&2
  fi
  if [ "$WITH_LLM" = "1" ]; then
    # Dispatches to LLM-assisted resolution. Either returns 0 with conflicts
    # cleared + LLM_ASSISTED=1, or calls on_failure itself (which exits).
    llm_resolve_or_abort
  else
    echo "resolve manually and re-run, or escalate to a full shepherd ticket (or retry with --with-llm)" >&2
    on_failure "$EXIT_CONFLICT" "git apply --3way"
  fi
fi
rm -f /tmp/upstream-port-apply-err.$$

# --------------------------------------------------------------------------
# Step 9.5 — recompute subject if LLM-assisted, to add the marker.
# --------------------------------------------------------------------------
# The "(LLM-assisted)" tag in the PR title makes it visible to reviewers and
# downstream tooling that the diff was not produced by a plain git apply.
# Recomputed (not patched) so the title-budget truncation stays correct.
if [ "$LLM_ASSISTED" = "1" ]; then
  SUBJECT_PREFIX="port: upstream/${SHORT_SHA} (LLM-assisted) "
  TITLE_BUDGET=$((80 - ${#SUBJECT_PREFIX}))
  if [ "${#UPSTREAM_TITLE}" -gt "$TITLE_BUDGET" ]; then
    UPSTREAM_TITLE_TRUNCATED="${UPSTREAM_TITLE:0:$((TITLE_BUDGET - 1))}…"
  else
    UPSTREAM_TITLE_TRUNCATED="$UPSTREAM_TITLE"
  fi
  SUBJECT="${SUBJECT_PREFIX}${UPSTREAM_TITLE_TRUNCATED}"
fi

# --------------------------------------------------------------------------
# Step 9.75 — resolve configurable build / test / work_dir.
# --------------------------------------------------------------------------
# Defaults are the original brainhuggers-cli cargo invocations so existing
# adopters keep working unchanged. Operators with a different stack point
# .repo.upstream_port.{build_cmd,test_cmd,work_dir} at their own commands.
# Env vars (BUREAU_UPSTREAM_PORT_BUILD / _TEST / _WORK_DIR) win over JSON.
PORT_BUILD_CMD="${BUREAU_UPSTREAM_PORT_BUILD:-$(jq -r '.repo.upstream_port.build_cmd // empty' "${BUREAU_CONFIG:-.bureau.json}" 2>/dev/null)}"
PORT_TEST_CMD="${BUREAU_UPSTREAM_PORT_TEST:-$(jq -r '.repo.upstream_port.test_cmd // empty' "${BUREAU_CONFIG:-.bureau.json}" 2>/dev/null)}"
PORT_WORK_DIR="${BUREAU_UPSTREAM_PORT_WORK_DIR:-$(jq -r '.repo.upstream_port.work_dir // empty' "${BUREAU_CONFIG:-.bureau.json}" 2>/dev/null)}"
[ -z "$PORT_BUILD_CMD" ] && PORT_BUILD_CMD='cargo build --release -p brainhuggers-cli'
[ -z "$PORT_TEST_CMD" ]  && PORT_TEST_CMD='cargo test --workspace --no-fail-fast'
[ -z "$PORT_WORK_DIR" ]  && PORT_WORK_DIR="${SCRIPT_DIR}/../rust"

# --------------------------------------------------------------------------
# Step 10 — release build.
# --------------------------------------------------------------------------
log_step "$PORT_BUILD_CMD"
if ! (cd "$PORT_WORK_DIR" && eval "$PORT_BUILD_CMD") \
    >"$TMP_BUILD_LOG" 2>&1; then
  echo "==> build failed ($PORT_BUILD_CMD)" >&2
  echo "--- last 40 lines of build output ---" >&2
  tail -n 40 "$TMP_BUILD_LOG" >&2
  on_failure "$EXIT_BUILD_FAILED" "build_cmd"
fi

# --------------------------------------------------------------------------
# Step 11 — full workspace test suite.
# --------------------------------------------------------------------------
log_step "$PORT_TEST_CMD"
if ! (cd "$PORT_WORK_DIR" && eval "$PORT_TEST_CMD") \
    >"$TMP_TEST_LOG" 2>&1; then
  echo "==> tests failed ($PORT_TEST_CMD)" >&2
  echo "failing tests:" >&2
  # `cargo test` prints `test <crate>::<name> ... FAILED` lines for failures;
  # also surface "test result: FAILED" summary lines for fallback context.
  FAILING="$(grep -E '^test .* \.\.\. FAILED$' "$TMP_TEST_LOG" || true)"
  if [ -n "$FAILING" ]; then
    printf '%s\n' "$FAILING" | sed -E 's/^test /  /; s/ \.\.\. FAILED$//' >&2
  else
    # No per-test FAILED markers — surface the summary lines instead.
    grep -E 'test result: FAILED' "$TMP_TEST_LOG" | sed 's/^/  /' >&2 || true
  fi
  on_failure "$EXIT_TEST_FAILED" "test_cmd"
fi

# --------------------------------------------------------------------------
# Step 12 — commit the port. NEW commit only; never amend, never rebase.
# --------------------------------------------------------------------------
log_step "committing port"
if ! git commit -m "$SUBJECT" -m "Co-Authored-By: Claude <noreply@anthropic.com>" \
    >/dev/null 2>&1; then
  git commit -m "$SUBJECT" -m "Co-Authored-By: Claude <noreply@anthropic.com>" >&2 || true
  on_failure "$EXIT_GH_FAILED" "git commit"
fi

# --------------------------------------------------------------------------
# Step 13 — optional Claude-generated PR body; deterministic fallback on any
# failure. Never aborts the pipeline.
# --------------------------------------------------------------------------
UPSTREAM_LINK="https://github.com/${UPSTREAM_REPO}/commit/${FULL_SHA}"
LLM_NOTE=""
if [ "$LLM_ASSISTED" = "1" ]; then
  LLM_NOTE="

⚠️ **LLM-assisted resolution.** \`git apply --3way\` produced conflicts; Claude was invoked once via \`--with-llm\` to translate the upstream hunks against the local code. Review the diff against the upstream link below carefully — the LLM's interpretation of upstream intent may differ from a human reviewer's."
fi
FALLBACK_BODY="Ports upstream/${SHORT_SHA} from ${UPSTREAM_REPO}.

Upstream commit: ${UPSTREAM_TITLE}
Upstream link: ${UPSTREAM_LINK}${LLM_NOTE}"

PR_BODY="$FALLBACK_BODY"
if command -v claude >/dev/null 2>&1; then
  log_step "generating PR body summary via claude (30s timeout)"
  DIFF_HEAD="$(head -n 80 "$TMP_DIFF" 2>/dev/null || true)"
  CLAUDE_PROMPT="In ≤4 sentences, summarise this upstream port for a PR body. \
Be concrete (what changed and why), not generic. Upstream title: \
${UPSTREAM_TITLE}

First ~80 lines of the diff:
${DIFF_HEAD}"
  # SECURITY: do NOT pass --dangerously-skip-permissions here. The prompt
  # embeds upstream-controlled commit title + diff bytes; bypassing permission
  # prompts would let a hostile upstream commit trigger arbitrary tool use.
  # FR-011 makes this call non-load-bearing — a deterministic fallback body
  # is always available.
  CLAUDE_OUT=""
  if command -v timeout >/dev/null 2>&1; then
    CLAUDE_OUT="$(timeout 30s claude -p --print \
      "$CLAUDE_PROMPT" 2>/dev/null || true)"
  else
    CLAUDE_OUT="$(claude -p --print \
      "$CLAUDE_PROMPT" 2>/dev/null || true)"
  fi
  if [ -n "$CLAUDE_OUT" ]; then
    PR_BODY="${CLAUDE_OUT}

Upstream link: ${UPSTREAM_LINK}${LLM_NOTE}"
  else
    log_step "claude summary unavailable — using deterministic fallback body"
  fi
else
  log_step "claude CLI not on PATH — using deterministic fallback body"
fi

# --------------------------------------------------------------------------
# Step 14 — push branch.
# --------------------------------------------------------------------------
log_step "git push -u origin ${BRANCH}"
if ! git push -u origin "$BRANCH" >/dev/null 2>&1; then
  git push -u origin "$BRANCH" >&2 || true
  on_failure "$EXIT_GH_FAILED" "git push"
fi

# --------------------------------------------------------------------------
# Step 15 — open draft PR.
# --------------------------------------------------------------------------
log_step "gh pr create --draft"
PR_URL="$(gh pr create --draft --title "$SUBJECT" --body "$PR_BODY" 2>&1)" || {
  echo "ERROR: gh pr create failed" >&2
  printf '%s\n' "$PR_URL" >&2
  on_failure "$EXIT_GH_FAILED" "gh pr create"
}

# Defensive: gh pr create's stdout SHOULD be a single PR URL. If it isn't,
# something went wrong upstream (e.g. gh printed a warning above the URL).
if ! [[ "$PR_URL" =~ ^https://github\.com/.+/pull/[0-9]+$ ]]; then
  echo "ERROR: gh pr create returned no usable PR URL: ${PR_URL}" >&2
  on_failure "$EXIT_GH_FAILED" "gh pr create returned no URL"
fi

# --------------------------------------------------------------------------
# Step 16 — emit PR URL on stdout and exit 0.
# --------------------------------------------------------------------------
printf '%s\n' "$PR_URL"
exit "$EXIT_OK"
