"""CONCORD: AN IMAGE-HEAVY ENCRYPTED ROOM SHOWS THE PICTURES ON SCREEN FIRST, NOT LAST.

Reported 2026-10-07: "decryption slow in image heavy rooms". Driven in the real bundled client: a
CORD room holding 60 messages, 40 of them carrying an AES-GCM sealed photo (JPEG/PNG, 200 KB-2 MB,
21 MB in all) served as ciphertext by a Blossom fixture with a real round trip and a finite pipe
(150 ms, 8 MB/s per connection). The room opens at its newest message, as it does for a person.

MEASURED ON THE OLD CODE before a line changed: the hydrator awaited every attachment in DOM order,
one download + decrypt at a time (never more than ONE fetch in flight), oldest first -- so the two
photos on screen were painted after 8.8 s, behind all 38 off-screen ones, and every one of the 40
was downloaded and decrypted whether anybody would scroll to it or not. Text was never the problem
(all 60 messages painted in ~50 ms) and nothing ran long on the main thread (0 long tasks: WebCrypto
is already off-thread), so neither is what this fixes.

What a person sees is asserted: the on-screen photos are painted quickly and BEFORE the off-screen
ones are fetched; a photo two screens away is not downloaded until it is scrolled near; scrolling to
the top still brings every picture there; a room repainting three times mid-load (as a live room
does) decrypts each blob exactly once; and no more than four downloads run at once.
"""
import asyncio
import hashlib
import io
import json
import random
import tempfile
import threading
import time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from PIL import Image

from tests.client import test_desktop_offline_full_app as desktop
from tests.client.test_concord_attach_survives_a_repaint import EXTRA


@pytest.fixture(scope='module', autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


N_MESSAGES = 60
N_IMAGES = 40
LATENCY = 0.15          # seconds per request before the first byte
BANDWIDTH = 8_000_000   # bytes per second, per connection


def _image(rng, i):
    """A photo-sized picture whose encoded size lands between ~200 KB and ~2 MB: noise does not
    compress, so the byte count is set by the pixel count."""
    png = i % 4 == 3
    w = rng.choice([640, 800, 1024, 1280])
    h = rng.choice([480, 600, 768])
    if png:
        w, h = w // 2, h // 2
    img = Image.frombytes('RGB', (w, h), rng.randbytes(w * h * 3))
    out = io.BytesIO()
    img.save(out, 'PNG' if png else 'JPEG', quality=92)
    return out.getvalue(), ('image/png' if png else 'image/jpeg'), w, h


def _fixture(store):
    rng = random.Random(7)
    messages, sizes = [], []
    pk = '02' * 32
    for i in range(N_MESSAGES):
        tags = []
        if i % 3 != 0 and len(sizes) < N_IMAGES or (N_MESSAGES - i) <= (N_IMAGES - len(sizes)):
            plain, mime, w, h = _image(rng, i)
            key, nonce = rng.randbytes(32), rng.randbytes(16)
            cipher = AESGCM(key).encrypt(nonce, plain, None)
            name = hashlib.sha256(cipher).hexdigest()
            Path(store, name).write_bytes(cipher)
            sizes.append(len(plain))
            tags.append(['imeta', f'url https://files.test/{name}', f'm {mime}',
                         'encryption-algorithm aes-gcm', f'decryption-key {key.hex()}',
                         f'decryption-nonce {nonce.hex()}', f'ox {hashlib.sha256(plain).hexdigest()}',
                         f'name photo{i}.{"png" if mime == "image/png" else "jpg"}', f'dim {w}x{h}'])
        messages.append({'id': f'{i:064x}', 'pubkey': pk, 'by': 'Bob', 'text': f'message number {i}',
                         'at': 1_700_000_000_000 + i * 60_000, 'kind': 9, 'tags': tags, 'remote': True})
    return messages, sizes


class BlossomHandler(SimpleHTTPRequestHandler):
    """Ciphertext served the way a remote Blossom serves it: a round trip, then a finite pipe."""
    def __init__(self, *a, **k):
        super().__init__(*a, directory=BlossomHandler.root, **k)

    def log_message(self, *a):
        pass

    def end_headers(self):
        self.send_header('Access-Control-Allow-Origin', '*')
        super().end_headers()

    def do_GET(self):
        p = Path(BlossomHandler.root, self.path.strip('/').split('?')[0])
        if not p.is_file():
            self.send_error(404)
            return
        data = p.read_bytes()
        time.sleep(LATENCY)
        self.send_response(200)
        self.send_header('Content-Type', 'application/octet-stream')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        chunk = 64 * 1024
        try:
            for at in range(0, len(data), chunk):
                self.wfile.write(data[at:at + chunk])
                time.sleep(chunk / BANDWIDTH)
        except (BrokenPipeError, ConnectionResetError):
            pass  # the page closed: the browser hung up mid-body


INSTRUMENT = r'''
window.__m={t0:0,fetchNow:0,fetchMax:0,fetches:0,decNow:0,decMax:0,decrypts:{},decOrder:[],long:[],urls:0,revoked:0,firstText:0,allText:0,painted:{}};
(()=>{
  try{new PerformanceObserver(l=>{for(const e of l.getEntries())__m.long.push([Math.round(e.startTime),Math.round(e.duration)]);}).observe({type:'longtask',buffered:true});}catch(_){}
  const real=window.fetch.bind(window);
  window.fetch=async(u,o)=>{const url=String(u&&u.url||u);
    if(!url.startsWith('https://files.test/'))return real(u,o);
    __m.fetches++;__m.fetchNow++;__m.fetchMax=Math.max(__m.fetchMax,__m.fetchNow);
    try{const b=await new Promise((ok,no)=>{const x=new XMLHttpRequest();x.open('GET',url.replace('https://files.test/','http://127.0.0.1:BLOSSOM/'));x.responseType='arraybuffer';x.onload=()=>ok(x);x.onerror=()=>no(new TypeError('network'));x.send();});return new Response(b.response,{status:b.status});}
    finally{__m.fetchNow--;}};
  const sub=crypto.subtle,dec=sub.decrypt.bind(sub);
  sub.decrypt=async(alg,key,data)=>{const n=data&&data.byteLength||0;if(n<50000)return dec(alg,key,data);
    const id=Array.from(new Uint8Array(alg.iv)).map(x=>x.toString(16).padStart(2,'0')).join('');
    __m.decrypts[id]=(__m.decrypts[id]||0)+1;__m.decOrder.push(id);__m.decNow++;__m.decMax=Math.max(__m.decMax,__m.decNow);
    try{return await dec(alg,key,data);}finally{__m.decNow--;}};
  const cu=URL.createObjectURL.bind(URL),ru=URL.revokeObjectURL.bind(URL);
  URL.createObjectURL=b=>{if(b&&/^image\//.test(b.type||''))__m.urls++;return cu(b);};
  URL.revokeObjectURL=u=>{__m.revoked++;return ru(u);};
})();
'''

# Per frame: what a person sees in the message pane right now.
SAMPLE = r'''(()=>{const now=performance.now()-__m.t0,box=document.querySelector('.cc-messages');if(!box)return null;
  const r=box.getBoundingClientRect(),msgs=[...box.querySelectorAll('.cc-message')];
  const inView=el=>{const q=el.getBoundingClientRect();return q.bottom>r.top&&q.top<r.bottom&&q.height>0;};
  if(msgs.length&&!__m.firstText)__m.firstText=now;
  if(msgs.length>=60&&!__m.allText)__m.allText=now;
  const hosts=[...box.querySelectorAll('.cc-encrypted-attachment')];
  const vis=hosts.filter(inView),ready=h=>{const i=h.querySelector('img');return !!i&&/^blob:/.test(i.src)&&i.complete&&i.naturalWidth>0;};
  for(const h of hosts)if(ready(h)){const k=h.dataset.ccAttachment;if(!__m.painted[k])__m.painted[k]={t:now,inView:inView(h)};}
  return {now,msgs:msgs.length,hosts:hosts.length,vis:vis.length,visReady:vis.filter(ready).length,ready:hosts.filter(ready).length};})()'''


def _run(store, port, messages, repaints=True):
    res = {}

    async def check(b):
        await b.call("Emulation.setDeviceMetricsOverride", {"width": 1280, "height": 850, "deviceScaleFactor": 1, "mobile": False})
        await desktop.login(b)
        await b.js(r'''(()=>{
          const room={name:'Photos',communityId:'c'.repeat(64),naddr:'fixture-community',
            channels:[{id:'fixture-general',name:'general'}],cord:{bundle:{relays:['wss://fixture.invalid']},hydrated:true}};
          localStorage.setItem('pc.concord.rooms.v1.'+__PC.me().pubkey,JSON.stringify([room]));localStorage.setItem('pc.concord.active.v1.'+__PC.me().pubkey,'0');
          localStorage.setItem('pc.concord.test.fixture-community',JSON.stringify(%s));
          window.PosterCordReader={inspectControl:()=>({controlPubkeys:[],channels:[{id:'fixture-general',name:'general',streamPubkeys:[]}]}),
            inspectChat:async()=>({messages:[],reactions:[],reactionIds:[]})};
          __m.t0=performance.now();__PC.switchMessagesTab('concord');return true;})()''' % json.dumps(messages))
        samples, repainted = [], 0
        t_end = time.time() + 40
        while time.time() < t_end:
            s = await b.js(SAMPLE)
            if s:
                samples.append(s)
                # A live room repaints constantly (messages, profiles, typing): do what it does.
                if repaints and s['now'] > 300 and repainted < 3 and s['now'] > 300 + repainted * 700:
                    await b.js("PCConcord.render()")
                    repainted += 1
                if s['vis'] and s['visReady'] == s['vis'] and not res.get('visAt'):
                    res['visAt'] = s['now']
                    res['atVisible'] = await b.js("({fetches:__m.fetches,decrypts:Object.keys(__m.decrypts).length})")
                if s['vis'] and s['visReady'] == s['vis'] and repainted >= 3 and (s['ready'] >= s['hosts'] or s['now'] > 12000):
                    break
            await asyncio.sleep(0.03)
        res['samples'] = samples
        res['m'] = await b.js("JSON.parse(JSON.stringify(__m))")
        # Scroll to the top: every image there must still arrive.
        await b.js("(()=>{const box=document.querySelector('.cc-messages');box.scrollTop=0;box.dispatchEvent(new Event('scroll'));})()")
        t_end = time.time() + 30
        while time.time() < t_end:
            s = await b.js(SAMPLE)
            if s and s['vis'] and s['visReady'] == s['vis']:
                res['topOk'] = True
                break
            await asyncio.sleep(0.05)
        res['final'] = await b.js("JSON.parse(JSON.stringify(__m))")
        res['screen'] = await b.js("document.body.innerText")

    asyncio.run(desktop.with_browser('online', '', check, EXTRA + INSTRUMENT.replace('BLOSSOM', str(port))))
    return res


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
def test_the_photos_on_screen_are_decrypted_first_and_the_rest_only_when_near():
    with tempfile.TemporaryDirectory(prefix='pc-blossom-') as store:
        messages, sizes = _fixture(store)
        BlossomHandler.root = store
        srv = ThreadingHTTPServer(('127.0.0.1', 0), BlossomHandler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            res = _run(store, srv.server_port, messages)
        finally:
            srv.shutdown()
    m = res['m']
    longs = m['long']
    summary = {
        'images': len(sizes), 'MB': round(sum(sizes) / 1e6, 1),
        'firstText': round(m['firstText']), 'allText': round(m['allText']),
        'visibleImagesPainted': round(res.get('visAt') or -1), 'atVisible': res.get('atVisible'),
        'fetchMax': m['fetchMax'], 'fetches': m['fetches'], 'decMax': m['decMax'],
        'decryptsPerBlob_max': max(m['decrypts'].values() or [0]), 'distinctDecrypted': len(m['decrypts']),
        'longtasks': len(longs), 'longtask_ms_total': sum(d for _, d in longs), 'longtask_max': max([d for _, d in longs] or [0]),
        'objectURLs': m['urls'], 'revoked': m['revoked'], 'topOk': res.get('topOk'),
        'final_fetches': res['final']['fetches'], 'final_urls': res['final']['urls'],
    }
    print('BENCH', json.dumps(summary))
    print('PAINT ORDER', sorted(((round(v['t']), int(k, 16), v['inView']) for k, v in m['painted'].items()))[:50])

    # Text first, always: the 60 messages are on screen long before the pictures.
    assert m['allText'] and m['allText'] < 2000, summary
    # The photos a person is looking at arrive promptly...
    assert res.get('visAt') and res['visAt'] < 3000, ('the photos on screen took too long', summary)
    # ...and NOT behind the ones nobody can see (the old code had fetched all 40 by then).
    assert res['atVisible']['fetches'] <= 8, ('the on-screen photos waited behind off-screen ones', summary)
    # A photo several screens away is not downloaded until it is scrolled near.
    assert m['fetches'] <= 12, ('off-screen photos were downloaded eagerly', summary)
    # Three repaints mid-load: each blob still decrypted once, never re-fetched.
    assert max(m['decrypts'].values()) == 1 and m['fetches'] == len(m['decrypts']), summary
    # Bounded, but in parallel: one-at-a-time is what made the old room slow.
    assert 2 <= m['fetchMax'] <= 4, summary
    # Scrolling to the top still brings every picture there, and nothing failed.
    assert res.get('topOk'), ('the photos at the top never arrived after scrolling there', summary)
    assert 'Could not decrypt' not in res['screen'], res['screen'][-400:]
