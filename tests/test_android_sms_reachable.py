"""There is a way to open Messages, and an empty list says which kind of empty it is.

    "still no SMS app"

The alias was in the manifest, exported, enabled, with MAIN/LAUNCHER — so it appeared in any OTHER
launcher, which is what made this look fixed. On OUR launcher it was unreachable, because two
individually reasonable filters met:

  * `AppRepo.installed()` skips our own package, so PosterChan is not listed forty times — which
    also drops `.sms.Messages` and `.phone.Phone`;
  * `HomeTiles.ours(dialer, sms)` withheld the Messages and Phone tiles until the app held the
    default SMS / dialer role.

Between them there was no path from our home screen to the messages app, and the role is normally
granted by opening the app and being asked. The app that asks was behind the role it was asking for.

Underneath that sat the reason the screen looked broken even when reached: nothing ever requested
READ_SMS at runtime for the NATIVE screen (SmsPlugin requests it for the WebView), the provider
refused, `SmsStore.query` swallowed the refusal into an empty list, and "No messages yet" was drawn
over a full inbox — "i see 0 of my sms messages in Text". A refusal and an empty inbox are not the
same sentence, and only one of them is fixable by the person reading it.
"""
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from tests.test_android_launch_view import method, strip_comments

ROOT = Path(__file__).resolve().parents[1]
MAIN = ROOT / "mobile/android/app/src/main"
JAVA = MAIN / "java/place/poster/app"
SMS = JAVA / "sms"

HAVE_JDK = shutil.which("javac") and shutil.which("java")

HARNESS = r"""
import java.util.*;
import place.poster.app.home.HomeTiles;

public class Harness {
    static int failed = 0;
    static void ok(String what, boolean cond) {
        if (!cond) { failed++; System.out.println("FAIL " + what); }
        else System.out.println("ok   " + what);
    }

    public static void main(String[] a) {
        Set<String> views = new HashSet<String>();
        for (place.poster.app.home.AppShelf.Entry e : HomeTiles.ours()) views.add(e.key());

        // The dead end: with no role held, these were both absent and nothing else offered them.
        ok("Messages is offered", views.contains("pc:" + HomeTiles.VIEW_TEXTS));
        ok("Phone is offered", views.contains("pc:" + HomeTiles.VIEW_PHONE));
        ok("the way back is still there", views.contains("pc:" + HomeTiles.VIEW_SETTINGS));

        // Messages must not START hidden, or it is offered and invisible, which is the same report.
        Set<String> hidden = HomeTiles.defaultHidden();
        ok("Messages is not hidden on a fresh phone", !hidden.contains("pc:" + HomeTiles.VIEW_TEXTS));
        ok("Phone is not hidden on a fresh phone", !hidden.contains("pc:" + HomeTiles.VIEW_PHONE));

        // And it must open the native screen, not a WebView view that does not exist.
        ok("Messages opens the native thread list",
           "place.poster.app.sms.ThreadListActivity".equals(
               HomeTiles.nativeTarget(HomeTiles.VIEW_TEXTS)));

        System.out.println(failed == 0 ? "ALL OK" : (failed + " FAILED"));
        if (failed != 0) System.exit(1);
    }
}
"""


@unittest.skipUnless(HAVE_JDK, "javac/java not installed")
class MessagesIsReachable(unittest.TestCase):
    def test_the_catalogue_offers_it_without_the_role(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            pkg = d / "src/place/poster/app/home"
            pkg.mkdir(parents=True)
            for f in ("HomeTiles.java", "AppShelf.java"):
                shutil.copy(JAVA / "home" / f, pkg / f)
            (d / "Harness.java").write_text(HARNESS)
            c = subprocess.run(["javac", "-nowarn", "-d", str(d),
                                str(pkg / "HomeTiles.java"), str(pkg / "AppShelf.java"),
                                str(d / "Harness.java")],
                               capture_output=True, text=True, cwd=d)
            self.assertEqual(c.returncode, 0, c.stderr)
            r = subprocess.run(["java", "-cp", str(d), "Harness"],
                               capture_output=True, text=True, cwd=d)
            self.assertIn("ALL OK", r.stdout, r.stdout + r.stderr)


class TheAliasIsStillAnOrdinaryApp(unittest.TestCase):
    """The other half of reachable: it must show up in a THIRD-PARTY launcher too, since the HOME
    role is opt-in and most people keep their own home screen."""

    def setUp(self):
        self.man = (MAIN / "AndroidManifest.xml").read_text()

    def _alias(self, name):
        i = self.man.index('android:name="%s"' % name)
        start = self.man.rindex("<activity-alias", 0, i)
        return self.man[start:self.man.index("</activity-alias>", i)]

    def test_messages_is_a_launcher_entry(self):
        block = self._alias(".sms.Messages")
        self.assertIn("android.intent.category.LAUNCHER", block)
        self.assertIn('android:exported="true"', block)

    def test_it_is_not_shipped_disabled(self):
        """The HOME alias ships `enabled=false` on purpose. Messages must not — nothing enables it."""
        self.assertNotIn('android:enabled="false"', self._alias(".sms.Messages"))

    def test_it_has_its_own_name_and_glyph(self):
        """Three drawer entries all reading PosterChan is the same report as the letter tiles."""
        block = self._alias(".sms.Messages")
        self.assertIn("@string/sms_title", block)
        self.assertIn("ic_launcher_messages", block)


class ARefusalIsNotAnEmptyInbox(unittest.TestCase):
    def test_the_store_reports_that_it_was_refused(self):
        src = strip_comments((SMS / "SmsStore.java").read_text())
        self.assertIn("public static boolean refused()", src,
                      "SmsStore cannot tell a caller that it was refused, so an empty list means "
                      "both 'no texts' and 'not allowed to look'")

    def test_every_read_sets_it(self):
        """A flag that is only ever set to true latches: one refusal and the screen claims a
        permission problem for the rest of the session."""
        body = method(strip_comments((SMS / "SmsStore.java").read_text()),
                      "private static List<SmsMsg> query")
        self.assertIn("refused = false;", body)
        self.assertIn("refused = true;", body)
        self.assertLess(body.index("refused = false;"), body.index("refused = true;"),
                        "the reset must happen before the query, not after it")

    def test_the_screen_asks_for_the_permission(self):
        src = strip_comments((SMS / "ThreadListActivity.java").read_text())
        self.assertIn("requestPermissions(", src,
                      "the native Messages screen never asks for READ_SMS — declaring a dangerous "
                      "permission in the manifest does not grant it on any phone this runs on")
        self.assertIn("READ_SMS", src)

    def test_a_refusal_redraws_the_screen(self):
        """Declining must change what is on screen, or it looks exactly like an empty inbox."""
        body = method(strip_comments((SMS / "ThreadListActivity.java").read_text()),
                      "public void onRequestPermissionsResult")
        self.assertIn("reload()", body)

    def test_the_empty_text_says_which_kind_of_empty(self):
        body = method(strip_comments((SMS / "ThreadListActivity.java").read_text()),
                      "private void draw")
        self.assertIn("sms_no_permission", body)
        self.assertIn("sms_empty", body)

    def test_the_refusal_is_read_next_to_its_own_query(self):
        """`refused` describes the LAST read. Read on the main thread later, a second reload could
        have overwritten it."""
        body = method(strip_comments((SMS / "ThreadListActivity.java").read_text()),
                      "private void reload")
        self.assertIn("SmsStore.refused()", body)
        self.assertLess(body.index("SmsStore.refused()"), body.index("main.post("))

    def test_the_string_exists(self):
        x = (MAIN / "res/values/strings.xml").read_text()
        self.assertIn('name="sms_no_permission"', x)
        self.assertIn('name="sms_empty"', x)


class TheDrawerStillHidesOurOwnPackage(unittest.TestCase):
    """The filter that made the tiles load-bearing. If this ever goes away the catalogue tile and the
    alias would BOTH be listed — one app, two Messages entries."""

    def test_installed_skips_our_package(self):
        body = method(strip_comments((JAVA / "home/AppRepo.java").read_text()),
                      "public List<AppShelf.Entry> installed")
        self.assertIn("self.equals(pkg)", body)


if __name__ == "__main__":
    unittest.main()
