#!/bin/bash
# An environment entry whose name is no shell identifier runs nothing in the secret filter.
#
# bash 3.2 (macOS /bin/bash) takes an environment entry such as `A[$(command)]=x` for a variable
# and lists the name in `compgen -e`. _bureau_env_build walked that list unquoted and expanded
# every name indirectly to see whether its value carries a secret: the subscript ran as a command
# in the Bureau shell, which holds the unexported .env keys, and a name with glob characters went
# through pathname expansion. Anything that can put an entry into the environment a Bureau script
# starts from (a tmux server's environment, a wrapper) could run code there and print a key.
#
#   A  bureau_without_secrets, bureau_untrusted_env and bureau_exec_runtime with such an entry in
#      the environment: the command in the subscript does not run (no sentinel file, no key on
#      stdout or stderr) and the wrapped command still runs
#   B  the filter still does its work beside such an entry: a well-named variable whose value
#      carries the key is removed from the child's environment, an innocent one stays
#   C  a name with glob characters is not expanded against the working directory
#   D  the caller's noglob setting and IFS are as before, with noglob on and off
#   E  the name check does not depend on the locale: under single-byte locales whose collation
#      puts `[`, `$` and `(` inside the range A-Z, a check written with ranges lets the entry
#      through; A is repeated under every such locale this machine has
#   F  the filter adds no local variable that hides an exported one: exported variables named
#      like the filter's own helpers and carrying the key are still removed
# Negative control: against a249a95, A fails on all three functions ("the subscript's command
# ran") and C fails ("a file name from the working directory reached the filter"). Against a
# check written as the regular expression ^[A-Za-z_][A-Za-z0-9_]*$ held in two more locals, E
# fails under kk_KZ.PT154 on macOS and F fails for both names.
# Where the running bash does not import such names at all, A and C hold trivially and say so.
set -uo pipefail
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SCRIPTS="$REPO_ROOT/templates/scripts"
TMP=$(mktemp -d -t bureau-test.malformed-names.XXXXXXXX)
trap 'rm -rf "$TMP"' EXIT
FAILS=0
ok() { echo "PASS $1"; }
bad() { echo "FAIL $1" >&2; FAILS=$((FAILS + 1)); }

KEY='lin_api_fake_0123456789'
SENTINEL="$TMP/ran"
# The entry's name: a subscript that creates the sentinel and prints the key.
EVIL_NAME='BUREAU_PROBE[$(printf${IFS}%s${IFS}"LEAK:$LINEAR_API_KEY">&2;:>"$SENTINEL")]'

# A second entry made only of characters that such a locale's A-Z range covers besides letters.
EVIL_NAME2='BUREAU_PROBE2[$(echo${IFS}LEAK2_$LINEAR_API_KEY>&2)]'

# run <function and arguments as shell code> — a fresh /bin/bash started with the entry in its
# environment, the real bureau-env.sh sourced, the key set as an unexported shell variable.
run() {
  rm -f "$SENTINEL"
  ( cd "$TMP/cwd" && /usr/bin/env -i PATH=/usr/bin:/bin HOME="$TMP" SENTINEL="$SENTINEL" \
      ${RUN_LOCALE:+LC_ALL="$RUN_LOCALE"} \
      CARRIER="prefix-$KEY-suffix" INNOCENT=stays "$EVIL_NAME=x" "$EVIL_NAME2=x" 'BUREAU_GLOB*=x' \
      _beb_noglob="prefix-$KEY-suffix" _beb_name_re="prefix-$KEY-suffix" \
      /bin/bash -c 'source "$1/bureau-env.sh"; LINEAR_API_KEY="$2"; '"$1" _ "$SCRIPTS" "$KEY" ) \
    >"$TMP/out" 2>"$TMP/err"
  echo "$?" >"$TMP/rc"
}
RUN_LOCALE=""
mkdir -p "$TMP/cwd"
# A file the glob name would match if it were expanded, named like a variable that carries the key.
: >"$TMP/cwd/BUREAU_GLOBCARRIER"

imports=$(/usr/bin/env -i PATH=/usr/bin:/bin "$EVIL_NAME=x" /bin/bash -c 'compgen -e' | grep -c 'BUREAU_PROBE' || true)
if [ "$imports" = 0 ]; then echo "NOTE this bash does not import an entry with such a name; A and C hold trivially here"; fi

# ── A  nothing in the subscript runs ───────────────────────────────────────────
for call in 'bureau_without_secrets /usr/bin/printf "ran-%s\n" child' \
            'bureau_untrusted_env /usr/bin/printf "ran-%s\n" child' \
            'bureau_exec_runtime /usr/bin/printf "ran-%s\n" child'; do
  fn=${call%% *}
  run "$call"
  if [ -e "$SENTINEL" ]; then bad "A $fn: the subscript's command ran"; else ok "A $fn: the subscript's command did not run"; fi
  if grep -q -F "$KEY" "$TMP/out" "$TMP/err"; then bad "A $fn: the key was printed"; else ok "A $fn: the key was not printed"; fi
  if grep -q -x 'ran-child' "$TMP/out" && [ "$(cat "$TMP/rc")" = 0 ]; then ok "A $fn: the wrapped command ran"
  else bad "A $fn: the wrapped command did not run (rc $(cat "$TMP/rc"): $(head -c 300 "$TMP/err"))"; fi
done

# ── B  the filter still removes a carrier ──────────────────────────────────────
run 'bureau_without_secrets /usr/bin/env'
if grep -q '^CARRIER=' "$TMP/out"; then bad "B a variable carrying the key reached the child"; else ok "B a variable carrying the key is removed"; fi
if grep -q -x 'INNOCENT=stays' "$TMP/out"; then ok "B an innocent variable stays"; else bad "B an innocent variable was removed"; fi
if grep -q -F "$KEY" "$TMP/out"; then bad "B the key reached the child's environment"; else ok "B the key is not in the child's environment"; fi

# ── C  no pathname expansion of a name ─────────────────────────────────────────
# With expansion, the name BUREAU_GLOB* becomes BUREAU_GLOBCARRIER, a name the test defines as a
# shell variable carrying the key: the filter would then try to unset it for the child.
run 'BUREAU_GLOBCARRIER="x-$LINEAR_API_KEY-x"; _bureau_env_build default seven 1; printf "%s\n" "${_BUREAU_ENV_ARGV[@]}"'
if grep -q -x 'BUREAU_GLOBCARRIER' "$TMP/out"; then bad "C a file name from the working directory reached the filter"
else ok "C a name with glob characters is not expanded against the working directory"; fi

# ── D  noglob and IFS as before ────────────────────────────────────────────────
run 'set +f; IFS=":"; bureau_without_secrets /usr/bin/true; case $- in (*f*) echo noglob-on ;; (*) echo noglob-off ;; esac; printf "ifs=[%s]\n" "$IFS"'
if grep -q -x 'noglob-off' "$TMP/out" && grep -q -x 'ifs=\[:\]' "$TMP/out"; then ok "D noglob off and IFS are kept"; else bad "D noglob off or IFS changed: $(tr '\n' ' ' <"$TMP/out")"; fi
run 'set -f; bureau_without_secrets /usr/bin/true; case $- in (*f*) echo noglob-on ;; (*) echo noglob-off ;; esac'
if grep -q -x 'noglob-on' "$TMP/out"; then ok "D noglob on is kept"; else bad "D noglob on was switched off"; fi

# ── E  the check does not follow the locale ────────────────────────────────────
# One locale per single-byte character set this machine has.
E_LOCALES=""
for E_SET in PT154 KOI8-R CP1251 CP866 ISO8859-1 ISO8859-5 ISO8859-15; do
  E_ONE=$(locale -a 2>/dev/null | grep -F -- ".$E_SET" | grep -E -- "\.$E_SET\$" | sort | sed -n 1p)
  [ -z "$E_ONE" ] || E_LOCALES="$E_LOCALES $E_ONE"
done
E_SEEN=0; E_FAILS_BEFORE=$FAILS
for RUN_LOCALE in C $E_LOCALES; do
  E_SEEN=$((E_SEEN + 1))
  run 'bureau_without_secrets /usr/bin/printf "ran-%s\n" child'
  if [ -e "$SENTINEL" ] || grep -q -F "$KEY" "$TMP/out" "$TMP/err"; then bad "E under LC_ALL=$RUN_LOCALE the subscript's command ran or the key was printed"; fi
  grep -q -x 'ran-child' "$TMP/out" || bad "E under LC_ALL=$RUN_LOCALE the wrapped command did not run"
done
RUN_LOCALE=""
E_FAILS_AFTER=$FAILS
[ "$E_FAILS_AFTER" != "$E_FAILS_BEFORE" ] || ok "E the entries run nothing under $E_SEEN locale(s): C$E_LOCALES"

# ── F  no local of the filter hides an exported variable ───────────────────────
run 'bureau_without_secrets /usr/bin/env'
for name in _beb_noglob _beb_name_re; do
  if grep -q "^$name=" "$TMP/out"; then bad "F the exported variable $name carrying the key reached the child"; else ok "F the exported variable $name carrying the key is removed"; fi
done

if [ "$FAILS" != 0 ]; then echo "$FAILS check(s) failed" >&2; exit 1; fi
echo "OK test_env_malformed_names"
