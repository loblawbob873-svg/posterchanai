"""A ✨ step that "fills" a control which is not a text box does what it means, and never throws.

Reported: clicking Reply on a step in a window's ✨ panel -> "action failed: illegal invocation". The
model's step was `fill` aimed at the post's Reply BUTTON with the reply text, and the panel called the
<input>/<textarea> value setter on a <button> -- TypeError: Illegal invocation (shell.log on the desktop:
os.js `set.set.call(el, st.text)`). A fill on a button means "open it, then type": the panel presses
Reply, waits for the reply box, and types the text there -- nothing is sent. A fill on a dropdown picks
the option. Real bundled client as a popped-out Social window; the model's reply is stubbed.
"""
import asyncio
import json
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop
from tests.client.test_popped_out_window_has_ai_full_app import _open


@pytest.fixture(scope="module", autouse=True)
def bundle():
    yield from desktop.bundle.__wrapped__()


SETUP = r"""(()=>{window.__errs=[];window.__published=[];
addEventListener('error',e=>__errs.push(String((e.error&&e.error.message)||e.message)));
addEventListener('unhandledrejection',e=>__errs.push(String((e.reason&&e.reason.message)||e.reason)));
window.__confirm=true;
Store.saveEvent({id:'a'.repeat(64),kind:1,pubkey:'b'.repeat(64),created_at:Math.floor(Date.now()/1000)-5,tags:[],content:'hello from bob',sig:''});
return true;})()"""

ANSWER = r"""(()=>{const rf=window.fetch;window.fetch=(u,o)=>{ if(String(u).includes('/api/chat-assist')){const b=JSON.parse(o.body);
  const reply=(b.controls||[]).find(c=>/^reply$/i.test(c.label)), pick=(b.controls||[]).find(c=>c.label==='Colour');
  const steps=[{do:'fill',ref:reply.ref,target:reply.label,label:'Reply',text:'Nice post!'},{do:'fill',ref:pick.ref,target:'Colour',label:'Colour',text:'Blue'}];
  return Promise.resolve(new Response(JSON.stringify({ok:true,answer:'ok',tasks:[],steps}),{status:200,headers:{'Content-Type':'application/json'}}));}
  return rf(u,o);};return true;})()"""


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
def test_a_fill_aimed_at_the_reply_button_opens_the_reply_and_types_it():
    res = {}

    async def check(b):
        await _open(b, 1100, 760)
        await b.js(SETUP)
        await b.js("__PC.switchView('global');true")
        await b.until("!!document.querySelector('#feed [data-a=\"reply\"]')")
        await b.js("""(()=>{const s=document.createElement('select');s.id='pick';s.setAttribute('aria-label','Colour');
            s.innerHTML='<option value="r">Red</option><option value="b">Blue</option>';s.style.cssText='display:block';
            document.getElementById('feed').prepend(s);return true;})()""")
        await b.js(ANSWER)
        await b.js("document.querySelector('.osw-ai-panel textarea').value='reply to it';document.querySelector('.osw-ai-panel [data-ai-ask]').click();true")
        await b.until("document.querySelectorAll('.osw-ai-step').length===2")
        await b.js("document.querySelector('.osw-ai-step[data-step=\"0\"] [data-act]').click();true")
        await asyncio.sleep(2)
        res["reply"] = await b.js("""(()=>{const t=document.getElementById('cmp');return {errs:__errs.splice(0),
            box:t?t.value:null, done:document.querySelector('.osw-ai-step[data-step="0"]').classList.contains('done')}})()""")
        await b.js("document.querySelector('.osw-ai-step[data-step=\"1\"] [data-act]').click();true")
        await asyncio.sleep(.5)
        res["pick"] = await b.js("({errs:__errs.splice(0), value:document.getElementById('pick').value})")
        # Undo puts back what was typed and picked -- and does not throw on the button that was pressed.
        await b.js("(()=>{const u=document.querySelector('.osw-ai-panel [data-ai-undo]');u&&u.click();})();true")
        await asyncio.sleep(.3)
        res["undo"] = await b.js("({errs:__errs.splice(0), value:document.getElementById('pick').value, box:(document.getElementById('cmp')||{}).value})")

    asyncio.run(desktop.with_browser("online", "?pcwin=global", check))
    assert res["reply"]["errs"] == [], res
    assert res["reply"]["box"] == "Nice post!" and res["reply"]["done"], ("the reply box did not open with the text", res)
    assert res["pick"] == {"errs": [], "value": "b"}, res
    assert res["undo"] == {"errs": [], "value": "r", "box": ""}, res


OPEN = r"""(()=>{window.__opened=[];PCOSWin.desktop=()=>({PCOSWin:{enabled:()=>true,routable:()=>true,open:(v,l)=>{__opened.push(v);return {};}},__PC:{}});
const rf=window.fetch;window.fetch=(u,o)=>{ if(String(u).includes('/api/chat-assist'))
  return Promise.resolve(new Response(JSON.stringify({ok:true,answer:'ok',tasks:[],steps:[
    {do:'open',label:'Open Messages',text:'messages'},{do:'open',label:'Open Social',text:'global'}]}),{status:200,headers:{'Content-Type':'application/json'}}));
  return rf(u,o);};
window.__toasts=[];const t=__PC.toast;__PC.toast=(m,...a)=>{__toasts.push(String(m));return t&&t(m,...a);};return true;})()"""


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
def test_an_open_step_in_a_window_opens_that_app_through_the_desktop():
    """'I clicked "Open Poster Chan bot status" and nothing happened': an Open step repainted the window it
    was pressed in -- nothing at all when it named the app already showing. It goes to the desktop, which
    opens that app's window; the app already here says so."""
    res = {}

    async def check(b):
        await _open(b, 1100, 760)
        await b.js(OPEN)
        view0 = await b.js("__PC.VIEW")
        await b.js("document.querySelector('.osw-ai-panel textarea').value='open it';document.querySelector('.osw-ai-panel [data-ai-ask]').click();true")
        await b.until("document.querySelectorAll('.osw-ai-step').length===2")
        await b.js("document.querySelector('.osw-ai-step[data-step=\"0\"] [data-go]').click();true")
        await asyncio.sleep(.4)
        res["other"] = await b.js(f"({{opened:__opened.slice(), view:__PC.VIEW==={view0!r}}})")
        # The panel closes after an Open; ask again for the second step.
        await b.js("document.querySelector('#pc-oswin-chrome [data-action=\"ai\"]').click();true")
        await b.until("!!document.querySelector('.osw-ai-panel')")
        await b.js("document.querySelector('.osw-ai-panel textarea').value='open it';document.querySelector('.osw-ai-panel [data-ai-ask]').click();true")
        await b.until("document.querySelectorAll('.osw-ai-step').length===2")
        await b.js("document.querySelector('.osw-ai-step[data-step=\"1\"] [data-go]').click();true")
        await asyncio.sleep(.4)
        res["same"] = await b.js("({opened:__opened.slice(), toasts:__toasts.slice()})")

    asyncio.run(desktop.with_browser("online", "?pcwin=global", check))
    assert res["other"] == {"opened": ["messages"], "view": True}, ("Open did not go to the desktop", res)
    assert res["same"]["opened"] == ["messages"] and any("this window" in t for t in res["same"]["toasts"]), res
