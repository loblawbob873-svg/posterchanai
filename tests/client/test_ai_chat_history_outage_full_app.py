"""'we cant have our core apps not working if network outage' -- AI chat never shows an existing
conversation as a brand-new chat because its history could not be read.

During a relay restart /api/conversations/<id> answered an EMPTY transcript and the AI screen drew the
"Welcome to PosterChan AI" splash over a real conversation. The server answers 503 now
(tests/test_chat_history_strict.py); this drives the SHIPPED ai.js: a failed load says so and offers
Try again, which brings the conversation back once the server answers. Phone and desktop width.
"""
import asyncio
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop


@pytest.fixture(scope='module', autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


PLAIN = "localStorage.setItem('pc_nostr_settings',JSON.stringify({...JSON.parse(localStorage.getItem('pc_nostr_settings')||'{}'),osMode:false}));"
STUB = r'''window.histDown=false;
const orig=window.fetch;
window.fetch=(url,opts)=>{const u=new URL(String(url),location.href);
 const reply=(v,s=200)=>Promise.resolve(new Response(JSON.stringify(v),{status:s,headers:{'Content-Type':'application/json'}}));
 if(u.pathname==='/api/conversations' && (!opts||!opts.method||opts.method==='GET'))return reply([{id:5,title:'Trip planning'}]);
 if(u.pathname==='/api/conversations/5'){ if(histDown) return reply({detail:'Could not reach your chat history just now — try again.'},503);
   return reply({id:5,title:'Trip planning',messages:[{id:1,role:'user',content:'Where should we stay in Denver?'},{id:2,role:'assistant',content:'Try LoDo.'}]});}
 return orig(url,opts);};
__PC.ensureAiSession=async()=>({can_ai:true});'''


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
@pytest.mark.parametrize('width', [390, 1280])
def test_an_unreadable_history_is_not_drawn_as_a_new_chat(width):
    async def check(b):
        await b.call('Emulation.setDeviceMetricsOverride', dict(width=width, height=900, deviceScaleFactor=1, mobile=width < 600))
        await desktop.login(b)
        await b.js(STUB)
        await b.js("__PC.switchView('ai')")
        await b.until("document.querySelector('#ai-msgs') && document.querySelector('#ai-msgs').textContent.includes('Denver')")

        await b.js("histDown=true; const s=document.querySelector('#ai-conv'); s.value='5'; s.dispatchEvent(new Event('change'))")
        await b.until("!!document.querySelector('#ai-msgs .ai-load-failed')")
        st = await b.js("({welcome:!!document.querySelector('#ai-msgs .ai-welcome'), text:document.querySelector('#ai-msgs').textContent})")
        assert not st['welcome'], ('an existing chat was shown as a new one', st)
        assert 'could not be reached' in st['text'], st

        await b.js("histDown=false; document.querySelector('#ai-load-retry').click()")
        await b.until("document.querySelector('#ai-msgs').textContent.includes('Denver') && !document.querySelector('#ai-msgs .ai-load-failed')")
    asyncio.run(desktop.with_browser('online', '', check, PLAIN))
