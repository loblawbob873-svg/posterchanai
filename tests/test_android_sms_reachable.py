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


class TheWebViewScreenAsksToo(unittest.TestCase):
    """THE SAME BUG ON THE OTHER HALF, AND IT OUTLIVED THE FIRST FIX.

    "still missing a nice sms app on android".

    The native ThreadListActivity was taught to request READ_SMS; the in-app Texts view — which is
    the screen almost everybody actually opens, because it does not need this app to be the phone's
    launcher — was not. The commit that fixed the native side said in as many words that "SmsPlugin
    requests it for the WebView", and it did not: `@CapacitorPlugin(permissions = ...)` names the
    permissions an alias covers, and asking is `requestPermissionForAlias`, which nothing called.

    So a person who made PosterChan their SMS app still saw an empty Texts screen, under a sentence
    blaming a role they had already granted.
    """

    def test_the_plugin_declares_the_permission_and_also_asks_for_it(self):
        src = strip_comments((SMS / "SmsPlugin.java").read_text())
        self.assertIn("android.permission.READ_SMS", src)
        self.assertIn('requestPermissionForAlias("sms"', src,
                      "SmsPlugin declares the sms permission alias and never requests it — a "
                      "dangerous permission is not granted by being declared")
        self.assertIn("@com.getcapacitor.annotation.PermissionCallback", src,
                      "a request with no callback resolves nothing and the caller waits for ever")

    def test_the_client_can_tell_the_two_switches_apart(self):
        """`isDefault` is whether messages ARRIVE here; `canRead` is whether we may look. They are
        separate grants and were reported as one, which is how reading ended up gated on the role."""
        body = method(strip_comments((SMS / "SmsPlugin.java").read_text()), "public void status")
        self.assertIn('o.put("canRead"', body)
        self.assertIn('o.put("isDefault"', body)

    def test_the_texts_view_asks_before_it_reports_an_empty_inbox(self):
        js = (ROOT / "static/js/client/sms.js").read_text()
        self.assertIn("ensureRead", js,
                      "the Texts view never asks for permission, so the provider refuses and the "
                      "empty list is drawn as 'No messages on this phone'")
        self.assertIn("fix: 'perm'", js,
                      "the one kind of empty a tap can fix is not named")


class ThePhoneCanSayWhatItMeasured(unittest.TestCase):
    """"posterchan still not working as default Messenger app despite being set as default
    messenger" — four rounds, no device here, and from the build side the failure REPORTS SUCCESS:
    the role is set, the screen draws, nothing throws.

    So the screen prints what was ASKED and what came BACK. The same answer as the music panel's
    counters and the /logs board's measured rows: a report that cannot be reproduced is answered by
    making the phone say what it saw, not by another round of guessing.
    """

    def setUp(self):
        self.src = strip_comments((SMS / "SmsPlugin.java").read_text())

    def test_the_plugin_reports_every_leg(self):
        body = method(self.src, "public void diagnose")
        for k in ("defaultPackage", "roleHeld", "canRead", "components", "read", "refused"):
            self.assertIn(k, body, "diagnose() does not report %s" % k)

    def test_it_checks_all_four_components_android_demands(self):
        """An app missing one never appears in the role picker, and a role "granted" to it does
        nothing — which is indistinguishable from a role that was granted and is not working."""
        body = method(self.src, "public void diagnose")
        for k in ("smsDeliver", "mmsDeliver", "sendTo", "respondViaMessage"):
            self.assertIn(k, body, k)

    def test_the_screen_can_show_it(self):
        js = (ROOT / "static/js/client/sms.js").read_text()
        self.assertIn("diagnose", js, "nothing in the client ever asks for it")
        self.assertIn("sms-why", js, "there is no way for a person to see it")
        for k in ("roleHeld", "canRead", "refused"):
            self.assertIn(k, js, "the panel drops %s" % k)


class ANewTextCanActuallyBeANNOUNCED(unittest.TestCase):
    """"make sure notifications work on new text messages" ... "otherwise useless".

    On Android 13+ POST_NOTIFICATIONS is a runtime grant and `NotificationManager.notify` does
    NOTHING without it — no error, no log, the message correctly stored and the screen correctly
    drawn. Music, screen sharing and push each declare AND request it for their own flows; the
    messages half declared nothing and asked nobody, so a person who had never opened the player and
    never turned push on had never been asked, and every incoming text arrived in silence.

    Its own alias, not folded in with the SMS three: being unable to READ texts and being unable to
    ANNOUNCE one are different failures with different fixes, and a refusal of one must not be
    readable as a refusal of the other.
    """

    def setUp(self):
        self.src = strip_comments((SMS / "SmsPlugin.java").read_text())

    def test_the_plugin_declares_it_separately(self):
        self.assertIn("POST_NOTIFICATIONS", self.src,
                      "the messages plugin never declares the permission its notifications need")
        self.assertIn('alias = "notify"', self.src,
                      "POST_NOTIFICATIONS shares an alias with the SMS permissions, so a refusal of "
                      "one reads as a refusal of the other")

    def test_it_asks_for_it(self):
        body = method(self.src, "public void ensureNotify")
        self.assertIn('requestPermissionForAlias("notify"', body,
                      "declaring it grants nothing — a dangerous permission has to be requested")

    def test_a_muted_channel_counts_as_no(self):
        """Android granting it and the person switching it off are both "no notifications", and the
        screen must not report success for the second."""
        self.assertIn("areNotificationsEnabled", method(self.src, "private boolean mayNotify"))

    def test_the_client_asks_and_reports(self):
        js = (ROOT / "static/js/client/sms.js").read_text()
        self.assertIn("ensureNotify", js, "nothing in the client ever asks")
        self.assertIn("canNotify", js, "the panel cannot say whether a text can be announced")


class TheNativeScreenNamesWhatAndroidNamed(unittest.TestCase):
    """"1.0.1336 says PosterChan is not this phone's messages app still!"

    A flat verdict is unanswerable. A role that was never granted, a role granted in another profile,
    and the two platform tables disagreeing all produce the identical sentence, and none of them
    tells the person anything they can act on. Android keeps the SMS ROLE and the message store's
    default-app row separately on 10+, and OEM builds do not always keep them in step; the STORE's
    row is the one that decides what is delivered, so the app must not simply believe the role — but
    it can say which one says what, and name the package. The web Texts panel already did; the
    native Messages screen, which is the one most likely to be open, did not.
    """

    def setUp(self):
        self.src = strip_comments((SMS / "ThreadListActivity.java").read_text())

    def test_the_notice_is_measured_rather_than_asserted(self):
        body = method(self.src, "private String whyNotDefault")
        self.assertIn("getDefaultSmsPackage", body,
                      "the notice never asks who Android actually names")
        self.assertIn("roleHeld", body,
                      "it cannot tell a role that was never granted from one the message store "
                      "disagrees with")

    def test_the_three_answers_are_three_sentences(self):
        body = method(self.src, "private String whyNotDefault")
        for k in ("sms_default_none", "sms_role_split", "sms_default_is"):
            self.assertIn(k, body, k)

    def test_every_one_of_them_exists(self):
        x = (MAIN / "res/values/strings.xml").read_text()
        for k in ("sms_default_none", "sms_role_split", "sms_default_is"):
            self.assertIn('name="%s"' % k, x)
        # The two that name a package must have somewhere to put it.
        for k in ("sms_role_split", "sms_default_is"):
            row = [l for l in x.splitlines() if 'name="%s"' % k in l][0]
            self.assertIn("%1$s", row, "%s names no package, so it says nothing new" % k)

    def test_the_screen_uses_it(self):
        body = method(self.src, "private void draw")
        self.assertIn("whyNotDefault()", body)


class TheDrawerStillHidesOurOwnPackage(unittest.TestCase):
    """The filter that made the tiles load-bearing. If this ever goes away the catalogue tile and the
    alias would BOTH be listed — one app, two Messages entries."""

    def test_installed_skips_our_package(self):
        body = method(strip_comments((JAVA / "home/AppRepo.java").read_text()),
                      "public List<AppShelf.Entry> installed")
        self.assertIn("self.equals(pkg)", body)


if __name__ == "__main__":
    unittest.main()
