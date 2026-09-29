"""blossom.js ships with the page, BEFORE app.js, and is built synchronously while app.js boots.

The "Blossom uploads + file browser" block — which media server to use (mediaServer, uploadTarget,
_serverOrigin, _absUrl, detectProto, checkBlossomAccess, restoreMediaServer), the ceiling on stuck
media fetches, the synced client prefs, the account-wide notification preferences and the per-device
push preferences — moved out of app.js into blossom.js. (The uploads themselves were already in
upload.js.) It is NOT a screen loaded on demand: every render path asks mediaServer() / _absUrl() /
notificationAllowed() synchronously, the composer's paste and drop upload resolves its target
synchronously, and the block's top-level statements wrap window.fetch and register the focus /
online / storage listeners — all of which must happen at boot, in the order they always did. So:

  * templates/client.html loads blossom.js with its own <script> tag, BEFORE app.js;
  * app.js builds it (`_blossomMod()`) while it boots, at the point where the block used to run;
  * the service worker precaches it;
  * every entry point the rest of app.js (and upload.js, settings.js, dms.js, window.__PC) calls is a
    forwarder the module returns;
  * the variables other code also reads or writes (_blossomOK, _fileTok, _prefTouched) stay declared
    in app.js, and the one blossom.js WRITES (_blossomOK) reaches it through a setter — as do the
    synced display prefs restoreClientPrefsNostr adopts (NO_IMAGES, NEW_POSTS_PILL, AUTO_NEW_POSTS).
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CLIENT = ROOT / "static" / "js" / "client"
TEMPLATE = (ROOT / "templates" / "client.html").read_text(encoding="utf-8")
APP = (CLIENT / "app.js").read_text(encoding="utf-8")
BLOSSOM = (CLIENT / "blossom.js").read_text(encoding="utf-8")


def _script_order(html):
    return [m.group(1) for m in re.finditer(r'<script[^>]*\bsrc="/static/js/client/([\w.-]+\.js)', html)]


def _deps():
    return APP[APP.index("function _blossomDeps(){"):APP.index("function _blossomMod(")]


def _code(src):
    """The source without its comments — comments name app.js's variables freely."""
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    return re.sub(r"(?<![:'\"\\])//[^\n]*", "", src)


def test_blossom_js_is_loaded_before_app_js():
    order = _script_order(TEMPLATE)
    assert "blossom.js" in order, "templates/client.html does not load blossom.js"
    assert order.index("blossom.js") < order.index("app.js"), (
        "blossom.js must be loaded BEFORE app.js — app.js builds it synchronously while it boots")
    assert order.count("blossom.js") == 1


def test_blossom_js_is_precached_by_the_service_worker():
    sw = (CLIENT / "sw.js").read_text(encoding="utf-8")
    shell = sw[sw.index("const SHELL"):sw.index("];", sw.index("const SHELL"))]
    assert "'/static/js/client/blossom.js'" in shell


def test_app_js_builds_blossom_eagerly_where_the_block_used_to_run():
    assert "_lzGet('blossom.js', 'PCBlossomFactory', _blossomDeps)" in APP
    build = re.search(r"^  _blossomMod\(\);", APP, re.M)
    assert build, "app.js no longer builds blossom.js while it boots"
    # After its deps and forwarders, before upload.js's block (which followed it) and window.__PC.
    assert (APP.index("function _blossomDeps(") < APP.index("function uploadTarget(){ return _lzRun(")
            < build.start() < APP.index("function _uploadDeps(") < APP.index("window.__PC = {"))
    assert BLOSSOM.lstrip().startswith("/*") and "window.PCBlossomFactory = function(dep){" in BLOSSOM


def test_the_top_level_side_effects_moved_with_the_block():
    """The fetch ceiling and the three listeners are statements, not declarations: they run when the
    factory is built, which is why the build point is the block's old position."""
    code = _code(BLOSSOM)
    assert "(function boundMediaFetches(){" in code and "(function boundMediaFetches(){" not in APP
    for listener in ("window.addEventListener('focus',_refreshNotificationPreferences);",
                     "window.addEventListener('online',()=>{_notificationRefreshAt=0;",
                     "if(event.key==='pc_notification_prefs:'+_notificationOwner())"):
        assert listener in code, listener
        assert listener not in _code(APP), listener


def test_every_entry_point_is_a_forwarder_the_module_returns():
    forwarded = set(re.findall(r"function (\w+)\(\)\{ return _lzRun\(_blossomMod, _blossomLoad, '(\w+)'", APP))
    names = {a for a, b in forwarded if a == b}
    assert names == {a for a, _ in forwarded}
    for must in ("mediaServer", "uploadTarget", "_serverOrigin", "_absUrl", "_blossomBuiltin",
                 "checkBlossomAccess", "detectProto", "restoreMediaServer", "notificationAllowed",
                 "notificationSound", "mirrorPushPrefs", "_notePublishedWraps",
                 "saveClientPrefsNostr", "restoreClientPrefsNostr"):
        assert must in names, f"{must} is no longer forwarded to blossom.js"
    ret = BLOSSOM[BLOSSOM.rindex("  return {"):]
    for n in names:
        assert re.search(r"\b" + re.escape(n) + r"\b", ret), f"blossom.js does not return {n}"
    # (a zero-argument function's real header reads exactly like its forwarder's, so take a line more)
    for real in ("function mediaServer(){\n    let s = ClientSettings.get('blossomEnabled')",
                 "function uploadTarget(){\n    if(ClientSettings.get('blossomEnabled')){", "function _absUrl(u){",
                 "async function mirrorPushPrefs(owner=_notificationOwner()){"):
        assert real in BLOSSOM and real not in APP, real


def test_the_state_object_is_not_shadowed_by_the_moved_code():
    """The moved code declares a local `const S=_capPlugin(…)` (_pushPrefsToDevice), so the live
    state object is `_S`: a live read rewritten to `S.<name>` inside that function would have read
    the plugin instead."""
    assert "  const _S = dep.state;" in BLOSSOM
    assert "const S=_capPlugin('PosterChanPush','setPrefs');" in BLOSSOM
    assert not re.search(r"(?<![\w$.])S\.(ME|CFG|VIEW|GUEST|NO_IMAGES|_blossomOK|_fileTok)\b", BLOSSOM)


def test_shared_state_stays_in_app_js_and_the_module_can_write_it():
    for decl in ("let _blossomOK=null;", "let _fileTok = '';", "const _prefTouched = new Set();"):
        assert decl in APP, decl
        assert decl not in BLOSSOM, decl
    deps = _deps()
    for n in ("_blossomOK", "NO_IMAGES", "NEW_POSTS_PILL", "AUTO_NEW_POSTS", "_onLandingView"):
        assert f"get {n}(){{ return {n}; }}, set {n}(v){{ {n} = v; }}" in deps, n
        assert not re.search(r"(?<![\w$.])" + n + r"(?![\w$])", _code(BLOSSOM).replace("_S." + n, "")), n
    assert "get _fileTok(){ return _fileTok; }" in deps   # read live: ensureFileAuth refills it
    assert re.search(r"\b_prefTouched\b", deps)            # a Set, never reassigned: handed over once
