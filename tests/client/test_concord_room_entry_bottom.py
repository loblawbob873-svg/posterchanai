from pathlib import Path
import shutil
import subprocess
import pytest

ROOT = Path(__file__).resolve().parents[2]
JS = (ROOT / "static/js/client/concord.js").read_text()
NODE = shutil.which("node") or shutil.which("nodejs")


def test_every_room_repaint_restores_bottom_or_the_users_saved_position():
    render = JS.split("function render(){", 1)[1].split("function bind(me)", 1)[0]
    assert "if(current){restoreChatScroll();" in render
    assert "if(returning&&current)restoreChatScroll();" not in render
    assert "st.pinned!==false?box.scrollHeight" in JS


def test_explicit_room_and_channel_entry_mark_the_scroller_pinned():
    server = JS.split("$$('[data-cc-server]')", 1)[1].split("$$('[data-cc-discover]')", 1)[0]
    activation = JS.split('async function activateJoinedRoom', 1)[1].split('function render()', 1)[0]
    channel = JS.split("$$('[data-cc-channel]')", 1)[1].split("$$('[data-cc-star]')", 1)[0]

    # Entering a room is an explicit navigation action, even from the mobile drawer. It must not
    # restore an old mid-history position, and the delayed relay hydration must not undo the jump.
    assert 'activateJoinedRoom(p,i,inDrawer)' in server
    assert "render();enterChatBottom();" in activation
    assert "if(roomIdentity(active)===identity)enterChatBottom();" in activation
    assert "if(!inDrawer)scrollChatBottom()" not in server
    assert "if(state.community===community&&state.channel===channel)enterChatBottom();" in channel
    assert "[0,60,180,450,900,1600]" in JS


def test_live_append_preserves_exact_offset_after_user_scrolls_up():
    preserve = JS.split("function preserveChatScroll(fn)", 1)[1].split("function restoreChatScroll", 1)[0]
    assert "repaintScrollTop(st.pinned,top,box.scrollHeight)" in preserve
    assert "top+(box.scrollHeight-height)" not in preserve
    assert "function repaintScrollTop(pinned,top,scrollHeight)" in JS
    assert "pinned!==false?scrollHeight" in JS


def test_delayed_prepend_preserves_the_visible_message_anchor():
    preserve = JS[JS.index("function preserveChatScroll("):JS.index("function restoreChatScroll(")]
    assert "anchorId" in preserve
    assert "el.dataset.messageId===anchorId" in preserve
    assert "Number(row.offsetTop)" in preserve


def test_delayed_media_growth_preserves_the_visible_message_anchor():
    watcher = JS[JS.index("function viewportAnchor("):JS.index("function removeMessageRow(")]
    assert "scroller.addEventListener('scroll',remember" in watcher
    assert "el.dataset.messageId===anchor.id" in watcher
    assert "Number(row.offsetTop)" in watcher


@pytest.mark.skipif(not NODE, reason="node is unavailable")
def test_delayed_history_and_media_execute_the_shipped_scroll_contract():
    result = subprocess.run([NODE, str(ROOT / "tests/client/concord_scroll_runtime.mjs")],
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "delayed scroll behavior holds" in result.stdout
