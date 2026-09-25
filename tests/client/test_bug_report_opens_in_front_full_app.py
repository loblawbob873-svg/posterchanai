"""PosterChanOS: Bug Report opens IN FRONT -- in its own window, like the reply composer.

Reported: "Bug Report window hides behind all other windows". The launcher clicked `#rb-report` in the
DESKTOP'S page, so the issue composer was a modal on the desktop surface -- the one every app window
sits above. Both halves through the shipped bundle: the desktop asks for a `bugreport` popup window
and draws no modal of its own; a page opened as that popup runs the same #rb-report path and shows
the composer. desktop/main.js keeps it sticky (a composer must not close on blur).
"""
import asyncio
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope='module', autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


def test_the_bug_report_window_is_sticky_like_a_composer():
    main = (ROOT / "desktop/main.js").read_text()
    line = next(l for l in main.splitlines() if l.startswith("const STICKY_POPUPS"))
    assert "'bugreport'" in line and "'compose'" in line, line


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
def test_the_desktop_asks_for_a_bug_report_window_and_draws_nothing_behind_the_apps():
    got = {}

    async def check(b):
        await desktop.login(b)
        await b.until("document.body.classList.contains('os-on') && !!document.querySelector('#os-desk')")
        await b.js("document.getElementById('osfr')?.remove();document.documentElement.classList.remove('osfr-on')")
        # The compositor side: on PosterChanOS popups are real toplevels; here the request is recorded.
        await b.js("window.__asked=[];window.pcPopup.open=(k,r,a)=>{__asked.push(k);return true};"
                   "window.PCOSShell=Object.assign(window.PCOSShell||{},{available:()=>true});true")
        await b.until("!!document.querySelector('#os-desk [data-view=\"__bug\"]')")
        await b.js("(()=>{const e=document.querySelector('#os-desk [data-view=\"__bug\"]');"
                   "e.dispatchEvent(new MouseEvent('dblclick',{bubbles:true}));e.click();})();true")
        await asyncio.sleep(1.0)
        got['asked'] = await b.js("__asked")
        got['modal_in_desktop'] = await b.js("!!document.querySelector('#modal-root .modal-bg')")

    asyncio.run(desktop.with_browser('online', '', check, ''))
    assert got['asked'][:1] == ['bugreport'], got
    assert not got['modal_in_desktop'], "the issue composer was drawn in the desktop page, behind the apps"


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
def test_a_bug_report_window_opens_the_issue_composer():
    got = {}

    async def check(b):
        await desktop.login(b)
        await b.until("document.body.classList.contains('os-popup-compose')")
        await b.until("!!document.querySelector('#modal-root .modal-bg') || window.__toastSeen")
        got['modal'] = await b.js("(()=>{const m=document.querySelector('#modal-root .modal');return m?m.innerText.slice(0,200):''})()")

    # No relay here: answer the repo lookup with the configured 30617 announcement -- installed from the
    # page's FIRST script, because the popup clicks #rb-report as soon as somebody is signed in.
    extra = r"""
    (function hook(){ if(!window.Relay || !Relay.query) return setTimeout(hook, 20);
      const pk='4b56bbf41c92e586e88927acb78836eb49f2b184081ef852625cf78be7d56bd6';
      const repo={id:'e'.repeat(64),pubkey:pk,kind:30617,created_at:Math.floor(Date.now()/1000),
        tags:[['d','posterchanai'],['name','posterchanai']],content:'',sig:'0'.repeat(128)};
      const q=Relay.query.bind(Relay);
      Relay.query=async(f,...r)=>(JSON.stringify(f).includes('30617')?[repo]:q(f,...r)); })();
    window.__toastSeen=false;
    window.addEventListener('DOMContentLoaded',()=>{ const o=new MutationObserver(()=>{
      if([...document.querySelectorAll('.toast,.os-toast')].some(t=>/repo|issue|bug/i.test(t.textContent))) window.__toastSeen=true;});
      o.observe(document.body,{childList:true,subtree:true}); });
    """
    asyncio.run(desktop.with_browser('online', '?pcpopup=bugreport', check, extra))
    assert got['modal'], "a bug-report window must show the issue composer (or say why it cannot)"
