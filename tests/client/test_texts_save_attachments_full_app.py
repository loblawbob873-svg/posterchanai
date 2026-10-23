"""SAVING A PICTURE MESSAGE'S MEDIA FROM THE TEXTS THREAD, ON THE APK AND IN A BROWSER.

2026-10-23: "sms apk does not let me save media attachments". In the thread (static/js/client/sms.js
drawAtt) only a part that was neither a picture, a video nor a sound had a Save button: a picture could
be saved only from the lightbox, the video viewer has no Save (the lightbox offers it for images only),
and audio had no way out at all. Every attachment now carries a Save under it.

The message is the PHONE's own: `SmsSweep.run`, run on a JVM (tests/sms_java_phone.py `sweep`), archives
a received MMS with a picture, a video and a voice note, sealing each part into the drive the way the
handset does. The shipped client reads it back from a real relay + drive (tests/client/wallpaper_harness.py)
and decrypts it. The APK is the Capacitor surface the page calls at click time -- `isNativePlatform()` and
the `MediaSave` plugin at its exact interface (`available()` -> {ok}, `save({data, name, mime, hint})` ->
{ok, where} or a rejection) -- a recorder that keeps what it was asked to write.

Asserted: the bytes MediaSave is handed are the attachment's decrypted bytes, for all three; its type
and a name with the right extension; the success says where; a refused write says it failed, in the
phone's words, and does NOT silently become a share sheet; in a browser the same button downloads the
same bytes.

Fail-first: PC_WALLPAPER_OVERRIDE="/static/js/client/sms.js=/tmp/old-sms.js,..." serves an older copy.
"""
import asyncio
import base64
import json
import os
import sys
import time
from pathlib import Path

import pytest
import websockets

from tests.client import wallpaper_harness as H

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'tests'))
sys.path.insert(0, str(ROOT))
import sms_java_phone  # noqa: E402
from app.services.nostr.nip44 import encrypt_self  # noqa: E402

pytestmark = [
    pytest.mark.skipif(not Path(H.CHROME).exists(), reason='Chrome required'),
    pytest.mark.skipif(not sms_java_phone.available(), reason='JDK + android.jar required'),
]

FEATURE_COVERS = ('texts',)
FILL = 230
MK = bytes(range(32))
ALICE = '+15550230001'
BODY = 'S1 the photos from saturday'
# Recognisable bytes behind real magic numbers -- what each kind of MMS part starts with.
PHOTO = b'\xff\xd8\xff\xe0' + bytes((i * 37 + 11) & 0xFF for i in range(5000))
VIDEO = b'\x00\x00\x00\x18ftyp3gp4' + bytes((i * 13 + 5) & 0xFF for i in range(7000))
VOICE = b'#!AMR\n' + bytes((i * 7 + 3) & 0xFF for i in range(3000))
PARTS = [('image/jpeg', 'IMG_0412.jpg', PHOTO), ('video/3gpp', 'VID_0413.3gp', VIDEO), ('audio/amr', 'voice', VOICE)]


def _pubkey(fill):
    import coincurve
    return coincurve.PrivateKey(bytes([fill]) * 32).public_key.format(True)[1:].hex()


async def publish(ev):
    async with websockets.connect('ws://127.0.0.1:%d' % H.RELAY.port) as ws:
        await ws.send(json.dumps(['EVENT', ev]))
        while True:
            m = json.loads(await asyncio.wait_for(ws.recv(), 10))
            if m[0] == 'OK' and m[1] == ev['id']:
                assert m[2], m
                return


STATE_JS = ("({errors:window.__errors,toasts:window.__toasts,saved:(window.__saved||[]).length,"
            "feed:(document.querySelector('#feed')||{}).innerText})")


async def until(b, expr, seconds=40):
    await H.wait(b, expr, seconds, state=STATE_JS)


# The APK, as the page sees it at click time.
APK = r'''(()=>{window.__saved=[];window.__shared=[];window.__refuse=%s;
  window.Capacitor={isNativePlatform:()=>true,Plugins:{
    MediaSave:{available:async()=>({ok:true}),
      save:async o=>{if(window.__refuse)throw new Error(window.__refuse);window.__saved.push(o);
        const k=/^image\//.test(o.mime)?'Images':/^video\//.test(o.mime)?'Videos':/^audio\//.test(o.mime)?'Music':'Files';
        return {ok:true,uri:'content://media/external/downloads/1',where:'Download/PosterChan/'+k+'/'+o.name};}},
    Filesystem:{writeFile:async o=>{window.__shared.push(o.path);return {uri:'file:///cache/'+o.path};}},
    Share:{share:async o=>{window.__shared.push(o);return {};}}}};return true})()'''

SAVES = "[...document.querySelectorAll('#sms-msgs .sms-att .sms-att-save')]"


@pytest.fixture(scope='module')
def env(tmp_path_factory):
    H.start()
    H.OVERRIDES.clear()
    for pair in filter(None, os.environ.get('PC_WALLPAPER_OVERRIDE', '').split(',')):
        path, _, src = pair.partition('=')
        H.OVERRIDES[path] = src
    pk, sec = _pubkey(FILL), ('%02x' % FILL) * 32

    def session(h, method, path, body):
        p = path.split('?')[0]
        if p == '/api/auth/nostr-login' and method == 'POST':
            if ((json.loads(body or b'{}') or {}).get('pubkey') or '') != pk:
                return False
            h._json({'access_token': 'tok-' + pk, 'token_type': 'bearer', 'user': {'id': 1, 'nostr_npub': pk}})
            return True
        if p == '/api/instance-welcome/access' and method == 'GET':
            h._json({'pubkey': pk, 'qualified': True})
            return True
        return False
    H.ROUTES.append(session)
    # The account's drive key, wrapped to itself, as the files index carries it (what the phone used).
    H.STATE['indexes'][pk] = {'mk': encrypt_self(bytes.fromhex(sec), json.dumps({'k': base64.b64encode(MK).decode()}))}
    phone = sms_java_phone.Phone()
    state = str(tmp_path_factory.mktemp('save') / 'phone.json')
    rows = [{'address': ALICE, 'body': BODY, 'date': int(time.time() * 1000) - 60_000, 'type': 1, 'mms': True,
             'parts': [{'ct': ct, 'name': nm, 'b64': base64.b64encode(data).decode()} for ct, nm, data in PARTS]}]
    out = phone.run('sweep', sec, state, {'mk': base64.b64encode(MK).decode(), 'rows': rows})
    assert not out.get('error') and out['attachments'] == 3, out
    for sha, b64 in out['blobs'].items():
        H.STATE['blobs'][sha] = base64.b64decode(b64)

    async def publish_all():
        for ev in out['events']:
            await publish(ev)
    asyncio.run(asyncio.wait_for(publish_all(), 30))
    yield {'pk': pk}
    H.ROUTES.remove(session)
    phone.close()


async def _thread(env):
    d = H.Device(sk_fill=FILL)
    b = await d.__aenter__()
    assert await b.js('__PC.me().pubkey') == env['pk']
    await b.js("document.querySelector('.nav-item[data-view=\"texts\"]').click()")
    await until(b, "__PC.VIEW==='texts' && !!document.querySelector('#feed .sms-wrap')")
    await until(b, "[...document.querySelectorAll('#feed .sms-thread .sms-snip')].some(x=>/S1 the photos/.test(x.textContent))")
    await b.js("[...document.querySelectorAll('#feed .sms-thread')].find(x=>/S1 the photos/.test("
               "x.querySelector('.sms-snip').textContent)).click()")
    await until(b, "!!document.querySelector('#sms-msgs')")
    # All three parts decrypted and drawn: a picture, a video and a sound.
    await until(b, "!!document.querySelector('#sms-msgs img.sms-att-img') && !!document.querySelector('#sms-msgs video.sms-att-img')"
                   " && !!document.querySelector('#sms-msgs audio.sms-att-img')", 60)
    return d, b


async def _save_buttons(b):
    """The Save under each attachment, by the attachment it belongs to."""
    await until(b, SAVES + ".length===3", 10)
    return await b.js("%s.map(x=>({kind:(x.closest('.sms-att').querySelector('img,video,audio')||{}).tagName||'',"
                      "label:x.getAttribute('aria-label'),text:x.textContent}))" % SAVES)


def test_every_media_attachment_saves_its_decrypted_bytes_to_the_phone(env):
    async def run():
        d, b = await _thread(env)
        try:
            buttons = await _save_buttons(b)
            assert [x['kind'] for x in buttons] == ['IMG', 'VIDEO', 'AUDIO'], buttons
            assert [x['label'] for x in buttons] == ['Save Photo', 'Save Video', 'Save Audio'], buttons
            await b.js(APK % 'null')
            got = {}
            for i, (ct, name, data) in enumerate(PARTS):
                await b.js("%s[%d].click()" % (SAVES, i))
                await until(b, "window.__saved.length===%d" % (i + 1), 20)
                o = await b.js("window.__saved[%d]" % i)
                got[ct] = o
                assert base64.b64decode(o['data']) == data, (ct, 'the saved bytes are not the attachment')
                assert o['mime'] == ct, o['mime']
                assert o['name'] == name if '.' in name else o['name'].startswith(name), o['name']
            assert got['audio/amr']['name'] == 'voice.amr', got['audio/amr']['name']
            await until(b, "window.__toasts.filter(t=>/saved to Download\\/PosterChan\\//.test(t)).length===3", 10)
            toasts = await b.js("window.__toasts")
            assert any(t == 'Photo saved to Download/PosterChan/Images/IMG_0412.jpg' for t in toasts), toasts
            assert await b.js("window.__shared") == [], 'a Save opened the share sheet'
            assert not await b.js("window.__errors.filter(e=>!/ResizeObserver/.test(e))")
        finally:
            await d.__aexit__()
    asyncio.run(run())


def test_a_save_the_phone_refuses_says_it_failed_and_does_not_open_the_share_sheet(env):
    async def run():
        d, b = await _thread(env)
        try:
            await _save_buttons(b)
            await b.js(APK % json.dumps('No space left on device'))
            await b.js("%s[2].click()" % SAVES)
            await until(b, "window.__toasts.some(t=>/Could not save Audio: No space left on device/.test(t))", 20)
            assert await b.js("window.__shared") == [], 'a refused save turned into a share sheet'
            assert not await b.js("window.__toasts.some(t=>/saved to/i.test(t))")
            # The button is usable again for a retry.
            assert await b.js("!%s[2].disabled" % SAVES)
        finally:
            await d.__aexit__()
    asyncio.run(run())


def test_in_a_browser_the_same_save_downloads_the_same_bytes(env):
    async def run():
        d, b = await _thread(env)
        try:
            await _save_buttons(b)
            # The web path is an <a download> of an object URL (saveBlobAs); read what it points at.
            await b.js(r'''(()=>{window.__dl=[];const c=HTMLAnchorElement.prototype.click;
              HTMLAnchorElement.prototype.click=function(){if(this.download){
                window.__dl.push(fetch(this.href).then(r=>r.arrayBuffer()).then(a=>({name:this.download,
                  b64:btoa(String.fromCharCode(...new Uint8Array(a)))})));return;}return c.apply(this,arguments)};return true})()''')
            await b.js("%s[1].click()" % SAVES)
            await until(b, "window.__dl.length===1", 20)
            o = await b.js("(async()=>await window.__dl[0])()")
            assert o['name'] == 'VID_0413.3gp', o['name']
            assert base64.b64decode(o['b64']) == VIDEO
        finally:
            await d.__aexit__()
    asyncio.run(run())


if __name__ == '__main__':
    sys.exit(pytest.main([os.path.abspath(__file__), '-q']))
