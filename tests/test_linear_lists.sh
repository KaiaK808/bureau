#!/bin/bash
# The picker and the in-flight count stop with 27 on a Linear answer that lacks a list they
# read, instead of reading it as `[]` (v3.1).
#
# Runs the REAL pick_issue, pipeline_pick_next and count_in_flight_issues from
# templates/scripts/bureau-config.sh under /bin/bash against a `curl` double that answers
# every call with one fixed body (the fetch and its shape check run for real). A body without
# the issue list, or a node without its labels, blockers (pick) or children (count), is
# unusable like any other broken answer: retried, then 27 — read as `[]` it let the picker take
# a needs-human ticket or a blocked one and the count report 0 in flight while the answer was
# broken. Well-formed answers give the same result as before. The negative control puts the
# v3.0.2 reads back into the current functions (no shape, `// []`; CI checks out without
# history): it picks the needs-human ticket and counts 0.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")" && cd .. && pwd)"
SCRIPTS="$REPO_ROOT/templates/scripts"
SB=$(mktemp -d -t bureau-test.lists.XXXXXXXX)
trap 'rm -rf "$SB"' EXIT
unset BUREAU_CONFIG BUREAU_DRY_RUN 2>/dev/null || true
fail() { echo "FAIL $*" >&2; printf '  | rc=%s out=%s calls=%s waits=%s\n  | err=%s\n' "${RC:-}" "${OUT:-}" "${CALLS:-}" "${WAITS:-}" "${ERR:-}" >&2; exit 1; }

git -C "$SB" init -q
cat > "$SB/.bureau.json" <<'EOF'
{
  "linear": {
    "teams": [{"id": "t", "key": "EXP", "name": "T",
      "states": {"triage": "s1", "spec": "s2", "spec_review": "s3", "design": "s4",
                 "build": "s5", "build_review": "s6", "done": "s7"}}],
    "labels": {"lane2": {"id": "l1", "name": "lane-2"}, "needs_human": {"id": "l2", "name": "needs-human"},
               "needs_ux": {"id": "l3", "name": "needs-ux"}, "ai_implementable": {"id": "l4", "name": "ai-implementable"}},
    "projects": []
  },
  "agents": {"poll_interval_minutes": 30, "max_review_cycles": 3},
  "repo": {"branch_prefix": "feat", "specs_dir": "specs"}
}
EOF
mkdir -p "$SB/bin"
cat > "$SB/bin/curl" <<EOF
#!/bin/bash
echo call >> "$SB/calls.log"
cat "$SB/body"
"$REPO_ROOT/tests/lib/curl-writeout.sh" 200 "\$@"
EOF
printf '#!/bin/bash\nprintf "%%s\\n" "$1" >> "%s/waits.log"\n' "$SB" > "$SB/bin/sleep"
chmod +x "$SB/bin/curl" "$SB/bin/sleep"

# call <body> <snippet> [<config>] — sets RC, OUT, ERR, CALLS (fetches) and WAITS.
call() {
  printf '%s' "$1" > "$SB/body"; rm -f "$SB/calls.log" "$SB/waits.log"
  set +e
  OUT=$(cd "$SB" && PATH="$SB/bin:$PATH" LINEAR_API_KEY=k BUREAU_LINEAR_RETRIES="${RETRIES:-0}" /bin/bash -c "
    set -uo pipefail
    source '${3:-$SCRIPTS/bureau-config.sh}'
    $2" 2>"$SB/err")
  RC=$?
  set -e
  ERR=$(cat "$SB/err")
  CALLS=0; [ ! -f "$SB/calls.log" ] || CALLS=$(wc -l < "$SB/calls.log" | tr -d ' ')
  WAITS=""; [ ! -f "$SB/waits.log" ] || WAITS=$(tr '\n' ' ' < "$SB/waits.log" | sed 's/ $//')
}

# Pick answers: EXP-4 (older, urgent) carries needs-human, EXP-5 is free.
N4='"identifier":"EXP-4","priority":1,"createdAt":"2026-01-01"'
N5='"identifier":"EXP-5","priority":2,"createdAt":"2026-01-02"'
L_HUMAN='"labels":{"nodes":[{"name":"lane-2"},{"name":"needs-human"}]}'
L_FREE='"labels":{"nodes":[{"name":"lane-2"}]}'
NOREL='"inverseRelations":{"nodes":[]}'
OPEN_BLOCKER='"inverseRelations":{"nodes":[{"type":"blocks","issue":{"identifier":"EXP-9","state":{"type":"started"}}}]}'
# Whole nodes (assigned, so no brace expansion touches them).
H4="{$N4,$L_HUMAN,$NOREL}"; F5="{$N5,$L_FREE,$NOREL}"; B4="{$N4,$L_FREE,$OPEN_BLOCKER}"
NOLABELS4="{$N4,$NOREL}"; LNULL4="{$N4,\"labels\":null,$NOREL}"; LEMPTY4="{$N4,\"labels\":{},$NOREL}"; NOREL4="{$N4,$L_FREE}"
RELEMPTY4="{$N4,$L_FREE,\"inverseRelations\":{}}"
pick_body() { printf '{"data":{"issues":{"nodes":[%s]}}}' "$1"; }
PICK='pick_issue s5 lane-2 needs-human'

# --- well-formed answers pick as before -------------------------------------------------------
call "$(pick_body "$H4,$F5")" "$PICK"
[ "$RC" = 0 ] && [ "$OUT" = EXP-5 ] || fail "well-formed: the free ticket is not picked"
call "$(pick_body "$B4,$F5")" "$PICK"
[ "$RC" = 0 ] && [ "$OUT" = EXP-5 ] || fail "well-formed: a blocked ticket was picked"
call '{"data":{"issues":{"nodes":[]}}}' "$PICK"
[ "$RC" = 0 ] && [ -z "$OUT" ] || fail "an empty queue is no longer an empty answer with 0"
call "$(pick_body "$H4,$F5")" 'pipeline_pick_next implement-pipeline.sh'
[ "$RC" = 0 ] && [ "$OUT" = EXP-5 ] || fail "well-formed: pipeline_pick_next does not pick the free ticket"
echo "PASS well-formed answers pick as before: needs-human and open blockers stay out, an empty queue is empty"

# --- answers that lack a list: 27, nothing picked ---------------------------------------------
for case_ in "no issue list|{\"data\":{\"issues\":{}}}" \
             "issue list null|{\"data\":{\"issues\":{\"nodes\":null}}}" \
             "node without labels|$(pick_body "$NOLABELS4,$F5")" \
             "labels null|$(pick_body "$LNULL4,$F5")" \
             "labels without nodes|$(pick_body "$LEMPTY4,$F5")" \
             "node without blockers|$(pick_body "$NOREL4,$F5")" \
             "blockers without nodes|$(pick_body "$RELEMPTY4,$F5")"; do
  call "${case_#*|}" "$PICK"
  [ "$RC" = 27 ] || fail "pick, ${case_%%|*}: exit $RC, wanted 27"
  [ -z "$OUT" ] || fail "pick, ${case_%%|*}: picked '$OUT' from a broken answer"
  case "$ERR" in *"unusable answer (no-data) after 1 attempt(s)"*) ;; *) fail "pick, ${case_%%|*}: not reported as an unusable answer" ;; esac
done
call "$(pick_body "$NOLABELS4,$F5")" 'pipeline_pick_next implement-pipeline.sh'
[ "$RC" = 27 ] && [ -z "$OUT" ] || fail "pipeline_pick_next does not hand the 27 on"
# Retried like any unusable answer.
RETRIES=1 call "$(pick_body "$NOLABELS4,$F5")" "$PICK"
[ "$RC" = 27 ] && [ "$CALLS" = 2 ] && [ "$WAITS" = 10 ] || fail "pick: a missing list is not retried once after 10 s"
echo "PASS the picker stops with 27 when the answer lacks the issue list, a node's labels or its blockers; retried first"

# --- the in-flight count ---------------------------------------------------------------------
CNT='count_in_flight_issues'
LEAF='"labels":{"nodes":[]},"children":{"nodes":[]}'
PARKED='"labels":{"nodes":[{"name":"needs-human"}]},"children":{"nodes":[]}'
EPIC='"labels":{"nodes":[]},"children":{"nodes":[{"id":"C"}]}'
count_body() { printf '{"data":{"issues":{"nodes":[%s]}}}' "$1"; }
C_LEAF="{$LEAF}"; C_PARKED="{$PARKED}"; C_EPIC="{$EPIC}"
C_NOLABELS='{"children":{"nodes":[]}}'; C_NOCHILDREN='{"labels":{"nodes":[]}}'; C_CHILDNULL='{"labels":{"nodes":[]},"children":null}'
C_CHILDEMPTY='{"labels":{"nodes":[]},"children":{}}'; C_LABELSEMPTY='{"labels":{},"children":{"nodes":[]}}'
call "$(count_body "$C_LEAF,$C_PARKED,$C_EPIC,$C_LEAF")" "$CNT"
[ "$RC" = 0 ] && [ "$OUT" = 2 ] || fail "count, well-formed: wanted 2 (two leaves; not parked, not the epic)"
call '{"data":{"issues":{"nodes":[]}}}' "$CNT"
[ "$RC" = 0 ] && [ "$OUT" = 0 ] || fail "count, nothing in flight: wanted 0"
for case_ in "no issue list|{\"data\":{\"issues\":{}}}" \
             "node without labels|$(count_body "$C_LEAF,$C_NOLABELS")" \
             "node without children|$(count_body "$C_LEAF,$C_NOCHILDREN")" \
             "children null|$(count_body "$C_LEAF,$C_CHILDNULL")" \
             "children without nodes|$(count_body "$C_LEAF,$C_CHILDEMPTY")" \
             "labels without nodes|$(count_body "$C_LEAF,$C_LABELSEMPTY")"; do
  call "${case_#*|}" "$CNT"
  [ "$RC" = 27 ] || fail "count, ${case_%%|*}: exit $RC, wanted 27"
  [ -z "$OUT" ] || fail "count, ${case_%%|*}: printed a count ($OUT) from a broken answer"
done
echo "PASS the in-flight count stops with 27 when the answer lacks the issue list, a node's labels or its children"

# A ticket parked by the configured needs-human name takes no slot, as the picker skips it.
cp "$SB/.bureau.json" "$SB/bureau.json.orig"
jq '.linear.labels.needs_human.name = "Human Review"' "$SB/bureau.json.orig" > "$SB/.bureau.json"
C_PARKED_NAMED='{"labels":{"nodes":[{"name":"Human Review"}]},"children":{"nodes":[]}}'
call "$(count_body "$C_LEAF,$C_PARKED_NAMED,$C_PARKED")" "$CNT"
[ "$RC" = 0 ] && [ "$OUT" = 1 ] || fail "count, configured needs-human name: wanted 1 (the leaf only)"
cp "$SB/bureau.json.orig" "$SB/.bureau.json"
echo "PASS the in-flight count leaves out a ticket parked by the configured needs-human name"

# --- negative control: the v3.0.2 reads ---------------------------------------------------------
mkdir -p "$SB/old"; cp "$SCRIPTS/bureau-config.sh" "$SCRIPTS/bureau-env.sh" "$SB/old/"
python3 - "$SB/old/bureau-config.sh" <<'PY'
import pathlib, sys
p = pathlib.Path(sys.argv[1]); t = p.read_text()
subs = [
  ("""answer=$(linear_raw "$payload" "$_BUREAU_SHAPE_ISSUE_LABELS"' and all(.data.issues.nodes[]; (.children.nodes | type) == "array")') || return $?""",
   'answer=$(linear_raw "$payload") || return $?'),
  ("""answer=$(linear_raw "$payload" "$_BUREAU_SHAPE_ISSUE_LABELS"' and all(.data.issues.nodes[]; (.inverseRelations.nodes | type) == "array")') || return $?""",
   'answer=$(linear_raw "$payload") || return $?'),
  ("    [.data.issues.nodes[]\n", "    [(.data.issues.nodes // [])[]\n"),
  ("         ([.labels.nodes[].name]\n", "         ([(.labels.nodes // [])[].name]\n"),
  ("     | select((.children.nodes | length) == 0)]\n", "     | select(((.children.nodes // []) | length) == 0)]\n"),
  ("    .data.issues.nodes\n    | map(select(.identifier", "    (.data.issues.nodes // [])\n    | map(select(.identifier"),
  ("        ([.labels.nodes[].name] | map(select(. as $n", "        ([(.labels.nodes // [])[].name] | map(select(. as $n"),
  ("        ([.inverseRelations.nodes[]\n", "        ([(.inverseRelations.nodes // [])[]\n"),
  ('          | map(select(. == "needs-human" or . == $human or . == "blocked" or . == "wip"))\n',
   '          | map(select(. == "needs-human" or . == "blocked" or . == "wip"))\n'),
]
for old, new in subs:
    if t.count(old) != 1: sys.exit("negative control: %d matches for %r" % (t.count(old), old[:70]))
    t = t.replace(old, new)
p.write_text(t)
PY
call "$(pick_body "$NOLABELS4,$F5")" "$PICK" "$SB/old/bureau-config.sh"
[ "$RC" = 0 ] && [ "$OUT" = EXP-4 ] \
  || fail "negative control: the v3.0.2 picker no longer takes the ticket whose labels are missing, so this proves nothing"
call "$(pick_body "$NOREL4,$F5")" "$PICK" "$SB/old/bureau-config.sh"
[ "$RC" = 0 ] && [ "$OUT" = EXP-4 ] \
  || fail "negative control: the v3.0.2 picker no longer takes the ticket whose blockers are missing, so this proves nothing"
call '{"data":{"issues":{}}}' "$CNT" "$SB/old/bureau-config.sh"
[ "$RC" = 0 ] && [ "$OUT" = 0 ] \
  || fail "negative control: the v3.0.2 count no longer reads a missing issue list as 0, so this proves nothing"
jq '.linear.labels.needs_human.name = "Human Review"' "$SB/bureau.json.orig" > "$SB/.bureau.json"
call "$(count_body "$C_LEAF,$C_PARKED_NAMED,$C_PARKED")" "$CNT" "$SB/old/bureau-config.sh"
cp "$SB/bureau.json.orig" "$SB/.bureau.json"
[ "$RC" = 0 ] && [ "$OUT" = 2 ] \
  || fail "negative control: the v3.0.2 count no longer counts a ticket parked by the configured name, so this proves nothing"
echo "PASS negative control: the v3.0.2 reads pick a ticket without its labels or blockers, count a missing list as 0 and count a ticket parked by the configured name"
