"""Articles and Torrents, pressed in the SHIPPED bundle:

  * Write article: Insert image uploads and puts the image in the body; Save draft stores a private draft
    (kind 30024); Publish publishes the article (kind 30023) and opens it;
  * on your own article: bookmark, copy link, Write a comment, ⚡, Edit (back into the editor) and Delete
    (confirmed: a NIP-09 deletion naming the article);
  * Torrents → Nostr → Publish: Cancel publishes nothing; a magnet link publishes a NIP-35 torrent (kind 2003).
"""
import asyncio
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop


@pytest.fixture(scope='module', autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


FIXTURE = r'''
localStorage.setItem('pc_nostr_settings',JSON.stringify({...JSON.parse(localStorage.getItem('pc_nostr_settings')||'{}'),osMode:false}));
window.__sent=[];window.__clip=[];
const __xs=WebSocket.prototype.send;
WebSocket.prototype.send=function(r){try{const m=JSON.parse(r);if(m[0]==='EVENT')__sent.push(m[1]);}catch(_){}return __xs.call(this,r)};
try{Object.defineProperty(navigator,'clipboard',{configurable:true,value:{writeText:async t=>{__clip.push(String(t));}}});}catch(_){}
const __ic=HTMLInputElement.prototype.click;
HTMLInputElement.prototype.click=function(){if(this.type==='file'){window.__picked=this.id;return;}return __ic.call(this);};
'''
SENT = "__sent.filter(e=>e.kind===%d)"


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
def test_write_publish_then_every_button_on_your_own_article():
    async def check(b):
        await desktop.login(b)
        await b.js("window.__publishOK=true; __PC.switchView('articles'); 1")
        await b.until("!!document.querySelector('#art-new')")
        await b.js("document.querySelector('#art-new').click(); 1")
        await b.until("!!document.querySelector('#ae-pub')")
        await b.js("(()=>{document.querySelector('#ae-title').value='Field notes';const t=document.querySelector('#ae-body');"
                   "t.value='Hello **world**.';t.dispatchEvent(new Event('input',{bubbles:true}));})(); 1")
        # Insert image: the picker opens, and the uploaded image lands in the body as markdown.
        await b.js("document.querySelector('#ae-insert').click(); 1")
        await b.until("window.__picked==='ae-body-file'")
        await b.js("(()=>{const i=document.querySelector('#ae-body-file');const dt=new DataTransfer();"
                   "dt.items.add(new File([new Uint8Array([137,80,78,71])],'pic.png',{type:'image/png'}));"
                   "i.files=dt.files;i.dispatchEvent(new Event('change'));})(); 1")
        await b.until("/!\\[[^\\]]*\\]\\(http/.test(document.querySelector('#ae-body').value)")
        await b.js("document.querySelector('#ae-draft').click(); 1")
        await b.until(f"{SENT % 30024}.length>0")
        await b.js("document.querySelector('#ae-pub').click(); 1")
        await b.until(f"{SENT % 30023}.length===1")
        # Publishing is done when the editor has handed back to the Articles list -- not when the event is SENT.
        await b.until("!!document.querySelector('#art-new') && !document.querySelector('#ae-pub')")
        art = await b.js(f"{SENT % 30023}[0]")
        # Publishing returns to the Articles list; open it as a reader does.
        await b.js("__PC.openNote(" + repr(art["id"]) + "); 1")
        await b.until("!!document.querySelector('#av-bm')")
        assert ["title", "Field notes"] in art["tags"] and "Hello **world**." in art["content"], art

        await b.js("document.querySelector('#av-bm').click(); 1")
        await b.until("document.querySelector('#av-bm').classList.contains('on')")
        await b.js("document.querySelector('#av-copy').click(); 1")
        await b.until("__clip.some(c=>/naddr1|nevent1|note1/.test(c))")
        await b.js("document.querySelector('#av-comment').click(); 1")
        await b.until("!!document.querySelector('#cmp-send')")
        await b.js("__PC.closeModal(); 1")
        await b.js("document.querySelector('#av-zap').click(); 1")
        await b.until("/lightning address|Zap/i.test(document.body.innerText)")
        await b.js("__PC.closeModal && __PC.closeModal(); 1")
        await b.js("document.querySelector('#av-edit').click(); 1")
        await b.until("!!document.querySelector('#ae-title') && document.querySelector('#ae-title').value==='Field notes'")
        await b.js("__PC.openNote(" + repr(art["id"]) + "); 1")
        await b.until("!!document.querySelector('#av-del')")
        await b.js("document.querySelector('#av-del').click(); 1")
        await b.until("!!document.querySelector('.uiconfirm-btns [data-uc=\"1\"]')")
        await b.js("document.querySelector('.uiconfirm-btns [data-uc=\"1\"]').click(); 1")
        await b.until(f"{SENT % 5}.some(e=>e.tags.some(t=>t[0]==='e'&&t[1]==={art['id']!r}))")
        assert not await b.js("__errors"), await b.js("__errors")
    asyncio.run(desktop.with_browser('online', '', check, FIXTURE))


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
def test_publish_a_torrent_and_cancel():
    ih = "c12fe1c06bba254a9dc9f519b335aa7c1367a88a"

    async def check(b):
        await desktop.login(b)
        await b.js("window.__publishOK=true; __PC.switchView('torrents'); 1")
        await b.until("!!document.querySelector('.tor-tabs .ntab[data-tt=nostr]')")
        await b.js("document.querySelector('.tor-tabs .ntab[data-tt=nostr]').click(); 1")
        await b.until("!!document.querySelector('#tor-add')")
        await b.js("document.querySelector('#tor-add').click(); 1")
        await b.until("!!document.querySelector('#tor-cancel')")
        await b.js("document.querySelector('#tor-cancel').click(); 1")
        await b.until("!document.querySelector('#tor-cancel')")
        await b.js("document.querySelector('#tor-add').click(); 1")
        await b.until("!!document.querySelector('#tor-go')")
        await b.js(f"(()=>{{const m=document.querySelector('#tor-mag');m.value='magnet:?xt=urn:btih:{ih}&dn=Debian+12';"
                   "m.dispatchEvent(new Event('input',{bubbles:true}));document.querySelector('#tor-go').click();})(); 1")
        await b.until(f"{SENT % 2003}.length===1 && !document.querySelector('#tor-go')")
        tor = await b.js(f"{SENT % 2003}[0]")
        assert ["x", ih] in tor["tags"] and ["title", "Debian 12"] in tor["tags"], tor["tags"]
        assert not await b.js("__errors"), await b.js("__errors")
    asyncio.run(desktop.with_browser('online', '', check, FIXTURE))


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
def test_a_publish_confirmed_after_you_moved_on_does_not_pull_you_back():
    """The relay's OK can land after the person has left the editor. Switching to the Articles list THEN painted it
    over whatever they had opened -- the gate's flaky '#av-bm' failure, and a real yank for anyone who taps away
    while 'Publishing…'. Back to the list only from the editor."""
    async def check(b):
        await desktop.login(b)
        await b.js(r"""(() => {
          window.__heldOK = [];
          const P = Object.getPrototypeOf(__sockets.find(s => s.readyState === 1)), send = P.send;
          P.send = function(raw){ const m = JSON.parse(raw);
            if(Array.isArray(m) && m[0] === 'EVENT' && m[1].kind === 30023){ __heldOK.push([this, m[1].id]); return; }
            return send.call(this, raw); };
          window.__releaseOK = () => { for(const [s, id] of __heldOK.splice(0)) s.fire('message', ['OK', id, true, '']); };
          __PC.switchView('articles'); })(); 1""")
        await b.until("!!document.querySelector('#art-new')")
        await b.js("document.querySelector('#art-new').click(); 1")
        await b.until("!!document.querySelector('#ae-pub')")
        await b.js("(()=>{document.querySelector('#ae-title').value='Late OK';const t=document.querySelector('#ae-body');"
                   "t.value='Body.';t.dispatchEvent(new Event('input',{bubbles:true}));})(); 1")
        await b.js("document.querySelector('#ae-pub').click(); 1")
        await b.until("__heldOK.length===1")
        await b.js("__PC.switchView('notifications'); 1")
        await b.until("!document.querySelector('#ae-pub')")
        await b.js("__releaseOK(); 1")
        await asyncio.sleep(1.5)
        got = await b.js("({ list: !!document.querySelector('#art-new'), title: document.querySelector('#view-title').textContent })")
        assert not got["list"], ("a publish confirmed after the person moved on pulled them back to Articles", got)
        assert not await b.js("__errors.slice()")

    asyncio.run(desktop.with_browser('online', '', check, FIXTURE))
