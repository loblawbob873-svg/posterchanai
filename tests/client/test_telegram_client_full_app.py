"""The Telegram client in the real bundled client — sign in, chats, attachments, live messages, layouts.

Asked for: a Telegram client inside the web UI / PosterChanOS, "sign in once", "cyberpunky" and
matching PosterChan, notifications, attachments, camera — on computers and TABLETS, not phones (a
phone already runs Telegram). The server is stubbed with the same API the real router answers
(tests/test_telegram_client.py covers that half); everything the person sees is the shipped view.
"""
import asyncio
import json
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop

FAKE = r"""
window.__tg = { posts:[], files:[], state:'none', notes:[] };
const __tgMsgs = [
  {id:1, chat_id:42, out:false, date:1700000001, text:'hey there', sender:'Alice', sender_id:7, reply_to:0, media:null},
  {id:2, chat_id:42, out:false, date:1700000002, text:'', sender:'Alice', sender_id:7, reply_to:0, media:{kind:'photo', name:'', size:1234, mime:'image/jpeg'}},
  {id:3, chat_id:42, out:true, date:1700000003, text:'the doc', sender:'', sender_id:1, reply_to:1, media:{kind:'file', name:'notes.pdf', size:52000, mime:'application/pdf'}}];
const __j = (o, s=200) => Promise.resolve(new Response(JSON.stringify(o), {status:s, headers:{'Content-Type':'application/json'}}));
const __f0 = window.fetch;
window.fetch = function(url, opts){
  const u = String(url);
  if(!u.includes('/api/tgc/')) return __f0(url, opts);
  const path = u.replace(/^.*\/api\/tgc/, ''), body = opts && opts.body;
  if(path.startsWith('/status')) return __j({configured:true, state:__tg.state, me:{name:'Me'}});
  if(path.startsWith('/login/phone')){ __tg.state='code'; return __j({state:'code'}); }
  if(path.startsWith('/login/code')){ __tg.state='password'; return __j({state:'password'}); }
  if(path.startsWith('/login/password')){ __tg.state='ready'; return __j({state:'ready', me:{name:'Me'}}); }
  if(path.startsWith('/ticket')) return __j({t:'TICKET', ttl:21600});
  if(path.startsWith('/dialogs')) return __j({ok:true, dialogs:[
    {id:42, title:'Alice', kind:'user', unread:2, last:{text:'the doc', date:1700000003, out:true}},
    {id:77, title:'Night City Crew', kind:'group', unread:0, last:{text:'yo', date:1699999999, out:false}}]});
  if(path.startsWith('/messages/')) return __j({ok:true, messages: path.includes('before=') ? [] : __tgMsgs.slice()});
  if(path.startsWith('/read')) return __j({ok:true});
  if(path.startsWith('/send-file')){
    const fd = body, f = fd.get('file');
    __tg.files.push({chat:fd.get('chat_id'), name:f.name, size:f.size, caption:fd.get('caption'), mode:fd.get('mode')});
    return __j({ok:true, message:{id:100+__tg.files.length, chat_id:42, out:true, date:1700000100, text:fd.get('caption')||'', sender:'', media:{kind:'file', name:f.name, size:f.size}}});
  }
  if(path.startsWith('/send')){ const b = JSON.parse(body); __tg.posts.push(b);
    return __j({ok:true, message:{id:200, chat_id:b.chat_id, out:true, date:1700000200, text:b.text, sender:'', media:null}}); }
  return __j({ok:false, error:'unexpected '+path}, 404);
};
// The live socket.
window.__tgSockets = [];
const __WS = window.WebSocket;
window.WebSocket = function(url){ if(!String(url).includes('/api/tgc/ws')) return new __WS(url);
  const s = { url, readyState:1, sent:[], send(x){ this.sent.push(x); }, close(){} };
  window.__tgSockets.push(s); setTimeout(() => s.onopen && s.onopen(), 0); return s; };
"""

LAYOUT = r"""(()=>{const r=e=>e&&e.getBoundingClientRect();const W=innerWidth;
  // The app scales itself by window width (body zoom .67/.72/.77/1), so sizes are compared at that scale.
  const z=parseFloat(getComputedStyle(document.body).zoom)||1;
  const vis=e=>!!e&&r(e).width>0&&r(e).height>0&&getComputedStyle(e).display!=='none';
  return {overflow:document.documentElement.scrollWidth>W+1, side:vis(document.querySelector('.tg-side')),
          chat:vis(document.querySelector('.tg-chat')), back:vis(document.querySelector('.tg-back')),
          composer:vis(document.querySelector('.tg-composer')),
          send:(()=>{const b=document.querySelector('.tg-send');return !!b&&r(b).right<=W+1&&r(b).width>=40*z})()}})()"""


@pytest.fixture(scope="module", autouse=True)
def bundle():
    yield from desktop.bundle.__wrapped__()


async def _open(b):
    await desktop.login(b)
    await b.js("try{ if(window.PCOS && PCOS.isOn()) PCOS.exit(); }catch(_){}")
    await b.js("__PC.switchView('tg')")


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
def test_sign_in_chat_attach_and_live_notifications_on_a_computer():
    async def check(b):
        await _open(b)
        # --- sign in: phone → code → 2FA password ---------------------------------------------------
        for value in ("+15550104477", "12345", "hunter2"):
            await b.until("!!document.querySelector('.tg-login input')")
            await b.js(f"""(()=>{{const f=document.querySelector('.tg-login form');f.v.value={json.dumps(value)};
                               f.requestSubmit();}})()""")
            await asyncio.sleep(.3)
        await b.until("document.querySelectorAll('.tg-dialog').length===2")
        assert "Night City Crew" in await b.js("document.querySelector('.tg-dialogs').textContent")
        assert await b.js("document.querySelector('.tg-dialog .tg-badge').textContent") == "2"
        # --- open a chat: text, a photo, a file -------------------------------------------------------
        await b.js("document.querySelector('.tg-dialog[data-chat=\"42\"]').click()")
        await b.until("document.querySelectorAll('.tg-msg').length===3")
        got = await b.js("""({photo:(document.querySelector('.tg-msg img')||{}).getAttribute&&document.querySelector('.tg-msg img').getAttribute('src'),
            file:(document.querySelector('.tg-file-chip')||{}).textContent||'', quote:!!document.querySelector('.tg-quote'),
            out:document.querySelectorAll('.tg-msg.out').length})""")
        assert "/api/tgc/media/42/2?thumb=1&t=TICKET" in got["photo"], got
        assert "notes.pdf" in got["file"] and got["quote"] and got["out"] == 1, got
        # --- attachments: two files, one caption -------------------------------------------------------
        await b.js("""(()=>{const dt=new DataTransfer();
            dt.items.add(new File([new Uint8Array([137,80,78,71])],'shot.png',{type:'image/png'}));
            dt.items.add(new File(['%PDF-1.4'],'report.pdf',{type:'application/pdf'}));
            const i=document.querySelector('.tg-file');i.files=dt.files;i.dispatchEvent(new Event('change'));})()""")
        assert await b.js("document.querySelectorAll('.tg-pending .tg-chip').length") == 2
        await b.js("document.querySelector('.tg-text').value='both files'; document.querySelector('.tg-send').click()")
        await b.until("__tg.files.length===2")
        files = await b.js("__tg.files")
        assert [f["name"] for f in files] == ["shot.png", "report.pdf"]
        assert files[0]["size"] == 4 and files[0]["chat"] == "42"
        assert [f["caption"] for f in files] == ["", "both files"], "the caption belongs on the last file only"
        assert await b.js("document.querySelectorAll('.tg-pending .tg-chip').length") == 0
        # --- text, by Enter --------------------------------------------------------------------------------
        await b.js("(()=>{const t=document.querySelector('.tg-text');t.value='hello';"
                   "t.dispatchEvent(new KeyboardEvent('keydown',{key:'Enter',bubbles:true}))})()")
        await b.until("__tg.posts.length===1")
        assert await b.js("__tg.posts[0]") == {"chat_id": 42, "text": "hello", "reply_to": 0}
        # --- the camera control is there ----------------------------------------------------------------------
        assert await b.js("!!document.querySelector('.tg-composer [data-act=\"camera\"]')")
        # --- a live message in ANOTHER chat: a notification, and the chat jumps up with a badge -----------
        await b.js("window.__notes=[]; __PC.osNotify=(t,body,o)=>__notes.push({t,body,tag:o&&o.tag});")
        await b.until("__tgSockets.length>=1 && __tgSockets[__tgSockets.length-1].sent.length>=1")
        assert "token" in await b.js("__tgSockets[__tgSockets.length-1].sent[0]"), "the socket must authenticate in its first frame"
        await b.js("""__tgSockets[__tgSockets.length-1].onmessage({data:JSON.stringify({type:'message',
            chat:{id:77,title:'Night City Crew'}, message:{id:9,chat_id:77,out:false,date:1700000300,text:'wake up samurai',sender:'V',media:null}})})""")
        notes = await b.js("__notes")
        assert notes and notes[0]["t"] == "Telegram · Night City Crew" and notes[0]["body"] == "wake up samurai", notes
        assert await b.js("document.querySelector('.tg-dialog').dataset.chat") == "77"
        assert await b.js("document.querySelector('.tg-dialog[data-chat=\"77\"] .tg-badge').textContent") == "1"
        lay = await b.js(LAYOUT)
        assert lay["side"] and lay["chat"] and lay["composer"] and lay["send"] and not lay["overflow"], lay

    asyncio.run(desktop.with_browser("online", "", check, extra_init=FAKE.replace("state:'none'", "state:'none'")))


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
@pytest.mark.parametrize("w,h", [(820, 1180), (1180, 820)], ids=["tablet-portrait", "tablet-landscape"])
def test_a_tablet_gets_the_client(w, h):
    async def check(b):
        await b.call("Emulation.setDeviceMetricsOverride", {"width": w, "height": h, "deviceScaleFactor": 2,
                                                            "mobile": True, "screenWidth": w, "screenHeight": h})
        await _open(b)
        await b.until("document.querySelectorAll('.tg-dialog').length===2")
        await b.js("document.querySelector('.tg-dialog[data-chat=\"42\"]').click()")
        await b.until("document.querySelectorAll('.tg-msg').length===3")
        lay = await b.js(LAYOUT)
        assert not lay["overflow"] and lay["chat"] and lay["composer"] and lay["send"], json.dumps(lay)
        if w < 860:
            assert lay["back"] and not lay["side"], "portrait tablet: one pane at a time, with a way back"
            await b.js("document.querySelector('.tg-back').click()")
            assert (await b.js(LAYOUT))["side"]

    asyncio.run(desktop.with_browser("online", "", check, extra_init=FAKE.replace("state:'none'", "state:'ready'")))


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
def test_a_phone_does_not_get_it():
    async def check(b):
        await b.call("Emulation.setDeviceMetricsOverride", {"width": 390, "height": 844, "deviceScaleFactor": 3,
                                                            "mobile": True, "screenWidth": 390, "screenHeight": 844})
        await b.call("Page.reload", {})                    # start as a phone starts, not resized into one
        await b.until("!!window.__PC && !!window.PCOS")
        await desktop.login(b)
        hidden = await b.js("""[...document.querySelectorAll('.nav-item[data-view="tg"]')].every(e=>getComputedStyle(e).display==='none')""")
        assert hidden, "the Telegram entry is offered on a phone"
        await b.js("__PC.switchView('tg')")
        await b.until("!!document.querySelector('.tg-app')")
        assert "lives on your phone" in await b.js("document.querySelector('.tg-app').textContent")
        assert await b.js("__tgSockets.length") == 0, "a phone opened a Telegram socket"

    asyncio.run(desktop.with_browser("online", "", check, extra_init=FAKE.replace("state:'none'", "state:'ready'")))
