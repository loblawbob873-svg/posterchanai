"""The real bundled client boots with keys.js split out of app.js, and a real key press still works.

Drives the SHIPPED desktop bundle (desktop/build-www.sh renders templates/client.html and copies
every client script) in headless Chrome, through the same harness as the offline desktop test, and
presses keys through CDP's Input.dispatchKeyEvent — the browser's own keyboard path, trusted events,
not a synthetic `dispatchEvent`. What a broken split looks like, and what each assertion catches:

  * keys.js loaded AFTER app.js (or missing) -> app.js's boot-time `_keysMod()` finds no factory, the
    global keydown listeners are never registered, and every key does nothing — silently. Caught by
    WHERE the factory is built (during app.js's own evaluation, from `_keysMod`, not later from a
    lazy `_lzRun`) and by the key presses themselves;
  * a dependency app.js forgot to pass, or a live binding read by value -> a ReferenceError /
    TypeError on the first shortcut (`__errors`, console errors, and the view that did not change);
  * `_vimPane`'s setter missing -> h/l would move nothing; the forwarders app.js keeps
    (`_selectNote`, `_vimOn`, …) not reaching the module -> switchView's cursor reset throws.
"""
import asyncio
from pathlib import Path

import pytest
from tests.client import test_desktop_offline_full_app as desktop


@pytest.fixture(scope='module', autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


# Record WHERE the factory is built: the script being evaluated at that moment and the call stack.
# A setter on window catches keys.js's own `window.PCKeysFactory = …` assignment and wraps it.
BUILD_PROBE = r'''
window.__consoleErrors=[];
{const ce=console.error.bind(console);console.error=(...a)=>{__consoleErrors.push(a.map(x=>String(x&&x.stack||x)).join(' ').slice(0,400));ce(...a);};}
addEventListener('unhandledrejection',e=>__consoleErrors.push('unhandled: '+String(e.reason&&e.reason.stack||e.reason).slice(0,400)));
{let real;Object.defineProperty(window,'PCKeysFactory',{configurable:true,get(){return real;},set(f){
  real=function(dep){
    window.__keysBuilds=(window.__keysBuilds||0)+1;
    window.__keysBuiltIn=(document.currentScript&&document.currentScript.src||'').split('?')[0].split('/').pop();
    window.__keysBuiltStack=String(new Error().stack);
    window.__keysBuiltBeforePC=!window.__PC;
    return f.apply(this,arguments);
  };}});}
'''


async def _press(b, key, code, vk, alt=False):
    mods = 1 if alt else 0
    await b.call('Input.dispatchKeyEvent', {'type': 'rawKeyDown', 'key': key, 'code': code,
                                            'windowsVirtualKeyCode': vk, 'modifiers': mods})
    await b.call('Input.dispatchKeyEvent', {'type': 'keyUp', 'key': key, 'code': code,
                                            'windowsVirtualKeyCode': vk, 'modifiers': mods})


async def _boot_and_type(b):
    order = await b.js("[...document.scripts].map(s=>(s.getAttribute('src')||'').split('?')[0].split('/').pop())"
                       ".filter(n=>n==='keys.js'||n==='app.js')")
    assert order == ['keys.js', 'app.js'], order
    # BUILT ONCE, WHILE app.js WAS BEING EVALUATED, by its top-level `_keysMod()` — not on demand.
    assert await b.js("window.__keysBuilds") == 1
    assert await b.js("window.__keysBuiltIn") == 'app.js'
    assert await b.js("window.__keysBuiltBeforePC") is True
    stack = await b.js("window.__keysBuiltStack")
    assert '_keysMod' in stack and '_lzRun' not in stack, stack

    await desktop.login(b)
    await b.js("document.activeElement && document.activeElement.blur && document.activeElement.blur()")

    # Alt+B: the moved keydown listener -> _runShortcut -> dep switchView (which calls the forwarded
    # _selectNote(null) and writes app.js's own _vimPane) -> Bookmarks.
    assert not await b.js("__PC.isView('bookmarks')")
    await _press(b, 'b', 'KeyB', 66, alt=True)
    await b.until("__PC.isView('bookmarks')")

    # Alt+N: a second view, so the first was not a fluke of the initial state.
    await _press(b, 'n', 'KeyN', 78, alt=True)
    await b.until("__PC.isView('notifications')")

    # Alt+/ with Vim keys on: the help sheet, built by the moved _shortcutHelp through the forwarded
    # _vimOn and the passed `modal`, lists the Vim section only because the setting reads live.
    await b.js("ClientSettings.set('vimKeys', true)")
    await _press(b, '/', 'Slash', 191, alt=True)
    await b.until("(()=>{const t=document.body.innerText;return t.includes('Keyboard shortcuts')&&t.includes('Vim movement');})()")
    await _press(b, 'Escape', 'Escape', 27)
    await b.until("!document.body.innerText.includes('Vim movement')")
    await b.js("ClientSettings.set('vimKeys', false)")

    # keys.js was fetched exactly once — by its <script> tag, never again by the lazy loader.
    fetched = await b.js("performance.getEntriesByType('resource').filter(e=>/\\/keys\\.js(\\?|$)/.test(e.name)).length")
    assert fetched == 1, fetched
    assert await b.js("window.__keysBuilds") == 1
    assert not await b.js('__errors'), await b.js('__errors')
    assert not await b.js('__consoleErrors'), await b.js('__consoleErrors')


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
def test_the_bundled_client_boots_with_keys_js_and_a_real_key_press_works():
    asyncio.run(desktop.with_browser('online', '', _boot_and_type, extra_init=BUILD_PROBE))
