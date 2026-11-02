"""Budget/stall exits reuse real dispatcher, shepherd, worker and ownership cleanup."""
import json
import sys
import unittest
from dispatcher_test import DispatcherFixture
from ticket_kill_switch_test import WithdrawalFixture


class DispatchBudget(DispatcherFixture):
    def test_budget_halts_shepherd_lane_and_preserves_work(self):
        self.config['agents']['ticket_budget']=dict(provider_calls=1); self.save_config()
        issue,branch=self.ticket(1)
        provider=self.bin/'claude'
        source=provider.read_text()
        provider.write_text(source.replace("event('provider-start')", "event('provider-start')\nPath('unfinished.txt').write_text('unpublished work\\n')",1))
        child=self.start()
        path=self.common/'dispatch-skips.json'
        self.wait_for(lambda:path.exists() and issue in json.loads(path.read_text()))
        self.stop(child)
        outcome=json.loads(path.read_text())[issue]
        self.assertEqual((25,'budget'),(outcome['code'],outcome['terminal_reason']))
        self.assertEqual(1,len(self.events('provider-end')))
        self.assertEqual({},json.loads((self.common/'leases.json').read_text()))
        self.assertTrue(list((self.common/'preserved').glob('*.json')))
        self.assertFalse(self.events('gh-create'))

    def test_under_budget_lane_retains_normal_handoff(self):
        self.config['agents']['ticket_budget']=dict(provider_calls=10); self.save_config()
        issue,branch=self.ticket(1)
        child=self.start(); path=self.common/'dispatch-skips.json'
        self.wait_for(lambda:path.exists() and issue in json.loads(path.read_text()))
        self.stop(child)
        outcome=json.loads(path.read_text())[issue]
        self.assertEqual(20,outcome['code'],self.output())
        self.assertIsNone(outcome['terminal_reason'])
        self.assertEqual(1,len(self.events('provider-end')))


class WorkerProgress(WithdrawalFixture):
    def test_stall_at_stage_boundary_preserves_and_releases_like_withdrawal(self):
        self.config['agents']['ticket_progress']=dict(enabled=True)
        self.save_config()
        # Keep the actual stage entry/ownership wrappers; this fake turn changes
        # files and checks boxes, but supplies no qualified progress evidence.
        stage=self.repo/'scripts/implement-pipeline.sh'
        stage.write_text('''#!/bin/bash
set -euo pipefail
source "$(dirname "$0")/bureau-config.sh"
ISSUE="$1"
bureau_stage_enter "$ISSUE" "$@"
echo activity >> unfinished.txt
sed -i.bak 's/\\[ \\]/[X]/g' specs/*/tasks.md
git add specs
git commit -qm 'fixture activity'
''')
        first=self.worker(); self.assertEqual(0,first.returncode,first.stdout+first.stderr)
        second=self.worker(); self.assertEqual(25,second.returncode,second.stdout+second.stderr)
        self.assertIn('terminal_reason=stalled',second.stderr)
        issue=self.config['linear']['teams'][0]['key']+'-9'
        ledger=json.loads((self.common/'bureau/tickets'/(issue+'.json')).read_text())
        self.assertEqual(('stalled',2),(ledger['terminal_reason'],ledger['non_improving_turns']))
        self.assertEqual({},ledger['facts'])
        self.assert_released()
        self.assertTrue((self.wt/'unfinished.txt').exists())
        self.assertEqual('fixture activity',self.git('-C',str(self.wt),'log','-1','--format=%s'))
        self.assertTrue(list((self.common/'bureau/preserved').glob('*.json')))


if __name__ == '__main__':
    suite=unittest.TestSuite([
        DispatchBudget('test_budget_halts_shepherd_lane_and_preserves_work'),
        DispatchBudget('test_under_budget_lane_retains_normal_handoff'),
        WorkerProgress('test_stall_at_stage_boundary_preserves_and_releases_like_withdrawal')])
    sys.exit(not unittest.TextTestRunner(verbosity=2).run(suite).wasSuccessful())
