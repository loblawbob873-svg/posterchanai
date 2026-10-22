"""The composer's poll, content warning, link cleaner and emoji picker work through the real controls
(2026-10-22: every button gets a test -- 5 of compose.js's buttons were named by none).

  * Poll: 📊 opens the builder, ＋ adds a third option, Post publishes a NIP-88 kind 1068 whose `option` tags are the
    three answers in order; fewer than two options is refused with a sentence.
  * Content warning (⋯ More → Sensitive): with a reason, the post carries ["content-warning", reason].
  * Clean links (⋯ More): a tracking parameter is stripped from a pasted link, with no network.
  * React → Emoji: the picked emoji is inserted into the text.
Real login, DOM and compose.js; the relay accepts and every published event is recorded.
"""
import asyncio
import json
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop

FIXTURE = r'''
localStorage.setItem('pc_nostr_settings',JSON.stringify({...JSON.parse(localStorage.getItem('pc_nostr_settings')||'{}'),osMode:false}));
window.__sentEvents=[];
const __cSend=WebSocket.prototype.send;
WebSocket.prototype.send=function(raw){try{const m=JSON.parse(raw);if(m[0]==='EVENT'&&m[1])__sentEvents.push(m[1]);}catch(_){}return __cSend.call(this,raw)};
'''


@pytest.fixture(scope='module', autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


async def open_composer(b):
    await b.js("window.__publishOK=true; __PC.compose({}); 1")
    await b.until("!!document.querySelector('#cmp-send') && !!document.querySelector('.modal textarea, #cmp-send')")


async def text(b, v):
    await b.js("(()=>{const t=document.querySelector('#cmp-send').closest('.modal, .modal-card, [role=dialog], body').querySelector('textarea');"
               f"t.value={json.dumps(v)};t.dispatchEvent(new Event('input',{{bubbles:true}}));}})(); 1")


async def more(b, item):
    await b.js("document.querySelector('#cmp-more').click(); 1")
    await b.until(f"!!document.querySelector('[data-m={item}]')")
    await b.js(f"document.querySelector('[data-m={item}]').click(); 1")


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
def test_a_poll_with_three_options():
    async def check(b):
        await desktop.login(b)
        await open_composer(b)
        await text(b, "Best pizza topping?")
        await b.js("document.querySelector('#cmp-poll').click(); 1")
        await b.until("!document.querySelector('#cmp-pollbox').classList.contains('hidden')")
        await b.js("(()=>{const o=document.querySelectorAll('#cmp-pollbox .poll-opt-in');o[0].value='Mushroom';})(); 1")
        await b.js("document.querySelector('#cmp-send').click(); 1")
        await b.until("/at least 2 poll options/.test(document.querySelector('#cmp-status').textContent)")
        await b.js("document.querySelector('#cmp-poll-add').click(); 1")
        await b.until("document.querySelectorAll('#cmp-pollbox .poll-opt-in').length===3")
        await b.js("(()=>{const o=document.querySelectorAll('#cmp-pollbox .poll-opt-in');o[1].value='Olive';o[2].value='Pineapple';})(); 1")
        await b.js("document.querySelector('#cmp-send').click(); 1")
        await b.until("__sentEvents.some(e=>e.kind===1068)")
        poll = await b.js("__sentEvents.find(e=>e.kind===1068)")
        assert poll["content"] == "Best pizza topping?", poll
        assert [t[2] for t in poll["tags"] if t[0] == "option"] == ["Mushroom", "Olive", "Pineapple"], poll["tags"]
        assert ["polltype", "singlechoice"] in poll["tags"]
    asyncio.run(desktop.with_browser('online', '', check, FIXTURE))


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
def test_content_warning_clean_links_and_emoji():
    async def check(b):
        await desktop.login(b)
        await open_composer(b)
        await text(b, "spoilers ahead https://example.com/article?id=7&utm_source=newsletter&utm_medium=email")
        await more(b, "clean")          # presses the composer's #cmp-clean
        await b.until("!/utm_/.test(document.querySelector('#cmp-send').closest('.modal, .modal-card, [role=dialog], body').querySelector('textarea').value)")
        kept = await b.js("document.querySelector('#cmp-send').closest('.modal, .modal-card, [role=dialog], body').querySelector('textarea').value")
        assert "https://example.com/article?id=7" in kept, kept          # the real parameter survives
        # React -> Emoji inserts it.
        await b.js("document.querySelector('#cmp-react').click(); 1")
        await b.until("!!document.querySelector('[data-m=emoji]')")
        await b.js("document.querySelector('[data-m=emoji]').click(); 1")
        await b.until("!!document.querySelector('.emoji-pop .ep-grid button[data-e]')")
        emoji = await b.js("(()=>{const e=document.querySelector('.emoji-pop .ep-grid button[data-e]');const v=e.dataset.e;e.dispatchEvent(new MouseEvent('mousedown',{bubbles:true,cancelable:true}));return v})()")
        await b.until(f"document.querySelector('#cmp-send').closest('.modal, .modal-card, [role=dialog], body').querySelector('textarea').value.includes({json.dumps(emoji)})")
        # Content warning with a reason.
        await more(b, "cw")
        await b.until("!document.querySelector('#cmp-cw-row').classList.contains('hidden')")
        # The menu entry presses the composer's own Sensitive toggle (#cmp-cw-btn), which now reads as on.
        assert await b.js("document.querySelector('#cmp-cw-btn').classList.contains('on')")
        await b.js("(()=>{const r=document.querySelector('#cmp-cw-reason');r.value='finale spoilers';r.dispatchEvent(new Event('input',{bubbles:true}));})(); 1")
        await b.js("document.querySelector('#cmp-send').click(); 1")
        await b.until("__sentEvents.some(e=>e.kind===1&&/spoilers ahead/.test(e.content))")
        note = await b.js("__sentEvents.find(e=>e.kind===1&&/spoilers ahead/.test(e.content))")
        assert ["content-warning", "finale spoilers"] in note["tags"], note["tags"]
    asyncio.run(desktop.with_browser('online', '', check, FIXTURE))
