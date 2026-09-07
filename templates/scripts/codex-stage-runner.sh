#!/bin/bash
# Compatibility entry point. New pipelines use run_stage_for + bureau-provider.py.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
export BUREAU_RUNNER_IMPLEMENT=codex
SCHEMA=""
while [ "$#" -gt 0 ]; do
  case "$1" in
    --model) export BUREAU_CODEX_MODEL_IMPLEMENT="$2"; shift 2 ;;
    --sandbox) export BUREAU_SANDBOX_IMPLEMENT="$2"; shift 2 ;;
    --read-only) export BUREAU_SANDBOX_IMPLEMENT=read-only; shift ;;
    --schema) SCHEMA="$2"; shift 2 ;;
    --) shift; break ;;
    -*) echo "Unknown flag: $1" >&2; exit 2 ;;
    *) break ;;
  esac
done
ARGS=(--stage implement --prompt-file -)
[ -n "$SCHEMA" ] && ARGS+=(--schema "$SCHEMA")
if [ "$#" = 1 ]; then
  printf '%s' "$1" | python3 "$SCRIPT_DIR/bureau-provider.py" "${ARGS[@]}"
elif [ "$#" = 0 ]; then
  exec python3 "$SCRIPT_DIR/bureau-provider.py" "${ARGS[@]}"
else
  echo 'Expected one prompt or stdin' >&2; exit 2
fi
