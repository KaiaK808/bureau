#!/usr/bin/env python3
"""Synchronized real workers exceed two lanes only with the semaphore removed."""
import sys
import time
import unittest
from dispatcher_test import DispatcherFixture


class SemaphoreControl(DispatcherFixture):
    def probe(self, bypass):
        for n in (1,2,3): self.ticket(n,hold=True)
        if bypass:
            file=self.repo/'scripts/bureau-dispatch.py'; text=file.read_text()
            needle='while len(self.lanes) < self.limit and not self.stopping:'
            self.assertEqual(text.count(needle),1)
            file.write_text(text.replace(needle,'while not self.stopping:',1))
        child=self.start()
        self.wait_for(lambda:len(self.started()) >= (3 if bypass else 2))
        time.sleep(.2)
        live=len(self.started())
        self.assertFalse(self.events('provider-end'))
        try:
            self.assertLessEqual(live,2,f'{live} synchronized writers exceeded agents.parallel_lanes=2')
        except AssertionError as error:
            if not bypass: raise
            print('PASS NEGATIVE CONTROL: '+str(error),flush=True)
        else:
            self.assertFalse(bypass,'lane-semaphore bypass escaped the concurrency assertion')
            print('PASS shipped dispatcher: exactly two synchronized writers, third held',flush=True)
        self.stop(child)

    def test_shipped_semaphore(self): self.probe(False)
    def test_private_semaphore_bypass(self): self.probe(True)


suite=unittest.TestSuite(SemaphoreControl(name) for name in
                         ('test_shipped_semaphore','test_private_semaphore_bypass'))
result=unittest.TextTestRunner(verbosity=2).run(suite)
sys.exit(not result.wasSuccessful())
