"""A looping fake ticket exceeds its token allowance only after removing enforcement."""
import unittest
from ticket_budget_test import AdapterFixture


class BudgetControl(AdapterFixture):
    def test_shipped_guard_and_removed_guard(self):
        self.config['agents']['ticket_budget']=dict(tokens=16); self.save()
        result=self.loop()
        self.assertEqual(25,result.returncode,result.stdout+result.stderr)
        self.assertEqual(2,self.events().count('end'))
        self.assertFalse((self.repo/'publication').exists())
        source=self.scripts/'bureau-ticket.py'; text=source.read_text()
        needle='limit = self.budget[key]'
        self.assertEqual(1,text.count(needle))
        source.write_text(text.replace(needle,'limit = None',1))
        # Keep the real ledger and provider adapters; remove just the run latch
        # for the deliberately broken private runtime's next execution.
        (self.root/'control/stop-25').unlink()
        result=self.loop()
        self.assertEqual(0,result.returncode,result.stdout+result.stderr)
        tokens=sum(row['tokens'] for row in self.ledger()['attempts'].values())
        try: self.assertLessEqual(tokens,16,'looping ticket exceeded its token budget')
        except AssertionError:
            self.assertEqual(64,tokens)
            print('PASS NEGATIVE CONTROL: removing the budget check allowed 64 tokens against a 16-token cap',flush=True)
        else: self.fail('removed budget guard escaped the budget assertion')


if __name__ == '__main__': unittest.main(verbosity=2)
