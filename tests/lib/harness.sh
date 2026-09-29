#!/bin/bash
# Test harness for bureau-init pipeline scripts.
#
# Exports:
#   sandbox_init <issue-id> <branch-name>  — set up an isolated sandbox dir,
#                                            git-init it, write fixture spec
#                                            files, copy the pipeline + stub
#                                            bureau-config into it, prep PATH.
#   run_implement_pipeline <args...>       — invoke the pipeline under test
#                                            and capture stdout/stderr/rc.
#   assert_eq <expected> <actual> <label>
#   assert_match <pattern> <text> <label>
#   assert_file_contains <pattern> <file> <label>
#   assert_calls_include <pattern>         — grep the recorded calls.log
#   assert_calls_exclude <pattern>
#   teardown                               — remove the sandbox.
#
# Tests source this file and then call sandbox_init + run_implement_pipeline
# + assert_* in their own scope. The harness leaves $SANDBOX, $SCRIPTS_DIR,
# $LAST_STDOUT, $LAST_STDERR, $LAST_RC populated for assertions.

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && cd .. && pwd)"
TESTS_DIR="$REPO_ROOT/tests"
FIXTURES_DIR="$TESTS_DIR/fixtures"
LIB_DIR="$TESTS_DIR/lib"

sandbox_init() {
  local issue="${1:-EXP-1}"
  local branch="${2:-test-branch}"

  SANDBOX=$(mktemp -d -t bureau-test.XXXXXXXX)
  export SANDBOX

  # Real git repo + a bare origin so `git fetch origin` and `git push origin`
  # in the pipeline succeed. The pipeline does `git rev-parse --verify
  # origin/$BRANCH` and `git checkout -B "$BRANCH" "origin/$BRANCH"` — both
  # require the branch to exist on origin.
  git -C "$SANDBOX" init -q -b main
  git -C "$SANDBOX" config user.email "test@bureau"
  git -C "$SANDBOX" config user.name  "Bureau Test"
  git -C "$SANDBOX" commit -q --allow-empty -m "init"
  git -C "$SANDBOX" checkout -q -b "$branch"
  git -C "$SANDBOX" commit -q --allow-empty -m "branch start on $branch"

  local fake_origin="$SANDBOX/.fake-origin.git"
  git init -q --bare "$fake_origin"
  git -C "$SANDBOX" remote add origin "$fake_origin"
  git -C "$SANDBOX" push -q origin main
  git -C "$SANDBOX" push -q origin "$branch"

  # Spec dir with a tasks.md for the branch. The stages find it with the real
  # bureau_spec_dir_for_branch (exact name > unique slug > unique number), so
  # the directory is named like the branch, with 001- added when the branch has
  # no number: "test-branch" → "001-test-branch", "001-foo" → "001-foo".
  local spec_fixture="001-$branch"
  case "$branch" in [0-9][0-9][0-9]-*) spec_fixture="$branch" ;; esac
  mkdir -p "$SANDBOX/specs/$spec_fixture"
  cat > "$SANDBOX/specs/$spec_fixture/tasks.md" <<EOF
# Test tasks
- [ ] T001 Do the first thing
- [ ] T002 Do the second thing
- [ ] T003 Do the third thing
EOF
  mkdir -p "$SANDBOX/logs"

  # Scripts dir with EVERY real pipeline + STUB bureau-config.sh on top.
  # Copy all so tests can target any pipeline without per-test wiring; the
  # stub bureau-config.sh wins because it's copied last under the same name.
  SCRIPTS_DIR="$SANDBOX/scripts"
  mkdir -p "$SCRIPTS_DIR"
  local f
  for f in "$REPO_ROOT/templates/scripts/"*; do
    [ ! -f "$f" ] || cp "$f" "$SCRIPTS_DIR/"
  done
  cp "$LIB_DIR/stub-bureau-config.sh" "$SCRIPTS_DIR/bureau-config.sh"
  sed -n '/^parse_claude_json() {/,${p;}' "$REPO_ROOT/templates/scripts/bureau-config.sh" > "$SCRIPTS_DIR/parse-result.sh"
  # Helpers that decide, not fetch, run for real: the Linear halt, the squash-range guard
  # the dependency restore (a no-op without package.json) and the review's verdict floor and exit come from the real config, and the script and marker list the guard reads were
  # copied above with every other file.
  sed -n -e '/^BUREAU_EXIT_LINEAR_UNUSABLE=/p' -e '/^halt_if_linear_unusable() {/,/^}/p' \
    -e '/^check_squash_range() {/,/^}/p' -e '/^comment_on_branch_pr() {/,/^}/p' \
    -e '/^restore_worktree_deps() {/,/^}/p' \
    -e '/^apply_build_failure() {/,/^}/p' -e '/^decide_review_verdict() {/,/^}/p' \
    -e '/^_review_count() {/,/^}/p' -e '/^_review_shown() {/,/^}/p' -e '/^review_verdict_from_text() {/,/^}/p' \
    -e '/^resolve_verdict_exit() {/,/^}/p' \
    -e '/^# ── Spec directory of a branch/,/^# ── End of spec directory of a branch/p' \
    "$REPO_ROOT/templates/scripts/bureau-config.sh" > "$SCRIPTS_DIR/real-helpers.sh"
  grep -q '^bureau_spec_dir_for_branch() {' "$SCRIPTS_DIR/real-helpers.sh" || { echo "harness: spec-dir matcher not found in bureau-config.sh" >&2; return 1; }
  # A needs-human label that cannot be written is held locally; the hold helpers run for real
  # on top of the stub's add_issue_label and alert_telegram.
  sed -n '/^# ── needs-human hold (EXP-1516)/,/^# ── End of needs-human hold/p' "$REPO_ROOT/templates/scripts/bureau-config.sh" >> "$SCRIPTS_DIR/real-helpers.sh"
  grep -q '^mark_needs_human() {' "$SCRIPTS_DIR/real-helpers.sh" || { echo "harness: needs-human hold block not found in bureau-config.sh" >&2; return 1; }
  sed -n '/^# Capture the caller boundary/,/^BUREAU_RUNTIME=/{ /^BUREAU_RUNTIME=/d; p; }' "$REPO_ROOT/templates/scripts/bureau-config.sh" > "$SCRIPTS_DIR/stop-boundary.sh"
  # The merge policy (agents.merge_mode and the predicates built on it) runs for real
  # against the sandbox .bureau.json (none = {} = auto); tests set the mode there with jq.
  sed -n '/^# ── Merge policy (agents.merge_mode)/,/^# ── End of merge policy/p' "$REPO_ROOT/templates/scripts/bureau-config.sh" > "$SCRIPTS_DIR/merge-policy.sh"
  grep -q '^bureau_merge_is_manual() {' "$SCRIPTS_DIR/merge-policy.sh" || { echo "harness: merge policy block not found in bureau-config.sh" >&2; return 1; }

  # Minimal .env in the sandbox cwd so the pipeline's `source .env` succeeds.
  cat > "$SANDBOX/.env" <<'EOF'
LINEAR_API_KEY=test-key
EOF

  # Stub gh binary first in PATH.
  STUB_PATH="$LIB_DIR/bin:$PATH"

  export FAKE_CLAUDE_BIN="$LIB_DIR/fake_claude.sh"
  chmod +x "$FAKE_CLAUDE_BIN" "$LIB_DIR/bin/gh"

  # Pre-seed the stub's "picked issue" so pipeline_pick_next returns it.
  export BUREAU_STUB_PICKED_ISSUE="$issue"
  export BUREAU_STUB_BRANCH="$branch"
  export PATH="$STUB_PATH"
}

run_implement_pipeline() {
  run_pipeline implement-pipeline.sh "$@"
}

# Generic pipeline runner. Captures stdout/stderr/rc into $LAST_*. The pipeline
# is allowed to fail (set +e); tests typically run pipelines that bail partway
# through because we only stub the early helpers and care about a specific
# stdout fragment (e.g. a header line printed before the failure point).
run_pipeline() {
  local script="$1"; shift
  pushd "$SANDBOX" >/dev/null
  set +e
  LAST_STDOUT=$(bash "$SCRIPTS_DIR/$script" "$@" 2>"$SANDBOX/stderr.log")
  LAST_RC=$?
  set -e
  LAST_STDERR=$(cat "$SANDBOX/stderr.log")
  popd >/dev/null
  export LAST_STDOUT LAST_STDERR LAST_RC
}

assert_eq() {
  local expected="$1" actual="$2" label="${3:-assertion}"
  if [ "$expected" != "$actual" ]; then
    echo "FAIL [$label]: expected '$expected', got '$actual'" >&2
    return 1
  fi
}

assert_match() {
  local pattern="$1" text="$2" label="${3:-match}"
  if ! grep -qE "$pattern" <<< "$text"; then
    echo "FAIL [$label]: pattern /$pattern/ not found in text:" >&2
    printf '%s\n' "$text" | sed 's/^/  | /' >&2
    return 1
  fi
}

assert_file_contains() {
  local pattern="$1" file="$2" label="${3:-file}"
  if ! grep -qE "$pattern" "$file" 2>/dev/null; then
    echo "FAIL [$label]: pattern /$pattern/ not found in $file:" >&2
    [ -f "$file" ] && sed 's/^/  | /' "$file" >&2 || echo "  | (file does not exist)" >&2
    return 1
  fi
}

assert_calls_include() {
  local pattern="$1" label="${2:-calls}"
  assert_file_contains "$pattern" "$SANDBOX/calls.log" "$label"
}

assert_calls_exclude() {
  local pattern="$1" label="${2:-calls}"
  if grep -qE "$pattern" "$SANDBOX/calls.log" 2>/dev/null; then
    echo "FAIL [$label]: pattern /$pattern/ should NOT appear in calls.log:" >&2
    sed 's/^/  | /' "$SANDBOX/calls.log" >&2
    return 1
  fi
}

teardown() {
  [ -n "${SANDBOX:-}" ] && [ -d "$SANDBOX" ] && rm -rf "$SANDBOX"
}

# Convenience: any test sourcing this file gets teardown on EXIT.
trap 'teardown' EXIT
