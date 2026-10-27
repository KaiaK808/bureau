#!/bin/bash
# The concurrency cap counts work: leaf issues of the configured projects, not epics, not
# parked tickets, not other projects.
#
# Runs the REAL count_in_flight_issues from templates/scripts/bureau-config.sh under /bin/bash
# against a stubbed `curl` that answers with a fixed set of issues and records the query.
# The negative control runs the old count (`parent: { null: true }`, no children rule) over
# the same answer: it counts the epic.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")" && cd .. && pwd)"
SCRIPTS="$REPO_ROOT/templates/scripts"
SB=$(mktemp -d -t bureau-test.inflight.XXXXXXXX)
trap 'rm -rf "$SB"' EXIT
fail() { echo "FAIL $*" >&2; exit 1; }

write_config() {  # $1 = projects JSON array
  cat > "$SB/.bureau.json" <<EOF
{
  "linear": {
    "teams": [{
      "id": "team-id", "key": "EXP", "name": "Test",
      "states": {
        "triage": "s1", "spec": "s2", "spec_review": "s3", "design": "s4",
        "build": "s5", "build_review": "s6", "done": "s7", "merge": "s8"
      }
    }],
    "labels": {
      "lane2":            { "id": "l1", "name": "lane-2" },
      "needs_human":      { "id": "l2", "name": "needs-human" },
      "needs_ux":         { "id": "l3", "name": "needs-ux" },
      "ai_implementable": { "id": "l4", "name": "ai-implementable" }
    },
    "projects": $1
  },
  "agents": { "poll_interval_minutes": 30, "max_review_cycles": 3 },
  "repo": { "branch_prefix": "feat", "specs_dir": "specs" }
}
EOF
}

# Five issues: three plain leaves (one of them a sub-issue), one parked and one epic. An answer
# that lacks the children list (or any other list the count reads) is unusable and ends with
# 27 since v3.1: tests/test_linear_lists.sh.
cat > "$SB/answer.json" <<'EOF'
{"data": {"issues": {"nodes": [{"labels": {"nodes": [{"name": "lane-2"}]}, "children": {"nodes": []}, "identifier": "EXP-1", "state": {"name": "Build"}}, {"labels": {"nodes": []}, "children": {"nodes": []}, "parent": {"id": "EPIC"}, "identifier": "EXP-2", "state": {"name": "Build"}}, {"labels": {"nodes": [{"name": "needs-human"}]}, "children": {"nodes": []}, "identifier": "EXP-3", "state": {"name": "Build"}}, {"labels": {"nodes": []}, "children": {"nodes": [{"id": "CHILD"}]}, "identifier": "EXP-4", "state": {"name": "Build"}}, {"labels": {"nodes": []}, "children": {"nodes": []}, "identifier": "EXP-5", "state": {"name": "Build"}}]}}}
EOF
mkdir -p "$SB/bin"
cat > "$SB/bin/curl" <<EOF
#!/bin/bash
prev=""; for a in "\$@"; do [ "\$prev" = "-d" ] && printf '%s' "\$a" | jq -r .query > "$SB/query"; prev="\$a"; done
cat "$SB/answer.json"
EOF
chmod +x "$SB/bin/curl"

count() {
  (cd "$SB" && PATH="$SB/bin:$PATH" LINEAR_API_KEY=k /bin/bash -c "
    set -euo pipefail
    source '$SCRIPTS/bureau-config.sh'
    ${1:-count_in_flight_issues}")
}

write_config '[]'
[ "$(count)" = 3 ] || fail "counted $(count), wanted 3 (the three leaves; not the parked one, not the epic)"
q=$(cat "$SB/query")
case "$q" in *"parent: { null: true }"*) fail "the query still drops sub-issues" ;; esac
case "$q" in *"project:"*) fail "a project clause without configured projects" ;; esac
case "$q" in *description*) fail "the admission query requests descriptions" ;; esac
case "$q" in *"identifier state { name }"*) ;; *) fail "the query lacks blocker identifiers/states" ;; esac
case "$q" in *"children(first: 1)"*) ;; *) fail "the query does not ask for children" ;; esac
echo "PASS the cap counts leaf issues, sub-issues included, and neither epics nor parked tickets"

write_config '["proj-a","proj-b"]'
count >/dev/null
case "$(cat "$SB/query")" in *'project: { id: { in: ["proj-a","proj-b"] } }'*) ;; *) fail "the configured projects do not reach the query: $(cat "$SB/query")" ;; esac
echo "PASS the configured projects scope the count, as in pick_issue"

# Negative control: the rule this replaces, over the same answer.
old=$(jq '[(.data.issues.nodes // [])[] | select(([(.labels.nodes // [])[].name] | map(select(. == "needs-human" or . == "blocked" or . == "wip")) | length) == 0)] | length' "$SB/answer.json")
[ "$old" = 4 ] || fail "negative control: the old rule no longer counts the epic, so this test proves nothing"
echo "PASS negative control: the old rule counts the epic as work (4)"

# Match the real Linear state filter instead of returning Merge unconditionally.
cat > "$SB/bin/curl" <<'PY'
#!/usr/bin/env python3
import json, re, sys
query = json.loads(sys.argv[sys.argv.index('-d') + 1])['query']
ids = re.search(r'state: \{ id: \{ in: (\[[^]]*\])', query).group(1)
nodes = [dict(identifier=i, state={'name': name}, labels={'nodes': []}, children={'nodes': []})
         for sid, i, name in [('s6', 'EXP-1638', 'Build Review'), ('s8', 'EXP-1621', 'Merge')]
         if sid in json.loads(ids)]
print(json.dumps({'data': {'issues': {'nodes': nodes}}}))
PY
chmod +x "$SB/bin/curl"
write_config '[]'
cp "$SB/.bureau.json" "$SB/base.json"
checks=0
for mode in manual auto unknown; do
  for value in absent null true false '"false"' 0 '[]' '{}'; do
    jq --arg mode "$mode" --arg val "$value" '.agents.merge_mode=$mode | if $val == "absent" then . else .agents.max_concurrent_include_merge=($val|fromjson) end' "$SB/base.json" > "$SB/.bureau.json"
    expected=2
    [ "$mode" = auto ] || expected=1
    case "$value" in true) expected=2 ;; false) expected=1 ;; esac
    result=$(count spec_admission_snapshot 2>"$SB/warning")
    [ "$(printf '%s' "$result" | jq .count)" = "$expected" ] || fail "$mode/$value: $result"
    printf '%s' "$result" | jq -e '.issues | any(.identifier == "EXP-1638" and .state == "Build Review")' >/dev/null || fail 'Build Review was excluded'
    case "$value" in absent|null|true|false) ! grep -q max_concurrent_include_merge "$SB/warning" || fail 'valid value warned' ;;
      *) grep -q max_concurrent_include_merge "$SB/warning" || fail 'invalid value did not warn' ;; esac
    for cap in 1 2; do
      set +e
      out=$(count "BUREAU_MAX_CONCURRENT_ISSUES=$cap; spec_admission_gate" 2>/dev/null); rc=$?
      set -e
      if [ "$expected" -ge "$cap" ]; then
        [ "$rc" = 28 ] || fail "$mode/$value/cap=$cap: rc=$rc"
        case "$out" in *'EXP-1638 — Build Review'*) ;; *) fail "missing blocker: $out" ;; esac
        if [ "$expected" = 2 ]; then case "$out" in *'EXP-1621 — Merge'*) ;; *) fail 'missing Merge blocker' ;; esac; fi
      else [ "$rc" = 0 ] && [ -z "$out" ] || fail 'free slot refused'; fi
      checks=$((checks + 1))
    done
  done
done
echo "PASS $checks cap decisions: manual/auto/override/invalid, caps 1/2, named blockers, Build Review retained"
