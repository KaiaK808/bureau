#!/bin/bash
# One-time setup for git merge drivers referenced by .gitattributes.
# Run once per clone after first checkout. Idempotent; safe to re-run.
set -euo pipefail

cd "$(dirname "$0")/.."

git config merge.ours.driver true
echo "Registered merge driver 'ours' (always keeps our version)."
echo "Used by .gitattributes for: .specify/feature.json"
