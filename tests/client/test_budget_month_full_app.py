"""Budget, a person's month through the real UI (2026-10-22: every button gets a test -- 11 of budget.js's 14 were
named by none).

Add a bill (saved as the encrypted kind-30078 `pcai:budget` doc); ☰ → Edit changes its name and amount; ☰ → Skip
this month hides it and the "skipped" toggle shows it again; mark it paid, then Reset month (confirmed) makes it
unpaid; on Plans, add a plan, Rename it and add an item to it; Import a pasted backup adds its bill. The AI bill
scanner (camera, file, ✨) is out of scope ("not AI at this time"). Real login, DOM and budget.js.
"""
import asyncio
import json
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop

FIXTURE = r'''
localStorage.setItem('pc_nostr_settings',JSON.stringify({...JSON.parse(localStorage.getItem('pc_nostr_settings')||'{}'),osMode:false}));
window.__sentEvents=[];
const __bSend=WebSocket.prototype.send;
WebSocket.prototype.send=function(raw){try{const m=JSON.parse(raw);if(m[0]==='EVENT'&&m[1])__sentEvents.push(m[1]);}catch(_){}return __bSend.call(this,raw)};
'''

ROW = "[...document.querySelectorAll('.bg-row[data-bill]')].find(r=>r.innerText.includes(%s))"


@pytest.fixture(scope='module', autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


async def fill(b, sel, v):
    await b.js(f"(()=>{{const e=document.querySelector({json.dumps(sel)});e.value={json.dumps(v)};e.dispatchEvent(new Event('input',{{bubbles:true}}));}})(); 1")


async def row_menu(b, name, item):
    await b.until("!!(" + ROW % json.dumps(name) + ")")
    await b.js("(" + ROW % json.dumps(name) + ").querySelector('[data-act=menu]').click(); 1")
    await b.until(f"!!document.querySelector('.bg-menu [data-m={item}]')")
    await b.js(f"document.querySelector('.bg-menu [data-m={item}]').click(); 1")


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
def test_a_month_in_budget():
    async def check(b):
        await desktop.login(b)
        # A relay that accepts (the fixture refuses every publish unless told): Import AWAITS its save.
        await b.js("window.__publishOK=true; __PC.switchView('budget'); 1")
        await b.until("!!document.querySelector('#bg-n')")
        # Add a bill: it shows, and the encrypted budget doc is published.
        await fill(b, "#bg-n", "Rent")
        await fill(b, "#bg-a", "1000")
        await b.js("document.querySelector('#bg-save').click(); 1")
        await b.until("!!(" + ROW % json.dumps("Rent") + ")")
        await b.until("__sentEvents.some(e=>e.kind===30078&&(e.tags||[]).some(t=>t[0]==='d'&&t[1]==='pcai:budget'))")
        assert "Rent" not in (await b.js("__sentEvents.find(e=>(e.tags||[]).some(t=>t[1]==='pcai:budget')).content")), \
            "the budget went to the relay in plain text"
        # Edit name and amount.
        await row_menu(b, "Rent", "edit")
        await b.until("!!document.querySelector('#bg-eok')")
        await fill(b, "#bg-en", "Rent (flat)")
        await fill(b, "#bg-ea", "1100")
        await b.js("document.querySelector('#bg-eok').click(); 1")
        await b.until("/1,?100/.test((" + ROW % json.dumps("Rent (flat)") + "||{innerText:''}).innerText)")
        # Skip this month: hidden; the toggle shows skipped rows; toggling back hides it again.
        await row_menu(b, "Rent (flat)", "hide")
        await b.until("!(" + ROW % json.dumps("Rent (flat)") + ")")
        await b.js("document.querySelector('#bg-hid').click(); 1")
        await b.until("!!(" + ROW % json.dumps("Rent (flat)") + ")")
        await row_menu(b, "Rent (flat)", "hide")                          # un-skip
        await b.js("document.querySelector('#bg-hid').click(); 1")
        await b.until("!!(" + ROW % json.dumps("Rent (flat)") + ")")
        # Paid, then Reset month: unpaid again.
        await b.js("(" + ROW % json.dumps("Rent (flat)") + ").querySelector('[data-act=paid]').click(); 1")
        await b.until("(" + ROW % json.dumps("Rent (flat)") + ").classList.contains('done')")
        await b.js("document.querySelector('#bg-reset').click(); 1")
        await b.until("!!document.querySelector('.uiconfirm-btns')")             # "Start this month over?"
        await b.js("[...document.querySelectorAll('.uiconfirm-btns button')].pop().click(); 1")
        await b.until("!(" + ROW % json.dumps("Rent (flat)") + ").classList.contains('done')")
        # Plans: add, rename, add an item.
        await b.js("document.querySelector('.bg-tab[data-tab=\"plans\"]').click(); 1")
        await b.until("!!document.querySelector('#bg-cn')")
        await fill(b, "#bg-cn", "Trip")
        await b.js("document.querySelector('#bg-csave').click(); 1")
        await b.until("document.querySelectorAll('.bg-plan').length===1")
        await b.js("document.querySelector('.bg-plan [data-act=catmenu]').click(); 1")
        await b.until("!!document.querySelector('.bg-menu [data-m=ren]')")
        await b.js("document.querySelector('.bg-menu [data-m=ren]').click(); 1")
        await b.until("!!document.querySelector('#bg-rok')")
        await fill(b, "#bg-rn", "Holiday")
        await b.js("document.querySelector('#bg-rok').click(); 1")
        await b.until("/Holiday/.test(document.querySelector('.bg-plan').innerText)")
        await b.js("document.querySelector('.bg-plan [data-act=additem]').click(); 1")
        await b.until("!!document.querySelector('#bg-iok')")
        await fill(b, "#bg-in", "Hotel")
        await fill(b, "#bg-ia", "200")
        await b.js("document.querySelector('#bg-iok').click(); 1")
        await b.until("/Hotel/.test(document.querySelector('.bg-plan').innerText)")
        # Import a pasted backup: its bill is added to what is there.
        await b.js("document.querySelector('.bg-tab[data-tab=\"bills\"]').click(); 1")
        await b.until("!!document.querySelector('#bg-import')")
        await b.js("document.querySelector('#bg-import').click(); 1")
        await b.until("!!document.querySelector('#bg-iok2')")
        await fill(b, "#bg-json", json.dumps({"bills": [{"name": "Phone", "cost": 30, "is_recurring": True}], "cats": [], "items": []}))
        await b.js("document.querySelector('#bg-iok2').click(); 1")
        await b.until("/Import 1 bill/.test((document.querySelector('.uiconfirm-btns')||{}).closest?.('.modal, .modal-card, body')?.innerText||document.body.innerText)")
        await b.js("[...document.querySelectorAll('.uiconfirm-btns button')].pop().click(); 1")   # "Import 1 bill(s)…?"
        await b.until("!!(" + ROW % json.dumps("Phone") + ") && !!(" + ROW % json.dumps("Rent (flat)") + ")")
    asyncio.run(desktop.with_browser('online', '', check, FIXTURE))
