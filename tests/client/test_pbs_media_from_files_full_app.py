"""PBS ADDS MEDIA FROM FILES: AN IMAGE OR A VIDEO, FROM THIS DEVICE OR FROM THE APP'S OWN FILES PICKER.

"I hope pbs supports adding media from Files" (2026-10-30). It did not: "Image" opened the operating system's chooser
and took images only. Now "Media" asks This device / Files:

  1. Files opens the app's REAL file picker (the one every other screen uses) titled "Add media from Files".
  2. A video and an image picked there -- both from an ENCRYPTED folder, so they arrive as `enc:<sha>` and are
     decrypted on this device -- are composited onto the broadcast canvas (pixels read back), the video's SOUND gets
     its own strip in the mixer and its meter moves.

The picker's PICK is the one stub (the drive listing and decryption are other suites' business): `blossomPicker` hands
PBS what the real one hands every caller, and `encFileUrl` answers with bytes made in the page -- a 2-second
magenta WebM with a 440 Hz tone, and a yellow PNG.
"""
import asyncio
from pathlib import Path

import pytest

from tests.client import pbs_fixture as F
from tests.client import test_desktop_offline_full_app as desktop


@pytest.fixture(scope='module', autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


SHA_V, SHA_I = 'a' * 64, 'b' * 64

# Wrap PBS's factory before pbs.js loads, so its deps carry the stub pick + decryption; the REAL picker stays
# reachable as __realPicker for part 1.
TRAP = r'''(()=>{let real;Object.defineProperty(window,'PCPbsFactory',{configurable:true,get(){return real;},set(f){
  real=deps=>{window.__realPicker=deps.blossomPicker;
    deps.blossomPicker=(ta,onPick,opts)=>{window.__pickerOpts=opts;(window.__pickQueue||[]).splice(0).forEach(x=>onPick(x));};
    deps.encFileUrl=async sha=>window.__decrypted&&window.__decrypted[sha]||'';
    return f(deps);};}});})();'''

MAKE = r'''(async()=>{
  const c=document.createElement('canvas');c.width=320;c.height=180;const g=c.getContext('2d');
  const ac=new AudioContext(),o=ac.createOscillator(),d=ac.createMediaStreamDestination();o.frequency.value=440;o.connect(d);o.start();
  const st=new MediaStream([...c.captureStream(30).getVideoTracks(),...d.stream.getAudioTracks()]);
  const iv=setInterval(()=>{g.fillStyle='#ff00ff';g.fillRect(0,0,320,180);},33);
  const rec=new MediaRecorder(st,{mimeType:'video/webm'}),parts=[];rec.ondataavailable=e=>parts.push(e.data);
  await new Promise(r=>{rec.onstop=r;rec.start();setTimeout(()=>rec.stop(),2000);});clearInterval(iv);o.stop();
  const vid=URL.createObjectURL(new Blob(parts,{type:'video/webm'}));
  const p=document.createElement('canvas');p.width=64;p.height=64;const pg=p.getContext('2d');pg.fillStyle='#ffff00';pg.fillRect(0,0,64,64);
  const img=URL.createObjectURL(await new Promise(r=>p.toBlob(r,'image/png')));
  window.__decrypted={'%s':vid,'%s':img};return true;})()''' % (SHA_V, SHA_I)


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
def test_media_from_files_adds_an_image_and_a_video_with_its_sound():
    got = {}

    async def check(b):
        await F.open_pbs(b, 1280)
        got['label'] = await b.js("document.querySelector('#pbs-add-image').textContent.trim()")
        # 1. the chooser, and the REAL picker behind "Files"
        await F.click(b, '#pbs-add-image')
        await b.until("!!document.querySelector('#pbs-media-files')")
        got['choices'] = await b.js("[...document.querySelectorAll('.pbs-src-choices button')].map(x=>x.textContent.trim())")
        await b.js("document.querySelector('#pbs-media-files').click();true")
        await asyncio.sleep(.5)
        # "This device" hands over to the system file chooser for images AND videos
        await b.js("window.__chooser=[];const c=HTMLInputElement.prototype.click;HTMLInputElement.prototype.click=function(){if(this.type==='file'){__chooser.push(this.accept);return;}return c.call(this);};true")
        await F.click(b, '#pbs-add-image')
        await b.until("!!document.querySelector('#pbs-media-device')")
        await F.click(b, '#pbs-media-device')
        await asyncio.sleep(.3)
        got['device'] = await b.js("({accept:__chooser.slice(-1)[0]||'',modal:!!document.querySelector('#pbs-media-device')})")
        got['filter'] = await b.js("!!(window.__pickerOpts&&window.__pickerOpts.filter&&__pickerOpts.filter({type:'video/mp4'})&&__pickerOpts.filter({type:'image/png'})&&!__pickerOpts.filter({type:'audio/mpeg',name:'a.mp3'}))")
        await b.js("__realPicker(null,()=>{}, {title:'Add media from Files'});true")
        await b.until("!!document.querySelector('.bp-file-picker')")
        got['real_title'] = await b.js("document.querySelector('.bp-file-picker h3').textContent")
        await b.js("document.querySelector('.bp-file-picker .bp-close').click();true")
        # 2. pick a video and an image from an encrypted folder
        await b.js(MAKE)
        await b.js("window.__pickQueue=[{url:'https://fixture.invalid/blossom/%s',type:'video/webm',ext:'webm',name:'Intro clip',sha:'%s',enc:true},"
                   "{url:'https://fixture.invalid/blossom/%s',type:'image/png',ext:'png',name:'Logo',sha:'%s',enc:true}];true" % (SHA_V, SHA_V, SHA_I, SHA_I))
        await F.click(b, '#pbs-add-image')
        await b.until("!!document.querySelector('#pbs-media-files')")
        await b.js("document.querySelector('#pbs-media-files').click();true")
        await b.until("document.querySelectorAll('.pbs-layer').length===2")
        await asyncio.sleep(2.5)
        got['layers'] = await b.js("[...document.querySelectorAll('.pbs-layer-n')].map(n=>n.textContent)")
        got['magenta'] = await F.count(b, (255, 0, 255))
        got['yellow'] = await F.count(b, (255, 255, 0))
        got['strips'] = await b.js("[...document.querySelectorAll('.pbs-strip')].map(s=>s.querySelector('b').textContent)")
        # Properties: "Play from the start" rewinds the video; "Center" centres the image
        await b.js("[...document.querySelectorAll('.pbs-layer')].find(l=>l.textContent.includes('Intro clip')).querySelector('.pbs-layer-main').click();true")
        await asyncio.sleep(1.2)
        got['t_before'] = await b.js("[...document.querySelectorAll('#pbs-media-host video')].map(v=>v.currentTime).find(t=>t>0)||0")
        await F.click(b, '#pbs-p-restart')
        got['t_after'] = await b.js("Math.min(...[...document.querySelectorAll('#pbs-media-host video')].filter(v=>v.src).map(v=>v.currentTime))")
        await b.js("[...document.querySelectorAll('.pbs-layer')].find(l=>l.textContent.includes('Logo')).querySelector('.pbs-layer-main').click();true")
        await asyncio.sleep(.2)
        await F.click(b, '#pbs-p-center')
        await asyncio.sleep(.2)
        got['centered'] = await b.js("(()=>{const s=document.querySelector('#pbs-sel').style;const l=parseFloat(s.left),w=parseFloat(s.width),t=parseFloat(s.top),h=parseFloat(s.height);return Math.abs(l+w/2-50)<1&&Math.abs(t+h/2-50)<1})()")
        got['vid_db'] = await b.js("(()=>{const s=[...document.querySelectorAll('.pbs-strip')].find(x=>x.dataset.ch.startsWith('v:'));return s?Number(s.querySelector('.pbs-meter').dataset.peakDb):-99})()")
        got['errors'] = await b.js('__errors')
    asyncio.run(desktop.with_browser('online', '', check, F.init() + TRAP))
    assert got['errors'] == [], got['errors']
    assert got['label'] == 'Media', got['label']
    assert any('This device' in c for c in got['choices']) and any('Files' in c for c in got['choices']), got['choices']
    assert got['filter'], 'the Files picker must offer images and videos (not audio)'
    assert got['device'] == {'accept': 'image/*,video/*', 'modal': False}, ('This device must open the chooser for images and videos', got['device'])
    assert got['t_before'] > 0.5 and got['t_after'] < 0.3, ('Play from the start did not rewind the video', got['t_before'], got['t_after'])
    assert got['centered'], 'Center did not centre the image'
    assert got['real_title'] == 'Add media from Files', got['real_title']
    assert sorted(got['layers']) == ['Intro clip', 'Logo'], got['layers']
    assert got['magenta']['n'] > 500, ('the video is not on the broadcast canvas', got['magenta'])
    assert got['yellow']['n'] > 300, ('the image is not on the broadcast canvas', got['yellow'])
    assert 'Intro clip' in got['strips'], ('the video has no strip in the mixer', got['strips'])
    assert got['vid_db'] > -40, ('the video\'s sound is not reaching the mix', got['vid_db'])
