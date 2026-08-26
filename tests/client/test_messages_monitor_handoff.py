from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
OS = (ROOT / "static/js/client/os.js").read_text(encoding="utf-8")
CONCORD = (ROOT / "static/js/client/concord.js").read_text(encoding="utf-8")


def test_messages_handoff_keeps_the_selected_communities_tab():
    assert "(opened==='messages'||opened==='concord')" in OS
    assert "(current==='messages'||current==='concord')" in OS
    assert "messagesTab==='concord'" in OS


def test_community_and_channel_identity_cross_to_the_other_renderer():
    assert "handoffState,acceptHandoff" in CONCORD
    assert "PCConcord.handoffState()" in OS
    assert "PCConcord.acceptHandoff(p.state)" in OS
    assert "room.communityId||room.naddr||room.url" in CONCORD
    assert "channel:state.channel||'general'" in CONCORD


def test_handoff_uses_one_canonical_messages_window_then_restores_its_tab():
    assert "return {view:handoffIdentity(w),messagesTab" in OS
    assert "if(p.messagesTab==='concord'||p.messagesTab==='messages')" in OS
    assert "w.appView=p.messagesTab;w.appPath='';repainting++" in OS
    assert "PC().switchView&&PC().switchView(p.messagesTab)" in OS


def test_chat_scroll_pin_crosses_with_the_messages_window():
    assert "pinned:scroll.pinned!==false" in CONCORD
    assert "writeScroll(key,st)" in CONCORD


def test_dragged_communities_window_is_reused_when_direct_messages_is_clicked():
    """Exact regression: Messages → Communities → other monitor → Direct remains one frame."""
    assert "if(p.messagesTab==='concord'||p.messagesTab==='messages')" in OS
    opened = OS[OS.index("function openApp(view, label, icon, render, noFeed, direct)"):]
    opened = opened[:opened.index("function ", 20)]
    assert "wins.find(w => sameAppWindow(w.view, view))" in opened
    assert "focusWin(existing, false); existing.appView = view" in opened
    assert "PC().switchView && PC().switchView(view)" in opened
