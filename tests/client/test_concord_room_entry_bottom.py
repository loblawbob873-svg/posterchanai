from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
JS = (ROOT / "static/js/client/concord.js").read_text()


def test_every_room_repaint_restores_bottom_or_the_users_saved_position():
    render = JS.split("function render(){", 1)[1].split("function bind(me)", 1)[0]
    assert "if(current)restoreChatScroll();" in render
    assert "if(returning&&current)restoreChatScroll();" not in render
    assert "st.pinned!==false?box.scrollHeight" in JS


def test_explicit_room_and_channel_entry_mark_the_scroller_pinned():
    assert "render(); if(!inDrawer)scrollChatBottom();" in JS
    assert "mobileDrawerOpen=false; render(); scrollChatBottom();" in JS
