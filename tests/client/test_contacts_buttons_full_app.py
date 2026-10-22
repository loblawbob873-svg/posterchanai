"""Contacts' buttons, pressed in the SHIPPED bundle against a stubbed contacts API:

  * no addressbook: "New addressbook" → Create makes one; "Import .vcf" opens the addressbook menu;
  * ☰ → New addressbook → Create posts the name and selects it;
  * ☰ → Import .vcf opens the file picker, and the chosen file is uploaded;
  * ☰ → Sync to a device → Generate shows a password, Copy copies it, Revoke (confirmed) revokes;
  * a contact's Share hands a .vcf to the share sheet; Delete (confirmed) deletes that card.
"""
import asyncio
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop


@pytest.fixture(scope='module', autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


BOUNDARIES = r'''
localStorage.setItem('pc_nostr_settings',JSON.stringify({...JSON.parse(localStorage.getItem('pc_nostr_settings')||'{}'),osMode:false}));
window.__copied=[];window.__shared=[];
try{Object.defineProperty(navigator,'clipboard',{configurable:true,value:{writeText:async t=>{__copied.push(String(t));}}});}catch(_){}
try{Object.defineProperty(navigator,'share',{configurable:true,value:async d=>{__shared.push({title:d.title,files:(d.files||[]).map(f=>f.name)});}});}catch(_){}
window.__picked=[];
const __iclick=HTMLInputElement.prototype.click;
HTMLInputElement.prototype.click=function(){if(this.type==='file'){__picked.push(this.id);return;}return __iclick.call(this);};
const alice=['BEGIN:VCARD','VERSION:3.0','UID:alice','FN:Alice Smith','N:Smith;Alice;;;','TEL:+15551234','END:VCARD',''].join('\r\n');
window.__books=JSON.parse(localStorage.getItem('__fixtureBooks')||'null')||[{id:'main',displayname:'Contacts'}];
window.__cards={main:[{uid:'alice',ics:alice}]};
window.__calls=[];
const contactsFetch=window.fetch;
window.fetch=async function(url,opts={}){
 const u=new URL(String(url),location.href),m=String(opts.method||'GET').toUpperCase(),p=u.pathname;
 const reply=(body,status=200)=>Promise.resolve(new Response(JSON.stringify(body),{status,headers:{'Content-Type':'application/json'}}));
 if(!p.startsWith('/api/contacts/')&&!p.startsWith('/api/calendar/'))return contactsFetch(url,opts);
 let body=null;try{body=opts.body&&typeof opts.body==='string'?JSON.parse(opts.body):null;}catch(_){}
 if(m!=='GET')__calls.push({op:m+' '+p+u.search,body,form:opts.body instanceof FormData});
 if(p==='/api/calendar/config')return reply({enabled:true,url:'https://fixture.invalid/caldav/',username:'me',has_password:true});
 if(p==='/api/calendar/password'&&m==='POST')return reply({password:'pw-contacts-9'});
 if(p==='/api/calendar/password'&&m==='DELETE')return reply({ok:true});
 if(p==='/api/contacts/books'&&m==='POST'){const id='book-'+__books.length;__books.push({id,displayname:body.name});__cards[id]=[];return reply({id});}
 if(p==='/api/contacts/books')return reply({books:__books});
 if(p==='/api/contacts/cards'&&m==='DELETE'){const b=u.searchParams.get('book');__cards[b]=(__cards[b]||[]).filter(c=>c.uid!==u.searchParams.get('uid'));return reply({ok:true});}
 if(p==='/api/contacts/cards')return reply({cards:__cards[u.searchParams.get('book')]||[]});
 if(p==='/api/contacts/import')return reply({imported:1,book:'main'});
 return reply({ok:true});
};
'''


async def contacts(b, books=None):
    await desktop.login(b)
    if books is not None:
        await b.js(f"window.__books={books}; 1")
    await b.js("__PC.switchView('contacts'); 1")


async def menu(b, button):
    await b.js("document.querySelector('#ct-menu').click(); 1")
    await b.until(f"!!document.querySelector('#{button}')")
    await b.js(f"document.querySelector('#{button}').click(); 1")


async def confirm(b):
    await b.until("!!document.querySelector('.uiconfirm-btns [data-uc=\"1\"]')")
    await b.js("document.querySelector('.uiconfirm-btns [data-uc=\"1\"]').click(); 1")


def _run(check):
    asyncio.run(desktop.with_browser('online', '', check, BOUNDARIES))


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
def test_no_addressbook_offers_new_and_import():
    async def check(b):
        await contacts(b, books='[]')
        await b.until("!!document.querySelector('#ct-import2')")
        await b.js("document.querySelector('#ct-import2').click(); 1")
        await b.until("!!document.querySelector('#ctb-add')")
        await b.js("__PC.closeModal(); 1")
        await b.until("!document.querySelector('#ctb-add') && !!document.querySelector('#ct-mkbook')")
        await b.js("document.querySelector('#ct-mkbook').click(); 1")
        await b.until("!!document.querySelector('#ctb-save')")
        await b.js("document.querySelector('#ctb-name').value='Family'; document.querySelector('#ctb-save').click(); 1")
        await b.until("!document.querySelector('#ctb-save') && !document.querySelector('#ct-mkbook')")
        assert await b.js("__calls.find(c=>c.op==='POST /api/contacts/books').body.name") == 'Family'
        assert not await b.js("__errors"), await b.js("__errors")
    _run(check)


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
def test_addressbook_menu_new_import_and_device_password():
    async def check(b):
        await contacts(b)
        await b.until("!!document.querySelector('.ct-row[data-uid=alice]')")
        await menu(b, 'ctb-add')
        await b.until("!!document.querySelector('#ctb-save')")
        await b.js("document.querySelector('#ctb-name').value='Work'; document.querySelector('#ctb-save').click(); 1")
        await b.until("!document.querySelector('#ctb-save') && document.querySelector('#ct-pick')?.value==='book-1'")

        await menu(b, 'ctb-import')
        await b.until("__picked.includes('ctb-file')")
        await b.js("(()=>{const i=document.querySelector('#ctb-file');const dt=new DataTransfer();"
                   "dt.items.add(new File(['BEGIN:VCARD\\r\\nVERSION:3.0\\r\\nFN:Bob\\r\\nEND:VCARD\\r\\n'],'bob.vcf',{type:'text/vcard'}));"
                   "i.files=dt.files;i.dispatchEvent(new Event('change'));})(); 1")
        await b.until("__calls.some(c=>c.op.startsWith('POST /api/contacts/import')&&c.form)")

        await b.until("!document.querySelector('#ctb-file')")
        await menu(b, 'ctb-phone')
        await b.until("!!document.querySelector('#ctd-gen')")
        await b.js("document.querySelector('#ctd-gen').click(); 1")
        await b.until("!!document.querySelector('#ctd-copy')")
        await b.js("document.querySelector('#ctd-copy').click(); 1")
        await b.until("__copied.includes('pw-contacts-9')")
        await b.js("document.querySelector('#ctd-clear').click(); 1")
        await confirm(b)
        await b.until("__calls.some(c=>c.op==='DELETE /api/calendar/password') && !document.querySelector('#ctd-gen')")
        assert not await b.js("__errors"), await b.js("__errors")
    _run(check)


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
def test_share_and_delete_a_contact():
    async def check(b):
        await contacts(b)
        await b.until("!!document.querySelector('.ct-row[data-uid=alice]')")
        await b.js("document.querySelector('.ct-row[data-uid=alice]').click(); 1")
        await b.until("!!document.querySelector('#cc-share')")
        await b.js("document.querySelector('#cc-share').click(); 1")
        await b.until("__shared.length===1")
        assert await b.js("__shared[0]") == {'title': 'Alice Smith', 'files': ['Alice Smith.vcf']}
        await b.js("document.querySelector('#cc-del').click(); 1")
        await confirm(b)
        await b.until("__calls.some(c=>c.op==='DELETE /api/contacts/cards?book=main&uid=alice')")
        await b.until("!document.querySelector('#cc-del') && !document.querySelector('.ct-row[data-uid=alice]')")
        assert not await b.js("__errors"), await b.js("__errors")
    _run(check)
