"""Every control in a Concord room can be pressed without a JavaScript error.

Reported: "concord invite link gives an error … current is not defined" — the room header's Invite button
passed a variable that only existed in the function that DRAWS the header, so every press threw. Nothing pressed
it: each Concord test drove one feature. This presses EVERY button in the room header and every entry of its ⋯
menu, at phone and desktop width, closing whatever each one opens, and fails on any uncaught error, unhandled
rejection, or toast that is a JavaScript error message. (The fixture room has no real relays, so a toast saying a
network step could not finish is not a failure — a crash is.)
"""
import asyncio
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop
from tests.client.test_concord_call_button_full_app import ROOM, EXTRA


@pytest.fixture(scope="module", autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


WATCH = r"""(()=>{window.__bad=[];
  addEventListener('unhandledrejection',e=>__bad.push('rejection: '+String(e.reason&&e.reason.message||e.reason)));
  addEventListener('error',e=>__bad.push('error: '+e.message));
  new MutationObserver(ms=>{for(const m of ms)for(const n of m.addedNodes){const t=(n.textContent||'');
    if(n.nodeType===1&&/is not defined|ReferenceError|TypeError|is not a function|Cannot read prop/i.test(t))__bad.push('toast: '+t.slice(0,160));}})
    .observe(document.body,{childList:true,subtree:true});})()"""
CLOSE = r"""(()=>{document.querySelectorAll('.modal-close,.cc-members-close,[data-close]').forEach(b=>{try{b.click()}catch(_){}});
  const d=document.getElementById('cc-members-dialog');if(d)d.classList.add('hidden');
  if(window.__PC&&__PC.closeModal)try{__PC.closeModal()}catch(_){}
  document.dispatchEvent(new KeyboardEvent('keydown',{key:'Escape',bubbles:true}));})()"""
HEADER = "[...document.querySelectorAll('.cc-conversation header button')].map(b=>b.id).filter(Boolean)"


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
@pytest.mark.parametrize("width", [360, 1280])
def test_every_room_control_presses_without_an_error(width):
    got = {"pressed": [], "bad": []}

    async def check(b):
        await b.call("Emulation.setDeviceMetricsOverride", dict(width=width, height=850, deviceScaleFactor=1,
                                                               mobile=width < 600))
        await desktop.login(b)
        await b.js(ROOM + "('cord','ok')")
        await b.until("!!document.querySelector('.cc-channel-list')")
        await b.until("!!document.querySelector('#cc-direct-send')")
        await b.js(WATCH)
        ids = [i for i in await b.js(HEADER) if i not in ("cc-back-channels", "cc-head-more", "cc-leave-shortcut")]
        for bid in ids:
            await b.js("(()=>{const x=document.getElementById(%r);if(x)x.click();})()" % bid)
            await asyncio.sleep(.6)
            got["pressed"].append(bid)
            await b.js(CLOSE)
            await asyncio.sleep(.2)
        # the ⋯ menu's own entries, by pressing ⋯ and choosing each
        rows = await b.js("""(async()=>{const m=document.getElementById('cc-head-more');if(!m)return [];m.click();
            await new Promise(r=>setTimeout(r,300));
            const items=[...document.querySelectorAll('.menu-popover [data-act],.menu-popover button,.mp-item')].map(x=>x.textContent.trim());
            document.dispatchEvent(new KeyboardEvent('keydown',{key:'Escape',bubbles:true}));return items;})()""")
        for label in rows:
            if "Leave" in label:
                continue
            await b.js("""(async()=>{document.getElementById('cc-head-more').click();await new Promise(r=>setTimeout(r,250));
                const it=[...document.querySelectorAll('.menu-popover [data-act],.menu-popover button,.mp-item')]
                  .find(x=>x.textContent.trim()===%r);if(it)it.click();})()""" % label)
            await asyncio.sleep(.6)
            got["pressed"].append("⋯ " + label)
            await b.js(CLOSE)
            await asyncio.sleep(.2)
        got["bad"] = await b.js("__bad")
        got["errors"] = await b.js("window.__errors||[]")
    asyncio.run(desktop.with_browser("online", "", check, EXTRA))
    assert "cc-direct-send" in got["pressed"] or any("Invite" in x for x in got["pressed"]), got["pressed"]
    assert len(got["pressed"]) >= 5, ("too few controls were pressed to mean anything", got["pressed"])
    problems = got["bad"] + [str(e) for e in got["errors"]]
    assert not problems, ("pressing room controls raised: %r (pressed %r)" % (problems, got["pressed"]))
