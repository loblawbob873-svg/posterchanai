"""Music opens IN FRONT of the windows you have open, however it is opened.

Reported: "Music player opening behind active windows". Two layers, one test each.

1. The windowed desktop (web, a tablet in desktop mode). The launcher and the desktop icon open
   Music through the `__music` EXTRA, which makes a Music window. Every OTHER way in -- `openMusic()`
   (the Android launcher tile), the widget-tap relaunch, `switchView('music')` -- called
   `renderMusicApp` directly, which paints into whichever window holds the one live #feed. Measured
   on master with Notes, Social and Calculator open: no Music window existed at all, the library was
   drawn inside the Calculator frame under the Calculator title, and the thing at the centre of the
   screen was whatever window happened to be on top.

   The assertion is what a person sees: a window titled Music exists, it is the only one, the
   library is inside it, and `elementFromPoint` at its centre lands inside it -- it is the topmost
   thing there -- with three windows open and after another window has been brought in front.

2. PosterChanOS. Music's frame was an in-page window on the desktop surface, which sits under every
   real toplevel (the Go Live / System Settings lesson: the shellFront publish passes against the
   test compositor and not on a real desk). It opens as its own toplevel now, `pcwin=__music`, and
   that window's page paints the library itself.
"""
import asyncio
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop
from tests.client.test_a_focused_desktop_window_comes_in_front_full_app import COMPOSITOR, _ready


@pytest.fixture(scope="module", autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


CHROME = pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")

# Where the Music window is, and what is actually on top at its centre.
FRONT = r"""(()=>{
  const title = w => ((w.querySelector('.osw-title')||{}).textContent||'').trim();
  const all = [...document.querySelectorAll('#os-desk .osw')];
  const music = all.filter(w => title(w) === 'Music');
  const lib = document.getElementById('ma-lib');
  const libIn = lib && lib.closest('.osw') ? title(lib.closest('.osw')) : null;
  if(music.length !== 1) return {count: music.length, titles: all.map(title), libIn};
  const r = music[0].getBoundingClientRect();
  const hit = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2);
  const hitWin = hit && hit.closest('.osw');
  return {count: 1, titles: all.map(title), libIn, focused: music[0].classList.contains('focused'),
          onTop: !!(hitWin && hitWin === music[0]),
          hit: hitWin ? title(hitWin) : (hit ? hit.tagName + '.' + hit.className : null)};
})()"""

OPENERS = {
    # The Android launcher tile (phoneshell.js) and every older native widget call this.
    "openMusic": "__PC.openMusic()",
    # A route that names the view: back/forward, a restored view, a deep link.
    "switchView": "__PC.switchView('music')",
    # The two ways that always worked -- kept so the fix cannot trade them for the others.
    "desktop-icon": "document.querySelector('.os-icon[data-view=\"__music\"]').click()",
    "start-menu": "(document.querySelector('#os-startmenu [data-view=\"__music\"]')"
                  "||(document.querySelector('#os-start').click(),document.querySelector('#os-startmenu [data-view=\"__music\"]'))).click()",
}


async def _open_windows(b):
    await desktop.login(b)
    await b.until("!!document.querySelector('.os-icon[data-view=\"__music\"]')")
    for v in ("notes", "global", "calculator"):
        await b.js(f"__PC.switchView('{v}')")
        await asyncio.sleep(.4)
    titles = await b.js("[...document.querySelectorAll('#os-desk .osw .osw-title')].map(t=>t.textContent.trim())")
    assert {"Notes", "Social", "Calculator"} <= set(titles), titles


async def _until_music_front(b):
    for _ in range(80):
        got = await b.js(FRONT)
        if got.get("count") == 1 and got.get("onTop") and got.get("libIn") == "Music":
            return got
        await asyncio.sleep(.1)
    return got


@CHROME
@pytest.mark.parametrize("opener", sorted(OPENERS))
def test_music_opens_on_top_of_the_windows_you_have_open(opener):
    async def check(b):
        await _open_windows(b)
        await b.js(OPENERS[opener])
        got = await _until_music_front(b)
        assert got.get("count") == 1, f"{opener}: no Music window was opened: {got}"
        assert got.get("libIn") == "Music", f"{opener}: the library was painted into another window: {got}"
        assert got.get("onTop") and got.get("focused"), f"{opener}: Music is behind another window: {got}"

        # Bring another window in front, then ask for Music again: it comes back to the top, and
        # there is still only one of it.
        await b.js("__PC.switchView('notes')")
        await asyncio.sleep(.5)
        assert not (await b.js(FRONT)).get("onTop"), "Notes did not come in front -- the probe proves nothing"
        await b.js(OPENERS[opener])
        got = await _until_music_front(b)
        assert got.get("count") == 1 and got.get("libIn") == "Music" and got.get("onTop"), \
            f"{opener}, second time: {got}"

    asyncio.run(desktop.with_browser("online", "", check))


RECORD = r"""
window.__opened=[];
window.open=function(u,n,f){ __opened.push(String(u)); return {closed:false,focus(){},close(){},addEventListener(){}}; };
"""


@CHROME
@pytest.mark.parametrize("opener", ["desktop-icon", "openMusic"])
def test_posterchanos_opens_music_as_its_own_window(opener):
    async def check(b):
        await _ready(b)
        await b.until("!!document.querySelector('.os-icon[data-view=\"__music\"]')")
        await b.js(OPENERS[opener])
        await b.until("__opened.some(u=>u.includes('pcwin=__music'))")
        await asyncio.sleep(.4)
        assert not await b.js("!!document.getElementById('ma-lib')"), \
            "Music was drawn on the desktop surface, under every open window"

    asyncio.run(desktop.with_browser("online", "", check, COMPOSITOR + RECORD))


@CHROME
def test_the_music_window_paints_the_library_in_its_own_page():
    async def check(b):
        await _ready(b)
        await b.js("location.href=location.pathname+'?pcwin=__music'")
        await b.until("!!document.getElementById('ma-lib')")
        assert await b.js("!document.querySelector('#os-desk .osw')"), "a window opened a desktop window"

    asyncio.run(desktop.with_browser("online", "", check, COMPOSITOR + RECORD))


# The Music window and the desk's Now-playing widget are different renderers on PosterChanOS. The
# other end of the channel is played here by the test itself: a BroadcastChannel in the same page
# hears every other BroadcastChannel object on the origin, exactly as a second window would.
PEER = r"""
window.__peer=[];
window.__peerCh=new BroadcastChannel('pc-music-window');
__peerCh.onmessage=e=>__peer.push(e.data);
"""

ADD_WIDGET = """(()=>{const d=document.querySelector('#os-desk');
  if(!document.querySelector('.os-wgtpick')){
    d.dispatchEvent(new MouseEvent('contextmenu',{bubbles:true,cancelable:true,clientX:300,clientY:300}));
    const row=[...document.querySelectorAll('.os-ctx .os-ctx-b')].find(x=>/add a widget/i.test(x.textContent));
    if(row) row.click();}
  const p=document.querySelector('.os-wgtpick .os-wgt-pick[data-t="music"]'); if(p) p.click();
  return !!document.querySelector('.os-wgt[data-type="music"] [data-m="next"]');})()"""


async def _widget(b):
    for _ in range(30):
        if await b.js(ADD_WIDGET):
            return
        await asyncio.sleep(.3)
    raise AssertionError("could not put a Now-playing widget on the desk")


@CHROME
def test_the_widget_plays_through_the_music_window_not_beside_it():
    async def check(b):
        await _ready(b)
        await b.until("!!document.querySelector('#os-desk')")
        await _widget(b)
        await b.js(PEER)
        # No Music window anywhere: ▶ opens one and leaves THIS page's player alone.
        await b.js("document.querySelector('.os-wgt[data-type=\"music\"] [data-m=\"toggle\"]').click()")
        await b.until("__opened.some(u=>u.includes('pcwin=__music'))")
        assert await b.js("!__PC.music().now()"), "the widget started a second player in the desktop page"
        # …and the press is handed to the window once it says it has loaded.
        await b.js("__peerCh.postMessage({hello:true})")
        await b.until("__peer.some(m=>m.cmd==='toggle')")

        # The window says what it is playing: the widget shows it, and its buttons drive the window.
        await b.js("__peerCh.postMessage({state:{now:{title:'Remote Song',playing:true,t:10,d:100,next:'',pos:1,total:3},shuffling:false}})")
        await b.until("/Remote Song/.test(document.querySelector('.os-wgt[data-type=\"music\"] .wgt-mtitle').textContent)")
        await b.js("__peer.length=0;document.querySelector('.os-wgt[data-type=\"music\"] [data-m=\"next\"]').click()")
        await b.until("__peer.some(m=>m.cmd==='next')")
        assert await b.js("!__PC.music().now()")
        # The window closing takes its state with it.
        await b.js("__peerCh.postMessage({state:{gone:true}})")
        await b.until("/nothing playing/i.test(document.querySelector('.os-wgt[data-type=\"music\"] .wgt-mtitle').textContent)")

    asyncio.run(desktop.with_browser("online", "", check, COMPOSITOR + RECORD))


@CHROME
def test_the_music_window_answers_the_widget():
    async def check(b):
        await _ready(b)
        await b.js("location.href=location.pathname+'?pcwin=__music'")
        await b.until("!!document.getElementById('ma-lib')")
        await b.js(PEER + r"""
          window.__did=[];
          const fake={now:()=>({title:'Here',playing:true,t:1,d:9}),shuffling:()=>false,
            toggle:()=>__did.push('toggle'),next:()=>__did.push('next'),prev:()=>__did.push('prev'),
            shuffle:()=>__did.push('shuffle'),seek:f=>__did.push('seek:'+f)};
          __PC.music=()=>fake;""")
        await b.js("__peerCh.postMessage({cmd:'ask'})")
        await b.until("__peer.some(m=>m.state&&m.state.now&&m.state.now.title==='Here')")
        await b.js("__peerCh.postMessage({cmd:'next'});__peerCh.postMessage({cmd:'seek',arg:0.5});__peerCh.postMessage({cmd:'rm -rf'})")
        await b.until("__did.includes('next') && __did.includes('seek:0.5')")
        assert await b.js("__did.length") == 2, await b.js("__did")

    asyncio.run(desktop.with_browser("online", "", check, COMPOSITOR + RECORD))
