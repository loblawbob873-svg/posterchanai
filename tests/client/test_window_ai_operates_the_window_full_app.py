"""Window ✨ OPERATES the window: "need way to interact with the current window and do stuff".

The panel sends the window's visible controls, numbered; the AI answers with click/fill/choose/toggle
steps naming those numbers; "Do all" performs them on the real elements. Anything that deletes, sends
or pays asks first, and a "no" stops the run. A password field is never sent. The real client in a
popped-out window (PCOS.pageWindowAI); the AI endpoint is answered in the page.
"""
import asyncio
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop


@pytest.fixture(scope='module', autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


FORM = r"""(()=>{ const f=document.getElementById('feed'); const d=document.createElement('div'); d.id='t-form';
  d.innerHTML=`<label for="t-title">Title</label><input id="t-title" type="text">
    <label>Colour <select id="t-col"><option value="r">Red</option><option value="b">Blue</option></select></label>
    <label><input id="t-pin" type="checkbox"> Pin it</label>
    <label for="t-pw">Secret password</label><input id="t-pw" type="password" value="hunter2">
    <button id="t-save" type="button">Save</button><button id="t-del" type="button">Delete everything</button>`;
  f.prepend(d); window.__saved=null; window.__deleted=false;
  d.querySelector('#t-save').onclick=()=>{ window.__saved=document.getElementById('t-title').value; };
  d.querySelector('#t-del').onclick=()=>{ window.__deleted=true; };
  window.__inputs=0; d.querySelector('#t-title').addEventListener('input',()=>window.__inputs++); })()"""

AI = r"""(()=>{ window.__aiCalls=[]; window.__confirms=[];
  const P=window.__PC; P.ensureAiSession=async()=>{};
  P.uiConfirm=async(msg)=>{ window.__confirms.push(msg); return false; };
  const real=P.authFetch; P.authFetch=async(url,opts)=>{ if(!String(url).includes('/api/chat-assist')) return real(url,opts); const body=JSON.parse(opts.body||'{}'); window.__aiCalls.push(body);
    const ref=l=>((body.controls||[]).find(c=>c.label===l)||{}).ref;
    const steps=window.__aiCalls.length>1&&window.__secondEmpty?[]:[
      {do:'fill',ref:ref('Title'),target:'Title',label:'Fill title',text:'Groceries',on:false},
      {do:'choose',ref:ref('Colour'),target:'Colour',label:'Pick blue',text:'Blue',on:false},
      {do:'toggle',ref:ref('Pin it'),target:'Pin it',label:'Pin',text:'',on:true},
      {do:'click',ref:999,target:'Made up',label:'Made up',text:'',on:false},
      {do:'click',ref:ref('Save'),target:'Save',label:'Save',text:'',on:false},
      ...(window.__noDelete?[]:[{do:'click',ref:ref('Delete everything'),target:'Delete everything',label:'Delete',text:'',on:false}])];
    return new Response(JSON.stringify({ok:true,answer:'Doing it.',tasks:[],steps}),{status:200}); }; })()"""


async def _open(b, pre=""):
    await b.call('Emulation.setDeviceMetricsOverride', dict(width=1280, height=900, deviceScaleFactor=1, mobile=False))
    await desktop.login(b)
    await b.js("try{ if(window.PCOS && PCOS.isOn()) PCOS.exit(); }catch(_){}")
    await b.js(FORM)
    await b.js(AI)
    if pre:
        await b.js(pre)
    await b.js("(()=>{const btn=document.createElement('button');document.body.appendChild(btn);PCOS.pageWindowAI(btn,{});})()")
    await b.until("!!document.querySelector('.osw-ai-panel textarea')")
    await b.js("(()=>{const p=document.querySelector('.osw-ai-panel');p.querySelector('textarea').value='make a groceries entry';p.querySelector('[data-ai-ask]').click();})()")
    await b.until("!!document.querySelector('.osw-ai-panel [data-ai-all]')")


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
def test_do_all_fills_chooses_ticks_and_presses_but_asks_before_deleting():
    async def check(b):
        await _open(b)
        sent = await b.js("JSON.stringify(window.__aiCalls[0].controls||[])")
        assert '"Title"' in sent and '"Save"' in sent and '"Colour"' in sent, f"the window's controls were not sent: {sent}"
        assert "hunter2" not in sent and "password" not in sent.lower(), "a password field reached the AI"
        n = await b.js("document.querySelectorAll('.osw-ai-panel .osw-ai-step [data-act]').length")
        assert n == 5, f"expected 5 action steps (the invented control dropped), got {n}"
        await b.js("document.querySelector('.osw-ai-panel [data-ai-all]').click()")
        await b.until("window.__confirms.length>0")
        await asyncio.sleep(.3)
        st = await b.js("({t:document.getElementById('t-title').value,c:document.getElementById('t-col').value,"
                        "p:document.getElementById('t-pin').checked,saved:window.__saved,del:window.__deleted,"
                        "inputs:window.__inputs,conf:window.__confirms})")
        assert st["t"] == "Groceries" and st["inputs"] >= 1, st
        assert st["c"] == "b" and st["p"] is True and st["saved"] == "Groceries", st
        assert st["del"] is False, "a delete was pressed after the person said no"
        assert len(st["conf"]) == 1 and "Delete everything" in st["conf"][0], st["conf"]
        done = await b.js("document.querySelectorAll('.osw-ai-panel .osw-ai-step.done').length")
        assert done == 4, done
    asyncio.run(desktop.with_browser('online', '', check))


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
def test_keep_going_reads_the_window_again_and_continues():
    async def check(b):
        await _open(b, "window.__noDelete=true; window.__secondEmpty=true")
        before = await b.js("window.__aiCalls.length")
        await b.js("(()=>{const k=document.querySelector('.osw-ai-panel [data-ai-keep]');k.checked=true;k.dispatchEvent(new Event('change'));"
                   "document.querySelector('.osw-ai-panel [data-ai-all]').click();})()")
        await b.until(f"window.__aiCalls.length>{before}")
        last = await b.js("window.__aiCalls[window.__aiCalls.length-1]")
        assert last["instruction"].startswith("Continue"), last["instruction"]
        assert any(c["label"] == "Title" and c["value"] == "Groceries" for c in last["controls"]), \
            "Continue did not re-read the window's controls as they are now"
        assert await b.js("window.__saved") == "Groceries"
    asyncio.run(desktop.with_browser('online', '', check))
