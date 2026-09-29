#!/bin/bash
# One rule for the merge gate's numbers, applied the same way by the gate and the doctor.
#
# agents.merge_min_required_checks and agents.merge_ci_start_grace_seconds are read by
# _merge_gate_number (bureau-config.sh, used by pr_ci_is_green and merge-pipeline.sh) and
# by gate_number (bureau-doctor.py). Each value of the table below goes through both, cut
# and imported from the shipped files: both must give the expected number and agree on
# whether to warn. Before, the gate read a string or a negative number differently from
# the doctor, and v3.0.2's gate let "2" and -1 through with fewer checks than written.
# The end-to-end cases (the merge stage with these values) are in
# tests/test_merge_gate_switches.sh.
set -euo pipefail
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
T=$(mktemp -d -t bureau-test.gatenumber.XXXXXXXX)
trap 'rm -rf "$T"' EXIT
fail() { echo "FAIL: $*" >&2; exit 1; }

sed -n '/^_merge_gate_number() {/,/^}/p' "$REPO_ROOT/templates/scripts/bureau-config.sh" > "$T/gate.sh"
grep -q '^_merge_gate_number() {' "$T/gate.sh" || fail '_merge_gate_number not found in bureau-config.sh'

# JSON value (or "absent") : number used : warn|ok. A string counts when, without ASCII
# blanks around it and one leading "+", it is digits with an optional fraction and exponent.
TABLE='absent:1:ok
null:1:ok
0:0:ok
2:2:ok
2.0:2:ok
-0.0:0:ok
9999999:9999999:ok
"2":2:warn
"007":7:warn
"99999999999999999999":9999999:warn
" 2":2:warn
"2 ":2:warn
"+2":2:warn
"\t2\n":2:warn
"\u000b2":2:warn
"b2":1:warn
"20b":1:warn
"2.0":2:warn
"1e3":1000:warn
"1E3":1000:warn
"1e-3":1:warn
"0.0":0:warn
"2.5":3:warn
"1e400":9999999:warn
"+ 2":1:warn
"++2":1:warn
"-2":1:warn
".5":1:warn
"5.":1:warn
"0x10":1:warn
"Infinity":1:warn
"two":1:warn
"-1":1:warn
"abc":1:warn
"":1:warn
1.5:2:warn
0.2:1:warn
-1:1:warn
-0.5:1:warn
10000000:9999999:warn
12345678901234567890:9999999:warn
1e20:9999999:warn
true:1:warn
false:1:warn
[]:1:warn
{}:1:warn'

shell_rule() {  # <config file> — "<number> <ok|warn>" from the gate's helper
  BUREAU_CONFIG="$1" /bin/bash -c '
    bureau_get() { jq -r "$1" "$BUREAU_CONFIG"; }
    source "$2"
    if v=$(_merge_gate_number merge_min_required_checks 1); then echo "$v ok"; else echo "$v warn"; fi' _ "$1" "$T/gate.sh"
}
doctor_rule() {  # <config file> — "<number> <ok|warn>" from the doctor's gate_number
  python3 - "$REPO_ROOT/templates/scripts/bureau-doctor.py" "$1" <<'PY'
import importlib.util, json, sys
spec = importlib.util.spec_from_file_location('doctor', sys.argv[1]); d = importlib.util.module_from_spec(spec); spec.loader.exec_module(d)
warnings = []
used = d.gate_number(json.load(open(sys.argv[2]))['agents'], 'merge_min_required_checks', 1, warnings)
print(used, 'warn' if warnings else 'ok')
PY
}

n=0
while IFS= read -r row; do
  [ -n "$row" ] || continue
  value=${row%%:*}; rest=${row#*:}; want="${rest%%:*} ${rest#*:}"
  if [ "$value" = absent ]; then echo '{"agents":{}}' > "$T/c.json"
  else printf '{"agents":{"merge_min_required_checks":%s}}' "$value" > "$T/c.json"; fi
  got_shell=$(shell_rule "$T/c.json"); got_doctor=$(doctor_rule "$T/c.json")
  [ "$got_shell" = "$want" ] || fail "gate: $value gave '$got_shell', wanted '$want'"
  [ "$got_doctor" = "$want" ] || fail "doctor: $value gave '$got_doctor', wanted '$want'"
  n=$((n + 1))
done <<< "$TABLE"
echo "PASS the gate and the doctor read $n values identically"

# The doctor's warning names the number the gate uses, and its CI-gate warning asks for it.
printf '{"agents":{"merge_min_required_checks":"2"}}' > "$T/c.json"
python3 - "$REPO_ROOT/templates/scripts/bureau-doctor.py" "$T/c.json" <<'PY' || fail 'the doctor warning does not name the number'
import importlib.util, json, sys
spec = importlib.util.spec_from_file_location('doctor', sys.argv[1]); d = importlib.util.module_from_spec(spec); spec.loader.exec_module(d)
warnings = []
d.gate_number(json.load(open(sys.argv[2]))['agents'], 'merge_min_required_checks', 1, warnings)
assert warnings == ['agents.merge_min_required_checks "2" should be a whole number of at least 0; the merge gate uses 2'], warnings
PY
echo 'PASS the doctor names the number the gate uses'

# Negative control: v3.0.2's gate read (`// 1`, then `[ completed -lt minimum ]`) lets -1
# pass a head without any check.
printf '{"agents":{"merge_min_required_checks":-1}}' > "$T/c.json"
old=$(jq -r '.agents.merge_min_required_checks // 1' "$T/c.json")
[ 0 -lt "$old" ] && fail 'negative control: the v3.0.2 read should let -1 need no check'
[ "$(shell_rule "$T/c.json")" = '1 warn' ] || fail 'negative control: the rule should turn -1 into 1'
echo 'PASS negative control: the v3.0.2 read lets -1 need no check; the rule needs 1'

echo 'OK test_gate_number_rule'
