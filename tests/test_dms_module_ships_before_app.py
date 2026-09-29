"""dms.js ships with the page, BEFORE app.js, and is built synchronously while app.js boots.

The DM block — receiving and unwrapping NIP-17 gift wraps (ensureDMs, ingestWrap, the history queue,
DmCache), legacy NIP-04, sending (sendDm), the unread badge and the DM notification (_dmNotify),
Concord direct invites and the kind-10050 DM-inbox list — moved out of app.js into dms.js. It is NOT
a screen loaded on demand: incoming DMs are subscribed at login, and a push, a notification or the
Android launcher lands on Messages at boot — none of that may wait on, or fail, a network load. So:

  * templates/client.html loads dms.js with its own <script> tag, BEFORE app.js;
  * app.js builds it (`_dmsMod()`) while it boots, at the point where the block used to sit (the
    block has no top-level side effects, only declarations, so the build point changes no order);
  * the service worker precaches it;
  * every entry point the rest of app.js (and dmthread.js, window.__PC) calls is a forwarder the
    module returns; DmCache, an OBJECT (logout's DmCache.forget), is reached through `_lzProxy`;
  * the state the rest of app.js also reads or writes (dmPeers, dmActive, _dmLoaded, _dmUnread,
    _wrapTried, the counters, _cordDirectOwner, DISCOVERY_RELAYS, …) stays declared in app.js, and
    the ones dms.js WRITES reach it through setters;
  * the signer's NIP-17 wrap/unwrap (seal verification included) stays with the signer abstraction
    in app.js — makeSigner uses it, and it is not DM-screen code.
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CLIENT = ROOT / "static" / "js" / "client"
TEMPLATE = (ROOT / "templates" / "client.html").read_text(encoding="utf-8")
APP = (CLIENT / "app.js").read_text(encoding="utf-8")
DMS = (CLIENT / "dms.js").read_text(encoding="utf-8")


def _script_order(html):
    return [m.group(1) for m in re.finditer(r'<script[^>]*\bsrc="/static/js/client/([\w.-]+\.js)', html)]


def _deps():
    return APP[APP.index("function _dmsDeps(){"):APP.index("function _dmsMod(")]


def test_dms_js_is_loaded_before_app_js():
    order = _script_order(TEMPLATE)
    assert "dms.js" in order, "templates/client.html does not load dms.js"
    assert order.index("dms.js") < order.index("app.js"), (
        "dms.js must be loaded BEFORE app.js — app.js builds it synchronously while it boots")
    assert order.count("dms.js") == 1


def test_dms_js_is_precached_by_the_service_worker():
    sw = (CLIENT / "sw.js").read_text(encoding="utf-8")
    shell = sw[sw.index("const SHELL"):sw.index("];", sw.index("const SHELL"))]
    assert "'/static/js/client/dms.js'" in shell


def test_app_js_builds_dms_eagerly_where_the_block_used_to_be():
    assert "_lzGet('dms.js', 'PCDmsFactory', _dmsDeps)" in APP
    build = re.search(r"^  _dmsMod\(\);", APP, re.M)
    assert build, "app.js no longer builds dms.js while it boots"
    # After its deps and the DmCache proxy, before the Email/Messages code that follows the old block
    # and before window.__PC.
    assert (APP.index("function _dmsDeps(") < APP.index("const DmCache = _lzProxy(_dmsMod, 'DmCache');")
            < build.start() < APP.index("function renderMessages(") < APP.index("window.__PC = {"))
    assert DMS.lstrip().startswith("/*") and "window.PCDmsFactory = function(dep){" in DMS


def test_every_entry_point_is_a_forwarder_the_module_returns():
    forwarded = set(re.findall(r"function (\w+)\(\)\{ return _lzRun\(_dmsMod, _dmsLoad, '(\w+)'", APP))
    names = {a for a, b in forwarded if a == b}
    assert names == {a for a, _ in forwarded}
    for must in ("ensureDMs", "ingestDM", "sendDm", "bumpDm", "recountDmUnread", "decryptMsg",
                 "ensureDmInboxList", "wireImgAttach", "_decorateDmFileAtts", "_dmProgress",
                 "_startCordDirectInbox", "sendCordDirectInvite", "cordDirectContext"):
        assert must in names, f"{must} is no longer forwarded to dms.js"
    ret = DMS[DMS.rindex("  return {"):]
    for n in names | {"DmCache"}:
        assert re.search(r"\b" + re.escape(n) + r"\b", ret), f"dms.js does not return {n}"
    assert "const DmCache = _lzProxy(_dmsMod, 'DmCache');" in APP
    assert "const DmCache = {" in DMS and "const DmCache = {" not in APP
    for real in ("async function ensureDMs(){", "async function ingestWrap(ev, live){",
                 "async function sendDm(pk, text){", "function _dmNotify(fromPk, selfNote, text){"):
        assert real in DMS and real not in APP, real


def test_the_dm_rules_moved_intact():
    """CLAUDE.md's DM rules travel with the code: the history read waits for a socket, the live
    subscriptions stay ungated, and the notification shares the push's identity."""
    ensure = DMS[DMS.index("async function ensureDMs(){"):DMS.index("// Unwrap a NIP-17 gift wrap")]
    assert "await Relay.ready(8000)" in ensure
    assert ensure.index("_watchDMs(modern)") < ensure.index("await Relay.ready(8000)"), (
        "the live subscriptions must be started BEFORE (and not gated on) the socket wait")
    notify = DMS[DMS.index("function _dmNotify("):DMS.index("function ingestDM(")]
    assert "tag:'pc-dm', type:'dm'" in notify and "route:'messages'" in notify


def test_the_signer_nip17_paths_stay_with_the_signer():
    for fn in ("async function _nip17unwrapVia(", "async function _nip17wrapVia("):
        assert fn in APP and fn not in DMS, fn


def test_shared_state_stays_in_app_js_and_the_module_can_write_it():
    for decl in ("const dmPeers = new Map();", "let dmActive = null;", "let _dmHandoffScroll = null;",
                 "const _dmFull = new Set();", "const _dmShown = new Map();",
                 "const _DM_INIT = 25, _DM_STEP = 30;", "let _dmScrollTop = false;",
                 "let _dmLoaded=false, _dmUnread=0;", "const _wrapTried = new Set();",
                 "let _dmDone = 0, _dmTotal = 0;", "let _cordDirectOwner='';",
                 "const DISCOVERY_RELAYS = ["):
        assert decl in APP, decl
        assert decl not in DMS, decl
    deps = _deps()
    for n in ("_dmLoaded", "_dmUnread", "_dmDone", "_dmTotal", "_cordDirectOwner"):   # dms.js assigns these
        assert f"get {n}(){{ return {n}; }}, set {n}(v){{ {n} = v; }}" in deps, n
        # (comments name them freely; code must only ever say S.<name>)
        code = re.sub(r"/\*.*?\*/", "", DMS, flags=re.S)
        code = re.sub(r"(?<![:'\"\\])//[^\n]*", "", code)
        assert not re.search(r"(?<![\w$.])" + n + r"(?![\w$])", code.replace("S." + n, "")), n
    for n in ("dmPeers", "_wrapTried", "DISCOVERY_RELAYS"):   # never reassigned: handed over once
        assert re.search(r"\b" + n + r"\b", deps), n
