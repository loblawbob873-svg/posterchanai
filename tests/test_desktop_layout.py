"""The desktop's own arrangement — run the SHIPPED code (static/js/client/os.js) under node.

Run: venv-unified/bin/python -m unittest tests.test_desktop_layout

WHY A NODE HARNESS. `computeLayout(sidebar, document)` is where a customised desktop can go wrong,
and every way it goes wrong is silent: an app that quietly stops appearing, an icon that ends up in
two places, a folder that keeps a member it was dragged out of, a feature added next month that
never shows up on a desktop somebody arranged a year ago. None of that raises anything — you get a
desktop, just not yours. So the real function is driven with real inputs rather than inferred from a
rendered screen (scripts/check_os_desktop.py does the rendering half).

The rules being asserted, all of which are decisions rather than accidents:

  * The document stores DECISIONS (order, folders, hidden), never the app list. The list is read from
    the sidebar every time, so a new feature appears on a customised desktop for free.
  * The built-in folders (Nostr Games) are a DEFAULT: they only claim views the document has no
    opinion about, so dragging one out keeps it out while a game added later still joins the rest.
  * A view is in at most one folder, a folder is never inside a folder, a hidden view is not also
    placed, and a folder with nothing in it is not drawn.
"""
import json
import re
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OS_JS = ROOT / "static" / "js" / "client" / "os.js"
STORE_JS = ROOT / "static" / "js" / "client" / "store.js"
APP_JS = ROOT / "static" / "js" / "client" / "app.js"
SW_JS = ROOT / "static" / "js" / "client" / "sw.js"
CSS = ROOT / "static" / "css" / "client.css"

# Enough of a document for os.js to evaluate: it touches the DOM only inside functions, but it does
# bind one keydown listener at load, and reads the zoom through getComputedStyle.
BOOT = """
global.window = {};
global.document = { addEventListener(){}, querySelector(){ return null; },
                    querySelectorAll(){ return []; } };
global.getComputedStyle = () => ({ zoom: '1' });
require(%s);
const PCOS = window.PCOS;
""" % json.dumps(str(OS_JS))

# A stand-in sidebar. The six games are the real ones, because the built-in folder names them.
APPS = [
    {"view": "home", "label": "Home", "icon": "#i-home"},
    {"view": "global", "label": "Social", "icon": "#i-globe"},
    {"view": "notes", "label": "Notes", "icon": "#i-note"},
    {"view": "news", "label": "News", "icon": "#i-news"},
    {"view": "chess", "label": "Chess", "icon": "#i-pawn"},
    {"view": "ttt", "label": "Tic Tac Toe", "icon": "#i-grid"},
    {"view": "hangman", "label": "Hangman", "icon": "#i-text"},
    {"view": "connect4", "label": "Connect 4", "icon": "#i-grid"},
    {"view": "blackjack", "label": "Blackjack", "icon": "#i-cards"},
    {"view": "holdem", "label": "Hold'em", "icon": "#i-spade"},
]


def _node(script: str):
    out = subprocess.run(["node", "-e", BOOT + script], capture_output=True, timeout=60)
    if out.returncode != 0:
        raise AssertionError(out.stderr.decode()[-2000:])
    return json.loads(out.stdout.decode() or "null")


def layout(doc, apps=None):
    """Run the shipped computeLayout and return it in a form that is easy to assert on."""
    return _node(f"""
      const lay = PCOS.__layout({json.dumps(apps if apps is not None else APPS)},
                                {json.dumps(doc)});
      console.log(JSON.stringify({{
        items: lay.items.map(i => i.view),
        labels: lay.items.map(i => i.label),
        folders: lay.folders.map(f => [f.key, f.label, f.members.map(m => m.view)]),
        hidden: lay.hidden.map(h => h.view),
      }}));
    """)


def norm(doc):
    return _node(f"console.log(JSON.stringify(PCOS.__normDoc({json.dumps(doc)})));")


def members(lay, key):
    for k, _label, views in lay["folders"]:
        if k == key:
            return views
    return None


def test_desktop_feed_has_a_visible_scrollbar_without_changing_mobile():
    css = CSS.read_text()
    assert "body.os-on .osw-body > .feed{scrollbar-width:thin" in css
    assert "body.os-on .osw-body > .feed::-webkit-scrollbar{width:10px" in css
    # The broad rule remains hidden; only an actual windowed-desktop feed overrides it.
    assert "*{scrollbar-width:none}" in css


@unittest.skipIf(shutil.which("node") is None, "node not installed")
class DefaultsTests(unittest.TestCase):
    def test_no_document_is_the_classic_desktop(self):
        """Nobody has customised anything: sidebar order, with the games folded into one tile."""
        lay = layout(None)
        self.assertEqual(lay["items"], ["home", "global", "notes", "news", "folder:games"])
        self.assertEqual(members(lay, "games"),
                         ["chess", "ttt", "hangman", "connect4", "blackjack", "holdem"])

    def test_an_empty_document_is_the_same_desktop(self):
        """A user who has a document but has changed nothing back must not see a different desktop
        from a user who has no document at all — that difference is what a first save would create."""
        self.assertEqual(layout({"v": 1, "folders": [], "order": [], "hidden": []})["items"],
                         layout(None)["items"])

    def test_a_folder_with_no_members_is_not_drawn(self):
        """A deployment with the games off sees nothing rather than an empty tile."""
        plain = [a for a in APPS if a["view"] in ("home", "news")]
        self.assertEqual(layout(None, plain)["items"], ["home", "news"])


@unittest.skipIf(shutil.which("node") is None, "node not installed")
class OrderTests(unittest.TestCase):
    def test_the_saved_order_is_the_order(self):
        lay = layout({"order": ["news", "folder:games", "home", "global", "notes"]})
        self.assertEqual(lay["items"], ["news", "folder:games", "home", "global", "notes"])

    def test_a_new_feature_appears_at_the_end(self):
        """THE rule that keeps a customised desktop alive: the document stores decisions, not the
        app list, so a feature shipped after it was written still shows up."""
        # NOT a view that a built-in FOLDER claims — one of those correctly lands in its group
        # instead, which is a different rule (and its own test below).
        later = APPS + [{"view": "kanban", "label": "Kanban", "icon": "#i-grid"}]
        lay = layout({"order": ["news", "home", "global", "notes", "folder:games"]}, later)
        self.assertEqual(lay["items"][-1], "kanban")

    def test_a_new_app_in_a_built_in_group_joins_it_rather_than_the_end(self):
        """The other half of the same rule, and what makes the Office and Nostr Games defaults worth
        having: a view the document has no opinion about, which a built-in folder claims, lands in
        that folder — including on a desktop somebody arranged before the folder existed."""
        later = APPS + [{"view": "mail", "label": "Email", "icon": "#i-mail"}]
        lay = layout({"order": ["news", "home", "global", "notes"]}, later)
        self.assertNotIn("mail", lay["items"])
        office = members(lay, "office")
        self.assertIsNotNone(office, "the Office group was not applied")
        self.assertIn("mail", office)

    def test_a_retired_feature_leaves_without_taking_anything_with_it(self):
        """A view the sidebar no longer has (nostr_only, no instance, a removed feature) is dropped
        from the arrangement — it must not leave a tile that opens nothing."""
        fewer = [a for a in APPS if a["view"] != "notes"]
        lay = layout({"order": ["notes", "news", "home"]}, fewer)
        self.assertNotIn("notes", lay["items"])
        self.assertEqual(lay["items"][:2], ["news", "home"])

    def test_an_app_is_never_drawn_twice(self):
        """A document that lists a view in the order AND in a folder is not a crash, it is a desktop
        with the same icon in two places — and clicking either would open one window, which reads as
        an icon that does nothing."""
        lay = layout({"folders": [{"key": "u1", "label": "Mine", "views": ["news"]}],
                      "order": ["news", "folder:u1", "home"]})
        self.assertEqual(lay["items"].count("news"), 0)
        self.assertEqual(members(lay, "u1"), ["news"])


@unittest.skipIf(shutil.which("node") is None, "node not installed")
class FolderTests(unittest.TestCase):
    def test_a_custom_folder_takes_its_members_off_the_desktop(self):
        lay = layout({"folders": [{"key": "u1", "label": "Reading", "views": ["news", "notes"]}],
                      "order": ["home", "folder:u1", "global"]})
        self.assertEqual(lay["items"], ["home", "folder:u1", "global", "folder:games"])
        self.assertEqual(members(lay, "u1"), ["news", "notes"])
        self.assertIn("Reading", lay["labels"])

    def test_dragging_a_game_out_keeps_it_out(self):
        """The built-in folder is a DEFAULT, so it only claims what the document left alone. If it
        re-collected everything it names, an icon dragged onto the desktop would jump back into the
        folder on the next repaint — the change would look like it never happened."""
        lay = layout({"order": ["home", "chess", "global", "notes", "news", "folder:games"]})
        self.assertIn("chess", lay["items"])
        self.assertNotIn("chess", members(lay, "games"))
        self.assertEqual(len(members(lay, "games")), 5)

    def test_a_game_the_document_never_mentioned_joins_the_folder_you_renamed(self):
        """The other half of the same rule. The document was written when it held two games (or the
        user renamed the folder, which materialises only what was in it then); the other four are
        still unplaced, and they must land in THAT folder rather than in a second one beside it with
        the built-in name — two Games folders, one of them stale, is the failure this prevents."""
        lay = layout({"folders": [{"key": "games", "label": "Board games",
                                   "views": ["chess", "ttt"]}]})
        self.assertEqual(members(lay, "games"),
                         ["chess", "ttt", "hangman", "connect4", "blackjack", "holdem"])
        self.assertIn("Board games", lay["labels"])
        self.assertEqual(lay["items"].count("folder:games"), 1)
        self.assertEqual(len([f for f in lay["folders"] if f[0] == "games"]), 1)

    def test_taking_a_folder_apart_is_not_undone_by_the_default(self):
        """'Take folder apart' lists the members EXPLICITLY in the order for exactly this reason: a
        built-in folder would otherwise re-form out of the same unplaced views on the next draw."""
        lay = layout({"order": ["chess", "ttt", "hangman", "connect4", "blackjack", "holdem",
                                "home", "global", "notes", "news"]})
        self.assertIsNone(members(lay, "games"))
        self.assertEqual(lay["items"][:6],
                         ["chess", "ttt", "hangman", "connect4", "blackjack", "holdem"])

    def test_a_folder_cannot_hold_a_folder(self):
        lay = layout({"folders": [{"key": "u1", "label": "Nested", "views": ["folder:games", "news"]}]})
        self.assertEqual(members(lay, "u1"), ["news"])
        self.assertIsNotNone(members(lay, "games"))

    def test_a_view_lands_in_one_folder_only(self):
        lay = layout({"folders": [{"key": "u1", "label": "A", "views": ["news"]},
                                  {"key": "u2", "label": "B", "views": ["news", "notes"]}]})
        self.assertEqual(members(lay, "u1"), ["news"])
        self.assertEqual(members(lay, "u2"), ["notes"])


@unittest.skipIf(shutil.which("node") is None, "node not installed")
class HiddenTests(unittest.TestCase):
    def test_hidden_leaves_the_desktop_and_is_still_reachable(self):
        """Hidden means hidden from the DESKTOP. It is reported separately so the start menu can
        still list it — an app you cannot get back to has not been hidden, it has been deleted."""
        lay = layout({"hidden": ["news"]})
        self.assertNotIn("news", lay["items"])
        self.assertEqual(lay["hidden"], ["news"])

    def test_hidden_wins_over_the_order(self):
        lay = layout({"order": ["news", "home"], "hidden": ["news"]})
        self.assertNotIn("news", lay["items"])

    def test_a_hidden_view_the_sidebar_no_longer_has_is_not_offered(self):
        """Otherwise the desktop menu offers 'Show Notes' for a feature this deployment does not
        have, and showing it does nothing at all."""
        fewer = [a for a in APPS if a["view"] != "notes"]
        self.assertEqual(layout({"hidden": ["notes"]}, fewer)["hidden"], [])


@unittest.skipIf(shutil.which("node") is None, "node not installed")
class DocumentTests(unittest.TestCase):
    """_normDoc is the only place the invariants are enforced, and it runs on everything READ as
    well as everything written — a document from a newer client, or half-written by a crash, must
    not be able to throw while drawing the desktop, because then there is no desktop."""

    def test_junk_normalises_to_a_blank_layout(self):
        for junk in (None, 3, "nope", [], {"folders": "x", "order": 7, "hidden": {}}):
            d = norm(junk)
            self.assertEqual([d["folders"], d["order"], d["hidden"]], [[], [], []], junk)

    def test_an_empty_folder_is_dropped(self):
        self.assertEqual(norm({"folders": [{"key": "u1", "label": "x", "views": []}]})["folders"], [])

    def test_a_folder_key_is_sanitised(self):
        d = norm({"folders": [{"key": "../../evil key", "label": "x", "views": ["news"]}]})
        self.assertEqual(d["folders"][0]["key"], "evilkey")

    def test_labels_are_bounded(self):
        d = norm({"folders": [{"key": "u1", "label": "L" * 500, "views": ["news"]}]})
        self.assertEqual(len(d["folders"][0]["label"]), 60)

    def test_duplicate_keys_collapse(self):
        d = norm({"folders": [{"key": "u1", "label": "a", "views": ["news"]},
                              {"key": "u1", "label": "b", "views": ["notes"]}]})
        self.assertEqual([f["key"] for f in d["folders"]], ["u1"])


class ShadowTests(unittest.TestCase):
    """os.js must not declare a local `Relay` or `Store` — it reaches both as GLOBALS.

    Shipped, in the first version of the layout store: `const Relay = () => window.Relay;` shadowed
    the pool object across the whole IIFE. Every existing use is written defensively —
    `window.Relay && Relay.conns && Relay.conns()` — so `Relay.conns` being `undefined` on a
    FUNCTION made the guard quietly false instead of throwing. Result: the desktop's taskbar said
    "no relays are configured" on a client that was connected and working, `Relay.wake` never woke
    the pool, and `Relay.watch` never updated the tray. Nothing in the console, and CLASSIC mode was
    fine, because classic never goes through this file.

    Checked as a declaration ban rather than by testing the widget: the widget needs a browser, and
    the bug is not in the widget — it is in a name.
    """

    SRC = OS_JS.read_text()

    def test_no_local_relay_or_store_binding(self):
        for name in ("Relay", "Store"):
            hits = re.findall(rf"^\s*(?:const|let|var|function)\s+{name}\b.*$", self.SRC, re.M)
            self.assertEqual(hits, [], f"os.js declares a local `{name}`, shadowing the global that "
                                       f"netConns/enter/the tray reach as `{name}.foo`: {hits}")

    def test_the_global_uses_are_still_there(self):
        """The other half — if these ever stop existing, the ban above protects nothing."""
        for use in ("window.Relay && Relay.conns", "Relay.watch(onNetChange)", "Relay.wake && Relay.wake()"):
            self.assertIn(use, self.SRC, f"expected os.js to use the global pool via {use!r}")


class RegistrationTests(unittest.TestCase):
    """The two registrations every private document in this client has got wrong at least once.

    Neither failure says anything: the layout is intact on a relay and simply not applied, so the
    desktop draws the default order and reads as a layout that was never saved.
    """

    def test_the_cache_does_not_evict_it(self):
        src = STORE_JS.read_text()
        self.assertIn("pcai:desktop", src,
                      "store.js _isPinned does not exempt pcai:desktop — minutes of reading the "
                      "global feed would evict the desktop layout by the newest-N rule")

    def test_a_relay_change_carries_it(self):
        src = APP_JS.read_text()
        self.assertIn("pcai:desktop", src.split("_CARRY_D")[1][:400],
                      "app.js _CARRY_D does not carry pcai:desktop, so changing relays leaves the "
                      "desktop layout behind on a pool the client no longer queries")

    def test_the_service_worker_still_precaches_os_js(self):
        self.assertIn("'/static/js/client/os.js'", SW_JS.read_text())


if __name__ == "__main__":
    unittest.main()


class EveryReadOfTheDesktopFlagAgrees(unittest.TestCase):
    """THE THREE READS OF `osMode` MUST USE THE SAME DEFAULT — whatever that default is.

    WHICH default is a product decision and has changed: it was true (the desktop is what a wide
    screen gets), and `c6e441ea` made it false so a new web visitor lands on the classic client.
    This class used to assert the VALUE, so that decision turned it red and it stayed red.

    What is a BUG either way is the reads disagreeing. Two of them run on an INVOLUNTARY exit — the
    screen became too narrow, or a login gate needed the page — and re-set the flag afterwards. With
    `restore()` defaulting one way and those defaulting the other, somebody who had never touched the
    setting counted as having chosen, `exit()` wrote that choice down, and ONE rotation or ONE
    sign-in made it permanent. That is what this guards, and it survives the default changing.
    """

    @classmethod
    def setUpClass(cls):
        cls.src = OS_JS.read_text(encoding="utf-8")

    def test_every_read_of_the_flag_uses_the_same_default(self):
        import re
        reads = re.findall(r"settings\(\)\.get\(KEY,\s*(true|false)\s*\)", self.src)
        self.assertTrue(reads, "the remembered flag is never read")
        self.assertEqual(len(set(reads)), 1,
                         "the reads of osMode disagree about the default: " + str(sorted(set(reads)))
                         + " — an involuntary exit then writes down a choice nobody made, and one "
                           "rotation or one sign-in makes it permanent")

    def test_the_choice_is_still_gated_on_the_screen_fitting(self):
        """Whatever the default, a phone-width screen must never land in the windowed desktop."""
        self.assertIn("&& fits()", self.src,
                      "the desktop can be entered on a screen too small to hold a window")

    def test_leaving_it_is_still_remembered(self):
        # "Until they change it" is exit() writing false. Without this the default would be a forced
        # mode nobody could turn off.
        self.assertIn("settings().set(KEY, false)", self.src,
                      "leaving the desktop does not turn the preference off")

    def test_the_size_gate_still_applies(self):
        # A phone, or a tablet held upright, must never land in it.
        self.assertIn("&& fits()", self.src, "the desktop default is not gated on screen size")


class TheStartMenuTakesTyping(unittest.TestCase):
    """PRESS SUPER, TYPE, AND IT SEARCHES.

    Opening the menu left the caret on the desktop, so typing "fire" to reach Firefox typed into
    nothing at all.

    The earlier answer was to bind Ctrl+F to "open the start menu with the caret in its search box",
    and that was the wrong answer to the right observation: Ctrl+F means FIND IN THE THING I AM
    LOOKING AT, everywhere, and a desktop that turns it into a launcher takes it away from every app
    that would have used it. It is left alone now, and the Super key -- which every desktop uses for
    this -- both opens the menu and takes the keyboard.
    """

    @classmethod
    def setUpClass(cls):
        cls.src = OS_JS.read_text(encoding="utf-8")

    def test_the_search_box_is_focused_when_the_menu_opens(self):
        i = self.src.index("root.appendChild(menu);")
        after = self.src[i:i + 2500]
        self.assertIn("q0.focus", after, "the start menu opens with the caret nowhere")

    def test_a_printable_key_reaches_the_search_box(self):
        """For a menu opened with a POINTER, where focusing on open would raise an on-screen
        keyboard the moment somebody clicked Start."""
        i = self.src.index("root.appendChild(menu);")
        after = self.src[i:i + 2500]
        self.assertIn("e.key.length === 1", after,
                      "typing into a pointer-opened menu still goes nowhere")

    def test_the_shell_takes_the_keyboard_before_the_menu_opens(self):
        """OR YOUR TYPING GOES TO FIREFOX. Reported as "it only works if no other window is
        focused": sway's binding fires on this page's behalf, but the compositor's keyboard focus is
        still on whatever app had it. Opening the menu and focusing its search box is then a DOM
        focus inside a window that is not receiving keys, and every character lands in the other
        app -- which is worse than nothing, because it types into somebody's editor."""
        i = self.src.index("if(p === 'pc:start')")
        tick = self.src[i:i + 200]
        self.assertIn("_bareSuper(true)", tick,
                      "the start tick bypasses the compositor-aware Super path")
        j = self.src.index("async function _bareSuper")
        self.assertIn("await _raiseShell()", self.src[j:j + 500],
                      "the compositor-aware Super path opens before taking the keyboard")

    def test_our_window_is_found_by_asking_not_by_remembering(self):
        """A shell that was restarted, or a second one, would hold a stale id -- and focusing a
        window that no longer exists fails silently."""
        i = self.src.index("async function _raiseShell()")
        body = self.src[i:i + 700]
        self.assertIn("wm.windows()", body, "the window id is remembered rather than asked for")
        self.assertIn("posterchan", body)

    def test_super_tick_subscription_is_not_blocked_by_tray_refresh(self):
        """The tray subscribes only after an async hardware refresh. Start cannot depend on it."""
        i = self.src.index("_tickOff = pcWM.onEvent")
        j = self.src.index("\n        PCOSShell.watch(", i)
        self.assertIn("pcWM.subscribe()", self.src[i:j],
                      "Super forwarding still waits for the tray's initial hardware refresh")

    def test_no_compositor_is_not_a_failure(self):
        """In a browser tab the question is meaningless and the DOM already has the keys."""
        i = self.src.index("async function _raiseShell()")
        body = self.src[i:i + 700]
        self.assertIn("return false", body)

    def test_the_keys_the_menu_needs_are_not_swallowed(self):
        """`length === 1` is the test for "a character" -- true for letters, digits and punctuation
        in every layout, false for Escape, Tab, the arrows and the F-keys. A keycode range would
        have to guess at layouts and would eat the keys that close the menu."""
        i = self.src.index("root.appendChild(menu);")
        after = self.src[i:i + 2500]
        self.assertIn("e.ctrlKey || e.altKey || e.metaKey) return", after,
                      "modifiers are swallowed, so Ctrl+F and the window bindings would break")


class SigningInPutsYouBackOnTheDesktop(unittest.TestCase):
    """THE GATE PROMISED IT AND NOTHING DELIVERED IT.

    `showAuth` SUSPENDS the desktop to put the sign-in gate on screen -- the gate is a layer below
    #os-root, so shown from inside the desktop it painted a perfectly good login form underneath the
    icons -- and its comment says "signing in drops you back on the desktop you were using".

    But `restore()` was only called once, during the config load at boot, before anybody has signed
    in. So a resumed session got its desktop back and a fresh LOGIN did not: `startApp` called
    `refresh()`, which only repaints a desktop that is already on. On PosterChanOS that is the whole
    machine reverting to the single-column client -- "I get the classic mode after logging in, which
    is not what we want on the OS".
    """

    @classmethod
    def setUpClass(cls):
        cls.src = (ROOT / "static" / "js" / "client" / "app.js").read_text(encoding="utf-8")

    def test_startapp_restores_the_desktop_and_not_only_refreshes_it(self):
        i = self.src.index("PCOS.refresh) PCOS.refresh();")
        before = self.src[max(0, i - 700):i]
        self.assertIn("PCOS.restore) PCOS.restore();", before,
                      "a fresh login never asks the desktop to come back")

    def test_the_gate_still_suspends_rather_than_exits(self):
        """Suspend keeps the remembered preference; exit would turn the desktop off for good, so
        signing in would land in classic mode permanently rather than for one session."""
        self.assertIn("PCOS.suspend && PCOS.suspend()", self.src)


class CtrlFBelongsToWhateverHasFocus(unittest.TestCase):
    """A DESKTOP MUST NOT TAKE A KEY EVERY APP USES.

    Ctrl+F opened the start menu, added when it did nothing here. Reported as "ctrl + f on desktop
    mode is launching the start menu? that is terrible, ctrl f should be original behavior" -- and
    that is right: the launcher already has the key every desktop gives it, and Super both opens the
    menu and takes the keyboard so typing reaches its search.
    """

    @classmethod
    def setUpClass(cls):
        cls.src = OS_JS.read_text(encoding="utf-8")

    def test_the_desktop_does_not_bind_ctrl_f(self):
        for line in self.src.splitlines():
            t = line.strip()
            if t.startswith("//") or t.startswith("*") or t.startswith("/*"):
                continue
            if "ctrlKey" in t and ("'f'" in t or "'F'" in t):
                self.fail("the desktop still takes Ctrl+F: " + t)

    def test_super_is_still_the_way_in(self):
        self.assertIn("pc:start", self.src, "nothing opens the start menu from the keyboard")
