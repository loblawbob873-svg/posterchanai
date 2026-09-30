"""A window's ✨ request reaches AI Chat, including when AI Chat is ANOTHER window.

Reported on PosterChanOS: the ✨ panel opens, Summarize is pressed, "the AI Chat window opens but
nothing happens". The request was built in the desktop's page, which opened AI Chat as its own window
and then waited for the chat box in ITSELF -- where it never is -- so the request was silently
dropped. This drives each half in the shipped bundle: the asking page leaves the request in shared
storage; an AI Chat window opening takes it into its composer; one already open takes it live; a stale
one is ignored; and nothing is ever sent without the person pressing Send.
"""
import asyncio
import json
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop


@pytest.fixture(scope='module', autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


KEY = 'pc_ai_window_draft'
PLAIN = "localStorage.setItem('pc_nostr_settings',JSON.stringify({...JSON.parse(localStorage.getItem('pc_nostr_settings')||'{}'),osMode:false}));"
AI_WIN = PLAIN + "window.pcShell.windowContext={role:'app',view:'ai'};window.pcShell.backgroundOwner=false;"
NOSEND = "window.__sent=[];__PC.ensureAiSession=async()=>({can_ai:true});"


async def _ai_ready(b):
    await desktop.login(b)
    await b.js(NOSEND)
    await b.js("__PC.switchView('ai')")
    await b.until("!!document.querySelector('#ai-input')")


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
def test_the_asking_page_leaves_the_request_for_ai_chat():
    """The asking page writes the request to shared storage BEFORE it opens AI Chat -- the write
    another window hears. Recorded at the storage call itself, because in a single test page AI Chat
    draws here and (rightly) takes the request straight back."""
    got = {}

    async def check(b):
        await desktop.login(b)
        await b.js("window.__writes=[];{const o=Storage.prototype.setItem;Storage.prototype.setItem=function(k,v){if(k==='%s')__writes.push(v);return o.apply(this,arguments)}}" % KEY)
        await b.js("__PC.askWindowContext({windows:[{title:'News',view:'news',text:'Headline one. Headline two.'}]},'Summarize this')")
        await b.until("__writes.length>0")
        got['left'] = await b.js("JSON.parse(__writes[0])")
    asyncio.run(desktop.with_browser('online', '', check, PLAIN))
    assert 'Summarize this' in got['left']['text'] and 'Headline one' in got['left']['text'], got
    assert abs(got['left']['at'] / 1000 - __import__('time').time()) < 120, got


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
def test_an_ai_window_that_opens_takes_the_request_into_its_composer():
    got = {}

    async def check(b):
        await _ai_ready(b)
        await b.until("document.querySelector('#ai-input').value.includes('Summarize this')")
        got['value'] = await b.js("document.querySelector('#ai-input').value")
        got['left'] = await b.js(f"localStorage.getItem('{KEY}')")
    # Written BEFORE this window's page loads -- the order it happens in on the machine.
    seed = f"localStorage.setItem('{KEY}',JSON.stringify({{text:'Summarize this\\n\\nWindow context',at:Date.now()}}));"
    asyncio.run(desktop.with_browser('online', '?pcwin=ai', check, AI_WIN + seed))
    assert got['value'].startswith('Summarize this'), got
    assert got['left'] is None, 'the request was left behind to fill the next AI window too'


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
def test_an_ai_window_already_open_takes_it_live_and_a_stale_one_is_ignored():
    got = {}

    async def check(b):
        await _ai_ready(b)
        # Stale: written two minutes ago by a window long gone.
        await b.js(f"localStorage.setItem('{KEY}',JSON.stringify({{text:'OLD',at:Date.now()-120000}}));"
                   f"dispatchEvent(new StorageEvent('storage',{{key:'{KEY}',newValue:localStorage.getItem('{KEY}')}}))")
        await asyncio.sleep(.3)
        got['stale'] = await b.js("document.querySelector('#ai-input').value")
        # Fresh, from another window (the storage event is how another page's write arrives).
        await b.js(f"localStorage.setItem('{KEY}',JSON.stringify({{text:'Extract tasks',at:Date.now()}}));"
                   f"dispatchEvent(new StorageEvent('storage',{{key:'{KEY}',newValue:localStorage.getItem('{KEY}')}}))")
        await b.until("document.querySelector('#ai-input').value==='Extract tasks'")
        got['sent'] = await b.js("!!document.querySelector('#ai-msgs .msg.user, .ai-msg.user')")
    asyncio.run(desktop.with_browser('online', '?pcwin=ai', check, AI_WIN))
    assert got['stale'] == '', got
    assert not got['sent'], 'the request was sent without the person pressing Send'
