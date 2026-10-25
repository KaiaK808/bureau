"""Read the real pipeline's metadata and Git, without substituting its parser."""
import json
from pathlib import Path
import subprocess
import sys


def assert_publication(root, stage):
    evidence = root / '.git/bureau/evidence/v1'
    receipts = [json.loads(p.read_text()) for p in evidence.glob('runs/*/receipts/*.json')]
    assert len(receipts) == 1
    receipt = receipts[0]
    manifest = json.loads((evidence / 'runs' / receipt['run_id'] / 'manifest.json').read_text())
    attempt = next(a for a in manifest['attempts'] if a['attempt_id'] == receipt['stage_attempt_id'])
    publications, reruns = attempt['publications'], attempt['gate_reruns']
    assert len(publications) == len(reruns) == 1
    publication, rerun = publications[0], reruns[0]
    before = receipt['start_head']
    after = subprocess.check_output(['git', '-C', str(root), 'rev-parse', 'HEAD'], text=True).strip()
    assert before != after and receipt['end_head'] == before
    assert receipt['stage'] == stage
    assert set(publication) == set('provider_attempt_id stage_attempt_id pre_head head parent_head bureau_generated recorded_at'.split())
    assert publication['provider_attempt_id'] == receipt['provider_attempt_id']
    assert publication['stage_attempt_id'] == receipt['stage_attempt_id']
    assert publication['pre_head'] == publication['parent_head'] == before and publication['head'] == after
    assert publication['bureau_generated'] is True and publication['recorded_at'] >= receipt['ended_at']
    assert subprocess.check_output(['git', '-C', str(root), 'show', '-s', '--format=%P', after], text=True).split() == [before]
    assert subprocess.check_output(['git', '-C', str(root), 'log', '-1', '--format=%(trailers:key=Bureau-Generated,valueonly)', after], text=True).strip() == 'true'
    assert all(g['head'] == g['end_head'] == before for g in receipt['gate_attempts'])
    assert rerun['head'] == rerun['result']['head'] == rerun['result']['end_head'] == after
    assert rerun['gate_attempt_ids'] == [g['attempt_id'] for g in receipt['gate_attempts']]
    checked = (root / 'logs/test-heads').read_text().splitlines()
    assert checked[-1] == after and checked.count(after) == 1
    assert all(h == before for h in checked[:-1])
    assert len(checked) >= 2 if stage == 'qa' else checked == [after]
    changed = subprocess.check_output(['git', '-C', str(root), 'diff-tree', '--no-commit-id', '--name-only', '-r', after], text=True).splitlines()
    assert ('feature.py' if stage == 'implement' else 'tests/test_added.py') in changed
    print('PASS assert_publication: ' + stage + ' gate proof A, executor publication B, one outside reservation/run at B')


if __name__ == '__main__':
    assert_publication(Path(sys.argv[1]), sys.argv[2])
