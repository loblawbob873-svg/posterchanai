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
PURE = ["AppShelf.java", "HomeTiles.java", "LauncherPrefs.java", "Desk.java",
        "HomeMetrics.java"]

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
    List<AppShelf.Entry> ours = HomeTiles.ours();

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

    // 11. Phone and Messages are offered whether or not we hold the role. See the test.
    say("no-roles", keys(AppShelf.arrange(new ArrayList<AppShelf.Entry>(), HomeTiles.ours(), null, null, "")));

    // 12. An empty phone (the package query failed) still leaves a way to Settings.
    say("no-apps", keys(AppShelf.arrange(new ArrayList<AppShelf.Entry>(), ours, null, null, "")));

    // 13. THE DOCK. Its last slot is always the essential tile, however it was arranged.
    say("dock", keys(AppShelf.dock(AppShelf.arrange(phone, ours, null, null, ""),
            Arrays.asList("com.android.chrome/com.android.chrome.Main"), 5)));
    say("dock-empty", keys(AppShelf.dock(AppShelf.arrange(phone, ours, null, null, ""),
            new ArrayList<String>(), 5)));
    say("dock-gone", keys(AppShelf.dock(AppShelf.arrange(phone, ours, null, null, ""),
            Arrays.asList("com.gone.app/x"), 5)));
    say("dock-cap", AppShelf.dock(AppShelf.arrange(phone, ours, null, null, ""),
            Arrays.asList("a/a", "com.android.chrome/com.android.chrome.Main",
                          "com.zebra.app/com.zebra.app.Main",
                          "org.thoughtcrime.securesms/org.thoughtcrime.securesms.Main",
                          "com.android.settings/com.android.settings.Main"), 3).size());

    // 14. THE DESKTOP GRID.
    List<Desk.Item> d = new ArrayList<Desk.Item>();
    say("desk-place", Desk.add(d, new Desk.Item("a", 0, 0, 1, 1), 4, 4)
                    + " " + Desk.add(d, new Desk.Item("b", 0, 0, 1, 1), 4, 4) + " " + d);
    // A widget takes the first rectangle that fits, not the first cell.
    say("desk-widget", Desk.add(d, new Desk.Item("widget:7", 0, 0, 2, 2), 4, 4) + " "
                     + Desk.byKey(d, "widget:7"));
    // Overlap is refused, and refusing leaves it where it was.
    Desk.Item w = Desk.byKey(d, "widget:7");
    say("desk-move-blocked", Desk.moveTo(d, w, 0, 0, 4, 4) + " " + w);
    say("desk-move-ok", Desk.moveTo(d, w, 2, 2, 4, 4) + " " + w);
    // A resize that would leave the grid, or collide, is refused rather than clamped.
    say("desk-resize-out", Desk.resize(d, w, 4, 4, 1, 1, 4, 4) + " " + w);
    say("desk-resize-ok", Desk.resize(d, w, 2, 2, 1, 1, 4, 4) + " " + w);
    // A full desktop says so instead of swallowing the app.
    List<Desk.Item> full = new ArrayList<Desk.Item>();
    for (int i = 0; i < 4; i++) Desk.add(full, new Desk.Item("f" + i, 0, 0, 1, 1), 2, 2);
    say("desk-full", Desk.add(full, new Desk.Item("one-too-many", 0, 0, 1, 1), 2, 2)
                   + " " + full.size());

    // 14b. A WIDGET THAT DOES NOT FIT IS OFFERED A SMALLER SHAPE BEFORE IT IS REFUSED.
    //      A tablet grid is 5-7 x 6-8 and a phone's is 4 x 3-6, so the same widget asking for the
    //      same rectangle finds room on one and not the other. `add` asks once; `addShrinking`
    //      walks down to the floor the provider declares.
    List<Desk.Item> tight = new ArrayList<Desk.Item>();
    for (int i = 0; i < 4; i++) Desk.add(tight, new Desk.Item("i" + i, 0, 0, 1, 1), 4, 2);
    Desk.Item wide = new Desk.Item("widget:31", 0, 0, 4, 2);
    say("shrink-add", Desk.add(new ArrayList<Desk.Item>(tight),
            new Desk.Item("widget:31", 0, 0, 4, 2), 4, 2));
    say("shrink-fits", Desk.addShrinking(tight, wide, 1, 1, 4, 2) + " " + wide);
    // The floor is obeyed: a widget that will not draw below 2x1 is refused rather than squeezed.
    List<Desk.Item> tight2 = new ArrayList<Desk.Item>();
    for (int i = 0; i < 7; i++) Desk.add(tight2, new Desk.Item("j" + i, 0, 0, 1, 1), 4, 2);
    Desk.Item floored = new Desk.Item("widget:32", 0, 0, 4, 2);
    say("shrink-floor", Desk.addShrinking(tight2, floored, 2, 1, 4, 2) + " " + floored
                      + " " + tight2.size());
    // Largest area first, and the shape closest to the one asked for wins a tie: a 4x1 strip that
    // has to lose a column stays a strip.
    List<Desk.Item> strip = new ArrayList<Desk.Item>();
    Desk.add(strip, new Desk.Item("k0", 3, 0, 1, 1), 4, 4);
    Desk.Item clock = new Desk.Item("widget:33", 0, 0, 4, 1);
    say("shrink-shape", Desk.addShrinking(strip, clock, 1, 1, 4, 4) + " " + clock);
    // Nothing is shrunk that did not have to be.
    List<Desk.Item> roomy = new ArrayList<Desk.Item>();
    Desk.Item big2 = new Desk.Item("widget:34", 0, 0, 3, 2);
    say("shrink-none", Desk.addShrinking(roomy, big2, 1, 1, 4, 4) + " " + big2);
    // A refusal leaves the item at the size the person actually chose, so the message is about that.
    List<Desk.Item> none = new ArrayList<Desk.Item>();
    Desk.add(none, new Desk.Item("z", 0, 0, 1, 1), 1, 1);
    Desk.Item nope = new Desk.Item("widget:35", 0, 0, 2, 2);
    say("shrink-refused", Desk.addShrinking(none, nope, 1, 1, 1, 1) + " " + nope
                        + " " + none.size());

    // 14c. A TILE THAT BECAME AVAILABLE LATER GETS A PLACE, ONCE — and never a second time.
    //      "posterchan is the default messaging app but still no desktop / app icon ... for text":
    //      the desktop is seeded once, so a tile added to the catalogue (or un-gated) afterwards
    //      lands nowhere on an install that already exists, for ever.
    Set<String> nothingOffered = new LinkedHashSet<String>();
    say("adopt-fresh", HomeTiles.unadopted(ours, nothingOffered, null, "", null).size()
                     + " " + HomeTiles.unadopted(ours, nothingOffered, null, "", null).contains("pc:_texts"));
    // THE RULE THAT MATTERS: a tile already offered is never placed again, whatever became of it.
    // Removing an icon does not hide it, so without this record a removal is re-added every launch.
    Set<String> offeredTexts = new LinkedHashSet<String>();
    offeredTexts.add("pc:_texts");
    say("adopt-remembered", HomeTiles.unadopted(ours, offeredTexts, null, "", null).contains("pc:_texts"));
    // Already on the desktop, in the dock, or hidden — all plainly dealt with.
    say("adopt-on-desk", HomeTiles.unadopted(ours, nothingOffered, null,
            "pc:_texts|0|0|1|1\npc:notes|1|0|1|1", null).contains("pc:_texts"));
    say("adopt-in-dock", HomeTiles.unadopted(ours, nothingOffered, null, "",
            Arrays.asList("pc:_texts")).contains("pc:_texts"));
    Set<String> hidTexts = new LinkedHashSet<String>();
    hidTexts.add("pc:_texts");
    say("adopt-hidden", HomeTiles.unadopted(ours, nothingOffered, hidTexts, "", null).contains("pc:_texts"));
    // The way back is never placed uninvited, and a tile this build cannot launch is not offered.
    say("adopt-essential", HomeTiles.unadopted(ours, nothingOffered, null, "", null).contains("pc:_settings"));
    say("adopt-absent", HomeTiles.unadopted(new ArrayList<AppShelf.Entry>(), nothingOffered,
            null, "", null).size());
    // THE ONE-TIME BASELINE. Everything the catalogue already had counts as offered, except the two
    // whose absence has a written cause — anything looser re-places icons people removed on purpose.
    Set<String> base = HomeTiles.alreadyOffered();
    say("adopt-baseline", base.contains("pc:_texts") + " " + base.contains("pc:_phone")
                        + " " + base.contains("pc:notes") + " " + base.contains("pc:chess"));
    say("adopt-after-baseline", HomeTiles.unadopted(ours, HomeTiles.alreadyOffered(), null, "", null));

    // 15. A SMALLER GRID RE-PLACES; IT NEVER DROPS.
    List<Desk.Item> big = new ArrayList<Desk.Item>();
    big.add(new Desk.Item("x", 3, 3, 1, 1));
    big.add(new Desk.Item("widget:9", 0, 0, 3, 2));
    List<Desk.Item> over = Desk.fit(big, 3, 3);
    say("desk-fit", big.size() + " over=" + over.size() + " " + big);

    // 16. Storage round-trips, and junk on disk is skipped rather than fatal.
    say("desk-roundtrip", Desk.serialize(Desk.parse(Desk.serialize(big))).equals(Desk.serialize(big)));
    say("desk-junk", Desk.parse("garbage\nb|1|1|1|1\n|||\nc|x|y|1|1").size());
    say("desk-widget-id", new Desk.Item("widget:12", 0, 0, 1, 1).widgetId()
                       + " " + new Desk.Item("com.a/b", 0, 0, 1, 1).widgetId());

    // 17. THE SCREEN IT IS ACTUALLY ON. Real devices, in dp, both ways up:
    //   phone portrait 411x820, phone landscape 820x411 (short side 411)
    //   7" tablet 600x960 / 960x600, 10" tablet 800x1280 / 1280x800
    say("cols-phone", HomeMetrics.deskCols(411));
    say("cols-tab7", HomeMetrics.deskCols(600));
    say("cols-tab10", HomeMetrics.deskCols(800));
    say("dock-phone", HomeMetrics.dockMax(411, 411) + " " + HomeMetrics.dockMax(820, 411));
    say("dock-tab10", HomeMetrics.dockMax(800, 800) + " " + HomeMetrics.dockMax(1280, 800));
    say("icon-dp", HomeMetrics.dockIconDp(411) + " " + HomeMetrics.dockIconDp(800));
    say("drawer-dp", HomeMetrics.drawerColumnDp(411) + " " + HomeMetrics.drawerColumnDp(800));
    say("rows-phone", HomeMetrics.deskRows(700, 411) + " " + HomeMetrics.deskRows(320, 411));
    say("rows-tab10", HomeMetrics.deskRows(1150, 800) + " " + HomeMetrics.deskRows(680, 800));
    say("rows-unknown", HomeMetrics.deskRows(0, 411) + " " + HomeMetrics.deskRows(0, 800));
    say("tablet", HomeMetrics.isTablet(599) + " " + HomeMetrics.isTablet(600));
    say("geometry", HomeMetrics.geometry(6, 4));
    // The swipe-up threshold: a phone (slop 24px on a x3 density, ~1700px of desk) is unchanged at
    // six times the slop; a tablet (slop 12px at x1.5, ~2200px of desk) is governed by the screen.
    say("swipe-phone", HomeMetrics.swipeUpMinPx(24, 1700));
    say("swipe-tablet", HomeMetrics.swipeUpMinPx(12, 2200));
    say("swipe-unmeasured", HomeMetrics.swipeUpMinPx(24, 0));

    // 13b. Our own tile and a phone app publishing the same key never double up.
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

    def test_phone_and_messages_are_offered_before_we_hold_the_role(self):
        """This assertion used to say the OPPOSITE, and the opposite was a dead end.

        Withholding the two tiles until the app was the default dialer / default SMS app reads as
        careful — a tile opening an empty call log is worse than no tile. But `AppRepo.installed()`
        skips our own package, so our `.sms.Messages` and `.phone.Phone` launcher aliases are not in
        the drawer either, and this list was the ONLY way to reach either screen from our home
        screen. The role is normally granted by opening the app and being asked, so the app that
        asks was behind the role it was asking for. Reported as "still no SMS app".

        The premise was also wrong. Neither screen is empty without its role: both read the system
        providers, and both draw a notice saying they are not the default and what to do about it.
        """
        rows = self.out["no-roles"]
        self.assertIn("pc:_texts", rows)
        self.assertIn("pc:_phone", rows)
        self.assertIn("pc:_settings", rows)

    def test_a_phone_with_no_readable_app_list_still_reaches_settings(self):
        self.assertIn("pc:_settings", self.out["no-apps"])

    def test_one_tile_per_key(self):
        self.assertEqual(self.out["dedupe"].count("pc:notes"), 1)

    # ---------------------------------------------------------------- the dock

    def test_the_dock_is_exactly_what_you_put_there(self):
        """It used to force "Phone settings" into the last slot. Reported as "the dock has a
        posterchan icon that loads settings and is a waste of space" and "cant remove it" — both fair:
        the dock is the most expensive space on the phone, and an unremovable item in the one row
        that is always visible is the worst place for something nobody chose."""
        self.assertNotIn("pc:_settings", self.out["dock"], "the dock still forces a slot")
        self.assertEqual(self.out["dock-empty"], "[]", "an empty dock is not empty")

    def test_the_way_back_did_not_go_away_it_moved(self):
        """Freeing the dock slot must not cost the escape hatch. "Phone settings" survives every
        filter in the drawer, and the wallpaper long-press menu offers it too — two routes that need
        no stored state and no dock space."""
        self.assertIn("pc:_settings", self.out["hide-all"], "the drawer can lose it")
        home = _code(open(os.path.join(HOME, "HomeActivity.java")).read())
        i = home.index("private void homeMenu()")
        menu = home[i:home.index("\n    }", i)]
        self.assertIn("home_phone_settings", menu, "the long-press menu has no route to Settings")
        self.assertIn("Settings.ACTION_SETTINGS", menu)

    def test_every_dock_item_can_be_removed(self):
        home = _code(open(os.path.join(HOME, "HomeActivity.java")).read())
        i = home.index("private void dockMenu(")
        body = home[i:home.index("\n    }", i)]
        self.assertNotIn("e.essential", body, "some dock item still refuses to be removed")

    def test_an_uninstalled_app_leaves_no_gap_in_the_dock(self):
        self.assertEqual(self.out["dock-gone"], "[]")

    def test_the_dock_is_capped_so_the_icons_stay_a_usable_size(self):
        self.assertEqual(self.out["dock-cap"], "3")

    def test_the_dock_and_the_drawer_are_not_flat_black(self):
        """Both were `--bg`, which on the flagship theme is #0a0a0f — "the black dock looks too
        plain" and "the app drawer is also black and unstylish". The client has translucency, a
        hairline and a bloom; the phone shell was inheriting none of it."""
        home = _code(open(os.path.join(HOME, "HomeActivity.java")).read())
        self.assertIn("Skin.glass(this, pal", home, "the dock is not a glass surface")
        i = home.index("if (drawer != null)")
        seg = home[i:i + 500]
        self.assertNotIn("0.97", seg, "the drawer is still all but opaque")
        skin = open(os.path.join(ROOT, "mobile/android/app/src/main/java/place/poster/app/ui/Skin.java"),
                    encoding="utf-8").read()
        self.assertIn("public static Drawable glass(", skin)

    def test_widgets_are_reachable_from_more_than_a_wallpaper_long_press(self):
        """"no widgets can be added to posterchan launcher home screen" — the flow existed, behind a
        long press on the wallpaper, which is not somewhere anybody looks first."""
        home = _code(open(os.path.join(HOME, "HomeActivity.java")).read())
        self.assertGreaterEqual(home.count("addWidget()"), 3,
                                "there is still only one way to reach the widget picker")
        # …and a refusal is said out loud. BIND_APPWIDGET is signature-level, so being refused is
        # normal — and silent, which reads as a broken app.
        w = _code(open(os.path.join(HOME, "Widgets.java")).read())
        self.assertIn("Toast.makeText", w, "a refused widget bind says nothing")

    def test_the_widget_list_is_ours_and_not_an_intent_nobody_answers(self):
        """THE BUG UNDER ALL THREE ROUNDS OF THIS.

        Step 1 was `startActivityForResult(ACTION_APPWIDGET_PICK)`. Nothing on a modern Android
        declares a filter for it — the system picker belonged to the era when a dialog owned "Add to
        Home screen" — so it threw ActivityNotFoundException, the catch freed the widget id and said
        "this phone has no widget picker", and every entry point arrived at that same dead end.
        Making the flow findable from three menus could not help.

        So the list must be built here, from the widget manager. `systemPickerExists()` may stay
        (a device test prints what it answers), but nothing in the flow may DEPEND on the intent."""
        w = _code(open(os.path.join(HOME, "Widgets.java")).read())
        self.assertIn("getInstalledProviders", w, "the picker does not ask the widget manager")
        self.assertIn("public java.util.List<Choice> providers(", w)
        start = w.index("public int add(Activity a, Choice c)")
        end = w.index("private static android.os.UserHandle profileOf")
        self.assertNotIn("ACTION_APPWIDGET_PICK", w[start:end],
                         "the add flow still fires an intent nothing answers")
        self.assertNotIn("ACTION_APPWIDGET_PICK", w[:start],
                         "something before the add flow still fires it")
        # The bind ask must survive: it is the sanctioned route for a signature-level permission.
        self.assertIn("ACTION_APPWIDGET_BIND", w)
        home = _code(open(os.path.join(HOME, "HomeActivity.java")).read())
        self.assertNotIn("widgets.pick(", home)
        # An empty list and a missing picker are different sentences, and telling them apart is the
        # whole difference between this round and the last three.
        self.assertIn("home_no_widgets", home)

    def test_the_picker_is_grouped_and_our_own_widgets_come_first(self):
        """"i want the calendar widget and weather widget!" turned out to be a report about a LIST,
        not about missing features: all three of ours were already installed and sitting in it,
        scattered alphabetically and every one of them labelled "PosterChan"."""
        w = _code(open(os.path.join(HOME, "Widgets.java")).read())
        i = w.index("public java.util.List<Choice> providers(")
        seg = w[i:i + 3000]
        self.assertIn("getPackageName()", seg, "the sort cannot tell our widgets from anyone's")
        self.assertIn("return am ? -1 : 1;", seg, "our own widgets are not sorted first")
        home = _code(open(os.path.join(HOME, "HomeActivity.java")).read())
        # A HEADER MUST NOT BE SELECTABLE, or tapping the word "Clock" silently adds whatever row
        # happened to be under it.
        self.assertIn("public boolean isEnabled(int i) { return choiceAt(i) != null; }", home)
        self.assertIn("areAllItemsEnabled", home)
        self.assertIn("if (c == null) return;", home, "a header tap is not refused")

    # ---------------------------------------------------------------- the tablet

    def test_a_tablet_gets_a_wider_grid_than_a_phone(self):
        """"the launcher needs to work on tablet mode too". Four columns and a five-slot dock are a
        phone layout; on a ten-inch screen they are four icons the size of coasters."""
        self.assertEqual(self.out["cols-phone"], "4")
        self.assertEqual(self.out["cols-tab7"], "5")
        self.assertEqual(self.out["cols-tab10"], "6")
        self.assertEqual(self.out["tablet"], "false true")

    def test_a_rotation_does_not_change_the_column_count(self):
        """COLUMNS COME FROM THE SHORT SIDE. If they came from the current width, every rotation
        would re-flow the arrangement through Desk.fit — nothing deleted, but rotate to landscape and
        back and your icons are not where you left them, for ever. A tablet rotates all the time."""
        # deskCols takes only smallestScreenWidthDp, so landscape and portrait are the same call.
        self.assertEqual(self.out["cols-tab10"], "6")
        src = open(os.path.join(HOME, "HomeMetrics.java")).read()
        self.assertIn("public static int deskCols(int smallestWidthDp)", src,
                      "deskCols can see the current width, which makes a rotation destructive")

    def test_a_phone_in_landscape_is_still_a_phone(self):
        """Its width in dp is tablet-sized and its ergonomics are not."""
        self.assertEqual(self.out["dock-phone"], "5 5")

    def test_a_tablet_dock_is_not_five_icons_floating_in_a_metre_of_bar(self):
        self.assertEqual(self.out["dock-tab10"], "6 9")
        self.assertEqual(self.out["icon-dp"], "52 64")
        self.assertEqual(self.out["drawer-dp"], "80 104")

    def test_rows_come_from_the_height_that_was_actually_available(self):
        """And a height of zero — every draw before the first layout pass — falls back rather than
        computing three rows out of nothing."""
        self.assertEqual(self.out["rows-phone"], "7 3")
        self.assertEqual(self.out["rows-tab10"], "8 5")
        self.assertEqual(self.out["rows-unknown"], "5 6")

    def test_the_swipe_up_gesture_scales_with_the_screen_not_just_the_density(self):
        """"the swipe-up-for-apps gesture has to feel right at that size too". ViewConfiguration's
        touch slop is a density answer to a size question: six times it is about 48dp, a deliberate
        drag in a hand and a twitch on a tablet propped on a desk. The phone number must not move."""
        self.assertEqual(self.out["swipe-phone"], "144")        # 24 * 6, unchanged
        self.assertEqual(self.out["swipe-tablet"], "137")       # 2200/16 beats 12 * 6
        self.assertEqual(self.out["swipe-unmeasured"], "144")   # before layout: fall back to feel
        desk = _code(open(os.path.join(HOME, "DeskView.java")).read())
        self.assertIn("HomeMetrics.swipeUpMinPx(slop, getHeight())", desk)
        # AND THE FLING PATH IS UNTOUCHED, which is why this cannot make the drawer hard to open.
        self.assertIn("vy < -flingMin", desk)

    def test_the_desktop_is_stored_per_grid_shape(self):
        """One arrangement re-flowed on every rotation is lossy in the way that matters. A shape
        that has never been seen INHERITS the previous one rather than starting empty — a blank
        desktop after a rotation reads as the launcher having thrown everything away."""
        self.assertEqual(self.out["geometry"], "6x4")
        prefs = _code(open(os.path.join(HOME, "LauncherPrefs.java")).read())
        self.assertIn("public String desk(String geometry)", prefs)
        self.assertIn("return v == null ? desk() : v;", prefs)
        home = _code(open(os.path.join(HOME, "HomeActivity.java")).read())
        self.assertNotIn("prefs.setDesk(Desk.serialize", home,
                         "an arrangement is still written without its grid shape")
        self.assertIn("onConfigurationChanged", home,
                      "a rotation never recomputes the grid at all")

    # ---------------------------------------------------------------- the desktop

    def test_things_land_in_the_first_free_spot(self):
        self.assertEqual(self.out["desk-place"], "true true [a@0,0 1x1, b@1,0 1x1]")

    def test_a_widget_takes_the_first_rectangle_that_fits(self):
        """Not the first free CELL — a 2x2 widget dropped at the first free cell would overhang
        whatever is beside it, which `free` then refuses, and the search would give up."""
        self.assertEqual(self.out["desk-widget"], "true widget:7@2,0 2x2")

    def test_a_move_onto_something_else_is_refused_and_puts_it_back(self):
        """Not clamped, not stacked. Two things in one cell means the one underneath is unreachable,
        and an icon that lands somewhere the person did not drop it feels like a different bug."""
        self.assertEqual(self.out["desk-move-blocked"], "false widget:7@2,0 2x2")
        self.assertEqual(self.out["desk-move-ok"], "true widget:7@2,2 2x2")

    def test_a_resize_off_the_grid_is_refused_rather_than_clamped(self):
        """A widget that comes back a different size from the one the person dragged to is a widget
        that feels broken, and they cannot tell whether it was them or the app."""
        self.assertEqual(self.out["desk-resize-out"], "false widget:7@2,2 2x2")
        self.assertEqual(self.out["desk-resize-ok"], "true widget:7@2,2 2x2")

    def test_a_tile_that_became_available_later_gets_a_place(self):
        """"posterchan is the default messaging app but still no desktop / app icon ... for text".

        The desktop is seeded on the FIRST RUN and never again — which is what stops a removed icon
        coming back, and which means a tile added to the catalogue afterwards, or un-gated afterwards
        (Messages and Phone were withheld until the app held the SMS / dialer role), lands nowhere at
        all on an install that already exists."""
        n, has = self.out["adopt-fresh"].split()
        self.assertGreater(int(n), 0)
        self.assertEqual(has, "true")

    def test_it_never_re_adds_something_that_was_removed(self):
        """THE RULE THE WHOLE MECHANISM EXISTS FOR. Removing an icon from the desktop does not hide
        it, so a removal leaves no trace and looks exactly like a tile that was never offered. The
        record of what has been OFFERED is what tells them apart, and it only ever grows."""
        self.assertEqual(self.out["adopt-remembered"], "false")

    def test_a_tile_already_somewhere_is_left_alone(self):
        for k in ("adopt-on-desk", "adopt-in-dock", "adopt-hidden"):
            self.assertEqual(self.out[k], "false", k)

    def test_the_way_back_is_never_placed_uninvited(self):
        """"Phone settings" lives in the long-press menu and is essential; putting it on somebody's
        desktop is not a fix for anything. A tile this build cannot offer is not placed either."""
        self.assertEqual(self.out["adopt-essential"], "false")
        self.assertEqual(self.out["adopt-absent"], "0")

    def test_the_baseline_only_forgives_the_two_with_a_written_cause(self):
        """An install that predates the record has no way to say which absences were choices. So
        everything the catalogue already had counts as offered, except Phone and Messages — whose
        absence is explained by `ours()` having withheld them. Anything looser re-places icons
        somebody deleted."""
        self.assertEqual(self.out["adopt-baseline"], "false false true true")
        self.assertEqual(self.out["adopt-after-baseline"], "[pc:_phone, pc:_texts]")

    def test_a_widget_that_does_not_fit_is_offered_a_smaller_shape(self):
        """"widgets need support to fit on mobile phone screen", against a launcher that "looks
        great on tablet" — the same sentence twice. A tablet grid is 5-7 columns by 6-8 rows and a
        phone's is 4 by 3-6, so the same widget asking for the same rectangle finds room on one and
        not the other. `Desk.add` asked once and the caller released the widget id and said the
        desktop was full; this walks down to the floor the provider declares.

        Verified to fail without `addShrinking`: `desk-add` on the same crowded grid is `false`."""
        self.assertEqual(self.out["shrink-add"], "false")
        self.assertTrue(self.out["shrink-fits"].startswith("true "), self.out["shrink-fits"])

    def test_a_widget_is_never_squeezed_below_what_it_says_it_can_draw(self):
        """minResizeWidth/minResizeHeight are the provider saying which smaller shapes it can still
        draw. Below that a widget is not small, it is broken — so it is refused instead, and the
        refusal leaves it at the size the person chose so the message is about that widget."""
        self.assertTrue(self.out["shrink-floor"].startswith("false "), self.out["shrink-floor"])
        self.assertIn("widget:32@0,0 4x2", self.out["shrink-floor"])
        self.assertTrue(self.out["shrink-floor"].endswith(" 7"), self.out["shrink-floor"])
        self.assertTrue(self.out["shrink-refused"].startswith("false widget:35@0,0 2x2 1"),
                        self.out["shrink-refused"])

    def test_a_shrunk_widget_keeps_the_shape_it_asked_for(self):
        """Largest area first, ties to the shape nearest the request: a 4x1 clock that has to lose a
        column stays a 3x1 strip rather than becoming a 2x2 square with the same cell count. And a
        widget with room is not shrunk at all."""
        self.assertEqual(self.out["shrink-shape"], "true widget:33@0,1 4x1")
        self.assertEqual(self.out["shrink-none"], "true widget:34@0,0 3x2")

    def test_a_full_desktop_says_so(self):
        """Silently swallowing the app is how somebody taps "add to home" four times and then
        reports that the launcher does nothing."""
        self.assertEqual(self.out["desk-full"], "false 4")

    def test_a_smaller_grid_re_places_rather_than_dropping(self):
        """THE RULE THAT LOSES SOMEBODY'S WORK IF IT IS WRONG. The grid changes size on a rotation, on
        a tablet, and on a restored backup from a bigger phone. `if (!fits) continue` deletes what
        they arranged, silently."""
        self.assertTrue(self.out["desk-fit"].startswith("2 over=0"), self.out["desk-fit"])

    def test_the_arrangement_survives_a_round_trip_and_junk_is_skipped(self):
        """This string comes off disk and may have been written by an older build; throwing here
        would take the home screen down with it."""
        self.assertEqual(self.out["desk-roundtrip"], "true")
        self.assertEqual(self.out["desk-junk"], "1")

    def test_a_widget_id_is_read_back_and_an_app_key_is_not_mistaken_for_one(self):
        self.assertEqual(self.out["desk-widget-id"], "12 -1")


@unittest.skipIf(not os.path.isdir(HOME), "no android sources here")
class LauncherSources(unittest.TestCase):
    def test_long_press_app_title_opens_android_app_info(self):
        """The menu heading behaves like Pixel/Samsung launchers: tap it for force-stop, storage,
        permissions and defaults on Android's own app-details page."""
        home = _code(open(os.path.join(HOME, "HomeActivity.java")).read())
        repo = _code(open(os.path.join(HOME, "AppRepo.java")).read())
        self.assertIn("showAppMenu(", home)
        self.assertIn("heading.setOnClickListener", home)
        self.assertIn("repo.appInfo(app)", home)
        self.assertIn("ACTION_APPLICATION_DETAILS_SETTINGS", repo)
        self.assertIn('Uri.parse("package:"', repo)

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

    def test_a_tile_that_cannot_launch_is_never_drawn_or_docked(self):
        """REPORTED FROM THE DOCK: "there is some P icon on the dock that says this app would not
        open, useless does nothing". Two failures stacked — no icon, and nothing behind it — on the
        one row that is always on screen.

        A tile that cannot launch must be ABSENT: not greyed, not showing an error when tapped. And
        the dock must never be seeded from a list of ids that nothing verified, or the very first
        thing somebody sees is a dead button."""
        src = _code(open(os.path.join(HOME, "HomeActivity.java")).read())
        self.assertIn("private boolean canLaunch(", src)
        self.assertIn("resolveActivity(", src, "nothing asks whether the target exists")
        # refreshRoles filters the offered tiles through it…
        i = src.index("private void refreshRoles()")
        self.assertIn("canLaunch(", src[i:i + 700], "the tile list is not filtered")
        # …and the dock is seeded from that filtered list, not from raw ids.
        j = src.index("private void seedHome()")
        seed = src[j:j + 1200]
        self.assertIn("AppShelf.byKey(ourTiles", seed, "the dock is seeded unverified")

    def test_our_own_tiles_never_fall_back_to_a_letter(self):
        """Every PosterChan screen has a real icon transcribed from the client's sprite, so a letter
        there is not a fallback — it is a bug wearing a disguise, and it hides which tile failed.
        Reported as "the icons are mostly letters for posterchan apps on launcher, ugly"."""
        src = _code(open(os.path.join(HOME, "HomeActivity.java")).read())
        i = src.index("private Drawable ourGlyph(")
        body = src[i:src.index("\n    }", i)]
        self.assertIn("R.mipmap.ic_launcher", body, "our tiles have no dependable fallback")
        self.assertIn("Log.w(", body, "a missing glyph is silent")
        # The letter is the LAST resort inside ourGlyph and is used nowhere else for our tiles.
        for where in ("bindCell", "dockIcon"):
            k = src.index("private View " + where) if ("private View " + where) in src else src.index(where)
            seg = src[k:k + 1400]
            self.assertNotIn("Skin.letter", seg, where + " still letters one of our tiles")

    def test_the_drawer_opens_by_swiping_up_and_has_no_dock_button(self):
        """What every Android launcher has done since Pixel dropped the button. A button for it reads
        as a launcher that has not caught up, and it costs a dock slot that belongs to a real app."""
        home = _code(open(os.path.join(HOME, "HomeActivity.java")).read())
        desk = _code(open(os.path.join(HOME, "DeskView.java")).read())
        self.assertIn("onSwipeUp", desk, "the desktop has no swipe")
        self.assertIn("public void onSwipeUp()", home)
        self.assertNotIn("home_all_apps", home, "the dock still carries a drawer button")
        # Thresholds from the platform, never hand-picked pixels — density differs per phone.
        self.assertIn("getScaledTouchSlop", desk)
        self.assertIn("getScaledMinimumFlingVelocity", desk)
        # And it must lose to a drag, or moving an icon upward opens the drawer.
        self.assertIn("editing == null && !dragging", desk)
        # Down closes it, symmetrically, and so does Back.
        self.assertIn("closeDrawer()", home)
        self.assertIn("onFling", home, "there is no swipe-down dismiss")

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
