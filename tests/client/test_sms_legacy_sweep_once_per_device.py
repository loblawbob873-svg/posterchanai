"""Texts' broad (unlabelled) archive sweep runs once per DEVICE, then asks only what changed -- not on every page load.

Measured on server1 2026-10-18: the relay's biggest answer stream was `sub-sms-` [30078|a1|u] -- this sweep, which is
author+kind over the WHOLE kind-30078 datastore -- ~45,000 frames per 5 minutes, in a burst right after every relay
restart: each deploy bumps the service worker, every open client reloads, and the in-memory "swept" flag was gone.
A cleared cache must still get the whole sweep, or old unlabelled messages would never come back.
"""
import shutil
import unittest

from tests.client.test_sms_archive import run, unlabelled_ev

STEPS = ['load', 'settle', 'render', 'settleArchive']


def broad_pages(result):
    return [c[1] for c in result['calls'] if c[0] == 'archivePage' and '#l' not in c[1]]


@unittest.skipIf(shutil.which("node") is None, "node not installed")
class LegacySweepOncePerDevice(unittest.TestCase):

    def test_a_reload_asks_only_what_changed_and_a_cleared_cache_sweeps_again(self):
        first = run(isPhone=False, generatedArchive=700, unlabelledArchive=True, steps=STEPS)
        self.assertTrue(broad_pages(first) and not any('since' in p for p in broad_pages(first)))
        mark = [k for k in first['storage'] if k.startswith('pc_sms_legacy_through_')]
        self.assertTrue(mark, "a complete sweep left no mark on the device")

        held = [unlabelled_ev('pcai:sms:cached0001', {'address': '+15550100', 'date': 100000000, 'body': 'held', 'incoming': True}, at=100000)]
        second = run(isPhone=False, generatedArchive=700, unlabelledArchive=True, steps=STEPS,
                     storage=first['storage'], cached=held)
        pages = broad_pages(second)
        self.assertTrue(pages, "the sweep did not run at all")
        self.assertTrue(all('since' in p for p in pages),
                        "a reload re-swept the whole datastore: %d broad pages without since" %
                        sum('since' not in p for p in pages))

        cleared = run(isPhone=False, generatedArchive=700, unlabelledArchive=True, steps=STEPS,
                      storage=first['storage'])
        self.assertTrue(any('since' not in p for p in broad_pages(cleared)),
                        "a device with no Texts cache must get the whole sweep again")
        self.assertEqual(len(cleared['docs']), 700)
