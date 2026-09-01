"""Conversation presentation for Email stays usable with a mouse, keyboard, and phone."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
APP = (ROOT / "static/js/client/app.js").read_text()
CSS = (ROOT / "static/css/client.css").read_text()


def test_thread_messages_have_collapsed_context_and_accessible_headers():
    assert 'class="mm-preview muted"' in APP
    assert 'role="button" tabindex="0" aria-expanded=' in APP
    assert "if(e.key==='Enter'||e.key===' ')" in APP


def test_thread_ends_with_reply_and_forward_actions():
    assert 'data-thread-reply="reply"' in APP
    assert 'data-thread-reply="forward"' in APP
    assert "this.action(b.dataset.threadReply,latest" in APP


def test_desktop_uses_conversation_cards_and_mobile_keeps_touch_actions_visible():
    assert ".mail-msg{border:1px solid var(--line);border-radius:12px" in CSS
    assert ".mail-thread-reply .btn{flex:1;min-height:44px}" in CSS
