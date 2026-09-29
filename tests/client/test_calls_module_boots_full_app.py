"""The real bundled client boots with calls.js split out of app.js, and the call code answers.

Drives the SHIPPED desktop bundle (desktop/build-www.sh renders templates/client.html and copies
every client script) in headless Chrome, through the same harness as the offline desktop test.
What a broken split looks like, and what each assertion catches:

  * calls.js loaded AFTER app.js -> app.js's boot-time `_callsMod()` finds no factory, so the
    module's top-level code (the visibility listener, the saved zoom, the per-tab call device id)
    does not run at boot (the `pc_call_device` assertion); calls.js MISSING -> every entry point
    goes through the lazy fallback, answers with a Promise and fetches the file again;
  * a dependency app.js forgot to pass, or a live binding read by value -> a ReferenceError /
    TypeError the moment the Calls screen paints (`__errors`, and the painted screen);
  * the incoming-call subscription (`startCallSignaling`, called at login) not reaching the module
    -> a throw at login (`__errors`).
"""
import asyncio
from pathlib import Path

import pytest
from tests.client import test_desktop_offline_full_app as desktop


@pytest.fixture(scope='module', autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


async def _boot_and_call(b):
    # Script order in the page as actually loaded: calls.js is a classic script BEFORE app.js.
    order = await b.js("[...document.scripts].map(s=>(s.getAttribute('src')||'').split('?')[0].split('/').pop())"
                       ".filter(n=>n==='calls.js'||n==='app.js')")
    assert order == ['calls.js', 'app.js'], order
    assert await b.js("typeof window.PCCallsFactory==='function'")
    # BUILT WHILE app.js BOOTED, before anything called into it: the module's own top-level code (the
    # per-tab call device id) has already run. Loaded late, the factory would be built only on the
    # first call and this would still be empty here.
    assert await b.js("!!sessionStorage.getItem('pc_call_device')"), "calls.js was not built at boot"

    await desktop.login(b)   # login runs startCallSignaling() -> the module's incoming-call subscription
    # An entry point answers SYNCHRONOUSLY — the module was built while app.js booted, not on demand.
    assert await b.js("__PC.__rdZoomState()===null")   # no call yet: the real answer, not a pending load
    assert await b.js("(()=>{const r=__PC.__rdZoomBounds(800,450,3840,2160); return !!r && typeof r.then!=='function' && r.fit>0;})()")

    # The Calls screen is painted by the moved renderCalls through the ordinary navigation path
    # (the same route a `type:'call'` notification takes: ?view=calls -> switchView('calls')).
    await b.js("__PC.switchView('calls')")
    await b.until("(document.querySelector('#feed')||{}).innerText && document.querySelector('#feed').innerText.trim().length>0")

    # The remote-desktop viewer UI, painted by the moved _callUI through a moved test hook, reading
    # live `ME` through the module's state getter; then torn down again by the moved code.
    assert await b.js("__PC.__rdFakeViewerSession({control:true, geometry:{width:1920,height:1080}})") is True
    assert await b.js("!!document.getElementById('call-overlay')")
    assert await b.js("__PC.__rdSetControl(false)===false")
    assert await b.js("Array.isArray(__PC.__rdOutbox())")

    # calls.js was fetched exactly once — by its <script> tag, never again by the lazy loader.
    fetched = await b.js("performance.getEntriesByType('resource').filter(e=>/\\/calls\\.js(\\?|$)/.test(e.name)).length")
    assert fetched == 1, fetched
    assert not await b.js('__errors'), await b.js('__errors')


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
def test_the_bundled_client_boots_with_calls_js_and_its_entry_points_answer():
    asyncio.run(desktop.with_browser('online', '', _boot_and_call))
