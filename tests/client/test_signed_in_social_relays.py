"""Signed-in relay preferences must not disconnect Social from public discovery.

This was visible only on particular PCs because ``relaysEnabled`` is device-local.  A device with
custom/private relays replaced the managed pool and consequently saw only posts those relays held,
while logged-out users on the same deployment continued to see the public timeline.
"""
from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[2]
APP = (ROOT / "static/js/client/app.js").read_text(encoding="utf-8")


def _function(name: str) -> str:
    start = APP.index(f"function {name}(")
    brace = APP.index("{", start)
    depth = 0
    for pos in range(brace, len(APP)):
        if APP[pos] == "{":
            depth += 1
        elif APP[pos] == "}":
            depth -= 1
            if depth == 0:
                return APP[start:pos + 1]
    raise AssertionError(f"function {name} does not close")


def _code(source: str) -> str:
    source = re.sub(r"/\*.*?\*/", "", source, flags=re.S)
    return re.sub(r"(?<!:)//[^\n]*", "", source)


def test_custom_relays_are_unionized_with_managed_discovery_relays():
    body = _code(_function("connectRelays"))
    custom = body.index("userRelays()")
    union = body.index("defaultRelays()", custom)
    configure = body.index("Relay.configure", union)
    assert custom < union < configure
    assert "[...list, ...defaultRelays().filter(Boolean)]" in body


def test_custom_relay_preference_is_not_overwritten_by_bootstrap():
    body = _code(_function("connectRelays"))
    assert "ClientSettings.set('relays'" not in body
    assert "ClientSettings.set('relaysEnabled'" not in body


def test_the_union_uses_the_normal_live_subscription_pool():
    connect = _code(_function("connectRelays"))
    timeline = _code(_function("renderTimeline"))
    assert "Relay.configure({ urls: list, verify: true })" in connect
    assert "Relay.subscribe(timelineFilter()" in timeline
    assert "subscribeFrom" not in timeline
