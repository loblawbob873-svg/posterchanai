"""Meme Builder: the buttons that ADD things from elsewhere, PLAY the preview, RECORD a voice-over and place
the MOUTH, each pressed in the real client and checked against what it is supposed to do -- the saved project
(what a reload or an export would use), the request the server would get, or the playback state:

  * ▶ (#mb-play) plays from the playhead and pauses where it is; a play-through to the end rewinds to 0;
  * "Preview clip" (#mb-prev-clip) / "Preview effect" (#mb-prev-fx) play ONLY that layer's slice and stop at its
    end, without rewinding -- not the whole meme;
  * the toolbar Sticker (#mb-add-sticker) adds an emoji as a square overlay in the middle;
  * ＋ Add → From Files (#mbm-blossom) puts the picked drive file on the timeline; → Generate with AI (#mbm-ai)
    sends the typed prompt to /client/meme/generate-image and lands the answer as a layer; → Effect
    (#mba-effect) asks /client/meme/effect for THAT effect and lays it over the selected layer; → Record a
    voice-over (#mba-rec): Start (#mbr-go) records with the preview playing, Stop & add (#mbr-stop) uploads the
    take as an audio layer from 0, Cancel (#mbr-cancel) adds nothing and lets go of the microphone;
  * the project menu's "Open a saved project…" (#mbp-open) lists the projects on the drive and opens one;
  * a drawing's "Draw on it" (#mb-draw-edit) draws into THAT drawing, and the bar's Clear (#mb-dclear) removes
    exactly it (↶ brings it back);
  * Make it talk's mouth picker: Photo (#mm-photo) / Drawing (#mm-draw) pick the renderer, which "Use it"
    remembers; Cancel (#mm-cancel) closes it with nothing spent (no voice studio).

Only the HTTP and relay boundaries are fixtures (test_effects_full_app's INIT, the local media server of
test_meme_drawing_full_app, and the meme endpoints answered below).
"""
import asyncio
import json
from pathlib import Path

import pytest

import tests.client.test_meme_drawing_full_app as draw
from tests.client.test_meme_drawing_full_app import CHROME, _MEDIA, _open

media = draw.media                      # the local media server + renderer fixture

PROJ = "JSON.parse(localStorage.getItem('pc_meme_project')||'{}')"
LAYERS = PROJ + ".layers"

# The meme endpoints this file's buttons call, answered (and recorded in __memeReq) ahead of the fixture.
EXTRA = r'''
(function(){
  window.__memeReq=[];
  const inner=window.fetch;
  const j=d=>Promise.resolve(new Response(JSON.stringify(d),{status:200,headers:{'Content-Type':'application/json'}}));
  window.fetch=function(url,opts){opts=opts||{};const u=String(url);
    if(u.includes('/client/meme/')&&!u.includes('/client/meme/render')){
      let body=null;try{body=JSON.parse(opts.body||'null')}catch(_){}
      __memeReq.push([u.replace(/^.*\/client\/meme\//,'').split('?')[0],body]);
      if(u.includes('/client/meme/effects'))return j({effects:[{name:'dancingman',label:'Dancing man'}]});
      if(u.includes('/client/meme/effect'))return j({url:'MEDIA/photo.png',dur:2});
      if(u.includes('/client/meme/generate-image'))return j({url:'MEDIA/photo.png'});
      if(u.includes('/client/meme/face'))return j({found:false});
    }
    return inner(url,opts);};
})();
'''


@pytest.fixture
def routed(media, monkeypatch):
    orig = draw._route
    monkeypatch.setattr(draw, '_route', lambda base, os_mode: orig(base, os_mode) + EXTRA.replace('MEDIA', base))
    return media


async def _fresh(b, n=1):
    await b.js("PCMeme.reset()")
    for _ in range(n):
        assert await b.js(f"PCMeme.addMedia({json.dumps(_MEDIA[0].base + '/photo.png')},'image/png')")
    await b.until(f"{LAYERS}.length==={n}")


async def _edit(b, js_body):
    """Change the saved project the way a reload would see it, then redraw from it."""
    await b.js("(()=>{const p=JSON.parse(localStorage.getItem('pc_meme_project'));" + js_body
               + ";localStorage.setItem('pc_meme_project',JSON.stringify(p));PCMeme.render();})(); 1")


async def _click(b, i):
    await b.until(f"!!document.getElementById({i!r})")
    await b.js(f"document.getElementById({i!r}).click(); 1")


async def _add(b, item):
    await _click(b, 'mb-add-media')
    await _click(b, item)


async def _select(b, lid):
    await b.until(f"!!document.querySelector('.mb-track[data-id=\"{lid}\"]')")
    await b.js(f"document.querySelector('.mb-track[data-id=\"{lid}\"]').click(); 1")


PLAYING = "document.getElementById('mb-play').textContent.includes('❚❚')"
SCRUB = "+document.getElementById('mb-scrub').value"


@pytest.mark.skipif(not Path(CHROME).exists(), reason='Chrome required')
def test_play_pause_and_the_one_layer_previews(routed):
    async def check(b):
        # Three one-second clips back to back: image 0-1, video 1-2, image 2-3.
        await _fresh(b, 1)
        assert await b.js(f"PCMeme.addMedia({json.dumps(_MEDIA[0].base + '/photo.png')},'video/mp4')")
        assert await b.js(f"PCMeme.addMedia({json.dumps(_MEDIA[0].base + '/photo.png')},'image/png')")
        await _edit(b, "p.layers.forEach((l,i)=>{l.start=i;l.dur=1;})")
        await b.until(f"{LAYERS}.length===3 && {LAYERS}[2].start===2")
        ids = await b.js(f"{LAYERS}.map(l=>l.id)")

        # ▶ plays from the playhead, ❚❚ pauses where it got to (no rewind).
        await _click(b, 'mb-play')
        await b.until(PLAYING)
        await b.until(f"{SCRUB}>=0.5")
        await b.js("document.getElementById('mb-play').click(); 1")
        await b.until("!" + PLAYING)
        paused = await b.js(SCRUB)
        assert 0.4 < paused < 3, paused
        await asyncio.sleep(.4)
        assert await b.js(SCRUB) == paused, "the playhead kept moving after pause"
        # Played through to the end, it stops and rewinds to the start.
        await b.js("document.getElementById('mb-play').click(); 1")
        await b.until(PLAYING)
        await b.until(f"!({PLAYING}) && {SCRUB}===0")

        # "Preview clip" plays only the video's slice: it starts at 1 and stops at its end (2), staying there.
        await _select(b, ids[1])
        await _click(b, 'mb-prev-clip')
        await b.until(PLAYING)
        start = await b.js(SCRUB)
        assert 0.95 <= start < 1.6, start
        await b.until("!" + PLAYING)
        end = await b.js(SCRUB)
        assert 1.8 <= end <= 2.3, f"Preview clip played on to {end}"

        # "Preview effect" on the last image layer plays just 2-3.
        await b.js("document.getElementById('mb-scrub').value=0; 1")
        await _select(b, ids[2])
        await _click(b, 'mb-prev-fx')
        await b.until(PLAYING)
        assert 1.95 <= await b.js(SCRUB) < 2.6
        await b.until("!" + PLAYING)
        assert 2.8 <= await b.js(SCRUB) <= 3.3
        assert not await b.js("window.__errors||[]")
    asyncio.run(_open(1280, 800, False, '', routed, check))


@pytest.mark.skipif(not Path(CHROME).exists(), reason='Chrome required')
def test_sticker_files_ai_and_effect_add_layers(routed):
    async def check(b):
        await _fresh(b, 1)
        photo = _MEDIA[0].base + '/photo.png'

        # Toolbar Sticker: an emoji becomes a square overlay, centred.
        await _click(b, 'mb-add-sticker')
        await b.until("!!document.querySelector('.emoji-pop .ep-grid button[data-e]')")
        await b.js("document.querySelector('.emoji-pop .ep-grid button[data-e]').dispatchEvent(new MouseEvent('mousedown',{bubbles:true,cancelable:true})); 1")
        await b.until(f"{LAYERS}.length===2")
        st = await b.js(f"(()=>{{const p={PROJ};return [p.w,p.h,p.layers.find(l=>l.fit==='contain')]}})()")
        w, h, sticker = st
        assert sticker and sticker['w'] == sticker['h'] == round(min(w, h) / 3), sticker
        assert sticker['x'] == round((w - sticker['w']) / 2) and sticker['y'] == round((h - sticker['h']) / 2), sticker

        # From Files: the drive's listing is shown and the picked file lands on the timeline.
        pk = await b.js("__PC.me().pubkey")
        routed.blobs['/list/' + pk] = (json.dumps([
            {'sha256': 'b' * 64, 'url': routed.base + '/drive-pic.png', 'type': 'image/png', 'size': 99, 'uploaded': 1700000000},
        ]).encode(), 'application/json')
        routed.blobs['/drive-pic.png'] = routed.blobs['/photo.png']
        await _add(b, 'mbm-blossom')
        await b.until("!!document.querySelector('#bp-grid [data-url]')")
        await b.js("document.querySelector('#bp-grid [data-url]').click(); 1")
        await b.until(f"{LAYERS}.length===3")
        assert await b.js(f"{LAYERS}.some(l=>l.type==='image'&&l.src.includes('/drive-pic.png'))")

        # Generate with AI: the prompt typed in the studio is what is sent; the answer is a new layer.
        await _add(b, 'mbm-ai')
        await b.until("!!document.getElementById('gen-prompt')")
        await b.js("(()=>{const t=document.getElementById('gen-prompt');t.value='a cat on a skateboard';"
                   "t.dispatchEvent(new Event('input',{bubbles:true}))})(); 1")
        await b.until("!document.getElementById('gen-go').disabled")
        await b.js("document.getElementById('gen-go').click(); 1")
        await b.until(f"{LAYERS}.length===4")
        req = await b.js("__memeReq.find(r=>r[0]==='generate-image')")
        assert req and req[1]['prompt'] == 'a cat on a skateboard', req
        assert await b.js(f"{LAYERS}.some(l=>l.name==='a cat on a skateboard'&&l.src==={json.dumps(photo)})")

        # Effect: with the first photo selected, the picked effect is requested BY NAME and laid over it.
        base = await b.js(f"{LAYERS}.find(l=>l.type==='image'&&!l.fit)")
        await _select(b, base['id'])
        await _add(b, 'mba-effect')
        await b.until("!!document.querySelector('#modal-root button[data-i]')")
        await b.js("document.querySelector('#modal-root button[data-i]').click(); 1")
        await b.until(f"{LAYERS}.length===5")
        req = await b.js("__memeReq.find(r=>r[0]==='effect')")
        assert req and req[1]['name'] == 'dancingman', req
        ov = await b.js(f"{LAYERS}.find(l=>l.name==='Dancing man')")
        assert ov['type'] == 'video' and [ov['x'], ov['y'], ov['w'], ov['h'], ov['start']] == \
            [base['x'], base['y'], base['w'], base['h'], base['start']], ov
        layers = await b.js(f"{LAYERS}.map(l=>l.id)")
        assert layers.index(ov['id']) == layers.index(base['id']) + 1, "the effect is not just above its layer"
        assert not await b.js("window.__errors||[]")
    asyncio.run(_open(1280, 800, False, '', routed, check))


@pytest.mark.skipif(not Path(CHROME).exists(), reason='Chrome required')
def test_open_a_saved_project(routed):
    async def check(b):
        await _fresh(b, 1)
        pk = await b.js("__PC.me().pubkey")
        saved = await b.js(PROJ)
        saved.update({'pcmeme-project': 1, 'savedAt': 1700000000, 'name': 'the saved build'})
        saved['layers'] = [dict(saved['layers'][0], id='savedA'), dict(saved['layers'][0], id='savedB', start=4)]
        routed.blobs['/proj.json'] = (json.dumps(saved).encode(), 'application/json')
        routed.blobs['/list/' + pk] = (json.dumps([
            {'sha256': 'c' * 64, 'url': routed.base + '/proj.json', 'type': 'application/json', 'size': 500, 'uploaded': 1700000000},
        ]).encode(), 'application/json')

        await _click(b, 'mb-project')
        await _click(b, 'mbp-open')
        await b.until("!!document.querySelector('.mb-projrow')")
        assert 'the saved build' in await b.js("document.querySelector('.mb-projrow').textContent")
        await b.js("document.querySelector('.mb-projrow').click(); 1")
        # It replaces a build with layers in it, so it asks first.
        await b.until("!!document.querySelector('.uiconfirm-btns [data-uc=\"1\"]')")
        await b.js("document.querySelector('.uiconfirm-btns [data-uc=\"1\"]').click(); 1")
        await b.until(f"{PROJ}.name==='the saved build' && {LAYERS}.map(l=>l.id).join()==='savedA,savedB'")
        await _click(b, 'mb-undo')
        await b.until(f"{LAYERS}.length===1 && !{PROJ}.name")
        assert not await b.js("window.__errors||[]")
    asyncio.run(_open(1280, 800, False, '', routed, check))


# A real MediaRecorder over a real (oscillator) audio stream: only the microphone itself is stood in for.
MIC = r'''(()=>{window.__mics=[];navigator.mediaDevices.getUserMedia=async()=>{
  const ac=new AudioContext();const o=ac.createOscillator();const d=ac.createMediaStreamDestination();
  o.connect(d);o.start();__mics.push(d.stream);return d.stream;};})(); 1'''


@pytest.mark.skipif(not Path(CHROME).exists(), reason='Chrome required')
def test_record_a_voice_over_and_cancel_one(routed):
    async def check(b):
        await _fresh(b, 1)
        await b.js(MIC)
        puts = len(routed.blobs)

        await _add(b, 'mba-rec')
        await b.until("!!document.getElementById('mbr-go') && document.getElementById('mbr-stop').disabled")
        await b.js("document.getElementById('mbr-go').click(); 1")
        # Recording: Start is spent, Stop is live, the clock runs and the preview plays along.
        await b.until("document.getElementById('mbr-go').disabled && !document.getElementById('mbr-stop').disabled")
        await b.until(PLAYING)
        await b.until("parseFloat(document.getElementById('mbr-time').textContent)>=0.6")
        await b.js("document.getElementById('mbr-stop').click(); 1")
        await b.until(f"{LAYERS}.some(l=>l.type==='audio')")
        vo = await b.js(f"{LAYERS}.find(l=>l.type==='audio')")
        assert vo['name'] == 'voice-over' and vo['start'] == 0 and 0.5 <= vo['dur'] <= 5 and vo['volume'] == 1, vo
        assert vo['src'].startswith(routed.base), vo
        assert len(routed.blobs) > puts, "the take was not uploaded"
        assert await b.js("__mics[0].getTracks().every(t=>t.readyState==='ended')"), "the microphone was left on"
        assert await b.js("!document.getElementById('mbr-go')")

        # Cancel mid-take: nothing is added, the mic is released and the preview stops.
        n = await b.js(f"{LAYERS}.length")
        await _add(b, 'mba-rec')
        await _click(b, 'mbr-go')
        await b.until("!document.getElementById('mbr-stop').disabled")
        await asyncio.sleep(.4)
        await b.js("document.getElementById('mbr-cancel').click(); 1")
        await b.until("!document.getElementById('mbr-cancel')")
        await asyncio.sleep(.5)
        assert await b.js(f"{LAYERS}.length") == n, "a cancelled take was added"
        assert await b.js("__mics[1].getTracks().every(t=>t.readyState==='ended')"), "cancel left the microphone on"
        assert not await b.js(PLAYING)

        # Cancel before Start: there is no recorder whose stop would release it, so Cancel itself must.
        await _add(b, 'mba-rec')
        await b.until("!!document.getElementById('mbr-cancel') && __mics.length===3")
        await b.js("document.getElementById('mbr-cancel').click(); 1")
        await b.until("!document.getElementById('mbr-cancel')")
        assert await b.js("__mics[2].getTracks().every(t=>t.readyState==='ended')"), "cancel left the microphone on"
        assert await b.js(f"{LAYERS}.length") == n
        assert not await b.js("window.__errors||[]")
    asyncio.run(_open(1280, 800, False, '', routed, check))


@pytest.mark.skipif(not Path(CHROME).exists(), reason='Chrome required')
def test_draw_on_a_chosen_drawing_and_clear_it(routed):
    async def check(b):
        await _fresh(b, 1)
        stroke = "{t:'pen',c:'#ff0000',w:200,p:[1000,1000,5000,5000]}"
        await _edit(b, "const d=(id)=>Object.assign({},p.layers[0],{id,name:'Drawing',src:'',fit:'contain',x:0,y:0,w:p.w,h:p.h,"
                       f"draw:{{strokes:[{stroke}]}}}});p.layers.push(d('drwA'),d('drwB'))")
        await b.until(f"{LAYERS}.length===3")
        # The LOWER drawing, not the topmost one the pen would otherwise pick.
        await _select(b, 'drwA')
        await _click(b, 'mb-draw-edit')
        await b.until("!!document.getElementById('mb-drawbar') && !document.getElementById('mb-dclear').disabled")
        await b.js("document.getElementById('mb-dclear').click(); 1")
        await b.until(f"{LAYERS}.map(l=>l.id).includes('drwB') && !{LAYERS}.map(l=>l.id).includes('drwA')")
        assert await b.js(f"{LAYERS}.length") == 2
        await b.js("document.getElementById('mb-ddone').click(); 1")
        await _click(b, 'mb-undo')
        await b.until(f"{LAYERS}.map(l=>l.id).includes('drwA')")
        assert not await b.js("window.__errors||[]")
    asyncio.run(_open(1280, 800, False, '', routed, check))


@pytest.mark.skipif(not Path(CHROME).exists(), reason='Chrome required')
def test_mouth_picker_photo_drawing_and_cancel(routed):
    async def check(b):
        await _fresh(b, 1)
        lid = await b.js(f"{LAYERS}[0].id")
        await b.js("localStorage.removeItem('pc_meme_talk_mode'); 1")

        async def open_picker():
            await _select(b, lid)
            await _click(b, 'mb-talk')
            await b.until("!!document.getElementById('mm-cancel')")

        studio = "/Voice cloning|vs-go/.test(document.getElementById('modal-root').innerHTML)"
        # Drawing / anime: picks the redraw renderer, and Use it remembers that choice.
        await open_picker()
        await b.js("document.getElementById('mm-draw').click(); 1")
        await b.until("/Flat art/.test(document.getElementById('mm-hint').textContent) && document.getElementById('mm-draw').classList.contains('btn-cyan')")
        assert await b.js("document.getElementById('mm-photo').classList.contains('btn-ghost')")
        await b.js("document.getElementById('mm-go').click(); 1")
        await b.until("localStorage.getItem('pc_meme_talk_mode')==='anime'")
        await b.until(studio)                     # on to the voice studio
        await b.js("__PC.closeModal(); 1")

        # Photo: back to the jaw warp.
        await open_picker()
        await b.js("document.getElementById('mm-draw').click(); document.getElementById('mm-photo').click(); 1")
        await b.until("/Photo:/.test(document.getElementById('mm-hint').textContent) && document.getElementById('mm-photo').classList.contains('btn-cyan')")
        await b.js("document.getElementById('mm-go').click(); 1")
        await b.until("localStorage.getItem('pc_meme_talk_mode')==='photo'")
        await b.until(studio)
        await b.js("__PC.closeModal(); 1")

        # Cancel: the picker closes and nothing goes on to the voice studio.
        await open_picker()
        await b.js("document.getElementById('mm-draw').click(); document.getElementById('mm-cancel').click(); 1")
        await b.until("!document.getElementById('mm-cancel')")
        await asyncio.sleep(.6)
        assert not await b.js(studio), "Cancel went on to the voice studio"
        assert await b.js("localStorage.getItem('pc_meme_talk_mode')") == 'photo', "Cancel saved the choice"
        assert not await b.js("window.__errors||[]")
    asyncio.run(_open(1280, 800, False, '', routed, check))
