"""timeline.js ships with the page, BEFORE app.js, and is built synchronously while app.js boots.

The Home / Nostrverse timeline (drawing, live updates, the new-posts pill, the timeline header and
composer, the sensitive-media rules) moved out of app.js -- the user's priority, "prirotity app.js
split". It cannot be a screen loaded on demand: every feed render asks _noteNode / isSensitive /
_tlNotes SYNCHRONOUSLY, so a lazy load would hand them a promise. So:
  * templates/client.html loads timeline.js with its own <script> tag, BEFORE app.js;
  * app.js builds it while it boots, where the block used to run, and a shell that predates the tag
    (the APK takes index.html from the live site) loads it then redraws instead of drawing promises;
  * the service worker precaches it and both bundles copy it (they copy every client script);
  * every entry point is a forwarder the module returns;
  * the variables other code reads (settings flags, _tl, the live buffer, the feed ceiling) stay
    declared in app.js, and the ones the module WRITES reach it through setters.
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CLIENT = ROOT / "static" / "js" / "client"
TEMPLATE = (ROOT / "templates" / "client.html").read_text(encoding="utf-8")
APP = (CLIENT / "app.js").read_text(encoding="utf-8")
TL = (CLIENT / "timeline.js").read_text(encoding="utf-8")


def _order(html):
    return [m.group(1) for m in re.finditer(r'<script[^>]*\bsrc="/static/js/client/([\w.-]+\.js)', html)]


def test_timeline_js_is_loaded_before_app_js_and_precached():
    order = _order(TEMPLATE)
    assert order.count("timeline.js") == 1 and order.index("timeline.js") < order.index("app.js")
    sw = (CLIENT / "sw.js").read_text(encoding="utf-8")
    assert "'/static/js/client/timeline.js'" in sw[sw.index("const SHELL"):sw.index("];", sw.index("const SHELL"))]
    for build in ("desktop/build-www.sh", "mobile/build-www.sh"):
        assert 'static/js/client/*.js' in (ROOT / build).read_text(), build


def test_app_js_builds_it_eagerly_and_survives_a_shell_without_the_tag():
    assert "_lzGet('timeline.js', 'PCTimelineFactory', _timelineDeps)" in APP
    at = APP.index("  if(!_timelineMod()){")
    tail = APP[at:at + 300]
    assert "_timelineLoad().then(" in tail and "renderView()" in tail
    last_fwd = max(m.end() for m in re.finditer(r"_lzRun\(_timelineMod, _timelineLoad", APP))
    assert last_fwd < at < APP.index("// ---------- infinite scroll-back ----------")
    assert "window.PCTimelineFactory = function(dep){" in TL


def test_every_entry_point_is_a_forwarder_the_module_returns():
    fwd = re.findall(r"function (\w+)\(\)\{ return _lzRun\(_timelineMod, _timelineLoad, '(\w+)'", APP)
    assert fwd and all(a == b for a, b in fwd)
    names = {a for a, _ in fwd}
    for must in ("renderTimeline", "_drawTimeline", "_noteNode", "_noteKey", "isSensitive", "_tlNotes",
                 "_flushPending", "_updateNewPostsPill", "_capFeedDom"):
        assert must in names, must
    ret = TL[TL.rindex("  return {"):]
    for n in names:
        assert re.search(r"\b" + re.escape(n) + r"\b", ret), f"timeline.js does not return {n}"
    assert "function renderTimeline(" in TL and not re.search(r"function renderTimeline\([^)]", APP)


def test_shared_state_stays_in_app_js_and_the_module_writes_it_through_setters():
    for decl in ("let _tl = { oldest:0, loading:false, done:false, pages:0 };",
                 "let NO_IMAGES = ClientSettings.get('noImages', false) === true;",
                 "let _liveT=null, _liveFn=null, _livePending=[];", "const _FEED_MAX_CARDS = 400;"):
        assert decl in APP and decl not in TL, decl
    deps = APP[APP.index("function _timelineDeps(){"):APP.index("function _timelineMod(")]
    for written in ("_tl", "_tlGen", "_tlMedia", "_livePending", "_liveT"):
        assert re.search(r"set " + re.escape(written) + r"\(", deps), f"no setter for {written}"
