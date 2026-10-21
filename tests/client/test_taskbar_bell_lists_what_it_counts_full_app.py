"""The desktop's taskbar bell lists everything it counts -- Telegram messages included.

Reported (Windows app): "Telegram notifications not appearing in Notifications. You see notification
bell with 1 but no description of what the notification is."

Two halves, both measured on the real bundled desktop:

  * a Telegram message that arrives while the chat is not open lights the bell AND puts a row in the
    taskbar panel ("Telegram · Night City Crew — 1 new message" + the first line); clicking it opens
    THAT chat, and opening the chat takes the row and the count away;
  * a row the bell already counted but the panel never drew (a Concord mention) is listed there too:
    the panel used to draw Nostr notifications + email only, so the bell read 1 over "Nothing new.".
"""
import asyncio
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop
from tests.client import test_telegram_client_full_app as tg

needs_chrome = pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")


@pytest.fixture(scope="module", autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


async def wait(b, expr, secs):
    for _ in range(int(secs * 10)):
        if await b.js(expr):
            return
        await asyncio.sleep(.1)
    raise AssertionError(("timed out", expr))


PANEL = r"""(()=>{const p=document.querySelector('#os-noti');
  return {open:!!p, text:p?p.textContent.replace(/\s+/g,' '):'', tg:p?p.querySelectorAll('.tg-notif').length:0,
          cc:p?p.querySelectorAll('.cc-mention-notif').length:0,
          dot:(document.querySelector('#os-bell .os-dot')||{}).textContent||''};})()"""


@needs_chrome
def test_a_telegram_message_is_listed_under_the_bell_and_opens_its_chat():
    got = {}

    async def check(b):
        await desktop.login(b)
        await b.until("!!document.querySelector('#os-bell')")
        # The background notifier connects on its own a few seconds after boot.
        await wait(b, "__tgSockets.length>=1", 20)
        before = await b.js("__PC.notifUnread()")
        await b.js("""__tgSockets[__tgSockets.length-1].onmessage({data:JSON.stringify({type:'message',
            chat:{id:77,title:'Night City Crew'}, message:{id:9,chat_id:77,out:false,date:Math.floor(Date.now()/1000),
            text:'wake up samurai',sender:'V',media:null}})})""")
        await b.until(f"__PC.notifUnread()==={before}+1")
        await b.js("document.querySelector('#os-bell').click()")
        await b.until("!!document.querySelector('#os-noti .tg-notif')")
        got["panel"] = await b.js(PANEL)
        await b.js("document.querySelector('#os-noti .tg-notif').click()")
        await wait(b, "__PC.isView('tg') && window.PCTelegram && PCTelegram._state.open===77", 15)
        await b.until(f"__PC.notifUnread()==={before}")
        got["after"] = await b.js("({unread:__PC.notifUnread(), ledger:Object.keys(JSON.parse(localStorage.getItem('pc.tg.unread.v1.'+__PC.me().pubkey)||'{}'))})")
        got["before"] = before

    asyncio.run(desktop.with_browser("online", "", check, extra_init=tg.FAKE.replace("state:'none'", "state:'ready'")))
    p = got["panel"]
    assert p["tg"] == 1, ("the Telegram message lit the bell with no row under it", p)
    assert "Telegram · Night City Crew" in p["text"] and "wake up samurai" in p["text"], p
    assert got["after"]["unread"] == got["before"] and got["after"]["ledger"] == [], (
        "opening the chat did not clear its notification", got["after"])


@needs_chrome
def test_every_row_the_bell_counts_is_in_the_panel():
    got = {}

    async def check(b):
        await desktop.login(b)
        await b.until("!!document.querySelector('#os-bell')")
        before = await b.js("__PC.notifUnread()")
        # A Concord mention, written the way concord.js writes it; the bell counted these before.
        await b.js("""localStorage.setItem('pc.concord.mentions.v1.'+__PC.me().pubkey, JSON.stringify({x:{
            room:'wss://relay.example/room', name:'Lounge Chat', channel:'general', ids:['m1'], at:Date.now(), last:'m1'}}));
            __PC.bumpNotif();""")
        got["unread"] = await b.js("__PC.notifUnread()") - before
        await b.js("document.querySelector('#os-bell').click()")
        await b.until("!!document.querySelector('#os-noti')")
        got["panel"] = await b.js(PANEL)

    asyncio.run(desktop.with_browser("online", "", check))
    assert got["unread"] == 1, got
    assert got["panel"]["cc"] == 1 and "Lounge Chat" in got["panel"]["text"], (
        "the bell counts a Concord mention the panel never lists", got["panel"])
