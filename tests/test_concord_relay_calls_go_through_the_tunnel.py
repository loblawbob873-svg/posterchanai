"""Concord reaches relays only through two WRAPPED doors (static/js/client/concord.js): the app's dependency
object (`PC()` -> `cordDeps`) and the Relay object (`window.PCConcordRelay` -> `cordRelay`), each adding the
tunnel's `via` to every outside-relay method. A new call site cannot forget it -- so this checks the doors, not
the calls: that nothing in concord.js reaches `window.__PC` or `window.Relay` except through them, and that the
wrappers cover every method that opens a socket to a relay. The browser half
(tests/client/test_concord_talks_only_to_this_node_full_app.py) proves the dials."""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONCORD = (ROOT / "static/js/client/concord.js").read_text(encoding="utf-8")
APP = (ROOT / "static/js/client/app.js").read_text(encoding="utf-8")
RELAY = (ROOT / "static/js/client/relay.js").read_text(encoding="utf-8")


def _body(name):
    i = CONCORD.index(f"function {name}(")
    return CONCORD[i:CONCORD.index("\n  }\n", i)]


def test_concord_reaches_the_app_and_the_relay_only_through_the_wrappers():
    assert "const PC=()=>cordDeps(window.__PC);" in CONCORD
    direct_pc = [m.start() for m in re.finditer(r"window\.__PC\b", CONCORD)]
    allowed = {CONCORD.index("const PC=()=>cordDeps(window.__PC);") + len("const PC=()=>cordDeps("),
               CONCORD.index("cordVia(window.__PC)") + len("cordVia(")}
    assert set(direct_pc) <= allowed, "concord.js reads window.__PC around the wrapper"
    for m in re.finditer(r"window\.Relay\b", CONCORD):
        before = CONCORD[max(0, m.start() - 26):m.start()]
        assert before.endswith("window.PCConcordRelay||") or "cordRelay(" in CONCORD[m.start() - 12:m.start()], (
            "window.Relay used without the wrapper: " + CONCORD[m.start() - 60:m.end() + 40])


def test_the_wrappers_cover_every_method_that_dials_a_relay():
    deps, relay = _body("cordDeps"), _body("cordRelay")
    for k in ("relayQueryFrom", "relayPublishTo", "relayPublishRoom", "publishNip29Authed"):
        assert k + ":" in deps, f"cordDeps does not route {k}"
    for k in ("queryFrom", "publishTo", "subscribeFrom", "publishAuthed"):
        assert k + ":" in relay, f"cordRelay does not route {k}"
    # ...and every Relay method that opens an EXTERNAL socket takes `via` (a new one must join both lists).
    for k in ("queryFrom", "publishTo", "subscribeFrom", "publishAuthed"):
        sig = RELAY[RELAY.index(f"    {'async ' if k in ('publishTo', 'publishAuthed') else ''}{k}("):][:400]
        assert "via" in sig.split(")")[0] + sig.split("{", 1)[1][:300], f"Relay.{k} takes no via"


def test_the_app_doors_pass_via_down():
    assert "relayPublishTo: (relays, ev, opts) => Relay.publishTo(relays, ev, opts)" in APP
    assert "relayPublishRoom: (relays, ev, authScope=null, via=null, opts=null) => {" in APP
    assert "authScope:a, via});" in APP
    assert "publishNip29Authed: async (relay, template, via=null) =>" in APP
    assert ",{via});" in APP
