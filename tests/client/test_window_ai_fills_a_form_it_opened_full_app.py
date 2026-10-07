"""✨ can ADD a contact: it presses "+ Contact", then SEES the form that opened and fills it.

"make that agentic window AI stuff actually useful". The app's forms (New contact, New event, a bill)
open as a dialog on <body>, outside the window's own slot -- so the controls ✨ was shown never included
them, and "add Bob, 555-1234" could only ever press "+ Contact" and stop in front of an empty form. (On a fresh
account "+ Contact" first asks to name an addressbook -- a dialog all the same, and the case run here.)
Driven in the shipped bundle with the model's replies stubbed: round one presses + Contact; pressing
"Do all" must look AGAIN on its own (a form opened, the job is not done), that second look must list the
form's fields, and its steps must land in the real inputs.
"""
import asyncio
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop


@pytest.fixture(scope="module", autouse=True)
def bundle():
    yield from desktop.bundle.__wrapped__()


DRIVE = r"""(async()=>{
  const sleep=ms=>new Promise(r=>setTimeout(r,ms));
  window.__sent=[]; const real=window.fetch;
  const reply=b=>{
    const c=b.controls||[], ref=l=>(c.find(x=>x.label===l)||{}).ref;
    if(__sent.length===1) return {answer:'Opening a new contact.',tasks:[],steps:[{do:'click',ref:ref('New contact'),label:'New contact'}]};
    const steps=[]; if(ref('First')) steps.push({do:'fill',ref:ref('First'),text:'Bob',label:'First name'});
    if(ref('Phone number')) steps.push({do:'fill',ref:ref('Phone number'),text:'555-1234',label:'Phone'});
    // A fresh account has no addressbook yet, so "+ Contact" first asks for one -- the same dialog shape.
    if(!steps.length && ref('Name')) steps.push({do:'fill',ref:ref('Name'),text:'Bob',label:'Name'});
    return {answer:'Filling it in.',tasks:[],steps};
  };
  window.fetch=(u,o)=>{ if(String(u).includes('/api/chat-assist')){ const b=JSON.parse(o.body);
      // Only the AGENT's rounds are scripted and counted. A direct tool the request reaches first (Contacts'
      // window_contact) gets nothing usable back, so the request falls back to the agent -- also covered here.
      if(b.action && b.action!=='window_steps') return Promise.resolve(new Response('{"ok":true}',{status:200,headers:{'Content-Type':'application/json'}}));
      __sent.push(b);
      return Promise.resolve(new Response(JSON.stringify(Object.assign({ok:true},reply(b))),{status:200,headers:{'Content-Type':'application/json'}})); }
    return real(u,o); };
  __PC.switchView('contacts');
  for(let i=0;i<50 && !document.getElementById('ct-new');i++) await sleep(100);
  PCOSWin.isWindow=()=>true; PCOSWin.viewOf=()=>'contacts';
  const btn=document.createElement('button'); document.body.appendChild(btn); PCOS.pageWindowAI(btn,{});
  await sleep(300);
  const ta=document.querySelector('.osw-ai-panel textarea'); ta.value='add Bob, 555-1234';
  document.querySelector('.osw-ai-panel [data-ai-ask]').click();
  const all=async n=>{ for(let i=0;i<60;i++){ const b=[...document.querySelectorAll('.osw-ai-panel [data-ai-all]')].pop();
      if(b && __sent.length>=n && !b.disabled){ b.click(); return true; } await sleep(100); } return false; };
  const first=await all(1);
  for(let i=0;i<60 && __sent.length<2;i++) await sleep(100);
  const second=__sent[1]||null;
  if(second) await all(2);
  await sleep(800);
  const dlg=[...document.querySelectorAll('.modal-bg .modal')].pop();
  const g=document.getElementById('cc-given'), tel=document.querySelector('#cc-tels input');
  return {first, rounds:__sent.length, labels:second?second.controls.map(c=>c.label):null,
          form:!!g, given:g?g.value:(dlg&&dlg.querySelector('input')?dlg.querySelector('input').value:null), tel:tel?tel.value:null};
})()"""

CHROME = pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")


@CHROME
def test_ai_presses_new_contact_then_fills_the_form_that_opened():
    got = {}

    async def check(b):
        await b.call("Emulation.setDeviceMetricsOverride", {"width": 1100, "height": 860, "deviceScaleFactor": 1, "mobile": False})
        await desktop.login(b)
        await b.js("try{ if(window.PCOS && PCOS.isOn()) PCOS.exit(); }catch(_){}")
        got.update(await b.js(DRIVE))

    asyncio.run(desktop.with_browser("online", "", check))
    assert got["first"], got
    assert got["rounds"] >= 2, ("pressing + Contact opened a form and ✨ never looked again", got)
    assert "Search contacts" not in got["labels"], ("✨ was shown the window BEHIND the open form", got)
    assert ("First" in got["labels"] and "Phone number" in got["labels"]) or got["labels"] == ["Name", "Create"], \
        ("the open form's fields were not shown to the model", got)
    assert got["given"] == "Bob", ("the form was not filled", got)
    if got["form"]:
        assert got["tel"] == "555-1234", got
