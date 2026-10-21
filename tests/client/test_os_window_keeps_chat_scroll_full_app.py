"""Desktop windows keep a chat's place when another window is focused -- Telegram included.

Reported (Windows app): "Telegram -> Clicking on another window causes the chat message window to scroll up."

The desktop has ONE live #feed and moves it into whichever window is focused; the window that loses it is
parked by moving its content into a slot. Moving a scroll box resets its offset, so os.js measures the chat
lists inside (INNER_SCROLLERS) before the move and puts them back after -- and Telegram's `.tg-msgs` was not
on that list. Worse, the reset to 0 reached Telegram's own scroll listener as "the reader reached the top",
which paged in older messages and anchored the list up there.

The shipped desktop, a Telegram chat long enough to scroll, read to the newest message; then another app is
focused and the Telegram window clicked again:
  * while parked and after coming back, the list is still at the newest message;
  * nothing asked Telegram for older messages.
"""
import asyncio
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop
from tests.client import test_telegram_client_full_app as tg


@pytest.fixture(scope="module", autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


LONG_CHAT = r"""
for(let i=0;i<80;i++) __tgMsgs.push({id:10+i,chat_id:42,out:i%3===0,date:1700000010+i,text:'message number '+i+' with enough words to wrap once or twice in a window',sender:'Alice',sender_id:7,reply_to:0,media:null,reactions:[]});
window.__olderAsks=0;
(()=>{const f=window.fetch;window.fetch=function(u,o){if(String(u).includes('/api/tgc/messages/')&&String(u).includes('before='))window.__olderAsks++;return f(u,o);};})();
"""

AT_BOTTOM = """(()=>{const l=document.querySelector('.tg-msgs');if(!l)return null;
  return {gap:Math.round(l.scrollHeight-l.scrollTop-l.clientHeight),top:Math.round(l.scrollTop),h:l.scrollHeight};})()"""


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
def test_focusing_another_window_does_not_scroll_the_telegram_chat():
    got = {}

    async def check(b):
        await desktop.login(b)
        await b.until("!!window.PCOS && PCOS.isOn() && !!document.querySelector('#os-desk')")
        await b.js("document.getElementById('osfr')?.remove();document.documentElement.classList.remove('osfr-on')")
        await b.js("__PC.switchView('tg')")
        await b.until("!!document.querySelector('.osw .tg-dialog[data-chat=\"42\"]')")
        await b.js("document.querySelector('.osw .tg-dialog[data-chat=\"42\"]').click()")
        await b.until("document.querySelectorAll('.tg-msgs .tg-msg').length>=80")
        await asyncio.sleep(1.2)
        got["before"] = await b.js(AT_BOTTOM)
        await b.js("__PC.switchView('notes')")
        await b.until("PCOS.windows().some(w=>w.appView==='notes')")
        await asyncio.sleep(.8)
        await b.until("!!document.querySelector('.tg-msgs')")
        got["parked"] = await b.js(AT_BOTTOM)
        # Click the Telegram window, as a person would.
        await b.js("""(()=>{const w=[...document.querySelectorAll('.osw')].find(e=>/Telegram/.test((e.querySelector('.osw-title')||{}).textContent||'')).querySelector('.osw-bar');
          for(const t of ['pointerdown','mousedown','pointerup','mouseup','click'])
            w.dispatchEvent(new MouseEvent(t,{bubbles:true,cancelable:true,clientX:w.getBoundingClientRect().left+40,clientY:w.getBoundingClientRect().top+12}));})()""")
        await b.until("[...document.querySelectorAll('.osw.focused .osw-title')].some(t=>/Telegram/.test(t.textContent))")
        await asyncio.sleep(1.0)
        got["back"] = await b.js(AT_BOTTOM)
        got["older"] = await b.js("window.__olderAsks")

    asyncio.run(desktop.with_browser("online", "", check, tg.FAKE.replace("state:'none'", "state:'ready'") + LONG_CHAT))
    assert got["before"] and got["before"]["top"] > 200 and got["before"]["gap"] < 5, ("the chat did not open at the newest message", got)
    assert got["parked"] and got["parked"]["gap"] < 5, ("parking the Telegram window scrolled its chat", got)
    assert got["back"] and got["back"]["gap"] < 5, ("coming back to the Telegram window left the chat scrolled up", got)
    assert got["older"] == 0, ("the park-time reset was read as reaching the top and paged in older messages", got)
