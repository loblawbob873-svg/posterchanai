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
  {id:1, chat_id:42, out:false, date:1700000001, text:'hey there', sender:'Alice', sender_id:7, reply_to:0, media:null, reactions:[{emoji:'👍', count:2, mine:false}]},
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
  if(path.startsWith('/react')){ const b = JSON.parse(body); __tg.reacts = (__tg.reacts||[]).concat([b]);
    if(b.emoji === '🚫') return __j({ok:false, error:'This chat does not allow that reaction.'}, 400);
    const mine = (__tg.mine||{})[b.msg_id] === b.emoji; __tg.mine = Object.assign({}, __tg.mine, {[b.msg_id]: mine ? null : b.emoji});
    const base = b.msg_id === 1 ? [{emoji:'👍', count:2, mine:false}] : [];
    const now = __tg.mine[b.msg_id]; if(now){ const r = base.find(x => x.emoji === now); if(r){ r.count++; r.mine = true; } else base.push({emoji:now, count:1, mine:true}); }
    return __j({ok:true, reactions:base}); }
  if(path.startsWith('/search')) return __j({ok:true, results:[
    {id:501, title:'Bob Stranger', kind:'user', known:false, username:'bobby', members:0},
    {id:-1000000000777, title:'Bob Fans', kind:'group', known:false, username:'', members:40}]});
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


BUTTON = r"""(sel=>{const b=document.querySelector(sel); if(!b) return null;
  const cs=getComputedStyle(b), rgb=s=>(s.match(/[\d.]+/g)||[]).map(Number);
  const [r,g,bl,a=1]=rgb(cs.backgroundColor), [fr,fg,fb]=rgb(cs.color);
  const L=(x,y,z)=>{const f=v=>{v/=255;return v<=.03928?v/12.92:((v+.055)/1.055)**2.4};return .2126*f(x)+.7152*f(y)+.0722*f(z)};
  const l1=L(r,g,bl), l2=L(fr,fg,fb);
  return {bg:cs.backgroundColor, fg:cs.color, alpha:a, contrast:(Math.max(l1,l2)+.05)/(Math.min(l1,l2)+.05)}})"""


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
def test_the_accent_buttons_are_visible_in_every_theme():
    """Reported: "the telegram button is all black", then "the telegram send button is also black".
    Every .tg-* accent was `var(--accent)`, which no theme defined, so "Send code", the chat's Send
    and the unread badge were transparent boxes of near-black text on a dark panel."""
    async def check(b):
        await _open(b)
        await b.until("!!document.querySelector('.tg-primary')")
        themes = await b.js("[''].concat([...new Set([...document.styleSheets].flatMap(s=>{try{return [...s.cssRules]}catch(_){return []}})"
                            ".map(r=>(r.selectorText||'').match(/^:root\\[data-theme=\"([\\w-]+)\"\\]$/)).filter(Boolean).map(m=>m[1]))])")
        assert len(themes) > 3, themes

        async def every_theme(sel):
            for t in themes:
                await b.js(f"document.documentElement.setAttribute('data-theme', {json.dumps(t)}) || (!{json.dumps(t)} && document.documentElement.removeAttribute('data-theme'))")
                got = await b.js(f"({BUTTON})({json.dumps(sel)})")
                assert got, f"{sel} is not on screen"
                assert got["alpha"] > .9, f"theme {t or 'default'}: {sel} has no background ({got})"
                assert got["contrast"] >= 3, f"theme {t or 'default'}: {sel}'s label is unreadable ({got})"

        await every_theme(".tg-primary")                          # Send code
        await b.js("__tg.state='ready'; __PC.switchView('global'); __PC.switchView('tg')")
        await b.until("document.querySelectorAll('.tg-dialog').length===2")
        await every_theme(".tg-dialog .tg-badge")                 # unread count
        await b.js("document.querySelector('.tg-dialog[data-chat=\"42\"]').click()")
        await b.until("!!document.querySelector('.tg-send')")
        await every_theme(".tg-send")                             # the chat's Send

    asyncio.run(desktop.with_browser("online", "", check, FAKE))


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
def test_on_posterchanos_it_opens_as_a_column_and_fits_its_window():
    """Reported: "on posterchanOS, the window opens big and the Telegram UI does not fit well". It took
    the reading-column default, and its one-pane layout was a VIEWPORT query — on a wide monitor it
    never saw its window get narrow, so the chat list and the chat stayed side by side and the chat was
    squeezed to a sliver."""
    async def check(b):
        await b.call("Emulation.setDeviceMetricsOverride", dict(width=2560, height=1440, deviceScaleFactor=1, mobile=False))
        await desktop.login(b)
        await b.until("!!window.PCOS && PCOS.isOn() && !!document.querySelector('#os-desk')")
        await b.js("document.getElementById('osfr')?.remove();document.documentElement.classList.remove('osfr-on')")
        opened = await b.js("(()=>{const a=PCOS.__place(0,'tg'), r=PCOS.__place(0,'global');return {tg:a.w, reading:r.w}})()")
        assert opened["tg"] <= 1080 and opened["tg"] < opened["reading"], f"Telegram opens as a reading column: {opened}"
        await b.js("__tg.state='ready'; __PC.switchView('tg')")
        await b.until("!!document.querySelector('.osw .tg-shell') && document.querySelectorAll('.osw .tg-dialog').length===2")
        await b.js("document.querySelector('.tg-app').closest('.osw').style.width='560px'")
        await asyncio.sleep(.2)
        vis = "(s=>{const e=document.querySelector('.osw '+s);return !!e&&e.getBoundingClientRect().width>0&&getComputedStyle(e).display!=='none'})"
        assert await b.js(f"{vis}('.tg-side')") and not await b.js(f"{vis}('.tg-chat')"), \
            "a narrow window still shows the list and the chat side by side"
        await b.js("document.querySelector('.osw .tg-dialog[data-chat=\"42\"]').click()")
        await b.until(f"{vis}('.tg-chat')")
        assert not await b.js(f"{vis}('.tg-side')"), "the open chat does not get the window to itself"
        await b.js("document.querySelector('.tg-app').closest('.osw').style.width='1100px'")
        await asyncio.sleep(.2)
        assert await b.js(f"{vis}('.tg-side')") and await b.js(f"{vis}('.tg-chat')"), "a wide window lost its two panes"

    asyncio.run(desktop.with_browser("online", "", check, FAKE))


REACTS = r"""(()=>[...document.querySelectorAll('.tg-msg[data-id="1"] .tg-react')].map(b=>({e:b.dataset.emoji,n:b.querySelector('small').textContent,mine:b.classList.contains('mine')})))()"""


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
def test_reactions_show_toggle_and_explain_a_refusal():
    """Reported: "Telegram: missing emoji reacts"."""
    async def check(b):
        await _open(b)
        await b.js("__tg.state='ready'; __PC.switchView('global'); __PC.switchView('tg')")
        await b.until("document.querySelectorAll('.tg-dialog').length===2")
        await b.js("document.querySelector('.tg-dialog[data-chat=\"42\"]').click()")
        await b.until("!!document.querySelector('.tg-msg[data-id=\"1\"] .tg-react')")
        assert await b.js(REACTS) == [{"e": "👍", "n": "2", "mine": False}]
        await b.js("document.querySelector('.tg-msg[data-id=\"1\"] .tg-react').click()")
        await b.until("!!document.querySelector('.tg-msg[data-id=\"1\"] .tg-react.mine')")
        assert await b.js(REACTS) == [{"e": "👍", "n": "3", "mine": True}]
        # The picker adds a new one.
        await b.js("document.querySelector('.tg-msg[data-id=\"1\"] [data-react-pick]').click()")
        await b.until("!!document.querySelector('.tg-react-pop')")
        await b.js("[...document.querySelectorAll('.tg-react-pop [data-e]')].find(x=>x.dataset.e==='🔥').click()")
        await b.until("__tg.reacts.length===2 && !!document.querySelector('.tg-msg[data-id=\"1\"] .tg-react.mine[data-emoji=\"🔥\"]')")
        assert not await b.js("!!document.querySelector('.tg-react-pop')"), "the picker stayed open"
        # A refusal puts the tally back and says why.
        before = await b.js(REACTS)
        await b.js("PCTelegram.react(1,'🚫')")
        await asyncio.sleep(.3)
        assert await b.js(REACTS) == before, "a refused reaction left a wrong count on screen"

    asyncio.run(desktop.with_browser("online", "", check, FAKE))


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
def test_search_finds_people_and_rooms_on_telegram_and_opens_them():
    """Reported: "Telegram: need user and room search" — the box only filtered chats already loaded."""
    async def check(b):
        await _open(b)
        await b.js("__tg.state='ready'; __PC.switchView('global'); __PC.switchView('tg')")
        await b.until("document.querySelectorAll('.tg-dialog').length===2")
        await b.js("const s=document.querySelector('.tg-search');s.value='bob';s.dispatchEvent(new Event('input'))")
        await b.until("document.querySelectorAll('.tg-found [data-found]').length===2")
        text = await b.js("document.querySelector('.tg-found').textContent")
        assert "Bob Stranger" in text and "@bobby" in text and "Bob Fans" in text and "40 members" in text, text
        await b.js("document.querySelector('.tg-found [data-found=\"501\"]').click()")
        await b.until("!!document.querySelector('.tg-ctitle') && document.querySelector('.tg-ctitle').textContent==='Bob Stranger'")
        assert await b.js("document.querySelector('.tg-search').value") == "", "the search stayed filled after opening a result"

    asyncio.run(desktop.with_browser("online", "", check, FAKE))


CALLS = r"""
__tg.calls = [];
const __f1 = window.fetch;
window.fetch = function(url, opts){
  const u = String(url);
  if(u.includes('/api/tgc/status')) return __f1(url, opts).then(r => r.json()).then(j => new Response(JSON.stringify(Object.assign(j, {calls:true})), {status:200, headers:{'Content-Type':'application/json'}}));
  if(u.includes('/api/tgc/call')){ const b = JSON.parse(opts.body); __tg.calls.push(b);
    return Promise.resolve(new Response(JSON.stringify({ok:true, state: b.action === 'hangup' ? 'idle' : 'calling'}), {status:200, headers:{'Content-Type':'application/json'}})); }
  return __f1(url, opts);
};
// A camera and a microphone that exist in a headless browser: a painted canvas and an oscillator.
navigator.mediaDevices.getUserMedia = async (c) => {
  window.__gum = (window.__gum || 0) + 1;
  const out = new MediaStream();
  if(c.audio){ const ac = new AudioContext(), o = ac.createOscillator(), d = ac.createMediaStreamDestination(); o.connect(d); o.start(); d.stream.getAudioTracks().forEach(t => out.addTrack(t)); }
  if(c.video){ const cv = document.createElement('canvas'); cv.width = 640; cv.height = 360; const g = cv.getContext('2d');
    setInterval(() => { g.fillStyle = '#' + Math.floor(Math.random()*0xffffff).toString(16).padStart(6,'0'); g.fillRect(0,0,640,360); }, 50);
    cv.captureStream(15).getVideoTracks().forEach(t => out.addTrack(t)); }
  return out;
};
window.__media = [];
const __WS2 = window.WebSocket;
window.WebSocket = function(url){ if(!String(url).includes('/api/tgc/call-media')) return new __WS2(url);
  const s = { url, readyState:1, sent:[], bufferedAmount:0, send(x){ this.sent.push(typeof x === 'string' ? x : new Uint8Array(x.buffer ? x.buffer : x).slice()); }, close(){ this.readyState = 3; } };
  window.__media.push(s); setTimeout(() => s.onopen && s.onopen(), 0); return s; };
"""

TINY_JPEG = "/9j/4AAQSkZJRgABAQEASABIAAD/2wBDAP//////////////////////////////////////////////////////////////////////////////////////wgALCAABAAEBAREA/8QAFBABAAAAAAAAAAAAAAAAAAAAAP/aAAgBAQABPxA="


async def _click(b, sel):
    import json as _j
    at = await b.js(f"(()=>{{const e=document.querySelector({_j.dumps(sel)});const r=e.getBoundingClientRect();return [r.x+r.width/2,r.y+r.height/2]}})()")
    for t in ("mousePressed", "mouseReleased"):
        await b.call("Input.dispatchMouseEvent", dict(type=t, x=at[0], y=at[1], button="left", clickCount=1))


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
def test_a_voice_call_dials_streams_audio_shows_the_other_side_and_hangs_up():
    """Asked for: "and voice and video call support" in the Telegram client."""
    async def check(b):
        await _open(b)
        await b.js("__tg.state='ready'; __PC.switchView('global'); __PC.switchView('tg')")
        await b.until("document.querySelectorAll('.tg-dialog').length===2")
        await b.js("document.querySelector('.tg-dialog[data-chat=\"77\"]').click()")
        await b.until("!!document.querySelector('.tg-ctitle')")
        assert not await b.js("!!document.querySelector('[data-act=\"call\"]')"), "a GROUP offered a call"
        await b.js("document.querySelector('.tg-back').click(); document.querySelector('.tg-dialog[data-chat=\"42\"]').click()")
        await b.until("!!document.querySelector('[data-act=\"call\"]')")
        await _click(b, '[data-act="call"]')
        await b.until("__tg.calls.length===1 && !!document.querySelector('.tgc-panel')")
        assert await b.js("__tg.calls[0]") == {"action": "start", "peer": 42, "video": False}
        assert "Calling" in await b.js("document.querySelector('.tgc-status').textContent")
        await b.until("__media.length===1 && __media[0].sent.length>0")
        first = await b.js("__media[0].sent[0]")
        assert isinstance(first, str) and '"token"' in first, "the media socket did not authenticate in its FIRST frame"
        # The node says the other side answered.
        await b.js("__tgSockets[0].onmessage({data:JSON.stringify({type:'call',peer:42,title:'Alice',state:'active',video:false,remote_video:false})})")
        await b.until("__media[0].sent.filter(x=>typeof x!=='string'&&x[0]===1).length>=10")
        sizes = await b.js("[...new Set(__media[0].sent.filter(x=>typeof x!=='string'&&x[0]===1).map(x=>x.length))]")
        assert sizes == [961], f"microphone frames are not 10 ms of 48 kHz PCM16: {sizes}"
        assert await b.js("/^0:0\\d$/.test(document.querySelector('.tgc-status').textContent)"), "no call timer"
        # The other side's camera arrives.
        await b.js(f"(()=>{{const j=Uint8Array.from(atob('{TINY_JPEG}'),c=>c.charCodeAt(0));const m=new Uint8Array(j.length+1);m[0]=0x12;m.set(j,1);__media[0].onmessage({{data:m.buffer}});}})()")
        await b.until("!!document.querySelector('.tgc-remote') && document.querySelector('.tgc-remote').src.startsWith('blob:')")
        await _click(b, '[data-tgc="hangup"]')
        await b.until("__tg.calls.some(c=>c.action==='hangup')")
        assert not await b.js("!!document.querySelector('.tgc-panel')"), "the call screen stayed up after hanging up"
        assert await b.js("__media[0].readyState") == 3, "the media socket stayed open"

    asyncio.run(desktop.with_browser("online", "", check, FAKE + CALLS))


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
def test_an_incoming_video_call_rings_and_can_be_answered_or_declined():
    async def check(b):
        await _open(b)
        await b.js("__tg.state='ready'; __PC.switchView('global'); __PC.switchView('tg')")
        await b.until("document.querySelectorAll('.tg-dialog').length===2 && __tgSockets.length>0")
        ring = "__tgSockets[0].onmessage({data:JSON.stringify({type:'call',peer:42,title:'Alice',state:'ringing',video:false,remote_video:true})})"
        await b.js(ring)
        await b.until("!!document.querySelector('.tgc-panel.tgc-ringing')")
        text = await b.js("document.querySelector('.tgc-panel').textContent")
        assert "Alice" in text and "Incoming video call" in text, text
        assert await b.js("!!document.querySelector('[data-tgc=\"accept-video\"]')"), "a video call offered no video answer"
        await _click(b, '[data-tgc="accept-video"]')
        await b.until("__tg.calls.some(c=>c.action==='accept')")
        assert await b.js("__tg.calls.find(c=>c.action==='accept')") == {"action": "accept", "peer": 42, "video": True}
        await b.js("__tgSockets[0].onmessage({data:JSON.stringify({type:'call',peer:42,title:'Alice',state:'active',video:true,remote_video:true})})")
        await b.until("__media.length>0 && __media.at(-1).sent.some(x=>typeof x!=='string'&&x[0]===2)")
        cam = await b.js("(()=>{const f=__media.at(-1).sent.find(x=>typeof x!=='string'&&x[0]===2);return [f[1],f[2]]})()")
        assert cam == [0xFF, 0xD8], "camera frames are not JPEG"
        await b.js("__tgSockets[0].onmessage({data:JSON.stringify({type:'call',peer:42,state:'idle',reason:'hung up'})})")
        await b.until("!document.querySelector('.tgc-panel')")
        # A second ring, declined.
        await b.js(ring)
        await b.until("!!document.querySelector('.tgc-panel.tgc-ringing')")
        await _click(b, '[data-tgc="hangup"]')
        await b.until("__tg.calls.some(c=>c.action==='hangup')")
        assert not await b.js("!!document.querySelector('.tgc-panel')")

    asyncio.run(desktop.with_browser("online", "", check, FAKE + CALLS))


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
def test_sent_and_read_ticks_and_a_read_on_the_phone_clears_the_badge():
    """"missing delivered and read checkmarks, and if I read a message on my phone it should be marked
    read on PosterChan". Your messages carry ✓ once Telegram has them and ✓✓ once the other side read
    them — in the chat AND on the chat list — and a read somewhere else (Telegram's inbox update) takes
    the chat's badge to what Telegram says is still unread."""
    fake = (FAKE.replace("state:'none'", "state:'ready'")
                .replace("last:{text:'the doc', date:1700000003, out:true}", "last:{id:3, text:'the doc', date:1700000003, out:true}")
                .replace("unread:0, last:{text:'yo'", "unread:5, last:{text:'yo'"))

    async def ticks(b):
        return await b.js("""({msg:[...document.querySelectorAll('.tg-msg.out')].map(m=>{const t=m.querySelector('.tg-tick');
              return t?(t.classList.contains('read')?'read':'sent')+':'+t.textContent:'none'}),
            row:(()=>{const t=document.querySelector('.tg-dialog[data-chat="42"] .tg-tick');return t?(t.classList.contains('read')?'read':'sent'):'none'})(),
            badge77:(document.querySelector('.tg-dialog[data-chat="77"] .tg-badge')||{}).textContent||''})""")

    async def check(b):
        await _open(b)
        await b.until("document.querySelectorAll('.tg-dialog').length===2")
        assert (await ticks(b))["row"] == "sent", "your last message on the chat list has no tick"
        await b.js("document.querySelector('.tg-dialog[data-chat=\"42\"]').click()")
        await b.until("document.querySelectorAll('.tg-msg').length===3")
        got = await ticks(b)
        assert got["msg"] == ["sent:✓"] and got["badge77"] == "5", got
        # A message you send gets its tick too.
        await b.js("(()=>{const t=document.querySelector('.tg-text');t.value='hello';"
                   "t.dispatchEvent(new KeyboardEvent('keydown',{key:'Enter',bubbles:true}))})()")
        await b.until("document.querySelectorAll('.tg-msg.out').length===2")
        assert (await ticks(b))["msg"] == ["sent:✓", "sent:✓"]
        # Alice reads up to 3: that one turns ✓✓, the newer one (200) stays ✓.
        await b.until("__tgSockets.length>=1")
        await b.js("__tgSockets[__tgSockets.length-1].onmessage({data:JSON.stringify({type:'read',chat_id:42,max_id:3,outbox:true})})")
        got = await ticks(b)
        assert got["msg"] == ["read:✓✓", "sent:✓"], got
        # Then everything: the chat list follows your newest message.
        await b.js("__tgSockets[__tgSockets.length-1].onmessage({data:JSON.stringify({type:'read',chat_id:42,max_id:200,outbox:true})})")
        got = await ticks(b)
        assert got["msg"] == ["read:✓✓", "read:✓✓"] and got["row"] == "read", got
        # You read Night City Crew on the phone: Telegram says 0 still unread.
        await b.js("__tgSockets[__tgSockets.length-1].onmessage({data:JSON.stringify({type:'read',chat_id:77,max_id:50,outbox:false,unread:0})})")
        assert (await ticks(b))["badge77"] == "", "reading on the phone left the badge here"

    asyncio.run(desktop.with_browser("online", "", check, extra_init=fake))


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
@pytest.mark.parametrize("width", [1280, 820])
def test_paste_a_picture_drop_a_file_and_copy_a_message(width):
    """"Telegram needs copy and paste support" / "paste image into message from clipboard". A picture
    on the clipboard becomes an attachment and is sent as one; text on the clipboard is left to the
    text box; a dropped file attaches the same way; Copy puts a message's text through copyValue (the
    helper that works in the APK and the desktop, where navigator.clipboard is refused)."""
    got = {}

    async def check(b):
        await b.call("Emulation.setDeviceMetricsOverride", {"width": width, "height": 900, "deviceScaleFactor": 1, "mobile": False})
        await _open(b)
        await b.until("document.querySelectorAll('.tg-dialog').length===2")
        await b.js("document.querySelector('.tg-dialog[data-chat=\"42\"]').click()")
        await b.until("document.querySelectorAll('.tg-msg').length===3")
        # Paste a screenshot.
        await b.js("""(()=>{const dt=new DataTransfer();dt.items.add(new File([new Uint8Array([137,80,78,71,1,2,3])],'image.png',{type:'image/png'}));
          const t=document.querySelector('.tg-text');t.focus();t.dispatchEvent(new ClipboardEvent('paste',{clipboardData:dt,bubbles:true,cancelable:true}));})()""")
        got['chips_after_image'] = await b.js("[...document.querySelectorAll('.tg-pending .tg-chip')].map(c=>c.textContent.trim().split(' ')[0])")
        # Paste text: no chip, nothing prevented.
        got['text_prevented'] = await b.js("""(()=>{const dt=new DataTransfer();dt.setData('text/plain','hello there');
          const ev=new ClipboardEvent('paste',{clipboardData:dt,bubbles:true,cancelable:true});document.querySelector('.tg-text').dispatchEvent(ev);return ev.defaultPrevented;})()""")
        got['chips_after_text'] = await b.js("document.querySelectorAll('.tg-pending .tg-chip').length")
        # Drop a file on the chat.
        await b.js("""(()=>{const dt=new DataTransfer();dt.items.add(new File(['%PDF-1.4'],'notes.pdf',{type:'application/pdf'}));
          const pane=document.querySelector('.tg-chat');pane.dispatchEvent(new DragEvent('drop',{dataTransfer:dt,bubbles:true,cancelable:true}));})()""")
        got['chips_after_drop'] = await b.js("[...document.querySelectorAll('.tg-pending .tg-chip')].map(c=>c.textContent.trim().split(' ')[0])")
        await b.js("document.querySelector('.tg-text').value='two attachments';document.querySelector('.tg-send').click()")
        await b.until("__tg.files.length===2")
        got['sent'] = await b.js("__tg.files.map(f=>({name:f.name,size:f.size,caption:f.caption}))")
        # Copy a message's text.
        await b.js("window.__copied=[];__PC.copyValue=(t,m)=>{__copied.push(t);return Promise.resolve(true)}")
        await b.js("[...document.querySelectorAll('.tg-msg')].find(m=>m.textContent.includes('hey there')).querySelector('[data-copy]').click()")
        got['copied'] = await b.js("__copied")
        got['copy_on_media_only'] = await b.js("[...document.querySelectorAll('.tg-msg')].filter(m=>!m.querySelector('.tg-body')).some(m=>!!m.querySelector('[data-copy]'))")
    asyncio.run(desktop.with_browser("online", "", check, extra_init=FAKE.replace("state:'none'", "state:'ready'")))
    assert len(got['chips_after_image']) == 1 and got['chips_after_image'][0].startswith('pasted-') and got['chips_after_image'][0].endswith('.png'), got
    assert got['text_prevented'] is False and got['chips_after_text'] == 1, ('a text paste was taken over', got)
    assert got['chips_after_drop'][-1] == 'notes.pdf', got
    assert [s['size'] for s in got['sent']] == [7, 8] and got['sent'][0]['name'].startswith('pasted-'), got
    assert got['sent'][-1]['caption'] == 'two attachments', got
    assert got['copied'] == ['hey there'], got
    assert not got['copy_on_media_only'], 'a Copy button on a message with no text'
