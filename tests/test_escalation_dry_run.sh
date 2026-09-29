#!/bin/bash
# A dry run writes no escalation record (v3.1).
#
# Runs the REAL log_escalation, and the needs-human path that precedes it in the stages
# (mark_needs_human, whose label write only logs in a dry run and returns 0), from
# templates/scripts/bureau-config.sh in a temporary repository whose path has a space; no
# double is needed, nothing reaches Linear. BUREAU_DRY_RUN=1: neither logs/escalations.log nor
# logs/events.jsonl is written, the intent is on stderr. Without the dry run the record still
# matches the documented line format. The negative control takes the guard out of the current
# function (CI checks out without history): the dry run writes both records again.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")" && cd .. && pwd)"
SCRIPTS="$REPO_ROOT/templates/scripts"
SB=$(mktemp -d -t bureau-test.escdry.XXXXXXXX)
trap 'rm -rf "$SB"' EXIT
unset BUREAU_CONFIG BUREAU_DRY_RUN 2>/dev/null || true
fail() { echo "FAIL $*" >&2; printf '  | rc=%s\n  | err=%s\n' "${RC:-}" "${ERR:-}" >&2; exit 1; }

R="$SB/repo one"
mkdir -p "$R"
git -C "$R" init -q
cat > "$R/.bureau.json" <<'EOF'
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

# esc <config> <env-prefix> — the stages' sequence: mark_needs_human, then log_escalation.
esc() {
  rm -rf "$R/logs"
  set +e
  ERR=$(cd "$R" && env $2 /bin/bash -c "
    set -euo pipefail
    source '$1'
    mark_needs_human EXP-402 code-review && log_escalation EXP-402 code-review 3 'REQUEST_CHANGES exceeded \"max_review_cycles=3\"' 56 049-some-branch" 2>&1 >/dev/null)
  RC=$?
  set -e
}

esc "$SCRIPTS/bureau-config.sh" BUREAU_DRY_RUN=1
[ "$RC" = 0 ] || fail "dry run: exit $RC"
[ ! -e "$R/logs/escalations.log" ] || fail "dry run: logs/escalations.log was written"
[ ! -e "$R/logs/events.jsonl" ] || fail "dry run: logs/events.jsonl was written"
case "$ERR" in *"[DRY_RUN] log_escalation EXP-402 code-review cycle=3 pr=56 branch=049-some-branch: REQUEST_CHANGES exceeded"*) ;; *) fail "dry run: the intent is not on stderr" ;; esac
echo "PASS a dry run writes neither escalation record and says what it would have logged"

# Without the dry run the record is written in the documented format (log_escalation alone:
# the label write would need Linear).
rm -rf "$R/logs"
(cd "$R" && /bin/bash -c "set -euo pipefail; source '$SCRIPTS/bureau-config.sh'
  log_escalation EXP-402 code-review 3 'REQUEST_CHANGES exceeded \"max_review_cycles=3\"' 56 049-some-branch")
REGEX='^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z[[:space:]]+ESCALATED[[:space:]]+EXP-402[[:space:]]+code-review[[:space:]]+cycle=3[[:space:]]+reason="REQUEST_CHANGES exceeded .max_review_cycles=3."[[:space:]]+pr=56[[:space:]]+branch=049-some-branch$'
grep -qE "$REGEX" "$R/logs/escalations.log" || fail "a real run no longer writes the documented line"
grep -q '"event":"escalation"' "$R/logs/events.jsonl" || fail "a real run no longer emits the escalation event"
echo "PASS a real run still writes both records in the documented format"

# Negative control: the function without its guard (the form every release before v3.1 had).
mkdir -p "$SB/old"; cp "$SCRIPTS/bureau-config.sh" "$SCRIPTS/bureau-env.sh" "$SB/old/"
python3 - "$SB/old/bureau-config.sh" <<'PY'
import pathlib, re, sys
p = pathlib.Path(sys.argv[1]); t = p.read_text()
t, n = re.subn(r'(\nlog_escalation\(\) \{\n  local issue="\$1" pipeline="\$2" cycle="\$3" reason="\$4" pr="\$5" branch="\$6"\n)  if \[ "\$\{BUREAU_DRY_RUN:-0\}" = "1" \]; then\n.*?\n  fi\n', r'\1', t, flags=re.S)
if n != 1: sys.exit("negative control: the guard was not found")
p.write_text(t)
PY
esc "$SB/old/bureau-config.sh" BUREAU_DRY_RUN=1
[ -s "$R/logs/escalations.log" ] && [ -s "$R/logs/events.jsonl" ] \
  || fail "negative control: without the guard the dry run no longer writes the records, so this proves nothing"
echo "PASS negative control: without the guard a dry run writes an escalation that never happened"
