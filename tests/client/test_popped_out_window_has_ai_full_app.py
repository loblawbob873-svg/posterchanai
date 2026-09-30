"""A popped-out PosterChan window has the same ✨ AI panel as the web desktop's windows.

Reported: "AI Features built into the posterchan windows on the webui are missing on PosterChanOS."
On the web desktop every window's title bar has ✨ (os.js toggleWindowAI: suggestions for what the
window shows, ask about it, watch it). On PosterChanOS each app is its own toplevel and draws
oswin.js's title bar, which had only − □ ×.

Runs the real bundled client as a popped-out Social window and asserts: ✨ is in the title bar and
on screen; it opens the same panel, fitted to the window; "Open in AI" hands the window's context to
the DESKTOP (which opens the AI window) and leaves this window's view alone.

A suggestion or "Ask" is ANSWERED IN THE PANEL (task: "Window AI: inline answers + approved per-app
actions"): the answer comes with Copy, Save to Notes, Insert into this window's text box (never sent,
and asks before replacing what is typed) and Continue in AI -- each a click, none automatic.
"""
import asyncio
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop


@pytest.fixture(scope="module", autouse=True)
def bundle():
    yield from desktop.bundle.__wrapped__()


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
@pytest.mark.parametrize("width,height", [(1100, 760), (420, 700)])
def test_the_sparkle_opens_the_ai_panel_and_hands_off_to_the_desktop(width, height):
    async def check(b):
        await desktop.login(b)
        await b.call("Emulation.setDeviceMetricsOverride", {"width": width, "height": height, "deviceScaleFactor": 1, "mobile": False})
        await b.until("!!(window.PCOSWin && PCOSWin.isWindow())")
        # The chrome is installed by adopt() on a real toplevel; the fixture has no compositor.
        await b.js("document.getElementById('pc-oswin-chrome') || PCOSWin.adopt({view:'global', label:'Social'})")
        await b.until("!!document.querySelector('#pc-oswin-chrome [data-action=\"ai\"]')")
        vis = await b.js("""(()=>{const r=document.querySelector('#pc-oswin-chrome [data-action="ai"]').getBoundingClientRect();
                           return r.width>=16&&r.height>=16&&r.right<=innerWidth&&r.left>=0})()""")
        assert vis, "✨ is not visible in the window's title bar"
        await b.js("""window.__asked=[]; PCOSWin.desktop=()=>({ __PC:{ askWindowContext:(ctx,ins,opt)=>{ __asked.push({ctx,ins,opt}); } } });""")
        view0 = await b.js("__PC.VIEW")
        await b.js("document.querySelector('#pc-oswin-chrome [data-action=\"ai\"]').click()")
        await b.until("!!document.querySelector('.osw-ai-panel')")
        fit = await b.js("""(()=>{const p=document.querySelector('.osw-ai-panel').getBoundingClientRect();
          return {fits:p.left>=0&&p.right<=innerWidth+1&&p.top>=38, actions:document.querySelectorAll('.osw-ai-panel [data-ai-action]').length,
                  title:(document.querySelector('.osw-ai-panel header b')||{}).textContent||''}})()""")
        assert fit["fits"] and fit["actions"] == 3, fit
        assert "Social" in fit["title"], fit
        await b.js("document.querySelector('.osw-ai-panel [data-ai-open]').click()")
        await asyncio.sleep(.3)
        asked = await b.js("__asked")
        assert len(asked) == 1 and asked[0]["ctx"]["windows"][0]["kind"] == "PosterChan app", asked
        assert await b.js("__PC.VIEW") == view0, "asking about the window repainted it"
        assert await b.js("!document.querySelector('.osw-ai-panel')"), "the panel stayed open after launching"

    asyncio.run(desktop.with_browser("online", "?pcwin=global", check))


STUB = r"""(()=>{
window.__req=[]; window.__reply={status:200, body:{ok:true, answer:'- First point\n- Second point'}};
const realFetch=window.fetch;
window.fetch=(url,opts)=>{ if(String(url).includes('/api/chat-assist')){ __req.push(JSON.parse(opts.body));
    return new Promise(r=>setTimeout(()=>r(new Response(JSON.stringify(__reply.body),{status:__reply.status,headers:{'Content-Type':'application/json'}})),250)); }
  return realFetch(url,opts); };
window.__asked=[]; PCOSWin.desktop=()=>({ __PC:{ askWindowContext:(ctx,ins,opt)=>{ __asked.push({ctx,ins,opt}); } } });
window.__copied=[]; __PC.copyValue=v=>{ __copied.push(v); return Promise.resolve(true); };
window.__notes=[]; window.PCNotes=Object.assign(window.PCNotes||{}, {save:async n=>{ __notes.push(n); return {ok:true}; }});
window.__confirm=false; __PC.uiConfirm=async()=>__confirm;
const f=document.getElementById('feed'); const t=document.createElement('textarea'); t.id='t-box'; t.style.cssText='display:block;width:200px;height:40px';
f.appendChild(t); t.focus();
})()
"""


async def _open(b, width, height):
    await desktop.login(b)
    await b.call("Emulation.setDeviceMetricsOverride", {"width": width, "height": height, "deviceScaleFactor": 1, "mobile": width < 600})
    await b.until("!!(window.PCOSWin && PCOSWin.isWindow())")
    await b.js("document.getElementById('pc-oswin-chrome') || PCOSWin.adopt({view:'global', label:'Social'})")
    await b.until("!!document.querySelector('#pc-oswin-chrome [data-action=\"ai\"]')")
    await b.js(STUB)
    await b.js("document.querySelector('#pc-oswin-chrome [data-action=\"ai\"]').click()")
    await b.until("!!document.querySelector('.osw-ai-panel')")


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
@pytest.mark.parametrize("width,height", [(1100, 760), (420, 700)])
def test_a_suggestion_is_answered_in_the_panel_with_actions_the_person_approves(width, height):
    async def check(b):
        await _open(b, width, height)
        view0 = await b.js("__PC.VIEW")
        await b.js("document.querySelector('.osw-ai-panel [data-ai-action=\"0\"]').click()")
        await b.until("!!document.querySelector('.osw-ai-answer.loading')")
        # A second tap while it thinks is the same question, not another request.
        await b.js("document.querySelector('.osw-ai-panel [data-ai-action=\"0\"]').click()")
        await b.until("!!document.querySelector('.osw-ai-text')")
        got = await b.js("""(()=>{const a=document.querySelector('.osw-ai-answer'), r=a.getBoundingClientRect();
          return {text:document.querySelector('.osw-ai-text').textContent, reqs:__req.length, asked:__asked.length,
                  req:__req[0], fits:r.left>=0&&r.right<=innerWidth+1,
                  btns:[...a.querySelectorAll('.osw-ai-do button')].map(b=>b.textContent.trim())}})()""")
        assert got["text"] == "- First point\n- Second point", got
        assert got["reqs"] == 1 and got["asked"] == 0, "answered here, once, without leaving for the AI screen"
        assert got["req"]["action"] == "window" and got["req"]["windows"][0]["kind"] == "PosterChan app", got
        assert got["req"]["instruction"], got
        assert got["fits"], got
        assert got["btns"] == ["Copy", "Save to Notes", "Insert", "Continue in AI"], got
        assert await b.js("__PC.VIEW") == view0, "asking repainted the window"
        assert await b.js("document.getElementById('t-box').value") == "", "nothing is put anywhere unasked"

        await b.js("document.querySelector('[data-ai-copy]').click()")
        await b.until("__copied.length===1")
        assert await b.js("__copied[0]") == "- First point\n- Second point"
        await b.js("document.querySelector('[data-ai-note]').click()")
        await b.until("__notes.length===1")
        note = await b.js("__notes[0]")
        assert note["body"] == "- First point\n- Second point" and "Social" in note["title"], note

        # Insert: asks before replacing what is typed, and a No changes nothing.
        await b.js("document.getElementById('t-box').value='my own words'")
        await b.js("document.querySelector('[data-ai-insert]').click()")
        await asyncio.sleep(.3)
        assert await b.js("document.getElementById('t-box').value") == "my own words"
        await b.js("__confirm=true; document.querySelector('[data-ai-insert]').click()")
        await b.until("document.getElementById('t-box').value.startsWith('- First')")
        assert await b.js("!document.querySelector('.osw-ai-panel')"), "the panel closes after inserting"
        assert await b.js("__asked.length") == 0, "insert never sends anything anywhere"

        # Continue in AI is the hand-off, on request.
        await b.js("document.querySelector('#pc-oswin-chrome [data-action=\"ai\"]').click()")
        await b.until("!!document.querySelector('.osw-ai-panel')")
        await b.js("document.querySelector('.osw-ai-panel textarea').value='What is this?'; document.querySelector('[data-ai-ask]').click()")
        await b.until("!!document.querySelector('[data-ai-more]')")
        assert await b.js("__req[1].instruction") == "What is this?"
        await b.js("document.querySelector('[data-ai-more]').click()")
        await b.until("__asked.length===1")
        assert await b.js("__asked[0].ins") == "What is this?"

    asyncio.run(desktop.with_browser("online", "?pcwin=global", check))


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
def test_a_refusal_is_said_in_the_panel():
    async def check(b):
        await _open(b, 1100, 760)
        await b.js("__reply={status:403, body:{ok:false, error:'AI access is not enabled for this account'}}")
        await b.js("document.querySelector('.osw-ai-panel [data-ai-action=\"1\"]').click()")
        await b.until("!!document.querySelector('.osw-ai-answer.error')")
        assert "not enabled" in await b.js("document.querySelector('.osw-ai-answer').textContent")
        assert await b.js("__asked.length") == 0
    asyncio.run(desktop.with_browser("online", "?pcwin=global", check))


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
def test_a_terminal_window_offers_to_type_the_command_and_never_runs_it():
    """Only a terminal window offers it, only with one command in the answer, and it closes the
    panel having TYPED (the terminal's typeIn), never run, that command."""
    async def check(b):
        await desktop.login(b)
        await b.until("!!(window.PCOSWin && PCOSWin.isWindow())")
        await b.js("document.getElementById('pc-oswin-chrome') || PCOSWin.adopt({view:'global', label:'Social'})")
        await b.until("!!document.querySelector('#pc-oswin-chrome [data-action=\"ai\"]')")
        await b.js(STUB)
        await b.js("window.__typed=[]; window.PCTerm=Object.assign(window.PCTerm||{}, {typeIn:t=>{ __typed.push(t); return true; }});"
                   "__reply={status:200, body:{ok:true, answer:'Install the header:\\n```bash\\nsudo apt install libfoo-dev\\n```'}};")
        # A Social window: the same answer offers no terminal action.
        await b.js("document.querySelector('#pc-oswin-chrome [data-action=\"ai\"]').click()")
        await b.until("!!document.querySelector('.osw-ai-panel')")
        await b.js("document.querySelector('.osw-ai-panel [data-ai-action=\"0\"]').click()")
        await b.until("!!document.querySelector('.osw-ai-text')")
        assert await b.js("!document.querySelector('[data-ai-type]')"), "only a terminal window types commands"
        await b.js("document.querySelector('[data-ai-dismiss]').click()")
        # The same page as a Terminal window.
        await b.js("PCOSWin.viewOf=()=>'terminal'")
        await b.js("document.querySelector('#pc-oswin-chrome [data-action=\"ai\"]').click()")
        await b.until("!!document.querySelector('.osw-ai-panel')")
        assert "Explain output" in await b.js("document.querySelector('.osw-ai-actions').textContent")
        await b.js("document.querySelector('.osw-ai-panel [data-ai-action=\"0\"]').click()")
        await b.until("!!document.querySelector('[data-ai-type]')")
        await b.js("document.querySelector('[data-ai-type]').click()")
        await b.until("__typed.length===1")
        assert await b.js("__typed[0]") == "sudo apt install libfoo-dev"
        assert await b.js("!document.querySelector('.osw-ai-panel')")
    asyncio.run(desktop.with_browser("online", "?pcwin=global", check))
