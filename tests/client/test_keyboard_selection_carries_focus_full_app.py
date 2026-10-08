"""j/k (Vim mode) move the highlight AND the keyboard with it, once a post card has focus (code review, 2026-10-07).

Post cards are Tab stops (cards.js), so a click inside one -- on an image, say -- focuses that card. Moving the
selection with j/k left focus on it, and the key handler then saw "focus is on another row": every post key did
nothing, and Enter opened the card that had focus instead of the highlighted one. Real key presses (CDP), in the
shipped client.
"""
import asyncio
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop
from tests.client.test_keys_module_boots_full_app import _press


@pytest.fixture(scope="module", autouse=True)
def bundle():
    yield from desktop.bundle.__wrapped__()


SEED = r"""(()=>{
 const sk=new Uint8Array(32).fill(9), pk=NostrTools.getPublicKey(sk), now=Math.floor(Date.now()/1000);
 const root=NostrTools.finalizeEvent({kind:1,created_at:now-900,tags:[],content:'Root post'},sk);
 Store.saveEvent(root); let parent=root;
 for(let i=0;i<5;i++){ const r=NostrTools.finalizeEvent({kind:1,created_at:now-800+i*10,tags:[['e',root.id,'','root'],['e',parent.id,'','reply'],['p',pk]],content:'Reply '+i},sk); Store.saveEvent(r); parent=r; }
 window.__root=root.id; return true; })()"""


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
def test_the_keyboard_follows_the_highlight_off_a_focused_card():
    got = {}

    async def check(b):
        await b.call("Emulation.setDeviceMetricsOverride", {"width": 1280, "height": 900, "deviceScaleFactor": 1, "mobile": False})
        await desktop.login(b)
        await b.js("try{ if(window.PCOS && PCOS.isOn()) PCOS.exit(); }catch(_){}; true")
        await b.js("ClientSettings.set('vimKeys', true); true")   # post-by-post movement is Vim mode's j/k
        await b.js(SEED)
        await b.js("__PC.openThread(__root);true")
        await b.until("document.querySelectorAll('#feed article.note[data-id]').length>=4")
        await asyncio.sleep(1)
        # The highlight is moved down first, THEN a click focuses a card in ANOTHER row (the first one) -- the
        # reviewer's case: the focused card is not in the row the highlight leaves.
        await _press(b, 'j', 'KeyJ', 74); await asyncio.sleep(.3)
        await _press(b, 'j', 'KeyJ', 74); await asyncio.sleep(.3)
        await b.js("(()=>{const c=document.querySelector('#feed article.note[data-id]'); c.focus(); window.__first=c.dataset.id;})();true")
        await _press(b, 'j', 'KeyJ', 74)
        await asyncio.sleep(.3)
        # In a thread the selectable row is a .thread-node WRAPPER around the card.
        got["sel"] = await b.js("(()=>{const s=document.querySelector('#feed .sel'); if(!s) return ''; const c=s.matches('article.note[data-id]')?s:s.querySelector('article.note[data-id]'); return c?c.dataset.id:''})()")
        got["focused"] = await b.js("(document.activeElement&&document.activeElement.dataset&&document.activeElement.dataset.id)||''")
        got["first"] = await b.js("__first")

    asyncio.run(desktop.with_browser("online", "", check))
    assert got["sel"] and got["sel"] != got["first"], ("j did not move the highlight", got)
    assert got["focused"] != got["first"], ("focus stayed on the card that was clicked, so post keys act on nothing", got)
