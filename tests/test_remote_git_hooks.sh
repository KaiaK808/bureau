#!/bin/bash
# Bureau's own git commands that talk to a remote run without the repository's hooks (v3.2, S2).
#
# Those commands keep the GitHub token variables (a credential helper may read them), so in
# v3.1 a hook from the branch that ran during them saw the token: `pre-push` on a push,
# `reference-transaction` on a fetch or a push (it fires on every ref update, and both update
# refs/remotes/origin/*). The git function in bureau-env.sh now adds `-c core.hooksPath=/dev/null`
# to push, fetch, pull, ls-remote, clone, remote and submodule. `repo.remote_git_runs_hooks: true`
# restores the v3.1 behaviour; any other value, an absent key and an unreadable .bureau.json keep
# the hooks off. Local commands (commit, checkout, merge, update-ref) still run the hooks, without
# any of the Bureau secrets (v3.1). Doctor warns on a value that is not a JSON boolean.
#
# Every case runs the REAL bureau-config.sh / bureau-env.sh in a sandbox repository whose tracked
# .githooks (core.hooksPath) record each run: the hook, whether GH_TOKEN or a .env key was in its
# environment, and the refs a reference-transaction saw. All git runs with GIT_CONFIG_GLOBAL=/dev/null
# and GIT_CONFIG_NOSYSTEM=1; every value is fake.
#   1  default (no key): Bureau's push lands and fetch updates origin/*, but neither runs a hook
#      (no pre-push, no reference-transaction on refs/remotes/); ls-remote works; the real
#      merge_origin_main_or_abort fetches without hooks and its local merge runs them without
#      the token; a local commit still runs pre-commit and reference-transaction
#   2  repo.remote_git_runs_hooks: true — pre-push and reference-transaction run again, with
#      GH_TOKEN and without the .env keys (the v3.1 behaviour)
#   3  false, "true", 1, "yes", {}, null and a .bureau.json jq cannot read (with and without
#      bureau-config.sh's bureau_get): hooks off
#   4  bureau-env.sh alone (no bureau_get, no BUREAU_CONFIG, as squash-marker-check.sh): hooks off
#   5  doctor: a non-boolean value is a warning; true, false and absent are not
# Negative control: against v3.1.0 (9411b3b) 1, 3 and 4 fail ("pre-push ran during Bureau's push,
# with GH_TOKEN") and 5 fails (no warning).
set -uo pipefail
export GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_NOSYSTEM=1 GIT_TERMINAL_PROMPT=0
source "$(dirname "$0")/lib/pr1-untrusted-env.sh"
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SCRIPTS="$REPO_ROOT/templates/scripts"
TMP=$(mktemp -d -t bureau-test.remote-hooks.XXXXXXXX)
TMP=$(cd "$TMP" && pwd -P)
trap 'rm -rf "$TMP"' EXIT
fail() { pr1_fail "$@"; }
MARKS="$TMP/hooks.log"
PROBES=(LINEAR_API_KEY="$PR1_LINEAR" TELEGRAM_BOT_TOKEN="$PR1_TG_TOKEN" TELEGRAM_ALERT_CHAT_ID="$PR1_TG_CHAT"
        GH_TOKEN="$PR1_GH" GITHUB_TOKEN="$PR1_GITHUB" OPERATOR_TOOL_VAR="$PR1_OPERATOR")

# repo <dir> — a repository with a bare origin whose tracked .githooks record every run as one
# line: "<hook> <arg1> gh=<yes|no> env=<yes|no> refs=<refs on stdin>"; core.hooksPath names it.
repo() {
  local r="$1" h
  rm -rf "$r" "$r.origin" "$r.other"; mkdir -p "$r/.githooks"
  git -C "$r" init -q -b main; git -C "$r" config user.email t@t; git -C "$r" config user.name t
  for h in pre-push reference-transaction pre-commit post-checkout post-merge; do
    cat > "$r/.githooks/$h" <<EOF
#!/bin/sh
refs=\$(cat 2>/dev/null | awk '{ printf "%s,", \$NF }')
gh=no; [ -z "\${GH_TOKEN:-}" ] || gh=yes
dotenv=no; /usr/bin/env | grep -qF -e '$PR1_LINEAR' -e '$PR1_TG_TOKEN' -e '$PR1_TG_CHAT' && dotenv=yes
echo "$h \${1:-} gh=\$gh env=\$dotenv refs=\$refs" >> '$MARKS'
exit 0
EOF
    chmod +x "$r/.githooks/$h"
  done
  printf '.bureau.json\n' > "$r/.git/info/exclude"
  git -C "$r" add -A; git -C "$r" commit -q -m init
  git init -q --bare "$r.origin"; git -C "$r.origin" symbolic-ref HEAD refs/heads/main; git -C "$r" remote add origin "$r.origin"; git -C "$r" push -q origin main
  git -C "$r" fetch -q origin
  git -C "$r" config core.hooksPath .githooks
  git clone -q "$r.origin" "$r.other"; git -C "$r.other" config user.email t@t; git -C "$r.other" config user.name t
  : > "$MARKS"
}
# upstream_commit — someone else pushes to origin/main, so the next fetch updates a ref.
ROUND=0
upstream_commit() {
  ROUND=$((ROUND + 1))
  (cd "$R.other" && git -c core.hooksPath=/dev/null pull -q origin main && echo "$ROUND" > up.txt && git add up.txt \
     && git commit -q -m "up $ROUND" && git -c core.hooksPath=/dev/null push -q origin HEAD:main)
}
# stage <shell code> — a stage shell in the repository: the probes exported, the real
# bureau-config.sh sourced (bureau_get reads $R/.bureau.json), then the code.
stage() {
  (cd "$R" && env "${PROBES[@]}" BUREAU_CONFIG="$R/.bureau.json" /bin/bash -c 'set -uo pipefail; source "$1/bureau-config.sh"; '"$1" _ "$SCRIPTS")
}
config() { printf '{"repo":%s}\n' "$1" > "$R/.bureau.json"; }
marks() { cat "$MARKS" 2>/dev/null; }

# remote_round <label> — from a stage shell: a local branch and commit (their hook runs go to
# $TMP/local.log), then Bureau's push of that branch and a fetch that updates origin/main (their
# hook runs stay in $MARKS), and an ls-remote.
remote_round() {
  upstream_commit
  : > "$MARKS"
  stage "git checkout -q -b c-$ROUND; echo w > w-$ROUND.txt; git add w-$ROUND.txt; git commit -q -m w-$ROUND" >/dev/null 2>&1
  cp "$MARKS" "$TMP/local.log"; : > "$MARKS"
  out=$(stage 'git push -q origin HEAD; echo "push=$?"; git fetch -q origin; echo "fetch=$?"
    git ls-remote origin refs/heads/main >/dev/null; echo "ls-remote=$?"' 2>&1)
  case "$out" in *push=0*fetch=0*ls-remote=0*) ;; *) fail "$1: a remote command failed: $(printf '%s' "$out" | tr '\n' ' ')" ;; esac
  [ "$(git -C "$R" rev-parse origin/main)" = "$(git -C "$R.origin" rev-parse main)" ] || fail "$1: the fetch did not update origin/main"
  [ "$(git -C "$R.origin" rev-parse "c-$ROUND" 2>/dev/null)" = "$(git -C "$R" rev-parse HEAD)" ] || fail "$1: the pushed branch is not on origin"
}
hooks_off() {  # <label>
  if marks | grep -q '^pre-push '; then fail "$1: pre-push ran during Bureau's push ($(marks | grep '^pre-push ' | head -1))"; fi
  if marks | grep -q 'refs=.*refs/remotes/'; then fail "$1: reference-transaction ran for a remote-tracking ref ($(marks | grep 'refs/remotes/' | head -1))"; fi
  if marks | grep -q 'gh=yes'; then fail "$1: a hook saw GH_TOKEN"; fi
}

# ── 1  default: no hooks on Bureau's remote git ───────────────────────────────
R="$TMP/repo"; repo "$R"; config '{}'
remote_round "1 default"
hooks_off "1 default"
# Local commands keep running the hooks, without any secret.
grep -q '^pre-commit ' "$TMP/local.log" || fail "1: the local commit did not run pre-commit"
grep -q '^reference-transaction .*refs/heads/' "$TMP/local.log" || fail "1: the local commit did not run reference-transaction"
if grep -q 'gh=yes\|env=yes' "$TMP/local.log"; then fail "1: a local hook saw a secret: $(grep 'gh=yes\|env=yes' "$TMP/local.log" | head -1)"; fi
# The real merge_origin_main_or_abort: its fetch runs no hook, its local merge runs them, tokenless.
git -C "$R" checkout -q main; git -C "$R" checkout -q -b behind; git -C "$R" reset -q --hard "$(git -C "$R" rev-list --max-parents=0 HEAD)"
echo mine > "$R/mine.txt"; git -C "$R" add mine.txt; git -C "$R" -c core.hooksPath=/dev/null commit -q -m mine
upstream_commit; : > "$MARKS"
out=$(stage 'merge_origin_main_or_abort EXP-1 QA >/dev/null 2>&1; echo "merge=$?"')
case "$out" in *merge=0*) ;; *) fail "1: merge_origin_main_or_abort failed: $out" ;; esac
git -C "$R" merge-base --is-ancestor "$(git -C "$R.origin" rev-parse main)" HEAD || fail "1: merge_origin_main_or_abort did not merge the fetched main"
hooks_off "1 merge_origin_main_or_abort"
marks | grep -q '^reference-transaction .*refs/heads/behind' || fail "1: the local merge of merge_origin_main_or_abort ran no hook"
pr1_pass "1 default: Bureau's push and fetch run no hook; local commands run them without secrets"

# ── 2  repo.remote_git_runs_hooks: true ───────────────────────────────────────
repo "$R"; config '{"remote_git_runs_hooks":true}'
remote_round "2 true"
marks | grep -q '^pre-push origin gh=yes env=no' || fail "2: pre-push did not run with GH_TOKEN and without the .env keys: $(marks | tr '\n' ';')"
marks | grep -q '^reference-transaction .*gh=yes env=no refs=.*refs/remotes/origin/main' || fail "2: the fetch ran no reference-transaction with the token: $(marks | tr '\n' ';')"
if marks | grep -q 'env=yes'; then fail "2: a hook saw a .env key"; fi
pr1_pass "2 true: the hooks run again on push and fetch, with the GitHub token and without the .env keys"

# ── 3  other values keep the hooks off ────────────────────────────────────────
for value in false '"true"' 1 '"yes"' '{}' null; do
  repo "$R"; config "{\"remote_git_runs_hooks\":$value}"
  remote_round "3 $value"; hooks_off "3 $value"
done
repo "$R"; printf '{"repo": {"remote_git_runs_hooks": true' > "$R/.bureau.json.broken"
upstream_commit; : > "$MARKS"
(cd "$R" && env "${PROBES[@]}" BUREAU_CONFIG="$R/.bureau.json.broken" /bin/bash -c 'source "$1/bureau-env.sh"; git push -q origin HEAD:refs/heads/broken; git fetch -q origin' _ "$SCRIPTS") >/dev/null 2>&1
[ "$(git -C "$R.origin" rev-parse broken 2>/dev/null)" = "$(git -C "$R" rev-parse HEAD)" ] || fail "3 unreadable: the push did not land"
hooks_off "3 unreadable .bureau.json"
# The same in a stage shell whose bureau_get reads a .bureau.json that turned unreadable after
# bureau-config.sh was sourced (bureau_get fails: hooks off, not on).
repo "$R"; config '{"remote_git_runs_hooks":true}'; upstream_commit; : > "$MARKS"
stage 'printf "{\"repo\": {\"remote_git_runs_hooks\": true" > "$BUREAU_CONFIG"; git push -q origin HEAD:refs/heads/broken2; git fetch -q origin' >/dev/null 2>&1
[ "$(git -C "$R.origin" rev-parse broken2 2>/dev/null)" = "$(git -C "$R" rev-parse HEAD)" ] || fail "3 unreadable for bureau_get: the push did not land"
hooks_off "3 unreadable .bureau.json for bureau_get"
pr1_pass "3 false, a string, a number, an object, null and an unreadable file keep the hooks off"

# ── 4  bureau-env.sh alone ────────────────────────────────────────────────────
repo "$R"; upstream_commit; : > "$MARKS"
(cd "$R" && env -u BUREAU_CONFIG "${PROBES[@]}" /bin/bash -c 'source "$1/bureau-env.sh"; git push -q origin HEAD:refs/heads/alone; git fetch -q origin' _ "$SCRIPTS") >/dev/null 2>&1
[ "$(git -C "$R.origin" rev-parse alone 2>/dev/null)" = "$(git -C "$R" rev-parse HEAD)" ] || fail "4: the push did not land"
hooks_off "4 bureau-env.sh alone"
pr1_pass "4 without bureau-config.sh and BUREAU_CONFIG: hooks off"

# ── 5  doctor ─────────────────────────────────────────────────────────────────
doctor_warnings() {  # <repo json> — the doctor's warnings about the key, one per line
  local d="$TMP/doctor"
  rm -rf "$d"; mkdir -p "$d/scripts"; cp "$SCRIPTS"/bureau-doctor.py "$SCRIPTS"/bureau-runtime.py "$SCRIPTS"/bureau-provider.py "$SCRIPTS"/bureau-stage.md "$d/scripts/"
  git -C "$d" init -q -b main
  jq -n --argjson repo "$1" '{version: 2, linear: {teams: [{id: "t", key: "EXP", states: {build: "s"}}]}, agents: {}, repo: $repo}' > "$d/.bureau.json"
  (cd "$d" && python3 scripts/bureau-doctor.py --repo "$d") | jq -r '(.warnings // [])[], (.errors // [])[]' | grep 'remote_git_runs_hooks' || true
}
for value in true false; do
  [ -z "$(doctor_warnings "{\"remote_git_runs_hooks\":$value}")" ] || fail "5: doctor warns on $value"
done
[ -z "$(doctor_warnings '{}')" ] || fail "5: doctor warns on an absent key"
for value in '"true"' 1 '"yes"'; do
  w=$(doctor_warnings "{\"remote_git_runs_hooks\":$value}")
  case "$w" in *"is not a JSON boolean"*) ;; *) fail "5: doctor does not warn on $value: ${w:-no warning}" ;; esac
done
pr1_pass "5 doctor warns on a value that is not a JSON boolean"

if [ "$PR1_FAILS" != 0 ]; then echo "$PR1_FAILS check(s) failed" >&2; exit 1; fi
echo "OK test_remote_git_hooks"
