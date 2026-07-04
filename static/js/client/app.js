/* PosterChan Nostr client controller. Talks only to the built-in relay (window.Relay) and the
 * built-in Blossom server. Crypto runs in the worker (local key) or the NIP-07 extension. */
(function(){
  const NT = () => window.NostrTools;
  const $ = (s,r=document)=>r.querySelector(s);
  const $$ = (s,r=document)=>[...r.querySelectorAll(s)];
  const enc = s => (s==null?'':String(s)).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  let LOGO = '/static/posterchan-relay.png';   // overridden by CFG.logo_url (Admin → custom logo)
  // Repost/boost glyph as an SVG (inherits currentColor → themes green/cyan with a glow), instead of
  // the 🔁 emoji which renders a fixed orange that clashes with the cyberpunk palette.
  const RT_ICON = '<svg class="rt-ico" viewBox="0 0 24 24" width="16" height="16" fill="currentColor" aria-hidden="true"><path d="M23.77 15.67a.75.75 0 00-1.06 0l-2.22 2.22V7.65a3.75 3.75 0 00-3.75-3.75h-5.85a.75.75 0 000 1.5h5.85c1.24 0 2.25 1.01 2.25 2.25v10.24l-2.22-2.22a.75.75 0 10-1.06 1.06l3.5 3.5c.147.147.34.22.53.22s.384-.073.53-.22l3.5-3.5a.75.75 0 000-1.06zm-10.66 3.28H7.26c-1.24 0-2.25-1.01-2.25-2.25V6.46l2.22 2.22a.75.75 0 101.06-1.06l-3.5-3.5a.75.75 0 00-1.06 0l-3.5 3.5a.75.75 0 101.06 1.06l2.22-2.22V16.7a3.75 3.75 0 003.75 3.75h5.85a.75.75 0 000-1.5z"/></svg>';
  // Reply glyph as an SVG (themed cyan + glow like the other action icons) — the 💬 emoji clashed
  // with the cyberpunk palette and read the same as the quote bubble; this reply-arrow is distinct.
  const REPLY_ICON = '<svg class="rp-ico" viewBox="0 0 24 24" width="20" height="20" fill="currentColor" aria-hidden="true"><path d="M10 9V6.5a1 1 0 00-1.7-.71l-6 6a1 1 0 000 1.42l6 6A1 1 0 0010 18.5V16c4.7 0 7.9 1.4 10.2 4.4.3.4 1 .15.96-.35C20.6 13.2 16.4 9.4 10 9z"/></svg>';
  // Quote-post glyph as an SVG (themes cyan + glow, and sizes like the repost icon) — the old ❝
  // text glyph floated high in its line box and couldn't be size-matched to the emoji actions.
  const QUOTE_ICON = '<svg class="q-ico" viewBox="0 0 24 24" width="20" height="20" fill="currentColor" aria-hidden="true"><path d="M1.751 10c0-4.42 3.584-8 8.005-8h4.366c4.49 0 8.129 3.64 8.129 8.13 0 2.96-1.607 5.68-4.196 7.11l-8.054 4.46v-3.69h-.067c-4.49.1-8.183-3.51-8.183-8.01z"/></svg>';
  // Web-of-trust shield — SVG so it takes the neon cyan colour + glow (emoji can't be recoloured).
  const WOT_ICON = '<svg class="wot-ico" viewBox="0 0 24 24" width="13" height="13" fill="currentColor" aria-hidden="true"><path d="M12 2l7 3v6c0 4.7-3.1 8.3-7 11-3.9-2.7-7-6.3-7-11V5l7-3z"/></svg>';
  // "online now" pulse — magenta neon (distinct from the green ONLINE dot above), not another 🟢.
  const LIVE_ICON = '<svg class="live-ico" viewBox="0 0 24 24" width="11" height="11" fill="currentColor" aria-hidden="true"><circle cx="12" cy="12" r="6"/></svg>';
  // "on relay" = raw live socket count connected to the built-in relay right now (broadcast glyph).
  const RELAY_ICON = '<svg class="relay-ico" viewBox="0 0 24 24" width="13" height="13" fill="currentColor" aria-hidden="true"><circle cx="12" cy="12" r="2.6"/><path d="M6.3 6.3a8 8 0 000 11.4l1.5-1.5a6 6 0 010-8.5L6.3 6.3zm11.4 0l-1.5 1.4a6 6 0 010 8.5l1.5 1.5a8 8 0 000-11.4z"/></svg>';
  const isDesktop = () => !window.matchMedia('(max-width:820px)').matches;   // pop-out player is desktop-only
  // ---- UI themes (slugs match static/css/client.css :root[data-theme] + schemas.CLIENT_THEMES) ----
  // Cyberpunk is the flagship default (the bare :root), so it carries NO data-theme attribute.
  const THEMES = [['cyberpunk','Cyberpunk'],['cherryblossom','Cherry Blossom'],
                  ['professional','Professional'],['win98','Windows 98'],['winxp','Windows XP'],
                  ['animegirl','Anime Girl 🌸'],['sovietgothic','Soviet Gothic ☭'],['dark','Dark 🌙']];
  const THEME_SLUGS = new Set(THEMES.map(t=>t[0]));
  // persist defaults true (a SAVED choice). Pass persist=false for a live preview that must NOT stick:
  // preview only repaints; it reverts to the cached/saved theme on reload because pc_theme is untouched.
  // The site-wide default theme the admin set (from /client/config); falls back to professional until
  // CFG loads or if the admin value is stale. Used wherever no per-user/per-device theme is chosen.
  function siteDefaultTheme(){ return THEME_SLUGS.has(CFG&&CFG.default_theme) ? CFG.default_theme : 'professional'; }
  function applyTheme(slug, persist){
    slug = THEME_SLUGS.has(slug) ? slug : siteDefaultTheme();   // site default when unknown/unset
    if(slug==='cyberpunk') document.documentElement.removeAttribute('data-theme');   // cyberpunk = bare :root
    else document.documentElement.setAttribute('data-theme', slug);
    if(persist!==false){ try{ localStorage.setItem('pc_theme', slug); }catch(_){} }   // no-flash re-apply next load
  }
  // Sync the account/Nostr theme on login — it's authoritative and follows you across devices. The
  // cached pc_theme already painted pre-load, so this only corrects an out-of-date device. Safe to
  // always apply (pc_theme only ever holds SAVED themes; preview never persists). MUST establish the
  // session first: /api/auth/settings needs the nostr-login cookie, else it 401s and the saved theme
  // never applies — the "logged in but the default theme loaded despite my saved one" bug.
  async function loadThemeFromServer(){
    try{ await ensureAiSession(); }catch(_){}
    try{ const r=await fetch('/api/auth/settings'); if(r.ok){ const s=await r.json(); if(s&&s.theme) applyTheme(s.theme); } }catch(_){}
  }
  // PWA install: capture the install prompt (fires before the app mounts) so a button can trigger it.
  let _deferredInstall = null;
  window.addEventListener('beforeinstallprompt', e=>{ e.preventDefault(); _deferredInstall=e; const b=$('#btn-install'); if(b) b.classList.remove('hidden'); });
  window.addEventListener('appinstalled', ()=>{ _deferredInstall=null; const b=$('#btn-install'); if(b) b.classList.add('hidden'); });
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
  // AI-chat artifact media (effect/video outputs at /api/files/…enc_<sha>): the URL can be requested a
  // beat before the Blossom blob is readable (the note↔blossom race), and that transient 404 then gets
  // CACHED by the browser/CDN — so it stayed broken until a manual log-out + cache clear. Retry a few
  // times with a cache-BUSTING query so we sidestep the cached 404 and load the instant the artifact is
  // ready; after a few tries hand off to __blobFallback (plain link). Self-heals — no cache clear needed.
  window.__aiMediaRetry = function(el){
    if(!el || !el.dataset) return;
    if(!el.dataset.osrc) el.dataset.osrc = (el.getAttribute('src')||'').split('#')[0].replace(/[?&]_r=\d+/,'');
    const n = (+el.dataset.r || 0), base = el.dataset.osrc;
    if(!base) return;
    if(n >= 5){ if(window.__blobFallback) window.__blobFallback(el); return; }
    el.dataset.r = n + 1;
    setTimeout(()=>{ el.src = base + (base.includes('?')?'&':'?') + '_r=' + Date.now(); if(el.tagName==='VIDEO') el.load(); }, 700 * (n + 1));
  };

  let CFG = {}, ME = null, FOLLOWS = new Set(), FOLLOWERS = new Set(), MUTED = new Set(), MUTED_WORDS = new Set(), PINNED = new Set(), BOOKMARKS = new Set(), VIEW = 'home', IS_ADMIN = false, GUEST = false;
  let _myFollowersLoaded = false;
  let signer = null;
  const subs = {};                 // view -> subId
  const seenNotif = { last: 0 };

  // NIP-17 for signers whose SECRET KEY we never hold (nip07 extension / nip46 remote signer):
  // they do the two key-dependent steps via NIP-44 — sign the kind-13 seal + nip44-encrypt the
  // rumor to the recipient — and the worker does the throwaway ephemeral outer kind-1059 layer.
  // Needs the wallet to support NIP-44 (modern extensions / Amber do). Mirrors the worker's
  // local-key path so bot↔player game DMs decrypt for ALL login types, not just local nsec.
  async function _nip17unwrapVia(nip44dec, wrap){
    const seal = JSON.parse(await nip44dec(wrap.pubkey, wrap.content));
    const rumor = JSON.parse(await nip44dec(seal.pubkey, seal.content));
    if (rumor.pubkey !== seal.pubkey) throw new Error('nip17: seal/rumor author mismatch');
    return rumor;
  }
  async function _eventId(ev){   // NIP-01 event id: sha256 of the canonical [0,pk,ts,kind,tags,content]
    const ser = JSON.stringify([0, ev.pubkey, ev.created_at, ev.kind, ev.tags, ev.content]);
    const buf = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(ser));
    return Array.from(new Uint8Array(buf)).map(b=>b.toString(16).padStart(2,'0')).join('');
  }
  async function _nip17wrapVia(myPk, nip44enc, signEvent, peer, text){
    const now = Math.floor(Date.now()/1000);
    const randPast = () => now - Math.floor(Math.random()*2*86400);   // NIP-59 timing privacy
    async function wrapFor(recipient){
      // The rumor (the real message) ALWAYS addresses the conversation `peer`, even for the
      // self-copy (recipient = myPk) — `recipient` only picks who can DECRYPT this wrap. p-tagging
      // `recipient` here mis-filed our own outgoing DM under our self-thread (so it never showed in
      // the peer's thread), while the peer still got `toPeer` and could reply. Mirrors the worker
      // (signer-worker.js nip17wrap), which wraps ONE peer-addressed rumor for both copies.
      const rumor = { pubkey: myPk, created_at: now, kind: 14, tags: [['p', peer]], content: text };
      rumor.id = await _eventId(rumor);                          // unsigned rumor — id only, no sig
      const sealContent = await nip44enc(recipient, JSON.stringify(rumor));
      const seal = await signEvent({ kind: 13, created_at: randPast(), tags: [], content: sealContent });
      const { wrap } = await Relay.worker.call('giftwrapSeal', { seal, recipient });
      return wrap;
    }
    return { toPeer: await wrapFor(peer), toSelf: await wrapFor(myPk) };
  }

  // ---------- signer abstraction ----------
  function makeSigner(mode, pubkey){
    if (mode === 'nip07'){
      const s = {
        mode, pubkey,
        signEvent: (tpl) => window.nostr.signEvent(tpl),
        nip04enc: (peer, txt) => window.nostr.nip04.encrypt(peer, txt),
        nip04dec: (peer, ct) => window.nostr.nip04.decrypt(peer, ct),
      };
      if (window.nostr && window.nostr.nip44){   // gift-wrapped DMs via the extension's NIP-44
        s.nip17wrap = (peer, text) => _nip17wrapVia(pubkey, (r,pt)=>window.nostr.nip44.encrypt(r,pt),
                                                    (tpl)=>window.nostr.signEvent(tpl), peer, text);
        s.nip17unwrap = (wrap) => _nip17unwrapVia((p,ct)=>window.nostr.nip44.decrypt(p,ct), wrap);
        s.nip44dec = (peer, ct) => window.nostr.nip44.decrypt(peer, ct);
        s.nip44enc = (peer, text) => window.nostr.nip44.encrypt(peer, text);
      }
      return s;
    }
    if (mode === 'nip46'){   // Amber / remote signer (NIP-46): the user's key stays in the signer
      return {
        mode, pubkey,
        signEvent: (tpl) => Nip46.signEvent(tpl),
        nip04enc: (peer, txt) => Nip46.nip04enc(peer, txt),
        nip04dec: (peer, ct) => Nip46.nip04dec(peer, ct),
        nip17wrap: (peer, text) => _nip17wrapVia(pubkey, (r,pt)=>Nip46.nip44enc(r,pt),
                                                 (tpl)=>Nip46.signEvent(tpl), peer, text),
        nip17unwrap: (wrap) => _nip17unwrapVia((p,ct)=>Nip46.nip44dec(p,ct), wrap),
        nip44dec: (peer, ct) => Nip46.nip44dec(peer, ct),
        nip44enc: (peer, text) => Nip46.nip44enc(peer, text),
      };
    }
    return {  // local key — crypto in the worker
      mode, pubkey,
      signEvent: (tpl) => Relay.worker.call('sign', { event: tpl }),
      nip04enc: (peer, txt) => Relay.worker.call('nip04enc', { peer, text: txt }).then(r=>r.ct),
      nip04dec: (peer, ct) => Relay.worker.call('nip04dec', { peer, ct }).then(r=>r.pt),
      nip44dec: (peer, ct) => Relay.worker.call('nip44dec', { peer, ct }).then(r=>r.pt),
      nip44enc: (peer, text) => Relay.worker.call('nip44enc', { peer, text }).then(r=>r.ct),
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
    if(GUEST || !signer){ _guestPrompt(); throw new Error('login required'); }   // read-only guest → nudge to log in
    const ev = await sign(kind, content, tags);
    Store.saveEvent(ev); invalidateCounts();
    const r = await Relay.publish(ev);
    if (!r.ok) toast('relay: ' + (r.msg||'rejected'));
    return { ev, ...r };
  }
  // A guest tried to do something that needs an account → drop the guest chrome and show login.
  function _guestPrompt(){ toast('Log in to interact'); const b=document.getElementById('guest-bar'); if(b) b.remove(); document.body.classList.remove('guest'); showAuth(); }

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
      // Permissions we request up front. Amber prompts per-action so an empty list still works,
      // but iOS signers like Clave PRE-authorize from this list and deny anything not in it
      // ("No permission"). List every op/kind the client signs so the first connect grants them all.
      const kinds=[0,1,3,4,5,6,7,1059,9734,10000,10002,10003,10050,27235,30078];
      const perms=['get_public_key','nip04_encrypt','nip04_decrypt','nip44_encrypt','nip44_decrypt']
        .concat(kinds.map(k=>'sign_event:'+k)).join(',');
      const origin=(location && location.origin) || '';
      const uri=`nostrconnect://${this.appPk}?relay=${encodeURIComponent(relay)}&secret=${secret}`
        +`&perms=${encodeURIComponent(perms)}&name=${encodeURIComponent(name||'PosterChan')}`
        +(origin?`&url=${encodeURIComponent(origin)}`:'');
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
    nip44enc(peer, text){ return this._send('nip44_encrypt',[peer, text]); },
    nip44dec(peer, ct){ return this._send('nip44_decrypt',[peer, ct]); },
  };

  // ---------- NIP-46 SIGNER side: "scan a QR to log in another device" (Primal-style) ----------
  // When you're logged in here with a LOCAL key (the worker holds your nsec), THIS device can act
  // as the remote signer for another machine: scan its nostrconnect:// QR, ack the connection, then
  // answer its get_public_key / sign_event / nipNN_(en|de)crypt requests — signing with your key,
  // which never leaves this device. The mirror image of the Nip46 *client* above.
  const Nip46Signer = {
    ws:null, relay:null, clientPk:null, secret:null, _subId:null, active:false,
    async start(uri, onStatus){
      this.stop();   // drop any previous pairing before linking a new device
      const m=String(uri||'').trim().match(/^nostrconnect:\/\/([0-9a-f]{64})\??(.*)$/i);
      if(!m) throw new Error('that QR is not a nostrconnect login link');
      this.clientPk=m[1].toLowerCase();
      const qs=new URLSearchParams(m[2]||'');
      this.relay=qs.getAll('relay')[0]; this.secret=qs.get('secret')||'';
      const appName=qs.get('name')||'the app';
      if(!this.relay) throw new Error('that QR is missing its relay');
      await this._open(this.relay);
      this.active=true;
      // Unsolicited connect ACK — tells the client our pubkey (this event's author) + echoes the
      // secret, which is exactly what its nostrconnect handshake waits for.
      await this._send({ id:'c'+Math.random().toString(36).slice(2,8), result:this.secret });
      onStatus && onStatus(appName);
      return appName;
    },
    _open(relay){
      return new Promise((res,rej)=>{
        let done=false; const ws=new WebSocket(relay); this.ws=ws;
        ws.onopen=()=>{ this._subId='ns'+Math.random().toString(36).slice(2,8);
          ws.send(JSON.stringify(['REQ', this._subId, { kinds:[24133], '#p':[ME.pubkey], since: Math.floor(Date.now()/1000)-5 }]));
          if(!done){ done=true; res(); } };
        ws.onmessage=(e)=>this._recv(e.data);
        ws.onerror=()=>{ if(!done){ done=true; rej(new Error('cannot reach the relay in the QR')); } };
        ws.onclose=()=>{ if(this.active && this.ws===ws){ this.ws=null; setTimeout(()=>{ if(this.active && !this.ws) this._open(relay).catch(()=>{}); }, 2000); } };
        setTimeout(()=>{ if(!done){ done=true; rej(new Error('relay timed out')); } }, 9000);
      });
    },
    async _decode(ct){ for(const op of ['nip04dec','nip44dec']){ try{ return JSON.parse((await Relay.worker.call(op,{ peer:this.clientPk, ct })).pt); }catch(_){} } return null; },
    async _send(payload){
      const ct=(await Relay.worker.call('nip04enc',{ peer:this.clientPk, text:JSON.stringify(payload) })).ct;
      const tpl={ kind:24133, content:ct, tags:[['p',this.clientPk]], created_at:Math.floor(Date.now()/1000), pubkey:ME.pubkey };
      const signed=await Relay.worker.call('sign',{ event:tpl });
      try{ this.ws && this.ws.send(JSON.stringify(['EVENT', signed])); }catch(_){}
    },
    async _recv(raw){
      let m; try{ m=JSON.parse(raw); }catch(_){ return; }
      if(m[0]!=='EVENT' || m[1]!==this._subId) return;
      const ev=m[2]; if(!ev || ev.kind!==24133 || ev.pubkey!==this.clientPk) return;
      const req=await this._decode(ev.content); if(!req || !req.id || !req.method) return;
      let result=null, error=null;
      try{ result=await this._handle(req.method, req.params||[]); }
      catch(e){ error=String((e&&e.message)||e); }
      await this._send(error ? { id:req.id, result:'', error } : { id:req.id, result });
    },
    async _handle(method, params){
      switch(method){
        case 'connect':        return 'ack';
        case 'ping':           return 'pong';
        case 'get_public_key': return ME.pubkey;
        case 'sign_event': {
          let tpl=params[0]; if(typeof tpl==='string') tpl=JSON.parse(tpl);
          tpl.pubkey=ME.pubkey; if(!tpl.created_at) tpl.created_at=Math.floor(Date.now()/1000);
          return JSON.stringify(await Relay.worker.call('sign',{ event:tpl }));
        }
        case 'nip04_encrypt':  return (await Relay.worker.call('nip04enc',{ peer:params[0], text:params[1] })).ct;
        case 'nip04_decrypt':  return (await Relay.worker.call('nip04dec',{ peer:params[0], ct:params[1] })).pt;
        case 'nip44_encrypt':  return (await Relay.worker.call('nip44enc',{ peer:params[0], text:params[1] })).ct;
        case 'nip44_decrypt':  return (await Relay.worker.call('nip44dec',{ peer:params[0], ct:params[1] })).pt;
        default: throw new Error('unsupported method: '+method);
      }
    },
    stop(){ this.active=false; try{ if(this.ws){ this.ws.onclose=null; this.ws.close(); } }catch(_){} this.ws=null; },
  };

  // Camera QR scanner (uses the native BarcodeDetector; falls back to pasting the link). On a
  // successful scan of a nostrconnect:// link, Nip46Signer logs that device in with our key.
  function _loadScript(src){ return new Promise((res,rej)=>{
    if([...document.scripts].some(s=>s.src.indexOf(src)>=0)) return res();
    const s=document.createElement('script'); s.src=src; s.onload=()=>res(); s.onerror=()=>rej(new Error('load failed')); document.head.appendChild(s); }); }
  // Build a QR detector: native BarcodeDetector (Chrome) if present, else lazy-load jsQR (Firefox /
  // iOS Safari, which have no BarcodeDetector). Returns an async fn(video)->decoded string|null, or
  // null if neither is available.
  async function _qrDetector(){
    if('BarcodeDetector' in window){
      try{ const bd=new BarcodeDetector({ formats:['qr_code'] });
        return async(v)=>{ const c=await bd.detect(v); return (c && c[0] && c[0].rawValue) || null; }; }catch(_){}
    }
    try{ await _loadScript('/static/vendor/qr/jsqr.js?v='+(window.__VER||'')); }catch(_){}
    if(window.jsQR){
      const cv=document.createElement('canvas'); const cx=cv.getContext('2d',{ willReadFrequently:true });
      return (v)=>{ const w=v.videoWidth, h=v.videoHeight; if(!w||!h) return null;
        cv.width=w; cv.height=h; cx.drawImage(v,0,0,w,h);
        const r=window.jsQR(cx.getImageData(0,0,w,h).data, w, h); return (r && r.data) || null; };
    }
    return null;
  }
  async function openQrScanner(){
    if(ME.mode!=='local'){ toast('Log in with your key (nsec) on this device first — extension/remote-signer logins can’t sign for another device'); return; }
    if(!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia){ return qrManualPrompt('Camera needs an HTTPS connection. Paste the link instead:'); }
    const detect=await _qrDetector();
    if(!detect){ return qrManualPrompt('QR scanning isn’t supported in this browser. Paste the link instead:'); }
    modal(`<h3>📷 Scan QR to log in another device</h3>
      <video id="qr-video" class="qr-video" playsinline muted></video>
      <div class="muted small" id="qr-hint">Point at the QR shown on the other device…</div>
      <div class="set-actions"><button class="btn btn-ghost small" id="qr-paste">paste link instead</button>
        <button class="btn btn-ghost small" id="qr-cancel">cancel</button></div>`, async root=>{
      const v=root.querySelector('#qr-video'), hint=root.querySelector('#qr-hint');
      root.querySelector('#qr-cancel').onclick=()=>closeModal();
      root.querySelector('#qr-paste').onclick=()=>{ closeModal(); qrManualPrompt(); };
      let stream=null, stopped=false;
      const cleanup=()=>{ stopped=true; try{ stream && stream.getTracks().forEach(t=>t.stop()); }catch(_){} };
      try{ stream=await navigator.mediaDevices.getUserMedia({ video:{ facingMode:'environment' } }); v.srcObject=stream; await v.play(); }
      catch(e){ hint.textContent='Camera unavailable ('+((e&&e.message)||e)+'). Use “paste link instead”.'; return; }
      const tick=async()=>{
        if(stopped || !document.body.contains(v)){ cleanup(); return; }   // modal closed → stop camera
        try{ const val=await detect(v); if(val && /^nostrconnect:/i.test(val)){ cleanup(); closeModal(); return onQrScanned(val); } }catch(_){}
        setTimeout(tick, 300);
      };
      tick();
    });
  }
  function qrManualPrompt(msg){
    modal(`<h3>Log in another device</h3>
      <p class="muted small">${enc(msg||'On the other device, open Sign in → “Open in Amber / scan QR”, then copy its connection link and paste it here.')}</p>
      <textarea class="input" id="qr-paste-uri" rows="3" placeholder="nostrconnect://…"></textarea>
      <button class="btn btn-neon full" id="qr-paste-go">Log in that device</button>`, root=>{
      root.querySelector('#qr-paste-go').onclick=()=>{ const u=root.querySelector('#qr-paste-uri').value.trim(); if(!u){ return; } closeModal(); onQrScanned(u); };
    });
  }
  async function onQrScanned(uri){
    try{
      const name=await Nip46Signer.start(uri);
      toast('✅ “'+name+'” is now logged in — your key stayed on this device');
    }catch(e){ toast('QR sign-in failed: '+((e&&e.message)||e)); Nip46Signer.stop(); }
  }

  // ---------- boot ----------
  // ---------- shareable URL routing ----------
  // The path IS the entity (njump-style): poster.place/<npub|nprofile> → profile, /<note|nevent>
  // → thread, /users/<name> → that local user. The server serves the SPA for these paths; here we
  // read location.pathname and open the right view, and push the canonical URL as you navigate so
  // every profile/post is linkable + back/forward works.
  let _routing = false;
  function _navUrl(path){
    if(_routing) return;                       // don't re-push while we're decoding the current URL
    // Stay INSIDE the PWA scope (/client, per the manifest). Pushing a bare "/" (or "/note1…") sends the
    // URL OUT of scope, which makes the browser reveal its address-bar/toolbar — the "toolbar appears on
    // login" bug (login's first switchView pushes "/"). When served under /client, prefix the base so the
    // URL stays in-scope; _entityFromPath already strips the /client prefix when decoding.
    const inClient = location.pathname === '/client' || location.pathname.startsWith('/client/');
    const target = inClient ? ('/client' + (path === '/' ? '' : path)) : path;
    try{ if(location.pathname !== target) history.pushState({}, '', target); }catch(_){}
  }
  // Shareable web link for a NIP-19 entity (npub/note/nevent/naddr) → poster.place/<entity>.
  function _webLink(entity){ return (location.origin||'') + '/' + entity; }
  function _entityFromPath(){
    let p; try{ p = decodeURIComponent(location.pathname||'/'); }catch(_){ p = location.pathname||'/'; }
    p = p.replace(/^\/client(?=\/|$)/,'').replace(/^\/+/,'').replace(/\/+$/,'');
    if(!p) return null;
    const seg = p.split('/');
    if(/^users$/i.test(seg[0]) && seg[1]) return { kind:'user', q: seg[1] };
    const m = seg[0].match(/^(?:nostr:)?((?:npub1|nprofile1|note1|nevent1|naddr1)[023456789acdefghjklmnpqrstuvwxyz]+)$/i);
    if(m) return { kind:'bech32', q: m[1] };
    return null;
  }
  async function routeFromPath(){
    const e = _entityFromPath();
    if(!e){ switchView('global'); return; }
    _routing = true;
    try{
      if(e.kind==='user'){
        let pk = safePk(e.q);
        if(!pk){ const name = e.q.includes('@') ? e.q : (e.q + '@' + location.host); pk = await nip05Resolve(name.toLowerCase()); }
        if(pk){ await renderProfileView(pk); return; }
      } else {
        const d = NT().nip19.decode(e.q);
        if(d.type==='npub'){ await renderProfileView(d.data); return; }
        if(d.type==='nprofile'){ await renderProfileView(d.data.pubkey); return; }
        if(d.type==='note'){ openThread(d.data); return; }
        if(d.type==='nevent'){ openThread(d.data.id); return; }
        if(d.type==='naddr'){ await openNaddr(d.data.pubkey, d.data.identifier, d.data.kind); return; }   // open the article/addressable event, not the author's profile
      }
    }catch(err){ console.warn('[route] could not open', e, err); }
    finally{ _routing = false; }
    switchView('global');   // unrecognised/failed → default feed
  }

  // PWA launch params: the home-screen shortcuts (?compose=1 / ?view=<name>) and the Web Share
  // Target (?title=&text=&url=) arrive as a query string on /client. Consume them once on boot, then
  // strip the query (replaceState) so a refresh/restart doesn't replay — e.g. re-pop the composer.
  // Returns true if it took over the initial view so the default switchView('global') is skipped.
  function _consumeLaunchParams(){
    let sp; try{ sp = new URLSearchParams(location.search); }catch(_){ return false; }
    if(![...sp.keys()].length) return false;
    const view = sp.get('view'), wantCompose = sp.has('compose');
    const shared = ['title','text','url'].map(k=>(sp.get(k)||'').trim()).filter(Boolean);
    const _clean = ()=>{ try{ const base=(location.pathname==='/client'||location.pathname.startsWith('/client/'))?'/client':'/'; history.replaceState({},'',base); }catch(_){} };
    // Share target / compose shortcut → open the composer pre-filled (de-duped: a shared URL is
    // often ALSO present in text, so keep unique lines only).
    if(wantCompose || shared.length){
      _clean();
      const seen=new Set(), lines=[]; shared.forEach(s=>{ if(!seen.has(s)){ seen.add(s); lines.push(s); } });
      switchView('home');                 // a sane backdrop behind the modal
      compose({ text: lines.join('\n\n') });
      return true;
    }
    const VALID = new Set(['home','global','notifications','messages','drafts','bookmarks','articles','market','streams','communities','settings']);
    if(view && VALID.has(view)){ _clean(); switchView(view); return true; }
    return false;
  }

  async function boot(){
    CFG = await fetch('/client/config').then(r=>r.json()).catch(()=>({}));
    // Custom branding (Admin → Site Settings): override the logo used as the avatar fallback + brand
    // marks, and point the favicon/splash at it. Blank → keep the built-in PosterChan logo.
    if (CFG.logo_url){
      LOGO = CFG.logo_url;
      try{
        document.querySelectorAll('link[rel="icon"],link[rel="shortcut icon"],link[rel="apple-touch-icon"]').forEach(l=>l.href=CFG.logo_url);
        document.querySelectorAll('.logo-img,.brand-logo').forEach(img=>img.src=CFG.logo_url);
      }catch(_){}
    }
    updateUserCount(); setInterval(()=>updateUserCount(true), 15000);   // WoT size: boot+login only; online: every 15s (onlineOnly → doesn't touch the frozen users count)
    await Store.init();
    if ('serviceWorker' in navigator){
      // auto-reload once when a NEW SW takes control (a deploy update), so it lands on installed
      // PWAs without manual cache-clearing. Only arm this when a controller ALREADY exists at load:
      // on the very first visit the SW's install→clients.claim() also fires controllerchange, which
      // used to trigger a spurious second reload right after login/first load.
      let _refreshing=false;
      if(navigator.serviceWorker.controller){
        navigator.serviceWorker.addEventListener('controllerchange', ()=>{ if(_refreshing) return; _refreshing=true; location.reload(); });
      }
      navigator.serviceWorker.register('/client/sw.js',{scope:'/client'}).catch(()=>{});
    }
    Relay.onStatus = renderConn;
    bindAuth();
    const s = Session.load();
    if (s) { try { await resume(s); return; } catch(e){ console.warn(e); Session.clear(); } }
    // Not logged in → browse PUBLICLY (read-only guest mode): the global feed and any note/profile,
    // like every other Nostr client. Nostr events are public; only posting/reacting needs an account
    // (the guest banner offers login). No more login wall just to read.
    startGuest();
  }
  // Public read-only session: no key, no signer. The guest sentinel ME (empty pubkey) keeps every
  // `ev.pubkey===ME.pubkey` comparison safely false, and publish() blocks writes → "log in to interact".
  function startGuest(){
    ME = { mode:'guest', pubkey:'', npub:'' };
    signer = null;
    startApp();
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
      // QR of the nostrconnect:// URI → scan it with a phone signer (Primal-style mobile login).
      // Fire-and-forget: the QR is a convenience, it must NEVER block/break the actual login flow.
      const qr=$('#amber-nc-qr');
      if(qr){ qr.classList.add('hidden');
        fetch('/client/qr',{method:'POST',headers:{'Content-Type':'text/plain'},body:uri})
          .then(r=> r.ok ? r.blob() : null)
          .then(b=>{ if(b){ qr.src=URL.createObjectURL(b); qr.classList.remove('hidden'); } })
          .catch(()=>{}); }
      $('#amber-nc-status').textContent='waiting for the signer to approve…';
      $('#amber-nc-box').classList.remove('hidden');
      const { userPk, session }=await done; finishAmberLogin(userPk, session);
    }catch(e){ amberErr(e.message||'could not connect'); Nip46.reset(); $('#amber-nc-box').classList.add('hidden'); }
    finally{ btn.disabled=false; btn.textContent='📲 Open in Amber / scan QR'; }
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
  let _gen = null, _captchaToken = null;
  async function genKey(){
    _gen = await Relay.worker.call('genKey', {});
    $('#signup-npub').textContent = _gen.npub; $('#signup-nsec').textContent = _gen.nsec;
    $('#signup-keys').classList.remove('hidden'); $('#btn-gen-key').classList.add('hidden'); $('#btn-signup-go').classList.remove('hidden');
    _ensureCaptchaUI(); loadCaptcha();
  }
  // Captcha gating new-account WoT admission (anti-spam/DDoS). Injected above the "Create account" button.
  function _ensureCaptchaUI(){
    if($('#signup-captcha-box')) return; const go=$('#btn-signup-go'); if(!go||!go.parentNode) return;
    const box=document.createElement('div'); box.id='signup-captcha-box'; box.className='captcha-box';
    box.innerHTML=`<div class="captcha-lbl">🤖 Prove you're human</div>
      <div class="captcha-row"><img id="signup-captcha-img" class="captcha-img" alt="captcha"><button type="button" id="signup-captcha-refresh" class="mini" title="New image">↻</button></div>
      <input id="signup-captcha" class="input" placeholder="enter the code above" autocomplete="off" autocapitalize="characters" maxlength="6">`;
    go.parentNode.insertBefore(box, go);
    $('#signup-captcha-refresh').onclick=loadCaptcha;
    $('#signup-captcha').onkeydown=e=>{ if(e.key==='Enter'){ e.preventDefault(); signupGo(); } };
  }
  async function loadCaptcha(){
    const img=$('#signup-captcha-img'); if(img){ img.src=''; img.alt='loading…'; }
    try{ const r=await fetch('/client/captcha').then(r=>r.json());
      _captchaToken=r.token; if(img) img.src=r.image; const inp=$('#signup-captcha'); if(inp){ inp.value=''; inp.focus(); } }
    catch(_){ if(img) img.alt='captcha failed to load'; }
  }
  async function signupGo(){
    if (!_gen) return;
    const ans = (($('#signup-captcha')||{}).value||'').trim();
    if (!ans){ $('#signup-status').textContent = 'enter the captcha code'; const i=$('#signup-captcha'); if(i) i.focus(); return; }
    // WoT admission is gated by the captcha — do it FIRST. If the captcha fails, abort + reload it and
    // create NO local session, so a bot can't spin up admitted accounts.
    $('#signup-status').textContent = 'checking…';
    let res; try {
      res = await fetch('/client/signup-follow', { method:'POST', headers:{'Content-Type':'application/json'},
        body: JSON.stringify({ pubkey: _gen.pubkey, captcha_token: _captchaToken, captcha_answer: ans }) }).then(r=>r.json());
    } catch(_){ res = { ok:false, error:'net' }; }
    if (res && res.error==='captcha'){ $('#signup-status').textContent = '❌ captcha incorrect — try again'; loadCaptcha(); return; }
    // captcha passed → set up the local session
    $('#signup-status').textContent = 'registering…';
    await Relay.worker.call('setKey', { sk: _gen.sk });
    signer = makeSigner('local', _gen.pubkey); ME = { mode:'local', pubkey: _gen.pubkey, npub: _gen.npub };
    Session.save({ mode:'local', sk: _gen.sk });
    if (res && !res.ok) toast('note: ' + (res.message||res.error||'could not auto-follow'));
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

  // First-run setup: fresh install with no admin → offer to claim it (chicken/egg: nobody can
  // grant AI access until an admin exists). Server re-checks + locks once any admin npub exists.
  async function maybeClaimAdmin(){
    // Only after a REAL login: the guest sentinel is a truthy ME with an empty pubkey, so without the
    // GUEST/pubkey guard this fired on page load before login and then claim-admin'd with no key →
    // "setup failed". (startApp() runs for guests too, and schedules this when admin_unclaimed.)
    if(!CFG.admin_unclaimed || IS_ADMIN || !ME || GUEST || !ME.pubkey) return;
    if(!confirm('This instance has no admin yet.\n\nBecome the admin? You\'ll be able to grant AI/Blossom access to users and manage settings.')) return;
    try{
      const auth=await sign(27235,'claim-admin',[['p',ME.pubkey]]);
      const r=await fetch('/client/claim-admin',{method:'POST',headers:{'Content-Type':'application/json'},
        body:JSON.stringify({pubkey:ME.pubkey,auth:btoa(JSON.stringify(auth))})}).then(r=>r.json());
      if(r&&r.ok){ toast('you are now the admin — reloading'); setTimeout(()=>location.reload(),900); }
      else toast('setup failed: '+((r&&r.error)||''));
    }catch(e){ toast('setup failed'); }
  }

  // ---------- app start ----------
  function startApp(){
    GUEST = !signer;   // a real login always has a signer; the guest sentinel does not
    document.body.classList.toggle('guest', GUEST);
    // ?embed=1 → chrome-less single-note view (for clean screenshots / link-preview captures)
    if(/[?&]embed\b/.test(location.search)) document.body.classList.add('embed');
    updateUserCount();   // refresh the online/WoT count now that we're logged in (id = our pubkey, not anon)
    if(!GUEST) checkBlossomAccess();   // learn Blossom permission (→ nostr.build if none); restoreMediaServer runs on relay-ready (hydrateUser)
    loadThemeFromServer();   // apply the user's Nostr-stored theme on login (best-effort; cache already painted)
    IS_ADMIN = Array.isArray(CFG.admin_npubs) && CFG.admin_npubs.includes(ME.npub);
    { const na=$('#nav-admin'); if(na) na.classList.toggle('hidden', !IS_ADMIN); }   // in-app Admin (admins only)
    // Warm the admin session only. We DON'T preload the hidden admin iframe anymore: /admin extends
    // base.html, whose script unregisters ALL service workers for the origin — including THIS PWA's —
    // forcing a reload that (on Firefox) reveals the browser toolbar and made code-updates churn. The
    // iframe's own SW also served a cached /admin, so even the base.html fix couldn't reach it. Admin
    // now loads on first open instead (slightly slower first paint; no PWA-wide damage).
    if(IS_ADMIN) setTimeout(()=>{ ensureAiSession().catch(()=>{}); }, 1500);
    else if(CFG.admin_unclaimed) setTimeout(maybeClaimAdmin, 1200);   // fresh install: offer first-run admin setup
    // Do NOT auto-request notification permission on login: in a PWA (Firefox especially) the permission
    // prompt pops the browser's chrome — the URL/shield/hamburger TOOLBAR — the instant you sign in, and
    // it can stay. Browsers also discourage auto-requests. In-app toasts work without it; OS
    // notifications are opt-in via Settings (a user gesture) instead. [was: Notification.requestPermission]
    $('#auth-gate').classList.add('hidden'); $('#app').classList.remove('hidden');
    if(GUEST){   // a slim banner offering login; the rest of the app renders read-only
      let gb=document.getElementById('guest-bar');
      if(!gb){ gb=document.createElement('div'); gb.id='guest-bar';
        gb.innerHTML='<span>👁 Viewing publicly — log in to post, reply, react or zap.</span><button class="btn btn-neon small" id="guest-login">Log in / Sign up</button>';
        document.body.appendChild(gb); }
      const gl=document.getElementById('guest-login'); if(gl) gl.onclick=()=>{ const b=document.getElementById('guest-bar'); if(b) b.remove(); document.body.classList.remove('guest'); showAuth(); };
    }
    $('#btn-logout').onclick = logout;
    { const b=$('#btn-install'); if(b){
        if(_deferredInstall) b.classList.remove('hidden');   // prompt already captured before mount
        b.onclick=async()=>{ if(!_deferredInstall) return; _deferredInstall.prompt();
          try{ await _deferredInstall.userChoice; }catch(_){} _deferredInstall=null; b.classList.add('hidden'); }; } }
    $('#me-card').onclick = ()=>renderProfileView(ME.pubkey);
    { const nm=$('#nav-music'); if(nm) nm.onclick=openMusic; }
    // Collapsible "Files" group (Blossom + Music), like Games/Discover.
    { const ft=$('#files-toggle'); if(ft){ const sub=$('#files-sub'), chev=$('#files-chev');
        const apply=o=>{ if(sub) sub.classList.toggle('collapsed', !o); if(chev) chev.textContent=o?'▾':'▸'; };
        apply(ClientSettings.get('filesOpen', false));
        ft.onclick=()=>{ const o=!ClientSettings.get('filesOpen', false); ClientSettings.set('filesOpen', o); apply(o); }; } }
    $$('.nav-item[data-view]').forEach(b=> b.onclick = ()=>switchView(b.dataset.view));
    // Collapsible "Discover" group (Articles / Streams / Communities) in the sidebar.
    { const dt=$('#disc-toggle'); if(dt){ const sub=$('#disc-sub'), chev=$('#disc-chev');
        const apply=o=>{ if(sub) sub.classList.toggle('collapsed', !o); if(chev) chev.textContent=o?'▾':'▸'; };
        apply(ClientSettings.get('discOpen', false));   // collapsed on first load — Discover is too cluttery open
        dt.onclick=()=>{ const o=!ClientSettings.get('discOpen', false); ClientSettings.set('discOpen', o); apply(o); }; } }
    // Collapsible "Games" group (Chess).
    { const gt=$('#games-toggle'); if(gt){ const sub=$('#games-sub'), chev=$('#games-chev');
        const apply=o=>{ if(sub) sub.classList.toggle('collapsed', !o); if(chev) chev.textContent=o?'▾':'▸'; };
        apply(ClientSettings.get('gamesOpen', false));
        gt.onclick=()=>{ const o=!ClientSettings.get('gamesOpen', false); ClientSettings.set('gamesOpen', o); apply(o); }; } }
    $('#btn-compose').onclick = ()=>compose(); $('#btn-compose-m').onclick = ()=>compose();
    // mobile overflow sheet — delegated so the tap is caught even if the node is re-created
    document.addEventListener('click', e=>{ if(e.target.closest && e.target.closest('#btn-more-m')){ e.preventDefault(); moreMenu(); } });
    $('#btn-refresh').onclick = ()=>renderView(true);
    bindSearch();
    bindFeedActions();
    // Media-grid toggle: flip Home/Global between the normal post list and an images-only picture grid.
    { const mt=$('#tl-media'); if(mt) mt.onclick=()=>{ _tlMedia=!_tlMedia; ClientSettings.set('tlMedia', _tlMedia);
        mt.classList.toggle('active', _tlMedia); if(VIEW==='home'||VIEW==='global') _drawTimeline(false); }; }
    $('#feed').addEventListener('scroll', onFeedScroll, { passive:true });   // infinite scroll-back
    bindMobileGestures();   // pull-to-refresh + swipe between primary tabs (mobile/PWA)
    // Perf/battery: pause ALL CSS animations (cyberpunk city parallax, glows) when the tab/PWA is
    // backgrounded — the GPU idles when you're not looking (laptop heat + mobile battery).
    let _hiddenAt = 0, _lastWake = 0;
    // Reconnect the relay + refetch the feed on resume. Debounced (4s) because a mobile resume fires
    // several of these signals close together. wake() reopens every socket; onReconnect re-runs the
    // per-user hydration + re-renders the feed view (see Relay.onReconnect below).
    const _resumeRelay = ()=>{ if(Date.now() - _lastWake < 4000) return; _lastWake = Date.now(); try{ Relay.wake(); }catch(_){} };
    document.addEventListener('visibilitychange', ()=>{ document.body.classList.toggle('anim-off', document.hidden);
      if(document.hidden){ _hiddenAt = Date.now(); return; }
      // Resumed to the foreground. A mobile PWA's relay WebSocket is frozen while backgrounded and very
      // often comes back DEAD-but-"open" (zombie) — the feed then looks stuck / a query "relay timeouts".
      // If we were away long enough for the OS to have suspended the socket, force a fresh relay
      // connection so the feed reconnects instantly instead of hanging on a dead socket.
      if(Date.now() - _hiddenAt > 6000) _resumeRelay();
      // Also: if an AI reply was still pending when we backgrounded, kick the recovery poll now (a slow
      // effect/video can finish while hidden where the timed recoverWatch is throttled). aiRecover self-guards.
      if(VIEW==='ai' && _ai && _ai.awaiting && _ai.convId) aiRecover(_ai.convId); });
    // visibilitychange alone is unreliable on a phone waking from OFF: it can fire BEFORE the radio is
    // back, so wake()'s reconnect fails and the feed hangs on "request timeout" with no new posts. The
    // `online` event (network actually returned) and `pageshow` w/ persisted (restored from bfcache)
    // give reliable second chances to reconnect + refetch. Debounced so they don't stack with the above.
    window.addEventListener('online', _resumeRelay);
    window.addEventListener('pageshow', e=>{ if(e && e.persisted) _resumeRelay(); });
    if(document.hidden) document.body.classList.add('anim-off');
    const rb=document.querySelector('.rightbar');
    if(rb){ rb.addEventListener('scroll', onRightbarScroll, { passive:true });   // Hot infinite-scroll
      rb.addEventListener('click', e=>{ const it=e.target.closest('.rb-item[data-open]'); if(it) renderThread(it.dataset.open); });   // hot + follows rows open their thread
      startAutoScroll(); }
    bumpDraft();   // show the saved-drafts count on the nav badge
    Drafts.pull();   // sync drafts from the encrypted Nostr event (cross-device)
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
    const _deepLink = _entityFromPath();   // /<npub>, /<nevent>, /users/<name> → open it once the relay's up
    // Per-user data load (mutes/follows/pins/bookmarks/profile + notification & deletion subs).
    // CRITICAL: onReady fires only ONCE per page load. The page connects to the relay while you're
    // still a GUEST (to show the public feed), so onReady fires THEN — with GUEST=true, which skips
    // this. When you log in WITHOUT a reload, startApp() runs again but the relay is already connected,
    // so onReady never re-fires → this never ran for the logged-in user → empty mutes/follows/notifs
    // until a manual refresh (which loads already-authed). So we ALSO call hydrateUser() directly after
    // connectRelays() when the socket is already open (see below). _hydrated guards against double-run.
    let _hydrated = false;
    const hydrateUser = ()=>{
      if(GUEST || _hydrated) return; _hydrated = true;
      restoreMediaServer();   // restore the synced media server (kind-10063/10096) — must run AFTER the
                              // relay is connected (this fires on onReady), else the query returns nothing
      Promise.allSettled([fetchFollows(), fetchMutes(), fetchPins(), fetchBookmarks(), fetchMyProfile()])
        .then(()=>{ if(!GUEST && ['home','global','notifications','messages','bookmarks'].includes(VIEW)){ try{ renderView(true); }catch(_){} } });
      watchNotifications(); watchDeletions();
      setTimeout(()=>ensureDMs(), 3000); setTimeout(()=>ensureDmInboxList(), 3500);
    };
    Relay.onReady = ()=>{
      hydrateUser();
      setTimeout(loadRightbar, 1500);
      if(_entityFromPath()) routeFromPath(); };   // deep-link needs relay data (profile/thread fetch)
    // On a RECONNECT (socket dropped + came back, common during the login burst), the relay re-arms
    // live subs but NOT one-shot query() subs — so follows/mutes/pins/bookmarks fired on first connect
    // are lost and home/mutes show empty until a manual refresh, while the live notifications sub
    // recovers (the reported "1 notification, 0 home, 0 mutes"). Re-run the one-shot hydration here.
    Relay.onReconnect = ()=>{
      if(GUEST) return;
      // Re-render only the views whose content depends on this per-user data AND that renderView()
      // handles cleanly — NOT thread/channel/group/search/hashtag/other-profile (renderView has no
      // case for those, so it'd blank them to a spinner). The fetches also self-render these.
      Promise.allSettled([fetchFollows(), fetchMutes(), fetchPins(), fetchBookmarks(), fetchMyProfile()])
        .then(()=>{ if(!GUEST && ['home','global','notifications','messages','bookmarks'].includes(VIEW)){ try{ renderView(true); }catch(_){} } });
    };
    connectRelays();
    // Guest→login WITHOUT a page reload: the relay is already connected from guest browsing, so the
    // once-only onReady won't fire again — run the per-user hydration now. (On a fresh/refresh load the
    // socket isn't open yet here, _ready is false, and onReady will fire it on connect. _hydrated dedupes.)
    if(!GUEST && Relay._ready) hydrateUser();
    renderMe();
    // Deep-linked entity: spinner until onReady routes it (the relay must be connected to fetch the
    // profile/note); otherwise land on the global feed immediately.
    if(_deepLink){ VIEW='thread'; $('#feed').innerHTML='<div class="spinner"></div>'; }
    else if(!_consumeLaunchParams()) switchView('global');   // PWA shortcut/share, else land on Nostrverse (global feed)
    window.addEventListener('popstate', ()=>{ if(ME) routeFromPath(); });   // back/forward
    setInterval(refreshRightbar, 150000);   // routinely refresh trending + prepend new hot posts (rightbar only on home/global)
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
  // Sidebar community stats under ONLINE: network size (WoT) + who's using the site right now.
  // Network size (WoT) comes from CFG once (it barely changes — daily rebuild); only "online now" is
  // polled. So updateUserCount fetches just the live online count, not the WoT size.
  // Stable id so the server's "online" count is per-USER, not per-connection: the logged-in pubkey
  // (truncated — enough to dedup, avoids logging full keys) when signed in, else a per-browser anon
  // id persisted in localStorage (so multiple tabs collapse to one viewer).
  function _viewerId(){
    try{
      if(window.ME && ME.pubkey) return 'k'+ME.pubkey.slice(0,16);
      let a=localStorage.getItem('pc_vid');
      if(!a){ a='a'+Math.random().toString(36).slice(2,10)+Date.now().toString(36); localStorage.setItem('pc_vid',a); }
      return a;
    }catch(_){ return ''; }
  }
  let _lastOnline=0, _lastRelay=0;   // cached for the mobile More sheet (which is built synchronously)
  async function updateUserCount(onlineOnly){
    const uc=$('#user-count');
    let online=0, users=Number(CFG.users)||0, relay=0;
    try{
      const s=await fetch('/client/stats?v='+encodeURIComponent(_viewerId())).then(r=>r.json());
      online=Number(s.online)||0;
      relay=Number(s.relay)||0;
      if(online>0) _lastOnline=online;
      // WoT size refreshes ONLY on boot/login (it barely changes — daily rebuild). The 15s poll passes
      // onlineOnly=true so the users count stays frozen and only the live "online" number updates.
      if(!onlineOnly && Number(s.users)>0){ users=Number(s.users); CFG.users=users; }
    }catch(_){}
    if(relay>0) _lastRelay=relay;   // cached for the mobile More sheet (built synchronously)
    if(!uc) return;   // sidebar element absent (mobile) — _lastOnline/_lastRelay cached above for the More sheet
    const parts=[];
    if(users>0) parts.push(`<span class="uc-stat">${WOT_ICON} ${users.toLocaleString()} users</span>`);
    if(online>0) parts.push(`<span class="uc-stat">${LIVE_ICON} ${online.toLocaleString()} online</span>`);
    if(relay>0) parts.push(`<span class="uc-stat" title="People connected to this relay right now">${RELAY_ICON} ${relay.toLocaleString()} on relay</span>`);
    if(parts.length){ uc.innerHTML=parts.join(''); uc.classList.remove('hidden'); }
    else uc.classList.add('hidden');
  }
  function renderMe(){
    if(GUEST){ const mc=$('#me-card'); if(mc){ mc.innerHTML=`<img src="${LOGO}"><div><div class="mn">Guest</div></div>`; mc.onclick=_guestPrompt; } return; }
    const p = Store.profile(ME.pubkey) || {};
    const av = p.picture || LOGO;
    // One line only: show the username if set — that's all that's needed. No username → fall back to
    // the NIP-05 handle, then a short npub. (No separate npub line cluttering it under the name.)
    const label = p.name || p.display_name || niceNip05(p.nip05) || (ME.npub.slice(0, 12) + '…');
    $('#me-card').innerHTML = `<img src="${enc(av)}" onerror="this.src='${LOGO}'"><div><div class="mn">${enc(label)}</div></div>`;
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
      MUTED = new Set(e.tags.filter(t=>t[0]==='p'&&t[1]).map(t=>t[1]));
      // NIP-51 muted words/phrases (lowercase) — hide matching posts from the timeline.
      MUTED_WORDS = new Set(e.tags.filter(t=>t[0]==='word'&&t[1]).map(t=>t[1].toLowerCase())); }
    // mutes can finish loading AFTER the first view render — refresh a filtered view so already-
    // shown muted users drop out of the feed / notifications / messages without a manual reload.
    if(['home','global','notifications','messages'].includes(VIEW)){ try{ renderView(true); }catch(_){} }
  }
  // Replace the `word` tags on the kind-10000 mute list, preserving p/t/e mutes. NIP-51, so the
  // list follows the user to any client.
  async function saveMutedWords(words){
    const clean=[...new Set(words.map(w=>String(w||'').trim().toLowerCase()).filter(Boolean))];
    const evs = await Relay.query([{ authors:[ME.pubkey], kinds:[10000], limit:1 }]);
    const cur = evs.length ? evs.sort((a,b)=>b.created_at-a.created_at)[0] : null;
    // Keep all non-word tags. If the relay didn't return our list (race / not-yet-synced), DON'T
    // start from empty — rebuild person-mutes from in-memory MUTED so we never wipe them.
    const base = cur ? cur.tags.filter(t=>t[0]!=='word') : [...MUTED].map(p=>['p',p]);
    const tags = base.concat(clean.map(w=>['word',w]));
    await publish(10000, cur?cur.content:'', tags);
    MUTED_WORDS = new Set(clean);
  }
  // True if a note's text contains any muted word/phrase (substring, case-insensitive). Applied to
  // the timeline feeds so muted-word posts never render.
  function mutedByWord(ev){
    if(!MUTED_WORDS.size || !ev || ev.kind!==1 || !ev.content) return false;
    const c=ev.content.toLowerCase();
    for(const w of MUTED_WORDS){ if(c.includes(w)) return true; }
    return false;
  }
  // Should this timeline event be hidden by mutes? Covers REPOSTS (kind 6): a repost of a muted
  // author — or of a note containing a muted word — is hidden too, by resolving the original
  // (embedded JSON, else the store; author from the p-tag as a fallback before it loads).
  function isMutedView(ev){
    if(!ev) return false;
    if(MUTED.has(ev.pubkey) || mutedByWord(ev)) return true;
    if(ev.kind===6){
      let inner=null; try{ inner=JSON.parse(ev.content); }catch(_){}
      const orig = inner || Store.get((ev.tags.find(t=>t[0]==='e')||[])[1]);
      const origPk = (orig && orig.pubkey) || (ev.tags.find(t=>t[0]==='p')||[])[1];
      if(origPk && MUTED.has(origPk)) return true;
      if(orig && mutedByWord(orig)) return true;
    }
    return false;
  }
  // replaceable-list edit helper: fetch newest (kind), add/remove a p-tag, republish preserving content+tags
  async function _editPList(kind, pk, add){
    const evs = await Relay.query([{ authors:[ME.pubkey], kinds:[kind], limit:1 }]);
    const cur = evs.length ? evs.sort((a,b)=>b.created_at-a.created_at)[0] : null;
    // Source of truth = UNION of the relay's current list and our in-memory set. A bare relay read is
    // unreliable: it can be empty (not-yet-synced) OR STALE — returning a version from before the last
    // follow finished indexing. Trusting it alone is how follows "kept getting forgotten" (each new
    // follow republished from a stale base and dropped the previous one). The union can never silently
    // lose a follow/mute we already know about. Non-p tags (kind-3 relay hints, kind-10000 muted words)
    // are preserved from cur, or rebuilt from memory when the relay returned nothing.
    const inmem = kind===3 ? FOLLOWS : kind===10000 ? MUTED : new Set();
    const fromRelay = cur ? cur.tags.filter(t=>t[0]==='p'&&t[1]).map(t=>t[1]) : [];
    const pset = new Set([...inmem, ...fromRelay]);
    if (add) pset.add(pk); else pset.delete(pk);
    const nonP = cur ? cur.tags.filter(t=>t[0]!=='p')
                     : (kind===10000 ? [...MUTED_WORDS].map(w=>['word',w]) : []);
    // Don't publish a self-follow p-tag (ME is kept in FOLLOWS only for the home-feed filter).
    const tags = nonP.concat([...pset].filter(p=>p!==ME.pubkey).map(p=>['p',p]));
    await publish(kind, cur?cur.content:'', tags);
  }
  // Follow many at once (e.g. "Follow all back") in a SINGLE kind-3 publish, merged onto the union of
  // the relay's current list + in-memory FOLLOWS (same anti-wipe rule as _editPList). Returns the count
  // actually added so callers can toast/refresh.
  async function followMany(pks){
    const evs = await Relay.query([{ authors:[ME.pubkey], kinds:[3], limit:1 }]);
    const cur = evs.length ? evs.sort((a,b)=>b.created_at-a.created_at)[0] : null;
    const pset = new Set([...FOLLOWS, ...(cur?cur.tags.filter(t=>t[0]==='p'&&t[1]).map(t=>t[1]):[])]);
    let added=0;
    for(const pk of pks){ if(pk && pk!==ME.pubkey && !pset.has(pk)){ pset.add(pk); FOLLOWS.add(pk); added++; } }
    if(!added) return 0;
    const nonP = cur ? cur.tags.filter(t=>t[0]!=='p') : [];
    await publish(3, cur?cur.content:'', nonP.concat([...pset].filter(p=>p!==ME.pubkey).map(p=>['p',p])));
    return added;
  }
  async function toggleFollow(pk){
    const have=FOLLOWS.has(pk); await _editPList(3, pk, !have);
    have?FOLLOWS.delete(pk):FOLLOWS.add(pk); toast(have?'unfollowed':'followed');
  }
  // Who follows ME — the authors of kind-3 contact lists that p-tag my pubkey. Loaded once, lazily
  // (on the first people list), then cached. Used to badge "Follows you" / mark mutuals.
  async function ensureMyFollowers(){
    if(_myFollowersLoaded || !ME) return;
    _myFollowersLoaded = true;   // set first so concurrent callers don't double-query
    try{
      const evs = await Relay.query([{ kinds:[3], '#p':[ME.pubkey], limit:1000 }]);
      evs.forEach(e=> FOLLOWERS.add(e.pubkey));
    }catch(_){ _myFollowersLoaded = false; }   // allow a retry if the query failed
  }
  async function toggleMute(pk){
    const have=MUTED.has(pk); await _editPList(10000, pk, !have);
    have?MUTED.delete(pk):MUTED.add(pk); toast(have?'unmuted':'muted'); if(['home','global','notifications','messages'].includes(VIEW)) renderView(true);
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
      // blue check is profile-only (saves a NIP-05 resolution per timeline author)
    }});
    $$('.rb-item[data-pk]').forEach(n=>{ const p=Store.profile(n.dataset.pk); if(p){
      const a=n.querySelector('.rb-av'); if(p.picture && a) a.src=p.picture;
      const b=n.querySelector('b'); if(b) b.textContent=p.name||p.display_name||b.textContent;
    }});
    // DM list rows + open-thread header: fill name (name → display_name → nip05) + avatar once the
    // peer's kind-0 arrives, so they don't stay stuck on the raw npub.
    $$('.dm-peer[data-peer]').forEach(n=>{ const p=Store.profile(n.dataset.peer); if(p){
      const a=n.querySelector('.dmav'); if(p.picture && a) a.src=p.picture;
      const b=n.querySelector('b'); if(b){ const nm=p.name||p.display_name||niceNip05(p.nip05); if(nm) b.textContent=nm; }
    }});
    $$('.dm-peer-name[data-prof]').forEach(nm=>{ const p=Store.profile(nm.dataset.prof); if(p){ const t=p.name||p.display_name||niceNip05(p.nip05); if(t) nm.textContent=t; }});
    // embedded/quoted notes — fill avatar + name + nip05 once the referenced author's profile loads
    $$('.quoted .name[data-prof]').forEach(nm=>{ const pk=nm.dataset.prof; const p=Store.profile(pk); if(p){
      const q=nm.closest('.quoted'); const a=q&&q.querySelector('.qav'); if(p.picture && a) a.src=p.picture;
      nm.textContent=p.name||p.display_name||nm.textContent;
      const h=q&&q.querySelector('.handle'); const nip=niceNip05(p.nip05); if(h && nip) h.textContent=nip;
      // blue check is profile-only (saves a NIP-05 resolution per timeline author)
    }});
  }

  // ---------- view routing ----------
  function switchView(v){
    if(window.PC_NOSTR_ONLY && v==='ai') v='home';   // AI disabled in Nostr-only deployments
    // Leaving Messages clears the open conversation so RE-entering Messages shows the list (not the last
    // thread). The profile "message @user" action sets dmActive THEN calls switchView (from a non-messages
    // view), so this guard won't wipe it. Without it, fix for the mobile thread-overlay would auto-open.
    if(VIEW==='messages' && v!=='messages') dmActive=null;
    _navUrl('/');   // top-level views aren't entity URLs — reset the address bar to the root
    VIEW = v;
    if(v==='notifications') _notifShown = 25;   // fresh entry → collapse pagination back to one page
    $$('.nav-item[data-view]').forEach(b=> b.classList.toggle('active', b.dataset.view===v));
    $('#view-title').textContent = { home:'Home', global:'Nostrverse', notifications:'Notifications', messages:'Messages', drafts:'Drafts', bookmarks:'Bookmarks', articles:'Articles', market:'Market 🛍️', streams:'Streams', communities:'Communities', pics:'Pics', chat:'Chat', torrents:'Torrents 🧲', repos:'Git Repos 🌱', '4chan':'4chan', chess:'Chess ♟️', ttt:'Tic-Tac-Toe ⭕', hangman:'Hangman 🎯', connect4:'Connect Four 🔴', blackjack:'Blackjack 🃏', holdem:"Texas Hold'em 🃏", blossom:'Files', profile:'Profile', settings:'Settings', ai:'PosterChan AI', admin:'Admin' }[v]||v;
    // Media-grid toggle button lives in the topbar but only applies to the Home/Global timelines.
    { const mt=$('#tl-media'); if(mt){ const show=(v==='home'||v==='global'); mt.classList.toggle('hidden', !show); mt.classList.toggle('active', show && _tlMedia); } }
    renderView(true);
  }
  function renderView(reset){
    cleanupInlineStream();   // leaving a view tears down the inline stream player (unless popped out)
    const feed = $('#feed');
    if(VIEW!=='ai' && _ai && _ai.ws){ try{ _ai.ws.onclose=null; _ai.ws.close(); }catch(_){} _ai.ws=null; }
    if(VIEW!=='channel' && _chatSub){ try{ Relay.close(_chatSub); }catch(_){} _chatSub=null; }   // leaving a chat room → drop its live sub
    if(VIEW!=='channel' && _chatReactPoll){ clearTimeout(_chatReactPoll); _chatReactPoll=null; }   // …and its reaction poll
    if(VIEW!=='group' && _groupPoll){ clearTimeout(_groupPoll); _groupPoll=null; }   // leaving a NIP-29 group → stop polling its relay
    feed.classList.toggle('feed-chat', VIEW==='channel' || VIEW==='group');   // never true here (both opened directly) → clears on leave
    if(VIEW!=='home' && VIEW!=='global') _hidePill();
    feed.classList.toggle('feed-dm', VIEW==='messages');   // full-height messages layout (no :has needed)
    feed.classList.toggle('feed-ai', VIEW==='ai');         // full-height chat layout (msgs scroll inside)
    feed.classList.toggle('feed-admin', VIEW==='admin');   // full-height admin iframe
    // Admin uses a PERSISTENT iframe (loaded once, kept alive) so revisiting it doesn't reload
    // /admin every time — that reload was the flicker / "not loading". Hide it + restore #feed for
    // every other view; renderAdmin shows it for admin.
    const _ah=document.getElementById('admin-host');
    if(VIEW!=='admin'){ if(_ah) _ah.style.display='none'; feed.style.display=''; }
    if (reset && VIEW!=='admin') feed.innerHTML = '<div class="spinner"></div>';
    if (VIEW==='home' || VIEW==='global') return renderTimeline(VIEW, reset);
    if (VIEW==='notifications') return renderNotifications();
    if (VIEW==='messages') return renderMessages();
    if (VIEW==='drafts'){ Drafts.pull(); return renderDrafts(); }   // re-sync from the relay on each entry
    if (VIEW==='bookmarks') return renderBookmarks();
    if (VIEW==='articles') return renderArticles();
    if (VIEW==='market') return renderMarket();
    if (VIEW==='streams') return renderStreams();
    if (VIEW==='communities') return renderCommunities();
    if (VIEW==='pics') return renderPics();
    if (VIEW==='chat') return renderChatrooms();
    if (VIEW==='torrents') return renderTorrents();
    if (VIEW==='repos') return renderRepos();
    if (VIEW==='4chan') return render4chan();
    if (window.PCGames && window.PCGames[VIEW]) return window.PCGames[VIEW]();   // game modules (chess.js/ttt.js/hangman.js)
    if (VIEW==='blossom') return renderBlossom();
    if (VIEW==='settings') return renderSettings();
    if (VIEW==='ai') return renderAI();
    if (VIEW==='admin') return renderAdmin();
    if (VIEW==='profile') return renderProfile(ME.pubkey);
  }

  // ---------- timeline ----------
  function timelineFilter(){
    // include kind 5 (NIP-09 deletions) so the feed drops posts the author deleted instead of
    // showing stale cached copies.
    // Also surface long-form articles (30023) so the Nostrverse timeline isn't notes-only. Articles
    // are self-contained cards (articleCard) that open in the reader. NIP-22 comments (1111) and
    // channel chat (40/42) are intentionally excluded — they're reply fragments / high-volume and
    // render as orphaned posts in a flat feed (they belong in the thread / a Channels view).
    // + articles (30023), and NEW communities (34550) / channels (40) so people discover them in the
    // feed instead of having to visit the Communities/Chat tabs (both are low-volume creation events,
    // so they surface without flooding). Channel chat MESSAGES (42) stay out — those would flood.
    if (VIEW==='home') return [{ kinds:[1,6,1068,5,30023,34550,40], authors:[...FOLLOWS], limit:80 }];
    return [{ kinds:[1,6,1068,5,30023,34550,40], limit:120 }];
  }
  // NIP-09: a kind-5 removes the AUTHOR'S OWN events it e-tags. Drop them from the cache, the feed,
  // AND notifications (a deleted bot post/reply must stop showing as a notification too).
  function _applyDeletion(ev){
    let removed = false;
    const _rm = id => {
      Store.removeEvent(id); removed = true;
      document.querySelectorAll(`[data-id="${id}"],[data-open="${id}"]`).forEach(n=>{
        const card = n.closest('.note,.notif,.stream-card,.pic-card,.article-card,.mkt-card,.community-card,.channel-card') || n;
        card.remove();
      });
    };
    for(const t of (ev.tags||[])){
      if(t[0]==='e' && t[1]){
        const tgt = Store.get(t[1]);
        if(tgt && tgt.pubkey!==ev.pubkey) continue;   // only the author can delete their own event
        _rm(t[1]);
      } else if(t[0]==='a' && t[1]){                  // addressable (kind:pubkey:dtag) — drafts/articles/etc.
        const parts = String(t[1]).split(':'); if(parts.length<3) continue;
        const [k, pk, dt] = [parts[0], parts[1], parts.slice(2).join(':')];
        if(pk!==ev.pubkey) continue;
        for(const e of Store.all()){
          if(String(e.kind)===k && e.pubkey===pk && (e.tags.find(x=>x[0]==='d')||[])[1]===dt) _rm(e.id);
        }
      }
    }
    if(removed){ try{ invalidateCounts(); }catch(_){}
      if(VIEW==='notifications') renderNotifications();
      else if(VIEW==='articles') renderArticles(); }
  }
  // Always-on deletion feed: catches kind-5s regardless of the current view (the notifications/feed
  // subs are view-scoped and don't carry deletions), so deleted posts/replies/notifications clear.
  function watchDeletions(){
    Relay.subscribe([{ kinds:[5], limit:500 }], { onEvent: ev => { if(Store.saveEvent(ev)) _applyDeletion(ev); } });
  }
  // pagination state for the home/global timelines (infinite scroll-back via `until`)
  let _tl = { oldest:0, loading:false, done:false, pages:0 };
  let _tlMedia = !!ClientSettings.get('tlMedia', false);   // Home/Global "media grid" toggle (image posts only)
  // Blur NIP-36 sensitive/NSFW posts behind a reveal. ON by default; User Settings → Muted toggles it.
  // Stored per-device in ClientSettings (localStorage), so the choice survives reloads/PWA restarts.
  let BLUR_NSFW = ClientSettings.get('blurNsfw', true) !== false;
  // A post counts as sensitive (and is blurred when BLUR_NSFW is on) if it carries a NIP-36
  // content-warning, OR a topic `t` tag in this set, OR an inline #nsfw-style hashtag — so the common
  // "#nsfw" convention auto-blurs even without a formal content-warning tag.
  const _NSFW_TAGS = new Set(['nsfw','porn','nude','nudity','sex','xxx','explicit','gore']);
  const _NSFW_RE = /(^|\s)#(nsfw|porn|nude|nudity|sex|xxx|explicit|gore)\b/i;
  function isSensitive(ev){
    if(!ev || !ev.tags) return false;
    if(ev.tags.some(t=>t[0]==='content-warning')) return true;
    if(ev.tags.some(t=>t[0]==='t' && _NSFW_TAGS.has(String(t[1]||'').toLowerCase()))) return true;
    return _NSFW_RE.test(ev.content||'');
  }
  let _liveSince = 0;   // sub start time — only events at/after this are "live" (prependable as new)
  function renderTimeline(view, reset){
    const fn = view==='home' ? (ev=>FOLLOWS.has(ev.pubkey)) : null;
    if(reset){ _tl = { oldest:0, loading:false, done:false, pages:0, eosed:false }; _resetLive(); _liveSince = Math.floor(Date.now()/1000); }
    _drawTimeline(false);
    if (subs[view]) Relay.close(subs[view]);
    subs[view] = Relay.subscribe(timelineFilter(), {
      onEvent: ev => { if (Store.saveEvent(ev)){ invalidateCounts(); needProfile(ev.pubkey);
        // Only prepend as "live" if it's genuinely new — NOT a backfilled/synced event with an old
        // created_at (those would otherwise jump to the top as if new). A small grace covers skew.
        if (VIEW===view && (ev.kind===1||ev.kind===6||ev.kind===1068||ev.kind===30023||ev.kind===34550||ev.kind===40) && _tl.eosed && ev.created_at >= _liveSince-120) _bufferLive(ev, fn); } },
      // Draw ONLY on the first EOSE. The relay re-EOSEs on reconnect/re-sync; redrawing then would
      // wipe + rebuild the feed under the user (the "disappears with the timeline update" bug).
      onEose: ()=>{ if(VIEW===view && !_tl.eosed){ _tl.eosed=true; _drawTimeline(false); } }
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
    if(_tlMedia) return;   // media grid doesn't live-prepend (would break the grid) — new images show on redraw/re-entry
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
    for(const ev of evs){ if(ev.kind===1&&isReply(ev))continue; if(isMutedView(ev))continue; if(_liveFn&&!_liveFn(ev))continue;
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
    decorateProfiles(); hydrateLinkCards(feed); hydratePolls(feed);
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
    const notes = Store.feed(e=>(!fn||fn(e))&&!isMutedView(e)).filter(e=>!isReply(e)).slice(0,200);
    // seed the scroll-back cursor from the initial draw only — once the user has paged older, a late
    // EOSE redraw must NOT move the cursor forward (it would re-query an already-loaded range)
    if(notes.length && _tl.pages===0) _tl.oldest = notes[notes.length-1].created_at;
    if(_tlMedia){
      // Media grid: the SAME feed (your follows / the nostrverse), image posts only, as a picture
      // grid — reuses Pics' _firstImage + .pics-grid/.pic-card styling. Scroll-back grows it (events
      // accumulate in Store); like the old Pics view it doesn't live-prepend (see flushLive).
      const pics=[]; const seen=new Set();
      for(const e of notes){ const img=_firstImage(e); if(!img||seen.has(e.id)) continue; seen.add(e.id); pics.push({e,img}); }
      feed.innerHTML = pics.length
        ? `<div class="pics-grid">${pics.map(x=>{ const cw=BLUR_NSFW && isSensitive(x.e);
            return `<div class="pic-card${cw?' cw':''}" data-id="${x.e.id}"><img src="${enc(x.img)}" loading="lazy" onerror="this.closest('.pic-card')&&this.closest('.pic-card').remove()">${cw?'<span class="pic-cw">🔞</span>':''}</div>`; }).join('')}</div>`
        : `<div class="empty">No media in this feed yet. ${VIEW==='home'?'Follow people or check Global.':''}</div>`;
      $$('.pic-card',feed).forEach(c=> c.onclick=()=> openThread(c.dataset.id));
      if(preserveScroll) feed.scrollTop=top;
      return;
    }
    feed.innerHTML = notes.length ? notes.map(noteHtml).join('') : `<div class="empty">No posts yet. ${VIEW==='home'?'Follow people or check Global.':''}</div>`;
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
  // ---------- mobile touch gestures (pull-to-refresh + swipe between primary tabs) ----------
  // Both ride ONE set of touch listeners on #feed; the first significant move locks the axis so a
  // vertical scroll never triggers a swipe and vice-versa. Deliberately conservative: pull only
  // engages at the very top of a list-style view, swipe only over plain content (not media/code/
  // games/inputs) and only between the bottom-nav's four primary feeds.
  let _gesturesBound = false;
  function bindMobileGestures(){
    const feed = $('#feed'); if(!feed || _gesturesBound) return; _gesturesBound = true;
    const SWIPE_VIEWS = ['home','global','notifications','messages'];   // mirrors the mobile bottom nav order
    const REFRESHABLE = new Set(['home','global','notifications','messages','bookmarks','drafts','articles','market','streams','communities']);
    const PTR_TRIGGER = 70, PTR_MAX = 110, SWIPE_MIN = 60;
    let sx=0, sy=0, axis='', pulling=false, swiping=false, active=false, startTop=0, ind=null;
    // Don't hijack horizontal drags that belong to scrollable/interactive children.
    // `.dm-thread` (an OPEN conversation), not `.dm-wrap` (the whole Messages pane) — so the
    // conversation LIST stays swipeable (swipe back to Notifications) while an open chat isn't yanked out from under you.
    const noSwipe = el => !!(el && el.closest && el.closest('.media,.gallery,img,video,pre,code,canvas,table,input,textarea,select,.poll,.carousel,.scrollx,.dm-thread'));
    const indicator = ()=>{ if(!ind || !ind.isConnected){ ind=document.createElement('div'); ind.className='ptr-ind'; ind.textContent='↻'; document.body.appendChild(ind); } return ind; };
    const resetInd = ()=>{ if(ind){ ind.style.opacity=''; ind.style.transform=''; ind.classList.remove('ready','spin'); } };
    feed.addEventListener('touchstart', e=>{
      if(e.touches.length!==1){ active=false; return; }
      const t=e.touches[0]; sx=t.clientX; sy=t.clientY; axis=''; pulling=false; swiping=false; active=true; startTop=feed.scrollTop;
    }, {passive:true});
    feed.addEventListener('touchmove', e=>{
      if(!active || e.touches.length!==1) return;
      const t=e.touches[0], dx=t.clientX-sx, dy=t.clientY-sy;
      if(!axis){
        if(Math.abs(dx)<8 && Math.abs(dy)<8) return;
        if(Math.abs(dx) > Math.abs(dy)*1.3){                     // horizontal → swipe-nav candidate
          if(window.innerWidth<=820 && !noSwipe(e.target)){ axis='x'; swiping=true; e.preventDefault(); } else { active=false; }
        } else if(dy>0 && startTop<=0 && REFRESHABLE.has(VIEW)){  // pull-down at the top → refresh
          axis='y'; pulling=true;
        } else { active=false; }                                 // ordinary vertical scroll — hands off
        return;
      }
      if(axis==='y' && pulling){
        const pull=Math.min(PTR_MAX, dy*0.5);
        if(pull>0){ e.preventDefault(); const i=indicator();
          i.style.opacity=Math.min(1, pull/PTR_TRIGGER); i.style.transform=`translateX(-50%) translateY(${pull}px) rotate(${pull*3}deg)`;
          i.classList.toggle('ready', pull>=PTR_TRIGGER); }
      } else if(axis==='x' && swiping){
        e.preventDefault();   // keep claiming the horizontal gesture so the OS/browser edge-back can't steal the back-direction swipe (→ touchcancel, handler never fires)
      }
    }, {passive:false});
    feed.addEventListener('touchcancel', ()=>{ active=false; resetInd(); }, {passive:true});
    feed.addEventListener('touchend', e=>{
      if(!active){ return; } active=false;
      if(axis==='y' && pulling){
        const ready = ind && ind.classList.contains('ready');
        if(ready){ ind.classList.add('spin'); ind.style.opacity='1'; ind.style.transform='translateX(-50%) translateY(8px)';
          try{ renderView(true); }catch(_){} setTimeout(resetInd, 600); }
        else resetInd();
      } else if(axis==='x' && swiping){
        const dx=(e.changedTouches[0].clientX)-sx;
        if(Math.abs(dx)>=SWIPE_MIN){ const cur=SWIPE_VIEWS.indexOf(VIEW);
          if(cur>=0){ const next=cur+(dx<0?1:-1); if(next>=0 && next<SWIPE_VIEWS.length) switchView(SWIPE_VIEWS[next]); } }
      }
    }, {passive:true});
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
    const filt = view==='home' ? [{ kinds:[1,6,1068,30023,34550,40], authors:[...FOLLOWS], until:until-1, limit:50 }]
                               : [{ kinds:[1,6,1068,30023,34550,40], until:until-1, limit:60 }];
    let evs=[]; try{ evs=await Relay.query(filt); }catch(_){}
    clearSentinel(feed);
    if(VIEW!==view){ _tl.loading=false; return; }   // user navigated away mid-fetch
    evs.sort((a,b)=>b.created_at-a.created_at);
    let minTs=until; const frag=document.createDocumentFragment();
    for(const ev of evs){
      Store.saveEvent(ev); needProfile(ev.pubkey);
      if(ev.created_at<minTs) minTs=ev.created_at;
      if(ev.kind===1 && isReply(ev)) continue;
      if(isMutedView(ev)) continue;
      if(view==='home' && !FOLLOWS.has(ev.pubkey)) continue;
      // a repost (kind 6) renders with the ORIGINAL's data-id, so dedupe against that, not the
      // repost's own id — otherwise a repost of an already-shown note appends a duplicate card.
      if(_tlMedia) continue;   // media grid redraws from Store below — skip building list nodes
      const dispId = ev.kind===6 ? ((ev.tags.find(t=>t[0]==='e')||[])[1]||ev.id) : ev.id;
      if(feed.querySelector('.note[data-id="'+dispId+'"]')) continue;   // already on screen
      const div=document.createElement('div'); div.innerHTML=noteHtml(ev); const node=div.firstElementChild; if(node) frag.appendChild(node);
    }
    invalidateCounts();
    if(_tlMedia){ if(evs.length) _drawTimeline(true); }   // grow the grid from the now-larger Store set
    else if(frag.childElementCount){ feed.appendChild(frag); decorateProfiles(); hydrateLinkCards(feed); hydrateCounts(); hydratePolls(feed); }
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
    if (isMutedView(ev)) return;
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
  // Substring match for addressable Discover kinds (articles/streams/communities) — the relay's NIP-50
  // FTS only covers kind-1, so search those by their title/name/summary/about/content client-side.
  function _matchAddr(e, ql){
    const g=k=>(e.tags.find(t=>t[0]===k)||[])[1]||'';
    return (g('title')+' '+g('name')+' '+g('d')+' '+g('summary')+' '+g('description')+' '+(e.content||'')).toLowerCase().includes(ql);
  }
  async function renderArticles(){
    const feed=$('#feed');
    feed.innerHTML=`<div class="art-top"><button class="btn btn-neon small" id="art-new">✎ Write article</button></div><div id="art-drafts"></div><div id="art-list"><div class="spinner"></div></div>`;
    $('#art-new').onclick=()=>renderArticleEditor();
    let evs=[], drafts=[];
    try{ evs=await Relay.query([{ kinds:[30023], limit:80 }]); }catch(_){}
    try{ drafts=await Relay.query([{ kinds:[30024], authors:[ME.pubkey], limit:50 }]); }catch(_){}   // my NIP-23 drafts
    evs.forEach(e=>{ Store.saveEvent(e); needProfile(e.pubkey); });
    drafts.forEach(e=>Store.saveEvent(e));
    if(VIEW!=='articles') return;
    // Drafts (your unpublished kind-30024) — resume or delete.
    const db=$('#art-drafts');
    if(db){
      const dd=_dedupAddr(drafts).sort((a,b)=>(b.created_at||0)-(a.created_at||0));
      db.innerHTML = dd.length ? '<div class="search-section-title">📝 Drafts</div>'+dd.map(d=>{
        const t=(d.tags.find(x=>x[0]==='title')||[])[1]||'(untitled)';
        const slug=(d.tags.find(x=>x[0]==='d')||[])[1]||'';
        return `<div class="draft-art" data-id="${d.id}" data-slug="${enc(slug)}"><span class="da-title">📝 ${enc(t)}</span><span class="spacer"></span><button class="btn btn-ghost small da-edit">Resume</button><button class="btn btn-ghost small da-del" style="color:var(--danger)">✕</button></div>`;
      }).join('') : '';
      $$('.draft-art',db).forEach(c=>{
        c.querySelector('.da-edit').onclick=()=>{ const e=Store.get(c.dataset.id); if(e) renderArticleEditor(e); };
        c.querySelector('.da-del').onclick=async()=>{ if(!confirm('Delete this draft?'))return; await _deleteArticleDraft(c.dataset.slug); c.remove(); toast('draft deleted'); };
      });
    }
    const arts=_dedupAddr(evs).sort((a,b)=>artTime(b)-artTime(a));
    const list=$('#art-list'); if(!list) return;
    list.innerHTML = arts.length ? arts.map(articleCard).join('') : '<div class="empty">No articles yet. Tap “Write article” to publish the first one.</div>';
    decorateProfiles();
    $$('.article-card',list).forEach(c=> c.onclick=ev=>{ if(ev.target.closest('[data-prof]')){ renderProfileView(c.dataset.pk); return; } const a=Store.get(c.dataset.id); if(a) openArticle(a); });
    if(arts.length) _fillArticleCommentCounts(arts, list);
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
        <div class="art-by"><img class="art-av" src="${enc(p.picture||LOGO)}" onerror="this.src='${LOGO}'"><span class="name" data-prof="${e.pubkey}">${enc(p.name||p.display_name||'anon')}</span><span class="muted small">· ${timeAgo(artTime(e))}</span><span class="art-cc muted small" data-addr="${enc(articleAddr(e))}"></span></div>
      </div></article>`;
  }
  // Fill the 💬 comment count on the listed article cards with ONE query (not one per card), counting
  // events by the article ROOT scope (#A / legacy #a) they carry. Best-effort, purely additive.
  async function _fillArticleCommentCounts(arts, list){
    const addrs=[...new Set(arts.map(articleAddr))]; if(!addrs.length) return;
    let cs=[]; try{ cs=await Relay.query([{ kinds:[1,1111], '#A':addrs, limit:500 }, { kinds:[1,1111], '#a':addrs, limit:500 }]); }catch(_){}
    const counts=new Map(), seen=new Set();
    for(const c of cs){ if(seen.has(c.id)) continue; seen.add(c.id);
      const a=(c.tags||[]).find(t=>(t[0]==='A'||t[0]==='a') && addrs.includes(t[1]));
      if(a) counts.set(a[1], (counts.get(a[1])||0)+1); }
    if(VIEW!=='articles' || !list) return;
    list.querySelectorAll('.art-cc').forEach(el=>{ const n=counts.get(el.dataset.addr)||0; if(n) el.textContent=` · 💬 ${n}`; });
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
        ${mine?`<button class="act" id="av-del" title="delete" style="color:var(--danger)">🗑</button>`:''}
        <button class="act" id="av-copy" title="copy link">🔗</button>
      </div>
      <div class="markdown av-body">${mdToHtml(e.content)}</div>
      <div class="av-comments">
        <div class="av-comments-hd"><span class="search-section-title">Comments</span>
          <button class="btn btn-neon small" id="av-comment">💬 Write a comment</button></div>
        <div id="av-comment-list"><div class="spinner"></div></div>
      </div>
    </div>`;
    $('#art-back').onclick=()=>switchView('articles');
    $('#av-bm').onclick=ev=>toggleBookmark(e.id, ev.currentTarget);
    $('#av-zap').onclick=()=>doZap(e.id, e.pubkey);
    { const ed=$('#av-edit'); if(ed) ed.onclick=()=>renderArticleEditor(e); }
    { const dl=$('#av-del'); if(dl) dl.onclick=()=>deleteArticle(e); }
    $('#av-copy').onclick=()=>{ try{ const naddr=NT().nip19.naddrEncode({ identifier:(e.tags.find(t=>t[0]==='d')||[])[1]||'', pubkey:e.pubkey, kind:30023 }); navigator.clipboard.writeText(_webLink(naddr)); toast('article link copied'); }catch(_){ navigator.clipboard.writeText(e.id); toast('id copied'); } };
    { const cb=$('#av-comment'); if(cb) cb.onclick=()=>{ if(GUEST){ _guestPrompt(); return; } compose({articleComment:e}); }; }
    feed.querySelectorAll('[data-prof]').forEach(el=> el.onclick=()=>renderProfileView(el.dataset.prof));
    feed.querySelectorAll('.markdown img').forEach(im=> im.onclick=()=>openLightbox(im.currentSrc||im.src));
    decorateProfiles();
    loadArticleComments(e);
  }
  // NIP-22 comments (kind 1111) on a NIP-23 article, scoped to the article's `a` coordinate. Older
  // clients may comment with a kind-1 carrying the same `#a` — query both.
  function articleAddr(e){ const d=(e.tags.find(t=>t[0]==='d')||[])[1]||''; return '30023:'+e.pubkey+':'+d; }
  // One threaded comment (+ its nested replies, recursively). Custom card (not noteCard) so the Reply
  // button posts a NIP-22 reply THREADED under this comment, not a plain kind-1.
  function _acCard(c, depth){
    const p=profOf(c.pubkey); needProfile(c.pubkey);
    const name=p.name||p.display_name||(NT().nip19.npubEncode(c.pubkey).slice(0,12)+'…');
    const handle=niceNip05(p.nip05)||('@'+NT().nip19.npubEncode(c.pubkey).slice(4,12));
    const mp=mediaParts(c.content);
    const kids=(c._kids||[]).map(k=>_acCard(k, depth+1)).join('');
    return `<div class="ac-item"${depth?` style="margin-left:${Math.min(depth,5)*14}px"`:''}>
      <div class="ac-hd"><img class="ac-av" src="${enc(p.picture||LOGO)}" onerror="this.src='${LOGO}'"><span class="name" data-prof="${c.pubkey}">${enc(name)}</span><span class="vchk" data-pk="${c.pubkey}"></span><span class="handle">${enc(handle)}</span><span class="time">${timeAgo(c.created_at)}</span></div>
      <div class="ac-body">${applyEmojis(linkify(mp.text), c)}</div>${mp.gallery}
      <div class="ac-act"><button class="btn btn-ghost small ac-reply" data-id="${c.id}">↩ Reply</button></div>
      ${kids}</div>`;
  }
  async function loadArticleComments(e){
    const addr=articleAddr(e);
    // #A = the NIP-22 ROOT scope → catches top-level AND nested replies (nested carry `A`=article but
    // `e`=parent, so `#a` alone would miss them). `#a` too for legacy/top-level. Pool dedups by id.
    let cs=[]; try{ cs=await Relay.query([{ kinds:[1,1111], '#A':[addr], limit:200 }, { kinds:[1,1111], '#a':[addr], limit:200 }]); }catch(_){}
    cs.forEach(x=>{ Store.saveEvent(x); needProfile(x.pubkey); });
    if(VIEW!=='article') return;                         // navigated away while loading
    const box=$('#av-comment-list'); if(!box) return;
    cs=cs.filter(x=>!isMutedView(x));
    // Build the reply tree: a comment nests under another comment IN this set that it e-tags; otherwise
    // it's top-level (its parent is the article). Guard against self/cyclic parents.
    const byId=new Map(cs.map(c=>[c.id,c])); cs.forEach(c=>c._kids=[]);
    const roots=[];
    for(const c of cs){
      const pid=(c.tags||[]).filter(t=>t[0]==='e'&&t[1]&&t[1]!==c.id).map(t=>t[1]).find(id=>byId.has(id));
      if(pid) byId.get(pid)._kids.push(c); else roots.push(c);
    }
    const sortRec=a=>{ a.sort((x,y)=>x.created_at-y.created_at); a.forEach(c=>sortRec(c._kids)); };   // oldest-first
    sortRec(roots);
    box.innerHTML = roots.length ? roots.map(c=>_acCard(c,0)).join('') : '<div class="empty">No comments yet — be the first to reply.</div>';
    box.querySelectorAll('.ac-reply').forEach(b=> b.onclick=()=>{ if(GUEST){ _guestPrompt(); return; } const c=byId.get(b.dataset.id); if(c) compose({articleComment:e, articleParent:c}); });
    box.querySelectorAll('[data-prof]').forEach(el=> el.onclick=()=>renderProfileView(el.dataset.prof));
    box.querySelectorAll('.ac-item img:not(.ac-av)').forEach(im=> im.onclick=()=>openLightbox(im.currentSrc||im.src));
    decorateProfiles();
  }
  function _insertAt(ta, text){ const s=ta.selectionStart||0, en=ta.selectionEnd||0; ta.value=ta.value.slice(0,s)+text+ta.value.slice(en); const c=s+text.length; ta.selectionStart=ta.selectionEnd=c; ta.focus(); }
  // Article drafts are NIP-23 **kind-30024** (draft long-form) events — same shape as a published
  // 30023 but a draft, so they live on your relay, sync across devices/clients, and you own them.
  // "Save draft" publishes/updates the 30024; publishing the article (30023) deletes the draft.
  let _aeDraftT=null;
  function _slugFor(title){ return ((title||'').toLowerCase().replace(/[^a-z0-9]+/g,'-').replace(/^-+|-+$/g,'').slice(0,60) || 'draft') + '-' + Math.random().toString(36).slice(2,7); }
  async function _saveArticleDraft(slug, a){
    const tags=[['d',slug],['title',a.title||'']];
    if(a.summary) tags.push(['summary',a.summary]);
    if(a.image) tags.push(['image',a.image]);
    return await publish(30024, a.body||'', tags);   // NIP-23 draft long-form
  }
  async function _deleteArticleDraft(slug){ if(!slug) return; try{ await publish(5, 'draft published', [['a', `30024:${ME.pubkey}:${slug}`]]); }catch(_){} }
  // On publish, the draft's slug may differ from the published slug (drafted across sessions / title
  // changed after the first autosave). Delete the exact slug AND any draft with the same title, so a
  // published article never leaves an orphaned draft behind.
  async function _deletePublishedDrafts(slug, title){
    const coords=new Set(); if(slug) coords.add(`30024:${ME.pubkey}:${slug}`);
    try{
      const drafts=await Relay.query([{ kinds:[30024], authors:[ME.pubkey], limit:50 }]);
      for(const d of (drafts||[])){
        const dt=(d.tags.find(t=>t[0]==='d')||[])[1]; if(!dt) continue;
        const ti=((d.tags.find(t=>t[0]==='title')||[])[1]||'').trim();
        if(dt===slug || (title && ti===title.trim())) coords.add(`30024:${ME.pubkey}:${dt}`);
      }
    }catch(_){}
    if(coords.size){ try{ await publish(5, 'draft published', [...coords].map(c=>['a',c])); }catch(_){} }
  }
  function renderArticleEditor(existing){
    VIEW='article'; $$('.nav-item[data-view]').forEach(b=>b.classList.remove('active')); $('#view-title').textContent=existing?'Edit article':'Write article';
    const feed=$('#feed'); const g=(k)=>existing?((existing.tags.find(t=>t[0]===k)||[])[1]||''):'';
    feed.innerHTML=`<div class="article-editor">
      <button class="btn btn-ghost small" id="ae-back">← Cancel</button>
      <label class="fld">Title<input class="input" id="ae-title" placeholder="Article title" value="${enc(g('title'))}"></label>
      <label class="fld">Summary<input class="input" id="ae-sum" placeholder="One-line summary (optional)" value="${enc(g('summary'))}"></label>
      <label class="fld">Header image<input class="input" id="ae-img" placeholder="https://… (optional)" value="${enc(g('image'))}"></label>
      <div class="row" style="margin:-6px 0 2px"><button type="button" class="btn btn-ghost small" id="ae-img-up">🖼 Upload header image</button></div>
      <input type="file" id="ae-img-file" accept="image/*" hidden>
      <div class="cmp-tabs"><button class="cmp-tab active" data-t="write">Write</button><button class="cmp-tab" data-t="preview">👁 Preview</button></div>
      <div class="row cmp-tools"><button class="btn btn-ghost small" id="ae-insert">📎 Insert image</button><input type="file" id="ae-body-file" accept="image/*" multiple hidden><span class="spacer"></span><span class="muted small">Markdown</span></div>
      <textarea id="ae-body" class="article-body" placeholder="Write your article in markdown…">${enc(existing?existing.content:'')}</textarea>
      <div id="ae-preview" class="markdown article-preview hidden"></div>
      <div class="row"><span class="muted small" id="ae-status"></span><span class="spacer"></span><button type="button" class="btn btn-ghost small" id="ae-draft">💾 Save draft</button><button class="btn btn-neon" id="ae-pub">Publish ▶</button></div>
    </div>`;
    $('#ae-back').onclick=()=>switchView('articles');
    const body=$('#ae-body');
    // Slug (d-tag): reused when editing a published article OR resuming a draft, so saving updates
    // the SAME 30024 (not a duplicate). Generated on first save for a brand-new article.
    let _aeSlug = g('d') || null;
    const _grabArticle=()=>({title:$('#ae-title').value, summary:$('#ae-sum').value, image:$('#ae-img').value, body:body.value});
    async function _doSaveDraft(announce){
      const a=_grabArticle();
      if(!(a.title||a.body||a.image||a.summary)){ if(announce) toast('nothing to save yet'); return; }
      if(!_aeSlug) _aeSlug=_slugFor(a.title);
      if($('#ae-status')) $('#ae-status').textContent='saving draft…';
      try{ await _saveArticleDraft(_aeSlug, a); if($('#ae-status')) $('#ae-status').textContent='✓ draft saved'; if(announce) toast('draft saved (in Articles)'); }
      catch(e){ if($('#ae-status')) $('#ae-status').textContent='draft save failed'; }
    }
    { const d=$('#ae-draft'); if(d) d.onclick=()=>_doSaveDraft(true); }
    // Gentle auto-save to a 30024 so work survives a refresh (cleared when you publish).
    body.addEventListener('input', ()=>{ clearTimeout(_aeDraftT); _aeDraftT=setTimeout(()=>_doSaveDraft(false), 4000); });
    $$('.cmp-tab',feed).forEach(b=> b.onclick=()=>{ $$('.cmp-tab',feed).forEach(x=>x.classList.toggle('active',x===b)); const pv=b.dataset.t==='preview'; body.classList.toggle('hidden',pv); const prev=$('#ae-preview'); prev.classList.toggle('hidden',!pv); if(pv) prev.innerHTML=mdToHtml(body.value)||'<div class="muted small">Nothing to preview.</div>'; });
    $('#ae-img-up').onclick=()=>$('#ae-img-file').click();
    $('#ae-img-file').onchange=async ev=>{ const f=ev.target.files[0]; if(!f)return; $('#ae-status').textContent='uploading image…'; try{ $('#ae-img').value=await uploadBlob(f); $('#ae-status').textContent='image uploaded'; }catch(err){ $('#ae-status').textContent='upload failed: '+err.message; } };
    $('#ae-insert').onclick=()=>$('#ae-body-file').click();
    $('#ae-body-file').onchange=async ev=>{ const files=[...ev.target.files]; for(let i=0;i<files.length;i++){ $('#ae-status').textContent=`uploading ${i+1}/${files.length}…`; try{ const url=await uploadBlob(files[i]); _insertAt(body, `\n![](${url})\n`); }catch(err){ $('#ae-status').textContent='upload failed: '+err.message; return; } } $('#ae-status').textContent=''; ev.target.value=''; };
    $('#ae-pub').onclick=()=>publishArticle({ title:$('#ae-title').value.trim(), summary:$('#ae-sum').value.trim(), image:$('#ae-img').value.trim(), body:body.value, d:_aeSlug });
  }
  async function deleteArticle(e){
    // NIP-09: a kind-5 deletion referencing the article by event id AND addressable coordinate.
    // It broadcasts to all upstream relays (deletions are broadcastable), so they remove it too.
    if(!confirm('Delete this article? This asks every relay (NIP-09) to remove it.')) return;
    const slug=(e.tags.find(t=>t[0]==='d')||[])[1]||'';
    const tags=[['e',e.id]]; if(slug) tags.push(['a',`30023:${e.pubkey}:${slug}`]);
    try{ await publish(5, 'deleted', tags); toast('deletion requested'); switchView('articles'); }
    catch(err){ toast('delete failed: '+(err.message||'')); }
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
    try{ const r=await publish(30023, body, tags); if(r && r.ok===false){ toast('relay: '+(r.msg||'rejected')); if($('#ae-status'))$('#ae-status').textContent=''; } else { _deletePublishedDrafts(slug, title); toast('article published'); switchView('articles'); } }
    catch(e){ toast('publish failed: '+e.message); }
  }

  // ---------- Market / classified listings (NIP-99, kind 30402; drafts 30403) ----------
  // Listings are parameterized-replaceable like articles: newest event per (pubkey, d-tag) wins,
  // so an edit/sold update replaces the old card instead of duplicating it.
  const MKT_CATS=['Electronics','Computers','Phones','Home','Furniture','Clothing','Vehicles','Bikes',
                  'Collectibles','Art','Books','Games','Music','Tools','Garden','Sports','Services','Digital','Free','Other'];
  const MKT_CURRENCIES=['USD','EUR','GBP','CAD','AUD','JPY','SATS','BTC'];
  function mktPriceTag(e){ return e.tags.find(t=>t[0]==='price')||null; }
  function mktStatus(e){ return ((e.tags.find(t=>t[0]==='status')||[])[1]||'active').toLowerCase(); }
  function mktImages(e){ return e.tags.filter(t=>t[0]==='image'&&t[1]).map(t=>t[1]); }
  function mktCats(e){ return e.tags.filter(t=>t[0]==='t'&&t[1]).map(t=>t[1]); }
  function fmtPrice(e){
    const p=mktPriceTag(e); if(!p) return '';
    const amt=p[1]||'', cur=(p[2]||'').toUpperCase(), freq=p[3]||'';
    if(!amt) return '';
    const n=Number(amt); const a=Number.isFinite(n)? n.toLocaleString() : amt;
    let s = (cur==='SATS') ? `${a} sats` : (cur==='BTC') ? `₿${a}` :
            (cur==='USD') ? `$${a}` : (cur==='EUR') ? `€${a}` : (cur==='GBP') ? `£${a}` : `${a} ${cur}`;
    if(freq) s+=` / ${freq}`;
    return s;
  }
  async function renderMarket(){
    const feed=$('#feed');
    feed.innerHTML=`<div class="art-top"><button class="btn btn-neon small" id="mkt-new">🏷 Sell something</button></div>
      <div class="mkt-filter"><input class="input" id="mkt-q" placeholder="🔎 Search listings…"><div class="mkt-cats" id="mkt-cats"></div></div>
      <div id="mkt-drafts"></div><div id="mkt-grid"><div class="spinner"></div></div>`;
    $('#mkt-new').onclick=()=>renderListingEditor();
    let evs=[], drafts=[];
    try{ evs=await Relay.query([{ kinds:[30402], limit:120 }]); }catch(_){}
    try{ drafts=await Relay.query([{ kinds:[30403], authors:[ME.pubkey], limit:50 }]); }catch(_){}
    evs.forEach(e=>{ Store.saveEvent(e); needProfile(e.pubkey); });
    drafts.forEach(e=>Store.saveEvent(e));
    if(VIEW!=='market') return;
    // my drafts (kind 30403) — resume or delete
    const db=$('#mkt-drafts');
    if(db){
      const dd=_dedupAddr(drafts).sort((a,b)=>(b.created_at||0)-(a.created_at||0));
      db.innerHTML = dd.length ? '<div class="search-section-title">📝 Draft listings</div>'+dd.map(d=>{
        const t=(d.tags.find(x=>x[0]==='title')||[])[1]||'(untitled)';
        return `<div class="draft-art" data-id="${d.id}" data-slug="${enc((d.tags.find(x=>x[0]==='d')||[])[1]||'')}"><span class="da-title">📝 ${enc(t)}</span><span class="spacer"></span><button class="btn btn-ghost small da-edit">Resume</button><button class="btn btn-ghost small da-del" style="color:var(--danger)">✕</button></div>`;
      }).join('') : '';
      $$('.draft-art',db).forEach(c=>{
        c.querySelector('.da-edit').onclick=()=>{ const e=Store.get(c.dataset.id); if(e) renderListingEditor(e); };
        c.querySelector('.da-del').onclick=async()=>{ if(!confirm('Delete this draft listing?'))return; try{ await publish(5,'draft deleted',[['a',`30403:${ME.pubkey}:${c.dataset.slug}`]]); }catch(_){} c.remove(); toast('draft deleted'); };
      });
    }
    const all=_dedupAddr(evs);
    // category chips (active first)
    const present=new Set(); all.forEach(e=>mktCats(e).forEach(c=>present.add(c)));
    const chips=$('#mkt-cats');
    let _activeCat='', _q='';
    const drawChips=()=>{ if(!chips) return; chips.innerHTML=[''].concat(MKT_CATS.filter(c=>present.has(c))).map(c=>`<button class="mkt-chip ${c===_activeCat?'on':''}" data-c="${enc(c)}">${c?enc(c):'All'}</button>`).join(''); $$('.mkt-chip',chips).forEach(b=> b.onclick=()=>{ _activeCat=b.dataset.c; drawChips(); paint(); }); };
    const grid=$('#mkt-grid');
    const paint=()=>{
      if(!grid) return;
      let list=all.slice();
      if(_activeCat) list=list.filter(e=>mktCats(e).includes(_activeCat));
      if(_q){ const ql=_q.toLowerCase(); list=list.filter(e=>_matchAddr(e,ql) || mktCats(e).some(c=>c.toLowerCase().includes(ql)) || ((e.tags.find(t=>t[0]==='location')||[])[1]||'').toLowerCase().includes(ql)); }
      // active listings first, then sold; newest within each
      list.sort((a,b)=> (mktStatus(a)==='sold')-(mktStatus(b)==='sold') || artTime(b)-artTime(a));
      grid.innerHTML = list.length ? `<div class="mkt-grid">${list.map(marketCard).join('')}</div>` : '<div class="empty">No listings yet. Tap “Sell something” to post the first one.</div>';
      decorateProfiles();
      $$('.mkt-card',grid).forEach(c=> c.onclick=ev=>{ if(ev.target.closest('[data-prof]')){ renderProfileView(c.dataset.pk); return; } const a=Store.get(c.dataset.id); if(a) openListing(a); });
    };
    drawChips(); paint();
    { const q=$('#mkt-q'); if(q) q.addEventListener('input', ()=>{ _q=q.value.trim(); paint(); }); }
  }
  function marketCard(e){
    const p=profOf(e.pubkey); needProfile(e.pubkey);
    const title=(e.tags.find(t=>t[0]==='title')||[])[1]||'(untitled)';
    const img=mktImages(e)[0]||'';
    const price=fmtPrice(e);
    const loc=(e.tags.find(t=>t[0]==='location')||[])[1]||'';
    const sold=mktStatus(e)==='sold';
    return `<article class="mkt-card ${sold?'sold':''}" data-id="${e.id}" data-pk="${e.pubkey}">
      <div class="mkt-thumb">${img?`<img src="${enc(img)}" loading="lazy" onerror="this.parentNode.classList.add('noimg');this.remove()">`:'<span class="mkt-noimg">🛍️</span>'}${sold?'<span class="mkt-sold-badge">SOLD</span>':''}</div>
      <div class="mkt-info">
        ${price?`<div class="mkt-price">${enc(price)}</div>`:''}
        <h3 class="mkt-title">${enc(title)}</h3>
        ${loc?`<div class="mkt-loc">📍 ${enc(loc)}</div>`:''}
        <div class="art-by"><img class="art-av" src="${enc(p.picture||LOGO)}" onerror="this.src='${LOGO}'"><span class="name" data-prof="${e.pubkey}">${enc(p.name||p.display_name||'anon')}</span><span class="muted small">· ${timeAgo(artTime(e))}</span></div>
      </div></article>`;
  }
  function openListing(e){
    VIEW='listing'; $$('.nav-item[data-view]').forEach(b=>b.classList.remove('active')); $('#view-title').textContent='Listing';
    const feed=$('#feed'); const p=profOf(e.pubkey); needProfile(e.pubkey);
    const title=(e.tags.find(t=>t[0]==='title')||[])[1]||'(untitled)';
    const imgs=mktImages(e); const price=fmtPrice(e);
    const loc=(e.tags.find(t=>t[0]==='location')||[])[1]||'';
    const cats=mktCats(e); const sold=mktStatus(e)==='sold';
    const mine=e.pubkey===ME.pubkey;
    feed.innerHTML=`<div class="listing-view">
      <button class="btn btn-ghost small" id="li-back">← Market</button>
      ${imgs.length?`<div class="li-gallery"><img class="li-main" id="li-main" src="${enc(imgs[0])}" onerror="this.style.display='none'">
        ${imgs.length>1?`<div class="li-thumbs">${imgs.map((u,i)=>`<img class="li-th ${i===0?'on':''}" data-u="${enc(u)}" src="${enc(u)}" onerror="this.remove()">`).join('')}</div>`:''}</div>`:''}
      <div class="li-head">
        ${price?`<div class="li-price">${enc(price)}${sold?' <span class="mkt-sold-badge">SOLD</span>':''}</div>`:(sold?'<div class="li-price"><span class="mkt-sold-badge">SOLD</span></div>':'')}
        <h1 class="li-title">${enc(title)}</h1>
        ${loc?`<div class="mkt-loc">📍 ${enc(loc)}</div>`:''}
        ${cats.length?`<div class="li-cats">${cats.map(c=>`<span class="mkt-chip on">${enc(c)}</span>`).join('')}</div>`:''}
      </div>
      <div class="li-by" data-prof="${e.pubkey}"><img class="art-av" src="${enc(p.picture||LOGO)}" onerror="this.src='${LOGO}'"><span class="name">${enc(p.name||p.display_name||'anon')}</span><span class="muted small">· ${timeAgo(artTime(e))}</span></div>
      <div class="li-actions">
        ${!mine?`<button class="btn btn-neon" id="li-msg">✉️ Contact seller</button>`:''}
        ${!mine?`<button class="btn btn-ghost" id="li-zap">⚡ Pay / Zap</button>`:''}
        ${mine?`<button class="btn btn-ghost" id="li-sold">${sold?'↩ Mark available':'✅ Mark sold'}</button>`:''}
        ${mine?`<button class="btn btn-ghost" id="li-edit">✏ Edit</button>`:''}
        ${mine?`<button class="btn btn-ghost" id="li-del" style="color:var(--danger)">🗑 Delete</button>`:''}
        <button class="btn btn-ghost" id="li-copy">🔗 Share</button>
      </div>
      <div class="markdown li-body">${mdToHtml(e.content)}</div>
    </div>`;
    $('#li-back').onclick=()=>switchView('market');
    $$('.li-th',feed).forEach(th=> th.onclick=()=>{ const m=$('#li-main'); if(m){ m.src=th.dataset.u; m.style.display=''; } $$('.li-th',feed).forEach(x=>x.classList.toggle('on',x===th)); });
    { const m=$('#li-main'); if(m) m.onclick=()=>openLightbox(m.currentSrc||m.src); }
    { const b=$('#li-msg'); if(b) b.onclick=()=>{ if(!dmPeers.has(e.pubkey)) dmPeers.set(e.pubkey,[]); dmActive=e.pubkey; switchView('messages'); setTimeout(()=>{ const i=$('#dm-in'); if(i){ i.value=`Hi! Is "${title}" still available?`; i.focus(); } },350); }; }
    { const b=$('#li-zap'); if(b) b.onclick=()=>doZap(e.id, e.pubkey); }
    { const b=$('#li-sold'); if(b) b.onclick=()=>toggleListingSold(e); }
    { const b=$('#li-edit'); if(b) b.onclick=()=>renderListingEditor(e); }
    { const b=$('#li-del'); if(b) b.onclick=()=>deleteListing(e); }
    $('#li-copy').onclick=()=>{ try{ const naddr=NT().nip19.naddrEncode({ identifier:(e.tags.find(t=>t[0]==='d')||[])[1]||'', pubkey:e.pubkey, kind:30402 }); navigator.clipboard.writeText(_webLink(naddr)); toast('listing link copied'); }catch(_){ navigator.clipboard.writeText(e.id); toast('id copied'); } };
    feed.querySelectorAll('[data-prof]').forEach(el=> el.onclick=()=>renderProfileView(el.dataset.prof));
    feed.querySelectorAll('.markdown img').forEach(im=> im.onclick=()=>openLightbox(im.currentSrc||im.src));
    decorateProfiles();
  }
  async function toggleListingSold(e){
    const now=mktStatus(e)==='sold';
    // republish the SAME addressable event with status flipped (keeps everything else intact)
    const tags=e.tags.filter(t=>t[0]!=='status'); tags.push(['status', now?'active':'sold']);
    try{ const r=await publish(30402, e.content, tags); if(r&&r.ok===false){ toast('relay: '+(r.msg||'rejected')); return; } toast(now?'marked available':'marked sold'); const ne=Store.get(r.ev.id)||r.ev; openListing(ne); }
    catch(err){ toast('failed: '+(err.message||'')); }
  }
  async function deleteListing(e){
    if(!confirm('Delete this listing? This asks every relay (NIP-09) to remove it.')) return;
    const slug=(e.tags.find(t=>t[0]==='d')||[])[1]||'';
    const tags=[['e',e.id]]; if(slug) tags.push(['a',`30402:${e.pubkey}:${slug}`]);
    try{ await publish(5,'deleted',tags); toast('deletion requested'); switchView('market'); }
    catch(err){ toast('delete failed: '+(err.message||'')); }
  }
  function renderListingEditor(existing){
    VIEW='listing'; $$('.nav-item[data-view]').forEach(b=>b.classList.remove('active')); $('#view-title').textContent=existing?'Edit listing':'New listing';
    const feed=$('#feed'); const g=(k)=>existing?((existing.tags.find(t=>t[0]===k)||[])[1]||''):'';
    const pTag=existing?mktPriceTag(existing):null;
    let images = existing?mktImages(existing):[];
    let cats = existing?mktCats(existing):[];
    let _slug = g('d') || null;
    feed.innerHTML=`<div class="article-editor">
      <button class="btn btn-ghost small" id="le-back">← Cancel</button>
      <label class="fld">Title<input class="input" id="le-title" placeholder="What are you selling?" value="${enc(g('title'))}"></label>
      <label class="fld">Summary<input class="input" id="le-sum" placeholder="One-line summary (optional)" value="${enc(g('summary'))}"></label>
      <div class="le-pricerow">
        <label class="fld">Price<input class="input" id="le-price" type="number" min="0" step="any" placeholder="0" value="${enc(pTag?(pTag[1]||''):'')}"></label>
        <label class="fld">Currency<select class="input" id="le-cur">${MKT_CURRENCIES.map(c=>`<option ${((pTag&&(pTag[2]||'').toUpperCase())===c)?'selected':''}>${c}</option>`).join('')}</select></label>
        <label class="fld">Per (optional)<input class="input" id="le-freq" placeholder="day / month…" value="${enc(pTag?(pTag[3]||''):'')}"></label>
      </div>
      <label class="fld">Location<input class="input" id="le-loc" placeholder="City / region (optional)" value="${enc(g('location'))}"></label>
      <div class="fld">Category<div class="mkt-cats" id="le-cats">${MKT_CATS.map(c=>`<button type="button" class="mkt-chip ${cats.includes(c)?'on':''}" data-c="${enc(c)}">${enc(c)}</button>`).join('')}</div></div>
      <div class="fld">Photos<div class="le-imgs" id="le-imgs"></div>
        <div class="row" style="margin-top:6px"><button type="button" class="btn btn-ghost small" id="le-img-up">🖼 Add photos</button><input type="file" id="le-img-file" accept="image/*" multiple hidden><span class="spacer"></span></div></div>
      <label class="fld">Description<textarea id="le-body" class="article-body" placeholder="Describe the item — condition, details… (markdown)">${enc(existing?existing.content:'')}</textarea></label>
      <label class="fld"><input type="checkbox" id="le-sold" ${existing&&mktStatus(existing)==='sold'?'checked':''}> Mark as sold</label>
      <div class="row"><span class="muted small" id="le-status"></span><span class="spacer"></span><button type="button" class="btn btn-ghost small" id="le-draft">💾 Save draft</button><button class="btn btn-neon" id="le-pub">Publish ▶</button></div>
    </div>`;
    $('#le-back').onclick=()=>switchView('market');
    $$('#le-cats .mkt-chip').forEach(b=> b.onclick=()=>{ const c=b.dataset.c; if(cats.includes(c)) cats=cats.filter(x=>x!==c); else cats.push(c); b.classList.toggle('on'); });
    const drawImgs=()=>{ const box=$('#le-imgs'); if(!box) return; box.innerHTML=images.map((u,i)=>`<div class="le-img"><img src="${enc(u)}" onerror="this.src='${LOGO}'"><button type="button" class="le-img-x" data-i="${i}">✕</button></div>`).join(''); $$('.le-img-x',box).forEach(x=> x.onclick=()=>{ images.splice(+x.dataset.i,1); drawImgs(); }); };
    drawImgs();
    $('#le-img-up').onclick=()=>$('#le-img-file').click();
    $('#le-img-file').onchange=async ev=>{ const files=[...ev.target.files]; for(let i=0;i<files.length;i++){ $('#le-status').textContent=`uploading ${i+1}/${files.length}…`; try{ images.push(await uploadBlob(files[i])); drawImgs(); }catch(err){ $('#le-status').textContent='upload failed: '+err.message; ev.target.value=''; return; } } $('#le-status').textContent=''; ev.target.value=''; };
    const _grab=()=>({ title:$('#le-title').value.trim(), summary:$('#le-sum').value.trim(), price:$('#le-price').value.trim(), cur:$('#le-cur').value, freq:$('#le-freq').value.trim(), loc:$('#le-loc').value.trim(), body:$('#le-body').value, sold:$('#le-sold').checked, images, cats });
    $('#le-draft').onclick=async()=>{
      const a=_grab(); if(!(a.title||a.body||a.images.length)){ toast('nothing to save yet'); return; }
      if(!_slug) _slug=_slugFor(a.title);
      $('#le-status').textContent='saving draft…';
      try{ await publishListing(a, _slug, 30403); $('#le-status').textContent='✓ draft saved'; toast('draft saved (in Market)'); }
      catch(err){ $('#le-status').textContent='draft save failed'; }
    };
    $('#le-pub').onclick=async()=>{
      const a=_grab(); if(!a.title){ toast('add a title'); return; }
      if(!_slug) _slug=_slugFor(a.title);
      $('#le-status').textContent='publishing…';
      try{
        const r=await publishListing(a, _slug, 30402);
        if(r&&r.ok===false){ toast('relay: '+(r.msg||'rejected')); $('#le-status').textContent=''; return; }
        if(existing && existing.kind===30403){ try{ await publish(5,'draft published',[['a',`30403:${ME.pubkey}:${_slug}`]]); }catch(_){} }
        toast('listing published'); switchView('market');
      }catch(err){ toast('publish failed: '+(err.message||'')); $('#le-status').textContent=''; }
    };
  }
  async function publishListing(a, slug, kind){
    const tags=[['d',slug],['title',a.title||''],['published_at',String(Math.floor(Date.now()/1000))]];
    if(a.summary) tags.push(['summary',a.summary]);
    if(a.price) tags.push(['price', String(a.price), (a.cur||'USD'), ...(a.freq?[a.freq]:[])]);
    if(a.loc) tags.push(['location',a.loc]);
    tags.push(['status', a.sold?'sold':'active']);
    (a.cats||[]).forEach(c=>tags.push(['t',c]));
    (a.images||[]).forEach(u=>tags.push(['image',u]));
    return await publish(kind, a.body||'', tags);
  }

  // ---------- torrents (NIP-35, kind 2003) ----------
  async function renderTorrents(){
    const feed=$('#feed'); feed.innerHTML='<div class="spinner"></div>';
    let evs=[]; try{ evs=await Relay.query([{ kinds:[2003], limit:80 }]); }catch(_){}
    evs.forEach(e=>{ Store.saveEvent(e); needProfile(e.pubkey); });
    if(VIEW!=='torrents') return;
    const tors=evs.sort((a,b)=>b.created_at-a.created_at);
    feed.innerHTML = tors.length ? tors.map(torrentCard).join('') : '<div class="empty">No torrents found on the relay yet (NIP-35 · kind 2003).</div>';
    decorateProfiles();
    $$('.tor-card .name[data-prof]',feed).forEach(n=> n.onclick=()=>renderProfileView(n.dataset.prof));
    $$('.tor-copy',feed).forEach(b=> b.onclick=async()=>{ try{ await navigator.clipboard.writeText(b.dataset.magnet); toast('magnet copied'); }catch(_){ window.prompt('Magnet link:', b.dataset.magnet); } });
  }
  function _fmtBytes(n){ n=Number(n)||0; const u=['B','KB','MB','GB','TB']; let i=0; while(n>=1024&&i<u.length-1){n/=1024;i++;} return n.toFixed(n<10&&i>0?1:0)+' '+u[i]; }
  function _magnet(e){
    const ih=((e.tags.find(t=>t[0]==='x')||[])[1]||'').trim();
    if(!/^([0-9a-f]{40}|[0-9a-f]{64})$/i.test(ih)) return '';   // valid btih v1(40)/v2(64) hex only
    const title=(e.tags.find(t=>t[0]==='title')||[])[1]||'';
    const trs=e.tags.filter(t=>t[0]==='tracker'&&t[1]).map(t=>'&tr='+encodeURIComponent(t[1])).join('');
    return `magnet:?xt=urn:btih:${ih}${title?'&dn='+encodeURIComponent(title):''}${trs}`;
  }
  function torrentCard(e){
    const p=profOf(e.pubkey); needProfile(e.pubkey);
    const title=(e.tags.find(t=>t[0]==='title')||[])[1]||'(untitled torrent)';
    const files=e.tags.filter(t=>t[0]==='file');
    const total=files.reduce((s,t)=>s+(Number(t[2])||0),0);
    const cats=e.tags.filter(t=>t[0]==='t'&&t[1]).slice(0,6).map(t=>`<span class="tor-tag">${enc(t[1])}</span>`).join('');
    const mag=_magnet(e);
    return `<article class="tor-card note"><div class="body">
      <div class="tor-title">🧲 ${enc(title)}</div>
      <div class="art-by"><img class="art-av" src="${enc(p.picture||LOGO)}" onerror="this.src='${LOGO}'"><span class="name" data-prof="${e.pubkey}">${enc(p.name||p.display_name||'anon')}</span>${total?`<span class="muted small">· ${_fmtBytes(total)} · ${files.length} file${files.length===1?'':'s'}</span>`:''}</div>
      ${e.content?`<div class="tor-desc">${enc(e.content.slice(0,400))}</div>`:''}
      ${cats?`<div class="tor-tags">${cats}</div>`:''}
      <div class="row tor-actions">${mag?`<a class="btn btn-cyan small" href="${enc(mag)}">🧲 Open magnet</a><button class="btn btn-ghost small tor-copy" data-magnet="${enc(mag)}">⧉ Copy</button>`:'<span class="muted small">no infohash</span>'}</div>
    </div></article>`;
  }
  // ---------- git repos (NIP-34, kind 30617 repository announcements) ----------
  async function renderRepos(){
    const feed=$('#feed'); feed.innerHTML='<div class="spinner"></div>';
    let evs=[]; try{ evs=await Relay.query([{ kinds:[30617], limit:80 }]); }catch(_){}
    evs.forEach(e=>{ Store.saveEvent(e); needProfile(e.pubkey); });
    if(VIEW!=='repos') return;
    const repos=_dedupAddr(evs).sort((a,b)=>b.created_at-a.created_at);
    feed.innerHTML = `<div class="art-top"><button class="btn btn-neon small" id="repo-new">＋ Announce a repo</button></div>`
      + (repos.length ? repos.map(repoCard).join('') : '<div class="empty">No git repos found on the relay yet (NIP-34 · kind 30617). Announce yours ↑</div>');
    $('#repo-new').onclick=()=>publishRepo();
    decorateProfiles();
    $$('.repo-card .name[data-prof]',feed).forEach(n=> n.onclick=()=>renderProfileView(n.dataset.prof));
    $$('.repo-clone',feed).forEach(b=> b.onclick=async()=>{ try{ await navigator.clipboard.writeText(b.dataset.clone); toast('clone URL copied'); }catch(_){ window.prompt('Clone:', b.dataset.clone); } });
  }
  // Publish a NIP-34 repo announcement (kind 30617) signed by the user, so it shows here + in other
  // Nostr git clients (gitworkshop, ngit, …). d-tag = repo id (replaceable per identifier).
  function publishRepo(existing){
    const tag=(e,k)=>(existing&&Array.isArray(existing.tags))?((existing.tags.find(t=>t[0]===k)||[])[1]||''):'';
    modal(`<h3>🌱 Announce a git repo</h3>
      <p class="muted small">Publishes a NIP-34 repo announcement (kind 30617) signed by your key.</p>
      <label class="fld">Repo id <span class="muted small">(short slug, e.g. posterchanai)</span><input class="input" id="rp-d" value="${enc(tag(existing,'d'))}" placeholder="my-app"></label>
      <label class="fld">Name<input class="input" id="rp-name" value="${enc(tag(existing,'name'))}" placeholder="My App"></label>
      <label class="fld">Description<textarea class="input" id="rp-desc" rows="2">${enc(tag(existing,'description'))}</textarea></label>
      <label class="fld">Clone URL<input class="input" id="rp-clone" value="${enc(tag(existing,'clone'))}" placeholder="https://git.example.com/me/my-app.git"></label>
      <label class="fld">Web URL<input class="input" id="rp-web" value="${enc(tag(existing,'web'))}" placeholder="https://git.example.com/me/my-app"></label>
      <div class="set-actions"><button class="btn btn-neon small" id="rp-pub">Publish</button><button class="btn btn-ghost small" id="rp-cancel">Cancel</button></div>
      <div class="muted small" id="rp-status"></div>`,
      root=>{
        $('#rp-cancel',root).onclick=closeModal;
        $('#rp-pub',root).onclick=async()=>{
          const v=id=>($('#'+id,root).value||'').trim();
          const d=v('rp-d'); const st=$('#rp-status',root);
          if(!d){ st.textContent='Repo id is required.'; return; }
          const tags=[['d',d]];
          if(v('rp-name')) tags.push(['name',v('rp-name')]);
          if(v('rp-desc')) tags.push(['description',v('rp-desc')]);
          if(v('rp-clone')) tags.push(['clone',v('rp-clone')]);
          if(v('rp-web')) tags.push(['web',v('rp-web')]);
          tags.push(['alt',`git repository: ${v('rp-name')||d}`]);
          st.textContent='publishing…';
          try{ const r=await publish(30617,'',tags);
            if(r && r.ok===false){ st.textContent='relay: '+(r.msg||'rejected'); }
            else { toast('repo announced'); closeModal(); switchView('repos'); }
          }catch(e){ st.textContent='failed: '+((e&&e.message)||e); }
        };
      });
  }
  function repoCard(e){
    const p=profOf(e.pubkey); needProfile(e.pubkey);
    const name=(e.tags.find(t=>t[0]==='name')||[])[1]||(e.tags.find(t=>t[0]==='d')||[])[1]||'(unnamed repo)';
    const desc=(e.tags.find(t=>t[0]==='description')||[])[1]||'';
    const clone=(e.tags.find(t=>t[0]==='clone')||[]).slice(1).filter(Boolean);
    const web=(e.tags.find(t=>t[0]==='web')||[]).slice(1).filter(Boolean);
    const wurl=_mdUrl(web[0]||'');   // scheme-allowlist (http/https only) — a relay-supplied javascript: href must never become clickable
    return `<article class="repo-card note"><div class="body">
      <div class="tor-title">🌱 ${enc(name)}</div>
      <div class="art-by"><img class="art-av" src="${enc(p.picture||LOGO)}" onerror="this.src='${LOGO}'"><span class="name" data-prof="${e.pubkey}">${enc(p.name||p.display_name||'anon')}</span></div>
      ${desc?`<div class="tor-desc">${enc(desc.slice(0,400))}</div>`:''}
      <div class="row tor-actions">${wurl?`<a class="btn btn-cyan small" href="${enc(wurl)}" target="_blank" rel="noopener">↗ Open</a>`:''}${clone.length?`<button class="btn btn-ghost small repo-clone" data-clone="${enc(clone[0])}">⧉ Copy clone URL</button>`:''}</div>
    </div></article>`;
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
  // ---------- communities (NIP-72 moderated communities, kind 34550) ----------
  async function renderCommunities(){
    const feed=$('#feed'); feed.innerHTML='<div class="spinner"></div>';
    let evs=[]; try{ evs=await Relay.query([{ kinds:[34550], limit:100 }]); }catch(_){}
    evs.forEach(e=>{ Store.saveEvent(e); needProfile(e.pubkey); });
    if(VIEW!=='communities') return;
    const comms=_dedupAddr(evs).sort((a,b)=>b.created_at-a.created_at);
    feed.innerHTML = comms.length
      ? `<div class="stream-grid">${comms.map(communityCard).join('')}</div>`
      : '<div class="empty">No communities yet. They show up here as people in your network create or post in NIP-72 communities.</div>';
    decorateProfiles();
    $$('.community-card',feed).forEach(c=> c.onclick=ev=>{ if(ev.target.closest('[data-prof]')){ renderProfileView(c.dataset.pk); return; } const x=Store.get(c.dataset.id); if(x) openCommunity(x); });
  }
  function communityCard(e){
    const p=profOf(e.pubkey); needProfile(e.pubkey);
    const name=(e.tags.find(t=>t[0]==='name')||[])[1]||(e.tags.find(t=>t[0]==='d')||[])[1]||'(unnamed)';
    const desc=(e.tags.find(t=>t[0]==='description')||[])[1]||'';
    const img=(e.tags.find(t=>t[0]==='image')||[])[1]||'';
    return `<article class="stream-card community-card" data-id="${e.id}" data-pk="${e.pubkey}">
      <div class="stream-thumb">${img?`<img src="${enc(img)}" loading="lazy" onerror="this.parentElement.classList.add('noimg')">`:'<span class="stream-play">☷</span>'}</div>
      <div class="stream-meta"><div class="stream-title">${enc(name)}</div>
        ${desc?`<div class="muted small">${enc(desc.slice(0,120))}</div>`:''}
        <div class="art-by"><img class="art-av" src="${enc(p.picture||LOGO)}" onerror="this.src='${LOGO}'"><span class="name" data-prof="${e.pubkey}">${enc(p.name||p.display_name||'anon')}</span></div>
      </div></article>`;
  }
  async function openCommunity(e){
    VIEW='community'; $$('.nav-item[data-view]').forEach(b=>b.classList.remove('active')); $('#view-title').textContent='Community';
    const feed=$('#feed'); const p=profOf(e.pubkey); needProfile(e.pubkey);
    const d=(e.tags.find(t=>t[0]==='d')||[])[1]||'';
    const name=(e.tags.find(t=>t[0]==='name')||[])[1]||d||'(unnamed)';
    const desc=(e.tags.find(t=>t[0]==='description')||[])[1]||'';
    const addr='34550:'+e.pubkey+':'+d;
    feed.innerHTML=`<div class="article-view">
      <button class="btn btn-ghost small" id="comm-back">← Communities</button>
      <h1 class="av-title">${enc(name)}</h1>
      ${desc?`<div class="about">${linkify(desc)}</div>`:''}
      <div class="row"><button class="btn btn-neon small" id="comm-post">✎ Post to this community</button></div>
      <div class="search-section-title">Recent posts</div>
      <div id="comm-posts"><div class="spinner"></div></div>
    </div>`;
    $('#comm-back').onclick=()=>switchView('communities');
    { const cp=$('#comm-post'); if(cp) cp.onclick=()=>compose({community:e}); }
    feed.querySelectorAll('[data-prof]').forEach(el=> el.onclick=()=>renderProfileView(el.dataset.prof));
    // Posts reference the community via an `a` tag (34550:pubkey:d). Modern NIP-72 clients post as
    // NIP-22 comments (kind 1111); older ones use kind 1 — query both. Note actions work via the
    // #feed delegated click handler.
    let posts=[]; try{ posts=await Relay.query([{ kinds:[1,1111], '#a':[addr], limit:80 }]); }catch(_){}
    posts.forEach(x=>{ Store.saveEvent(x); needProfile(x.pubkey); });
    if(VIEW!=='community') return;
    const box=$('#comm-posts'); if(!box) return;
    posts=posts.filter(x=>!isMutedView(x)).sort((a,b)=>b.created_at-a.created_at);
    box.innerHTML = posts.length ? posts.map(x=>noteCard(x)).join('') : '<div class="empty">No posts in this community yet.</div>';
    decorateProfiles();
  }
  // ---------- public chat (NIP-28: kind 40 channel · 41 metadata · 42 message) ----------
  // Pure Nostr, NO DB: a channel IS a kind-40 event (its id = the channel id); messages are kind-42
  // with a root `e`-tag to that id. Instance-local — only WoT members' channels/messages reach our
  // relay. Live messages kept in _chatMsgs (Store.feed() is kind-1/6 only, so we hold our own set).
  let _chatSub=null, _chatId=null, _chatMsgs=new Map();
  // Engagement on chat messages, shared by NIP-28 channels + NIP-29 groups:
  //  _chatReacts:  targetMsgId → Map(emoji → Set(reactor-pubkey))  ('+'/'' → ❤️, '-' → 👎)
  //  _chatZaps:    targetMsgId → { sats, n }   (kind-9735 zap receipts)
  //  _chatDeleted: targetMsgId → Set(deleter-pubkey)  (kind-5; honoured only when deleter == author)
  //  _chatAuxSeen: processed kind-5/9735 ids, so re-polls don't double-count zaps
  //  _chatReplyTo: id of the message the compose box is replying to (null = top-level)
  let _chatReacts=new Map(), _chatZaps=new Map(), _chatDeleted=new Map(), _chatAuxSeen=new Set();
  let _chatReactPoll=null, _chatReplyTo=null;
  function _chanMeta(e){ let m={}; try{ m=JSON.parse(e.content||'{}')||{}; }catch(_){} return { name:(m.name||'').trim()||'(unnamed)', about:m.about||'', picture:m.picture||'' }; }
  async function renderChatrooms(){
    const feed=$('#feed'); feed.innerHTML='<div class="spinner"></div>';
    // Instance-local: the relay holds many synced channel DEFINITIONS (kind-40) but only messages
    // (kind-42) from WoT members. Showing 50 empty foreign channels is noise — list only channels
    // that have activity HERE, plus your own, newest-active first.
    let chans=[], msgs=[];
    try{ [chans, msgs] = await Promise.all([ Relay.query([{ kinds:[40], limit:200 }]), Relay.query([{ kinds:[42], limit:500 }]) ]); }catch(_){}
    chans.forEach(e=>{ Store.saveEvent(e); needProfile(e.pubkey); });
    if(VIEW!=='chat') return;
    const active=new Map(); msgs.forEach(m=>{ const r=(m.tags.find(t=>t[0]==='e')||[])[1]; if(r) active.set(r, Math.max(active.get(r)||0, m.created_at)); });
    const shown=chans.filter(c=> active.has(c.id) || (ME && c.pubkey===ME.pubkey))
      .sort((a,b)=> (active.get(b.id)||b.created_at) - (active.get(a.id)||a.created_at));
    feed.innerHTML=`<div class="chat-list">
      <div class="row" style="margin-bottom:12px"><button class="btn btn-neon small" id="ch-new">＋ New channel</button></div>
      ${shown.length?`<div class="stream-grid">${shown.map(channelCard).join('')}</div>`
        :'<div class="empty">No active channels yet. Tap ＋ New channel to start one — channels appear here once they have messages on this instance.</div>'}
      <div id="nip29-groups"></div></div>`;
    decorateProfiles();
    { const b=$('#ch-new'); if(b) b.onclick=createChannel; }
    $$('.channel-card',feed).forEach(c=> c.onclick=ev=>{ if(ev.target.closest('[data-prof]')){ renderProfileView(c.dataset.pk); return; } const x=Store.get(c.dataset.id); if(x) openChannel(x); });
    loadNip29Groups();   // async-append the NIP-29 (0xchat) groups section — read-only browse
  }
  function channelCard(e){
    const p=profOf(e.pubkey); needProfile(e.pubkey); const m=_chanMeta(e);
    return `<article class="stream-card channel-card" data-id="${e.id}" data-pk="${e.pubkey}">
      <div class="stream-thumb">${m.picture?`<img src="${enc(m.picture)}" loading="lazy" onerror="this.parentElement.classList.add('noimg')">`:'<span class="stream-play">✺</span>'}</div>
      <div class="stream-meta"><div class="stream-title">${enc(m.name)}</div>
        ${m.about?`<div class="muted small">${enc(m.about.slice(0,120))}</div>`:''}
        <div class="art-by"><img class="art-av" src="${enc(p.picture||LOGO)}" onerror="this.src='${LOGO}'"><span class="name" data-prof="${e.pubkey}">${enc(p.name||p.display_name||'anon')}</span></div>
      </div></article>`;
  }
  async function createChannel(){
    const name=(prompt('Channel name?')||'').trim(); if(!name) return;
    const about=(prompt('Description (optional)?')||'').trim();
    try{ const { ev }=await publish(40, JSON.stringify({ name, about }), []); toast('channel created'); if(ev){ Store.saveEvent(ev); openChannel(ev); } }
    catch(e){ toast('create failed: '+((e&&e.message)||e)); }
  }
  async function openChannel(e, focusId){
    VIEW='channel'; _chatId=e.id; _chatMsgs=new Map(); _chatReacts=new Map(); _chatZaps=new Map(); _chatDeleted=new Map(); _chatAuxSeen=new Set(); _chatReplyTo=null;
    if(_chatSub){ try{ Relay.close(_chatSub); }catch(_){} _chatSub=null; }
    if(_chatReactPoll){ clearTimeout(_chatReactPoll); _chatReactPoll=null; }
    $$('.nav-item[data-view]').forEach(b=>b.classList.remove('active'));
    const m=_chanMeta(e); $('#view-title').textContent=m.name;
    const feed=$('#feed'); feed.classList.add('feed-chat');
    feed.innerHTML=`<div class="chatroom">
      <div class="chatroom-head"><button class="btn btn-ghost small" id="ch-back">←</button><span class="chatroom-title">✺ ${enc(m.name)}</span></div>
      ${m.about?`<div class="chatroom-about">${linkify(m.about)}</div>`:''}
      <div id="ch-msgs" class="chatroom-msgs"><div class="spinner"></div></div>
      <div id="chat-reply-bar" class="chat-reply-bar hidden"></div>
      <div class="chatroom-compose"><button class="mini" id="ch-attach" title="attach image">📎</button>${window.PC_NOSTR_ONLY?'':'<button class="mini" id="ch-translate" title="translate your message">🌐</button>'}<input type="file" id="ch-file" accept="image/*,video/*" multiple hidden><textarea id="ch-input" rows="1" placeholder="Message…"></textarea><button class="btn btn-neon" id="ch-send">Send</button></div>
    </div>`;
    $('#ch-back').onclick=()=>switchView('chat');
    { const mb=$('#ch-msgs'); if(mb) mb.addEventListener('click', _onChatMsgClick); }   // delegated reply/react taps
    const send=()=>postToChannel(e);
    { const b=$('#ch-send'); if(b) b.onclick=send; }
    { const ta=$('#ch-input'); if(ta){ ta.onkeydown=ev=>{ if(ev.key==='Enter' && !ev.shiftKey){ ev.preventDefault(); send(); } };
      ta.oninput=()=>{ ta.style.height='auto'; ta.style.height=Math.min(ta.scrollHeight,120)+'px'; }; } }
    // 🌐 translate YOUR draft to another language before sending (same picker as new Post / reply).
    { const tb=$('#ch-translate'), ta=$('#ch-input'); if(tb && ta) tb.onclick=()=>composeTranslate(ta, tb); }
    // 📎 attach: upload to Blossom, append the URL to the message (inline media renders in kind-42).
    { const ab=$('#ch-attach'), fi=$('#ch-file'), ta=$('#ch-input');
      if(ab && fi && ta){
        ab.onclick=()=>fi.click();
        fi.onchange=async ev=>{
          const files=[...ev.target.files]; ev.target.value=''; if(!files.length) return;
          ab.disabled=true; const lbl=ab.textContent; ab.textContent='⏳';
          for(let i=0;i<files.length;i++){
            try{ const url=await uploadBlob(files[i]);
              ta.value += (ta.value && !ta.value.endsWith('\n') ? '\n' : '') + url;
              ta.dispatchEvent(new Event('input')); }
            catch(err){ if(_blossomDenied(err)){ requestBlossomAccess(); toast('🔒 No upload access — requested it from the admin.'); }
              else toast('upload failed: '+((err&&err.message)||err)); break; }
          }
          ab.disabled=false; ab.textContent=lbl; ta.focus();
        };
      } }
    let msgs=[]; try{ msgs=await Relay.query([{ kinds:[42], '#e':[e.id], limit:300 }]); }catch(_){}
    if(VIEW!=='channel' || _chatId!==e.id) return;
    msgs.forEach(x=>{ _chatMsgs.set(x.id,x); needProfile(x.pubkey); });
    _drawChannel(true);
    if(focusId) _scrollChatTo(focusId);   // deep-link from a notification → highlight that message
    _pollChannelMeta();   // fetch + keep reactions/zaps/deletions for the on-screen messages live
    _chatSub=Relay.subscribe([{ kinds:[42], '#e':[e.id] }], { onEvent: ev=>{ if(ev.kind===42 && !_chatMsgs.has(ev.id)){ _chatMsgs.set(ev.id,ev); needProfile(ev.pubkey); if(VIEW==='channel' && _chatId===e.id) _drawChannel(); } } });
  }
  function _drawChannel(force){
    const box=$('#ch-msgs'); if(!box || !_chatId) return;
    const atBottom = force || (box.scrollHeight-box.scrollTop-box.clientHeight < 90);
    const msgs=[..._chatMsgs.values()].filter(x=>!isMutedView(x) && !_isChatDeleted(x)).sort((a,b)=>a.created_at-b.created_at);
    box.innerHTML = msgs.length ? msgs.map(chatMsg).join('') : '<div class="empty">No messages yet — say hi 👋</div>';
    decorateProfiles();
    if(atBottom) box.scrollTop=box.scrollHeight;
  }
  function chatMsg(e){
    const p=profOf(e.pubkey); needProfile(e.pubkey); const mine=ME && e.pubkey===ME.pubkey;
    const pills=chatReactPills(e.id), zap=chatZapPill(e.id);
    // Both NIP-28 channels (publish to our pool) and NIP-29 groups (authed publish to the group relay)
    // are writable now, so show the reply/react affordances in both.
    const canWrite = VIEW==='channel' || VIEW==='group';
    // 🌐 translate is a personal read-only action (DOM-only, like translatePost), so it's available on
    // every message regardless of write access — but only when the node has the AI backend (not nostr-only).
    const tr = window.PC_NOSTR_ONLY ? '' : `<button class="chat-mini chat-tr-btn" data-translate="${enc(e.id)}" title="translate">🌐</button>`;
    const acts = (canWrite
      ? `<button class="chat-mini chat-reply-btn" data-reply="${enc(e.id)}" title="reply">↩</button><button class="chat-mini chat-react-add" data-react-add="${enc(e.id)}" data-pk="${enc(e.pubkey)}" title="react">😀</button>`
      : '') + tr;
    // reply context: when this message replies to one already loaded, show a tappable quote line.
    let rq=''; const par=_chatReplyParent(e);
    if(par){ const pm=_chatMsgs.get(par)||_groupMsgs.get(par); if(pm){ const pp=profOf(pm.pubkey);
      rq=`<button class="chat-replyq" data-scroll="${enc(par)}">↳ ${enc(pp.name||pp.display_name||'anon')}: ${enc((pm.content||'').replace(/\s+/g,' ').slice(0,60))}</button>`; } }
    return `<div class="chat-msg${mine?' mine':''}" data-pk="${e.pubkey}" data-mid="${enc(e.id)}">
      <img class="chat-av" data-prof="${e.pubkey}" src="${enc(p.picture||LOGO)}" onerror="this.src='${LOGO}'">
      <div class="chat-body"><div class="chat-by"><span class="name" data-prof="${e.pubkey}">${enc(p.name||p.display_name||'anon')}</span><span class="muted small">· ${timeAgo(e.created_at)}</span></div>
      ${rq}
      <div class="chat-txt">${linkify(e.content||'')}</div>
      ${pills||zap||acts?`<div class="chat-reacts">${pills}${zap}${acts}</div>`:''}</div></div>`;
  }
  // The id this message replies to (for in-room threading). NIP-28: a non-root e-tag; NIP-29: the
  // root is the group (h-tag) so any e-tag is the parent; an explicit "reply" marker always wins.
  function _chatReplyParent(e){
    const es=e.tags.filter(t=>t[0]==='e'&&t[1]); if(!es.length) return null;
    const reply=es.find(t=>t[3]==='reply'); if(reply) return reply[1];
    // NIP-28: the root e-tag is the channel, so an unmarked NON-root e-tag is the reply parent.
    // NIP-29: the root is the group (h-tag), so a bare e-tag may be a quote/mention — require the
    // explicit "reply" marker (handled above) rather than guessing, to avoid false reply-quotes.
    if(e.kind===42){ const nonRoot=es.find(t=>t[1]!==_chatId && t[3]!=='root'); return nonRoot?nonRoot[1]:null; }
    return null;
  }
  function _lastE(ev){ let id=null; for(const t of ev.tags){ if(t[0]==='e' && t[1]) id=t[1]; } return id; }
  function _cssEsc(s){ return (window.CSS&&CSS.escape)?CSS.escape(s):String(s).replace(/["\\]/g,'\\$&'); }
  function _flashChatMsg(el){ if(!el) return; el.scrollIntoView({block:'center',behavior:'smooth'}); el.classList.add('flash'); setTimeout(()=>el.classList.remove('flash'),1300); }
  // Scroll a chat message into view and flash it. Retries briefly since the list may still be drawing
  // (e.g. right after openChannel kicks off its message fetch).
  function _scrollChatTo(mid){
    let tries=0;
    const tick=()=>{ const box=$('#ch-msgs')||$('#grp-msgs'); const el=box&&box.querySelector(`.chat-msg[data-mid="${_cssEsc(mid)}"]`);
      if(el){ _flashChatMsg(el); return; }
      if(++tries<12) setTimeout(tick, 250);
      else toast('linked message isn’t in the recent history');   // older than the fetch window → not loaded
    };
    setTimeout(tick, 120);
  }
  // Record a kind-7 reaction into _chatReacts keyed by its target (last e-tag, per NIP-25).
  // Returns true if it was new (so the caller can redraw).
  const _chatEmojiImg = new Map();   // NIP-30 custom-emoji ":shortcode:" → image URL (stable cache)
  function _recordReact(ev){
    if(!ev || ev.kind!==7) return false;
    const tid=_lastE(ev); if(!tid) return false;
    let emoji=ev.content||''; if(emoji==='+'||emoji===''){ emoji='❤️'; } else if(emoji==='-'){ emoji='👎'; }
    // NIP-30 custom emoji: content is ":shortcode:" with an ["emoji", shortcode, url] tag. Cache the
    // url so the pill shows the IMAGE — otherwise the bare ":shortcode:" text reads like inline code.
    if(/^:[^:\s]+:$/.test(emoji)){ const nm=emoji.slice(1,-1); const t=(ev.tags||[]).find(x=>x[0]==='emoji'&&x[1]===nm&&x[2]); if(t) _chatEmojiImg.set(emoji, t[2]); }
    let m=_chatReacts.get(tid); if(!m){ m=new Map(); _chatReacts.set(tid,m); }
    let s=m.get(emoji); if(!s){ s=new Set(); m.set(emoji,s); }
    if(s.has(ev.pubkey)) return false; s.add(ev.pubkey); return true;
  }
  // Fold a kind 7 (reaction), 9735 (zap receipt) or 5 (deletion) into the chat engagement maps.
  // Zaps/deletions are deduped by event id (_chatAuxSeen) so re-polls don't double-count sats.
  function _recordChatAux(ev){
    if(!ev) return false;
    if(ev.kind===7) return _recordReact(ev);
    if(_chatAuxSeen.has(ev.id)) return false;
    if(ev.kind===9735){ const tid=_lastE(ev); if(!tid) return false; const sats=zapAmount(ev); if(!sats) return false;   // unparseable → leave unseen so a fuller re-fetch can still count it
      _chatAuxSeen.add(ev.id); const z=_chatZaps.get(tid)||{sats:0,n:0}; z.sats+=sats; z.n++; _chatZaps.set(tid,z); return true; }
    if(ev.kind===5){ _chatAuxSeen.add(ev.id); let ch=false; for(const t of ev.tags){ if(t[0]==='e' && t[1]){
      let s=_chatDeleted.get(t[1]); if(!s){ s=new Set(); _chatDeleted.set(t[1],s); } if(!s.has(ev.pubkey)){ s.add(ev.pubkey); ch=true; } } } return ch; }
    return false;
  }
  // Hide a message only when its OWN author published the kind-5 deleting it (NIP-09) — for channels
  // AND groups. (Honouring any group kind-5 would let any member hide anyone's message; real NIP-29
  // moderation is a kind-9005, which we don't act on here.)
  function _isChatDeleted(msg){ const s=_chatDeleted.get(msg.id); return !!(s && s.has(msg.pubkey)); }
  function chatReactPills(id){
    const m=_chatReacts.get(id); if(!m) return '';
    const mine=ME&&ME.pubkey;
    return [...m.entries()].filter(([,s])=>s.size).sort((a,b)=>b[1].size-a[1].size).map(([emoji,s])=>{
      const on=mine && s.has(mine);
      const url=_chatEmojiImg.get(emoji);
      const disp = url ? `<img class="chat-react-img" src="${enc(url)}" alt="${enc(emoji)}" loading="lazy">` : enc(emoji);
      return `<button class="chat-react${on?' on':''}" data-react="${enc(id)}" data-emoji="${enc(emoji)}" title="react ${enc(emoji)}">${disp} <span class="n">${s.size}</span></button>`;
    }).join('');
  }
  function chatZapPill(id){ const z=_chatZaps.get(id); return z&&z.sats?`<span class="chat-zap" title="${z.n} zap${z.n>1?'s':''}">⚡ ${fmtSats(z.sats)}</span>`:''; }
  // Delegated click on a chat-message list: jump to a quoted parent, start a reply, or react.
  function _onChatMsgClick(ev){
    const sc=ev.target.closest('.chat-replyq');
    if(sc){ const cont=ev.currentTarget, el=cont.querySelector(`.chat-msg[data-mid="${_cssEsc(sc.dataset.scroll)}"]`);
      if(el) _flashChatMsg(el); else toast('that message isn’t loaded'); return; }
    const tb=ev.target.closest('.chat-tr-btn'); if(tb){ translateChatMsg(tb.dataset.translate); return; }
    const rb=ev.target.closest('.chat-reply-btn'); if(rb){ _setChatReply(rb.dataset.reply); return; }
    const pill=ev.target.closest('.chat-react'); if(pill){ _doChatReact(pill.dataset.react, pill.dataset.emoji); return; }
    const add=ev.target.closest('.chat-react-add'); if(add){ openEmojiPopover(add, (emoji, close)=>{ close(); _doChatReact(add.dataset.reactAdd, emoji); }); }
  }
  async function _doChatReact(id, emoji){
    if(!ME){ toast('log in to react'); return; }
    const cur=_chatReacts.get(id); if(cur){ const s=cur.get(emoji); if(s && s.has(ME.pubkey)){ toast('already reacted '+emoji); return; } }
    const msg=_chatMsgs.get(id)||_groupMsgs.get(id), pk=msg?msg.pubkey:null;
    try{
      if(VIEW==='group'){ const ev=await _groupPublish(7, emoji, eTags(id,pk)); if(ev && _recordReact(ev)) _drawGroup(); return; }
      const { ev }=await publish(7, emoji, eTags(id,pk)); if(ev && _recordReact(ev)) _drawChannel(); toast('reacted '+emoji);
    }catch(e){ toast('react failed: '+((e&&e.message)||e)); }
  }
  // Translate a chat message in-place (channel + group), via the node's AI backend. DOM-only — the
  // stored event is untouched, so a refresh restores the original. Mirrors translatePost.
  async function translateChatMsg(id){
    const msg=_chatMsgs.get(id)||_groupMsgs.get(id); if(!msg){ toast('message not loaded'); return; }
    const src=(msg.content||'').trim(); if(!src){ toast('nothing to translate'); return; }
    const box=$('#ch-msgs')||$('#grp-msgs');
    const node=box&&box.querySelector(`.chat-msg[data-mid="${_cssEsc(id)}"] .chat-txt`);
    if(!node){ toast('message not visible'); return; }
    if(!node.dataset.orig) node.dataset.orig=node.innerHTML; node.style.opacity='.5';
    try{
      const r=await fetch('/client/translate',{ method:'POST', headers:{'Content-Type':'application/json'},
        body:JSON.stringify({ text:src, to:(navigator.language||'en') }) });
      const j=await r.json().catch(()=>({}));
      if(!r.ok || !j.text){ toast(j.error||'translation unavailable'); node.style.opacity=''; return; }
      if(j.text.trim()===src.trim()){ node.style.opacity=''; toast('nothing to translate — looks already in your language (or just sounds/emoji)'); return; }
      node.style.opacity=''; node.innerHTML=linkify(j.text)+'<div class="muted small tr-tag">🌐 translated · refresh to restore</div>';
    }catch(_){ toast('translate failed'); node.style.opacity=''; }
  }
  // ---- in-room reply target (shared by channel + group compose) ----
  function _setChatReply(id){ const msg=_chatMsgs.get(id)||_groupMsgs.get(id); if(!msg) return; _chatReplyTo=id; _renderReplyBar(); const ta=$('#ch-input')||$('#grp-input'); if(ta) ta.focus(); }
  function _clearChatReply(){ _chatReplyTo=null; _renderReplyBar(); }
  function _renderReplyBar(){
    const bar=$('#chat-reply-bar'); if(!bar) return;
    if(!_chatReplyTo){ bar.classList.add('hidden'); bar.innerHTML=''; return; }
    const msg=_chatMsgs.get(_chatReplyTo)||_groupMsgs.get(_chatReplyTo), p=msg?profOf(msg.pubkey):{};
    bar.classList.remove('hidden');
    bar.innerHTML=`<span class="muted small">↩ replying to <b>${enc(msg?(p.name||p.display_name||'anon'):'…')}</b>: ${enc(msg?(msg.content||'').replace(/\s+/g,' ').slice(0,50):'')}</span><button class="chat-reply-x" id="chat-reply-x" title="cancel">✕</button>`;
    const x=$('#chat-reply-x'); if(x) x.onclick=_clearChatReply;
  }
  // Channel engagement poll: channels keep a live kind-42 sub, but message-targeted kind 7/9735/5
  // can't be caught by the channel-root #e filter, so poll them for the on-screen ids (auto-stops on
  // leave via switchView clearing _chatReactPoll).
  function _pollChannelMeta(){
    if(VIEW!=='channel') return;
    const id=_chatId, ids=[..._chatMsgs.keys()];
    if(!ids.length){ _chatReactPoll=setTimeout(_pollChannelMeta, 10000); return; }
    Relay.query([{ kinds:[7,9735,5], '#e':ids, limit:800 }]).then(rs=>{
      if(VIEW!=='channel' || _chatId!==id) return;
      let changed=false; rs.forEach(r=>{ if(_recordChatAux(r)) changed=true; }); if(changed) _drawChannel();
    }).catch(()=>{}).finally(()=>{ if(VIEW==='channel' && _chatId===id) _chatReactPoll=setTimeout(_pollChannelMeta, 10000); });
  }
  async function postToChannel(chan){
    const ta=$('#ch-input'); if(!ta) return; const text=ta.value.trim(); if(!text) return;
    ta.value=''; ta.style.height='auto'; ta.disabled=true;
    const tags=[['e', chan.id, '', 'root']];
    if(_chatReplyTo && _chatReplyTo!==chan.id){ const par=_chatMsgs.get(_chatReplyTo); tags.push(['e', _chatReplyTo, '', 'reply']); if(par) tags.push(['p', par.pubkey]); }
    try{ const { ev }=await publish(42, text, tags); if(ev && !_chatMsgs.has(ev.id)){ _chatMsgs.set(ev.id,ev); if(VIEW==='channel' && _chatId===chan.id) _drawChannel(true); } }
    catch(e){ toast('send failed: '+((e&&e.message)||e)); }
    finally{ _clearChatReply(); ta.disabled=false; ta.focus(); }   // text was cleared optimistically → always drop the reply target so the next msg isn't mis-threaded
  }
  // ---- NIP-29 group writes (kind 9 message / 7 react / 9021 join) via NIP-42 authed publish ----
  async function _groupPublish(kind, content, extraTags){
    if(!ME){ toast('log in first'); return null; }
    const relay=_groupRelay, gid=_groupId; if(!relay||!gid){ toast('no group open'); return null; }
    try{   // catch signer rejections (extension/NIP-46 decline) too, so a write never fails silently
      const ev=await sign(kind, content, [['h', gid], ...(extraTags||[])]);
      const r=await Relay.publishAuthed(relay, ev, ch=>sign(22242, '', [['relay', relay], ['challenge', ch]]));
      if(!r.ok){ toast('group: '+(r.msg||'rejected')); return null; }
      return ev;
    }catch(e){ toast('group post failed: '+((e&&e.message)||e)); return null; }
  }
  async function postToGroup(){
    const ta=$('#grp-input'); if(!ta) return; const text=ta.value.trim(); if(!text) return;
    ta.disabled=true;
    const tags=[]; if(_chatReplyTo){ const par=_groupMsgs.get(_chatReplyTo); tags.push(['e', _chatReplyTo, _groupRelay, 'reply']); if(par) tags.push(['p', par.pubkey]); }
    try{ const ev=await _groupPublish(9, text, tags);
      if(ev){ ta.value=''; ta.style.height='auto'; _groupSeen.add(ev.id); _groupMsgs.set(ev.id,ev); needProfile(ev.pubkey); _clearChatReply(); if(VIEW==='group') _drawGroup(true); } }
    finally{ ta.disabled=false; ta.focus(); }
  }
  async function joinGroup(){ const ev=await _groupPublish(9021, 'join request', []); if(ev) toast('join request sent — may need admin approval'); }
  // ---------- NIP-29 relay-based groups (0xchat &c.) — READ-ONLY browse (Phase 1) ----------
  // Unlike our NIP-28 channels, NIP-29 groups live on DEDICATED external relays (not our pool):
  // kind-39000 = group metadata (relay-authored, addressable by d=group-id), kind-9 = chat messages
  // tagged h=group-id. We read them via the bounded ephemeral queryFrom (one-shot sockets, closed
  // after EOSE) and POLL every ~6s while a group is open — no persistent connection, CPU-bounded,
  // auto-stops on leave (switchView clears _groupPoll). Read-only for now: join/post (NIP-42 auth +
  // kind 9021/9) is a later phase.
  // (relay.groups.nip29.com dropped — broken TLS cert / hostname mismatch, browser WSS rejects it.)
  const NIP29_RELAYS = ['wss://groups.0xchat.com/', 'wss://relay.highlighter.com/'];
  const _NIP29_MAX = 60;   // 0xchat's relay ignores `limit` and dumps ALL (~1000+) groups → cap the render
  let _groupId=null, _groupRelay=null, _groupMsgs=new Map(), _groupPoll=null, _groupSeen=new Set();
  function _nip29Meta(e){ const tag=k=>(e.tags.find(t=>t[0]===k)||[])[1]||'';
    return { id:tag('d'), name:tag('name')||'(unnamed group)', about:tag('about'), picture:tag('picture') }; }
  async function loadNip29Groups(){
    if(!$('#nip29-groups')) return;
    // Query each group relay SEPARATELY so we know which relay each group lives on (queryFrom flattens
    // across relays and the group id is only unique per relay). Failures per relay are ignored.
    let groups=[];
    try{
      const lists=await Promise.all(NIP29_RELAYS.map(r=>
        Relay.queryFrom([r], [{ kinds:[39000], limit:100 }]).then(evs=>evs.map(e=>({e, relay:r}))).catch(()=>[])));
      const seen=new Set();
      for(const {e, relay} of lists.flat()){
        const m=_nip29Meta(e); if(!m.id) continue; const key=relay+"'"+m.id; if(seen.has(key)) continue; seen.add(key);
        groups.push({ relay, m, key });
      }
    }catch(_){}
    const box=$('#nip29-groups'); if(!box || VIEW!=='chat') return;
    const total=groups.length;
    // pictured groups first (more "real"/curated), then by name; cap the render (relay dumps 1000+).
    groups.sort((a,b)=> (b.m.picture?1:0)-(a.m.picture?1:0) || a.m.name.localeCompare(b.m.name));
    const shownG=groups.slice(0, _NIP29_MAX);
    box.innerHTML = total
      ? `<div class="search-section-title" style="margin-top:18px">Groups · NIP-29 <span class="muted small">(0xchat &amp; others — read-only${total>_NIP29_MAX?` · showing ${_NIP29_MAX} of ${total}`:''})</span></div>
         <div class="stream-grid">${shownG.map(nip29Card).join('')}</div>`
      : '';   // none found (or the relays require login) → no section
    $$('.nip29-card',box).forEach(c=>{ const g=groups.find(x=>x.key===c.dataset.key); if(g) c.onclick=()=>openGroup(g); });
  }
  function nip29Card(g){ const m=g.m;
    return `<article class="stream-card nip29-card" data-key="${enc(g.key)}">
      <div class="stream-thumb">${m.picture?`<img src="${enc(m.picture)}" loading="lazy" onerror="this.parentElement.classList.add('noimg')">`:'<span class="stream-play">👥</span>'}</div>
      <div class="stream-meta"><div class="stream-title">${enc(m.name)}</div>
        ${m.about?`<div class="muted small">${enc(m.about.slice(0,120))}</div>`:''}
        <div class="muted small">🛰 ${enc(g.relay.replace(/^wss:\/\//,'').replace(/\/$/,''))}</div>
      </div></article>`;
  }
  async function openGroup(g){
    VIEW='group'; _groupId=g.m.id; _groupRelay=g.relay; _groupMsgs=new Map(); _groupSeen=new Set();
    _chatReacts=new Map(); _chatZaps=new Map(); _chatDeleted=new Map(); _chatAuxSeen=new Set(); _chatReplyTo=null;
    if(_groupPoll){ clearTimeout(_groupPoll); _groupPoll=null; }
    $$('.nav-item[data-view]').forEach(b=>b.classList.remove('active'));
    $('#view-title').textContent=g.m.name;
    const feed=$('#feed'); feed.classList.add('feed-chat');
    // Writable now: posting/reacting/joining go out as NIP-42-authed events to the group's relay. The
    // relay rejects non-members, so a "join" button sends a kind-9021 request first.
    feed.innerHTML=`<div class="chatroom">
      <div class="chatroom-head"><button class="btn btn-ghost small" id="grp-back">←</button><span class="chatroom-title">👥 ${enc(g.m.name)}</span><button class="btn btn-ghost small" id="grp-join" title="request to join">＋ Join</button></div>
      ${g.m.about?`<div class="chatroom-about">${linkify(g.m.about)}</div>`:''}
      <div id="grp-msgs" class="chatroom-msgs"><div class="spinner"></div></div>
      <div id="chat-reply-bar" class="chat-reply-bar hidden"></div>
      <div class="chatroom-compose"><textarea id="grp-input" rows="1" placeholder="Message… (members only)"></textarea><button class="btn btn-neon" id="grp-send">Send</button></div>
    </div>`;
    $('#grp-back').onclick=()=>switchView('chat');
    { const jb=$('#grp-join'); if(jb) jb.onclick=joinGroup; }
    { const sb=$('#grp-send'); if(sb) sb.onclick=postToGroup; }
    { const ta=$('#grp-input'); if(ta){ ta.onkeydown=ev=>{ if(ev.key==='Enter' && !ev.shiftKey){ ev.preventDefault(); postToGroup(); } };
      ta.oninput=()=>{ ta.style.height='auto'; ta.style.height=Math.min(ta.scrollHeight,120)+'px'; }; } }
    { const mb=$('#grp-msgs'); if(mb) mb.addEventListener('click', _onChatMsgClick); }   // delegated reply/react taps
    _pollGroup(true);
  }
  async function _pollGroup(first){
    const relay=_groupRelay, id=_groupId; if(!relay||!id) return;
    // NIP-29 tags ALL group content with h=group-id (messages, reactions, zaps, deletions), so one #h
    // filter per kind fetches everything from the group's relay.
    let evs=[], aux=[];
    try{ [evs, aux]=await Promise.all([
      Relay.queryFrom([relay], [{ kinds:[9], '#h':[id], limit:200 }]),
      Relay.queryFrom([relay], [{ kinds:[7,9735,5], '#h':[id], limit:300 }]).catch(()=>[]),
    ]); }catch(_){}
    if(VIEW!=='group' || _groupId!==id) return;
    // External relay is untrusted → verify signatures of UNSEEN events (messages + engagement) only.
    const fresh=[...(evs||[]), ...(aux||[])].filter(e=>e && e.id && !_groupSeen.has(e.id));
    if(fresh.length){ try{ const v=await Relay.worker.call('verifyBatch',{events:fresh});
      const ok=new Set(v.filter(r=>r.valid).map(r=>r.id));
      for(const e of fresh){ if(!ok.has(e.id)){ _groupSeen.add(e.id); continue; }   // bad sig → never retry
        if(e.kind===9){ _groupSeen.add(e.id); _groupMsgs.set(e.id,e); needProfile(e.pubkey); }
        else if(_recordChatAux(e)) _groupSeen.add(e.id); }   // recorded → seen; an unparseable zap stays retryable next poll
    }catch(_){} }
    if(VIEW!=='group' || _groupId!==id) return;
    _drawGroup(first);
    _groupPoll=setTimeout(()=>{ if(VIEW==='group' && _groupId===id) _pollGroup(false); }, 6000);
  }
  function _drawGroup(force){
    const box=$('#grp-msgs'); if(!box || !_groupId) return;
    const atBottom = force || (box.scrollHeight-box.scrollTop-box.clientHeight < 90);
    const msgs=[..._groupMsgs.values()].filter(x=>!isMutedView(x) && !_isChatDeleted(x)).sort((a,b)=>a.created_at-b.created_at);
    box.innerHTML = msgs.length ? msgs.map(chatMsg).join('') : '<div class="empty">No messages, or this group requires login to read.</div>';
    decorateProfiles();
    if(atBottom) box.scrollTop=box.scrollHeight;
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
  // Monero address published in kind-0 metadata. No standard key exists yet, so we WRITE `xmr` and
  // READ a few aliases other clients might use. Light validation: mainnet std(95)/integrated(106),
  // base58, starts 4 (std/integrated) or 8 (subaddress).
  function xmrOf(p){ return ((p&&(p.xmr||p.monero||p.monero_address))||'').toString().trim(); }
  function isXmrAddr(a){ return /^[48][1-9A-HJ-NP-Za-km-z]{94}([1-9A-HJ-NP-Za-km-z]{11})?$/.test((a||'').trim()); }  // base58 (no 0 O I l), exactly 95 (std/sub) or 106 (integrated)
  function noteHtml(ev){
    if (ev.kind===6){  // repost
      let inner=null; try{ inner=JSON.parse(ev.content); }catch(_){}
      if(inner && inner.id) Store.saveEvent(inner);
      const origId=(ev.tags.find(t=>t[0]==='e')||[])[1];
      const orig = inner || Store.get(origId);
      const rp = profOf(ev.pubkey); needProfile(ev.pubkey);
      if(orig){ needProfile(orig.pubkey); return noteCard(orig, `<div class="repost-tag">${RT_ICON} ${enc(rp.name||'someone')} reposted</div>`); }
      needEvent(origId);   // fetch the original; flushEvents patches this placeholder in place
      return `<article class="note" data-orig="${origId}" data-reposter="${enc(rp.name||'someone')}"><div class="body"><div class="repost-tag">${RT_ICON} ${enc(rp.name||'someone')} reposted</div><div class="muted small">loading post…</div></div></article>`;
    }
    if (ev.kind===1068) return pollCard(ev);   // NIP-88 poll
    if (ev.kind===30023) return articleCard(ev);   // NIP-23 long-form article → reader card
    if (ev.kind===34550) return communityCard(ev);  // NIP-72 community → discovery card in the feed
    if (ev.kind===40) return channelCard(ev);        // NIP-28 channel → discovery card in the feed
    return noteCard(ev);
  }
  // ---------- NIP-88 polls: kind-1068 poll, kind-1018 responses ----------
  const _myPollVotes = {};   // pollId -> Set(optionId)
  function pollCard(ev){
    const p=profOf(ev.pubkey); needProfile(ev.pubkey);
    const name=p.name||p.display_name||(NT().nip19.npubEncode(ev.pubkey).slice(0,12)+'…');
    const handle=niceNip05(p.nip05)||('@'+NT().nip19.npubEncode(ev.pubkey).slice(4,12));
    const opts=ev.tags.filter(t=>t[0]==='option'&&t[1]).map(t=>({id:t[1],label:t[2]||t[1]}));
    const multi=((ev.tags.find(t=>t[0]==='polltype')||[])[1]==='multiplechoice');
    const endsAt=parseInt((ev.tags.find(t=>t[0]==='endsAt')||[])[1]||'0',10);
    const ended=endsAt && endsAt<Math.floor(Date.now()/1000);
    const optHtml=opts.map(o=>`<button class="poll-opt" data-poll="${ev.id}" data-opt="${enc(o.id)}"${ended?' disabled':''}><span class="poll-bar"></span><span class="poll-label">${enc(o.label)}</span><span class="poll-pct"></span></button>`).join('');
    return `<article class="note poll" data-id="${ev.id}" data-pk="${ev.pubkey}">
      <img class="av" src="${enc(p.picture||LOGO)}" onerror="this.src='${LOGO}'">
      <div class="body">
        <div class="hd"><span class="name" data-prof="${ev.pubkey}">${enc(name)}</span><span class="vchk"></span>
          <span class="handle">${enc(handle)}</span><span class="time">${timeAgo(ev.created_at)}</span></div>
        <div class="poll-q">📊 ${linkify(ev.content||'')}</div>
        <div class="poll-opts">${optHtml}</div>
        <div class="poll-foot muted small">${multi?'Multiple choice':'Single choice'}${ended?' · ended':''} · <span class="poll-total">…</span></div>
        <div class="acts"><button class="act" data-a="reply" title="reply">${REPLY_ICON} <span class="n"></span></button>
          <button class="act actm" data-a="menu" title="more">☰</button></div>
      </div></article>`;
  }
  async function hydratePolls(scope){
    for(const card of $$('.note.poll:not([data-poll-done])', scope||document)){
      card.setAttribute('data-poll-done','1');
      const pid=card.dataset.id;
      let votes=[]; try{ votes=await Relay.query([{ kinds:[1018], '#e':[pid], limit:1000 }]); }catch(_){}
      const latest=new Map();
      for(const v of votes.sort((a,b)=>a.created_at-b.created_at)) latest.set(v.pubkey, v);
      const counts={}; let total=0; const mine=_myPollVotes[pid]||new Set();
      for(const v of latest.values()){
        const chosen=[...new Set(v.tags.filter(t=>t[0]==='response').map(t=>t[1]))];
        if(v.pubkey===ME.pubkey) chosen.forEach(o=>mine.add(o));
        chosen.forEach(o=>{ counts[o]=(counts[o]||0)+1; total++; });
      }
      _myPollVotes[pid]=mine;
      card.querySelectorAll('.poll-opt').forEach(b=>{
        const c=counts[b.dataset.opt]||0, pct=total?Math.round(c*100/total):0;
        const bar=b.querySelector('.poll-bar'); if(bar) bar.style.width=pct+'%';
        const pc=b.querySelector('.poll-pct'); if(pc) pc.textContent=pct+'% ('+c+')';
        b.classList.toggle('voted', mine.has(b.dataset.opt));
      });
      const tot=card.querySelector('.poll-total'); if(tot) tot.textContent=total+' vote'+(total===1?'':'s');
    }
  }
  async function votePoll(pollId, optId){
    if((_myPollVotes[pollId]||new Set()).has(optId)){ toast('already voted'); return; }
    try{
      await publish(1018, '', [['e', pollId], ['response', optId]]);
      (_myPollVotes[pollId]=_myPollVotes[pollId]||new Set()).add(optId);
      toast('✓ voted');
      const card=$(`.note.poll[data-id="${pollId}"]`); if(card){ card.removeAttribute('data-poll-done'); hydratePolls(card.parentNode||document); }
    }catch(e){ toast('vote failed'); }
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
  // ---- NIP-30 custom emoji ----------------------------------------------------------------------
  // Fediverse-bridged notes (and reactions) carry ["emoji", shortcode, url] tags; render the actual
  // image in place of the bare ":shortcode:" text (otherwise it reads like inline code). Restricted
  // to shortcodes the event actually declares, so it can't mangle an unrelated ":foo:" in a URL.
  function emojiTagMap(ev){ const m={}; for(const t of ((ev&&ev.tags)||[])){ if(t[0]==='emoji'&&t[1]&&t[2]) m[t[1]]=t[2]; } return m; }
  function applyEmojis(htmlStr, ev){
    const map=emojiTagMap(ev); if(!Object.keys(map).length) return htmlStr;
    // Alternate the regex so it CONSUMES whole HTML tags untouched, then matches a :shortcode: only in
    // text — otherwise a shortcode that appears inside an <a href="…:x:…"> attribute would be replaced
    // mid-tag and corrupt the markup. Shortcode charset allows a trailing @host (remote/federated
    // custom emoji, e.g. :blobcat@host:) to match the server's NIP-30 tags.
    return (htmlStr||'').replace(/<[^>]*>|:([a-zA-Z0-9_+\-]+(?:@[a-zA-Z0-9.\-]+)?):/g,(m,sc)=>
      sc ? (map[sc] ? `<img class="emoji-inline" src="${enc(map[sc])}" alt="${enc(m)}" title="${enc(m)}" loading="lazy">` : m) : m);
  }
  // Display form of a kind-7 reaction's emoji: an <img> for a NIP-30 custom emoji, else escaped text.
  function reactDisp(e){
    let c=(e&&e.content)||''; if(c==='+'||c==='') return '❤️'; if(c==='-') return '👎';
    if(/^:[^:\s]+:$/.test(c)){ const nm=c.slice(1,-1); const t=((e.tags)||[]).find(x=>x[0]==='emoji'&&x[1]===nm&&x[2]);
      if(t) return `<img class="emoji-inline" src="${enc(t[2])}" alt="${enc(c)}" title="${enc(c)}" loading="lazy">`; }
    return enc(c);
  }
  // Inner HTML of the NIP-36 content-warning reveal overlay (used by noteCard's blurred template).
  function _cwRevealInner(reason){ return `🔞 Sensitive content${reason?' — '+enc(reason):''}<span class="cw-show">Show</span>`; }
  // Mark one of YOUR OWN posts NSFW after the fact. Nostr events are immutable, so the only way to
  // get a warning that EVERY client honours (NIP-36) is to re-post: publish an identical copy carrying
  // the content-warning tag, then delete the original. The copy is a NEW event — its engagement
  // (likes/replies/zaps) starts fresh — so this is destructive and confirmed first.
  async function repostWithWarning(id){
    const ev=Store.get(id); if(!ev){ toast('post not loaded'); return; }
    if(ev.pubkey!==ME.pubkey){ toast('you can only do this to your own posts'); return; }
    if(!confirm('Re-post this with an NSFW warning?\n\nNostr posts can’t be edited, so this DELETES the original and publishes a fresh copy with a content-warning that every client blurs. The new post won’t carry over the original’s likes, replies or zaps.')) return;
    const reason=(prompt('Content warning reason (optional):')||'').trim();
    try{
      // Re-use the original content + tags (mentions, reply/quote refs, imeta, hashtags), dropping any
      // existing content-warning, and append ours. Keep the same kind so polls/community posts survive.
      const tags=(ev.tags||[]).filter(t=>t[0]!=='content-warning').map(t=>t.slice());
      tags.push(['content-warning', reason]);
      const { ev:nu }=await publish(ev.kind||1, ev.content||'', tags);
      await publish(5, 'replaced with a content-warning version', [['e', id]]);   // delete the original
      if(nu) Store.saveEvent(nu);
      toast('🔞 re-posted with warning');
      renderView(true);
    }catch(e){ toast('failed: '+((e&&e.message)||e)); }
  }
  function noteCard(ev, prefix=''){
   try{
    const p = profOf(ev.pubkey); needProfile(ev.pubkey);
    const mp = mediaParts(ev.content);
    // Wall-of-text guard: clamp very long posts with a "Show more" toggle so the feed stays scannable.
    const bodyTxt = stripQuoteRef(mp.text, ev);
    const longTxt = !!bodyTxt && (bodyTxt.length > 480 || (bodyTxt.match(/\n/g)||[]).length > 10);
    const name = p.name||p.display_name||(NT().nip19.npubEncode(ev.pubkey).slice(0,12)+'…');
    const av = p.picture || LOGO;
    const handle = niceNip05(p.nip05) || ('@'+NT().nip19.npubEncode(ev.pubkey).slice(4,12));
    const counts = countsFor(ev.id);
    const liked = myReaction(ev.id);
    const mine = ev.pubkey===ME.pubkey;
    // NIP-36 content warning: blur the body + media behind a reveal button.
    const cwTag = ev.tags.find(t=>t[0]==='content-warning');
    const cw = BLUR_NSFW && (!!cwTag || isSensitive(ev));   // content-warning OR #nsfw tag; honour the toggle
    const cwReason = cwTag ? String(cwTag[1]||'').trim() : (cw ? 'NSFW' : '');
    return `<article class="note" data-id="${ev.id}" data-pk="${ev.pubkey}">
      <img class="av" src="${enc(av)}" onerror="this.src='${LOGO}'">
      <div class="body">${prefix}
        <div class="hd"><span class="name" data-prof="${ev.pubkey}">${enc(name)}</span><span class="vchk"></span>
          <span class="handle">${enc(handle)}</span><span class="time">${timeAgo(ev.created_at)}</span>${PINNED.has(ev.id)?'<span class="pin-badge" title="Pinned to your profile">📌</span>':''}</div>
        ${cw?`<div class="cw-wrap cw-on"><div class="cw-reveal" onclick="event.stopPropagation();var w=this.parentElement;w.classList.remove('cw-on');this.remove();">${_cwRevealInner(cwReason)}</div><div class="cw-inner">`:''}
        <div class="txt${longTxt?' clamp':''}">${applyEmojis(linkify(bodyTxt), ev)}</div>
        ${longTxt?`<button class="txt-more" onclick="event.stopPropagation();var t=this.previousElementSibling;t.classList.toggle('clamp');this.textContent=t.classList.contains('clamp')?'Show more ↓':'Show less ↑';">Show more ↓</button>`:''}
        ${mp.gallery}
        ${linkCardHtml(mp.text)}
        ${quoteHtml(ev)}
        ${cw?`</div></div>`:''}
        <div class="acts">
          <button class="act" data-a="reply" title="reply">${REPLY_ICON} <span class="n">${counts.replies?fmtSats(counts.replies):''}</span></button>
          <button class="act rt ${counts.iRt?'on':''}" data-a="repost" title="repost">${RT_ICON} <span class="n">${counts.reposts?fmtSats(counts.reposts):''}</span></button>
          <button class="act actq" data-a="quote" title="quote post">${QUOTE_ICON}</button>
          <button class="act ${liked?'on':''}" data-a="react" title="react">${liked||'😀'} <span class="n">${counts.reactions?fmtSats(counts.reactions):''}</span></button>
          <button class="act actz ${counts.zaps?'on':''}" data-a="zap" title="zap (lightning)">⚡ <span class="n">${counts.zaps?fmtSats(counts.zaps):''}</span></button>
          ${isXmrAddr(xmrOf(p))?`<button class="act actxmr" data-a="xmrtip" title="tip Monero (XMR)">ɱ</button>`:''}
          <button class="act actm ${BOOKMARKS.has(ev.id)?'on':''}" data-a="menu" title="more">☰</button>
        </div>
      </div></article>`;
   }catch(e){ return `<article class="note" data-id="${(ev&&ev.id)||''}" data-pk="${(ev&&ev.pubkey)||''}"><div class="body"><div class="txt muted small">⚠ couldn't render this post</div></div></article>`; }
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
    if(kind===30023) openArticle(ev); else if(kind===30402) openListing(ev); else renderThread(ev.id);
  }
  function quoteHtml(ev){
    const q=(ev.tags.find(t=>t[0]==='q')||[])[1]; if(!q) return '';
    const mc=q.match(/^(\d+):([0-9a-f]{64}):(.*)$/i);   // addressable quote: kind:pubkey:dtag (NIP-18/22)
    if(mc){ const key=`${+mc[1]}:${mc[2]}:${mc[3]}`; const c=_adCache.get(key);
      if(c) return addrDiv(c);
      needAddr(+mc[1], mc[2], mc[3]);
      return `<div class="quoted muted small" data-naload="${enc(key)}">📄 quoted post loading…</div>`; }
    if(!/^[0-9a-f]{64}$/i.test(q)) return '';            // not a valid event ref → don't render junk
    const o=Store.get(q); if(!o){ needEvent(q); return `<div class="quoted muted small" data-qload="${enc(q)}">quoted post loading…</div>`; }
    return quotedDiv(o);
  }
  function quotedDiv(o){ const p=profOf(o.pubkey); needProfile(o.pubkey);
    const name = p.name||p.display_name||(NT().nip19.npubEncode(o.pubkey).slice(0,12)+'…');
    const av = p.picture || LOGO;
    const handle = niceNip05(p.nip05) || ('@'+NT().nip19.npubEncode(o.pubkey).slice(4,12));
    const mp = mediaParts(o.content);
    return `<div class="quoted" data-open="${o.id}">
      <div class="hd"><img class="qav" src="${enc(av)}" onerror="this.src='${LOGO}'"><span class="name" data-prof="${o.pubkey}">${enc(name)}</span><span class="vchk" data-pk="${o.pubkey}"></span><span class="handle">${enc(handle)}</span><span class="time">${timeAgo(o.created_at)}</span></div>
      <div class="txt">${applyEmojis(linkify(stripQuoteRef(mp.text, o)), o)}</div>
      ${mp.gallery}</div>`; }
  // NIP-10 parent of a reply: the explicit `reply` marker, else `root`, else the last e-tag.
  function replyParentId(ev){
    const es=(ev.tags||[]).filter(t=>t[0]==='e'&&t[1]);
    const t=es.find(t=>t[3]==='reply')||es.find(t=>t[3]==='root')||es[es.length-1];
    return t?t[1]:null;
  }
  // "↩ replying to" context block shown ABOVE a reply (e.g. on a profile's Replies tab) so you can
  // see what's being answered. Reuses quotedDiv + the data-qload fetch/patch path for the parent.
  function replyContextHtml(ev){
    const pid=replyParentId(ev); if(!pid) return '';
    const o=Store.get(pid);
    const inner=o?quotedDiv(o):(needEvent(pid),`<div class="quoted muted small" data-qload="${pid}">post loading…</div>`);
    return `<div class="reply-ctx"><span class="reply-ctx-lbl">↩ replying to</span>${inner}</div>`;
  }
  const _evQ=new Set(); let _evT=null;
  function needEvent(id){ if(id&&/^[0-9a-f]{64}$/i.test(id)&&!Store.get(id)){ _evQ.add(id); if(!_evT)_evT=setTimeout(flushEvents,150);} }
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
      const div=document.createElement('div'); div.innerHTML=noteCard(e, `<div class="repost-tag">${RT_ICON} ${enc(el.dataset.reposter||'someone')} reposted</div>`);
      if(div.firstElementChild) el.replaceWith(div.firstElementChild);
    });
    $$(`[data-qload="${e.id}"]`).forEach(el=>{
      const div=document.createElement('div'); div.innerHTML=quotedDiv(e);
      if(div.firstElementChild) el.replaceWith(div.firstElementChild);
    });
  }

  // ---- addressable (naddr) embeds: fetch the article/event by its coordinate + render a preview ----
  const _adCache=new Map(); const _adQ=new Map(); let _adT=null;   // "kind:pubkey:dtag" -> event
  function needAddr(kind, pubkey, d){
    const key=`${kind}:${pubkey}:${d}`;
    if(_adCache.has(key)) return; _adQ.set(key, {kind, pubkey, d, key});
    if(!_adT) _adT=setTimeout(flushAddrs, 150);
  }
  async function flushAddrs(){
    _adT=null; const items=[..._adQ.values()]; _adQ.clear();
    for(const it of items){
      try{
        const evs=await Relay.query([{ kinds:[it.kind], authors:[it.pubkey], '#d':[it.d], limit:1 }]);
        const e=(evs||[]).sort((a,b)=>(b.created_at||0)-(a.created_at||0))[0];
        if(e){ Store.saveEvent(e); _adCache.set(it.key, e); needProfile(e.pubkey);
          $$('[data-naload]').forEach(el=>{ if(el.dataset.naload!==it.key) return;
            const div=document.createElement('div'); div.innerHTML=addrDiv(e);
            if(div.firstElementChild) el.replaceWith(div.firstElementChild); }); }
      }catch(_){}
    }
    decorateProfiles();
  }
  // Preview card for an addressable event (NIP-23 article etc.) — clickable via the .naddrlink handler.
  function addrDiv(e){
    const p=profOf(e.pubkey); needProfile(e.pubkey);
    const d=(e.tags.find(t=>t[0]==='d')||[])[1]||'';
    const title=(e.tags.find(t=>t[0]==='title')||[])[1]||'(untitled)';
    const summary=(e.tags.find(t=>t[0]==='summary')||[])[1]||'';
    const img=(e.tags.find(t=>t[0]==='image')||[])[1]||'';
    const name=p.name||p.display_name||(NT().nip19.npubEncode(e.pubkey).slice(0,12)+'…');
    return `<div class="quoted naddrlink" data-pk="${enc(e.pubkey)}" data-d="${enc(d)}" data-k="${enc(String(e.kind))}">
      ${img?`<img class="m" src="${enc(img)}" loading="lazy">`:''}
      <div class="hd"><span class="name">📄 ${e.kind===30023?'Article':'Post'} · ${enc(name)}</span></div>
      <div class="txt"><b>${enc(title)}</b>${summary?`<br><span class="muted small">${enc(summary)}</span>`:''}</div></div>`;
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
      const po=e.target.closest('.poll-opt'); if(po && !po.disabled){ e.preventDefault(); votePoll(po.dataset.poll, po.dataset.opt); return; }
      const na=e.target.closest('.naddrlink'); if(na){ e.preventDefault(); openNaddr(na.dataset.pk, na.dataset.d, na.dataset.k); return; }
      // Files grid: thumbnails load ?thumb=1, so open the parent link's FULL url in the lightbox
      // (images) — videos/docs fall through to their <a> (new tab / download).
      const fa=e.target.closest('.file-card a'); if(fa){ const fm=fa.dataset.mime||'';
        if(/^video\//.test(fm)){ e.preventDefault(); openLightbox(fa.getAttribute('href'), 'video'); }
        else if(/^audio\//.test(fm)){ e.preventDefault(); openLightbox(fa.getAttribute('href'), 'audio'); }
        else if(/^image\//.test(fm) || fa.querySelector('img')){ e.preventDefault(); openLightbox(fa.getAttribute('href')); }
        return; }   // docs: fall through to the link (download / new tab)
      const im=e.target.closest('.txt img, .note-preview img, .media-row img, .media-grid img'); if(im){ e.preventDefault(); openLightbox(im.currentSrc||im.src); return; }
      const av=e.target.closest('.av'); if(av){ const n=e.target.closest('.note'); if(n){ renderProfileView(n.dataset.pk); return; } }
      const prof=e.target.closest('[data-prof]'); if(prof){ renderProfileView(prof.dataset.prof); return; }
      const q=e.target.closest('[data-open]'); if(q){ openThread(q.dataset.open); return; }
      // Article cards (kind-30023) that appear inline in the timeline: open the reader (or the author's
      // profile when the name is tapped). Mirrors the Articles-list handler; the .note path below
      // doesn't match them.
      const artc=e.target.closest('.article-card'); if(artc){ if(e.target.closest('[data-prof]')){ renderProfileView(artc.dataset.pk); return; } const a=Store.get(artc.dataset.id); if(a) openArticle(a); return; }
      // Community / channel discovery cards surfaced in the feed → open the community / channel.
      const cc=e.target.closest('.community-card,.channel-card'); if(cc){ if(e.target.closest('[data-prof]')){ renderProfileView(cc.dataset.pk); return; } const x=Store.get(cc.dataset.id); if(x){ cc.classList.contains('community-card')?openCommunity(x):openChannel(x); } return; }
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
      if(a==='xmrtip') return doXmrTip(id,pk);
      if(a==='bookmark') return toggleBookmark(id,btn);
      if(a==='copyid'){ try{ navigator.clipboard.writeText(_webLink(NT().nip19.neventEncode({id}))); toast('link copied'); }catch(_){ try{ navigator.clipboard.writeText(_webLink(NT().nip19.noteEncode(id))); toast('link copied'); }catch(__){ navigator.clipboard.writeText(id); toast('id copied'); } } return; }
      if(a==='translate') return translatePost(id);
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
  // ---------- NIP-47 Nostr Wallet Connect — one-tap zaps ----------
  // Paste a nostr+walletconnect:// string in Settings (from Alby / Primal / Coinos / …). Zap
  // invoices are then paid through it automatically (kind-23194 pay_invoice → 23195 response,
  // NIP-04 encrypted with the wallet CONNECTION key — never your nostr key). No invoice popups.
  const Nwc = {
    parse(uri){
      const m=String(uri||'').trim().match(/^nostr\+walletconnect:\/\/([0-9a-f]{64})\??(.*)$/i);
      if(!m) return null;
      const qs=new URLSearchParams(m[2]||''); const relay=qs.getAll('relay')[0]; const secret=(qs.get('secret')||'').toLowerCase();
      if(!relay || !/^[0-9a-f]{64}$/.test(secret)) return null;
      return { walletPk:m[1].toLowerCase(), relay, secret };
    },
    configured(){ return !!this.parse(ClientSettings.get('nwc','')); },
    _hex(h){ const a=new Uint8Array(h.length/2); for(let i=0;i<a.length;i++) a[i]=parseInt(h.substr(i*2,2),16); return a; },
    async payInvoice(bolt11){
      const cfg=this.parse(ClientSettings.get('nwc','')); if(!cfg) throw new Error('no wallet connected');
      const N=NT(); const sk=this._hex(cfg.secret); const myPk=N.getPublicKey(sk);
      const content=await N.nip04.encrypt(sk, cfg.walletPk, JSON.stringify({ method:'pay_invoice', params:{ invoice:bolt11 } }));
      const ev=N.finalizeEvent({ kind:23194, content, tags:[['p',cfg.walletPk]], created_at:Math.floor(Date.now()/1000) }, sk);
      return await new Promise((res,rej)=>{
        let done=false; const ws=new WebSocket(cfg.relay); const sub='nwc'+Math.random().toString(36).slice(2,8);
        const fin=(fn,a)=>{ if(done) return; done=true; try{ ws.close(); }catch(_){} fn(a); };
        // Filter by author + our #p (robust: 23195 is ephemeral and not every relay indexes #e on a
        // live sub); then confirm the e-tag matches OUR request id in code.
        ws.onopen=()=>{ ws.send(JSON.stringify(['REQ',sub,{ kinds:[23195], authors:[cfg.walletPk], '#p':[myPk], since:Math.floor(Date.now()/1000)-5 }]));
          ws.send(JSON.stringify(['EVENT', ev])); };
        ws.onmessage=async(e)=>{ let m; try{ m=JSON.parse(e.data); }catch(_){ return; }
          if(m[0]!=='EVENT' || m[1]!==sub) return; const r=m[2]; if(!r || r.kind!==23195 || r.pubkey!==cfg.walletPk) return;
          if(!(r.tags||[]).some(t=>t[0]==='e' && t[1]===ev.id)) return;   // the response to THIS request
          try{ const j=JSON.parse(await N.nip04.decrypt(sk, cfg.walletPk, r.content));
            if(j && j.error) return fin(rej, new Error((j.error.message)||(j.error.code)||'wallet declined'));
            fin(res, (j&&j.result)||{}); }catch(err){ fin(rej, err); } };
        ws.onerror=()=>fin(rej, new Error('cannot reach the wallet relay'));
        setTimeout(()=>fin(rej, new Error('wallet timed out — is it online?')), 45000);
      });
    },
  };
  async function doZap(noteId, pk){
    const p=profOf(pk); const addr=p.lud16||p.lud06;
    if(!addr){ toast('no lightning address on this profile'); return; }
    const presets=[21,100,500,1000,5000];
    modal(`<h3>⚡ Zap ${enc(p.name||p.display_name||'')}</h3>
      <div class="zap-presets">${presets.map(a=>`<button class="zap-amt" data-amt="${a}">${a>=1000?(a/1000)+'k':a} sats</button>`).join('')}</div>
      <div class="row" style="gap:8px;margin-top:10px"><input class="input" id="zap-custom" type="number" min="1" placeholder="custom amount (sats)"><button class="btn btn-neon small" id="zap-go">⚡ Zap</button></div>`,
      root=>{
        $$('.zap-amt',root).forEach(b=> b.onclick=()=>{ closeModal(); _runZap(noteId, pk, +b.dataset.amt); });
        $('#zap-go',root).onclick=()=>{ const v=parseInt(($('#zap-custom',root)||{}).value||'0',10); if(v>0){ closeModal(); _runZap(noteId, pk, v); } else toast('enter an amount'); };
        const ci=$('#zap-custom',root); if(ci) ci.addEventListener('keydown',e=>{ if(e.key==='Enter') $('#zap-go',root).click(); });
      });
  }
  async function _runZap(noteId, pk, amt){
    const p=profOf(pk); const addr=p.lud16||p.lud06;
    if(!addr || !amt || amt<1) return;
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
      // 1) an installed WebLN extension (Alby etc.) — the most direct one-click path → 2) a
      // configured NWC wallet (great when there's no extension, e.g. on mobile) → 3) show the invoice.
      if(window.webln){ try{ await window.webln.enable(); await window.webln.sendPayment(pr); toast('⚡ zapped '+amt+' sats'); return; }catch(e){} }
      if(Nwc.configured()){ try{ toast('paying via your wallet…'); await Nwc.payInvoice(pr); toast('⚡ zapped '+amt+' sats'); return; }
        catch(e){ toast('wallet: '+((e&&e.message)||e)); } }
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
  // ---------- Monero tips (non-custodial) ----------
  // Mirrors the zap UX but there's no LNURL/invoice: the recipient publishes an XMR address in their
  // kind-0, and the SENDER pays from their own wallet (scan the QR, or the monero: deeplink opens a
  // wallet app prefilled). Nothing touches this server except the QR render (segno, /client/qr —
  // stays on the box, no third-party leak). On confirmation we post a public kind-1 tip note (the
  // chosen "always post" behaviour) crediting the recipient — there's no cryptographic receipt for XMR.
  async function doXmrTip(noteId, pk){
    const p=profOf(pk); const addr=xmrOf(p);
    if(!isXmrAddr(addr)){ toast('no Monero address on this profile'); return; }
    const name=enc(p.name||p.display_name||'anon');
    const uri=a=>'monero:'+addr+(a?('?tx_amount='+encodeURIComponent(a)):'');
    modal(`<h3>ɱ Tip ${name} · Monero</h3>
      <p class="muted small">Send XMR from your own wallet — scan the QR or open your wallet app. Non-custodial: nothing touches this server.</p>
      <div class="row" style="gap:8px;margin:8px 0"><input class="input" id="xmr-amt" type="number" min="0" step="0.0001" placeholder="amount (XMR) — optional"><a class="btn btn-neon small" id="xmr-open" href="${uri('')}">📲 Open wallet</a></div>
      <div class="xmr-qr" id="xmr-qr"><div class="muted small">generating QR…</div></div>
      <div class="keybox" style="margin-top:8px"><code id="xmr-addr">${enc(addr)}</code></div>
      <div class="row" style="gap:8px;margin-top:8px"><button class="btn btn-cyan small" id="xmr-copy">Copy address</button><span class="spacer"></span>${GUEST?'':`<button class="btn btn-neon small" id="xmr-sent" title="post a public tip note crediting them">✓ I sent it</button>`}</div>`,
      root=>{
        const amtEl=$('#xmr-amt',root), qrBox=$('#xmr-qr',root), openBtn=$('#xmr-open',root); let _u=null, _t=null;
        const amtVal=()=>{ const n=parseFloat(amtEl.value); return (isFinite(n)&&n>0)?String(n):''; };   // omit blank / 0 / NaN — never tx_amount=0
        const qrFail='<div class="muted small">QR unavailable — scan or copy the address below.</div>';
        const renderQr=()=>{ qrBox.innerHTML='<div class="muted small">generating QR…</div>';
          fetch('/client/qr',{method:'POST',headers:{'Content-Type':'text/plain'},body:uri(amtVal())})
            .then(r=>r.ok?r.blob():null).then(b=>{ if(!b){ qrBox.innerHTML=qrFail; return; }
              if(_u) URL.revokeObjectURL(_u); _u=URL.createObjectURL(b); qrBox.innerHTML=`<img alt="Monero tip QR" src="${_u}">`; })
            .catch(()=>{ qrBox.innerHTML=qrFail; }); };
        const sync=()=>{ openBtn.href=uri(amtVal()); };   // deeplink tracks the amount IMMEDIATELY (QR fetch is debounced, the href is not)
        sync(); renderQr();
        amtEl.addEventListener('input',()=>{ sync(); clearTimeout(_t); _t=setTimeout(renderQr,400); });
        $('#xmr-copy',root).onclick=()=>{ try{ navigator.clipboard.writeText(addr).then(()=>toast('address copied'),()=>prompt('Copy the Monero address:',addr)); }catch(_){ prompt('Copy the Monero address:',addr); } };
        { const s=$('#xmr-sent',root); if(s) s.onclick=()=>{ const a=amtVal(); closeModal(); _postXmrTipNote(noteId, pk, a); }; }
      });
  }
  async function _postXmrTipNote(noteId, pk, amt){
    try{
      const who='nostr:'+NT().nip19.npubEncode(pk);
      const body=`ɱ Tipped${amt?(' '+amt+' XMR'):''} ${who} via Monero`;
      const tags=[['p',pk],['t','monerotip']].concat(noteId?[['e',noteId]]:[]).concat(amt?[['amount_xmr',String(amt)]]:[]);
      await publish(1, body, tags); toast('ɱ tip note posted');
    }catch(e){ toast('could not post tip note'); }
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
  // Place a popover. On phones it becomes a full-width bottom action-sheet with a dim backdrop
  // (always attached + readable — a wide menu can't sensibly hang off a tiny right-edge button on a
  // narrow screen). On desktop it's an anchored dropdown: below the button if it fits, else flipped
  // above; left-aligned, or right-aligned when the button sits near the right edge.
  function _placePop(pop, anchorBtn){
    // ONE consistent rule for every menu (timeline ☰, profile ☰, compose Attach/React/Translate,
    // emoji): without the desktop right column (<1180px) a right-edge button's menu would spill
    // ~1-2" to the left, so ALL menus become a centered bottom action-sheet there. At >=1180px the
    // layout has room, so they're anchored dropdowns under their button.
    if(window.matchMedia('(max-width:1179px)').matches){
      document.querySelectorAll('.pop-backdrop').forEach(b=>b.remove());
      const bd=document.createElement('div'); bd.className='pop-backdrop';
      document.documentElement.appendChild(bd);
      pop.classList.add('sheet');   // CSS pins it to the bottom as a centered card
      return;
    }
    const M=8, vw=window.innerWidth, vh=window.innerHeight;
    const r=(anchorBtn||document.body).getBoundingClientRect();
    const pw=pop.offsetWidth, ph=pop.offsetHeight;   // ph already respects the popover's CSS max-height
    // Horizontal: drop straight under the button (left edges aligned, like a normal dropdown). If
    // that would run off the right edge (a button near the right side), right-align it to the button
    // instead so it stays attached. Always clamp on-screen.
    let left = r.left;
    if(left + pw > vw - M) left = r.right - pw;
    left = Math.max(M, Math.min(left, vw-M-pw));
    // Vertical: just below the button; if it would overflow the bottom, flip above (bottom hugs the
    // button) when there's room, otherwise pin within the viewport. Keeps it attached to the button.
    let top = r.bottom + 6;
    if(top + ph > vh - M){
      const above = r.top - 6 - ph;
      top = (above >= M) ? above : Math.max(M, vh - M - ph);
    }
    pop.style.left=left+'px'; pop.style.top=top+'px';
    // BULLETPROOF on-screen guarantee: re-measure the menu's REAL rendered rect (the earlier offsetWidth
    // can be wrong/stale for long items, so a right-edge ☰ menu hung off the right and taps fell through
    // to the Zap button behind it) and shove it fully inside the viewport. getBoundingClientRect forces a
    // synchronous layout, so this corrects before the first paint — no visible jump.
    {
      const b=pop.getBoundingClientRect();
      if(b.right > vw - M) left -= (b.right - (vw - M));
      if(left < M) left = M;
      if(b.bottom > vh - M) top -= (b.bottom - (vh - M));
      if(top < M) top = M;
      pop.style.left=left+'px'; pop.style.top=top+'px';
    }
  }
  function openEmojiPopover(anchorBtn, onPick){
    document.querySelectorAll('.emoji-pop,.pop-backdrop').forEach(p=>p.remove());   // never stack pickers
    const pop=document.createElement('div'); pop.className='emoji-pop';
    pop.innerHTML=REACTION_EMOJIS.map(x=>`<button data-e="${x}">${x}</button>`).join('');
    document.documentElement.appendChild(pop);   // <html>, not <body>: body has zoom:.85 on desktop,
    _placePop(pop, anchorBtn);                    // which throws off fixed-position math for a body child
    const close=()=>{ pop.remove(); document.querySelectorAll('.pop-backdrop').forEach(b=>b.remove()); document.removeEventListener('click',onDoc,true); const f=$('#feed'); if(f) f.removeEventListener('scroll',close); };
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
    document.querySelectorAll('.menu-pop,.emoji-pop,.pop-backdrop').forEach(p=>p.remove());   // never stack popovers
    const pop=document.createElement('div'); pop.className='menu-pop';
    pop.innerHTML=items.map(([a,label,cls])=>`<button data-m="${a}"${cls?` class="${cls}"`:''}>${enc(label)}</button>`).join('');
    document.documentElement.appendChild(pop);   // <html>, not <body>: body has zoom:.85 on desktop,
    _placePop(pop, anchorBtn);                    // which throws off fixed-position math for a body child
    const close=()=>{ pop.remove(); document.querySelectorAll('.pop-backdrop').forEach(b=>b.remove()); document.removeEventListener('click',onDoc,true); const f=$('#feed'); if(f) f.removeEventListener('scroll',close); };
    const onDoc=e=>{ if(!pop.contains(e.target) && !anchorBtn.contains(e.target)) close(); };
    setTimeout(()=>{ document.addEventListener('click',onDoc,true); const f=$('#feed'); if(f) f.addEventListener('scroll',close,{once:true}); },0);
    $$('[data-m]',pop).forEach(b=> b.onclick=()=>{ close(); onPick(b.dataset.m); });
    return close;
  }
  // Translate the DRAFT in a compose box into a chosen language (reply / quote / new post all use
  // compose(), so this covers all three). Pops a language picker, then replaces the draft text.
  async function composeTranslate(ta, btn){
    const text=(ta.value||'').trim();
    if(!text){ toast('write something first'); return; }
    // Thai kept near the top so it's visible without scrolling the picker (the popover caps at ~12
    // rows before it scrolls — Thai lower in the list read as "missing").
    const langs=['English','Thai','Chinese','Spanish','French','German','Italian','Portuguese','Tagalog','Cebuano','Swahili','Japanese','Korean','Hindi','Arabic','Russian','Indonesian'];
    const items=langs.map(n=>[n,'🌐 '+n]).concat([['__other','✏️ Other…']]);
    openMenuPopover(btn, items, async name=>{
      let to=name;
      if(name==='__other'){ to=(prompt('Translate to which language?')||'').trim(); if(!to) return; }
      const old=ta.value; ta.value='translating…'; ta.disabled=true;
      try{
        const r=await fetch('/client/translate',{ method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({ text, to }) });
        const j=await r.json().catch(()=>({}));
        ta.disabled=false;
        if(r.ok && j.text){
          if(j.text.trim()===text.trim()){ ta.value=old; toast('no change — already '+to+'? (or just sounds/emoji)'); }
          else { ta.value=j.text; ta.focus(); ta.dispatchEvent(new Event('input')); toast('translated → '+to); }
        } else { ta.value=old; toast(j.error||'translation unavailable'); }
      }catch(e){ ta.disabled=false; ta.value=old; toast('translate failed'); }
    });
  }
  // the per-post "☰ more" menu — holds the secondary actions (bookmark / copy id / pin / delete /
  // block) so the action row stays a clean 5 across.
  // 📡 Rebroadcast: re-publish the full signed event to the built-in relay, which fans it back out to
  // the upstream public relays (the relay's paced outbox) — useful to re-propagate a post that didn't
  // reach the wider network. Works for any post; the event must be the complete signed event.
  async function rebroadcastPost(id){
    let ev=Store.get(id); if(!ev){ ev=await fetchEvent(id); if(ev) Store.saveEvent(ev); }
    if(!ev || !ev.sig){ toast('post not loaded'); return; }
    toast('📡 rebroadcasting…');
    try{ const r=await Relay.publish(ev); toast(r&&r.ok ? '📡 rebroadcast to relays' : ('relay: '+((r&&r.msg)||'rejected'))); }
    catch(_){ toast('rebroadcast failed'); }
  }
  function openPostMenu(id, pk, art, anchorBtn){
    const mine = pk===ME.pubkey;
    const items=[['bookmark', BOOKMARKS.has(id)?'🔖 Remove bookmark':'🔖 Bookmark'], ['copyid','🔗 Copy link']];
    if(mine) items.push(['delete','🗑️ Delete','danger']);   // near the top so it's reachable on a crowded menu
    if(!window.PC_NOSTR_ONLY) items.push(['translate','🌐 Translate']);   // uses the node's AI backend
    if(!window.PC_NOSTR_ONLY) items.push(['summary','📝 Summary']);       // AI summary of the post/thread
    if(!window.PC_NOSTR_ONLY) items.push(['narrate','🔊 Read Aloud']);    // TTS the post (author + content)
    if(!window.PC_NOSTR_ONLY) items.push(['effect','🎬 Effect']);         // apply an effect to the post's image
    if(!window.PC_NOSTR_ONLY) items.push(['screenshot','📸 Screenshot']); // render the post as a clean card → Blossom link
    if(mine) items.push(['pin', PINNED.has(id)?'📌 Unpin from profile':'📌 Pin to profile']);
    if(mine){ const ev=Store.get(id); const tagged=!!(ev && ev.tags.some(t=>t[0]==='content-warning'));
      // Only offer it when the post isn't already warned (a re-posted copy already carries the tag).
      if(!tagged) items.push(['nsfw','🔞 Re-post with NSFW warning']); }
    if(mine) items.push(['rebroadcast','📡 Rebroadcast to relays']);   // re-propagate your own post (moved down)
    if(!mine) items.push(['mute', MUTED.has(pk)?'🔊 Unmute author':'🔇 Mute author']);   // personal NIP-51 mute (any user)
    if(IS_ADMIN && !mine) items.push(['block','🚫 Block author','danger']);
    openMenuPopover(anchorBtn, items, a=>{
      if(a==='bookmark'){ toggleBookmark(id, null).then(()=>{ if(anchorBtn) anchorBtn.classList.toggle('on', BOOKMARKS.has(id)); }); return; }
      if(a==='copyid'){ try{ navigator.clipboard.writeText(_webLink(NT().nip19.neventEncode({id}))); toast('link copied'); }catch(_){ try{ navigator.clipboard.writeText(_webLink(NT().nip19.noteEncode(id))); toast('link copied'); }catch(__){ navigator.clipboard.writeText(id); toast('id copied'); } } return; }
      if(a==='rebroadcast') return rebroadcastPost(id);
      if(a==='translate') return translatePost(id);
      if(a==='summary') return summarizePost(id);
      if(a==='narrate') return narratePost(id, pk);
      if(a==='effect') return effectPost(id, pk);
      if(a==='screenshot') return screenshotPost(id);
      if(a==='pin') return togglePin(id);
      if(a==='nsfw') return repostWithWarning(id);
      if(a==='delete') return doDelete(id, art);
      if(a==='mute') return toggleMute(pk);
      if(a==='block') return doBlock(pk);
    });
  }
  // 📸 Screenshot: render the post as a clean tweet-style CARD (just the post, like the Nitter cards)
  // server-side from the note's own fields — reliable + instance-branded, no live-SPA capture/timing.
  // The card PNG is uploaded to Blossom and its link copied (image-on-clipboard is unreliable after a
  // multi-second async op; a text link copies fine).
  // Clean plain text for the screenshot card: a flat card can't render Nostr embeds, so resolve
  // nostr: mentions to @names and replace quote/embed refs (nevent/note) with the quoted post's
  // text when we have it cached (else strip the raw bech32 — the gibberish token looked broken).
  function _cardText(ev){
    let t=(mediaParts(ev.content).text||ev.content||'');
    t=t.replace(/nostr:(npub1[0-9a-z]+|nprofile1[0-9a-z]+)/gi,(m,b)=>{
      try{ const d=NT().nip19.decode(b); const pk=d.type==='npub'?d.data:(d.data&&d.data.pubkey);
        if(pk){ const pr=profOf(pk); const nm=pr&&(pr.name||pr.display_name); return '@'+(nm||(NT().nip19.npubEncode(pk).slice(4,12)+'…')); } }catch(_){}
      return '';
    });
    t=t.replace(/\s*nostr:(nevent1[0-9a-z]+|note1[0-9a-z]+)/gi,(m,b)=>{
      try{ const d=NT().nip19.decode(b); const eid=d.type==='note'?d.data:(d.data&&d.data.id); const o=eid&&Store.get(eid);
        if(o){ const op=profOf(o.pubkey); const onm=(op&&(op.name||op.display_name))||'anon';
          const ot=(mediaParts(o.content).text||o.content||'').replace(/nostr:[0-9a-z]+/gi,'').replace(/\s+/g,' ').trim().slice(0,160);
          return `\n\n↩ ${onm}: “${ot}”`; } }catch(_){}
      return '';
    });
    t=t.replace(/\s*nostr:naddr1[0-9a-z]+/gi,'');
    return t.replace(/\n{3,}/g,'\n\n').trim();
  }
  async function screenshotPost(id){
    let ev=Store.get(id); if(!ev){ ev=await fetchEvent(id); if(ev) Store.saveEvent(ev); }
    if(!ev){ toast('post not loaded'); return; }
    const p=profOf(ev.pubkey);
    const name=p.name||p.display_name||'';
    const handle=niceNip05(p.nip05)||('@'+NT().nip19.npubEncode(ev.pubkey).slice(4,12)+'…');
    const text=_cardText(ev);
    let timestamp=''; try{ timestamp=new Date(ev.created_at*1000).toLocaleDateString(undefined,{year:'numeric',month:'short',day:'numeric'}); }catch(_){}
    toast('📸 rendering…');
    try{
      // Pass the avatar + first-image URLs; the SERVER fetches them (the client can't read most of
      // them as bytes — cross-origin CORS — which is why the card was missing the avatar).
      const r=await fetch('/client/screenshot',{ method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({
        name, handle, text, timestamp, avatar_url: p.picture||'', image_url: postImageUrl(ev)||'' }) });
      const j=await r.json().catch(()=>({}));
      if(!r.ok || !j.image){ toast('screenshot failed: '+(j.error||('http '+r.status))); return; }
      const bin=Uint8Array.from(atob(j.image), c=>c.charCodeAt(0));
      const file=new File([bin], 'post.png', { type:'image/png' });
      toast('📤 uploading…');
      const link=await uploadBlob(file);
      try{ await navigator.clipboard.writeText(link); }catch(_){}
      modal(`<h3>📸 Post card</h3><img src="${enc(link)}" style="max-width:100%;max-height:54vh;border-radius:10px;display:block;margin:0 auto">`+
        `<div class="muted small" style="margin-top:10px;word-break:break-all">${enc(link)}</div>`+
        `<div class="row" style="justify-content:center;gap:8px;margin-top:12px"><button class="btn btn-neon small" id="ss-copy">📋 Copy link</button><a class="btn btn-ghost small" href="${enc(link)}" target="_blank" rel="noopener">↗ Open</a><button class="btn btn-ghost small" id="ss-close">Close</button></div>`,
        root=>{
          const cp=root.querySelector('#ss-copy'); if(cp) cp.onclick=async()=>{ try{ await navigator.clipboard.writeText(link); toast('📋 link copied'); }catch(_){ toast(link); } };
          const cl=root.querySelector('#ss-close'); if(cl) cl.onclick=closeModal;
        });
      toast('📸 card ready — link copied');
    }catch(e){
      if(typeof _blossomDenied==='function' && _blossomDenied(e)){ requestBlossomAccess(); toast('🔒 No upload access — requested it from the admin.'); }
      else toast('screenshot failed: '+((e&&e.message)||e));
    }
  }
  // Translate a post in-place via the node's AI backend. Only edits the DOM (the stored event is
  // untouched), so switching views / refreshing restores the original — exactly as asked.
  async function translatePost(id){
    const ev=Store.get(id); if(!ev){ toast('post not loaded'); return; }
    const src=(mediaParts(ev.content).text || ev.content || '').trim();
    if(!src){ toast('nothing to translate'); return; }
    const nodes=$$('.note[data-id="'+id+'"] > .body > .txt');
    if(!nodes.length){ toast('open the timeline to translate this'); return; }
    nodes.forEach(n=>{ if(!n.dataset.orig) n.dataset.orig=n.innerHTML; n.style.opacity='.5'; });
    try{
      const r=await fetch('/client/translate',{ method:'POST', headers:{'Content-Type':'application/json'},
        body:JSON.stringify({ text:src, to:(navigator.language||'en') }) });
      const j=await r.json().catch(()=>({}));
      if(!r.ok || !j.text){ toast(j.error||'translation unavailable'); nodes.forEach(n=>n.style.opacity=''); return; }
      if(j.text.trim()===src.trim()){ nodes.forEach(n=>n.style.opacity=''); toast('nothing to translate — looks already in your language (or just sounds/emoji)'); return; }
      nodes.forEach(n=>{ n.style.opacity='';
        n.innerHTML=linkify(j.text)+'<div class="muted small tr-tag">🌐 translated · refresh to restore</div>'; });
    }catch(_){ toast('translate failed'); nodes.forEach(n=>n.style.opacity=''); }
  }
  // 🔊 Read Aloud — narrate the post via the node's built-in TTS: author name, then the content.
  // URLs, hashtags and attachments are stripped (mediaParts removes media; regex drops links/tags).
  let _narrateAudio=null;
  async function narratePost(id, pk){
    let ev=Store.get(id); if(!ev){ ev=await fetchEvent(id); if(ev) Store.saveEvent(ev); }
    if(!ev){ toast('post not loaded'); return; }
    pk = pk || ev.pubkey;
    let body=(mediaParts(ev.content).text || ev.content || '');
    body=body.replace(/https?:\/\/\S+/gi,' ').replace(/\b(?:nostr|wss?):\S+/gi,' ')
             .replace(/\bwww\.\S+/gi,' ')   // scheme-less URLs (www.x.com)
             .replace(/\b[a-z0-9-]+\.(?:com|net|org|io|gg|tv|xyz|co|app|me|info|dev|news|social|place|lol|sh|gov|edu)\b\S*/gi,' ')   // bare domains
             .replace(/#[\p{L}\p{N}_]+/gu,' ').replace(/\s+/g,' ').trim();
    if(!body){ toast('nothing to read aloud'); return; }
    const who=profOf(pk)||{}; const name=((who.display_name||who.name||'someone')+'').replace(/[#@_]/g,' ').replace(/\s+/g,' ').trim()||'someone';
    try{ if(_narrateAudio){ _narrateAudio.pause(); _narrateAudio=null; } }catch(_){}
    toast('🔊 reading aloud…');
    try{
      const r=await fetch('/client/narrate',{method:'POST',headers:{'Content-Type':'application/json'},
        body:JSON.stringify({ text:`${name}. ${body}`.slice(0,2000) })});
      const j=await r.json().catch(()=>({}));
      if(!r.ok || !j.audio){ toast(j.error||'narration unavailable'); return; }
      _narrateAudio=new Audio('data:audio/mp3;base64,'+j.audio);
      _narrateAudio.play().catch(()=>{   // autoplay blocked (e.g. fired from a long-press timer) — play on the next tap
        toast('tap anywhere to play 🔊');
        const go=()=>{ document.removeEventListener('click',go); document.removeEventListener('touchend',go); try{ _narrateAudio && _narrateAudio.play(); }catch(_){} };
        document.addEventListener('click', go, {once:true});
        document.addEventListener('touchend', go, {once:true});
      });
    }catch(_){ toast('narration failed'); }
  }
  // Press-and-hold a post → Read Aloud. Pointer events cover BOTH mobile long-press AND desktop
  // click-and-hold in one path. Ignores holds on links/buttons/media/inputs; a real drag/scroll cancels.
  (function(){
    let t=null, sx=0, sy=0, held=false;
    const start=e=>{
      if(e.pointerType==='mouse' && e.button!==0) return;   // left button only on desktop
      const note=e.target.closest && e.target.closest('.note[data-id]'); if(!note) return;
      if(e.target.closest('a,button,img,video,input,textarea,[contenteditable]')) return;
      const id=note.dataset.id; if(!id) return;
      sx=e.clientX; sy=e.clientY; held=false;
      clearTimeout(t);
      t=setTimeout(()=>{ t=null; held=true; try{ navigator.vibrate && navigator.vibrate(15); }catch(_){} narratePost(id); }, 550);
    };
    const clear=()=>{ clearTimeout(t); t=null; };
    const up=()=>{   // release: if THIS press triggered a hold, swallow the click it's about to synthesize
      clear();
      if(held){ held=false;
        const swallow=ev=>{ ev.stopPropagation(); ev.preventDefault(); };
        document.addEventListener('click', swallow, {capture:true, once:true});
        setTimeout(()=>document.removeEventListener('click', swallow, {capture:true}), 500);   // tight window: the synthetic click follows release within ms
      }
    };
    const move=e=>{ if(Math.hypot(e.clientX-sx, e.clientY-sy) > 12){ clear(); held=false; } };   // real drag/scroll cancels
    document.addEventListener('pointerdown', start, {passive:true});
    document.addEventListener('pointerup', up, {passive:true});
    document.addEventListener('pointermove', move, {passive:true});
    document.addEventListener('pointercancel', ()=>{ clear(); held=false; }, {passive:true});
  })();
  // Summarize the post (and its surrounding thread) via the node's AI backend, shown in a modal.
  async function summarizePost(id){
    let ev=Store.get(id); if(!ev){ ev=await fetchEvent(id); if(ev) Store.saveEvent(ev); }
    if(!ev){ toast('post not loaded'); return; }
    modal('<h3>📝 Summary</h3><div id="sum-body" style="max-height:60vh;overflow:auto;line-height:1.55;white-space:pre-wrap;font-size:15px;overflow-wrap:anywhere"><div class="spinner"></div></div>'+
          '<div class="row" style="justify-content:flex-end;gap:8px;margin-top:14px"><button class="btn btn-neon small" id="sum-post" disabled>📣 Post summary</button><button class="btn btn-ghost small" id="sum-close">Close</button></div>',
      root=>{ const c=root.querySelector('#sum-close'); if(c) c.onclick=closeModal; });
    const named=e=>{ const p=profOf(e.pubkey); const nm=p.name||p.display_name||NT().nip19.npubEncode(e.pubkey).slice(0,12); return nm+': '+((mediaParts(e.content).text||e.content||'').trim()); };
    try{
      const seen=new Set([ev.id]);
      // walk up the reply chain for context (capped), oldest first
      const chain=[ev]; let cur=ev, hops=0;
      while(cur && hops<6){
        const es=(cur.tags||[]).filter(t=>t[0]==='e');
        const pid=((es.find(t=>t[3]==='reply')||es.find(t=>t[3]==='root')||es[es.length-1])||[])[1];
        if(!pid || seen.has(pid)) break;
        let p=Store.get(pid); if(!p){ p=await fetchEvent(pid); if(p) Store.saveEvent(p); }
        if(!p) break; chain.unshift(p); seen.add(pid); cur=p; hops++;
      }
      let replies=[];
      try{ replies=(await Relay.query([{ kinds:[1], '#e':[id], limit:100 }])).filter(r=>r.id!==id && !seen.has(r.id)); }catch(_){}
      replies.sort((a,b)=>a.created_at-b.created_at);
      [...chain, ...replies].forEach(e=>needProfile(e.pubkey));
      const text=[...chain.map(named), ...replies.map(named)].join('\n\n').slice(0,8000);
      const r=await fetch('/client/summarize',{ method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({ text }) });
      const j=await r.json().catch(()=>({}));
      const body=$('#sum-body');
      if(body) body.innerHTML=(r.ok && j.text) ? linkify(j.text) : ('<div class="muted">'+enc(j.error||'summary unavailable')+'</div>');
      if(r.ok && j.text){ const pb=$('#sum-post'); if(pb){ pb.disabled=false; pb.onclick=()=>{ closeModal(); compose({text: j.text}); }; } }   // share the summary as a new note
    }catch(_){ const body=$('#sum-body'); if(body) body.innerHTML='<div class="muted">summary failed</div>'; }
  }
  // Pull the first image URL off a post (imeta tag first, then a URL in the content).
  function postImageUrl(ev){
    // An image is either a normal extension URL OR an extensionless Blossom hash URL
    // (media.poster.place/<sha256>) — the same set linkify() renders as <img>. postImageUrl used to
    // miss the hash URLs, so effects on app-uploaded images said "no image" / couldn't see it.
    const isImg = u => /\.(jpe?g|png|gif|webp|bmp|avif)([?#]|$)/i.test(u) || /\/[0-9a-f]{64}([?#]|$)/i.test(u);
    for(const t of (ev.tags||[])){
      if(t[0]==='imeta'){ const m=/url\s+(\S+)/.exec(t.slice(1).join(' ')); if(m && isImg(m[1])) return m[1]; }
    }
    for(const raw of ((ev.content||'').match(/https?:\/\/\S+/gi)||[])){
      const u=raw.replace(/[)\].,!?]+$/,''); if(isImg(u)) return u;
    }
    return null;
  }
  // 🎬 Effect: copy the post's image into a fresh AI chat (the effects studio) and remember the post,
  // so the generated effect can be posted back as a reply. Guides the user with tappable effects.
  async function effectPost(id, pk){
    let ev=Store.get(id); if(!ev){ ev=await fetchEvent(id); if(ev) Store.saveEvent(ev); }
    if(!ev){ toast('post not loaded'); return; }
    const url=postImageUrl(ev);
    if(!url){ toast('this post has no image to apply an effect to'); return; }
    // gate on AI permission — show a nice modal if the account isn't allowed
    let a={}; try{ a=await ensureAiSession(); }catch(_){}
    if(!a || !a.can_ai){
      modal('<h3>🎬 Effects</h3><div class="muted" style="line-height:1.5">Effects use the node’s AI features, which aren’t enabled for your account yet. Request access and an admin can approve it.</div>'+
            '<div class="row" style="justify-content:flex-end;gap:8px;margin-top:16px"><button class="btn btn-ghost small" id="fx-close">Close</button><button class="btn btn-neon small" id="fx-req">Request AI access</button></div>',
        root=>{ const c=root.querySelector('#fx-close'); if(c) c.onclick=closeModal; const q=root.querySelector('#fx-req'); if(q) q.onclick=()=>{ closeModal(); switchView('ai'); }; });
      return;
    }
    toast('opening the Effects studio…');
    _ai.replyTo={ id, pk }; _ai.fxImage=null; _ai.fxMedia={};
    // Hand the image off to aiMount via _ai.pendingFx instead of polling for #ai-input: the old wait()
    // loop fired as soon as the input existed, but aiMount's own conversation load was still in flight,
    // so its box re-render wiped the freshly-attached image + guide (the "had to do it twice" bug).
    _ai.pendingFx={ url };
    switchView('ai');
  }
  // Consumed by aiMount once the chat is fully mounted + conversations loaded — so the attach + guide
  // land last and survive. Opens a fresh conversation for the effect, fetches the source image, attaches.
  async function startEffectStudio(url){
    try{
      await aiNewConversation();
      let blob=null;
      try{ blob=await fetch('/client/proxy-image?url='+encodeURIComponent(url)).then(r=>r.ok?r.blob():null); }catch(_){}
      if(!blob){ try{ blob=await fetch(url).then(r=>r.blob()); }catch(_){} }
      if(!blob){ toast('could not load the post image'); return; }
      const ext=((url.split(/[?#]/)[0].split('.').pop())||'jpg').toLowerCase();
      _ai.fxImage=new File([blob], 'effect-source.'+ext, { type:blob.type||'image/jpeg' });
      await aiAddFiles([_ai.fxImage]);
      showEffectGuide();
    }catch(_){ toast('could not open the Effects studio'); }
  }
  // Telegram-style Effects studio: pick an effect → add motion → optional caption → send. The full
  // effect catalog comes from /client/effects so it never drifts from the bot. Cached after first load.
  let _fxCatalog=null;
  async function showEffectGuide(){
    if(!_fxCatalog){ try{ _fxCatalog=await fetch('/client/effects').then(r=>r.json()); }catch(_){ _fxCatalog={enhance:[],effects:[],motions:[]}; } }
    aiAddMessage('assistant', effectGuideHtml(_fxCatalog));
  }
  function effectGuideHtml(cat){
    cat=cat||{};
    const chip=o=>`<button class="fx-cmd" data-cmd="${enc(o.name)}" title="${enc(o.desc||o.name)}">${enc(o.name)}</button>`;
    const mot=c=>`<button class="fx-mot" data-add="${enc(c)}">${enc(c)}</button>`;
    const enh=(cat.enhance||[]).map(chip).join('');
    const eff=(cat.effects||[]).map(chip).join('');
    const mots=(cat.motions||['zoom','shake','pulse','trippy']).map(mot).join('');
    const stk=(cat.chars||[]).map(n=>`<button class="fx-char" data-char="${enc(n)}">🧷 ${enc(n)}</button>`).join('');
    return '<div class="fx-guide"><b>🎬 Effects studio</b> — your image is attached. Pick <b>one base effect</b>, then optionally <b>add</b> a motion, sticker and caption — they <b>stack together</b>. Hit ▶ Send; when the result appears, tap <b>↩ Send the Reply</b>.'+
      (enh?'<div class="muted small" style="margin:10px 0 4px">✨ Enhance <span style="opacity:.7">(base — pick one)</span></div><div class="fx-grid">'+enh+'</div>':'')+
      '<div class="muted small" style="margin:10px 0 4px">🎭 Effects <span style="opacity:.7">(base — pick one, '+((cat.effects||[]).length)+')</span></div><div class="fx-grid">'+eff+'</div>'+
      '<div class="muted small" style="margin:10px 0 4px">🌀 Motion <span style="opacity:.7">(optional add-on — trippy/glow/alive stack; zoom/shake/pulse one at a time)</span></div><div class="fx-row" style="display:flex;flex-wrap:wrap;gap:6px">'+mots+'</div>'+
      (stk?'<div class="muted small" style="margin:10px 0 4px">🧷 Sticker <span style="opacity:.7">(optional add-on)</span></div><div class="fx-row" style="display:flex;flex-wrap:wrap;gap:6px">'+stk+'</div>':'')+
      '<div class="muted small" style="margin:10px 0 4px">💬 Caption <span style="opacity:.7">(optional add-on)</span></div><div class="fx-row" style="display:flex;gap:6px"><button class="fx-mot" data-add="meme ">＋ meme text</button></div></div>';
  }
  // Post the generated effect media (data:base64 in _ai.fxMedia) back as a reply to the source post.
  async function sendEffectReply(mid, btn){
    const m=_ai.fxMedia[mid], to=_ai.replyTo;
    if(!m || !to){ toast('nothing to reply with'); return; }
    if(btn){ btn.disabled=true; btn.textContent='posting…'; }
    try{
      if(!m.url){ const bin=Uint8Array.from(atob(m.b64), c=>c.charCodeAt(0)); m.url=await uploadBlob(new File([bin], 'effect.'+m.ext, { type:m.mime })); }
      await publish(1, m.url, eTags(to.id, to.pk));
      toast('✓ reply posted'); if(btn){ btn.textContent='✓ replied'; btn.classList.add('on'); }
    }catch(e){ toast('reply failed: '+((e&&e.message)||e)); if(btn){ btn.disabled=false; btn.textContent='↩ Send the Reply'; } }
  }
  // Combined-effects rules (match the bot/Telegram): an effect takes ONE geometry motion
  // (zoom/shake/medshake/beginshake/pulse — they don't stack); glow/alive/trippy COMPOSE (toggle).
  // The studio input is the state; these parse + rewrite it so taps build a valid command.
  const _FX_GEO=['zoom','shake','medshake','beginshake','pulse'];
  // parse the studio command into effect + mods + char(sticker) + meme(caption). Order on rebuild:
  // `effect [motion] [glow] [alive] [trippy] [char <name>] [meme <text>]` (char MUST precede meme).
  function _fxParse(v){
    const t=(v||'').trim().split(/\s+/).filter(Boolean);
    const mi=t.indexOf('meme'); const pre=mi>=0?t.slice(0,mi):t; const meme=mi>=0?t.slice(mi):[];
    let char=''; let head=pre.slice(); const ci=pre.indexOf('char');
    if(ci>=0 && pre[ci+1]){ char=pre[ci+1]; head=pre.slice(0,ci).concat(pre.slice(ci+2)); }
    return { effect:head[0]||'', mods:head.slice(1), char, meme };
  }
  function _fxJoin(p){ return [p.effect,...p.mods,...(p.char?['char',p.char]:[]),...p.meme].filter(Boolean).join(' '); }
  function _fxSetEffect(ta, eff){ const p=_fxParse(ta.value); p.effect=eff; ta.value=_fxJoin(p); }
  function _fxApplyMod(ta, mod){ const p=_fxParse(ta.value);
    if(mod==='meme '){ if(!p.meme.length) p.meme=['meme']; ta.value=_fxJoin(p)+' '; return; }
    if(_FX_GEO.includes(mod)){ p.mods=p.mods.filter(m=>!_FX_GEO.includes(m) && m!==mod); p.mods.push(mod); }   // ONE geometry motion
    else { p.mods.includes(mod) ? (p.mods=p.mods.filter(m=>m!==mod)) : p.mods.push(mod); }                      // glow/alive/trippy compose (toggle)
    ta.value=_fxJoin(p); }
  function _fxApplyChar(ta, name){ const p=_fxParse(ta.value); p.char=(p.char===name)?'':name; ta.value=_fxJoin(p); }   // sticker overlay — single, toggle
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
    _save(a){ try{ localStorage.setItem(this.key(), JSON.stringify(a.slice(0,300))); }catch(_){} bumpDraft(); this._sync(a); },
    get(id){ return this.all().find(x=>x.id===id && !x.del); },
    // live = real drafts (tombstones + empties hidden); all() keeps tombstones for the sync merge.
    live(){ return this.all().filter(d=> d && !d.del && (d.text||'').trim()); },
    save(d){ const a=this.all(); d.id=d.id||('d'+Date.now().toString(36)+Math.random().toString(36).slice(2,6)); d.ts=Math.floor(Date.now()/1000);
      const i=a.findIndex(x=>x.id===d.id); if(i>=0)a[i]=d; else a.unshift(d); this._save(a); return d.id; },
    // TOMBSTONE, don't drop: pull() merges by union (newest-ts wins), so a plain delete gets
    // resurrected from the server/other-device copy. A `del:true` entry with a fresh ts makes the
    // deletion win the merge and propagate. Old tombstones (>30d) are pruned so the doc stays bounded.
    remove(id){ const now=Math.floor(Date.now()/1000);
      let a=this.all().map(x=> x.id===id ? {id, ts:now, del:true} : x);
      if(!a.some(x=>x.id===id)) a.push({id, ts:now, del:true});
      a=a.filter(x=> !(x.del && now-(x.ts||0) > 2592000));
      this._save(a); },
    // Sync to/from a single encrypted Nostr event (kind-30078 pcai:drafts under the storage key),
    // so drafts written on one device appear on another. Push is debounced.
    _sync(a){ if(typeof ME==='undefined'||!ME) return; clearTimeout(this._t); this._t=setTimeout(async()=>{
      try{ const auth=await sign(27235,'drafts',[['p',ME.pubkey]]);
        await fetch('/client/drafts',{method:'POST',headers:{'Content-Type':'application/json'},
          body:JSON.stringify({pubkey:ME.pubkey,auth:btoa(JSON.stringify(auth)),drafts:a})}); }catch(_){} }, 900); },
    async pull(){ if(typeof ME==='undefined'||!ME) return;
      try{ const auth=await sign(27235,'drafts',[['p',ME.pubkey]]);
        const r=await fetch('/client/drafts',{method:'POST',headers:{'Content-Type':'application/json'},
          body:JSON.stringify({pubkey:ME.pubkey,auth:btoa(JSON.stringify(auth))})}).then(r=>r.json());
        if(r && r.ok && Array.isArray(r.drafts)){
          // union by id, newest ts wins — never drops a draft made offline on either device
          const map={}; [...r.drafts, ...this.all()].forEach(d=>{ if(d&&d.id&&(!map[d.id]||(d.ts||0)>=(map[d.id].ts||0))) map[d.id]=d; });
          const merged=Object.values(map).sort((a,b)=>(b.ts||0)-(a.ts||0));
          try{ localStorage.setItem(this.key(), JSON.stringify(merged.slice(0,300))); }catch(_){}
          bumpDraft(); if(VIEW==='drafts') renderDrafts();
        } }catch(_){} },
  };
  function bumpDraft(){ const n=Drafts.live().length; $$('#draft-badge,#more-badge-m').forEach(b=>{ if(n){b.textContent=n>99?'99+':n;b.classList.remove('hidden');}else b.classList.add('hidden'); }); }
  // mobile overflow sheet — holds the secondary views so the bottom bar stays uncluttered
  function moreMenu(){
    const dn=Drafts.live().length;   // per-item counts so the ☰ badge is explained once opened
    const counts={drafts:dn};
    // Discover + Games each live in their OWN sub-sheet (one row here) so they don't crowd the More sheet.
    const items=[['ai','🤖','PosterChan AI'],['drafts','✐','Drafts'],['bookmarks','🔖','Bookmarks'],['__discover','🧭','Discover'],['__games','🎮','Games'],['__files','📁','Files'],['profile','👤','Profile'],['settings','⚙','Settings'],['logout','⎋','Logout']]
      .filter(([v])=> !(window.PC_NOSTR_ONLY && v==='ai'));   // hide AI in Nostr-only deployments
    const _wot=Number(CFG.users)||0;   // WoT network size + live online + on-relay (same stats as the desktop sidebar)
    const _stat=(_wot||_lastOnline||_lastRelay)?`<div class="more-stats muted small" style="display:flex;gap:16px;justify-content:center;flex-wrap:wrap;margin:-2px 0 12px">${_wot?`<span>${WOT_ICON} ${_wot.toLocaleString()} users</span>`:''}${_lastOnline?`<span>${LIVE_ICON} ${_lastOnline.toLocaleString()} online</span>`:''}${_lastRelay?`<span title="People connected to this relay right now">${RELAY_ICON} ${_lastRelay.toLocaleString()} on relay</span>`:''}</div>`:'';
    modal(`<h3>More</h3>${_stat}<div class="more-grid">${items.map(([v,ic,lbl])=>{const c=counts[v]||0;return `<button class="more-item${v==='logout'?' more-logout':''}" data-v="${v}"><span class="more-ic">${ic}</span><span>${enc(lbl)}${c?` <i class="badge">${c>99?'99+':c}</i>`:''}</span></button>`;}).join('')}</div>`, root=>{
      $$('.more-item',root).forEach(b=> b.onclick=()=>{ const v=b.dataset.v; if(v==='__discover'){ closeModal(); discoverMenu(); return; } if(v==='__games'){ closeModal(); gamesMenu(); return; } if(v==='__files'){ closeModal(); filesMenu(); return; } closeModal(); if(v==='logout') logout(); else if(v==='profile') renderProfileView(ME.pubkey); else switchView(v); });
    });
  }
  function filesMenu(){   // mobile Files sub-sheet — mirrors the desktop sidebar's Files group
    const items=[['blossom','🌸','Blossom'],['__music','🎵','Music']];
    modal(`<h3>📁 Files</h3><div class="more-grid">${items.map(([v,ic,lbl])=>`<button class="more-item" data-v="${v}"><span class="more-ic">${ic}</span><span>${enc(lbl)}</span></button>`).join('')}</div>`, root=>{
      $$('.more-item',root).forEach(b=> b.onclick=()=>{ const v=b.dataset.v; closeModal(); if(v==='__music') openMusic(); else switchView(v); });
    });
  }
  function discoverMenu(){   // mobile Discover sub-sheet — mirrors the desktop sidebar's Discover group (incl. Market)
    const items=[['articles','📰','Articles'],['market','🛍️','Market'],['streams','📺','Streams'],['communities','👥','Communities'],['chat','💬','Chat'],['torrents','🧲','Torrents'],['repos','🌱','Git Repos'],['4chan','🍀','4chan']];
    modal(`<h3>🧭 Discover</h3><div class="more-grid">${items.map(([v,ic,lbl])=>`<button class="more-item" data-v="${v}"><span class="more-ic">${ic}</span><span>${enc(lbl)}</span></button>`).join('')}</div>`, root=>{
      $$('.more-item',root).forEach(b=> b.onclick=()=>{ closeModal(); switchView(b.dataset.v); });
    });
  }
  function gamesMenu(){
    const items=[['chess','♟️','Chess'],['ttt','⭕','Tic-Tac-Toe'],['hangman','🎯','Hangman'],['connect4','🔴','Connect Four'],['blackjack','🃏','Blackjack'],['holdem','🂡',"Texas Hold'em"]];
    modal(`<h3>🎮 Games</h3><div class="more-grid">${items.map(([v,ic,lbl])=>`<button class="more-item" data-v="${v}"><span class="more-ic">${ic}</span><span>${enc(lbl)}</span></button>`).join('')}</div>`, root=>{
      $$('.more-item',root).forEach(b=> b.onclick=()=>{ closeModal(); switchView(b.dataset.v); });
    });
  }
  function renderDrafts(){
    const feed=$('#feed'); const list=Drafts.live();
    feed.innerHTML = list.length ? list.map(d=>{
      const ctx = d.reply?'<span class="muted small">↩ reply</span>' : d.quote?'<span class="muted small">❝ quote</span>' : '';
      return `<div class="note draft-card" data-draft="${d.id}"><div class="draft-body">${linkify(d.text||'')}</div>
        <div class="draft-foot"><span class="muted small">${ctx} saved ${timeAgo(d.ts)}</span>
          <span class="spacer"></span>
          <button class="btn btn-ghost small" data-act="edit">✏ Edit</button>
          <button class="btn btn-ghost small" data-act="del" style="color:var(--danger)">🗑 Delete</button>
          <button class="btn btn-neon small" data-act="send">Send ▶</button></div></div>`;
    }).join('') : '<div class="empty">No drafts. Write a post and tap 💾 Draft to save it for later.</div>';
    feed.querySelectorAll('.draft-card').forEach(card=>{
      const id=card.dataset.draft;
      card.querySelector('[data-act="edit"]').onclick=()=>{ const d=Drafts.get(id); if(d) compose({reply:d.reply,replyPk:d.replyPk,quote:d.quote,draftId:id,text:d.text,cw:d.cw,cwReason:d.cwReason}); };
      card.querySelector('[data-act="del"]').onclick=()=>{ if(confirm('Delete this draft?')){ Drafts.remove(id); renderDrafts(); } };
      card.querySelector('[data-act="send"]').onclick=()=>sendDraft(id);
    });
    hydrate(feed);
  }
  // Append a quoted note as an inline `nostr:nevent` (WITH relay hint + author) to the post content.
  // A NIP-18 quote needs BOTH the `q` tag AND the inline nevent: many clients (Damus/Amethyst/Primal)
  // render the quote from the CONTENT nevent, not the q-tag, so a q-tag-only quote shows as bare text
  // there. No-op if the content already carries an nevent/note reference (user pasted one).
  function _appendQuoteNevent(content, id, pk){
    try{
      if(/nostr:(nevent1|note1)/i.test(content||'')) return content;
      const nev='nostr:'+NT().nip19.neventEncode({ id, relays:[CFG.relay_url].filter(Boolean), author:pk||undefined });
      return (content && content.trim() ? content.trim()+'\n\n' : '')+nev;
    }catch(_){ return content; }
  }
  async function sendDraft(id){
    const d=Drafts.get(id); if(!d || !(d.text||'').trim()) return;
    let tags=[]; let content=d.text;
    if(d.reply){ const o=Store.get(d.reply); tags=replyTags(o, d.reply, d.replyPk); }
    if(d.quote){ const o=Store.get(d.quote); const qpk=(o&&o.pubkey)||''; tags.push(['q', d.quote, CFG.relay_url||'', qpk]); if(qpk)tags.push(['p',qpk]); content=_appendQuoteNevent(content, d.quote, qpk); }
    mentionTags(d.text).forEach(t=>{ if(!tags.some(x=>x[0]==='p'&&x[1]===t[1])) tags.push(t); });
    if(d.cw) tags.push(['content-warning', d.cwReason||'']);   // honour a draft's 🔞 flag on direct send too
    try{ await publish(1, content, tags); Drafts.remove(id); toast('posted'); if(VIEW==='drafts') renderDrafts(); }
    catch(e){ toast('post failed: '+e.message); }
  }
  // Tags for a top-level community post (NIP-72 + NIP-22 comment, kind 1111). Uppercase A/K/P =
  // root scope (the community); lowercase a/k/p = parent (== root for a top-level post). Including
  // the lowercase `a` also makes it match our own `#a` community-posts query.
  function communityPostTags(c){
    const d=(c.tags.find(t=>t[0]==='d')||[])[1]||''; const addr='34550:'+c.pubkey+':'+d; const r=CFG.relay_url||'';
    return [['A',addr,r],['K','34550'],['P',c.pubkey,r],['a',addr,r],['k','34550'],['p',c.pubkey,r]];
  }
  // NIP-22 comment on a NIP-23 article. Root scope (uppercase A/K/P) is ALWAYS the article. The parent
  // (lowercase) is the article for a top-level comment, or another comment (e/k/p) for a threaded reply.
  function articleCommentTags(a, parent){
    const d=(a.tags.find(t=>t[0]==='d')||[])[1]||''; const addr='30023:'+a.pubkey+':'+d; const r=CFG.relay_url||'';
    const tags=[['A',addr,r],['K','30023'],['P',a.pubkey,r]];
    if(parent) tags.push(['e',parent.id,r,parent.pubkey],['k',String(parent.kind||1111)],['p',parent.pubkey,r]);
    else tags.push(['a',addr,r],['k','30023'],['p',a.pubkey,r]);
    return tags;
  }
  function compose({reply=null, replyPk=null, quote=null, draftId=null, text='', community=null, articleComment=null, articleParent=null, cw=false, cwReason=''}={}){
    const title = articleComment?(articleParent?'Reply to comment':'Comment on article'):community?('Post to '+((community.tags.find(t=>t[0]==='name')||[])[1]||(community.tags.find(t=>t[0]==='d')||[])[1]||'community')):reply?'Reply':quote?'Quote post':'New post';
    let qhtml=''; if(quote){ const o=Store.get(quote); if(o) qhtml=`<div class="quoted"><b>${enc((profOf(o.pubkey).name)||'anon')}</b><div class="txt">${linkify(o.content)}</div></div>`; }
    modal(`<h3>${title}</h3>${qhtml}
      <div class="cmp-tabs"><button class="cmp-tab active" data-t="write">Write</button><button class="cmp-tab" data-t="preview">👁 Preview</button></div>
      <textarea id="cmp" placeholder="what's happening on the net?"></textarea>
      <div class="muted small mention-hint hidden" id="cmp-mentions"></div>
      <div id="cmp-preview" class="note-preview hidden"></div>
      <div class="row cmp-tools"><div class="cmp-left"><button class="btn btn-ghost small" id="cmp-attach">📎 Attach</button><button class="btn btn-ghost small" id="cmp-react">😀 React</button><button class="btn btn-ghost small" id="cmp-translate">🌐 Translate</button>${(reply||quote||community||articleComment)?'':'<button class="btn btn-ghost small" id="cmp-poll">📊 Poll</button>'}<button class="btn btn-ghost small" id="cmp-ai" title="AI tools">🤖 AI ▾</button><button class="btn btn-ghost small" id="cmp-cw-btn" title="mark sensitive / NSFW (NIP-36)">🔞</button><input type="file" id="cmp-file" multiple hidden></div>
      </div>
      <div id="cmp-cw-row" class="cmp-cw-row hidden"><input class="input" id="cmp-cw-reason" maxlength="120" placeholder="🔞 sensitive — reason (optional, e.g. nudity)"></div>
      <div class="cmp-actions" style="display:block;text-align:center;margin-top:12px"><button class="btn btn-ghost small" id="cmp-draft" style="display:inline-block;margin:0 5px;min-width:120px">💾 Draft</button><button class="btn btn-neon small" id="cmp-send" style="display:inline-block;margin:0 5px;min-width:120px">Post ▶</button></div>
      <div id="cmp-pollbox" class="poll-build hidden">
        <div class="muted small">Poll options</div>
        <div id="cmp-poll-opts"><input class="input poll-opt-in" placeholder="Option 1"><input class="input poll-opt-in" placeholder="Option 2"></div>
        <div class="row"><button class="btn btn-ghost small" id="cmp-poll-add">＋ Add option</button>
          <label class="muted small" style="margin-left:auto"><input type="checkbox" id="cmp-poll-multi"> Allow multiple</label></div>
      </div>
      <div class="muted small" id="cmp-status"></div>`, root=>{
      const ta=$('#cmp',root); attachMentionAutocomplete(ta); if(text) ta.value=text;
      // Auto-save as a Draft if the composer is dismissed by ACCIDENT (click-outside / Escape) with
      // unsaved text — leaving the New Post / reply window shouldn't lose what you typed. Posting or the
      // 💾 Draft button set `committed` so they don't double-save; an empty composer saves nothing.
      let committed=false;
      const _autoSaveDraft=()=>{ if(committed || !(ta.value||'').trim()) return; committed=true;
        try{ Drafts.save({id:draftId, text:ta.value.trim(), reply, replyPk, quote, ..._cwState()}); toast('saved to drafts 💾'); if(VIEW==='drafts') renderView(true); }catch(_){} };
      const _closeCmp=()=>{ document.removeEventListener('keydown',_escSave); closeModal(); };
      const _escSave=e=>{ if(e.key==='Escape'){ e.preventDefault(); _autoSaveDraft(); _closeCmp(); } };
      document.addEventListener('keydown', _escSave);
      { const _bg=root.parentElement; if(_bg) _bg.onclick=e=>{ if(e.target===_bg){ _autoSaveDraft(); _closeCmp(); } }; }   // click-outside → save then close
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
          catch(err){ if(_blossomDenied(err)){ requestBlossomAccess(); $('#cmp-status',root).textContent='🔒 No upload access — requested it from the admin.'; } else $('#cmp-status',root).textContent='upload failed: '+err.message; return; } }
        $('#cmp-status',root).textContent='';
      });
      // 📎 Attach → pick Local (this device) or Blossom (your uploaded files)
      $('#cmp-attach',root).onclick=()=>openMenuPopover($('#cmp-attach',root), [['local','💻 Local'],['blossom','🌸 Blossom']], a=>{
        if(a==='local') $('#cmp-file',root).click(); else if(a==='blossom') blossomPicker(ta); });
      // 😀 React → insert an Emoji or a GIF
      $('#cmp-react',root).onclick=(e)=>{ e.stopPropagation();
        const items=[['emoji','😀 Emoji']]; if(CFG.gif_enabled) items.push(['gif','🎬 GIF']);
        openMenuPopover($('#cmp-react',root), items, a=>{
          if(a==='emoji') openEmojiPopover($('#cmp-react',root), (emoji)=>{ _insertAt(ta, emoji); });
          else if(a==='gif') gifPicker(ta); }); };
      { const tb=$('#cmp-translate',root); if(tb) tb.onclick=()=>composeTranslate(ta, tb); }
      // 📊 Poll → toggle the poll-builder; ＋ Add option grows the list
      { const pb=$('#cmp-poll',root), box=$('#cmp-pollbox',root);
        if(pb) pb.onclick=()=>{ const on=box.classList.toggle('hidden')===false; pb.classList.toggle('active',on); };
        const add=$('#cmp-poll-add',root);
        if(add) add.onclick=()=>{ const wrap=$('#cmp-poll-opts',root); const n=wrap.children.length+1;
          const i=document.createElement('input'); i.className='input poll-opt-in'; i.placeholder='Option '+n; wrap.appendChild(i); i.focus(); };
      }
      // 🤖 AI → a small menu (✨ AI Enhancer = summarize a pasted link into a post; # Hashtags = suggest
      // + append). Uses the shared openMenuPopover so it's consistent with the other menus and becomes a
      // readable bottom-sheet on mobile (was a cramped hand-rolled dropdown).
      { const aiBtn=$('#cmp-ai',root);
        const firstUrl=()=>{ const m=(ta.value||'').match(/https?:\/\/[^\s]+/i); return m?m[0]:null; };
        const hasImage=()=>/(?:!\[|https?:\/\/\S+\.(?:png|jpe?g|gif|webp)\b|\/blossom\/|media\.)/i.test(ta.value||'');
        let lastTags='';   // the EXACT hashtag block we last appended — only strip THIS on re-run, never the user's own tags
        const doEnhance=async()=>{
          const url=firstUrl(); if(!url){ toast('paste a link into the post first'); return; }
          $('#cmp-status',root).textContent='summarizing link…';
          try{
            const r=await fetch('/client/compose-from-url',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({url})}).then(r=>r.json());
            if(r&&r.text){ ta.value=r.text; lastTags=''; $('#cmp-status',root).textContent=''; ta.dispatchEvent(new Event('input')); }
            else $('#cmp-status',root).textContent='couldn\'t summarize: '+((r&&r.error)||'no content');
          }catch(_){ $('#cmp-status',root).textContent='summarize failed'; }
        };
        const doTags=async()=>{
          const body=(ta.value||'').trim(); if(!body && !hasImage()){ toast('write something first'); return; }
          $('#cmp-status',root).textContent='finding hashtags…';
          try{
            const r=await fetch('/client/hashtags',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({text:body, has_image:hasImage()})}).then(r=>r.json());
            if(r&&r.hashtags){
              // Re-run: strip ONLY the exact block WE appended last time (if it's still at the end), so a
              // user's own hand-typed trailing #tags are preserved — never blanket-strip trailing hashtags.
              let base=body;
              if(lastTags && base.endsWith(lastTags)) base=base.slice(0, base.length-lastTags.length).replace(/\s+$/,'');
              ta.value = base + (base?'\n\n':'') + r.hashtags; lastTags=r.hashtags;
              $('#cmp-status',root).textContent=''; ta.dispatchEvent(new Event('input'));
            } else $('#cmp-status',root).textContent='no hashtags: '+((r&&r.error)||'try again');
          }catch(_){ $('#cmp-status',root).textContent='hashtags failed'; }
        };
        if(aiBtn) aiBtn.onclick=(e)=>{ e.stopPropagation(); openMenuPopover(aiBtn, [['enhance','✨ AI Enhancer'],['tags','# Hashtags']], a=>{ if(a==='enhance') doEnhance(); else if(a==='tags') doTags(); }); };
      }
      $('#cmp-file',root).onchange=async e=>{ const files=[...e.target.files]; if(!files.length)return;
        for(let i=0;i<files.length;i++){ $('#cmp-status',root).textContent=`uploading ${i+1}/${files.length}…`;
          try{ const url=await uploadBlob(files[i]); ta.value+=(ta.value?'\n':'')+url; }
          catch(err){ if(_blossomDenied(err)){ requestBlossomAccess(); $('#cmp-status',root).textContent='🔒 No upload access — requested it from the admin.'; } else $('#cmp-status',root).textContent='upload failed: '+err.message; return; } }
        $('#cmp-status',root).textContent=''; e.target.value=''; };
      // drag & drop files onto the composer → upload + append URLs (same as 📎 Attach / paste)
      { const _cmpDrop=async files=>{ files=files.filter(Boolean); if(!files.length)return;
          for(let i=0;i<files.length;i++){ $('#cmp-status',root).textContent=`uploading ${i+1}/${files.length}…`;
            try{ const url=await uploadBlob(files[i]); ta.value+=(ta.value?'\n':'')+url; }
            catch(err){ if(_blossomDenied(err)){ requestBlossomAccess(); $('#cmp-status',root).textContent='🔒 No upload access — requested it from the admin.'; } else $('#cmp-status',root).textContent='upload failed: '+err.message; return; } }
          $('#cmp-status',root).textContent=''; };
        root.addEventListener('dragover',e=>{ if(e.dataTransfer&&[...(e.dataTransfer.types||[])].includes('Files')){ e.preventDefault(); root.classList.add('cmp-drop'); } });
        root.addEventListener('dragleave',e=>{ if(e.target===root) root.classList.remove('cmp-drop'); });
        root.addEventListener('drop',async e=>{ if(!(e.dataTransfer&&[...(e.dataTransfer.types||[])].includes('Files')))return; e.preventDefault(); root.classList.remove('cmp-drop'); await _cmpDrop([...(e.dataTransfer.files||[])]); });
      }
      $('#cmp-draft',root).onclick=()=>{
        const body=ta.value.trim(); if(!body){ toast('nothing to save'); return; }
        committed=true; Drafts.save({id:draftId, text:body, reply, replyPk, quote, ..._cwState()}); _closeCmp(); toast('saved to drafts');
        if(VIEW==='drafts') renderView(true);
      };
      // 🔞 sensitive / NSFW (NIP-36): toggle a content-warning, optionally with a reason.
      { const cb=$('#cmp-cw-btn',root); if(cb) cb.onclick=()=>{ cb.classList.toggle('on'); const r=$('#cmp-cw-row',root); if(r) r.classList.toggle('hidden', !cb.classList.contains('on')); const ri=$('#cmp-cw-reason',root); if(ri && cb.classList.contains('on')) ri.focus(); }; }
      // restore the toggle when re-opening a draft that had it set (else a sensitive post posts unflagged)
      if(cw){ const cb=$('#cmp-cw-btn',root); if(cb) cb.classList.add('on'); const r=$('#cmp-cw-row',root); if(r) r.classList.remove('hidden'); const ri=$('#cmp-cw-reason',root); if(ri) ri.value=cwReason||''; }
      const _cwState=()=>{ const cb=$('#cmp-cw-btn',root); return cb && cb.classList.contains('on') ? { cw:true, cwReason:(($('#cmp-cw-reason',root)||{}).value||'').trim() } : { cw:false, cwReason:'' }; };
      const _applyCw=(tags)=>{ const s=_cwState(); if(s.cw) tags.push(['content-warning', s.cwReason]); };
      $('#cmp-send',root).onclick=async()=>{
        const text=ta.value.trim(); if(!text && !quote)return;   // a quote-repost may have no comment
        committed=true; document.removeEventListener('keydown',_escSave);   // posting → don't auto-save; drop the Escape hook
        // 📊 Poll (NIP-88 kind-1068) — only for top-level posts; question = text, options from the builder.
        { const pbox=$('#cmp-pollbox',root); if(pbox && !pbox.classList.contains('hidden')){
            const labels=[...$$('.poll-opt-in',root)].map(i=>i.value.trim()).filter(Boolean);
            if(labels.length<2){ $('#cmp-status',root).textContent='add at least 2 poll options'; return; }
            const multi=$('#cmp-poll-multi',root).checked;
            const tags=[['polltype', multi?'multiplechoice':'singlechoice']];
            labels.forEach((l,i)=>tags.push(['option','opt'+(i+1), l]));
            mentionTags(text).forEach(t=>{ if(!tags.some(x=>x[0]==='p'&&x[1]===t[1])) tags.push(t); });
            imetaTagsFor(text).forEach(t=>tags.push(t));
            _applyCw(tags);
            closeModal(); try{ await publish(1068, text, tags); toast('poll posted'); if(VIEW==='home'||VIEW==='global') renderView(true); }
            catch(e){ toast('poll failed: '+((e&&e.message)||e)); } return;
          } }
        // Community post / article comment → NIP-22 comment (kind 1111) scoped to that root.
        if(community || articleComment){
          let tags = articleComment ? articleCommentTags(articleComment, articleParent) : communityPostTags(community);
          mentionTags(text).forEach(t=>{ if(!tags.some(x=>x[0]==='p'&&x[1]===t[1])) tags.push(t); });
          imetaTagsFor(text).forEach(t=>tags.push(t));
          _applyCw(tags);
          closeModal();
          try{ await publish(1111, text, tags);
            if(articleComment){ toast('comment posted'); if(VIEW==='article') openArticle(articleComment); }
            else { toast('posted to community'); if(VIEW==='community') openCommunity(community); }
          }catch(e){ toast('post failed: '+((e&&e.message)||e)); } return;
        }
        let tags=[]; let content=text;
        if(reply){ const o=Store.get(reply); tags=replyTags(o, reply, replyPk); }
        if(quote){ const o=Store.get(quote); const qpk=(o&&o.pubkey)||''; tags.push(['q', quote, CFG.relay_url||'', qpk]); if(qpk)tags.push(['p',qpk]); content=_appendQuoteNevent(content, quote, qpk); }
        mentionTags(text).forEach(t=>{ if(!tags.some(x=>x[0]==='p'&&x[1]===t[1])) tags.push(t); });
        imetaTagsFor(text).forEach(t=>tags.push(t));
        _applyCw(tags);
        closeModal(); await publish(1, content, tags); if(draftId) Drafts.remove(draftId);
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
    if (s) return s.replace(/\/+$/, '');         // user's own server (their CORS rules apply)
    // Built-in server: hit it SAME-ORIGIN via /blossom (the app that serves this client also mounts
    // the Blossom router), so uploads/list/delete need NO CORS preflight. blossom_public_url (e.g.
    // media.poster.place) is only the PUBLIC url the server returns for each blob — used for sharing,
    // not for the API call — so cross-origin CORS can never block an upload again.
    return (self.location && self.location.origin ? self.location.origin : '') + '/blossom';
  }
  const NOSTR_BUILD='https://nostr.build';   // NIP-96 fallback host (not Blossom — uploads via uploadNip96)
  let _blossomOK=null;   // built-in Blossom upload permission for ME: true/false once checked, null=unknown
  // The effective UPLOAD target as {url, proto}. proto is 'blossom' (BUD-02 PUT /upload + kind-24242) or
  // 'nip96' (POST multipart + NIP-98). Priority: the user's own enabled server (proto inferred/stored) →
  // else, if this user has NO built-in Blossom permission, default to nostr.build (NIP-96) so a brand-new
  // user can still upload out of the box → else the built-in /blossom.
  function _blossomBuiltin(){ return { url:(self.location&&self.location.origin?self.location.origin:'')+'/blossom', proto:'blossom' }; }
  function uploadTarget(){
    if(ClientSettings.get('blossomEnabled')){
      let s=(ClientSettings.get('mediaServer')||'').trim();
      if(s){ if(!/^https?:\/\//i.test(s)) s='https://'+s; s=s.replace(/\/+$/,'');
        // nostr.build is ALWAYS NIP-96 — the hostname wins over any stored/legacy proto (a legacy
        // kind-10063 record would otherwise mis-restore it as 'blossom' and fail the PUT). Other
        // hosts use the proto detected (by capability) at save time; default blossom.
        const proto=/(^|\.)nostr\.build$/i.test((()=>{try{return new URL(s).hostname;}catch(_){return '';}})())
          ? 'nip96' : (ClientSettings.get('mediaProto','')||'blossom');
        return { url:s, proto }; }
    }
    if(_blossomOK===false) return { url:NOSTR_BUILD, proto:'nip96' };
    return _blossomBuiltin();
  }
  // Detect a media server's protocol by CAPABILITY (does it publish a NIP-96 well-known?) rather than
  // by hostname — so any NIP-96 host works, not just nostr.build. Falls back to a hostname guess if the
  // probe is blocked (CORS) or unreachable.
  async function detectProto(url){
    const base=url.replace(/\/+$/,'');
    try{ const r=await fetch(base+'/.well-known/nostr/nip96.json'); if(r.ok && await r.json().catch(()=>null)) return 'nip96'; }catch(_){}
    return /(^|\.)nostr\.build$/i.test((()=>{try{return new URL(base).hostname;}catch(_){return '';}})())?'nip96':'blossom';
  }
  // Query whether ME has built-in Blossom upload permission (whitelist). Sets _blossomOK so uploadTarget
  // can fall back to nostr.build for users who don't. Cheap, cached; called on login.
  async function checkBlossomAccess(){
    try{
      if(!ME||!ME.pubkey){ _blossomOK=null; return; }
      if(!CFG.blossom_enabled){ _blossomOK=false; return; }   // built-in server off → nobody can use it
      const r=await fetch('/client/blossom-access?pubkey='+encodeURIComponent(ME.pubkey)).then(r=>r.json());
      _blossomOK=!!(r&&r.whitelisted);
    }catch(_){ _blossomOK=null; }   // unknown → keep built-in default (don't wrongly divert to nostr.build)
  }
  // Boot-time RESTORE of the synced media server (kind-10063 Blossom / kind-10096 NIP-96) so a fresh
  // device uploads to the user's chosen server without first opening Settings. DOM-free (safe on boot);
  // the Settings modal reads the same ClientSettings when it renders. No-op if this device already has one.
  async function restoreMediaServer(){
    try{
      if(ClientSettings.get('mediaServer','') || !ME || !ME.pubkey) return;
      const evs=await Relay.query([{ authors:[ME.pubkey], kinds:[10063,10096], limit:4 }]);
      const rMedia=evs.filter(e=>e.kind===10063||e.kind===10096).sort((a,b)=>b.created_at-a.created_at)[0];
      const srv=rMedia && (rMedia.tags.find(t=>t[0]==='server')||[])[1];
      if(!srv) return;
      ClientSettings.set('mediaServer', srv);
      ClientSettings.set('mediaProto', rMedia.kind===10096?'nip96':'blossom');
      ClientSettings.set('blossomEnabled', true);
    }catch(_){}
  }
  async function sha256hex(buf){ const h=await crypto.subtle.digest('SHA-256', buf); return [...new Uint8Array(h)].map(b=>b.toString(16).padStart(2,'0')).join(''); }
  const _MIME_EXT={'image/jpeg':'jpg','image/png':'png','image/gif':'gif','image/webp':'webp','image/avif':'avif',
    'video/mp4':'mp4','video/webm':'webm','video/quicktime':'mov','audio/mpeg':'mp3','audio/ogg':'ogg','audio/wav':'wav','audio/mp4':'m4a','audio/aac':'aac','audio/flac':'flac'};
  function extFor(file){ const n=(file.name||'').match(/\.([a-z0-9]{2,5})$/i); if(n) return n[1].toLowerCase(); return _MIME_EXT[file.type]||''; }
  // NIP-92 source metadata for media we've uploaded this session, keyed by the exact URL we append to
  // a note. Lets imetaTagsFor() emit `imeta` tags so other clients render our art inline at the right
  // aspect ratio (dim), verify it (x = sha256) and know its type (m). Session-scoped, never persisted.
  const _MEDIA_META = new Map();
  // Pixel dimensions of an image/video file as "WxH" (decoded locally, no network). '' if unknown.
  async function _mediaDim(file){
    const t=file.type||'';
    try{
      if(/^image\//.test(t) && self.createImageBitmap){
        const bm=await createImageBitmap(file); const d=(bm.width&&bm.height)?(bm.width+'x'+bm.height):''; if(bm.close) bm.close(); return d;
      }
      if(/^video\//.test(t)){
        return await new Promise(res=>{ const v=document.createElement('video'); v.preload='metadata'; const u=URL.createObjectURL(file);
          v.onloadedmetadata=()=>{ res(v.videoWidth&&v.videoHeight?(v.videoWidth+'x'+v.videoHeight):''); URL.revokeObjectURL(u); };
          v.onerror=()=>{ res(''); URL.revokeObjectURL(u); }; v.src=u; });
      }
    }catch(_){}
    return '';
  }
  // Downscale + re-encode large images BEFORE they're uploaded or sent — keeps Blossom storage small
  // and, crucially, keeps base64 chat attachments under the size that made multi-image / big-image
  // sends hang. Skips animated/vector (gif/svg) and anything already small; never upsizes. Pure
  // browser canvas work, so it's cheap and offloads the server.
  async function compressImage(file, opts){
    const o = opts || {}; const maxDim = o.maxDim || 3072, quality = o.quality || 0.9, maxBytes = o.maxBytes || 900*1024;
    try{
      const t=(file.type||'').toLowerCase();
      if(!/^image\//.test(t) || /gif|svg/.test(t)) return file;     // keep animation/vector intact
      // PNG keeps its format (lossless, ALPHA preserved) so transparent avatars/logos/screenshots
      // aren't flattened onto a black background; everything else re-encodes to JPEG. imageOrientation
      // bakes EXIF rotation into the pixels so a portrait phone photo isn't uploaded sideways.
      const isPng = /png/.test(t);
      const bmp=await createImageBitmap(file, {imageOrientation:'from-image'});
      let w=bmp.width, h=bmp.height; const scale=Math.min(1, maxDim/Math.max(w,h));
      if(scale>=1 && file.size<=maxBytes){ if(bmp.close) bmp.close(); return file; }   // already small + upright-enough
      w=Math.round(w*scale); h=Math.round(h*scale);
      const cv=document.createElement('canvas'); cv.width=w; cv.height=h;
      cv.getContext('2d').drawImage(bmp,0,0,w,h); if(bmp.close) bmp.close();
      const outType = isPng ? 'image/png' : 'image/jpeg';
      const blob=await new Promise(r=> cv.toBlob(r, outType, quality));
      if(!blob || blob.size>=file.size) return file;                // never make it bigger
      const ext = isPng ? 'png' : 'jpg';
      return new File([blob], (file.name||'image').replace(/\.\w+$/,'')+'.'+ext, {type:outType});
    }catch(_){ return file; }
  }
  // NIP-96 upload (nostr.build et al.): discover the endpoint from /.well-known/nostr/nip96.json,
  // POST multipart with a NIP-98 (kind-27235) Authorization header, and read the file URL out of the
  // returned nip94_event tags. Used when the target proto is 'nip96' (Blossom uploadBlob can't talk to it).
  async function uploadNip96(file, server){
    const base=server.replace(/\/+$/,'');
    let api=base+'/api/v2/nip96/upload';
    try{ const wk=await fetch(base+'/.well-known/nostr/nip96.json').then(r=>r.ok?r.json():null);
      if(wk&&wk.api_url) api=new URL(wk.api_url, base+'/').href; }catch(_){}   // resolve a relative api_url against the host
    const auth=await sign(27235,'',[['u',api],['method','POST']]);   // NIP-98 HTTP-auth event
    const fd=new FormData(); fd.append('file', file, file.name||('upload.'+(extFor(file)||'bin')));
    let res;
    try{ res=await fetch(api,{ method:'POST', headers:{ 'Authorization':'Nostr '+btoa(JSON.stringify(auth)) }, body:fd }); }
    catch(e){ throw new Error(`couldn't reach ${server} — check the URL, and that it allows cross-origin (CORS) uploads`); }
    if(!res.ok){ const t=await res.text().catch(()=>String(res.status)); throw new Error(res.headers.get('x-reason')||('upload failed: '+t)); }
    const d=await res.json();
    const tags=(d&&d.nip94_event&&d.nip94_event.tags)||[];
    const url=(tags.find(t=>t[0]==='url')||[])[1] || (d&&d.url) || '';
    if(!url) throw new Error('nostr.build: no URL in the upload response');
    try{ const t=file.type||''; if(/^(image|video)\//.test(t)){ const x=(tags.find(t=>t[0]==='x')||[])[1]; _MEDIA_META.set(url,{ m:t, x:x||undefined, dim:await _mediaDim(file) }); } }catch(_){}
    return url;
  }
  async function uploadBlob(file, opts){
    // Resolve built-in Blossom permission before routing, so a brand-new user's FIRST upload (right
    // after login, before the async check resolves) still diverts to nostr.build instead of 403ing
    // the built-in server. Only matters when they haven't set their own server.
    if(_blossomOK===null && !ClientSettings.get('blossomEnabled')){ try{ await checkBlossomAccess(); }catch(_){} }
    let tgt=uploadTarget();
    // Private / no-mirror content (encrypted vault blobs) must NEVER land on the public nostr.build
    // auto-fallback — keep it on the built-in server even if that surfaces a permission error.
    if(opts&&opts.noMirror && tgt.proto==='nip96' && !ClientSettings.get('blossomEnabled')) tgt=_blossomBuiltin();
    const server=tgt.url; if(!server) throw new Error('no media server set');
    file=await compressImage(file);   // auto-compress images (no-op for video/gif/already-small)
    if(tgt.proto==='nip96') return await uploadNip96(file, server);
    const buf=await file.arrayBuffer(); const hash=await sha256hex(buf);
    const auth=await sign(24242,'Upload blob',[['t','upload'],['x',hash],['expiration',String(Math.floor(Date.now()/1000)+3600)]]);
    const hdr={ 'Authorization':'Nostr '+btoa(JSON.stringify(auth)), 'Content-Type':file.type||'application/octet-stream' };
    if(opts&&opts.noMirror) hdr['X-No-Mirror']='1';   // don't DR-mirror (e.g. encrypted music) to public backups
    let res;
    try {
      res=await fetch(server+'/upload',{ method:'PUT', headers:hdr, body:buf });
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
    const ext=extFor(file); const url=(d.url||server+'/'+hash) + (ext?('.'+ext):'');
    // Record NIP-92 source metadata so a note carrying this URL gets an `imeta` tag (see imetaTagsFor).
    try{ const t=file.type||''; if(/^(image|video)\//.test(t)){ _MEDIA_META.set(url, { m:t, x:hash, dim:await _mediaDim(file) }); } }catch(_){}
    return url;
  }
  // NIP-92: one `imeta` tag per uploaded media URL that appears in the note content, so other clients
  // render our images/video inline (right aspect ratio via dim) and can verify them (x = sha256).
  function imetaTagsFor(content){
    const out=[], seen=new Set();
    for(let u of ((content||'').match(/https?:\/\/\S+/g)||[])){
      u=u.replace(/[)\].,>'"]+$/,'');               // drop trailing punctuation (e.g. markdown `![](url)`)
      if(seen.has(u)) continue; seen.add(u);
      const m=_MEDIA_META.get(u); if(!m) continue;
      const parts=['url '+u]; if(m.m) parts.push('m '+m.m); if(m.dim) parts.push('dim '+m.dim); if(m.x) parts.push('x '+m.x);
      out.push(['imeta', ...parts]);
    }
    return out;
  }
  // ---- Blossom access (request-to-upload) ----
  // Pre-flight whether THIS user may upload (BUD-06 HEAD /upload → 200 allowed / 403 denied), so the
  // Files view + post composer can offer a "request access" flow instead of a dead upload button.
  async function blossomCanUpload(){
    const server=mediaServer(); if(!server) return false;
    try{
      const auth=await sign(24242,'Upload blob',[['t','upload'],['expiration',String(Math.floor(Date.now()/1000)+3600)]]);
      const res=await fetch(server+'/upload',{ method:'HEAD', headers:{ 'Authorization':'Nostr '+btoa(JSON.stringify(auth)) }});
      return res.ok;            // 200 = allowed; 401/403/413 = not
    }catch(_){ return true; }    // CORS/network hiccup → don't gate; let the real upload speak
  }
  function _blossomDenied(err){ const m=String(err&&err.message||err||'').toLowerCase(); return m.includes('not authorized')||m.includes('403')||m.includes('privilege'); }
  let _blossomReqSent=false;
  // DM the instance operator asking for upload access; the admin grants it in Admin → Users.
  async function requestBlossomAccess(btn){
    if(!btn && _blossomReqSent) return;   // auto-trigger (failed upload): only DM the admin once/session
    const op=safePk(CFG.operator_npub||'');
    if(!op){ if(btn) toast('no admin contact is configured on this server'); return; }
    if(btn){ btn.disabled=true; btn.textContent='Sending…'; }
    const me=profOf(ME.pubkey)||{}; const nm=me.name||me.display_name||'A user';
    const body=`🌸 Blossom upload-access request\n${nm} (${ME.npub}) would like permission to upload files on ${location.host}. You can grant it in Admin → Users.`;
    try{ await sendDm(op, body); _blossomReqSent=true; toast('✅ Request sent to the admin'); if(btn) btn.textContent='✅ Request sent'; }
    catch(e){ toast('could not send the request'); if(btn){ btn.disabled=false; btn.textContent='🌸 Request upload access'; } }
  }
  // Grid thumbnail. Images load a small server-side JPEG (?thumb=1) instead of the full file, and
  // videos show an icon rather than downloading the whole clip — both to save bandwidth in the grid.
  function thumbUrl(u){ return u + (u.indexOf('?')<0?'?':'&') + 'thumb=1'; }
  function blobThumb(b){
    const t=b.type||'', ext=(t.split('/')[1]||'file').slice(0,10);
    if(/image/.test(t)) return `<img src="${enc(thumbUrl(b.url))}" loading="lazy">`;
    // video: ffmpeg frame thumbnail (server ?thumb=1); falls back to a 🎬 icon if it can't be decoded
    if(/video/.test(t)) return `<img class="vthumb" data-ext="${enc(ext)}" src="${enc(thumbUrl(b.url))}" loading="lazy">`;
    if(/audio/.test(t)) return `<div class="file-icon">🎵<span>${enc(ext)}</span></div>`;
    const icon = /zip|compress|tar|gzip|7z|rar/.test(t)?'📦' : /pdf/.test(t)?'📕' : /text|json|xml|csv/.test(t)?'📄' : '📎';
    return `<div class="file-icon">${icon}<span>${enc(ext)}</span></div>`;
  }
  function copyUrl(u){ try{ u=new URL(u, location.href).href; }catch(_){}
    try{ navigator.clipboard.writeText(u); toast('URL copied'); }catch(_){ const t=document.createElement('textarea'); t.value=u; document.body.appendChild(t); t.select(); try{document.execCommand('copy'); toast('URL copied');}catch(e){toast('copy failed');} t.remove(); } }
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
    FilesIdx.loadLocal();
    (async()=>{
      let list=[]; try{ const r=await fetch(server+'/list/'+ME.pubkey); if(r.ok) list=await r.json(); }catch(_){}
      // Same filter as the Files grid: hide the octet-stream noise (encrypted ciphertext, stale/live
      // index blobs, unnamed binaries) — none of it renders as media in a post, and it floods the picker.
      list = list.filter(b=>{
        if(b.sha256===FilesIdx._lastIndexSha) return false;        // the encrypted Files index blob itself
        const m=FilesIdx.meta(b.sha256);
        if(m && m.enc) return false;                               // encrypted ciphertext — not publicly viewable
        if(!m && /octet-stream/.test(b.type||'')) return false;    // stale index blobs / unnamed binaries
        return true;
      });
      const grid=bg.querySelector('#bp-grid');
      grid.innerHTML = list.length ? list.map(b=>{
        return `<div class="file-card" data-url="${enc(b.url)}" data-type="${enc(b.type||'')}">${blobThumb(b)}</div>`;
      }).join('') : '<div class="empty">No files yet — upload some in the Files tab.</div>';
      bg.querySelectorAll('[data-url]').forEach(el=> el.onclick=()=>{ const ext=_MIME_EXT[el.dataset.type]||''; ta.value+=(ta.value?'\n':'')+el.dataset.url+(ext?('.'+ext):''); bg.remove(); toast('attached'); });
    })();
  }

  // ---- 4chan browser (Discover) — live catalog/thread via the /api/4chan/* backend (proxies
  // a.4cdn.org through the built-in proxy). Content is ephemeral + re-fetchable, so it is NOT stored
  // as relay events (that would bloat the WoT relay with non-Nostr junk); the view fetches fresh.
  // ---------- Pics: a picture-first feed (NIP-68 kind-20 + image notes) as a media grid ----------
  function _firstImage(ev){
    for(const t of (ev.tags||[])){
      if((t[0]==='url'||t[0]==='image') && /^https?:\/\//i.test(t[1]||'')) return t[1];
      if(t[0]==='imeta'){ const u=(t.find(x=>/^url\s/i.test(x))||'').replace(/^url\s+/i,''); if(/^https?:\/\//i.test(u)) return u; }
    }
    const m=(ev.content||'').match(/https?:\/\/[^\s)<]+\.(?:jpe?g|png|gif|webp|avif)(?:\?[^\s)<]*)?/i);
    return m?m[0]:null;
  }
  async function renderPics(){
    const feed=$('#feed');
    feed.innerHTML='<div class="pics-grid" id="pics-grid"><div class="spinner"></div></div>';
    let evs=[];
    try{ evs=await Relay.query([{kinds:[20], limit:80},{kinds:[1], limit:160}]); }catch(_){}
    evs.forEach(e=>{ Store.saveEvent(e); needProfile(e.pubkey); });
    if(VIEW!=='pics') return;
    const pics=[]; const seen=new Set();
    for(const e of evs.sort((a,b)=>b.created_at-a.created_at)){
      if(e.kind===1 && isReply(e)) continue;
      if(isMutedView(e)) continue;
      const img=_firstImage(e); if(!img || seen.has(e.id)) continue;
      seen.add(e.id); pics.push({e,img});
      if(pics.length>=120) break;
    }
    const grid=$('#pics-grid'); if(!grid) return;
    grid.innerHTML = pics.length ? pics.map(x=>`<div class="pic-card" data-id="${x.e.id}"><img src="${enc(x.img)}" loading="lazy" onerror="this.closest('.pic-card')&&this.closest('.pic-card').remove()"></div>`).join('') : '<div class="empty">No pics found yet.</div>';
    $$('.pic-card',grid).forEach(c=> c.onclick=()=> openThread(c.dataset.id));
  }

  // ---------- 4chan browser (Discover) ----------
  // Board picker + mobile-friendly grid; thumbnails open in the shared lightbox. ----
  const _4CHAN_BOARDS = [['g','/g/ Tech'],['a','/a/ Anime'],['pol','/pol/ Politics'],['h','/h/ NSFW']];
  let _4chanBoard = '4chan' in (window._pcState||{}) ? window._pcState['4chan'] : 'g';

  async function render4chan(){
    const feed=$('#feed');
    feed.innerHTML = `<div class="fc-bar">${_4CHAN_BOARDS.map(([b,l])=>
      `<button class="fc-tab${b===_4chanBoard?' on':''}${b==='h'?' nsfw':''}" data-board="${b}">${enc(l)}</button>`).join('')}</div>
      <div class="fc-grid" id="fc-grid"><div class="spinner"></div></div>`;
    $$('.fc-tab',feed).forEach(t=> t.onclick=()=>{ _4chanBoard=t.dataset.board; (window._pcState=window._pcState||{})['4chan']=_4chanBoard; render4chan(); });
    let data=null;
    try{ const r=await fetch('/api/4chan/catalog?board='+encodeURIComponent(_4chanBoard)); if(!r.ok) throw 0; data=await r.json(); }
    catch(_){ const g=$('#fc-grid'); if(g) g.innerHTML='<div class="empty">Couldn\'t load /'+enc(_4chanBoard)+'/ — the board may be blocked or 4chan is unreachable.</div>'; return; }
    if(VIEW!=='4chan') return;
    const grid=$('#fc-grid'); if(!grid) return;
    const threads=(data&&data.threads)||[];
    grid.innerHTML = threads.length ? threads.map(t=>
      `<div class="fc-card" data-id="${enc(String(t.thread_id))}">
        ${t.thumb_url?`<img class="fc-thumb" src="${enc('/api/4chan/proxy?url='+encodeURIComponent(t.thumb_url))}" loading="lazy" onerror="this.style.visibility='hidden'">`:'<div class="fc-thumb"></div>'}
        <div class="fc-card-body"><div class="fc-title">${enc(t.title||'(no subject)')}</div>
          <div class="fc-meta">💬 ${t.replies||0} · 🖼 ${t.images||0}</div></div>
      </div>`).join('') : '<div class="empty">No threads.</div>';
    $$('.fc-card',grid).forEach(c=> c.onclick=()=> open4chanThread(_4chanBoard, c.dataset.id));
  }

  async function open4chanThread(board, id){
    const feed=$('#feed');
    feed.innerHTML = `<div class="fc-thread-top">
        <button class="btn btn-ghost small" id="fc-back">← /${enc(board)}/</button>
        <a class="btn btn-ghost small" href="https://boards.4chan.org/${enc(board)}/thread/${enc(id)}" target="_blank" rel="noopener">Open on 4chan ↗</a>
        <button class="btn btn-cyan small" id="fc-sum">✨ Summarize</button>
        <button class="btn btn-neon small" id="fc-share">🚀 Share</button>
      </div>
      <div class="fc-summary hidden" id="fc-summary"></div>
      <div class="fc-posts" id="fc-posts"><div class="spinner"></div></div>`;
    $('#fc-back').onclick=()=> render4chan();
    $('#fc-sum').onclick=()=> summarize4chan(board, id);
    // Share on Nostr → open the composer pre-filled with the thread link
    $('#fc-share').onclick=()=> compose({text: `https://boards.4chan.org/${board}/thread/${id}`});
    let data=null;
    try{ const r=await fetch(`/api/4chan/thread?board=${encodeURIComponent(board)}&thread_id=${encodeURIComponent(id)}`); if(!r.ok) throw 0; data=await r.json(); }
    catch(_){ const p=$('#fc-posts'); if(p) p.innerHTML='<div class="empty">Couldn\'t load the thread.</div>'; return; }
    if(VIEW!=='4chan') return;
    const posts=(data&&data.posts)||[]; const box=$('#fc-posts'); if(!box) return;
    box.innerHTML = posts.length ? posts.map(p=>{
      // The thread endpoint ALREADY returns thumb_url/image_url as /api/4chan/proxy paths — use them
      // as-is (re-wrapping them double-proxied → broken → no image). Show the small THUMBNAIL to save
      // bandwidth; tap opens the full-size image (or video) in the lightbox.
      let media='';
      if(p.thumb_url){
        const isVid=/\.(webm|mp4|m4v|mov|ogg)$/i.test(p.image_url_direct||p.image_url||'');
        media=`<a class="fc-post-thumb${isVid?' vid':''}" data-full="${enc(p.image_url||p.thumb_url)}" data-kind="${isVid?'video':'image'}">`
            + `<img src="${enc(p.thumb_url)}" loading="lazy" onerror="this.parentNode.style.display='none'">`
            + `${isVid?'<span class="fc-play">▶</span>':''}</a>`;
      }
      const body = p.com ? `<div class="fc-post-body">${enc(p.com)}</div>` : '';
      return `<div class="fc-post"><div class="fc-post-hd"><span class="fc-no">#${enc(String(p.no))}</span> <span class="fc-name">${enc(p.name||'Anonymous')}</span></div>${media}${body}</div>`;
    }).join('') : '<div class="empty">No posts.</div>';
    $$('.fc-post-thumb',box).forEach(a=> a.onclick=()=> openLightbox(a.dataset.full, a.dataset.kind==='video'?'video':undefined));
  }

  async function summarize4chan(board, id){
    const box=$('#fc-summary'); if(!box) return;
    box.classList.remove('hidden'); box.innerHTML='<div class="spinner"></div>';
    try{
      const r=await fetch(`/api/4chan/summarize?board=${encodeURIComponent(board)}&thread_id=${encodeURIComponent(id)}`, {credentials:'include'});
      const j=await r.json().catch(()=>({}));
      box.innerHTML = (r.ok && j.summary) ? `<h4>✨ Summary</h4>${linkify(j.summary)}` : `<div class="muted">${enc(j.error||'Summary unavailable (needs AI access).')}</div>`;
    }catch(_){ box.innerHTML='<div class="muted">Summary failed.</div>'; }
  }

  // Files view = two tabs: Public (your built-in Blossom blobs, shareable URLs) and AI Chat
  // (encrypted artifacts under your storage key, only readable here). The active tab is remembered
  // so re-rendering after an upload/delete stays put.
  let _filesTab = 'public';
  let _filesAdminPk = null;   // when set, the Admin tab is drilled into this user's files
  function openMusicFolder(){ _filesTab='public'; _filesFolder='Music'; switchView('blossom'); }   // the file-manager Music folder
  function openMusic(){   // the Music nav button → shuffle-play your whole library right away
    FilesIdx.loadLocal();
    const go=()=>{ const tracks=musicTracks(null);
      if(!tracks.length){   // no music yet → open the Music folder + guide them
        openMusicFolder();
        modal(`<h3>🎵 Your music library is empty</h3>
          <p class="muted small">Add some songs to start playing:</p>
          <div class="muted small" style="line-height:1.9;margin:10px 0">
            1. You're now in <b>Files → 🎵 Music</b><br>
            2. <b>Drag &amp; drop audio files</b> into the drop zone (or tap “choose files”)<br>
            3. Each track is <b>Opus-compressed + encrypted</b> automatically<br>
            4. Then the <b>🎵 Music</b> button shuffle-plays your whole library
          </div>
          <div class="row" style="justify-content:flex-end"><button class="btn btn-cyan" id="nm-ok">Got it</button></div>`,
          root=>{ const b=$('#nm-ok',root); if(b) b.onclick=closeModal; });
        return;
      }
      MusicPlayer.shuffle=true; MusicPlayer.refreshQueue();
      MusicPlayer.play(MusicPlayer.queue[Math.floor(Math.random()*MusicPlayer.queue.length)]); };
    if(!FilesIdx._pulled){ FilesIdx._pulled=true; FilesIdx.pull().then(go); } else go();
  }
  async function renderBlossom(){
    const feed=$('#feed');
    feed.innerHTML=`<div class="files-tabs">
        <button class="ftab${_filesTab==='public'?' active':''}" data-ft="public">🌸 Public</button>
        <button class="ftab${_filesTab==='ai'?' active':''}" data-ft="ai">🤖 AI Chat</button>
        ${IS_ADMIN?`<button class="ftab${_filesTab==='admin'?' active':''}" data-ft="admin">🛡️ Admin</button>`:''}
      </div><div id="files-pane"></div>`;
    $$('.ftab',feed).forEach(b=> b.onclick=()=>{ _filesAdminPk=null; _filesTab=b.dataset.ft; renderBlossom(); });
    const pane=$('#files-pane',feed);
    if(_filesTab==='admin') return renderBlossomAdmin(pane);
    if(_filesTab==='ai') return renderAiFiles(pane);
    return renderPublicFiles(pane);
  }
  // Admin tab: per-user storage overview. Tap a row → review that user's files; tap avatar/name → profile.
  async function renderBlossomAdmin(pane){
    if(!IS_ADMIN){ pane.innerHTML='<div class="empty">Admins only.</div>'; return; }
    if(_filesAdminPk) return renderBlossomAdminUser(pane, _filesAdminPk);
    pane.innerHTML='<div class="spinner"></div>';
    let users=[], total=0, err='';
    try{
      const auth=await sign(27235,'blossom-usage',[['p',ME.pubkey]]);   // content 'blossom-usage' binds the admin proof to THIS action (server checks it)
      const r=await fetch('/client/admin-blossom-usage',{method:'POST',headers:{'Content-Type':'application/json'},
        body:JSON.stringify({auth:btoa(JSON.stringify(auth))})}).then(r=>r.json());
      if(r&&r.ok){ users=r.users||[]; total=r.total||0; } else err=(r&&r.error)||'failed';
    }catch(_){ err='request failed'; }
    if(err){ pane.innerHTML='<div class="empty">'+enc(err)+'</div>'; return; }
    if(!users.length){ pane.innerHTML='<div class="empty">No Blossom uploads on this server yet.</div>'; return; }
    const miss=users.map(u=>u.pubkey).filter(pk=>!Store.haveProfile(pk)).slice(0,300);
    if(miss.length){ try{ (await Relay.query([{authors:miss,kinds:[0],limit:miss.length}])).forEach(e=>Store.saveProfile(e)); }catch(_){} }
    pane.innerHTML=`<div style="display:flex;gap:10px;padding:10px 4px 6px;flex-wrap:wrap">
        <div style="flex:1;min-width:120px;background:var(--panel,#16161c);border:1px solid var(--border,#333);border-radius:12px;padding:10px 14px">
          <div style="font-size:22px;font-weight:700;line-height:1.1;color:var(--cyan,#0ff)">${users.length}</div>
          <div class="muted" style="font-size:12px">📊 uploader${users.length===1?'':'s'}</div>
        </div>
        <div style="flex:1;min-width:120px;background:var(--panel,#16161c);border:1px solid var(--border,#333);border-radius:12px;padding:10px 14px">
          <div style="font-size:22px;font-weight:700;line-height:1.1;color:var(--cyan,#0ff)">${_fmtBytes(total)}</div>
          <div class="muted" style="font-size:12px">💾 total stored</div>
        </div>
      </div>
      <div class="muted small" style="padding:2px 6px 8px">Tap a row to review files · tap an avatar or name for their profile</div>
      <div class="people-list">${users.map((u,i)=>{ const p=Store.profile(u.pubkey)||{}; const nm=p.name||p.display_name||(u.npub.slice(0,12)+'…');
        return `<div class="psearch badm-row" data-i="${i}" style="cursor:pointer">
          <img src="${enc(p.picture||LOGO)}" class="badm-prof" data-pk="${u.pubkey}" style="cursor:pointer" onerror="this.src='${LOGO}'">
          <div class="pinfo"><b class="badm-prof" data-pk="${u.pubkey}" style="cursor:pointer">${enc(nm)}</b><div class="muted small">${enc(u.npub.slice(0,18))}… · ${u.count} file(s)</div></div>
          <span style="align-self:center;text-align:right"><b>${_fmtBytes(u.size)}</b><br><span class="muted small">review ›</span></span>
        </div>`; }).join('')}</div>`;
    $$('.badm-prof',pane).forEach(el=> el.onclick=(e)=>{ e.stopPropagation(); renderProfileView(el.dataset.pk); });
    $$('.badm-row',pane).forEach(el=> el.onclick=()=>{ _filesAdminPk=users[+el.dataset.i].pubkey; renderBlossom(); });
  }
  // Admin drill-in: a moderation grid of ONE user's public blobs from THIS node's built-in Blossom server
  // (CFG.blossom_url — the SAME store the usage overview is computed from, not the admin's own configured
  // mediaServer()). Tiles load DOWNSCALED ?thumb=1 previews and link to the full blob in a new tab; reuses
  // the existing purge for deletion.
  async function renderBlossomAdminUser(pane, pk){
    const server=(CFG.blossom_url||mediaServer()||'').replace(/\/$/,'');
    const p=Store.profile(pk)||{}; const nm=p.name||p.display_name||(NT().nip19.npubEncode(pk).slice(0,14)+'…');
    pane.innerHTML=`<div class="row" style="align-items:center;gap:8px;padding:6px 4px">
        <button class="btn btn-ghost small" id="badm-back">‹ Back</button>
        <img src="${enc(p.picture||LOGO)}" class="badm-prof" data-pk="${pk}" style="width:28px;height:28px;border-radius:50%;cursor:pointer" onerror="this.src='${LOGO}'">
        <b class="badm-prof" data-pk="${pk}" style="cursor:pointer">${enc(nm)}</b>
        <span style="flex:1"></span>
        <button class="btn small danger" id="badm-purge">🗑️ Purge all</button>
      </div><div class="files-grid" id="badm-grid"><div class="spinner"></div></div>`;
    $('#badm-back',pane).onclick=()=>{ _filesAdminPk=null; renderBlossom(); };
    $$('.badm-prof',pane).forEach(el=> el.onclick=()=>renderProfileView(el.dataset.pk));
    // Purge reuses the existing flow; on Cancel it returns without deleting — re-render in place (keep
    // _filesAdminPk) so a cancel doesn't bounce the admin out of the drill-in.
    $('#badm-purge',pane).onclick=async()=>{ await doPurgeBlossom(pk); renderBlossom(); };
    const g=$('#badm-grid',pane);
    if(!server){ g.innerHTML='<div class="empty">Blossom server not configured.</div>'; return; }
    let list=null;
    try{ const r=await fetch(server+'/list/'+pk); if(r.ok) list=await r.json(); }catch(_){}
    if(!list){ g.innerHTML='<div class="empty">Couldn’t load this user’s files.</div>'; return; }
    if(!list.length){ g.innerHTML='<div class="empty">No files.</div>'; return; }
    g.innerHTML = list.map(b=>{
      const full=server+'/'+b.sha256, thumb=full+'?thumb=1', t=(b.type||'').toLowerCase(), sz=_fmtBytes(b.size||0);
      const isImg=t.startsWith('image/'), isVid=t.startsWith('video/');
      let inner;
      if(isImg||isVid) inner=`<img src="${enc(thumb)}" loading="lazy" style="width:100%;height:100%;object-fit:cover" onerror="this.style.display='none'">`+(isVid?`<span style="position:absolute;top:4px;left:4px;font-size:14px;text-shadow:0 0 3px #000">▶</span>`:'');
      else inner=`<div style="display:flex;align-items:center;justify-content:center;height:100%;font-size:24px">📄</div>`;
      return `<a href="${enc(full)}" target="_blank" rel="noopener" title="${enc(t)} · ${sz}" style="position:relative;aspect-ratio:1;display:block;border-radius:8px;overflow:hidden;background:var(--panel,#16161c);border:1px solid var(--border,#333)">${inner}<span style="position:absolute;bottom:0;left:0;right:0;background:rgba(0,0,0,.6);color:#fff;font-size:10px;padding:1px 4px">${sz}</span></a>`;
    }).join('');
  }
  // Folder index — the folder tree + each file's {name,folder,...}. One encrypted doc under the storage
  // key (cross-device, survives PWA reinstalls), cached in localStorage for instant render. Blossom is
  // flat/content-addressed, so foldering is this client-side overlay keyed by blob sha256.
  const FilesIdx = {
    data: { folders: ['Music'], files: {}, encFolders: [] }, _pulled:false, _pullDone:false, _t:null, mk:null, _mkWrapped:null, _batch:false, _lastIndexSha:null, _dirty:false, _saving:false,
    _key(){ return 'pc_files_idx_'+((ME&&ME.pubkey)||'anon'); },
    _norm(){ if(!this.data||typeof this.data!=='object') this.data={folders:['Music'],files:{},encFolders:[]};
      if(!Array.isArray(this.data.folders)) this.data.folders=['Music'];
      if(!this.data.files||typeof this.data.files!=='object') this.data.files={};
      if(!Array.isArray(this.data.encFolders)) this.data.encFolders=[];   // names of encrypted folders
      if(!this.data.folders.includes('Music')) this.data.folders.unshift('Music'); return this.data; },
    loadLocal(){ try{ const d=JSON.parse(localStorage.getItem(this._key())||'null'); if(d) this.data=d; }catch(_){}
      try{ this._mkWrapped = localStorage.getItem(this._key()+'_mk') || this._mkWrapped; }catch(_){} return this._norm(); },
    saveLocal(){ this._norm(); try{ localStorage.setItem(this._key(), JSON.stringify(this.data)); if(this._mkWrapped) localStorage.setItem(this._key()+'_mk', this._mkWrapped); }catch(_){} },
    // The master key (AES-256) is generated once, NIP-44 self-wrapped, and kept in the index pointer.
    async _ensureMK(){
      if(this.mk) return this.mk;
      if(this._mkWrapped){ try{ this.mk=_b64u8(JSON.parse(await signer.nip44dec(ME.pubkey,this._mkWrapped)).k); return this.mk; }catch(_){} }
      this.mk=crypto.getRandomValues(new Uint8Array(32));
      this._mkWrapped=await signer.nip44enc(ME.pubkey, JSON.stringify({k:_u8b64(this.mk)})); this.saveLocal();
      return this.mk;
    },
    async pull(){
      try{ const auth=await sign(27235,'files-index',[['p',ME.pubkey]]);
        const r=await fetch('/client/files-index',{method:'POST',headers:{'Content-Type':'application/json'},
          body:JSON.stringify({pubkey:ME.pubkey,auth:btoa(JSON.stringify(auth))})}).then(r=>r.json());
        const ptr=r&&r.ok&&r.index;
        if(ptr&&typeof ptr==='object'){
          if(ptr.mk) this._mkWrapped=ptr.mk;
          let idx=null;
          if(ptr.indexSha){                         // v2: index lives in an encrypted Blossom blob (scales to 1000s)
            this._lastIndexSha=ptr.indexSha;        // remember it so the NEXT save GCs this superseded blob
            await this._ensureMK();                 // (without this, every session leaked its old index blob)
            const br=await fetch(mediaServer()+'/'+ptr.indexSha);
            if(br.ok){ const d=JSON.parse(new TextDecoder().decode(await _masterDecrypt(this.mk, new Uint8Array(await br.arrayBuffer()))));
              if(d&&d.files) idx=d; }
          } else if(ptr.files){ idx=ptr; }          // v1: small index stored inline in the pointer
          // Don't clobber edits made WHILE this (possibly slow blob-fetch) pull was in flight — local is
          // newer and syncs on the next save. Without this, creating a folder + uploading during the
          // initial load got wiped: the files lost their metadata and vanished / showed as octet-stream.
          if(idx && !this._dirty && !this._saving){ this.data=idx; this.saveLocal(); }
        }
        this._pullDone=true;   // only AFTER a successful pull is it safe to GC orphan index blobs (we now
                               // know _lastIndexSha + the real file metadata — see _gcOrphanIndexBlobs)
      }catch(_){}
      return this._norm();
    },
    push(){ this._dirty=true; this.saveLocal(); if(this._batch) return; clearTimeout(this._t); this._t=setTimeout(()=>this._save(), 900); },
    async _save(){
      this._saving=true;   // while a save's index-blob upload + POST is in flight the server is NOT yet
                           // up to date — pull() must not apply stale server data during this window (it
                           // would wipe the very file being saved). Cleared in finally.
      try{ this._norm();
        this._dirty=false;   // capture point: edits AFTER this re-mark dirty (and reschedule) so pull won't clobber them
        const idx={folders:this.data.folders, files:this.data.files, encFolders:this.data.encFolders}; const json=JSON.stringify(idx);
        const ptr={}; if(this._mkWrapped) ptr.mk=this._mkWrapped;
        if(json.length < 45000){ ptr.folders=idx.folders; ptr.files=idx.files; ptr.encFolders=idx.encFolders; }   // small → inline (NIP-44 doc)
        else {                                                                       // large → encrypted Blossom blob
          const mk=await this._ensureMK(); ptr.mk=this._mkWrapped;
          const url=await uploadBlob(new File([await _masterEncrypt(mk, new TextEncoder().encode(json))],'files-index.enc',{type:'application/octet-stream'}), {noMirror:true});
          ptr.indexSha=_shaFromUrl(url);
        }
        const auth=await sign(27235,'files-index',[['p',ME.pubkey]]);
        await fetch('/client/files-index',{method:'POST',headers:{'Content-Type':'application/json'},
          body:JSON.stringify({pubkey:ME.pubkey,auth:btoa(JSON.stringify(auth)),index:ptr})});
        if(ptr.indexSha && this._lastIndexSha && this._lastIndexSha!==ptr.indexSha) _delBlobSilent(this._lastIndexSha);   // GC the superseded index blob
        if(ptr.indexSha) this._lastIndexSha=ptr.indexSha;
      }catch(e){ console.warn('files-index save failed', e); }
      finally{ this._saving=false; }
    },
    beginBatch(){ this._batch=true; },
    async endBatch(){ this._batch=false; await this._save(); },
    folders(){ return this._norm().folders; },
    isEncFolder(name){ return name==='Music' || this._norm().encFolders.includes(name); },   // Music is always encrypted
    addFolder(name, enc){ name=(name||'').trim().slice(0,40); if(!name||this._norm().folders.includes(name)) return false; this.data.folders.push(name); if(enc&&!this.data.encFolders.includes(name)) this.data.encFolders.push(name); this.push(); return true; },
    removeFolder(name){ this._norm(); if(name==='Music'||!name) return false; this.data.folders=this.data.folders.filter(f=>f!==name); this.data.encFolders=this.data.encFolders.filter(f=>f!==name); for(const sha in this.data.files){ if(this.data.files[sha].folder===name) this.data.files[sha].folder=''; } this.push(); return true; },
    meta(sha){ return this._norm().files[sha]||null; },
    folderOf(sha){ const m=this._norm().files[sha]; return (m&&m.folder)||''; },
    setFile(sha, m){ this._norm(); this.data.files[sha]=Object.assign(this.data.files[sha]||{}, m); this.push(); },
    move(sha, folder){ this._norm(); this.data.files[sha]=Object.assign(this.data.files[sha]||{}, {folder}); this.push(); },
    forget(sha){ this._norm(); delete this.data.files[sha]; this.push(); },
  };
  function _shaFromUrl(url){ const m=String(url||'').match(/([0-9a-f]{64})/i); return m?m[1].toLowerCase():''; }
  let _filesFolder = '';   // current folder ('' = All)
  // Pagination for the FILES GRID only (NOT the Music list — the player needs the whole queue). Render
  // a page at a time so a big folder doesn't fire hundreds of thumbnail requests (CPU) at once.
  const _FILES_PAGE = 60;
  let _filesShown = _FILES_PAGE, _filesShownFolder = null;
  // Public tab — your Blossom blobs, organised into client-side folders. Drag-drop + folders + grid.
  async function renderPublicFiles(pane){
    const server=mediaServer();
    if(!server){ pane.innerHTML='<div class="empty">Blossom server not configured.</div>'; return; }
    FilesIdx.loadLocal();
    if(!FilesIdx._pulled){ FilesIdx._pulled=true; FilesIdx.pull().then(()=>{ if(VIEW==='blossom') renderBlossom(); }); }
    pane.innerHTML='<div class="spinner"></div>';
    const canUp=await blossomCanUpload();
    const folders=FilesIdx.folders();
    const folderBar = `<div class="folder-bar">
        <button class="folder-chip${_filesFolder===''?' active':''}" data-folder="">🗂 All</button>
        ${folders.map(f=>`<button class="folder-chip${_filesFolder===f?' active':''}" data-folder="${enc(f)}">${f==='Music'?'🎵':(FilesIdx.isEncFolder(f)?'🔒':'📁')} ${enc(f)}</button>`).join('')}
        <button class="folder-chip newfolder" id="bl-newfolder">＋ New folder</button>
        ${(_filesFolder && _filesFolder!=='Music') ? `<button class="folder-chip delfolder" id="bl-delfolder" title="Delete this folder">🗑 Delete “${enc(_filesFolder)}”</button>` : ''}
      </div>`;
    const head = canUp
      ? `${folderBar}<div class="drop-zone" id="bl-drop"><input type="file" id="bl-file" multiple ${_filesFolder==='Music'?'accept="audio/*"':''} hidden><input type="file" id="bl-folder" webkitdirectory hidden>
          <div class="dz-inner"><span class="dz-ic">⬆</span> Drop files/folders here, or <button class="btn btn-cyan small" id="bl-pick">choose files</button> <button class="btn btn-neon small" id="bl-pickfolder">📁 choose folder</button>
          <div class="muted small">→ ${_filesFolder?((FilesIdx.isEncFolder(_filesFolder)?'🔒 ':'📁 ')+enc(_filesFolder)):'All files'} · uploaded one at a time${_filesFolder==='Music'?' · non-audio skipped':(FilesIdx.isEncFolder(_filesFolder)?' · encrypted on this device':'')}</div></div>
          <div class="up-queue" id="bl-queue"></div></div>`
      : `${folderBar}<div class="blossom-locked glass"><b>🔒 Upload access needed</b>
           <p class="muted small">You don't have permission to upload files to this server yet. Request access and the admin can grant it from Admin → Users.</p>
           <button class="btn btn-cyan" id="bl-request">🌸 Request upload access</button></div>`;
    pane.innerHTML = head + '<div class="files-grid" id="bl-grid"><div class="spinner"></div></div>';
    $$('.folder-chip[data-folder]',pane).forEach(b=> b.onclick=()=>{ _filesFolder=b.dataset.folder; renderBlossom(); });
    { const nf=$('#bl-newfolder',pane); if(nf) nf.onclick=_newFolderModal; }
    { const df=$('#bl-delfolder',pane); if(df) df.onclick=()=>{ if(confirm('Delete folder “'+_filesFolder+'”? Its files move to All — the files themselves aren\'t deleted.')){ FilesIdx.removeFolder(_filesFolder); _filesFolder=''; renderBlossom(); } }; }
    if(canUp){
      const fileInput=$('#bl-file',pane), folderInput=$('#bl-folder',pane), drop=$('#bl-drop',pane);
      $('#bl-pick',pane).onclick=()=>fileInput.click();
      { const fb=$('#bl-pickfolder',pane); if(fb) fb.onclick=()=>folderInput&&folderInput.click(); }
      fileInput.onchange=()=>{ const fs=[...fileInput.files]; fileInput.value=''; uploadFilesSeq(fs); };
      if(folderInput){ try{ folderInput.webkitdirectory=true; folderInput.setAttribute('webkitdirectory',''); folderInput.setAttribute('directory',''); }catch(_){}
        folderInput.onchange=()=>{ const fs=[...folderInput.files]; folderInput.value=''; uploadFilesSeq(fs); }; }
      drop.ondragover=e=>{ if(e.dataTransfer&&[...(e.dataTransfer.types||[])].includes('Files')){ e.preventDefault(); drop.classList.add('over'); } };
      drop.ondragleave=()=>drop.classList.remove('over');
      drop.ondrop=e=>{ e.preventDefault(); drop.classList.remove('over');
        const dt=e.dataTransfer, items=dt&&dt.items, entries=[];
        // capture FileSystem entries SYNCHRONOUSLY (invalid after the event) so dropped FOLDERS recurse
        if(items&&items.length&&items[0].webkitGetAsEntry){ for(let i=0;i<items.length;i++){ const en=items[i].webkitGetAsEntry(); if(en) entries.push(en); } }
        if(entries.length){ _walkEntries(entries).then(fs=>{ if(fs.length) uploadFilesSeq(fs); }); }
        else { const fs=[...((dt&&dt.files)||[])]; if(fs.length) uploadFilesSeq(fs); }
      };
    } else { const rb=$('#bl-request',pane); if(rb) rb.onclick=()=>requestBlossomAccess(rb); }
    let list=null;
    try{ const r=await fetch(server+'/list/'+ME.pubkey); if(!r.ok) throw new Error('HTTP '+r.status); list=await r.json(); }
    catch(e){ const g=$('#bl-grid',pane); if(g) g.innerHTML='<div class="empty">Couldn\'t load files from '+enc(server)+' ('+enc(e.message)+').</div>'; }
    if(list!==null){ if(_filesFolder==='Music') _renderMusicList($('#bl-grid',pane), list); else _renderFilesGrid($('#bl-grid',pane), list); _gcOrphanIndexBlobs(list); }
  }
  function _renderFilesGrid(grid, list){
    if(!grid) return;
    // hide encrypted MUSIC ciphertext from the normal grid (it lives in the Music folder's track list);
    // encrypted files in other folders DO show, as lock cards that decrypt in-browser on open.
    const inFolder = list.filter(b=>{
      if(b.sha256===FilesIdx._lastIndexSha) return false;                      // the encrypted Files index blob itself
      const m=FilesIdx.meta(b.sha256);
      if(!m && /octet-stream/.test(b.type||'')) return false;                  // stale index blobs / unnamed binaries (the "OCTET-STE" noise)
      if(m && m.enc && FilesIdx.folderOf(b.sha256)==='Music') return false;    // music ciphertext → Music list only
      return _filesFolder==='' ? true : FilesIdx.folderOf(b.sha256)===_filesFolder;
    });
    if(_filesShownFolder!==_filesFolder){ _filesShownFolder=_filesFolder; _filesShown=_FILES_PAGE; }   // reset paging on folder change
    const _shown = inFolder.slice(0, _filesShown), _more = inFolder.length - _shown.length;
    grid.innerHTML = inFolder.length ? (_shown.map(b=>{
      const m=FilesIdx.meta(b.sha256)||{}; const nm=m.name||'';
      if(m.enc){   // encrypted file — lock card; opening decrypts in-browser (never exposes the ciphertext URL)
        const ext=((m.mime||'').split('/')[1]||'enc').slice(0,10);
        return `<div class="file-card enc" draggable="true" data-sha="${b.sha256}"><a href="#" class="enc-open" data-sha="${b.sha256}"><div class="file-icon">🔒<span>${enc(ext)}</span></div></a>
          <button class="del" data-sha="${b.sha256}">✕</button>
          <div class="meta"><span title="${enc(nm)}">${nm?enc(nm.slice(0,18)):'encrypted'}</span><button class="movebtn" data-sha="${b.sha256}" title="Move to folder">📁</button></div></div>`;
      }
      return `<div class="file-card" draggable="true" data-sha="${b.sha256}"><a href="${enc(b.url)}" data-mime="${enc(b.type||'')}" target="_blank">${blobThumb(b)}</a>
        <button class="copy" data-url="${enc(b.url)}" title="Copy URL">⧉</button><button class="del" data-sha="${b.sha256}">✕</button>
        <div class="meta"><span title="${enc(nm)}">${nm?enc(nm.slice(0,18)):(((b.size||0)/1024|0)+'KB')}</span><button class="movebtn" data-sha="${b.sha256}" title="Move to folder">📁</button></div></div>`;
    }).join('') + (_more>0 ? `<button class="btn btn-ghost bl-more" style="grid-column:1/-1;margin:10px auto;display:block">↓ Load ${Math.min(_more,_FILES_PAGE)} more · ${_more} left</button>` : '')) : '<div class="empty">No files'+(_filesFolder?(' in '+enc(_filesFolder)):'')+' yet — drop some above.</div>';
    { const mb=$('.bl-more',grid); if(mb) mb.onclick=()=>{ _filesShown+=_FILES_PAGE; _renderFilesGrid(grid, list); }; }
    $$('.enc-open',grid).forEach(a=> a.onclick=async e=>{ e.preventDefault(); try{ toast('decrypting…'); const u=await trackUrl(a.dataset.sha); window.open(u,'_blank'); }catch(err){ toast('decrypt failed: '+(err.message||'')); } });
    $$('.vthumb',grid).forEach(im=> im.onerror=()=>{ const d=document.createElement('div'); d.className='file-icon'; d.innerHTML='🎬<span>'+enc(im.dataset.ext||'video')+'</span>'; im.replaceWith(d); });
    $$('.del',grid).forEach(b=> b.onclick=()=>delBlob(b.dataset.sha));
    $$('.copy',grid).forEach(b=> b.onclick=()=>copyUrl(b.dataset.url));
    $$('.movebtn',grid).forEach(b=> b.onclick=(e)=>_moveMenu(e.currentTarget, b.dataset.sha));
    $$('.file-card',grid).forEach(card=> card.ondragstart=e=>{ if(e.dataTransfer) e.dataTransfer.setData('text/sha', card.dataset.sha); });
    $$('.folder-chip[data-folder]').forEach(chip=>{
      chip.ondragover=e=>{ e.preventDefault(); chip.classList.add('drop'); };
      chip.ondragleave=()=>chip.classList.remove('drop');
      chip.ondrop=e=>{ e.preventDefault(); chip.classList.remove('drop'); const sha=e.dataTransfer&&e.dataTransfer.getData('text/sha'); if(sha){ FilesIdx.move(sha, chip.dataset.folder); toast('moved to '+(chip.dataset.folder||'All')); renderBlossom(); } };
    });
  }
  function _moveMenu(anchor, sha){
    const opts=[['__all','🗂 All']].concat(FilesIdx.folders().map(f=>[f,(f==='Music'?'🎵 ':'📁 ')+f]));
    openMenuPopover(anchor, opts, v=>{ FilesIdx.move(sha, v==='__all'?'':v); toast('moved'); renderBlossom(); });
  }
  // Upload a batch ONE AT A TIME (sequential), into the current folder, with a per-file progress queue.
  // In the Music folder, audio files go through the compress→encrypt pipeline; everything else uploads
  // straight to Blossom.
  // Recurse dropped folders → a flat File[] (FileSystem entries captured synchronously from the drop).
  async function _walkEntries(entries){
    const out=[];
    async function walk(entry){
      if(entry.isFile){ await new Promise(res=>entry.file(f=>{ out.push(f); res(); }, ()=>res())); }
      else if(entry.isDirectory){ const reader=entry.createReader();
        await new Promise(res=>{ const read=()=>reader.readEntries(async ents=>{ if(!ents.length){ res(); return; } for(const en of ents){ await walk(en); } read(); }, ()=>res()); read(); }); }
    }
    for(const en of entries){ await walk(en); }
    return out;
  }
  // Persistent floating upload progress (appended to <html> so it survives view changes) + a stop button.
  // Generic drag (mouse + touch) for a fixed-position widget; `noDrag` = selector to ignore (buttons etc.)
  function _makeDraggable(el, handle, noDrag){
    if(!el||!handle) return; let sx,sy,ox,oy,on=false;
    const move=e=>{ if(!on) return; const p=e.touches?e.touches[0]:e; el.style.right='auto'; el.style.bottom='auto'; el.style.left=Math.max(0,Math.min(innerWidth-50,ox+p.clientX-sx))+'px'; el.style.top=Math.max(0,Math.min(innerHeight-40,oy+p.clientY-sy))+'px'; if(e.cancelable)e.preventDefault(); };
    const up=()=>{ on=false; removeEventListener('mousemove',move); removeEventListener('mouseup',up); removeEventListener('touchmove',move); removeEventListener('touchend',up); };
    const down=e=>{ if(e.target.closest('button'+(noDrag?(','+noDrag):''))) return; const p=e.touches?e.touches[0]:e; const r=el.getBoundingClientRect(); on=true; sx=p.clientX; sy=p.clientY; ox=r.left; oy=r.top; el.style.right='auto'; el.style.bottom='auto'; el.style.left=ox+'px'; el.style.top=oy+'px'; addEventListener('mousemove',move); addEventListener('mouseup',up); addEventListener('touchmove',move,{passive:false}); addEventListener('touchend',up); };
    handle.onmousedown=down; handle.ontouchstart=down;
  }
  let _uploadCancel=false, _uploadBadgeT=0;
  function _uploadBadge(text, done){
    clearTimeout(_uploadBadgeT);   // a new update cancels a pending auto-remove (so it won't wipe a fresh upload)
    let b=document.getElementById('upload-badge');
    if(text===null){ if(b) b.remove(); return; }
    if(!b){ b=document.createElement('div'); b.id='upload-badge'; document.documentElement.appendChild(b); _makeDraggable(b, b, '.upbadge-x'); }
    b.className='upbadge'+(done?' done':'');
    b.innerHTML=`<span class="upbadge-ic">${done?'✅':'⬆'}</span><span class="upbadge-txt">${enc(text)}</span><span class="upbadge-x" title="${done?'dismiss':'stop'}">✕</span>`;
    const x=b.querySelector('.upbadge-x'); if(x) x.onclick=()=>{ if(done){ _uploadBadge(null); } else { _uploadCancel=true; const t=b.querySelector('.upbadge-txt'); if(t) t.textContent='stopping…'; } };
    if(done) _uploadBadgeT=setTimeout(()=>_uploadBadge(null), 12000);
  }
  // New-folder dialog: name + Encrypted/Public. An encrypted folder AES-encrypts everything dropped in
  // it (client-side, under your master key) before upload — Blossom only ever sees ciphertext.
  function _newFolderModal(){
    modal(`<h3>📁 New folder</h3>
      <label class="fld">Name<input class="input" id="nf-name" placeholder="Folder name" maxlength="40"></label>
      <div class="fld">Contents
        <label class="nf-opt"><input type="radio" name="nf-enc" value="0" checked> 🌐 <b>Public</b><span class="muted small"> — files upload as-is, shareable by URL</span></label>
        <label class="nf-opt"><input type="radio" name="nf-enc" value="1"> 🔒 <b>Encrypted</b><span class="muted small"> — encrypted on this device; only you can open them</span></label>
      </div>
      <div class="row" style="justify-content:flex-end;gap:8px;margin-top:14px"><button class="btn btn-ghost small" id="nf-cancel">Cancel</button><button class="btn btn-neon small" id="nf-create">Create</button></div>`,
      root=>{
        const nm=$('#nf-name',root); if(nm) nm.focus();
        const c=$('#nf-cancel',root); if(c) c.onclick=closeModal;
        const go=()=>{ const name=((nm&&nm.value)||'').trim().slice(0,40); if(!name){ toast('enter a name'); return; }
          const isEnc=(($('input[name="nf-enc"]:checked',root)||{}).value==='1');
          if(FilesIdx.addFolder(name, isEnc)){ _filesFolder=name; closeModal(); renderBlossom(); } else toast('folder exists'); };
        const g=$('#nf-create',root); if(g) g.onclick=go;
        if(nm) nm.addEventListener('keydown',e=>{ if(e.key==='Enter') go(); });
      });
  }
  // Encrypt ANY file with the master key (IV from content → identical input dedups), upload the
  // ciphertext (noMirror), and record it like a music track so trackUrl() can decrypt it on open.
  async function uploadEncFile(file, folder, statEl){
    if(!signer.nip44enc) throw new Error("signer can't encrypt (needs NIP-44)");
    const setS=t=>{ if(statEl) statEl.textContent=t; };
    const mk=await FilesIdx._ensureMK();
    const buf=new Uint8Array(await file.arrayBuffer());
    setS('encrypting…');
    const blob=await _masterEncrypt(mk, buf, await _contentIV(buf));
    setS('uploading…');
    const url=await uploadBlob(new File([blob],(file.name||'file')+'.enc',{type:'application/octet-stream'}), {noMirror:true});
    const sha=_shaFromUrl(url); if(!sha) throw new Error('upload returned no hash');
    FilesIdx.setFile(sha,{name:file.name||'file', folder, mime:file.type||'application/octet-stream', enc:true, mk:true, size:buf.length, ts:Math.floor(Date.now()/1000)});
  }
  async function uploadFilesSeq(files){
    files=files.filter(Boolean); if(!files.length) return;
    const folder=_filesFolder, music=folder==='Music';   // capture: navigating mid-upload won't misfile
    // FAIL-CLOSED: never upload into a NAMED folder before the index has loaded. If the folder's
    // encrypted flag isn't known yet, uploading would silently take the PLAINTEXT path and put a
    // world-readable blob on Blossom (the leaked-file bug). Refuse until we know the folder's status.
    if(!music && folder && !FilesIdx._pullDone){ toast('One sec — still loading your folders. Try that again in a moment.'); return; }
    _uploadCancel=false;
    const encFolder=!music && FilesIdx.isEncFolder(folder);   // non-Music encrypted folder → encrypt every file
    const big=files.length>20;   // a folder import → compact summary, not 2000 DOM rows
    const q=$('#bl-queue');
    if(q) q.innerHTML = big ? `<div class="up-summary" id="up-sum">Preparing ${files.length} files…</div>`
      : files.map((f,i)=>`<div class="up-item"><span class="up-name">${enc(f.name)}</span><span class="up-stat" id="up-stat-${i}">queued</span></div>`).join('');
    FilesIdx.beginBatch();   // collapse the index save (a 2000-file import must NOT re-save the index per file)
    let done=0, ok=0, skip=0, fail=0;
    for(let i=0;i<files.length;i++){
      if(_uploadCancel) break;
      const stat=big?null:$('#up-stat-'+i);
      try{
        if(music){
          if(!(files[i].type||'').startsWith('audio/')){ skip++; if(stat) stat.textContent='skipped (not audio)'; }
          else if(_musicHasSrc(files[i])){ skip++; if(stat){ stat.textContent='already imported ✓'; stat.className='up-stat ok'; } }   // resume
          else { await uploadMusicTrack(files[i], stat); ok++; if(stat){ stat.textContent='✓'; stat.className='up-stat ok'; }
            if(++done%25===0){ await FilesIdx.endBatch(); FilesIdx.beginBatch(); } }   // checkpoint so a crash keeps progress
        } else if(encFolder){
          await uploadEncFile(files[i], folder, stat);
          ok++; if(stat){ stat.textContent='🔒'; stat.className='up-stat ok'; }
          if(++done%25===0){ await FilesIdx.endBatch(); FilesIdx.beginBatch(); }
        } else {
          if(stat) stat.textContent='uploading…';
          const url=await uploadBlob(files[i]); const sha=_shaFromUrl(url);
          if(sha) FilesIdx.setFile(sha, {name:files[i].name, folder, mime:files[i].type||'', size:files[i].size, ts:Math.floor(Date.now()/1000)});
          ok++; if(stat){ stat.textContent='✓'; stat.className='up-stat ok'; }
          if(++done%25===0){ await FilesIdx.endBatch(); FilesIdx.beginBatch(); }
        }
      }catch(e){ fail++; if(_blossomDenied(e)) requestBlossomAccess(); if(stat){ stat.textContent='✗'; stat.className='up-stat err'; stat.title=e.message||'failed'; } }
      const prog=`${i+1} / ${files.length} · ✓${ok}${skip?' ⏭'+skip:''}${fail?' ✗'+fail:''}`;
      _uploadBadge('Uploading '+prog);   // persists across views
      if(big){ const s=$('#up-sum'); if(s) s.textContent='Uploading… '+prog; }
    }
    await FilesIdx.endBatch();
    const summary=`${_uploadCancel?'Stopped':'Done'} — ✓ ${ok} added${skip?(' · ⏭ '+skip+' skipped'):''}${fail?(' · ✗ '+fail+' failed'):''}`;
    _uploadBadge(summary, true);   // self-removes after 12s (timer lives in _uploadBadge)
    if(big&&q){ const s=$('#up-sum'); if(s) s.textContent=summary; }
    toast(summary);
    setTimeout(()=>{ if(VIEW==='blossom') renderBlossom(); }, 700);
  }

  // ---- Music: Opus-compressed + AES-256-GCM-encrypted tracks in the Music folder ----------------------
  // raw audio → server Opus transcode (compression) → AES-GCM encrypt (random per-file key) → upload the
  // CIPHERTEXT to the user's Blossom → store the NIP-44 self-wrapped key in the index. Playback fetches
  // the ciphertext, unwraps the key, decrypts in-browser → object URL. So Blossom only ever holds opaque
  // ciphertext; only the owner's signer can unwrap it.
  function _u8b64(u8){ let s='',C=0x8000; for(let i=0;i<u8.length;i+=C) s+=String.fromCharCode.apply(null,u8.subarray(i,i+C)); return btoa(s); }
  function _b64u8(b){ const s=atob(b),u=new Uint8Array(s.length); for(let i=0;i<s.length;i++) u[i]=s.charCodeAt(i); return u; }
  async function _aesEncrypt(plain){ const key=crypto.getRandomValues(new Uint8Array(32)),iv=crypto.getRandomValues(new Uint8Array(12));
    const ck=await crypto.subtle.importKey('raw',key,'AES-GCM',false,['encrypt']);
    const ct=new Uint8Array(await crypto.subtle.encrypt({name:'AES-GCM',iv},ck,plain)); return {ct,key,iv}; }
  async function _aesDecrypt(ct,key,iv){ const ck=await crypto.subtle.importKey('raw',key,'AES-GCM',false,['decrypt']);
    return new Uint8Array(await crypto.subtle.decrypt({name:'AES-GCM',iv},ck,ct)); }
  // SCALABLE encryption (Phase 2.5): ONE master key (wrapped once) + the IV prepended to the blob. For
  // tracks the IV is DERIVED from the content (sha256(plain)[:12]) → identical input → identical
  // ciphertext/hash → Blossom DEDUP + resumable import. For the index a random IV is used (it changes).
  async function _contentIV(plain){ return new Uint8Array(await crypto.subtle.digest('SHA-256', plain)).slice(0,12); }
  async function _masterEncrypt(mk, plain, iv){ iv = iv || crypto.getRandomValues(new Uint8Array(12));
    const ck=await crypto.subtle.importKey('raw',mk,'AES-GCM',false,['encrypt']);
    const ct=new Uint8Array(await crypto.subtle.encrypt({name:'AES-GCM',iv},ck,plain));
    const out=new Uint8Array(12+ct.length); out.set(iv,0); out.set(ct,12); return out; }
  async function _masterDecrypt(mk, blob){ const iv=blob.slice(0,12), ct=blob.slice(12);
    const ck=await crypto.subtle.importKey('raw',mk,'AES-GCM',false,['decrypt']);
    return new Uint8Array(await crypto.subtle.decrypt({name:'AES-GCM',iv},ck,ct)); }
  // Already-imported check (resume a bulk import): match a source file by name+size.
  function _musicHasSrc(file){ const fs=FilesIdx._norm().files; for(const sha in fs){ const m=fs[sha]; if(m&&m.folder==='Music'&&m.srcName===file.name&&m.srcSize===file.size) return true; } return false; }
  async function uploadMusicTrack(file, statEl){
    if(!signer.nip44enc) throw new Error('signer can\'t encrypt (needs NIP-44)');
    const setS=t=>{ if(statEl) statEl.textContent=t; };
    const mk=await FilesIdx._ensureMK();
    setS('compressing…');
    const auth=await sign(27235,'music',[['p',ME.pubkey]]);
    const cr=await fetch('/client/music-compress',{method:'POST',headers:{'X-Pubkey':ME.pubkey,'X-Auth':btoa(JSON.stringify(auth))},body:file});
    if(!cr.ok){ let m='compress failed'; try{ m=(await cr.json()).error||m; }catch(_){} throw new Error(m); }
    const opus=new Uint8Array(await cr.arrayBuffer());
    setS('encrypting…');
    const blob=await _masterEncrypt(mk, opus, await _contentIV(opus));   // deterministic IV → identical hash → dedup
    setS('uploading…');
    // noMirror: never DR-mirror encrypted music to the public backup servers (bandwidth/abuse).
    const url=await uploadBlob(new File([blob],(file.name||'track')+'.enc',{type:'application/octet-stream'}), {noMirror:true});
    const sha=_shaFromUrl(url); if(!sha) throw new Error('upload returned no hash');
    FilesIdx.setFile(sha,{name:(file.name||'track').replace(/\.[^.]+$/,''),folder:'Music',mime:'audio/ogg',enc:true,mk:true,size:opus.length,srcName:file.name,srcSize:file.size,ts:Math.floor(Date.now()/1000)});
  }
  // One-time-per-session cleanup of leaked Files-index blobs (the old cross-session GC bug left stale
  // encrypted index blobs on Blossom — the "OCTET-STE" files filling the drive). A blob qualifies only
  // if it has NO file metadata, is octet-stream, small, AND decrypts with the master key to an object
  // shaped like an index ({files, folders}) — so it can never touch a real user file (those have meta,
  // and random/other ciphertext fails AES-GCM auth and is skipped).
  let _idxGcDone=false;
  async function _gcOrphanIndexBlobs(list){
    // CRITICAL: wait until pull() finished. Before it does, _lastIndexSha is null and FilesIdx.files is
    // empty, so the user's LIVE index blob (which is {files,folders}-shaped) would not be excluded and
    // would be deleted, destroying the whole index. Re-runs after pull's .then() re-renders.
    if(_idxGcDone || !FilesIdx._pullDone) return;
    _idxGcDone=true;
    try{
      const cur=FilesIdx._lastIndexSha;
      // Index blobs are small JSON ciphertext — only consider small octet-stream blobs, and cap tight
      // (fetch+AES-decrypt per candidate is CPU; don't churn through big media on every session).
      const cands=(list||[]).filter(b=> b.sha256!==cur && !FilesIdx.meta(b.sha256) && /octet-stream/.test(b.type||'') && (b.size||0)<512*1024).slice(0,8);
      if(!cands.length) return;
      const mk=await FilesIdx._ensureMK(); if(!mk) return;
      for(const b of cands){
        try{
          const r=await fetch(mediaServer()+'/'+b.sha256); if(!r.ok) continue;
          let obj=null; try{ obj=JSON.parse(new TextDecoder().decode(await _masterDecrypt(mk, new Uint8Array(await r.arrayBuffer())))); }catch(_){ continue; }
          if(obj && typeof obj==='object' && obj.files && obj.folders) await _delBlobSilent(b.sha256);   // a stale Files index → reclaim it
        }catch(_){}
      }
    }catch(_){}
  }
  const _trackUrls={}, _trackUrlOrder=[];   // sha -> decrypted object URL (LRU-capped so a long session doesn't leak)
  async function _delBlobSilent(sha){ try{ const server=mediaServer(); const auth=await sign(24242,'Delete blob',[['t','delete'],['x',sha],['expiration',String(Math.floor(Date.now()/1000)+3600)]]);
    await fetch(server+'/'+sha,{method:'DELETE',headers:{'Authorization':'Nostr '+btoa(JSON.stringify(auth))}}); }catch(_){} }
  async function trackUrl(sha){
    if(_trackUrls[sha]) return _trackUrls[sha];
    const m=FilesIdx.meta(sha); if(!m||!m.enc) throw new Error('not an encrypted track');
    const r=await fetch(mediaServer()+'/'+sha); if(!r.ok) throw new Error('blob HTTP '+r.status);
    const blob=new Uint8Array(await r.arrayBuffer());
    let plain;
    if(m.mk){ plain=await _masterDecrypt(await FilesIdx._ensureMK(), blob); }            // v2 master-key (IV prepended)
    else if(m.keyenc){ const {k,iv}=JSON.parse(await signer.nip44dec(ME.pubkey,m.keyenc)); plain=await _aesDecrypt(blob,_b64u8(k),_b64u8(iv)); }  // v1 per-track key
    else throw new Error('no key');
    const u=URL.createObjectURL(new Blob([plain],{type:m.mime||'audio/ogg'})); _trackUrls[sha]=u; _trackUrlOrder.push(sha);
    while(_trackUrlOrder.length>6){ const old=_trackUrlOrder.shift(); if(old!==(MusicPlayer&&MusicPlayer.cur) && _trackUrls[old]){ URL.revokeObjectURL(_trackUrls[old]); delete _trackUrls[old]; } }
    return u;
  }
  function musicTracks(list){
    const have=list?new Set(list.map(b=>b.sha256)):null;
    return Object.keys(FilesIdx._norm().files)
      .filter(sha=> FilesIdx.folderOf(sha)==='Music' && FilesIdx.meta(sha).enc && (!have||have.has(sha)))
      .map(sha=>({sha, m:FilesIdx.meta(sha)})).sort((a,b)=>(b.m.ts||0)-(a.m.ts||0));
  }
  function _renderMusicList(grid, list){
    if(!grid) return;
    const tracks=musicTracks(list);
    grid.className='music-list';
    grid.innerHTML = tracks.length ? tracks.map(t=>`<div class="track" data-sha="${t.sha}">
        <button class="track-play" data-sha="${t.sha}">▶</button>
        <span class="track-name">${enc(t.m.name||'track')}</span>
        <span class="track-meta">🔒 ${(((t.m.size||0)/1048576)).toFixed(1)}MB</span>
        <button class="track-del" data-sha="${t.sha}" title="Delete">✕</button>
      </div>`).join('') : '<div class="empty">No music yet — drop audio files here. They\'re Opus-compressed + encrypted automatically.</div>';
    $$('.track-play',grid).forEach(b=> b.onclick=()=>MusicPlayer.play(b.dataset.sha));
    $$('.track-del',grid).forEach(b=> b.onclick=()=>delBlob(b.dataset.sha));
    _updateMusicListBtns();
  }
  function _fmtTime(s){ s=Math.floor(s||0); return Math.floor(s/60)+':'+String(s%60).padStart(2,'0'); }
  function _updateMusicListBtns(){ const playing=_audioEl&&!_audioEl.paused; $$('.track-play').forEach(b=> b.textContent=(b.dataset.sha===MusicPlayer.cur&&playing)?'⏸':'▶'); }
  // The floating cyberpunk player — a persistent widget appended to <body> (NOT #feed), so it hovers over
  // EVERY view and keeps playing as you navigate. Minimizable to a mini bar; draggable anywhere.
  let _audioEl=null;
  const MusicPlayer = {
    el:null, min:false, cur:null, queue:[], shuffle:false, _loading:false, _history:[], _search:'', _viz:{an:null,raf:0,failed:false},
    ensure(){
      if(this.el) return this.el;
      // append to <html> NOT <body>: body has zoom:.85 on desktop, which throws off a fixed body child's
      // position (it would mis-overlap the sidebar + block its clicks). Same fix as the popovers.
      const d=document.createElement('div'); d.id='music-player'; d.className='mp hidden'; document.documentElement.appendChild(d); this.el=d;
      if(!_audioEl) _audioEl=new Audio();
      _audioEl.ontimeupdate=()=>this._tick();
      _audioEl.onended=()=>this.next();
      _audioEl.onplay=()=>{ this._render(); this._startViz(); this._media(); };
      _audioEl.onpause=()=>{ this._render(); this._media(); };
      // Hardware / keyboard media keys (play/pause, ⏮/⏭) + OS lock-screen controls via MediaSession.
      if('mediaSession' in navigator){ const ms=navigator.mediaSession; try{
        ms.setActionHandler('play',          ()=>{ if(_audioEl) _audioEl.play(); });
        ms.setActionHandler('pause',         ()=>{ if(_audioEl) _audioEl.pause(); });
        ms.setActionHandler('previoustrack', ()=>this.prev());
        ms.setActionHandler('nexttrack',     ()=>this.next());
        ms.setActionHandler('stop',          ()=>this.close());
        try{ ms.setActionHandler('seekto', e=>{ if(_audioEl && _audioEl.duration && e.seekTime!=null) _audioEl.currentTime=e.seekTime; }); }catch(_){}
      }catch(_){} }
      return d;
    },
    _media(){
      // Push the current track + play state to the OS media UI so the media keys show the right song.
      if(!('mediaSession' in navigator)) return;
      try{
        const m=this.cur?FilesIdx.meta(this.cur):null;
        if(window.MediaMetadata) navigator.mediaSession.metadata=new MediaMetadata({
          title:(m&&m.name)||'Track', artist:'PosterChan', album:'Library',
          artwork:[{src:LOGO, sizes:'512x512', type:'image/png'}] });
        navigator.mediaSession.playbackState=(_audioEl && !_audioEl.paused)?'playing':'paused';
      }catch(_){}
    },
    refreshQueue(){ this.queue=musicTracks(null).map(t=>t.sha); if(this.cur && !this.queue.includes(this.cur)) this.queue.unshift(this.cur); },
    async play(sha, opts){
      this.ensure();
      if(sha===this.cur && _audioEl.src){ this.toggle(); return; }   // tapping the playing track = pause/resume
      // keep a STABLE play order — only (re)build the queue when it's empty or doesn't contain this track,
      // NOT on every track, so auto-advance plays in order instead of jumping around ("shuffling everything").
      if(!this.queue.length || !this.queue.includes(sha)) this.refreshQueue();
      if(!this.queue.includes(sha)) this.queue.unshift(sha);
      // remember the outgoing track so ⏮ returns to it (matters in shuffle — next() is random, so the
      // queue-order-previous isn't what you just heard). `opts.back` = we're navigating backward, don't record.
      if(this.cur && this.cur!==sha && !(opts&&opts.back)){ this._history.push(this.cur); if(this._history.length>200) this._history.shift(); }
      this.cur=sha; this._loading=true; this.el.classList.remove('hidden'); this._render();
      try{ const u=await trackUrl(sha);
        if(this.cur!==sha) return;   // a newer ⏭/⏮ superseded this load while we awaited the URL —
                                     // don't clobber _audioEl.src (that's the "skip plays the wrong song" bug)
        this._loading=false; _audioEl.src=u; await _audioEl.play(); }
      catch(e){ if(this.cur===sha){ this._loading=false; toast('play failed: '+(e.message||e)); } }
      if(this.cur===sha) this._render();
    },
    toggle(){ if(_audioEl){ if(_audioEl.paused) _audioEl.play(); else _audioEl.pause(); } },
    next(){ if(!this.queue.length) return; let i=this.queue.indexOf(this.cur);
      i=this.shuffle ? this._randIdx(i) : (i+1)%this.queue.length; this.play(this.queue[i]); },
    _randIdx(cur){ if(this.queue.length<2) return 0; let r; do{ r=Math.floor(Math.random()*this.queue.length); }while(r===cur); return r; },   // don't replay the same track
    prev(){ if(_audioEl && _audioEl.currentTime>3){ _audioEl.currentTime=0; return; }   // >3s in = restart current
      if(this._history.length){ this.play(this._history.pop(), {back:true}); return; }   // back to the track actually played before
      if(!this.queue.length) return; let i=this.queue.indexOf(this.cur); this.play(this.queue[(i-1+this.queue.length)%this.queue.length], {back:true}); },
    seekTo(f){ if(_audioEl && _audioEl.duration) _audioEl.currentTime=Math.max(0,Math.min(1,f))*_audioEl.duration; },
    setMin(m){ this.min=m; this._render(); },
    close(){ if(_audioEl) _audioEl.pause(); if(this.el) this.el.classList.add('hidden'); },
    _tick(){ if(!this.el||this.el.classList.contains('hidden')||this.min) return;
      const f=this.el.querySelector('.mp-seek-fill'), c=this.el.querySelector('.mp-cur'), du=this.el.querySelector('.mp-dur');
      if(_audioEl && _audioEl.duration){ if(f) f.style.width=((_audioEl.currentTime/_audioEl.duration*100)||0)+'%'; if(c) c.textContent=_fmtTime(_audioEl.currentTime); if(du) du.textContent=_fmtTime(_audioEl.duration); } },
    _render(){
      this.ensure(); const d=this.el; const m=this.cur?FilesIdx.meta(this.cur):null; const name=(m&&m.name)||'—';
      const playing=_audioEl && !_audioEl.paused; const pl=this._loading?'…':(playing?'⏸':'▶');
      if(this.min){
        d.className='mp mp-mini'+(playing?' playing':'');
        d.innerHTML=`<span class="mp-eq${playing?' on':''}">🎵</span><span class="mp-title" title="${enc(name)}">${enc(name)}</span><button class="mp-play">${pl}</button><button class="mp-exp" title="Expand">▢</button>`;
      } else {
        d.className='mp'+(playing?' playing':'');
        d.innerHTML=`<div class="mp-scan"></div><div class="mp-head"><span class="mp-logo">🎵 NEON PLAYER</span><button class="mp-min" title="Minimize">▁</button><button class="mp-close" title="Close">✕</button></div>
          <canvas class="mp-viz"></canvas>
          <div class="mp-now" title="${enc(name)}">${enc(name)}</div>
          <div class="mp-seek"><div class="mp-seek-fill"></div></div>
          <div class="mp-time"><span class="mp-cur">0:00</span><span class="mp-dur">0:00</span></div>
          <div class="mp-controls"><button class="mp-prev" title="Previous">⏮</button><button class="mp-play mp-big">${pl}</button><button class="mp-next" title="Next">⏭</button><button class="mp-shuffle${this.shuffle?' on':''}" title="Shuffle">🔀</button></div>
          <div class="mp-search-row"><input class="mp-search" type="search" placeholder="🔍 Search tracks…" value="${enc(this._search||'')}"></div>
          <div class="mp-list">${this._listHtml()}</div>`;
      }
      this._wire(); this._tick(); _updateMusicListBtns();
      if(!this.min && _audioEl && !_audioEl.paused) this._startViz();
    },
    _wire(){
      const d=this.el, qq=s=>d.querySelector(s), b=(s,fn)=>{ const e=qq(s); if(e) e.onclick=fn; };
      b('.mp-play',()=>this.toggle()); b('.mp-min',()=>this.setMin(true)); b('.mp-exp',()=>this.setMin(false));
      b('.mp-close',()=>this.close()); b('.mp-prev',()=>this.prev()); b('.mp-next',()=>this.next());
      b('.mp-shuffle',()=>{ this.shuffle=!this.shuffle; this._render(); });
      const seek=qq('.mp-seek'); if(seek) seek.onclick=e=>{ const r=seek.getBoundingClientRect(); this.seekTo((e.clientX-r.left)/r.width); };
      const srch=qq('.mp-search');
      if(srch){ srch.oninput=()=>{ this._search=srch.value; const lst=qq('.mp-list'); if(lst){ lst.innerHTML=this._listHtml(); this._wireList(); } };
        srch.onkeydown=e=>{ if(e.key==='Enter'){ const s=this._shownShas(); if(s.length) this.play(s[0]); } }; }   // Enter = play first match
      this._wireList();
      this._drag(this.min ? d : (qq('.mp-head')||d));   // minimized: the whole mini-bar is the drag handle
    },
    // Search the WHOLE Music library by name (not just the current queue); empty search = the queue.
    _libTracks(){ return musicTracks(null).map(t=>({sha:t.sha, name:(t.m&&t.m.name)||'track'})); },
    _shownShas(){ const q=(this._search||'').trim().toLowerCase();
      if(q) return this._libTracks().filter(t=>t.name.toLowerCase().includes(q)).map(t=>t.sha);
      return this.queue; },
    _listHtml(){ const playing=_audioEl && !_audioEl.paused; const shas=this._shownShas();
      if(!shas.length) return `<div class="muted small" style="padding:10px;text-align:center">${this._search?'No matches':'No tracks in Music yet'}</div>`;
      return shas.map(sha=>{ const mm=FilesIdx.meta(sha)||{}; return `<button class="mp-track${sha===this.cur?' on':''}" data-sha="${sha}"><span class="mp-tnum">${sha===this.cur&&playing?'▶':'♪'}</span><span>${enc(mm.name||'track')}</span></button>`; }).join(''); },
    _wireList(){ const d=this.el; if(!d) return; d.querySelectorAll('.mp-list .mp-track').forEach(t=> t.onclick=()=>this.play(t.dataset.sha)); },
    _drag(handle){ if(!handle) return; const d=this.el; let sx,sy,ox,oy,on=false;
      const move=e=>{ if(!on) return; const p=e.touches?e.touches[0]:e; d.style.left=Math.max(0,Math.min(innerWidth-50,ox+p.clientX-sx))+'px'; d.style.top=Math.max(0,Math.min(innerHeight-40,oy+p.clientY-sy))+'px'; if(e.cancelable)e.preventDefault(); };
      const up=()=>{ on=false; removeEventListener('mousemove',move); removeEventListener('mouseup',up); removeEventListener('touchmove',move); removeEventListener('touchend',up); };
      const down=e=>{ if(e.target.closest('button')) return; const p=e.touches?e.touches[0]:e; const r=d.getBoundingClientRect(); on=true; sx=p.clientX; sy=p.clientY; ox=r.left; oy=r.top; d.style.right='auto'; d.style.bottom='auto'; d.style.left=ox+'px'; d.style.top=oy+'px'; addEventListener('mousemove',move); addEventListener('mouseup',up); addEventListener('touchmove',move,{passive:false}); addEventListener('touchend',up); };
      handle.onmousedown=down; handle.ontouchstart=down; },
    _startViz(){ if(this._setupViz()){ try{ this._viz.ctx.resume(); }catch(_){} this._drawViz(); } },
    _setupViz(){ const v=this._viz; if(v.an) return true; if(v.failed) return false;
      try{ const AC=window.AudioContext||window.webkitAudioContext; if(!AC){ v.failed=true; return false; }
        v.ctx=new AC(); v.src=v.ctx.createMediaElementSource(_audioEl); v.an=v.ctx.createAnalyser();
        v.an.fftSize=128; v.an.smoothingTimeConstant=0.82; v.src.connect(v.an); v.an.connect(v.ctx.destination); return true;
      }catch(e){ v.failed=true; return false; } },
    _drawViz(){ const v=this._viz; if(!v.an) return; if(v.raf) cancelAnimationFrame(v.raf);
      const dpr=Math.min(2,window.devicePixelRatio||1), data=new Uint8Array(v.an.frequencyBinCount);
      const loop=()=>{ const cv=this.el&&this.el.querySelector('.mp-viz');
        if(!cv || this.el.classList.contains('hidden') || this.min || !_audioEl || _audioEl.paused){ v.raf=0; return; }
        const W=cv.width=Math.floor(cv.clientWidth*dpr), H=cv.height=Math.floor(cv.clientHeight*dpr);
        if(!W||!H){ v.raf=requestAnimationFrame(loop); return; }
        const cx=cv.getContext('2d'); v.an.getByteFrequencyData(data); cx.clearRect(0,0,W,H);
        const n=Math.min(data.length,42), bw=W/n;
        for(let i=0;i<n;i++){ const h=Math.max(2*dpr,(data[i]/255)*H), x=i*bw;
          const g=cx.createLinearGradient(0,H,0,H-h); g.addColorStop(0,'#00f0ff'); g.addColorStop(.5,'#7df0ff'); g.addColorStop(1,'#ff2bd6');
          cx.fillStyle=g; cx.shadowColor='#00f0ff'; cx.shadowBlur=7*dpr; cx.fillRect(x+dpr,H-h,Math.max(1,bw-2*dpr),h); }
        v.raf=requestAnimationFrame(loop); };
      v.raf=requestAnimationFrame(loop); },
  };
  // AI Chat tab — uploads + generated images, stored encrypted under the storage key (separate from
  // the public Blossom list); shown via the decrypting /client/file route. Renders into `pane`.
  async function renderAiFiles(pane){
    let files=[], err='';
    try{ const auth=await sign(27235,'ai-files',[['p',ME.pubkey]]);
      const r=await fetch('/client/ai-files',{method:'POST',headers:{'Content-Type':'application/json'},
        body:JSON.stringify({pubkey:ME.pubkey,auth:btoa(JSON.stringify(auth))})}).then(r=>r.json());
      if(r && r.ok===false) err=r.error||'request failed';
      files=(r&&r.files)||[]; }catch(e){ err=e.message||'sign/fetch failed'; }
    // Always show the section so it's discoverable. Surface errors/empty rather than vanishing.
    if(!files.length){
      pane.innerHTML=`<div class="empty" style="margin:16px">${err?('Couldn\'t load: '+enc(err)):'No AI chat files yet — upload a file or generate an image in PosterChan AI.'}</div>`;
      return;
    }
    pane.innerHTML=`<div class="files-grid">${files.map(f=>{
        const isImg=/^image\//.test(f.mime)||f.kind==='generated';
        const thumb=isImg?`<img src="${enc(thumbUrl(f.url))}" loading="lazy">`:`<div class="file-icon">📎<span>${enc((f.mime.split('/')[1]||'file').slice(0,8))}</span></div>`;
        return `<div class="file-card" data-sha="${enc(f.sha)}"><a href="${enc(f.url)}" data-mime="${enc(f.mime||'')}" target="_blank">${thumb}</a><button class="copy" data-url="${enc(f.url)}" title="Copy URL">⧉</button><button class="del" data-sha="${enc(f.sha)}">✕</button><div class="meta"><span>${enc(f.name.slice(0,16))}</span></div></div>`;
      }).join('')}</div>`;
    $$('.del',pane).forEach(b=> b.onclick=()=>delAiFile(b.dataset.sha));
    $$('.copy',pane).forEach(b=> b.onclick=()=>copyUrl(b.dataset.url));
  }
  async function delAiFile(sha){
    if(!confirm('Delete this AI file?')) return;
    try{ const auth=await sign(27235,'ai-file-delete',[['p',ME.pubkey]]);
      const r=await fetch('/client/ai-file-delete',{method:'POST',headers:{'Content-Type':'application/json'},
        body:JSON.stringify({pubkey:ME.pubkey,sha,auth:btoa(JSON.stringify(auth))})}).then(r=>r.json());
      if(r&&r.ok){ toast('deleted'); renderBlossom(); } else toast('delete failed');
    }catch(_){ toast('delete failed'); }
  }
  async function delBlob(sha){
    if(!confirm('Delete this blob?'))return; const server=mediaServer();
    const auth=await sign(24242,'Delete blob',[['t','delete'],['x',sha],['expiration',String(Math.floor(Date.now()/1000)+3600)]]);
    const res=await fetch(server+'/'+sha,{ method:'DELETE', headers:{'Authorization':'Nostr '+btoa(JSON.stringify(auth))} });
    if(res.ok){ FilesIdx.forget(sha); delete _trackUrls[sha]; toast('deleted'); renderBlossom(); } else toast('delete failed');
  }

  // ---------- notifications ----------
  let _notifReady=false;
  // A follow is a kind-3 (the follower's WHOLE contact list), republished every time they
  // follow/unfollow ANYONE — so each republish looked like a brand-new "followed you". Record the
  // FIRST time we see each follower and key the notification off that stable time, so a known
  // follower re-saving their list never re-pings or re-lights the badge.
  let _followSeen={}; try{ _followSeen=JSON.parse(localStorage.getItem('pc_follow_seen')||'{}')||{}; }catch(_){ _followSeen={}; }
  function _followTs(pk, fallback){ if(!(pk in _followSeen)){ _followSeen[pk]=fallback||Math.floor(Date.now()/1000); try{ localStorage.setItem('pc_follow_seen', JSON.stringify(_followSeen)); }catch(_){} } return _followSeen[pk]; }
  function _notifTs(e){ return e.kind===3 ? (_followSeen[e.pubkey]!=null?_followSeen[e.pubkey]:e.created_at) : e.created_at; }
  async function watchNotifications(){
    seenNotif.last = +(localStorage.getItem('pc_notif_seen')||0);
    // Seed the known-follower set from the FULL current follower list BEFORE going live. kind-3 is the
    // follower's whole contact list, republished on every edit — so without a comprehensive seed, a
    // fresh client / cleared storage / a follower beyond the live sub's 150-cap re-pings as a "new
    // follow" every time anyone edits their list. We mark every existing follower as already-seen (their
    // _followSeen time = last-checked), so only a pubkey we've NEVER recorded — a genuinely new follower
    // arriving live — pings/badges. (the recurring "follow spam from people who followed long ago")
    try{
      const followers = await Relay.query([{ kinds:[3], '#p':[ME.pubkey], limit:1000 }]);
      let changed=false;
      for(const e of (followers||[])){ FOLLOWERS.add(e.pubkey);
        if(!(e.pubkey in _followSeen)){ _followSeen[e.pubkey]=seenNotif.last; changed=true; } }
      if(changed){ try{ localStorage.setItem('pc_follow_seen', JSON.stringify(_followSeen)); }catch(_){} }
    }catch(_){}
    Relay.subscribe([{ '#p':[ME.pubkey], kinds:[1,6,7,9735,3,1984,42,1111], limit:150 }], {   // 42=chat, 1111=community comments
      onEvent: ev => { if(ev.pubkey===ME.pubkey) return; if(Store.saveEvent(ev)){ invalidateCounts(); needProfile(ev.kind===9735?(zapSender(ev)||ev.pubkey):ev.pubkey);
        if(ev.kind===3){
          const firstTime = !(ev.pubkey in _followSeen);
          const ts = _followTs(ev.pubkey, ev.created_at);
          if(firstTime && ts>seenNotif.last){ bumpNotif(); if(_notifReady) notifPing(ev); }   // genuinely new follower only
          if(VIEW==='notifications') renderNotifications();
          return;                                  // a re-saved contact list never re-notifies
        }
        if(ev.created_at>seenNotif.last){ bumpNotif(); if(_notifReady) notifPing(ev); }
        if(VIEW==='notifications') renderNotifications(); } },
      onEose: ()=>{ _notifReady=true; if(VIEW==='notifications') renderNotifications(); else bumpNotif(); }   // show unseen count on load; ping LIVE ones
    });
  }
  function notifPing(ev){
    // NEVER toast/OS-notify for follows. kind-3 is a whole contact list, republished on every edit, so
    // an OLD follower constantly looks "new" — detecting genuine new follows reliably is impossible
    // (the relay can't be trusted to have every follower's current list). Follows still show in the
    // Notifications list (Follows tab); they just don't interrupt. Kills the recurring follow spam.
    if(ev.kind===3) return;
    const fromPk = ev.kind===9735?(zapSender(ev)||ev.pubkey):ev.pubkey;
    if(MUTED.has(fromPk)) return;   // no toast / OS notification for a muted author
    const p=profOf(fromPk); const who=p.name||p.display_name||'someone';
    const what = ev.kind===9735?`⚡ zapped you ${fmtSats(zapAmount(ev))} sats`
      : ev.kind===3?'🫂 followed you'
      : ev.kind===1984?'🚩 reported you'
      : ev.kind===7?`reacted ${ev.content==='+'||ev.content===''?'❤️':enc(ev.content)}`
      : ev.kind===6?'reposted you'
      : ev.kind===42?'💬 messaged you in chat'
      : ev.kind===1111?'👥 replied to you in a community'
      : isReply(ev)?'replied to you' : 'mentioned you';
    notifToast(`🔔 ${who} ${what}`, p.picture);
    try{ if(window.Notification && Notification.permission==='granted') new Notification('PosterChan', { body:`${who} ${what}`, icon:p.picture||LOGO }); }catch(_){}
  }
  function notifToast(msg, pic){
    const t=document.createElement('div'); t.className='toast notif-toast';
    t.innerHTML=`<img src="${enc(pic||LOGO)}" onerror="this.src='${LOGO}'"><span>${enc(msg)}</span>`;
    t.onclick=()=>{ switchView('notifications'); t.remove(); };
    $('#toast-root').appendChild(t); setTimeout(()=>t.remove(),5000);
  }
  function notifList(){
    const evs=Store.all().filter(e=>[1,6,7,9735,3,1984].includes(e.kind) && e.pubkey!==ME.pubkey && !MUTED.has(e.kind===9735?(zapSender(e)||e.pubkey):e.pubkey) && e.tags.some(t=>t[0]==='p'&&t[1]===ME.pubkey)).sort((a,b)=>_notifTs(b)-_notifTs(a));
    // dedupe follows by author — a follower re-saving their contact list shouldn't show "followed you" repeatedly
    const seen3=new Set(); const out=[];
    for(const e of evs){ if(e.kind===3){ if(seen3.has(e.pubkey)) continue; seen3.add(e.pubkey); } out.push(e); }
    return out.slice(0,2000);
  }
  function bumpNotif(){ const n=notifList().filter(e=>e.kind!==3 && _notifTs(e)>seenNotif.last).length; $$('#notif-badge,#notif-badge-m').forEach(b=>{ if(n){b.textContent=n>99?'99+':n;b.classList.remove('hidden');}else b.classList.add('hidden');}); }
  let _notifShown = 25;   // paginate: render a page at a time, "Load more" reveals the next
  let _notifFilter = 'all';
  const _NOTIF_TABS = [['all','All'],['mentions','@ Mentions'],['reactions','♥ Reactions'],['zaps','⚡ Zaps'],['follows','🫂 Follows'],['reports','🚩 Reports']];
  function _notifMatch(e){
    switch(_notifFilter){
      case 'mentions': return e.kind===1 || e.kind===42 || e.kind===1111;   // incl. chat + community replies
      case 'reactions': return e.kind===7||e.kind===6;
      case 'zaps': return e.kind===9735;
      case 'follows': return e.kind===3;
      case 'reports': return e.kind===1984;
      default: return true;
    }
  }
  function renderNotifications(){
    const feed=$('#feed');
    const all=notifGrouped(notifList().filter(_notifMatch));
    const list=all.slice(0, _notifShown);
    const tabs=`<div class="notif-tabs">${_NOTIF_TABS.map(([k,l])=>`<button class="ntab${k===_notifFilter?' on':''}" data-nf="${k}">${enc(l)}</button>`).join('')}</div>`;
    feed.innerHTML = tabs + (all.length
      ? list.map(notifHtml).join('') + (all.length>_notifShown
          ? `<button class="btn btn-ghost full" id="notif-more">Load ${Math.min(25, all.length-_notifShown)} more (${all.length-_notifShown})</button>` : '')
      : '<div class="empty">No notifications here.</div>');
    $$('.ntab',feed).forEach(b=> b.onclick=()=>{ _notifFilter=b.dataset.nf; _notifShown=25; renderNotifications(); });
    list.forEach(e=>{ if(e.type==='group') e.events.forEach(x=>needProfile(x.pubkey)); else needProfile(e.kind===9735?(zapSender(e)||e.pubkey):e.pubkey); });
    seenNotif.last = Math.floor(Date.now()/1000); localStorage.setItem('pc_notif_seen', seenNotif.last);
    $$('#notif-badge,#notif-badge-m').forEach(b=>b.classList.add('hidden'));
    // row opens the post; avatar opens the sender's profile (stop the row handler firing too)
    feed.querySelectorAll('.notif').forEach(n=> n.onclick=()=> n.dataset.prof ? renderProfileView(n.dataset.prof) : openThread(n.dataset.open));
    feed.querySelectorAll('.notif-av').forEach(a=> a.onclick=(ev)=>{ ev.stopPropagation(); renderProfileView(a.dataset.pk); });
    const more=$('#notif-more'); if(more) more.onclick=async ()=>{
      _notifShown+=25;
      // Reaching the end of what's loaded → fetch OLDER notifications from the relay (paginate back
      // in time with `until`), so notifications aren't capped at the initial window.
      if(_notifShown >= all.length-5 && all.length){
        more.textContent='Loading older…'; more.disabled=true;
        const oldest=all[all.length-1].created_at;
        try{
          const older=await Relay.query([{ '#p':[ME.pubkey], kinds:[1,6,7,9735,42,1111], until: oldest-1, limit:100 }]);
          older.forEach(e=>{ if(e.pubkey!==ME.pubkey) Store.saveEvent(e); });
        }catch(_){}
      }
      renderNotifications();
    };
  }
  // Collapse reactions/reposts on the SAME post into one row ("X and N others reacted"); everything
  // else stays an individual notification.
  function notifGrouped(list){
    const groups=new Map(); const out=[];
    for(const e of list){
      if(e.kind===7||e.kind===6){
        const tgt=(e.tags.filter(t=>t[0]==='e').pop()||[])[1]||e.id;
        const key=e.kind+':'+tgt;
        let g=groups.get(key);
        if(!g){ g={type:'group', kind:e.kind, tgt, events:[], created_at:e.created_at}; groups.set(key,g); out.push(g); }
        g.events.push(e); if(e.created_at>g.created_at) g.created_at=e.created_at;
      } else out.push(e);
    }
    return out.sort((a,b)=>b.created_at-a.created_at);
  }
  function notifHtml(e){
    if(e.type==='group'){
      const first=e.events[0], fp=first.pubkey, p=profOf(fp), av=p.picture||LOGO, others=e.events.length-1;
      const verb = e.kind===6?'reposted your note':`reacted ${reactDisp(first)} to your post`;
      const who = (p.name||p.display_name||'someone')+(others>0?` <span class="muted">and ${others} other${others>1?'s':''}</span>`:'');
      return `<div class="notif ${e.kind===6?'rt':'like'}" data-open="${enc(e.tgt)}"><span class="ic">${e.kind===6?'↻':'♥'}</span><img class="notif-av" data-pk="${fp}" src="${enc(av)}" onerror="this.src='${LOGO}'"><div><b>${who}</b> ${verb}<div class="muted small">${timeAgo(e.created_at)}</div></div></div>`;
    }
    const fromPk = e.kind===9735?(zapSender(e)||e.pubkey):e.pubkey;
    const p=profOf(fromPk); const av=p.picture||LOGO;
    // What to open on click: for a reply/mention (kind-1) or a chat reply (kind-42) open the
    // notification event ITSELF — for kind-1 the thread view centers their reply with your post above
    // it; for kind-42 the chat redirect scrolls to THEIR message (its last e-tag is the parent, i.e.
    // your message, which would be the wrong target). For a reaction/repost/zap, open the post they
    // acted on (the last referenced e-tag).
    const ref=(e.tags.filter(t=>t[0]==='e').pop()||[])[1]||'';
    const tgt = (e.kind===1 || e.kind===42) ? e.id : (ref||e.id);
    let cls,ic,txt;
    if(e.kind===9735){cls='zap';ic='⚡';txt=`zapped you <b>${fmtSats(zapAmount(e))} sats</b>`;}
    else if(e.kind===3){cls='follow';ic='🫂';txt='followed you';}
    else if(e.kind===1984){cls='report';ic='🚩';const tg=e.tags.find(t=>t[0]==='p'&&t[1]===ME.pubkey)||e.tags.find(t=>t[0]==='e');const ty=(tg&&tg[2])||(e.tags.find(t=>t[0]==='report')||[])[1]||'other';txt=`reported you <b>${enc(ty)}</b>${e.content?': '+enc((e.content||'').slice(0,80)):''}`;}
    else if(e.kind===7){cls='like';ic='♥';txt=`reacted ${reactDisp(e)} to your post`;}
    else if(e.kind===6){cls='rt';ic='↻';txt='reposted your note';}
    else if(e.kind===42){cls='reply';ic='💬';txt='chat: '+applyEmojis(enc((e.content||'').slice(0,80)), e);}
    else if(e.kind===1111){cls='reply';ic='👥';txt='community: '+applyEmojis(enc((e.content||'').slice(0,80)), e);}
    else if(isReply(e)){cls='reply';ic='💬';txt='replied: '+applyEmojis(enc((e.content||'').slice(0,80)), e);}
    else {cls='mention';ic='@';txt='mentioned you: '+applyEmojis(enc((e.content||'').slice(0,80)), e);}
    // follows/reports have no thread → the row opens the sender's profile (data-prof); others open the post.
    const isProf = e.kind===3||e.kind===1984;
    return `<div class="notif ${cls}" ${isProf?`data-prof="${fromPk}"`:`data-open="${tgt}"`}><span class="ic">${ic}</span><img class="notif-av" data-pk="${fromPk}" src="${enc(av)}" onerror="this.src='${LOGO}'"><div><b>${enc(p.name||p.display_name||'anon')}</b> ${txt}<div class="muted small">${timeAgo(e.created_at)}</div></div></div>`;
  }

  // ---------- DMs: NIP-17 gift-wrapped (modern, local-key) + NIP-04 (legacy, read-compat) ----------
  const dmPeers = new Map();  // peer -> [{ev, text}]
  let dmActive = null;
  const _dmFull = new Set();   // peers whose full DM history has been backfilled on open (once each)
  const _dmShown = new Map();  // pk -> how many recent messages are rendered (paginated)
  const _DM_INIT = 1, _DM_STEP = 20;   // show last 1 on open (instant); "load older" reveals 20 more at a time
  let _dmScrollTop = false;    // next thread render keeps the top (after "load older") instead of bottom
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
    // Persistent unread badge (like Notifications): count incoming DMs newer than the last time the
    // Messages view was opened, so DMs received while away still alert — not just live ones.
    recountDmUnread();
    // live sub for legacy DMs (since now is fine — kind-4 timestamps are real)
    const since=Math.floor(Date.now()/1000)-60;
    Relay.subscribe([{ kinds:[4], '#p':[ME.pubkey], since }, { kinds:[4], authors:[ME.pubkey], since }], {
      onEvent: ev => { Store.saveEvent(ev); if(ingestDM(ev) && ev.pubkey!==ME.pubkey && !MUTED.has(ev.pubkey)){ _dmUnread++; bumpDm(); _dmNotify(ev.pubkey); }
        _scheduleDmRefresh(); }   // debounced: never rebuilds per-message (would thrash + drop the mobile overlay)
    });
    // NIP-17 gift wraps carry RANDOMIZED past timestamps, so a `since` filter would drop them —
    // subscribe with no `since` and let Store dedup skip the ones we've already unwrapped.
    if(modern){
      // Gift wraps carry RANDOMIZED past timestamps, so we can't `since`-filter the live sub — the
      // relay replays the WHOLE history on connect. Treat everything up to the initial EOSE as
      // backlog (ingest silently, NO notification); only wraps arriving AFTER EOSE are genuinely new.
      // Without this every historical DM fired a notification on login — the "flooded on login" bug.
      let _dmLive = false;
      Relay.subscribe([{ kinds:[1059], '#p':[ME.pubkey] }], {
        onEvent: async ev => { if(!Store.saveEvent(ev)) return; await ingestWrap(ev, _dmLive); },
        onEose: () => { _dmLive = true; }
      });
    }
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
    if(live && !mine && !MUTED.has(peer)){ _dmUnread++; bumpDm(); _dmNotify(peer); }
    _scheduleDmRefresh();
    return true;
  }
  // ---------- NIP-17 DM relay list (kind 10050) — discovery + outbox delivery ----------
  // The relays where WE receive gift-wrapped DMs: our own list when enabled, else the built-in relay.
  // Other clients (0xchat/Amethyst/Coracle) read our kind-10050 to know where to deliver DMs to us.
  function myInboxRelays(){
    const list = ClientSettings.get('relaysEnabled') ? userRelays() : [];
    return [...new Set((list.length ? list : [CFG.relay_url]).map(u=>normalizeRelay(u)).filter(Boolean))];
  }
  // Publish our kind-10050 DM-inbox list. Idempotent + change-only (compares to the current event),
  // runs once per session from Relay.onReady — no polling, no spin. The relay broadcasts kind-10050
  // upstream, so other clients can discover where to gift-wrap-DM us.
  async function ensureDmInboxList(){
    try{
      const want = myInboxRelays(); if(!want.length) return;
      const evs = await Relay.query([{ authors:[ME.pubkey], kinds:[10050], limit:1 }]);
      const cur = evs.length ? evs.sort((a,b)=>b.created_at-a.created_at)[0] : null;
      const have = cur ? [...new Set(cur.tags.filter(t=>t[0]==='relay'&&t[1]).map(t=>normalizeRelay(t[1])).filter(Boolean))] : [];
      if(cur && have.length===want.length && want.every(u=>have.includes(u))) return;   // unchanged → don't republish
      await publish(10050, '', want.map(u=>['relay', u]));
    }catch(_){}
  }
  // Discovery/indexer relays queried to find an EXTERNAL (non-WoT) peer's DM-inbox list — these
  // specialise in profiles/relay-lists (kind 0/10002/10050), so they're low-volume to hit, plus
  // 0xchat's own relay where its users publish theirs.
  const DISCOVERY_RELAYS = ['wss://purplepag.es/', 'wss://user.kindpag.es/', 'wss://relay.nostr.band/', 'wss://relay.0xchat.com/'];
  // A peer's DM-inbox relays (their kind-10050), lazily fetched + cached (1h TTL); falls back to their
  // NIP-65 read relays (kind 10002). Tries our pool first (has it for WoT members), then external
  // discovery relays for strangers (our WoT-only relay never stored those). Looked up ONLY when
  // sending to a not-yet-cached peer — never per message or per render — so it adds no steady-state CPU.
  const _inboxCache = new Map();   // pubkey -> { relays:[...], ts }
  const _INBOX_TTL = 3600*1000;
  function _pick10050(evs, pk){ const ev=evs.filter(e=>e&&e.kind===10050&&e.pubkey===pk).sort((a,b)=>b.created_at-a.created_at)[0];
    return ev?ev.tags.filter(t=>t[0]==='relay'&&t[1]).map(t=>normalizeRelay(t[1])).filter(Boolean):[]; }
  function _pick10002(evs, pk){ const ev=evs.filter(e=>e&&e.kind===10002&&e.pubkey===pk).sort((a,b)=>b.created_at-a.created_at)[0];
    return ev?ev.tags.filter(t=>t[0]==='r'&&t[1]&&(t.length<3||t[2]==='read')).map(t=>normalizeRelay(t[1])).filter(Boolean):[]; }
  async function dmInboxRelays(pk){
    const c=_inboxCache.get(pk); const now=Date.now();
    if(c && (now-c.ts)<_INBOX_TTL) return c.relays;
    let relays=[];
    try{
      const evs=await Relay.query([{ authors:[pk], kinds:[10050,10002], limit:2 }]);
      relays=_pick10050(evs, pk); if(!relays.length) relays=_pick10002(evs, pk);
      if(!relays.length){
        // Stranger (not in our WoT) → ask external discovery relays. They're untrusted, so VERIFY
        // signatures before trusting a relay list — a forged one would misroute the (encrypted) wrap.
        let ext=await Relay.queryFrom(DISCOVERY_RELAYS, [{ authors:[pk], kinds:[10050,10002], limit:2 }]);
        if(ext.length){ try{ const v=await Relay.worker.call('verifyBatch',{events:ext});
          const ok=new Set(v.filter(r=>r.valid).map(r=>r.id)); ext=ext.filter(e=>ok.has(e.id)); }catch(_){ ext=[]; } }
        relays=_pick10050(ext, pk); if(!relays.length) relays=_pick10002(ext, pk);
      }
    }catch(_){}
    relays=[...new Set(relays)];
    _inboxCache.set(pk, { relays, ts:now });
    return relays;
  }
  // Send a DM: NIP-17 gift wraps for local-key users, legacy NIP-04 for NIP-07 (no exposed secret).
  async function sendDm(pk, text){
    if(signer && signer.nip17wrap){
      const { toPeer, toSelf } = await signer.nip17wrap(pk, text);
      Store.saveEvent(toSelf); await ingestWrap(toSelf, false);   // show our own message right away
      const r1=await Relay.publish(toPeer); await Relay.publish(toSelf);
      if(VIEW==='messages') renderMessages();   // our message already shows (ingestWrap above) — don't block on delivery
      // NIP-17 outbox delivery (backgrounded): push the wrap to the RECIPIENT's own DM-inbox relays
      // (kind 10050) so clients that don't read our relay (0xchat/Amethyst) receive it. publishTo skips
      // relays already in our pool + is bounded, so it's a no-op when the peer reads our relay. We only
      // warn when NOTHING accepted it — our relay rejects wraps to a non-WoT recipient (expected for an
      // external user), which is fine once their own inbox relay has taken it.
      dmInboxRelays(pk).then(inbox=>{
        if(!inbox.length){ if(r1 && r1.ok===false) toast('message not delivered — recipient has no DM inbox relays'); return; }
        Relay.publishTo(inbox, toPeer).then(n=>{ if(r1 && r1.ok===false && !n) toast('message not delivered — no inbox relay accepted it'); });
      }).catch(()=>{});
    } else {
      const ct=await signer.nip04enc(pk, text); await publish(4, ct, [['p',pk]]);
    }
  }
  function bumpDm(){ $$('#dm-badge,#dm-badge-m').forEach(b=>{ if(_dmUnread){ b.textContent=_dmUnread>99?'99+':_dmUnread; b.classList.remove('hidden'); } else b.classList.add('hidden'); }); }
  function recountDmUnread(){ const seen=ClientSettings.get('dmSeen',0); let n=0;
    for(const [pk,arr] of dmPeers){ if(MUTED.has(pk)) continue; for(const m of arr){ if(!m.mine && (m.t||0)>seen) n++; } } _dmUnread=n; bumpDm(); }
  function _dmNotify(fromPk){
    const p=fromPk?profOf(fromPk):{}; const who=p.name||p.display_name||'someone';
    notifToast(`✉ ${who} sent you a message`, p.picture);   // in-app toast (no OS permission needed)
    try{ if(window.Notification && Notification.permission==='granted') new Notification('✉ New message', {body:`${who} sent you a DM`, tag:'pc-dm', icon:p.picture||LOGO}); }catch(_){}
  }
  // Index DMs WITHOUT decrypting (decryption is CPU-heavy ECDH+AES in the worker; decrypting all
  // 200 on load jams the worker and stalls timeline verification). Decrypt lazily on view.
  function ingestDM(ev){
    const mine = ev.pubkey===ME.pubkey;
    // Only DMs that involve ME. The relay stores other WoT members' kind-4 DMs and the client
    // caches them (Store.byKind(4)) — without this guard they'd show as "couldn't decrypt".
    const toMe = (ev.tags||[]).some(t=>t[0]==='p' && t[1]===ME.pubkey);
    if(!mine && !toMe) return false;
    const peer = mine ? (ev.tags.find(t=>t[0]==='p')||[])[1] : ev.pubkey;
    if(!peer) return false; needProfile(peer);
    if(!dmPeers.has(peer)) dmPeers.set(peer, []);
    const arr=dmPeers.get(peer); if(arr.find(m=>m.id===ev.id)) return false;
    arr.push({ id:ev.id, mine, ev, text:null, t:ev.created_at }); arr.sort((a,b)=>a.t-b.t);
    _scheduleDmRefresh();
    return true;
  }
  async function decryptMsg(peer, m){
    if(m.text!=null) return m.text;
    try{ m.text=await signer.nip04dec(peer, m.ev.content); }catch(_){ m.text='🔒 (couldn\'t decrypt)'; }
    return m.text;
  }
  function renderMessages(){
    _dmUnread=0; ClientSettings.set('dmSeen', Math.floor(Date.now()/1000)); bumpDm();   // mark DMs read (persistent)
    if(!_dmLoaded){ ensureDMs(); }   // lazy-load on first open
    const feed=$('#feed');
    // Preserve the list scroll across the rebuild. A background refresh (the NIP-17 history replay)
    // rebuilds the whole list, which would otherwise reset scroll to the TOP — yanking you up as you
    // try to scroll (and a jump-to-top is what makes the mobile browser re-reveal its toolbar).
    const _prevList=$('#dm-list'); const _listScroll=_prevList?_prevList.scrollTop:0;
    feed.innerHTML=`<div class="dm-wrap"><div class="dm-list" id="dm-list"></div><div class="dm-thread" id="dm-thread"><div class="empty">${_dmLoaded?'Select a conversation, or start one.':'Loading…'}</div></div></div>`;
    const list=$('#dm-list');
    // Optional privacy: don't reveal message previews in the list until you open the conversation.
    const hidePrev = ClientSettings.get('hideDmPreview', false);
    const peers=[...dmPeers.keys()].filter(pk=>!MUTED.has(pk)).sort((a,b)=>{ const la=dmPeers.get(a).slice(-1)[0]||{}, lb=dmPeers.get(b).slice(-1)[0]||{}; return (lb.t||0)-(la.t||0); });
    list.innerHTML = `<div class="dm-peer" id="dm-new"><span class="ic">+</span><b>New message</b></div>` + peers.map(pk=>{
      const p=profOf(pk); needProfile(pk); const last=dmPeers.get(pk).slice(-1)[0]||{};
      const prev = hidePrev ? '••• tap to view' : (last.text!=null ? enc(last.text.slice(0,28)) : '🔒 …');
      const nm = p.name||p.display_name||niceNip05(p.nip05)||(NT().nip19.npubEncode(pk).slice(0,12)+'…');
      return `<div class="dm-peer" data-peer="${pk}"><img class="dmav" data-prof="${pk}" src="${enc(p.picture||LOGO)}" onerror="this.src='${LOGO}'"><div><b>${enc(nm)}</b><div class="muted small">${prev}</div></div></div>`;
    }).join('');
    $('#dm-new').onclick=newDmModal;
    if(_listScroll && list) list.scrollTop=_listScroll;   // restore scroll so a background refresh doesn't jump to top
    $$('[data-peer]',list).forEach(el=> el.onclick=()=>openDm(el.dataset.peer));
    // Tapping the AVATAR opens the sender's profile instead of the conversation.
    $$('.dmav',list).forEach(av=> av.onclick=e=>{ e.stopPropagation(); renderProfileView(av.dataset.prof); });
    // lazily decrypt ONLY the last message of each peer for the preview (not every message). When
    // previews are hidden, skip decryption entirely (privacy + a CPU saving on every list render).
    const need=hidePrev ? [] : peers.filter(pk=>{ const l=dmPeers.get(pk).slice(-1)[0]; return l && l.text==null; });
    if(need.length) Promise.all(need.map(pk=>decryptMsg(pk, dmPeers.get(pk).slice(-1)[0]))).then(()=>{ if(VIEW==='messages' && !dmActive) renderMessages(); });
    // Preserve an OPEN conversation across re-renders: re-apply `has-active` to the rebuilt list (it's
    // what shows the thread as a full-screen overlay on mobile) — without this, any renderMessages()
    // (incoming DM, refresh) drops the class and bounces the user back to the conversation list.
    if(dmActive && dmPeers.has(dmActive)){ list.classList.add('has-active'); renderDmThread(dmActive); }
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
  function openDm(pk){ _dmShown.delete(pk); dmActive=pk; $$('.dm-peer').forEach(e=>e.classList.toggle('active',e.dataset.peer===pk)); $('#dm-list').classList.add('has-active'); renderDmThread(pk); }
  // Coalesce a STORM of incoming-message renders into ONE every 350ms. On load, the NIP-17 sub replays
  // your WHOLE DM history and unwraps each message — rendering per message was the "window keeps moving"
  // thrash (and re-scrolled to bottom each time). One debounced render absorbs the whole burst.
  let _dmRefreshTimer=null, _dmThreadSig='';
  function _threadSig(pk){ const arr=dmPeers.get(pk)||[]; return pk+'|'+(arr.length?(arr[arr.length-1].id||''):'')+'|'+(_dmShown.get(pk)||_DM_INIT); }
  function _scheduleDmRefresh(){
    if(_dmRefreshTimer || VIEW!=='messages') return;
    _dmRefreshTimer=setTimeout(()=>{ _dmRefreshTimer=null; if(VIEW!=='messages') return;
      if(dmActive){
        // Skip the rebuild if the VISIBLE window is unchanged — the NIP-17 replay streams OLDER history
        // in, which doesn't touch the newest message we show, so re-rendering would just flicker.
        const sig=_threadSig(dmActive); if(sig===_dmThreadSig) return; _dmThreadSig=sig;
        renderDmThread(dmActive);
      } else renderMessages(); }, 350);
  }
  async function renderDmThread(pk){
    const wrap=$('#dm-thread'); if(!wrap)return;
    // Backfill this conversation's FULL history once (the initial bulk fetch caps at limit:300 across
    // ALL peers, so a busy inbox leaves old threads showing only their newest message). NIP-17 is
    // already fully loaded by its no-limit live sub; this targeted query backfills LEGACY kind-4 for
    // this peer. Runs in the background so the thread still renders instantly from what's cached.
    if(!_dmFull.has(pk)){ _dmFull.add(pk);
      Relay.query([{kinds:[4], authors:[pk], '#p':[ME.pubkey]}, {kinds:[4], authors:[ME.pubkey], '#p':[pk]}])
        .then(evs=>{ let added=false; for(const e of (evs||[])){ Store.saveEvent(e); if(ingestDM(e)) added=true; }
          if(added) _scheduleDmRefresh(); })   // re-render only if new msgs arrived (no loop: _dmFull set)
        .catch(()=>{});
    }
    const p=profOf(pk); needProfile(pk); const all=dmPeers.get(pk)||[];
    // PAGINATION: render only the last N messages (3 on open) so a long thread opens instantly on mobile;
    // "Load older" reveals 20 more each tap. Decrypt ONLY the visible slice — decryption (ECDH+AES per
    // message) is the slow/glitchy part, so decrypting a whole history on open is what lagged.
    const shown=Math.min(all.length, _dmShown.get(pk)||_DM_INIT);
    const start=all.length-shown;
    const msgs=all.slice(start);
    for(const m of msgs){ if(m.text==null) await decryptMsg(pk,m); }
    if(dmActive!==pk) return;   // user switched away while decrypting
    // Was the user pinned to the bottom before this re-render? If they'd scrolled up to read, DON'T
    // yank them back down when a background message lands (part of "the window keeps moving").
    const _prev=$('#dm-msgs'); const _atBottom = !_prev || (_prev.scrollHeight - _prev.scrollTop - _prev.clientHeight < 80);
    const older = start>0 ? `<button class="dm-older" id="dm-older">⬆ Load older (${start})</button>` : '';
    wrap.innerHTML=`<div class="topbar"><button class="mini" id="dm-back">←</button> <b class="dm-peer-name" data-prof="${pk}" style="cursor:pointer">${enc(p.name||p.display_name||niceNip05(p.nip05)||(NT().nip19.npubEncode(pk).slice(0,14)+'…'))}</b><span class="spacer"></span><button class="mini" id="dm-mute" title="Mute this sender">${MUTED.has(pk)?'🔊 Unmute':'🔇 Mute'}</button></div>
      <div class="dm-msgs" id="dm-msgs">${older}${msgs.map(m=>`<div class="bubble ${m.mine?'me':'them'}">${linkify(m.text||'')}</div>`).join('')}</div>
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
    { const nm=wrap.querySelector('.dm-peer-name'); if(nm) nm.onclick=()=>renderProfileView(pk); }
    // Mute the DM sender straight from the conversation. Muting drops back to the list (the thread
    // is filtered out); toggleMute re-renders Messages so it disappears immediately.
    { const mb=$('#dm-mute'); if(mb) mb.onclick=async()=>{ if(!MUTED.has(pk)){ dmActive=null; const dl=$('#dm-list'); if(dl) dl.classList.remove('has-active'); } await toggleMute(pk); }; }
    const inp=$('#dm-in');
    $('#dm-attach').onclick=()=>$('#dm-file').click();
    $('#dm-file').onchange=async e=>{ const files=[...e.target.files]; for(let i=0;i<files.length;i++){ try{ const url=await uploadBlob(files[i]); inp.value+=(inp.value?' ':'')+url; }catch(err){ toast('upload failed: '+err.message); } } e.target.value=''; inp.focus(); };
    $('#dm-files').onclick=()=>blossomPicker(inp);
    { const g=$('#dm-gif'); if(g) g.onclick=()=>gifPicker(inp); }
    const send=async()=>{ const t=inp.value.trim(); if(!t)return; inp.value='';
      try{ await sendDm(pk, t); }catch(e){ toast('dm failed: '+e.message);} };
    $('#dm-send').onclick=send; $('#dm-in').onkeydown=e=>{ if(e.key==='Enter' && !e.shiftKey){ e.preventDefault(); send(); } };
    { const ob=$('#dm-older'); if(ob) ob.onclick=()=>{ _dmShown.set(pk, Math.min((_dmShown.get(pk)||_DM_INIT)+_DM_STEP, all.length)); _dmScrollTop=true; renderDmThread(pk); }; }
    const m=$('#dm-msgs'); if(m){ if(_dmScrollTop){ _dmScrollTop=false; m.scrollTop=0; } else if(_atBottom) m.scrollTop=m.scrollHeight;
      // Click a DM image to open it full-size (the feed lightbox handler is bound to #feed only, so DM
      // images otherwise had no way to enlarge — the reported "images too small, can't click" issue).
      m.addEventListener('click', ce=>{ const im=ce.target.closest('img'); if(im){ ce.preventDefault(); openLightbox(im.currentSrc||im.src); } }); }
    _dmThreadSig=_threadSig(pk);   // mark what we just rendered so a debounced refresh won't re-render it
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
  // Patch the already-painted profile header in place when a background kind-0 refresh changed it
  // (live rename / new avatar), so we never have to block the first paint on that refetch.
  function _patchProfileHeader(pk){
    const feed=$('#feed'); if(!feed) return; const p=Store.profile(pk)||{};
    const av=feed.querySelector('.pav'); if(av){ const s=p.picture||LOGO; if(av.getAttribute('src')!==s) av.src=s; }
    const bn=feed.querySelector('.prof .banner'); if(bn){ const want=p.banner?`<img src="${enc(p.banner)}" onerror="this.remove()">`:''; if(bn.innerHTML!==want) bn.innerHTML=want; }
    const h2=feed.querySelector('.prof .pbody h2'); if(h2 && h2.firstChild) h2.firstChild.textContent=(p.name||p.display_name||'anon');
    const ab=feed.querySelector('.prof .about'); if(ab) ab.innerHTML=linkify(p.about||'');
  }
  async function renderProfileView(pk){
    cleanupInlineStream();   // e.g. tapping the host's name from a stream
    _hidePill();
    try{ _navUrl('/'+NT().nip19.npubEncode(pk)); }catch(_){}   // shareable URL: poster.place/<npub>
    if(VIEW!=='profile'){ VIEW='profile'; $$('.nav-item[data-view]').forEach(b=>b.classList.toggle('active',b.dataset.view==='profile')); $('#view-title').textContent='Profile'; }
    const feed=$('#feed'); feed.innerHTML='<div class="spinner"></div>';
    { const e=await Relay.query([{authors:[pk],kinds:[0],limit:1}]); for(const x of e)Store.saveProfile(x); }   // always refetch newest kind-0 so a renamed / re-avatar'd profile updates live (not just first view)
    const p=Store.profile(pk)||{}; const mine=pk===ME.pubkey;
    // Only the author's recent notes block the first paint. following/followers/pinned are loaded
    // in the BACKGROUND below — the followers query alone can pull up to 1000 kind-3 events, which
    // was the multi-second stall on every profile open.
    const notes=await Relay.query([{authors:[pk],kinds:[1,1068,6],limit:80}]); notes.forEach(n=>Store.saveEvent(n));   // include polls + reposts
    if(VIEW!=='profile') return;   // navigated away during the notes fetch
    const npub=NT().nip19.npubEncode(pk);
    feed.innerHTML=`<div class="prof"><div class="banner">${p.banner?`<img src="${enc(p.banner)}" onerror="this.remove()">`:''}</div>
      <div class="phead"><img class="pav" src="${enc(p.picture||LOGO)}" onerror="this.src='${LOGO}'">
        <div style="flex:1"></div>${mine?`<button class="btn btn-cyan small" id="edit-prof">Edit</button> <button class="btn btn-ghost small" id="open-settings">⚙ Settings</button> <button class="btn btn-ghost small prof-menu-btn" id="prof-menu" title="more">☰</button>`:`
          <button class="btn btn-ghost small" id="zap-prof">⚡ Zap</button>
          ${isXmrAddr(xmrOf(p))?`<button class="btn btn-ghost small" id="xmrtip-prof" title="tip Monero (XMR)">ɱ Tip</button>`:''}
          <button class="btn btn-ghost small prof-menu-btn" id="prof-menu" title="more">☰</button>`}</div>
      <div class="pbody"><h2>${enc(p.name||p.display_name||'anon')}<span class="vchk" id="prof-vchk"></span></h2>
        ${niceNip05(p.nip05)?`<div class="muted small">${enc(niceNip05(p.nip05))}</div>`:''}
        <div class="npubrow"><code>${enc(npub.slice(0,24))}…</code><button class="mini icon-btn" id="copy-npub" title="Copy npub"><svg viewBox="0 0 16 16" width="15" height="15" fill="currentColor"><path d="M0 0h6v6H0zM2 2v2h2V2zM10 0h6v6h-6zM12 2v2h2V2zM0 10h6v6H0zM2 12v2h2v-2zM9 9h2v2H9zM13 9h3v2h-3zM9 13h2v3H9zM12 12h4v4h-2v-2h-2z"/></svg></button></div>
        ${p.lud16?`<button class="ln-addr" id="prof-ln" title="send a zap">⚡ ${enc(p.lud16)}</button>`:''}
        ${isXmrAddr(xmrOf(p))?`<button class="ln-addr xmr" id="prof-xmr" title="tip Monero (XMR)">ɱ ${enc(xmrOf(p).slice(0,10))}…${enc(xmrOf(p).slice(-6))}</button>`:''}
        <div class="about">${linkify(p.about||'')}</div>
        <div class="follow-stats"><button class="statbtn" id="show-following"><b>·</b> Following</button><button class="statbtn" id="show-followers"><b>·</b> Followers</button></div>
      </div></div>
      <div class="prof-tabs"><button class="prof-tab active" data-tab="notes">Notes</button><button class="prof-tab" data-tab="replies">Replies</button><button class="prof-tab" data-tab="media">Media</button><button class="prof-tab" data-tab="articles">Articles</button></div>
      <div id="prof-list"></div>`;
    let pinnedHtml = '';   // filled by the deferred pinned query below; listFor() reads it live
    const listFor=(tab)=>{
      const lim=_prof.limit;
      if(tab==='replies'){ const r=Store.feed(e=>e.pubkey===pk && isReply(e)).slice(0,lim);
        return r.length ? r.map(e=>`<div class="reply-pair">${replyContextHtml(e)}${noteHtml(e)}</div>`).join('') : '<div class="empty">No replies yet.</div>'; }
      if(tab==='media'){ const m=Store.feed(e=>e.pubkey===pk && hasMedia(e)).slice(0,lim);
        if(!m.length) return '<div class="empty">No media yet.</div>';
        // gallery only — pull each post's media tags out of its mediaParts() gallery and grid them
        const items=m.map(e=>mediaParts(e.content).gallery.replace(/^<div class="media-row">/,'').replace(/<\/div>$/,'')).join('');
        return `<div class="media-grid">${items}</div>`; }
      if(tab==='articles'){ const a=_dedupAddr(Store.feed(e=>e.pubkey===pk && e.kind===30023)).slice(0,lim);
        return a.length ? a.map(articleCard).join('') : `<div class="empty">${_prof.artLoaded?'No articles yet.':'Loading…'}</div>`; }
      const n=Store.feed(e=>e.pubkey===pk && !isReply(e)).slice(0,lim);
      return pinnedHtml + (n.length ? n.map(e=>noteHtml(e)).join('') : '<div class="empty">No posts yet.</div>');
    };
    const fillList=(tab)=>{ const el=$('#prof-list'); if(el) el.innerHTML=listFor(tab); };
    // pagination cursor: oldest author kind-1 we hold (drives loadOlderProfile via `until`)
    const authorNotes=Store.feed(e=>e.pubkey===pk);
    _prof = { pk, tab:'notes', loading:false, done:false, limit:40, fill:fillList, following:[], followers:[],
              oldest: authorNotes.length ? authorNotes[authorNotes.length-1].created_at : 0 };
    fillList('notes');
    hydrate(feed);
    decorateVerified($('#prof-vchk'), pk, p.nip05);
    $$('.prof-tab',feed).forEach(t=> t.onclick=async()=>{ $$('.prof-tab',feed).forEach(x=>x.classList.toggle('active',x===t)); const tab=t.dataset.tab; _prof.tab=tab; fillList(tab); hydrate(feed);
      // Articles (kind-30023) aren't part of the initial note load — lazy-fetch them once on first open.
      if(tab==='articles' && !_prof.artLoaded){ _prof.artLoaded=true;
        try{ const a=await Relay.query([{authors:[pk],kinds:[30023],limit:40}]); for(const e of (a||[])) Store.saveEvent(e); }catch(_){}
        if(VIEW==='profile' && _prof.pk===pk && _prof.tab==='articles'){ fillList('articles'); hydrate(feed); } }
    });
    $('#copy-npub').onclick=()=>{ navigator.clipboard.writeText(npub); toast('npub copied'); };
    { const ln=$('#prof-ln'); if(ln) ln.onclick=()=>doZap(null, pk); }
    { const xb=$('#prof-xmr'); if(xb) xb.onclick=()=>doXmrTip(null, pk); }
    { const xt=$('#xmrtip-prof'); if(xt) xt.onclick=()=>doXmrTip(null, pk); }
    $('#show-following').onclick=()=>peopleModal('Following', _prof.following||[]);
    $('#show-followers').onclick=async()=>{   // lazy-load the follower LIST only when actually opened (count was already fetched via NIP-45)
      if(!_prof.followers || !_prof.followers.length){
        const fe=await Relay.query([{kinds:[3],'#p':[pk],limit:1000}]).catch(()=>[]);
        _prof.followers=[...new Set(fe.map(e=>e.pubkey))];
      }
      peopleModal('Followers', _prof.followers||[]);
    };
    if(mine){ $('#edit-prof').onclick=()=>editProfile(p); $('#open-settings').onclick=()=>switchView('settings'); }
    else { const z=$('#zap-prof'); if(z)z.onclick=()=>doZap(null,pk); }
    { const mn=$('#prof-menu'); if(mn)mn.onclick=()=>openProfileMenu(pk, mn); }   // ☰ on own + others' profiles
    // Background: following / followers / pinned — fetched in PARALLEL after the first paint and
    // patched in, so the profile opens instantly instead of waiting on (esp.) the 1000-event
    // followers query. Re-checks _prof.pk so a fast navigation away doesn't patch the wrong profile.
    (async()=>{
      const [k3, followerCount, pinList] = await Promise.all([
        Relay.query([{authors:[pk],kinds:[3],limit:1}]).catch(()=>[]),
        Relay.count([{kinds:[3],'#p':[pk]}]).catch(()=>0),   // NIP-45 COUNT — don't pull 1000 contact-list blobs just to tally (the profile-open spike). The list is lazy-loaded on "Followers" click.
        Relay.query([{authors:[pk],kinds:[10001],limit:1}]).catch(()=>[]),
      ]);
      if(VIEW!=='profile' || _prof.pk!==pk) return;
      _prof.following = k3.length ? (k3.sort((a,b)=>b.created_at-a.created_at)[0].tags.filter(t=>t[0]==='p'&&t[1]).map(t=>t[1])) : [];
      const ff=$('#show-following b'); if(ff) ff.textContent=_prof.following.length;
      const fr=$('#show-followers b'); if(fr) fr.textContent=Number(followerCount)||0;
      const pinIds=pinList.length ? pinList.sort((a,b)=>b.created_at-a.created_at)[0].tags.filter(t=>t[0]==='e'&&t[1]).map(t=>t[1]) : [];
      if(pinIds.length){
        const got=await Relay.query([{ids:pinIds}]).catch(()=>[]); got.forEach(e=>Store.saveEvent(e));
        const pinned=pinIds.map(id=>Store.get(id)).filter(Boolean);
        if(pinned.length && VIEW==='profile' && _prof.pk===pk){
          pinnedHtml='<div class="search-section-title">📌 Pinned</div>'+pinned.map(e=>noteHtml(e)).join('');
          if(_prof.tab==='notes'){ fillList('notes'); hydrate(feed); }
        }
      }
    })();
  }
  // the profile "☰ more" menu — Follow / Message / Mute / Block, kept off the header for a clean look
  async function openProfileMenu(pk, anchorBtn){
    const mine = pk===ME.pubkey;
    const items = mine ? [['reports','🚩 Reports received']] : [
      ['follow', FOLLOWS.has(pk)?'➖ Unfollow':'➕ Follow'],
      ['message','✉️ Message'],
      ['mute', MUTED.has(pk)?'🔊 Unmute':'🔇 Mute'],
      ['reports','🚩 Reports received'],
    ];
    items.push(['relays','🖧 Relays']);   // view the relays this user publishes to (NIP-65)
    if(IS_ADMIN){
      // admin extras: one consolidated permissions panel (AI, Blossom, image/music/video/torrent)
      // + relay block. State is fetched inside openPermissions so the menu opens instantly.
      items.push(['caps','🔑 Permissions']);
      items.push(['relay-sync','🔄 Sync notes']);
      items.push(['purge-blossom','🗑️ Purge Blossom','danger']);
      items.push(['block','🚫 Block','danger']);
    }
    openMenuPopover(anchorBtn, items, async a=>{
      if(a==='follow'){ await toggleFollow(pk); renderProfileView(pk); return; }
      if(a==='message'){ if(!dmPeers.has(pk))dmPeers.set(pk,[]); dmActive=pk; switchView('messages'); return; }
      if(a==='mute'){ await toggleMute(pk); renderProfileView(pk); return; }
      if(a==='reports') return showReports(pk);
      if(a==='relays') return showRelays(pk);
      if(a==='caps') return openPermissions(pk);
      if(a==='relay-sync') return doRelaySync(pk);
      if(a==='purge-blossom') return doPurgeBlossom(pk);
      if(a==='block') return doBlock(pk);
    });
  }
  // NIP-56 reports a user has RECEIVED (kind-1984 p-tagging them). Fetched from UPSTREAM relays via
  // /client/reports (the built-in relay only stores WoT-authored events, so reports about an arbitrary
  // user aren't local). Open to any user. Tap a report to see it in full (reason + reported post).
  async function showReports(pk){
    const who = (()=>{ const p=profOf(pk); return p.name||p.display_name||(NT().nip19.npubEncode(pk).slice(0,12)+'…'); })();
    modal(`<h3>🚩 Reports received — ${enc(who)}</h3><div id="rep-list" class="people-list"><div class="spinner"></div></div>`, async root=>{
      let reports=[];
      try{ const r=await fetch('/client/reports?pubkey='+encodeURIComponent(pk)).then(r=>r.json()); if(r&&r.ok) reports=r.reports||[]; }catch(_){}
      const h3=root.querySelector('h3'); if(h3) h3.textContent='🚩 Reports received — '+who+' ('+reports.length+')';
      const miss=[...new Set(reports.map(x=>x.reporter))].filter(a=>a&&!Store.haveProfile(a)).slice(0,200);
      if(miss.length){ try{ (await Relay.query([{authors:miss,kinds:[0],limit:miss.length}])).forEach(e=>Store.saveProfile(e)); }catch(_){} }
      const list=$('#rep-list',root); if(!list) return;
      list.innerHTML = reports.length ? reports.map((x,i)=>{
        const rp=Store.profile(x.reporter)||{};
        const rn=rp.name||rp.display_name||(NT().nip19.npubEncode(x.reporter).slice(0,12)+'…');
        const reason=(x.reason||'').trim();
        return `<div class="psearch rep-row" data-i="${i}"><img src="${enc(rp.picture||LOGO)}" onerror="this.src='${LOGO}'"><div class="pinfo"><b>${enc(rn)}</b><div class="muted small">🚩 ${enc(x.type||'other')}${reason?' · '+enc(reason.slice(0,120)):''} · ${timeAgo(x.created_at)}</div></div><span class="muted" style="align-self:center">›</span></div>`;
      }).join('') : '<div class="empty">No reports for this user. 🎉</div>';
      $$('.rep-row',list).forEach(el=> el.onclick=()=> showReportDetail(reports[+el.dataset.i]));
    });
  }
  // The full report: who filed it, the type, the full reason, and the reported post (fetched if we
  // can find it).
  async function showReportDetail(rep){
    if(!rep) return;
    const rp=Store.profile(rep.reporter)||{};
    const rn=rp.name||rp.display_name||(NT().nip19.npubEncode(rep.reporter).slice(0,16)+'…');
    const reason=(rep.reason||'').trim();
    modal(`<h3>🚩 Report</h3>
      <div class="report-detail">
        <div class="rd-row"><span class="muted small">Reported by</span> <b class="lnk" data-prof="${rep.reporter}">${enc(rn)}</b></div>
        <div class="rd-row"><span class="muted small">Type</span> <span class="rep-type">${enc(rep.type||'other')}</span> <span class="muted small">· ${timeAgo(rep.created_at)}</span></div>
        ${reason?`<div class="rd-reason">${linkify(reason)}</div>`:'<div class="muted small">No reason given.</div>'}
        ${rep.event?'<div id="rd-event"><div class="spinner"></div></div>':''}
      </div>`, async root=>{
      $$('[data-prof]',root).forEach(el=> el.onclick=()=>{ closeModal(); renderProfileView(el.dataset.prof); });
      if(rep.event){
        let ev=Store.get(rep.event);
        if(!ev){ try{ (await Relay.query([{ids:[rep.event]}])).forEach(e=>Store.saveEvent(e)); ev=Store.get(rep.event); }catch(_){} }
        const box=$('#rd-event',root); if(!box) return;
        box.innerHTML = ev ? ('<div class="muted small" style="margin:8px 0 4px">Reported post:</div>'+noteHtml(ev))
                           : `<div class="muted small">Reported post isn't available here (id ${enc(rep.event.slice(0,12))}…).</div>`;
        if(ev) decorateProfiles();
      }
    });
  }
  // admin: backfill this account's Nostr post history into the built-in relay (the "Sync a user's
  // data" action from Admin → Relay). Signed like doBlock so the server checks admin.
  async function doRelaySync(pk){
    if(!IS_ADMIN) return;
    try {
      const auth = await sign(27235, 'relay-sync', [['action','sync'],['p',pk]]);
      const r = await fetch('/client/relay-sync', { method:'POST', headers:{'Content-Type':'application/json'},
        body: JSON.stringify({ target: pk, auth: btoa(JSON.stringify(auth)) }) }).then(r=>r.json());
      toast(r.ok ? 'sync queued — notes backfilling 🔄' : ('sync failed: ' + (r.error||'')));
    } catch(e){ toast('sync failed'); }
  }
  // admin: delete ALL of this account's blobs from the built-in Blossom server (bytes + index rows).
  // Irreversible; signed like doBlock so the server checks admin.
  async function doPurgeBlossom(pk){
    if(!IS_ADMIN) return;
    if(!confirm('Purge ALL of this user\'s files from the Blossom server? This permanently deletes the stored bytes and cannot be undone.')) return;
    try {
      const auth = await sign(27235, 'blossom-purge', [['action','purge'],['p',pk]]);
      const r = await fetch('/client/blossom-purge', { method:'POST', headers:{'Content-Type':'application/json'},
        body: JSON.stringify({ target: pk, auth: btoa(JSON.stringify(auth)) }) }).then(r=>r.json());
      toast(r.ok ? ('purged '+(r.deleted||0)+' file(s) 🗑️') : ('purge failed: ' + (r.error||'')));
    } catch(e){ toast('purge failed'); }
  }
  // admin: per-user feature permissions (image/music/video/torrent) from the profile menu — replaces
  // the Admin → Users capability toggles.
  // admin: toggle a single per-user capability (e.g. can_torrent) inline from the profile menu.
  async function toggleCap(pk, cap, val){
    if(!IS_ADMIN) return;
    try{
      const auth = await sign(27235, 'user-caps', [['p',pk]]);
      const r = await fetch('/client/user-caps', { method:'POST', headers:{'Content-Type':'application/json'},
        body: JSON.stringify({ target: pk, caps:{[cap]:val}, auth: btoa(JSON.stringify(auth)) }) }).then(r=>r.json());
      toast(r.ok ? (val?'access granted':'access revoked') : ('failed: '+(r.error||'')));
    }catch(e){ toast('change failed'); }
  }
  // admin: one consolidated permissions panel for a user — AI access, Blossom uploads, and the
  // per-feature caps (image/music/video/torrent). Each maps to its own endpoint; on save we only
  // sign + call the ones that actually changed (fewer signer prompts).
  async function openPermissions(pk){
    if(!IS_ADMIN) return;
    let caps={}, aiOn=false, blossomOn=false, bridgeOn=false;
    try{ const r=await fetch('/client/ai-access?pubkey='+encodeURIComponent(pk)).then(r=>r.json()); aiOn=!!(r&&r.enabled); }catch(_){}
    try{ const r=await fetch('/client/blossom-access?pubkey='+encodeURIComponent(pk)).then(r=>r.json()); blossomOn=!!(r&&r.whitelisted); }catch(_){}
    try{ const r=await fetch('/client/bridge-access?pubkey='+encodeURIComponent(pk)).then(r=>r.json()); bridgeOn=!!(r&&r.enabled); }catch(_){}
    try{ const r=await fetch('/client/user-caps?pubkey='+encodeURIComponent(pk)).then(r=>r.json()); if(r&&r.exists) caps=r.caps||{}; }catch(_){}
    let nipName='', nipDomain=location.host;
    try{ const r=await fetch('/client/admin-nip05?pubkey='+encodeURIComponent(pk)).then(r=>r.json()); if(r&&r.ok){ nipName=r.name||''; if(r.nip05) nipDomain=r.nip05.split('@')[1]||nipDomain; } }catch(_){}
    const _pp=profOf(pk)||{};
    const defNip=((_pp.name||_pp.display_name||'')).toLowerCase().replace(/[^a-z0-9_.\-]/g,'').replace(/^[._\-]+|[._\-]+$/g,'').slice(0,30);
    const C=[['can_image','🖼️ Image'],['can_music','🎵 Music'],['can_video','🎬 Video'],['can_torrent','🧲 Torrents']];
    const row=(id,checked,label)=>`<label class="fld" style="flex-direction:row;align-items:center;gap:8px"><input type="checkbox" ${id} ${checked?'checked':''}> ${label}</label>`;
    modal(`<h3>🔑 Additional permissions</h3>
      ${row('id="perm-ai"', aiOn, '🤖 AI access')}
      ${row('id="perm-blossom"', blossomOn, '🌸 Blossom uploads')}
      <label class="fld" style="flex-direction:row;align-items:center;gap:8px"><input type="checkbox" id="perm-nip05" ${nipName?'checked':''}> 🪪 NIP-05 <span class="muted small">${enc((nipName||defNip||('user'+pk.slice(0,8)))+'@'+nipDomain)}</span></label>
      ${row('id="perm-bridge"', bridgeOn, '🌉 Bridge Access <span class="muted small">(create fedi account + enable bridge)</span>')}
      <hr style="border:none;border-top:1px solid var(--line,#333);margin:10px 0">
      <p class="muted small">AI features</p>
      ${C.map(([k,l])=>row('data-cap="'+k+'"', !!caps[k], l)).join('')}
      <button class="btn btn-neon full" id="caps-save">Save</button>`, root=>{
      $('#caps-save',root).onclick=async()=>{
        const wantAi=$('#perm-ai',root).checked, wantBl=$('#perm-blossom',root).checked;
        const out={}; let capsChanged=false;
        $$('[data-cap]',root).forEach(c=>{ out[c.dataset.cap]=c.checked; if(c.checked!==!!caps[c.dataset.cap]) capsChanged=true; });
        let ok=true;
        try{
          if(wantAi!==aiOn){
            const auth=await sign(27235,'ai-access',[['action',wantAi?'grant':'revoke'],['p',pk]]);
            const r=await fetch('/client/ai-access',{method:'POST',headers:{'Content-Type':'application/json'},
              body:JSON.stringify({target:pk,grant:wantAi,auth:btoa(JSON.stringify(auth))})}).then(r=>r.json()); ok=ok&&r.ok;
          }
          if(wantBl!==blossomOn){
            const auth=await sign(27235,'blossom',[['action',wantBl?'grant':'revoke'],['p',pk]]);
            const r=await fetch('/client/blossom-access',{method:'POST',headers:{'Content-Type':'application/json'},
              body:JSON.stringify({target:pk,grant:wantBl,auth:btoa(JSON.stringify(auth))})}).then(r=>r.json()); ok=ok&&r.ok;
          }
          // NIP-05: simple toggle — grant their-own-name@domain when checked, remove when unchecked.
          const wantNip=$('#perm-nip05',root).checked;
          if(wantNip && !nipName){
            const auth=await sign(27235,'nip05',[['action','grant'],['p',pk]]);
            const r=await fetch('/client/admin-nip05',{method:'POST',headers:{'Content-Type':'application/json'},
              body:JSON.stringify({target:pk, name:(defNip||('user'+pk.slice(0,8))), auth:btoa(JSON.stringify(auth))})}).then(r=>r.json());
            ok=ok&&r.ok; if(r&&!r.ok&&r.error) toast(r.error);
          } else if(!wantNip && nipName){
            const auth=await sign(27235,'nip05',[['action','revoke'],['p',pk]]);
            const r=await fetch('/client/admin-nip05',{method:'POST',headers:{'Content-Type':'application/json'},
              body:JSON.stringify({target:pk,remove:true,auth:btoa(JSON.stringify(auth))})}).then(r=>r.json());
            ok=ok&&r.ok; if(r&&!r.ok&&r.error) toast(r.error);
          }
          if(capsChanged){
            const auth=await sign(27235,'user-caps',[['p',pk]]);
            const r=await fetch('/client/user-caps',{method:'POST',headers:{'Content-Type':'application/json'},
              body:JSON.stringify({target:pk,caps:out,auth:btoa(JSON.stringify(auth))})}).then(r=>r.json()); ok=ok&&r.ok;
          }
          const wantBridge=$('#perm-bridge',root).checked;
          if(wantBridge!==bridgeOn){
            toast(wantBridge?'creating fediverse account…':'disabling bridge…');
            const auth=await sign(27235,'bridge-access',[['action',wantBridge?'grant':'revoke'],['p',pk]]);
            const r=await fetch('/client/bridge-access',{method:'POST',headers:{'Content-Type':'application/json'},
              body:JSON.stringify({target:pk,grant:wantBridge,auth:btoa(JSON.stringify(auth))})}).then(r=>r.json());
            ok=ok&&r.ok; if(r&&!r.ok&&r.error) toast(r.error);
          }
          toast(ok?'permissions saved':'some changes failed'); closeModal();
        }catch(_){ toast('save failed'); }
      };
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
      <label class="fld">ɱ Monero address<input class="input" id="pf-xmr" placeholder="4… or 8… (XMR — others can tip you)" value="${enc(xmrOf(p))}"></label>
      <label class="fld">Picture URL<input class="input" id="pf-pic" placeholder="https://…" value="${enc(p.picture||'')}"></label>
      <label class="fld">Banner URL<input class="input" id="pf-banner" placeholder="https://…" value="${enc(p.banner||'')}"></label>
      <label class="fld">About<textarea id="pf-about" placeholder="a few words about you">${enc(p.about||'')}</textarea></label>
      <div class="row"><button class="mini" id="pf-up">🖼 upload pic</button><input type="file" id="pf-file" accept="image/*" hidden><span class="spacer"></span><button class="btn btn-neon" id="pf-save">Save</button></div>`, root=>{
      $('#pf-up',root).onclick=()=>$('#pf-file',root).click();
      $('#pf-file',root).onchange=async e=>{ const f=e.target.files[0]; if(!f)return; try{ $('#pf-pic',root).value=await uploadBlob(f); toast('uploaded'); }catch(err){toast('upload failed');} };
      $('#pf-save',root).onclick=async()=>{ const _xmr=$('#pf-xmr',root).value.trim();
        if(_xmr && !isXmrAddr(_xmr)){ toast('that doesn\'t look like a Monero address (starts 4 or 8)'); $('#pf-xmr',root).focus(); return; }   // keeps the modal open → other edits aren't lost
        const meta={ ...p, name:$('#pf-name',root).value.trim(), nip05:$('#pf-nip05',root).value.trim(), lud16:$('#pf-lud16',root).value.trim(), xmr:_xmr, picture:$('#pf-pic',root).value.trim(), banner:$('#pf-banner',root).value.trim(), about:$('#pf-about',root).value.trim() };
        delete meta.monero; delete meta.monero_address;   // `xmr` is canonical — drop legacy aliases so clearing the field actually removes the address (xmrOf reads them too)
        closeModal(); await publish(0, JSON.stringify(meta), []); Store.saveProfile({pubkey:ME.pubkey,created_at:Math.floor(Date.now()/1000),content:JSON.stringify(meta)}); toast('profile saved'); renderMe(); renderProfileView(ME.pubkey); };
    });
  }
  // Show the relays a user publishes to (NIP-65 kind-10002), with read/write markers.
  async function showRelays(pk){
    modal(`<h3>🖧 Relays</h3><div id="rl-body" class="muted small">Loading…</div>`);
    let evs=[]; try{ evs=await Relay.query([{authors:[pk],kinds:[10002],limit:1}]); }catch(_){}
    const ev=(evs||[]).sort((a,b)=>b.created_at-a.created_at)[0];
    const body=$('#rl-body'); if(!body) return;
    const rs=ev ? (ev.tags||[]).filter(t=>t[0]==='r'&&t[1]) : [];
    if(!rs.length){ body.textContent='This user hasn’t published a relay list (NIP-65).'; return; }
    body.classList.remove('muted','small');
    body.innerHTML='<div class="prof-relays">'+rs.map(t=>{
      const mode = t[2] ? enc(t[2]) : 'read/write';
      return `<div class="prof-relay"><code>${enc(t[1])}</code><span class="muted small">${mode}</span></div>`;
    }).join('')+'</div>';
  }
  async function peopleModal(title, pks){
    modal(`<div style="display:flex;align-items:center;justify-content:space-between;gap:8px;flex-wrap:wrap"><h3 style="margin:0">${enc(title)} (${pks.length})</h3><button id="follow-all-back" class="btn btn-cyan small" style="display:none">Follow all back</button></div><div id="people-list" class="people-list"><div class="spinner"></div></div>`, async root=>{
      const miss=pks.filter(p=>!Store.haveProfile(p)).slice(0,300);
      if(miss.length){ try{ const evs=await Relay.query([{authors:miss,kinds:[0],limit:miss.length}]); evs.forEach(e=>Store.saveProfile(e)); }catch(_){} }
      await ensureMyFollowers();   // so we can flag mutuals ("Follows you") in any people list
      const list=$('#people-list',root); if(!list) return;
      list.innerHTML = pks.length ? pks.slice(0,400).map(p=>{ const m=Store.profile(p)||{};
        // "Follows you" badge = this person follows ME back (mutual). "Follow back" button = anyone I
        // don't follow yet — so the Following list shows who's reciprocal and the Followers list shows
        // who I haven't followed back.
        const followsMe = p!==ME.pubkey && FOLLOWERS.has(p);
        const canFollow = p!==ME.pubkey && !FOLLOWS.has(p);
        return `<div class="psearch" data-prof="${p}"><img src="${enc(m.picture||LOGO)}" onerror="this.src='${LOGO}'"><div class="pinfo"><b>${enc(m.name||m.display_name||NT().nip19.npubEncode(p).slice(0,14))}${followsMe?'<span class="follows-you">Follows you</span>':''}</b><div class="muted small">${enc(niceNip05(m.nip05)||'')}</div></div>${canFollow?`<button class="btn btn-cyan small pfollow" data-fb="${p}">Follow back</button>`:''}</div>`;
      }).join('') : '<div class="empty">Nobody here.</div>';
      $$('[data-prof]',list).forEach(el=> el.onclick=(ev)=>{ if(ev.target.closest('.pfollow')) return; closeModal(); renderProfileView(el.dataset.prof); });
      $$('.pfollow',list).forEach(b=> b.onclick=async(ev)=>{ ev.stopPropagation(); b.disabled=true; b.textContent='…';
        try{ await toggleFollow(b.dataset.fb); b.textContent='Following ✓'; b.classList.remove('btn-cyan'); b.classList.add('btn-ghost'); }
        catch(_){ b.disabled=false; b.textContent='Follow back'; } });
      // "Follow all back": one-tap follow of everyone in this list I don't already follow (one publish).
      const followable=pks.filter(p=>p!==ME.pubkey && !FOLLOWS.has(p));
      const fab=$('#follow-all-back',root);
      if(fab && followable.length){
        fab.style.display=''; fab.textContent=`Follow all back (${followable.length})`;
        fab.onclick=async()=>{ fab.disabled=true; const orig=fab.textContent; fab.textContent='…';
          try{ const n=await followMany(followable);
            $$('.pfollow',list).forEach(b=>{ b.disabled=true; b.textContent='Following ✓'; b.classList.remove('btn-cyan'); b.classList.add('btn-ghost'); });
            fab.style.display='none'; toast(`followed ${n} back`);
            if(VIEW==='home') renderView(true);
          }catch(_){ fab.disabled=false; fab.textContent=orig; } };
      }
    });
  }
  // ---------- AI view (the old PosterChan AI web UI, merged in as a client view) ----------
  let _aiAuth = null;   // cached {can_ai, is_admin, username} for this session
  let _aiAuthP=null;
  async function ensureAiSession(){
    if(_aiAuth) return _aiAuth;
    if(_aiAuthP) return _aiAuthP;   // dedupe concurrent callers (e.g. the 2.5s warm + a click) → one sign(), one login
    _aiAuthP = (async()=>{
      try{
        const auth = await sign(27235, 'ai-login', [['p', ME.pubkey]]);   // prove key ownership
        const r = await fetch('/api/auth/nostr-login', { method:'POST', headers:{'Content-Type':'application/json'},
          body: JSON.stringify({ pubkey: ME.pubkey, auth: btoa(JSON.stringify(auth)) }) }).then(r=>r.json());
        if(r && r.user){ _aiAuth = r.user; return _aiAuth; }   // cache only a GOOD session
        return { can_ai:false, error:!r };                      // transient failure → not cached, retryable
      }catch(_){ return { can_ai:false, error:true }; }
      finally{ _aiAuthP=null; }
    })();
    return _aiAuthP;
  }
  // In-app Admin: the standalone admin panel embedded in the client (same-origin, the Nostr-login
  // cookie authorizes it). Admins only.
  // Persistent admin iframe: created ONCE (hidden) and kept alive — opening Admin just reveals it.
  // The iframe is shown only after it has loaded (opacity 0→1 on onload, spinner until then), so you
  // never see a blank/half-rendered frame. Preloaded at startup (see _preloadAdmin) so the first
  // open from home/global is instant instead of timeline→spinner→blank-iframe→content (the flicker).
  function _ensureAdminHost(){
    let host=document.getElementById('admin-host');
    if(!host){
      host=document.createElement('div'); host.id='admin-host'; host.style.display='none';
      host.innerHTML='<div class="spinner"></div>';
      const ifr=document.createElement('iframe'); ifr.className='admin-frame'; ifr.src='/admin?t='+Date.now(); ifr.title='Admin'; ifr.style.opacity='0';
      ifr.addEventListener('load', ()=>{ ifr.dataset.loaded='1'; ifr.style.opacity='1'; const sp=host.querySelector('.spinner'); if(sp) sp.remove(); });
      host.appendChild(ifr);
      (document.querySelector('.main')||document.body).appendChild(host);
    }
    return host;
  }
  function _preloadAdmin(){ _ensureAdminHost(); }   // load /admin hidden so the first open is instant
  function _adminFrame(feed){
    // The iframe is created + loaded ONCE (post-auth, see _ensureAdminHost / _preloadAdmin) and kept
    // alive — re-entering admin just REVEALS it, never reloads it. (Reloading on every enter made the
    // panel slow + flickery and re-ran all its fetches.) After a deploy, a full page refresh picks up
    // new admin CSS/JS.
    const host=_ensureAdminHost();
    feed.style.display='none';   // hide the feed; the persistent iframe fills the main area
    host.style.display='block';
    const ifr=host.querySelector('iframe');
    if(ifr && ifr.dataset.loaded==='1') ifr.style.opacity='1';   // already loaded → show instantly
  }
  function renderAdmin(){
    const feed=$('#feed');
    if(!IS_ADMIN){ feed.innerHTML='<div class="empty">Admins only.</div>'; return; }
    // /admin needs the session cookie nostr-login sets. If it's already established, render the
    // iframe SYNCHRONOUSLY (no await → no window for a re-render to clobber it). Otherwise show a
    // spinner and render when it resolves.
    if(_aiAuth && _aiAuth.is_admin){ _adminFrame(feed); return; }
    feed.innerHTML='<div class="spinner"></div>';
    ensureAiSession().then(a=>{
      if(VIEW!=='admin') return;
      if(a && a.is_admin) _adminFrame(feed);
      else feed.innerHTML='<div class="empty">Admin session unavailable — log in with your admin Nostr key.</div>';
    });
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
  let _ai = { ws:null, convId:null, streamEl:null, streamBuf:"", attach:[], replyTo:null, fxImage:null, fxMedia:{}, pendingFx:null, awaiting:false };
  function _cookie(name){ const m=document.cookie.match(new RegExp('(?:^|; )'+name+'=([^;]*)')); return m?decodeURIComponent(m[1]):''; }

  async function aiMount(feed){
    feed.innerHTML=`<div class="ai-chat">
      <div class="ai-bar"><select id="ai-conv" class="input"></select><button class="btn btn-ghost small" id="ai-new">＋ New</button><button class="btn btn-ghost small" id="ai-tts" title="Voice narration">🔊</button><button class="btn btn-ghost small" id="ai-del" title="delete this chat">🗑️</button></div>
      <div class="ai-msgs" id="ai-msgs"></div>
      <div class="ai-compose">
        <button class="mini" id="ai-attach" title="attach">📎</button><input type="file" id="ai-file" multiple hidden>
        <button class="mini" id="ai-mic" title="Voice input (speech-to-text)">🎤</button>
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
    { const mic=$('#ai-mic'); if(mic) mic.onclick=aiToggleMic; }
    { const tb=$('#ai-tts'); if(tb) tb.onclick=aiToggleTTS; _aiTtsBtn(); }
    $('#ai-send').onclick=aiSend;
    const ta=$('#ai-input');
    ta.addEventListener('keydown',e=>{ if(e.key==='Enter' && !e.shiftKey){ e.preventDefault(); aiSend(); } });
    ta.addEventListener('input',()=>{ ta.style.height='auto'; ta.style.height=Math.min(ta.scrollHeight,200)+'px'; aiUpdateLinkActions(); });
    // Ctrl/⌘-V an image from the clipboard → attach it (so you can paste a screenshot then `post`, etc.)
    ta.addEventListener('paste', async e=>{
      const items=(e.clipboardData && e.clipboardData.items)||[]; const files=[];
      for(const it of items){ if(it.type && it.type.startsWith('image/')){ const f=it.getAsFile();
        if(f) files.push(f.name?f:new File([f],'pasted-'+Date.now()+'.png',{type:f.type||'image/png'})); } }
      if(files.length){ e.preventDefault(); await aiAddFiles(files); toast(files.length+' image'+(files.length>1?'s':'')+' attached'); }
    });
    // Drag-and-drop files onto the chat → attach them (same as the 📎 button). Recurses dropped folders.
    { const chat=feed.querySelector('.ai-chat');
      if(chat){
        chat.addEventListener('dragover',e=>{ if(e.dataTransfer&&[...(e.dataTransfer.types||[])].includes('Files')){ e.preventDefault(); chat.classList.add('ai-drop'); } });
        chat.addEventListener('dragleave',e=>{ if(e.target===chat) chat.classList.remove('ai-drop'); });
        chat.addEventListener('drop',async e=>{ if(!(e.dataTransfer&&[...(e.dataTransfer.types||[])].includes('Files'))) return;
          e.preventDefault(); chat.classList.remove('ai-drop');
          const dt=e.dataTransfer, items=dt&&dt.items, entries=[];
          if(items&&items.length&&items[0].webkitGetAsEntry){ for(let i=0;i<items.length;i++){ const en=items[i].webkitGetAsEntry(); if(en) entries.push(en); } }
          let files=[];
          if(entries.length){ files=await _walkEntries(entries); } else { files=[...((dt&&dt.files)||[])]; }
          if(files.length){ await aiAddFiles(files); toast(files.length+' file'+(files.length>1?'s':'')+' attached'); }
        });
      } }
    $('#ai-msgs').addEventListener('click',e=>{
      const eg=e.target.closest('.ai-eg'); if(eg){ e.preventDefault(); const ta=$('#ai-input'); if(ta){ ta.value=eg.dataset.cmd; ta.focus(); ta.dispatchEvent(new Event('input')); } return; }   // welcome example → prefill, let the user type
      const cmd=e.target.closest('.ai-cmd'); if(cmd){ e.preventDefault(); const ta=$('#ai-input'); if(ta){ ta.value=cmd.dataset.cmd; aiSend(); } return; }
      const ab=e.target.closest('.ai-addbill'); if(ab){ e.preventDefault();
        const income=ab.dataset.income==='1';
        const v=prompt(income?'Add income — name and amount, e.g. "Paycheck 2000"':'Add bill — name and amount, e.g. "Rent 1200"');
        if(v && v.trim()){ const ta=$('#ai-input'); if(ta){ ta.value='addbill '+v.trim()+(income?' income':''); aiSend(); } } return; }
      const fxc=e.target.closest('.fx-cmd'); if(fxc){ e.preventDefault();
        if(fxc.dataset.cmd==='__fxguide'){ showEffectGuide(); return; }   // 🎬 Effects → open the studio picker
        const ta=$('#ai-input'); if(ta){ if(_ai.fxImage && !_ai.attach.length) aiAddFiles([_ai.fxImage]); _fxSetEffect(ta, fxc.dataset.cmd); ta.focus(); ta.dispatchEvent(new Event('input')); } return; }   // effect chip → set base effect (keeps motion/caption)
      const fxm=e.target.closest('.fx-mot'); if(fxm){ e.preventDefault(); const ta=$('#ai-input'); if(ta){ if(_ai.fxImage && !_ai.attach.length) aiAddFiles([_ai.fxImage]); _fxApplyMod(ta, fxm.dataset.add); ta.focus(); ta.dispatchEvent(new Event('input')); } return; }   // motion → single geometry / glow·alive·trippy compose
      const fxh=e.target.closest('.fx-char'); if(fxh){ e.preventDefault(); const ta=$('#ai-input'); if(ta){ if(_ai.fxImage && !_ai.attach.length) aiAddFiles([_ai.fxImage]); _fxApplyChar(ta, fxh.dataset.char); ta.focus(); ta.dispatchEvent(new Event('input')); } return; }   // sticker (char overlay) → single, toggle
      const rfx=e.target.closest('.ai-reply-fx'); if(rfx){ e.preventDefault(); sendEffectReply(rfx.dataset.mid, rfx); return; }   // post the generated effect back as a reply
      const cfx=e.target.closest('.ai-copy-fx'); if(cfx){ e.preventDefault(); copyEffectUrl(cfx.dataset.mid, cfx); return; }   // upload + copy the public Blossom URL
      const cpf=e.target.closest('.ai-copyfile'); if(cpf){ e.preventDefault(); copyFileUrl(cpf.dataset.url, cpf); return; }   // inline /api/files/ media → re-upload + copy public URL
      const rpf=e.target.closest('.ai-replyfile'); if(rpf){ e.preventDefault(); replyFileUrl(rpf.dataset.url, rpf); return; }
      const ppf=e.target.closest('.ai-postfile'); if(ppf){ e.preventDefault(); postFileUrl(ppf.dataset.url, ppf); return; }     // share generated media → new Nostr post
      const pfx=e.target.closest('.ai-post-fx'); if(pfx){ e.preventDefault(); postEffectMedia(pfx.dataset.mid, pfx); return; }   // share effect media → new Nostr post
      const mag=e.target.closest('.ai-magnet'); if(mag){ const ta=$('#ai-input'); if(ta){ ta.value='torrents add '+mag.dataset.magnet; aiSend(); } return; }
      const fco=e.target.closest('.fc-opt'); if(fco){ e.preventDefault(); const st=_ai.decks&&_ai.decks[fco.dataset.fc]; if(st && st.answered[st.idx]==null){ const i=+fco.dataset.opt; st.answered[st.idx]=i; if(i===(st.cards[st.idx]||{}).correct) st.score++; _fcRedraw(fco.dataset.fc); } return; }   // answer a card → ✓/✗ + explanation
      const fcn=e.target.closest('.fc-next'); if(fcn){ e.preventDefault(); const st=_ai.decks&&_ai.decks[fcn.dataset.fc]; if(st && st.idx<st.cards.length-1){ st.idx++; _fcRedraw(fcn.dataset.fc); } return; }
      const fcp=e.target.closest('.fc-prev'); if(fcp){ e.preventDefault(); const st=_ai.decks&&_ai.decks[fcp.dataset.fc]; if(st && st.idx>0){ st.idx--; _fcRedraw(fcp.dataset.fc); } return; }
      const fcr=e.target.closest('.fc-restart'); if(fcr){ e.preventDefault(); const st=_ai.decks&&_ai.decks[fcr.dataset.fc]; if(st){ st.idx=0; st.score=0; st.answered=new Array(st.cards.length).fill(null); _fcRedraw(fcr.dataset.fc); } return; }
      const im=e.target.closest('img'); if(im){ openLightbox(im.dataset.full||im.src); }
    });
    await aiLoadConversations();
    // 🎬 Effect handoff: if we entered the AI view to apply an effect to a post's image, set it up now
    // that the chat is fully mounted (fixes the race where the conv load wiped the attached image).
    if(_ai.pendingFx){ const fx=_ai.pendingFx; _ai.pendingFx=null; await startEffectStudio(fx.url); }
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
      await aiOpenConversation(c.id);   // await so callers (e.g. the Effects studio) attach AFTER the conv render settles
      return c.id;
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
    if(!id) return; _ai.convId=id; _ai.streamEl=null; _ai.streamBuf=""; _ai.decks={};   // decks re-hydrate from [[FC]] markers on render — drop the old set so it can't leak across opens
    const sel=$('#ai-conv'); if(sel && sel.value!=String(id)) sel.value=String(id);
    const box=$('#ai-msgs'); if(box) box.innerHTML='<div class="spinner"></div>';
    let conv=null; try{ conv=await fetch('/api/conversations/'+id).then(r=>r.json()); }catch(_){}
    if(VIEW!=='ai' || _ai.convId!==id) return;
    if(box){ box.innerHTML='';
      const msgs = (conv && conv.messages) || [];
      if(!msgs.length) box.innerHTML = _aiWelcomeHtml();   // fresh chat → friendly splash with starter commands
      for(const m of msgs){
        let html = m.role==='user'?enc(m.content):aiFormat(m.content||'');
        if(m.image_path) html += `<div class="ai-media"><img src="${enc(m.image_path)}" loading="lazy" onerror="window.__aiMediaRetry(this)"></div>`;
        aiAddMessage(m.role, html);
      }
      aiScroll();
    }
    aiConnect(id);
  }
  function _aiWelcomeHtml(){
    return `<div class="ai-welcome">
      <img class="aw-logo" src="${LOGO}" alt="PosterChan" onerror="this.style.display='none'">
      <h3>Welcome to PosterChan AI</h3>
      <p class="muted">Just chat with me, or run a command. A few to try (tap to fill it in):</p>
      <div class="aw-cmds">
        <button class="ai-eg" data-cmd="geni ">🎨 <b>geni</b> &lt;prompt&gt;<span>generate an image</span></button>
        <button class="ai-eg" data-cmd="musicgeni ">🎵 <b>musicgeni</b> &lt;prompt&gt;<span>generate a song</span></button>
        <button class="ai-eg" data-cmd="ytdl mp3 ">🎧 <b>ytdl mp3</b> &lt;url&gt;<span>download audio as MP3</span></button>
        <button class="ai-eg" data-cmd="ytdl video ">🎬 <b>ytdl video</b> &lt;url&gt;<span>download a video</span></button>
        <button class="ai-eg" data-cmd="screenshot ">📸 <b>screenshot</b> &lt;url&gt;<span>capture a web page</span></button>
        <button class="ai-eg" data-cmd="translate ">🌐 <b>translate</b> &lt;text&gt;<span>translate text</span></button>
        <button class="ai-eg" data-cmd="search ">🔍 <b>search</b> &lt;query&gt;<span>web search</span></button>
        <button class="ai-eg" data-cmd="images ">🖼️ <b>images</b> &lt;query&gt;<span>image search</span></button>
      </div>
      <p class="muted small">Type <span class="ai-cmd" data-cmd="help">help</span> for the full list of commands.</p>
    </div>`;
  }
  function aiConnect(id){
    try{ if(_ai.ws){ _ai.ws.onclose=null; _ai.ws.close(); } }catch(_){}
    clearTimeout(_ai.wsWatch);
    const proto = location.protocol==='https:'?'wss':'ws';
    const tok=_cookie('access_token');
    let opened=false;
    const ws=new WebSocket(`${proto}://${location.host}/api/ws/chat/${id}`+(tok?`?token=${encodeURIComponent(tok)}`:''));
    _ai.ws=ws;
    ws.onopen=()=>{ opened=true; _ai.wsBroken=false; clearTimeout(_ai.wsWatch); const q=_ai.pending||[]; _ai.pending=[]; for(const p of q){ try{ ws.send(JSON.stringify(p)); }catch(_){} } };
    ws.onmessage=e=>{ let d; try{ d=JSON.parse(e.data); }catch(_){ return; } aiHandle(d); };
    // No keepalive on this WS: a slow effect/image/video generation can outlast an idle/proxy timeout
    // and the socket closes mid-flight. The server still finishes + PERSISTS the reply, so its live push
    // was lost and the answer only appeared after a manual refresh ("sometimes I never get an update").
    // If a reply was pending, pull it in (below). Idle drops need nothing — aiWsSend reconnects on send.
    ws.onclose=()=>{ if(_ai.ws===ws && _ai.awaiting && VIEW==='ai' && _ai.convId===id) aiRecover(id); };
    // If the socket can't even OPEN — e.g. a CDN/proxy that drops the WS upgrade (Cloudflare over
    // HTTP/3 does this) — a queued message would sit forever and never send. After a grace period,
    // fall back to plain HTTP (POST /api/chat/send) so the command still runs + persists. Every later
    // send then goes straight over HTTP too, until a socket actually opens again (self-heals).
    _ai.wsWatch = setTimeout(()=>{ if(!opened){ _ai.wsBroken=true; aiHttpFlush(id); } }, 6000);
  }
  // WS upgrade failed → run any queued payloads over plain HTTP (the endpoint persists exactly like the
  // WS), then re-render the conversation so the reply shows. Used transparently when the socket won't open.
  async function aiHttpFlush(id){
    const q=_ai.pending||[]; _ai.pending=[];
    for(const p of q){ await aiHttpSend(p, id); }
  }
  async function aiHttpSend(payload, id){
    id = id || _ai.convId; if(!id) return;
    try{
      const r=await fetch('/api/chat/send',{ method:'POST', headers:{'Content-Type':'application/json'},
        body:JSON.stringify({ conversation_id:id, content:payload.content||'', images:payload.images||[],
          pdfs:payload.pdfs||[], documents:payload.documents||[], videos:payload.videos||[], files:payload.files||[] }) });
      if(!r.ok) throw new Error('http '+r.status);
    }catch(e){ if(VIEW==='ai' && _ai.convId===id) aiAddMessage('assistant', enc('⚠️ Could not reach the server — try again.')); }
    _ai.awaiting=false;
    if(VIEW==='ai' && _ai.convId===id) aiOpenConversation(id);   // re-render the persisted reply (also retries the WS)
  }
  // The chat WS dropped while a reply was pending. Poll the persisted conversation (HTTP, no socket
  // needed) until the new assistant message lands — covers slow effects that finish after the drop —
  // then re-render it (which also reopens the WS). Bounded so it can't poll forever.
  function aiRecover(id){
    if(_ai.recovering) return;   // one poller at a time — onclose AND the stall-watchdog can both call this
    _ai.recovering=true;
    const had = $('#ai-msgs') ? $('#ai-msgs').querySelectorAll('.ai-msg.assistant').length : 0;
    let tries=0;
    (async function poll(){
      // ~5 min ceiling: a slow effect/video/music render can finish minutes after the socket drops. The
      // old 60s bound expired before the file landed, leaving the reply unseen until a manual refresh
      // (the recurring "clay never showed" report). Effects are the slowest path, so size for them.
      if(!_ai.awaiting || VIEW!=='ai' || _ai.convId!==id || ++tries>100){ _ai.recovering=false; return; }
      let conv=null; try{ conv=await fetch('/api/conversations/'+id).then(r=>r.json()); }catch(_){}
      const got=((conv&&conv.messages)||[]).filter(m=>m.role==='assistant').length;
      if(got>had){ _ai.awaiting=false; _ai.recovering=false; if(VIEW==='ai' && _ai.convId===id) aiOpenConversation(id); return; }
      setTimeout(poll, 3000);
    })();
  }
  // Send (or queue) a payload on the chat WS — never fail just because it's mid-connect; queue it
  // and the onopen handler flushes. Reconnects if the socket is closed.
  function aiWsSend(payload){
    if(_ai.wsBroken){ aiHttpSend(payload); return; }   // socket can't open here → straight to HTTP
    if(_ai.ws && _ai.ws.readyState===1){ try{ _ai.ws.send(JSON.stringify(payload)); return; }catch(_){} }
    (_ai.pending=_ai.pending||[]).push(payload);
    if(!_ai.ws || _ai.ws.readyState>1) aiConnect(_ai.convId);   // CLOSING/CLOSED → reconnect; CONNECTING → just wait
  }
  function aiAddMessage(role, html){
    const box=$('#ai-msgs'); if(!box) return null;
    const w=box.querySelector('.ai-welcome'); if(w) w.remove();   // first real message → drop the splash
    const el=document.createElement('div'); el.className='ai-msg '+(role==='user'?'user':'assistant');
    el.innerHTML=`<div class="ai-bubble">${html}</div>`;
    if(role!=='user' && !/ai-err/.test(html)){   // 🔊 manual read-aloud (built-in TTS); not on error bubbles
      const spk=document.createElement('button'); spk.textContent='🔊'; spk.title='Read aloud';
      spk.style.cssText='background:none;border:none;cursor:pointer;opacity:.55;font-size:13px;padding:2px 4px;align-self:flex-start';
      spk.onclick=()=>{ const b=el.querySelector('.ai-bubble'); aiSpeak(b?b.textContent:''); };   // manual click always speaks (even when auto-narration is muted)
      el.appendChild(spk);
    }
    box.appendChild(el); aiScroll(); return el;
  }
  // Voice narration of AI replies — on by default, mutable via the 🔊/🔇 toggle (matches the old UI).
  let _ttsEnabled = localStorage.getItem('ttsEnabled') !== 'false';
  function _aiTtsBtn(){ const b=$('#ai-tts'); if(b){ b.textContent=_ttsEnabled?'🔊':'🔇'; b.title=_ttsEnabled?'Voice narration on — tap to mute':'Voice narration muted — tap to enable'; } }
  function aiToggleTTS(){
    _ttsEnabled=!_ttsEnabled;
    try{ localStorage.setItem('ttsEnabled', _ttsEnabled); }catch(_){}
    if(!_ttsEnabled){ try{ if(_narrateAudio){ _narrateAudio.pause(); _narrateAudio=null; } }catch(_){} }   // mute = stop current
    _aiTtsBtn();
  }
  // Speak text via the node's built-in TTS. isAuto=true is the auto-narration path (skipped when muted);
  // a manual 🔊 click passes no flag and always speaks.
  async function aiSpeak(text, isAuto){
    if(isAuto && !_ttsEnabled) return;
    text=(text||'').replace(/https?:\/\/\S+/gi,' ').replace(/\s+/g,' ').trim().slice(0,2000);
    if(!text){ return; }
    try{ if(_narrateAudio){ _narrateAudio.pause(); _narrateAudio=null; } }catch(_){}
    toast('🔊 reading…');
    try{
      const r=await fetch('/client/narrate',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({text})});
      const j=await r.json().catch(()=>({}));
      if(!r.ok || !j.audio){ toast(j.error||'narration unavailable'); return; }
      _narrateAudio=new Audio('data:audio/mp3;base64,'+j.audio); _narrateAudio.play().catch(()=>toast('tap 🔊 to play'));
    }catch(_){ toast('narration failed'); }
  }
  // 🎤 Voice input: record a clip, transcribe via the node's Whisper STT, append to the AI input.
  let _aiRec=null, _aiChunks=[], _aiMicStarting=false;
  async function aiToggleMic(){
    const mic=$('#ai-mic');
    if(_aiRec && _aiRec.state==='recording'){ _aiRec.stop(); return; }
    if(_aiMicStarting) return;   // a start is already in flight (async getUserMedia) — ignore the double-tap
    if(!(navigator.mediaDevices && navigator.mediaDevices.getUserMedia && window.MediaRecorder)){ toast('voice input not supported on this browser'); return; }
    _aiMicStarting=true;
    let stream=null;
    try{
      stream=await navigator.mediaDevices.getUserMedia({audio:true});
      _aiChunks=[];
      const mime=['audio/webm;codecs=opus','audio/webm','audio/mp4',''].find(t=>!t || MediaRecorder.isTypeSupported(t));
      _aiRec=new MediaRecorder(stream, mime?{mimeType:mime}:undefined);
      _aiRec.ondataavailable=e=>{ if(e.data && e.data.size) _aiChunks.push(e.data); };
      _aiRec.onstop=async()=>{
        try{ stream.getTracks().forEach(t=>t.stop()); }catch(_){}   // always release the mic first
        if(mic){ mic.style.color=''; mic.textContent='🎤'; }
        const blob=new Blob(_aiChunks,{type:(_aiRec&&_aiRec.mimeType)||'audio/webm'});
        if(blob.size<200){ return; }
        toast('transcribing…');
        const fd=new FormData(); fd.append('audio', blob, 'voice.webm');
        try{
          const r=await fetch('/client/stt',{method:'POST',body:fd});
          const j=await r.json().catch(()=>({}));
          if(!r.ok || !(j.text||'').trim()){ toast(j.error||'voice input unavailable'); return; }
          const ta=$('#ai-input'); if(ta){ ta.value=(ta.value?ta.value.trim()+' ':'')+j.text.trim(); ta.focus(); ta.dispatchEvent(new Event('input')); }
        }catch(_){ toast('voice input failed'); }
      };
      _aiRec.start();
      if(mic){ mic.style.color='#ff4d6d'; mic.textContent='⏹'; }
      toast('🎤 recording — tap to stop');
    }catch(e){
      try{ if(stream) stream.getTracks().forEach(t=>t.stop()); }catch(_){}
      toast(e && e.name==='NotAllowedError' ? 'microphone permission denied' : 'could not start recording');
    }finally{
      _aiMicStarting=false;
    }
  }
  function aiScroll(){ const box=$('#ai-msgs'); if(box) box.scrollTop=box.scrollHeight; }
  function aiHandle(d){
    // Stale re-delivery: the server replays a queued `response` whose live push was missed (socket
    // dropped / user on another conversation). We reload the whole conversation from the DB on
    // open/recover, so replaying a reply that was PERSISTED would render it TWICE (the reported
    // double-geni-image). Skip only those (`persisted`). Non-persisted replays — e.g. interim agent
    // progress chunks the server never saves — still render, since the DB reload won't have them.
    if(d.pending && d.persisted){ if(d.type==='response') _ai.awaiting=false; return; }
    // Any live frame means the socket is healthy → push the stall-watchdog out so an actively-streaming
    // (or progress-reporting) reply never trips the recovery poll.
    if(_ai.awaiting && _ai.recoverWatch){ clearTimeout(_ai.recoverWatch); const _cid=_ai.convId;
      _ai.recoverWatch=setTimeout(()=>{ if(_ai.awaiting && VIEW==='ai' && _ai.convId===_cid) aiRecover(_cid); }, 30000); }
    if(d.type==='stream'){
      const c=(d.data&&d.data.content)??d.content??''; if(typeof c!=='string') return;
      if(!_ai.streamEl){ _ai.streamBuf=''; _ai.streamEl=aiAddMessage('assistant',''); }
      _ai.streamBuf+=c; const b=_ai.streamEl.querySelector('.ai-bubble'); if(b){ b.textContent=_ai.streamBuf; } aiScroll();
    } else if(d.type==='stream_clear'){
      _ai.streamBuf=''; if(_ai.streamEl){ const b=_ai.streamEl.querySelector('.ai-bubble'); if(b) b.textContent=''; }
    } else if(d.type==='stream_end'){
      const said=_ai.streamBuf;
      if(_ai.streamEl){ const b=_ai.streamEl.querySelector('.ai-bubble'); if(b) b.innerHTML=aiFormat(_ai.streamBuf); }
      _ai.streamEl=null; _ai.streamBuf=''; _ai.awaiting=false; aiScroll();
      aiSpeak(said, true);   // auto-narrate the reply (no-op when muted)
    } else if(d.type==='text'){
      aiAddMessage('assistant', aiFormat(d.content||'')); _ai.awaiting=false;
      aiSpeak(d.content||'', true);
    } else if(d.type==='response'){
      aiAddMessage('assistant', aiRenderResponse(d.data||{})); _ai.awaiting=false;
    } else if(d.type==='error'){
      aiAddMessage('assistant', `<span class="ai-err">⚠ ${enc(d.message||'error')}</span>`);
      _ai.streamEl=null; _ai.streamBuf=''; _ai.awaiting=false;
    } else if(d.type==='reminder'){
      reminderAlert((d.content!=null?d.content:(d.data&&d.data.content))||'Reminder');   // fired reminder → popup + sound
    }
  }
  // A reminder fired (pushed over the chat WS) — full-screen pulsing card + a beep, like the old UI.
  function reminderAlert(text){
    try{
      const ac=new (window.AudioContext||window.webkitAudioContext)();
      [0,0.18,0.36].forEach(t=>{ const o=ac.createOscillator(),g=ac.createGain(); o.connect(g); g.connect(ac.destination);
        o.type='sine'; o.frequency.value=880; g.gain.setValueAtTime(0.001,ac.currentTime+t); g.gain.exponentialRampToValueAtTime(0.25,ac.currentTime+t+0.02);
        g.gain.exponentialRampToValueAtTime(0.001,ac.currentTime+t+0.15); o.start(ac.currentTime+t); o.stop(ac.currentTime+t+0.16); });
    }catch(_){}
    try{ if(window.Notification && Notification.permission==='granted') new Notification('⏰ Reminder', {body:String(text).replace(/<[^>]+>/g,''), tag:'pc-reminder'}); }catch(_){}
    const ex=document.getElementById('reminderOverlay'); if(ex) ex.remove();
    const ov=document.createElement('div'); ov.id='reminderOverlay';
    ov.style.cssText='position:fixed;inset:0;z-index:600;display:grid;place-items:center;padding:24px;background:rgba(4,2,12,.8);backdrop-filter:blur(4px)';
    ov.innerHTML=`<div class="reminder-card"><div style="font-size:42px">⏰</div><h2 style="margin:10px 0">Reminder</h2>
      <div style="font-size:18px;margin-bottom:20px">${aiFormat(String(text||''))}</div>
      <button class="btn btn-neon" id="reminderDismiss">Dismiss</button></div>`;
    const close=()=>ov.remove();
    ov.addEventListener('click',e=>{ if(e.target===ov) close(); });
    document.body.appendChild(ov);
    const b=ov.querySelector('#reminderDismiss'); if(b) b.onclick=close;
  }
  // Markdown + the backend's custom inline markup the old web UI rendered: !video[](url), !audio[](url),
  // ![](url) images, links, and magnet/.torrent → an "add torrent" action. So command outputs
  // (musicgeni/videogeni/compress/clip = !video/!audio; torrents = magnet) display right, not as text.
  function aiFormat(src){
    src=String(src||''); const slots=[]; const stash=h=>{ slots.push(h); return ` S${slots.length-1} `; };
    // Persisted flashcard deck: [[FC]]<base64 JSON>[[/FC]] (saved server-side) → re-hydrate the
    // interactive quiz on reload, identical to the live render. Decode is unicode-safe (atob+escape).
    src=src.replace(/\[\[FC\]\]([A-Za-z0-9+/=]+)\[\[\/FC\]\]/g,(m,b64)=>{
      try{ const data=JSON.parse(decodeURIComponent(escape(atob(b64)))); if(data && Array.isArray(data.cards) && data.cards.length){
        _ai.decks=_ai.decks||{}; const id='fc'+Date.now().toString(36)+Math.floor(Math.random()*1e4).toString(36);
        _ai.decks[id]={ cards:data.cards, idx:0, answered:new Array(data.cards.length).fill(null), score:0, title:data.title };
        return stash(`<div class="flashcard-deck" id="${id}">${_fcRender(id)}</div>`);
      } }catch(_){}
      return '';
    });
    src=src.replace(/!video\[([^\]]*)\]\(\s*((?:https?:\/\/|\/)[^)\s]+)\s*\)/g,(m,a,u)=>stash(`<div class="ai-media"><video controls src="${enc(u)}" onerror="window.__aiMediaRetry(this)"></video></div>`+_aiFileActions(u)));
    src=src.replace(/!audio\[([^\]]*)\]\(\s*((?:https?:\/\/|\/)[^)\s]+)\s*\)/g,(m,a,u)=>stash(`<div class="ai-media"><audio controls src="${enc(u)}"></audio></div>`+_aiFileActions(u)));
    // inline images from a command output (effects/stamps, compress/convert) → show with the same
    // copy-link / reply buttons; stash BEFORE mdToHtml so it doesn't render a plain <img>.
    src=src.replace(/!\[([^\]]*)\]\(\s*((?:https?:\/\/|\/)[^)\s]+)\s*\)/g,(m,a,u)=>stash(`<div class="ai-media"><img src="${enc(u)}" data-full="${enc(u)}" onerror="window.__aiMediaRetry(this)"></div>`+_aiFileActions(u)));
    // Torrent browse buttons: [Download](cmd:torrents download tv 1) → a command button; and
    // [Add](magnet:<url-encoded magnet>) → an add-torrent button. (cmd: hrefs contain spaces that
    // would break markdown link parsing, so stash them BEFORE mdToHtml runs.)
    src=src.replace(/\[([^\]]+)\]\(cmd:([^)]+)\)/g,(m,label,cmd)=>stash(`<button class="ai-cmd" data-cmd="${enc(cmd.trim())}">${enc(label)}</button>`));
    src=src.replace(/\[([^\]]+)\]\(magnet:([^)\s]+)\)/g,(m,label,mag)=>{ let u=mag; try{ u=decodeURIComponent(mag); }catch(_){} return stash(`<button class="ai-magnet" data-magnet="${enc(u)}">🧲 ${enc(label)}</button>`); });
    src=src.replace(/magnet:\?[^\s)<]+/gi,u=>stash(`<button class="ai-magnet" data-magnet="${enc(u)}">🧲 Add torrent</button>`));
    let html=mdToHtml(src);
    return html.replace(/ S(\d+) /g,(m,i)=>slots[+i]||'');
  }
  // Render the rich command payloads the backend streams as a `response`.
  // When the effects studio is active (_ai.replyTo set), offer a button to post the generated media
  // back as a reply to the source post. Stashes the base64 so the reply can upload it to Blossom.
  // Inline command-output media (effects/compress/convert) lives at an authed /api/files/ artifact
  // URL (encrypted at rest) — NOT shareable. These fetch those bytes and RE-UPLOAD to PUBLIC Blossom
  // so the link works in a Nostr reply. Only for local (/) URLs; external media is already public.
  function _aiFileActions(u){
    if(!/^\//.test(u)) return '';
    const copy=`<button class="btn btn-ghost small ai-copyfile" data-url="${enc(u)}">📋 Copy link</button>`;
    const post=`<button class="btn btn-neon small ai-postfile" data-url="${enc(u)}">🚀 Post</button>`;
    const reply=_ai.replyTo?`<button class="btn btn-cyan small ai-replyfile" data-url="${enc(u)}">↩ Send the Reply</button>`:'';
    return `<div class="fx-reply-row" style="margin-top:6px;display:flex;gap:8px;flex-wrap:wrap">${reply}${post}${copy}</div>`;
  }
  async function _fileToPublicUrl(u){
    _ai.pubUrl=_ai.pubUrl||{};
    if(_ai.pubUrl[u]) return _ai.pubUrl[u];
    const blob=await fetch(u, { credentials:'include' }).then(r=>{ if(!r.ok) throw new Error('fetch '+r.status); return r.blob(); });
    const ext=((u.split(/[?#]/)[0].split('.').pop())||'bin').toLowerCase();
    const pub=await uploadBlob(new File([blob], 'media.'+ext, { type:blob.type||'application/octet-stream' }));
    _ai.pubUrl[u]=pub; return pub;
  }
  async function copyFileUrl(u, btn){
    if(btn){ btn.disabled=true; btn.textContent='uploading…'; }
    try{ const pub=await _fileToPublicUrl(u); try{ await navigator.clipboard.writeText(pub); toast('link copied'); }catch(_){ toast(pub); } if(btn){ btn.textContent='✓ copied'; btn.disabled=false; } }
    catch(e){ toast('failed: '+((e&&e.message)||e)); if(btn){ btn.disabled=false; btn.textContent='📋 Copy link'; } }
  }
  async function replyFileUrl(u, btn){
    const to=_ai.replyTo; if(!to){ toast('no post to reply to'); return; }
    if(btn){ btn.disabled=true; btn.textContent='posting…'; }
    try{ const pub=await _fileToPublicUrl(u); await publish(1, pub, eTags(to.id, to.pk)); toast('✓ reply posted'); if(btn){ btn.textContent='✓ replied'; } }
    catch(e){ toast('reply failed: '+((e&&e.message)||e)); if(btn){ btn.disabled=false; btn.textContent='↩ Send the Reply'; } }
  }
  // Share generated media as a NEW Nostr post: re-upload the (authed/local) artifact to public Blossom,
  // then open the composer pre-filled with the public link (add a caption, then post).
  async function postFileUrl(u, btn){
    if(btn){ btn.disabled=true; btn.textContent='uploading…'; }
    try{ const pub=await _fileToPublicUrl(u); compose({text: pub}); }
    catch(e){ toast('failed: '+((e&&e.message)||e)); }
    finally{ if(btn){ btn.disabled=false; btn.textContent='🚀 Post'; } }
  }
  async function postEffectMedia(mid, btn){
    const m=_ai.fxMedia[mid]; if(!m){ toast('nothing to post'); return; }
    if(btn){ btn.disabled=true; btn.textContent='uploading…'; }
    try{
      if(!m.url){ const bin=Uint8Array.from(atob(m.b64), c=>c.charCodeAt(0)); m.url=await uploadBlob(new File([bin], 'media.'+m.ext, { type:m.mime })); }
      compose({text: m.url});
    }catch(e){ toast('failed: '+((e&&e.message)||e)); }
    finally{ if(btn){ btn.disabled=false; btn.textContent='🚀 Post'; } }
  }
  function _fxReplyBtn(b64, mime, ext){
    if(!b64) return '';
    const mid='fx'+Date.now().toString(36)+Math.floor(Math.random()*1e4).toString(36);
    _ai.fxMedia[mid]={ b64, mime, ext };
    const copy=`<button class="btn btn-ghost small ai-copy-fx" data-mid="${mid}">📋 Copy link</button>`;
    const post=`<button class="btn btn-neon small ai-post-fx" data-mid="${mid}">🚀 Post</button>`;
    const reply=_ai.replyTo?`<button class="btn btn-cyan small ai-reply-fx" data-mid="${mid}">↩ Send the Reply</button>`:'';
    return `<div class="fx-reply-row" style="margin-top:6px;display:flex;gap:8px;flex-wrap:wrap">${reply}${post}${copy}</div>`;
  }
  // Upload generated media to Blossom and copy its URL — paste the link into any reply yourself.
  async function copyEffectUrl(mid, btn){
    const m=_ai.fxMedia[mid]; if(!m){ toast('nothing to copy'); return; }
    if(btn){ btn.disabled=true; btn.textContent='uploading…'; }
    try{
      if(!m.url){ const bin=Uint8Array.from(atob(m.b64), c=>c.charCodeAt(0)); m.url=await uploadBlob(new File([bin], 'effect.'+m.ext, { type:m.mime })); }
      try{ await navigator.clipboard.writeText(m.url); toast('link copied'); }catch(_){ toast(m.url); }
      if(btn){ btn.textContent='✓ copied'; btn.disabled=false; }
    }catch(e){ toast('upload failed: '+((e&&e.message)||e)); if(btn){ btn.disabled=false; btn.textContent='📋 Copy link'; } }
  }
  // --- Interactive multiple-choice flashcards (study quiz) ---------------------------------------
  // Self-contained port of the old web UI deck: state lives in _ai.decks[id]; taps re-render via the
  // #ai-msgs click delegation. No KaTeX here (math cards show raw $…$ — rare from a web page).
  function _fcRender(id){
    const st=_ai.decks&&_ai.decks[id]; if(!st) return '';
    const total=st.cards.length, card=st.cards[st.idx]||{}, picked=st.answered[st.idx];
    const letters=['A','B','C','D','E','F'];
    const opts=(card.options||[]).map((o,i)=>{
      let cls='fc-opt';
      if(picked!=null){ if(i===card.correct) cls+=' fc-correct'; else if(i===picked) cls+=' fc-wrong'; }
      return `<button type="button" class="${cls}" data-fc="${id}" data-opt="${i}"${picked!=null?' disabled':''}>${enc((letters[i]||'•')+'. '+o)}</button>`;
    }).join('');
    const explain=(picked!=null && card.explanation)?`<div class="fc-explain"><strong>Why:</strong> ${enc(card.explanation)}</div>`:'';
    const done=st.answered.filter(a=>a!=null).length;
    return `<div class="fc-head"><span class="fc-title">🎴 ${enc(st.title||'Flashcards')}</span><span class="fc-progress">${st.idx+1}/${total}</span></div>`
      +`<div class="fc-card"><div class="fc-q">${enc(card.question||'')}</div><div class="fc-options">${opts}</div>${explain}</div>`
      +`<div class="fc-controls"><button type="button" class="fc-prev" data-fc="${id}"${st.idx===0?' disabled':''}>◀ Prev</button>`
      +`<span class="fc-score">Score ${st.score}/${done}</span>`
      +`<button type="button" class="fc-restart" data-fc="${id}">↻ Restart</button>`
      +`<button type="button" class="fc-next" data-fc="${id}"${st.idx>=total-1?' disabled':''}>Next ▶</button></div>`;
  }
  function _fcRedraw(id){ const el=document.getElementById(id); if(el) el.innerHTML=_fcRender(id); aiScroll(); }
  function aiRenderResponse(d){
    const head = d.content ? aiFormat(d.content) : '';
    if(d.type==='flashcards' && Array.isArray(d.cards) && d.cards.length){
      _ai.decks=_ai.decks||{};
      const id='fc'+Date.now().toString(36)+Math.floor(Math.random()*1e4).toString(36);
      _ai.decks[id]={ cards:d.cards, idx:0, answered:new Array(d.cards.length).fill(null), score:0, title:d.title };
      return `<div class="flashcard-deck" id="${id}">${_fcRender(id)}</div>`;
    }
    if(d.type==='budget'){
      // Telegram-parity interactive budget: summary text + a Pay button per unpaid bill, Refresh, and
      // Add bill / income. Pay/Refresh reuse the .ai-cmd run-a-command path; Add prompts then sends.
      const bills=Array.isArray(d.bills)?d.bills:[];
      const pays=bills.map(b=>`<button class="ai-cmd" data-cmd="pay ${enc(b.name)}" title="Pay ${enc(b.name)}">✅ ${enc(b.name)} $${(Number(b.amount)||0).toFixed(0)}</button>`).join('');
      return head
        +`<div class="ai-budget-btns">${pays||'<span class="muted small">No unpaid bills 🎉</span>'}</div>`
        +`<div class="ai-budget-btns"><button class="ai-cmd" data-cmd="budget">🔄 Refresh</button>`
        +`<button class="ai-addbill">➕ Add bill</button>`
        +`<button class="ai-addbill" data-income="1">💵 Add income</button></div>`;
    }
    if(d.type==='generated_image' && d.image) return head+`<div class="ai-media"><img src="data:image/png;base64,${d.image}" alt="generated"></div>`+_fxReplyBtn(d.image,'image/png','png');
    if(d.type==='generated_video' && d.video) return head+`<div class="ai-media"><video controls src="data:video/mp4;base64,${d.video}"></video></div>`+_fxReplyBtn(d.video,'video/mp4','mp4');
    if(d.type==='generated_audio' && d.audio){ const fmt=(d.format||'mp3').toLowerCase(); const mime=({mp3:'audio/mpeg',wav:'audio/wav',flac:'audio/flac',opus:'audio/ogg',aac:'audio/aac'})[fmt]||'audio/mpeg';
      return head+`<div class="ai-media"><audio controls src="data:${mime};base64,${d.audio}"></audio></div>`; }
    if((d.type==='meme') && d.image) return head+`<div class="ai-media"><img src="data:image/png;base64,${d.image}" alt="meme"></div>`+_fxReplyBtn(d.image,'image/png','png');
    if(d.type==='mail_attachment' && d.data){ const mime=d.mime_type||'application/octet-stream';
      if(mime.startsWith('image/')) return head+`<div class="ai-media"><img src="data:${mime};base64,${d.data}"></div>`;
      return head+`<a class="ai-file" href="data:${mime};base64,${d.data}" download="${enc(d.filename||'attachment')}">📎 ${enc(d.filename||'attachment')}</a>`; }
    if(d.type==='images' && Array.isArray(d.images)){
      const items=d.images.slice(0,12).map(im=>{ const src=im.thumb_id?('/api/proxy-image/'+im.thumb_id):(im.img_src||im.thumbnail_src||im.thumbnail||''); const full=im.img_src||src; return src?`<img loading="lazy" src="${enc(src)}" data-full="${enc(full)}">`:''; }).join('');
      return head+`<div class="ai-imggrid">${items}</div>`;
    }
    if(d.type==='search' && Array.isArray(d.results)){
      return head+'<div class="ai-search">'+d.results.map(r=>`<div class="ai-sr"><a href="${enc(r.url||'')}" target="_blank" rel="noopener">${enc(r.title||r.url||'')}</a><div class="muted small">${enc((r.content||'').slice(0,200))}</div></div>`).join('')+'</div>';
    }
    if(d.type==='saved_searches' && Array.isArray(d.saved_searches)){
      // Telegram-style: each pin = its description, then a Run + Delete button.
      return head+'<div class="ai-pins">'+d.saved_searches.map(p=>{
        const run=p.run||('search '+(p.query||''));
        return `<div class="ai-pin"><div class="ai-pin-q">📌 ${enc(p.query||run)}</div>`
          +`<div class="ai-pin-btns"><button class="ai-cmd" data-cmd="${enc(run)}">▶ Run</button>`
          +`<button class="ai-cmd ai-cmd-danger" data-cmd="pin delete ${enc(String(p.id))}">🗑 Delete</button></div></div>`;
      }).join('')+'</div>';
    }
    if((d.type==='files'||d.type==='reminders') && Array.isArray(d.files||d.reminders)){
      const arr=d.files||d.reminders;
      return head+'<div class="ai-files">'+arr.map(f=>{ const u=f.url||f.path||''; const n=f.filename||f.name||f.query||f.text||u||'item'; return u?`<a class="ai-file" href="${enc(u)}" target="_blank" rel="noopener">📄 ${enc(n)}</a>`:`<span class="ai-file">${enc(n)}</span>`; }).join('')+'</div>';
    }
    return head || aiFormat(d.text||d.message||'');   // graceful fallback: never drop a payload
  }
  async function aiAddFiles(files){
    for(const f of files){
      const ext=(f.name.split('.').pop()||'').toLowerCase();
      const kind = /^image\//.test(f.type)?'image'
                 : (/^video\//.test(f.type) || /^(mp4|webm|mov|m4v|mkv|avi)$/.test(ext))?'video'   // was missing → mp4 fell through to 'doc' (got flashcards/read-text)
                 : (f.type==='application/pdf'||ext==='pdf')?'pdf'
                 : /^text\/|json|xml|csv|^$/.test(f.type)?'text' : 'doc';
      try{
        if(kind==='text'){ _ai.attach.push({kind, name:f.name, text:await f.text()}); }
        else {
          // Compress images before base64-encoding them into the chat payload — a 17MB photo (or six
          // at once) otherwise produced a multi-MB base64 blob that stalled the send. No-op for
          // video/gif/already-small files.
          const src = kind==='image' ? await compressImage(f) : f;
          const b64=await new Promise((res,rej)=>{ const r=new FileReader(); r.onload=()=>res(String(r.result).split(',')[1]||''); r.onerror=rej; r.readAsDataURL(src); });
          _ai.attach.push({kind, name:f.name, ext, b64});
        }
      }catch(_){}
    }
    aiRenderAttach();
  }
  // What you can DO with an attached file (old web UI / Telegram media-action keyboard). Each is
  // [label, mode, command]: mode 'fx' opens the Effects picker; 'run' sends the command immediately
  // (one-shot, e.g. compress/ocr); 'fill' prefills so you complete an argument (clip/convert/meme).
  // Commands match the upload allowlist in chat.py (note: it's `ocr`, not "readtext").
  function _aiAttachActions(){
    const k=new Set(_ai.attach.map(a=>a.kind));
    if(k.has('image')) return [['🎬 Effects','fx','__fxguide'],['🪄 Remove BG','run','removebackground'],['⭕ Circle crop','run','circlecrop'],['🔤 Read text','run','ocr'],['🗜 Compress','run','compress'],['🔄 Convert','fill','convert '],['😂 Meme','fill','meme ']];
    if(k.has('pdf')||k.has('doc')) return [['🎴 Flashcards','run','flashcards'],['🔤 Read text','run','ocr']];
    if(k.has('video')) return [['🗜 Compress','run','compress'],['✂️ Clip','fill','clip '],['🎵 Extract audio','run','extractaudio']];   // matches Telegram's video keyboard (Convert is image↔PDF only — useless for video)
    return [['🗜 Compress','run','compress'],['🔄 Convert','fill','convert ']];
  }
  function _aiMediaAction(mode, cmd){
    if(mode==='fx'){ showEffectGuide(); return; }
    const ta=$('#ai-input'); if(!ta) return;
    ta.value=cmd; ta.focus(); ta.dispatchEvent(new Event('input'));
    if(mode==='run') aiSend();   // one-shot — runs on the attached file now; 'fill' waits for the arg
  }
  // Old web-UI link actions: when the AI input holds a video URL (YouTube/TikTok/X/IG/Vimeo/Twitch/…)
  // and no file is attached, offer a Download (→ ytdl). Updates live as you type/paste.
  // Link-action bar — faithful port of the old web UI's updateLinkActionBar (Telegram parity): when
  // the input is a SINGLE bare URL/magnet, offer the right actions for that link type. Most run
  // immediately; ✂️ Clip prefills so you can edit the timecodes before sending.
  function aiUpdateLinkActions(){
    const bar=$('#ai-attachbar'); if(!bar || _ai.attach.length) return;
    const text=((($('#ai-input')||{}).value)||'').trim();
    const isMagnet=/^magnet:\?/i.test(text);
    const um=text.match(/^https?:\/\/\S+$/i);
    if(!isMagnet && !um){ if(bar.dataset.link){ bar.innerHTML=''; delete bar.dataset.link; } return; }
    const url=isMagnet?text:um[0];
    const isTorrent=isMagnet || /\.torrent(\?|$)/i.test(url);
    const isYT=/(?:youtube\.com\/|youtu\.be\/)/i.test(url);
    const isX=/\/\/[^/]*(?:x\.com|twitter\.com|nitter)/i.test(url);
    let acts;   // [label, command, prefillOnly]
    if(isTorrent) acts=[['🧲 Add Torrent','torrents add '+url,0]];
    else if(isYT) acts=[['📋 Summary','yt '+url,0],['🎵 MP3','ytdl '+url,0],['🎬 Movie','ytdl video '+url,0],['✂️ Clip','ytdl video '+url+' clip 0:00 0:30',1],['📣 Post','post '+url,0]];
    else if(isX) acts=[['🎵 MP3','ytdl '+url,0],['🎬 Video','ytdl video '+url,0],['✂️ Clip','ytdl video '+url+' clip 0:00 0:30',1],['📣 Post','post '+url,0]];
    else acts=[['📋 Summary','Summarize this page: '+url,0],['📸 Screenshot','screenshot '+url,0],['🌐 Translate','Translate this page to English: '+url,1],['🎴 Flashcards','flashcards '+url,0],['📣 Post','post '+url,0]];   // Translate prefills (edit the target language, then Enter)
    bar.dataset.link='1';
    bar.innerHTML='<div class="fx-row" style="display:flex;flex-wrap:wrap;gap:6px">'+acts.map((a,i)=>`<button class="fx-mot fx-linkact" data-i="${i}">${enc(a[0])}</button>`).join('')+'</div>';
    $$('.fx-linkact',bar).forEach(b=>{ const a=acts[+b.dataset.i]; b.onclick=()=>{ const t=$('#ai-input'); if(!t) return;
      t.value=a[1]; t.focus(); t.dispatchEvent(new Event('input'));
      if(!a[2]){ aiSend(); if(bar){ bar.innerHTML=''; delete bar.dataset.link; } }   // prefillOnly (✂️ Clip) → let the user edit timecodes, then Enter
    }; });
  }
  function aiRenderAttach(){
    const bar=$('#ai-attachbar'); if(!bar) return;
    // clear stale chips first — else removing the LAST attachment leaves its chip in the DOM
    // (aiUpdateLinkActions only wipes the bar when it had link-actions), so the ✕ looks dead.
    if(!_ai.attach.length){ bar.innerHTML=''; delete bar.dataset.link; aiUpdateLinkActions(); return; }
    const chips=_ai.attach.map((a,i)=>`<span class="ai-chip">${enc(a.name)} <button data-i="${i}" class="ai-chip-x">✕</button></span>`).join('');
    const acts=_aiAttachActions();
    const actions='<div class="fx-row" style="display:flex;flex-wrap:wrap;gap:6px;margin-top:8px">'+
      acts.map((a,i)=>`<button class="fx-mot fx-act" data-i="${i}">${enc(a[0])}</button>`).join('')+'</div>';
    bar.innerHTML='<div class="ai-chips" style="display:flex;flex-wrap:wrap;gap:6px">'+chips+'</div>'+actions;
    $$('.ai-chip-x',bar).forEach(b=> b.onclick=()=>{ _ai.attach.splice(+b.dataset.i,1); aiRenderAttach(); });
    $$('.fx-act',bar).forEach(b=>{ const a=acts[+b.dataset.i]; b.onclick=()=>_aiMediaAction(a[1], a[2]); });   // wire directly (attach bar is outside the #ai-msgs delegation)
  }
  function aiSend(){
    const ta=$('#ai-input'); if(!ta) return; const text=ta.value.trim();
    if(!text && !_ai.attach.length) return;
    const att=_ai.attach.slice(); _ai.attach=[]; aiRenderAttach();
    const labels=att.map(a=>`📎 ${enc(a.name)}`).join(' ');
    aiAddMessage('user', (text?enc(text):'') + (labels?`<div class="ai-userfiles">${labels}</div>`:''));
    const payload={ type:'message', content:text };
    const imgs=att.filter(a=>a.kind==='image').map(a=>({base64:a.b64, filename:a.name}));   if(imgs.length) payload.images=imgs;
    const pdfs=att.filter(a=>a.kind==='pdf').map(a=>({base64:a.b64, filename:a.name}));       if(pdfs.length) payload.pdfs=pdfs;
    const docs=att.filter(a=>a.kind==='doc').map(a=>({base64:a.b64, filename:a.name, type:a.ext})); if(docs.length) payload.documents=docs;
    const vids=att.filter(a=>a.kind==='video').map(a=>({base64:a.b64, filename:a.name}));      if(vids.length) payload.videos=vids;   // compress/clip/extractaudio operate on the video bytes
    const txts=att.filter(a=>a.kind==='text').map(a=>({content:a.text, filename:a.name}));     if(txts.length) payload.files=txts;
    const cid=_ai.convId;
    _ai.awaiting=true;   // a reply is now pending — if the WS drops before it lands, aiRecover() polls it back
    // Safety net for a socket that STALLS without ever firing onclose (a proxy silently stops forwarding,
    // or the result frame is dropped) — onclose-only recovery never fires there. If awaiting isn't
    // resolved within 30s, start the recovery poll anyway. Live stream chunks reset it (see aiHandle), so
    // a long but healthy streamed answer won't trip it; an effect (no streaming) just polls until it lands.
    clearTimeout(_ai.recoverWatch);
    _ai.recoverWatch=setTimeout(()=>{ if(_ai.awaiting && VIEW==='ai' && _ai.convId===cid) aiRecover(cid); }, 30000);
    aiWsSend(payload);   // sends now if open, else queues + (re)connects and flushes on open
    ta.value=''; ta.style.height='auto';
  }

  // ---------- settings view ----------
  // Local working copy of the relay list while editing (committed to ClientSettings on Save).
  let _setRelays = [];
  function renderSettings(){
    const feed=$('#feed');
    feed.innerHTML = `<div class="settings">
      <section class="set-card">
        <div class="set-head"><div><div class="set-title">Account</div>
          <div class="muted small">${enc(ME.npub.slice(0,20))}…</div></div></div>
        <div class="set-body">
          <div class="set-actions">
            <button class="btn btn-ghost small" id="set-copy-npub">🔑 Copy npub</button>
            ${ME.mode==='local'?`<button class="btn btn-ghost small" id="set-show-nsec" style="color:#ffcf2b">🔓 Show private key (nsec)</button>`:''}
            <button class="btn btn-ghost small" id="set-sync-posts">⤓ Sync my posts to this relay</button>
            <button class="btn btn-ghost small" id="set-logout">🚪 Logout</button>
            <button class="btn btn-ghost small" id="set-del-account" style="color:var(--danger)">🗑️ Delete my account</button>
          </div>
          <div class="muted small" id="set-sync-status">Pulls your posts from other relays into this one.</div>
        </div>
      </section>
      <section class="set-card">
        <div class="set-head"><div>
          <div class="set-title">Log in another device</div>
          <div class="muted small">${ME.mode==='local'
            ? 'On a computer, open this site → Sign in → “Open in Amber / scan QR”. Scan that QR here to log the computer in with your key — it never leaves this phone.'
            : 'Available when you sign in on this device with your key (nsec). Extension / remote-signer sessions can’t sign for another device.'}</div>
        </div></div>
        <div class="set-body">
          <button class="btn btn-neon small" id="set-scan-qr">📷 Scan QR code</button>
        </div>
      </section>
      <div id="user-settings"></div>
    </div>`;

    { const sq=$('#set-scan-qr'); if(sq) sq.onclick=()=>openQrScanner(); }
    { const ab=$('#set-admin'); if(ab) ab.onclick=()=>switchView('admin'); }
    { const da=$('#set-del-account'); if(da) da.onclick=async()=>{
        if(!confirm('Permanently delete your account and all your AI chats + files on this server? This cannot be undone.')) return;
        try{ const auth=await sign(27235,'delete-account',[['p',ME.pubkey]]);
          const r=await fetch('/client/delete-account',{method:'POST',headers:{'Content-Type':'application/json'},
            body:JSON.stringify({pubkey:ME.pubkey,auth:btoa(JSON.stringify(auth))})}).then(r=>r.json());
          if(r && r.ok){ toast('account deleted'); Session.clear(); try{Relay.worker.call('clearKey',{});}catch(_){} setTimeout(()=>location.reload(),800); }
          else toast('delete failed: '+((r&&r.error)||''));
        }catch(_){ toast('delete failed'); }
      }; }
    { const cn=$('#set-copy-npub'); if(cn) cn.onclick=async()=>{ try{ await navigator.clipboard.writeText(ME.npub); toast('npub copied'); }catch(_){ window.prompt('Your npub:', ME.npub); } }; }
    { const sn=$('#set-show-nsec'); if(sn) sn.onclick=async()=>{
        let r; try{ r=await Relay.worker.call('exportNsec', {}); }catch(_){ r=null; }
        const nsec=r&&r.nsec; if(!nsec){ toast('secret key not available on this login'); return; }
        modal(`<h3>🔓 Your private key (nsec)</h3>
          <p class="muted small" style="color:#ff9b6b">Anyone with this key has FULL control of your account. Never share it. Store it somewhere safe — it's the only way to recover your account.</p>
          <div class="keyrow"><code id="nsec-val">${enc(nsec)}</code></div>
          <div class="set-actions"><button class="btn btn-neon small" id="nsec-copy">📋 Copy nsec</button><button class="btn btn-ghost small" id="nsec-close">Close</button></div>`,
          root=>{
            $('#nsec-copy',root).onclick=async()=>{ try{ await navigator.clipboard.writeText(nsec); toast('nsec copied — keep it secret!'); }catch(_){ window.prompt('Your nsec (copy it):', nsec); } };
            $('#nsec-close',root).onclick=closeModal;
          });
      }; }
    { const lo=$('#set-logout'); if(lo) lo.onclick=()=>{ if(confirm('Log out of this device?')) logout(); }; }
    { const sp=$('#set-sync-posts'); if(sp) sp.onclick=async()=>{
        const st=$('#set-sync-status'); if(st) st.textContent='syncing… pulling your posts from other relays.';
        try{ const auth=await sign(27235,'sync-posts',[['p',ME.pubkey]]);
          const r=await fetch('/client/sync-posts',{method:'POST',headers:{'Content-Type':'application/json'},
            body:JSON.stringify({pubkey:ME.pubkey,auth:btoa(JSON.stringify(auth))})}).then(r=>r.json());
          if(st) st.textContent = r.ok ? '✓ Sync started — your posts will appear shortly.' : ('failed: '+(r.error||''));
        }catch(_){ if(st) st.textContent='sync failed'; }
      }; }
    renderUserSettings();   // tabbed User Settings — incl. the moved Relays / Media / Zaps / Muted tabs
  }
  // Retrieve relay list (NIP-65 10002) + Blossom servers (BUD-03 10063) from Nostr. Only fills the
  // UI when this device hasn't set them locally — so a fresh device inherits your synced choices.
  async function loadNostrPrefs(){
    try{
      const evs=await Relay.query([{ authors:[ME.pubkey], kinds:[10002], limit:2 }]);
      const r10002=evs.filter(e=>e.kind===10002).sort((a,b)=>b.created_at-a.created_at)[0];
      if(r10002 && !userRelays().length){
        const urls=r10002.tags.filter(t=>t[0]==='r'&&t[1]).map(t=>normalizeRelay(t[1])).filter(Boolean);
        if(urls.length){ _setRelays=urls; drawRelayRows(); }
      }
    }catch(_){}
    // Media server restore is the SINGLE source of truth (restoreMediaServer, also run at login); here we
    // just apply it (no-op if already set) then reflect the current choice into the settings inputs.
    await restoreMediaServer();
    const srv=ClientSettings.get('mediaServer','');
    if(srv){ const mi=$('#set-media'); if(mi && !mi.value) mi.value=srv;
      const on=$('#set-blossom-on'); if(on){ on.checked=true; const body=$('#set-blossom-body'); if(body) body.classList.remove('disabled'); } }
  }
  // Per-user settings — faithful port of the old web-UI modal (6 tabs). Loads /api/auth/settings,
  // saves text/toggles via PUT, and wires the real connect flows (Telegram link, Matrix login,
  // Pleroma OAuth, Misskey MiAuth, Nostr key) to their existing endpoints.
  let _usMail=[];
  let _nostrPrefsLoaded=false;   // load relay/media prefs from Nostr ONCE per session, not on every re-render
  async function renderUserSettings(){
    const host=$('#user-settings'); if(!host) return;
    // /api/auth/settings needs the nostr-login session cookie. Establish it FIRST — otherwise the
    // very first open 401s (cookie not set yet) and shows "Couldn't load", and you had to click
    // Settings a second time once the session warmed (the flicker/"do it twice" bug).
    await ensureAiSession();
    if(VIEW!=='settings') return;   // navigated away during the (first-time) sign/login
    // Load settings FIRST. If this fails we must NOT render an empty editable form — saving it would
    // wipe the user's real settings with blanks (that's how telegram_notifications got cleared).
    let s=null; try{ const r=await fetch('/api/auth/settings'); if(r.ok) s=await r.json(); }catch(_){}
    if(!host || VIEW!=='settings') return;
    if(!s || typeof s!=='object'){
      host.innerHTML='<section class="set-card"><div class="set-body"><div class="muted">Couldn’t load your settings.</div><button class="btn btn-ghost small" id="us-retry">Retry</button></div></section>';
      const rt=$('#us-retry'); if(rt) rt.onclick=renderUserSettings; return;
    }
    _usMail = Array.isArray(s.mail_accounts)? s.mail_accounts.slice() : [];
    // The select must reflect the theme CURRENTLY applied on this device (local choice wins over the
    // server value), and we must NOT re-apply on open (that would revert an unsaved live preview).
    let _curTheme; try{ _curTheme=localStorage.getItem('pc_theme'); }catch(_){}
    _curTheme=_curTheme||s.theme||siteDefaultTheme();
    if(!THEME_SLUGS.has(_curTheme)) _curTheme=siteDefaultTheme();   // stale/removed slug → don't desync the dropdown
    const tabs=[['profile','Profile'],['relays','Relays'],['media','Media'],['zaps','Zaps'],['muted','Muted'],['mail','Mail'],['telegram','Telegram'],['social','Social'],['finance','Finance'],['keys','API Keys']];
    const relaysOn=!!ClientSettings.get('relaysEnabled'), blossomOn=!!ClientSettings.get('blossomEnabled');
    // init the relay rows ONCE — renderUserSettings re-runs on connect/disconnect actions in other
    // tabs; re-seeding from saved values each time would wipe in-progress relay edits.
    if(!_nostrPrefsLoaded){ _setRelays=userRelays(); if(!_setRelays.length) _setRelays=['']; }
    host.innerHTML=`<section class="set-card us">
      <div class="set-head"><div class="set-title">User Settings</div></div>
      <div class="us-tabs">${tabs.map((t,i)=>`<button class="us-tab${i===0?' active':''}" data-tab="${t[0]}">${t[1]}</button>`).join('')}</div>
      <div class="set-body">
        <div class="us-pane active" data-pane="profile">
          <label class="fld">Theme <span class="muted small">(applies instantly; saved to your account)</span>
            <select class="input" id="us-theme">${THEMES.map(t=>`<option value="${t[0]}"${_curTheme===t[0]?' selected':''}>${t[1]}</option>`).join('')}</select>
          </label>
          <label class="fld">Notification email<input class="input" id="us-email" value="${enc(s.notification_email||'')}" placeholder="you@example.com"></label>
          <label class="fld">News sources <span class="muted small">(one per line: url|name) — used by the <code>news</code> command</span><textarea class="input" id="us-news-src" rows="4">${enc(s.news_sources||'')}</textarea></label>
        </div>
        <div class="us-pane" data-pane="mail">
          <div class="muted small">IMAP/SMTP accounts for the <code>mail</code> command. First account is the default sender.</div>
          <div id="us-mail-list"></div>
          <button class="btn btn-ghost small" id="us-mail-add">＋ Add email account</button>
        </div>
        <div class="us-pane" data-pane="telegram">
          <div class="${s.telegram_chat_id?'us-ok':'muted small'}" id="us-tg-status">${s.telegram_chat_id?('✓ Linked (chat '+enc(String(s.telegram_chat_id))+')'):'⚠ Not linked — generate a key below and send it to your bot.'}</div>
          <div class="set-actions">
            <button class="btn btn-ghost small" id="us-tg-key">Generate link key</button>
            ${s.telegram_chat_id?'<button class="btn btn-ghost small" id="us-tg-unlink" style="color:var(--danger)">Unlink Telegram</button>':''}
          </div>
          <div id="us-tg-keybox" class="muted small"></div>
          <label class="fld">Notify me about <span class="muted small">(comma list: news,downloads,mentions,inbox)</span><input class="input" id="us-tg-notif" value="${enc(s.telegram_notifications||'')}"></label>
          <label class="fld" style="flex-direction:row;justify-content:space-between;align-items:center">Relay notifications to Telegram<label class="switch"><input type="checkbox" id="us-social-notif" ${s.social_notif_enabled?'checked':''}><span class="slider"></span></label></label>
          <label class="fld">Nitter feeds <span class="muted small">(one RSS URL per line)</span><textarea class="input" id="us-nitter" rows="4">${enc(s.nitter_feeds||'')}</textarea></label>
        </div>
        <div class="us-pane" data-pane="social">
          <div class="us-conn"><div class="set-title small">Matrix</div>
            <label class="fld">Homeserver<input class="input" id="us-mx-hs" value="${enc(s.matrix_homeserver||'')}" placeholder="https://matrix.org"></label>
            ${s.matrix_has_access_token
              ? `<div class="muted small">✓ Connected as ${enc(s.matrix_user_id||'')}</div><button class="btn btn-ghost small" id="us-mx-disc" style="color:var(--danger)">Disconnect</button>`
              : `<label class="fld">Username<input class="input" id="us-mx-user" placeholder="@you:matrix.org"></label>
                 <label class="fld">Password<input class="input" id="us-mx-pass" type="password"></label>
                 <button class="btn btn-ghost small" id="us-mx-conn">Connect</button>`}
            <label class="fld">DM bot user id<input class="input" id="us-mx-bot" value="${enc(s.matrix_dm_bot_user_id||'')}" placeholder="@posterchan:server"></label>
            <label class="fld" style="flex-direction:row;justify-content:space-between;align-items:center">Relay notifications to Matrix DM<label class="switch"><input type="checkbox" id="us-mx-notif" ${s.matrix_notif_enabled?'checked':''}><span class="slider"></span></label></label>
            <div class="us-stat muted small" id="us-mx-stat"></div>
          </div>
          <div class="us-conn"><div class="set-title small">Pleroma / Mastodon</div>
            <label class="fld">Instance URL<input class="input" id="us-plr-url" value="${enc(s.pleroma_instance_url||'')}" placeholder="https://pleroma.example"></label>
            ${s.pleroma_has_access_token
              ? `<div class="muted small">✓ Connected to ${enc(s.pleroma_instance_url||'')}</div><button class="btn btn-ghost small" id="us-plr-disc" style="color:var(--danger)">Disconnect</button>`
              : `<button class="btn btn-ghost small" id="us-plr-conn">Connect with OAuth</button>`}
            <label class="fld" style="flex-direction:row;justify-content:space-between;align-items:center">Bridge my fedi DMs &amp; notifications to Nostr<label class="switch"><input type="checkbox" id="us-fedi-bridge" ${s.fedi_bridge_enabled?'checked':''}><span class="slider"></span></label></label>
            <div class="muted small">Your fediverse DMs arrive as Nostr DMs and your notifications as Nostr events; replying/liking/reposting a bridged post posts back through this account. Needs a NIP-05 name on this instance.</div>
            <label class="fld" style="flex-direction:row;justify-content:space-between;align-items:center">Cross-post my posts to the Fediverse<label class="switch"><input type="checkbox" id="us-fedi-crosspost" ${s.fedi_crosspost_enabled?'checked':''}><span class="slider"></span></label></label>
            <div class="muted small">When on, your top-level Nostr notes are also posted to your linked Pleroma account as public posts. Replies stay where you make them.</div>
            <div class="us-stat muted small" id="us-plr-stat"></div>
          </div>
          <div class="us-conn"><div class="set-title small">Misskey</div>
            <label class="fld">Instance URL<input class="input" id="us-mk-url" value="${enc(s.misskey_instance_url||'')}" placeholder="https://misskey.example"></label>
            ${s.misskey_has_api_token
              ? `<div class="muted small">✓ Connected to ${enc(s.misskey_instance_url||'')}</div><button class="btn btn-ghost small" id="us-mk-disc" style="color:var(--danger)">Disconnect</button>`
              : `<button class="btn btn-ghost small" id="us-mk-conn">Connect with MiAuth</button>`}
            <div class="us-stat muted small" id="us-mk-stat"></div>
          </div>
        </div>
        <div class="us-pane" data-pane="finance">
          <div class="muted small">Budget Manager API key — drives <code>budget</code>, <code>bills</code>, <code>pay</code>, <code>addbill</code>.</div>
          <div class="${s.finance_has_api_key?'us-ok':'muted'}">${s.finance_has_api_key?'✓ Connected — an API key is set.':'⚠ Not connected — paste your Budget Manager API key below.'}</div>
          <label class="fld">API key${s.finance_has_api_key?' <span class="muted small">(leave blank to keep the current one)</span>':''}<input class="input" id="us-fin" type="password" placeholder="${s.finance_has_api_key?'•••••••• (set)':'X-API-Key'}"></label>
          ${s.finance_has_api_key?'<button class="btn btn-ghost small" id="us-fin-clear" style="color:var(--danger)">Remove key</button>':''}
        </div>
        <div class="us-pane" data-pane="keys">
          <div class="muted small">API keys let external apps use the AI API as you.</div>
          <div class="set-actions"><input class="input" id="us-key-name" placeholder="Key name (optional)"><button class="btn btn-ghost small" id="us-key-new">Generate new key</button></div>
          <div id="us-key-list"></div>
        </div>
        <div class="us-pane" data-pane="relays">
          <label class="fld" style="flex-direction:row;justify-content:space-between;align-items:center">Use my own relays<label class="switch"><input type="checkbox" id="set-relays-on" ${relaysOn?'checked':''}><span class="slider"></span></label></label>
          <div class="muted small">By default this app uses the built-in relay. Turn this on to connect to your own relays instead — events from them are signature-verified.</div>
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
          <div class="set-actions"><button class="btn btn-neon small" id="set-relays-save">Save &amp; reload</button></div>
        </div>
        <div class="us-pane" data-pane="media">
          <label class="fld" style="flex-direction:row;justify-content:space-between;align-items:center">Use my own Blossom server<label class="switch"><input type="checkbox" id="set-blossom-on" ${blossomOn?'checked':''}><span class="slider"></span></label></label>
          <div class="muted small">Where your uploaded images &amp; files are stored. Turn this on to use your own Blossom server instead of the built-in one.</div>
          <div class="set-body ${blossomOn?'':'disabled'}" id="set-blossom-body">
            <input class="input" id="set-media" placeholder="https://your-blossom-server.com" value="${enc(ClientSettings.get('mediaServer',''))}">
            <div class="media-presets"><span class="muted small">Quick pick:</span>
              <button type="button" class="btn btn-ghost small mp-preset" data-url="https://nostr.build">nostr.build</button>
              <button type="button" class="btn btn-ghost small mp-preset" data-url="https://blossom.primal.net">Primal</button>
              <button type="button" class="btn btn-ghost small mp-preset" data-url="https://blossom.band">blossom.band</button>
            </div>
            <div class="muted small">Must be an <code>https://</code> server that allows cross-origin (CORS) uploads. Default built-in: <code>${enc(CFG.blossom_url||'none')}</code></div>
          </div>
          <div class="set-actions"><button class="btn btn-neon small" id="set-media-save">Save &amp; reload</button></div>
        </div>
        <div class="us-pane" data-pane="zaps">
          <div class="muted small">Got the <b>Alby</b> (or any WebLN) browser extension? Zaps already use it — just tap ⚡. Otherwise connect a wallet with a <b>Nostr Wallet Connect</b> string (NIP-47) — handy on mobile or with Alby Hub / Coinos / Primal. Stored only in this browser.</div>
          <div class="set-actions"><button class="btn btn-cyan small" id="set-webln">⚡ Connect Alby / WebLN extension</button></div>
          <input class="input" id="set-nwc" type="password" placeholder="nostr+walletconnect://… (for wallets without an extension)" value="${enc(ClientSettings.get('nwc',''))}">
          <div class="set-actions"><button class="btn btn-neon small" id="set-nwc-save">Save wallet</button>
            <button class="btn btn-cyan small" id="set-nwc-clear">Disconnect</button></div>
          <div class="muted small" id="set-nwc-status">${Nwc.configured()?'✓ NWC wallet connected — zaps pay instantly':''}</div>
        </div>
        <div class="us-pane" data-pane="muted">
          <label class="fld" style="flex-direction:row;justify-content:space-between;align-items:center">Blur sensitive / NSFW posts<label class="switch"><input type="checkbox" id="set-blur-nsfw" ${BLUR_NSFW?'checked':''}><span class="slider"></span></label></label>
          <div class="muted small">Posts flagged sensitive (NIP-36 content warning) are blurred behind a “Show” reveal. Turn this off to see them unblurred. Saved on this device.</div>
          <label class="fld" style="flex-direction:row;justify-content:space-between;align-items:center">Hide DM previews until opened<label class="switch"><input type="checkbox" id="set-hide-dm-prev" ${ClientSettings.get('hideDmPreview', false)?'checked':''}><span class="slider"></span></label></label>
          <div class="muted small">Don’t show the last message text in the Messages list — only reveal it when you open the conversation. Saved on this device.</div>
          <div class="muted small">Hide posts containing any of these words or phrases (case-insensitive, one per line). Saved to your Nostr mute list (NIP-51), so it follows you to other clients.</div>
          <textarea class="input" id="set-muted-words" rows="4" placeholder="one word or phrase per line">${enc([...MUTED_WORDS].join('\n'))}</textarea>
          <div class="set-actions"><button class="btn btn-neon small" id="set-words-save">Save muted words</button></div>
          <div class="muted small" id="set-words-status"></div>
        </div>
      </div>
      <button class="btn btn-neon" id="us-save">Save settings</button>
      <div class="muted small set-foot" id="us-save-status"></div>
    </section>`;
    // tab switching
    $$('.us-tab',host).forEach(b=> b.onclick=()=>{
      $$('.us-tab',host).forEach(x=>x.classList.toggle('active',x===b));
      $$('.us-pane',host).forEach(p=>p.classList.toggle('active', p.dataset.pane===b.dataset.tab));
      if(b.dataset.tab==='keys') usLoadKeys();
    });
    usRenderMail();

    // ---- moved into tabs: Relays / Media / Zaps / Muted (client-side, own save semantics) ----
    drawRelayRows();
    const syncRelays=()=>{ _setRelays=$$('#set-relay-list .relay-row input').map(i=>i.value.trim()); };
    { const t=$('#set-relays-on'); if(t) t.onchange=e=>$('#set-relays-body').classList.toggle('disabled', !e.target.checked); }
    { const t=$('#set-blossom-on'); if(t) t.onchange=e=>$('#set-blossom-body').classList.toggle('disabled', !e.target.checked); }
    // Media-server quick-pick presets (nostr.build / Primal / blossom.band): fill the field + turn the
    // override on so the user can Save. They still need to tap Save & reload to apply.
    $$('.mp-preset').forEach(b=> b.onclick=()=>{ const mi=$('#set-media'); if(mi) mi.value=b.dataset.url;
      const on=$('#set-blossom-on'); if(on){ on.checked=true; } $('#set-blossom-body').classList.remove('disabled'); });
    { const b=$('#set-relay-add'); if(b) b.onclick=()=>{ syncRelays(); _setRelays.push(''); drawRelayRows(); }; }
    { const b=$('#set-relay-ext'); if(b) b.onclick=async()=>{ syncRelays(); await importExtensionRelays(); }; }
    { const b=$('#set-relay-nip05'); if(b) b.onclick=async()=>{ syncRelays(); await importNip05Relays($('#set-nip05').value.trim()); }; }
    { const b=$('#set-relays-save'); if(b) b.onclick=async()=>{
        syncRelays();
        const urls=[...new Set(_setRelays.map(u=>normalizeRelay(u)).filter(Boolean))];
        ClientSettings.set('relaysEnabled', $('#set-relays-on').checked);
        ClientSettings.set('relays', urls);
        try{ if(urls.length) await publish(10002,'',urls.map(u=>['r',u])); }catch(_){}
        toast('relays saved — reloading'); setTimeout(()=>location.reload(),600);
      }; }
    { const b=$('#set-media-save'); if(b) b.onclick=async()=>{
        const media=$('#set-media').value.trim();
        const on=$('#set-blossom-on').checked;
        if(on && media){
          const proto=await detectProto(media);   // capability probe (NIP-96 well-known) — works for any host
          ClientSettings.set('blossomEnabled', true);
          ClientSettings.set('mediaServer', media);
          ClientSettings.set('mediaProto', proto);
          // Persist to Nostr so a fresh device restores it: kind-10063 (BUD-03 Blossom) or kind-10096
          // (NIP-96 file-storage server list). Newest of the two wins on restore.
          try{ await publish(proto==='nip96'?10096:10063,'',[['server',media]]); }catch(_){}
        } else {
          // Reverting to the built-in server: clear locally AND publish EMPTY replaceable lists to both
          // kinds, so no other device re-restores a now-abandoned server (the stale-restore bug).
          ClientSettings.set('blossomEnabled', false);
          ClientSettings.set('mediaServer', '');
          ClientSettings.set('mediaProto', '');
          try{ await publish(10063,'',[]); }catch(_){}
          try{ await publish(10096,'',[]); }catch(_){}
        }
        toast('media server saved — reloading'); setTimeout(()=>location.reload(),600);
      }; }
    { const we=$('#set-webln'); if(we) we.onclick=async()=>{ const st=$('#set-nwc-status');
        if(!window.webln){ if(st) st.textContent='No WebLN extension found — install Alby, or paste an NWC string below.'; return; }
        try{ await window.webln.enable(); if(st) st.textContent='✓ Extension connected — tap ⚡ on any post to zap'; toast('⚡ wallet extension connected'); }
        catch(e){ if(st) st.textContent='Extension declined: '+((e&&e.message)||e); } }; }
    { const nb=$('#set-nwc-save'); if(nb) nb.onclick=()=>{ const st=$('#set-nwc-status'); const u=($('#set-nwc').value||'').trim();
        if(u && !Nwc.parse(u)){ if(st) st.textContent='Not a valid nostr+walletconnect:// string'; return; }
        ClientSettings.set('nwc', u); if(st) st.textContent=u?'✓ Wallet connected — zaps pay instantly':'cleared'; toast(u?'wallet saved':'wallet cleared'); }; }
    { const nc=$('#set-nwc-clear'); if(nc) nc.onclick=()=>{ ClientSettings.set('nwc',''); const i=$('#set-nwc'); if(i) i.value=''; const st=$('#set-nwc-status'); if(st) st.textContent='Disconnected'; toast('wallet disconnected'); }; }
    // Blur-NSFW toggle: persist immediately (per-device) and re-render the open feed so it applies live.
    { const bn=$('#set-blur-nsfw'); if(bn) bn.onchange=()=>{
        BLUR_NSFW = bn.checked; ClientSettings.set('blurNsfw', BLUR_NSFW);
        toast(BLUR_NSFW?'sensitive posts blurred':'sensitive posts shown');
        if(['home','global','notifications','messages','bookmarks'].includes(VIEW)){ try{ renderView(true); }catch(_){} }
      }; }
    // Hide-DM-preview toggle: persist per-device and re-render Messages so it applies immediately.
    { const hd=$('#set-hide-dm-prev'); if(hd) hd.onchange=()=>{
        ClientSettings.set('hideDmPreview', hd.checked);
        toast(hd.checked?'DM previews hidden':'DM previews shown');
        if(VIEW==='messages'){ try{ renderMessages(); }catch(_){} }
      }; }
    { const wb=$('#set-words-save'); if(wb) wb.onclick=async()=>{
        const words=($('#set-muted-words').value||'').split('\n').map(w=>w.trim()).filter(Boolean);
        wb.disabled=true; const st=$('#set-words-status'); if(st) st.textContent='saving…';
        try{ await saveMutedWords(words); if(st) st.textContent='Saved — '+MUTED_WORDS.size+' muted word(s). New posts are filtered immediately.'; }
        catch(e){ if(st) st.textContent='Save failed: '+((e&&e.message)||e); }
        finally{ wb.disabled=false; }
      }; }
    // Fill relays/media from Nostr (10002/10063) ONCE — renderUserSettings re-runs on many settings
    // sub-actions, and re-querying would clobber in-progress relay edits each time.
    if(!_nostrPrefsLoaded){ _nostrPrefsLoaded=true; loadNostrPrefs(); }
    usLoadKeys();   // populate API Keys immediately (not only on tab click)
    $('#us-mail-add').onclick=()=>{ _usMail.push({email:'',imap_server:'',imap_port:993,smtp_server:'',smtp_port:587,password:''}); usRenderMail(); };
    // Telegram link key
    { const k=$('#us-tg-key'); if(k) k.onclick=async()=>{ const box=$('#us-tg-keybox'); box.textContent='generating…';
        try{ const d=await fetch('/api/telegram/generate-key',{method:'POST'}).then(r=>r.json());
          if(!d.key){ box.textContent='failed: '+enc(d.detail||''); return; }
          // One-tap deep link (opens the bot with the key pre-filled → just tap Start). Fall back to
          // the manual command if the bot username is unknown.
          box.innerHTML = (d.deep_link
            ? `<a class="btn btn-cyan small" href="${enc(d.deep_link)}" target="_blank" rel="noopener">📲 Link Telegram${d.bot_username?(' (@'+enc(d.bot_username)+')'):''}</a><div class="muted small" style="margin-top:6px">or send <code>/start ${enc(d.key)}</code> to the bot manually</div>`
            : `Send this to the bot in Telegram: <code>/start ${enc(d.key)}</code>`);
        }catch(_){ box.textContent='failed'; } }; }
    { const u=$('#us-tg-unlink'); if(u) u.onclick=async()=>{ if(!confirm('Unlink Telegram?'))return;
        await fetch('/api/telegram/unlink',{method:'POST'}); toast('unlinked'); renderUserSettings(); }; }
    // Matrix
    { const c=$('#us-mx-conn'); if(c) c.onclick=async()=>{ const st=$('#us-mx-stat'); st.textContent='connecting…';
        const r=await fetch('/api/matrix/connect',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({homeserver:$('#us-mx-hs').value.trim(),username:$('#us-mx-user').value.trim(),password:$('#us-mx-pass').value})});
        const d=await r.json().catch(()=>({})); if(r.ok){ toast('Matrix connected'); renderUserSettings(); } else st.textContent=d.detail||'connect failed'; }; }
    { const d=$('#us-mx-disc'); if(d) d.onclick=async()=>{ if(!confirm('Disconnect Matrix?'))return; await fetch('/api/matrix/disconnect',{method:'POST'}); renderUserSettings(); }; }
    // Pleroma OAuth (opens instance; callback posts 'pleroma_connected')
    { const c=$('#us-plr-conn'); if(c) c.onclick=async()=>{ const st=$('#us-plr-stat'); const url=$('#us-plr-url').value.trim(); if(!url){st.textContent='enter the instance URL';return;} st.textContent='registering app…';
        const r=await fetch('/api/pleroma/oauth/start',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({instance_url:url})}); const d=await r.json().catch(()=>({}));
        if(!r.ok){ st.textContent=d.detail||'failed'; return; } window.open(d.auth_url,'_blank'); st.textContent='waiting for authorization…';
        const h=e=>{ if(e.data==='pleroma_connected'){ window.removeEventListener('message',h); renderUserSettings(); } }; window.addEventListener('message',h); }; }
    { const d=$('#us-plr-disc'); if(d) d.onclick=async()=>{ if(!confirm('Disconnect Pleroma?'))return; await fetch('/api/pleroma/disconnect',{method:'POST'}); renderUserSettings(); }; }
    // Misskey MiAuth
    { const c=$('#us-mk-conn'); if(c) c.onclick=async()=>{ const st=$('#us-mk-stat'); const url=$('#us-mk-url').value.trim(); if(!url){st.textContent='enter the instance URL';return;} st.textContent='starting MiAuth…';
        const r=await fetch('/api/misskey/miauth/start',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({instance_url:url})}); const d=await r.json().catch(()=>({}));
        if(!r.ok){ st.textContent=d.detail||'failed'; return; } window.open(d.auth_url,'_blank'); st.textContent='waiting for authorization…';
        const h=e=>{ if(e.data==='misskey_connected'){ window.removeEventListener('message',h); renderUserSettings(); } }; window.addEventListener('message',h); }; }
    { const d=$('#us-mk-disc'); if(d) d.onclick=async()=>{ if(!confirm('Disconnect Misskey?'))return; await fetch('/api/misskey/disconnect',{method:'DELETE'}); renderUserSettings(); }; }
    // Finance: remove the stored key
    { const fc=$('#us-fin-clear'); if(fc) fc.onclick=async()=>{ if(!confirm('Remove your Budget Manager API key?'))return;
        await fetch('/api/auth/settings',{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({finance_api_key:''})});
        toast('finance key removed'); renderUserSettings(); }; }
    // API keys
    $('#us-key-new').onclick=async()=>{ const name=$('#us-key-name').value.trim();
      const d=await fetch('/api/auth/api-keys',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name})}).then(r=>r.json()).catch(()=>({}));
      if(d && d.key){ alert('Your new key (shown once):\n\n'+d.key); $('#us-key-name').value=''; usLoadKeys(); } else toast('create failed'); };
    // Save (text + toggles; connect flows persist themselves)
    $('#us-save').onclick=async()=>{
      const body={ notification_email:$('#us-email').value.trim(), news_sources:$('#us-news-src').value,
        telegram_notifications:$('#us-tg-notif').value.trim(), social_notif_enabled:$('#us-social-notif').checked,
        matrix_notif_enabled:$('#us-mx-notif').checked, matrix_homeserver:$('#us-mx-hs').value.trim(),
        fedi_bridge_enabled:(($('#us-fedi-bridge')||{}).checked)||false,
        fedi_crosspost_enabled:(($('#us-fedi-crosspost')||{}).checked)||false,
        matrix_dm_bot_user_id:$('#us-mx-bot').value.trim(), pleroma_instance_url:$('#us-plr-url').value.trim(),
        misskey_instance_url:$('#us-mk-url').value.trim(), nitter_feeds:$('#us-nitter').value,
        theme:($('#us-theme')&&$('#us-theme').value)||'professional',
        mail_accounts:usCollectMail() };
      const fin=$('#us-fin').value.trim(); if(fin) body.finance_api_key=fin;
      const st=$('#us-save-status'); if(st) st.textContent='saving…';
      // Persist the client-side tabs too, so the single Save button saves EVERYTHING (not just the
      // server account settings — the "relay/media edits silently dropped" bug). Reload only when the
      // relay/media config actually changed (those reconnect the app).
      let needReload=false;
      if($('#set-relays-on')){
        syncRelays();
        const urls=[...new Set(_setRelays.map(u=>normalizeRelay(u)).filter(Boolean))];
        const on=$('#set-relays-on').checked;
        if(on!==!!ClientSettings.get('relaysEnabled') || JSON.stringify(urls)!==JSON.stringify(userRelays())) needReload=true;
        ClientSettings.set('relaysEnabled', on); ClientSettings.set('relays', urls);
        // only publish the NIP-65 list when the user actually enabled their own relays — don't mutate
        // their relay list (which follows them to other clients) just because URLs are prefilled.
        try{ if(on && urls.length) await publish(10002,'',urls.map(u=>['r',u])); }catch(_){}
      }
      if($('#set-media')){
        const media=$('#set-media').value.trim(), on=$('#set-blossom-on').checked;
        if(on!==!!ClientSettings.get('blossomEnabled') || media!==ClientSettings.get('mediaServer','')) needReload=true;
        ClientSettings.set('blossomEnabled', on); ClientSettings.set('mediaServer', media);
        try{ if(on && media) await publish(10063,'',[['server',media]]); }catch(_){}
      }
      if($('#set-nwc')){ const u=($('#set-nwc').value||'').trim(); if(!u || Nwc.parse(u)) ClientSettings.set('nwc', u); }
      try{ const r=await fetch('/api/auth/settings',{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
        if(r.ok){ applyTheme(body.theme); toast('settings saved');
          if(st) st.textContent=needReload?'✓ Saved — reloading':'✓ Saved';
          if(needReload) setTimeout(()=>location.reload(),600);
        } else if(st) st.textContent='save failed ('+r.status+')';
      }catch(_){ if(st) st.textContent='save failed'; }
    };
    // Live theme preview: apply on change without waiting for Save (revert is a page reload / re-save).
    { const ts=$('#us-theme'); if(ts) ts.onchange=()=>applyTheme(ts.value, false); }   // PREVIEW only (no persist); Save writes it
  }
  function usRenderMail(){
    const wrap=$('#us-mail-list'); if(!wrap) return;
    wrap.innerHTML=_usMail.map((a,i)=>`<div class="us-mail" data-i="${i}">
      <input class="input" data-f="email" placeholder="email" value="${enc(a.email||'')}">
      <input class="input" data-f="imap_server" placeholder="IMAP server" value="${enc(a.imap_server||'')}">
      <input class="input" data-f="imap_port" placeholder="993" value="${enc(String(a.imap_port||993))}">
      <input class="input" data-f="smtp_server" placeholder="SMTP server" value="${enc(a.smtp_server||'')}">
      <input class="input" data-f="smtp_port" placeholder="587" value="${enc(String(a.smtp_port||587))}">
      <input class="input" data-f="password" type="password" placeholder="${a.password?'•••• (kept — blank keeps it)':'password'}">
      <button class="mini us-mail-del" data-i="${i}" title="remove">✕</button></div>`).join('');
    $$('.us-mail-del',wrap).forEach(b=> b.onclick=()=>{ _usMail.splice(+b.dataset.i,1); usRenderMail(); });
  }
  function usCollectMail(){
    const wrap=$('#us-mail-list'); if(!wrap) return _usMail;
    return $$('.us-mail',wrap).map(row=>{ const o={}; $$('input',row).forEach(inp=>{ const f=inp.dataset.f; let v=inp.value; if(f==='imap_port'||f==='smtp_port') v=parseInt(v,10)||(f==='imap_port'?993:587); if(f==='password'&&!v) return; o[f]=v; }); return o; }).filter(o=>o.email);
  }
  async function usLoadKeys(){
    const wrap=$('#us-key-list'); if(!wrap) return;
    let keys=[]; try{ keys=await fetch('/api/auth/api-keys').then(r=>r.json()); }catch(_){}
    wrap.innerHTML=(keys||[]).map(k=>`<div class="us-key"><div><b>${enc(k.name||'Default')}</b> <span class="muted small">${k.is_active?'active':'disabled'}</span></div>
      <div><button class="mini" data-tog="${k.id}">${k.is_active?'Disable':'Enable'}</button><button class="mini" data-del="${k.id}" style="color:var(--danger)">Delete</button></div></div>`).join('')||'<div class="muted small">No keys yet.</div>';
    $$('[data-tog]',wrap).forEach(b=> b.onclick=async()=>{ await fetch('/api/auth/api-keys/'+b.dataset.tog+'/toggle',{method:'PUT'}); usLoadKeys(); });
    $$('[data-del]',wrap).forEach(b=> b.onclick=async()=>{ if(!confirm('Delete this API key?'))return; await fetch('/api/auth/api-keys/'+b.dataset.del,{method:'DELETE'}); usLoadKeys(); });
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
  // Thread context fallback: our relay is WoT-gated, so a post we replied to whose author isn't in
  // the trust set isn't stored here. Fetch such a missing event from public relays (UNTRUSTED → the
  // signer worker verifies the signature before we display it), so threads aren't truncated.
  const PUBLIC_FALLBACK_RELAYS = ['wss://relay.damus.io','wss://nos.lol','wss://relay.primal.net','wss://relay.nostr.band'];
  function fetchFromPublicRelays(filters, timeout=4500){
    return new Promise(resolve=>{
      const got=new Map(); let pending=PUBLIC_FALLBACK_RELAYS.length, done=false;
      const finish=()=>{ if(done)return; done=true; clearTimeout(to); resolve([...got.values()]); };
      const to=setTimeout(finish, timeout);
      PUBLIC_FALLBACK_RELAYS.forEach(url=>{
        let ws, counted=false; const done1=()=>{ if(counted)return; counted=true; if(--pending<=0)finish(); };
        try{ ws=new WebSocket(url); }catch(_){ done1(); return; }
        const sid='pf'+Math.random().toString(36).slice(2,8);
        ws.onopen=()=>{ try{ ws.send(JSON.stringify(['REQ',sid,...filters])); }catch(_){ try{ws.close();}catch(__){} } };
        ws.onmessage=e=>{ try{ const m=JSON.parse(e.data); if(m[0]==='EVENT'&&m[2]&&m[2].id) got.set(m[2].id,m[2]); else if(m[0]==='EOSE'){ try{ws.close();}catch(_){}} }catch(_){} };
        ws.onerror=()=>{ try{ws.close();}catch(_){} };
        ws.onclose=done1;
      });
    });
  }
  async function fetchEvent(id){
    let r=await Relay.query([{ ids:[id] }]); if(r[0]) return r[0];
    const pub=await fetchFromPublicRelays([{ ids:[id] }]);
    for(const ev of pub){ try{ const v=await Relay.worker.call('verify',{event:ev}); if(v&&v.valid) return ev; }catch(_){} }
    return null;
  }
  function openThread(id){
    try{ _navUrl('/'+NT().nip19.neventEncode({ id })); }catch(_){ try{ _navUrl('/'+NT().nip19.noteEncode(id)); }catch(__){} }
    renderThread(id);
  }
  async function renderThread(id){
    VIEW='thread'; _hidePill(); $$('.nav-item[data-view]').forEach(b=>b.classList.remove('active')); $('#view-title').textContent='Thread';
    const feed=$('#feed'); feed.innerHTML='<div class="spinner"></div>';
    let ev=Store.get(id);
    if(!ev){ ev=await fetchEvent(id); if(ev) Store.saveEvent(ev); }
    if(!ev){ feed.innerHTML='<div class="empty">Post not found on the relay.</div>'; return; }
    // A chat message (NIP-28 kind-42) has no normal thread — e.g. a reaction notification links here.
    // Resolve its channel and open the room, then scroll to + flash the message.
    if(ev.kind===42){
      // NIP-28: the channel is the `root`-marked e-tag; with no markers the convention is the FIRST e-tag.
      const root=(ev.tags.find(t=>t[0]==='e' && t[3]==='root') || ev.tags.find(t=>t[0]==='e') || [])[1];
      let chan=root ? (Store.get(root)||await fetchEvent(root)) : null;
      if(chan){ Store.saveEvent(chan); openChannel(chan, id); return; }
    }
    // fetch the parent (for reply context) and the replies
    let parent=null;
    const es=ev.tags.filter(t=>t[0]==='e');
    const parentId=((es.find(t=>t[3]==='reply')||es.find(t=>t[3]==='root')||es[es.length-1])||[])[1];
    if(parentId && parentId!==id){ parent=Store.get(parentId); if(!parent){ parent=await fetchEvent(parentId); if(parent) Store.saveEvent(parent); } }
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
    // 1b. note/nevent (optionally nostr:-prefixed) -> open that note's thread
    if(/^(?:nostr:)?(?:note1|nevent1)[0-9a-z]{20,}$/i.test(q)){
      try{ const d=NT().nip19.decode(q.replace(/^nostr:/i,'')); const id=d.type==='note'?d.data:(d.data&&d.data.id); if(id) return renderThread(id); }catch(_){}
    }
    // 2. NIP-05 address (name@domain) -> resolve to a pubkey -> jump
    if(/^[\w.\-+]+@[\w.\-]+\.[a-z]{2,}$/i.test(q)){
      const rp=await nip05Resolve(q.toLowerCase());
      if(rp){ return renderProfileView(rp); }
    }
    // 3. posts via NIP-50 full-text (relay indexes note content); profiles by name/nip05 over the
    //    locally-cached profile set (the relay's FTS doesn't cover kind-0, so we match what we know).
    // Posts via NIP-50 FTS, and the Discover kinds (articles/streams/communities) fetched + filtered
    // client-side (FTS doesn't index them) — run in parallel.
    const ql=q.toLowerCase();
    const [postEvs, addrEvs] = await Promise.all([
      Relay.query([{ kinds:[1], search:q, limit:40 }]).catch(()=>[]),
      Relay.query([{ kinds:[30023,30311,34550,2003,30617], limit:240 }]).catch(()=>[]),
    ]);
    postEvs.forEach(e=>{ Store.saveEvent(e); needProfile(e.pubkey); });
    addrEvs.forEach(e=>{ Store.saveEvent(e); needProfile(e.pubkey); });
    if(VIEW!=='search') return;
    const arts =_dedupAddr(addrEvs.filter(e=>e.kind===30023 && _matchAddr(e,ql))).sort((a,b)=>artTime(b)-artTime(a)).slice(0,12);
    const strms=_dedupAddr(addrEvs.filter(e=>e.kind===30311 && _matchAddr(e,ql))).sort((a,b)=>b.created_at-a.created_at).slice(0,12);
    const comms=_dedupAddr(addrEvs.filter(e=>e.kind===34550 && _matchAddr(e,ql))).sort((a,b)=>b.created_at-a.created_at).slice(0,12);
    const tors =addrEvs.filter(e=>e.kind===2003 && _matchAddr(e,ql)).sort((a,b)=>b.created_at-a.created_at).slice(0,12);
    const repos=_dedupAddr(addrEvs.filter(e=>e.kind===30617 && _matchAddr(e,ql))).sort((a,b)=>b.created_at-a.created_at).slice(0,12);
    const profs=Store.profileList().filter(p=>(((p.meta.name||'')+(p.meta.display_name||'')+(p.meta.nip05||'')).toLowerCase().includes(ql))).slice(0,12);
    let html='';
    if(profs.length){ html+='<div class="search-section-title">Profiles</div>'; for(const p of profs){ const m=p.meta; html+=`<div class="psearch" data-prof="${p.pubkey}"><img src="${enc(m.picture||LOGO)}" onerror="this.src='${LOGO}'"><div><b>${enc(m.name||m.display_name||'anon')}</b><div class="muted small">${enc(niceNip05(m.nip05)||(m.about||'').slice(0,60))}</div></div></div>`; } }
    if(arts.length){  html+='<div class="search-section-title">📝 Articles</div>'+arts.map(articleCard).join(''); }
    if(strms.length){ html+='<div class="search-section-title">▷ Streams</div><div class="stream-grid">'+strms.map(streamCard).join('')+'</div>'; }
    if(comms.length){ html+='<div class="search-section-title">☷ Communities</div><div class="stream-grid">'+comms.map(communityCard).join('')+'</div>'; }
    if(tors.length){  html+='<div class="search-section-title">🧲 Torrents</div>'+tors.map(torrentCard).join(''); }
    if(repos.length){ html+='<div class="search-section-title">🌱 Git Repos</div>'+repos.map(repoCard).join(''); }
    const posts=postEvs.sort((a,b)=>b.created_at-a.created_at);
    html+='<div class="search-section-title">Posts</div>';
    html+= posts.length ? `<div id="search-posts">${posts.map(e=>noteHtml(e)).join('')}</div>` : '<div class="empty">No matching posts.</div>';
    feed.innerHTML=html; hydrate(feed);
    $$('[data-prof]',feed).forEach(el=> el.onclick=()=>renderProfileView(el.dataset.prof));
    // Discover result cards → open the right view (community vs stream share .stream-card → split by kind).
    $$('.article-card',feed).forEach(c=> c.onclick=ev=>{ if(ev.target.closest('[data-prof]')){ renderProfileView(c.dataset.pk); return; } const a=Store.get(c.dataset.id); if(a) openArticle(a); });
    $$('.stream-card',feed).forEach(c=> c.onclick=ev=>{ if(ev.target.closest('[data-prof]')){ renderProfileView(c.dataset.pk); return; } const x=Store.get(c.dataset.id); if(x) (x.kind===34550?openCommunity:openStream)(x); });
    $$('.tor-copy',feed).forEach(b=> b.onclick=async()=>{ try{ await navigator.clipboard.writeText(b.dataset.magnet); toast('magnet copied'); }catch(_){ window.prompt('Magnet:', b.dataset.magnet); } });
    $$('.repo-clone',feed).forEach(b=> b.onclick=async()=>{ try{ await navigator.clipboard.writeText(b.dataset.clone); toast('clone URL copied'); }catch(_){ window.prompt('Clone:', b.dataset.clone); } });
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
  function hydrate(scope){ decorateProfiles(); hydrateLinkCards(scope); hydrateCounts(); hydratePolls(scope); }
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
    // The leading group skips entities that are part of a URL/word (e.g. "zapstore.dev/apps/naddr1…")
    // — those were already turned into <a> links above, and re-embedding them broke the href HTML.
    h=h.replace(/(^|[^\w/.])((?:nostr:)?(?:npub1|nprofile1|nevent1|note1|naddr1)[0-9a-z]{20,})/gi, (m,pre,ent)=>{
      try{
        const d=NT().nip19.decode(ent.replace(/^nostr:/i,''));
        if(d.type==='npub' || d.type==='nprofile'){
          const pk = d.type==='npub' ? d.data : d.data.pubkey;
          needProfile(pk); const nm=(Store.profile(pk)||{}).name||(Store.profile(pk)||{}).display_name;
          return pre+`<a href="#" class="mention" data-np="${NT().nip19.npubEncode(pk)}">@${nm?enc(nm):'profile'}</a>`;
        }
        if(d.type==='note' || d.type==='nevent'){
          const id = d.type==='note' ? d.data : d.data.id;
          const o = Store.get(id);
          if(o) return pre+quotedDiv(o);                   // already cached → embed now
          needEvent(id);                                   // else fetch; patchLoaded swaps it in
          return pre+`<div class="quoted muted small" data-qload="${enc(id)}">referenced note loading…</div>`;
        }
        if(d.type==='naddr'){                              // addressable event (e.g. NIP-23 article)
          const a=d.data||{};
          if(a.kind!=null && a.pubkey && a.identifier!=null){
            const key=`${a.kind}:${a.pubkey}:${a.identifier}`;
            const cached=_adCache.get(key);
            if(cached) return pre+addrDiv(cached);         // already fetched → embed now
            needAddr(a.kind, a.pubkey, a.identifier);       // else fetch; flushAddrs patches it in
            return pre+`<div class="quoted muted small" data-naload="${enc(key)}">📄 referenced post loading…</div>`;
          }
          return pre+`<a href="#" class="naddrlink" data-pk="${enc(a.pubkey||'')}" data-d="${enc(a.identifier||'')}" data-k="${enc(String(a.kind||''))}">📄 view</a>`;
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
  function openLightbox(src, kind){ try{ const x=new URL(src, location.href); x.searchParams.delete('thumb'); src=x.href; }catch(_){}  // always full-res, never the ?thumb=1 grid image
    const bg=document.createElement('div'); bg.className='lightbox';
    let el;
    if(kind==='video'){ el=document.createElement('video'); el.src=src; el.controls=true; el.autoplay=true; el.playsInline=true; el.setAttribute('playsinline',''); }
    else if(kind==='audio'){ el=document.createElement('audio'); el.src=src; el.controls=true; el.autoplay=true; }
    else { el=document.createElement('img'); el.src=src; }
    bg.appendChild(el);
    bg.onclick=(e)=>{ if(e.target===bg) bg.remove(); };   // click backdrop (not the media/controls) to close
    document.body.appendChild(bg); }

  // ---------- right column: Hot / Trending (desktop) ----------
  // First paint: build all three sections. Hot is an infinite-scroll feed (see _hot below).
  async function loadRightbar(){
    if(!document.querySelector('.rightbar')) return;
    loadHot(true);                   // Hot = most-engaged posts, infinite-scroll
    loadTrendingTags();              // Trending = trending hashtags (last 24h)
    loadDiscover();                  // curated hashtag shortcuts for newcomers
    loadFollows();                   // From follows = what people you follow liked/boosted
  }
  // Routine update (timer): refresh the chip clouds and prepend any freshly-hot posts to the top
  // of the Hot feed WITHOUT rebuilding it (so an in-progress scroll isn't yanked back up).
  function refreshRightbar(){
    if(document.hidden || !document.querySelector('.rightbar')) return;
    // Only auto-refresh the (heavy: 300-event trending tally + follow reposts) rightbar while the user is
    // on a feed it belongs to. Refreshing trending/hot every interval while reading a Community/Profile/
    // Files view was needless CPU + relay load for content that isn't even being looked at.
    if(VIEW!=='home' && VIEW!=='global') return;
    loadTrendingTags(); loadDiscover(); loadFollows(); refreshHotTop();
  }
  // Curated hashtag shortcuts — friendly entry points into popular communities for new users.
  const DISCOVER_TAGS = [['foodstr','🍔'], ['asknostr','💬'], ['AI','🤖'], ['Bitcoin','₿'],
                         ['nostr','🟣'], ['art','🎨'], ['news','📰'], ['memes','😂']];
  function loadDiscover(){
    const el=document.getElementById('rb-discover'); if(!el) return;
    el.innerHTML=`<div class="tag-cloud">${DISCOVER_TAGS.map(([t,ic])=>
      `<button class="tag-chip disc" data-tag="${t.toLowerCase()}"><span class="disc-ic">${ic}</span> #${enc(t)}</button>`).join('')}</div>`;
    el.querySelectorAll('.tag-chip').forEach(b=> b.onclick=()=>renderHashtag(b.dataset.tag));
  }
  // Trending HASHTAGS: tally #tags across recent notes (explicit `t` tags + inline #hashtags),
  // rank by how many distinct posts used each, render clickable chips → a #tag feed.
  async function loadTrendingTags(){
    const el=document.getElementById('rb-trending'); if(!el) return;
    const since=Math.floor(Date.now()/1000)-24*3600;
    let evs=[]; try{ evs=await Relay.query([{ kinds:[1], since, limit:300 }]); }catch(_){}   // 300 recent posts is plenty for the tag tally; 600 doubled the relay serialize + client regex cost
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
    // The relay's #t filter is case-SENSITIVE, but trending lowercases tags AND counts inline #hashtags —
    // so a post tagged "LillyPhillips" (or one that only writes #LillyPhillips in its text) trended yet the
    // exact-lowercase #t query returned nothing. Also pull a content SEARCH, then keep only posts that
    // genuinely use the tag: a case-insensitive `t` tag OR an inline #tag in the text (matches trending).
    let evs=[]; try{ evs=await Relay.query([{ kinds:[1], '#t':[tag], limit:60 }, { kinds:[1], search:tag, limit:80 }]); }catch(_){}
    evs.forEach(e=>{ Store.saveEvent(e); needProfile(e.pubkey); });
    if(VIEW!=='hashtag') return;
    const _t=tag.replace(/[^a-z0-9_]/g,''), _rx=new RegExp('(^|\\s)#'+_t+'\\b','i');
    const posts=evs.filter(e=>e.kind===1 && ((e.tags||[]).some(t=>t[0]==='t'&&String(t[1]||'').toLowerCase().replace(/^#/,'')===tag) || _rx.test(e.content||'')))
                   .sort((a,b)=>b.created_at-a.created_at);
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
  // ---- Hot: infinite-scroll feed of the most-engaged posts ----------------------------------
  // Engagement = count of reactions/reposts (kinds 6,7) pointing at a note. We rank within a time
  // window and append the next page on scroll; when a window is exhausted we widen it (4h→8h→…)
  // until the cap, so scrolling keeps surfacing older-but-hot posts instead of dead-ending.
  const HOT_WIN0=4*3600, HOT_WIN_MAX=14*24*3600, HOT_PAGE=12, HOT_MAX=48;
  let _hot={ loading:false, done:false, win:HOT_WIN0, shown:new Set() };
  // Rank notes by engagement within `windowSec`; returns [[noteId,count],…] sorted desc.
  async function rankHot(windowSec){
    const since=Math.floor(Date.now()/1000)-windowSec;
    let evs=[]; try{ evs=await Relay.query([{ kinds:[6,7], since, limit:1500 }]); }catch(_){}
    const tally={};
    for(const e of evs){ const id=(e.tags.filter(t=>t[0]==='e').pop()||[])[1]; if(id) tally[id]=(tally[id]||0)+1; }
    return Object.entries(tally).sort((a,b)=>b[1]-a[1]);
  }
  async function fetchNotes(ids){
    const miss=ids.filter(id=>!Store.get(id)); if(!miss.length) return;
    try{ const notes=await Relay.query([{ ids:miss }]); notes.forEach(e=>{ Store.saveEvent(e); needProfile(e.pubkey); }); }catch(_){}
  }
  // Clean rightbar snippet: drop URLs and raw nostr: refs (nostr:nevent1…/npub1… looked ugly).
  function rbSnippet(content){
    return (content||'')
      .replace(/https?:\/\/\S+/g,'')
      .replace(/(?:nostr:)?(?:npub1|nprofile1|nevent1|note1|naddr1)[0-9a-z]{20,}/gi,'')
      .replace(/\s+/g,' ').trim().slice(0,140);
  }
  // Rightbar row body: snippet text + image thumbnail. Shows the actual attachment instead of a
  // bare "media" placeholder when a post is media-only.
  function rbBody(ev){
    const txt=rbSnippet(ev.content), img=postImageUrl(ev);
    let h = txt ? `<div class="rb-txt">${enc(txt)}</div>` : '';
    if(img) h += `<img class="rb-media" src="${enc(img)}" loading="lazy" onerror="this.remove()">`;
    return h || '<div class="rb-txt"><i>media</i></div>';
  }
  function hotRowHtml(id, count){
    const ev=Store.get(id); if(!ev||ev.kind!==1||isMutedView(ev)) return ''; const pr=profOf(ev.pubkey);   // respect mutes + word filter
    const txt=rbSnippet(ev.content);
    return `<div class="rb-item" data-open="${id}" data-pk="${ev.pubkey}"><div class="rb-head"><img class="rb-av" src="${enc(pr.picture||LOGO)}" onerror="this.src='${LOGO}'"><b>${enc(pr.name||pr.display_name||'anon')}</b> <span class="rb-fire">${count} 🔥</span></div>${rbBody(ev)}</div>`;
  }
  // "From follows": posts the people YOU follow have liked (kind 7) or boosted (kind 6), ranked by
  // how many of your follows engaged. Surfaces what your own network is reacting to in the rightbar.
  async function loadFollows(){
    const el=document.getElementById('rb-follows'); if(!el) return;
    const authors=[...FOLLOWS]; if(!authors.length){ el.innerHTML='<div class="muted small">Follow people to see what they’re into.</div>'; return; }
    const since=Math.floor(Date.now()/1000)-24*3600;
    let evs=[]; try{ evs=await Relay.query([{ kinds:[6,7], authors, since, limit:500 }]); }catch(_){}
    const tally={}, icon={};
    for(const e of evs){ if(e.pubkey===ME.pubkey) continue; const id=(e.tags.filter(t=>t[0]==='e').pop()||[])[1]; if(!id) continue;
      tally[id]=(tally[id]||0)+1; if(e.kind===6) icon[id]='🔁'; else if(!icon[id]) icon[id]='❤️'; }
    const top=Object.entries(tally).sort((a,b)=>b[1]-a[1]).slice(0,12).map(x=>x[0]);
    if(!top.length){ el.innerHTML='<div class="muted small">Nothing from your follows yet.</div>'; return; }
    await fetchNotes(top);
    const rows=top.map(id=>{ const ev=Store.get(id); if(!ev||ev.kind!==1||isMutedView(ev)) return ''; const pr=profOf(ev.pubkey);   // respect mutes + word filter
      const txt=rbSnippet(ev.content);
      return `<div class="rb-item" data-open="${id}" data-pk="${ev.pubkey}"><div class="rb-head"><img class="rb-av" src="${enc(pr.picture||LOGO)}" onerror="this.src='${LOGO}'"><b>${enc(pr.name||pr.display_name||'anon')}</b> <span class="rb-fire">${icon[id]||'❤️'} ${tally[id]}</span></div>${rbBody(ev)}</div>`;
    }).filter(Boolean).join('');
    el.innerHTML=rows||'<div class="muted small">Nothing yet.</div>'; decorateProfiles();
  }
  // Materialize up to HOT_PAGE not-yet-shown ranked items into the Hot column. `where` = append
  // (scroll-down) or prepend (routine refresh). Returns how many rows were actually added.
  async function addHot(el, ranked, where){
    const pick=[];
    for(const [id,c] of ranked){ if(_hot.shown.has(id)) continue; pick.push([id,c]); if(pick.length>=HOT_PAGE) break; }
    if(!pick.length) return 0;
    pick.forEach(([id])=>_hot.shown.add(id));   // mark before fetch so concurrent calls don't double-add
    await fetchNotes(pick.map(x=>x[0]));
    const frag=document.createDocumentFragment();
    for(const [id,c] of pick){ const html=hotRowHtml(id,c); if(!html) continue;
      const d=document.createElement('div'); d.innerHTML=html; const node=d.firstElementChild; if(node) frag.appendChild(node); }
    const n=frag.childElementCount; if(!n) return 0;
    Array.from(el.children).forEach(c=>{ if(!c.classList||!c.classList.contains('rb-item')) c.remove(); });  // drop loader/placeholder
    if(where==='prepend' && el.firstChild) el.insertBefore(frag, el.firstChild); else el.appendChild(frag);
    decorateProfiles();
    return n;
  }
  async function loadHot(reset){
    const el=document.getElementById('rb-hot'); if(!el) return;
    if(reset){ _hot={ loading:true, done:false, win:HOT_WIN0, shown:new Set() }; el.innerHTML='<div class="muted small">loading…</div>'; }
    const added=await addHot(el, await rankHot(_hot.win), 'append');
    if(reset && !added) el.innerHTML='<div class="muted small">Nothing yet.</div>';
    _hot.loading=false;
  }
  // Scroll-down handler: widen the window until we manage to append something or hit the cap.
  async function loadMoreHot(){
    if(_hot.loading||_hot.done) return; const el=document.getElementById('rb-hot'); if(!el) return;
    if(el.querySelectorAll('.rb-item').length>=HOT_MAX){ _hot.done=true; return; }   // cap so the column can loop
    _hot.loading=true;
    let added=0, guard=0;
    while(added===0 && guard++<8){
      added=await addHot(el, await rankHot(_hot.win), 'append');
      if(added===0){ if(_hot.win>=HOT_WIN_MAX){ _hot.done=true; break; } _hot.win=Math.min(_hot.win*2, HOT_WIN_MAX); }
    }
    _hot.loading=false;
  }
  // Routine refresh: prepend genuinely-new hot posts, but only when the user is near the top so we
  // never jump their scroll position out from under them.
  async function refreshHotTop(){
    const el=document.getElementById('rb-hot'); if(!el||_hot.loading) return;
    const rb=document.querySelector('.rightbar'); if(rb && rb.scrollTop>140) return;
    _hot.loading=true; try{ await addHot(el, await rankHot(HOT_WIN0), 'prepend'); } finally{ _hot.loading=false; }
  }
  function onRightbarScroll(){
    const rb=document.querySelector('.rightbar'); if(!rb) return;
    if(rb.scrollTop+rb.clientHeight >= rb.scrollHeight-320) loadMoreHot();
  }
  // Gentle auto-scroll "ticker": creep the rightbar down on its own so the column cycles through
  // Trending → Discover → Hot over and over without a hand on the wheel. Pauses while the pointer is
  // over the column (reading/clicking) or the tab is hidden. When the bottom is reached it loops
  // back to the top and refreshes the lap. Honours prefers-reduced-motion.
  const _auto={ on:true, acc:0, last:0, hold:0 };
  function startAutoScroll(){
    const rb=document.querySelector('.rightbar'); if(!rb) return;
    if(window.matchMedia && matchMedia('(prefers-reduced-motion: reduce)').matches) return;
    rb.addEventListener('mouseenter', ()=>{ _auto.on=false; });
    rb.addEventListener('mouseleave', ()=>{ _auto.on=true; });
    const SPEED=20;   // px/sec — slow, readable creep
    const step=(ts)=>{
      requestAnimationFrame(step);
      const dt=_auto.last?Math.min(ts-_auto.last,100):0; _auto.last=ts;
      if(!_auto.on || document.hidden) return;
      if(_auto.hold>0){ _auto.hold-=dt; return; }   // brief pause at the top of each lap
      const max=rb.scrollHeight-rb.clientHeight; if(max<=0) return;
      if(rb.scrollTop>=max-1){          // reached the end of the column → start the lap over
        if(!_hot.done){ loadMoreHot(); return; }   // still has more Hot to surface first
        rb.scrollTop=0; _auto.acc=0; _auto.hold=1500; refreshRightbar();   // loop + refresh the lap
        return;
      }
      _auto.acc += SPEED*dt/1000;
      if(_auto.acc>=1){ const d=Math.floor(_auto.acc); _auto.acc-=d; rb.scrollTop+=d; }
    };
    requestAnimationFrame(step);
  }

  // Shared surface for separate game modules (chess.js, future tic-tac-toe, …) so per-game UI lives
  // in its own file without bloating this core. Live getters for the mutable ME/CFG/VIEW.
  window.__PC = {
    $, $$, enc, publish, sendDm, safePk, nip05Resolve, profOf, needProfile, niceNip05, LOGO, toast,
    ensureProfile: _ensureProfile, NT,
    // NIP-44 decrypt with the current signer (any login type) — games use it to read their own
    // encrypted hole cards from a public game-state doc.
    nip44dec: (peer, ct) => (signer && signer.nip44dec) ? signer.nip44dec(peer, ct) : Promise.reject(new Error('no nip44')),
    get ME(){ return ME; }, get CFG(){ return CFG; }, get VIEW(){ return VIEW; },
  };

  document.addEventListener('DOMContentLoaded', boot);
})();
