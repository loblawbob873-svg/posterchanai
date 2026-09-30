"""rail.js — the right-column rail (Topics + the Notifications list) — ships with the page, BEFORE
app.js, and is built synchronously while app.js boots.

notifs.js asks `_rightbarShown()` and calls `loadNotifsSoon()` on every arriving notification, in the
same turn; a lazily-loaded module would hand those callers a Promise. So, exactly like notifs.js and
notifview.js: its own <script> tag before app.js, built by app.js at boot, precached by the service
worker, and every entry point a forwarder the module returns.
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CLIENT = ROOT / "static" / "js" / "client"
TEMPLATE = (ROOT / "templates" / "client.html").read_text(encoding="utf-8")
APP = (CLIENT / "app.js").read_text(encoding="utf-8")
RAIL = (CLIENT / "rail.js").read_text(encoding="utf-8")


def _no_stubs(src):
    return re.sub(r"function \w+\(\)\{ return _lzRun\([^\n]*", "", src)


def test_loaded_before_app_js_built_eagerly_and_precached():
    order = [m.group(1) for m in re.finditer(r'<script[^>]*\bsrc="/static/js/client/([\w.-]+\.js)', TEMPLATE)]
    assert order.count("rail.js") == 1 and order.index("rail.js") < order.index("app.js")
    assert re.search(r"^  _railMod\(\);", APP, re.M), "app.js no longer builds rail.js while it boots"
    sw = (CLIENT / "sw.js").read_text(encoding="utf-8")
    shell = sw[sw.index("const SHELL"):sw.index("];", sw.index("const SHELL"))]
    assert "'/static/js/client/rail.js'" in shell


def test_the_rail_moved_and_every_entry_point_is_forwarded():
    forwarded = {a for a, b in re.findall(r"function (\w+)\(\)\{ return _lzRun\(_railMod, _railLoad, '(\w+)'", APP) if a == b}
    for must in ("_rightbarShown", "loadNotifs", "loadNotifsSoon", "loadRightbar", "refreshRightbar", "fetchNotes"):
        assert must in forwarded, must
        assert re.search(r"\b" + must + r"\b", RAIL[RAIL.rindex("  return {"):]), must
    for real in ("async function loadTopics(){", "function loadNotifs(){", "const RB_NOTIF_ROWS=5;"):
        assert real in RAIL and real not in _no_stubs(APP), real
