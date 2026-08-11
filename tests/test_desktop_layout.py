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
        later = APPS + [{"view": "mail", "label": "Mail", "icon": "#i-mail"}]
        lay = layout({"order": ["news", "home", "global", "notes", "folder:games"]}, later)
        self.assertEqual(lay["items"][-1], "mail")

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
