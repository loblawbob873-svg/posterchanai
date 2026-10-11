"""A song made with `musicgeni` in AI chat can be turned into an MP3 and SAVED to the phone.

Asked by a member on the APK (2026-10-11): "can I download the MP3 of a song I generated?" `musicgeni`
answers with a branded MP4 (the song over a PosterChan card). The way to an MP3 is the same one used
for YouTube and any video -- "Convert to MP3", which sends the MP4 back through `extractaudio` -- and
this drives the SHIPPED client through all of it, at phone and desktop width:

  * after a reload (the persisted `!video[song](...)` form) AND live (a `generated_video` payload with
    `has_audio`) the song carries a "Convert to MP3" button;
  * pressing it sends `extractaudio` with the song's MP4 attached -- the exact bytes, under a name that
    says what it is (every song came back as `song.mp3`, so a phone's Download folder filled with
    `song.mp3`, `song (1).mp3`, ...);
  * the MP3 that comes back has a Download button, and on the APK (MediaSave) that SAVES an audio/mpeg
    file to the phone without opening the share sheet.

Only the instance (fetch), the chat socket and the Capacitor plugins are fixtures.
"""
import asyncio
import base64
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop


@pytest.fixture(scope='module', autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


MP4 = b'\x00\x00\x00\x18ftypmp42' + b'song-video-bytes' * 8
MP3 = b'ID3\x04\x00' + b'song-mp3-bytes' * 8
SONG_MP4 = '/api/files/vyram/5/enc_' + 'a' * 64 + '.mp4'
SONG_MP3 = '/api/files/vyram/5/enc_' + 'b' * 64 + '.mp3'

PLAIN = "localStorage.setItem('pc_nostr_settings',JSON.stringify({...JSON.parse(localStorage.getItem('pc_nostr_settings')||'{}'),osMode:false}));"
STUB = r'''
window.__sent=[]; window.__saved=[]; window.__shares=[];
const MP4=Uint8Array.from(atob('%(mp4)s'),c=>c.charCodeAt(0)), MP3=Uint8Array.from(atob('%(mp3)s'),c=>c.charCodeAt(0));
const orig=window.fetch;
window.fetch=(url,opts)=>{const u=new URL(String(url),location.href);
 const reply=(v,s=200)=>Promise.resolve(new Response(JSON.stringify(v),{status:s,headers:{'Content-Type':'application/json'}}));
 if(u.pathname==='/api/conversations' && (!opts||!opts.method||opts.method==='GET'))return reply([{id:5,title:'Songs'}]);
 if(u.pathname==='/api/conversations/5')return reply({id:5,title:'Songs',messages:[
   {id:1,role:'user',content:'musicgeni lofi beat about rain'},
   {id:2,role:'assistant',content:'Generated song for: lofi beat about rain\n\n!video[song](%(song_mp4)s)'}]});
 if(u.pathname==='%(song_mp4)s')return Promise.resolve(new Response(new Blob([MP4],{type:'video/mp4'}),{status:200}));
 if(u.pathname==='%(song_mp3)s')return Promise.resolve(new Response(new Blob([MP3],{type:'audio/mpeg'}),{status:200}));
 return orig(url,opts);};
// The chat socket: opens, records what is sent, and answers `extractaudio` the way the server does --
// a `files` result normalized to text with an `!audio[<name>.mp3](...)` link.
const RealWS=window.WebSocket;
window.WebSocket=function(url,p){
  if(!String(url).includes('/api/ws/chat/')) return new RealWS(url,p);
  const ws={readyState:0,url:String(url),close(){this.readyState=3;},
    send(s){const m=JSON.parse(s); __sent.push(m);
      if(m.content==='extractaudio'&&m.videos&&m.videos.length){
        const name=m.videos[0].filename.replace(/\.[^.]+$/,'')+'.mp3';
        setTimeout(()=>ws.onmessage&&ws.onmessage({data:JSON.stringify({type:'response',data:{type:'text',
          content:'## 🎵 Extract audio\n\n🎵 '+m.videos[0].filename+' → 1 KB\n\n!audio['+name+'](%(song_mp3)s)'}})}),20);
      }}};
  setTimeout(()=>{ws.readyState=1; ws.onopen&&ws.onopen();},10);
  window.__chatWS=ws; return ws;};
window.WebSocket.OPEN=1;
__PC.ensureAiSession=async()=>({can_ai:true});
''' % {'mp4': base64.b64encode(MP4).decode(), 'mp3': base64.b64encode(MP3).decode(),
       'song_mp4': SONG_MP4, 'song_mp3': SONG_MP3}

# The APK: Capacitor with MediaSave (Android 10+). Installed only for the Download press, so the boot
# above is the ordinary one.
NATIVE = r'''window.Capacitor={isNativePlatform:()=>true,Plugins:{
  MediaSave:{available:async()=>({ok:true}),save:async o=>{__saved.push({name:o.name,mime:o.mime,
     bytes:atob(o.data||'').length,head:atob(o.data||'').slice(0,3)});return{ok:true,where:'Download/PosterChan/'+o.name};}},
  Filesystem:{writeFile:async o=>({uri:'file:///cache/'+o.path})},
  Share:{share:async o=>{__shares.push(o);}}}};'''


async def _convert_and_save(b, button_sel, msg_index):
    await b.js(f"document.querySelectorAll('#ai-msgs .ai-msg.assistant')[{msg_index}].querySelector('{button_sel}').click()")
    await b.until("__sent.some(m=>m.content==='extractaudio')")
    sent = await b.js("(()=>{const m=__sent.filter(m=>m.content==='extractaudio').pop();"
                      "return {name:m.videos[0].filename, b64:m.videos[0].base64, n:__sent.filter(m=>m.content==='extractaudio').length}})()")
    assert base64.b64decode(sent['b64']) == MP4, 'Convert to MP3 did not send the song\'s own MP4'
    assert sent['name'].endswith('.mp4') and sent['name'] != 'song.mp4', \
        ('every converted song comes back as song.mp3', sent['name'])
    # The MP3 arrives as its own message with a Download button.
    await b.until("[...document.querySelectorAll('#ai-msgs .ai-msg.assistant audio')].some(a=>a.src.includes('enc_bbbb'))")
    await b.js(NATIVE)
    await b.js("(()=>{const a=[...document.querySelectorAll('#ai-msgs .ai-msg.assistant')].filter(m=>m.querySelector('audio')).pop();"
               "a.querySelector('.ai-dlfile').click()})()")
    await b.until("__saved.length>0")
    st = await b.js("({saved:__saved, shares:__shares.length})")
    await b.js("delete window.Capacitor; __saved.length=0; __sent.length=0;")
    return sent, st


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
@pytest.mark.parametrize('width', [390, 1280])
def test_a_generated_song_converts_to_an_mp3_that_saves_to_the_phone(width):
    async def check(b):
        await b.call('Emulation.setDeviceMetricsOverride', dict(width=width, height=900, deviceScaleFactor=1, mobile=width < 600))
        await desktop.login(b)
        await b.js(STUB)
        await b.js("__PC.switchView('ai')")
        # Reload form: the persisted `!video[song](...)`.
        await b.until("!!document.querySelector('#ai-msgs .ai-mp3file')")
        sent, st = await _convert_and_save(b, '.ai-mp3file', 0)
        saved = st['saved'][0]
        assert saved['mime'] == 'audio/mpeg' and saved['head'] == 'ID3' and saved['bytes'] == len(MP3), saved
        assert saved['name'].endswith('.mp3') and saved['name'].startswith('posterchan-song'), saved
        assert st['shares'] == 0, 'Download opened the share sheet instead of saving'
        # The button sits inside the bubble, not off the side of a phone screen.
        fits = await b.js("(()=>{const r=document.querySelector('#ai-msgs .ai-mp3file').getBoundingClientRect();"
                          "return r.width>0 && r.right<=window.innerWidth+1 && r.left>=-1})()")
        assert fits, 'Convert to MP3 is off screen'

        # "the links won't play / not authenticated in browser": in the APK a <video>/<audio> pointing at
        # the instance's /api/files/ cannot carry the session (the page is https://localhost), so it
        # errors. The last resort used to REPLACE the player with a link that opens the system browser,
        # which has no session either. It must fetch the bytes with the app's auth and keep playing.
        await b.js("window.__blobFallback(document.querySelector('#ai-msgs .ai-media video'))")
        await b.until("(()=>{const v=document.querySelector('#ai-msgs .ai-media video');return !!v&&v.src.startsWith('blob:')})()")
        await b.js("window.__blobFallback([...document.querySelectorAll('#ai-msgs .ai-media audio')].pop())")
        await b.until("(()=>{const a=[...document.querySelectorAll('#ai-msgs .ai-media audio')].pop();return !!a&&a.src.startsWith('blob:')})()")
        links = await b.js("[...document.querySelectorAll('#ai-msgs .ai-media a[target=_blank]')].length")
        assert links == 0, 'a failed song player became a link to the system browser'
        # The MP3 player gets the same self-healing load the video always had.
        hooked = await b.js("(()=>{const a=[...document.querySelectorAll('#ai-msgs .ai-media audio')].pop();"
                            "return (a.getAttribute('onerror')||'').includes('__aiMediaRetry')})()")
        assert hooked, 'the MP3 player has no retry when it fails to load'

        # Live form: a `generated_video` payload the server pushes as the song finishes.
        await b.js("__chatWS.onmessage({data:JSON.stringify({type:'response',data:{type:'generated_video',"
                   "content:'Generated song for: rain',video:'%s',format:'mp4',has_audio:true}})})"
                   % base64.b64encode(MP4).decode())
        await b.until("!!document.querySelector('#ai-msgs .ai-mp3-fx')")
        n = await b.js("[...document.querySelectorAll('#ai-msgs .ai-msg.assistant')].findIndex(m=>m.querySelector('.ai-mp3-fx'))")
        sent, st = await _convert_and_save(b, '.ai-mp3-fx', n)
        saved = st['saved'][0]
        assert saved['mime'] == 'audio/mpeg' and saved['head'] == 'ID3', saved
        assert saved['name'].startswith('posterchan-song'), saved
        assert st['shares'] == 0
    asyncio.run(desktop.with_browser('online', '', check, PLAIN))
