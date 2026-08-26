from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
JS = (ROOT / "static/js/client/concord.js").read_text()
CSS = (ROOT / "static/css/concord.css").read_text()


def test_reply_preview_scrolls_to_and_highlights_original():
    handler = JS.split("$$('.cc-message-reply')", 1)[1].split("const toggleReaction", 1)[0]
    assert "message.reply&&String(message.reply.id" in handler
    assert "scrollIntoView({block:'center',behavior:'smooth'})" in handler
    assert "st.pinned=false" in handler
    assert "cc-message-target" in handler
    assert "e.key==='Enter'||e.key===' '" in handler


def test_missing_original_is_explained_instead_of_ignored():
    assert "original message is not in the loaded room history" in JS
    assert "@keyframes cc-target-flash" in CSS
