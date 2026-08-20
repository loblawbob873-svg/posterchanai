"""THE HOME SCREEN'S DECISIONS, RUN — not grepped.

A launcher that fails takes the phone's home screen with it. There is no second home screen to fall
back to and no way to reach Settings without knowing a hardware key sequence, so the rules that
decide what is on the grid are written in a class with no Android in it (AppShelf, HomeTiles,
LauncherPrefs) and this file COMPILES AND RUNS them under plain javac.

Every assertion below was checked against the pre-fix behaviour: comment out the rule and the test
fails. That matters more here than anywhere else in this repo — the failure mode is not a bad screen,
it is a phone somebody cannot use.
"""
import glob
import json
import re
import os
import shutil
import subprocess
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
JAVA = os.path.join(ROOT, "mobile", "android", "app", "src", "main", "java")
HOME = os.path.join(JAVA, "place", "poster", "app", "home")
STUBS = os.path.join(ROOT, "tests", "androidstubs")
JAVAC = shutil.which("javac")
JAVARUN = shutil.which("java")

# The pure half of the package. HomeActivity / AppRepo / HomeRoles / HomePlugin need the real SDK and
# are compiled by CI's `assembleDebug` (.github/workflows/android-emulator.yml) and exercised on a
# real device by mobile/android/app/src/androidTest — a stub of RoleManager or the package manager
# would be a stub of exactly the thing being tested.
PURE = ["AppShelf.java", "HomeTiles.java", "LauncherPrefs.java"]

def _code(src):
    """The Java with its comments removed.

    These two tests are about what the file DOES, and every one of these classes explains at length
    why it must not touch a WebView — so a naive substring search fails on the very comment that
    documents the rule. Stripping comments is what makes the assertion mean 'no code does this'."""
    import re as _re
    src = _re.sub(r"/\*.*?\*/", " ", src, flags=_re.S)
    return _re.sub(r"//[^\n]*", " ", src)


HARNESS = r"""
import java.util.*;
import place.poster.app.home.*;

public class Harness {
  static AppShelf.Entry app(String pkg, String label) {
    return AppShelf.Entry.app(pkg, pkg + ".Main", label);
  }
  static List<String> keys(List<AppShelf.Entry> rows) {
    List<String> out = new ArrayList<String>();
    for (AppShelf.Entry e : rows) out.add(e.key());
    return out;
  }
  static void say(String name, Object v) { System.out.println(name + "\t" + v); }

  public static void main(String[] a) {
    List<AppShelf.Entry> phone = new ArrayList<AppShelf.Entry>();
    phone.add(app("com.android.settings", "Settings"));
    phone.add(app("com.android.chrome", "Chrome"));
    phone.add(app("org.thoughtcrime.securesms", "Signal"));
    phone.add(app("com.zebra.app", "Zebra"));
    List<AppShelf.Entry> ours = HomeTiles.ours(true, true);

    // 1. The essential tile survives every filter there is.
    Set<String> hideEverything = new HashSet<String>();
    for (AppShelf.Entry e : phone) hideEverything.add(e.key());
    for (AppShelf.Entry e : ours) hideEverything.add(e.key());
    say("hide-all", keys(AppShelf.arrange(phone, ours, hideEverything, null, "")));

    // 2. Hiding every PHONE app but leaving ours is still an emptied grid, and is ignored wholesale.
    Set<String> hidePhone = new HashSet<String>();
    for (AppShelf.Entry e : phone) hidePhone.add(e.key());
    say("hide-phone", keys(AppShelf.arrange(phone, ours, hidePhone, null, "")));

    // 3. Hiding SOME apps is obeyed.
    Set<String> hideOne = new HashSet<String>();
    hideOne.add("com.android.chrome/com.android.chrome.Main");
    say("hide-one", keys(AppShelf.arrange(phone, ours, hideOne, null, "")));

    // 4. A search sees a hidden app.
    say("search-hidden", keys(AppShelf.arrange(phone, ours, hideOne, null, "chrome")));

    // 5. Search by package name, for an app whose label is unreadable here.
    say("search-pkg", keys(AppShelf.arrange(phone, ours, null, null, "securesms")));

    // 6. The saved order leads, the rest follow alphabetically.
    List<String> order = new ArrayList<String>();
    order.add("com.zebra.app/com.zebra.app.Main");
    order.add("pc:_settings");
    say("ordered", keys(AppShelf.arrange(phone, ours, null, order, "")));

    // 7. TOTAL order: two apps with the same label must not swap places between draws.
    List<AppShelf.Entry> same = new ArrayList<AppShelf.Entry>();
    same.add(app("b.pkg", "Same"));
    same.add(app("a.pkg", "Same"));
    List<String> once = keys(AppShelf.arrange(same, new ArrayList<AppShelf.Entry>(), null, null, ""));
    Collections.reverse(same);
    List<String> twice = keys(AppShelf.arrange(same, new ArrayList<AppShelf.Entry>(), null, null, ""));
    say("stable", once.equals(twice) + " " + once);

    // 8. hide() refuses an essential entry.
    AppShelf.Entry ess = null, plain = null;
    for (AppShelf.Entry e : ours) { if (e.essential) ess = e; else if (plain == null) plain = e; }
    say("hide-essential", AppShelf.hide(new HashSet<String>(), ess));
    say("hide-plain", AppShelf.hide(new HashSet<String>(), plain).contains(plain.key()));

    // 9. Pinning: the key leads, it is never duplicated, and unpinning puts it back in the crowd.
    List<String> k = new ArrayList<String>(Arrays.asList("a", "b", "c"));
    say("pin", AppShelf.pin(k, "c") + " " + AppShelf.pin(AppShelf.pin(k, "c"), "c")
             + " " + AppShelf.unpin(AppShelf.pin(k, "c"), "c")
             + " " + AppShelf.pinned(k, "b") + " " + AppShelf.pinned(k, "z"));
    say("pin-empty", AppShelf.pin(null, "a") + " " + AppShelf.unpin(null, "a"));

    // 10. defaultHidden never hides the essential tile, and never hides a default-on tile.
    say("seed", new TreeSet<String>(HomeTiles.defaultHidden()));

    // 11. The dialer/messages tiles are only offered when the role is actually held.
    say("no-roles", keys(AppShelf.arrange(new ArrayList<AppShelf.Entry>(), HomeTiles.ours(false, false), null, null, "")));

    // 12. An empty phone (the package query failed) still leaves a way to Settings.
    say("no-apps", keys(AppShelf.arrange(new ArrayList<AppShelf.Entry>(), ours, null, null, "")));

    // 13. Our own tile and a phone app publishing the same key never double up.
    List<AppShelf.Entry> dup = new ArrayList<AppShelf.Entry>(phone);
    dup.add(AppShelf.Entry.ours("notes", "Notes (impostor)", false));
    say("dedupe", keys(AppShelf.arrange(dup, ours, null, null, "notes")));
  }
}
"""


@unittest.skipIf(not JAVAC or not JAVARUN, "no JDK on this node")
@unittest.skipIf(not os.path.isdir(HOME), "no android sources here")
class Launcher(unittest.TestCase):
    out = None

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        src = [os.path.join(HOME, f) for f in PURE]
        harness = os.path.join(cls.tmp, "Harness.java")
        with open(harness, "w") as f:
            f.write(HARNESS)
        r = subprocess.run([JAVAC, "-nowarn", "-d", cls.tmp,
                            "-sourcepath", STUBS + os.pathsep + JAVA] + src + [harness],
                           capture_output=True, text=True, timeout=300)
        assert r.returncode == 0, r.stderr[-4000:]
        r = subprocess.run([JAVARUN, "-cp", cls.tmp, "Harness"],
                           capture_output=True, text=True, timeout=120)
        assert r.returncode == 0, r.stderr[-4000:]
        cls.out = {}
        for line in r.stdout.splitlines():
            if "\t" in line:
                k, v = line.split("\t", 1)
                cls.out[k] = v

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_the_way_back_survives_hiding_everything(self):
        """PHONE SETTINGS IS THE ESCAPE HATCH and nothing may remove it.

        It opens the system Settings app by intent action, which works when the package query
        returned nothing, when the arrangement is corrupt and when this app's own UI will not start.
        Without it a bad build is a phone whose owner cannot change their home screen back."""
        self.assertIn("pc:_settings", self.out["hide-all"])

    def test_an_arrangement_that_would_empty_the_grid_is_ignored(self):
        """A hidden set covering every phone app is a broken arrangement, not an instruction.

        'Nothing' counts our own tiles as nothing on purpose: a grid of PosterChan screens and no
        phone apps is the kiosk this launcher must never become."""
        rows = self.out["hide-phone"]
        self.assertIn("com.android.chrome", rows)
        self.assertIn("com.android.settings", rows)

    def test_hiding_some_apps_still_works(self):
        rows = self.out["hide-one"]
        self.assertNotIn("com.android.chrome", rows)
        self.assertIn("com.android.settings", rows)

    def test_a_search_always_finds_a_hidden_app(self):
        """Hiding is about the grid, never about reachability — otherwise it is a one-way door that
        needs another launcher to undo."""
        self.assertIn("com.android.chrome", self.out["search-hidden"])

    def test_search_matches_the_package_name_too(self):
        """How you find an app whose label is in a script you cannot type."""
        self.assertIn("securesms", self.out["search-pkg"])

    def test_the_saved_order_leads(self):
        rows = json.loads("[" + ",".join(
            '"%s"' % x for x in self.out["ordered"].strip("[]").split(", ")) + "]")
        self.assertEqual(rows[0], "com.zebra.app/com.zebra.app.Main")
        self.assertEqual(rows[1], "pc:_settings")

    def test_the_order_is_total(self):
        """A comparator that calls two tiles equal reshuffles them on every redraw, which on a home
        screen reads as icons that will not stay put."""
        self.assertTrue(self.out["stable"].startswith("true "), self.out["stable"])

    def test_an_essential_tile_cannot_be_hidden(self):
        self.assertEqual(self.out["hide-essential"], "[]")
        self.assertEqual(self.out["hide-plain"], "true")

    def test_pinning_puts_a_tile_first_and_never_twice(self):
        """The saved order is a short list of keys that LEAD; everything else follows
        alphabetically. Pinning the same tile twice must not put it in the list twice, or the
        arrangement grows every time somebody presses the menu item."""
        self.assertEqual(self.out["pin"], "[c, a, b] [c, a, b] [a, b] true false")

    def test_pinning_an_unarranged_home_screen_is_not_an_error(self):
        self.assertEqual(self.out["pin-empty"], "[a] []")

    def test_the_first_run_seed_never_hides_the_essential_tile(self):
        seed = self.out["seed"]
        self.assertNotIn("pc:_settings", seed)
        self.assertNotIn("pc:global", seed)      # a default-on tile
        self.assertIn("pc:chess", seed)          # a catalogue tile that starts hidden

    def test_phone_and_messages_are_only_offered_once_the_role_is_held(self):
        """Offered before that they are two tiles that open an empty call log."""
        rows = self.out["no-roles"]
        self.assertNotIn("pc:_phone", rows)
        self.assertNotIn("pc:_texts", rows)
        self.assertIn("pc:_settings", rows)

    def test_a_phone_with_no_readable_app_list_still_reaches_settings(self):
        self.assertIn("pc:_settings", self.out["no-apps"])

    def test_one_tile_per_key(self):
        self.assertEqual(self.out["dedupe"].count("pc:notes"), 1)


@unittest.skipIf(not os.path.isdir(HOME), "no android sources here")
class LauncherSources(unittest.TestCase):
    def test_the_home_screen_never_touches_the_webview(self):
        """THE WHOLE SAFETY ARGUMENT, ASSERTED.

        A launcher that fails takes the phone's home screen with it, and this app's WebView renderer
        is measured to die under memory pressure (MainActivity.surviveRenderProcessDeath). So the home
        screen is a plain Activity that draws views: no BridgeActivity, no WebView, no Capacitor
        bridge. A refactor that "tidies" HomeActivity into the app's usual base class would undo that
        silently — the launcher would look identical and would die with the renderer."""
        src = _code(open(os.path.join(HOME, "HomeActivity.java")).read())
        self.assertIn("extends Activity", src)
        for banned in ("BridgeActivity", "WebView", "getBridge(", "com.getcapacitor"):
            self.assertNotIn(banned, src, "the home screen must not depend on " + banned)

    def test_nothing_in_the_launcher_polls(self):
        """BATTERY, AS A RULE RATHER THAN AN INTENTION.

        With the HOME role this process is foreground whenever nothing else is — resident for the
        life of the battery — so anything it polls, it polls for ever. The app list is re-read only
        on a PACKAGE_* broadcast and now-playing only when MusicService pushes."""
        for f in glob.glob(os.path.join(HOME, "*.java")):
            src = _code(open(f).read())
            name = os.path.basename(f)
            for banned in ("scheduleAtFixedRate", "setRepeating", "setInexactRepeating",
                           "PeriodicWorkRequest", "newWakeLock", "postAtTime"):
                self.assertNotIn(banned, src, name + " must not " + banned)
            # postDelayed IS used, once, to coalesce a burst of package broadcasts. A loop would be a
            # poll wearing a different name, so it may never be posted from its own runnable.
            self.assertNotIn("postDelayed(this", src, name + " posts itself — that is a poll")

    def test_the_home_component_ships_disabled(self):
        """OPT-IN MEANS THE CHOOSER NEVER APPEARS UNASKED.

        A CATEGORY_HOME activity makes Android offer this app in "Select a Home app" from the moment
        it is installed. Shipping the component disabled is what makes opt-in true rather than nearly
        true."""
        man = open(os.path.join(ROOT, "mobile", "android", "app", "src", "main",
                                "AndroidManifest.xml")).read()
        i = man.index(".home.HomeActivity")
        block = man[i:i + 1200]
        self.assertIn('android:enabled="false"', block)
        self.assertIn("android.intent.category.HOME", block)
        self.assertIn("android.intent.category.DEFAULT", block)

    def test_the_native_tiles_point_at_classes_that_exist(self):
        """The Phone and Messages tiles start a native screen BY NAME, so this file compiles whether
        or not those halves are in the build. The cost of that is that a typo is a toast instead of a
        screen — with the tile drawing perfectly."""
        src = open(os.path.join(HOME, "HomeActivity.java")).read()
        for cls in re.findall(r'startNative\("([^"]+)"\)', src):
            path = os.path.join(JAVA, *cls.split(".")) + ".java"
            self.assertTrue(os.path.exists(path), cls + " does not exist")

    def test_the_launcher_can_see_other_home_apps(self):
        """Android 11+ hides the package list. Without a MAIN/HOME <queries> entry,
        HomeRoles.releaseHome always answers 'there is no other home screen' and the launcher can
        never be given back — which is the brick this whole feature is written to avoid."""
        man = open(os.path.join(ROOT, "mobile", "android", "app", "src", "main",
                                "AndroidManifest.xml")).read()
        q = man[man.index("<queries>"):man.index("</queries>")]
        self.assertIn("android.intent.category.HOME", q)


if __name__ == "__main__":
    unittest.main()
