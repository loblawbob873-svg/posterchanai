"""The desktop's Now-playing widget PLAYS when you press Play or Shuffle -- it does not open Music.

Reported: "the desktop music widget is now broken! clicking play or shuffle opens up the music app
instead of ....playing or shuffling!"

a5c564702 made Music a real toplevel on PosterChanOS and gave the widget a stand-in player for "no
Music window is open": every button stored the press, opened the Music window (which the compositor
maps in front and focuses) and handed the press over when that window said hello. Measured in the
shipped client, the hand-over cannot work: the Music page posts its hello while its document is
still LOADING (`readyState=loading`, nobody signed in yet), the opener answers at once, and the
press lands on a player whose library is not loaded -- `toggle()` pulls an index that has no account
to pull for, finds nothing and plays nothing. So the person got exactly what they reported: the
Music app in their face and silence. (Prev/next were also rewritten to "toggle" on the way.)

The widget plays where it is now, as it did before. The guarantees a5c564702 was for still hold:
a Music window that holds a track is driven by the widget (one player, not two), and whichever page
starts sounding tells the others to stop (`exclusive` on the channel), so the desk and a Music
window never both play.

Driven through the real bundled client with REAL mouse presses (Input.dispatchMouseEvent, which is
what grants the user activation an <audio> needs to start) against a stub five-track library whose
bytes are a real WAV, in both desktop shapes: the windowed desktop on the web, and PosterChanOS with
the compositor bridge stubbed the way `PCOSWin.enabled()` expects (pcWM + PCOSShell.available()).
"""
import asyncio
import json
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop
from tests.client.test_a_focused_desktop_window_comes_in_front_full_app import COMPOSITOR, _ready
from tests.client.test_music_opens_in_front_full_app import RECORD, _widget


@pytest.fixture(scope="module", autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


CHROME = pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")

# A five-track library in the Music folder. The bytes come through the shared-track hook
# (`m.shared` -> PCMusicShare.plain), which is the one seam where a test can hand the player real
# audio without an encrypted blob server; everything after it -- the queue, play(), the <audio>
# element, the widget -- is the shipped code.
LIB = r"""
(()=>{
  const wav=(()=>{const sr=8000,n=sr*30,b=new ArrayBuffer(44+n*2),v=new DataView(b),w=(o,s)=>{for(let i=0;i<s.length;i++)v.setUint8(o+i,s.charCodeAt(i))};
    w(0,'RIFF');v.setUint32(4,36+n*2,true);w(8,'WAVE');w(12,'fmt ');v.setUint32(16,16,true);v.setUint16(20,1,true);v.setUint16(22,1,true);
    v.setUint32(24,sr,true);v.setUint32(28,sr*2,true);v.setUint16(32,2,true);v.setUint16(34,16,true);w(36,'data');v.setUint32(40,n*2,true);
    for(let i=0;i<n;i++)v.setInt16(44+i*2,Math.round(Math.sin(i/8)*300),true);return new Uint8Array(b);})();
  window.__stubPlays=[];
  const plain=async(sha)=>{__stubPlays.push(sha);return wav;};
  let real=null;
  Object.defineProperty(window,'PCMusicShare',{configurable:true,
    get(){ if(real){ real.plain=plain; return real; } return {plain, meta:()=>null}; },
    set(v){ real=v; }});
  window.__seedLib=()=>{
    const F=__PC.filesIdx(); F._norm();
    for(let i=1;i<=5;i++) F.data.files[String(i).repeat(64)]={folder:'Music',enc:true,shared:true,name:'Stub Song '+i,mime:'audio/wav',ts:1000+i};
    F._pullDone=true; F._pullOk=true; return true;
  };
})();
"""

WGT = '.os-wgt[data-type="music"]'

STATE = """(()=>{const P=__PC.music();const n=P.now();
  const title=w=>((w.querySelector('.osw-title')||{}).textContent||'').trim();
  return {now:n, shuffling:P.shuffling(), plays:__stubPlays.length,
    opened:(window.__opened||[]).filter(u=>u.includes('pcwin=')),
    musicWindows:[...document.querySelectorAll('#os-desk .osw')].filter(w=>title(w)==='Music').length,
    library:!!document.getElementById('ma-lib'),
    title:(document.querySelector('%s .wgt-mtitle')||{}).textContent,
    icon:((document.querySelector('%s [data-m=toggle] use')||{getAttribute:()=>''}).getAttribute('href'))}})()""" % (WGT, WGT)


async def press(b, which):
    """A real mouse press on the widget's button -- the only kind that may start audio."""
    sel = f'{WGT} [data-m="{which}"]'
    xy = await b.js(f"(()=>{{const e=document.querySelector({json.dumps(sel)});const r=e.getBoundingClientRect();"
                    "return [r.left+r.width/2,r.top+r.height/2]})()")
    for t in ("mousePressed", "mouseReleased"):
        await b.call("Input.dispatchMouseEvent", {"type": t, "x": xy[0], "y": xy[1], "button": "left", "clickCount": 1})


async def until_state(b, ok, timeout=12.0):
    got = None
    for _ in range(int(timeout / .2)):
        got = await b.js(STATE)
        if ok(got):
            return got
        await asyncio.sleep(.2)
    return got


async def _desk(b, path):
    if path == "web":
        await desktop.login(b)
        await b.until("!!document.querySelector('#os-desk')")
        assert not await b.js("!!(window.PCOSWin && PCOSWin.enabled())")
    else:
        await _ready(b)
        await b.until("!!document.querySelector('#os-desk')")
        # The precondition of the reported bug: on this desk Music opens as its own toplevel.
        assert await b.js("PCOSWin.enabled()"), "the compositor stub did not make toplevels available"
    await _widget(b)
    assert await b.js("__seedLib()")
    await b.js("window.__opened=window.__opened||[];__opened.length=0")


def _init(path):
    return LIB + ("" if path == "web" else COMPOSITOR + RECORD)


@CHROME
@pytest.mark.parametrize("path", ["web", "posterchanos"])
@pytest.mark.parametrize("button", ["toggle", "shuffle"])
def test_play_and_shuffle_on_the_widget_play_and_do_not_open_music(path, button):
    async def check(b):
        await _desk(b, path)
        await press(b, button)
        got = await until_state(b, lambda s: bool(s["now"] and s["now"].get("playing")))
        assert got["now"] and got["now"].get("playing"), f"{path}/{button}: nothing is playing: {got}"
        assert got["plays"] >= 1, f"{path}/{button}: no track was ever loaded: {got}"
        assert not got["opened"], f"{path}/{button}: the press opened a Music window instead of playing: {got}"
        assert got["musicWindows"] == 0 and not got["library"], \
            f"{path}/{button}: the press brought the Music app up: {got}"
        if button == "shuffle":
            assert got["shuffling"], f"{path}: Shuffle played without shuffling: {got}"
        # The widget says what is playing, and its main button became Pause.
        got = await until_state(b, lambda s: "Stub Song" in (s["title"] or "") and s["icon"] == "#i-pause")
        assert "Stub Song" in (got["title"] or ""), f"{path}/{button}: the widget does not show the track: {got}"
        assert got["icon"] == "#i-pause", f"{path}/{button}: the widget's main button is not Pause: {got}"
        # …and Pause on the widget pauses it.
        await press(b, "toggle")
        got = await until_state(b, lambda s: bool(s["now"]) and not s["now"].get("playing"))
        assert got["now"] and not got["now"].get("playing"), f"{path}/{button}: Pause did not pause: {got}"
        assert not got["opened"] and got["musicWindows"] == 0

    asyncio.run(desktop.with_browser("online", "", check, _init(path)))


# The other end of the channel, played by the test: a BroadcastChannel in the same page hears every
# other BroadcastChannel object on the origin, exactly as the Music toplevel would.
PEER = r"""
window.__peer=[];
window.__peerCh=new BroadcastChannel('pc-music-window');
__peerCh.onmessage=e=>__peer.push(e.data);
"""


@CHROME
def test_a_music_window_that_holds_a_track_is_driven_not_doubled():
    """The guarantee a5c564702 was for: with a Music window holding a track, the widget is ITS
    remote -- it shows that track, and its buttons (prev/next as prev/next) go to that window
    instead of starting a second player on the desk."""
    async def check(b):
        await _desk(b, "posterchanos")
        await b.js(PEER)
        await b.js("__peerCh.postMessage({state:{now:{title:'Remote Song',playing:true,t:10,d:100,next:'',pos:1,total:3},shuffling:false}})")
        await b.until(f"/Remote Song/.test(document.querySelector('{WGT} .wgt-mtitle').textContent)")
        for which in ("toggle", "next", "prev", "shuffle"):
            await b.js("__peer.length=0")
            await press(b, which)
            await b.until(f"__peer.some(m=>m.cmd==='{which}')")
        got = await b.js(STATE)
        assert not got["now"], f"the widget started a second player beside the Music window: {got}"
        assert not got["opened"]

    asyncio.run(desktop.with_browser("online", "", check, _init("posterchanos") + PEER))


@CHROME
def test_the_desk_and_a_music_window_never_both_sound():
    async def check(b):
        await _desk(b, "posterchanos")
        await b.js(PEER)
        await press(b, "toggle")
        got = await until_state(b, lambda s: bool(s["now"] and s["now"].get("playing")))
        assert got["now"] and got["now"].get("playing"), got
        # Starting to sound says so, so a Music window that is playing stops.
        await b.until("__peer.some(m=>m&&m.exclusive)")
        # A Music window that starts playing says the same -- and the desk stops.
        await b.js("__peerCh.postMessage({exclusive:'music-window'});"
                   "__peerCh.postMessage({state:{now:{title:'Remote Song',playing:true,t:1,d:100,next:'',pos:1,total:3},shuffling:false}})")
        got = await until_state(b, lambda s: bool(s["now"]) and not s["now"].get("playing")
                                and "Remote Song" in (s["title"] or ""))
        assert got["now"] and not got["now"].get("playing"), f"the desk kept playing under the Music window: {got}"
        # The widget follows what is audible: the Music window's track.
        assert "Remote Song" in (got["title"] or ""), got

    asyncio.run(desktop.with_browser("online", "", check, _init("posterchanos") + PEER))


@CHROME
def test_the_music_window_says_when_it_starts_sounding():
    async def check(b):
        await _ready(b)
        await b.js("location.href=location.pathname+'?pcwin=__music'")
        await b.until("!!document.getElementById('ma-lib')")
        await b.js(PEER + r"""
          window.__playing=false;
          const fake={now:()=>({title:'Here',playing:__playing,t:1,d:9}),shuffling:()=>false,pause:()=>{__playing=false},
            toggle:()=>{},next:()=>{},prev:()=>{},shuffle:()=>{},seek:()=>{}};
          __PC.music=()=>fake;""")
        await b.js("__playing=true; PCOS.musicChanged()")
        await b.until("__peer.some(m=>m&&m.exclusive)")
        # And it obeys the desk's.
        await b.js("__peerCh.postMessage({exclusive:'desk'})")
        await b.until("__playing===false")

    asyncio.run(desktop.with_browser("online", "", check, COMPOSITOR + RECORD))
