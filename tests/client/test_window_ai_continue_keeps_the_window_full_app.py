"""Window ✨ → Continue in a POPPED-OUT window must still send that window.

"the features we have don't even work: 'There is no window to ask about.'" On PosterChanOS every app is
its own window (oswin.js → PCOS.pageWindowAI), which is not one of the page's desktop windows, and
Continue filtered the connected windows by that list -- dropping the window itself. The server got
`windows: []` and answered 400. The real client; the AI endpoint is answered in the page.
"""
import asyncio
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop


@pytest.fixture(scope='module', autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


ANSWER_STEPS = r"""(()=>{ window.__aiCalls=[];
  const P=window.__PC; P.authFetch=async(url,opts)=>{ const body=JSON.parse(opts.body||'{}'); window.__aiCalls.push(body);
    if(!body.windows||!body.windows.length) return new Response(JSON.stringify({ok:false,error:'There is no window to ask about.'}),{status:400});
    return new Response(JSON.stringify({ok:true,answer:'Here is what you can do.',tasks:[],steps:[]}),{status:200}); };
  P.ensureAiSession=async()=>{}; })()"""


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
def test_continue_in_a_popped_out_window_sends_that_window():
    async def check(b):
        await b.call('Emulation.setDeviceMetricsOverride', dict(width=1280, height=900, deviceScaleFactor=1, mobile=False))
        await desktop.login(b)
        await b.js("try{ if(window.PCOS && PCOS.isOn()) PCOS.exit(); }catch(_){}")
        await b.js(ANSWER_STEPS)
        await b.js("(()=>{const btn=document.createElement('button');document.body.appendChild(btn);PCOS.pageWindowAI(btn,{});})()")
        await b.until("!!document.querySelector('.osw-ai-panel [data-ai-action]')")
        await b.js("document.querySelector('.osw-ai-panel [data-ai-action]').click()")
        await b.until("!!document.querySelector('.osw-ai-panel [data-ai-continue]')")
        await b.js("document.querySelector('.osw-ai-panel [data-ai-continue]').click()")
        await b.until("window.__aiCalls.length>=2")
        await asyncio.sleep(.3)
        calls = await b.js("window.__aiCalls.map(c=>({action:c.action,n:(c.windows||[]).length}))")
        assert calls[0] == {"action": "window_steps", "n": 1}, calls
        assert calls[1] == {"action": "window_steps", "n": 1}, f"Continue sent no window: {calls}"
        err = await b.js("(document.querySelector('.osw-ai-panel .osw-ai-answer.error')||{}).textContent||''")
        assert "no window" not in err, err
    asyncio.run(desktop.with_browser('online', '', check))


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
def test_every_panel_action_in_a_popped_out_window_reaches_the_ai_with_the_window():
    """Regression net for the whole panel, not just the button that broke: every suggestion, then
    Continue twice. Each must send the window and render an answer, never an error."""
    async def check(b):
        await b.call('Emulation.setDeviceMetricsOverride', dict(width=1280, height=900, deviceScaleFactor=1, mobile=False))
        await desktop.login(b)
        await b.js("try{ if(window.PCOS && PCOS.isOn()) PCOS.exit(); }catch(_){}")
        await b.js(ANSWER_STEPS)
        await b.js("(()=>{const btn=document.createElement('button');btn.id='ai-btn';document.body.appendChild(btn);PCOS.pageWindowAI(btn,{});})()")
        await b.until("!!document.querySelector('.osw-ai-panel [data-ai-action]')")
        n = await b.js("document.querySelectorAll('.osw-ai-panel [data-ai-action]').length")
        assert n >= 3, f"only {n} suggestions"
        for i in range(n):
            before = await b.js("window.__aiCalls.length")
            await b.js(f"document.querySelectorAll('.osw-ai-panel [data-ai-action]')[{i}].click()")
            await b.until(f"window.__aiCalls.length>{before} && !!document.querySelector('.osw-ai-panel [data-ai-continue]')")
            err = await b.js("(document.querySelector('.osw-ai-panel .osw-ai-answer.error')||{}).textContent||''")
            assert not err, f"suggestion {i}: {err}"
        for _ in range(2):
            before = await b.js("window.__aiCalls.length")
            await b.js("document.querySelector('.osw-ai-panel [data-ai-continue]').click()")
            await b.until(f"window.__aiCalls.length>{before} && !!document.querySelector('.osw-ai-panel [data-ai-continue]')")
        bad = await b.js("window.__aiCalls.filter(c=>!(c.windows&&c.windows.length)).length")
        assert bad == 0, await b.js("window.__aiCalls.map(c=>(c.windows||[]).length)")
        assert not await b.js("(document.querySelector('.osw-ai-panel .osw-ai-answer.error')||{}).textContent||''")
    asyncio.run(desktop.with_browser('online', '', check))
