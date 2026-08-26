from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
OS = (ROOT / "static/js/client/os.js").read_text(encoding="utf-8")
CONCORD = (ROOT / "static/js/client/concord.js").read_text(encoding="utf-8")


def test_messages_handoff_keeps_the_selected_communities_tab():
    assert "if(opened==='messages'&&current==='concord') return 'concord';" in OS


def test_community_and_channel_identity_cross_to_the_other_renderer():
    assert "handoffState,acceptHandoff" in CONCORD
    assert "PCConcord.handoffState()" in OS
    assert "PCConcord.acceptHandoff(p.state)" in OS
    assert "room.communityId||room.naddr||room.url" in CONCORD
    assert "channel:state.channel||'general'" in CONCORD


def test_chat_scroll_pin_crosses_with_the_messages_window():
    assert "pinned:scroll.pinned!==false" in CONCORD
    assert "writeScroll(key,st)" in CONCORD
