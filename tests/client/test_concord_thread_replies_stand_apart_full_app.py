"""Communities: a thread reply is told apart from the main chat, and a busy room can fold threads away.

The Lounge's owner: "I see no differentiation between thread replies and main chat replies (can be
helpful to declutter busy rooms)". Driven for real in the shipped client: reply to a message, then in
the CHANNEL the reply is marked as part of a thread (a thin accent line and an "in thread" tag that
opens it -- NOT indented), and a small button in the channel header, beside the pins, folds them away
leaving only each conversation's first message with its "N replies" button -- kept across redraws.
"hide threaded replies looks very ugly and a screen waste": it was a full-width strip across the top
of the channel, so the test now also asserts there is no such strip and the reply costs no width.
At desktop and phone widths.
"""
import asyncio
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop
from tests.client.test_concord_scroll_never_shows_the_top_full_app import SETUP


@pytest.fixture(scope="module", autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


STATE = r"""(()=>{const q=s=>document.querySelector(s),all=s=>[...document.querySelectorAll(s)];
  const tg=q('#cc-threads-toggle'),list=q('.cc-message-list'),head=tg&&tg.closest('header');
  const r=n=>n&&n.getBoundingClientRect();
  const reply=all('.cc-message').find(m=>/my reply text/.test(m.textContent));
  const plain=all('.cc-message').find(m=>/message number 59/.test(m.textContent));
  const root=q('.cc-message[data-message-id^="m050"]');
  return {strip:!!q('.cc-threads-bar'), toggle:tg?tg.getAttribute('aria-label'):null, inHeader:!!head,
    toggleH:tg?Math.round(r(tg).height):0, pressed:tg?tg.getAttribute('aria-pressed'):null,
    replyShown:!!reply, replyMarked:!!(reply&&reply.classList.contains('cc-in-thread')),
    replyLeft:reply?Math.round(r(reply).left):0, plainLeft:plain?Math.round(r(plain).left):0,
    replyW:reply?Math.round(r(reply).width):0, plainW:plain?Math.round(r(plain).width):0,
    replyOpens:!!(reply&&reply.querySelector('.cc-in-thread-tag[data-cc-thread]')),
    tag:reply&&reply.querySelector('.cc-in-thread-tag')?reply.querySelector('.cc-in-thread-tag').textContent:null,
    rootMarked:!!(root&&root.classList.contains('cc-in-thread')),
    rootCount:root&&root.querySelector('.cc-thread-open')?root.querySelector('.cc-thread-open').textContent.trim():null,
    messages:all('.cc-message').length}})()"""


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
@pytest.mark.parametrize("width", [1280, 390])
def test_thread_replies_are_marked_in_the_channel_and_can_be_folded_away(width):
    res = {}

    async def check(b):
        await b.call("Emulation.setDeviceMetricsOverride", dict(width=width, height=850, deviceScaleFactor=1, mobile=width < 600))
        await desktop.login(b)
        await b.js("try{ localStorage.removeItem('pc.concord.hideThreadReplies'); if(window.PCOS && PCOS.isOn()) PCOS.exit(); }catch(_){}")
        await b.js(SETUP)
        await b.js("(()=>{const c=document.querySelector('[data-cc-channel]');if(c&&!document.querySelector('.cc-app.show-chat .cc-message'))c.click();})()")
        await b.until("document.querySelectorAll('.cc-message').length>=50")
        res["before"] = await b.js("!!document.querySelector('#cc-threads-toggle') || !!document.querySelector('.cc-threads-bar')")
        await b.js("document.querySelector('.cc-message[data-message-id^=\"m050\"] [data-cc-reply]').click()")
        await b.js("(()=>{const i=document.querySelector('#cc-input');i.value='my reply text';i.dispatchEvent(new Event('input',{bubbles:true}));})()")
        await b.js("document.querySelector('#cc-send').click()")
        await b.until("!!document.querySelector('#cc-threads-toggle')")
        await asyncio.sleep(.3)
        res["shown"] = await b.js(STATE)
        # The reply's "in thread" line opens the thread; Back returns to the channel.
        await b.js("(()=>{const m=[...document.querySelectorAll('.cc-message')].find(x=>/my reply text/.test(x.textContent));m.querySelector('.cc-in-thread-tag[data-cc-thread]').click();})()")
        await b.until("!!document.querySelector('.cc-thread-bar')")
        res["thread"] = await b.js("({bar:!!document.querySelector('.cc-thread-bar'),n:document.querySelectorAll('.cc-message').length,marked:document.querySelectorAll('.cc-in-thread').length})")
        await b.js("document.querySelector('#cc-thread-back').click()")
        await b.until("!document.querySelector('.cc-thread-bar') && !!document.querySelector('#cc-threads-toggle')")
        # Fold: only the conversation's first message stays, with its count.
        await b.js("document.querySelector('#cc-threads-toggle').click()")
        await asyncio.sleep(.3)
        res["folded"] = await b.js(STATE)
        # Still folded after the room redraws (a new message, a profile, a reload of the view).
        await b.js("(()=>{const c=document.querySelector('[data-cc-channel]');c&&c.click();})()")
        await asyncio.sleep(.5)
        res["refolded"] = await b.js(STATE)
        await b.js("document.querySelector('#cc-threads-toggle').click()")
        await asyncio.sleep(.3)
        res["unfolded"] = await b.js(STATE)

    asyncio.run(desktop.with_browser("online", "", check))
    s = res["shown"]
    assert res["before"] is False, "a channel with no threads should not carry a threads toggle"
    assert not s["strip"], "the full-width threads strip is back -- a row of screen for one switch"
    assert s["toggle"] == "Hide thread replies (1)" and s["inHeader"] and s["toggleH"] <= 44, ("the toggle is not a small header button", s)
    assert s["replyShown"] and s["replyMarked"] and s["replyOpens"] and s["tag"] and "in thread" in s["tag"], s
    assert abs(s["replyLeft"] - s["plainLeft"]) <= 2 and abs(s["replyW"] - s["plainW"]) <= 2, ("a thread reply is indented: wasted width", s)
    assert not s["rootMarked"] and s["rootCount"] == "1 reply", s
    assert res["thread"]["bar"] and res["thread"]["marked"] == 0, ("inside the thread nothing should be marked", res["thread"])
    f = res["folded"]
    assert not f["replyShown"] and f["rootCount"] == "1 reply" and f["toggle"] == "Show thread replies (1)" and f["pressed"] == "true", ("folding did not hide the reply", f)
    assert f["messages"] == s["messages"] - 1, f
    assert not res["refolded"]["replyShown"], ("the fold did not survive a redraw", res["refolded"])
    assert res["unfolded"]["replyShown"] and res["unfolded"]["toggle"] == "Hide thread replies (1)", res["unfolded"]
