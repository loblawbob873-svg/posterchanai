"""PBS's MIXER: the device you pick is the device it opens, the meters move with the sound, and a fader or a mute
is what the viewers hear -- the meters read AFTER the gain.

Run: venv-unified/bin/python -m pytest tests/client/test_pbs_audio_full_app.py

The microphone is a 440 Hz OscillatorNode at half scale (about -6 dBFS peak) and the screen's own sound a 330 Hz
one (tests/client/pbs_fixture.py). The meter's numbers are read off the element the person looks at
(`data-peak-db`), and its drawn length off its CSS variable.

  mic selection   "Use microphone" asks getUserMedia for audio; picking "Fixture Mic B" re-opens THAT device
                  (deviceId exact), not the default
  level meters    a tone reads about -6 dB peak and fills most of the bar; silence reads the floor
  per-source vol  the microphone's fader at 0 drops ITS meter to the floor while desktop audio keeps playing
  mute            Mute drops the meter to the floor and says so (aria-pressed); Unmute brings it back
  desktop audio   a screen shared with sound gets its own strip, independent of the microphone
"""
import asyncio
from pathlib import Path

import pytest

from tests.client import pbs_fixture as F
from tests.client import test_desktop_offline_full_app as desktop


@pytest.fixture(scope='module', autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


LEVEL = """(k=>{const m=document.querySelector('.pbs-strip[data-ch="'+k+'"] .pbs-meter');if(!m)return null;
  return {peak:Number(m.dataset.peakDb),rms:Number(m.dataset.rmsDb),bar:parseFloat(getComputedStyle(m).getPropertyValue('--rms'))||0,
          muted:(m.closest('.pbs-strip').querySelector('.pbs-mute')||{}).getAttribute?.('aria-pressed')}})"""


async def level(b, k):
    await asyncio.sleep(.5)
    return await b.js('%s(%r)' % (LEVEL, k))


async def _range(b, sel, v):
    await b.js("(()=>{const e=document.querySelector(%r);e.value=%r;e.dispatchEvent(new Event('input',{bubbles:true}));return true})()" % (sel, str(v)))


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
def test_the_mixer_opens_the_picked_mic_meters_it_and_obeys_fader_and_mute():
    got = {}

    async def check(b):
        await F.open_pbs(b, 1280)
        got['idle'] = await level(b, 'mic')
        await F.click(b, '#pbs-mic-on')
        await b.until("!document.querySelector('#pbs-mic-on')")
        got['loud'] = await level(b, 'mic')
        got['devices'] = await b.js("[...document.querySelectorAll('#pbs-mic-dev option')].map(o=>o.textContent)")
        await b.js("(()=>{const s=document.querySelector('#pbs-mic-dev');s.value='mic-b';s.dispatchEvent(new Event('change',{bubbles:true}));return true})()")
        await asyncio.sleep(.6)
        got['gum'] = await b.js('__gum')
        got['picked'] = await b.js("document.querySelector('#pbs-mic-dev').value")
        # desktop audio arrives with the screen
        await F.click(b, '#pbs-add-screen')
        await b.until("!!document.querySelector('.pbs-strip[data-ch=desk]:not(.idle)')")
        got['desk'] = await level(b, 'desk')
        # per-source volume: the mic's fader to 0, desktop untouched
        await _range(b, '.pbs-strip[data-ch=mic] .pbs-vol', 0)
        got['mic_fader0'] = await level(b, 'mic')
        got['desk_while_mic0'] = await level(b, 'desk')
        await _range(b, '.pbs-strip[data-ch=mic] .pbs-vol', 100)
        got['mic_fader100'] = await level(b, 'mic')
        # mute
        await F.click(b, '.pbs-strip[data-ch=mic] .pbs-mute')
        got['muted'] = await level(b, 'mic')
        await F.click(b, '.pbs-strip[data-ch=mic] .pbs-mute')
        got['unmuted'] = await level(b, 'mic')
        got['errors'] = await b.js('__errors')
        await F.shot(b, 'mixer-1280.png')
    asyncio.run(desktop.with_browser('online', '', check, F.init()))
    assert got['errors'] == [], got['errors']
    assert got['idle']['peak'] <= -59, ('a strip with no input must read silence', got['idle'])
    assert got['loud']['peak'] >= -12 and got['loud']['bar'] > 60, ('the meter does not move with the microphone', got['loud'])
    assert 'Fixture Mic B' in got['devices'], got['devices']
    audio = [g['audio'] for g in got['gum'] if g.get('audio')]
    assert len(audio) >= 2 and audio[-1].get('deviceId') == {'exact': 'mic-b'}, ('picking a microphone did not open it', audio)
    assert got['picked'] == 'mic-b'
    assert got['desk']['peak'] >= -12, ('desktop audio from the screen is not metered', got['desk'])
    assert got['mic_fader0']['peak'] <= -59, ('the fader at 0 still reaches the mix', got['mic_fader0'])
    assert got['desk_while_mic0']['peak'] >= -12, ('one fader moved another source', got['desk_while_mic0'])
    assert got['mic_fader100']['peak'] >= -12, got['mic_fader100']
    assert got['muted']['peak'] <= -59 and got['muted']['muted'] == 'true', ('Mute did not silence the meter', got['muted'])
    assert got['unmuted']['peak'] >= -12 and got['unmuted']['muted'] == 'false', got['unmuted']
