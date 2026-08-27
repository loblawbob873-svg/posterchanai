from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
JS = (ROOT / "static/js/client/concord.js").read_text()


def test_every_room_repaint_restores_bottom_or_the_users_saved_position():
    render = JS.split("function render(){", 1)[1].split("function bind(me)", 1)[0]
    assert "if(current)restoreChatScroll();" in render
    assert "if(returning&&current)restoreChatScroll();" not in render
    assert "st.pinned!==false?box.scrollHeight" in JS


def test_explicit_room_and_channel_entry_mark_the_scroller_pinned():
    server = JS.split("$$('[data-cc-server]')", 1)[1].split("$$('[data-cc-discover]')", 1)[0]
    channel = JS.split("$$('[data-cc-channel]')", 1)[1].split("$$('[data-cc-star]')", 1)[0]

    # Entering a room is an explicit navigation action, even from the mobile drawer. It must not
    # restore an old mid-history position, and the delayed relay hydration must not undo the jump.
    assert "render(); enterChatBottom();" in server
    assert "finally{ if(state.community===i)enterChatBottom(); }" in server
    assert "if(!inDrawer)scrollChatBottom()" not in server
    assert "if(state.community===community&&state.channel===channel)enterChatBottom();" in channel
    assert "[0,60,180,450,900,1600]" in JS


def test_live_append_preserves_exact_offset_after_user_scrolls_up():
    preserve = JS.split("function preserveChatScroll(fn)", 1)[1].split("function restoreChatScroll", 1)[0]
    assert "repaintScrollTop(st.pinned,top,box.scrollHeight)" in preserve
    assert "top+(box.scrollHeight-height)" not in preserve
    assert "function repaintScrollTop(pinned,top,scrollHeight)" in JS
    assert "pinned!==false?scrollHeight" in JS
