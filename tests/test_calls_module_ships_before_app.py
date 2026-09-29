"""calls.js ships with the page, BEFORE app.js, and is built synchronously while app.js boots.

The voice/video call code (and remote desktop) moved out of app.js into calls.js. Unlike the screens
that load on first use, several of its entry points cannot wait for a file: an incoming call has to
ring, and answering or hanging up from the ongoing-call notification / Android's native call UI
arrives as a callback that expects the call to end NOW. So:

  * templates/client.html loads calls.js with its own <script> tag, BEFORE app.js (the bundle builds,
    desktop/build-www.sh and mobile/build-www.sh, copy every client .js and render this template, so
    they inherit the order);
  * app.js builds the module (`_callsMod()`) at the point where the code used to sit, so its
    top-level statements run in the order they always did and every entry point is synchronous;
  * the service worker precaches it, or an offline PWA boots without calls at all.
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CLIENT = ROOT / "static" / "js" / "client"
TEMPLATE = (ROOT / "templates" / "client.html").read_text(encoding="utf-8")


def _script_order(html):
    return [m.group(1) for m in re.finditer(r'<script[^>]*\bsrc="/static/js/client/([\w.-]+\.js)', html)]


def test_calls_js_is_loaded_before_app_js():
    order = _script_order(TEMPLATE)
    assert "calls.js" in order, "templates/client.html does not load calls.js"
    assert "app.js" in order
    assert order.index("calls.js") < order.index("app.js"), (
        "calls.js must be loaded BEFORE app.js — app.js builds it synchronously while it boots")
    assert order.count("calls.js") == 1


def test_calls_js_is_precached_by_the_service_worker():
    sw = (CLIENT / "sw.js").read_text(encoding="utf-8")
    shell = sw[sw.index("const SHELL"):sw.index("];", sw.index("const SHELL"))]
    assert "'/static/js/client/calls.js'" in shell


def test_app_js_builds_calls_eagerly_not_on_first_use():
    app = (CLIENT / "app.js").read_text(encoding="utf-8")
    assert "_lzGet('calls.js', 'PCCallsFactory', _callsDeps)" in app
    # A bare `_callsMod();` statement at the IIFE's top level — not inside an entry point.
    assert re.search(r"^  _callsMod\(\);", app, re.M), "app.js no longer builds calls.js while it boots"
    # ...and it is built AFTER its deps function exists and BEFORE window.__PC is published.
    assert app.index("function _callsDeps(") < app.index("\n  _callsMod();") < app.index("window.__PC = {")
    calls = (CLIENT / "calls.js").read_text(encoding="utf-8")
    assert calls.lstrip().startswith("/*") and "window.PCCallsFactory = function(dep){" in calls


def test_every_call_entry_point_is_a_forwarder_the_module_returns():
    """Every function app.js (or window.__PC, the Android bridge, other modules through __PC) calls
    by name must be forwarded to calls.js, and calls.js must return it."""
    app = (CLIENT / "app.js").read_text(encoding="utf-8")
    calls = (CLIENT / "calls.js").read_text(encoding="utf-8")
    forwarded = set(re.findall(r"function (\w+)\(\)\{ return _lzRun\(_callsMod, _callsLoad, '(\w+)'", app))
    names = {a for a, b in forwarded if a == b}
    assert names == {a for a, _ in forwarded}
    for must in ("startCall", "startGroupCall", "startCallSignaling", "renderCalls", "startRemoteDesktop",
                 "setRemoteDesktopArmed", "setRemoteDesktopHost", "_hangupActiveCall"):
        assert must in names, f"{must} is no longer forwarded to calls.js"
    ret = calls[calls.rindex("  return {"):]
    for n in names:
        assert re.search(r"\b" + re.escape(n) + r"\b", ret), f"calls.js does not return {n}"
    # The Android ongoing-call notification's Hang up reaches the module, and a group call is left
    # as a group call.
    i = app.index("_CallP.addListener('callAction'")
    assert "_hangupActiveCall()" in app[i:i + 200]
    j = calls.index("function _hangupActiveCall(){")
    assert "if(_room) _roomLeave(); else _hangup(false);" in calls[j:j + 120]
