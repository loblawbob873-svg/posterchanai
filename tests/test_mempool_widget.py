"""The Bitcoin blocks widget: pending on the left, confirmed on the right.

Run: venv-unified/bin/python -m pytest tests/test_mempool_widget.py

Asked for as "add desktop widget for mempool.space: Just the pending/confirmed block display", and
"make it look nice".

Three things decide whether this is a good widget or a bad one, and none of them is the drawing:

  * THE SERVER FETCHES IT. The client could call mempool.space directly — it is free and needs no key
    — but then every reader's IP goes to a third party on a timer, which is the opposite of what the
    rest of this app does, and a client behind Tor would be leaving through its own exit anyway. One
    node-side fetch, cached, means the upstream sees this server and nothing else. It is also the only
    way it stays cheap: a widget on twenty desktops is twenty pollers, or two requests a minute.
  * WHAT IS RETURNED IS NOT WHAT IS FETCHED. The upstream answers carry full block extras, pool
    objects and fee histograms; a tile draws four numbers. Reducing it here is the difference between
    a few hundred bytes on the wire and a few hundred KB, twice a minute, per open desktop.
  * IT FITS. A row that is always clipped at the right edge reads as broken rather than scrollable,
    and a widget lives at four named sizes and is resizable on top of that.
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OSJS = (ROOT / "static" / "js" / "client" / "os.js").read_text(encoding="utf-8")
SVC = (ROOT / "app" / "services" / "mempool_service.py").read_text(encoding="utf-8")
CSS = (ROOT / "static" / "css" / "client.css").read_text(encoding="utf-8")


def _widget():
    """The whole `mempool:` entry — sliced to the NEXT registry entry rather than a guessed window,
    which is how the first version of this test missed half of it."""
    i = OSJS.index("    mempool: {")
    return OSJS[i:OSJS.index("\n    calendar: {", i)]


def test_the_client_never_talks_to_mempool_space():
    """An IP leak on a timer, and one this app would be alone in accepting."""
    body = _widget()
    assert "'/api/mempool/blocks'" in body
    # The ONLY mention allowed is the link a tile opens — everything else must go through this node.
    assert "openExternal('https://mempool.space')" in body
    others = [m for m in re.findall(r"[\w.]*mempool\.space[\w./]*", body)
              if m != "mempool.space"]
    assert not others, f"the widget reaches the upstream directly: {others}"
    assert "fetch('https://" not in body and "_wgtJson('https://" not in body


def test_it_is_offered_in_the_picker_and_registered():
    from json import loads  # noqa: F401  (kept parallel with the other widget tests)
    assert re.search(r"\n    mempool: \{", OSJS), "the widget is not in the registry"
    main = (ROOT / "app" / "main.py").read_text(encoding="utf-8")
    assert "app.include_router(mempool.router)" in main, "the endpoint is not mounted"


def test_the_node_caches_it_so_a_desk_full_of_these_costs_two_requests_a_minute():
    assert "_TTL = 30.0" in SVC
    assert "_lock" in SVC and "async with _lock:" in SVC, (
        "ten widgets loading at once would make ten upstream requests")
    # …and the double-check inside the lock, or the queue behind it all fetch anyway.
    i = SVC.index("async with _lock:")
    assert "_cache.get(base)" in SVC[i:i + 300]


def test_a_blip_serves_the_last_good_answer():
    """A block explorer that blanks on a failed fetch is less use than one that is thirty seconds
    behind and says so."""
    assert 'dict(hit[1], stale=True)' in SVC
    assert "d.stale" in _widget(), "the widget does not say when it is showing a stale answer"


def test_the_payload_is_reduced_on_the_server():
    """Four numbers per tile against a full block object with its pool and fee histogram."""
    for field in ('"median"', '"lo"', '"hi"', '"tx"', '"height"', '"ts"', '"pool"'):
        assert field in SVC
    assert "[:3]" in SVC and "[:4]" in SVC, "the upstream list is sent whole"


def test_an_operator_can_point_it_at_their_own_node():
    """Which is the entire point of running one."""
    from app.schemas import SettingsResponse
    assert "mempool_api_base" in SettingsResponse.model_fields
    assert 'settings_store.get("mempool_api_base")' in SVC


def test_the_row_fits_the_widget_it_is_in():
    """A row always clipped at the right edge reads as broken rather than scrollable, and this is
    resizable on top of four named sizes."""
    body = _widget()
    assert "box.clientWidth" in body, "the tile count is fixed regardless of the width"
    assert "Math.floor((w - 18) / 71)" in body
    assert "Math.min(3, Math.ceil(slots / 2))" in body, (
        "an odd number of slots should favour the CONFIRMED side — the tip is the interesting end")


def test_a_projected_block_does_not_look_like_a_real_one():
    assert ".mp-b.p{border-style:dashed" in CSS
    body = _widget()
    assert "'~' + _mpFee(o.median)" in body, "a projected fee is shown as if it were settled"


def test_the_fee_colour_is_bucketed_not_a_gradient():
    """The question is "is it cheap right now", and four steps answer it more clearly than a ramp."""
    i = OSJS.index("function _mpHue(")
    body = OSJS[i:i + 500]
    assert body.count("return '") >= 5
    assert "hsl(var(--mp-h)" in CSS, "the tile ignores the hue it is given"
