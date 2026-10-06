#!/bin/bash
# The shepherd refuses a ticket a human holds before it claims it (v3.1).
#
# Runs the REAL shepherd.sh with the REAL bureau-config.sh, bureau-env.sh and runtime in a
# sandbox repository whose path has a space. Only the edges are doubles: `curl` plays Linear
# (answers by query, records every call and mutation) and Telegram (records the alert text);
# bureau-worker.sh is replaced by a recorder, so "a stage ran" is one line in stages.log and
# the stage moves the ticket to Done. A hold is: needs-human, the configured
# linear.labels.needs_human.name, blocked or wip on the ticket, or the local hold file that
# mark_needs_human writes when it cannot write the label (<git common dir>/bureau/
# needs-human-held/<ISSUE>).
#
# Held → exit 25 before the claim: no shepherd-focused, no --from-stage move, no comment, no
# alert, and a line naming the hold and how to release it. A hold file answers without any
# label read; labels match exactly (needs-human-later, Needs-Human and WIP hold nothing). A
# hold left on a finished ticket changes nothing: Done ends with 0 and a cancelled ticket with
# 26, as in the loop, and an orchestrated chain goes on past it. --dry-run gives the same
# answer (25, no route). A label read that fails before
# the claim writes nothing either (27 with the fault class, 1 for any other code, 130 for a
# signal). The loop keeps the same check on every turn (a configured label or a hold file
# that appears after a move halts with 25 and a comment). Negative controls rebuild the
# v3.0.2 checks out of the current shepherd (CI checks out without history): the held
# ticket is claimed, moved and its stage runs; and a check that refuses a finished ticket
# stops the orchestrated chain.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")" && cd .. && pwd)"
SCRIPTS="$REPO_ROOT/templates/scripts"
# Physical path: the runtime hands the stages a resolved BUREAU_CONFIG (macOS /var → /private/var).
SB=$(cd "$(mktemp -d -t bureau-test.shephold.XXXXXXXX)" && pwd -P)
trap 'rm -rf "$SB"' EXIT
unset BUREAU_CONFIG BUREAU_DRY_RUN BUREAU_NO_MERGE BUREAU_STOP_REQUESTED BUREAU_ACTIVE_ENTRY BUREAU_RUN_ID \
      BUREAU_ALERT_THROTTLE_FILE TELEGRAM_BOT_TOKEN TELEGRAM_ALERT_CHAT_ID TMUX 2>/dev/null || true

fail() {
  echo "FAIL $*" >&2
  printf '  | rc=%s\n  | linear: %s\n  | stages: %s\n' "${RC:-}" "${LIN:-}" "${STAGES:-}" >&2
  sed 's/^/  | out: /' "$SB/out" >&2 2>/dev/null || true
  sed 's/^/  | err: /' "$SB/err" >&2 2>/dev/null || true
  exit 1
}

# --- the fake Linear and Telegram --------------------------------------------------------
# State: $SB/state (a state id), $SB/state-name (the name Linear gives a state the config does
# not know, default "?"), $SB/labels.json (the ticket's label names), $SB/labels-broken and
# $SB/state-broken (that read answers with an error page). $SB/on-move runs after every move.
mkdir -p "$SB/bin" "$SB/tmp"
cat > "$SB/bin/curl" <<EOF
#!/bin/bash
sb="$SB"
prev=""; payload=""; text=""; url=""; config=""
for a in "\$@"; do
  case "\$prev" in
    -d) payload="\$a" ;;
    --data-urlencode) case "\$a" in text=*) text="\${a#text=}" ;; esac ;;
    -K) [ "\$a" != - ] || config=\$(cat) ;;
  esac
  case "\$a" in https://*) url="\$a" ;; esac
  prev="\$a"
done
# The Telegram URL (it holds the token) and the text come on stdin as curl config lines (curl -K -).
case "\$config" in *'url = "https://api.telegram.org/'*) url=https://api.telegram.org/ ;; esac
config_text=\$(printf '%s\n' "\$config" | sed -n 's/^data-urlencode = "text=\(.*\)"\$/\1/p')
[ -z "\$config_text" ] || text="\$config_text"
case "\$url" in *api.telegram.org*) printf '%s\n' "\$text" >> "\$sb/alerts.log"; exit 0 ;; esac
labels=\$(jq -c '{nodes: map({name: .})}' "\$sb/labels.json")
case "\$payload" in
  *commentCreate*)    echo comment >> "\$sb/linear.log"; b='{"data":{"commentCreate":{"success":true}}}' ;;
  *issueAddLabel*)    echo add-label >> "\$sb/linear.log"; b='{"data":{"issueAddLabel":{"success":true}}}' ;;
  *issueRemoveLabel*) echo remove-label >> "\$sb/linear.log"; b='{"data":{"issueRemoveLabel":{"success":true}}}' ;;
  *issueUpdate*)
    sid=\$(printf '%s' "\$payload" | jq -r .variables.sid)
    echo "move-\$sid" >> "\$sb/linear.log"; printf '%s' "\$sid" > "\$sb/state"
    [ ! -f "\$sb/on-move" ] || bash "\$sb/on-move"
    b='{"data":{"issueUpdate":{"success":true}}}' ;;
  *issueLabels*)      echo label-id >> "\$sb/linear.log"; b='{"data":{"issueLabels":{"nodes":[{"id":"L1","team":null}]}}}' ;;
  *viewer*)           echo viewer >> "\$sb/linear.log"; b='{"data":{"viewer":{"id":"V"}}}' ;;
  *'nodes { identifier title description project'*)
    echo labels >> "\$sb/linear.log"
    if [ -f "\$sb/labels-broken" ]; then b='<html>502 Bad Gateway</html>'
    else b=\$(jq -nc --argjson l "\$labels" '{data:{issues:{nodes:[{identifier:"EXP-7",title:"T",description:"D",project:null,labels:\$l}]}}}'); fi ;;
  *'nodes { id identifier title description state'*)
    echo state >> "\$sb/linear.log"
    if [ -f "\$sb/state-broken" ]; then b='<html>502 Bad Gateway</html>'
    else b=\$(jq -nc --argjson l "\$labels" --arg s "\$(cat "\$sb/state")" --arg n "\$(cat "\$sb/state-name" 2>/dev/null || echo '?')" '{data:{issues:{nodes:[{id:"U7",identifier:"EXP-7",title:"T",description:"D",state:{id:\$s,name:\$n},labels:\$l}]}}}'); fi ;;
  *'nodes { branchName'*) echo branch >> "\$sb/linear.log"; b='{"data":{"issues":{"nodes":[{"branchName":"exp-7-x","comments":{"nodes":[]}}]}}}' ;;
  *'nodes { id } }'*) echo uuid >> "\$sb/linear.log"; b='{"data":{"issues":{"nodes":[{"id":"U7"}]}}}' ;;
  *)                  echo unmatched >> "\$sb/linear.log"; b='{}' ;;
esac
printf '%s' "\$b"
"$REPO_ROOT/tests/lib/curl-writeout.sh" 200 "\$@"
EOF
chmod +x "$SB/bin/curl"

# new_repo <needs-human name> — a fresh sandbox repository with the real scripts; the worker
# is a recorder whose "stage" moves the ticket to Done. Sets REPO and HOLD.
new_repo() {
  REPO=$(mktemp -d "$SB/repo XXXXXXXX")
  mkdir -p "$REPO/scripts"
  git -C "$REPO" init -q
  git -C "$REPO" -c user.name=t -c user.email=t@t commit -q --allow-empty -m init
  cp "$SCRIPTS"/*.sh "$SCRIPTS"/*.py "$REPO/scripts/"
  cat > "$REPO/scripts/bureau-worker.sh" <<WORKER
#!/bin/bash
echo "\$2" >> "$SB/stages.log"
printf s8 > "$SB/state"
WORKER
  jq -n --arg human "$1" '{
    linear: {teams: [{id: "t", key: "EXP", name: "T",
      states: {triage: "s1", spec: "s2", spec_review: "s3", design: "s4", build: "s5", build_review: "s6", merge: "s7", done: "s8"}}],
      labels: {lane2: {id: "l1", name: "lane-2"}, needs_human: {id: "l2", name: $human},
               needs_ux: {id: "l3", name: "needs-ux"}, ai_implementable: {id: "l4", name: "ai-implementable"}},
      projects: []},
    agents: {poll_interval_minutes: 30, max_review_cycles: 3},
    repo: {branch_prefix: "feat", specs_dir: "specs"}}' > "$REPO/.bureau.json"
  printf 'LINEAR_API_KEY=k\nTELEGRAM_BOT_TOKEN=t\nTELEGRAM_ALERT_CHAT_ID=c\n' > "$REPO/.env"
  HOLD="$REPO/.git/bureau/needs-human-held"
}

# ticket <state-id> <labels-json> — what the fake Linear answers for the fixture ticket.
ticket() {
  printf '%s' "$1" > "$SB/state"; printf '%s' "$2" > "$SB/labels.json"
  rm -f "$SB/labels-broken" "$SB/state-broken" "$SB/state-name" "$SB/on-move" "$SB/stages.log"
}

# run [shepherd args…] — sets RC, LIN (Linear calls), STAGES, ALERTS.
run() {
  rm -f "$SB/linear.log" "$SB/alerts.log"
  set +e
  (cd "$REPO" && PATH="$SB/bin:$PATH" TMPDIR="$SB/tmp" BUREAU_LINEAR_RETRIES=0 BUREAU_SHEPHERD_CONFIRM_SECONDS=0 BUREAU_DISABLE_THROTTLE=1 \
     bash scripts/shepherd.sh --no-tmux "$@" > "$SB/out" 2> "$SB/err")
  RC=$?
  set -e
  LIN=""; [ ! -f "$SB/linear.log" ] || LIN=$(tr '\n' ' ' < "$SB/linear.log")
  STAGES=""; [ ! -f "$SB/stages.log" ] || STAGES=$(tr '\n' ' ' < "$SB/stages.log")
  ALERTS=""; [ ! -f "$SB/alerts.log" ] || ALERTS=$(cat "$SB/alerts.log")
}

# nothing_written <label> — no mutation reached Linear, no alert went out, no stage ran.
nothing_written() {
  case " $LIN" in *" add-label"*|*" remove-label"*|*" move-"*|*" comment"*) fail "$1: a Linear write went out" ;; esac
  [ -z "$ALERTS" ] || fail "$1: an alert went out"
  [ -z "$STAGES" ] || fail "$1: a stage ran"
}

# old_checks <repo> — the v3.0.2 checks, rebuilt out of the current shepherd: no check before
# the claim, none in the dry run, and the loop's label check of v3.0.2 (three fixed names, no
# hold file). Fails loudly when the text it replaces has moved.
old_checks() {
  python3 - "$1/scripts/shepherd.sh" <<'PY_EOF' || fail "could not build the negative control"
import pathlib, re, sys
p = pathlib.Path(sys.argv[1]); t = p.read_text()
def cut(pattern, new=''):
    global t
    t, n = re.subn(pattern, new, t, flags=re.S)
    if n != 1: sys.exit("pattern matched %d times: %s" % (n, pattern[:60]))
cut(r'\n: > "\$SHEPHERD_FAULT_FILE" 2>/dev/null \|\| true\nHOLD_RC=0\n.*?\n  exit 25\nfi\n', '\n')
cut(r'\n  # The hold check a run makes before its claim.*?\n    exit 1\n  fi\n', '\n')
cut(r'\n  if \[ -n "\$HOLD" \]; then\n    echo "  Held: .*?\n  fi\n', '\n')
cut(r'\n_shepherd_human_label\(\) \{\n.*?\n\}\n', r'''
_shepherd_human_label() {
  local detail
  detail=$(_BUREAU_LINEAR_FAULT_FILE="$SHEPHERD_FAULT_FILE" get_issue_detail "$ISSUE") || return $?
  printf '%s' "$detail" | jq -rn '
    input | .labels as $on
    | [ "needs-human", "blocked", "wip" ][] | select(. as $l | $on | any(.[]; . == $l))' \\
    | sed -n 1p
}
''')
p.write_text(t)
PY_EOF
}

# --- a free ticket: the check lets it through, before the claim ----------------------------
new_repo "needs-human"
ticket s5 '["lane-2"]'
run EXP-7
[ "$RC" = 0 ] || fail "free ticket: exit $RC, wanted 0 (its stage moves it to Done)"
[ "$STAGES" = "implement-pipeline.sh " ] || fail "free ticket: the stage did not run once"
case "$LIN" in "viewer labels uuid label-id add-label "*) ;; *) fail "free ticket: the labels are not read between the start check and the claim" ;; esac
ticket s5 '["lane-2"]'
run --from-stage build EXP-7
[ "$RC" = 0 ] || fail "free ticket, --from-stage: exit $RC"
case "$LIN" in "viewer labels uuid label-id add-label uuid move-s5 "*) ;; *) fail "free ticket, --from-stage: not check, claim, move in that order" ;; esac
# Labels match exactly, as in the picker: a longer name or another case holds nothing.
for free in needs-human-later Needs-Human WIP; do
  ticket s5 "[\"lane-2\",\"$free\"]"
  run EXP-7
  [ "$RC" = 0 ] && [ "$STAGES" = "implement-pipeline.sh " ] || fail "label '$free' held the ticket"
done
echo "PASS a free ticket: the labels are read after the start check and before the claim; claim, move and stage follow; labels match exactly"

# --- held by a label: refused before the claim, nothing written ----------------------------
for held in needs-human blocked wip; do
  ticket s5 "[\"lane-2\",\"$held\"]"
  run --from-stage build EXP-7
  [ "$RC" = 25 ] || fail "$held: exit $RC, wanted 25"
  [ "$LIN" = "viewer labels " ] || fail "$held: Linear calls beyond the start check and the label read"
  nothing_written "$held"
  grep -q "EXP-7 carries '$held' — a human holds it; remove the label in Linear to release it — not claimed, nothing written (exit 25)" "$SB/err" \
    || fail "$held: the refusal does not name the label and the release"
done
# The configured name counts as well as the default one.
new_repo "Human Review"
ticket s5 '["Human Review"]'
run --from-stage build EXP-7
[ "$RC" = 25 ] && [ "$LIN" = "viewer labels " ] || fail "configured needs-human name: not refused before the claim"
nothing_written "configured name"
grep -q "carries 'Human Review'" "$SB/err" || fail "configured name: the refusal does not name it"
# Without --from-stage a held ticket's state is read (only then) to tell a leftover hold on a
# finished ticket from a real one; an open ticket is refused all the same.
run EXP-7
[ "$RC" = 25 ] && [ "$LIN" = "viewer labels state " ] || fail "configured name, no --from-stage: wanted 25 after the label and state reads"
nothing_written "configured name, no --from-stage"
echo "PASS a ticket held by needs-human, blocked, wip or the configured name: 25 before the claim, nothing written"

# --- held locally: the hold file answers without a label read ------------------------------
new_repo "needs-human"
ticket s5 '["lane-2"]'
mkdir -p "$HOLD"; printf 'stage=qa\texit=1\tat=x\n' > "$HOLD/EXP-7"
run --from-stage build EXP-7
[ "$RC" = 25 ] || fail "hold file: exit $RC, wanted 25"
[ "$LIN" = "viewer " ] || fail "hold file: Linear was asked for more than the start check"
nothing_written "hold file"
grep -qF "EXP-7 is held for a human in $HOLD/EXP-7 (a stage could not write the needs-human label)" "$SB/err" \
  || fail "hold file: the refusal does not name the file"
grep -q "to release it without the label, delete that file — not claimed, nothing written (exit 25)" "$SB/err" \
  || fail "hold file: the refusal does not say how to release it"
[ -f "$HOLD/EXP-7" ] || fail "hold file: the shepherd removed the hold"
# Another ticket's hold does not hold this one.
mv "$HOLD/EXP-7" "$HOLD/EXP-70"
run EXP-7
[ "$RC" = 0 ] && [ "$STAGES" = "implement-pipeline.sh " ] || fail "a hold of EXP-70 held EXP-7"
rm -rf "$HOLD"
echo "PASS a ticket held locally: 25 before the claim without a label read, the file named, the hold kept"

# --- the dry run gives the same answer -------------------------------------------------------
ticket s5 '["lane-2"]'
mkdir -p "$HOLD"; : > "$HOLD/EXP-7"
run --dry-run --from-stage build EXP-7
[ "$RC" = 25 ] && [ "$LIN" = "state " ] || fail "dry run, hold file: wanted 25 after the state read alone"
grep -qF "Held: EXP-7 is held for a human in $HOLD/EXP-7" "$SB/out" || fail "dry run, hold file: the hold is not printed"
grep -q "A run refuses this ticket before claiming it: exit 25, nothing written" "$SB/out" || fail "dry run: the refusal is not printed"
grep -q "Forward route" "$SB/out" && fail "dry run, hold file: a route was printed"
nothing_written "dry run, hold file"
rm -rf "$HOLD"
new_repo "Human Review"
ticket s5 '["Human Review"]'
run --dry-run EXP-7
[ "$RC" = 25 ] && [ "$LIN" = "state labels " ] || fail "dry run, configured name: wanted 25"
grep -q "Held: EXP-7 carries 'Human Review'" "$SB/out" || fail "dry run, configured name: the label is not printed"
nothing_written "dry run, configured name"
ticket s5 '["lane-2"]'
run --dry-run EXP-7
[ "$RC" = 0 ] && grep -q "Build → implement-pipeline.sh" "$SB/out" || fail "dry run, free ticket: no route"
echo "PASS the dry run answers like a run: a held ticket prints the hold and no route and ends with 25"

# --- a label read that fails before the claim writes nothing ---------------------------------
ticket s5 '["lane-2"]'; : > "$SB/labels-broken"
run --from-stage build EXP-7
[ "$RC" = 27 ] || fail "label read unusable: exit $RC, wanted 27"
[ "$LIN" = "viewer labels " ] || fail "label read unusable: calls after the failed read"
case " $LIN" in *" add-label"*|*" move-"*|*" comment"*) fail "label read unusable: a Linear write went out" ;; esac
[ -z "$STAGES" ] || fail "label read unusable: a stage ran"
grep -q "could not read the labels of EXP-7 — Linear stayed unusable after every retry (fault: not-json); EXP-7 not claimed, nothing written" "$SB/err" \
  || fail "label read unusable: the line does not name the read, the fault and that nothing was written"
case "$ALERTS" in *"shepherd did not start (could not read the labels, linear-unusable: not-json)"*) ;; *) fail "label read unusable: no alert naming the read and the fault" ;; esac
case "$ALERTS" in *"Repo: \`${REPO##*/}\`"*) ;; *) fail "the alert does not name the repository" ;; esac
run --dry-run EXP-7
[ "$RC" = 27 ] || fail "dry run, label read unusable: exit $RC"
grep -q "dry-run: could not read the labels of EXP-7 — Linear stayed unusable after every retry (fault: not-json). No route printed." "$SB/err" \
  || fail "dry run, label read unusable: the line does not name the read"
nothing_written "dry run, label read unusable"
# Any other failed read: 1; a read ended by a signal: 130, cancelled, no alert.
for pair in 5:1 143:130; do
  new_repo "needs-human"; ticket s5 '["lane-2"]'
  echo "get_issue_detail() { return ${pair%%:*}; }" >> "$REPO/scripts/bureau-config.sh"
  run EXP-7
  [ "$RC" = "${pair##*:}" ] || fail "label read ending ${pair%%:*}: exit $RC, wanted ${pair##*:}"
  case " $LIN" in *" add-label"*|*" move-"*|*" comment"*) fail "label read ending ${pair%%:*}: a Linear write went out" ;; esac
  [ -z "$STAGES" ] || fail "label read ending ${pair%%:*}: a stage ran"
done
grep -q "cancelled before EXP-7 was claimed; nothing written" "$SB/err" || fail "label read ending 143: not a cancelled run"
[ -z "$ALERTS" ] || fail "label read ending 143: alerted"
echo "PASS a label read that fails before the claim writes nothing: 27 with the fault and an alert, 1 for another code, 130 for a signal"

# --- a hold left on a finished ticket changes nothing -----------------------------------------
# A human finishes a ticket the shepherd halted on and leaves the label, or the hold file. The
# loop ends such a ticket before its labels matter (Done 0, cancelled 26); so does the check
# before the claim, and an orchestrated chain goes on past it.
new_repo "needs-human"
for held in needs-human blocked wip; do
  ticket s8 "[\"lane-2\",\"$held\"]"
  run EXP-7
  [ "$RC" = 0 ] || fail "Done + $held: exit $RC, wanted 0"
  [ "$LIN" = "viewer labels state " ] || fail "Done + $held: not the label read, then the state read alone"
  nothing_written "Done + $held"
  grep -q "terminal state 'Done' — done (the hold left on it stays: label $held; nothing claimed, nothing written)" "$SB/out" \
    || fail "Done + $held: no terminal line"
done
ticket s8 '["lane-2"]'; mkdir -p "$HOLD"; : > "$HOLD/EXP-7"
run EXP-7
[ "$RC" = 0 ] && [ "$LIN" = "viewer state " ] || fail "Done + hold file: wanted 0 after the state read alone"
nothing_written "Done + hold file"
[ -f "$HOLD/EXP-7" ] || fail "Done + hold file: the hold was removed"
rm -rf "$HOLD"
for name in Canceled Cancelled Duplicate; do
  ticket s9 '["needs-human"]'; printf '%s' "$name" > "$SB/state-name"
  run EXP-7
  [ "$RC" = 26 ] || fail "$name + needs-human: exit $RC, wanted 26"
  nothing_written "$name + needs-human"
done
# --from-stage would move the ticket back into the pipeline: its hold refuses it, no state read.
ticket s8 '["needs-human"]'
run --from-stage build EXP-7
[ "$RC" = 25 ] && [ "$LIN" = "viewer labels " ] || fail "Done + needs-human, --from-stage: wanted 25 without a state read"
nothing_written "Done + needs-human, --from-stage"
# The state read on the hold path fails like the label read: nothing written, 27 and an alert.
ticket s5 '["needs-human"]'; : > "$SB/state-broken"
run EXP-7
[ "$RC" = 27 ] && [ "$LIN" = "viewer labels state " ] || fail "held, state read unusable: wanted 27"
case " $LIN" in *" add-label"*|*" move-"*|*" comment"*) fail "held, state read unusable: a Linear write went out" ;; esac
grep -q "could not read the state of EXP-7 — Linear stayed unusable after every retry (fault: not-json); EXP-7 not claimed, nothing written" "$SB/err" \
  || fail "held, state read unusable: the line does not name the state read"
case "$ALERTS" in *"shepherd did not start (could not read the state, linear-unusable: not-json)"*) ;; *) fail "held, state read unusable: no alert" ;; esac
# The dry run: a finished ticket's labels are not read, its route is printed as before.
ticket s8 '["needs-human"]'
run --dry-run EXP-7
[ "$RC" = 0 ] && [ "$LIN" = "state " ] || fail "dry run, Done + needs-human: wanted 0 after the state read alone"
grep -q "Current state: Done" "$SB/out" && ! grep -q "Held:" "$SB/out" || fail "dry run, Done + needs-human: printed a hold"
run --dry-run --from-stage build EXP-7
[ "$RC" = 25 ] && grep -q "Held: EXP-7 carries 'needs-human'" "$SB/out" || fail "dry run, Done + needs-human, --from-stage: wanted the hold and 25"
# A real orchestrated chain goes on past a finished ticket with a leftover label.
ticket s8 '["lane-2","needs-human"]'
orch() {  # orch <chain> — the real orchestrate.sh over the real shepherd; sets RC
  rm -f "$SB/linear.log" "$SB/alerts.log"
  set +e
  (cd "$REPO" && PATH="$SB/bin:$PATH" TMPDIR="$SB/tmp" BUREAU_LINEAR_RETRIES=0 BUREAU_SHEPHERD_CONFIRM_SECONDS=0 BUREAU_DISABLE_THROTTLE=1 \
     bash scripts/orchestrate.sh --chain "$1" > "$SB/out" 2> "$SB/err")
  RC=$?
  set -e
}
orch EXP-7,EXP-8
[ "$RC" = 0 ] || fail "chain over Done + needs-human: exit $RC, wanted 0"
grep -q "✓ EXP-7 reached terminal state" "$SB/out" && grep -q "✓ EXP-8 reached terminal state" "$SB/out" \
  || fail "chain over Done + needs-human: the lane did not go on"
case " $(tr '\n' ' ' < "$SB/linear.log")" in *" add-label"*|*" move-"*|*" comment"*) fail "chain over Done + needs-human: a Linear write went out" ;; esac
# Negative control: the check without the finished-ticket branch stops the lane at once.
NCREPO="$REPO"; new_repo "needs-human"
python3 - "$REPO/scripts/shepherd.sh" <<'PY_EOF' || fail "could not build the negative control"
import pathlib, re, sys
p = pathlib.Path(sys.argv[1]); t = p.read_text()
t, n = re.subn(r'\n  case "\$HELD_STATE" in\n.*?\n  esac\n', '\n', t, flags=re.S)
if n != 1: sys.exit(1)
p.write_text(t)
PY_EOF
ticket s8 '["lane-2","needs-human"]'
orch EXP-7,EXP-8
[ "$RC" = 25 ] && grep -q "✗ EXP-7 exited 25 — STOPPING this lane" "$SB/err" && ! grep -q "→ shepherd EXP-8" "$SB/out" \
  || fail "negative control: without the finished-ticket branch the chain no longer stops at the leftover label, so this proves nothing"
REPO="$NCREPO"
echo "PASS a hold left on a finished ticket: Done ends with 0 and a cancelled one with 26, nothing written, the chain goes on; --from-stage still refuses it"

# --- the loop keeps the check on every turn -------------------------------------------------
new_repo "Human Review"
ticket s2 '["lane-2"]'   # Spec: the shepherd moves it to Triage itself, then reads again
printf "printf '%%s' '[\"Human Review\"]' > '%s/labels.json'\n" "$SB" > "$SB/on-move"
run EXP-7
[ "$RC" = 25 ] || fail "configured name after the move: exit $RC, wanted 25"
grep -q "'Human Review' label present on EXP-7 @ 'Triage' — halting" "$SB/out" || fail "configured name after the move: no halt line"
case "$LIN" in *comment*) ;; *) fail "configured name after the move: no halt comment" ;; esac
[ -z "$STAGES" ] || fail "configured name after the move: a stage ran"
ticket s2 '["lane-2"]'
printf "mkdir -p '%s'; : > '%s/EXP-7'\n" "$HOLD" "$HOLD" > "$SB/on-move"
run EXP-7
[ "$RC" = 25 ] || fail "hold file after the move: exit $RC, wanted 25"
grep -qF "EXP-7 is held for a human in $HOLD/EXP-7" "$SB/out" || fail "hold file after the move: no halt line"
case "$LIN" in *comment*) ;; *) fail "hold file after the move: no halt comment" ;; esac
[ -z "$STAGES" ] || fail "hold file after the move: a stage ran"
rm -rf "$HOLD"
echo "PASS the loop halts on the configured name and on a hold file that appear during the run"

# --- negative controls: the v3.0.2 checks --------------------------------------------------
new_repo "Human Review"; old_checks "$REPO"
ticket s5 '["Human Review"]'
run --from-stage build EXP-7
case "$LIN" in *add-label*move-s5*) ;; *) fail "negative control: the v3.0.2 shepherd no longer claims and moves a held ticket, so this proves nothing" ;; esac
[ "$STAGES" = "implement-pipeline.sh " ] || fail "negative control: the v3.0.2 shepherd no longer runs the stage of a ticket held by the configured name"
ticket s5 '["lane-2"]'
mkdir -p "$HOLD"; : > "$HOLD/EXP-7"
run --from-stage build EXP-7
[ "$STAGES" = "implement-pipeline.sh " ] || fail "negative control: the v3.0.2 shepherd no longer runs the stage of a locally held ticket"
run --dry-run EXP-7
[ "$RC" = 0 ] && grep -q "Forward route" "$SB/out" || fail "negative control: the v3.0.2 dry run no longer prints a route for a held ticket"
rm -rf "$HOLD"
new_repo "needs-human"; old_checks "$REPO"
ticket s5 '["lane-2","needs-human"]'
run --from-stage build EXP-7
case "$LIN" in *add-label*move-s5*) ;; *) fail "negative control: the v3.0.2 shepherd no longer claims and moves before its check" ;; esac
[ "$RC" = 25 ] || fail "negative control: the v3.0.2 loop check should still halt on needs-human (after the claim)"
echo "PASS negative control: the v3.0.2 checks claim, move and run the stage of a ticket held by the configured name or a hold file"
