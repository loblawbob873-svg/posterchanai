"""THE DESKTOP ASSISTANT: "Do it" in the command bar plans desktop actions; the person presses to run them.

Asked 2026-10-28 ("AI desktop assistant"). The shipped desktop, real keys and clicks. The model's answer is canned
(the server's parser is tests/test_desktop_assist.py); what this asserts is what the PERSON gets: the request carries
the desktop as it is (the apps, the open windows, the workspaces), the plan shows as buttons and NOTHING happens
until one is pressed, Run all opens Notes and Settings and puts them side by side, a volume step sets that app's
level, and a step that cannot run says so instead of pretending.
"""
import asyncio
import json
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop

CHROME = pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')

PLAN = {"answer": "Opening Notes and Settings side by side, and turning Notes down.",
        "steps": [{"do": "open", "view": "notes", "label": "Open Notes"},
                  {"do": "open", "view": "settings", "label": "Open Settings"},
                  {"do": "snap", "win": 1, "zone": "left", "label": "Notes on the left"},
                  {"do": "snap", "win": 2, "zone": "right", "label": "Settings on the right"},
                  {"do": "volume", "app": "notes", "level": 30, "label": "Notes at 30%"}]}
STUB = r"""(plan=>{ window.__asked=[];
  const af=__PC.authFetch; __PC.authFetch=(u,o)=>{ if(!String(u).includes('/api/chat-assist')) return af(u,o);
    const b=JSON.parse(o.body); __asked.push(b);
    return Promise.resolve(new Response(JSON.stringify({ok:true,...plan}),{status:200,headers:{'Content-Type':'application/json'}})); };
  __PC.ensureAiSession=async()=>true; })"""
TITLES = "[...document.querySelectorAll('#os-root .osw')].filter(w=>getComputedStyle(w).display!=='none').map(w=>w.querySelector('.osw-title').textContent)"
GEOM = """(t=>{const w=[...document.querySelectorAll('#os-root .osw')].find(x=>x.querySelector('.osw-title').textContent===t).getBoundingClientRect(),
  d=document.querySelector('#os-desk').getBoundingClientRect();return {l:(w.left-d.left)/d.width,r:(w.right-d.left)/d.width}})(%s)"""


@pytest.fixture(scope='module', autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


async def _key(b, key, code, vk, ctrl=False):
    for kind in ('rawKeyDown', 'keyUp'):
        await b.call('Input.dispatchKeyEvent', {'type': kind, 'key': key, 'code': code, 'windowsVirtualKeyCode': vk,
                                                'nativeVirtualKeyCode': vk, 'modifiers': 2 if ctrl else 0})


async def _click(b, sel):
    x, y = await b.js("(()=>{const r=document.querySelector(%r).getBoundingClientRect();return [r.left+r.width/2,r.top+r.height/2]})()" % sel)
    for kind in ('mousePressed', 'mouseReleased'):
        await b.call('Input.dispatchMouseEvent', {'type': kind, 'x': x, 'y': y, 'button': 'left', 'clickCount': 1})


async def _ask(b, words):
    await _key(b, ' ', 'Space', 32, ctrl=True)
    await b.until("document.activeElement && document.activeElement.classList.contains('cmd-q')")
    await b.call('Input.insertText', {'text': words})
    await b.until("[...document.querySelectorAll('.cmd-sec[data-source=more] .cmd-row b')].some(x=>/^Do it/.test(x.textContent))")
    for _ in range(14):
        if await b.js("/^Do it/.test((document.querySelector('.cmd-row.on b')||{}).textContent||'')"):
            break
        await _key(b, 'ArrowDown', 'ArrowDown', 40)
    await _key(b, 'Enter', 'Enter', 13)
    await b.until("!!document.querySelector('.pc-assist .as-step')")


@CHROME
def test_do_it_plans_then_runs_only_when_pressed():
    got = {}

    async def check(b):
        await b.call('Emulation.setDeviceMetricsOverride', {'width': 1600, 'height': 900, 'deviceScaleFactor': 1, 'mobile': False})
        await desktop.login(b)
        await b.until("document.body.classList.contains('os-on')")
        await asyncio.sleep(.5)
        await b.js(STUB + "(%s);true" % json.dumps(PLAN))
        before = await b.js(TITLES)
        await _ask(b, 'notes and settings side by side, notes quieter')
        got['asked'] = await b.js("__asked[0]")
        got['steps'] = await b.js("[...document.querySelectorAll('.as-step b')].map(x=>x.textContent)")
        await asyncio.sleep(.6)
        got['untouched'] = (await b.js(TITLES)) == before          # nothing ran on its own
        await _click(b, '.pc-assist .as-all')
        await b.until("document.querySelectorAll('.pc-assist .as-step.done').length===5 || !document.querySelector('.pc-assist')")
        await asyncio.sleep(.5)
        got['titles'] = sorted(await b.js(TITLES))
        got['notes'] = await b.js(GEOM % json.dumps('Notes'))
        got['settings'] = await b.js(GEOM % json.dumps('Settings'))
        got['vol'] = await b.js("PCOS.appVolumes().find(r=>r.app==='notes')")

    asyncio.run(desktop.with_browser('online', '', check))
    a = got['asked']
    assert a['action'] == 'desktop_steps' and a['instruction'] == 'notes and settings side by side, notes quieter', a
    assert any(x['view'] == 'notes' for x in a['apps']) and 'workspaces' in a and 'open_windows' in a, a
    assert got['steps'][:2] == ['Open Notes', 'Open Settings'], got['steps']
    assert got['untouched'], "the plan ran before anybody pressed anything"
    assert 'Notes' in got['titles'] and 'Settings' in got['titles'], got['titles']
    assert got['notes']['l'] < .02 and abs(got['notes']['r'] - .5) < .03, ("Notes is not on the left half", got['notes'])
    assert abs(got['settings']['l'] - .5) < .03 and got['settings']['r'] > .97, ("Settings is not on the right half", got['settings'])
    assert abs(got['vol']['v'] - .3) < 1e-6 and not got['vol']['m'], got['vol']


@CHROME
def test_a_step_that_cannot_run_says_why():
    plan = {"answer": "Closing the window.", "steps": [{"do": "close", "win": 1, "label": "Close Notes"}]}

    async def check(b):
        await desktop.login(b)
        await b.until("document.body.classList.contains('os-on')")
        await b.js("PCOS.routeView('notes');true")
        await b.until("[...document.querySelectorAll('#os-root .osw .osw-title')].some(t=>t.textContent==='Notes')")
        await b.js(STUB + "(%s);true" % json.dumps(plan))
        await _ask(b, 'close notes')
        # The window goes away BEFORE the person presses the step.
        await b.js("document.querySelector('#os-root .osw .osw-b[data-w=close]').click();true")
        await asyncio.sleep(.4)
        await _click(b, '.pc-assist .as-step')
        await b.until("!!document.querySelector('.pc-assist .as-step.bad')")
        said = await b.js("document.querySelector('.pc-assist .as-step .as-s').textContent")
        assert 'not open any more' in said, said
    asyncio.run(desktop.with_browser('online', '', check))
