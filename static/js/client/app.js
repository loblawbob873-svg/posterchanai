/* PosterChan Nostr client controller. Talks only to the built-in relay (window.Relay) and the
 * built-in Blossom server. Crypto runs in the worker (local key) or the NIP-07 extension. */
(function(){
  const NT = () => window.NostrTools;
  const $ = (s,r=document)=>r.querySelector(s);
  const $$ = (s,r=document)=>[...r.querySelectorAll(s)];
  const enc = s => (s==null?'':String(s)).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const LOGO = '/static/posterchan-relay.png';
  const isDesktop = () => !window.matchMedia('(max-width:820px)').matches;   // pop-out player is desktop-only
  // Extensionless Blossom blobs (/<sha256>) carry NO type in the URL, so we render them as <img>;
  // if that fails the blob is likely a video (bots post bare video URLs) — try <video>, and only
  // if THAT fails fall back to a plain link. (Fixes videos showing as a link instead of playing.)
  window.__blobFallback = function(el){
    const src = el.currentSrc || el.src;
    if(el.tagName === 'IMG'){
      const v=document.createElement('video'); v.src=src; v.controls=true; v.playsInline=true;
      v.preload='metadata'; v.className=el.className; v.onerror=()=>window.__blobFallback(v); el.replaceWith(v);
    } else {
      const a=document.createElement('a'); a.href=src; a.target='_blank'; a.rel='noopener'; a.textContent=src; el.replaceWith(a);
    }
  };

  let CFG = {}, ME = null, FOLLOWS = new Set(), MUTED = new Set(), PINNED = new Set(), BOOKMARKS = new Set(), VIEW = 'home', IS_ADMIN = false;
  let signer = null;
  const subs = {};                 // view -> subId
  const seenNotif = { last: 0 };

  // ---------- signer abstraction ----------
  function makeSigner(mode, pubkey){
    if (mode === 'nip07'){
      return {
        mode, pubkey,
        signEvent: (tpl) => window.nostr.signEvent(tpl),
        nip04enc: (peer, txt) => window.nostr.nip04.encrypt(peer, txt),
        nip04dec: (peer, ct) => window.nostr.nip04.decrypt(peer, ct),
      };
    }
    if (mode === 'nip46'){   // Amber / remote signer (NIP-46): the user's key stays in the signer
      return {
        mode, pubkey,
        signEvent: (tpl) => Nip46.signEvent(tpl),
        nip04enc: (peer, txt) => Nip46.nip04enc(peer, txt),
        nip04dec: (peer, ct) => Nip46.nip04dec(peer, ct),
      };
    }
    return {  // local key — crypto in the worker
      mode, pubkey,
      signEvent: (tpl) => Relay.worker.call('sign', { event: tpl }),
      nip04enc: (peer, txt) => Relay.worker.call('nip04enc', { peer, text: txt }).then(r=>r.ct),
      nip04dec: (peer, ct) => Relay.worker.call('nip04dec', { peer, ct }).then(r=>r.pt),
      // NIP-17 gift-wrapped DMs (local-key only — needs the secret key the extension never exposes)
      nip17wrap: (peer, text) => Relay.worker.call('nip17wrap', { peer, text }),
      nip17unwrap: (wrap) => Relay.worker.call('nip17unwrap', { wrap }).then(r=>r.rumor),
    };
  }
  // build + sign an event from a template
  async function sign(kind, content, tags=[]){
    const tpl = { kind, content, tags, created_at: Math.floor(Date.now()/1000), pubkey: ME.pubkey };
    return await signer.signEvent(tpl);
  }
  async function publish(kind, content, tags){
    const ev = await sign(kind, content, tags);
    Store.saveEvent(ev); invalidateCounts();
    const r = await Relay.publish(ev);
    if (!r.ok) toast('relay: ' + (r.msg||'rejected'));
    return { ev, ...r };
  }

  // ---------- NIP-46 remote signer (Amber / nsecbunker) ----------
  // The user's secret key lives in the remote signer. We hold an EPHEMERAL "app key" (in the
  // worker) purely to encrypt/sign the NIP-46 transport (kind-24133 events over the signer's relay);
  // every user-facing sign/encrypt is forwarded to the signer, which prompts the user to approve.
  const Nip46 = {
    ws:null, relay:null, appSk:null, appPk:null, remotePk:null, userPk:null,
    _pending:new Map(), _subId:null, _onEvent:null,
    reset(){ this._wantOpen=false; try{ if(this.ws){ this.ws.onclose=null; this.ws.close(); } }catch(_){} this.ws=null; this._pending.clear(); this._onEvent=null; },
    // NIP-46 transport is NIP-04 by default, but some signers reply with NIP-44 — try each scheme
    // through to a valid JSON payload (a wrong scheme may return garbage rather than throw).
    async _decode(peer, ct){
      for(const op of ['nip04dec','nip44dec']){
        try{ return JSON.parse((await Relay.worker.call(op,{ peer, ct })).pt); }catch(_){}
      }
      return null;
    },
    // load (or reuse) the ephemeral app key into the worker
    async _ensureAppKey(sk){
      const g = sk ? { sk } : await Relay.worker.call('genKey', {});
      const r = await Relay.worker.call('setKey', { sk: g.sk });
      this.appSk = g.sk; this.appPk = r.pubkey; return this.appPk;
    },
    // open a socket to the signer's relay + subscribe for responses addressed to our app key
    _openRelay(relay){
      this._wantOpen=true;
      return new Promise((res,rej)=>{
        let done=false; const ws=new WebSocket(relay); this.ws=ws; this.relay=relay;
        ws.onopen=()=>{ this._subId='n46'+Math.random().toString(36).slice(2,8);
          ws.send(JSON.stringify(['REQ', this._subId, { kinds:[24133], '#p':[this.appPk], since: Math.floor(Date.now()/1000)-5 }]));
          if(!done){ done=true; res(); } };
        ws.onmessage=(e)=>this._recv(e.data);
        ws.onerror=()=>{ if(!done){ done=true; rej(new Error('cannot reach signer relay')); } };
        // a remote signer is contacted only when signing, so the relay may idle-drop us — reconnect
        // (and re-subscribe) so the next sign still gets through without forcing a re-pair.
        ws.onclose=()=>{ if(this._wantOpen && this.ws===ws){ this.ws=null; setTimeout(()=>{ if(this._wantOpen && !this.ws) this._openRelay(relay).catch(()=>{}); }, 2000); } };
        setTimeout(()=>{ if(!done){ done=true; rej(new Error('signer relay timed out')); } }, 9000);
      });
    },
    async _recv(raw){
      let m; try{ m=JSON.parse(raw); }catch(_){ return; }
      if(m[0]!=='EVENT' || m[1]!==this._subId) return;
      const ev=m[2]; if(!ev || ev.kind!==24133) return;
      const payload=await this._decode(ev.pubkey, ev.content); if(!payload) return;
      if(this._onEvent) try{ this._onEvent(ev, payload); }catch(_){}   // nostrconnect handshake hook
      // the signer needs the user to approve in-app → open the deep link / approval URL
      if(payload.result==='auth_url' || (payload.error && /^https?:\/\//i.test(payload.error||''))){
        try{ window.open(payload.error,'_blank'); }catch(_){} return;
      }
      const p=this._pending.get(payload.id);
      if(p){ this._pending.delete(payload.id); payload.error ? p.rej(new Error(payload.error)) : p.res(payload.result); }
    },
    async _send(method, params){
      if(!this.remotePk || !this.ws) throw new Error('signer not connected');
      const id='r'+Math.random().toString(36).slice(2,10);
      const ct=(await Relay.worker.call('nip04enc',{peer:this.remotePk, text:JSON.stringify({ id, method, params })})).ct;
      const tpl={ kind:24133, content:ct, tags:[['p',this.remotePk]], created_at:Math.floor(Date.now()/1000), pubkey:this.appPk };
      const signed=await Relay.worker.call('sign',{ event:tpl });
      return new Promise((res,rej)=>{
        this._pending.set(id,{res,rej});
        try{ this.ws.send(JSON.stringify(['EVENT', signed])); }catch(e){ this._pending.delete(id); return rej(e); }
        // generous timeout — the user may need to physically approve on their phone
        setTimeout(()=>{ if(this._pending.has(id)){ this._pending.delete(id); rej(new Error('signer request timed out')); } }, 120000);
      });
    },
    // bunker://<remote-signer-pubkey>?relay=wss://…&secret=…  (Amber gives you this string)
    async connectBunker(uri){
      const mm=String(uri||'').trim().match(/^bunker:\/\/([0-9a-fA-F]{64})\??(.*)$/);
      if(!mm) throw new Error('not a bunker:// link');
      const remote=mm[1].toLowerCase(); const qs=new URLSearchParams(mm[2]||'');
      const relays=qs.getAll('relay'); const secret=qs.get('secret')||'';
      if(!relays.length) throw new Error('bunker link is missing its relay');
      await this._ensureAppKey(); await this._openRelay(relays[0]); this.remotePk=remote;
      await this._send('connect',[remote, secret]);     // may bounce through an auth_url first
      const userPk=await this._send('get_public_key',[]);
      this.userPk=userPk;
      return { userPk, session:{ mode:'nip46', sk:this.appSk, relay:this.relay, remotePk:remote, userPk } };
    },
    // nostrconnect://<app-pubkey>?relay=…&secret=…  (WE present this; the signer connects to us)
    async beginNostrConnect(relay, name){
      await this._ensureAppKey(); await this._openRelay(relay);
      const secret=Math.random().toString(36).slice(2,12);
      const uri=`nostrconnect://${this.appPk}?relay=${encodeURIComponent(relay)}&secret=${secret}&name=${encodeURIComponent(name||'PosterChan')}`;
      const done=new Promise((res,rej)=>{
        const to=setTimeout(()=>{ this._onEvent=null; rej(new Error('timed out waiting for the signer')); }, 180000);
        this._onEvent=async (ev, payload)=>{
          if(!payload.result || payload.result==='auth_url') return;   // wait for the connect ack
          this.remotePk=ev.pubkey; this._onEvent=null; clearTimeout(to);
          try{ const pk=await this._send('get_public_key',[]); this.userPk=pk;
            res({ userPk:pk, session:{ mode:'nip46', sk:this.appSk, relay, remotePk:this.remotePk, userPk:pk } }); }
          catch(e){ rej(e); }
        };
      });
      return { uri, done };
    },
    async resume(s){
      await this._ensureAppKey(s.sk); await this._openRelay(s.relay);
      this.remotePk=s.remotePk; this.userPk=s.userPk||null;
      if(!this.userPk) this.userPk=await this._send('get_public_key',[]);
      return this.userPk;
    },
    // signer interface — every user op is forwarded to the remote signer
    async signEvent(tpl){ return JSON.parse(await this._send('sign_event',[JSON.stringify(tpl)])); },
    nip04enc(peer, text){ return this._send('nip04_encrypt',[peer, text]); },
    nip04dec(peer, ct){ return this._send('nip04_decrypt',[peer, ct]); },
  };

  // ---------- boot ----------
  async function boot(){
    CFG = await fetch('/client/config').then(r=>r.json()).catch(()=>({}));
    await Store.init();
    if ('serviceWorker' in navigator){
      // auto-reload once when a new SW takes control, so deploys land on installed PWAs without
      // manual cache-clearing (the stale-app.js / "no GIF button on mobile" problem)
      let _refreshing=false;
      navigator.serviceWorker.addEventListener('controllerchange', ()=>{ if(_refreshing) return; _refreshing=true; location.reload(); });
      navigator.serviceWorker.register('/client/sw.js',{scope:'/client'}).catch(()=>{});
    }
    Relay.onStatus = renderConn;
    bindAuth();
    const s = Session.load();
    if (s) { try { await resume(s); return; } catch(e){ console.warn(e); Session.clear(); } }
    showAuth();
  }

  async function resume(s){
    if (s.mode === 'nip07'){
      if (!window.nostr) throw new Error('extension gone');
      const pk = await window.nostr.getPublicKey();
      signer = makeSigner('nip07', pk); ME = { mode:'nip07', pubkey: pk, npub: NT().nip19.npubEncode(pk) };
    } else if (s.mode === 'nip46'){
      const pk = await Nip46.resume(s);
      signer = makeSigner('nip46', pk); ME = { mode:'nip46', pubkey: pk, npub: NT().nip19.npubEncode(pk) };
    } else {
      const r = await Relay.worker.call('setKey', { sk: s.sk });
      signer = makeSigner('local', r.pubkey); ME = { mode:'local', pubkey: r.pubkey, npub: NT().nip19.npubEncode(r.pubkey) };
    }
    Session.save(s);
    startApp();
  }

  // ---------- auth UI ----------
  function showAuth(){ $('#auth-gate').classList.remove('hidden'); $('#app').classList.add('hidden'); }
  function bindAuth(){
    $('#btn-nip07').onclick = loginNip07;
    $('#btn-nsec-login').onclick = loginNsec;
    $('#btn-amber').onclick = ()=>{ amberErr(''); $('#auth-login').classList.add('hidden'); $('#auth-amber').classList.remove('hidden'); };
    $('#btn-amber-back').onclick = ()=>{ Nip46.reset(); $('#amber-nc-box').classList.add('hidden'); $('#auth-amber').classList.add('hidden'); $('#auth-login').classList.remove('hidden'); };
    $('#btn-amber-connect').onclick = loginAmberBunker;
    $('#btn-amber-nc').onclick = loginAmberNostrConnect;
    $('#btn-show-signup').onclick = ()=>{ $('#auth-login').classList.add('hidden'); $('#auth-signup').classList.remove('hidden'); };
    $('#btn-back-login').onclick = ()=>{ $('#auth-signup').classList.add('hidden'); $('#auth-login').classList.remove('hidden'); };
    $('#btn-gen-key').onclick = genKey;
    $('#btn-signup-go').onclick = signupGo;
    document.addEventListener('click', e=>{ const c = e.target.closest('[data-copy]'); if(c){ navigator.clipboard.writeText($('#'+c.dataset.copy).textContent); toast('copied'); } });
  }
  function authErr(m){ $('#auth-error').textContent = m||''; }

  async function loginNip07(){
    authErr('');
    if (!window.nostr){ authErr('No NIP-07 extension found (try Alby/nos2x).'); return; }
    try {
      const pk = await window.nostr.getPublicKey();
      signer = makeSigner('nip07', pk); ME = { mode:'nip07', pubkey: pk, npub: NT().nip19.npubEncode(pk) };
      Session.save({ mode:'nip07' }); startApp();
    } catch(e){ authErr('extension declined'); }
  }
  function amberErr(m){ const el=$('#amber-error'); if(el) el.textContent=m||''; }
  function finishAmberLogin(pk, session){
    signer = makeSigner('nip46', pk); ME = { mode:'nip46', pubkey: pk, npub: NT().nip19.npubEncode(pk) };
    Session.save(session);
    $('#auth-amber').classList.add('hidden'); $('#amber-nc-box').classList.add('hidden'); $('#auth-login').classList.remove('hidden');
    startApp();
  }
  async function loginAmberBunker(){
    amberErr(''); const uri=$('#amber-input').value.trim();
    if(!uri){ amberErr('paste your bunker:// link'); return; }
    if(/^nostrconnect:/i.test(uri)){ amberErr('that’s a nostrconnect link — use the button below instead'); return; }
    const btn=$('#btn-amber-connect'); btn.disabled=true; btn.textContent='connecting…';
    try{ const { userPk, session }=await Nip46.connectBunker(uri); finishAmberLogin(userPk, session); }
    catch(e){ amberErr(e.message||'could not connect'); Nip46.reset(); }
    finally{ btn.disabled=false; btn.textContent='Connect'; }
  }
  async function loginAmberNostrConnect(){
    amberErr(''); const btn=$('#btn-amber-nc'); btn.disabled=true; btn.textContent='preparing…';
    try{
      const { uri, done }=await Nip46.beginNostrConnect('wss://relay.nsec.app', 'PosterChan');
      $('#amber-nc-uri').textContent=uri;
      const open=$('#amber-nc-open'); if(open) open.href=uri;
      $('#amber-nc-status').textContent='waiting for the signer to approve…';
      $('#amber-nc-box').classList.remove('hidden');
      const { userPk, session }=await done; finishAmberLogin(userPk, session);
    }catch(e){ amberErr(e.message||'could not connect'); Nip46.reset(); $('#amber-nc-box').classList.add('hidden'); }
    finally{ btn.disabled=false; btn.textContent='📡 Open in Amber (nostrconnect)'; }
  }
  async function loginNsec(){
    authErr('');
    const v = $('#nsec-input').value.trim(); if (!v) return;
    try {
      const r = await Relay.worker.call('decodeNsec', { nsec: v });
      await Relay.worker.call('setKey', { sk: r.sk });
      signer = makeSigner('local', r.pubkey); ME = { mode:'local', pubkey: r.pubkey, npub: r.npub };
      Session.save({ mode:'local', sk: r.sk }); startApp();
    } catch(e){ authErr('invalid nsec'); }
  }
  let _gen = null;
  async function genKey(){
    _gen = await Relay.worker.call('genKey', {});
    $('#signup-npub').textContent = _gen.npub; $('#signup-nsec').textContent = _gen.nsec;
    $('#signup-keys').classList.remove('hidden'); $('#btn-gen-key').classList.add('hidden'); $('#btn-signup-go').classList.remove('hidden');
  }
  async function signupGo(){
    if (!_gen) return;
    $('#signup-status').textContent = 'registering…';
    await Relay.worker.call('setKey', { sk: _gen.sk });
    signer = makeSigner('local', _gen.pubkey); ME = { mode:'local', pubkey: _gen.pubkey, npub: _gen.npub };
    Session.save({ mode:'local', sk: _gen.sk });
    // ask the node's operator to follow us so the WoT relay accepts our posts
    try {
      const res = await fetch('/client/signup-follow', { method:'POST', headers:{'Content-Type':'application/json'},
        body: JSON.stringify({ pubkey: _gen.pubkey }) }).then(r=>r.json());
      if (!res.ok) toast('note: ' + (res.error||'could not auto-follow'));
    } catch(_){}
    // claim a NIP-05 name on this node's identity server (proves key ownership with a self-signed
    // event so names can't be squatted). The node assigns a free name@domain and returns it.
    const nm = $('#signup-name').value.trim();
    let nip05 = null;
    try {
      const auth = await sign(27235, 'claim-nip05', [['p', _gen.pubkey]]);
      const r = await fetch('/client/claim-nip05', { method:'POST', headers:{'Content-Type':'application/json'},
        body: JSON.stringify({ pubkey: _gen.pubkey, name: nm, auth: btoa(JSON.stringify(auth)) }) }).then(r=>r.json());
      if (r && r.ok) nip05 = r.nip05;
    } catch(_){}
    // publish the initial profile (name + the assigned NIP-05 so the verified badge shows)
    const prof = {}; if (nm) prof.name = nm; if (nip05) prof.nip05 = nip05;
    startApp();
    if (Object.keys(prof).length){ try { await publish(0, JSON.stringify(prof), []); } catch(_){}}
    if (nip05) toast('your handle: ' + nip05);
  }

  // ---------- app start ----------
  function startApp(){
    IS_ADMIN = Array.isArray(CFG.admin_npubs) && CFG.admin_npubs.includes(ME.npub);
    try{ if(window.Notification && Notification.permission==='default') Notification.requestPermission(); }catch(_){}
    $('#auth-gate').classList.add('hidden'); $('#app').classList.remove('hidden');
    $('#btn-logout').onclick = logout;
    $('#me-card').onclick = ()=>renderProfileView(ME.pubkey);
    $$('.nav-item[data-view]').forEach(b=> b.onclick = ()=>switchView(b.dataset.view));
    $('#btn-compose').onclick = ()=>compose(); $('#btn-compose-m').onclick = ()=>compose();
    // mobile overflow sheet — delegated so the tap is caught even if the node is re-created
    document.addEventListener('click', e=>{ if(e.target.closest && e.target.closest('#btn-more-m')){ e.preventDefault(); moreMenu(); } });
    $('#btn-refresh').onclick = ()=>renderView(true);
    bindSearch();
    bindFeedActions();
    $('#feed').addEventListener('scroll', onFeedScroll, { passive:true });   // infinite scroll-back
    bumpDraft();   // show the saved-drafts count on the nav badge
    // YouTube facade → load the real player iframe on click (kept out of the timeline until then
    // for performance), and don't let the click bubble up to open the note thread.
    document.addEventListener('click', e=>{
      const yt=e.target.closest && e.target.closest('.yt-embed[data-yt]'); if(!yt) return;
      e.preventDefault(); e.stopPropagation();
      const f=document.createElement('div'); f.className='yt-frame';
      f.innerHTML=`<iframe src="https://www.youtube.com/embed/${yt.dataset.yt}?autoplay=1" allow="autoplay; encrypted-media; fullscreen" allowfullscreen loading="lazy"></iframe>`;
      yt.replaceWith(f);
    });
    // Run initial queries only once the relay socket is open (otherwise the REQs are dropped
    // and profiles/follows never resolve — names would show as raw npubs).
    Relay.onReady = ()=>{ fetchFollows(); fetchMutes(); fetchPins(); fetchBookmarks(); fetchMyProfile(); watchNotifications();
      setTimeout(()=>ensureDMs(), 3000); setTimeout(loadRightbar, 1500); };   // DMs + rightbar load after the timeline
    connectRelays();
    renderMe();
    switchView('home');
    setInterval(loadRightbar, 300000);   // refresh hot/trending every 5 min
    // Re-fetch profiles for on-screen authors still showing as npub — as the relay backfills
    // profiles, already-displayed posts resolve to names/avatars without needing a re-render.
    setInterval(()=>{ if(document.hidden) return; let n=0; $$('.note[data-pk]').forEach(el=>{ if(n<60 && !Store.haveProfile(el.dataset.pk)){ needProfile(el.dataset.pk); n++; } }); }, 12000);
  }
  function logout(){ Session.clear(); Relay.worker.call('clearKey',{}); location.reload(); }

  // Decide which relays to connect: the user's own list when they've enabled it (untrusted, so the
  // pool verifies signatures), otherwise the single built-in WoT relay (trusted).
  function userRelays(){ return (ClientSettings.get('relays')||[]).map(u=>String(u||'').trim()).filter(Boolean); }
  function connectRelays(){
    const list = ClientSettings.get('relaysEnabled') ? userRelays() : [];
    if (list.length) Relay.configure({ urls: list, verify: true });
    else Relay.connect(CFG.relay_url);
  }

  function renderConn(s){
    const map = { ok:['ok','online'], connecting:['','connecting…'], off:['off','reconnecting…'], init:['','…'] };
    const [cls,txt] = map[s]||['',''];
    const el = $('#conn-status'); if(!el) return; el.className = 'conn ' + cls; el.querySelector('span').textContent = txt;
  }
  function renderMe(){
    const p = Store.profile(ME.pubkey) || {};
    const av = p.picture || LOGO;
    $('#me-card').innerHTML = `<img src="${enc(av)}" onerror="this.src='${LOGO}'"><div><div class="mn">${enc(p.name||p.display_name||'anon')}</div><div class="mk">${enc(ME.npub.slice(0,12))}…</div></div>`;
  }

  // ---------- follows + profiles ----------
  async function fetchFollows(){
    const evs = await Relay.query([{ authors:[ME.pubkey], kinds:[3], limit:1 }]);
    if (evs.length){ const e = evs.sort((a,b)=>b.created_at-a.created_at)[0];
      FOLLOWS = new Set(e.tags.filter(t=>t[0]==='p'&&t[1]).map(t=>t[1])); }
    FOLLOWS.add(ME.pubkey);
    // prefetch follows' profiles so @-mention autocomplete has names to suggest right away
    [...FOLLOWS].slice(0,300).forEach(needProfile);
    if (VIEW==='home') renderView(true);
  }
  async function fetchMutes(){
    const evs = await Relay.query([{ authors:[ME.pubkey], kinds:[10000], limit:1 }]);
    if (evs.length){ const e=evs.sort((a,b)=>b.created_at-a.created_at)[0];
      MUTED = new Set(e.tags.filter(t=>t[0]==='p'&&t[1]).map(t=>t[1])); }
  }
  // replaceable-list edit helper: fetch newest (kind), add/remove a p-tag, republish preserving content+tags
  async function _editPList(kind, pk, add){
    const evs = await Relay.query([{ authors:[ME.pubkey], kinds:[kind], limit:1 }]);
    const cur = evs.length ? evs.sort((a,b)=>b.created_at-a.created_at)[0] : null;
    let tags = cur ? cur.tags.map(t=>[...t]) : [];
    const has = tags.some(t=>t[0]==='p'&&t[1]===pk);
    if (add && !has) tags.push(['p',pk]);
    else if (!add && has) tags = tags.filter(t=>!(t[0]==='p'&&t[1]===pk));
    else return;
    await publish(kind, cur?cur.content:'', tags);
  }
  async function toggleFollow(pk){
    const have=FOLLOWS.has(pk); await _editPList(3, pk, !have);
    have?FOLLOWS.delete(pk):FOLLOWS.add(pk); toast(have?'unfollowed':'followed');
  }
  async function toggleMute(pk){
    const have=MUTED.has(pk); await _editPList(10000, pk, !have);
    have?MUTED.delete(pk):MUTED.add(pk); toast(have?'unmuted':'muted'); if(VIEW==='home'||VIEW==='global') renderView(true);
  }
  async function fetchPins(){
    const evs=await Relay.query([{ authors:[ME.pubkey], kinds:[10001], limit:1 }]);
    if(evs.length){ const e=evs.sort((a,b)=>b.created_at-a.created_at)[0];
      PINNED=new Set(e.tags.filter(t=>t[0]==='e'&&t[1]).map(t=>t[1])); }
  }
  async function _editEList(kind, eid, add){    // replaceable e-tag list (e.g. pinned notes k10001)
    const evs=await Relay.query([{ authors:[ME.pubkey], kinds:[kind], limit:1 }]);
    const cur=evs.length?evs.sort((a,b)=>b.created_at-a.created_at)[0]:null;
    let tags=cur?cur.tags.map(t=>[...t]):[];
    const has=tags.some(t=>t[0]==='e'&&t[1]===eid);
    if(add&&!has) tags.push(['e',eid]); else if(!add&&has) tags=tags.filter(t=>!(t[0]==='e'&&t[1]===eid)); else return;
    await publish(kind, cur?cur.content:'', tags);
  }
  async function togglePin(id){
    const have=PINNED.has(id); await _editEList(10001, id, !have);
    have?PINNED.delete(id):PINNED.add(id); toast(have?'unpinned':'pinned 📌');
    if(VIEW==='profile') renderProfileView(ME.pubkey);
  }
  // ---------- bookmarks (NIP-51 kind 10003 — replaceable e-tag list) ----------
  async function fetchBookmarks(){
    const evs=await Relay.query([{ authors:[ME.pubkey], kinds:[10003], limit:1 }]);
    if(evs.length){ const e=evs.sort((a,b)=>b.created_at-a.created_at)[0];
      BOOKMARKS=new Set(e.tags.filter(t=>t[0]==='e'&&t[1]).map(t=>t[1])); }
    if(VIEW==='bookmarks') renderBookmarks();
    else try{ decorateCounts(); }catch(_){}   // light up the 🔖 on any posts already on screen
  }
  async function toggleBookmark(id, btn){
    const have=BOOKMARKS.has(id); await _editEList(10003, id, !have);
    have?BOOKMARKS.delete(id):BOOKMARKS.add(id); toast(have?'removed bookmark':'bookmarked 🔖');
    if(btn) btn.classList.toggle('on', !have);
    if(VIEW==='bookmarks') renderBookmarks();
  }
  const _profQ = new Set(); let _profT = null;
  const _profMiss = new Map();   // pubkey -> ts of a lookup that returned nothing (throttle retries)
  const _PROF_MISS_TTL = 300000; // 5 min before re-asking the relay for a not-found profile
  function needProfile(pk){
    if(!pk || Store.haveProfile(pk)) return;
    const miss=_profMiss.get(pk); if(miss && Date.now()-miss < _PROF_MISS_TTL) return;  // don't hammer
    _profQ.add(pk); if(!_profT) _profT=setTimeout(flushProfiles,120);
  }
  async function flushProfiles(){
    _profT=null; const pks=[..._profQ]; _profQ.clear(); if(!pks.length) return;
    const evs = await Relay.query([{ authors:pks, kinds:[0], limit:pks.length }]);
    const got=new Set(); let changed=false;
    for(const e of evs){ Store.saveProfile(e); got.add(e.pubkey); changed=true; }
    const now=Date.now();
    for(const pk of pks){ if(!got.has(pk)) _profMiss.set(pk, now); }   // relay has no profile yet → back off
    if(_profMiss.size>5000){ for(const k of _profMiss.keys()){ _profMiss.delete(k); if(_profMiss.size<=4000) break; } }
    if(changed){ renderMe(); decorateProfiles(); }
  }
  async function fetchMyProfile(){ const e=await Relay.query([{authors:[ME.pubkey],kinds:[0],limit:1}]); if(e.length){Store.saveProfile(e.sort((a,b)=>b.created_at-a.created_at)[0]); renderMe();} }
  function decorateProfiles(){
    $$('.note[data-pk]').forEach(n=>{ const p=Store.profile(n.dataset.pk); if(p){
      const a=n.querySelector('.av'); if(p.picture && a) a.src=p.picture;
      const nm=n.querySelector('.name'); if(nm) nm.textContent=p.name||p.display_name||nm.textContent;
      const h=n.querySelector('.handle'); const nip=niceNip05(p.nip05); if(h && nip) h.textContent=nip;
      decorateVerified(n.querySelector('.vchk'), n.dataset.pk, p.nip05);
    }});
    $$('.rb-item[data-pk]').forEach(n=>{ const p=Store.profile(n.dataset.pk); if(p){
      const a=n.querySelector('.rb-av'); if(p.picture && a) a.src=p.picture;
      const b=n.querySelector('b'); if(b) b.textContent=p.name||p.display_name||b.textContent;
    }});
  }

  // ---------- view routing ----------
  function switchView(v){
    VIEW = v;
    $$('.nav-item[data-view]').forEach(b=> b.classList.toggle('active', b.dataset.view===v));
    $('#view-title').textContent = { home:'Home', global:'Global', notifications:'Notifications', messages:'Messages', drafts:'Drafts', bookmarks:'Bookmarks', articles:'Articles', streams:'Streams', blossom:'Files', profile:'Profile', settings:'Settings', ai:'PosterChan AI' }[v]||v;
    renderView(true);
  }
  function renderView(reset){
    cleanupInlineStream();   // leaving a view tears down the inline stream player (unless popped out)
    const feed = $('#feed');
    if(VIEW!=='ai' && _ai && _ai.ws){ try{ _ai.ws.onclose=null; _ai.ws.close(); }catch(_){} _ai.ws=null; }
    if(VIEW!=='home' && VIEW!=='global') _hidePill();
    feed.classList.toggle('feed-dm', VIEW==='messages');   // full-height messages layout (no :has needed)
    feed.classList.toggle('feed-ai', VIEW==='ai');         // full-height chat layout (msgs scroll inside)
    if (reset) feed.innerHTML = '<div class="spinner"></div>';
    if (VIEW==='home' || VIEW==='global') return renderTimeline(VIEW, reset);
    if (VIEW==='notifications') return renderNotifications();
    if (VIEW==='messages') return renderMessages();
    if (VIEW==='drafts') return renderDrafts();
    if (VIEW==='bookmarks') return renderBookmarks();
    if (VIEW==='articles') return renderArticles();
    if (VIEW==='streams') return renderStreams();
    if (VIEW==='blossom') return renderBlossom();
    if (VIEW==='settings') return renderSettings();
    if (VIEW==='ai') return renderAI();
    if (VIEW==='profile') return renderProfile(ME.pubkey);
  }

  // ---------- timeline ----------
  function timelineFilter(){
    if (VIEW==='home') return [{ kinds:[1,6], authors:[...FOLLOWS], limit:80 }];
    return [{ kinds:[1,6], limit:120 }];
  }
  // pagination state for the home/global timelines (infinite scroll-back via `until`)
  let _tl = { oldest:0, loading:false, done:false, pages:0 };
  function renderTimeline(view, reset){
    const fn = view==='home' ? (ev=>FOLLOWS.has(ev.pubkey)) : null;
    if(reset){ _tl = { oldest:0, loading:false, done:false, pages:0 }; _resetLive(); }
    _drawTimeline(false);
    if (subs[view]) Relay.close(subs[view]);
    subs[view] = Relay.subscribe(timelineFilter(), {
      onEvent: ev => { if (Store.saveEvent(ev)){ invalidateCounts(); needProfile(ev.pubkey); if (VIEW===view && (ev.kind===1||ev.kind===6)) _bufferLive(ev, fn); } },
      onEose: ()=>{ if(VIEW===view) _drawTimeline(false); }
    });
  }
  // Batched live updates: a busy global feed must NOT prepend + re-render per event (that pegged
  // the CPU and flashed). Buffer incoming notes and prepend them together a few times a second,
  // capping the feed and keeping scroll stable.
  let _liveBuf=[], _liveT=null, _liveFn=null, _livePending=[];
  const _LIVE_READ_PX=400;   // once scrolled this far down we stop auto-prepending (see below)
  function _bufferLive(ev, fn){ _liveFn=fn; _liveBuf.push(ev); if(!_liveT) _liveT=setTimeout(flushLive, 1800); }
  function flushLive(){
    _liveT=null; const evs=_liveBuf.splice(0);
    if((VIEW!=='home'&&VIEW!=='global') || !evs.length) return;
    const feed=$('#feed'); if(!feed) return;
    // While the user is reading below the top, DON'T mutate the timeline under them (prepending +
    // hydrating link cards shifts content and is what made it "keep refreshing"). Stash the new
    // posts and surface them with a "↑ N new posts" pill; flush when they scroll back up / tap it.
    if(feed.scrollTop > _LIVE_READ_PX){
      for(const ev of evs) _livePending.push(ev);
      if(_livePending.length>300) _livePending=_livePending.slice(-300);
      _updateNewPostsPill(); return;
    }
    _prependLive(evs, feed);
  }
  function _prependLive(evs, feed){
    const sp=feed.querySelector('.spinner'); if(sp)sp.remove(); const em=feed.querySelector('.empty'); if(em)em.remove();
    evs.sort((a,b)=>b.created_at-a.created_at);
    const frag=document.createDocumentFragment();
    for(const ev of evs){ if(ev.kind===1&&isReply(ev))continue; if(MUTED.has(ev.pubkey))continue; if(_liveFn&&!_liveFn(ev))continue;
      const dispId = ev.kind===6 ? ((ev.tags.find(t=>t[0]==='e')||[])[1]||ev.id) : ev.id;
      if(feed.querySelector('.note[data-id="'+dispId+'"]')) continue;   // don't double-insert
      const div=document.createElement('div'); div.innerHTML=noteHtml(ev); const node=div.firstElementChild; if(node) frag.appendChild(node); }
    if(!frag.childElementCount) return;
    const atTop=feed.scrollTop<100, beforeH=feed.scrollHeight;
    feed.insertBefore(frag, feed.firstChild);
    if(!atTop) feed.scrollTop += (feed.scrollHeight - beforeH);   // keep scroll stable on prepend
    // cap the feed at 200 — but NOT once the user has paginated older posts, or we'd delete the
    // scroll-back history they just loaded as soon as a new live note arrives at the top.
    if(_tl.pages===0){ const notes=[...feed.querySelectorAll('.note')]; for(let i=200;i<notes.length;i++) notes[i].remove(); }
    decorateProfiles(); hydrateLinkCards(feed);
  }
  // "new posts" pill — only on the live timelines; clicking it jumps to top and shows them
  function _newPostsPill(){
    let p=document.getElementById('new-posts-pill');
    if(!p){ p=document.createElement('button'); p.id='new-posts-pill'; p.className='new-posts-pill hidden';
      p.onclick=()=>{ const feed=$('#feed'); if(feed) feed.scrollTop=0; _flushPending(); };
      (document.querySelector('.main')||document.body).appendChild(p); }
    return p;
  }
  function _updateNewPostsPill(){
    const p=_newPostsPill(); const n=(VIEW==='home'||VIEW==='global')?_livePending.length:0;
    if(n>0){ p.textContent='↑ '+n+' new post'+(n>1?'s':''); p.classList.remove('hidden'); } else p.classList.add('hidden');
  }
  function _flushPending(){
    const feed=$('#feed'); if(!feed) return;
    const evs=_livePending.splice(0); if(evs.length) _prependLive(evs, feed);
    _updateNewPostsPill();
  }
  function _resetLive(){ _livePending=[]; _updateNewPostsPill(); }
  function _hidePill(){ const p=document.getElementById('new-posts-pill'); if(p) p.classList.add('hidden'); }
  function _drawTimeline(preserveScroll){
    if(VIEW!=='home' && VIEW!=='global') return;
    const feed=$('#feed'); if(!feed) return;
    const top=preserveScroll?feed.scrollTop:0;
    const fn = VIEW==='home' ? (e=>FOLLOWS.has(e.pubkey)) : null;
    const notes = Store.feed(e=>(!fn||fn(e))&&!MUTED.has(e.pubkey)).filter(e=>!isReply(e)).slice(0,200);
    feed.innerHTML = notes.length ? notes.map(noteHtml).join('') : `<div class="empty">No posts yet. ${VIEW==='home'?'Follow people or check Global.':''}</div>`;
    // seed the scroll-back cursor from the initial draw only — once the user has paged older, a late
    // EOSE redraw must NOT move the cursor forward (it would re-query an already-loaded range)
    if(notes.length && _tl.pages===0) _tl.oldest = notes[notes.length-1].created_at;
    hydrate(feed); if(preserveScroll) feed.scrollTop=top;
  }
  // ---------- infinite scroll-back ----------
  function onFeedScroll(){
    const feed=$('#feed'); if(!feed) return;
    // scrolled back near the top → show the buffered live posts and clear the pill
    if((VIEW==='home'||VIEW==='global') && _livePending.length && feed.scrollTop <= _LIVE_READ_PX) _flushPending();
    if(feed.scrollTop + feed.clientHeight < feed.scrollHeight - 700) return;   // not near the bottom yet
    if(VIEW==='home'||VIEW==='global') loadOlderTimeline();
    else if(VIEW==='profile') loadOlderProfile();
    else if(VIEW==='search') loadOlderSearch();
    else if(VIEW==='hashtag') loadOlderHashtag();
  }
  function loadSentinel(feed){
    let s=feed.querySelector('.load-sentinel'); if(s) return s;
    s=document.createElement('div'); s.className='load-sentinel'; s.innerHTML='<div class="spinner"></div>';
    feed.appendChild(s); return s;
  }
  function clearSentinel(feed){ const s=feed.querySelector('.load-sentinel'); if(s) s.remove(); }
  async function loadOlderTimeline(){
    if(_tl.loading || _tl.done || !_tl.oldest) return;
    _tl.loading=true; const view=VIEW; const feed=$('#feed'); loadSentinel(feed);
    const until=_tl.oldest;
    const filt = view==='home' ? [{ kinds:[1,6], authors:[...FOLLOWS], until:until-1, limit:50 }]
                               : [{ kinds:[1,6], until:until-1, limit:60 }];
    let evs=[]; try{ evs=await Relay.query(filt); }catch(_){}
    clearSentinel(feed);
    if(VIEW!==view){ _tl.loading=false; return; }   // user navigated away mid-fetch
    evs.sort((a,b)=>b.created_at-a.created_at);
    let minTs=until; const frag=document.createDocumentFragment();
    for(const ev of evs){
      Store.saveEvent(ev); needProfile(ev.pubkey);
      if(ev.created_at<minTs) minTs=ev.created_at;
      if(ev.kind===1 && isReply(ev)) continue;
      if(MUTED.has(ev.pubkey)) continue;
      if(view==='home' && !FOLLOWS.has(ev.pubkey)) continue;
      // a repost (kind 6) renders with the ORIGINAL's data-id, so dedupe against that, not the
      // repost's own id — otherwise a repost of an already-shown note appends a duplicate card.
      const dispId = ev.kind===6 ? ((ev.tags.find(t=>t[0]==='e')||[])[1]||ev.id) : ev.id;
      if(feed.querySelector('.note[data-id="'+dispId+'"]')) continue;   // already on screen
      const div=document.createElement('div'); div.innerHTML=noteHtml(ev); const node=div.firstElementChild; if(node) frag.appendChild(node);
    }
    invalidateCounts();
    if(frag.childElementCount){ feed.appendChild(frag); decorateProfiles(); hydrateLinkCards(feed); hydrateCounts(); }
    _tl.pages++;
    if(minTs<_tl.oldest) _tl.oldest=minTs;
    if(!evs.length || minTs>=until) _tl.done=true;   // relay returned nothing older → end of feed
    _tl.loading=false;
  }
  let _redrawT=null;
  function scheduleRedraw(){ if(_redrawT) return; _redrawT=setTimeout(()=>{ _redrawT=null; _drawTimeline(true); }, 350); }
  function isReply(ev){ return ev.kind===1 && ev.tags.some(t=>t[0]==='e'); }
  // True when a note carries inline media (image/video URL or a blossom 64-hex blob) — same
  // detection mediaParts() uses to pull a gallery out of the text. Drives the profile Media tab.
  function hasMedia(ev){
    if(ev.kind!==1) return false;
    return (ev.content||'').replace(/[)\].,!?]+$/,'').match(/(https?:\/\/[^\s<]+)/g)?.some(u=>{
      u=u.replace(/[)\].,!?]+$/,'');
      return /\.(jpe?g|png|gif|webp|avif|mp4|webm|mov|m4v)(\?|#|$)/i.test(u) || /\/[0-9a-f]{64}(\?|#|$)/i.test(u);
    }) || false;
  }
  function prependNote(ev, fn){
    if (ev.kind===1 && isReply(ev)) return;
    if (MUTED.has(ev.pubkey)) return;
    if (fn && !fn(ev)) return;
    const feed=$('#feed'); const sp=feed.querySelector('.spinner'); if(sp)sp.remove(); const em=feed.querySelector('.empty'); if(em)em.remove();
    const div=document.createElement('div'); div.innerHTML=noteHtml(ev); const node=div.firstElementChild;
    if(node){ feed.insertBefore(node, feed.firstChild); hydrate(node.parentElement); }
  }

  // ---------- bookmarks timeline ----------
  async function renderBookmarks(){
    const feed=$('#feed'); feed.innerHTML='<div class="spinner"></div>';
    const ids=[...BOOKMARKS];
    if(!ids.length){ feed.innerHTML='<div class="empty">No bookmarks yet. Tap 🔖 on a post to save it here.</div>'; return; }
    const missing=ids.filter(id=>!Store.get(id));
    if(missing.length){ try{ const evs=await Relay.query([{ ids:missing }]); evs.forEach(e=>{ Store.saveEvent(e); needProfile(e.pubkey); }); }catch(_){} }
    if(VIEW!=='bookmarks') return;   // user navigated away while fetching
    const notes=ids.map(id=>Store.get(id)).filter(Boolean).sort((a,b)=>b.created_at-a.created_at);
    feed.innerHTML = notes.length ? notes.map(noteHtml).join('')
      : '<div class="empty">Couldn\'t load your bookmarked posts from the relay.</div>';
    hydrate(feed);
  }

  // ---------- minimal, SAFE markdown renderer (for NIP-23 articles) ----------
  // Everything is HTML-escaped FIRST, so author content can't inject markup; the markdown
  // transforms then run over the escaped text. URLs in links/images are scheme-checked so a
  // `javascript:` payload can never become an href/src.
  function _mdUrl(u){ u=(u||'').trim(); return /^(https?:\/\/|\/)/i.test(u) ? u : ''; }
  function mdInline(s){
    s=s.replace(/`([^`]+)`/g,(m,c)=>`<code>${c}</code>`);
    s=s.replace(/!\[([^\]]*)\]\(([^)\s]+)(?:\s+&quot;[^)]*)?\)/g,(m,alt,url)=>{ const u=_mdUrl(url); return u?`<img src="${u}" alt="${alt}" loading="lazy">`:m; });
    s=s.replace(/\[([^\]]+)\]\(([^)\s]+)(?:\s+&quot;[^)]*)?\)/g,(m,txt,url)=>{ const u=_mdUrl(url); return u?`<a href="${u}" target="_blank" rel="noopener">${txt}</a>`:m; });
    s=s.replace(/\*\*([^*]+)\*\*/g,'<b>$1</b>').replace(/__([^_]+)__/g,'<b>$1</b>');
    s=s.replace(/(^|[^*])\*([^*\s][^*]*?)\*(?!\*)/g,'$1<i>$2</i>');
    s=s.replace(/(^|\s)_([^_\s][^_]*?)_(?=\s|$)/g,'$1<i>$2</i>');
    // bare URLs not already inside an href/src
    s=s.replace(/(^|[^"\/>=])(https?:\/\/[^\s<]+)/g,(m,pre,url)=>`${pre}<a href="${url}" target="_blank" rel="noopener">${url}</a>`);
    return s;
  }
  function mdToHtml(src){
    const lines=enc(src||'').split('\n'); let html='', i=0, para=[];
    const flush=()=>{ if(para.length){ html+='<p>'+mdInline(para.join('<br>'))+'</p>'; para=[]; } };
    while(i<lines.length){
      const ln=lines[i];
      if(/^```/.test(ln)){ flush(); i++; const code=[]; while(i<lines.length && !/^```/.test(lines[i])){ code.push(lines[i]); i++; } i++; html+='<pre><code>'+code.join('\n')+'</code></pre>'; continue; }
      const h=ln.match(/^(#{1,6})\s+(.*)$/); if(h){ flush(); const lvl=Math.min(h[1].length+1,6); html+=`<h${lvl}>${mdInline(h[2])}</h${lvl}>`; i++; continue; }
      if(/^\s*([-*_])\1\1+\s*$/.test(ln)){ flush(); html+='<hr>'; i++; continue; }
      if(/^\s*&gt;\s?/.test(ln)){ flush(); const q=[]; while(i<lines.length && /^\s*&gt;\s?/.test(lines[i])){ q.push(lines[i].replace(/^\s*&gt;\s?/,'')); i++; } html+='<blockquote>'+mdInline(q.join('<br>'))+'</blockquote>'; continue; }
      if(/^\s*[-*]\s+/.test(ln)){ flush(); const it=[]; while(i<lines.length && /^\s*[-*]\s+/.test(lines[i])){ it.push('<li>'+mdInline(lines[i].replace(/^\s*[-*]\s+/,''))+'</li>'); i++; } html+='<ul>'+it.join('')+'</ul>'; continue; }
      if(/^\s*\d+\.\s+/.test(ln)){ flush(); const it=[]; while(i<lines.length && /^\s*\d+\.\s+/.test(lines[i])){ it.push('<li>'+mdInline(lines[i].replace(/^\s*\d+\.\s+/,''))+'</li>'); i++; } html+='<ol>'+it.join('')+'</ol>'; continue; }
      if(/^\s*$/.test(ln)){ flush(); i++; continue; }
      para.push(ln); i++;
    }
    flush(); return html;
  }

  // ---------- long-form articles (NIP-23, kind 30023) ----------
  function artTime(e){ const p=parseInt((e.tags.find(t=>t[0]==='published_at')||[])[1],10); return p||e.created_at; }
  // collapse to the newest event per (pubkey, d-tag) — 30023 is a parameterized replaceable event
  function _dedupAddr(evs){
    const m=new Map();
    for(const e of evs){ const d=(e.tags.find(t=>t[0]==='d')||[])[1]||''; const k=e.pubkey+':'+d; const cur=m.get(k); if(!cur||cur.created_at<e.created_at) m.set(k,e); }
    return [...m.values()];
  }
  async function renderArticles(){
    const feed=$('#feed');
    feed.innerHTML=`<div class="art-top"><button class="btn btn-neon small" id="art-new">✎ Write article</button></div><div id="art-list"><div class="spinner"></div></div>`;
    $('#art-new').onclick=()=>renderArticleEditor();
    let evs=[]; try{ evs=await Relay.query([{ kinds:[30023], limit:80 }]); }catch(_){}
    evs.forEach(e=>{ Store.saveEvent(e); needProfile(e.pubkey); });
    if(VIEW!=='articles') return;
    const arts=_dedupAddr(evs).sort((a,b)=>artTime(b)-artTime(a));
    const list=$('#art-list'); if(!list) return;
    list.innerHTML = arts.length ? arts.map(articleCard).join('') : '<div class="empty">No articles yet. Tap “Write article” to publish the first one.</div>';
    decorateProfiles();
    $$('.article-card',list).forEach(c=> c.onclick=ev=>{ if(ev.target.closest('[data-prof]')){ renderProfileView(c.dataset.pk); return; } const a=Store.get(c.dataset.id); if(a) openArticle(a); });
  }
  function articleCard(e){
    const p=profOf(e.pubkey); needProfile(e.pubkey);
    const title=(e.tags.find(t=>t[0]==='title')||[])[1]||'(untitled)';
    const summary=(e.tags.find(t=>t[0]==='summary')||[])[1]||'';
    const img=(e.tags.find(t=>t[0]==='image')||[])[1]||'';
    return `<article class="article-card" data-id="${e.id}" data-pk="${e.pubkey}">
      ${img?`<img class="art-img" src="${enc(img)}" loading="lazy" onerror="this.remove()">`:''}
      <div class="art-meta"><h3 class="art-title">${enc(title)}</h3>
        ${summary?`<div class="art-sum">${enc(summary.slice(0,200))}</div>`:''}
        <div class="art-by"><img class="art-av" src="${enc(p.picture||LOGO)}" onerror="this.src='${LOGO}'"><span class="name" data-prof="${e.pubkey}">${enc(p.name||p.display_name||'anon')}</span><span class="muted small">· ${timeAgo(artTime(e))}</span></div>
      </div></article>`;
  }
  function openArticle(e){
    VIEW='article'; $$('.nav-item[data-view]').forEach(b=>b.classList.remove('active')); $('#view-title').textContent='Article';
    const feed=$('#feed'); const p=profOf(e.pubkey); needProfile(e.pubkey);
    const title=(e.tags.find(t=>t[0]==='title')||[])[1]||'(untitled)';
    const img=(e.tags.find(t=>t[0]==='image')||[])[1]||'';
    const mine=e.pubkey===ME.pubkey;
    feed.innerHTML=`<div class="article-view">
      <button class="btn btn-ghost small" id="art-back">← Articles</button>
      ${img?`<img class="av-banner" src="${enc(img)}" onerror="this.remove()">`:''}
      <h1 class="av-title">${enc(title)}</h1>
      <div class="av-by"><img class="art-av" src="${enc(p.picture||LOGO)}" onerror="this.src='${LOGO}'"><span class="name" data-prof="${e.pubkey}">${enc(p.name||p.display_name||'anon')}</span><span class="muted small">· ${timeAgo(artTime(e))}</span></div>
      <div class="av-actions">
        <button class="act actb ${BOOKMARKS.has(e.id)?'on':''}" id="av-bm" title="bookmark">🔖</button>
        <button class="act actz" id="av-zap" title="zap">⚡</button>
        ${mine?`<button class="act" id="av-edit" title="edit">✏</button>`:''}
        <button class="act" id="av-copy" title="copy link">🔗</button>
      </div>
      <div class="markdown av-body">${mdToHtml(e.content)}</div>
    </div>`;
    $('#art-back').onclick=()=>switchView('articles');
    $('#av-bm').onclick=ev=>toggleBookmark(e.id, ev.currentTarget);
    $('#av-zap').onclick=()=>doZap(e.id, e.pubkey);
    { const ed=$('#av-edit'); if(ed) ed.onclick=()=>renderArticleEditor(e); }
    $('#av-copy').onclick=()=>{ try{ const naddr=NT().nip19.naddrEncode({ identifier:(e.tags.find(t=>t[0]==='d')||[])[1]||'', pubkey:e.pubkey, kind:30023 }); navigator.clipboard.writeText('nostr:'+naddr); toast('article link copied'); }catch(_){ navigator.clipboard.writeText(e.id); toast('id copied'); } };
    feed.querySelectorAll('[data-prof]').forEach(el=> el.onclick=()=>renderProfileView(el.dataset.prof));
    feed.querySelectorAll('.markdown img').forEach(im=> im.onclick=()=>openLightbox(im.currentSrc||im.src));
    decorateProfiles();
  }
  function _insertAt(ta, text){ const s=ta.selectionStart||0, en=ta.selectionEnd||0; ta.value=ta.value.slice(0,s)+text+ta.value.slice(en); const c=s+text.length; ta.selectionStart=ta.selectionEnd=c; ta.focus(); }
  function renderArticleEditor(existing){
    VIEW='article'; $$('.nav-item[data-view]').forEach(b=>b.classList.remove('active')); $('#view-title').textContent=existing?'Edit article':'Write article';
    const feed=$('#feed'); const g=(k)=>existing?((existing.tags.find(t=>t[0]===k)||[])[1]||''):'';
    feed.innerHTML=`<div class="article-editor">
      <button class="btn btn-ghost small" id="ae-back">← Cancel</button>
      <label class="fld">Title<input class="input" id="ae-title" placeholder="Article title" value="${enc(g('title'))}"></label>
      <label class="fld">Summary<input class="input" id="ae-sum" placeholder="One-line summary (optional)" value="${enc(g('summary'))}"></label>
      <label class="fld">Header image<div class="row"><input class="input" id="ae-img" placeholder="https://… (optional)" value="${enc(g('image'))}"><button class="btn btn-ghost small" id="ae-img-up">🖼 Upload</button></div></label>
      <input type="file" id="ae-img-file" accept="image/*" hidden>
      <div class="cmp-tabs"><button class="cmp-tab active" data-t="write">Write</button><button class="cmp-tab" data-t="preview">👁 Preview</button></div>
      <div class="row cmp-tools"><button class="btn btn-ghost small" id="ae-insert">📎 Insert image</button><input type="file" id="ae-body-file" accept="image/*" multiple hidden><span class="spacer"></span><span class="muted small">Markdown</span></div>
      <textarea id="ae-body" class="article-body" placeholder="Write your article in markdown…">${enc(existing?existing.content:'')}</textarea>
      <div id="ae-preview" class="markdown article-preview hidden"></div>
      <div class="row"><span class="muted small" id="ae-status"></span><span class="spacer"></span><button class="btn btn-neon" id="ae-pub">Publish ▶</button></div>
    </div>`;
    $('#ae-back').onclick=()=>switchView('articles');
    const body=$('#ae-body');
    $$('.cmp-tab',feed).forEach(b=> b.onclick=()=>{ $$('.cmp-tab',feed).forEach(x=>x.classList.toggle('active',x===b)); const pv=b.dataset.t==='preview'; body.classList.toggle('hidden',pv); const prev=$('#ae-preview'); prev.classList.toggle('hidden',!pv); if(pv) prev.innerHTML=mdToHtml(body.value)||'<div class="muted small">Nothing to preview.</div>'; });
    $('#ae-img-up').onclick=()=>$('#ae-img-file').click();
    $('#ae-img-file').onchange=async ev=>{ const f=ev.target.files[0]; if(!f)return; $('#ae-status').textContent='uploading image…'; try{ $('#ae-img').value=await uploadBlob(f); $('#ae-status').textContent='image uploaded'; }catch(err){ $('#ae-status').textContent='upload failed: '+err.message; } };
    $('#ae-insert').onclick=()=>$('#ae-body-file').click();
    $('#ae-body-file').onchange=async ev=>{ const files=[...ev.target.files]; for(let i=0;i<files.length;i++){ $('#ae-status').textContent=`uploading ${i+1}/${files.length}…`; try{ const url=await uploadBlob(files[i]); _insertAt(body, `\n![](${url})\n`); }catch(err){ $('#ae-status').textContent='upload failed: '+err.message; return; } } $('#ae-status').textContent=''; ev.target.value=''; };
    $('#ae-pub').onclick=()=>publishArticle({ title:$('#ae-title').value.trim(), summary:$('#ae-sum').value.trim(), image:$('#ae-img').value.trim(), body:body.value, d:g('d') });
  }
  async function publishArticle({title, summary, image, body, d}){
    if(!title){ toast('add a title'); return; }
    if(!body.trim()){ toast('write something first'); return; }
    const slug = d || ((title.toLowerCase().replace(/[^a-z0-9]+/g,'-').replace(/^-+|-+$/g,'').slice(0,60) || 'post') + '-' + Math.random().toString(36).slice(2,7));
    const tags=[['d',slug],['title',title],['published_at',String(Math.floor(Date.now()/1000))]];
    if(summary) tags.push(['summary',summary]);
    if(image) tags.push(['image',image]);
    mentionTags(body).forEach(t=>{ if(!tags.some(x=>x[0]==='p'&&x[1]===t[1])) tags.push(t); });
    $('#ae-status') && ($('#ae-status').textContent='publishing…');
    try{ const r=await publish(30023, body, tags); if(r && r.ok===false){ toast('relay: '+(r.msg||'rejected')); if($('#ae-status'))$('#ae-status').textContent=''; } else { toast('article published'); switchView('articles'); } }
    catch(e){ toast('publish failed: '+e.message); }
  }

  // ---------- live streams (NIP-53 Live Activities, kind 30311) ----------
  function streamStatus(e){ return ((e.tags.find(t=>t[0]==='status')||[])[1]||'').toLowerCase(); }
  function streamHost(e){ const h=e.tags.find(t=>t[0]==='p'&&(t[3]||'').toLowerCase()==='host'); return (h&&h[1])||e.pubkey; }
  async function renderStreams(){
    const feed=$('#feed'); feed.innerHTML='<div class="spinner"></div>';
    let evs=[]; try{ evs=await Relay.query([{ kinds:[30311], limit:80 }]); }catch(_){}
    evs.forEach(e=>{ Store.saveEvent(e); needProfile(e.pubkey); });
    if(VIEW!=='streams') return;
    const rank=e=>({live:0,planned:1,ended:2}[streamStatus(e)] ?? 3);
    const streams=_dedupAddr(evs).sort((a,b)=> rank(a)-rank(b) || b.created_at-a.created_at);
    feed.innerHTML = streams.length ? `<div class="stream-grid">${streams.map(streamCard).join('')}</div>` : '<div class="empty">No streams found yet.</div>';
    decorateProfiles();
    $$('.stream-card',feed).forEach(c=> c.onclick=ev=>{ if(ev.target.closest('[data-prof]')){ renderProfileView(c.dataset.pk); return; } const s=Store.get(c.dataset.id); if(s) openStream(s); });
  }
  function streamCard(e){
    const hpk=streamHost(e); const p=profOf(hpk); needProfile(hpk);
    const title=(e.tags.find(t=>t[0]==='title')||[])[1]||'(untitled stream)';
    const img=(e.tags.find(t=>t[0]==='image')||[])[1]||(e.tags.find(t=>t[0]==='thumbnail')||[])[1]||'';
    const st=streamStatus(e);
    const badge = st==='live'?'<span class="live-badge">● LIVE</span>' : st==='ended'?'<span class="ended-badge">ended</span>' : st==='planned'?'<span class="planned-badge">soon</span>' : '';
    const viewers=(e.tags.find(t=>t[0]==='current_participants')||[])[1];
    return `<article class="stream-card" data-id="${e.id}" data-pk="${hpk}">
      <div class="stream-thumb">${img?`<img src="${enc(img)}" loading="lazy" onerror="this.parentElement.classList.add('noimg')">`:'<span class="stream-play">▶</span>'}${badge}</div>
      <div class="stream-meta"><div class="stream-title">${enc(title)}</div>
        <div class="art-by"><img class="art-av" src="${enc(p.picture||LOGO)}" onerror="this.src='${LOGO}'"><span class="name" data-prof="${hpk}">${enc(p.name||p.display_name||'anon')}</span>${viewers?`<span class="muted small">· ${enc(viewers)} watching</span>`:''}</div>
      </div></article>`;
  }
  function openStream(e){
    VIEW='stream'; $$('.nav-item[data-view]').forEach(b=>b.classList.remove('active')); $('#view-title').textContent='Stream';
    const feed=$('#feed'); const hpk=streamHost(e); const p=profOf(hpk); needProfile(hpk);
    const title=(e.tags.find(t=>t[0]==='title')||[])[1]||'(untitled stream)';
    const summary=(e.tags.find(t=>t[0]==='summary')||[])[1]||'';
    const url=(e.tags.find(t=>t[0]==='streaming')||[])[1]||(e.tags.find(t=>t[0]==='recording')||[])[1]||'';
    const st=streamStatus(e);
    feed.innerHTML=`<div class="stream-view">
      <button class="btn btn-ghost small" id="st-back">← Streams</button>
      <h1 class="av-title">${enc(title)}${st==='live'?' <span class="live-badge">● LIVE</span>':''}</h1>
      <div class="av-by"><img class="art-av" src="${enc(p.picture||LOGO)}" onerror="this.src='${LOGO}'"><span class="name" data-prof="${hpk}">${enc(p.name||p.display_name||'anon')}</span>${st?`<span class="muted small">· ${enc(st)}</span>`:''}</div>
      ${url?`<video class="stream-player" id="st-video" controls playsinline></video>
        <div class="muted small" id="st-note"></div>
        <div class="row">${isDesktop()?`<button class="btn btn-ghost small" id="st-pop">⧉ Pop out player</button>`:''}<a class="btn btn-ghost small" href="${enc(url)}" target="_blank" rel="noopener">▶ Open stream URL</a></div>`:'<div class="empty">No stream URL provided.</div>'}
      ${summary?`<div class="about">${linkify(summary)}</div>`:''}
    </div>`;
    $('#st-back').onclick=()=>switchView('streams');
    feed.querySelectorAll('[data-prof]').forEach(el=> el.onclick=()=>renderProfileView(el.dataset.prof));
    decorateProfiles();
    if(url) attachStream(url);
    { const pb=$('#st-pop'); if(pb) pb.onclick=()=>popOutStream(e); }
  }
  // ---------- floating mini-player: keep a stream playing while you browse other views ----------
  // Moving the live <video> node (with its attached hls.js) OUT of #feed and into a fixed,
  // persistent container means a feed re-render can't kill it — playback simply continues.
  // _streamHls = the hls bound to the INLINE player; _miniHls = the one handed to the floating
  // mini-player. Keeping them separate means a feed re-render (cleanupInlineStream) can't tear down
  // a popped-out stream, and closing the mini can't tear down a newer inline stream.
  let _streamHls=null, _miniHls=null, _miniEv=null;
  function cleanupInlineStream(){ if(_streamHls){ try{ _streamHls.destroy(); }catch(_){} _streamHls=null; } }
  function popOutStream(ev){
    const v=$('#st-video'); if(!v) return;
    closeMini();   // only one mini at a time
    let mp=$('#mini-player'); if(!mp){ mp=document.createElement('div'); mp.id='mini-player'; mp.className='mini-player'; document.body.appendChild(mp); }
    const title=(ev.tags.find(t=>t[0]==='title')||[])[1]||'stream';
    mp.innerHTML=`<div class="mini-bar"><span class="mini-title">▶ ${enc(title)}</span><button class="mini-x" id="mini-open" title="back to stream">⤢</button><button class="mini-x" id="mini-close" title="close">✕</button></div>`;
    mp.appendChild(v); v.removeAttribute('id');   // MOVE the playing element (hls stays attached → no interruption)
    _miniHls=_streamHls; _streamHls=null;          // hand hls ownership to the mini
    _miniEv=ev; mp.classList.add('on');
    $('#mini-close').onclick=()=>closeMini();
    $('#mini-open').onclick=()=>{ const e2=_miniEv; closeMini(); if(e2) openStream(e2); };
    v.play().catch(()=>{});
    toast('stream popped out — keeps playing while you browse');
  }
  function closeMini(){
    const mp=$('#mini-player'); if(!mp) return;
    if(_miniHls){ try{ _miniHls.destroy(); }catch(_){} _miniHls=null; }
    _miniEv=null; mp.classList.remove('on'); mp.innerHTML='';
  }
  // lazy-load the vendored hls.js only when a stream is actually opened (it's ~400 KB)
  let _hlsP=null;
  function loadHls(){
    if(window.Hls) return Promise.resolve();
    if(_hlsP) return _hlsP;
    _hlsP=new Promise((res,rej)=>{ const s=document.createElement('script'); s.src='/static/vendor/hls/hls.min.js'; s.onload=()=>res(); s.onerror=()=>rej(new Error('hls load failed')); document.head.appendChild(s); });
    return _hlsP;
  }
  // Most Nostr streams are HLS (.m3u8). Chrome/Firefox can't play that natively ("invalid MIME
  // type") — only Safari can — so route HLS through hls.js; play everything else (mp4/webm
  // recordings, native-HLS Safari) straight off the <video> src.
  function attachStream(url){
    const v=$('#st-video'); if(!v) return;
    cleanupInlineStream();   // drop any previous inline hls before attaching a new one
    const isHls=/\.m3u8(\?|#|$)/i.test(url);
    if(isHls && !v.canPlayType('application/vnd.apple.mpegurl')){
      loadHls().then(()=>{
        if(window.Hls && window.Hls.isSupported()){
          const h=new window.Hls({ maxBufferLength:30 }); _streamHls=h;
          h.loadSource(url); h.attachMedia(v);
          h.on(window.Hls.Events.ERROR,(_e,d)=>{ if(d&&d.fatal){ const n=$('#st-note'); if(n) n.textContent='Could not play this stream here — try the “Open stream URL” link below.'; } });
        } else { v.src=url; }
      }).catch(()=>{ v.src=url; });
    } else {
      v.src=url;
    }
  }

  // ---------- note rendering ----------
  function profOf(pk){ return Store.profile(pk)||{}; }
  function noteHtml(ev){
    if (ev.kind===6){  // repost
      let inner=null; try{ inner=JSON.parse(ev.content); }catch(_){}
      if(inner && inner.id) Store.saveEvent(inner);
      const origId=(ev.tags.find(t=>t[0]==='e')||[])[1];
      const orig = inner || Store.get(origId);
      const rp = profOf(ev.pubkey); needProfile(ev.pubkey);
      if(orig){ needProfile(orig.pubkey); return noteCard(orig, `<div class="repost-tag">🔁 ${enc(rp.name||'someone')} reposted</div>`); }
      needEvent(origId);   // fetch the original; flushEvents patches this placeholder in place
      return `<article class="note" data-orig="${origId}" data-reposter="${enc(rp.name||'someone')}"><div class="body"><div class="repost-tag">🔁 ${enc(rp.name||'someone')} reposted</div><div class="muted small">loading post…</div></div></article>`;
    }
    return noteCard(ev);
  }
  // Pull media URLs OUT of the text into a flex gallery — leaving them inline let the post's
  // newlines (white-space:pre-wrap) break each onto its own row (vertical stacking).
  function mediaParts(raw){
    const media=[];
    const text=(raw||'').replace(/(https?:\/\/[^\s<]+)/g,(url)=>{
      const u=url.replace(/[)\].,!?]+$/,''); const tail=url.slice(u.length); const E=enc(u);
      if(/\.(jpe?g|png|gif|webp|avif)(\?|#|$)/i.test(u)){ media.push(`<img src="${E}" loading="lazy">`); return tail; }
      if(/\.(mp4|webm|mov|m4v)(\?|#|$)/i.test(u)){ media.push(`<video src="${E}" controls preload="metadata" playsinline></video>`); return tail; }
      if(/\/[0-9a-f]{64}(\?|#|$)/i.test(u)){ media.push(`<img src="${E}" loading="lazy" onerror="this.onerror=null;window.__blobFallback(this);">`); return tail; }
      return url;  // non-media URL: leave for linkify
    });
    return { text, gallery: media.length?`<div class="media-row">${media.join('')}</div>`:'' };
  }
  function noteCard(ev, prefix=''){
    const p = profOf(ev.pubkey); needProfile(ev.pubkey);
    const mp = mediaParts(ev.content);
    const name = p.name||p.display_name||(NT().nip19.npubEncode(ev.pubkey).slice(0,12)+'…');
    const av = p.picture || LOGO;
    const handle = niceNip05(p.nip05) || ('@'+NT().nip19.npubEncode(ev.pubkey).slice(4,12));
    const counts = countsFor(ev.id);
    const liked = myReaction(ev.id);
    const mine = ev.pubkey===ME.pubkey;
    return `<article class="note" data-id="${ev.id}" data-pk="${ev.pubkey}">
      <img class="av" src="${enc(av)}" onerror="this.src='${LOGO}'">
      <div class="body">${prefix}
        <div class="hd"><span class="name" data-prof="${ev.pubkey}">${enc(name)}</span><span class="vchk"></span>
          <span class="handle">${enc(handle)}</span><span class="time">${timeAgo(ev.created_at)}</span></div>
        <div class="txt">${linkify(stripQuoteRef(mp.text, ev))}</div>
        ${mp.gallery}
        ${linkCardHtml(mp.text)}
        ${quoteHtml(ev)}
        <div class="acts">
          <button class="act" data-a="reply" title="reply">💬 <span class="n">${counts.replies||''}</span></button>
          <button class="act rt ${counts.iRt?'on':''}" data-a="repost" title="repost">🔁 <span class="n">${counts.reposts||''}</span></button>
          <button class="act actq" data-a="quote" title="quote post">❝</button>
          <button class="act ${liked?'on':''}" data-a="react" title="react">${liked||'😀'} <span class="n">${counts.reactions||''}</span></button>
          <button class="act actz ${counts.zaps?'on':''}" data-a="zap" title="zap (lightning)">⚡ <span class="n">${counts.zaps?fmtSats(counts.zaps):''}</span></button>
          <button class="act actm ${BOOKMARKS.has(ev.id)?'on':''}" data-a="menu" title="more">☰</button>
        </div>
      </div></article>`;
  }
  // A NIP-18 quote post carries both a `q` tag (rendered by quoteHtml) AND usually the same
  // nostr:nevent inline — strip the inline one so the quoted note doesn't embed twice.
  function stripQuoteRef(text, ev){
    const q=(ev.tags.find(t=>t[0]==='q')||[])[1]; if(!q) return text;
    return (text||'').replace(/(?:nostr:)?(?:nevent1|note1)[0-9a-z]{20,}/gi, m=>{
      try{ const d=NT().nip19.decode(m.replace(/^nostr:/i,'')); const id=d.type==='note'?d.data:(d.data&&d.data.id); return id===q?'':m; }catch(_){ return m; }
    }).replace(/[ \t]+\n/g,'\n').replace(/\n{3,}/g,'\n\n').trim();
  }
  // Open an addressable event (naddr): articles (k30023) open in the reader, else as a thread.
  async function openNaddr(pk, d, kind){
    kind=parseInt(kind,10)||0;
    const filt={ authors:[pk], kinds:[kind] }; if(d) filt['#d']=[d];
    let evs=[]; try{ evs=await Relay.query([filt]); }catch(_){}
    const ev=evs.sort((a,b)=>b.created_at-a.created_at)[0];
    if(!ev){ toast('referenced post not found on the relay'); return; }
    Store.saveEvent(ev); needProfile(ev.pubkey);
    if(kind===30023) openArticle(ev); else renderThread(ev.id);
  }
  function quoteHtml(ev){
    const q=(ev.tags.find(t=>t[0]==='q')||[])[1]; if(!q) return '';
    const o=Store.get(q); if(!o){ needEvent(q); return `<div class="quoted muted small" data-qload="${q}">quoted post loading…</div>`; }
    return quotedDiv(o);
  }
  function quotedDiv(o){ const p=profOf(o.pubkey); needProfile(o.pubkey);
    return `<div class="quoted" data-open="${o.id}"><div class="hd"><b>${enc(p.name||'anon')}</b> <span class="handle">${timeAgo(o.created_at)}</span></div><div class="txt">${linkify(o.content)}</div></div>`; }
  const _evQ=new Set(); let _evT=null;
  function needEvent(id){ if(id&&!Store.get(id)){ _evQ.add(id); if(!_evT)_evT=setTimeout(flushEvents,150);} }
  async function flushEvents(){
    _evT=null; const ids=[..._evQ]; _evQ.clear(); if(!ids.length) return;
    const evs=await Relay.query([{ids}]);
    for(const e of evs){ Store.saveEvent(e); needProfile(e.pubkey); patchLoaded(e); }
    decorateProfiles();
  }
  // Patch repost/quote placeholders in place when their referenced event loads — NO full feed
  // re-render (that flashed the whole screen on the busy global feed).
  function patchLoaded(e){
    $$(`.note[data-orig="${e.id}"]`).forEach(el=>{
      const div=document.createElement('div'); div.innerHTML=noteCard(e, `<div class="repost-tag">🔁 ${enc(el.dataset.reposter||'someone')} reposted</div>`);
      if(div.firstElementChild) el.replaceWith(div.firstElementChild);
    });
    $$(`[data-qload="${e.id}"]`).forEach(el=>{
      const div=document.createElement('div'); div.innerHTML=quotedDiv(e);
      if(div.firstElementChild) el.replaceWith(div.firstElementChild);
    });
  }

  // reaction/repost counts — built ONCE per render pass (single scan of the store) instead of
  // re-scanning the whole store for every rendered note (was O(notes × store)).
  let CIDX = null;
  function invalidateCounts(){ CIDX = null; }
  function buildCounts(){
    const c = { replies:{}, reactions:{}, reposts:{}, zaps:{}, zapN:{}, myRt:new Set(), myReact:{} };
    const lastE = e => { for(let i=e.tags.length-1;i>=0;i--) if(e.tags[i][0]==='e') return e.tags[i][1]; return null; };
    for(const e of Store.all()){
      const id = lastE(e); if(!id) continue;
      if(e.kind===1) c.replies[id]=(c.replies[id]||0)+1;
      else if(e.kind===7){ c.reactions[id]=(c.reactions[id]||0)+1; if(e.pubkey===ME.pubkey) c.myReact[id]=(e.content==='+'||e.content===''?'❤️':e.content); }
      else if(e.kind===6){ c.reposts[id]=(c.reposts[id]||0)+1; if(e.pubkey===ME.pubkey) c.myRt.add(id); }
      else if(e.kind===9735){ const sats=zapAmount(e); if(sats){ c.zaps[id]=(c.zaps[id]||0)+sats; c.zapN[id]=(c.zapN[id]||0)+1; } }
    }
    CIDX = c;
  }
  function countsFor(id){ if(!CIDX) buildCounts(); return { replies:CIDX.replies[id]||0, reactions:CIDX.reactions[id]||0, reposts:CIDX.reposts[id]||0, zaps:CIDX.zaps[id]||0, zapN:CIDX.zapN[id]||0, iRt:CIDX.myRt.has(id) }; }
  function myReaction(id){ if(!CIDX) buildCounts(); return CIDX.myReact[id]||null; }
  // (reaction display: '+' shows as ❤️, custom emoji shown as-is — see buildCounts/pickEmoji)

  // ---------- interactions ----------
  function bindFeedActions(){
    $('#feed').addEventListener('click', async (e)=>{
      if(e.target.closest('.yt-embed')) return;  // YouTube facade → handled by the player loader; don't lightbox the thumb
      const mn=e.target.closest('.mention'); if(mn){ e.preventDefault(); const pk=safePk(mn.dataset.np); if(pk) renderProfileView(pk); return; }
      const evl=e.target.closest('.evlink'); if(evl){ e.preventDefault(); renderThread(evl.dataset.ev); return; }
      const ht=e.target.closest('.hashtag'); if(ht){ e.preventDefault(); renderHashtag(ht.dataset.tag); return; }
      const na=e.target.closest('.naddrlink'); if(na){ e.preventDefault(); openNaddr(na.dataset.pk, na.dataset.d, na.dataset.k); return; }
      const im=e.target.closest('.txt img, .note-preview img, .media-row img, .media-grid img'); if(im){ e.preventDefault(); openLightbox(im.currentSrc||im.src); return; }
      const av=e.target.closest('.av'); if(av){ const n=e.target.closest('.note'); if(n){ renderProfileView(n.dataset.pk); return; } }
      const prof=e.target.closest('[data-prof]'); if(prof){ renderProfileView(prof.dataset.prof); return; }
      const q=e.target.closest('[data-open]'); if(q){ openThread(q.dataset.open); return; }
      const btn=e.target.closest('.act');
      const art=e.target.closest('.note');
      // Click anywhere else on the card body opens the post's thread, so the user doesn't have to
      // aim for the timestamp. Skip clicks on attachments / links / form controls (images already
      // returned above as a lightbox; video & co. must keep their own controls), and skip when the
      // user just drag-SELECTED text (so highlight-to-copy works instead of opening the thread).
      const hasSelection = window.getSelection && String(window.getSelection()).length>0;
      if(!btn){ if(art && !hasSelection && !e.target.closest('a,video,audio,button,input,textarea,select,label,.media-row,.media-grid,.link-card')) renderThread(art.dataset.id); return; }
      if(!art) return;   // .act outside a note (article/stream view) binds its own handler
      const id=art.dataset.id; const pk=art.dataset.pk;
      const a=btn.dataset.a;
      if(a==='react') return pickEmoji(id,pk,btn);
      if(a==='repost') return doRepost(id,pk,btn);
      if(a==='quote') return compose({quote:id});
      if(a==='reply') return compose({reply:id, replyPk:pk});
      if(a==='delete') return doDelete(id,art);
      if(a==='zap') return doZap(id,pk);
      if(a==='bookmark') return toggleBookmark(id,btn);
      if(a==='copyid'){ try{ navigator.clipboard.writeText(NT().nip19.noteEncode(id)); toast('note id copied'); }catch(_){ navigator.clipboard.writeText(id); toast('event id copied'); } return; }
      if(a==='pin') return togglePin(id);
      if(a==='block') return doBlock(pk);
      if(a==='menu') return openPostMenu(id, pk, art, btn);
    });
  }
  // ---------- zaps (NIP-57 lightning) ----------
  // sats in a zap RECEIPT (kind 9735): trust the embedded zap-request `amount` tag (millisats)
  // first — it's what the sender asked to pay — then fall back to decoding the bolt11 invoice.
  function zapAmount(ev){
    try{
      const desc=(ev.tags.find(t=>t[0]==='description')||[])[1];
      if(desc){ const zr=JSON.parse(desc); const a=(zr.tags||[]).find(t=>t[0]==='amount'); if(a&&a[1]){ const s=Math.round(parseInt(a[1],10)/1000); if(s>0) return s; } }
    }catch(_){}
    const b=(ev.tags.find(t=>t[0]==='bolt11')||[])[1]; if(b){ const s=bolt11Sats(b); if(s>0) return s; }
    const a=(ev.tags.find(t=>t[0]==='amount')||[])[1]; if(a){ const s=Math.round(parseInt(a,10)/1000); if(s>0) return s; }
    return 0;
  }
  // decode the amount out of a BOLT11 invoice HRP (lnbc<amount><multiplier>) → sats
  function bolt11Sats(inv){
    // require the '1' separator after the optional multiplier so an amountless invoice (lnbc1…)
    // doesn't misparse its separator digit as the amount
    try{ const m=/^ln(?:bc|tb|bcrt)(\d+)([munp])?1/i.exec(String(inv).trim().toLowerCase()); if(!m) return 0;
      const num=parseInt(m[1],10); const map={m:1e-3,u:1e-6,n:1e-9,p:1e-12};
      const btc = m[2] ? num*map[m[2]] : num; return Math.round(btc*1e8);
    }catch(_){ return 0; }
  }
  // the zapper (sender) pubkey — the receipt is signed by the LNURL server, so read the request
  function zapSender(ev){
    try{ const desc=(ev.tags.find(t=>t[0]==='description')||[])[1]; if(desc){ const zr=JSON.parse(desc); if(zr.pubkey) return zr.pubkey; } }catch(_){}
    return (ev.tags.find(t=>t[0]==='P')||[])[1] || null;
  }
  function fmtSats(n){ n=n||0; if(n>=1000000) return (n/1000000).toFixed(n>=10000000?0:1).replace(/\.0$/,'')+'M'; if(n>=1000) return (n/1000).toFixed(n>=10000?0:1).replace(/\.0$/,'')+'k'; return String(n); }
  // direct fetch first (most LNURL endpoints set CORS); on failure fall back to the node's proxy
  // (handles services that DON'T send CORS headers, same fix as NIP-05 verification).
  async function corsJson(url){
    try{ const r=await fetch(url); if(r.ok) return await r.json(); }catch(_){}
    try{ const r=await fetch('/client/lnurl?url='+encodeURIComponent(url)); if(r.ok) return await r.json(); }catch(_){}
    return null;
  }
  async function lnurlResolve(addr){
    addr=(addr||'').trim(); if(!addr) return null;
    let url=null;
    if(addr.includes('@')){ const [name,domain]=addr.split('@'); url=`https://${domain}/.well-known/lnurlp/${encodeURIComponent(name)}`; }
    else if(/^lnurl1/i.test(addr)){ try{ const d=NT().nip19.decode(addr); url=d&&d.data; }catch(_){ try{ url=new TextDecoder().decode(bech32ToBytes(addr)); }catch(__){} } }
    if(!url) return null;
    const j=await corsJson(url); if(!j) return null;
    return { callback:j.callback, allowsNostr:!!j.allowsNostr, min:j.minSendable, max:j.maxSendable };
  }
  async function doZap(noteId, pk){
    const p=profOf(pk); const addr=p.lud16||p.lud06;
    if(!addr){ toast('no lightning address on this profile'); return; }
    const amt=parseInt(prompt('Zap amount (sats):','21')||'0',10); if(!amt||amt<1) return;
    toast('preparing zap…');
    try{
      const lnurl=await lnurlResolve(addr);
      if(!lnurl||!lnurl.callback){ toast('couldn\'t resolve '+addr); return; }
      const msat=amt*1000;
      let url=lnurl.callback+(lnurl.callback.includes('?')?'&':'?')+'amount='+msat;
      if(lnurl.allowsNostr){
        const zr=await sign(9734,'',[['relays',CFG.relay_url||''],['amount',String(msat)],['p',pk]].concat(noteId?[['e',noteId]]:[]));
        url+='&nostr='+encodeURIComponent(JSON.stringify(zr));
      }
      const inv=await corsJson(url);
      const pr=inv && inv.pr; if(!pr){ toast('no invoice'+(inv&&inv.reason?': '+inv.reason:'')); return; }
      if(window.webln){ try{ await window.webln.enable(); await window.webln.sendPayment(pr); toast('⚡ zapped '+amt+' sats'); return; }catch(e){} }
      invoiceModal(pr, amt);
    }catch(e){ toast('zap failed: '+e.message); }
  }
  function invoiceModal(pr, amt){
    modal(`<h3>⚡ Zap ${amt} sats</h3><p class="muted small">Pay with your Lightning wallet:</p>
      <a class="btn btn-neon full" href="lightning:${enc(pr)}">Open in wallet</a>
      <div class="keybox" style="margin-top:10px"><code id="z-inv">${enc(pr)}</code></div>
      <button class="btn btn-cyan full" id="z-copy">Copy invoice</button>`, root=>{
      $('#z-copy',root).onclick=()=>{ navigator.clipboard.writeText(pr); toast('invoice copied'); };
    });
  }
  function bech32ToBytes(s){ const d=NT().nip19; throw new Error('lnurl decode unsupported'); }
  async function doBlock(pk){
    if(!IS_ADMIN) return;
    if(!confirm('Block this npub on the relay? Their events get rejected and purged.')) return;
    try {
      const auth = await sign(27235, 'block', [['action','block'],['p',pk]]);
      const r = await fetch('/client/block', { method:'POST', headers:{'Content-Type':'application/json'},
        body: JSON.stringify({ target: pk, auth: btoa(JSON.stringify(auth)) }) }).then(r=>r.json());
      toast(r.ok ? 'blocked on relay' : ('block failed: ' + (r.error||'')));
    } catch(e){ toast('block failed'); }
  }
  function eTags(id,pk){ const t=[['e',id]]; if(pk)t.push(['p',pk]); return t; }
  // The emoji picker is the only way to react now (the dedicated 🤍 like button was removed). After
  // publishing, refresh the counters in place (decorateCounts) so the reaction count goes up and the
  // react button shows your chosen emoji — without re-rendering/resetting the whole feed.
  const REACTION_EMOJIS=['❤️','🔥','😂','🤣','😮','😯','😢','😭','👍','👎','🤙','💀','⚡','🚀','🤔','🥰','😍','😘','😎','🤩','🥳','😏','😊','🙂','😉','😌','😋','😛','😜','🤪','😅','😆','😁','😄','😀','🙃','😇','🤗','🤭','🤫','🫡','🧐','🤓','🥸','😐','😑','😶','🙄','😬','🤨','😴','🤤','😪','😷','🤒','🤕','🤢','🤮','🥵','🥶','🥴','😵','🤯','😳','🥺','😤','😠','😡','🤬','😱','😨','😰','😥','😓','🥱','🤠','😈','👿','👹','👺','🤡','💩','👻','👽','👾','🤖','🎃','👀','👏','🙌','🙏','🤝','💪','👊','✌️','🤞','🤟','🤘','👌','🫶','💯','💔','🧡','💛','💚','💙','💜','🖤','🤍','⭐','✨','💥','🎉'];
  // Shared emoji popover anchored under a button. onPick(emoji, close) decides what to do (react,
  // or insert into a textarea — for the latter it can keep the picker open for multiple inserts).
  function openEmojiPopover(anchorBtn, onPick){
    document.querySelectorAll('.emoji-pop').forEach(p=>p.remove());   // never stack pickers
    const pop=document.createElement('div'); pop.className='emoji-pop';
    pop.innerHTML=REACTION_EMOJIS.map(x=>`<button data-e="${x}">${x}</button>`).join('');
    document.body.appendChild(pop);
    const r=(anchorBtn||document.body).getBoundingClientRect();
    let left=Math.max(8, Math.min(r.left, window.innerWidth-8-pop.offsetWidth));
    let top=r.bottom+6; if(top+pop.offsetHeight>window.innerHeight-8) top=Math.max(8, r.top-pop.offsetHeight-6);
    pop.style.left=left+'px'; pop.style.top=top+'px';
    const close=()=>{ pop.remove(); document.removeEventListener('click',onDoc,true); const f=$('#feed'); if(f) f.removeEventListener('scroll',close); };
    const onDoc=e=>{ if(!pop.contains(e.target) && !(anchorBtn && anchorBtn.contains(e.target))) close(); };
    setTimeout(()=>{ document.addEventListener('click',onDoc,true); const f=$('#feed'); if(f) f.addEventListener('scroll',close,{once:true}); },0);
    // mousedown + preventDefault keeps the textarea focused so insert-at-cursor works
    $$('[data-e]',pop).forEach(b=> b.onmousedown=ev=>{ ev.preventDefault(); onPick(b.dataset.e, close); });
    return close;
  }
  function pickEmoji(id,pk,btn){
    if(myReaction(id)){ toast('already reacted'); return; }
    openEmojiPopover(btn, (emoji, close)=>{ close(); publish(7,emoji,eTags(id,pk)).then(()=>{ toast('reacted '+emoji); decorateCounts(); }); });
  }
  // Generic "☰ more" popover anchored under a button. items = [action, label, optional css class];
  // onPick(action) fires after the menu closes. Shared by the post menu and the profile menu.
  function openMenuPopover(anchorBtn, items, onPick){
    document.querySelectorAll('.menu-pop,.emoji-pop').forEach(p=>p.remove());   // never stack popovers
    const pop=document.createElement('div'); pop.className='menu-pop';
    pop.innerHTML=items.map(([a,label,cls])=>`<button data-m="${a}"${cls?` class="${cls}"`:''}>${enc(label)}</button>`).join('');
    document.body.appendChild(pop);
    const r=anchorBtn.getBoundingClientRect();
    const left=Math.max(8, Math.min(r.right-pop.offsetWidth, window.innerWidth-8-pop.offsetWidth));
    let top=r.bottom+6; if(top+pop.offsetHeight>window.innerHeight-8) top=Math.max(8, r.top-pop.offsetHeight-6);
    pop.style.left=left+'px'; pop.style.top=top+'px';
    const close=()=>{ pop.remove(); document.removeEventListener('click',onDoc,true); const f=$('#feed'); if(f) f.removeEventListener('scroll',close); };
    const onDoc=e=>{ if(!pop.contains(e.target) && !anchorBtn.contains(e.target)) close(); };
    setTimeout(()=>{ document.addEventListener('click',onDoc,true); const f=$('#feed'); if(f) f.addEventListener('scroll',close,{once:true}); },0);
    $$('[data-m]',pop).forEach(b=> b.onclick=()=>{ close(); onPick(b.dataset.m); });
    return close;
  }
  // the per-post "☰ more" menu — holds the secondary actions (bookmark / copy id / pin / delete /
  // block) so the action row stays a clean 5 across.
  function openPostMenu(id, pk, art, anchorBtn){
    const mine = pk===ME.pubkey;
    const items=[['bookmark', BOOKMARKS.has(id)?'🔖 Remove bookmark':'🔖 Bookmark'], ['copyid','🆔 Copy event ID']];
    if(mine) items.push(['pin', PINNED.has(id)?'📌 Unpin from profile':'📌 Pin to profile']);
    if(mine) items.push(['delete','🗑️ Delete','danger']);
    if(IS_ADMIN && !mine) items.push(['block','🚫 Block author','danger']);
    openMenuPopover(anchorBtn, items, a=>{
      if(a==='bookmark'){ toggleBookmark(id, null).then(()=>{ if(anchorBtn) anchorBtn.classList.toggle('on', BOOKMARKS.has(id)); }); return; }
      if(a==='copyid'){ try{ navigator.clipboard.writeText(NT().nip19.noteEncode(id)); toast('note id copied'); }catch(_){ navigator.clipboard.writeText(id); toast('event id copied'); } return; }
      if(a==='pin') return togglePin(id);
      if(a==='delete') return doDelete(id, art);
      if(a==='block') return doBlock(pk);
    });
  }
  async function doRepost(id,pk,btn){
    if(countsFor(id).iRt){ toast('already reposted'); return; }
    const o=Store.get(id);
    await publish(6, o?JSON.stringify(o):'', eTags(id,pk));
    btn.classList.add('on'); const n=btn.querySelector('.n'); n.textContent=(parseInt(n.textContent||'0')+1); toast('reposted');
  }
  async function doDelete(id,art){
    if(!confirm('Delete this post? (publishes a NIP-09 deletion request)')) return;
    await publish(5, 'deleted by author', [['e',id]]);
    Store.removeEvent(id); if(art)art.remove(); toast('deletion requested');
  }

  // ---------- compose ----------
  // ---------- drafts (local-only, per-account; never published until you send) ----------
  const Drafts = {
    key(){ return 'pc_drafts_' + ((typeof ME!=='undefined' && ME && ME.pubkey) || 'anon'); },
    all(){ try{ return JSON.parse(localStorage.getItem(this.key())||'[]'); }catch(_){ return []; } },
    _save(a){ try{ localStorage.setItem(this.key(), JSON.stringify(a.slice(0,300))); }catch(_){} bumpDraft(); },
    get(id){ return this.all().find(x=>x.id===id); },
    save(d){ const a=this.all(); d.id=d.id||('d'+Date.now().toString(36)+Math.random().toString(36).slice(2,6)); d.ts=Math.floor(Date.now()/1000);
      const i=a.findIndex(x=>x.id===d.id); if(i>=0)a[i]=d; else a.unshift(d); this._save(a); return d.id; },
    remove(id){ this._save(this.all().filter(x=>x.id!==id)); },
  };
  function bumpDraft(){ const n=Drafts.all().length; $$('#draft-badge,#more-badge-m').forEach(b=>{ if(n){b.textContent=n>99?'99+':n;b.classList.remove('hidden');}else b.classList.add('hidden'); }); }
  // mobile overflow sheet — holds the secondary views so the bottom bar stays uncluttered
  function moreMenu(){
    const items=[['drafts','✐','Drafts'],['bookmarks','🔖','Bookmarks'],['articles','📰','Articles'],['streams','📺','Streams'],['blossom','🌸','Files'],['profile','👤','Profile'],['settings','⚙','Settings']];
    modal(`<h3>More</h3><div class="more-grid">${items.map(([v,ic,lbl])=>`<button class="more-item" data-v="${v}"><span class="more-ic">${ic}</span><span>${enc(lbl)}</span></button>`).join('')}</div>`, root=>{
      $$('.more-item',root).forEach(b=> b.onclick=()=>{ closeModal(); if(b.dataset.v==='profile') renderProfileView(ME.pubkey); else switchView(b.dataset.v); });
    });
  }
  function renderDrafts(){
    const feed=$('#feed'); const list=Drafts.all();
    feed.innerHTML = list.length ? list.map(d=>{
      const ctx = d.reply?'<span class="muted small">↩ reply</span>' : d.quote?'<span class="muted small">❝ quote</span>' : '';
      return `<div class="note draft-card" data-draft="${d.id}"><div class="draft-body">${linkify(d.text||'')}</div>
        <div class="draft-foot"><span class="muted small">${ctx} saved ${timeAgo(d.ts)}</span>
          <span class="spacer"></span>
          <button class="btn btn-ghost small" data-act="edit">✏ Edit</button>
          <button class="btn btn-ghost small" data-act="del" style="color:#ff6b8b">🗑 Delete</button>
          <button class="btn btn-neon small" data-act="send">Send ▶</button></div></div>`;
    }).join('') : '<div class="empty">No drafts. Write a post and tap 💾 Draft to save it for later.</div>';
    feed.querySelectorAll('.draft-card').forEach(card=>{
      const id=card.dataset.draft;
      card.querySelector('[data-act="edit"]').onclick=()=>{ const d=Drafts.get(id); if(d) compose({reply:d.reply,replyPk:d.replyPk,quote:d.quote,draftId:id,text:d.text}); };
      card.querySelector('[data-act="del"]').onclick=()=>{ if(confirm('Delete this draft?')){ Drafts.remove(id); renderDrafts(); } };
      card.querySelector('[data-act="send"]').onclick=()=>sendDraft(id);
    });
    hydrate(feed);
  }
  async function sendDraft(id){
    const d=Drafts.get(id); if(!d || !(d.text||'').trim()) return;
    let tags=[];
    if(d.reply){ const o=Store.get(d.reply); tags=replyTags(o, d.reply, d.replyPk); }
    if(d.quote){ tags.push(['q',d.quote]); const o=Store.get(d.quote); if(o)tags.push(['p',o.pubkey]); }
    mentionTags(d.text).forEach(t=>{ if(!tags.some(x=>x[0]==='p'&&x[1]===t[1])) tags.push(t); });
    try{ await publish(1, d.text, tags); Drafts.remove(id); toast('posted'); if(VIEW==='drafts') renderDrafts(); }
    catch(e){ toast('post failed: '+e.message); }
  }
  function compose({reply=null, replyPk=null, quote=null, draftId=null, text=''}={}){
    const title = reply?'Reply':quote?'Quote post':'New post';
    let qhtml=''; if(quote){ const o=Store.get(quote); if(o) qhtml=`<div class="quoted"><b>${enc((profOf(o.pubkey).name)||'anon')}</b><div class="txt">${linkify(o.content)}</div></div>`; }
    modal(`<h3>${title}</h3>${qhtml}
      <div class="cmp-tabs"><button class="cmp-tab active" data-t="write">Write</button><button class="cmp-tab" data-t="preview">👁 Preview</button></div>
      <textarea id="cmp" placeholder="what's happening on the net?"></textarea>
      <div class="muted small mention-hint hidden" id="cmp-mentions"></div>
      <div id="cmp-preview" class="note-preview hidden"></div>
      <div class="row cmp-tools"><button class="btn btn-ghost small" id="cmp-img">📎 Attach</button><button class="btn btn-ghost small" id="cmp-blossom">🌸 Files</button><button class="btn btn-ghost small" id="cmp-emoji">😀 Emoji</button>${CFG.gif_enabled?`<button class="btn btn-ghost small" id="cmp-gif">🎬 GIF</button>`:''}<input type="file" id="cmp-file" multiple hidden>
      <span class="spacer"></span><button class="btn btn-ghost small" id="cmp-draft">💾 Draft</button><button class="btn btn-neon small" id="cmp-send">Post ▶</button></div>
      <div class="muted small" id="cmp-status"></div>`, root=>{
      const ta=$('#cmp',root); attachMentionAutocomplete(ta); if(text) ta.value=text;
      const _mh=$('#cmp-mentions',root); ta.addEventListener('input', ()=>updateMentionHint(ta,_mh)); updateMentionHint(ta,_mh);
      ta.addEventListener('keydown', e=>{ if((e.ctrlKey||e.metaKey) && e.key==='Enter'){ e.preventDefault(); const sb=$('#cmp-send',root); if(sb) sb.click(); } });   // Ctrl/⌘+Enter to post
      $$('.cmp-tab',root).forEach(b=> b.onclick=()=>{
        $$('.cmp-tab',root).forEach(x=>x.classList.toggle('active',x===b));
        const pv=b.dataset.t==='preview', prev=$('#cmp-preview',root);
        ta.classList.toggle('hidden',pv); prev.classList.toggle('hidden',!pv);
        if(pv) prev.innerHTML = ta.value.trim() ? `<div class="txt">${linkify(ta.value)}</div>` : '<div class="muted small">Nothing to preview.</div>';
      });
      // paste image (or any file) from clipboard -> upload + append URL
      ta.addEventListener('paste', async (e)=>{
        const files=[...(e.clipboardData&&e.clipboardData.items||[])].filter(it=>it.kind==='file').map(it=>it.getAsFile()).filter(Boolean);
        if(!files.length) return; e.preventDefault();
        for(let i=0;i<files.length;i++){ $('#cmp-status',root).textContent=`uploading pasted ${i+1}/${files.length}…`;
          try{ const url=await uploadBlob(files[i]); ta.value+=(ta.value?'\n':'')+url; }
          catch(err){ $('#cmp-status',root).textContent='upload failed: '+err.message; return; } }
        $('#cmp-status',root).textContent='';
      });
      $('#cmp-img',root).onclick=()=>$('#cmp-file',root).click();
      $('#cmp-blossom',root).onclick=()=>blossomPicker(ta);
      $('#cmp-emoji',root).onclick=(e)=>{ e.stopPropagation(); openEmojiPopover($('#cmp-emoji',root), (emoji)=>{ _insertAt(ta, emoji); }); };
      { const gb=$('#cmp-gif',root); if(gb) gb.onclick=()=>gifPicker(ta); }
      $('#cmp-file',root).onchange=async e=>{ const files=[...e.target.files]; if(!files.length)return;
        for(let i=0;i<files.length;i++){ $('#cmp-status',root).textContent=`uploading ${i+1}/${files.length}…`;
          try{ const url=await uploadBlob(files[i]); ta.value+=(ta.value?'\n':'')+url; }
          catch(err){ $('#cmp-status',root).textContent='upload failed: '+err.message; return; } }
        $('#cmp-status',root).textContent=''; e.target.value=''; };
      $('#cmp-draft',root).onclick=()=>{
        const body=ta.value.trim(); if(!body){ toast('nothing to save'); return; }
        Drafts.save({id:draftId, text:body, reply, replyPk, quote}); closeModal(); toast('saved to drafts');
        if(VIEW==='drafts') renderView(true);
      };
      $('#cmp-send',root).onclick=async()=>{
        const text=ta.value.trim(); if(!text)return;
        let tags=[];
        if(reply){ const o=Store.get(reply); tags=replyTags(o, reply, replyPk); }
        if(quote){ tags.push(['q',quote]); const o=Store.get(quote); if(o)tags.push(['p',o.pubkey]); }
        mentionTags(text).forEach(t=>{ if(!tags.some(x=>x[0]==='p'&&x[1]===t[1])) tags.push(t); });
        closeModal(); await publish(1, text, tags); if(draftId) Drafts.remove(draftId);
        toast('posted'); if(VIEW==='home'||VIEW==='global'||VIEW==='drafts') renderView(true);
      };
      ta.focus();
    });
  }
  function replyTags(parent, id, pk){
    const tags=[]; let root=null;
    if(parent){
      const marked=parent.tags.find(t=>t[0]==='e'&&t[3]==='root');
      if(marked) root=marked[1];
      else { const firstE=parent.tags.find(t=>t[0]==='e'); if(firstE) root=firstE[1]; }  // positional NIP-10
    }
    if(root && root!==id){ tags.push(['e',root,'','root']); tags.push(['e',id,'','reply']); }
    else tags.push(['e',id,'','root']);
    // p-tag the parent author + carry forward thread participants so everyone is notified
    const seen=new Set();
    const authors=[pk].concat(parent?parent.tags.filter(t=>t[0]==='p').map(t=>t[1]):[]);
    for(const a of authors){ if(a&&!seen.has(a)){ seen.add(a); tags.push(['p',a]); } }
    return tags;
  }
  function niceNip05(n){ if(!n) return null; n=String(n).trim(); if(!n) return null; return n.startsWith('_@')?('@'+n.slice(2)):n; }

  async function _ensureProfile(pk){ if(!pk || Store.haveProfile(pk)) return; try{ const e=await Relay.query([{ authors:[pk], kinds:[0], limit:1 }]); if(e[0]) Store.saveProfile(e[0]); }catch(_){} }
  // @-autocomplete: type "@name" (local profiles), a full NIP-05 "@name@domain.tld", or an
  // "@npub1…/@nprofile1…" — the latter two are resolved live (NIP-05 lookup / decode + profile
  // fetch) so you can mention people who aren't cached yet. Picking inserts a nostr:npub mention.
  function attachMentionAutocomplete(ta){
    let box=null, seq=0;
    const close=()=>{ if(box){box.remove();box=null;} };
    const local=q=>Store.profileList().filter(p=>(((p.meta.name||'')+(p.meta.display_name||'')+(p.meta.nip05||'')).toLowerCase().includes(q))).slice(0,6);
    function render(list, left, pos){
      close(); if(!list.length) return;
      box=document.createElement('div'); box.className='mention-box';
      box.innerHTML=list.map(p=>`<div class="mention-opt" data-pk="${p.pubkey}"><img src="${enc((p.meta||{}).picture||LOGO)}" onerror="this.src='${LOGO}'"><span><b>${enc((p.meta||{}).name||(p.meta||{}).display_name||'anon')}</b> <span class="muted small">${enc(niceNip05((p.meta||{}).nip05)||(NT().nip19.npubEncode(p.pubkey).slice(0,14)+'…'))}</span></span></div>`).join('');
      ta.insertAdjacentElement('afterend', box);
      box.querySelectorAll('[data-pk]').forEach(el=> el.onmousedown=ev=>{ ev.preventDefault();
        const np=NT().nip19.npubEncode(el.dataset.pk);
        const newLeft=left.replace(/@[^\s@]+(?:@[^\s@]*)?$/,'nostr:'+np+' ');
        ta.value=newLeft+ta.value.slice(pos); ta.focus(); ta.selectionStart=ta.selectionEnd=newLeft.length; close(); });
    }
    ta.addEventListener('input', ()=>{
      const pos=ta.selectionStart, left=ta.value.slice(0,pos), m=left.match(/(?:^|\s)@([^\s@]+(?:@[^\s@]*)?)$/);
      if(!m){ close(); return; }
      const q=m[1].toLowerCase(); const my=++seq;
      let list=local(q); render(list, left, pos);
      // remote resolution for a typed NIP-05 address or npub/nprofile not in the cache
      (async()=>{
        let pk=null;
        if(/^(?:nostr:)?(?:npub1|nprofile1)[0-9a-z]{20,}$/i.test(q)) pk=refToPk(q);
        else if(/^[\w.\-]+@[\w.\-]+\.[a-z]{2,}$/i.test(q)) pk=await nip05Resolve(q);
        if(!pk || my!==seq) return;
        await _ensureProfile(pk); if(my!==seq) return;
        if(!list.some(p=>p.pubkey===pk)){ const meta=Store.profile(pk)||{nip05:q.includes('@')?q:''}; list=[{pubkey:pk, meta}, ...list].slice(0,6); render(list, left, pos); }
      })();
    });
    ta.addEventListener('blur', ()=>setTimeout(close,200));
  }
  // decode an npub/nprofile (with or without the nostr: prefix) to a hex pubkey
  function refToPk(v){
    try{ const d=NT().nip19.decode(String(v).replace(/^nostr:/i,'')); if(d.type==='npub') return d.data; if(d.type==='nprofile') return d.data.pubkey; }catch(_){}
    return safePk(v);
  }
  // p-tags that drive the recipient's notification. Catches: explicit npub/nprofile refs (with or
  // without `nostr:`), AND bare `@name` typed without picking the autocomplete — resolved against
  // cached profiles by an EXACT name / display-name / nip05 local-part match (1 unique hit only, so
  // it never mis-tags the wrong person). Without this, an un-converted @mention sent no p-tag, so
  // the mentioned user never got notified.
  function mentionTags(text){
    const out=[]; const add=pk=>{ if(pk && !out.some(t=>t[1]===pk)) out.push(['p',pk]); };
    for(const m of (text||'').matchAll(/(?:nostr:)?((?:npub1|nprofile1)[0-9a-z]{20,})/gi)) add(refToPk(m[1]));
    for(const m of (text||'').matchAll(/(?:^|\s)@([a-z0-9_.\-]{2,40})/gi)){
      const q=m[1].toLowerCase();
      const hits=Store.profileList().filter(p=>{
        const nm=(p.meta.name||'').toLowerCase(), dn=(p.meta.display_name||'').toLowerCase();
        const n5=(niceNip05(p.meta.nip05)||'').replace(/^@/,'').split('@')[0].toLowerCase();
        return nm===q || dn===q || n5===q;
      });
      if(hits.length===1) add(hits[0].pubkey);
    }
    return out;
  }
  // live hint under the composer: resolve any npub/nprofile in the text to a readable @name so a
  // pasted "nostr:npub1…" gives immediate feedback about who you're mentioning.
  function updateMentionHint(ta, hintEl){
    if(!hintEl) return;
    const uniq=[...new Set([...(ta.value||'').matchAll(/(?:nostr:)?((?:npub1|nprofile1)[0-9a-z]{20,})/gi)].map(m=>refToPk(m[1])).filter(Boolean))];
    if(!uniq.length){ hintEl.textContent=''; hintEl.classList.add('hidden'); return; }
    let missing=false;
    const names=uniq.map(pk=>{ const p=Store.profile(pk); if(!p){ needProfile(pk); missing=true; return '@'+NT().nip19.npubEncode(pk).slice(0,12)+'…'; } return '@'+(p.name||p.display_name||'anon'); });
    hintEl.textContent='↳ mentioning '+names.join(', '); hintEl.classList.remove('hidden');
    if(missing && !hintEl._t){ hintEl._t=setTimeout(()=>{ hintEl._t=null; updateMentionHint(ta,hintEl); }, 800); }
  }

  // ---------- Blossom uploads + file browser ----------
  // The user's custom Blossom server only applies once they've enabled the override in Settings;
  // otherwise everything uses the built-in server from /client/config.
  function mediaServer(){
    let s = ClientSettings.get('blossomEnabled') ? (ClientSettings.get('mediaServer')||'').trim() : '';
    // accept a bare host ("blossom.example.com") — without a scheme fetch() would treat it as a
    // RELATIVE path and POST to poster.place instead of the user's server.
    if (s && !/^https?:\/\//i.test(s)) s = 'https://' + s;
    return (s || CFG.blossom_url || '').replace(/\/+$/,'');
  }
  async function sha256hex(buf){ const h=await crypto.subtle.digest('SHA-256', buf); return [...new Uint8Array(h)].map(b=>b.toString(16).padStart(2,'0')).join(''); }
  const _MIME_EXT={'image/jpeg':'jpg','image/png':'png','image/gif':'gif','image/webp':'webp','image/avif':'avif',
    'video/mp4':'mp4','video/webm':'webm','video/quicktime':'mov','audio/mpeg':'mp3','audio/ogg':'ogg','audio/wav':'wav','audio/mp4':'m4a','audio/aac':'aac','audio/flac':'flac'};
  function extFor(file){ const n=(file.name||'').match(/\.([a-z0-9]{2,5})$/i); if(n) return n[1].toLowerCase(); return _MIME_EXT[file.type]||''; }
  async function uploadBlob(file){
    const server=mediaServer(); if(!server) throw new Error('no media server set');
    const buf=await file.arrayBuffer(); const hash=await sha256hex(buf);
    const auth=await sign(24242,'Upload blob',[['t','upload'],['x',hash],['expiration',String(Math.floor(Date.now()/1000)+3600)]]);
    let res;
    try {
      res=await fetch(server+'/upload',{ method:'PUT', headers:{ 'Authorization':'Nostr '+btoa(JSON.stringify(auth)), 'Content-Type':file.type||'application/octet-stream' }, body:buf });
    } catch(e){
      // fetch rejects (vs. an HTTP error) only when the browser can't complete the request at all:
      // server unreachable, blocked mixed content (http:// on this https page), or — most often for
      // a custom server — it doesn't send CORS headers allowing this site to upload to it.
      throw new Error(`couldn't reach ${server} — check the URL, and that the server allows cross-origin (CORS) uploads`);
    }
    if(!res.ok){ const t=await res.text().catch(()=>res.status); throw new Error(res.headers.get('x-reason')||t); }
    const d=await res.json();
    // Our Blossom URLs are extensionless (/<sha256>); append the file extension so clients (incl.
    // linkify below) can detect the media type and embed/play it. The server ignores the suffix.
    const ext=extFor(file); return (d.url||server+'/'+hash) + (ext?('.'+ext):'');
  }
  function blobThumb(b){
    const t=b.type||'', ext=(t.split('/')[1]||'file').slice(0,10);
    if(/image/.test(t)) return `<img src="${enc(b.url)}" loading="lazy">`;
    if(/video/.test(t)) return `<video src="${enc(b.url)}" muted></video>`;
    if(/audio/.test(t)) return `<div class="file-icon">🎵<span>${enc(ext)}</span></div>`;
    const icon = /zip|compress|tar|gzip|7z|rar/.test(t)?'📦' : /pdf/.test(t)?'📕' : /text|json|xml|csv/.test(t)?'📄' : '📎';
    return `<div class="file-icon">${icon}<span>${enc(ext)}</span></div>`;
  }
  function gifPicker(ta){
    const bg=document.createElement('div'); bg.className='modal-bg'; bg.style.zIndex='200';
    bg.innerHTML=`<div class="modal glass neon-border"><h3>🎬 GIFs</h3><input class="input" id="gif-q" placeholder="search GIFs…" autocomplete="off"><div id="gif-grid" class="gif-grid"><div class="spinner"></div></div></div>`;
    bg.onclick=e=>{ if(e.target===bg) bg.remove(); };
    $('#modal-root').appendChild(bg);
    const grid=bg.querySelector('#gif-grid'), q=bg.querySelector('#gif-q'); let t=null;
    async function load(query){
      grid.innerHTML='<div class="spinner"></div>';
      let j={}; try{ j=await fetch('/client/gif?q='+encodeURIComponent(query||'')).then(r=>r.json()); }catch(_){}
      const rs=j.results||[];
      if(!rs.length){ grid.innerHTML='<div class="empty">'+(j.error?'GIF search not configured (set a Tenor key in Admin).':'No GIFs.')+'</div>'; return; }
      grid.innerHTML=rs.map(g=>`<img class="gif-item" src="${enc(g.preview)}" data-url="${enc(g.url)}" loading="lazy">`).join('');
      grid.querySelectorAll('.gif-item').forEach(im=> im.onclick=()=>{ ta.value+=(ta.value?'\n':'')+im.dataset.url+' '; bg.remove(); toast('GIF added'); });
    }
    q.oninput=()=>{ clearTimeout(t); t=setTimeout(()=>load(q.value.trim()),350); };
    load(''); q.focus();
  }
  function blossomPicker(ta){
    const server=mediaServer(); if(!server){ toast('no media server set'); return; }
    const bg=document.createElement('div'); bg.className='modal-bg'; bg.style.zIndex='200';
    bg.innerHTML=`<div class="modal glass neon-border"><h3>🌸 Attach from your Blossom files</h3><div id="bp-grid" class="files-grid"><div class="spinner"></div></div></div>`;
    bg.onclick=e=>{ if(e.target===bg) bg.remove(); };
    $('#modal-root').appendChild(bg);
    (async()=>{
      let list=[]; try{ const r=await fetch(server+'/list/'+ME.pubkey); if(r.ok) list=await r.json(); }catch(_){}
      const grid=bg.querySelector('#bp-grid');
      grid.innerHTML = list.length ? list.map(b=>{
        return `<div class="file-card" data-url="${enc(b.url)}" data-type="${enc(b.type||'')}">${blobThumb(b)}</div>`;
      }).join('') : '<div class="empty">No files yet — upload some in the Files tab.</div>';
      bg.querySelectorAll('[data-url]').forEach(el=> el.onclick=()=>{ const ext=_MIME_EXT[el.dataset.type]||''; ta.value+=(ta.value?'\n':'')+el.dataset.url+(ext?('.'+ext):''); bg.remove(); toast('attached'); });
    })();
  }
  async function renderBlossom(){
    const feed=$('#feed'); const server=mediaServer();
    feed.innerHTML=`<div class="uploader"><input type="file" id="bl-file" multiple> <button class="btn btn-cyan small" id="bl-up">Upload</button> <span class="muted small">→ ${enc(server||'(no server)')}</span></div><div class="files-grid" id="bl-grid"><div class="spinner"></div></div>`;
    $('#bl-up').onclick=async()=>{ const files=[...$('#bl-file').files]; if(!files.length)return;
      for(let i=0;i<files.length;i++){ try{ await uploadBlob(files[i]); toast(`uploaded ${i+1}/${files.length}`); }catch(e){ toast('upload failed: '+e.message);} }
      renderBlossom(); };
    if(!server){ $('#bl-grid').innerHTML='<div class="empty">Blossom server not configured.</div>'; return; }
    let list=[];
    try{ const r=await fetch(server+'/list/'+ME.pubkey); if(!r.ok) throw new Error('HTTP '+r.status); list=await r.json(); }
    catch(e){ $('#bl-grid').innerHTML='<div class="empty">Couldn\'t load files from '+enc(server)+' ('+enc(e.message)+').</div>'; return; }
    const grid=$('#bl-grid');
    grid.innerHTML = list.length ? list.map(b=>{
      return `<div class="file-card" data-sha="${b.sha256}"><a href="${enc(b.url)}" target="_blank" download>${blobThumb(b)}</a><button class="del" data-sha="${b.sha256}">✕</button><div class="meta"><span>${((b.size||0)/1024|0)}KB</span><span>${(b.type||'').split('/')[1]||''}</span></div></div>`;
    }).join('') : '<div class="empty">No files yet — upload one above.</div>';
    $$('.del',grid).forEach(b=> b.onclick=()=>delBlob(b.dataset.sha));
  }
  async function delBlob(sha){
    if(!confirm('Delete this blob?'))return; const server=mediaServer();
    const auth=await sign(24242,'Delete blob',[['t','delete'],['x',sha],['expiration',String(Math.floor(Date.now()/1000)+3600)]]);
    const res=await fetch(server+'/'+sha,{ method:'DELETE', headers:{'Authorization':'Nostr '+btoa(JSON.stringify(auth))} });
    if(res.ok){ toast('deleted'); renderBlossom(); } else toast('delete failed');
  }

  // ---------- notifications ----------
  let _notifReady=false;
  function watchNotifications(){
    seenNotif.last = +(localStorage.getItem('pc_notif_seen')||0);
    Relay.subscribe([{ '#p':[ME.pubkey], kinds:[1,6,7,9735], limit:60 }], {
      onEvent: ev => { if(ev.pubkey===ME.pubkey) return; if(Store.saveEvent(ev)){ invalidateCounts(); needProfile(ev.kind===9735?(zapSender(ev)||ev.pubkey):ev.pubkey);
        if(ev.created_at>seenNotif.last){ bumpNotif(); if(_notifReady) notifPing(ev); }
        if(VIEW==='notifications') renderNotifications(); } },
      onEose: ()=>{ _notifReady=true; if(VIEW==='notifications') renderNotifications(); else bumpNotif(); }   // show unseen count on load; ping LIVE ones
    });
  }
  function notifPing(ev){
    const fromPk = ev.kind===9735?(zapSender(ev)||ev.pubkey):ev.pubkey;
    const p=profOf(fromPk); const who=p.name||p.display_name||'someone';
    const what = ev.kind===9735?`⚡ zapped you ${fmtSats(zapAmount(ev))} sats`
      : ev.kind===7?`reacted ${ev.content==='+'||ev.content===''?'❤️':enc(ev.content)}`
      : ev.kind===6?'reposted you' : isReply(ev)?'replied to you' : 'mentioned you';
    notifToast(`🔔 ${who} ${what}`, p.picture);
    try{ if(window.Notification && Notification.permission==='granted') new Notification('PosterChan', { body:`${who} ${what}`, icon:p.picture||LOGO }); }catch(_){}
  }
  function notifToast(msg, pic){
    const t=document.createElement('div'); t.className='toast notif-toast';
    t.innerHTML=`<img src="${enc(pic||LOGO)}" onerror="this.src='${LOGO}'"><span>${enc(msg)}</span>`;
    t.onclick=()=>{ switchView('notifications'); t.remove(); };
    $('#toast-root').appendChild(t); setTimeout(()=>t.remove(),5000);
  }
  function notifList(){ return Store.all().filter(e=>[1,6,7,9735].includes(e.kind) && e.pubkey!==ME.pubkey && !MUTED.has(e.pubkey) && e.tags.some(t=>t[0]==='p'&&t[1]===ME.pubkey)).sort((a,b)=>b.created_at-a.created_at).slice(0,100); }
  function bumpNotif(){ const n=notifList().filter(e=>e.created_at>seenNotif.last).length; $$('#notif-badge,#notif-badge-m').forEach(b=>{ if(n){b.textContent=n>99?'99+':n;b.classList.remove('hidden');}else b.classList.add('hidden');}); }
  function renderNotifications(){
    const list=notifList(); const feed=$('#feed');
    feed.innerHTML = list.length ? list.map(notifHtml).join('') : '<div class="empty">No notifications.</div>';
    list.forEach(e=>needProfile(e.kind===9735?(zapSender(e)||e.pubkey):e.pubkey));
    seenNotif.last = Math.floor(Date.now()/1000); localStorage.setItem('pc_notif_seen', seenNotif.last);
    $$('#notif-badge,#notif-badge-m').forEach(b=>b.classList.add('hidden'));
    feed.querySelectorAll('[data-open]').forEach(n=> n.onclick=()=>openThread(n.dataset.open));
  }
  function notifHtml(e){
    const fromPk = e.kind===9735?(zapSender(e)||e.pubkey):e.pubkey;
    const p=profOf(fromPk); const av=p.picture||LOGO; const tgt=(e.tags.filter(t=>t[0]==='e').pop()||[])[1]||'';
    let cls,ic,txt;
    if(e.kind===9735){cls='zap';ic='⚡';txt=`zapped you <b>${fmtSats(zapAmount(e))} sats</b>`;}
    else if(e.kind===7){cls='like';ic='♥';txt=`reacted ${enc(e.content==='+'?'❤️':e.content)} to your post`;}
    else if(e.kind===6){cls='rt';ic='↻';txt='reposted your note';}
    else if(isReply(e)){cls='reply';ic='💬';txt='replied: '+enc((e.content||'').slice(0,80));}
    else {cls='mention';ic='@';txt='mentioned you: '+enc((e.content||'').slice(0,80));}
    return `<div class="notif ${cls}" data-open="${tgt}"><span class="ic">${ic}</span><img src="${enc(av)}" onerror="this.src='${LOGO}'"><div><b>${enc(p.name||p.display_name||'anon')}</b> ${txt}<div class="muted small">${timeAgo(e.created_at)}</div></div></div>`;
  }

  // ---------- DMs: NIP-17 gift-wrapped (modern, local-key) + NIP-04 (legacy, read-compat) ----------
  const dmPeers = new Map();  // peer -> [{ev, text}]
  let dmActive = null;
  let _dmLoaded=false, _dmUnread=0;
  async function ensureDMs(){
    if(_dmLoaded) return; _dmLoaded=true;
    const modern = !!(signer && signer.nip17unwrap);   // gift wraps need the local secret key
    Store.byKind(4).forEach(ingestDM);                 // show cached legacy DMs instantly
    if(modern) Store.byKind(1059).forEach(w=>ingestWrap(w, false));   // unwrap cached gift wraps (async)
    if(VIEW==='messages') renderMessages();
    const filt=[{ kinds:[4], '#p':[ME.pubkey], limit:300 }, { kinds:[4], authors:[ME.pubkey], limit:300 }];
    if(modern) filt.push({ kinds:[1059], '#p':[ME.pubkey], limit:400 });
    const evs=await Relay.query(filt);
    for(const e of evs){ Store.saveEvent(e); if(e.kind===1059) await ingestWrap(e, false); else ingestDM(e); }
    // live sub for legacy DMs (since now is fine — kind-4 timestamps are real)
    const since=Math.floor(Date.now()/1000)-60;
    Relay.subscribe([{ kinds:[4], '#p':[ME.pubkey], since }, { kinds:[4], authors:[ME.pubkey], since }], {
      onEvent: ev => { Store.saveEvent(ev); if(ingestDM(ev) && ev.pubkey!==ME.pubkey){ _dmUnread++; bumpDm(); } if(VIEW==='messages') renderMessages(); }
    });
    // NIP-17 gift wraps carry RANDOMIZED past timestamps, so a `since` filter would drop them —
    // subscribe with no `since` and let Store dedup skip the ones we've already unwrapped.
    if(modern) Relay.subscribe([{ kinds:[1059], '#p':[ME.pubkey] }], {
      onEvent: async ev => { if(!Store.saveEvent(ev)) return; await ingestWrap(ev, true); }
    });
    if(VIEW==='messages') renderMessages();
  }
  // Unwrap a NIP-17 gift wrap (kind 1059) → its inner kind-14 chat rumor (already plaintext). `live`
  // bumps the unread badge for incoming (non-self) messages.
  async function ingestWrap(ev, live){
    if(!signer || !signer.nip17unwrap) return false;
    let rumor; try{ rumor=await signer.nip17unwrap(ev); }catch(_){ return false; }
    if(!rumor || rumor.kind!==14 || rumor.content==null) return false;
    const mine = rumor.pubkey===ME.pubkey;
    const peer = mine ? (rumor.tags.find(t=>t[0]==='p')||[])[1] : rumor.pubkey;
    if(!peer) return false; needProfile(peer);
    if(!dmPeers.has(peer)) dmPeers.set(peer, []);
    const arr=dmPeers.get(peer); if(arr.find(m=>m.id===ev.id)) return false;
    arr.push({ id:ev.id, mine, text:rumor.content, t:rumor.created_at, nip17:true }); arr.sort((a,b)=>a.t-b.t);
    if(live && !mine){ _dmUnread++; bumpDm(); }
    if(VIEW==='messages'){ if(dmActive===peer) renderDmThread(peer); else renderMessages(); }
    return true;
  }
  // Send a DM: NIP-17 gift wraps for local-key users, legacy NIP-04 for NIP-07 (no exposed secret).
  async function sendDm(pk, text){
    if(signer && signer.nip17wrap){
      const { toPeer, toSelf } = await signer.nip17wrap(pk, text);
      Store.saveEvent(toSelf); await ingestWrap(toSelf, false);   // show our own message right away
      const r1=await Relay.publish(toPeer); await Relay.publish(toSelf);
      if(r1 && r1.ok===false) toast('relay: '+(r1.msg||'message rejected'));
      if(VIEW==='messages') renderMessages();
    } else {
      const ct=await signer.nip04enc(pk, text); await publish(4, ct, [['p',pk]]);
    }
  }
  function bumpDm(){ $$('#dm-badge,#dm-badge-m').forEach(b=>{ if(_dmUnread){ b.textContent=_dmUnread>99?'99+':_dmUnread; b.classList.remove('hidden'); } else b.classList.add('hidden'); }); }
  // Index DMs WITHOUT decrypting (decryption is CPU-heavy ECDH+AES in the worker; decrypting all
  // 200 on load jams the worker and stalls timeline verification). Decrypt lazily on view.
  function ingestDM(ev){
    const mine = ev.pubkey===ME.pubkey;
    const peer = mine ? (ev.tags.find(t=>t[0]==='p')||[])[1] : ev.pubkey;
    if(!peer) return false; needProfile(peer);
    if(!dmPeers.has(peer)) dmPeers.set(peer, []);
    const arr=dmPeers.get(peer); if(arr.find(m=>m.id===ev.id)) return false;
    arr.push({ id:ev.id, mine, ev, text:null, t:ev.created_at }); arr.sort((a,b)=>a.t-b.t);
    if(VIEW==='messages' && dmActive===peer) renderDmThread(peer);
    return true;
  }
  async function decryptMsg(peer, m){
    if(m.text!=null) return m.text;
    try{ m.text=await signer.nip04dec(peer, m.ev.content); }catch(_){ m.text='🔒 (couldn\'t decrypt)'; }
    return m.text;
  }
  function renderMessages(){
    _dmUnread=0; bumpDm();
    if(!_dmLoaded){ ensureDMs(); }   // lazy-load on first open
    const feed=$('#feed');
    feed.innerHTML=`<div class="dm-wrap"><div class="dm-list" id="dm-list"></div><div class="dm-thread" id="dm-thread"><div class="empty">${_dmLoaded?'Select a conversation, or start one.':'Loading…'}</div></div></div>`;
    const list=$('#dm-list');
    const peers=[...dmPeers.keys()].sort((a,b)=>{ const la=dmPeers.get(a).slice(-1)[0]||{}, lb=dmPeers.get(b).slice(-1)[0]||{}; return (lb.t||0)-(la.t||0); });
    list.innerHTML = `<div class="dm-peer" id="dm-new"><span class="ic">＋</span><b>New message</b></div>` + peers.map(pk=>{
      const p=profOf(pk); const last=dmPeers.get(pk).slice(-1)[0]||{};
      const prev = last.text!=null ? enc(last.text.slice(0,28)) : '🔒 …';
      return `<div class="dm-peer" data-peer="${pk}"><img src="${enc(p.picture||LOGO)}" onerror="this.src='${LOGO}'"><div><b>${enc(p.name||NT().nip19.npubEncode(pk).slice(0,12))}</b><div class="muted small">${prev}</div></div></div>`;
    }).join('');
    $('#dm-new').onclick=newDmModal;
    $$('[data-peer]',list).forEach(el=> el.onclick=()=>openDm(el.dataset.peer));
    // lazily decrypt ONLY the last message of each peer for the preview (not every message)
    const need=peers.filter(pk=>{ const l=dmPeers.get(pk).slice(-1)[0]; return l && l.text==null; });
    if(need.length) Promise.all(need.map(pk=>decryptMsg(pk, dmPeers.get(pk).slice(-1)[0]))).then(()=>{ if(VIEW==='messages' && !dmActive) renderMessages(); });
    if(dmActive && dmPeers.has(dmActive)) renderDmThread(dmActive);
  }
  function safePk(v){ try{ if(v.startsWith('npub')){const d=NT().nip19.decode(v); return d.data;} if(/^[0-9a-f]{64}$/i.test(v))return v.toLowerCase(); }catch(_){} return null; }
  function newDmModal(){
    modal(`<h3>✉ New message</h3>
      <input class="input" id="dm-to" placeholder="@name, npub1…, or name@domain" autocomplete="off">
      <div id="dm-ac" class="mention-box hidden"></div>
      <textarea id="dm-body" placeholder="encrypted message…"></textarea>
      <div class="row cmp-tools"><button class="btn btn-ghost small" id="dm-attach">📎 Attach</button><button class="btn btn-ghost small" id="dm-files">🌸 Files</button>${CFG.gif_enabled?`<button class="btn btn-ghost small" id="dm-gif">🎬 GIF</button>`:''}<input type="file" id="dm-file" multiple hidden><span class="spacer"></span><button class="btn btn-neon" id="dm-go">Send ▶</button></div>
      <div class="muted small" id="dm-status"></div>`, root=>{
      let toPk=null; const to=$('#dm-to',root), ac=$('#dm-ac',root), body=$('#dm-body',root);
      to.addEventListener('input', ()=>{ const v=to.value.trim(); toPk=null;
        const pk=safePk(v); if(pk){ toPk=pk; ac.classList.add('hidden'); return; }
        const q=v.replace(/^@/,'').toLowerCase(); if(q.length<2){ ac.classList.add('hidden'); return; }
        const matches=Store.profileList().filter(p=>(((p.meta.name||'')+(p.meta.display_name||'')+(p.meta.nip05||'')).toLowerCase().includes(q))).slice(0,6);
        if(!matches.length){ ac.classList.add('hidden'); return; }
        ac.classList.remove('hidden'); ac.innerHTML=matches.map(p=>`<div class="mention-opt" data-pk="${p.pubkey}"><img src="${enc(p.meta.picture||LOGO)}" onerror="this.src='${LOGO}'"><b>${enc(p.meta.name||p.meta.display_name||'anon')}</b></div>`).join('');
        $$('[data-pk]',ac).forEach(el=> el.onmousedown=ev=>{ ev.preventDefault(); toPk=el.dataset.pk; to.value='@'+((Store.profile(toPk)||{}).name||NT().nip19.npubEncode(toPk).slice(0,12)); ac.classList.add('hidden'); });
      });
      $('#dm-attach',root).onclick=()=>$('#dm-file',root).click();
      $('#dm-file',root).onchange=async e=>{ const files=[...e.target.files]; for(let i=0;i<files.length;i++){ $('#dm-status',root).textContent=`uploading ${i+1}/${files.length}…`; try{ const url=await uploadBlob(files[i]); body.value+=(body.value?'\n':'')+url; }catch(err){ $('#dm-status',root).textContent='upload failed: '+err.message; return; } } $('#dm-status',root).textContent=''; };
      $('#dm-files',root).onclick=()=>blossomPicker(body);
      { const g=$('#dm-gif',root); if(g) g.onclick=()=>gifPicker(body); }
      $('#dm-go',root).onclick=async()=>{
        let pk=toPk||safePk(to.value.trim().replace(/^@/,''));
        const v=to.value.trim();
        if(!pk && /^[\w.\-+]+@[\w.\-]+\.[a-z]{2,}$/i.test(v)){ $('#dm-status',root).textContent='resolving…'; pk=await nip05Resolve(v.toLowerCase()); }
        if(!pk){ $('#dm-status',root).textContent='pick a valid recipient (npub / NIP-05)'; return; }
        const txt=body.value.trim(); if(!txt){ $('#dm-status',root).textContent='write a message'; return; }
        closeModal();
        try{ await sendDm(pk, txt); if(!dmPeers.has(pk))dmPeers.set(pk,[]); needProfile(pk); switchView('messages'); setTimeout(()=>openDm(pk),80); }
        catch(e){ toast('dm failed: '+e.message); }
      };
      to.focus();
    });
  }
  function openDm(pk){ dmActive=pk; $$('.dm-peer').forEach(e=>e.classList.toggle('active',e.dataset.peer===pk)); $('#dm-list').classList.add('has-active'); renderDmThread(pk); }
  async function renderDmThread(pk){
    const wrap=$('#dm-thread'); if(!wrap)return; const p=profOf(pk); const msgs=dmPeers.get(pk)||[];
    // decrypt this conversation's messages on open (bounded to one peer's thread)
    for(const m of msgs){ if(m.text==null) await decryptMsg(pk,m); }
    if(dmActive!==pk) return;   // user switched away while decrypting
    wrap.innerHTML=`<div class="topbar"><button class="mini" id="dm-back">←</button> <b>${enc(p.name||NT().nip19.npubEncode(pk).slice(0,14))}</b></div>
      <div class="dm-msgs" id="dm-msgs">${msgs.map(m=>`<div class="bubble ${m.mine?'me':'them'}">${linkify(m.text||'')}</div>`).join('')}</div>
      <div class="dm-compose">
        <textarea class="input" id="dm-in" rows="2" placeholder="encrypted message…"></textarea>
        <div class="dm-tools">
          <button class="mini" id="dm-attach" title="attach">📎</button>
          <button class="mini" id="dm-files" title="your Blossom files">🌸</button>
          ${CFG.gif_enabled?`<button class="mini" id="dm-gif" title="GIF">🎬</button>`:''}
          <input type="file" id="dm-file" multiple hidden>
          <span class="spacer"></span>
          <button class="btn btn-neon" id="dm-send">Send ▶</button>
        </div></div>`;
    $('#dm-back').onclick=()=>{ $('#dm-list').classList.remove('has-active'); dmActive=null; };
    const inp=$('#dm-in');
    $('#dm-attach').onclick=()=>$('#dm-file').click();
    $('#dm-file').onchange=async e=>{ const files=[...e.target.files]; for(let i=0;i<files.length;i++){ try{ const url=await uploadBlob(files[i]); inp.value+=(inp.value?' ':'')+url; }catch(err){ toast('upload failed: '+err.message); } } e.target.value=''; inp.focus(); };
    $('#dm-files').onclick=()=>blossomPicker(inp);
    { const g=$('#dm-gif'); if(g) g.onclick=()=>gifPicker(inp); }
    const send=async()=>{ const t=inp.value.trim(); if(!t)return; inp.value='';
      try{ await sendDm(pk, t); }catch(e){ toast('dm failed: '+e.message);} };
    $('#dm-send').onclick=send; $('#dm-in').onkeydown=e=>{ if(e.key==='Enter' && !e.shiftKey){ e.preventDefault(); send(); } };
    const m=$('#dm-msgs'); if(m)m.scrollTop=m.scrollHeight;
  }

  // ---------- profile ----------
  let _prof = { pk:null, tab:'notes', oldest:0, loading:false, done:false, limit:40, fill:null };
  // scroll-back for the active profile tab — pull older author notes, then re-fill the tab list
  // (notes/replies/media all derive from the author's kind-1 stream, so one fetch grows all three).
  async function loadOlderProfile(){
    if(_prof.loading || _prof.done || !_prof.pk || !_prof.oldest) return;
    _prof.loading=true; const pk=_prof.pk; const feed=$('#feed'); loadSentinel(feed);
    const until=_prof.oldest;
    let evs=[]; try{ evs=await Relay.query([{ authors:[pk], kinds:[1], until:until-1, limit:60 }]); }catch(_){}
    clearSentinel(feed);
    if(VIEW!=='profile' || _prof.pk!==pk){ _prof.loading=false; return; }
    let minTs=until;
    for(const e of evs){ Store.saveEvent(e); needProfile(e.pubkey); if(e.created_at<minTs) minTs=e.created_at; }
    invalidateCounts();
    _prof.limit += 60;
    if(minTs<_prof.oldest) _prof.oldest=minTs;
    if(!evs.length || minTs>=until) _prof.done=true;
    if(_prof.fill){ _prof.fill(_prof.tab); hydrate(feed); }
    _prof.loading=false;
  }
  function renderProfile(pk){ renderProfileView(pk); }
  async function renderProfileView(pk){
    cleanupInlineStream();   // e.g. tapping the host's name from a stream
    _hidePill();
    if(VIEW!=='profile'){ VIEW='profile'; $$('.nav-item[data-view]').forEach(b=>b.classList.toggle('active',b.dataset.view==='profile')); $('#view-title').textContent='Profile'; }
    const feed=$('#feed'); feed.innerHTML='<div class="spinner"></div>';
    if(!Store.haveProfile(pk)){ const e=await Relay.query([{authors:[pk],kinds:[0],limit:1}]); for(const x of e)Store.saveProfile(x); }
    const p=Store.profile(pk)||{}; const mine=pk===ME.pubkey;
    const notes=await Relay.query([{authors:[pk],kinds:[1],limit:80}]); notes.forEach(n=>Store.saveEvent(n));
    // following (their latest kind-3) + followers (kind-3s that p-tag them)
    const k3=await Relay.query([{authors:[pk],kinds:[3],limit:1}]);
    const following=k3.length ? (k3.sort((a,b)=>b.created_at-a.created_at)[0].tags.filter(t=>t[0]==='p'&&t[1]).map(t=>t[1])) : [];
    const followerEvs=await Relay.query([{kinds:[3],'#p':[pk],limit:1000}]);
    const followers=[...new Set(followerEvs.map(e=>e.pubkey))];
    // pinned notes (NIP-51 kind-10001)
    const pinList=await Relay.query([{authors:[pk],kinds:[10001],limit:1}]);
    const pinIds=pinList.length ? pinList.sort((a,b)=>b.created_at-a.created_at)[0].tags.filter(t=>t[0]==='e'&&t[1]).map(t=>t[1]) : [];
    let pinned=[];
    if(pinIds.length){ const got=await Relay.query([{ids:pinIds}]); got.forEach(e=>Store.saveEvent(e)); pinned=pinIds.map(id=>Store.get(id)).filter(Boolean); }
    const npub=NT().nip19.npubEncode(pk);
    feed.innerHTML=`<div class="prof"><div class="banner">${p.banner?`<img src="${enc(p.banner)}" onerror="this.remove()">`:''}</div>
      <div class="phead"><img class="pav" src="${enc(p.picture||LOGO)}" onerror="this.src='${LOGO}'">
        <div style="flex:1"></div>${mine?`<button class="btn btn-cyan small" id="edit-prof">Edit</button> <button class="btn btn-ghost small" id="open-settings">⚙ Settings</button>`:`
          <button class="btn btn-ghost small" id="zap-prof">⚡ Zap</button>
          <button class="btn btn-ghost small prof-menu-btn" id="prof-menu" title="more">☰</button>`}</div>
      <div class="pbody"><h2>${enc(p.name||p.display_name||'anon')}<span class="vchk" id="prof-vchk"></span></h2>
        ${niceNip05(p.nip05)?`<div class="muted small">${enc(niceNip05(p.nip05))}</div>`:''}
        <div class="npubrow"><code>${enc(npub.slice(0,24))}…</code><button class="mini" id="copy-npub">📋 copy npub</button></div>
        ${p.lud16?`<button class="ln-addr" id="prof-ln" title="send a zap">⚡ ${enc(p.lud16)}</button>`:''}
        <div class="about">${linkify(p.about||'')}</div>
        <div class="follow-stats"><button class="statbtn" id="show-following"><b>${following.length}</b> Following</button><button class="statbtn" id="show-followers"><b>${followers.length}${followerEvs.length>=1000?'+':''}</b> Followers</button></div>
      </div></div>
      <div class="prof-tabs"><button class="prof-tab active" data-tab="notes">Notes</button><button class="prof-tab" data-tab="replies">Replies</button><button class="prof-tab" data-tab="media">Media</button></div>
      <div id="prof-list"></div>`;
    const pinnedHtml = pinned.length?`<div class="search-section-title">📌 Pinned</div>`+pinned.map(e=>noteHtml(e)).join(''):'';
    const listFor=(tab)=>{
      const lim=_prof.limit;
      if(tab==='replies'){ const r=Store.feed(e=>e.pubkey===pk && isReply(e)).slice(0,lim);
        return r.length ? r.map(e=>noteHtml(e)).join('') : '<div class="empty">No replies yet.</div>'; }
      if(tab==='media'){ const m=Store.feed(e=>e.pubkey===pk && hasMedia(e)).slice(0,lim);
        if(!m.length) return '<div class="empty">No media yet.</div>';
        // gallery only — pull each post's media tags out of its mediaParts() gallery and grid them
        const items=m.map(e=>mediaParts(e.content).gallery.replace(/^<div class="media-row">/,'').replace(/<\/div>$/,'')).join('');
        return `<div class="media-grid">${items}</div>`; }
      const n=Store.feed(e=>e.pubkey===pk && !isReply(e)).slice(0,lim);
      return pinnedHtml + (n.length ? n.map(e=>noteHtml(e)).join('') : '<div class="empty">No posts yet.</div>');
    };
    const fillList=(tab)=>{ const el=$('#prof-list'); if(el) el.innerHTML=listFor(tab); };
    // pagination cursor: oldest author kind-1 we hold (drives loadOlderProfile via `until`)
    const authorNotes=Store.feed(e=>e.pubkey===pk);
    _prof = { pk, tab:'notes', loading:false, done:false, limit:40, fill:fillList,
              oldest: authorNotes.length ? authorNotes[authorNotes.length-1].created_at : 0 };
    fillList('notes');
    hydrate(feed);
    decorateVerified($('#prof-vchk'), pk, p.nip05);
    $$('.prof-tab',feed).forEach(t=> t.onclick=()=>{ $$('.prof-tab',feed).forEach(x=>x.classList.toggle('active',x===t)); _prof.tab=t.dataset.tab; fillList(t.dataset.tab); hydrate(feed); });
    $('#copy-npub').onclick=()=>{ navigator.clipboard.writeText(npub); toast('npub copied'); };
    { const ln=$('#prof-ln'); if(ln) ln.onclick=()=>doZap(null, pk); }
    $('#show-following').onclick=()=>peopleModal('Following', following);
    $('#show-followers').onclick=()=>peopleModal('Followers', followers);
    if(mine){ $('#edit-prof').onclick=()=>editProfile(p); $('#open-settings').onclick=()=>switchView('settings'); }
    else {
      const z=$('#zap-prof'); if(z)z.onclick=()=>doZap(null,pk);
      const mn=$('#prof-menu'); if(mn)mn.onclick=()=>openProfileMenu(pk, mn);
    }
  }
  // the profile "☰ more" menu — Follow / Message / Mute / Block, kept off the header for a clean look
  async function openProfileMenu(pk, anchorBtn){
    const items=[
      ['follow', FOLLOWS.has(pk)?'✓ Following — unfollow':'＋ Follow'],
      ['message','✉ Message'],
      ['mute', MUTED.has(pk)?'🔊 Unmute':'🔇 Mute'],
    ];
    let blossomGranted=false, aiGranted=false;
    if(IS_ADMIN){
      // admin extras: toggle this account's AI + Blossom access + relay block. Fetch current state
      // so each item reads Grant vs Revoke.
      try{ const r=await fetch('/client/ai-access?pubkey='+encodeURIComponent(pk)).then(r=>r.json()); aiGranted=!!(r&&r.enabled); }catch(_){}
      try{ const r=await fetch('/client/blossom-access?pubkey='+encodeURIComponent(pk)).then(r=>r.json()); blossomGranted=!!(r&&r.whitelisted); }catch(_){}
      items.push(['ai', aiGranted?'🤖 Revoke AI access':'🤖 Grant AI access']);
      items.push(['blossom', blossomGranted?'🌸 Revoke Blossom access':'🌸 Grant Blossom access']);
      items.push(['block','🚫 Block (relay)','danger']);
    }
    openMenuPopover(anchorBtn, items, async a=>{
      if(a==='follow'){ await toggleFollow(pk); renderProfileView(pk); return; }
      if(a==='message'){ switchView('messages'); setTimeout(()=>{ if(!dmPeers.has(pk))dmPeers.set(pk,[]); openDm(pk); },50); return; }
      if(a==='mute'){ await toggleMute(pk); renderProfileView(pk); return; }
      if(a==='ai') return toggleAiAccess(pk, !aiGranted);
      if(a==='blossom') return toggleBlossomAccess(pk, !blossomGranted);
      if(a==='block') return doBlock(pk);
    });
  }
  // admin: grant/revoke this account's AI access (the can_ai flag). Signed like doBlock.
  async function toggleAiAccess(pk, grant){
    if(!IS_ADMIN) return;
    try{
      const auth = await sign(27235, 'ai-access', [['action', grant?'grant':'revoke'],['p',pk]]);
      const r = await fetch('/client/ai-access', { method:'POST', headers:{'Content-Type':'application/json'},
        body: JSON.stringify({ target: pk, grant, auth: btoa(JSON.stringify(auth)) }) }).then(r=>r.json());
      toast(r.ok ? (grant?'granted AI access 🤖':'revoked AI access') : ('failed: '+(r.error||'')));
    }catch(e){ toast('AI access change failed'); }
  }
  // admin: grant/revoke this account's Blossom upload access (adds/removes its npub from the
  // blossom_whitelist setting — Admin → Blossom). Signed like doBlock so the server checks admin.
  async function toggleBlossomAccess(pk, grant){
    if(!IS_ADMIN) return;
    try{
      const auth = await sign(27235, 'blossom', [['action', grant?'grant':'revoke'],['p',pk]]);
      const r = await fetch('/client/blossom-access', { method:'POST', headers:{'Content-Type':'application/json'},
        body: JSON.stringify({ target: pk, grant, auth: btoa(JSON.stringify(auth)) }) }).then(r=>r.json());
      toast(r.ok ? (grant?'granted Blossom access 🌸':'revoked Blossom access') : ('failed: '+(r.error||'')));
    }catch(e){ toast('blossom access change failed'); }
  }
  function editProfile(p){
    modal(`<h3>Edit profile</h3>
      <label class="fld">Display name<input class="input" id="pf-name" placeholder="your name" value="${enc(p.name||p.display_name||'')}"></label>
      <label class="fld">NIP-05 identifier<input class="input" id="pf-nip05" placeholder="name@domain" value="${enc(p.nip05||'')}"></label>
      <label class="fld">⚡ Lightning address<input class="input" id="pf-lud16" placeholder="you@walletofsatoshi.com" value="${enc(p.lud16||'')}"></label>
      <label class="fld">Picture URL<input class="input" id="pf-pic" placeholder="https://…" value="${enc(p.picture||'')}"></label>
      <label class="fld">Banner URL<input class="input" id="pf-banner" placeholder="https://…" value="${enc(p.banner||'')}"></label>
      <label class="fld">About<textarea id="pf-about" placeholder="a few words about you">${enc(p.about||'')}</textarea></label>
      <div class="row"><button class="mini" id="pf-up">🖼 upload pic</button><input type="file" id="pf-file" accept="image/*" hidden><span class="spacer"></span><button class="btn btn-neon" id="pf-save">Save</button></div>`, root=>{
      $('#pf-up',root).onclick=()=>$('#pf-file',root).click();
      $('#pf-file',root).onchange=async e=>{ const f=e.target.files[0]; if(!f)return; try{ $('#pf-pic',root).value=await uploadBlob(f); toast('uploaded'); }catch(err){toast('upload failed');} };
      $('#pf-save',root).onclick=async()=>{ const meta={ ...p, name:$('#pf-name',root).value.trim(), nip05:$('#pf-nip05',root).value.trim(), lud16:$('#pf-lud16',root).value.trim(), picture:$('#pf-pic',root).value.trim(), banner:$('#pf-banner',root).value.trim(), about:$('#pf-about',root).value.trim() };
        closeModal(); await publish(0, JSON.stringify(meta), []); Store.saveProfile({pubkey:ME.pubkey,created_at:Math.floor(Date.now()/1000),content:JSON.stringify(meta)}); toast('profile saved'); renderMe(); renderProfileView(ME.pubkey); };
    });
  }
  async function peopleModal(title, pks){
    modal(`<h3>${enc(title)} (${pks.length})</h3><div id="people-list" class="people-list"><div class="spinner"></div></div>`, async root=>{
      const miss=pks.filter(p=>!Store.haveProfile(p)).slice(0,300);
      if(miss.length){ try{ const evs=await Relay.query([{authors:miss,kinds:[0],limit:miss.length}]); evs.forEach(e=>Store.saveProfile(e)); }catch(_){} }
      const list=$('#people-list',root); if(!list) return;
      list.innerHTML = pks.length ? pks.slice(0,400).map(p=>{ const m=Store.profile(p)||{};
        return `<div class="psearch" data-prof="${p}"><img src="${enc(m.picture||LOGO)}" onerror="this.src='${LOGO}'"><div><b>${enc(m.name||m.display_name||NT().nip19.npubEncode(p).slice(0,14))}</b><div class="muted small">${enc(niceNip05(m.nip05)||'')}</div></div></div>`;
      }).join('') : '<div class="empty">Nobody here.</div>';
      $$('[data-prof]',list).forEach(el=> el.onclick=()=>{ closeModal(); renderProfileView(el.dataset.prof); });
    });
  }
  // ---------- AI view (the old PosterChan AI web UI, merged in as a client view) ----------
  let _aiAuth = null;   // cached {can_ai, is_admin, username} for this session
  async function ensureAiSession(){
    if(_aiAuth) return _aiAuth;
    try{
      const auth = await sign(27235, 'ai-login', [['p', ME.pubkey]]);   // prove key ownership
      const r = await fetch('/api/auth/nostr-login', { method:'POST', headers:{'Content-Type':'application/json'},
        body: JSON.stringify({ pubkey: ME.pubkey, auth: btoa(JSON.stringify(auth)) }) }).then(r=>r.json());
      _aiAuth = (r && r.user) ? r.user : { can_ai:false, error:!r };
    }catch(_){ _aiAuth = { can_ai:false, error:true }; }
    return _aiAuth;
  }
  async function renderAI(){
    const feed=$('#feed'); feed.innerHTML='<div class="spinner"></div>';
    const a = await ensureAiSession();
    if(VIEW!=='ai') return;
    if(a.error){ feed.innerHTML='<div class="empty">Could not start an AI session — try again.</div>'; return; }
    if(a.can_ai){ return aiMount(feed); }
    feed.innerHTML=`<div class="ai-view ai-gate">
      <h2>🤖 PosterChan AI</h2>
      <p class="muted">AI access isn't enabled for your account yet. Request access and an admin will approve it.</p>
      <button class="btn btn-neon" id="ai-request">Request AI access</button>
      <div class="muted small" id="ai-request-status"></div></div>`;
    $('#ai-request').onclick=requestAiAccess;
  }
  async function requestAiAccess(){
    const s=$('#ai-request-status'); const b=$('#ai-request'); if(b) b.disabled=true;
    if(s) s.textContent='sending request…';
    try{
      const auth = await sign(27235, 'ai-request', [['p', ME.pubkey]]);
      const r = await fetch('/api/auth/ai-request', { method:'POST', headers:{'Content-Type':'application/json'},
        body: JSON.stringify({ pubkey: ME.pubkey, auth: btoa(JSON.stringify(auth)) }) }).then(r=>r.json());
      if(s) s.textContent = (r && r.ok) ? '✓ Request sent — an admin will approve it.' : ('Could not send request: '+((r&&r.error)||''));
    }catch(_){ if(s) s.textContent='Could not send request.'; }
    if(b) b.disabled=false;
  }

  // ----- the chat itself (ported from the old web UI; talks to /api/ws/chat over the session) -----
  let _ai = { ws:null, convId:null, streamEl:null, streamBuf:"", attach:[] };
  function _cookie(name){ const m=document.cookie.match(new RegExp('(?:^|; )'+name+'=([^;]*)')); return m?decodeURIComponent(m[1]):''; }

  async function aiMount(feed){
    feed.innerHTML=`<div class="ai-chat">
      <div class="ai-bar"><select id="ai-conv" class="input"></select><button class="btn btn-ghost small" id="ai-new">＋ New</button><button class="btn btn-ghost small" id="ai-del" title="delete this chat">🗑️</button></div>
      <div class="ai-msgs" id="ai-msgs"></div>
      <div class="ai-compose">
        <button class="mini" id="ai-attach" title="attach">📎</button><input type="file" id="ai-file" multiple hidden>
        <textarea id="ai-input" class="input" rows="1" placeholder="Message PosterChan AI…  (try: geni a neon city, or /help)"></textarea>
        <button class="btn btn-neon" id="ai-send">▶</button>
      </div>
      <div class="ai-attachbar" id="ai-attachbar"></div>
    </div>`;
    _ai.attach=[];
    $('#ai-new').onclick=()=>aiNewConversation();
    $('#ai-del').onclick=()=>aiDeleteConversation();
    $('#ai-conv').onchange=e=>aiOpenConversation(parseInt(e.target.value,10));
    $('#ai-attach').onclick=()=>$('#ai-file').click();
    $('#ai-file').onchange=e=>aiAddFiles([...e.target.files]).then(()=>{ e.target.value=''; });
    $('#ai-send').onclick=aiSend;
    const ta=$('#ai-input');
    ta.addEventListener('keydown',e=>{ if(e.key==='Enter' && !e.shiftKey){ e.preventDefault(); aiSend(); } });
    ta.addEventListener('input',()=>{ ta.style.height='auto'; ta.style.height=Math.min(ta.scrollHeight,200)+'px'; });
    $('#ai-msgs').addEventListener('click',e=>{ const im=e.target.closest('img'); if(im){ openLightbox(im.dataset.full||im.src); } });
    await aiLoadConversations();
  }
  async function aiLoadConversations(){
    let convs=[]; try{ convs=await fetch('/api/conversations').then(r=>r.json()); }catch(_){}
    const sel=$('#ai-conv'); if(!sel) return;
    sel.innerHTML=(convs||[]).map(c=>`<option value="${c.id}">${enc(c.title||'New Chat')}</option>`).join('');
    if(convs && convs.length) aiOpenConversation(convs[0].id);
    else aiNewConversation();
  }
  async function aiNewConversation(){
    try{
      const c=await fetch('/api/conversations',{ method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({title:'New Chat'}) }).then(r=>r.json());
      const sel=$('#ai-conv'); if(sel){ const o=document.createElement('option'); o.value=c.id; o.textContent=c.title||'New Chat'; sel.prepend(o); sel.value=c.id; }
      aiOpenConversation(c.id);
    }catch(_){ toast('could not start a chat'); }
  }
  async function aiDeleteConversation(){
    const id=_ai.convId; if(!id) return;
    if(!confirm('Delete this chat and all its messages?')) return;
    try{ const r=await fetch('/api/conversations/'+id, { method:'DELETE' }); if(!r.ok) throw 0; }
    catch(_){ toast('delete failed'); return; }
    try{ if(_ai.ws){ _ai.ws.onclose=null; _ai.ws.close(); } _ai.ws=null; }catch(_){}
    _ai.convId=null;
    const sel=$('#ai-conv'); if(sel){ const o=sel.querySelector(`option[value="${id}"]`); if(o) o.remove(); }
    if(sel && sel.options.length){ aiOpenConversation(parseInt(sel.options[0].value,10)); }
    else aiNewConversation();
    toast('chat deleted');
  }
  async function aiOpenConversation(id){
    if(!id) return; _ai.convId=id; _ai.streamEl=null; _ai.streamBuf="";
    const sel=$('#ai-conv'); if(sel && sel.value!=String(id)) sel.value=String(id);
    const box=$('#ai-msgs'); if(box) box.innerHTML='<div class="spinner"></div>';
    let conv=null; try{ conv=await fetch('/api/conversations/'+id).then(r=>r.json()); }catch(_){}
    if(VIEW!=='ai' || _ai.convId!==id) return;
    if(box){ box.innerHTML='';
      for(const m of (conv && conv.messages || [])) aiAddMessage(m.role, m.role==='user'?enc(m.content):mdToHtml(m.content||''));
      aiScroll();
    }
    aiConnect(id);
  }
  function aiConnect(id){
    try{ if(_ai.ws){ _ai.ws.onclose=null; _ai.ws.close(); } }catch(_){}
    const proto = location.protocol==='https:'?'wss':'ws';
    const tok=_cookie('access_token');
    _ai.ws=new WebSocket(`${proto}://${location.host}/api/ws/chat/${id}`+(tok?`?token=${encodeURIComponent(tok)}`:''));
    _ai.ws.onmessage=e=>{ let d; try{ d=JSON.parse(e.data); }catch(_){ return; } aiHandle(d); };
  }
  function aiAddMessage(role, html){
    const box=$('#ai-msgs'); if(!box) return null;
    const el=document.createElement('div'); el.className='ai-msg '+(role==='user'?'user':'assistant');
    el.innerHTML=`<div class="ai-bubble">${html}</div>`; box.appendChild(el); aiScroll(); return el;
  }
  function aiScroll(){ const box=$('#ai-msgs'); if(box) box.scrollTop=box.scrollHeight; }
  function aiHandle(d){
    if(d.type==='stream'){
      const c=(d.data&&d.data.content)??d.content??''; if(typeof c!=='string') return;
      if(!_ai.streamEl){ _ai.streamBuf=''; _ai.streamEl=aiAddMessage('assistant',''); }
      _ai.streamBuf+=c; const b=_ai.streamEl.querySelector('.ai-bubble'); if(b){ b.textContent=_ai.streamBuf; } aiScroll();
    } else if(d.type==='stream_clear'){
      _ai.streamBuf=''; if(_ai.streamEl){ const b=_ai.streamEl.querySelector('.ai-bubble'); if(b) b.textContent=''; }
    } else if(d.type==='stream_end'){
      if(_ai.streamEl){ const b=_ai.streamEl.querySelector('.ai-bubble'); if(b) b.innerHTML=mdToHtml(_ai.streamBuf); }
      _ai.streamEl=null; _ai.streamBuf=''; aiScroll();
    } else if(d.type==='text'){
      aiAddMessage('assistant', mdToHtml(d.content||''));
    } else if(d.type==='response'){
      aiAddMessage('assistant', aiRenderResponse(d.data||{}));
    } else if(d.type==='error'){
      aiAddMessage('assistant', `<span class="ai-err">⚠ ${enc(d.message||'error')}</span>`);
      _ai.streamEl=null; _ai.streamBuf='';
    }
  }
  // Render the rich command payloads (image/music/video gen, search, files) the backend streams.
  function aiRenderResponse(d){
    const head = d.content ? mdToHtml(d.content) : '';
    if(d.type==='generated_image' && d.image) return head+`<div class="ai-media"><img src="data:image/png;base64,${d.image}" alt="generated"></div>`;
    if(d.type==='generated_video' && d.video) return head+`<div class="ai-media"><video controls src="data:video/mp4;base64,${d.video}"></video></div>`;
    if(d.type==='generated_audio' && d.audio){ const fmt=(d.format||'mp3').toLowerCase(); const mime=({mp3:'audio/mpeg',wav:'audio/wav',flac:'audio/flac',opus:'audio/ogg',aac:'audio/aac'})[fmt]||'audio/mpeg';
      return head+`<div class="ai-media"><audio controls src="data:${mime};base64,${d.audio}"></audio></div>`; }
    if(d.type==='images' && Array.isArray(d.images)){
      const items=d.images.slice(0,12).map(im=>{ const src=im.thumb_id?('/api/proxy-image/'+im.thumb_id):(im.img_src||im.thumbnail_src||im.thumbnail||''); const full=im.img_src||src; return src?`<img loading="lazy" src="${enc(src)}" data-full="${enc(full)}">`:''; }).join('');
      return head+`<div class="ai-imggrid">${items}</div>`;
    }
    if(d.type==='search' && Array.isArray(d.results)){
      return head+'<div class="ai-search">'+d.results.map(r=>`<div class="ai-sr"><a href="${enc(r.url||'')}" target="_blank" rel="noopener">${enc(r.title||r.url||'')}</a><div class="muted small">${enc((r.content||'').slice(0,200))}</div></div>`).join('')+'</div>';
    }
    if(d.type==='files' && Array.isArray(d.files)){
      return head+'<div class="ai-files">'+d.files.map(f=>{ const u=f.url||f.path||''; const n=f.filename||f.name||u; return `<a class="ai-file" href="${enc(u)}" target="_blank" rel="noopener">📄 ${enc(n)}</a>`; }).join('')+'</div>';
    }
    return head || mdToHtml(d.text||d.message||'');   // graceful fallback: never drop a payload
  }
  async function aiAddFiles(files){
    for(const f of files){
      const ext=(f.name.split('.').pop()||'').toLowerCase();
      const kind = /^image\//.test(f.type)?'image' : (f.type==='application/pdf'||ext==='pdf')?'pdf'
                 : /^text\/|json|xml|csv|^$/.test(f.type)?'text' : 'doc';
      try{
        if(kind==='text'){ _ai.attach.push({kind, name:f.name, text:await f.text()}); }
        else { const b64=await new Promise((res,rej)=>{ const r=new FileReader(); r.onload=()=>res(String(r.result).split(',')[1]||''); r.onerror=rej; r.readAsDataURL(f); }); _ai.attach.push({kind, name:f.name, ext, b64}); }
      }catch(_){}
    }
    aiRenderAttach();
  }
  function aiRenderAttach(){
    const bar=$('#ai-attachbar'); if(!bar) return;
    bar.innerHTML=_ai.attach.map((a,i)=>`<span class="ai-chip">${enc(a.name)} <button data-i="${i}" class="ai-chip-x">✕</button></span>`).join('');
    $$('.ai-chip-x',bar).forEach(b=> b.onclick=()=>{ _ai.attach.splice(+b.dataset.i,1); aiRenderAttach(); });
  }
  function aiSend(){
    const ta=$('#ai-input'); if(!ta) return; const text=ta.value.trim();
    if(!text && !_ai.attach.length) return;
    if(!_ai.ws || _ai.ws.readyState!==1){ toast('connecting… try again in a moment'); aiConnect(_ai.convId); return; }
    const att=_ai.attach.slice(); _ai.attach=[]; aiRenderAttach();
    const labels=att.map(a=>`📎 ${enc(a.name)}`).join(' ');
    aiAddMessage('user', (text?enc(text):'') + (labels?`<div class="ai-userfiles">${labels}</div>`:''));
    const payload={ type:'message', content:text };
    const imgs=att.filter(a=>a.kind==='image').map(a=>({base64:a.b64, filename:a.name}));   if(imgs.length) payload.images=imgs;
    const pdfs=att.filter(a=>a.kind==='pdf').map(a=>({base64:a.b64, filename:a.name}));       if(pdfs.length) payload.pdfs=pdfs;
    const docs=att.filter(a=>a.kind==='doc').map(a=>({base64:a.b64, filename:a.name, type:a.ext})); if(docs.length) payload.documents=docs;
    const txts=att.filter(a=>a.kind==='text').map(a=>({content:a.text, filename:a.name}));     if(txts.length) payload.files=txts;
    try{ _ai.ws.send(JSON.stringify(payload)); }catch(_){ toast('send failed'); }
    ta.value=''; ta.style.height='auto';
  }

  // ---------- settings view ----------
  // Local working copy of the relay list while editing (committed to ClientSettings on Save).
  let _setRelays = [];
  function renderSettings(){
    const feed=$('#feed');
    const relaysOn = !!ClientSettings.get('relaysEnabled');
    const blossomOn = !!ClientSettings.get('blossomEnabled');
    _setRelays = userRelays();
    if(!_setRelays.length) _setRelays = [''];
    feed.innerHTML = `<div class="settings">
      <section class="set-card">
        <div class="set-head">
          <div><div class="set-title">Relays</div>
            <div class="muted small">By default this app uses the built-in relay. Turn this on to connect to your own relays instead — events from them are signature-verified.</div></div>
          <label class="switch"><input type="checkbox" id="set-relays-on" ${relaysOn?'checked':''}><span class="slider"></span></label>
        </div>
        <div class="set-body ${relaysOn?'':'disabled'}" id="set-relays-body">
          <div id="set-relay-list"></div>
          <div class="set-actions">
            <button class="btn btn-ghost small" id="set-relay-add">＋ Add relay</button>
            <button class="btn btn-ghost small" id="set-relay-ext">⇣ Import from extension</button>
          </div>
          <div class="set-actions">
            <input class="input" id="set-nip05" placeholder="you@domain.com" value="${enc(ME&&niceImport()||'')}">
            <button class="btn btn-ghost small" id="set-relay-nip05">⇣ Import from NIP-05</button>
          </div>
          <div class="muted small">Default built-in relay: <code>${enc(CFG.relay_url||'none')}</code></div>
        </div>
      </section>

      <section class="set-card">
        <div class="set-head">
          <div><div class="set-title">Media server (Blossom)</div>
            <div class="muted small">Where your uploaded images &amp; files are stored. Turn this on to use your own Blossom server instead of the built-in one.</div></div>
          <label class="switch"><input type="checkbox" id="set-blossom-on" ${blossomOn?'checked':''}><span class="slider"></span></label>
        </div>
        <div class="set-body ${blossomOn?'':'disabled'}" id="set-blossom-body">
          <input class="input" id="set-media" placeholder="https://your-blossom-server.com" value="${enc(ClientSettings.get('mediaServer',''))}">
          <div class="muted small">Must be an <code>https://</code> server that allows cross-origin (CORS) uploads. Default built-in: <code>${enc(CFG.blossom_url||'none')}</code></div>
        </div>
      </section>

      <button class="btn btn-neon full" id="set-save">Save &amp; reload</button>
      <div class="muted small set-foot">Changing relays or media server reconnects the app, so it reloads on save.</div>
    </div>`;

    drawRelayRows();
    const syncRelays=()=>{ _setRelays = $$('#set-relay-list .relay-row input').map(i=>i.value.trim()); };

    $('#set-relays-on').onchange=e=>$('#set-relays-body').classList.toggle('disabled', !e.target.checked);
    $('#set-blossom-on').onchange=e=>$('#set-blossom-body').classList.toggle('disabled', !e.target.checked);
    $('#set-relay-add').onclick=()=>{ syncRelays(); _setRelays.push(''); drawRelayRows(); };
    $('#set-relay-ext').onclick=async()=>{ syncRelays(); await importExtensionRelays(); };
    $('#set-relay-nip05').onclick=async()=>{ syncRelays(); await importNip05Relays($('#set-nip05').value.trim()); };
    $('#set-save').onclick=()=>{
      syncRelays();
      const urls=[...new Set(_setRelays.map(u=>normalizeRelay(u)).filter(Boolean))];
      ClientSettings.set('relaysEnabled', $('#set-relays-on').checked);
      ClientSettings.set('relays', urls);
      ClientSettings.set('blossomEnabled', $('#set-blossom-on').checked);
      ClientSettings.set('mediaServer', $('#set-media').value.trim());
      toast('settings saved — reloading'); setTimeout(()=>location.reload(), 600);
    };
  }
  function niceImport(){ const p=Store.profile(ME.pubkey)||{}; return p.nip05?String(p.nip05).replace(/^_@/,''):''; }
  function drawRelayRows(){
    const wrap=$('#set-relay-list'); if(!wrap) return;
    wrap.innerHTML = _setRelays.map((u,i)=>`<div class="relay-row"><span class="rr-dot"></span><input class="input" value="${enc(u)}" placeholder="wss://relay.example.com" data-i="${i}"><button class="mini rr-del" data-i="${i}" title="remove">✕</button></div>`).join('');
    $$('.rr-del',wrap).forEach(b=> b.onclick=()=>{ _setRelays = $$('#set-relay-list input').map(x=>x.value.trim()); _setRelays.splice(+b.dataset.i,1); if(!_setRelays.length)_setRelays=['']; drawRelayRows(); });
  }
  // Normalize a relay URL: default a bare host to wss://, keep an explicit ws://, strip trailing /
  function normalizeRelay(u){
    u=String(u||'').trim(); if(!u) return '';
    if(!/^wss?:\/\//i.test(u)) u='wss://'+u;
    return u.replace(/\/+$/,'');
  }
  function mergeRelays(found){
    const have=new Set(_setRelays.map(normalizeRelay).filter(Boolean));
    let added=0; for(const u of found){ const n=normalizeRelay(u); if(n && !have.has(n)){ have.add(n); added++; } }
    _setRelays=[...have]; if(!_setRelays.length)_setRelays=['']; drawRelayRows();
    return added;
  }
  // NIP-07: window.nostr.getRelays() -> { url: {read,write} }
  async function importExtensionRelays(){
    if(!window.nostr || !window.nostr.getRelays){ toast('no extension relay list'); return; }
    try{ const r=await window.nostr.getRelays(); const urls=Array.isArray(r)?r:Object.keys(r||{});
      const n=mergeRelays(urls); toast(n?`added ${n} relay${n>1?'s':''}`:'no new relays'); }
    catch(_){ toast('extension import failed'); }
  }
  // NIP-05: resolve to a pubkey, then pull its relays from the nostr.json `relays` map, falling
  // back to the author's kind:10002 (NIP-65) relay-list event on the currently connected relay.
  async function importNip05Relays(addr){
    addr=(addr||'').trim().replace(/^@/,''); if(!addr.includes('@')){ toast('enter a name@domain address'); return; }
    const [name,domain]=addr.split('@');
    try{
      const j=await fetch(`https://${domain}/.well-known/nostr.json?name=${encodeURIComponent(name)}`).then(r=>r.json());
      const pk=j&&j.names&&j.names[name];
      let urls=(pk && j.relays && j.relays[pk]) ? j.relays[pk] : [];
      if((!urls||!urls.length) && pk){
        const ev=await Relay.query([{ authors:[pk], kinds:[10002], limit:1 }]);
        if(ev[0]) urls=ev[0].tags.filter(t=>t[0]==='r').map(t=>t[1]);
      }
      if(!urls||!urls.length){ toast('no relays found for that NIP-05'); return; }
      const n=mergeRelays(urls); toast(n?`added ${n} relay${n>1?'s':''}`:'no new relays');
    }catch(_){ toast('NIP-05 lookup failed'); }
  }
  function openThread(id){ renderThread(id); }
  async function renderThread(id){
    VIEW='thread'; _hidePill(); $$('.nav-item[data-view]').forEach(b=>b.classList.remove('active')); $('#view-title').textContent='Thread';
    const feed=$('#feed'); feed.innerHTML='<div class="spinner"></div>';
    let ev=Store.get(id);
    if(!ev){ const r=await Relay.query([{ ids:[id] }]); if(r[0]){ Store.saveEvent(r[0]); ev=r[0]; } }
    if(!ev){ feed.innerHTML='<div class="empty">Post not found on the relay.</div>'; return; }
    // fetch the parent (for reply context) and the replies
    let parent=null;
    const es=ev.tags.filter(t=>t[0]==='e');
    const parentId=((es.find(t=>t[3]==='reply')||es.find(t=>t[3]==='root')||es[es.length-1])||[])[1];
    if(parentId && parentId!==id){ parent=Store.get(parentId); if(!parent){ const r=await Relay.query([{ids:[parentId]}]); if(r[0]){ Store.saveEvent(r[0]); parent=r[0]; } } }
    const replies=(await Relay.query([{ kinds:[1], '#e':[id], limit:100 }])).filter(r=>r.id!==id);
    replies.forEach(r=>{ Store.saveEvent(r); needProfile(r.pubkey); });
    needProfile(ev.pubkey); if(parent) needProfile(parent.pubkey);
    if(VIEW!=='thread') return;
    let html='';
    if(parent) html+=`<div class="thread-parent">${noteHtml(parent)}</div>`;
    html+=`<div class="thread-focus">${noteHtml(ev)}</div>`;
    const rs=replies.sort((a,b)=>a.created_at-b.created_at);
    html+=`<div class="search-section-title">Replies (${rs.length})</div>`;
    html+= rs.length ? rs.map(e=>noteHtml(e)).join('') : '<div class="empty">No replies yet.</div>';
    feed.innerHTML=html; hydrate(feed);
  }

  // ---------- search (NIP-50 posts + profile lookup) ----------
  function bindSearch(){
    const inp=$('#search-input'); if(!inp) return; let t=null;
    inp.addEventListener('input', ()=>{ clearTimeout(t); const q=inp.value.trim(); if(q.length<2) return; t=setTimeout(()=>runSearch(q),350); });
    inp.addEventListener('keydown', e=>{ if(e.key==='Enter'){ const q=inp.value.trim(); if(q) runSearch(q); } });
  }
  async function nip05Resolve(addr){
    let [name, domain] = addr.split('@'); if(!domain){ domain=name; name='_'; }
    // Go through the node's CORS proxy FIRST: most domains' /.well-known/nostr.json lack the
    // Access-Control-Allow-Origin header, so a direct browser fetch fails and the blue check never
    // shows. The proxy fetches server-side (SSRF-guarded) and returns the name→pubkey mapping.
    try {
      const j = await fetch(`/client/nip05?domain=${encodeURIComponent(domain)}&name=${encodeURIComponent(name)}`).then(r=>r.json());
      const pk = (j && ((j.names && j.names[name]) || j.pubkey)) || null;
      if(pk) return pk;
    } catch(_){}
    // fallback: direct (works for the minority of domains that do send CORS headers)
    try {
      const j = await fetch(`https://${domain}/.well-known/nostr.json?name=${encodeURIComponent(name)}`).then(r=>r.json());
      return (j && j.names && j.names[name]) || null;
    } catch(_){ return null; }
  }
  // ---------- NIP-05 verification (blue check) ----------
  // Confirm a profile's claimed name@domain actually points back to its pubkey (per NIP-05),
  // then show a blue ✓. Each handle is fetched once per session: _vP holds the in-flight/settled
  // promise, _vR the resolved boolean for a synchronous peek during (re)decoration.
  const VCHECK = '<span class="verified" title="NIP-05 verified">✓</span>';
  const _nip05vP = new Map();   // "pubkey|handle" -> Promise<bool>
  const _nip05vR = new Map();   // "pubkey|handle" -> bool (settled)
  function verifyNip05(pubkey, nip05){
    if(!pubkey || !nip05 || nip05.indexOf('@') < 0) return Promise.resolve(false);
    const key = pubkey + '|' + nip05.toLowerCase();
    if(_nip05vP.has(key)) return _nip05vP.get(key);
    const pr = nip05Resolve(nip05.toLowerCase())
      .then(pk => { const ok = pk === pubkey; _nip05vR.set(key, ok); return ok; })
      .catch(() => { _nip05vR.set(key, false); return false; });
    _nip05vP.set(key, pr);
    if(_nip05vP.size > 3000){ const k=_nip05vP.keys().next().value; _nip05vP.delete(k); _nip05vR.delete(k); }
    return pr;
  }
  // Fill a `.vchk` slot with the blue check iff the handle verifies. Uses the settled cache for a
  // synchronous paint on re-decoration, and only kicks off a network check the first time.
  function decorateVerified(slot, pubkey, nip05){
    if(!slot) return;
    if(!nip05 || nip05.indexOf('@') < 0){ slot.innerHTML=''; return; }
    const key = pubkey + '|' + nip05.toLowerCase();
    if(_nip05vR.has(key)){ slot.innerHTML = _nip05vR.get(key) ? VCHECK : ''; return; }
    verifyNip05(pubkey, nip05).then(ok => { if(ok && document.contains(slot)) slot.innerHTML = VCHECK; });
  }
  async function runSearch(q){
    VIEW='search'; $$('.nav-item[data-view]').forEach(b=>b.classList.remove('active')); $('#view-title').textContent='Search';
    const feed=$('#feed'); feed.innerHTML='<div class="spinner"></div>';
    // 1. direct npub/hex -> jump to that profile
    const pk=safePk(q); if(pk){ return renderProfileView(pk); }
    // 2. NIP-05 address (name@domain) -> resolve to a pubkey -> jump
    if(/^[\w.\-+]+@[\w.\-]+\.[a-z]{2,}$/i.test(q)){
      const rp=await nip05Resolve(q.toLowerCase());
      if(rp){ return renderProfileView(rp); }
    }
    // 3. posts via NIP-50 full-text (relay indexes note content); profiles by name/nip05 over the
    //    locally-cached profile set (the relay's FTS doesn't cover kind-0, so we match what we know).
    const postEvs = await Relay.query([{ kinds:[1], search:q, limit:40 }]).catch(()=>[]);
    postEvs.forEach(e=>{ Store.saveEvent(e); needProfile(e.pubkey); });
    const ql=q.toLowerCase();
    const profs=Store.profileList().filter(p=>(((p.meta.name||'')+(p.meta.display_name||'')+(p.meta.nip05||'')).toLowerCase().includes(ql))).slice(0,12);
    let html='';
    if(profs.length){ html+='<div class="search-section-title">Profiles</div>'; for(const p of profs){ const m=p.meta; html+=`<div class="psearch" data-prof="${p.pubkey}"><img src="${enc(m.picture||LOGO)}" onerror="this.src='${LOGO}'"><div><b>${enc(m.name||m.display_name||'anon')}</b><div class="muted small">${enc(niceNip05(m.nip05)||(m.about||'').slice(0,60))}</div></div></div>`; } }
    const posts=postEvs.sort((a,b)=>b.created_at-a.created_at);
    html+='<div class="search-section-title">Posts</div>';
    html+= posts.length ? `<div id="search-posts">${posts.map(e=>noteHtml(e)).join('')}</div>` : '<div class="empty">No matching posts.</div>';
    feed.innerHTML=html; hydrate(feed);
    $$('[data-prof]',feed).forEach(el=> el.onclick=()=>renderProfileView(el.dataset.prof));
    // pagination cursor for scroll-back through more search hits
    _search = { q, loading:false, done:posts.length<40, oldest: posts.length ? posts[posts.length-1].created_at : 0 };
  }
  // scroll-back for NIP-50 search results (appends older matching posts under #search-posts)
  let _search = { q:'', oldest:0, loading:false, done:false };
  async function loadOlderSearch(){
    if(_search.loading || _search.done || !_search.q || !_search.oldest) return;
    const cont=$('#search-posts'); if(!cont){ _search.done=true; return; }
    _search.loading=true; const q=_search.q; const feed=$('#feed'); loadSentinel(feed);
    const until=_search.oldest;
    let evs=[]; try{ evs=await Relay.query([{ kinds:[1], search:q, until:until-1, limit:30 }]); }catch(_){}
    clearSentinel(feed);
    if(VIEW!=='search' || _search.q!==q){ _search.loading=false; return; }
    evs.sort((a,b)=>b.created_at-a.created_at);
    let minTs=until; const frag=document.createDocumentFragment();
    for(const ev of evs){
      Store.saveEvent(ev); needProfile(ev.pubkey);
      if(ev.created_at<minTs) minTs=ev.created_at;
      if(cont.querySelector('.note[data-id="'+ev.id+'"]')) continue;
      const div=document.createElement('div'); div.innerHTML=noteHtml(ev); const node=div.firstElementChild; if(node) frag.appendChild(node);
    }
    invalidateCounts();
    if(frag.childElementCount){ cont.appendChild(frag); decorateProfiles(); hydrateLinkCards(feed); hydrateCounts(); }
    if(minTs<_search.oldest) _search.oldest=minTs;
    if(!evs.length || minTs>=until) _search.done=true;
    _search.loading=false;
  }

  // ---------- helpers ----------
  function hydrate(scope){ decorateProfiles(); hydrateLinkCards(scope); hydrateCounts(); }
  // Fetch reactions/reposts/replies for the posts currently on screen and show the counts +
  // liked/reposted state (the timeline sub only carries notes, so without this the counts are 0).
  let _ixT=null;
  function hydrateCounts(){ if(_ixT) return; _ixT=setTimeout(async()=>{
    _ixT=null;
    const ids=[...new Set($$('.note[data-id]').map(n=>n.dataset.id))].slice(0,200);
    if(!ids.length) return;
    try{ const evs=await Relay.query([{ kinds:[1,6,7,9735], '#e':ids, limit:600 }]);
      let any=false; for(const e of evs){ if(Store.saveEvent(e)){ any=true; needProfile(e.pubkey); } }
      if(any){ invalidateCounts(); }
    }catch(_){}
    decorateCounts();
  }, 450); }
  function decorateCounts(){
    $$('.note[data-id]').forEach(n=>{
      const id=n.dataset.id, c=countsFor(id), mr=myReaction(id);
      const setN=(a,v)=>{ const s=n.querySelector('.act[data-a="'+a+'"] .n'); if(s) s.textContent=v||''; };
      setN('reply',c.replies); setN('repost',c.reposts); setN('react',c.reactions); setN('zap',c.zaps?fmtSats(c.zaps):'');
      const rk=n.querySelector('.act[data-a="react"]'); if(rk){ rk.classList.toggle('on',!!mr); if(rk.firstChild) rk.firstChild.textContent=(mr||'😀')+' '; }
      const rt=n.querySelector('.act[data-a="repost"]'); if(rt) rt.classList.toggle('on',c.iRt);
      const zp=n.querySelector('.act[data-a="zap"]'); if(zp) zp.classList.toggle('on',!!c.zaps);
      const bm=n.querySelector('.act[data-a="bookmark"]'); if(bm) bm.classList.toggle('on',BOOKMARKS.has(id));
    });
  }
  function timeAgo(ts){ const s=Math.floor(Date.now()/1000)-ts; if(s<60)return s+'s'; if(s<3600)return (s/60|0)+'m'; if(s<86400)return (s/3600|0)+'h'; return (s/86400|0)+'d'; }
  // ---------- link preview cards (OpenGraph via /client/preview, lazy on scroll) ----------
  const _pv=new Map();
  function firstLink(text){
    const m=(text||'').match(/https?:\/\/[^\s<]+/g); if(!m) return null;
    for(let u of m){ u=u.replace(/[)\].,!?]+$/,''); if(ytId(u)) continue;  // YouTube is embedded inline, not carded
      if(!/\.(jpe?g|png|gif|webp|avif|mp4|webm|mov|m4v|mp3|ogg|wav|m4a|aac|flac)(\?|#|$)/i.test(u)) return u; }
    return null;
  }
  function linkCardHtml(content){ const u=firstLink(content); return u?`<div class="link-card" data-url="${enc(u)}"></div>`:''; }
  // Fill directly on render (the empty placeholder is display:none via CSS until filled; an
  // IntersectionObserver never fires on a zero-height hidden element, which broke lazy loading).
  function hydrateLinkCards(scope){ $$('.link-card[data-url]:not([data-done])', scope||document).forEach(el=>{ el.setAttribute('data-done','1'); fillLinkCard(el); }); }
  async function fetchPreview(url){ if(_pv.has(url)) return _pv.get(url); let d=null; try{ d=await fetch('/client/preview?url='+encodeURIComponent(url)).then(r=>r.json()); }catch(_){} _pv.set(url,d); if(_pv.size>600) _pv.delete(_pv.keys().next().value); return d; }
  async function fillLinkCard(el){
    const url=el.dataset.url; const d=await fetchPreview(url);
    if(!d || (!d.title && !d.image && !d.description)){ el.remove(); return; }
    const host=(()=>{ try{ return new URL(url).hostname.replace(/^www\./,''); }catch(_){ return url; } })();
    el.innerHTML=`${d.image?`<img class="lc-img" src="${enc(d.image)}" loading="lazy" onerror="this.remove()">`:''}<div class="lc-body"><div class="lc-site">${enc(d.site||host)}</div>${d.title?`<div class="lc-title">${enc(d.title)}</div>`:''}${d.description?`<div class="lc-desc">${enc(d.description.slice(0,160))}</div>`:''}</div>`;
    el.onclick=(ev)=>{ ev.stopPropagation(); window.open(url,'_blank','noopener'); };
  }
  // YouTube video id from watch / youtu.be / shorts / embed / live URLs (else null).
  function ytId(u){
    const m=u.match(/(?:youtube\.com\/(?:watch\?(?:[^#]*&)?v=|shorts\/|embed\/|v\/|live\/)|youtu\.be\/)([\w-]{11})/i);
    return m?m[1]:null;
  }
  function linkify(txt){
    let h=enc(txt);
    // images / video / audio embed (extension may be followed by ?query or #frag); else link.
    h=h.replace(/(https?:\/\/[^\s<]+)/g, url=>{
      const u=url.replace(/[)\].,!?]+$/,'');          // don't swallow trailing punctuation
      const tail=url.slice(u.length);
      let tag;
      const yid=ytId(u);
      if(yid) tag=`<span class="yt-embed" data-yt="${yid}" title="play"><img class="yt-thumb" src="https://i.ytimg.com/vi/${yid}/hqdefault.jpg" loading="lazy" onerror="this.src='https://i.ytimg.com/vi/${yid}/0.jpg'"><span class="yt-play">▶</span></span>`;
      else if(/\.(jpe?g|png|gif|webp|avif)(\?|#|$)/i.test(u)) tag=`<img class="m" src="${u}" loading="lazy">`;
      else if(/\.(mp4|webm|mov|m4v)(\?|#|$)/i.test(u)) tag=`<video class="m" src="${u}" controls preload="metadata" playsinline></video>`;
      else if(/\.(mp3|ogg|wav|m4a|aac|flac)(\?|#|$)/i.test(u)) tag=`<br><audio src="${u}" controls preload="none"></audio>`;
      // extensionless Blossom hash URLs (e.g. media.poster.place/<sha256>) — bots post these for
      // nitter/fedi media. Try as an image; if it isn't one, swap to a plain link on error.
      else if(/\/[0-9a-f]{64}(\?|#|$)/i.test(u)) tag=`<img class="m" src="${u}" loading="lazy" onerror="this.onerror=null;window.__blobFallback(this);">`;
      else tag=`<a href="${u}" target="_blank" rel="noopener">${u}</a>`;
      return tag+tail;
    });
    // nostr entities: npub/nprofile → profile mention; note/nevent → EMBEDDED note preview
    // (fetched + patched in place, like a quote); naddr → openable article/addressable link.
    h=h.replace(/(?:nostr:)?((?:npub1|nprofile1|nevent1|note1|naddr1)[0-9a-z]{20,})/gi, (m,ent)=>{
      try{
        const d=NT().nip19.decode(ent);
        if(d.type==='npub' || d.type==='nprofile'){
          const pk = d.type==='npub' ? d.data : d.data.pubkey;
          needProfile(pk); const nm=(Store.profile(pk)||{}).name||(Store.profile(pk)||{}).display_name;
          return `<a href="#" class="mention" data-np="${NT().nip19.npubEncode(pk)}">@${nm?enc(nm):'profile'}</a>`;
        }
        if(d.type==='note' || d.type==='nevent'){
          const id = d.type==='note' ? d.data : d.data.id;
          const o = Store.get(id);
          if(o) return quotedDiv(o);                       // already cached → embed now
          needEvent(id);                                   // else fetch; patchLoaded swaps it in
          return `<div class="quoted muted small" data-qload="${id}">referenced note loading…</div>`;
        }
        if(d.type==='naddr'){                              // addressable event (e.g. NIP-23 article)
          const a=d.data||{};
          return `<a href="#" class="naddrlink" data-pk="${enc(a.pubkey||'')}" data-d="${enc(a.identifier||'')}" data-k="${enc(String(a.kind||''))}">📄 ${a.kind===30023?'article':'view'}</a>`;
        }
      }catch(_){}
      return m;
    });
    // #hashtags → clickable (only when preceded by start/space, so URL #fragments aren't touched)
    h=h.replace(/(^|\s)#([a-z0-9_]{2,30})\b/gi, (m,pre,tag)=>`${pre}<a href="#" class="hashtag" data-tag="${tag.toLowerCase()}">#${tag}</a>`);
    return h;
  }

  // ---------- modal + toast ----------
  function modal(html, onMount){ const bg=document.createElement('div'); bg.className='modal-bg'; bg.innerHTML=`<div class="modal glass neon-border">${html}</div>`; bg.onclick=e=>{ if(e.target===bg) closeModal(); }; $('#modal-root').appendChild(bg); document.body.classList.add('modal-open'); if(onMount)onMount(bg.querySelector('.modal')); }
  function closeModal(){ $('#modal-root').innerHTML=''; document.body.classList.remove('modal-open'); }
  function toast(m){ const t=document.createElement('div'); t.className='toast'; t.textContent=m; $('#toast-root').appendChild(t); setTimeout(()=>t.remove(),3200); }
  function openLightbox(src){ const bg=document.createElement('div'); bg.className='lightbox'; const i=document.createElement('img'); i.src=src; bg.appendChild(i); bg.onclick=()=>bg.remove(); document.body.appendChild(bg); }

  // ---------- right column: Hot / Trending (desktop) ----------
  async function loadRightbar(){
    if(!document.querySelector('.rightbar')) return;
    rankInto('rb-hot', 4*3600, 9);   // Hot = most-engaged posts (last 4h)
    loadTrendingTags();              // Trending = trending hashtags (last 24h)
  }
  // Trending HASHTAGS: tally #tags across recent notes (explicit `t` tags + inline #hashtags),
  // rank by how many distinct posts used each, render clickable chips → a #tag feed.
  async function loadTrendingTags(){
    const el=document.getElementById('rb-trending'); if(!el) return;
    const since=Math.floor(Date.now()/1000)-24*3600;
    let evs=[]; try{ evs=await Relay.query([{ kinds:[1], since, limit:600 }]); }catch(_){}
    const tally={};
    for(const e of evs){
      const seen=new Set();
      for(const t of (e.tags||[])){ if(t[0]==='t' && t[1]){ const g=String(t[1]).toLowerCase().replace(/^#/,''); if(/^[a-z0-9_]{2,30}$/.test(g)) seen.add(g); } }
      for(const m of (e.content||'').matchAll(/(?:^|\s)#([a-z0-9_]{2,30})\b/gi)) seen.add(m[1].toLowerCase());
      for(const g of seen) tally[g]=(tally[g]||0)+1;
    }
    const top=Object.entries(tally).filter(([,c])=>c>=2).sort((a,b)=>b[1]-a[1]).slice(0,14);
    if(!top.length){ el.innerHTML='<div class="muted small">No trending tags yet.</div>'; return; }
    el.innerHTML=`<div class="tag-cloud">${top.map(([g,c])=>`<button class="tag-chip" data-tag="${enc(g)}">#${enc(g)} <span class="tag-n">${c}</span></button>`).join('')}</div>`;
    el.querySelectorAll('.tag-chip').forEach(b=> b.onclick=()=>renderHashtag(b.dataset.tag));
  }
  // a feed of every post carrying a hashtag (NIP-12 `t` filter), with scroll-back pagination
  let _hashtag={ tag:'', oldest:0, loading:false, done:false };
  async function renderHashtag(tag){
    tag=String(tag||'').toLowerCase().replace(/^#/,''); if(!tag) return;
    VIEW='hashtag'; _hidePill(); $$('.nav-item[data-view]').forEach(b=>b.classList.remove('active')); $('#view-title').textContent='#'+tag;
    cleanupInlineStream();
    const feed=$('#feed'); feed.innerHTML='<div class="spinner"></div>';
    let evs=[]; try{ evs=await Relay.query([{ kinds:[1], '#t':[tag], limit:60 }]); }catch(_){}
    evs.forEach(e=>{ Store.saveEvent(e); needProfile(e.pubkey); });
    if(VIEW!=='hashtag') return;
    const posts=evs.filter(e=>e.kind===1).sort((a,b)=>b.created_at-a.created_at);
    feed.innerHTML = `<div class="search-section-title"># ${enc(tag)}</div>` +
      (posts.length ? `<div id="hashtag-posts">${posts.map(noteHtml).join('')}</div>` : `<div class="empty">No posts found for #${enc(tag)} yet.</div>`);
    hydrate(feed);
    _hashtag={ tag, loading:false, done:posts.length<60, oldest: posts.length?posts[posts.length-1].created_at:0 };
  }
  async function loadOlderHashtag(){
    if(_hashtag.loading || _hashtag.done || !_hashtag.tag || !_hashtag.oldest) return;
    const cont=$('#hashtag-posts'); if(!cont){ _hashtag.done=true; return; }
    _hashtag.loading=true; const tag=_hashtag.tag; const feed=$('#feed'); loadSentinel(feed);
    const until=_hashtag.oldest;
    let evs=[]; try{ evs=await Relay.query([{ kinds:[1], '#t':[tag], until:until-1, limit:40 }]); }catch(_){}
    clearSentinel(feed);
    if(VIEW!=='hashtag' || _hashtag.tag!==tag){ _hashtag.loading=false; return; }
    evs.sort((a,b)=>b.created_at-a.created_at);
    let minTs=until; const frag=document.createDocumentFragment();
    for(const ev of evs){ Store.saveEvent(ev); needProfile(ev.pubkey); if(ev.created_at<minTs) minTs=ev.created_at;
      if(cont.querySelector('.note[data-id="'+ev.id+'"]')) continue;
      const div=document.createElement('div'); div.innerHTML=noteHtml(ev); const node=div.firstElementChild; if(node) frag.appendChild(node); }
    invalidateCounts();
    if(frag.childElementCount){ cont.appendChild(frag); decorateProfiles(); hydrateLinkCards(feed); hydrateCounts(); }
    if(minTs<_hashtag.oldest) _hashtag.oldest=minTs;
    if(!evs.length || minTs>=until) _hashtag.done=true;
    _hashtag.loading=false;
  }
  async function rankInto(elId, windowSec, n){
    const el=document.getElementById(elId); if(!el) return;
    const since=Math.floor(Date.now()/1000)-windowSec;
    let evs=[]; try{ evs=await Relay.query([{ kinds:[6,7], since, limit:800 }]); }catch(_){}
    const tally={};
    for(const e of evs){ const id=(e.tags.filter(t=>t[0]==='e').pop()||[])[1]; if(id) tally[id]=(tally[id]||0)+1; }
    const top=Object.entries(tally).sort((a,b)=>b[1]-a[1]).slice(0,n).map(x=>x[0]);
    if(!top.length){ el.innerHTML='<div class="muted small">Nothing yet.</div>'; return; }
    try{ const notes=await Relay.query([{ ids:top }]); notes.forEach(e=>{ Store.saveEvent(e); needProfile(e.pubkey); }); }catch(_){}
    const rows=top.map(id=>{ const ev=Store.get(id); if(!ev||ev.kind!==1) return ''; const pr=profOf(ev.pubkey);
      const txt=(ev.content||'').replace(/https?:\/\/\S+/g,'').trim().slice(0,115);
      return `<div class="rb-item" data-open="${id}" data-pk="${ev.pubkey}"><div class="rb-head"><img class="rb-av" src="${enc(pr.picture||LOGO)}" onerror="this.src='${LOGO}'"><b>${enc(pr.name||pr.display_name||'anon')}</b> <span class="muted">· ${tally[id]} 🔥</span></div><div class="rb-txt">${enc(txt)||'<i>media</i>'}</div></div>`;
    }).filter(Boolean).join('');
    el.innerHTML=rows||'<div class="muted small">Nothing yet.</div>';
    el.querySelectorAll('.rb-item[data-open]').forEach(it=> it.onclick=()=>renderThread(it.dataset.open));
    decorateProfiles();
  }

  document.addEventListener('DOMContentLoaded', boot);
})();
