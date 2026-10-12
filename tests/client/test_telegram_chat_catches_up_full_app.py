"""A TELEGRAM CHAT CATCHES UP ON WHAT IT MISSED -- reopened, or when the live socket comes back.

Reported 2026-10-12: "laptop, phone are in sync but not desktop with the chat with my wife" -- missing the
latest messages. telegram.js fetched a chat ONCE (openChat: `if(!st.msgs.has(id))`) and afterwards fed it
only from the live socket. The socket drops on every node restart (several deploys that day) and reconnects
8 s later; whatever arrived in between was never fetched, and reopening the chat did not ask either. The
desktop had run one session through all of those restarts; the laptop's had started that morning.

The node is the stub in test_telegram_client_full_app.py: it has a message the client was never told about,
exactly as after a dropped socket.
"""
import asyncio
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop
from tests.client.test_telegram_client_full_app import FAKE, _open

ARRIVE = ("__tgMsgs.push({id:%d, chat_id:42, out:false, date:1700000100+%d, text:'%s', sender:'Alice', sender_id:7,"
          " reply_to:0, media:null}); true")
SHOWN = "[...document.querySelectorAll('.tg-msg')].some(m=>m.textContent.includes('%s'))"


@pytest.fixture(scope="module", autouse=True)
def bundle():
    yield from desktop.bundle.__wrapped__()


async def _wait(b, expr, seconds):
    for _ in range(int(seconds * 5)):
        if await b.js(expr):
            return True
        await asyncio.sleep(0.2)
    return False


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
def test_reopening_a_chat_shows_messages_that_arrived_while_it_was_closed():
    async def check(b):
        await _open(b)
        await b.until("document.querySelectorAll('.tg-dialog').length===2")
        await b.js("document.querySelector('.tg-dialog[data-chat=\"42\"]').click()")
        await b.until("document.querySelectorAll('.tg-msg').length===3")
        await b.js("document.querySelector('.tg-dialog[data-chat=\"77\"]').click()")
        await b.js(ARRIVE % (50, 50, 'MISSEDWHILEAWAY'))          # on the node; no socket event reached us
        await b.js("document.querySelector('.tg-dialog[data-chat=\"42\"]').click()")
        assert await _wait(b, SHOWN % 'MISSEDWHILEAWAY', 6), "reopening the chat did not fetch what it missed"
        assert await b.js("document.querySelectorAll('.tg-msg').length") == 4, "the chat now holds duplicates"
    asyncio.run(desktop.with_browser("online", "", check, extra_init=FAKE.replace("state:'none'", "state:'ready'")))


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
def test_the_open_chat_catches_up_when_the_live_socket_comes_back():
    async def check(b):
        await _open(b)
        await b.until("document.querySelectorAll('.tg-dialog').length===2")
        await b.js("document.querySelector('.tg-dialog[data-chat=\"42\"]').click()")
        await b.until("document.querySelectorAll('.tg-msg').length===3")
        await b.until("window.__tgSockets.length>=1")
        n = await b.js("window.__tgSockets.length")
        await b.js("(s=>{s.readyState=3; s.onclose&&s.onclose();})(window.__tgSockets[window.__tgSockets.length-1])")
        await b.js(ARRIVE % (60, 60, 'SENTDURINGTHEGAP'))         # the node restarted; this came in meanwhile
        assert await _wait(b, "window.__tgSockets.length>%d" % n, 15), "the socket never reconnected"
        assert await _wait(b, SHOWN % 'SENTDURINGTHEGAP', 8), "the reconnect did not fetch what came in during the gap"
    asyncio.run(desktop.with_browser("online", "", check, extra_init=FAKE.replace("state:'none'", "state:'ready'")))
