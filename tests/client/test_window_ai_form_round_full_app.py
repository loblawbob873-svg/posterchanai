"""✨ A STEP THAT OPENS A FORM ENDS THE ROUND -- and the next round READS the form.

A task that spans windows ("add Bob Smith 555-1234 to my contacts") is two rounds: press New contact, then
fill the dialog that opened. Measured against the node's model (scripts/eval_window_ai.py, multi-* cases),
two things went wrong between the rounds, both silent:

  * the first round's plan often carries steps AFTER the press that opens the form, planned against the
    window as it was. "Do all" ran them anyway: the dialog covers the page but the controls under it are
    still connected, so the contact's name was typed into the Search box hidden BEHIND the form;
  * the second round was sent the text of the page BEHIND the dialog ("2 contacts, Alice Jones…") -- the
    form's own words (its headings, which field is which) never reached the model.

Driven in the shipped bundle with an addressbook on the (stubbed) server and the model's replies stubbed.
"""
import asyncio
import json
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop


@pytest.fixture(scope="module", autouse=True)
def bundle():
    yield from desktop.bundle.__wrapped__()


VCARD = "BEGIN:VCARD\r\nVERSION:3.0\r\nUID:c1\r\nFN:Alice Jones\r\nN:Jones;Alice;;;\r\nTEL:555-0101\r\nEND:VCARD\r\n"
API = {"/api/contacts/books": {"books": [{"id": "personal", "displayname": "Personal", "kind": "VADDRESSBOOK"}]},
       "/api/contacts/cards": {"cards": [{"uid": "c1", "ics": VCARD}]}}

DRIVE = r"""(async(api)=>{
  const sleep=ms=>new Promise(r=>setTimeout(r,ms));
  window.__sent=[]; const real=window.fetch;
  const reply=b=>{
    const c=b.controls||[], ref=l=>(c.find(x=>x.label===l)||{}).ref;
    // Round one, as the model really plans it: press New contact AND type the name into the search box
    // it could see -- a step for a window that is about to be covered by the form.
    if(__sent.length===1) return {answer:'Adding Bob.',tasks:[],steps:[
      {do:'click',ref:ref('New contact'),target:'New contact',label:'New contact'},
      {do:'fill',ref:ref('Search contacts'),target:'Search contacts',text:'Bob Smith',label:'Search'}]};
    return {answer:'Filling it in.',tasks:[],steps:ref('First')?[{do:'fill',ref:ref('First'),target:'First',text:'Bob',label:'First'}]:[]};
  };
  window.fetch=(u,o)=>{ const s=String(u);
    if(s.includes('/api/chat-assist')){ const b=JSON.parse(o.body); __sent.push(b);
      return Promise.resolve(new Response(JSON.stringify(Object.assign({ok:true},reply(b))),{status:200,headers:{'Content-Type':'application/json'}})); }
    for(const k of Object.keys(api)) if(s.includes(k))
      return Promise.resolve(new Response(JSON.stringify(api[k]),{status:200,headers:{'Content-Type':'application/json'}}));
    return real(u,o); };
  __PC.switchView('contacts');
  for(let i=0;i<60 && !document.querySelector('#feed [aria-label="New contact"],#ct-new');i++) await sleep(100);
  await sleep(500);
  PCOSWin.isWindow=()=>true; PCOSWin.viewOf=()=>'contacts';
  const btn=document.createElement('button'); document.body.appendChild(btn); PCOS.pageWindowAI(btn,{});
  await sleep(300);
  document.querySelector('.osw-ai-panel textarea').value='add Bob Smith 555-1234 to my contacts';
  document.querySelector('.osw-ai-panel [data-ai-ask]').click();
  const all=async n=>{ for(let i=0;i<60;i++){ const b=[...document.querySelectorAll('.osw-ai-panel [data-ai-all]')].pop();
      if(b && __sent.length>=n && !b.disabled){ b.click(); return true; } await sleep(100); } return false; };
  await all(1);
  for(let i=0;i<60 && __sent.length<2;i++) await sleep(100);
  const second=__sent[1]||null;
  if(second) await all(2);
  await sleep(800);
  const search=[...document.querySelectorAll('#feed input')].find(i=>/search/i.test(i.getAttribute('aria-label')||i.placeholder||''));
  const g=document.getElementById('cc-given');
  return {rounds:__sent.length, search:search?search.value:null,
          text:second?second.windows[0].text:null, history:second?second.history:null, given:g?g.value:null};
})"""


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
def test_a_form_that_opened_ends_the_round_and_is_what_the_next_round_reads():
    got = {}

    async def check(b):
        await b.call("Emulation.setDeviceMetricsOverride", {"width": 1100, "height": 860, "deviceScaleFactor": 1, "mobile": False})
        await desktop.login(b)
        await b.js("try{ if(window.PCOS && PCOS.isOn()) PCOS.exit(); }catch(_){}")
        got.update(await b.js(DRIVE + "(%s)" % json.dumps(API)))

    asyncio.run(desktop.with_browser("online", "", check))
    assert got["rounds"] >= 2, ("the form opened and ✨ never looked again", got)
    assert got["search"] == "", ("a step planned for the page was typed into the search box BEHIND the form", got)
    assert got["text"].startswith("Open dialog: New contact"), ("round two was not shown the form's own text", got["text"][:200])
    assert got["history"][0]["q"] == "add Bob Smith 555-1234 to my contacts", got["history"]
    assert got["history"][0]["did"] == ["pressed “New contact”"], ("history must say exactly what ran", got["history"])
    assert got["given"] == "Bob", ("the form was not filled", got)
