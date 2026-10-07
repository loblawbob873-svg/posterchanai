"""Every PosterChan window gets ✨ buttons made for it -- and windows holding secrets get none.

The owner: "make sure all the AI window features have useful abilities in all the posterchan windows".
Walks EVERY view the sidebar lists (read from the shipped page, never a copied list) plus the desktop's
own screens, opening the ✨ panel for each the way a popped-out window does (PCOS.pageWindowAI), and
checks what a person would see:

  * no window falls back to the generic Summarize / To-dos / Help me set -- each has its own first button;
  * Passwords, the signer, the wallet and its connections show "AI is off" and send NOTHING, even when
    the person presses ✨ there;
  * a fixed-question button (Calendar's "What's coming up") sends that question with the window.
"""
import asyncio
import json
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop


@pytest.fixture(scope="module", autouse=True)
def bundle():
    yield from desktop.bundle.__wrapped__()


PRIVATE = {"vault", "signer", "wallet", "exodus", "connect"}
EXTRAS = ["__files", "__music", "__games", "__ossettings", "__tasks", "__installer", "__golive", "__remote"]

WALK = r"""(async(extras)=>{
  window.__sent=[]; const real=window.fetch;
  window.fetch=(u,o)=>{ if(String(u).includes('/api/chat-assist')){ __sent.push(JSON.parse(o.body));
      return Promise.resolve(new Response(JSON.stringify({ok:true,answer:'ok',tasks:[],steps:[]}),{status:200,headers:{'Content-Type':'application/json'}})); }
    return real(u,o); };
  const views=[...new Set([...document.querySelectorAll('.sidebar .nav-item[data-view]')].map(b=>b.dataset.view))].concat(extras);
  const btn=document.createElement('button'); document.body.appendChild(btn);
  const out={};
  for(const v of views){
    PCOSWin.isWindow=()=>true; PCOSWin.viewOf=()=>v;
    PCOS.pageWindowAI(btn,{});
    const p=document.querySelector('.osw-ai-panel');
    out[v]={private:!!(p&&p.classList.contains('osw-ai-private')),
            labels:p?[...p.querySelectorAll('[data-ai-action] b')].map(x=>x.textContent):[],
            ask:!!(p&&p.querySelector('textarea'))};
    // A press on ✨ in a private window, and a click on anything in its panel, must send nothing.
    if(out[v].private){ const before=__sent.length; p.querySelectorAll('button').forEach(b=>{ if(!b.hasAttribute('data-ai-dismiss')) b.click(); });
      out[v].sent=__sent.length-before; }
    PCOS.pageWindowAI(btn,{});   // toggles it closed
  }
  return out;})(%s)"""

GENERIC = ["Summarize", "To-dos & dates", "Help me with…"]


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
def test_every_window_has_its_own_buttons_and_secret_windows_have_none():
    got = {}

    async def check(b):
        await b.call("Emulation.setDeviceMetricsOverride", {"width": 1280, "height": 900, "deviceScaleFactor": 1, "mobile": False})
        await desktop.login(b)
        await b.js("try{ if(window.PCOS && PCOS.isOn()) PCOS.exit(); }catch(_){}")
        got["walk"] = await b.js(WALK % json.dumps(EXTRAS))
        # A fixed question goes out with the window: Budget's first button.
        await b.js("PCOSWin.isWindow=()=>true;PCOSWin.viewOf=()=>'budget';"
                   "(()=>{const x=document.createElement('button');document.body.appendChild(x);PCOS.pageWindowAI(x,{});})()")
        await b.until("!!document.querySelector('.osw-ai-panel [data-ai-action]')")
        await b.js("document.querySelector('.osw-ai-panel [data-ai-action]').click()")
        await b.until("__sent.length>0")
        got["budget"] = await b.js("__sent[__sent.length-1]")
        # Calendar's "What's coming up" answers from the calendar itself and sends NOTHING to the model:
        # asked of the model, an empty calendar came back with six invented meetings.
        await b.js("document.querySelector('.osw-ai-panel [data-ai-dismiss]').click();PCOSWin.viewOf=()=>'calendar';"
                   "window.__calBefore=__sent.length;(()=>{const x=document.createElement('button');document.body.appendChild(x);PCOS.pageWindowAI(x,{});})()")
        await b.until("!!document.querySelector('.osw-ai-panel [data-ai-action]')")
        await b.js("document.querySelector('.osw-ai-panel [data-ai-action]').click()")
        await b.until("(document.querySelector('.osw-ai-panel .osw-ai-answer')||{}).className==='osw-ai-answer'")
        got["cal"] = await b.js("({sent:__sent.length-__calBefore, text:document.querySelector('.osw-ai-panel .osw-ai-answer').innerText})")

    asyncio.run(desktop.with_browser("online", "", check))
    walk = got["walk"]
    assert len(walk) >= 40, f"only {len(walk)} views walked: {sorted(walk)}"
    for v in PRIVATE & set(walk):
        assert walk[v]["private"] and not walk[v]["labels"] and not walk[v]["ask"], (v, walk[v])
        assert walk[v]["sent"] == 0, (v, "a private window sent something to the AI")
    generic = sorted(v for v, r in walk.items() if not r["private"] and r["labels"][:3] == GENERIC)
    assert generic == [], f"these windows got only the generic buttons: {generic}"
    empty = sorted(v for v, r in walk.items() if not r["private"] and not r["labels"])
    assert empty == [], f"these windows got no buttons at all: {empty}"
    bud = got["budget"]
    assert bud["action"] == "window_recipe" and bud["recipe"] == "explain" and "this month" in bud["instruction"], bud
    cal = got["cal"]
    assert cal["sent"] == 0, ("What's coming up asked the model instead of reading the calendar", cal)
    assert "calendar" in cal["text"].lower(), cal
