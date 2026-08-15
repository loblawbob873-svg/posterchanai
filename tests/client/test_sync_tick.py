"""Does a backgrounded Android app actually sweep? — the shipped sync.js, driven under node.

Run: venv-unified/bin/python -m unittest tests.client.test_sync_tick

Reported as "syncing stops every time the screen goes off", with "Stay connected" already on. Three
facts had to line up for that, and no single file shows them:

  * fs-android.js has NO watcher. `watch()` answers false and `onChanged()` is empty, because SAF
    exposes no tree notification worth having and polling one is the battery bug the whole sync
    policy exists to avoid. So nothing filesystem-side ever asks for a sweep on a phone.
  * That leaves one automatic trigger, a JS `setInterval` — and Android throttles timers in a hidden
    WebView, so with the screen off it effectively never fires.
  * `nudge()` then refuses anyway while `document.hidden`, unless `_keptAlive`, which is read from a
    plugin call that can throw on exactly the platform where it matters.

So the clock moved native: StayAwakeService arms an `AlarmManager.setAndAllowWhileIdle` alarm — and
NOT a Handler, whose delays are measured on `uptimeMillis()` and stop advancing in deep sleep, which
would fire only when something else happened to wake the phone — and each firing arrives here as a
`folderSyncTick` event. (That half is guarded in tests/test_android_folder_sync.py, since Android
only builds on CI.) `sync_tick_sim.js` loads the REAL sync.js into a screen-off world and asserts a
sweep happens — the assertion is that the platform adapter's `scan()` was called, because that is the
first thing a sweep does and the trigger alone cannot fake it.

Every scenario was verified to FAIL against the pre-fix states: no subscription at all, a
subscription not forced past the idle test, and the force flag held per-call so that coalescing
dropped it.
"""
import json
import os
import shutil
import subprocess
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SIM = os.path.join(HERE, "sync_tick_sim.js")
NODE = shutil.which("node") or shutil.which("nodejs")


@unittest.skipIf(not NODE, "no node on this node")
class TestSyncTick(unittest.TestCase):
    _rows = None

    @classmethod
    def setUpClass(cls):
        r = subprocess.run([NODE, SIM], capture_output=True, text=True, timeout=300)
        if not r.stdout.strip():
            raise AssertionError("the simulation produced nothing:\n" + r.stderr[-2000:])
        try:
            rows = json.loads(r.stdout)
        except json.JSONDecodeError:
            raise AssertionError("simulation crashed:\n" + r.stdout[-1500:] + "\n" + r.stderr[-1500:])
        cls._rows = {row["name"]: row for row in rows}

    def check(self, name):
        self.assertIn(name, self._rows,
                      "scenario missing from the simulation: %r (have %s)" % (name, list(self._rows)))
        row = self._rows[name]
        self.assertTrue(row["ok"], "%s: %s" % (name, row["detail"]))

    def test_a_native_tick_sweeps_with_the_screen_off(self):
        """The one this file exists for."""
        self.check("a native tick sweeps a folder with the screen off")

    def test_the_bug_is_reproduced_without_the_tick(self):
        """PROOF THE TEST ABOVE IS NOT VACUOUS. Without a native tick, a hidden app given everything
        it really has — including its JS heartbeat, run by hand, which is more than a throttled
        WebView would grant it — sweeps nothing at all."""
        self.check("without the tick, a hidden app sweeps nothing (the bug, reproduced)")

    def test_an_older_apk_still_starts(self):
        """The bridge has no onTick on an APK built before the plugin gained one. That must be
        today's behaviour, not a throw that takes folder sync out entirely."""
        self.check("an APK with no onTick still starts, and does not throw")

    def test_an_unforced_nudge_cannot_cancel_a_forced_one(self):
        """nudge() coalesces into ONE timer, and the force flag used to belong to the call that
        scheduled it — so an unforced trigger arriving inside the 1500ms window replaced a forced one
        and the flag was gone. Correlated, not rare: the phone wakes, the pending alarm fires forced,
        and the reconnecting radio raises `online` milliseconds later. The sweep is then skipped for
        another whole alarm period, with the screen off, which is the bug this whole file is about."""
        self.check("an unforced nudge arriving right after a tick cannot cancel it")

    def test_the_tick_does_not_bypass_the_policy(self):
        """It skips the "is anybody looking" test and NOTHING else. On battery or on cellular,
        `shouldSync` still declines — which is what the "only when plugged in" and "Wi-Fi only"
        switches mean, and they must keep meaning it when the trigger is native."""
        self.check("the tick does not bypass the battery and network policy")

    def test_a_paused_folder_is_not_started_by_a_tick(self):
        """A folder nobody has pressed Start on has never been asked to sync at all, and a
        background clock is the worst possible thing to have start its first sweep."""
        self.check("a paused folder is still not started by a tick")


if __name__ == "__main__":
    unittest.main()
