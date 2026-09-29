"""drafts.js ships with the page, BEFORE app.js, and is built synchronously while app.js boots.

The drafts block — the local-only, per-account Drafts object (save/remove/pull/push, the #draft-badge),
scheduled posts (Scheduled: create/list/cancel), the Drafts view with its ⏰ Scheduled section and
sending a draft — moved out of app.js into drafts.js. It is NOT a screen loaded on demand: Drafts and
Scheduled are OBJECTS that the composer (compose.js), the offline outbox's draft bookkeeping, the ☰
badge and the login pull read synchronously, and an object moved out of app.js can only be reached
through `_lzProxy`, which needs its module present — a property read cannot wait for a load. So:

  * templates/client.html loads drafts.js with its own <script> tag, BEFORE app.js;
  * app.js builds it (`_draftsMod()`) while it boots, at the point where the block used to sit (the
    block has no top-level side effects, only declarations, so the build point changes no order);
  * the service worker precaches it;
  * the functions the rest of app.js calls (renderDrafts, bumpDraft, _appendQuoteNevent) are
    forwarders the module returns; Drafts and Scheduled are reached through `_lzProxy`;
  * the mobile ☰ sheet (moreMenu, filesMenu, the ☰ badge) that used to sit INSIDE this range is not
    drafts code — it stays in app.js, just above, and drafts.js reaches bumpMoreBadge as a dependency;
  * the queued-draft bookkeeping (_qDrafts, _qDraftSet, _reconcileDeliveredDrafts) stays with the
    Outbox in app.js.
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CLIENT = ROOT / "static" / "js" / "client"
TEMPLATE = (ROOT / "templates" / "client.html").read_text(encoding="utf-8")
APP = (CLIENT / "app.js").read_text(encoding="utf-8")
DRAFTS = (CLIENT / "drafts.js").read_text(encoding="utf-8")


def _script_order(html):
    return [m.group(1) for m in re.finditer(r'<script[^>]*\bsrc="/static/js/client/([\w.-]+\.js)', html)]


def _deps():
    return APP[APP.index("function _draftsDeps(){"):APP.index("function _draftsMod(")]


def _code(src):
    """The source without its comments — comments name app.js's variables freely."""
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    return re.sub(r"(?<![:'\"\\])//[^\n]*", "", src)


def test_drafts_js_is_loaded_before_app_js():
    order = _script_order(TEMPLATE)
    assert "drafts.js" in order, "templates/client.html does not load drafts.js"
    assert order.index("drafts.js") < order.index("app.js"), (
        "drafts.js must be loaded BEFORE app.js — app.js builds it synchronously while it boots")
    assert order.count("drafts.js") == 1


def test_drafts_js_is_precached_by_the_service_worker():
    sw = (CLIENT / "sw.js").read_text(encoding="utf-8")
    shell = sw[sw.index("const SHELL"):sw.index("];", sw.index("const SHELL"))]
    assert "'/static/js/client/drafts.js'" in shell


def test_app_js_builds_drafts_eagerly_where_the_block_used_to_be():
    assert "_lzGet('drafts.js', 'PCDraftsFactory', _draftsDeps)" in APP
    build = re.search(r"^  _draftsMod\(\);", APP, re.M)
    assert build, "app.js no longer builds drafts.js while it boots"
    # After its deps and both proxies, before the composer's entry points (which followed the old
    # block, and whose deps hand Drafts/Scheduled to compose.js) and before window.__PC.
    assert (APP.index("function _draftsDeps(") < APP.index("const Drafts = _lzProxy(_draftsMod, 'Drafts');")
            < APP.index("const Scheduled = _lzProxy(_draftsMod, 'Scheduled');")
            < build.start() < APP.index("function _composeDeps(") < APP.index("window.__PC = {"))
    assert DRAFTS.lstrip().startswith("/*") and "window.PCDraftsFactory = function(dep){" in DRAFTS


def test_every_entry_point_is_a_forwarder_the_module_returns():
    forwarded = set(re.findall(r"function (\w+)\(\)\{ return _lzRun\(_draftsMod, _draftsLoad, '(\w+)'", APP))
    names = {a for a, b in forwarded if a == b}
    assert names == {a for a, _ in forwarded}
    assert names == {"renderDrafts", "bumpDraft", "_appendQuoteNevent"}, names
    ret = DRAFTS[DRAFTS.rindex("  return {"):]
    for n in names | {"Drafts", "Scheduled"}:
        assert re.search(r"\b" + re.escape(n) + r"\b", ret), f"drafts.js does not return {n}"
    for obj in ("Drafts", "Scheduled"):
        assert f"const {obj} = _lzProxy(_draftsMod, '{obj}');" in APP
        assert f"const {obj} = {{" in DRAFTS and f"const {obj} = {{" not in APP
    for real in ("function renderDrafts(){\n    _reconcileDeliveredDrafts();", "async function _renderScheduled(){",
                 "async function sendDraft(id, btn){", "async function _sendDraft(id){",
                 "function _appendQuoteNevent(content, id, pk){"):
        assert real in DRAFTS and real not in APP, real


def test_compose_js_still_receives_the_drafts_objects():
    """compose.js is handed Drafts and Scheduled at ITS build, by value — which is only safe because
    the value is the proxy app.js keeps, never a snapshot of the module's object."""
    compose_deps = APP[APP.index("function _composeDeps(){"):APP.index("function _composeMod(")]
    for n in ("Drafts", "Scheduled", "_appendQuoteNevent"):
        assert re.search(r"\b" + n + r"\b", compose_deps), n


def test_the_mobile_sheet_stays_in_app_js():
    for fn in ("function moreMenu(){", "function filesMenu(){", "function discoverMenu(){",
               "function gamesMenu(){", "function moreBadgeCount(){", "function bumpMoreBadge(){"):
        assert fn in APP and fn not in DRAFTS, fn
    assert re.search(r"\bbumpMoreBadge\b", _deps())


def test_the_module_reads_live_state_through_getters():
    deps = _deps()
    for n in ("CFG", "ME", "VIEW"):
        assert f"get {n}(){{ return {n}; }}" in deps, n
        assert not re.search(r"(?<![\w$.])" + n + r"(?![\w$])", _code(DRAFTS).replace("S." + n, "")), n
