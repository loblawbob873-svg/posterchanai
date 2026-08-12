"""Hiding rows from the left nav — the shipped app.js code against the shipped sidebar.

Run: venv-unified/bin/python -m unittest tests.client.test_nav_hide

The preference itself is small. What is not small is what a wrong one costs: this is the only thing
in the client that can make a whole feature's DOOR disappear, on every device the account is signed
in on, from one switch. So the rules are asserted rather than assumed.

  * SETTINGS, BOOKMARKS AND BLOSSOM CAN NEVER BE HIDDEN. Settings is the way back from every mistake
    the preference can make — hide it and the only way to un-hide anything is to guess a URL, on a
    device that may be a phone. Bookmarks and Blossom are the only lists of what the user SAVED and
    UPLOADED; nothing else enumerates them. The rule lives in the data (`navHiddenSet` on the way in,
    `setNavHidden` on the way out), not in the checkbox, because the document is written by other
    devices running other versions of this client.
  * A ROW THIS NODE DOES NOT OFFER IS NOT A ROW THIS NODE MAY UN-HIDE. The switch list is built from
    the DOM, and the DOM differs per node (instance gating, nostr_only) and per shell version. An
    editor that saved only what it could see would un-hide Email everywhere the moment somebody
    opened Settings on a nostr-only node.
  * A GROUP HEADER CARRIES ITS CHILDREN, and a group whose children are all gone folds — otherwise
    turning off the last game leaves a "Games" triangle that opens onto nothing.
  * `nav-off` IS NOT `hidden`. applyInstanceGating owns `hidden` and re-toggles it on every run, so a
    preference written into that class would be silently undone the next time the instance changed.

The sidebar the sim runs against is parsed out of templates/client.html, so the group ids and the
row keys are the real ones — which is also what makes the last test here meaningful.
"""
import json
import re
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SIM = ROOT / "tests" / "client" / "nav_hide_sim.js"
TPL = ROOT / "templates" / "client.html"
APP = ROOT / "static" / "js" / "client" / "app.js"
CSS = ROOT / "static" / "css" / "client.css"

LOCKED = {"settings", "bookmarks", "blossom"}


def sidebar_spec():
    """The real <nav class="nav"> from templates/client.html, as the sim's element tree.

    Line-based on purpose: the template is one row per line and this has to stay readable. Jinja
    guards are stripped, so the spec is the FULL nav — the nostr-only variant is a separate case the
    tests build by dropping rows, not something to bake in here.
    """
    src = TPL.read_text(encoding="utf-8")
    nav = src[src.index('<nav class="nav">'):]
    nav = nav[:nav.index("</nav>")]
    root, stack, depth = [], [], []
    for raw in nav.splitlines():
        line = re.sub(r"\{%.*?%\}", "", raw).strip()
        if not line or line.startswith("<!--"):
            continue
        host = stack[-1]["children"] if stack else root
        if line.startswith('<div class="nav-group"'):
            grp = {"cls": "nav-group", "children": []}
            host.append(grp)
            stack.append(grp)
            depth.append(0)
            continue
        if stack and line.startswith("<div"):
            depth[-1] += 1
            continue
        if stack and line.startswith("</div>"):
            if depth[-1] == 0:
                stack.pop()
                depth.pop()
            else:
                depth[-1] -= 1
            continue
        m = re.match(r'<button class="([^"]*)"([^>]*)>', line)
        if not m:
            continue
        cls, attrs = m.group(1), m.group(2)
        view = re.search(r'data-view="([^"]*)"', attrs)
        el_id = re.search(r'id="([^"]*)"', attrs)
        label = re.search(r"<span[^>]*>([^<]*)", line)
        host.append({"cls": cls,
                     "view": view.group(1) if view else "",
                     "id": el_id.group(1) if el_id else "",
                     "label": (label.group(1) if label else "").strip()})
    return root


SPEC = sidebar_spec()


def run(**opts):
    opts.setdefault("sidebar", SPEC)
    out = subprocess.run(["node", str(SIM), json.dumps(opts)], capture_output=True, timeout=90)
    if out.returncode != 0:
        raise AssertionError(out.stderr.decode()[-3000:])
    return json.loads(out.stdout.decode())


@unittest.skipIf(shutil.which("node") is None, "node not installed")
class SidebarFixtureTests(unittest.TestCase):
    """The fixture is the product. If this stops describing the real nav, nothing below means much."""

    def test_the_real_sidebar_parsed_out_of_the_template(self):
        rows = run()["rows"]
        keys = [r["key"] for r in rows]
        for expect in ("global", "notes", "settings", "bookmarks", "blossom", "__music",
                       "group:disc", "group:games", "group:files", "chess", "torrents"):
            self.assertIn(expect, keys, f"{expect} missing — the template parse has drifted")
        self.assertGreater(len(keys), 25, "the nav has ~35 rows; the parse found almost none")
        self.assertEqual(len(keys), len(set(keys)), "a key was offered twice")
        # Labels come from the row's own <span>, minus its badge.
        by = {r["key"]: r for r in rows}
        self.assertEqual(by["global"]["label"], "Social")
        self.assertEqual(by["messages"]["label"], "Messages")   # the <i id=dm-badge> is not a label
        self.assertTrue(by["chess"]["sub"] and not by["global"]["sub"])
        self.assertTrue(by["group:games"]["group"])


@unittest.skipIf(shutil.which("node") is None, "node not installed")
class LockedRowTests(unittest.TestCase):

    def test_the_three_locked_rows_survive_a_document_that_names_them(self):
        """Written by another client, an older one, or by hand. The data drops them, not the UI."""
        r = run(settings={"navHidden": ["settings", "bookmarks", "blossom", "torrents"]})
        self.assertEqual(r["hiddenSet"], ["torrents"])
        for k in LOCKED:
            self.assertIn(k, r["visible"], f"{k} was hidden by a document that asked for it")

    def test_setNavHidden_refuses_to_write_them(self):
        r = run(hide=["settings", "bookmarks", "blossom", "4chan"])
        self.assertEqual(r["published"], [{"navHidden": ["4chan"]}])
        self.assertEqual(r["settings"]["navHidden"], ["4chan"])

    def test_their_switches_are_disabled_and_say_why(self):
        r = run(editor={})
        self.assertEqual(set(r["editorLocked"]), LOCKED, "exactly three rows are locked")
        for k in LOCKED:
            self.assertRegex(r["html"], rf'data-navkey="{k}"[^>]*disabled')
        self.assertIn("always shown", r["html"])

    def test_the_editor_cannot_turn_them_off_even_if_the_box_is_forced(self):
        """A `disabled` attribute is a hint to a person, not a guarantee about the DOM."""
        r = run(editor={"uncheck": ["settings", "bookmarks", "blossom", "news"]})
        self.assertEqual(r["hiddenSet"], ["news"])
        for k in LOCKED:
            self.assertIn(k, r["visible"])


@unittest.skipIf(shutil.which("node") is None, "node not installed")
class HidingTests(unittest.TestCase):

    def test_a_hidden_row_leaves_the_sidebar_and_nothing_else_does(self):
        r = run(hide=["torrents", "4chan", "notes"])
        for k in ("torrents", "4chan", "notes"):
            self.assertNotIn(k, r["visible"])
        for k in ("global", "messages", "news", "chess", "settings"):
            self.assertIn(k, r["visible"])

    def test_hiding_uses_its_own_class_never_hidden(self):
        """`hidden` belongs to applyInstanceGating, which re-toggles it on every run — a preference
        written there is undone the next time the instance changes, with nothing said."""
        block = APP.read_text(encoding="utf-8")
        block = block[block.index("/* ===== HIDING ROWS FROM THE LEFT NAV"):
                      block.index("/* ===== end of the left-nav hiding block =====")]
        # The only `hidden` in the block is the group fold (which is gating's own concern) and the
        # read that skips gated-off rows. Nothing may WRITE it against a nav-item.
        self.assertNotIn("classList.toggle('hidden'", block.replace("g.classList.toggle('hidden'", ""))
        self.assertIn(".nav-off{display:none !important}", CSS.read_text(encoding="utf-8"))

    def test_hiding_a_group_takes_its_children_with_it(self):
        r = run(hide=["group:games"])
        for k in ("chess", "ttt", "hangman", "connect4", "blackjack", "holdem"):
            self.assertNotIn(k, r["visible"])
        self.assertIn("news", r["visible"])          # a different group is untouched

    def test_a_group_whose_every_child_is_off_folds_itself(self):
        r = run(hide=["chess", "ttt", "hangman", "connect4", "blackjack", "holdem"])
        self.assertNotIn("group:games", r["visible"], "an empty Games triangle was left behind")
        self.assertIn("group:disc", r["visible"])

    def test_one_surviving_child_keeps_the_group(self):
        r = run(hide=["ttt", "hangman", "connect4", "blackjack", "holdem"])
        self.assertIn("group:games", r["visible"])
        self.assertIn("chess", r["visible"])

    def test_applying_the_same_set_twice_changes_nothing(self):
        """applyNavHidden runs from boot, from applyInstanceGating and from every save."""
        once = run(hide=["torrents"])
        twice = run(settings={"navHidden": ["torrents"]}, hide=["torrents"])
        self.assertEqual(once["visible"], twice["visible"])

    def test_turning_a_row_back_on_brings_it_back(self):
        r = run(settings={"navHidden": ["torrents", "4chan"]}, hide=["4chan"])
        self.assertIn("torrents", r["visible"])
        self.assertNotIn("4chan", r["visible"])


@unittest.skipIf(shutil.which("node") is None, "node not installed")
class EditorTests(unittest.TestCase):

    def test_the_editor_offers_every_row_the_sidebar_has(self):
        r = run(editor={})
        self.assertEqual(set(r["editorKeys"]), {row["key"] for row in r["rows"]})

    def test_a_row_this_node_cannot_show_keeps_its_stored_setting(self):
        """THE ONE THAT LOSES OTHER DEVICES' CHOICES. The row list is per node and per shell version;
        an editor that saved only what it could see would un-hide `mail` on the laptop the moment
        Settings was opened on a nostr-only node that has no Email row at all."""
        nostr_only = [row for row in SPEC if row.get("view") not in
                      ("mail", "ai", "websearch", "terminal", "calendar", "contacts", "translate")]
        r = run(sidebar=nostr_only, settings={"navHidden": ["mail", "translate", "news"]},
                editor={"check": ["news"]})
        self.assertEqual(sorted(r["hiddenSet"]), ["mail", "translate"])
        self.assertEqual(r["published"], [{"navHidden": ["mail", "translate"]}])

    def test_a_gated_off_row_is_not_offered_as_a_choice(self):
        """`hidden` already took it away — a switch for it would promise something it cannot do."""
        spec = [dict(row, cls=row["cls"] + " hidden") if row.get("view") == "meme" else row
                for row in SPEC]
        r = run(sidebar=spec, editor={})
        self.assertNotIn("meme", r["editorKeys"])
        self.assertIn("notes", r["editorKeys"])

    def test_the_save_is_written_locally_and_published(self):
        r = run(editor={"uncheck": ["torrents", "4chan"]})
        self.assertEqual(sorted(r["settings"]["navHidden"]), ["4chan", "torrents"])
        self.assertEqual(len(r["published"]), 1)
        self.assertEqual(sorted(r["published"][0]["navHidden"]), ["4chan", "torrents"])


@unittest.skipIf(shutil.which("node") is None, "node not installed")
class MobileSheetTests(unittest.TestCase):
    """The phone's ☰ More sheet is the same nav under another name, and its three sub-sheet entries
    are literals (`__discover`, `__games`, `__files`). They are mapped onto the group keys the
    SIDEBAR produces, so a renamed `#disc-toggle` would leave Discover un-hideable on phones with
    nothing on screen to say so."""

    def test_every_sheet_key_maps_onto_a_real_group(self):
        keys = {row["key"] for row in run()["rows"]}
        src = APP.read_text(encoding="utf-8")
        m = re.search(r"const _SHEET_NAV_KEY = \{([^}]*)\}", src)
        self.assertIsNotNone(m, "_SHEET_NAV_KEY is gone")
        for mapped in re.findall(r"'([^']+)'", m.group(1)):
            self.assertIn(mapped, keys, f"{mapped} is not a group the sidebar produces")

    def test_the_sheets_all_consult_the_same_set(self):
        """Four surfaces read this preference; a new one that forgets is a row that comes back on a
        phone after being switched off on a laptop."""
        src = APP.read_text(encoding="utf-8")
        for fn in ("function moreMenu(", "function discoverMenu(", "function gamesMenu(",
                   "function filesMenu("):
            body = src[src.index(fn):]
            body = body[:body.index("\n  function ", 10)]
            self.assertTrue("navHiddenSet()" in body or "NAV_OFF" in body,
                            f"{fn.strip('function (')} does not consult the hidden set")


if __name__ == "__main__":
    unittest.main()
