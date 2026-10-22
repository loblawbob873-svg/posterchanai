"""The password vault: create a login with a generated password, copy it, export, and the health panel
(2026-10-22: every button gets a test -- the generator, export and health buttons were named by none).

  * New login: name, user, site; Generate opens the generator, ↻ makes a different password, Copy copies it,
    "Use it" puts it in the login; the login is listed and Copy password copies the same value.
  * Export: "Chrome / Firefox (.csv)" downloads a CSV holding that login's site, user and password, and
    "Everything (.json)" a JSON with it; Close closes.
  * Health: opens and Close closes.
The Android-only autofill panel (pd-*) is tests/client/test_vault_device_panel_full_app.py. Real login, DOM and vault.js; downloads
and the clipboard are recorded.
"""
import asyncio
import json
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop

FIXTURE = r'''
localStorage.setItem('pc_nostr_settings',JSON.stringify({...JSON.parse(localStorage.getItem('pc_nostr_settings')||'{}'),osMode:false}));
window.__clip=[]; window.__blobs=new Map(); window.__downloads=[];
const __exec=document.execCommand.bind(document);
document.execCommand=function(c,...r){ if(c==='copy'){ const a=document.activeElement; __clip.push(a&&'value' in a?a.value:String(getSelection())); return true; } return __exec(c,...r); };
try{ navigator.clipboard.writeText=async t=>{ __clip.push(String(t)); }; }catch(_){}
const __cou=URL.createObjectURL.bind(URL);
URL.createObjectURL=function(b){ const u=__cou(b); try{ __blobs.set(u,b); }catch(_){} return u; };
const __aclick=HTMLAnchorElement.prototype.click;
HTMLAnchorElement.prototype.click=function(){ if(this.download){ __downloads.push({name:this.download,href:this.href}); return; } return __aclick.call(this); };
window.__dl=async(ext)=>{ const d=__downloads.find(x=>x.name.endsWith(ext)); if(!d) return null; const b=__blobs.get(d.href); return b?await b.text():null; };
'''


@pytest.fixture(scope='module', autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


async def type_in(b, sel, v):
    await b.js(f"(()=>{{const e=document.querySelector({json.dumps(sel)});e.focus();e.value={json.dumps(v)};"
               f"e.dispatchEvent(new Event('input',{{bubbles:true}}));e.dispatchEvent(new Event('change',{{bubbles:true}}));}})(); 1")


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
def test_generate_a_password_save_copy_export_and_health():
    async def check(b):
        await desktop.login(b)
        await b.js("window.__publishOK=true; __PC.switchView('vault'); 1")   # a relay that accepts the vault's writes
        await b.until("!!document.querySelector('.pv-new')")
        await b.js("document.querySelector('.pv-new').click(); 1")
        await b.until("!!document.querySelector('.pv-title')")
        await type_in(b, ".pv-title", "Example Bank")
        await type_in(b, ".pv-user", "alice@example.com")
        await type_in(b, ".pv-uris", "https://bank.example")
        # Generator: ↻ changes it, Copy copies it, Use it fills the login.
        await b.js("document.querySelector('.pv-gen').click(); 1")
        await b.until("(document.querySelector('#pg-out')||{}).textContent.length>=8")
        first = await b.js("document.querySelector('#pg-out').textContent")
        await b.js("document.querySelector('#pg-again').click(); 1")
        await b.until(f"document.querySelector('#pg-out').textContent!=={json.dumps(first)}")
        generated = await b.js("document.querySelector('#pg-out').textContent")
        await b.js("document.querySelector('#pg-copy').click(); 1")
        await b.until(f"__clip.includes({json.dumps(generated)})")
        await b.js("document.querySelector('#pg-use').click(); 1")
        await b.until(f"document.querySelector('.pv-pass').value==={json.dumps(generated)}")
        # Back to the list: the login is there, and Copy password copies the same value.
        await b.js("document.querySelector('.pv-back').click(); 1")
        await b.until("[...document.querySelectorAll('.pv-item')].some(i=>/Example Bank/.test(i.innerText))")
        await b.js("[...document.querySelectorAll('.pv-item')].find(i=>/Example Bank/.test(i.innerText)).click(); 1")
        await b.until("!!document.querySelector('.pv-copy-p')")
        before = await b.js("__clip.length")
        await b.js("document.querySelector('.pv-copy-p').click(); 1")
        await b.until(f"__clip.length>{before} && __clip[__clip.length-1]==={json.dumps(generated)}")
        await b.js("document.querySelector('.pv-back').click(); 1")
        # Export: CSV and JSON both carry the login; Close closes.
        await b.until("!!document.querySelector('.pv-export')")
        await b.js("document.querySelector('.pv-export').click(); 1")
        await b.until("!!document.querySelector('#pe-csv')")
        await b.js("document.querySelector('#pe-csv').click(); 1")
        await b.until("__downloads.some(d=>d.name.endsWith('.csv'))")
        csv = await b.js("__dl('.csv')")
        assert csv and "bank.example" in csv and "alice@example.com" in csv and generated in csv, csv
        if not await b.js("!!document.querySelector('#pe-json')"):
            await b.js("document.querySelector('.pv-export').click(); 1")
            await b.until("!!document.querySelector('#pe-json')")
        await b.js("document.querySelector('#pe-json').click(); 1")
        await b.until("__downloads.some(d=>d.name.endsWith('.json'))")
        js = await b.js("__dl('.json')")
        assert js and generated in js and "Example Bank" in js, js
        if await b.js("!!document.querySelector('#pe-close')"):
            await b.js("document.querySelector('#pe-close').click(); 1")
            await b.until("!document.querySelector('#pe-close')")
        # Health opens and closes.
        await b.until("!!document.querySelector('.pv-health')")
        await b.js("document.querySelector('.pv-health').click(); 1")
        await b.until("!!document.querySelector('#ph-close')")
        await b.js("document.querySelector('#ph-close').click(); 1")
        await b.until("!document.querySelector('#ph-close')")
    asyncio.run(desktop.with_browser('online', '', check, FIXTURE))
