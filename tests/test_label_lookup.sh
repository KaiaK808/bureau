#!/bin/bash
# A label is attached by the id that belongs to the issue's team, and a failed lookup is
# never mistaken for "no such label".
#
# Runs the REAL add_issue_label / remove_issue_label / _resolve_label_id from
# templates/scripts/bureau-config.sh under /bin/bash against a stubbed `curl`: issue lookups
# answer with a UUID, label lookups with the candidates of the case, mutations record the
# label id they were sent. The negative control runs the old name-only lookup (`first: 1`)
# against the case that broke it: another team's label of the same name listed first.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")" && cd .. && pwd)"
SCRIPTS="$REPO_ROOT/templates/scripts"
SB=$(mktemp -d -t bureau-test.labels.XXXXXXXX)
trap 'rm -rf "$SB"' EXIT

fail() { echo "FAIL $*" >&2; printf '  | rc=%s stdout=%s\n  | stderr=%s\n' "${RC:-}" "${OUT:-}" "${ERR:-}" >&2; exit 1; }

cat > "$SB/.bureau.json" <<'EOF'
{
  "linear": {
    "teams": [{
      "id": "team-id", "key": "EXP", "name": "Test",
      "states": {
        "triage": "s1", "spec": "s2", "spec_review": "s3", "design": "s4",
        "build": "s5", "build_review": "s6", "done": "s7"
      }
    }],
    "labels": {
      "lane2":            { "id": "l1", "name": "lane-2" },
      "needs_human":      { "id": "l2", "name": "needs-human" },
      "needs_ux":         { "id": "l3", "name": "needs-ux" },
      "ai_implementable": { "id": "l4", "name": "ai-implementable" }
    },
    "projects": []
  },
  "agents": { "poll_interval_minutes": 30, "max_review_cycles": 3 },
  "repo": { "branch_prefix": "feat", "specs_dir": "specs" }
}
EOF

mkdir -p "$SB/bin"
cat > "$SB/bin/curl" <<EOF
#!/bin/bash
prev=""; payload=""
for a in "\$@"; do [ "\$prev" = "-d" ] && payload="\$a"; prev="\$a"; done
case "\$payload" in
  *issueAddLabel*|*issueRemoveLabel*)
    printf '%s\n' "\$payload" | jq -r '.variables.lid' >> "$SB/mutations.log"
    echo '{"data":{"issueAddLabel":{"success":true},"issueRemoveLabel":{"success":true}}}' ;;
  *issueLabels*)
    [ -f "$SB/labels.broken" ] && { echo '<html>502</html>'; exit 0; }
    printf '{"data":{"issueLabels":{"nodes":%s}}}' "\$(cat "$SB/labels.json")" ;;
  *) echo '{"data":{"issues":{"nodes":[{"id":"ISSUE-UUID"}]}}}' ;;
esac
EOF
printf '#!/bin/bash\n:\n' > "$SB/bin/sleep"
chmod +x "$SB/bin/curl" "$SB/bin/sleep"

labels() { printf '%s' "$1" > "$SB/labels.json"; rm -f "$SB/labels.broken"; }
helper() {  # $1 = snippet; sets OUT ERR RC SENT (label ids sent in mutations, space-separated)
  rm -f "$SB/mutations.log"
  set +e
  OUT=$(cd "$SB" && PATH="$SB/bin:$PATH" LINEAR_API_KEY=k BUREAU_LINEAR_RETRIES=0 /bin/bash -c "
    set -uo pipefail
    source '$SCRIPTS/bureau-config.sh'
    $1" 2>"$SB/err")
  RC=$?
  set -e
  ERR=$(cat "$SB/err")
  SENT=""
  if [ -f "$SB/mutations.log" ]; then SENT=$(tr '\n' ' ' < "$SB/mutations.log" | sed 's/ $//'); fi
}

OTHER='{"id":"L-SPO","team":{"key":"SPO"}}'
OWN='{"id":"L-EXP","team":{"key":"EXP"}}'
WORKSPACE='{"id":"L-WS","team":null}'

labels "[$OTHER,$OWN]"
helper 'add_issue_label EXP-1 needs-human'
[ "$RC" = 0 ] && [ "$SENT" = "L-EXP" ] || fail "own team's label not chosen over another team's listed first"
helper 'add_issue_label 3f1c2a9e-0000-4000-8000-000000000000 needs-human'
[ "$RC" = 0 ] && [ "$SENT" = "L-EXP" ] || fail "a UUID reference did not fall back to the configured team"
labels "[$OTHER,$WORKSPACE]"
helper 'add_issue_label EXP-1 needs-human'
[ "$RC" = 0 ] && [ "$SENT" = "L-WS" ] || fail "workspace label not chosen when the team has none"
labels "[$WORKSPACE,$OWN]"
helper 'add_issue_label EXP-1 needs-human'
[ "$RC" = 0 ] && [ "$SENT" = "L-EXP" ] || fail "workspace label chosen over the team's own"
echo "PASS the team's own label wins, then a workspace label, whatever order Linear lists them in"

labels "[$OTHER]"
helper 'add_issue_label EXP-1 needs-human'
[ "$RC" = 1 ] && [ -z "$SENT" ] || fail "only another team's label: add must fail without sending"
case "$ERR" in *"no label named 'needs-human'"*) ;; *) fail "missing label not reported" ;; esac
helper 'remove_issue_label EXP-1 needs-human'
[ "$RC" = 0 ] && [ -z "$SENT" ] || fail "only another team's label: remove must be an idempotent success without sending"
echo "PASS another team's label is never attached; removing a label the team does not have is a no-op"

for broken in '{"id":"L-X"}' '{"id":"L-X","team":{"key":""}}' '{"id":"","team":{"key":"EXP"}}' '{"id":null,"team":null}' '{"id":"L-X","team":"EXP"}'; do
  labels "[$OTHER,$broken]"
  helper 'add_issue_label EXP-1 needs-human'
  [ "$RC" = 1 ] && [ -z "$SENT" ] || fail "malformed node $broken: add must fail without sending"
  case "$ERR" in *"label lookup failed"*) ;; *) fail "malformed node $broken: not reported as a failed lookup" ;; esac
  helper 'remove_issue_label EXP-1 needs-human'
  [ "$RC" = 1 ] && [ -z "$SENT" ] || fail "malformed node $broken: remove reported success while the label may still be attached"
done
labels "[{\"id\":\"L-X\"},$OWN]"
helper 'add_issue_label EXP-1 needs-human'
[ "$RC" = 0 ] && [ "$SENT" = "L-EXP" ] || fail "a malformed sibling blocked a usable winner"
echo "PASS a label that cannot be classified is a failed lookup, never 'not found' — unless a usable winner exists"

touch "$SB/labels.broken"
helper 'add_issue_label EXP-1 needs-human'
[ "$RC" = 27 ] && [ -z "$SENT" ] || fail "Linear unusable on the label lookup: add must return 27"
helper 'remove_issue_label EXP-1 needs-human'
[ "$RC" = 27 ] && [ -z "$SENT" ] || fail "Linear unusable on the label lookup: remove must return 27"
echo "PASS Linear failing on the label lookup is 27 for add and remove"

# Negative control: the lookup this replaces, against the case that broke it.
labels "[$OTHER,$OWN]"
helper '
add_issue_label() {
  local ref="$1" name="$2" uuid label_answer label_id payload label_result
  uuid=$(_resolve_issue_uuid "$ref") || return $?
  label_answer=$(linear_query "{ issueLabels(filter: { name: { eq: \\\"$name\\\" } }, first: 1) { nodes { id } } }") || return $?
  label_id=$(printf "%s" "$label_answer" | jq -r ".data.issueLabels.nodes[0].id // empty")
  payload=$(jq -n --arg id "$uuid" --arg lid "$label_id" "{query: \"mutation(\$id: String!, \$lid: String!) { issueAddLabel(id: \$id, labelId: \$lid) { success } }\", variables: {id: \$id, lid: \$lid}}")
  label_result=$(linear_raw "$payload") || return $?
}
add_issue_label EXP-1 needs-human'
[ "$SENT" = "L-SPO" ] || fail "negative control: the old lookup no longer picks the other team's label, so this test proves nothing"
echo "PASS negative control: the old name-only lookup sends the other team's label id"
