"""notifs.js ships with the page, BEFORE app.js, and is built synchronously while app.js boots.

The notifications block — the live subscription (watchNotifications: the follower seed, the follows
sub and the mentions/reactions/zaps sub), the ping (notifPing → notifToast / osNotify), reminder
history, notifList (the gate that decides what a notification IS) and the ONE unread count every badge
paints from (notifUnread, bumpNotif) — moved out of app.js into notifs.js. It is NOT a screen loaded
on demand: the subscription starts at login, the Android and desktop notification routes land here at
boot, and the block's top-level statements (the follower pins read from localStorage, the one-time
epoch repair) must run when and in the order they always did. So:

  * templates/client.html loads notifs.js with its own <script> tag, BEFORE app.js;
  * app.js builds it (`_notifsMod()`) while it boots, at the point where the block used to run;
  * the service worker precaches it;
  * every entry point the rest of app.js (the Notifications view, the rail, window.__PC, dms.js …)
    calls is a forwarder the module returns;
  * `_notifEpoch`, which the Notifications view's filter also reads, stays declared in app.js just
    above the block and is read through a getter; the view itself (below the in-app updater) and the
    right-column rail stay in app.js.
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CLIENT = ROOT / "static" / "js" / "client"
TEMPLATE = (ROOT / "templates" / "client.html").read_text(encoding="utf-8")
APP = (CLIENT / "app.js").read_text(encoding="utf-8")
NOTIFS = (CLIENT / "notifs.js").read_text(encoding="utf-8")


def _script_order(html):
    return [m.group(1) for m in re.finditer(r'<script[^>]*\bsrc="/static/js/client/([\w.-]+\.js)', html)]


def _deps():
    return APP[APP.index("function _notifsDeps(){"):APP.index("function _notifsMod(")]


def _code(src):
    """The source without its comments — comments name app.js's variables freely."""
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    return re.sub(r"(?<![:'\"\\])//[^\n]*", "", src)


def test_notifs_js_is_loaded_before_app_js():
    order = _script_order(TEMPLATE)
    assert "notifs.js" in order, "templates/client.html does not load notifs.js"
    assert order.index("notifs.js") < order.index("app.js"), (
        "notifs.js must be loaded BEFORE app.js — app.js builds it synchronously while it boots")
    assert order.count("notifs.js") == 1


def test_notifs_js_is_precached_by_the_service_worker():
    sw = (CLIENT / "sw.js").read_text(encoding="utf-8")
    shell = sw[sw.index("const SHELL"):sw.index("];", sw.index("const SHELL"))]
    assert "'/static/js/client/notifs.js'" in shell


def test_app_js_builds_notifs_eagerly_where_the_block_used_to_run():
    assert "_lzGet('notifs.js', 'PCNotifsFactory', _notifsDeps)" in APP
    build = re.search(r"^  _notifsMod\(\);", APP, re.M)
    assert build, "app.js no longer builds notifs.js while it boots"
    # After the epoch it reads and its deps, before the in-app updater that followed the old block,
    # the Notifications view, the rail and window.__PC.
    assert (APP.index("let _notifEpoch = +(localStorage.getItem('pc_notif_epoch')||0);")
            < APP.index("function _notifsDeps(") < build.start()
            < APP.index("// ---- In-app updater:") < APP.index("function renderNotifications(){ return _lzRun(")
            < APP.index("function loadNotifs(){") < APP.index("window.__PC = {"))
    assert NOTIFS.lstrip().startswith("/*") and "window.PCNotifsFactory = function(dep){" in NOTIFS


def test_the_top_level_statements_moved_with_the_block():
    """The follower pins and the one-time epoch repair are statements, not declarations: they run
    when the factory is built, which is why the build point is the block's old position."""
    code = _code(NOTIFS)
    for stmt in ("let _followSeen={}; try{ _followSeen=JSON.parse(localStorage.getItem('pc_follow_seen')||'{}')||{}; }",
                 "if(!localStorage.getItem('pc_notif_epoch_migrated')){"):
        assert stmt in code, stmt
        assert stmt not in _code(APP), stmt


def test_every_entry_point_is_a_forwarder_the_module_returns():
    forwarded = set(re.findall(r"function (\w+)\(\)\{ return _lzRun\(_notifsMod, _notifsLoad, '(\w+)'", APP))
    names = {a for a, b in forwarded if a == b}
    assert names == {a for a, _ in forwarded}
    for must in ("watchNotifications", "notifToast", "notifList", "notifUnread", "bumpNotif",
                 "_notifTs", "_quotesMe", "_rememberReminder", "hydrateReminderNotifications"):
        assert must in names, f"{must} is no longer forwarded to notifs.js"
    ret = NOTIFS[NOTIFS.rindex("  return {"):]
    for n in names:
        assert re.search(r"\b" + re.escape(n) + r"\b", ret), f"notifs.js does not return {n}"
    for real in ("async function watchNotifications(){", "function notifPing(ev){",
                 "function notifToast(html, pic, onClick, notificationType){", "function notifList(){\n",
                 "function bumpNotif(){ const n=notifUnread();"):
        assert real in NOTIFS and real not in APP, real


def test_the_view_and_the_rail_are_not_in_notifs_js():
    """The rail and the updater stay in app.js; the VIEW moved to notifview.js (its own module, see
    test_notifview_module_ships_before_app.py) — neither belongs in the subscription module."""
    for fn in ("function loadNotifs(){", "function _onNewController(){"):
        assert fn not in NOTIFS, fn
    for fn in ("function renderNotifications(){", "function _notifMatch(e){", "function notifGrouped(list){",
               "function markNotifsRead(){"):
        assert fn not in NOTIFS, fn


def test_the_ping_still_routes_to_the_post():
    """The Android/desktop route a notification carries is built here: a post notification opens
    its post, anything else the Notifications view."""
    ping = NOTIFS[NOTIFS.index("function notifPing(ev){"):NOTIFS.index("function notifToast(")]
    assert "route:target?'post:'+target:'notifications'" in ping
    assert "onClick:()=>target?openThread(target):switchView('notifications')" in ping


def test_shared_state_is_read_live():
    deps = _deps()
    for n in ("_notifEpoch", "ME", "VIEW", "GUEST", "LOGO", "_newBuild", "_apkUpdate", "_aiToken"):
        assert f"get {n}(){{ return {n}; }}" in deps, n
        assert not re.search(r"(?<![\w$.])" + n + r"(?![\w$])", _code(NOTIFS).replace("S." + n, "")), n
    assert "let _notifEpoch" in APP and "let _notifEpoch" not in NOTIFS
    assert re.search(r"\bseenNotif\b", deps) and re.search(r"\bFOLLOWERS\b", deps)
