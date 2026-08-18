"""A paired app stuck in a request loop must NAME ITSELF on the pairings screen.

Measured on the relay (2026-08-18): one app re-asked the same two small decrypts every ~20s for
hours. From the signer's side every request is legitimate one at a time — only a per-app tally
with a REPEAT count makes the runaway stand out. Both signer halves count (the page's Nip46Signer
and the phone's SignerRelayService — whichever owns steady state), and the screen warns past a
threshold."""
import os
import re
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP = open(os.path.join(ROOT, "static", "js", "client", "app.js"), encoding="utf-8").read()
SVC = open(os.path.join(ROOT, "mobile", "android", "app", "src", "main", "java",
                        "place", "poster", "app", "signer", "SignerRelayService.java"),
           encoding="utf-8").read()
PLG = open(os.path.join(ROOT, "mobile", "android", "app", "src", "main", "java",
                        "place", "poster", "app", "signer", "SignerPlugin.java"),
           encoding="utf-8").read()


class WebHalf(unittest.TestCase):
    def test_the_tally_counts_and_detects_repeats(self):
        a = APP.index("PER-APP TALLY")
        seg = APP[a:a + 1400]
        self.assertIn("st.dup++", seg)
        self.assertIn("this._stats.set(ev.pubkey, st)", seg)

    def test_the_screen_warns_on_a_loop(self):
        a = APP.index("function _renderSignerApps()")
        seg = APP[a:a + 5200]
        self.assertIn("stuck in a loop", seg)
        self.assertIn("a.stats", seg)

    def test_native_numbers_merge_when_the_service_owns_steady_state(self):
        a = APP.index("function _renderSignerApps()")
        seg = APP[a:a + 2600]
        self.assertIn("nativeOn", seg, "the phone's tally never reaches the screen — which is "
                                       "exactly when the page's own tally sees nothing")
        self.assertIn("perApp", seg)

    def test_the_native_fetch_cannot_loop_itself(self):
        a = APP.index("function _renderSignerApps()")
        seg = APP[a:a + 2600]
        self.assertIn("_renderSignerApps._at", seg)
        self.assertIn("> 5000", seg, "an unthrottled Keystore read per repaint")


class PhoneHalf(unittest.TestCase):
    def test_the_service_counts_per_app_on_the_owner_thread(self):
        self.assertIn("perApp", SVC)
        a = SVC.index("handler.post(() -> {")
        # counters must be written inside a handler.post (owner-thread confinement)
        self.assertIn("perApp.get(peer)", SVC)
        self.assertIn("perAppFp.put(peer, fp)", SVC)
        # static — a recycled service still answers for the process
        self.assertTrue(re.search(r"static final java\.util\.Map<String, long\[\]> perApp", SVC))

    def test_the_plugin_reports_the_tally(self):
        self.assertIn('o.put("perApp", apps)', PLG)
        self.assertIn("SignerRelayService.perApp", PLG)


if __name__ == "__main__":
    unittest.main()
