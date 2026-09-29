/* Telegram voice and video calls — the browser half (the node half is app/services/telegram_client/calls.py).
 *
 * Telegram's call media cannot reach a browser directly, so the node runs the call and this file is the
 * other end of a bridge: the microphone goes up as 10 ms PCM16 frames (tgcall-worklet.js), the camera as
 * JPEG frames from a canvas, and the other side comes back the same way on one binary websocket
 * (/api/tgc/call-media, session token in the FIRST frame — never the URL). Control is plain HTTP
 * (/api/tgc/call) and call state arrives on the Telegram client's existing event socket, so a ringing
 * call reaches whichever window is open — telegram.js hands every `{type:'call'}` event to onEvent().
 *
 * The call screen is a fixed panel on <body>, not a modal: a call has to survive switching views, and a
 * modal would close the moment anything else opened one.
 */
(function(){
  'use strict';
  const PC = () => window.__PC || {};
  const esc = s => String(s == null ? '' : s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const PCM = 0x01, JPEG = 0x02, PCM_OUT = 0x11, JPEG_OUT = 0x12;
  const st = { peer:0, title:'', state:'idle', video:false, remote_video:false, emojis:'', since:0,
               ws:null, ctx:null, stream:null, cap:null, play:null, muted:false, camTimer:0, clock:0,
               remoteUrl:'', panel:null, sending:false };

  function base(){ const P = PC(); return ((P.apiBase ? P.apiBase() : '') || '').replace(/\/+$/, ''); }
  async function post(action, extra){
    const P = PC();
    try{ if(P.ensureAiSession) await P.ensureAiSession(); }catch(_){}
    const opts = { method:'POST', headers:{'Content-Type':'application/json'},
                   body:JSON.stringify(Object.assign({ action }, extra || {})) };
    const r = await (P.authFetch ? P.authFetch(base() + '/api/tgc/call', opts)
                                 : fetch(base() + '/api/tgc/call', Object.assign({ credentials:'include' }, opts)));
    let j = null; try{ j = await r.json(); }catch(_){}
    if(!r.ok || !j || j.ok === false) throw new Error((j && (j.error || j.detail)) || ('HTTP ' + r.status));
    return j;
  }
  const toast = m => { try{ const P = PC(); if(P.toast) P.toast(m); }catch(_){} };

  // ---- the screen --------------------------------------------------------------------------------
  function panel(){
    if(st.panel && st.panel.isConnected) return st.panel;
    const el = document.createElement('div');
    el.className = 'tgc-panel'; el.setAttribute('role', 'dialog'); el.setAttribute('aria-label', 'Telegram call');
    document.body.appendChild(el); st.panel = el; return el;
  }
  function close(){
    if(st.panel){ try{ st.panel.remove(); }catch(_){} st.panel = null; }
    clearInterval(st.clock); st.clock = 0;
  }
  function mmss(ms){ const s = Math.max(0, Math.floor(ms / 1000)); return Math.floor(s / 60) + ':' + String(s % 60).padStart(2, '0'); }
  function paint(){
    if(st.state === 'idle'){ close(); return; }
    const el = panel(), ringing = st.state === 'ringing';
    const status = ringing ? (st.remote_video ? 'Incoming video call' : 'Incoming call')
                 : st.state === 'calling' ? 'Calling…' : '';
    el.className = 'tgc-panel tgc-' + st.state + (st.remote_video ? ' tgc-has-video' : '');
    el.innerHTML = `
      <div class="tgc-stage">
        <img class="tgc-remote" alt="" ${st.remoteUrl ? `src="${esc(st.remoteUrl)}"` : 'hidden'}>
        <div class="tgc-who"><b>${esc(st.title || 'Telegram')}</b><span class="tgc-status">${esc(status)}</span>
          ${st.emojis ? `<span class="tgc-key" title="Compare these with the other person to confirm the call is private">${esc(st.emojis)}</span>` : ''}</div>
        <video class="tgc-self" muted playsinline autoplay ${st.video ? '' : 'hidden'}></video>
      </div>
      <div class="tgc-bar">${ringing ? `
        <button class="tgc-btn tgc-accept" data-tgc="accept" aria-label="Answer">📞<span>Answer</span></button>
        ${st.remote_video ? '<button class="tgc-btn tgc-accept" data-tgc="accept-video" aria-label="Answer with video">🎥<span>Video</span></button>' : ''}
        <button class="tgc-btn tgc-end" data-tgc="hangup" aria-label="Decline">✕<span>Decline</span></button>` : `
        <button class="tgc-btn${st.muted ? ' on' : ''}" data-tgc="mute" aria-pressed="${st.muted}" aria-label="${st.muted ? 'Unmute' : 'Mute'}">${st.muted ? '🔇' : '🎙'}<span>${st.muted ? 'Unmute' : 'Mute'}</span></button>
        <button class="tgc-btn${st.video ? ' on' : ''}" data-tgc="camera" aria-pressed="${st.video}" aria-label="${st.video ? 'Camera off' : 'Camera on'}">📷<span>${st.video ? 'Camera off' : 'Camera'}</span></button>
        <button class="tgc-btn tgc-end" data-tgc="hangup" aria-label="Hang up">✕<span>Hang up</span></button>`}
      </div>`;
    const self = el.querySelector('.tgc-self');
    if(self && st.stream && st.video){ try{ self.srcObject = st.stream; }catch(_){} }
    el.querySelectorAll('[data-tgc]').forEach(b => b.onclick = () => act(b.dataset.tgc));
    clearInterval(st.clock); st.clock = 0;
    if(st.state === 'active'){
      const tick = () => { const s = el.querySelector('.tgc-status'); if(s) s.textContent = mmss(Date.now() - st.since); };
      tick(); st.clock = setInterval(tick, 1000);
    }
  }

  // ---- actions -------------------------------------------------------------------------------------
  async function act(what){
    try{
      if(what === 'accept' || what === 'accept-video'){
        const video = what === 'accept-video';
        await media(video);                       // the microphone prompt BEFORE Telegram hears "yes"
        await post('accept', { peer:st.peer, video });
      }else if(what === 'hangup'){
        const was = st.state; st.state = 'idle'; teardown(); paint();
        await post('hangup', { peer:st.peer }).catch(() => {});
        if(was === 'ringing') toast('Call declined');
      }else if(what === 'mute'){
        st.muted = !st.muted;
        if(st.stream) st.stream.getAudioTracks().forEach(t => t.enabled = !st.muted);
        paint();
      }else if(what === 'camera'){
        const on = !st.video;
        if(on) await camera(true);
        await post('camera', { video:on });
        if(!on) await camera(false);
      }
    }catch(e){ toast(String(e && e.message || e)); }
  }
  async function start(peer, title, video){
    if(st.state !== 'idle'){ toast('You are already on a call.'); return; }
    st.peer = Number(peer); st.title = title || ''; st.state = 'calling'; st.video = !!video; st.emojis = '';
    paint();
    try{
      await media(!!video);
      await post('start', { peer:st.peer, video:!!video });
    }catch(e){ st.state = 'idle'; teardown(); paint(); toast(String(e && e.message || e)); }
  }

  // ---- events from the node --------------------------------------------------------------------------
  function onEvent(ev){
    if(!ev || ev.type !== 'call') return;
    const was = st.state;
    if(ev.state === 'idle'){
      if(was === 'idle') return;
      st.state = 'idle'; teardown(); paint();
      if(ev.reason && ev.reason !== 'hung up') toast('Call ended: ' + ev.reason);
      else toast('Call ended');
      return;
    }
    st.peer = Number(ev.peer) || st.peer; st.title = ev.title || st.title;
    st.remote_video = !!ev.remote_video; if(ev.emojis) st.emojis = ev.emojis;
    if(typeof ev.video === 'boolean' && ev.state !== 'ringing') st.video = ev.video;
    st.state = ev.state;
    if(ev.state === 'active' && was !== 'active'){ st.since = Date.now(); if(!st.ws) media(st.video).catch(() => {}); }
    if(ev.state === 'ringing' && was !== 'ringing') ring();
    paint();
  }
  function ring(){
    try{
      const P = PC(), text = (st.title || 'Somebody') + (st.remote_video ? ' is video calling you' : ' is calling you');
      if(P.notify) P.notify('Telegram', text, { tag:'tg-call' });
      else if(window.Notification && Notification.permission === 'granted') new Notification('Telegram', { body:text, tag:'tg-call' });
    }catch(_){}
  }

  // ---- media ---------------------------------------------------------------------------------------
  async function media(video){
    if(!st.stream){
      st.stream = await navigator.mediaDevices.getUserMedia({
        audio:{ echoCancellation:true, noiseSuppression:true, autoGainControl:true, channelCount:1 },
        video: video ? { width:{ ideal:640 }, height:{ ideal:360 }, frameRate:{ ideal:15 } } : false });
    }else if(video && !st.stream.getVideoTracks().length){ await camera(true); }
    st.video = !!video || st.video;
    if(!st.ctx){
      st.ctx = new (window.AudioContext || window.webkitAudioContext)({ sampleRate:48000 });
      await st.ctx.audioWorklet.addModule(new URL('/static/js/client/tgcall-worklet.js', location.href).href);
      const src = st.ctx.createMediaStreamSource(st.stream);
      st.cap = new AudioWorkletNode(st.ctx, 'pc-tg-capture');
      st.play = new AudioWorkletNode(st.ctx, 'pc-tg-play', { outputChannelCount:[1] });
      src.connect(st.cap);
      st.play.connect(st.ctx.destination);
      st.cap.port.onmessage = e => send(PCM, e.data);
    }
    if(st.ctx.state === 'suspended'){ try{ await st.ctx.resume(); }catch(_){} }
    openSocket();
    if(st.video) camLoop(true);
    paint();
  }
  async function camera(on){
    if(on){
      if(!st.stream || !st.stream.getVideoTracks().length){
        const v = await navigator.mediaDevices.getUserMedia({ video:{ width:{ ideal:640 }, height:{ ideal:360 }, frameRate:{ ideal:15 } } });
        v.getVideoTracks().forEach(t => st.stream ? st.stream.addTrack(t) : null);
        if(!st.stream) st.stream = v;
      }
      st.video = true; camLoop(true);
    }else{
      st.video = false; camLoop(false);
      if(st.stream) st.stream.getVideoTracks().forEach(t => { t.stop(); st.stream.removeTrack(t); });
    }
    paint();
  }
  function camLoop(on){
    clearInterval(st.camTimer); st.camTimer = 0;
    if(!on) return;
    const vid = document.createElement('video'); vid.muted = true; vid.playsInline = true;
    try{ vid.srcObject = st.stream; vid.play().catch(() => {}); }catch(_){}
    const cv = document.createElement('canvas'); cv.width = 640; cv.height = 360;
    const g = cv.getContext('2d');
    st.camTimer = setInterval(() => {
      if(!st.video || !st.ws || st.ws.readyState !== 1 || st.sending || st.ws.bufferedAmount > 256 * 1024) return;
      if(!vid.videoWidth) return;
      const r = Math.min(640 / vid.videoWidth, 360 / vid.videoHeight);
      cv.width = Math.max(2, Math.round(vid.videoWidth * r) & ~1); cv.height = Math.max(2, Math.round(vid.videoHeight * r) & ~1);
      g.drawImage(vid, 0, 0, cv.width, cv.height);
      st.sending = true;
      cv.toBlob(b => { st.sending = false; if(b) b.arrayBuffer().then(buf => send(JPEG, buf)); }, 'image/jpeg', 0.6);
    }, 1000 / 12);
  }
  function send(kind, buf){
    const ws = st.ws; if(!ws || ws.readyState !== 1) return;
    if(kind === PCM && st.muted) return;
    const out = new Uint8Array(buf.byteLength + 1); out[0] = kind; out.set(new Uint8Array(buf), 1);
    try{ ws.send(out); }catch(_){}
  }
  function openSocket(){
    if(st.ws && st.ws.readyState <= 1) return;
    const P = PC(), origin = (P.apiBase ? P.apiBase() : location.origin) || location.origin;
    let ws; try{ ws = new WebSocket(origin.replace(/^http/, 'ws') + '/api/tgc/call-media'); }catch(_){ return; }
    ws.binaryType = 'arraybuffer'; st.ws = ws;
    ws.onopen = async () => { try{ if(P.ensureAiSession) await P.ensureAiSession(); }catch(_){}
      ws.send(JSON.stringify({ token: P.aiToken ? P.aiToken() : '' })); };
    ws.onmessage = e => {
      if(typeof e.data === 'string'){ try{ onEvent(JSON.parse(e.data)); }catch(_){} return; }
      const b = new Uint8Array(e.data); if(!b.length) return;
      if(b[0] === PCM_OUT && st.play){ const pcm = b.slice(1); st.play.port.postMessage(pcm.buffer, [pcm.buffer]); }
      else if(b[0] === JPEG_OUT){
        const url = URL.createObjectURL(new Blob([b.subarray(1)], { type:'image/jpeg' }));
        const img = st.panel && st.panel.querySelector('.tgc-remote');
        if(st.remoteUrl) try{ URL.revokeObjectURL(st.remoteUrl); }catch(_){}
        st.remoteUrl = url;
        if(img){ img.hidden = false; img.src = url; }
      }
    };
    ws.onclose = () => { if(st.ws === ws) st.ws = null; };
  }
  function teardown(){
    camLoop(false);
    if(st.ws){ try{ st.ws.close(); }catch(_){} st.ws = null; }
    if(st.stream){ st.stream.getTracks().forEach(t => { try{ t.stop(); }catch(_){} }); st.stream = null; }
    if(st.ctx){ try{ st.ctx.close(); }catch(_){} st.ctx = null; }
    st.cap = st.play = null;
    if(st.remoteUrl){ try{ URL.revokeObjectURL(st.remoteUrl); }catch(_){} st.remoteUrl = ''; }
    st.muted = false; st.video = false; st.remote_video = false; st.emojis = '';
  }

  window.PCTgCall = { start, onEvent, _state:st };
})();
