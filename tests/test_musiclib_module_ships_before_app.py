"""musiclib.js ships with the page, BEFORE app.js, and is built synchronously while app.js boots.

The encrypted Music library (upload, the master-key AES helpers, trackUrl / musicTracks / _trackMeta,
MusicOffline) and the DM's shareable encrypted attachments moved out of app.js into musiclib.js. It is
NOT a screen loaded on demand: musicplayer.js, Files, Music, the DM screen and the drive index call into
it synchronously, and the Android car / Bluetooth / media-button path resumes a track through
musicplayer.js -> musicTracks/trackUrl with the app in the background — a cold resume must not wait on,
or fail, a network load. So:

  * templates/client.html loads musiclib.js with its own <script> tag, BEFORE app.js;
  * app.js builds it (`_musiclibMod()`) while it boots — right after `const MusicPlayer`, because the
    deps object names MusicPlayer and would read it in its temporal dead zone any earlier (the block
    has no top-level side effects, only declarations, so the build point changes no ordering);
  * the service worker precaches it;
  * MusicOffline, an OBJECT the rest of app.js and settings/music/filesindex.js use, is reached through
    `_lzProxy` exactly like MusicPlayer and FilesIdx;
  * the variables other code also reads or writes (_blobHave, _blobSizes, _audioEl, _trackUrls,
    _ENC_MARK) stay declared in app.js.
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CLIENT = ROOT / "static" / "js" / "client"
TEMPLATE = (ROOT / "templates" / "client.html").read_text(encoding="utf-8")
APP = (CLIENT / "app.js").read_text(encoding="utf-8")
LIB = (CLIENT / "musiclib.js").read_text(encoding="utf-8")


def _script_order(html):
    return [m.group(1) for m in re.finditer(r'<script[^>]*\bsrc="/static/js/client/([\w.-]+\.js)', html)]


def test_musiclib_js_is_loaded_before_app_js():
    order = _script_order(TEMPLATE)
    assert "musiclib.js" in order, "templates/client.html does not load musiclib.js"
    assert order.index("musiclib.js") < order.index("app.js"), (
        "musiclib.js must be loaded BEFORE app.js — app.js builds it synchronously while it boots")
    assert order.count("musiclib.js") == 1


def test_musiclib_js_is_precached_by_the_service_worker():
    sw = (CLIENT / "sw.js").read_text(encoding="utf-8")
    shell = sw[sw.index("const SHELL"):sw.index("];", sw.index("const SHELL"))]
    assert "'/static/js/client/musiclib.js'" in shell


def test_app_js_builds_musiclib_eagerly_after_music_player_is_declared():
    assert "_lzGet('musiclib.js', 'PCMusicLibFactory', _musiclibDeps)" in APP
    build = re.search(r"^  _musiclibMod\(\);", APP, re.M)
    assert build, "app.js no longer builds musiclib.js while it boots"
    mp = APP.index("  const MusicPlayer = _lzProxy(")
    # After MusicPlayer (a dep — TDZ otherwise), after its deps function, before window.__PC.
    assert APP.index("function _musiclibDeps(") < mp < build.start() < APP.index("window.__PC = {")
    deps = APP[APP.index("function _musiclibDeps(){"):APP.index("function _musiclibMod(")]
    assert re.search(r"\bMusicPlayer\b", deps)
    assert LIB.lstrip().startswith("/*") and "window.PCMusicLibFactory = function(dep){" in LIB


def test_every_entry_point_is_a_forwarder_the_module_returns():
    forwarded = set(re.findall(r"function (\w+)\(\)\{ return _lzRun\(_musiclibMod, _musiclibLoad, '(\w+)'", APP))
    names = {a for a, b in forwarded if a == b}
    assert names == {a for a, _ in forwarded}
    for must in ("trackUrl", "musicTracks", "musicEntries", "_trackMeta", "_fmtTime", "uploadMusicTrack",
                 "_masterEncrypt", "_masterDecrypt", "_refreshBlobHave", "uploadSharedEnc", "dmEncOn",
                 "decorateEncAtts", "dmAttachMenu"):
        assert must in names, f"{must} is no longer forwarded to musiclib.js"
    ret = LIB[LIB.rindex("  return {"):]
    for n in names | {"MusicOffline"}:
        assert re.search(r"\b" + re.escape(n) + r"\b", ret), f"musiclib.js does not return {n}"
    assert "const MusicOffline = _lzProxy(_musiclibMod, 'MusicOffline');" in APP
    assert "const MusicOffline = {" in LIB and "const MusicOffline = {" not in APP
    assert "async function trackUrl(sha){" in LIB and "async function trackUrl(sha){" not in APP


def test_shared_variables_stay_in_app_js_and_the_module_can_write_them():
    for decl in ("let _blobHave=null;", "let _blobSizes = new Map();", "let _audioEl=null;",
                 "const _trackUrls={}, _trackUrlOrder=[];", "const _ENC_MARK = '#pcenc1=';"):
        assert decl in APP, decl
        assert decl not in LIB, decl
    deps = APP[APP.index("function _musiclibDeps(){"):APP.index("function _musiclibMod(")]
    for n in ("_blobHave", "_blobSizes"):     # _refreshBlobHave assigns both
        assert f"get {n}(){{ return {n}; }}, set {n}(v){{ {n} = v; }}" in deps, n
        assert not re.search(r"(?<![\w$.])" + n + r"(?![\w$])", LIB.replace("S." + n, "")), n
