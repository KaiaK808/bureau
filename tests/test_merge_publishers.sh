#!/bin/bash
# Real standalone gates on both snapshots, projected offline transports, and controls.
set -uo pipefail
export BUREAU_MERGE_TEST_LIBRARY_ONLY=1
source "$(dirname "$0")/test_merge_pipeline_correctness.sh"
unset BUREAU_MERGE_TEST_LIBRARY_ONLY
export MERGE_REVIEW_FAKE="$REPO_ROOT/tests/lib/merge-review-fake.py"
export GIT_CEILING_DIRECTORIES="$SANDBOX_ROOT"
export GIT_ALLOW_PROTOCOL=file PYTHONDONTWRITEBYTECODE=1
mkdir -p "$SANDBOX_ROOT/deny"
for command in gh curl wget ssh linear claude codex; do
  printf '#!/bin/sh\necho "DENIED real transport" >&2\nexit 97\n' > "$SANDBOX_ROOT/deny/$command"
  chmod +x "$SANDBOX_ROOT/deny/$command"
done
export PATH="$SANDBOX_ROOT/deny:$PATH"
FAILS=0
RUNTIME_REF="${1:-}"

publisher_case() {
  local name="$1" phase="$2" mutation="${3:-}" sb expected rc
  sb=$(make_sandbox "${name}-${phase}-${mutation:-normal}")
  populate_happy_fixtures "$sb"
  expected=$(python3 -I -B "$REPO_ROOT/tests/merge_publishers_cases.py" "$sb" "$name" "$phase") || return 1
  if [ -n "$RUNTIME_REF" ]; then
    git -C "$REPO_ROOT" show "$RUNTIME_REF:templates/scripts/merge-pipeline.sh" > "$sb/scripts/merge-pipeline.sh" || return 1
    git -C "$REPO_ROOT" show "$RUNTIME_REF:templates/scripts/bureau-config.sh" > "$sb/scripts/bureau-config.sh.base" || return 1
    sed -n '/^# ── TEST OVERRIDES/,$p' "$sb/scripts/bureau-config.sh" >> "$sb/scripts/bureau-config.sh.base"
    mv "$sb/scripts/bureau-config.sh.base" "$sb/scripts/bureau-config.sh"
  fi
  python3 -I -B - "$sb/scripts/bureau-merge-review.py" "$mutation" "$name" <<'PY'
from pathlib import Path
import sys
p = Path(sys.argv[1]); text = p.read_text()
mutations = {
    'author': ("if actor in policy['entries']:", 'if True:'),
    'type': ("kind = required(author, '__typename', str)", "kind = 'Bot'"),
    'heading': ("if not lines or lines[0] != heading:", "if not lines or False:"),
    'header': ("if not match:\n                break", "if not match:\n                continue"),
    'typename': ('author{__typename login}', 'author{login}'),
    'edits': ("if selected['lastEditedAt'] is not None or reread['lastEditedAt'] is not None or reread != selected:", 'if False:'),
}
if sys.argv[2]:
    old, new = mutations[sys.argv[2]]
    assert old in text
    text = text.replace(old, new)
if sys.argv[3] == 'deadline':
    text = text.replace('time.monotonic() + 90', "time.monotonic() + (.08 if sys.argv[1] == 'review' else 90)")
p.write_text(text)
PY
  run_pipeline "$sb" TEAM-1; rc=$?
  if [ "$rc" != "$expected" ]; then
    echo "FAIL $name $phase ${mutation:-normal}: got $rc, expected $expected" >&2
    tail -12 "$sb/pipeline.out" "$sb/pipeline.err" >&2
    return 1
  fi
  if [ "$expected" = 0 ]; then
    [ -s "$sb/stub_data/merge_calls.log" ] || return 1
    [ "$(cat "$sb/stub_data/policy-reads")" = 1 ] || return 1
    [ "$(cat "$sb/stub_data/review-reads")" = 2 ] || return 1
  else
    [ ! -s "$sb/stub_data/merge_calls.log" ] || return 1
  fi
  if [ "$phase" = jit ]; then
    [ "$(cat "$sb/stub_data/review-reads" 2>/dev/null || echo 0)" -ge 2 ] || { echo "FAIL JIT snapshot not reached: $name" >&2; return 1; }
  fi
  if [ "$expected" = 25 ]; then
    grep -q 'agents.merge_publishers\|ci_green:' "$sb/pipeline.out" || return 1
  fi
  ! grep -q 'api user' "$sb/gh_invocations.log" || return 1
  return 0
}

for phase in ${PUBLISHER_PHASES:-initial jit}; do
  for name in ${PUBLISHER_CASES:-outsider-APPROVE outsider-BLOCK fenced quoted prefixed wrong-issue provider-injection duplicate-verdict duplicate-build human-app-collision app-human-collision personal app app-prefix actions outsider-block-ignored null-discussion pinned-policy checkout-policy null-author unknown-type missing-type empty-login edit-approve-block edit-block-approve already-edited deleted hidden unreadable-body null-slot graphql-errors count cursor duplicate-id truncated later-block later-read-error read-error-red-ci read-error-green-ci block request-changes}; do
    if publisher_case "$name" "$phase"; then echo "PASS $name $phase"; else FAILS=$((FAILS + 1)); fi
  done
done
for name in ${PUBLISHER_EXTRA_CASES:-policy-missing policy-file-missing policy-invalid policy-empty policy-duplicate policy-too-many policy-read-error threads-truncated threads-errors threads-null status-truncated status-errors page-cap deadline block-without-head block-without-base}; do
  if publisher_case "$name" initial; then echo "PASS $name"; else FAILS=$((FAILS + 1)); fi
done
if [ -z "$RUNTIME_REF" ] && [ "${PUBLISHER_SKIP_CONTROLS:-0}" != 1 ]; then
  for pair in author:outsider-APPROVE type:human-app-collision heading:wrong-issue header:provider-injection typename:app edits:edit-approve-block; do
    if publisher_case "${pair#*:}" initial "${pair%%:*}"; then
      echo "FAIL negative control survived: $pair"; FAILS=$((FAILS + 1))
    else echo "PASS negative control rejected: $pair"; fi
  done
fi
[ "$FAILS" = 0 ] || { echo "FAIL publisher assertions: $FAILS"; exit 1; }
if [ -n "$RUNTIME_REF" ]; then
  git -C "$REPO_ROOT" show "$RUNTIME_REF:templates/scripts/bureau-doctor.py" > "$SANDBOX_ROOT/doctor.py"
  python3 -B "$REPO_ROOT/tests/merge_publishers_policy_test.py" "$SANDBOX_ROOT/doctor.py" || exit 1
else
  python3 -B "$REPO_ROOT/tests/merge_publishers_policy_test.py" || exit 1
fi
echo 'OK test_merge_publishers'
