from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
APP = (ROOT / "static/js/client/app.js").read_text(encoding="utf-8")


def _reconnect_body():
    start = APP.index("Relay.onReconnect = ()=>{")
    return APP[start:APP.index("\n    };", start) + 7]


def test_timeline_reconnect_uses_visible_card_anchor_without_restarting_view():
    body = _reconnect_body()
    assert "if(VIEW==='home'||VIEW==='global') _drawTimeline(true)" in body
    assert "else renderView(false)" in body
    assert "renderView(true)" not in body


def test_anchor_preserving_draw_keeps_inline_composer_node_alive():
    draw = APP[APP.index("function _drawTimeline(preserveScroll)"):]
    draw = draw[:draw.index("\n  // Bring `box`'s cards")]
    assert "const place=preserveScroll?_tlAnchor(feed):null" in draw
    assert "if(!notesEl)" in draw
    assert "_restoreTlAnchor(feed, place)" in draw
