"""SCREEN RECORDER: record, see it, trim it on this node, keep it in Files -- the shipped client, real clicks.

Asked 2026-10-28 ("Screen Recorder and clip"). Headless Chrome has no screen to pick, so getDisplayMedia hands back
a CANVAS stream (an animated picture) and the microphone an oscillator -- everything after that is real: the
browser's own MediaRecorder, the review player, the trim handles, and the trim itself, which goes through the
SHIPPED /client/media/clip-video router (forwarded from the test instance) to real ffmpeg. Checked by what a person
gets: a recording that plays, a trim of the asked-for length (ffprobe), and a file in Files -> Recordings.
"""
import asyncio
import json
import subprocess
import tempfile
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.routers import client as C
from tests.client import realnode_harness as R
from tests.client import wallpaper_harness as H

CHROME = pytest.mark.skipif(not R.chrome_ok(), reason='Chrome required')
ME = 1

FAKE_SCREEN = r'''
(()=>{
  const md = navigator.mediaDevices || (navigator.mediaDevices = {});
  window.__dispCalls = 0;
  md.getDisplayMedia = async (c) => {
    window.__dispCalls++;
    const cv = document.createElement('canvas'); cv.width = 640; cv.height = 360;
    const g = cv.getContext('2d'); let n = 0;
    setInterval(() => { g.fillStyle = 'hsl(' + (n++ * 7 % 360) + ',80%,50%)'; g.fillRect(0, 0, 640, 360);
                        g.fillStyle = '#fff'; g.font = '48px sans-serif'; g.fillText('frame ' + n, 40, 200); }, 33);
    return cv.captureStream(30);
  };
  md.getUserMedia = async () => {
    const ac = new AudioContext(); const o = ac.createOscillator(); const d = ac.createMediaStreamDestination();
    o.connect(d); o.start(); return d.stream;
  };
})();
'''
TRIMS = []


def _forward_clip(handler, method, path, body):
    """Hand /client/media/clip-video to the SHIPPED router (real ffmpeg), as nginx -> 3051 would."""
    if method != 'POST' or path.split('?')[0] != '/client/media/clip-video':
        return False
    app = FastAPI()
    app.include_router(C.router)
    with TestClient(app) as tc:
        r = tc.post('/client/media/clip-video', content=body, headers={'content-type': handler.headers.get('Content-Type')})
    TRIMS.append(r.status_code)
    handler.send_response(r.status_code)
    handler.send_header('Content-Type', r.headers.get('content-type', 'application/octet-stream'))
    handler.send_header('Content-Length', str(len(r.content)))
    handler.end_headers()
    handler.wfile.write(r.content)
    return True


async def click(b, sel):
    await R.wait(b, "!!document.querySelector(%s) && !document.querySelector(%s).disabled" % (json.dumps(sel), json.dumps(sel)), 20, sel)
    x, y = await b.js("(()=>{const e=document.querySelector(%s);e.scrollIntoView({block:'center'});"
                      "const r=e.getBoundingClientRect();return [r.left+r.width/2,r.top+r.height/2]})()" % json.dumps(sel))
    for kind in ('mousePressed', 'mouseReleased'):
        await b.call('Input.dispatchMouseEvent', {'type': kind, 'x': x, 'y': y, 'button': 'left', 'clickCount': 1})


def _duration(data):
    p = Path(tempfile.mkdtemp(prefix='pc-rec-')) / 'clip.mp4'
    p.write_bytes(data)
    out = subprocess.run(['ffprobe', '-v', 'error', '-show_entries', 'format=duration', '-of', 'json', str(p)],
                         capture_output=True, text=True, timeout=60)
    return float(json.loads(out.stdout)['format']['duration'])


@CHROME
def test_record_review_trim_and_keep_in_files():
    got = {}
    TRIMS.clear()

    async def main():
        H.ROUTES.append(_forward_clip)
        try:
            with R.Node(members=[R.pubkey(ME)]) as n:
                async with R.Device(n, ME, pre_init=FAKE_SCREEN) as d:
                    b = d.b
                    await click(b, ".nav-item[data-view=recorder]")
                    await click(b, ".rec-big")
                    await R.wait(b, "!!document.querySelector('.rec-live')", 15, 'recording')
                    await asyncio.sleep(2.2)
                    await click(b, ".rec-pause")
                    await R.wait(b, "!!document.querySelector('.rec-live.paused')", 10, 'paused')
                    await asyncio.sleep(1.5)                                  # paused time is not recorded time
                    await click(b, ".rec-pause")
                    await asyncio.sleep(2.2)
                    await click(b, ".rec-stop")
                    await R.wait(b, "!!document.querySelector('.rec-review video')", 20, 'the review player')
                    got['meta'] = await b.js("document.querySelector('.rec-meta').textContent")
                    got['dur'] = await b.js("+document.querySelector('.rec-b').max")
                    got['streams_live'] = await b.js("window.__dispCalls")
                    # The recording PLAYS: the player decodes a real frame.
                    await b.js("document.querySelector('.rec-video').muted=true;document.querySelector('.rec-video').play().catch(()=>{});true")
                    await R.wait(b, "(()=>{const v=document.querySelector('.rec-video');return v.videoWidth>0&&v.currentTime>0.2})()", 20, 'the recording plays')
                    got['w'] = await b.js("document.querySelector('.rec-video').videoWidth")
                    # Trim to 1.0 .. 3.0 with the handles, the way a person drags them.
                    await b.js("(()=>{const a=document.querySelector('.rec-a'),e=document.querySelector('.rec-b');"
                               "a.value='1';a.dispatchEvent(new Event('input'));e.value='3';e.dispatchEvent(new Event('input'));return true})()")
                    got['labels'] = await b.js("[document.querySelector('.rec-av').textContent,document.querySelector('.rec-bv').textContent]")
                    await click(b, ".rec-keep")
                    await R.wait(b, "(window.__toastLog||[]).some(t=>/saved to Files/.test(t))", 60, 'saved to Files')
                    got['file'] = await b.js("""(async()=>{const FI=__PC.filesIdx(); await FI.ensure();
                      FI._norm&&FI._norm(); const all=Object.entries((FI.data&&FI.data.files)||{}).map(([sha,f])=>({sha,...f}));
                      return all.filter(f=>f.folder==='Recordings').map(f=>({name:f.name,mime:f.mime,size:f.size,sha:f.sha}))})()""")
                    # The kept bytes are the TRIM: fetch the encrypted file back through the client and measure it.
                    got['bytes'] = await b.js("""(async()=>{const f=(%s)[0]; const url=await __PC.encFileUrl(f.sha);
                      const buf=new Uint8Array(await (await fetch(url)).arrayBuffer());
                      let s='';for(let i=0;i<buf.length;i+=8192)s+=String.fromCharCode.apply(null,buf.subarray(i,i+8192));return btoa(s)})()"""
                                              % json.dumps(got['file']))
        finally:
            H.ROUTES.remove(_forward_clip)

    asyncio.run(main())
    import base64
    assert 3.6 <= got['dur'] <= 5.6, ("the measured length counts the pause or misses time", got['dur'])
    assert got['w'] == 640, got
    assert got['labels'] == ['0:01.0', '0:03.0'], got['labels']
    assert TRIMS == [200], ("the trim did not go through this node's clip endpoint", TRIMS)
    assert len(got['file']) == 1 and got['file'][0]['name'].endswith('(trimmed).mp4'), got['file']
    assert abs(_duration(base64.b64decode(got['bytes'])) - 2.0) < 0.5


@CHROME
def test_post_hands_an_mp4_to_the_composer():
    """Post converts the WebM to MP4 on this node (iOS plays no WebM) and the composer uploads it into the post."""
    got = {}
    TRIMS.clear()

    async def main():
        H.ROUTES.append(_forward_clip)
        try:
            with R.Node(members=[R.pubkey(ME)]) as n:
                async with R.Device(n, ME, pre_init=FAKE_SCREEN) as d:
                    b = d.b
                    await b.js("__PC.switchView('recorder');true")
                    await click(b, ".rec-big")
                    await asyncio.sleep(1.5)
                    await click(b, ".rec-stop")
                    await R.wait(b, "!!document.querySelector('.rec-review')", 20, 'review')
                    got['type'] = await b.js("document.querySelector('.rec-meta').textContent")
                    await click(b, ".rec-post")
                    await R.wait(b, "!!document.querySelector('.cmp-modal')", 30, 'the composer')
                    await R.wait(b, "/\\/blossom\\/[0-9a-f]{64}/.test((document.querySelector('.cmp-modal textarea')||{}).value||'')",
                                 60, 'the clip uploaded into the post')
                    got['text'] = await b.js("document.querySelector('.cmp-modal textarea').value")
        finally:
            H.ROUTES.remove(_forward_clip)

    asyncio.run(main())
    sha = __import__('re').search(r'/blossom/([0-9a-f]{64})', got['text']).group(1)
    data = H.STATE['blobs'][sha]
    if 'webm' in got['type']:
        assert TRIMS == [200], ("a WebM was posted without converting it to MP4", TRIMS)
    assert abs(_duration(data) - 1.5) < 0.8 and data[4:8] == b'ftyp', "the post carries no playable MP4"


@CHROME
def test_a_cancelled_pick_says_so_and_stays_ready():
    async def main():
        with R.Node(members=[R.pubkey(ME)]) as n:
            cancel = "navigator.mediaDevices.getDisplayMedia=async()=>{throw new DOMException('denied','NotAllowedError')};"
            async with R.Device(n, ME, pre_init=FAKE_SCREEN + cancel) as d:
                b = d.b
                await b.js("__PC.switchView('recorder');true")
                await click(b, ".rec-big")
                await R.wait(b, "(window.__toastLog||[]).some(t=>/Recording cancelled/.test(t))", 10, 'cancelled toast')
                assert await b.js("!!document.querySelector('.rec-big') && !document.querySelector('.rec-live')")
    asyncio.run(main())


@CHROME
def test_on_a_phone_it_explains_instead_of_offering_a_dead_button():
    async def main():
        with R.Node(members=[R.pubkey(ME)]) as n:
            async with R.Device(n, ME, phone=True, pre_init="delete navigator.mediaDevices.getDisplayMedia;"
                                "Object.defineProperty(MediaDevices.prototype,'getDisplayMedia',{value:undefined});") as d:
                b = d.b
                await b.js("__PC.switchView('recorder');true")
                await R.wait(b, "/not available here/.test(document.getElementById('feed').textContent)", 15, 'the explanation')
                assert await b.js("!document.querySelector('.rec-big')")
    asyncio.run(main())
