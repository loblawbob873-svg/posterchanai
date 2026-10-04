"""Communities: thread replies collapse under the message they answer.

The Lounge's owner, in order: "I see no differentiation between thread replies and main chat replies
(can be helpful to declutter busy rooms)"; then "hide threaded replies looks very ugly and a screen
waste" (a full-width strip); then "make the thread collapse under the message being responded to.
Right now, it is treated separately on the back end but everything looks almost exactly the same in
the chat". Driven for real in the shipped client, at desktop and phone widths:

  * by default a reply leaves the channel's flow; the message it answers carries ONE compact summary
    row (count, who, the last reply's words) -- no strip anywhere;
  * the summary expands the replies in place, DIRECTLY under that message (before the next one), and
    collapses them again; replying from the channel opens the thread, so the sender sees the reply land
    (collapsed, it would vanish as it is sent); "Open thread" opens the thread view;
  * the header button switches to the flat view (every reply in the chat, marked, full width) and back.
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
  const list=q('.cc-message-list'), kids=list?[...list.children]:[];
  const idx=el=>kids.indexOf(el);
  const reply=all('.cc-message').find(m=>/my reply text/.test(m.textContent));
  const root=q('.cc-message[data-message-id^="m050"]'), next=q('.cc-message[data-message-id^="m051"]');
  const plain=q('.cc-message[data-message-id^="m059"]');
  const sum=root&&root.nextElementSibling&&root.nextElementSibling.classList.contains('cc-thread-summary')?root.nextElementSibling:null;
  const tg=q('#cc-threads-toggle'), r=n=>n&&n.getBoundingClientRect();
  return {strip:!!q('.cc-threads-bar'), toggle:tg?tg.getAttribute('aria-label'):null, toggleInHeader:!!(tg&&tg.closest('header')),
    summary:sum?sum.textContent.replace(/\s+/g,' ').trim():null, summaryH:sum?Math.round(r(sum).height):0,
    expanded:sum?sum.querySelector('[data-cc-thread-expand]').getAttribute('aria-expanded'):null,
    replyShown:!!reply, replyUnderRoot:!!(reply&&root&&next&&idx(reply)>idx(root)&&idx(reply)<idx(next)),
    replyAtBottom:!!(reply&&plain&&idx(reply)>idx(plain)),
    replyMarked:!!(reply&&reply.classList.contains('cc-in-thread')), replyInline:!!(reply&&reply.classList.contains('cc-thread-inline')),
    replyW:reply?Math.round(r(reply).width):0, plainW:plain?Math.round(r(plain).width):0,
    oldCountButton:!!(root&&root.querySelector('.cc-thread-open')),
    messages:all('.cc-message').length}})()"""


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
@pytest.mark.parametrize("width", [1280, 390])
def test_replies_collapse_under_the_message_they_answer(width):
    res = {}

    async def check(b):
        await b.call("Emulation.setDeviceMetricsOverride", dict(width=width, height=850, deviceScaleFactor=1, mobile=width < 600))
        await desktop.login(b)
        await b.js("try{ localStorage.removeItem('pc.concord.threadsInChat'); if(window.PCOS && PCOS.isOn()) PCOS.exit(); }catch(_){}")
        await b.js(SETUP)
        await b.js("(()=>{const c=document.querySelector('[data-cc-channel]');if(c&&!document.querySelector('.cc-app.show-chat .cc-message'))c.click();})()")
        await b.until("document.querySelectorAll('.cc-message').length>=50")
        res["before"] = await b.js("!!document.querySelector('#cc-threads-toggle') || !!document.querySelector('.cc-thread-summary')")
        await b.js("document.querySelector('.cc-message[data-message-id^=\"m050\"] [data-cc-reply]').click()")
        await b.js("(()=>{const i=document.querySelector('#cc-input');i.value='my reply text';i.dispatchEvent(new Event('input',{bubbles:true}));})()")
        await b.js("document.querySelector('#cc-send').click()")
        await b.until("!!document.querySelector('.cc-thread-summary')")
        await asyncio.sleep(.3)
        # Replying from the channel opens that thread: the sender sees the reply land under its message.
        res["expanded"] = await b.js(STATE)
        await b.js("document.querySelector('[data-cc-thread-expand]').click()")
        await asyncio.sleep(.3)
        res["collapsed"] = await b.js(STATE)
        await b.js("document.querySelector('[data-cc-thread-expand]').click()")
        await b.until("[...document.querySelectorAll('.cc-message')].some(m=>/my reply text/.test(m.textContent))")
        # A redraw (a channel click repaints everything) keeps it where it is.
        await b.js("(()=>{const c=document.querySelector('[data-cc-channel]');c&&c.click();})()")
        await asyncio.sleep(.5)
        res["redrawn"] = await b.js(STATE)
        await b.js("document.querySelector('[data-cc-thread-expand]').click()")
        await asyncio.sleep(.3)
        res["recollapsed"] = await b.js(STATE)
        # Open thread -> the thread view; Back -> the channel.
        await b.js("document.querySelector('.cc-ts-open').click()")
        await b.until("!!document.querySelector('.cc-thread-bar')")
        res["thread"] = await b.js("({n:document.querySelectorAll('.cc-message').length, reply:[...document.querySelectorAll('.cc-message')].some(m=>/my reply text/.test(m.textContent))})")
        await b.js("document.querySelector('#cc-thread-back').click()")
        await b.until("!document.querySelector('.cc-thread-bar') && !!document.querySelector('.cc-thread-summary')")
        # Flat view from the header, and back.
        await b.js("document.querySelector('#cc-threads-toggle').click()")
        await asyncio.sleep(.3)
        res["flat"] = await b.js(STATE)
        await b.js("document.querySelector('#cc-threads-toggle').click()")
        await asyncio.sleep(.3)
        res["back"] = await b.js(STATE)

    asyncio.run(desktop.with_browser("online", "", check))
    c = res["collapsed"]
    assert res["before"] is False, "a channel with no threads should carry no thread controls"
    assert not c["strip"], "the full-width strip is back -- a row of screen for one switch"
    assert not c["replyShown"], ("the reply still sits in the main chat instead of under its message", c)
    assert c["summary"] and "1 reply" in c["summary"] and "my reply text" in c["summary"] and c["expanded"] == "false", ("no summary under the message answered", c)
    assert c["summaryH"] <= 40, ("the summary row is not compact", c)
    assert not c["oldCountButton"], c
    assert c["toggle"] == "Show replies in the chat (1)" and c["toggleInHeader"], c
    e = res["expanded"]
    assert e["replyShown"] and e["replyUnderRoot"] and e["replyInline"] and e["expanded"] == "true", ("expanding did not put the reply under its message", e)
    assert res["redrawn"]["replyUnderRoot"], ("a redraw moved the opened reply", res["redrawn"])
    assert not res["recollapsed"]["replyShown"], ("collapsing did not hide it again", res["recollapsed"])
    assert res["thread"]["reply"], ("Open thread did not show the thread", res["thread"])
    f = res["flat"]
    assert f["replyShown"] and f["replyAtBottom"] and f["replyMarked"] and not f["replyInline"], ("the flat view is wrong", f)
    assert abs(f["replyW"] - f["plainW"]) <= 2, ("in the flat view a reply is indented: wasted width", f)
    assert f["toggle"] == "Collapse replies under their messages (1)" and f["summary"] is None, f
    assert not res["back"]["replyShown"] and res["back"]["summary"], ("switching back did not collapse", res["back"])
