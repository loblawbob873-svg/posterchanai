"""COMMUNITIES IS ITS OWN PLACE, NOT A TAB INSIDE MESSAGES.

This file used to assert the opposite, under the name `test_messages_communities_merge.py`, because
commit 570ded8dc folded Concord into Messages as two tabs. That was a product decision — its commit
message is one line and records no defect — and it has been reversed by another: "Separate
Communities from Messages".

WHAT THE MERGE COST, and why the reversal is more than cosmetic:

  * Communities had no sidebar row, so it could only be reached by opening Messages first, and
    os.js builds BOTH the desktop icon grid and the start menu from those rows — so it could never
    have a desktop icon of its own.
  * It had nowhere to put an unread badge. A community with unread messages looked exactly like a
    quiet one unless you opened Messages and went looking.
  * Two apps shared one window on the desktop, which is why `sameAppWindow` had to claim they were
    the same application.

They share a metaphor and nothing else: a DM is a NIP-17 gift wrap between two people; a community
is a CORD control plane with channels, roles, moderation and its own relays.

THE ONE THING THE MERGE GOT RIGHT IS PRESERVED. `sameAppWindow` was coupled because a launch of
either tab had to find the one window that held both — without it, "two messages windows appear and
no DM window to the user". With separate views each matches ITSELF, which makes the one-window-per-app
lookup correct again rather than merely quiet; the tests below check that both directions hold.
"""
import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
APP = (ROOT / "static/js/client/app.js").read_text()
CONCORD = (ROOT / "static/js/client/concord.js").read_text()
HTML = (ROOT / "templates/client.html").read_text()
TILES = (ROOT / "mobile/android/app/src/main/java/place/poster/app/home/HomeTiles.java").read_text()


def test_both_are_top_level_launchers():
    """os.js reads these rows to build the desktop icon grid and the start menu, so a view with no
    row here exists on a phone and nowhere else."""
    assert 'data-view="messages"' in HTML
    assert 'data-view="concord"' in HTML, (
        "Communities has no sidebar row again — it is reachable only by opening Messages first, and "
        "it cannot have a desktop icon")


def test_neither_view_paints_a_tab_bar_any_more():
    """A tab row would be a second way to change view that the sidebar does not know about — and the
    highlight, the badge and the desktop icon all read the sidebar."""
    assert 'class="messages-tabs"' not in APP, "Messages still paints a Communities tab"
    assert 'id="messages-communities"' not in APP
    assert 'class="messages-tabs"' not in CONCORD, "Communities still paints a Direct messages tab"
    assert 'id="messages-direct"' not in CONCORD


def test_the_in_app_tab_switch_is_gone_or_unused():
    """`switchMessagesTab` existed to change tab WITHOUT the desktop treating it as opening another
    app. With no tabs there is nothing for it to do; it may remain as a no-op shim for an older
    saved shortcut, but nothing may still be painting a control that calls it."""
    assert "switchMessagesTab" not in CONCORD, (
        "Communities still calls the tab switcher, so it is still behaving as a tab")


def test_successful_dm_send_does_not_remount_messages():
    send = APP[APP.index("async function sendDm(pk, text){"):]
    send = send[:send.index("\n  }") + 4]
    assert "renderMessages()" not in send
    assert "await ingestWrap(toSelf, false)" in send
    assert "_keepDmOpen(pk)" in send


def test_send_repairs_mobile_thread_chrome_without_resurrecting_a_closed_thread():
    helper = APP[APP.index("function _keepDmOpen(pk){"):]
    helper = helper[:helper.index("\n  }") + 4]
    assert "VIEW!=='messages' || dmActive!==pk" in helper
    assert "classList.add('has-active')" in helper
    assert "renderDmThread(pk)" in helper


def test_successful_dm_send_runtime_stays_in_open_thread():
    runtime = ROOT / "tests/client/dm_send_stays_open_runtime.mjs"
    run = subprocess.run(["node", str(runtime)], capture_output=True, text=True, timeout=30)
    assert run.returncode == 0, run.stderr
    assert "dm send stayed in the open thread" in run.stdout


def test_android_launcher_uses_unambiguous_texts_and_messages_names():
    assert 'new Tile(VIEW_TEXTS,      "Texts"' in TILES
    assert 'new Tile("messages",      "Messages"' in TILES
    # The Android launcher catalogue is a separate surface with its own tests; Communities is not
    # a tile there yet, and adding one is a change to HomeTiles, not to this file.
    assert 'new Tile("concord"' not in TILES


def test_concord_still_routes_for_invites_and_saved_shortcuts():
    assert "renderModuleView('concord','concord.js','PCConcord','render')" in APP
    assert "if(v==='concord') $('#view-title').textContent='Messages'" not in APP, (
        "the Communities view still titles itself Messages")


def test_desktop_treats_direct_messages_and_concord_as_one_window_at_runtime():
    """Run the shipped desktop router decision, instead of asserting that a fix-shaped string exists."""
    os_js = ROOT / "static/js/client/os.js"
    boot = f"""
global.window = new EventTarget();
global.document = {{ addEventListener(){{}}, querySelector(){{ return null; }},
                    querySelectorAll(){{ return []; }} }};
global.getComputedStyle = () => ({{ zoom: '1' }});
require({json.dumps(str(os_js))});
const same = window.PCOS.__sameAppWindow;
console.log(JSON.stringify([
  same('messages', 'concord'), same('concord', 'messages'),
  same('messages', 'messages'), same('concord', 'concord'),
  same('messages', 'mail'), same('concord', 'texts'), same('home', 'global')
]));
"""
    run = subprocess.run(["node", "-e", boot], capture_output=True, text=True, timeout=30)
    assert run.returncode == 0, run.stderr
    assert json.loads(run.stdout) == [False, False, True, True, False, False, False], (
        "the desktop still treats Direct Messages and Communities as one application: opening one "
        "would focus and repaint the other, and neither could have a window of its own")


def test_desktop_router_uses_the_tested_messages_window_identity():
    os_js = (ROOT / "static/js/client/os.js").read_text()
    route = os_js[os_js.index("function routeView(view, focusOnly)"):]
    assert "wins.find(x => sameAppWindow(x.view, view))" in route[:1000]


def test_desktop_launcher_gives_each_app_its_own_window():
    os_js = (ROOT / "static/js/client/os.js").read_text()
    opened = os_js[os_js.index("function openApp(view, label, icon, render, noFeed, direct)"):]
    opened = opened[:opened.index("function ", 20)]
    assert "wins.find(w => sameAppWindow(w.view, view))" in opened
    assert "focusWin(existing, false); existing.appView = view" in opened
    assert "PC().switchView && PC().switchView(view)" in opened


# ───────── what separating them has to deliver, beyond not being a tab ──────────────────────────

def test_communities_can_show_an_unread_count():
    """As a tab it had nowhere to put one: a community with unread messages looked exactly like a
    quiet one unless you opened Messages and went looking. The count is ROOMS, not messages —
    the number somebody acts on is how many communities want them, and counting every message in
    every channel of every room on every repaint is work nobody reads."""
    assert 'id="cc-badge"' in HTML, "the sidebar row has no badge slot"
    assert 'id="cc-badge-m"' in APP, "the phone bar has no badge slot"
    fn = CONCORD.split("function unreadRooms(){", 1)[1].split("\n", 1)[0]
    assert "isUnread(r)" in fn
    paint = CONCORD.split("function paintUnreadBadge(){", 1)[1].split("\n  }", 1)[0]
    assert "#cc-badge,#cc-badge-m" in paint


def test_the_badge_updates_while_you_are_somewhere_else():
    """A badge that only paints when Communities renders is a badge you only see once you have
    already looked — which is the state the merge left us in."""
    # The LIVE merge specifically — anchored on its own neighbour, because `saveTestMessages` is
    # called from several places and the first one is not this.
    merge = CONCORD.split("notifyMentions(p,room,next,viewer,me,channel.name)", 1)[1][:300]
    assert "paintUnreadBadge()" in merge, (
        "the background merge no longer repaints the badge, so the count is stale until you open "
        "the view it is telling you to open")


def test_the_sidebar_highlight_follows_one_row_each():
    """`concord` used to light the Messages row because it WAS a tab inside Messages. With its own
    row that special case lights the wrong one."""
    line = [l for l in APP.splitlines() if "classList.toggle('active', b.dataset.view===v" in l]
    assert line, "the nav highlight moved — re-point this test"
    assert "v==='concord'" not in line[0], (
        "opening Communities still highlights the Messages row")
