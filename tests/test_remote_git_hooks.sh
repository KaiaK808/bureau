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
#   5  doctor: reports the mode (remote_git_hooks: on, operator or off); a value that is neither a
#      JSON boolean nor "operator" is a warning; true, false, "operator" and absent are not
#   6  doctor: the main checkout uses Git LFS (filter=lfs, in any position, in its .gitattributes, a
#      committed or untracked assets/.gitattributes, or .git/info/attributes) and the key is not
#      true — a warning naming the key (also from a linked worktree); none for true, another filter,
#      a comment line or a name that only starts with lfs; for "operator" a warning only while the
#      git common dir's hooks/ has no executable pre-push
#   7  the two `git ls-remote` of bureau-supervision.py (a stopped review's check, the gate waits of
#      the pickers) get -c core.hooksPath=/dev/null and the hook event switches of git() too (a git
#      on PATH records its arguments)
#   8  static: every shell template with a remote git command has the git() function, and every
#      remote git command line in the Python templates carries NO_HOOKS
#   9  git 2.54 and later: hooks the configuration defines (hook.<name>.command and .event) run
#      neither on Bureau's push nor on its fetches, also under a name with `=` and next to a hook
#      named like the event, also under "operator"; true runs them (skipped, with a SKIP line, on
#      an older git)
#  10  repo.remote_git_runs_hooks: "operator" — the branch supplies hooks through core.hooksPath
#      (.githooks, tracked) and the operator has hooks in the git common dir's hooks/: Bureau's push
#      and fetch run only the operator's (with GH_TOKEN), from the main checkout, from a linked
#      worktree, with a path containing a space and with a git that predates --path-format; true
#      runs the configured .githooks as before, false and absent run none; outside a repository
#      the hooks stay off. Negative controls: a copy that counts "operator" as off, and one that
#      runs every configured hook for it, both fail the same assertion
#  11  off and "operator" and recursion: a populated submodule whose configuration includes a
#      tracked file defining configured pre-push and reference-transaction hooks (also with a dormant
#      hook.pre-push.command); Bureau's fetch (on-demand) and push (push.recurseSubmodules=on-demand)
#      start no child git in it; a call whose arguments ask for recursion (--recurse-submodules and
#      its unique prefixes such as --recurse-submodule=on-demand and --recurse-sub=yes, `submodule`,
#      clone --recursive and clone --recurse-submodules=no) is refused with 128; --recurse-submodule=no,
#      -c user.name=Recurse, an argument after `--` and a -C path containing the word are not;
#      recursion or core.hooksPath set through git's own -c, --config-env, -c include.path=<file> or
#      a -c behind --namespace's value loses to Bureau's options, which come after the caller's.
#      Every clone and a git option git does not define are refused; --attr-source and
#      --shallow-file with a separate value still get the overrides; the scan stops at an option
#      delimiter `--` (ls-remote -q -- origin --recurse-submodules runs), but skips a separate option
#      value of `--` and keeps scanning after an unknown option; true runs an unknown option as before. An include and
#      an includeIf inside the working tree, GIT_CONFIG_COUNT and GIT_CONFIG_PARAMETERS each set
#      core.hooksPath to the branch's .githooks (a plain push shows they do): only the operator's
#      hooks run. Recursion is detected through the objects that reach the submodule, on any git;
#      the submodule's configured hook can only fire on git 2.54 or later (BUREAU_TEST_GIT_DIR as in
#      9, SKIP otherwise). Negative controls: a copy without the recursion switches recurses on fetch
#      and push in both modes; a copy that lets an explicit recursion through recurses on the
#      explicit on-demand push; a copy with the previous exact-name scan recurses on the abbreviated
#      push; a copy with the previous option order lets an included file and the --namespace form
#      bring back recursion and the branch's hooks; a copy with the previous parse misreads
#      --attr-source and --shallow-file (recursion, the branch's pre-push, a broken call); a copy
#      with the previous `--` rule refuses the ls-remote; a copy that lets clone through runs a clone
#      template's hook next to a dormant hook.post-checkout.command (git 2.54 and later); a copy that keeps the configured core.hooksPath
#      for "operator" runs the branch's hooks in every include and environment case
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
# run_case <label> <command> [arguments] — one stage shell gets at most 60 s.
# Kill its process group too: git may be waiting for a hook's children. The
# main test reports the timeout even from a command substitution or a quiet control.
CASE_PID=$$
exec 3>&2
trap 'cat "$TMP/timeouts.log" >&3; exit 1' USR1
run_case() {
  local label="$1"; shift
  perl -e '
    use POSIX ();
    my ($label, $log, $test, @cmd) = @ARGV;
    my $pid = fork(); defined $pid or die "fork: $!";
    if (!$pid) { POSIX::setpgid(0, 0) == 0 or die "setpgid: $!"; exec @cmd; die "exec: $!"; }
    $SIG{ALRM} = sub {
      kill "KILL", -$pid; waitpid($pid, 0);
      open my $fh, ">>", $log or die "timeout log: $!";
      print $fh "FAIL $label: stage shell timed out after 60 s\n"; close $fh;
      kill "USR1", $test; exit 124;
    };
    alarm 60; waitpid($pid, 0); my $status = $?; alarm 0;
    exit(($status & 127) ? 128 + ($status & 127) : $status >> 8);
  ' "$label" "$TMP/timeouts.log" "$CASE_PID" "$@"
}
MARKS="$TMP/hooks.log"
PROBES=(LINEAR_API_KEY="$PR1_LINEAR" TELEGRAM_BOT_TOKEN="$PR1_TG_TOKEN" TELEGRAM_ALERT_CHAT_ID="$PR1_TG_CHAT"
        GH_TOKEN="$PR1_GH" GITHUB_TOKEN="$PR1_GITHUB" OPERATOR_TOOL_VAR="$PR1_OPERATOR")

# repo <dir> — a repository with a bare origin whose tracked .githooks record every run as one
# line: "<hook> <arg1> gh=<yes|no> env=<yes|no> refs=<refs on stdin>"; core.hooksPath names it.
# The first call for a path builds the repositories and keeps a copy under $TMP/cache; later calls
# for the same path restore that copy (the paths inside stay valid), which saves the git runs.
repo() {
  local r="$1" h cache
  cache="$TMP/cache/$(printf '%s' "$r" | cksum | tr ' ' _)"
  rm -rf "$r" "$r.origin" "$r.other"
  if [ -d "$cache" ]; then
    cp -Rp "$cache/r" "$r"; cp -Rp "$cache/origin" "$r.origin"; cp -Rp "$cache/other" "$r.other"
    : > "$MARKS"; return 0
  fi
  mkdir -p "$r/.githooks"
  git -C "$r" init -q -b main; git -C "$r" config user.email t@t; git -C "$r" config user.name t
  for h in pre-push reference-transaction pre-commit post-checkout post-merge; do
    cat > "$r/.githooks/$h" <<EOF
#!/bin/sh
refs=
while IFS= read -r line; do refs="\$refs\${line##* },"; done 2>/dev/null
gh=no; [ -z "\${GH_TOKEN:-}" ] || gh=yes
dotenv=no; /usr/bin/env | grep -F -e '$PR1_LINEAR' -e '$PR1_TG_TOKEN' -e '$PR1_TG_CHAT' >/dev/null && dotenv=yes
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
  mkdir -p "$cache"; cp -Rp "$r" "$cache/r"; cp -Rp "$r.origin" "$cache/origin"; cp -Rp "$r.other" "$cache/other"
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
  (cd "$R" && run_case "stage ($SCRIPTS): $1" env "${PROBES[@]}" BUREAU_CONFIG="$R/.bureau.json" /bin/bash -c 'set -uo pipefail; source "$1/bureau-config.sh"; '"$1" _ "$SCRIPTS")
}
config() { printf '{"repo":%s}\n' "$1" > "$R/.bureau.json"; }
marks() { cat "$MARKS" 2>/dev/null; }

# remote_round <label> — from a stage shell: a local branch and commit (their hook runs go to
# $TMP/local.log), then Bureau's push of that branch and a fetch that updates origin/main (their
# hook runs stay in $MARKS), and an ls-remote.
remote_round() {
  upstream_commit
  : > "$MARKS"
  # One stage shell: the local commit (its hook runs go to $TMP/local.log), then Bureau's remote
  # commands (their hook runs stay in $MARKS).
  out=$(stage "{ git checkout -q -b c-$ROUND; echo w > w-$ROUND.txt; git add w-$ROUND.txt; git commit -q -m w-$ROUND; } >/dev/null 2>&1
    cp '$MARKS' '$TMP/local.log'; : > '$MARKS'"'
    git push -q origin HEAD; echo "push=$?"; git fetch -q origin; echo "fetch=$?"
    git ls-remote origin refs/heads/main >/dev/null; echo "ls-remote=$?"' 2>&1)
  case "$out" in *push=0*fetch=0*ls-remote=0*) ;; *) fail "$1: a remote command failed: $(printf '%s' "$out" | tr '\n' ' ')" ;; esac
  [ "$(git -C "$R" rev-parse origin/main)" = "$(git -C "$R.origin" rev-parse main)" ] || fail "$1: the fetch did not update origin/main"
  [ "$(git -C "$R.origin" rev-parse "c-$ROUND" 2>/dev/null)" = "$(git -C "$R" rev-parse HEAD)" ] || fail "$1: the pushed branch is not on origin"
}
hooks_off() {  # <label>
  if grep -q '^pre-push ' <<< "$(marks)"; then fail "$1: pre-push ran during Bureau's push ($(grep '^pre-push ' <<< "$(marks)" | sed -n 1p))"; fi
  if grep -q 'refs=.*refs/remotes/' <<< "$(marks)"; then fail "$1: reference-transaction ran for a remote-tracking ref ($(grep 'refs/remotes/' <<< "$(marks)" | sed -n 1p))"; fi
  if grep -q 'gh=yes' <<< "$(marks)"; then fail "$1: a hook saw GH_TOKEN"; fi
}

# ── 1  default: no hooks on Bureau's remote git ───────────────────────────────
R="$TMP/repo"; repo "$R"; config '{}'
remote_round "1 default"
hooks_off "1 default"
# Local commands keep running the hooks, without any secret.
grep -q '^pre-commit ' "$TMP/local.log" || fail "1: the local commit did not run pre-commit"
grep -q '^reference-transaction .*refs/heads/' "$TMP/local.log" || fail "1: the local commit did not run reference-transaction"
if grep -q 'gh=yes\|env=yes' "$TMP/local.log"; then fail "1: a local hook saw a secret: $(grep 'gh=yes\|env=yes' "$TMP/local.log" | sed -n 1p)"; fi
# The real merge_origin_main_or_abort: its fetch runs no hook, its local merge runs them, tokenless.
git -C "$R" checkout -q main; git -C "$R" checkout -q -b behind; git -C "$R" reset -q --hard "$(git -C "$R" rev-list --max-parents=0 HEAD)"
echo mine > "$R/mine.txt"; git -C "$R" add mine.txt; git -C "$R" -c core.hooksPath=/dev/null commit -q -m mine
upstream_commit; : > "$MARKS"
out=$(stage 'merge_origin_main_or_abort EXP-1 QA >/dev/null 2>&1; echo "merge=$?"')
case "$out" in *merge=0*) ;; *) fail "1: merge_origin_main_or_abort failed: $out" ;; esac
git -C "$R" merge-base --is-ancestor "$(git -C "$R.origin" rev-parse main)" HEAD || fail "1: merge_origin_main_or_abort did not merge the fetched main"
hooks_off "1 merge_origin_main_or_abort"
grep -q '^reference-transaction .*refs/heads/behind' <<< "$(marks)" || fail "1: the local merge of merge_origin_main_or_abort ran no hook"
pr1_pass "1 default: Bureau's push and fetch run no hook; local commands run them without secrets"

# ── 2  repo.remote_git_runs_hooks: true ───────────────────────────────────────
repo "$R"; config '{"remote_git_runs_hooks":true}'
remote_round "2 true"
grep -q '^pre-push origin gh=yes env=no' <<< "$(marks)" || fail "2: pre-push did not run with GH_TOKEN and without the .env keys: $(marks | tr '\n' ';')"
grep -q '^reference-transaction .*gh=yes env=no refs=.*refs/remotes/origin/main' <<< "$(marks)" || fail "2: the fetch ran no reference-transaction with the token: $(marks | tr '\n' ';')"
if grep -q 'env=yes' <<< "$(marks)"; then fail "2: a hook saw a .env key"; fi
pr1_pass "2 true: the hooks run again on push and fetch, with the GitHub token and without the .env keys"

# ── 3  other values keep the hooks off ────────────────────────────────────────
for value in false '"true"' 1 '"yes"' '{}' null; do
  repo "$R"; config "{\"remote_git_runs_hooks\":$value}"
  remote_round "3 $value"; hooks_off "3 $value"
done
repo "$R"; printf '{"repo": {"remote_git_runs_hooks": true' > "$R/.bureau.json.broken"
upstream_commit; : > "$MARKS"
(cd "$R" && run_case '3 unreadable' env "${PROBES[@]}" BUREAU_CONFIG="$R/.bureau.json.broken" /bin/bash -c 'source "$1/bureau-env.sh"; git push -q origin HEAD:refs/heads/broken; git fetch -q origin' _ "$SCRIPTS") >/dev/null 2>&1
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
(cd "$R" && run_case '4 bureau-env.sh alone' env -u BUREAU_CONFIG "${PROBES[@]}" /bin/bash -c 'source "$1/bureau-env.sh"; git push -q origin HEAD:refs/heads/alone; git fetch -q origin' _ "$SCRIPTS") >/dev/null 2>&1
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
  if [ -n "${DOCTOR_SETUP:-}" ]; then eval "$DOCTOR_SETUP"; fi
  (cd "$at" && python3 "$d/scripts/bureau-doctor.py" --repo "$at") > "$TMP/doctor.json"
  jq -r '(.warnings // [])[], (.errors // [])[]' "$TMP/doctor.json" | grep 'remote_git_runs_hooks' || true
}
doctor_mode() { jq -r '.remote_git_hooks // "missing"' "$TMP/doctor.json"; }
for pair in true:on false:off '"operator"':operator; do
  value="${pair%:*}"
  [ -z "$(doctor_warnings "{\"remote_git_runs_hooks\":$value}")" ] || fail "5: doctor warns on $value"
  [ "$(doctor_mode)" = "${pair##*:}" ] || fail "5: doctor reports the mode $(doctor_mode) for $value, wanted ${pair##*:}"
done
[ -z "$(doctor_warnings '{}')" ] || fail "5: doctor warns on an absent key"
[ "$(doctor_mode)" = off ] || fail "5: doctor reports the mode $(doctor_mode) for an absent key"
for value in '"true"' 1 '"yes"' '"Operator"'; do
  w=$(doctor_warnings "{\"remote_git_runs_hooks\":$value}")
  case "$w" in *'is not a JSON boolean or "operator"'*) ;; *) fail "5: doctor does not warn on $value: ${w:-no warning}" ;; esac
  [ "$(doctor_mode)" = off ] || fail "5: doctor reports the mode $(doctor_mode) for $value"
done
pr1_pass "5 doctor reports the mode (on, operator, off) and warns on a value that is not a JSON boolean or \"operator\""

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
# "operator" runs the common dir's hooks/: the LFS pre-push must be there (git lfs install puts it
# there when core.hooksPath is not set). Without it a warning; with an executable one none.
w=$(doctor_warnings '{"remote_git_runs_hooks":"operator"}' "$LFS")
case "$w" in *'"operator" and the main checkout uses Git LFS'*) ;; *) fail "6: no warning for operator without a pre-push hook: ${w:-no warning}" ;; esac
case "$w" in *"is not true, but"*) fail "6: the off-mode LFS warning for operator" ;; esac
DOCTOR_SETUP='printf "#!/bin/sh\nexit 0\n" > "$d/.git/hooks/pre-push"; chmod +x "$d/.git/hooks/pre-push"'
w=$(doctor_warnings '{"remote_git_runs_hooks":"operator"}' "$LFS")
case "$w" in *"uses Git LFS"*) fail "6: a Git LFS warning for operator with an executable pre-push: $w" ;; esac
w=$(doctor_warnings '{"remote_git_runs_hooks":"operator"}' "$LFS" worktree)
case "$w" in *"uses Git LFS"*) fail "6: a Git LFS warning for operator from a linked worktree with an executable pre-push: $w" ;; esac
DOCTOR_SETUP=''
pr1_pass "6 doctor warns when the main checkout uses Git LFS and the hooks are off, or on operator without a pre-push hook"

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
for event in $(run_case '7 event list' /bin/bash -c 'source "$1/bureau-env.sh"; printf "%s" "$_BUREAU_GIT_HOOK_EVENTS"' _ "$SCRIPTS"); do OFF="$OFF -c hook.$event.enabled=false"; done
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
# The empty name (`[hook ""]`) counts too, and so does a hook when the operator's environment
# exports GIT_CONFIG (which only `git config` reads). A name with an event and no command (git
# refuses every hook then) is switched off as well. With
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
  hooks9() {  # <repo json> [GIT_CONFIG value] — a fresh repository with the configured hooks;
    # Bureau's push and fetch, with GIT_CONFIG exported in the stage shell when given
    repo "$R"; config "$1"
    mkdir -p "$R/tools"
    printf '%s\n' '#!/bin/sh' 'gh=no; [ -z "${GH_TOKEN:-}" ] || gh=yes' \
      "dotenv=no; /usr/bin/env | grep -F -e '$PR1_LINEAR' -e '$PR1_TG_TOKEN' -e '$PR1_TG_CHAT' >/dev/null && dotenv=yes" \
      "echo \"config-hook \$1 gh=\$gh env=\$dotenv\" >> '$MARKS'" 'while IFS= read -r line; do :; done 2>/dev/null' 'exit 0' > "$R/tools/scan.sh"
    chmod +x "$R/tools/scan.sh"
    git -C "$R" add tools; git -C "$R" -c core.hooksPath=/dev/null commit -q -m tools
    git9 git -C "$R" config hook.scan.command "$R/tools/scan.sh pre-push"
    git9 git -C "$R" config --add hook.scan.event pre-push
    git9 git -C "$R" config 'hook.eq=name.command' "$R/tools/scan.sh pre-push-eq"
    git9 git -C "$R" config --add 'hook.eq=name.event' pre-push
    git9 git -C "$R" config 'hook..command' "$R/tools/scan.sh pre-push-empty"
    git9 git -C "$R" config --add 'hook..event' pre-push
    git9 git -C "$R" config hook.txscan.command "$R/tools/scan.sh reference-transaction"
    git9 git -C "$R" config --add hook.txscan.event reference-transaction
    git9 git -C "$R" config hook.pre-push.command "$R/tools/scan.sh named-like-the-event"
    git9 git -C "$R" config hook.reference-transaction.command "$R/tools/scan.sh named-like-the-event"
    upstream_commit; : > "$MARKS"
    # A local commit runs the configured hooks (reference-transaction), tokenless; then Bureau's
    # push and fetch from the stage shell, and a fetch with -C from another directory.
    (cd "$R" && git9 run_case "9 $1 local commit" env "${PROBES[@]}" BUREAU_CONFIG="$R/.bureau.json" /bin/bash -c 'source "$1/bureau-config.sh"
      git checkout -q -b c9; echo w > w9.txt; git add w9.txt; git commit -q -m w9' _ "$SCRIPTS") >/dev/null 2>&1
    cp "$MARKS" "$TMP/local9.log"; : > "$MARKS"
    # A name with an event and no command makes git refuse to run any hook of the repository; it
    # is switched off like the others, so Bureau's remote commands still run (default only: with
    # the hooks on, git refuses them, as it would without Bureau).
    if [ "$1" = '{}' ]; then git9 git -C "$R" config hook.nocommand.event pre-push; fi
    out=$(cd "$R" && git9 run_case "9 $1 push and fetch" env "${PROBES[@]}" BUREAU_CONFIG="$R/.bureau.json" ${2:+GIT_CONFIG="$2"} /bin/bash -c 'source "$1/bureau-config.sh"
      git push -q origin HEAD; echo "push=$?"; git fetch -q origin; echo "fetch=$?"
      (cd / && git -C "$2" fetch -q origin; echo "fetch-C=$?")' _ "$SCRIPTS" "$R" 2>&1)
    case "$out" in *push=0*fetch=0*fetch-C=0*) ;; *) fail "9 $1: a remote command failed: $(printf '%s' "$out" | tr '\n' ' ')" ;; esac
    [ "$(git -C "$R.origin" rev-parse c9 2>/dev/null)" = "$(git -C "$R" rev-parse HEAD)" ] || fail "9 $1: the push did not land"
    # A clone whose template directory brings a configured post-checkout hook: git() cannot list
    # its names before the clone exists, so unless the key is true Bureau's clone is refused (128,
    # nothing cloned); with true it runs the template's hook (git 2.55 runs hooks on a clone's
    # checkout from the template configuration).
    rm -rf "$TMP/tmpl9" "$TMP/clone9"; mkdir -p "$TMP/tmpl9"
    printf '[hook "fromtemplate"]\n\tcommand = %s post-checkout-template\n\tevent = post-checkout\n' "$R/tools/scan.sh" > "$TMP/tmpl9/config"
    out=$(cd "$R" && git9 run_case "9 $1 clone" env "${PROBES[@]}" BUREAU_CONFIG="$R/.bureau.json" /bin/bash -c 'source "$1/bureau-config.sh"
      git -c init.templateDir="$2" clone -q "$3" "$4"; echo "clone=$?"' _ "$SCRIPTS" "$TMP/tmpl9" "$R.origin" "$TMP/clone9" 2>&1)
    if [ "$1" = '{"remote_git_runs_hooks":true}' ]; then
      case "$out" in *clone=0*) ;; *) fail "9 $1: the clone failed: $(printf '%s' "$out" | tr '\n' ' ')" ;; esac
    else
      case "$out" in *"bureau git: refused 'git clone'"*clone=128*) ;; *) fail "9 $1: the clone was not refused: $(printf '%s' "$out" | tr '\n' ' ')" ;; esac
      [ ! -e "$TMP/clone9" ] || fail "9 $1: a refused clone created its directory"
    fi
  }
  hooks9 '{}'
  if grep -q '^config-hook' <<< "$(marks)"; then fail "9 default: a hook the configuration defines ran during Bureau's push or fetch: $(marks | sort | uniq -c | tr -s ' ' | tr '\n' ';')"; fi
  # GIT_CONFIG from the operator's environment: `git config` alone would read only that file, the
  # remote command reads the repository's configuration all the same.
  hooks9 '{}' /dev/null
  if grep -q '^config-hook' <<< "$(marks)"; then fail "9 GIT_CONFIG=/dev/null: a hook the configuration defines ran during Bureau's push or fetch: $(marks | sort | uniq -c | tr -s ' ' | tr '\n' ';')"; fi
  grep -q '^config-hook reference-transaction gh=no env=no' "$TMP/local9.log" || fail "9: the configured hooks did not run at all (the local commit ran none): $(tr '\n' ';' < "$TMP/local9.log")"
  # "operator" keeps the per-name switches: the configured hooks stay off as in the default.
  hooks9 '{"remote_git_runs_hooks":"operator"}'
  if grep -q '^config-hook' <<< "$(marks)"; then fail "9 operator: a hook the configuration defines ran during Bureau's push or fetch: $(marks | sort | uniq -c | tr -s ' ' | tr '\n' ';')"; fi
  hooks9 '{"remote_git_runs_hooks":true}'
  wanted='pre-push pre-push-eq pre-push-empty reference-transaction'
  if [ "${GIT9_VERSION% *}" -gt 2 ] || [ "${GIT9_VERSION#* }" -ge 55 ]; then wanted="$wanted post-checkout-template"; fi
  for hook in $wanted; do
    grep -q "^config-hook $hook gh=yes env=no" <<< "$(marks)" || fail "9 true: the configured $hook hook did not run with GH_TOKEN and without the .env keys: $(marks | sort -u | tr '\n' ';')"
  done
  pr1_pass "9 hooks the configuration defines stay off on Bureau's push and fetch ($(git9 git --version)); true runs them"
else
  echo "SKIP 9 hooks the configuration defines: needs git 2.54 or later, found $(git9 git --version 2>/dev/null || echo none); set BUREAU_TEST_GIT_DIR to the bin directory of one"
fi

# ── 10  "operator": only the main checkout's own hooks directory ──────────────
# The fixture's branch supplies its hooks through core.hooksPath=.githooks (tracked, as husky
# does); the operator's hooks sit in the git common dir's hooks/, which no commit can write. Both
# record every run, the operator's as "operator-<hook>". Bureau's push and fetch run from the main
# checkout and from a linked worktree (where the stages run), with the real bureau-config.sh.
# operator_hooks <dir> — the operator's pre-push and reference-transaction in <dir>.
operator_hooks() {
  local h
  mkdir -p "$1"
  for h in pre-push reference-transaction; do
    cat > "$1/$h" <<EOF
#!/bin/sh
refs=
while IFS= read -r line; do refs="\$refs\${line##* },"; done 2>/dev/null
gh=no; [ -z "\${GH_TOKEN:-}" ] || gh=yes
echo "operator-$h \${1:-} gh=\$gh refs=\$refs dir=\$(git rev-parse --absolute-git-dir </dev/null 2>/dev/null)" >> '$MARKS'
exit 0
EOF
    chmod +x "$1/$h"
  done
}
# worktree_round <label> — Bureau's push of a new branch and a fetch that updates origin/main, from
# a linked worktree of $R (a stage worktree).
worktree_round() {
  local wt="$TMP/wt-$ROUND"
  upstream_commit
  rm -rf "$wt"; git -C "$R" -c core.hooksPath=/dev/null worktree add -q -b "wt-$ROUND" "$wt" main
  echo w > "$wt/w.txt"; git -C "$wt" add w.txt; git -C "$wt" -c core.hooksPath=/dev/null commit -q -m "wt $ROUND"
  : > "$MARKS"
  out=$(cd "$wt" && run_case "$1" env "${PROBES[@]}" BUREAU_CONFIG="$R/.bureau.json" /bin/bash -c 'set -uo pipefail; source "$1/bureau-config.sh"
    git push -q origin HEAD; echo "push=$?"; git fetch -q origin; echo "fetch=$?"' _ "$SCRIPTS" 2>&1)
  case "$out" in *push=0*fetch=0*) ;; *) fail "$1: a remote command failed: $(printf '%s' "$out" | tr '\n' ' ')" ;; esac
  [ "$(git -C "$R.origin" rev-parse "wt-$ROUND" 2>/dev/null)" = "$(git -C "$wt" rev-parse HEAD)" ] || fail "$1: the pushed branch is not on origin"
}
# operator_only <label> — the operator's hooks ran on the push (with the token) and on the fetch's
# remote-tracking update, and no hook of the branch's .githooks did.
operator_only() {
  grep -q '^operator-pre-push origin gh=yes' <<< "$(marks)" || fail "$1: the operator's pre-push did not run with the token: $(marks | tr '\n' ';')"
  grep -q '^operator-reference-transaction .*refs=.*refs/remotes/origin/main' <<< "$(marks)" \
    || fail "$1: the operator's reference-transaction did not see the fetch: $(marks | tr '\n' ';')"
  if grep -qv '^operator-' <<< "$(marks)"; then fail "$1: a hook from the branch's core.hooksPath ran: $(grep -v '^operator-' <<< "$(marks)" | sed -n 1p)"; fi
}
repo "$R"; config '{"remote_git_runs_hooks":"operator"}'; operator_hooks "$R/.git/hooks"
remote_round "10 operator"
operator_only "10 operator"
grep -q '^pre-commit ' "$TMP/local.log" || fail "10 operator: the local commit no longer ran the branch's pre-commit"
if grep -q '^operator-' "$TMP/local.log"; then fail "10 operator: a local command ran the operator's hooks instead of the configured ones"; fi
worktree_round "10 operator worktree"
operator_only "10 operator worktree"
# A git older than 2.31 does not know --path-format and prints it back, then a relative common
# dir; git() then takes the common dir from the git dir's commondir file. A git on PATH that
# answers like that, from the main checkout and from a worktree.
mkdir -p "$TMP/oldgit"
cat > "$TMP/oldgit/git" <<EOF
#!/bin/bash
for a in "\$@"; do
  if [ "\$a" = --path-format=absolute ]; then echo old-git-asked >> '$TMP/oldgit.log'; echo --path-format=absolute; exit 0; fi
done
exec '$(type -P git)' "\$@"
EOF
chmod +x "$TMP/oldgit/git"; : > "$TMP/oldgit.log"
PATH_BEFORE_OLDGIT="$PATH"; PATH="$TMP/oldgit:$PATH"
remote_round "10 operator old git"; operator_only "10 operator old git"
worktree_round "10 operator old git worktree"; operator_only "10 operator old git worktree"
PATH="$PATH_BEFORE_OLDGIT"
grep -q old-git-asked "$TMP/oldgit.log" || fail "10 old git: the wrapper was never asked (the fallback went untested)"
# A path with a space in it and an absolute core.hooksPath the branch cannot write either: still
# only the common dir's hooks/.
R="$TMP/repo with space"; repo "$R"; config '{"remote_git_runs_hooks":"operator"}'; operator_hooks "$R/.git/hooks"
git -C "$R" config core.hooksPath "$R/.githooks"
remote_round "10 operator space"
operator_only "10 operator space"
R="$TMP/repo"
# true: the configured core.hooksPath (the branch's .githooks) as before, not the operator's.
repo "$R"; config '{"remote_git_runs_hooks":true}'; operator_hooks "$R/.git/hooks"
remote_round "10 true"
grep -q '^pre-push origin gh=yes' <<< "$(marks)" || fail "10 true: the configured pre-push did not run: $(marks | tr '\n' ';')"
if grep -q '^operator-' <<< "$(marks)"; then fail "10 true: the operator's hooks ran although core.hooksPath names .githooks"; fi
# false and absent: none.
for value in false absent; do
  repo "$R"; if [ "$value" = absent ]; then config '{}'; else config "{\"remote_git_runs_hooks\":$value}"; fi
  operator_hooks "$R/.git/hooks"
  remote_round "10 $value"; hooks_off "10 $value"
  if grep -q '^operator-' <<< "$(marks)"; then fail "10 $value: the operator's hooks ran"; fi
done
# Outside a repository (an ls-remote by URL) "operator" keeps the hooks off and the command works.
repo "$R"; config '{"remote_git_runs_hooks":"operator"}'; operator_hooks "$R/.git/hooks"; : > "$MARKS"
out=$(cd "$TMP" && run_case '10 operator outside a repository' env "${PROBES[@]}" BUREAU_CONFIG="$R/.bureau.json" /bin/bash -c 'source "$1/bureau-env.sh"
  git ls-remote "$2" refs/heads/main >/dev/null; echo "ls-remote=$?"' _ "$SCRIPTS" "$R.origin" 2>&1)
case "$out" in *ls-remote=0*) ;; *) fail "10 operator outside a repository: ls-remote failed: $out" ;; esac
# Negative control: the same "operator" case against a copy of the scripts whose mode reader
# counts "operator" as off, as bureau-env.sh did before this change: the operator's hooks never run.
mkdir -p "$TMP/before"; cp "$SCRIPTS"/*.sh "$TMP/before/"
sed -e '/^_bureau_remote_git_hooks_mode() {/,/^}/c\
_bureau_remote_git_hooks_mode() { printf "off\\n"; }' "$SCRIPTS/bureau-env.sh" > "$TMP/before/bureau-env.sh"
cmp -s "$SCRIPTS/bureau-env.sh" "$TMP/before/bureau-env.sh" && fail "10 control: the copy is unchanged (the sed found nothing)"
repo "$R"; config '{"remote_git_runs_hooks":"operator"}'; operator_hooks "$R/.git/hooks"
SCRIPTS_REAL="$SCRIPTS"; SCRIPTS="$TMP/before"
remote_round "10 control"
SCRIPTS="$SCRIPTS_REAL"
control_fails=$PR1_FAILS
operator_only "10 control" 2>/dev/null
if [ "$PR1_FAILS" = "$control_fails" ]; then fail "10 control: operator_only passed against a copy without the operator mode"; else PR1_FAILS=$control_fails; fi
# A second control: a copy whose "operator" runs every configured hook, as true does (the
# branch's .githooks): operator_only fails on it too, so it notices a mode that runs them.
sed -e '/^_bureau_remote_git_hooks_mode() {/,/^}/c\
_bureau_remote_git_hooks_mode() { printf "on\\n"; }' "$SCRIPTS/bureau-env.sh" > "$TMP/before/bureau-env.sh"
repo "$R"; config '{"remote_git_runs_hooks":"operator"}'; operator_hooks "$R/.git/hooks"
SCRIPTS="$TMP/before"
remote_round "10 control 2"
SCRIPTS="$SCRIPTS_REAL"
control_fails=$PR1_FAILS
operator_only "10 control 2" 2>/dev/null
if [ "$PR1_FAILS" = "$control_fails" ]; then fail "10 control 2: operator_only passed against a copy that runs the configured hooks"; else PR1_FAILS=$control_fails; fi
pr1_pass "10 \"operator\" runs only the common dir's hooks (main checkout and worktree), true the configured ones, false and absent none"

# ── 11  restricted hooks: submodule recursion, includes and the configuration environment ──
# A push with push.recurseSubmodules=on-demand and a fetch (fetch.recurseSubmodules defaults to
# on-demand) start a child git in each populated submodule. That child reads the submodule's own
# configuration, here a file tracked in the submodule that its configuration includes, which
# defines hook.probe.command for pre-push and reference-transaction (git 2.54 and later): names
# git() cannot list beforehand; the "dormant" variant adds hook.pre-push.command without an event,
# which on git 2.55 turns the event switch into a per-name one. With the hooks restricted ("off"
# and "operator") Bureau's remote git therefore never recurses: -c push.recurseSubmodules=no
# -c fetch.recurseSubmodules=no -c submodule.recurse=false on every call, after the caller's own
# options of git, and a call whose arguments ask for recursion (`submodule`, --recurse-submodules
# under any prefix git accepts, --recursive) is refused with 128. Recursion is detected without hooks, on any git: the fetch recursed when the
# submodule's object store holds origin's new submodule commit, the push recursed when the
# submodule's remote received the local submodule commit. The submodule's configured hook (a probe
# that records the token) is checked as well and can only fire on git 2.54 or later (the git of
# BUREAU_TEST_GIT_DIR when set, as in section 9). Then an include inside the working tree and
# GIT_CONFIG_COUNT / GIT_CONFIG_PARAMETERS in the environment that set core.hooksPath to the
# branch's directory: the command-line -c wins over each.
GIT_NEW=0
if [ -n "$GIT9_VERSION" ] && { [ "${GIT9_VERSION% *}" -gt 2 ] || [ "${GIT9_VERSION#* }" -ge 54 ]; }; then GIT_NEW=1; fi
probe_script() {  # <path> <label> — a branch-supplied hook command that records the token
  printf '%s\n' '#!/bin/sh' 'gh=no; [ -z "${GH_TOKEN:-}" ] || gh=yes' "echo \"$2 \$1 gh=\$gh\" >> '$MARKS'" 'while IFS= read -r line; do :; done 2>/dev/null' 'exit 0' > "$1"
  chmod +x "$1"
}
# bureau_git <shell code> — a stage shell in $R with the probes, the real bureau-config.sh of
# $SCRIPTS and the git of BUREAU_TEST_GIT_DIR when set.
bureau_git() {
  (cd "$R" && git9 run_case "11 ($SCRIPTS): $1" env "${PROBES[@]}" BUREAU_CONFIG="$R/.bureau.json" /bin/bash -c 'set -uo pipefail; source "$1/bureau-config.sh"
    '"$1" _ "$SCRIPTS" 2>&1)
}
# submodule_fixture <repo json> [dormant] — $R as in section 10 plus a populated submodule `sub`
# whose configuration includes its tracked hooks.cfg; $R.other has it too, to move origin's
# submodule pointer. Then: origin's pointer moves to UP_SHA (a fetch would recurse on demand),
# and locally a new submodule commit MINE_SHA, on no remote yet, is committed in the branch
# REC_BRANCH (a push would recurse with push.recurseSubmodules=on-demand, which $R sets). Built
# once and restored by copy for every case (the paths inside stay valid); the repo json and the
# dormant hook.pre-push.command are applied after the restore.
REC_BRANCH=rec-11
submodule_fixture() {
  local s="$TMP/sub" m="$R/.git/modules/sub" c="$TMP/cache11"
  rm -rf "$R" "$R.origin" "$R.other" "$s" "$s.origin"
  if [ -d "$c" ]; then
    cp -Rp "$c/r" "$R"; cp -Rp "$c/origin" "$R.origin"; cp -Rp "$c/other" "$R.other"
    cp -Rp "$c/sub" "$s"; cp -Rp "$c/sub.origin" "$s.origin"
  else
    repo "$R"; operator_hooks "$R/.git/hooks"
    mkdir -p "$s"
    git -C "$s" init -q -b main; git -C "$s" config user.email t@t; git -C "$s" config user.name t
    probe_script "$s/probe.sh" sub-config-hook
    printf '[hook "probe"]\n\tcommand = %s/probe.sh pre-push\n\tevent = pre-push\n[hook "probetx"]\n\tcommand = %s/probe.sh reference-transaction\n\tevent = reference-transaction\n' "$s" "$s" > "$s/hooks.cfg"
    git -C "$s" add -A; git -C "$s" commit -q -m sub
    git init -q --bare "$s.origin"; git -C "$s.origin" symbolic-ref HEAD refs/heads/main
    git -C "$s" push -q "$s.origin" main
    git -C "$R" -c core.hooksPath=/dev/null -c protocol.file.allow=always submodule -q add "$s.origin" sub
    git -C "$R" -c core.hooksPath=/dev/null commit -q -m 'add sub'
    git -C "$R" -c core.hooksPath=/dev/null push -q origin main
    git --git-dir="$m" config protocol.file.allow always
    git --git-dir="$m" config include.path ../../../sub/hooks.cfg
    git --git-dir="$m" config user.email t@t; git --git-dir="$m" config user.name t
    git -C "$R" config protocol.file.allow always
    git -C "$R" config push.recurseSubmodules on-demand
    rm -rf "$R.other"; git -c protocol.file.allow=always clone -q --recurse-submodules "$R.origin" "$R.other" 2>/dev/null
    git -C "$R.other" config user.email t@t; git -C "$R.other" config user.name t
    git -C "$R.other/sub" config user.email t@t; git -C "$R.other/sub" config user.name t
    (cd "$R.other/sub" && echo up > up.txt && git add up.txt && git -c core.hooksPath=/dev/null commit -q -m up \
       && git -c core.hooksPath=/dev/null push -q origin HEAD:main) \
      && (cd "$R.other" && git add sub && git -c core.hooksPath=/dev/null commit -q -m 'move sub' \
       && git -c core.hooksPath=/dev/null push -q origin HEAD:main) || fail "11 fixture: could not move origin's submodule"
    git -C "$R" checkout -q -b "$REC_BRANCH"
    (cd "$R/sub" && git checkout -q -B "$REC_BRANCH" && echo mine > mine.txt && git add mine.txt && git -c core.hooksPath=/dev/null commit -q -m mine)
    git -C "$R" add sub; git -C "$R" -c core.hooksPath=/dev/null commit -q -m 'local sub'
    mkdir -p "$c"; cp -Rp "$R" "$c/r"; cp -Rp "$R.origin" "$c/origin"; cp -Rp "$R.other" "$c/other"
    cp -Rp "$s" "$c/sub"; cp -Rp "$s.origin" "$c/sub.origin"
  fi
  UP_SHA=$(git -C "$R.other/sub" rev-parse HEAD); MINE_SHA=$(git -C "$R/sub" rev-parse HEAD)
  config "$1"
  if [ "${2:-}" = dormant ]; then
    printf '[hook "pre-push"]\n\tcommand = %s/probe.sh dormant\n' "$s" >> "$R/sub/hooks.cfg"
    [ "$(git -C "$R/sub" config --get hook.pre-push.command)" = "$s/probe.sh dormant" ] || fail "11 fixture: the submodule's include does not see the dormant hook"
  else
    if git -C "$R/sub" config --get hook.pre-push.command >/dev/null; then fail "11 fixture: the plain variant has a dormant hook"; fi
  fi
  : > "$MARKS"
}
fetch_recursed() { git -C "$R/sub" cat-file -e "$UP_SHA^{commit}" 2>/dev/null; }
push_recursed() { git -C "$TMP/sub.origin" cat-file -e "$MINE_SHA^{commit}" 2>/dev/null; }
config_hook_ran() { grep -q '^sub-config-hook' <<< "$(marks)"; }
# no_recursion <label> — after Bureau's fetch and push: no child git in the submodule, no
# configured hook of the submodule, and the superproject's push landed.
no_recursion() {
  if fetch_recursed; then fail "$1: the fetch recursed into the submodule"; fi
  if push_recursed; then fail "$1: the push recursed into the submodule"; fi
  if config_hook_ran; then fail "$1: the submodule's configured hook ran: $(grep '^sub-config-hook' <<< "$(marks)" | sed -n 1p)"; fi
  [ "$(git -C "$R.origin" rev-parse "$REC_BRANCH" 2>/dev/null)" = "$(git -C "$R" rev-parse HEAD)" ] || fail "$1: the superproject's push did not land"
}
for mode in '{"remote_git_runs_hooks":"operator"}' '{}'; do
  for variant in plain dormant; do
    label="11 $mode $variant"
    submodule_fixture "$mode" "$variant"
    out=$(bureau_git 'git fetch -q origin; echo "fetch=$?"; git push -q origin HEAD; echo "push=$?"')
    case "$out" in *fetch=0*push=0*) ;; *) fail "$label: a remote command failed: $(printf '%s' "$out" | tr '\n' ' ')" ;; esac
    no_recursion "$label"
    if [ "$mode" != '{}' ]; then
      grep -q '^operator-pre-push origin gh=yes .*dir=.*/repo/\.git$' <<< "$(marks)" || fail "$label: the operator's pre-push did not run for the superproject: $(marks | tr '\n' ';')"
    fi
  done
  # A call that asks for recursion in its own arguments is refused with 128 before git runs,
  # under every spelling git accepts: the full option and a unique prefix of it; so are every
  # clone (the new repository's configuration can define hooks no switch reaches) and a git
  # option before the subcommand that git.c does not list (where the subcommand is a guess).
  submodule_fixture "$mode"
  rm -rf "$TMP/clone-rec"
  # One stage shell runs every call (a shell per call costs most of this test's time on macOS);
  # each call reports its exit status and must be refused.
  calls=('git push -q --recurse-submodules=on-demand origin HEAD' 'git push -q --recurse-submodule=on-demand origin HEAD'
         'git push -o -- --recurse-submodules=on-demand origin HEAD'
         'git push --push-option -- --recurse-submodule=on-demand origin HEAD'
         'git fetch -o -- --recurse-submodules origin' 'git pull -X -- --recurse-submodules origin main'
         'git ls-remote --upload-pack -- --recurse-submodules origin'
         'git push --future-option -- --recurse-submodules=on-demand origin HEAD'
         'git fetch -q --recurse-submodules origin' 'git fetch -q --recurse-sub=yes origin'
         'git pull -q --recurse-submodule=yes origin main' 'git submodule -q update --remote'
         "git clone -q '$R.origin' '$TMP/clone-rec'" "git clone -q --recursive '$R.origin' '$TMP/clone-rec'"
         "git clone -q --recurse-submodules=no '$R.origin' '$TMP/clone-rec'" 'git --frobnicate push -q origin HEAD')
  script=""; i=0
  for call in "${calls[@]}"; do script="$script $call 2>&1 | sed -n 's/^bureau git: refused.*/refused-msg/p'; echo \"call$i=\${PIPESTATUS[0]}\";"; i=$((i + 1)); done
  : > "$MARKS"
  out=$(bureau_git "$script")
  i=0
  for call in "${calls[@]}"; do
    grep -qx "call$i=128" <<< "$out" || fail "11 $mode refuse: '$call' was not refused with 128: $(printf '%s' "$out" | tr '\n' ' ')"
    i=$((i + 1))
  done
  [ "$(grep -c '^refused-msg$' <<< "$out")" = "${#calls[@]}" ] || fail "11 $mode refuse: not every refusal said why: $(printf '%s' "$out" | tr '\n' ' ')"
  if grep -q . <<< "$(marks)"; then fail "11 $mode refuse: a hook ran for a refused call: $(marks | tr '\n' ';')"; fi
  if push_recursed || fetch_recursed; then fail "11 $mode refuse: a refused call reached the submodule"; fi
  [ ! -e "$TMP/clone-rec" ] || fail "11 $mode refuse: a refused clone ran"
  # Not refused: --recurse-submodules=no (also abbreviated), an unrelated configuration value that
  # contains the word, an argument after `--` that only looks like the option, a -C path with the
  # word. Recursion asked for through git's own options (-c, --config-env, a file -c include.path
  # names, a -c hidden behind --namespace's value) is not refused but loses: Bureau's options come
  # after the caller's, and the last value of a key wins.
  ln -sfn "$R" "$TMP/recursive-link"
  git -C "$R.origin" config receive.advertisePushOptions true
  printf '[push]\n\trecurseSubmodules = on-demand\n[fetch]\n\trecurseSubmodules = yes\n[submodule]\n\trecurse = true\n[core]\n\thooksPath = %s/.githooks\n' "$R" > "$TMP/flags.cfg"
  : > "$MARKS"
  out=$(cd "$R" && git9 run_case "11 $mode allowed calls" env "${PROBES[@]}" RECVAL=yes BUREAU_CONFIG="$R/.bureau.json" /bin/bash -c 'set -uo pipefail; source "$1/bureau-config.sh"
    git push -q --recurse-submodule=no origin HEAD; echo "push-no=$?"
    git push -q -o neutral-option origin HEAD; echo "push-option=$?"
    git -c user.name=Recurse ls-remote -q origin >/dev/null; echo "user=$?"
    git ls-remote -q -- origin --recurse-submodules >/dev/null 2>&1; echo "dashdash=$?"
    git -C "$2" fetch -q origin; echo "path=$?"
    git -c include.path="$3" fetch -q origin; echo "include-fetch=$?"
    git -c include.path="$3" push -q origin HEAD; echo "include-push=$?"
    git -c submodule.recurse=true fetch -q origin; echo "c=$?"
    git --config-env=fetch.recurseSubmodules=RECVAL fetch -q origin; echo "config-env=$?"
    git --namespace -c -c push.recurseSubmodules=on-demand push -q origin HEAD 2>/dev/null; echo "namespace=$?"' \
    _ "$SCRIPTS" "$TMP/recursive-link" "$TMP/flags.cfg" 2>&1)
  for want in push-no=0 push-option=0 user=0 path=0 include-fetch=0 include-push=0 c=0 config-env=0; do
    case "$out" in *"$want"*) ;; *) fail "11 $mode: expected $want: $(printf '%s' "$out" | tr '\n' ' ')" ;; esac
  done
  case "$out" in *'bureau git: refused'*|*dashdash=128*) fail "11 $mode: a call that does not ask for recursion was refused: $(printf '%s' "$out" | tr '\n' ' ')" ;; esac
  if push_recursed || fetch_recursed; then fail "11 $mode override: a caller option brought recursion back"; fi
  if grep -qvE '^(operator-|$)' <<< "$(marks)"; then fail "11 $mode override: a hook outside the operator's directory ran: $(grep -vE '^(operator-|$)' <<< "$(marks)" | sed -n 1p)"; fi
done
# Optional values do not consume a separate word; a recursion spelling that is
# itself a required option value does. Check without asking git to run an invalid call.
out=$(bureau_git '_bureau_git_asks_recursion push --force-with-lease --recurse-submodules=on-demand; echo "lease=$?"
  _bureau_git_asks_recursion pull --gpg-sign --recurse-submodules=on-demand; echo "sign=$?"
  _bureau_git_asks_recursion push -o --recurse-submodules=on-demand -- origin HEAD; echo "value=$?"')
for want in lease=0 sign=0 value=1; do
  grep -qx "$want" <<< "$out" || fail "11 option values: expected $want: $(printf '%s' "$out" | tr '\n' ' ')"
done
# Negative controls, each red against the previous round: (a) a copy without the recursion
# switches: the default fetch and the configured push recurse in both modes; (b) a copy that lets
# an explicit recursion through: the explicit on-demand push recurses; (c) a copy with the previous
# argument scan (exact option names only): the abbreviated --recurse-submodule=on-demand push
# recurses; (d) a copy that places Bureau's options before the caller's, as before: a -c
# include.path file brings back recursion and the branch's core.hooksPath, and so does a -c hidden
# behind --namespace's value. On git 2.54 and later the submodule's configured hook runs with the
# token in (a), (b) and (c); the fixtures carry the dormant hook.pre-push.command, without which git
# 2.55's event switches keep that hook off in the default mode on their own.
for c in norec norefuse oldscan oldorder; do mkdir -p "$TMP/$c"; cp "$SCRIPTS"/*.sh "$TMP/$c/"; done
sed 's/^\( *\)_bg_hooks+=(-c push.recurseSubmodules=no.*$/\1:/' "$SCRIPTS/bureau-env.sh" > "$TMP/norec/bureau-env.sh"
sed 's/^\( *\)if _bureau_git_asks_recursion .*; then$/\1if false; then/' "$SCRIPTS/bureau-env.sh" > "$TMP/norefuse/bureau-env.sh"
cat >> "$TMP/oldscan/bureau-env.sh" <<'EOF'
_bureau_git_asks_recursion() {
  local a sub="$1"; shift
  [ "$sub" = submodule ] && return 0
  for a in "$@"; do
    case "$a" in --recurse-submodules=no) ;; --recurse-submodules|--recurse-submodules=*) return 0 ;; esac
  done
  return 1
}
EOF
sed 's/^\( *\)git "\${@:1:\$_bg_lead}" \${_bg_hooks\[@\]+"\${_bg_hooks\[@\]}"} "\${@:\$((_bg_lead + 1))}"$/\1git ${_bg_hooks[@]+"${_bg_hooks[@]}"} "$@"/' "$SCRIPTS/bureau-env.sh" > "$TMP/oldorder/bureau-env.sh"
for c in norec norefuse oldorder; do
  cmp -s "$SCRIPTS/bureau-env.sh" "$TMP/$c/bureau-env.sh" && fail "11 control $c: the copy is unchanged (the sed found nothing)"
done
control_hook() {  # <label> — on git 2.54 and later the submodule's configured hook ran with the token
  if [ "$GIT_NEW" = 1 ]; then grep -q '^sub-config-hook .*gh=yes' <<< "$(marks)" || fail "$1: the submodule's configured hook did not run with the token: $(marks | tr '\n' ';')"; fi
}
for mode in '{"remote_git_runs_hooks":"operator"}' '{}'; do
  submodule_fixture "$mode" dormant
  SCRIPTS="$TMP/norec"; bureau_git 'git fetch -q origin; git push -q origin HEAD' >/dev/null; SCRIPTS="$SCRIPTS_REAL"
  fetch_recursed || fail "11 control a $mode: the copy's fetch did not recurse (the fixture proves nothing)"
  push_recursed || fail "11 control a $mode: the copy's push did not recurse (the fixture proves nothing)"
  control_hook "11 control a $mode"
  submodule_fixture "$mode" dormant
  SCRIPTS="$TMP/norefuse"; bureau_git 'git push -q --recurse-submodules=on-demand origin HEAD' >/dev/null; SCRIPTS="$SCRIPTS_REAL"
  push_recursed || fail "11 control b $mode: the copy's explicit on-demand push did not recurse"
  control_hook "11 control b $mode"
done
# (c) and (d) do not depend on the mode: once, in the default.
mode='{}'
submodule_fixture "$mode" dormant
SCRIPTS="$TMP/oldscan"; out=$(bureau_git 'git push -q --recurse-submodule=on-demand origin HEAD'); SCRIPTS="$SCRIPTS_REAL"
case "$out" in *'bureau git: refused'*) fail "11 control c $mode: the previous scan refused the abbreviation" ;; esac
push_recursed || fail "11 control c $mode: the abbreviated on-demand push did not recurse under the previous scan"
control_hook "11 control c $mode"
submodule_fixture "$mode"
SCRIPTS="$TMP/oldorder"; bureau_git "git -c include.path='$TMP/flags.cfg' push -q origin HEAD" >/dev/null; SCRIPTS="$SCRIPTS_REAL"
push_recursed || fail "11 control d $mode: a -c include.path file did not bring recursion back with the previous order"
grep -q '^pre-push ' <<< "$(marks)" || fail "11 control d $mode: the branch's .githooks pre-push did not run with the previous order: $(marks | tr '\n' ';')"
submodule_fixture "$mode"
SCRIPTS="$TMP/oldorder"; bureau_git 'git --namespace -c -c push.recurseSubmodules=on-demand push -q origin HEAD' >/dev/null; SCRIPTS="$SCRIPTS_REAL"
push_recursed || fail "11 control d $mode: --namespace -c -c push.recurseSubmodules=on-demand did not recurse with the previous order"
# git's own options that take a separate value (--attr-source, --shallow-file, git 2.55): the
# value is no subcommand, so the hook and recursion overrides still apply and land after the value.
# --attr-source needs git 2.40 or later (skipped before).
ATTR_OK=0; if git9 git --attr-source HEAD version >/dev/null 2>&1; then ATTR_OK=1; fi
global_round() {  # <label> <repo json> <call> — the call from a stage shell on a fresh fixture
  submodule_fixture "$2"
  # a branch named push in both repositories, so `--attr-source push` names a tree there
  git -C "$R" -c core.hooksPath=/dev/null branch -f push main; git -C "$R.origin" branch -f push main; : > "$MARKS"
  out=$(bureau_git "$3"' ; echo "rc=$?"')
  printf '%s' "$out"
}
for mode in '{"remote_git_runs_hooks":"operator"}' '{}'; do
  if [ "$ATTR_OK" = 1 ]; then
    out=$(global_round "11 $mode attr" "$mode" 'git --attr-source HEAD push -q origin HEAD')
    case "$out" in *rc=0*) ;; *) fail "11 $mode: git --attr-source HEAD push failed: $(printf '%s' "$out" | tr '\n' ' ')" ;; esac
    if push_recursed; then fail "11 $mode: git --attr-source HEAD push recursed into the submodule"; fi
    if grep -qvE '^(operator-|$)' <<< "$(marks)"; then fail "11 $mode: a hook outside the operator's directory ran on git --attr-source HEAD push: $(grep -vE '^(operator-|$)' <<< "$(marks)" | sed -n 1p)"; fi
    out=$(global_round "11 $mode attr2" "$mode" 'git --attr-source push fetch -q origin')
    case "$out" in *rc=0*) ;; *) fail "11 $mode: git --attr-source push fetch failed: $(printf '%s' "$out" | tr '\n' ' ')" ;; esac
    if fetch_recursed; then fail "11 $mode: git --attr-source push fetch recursed into the submodule"; fi
  fi
  out=$(global_round "11 $mode shallow" "$mode" "git --shallow-file '$TMP/no-shallow' fetch -q origin")
  case "$out" in *rc=0*) ;; *) fail "11 $mode: git --shallow-file <file> fetch failed: $(printf '%s' "$out" | tr '\n' ' ')" ;; esac
  if fetch_recursed; then fail "11 $mode: git --shallow-file <file> fetch recursed into the submodule"; fi
done
[ "$ATTR_OK" = 1 ] || echo "SKIP 11 --attr-source: needs git 2.40 or later, found $(git9 git --version 2>/dev/null)"
# true runs an unknown leading option as before: git itself answers it.
submodule_fixture '{"remote_git_runs_hooks":true}'
out=$(bureau_git 'git --frobnicate push -q origin HEAD; echo "rc=$?"')
case "$out" in *'bureau git: refused'*) fail "11 true: an unknown git option was refused under true" ;; esac
# More negative controls: (e) a copy with the previous parse (no --attr-source and --shallow-file,
# any other option skipped) takes the value for the subcommand, leaves the overrides out, and the push and the fetch
# recurse (and the branch's .githooks pre-push runs); (f) a copy with the previous `--` rule
# refuses ls-remote -q -- origin --recurse-submodules; (g) a copy that lets clone through clones,
# and on git 2.54 and later runs the hook a clone template defines next to a dormant
# hook.post-checkout.command, with the token.
mkdir -p "$TMP/oldparse" "$TMP/olddash" "$TMP/noclone"
for c in oldparse olddash noclone; do cp "$SCRIPTS"/*.sh "$TMP/$c/"; done
sed -e 's/|--attr-source|--shallow-file) _bg_skip=1 ;;$/) _bg_skip=1 ;;/' \
    -e 's/^\( *\)-\*) _bg_unknown="\${_bg_unknown:-\$_bg_arg}" ;;$/\1-*) ;;/' "$SCRIPTS/bureau-env.sh" > "$TMP/oldparse/bureau-env.sh"
grep -q '_bg_unknown="\${' "$TMP/oldparse/bureau-env.sh" && fail "11 control oldparse: the unknown-option rule is still in the copy"
sed 's/^\( *\)if \[ "\$_bg_sub" = clone \]; then$/\1if false; then/' "$SCRIPTS/bureau-env.sh" > "$TMP/noclone/bureau-env.sh"
cat >> "$TMP/olddash/bureau-env.sh" <<'EOF2'
_bureau_git_asks_recursion() {
  local sub="$1" a name long prev=""; shift
  [ "$sub" = submodule ] && return 0
  for a in "$@"; do
    if [ "$a" = -- ]; then case "$prev" in -*=*|"") break ;; -*) ;; *) break ;; esac; fi
    prev="$a"
    case "$a" in --rec*) ;; *) continue ;; esac
    name="${a%%=*}"
    for long in --recurse-submodules-default --recursive ""; do
      [ -n "$long" ] || continue 2
      [ "${long#"$name"}" = "$long" ] || break
    done
    [ "$a" = "$name=no" ] && continue
    return 0
  done
  return 1
}
EOF2
for c in oldparse noclone; do
  cmp -s "$SCRIPTS/bureau-env.sh" "$TMP/$c/bureau-env.sh" && fail "11 control $c: the copy is unchanged (the sed found nothing)"
done
mode='{}'
if [ "$ATTR_OK" = 1 ]; then
  SCRIPTS="$TMP/oldparse"; global_round "11 control e" "$mode" 'git --attr-source HEAD push -q origin HEAD' >/dev/null; SCRIPTS="$SCRIPTS_REAL"
  push_recursed || fail "11 control e: with the previous parse git --attr-source HEAD push did not recurse"
  grep -q '^pre-push ' <<< "$(marks)" || fail "11 control e: with the previous parse the branch's pre-push did not run: $(marks | tr '\n' ';')"
  SCRIPTS="$TMP/oldparse"; out=$(global_round "11 control e2" "$mode" 'git --attr-source push fetch -q origin'); SCRIPTS="$SCRIPTS_REAL"
  case "$out" in *rc=0*) fail "11 control e: with the previous parse git --attr-source push fetch did not break" ;; esac
fi
SCRIPTS="$TMP/oldparse"; global_round "11 control e3" "$mode" "git --shallow-file '$TMP/no-shallow' fetch -q origin" >/dev/null; SCRIPTS="$SCRIPTS_REAL"
fetch_recursed || fail "11 control e: with the previous parse git --shallow-file <file> fetch did not recurse"
submodule_fixture "$mode"
SCRIPTS="$TMP/olddash"; out=$(bureau_git 'git ls-remote -q -- origin --recurse-submodules >/dev/null; echo "rc=$?"'); SCRIPTS="$SCRIPTS_REAL"
case "$out" in *'bureau git: refused'*rc=128*) ;; *) fail "11 control f: the previous rule did not refuse ls-remote -q -- origin --recurse-submodules: $(printf '%s' "$out" | tr '\n' ' ')" ;; esac
rm -rf "$TMP/tmpl11" "$TMP/clone11"; mkdir -p "$TMP/tmpl11"
probe_script "$TMP/tmpl11-probe.sh" clone-template-hook
printf '[hook "late"]\n\tcommand = %s late\n\tevent = post-checkout\n[hook "post-checkout"]\n\tcommand = true\n' "$TMP/tmpl11-probe.sh" > "$TMP/tmpl11/config"
: > "$MARKS"
out=$(bureau_git "git -c init.templateDir='$TMP/tmpl11' clone -q '$R.origin' '$TMP/clone11'; echo \"rc=\$?\"")
case "$out" in *"bureau git: refused 'git clone'"*rc=128*) ;; *) fail "11: Bureau's clone was not refused: $(printf '%s' "$out" | tr '\n' ' ')" ;; esac
[ ! -e "$TMP/clone11" ] || fail "11: a refused clone created its directory"
SCRIPTS="$TMP/noclone"; bureau_git "git -c init.templateDir='$TMP/tmpl11' clone -q '$R.origin' '$TMP/clone11'" >/dev/null; SCRIPTS="$SCRIPTS_REAL"
[ -d "$TMP/clone11/.git" ] || fail "11 control g: the copy without the clone refusal did not clone"
if [ "$GIT_NEW" = 1 ]; then grep -q '^clone-template-hook late gh=yes' <<< "$(marks)" || fail "11 control g: the clone template's hook did not run with the token: $(marks | tr '\n' ';')"; fi
if [ "$GIT_NEW" = 0 ]; then
  echo "SKIP 11 the submodule's configured hook (plain and dormant): needs git 2.54 or later, found $(git9 git --version 2>/dev/null); recursion itself is detected on any git; set BUREAU_TEST_GIT_DIR to the bin directory of one"
fi

# An include inside the working tree and the configuration environment, each setting
# core.hooksPath to the branch's .githooks (and, for git 2.54 and later, a configured pre-push).
# include_round <label> <what> — what: include, includeif, count, parameters.
include_round() {
  repo "$R"; config '{"remote_git_runs_hooks":"operator"}'; operator_hooks "$R/.git/hooks"
  probe_script "$R/inc-probe.sh" inc-config-hook
  printf '[core]\n\thooksPath = .githooks\n[hook "inc"]\n\tcommand = %s/inc-probe.sh pre-push\n\tevent = pre-push\n' "$R" > "$R/tracked.cfg"
  git -C "$R" config --unset core.hooksPath
  local -a extra=()
  case "$2" in
    include) git -C "$R" config include.path ../tracked.cfg ;;
    includeif) git -C "$R" config 'includeIf.onbranch:**.path' ../tracked.cfg ;;
    count) extra=(GIT_CONFIG_COUNT=2 GIT_CONFIG_KEY_0=core.hooksPath GIT_CONFIG_VALUE_0="$R/.githooks"
                  GIT_CONFIG_KEY_1=hook.inc.command GIT_CONFIG_VALUE_1="$R/inc-probe.sh pre-push") ;;
    parameters) extra=(GIT_CONFIG_PARAMETERS="'core.hooksPath'='$R/.githooks' 'hook.inc.command'='$R/inc-probe.sh pre-push' 'hook.inc.event'='pre-push'") ;;
  esac
  git -C "$R" add -A; git -C "$R" -c core.hooksPath=/dev/null commit -q -m tracked
  upstream_commit; : > "$MARKS"
  out=$(cd "$R" && git9 run_case "$1 ($SCRIPTS)" env "${PROBES[@]}" ${extra[@]+"${extra[@]}"} BUREAU_CONFIG="$R/.bureau.json" /bin/bash -c 'set -uo pipefail; source "$1/bureau-config.sh"
    git push -q origin HEAD:refs/heads/inc; echo "push=$?"; git fetch -q origin; echo "fetch=$?"' _ "$SCRIPTS" 2>&1)
  case "$out" in *push=0*fetch=0*) ;; *) fail "$1: a remote command failed: $(printf '%s' "$out" | tr '\n' ' ')" ;; esac
}
for what in include includeif count parameters; do
  include_round "11 $what" "$what"
  operator_only "11 $what"
  if grep -q '^inc-config-hook' <<< "$(marks)"; then fail "11 $what: the configured hook from the $what ran"; fi
  # Without Bureau (a plain git push with the same configuration) the branch's .githooks run:
  # the fixture does redirect core.hooksPath.
  : > "$MARKS"
  case "$what" in
    count) (cd "$R" && env GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=core.hooksPath GIT_CONFIG_VALUE_0="$R/.githooks" git push -q origin HEAD:refs/heads/plain) ;;
    parameters) (cd "$R" && env GIT_CONFIG_PARAMETERS="'core.hooksPath'='$R/.githooks'" git push -q origin HEAD:refs/heads/plain) ;;
    *) git -C "$R" push -q origin HEAD:refs/heads/plain ;;
  esac
  grep -q '^pre-push ' <<< "$(marks)" || fail "11 $what: the fixture does not redirect core.hooksPath (a plain push ran no .githooks hook): $(marks | tr '\n' ';')"
  # Negative control: a copy whose "operator" leaves core.hooksPath as configured (every
  # configured hook, as true): the branch's hooks run and operator_only fails.
  SCRIPTS="$TMP/before"; include_round "11 $what control" "$what"; SCRIPTS="$SCRIPTS_REAL"
  control_fails=$PR1_FAILS
  operator_only "11 $what control" 2>/dev/null
  if [ "$PR1_FAILS" = "$control_fails" ]; then fail "11 $what control: operator_only passed against a copy that keeps the configured core.hooksPath"; else PR1_FAILS=$control_fails; fi
done
pr1_pass "11 off and \"operator\": no recursion into submodules (configured, asked for under any spelling and refused, or set through git's own options and overridden); includes and GIT_CONFIG_COUNT/GIT_CONFIG_PARAMETERS cannot redirect core.hooksPath"

if [ "$PR1_FAILS" != 0 ]; then echo "$PR1_FAILS check(s) failed" >&2; exit 1; fi
echo "OK test_remote_git_hooks"
