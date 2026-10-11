"""A saved voice that is a big phone video speaks a one-word line, and the video is cut ONCE, never re-sent.

Reported on the Android APK: a member's voice was a phone video; every line uploaded that whole video as
the reference, and the server refused anything over 16 MB as "that reference clip is too long" -- so a
one-word line failed. The server side is tests/test_voice_reference_size.py. This drives the SHIPPED
ai.js voice studio (phone and desktop width) against stubbed endpoints and asserts what goes over the
wire:

  * an EXISTING voice whose sample is a 20 MB video: the first line sends the video to
    /client/voice/reference ONCE, speaks with the small WAV that comes back, and remembers it on the
    voice (`ref`); the second line never fetches or uploads the video again;
  * a NEW voice uploaded from the device: the video is normalised first and only the small WAV is put
    on the drive; the voice is saved pointing at it.
"""
import asyncio
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop


@pytest.fixture(scope='module', autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


BIG = 20 * 1024 * 1024
PLAIN = "localStorage.setItem('pc_nostr_settings',JSON.stringify({...JSON.parse(localStorage.getItem('pc_nostr_settings')||'{}'),osMode:false}));"

# Swap ai.js's network-facing deps (upload, publish, proof, prompt) as its factory is installed, so the
# studio runs unmodified against fixtures. Everything else it uses is the real app.
INIT = PLAIN + r'''
window.__vs={uploads:[],published:[],toasts:[]};
(function(){let real;Object.defineProperty(window,'PCAiFactory',{configurable:true,
  get(){return real;},
  set(f){real=function(dep){const d=Object.assign({},dep);
    d.uploadBlob=async(file)=>{const sha=(window.__vs.uploads.length+1).toString(16).padStart(64,'a');
      window.__vs.uploads.push({name:file.name,type:file.type,size:file.size});
      window.__vs.files['https://drive.test/'+sha+'.wav']=file;return 'https://drive.test/'+sha+'.wav';};
    d.publish=async(kind,content,tags)=>{window.__vs.published.push({kind,content,tags});window.__vs.doc=content;return {id:'e'.repeat(64)};};
    d.selfProof=async()=>'proof';
    d.uiPrompt=async()=>'gran';
    const t=d.toast;d.toast=(m)=>{window.__vs.toasts.push(String(m));try{t(m);}catch(_){}}
    return f(d);};}});})();
'''

STUB = r'''(()=>{const V=window.__vs;
V.files={'https://drive.test/video.mp4':new Blob([new Uint8Array(%d)],{type:'video/mp4'})};
V.fetched={};V.refCalls=[];V.speaks=[];
V.doc=JSON.stringify({voices:[{id:'v1',name:'Vyram',url:'https://drive.test/video.mp4',created:1}]});
const wav=()=>{const b=new Uint8Array(44+48000*2);b.set([82,73,70,70],0);return new Blob([b],{type:'audio/wav'});};
Relay.ready=async()=>true;
const q=Relay.query;Relay.query=async(f)=>{const x=(f||[])[0]||{};
  if((x['#d']||[]).includes('pcai:voices'))return [{kind:30078,created_at:2,pubkey:__PC.me().pubkey,content:V.doc,tags:[['d','pcai:voices']]}];
  return q.apply(Relay,arguments);};
const orig=window.fetch;
window.fetch=async(url,opts)=>{const s=String(url);const u=new URL(s,location.href);
  const json=(v,st=200)=>new Response(JSON.stringify(v),{status:st,headers:{'Content-Type':'application/json'}});
  if(V.files[s]){V.fetched[s]=(V.fetched[s]||0)+1;return new Response(V.files[s],{status:200});}
  if(u.pathname==='/client/voice/status')return json({enabled:true,installed:true,downloaded:true,ready:true,queue:0,max_chars:800,max_ref_seconds:30});
  if(u.pathname==='/client/voice/reference'){const r=opts.body.get('reference');V.refCalls.push({size:r.size,type:r.type});
    return new Response(wav(),{status:200,headers:{'Content-Type':'audio/wav','X-Ref-Seconds':'2.00'}});}
  if(u.pathname==='/client/voice/speak'){const r=opts.body.get('reference');
    V.speaks.push({size:r.size,type:r.type,text:opts.body.get('text')});return new Response(wav(),{status:200,headers:{'Content-Type':'audio/wav'}});}
  return orig(url,opts);};
})()''' % BIG


async def _studio(b):
    await b.js("__PC.openVoiceStudio()")
    await b.until("!!document.querySelector('#vs-go') && !!document.querySelector('.vs-pick')")


async def _speak(b, text, n):
    await b.js("(()=>{const t=document.querySelector('#vs-text');t.value=%r;document.querySelector('#vs-go').click()})()" % text)
    await b.until("__vs.speaks.length===%d && !document.querySelector('#vs-go').disabled" % n)


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
@pytest.mark.parametrize('width', [390, 1280])
def test_a_big_video_voice_is_cut_once_and_speaks_a_one_word_line(width):
    async def check(b):
        await b.call('Emulation.setDeviceMetricsOverride', dict(width=width, height=900, deviceScaleFactor=1, mobile=width < 600))
        await desktop.login(b)
        await b.js(STUB)
        await _studio(b)

        await _speak(b, 'hi', 1)
        st = await b.js("({ref:__vs.refCalls,speaks:__vs.speaks,fetched:__vs.fetched,toasts:__vs.toasts})")
        assert st['ref'] == [{'size': BIG, 'type': 'video/mp4'}], st
        assert st['speaks'][0]['text'] == 'hi', st
        assert st['speaks'][0]['size'] < 2 * 1024 * 1024 and st['speaks'][0]['type'] == 'audio/wav', \
            ('the line must carry the small normalised WAV, not the 20 MB video', st)
        # …and the voice now remembers its small copy, on the drive and in the library.
        await b.until("__vs.published.some(p=>p.content.includes('\"ref\"'))")
        saved = await b.js("JSON.parse(__vs.doc).voices[0]")
        assert saved['url'] == 'https://drive.test/video.mp4', ('the original is kept', saved)
        assert saved['ref'].startswith('https://drive.test/') and saved['ref'] != saved['url'], saved
        assert all(u['size'] < 2 * 1024 * 1024 for u in await b.js("__vs.uploads")), 'the video was never re-uploaded'

        await _speak(b, 'yo', 2)
        st = await b.js("({ref:__vs.refCalls.length,speaks:__vs.speaks,video:__vs.fetched['https://drive.test/video.mp4']})")
        assert st['ref'] == 1 and st['video'] == 1, ('the big video was fetched/normalised again', st)
        assert st['speaks'][1]['size'] < 2 * 1024 * 1024 and st['speaks'][1]['text'] == 'yo', st
    asyncio.run(desktop.with_browser('online', '', check, INIT))


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
@pytest.mark.parametrize('width', [390])
def test_a_new_video_voice_puts_only_the_small_wav_on_the_drive(width):
    async def check(b):
        await b.call('Emulation.setDeviceMetricsOverride', dict(width=width, height=900, deviceScaleFactor=1, mobile=True))
        await desktop.login(b)
        await b.js(STUB)
        # The Upload button builds a file <input> and clicks it; answer that with a 20 MB phone video.
        await b.js("""(()=>{const c=HTMLInputElement.prototype.click;HTMLInputElement.prototype.click=function(){
          if(this.type!=='file')return c.call(this);const dt=new DataTransfer();
          dt.items.add(new File([new Uint8Array(%d)],'clip.mp4',{type:'video/mp4'}));this.files=dt.files;
          setTimeout(()=>this.onchange&&this.onchange(),0);};})()""" % BIG)
        await _studio(b)
        await b.js("document.querySelector('#vs-up').click()")
        await b.until("JSON.parse(__vs.doc).voices.length===2")
        st = await b.js("({ref:__vs.refCalls,uploads:__vs.uploads,voice:JSON.parse(__vs.doc).voices[1]})")
        assert st['ref'] == [{'size': BIG, 'type': 'video/mp4'}], st
        assert st['uploads'] and all(u['size'] < 2 * 1024 * 1024 and u['type'] == 'audio/wav' for u in st['uploads']), \
            ('only the normalised WAV goes on the drive', st)
        assert st['voice']['name'] == 'gran' and st['voice']['ref'] and st['voice']['url'] == st['voice']['ref'], st
    asyncio.run(desktop.with_browser('online', '', check, INIT))


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
def test_an_over_cap_clip_says_its_size_and_spends_no_gpu():
    async def check(b):
        await b.call('Emulation.setDeviceMetricsOverride', dict(width=390, height=900, deviceScaleFactor=1, mobile=True))
        await desktop.login(b)
        await b.js(STUB)
        await b.js("""(()=>{const f=window.fetch;window.fetch=async(url,opts)=>{
          if(new URL(String(url),location.href).pathname==='/client/voice/reference')
            return new Response(JSON.stringify({detail:'that file is over 200 MB — a voice only needs a few seconds of speech, so trim it or pick a smaller clip'}),{status:413,headers:{'Content-Type':'application/json'}});
          return f(url,opts);};})()""")
        await _studio(b)
        await b.js("(()=>{document.querySelector('#vs-text').value='hi';document.querySelector('#vs-go').click()})()")
        await b.until("__vs.toasts.some(t=>t.includes('over 200 MB'))")
        st = await b.js("({speaks:__vs.speaks.length,toasts:__vs.toasts})")
        assert st['speaks'] == 0, ('a refused clip must not be sent to the GPU', st)
        assert not any('too long' in t for t in st['toasts']), st
    asyncio.run(desktop.with_browser('online', '', check, INIT))
