"""Window ✨ can act on a list ROW and on a labelled switch -- in Mail and in Settings.

"improve window AI agentic features and make sure they actually work". Measured on the windows the
client really sends: in Mail the three emails were not in the control list at all (a row made
clickable with `row.onclick = …` is invisible to any selector), so "open the receipt email" had
nothing to point at; every row's checkbox was called "on" (a checkbox's VALUE, which is "on" for all
of them); and Settings' switches -- <label>Text<label class="switch"><input></label></label> -- were
"on" too, because the nearest label is the empty one.

Drives the shipped client: the panel's request is read for what the AI is told, and a step naming a
row / a switch is run with Do all on the real elements.
"""
import asyncio
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop
from tests.client.test_mail_uses_flat_icons_full_app import STUB as MAILSTUB


@pytest.fixture(scope='module', autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


MAIL = (MAILSTUB.replace("const row={uid:'7'", "const rows=[{uid:'9',account:'me@home.test',folder:'INBOX',subject:'Lunch on Friday?',from:'Dana <dana@x.test>',ts:30,preview:'are you free',read:false,attachments:0},{uid:'8',account:'me@home.test',folder:'INBOX',subject:'Your receipt from the hardware store',from:'Store <shop@x.test>',ts:20,preview:'thanks for shopping',read:true,attachments:1}];const row={uid:'7'")
        .replace("?[row]:[]", "?[...rows,row]:[]")
        .replace("if(p.endsWith('/message'))return reply({message:Object.assign({},row,", "if(p.endsWith('/message'))return reply({message:Object.assign({},[...rows,row].find(x=>x.uid===u.searchParams.get('uid'))||row,"))

# The AI answers with ONE step, chosen from the controls the panel sent by `pick` (a JS predicate).
AI = r"""(pick, extra)=>{ window.__aiCalls=[]; const P=window.__PC; P.ensureAiSession=async()=>{};
  P.uiConfirm=async()=>true;
  const real=P.authFetch; P.authFetch=async(url,opts)=>{ if(!String(url).includes('/api/chat-assist')) return real(url,opts);
    const body=JSON.parse(opts.body||'{}'); window.__aiCalls.push(body);
    const c=(body.controls||[]).find(new Function('c','return '+pick));
    const steps=c?[Object.assign({do:'click',ref:c.ref,target:c.label,label:'go',text:'',on:false},extra||{})]:[];
    return new Response(JSON.stringify({ok:true,answer:'ok',tasks:[],steps}),{status:200}); }; }"""


async def boot(b):
    await b.call('Emulation.setDeviceMetricsOverride', dict(width=1280, height=900, deviceScaleFactor=1, mobile=False))
    await desktop.login(b)
    await b.js("try{ if(window.PCOS && PCOS.isOn()) PCOS.exit(); }catch(_){}")


async def ask_and_do_all(b):
    await b.js("(()=>{const btn=document.createElement('button');document.body.appendChild(btn);PCOS.pageWindowAI(btn,{});})()")
    await b.until("!!document.querySelector('.osw-ai-panel textarea')")
    await b.js("(()=>{const p=document.querySelector('.osw-ai-panel');p.querySelector('textarea').value='do it';p.querySelector('[data-ai-ask]').click();})()")
    await b.until("!!document.querySelector('.osw-ai-panel [data-ai-all]')")
    await b.js("document.querySelector('.osw-ai-panel [data-ai-all]').click(); true")


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
def test_open_an_email_from_the_list():
    out = {}

    async def check(b):
        await boot(b)
        await b.js(MAIL)
        await b.until("document.querySelectorAll('.mail-item').length===3")
        await b.js("(" + AI + ")(\"c.role==='item' && /receipt/i.test(c.label)\")")
        await ask_and_do_all(b)
        await b.until("(document.querySelector('.mr-subj')||{}).textContent==='Your receipt from the hardware store'")
        out['controls'] = await b.js("window.__aiCalls[0].controls")

    asyncio.run(desktop.with_browser('online', '', check))
    ctls = out['controls']
    items = [c for c in ctls if c['role'] == 'item']
    assert len(items) == 3 and any('Lunch on Friday?' in c['label'] for c in items), ctls
    boxes = [c for c in ctls if c['role'] == 'checkbox' and c['label'] == 'Select']
    assert len(boxes) == 3 and all(c['near'] for c in boxes), ("a row's checkbox does not say which row", boxes)
    assert any(c['role'] == 'checkbox' and c['label'] == 'Select all' for c in ctls), ("select-all reads like a row's box", ctls)
    assert not [c for c in ctls if c['label'] == 'on'], ("a control is named after its value", ctls)


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
def test_turn_on_a_switch_in_settings():
    out = {}

    async def check(b):
        await boot(b)
        await b.js("__PC.switchView('settings'); true")
        await b.until("!!document.querySelector('#set-no-images')")
        out['before'] = await b.js("document.querySelector('#set-no-images').checked")
        await b.js("(" + AI + ")(\"c.label==='Data saver'\", {do:'toggle', on:true})")
        await ask_and_do_all(b)
        await b.until("document.querySelector('#set-no-images').checked")
        out['controls'] = await b.js("window.__aiCalls[0].controls")

    asyncio.run(desktop.with_browser('online', '', check))
    assert out['before'] is False, out.get('before')
    assert not [c for c in out['controls'] if c['label'] == 'on'], out['controls']
