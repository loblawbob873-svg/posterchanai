"""Opening a launcher app must bring you to THAT app.

Reported: "if I open phone, then go home and choose texts, I see Phone." Nothing threw, the
animation was right, and the wrong app was on screen.

FLAG_ACTIVITY_NEW_TASK FINDS A TASK BY AFFINITY. Phone, Texts and MainActivity all carried the
default affinity, and MainActivity is singleTask — so all three lived in one task, and starting one
while another was on top brought that TASK forward and showed whatever was at the top of it.
HomeActivity already had its own affinity; the other two did not.

This matters more than a cosmetic mis-navigation: a 2FA code arrives in Texts, and an app that shows
you the dialer instead is an app you cannot receive it in.
"""
import os
import re
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MANIFEST = os.path.join(ROOT, "mobile", "android", "app", "src", "main", "AndroidManifest.xml")
HOME = os.path.join(ROOT, "mobile", "android", "app", "src", "main", "java",
                    "place", "poster", "app", "home", "HomeActivity.java")

# Every activity a person can reach as its own "app" — from the launcher's tiles, the drawer, or a
# system role. Each must come forward on its own, so each needs its own task.
LAUNCHABLE = {
    ".sms.ThreadListActivity": "place.poster.app.texts",
    ".phone.DialerActivity": "place.poster.app.phone",
    ".home.HomeActivity": "place.poster.app.home",
}


def _block(xml, name):
    """The <activity> element declaring `name`.

    Found by the attribute rather than by a regex over the whole element: these are formatted across
    many lines and android:name is not always first, which is how the first version of this test
    reported "no <activity> for .MainActivity" about a manifest that plainly declares one."""
    at = xml.index('android:name="%s"' % name)
    start = xml.rindex("<activity", 0, at)
    close = xml.index(">", at)
    if xml[close - 1] == "/":                       # <activity ... />
        return xml[start:close + 1]
    end = xml.index("</activity>", close)
    return xml[start:end + len("</activity>")]


class EachLauncherAppOpensItself(unittest.TestCase):
    def setUp(self):
        self.xml = open(MANIFEST, encoding="utf-8").read()

    def test_every_launchable_activity_has_its_own_task(self):
        seen = {}
        for name, want in LAUNCHABLE.items():
            blk = _block(self.xml, name)
            m = re.search(r'android:taskAffinity="([^"]*)"', blk)
            self.assertIsNotNone(
                m, "%s shares the default task with MainActivity, so launching it just brings that "
                   "task forward and shows whatever was on top" % name)
            self.assertEqual(m.group(1), want, name)
            self.assertNotIn(m.group(1), seen,
                             "%s and %s share a task affinity" % (name, seen.get(m.group(1))))
            seen[m.group(1)] = name

    def test_none_of_them_shares_mainactivitys_task(self):
        """MainActivity is singleTask and owns the default affinity. Anything sharing it is at the
        mercy of whatever that task last showed."""
        main = _block(self.xml, ".MainActivity")
        mine = re.search(r'android:taskAffinity="([^"]*)"', main)
        default = mine.group(1) if mine else ""
        for name in LAUNCHABLE:
            blk = _block(self.xml, name)
            got = re.search(r'android:taskAffinity="([^"]*)"', blk).group(1)
            self.assertNotEqual(got, default, "%s is in MainActivity's task" % name)

    def test_the_launch_brings_the_activity_forward_rather_than_stacking(self):
        """A separate task is not enough on its own: with the activity already somewhere in its
        task, the press has to reach IT, not put a second copy on top of it."""
        src = open(HOME, encoding="utf-8").read()
        start = src[src.index("private void startNative("):]
        start = start[:start.index("\n    }")]
        self.assertIn("FLAG_ACTIVITY_NEW_TASK", start)
        self.assertIn("FLAG_ACTIVITY_CLEAR_TOP", start)
