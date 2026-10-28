#!/usr/bin/env python3
"""Synchronized real workers exceed two lanes only with the semaphore removed."""
import sys
import time
import unittest
from dispatcher_test import DispatcherFixture
from mutation_checks import caught_mutation


class SemaphoreControl(DispatcherFixture):
    def probe(self, bypass):
        self.wait_for(lambda:len(self.started()) >= (3 if bypass else 2))
        time.sleep(.2)
        live=len(self.started())
        self.assertFalse(self.events('provider-end'))
        self.assertLessEqual(live,2,f'{live} synchronized writers exceeded agents.parallel_lanes=2')

    def test_shipped_semaphore(self):
        for n in (1,2,3): self.ticket(n,hold=True)
        child=self.start()
        try:
            self.probe(False)
        finally:
            self.stop(child)
        print('PASS shipped dispatcher: exactly two synchronized writers, third held',flush=True)

    def test_private_semaphore_bypass(self):
        for n in (1,2,3): self.ticket(n,hold=True)
        file=self.repo/'scripts/bureau-dispatch.py'; text=file.read_text()
        needle='while len(self.lanes) < self.limit and not self.stopping:'
        self.assertEqual(text.count(needle),1,'semaphore mutation anchor changed')
        file.write_text(text.replace(needle,'while not self.stopping:',1))
        child=self.start()
        try:
            with caught_mutation() as caught:
                self.probe(True)
        finally:
            self.stop(child)
        error=caught[0]
        print('PASS NEGATIVE CONTROL: '+(str(error) or type(error).__name__),flush=True)


if __name__ == '__main__':
    suite=unittest.TestSuite(SemaphoreControl(name) for name in
                             ('test_shipped_semaphore','test_private_semaphore_bypass'))
    result=unittest.TextTestRunner(verbosity=2).run(suite)
    sys.exit(not result.wasSuccessful())
