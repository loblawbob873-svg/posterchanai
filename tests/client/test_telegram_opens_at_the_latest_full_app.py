"""Opening a Telegram chat lands on its LATEST message, and stays there while its pictures load.

Reported: "telegram is having that scroll issue where if you click on conversation, it does not bring
you to latest". The chat was scrolled to the bottom ONCE, when the list was painted — and every photo
in it is an <img> with no size until it loads. Each one that arrives later grows the list below the
reader, so a chat whose recent messages carry pictures opened a screen or more above the newest one.

The pictures are made to arrive late here on purpose (the node fetches them from Telegram, which is
never instant), because a test whose images load in the same frame as the paint cannot see this.
"""
import asyncio
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop
from tests.client.test_telegram_client_full_app import FAKE, _open

# 40 messages; the newest ten are photos. Media URLs are answered with a real 600x400 picture after a
# delay, the way a thumbnail fetched through the node arrives.
MANY = r"""
__tgMsgs.length = 0;
for(let i = 1; i <= 40; i++) __tgMsgs.push({id:i, chat_id:42, out:i % 3 === 0, date:1700000000 + i, text:'message ' + i,
  sender:'Alice', sender_id:7, reply_to:0, media: i > 30 ? {kind:'photo', name:'', size:1234, mime:'image/jpeg'} : null});
(() => {
  const c = document.createElement('canvas'); c.width = 600; c.height = 400;
  const g = c.getContext('2d'); g.fillStyle = '#f0f'; g.fillRect(0, 0, 600, 400);
  const pic = c.toDataURL('image/png'); let n = 0;
  new MutationObserver(ms => { for(const m of ms) for(const node of m.addedNodes){
    const imgs = node.querySelectorAll ? node.querySelectorAll('img[src*="/api/tgc/media/"]') : [];
    for(const img of imgs){ img.removeAttribute('loading'); img.src = ''; const k = n++;
      setTimeout(() => { img.src = pic; }, 300 + 120 * (k % 10)); } } })
    .observe(document, {childList:true, subtree:true});
})();
"""

WHERE = r"""(()=>{const m=document.querySelector('.tg-msgs'); const last=[...document.querySelectorAll('.tg-msg')].pop();
  const r=last&&last.getBoundingClientRect(), mr=m.getBoundingClientRect();
  return {fromBottom:Math.round(m.scrollHeight-m.scrollTop-m.clientHeight), scrollable:m.scrollHeight>m.clientHeight+50,
          lastVisible:!!r&&r.bottom<=mr.bottom+2&&r.top<mr.bottom, loaded:[...m.querySelectorAll('img')].filter(i=>i.naturalWidth>0).length}})()"""


@pytest.fixture(scope="module", autouse=True)
def bundle():
    yield from desktop.bundle.__wrapped__()


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
def test_a_chat_opens_on_its_newest_message_after_the_pictures_load():
    async def check(b):
        await _open(b)
        await b.until("document.querySelectorAll('.tg-dialog').length===2")
        await b.js("document.querySelector('.tg-dialog[data-chat=\"42\"]').click()")
        await b.until("document.querySelectorAll('.tg-msg').length===40")
        await b.until("[...document.querySelectorAll('.tg-msgs img')].filter(i=>i.naturalWidth>0).length===10")
        await asyncio.sleep(.5)
        at = await b.js(WHERE)
        assert at["scrollable"], at
        assert at["fromBottom"] <= 2 and at["lastVisible"], ("the chat did not open on its newest message", at)
        # Reading history is respected: scroll up, let a late picture land, and stay where you are.
        await b.js("document.querySelector('.tg-msgs').scrollTop = 200")
        await asyncio.sleep(.6)
        assert await b.js("document.querySelector('.tg-msgs').scrollTop") < 400, "scrolling up was undone"

    asyncio.run(desktop.with_browser("online", "", check,
                                     extra_init=FAKE.replace("state:'none'", "state:'ready'") + MANY))
