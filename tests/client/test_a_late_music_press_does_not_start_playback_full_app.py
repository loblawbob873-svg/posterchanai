"""A media press that reaches the page late does not start the music when the app is next opened.

Reported 2026-10-29 from the APK: "opening Files started playing music" (and on 10-28 "opening posterchan apps on
android randomly starts music"). Nothing on the page plays by itself -- every play() is a tap or a `musicTransport`
press from the native MusicService. The press that does it is one made while the app was in the BACKGROUND: a
Bluetooth headset or car sending PLAY as it connects, a headset button, the standby session. MusicService's cold
path hands it straight to the page (`if (listener != null) emit(want, 0)` -- no receipt check, no wake-up), and a
backgrounded app's WebView renderer is frozen by Android, so the event waits in it until the renderer thaws -- which
is the moment the person next opens PosterChan, on whatever screen they asked for. The 10-28 fix dropped the PARKED
copy of such a press (LaunchPress.abandonRefused); the copy queued in the page was still performed.

MusicPlugin now stamps every press with when it was made (`at`), and the page performs a press that makes sound
(play / next / prev) only while it is fresh. This drives the SHIPPED client at a phone's size with a stand-in
MusicControls plugin: Files is opened, a play press made five minutes ago arrives (the thaw), and nothing plays --
no play() call, no track loaded, still on Files -- while the service is still answered (ack) and told why
(playBlocked). A press made now plays the last track, so the check can see playback when it happens.
"""
import asyncio
import json
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop
from tests.client import music_library_fixture as L


@pytest.fixture(scope="module", autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


NATIVE = r'''
window.__acks=0; window.__blocked=[]; window.__plays=0;
{const p=HTMLMediaElement.prototype.play;HTMLMediaElement.prototype.play=function(){window.__plays++;return p.apply(this,arguments);};}
window.Capacitor={isNativePlatform:()=>true,getPlatform:()=>'android',Plugins:{
 HomeScreen:{consumeLaunchView:async()=>({view:''}), addListener:()=>({remove(){}}), formFactor:async()=>({form:'phone'}),
   setSystemBarsHidden:async o=>o, systemBars:async()=>({hidden:false})},
 MusicControls:{addListener:(n,cb)=>{ if(n==='musicTransport') window.__transport=cb; return {remove(){}}; },
   setOptions:async()=>({}), ack:async()=>{ window.__acks++; return {}; }, update:async()=>({}), stop:async()=>({}),
   consumeLaunchAction:async()=>({action:''}), playBlocked:async o=>{ window.__blocked.push(o.reason); return {}; }}
}};'''

PLAYING = ("({plays:__plays, cur:__PC.MusicPlayer.cur||null,"
           " sounding:[...document.querySelectorAll('audio,video')].filter(a=>!a.paused).length"
           "+((__PC.MusicPlayer.cur&&window.__aud&&!__aud.paused)?1:0),"
           " view:(document.querySelector('#view-title')||{}).textContent||''})")
CATCH = "{const A=window.Audio;window.Audio=function(){const e=new A(...arguments);window.__aud=e;return e;};}"


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
def test_a_press_made_while_the_app_slept_does_not_play_when_files_is_opened():
    got = {}
    last = json.dumps({"sha": L.SHAS["a"], "pos": 0, "queue": [L.SHAS["a"], L.SHAS["b"]]})

    async def check(b):
        await b.call("Emulation.setDeviceMetricsOverride", {"width": 412, "height": 900, "deviceScaleFactor": 2, "mobile": True})
        await desktop.login(b)
        await b.until("!!window.__PC_BOOTED && !!window.__transport")
        await b.js("try{ if(window.PCOS && PCOS.isOn()) PCOS.exit(); }catch(_){}; window.__publishOK=true; __seedLib();"
                   f"localStorage.setItem('pc_music_last', {json.dumps(last)}); 1")
        # The person opens Files...
        await b.js("__PC.switchView('blossom'); 1")
        await b.until("/File Manager/.test(document.querySelector('#view-title').textContent)")
        await asyncio.sleep(1)
        got["before"] = await b.js(PLAYING)
        acks = await b.js("__acks")
        # ...and the renderer, thawing, delivers the PLAY a headset sent five minutes ago.
        await b.js("__transport({action:'play', value:0, at:Date.now()-5*60*1000}); 1")
        await asyncio.sleep(3)
        got["late"] = await b.js(PLAYING)
        got["late_acked"] = (await b.js("__acks")) > acks
        got["blocked"] = await b.js("__blocked.slice()")
        # A press made NOW is performed: the last track starts (the check can see playback when it happens).
        await b.js("__transport({action:'play', value:0, at:Date.now()}); 1")
        await b.until("__plays>0 && !!__PC.MusicPlayer.cur")
        got["fresh"] = await b.js(PLAYING)
        got["errors"] = await b.js("(window.__errors||[]).slice(-5)")

    asyncio.run(desktop.with_browser("online", "", check, L.PLAIN + L.LIB + NATIVE + CATCH))
    assert got["before"]["plays"] == 0 and got["before"]["cur"] is None, got
    late = got["late"]
    assert late["plays"] == 0 and late["cur"] is None and late["sounding"] == 0, (
        "a press made while the app was asleep started the music when Files was opened", got)
    assert "File Manager" in late["view"], got
    assert got["late_acked"], ("the service must still hear that the page is alive", got)
    assert any("late" in r or "after it was made" in r for r in got["blocked"]), got
    assert got["fresh"]["cur"] == L.SHAS["a"], got
