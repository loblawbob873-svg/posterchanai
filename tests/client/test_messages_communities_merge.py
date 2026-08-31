import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
APP = (ROOT / "static/js/client/app.js").read_text()
CONCORD = (ROOT / "static/js/client/concord.js").read_text()
HTML = (ROOT / "templates/client.html").read_text()
TILES = (ROOT / "mobile/android/app/src/main/java/place/poster/app/home/HomeTiles.java").read_text()


def test_only_messages_is_a_top_level_chat_launcher():
    assert 'data-view="messages"' in HTML
    assert 'data-view="concord"' not in HTML
    assert "{ view:'concord', into:'#disc-sub'" not in APP
    assert "['concord','concord','Concord']" not in APP


def test_messages_separates_direct_messages_and_communities_inside_one_app():
    assert 'class="messages-tabs"' in APP
    assert 'id="messages-communities"' in APP
    assert "switchView('concord')" in APP
    assert 'class="messages-tabs"' in CONCORD
    assert 'id="messages-direct"' in CONCORD
    assert "(p.switchMessagesTab||p.switchView)('messages')" in CONCORD


def test_internal_messages_tabs_bypass_desktop_window_routing():
    assert "function switchMessagesTab(v){" in APP
    helper = APP[APP.index("function switchMessagesTab(v){"):]
    helper = helper[:helper.index("\n  }") + 4]
    assert "switchView._osIn=1" in helper
    assert "finally{ switchView._osIn=prior; }" in helper


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
    assert 'new Tile("concord"' not in TILES


def test_old_concord_routes_remain_for_invites_and_saved_shortcuts():
    assert "renderModuleView('concord','concord.js','PCConcord','render')" in APP
    assert "if(v==='concord') $('#view-title').textContent='Messages'" in APP


def test_desktop_treats_direct_messages_and_concord_as_one_window_at_runtime():
    """Run the shipped desktop router decision, instead of asserting that a fix-shaped string exists."""
    os_js = ROOT / "static/js/client/os.js"
    boot = f"""
global.window = {{}};
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
    assert json.loads(run.stdout) == [True, True, True, True, False, False, False]


def test_desktop_router_uses_the_tested_messages_window_identity():
    os_js = (ROOT / "static/js/client/os.js").read_text()
    route = os_js[os_js.index("function routeView(view, focusOnly)"):]
    assert "wins.find(x => sameAppWindow(x.view, view))" in route[:1000]


def test_desktop_launcher_reuses_messages_window_for_both_tabs():
    os_js = (ROOT / "static/js/client/os.js").read_text()
    opened = os_js[os_js.index("function openApp(view, label, icon, render, noFeed, direct)"):]
    opened = opened[:opened.index("function ", 20)]
    assert "wins.find(w => sameAppWindow(w.view, view))" in opened
    assert "focusWin(existing, false); existing.appView = view" in opened
    assert "PC().switchView && PC().switchView(view)" in opened
