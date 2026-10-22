"""A post's ⋯ menu: the raw-event viewer and the report flow work through the real menu on a real card
(2026-10-22: every button gets a test -- 14 of menus.js's 16 buttons were named by none).

  * Raw event (JSON): Copy JSON copies the event, Copy nevent copies an nevent1…, Close closes.
  * Report post: Submit with no reason is refused with a sentence; Cancel closes having sent nothing; a real report
    publishes a signed kind 1984 naming the post (`e`) and its author (`p`); afterwards "Mute this user" publishes
    the account's mute list (kind 10000) and Close closes.
Admin-only Block, Communities-only Ban, and Screenshot (a server render) get their own tests. The AI summary is out of
scope ("not AI at this time"). Real login, DOM, cards.js and menus.js; the clipboard and relay are recorded.
"""
import asyncio
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop

FIXTURE = r'''
localStorage.setItem('pc_nostr_settings',JSON.stringify({...JSON.parse(localStorage.getItem('pc_nostr_settings')||'{}'),osMode:false}));
window.__sentEvents=[]; window.__clip=[];
const __mSend=WebSocket.prototype.send;
WebSocket.prototype.send=function(raw){try{const m=JSON.parse(raw);if(m[0]==='EVENT'&&m[1])__sentEvents.push(m[1]);}catch(_){}return __mSend.call(this,raw)};
const __exec=document.execCommand.bind(document);
document.execCommand=function(c,...r){ if(c==='copy'){ const a=document.activeElement; __clip.push(a&&'value' in a?a.value:String(getSelection())); return true; } return __exec(c,...r); };
try{ navigator.clipboard.writeText=async t=>{ __clip.push(String(t)); }; }catch(_){}
'''

SETUP = r'''(()=>{
  const sk=new Uint8Array(32).fill(7), pk=NostrTools.getPublicKey(sk);
  const ev=NostrTools.finalizeEvent({kind:1,created_at:Math.floor(Date.now()/1000)-30,content:'a post to report',tags:[]},sk);
  window.__publishOK=true;   // the fixture relay refuses every publish unless told to accept, as a real relay would
  Store.saveEvent(ev); window.__post={id:ev.id,pk};
  __PC.openNote(ev.id); return ev.id;
})()'''


@pytest.fixture(scope='module', autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


async def menu(b, item):
    await b.until("!!document.querySelector('article.note[data-id=\"'+__post.id+'\"] [data-a=menu]')")
    await b.js("document.querySelector('article.note[data-id=\"'+__post.id+'\"] [data-a=menu]').click(); 1")
    await b.until(f"!!document.querySelector('[data-m={item}]')")
    await b.js(f"document.querySelector('[data-m={item}]').click(); 1")


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
def test_raw_event_and_report_from_the_post_menu():
    async def check(b):
        await desktop.login(b)
        pid = await b.js(SETUP)
        # Raw event viewer.
        await menu(b, "rawjson")
        await b.until("!!document.querySelector('#rawev-copy')")
        await b.js("document.querySelector('#rawev-copy').click(); 1")
        await b.until("__clip.some(c=>c.includes('\"id\": \"'+__post.id+'\"') || c.includes('\"id\":\"'+__post.id+'\"'))")
        await b.js("document.querySelector('#rawev-copyid').click(); 1")
        await b.until("__clip.some(c=>/^nevent1/.test(c))")
        await b.js("document.querySelector('#rawev-close').click(); 1")
        await b.until("!document.querySelector('#rawev-close')")
        # Report: no reason -> a sentence, nothing sent; Cancel closes.
        await menu(b, "report")
        await b.until("!!document.querySelector('#rep-submit')")
        await b.js("document.querySelector('#rep-submit').click(); 1")
        await b.until("/Pick a reason first/.test(document.querySelector('.rep-err').textContent)")
        await b.js("document.querySelector('#rep-cancel').click(); 1")
        await b.until("!document.querySelector('#rep-cancel')")
        assert not await b.js("__sentEvents.some(e=>e.kind===1984)"), "Cancel sent a report"
        # A real report.
        await menu(b, "report")
        await b.until("!!document.querySelector('input[name=rep-type]')")
        await b.js("(()=>{const r=document.querySelector('input[name=rep-type]');r.checked=true;r.dispatchEvent(new Event('change',{bubbles:true}));"
                   "document.querySelector('#rep-comment').value='spam links';document.querySelector('#rep-submit').click();})(); 1")
        await b.until("__sentEvents.some(e=>e.kind===1984)")
        await b.until("!!document.querySelector('.rep-done')")
        assert not await b.js("/Not sent/.test((document.querySelector('.rep-err')||{}).textContent||'')"), "the report said it was not sent"
        rep = await b.js("__sentEvents.find(e=>e.kind===1984)")
        tags = {t[0]: t[1] for t in rep["tags"]}
        assert tags.get("e") == pid and tags.get("p") == await b.js("__post.pk"), rep["tags"]
        assert rep["sig"] and "spam links" in rep["content"], rep
        # Afterwards: mute the author, then close.
        await b.until("!!document.querySelector('#rep-mute')")
        await b.js("document.querySelector('#rep-mute').click(); 1")
        await b.until("__sentEvents.some(e=>e.kind===10000)")
        await b.js("document.querySelector('#rep-close').click(); 1")
        await b.until("!document.querySelector('#rep-close')")
    asyncio.run(desktop.with_browser('online', '', check, FIXTURE))
