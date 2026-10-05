"""Mail draws the app's flat sprite icons, not emoji -- in the list, the reader, Reply and Compose.

"Email -> Change emoji to flat icons to match the rest of the ui, same for Reply, Compose". The rest
of the client draws its controls from one sprite (sprite.js); Mail drew its own from the emoji font --
✏️ Compose, 📥/📤/🗄 folders, 🔄, ● 🗄 🗑 in the bulk bar, ✉️ 👤 📎 📁 💾 🔐 in the composer. Besides not
matching, an emoji is drawn by a FONT, and a platform without one (a minimal Gentoo install, a
WebView) draws nothing at all.

Drives the SHIPPED bundle: opens Mail with a server that still sends the OLD emoji folder labels (a
node not yet upgraded), selects a message, opens Reply and a fresh Compose, and checks every visible
piece of Mail chrome: no emoji in its text, every icon a <use> of a symbol the sprite really defines
(a missing one renders as blank space with no error), and each folder showing the icon of its role.
"""
import asyncio
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop
from tests.client.test_cord_direct_invites_full_app import click


@pytest.fixture(scope='module', autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


STUB = r'''
const originalFetch=window.fetch;
const row={uid:'7',account:'me@home.test',folder:'INBOX',subject:'Invoice',from:'Sender <s@x.test>',ts:10,preview:'pay me',read:false,attachments:1};
window.fetch=(url,opts)=>{const u=new URL(String(url),location.href);if(!u.pathname.startsWith('/api/mail/'))return originalFetch(url,opts);
const reply=(v,s=200)=>Promise.resolve(new Response(JSON.stringify(v),{status:s,headers:{'Content-Type':'application/json'}}));
const p=u.pathname;
if(p.endsWith('/accounts'))return reply({accounts:[{email:'me@home.test'}]});
if(p.endsWith('/folders'))return reply({folders:['INBOX','Sent','Drafts','Archive','Junk','Trash','Work','Receipts'],sent:'Sent',
  labels:{INBOX:'📥 Inbox',Sent:'📤 Sent',Drafts:'📝 Drafts',Archive:'🗄 Archive',Junk:'⚠️ Spam',Trash:'🗑 Trash',Work:'📁 Work',Receipts:'📁 Receipts'}});
if(p.endsWith('/messages'))return reply({messages:u.searchParams.get('folder')==='INBOX'?[row]:[],next_until:0});
if(p.endsWith('/message'))return reply({message:Object.assign({},row,{text:'Please pay.',to:'me@home.test',attachments:[{name:'invoice.pdf',type:'application/pdf',size:1200}]})});
if(p.endsWith('/thread'))return reply({messages:[]});
return reply({ok:true});};
__PC.switchView('mail');'''

# Every visible text node and every icon reference inside the given roots.
AUDIT = r'''(sel=>{
  const roots=[...document.querySelectorAll(sel)];
  const emoji=/[\p{Extended_Pictographic}←-⇿⌀-➿⬀-⯿]/u;
  const bad=[], missing=[], icons=new Set();
  for(const r of roots){
    const w=document.createTreeWalker(r, NodeFilter.SHOW_TEXT);
    for(let n=w.nextNode(); n; n=w.nextNode()){
      const el=n.parentElement; if(!el || el.closest('.mail-body,.mm-body,iframe,.mi-prev,.mi-subj-text,.mr-subj')) continue;
      if(el.getClientRects().length===0) continue;                 // not on screen
      if(emoji.test(n.textContent)) bad.push(n.textContent.trim().slice(0,40));
    }
    for(const el of r.querySelectorAll('[placeholder]')) if(emoji.test(el.placeholder)) bad.push('placeholder: '+el.placeholder);
    for(const u of r.querySelectorAll('svg use')){
      const id=(u.getAttribute('href')||'').slice(1); icons.add(id);
      if(!id || !document.getElementById(id)) missing.push(id||'(none)');
    }
  }
  return {roots:roots.length, bad, missing, icons:[...icons]};
})'''


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
@pytest.mark.parametrize('width', [390, 1280])
def test_mail_reply_and_compose_use_the_sprite(width):
    out = {}

    async def check(b):
        await b.call('Emulation.setDeviceMetricsOverride', dict(width=width, height=900, deviceScaleFactor=1, mobile=width < 600))
        await b.until("document.body.classList.contains('guest')")
        await desktop.login(b)
        await b.js(STUB)
        await b.until("document.querySelectorAll('.mail-item').length===1 && document.querySelectorAll('.mail-folder[data-folder]').length>=3")
        out['list'] = await b.js(AUDIT + "('.mail-side,.mail-list')")
        await click(b, '#mail-folders-open')                                     # every folder, by role
        await b.until("document.querySelectorAll('.mail-fbrowse[data-folder]').length>=8")
        out['folders'] = await b.js("[...document.querySelectorAll('.mail-fbrowse[data-folder]')].map(f=>[f.dataset.folder,(f.querySelector('use')||{getAttribute:()=>''}).getAttribute('href'),f.textContent.trim()])")
        out['browser'] = await b.js(AUDIT + "('#modal-root .modal')")
        await b.js("(window.__PC&&__PC.closeModal?__PC.closeModal():document.querySelector('#modal-root').innerHTML=''); true")
        out['compose_btn'] = await b.js("(document.querySelector('#mail-compose use')||{getAttribute:()=>''}).getAttribute('href')")
        await b.js("document.querySelector('#mail-selall').click(); true")         # the bulk bar
        await b.until("!!document.querySelector('[data-bulk=\"delete\"]')")
        out['bulk'] = await b.js(AUDIT + "('.mail-bulk')")
        await b.js("document.querySelector('#mail-selall').click(); true")
        await click(b, '.mail-item')
        await b.until("!!document.querySelector('.mail-actions [data-act=\"reply\"]') && !!document.querySelector('.mail-att')")
        out['reader'] = await b.js(AUDIT + "('.mail-read')")
        await click(b, '.mail-actions [data-act="reply"]')
        await b.until("!!document.querySelector('#cm-send')")
        out['reply'] = await b.js(AUDIT + "('#modal-root .modal')")
        out['reply_buttons'] = await b.js("['cm-contacts','cm-attach','cm-blossom','cm-draft','cm-nmail','cm-send'].map(id=>[id,!!document.querySelector('#'+id+' svg use')])")
        await b.js("document.dispatchEvent(new KeyboardEvent('keydown',{key:'Escape',bubbles:true})); true")
        await b.js("(typeof closeModal==='function'?closeModal():(window.__PC&&__PC.closeModal&&__PC.closeModal())); true")
        await b.until("!document.querySelector('#cm-send')")
        if width < 600:                                                          # the reader covers the list on a phone
            await click(b, '#mail-back')
        await click(b, '#mail-compose')
        await b.until("!!document.querySelector('#cm-send')")
        out['compose'] = await b.js(AUDIT + "('#modal-root .modal')")

    asyncio.run(desktop.with_browser('online', '', check))
    f = {k: (icon, text) for k, icon, text in out['folders']}
    assert f['INBOX'] == ('#i-mail', 'Inbox') and f['Sent'] == ('#i-send', 'Sent'), out['folders']
    assert f['Drafts'] == ('#i-draft', 'Drafts') and f['Archive'] == ('#i-download', 'Archive'), out['folders']
    assert f['Trash'] == ('#i-trash', 'Trash') and f['Junk'] == ('#i-warn', 'Spam'), out['folders']
    assert f['Work'] == ('#i-folder', 'Work') and f['Receipts'] == ('#i-folder', 'Receipts'), out['folders']
    assert out['compose_btn'] == '#i-pen', out
    for part in ('list', 'browser', 'bulk', 'reader', 'reply', 'compose'):
        a = out[part]
        assert a['roots'] >= 1 and a['icons'], (part, a)
        assert not a['bad'], (part, 'emoji drawn by a font, not the sprite', a['bad'])
        assert not a['missing'], (part, 'an icon the sprite does not define renders as blank space', a['missing'])
    assert all(ok for _, ok in out['reply_buttons']), out['reply_buttons']
