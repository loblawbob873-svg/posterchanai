"""PER-APP VOLUME on the PosterChan desktop: each window, Music and "other sounds" get their own level and mute.

Asked 2026-10-28 ("do per-app volume too"). PosterChanOS already mixes per APPLICATION through PipeWire -- where every
PosterChan window is ONE stream -- and a browser or the Windows/macOS app had no mixer at all. The shipped desktop,
real clicks: the tray's speaker opens the mixer, a row per open window says which one is playing, its slider sets
what that window's sound really plays at (read through the browser's OWN volume accessor, saved before the client
loaded -- not what the app reads back), the app's own volume is untouched, mute silences only that row, Music
(the player's detached <audio>) is its own row, and the levels survive a reload.
"""
import asyncio
import base64
import os
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop

CHROME = pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')

NATIVE = r"""window.__nativeVol = Object.getOwnPropertyDescriptor(HTMLMediaElement.prototype, 'volume');
window.__realVol = el => window.__nativeVol.get.call(el);"""
# One second of a 440 Hz tone, as a WAV data URL, so the elements really play.
TONE = r"""(()=>{const sr=8000,n=sr,b=new ArrayBuffer(44+n*2),v=new DataView(b);const w=(o,s)=>{for(let i=0;i<s.length;i++)v.setUint8(o+i,s.charCodeAt(i))};
  w(0,'RIFF');v.setUint32(4,36+n*2,true);w(8,'WAVEfmt ');v.setUint32(16,16,true);v.setUint16(20,1,true);v.setUint16(22,1,true);
  v.setUint32(24,sr,true);v.setUint32(28,sr*2,true);v.setUint16(32,2,true);v.setUint16(34,16,true);w(36,'data');v.setUint32(40,n*2,true);
  for(let i=0;i<n;i++)v.setInt16(44+i*2,Math.sin(i*2*Math.PI*440/sr)*8000,true);
  let s='';const u=new Uint8Array(b);for(let i=0;i<u.length;i++)s+=String.fromCharCode(u[i]);return 'data:audio/wav;base64,'+btoa(s)})()"""


@pytest.fixture(scope='module', autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


async def _click(b, sel):
    x, y = await b.js("(()=>{const r=document.querySelector(%r).getBoundingClientRect();return [r.left+r.width/2,r.top+r.height/2]})()" % sel)
    await b.call('Input.dispatchMouseEvent', {'type': 'mouseMoved', 'x': x, 'y': y})
    for kind in ('mousePressed', 'mouseReleased'):
        await b.call('Input.dispatchMouseEvent', {'type': kind, 'x': x, 'y': y, 'button': 'left', 'clickCount': 1})


async def _open(b, view, title):
    await b.js("PCOS.routeView(%r);true" % view)
    await b.until("[...document.querySelectorAll('#os-root .osw .osw-title')].some(t=>t.textContent===%r)" % title)
    await asyncio.sleep(.3)


async def _plant(b, title, id_):
    """A sound playing INSIDE that window, at the app's own volume 0.8."""
    await b.js("""(async()=>{const w=[...document.querySelectorAll('#os-root .osw')].find(x=>x.querySelector('.osw-title').textContent===%r);
      const a=document.createElement('audio');a.id=%r;a.loop=true;a.src=%s;w.querySelector('.osw-body').appendChild(a);
      a.volume=0.8;await a.play();return true})()""" % (title, id_, TONE))


async def _slider(b, app, pct):
    """Drag the row's slider to `pct` with the mouse, the way a person does."""
    sel = ".pc-mix-row[data-app='%s'] input" % app
    x0, y, w = await b.js("(()=>{const r=document.querySelector(%r).getBoundingClientRect();return [r.left,r.top+r.height/2,r.width]})()" % sel)
    x = x0 + w * pct / 100
    await b.call('Input.dispatchMouseEvent', {'type': 'mouseMoved', 'x': x, 'y': y})
    await b.call('Input.dispatchMouseEvent', {'type': 'mousePressed', 'x': x, 'y': y, 'button': 'left', 'clickCount': 1})
    await b.call('Input.dispatchMouseEvent', {'type': 'mouseReleased', 'x': x, 'y': y, 'button': 'left', 'clickCount': 1})


REAL = "Math.round(__realVol(document.getElementById(%r))*100)/100"


@CHROME
def test_each_window_has_its_own_level_and_mute_and_they_survive_a_reload():
    got = {}

    async def check(b):
        await b.call('Emulation.setDeviceMetricsOverride', {'width': 1440, 'height': 900, 'deviceScaleFactor': 1, 'mobile': False})
        await desktop.login(b)
        await b.until("document.body.classList.contains('os-on')")
        await b.js("try{localStorage.removeItem('pc_app_volume')}catch(_){};true")
        await _open(b, 'notes', 'Notes')
        await _click(b, '#os-root .osw.focused .osw-title')     # a real click: the page may now play sound
        await _plant(b, 'Notes', 'snd-notes')
        await _open(b, 'settings', 'Settings')
        await _plant(b, 'Settings', 'snd-settings')
        # Music: the player's own detached element.
        await b.js("(async()=>{const a=new Audio();a.dataset.pcApp='music';a.id='m';window.__music=a;a.loop=true;a.src=%s;a.volume=0.5;await a.play();return true})()" % TONE)
        await b.until("!!document.querySelector('#os-vol')")
        await _click(b, '#os-vol')
        await b.until("!!document.querySelector('.pc-mixer .pc-mix-row')")
        got['rows'] = await b.js("[...document.querySelectorAll('.pc-mix-row')].map(r=>[r.dataset.app,r.querySelector('.pc-mix-name').textContent.trim(),r.classList.contains('playing')])")
        got['fits'] = await b.js("(()=>{const r=document.querySelector('.pc-mixer').getBoundingClientRect();return r.left>=0&&r.right<=innerWidth&&r.top>=0&&r.bottom<=innerHeight})()")
        if os.environ.get('PC_SHOTS'):
            Path(os.environ['PC_SHOTS']).mkdir(parents=True, exist_ok=True)
            Path(os.environ['PC_SHOTS'], 'app-mixer.png').write_bytes(base64.b64decode((await b.call('Page.captureScreenshot', {'format': 'png'}))['data']))
        await _slider(b, 'notes', 25)
        await asyncio.sleep(.2)
        got['notes_real'] = await b.js(REAL % 'snd-notes')
        got['notes_app'] = await b.js("document.getElementById('snd-notes').volume")
        got['settings_real'] = await b.js(REAL % 'snd-settings')
        await _click(b, ".pc-mix-row[data-app='settings'] .pc-mix-mute")
        await asyncio.sleep(.2)
        got['settings_muted'] = await b.js(REAL % 'snd-settings')
        got['notes_after_mute'] = await b.js(REAL % 'snd-notes')
        got['music_real'] = await b.js("Math.round(__realVol(window.__music)*100)/100")
        # The app changes its own volume: the window's level still applies on top.
        await b.js("document.getElementById('snd-notes').volume=0.4;true")
        got['notes_app_change'] = await b.js(REAL % 'snd-notes')
        await b.call('Page.reload')
        await b.until("!!window.PCOS && document.body.classList.contains('os-on')")
        await _open(b, 'notes', 'Notes')
        await _click(b, '#os-root .osw.focused .osw-title')
        await _plant(b, 'Notes', 'snd-notes2')
        got['after_reload'] = await b.js(REAL % 'snd-notes2')

    asyncio.run(desktop.with_browser('online', '', check, extra_init=NATIVE))
    apps = {r[0]: r for r in got['rows']}
    assert {'notes', 'settings', 'music'} <= set(apps), got['rows']
    assert apps['notes'][2] and apps['settings'][2] and apps['music'][2], ("a playing window was not marked playing", got['rows'])
    assert apps['music'][1].startswith('Music'), got['rows']
    assert got['fits'], 'the mixer runs off the screen'
    assert 0.17 <= got['notes_real'] <= 0.23, ("Notes did not play at 25% of its own 0.8", got)
    assert abs(got['notes_app'] - 0.8) < 1e-6, ("the app no longer reads back its own volume", got)
    assert got['settings_real'] == 0.8, ("changing Notes changed Settings", got)
    assert got['settings_muted'] == 0 and 0.17 <= got['notes_after_mute'] <= 0.23, got
    assert got['music_real'] == 0.5, got
    assert 0.08 <= got['notes_app_change'] <= 0.12, got
    assert 0.17 <= got['after_reload'] <= 0.23, ("the level was forgotten on reload", got)
