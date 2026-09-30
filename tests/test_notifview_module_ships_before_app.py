"""notifview.js ships with the page, BEFORE app.js, and is built synchronously while app.js boots.

The Notifications VIEW — renderNotifications, the filter tabs and their zap cursor, _notifMatch,
notifGrouped, notifHtml, the context line of a reply (_notifCtxId / _notifCtxHtml), markNotifsRead and
the "Update available" row — moved out of app.js into notifview.js. Other code reads its answers in the
same turn: notifs.js's toast asks _notifCtxId, the right-column rail renders notifHtml, the updater
repaints the list. A lazily-loaded module would hand those callers a Promise, so:

  * templates/client.html loads notifview.js with its own <script> tag, BEFORE app.js;
  * app.js builds it (`_notifviewMod()`) while it boots;
  * the service worker precaches it;
  * switchView resets the view through `_notifFreshEntry()` — pagination and scroll-to-top are the
    module's own state, so app.js cannot assign them directly any more;
  * the in-app updater, the right-column rail (loadNotifs) and the subscription (notifs.js) stay where
    they were.
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CLIENT = ROOT / "static" / "js" / "client"
TEMPLATE = (ROOT / "templates" / "client.html").read_text(encoding="utf-8")
APP = (CLIENT / "app.js").read_text(encoding="utf-8")
VIEW = (CLIENT / "notifview.js").read_text(encoding="utf-8")


def _no_stubs(src):
    """app.js keeps a one-line forwarder per moved function, which spells the same signature."""
    return re.sub(r"function \w+\(\)\{ return _lzRun\([^\n]*", "", src)


def _code(src):
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    return re.sub(r"(?<![:'\"\\])//[^\n]*", "", src)


def test_loaded_before_app_js_and_precached():
    order = [m.group(1) for m in re.finditer(r'<script[^>]*\bsrc="/static/js/client/([\w.-]+\.js)', TEMPLATE)]
    assert order.count("notifview.js") == 1
    assert order.index("notifview.js") < order.index("app.js")
    sw = (CLIENT / "sw.js").read_text(encoding="utf-8")
    shell = sw[sw.index("const SHELL"):sw.index("];", sw.index("const SHELL"))]
    assert "'/static/js/client/notifview.js'" in shell


def test_app_js_builds_it_eagerly():
    assert "_lzGet('notifview.js', 'PCNotifViewFactory', _notifviewDeps)" in APP
    build = re.search(r"^  _notifviewMod\(\);", APP, re.M)
    assert build, "app.js no longer builds notifview.js while it boots"
    assert APP.index("function _notifviewDeps(") < build.start() < APP.index("window.__PC = {")
    assert "window.PCNotifViewFactory = function(dep){" in VIEW


def test_the_view_moved_and_every_entry_point_is_forwarded():
    forwarded = set(re.findall(r"function (\w+)\(\)\{ return _lzRun\(_notifviewMod, _notifviewLoad, '(\w+)'", APP))
    names = {a for a, b in forwarded if a == b}
    assert names == {a for a, _ in forwarded}
    for must in ("renderNotifications", "renderNotificationsSoon", "notifHtml", "notifGrouped",
                 "markNotifsRead", "_notifMatch", "_notifCtxId", "_notifFreshEntry"):
        assert must in names, f"{must} is no longer forwarded to notifview.js"
    ret = VIEW[VIEW.rindex("  return {"):]
    for n in names:
        assert re.search(r"\b" + re.escape(n) + r"\b", ret), f"notifview.js does not return {n}"
    for real in ("function renderNotifications(){", "function _notifMatch(e){", "function notifGrouped(list){",
                 "function markNotifsRead(){", "function notifHtml(e){"):
        assert real in VIEW and real not in _no_stubs(APP), real
    for stays in ("function _onNewController(){", "function applyUpdate(){"):
        assert stays in _no_stubs(APP) and stays not in VIEW, stays
    # The rail (loadNotifs) is its own module now — see test_rail_module_ships_before_app.py.
    assert "function loadNotifs(){" not in VIEW


def test_a_fresh_entry_still_resets_pagination_and_scroll():
    """switchView used to assign _notifShown/_notifScrollTop itself. Those now live in the module, so
    an assignment left behind in app.js would create a stray global and reset nothing."""
    code = _code(APP)
    assert not re.search(r"(?<![\w$.])_notifShown\b", code) and not re.search(r"(?<![\w$.])_notifScrollTop\b", code)
    assert re.search(r"if\(v==='notifications'\) _notifFreshEntry\(\);", code)
    fresh = VIEW[VIEW.index("function _notifFreshEntry(){"):]
    body = fresh[:fresh.index("}")]
    assert "_notifShown = 25" in body and "_notifScrollTop = true" in body, body
    render = VIEW[VIEW.index("function renderNotifications(){"):VIEW.index("function notifGrouped(")]
    assert "if(_notifScrollTop){ _notifScrollTop=false; feed.scrollTop=0; }" in render


def test_shared_state_is_read_live():
    deps = APP[APP.index("function _notifviewDeps(){"):APP.index("function _notifviewMod(")]
    for n in ("ME", "VIEW", "LOGO", "_newBuild", "_apkUpdate", "_desktopUpdate", "_notifEpoch", "_updApplying"):
        assert f"get {n}(){{ return {n}; }}" in deps, n
        assert not re.search(r"(?<![\w$.])" + n + r"(?![\w$])", _code(VIEW).replace("S." + n, "")), n
    assert "set _updBadge(v){ _updBadge = v; }" in deps, "the view clears the update badge through a setter"
