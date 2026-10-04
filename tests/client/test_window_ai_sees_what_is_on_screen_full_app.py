"""The window ✨ panel tells the model what is on screen NOW, and which post each button belongs to.

Measured from the requests the panel sent: the Social window's text read "No posts yet." beside two Reply
buttons -- the TEXT was read when ✨ was pressed, the CONTROLS when Ask was, so anything that loaded in
between had buttons the model could press and words it could not read. And every post's "reply" was
listed bare, so "reply to carol" could only be a guess. Real bundled client as a popped-out Social
window; the request body is what is checked.
"""
import asyncio
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop
from tests.client.test_popped_out_window_has_ai_full_app import _open


@pytest.fixture(scope="module", autouse=True)
def bundle():
    yield from desktop.bundle.__wrapped__()


POSTS = r"""(()=>{const now=Math.floor(Date.now()/1000);
Store.saveEvent({id:'a'.repeat(64),kind:1,pubkey:'b'.repeat(64),created_at:now-50,tags:[],content:'Anyone know a good self-hosted calendar that syncs with my phone?',sig:''});
Store.saveEvent({id:'c'.repeat(64),kind:1,pubkey:'d'.repeat(64),created_at:now-20,tags:[],content:'Meeting with the relay operators moved to Friday 3pm.',sig:''});
return true;})()"""


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
def test_the_request_carries_the_posts_that_loaded_after_the_panel_opened_and_whose_reply_is_whose():
    res = {}

    async def check(b):
        await _open(b, 1100, 760)                      # the panel is open BEFORE the posts arrive
        await b.js(POSTS)
        await b.js("__PC.switchView('global');true")
        await b.until("document.querySelectorAll('#feed .note').length>=2")
        await b.js("document.querySelector('.osw-ai-panel textarea').value='reply to carol';document.querySelector('.osw-ai-panel [data-ai-ask]').click();true")
        await b.until("__req.length>0")
        res["req"] = await b.js("__req[0]")

    asyncio.run(desktop.with_browser("online", "?pcwin=global", check))
    text = res["req"]["windows"][0]["text"]
    assert "relay operators" in text and "self-hosted calendar" in text, ("the model was sent stale window text", text[:300])
    replies = [c for c in res["req"]["controls"] if c["label"].lower() == "reply"]
    assert len(replies) == 2, res["req"]["controls"]
    nears = sorted(c["near"] for c in replies)
    assert any("relay operators" in n for n in nears) and any("self-hosted calendar" in n for n in nears), \
        ("each Reply must say which post it answers", nears)
    # The composer's Post says it belongs to the composer; the page's "New post" (opens an empty box) does
    # not -- measured, the model pressed "New post" to send what it had typed.
    by = {c["label"]: c for c in res["req"]["controls"]}
    assert by["Post"]["near"] == "Write a post" and by["How was your weekend?"]["near"] == "Write a post", by.get("Post")
    assert by.get("New post", {}).get("near", "") == "", by.get("New post")
