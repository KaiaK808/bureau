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
#   6  doctor: the main checkout uses Git LFS (filter=lfs, in any position, in its .gitattributes, a
#      committed or untracked assets/.gitattributes, or .git/info/attributes) and the key is not
#      true — a warning naming the key (also from a linked worktree); none for true, another filter,
#      a comment line or a name that only starts with lfs
#   7  the two `git ls-remote` of bureau-supervision.py (a stopped review's check, the gate waits of
#      the pickers) get -c core.hooksPath=/dev/null and the hook event switches of git() too (a git
#      on PATH records its arguments)
#   8  static: every shell template with a remote git command has the git() function, and every
#      remote git command line in the Python templates carries NO_HOOKS
#   9  git 2.54 and later: hooks the configuration defines (hook.<name>.command and .event) run
#      neither on Bureau's push nor on its fetches, also under a name with `=` and next to a hook
#      named like the event; true runs them (skipped, with a SKIP line, on an older git)
# Negative control: against v3.1.0 (9411b3b) 1, 3 and 4 fail ("pre-push ran during Bureau's push,
# with GH_TOKEN") and 5 and 6 fail (no warning); 7 and 8 fail against the merge of main (a9c754c),
# whose supervision ls-remote runs without the flag; against 735496e 6 fails for the committed and
# the untracked assets/.gitattributes and for info/attributes, 7 on the event switches and 9 (with
# git 2.54 or 2.55) on every configured hook.
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
# doctor_warnings <repo json> [attributes text] [place] — the doctor's warnings and errors about
# the key, one per line. The text goes to the main checkout's .gitattributes, or with place
# "nested" to a committed assets/.gitattributes, with "untracked" to an uncommitted
# assets/.gitattributes, with "info" to .git/info/attributes; with
# "worktree" doctor runs in a linked worktree of the repository whose own checkout has none of them.
doctor_warnings() {
  local d="$TMP/doctor" at="$TMP/doctor" file=.gitattributes
  rm -rf "$d" "$TMP/doctor-wt"; mkdir -p "$d/scripts"; cp "$SCRIPTS"/bureau-doctor.py "$SCRIPTS"/bureau-runtime.py "$SCRIPTS"/bureau-provider.py "$SCRIPTS"/bureau-stage.md "$d/scripts/"
  git -C "$d" init -q -b main; git -C "$d" -c user.email=t@t -c user.name=t commit -q --allow-empty -m init
  jq -n --argjson repo "$1" '{version: 2, linear: {teams: [{id: "t", key: "EXP", states: {build: "s"}}]}, agents: {}, repo: $repo}' > "$d/.bureau.json"
  case "${3:-}" in nested|untracked) file=assets/.gitattributes ;; info) file=.git/info/attributes ;; esac
  if [ -n "${2:-}" ]; then mkdir -p "$(dirname "$d/$file")"; printf '%s\n' "$2" > "$d/$file"; fi
  if [ "${3:-}" = nested ]; then git -C "$d" add assets; git -C "$d" -c user.email=t@t -c user.name=t commit -q -m attributes; fi
  if [ "${3:-}" = worktree ]; then git -C "$d" worktree add -q "$TMP/doctor-wt" -b wt; at="$TMP/doctor-wt"; fi
  (cd "$at" && python3 "$d/scripts/bureau-doctor.py" --repo "$at") | jq -r '(.warnings // [])[], (.errors // [])[]' | grep 'remote_git_runs_hooks' || true
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

# ── 6  doctor: Git LFS needs the hooks ────────────────────────────────────────
# git lfs uploads its objects in its pre-push hook: with the hooks off, Bureau's push leaves them
# off the remote. Doctor warns when the main checkout's .gitattributes uses filter=lfs and the key
# is not the JSON value true.
LFS='*.psd filter=lfs diff=lfs merge=lfs -text'
for value in absent false '"true"' 1; do
  if [ "$value" = absent ]; then cfg='{}'; else cfg="{\"remote_git_runs_hooks\":$value}"; fi
  w=$(doctor_warnings "$cfg" "$LFS")
  case "$w" in *"uses Git LFS (filter=lfs"*) ;; *) fail "6: no Git LFS warning with the key $value: ${w:-no warning}" ;; esac
done
w=$(doctor_warnings '{"remote_git_runs_hooks":true}' "$LFS")
case "$w" in *"uses Git LFS"*) fail "6: a Git LFS warning although the key is true" ;; esac
for text in '*.txt filter=probe' "# $LFS" '*.bin filter=lfsish'; do
  w=$(doctor_warnings '{}' "$text")
  case "$w" in *"uses Git LFS"*) fail "6: a Git LFS warning for .gitattributes '$text'" ;; esac
done
w=$(doctor_warnings '{}' "$LFS" worktree)
case "$w" in *"uses Git LFS (filter=lfs"*) ;; *) fail "6: doctor in a linked worktree does not read the main checkout's .gitattributes: ${w:-no warning}" ;; esac
# The filter anywhere among the attributes; a .gitattributes below the top (git applies it to its
# directory, committed or not) and the git directory's info/attributes count too.
for place in '*.bin -text filter=lfs|' "$LFS|nested" "$LFS|untracked" "$LFS|info"; do
  w=$(doctor_warnings '{}' "${place%|*}" "${place##*|}")
  case "$w" in *"uses Git LFS (filter=lfs"*) ;; *) fail "6: no Git LFS warning for '${place%|*}' in ${place##*|}: ${w:-no warning}" ;; esac
done
w=$(doctor_warnings '{"remote_git_runs_hooks":true}' "$LFS" nested)
case "$w" in *"uses Git LFS"*) fail "6: a Git LFS warning for a nested .gitattributes although the key is true" ;; esac
w=$(doctor_warnings '{}' "# $LFS" info)
case "$w" in *"uses Git LFS"*) fail "6: a Git LFS warning for a comment in info/attributes" ;; esac
pr1_pass "6 doctor warns when the main checkout uses Git LFS and the hooks are off"

# ── 7  the remote git Bureau's Python starts ──────────────────────────────────
# bureau-supervision.py runs `git ls-remote` for a stopped review's check (the review stage) and
# for the gate waits (the pickers, v3.2). ls-remote starts no hook, but it keeps the GitHub tokens
# like every remote command, so it carries the same flag. A git on PATH records the arguments it
# gets, then runs the real git.
repo "$R"; config '{}'
REAL_GIT=$(type -P git); mkdir -p "$TMP/argv"
printf '#!/bin/sh\nprintf "%%s\\n" "$*" >> "%s"\nexec "%s" "$@"\n' "$TMP/argv.log" "$REAL_GIT" > "$TMP/argv/git"; chmod +x "$TMP/argv/git"
printf '#!/bin/sh\necho %s\n' "'{\"state\":\"OPEN\",\"baseRefName\":\"main\"}'" > "$TMP/argv/gh"; chmod +x "$TMP/argv/gh"
git -C "$R" -c core.hooksPath=/dev/null push -q origin HEAD:refs/heads/feat
HEAD_SHA=$(git -C "$R" rev-parse HEAD); : > "$MARKS"; : > "$TMP/argv.log"
supervise() { (cd "$R" && PATH="$TMP/argv:$PATH" env "${PROBES[@]}" python3 -I "$SCRIPTS/bureau-supervision.py" --repo "$R" "$@" 2>&1); }
supervise merge-wait EXP-1 --branch feat --head "$HEAD_SHA" --outcome not-yet >/dev/null
out=$(supervise gate-waits --stage merge --first 300 --cap 3600)
case "$out" in *'"issue": "EXP-1"'*) ;; *) fail "7 gate-waits: the held ticket is not listed (its ls-remote failed?): $out" ;; esac
DETAIL='{"identifier":"EXP-1","title":"t","description":"d","labels":[]}'
printf '%s' "$DETAIL" | supervise stop EXP-1 --branch feat --state 'Build Review' --head "$HEAD_SHA" --base "$HEAD_SHA" --reviewed-head "$HEAD_SHA" --pr 5 >/dev/null
out=$(printf '%s' "$DETAIL" | supervise check EXP-1 --branch feat --state 'Build Review')
case "$out" in *'"stopped": true'*) ;; *) fail "7 check: the stop was not confirmed against origin (its ls-remote failed?): $out" ;; esac
[ "$(grep -c 'ls-remote' "$TMP/argv.log")" = 2 ] || fail "7: expected two ls-remote runs (gate-waits, check): $(tr '\n' ';' < "$TMP/argv.log")"
# The same switches as git() of bureau-env.sh: no hooks directory, and every hook event git knows
# switched off for the hooks the configuration defines (git 2.55; ls-remote fires none of them).
OFF="-c core.hooksPath=/dev/null"
for event in $(/bin/bash -c 'source "$1/bureau-env.sh"; printf "%s" "$_BUREAU_GIT_HOOK_EVENTS"' _ "$SCRIPTS"); do OFF="$OFF -c hook.$event.enabled=false"; done
case "$OFF" in *hook.pre-push.enabled=false*hook.reference-transaction.enabled=false*) ;; *) fail "7: the event list of bureau-env.sh lacks pre-push or reference-transaction: $OFF" ;; esac
bare=$(grep 'ls-remote' "$TMP/argv.log" | grep -vF -- "$OFF ls-remote " || true)
[ -z "$bare" ] || fail "7: Bureau's Python ran a remote git without the hook switches of git(): $(printf '%s' "$bare" | cut -c1-200 | tr '\n' ';')"
hooks_off "7 bureau-supervision.py"
pr1_pass "7 the ls-remote of bureau-supervision.py (stop check, gate waits) runs with the hooks off"

# ── 8  every remote git in the templates gets the flag (static) ──────────────
# Shell: a script that runs push, fetch, pull, ls-remote, clone, remote or submodule has the git()
# function (it sources bureau-env.sh, directly or through bureau-config.sh), which adds the flag;
# tests/test_untrusted_env_bureau.sh A already checks that no git starts past that function.
# Python: every remote git command line carries NO_HOOKS. Prints each site it checked.
lint=$(python3 - "$SCRIPTS" <<'PY'
import pathlib, re, sys
scripts = pathlib.Path(sys.argv[1]); remote = '(push|fetch|pull|ls-remote|clone|remote|submodule)'
for path in sorted(scripts.glob('*.sh')):
    if path.name == 'bureau-env.sh': continue
    text = path.read_text()
    sources = re.search(r'^\s*(source|\.)\s+.*(bureau-config\.sh|bureau-env\.sh|\$BUREAU_CONFIG_SH)', text, re.M)
    for n, line in enumerate(text.splitlines(), 1):
        if line.lstrip().startswith('#') or not re.search(r'(^|[^\w.-])git(\s+(-C\s+\S+|-c\s+\S+|--[\w-]+(=\S+)?))*\s+' + remote + r'\b', line): continue
        print(('ok ' if sources else 'BAD (no git function) ') + path.name + ':' + str(n))
for path in sorted(scripts.glob('*.py')):
    for n, line in enumerate(path.read_text().splitlines(), 1):
        if line.lstrip().startswith('#') or not re.search(r"(\['git'|\bgit\(|quiet_git\().*'" + remote + "'", line): continue
        print(('ok ' if 'NO_HOOKS' in line else 'BAD (no NO_HOOKS) ') + path.name + ':' + str(n))
PY
)
grep -q ' bureau-supervision\.py:' <<< "$lint" || fail "8: the lint found no Python site (its pattern is broken)"
grep -q ' upstream-port\.sh:' <<< "$lint" || fail "8: the lint found no shell site in upstream-port.sh (its pattern is broken)"
bad=$(grep '^BAD' <<< "$lint" || true)
[ -z "$bad" ] || fail "8: a remote git without the hooks flag: $(printf '%s' "$bad" | tr '\n' ';')"
pr1_pass "8 every remote git in the templates runs through git() or carries NO_HOOKS ($(grep -c '^ok' <<< "$lint") sites)"

# ── 9  hooks the configuration defines (git 2.54 and later) ──────────────────
# hook.<name>.command + hook.<name>.event (git 2.54; what the pre-commit framework sets up for its
# pre-push stage, running hooks from the branch's .pre-commit-config.yaml) run whatever
# core.hooksPath says. Here the repository's configuration names a TRACKED script for pre-push and
# reference-transaction, under three names: scan, a name with `=` in it (cannot follow -c), and
# txscan; and it defines hooks named like the events (hook.pre-push.command,
# hook.reference-transaction.command), which makes git 2.55 read hook.<event>.enabled=false as a
# per-name switch, so only the per-name switches stop them, also for a fetch with -C from another
# directory (the names are listed with the command's own options).
# A name with an event and no command (git refuses every hook then) is switched off as well. With
# git 2.55 also a clone whose template brings a configured post-checkout hook (only the event
# switch reaches that one). Runs with the git of BUREAU_TEST_GIT_DIR when set, else with the git on
# PATH, and is skipped, with a SKIP line, when that git is older than 2.54.
GIT9_DIR="${BUREAU_TEST_GIT_DIR:-}"
GIT9_PATH="$PATH"; GIT9_EXEC=""
if [ -n "$GIT9_DIR" ]; then
  GIT9_PATH="$GIT9_DIR:$PATH"
  if [ -d "$GIT9_DIR/../libexec/git-core" ]; then GIT9_EXEC="$(cd "$GIT9_DIR/../libexec/git-core" && pwd)"; fi
fi
git9() { if [ -n "$GIT9_EXEC" ]; then PATH="$GIT9_PATH" GIT_EXEC_PATH="$GIT9_EXEC" "$@"; else PATH="$GIT9_PATH" "$@"; fi; }
GIT9_VERSION=$(git9 git --version 2>/dev/null | sed -n 's/^git version \([0-9][0-9]*\)\.\([0-9][0-9]*\).*/\1 \2/p')
if [ -n "$GIT9_VERSION" ] && { [ "${GIT9_VERSION% *}" -gt 2 ] || [ "${GIT9_VERSION#* }" -ge 54 ]; }; then
  hooks9() {  # <repo json> — a fresh repository with the configured hooks; Bureau's push and fetch
    repo "$R"; config "$1"
    mkdir -p "$R/tools"
    printf '%s\n' '#!/bin/sh' 'gh=no; [ -z "${GH_TOKEN:-}" ] || gh=yes' \
      "dotenv=no; /usr/bin/env | grep -qF -e '$PR1_LINEAR' -e '$PR1_TG_TOKEN' -e '$PR1_TG_CHAT' && dotenv=yes" \
      "echo \"config-hook \$1 gh=\$gh env=\$dotenv\" >> '$MARKS'" 'cat >/dev/null' 'exit 0' > "$R/tools/scan.sh"
    chmod +x "$R/tools/scan.sh"
    git -C "$R" add tools; git -C "$R" -c core.hooksPath=/dev/null commit -q -m tools
    git9 git -C "$R" config hook.scan.command "$R/tools/scan.sh pre-push"
    git9 git -C "$R" config --add hook.scan.event pre-push
    git9 git -C "$R" config 'hook.eq=name.command' "$R/tools/scan.sh pre-push-eq"
    git9 git -C "$R" config --add 'hook.eq=name.event' pre-push
    git9 git -C "$R" config hook.txscan.command "$R/tools/scan.sh reference-transaction"
    git9 git -C "$R" config --add hook.txscan.event reference-transaction
    git9 git -C "$R" config hook.pre-push.command "$R/tools/scan.sh named-like-the-event"
    git9 git -C "$R" config hook.reference-transaction.command "$R/tools/scan.sh named-like-the-event"
    upstream_commit; : > "$MARKS"
    # A local commit runs the configured hooks (reference-transaction), tokenless; then Bureau's
    # push and fetch from the stage shell, and a fetch with -C from another directory.
    (cd "$R" && git9 env "${PROBES[@]}" BUREAU_CONFIG="$R/.bureau.json" /bin/bash -c 'source "$1/bureau-config.sh"
      git checkout -q -b c9; echo w > w9.txt; git add w9.txt; git commit -q -m w9' _ "$SCRIPTS") >/dev/null 2>&1
    cp "$MARKS" "$TMP/local9.log"; : > "$MARKS"
    # A name with an event and no command makes git refuse to run any hook of the repository; it
    # is switched off like the others, so Bureau's remote commands still run (default only: with
    # the hooks on, git refuses them, as it would without Bureau).
    if [ "$1" = '{}' ]; then git9 git -C "$R" config hook.nocommand.event pre-push; fi
    out=$(cd "$R" && git9 env "${PROBES[@]}" BUREAU_CONFIG="$R/.bureau.json" /bin/bash -c 'source "$1/bureau-config.sh"
      git push -q origin HEAD; echo "push=$?"; git fetch -q origin; echo "fetch=$?"
      (cd / && git -C "$2" fetch -q origin; echo "fetch-C=$?")' _ "$SCRIPTS" "$R" 2>&1)
    case "$out" in *push=0*fetch=0*fetch-C=0*) ;; *) fail "9 $1: a remote command failed: $(printf '%s' "$out" | tr '\n' ' ')" ;; esac
    [ "$(git -C "$R.origin" rev-parse c9 2>/dev/null)" = "$(git -C "$R" rev-parse HEAD)" ] || fail "9 $1: the push did not land"
    # git 2.55: a clone whose template directory brings a configured post-checkout hook. git()
    # lists the names before the clone exists, so only the event switch stops this one; git 2.54
    # has no event switch and runs it (a documented limit; Bureau's stages run no clone).
    if [ "${GIT9_VERSION% *}" -gt 2 ] || [ "${GIT9_VERSION#* }" -ge 55 ]; then
      rm -rf "$TMP/tmpl9" "$TMP/clone9"; mkdir -p "$TMP/tmpl9"
      printf '[hook "fromtemplate"]\n\tcommand = %s post-checkout-template\n\tevent = post-checkout\n' "$R/tools/scan.sh" > "$TMP/tmpl9/config"
      out=$(cd "$R" && git9 env "${PROBES[@]}" BUREAU_CONFIG="$R/.bureau.json" /bin/bash -c 'source "$1/bureau-config.sh"
        git -c init.templateDir="$2" clone -q "$3" "$4"; echo "clone=$?"' _ "$SCRIPTS" "$TMP/tmpl9" "$R.origin" "$TMP/clone9" 2>&1)
      case "$out" in *clone=0*) ;; *) fail "9 $1: the clone failed: $(printf '%s' "$out" | tr '\n' ' ')" ;; esac
    fi
  }
  hooks9 '{}'
  if marks | grep -q '^config-hook'; then fail "9 default: a hook the configuration defines ran during Bureau's push or fetch: $(marks | sort | uniq -c | tr -s ' ' | tr '\n' ';')"; fi
  grep -q '^config-hook reference-transaction gh=no env=no' "$TMP/local9.log" || fail "9: the configured hooks did not run at all (the local commit ran none): $(tr '\n' ';' < "$TMP/local9.log")"
  hooks9 '{"remote_git_runs_hooks":true}'
  wanted='pre-push pre-push-eq reference-transaction'
  if [ "${GIT9_VERSION% *}" -gt 2 ] || [ "${GIT9_VERSION#* }" -ge 55 ]; then wanted="$wanted post-checkout-template"; fi
  for hook in $wanted; do
    marks | grep -q "^config-hook $hook gh=yes env=no" || fail "9 true: the configured $hook hook did not run with GH_TOKEN and without the .env keys: $(marks | sort -u | tr '\n' ';')"
  done
  pr1_pass "9 hooks the configuration defines stay off on Bureau's push and fetch ($(git9 git --version)); true runs them"
else
  echo "SKIP 9 hooks the configuration defines: needs git 2.54 or later, found $(git9 git --version 2>/dev/null || echo none); set BUREAU_TEST_GIT_DIR to the bin directory of one"
fi

if [ "$PR1_FAILS" != 0 ]; then echo "$PR1_FAILS check(s) failed" >&2; exit 1; fi
echo "OK test_remote_git_hooks"
