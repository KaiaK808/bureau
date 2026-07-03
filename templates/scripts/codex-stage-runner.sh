#!/usr/bin/env bash
# codex-stage-runner.sh — present a `claude -p`-compatible interface backed by
# `codex exec`, so a bureau pipeline stage can run on Codex (OpenAI/ChatGPT
# budget) instead of the Claude session quota WITHOUT changing how the stage
# script captures output.
#
# Contract parity with `$CLAUDE "PROMPT"`:
#   - Last positional arg is the prompt (same as `claude -p … "PROMPT"`).
#   - Emits the agent's FINAL message to stdout — prose + the trailing fenced
#     ```json verdict block that `parse_claude_json` (bureau-config.sh) reads.
#   - Exit code mirrors codex's.
#
# Why a wrapper and not a bare command string: `codex exec` only yields CLEAN
# output (no workdir/model/session-id header, no "tokens used" footer, no
# duplicated block) via `--output-last-message <file>`. A bare `$CLAUDE`-style
# command string can't allocate a per-call tempfile, so we wrap.
#
# Usage (drop-in for the CLAUDE var):
#   CLAUDE="bash scripts/codex-stage-runner.sh --model gpt-5.5 --sandbox workspace-write --"
#   RESULT=$($CLAUDE "your prompt")
#
# Flags (all optional; everything after `--` or the first non-flag is the prompt):
#   --model <m>          → codex -m <m>      (omit → codex default)
#   --sandbox <mode>     → codex -s <mode>   (default: workspace-write; QA/review write tests/notes)
#   --read-only          → shorthand for --sandbox read-only
#   --schema <file>      → codex --output-schema <file> (enforce verdict shape)
#
# ⚠ STAGE SCOPE — route ONLY review-type stages (code_review; maybe spec_review)
#   to this runner. Do NOT route `qa` or `implement` (or anything that runs the
#   project's build/test suite) to Codex: its exec sandbox has no network
#   listeners, trust-store, or git-metadata writes, so real suites fail
#   spuriously and the stage false-halts `needs-human`. Keep those on Claude.
set -euo pipefail

MODEL=""
SANDBOX="workspace-write"
SCHEMA=""

while [ $# -gt 0 ]; do
  case "$1" in
    --model)      MODEL="$2"; shift 2 ;;
    --sandbox)    SANDBOX="$2"; shift 2 ;;
    --read-only)  SANDBOX="read-only"; shift ;;
    --schema)     SCHEMA="$2"; shift 2 ;;
    --)           shift; break ;;
    -*)           echo "codex-stage-runner: unknown flag $1" >&2; exit 2 ;;
    *)            break ;;  # first non-flag is the prompt
  esac
done

PROMPT="${1:-}"
if [ -z "$PROMPT" ]; then
  echo "codex-stage-runner: no prompt given" >&2
  exit 2
fi

# Per-call tempfile for the clean final message.
LAST_MSG=$(mktemp -t codex-stage-last.XXXXXX)
trap 'rm -f "$LAST_MSG"' EXIT

# Build the codex exec argv. `--skip-git-repo-check` keeps parity with how the
# pipeline runs claude headlessly even in odd cwd states. `codex exec` is
# inherently non-interactive (no approval prompts — unlike interactive `codex`,
# it has no -a/--ask-for-approval flag); the sandbox mode alone bounds what
# Codex may write (workspace-write = edit the repo, cannot escape it).
CODEX_ARGS=(exec --skip-git-repo-check -s "$SANDBOX" -o "$LAST_MSG")
[ -n "$MODEL" ]  && CODEX_ARGS+=(-m "$MODEL")
[ -n "$SCHEMA" ] && CODEX_ARGS+=(--output-schema "$SCHEMA")

# Drive codex. Send its own progress/header to stderr (so it lands in the
# stage log like claude's does) and keep stdout for the final message only.
# `|| rc=$?` so `set -e` doesn't abort before we surface the final message and
# the real exit code.
rc=0
codex "${CODEX_ARGS[@]}" "$PROMPT" >&2 || rc=$?

# Emit the clean final message (prose + fenced json) on stdout — this is what
# the stage script captures into QA_RESULT / the review .txt files.
cat "$LAST_MSG"
exit "$rc"
