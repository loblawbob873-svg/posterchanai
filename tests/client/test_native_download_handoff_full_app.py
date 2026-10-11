"""The ⋮ → Download built into a PLAYER saves the whole file on the APK, and every save lands in ONE tree.

Reported by a member on the APK (2026-10-11): after "Convert to MP3" in the AI chat, "pressing the orange
download button works but hitting the one in the mp3 player does not" -- it "fails to download & adds an
incomplete file to the phone". The orange button fetches with the app's auth and saves through MediaSave.
The player's own download is Android's: the WebView's DownloadListener handed the URL to DownloadManager
with no session and no Bearer token, and an AI-chat artifact (`/api/files/...`) is behind the user's
auth -- a 401, "download failed", and a stub left in Downloads.

MainActivity now hands a download on THIS instance back to the page as `pcNativeDownload` (the routing
rule is javac-tested in tests/test_android_download_route.py). This drives the SHIPPED client's half:

  handoff-saves-full-bytes   the event for an absolute instance URL that REQUIRES the Bearer token (the
                             fixture answers 401 without it) reaches MediaSave.save with the exact MP3
                             bytes as audio/mpeg, named from Content-Disposition, and no share sheet;
  refusal-saves-nothing      when the instance refuses, NOTHING is written (the old listener saved the
                             401's error body as the "file") and a toast says why;
  one-posterchan-tree        the orange MP3 Download reports Download/PosterChan/Music, a generated image
                             Download/PosterChan/Images, and a cloned-voice line is SAID to be a voice
                             (`hint:'voice'`) so it goes to Download/PosterChan/Voices -- MediaSave cannot
                             tell a voice from a song by its bytes.

The Capacitor MediaSave fixture answers `where` with the folder MediaSaveRules computes for that MIME and
hint (the Java rule itself is run under javac in tests/test_android_media_save.py).
"""
import asyncio
import base64
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop
from tests.client import test_generated_song_to_mp3_full_app as song


@pytest.fixture(scope='module', autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


MP3 = song.MP3
PNG = b'\x89PNG\r\n\x1a\n' + b'png-bytes' * 8
GATED = '/api/files/vyram/5/enc_' + 'c' * 64 + '.mp3'
REFUSED = '/api/files/vyram/5/enc_' + 'd' * 64 + '.mp3'

# The instance: the gated artifact answers ONLY with the session's Bearer token; the refused one never.
GATE = r'''
window.__authSeen=[];
const MP3G=Uint8Array.from(atob('%(mp3)s'),c=>c.charCodeAt(0));
const prevFetch=window.fetch;
window.fetch=(url,opts)=>{const u=new URL(String(url),location.href);
  const h=(opts&&opts.headers)||{}; const auth=(h.get?h.get('Authorization'):(h.Authorization||h.authorization))||'';
  if(u.pathname==='%(gated)s'){ __authSeen.push(auth);
    if(auth!=='Bearer '+window.__PC_TOKEN__ || !window.__PC_TOKEN__)
      return Promise.resolve(new Response('{"detail":"Not authenticated"}',{status:401,headers:{'Content-Type':'application/json'}}));
    return Promise.resolve(new Response(new Blob([MP3G],{type:'audio/mpeg'}),{status:200,
      headers:{'Content-Disposition':'attachment; filename="posterchan-song-rain.mp3"'}})); }
  if(u.pathname==='%(refused)s')
    return Promise.resolve(new Response('{"detail":"Not authenticated"}',{status:401,headers:{'Content-Type':'application/json'}}));
  return prevFetch(url,opts);};
''' % {'mp3': base64.b64encode(MP3).decode(), 'gated': GATED, 'refused': REFUSED}

# The APK with MediaSave; `where` mirrors MediaSaveRules.folder(kind(mime, hint)).
NATIVE = r'''window.Capacitor={isNativePlatform:()=>true,Plugins:{
  MediaSave:{available:async()=>({ok:true}),save:async o=>{
     const m=String(o.mime||''), k=o.hint==='voice'?'Voices':m.startsWith('image/')?'Images':m.startsWith('video/')?'Videos':m.startsWith('audio/')?'Music':'Files';
     const bin=atob(o.data||''); __saved.push({name:o.name,mime:m,hint:o.hint||'',bytes:bin.length,b64:o.data||''});
     return{ok:true,where:'Download/PosterChan/'+k+'/'+o.name};}},
  Filesystem:{writeFile:async o=>({uri:'file:///cache/'+o.path})},
  Share:{share:async o=>{__shares.push(o);}}}};'''

TOASTS = "[...document.querySelectorAll('#toast-root .toast')].map(t=>t.textContent)"


def _fire(url_expr, mime, disp=''):
    return ("window.dispatchEvent(new CustomEvent('pcNativeDownload',{detail:{url:%s,mime:%r,contentDisposition:%r}}))"
            % (url_expr, mime, disp))


BASE = "(String(window.__PC_API_BASE__||location.origin).replace(/\\/+$/,''))"


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
def test_the_players_own_download_saves_the_whole_file_with_the_apps_auth():
    async def check(b):
        await desktop.login(b)
        await b.js("window.__saved=[]; window.__shares=[];")
        await b.js("__PC.ensureAiSession()")
        await b.until("!!window.__PC_TOKEN__")
        await b.js(GATE)
        await b.js(NATIVE)
        # handoff-saves-full-bytes: an ABSOLUTE instance URL, the shape the <audio> element's ⋮ hands over.
        await b.js(_fire(BASE + "+%r" % GATED, 'audio/mpeg', 'attachment; filename="posterchan-song-rain.mp3"'))
        await b.until("__saved.length>0 || (%s).some(t=>/could not save/.test(t))" % TOASTS)
        st = await b.js("({saved:__saved, shares:__shares.length, auth:__authSeen, toasts:%s})" % TOASTS)
        assert st['auth'] and st['auth'][-1].startswith('Bearer '), \
            ('the page fetched the artifact without the app session', st['auth'])
        assert len(st['saved']) == 1, st
        s = st['saved'][0]
        assert base64.b64decode(s['b64']) == MP3, 'MediaSave did not receive the exact MP3 bytes'
        assert s['mime'] == 'audio/mpeg', s['mime']
        assert s['name'] == 'posterchan-song-rain.mp3', s['name']
        assert st['shares'] == 0, 'the hand-off opened the share sheet instead of saving'
        assert any('Download/PosterChan/Music/posterchan-song-rain.mp3' in t for t in st['toasts']), st['toasts']

        # refusal-saves-nothing: a 401 is not a file. The old listener saved the error body.
        await b.js("__saved.length=0")
        await b.js(_fire(BASE + "+%r" % REFUSED, 'audio/mpeg'))
        await b.until("(%s).some(t=>/could not save/.test(t))" % TOASTS)
        st = await b.js("({saved:__saved, toasts:%s})" % TOASTS)
        assert st['saved'] == [], ('a refused download still wrote a file', st['saved'])
        why = [t for t in st['toasts'] if 'could not save' in t]
        assert why and '401' in why[-1], ('the failure did not say why', why)
    asyncio.run(desktop.with_browser('online', '', check, song.PLAIN))


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
def test_saves_land_in_one_posterchan_tree_and_a_voice_is_said_to_be_one():
    async def check(b):
        await b.call('Emulation.setDeviceMetricsOverride', dict(width=390, height=900, deviceScaleFactor=1, mobile=True))
        await desktop.login(b)
        await b.js(song.STUB)
        await b.js("__PC.switchView('ai')")
        # The orange Download on the converted MP3 (persisted form) -> Music.
        await b.until("!!document.querySelector('#ai-msgs .ai-mp3file')")
        await b.js("document.querySelector('#ai-msgs .ai-mp3file').click()")
        await b.until("[...document.querySelectorAll('#ai-msgs .ai-msg.assistant audio')].some(a=>a.src.includes('enc_bbbb'))")
        await b.js(NATIVE)
        await b.js("(()=>{const a=[...document.querySelectorAll('#ai-msgs .ai-msg.assistant')].filter(m=>m.querySelector('audio')).pop();"
                   "a.querySelector('.ai-dlfile').click()})()")
        await b.until("__saved.length>0")
        await b.until("(%s).some(t=>t.includes('Download/PosterChan/Music/'))" % TOASTS)
        assert (await b.js("__saved[0]"))['mime'] == 'audio/mpeg'

        # A generated image -> Images.
        await b.js("__saved.length=0")
        await b.js("__chatWS.onmessage({data:JSON.stringify({type:'response',data:{type:'generated_image',"
                   "content:'a cat',image:'%s',mime:'image/png'}})})" % base64.b64encode(PNG).decode())
        await b.until("!!document.querySelector('#ai-msgs .ai-dl-fx')")
        await b.js("[...document.querySelectorAll('#ai-msgs .ai-dl-fx')].pop().click()")
        await b.until("__saved.length>0")
        await b.until("(%s).some(t=>t.includes('Download/PosterChan/Images/'))" % TOASTS)
        img = await b.js("__saved[0]")
        assert img['mime'] == 'image/png' and img['hint'] == '', img

        # A cloned-voice line (live payload marked kind:'voice') -> said to be a voice.
        await b.js("__saved.length=0")
        await b.js("__chatWS.onmessage({data:JSON.stringify({type:'response',data:{type:'generated_video',"
                   "content:'hello there',video:'%s',format:'mp4',has_audio:true,kind:'voice'}})})"
                   % base64.b64encode(song.MP4).decode())
        await b.until("document.querySelectorAll('#ai-msgs .ai-dl-fx').length>=2")
        await b.js("[...document.querySelectorAll('#ai-msgs .ai-dl-fx')].pop().click()")
        await b.until("__saved.length>0")
        v = await b.js("__saved[0]")
        assert v['hint'] == 'voice', ('a voice line was saved as an ordinary video', v)
        await b.until("(%s).some(t=>t.includes('Download/PosterChan/Voices/'))" % TOASTS)
    asyncio.run(desktop.with_browser('online', '', check, song.PLAIN))
