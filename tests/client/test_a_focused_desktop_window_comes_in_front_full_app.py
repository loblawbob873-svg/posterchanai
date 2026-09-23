"""A window the desktop draws comes IN FRONT of the windows it overlaps, however it was opened.

Two reports, one mechanism. Preview, Search, folders and System Settings are drawn INSIDE the
desktop surface, so they are visible only when main puts the desktop above the applications they
overlap -- the ids the renderer publishes as `covers` with `front: true` (pcWM.shellFront).

1. "pc-open tried opening PDF and the window went behind all the other windows." Measured on the
   desktop: the Preview frame was focused and maximised, and `covers` went out EMPTY. `domStackPlan`
   asked only what fraction of the FRAME a window overlapped; a maximised document over a
   1358px-wide Notes window overlaps a small part of itself, so Notes was never listed.

2. "Taskbar Search -> Search window always goes behind active windows and always stays behind
   firefox." `focusWin` stated `front: true`, then its own `drawBar()` re-derived `false` from the
   stale `_foreignFocused` (Firefox had the keyboard a moment ago), and the shell's focus was then
   answered by the bottom guard's lowerShell.

3. "I can't open music anymore ... I see it on the taskbar." Measured on the desktop: the Music
   window was drawn, buried under the popped-out Terminal and Social. A press on its taskbar button
   is a press on the desktop surface, so Wayfire hands the desktop the keyboard; the adopt pass sees
   that before `click` fires, `_webTaskActive` then called Music "the window you are using", and the
   button MINIMISED it. The answer is taken at pointerdown now.

The fixture is the compositor as main presents it: a snapshot with the desktop's own surface, a
popped-out PosterChan window and Firefox, and a log of what the page publishes. Neither case has a
press on the frame -- pc-open arrives over a socket and Search opens from a key press in the bar.
"""
import asyncio
import json
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop


@pytest.fixture(scope='module', autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


COMPOSITOR = r'''
window.__publishOK=true;
window.__wm={log:[],focus:40};
const __rows=()=>[
  {id:13,app:'place.poster.desktop',title:'PosterChan Desktop',workspace:'1',
   rect:{x:0,y:0,width:innerWidth,height:innerHeight},focused:__wm.focus===13},
  {id:35,app:'place.poster.desktop',title:'PosterChan Window — notes',workspace:'1',
   rect:{x:200,y:60,width:400,height:400},focused:__wm.focus===35},
  // The popped-out TERMINAL -- our own app id, so never "foreign", and in every report the window
  // that had the keyboard when the thing behind it went missing.
  {id:31,app:'place.poster.desktop',title:'PosterChan Window — terminal',workspace:'1',
   rect:{x:40,y:30,width:innerWidth-80,height:innerHeight-140},focused:__wm.focus===31},
  {id:40,app:'firefox',title:'Mozilla Firefox',workspace:'1',
   rect:{x:0,y:0,width:innerWidth,height:innerHeight-60},focused:__wm.focus===40}];
window.pcWM={windows:async()=>__rows(),launch:async()=>({pid:1}),
  snapshot:async()=>({windows:__rows(),allIds:[13,31,35,40],shellId:13}),
  onEvent:(cb)=>{__wm.emit=cb;return()=>{}},
  focus:async(id)=>{__wm.log.push({focus:id});__wm.focus=id;return true},
  shellFront:async(w)=>{__wm.log.push({front:!!(w&&w.front),covers:(w&&w.covers)||[]});return true}};
'''


def _last_front(log):
    fronts = [x for x in log if 'front' in x]
    assert fronts, log
    return fronts[-1]


async def _until_front(b, cover_id, timeout=15.0):
    """Wait until the page has published front:true with `cover_id` among the covers."""
    expr = ("(()=>{const f=__wm.log.filter(x=>'front' in x);const l=f[f.length-1];"
            "return !!(l&&l.front&&l.covers.includes(%s))})()" % cover_id)
    for _ in range(int(timeout / .1)):
        if await b.js(expr):
            return
        await asyncio.sleep(.1)


async def _ready(b):
    await desktop.login(b)
    await b.until("!!document.body && document.body.classList.contains('os-on') && !!document.querySelector('#os-desk')")
    await b.js("document.getElementById('osfr')?.remove();document.documentElement.classList.remove('osfr-on')")
    await asyncio.sleep(1)
    await b.js("__wm.focus=40;__wm.log=[]")


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
def test_a_maximised_preview_is_put_above_a_smaller_window_it_covers():
    async def check(b):
        await _ready(b)
        await b.js("__wm.focus=35")        # the terminal-ish popped-out window has the keyboard
        opened = await b.js("PCPreview.open({name:'m.pdf',mime:'application/pdf',"
                            "blob:new Blob(['%PDF-1.4'],{type:'application/pdf'})})")
        assert opened is True
        # Poll, never sleep: under a loaded full-suite run the snapshot round trip is slow, and a
        # fixed 1.5s read the log before the cover list had been published.
        await _until_front(b, "35")
        log = await b.js('__wm.log')
        last = _last_front(log)
        assert last['front'] is True, log
        assert 35 in last['covers'], 'the maximised Preview left Notes drawn over it: ' + json.dumps(log)
        assert {'focus': 13} in log, log

    asyncio.run(desktop.with_browser('online', '', check, COMPOSITOR))


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
def test_a_taskbar_search_is_not_sunk_under_the_browser_that_had_focus():
    async def check(b):
        await _ready(b)
        await b.js("(()=>{const q=document.querySelector('#os-q-bar');q.focus();q.value='bitcoin';"
                   "q.dispatchEvent(new KeyboardEvent('keydown',{key:'Enter',bubbles:true}))})()")
        await _until_front(b, "40")
        assert await b.js("[...document.querySelectorAll('.osw.focused .osw-title')].some(t=>/search/i.test(t.textContent))")
        log = await b.js('__wm.log')
        after = log[next(i for i, x in enumerate(log) if x.get('front') is True):]
        assert all(x.get('front') is not False for x in after), \
            'focusWin stated the front and then its own drawBar took it back: ' + json.dumps(log)
        assert 40 in _last_front(log)['covers'], log

    asyncio.run(desktop.with_browser('online', '', check, COMPOSITOR))


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
def test_a_buried_windows_taskbar_button_raises_it_even_though_the_press_focuses_the_desktop():
    async def check(b):
        await _ready(b)
        await b.until("typeof __wm.emit==='function'")
        await b.js("window.__w=PCOS.openDoc('buried','Music','i-music',()=>{},true);true")
        await b.until("!!document.querySelector('.os-task[data-id]')")
        # The terminal takes the keyboard; the desktop learns it from the compositor.
        await b.js("__wm.focus=31;__wm.emit({name:'window',change:'focus'})")
        await asyncio.sleep(.5)
        # Wayfire's click-to-focus: the PRESS focuses the desktop, and the event reaches the page
        # over IPC once the press itself has been dispatched -- never inside it.
        await b.js("window.addEventListener('pointerdown',()=>{__wm.focus=13;"
                   "setTimeout(()=>__wm.emit({name:'window',change:'focus'}),0)},true);true")
        box = await b.js("(()=>{const t=[...document.querySelectorAll('.os-task[data-id]')].find(x=>"
                         "/music/i.test(x.title||x.getAttribute('aria-label')||x.textContent));"
                         "const r=t.getBoundingClientRect();return [r.left+r.width/2,r.top+r.height/2]})()")
        await b.call('Input.dispatchMouseEvent', {'type': 'mousePressed', 'x': box[0], 'y': box[1],
                                                  'button': 'left', 'buttons': 1, 'clickCount': 1})
        await asyncio.sleep(.25)          # a real press lasts ~100ms; the adopt pass lands inside it
        await b.call('Input.dispatchMouseEvent', {'type': 'mouseReleased', 'x': box[0], 'y': box[1],
                                                  'button': 'left', 'buttons': 0, 'clickCount': 1})
        await _until_front(b, "31")
        state = await b.js("({min:!!__w.min,cls:__w.el.className})")
        assert not state['min'], 'the taskbar press minimised the window it was meant to raise: %r' % state
        assert 31 in _last_front(await b.js('__wm.log'))['covers']

    asyncio.run(desktop.with_browser('online', '', check, COMPOSITOR))


# ---- the everyday sequence around those three bugs ------------------------------------------------
# Each of the reports above was one step of an ordinary day at the desk: use Firefox, go back to a
# window drawn on the desktop, go back to Firefox, close things. A fix for one step that breaks the
# next is how this area kept regressing, so the whole walk is one test and every step is asserted.

def _front(log):
    fronts = [x for x in log if 'front' in x]
    return fronts[-1] if fronts else None


async def _press(b, selector):
    box = await b.js("(()=>{const r=document.querySelector(%s).getBoundingClientRect();"
                     "return [r.left+r.width/2,r.top+Math.min(r.height/2,40)]})()" % json.dumps(selector))
    for kind in ('mousePressed', 'mouseReleased'):
        await b.call('Input.dispatchMouseEvent', {'type': kind, 'x': box[0], 'y': box[1], 'button': 'left',
                                                  'buttons': 1 if kind == 'mousePressed' else 0, 'clickCount': 1})


async def _compositor_focus(b, view_id):
    """The compositor moves the keyboard and tells the page afterwards, over IPC."""
    await b.js("__wm.focus=%d;setTimeout(()=>__wm.emit({name:'window',change:'focus'}),0);true" % view_id)


async def _until(b, expr, timeout=15.0):
    for _ in range(int(timeout / .1)):
        if await b.js(expr):
            return True
        await asyncio.sleep(.1)
    return False


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
def test_a_day_at_the_desk_never_leaves_a_window_behind_or_an_app_pinned_under_the_desktop():
    steps = {}

    async def check(b):
        await _ready(b)
        await b.until("typeof __wm.emit==='function'")
        # 1. Firefox has the keyboard; open a window the desktop draws (Music, Settings, a folder…).
        await _compositor_focus(b, 40)
        await asyncio.sleep(.3)
        await b.js("window.__a=PCOS.openDoc('day-a','Music','i-music',()=>{},true);true")
        await _until_front(b, "40")
        steps['open'] = _front(await b.js('__wm.log'))
        # 2. Back to Firefox: the desktop must give up the front and stop covering Firefox, or Firefox
        #    is pinned UNDER the desktop with nothing on screen explaining why.
        await b.js("__wm.log=[]")
        await _compositor_focus(b, 40)
        ok = await _until(b, "(()=>{const f=__wm.log.filter(x=>'front' in x);const l=f[f.length-1];"
                             "return !!l && !l.front})()")
        steps['back_to_firefox'] = {'released': ok, 'last': _front(await b.js('__wm.log'))}
        # 3. Click INTO the desktop's window (not its taskbar button): it comes forward over Firefox.
        await b.js("__wm.log=[]")
        await b.js("window.addEventListener('pointerdown',()=>{__wm.focus=13;"
                   "setTimeout(()=>__wm.emit({name:'window',change:'focus'}),0)},{capture:true,once:true});true")
        await _press(b, '.osw.osw-document, .osw')
        await _until_front(b, "40")
        steps['click_in'] = _front(await b.js('__wm.log'))
        # 4. A second desktop window, then switch between the two: the front never drops.
        await b.js("__wm.log=[]")
        await b.js("window.__b=PCOS.openDoc('day-b','Settings','i-gear',()=>{},true);true")
        await _until_front(b, "40")
        await b.js("__wm.log=[]")
        await b.js("__a.el.dispatchEvent(new PointerEvent('pointerdown',{bubbles:true,button:0}));true")
        await asyncio.sleep(.8)
        log = await b.js('__wm.log')
        steps['switch'] = {'dropped': any(x.get('front') is False for x in log), 'last': _front(log),
                           'focused_a': await b.js("__a.el.classList.contains('focused')")}
        # 5. Close both: nothing may stay pinned under the desktop.
        await b.js("__wm.log=[]")
        await b.js("PCOS.closeDoc('day-a');PCOS.closeDoc('day-b');true")
        await _until(b, "(()=>{const f=__wm.log.filter(x=>'front' in x);const l=f[f.length-1];"
                        "return !!l && !l.front && !l.covers.length})()")
        steps['closed'] = _front(await b.js('__wm.log'))

    asyncio.run(desktop.with_browser('online', '', check, COMPOSITOR))
    s = steps
    assert s['open'] and s['open']['front'] and 40 in s['open']['covers'], s
    assert s['back_to_firefox']['released'], \
        'Firefox took the keyboard back and the desktop still claimed the front: %r' % s['back_to_firefox']
    assert s['click_in'] and s['click_in']['front'] and 40 in s['click_in']['covers'], s
    assert not s['switch']['dropped'] and s['switch']['focused_a'], \
        'switching between two desktop windows dropped the front: %r' % s['switch']
    assert s['closed'] and not s['closed']['front'] and s['closed']['covers'] == [], \
        'closing every desktop window left applications pinned under the desktop: %r' % s['closed']


SHOT = r'''
/* grim/slurp as the page sees them: the capture takes a moment, and slurp's overlay closing hands
 * the keyboard to the topmost application -- Firefox -- which the compositor then reports. */
window.pcShot = { available: async () => ({ ok: true, region: true }),
  take: async () => { __wm.shots = (__wm.shots || 0) + 1;
    __wm.focus = 40; setTimeout(() => __wm.emit({ name: 'window', change: 'focus' }), 0);
    await new Promise(r => setTimeout(r, 400));
    return { ok: true, path: '/home/u/Pictures/Screenshots/shot.png' }; } };
'''


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
def test_a_screenshot_gives_back_the_window_that_was_in_front():
    """"can't even take a screenshot because firefox then covers music" -- and the other direction:
    a screenshot taken while Firefox was in use must leave Firefox where it was."""
    got = {}

    async def check(b):
        await _ready(b)
        await b.until("typeof __wm.emit==='function' && !!window.PCOSShell && !!PCOSShell.takeShot")
        # Music in front of Firefox.
        await b.js("window.__m=PCOS.openDoc('shot-music','Music','i-music',()=>{},true);true")
        await _until_front(b, "40")
        await b.js("__wm.log=[]")
        await b.js("PCOSShell.takeShot('screen');true")
        await _until(b, "(__wm.shots||0)>=1")
        await asyncio.sleep(1.5)
        log = await b.js('__wm.log')
        got['with_music'] = {'last': _front(log), 'refocused': {'focus': 13} in log}
        # Now Firefox is the thing in use: a second screenshot must not drag Music back over it.
        await b.js("__wm.focus=40;__wm.emit({name:'window',change:'focus'});true")
        await asyncio.sleep(.6)
        await b.js("__wm.log=[]")
        await b.js("PCOSShell.takeShot('screen');true")
        await _until(b, "(__wm.shots||0)>=2")
        await asyncio.sleep(1.5)
        log = await b.js('__wm.log')
        got['with_firefox'] = {'last': _front(log), 'focused_shell': {'focus': 13} in log}

    asyncio.run(desktop.with_browser('online', '', check, COMPOSITOR + SHOT))
    w = got['with_music']
    assert w['last'] and w['last']['front'] and 40 in w['last']['covers'] and w['refocused'], \
        'after the screenshot Firefox covered the window that was in front: %r' % w
    f = got['with_firefox']
    assert not f['focused_shell'] and not (f['last'] and f['last']['front']), \
        'a screenshot of Firefox pulled the desktop in front of it: %r' % f
