#!/bin/bash
# merge-body.sh — the subject and body Bureau writes into a merge commit.
#
# SOURCE, DO NOT RUN. This file only defines functions: no top-level side
# effects, no network, no `set`. merge-pipeline.sh sources it next to
# bureau-config.sh.
#
# Why (carried over from installation B, EXP-1318): `gh pr merge` without --body
# lets GitHub compose the squash message from the branch's commit list. A CI
# suppressor anywhere in that list — `[skip ci]` in an iteration commit, a task
# title, a body the agent wrote — lands in the merge commit on main, and GitHub
# then creates no CI run for the push to main at all. With
# `squash_merge_commit_message: COMMIT_MESSAGES` (the default on many repos)
# that is exactly what reaches main. So the merge sets its own subject and body,
# and every suppressor GitHub honours is defanged on the way, whatever its
# source: commit list, PR title or PR body.
#
# The second layer, which reads the commits themselves, is
# squash-marker-check.sh (check_squash_range in bureau-config.sh).

# sanitize_ci_markers <text>            # argument, or stdin without one
#   → stdout: the same text with every CI suppressor GitHub honours defanged.
#   → always exits 0 (a pure text transformation).
#
# Case-insensitive, anywhere, in any line. Only the token GitHub matches is
# broken; the text around it stays readable:
#   [skip ci] [ci skip] [no ci] [skip actions] [actions skip]  → round brackets
#   skip-checks: <trailer>                                     → hyphen to space
#                                                                (no longer a valid trailer key)
#   ***NO_CI***                                                → stars dropped
#
# Guarantees (tests/test_merge_body.sh):
#   G1 marker-free  — the output contains none of the forms above.
#   G2 idempotent   — the defanged forms do not match again: sanitize(sanitize(x)) == sanitize(x).
#   G3 unchanged    — without a marker the output is byte-identical to the input,
#                     trailing newlines included.
#   G4 readable     — only the token is defanged.
sanitize_ci_markers() {
  local _input
  if [ "$#" -gt 0 ]; then
    _input="$1"
  else
    # `$(cat)` would strip trailing newlines and break G3 on the stdin path;
    # `IFS= read -rd ''` reads stdin verbatim up to EOF.
    IFS= read -rd '' _input || true
  fi
  # `printf '%s'` adds no newline (G3). The `I` flag is case-insensitive
  # matching; GNU sed and the BSD sed on macOS both have it.
  printf '%s' "$_input" | sed -E \
    -e 's/\[(skip ci|ci skip|no ci|skip actions|actions skip)\]/(\1)/Ig' \
    -e 's/(skip)-(checks[[:space:]]*:)/\1 \2/Ig' \
    -e 's/\*\*\*(no_ci)\*\*\*/(\1)/Ig'
}

# build_merge_body <pr_title> <pr_body>
#   → stdout: the sanitised merge-commit body.
#
# The body is the sanitised PR body. An empty or whitespace-only PR body falls
# back to the PR title, so the body is never empty and the caller never falls
# back to GitHub's commit-list default. The subject is built at the call site
# as `sanitize_ci_markers "$pr_title"`.
#
# Guarantees:
#   G5 never empty          — an empty PR body gives sanitize(pr_title).
#   G6 never GitHub default — the caller always passes a non-empty --subject and --body.
#   G7 source-independent   — G1 holds for the fallback too.
build_merge_body() {
  local _pr_title="$1" _pr_body="$2" _source
  if [ -z "${_pr_body//[[:space:]]/}" ]; then
    _source="$_pr_title"
  else
    _source="$_pr_body"
  fi
  sanitize_ci_markers "$_source"
}
