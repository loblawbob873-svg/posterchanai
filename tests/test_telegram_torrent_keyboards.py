"""Telegram `torrents` shows its category buttons and its 📥 download buttons again.

Found in the 2026-10-08 cleanup (pyflakes: `reply_markup` assigned, never used). When the Telegram handler was
split into modules (2026-06-16, "so every module fits the 16k window"), the torrents branch kept building its
keyboards into a LOCAL `reply_markup` inside _msg_command -- a different function from the one that sends the
reply -- so for four months `torrents` answered "choose a category:" with no categories, and a category's results
with no 📥 buttons. The keyboard now rides in the result and messages.py sends it.
"""
import ast
import asyncio
from pathlib import Path

from app.routers.telegram import messages, messages_command


def _labels(kbd):
    return [b.get("text", "") for row in (kbd or {}).get("inline_keyboard", []) for b in row]


def test_bare_torrents_comes_back_with_the_category_buttons():
    res = asyncio.run(messages_command._msg_command(None, "", [], "123", "torrents", None, None, False, None, "torrents", None))
    assert res.get("type") == "text" and "choose a category" in res["content"]
    labels = _labels(res.get("reply_markup"))
    assert any("Movies" in t for t in labels) and any("TV" in t for t in labels), labels


def test_messages_sends_the_keyboard_the_command_returned():
    src = Path(messages.__file__).read_text()
    tree = ast.parse(src)
    fn = next(n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef) and n.name == "_handle_message")
    body = ast.get_source_segment(src, fn)
    i = body.index("_r = await _msg_command(")
    assert 'reply_markup = result.pop("reply_markup", None)' in body[i:i + 400]
    assert "send_message(chat_id, response_content, reply_markup=reply_markup)" in body
