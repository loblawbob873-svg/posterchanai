"""AN INCOMING CALL HAS TO MAKE A SOUND.

`IN_CALL_SERVICE_RINGING` is the flag by which a default dialer tells telecom who plays the
ringtone. TRUE means THIS APP does. It was true -- on the reasoning, written in the manifest, that
"without RINGING=true the platform keeps its own ringer and ours is never asked", which is exactly
right and exactly the trap: no ringer was ever built. So the platform stayed silent because we told
it to, and an incoming call made no sound at all. Reported as "i just got a phone call but no
sound", which on a phone means a missed call.

Handing it back is also the better answer, not merely the quick one: the platform rings with the
ringtone the OWNER chose and honours Do Not Disturb, the silent switch, per-contact overrides and
escalating vibrate -- every one of which a hand-rolled ringer has to reimplement and usually gets
wrong.

Each check here was verified to fail with its rule removed.
"""
import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import androidcompile as ac  # noqa: E402

MANIFEST = os.path.join(os.path.dirname(ac.JAVA), "AndroidManifest.xml")
PHONE = os.path.join(ac.JAVA, "place", "poster", "app", "phone")


@unittest.skipIf(not os.path.isfile(MANIFEST), "no android manifest here")
class TheRingtoneHasAnOwner(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.man = open(MANIFEST, encoding="utf-8").read()

    def test_the_platform_is_asked_to_ring(self):
        m = re.search(r'IN_CALL_SERVICE_RINGING"\s+android:value="(\w+)"', self.man)
        self.assertIsNotNone(m, "the flag is gone — telecom's behaviour is then undefined for us")
        self.assertEqual(m.group(1), "false",
                         "this app claims the ringtone, and nothing here plays one")

    def test_nothing_in_the_app_claims_to_ring(self):
        """The claim and the capability must agree. If a ringer is ever written, THIS test is the
        one to change -- and only after the ringer exists, not before."""
        if not os.path.isdir(PHONE):
            self.skipTest("no phone package")
        rings = []
        for f in os.listdir(PHONE):
            if not f.endswith(".java"):
                continue
            src = open(os.path.join(PHONE, f), encoding="utf-8").read()
            if "RingtoneManager" in src or "Ringtone " in src:
                rings.append(f)
        m = re.search(r'IN_CALL_SERVICE_RINGING"\s+android:value="(\w+)"', self.man)
        if m and m.group(1) == "true":
            self.assertTrue(rings, "RINGING=true and no file plays a ringtone — the phone is silent")
        else:
            self.assertEqual(rings, [],
                             "a ringer exists but telecom was told to ring: that is two ringtones")

    def test_the_call_notification_does_not_chime_over_the_ringtone(self):
        """A channel's settings are fixed once created, so the id carries a version. The
        notification is the UI for a call, not a second announcement of it."""
        src = open(os.path.join(PHONE, "InCallNotifier.java"), encoding="utf-8").read()
        i = src.index("CHANNEL_RINGING,")
        block = src[i:i + 900]
        self.assertIn("c.setSound(null, null)", block,
                      "the ringing channel plays the default notification chime over the ringtone")
        self.assertIn("enableVibration(true)", block, "the call notification stopped vibrating")

    def test_the_channel_id_changed_with_its_defaults(self):
        src = open(os.path.join(PHONE, "InCallNotifier.java"), encoding="utf-8").read()
        m = re.search(r'CHANNEL_RINGING = "([^"]+)"', src)
        self.assertIsNotNone(m)
        self.assertNotEqual(m.group(1), "pcai_cell_incoming",
                            "the id is the original, so phones keep the old chiming channel")


if __name__ == "__main__":
    unittest.main()
