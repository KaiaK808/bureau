#!/usr/bin/env python3
# A successful semantic repair followed by a failing HEAD read hands over no
# resolver cause: the later failure keeps its own cause (other), exit 17.
import importlib.util, json, os, subprocess, sys, tempfile, uuid
from pathlib import Path
scripts = (Path(__file__).resolve().parent.parent / "templates/scripts") if len(sys.argv) < 2 else Path(sys.argv[1]).resolve()
spec = importlib.util.spec_from_file_location('handoff_evidence', scripts/'bureau-evidence.py')
e = importlib.util.module_from_spec(spec); spec.loader.exec_module(e)
with tempfile.TemporaryDirectory(prefix='bureau-test-handoff-') as directory:
    repo=Path(directory)
    for args in [('init','-q','-b','main'),('config','user.name','Fixture'),('config','user.email','fixture@example.invalid'),('commit','-qm','fixture','--allow-empty')]:
        subprocess.run(['git','-C',str(repo),*args],check=True)
    (repo/'.bureau.json').write_text('{}')
    head=subprocess.check_output(['git','-C',str(repo),'rev-parse','HEAD'],text=True).strip()
    store=e.Store(repo/'.git/bureau'); run=uuid.uuid4().hex
    store.begin_run(run,'T-1',repo,head)
    attempt=store.begin_attempt(run,'T-1','code_review','stage',repo,head)
    trace=repo/'trace'
    shell=r"""
set -euo pipefail
source "$SOURCE_CONFIG"
git() {
  case "$*" in
    'fetch origin --quiet') return 0 ;;
    'merge-base --is-ancestor origin/main HEAD') return 1 ;;
    'rev-parse HEAD') printf '%s\n' head-read-failed >> "$TRACE"; return 1 ;;
    *) command git "$@" ;;
  esac
}
bureau_ticket_checkpoint() { :; }
bureau_exec_runtime() {
  while [ "$#" -gt 0 ]; do
    if [ "$1" = --result-file ]; then
      printf '%s\n' '{"semantic":true}' > "$2"
      printf '%s\n' transaction-exit-0-semantic-true >> "$TRACE"
      return 0
    fi
    shift
  done
  return 99
}
_throttle_should_suppress() { return 1; }
_throttle_record() { :; }
post_comment() { printf '%s\n' comment >> "$TRACE"; }
mark_needs_human() { printf '%s\n' hold >> "$TRACE"; }
rc=0
merge_origin_main_or_abort T-1 'Code Review' || rc=$?
printf 'enclosing-exit=%s\n' "$rc" >> "$TRACE"
exit "$rc"
"""
    env={**os.environ,'SOURCE_CONFIG':str(scripts/'bureau-config.sh'),'BUREAU_CONFIG':str(repo/'.bureau.json'),
         'BUREAU_RUN_ID':run,'BUREAU_STAGE_ATTEMPT_ID':attempt,'BUREAU_DRY_RUN':'0','TRACE':str(trace)}
    result=subprocess.run(['/bin/bash','-c',shell],cwd=repo,env=env,capture_output=True,text=True,timeout=30)
    store.end_attempt(run,attempt,result.returncode,head)
    rows=[json.loads(line) for line in (store.root/'stage-outcomes.jsonl').read_text().splitlines()]
    manifest=store._manifest(run)
    signals=next(a for a in manifest['attempts'] if a['attempt_id']==attempt).get('outcome_signals',[])
    events=trace.read_text().splitlines()
    actual={'exit':result.returncode,'cause_class':rows[-1]['cause_class'],'outcome_signals':signals,'events':events}
    print(json.dumps(actual,sort_keys=True),flush=True)
    assert events==['transaction-exit-0-semantic-true','head-read-failed','comment','hold','enclosing-exit=17'], (events,result.stdout,result.stderr)
    assert (result.returncode,rows[-1]['cause_class'],signals)==(17,'other',[]), actual
print('PASS successful repair then failed HEAD read keeps cause other')
