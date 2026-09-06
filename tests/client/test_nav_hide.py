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


def _group_members(toggle_id):
    """The rows inside one .nav-group, from the template — never a hand-written list.

    A hardcoded copy of the six games passed happily until a seventh row (Webxdc) joined the group,
    at which point "hide every child" no longer emptied it and the fold assertion failed for a
    reason that had nothing to do with folding.
    """
    for row in SPEC:
        if row.get("cls") == "nav-group" and any(k.get("id") == toggle_id for k in row["children"]):
            return [k["view"] for k in row["children"] if k.get("view")]
    raise AssertionError(f"no .nav-group holding #{toggle_id} — the template parse has drifted")


GAMES_GROUP = _group_members("games-toggle")


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
        r = run()
        rows = r["rows"]
        keys = [row["key"] for row in rows]
        for expect in ("global", "notes", "__music", "group:disc", "group:games", "group:files",
                       "chess", "torrents"):
            self.assertIn(expect, keys, f"{expect} missing — the template parse has drifted")
        # The locked three are in the SIDEBAR (so the parse found them) but never in the editor.
        for locked in LOCKED:
            self.assertIn(locked, r["visible"], f"{locked} missing — the template parse has drifted")
        self.assertGreater(len(keys), 25, "the nav has ~35 rows; the parse found almost none")
        # Webxdc is in the Games group — the one nav row that is a directory of other people's apps.
        self.assertIn("xdc", GAMES_GROUP)
        # …and it needs nothing added to this screen: the Sidebar tab reads the nav from the DOM, so
        # a row shipped today is switchable today. That is the whole reason it is not a hand list.
        self.assertIn("xdc", keys)
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
        r = run(hide=["settings", "bookmarks", "blossom", "markets"])
        self.assertEqual(r["published"], [{"navHidden": ["markets"]}])
        self.assertEqual(r["settings"]["navHidden"], ["markets"])

    def test_they_are_not_listed_in_the_editor_at_all(self):
        """Not a disabled switch — a control that cannot move is not a setting, and three of them at
        the top of the list read as a bug ("Settings (always shown) — why is it even there!")."""
        r = run(editor={})
        for k in LOCKED:
            self.assertNotIn(k, r["editorKeys"], f"{k} was offered a switch")
            self.assertNotIn(f'data-navkey="{k}"', r["html"])
        self.assertNotIn("always shown", r["html"])
        self.assertNotIn("disabled", r["html"])
        # …and the list still says why, since the user cannot act on it anywhere else.
        self.assertIn("aren't listed", r["html"])

    def test_the_editor_cannot_turn_them_off_even_if_a_box_is_forged(self):
        """No switch exists, but the DOM is not a guarantee — the save still filters."""
        r = run(editor={"forge": ["settings", "bookmarks", "blossom"], "uncheck":
                        ["settings", "bookmarks", "blossom", "news"]})
        self.assertEqual(r["hiddenSet"], ["news"])
        # The WRITE side too — what reaches the relay is what other devices will read back.
        self.assertEqual(r["published"], [{"navHidden": ["news"]}])
        for k in LOCKED:
            self.assertIn(k, r["visible"])


@unittest.skipIf(shutil.which("node") is None, "node not installed")
class HidingTests(unittest.TestCase):

    def test_a_hidden_row_leaves_the_sidebar_and_nothing_else_does(self):
        r = run(hide=["torrents", "markets", "notes"])
        for k in ("torrents", "markets", "notes"):
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
        r = run(hide=GAMES_GROUP)
        self.assertNotIn("group:games", r["visible"], "an empty Games triangle was left behind")
        self.assertIn("group:disc", r["visible"])

    def test_one_surviving_child_keeps_the_group(self):
        r = run(hide=GAMES_GROUP[1:])
        self.assertIn("group:games", r["visible"])
        self.assertIn(GAMES_GROUP[0], r["visible"])

    def test_applying_the_same_set_twice_changes_nothing(self):
        """applyNavHidden runs from boot, from applyInstanceGating and from every save."""
        once = run(hide=["torrents"])
        twice = run(settings={"navHidden": ["torrents"]}, hide=["torrents"])
        self.assertEqual(once["visible"], twice["visible"])

    def test_turning_a_row_back_on_brings_it_back(self):
        r = run(settings={"navHidden": ["torrents", "markets"]}, hide=["markets"])
        self.assertIn("torrents", r["visible"])
        self.assertNotIn("markets", r["visible"])


@unittest.skipIf(shutil.which("node") is None, "node not installed")
class EditorTests(unittest.TestCase):

    def test_the_editor_offers_every_row_the_sidebar_has(self):
        """A SWITCH for every hideable row; a 🔒 (still orderable, still listed) for the locked
        ones — Settings is the way back to this screen, so hiding it is not a setting, but the user
        may still put it at the top ("I can't reorder Settings")."""
        r = run(editor={})
        hideable = {row["key"] for row in r["rows"] if not row.get("locked")}
        self.assertEqual(set(r["editorKeys"]), hideable)
        locked = {row["key"] for row in r["rows"] if row.get("locked")}
        for k in locked:
            self.assertIn('data-navrow="%s"' % k, r["html"], "%s lost its row (and its arrows)" % k)
            self.assertNotIn('data-navkey="%s"' % k, r["html"], "%s grew a switch it must not have" % k)

    def test_a_row_this_node_cannot_show_keeps_its_stored_setting(self):
        """THE ONE THAT LOSES OTHER DEVICES' CHOICES. The row list is per node and per shell version;
        an editor that saved only what it could see would un-hide `mail` on the laptop the moment
        Settings was opened on a nostr-only node that has no Email row at all."""
        nostr_only = [row for row in SPEC if row.get("view") not in
                      ("mail", "ai", "websearch", "terminal", "calendar", "contacts", "translate")]
        r = run(sidebar=nostr_only, settings={"navHidden": ["mail", "translate", "news"]},
                editor={"check": ["news"]})
        self.assertEqual(sorted(r["hiddenSet"]), ["mail", "translate"])
        # `navHidden` is a SET of keys — the order it happens to be written in carries no meaning and
        # follows wherever a row sits in the sidebar, so comparing it as a list makes this test fail
        # whenever a row moves (it did, when Email joined the Office group).
        self.assertEqual(len(r["published"]), 1)
        self.assertEqual(sorted(r["published"][0]["navHidden"]), ["mail", "translate"])

    def test_a_gated_off_row_is_not_offered_as_a_choice(self):
        """`hidden` already took it away — a switch for it would promise something it cannot do."""
        spec = [dict(row, cls=row["cls"] + " hidden") if row.get("view") == "meme" else row
                for row in SPEC]
        r = run(sidebar=spec, editor={})
        self.assertNotIn("meme", r["editorKeys"])
        self.assertIn("notes", r["editorKeys"])

    def test_the_save_is_written_locally_and_published(self):
        r = run(editor={"uncheck": ["torrents", "markets"]})
        self.assertEqual(sorted(r["settings"]["navHidden"]), ["markets", "torrents"])
        self.assertEqual(len(r["published"]), 1)
        self.assertEqual(sorted(r["published"][0]["navHidden"]), ["markets", "torrents"])


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


DESKTOP_SIM = ROOT / "tests" / "client" / "nav_hide_desktop_sim.js"

GAMES = ["chess", "ttt", "hangman", "connect4", "blackjack", "holdem"]


def desktop(off_views=(), off_group=False, doc=None):
    """The launcher's view of a sidebar where some rows carry `nav-off`."""
    rows = [{"cls": "nav-item", "view": v, "label": v.title()}
            for v in ("global", "notes", "torrents", "settings", "bookmarks", "blossom")]
    rows += [{"cls": "nav-item sub", "view": v, "label": v.title(), "group": "games-grp"}
             for v in GAMES]
    for r in rows:
        if r["view"] in off_views:
            r["cls"] += " nav-off"
    if off_group:
        for r in rows:
            if r.get("group") == "games-grp":
                r["group"] = "games-grp nav-off"
    out = subprocess.run(["node", str(DESKTOP_SIM), json.dumps({"sidebar": rows, "doc": doc or {}})],
                         capture_output=True, timeout=90)
    if out.returncode != 0:
        raise AssertionError(out.stderr.decode()[-3000:])
    return json.loads(out.stdout.decode())


@unittest.skipIf(shutil.which("node") is None, "node not installed")
class DesktopLauncherTests(unittest.TestCase):
    """A row switched off in Settings → Sidebar has to leave the DESKTOP too.

    It did not, at first: the preference was read as "hide the door, keep the start menu", so
    switching Games off tidied the sidebar and left all six icons on the desktop — reported as
    "games did not work as a whole group from Desktop". The start menu is not a way back, it is the
    same launcher; the way back is the switch.
    """

    def test_the_desktop_starts_out_showing_everything(self):
        d = desktop()
        self.assertIn("torrents", d["desktop"])
        self.assertEqual(sorted(d["folders"][0]["members"]), sorted(GAMES))

    def test_a_switched_off_row_leaves_the_desktop(self):
        d = desktop(off_views=["torrents", "notes"])
        self.assertNotIn("torrents", d["launch"])
        self.assertNotIn("torrents", d["desktop"])
        self.assertNotIn("notes", d["desktop"])
        self.assertIn("global", d["desktop"])

    def test_switching_off_a_whole_group_empties_its_folder(self):
        """THE REPORTED BUG. `nav-off` sits on the .nav-group, not on each child, so a check that
        only looked at the button itself left the Nostr Games folder fully populated."""
        d = desktop(off_group=True)
        for g in GAMES:
            self.assertNotIn(g, d["launch"], f"{g} survived its group being switched off")
        self.assertEqual(d["folders"], [], "an empty Nostr Games folder was still drawn")
        self.assertIn("global", d["desktop"])

    def test_switching_off_every_member_one_by_one_also_empties_it(self):
        d = desktop(off_views=GAMES)
        self.assertEqual(d["folders"], [])

    def test_a_locked_row_is_on_the_desktop_like_any_other(self):
        for k in ("settings", "bookmarks", "blossom"):
            self.assertIn(k, desktop()["desktop"])

    def test_the_users_own_desktop_arrangement_still_applies(self):
        """The launcher list shrinking must not disturb the pcai:desktop document — order, folders
        and its own hidden set are a separate decision and keep working."""
        d = desktop(off_views=["torrents"], doc={"order": ["notes", "global"], "hidden": ["blossom"]})
        self.assertEqual(d["desktop"][:2], ["notes", "global"])
        self.assertNotIn("blossom", d["desktop"])     # hidden by the DESKTOP's own document
        self.assertNotIn("torrents", d["desktop"])    # …and by the sidebar switch


@unittest.skipIf(shutil.which("node") is None, "node not installed")
class DesktopWiringTests(unittest.TestCase):
    """The parts a stub sidebar cannot reach."""

    def setUp(self):
        self.os_js = (ROOT / "static" / "js" / "client" / "os.js").read_text(encoding="utf-8")

    def test_the_launcher_uses_launchApps_and_the_lookups_do_not(self):
        """`apps()` stays COMPLETE on purpose: openApp and routeView use it to look up a view's
        label and icon, so filtering there would stop a hidden view opening from a link — the one
        thing this preference must never do."""
        self.assertIn("computeLayout(launchApps(), _doc)", self.os_js)
        self.assertIn("launchApps().filter(a => a.label.toLowerCase()", self.os_js)
        self.assertIn("const app = apps().find(a => a.view === view)", self.os_js)
        self.assertIn("if(!apps().some(a => a.view === view)) return false;", self.os_js)

    def test_the_two_shadowing_extras_answer_to_their_own_rows(self):
        """Music and Go Live are EXTRAS in os.js but real rows in the sidebar (#nav-music,
        #nav-golive), so they have to follow the same switch."""
        self.assertIn("_EXTRA_ROW = { __music: 'nav-music', __golive: 'nav-golive' }", self.os_js)

    def test_the_ancestor_walk_is_bounded_to_the_nav_group(self):
        """`closest('.hidden')` would answer "everything is gone" during boot — the whole app shell
        is `<div id="app" class="app hidden">` until sign-in — and draw an empty desktop."""
        gone = self.os_js[self.os_js.index("function _navGone("):]
        gone = gone[:gone.index("\n  }") + 4]
        self.assertIn("closest('.nav-group')", gone)
        self.assertNotIn("closest('.hidden')", gone)
        self.assertNotIn("closest('.nav-off')", gone)

    def test_changing_the_setting_redraws_the_desktop(self):
        """Otherwise the icons only go on the next reload, which reads as the switch not working."""
        self.assertIn("navChanged: refreshIcons", self.os_js)
        app = APP.read_text(encoding="utf-8")
        apply_fn = app[app.index("  function applyNavHidden(){"):]
        apply_fn = apply_fn[:apply_fn.index("\n  function ")]
        self.assertIn("PCOS.navChanged", apply_fn)

    def test_explicit_app_routing_cannot_restore_another_windows_stale_view(self):
        route = self.os_js[self.os_js.index("function routeView(view, focusOnly)"):]
        route = route[:route.index("\n  /* CLOSE")]
        self.assertIn("w.appView=view; w.appPath=''; focusWin(w, false)", route)


if __name__ == "__main__":
    unittest.main()


class SidebarOrderTests(unittest.TestCase):
    """Reordering, run through the shipped block against the stub sidebar (2026-08)."""

    S = [
        {"cls": "nav-item", "view": "home", "label": "Home"},
        {"cls": "nav-item", "view": "notes", "label": "Notes"},
        {"cls": "nav-group", "children": [
            {"cls": "nav-grouphd", "id": "files-toggle", "label": "Files"},
            {"cls": "nav-item sub", "view": "blossom", "label": "Blossom"},
        ]},
        {"cls": "nav-item", "view": "vault", "label": "Passwords"},
    ]

    def test_the_saved_order_rearranges_the_nav(self):
        out = run(sidebar=self.S, order=["vault", "group:files", "home", "notes"])
        self.assertEqual(out["navSequence"], ["vault", "group:files", "home", "notes"])
        self.assertEqual(out["navOrderSaved"], ["vault", "group:files", "home", "notes"])

    def test_a_row_the_order_never_heard_of_keeps_a_place(self):
        """The desktop-layout rule: decisions, not the list — a feature shipped after the
        arrangement still appears, after the knowns, rather than vanishing."""
        out = run(sidebar=self.S, order=["notes", "home"])
        self.assertEqual(out["navSequence"][:2], ["notes", "home"])
        self.assertEqual(sorted(out["navSequence"][2:]), ["group:files", "vault"])

    def test_the_order_is_published_to_the_prefs_doc(self):
        out = run(sidebar=self.S, order=["notes", "home"])
        # `published` records the PATCHES handed to saveClientPrefsNostr — the doc-merge itself is
        # the shipped generic path already covered by the hide tests.
        pub = [p for p in out["published"] if "navOrder" in p]
        self.assertTrue(pub, "the order never reached the Nostr prefs doc — it dies with the tab")
        self.assertEqual(pub[-1]["navOrder"], ["notes", "home"])

    def test_a_group_moves_as_one_thing(self):
        out = run(sidebar=self.S, order=["group:files", "home", "notes", "vault"])
        self.assertEqual(out["navSequence"][0], "group:files")


class GroupPickerTests(unittest.TestCase):
    """The ▦ group button must actually RENDER. The data-grpkey click handler shipped while
    _navHideHtml drew no element carrying data-grpkey — a fully wired feature with no button,
    reported as "the left navbar grouping thing never got fixed". A handler and its button live
    in different functions, so each needs its own assertion."""
    S = SidebarOrderTests.S

    def test_every_movable_row_offers_the_group_picker(self):
        out = run(sidebar=self.S)
        html = out["html"]
        import re as _re
        rows = _re.findall(r'data-navrow="([^"]+)"', html)
        movable = [k for k in rows if not k.startswith("group:") and k != "__bug"]
        offered = set(_re.findall(r'data-grpkey="([^"]+)"', html))
        for k in movable:
            self.assertIn(k, offered, "row %r has no ▦ — grouping is invisible for it" % k)
        for k in rows:
            if k.startswith("group:"):
                self.assertNotIn(k, offered, "a group header offered to move into a group")


class MovedOutTakesTheGroupsPlace(unittest.TestCase):
    """"I tried to put git outside the discover and it doesnt show" — a key absent from a saved
    order is appended after everything known, i.e. the very bottom of the sidebar, below the fold.
    A row moved to top level must slot in right after its former group instead (the rule the
    desktop icons already follow)."""
    S = SidebarOrderTests.S

    def test_the_moved_key_joins_the_order_after_its_group(self):
        out = run(sidebar=self.S, order=["home", "group:files", "notes"],
                  groupMove={"blossom": ""})
        saved = out.get("navOrderSaved") or []
        self.assertIn("blossom", saved, "the moved-out key never joined the explicit order — it "
                                        "renders at the very bottom of the sidebar")
        self.assertEqual(saved.index("blossom"), saved.index("group:files") + 1,
                         "it joined the order but not at its group's slot")

    def test_group_membership_and_position_publish_atomically(self):
        out = run(sidebar=self.S, order=["home", "group:files", "notes"],
                  groupMove={"blossom": ""})
        writes = [p for p in out["published"] if "navGroupOf" in p]
        self.assertTrue(writes)
        self.assertIn("navOrder", writes[-1], "moving out raced two versions of the same prefs document")

    def test_no_explicit_order_stores_none(self):
        out = run(sidebar=self.S, groupMove={"blossom": ""})
        self.assertFalse(out.get("navOrderSaved"),
                         "a group move invented an order pref the user never made")


class MobileNavPrefTests(unittest.TestCase):
    S = SidebarOrderTests.S

    def test_the_bar_choice_saves_and_reads_back(self):
        out = run(sidebar=self.S, mobileNav=["notes", "vault", "home", "messages"])
        self.assertEqual(out["mobileNavSaved"], ["notes", "vault", "home", "messages"])
        self.assertEqual(out["mobileNavList"], ["notes", "vault", "home", "messages"])

    def test_two_buttons_is_a_valid_bar(self):
        """"Still have to select 4 when I only want 2" — an empty slot is a choice, not a gap to
        backfill. Defaults appear only for an account that never configured the bar at all."""
        out = run(sidebar=self.S, mobileNav=["notes", "", "home", ""])
        self.assertEqual(out["mobileNavList"], ["notes", "", "home", ""])
        self.assertEqual(out["mobileNavSaved"], ["notes", "", "home", ""])

    def test_duplicates_collapse_but_empties_do_not(self):
        out = run(sidebar=self.S, mobileNav=["home", "home", "", ""])
        self.assertEqual(out["mobileNavList"], ["home", "", "", ""])

    def test_the_bar_choice_is_published_to_the_prefs_doc(self):
        out = run(sidebar=self.S, mobileNav=["notes", "vault", "home", "messages"])
        pub = [p for p in out["published"] if "mobileNav" in p]
        self.assertTrue(pub, "the bar choice never reached the Nostr prefs doc")
        self.assertEqual(pub[-1]["mobileNav"], ["notes", "vault", "home", "messages"])


class TimelineTabTests(unittest.TestCase):
    """Hiding Social tabs, run through the shipped setter — including the guard that no document,
    stale editor or hostile pref can hide ALL of them: a tab row with nothing in it is a feed with
    no way in, so the refusal lives in the setter and the reader both."""

    def test_hiding_saves_and_publishes(self):
        out = run(tlHide=["trending"])
        self.assertEqual(out["tlHidden"], ["trending"])
        pub = [p for p in out["published"] if "tlHidden" in p]
        self.assertEqual(pub[-1]["tlHidden"], ["trending"])

    def test_all_three_is_refused_by_the_setter(self):
        out = run(tlHide=["home", "global", "trending"])
        self.assertIsNone(out["tlSaved"], "the setter stored a row with nothing in it")

    def test_a_document_that_hides_everything_keeps_nostrverse(self):
        """The READER's half of the guard: the setter can be routed around by a doc written
        elsewhere, and the reader must still answer with somewhere to stand."""
        out = run(settings={"tlHidden": ["home", "global", "trending"]}, tlHide=["home"])
        self.assertNotIn("global", out["tlHidden"])


class MobileNavChoiceTests(unittest.TestCase):
    """Home and Trending are tabs, not sidebar rows — a picker built from the sidebar alone could
    never offer Home, which read as "Home is not a selectable item". The timelines are explicit."""

    def test_the_timelines_are_always_offered(self):
        # A sidebar WITHOUT home/trending rows — the real shape (Home is a tab).
        S = [{"cls": "nav-item", "view": "global", "label": "Social"},
             {"cls": "nav-item", "view": "notes", "label": "Notes"}]
        out = run(sidebar=S, mobileNav=["home", "global", "trending", "notes"])
        for v in ("home", "global", "trending", "notes"):
            self.assertIn(v, out["mobileNavChoices"], "%s is not a selectable item" % v)

    def test_social_is_not_listed_twice(self):
        S = [{"cls": "nav-item", "view": "global", "label": "Social"}]
        out = run(sidebar=S, mobileNav=["home", "global", "trending", "notes"])
        self.assertEqual(out["mobileNavChoices"].count("global"), 1)


class GroupMembershipTests(unittest.TestCase):
    """The desktop launcher's model on the sidebar: template groups are DEFAULTS applying only
    where the user has no opinion; an override moves one item — "I care about Calendar, but I
    don't want to reorder the whole group together"."""

    def test_an_override_saves_and_publishes(self):
        out = run(groupMove={"calendar": ""})
        self.assertEqual(out["groupOf"], {"calendar": ""})
        pub = [p for p in out["published"] if "navGroupOf" in p]
        self.assertEqual(pub[-1]["navGroupOf"], {"calendar": ""})

    def test_absent_prefs_move_nothing(self):
        out = run(tlHide=[])
        self.assertNotIn("groupOf", out)

    def test_the_apply_half_is_structural(self):
        import os
        root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        app = open(os.path.join(root, "static", "js", "client", "app.js"), encoding="utf-8").read()
        at = app.index("function applyNavGroups()")
        body = app[at:at + 2600]
        self.assertIn("el.classList.add('sub')", body)
        self.assertIn("el.classList.remove('sub')", body, "moving OUT of a group keeps the sub style")
        self.assertIn("_foldEmptyNavGroups()", body, "an emptied group leaves a corpse header")
        # moving out places the row right after the group it left — not at the end of the nav
        self.assertIn("insertBefore(el, grp.nextSibling)", body)
        # …and the editor offers the way out by name
        self.assertIn("Top level (its own row)", app)


class SidebarRecoveryTests(unittest.TestCase):
    def test_show_all_is_a_real_persisted_recovery_not_cosmetic_boxes(self):
        import os
        root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        app = open(os.path.join(root, "static", "js", "client", "app.js"), encoding="utf-8").read()
        self.assertIn('id="nav-show-all"', app)
        at = app.index("const showAll = $('#nav-show-all')")
        body = app[at:at + 900]
        self.assertIn("cb.checked = true", body)
        self.assertIn("await setNavHidden(keep)", body)
        self.assertIn("!shown.has(k)", body,
                      "showing this device must preserve unavailable instance-only choices")


def test_media_center_is_a_top_level_launcher_entry():
    rows = [row for row in sidebar_spec() if row.get('view') == 'media-center']
    assert len(rows) == 1
    assert rows[0]['cls'] == 'nav-item'
    assert rows[0]['label'] == 'Media Center'
