/* Concord communities — a dedicated Discord/Matrix-shaped workspace (separate from public Chat). */
(function(){
  'use strict';
  // Do not depend on the monolithic shell CSS being current: an older service worker may serve its
  // cached client.css for one navigation. Concord owns a versioned sheet and loads it itself too.
  if(!document.querySelector('link[data-concord-css]')){
    const l=document.createElement('link'); l.rel='stylesheet'; l.dataset.concordCss='1';
    l.href='/static/css/concord.css?v=26'; (document.head||document.documentElement).appendChild(l);
  }
  const PC=()=>cordDeps(window.__PC);
  /* CONCORD THEMES. How Concord looks to ME (per account, never per community): a preset name plus an
   * optional accent and background. The palette itself is CSS (concord.css, "CONCORD THEMES"); this only
   * puts `data-cc-theme` and any custom colours on <html>, so the menus and sheets Concord opens outside
   * its own root follow too. Painted from a localStorage copy at load -- before any relay is asked -- and
   * synced in the account's `pcai:client-prefs` document as `concordTheme`; the newer `at` wins. */
  const ccTheme=(()=>{
    const PRESETS=[['cyberpunk','Cyberpunk'],['classic','Classic'],['synthwave','Synthwave'],['monero','Monero'],['light','Light'],['oled','OLED black'],['app','Match app theme']];
    const IDS=PRESETS.map(x=>x[0]),DEF='cyberpunk',KEY='pc.concord.theme.v1.',LAST='pc.concord.theme.v1:last';
    const hexOk=v=>/^#[0-9a-f]{6}$/i.test(String(v||''))?String(v).toLowerCase():'';
    const clean=v=>v&&typeof v==='object'?{theme:IDS.includes(v.theme)?v.theme:DEF,accent:hexOk(v.accent),bg:hexOk(v.bg),at:Math.max(0,Number(v.at)||0)}:null;
    const load=k=>{try{return clean(JSON.parse(localStorage.getItem(k)||'null'));}catch(_){return null;}};
    const store=(k,v)=>{try{localStorage.setItem(k,JSON.stringify(v));}catch(_){}};
    const rgb=h=>[1,3,5].map(i=>parseInt(h.slice(i,i+2),16));
    const hex=c=>'#'+c.map(x=>Math.round(Math.max(0,Math.min(255,x))).toString(16).padStart(2,'0')).join('');
    const mix=(a,b,t)=>{const x=rgb(a),y=rgb(b);return hex(x.map((v,i)=>v+(y[i]-v)*t));};
    const lum=h=>{const [r,g,b]=rgb(h).map(x=>{x/=255;return x<=.03928?x/12.92:((x+.055)/1.055)**2.4;});return .2126*r+.7152*g+.0722*b;};
    /* Everything a custom background has to repaint: the core surfaces and, because Classic writes its
     * roles as literals, every role that is a surface or text on one. Text is chosen from the
     * background's own lightness so a custom colour cannot make the messages unreadable. */
    function bgVars(bg){
      const dark=lum(bg)<.3,ink=dark?'#eceefa':'#1b1f2e',k=dark?'#000000':'#000000',w=dark?'#ffffff':'#000000';
      const s={main:bg,side:mix(bg,k,dark?.25:.04),rail:mix(bg,k,dark?.45:.1),hover:mix(bg,w,dark?.08:.07),raised:mix(bg,w,dark?.05:.025),
        pop:mix(bg,k,dark?.2:0),sunk:mix(bg,k,dark?.35:.06),chip:mix(bg,w,dark?.12:.11),text:ink,strong:dark?'#ffffff':'#0a0c14',muted:mix(ink,bg,.3)};
      const v={'--cct-tint-rgb':dark?'255,255,255':'20,30,60'};
      for(const n of ['main','side','rail','hover','raised','pop','sunk','chip','text','strong','muted'])v['--cct-'+n]=s[n];
      v['--cct-dim']=s.muted;
      const as={raised:['compose','reply','card','room','joined'],hover:['room-hover','joined-hover','chip-2-hover'],chip:['chip-2','public-icon'],
        pop:['toolbar','menu'],sunk:['identity','media'],strong:['title'],text:['fg-menu','input','label','icon','icon-2','react-fg'],
        muted:['fg-2','fg-3','fg-4','discover-fg','section','hash','header-hash','welcome','placeholder','room-sub','reply-fg']};
      for(const [from,roles] of Object.entries(as))for(const r of roles)v['--cct-'+r]=s[from];
      return v;
    }
    let owner='',cur=load(LAST)||{theme:DEF,accent:'',bg:'',at:0},painted=[];
    function paint(t){
      /* Never fatal: Concord's node harnesses (and any odd embed) hand it a minimal document. */
      const root=typeof document!=='undefined'&&document.documentElement;
      if(!root||typeof root.setAttribute!=='function'||!root.style||typeof root.style.setProperty!=='function')return;
      try{
        root.setAttribute('data-cc-theme',t.theme);
        for(const n of painted)root.style.removeProperty(n);
        const v=t.bg?bgVars(t.bg):{};
        /* --cct-line too: it was the one colour a custom accent left alone, so every bordered tile and sheet kept
         * the PRESET's line colour under the new accent (git issue "Concord Theme match": "one color that does not change"). */
        if(t.accent){const c=rgb(t.accent);v['--cct-accent']=t.accent;v['--cct-accent-rgb']=c.join(',');v['--cct-on-accent']=lum(t.accent)>.36?'#05060b':'#ffffff';v['--cct-line']=`rgba(${c.join(',')},.28)`;}
        painted=Object.keys(v); for(const n of painted)root.style.setProperty(n,v[n]);
      }catch(_){}
      try{document.querySelectorAll('[data-cc-theme-pick]').forEach(b=>{const on=b.dataset.ccThemePick===t.theme;b.classList.toggle('on',on);b.setAttribute('aria-checked',String(on));});}catch(_){}
    }
    function remember(t){cur=t;store(LAST,t);if(owner)store(KEY+owner,t);paint(t);}
    function sync(){try{const p=PC();if(p&&p.saveConcordTheme&&owner)p.saveConcordTheme({theme:cur.theme,accent:cur.accent,bg:cur.bg,at:cur.at});}catch(_){}}
    /* Concord's render tells us whose workspace this is; a different account gets ITS theme (or the default). */
    function forOwner(pk){pk=String(pk||'');if(!pk||pk===owner)return;owner=pk;const mine=load(KEY+pk);remember(mine||{theme:DEF,accent:'',bg:'',at:0});}
    function set(patch,{save=true}={}){const t=clean({...cur,...(patch||{}),at:Date.now()});if(!t)return cur;remember(t);if(save)sync();return t;}
    /* From the account's synced prefs (blossom.js restoreClientPrefsNostr). Newer wins either way. */
    function adopt(remote,pk){
      const r=clean(remote); pk=String(pk||'');
      if(!r||(pk&&owner&&pk!==owner))return;
      if(pk&&!owner)owner=pk;
      const mine=load(KEY+owner);
      if(!mine||r.at>mine.at)remember(r);
      else if(mine.at>r.at)sync();
    }
    function openPicker(p){
      if(!p||!p.modal)return;
      const tiles=PRESETS.map(([id,label])=>`<button type="button" class="cc-theme-swatch${cur.theme===id?' on':''}" data-cc-theme="${id}" data-cc-theme-pick="${id}" role="radio" aria-checked="${cur.theme===id}"><span class="cc-theme-prev" aria-hidden="true"><i></i><i></i><i><b></b><b></b></i></span><span>${label}${id===DEF?' <small>default</small>':''}</span></button>`).join('');
      p.modal(`<h3>Concord theme</h3><p class="muted">How Concord looks to you, on every device you sign in to.</p>
        <div class="cc-theme-grid" role="radiogroup" aria-label="Theme">${tiles}</div>
        <div class="cc-theme-custom"><label>Accent colour<input type="color" id="cc-theme-accent" value="${cur.accent||'#00f0ff'}"></label><label>Background<input type="color" id="cc-theme-bg" value="${cur.bg||'#0e0c1f'}"></label>
        <button type="button" class="btn btn-ghost" id="cc-theme-reset">Reset to preset colours</button></div>
        ${window.PCLook?'<p class="cc-theme-whole"><button type="button" class="btn btn-ghost" id="cc-theme-whole">Use these colours for the whole app</button> <span class="muted">Everything else (sections, wallpaper) is in Settings → Appearance.</span></p>':''}
        <p class="cc-theme-note" id="cc-theme-note">${cur.accent||cur.bg?'Custom colours are on.':'Using the preset’s own colours.'}</p>
        <div class="cc-join-actions"><button type="button" class="btn btn-neon" id="cc-theme-done">Done</button></div>`,box=>{
        const note=()=>{const n=box.querySelector('#cc-theme-note');if(n)n.textContent=cur.accent||cur.bg?'Custom colours are on.':'Using the preset’s own colours.';};
        box.querySelectorAll('[data-cc-theme-pick]').forEach(b=>b.onclick=()=>{set({theme:b.dataset.ccThemePick});note();});
        for(const [id,key] of [['#cc-theme-accent','accent'],['#cc-theme-bg','bg']]){const i=box.querySelector(id);if(!i)continue;
          i.oninput=()=>{set({[key]:i.value},{save:false});note();}; i.onchange=()=>{set({[key]:i.value});note();};}
        const reset=box.querySelector('#cc-theme-reset');if(reset)reset.onclick=()=>{set({accent:'',bg:''});note();};
        /* "It should change the app theme in every page" (git issue). The colours go to Settings -> Appearance and
         * Concord switches to Match app theme, so the one look is kept in one place and Concord follows it. */
        const whole=box.querySelector('#cc-theme-whole');if(whole)whole.onclick=()=>{
          if(!cur.accent&&!cur.bg){const n=box.querySelector('#cc-theme-note');if(n)n.textContent='Pick an accent or background first.';return;}
          window.PCLook.set({accent:cur.accent||window.PCLook.get().accent,bg:cur.bg||window.PCLook.get().bg});
          set({theme:'app',accent:'',bg:''});const n=box.querySelector('#cc-theme-note');if(n)n.textContent='The whole app uses these colours now; Concord matches it.';};
        const done=box.querySelector('#cc-theme-done');if(done)done.onclick=()=>{try{p.closeModal&&p.closeModal();}catch(_){}};
      });
    }
    paint(cur);
    return {PRESETS,forOwner,set,adopt,openPicker,get:()=>({...cur})};
  })();
  window.PCConcordTheme=ccTheme;
  const concordScriptUrl=document.currentScript&&document.currentScript.src;
  const callModules=new Map();
  /* Automatic reads must contain only known Concord endpoints. relay.ditto.pub currently refuses
   * WebSocket handshakes, and copying the user's general pool into external discovery also created
   * redundant Damus sockets. Explicit invite/bootstrap and room bundle relays remain authoritative
   * below, even when they name one of those hosts. */
  const CORD_RELAYS=['wss://jskitty.com/nostr','wss://asia.vectorapp.io/nostr','wss://nostr.computingcache.com','wss://relay.dreamith.to'];
  /* How much cached history the channel on screen decrypts before it is drawn. A channel view
   * shows a couple of dozen messages; everything past that is scrollback and belongs behind the
   * paint, not in front of it. */
  const FIRST_PAINT_WRAPS=60;
  /* ...AND A WRAP IS NOT A MESSAGE, WHICH IS WHY 60 OF THEM SOMETIMES SHOWS ALMOST NOTHING.
   *
   * The budget above is a decrypt cost, counted in envelopes. What lands on screen is what those
   * envelopes FOLD into, and reactions, edits, deletions and control events all consume one without
   * adding a line — so an active channel can spend the whole allowance and paint a handful of
   * messages. Reported as "concord caching always caches a little bit of the room history and then
   * loads the new content": the screenful arrives, it is too short, and the rest visibly fills in
   * behind it.
   *
   * So the first paint asks for a screenful of MESSAGES and tops up ONCE if it did not get one. The
   * fast path is unchanged — a channel whose 60 envelopes already fold into a screenful never pays
   * for the second read. */
  const FIRST_PAINT_MESSAGES=24;
  const DISCOVER_RELAYS=['wss://relay.dreamith.to'];
  const LEGACY_RECOVERY_RELAYS=['wss://relay.ditto.pub','wss://relay.damus.io'];
  /* Invite relays bootstrap the first read; authenticated metadata owns subsequent transport. */
  function roomRelays(bundle){
    let listed=bundle?.relays||[],authoritative=false;
    try{const room=typeof saved==='function'?saved().find(r=>r.cord?.bundle?.community_id===bundle?.community_id):null;const wraps=roomControls.get(bundle?.community_id)||roomControls.get(room?.communityId||room?.naddr);if(wraps?.length){const info=window.PosterCordReader.inspectControl(bundle,wraps);if(info.relaysAuthoritative){listed=info.relays;authoritative=true;}}}catch(_){}
    const own=[...new Set(listed.map(normalizeRelay).filter(Boolean))];
    return (authoritative||own.length?own:CORD_RELAYS).slice(0,8);
  }
  /* CONCORD ON THIS NODE'S RELAY (app/services/nostr_relay/concord_mirror.py).
   *
   * On an instance, a community's messages are read from and written to THIS NODE'S relay, never the
   * community's own relays, in two cases: the community was created here (its only relay IS this node's),
   * or the node has ACCEPTED a mirror registration naming every stream the read asks for. A registration
   * (kind 27059, sent to this relay only) names the community's relay URLs and its derived stream pubkeys
   * -- what every one of those relays already sees in any member's REQ -- under an opaque group label; the
   * node then holds the wraps, opaque, and forwards members' posts upstream.
   *
   * THE FALLBACK IS "COULD NOT ASK", NEVER "EMPTY". When this node's relay cannot answer (no socket, a
   * timeout, or its own `unavailable:` -- the mirror is not in step with that community yet), the read goes
   * to the community's own relays exactly as it did before this existed. A stream the node was never told
   * about is never asked of it: an empty answer about it would be a claim it has not earned. Browsers
   * cannot use the node's outbound proxy, so that fallback is a direct connection from this device, as
   * every Concord read was before. */
  const MIRROR_KIND=27059,MIRROR_FRESH_MS=86400000,MIRROR_TRUST_MS=3*86400000,MIRROR_REFUSED_MS=3600000,MIRROR_MAX_AUTHORS=128;
  const mirrorBusy=new Map(),mirrorRefused=new Map();
  /* CONCORD TALKS ONLY TO THIS NODE (app/services/concord_tunnel.py). Every connection Concord makes to a relay
   * that is not this node's goes through `/ws/concord-relay` on the instance, with a short-lived member ticket:
   * the CORD relays (membership vault, invites, listings), the directory, NIP-29 rooms and the rooms of
   * communities hosted elsewhere. `via` only changes the socket that is DIALED -- targets, cooldowns, reports and
   * AUTH keep the real relay url. No instance (relays-only app) or a node that cannot carry it ({ticket:''})
   * dials directly, as before; a ticket that could not be fetched yet is NOT a reason to go direct: the dial
   * goes to the tunnel, fails like an unreachable relay and is retried once the ticket arrives. */
  const TUNNEL={ticket:'',until:0,off:false,busy:null,timer:null};
  /* /ws/concord-relay is an APP route, so it is reached at the INSTANCE's address -- never the relay's host:port, which on
   * a node reached directly (http://host:3051, relay ws://host:3052) or over its .onion is the relay process, which
   * accepted the dial as an ordinary relay connection. With no instance origin (relays-only) there is no tunnel. */
  function tunnelBase(p){if(!homeRelayUrl(p))return '';let o='';
    /* SYNCHRONOUSLY KNOWN, never p.serverOrigin(): that one lives in a lazily loaded module and answers nothing until it
     * loads, which would quietly turn the tunnel off. A bundle names its instance in __PC_API_BASE__ ('' = relays only,
     * no tunnel); a page served by the instance IS on it. */
    try{o=typeof window.__PC_API_BASE__==='string'?window.__PC_API_BASE__:String((typeof location!=='undefined'&&location.origin)||'');}catch(_){o='';}
    if(!o)return '';try{const u=new URL(o);return (u.protocol==='http:'?'ws:':'wss:')+'//'+u.host+'/ws/concord-relay';}catch(_){return '';}}
  function refreshTunnel(){
    if(TUNNEL.busy)return TUNNEL.busy;
    TUNNEL.busy=(async()=>{try{const r=await fetch('/client/concord/tunnel-ticket',{credentials:'include',cache:'no-store'});
      if(r.ok){const j=await r.json();TUNNEL.ticket=String(j&&j.ticket||'');TUNNEL.off=!TUNNEL.ticket;TUNNEL.until=Date.now()+Math.max(60,Number(j&&j.ttl||0)*0.8)*1000;}
      else if(r.status===401||r.status===403){TUNNEL.ticket='';TUNNEL.off=true;TUNNEL.until=Date.now()+300000;}
    }catch(_){}finally{TUNNEL.busy=null;}
      clearTimeout(TUNNEL.timer);TUNNEL.timer=setTimeout(refreshTunnel,TUNNEL.ticket?Math.max(60000,TUNNEL.until-Date.now()):30000);})();
    return TUNNEL.busy;
  }
  function cordVia(p){
    const base=tunnelBase(p||(typeof PC==='function'?PC():null));if(!base)return null;
    if(Date.now()>TUNNEL.until)refreshTunnel();
    if(TUNNEL.off&&!TUNNEL.ticket)return null;
    const home=homeRelayUrl(p||(typeof PC==='function'?PC():null));
    return u=>{const n=normalizeRelay(u);if(!n||n===home)return u;return base+'?u='+encodeURIComponent(u)+'&t='+encodeURIComponent(TUNNEL.ticket);};
  }
  /* The two doors Concord reaches relays through -- the app's dependency object (PC()) and (window.PCConcordRelay||window.Relay) -- are
   * WRAPPED, so every outside-relay call carries `via` without any call site knowing: a new call cannot forget
   * it, and a function a test slices out still runs against whatever it is handed. */
  /* A SEND THE TUNNEL COULD NOT CARRY GOES DIRECT ONCE. A send is one signed event -- the same id however many times
   * it goes out -- so a second copy is harmless, while a lost one is a message that never arrived: 2026-10-13 the
   * per-member cap was full of Concord's live subscriptions and every send after it was a socket closed before it
   * opened ("concord not even sending messages"). Reads keep the tunnel; the next send tries it again first.
   * `via` is the function the call was given (a falsy one means it never went through the tunnel). An authed
   * NIP-42 publish signs nothing new on retry, but its failure text is checked: a relay that REFUSED the event
   * would refuse it direct too. */
  const SOCKET_FAIL=/^(closed|socket error|connect failed|timeout)$/;
  function sendOr(first,via,direct,authed){
    const v=via&&typeof via==='object'?via.via:via;if(!v)return first;
    return Promise.resolve(first).then(r=>{
      const ok=typeof r==='number'?r>0:!!(r&&r.ok);if(ok)return r;
      if(authed&&!SOCKET_FAIL.test(String(r&&r.msg||'')))return r;
      return direct();
    });
  }
  const WRAPPED=new WeakMap();
  function viaOpts(o){const via=cordVia(window.__PC);return via?{...(o||{}),via}:o;}
  function cordDeps(raw){
    if(!raw||typeof raw!=='object')return raw;let w=WRAPPED.get(raw);if(w)return w;
    const over={
      relayQueryFrom:(relays,filters,o)=>raw.relayQueryFrom(relays,filters,viaOpts(o)),
      relayPublishTo:(relays,ev,o)=>{const vo=viaOpts(o);return sendOr(raw.relayPublishTo(relays,ev,vo),vo,()=>raw.relayPublishTo(relays,ev,o));},
      relayPublishRoom:(relays,ev,auth=null)=>{const via=cordVia(raw);
        return sendOr(raw.relayPublishRoom(relays,ev,auth,via),via,()=>raw.relayPublishRoom(relays,ev,auth,null));},
      publishNip29Authed:(relay,template)=>{const via=cordVia(raw);const go=raw.publishNip29Authed(relay,template,via);
        return via?go.catch(e=>{if(!SOCKET_FAIL.test(String(e&&e.message||'')))throw e;return raw.publishNip29Authed(relay,template,null);}):go;},
    };
    w=new Proxy(raw,{get(t,k){if(Object.prototype.hasOwnProperty.call(over,k)&&typeof t[k]==='function')return over[k];return t[k];}});
    WRAPPED.set(raw,w);return w;
  }
  function cordRelay(R){
    if(!R||typeof R!=='object')return R;let w=WRAPPED.get(R);if(w)return w;
    const over={
      queryFrom:(u,f,o)=>R.queryFrom(u,f,viaOpts(o)),
      publishTo:(u,ev,o)=>{const vo=viaOpts(o);return sendOr(R.publishTo(u,ev,vo),vo,()=>R.publishTo(u,ev,o));},
      subscribeFrom:(u,f,o)=>R.subscribeFrom(u,f,viaOpts(o)),
      publishAuthed:(u,ev,sign,o)=>{const vo=viaOpts(o);return sendOr(R.publishAuthed(u,ev,sign,vo),vo,()=>R.publishAuthed(u,ev,sign,o),true);},
    };
    w=new Proxy(R,{get(t,k){if(Object.prototype.hasOwnProperty.call(over,k))return over[k];const v=t[k];return typeof v==='function'?v.bind(t):v;},
      set(t,k,v){t[k]=v;return true;}});
    WRAPPED.set(R,w);return w;
  }
  /* concord-git.js ARRIVES WITH THE FIRST CONCORD PAINT, not with the app: 88 KB nobody who never opens Communities
   * needs on a cold load (tests/test_client_boot_payload_budget.py). Every use of PCConcordGit here is guarded, so
   * the first paint simply has no git rows and the repaint on load adds them. Same ?v= as this file, so the
   * service worker's precache answers it offline. */
  let concordGitLoad=null;
  function loadConcordGit(){
    if(concordGitLoad||window.PCConcordGit)return;
    const me=document.querySelector('script[src*="/static/js/client/concord.js"]'),src=me&&me.getAttribute('src')||'';
    const s=document.createElement('script');s.src='/static/js/client/concord-git.js'+(src.indexOf('?')>=0?src.slice(src.indexOf('?')):'');
    concordGitLoad=s;s.onload=()=>backgroundRender();s.onerror=()=>{concordGitLoad=null;s.remove();};document.head.appendChild(s);
  }
  Object.defineProperty(window,'PCConcordRelay',{configurable:true,get(){return cordRelay(window.Relay);}});
  function homeRelayUrl(p){try{return normalizeRelay(p&&p.homeRelay&&p.homeRelay())||'';}catch(_){return '';}}
  function relaysAreHome(p,relays){const h=homeRelayUrl(p);return !!h&&(relays||[]).some(u=>normalizeRelay(u)===h);}
  function mirrorKey(pk){return 'pc.concord.mirror.v1.'+pk;}
  function mirrorState(pk){try{const v=JSON.parse(localStorage.getItem(mirrorKey(pk))||'null');return v&&typeof v==='object'&&v.groups&&typeof v.groups==='object'?v:{groups:{}};}catch(_){return {groups:{}};}}
  function saveMirrorState(pk,v){try{localStorage.setItem(mirrorKey(pk),JSON.stringify(v));}catch(_){}}
  /* Streams this node has said it mirrors for the signed-in account, newer than MIRROR_TRUST_MS. */
  function mirroredAuthors(pk){const out=new Set(),now=Date.now();if(!pk)return out;for(const g of Object.values(mirrorState(pk).groups))if(g&&now-Number(g.at||0)<MIRROR_TRUST_MS)for(const a of g.authors||[])out.add(a);return out;}
  /* THE RELAYS THIS NODE SAID IT CANNOT READ for every stream in `filters` (the mirror's registration reply, after
   * "read directly:" -- relay.damus.io gates kind 1059 behind an AUTH as the stream key, which only members hold),
   * or null when any stream was registered by a build that did not record them. With a list the client asks this
   * node for its copy (subscription 'pcmp…', served even though that relay refused the node) and reads ONLY those
   * relays itself. Without, the node's strict answer stands: unavailable, then every relay of the community --
   * measured 2026-10-14, ~64 tunnels a minute per device for two communities that list damus. */
  const HOME_PARTIAL='pcmp',READ_DIRECTLY='read directly:';
  function parseReadDirectly(msg){const s=String(msg||''),i=s.indexOf(READ_DIRECTLY);if(i<0)return [];
    return [...new Set(s.slice(i+READ_DIRECTLY.length).trim().split(/\s+/).map(normalizeRelay).filter(Boolean))];}
  function homeDirect(filters){
    const groups=Object.values(mirrorState(accountPk()).groups),now=Date.now(),direct=new Set();
    for(const f of filters||[])for(const a of f&&f.authors||[]){
      const g=groups.find(x=>x&&now-Number(x.at||0)<MIRROR_TRUST_MS&&(x.authors||[]).includes(a)&&Array.isArray(x.direct));
      if(!g)return null;g.direct.forEach(u=>direct.add(u));
    }
    return [...direct];
  }
  /* THIS NODE'S RELAY, when it may answer `filters` for a community on `relays` -- else ''. */
  function homeServes(p,relays,filters){
    const h=homeRelayUrl(p);if(!h||!Array.isArray(filters)||!filters.length)return '';
    for(const f of filters)if(!f||!Array.isArray(f.kinds)||!f.kinds.length||!f.kinds.every(k=>k===1059||k===21059)||!Array.isArray(f.authors)||!f.authors.length)return '';
    if(relaysAreHome(p,relays))return h;
    const held=mirroredAuthors(accountPk());if(!held.size)return '';
    for(const f of filters)for(const a of f.authors)if(!held.has(a))return '';
    return h;
  }
  async function mirrorGroup(communityId){
    const id=String(communityId||'');if(!id||!(globalThis.crypto&&crypto.subtle))return '';
    const d=await crypto.subtle.digest('SHA-256',new TextEncoder().encode('pc-concord-mirror:'+id));
    return [...new Uint8Array(d)].map(b=>b.toString(16).padStart(2,'0')).join('');
  }
  function mirrorAuthorsOf(bundle,wraps){
    const r=window.PosterCordReader,out=[],add=a=>{a=String(a||'').toLowerCase();if(/^[0-9a-f]{64}$/.test(a)&&!out.includes(a))out.push(a);};
    try{const info=r.inspectControl(bundle,wraps||[]);(info.controlPubkeys||[]).forEach(add);(info.guestbookPubkeys||[]).forEach(add);for(const ch of info.channels||[])(ch.streamPubkeys||[]).forEach(add);}catch(_){}
    try{if(r.inspectRekeyStreams)r.inspectRekeyStreams(bundle).forEach(add);}catch(_){}
    return out.slice(0,MIRROR_MAX_AUTHORS);
  }
  async function sendMirrorRequest(p,body){
    const h=homeRelayUrl(p),R=(window.PCConcordRelay||window.Relay);if(!h||!R||!R.publishTo||!p.signTemplate)return {ok:false,msg:'no home relay'};
    const ev=await p.signTemplate({kind:MIRROR_KIND,created_at:Math.floor(Date.now()/1000),tags:[['t','concord-mirror']],content:JSON.stringify({v:1,...body})});
    if(!ev||!ev.id)return {ok:false,msg:'not signed'};
    return await R.publishTo([h],ev,{includeManaged:true,detailed:true,max:1})||{ok:false};
  }
  /* Ask this node to mirror a joined EXTERNAL community, once a day at most (and never for one created
   * here). Resolves true when every stream it named is mirrored. Never throws: a refusal leaves the room
   * reading its own relays, which is exactly what it did before. */
  function ensureMirror(p,room,bundle,wraps){
    const pk=accountPk(),relays=bundle?roomRelays(bundle):[];
    if(!pk||!bundle||bundle.dissolved||!homeRelayUrl(p)||relaysAreHome(p,relays))return Promise.resolve(false);
    const key=pk+'\n'+String(bundle.community_id||'');if(mirrorBusy.has(key))return mirrorBusy.get(key);
    const job=(async()=>{
      const group=await mirrorGroup(bundle.community_id);if(!group)return false;
      const want=mirrorAuthorsOf(bundle,wraps);if(!want.length)return false;
      const st=mirrorState(pk),prior=st.groups[group],now=Date.now();
      const same=!!prior&&JSON.stringify(prior.relays||[])===JSON.stringify(relays);
      if(same&&now-Number(prior.at||0)<MIRROR_FRESH_MS&&want.every(a=>(prior.authors||[]).includes(a))&&Array.isArray(prior.direct))return true;
      const refused=mirrorRefused.get(key);if(refused&&now-refused<MIRROR_REFUSED_MS)return false;
      const authors=[...new Set([...(same&&now-Number(prior.at||0)<MIRROR_TRUST_MS?prior.authors||[]:[]),...want])].slice(0,MIRROR_MAX_AUTHORS);
      let res=null;try{res=await sendMirrorRequest(p,{op:'mirror',group,relays,authors});}catch(_){res=null;}
      if(accountPk()!==pk)return false;
      if(!res||!res.ok){mirrorRefused.set(key,Date.now());return false;}
      const next=mirrorState(pk);next.groups[group]={relays,authors,at:Date.now(),direct:parseReadDirectly(res.msg)};saveMirrorState(pk,next);mirrorRefused.delete(key);
      return want.every(a=>authors.includes(a));
    })().catch(()=>false).finally(()=>{mirrorBusy.delete(key);});
    mirrorBusy.set(key,job);return job;
  }
  /* EVERY JOINED COMMUNITY IS HANDED TO THIS NODE, NOT ONLY THE ONE BEING LOOKED AT. `ensureMirror` ran only
   * from a room's load, so a community was held by the node only once somebody had OPENED it on that device --
   * measured 2026-10-13: poster.place's mirror held ONE community while its members had joined many, and every
   * other room was read from its own relays by every device. This walks the saved list once per Concord start
   * (each community answers from its once-a-day freshness check after the first time), using the control
   * envelopes already cached for it, one at a time so a large list never becomes a burst of signatures. A
   * community with no cached control set yet has no stream keys to name and is skipped until it is opened. */
  let mirrorAllJob=null;
  function mirrorAllJoined(p){
    if(mirrorAllJob||!p||!homeRelayUrl(p))return mirrorAllJob;
    mirrorAllJob=(async()=>{let asked=0,held=0;
      for(const room of saved()){
        const bundle=room&&room.cord&&room.cord.bundle;if(!bundle||bundle.dissolved)continue;
        const loadKey=room.communityId||room.naddr;if(!loadKey)continue;
        const controls=await cachedEnvelopes(envelopeCacheKey(loadKey,'control'));
        asked++;try{if(await ensureMirror(p,room,bundle,controls))held++;}catch(_){}
      }
      return {asked,held};
    })().finally(()=>{setTimeout(()=>{mirrorAllJob=null;},300000);});
    return mirrorAllJob;
  }
  /* LEAVING STOPS THE MIRROR. Forgotten here first: even if the node never hears it, this device no longer
   * reads the room from it, and the node's own 14-day expiry ends the mirror on its own. */
  async function leaveMirror(p,room){
    const pk=accountPk(),bundle=room&&room.cord&&room.cord.bundle;if(!pk||!bundle)return false;
    const group=await mirrorGroup(bundle.community_id);if(!group)return false;
    const st=mirrorState(pk);if(!st.groups[group])return false;delete st.groups[group];saveMirrorState(pk,st);
    try{const res=await sendMirrorRequest(p,{op:'leave',group});return !!(res&&res.ok);}catch(_){return false;}
  }
  /* A wrap for a community this node holds goes HERE, and only to the community's own relays when this
   * node's relay did not accept it (its OK waits for the forward upstream, so "accepted" means people
   * elsewhere will see it). */
  async function roomPublish(relays,ev,auth,direct){
    const p=PC()||{},h=ev&&(ev.kind===1059||ev.kind===21059)?homeServes(p,relays,[{kinds:[ev.kind],authors:[ev.pubkey]}]):'';
    if(h){
      let res=null;try{res=await direct([h],ev,null);}catch(_){res=null;}
      if(res&&res.ok)return res;
      if((relays||[]).every(u=>normalizeRelay(u)===h))return res||{ok:false,msg:'this server’s relay did not accept it'};
    }
    return direct(relays,ev,auth);
  }
  /* The ONE door every room write goes through (app.js `relayPublishRoom`) asks this. */
  window.PCConcordMirror={publish:roomPublish,serves:(relays,filters)=>homeServes(PC()||{},relays,filters)};
  function cordControlStamp(wraps){return [...new Set((wraps||[]).map(ev=>ev&&ev.id).filter(Boolean))].sort();}
  function cordPlaneContext(p,bundle,controls=[],room=null){
    const owner=String(p&&p.viewer&&p.viewer().pubkey||''), identity=room&&roomIdentity(room),
      material=JSON.stringify(bundle),loadKey=room&&(room.communityId||room.naddr),
      generation=loadKey&&JSON.stringify(cordControlStamp(roomControls.get(loadKey)));
    const membershipCurrent=()=>{
      if(String(p&&p.viewer&&p.viewer().pubkey||'')!==owner)return false;
      if(!room)return true;
      const live=saved().find(r=>roomIdentity(r)===identity);
      return !!live&&JSON.stringify(live.cord&&live.cord.bundle)===material;
    };
    return {bundle,controls:controls||[],membershipCurrent,current:()=>membershipCurrent()&&
      (!generation||JSON.stringify(cordControlStamp(roomControls.get(loadKey)))===generation)};
  }
  function cordPlaneAuth(p,plane,author,relays){
    const reader=window.PosterCordReader;
    if(!plane||!reader||!reader.createPlaneAuth)return null;
    if(!plane.current())throw new Error('Concord membership changed during transport');
    const auth=reader.createPlaneAuth(plane.bundle,plane.controls,author,relays);
    return auth ? {...auth,current:plane.current} : null;
  }
  function cordPlaneSubscribe(p,R,relays,filters,options,plane){
    /* (typeof: harnesses run this function sliced out of the file, without the mirror helpers.) */
    const h=typeof homeServes==='function'?homeServes(p,relays,filters):'';
    if(!h||!R||!R.subscribeFrom)return cordPlaneSubscribeDirect(p,R,relays,filters,options,plane);
    /* This node's relay first; the community's own relays only if it cannot carry the stream -- and, where it said
     * it cannot read some of them (homeDirect), those relays alongside it, never all of them. */
    const direct=typeof homeDirect==='function'?homeDirect(filters):null;
    let fallback=null,beside=null,stopped=false;
    const fall=()=>{if(stopped||fallback)return;try{if(beside){beside();beside=null;}fallback=cordPlaneSubscribeDirect(p,R,relays,filters,options,plane);}catch(e){console.warn('Concord subscription fallback failed',String(e&&e.message||e));}};
    const inner=R.subscribeFrom([h],filters,{...options,max:1,exact:true,...(direct?{subPrefix:HOME_PARTIAL}:{}),onClosed:(_u,why)=>{if(/^unavailable:/i.test(String(why||'')))fall();}});
    if(direct&&direct.length)try{beside=cordPlaneSubscribeDirect(p,R,direct,filters,options,plane);}catch(e){console.warn('Concord subscription to the relays this server cannot read failed',String(e&&e.message||e));}
    if(inner&&inner.ready&&inner.ready.then)inner.ready.then(ok=>{if(!ok)fall();});
    const stop=()=>{stopped=true;try{inner&&inner();}catch(_){}if(fallback)try{fallback();}catch(_){}if(beside)try{beside();}catch(_){}};
    stop.publish=event=>(inner&&inner.publish?inner.publish(event):0)+(fallback&&fallback.publish?fallback.publish(event):0)+(beside&&beside.publish?beside.publish(event):0);
    stop.hasTargets=true;stop.ready=inner&&inner.ready?inner.ready:Promise.resolve(true);
    return stop;
  }
  function cordPlaneSubscribeDirect(p,R,relays,filters,options,plane){
    const subscriptions=[],relayCap=Math.max(1,Math.min(4,Number(options.max)||4)),planeLimit=Math.floor(8/relayCap);
    // At most eight live sockets. Reader streams are current-first; archived epochs beyond
    // this budget remain covered by the bounded sequential history refresh.
    /* ONE UNREADABLE AUTHOR MUST NOT SILENCE THE ROOM — the live half of the same rule as cordQuery.
     *
     * `cordPlaneAuth` throws for an author whose plane key this membership does not hold, and that
     * throw used to tear down EVERY subscription already made and propagate. So a partially granted
     * membership got no live stream at all: history would load and then nothing new ever arrived,
     * which reads as "I have to switch rooms to see new messages" — switching rooms rebuilds the
     * subscription and briefly works, so it looks like a refresh bug rather than an access one.
     *
     * Skip the authors we cannot authenticate as, keep the streams we can, and only fail when
     * nothing at all could be subscribed. */
    let firstFailure=null;
    try{for(const filter of filters)for(const author of [...new Set(filter.authors||[])]){
      if(subscriptions.length>=planeLimit)break;
      let authScope;
      try{ authScope=cordPlaneAuth(p,plane,author,relays); }
      catch(err){ firstFailure=firstFailure||err; continue; }
      subscriptions.push(R.subscribeFrom(relays,[{...filter,authors:[author]}],{...options,max:relayCap,authScope}));
    }}catch(e){subscriptions.forEach(stop=>stop());throw e;}
    if(!subscriptions.length&&firstFailure){throw firstFailure;}
    const stop=()=>subscriptions.forEach(close=>close());
    stop.publish=event=>subscriptions.reduce((n,sub)=>n+(sub.publish?sub.publish(event):0),0);
    stop.hasTargets=subscriptions.some(sub=>sub.hasTargets);
    stop.ready=Promise.all(subscriptions.map(sub=>sub.ready)).then(results=>results.every(Boolean));
    return stop;
  }
  async function cordQuery(p,relays,filters,{timeout=8000,max=8,signal=null,purpose='concord room',minInterval=0,allowBlocked=true,failureCooldown=1800000,plane=null,report=null,urgent=false,home=true}={}){
    /* THIS NODE'S RELAY FIRST (see homeServes). It answering -- EOSE, even an empty one -- is the answer;
     * a rate-limited skip is the old rate limit; anything else is "could not ask" and the read goes on
     * to the community's own relays below, unchanged. */
    const h=home!==false&&p.relayQueryFrom&&typeof homeServes==='function'?homeServes(p,relays,filters):'';
    if(h){
      const reach={},direct=typeof homeDirect==='function'?homeDirect(filters):null;let got=null;
      try{got=await p.relayQueryFrom([h],filters,{timeout,max:1,signal,purpose:purpose+' (this server)',minInterval,allowBlocked:true,failureCooldown:30000,exact:true,report:reach,urgent:true,...(direct?{subPrefix:HOME_PARTIAL}:{})});}catch(_){got=null;}
      const note=(bucket,u)=>{if(report)(report[bucket]||(report[bucket]=[])).push(u);};
      if((reach.ok||[]).includes(h)){note('asked',h);note('ok',h);
        /* The other half: the relays this node said it cannot read -- only those, through the tunnel. Not
         * reaching them leaves the node's answer standing; it is never thrown away for a relay that refused. */
        if(direct&&direct.length){
          let extra=[];try{extra=await cordQuery(p,direct,filters,{timeout,max,signal,purpose,minInterval,allowBlocked,failureCooldown,plane,report:null,urgent,home:false});}catch(_){extra=[];}
          if(extra.length){const byId=new Map();for(const ev of [...(got||[]),...extra])if(ev&&ev.id)byId.set(ev.id,ev);got=[...byId.values()];}
        }
        cordQuery.lastUnreadablePlanes=[];return got||[];}
      if((reach.held||[]).includes(h)&&!(reach.failed||[]).length&&!(reach.unavailable||[]).length){note('held',h);return [];}
      if(signal&&signal.aborted)return [];
    }
    /* queryFrom intentionally skips relays already owned by the shared pool. Always ask both paths:
       otherwise opening a room can silently omit the newest wraps from whichever relay is connected. */
    const jobs=[];
    if(plane&&window.PosterCordReader&&window.PosterCordReader.createPlaneAuth){
      if(!p.relayQueryFrom)throw new Error('Concord plane transport unavailable');
      const byId=new Map();
      // Authors this membership cannot authenticate as; reported, never silently treated as empty.
      const unreadablePlanes=new Set();
      // One author per socket: relays may require every queried author to authenticate.
      // Sequential planes also bound fan-out to the explicit relay cap.
      /* ONE UNREADABLE AUTHOR MUST NOT BLANK THE ROOM.
       *
       * `cordPlaneAuth` throws when this membership does not hold that author's plane key, and it
       * was called inside this loop with nothing catching it — so a single un-held stream key
       * aborted the WHOLE history: every other author, and all six pagination pages above. The
       * room then rendered empty with nothing saying why, which is exactly what a partially
       * granted membership looks like from the outside.
       *
       * The generic branch below has always been tolerant (Promise.allSettled, throwing only when
       * EVERY job failed). This makes the plane branch match that contract: read what we are
       * entitled to read, remember what we could not, and only fail when nothing succeeded. */
      for(const filter of filters){
        const page=new Map();
        const authors=[...new Set(filter.authors||[])];
        let firstFailure=null,succeeded=0;
        for(const author of authors){
          let authScope;
          try{ authScope=cordPlaneAuth(p,plane,author,relays); }
          catch(err){ firstFailure=firstFailure||err; unreadablePlanes.add(author);
            /* A stream we hold no key for is a stream nothing was read from — it must not be
             * indistinguishable from one that answered with nothing. */
            if(report)(report.unheld||(report.unheld=[])).push(author);
            continue; }
          let events;
          try{
            events=await p.relayQueryFrom(relays,[{...filter,authors:[author]}],
              {timeout,max,signal,purpose,minInterval,allowBlocked,failureCooldown,exact:true,authScope,report,urgent});
          }catch(err){ firstFailure=firstFailure||err; continue; }
          succeeded++;
          if(!plane.current())return [];
          for(const ev of events||[])if(ev&&ev.id)page.set(ev.id,ev);
        }
        if(authors.length&&!succeeded&&firstFailure)throw firstFailure;
        // Preserve the combined filter's global limit. Otherwise an ancient epoch's
        // first page can move the shared cursor past an unread newer epoch's tail.
        const ordered=[...page.values()].sort((a,b)=>Number(b.created_at)-Number(a.created_at)||String(b.id).localeCompare(String(a.id))),
          limit=Number.isSafeInteger(filter.limit)&&filter.limit>0?filter.limit:ordered.length;
        for(const ev of ordered.slice(0,limit))byId.set(ev.id,ev);
      }
      if(unreadablePlanes.size)cordQuery.lastUnreadablePlanes=[...unreadablePlanes];
      else cordQuery.lastUnreadablePlanes=[];
      return [...byId.values()];
    }
    if(p.relayQuery)jobs.push(Promise.resolve().then(()=>p.relayQuery(filters,timeout)));
    if(p.relayQueryFrom)jobs.push(Promise.resolve().then(()=>p.relayQueryFrom(relays,filters,{timeout,max,signal,purpose,minInterval,allowBlocked,failureCooldown,report,urgent})));
    const settled=await Promise.allSettled(jobs),ok=settled.filter(result=>result.status==='fulfilled');
    if(jobs.length&&!ok.length)throw settled[0].reason;
    const batches=ok.map(result=>result.value||[]),byId=new Map();
    for(const ev of batches.flat())if(ev&&ev.id)byId.set(ev.id,ev);
    return [...byId.values()];
  }
  /* Tuple encoding prevents room/channel boundary collisions (`a:b`+`c` versus `a`+`b:c`) and
   * lets cache cleanup compare the room identity exactly instead of prefix-deleting another room. */
  function envelopeCacheKey(loadKey,stream){return JSON.stringify([String(loadKey||''),String(stream||'control')]);}
  function mergeEnvelopes(...groups){const byId=new Map();for(const ev of groups.flat())if(ev&&ev.id)byId.set(ev.id,ev);return [...byId.values()];}
  async function cachedEnvelopes(key){try{return window.PCConcordCache?await window.PCConcordCache.get(key):[];}catch(e){console.warn('Concord cache read failed',e);return [];}}
  async function cachedEnvelopePage(key,limit=300){try{if(!window.PCConcordCache)return[];if(window.PCConcordCache.page){const got=await window.PCConcordCache.page(key,{limit});return got&&Array.isArray(got.events)?got.events:[];}return await window.PCConcordCache.get(key);}catch(e){console.warn('Concord cache read failed',e);return[];}}
  /* The community's Keep-history policy rides every write, read off the saved room (never a fresh fold per
   * write). The control plane is never trimmed whatever it says (CORD-08 §1: control/guestbook never expire). */
  function cacheKeepFor(key){
    try{const [loadKey,stream]=JSON.parse(key);if(stream==='control'||String(stream).startsWith('guestbook:'))return {plane:'control'};
      const room=saved().find(r=>(r.communityId||r.naddr)===loadKey);return {keepDays:room?retentionDays(room.history_retention):null};}catch(_){return {};}
  }
  /* Kept history is never trimmed to make room, so when it gets large the person is TOLD, once a session,
   * instead (concord-cache.js KEPT_WARN_BYTES). */
  let keptWarned=false;
  try{window.addEventListener('pc-concord-storage',e=>{if(keptWarned)return;keptWarned=true;const mb=Math.round(((e.detail&&e.detail.keptBytes)||0)/1048576);
    try{const p=PC();if(p&&p.toast)p.toast('Communities that keep their history are using '+mb+' MB on this device. A community’s Keep history setting can shorten it.');}catch(_){}});}catch(_){}
  async function cacheEnvelopes(key,events){try{if(window.PCConcordCache&&events&&events.length)await window.PCConcordCache.put(key,events,cacheKeepFor(key));}catch(e){console.warn('Concord cache write failed',e);}}
  async function queryEnvelopeHistory(p,relays,authors,cached=[],queryOptions={}){
    let all=mergeEnvelopes(cached),until=null;
    for(let page=0;page<6&&(page===0||all.length<5000);page++){
      const filter={kinds:[1059],authors,limit:1000};if(until!=null)filter.until=until;
      /* minInterval gates logical background REFRESHES, not pagination inside one refresh. Only
         page zero consults/arms it; later pages must still reach older history. */
      const batch=await cordQuery(p,relays,[filter],{timeout:10000,max:8,...queryOptions,minInterval:page===0?Number(queryOptions.minInterval||0):0});
      const merged=mergeEnvelopes(all,batch),oldest=batch.reduce((n,event)=>Math.min(n,Number(event.created_at)||n),Infinity);
      all=merged;if(batch.length<1000||!Number.isFinite(oldest)||oldest<=0||until===oldest-1)break;until=oldest-1;
    }
    return all.sort((a,b)=>(Number(a.created_at)||0)-(Number(b.created_at)||0)).slice(-5000);
  }
  /* A ROOM THAT HOLDS NOTHING MUST ACTUALLY ASK — "no channels readable yet — 8 relay(s) asked, 0
   * control event(s) held, 0 new", reported from the app over a community the bot (a member) read
   * fine, was a room that asked NOBODY.
   *
   * `queryFrom` skips a relay, silently and with `[]`, while another read of the same slot is in
   * flight there, and for thirty minutes after it failed once. A member's split-control plane has
   * no AUTH key, so its slot is the bare relay URL -- shared with every other community on the same
   * relays, which for Armada/Vector rooms is all of them, and they are swept in the background. So
   * opening a room while another room's read was in flight, or within half an hour of one bad
   * moment on a phone's radio, fetched nothing and blamed eight relays that were never asked. The
   * on-device control cache hides it everywhere it exists, which is why a fresh install found it.
   *
   * While nothing at all is held the read is `urgent`: neither rule decides (its own socket, no
   * waiting -- waiting here holds the live subscription back). With something held this is exactly
   * the old read. The `reach` it returns is what was MEASURED, so the message can say which of the
   * three things happened. tests/client/test_concord_unasked_relays.py */
  async function queryControlUntilAsked(p,relays,authors,known,options={}){
    const reach={},wraps=await queryEnvelopeHistory(p,relays,authors,known,{...options,report:reach,urgent:!(known||[]).length});
    const ok=[...new Set(reach.ok||[])],failed=[...new Set(reach.failed||[])].filter(u=>!ok.includes(u)),
      spoke=new Set([...ok,...failed]),unasked=[...new Set([...(reach.held||[]),...(reach.cooled||[])])].filter(u=>!spoke.has(u));
    return {wraps,reach:{ok,failed,unasked,unheld:[...new Set(reach.unheld||[])]}};
  }
  /* SAY WHICH OF THE THREE IT WAS, AND WHAT TO DO ABOUT IT. Relays that answered with nothing this
   * membership can open (keys rotated / a stale invite), relays that did not answer (the network),
   * and relays that could not be asked yet (busy, resting) are three problems with three fixes. */
  function unreadableControlMessage(reach,relays,held,fresh){
    const n=[...new Set(relays||[])].length,host=u=>{try{return new URL(u).host;}catch(_){return String(u);}};
    if(reach.unheld.length&&!reach.ok.length)
      return 'no channels readable yet — this membership holds no key for the community\'s channel list. Ask a member for a fresh invite link and open it again';
    if(reach.ok.length)
      return `no channels readable yet — ${reach.ok.length} of ${n} relay(s) answered, but nothing they hold opens with your membership (${held} control event(s) held, ${fresh} new). If this lasts the community's keys were probably rotated: ask a member for a fresh invite link`;
    if(reach.failed.length&&!reach.unasked.length)
      return `no channels readable yet — none of this community's ${n} relay(s) answered (${reach.failed.slice(0,3).map(host).join(', ')}${reach.failed.length>3?'…':''}). Check your connection; the room keeps retrying on its own`;
    return `no channels readable yet — ${reach.unasked.length||n} of ${n} relay(s) could not be asked yet${reach.failed.length?' and '+reach.failed.length+' did not answer':''}. The room keeps retrying on its own; if it lasts, check your connection`;
  }
  /* `thread` is the id of the root whose conversation is open, or null for the channel.
   * Cleared on every room/channel change — a thread belongs to one channel. */
  const state={ community:null, channel:null, thread:null };
  let replyTarget=null, reactionTarget=null, mobileChatOpen=false, mobileDrawerOpen=false, discoveryOpen=false;
  let discovered=[], discoveryStarted=false, discoverySubscription=null, discoveryAbortController=null, discoveryLoaded=false, discoveryUnreachable='', membershipBusy=false, membershipRetryTimer=null;
  let discoveryPaintPending=false;
  let membershipViewer='';const membershipDocs=new Map();
  /* 95 MB, under this node's Blossom ceiling (blossom_max_upload_mb, 100) and the edge's body limit. It was 20 MB,
   * which refused a 20 MB webxdc game (Cat Tail Doom) that every server on the path would have taken. */
  const CONCORD_UPLOAD_MAX=95*1024*1024;
  let nip29Busy=false,nip29RetryTimer=null,webxdcHydrationEpoch=0;
  let roomReadIdentity='',roomReadAbortController=null;
  function ownRoomReads(identity){
    identity=String(identity||'');if(identity===roomReadIdentity&&roomReadAbortController)return roomReadAbortController.signal;
    if(roomReadAbortController)roomReadAbortController.abort();roomReadIdentity=identity;
    roomReadAbortController=identity&&typeof AbortController==='function'?new AbortController():null;
    return roomReadAbortController&&roomReadAbortController.signal;
  }
  const discoveryIconLoads=new Set();
  const recoveredOwnedInvites=new Set();
  const roomLoads=new Map();
  /* `room.cord.hydrated` is durable membership metadata, not renderer state. Treating it as a
   * process-local cache flag made a fresh browser/Desktop process skip IndexedDB restoration and
   * show an empty room until the live poll happened to run. */
  const hydratedRoomViews=new Set();
  const roomLoadNotices=new Map();
  const roomControls=new Map();
  /* WHETHER A CHANNEL WAS ACTUALLY READ, PER CHANNEL — because "no messages" is a CLAIM.
   *
   * A channel with nothing in it and a channel whose relays refused us are the same empty array,
   * and the empty state said "This is the start of this encrypted channel" for both. Measured on
   * two joined Vector rooms whose relay set includes `wss://asia.vectorapp.io/nostr`: that relay
   * answers every kind-1059 filter with `auth-required` and then rejects the AUTH itself with
   * "relay needs serviceUrl to be configured before AUTH can work", so its gift wraps are
   * unreadable to any client — and after the first failure it is COOLED DOWN for half an hour, at
   * which point `queryFrom` skips it and returns `[]` with no warning at all. The room then
   * declares itself brand new, twice over.
   *
   * Keyed by `channelStoreId`, so it survives a room switch and belongs to the channel it
   * describes rather than to whatever is on screen. */
  const channelReach=new Map();
  function noteChannelReach(storeId,report,relays){
    if(!storeId)return;
    const seen=b=>[...new Set((report&&report[b])||[])];
    const ok=seen('ok'),failed=seen('failed').filter(u=>!ok.includes(u)),
      cooled=seen('cooled').filter(u=>!ok.includes(u)&&!failed.includes(u)),unheld=seen('unheld');
    /* A PASS THAT ASKED NOBODY TEACHES NOTHING. The live tick is rate limited per relay per
     * minute, so most ticks reach no socket at all; recording that would erase what the last real
     * read measured and make a healthy channel look unreachable every other second. */
    if(!ok.length&&!failed.length&&!cooled.length&&!unheld.length&&channelReach.has(storeId))return;
    channelReach.set(storeId,{ok,failed,cooled,unheld,total:[...new Set(relays||[])].length,at:Date.now()});
  }
  /* An unread channel reports nothing rather than guessing: no record is "not asked yet", which is
   * a spinner's business, not the empty state's. */
  function channelUnread(room,name){
    const reach=channelReach.get(channelStoreId(room,name));
    if(!reach)return null;
    const missed=[...reach.failed,...reach.cooled];
    return missed.length||reach.unheld.length?{...reach,missed}:null;
  }
  /* Blob URLs die with their renderer. Keep encrypted icon pointer identity in memory so a saved
   * room never suppresses re-decryption after the next browser/native-shell launch. */
  /* Durable storage keeps the encrypted pointer; only this renderer keeps its decrypted blob URL.
   * A blob: URL written to localStorage is guaranteed dead after reload. */
  const roomIconRefs=new Map(),storedIconLoads=new Set(),failedIconRefs=new Map();
  const pendingAttachments=new Map();
  /* A relay poll, profile/icon hydration or decrypted attachment can repaint the whole Concord
   * workspace while somebody is typing. The textarea is part of that workspace, so the browser
   * cannot preserve it for us. Keep a per-room/channel draft outside the DOM and restore the exact
   * selection after every repaint. This deliberately stays in renderer memory: drafts are private
   * plaintext and must not be written to localStorage. */
  const composerDrafts=new Map(),sendingDrafts=new Set();
  /* Relay messages, profiles, icons and decrypted attachments may all finish while the on-screen
   * keyboard owns a control inside Concord. Replacing #feed at that moment destroys the real DOM
   * textarea; restoring its value/focus later still closes and reopens Android's keyboard. Defer
   * background paints until focus leaves the workspace. Data is already committed to the in-memory
   * stores, and one eventual paint coalesces every pending update. User actions still call render()
   * directly, so navigation and Send are immediate. */
  let backgroundRenderPending=false,backgroundFocusHost=null;
  /* COMMUNITY SETTINGS HAS FOUR WAYS OUT, AND A SAVE SAYS WHAT HAPPENED. Reported as "no way to leave
   * Community Settings": the only exit was a "Cancel" sitting in a row of five identically-styled
   * buttons (Leave community, New private channel, Rotate community keys…) at the bottom of a scrolling
   * card -- no ✕, Escape did nothing, and the Android back button walked the app's history out from
   * under it. Save was worse: it greyed itself out for as long as the relays took (a metadata read, a
   * publish, a notice per channel) and a refusal was a 3-second toast, so a slow or refused save read
   * as "Save does nothing and the sheet will not close". The state lives here, not in the DOM, because
   * render() rebuilds the sheet while a save is in flight. */
  let settingsSaving=false,settingsError='';
  const settingsDialog=()=>document.querySelector?.('#cc-settings-dialog')||null;
  function communitySettingsOpen(){const d=settingsDialog();return !!(d&&!d.classList.contains('hidden'));}
  function closeCommunitySettings(){
    const d=settingsDialog();if(!d||d.classList.contains('hidden'))return false;
    d.classList.add('hidden');settingsError='';
    const err=document.querySelector('#cc-settings-error');if(err){err.textContent='';err.hidden=true;}
    try{const opener=document.querySelector('#cc-edit-icon');if(opener)opener.focus({preventScroll:true});}catch(_){}
    if(backgroundRenderPending)backgroundRender();
    return true;
  }
  try{document.addEventListener('keydown',e=>{
    if(e.key!=='Escape'||e.defaultPrevented||!communitySettingsOpen())return;
    /* Something stacked ABOVE the sheet owns Escape: a confirm, the key-rotation review, a lightbox, a menu. */
    if(document.body.classList.contains('modal-open')||document.querySelector('.uiconfirm,.uiconfirm-bg,.lightbox,.emoji-pop,.menu-pop,.cc-member-menu'))return;
    if([...document.querySelectorAll('body>.cc-join')].some(o=>!o.classList.contains('hidden')))return;
    e.preventDefault();e.stopPropagation();closeCommunitySettings();
  });}catch(_){}
  let activeMentionState={choices:[],index:0,recipients:new Map()};
  const attachmentCache=new Map(),attachmentLoads=new Map();
  /* AN IMAGE'S BOX IS KNOWN BEFORE ITS PIXELS ARE. Every repaint rebuilds a room's messages, and an
   * encrypted image used to come back as a one-line "Decrypting…" placeholder and grow to its real
   * height a moment later — even when it was already decrypted — so a room sitting at the bottom
   * shrank and re-grew on every message: "if a user or myself send message, the scroll bar goes up"
   * (measured: the rebuilt room came back 2,527 px short). The size comes from the imeta `dim` Vector
   * writes, else from the first time the image loaded here; a decrypted image is drawn at once. */
  const attachmentDims=new Map();
  function attachmentKey(file){ return file.url+'\0'+file.hash; }
  function attachmentSize(file){ const d=attachmentDims.get(attachmentKey(file)); return d||(file.w&&file.h?{w:file.w,h:file.h}:null); }
  /* The reserved box, as the stylesheet will lay it out: at most 560 wide and 480 tall (concord.css
   * caps both), the image's own shape. Carried as variables on a `cc-sized` wrapper because the
   * stylesheet forces `width:auto !important` on the image itself. */
  function attachmentBoxStyle(file){ const d=attachmentSize(file); if(!d)return '';
    const w=Math.max(1,Math.round(Math.min(d.w,560,480*d.w/d.h)));
    return ` style="--cc-w:${w}px;--cc-ar:${d.w}/${d.h}"`; }
  /* A DECRYPTED VIDEO IS DRAWN ONCE AND KEPT ("video in room Lounge causes chat position to keep
   * moving"). The hydrator used to rebuild every encrypted <video> after EVERY repaint -- and a live room
   * repaints constantly (messages, profiles, typing) -- so each time the player collapsed to nothing,
   * reloaded and grew back, shoving the chat around it. Now, like an image: the message paints the
   * player straight from the decrypted cache marked ready, the hydrator leaves a ready host alone, and
   * the video's size, once known, reserves its box so nothing around it moves. */
  function attachmentVideoHtml(p,file,got){ const url=p.enc(got.url),label=p.enc(got.name||'attachment'),box=attachmentBoxStyle(file);
    return `<div class="cc-attachment-media"${box?' data-cc-sized':''}${box}><video src="${url}" controls playsinline preload="metadata" title="Double-click to expand"></video><button class="cc-attachment-expand" type="button" aria-label="Open ${label}">↗</button></div>`; }
  function attachmentImageHtml(p,file,got){ const url=p.enc(got.url),label=p.enc(got.name||'attachment'),box=attachmentBoxStyle(file);
    return `<button class="cc-attachment-open" type="button"${box?' data-cc-sized':''} aria-label="Open ${label}"${box}><img src="${url}" alt="${label}"></button>`; }
  const scrollStates=new Map();
  let liveTimer=null,liveBusy=false,metadataBusy=false,metadataCursor=0;
  let liveWarned='';   // the last live-sync failure reported — see refreshActiveChannel
  let resumeRequested=false;
  let actionDismissOff=null;
  /* THE COMMUNITIES ON THIS SCREEN BELONG TO THE ACCOUNT SIGNED IN, NOT TO THE DEVICE.
   *
   * Reported as "using Concord with @beatboxserenade shows groups that profile is not in... the groups
   * are those from [the other] profile": the joined-room list was ONE localStorage key for the whole
   * browser profile, and the account switcher reloads the page without touching it -- so account B
   * was drawn account A's rooms, swept them, and (via the vault backfill below) could publish them
   * into B's OWN membership vault. Every piece of per-account Concord state is now keyed on the
   * signed-in pubkey, the pattern cord-direct-invites.js and cord-invite-links.js already used: the
   * room list, the open room, read markers, the mention cursors and ledger, declined invites and
   * starred channels. No pubkey, no rooms -- a signed-out page owns nothing.
   *
   * The old shared list (`pc.concord.invites`) is FROZEN: never written and never deleted here. It can
   * hold the rooms of every account that used this device, so it is handed out room by room, only to
   * an account whose own evidence names the room -- see adoptLegacyRoom. */
  const LEGACY_ROOMS_KEY='pc.concord.invites',ROOMS_KEY='pc.concord.rooms.v1.',ACTIVE_KEY='pc.concord.active.v1.',
    ADOPTED_KEY='pc.concord.legacy-adopted.v1.';
  function accountPk(){ try{ const p=PC(); return String(p&&p.viewer&&p.viewer().pubkey||''); }catch(_){ return ''; } }
  function accountKey(base,pk=accountPk()){ return pk?base+pk:''; }
  function saved(){ const key=accountKey(ROOMS_KEY); if(!key)return []; try{ const v=cordJsonParse(localStorage.getItem(key)||'[]'); if(!Array.isArray(v))return []; const clean=uniqueRooms(v);if(clean.length!==v.length){preserveRoomSelection(v,clean);writeRooms(clean);}return clean; }catch(_){ return []; } }
  function writeRooms(v){const key=accountKey(ROOMS_KEY);if(!key)return;try{localStorage.setItem(key,JSON.stringify(v.slice(0,50),(k,value)=>k==='icon'&&/^blob:/i.test(String(value||''))?'':value));}catch(_){}}
  function activeGet(){ const key=accountKey(ACTIVE_KEY); try{ return key?localStorage.getItem(key):null; }catch(_){ return null; } }
  function activeSet(index){ const key=accountKey(ACTIVE_KEY); if(!key)return; try{ if(index==null)localStorage.removeItem(key); else localStorage.setItem(key,String(index)); }catch(_){} }
  /* THE PRE-SEPARATION LIST, READ-ONLY. */
  function legacyRooms(){ try{ const v=cordJsonParse(localStorage.getItem(LEGACY_ROOMS_KEY)||'[]'); return Array.isArray(v)?uniqueRooms(v.filter(r=>r&&typeof r==='object')):[]; }catch(_){ return []; } }
  function legacyAdopted(pk){ try{ const v=JSON.parse(localStorage.getItem(ADOPTED_KEY+pk)||'[]'); return Array.isArray(v)?v.map(String):[]; }catch(_){ return []; } }
  function noteLegacyAdopted(pk,room){
    const id=roomIdentity(room);if(!pk||!id)return;
    const rows=legacyAdopted(pk);if(rows.includes(id))return;
    try{ localStorage.setItem(ADOPTED_KEY+pk,JSON.stringify([...rows,id].slice(-256))); }catch(_){}
  }
  /* The legacy record for `ref`, if this account has not already been handed it once. ONCE, because
   * the legacy list never changes: a room adopted and then left would otherwise come straight back
   * from it on the next pass. */
  function legacyRoomFor(pk,ref){
    if(!pk||!ref)return null;
    const room=legacyRooms().find(r=>sameRoom(r,ref));
    if(!room||legacyAdopted(pk).includes(roomIdentity(room))||wasLocallyLeft(pk,room))return null;
    return room;
  }
  /* HAND ONE LEGACY ROOM TO ONE ACCOUNT -- called only where that account's own evidence names it
   * (its decoded membership vault, its kind-10009 group list, a room whose bundle names it as owner,
   * or the person choosing it in "Check my communities"). The room's per-room device state goes with
   * it: read markers and stars were written under the shared keys too. The mention cursor does NOT --
   * a cursor advanced while ANOTHER account read the room would hide this account's mentions, and a
   * mention shown twice is the safe failure. Returns the record to add, or null. */
  function adoptLegacyRoom(pk,ref){
    const room=legacyRoomFor(pk,ref);if(!room)return null;
    try{
      const prefixes=[],naddr=String(room.naddr||''),star=String(room.communityId||room.naddr||room.url||'');
      if(naddr)prefixes.push(['pc.concord.read.'+naddr,READ_KEY+pk+'.'+naddr]);
      if(star)prefixes.push(['pc.concord.star.'+star+':',STAR_KEY+pk+'.'+star+':']);
      const keys=[];for(let i=0;i<(localStorage.length||0);i++){const k=localStorage.key(i);if(k)keys.push(k);}
      for(const k of keys)for(const [from,to] of prefixes){
        if(k!==from&&!k.startsWith(from+':')&&!(from.endsWith(':')&&k.startsWith(from)))continue;
        const target=to+k.slice(from.length);
        if(localStorage.getItem(target)==null)localStorage.setItem(target,localStorage.getItem(k));
      }
    }catch(_){}
    return JSON.parse(JSON.stringify(room));
  }
  /* Saved FIRST, marked "handed over" only once the account's list really holds it: a write that did
   * not land must leave the room claimable, not adopted into nothing. */
  function saveAdopted(pk,rooms,added){
    if(!added.length)return 0;
    save(rooms);
    const now=saved();let kept=0;
    for(const room of added)if(now.some(r=>sameRoom(r,room))){noteLegacyAdopted(pk,room);kept++;}
    return kept;
  }
  /* Evidence that needs no network: a room whose own join bundle names this account as its owner. */
  function adoptOwnedLegacyRooms(pk){
    if(!pk)return false;
    const rooms=saved(),added=[];
    for(const room of legacyRooms()){
      const b=room&&room.cord&&room.cord.bundle,owner=String(b&&(b.owner||b.creator_npub)||'');
      if(owner!==pk||rooms.some(r=>sameRoom(r,room)))continue;
      const got=adoptLegacyRoom(pk,room);if(got){rooms.push(got);added.push(got);}
    }
    return saveAdopted(pk,rooms,added)>0;
  }
  /* Rooms the old shared list holds that nothing has attributed to this account. Never shown as
   * joined; listed in "Check my communities" so the person can say which are theirs. */
  function unclaimedLegacyRooms(pk){
    if(!pk)return [];
    const mine=saved(),adopted=legacyAdopted(pk);
    return legacyRooms().filter(r=>roomIdentity(r)&&!adopted.includes(roomIdentity(r))&&!mine.some(m=>sameRoom(m,r))&&!wasLocallyLeft(pk,r));
  }
  function claimLegacyRooms(pk,refs){
    const rooms=saved(),added=[];
    for(const ref of refs||[]){if(rooms.some(r=>sameRoom(r,ref)))continue;const got=adoptLegacyRoom(pk,ref);if(got){rooms.push(got);added.push(got);}}
    return saveAdopted(pk,rooms,added);
  }
  function save(v){const clean=uniqueRooms(v);preserveRoomSelection(v,clean);v.splice(0,v.length,...clean);writeRooms(v);}
  function preserveRoomSelection(before,after){
    const selected=state.community==null?null:before[state.community];
    if(selected){const index=after.findIndex(room=>sameRoom(room,selected));if(index>=0)state.community=index;}
    const remembered=Number(activeGet()||0),room=before[remembered];
    if(room){const index=after.findIndex(item=>sameRoom(item,room));if(index>=0)activeSet(String(index));}
  }
  function scrollKey(){ const room=state.community==null?null:saved()[state.community]; return `${deliveryOwner(PC())}:${room&&(room.communityId||room.naddr||room.url)||'home'}:${state.channel||'general'}`; }
  function composerKey(room,channel){return `${deliveryOwner(PC())}:${room&&(room.communityId||room.naddr||room.url)||'home'}:${channel||'general'}`;}
  /* THE COMPOSER ON SCREEN NOW, NEVER THE ONE bind() SAW.
   *
   * `render()` rewrites the whole workspace, so `#cc-input` is a NEW element after every repaint —
   * and this screen repaints constantly: a relay message, a profile, a room icon, a delivery status
   * update. Anything that captured the textarea and then AWAITED (a Blossom listing, a file upload)
   * comes back holding a DETACHED node, and `input.value += url` succeeds on it. No error, no
   * toast, nothing on screen: "attaching files from blossom in concord room did nothing".
   *
   * MEASURED, both ways: pick a file with no repaint in between and the URL lands; force one
   * repaint — which is the ordinary case in a live room, since browsing the drive takes seconds —
   * and it is silently lost. `captureComposer` and `clearComposer` already re-query for this exact
   * reason; this is the same lookup with a name. */
  let _cordPasteUpload = null;   // the CURRENT render's uploader — see the paste binding in bind()
  function liveComposer(){
    return (document.querySelector&&document.querySelector('#cc-input'))||(PC()&&PC().$&&PC().$('#cc-input'))||null;
  }
  function captureComposer(){
    const input=(document.querySelector&&document.querySelector('#cc-input'))||(PC()&&PC().$&&PC().$('#cc-input'));
    if(!input)return;
    const room=state.community==null?null:saved()[state.community],key=input.dataset&&input.dataset.ccDraftKey||composerKey(room,state.channel),start=Number.isFinite(input.selectionStart)?input.selectionStart:String(input.value||'').length,
      end=Number.isFinite(input.selectionEnd)?input.selectionEnd:start;
    const focused=document.activeElement===input;
    composerDrafts.set(key,{value:String(input.value||''),start,end,direction:input.selectionDirection||'none',
      focused,replyTarget,mentionChoices:[...(activeMentionState.choices||[])],
      mentionIndex:Number(activeMentionState.index)||0,mentionRecipients:new Map(activeMentionState.recipients||[])});
  }
  function clearComposer(key){const input=(document.querySelector&&document.querySelector('#cc-input'))||(PC()&&PC().$&&PC().$('#cc-input'));if(input){const room=state.community==null?null:saved()[state.community],liveKey=input.dataset&&input.dataset.ccDraftKey||composerKey(room,state.channel);if(liveKey===key){input.value='';try{input.setSelectionRange(0,0);}catch(_){}}}composerDrafts.delete(key);activeMentionState={choices:[],index:0,recipients:new Map()};replyTarget=null;}
  function cloneComposerDraft(draft){return draft?{...draft,mentionChoices:[...(draft.mentionChoices||[])],mentionRecipients:new Map(draft.mentionRecipients||[])}:null;}
  /* Sending is optimistic, so the composer must look sent immediately instead of displaying a
   * duplicate copy until signing/relay I/O finishes. Keep the submitted value off-DOM solely as a
   * rollback snapshot. A later render can therefore never resurrect it from composerDrafts. */
  function beginComposerSend(key){captureComposer();const submitted=cloneComposerDraft(composerDrafts.get(key));clearComposer(key);return submitted;}
  function restoreFailedComposer(key,submitted){
    if(!submitted)return false;
    captureComposer();
    const input=(document.querySelector&&document.querySelector('#cc-input'))||(PC()&&PC().$&&PC().$('#cc-input')),
      room=state.community==null?null:saved()[state.community],liveKey=input&&(input.dataset&&input.dataset.ccDraftKey||composerKey(room,state.channel)),
      current=composerDrafts.get(key),currentValue=liveKey===key&&input?String(input.value||''):String(current&&current.value||'');
    /* Never overwrite text written while the first message was in flight. */
    if(currentValue!=='')return false;
    composerDrafts.set(key,cloneComposerDraft(submitted));
    if(liveKey===key&&input){input.value=String(submitted.value||'');try{input.setSelectionRange(Number(submitted.start)||0,Number(submitted.end)||Number(submitted.start)||0,submitted.direction||'none');}catch(_){}}
    activeMentionState={choices:[...(submitted.mentionChoices||[])],index:Number(submitted.mentionIndex)||0,recipients:new Map(submitted.mentionRecipients||[])};
    replyTarget=submitted.replyTarget||null;
    return true;
  }
  function backgroundRender(){
    const p=PC(),feed=p&&p.$&&p.$('#feed'),active=document.activeElement,
      input=document.querySelector&&document.querySelector('#cc-input'),
      concordHost=input&&(input.closest&&input.closest('.cc-shell,.cc-layout,[data-view="concord"]')),
      inside=!!(active&&active!==document.body&&active!==document.documentElement&&active.isConnected!==false&&
        (active===input||(concordHost&&concordHost.contains&&concordHost.contains(active))||
          (feed&&feed.contains&&feed.contains(active))));
    const settingsOpen=document.querySelector?.('#cc-settings-dialog:not(.hidden)');
    if(inside||settingsOpen){
      backgroundRenderPending=true;
      /* The native mobile handoff can briefly detach or replace the classic #feed host while the
       * real textarea remains connected in Concord's managed window. Tying protection to #feed made
       * that exact state repaint the composer and close Android's keyboard. Follow the focused
       * control itself; focusout is the lifecycle signal we actually need and works in every host. */
      if(backgroundFocusHost!==active&&active.addEventListener){
        backgroundFocusHost=active;
        active.addEventListener('focusout',()=>{backgroundFocusHost=null;setTimeout(()=>{if(backgroundRenderPending)backgroundRender();},0);},{once:true,capture:true});
      }
      /* WHAT MUST SURVIVE IS THE COMPOSER, NOT THE WHOLE SCREEN. Deferring everything meant a
       * message that arrived while somebody was typing was decrypted, merged, saved — and not
       * drawn until they sent something or changed room. Paint the messages now; the deferred
       * full render still follows on focusout for the rail, the member list and the unread marks. */
      try{ patchMessageList(); }catch(e){ console.warn('Concord message repaint failed',e); }
      try{ patchServerRail(); }catch(e){ console.warn('Concord rail repaint failed',e); }
      return false;
    }
    backgroundRenderPending=false;backgroundFocusHost=null;render();return true;
  }
  /* THE COMMUNITY ICONS, REPAINTED IN PLACE. An encrypted icon decrypts after the rail was drawn and asks for a
   * background render -- which is deferred for as long as the cursor is in the message box, i.e. the whole time
   * somebody is chatting. So a community icon that had just been saved (and decrypted fine) went on showing its
   * two letters ("PO") until the user clicked out of the box. Only the icon inside each rail button is touched;
   * nothing near the composer moves. */
  function patchServerRail(){
    const p=PC(),rooms=saved();if(!p||!p.enc||!document.querySelectorAll)return;
    for(const btn of document.querySelectorAll('[data-cc-server]')){
      const i=Number(btn.dataset.ccServer),r=rooms[i];if(!r)continue;
      const html=roomIcon(p,r,i);if(btn.innerHTML!==html)btn.innerHTML=html;
    }
  }
  /* REPAINT THE MESSAGES WITHOUT REPLACING THE COMPOSER.
   *
   * Reported as "new concord room messages are only appearing after I send or switch rooms". The
   * cursor sitting in the message box is the ordinary state of somebody in a chat, and that is
   * exactly the state backgroundRender() refuses to paint in — so the live stream was invisible
   * for as long as you were typing, and both of those actions cured it only because both repaint.
   *
   * The refusal itself is right: render() rebuilds the whole workspace, a replaced textarea loses
   * focus, and on Android that closes the soft keyboard mid-sentence. So this replaces the ONE pane
   * the new message belongs to and leaves the composer, its text, its selection and the keyboard
   * untouched. It is not a second source of truth — the same messagesPaneHtml() render() uses. */
  function patchMessageList(){
    const p=PC();
    if(!p||typeof p.isView!=='function'||!p.isView('concord'))return false;
    /* The same ownership proof render() demands. On the windowed desktop #feed belongs to whichever
     * window is focused, so an unowned paint writes over the app somebody is actually looking at. */
    if(window.PCOS&&PCOS.isOn&&PCOS.isOn()&&
       (!PCOS.ownsFeedView||!PCOS.ownsFeedView('concord')))return false;
    if(state.community==null)return false;
    const current=saved()[state.community];
    if(!current)return false;
    const scroller=document.querySelector('.cc-messages');
    if(!scroller||scroller.isConnected===false)return false;
    /* Focus inside the pane itself — an open reaction picker, a poll button being pressed — is the
     * same loss this function exists to avoid, one level down. Leave it to the full render. */
    const active=document.activeElement;
    if(active&&scroller.contains&&scroller.contains(active))return false;
    const viewer=p.viewer?p.viewer():{},profile=viewer.profile||{},
      me=profile.display_name||profile.name||(profile.nip05&&p.niceNip05(profile.nip05))||
        (viewer.npub?viewer.npub.slice(0,12)+'\u2026':'You'),
      messages=(current.local||current.cord||current.protocol==='nip29')?paintedMessages(current):[];
    /* The same read rule render() applies. Without it a channel you are reading and typing into
     * grows an unread mark, because the only thing that clears it is the paint being skipped. */
    const narrow=!!(window.matchMedia&&window.matchMedia('(max-width:820px)').matches);
    if(conversationIsVisible(narrow,mobileChatOpen,mobileDrawerOpen))markRead(current,state.channel||'general');
    /* NOT innerHTML: this runs on every live tick while somebody is in the composer, and a rebuilt pane is
     * a new <video> for every clip in it -- the loading circle, every few seconds. */
    /* Where the reader was, written back IN THIS TASK, as render() does: the rAF restore below runs a
     * frame later, and until then a reader at the bottom sat one new message above it. */
    const was={top:scroller.scrollTop,atBottom:scroller.scrollHeight-scroller.clientHeight-scroller.scrollTop<=2};
    paintPane(scroller,messagesPaneHtml(p,messages,current,viewer,me));
    try{ const st=readScroll(scrollKey()); setProgrammaticScroll(scroller,st.pinned!==false||was.atBottom?scroller.scrollHeight:was.top); }catch(_){ }
    if(p.hydrateLinkCards)p.hydrateLinkCards(scroller);
    wireRoomMedia(p);
    hydrateEncryptedAttachments(messages);
    hydrateWebxdcCards(current);
    bindMessages();
    restoreChatScroll();
    return true;
  }
  function handoffState(){ if(pendingHandoff)return JSON.parse(JSON.stringify(pendingHandoff)); const room=state.community==null?null:saved()[state.community],key=scrollKey(),scroll=readScroll(key); return {room:room&&(room.communityId||room.naddr||room.url)||'',channel:state.channel||'general',mobileChatOpen:!!mobileChatOpen,mobileDrawerOpen:!!mobileDrawerOpen,scroll:{top:Number(scroll.top)||0,height:Number(scroll.height)||0,pinned:scroll.pinned!==false}}; }
  /* A handoff that lands before anybody is signed in (concord.js evaluates before the session is
   * restored) cannot be matched against a room list -- there is no account to own one yet. It is kept
   * as it came and applied on the first render that has an account; until then it is still the state
   * this window would hand on. */
  let pendingHandoff=null;
  function acceptHandoff(value){ if(!accountPk()){pendingHandoff=value&&typeof value==='object'?value:{};return;} pendingHandoff=null; const v=value&&typeof value==='object'?value:{},rooms=saved(),i=rooms.findIndex(room=>(room.communityId||room.naddr||room.url)===String(v.room||'')); state.community=i>=0?i:(rooms.length?Math.max(0,Math.min(Number(activeGet())||0,rooms.length-1)):null);state.channel=String(v.channel||'general').slice(0,80);mobileChatOpen=!!v.mobileChatOpen;mobileDrawerOpen=!!v.mobileDrawerOpen;if(state.community!=null&&v.scroll){const key=scrollKey(),st={top:Math.max(0,Number(v.scroll.top)||0),height:Math.max(0,Number(v.scroll.height)||0),pinned:v.scroll.pinned!==false};writeScroll(key,st);} }
  function readScroll(key){ if(scrollStates.has(key))return scrollStates.get(key); try{ const v=JSON.parse(sessionStorage.getItem('pc.concord.scroll.'+key)||'null'); if(v&&typeof v==='object')return v; }catch(_){} return {pinned:true}; }
  function writeScroll(key,st){ scrollStates.set(key,st); try{ sessionStorage.setItem('pc.concord.scroll.'+key,JSON.stringify({top:Number(st.top)||0,height:Number(st.height)||0,pinned:st.pinned!==false})); }catch(_){} }
  /* A PROGRAMMATIC SCROLL IS TOLD APART FROM A FINGER BY WHERE IT LANDED, NOT BY WHEN IT HAPPENED.
   *
   * `ccScrollRestore` used to be the whole test, and it is a TIME WINDOW: `onscroll` discarded every
   * scroll event between setting the flag and the next animation frame. A browser's frame is ~16ms
   * and a reader's flick is hundreds of events, so the ones that landed inside a restore were thrown
   * away — and `pinned` is computed from exactly those events. A picture finishing decryption above
   * the reader fires a resize, the resize restores the pin, the flick inside that frame is
   * swallowed, `pinned` stays true, and the next picture snaps them to the bottom. Reported as "my
   * position keeps getting reset when I scroll through a room history", and it repeats for as long
   * as media keeps landing, which in a busy room is the whole way up.
   *
   * Recording the position we asked for makes the question answerable: a scroll event still sitting
   * at that number is ours, anything else is the reader, and the reader always wins. Read back
   * rather than trusting `top` — the browser clamps it to the scrollable range. */
  function setProgrammaticScroll(box,top,done){ if(!box)return;box.dataset.ccScrollRestore='1';box.scrollTop=top;box.dataset.ccScrollAt=String(box.scrollTop);const later=window.requestAnimationFrame||((fn)=>setTimeout(fn,0));later(()=>{if(box.isConnected){delete box.dataset.ccScrollRestore;delete box.dataset.ccScrollAt;}if(done)done();}); }
  /* Is this scroll event the one `setProgrammaticScroll` just caused? Answering yes CONSUMES the
   * flag, so a reader who moves inside a restore is heard for the rest of that frame too. */
  function programmaticScrollEvent(box){
    if(!box||!box.dataset||!box.dataset.ccScrollRestore) return false;
    if(Math.abs((Number(box.scrollTop)||0)-(Number(box.dataset.ccScrollAt)||0))<=2) return true;
    delete box.dataset.ccScrollRestore; delete box.dataset.ccScrollAt;
    return false;
  }
  function scrollChatBottom(){ const key=scrollKey(),st=readScroll(key); st.pinned=true; writeScroll(key,st); const later=window.requestAnimationFrame||((fn)=>setTimeout(fn,0)); later(()=>{ const box=document.querySelector('.cc-messages'); if(box)setProgrammaticScroll(box,box.scrollHeight,()=>{st.top=box.scrollTop;st.height=box.scrollHeight;writeScroll(key,st);}); }); }
  /* Entering a room is different from preserving a room somebody is already reading. History,
   * decrypted attachments and link previews all grow the scroller asynchronously, so one rAF can
   * reach what was the bottom and still leave the person hundreds of pixels above the final bottom. */
  /* ENTERING IS ONE ACT, AND IT IS OVER THE MOMENT THE READER SCROLLS.
   *
   * `activateJoinedRoom` calls this TWICE by design — once beside render(), and again in its
   * `finally` once the room's history has loaded — because history, attachments and link previews
   * all arrive late and the first pin lands on a shorter room. On a real community that second call
   * is SECONDS after the first, by which time the reader may well have started reading backwards,
   * and it re-pinned them to the newest message without asking. Measured: read back 364px, let
   * hydration finish, and the phone is at the bottom again. That is "my position keeps getting
   * reset when I scroll through a room history".
   *
   * So a later entry for the SAME room defers to the reader: `pinned===false` is only ever written
   * by the scroll handler, i.e. by a finger. Changing rooms changes the key and pins normally. */
  function enterChatBottom(late){ const key=scrollKey();
    /* `late` is the SECOND call for one entry, and it must defer to a reader who has started
       reading. `pinned===false` is only ever written by the scroll handler, i.e. by a finger; a
       fresh entry (a channel tapped, a room joined) still lands on the newest message. */
    if(late&&enterChatBottom.key===key&&readScroll(key).pinned===false) return;
    const token=Date.now()+Math.random();enterChatBottom.token=token;enterChatBottom.key=key;const st=readScroll(key);st.pinned=true;st.top=Number.MAX_SAFE_INTEGER;writeScroll(key,st);for(const delay of [0,60,180,450,900,1600])setTimeout(()=>{if(enterChatBottom.token!==token||scrollKey()!==key||st.pinned===false)return;const box=document.querySelector('.cc-messages');if(!box)return;setProgrammaticScroll(box,box.scrollHeight,()=>{st.top=box.scrollTop;st.height=box.scrollHeight;writeScroll(key,st);});},delay); }
  function repaintScrollTop(pinned,top,scrollHeight){ return pinned!==false?scrollHeight:Math.max(0,Number(top)||0); }
  function preserveChatScroll(fn){
    const key=scrollKey(),old=document.querySelector('.cc-messages'),st=readScroll(key),
      top=old?old.scrollTop:Number(st.top)||0;
    /* Pixel offsets only preserve appended history. Delayed backfill inserts older rows above the
     * viewport, where restoring the same number silently changes which message the reader sees.
     * Keep the first visible message and its position inside the viewport as the stronger anchor. */
    const oldRows=old&&old.querySelectorAll?[...old.querySelectorAll('.cc-message[data-message-id]')]:[],
      anchor=st.pinned===false?oldRows.find(row=>(Number(row.offsetTop)||0)+(Number(row.offsetHeight)||0)>top):null,
      anchorId=anchor&&anchor.dataset&&anchor.dataset.messageId,
      anchorGap=anchor?(Number(anchor.offsetTop)||0)-top:0;
    fn();
    const later=window.requestAnimationFrame||((f)=>setTimeout(f,0));
    later(()=>{ const box=document.querySelector('.cc-messages'); if(box){
      let next=repaintScrollTop(st.pinned,top,box.scrollHeight);
      if(st.pinned===false&&anchorId&&box.querySelectorAll){
        const row=[...box.querySelectorAll('.cc-message[data-message-id]')]
          .find(el=>el.dataset&&el.dataset.messageId===anchorId);
        if(row)next=Math.max(0,(Number(row.offsetTop)||0)-anchorGap);
      }
      box.scrollTop=next;st.top=box.scrollTop;st.height=box.scrollHeight;writeScroll(key,st);
    } });
  }
  function restoreChatScroll(){ const key=scrollKey(),st=readScroll(key),later=window.requestAnimationFrame||((f)=>setTimeout(f,0)); later(()=>{ const box=document.querySelector('.cc-messages'); if(box)setProgrammaticScroll(box,st.pinned!==false?box.scrollHeight:Number(st.top)||0,()=>{st.top=box.scrollTop;st.height=box.scrollHeight;writeScroll(key,st);}); }); }
  function viewportAnchor(scroller){
    if(!scroller||!scroller.querySelectorAll)return null;
    const top=Number(scroller.scrollTop)||0,row=[...scroller.querySelectorAll('.cc-message[data-message-id]')]
      .find(el=>(Number(el.offsetTop)||0)+(Number(el.offsetHeight)||0)>top);
    return row&&row.dataset&&row.dataset.messageId?{id:row.dataset.messageId,gap:(Number(row.offsetTop)||0)-top}:null;
  }
  /* IS THE READER'S HAND ON THIS SCROLLER RIGHT NOW?
   *
   * Writing `scrollTop` during a gesture CANCELS it. A fling that was going to carry four hundred
   * pixels further up stops dead where the write landed, so the reader ends up somewhere they did
   * not choose — and a room full of decrypting pictures fires a resize every few hundred
   * milliseconds, which is "it constantly jerks me to different positions". The restore is usually
   * computing the right number; writing it at the wrong moment is the whole problem. A correction
   * therefore waits for the hand to leave.
   *
   * A `scroll` event cannot START a gesture — our own writes fire one, and that would latch this
   * on for ever. It only EXTENDS one that a real input device began, which is how momentum past
   * the end of the finger keeps counting. */
  let chatGestureUntil=0;
  function chatHandOn(){ return Date.now()<chatGestureUntil; }
  function scrollGesture(scroller){
    const begin=()=>{ chatGestureUntil=Date.now()+220; };
    const extend=()=>{ if(chatHandOn()) begin(); };
    const on=(type,fn)=>{ try{ if(scroller.addEventListener)scroller.addEventListener(type,fn,{passive:true}); }catch(_){ } };
    on('pointerdown',begin); on('touchstart',begin); on('touchmove',begin);
    on('wheel',begin); on('keydown',begin); on('scroll',extend);
    return chatHandOn;
  }
  /* A REPAINT MID-FLICK IS A JERK NO RESTORE CAN UNDO.
   *
   * `preserveChatScroll` REPLACES the rows, and replacing the content of a scroller kills a
   * momentum scroll outright — the restore afterwards puts the right pixel back, but the fling that
   * was going to carry the reader another few hundred pixels is gone. A busy room repaints on every
   * arriving message, so scrolling back through history in one is a series of dead flings: "it
   * constantly jerks me to different positions".
   *
   * So the repaint waits for the hand to leave. Only the PAINT waits — whatever the caller needed
   * to persist has already happened by the time it gets here — and repeated calls coalesce into
   * one, because five messages arriving during a flick are still one repaint. */
  /* AND A DEFERRED PAINT IS CHECKED TWICE. Waiting for the hand means the request and the paint no
   * longer happen in the same instant, so "Concord is on screen" can stop being true in between - a
   * held repaint would then draw a room over whatever the reader navigated to. Callers re-test
   * their own precondition inside the callback; this helper deliberately does not guess what it is. */
  let chatRepaintHold=null;
  function whenHandLeaves(fn){
    if(!chatHandOn()){ fn(); return; }
    if(chatRepaintHold)clearTimeout(chatRepaintHold);
    chatRepaintHold=setTimeout(()=>{ chatRepaintHold=null; whenHandLeaves(fn); },240);
  }
  function watchPinnedRoomGrowth(scroller){
    if(!scroller||typeof ResizeObserver==='undefined')return;
    const key=scrollKey(),content=scroller.querySelector('.cc-message-list')||scroller;
    /* THE ANCHOR STARTS UNKNOWN, AND THAT IS THE WHOLE FIX FOR "IT JUMPS TO THE TOP".
     *
     * This runs as part of the paint, which is BEFORE the restore has put the reader back — a
     * freshly painted pane is at scrollTop 0, so asking it what the reader is looking at answers
     * "the oldest message in the room". The first ResizeObserver callback then faithfully restores
     * them to it. Measured on a 390px viewport with a real touch fling: read back 370px, let a
     * message arrive, and `preserveChatScroll` computes the right pixel (16669) and writes it — and
     * a few frames later this watcher moves it to 0, because it believed the reader was reading
     * message-0. That is the report, exactly: "i enter room, scroll up a little, and it jumps to
     * the top".
     *
     * `null` means "I do not know where they are", and `wanted()` already answers null to that by
     * leaving the scroller alone — which is right, since `preserveChatScroll` has just done the
     * anchoring for this paint. The anchor becomes known again the moment the scroller MOVES, which
     * includes our own restore landing. */
    let anchor=null;
    const remember=()=>{anchor=viewportAnchor(scroller);};
    const handOn=scrollGesture(scroller);
    /* Remember on EVERY scroll, ours included. The position a restore just wrote is the reader's
     * place by definition, and skipping it was what left the anchor stuck at whatever the pane held
     * when it was wired. Consuming the programmatic flag is the other listener's job (it is
     * registered first, on `onscroll`), so this one must not race it for that decision. */
    if(scroller.addEventListener)scroller.addEventListener('scroll',remember,{passive:true});
    let waiting=null;
    /* Where this scroller SHOULD be, given what the reader was looking at. `null` means "leave it
     * alone" — which is most of the time, because growth below an unpinned reader moves nothing
     * they can see. */
    const wanted=(st)=>{
      if(st.pinned!==false) return scroller.scrollHeight;
      /* Decrypted images and link cards can gain height above an unpinned reader long after
       * render. A fixed pixel offset would replace the message in view, so restore the last
       * visible row and its viewport gap. Growth below it naturally produces the same offset. */
      const row=anchor&&[...scroller.querySelectorAll('.cc-message[data-message-id]')]
        .find(el=>el.dataset&&el.dataset.messageId===anchor.id);
      return row?Math.max(0,(Number(row.offsetTop)||0)-anchor.gap):null;
    };
    const correct=()=>{
      const st=readScroll(key);
      if(!scroller.isConnected||scrollKey()!==key) return;
      /* THE HAND WINS. Come back when it has left rather than fighting it for the scroller. */
      if(handOn()){ hold(); return; }
      const want=wanted(st);
      /* WHERE THE WRITE WOULD ACTUALLY LAND. The pinned branch asks for `scrollHeight`, which is
       * past the end — a browser clamps it to `scrollHeight - clientHeight` — so comparing the
       * REQUEST against the current position would report a difference on every single resize and
       * write every time. Compare the landing, write the request. */
      const max=Math.max(0,(Number(scroller.scrollHeight)||0)-(Number(scroller.clientHeight)||0));
      const landing=want==null?null:Math.max(0,Math.min(want,max));
      /* A NO-OP WRITE IS NOT FREE. Assigning the value it already holds still cancels a momentum
       * scroll, so "nothing to correct" has to mean writing nothing at all. */
      if(landing==null||Math.abs((Number(scroller.scrollTop)||0)-landing)<=1){
        st.top=scroller.scrollTop;st.height=scroller.scrollHeight;writeScroll(key,st);remember();
        return;
      }
      setProgrammaticScroll(scroller,want,()=>{st.top=scroller.scrollTop;st.height=scroller.scrollHeight;writeScroll(key,st);remember();});
    };
    function hold(){ if(waiting)clearTimeout(waiting); waiting=setTimeout(()=>{waiting=null;correct();},240); }
    const observer=new ResizeObserver(()=>{
      if(!scroller.isConnected){observer.disconnect();if(waiting){clearTimeout(waiting);waiting=null;}return;}
      correct();
    });
    observer.observe(content);
  }
  function removeMessageRow(id){ const box=document.querySelector('.cc-messages'),row=[...document.querySelectorAll('.cc-message[data-message-id]')].find(el=>el.dataset.messageId===id); if(!box||!row)return false; const key=scrollKey(),st=readScroll(key),top=box.scrollTop,height=box.scrollHeight,above=(Number(row.offsetTop)||0)+(Number(row.offsetHeight)||0)<=top; row.remove(); const later=window.requestAnimationFrame||((f)=>setTimeout(f,0)); later(()=>{ if(!box.isConnected)return; const lost=Math.max(0,height-box.scrollHeight); box.scrollTop=st.pinned!==false?box.scrollHeight:(above?Math.max(0,top-lost):top);st.top=box.scrollTop;st.height=box.scrollHeight;writeScroll(key,st); }); return true; }
  function roomName(r,i){ return (r&&r.name)||`Encrypted community ${i+1}`; }
  function roomIdentity(room){ return String(room&&(room.communityId||room.naddr||room.url)||''); }
  function communityKey(room){const id=String(room&&room.communityId||''),bundleId=String(room&&room.cord&&room.cord.bundle&&room.cord.bundle.community_id||'');return !id||/^naddr1/i.test(id)?bundleId||id:id;}
  function inviteKey(room){
    const parts=inviteParts(room&&room.url);if(!parts)return '';
    // Relay hints and the hosting domain are routing, not identity. Keep the secret in the
    // comparison: one signer can publish separate encrypted bundles at the same address.
    let address=parts.naddr.toLowerCase(),secret=parts.secret;
    try{
      const parsed=window.PosterCord&&window.PosterCord.inviteDetails(parts.url);
      if(parsed&&parsed.linkSigner){
        address=parsed.linkSigner;
        // inviteDetails validates the complete CORD-05 fragment. Version 4 ends in its
        // 16-byte token; preceding bytes contain relay hints which can change on repost.
        // Retain opaque secrets for unknown formats instead of guessing their token layout.
        if(secret.length<=4096){const bytes=atob(secret.replace(/-/g,'+').replace(/_/g,'/'));
          if(bytes.charCodeAt(0)===4&&bytes.length>=18)secret=Array.from(bytes.slice(-16),c=>c.charCodeAt(0).toString(16).padStart(2,'0')).join('');}
      }
    }catch(_){}
    return address+'#'+secret;
  }
  function roomInviteKeys(room){return [room,...(room&&room.inviteAliases||[]).map(url=>({url}))].map(inviteKey).filter(Boolean);}
  function sameRoom(a,b){
    if(!a||!b)return false;
    const ac=communityKey(a),bc=communityKey(b);
    if(ac&&bc){if(ac===bc)return true;if(!/^naddr1/i.test(ac)&&!/^naddr1/i.test(bc))return false;}
    const ak=roomInviteKeys(a),bk=roomInviteKeys(b);
    if(ak.length&&bk.length)return ak.some(key=>bk.includes(key));
    return !!roomIdentity(a)&&roomIdentity(a)===roomIdentity(b);
  }
  function mergeRoom(a,b){
    // Prefer hydrated channel/control state over an announcement's placeholder #general.
    const richness=room=>room&&room.cord&&room.cord.bundle?(room.cord.hydrated?3:room.cord.armadaList?1:2):0;
    const first=richness(a)>richness(b)?b:a,last=first===a?b:a;
    const merged={...first,...last};
    if(a.cord||b.cord)merged.cord={...(first.cord||{}),...(last.cord||{})};
    const aId=communityKey(a),bId=communityKey(b),id=!aId||/^naddr1/i.test(aId)?bId||aId:aId;if(id)merged.communityId=id;
    const aliases=new Map();
    for(const url of [merged.url,a.url,b.url,...(a.inviteAliases||[]),...(b.inviteAliases||[])].filter(Boolean)){const key=inviteKey({url})||url;if(!aliases.has(key))aliases.set(key,url);}
    if(aliases.size>1)merged.inviteAliases=[...aliases.values()];else delete merged.inviteAliases;
    return merged;
  }
  function uniqueRooms(rows){
    const result=[];
    for(const row of rows||[]){if(!row||typeof row!=='object')continue;let room=row,index=result.findIndex(other=>sameRoom(other,room));
      if(index<0){result.push(room);continue;}
      room=mergeRoom(result[index],room);result[index]=room;
      // Hydration can bridge an old invite-only row and a membership row with a community id.
      for(let j=result.length-1;j>index;j--)if(sameRoom(result[j],room)){room=mergeRoom(result[j],room);result[index]=room;result.splice(j,1);}
    }
    return result;
  }
  function notificationRoute(room,channel,message){
    return 'concord:'+encodeURIComponent(roomIdentity(room))+':'+
      encodeURIComponent(String(channel||'general'))+':'+encodeURIComponent(messageId(message));
  }
  function removeCommunityByIdentity(rooms,identity){
    const next=Array.isArray(rooms)?rooms.slice():[],index=next.findIndex(room=>roomIdentity(room)===String(identity||''));
    if(index>=0)next.splice(index,1);
    return {rooms:next,index};
  }
  function memberTapAction(narrow,longPressed){ return longPressed?'consume':(narrow?'profile':'menu'); }
  function memberViewportIsNarrow(){ return !!(window.matchMedia&&window.matchMedia('(max-width:820px)').matches); }
  /* A HIDDEN ANCHOR MEASURES 0x0, AND 0x0 IS A REAL POSITION: THE TOP-LEFT CORNER.
   *
   * Every term below is relative to the anchor, so an anchor with no box collapses the whole thing
   * to `{left:8, top:6}` -- which is not a failure anybody sees as a failure, it is a picker in the
   * corner of the screen. Reported as "clicking emoji on a chat does nothing", "react broken", and
   * then precisely: "on webui it loads the emoji picker in the top-left corner, on windows app you
   * don't see it at all".
   *
   * The anchor is hidden by the caller itself. `closeMessageActions()` strips `cc-actions-open`,
   * and under `(hover:none)` -- which a touch-capable Windows machine reports -- that CSS makes the
   * toolbar's buttons `display:none`. The button that was just clicked stops having a box, and it
   * is then measured.
   *
   * So the rect may be handed IN, measured before anything was closed. And a degenerate rect is
   * refused here rather than honoured, because this function cannot tell a corner somebody wants
   * from a corner that means "I could not measure": callers get a null and place the picker off
   * something that is definitely laid out. */
  function reactionPickerPosition(anchor,picker,viewport,anchorRect){
    const a=anchorRect||anchor.getBoundingClientRect();
    if(!a||!(a.width>0)||!(a.height>0))return null;
    const gap=6,margin=8,view=viewport||{},vw=Number(view.width||window.innerWidth||0),vh=Number(view.height||window.innerHeight||0),r=picker.getBoundingClientRect(),w=r.width||picker.offsetWidth||0,h=r.height||picker.offsetHeight||0;
    const left=Math.max(margin,Math.min(a.right-w,vw-w-margin));
    const below=a.bottom+gap,above=a.top-h-gap,top=below+h<=vh-margin?below:(above>=margin?above:Math.max(margin,Math.min(below,vh-h-margin)));
    return{left,top};
  }
  function placeReactionPicker(anchor,picker,measured){
    /* The message list scrolls and clips its descendants. Portal the picker to the viewport, then
       flip it above a bottom-edge trigger (the common last-message case) and clamp both axes. */
    document.body.appendChild(picker);
    /* `measured` is the anchor's rect as it was BEFORE the caller closed anything -- see the note on
       reactionPickerPosition. Falling back to the message row is what keeps this on screen when even
       that is missing: a row is always laid out, and beside the right message is a far better answer
       than the corner of the display. */
    let at=reactionPickerPosition(anchor,picker,null,measured);
    if(!at){
      let row=null;
      try{ row=anchor.closest?anchor.closest('.cc-message'):null; }catch(_){ row=null; }
      if(row)at=reactionPickerPosition(row,picker);
    }
    if(!at)return false;
    /* RECT PIXELS IN, STYLE PIXELS OUT -- and they are not the same pixel under `body{zoom}`.
     *
     * `reactionPickerPosition` works in `getBoundingClientRect()` space, which IS scaled by the
     * page zoom; `style.left` on a `position:fixed` child of that zoomed body is NOT -- it is
     * multiplied by the zoom on the way to the screen. So at a 1.25 display scale the picker was
     * placed 25% further right and further down than it was told, which for a control anchored
     * near the right edge of a message row puts it off the screen entirely. Reported as "emoji
     * reaction in concord does nothing": the button worked, the picker opened, and nobody could
     * see it. Same mistake as the taskbar flyouts and the desktop's own `openPop` before them.
     *
     * The ratio is measured off the anchor rather than read from a global: this file has no zf(),
     * and the anchor is an element inside the same zoomed body, so its own rect against its layout
     * width is exactly the factor. An anchor with no layout width leaves it at 1, which is what
     * every un-zoomed page has always done. */
    let k=1;
    try{
      const ar=(measured&&measured.width>0)?measured:anchor.getBoundingClientRect();
      const ow=(measured&&measured.offsetWidth>0)?measured.offsetWidth:anchor.offsetWidth;
      if(ar.width>0&&ow>0)k=ar.width/ow;
      if(!(k>0)||!isFinite(k))k=1;
    }catch(_){ k=1; }
    picker.style.setProperty('position','fixed','important');
    picker.style.setProperty('inset','auto','important');
    picker.style.setProperty('left',(at.left/k)+'px','important');
    picker.style.setProperty('top',(at.top/k)+'px','important');
    return true;
  }
  function normalizeIcon(raw){
    const v=String(raw||'').trim(); if(!v)return '';
    try{ const u=new URL(v); if(u.protocol==='https:'||u.protocol==='http:'||u.protocol==='blob:')return u.href; }catch(_){}
    return Array.from(v).slice(0,4).join('');
  }
  function roomIcon(p,r,i){
    const key=r&&roomIdentity(r),cached=key&&roomIconRefs.get(key);
    const icon=normalizeIcon(cached&&cached.url||r&&r.icon);
    if(r&&r.iconPointer&&!cached&&!storedIconLoads.has(key))void hydrateStoredRoomIcon(p,r,key);
    if(/^(https?:\/\/|blob:)/i.test(icon)) return `<img class="cc-server-img" src="${p.enc(icon)}" alt="">`;
    return `<span class="cc-server-glyph">${p.enc(icon||roomName(r,i).slice(0,2).toUpperCase())}</span>`;
  }
  function publicRoomIcon(p,r){
    const icon=normalizeIcon(r&&r.icon);
    if(/^(https?:\/\/|blob:)/i.test(icon))return `<img src="${p.enc(icon)}" alt="" loading="lazy">`;
    return `<span>${p.enc(icon||(r.name||'C').slice(0,2).toUpperCase())}</span>`;
  }
  /* ONE RUMOR, ONE ROW.
   *
   * Sending is optimistic.  While createChatWrap/publish is still resolving, the four-second live
   * query can already receive that rumor and append its permanent id.  The publish continuation
   * then renames the pending row to the SAME id.  Previously both rows were saved and the next live
   * pass built a dedupe Map but declined to persist it when no new event arrived, so one send could
   * remain visibly doubled forever.  Normalize at both storage boundaries: writes prevent the race,
   * reads repair caches produced by an older build.  Later relay data wins while retaining useful
   * optimistic fields it does not carry (for example the already-painted reply summary). */
  function messageExpired(m,now=Math.floor(Date.now()/1000)){
    if(!m||m.kind===5||m.kind===1740||m.kind>=20000&&m.kind<30000)return false;
    const at=(m.tags||[]).find(t=>t[0]==='expiration')?.[1];
    return typeof at==='string'&&/^(0|[1-9][0-9]*)$/.test(at)&&Number.isSafeInteger(Number(at))&&Number(at)<=now;
  }
  function expireMessageEffects(m,now=Math.floor(Date.now()/1000)){
    const expired=at=>at!==null&&at!==undefined&&Number.isSafeInteger(Number(at))&&Number(at)<=now;
    if(expired(m.reply?.expires))m={...m,reply:{...m.reply,text:'Expired message'}};
    if(expired(m.editedExpires)&&typeof m.originalText==='string'){m={...m,text:m.originalText,tags:(m.tags||[]).filter(t=>t[0]!=='edited')};delete m.editedExpires;delete m.originalText;}
    for(const [emoji,entries] of Object.entries(m.reactionIds||{}))for(const [pk,id] of Object.entries(entries))if(expired(m.reactionExpirations?.[id])){m={...m,reactionIds:{...m.reactionIds,[emoji]:{...m.reactionIds[emoji]}},reactions:{...m.reactions,[emoji]:(m.reactions?.[emoji]||[]).filter(x=>x!==pk)}};delete m.reactionIds[emoji][pk];if(!m.reactions[emoji].length){delete m.reactions[emoji];delete m.reactionIds[emoji];}}
    if(m.votes){const votes=m.votes.filter(v=>!expired(v.expires));if(votes.length!==m.votes.length)m={...m,votes};}
    if(m.zaps){const zaps=m.zaps.filter(v=>!expired(v.expires));if(zaps.length!==m.zaps.length)m={...m,zaps};}
    return m;
  }
  function uniqueMessages(v){
    const byId=new Map();
    for(let m of Array.isArray(v)?v:[]){
      if(!m||typeof m!=='object'||messageExpired(m))continue;
      m=expireMessageEffects(m);
      const id=messageId(m),old=byId.get(id);
      byId.set(id,old?{...old,...m}:m);
    }
    return [...byId.values()];
  }
  const remoteMessages=new Map(),remoteStoreIds=new Set();
  function testMessages(id){ if(remoteMessages.has(id))return uniqueMessages(remoteMessages.get(id));try{ const v=JSON.parse(localStorage.getItem('pc.concord.test.'+id)||'[]'); return uniqueMessages(v); }catch(_){ return []; } }
  function markRemoteStore(id){if(!remoteMessages.has(id))remoteMessages.set(id,testMessages(id));remoteStoreIds.add(id);try{localStorage.removeItem('pc.concord.test.'+id);}catch(_){} }
  function saveTestMessages(id,v){const clean=uniqueMessages(v);if(remoteStoreIds.has(id)){remoteMessages.set(id,clean.slice(-5000));try{localStorage.removeItem('pc.concord.test.'+id);}catch(_){}return;}try{localStorage.setItem('pc.concord.test.'+id,JSON.stringify(clean.slice(-200)));}catch(_){} }
  async function clearRoomCache(room){nip29EventStreams.delete(roomIdentity(room));const loadKey=room&&(room.communityId||room.naddr);if(!loadKey)return;try{if(window.PCConcordCache)await window.PCConcordCache.dropRoom(loadKey);}catch(e){console.warn('Concord room cache cleanup failed',e);}for(const channel of channelsOf(room)){const id=channelStoreId(room,channel.name);remoteMessages.delete(id);remoteStoreIds.delete(id);try{localStorage.removeItem('pc.concord.test.'+id);}catch(_){}}for(const id of [...remoteMessages.keys()])if(room.naddr&&id.startsWith(room.naddr)){remoteMessages.delete(id);remoteStoreIds.delete(id);}const icon=roomIconRefs.get(loadKey);if(icon&&/^blob:/i.test(String(icon.url||'')))try{URL.revokeObjectURL(icon.url);}catch(_){}roomIconRefs.delete(loadKey);}
  function pendingEchoMatch(messages,remote){
    const candidates=(messages||[]).filter(m=>m&&m.pending&&String(m.pubkey||'')===String(remote&&remote.pubkey||'')&&String(m.text||'')===String(remote&&remote.text||'')&&Number(m.kind||9)===Number(remote&&remote.kind||9)).map(m=>({message:m,gap:Math.abs(Number(m.at||0)-Number(remote&&remote.at||0))})).filter(x=>x.gap<120000).sort((a,b)=>a.gap-b.gap);
    if(!candidates.length)return null;
    /* Two intentional identical sends can coexist. A relay echo has no trustworthy way to identify
     * either pending row from content and approximate time alone; leave it separate until createChatWrap returns the
     * permanent rumor id, rather than guessing and collapsing a real message. */
    if(candidates.length>1)return null;
    return candidates[0].message;
  }
  function mergeRelayMessages(prior,incoming){
    const out=uniqueMessages(prior),byId=new Map(out.map(m=>[messageId(m),m]));
    for(const remote of incoming||[]){
      const id=messageId(remote);if(byId.has(id)){Object.assign(byId.get(id),remote);continue;}
      /* A relay echo may arrive before createChatWrap() returns its permanent rumor id. Reconcile
       * that echo with the optimistic row by authorship/content/kind and a tight send-time window;
       * otherwise the same send is painted twice until another refresh happens. */
      const pending=pendingEchoMatch(out,remote);
      if(pending){byId.delete(messageId(pending));Object.assign(pending,remote,{pending:false,remote:true});byId.set(id,pending);}
      else{out.push(remote);byId.set(id,remote);}
    }
    return uniqueMessages(out);
  }
  function channelStoreId(room,name){ const channel=name||'general',c=room&&Array.isArray(room.channels)&&room.channels.find(x=>x.name===channel),identity=String(room&&(room.naddr||room.communityId||room.url)||''); return identity+(channel!=='general'?'.'+(c&&c.id||channel):''); }
  function channelsOf(room){
    const channels=room&&Array.isArray(room.channels)?room.channels.filter(c=>c&&c.name):[];
    return channels.length||room?.deletedChannels?.length?channels:[{name:'general',private:false}];
  }
  function activeMessages(room){ return testMessages(channelStoreId(room,state.channel)); }
  /* A CORD stream may retain 5,000 decrypted messages in renderer memory. Building every row,
   * link preview and media control in one innerHTML assignment blocks Android's UI thread (and can
   * make desktop Chromium report the window as frozen). The durable encrypted cache still keeps
   * the complete history; the conversation paints a bounded newest window, matching the 300-row
   * cache page used during cold launch. */
  const MAX_PAINTED_MESSAGES=300;
  function paintedMessages(room){const all=activeMessages(room).filter(m=>!m.delivery||m.delivery==='sent'||m.pubkey===deliveryOwner(PC()));return all.length>MAX_PAINTED_MESSAGES?all.slice(-MAX_PAINTED_MESSAGES):all;}
  /* CORD control/chat stream pubkeys identify shared transport groups, not people.
   * The invite's community-bound owner and authenticated message authors are known participants;
   * a stream read does not provide a complete membership roster. Never offer group keys as
   * profiles, mention recipients or call targets, including keys copied into legacy member lists. */
  var _partsCache=new Map(),roomGuestbooks=new Map(),guestbookViewer='';
  function ownGuestbookViewer(owner){if(guestbookViewer!==owner){roomGuestbooks.clear();guestbookViewer=owner;}}
  async function refreshGuestbookMembers(p,room,controls){
    const reader=window.PosterCordReader,bundle=room&&room.cord&&room.cord.bundle;
    if(!reader||!reader.inspectGuestbook||!bundle)return;
    const owner=deliveryOwner(p),identity=roomIdentity(room),loadKey=room.communityId||room.naddr,key=owner+'\n'+identity,material=JSON.stringify(bundle),controlIds=JSON.stringify(cordControlStamp(controls));
    ownGuestbookViewer(owner);
    const current=()=>roomGuestbooks.get(key)===entry&&deliveryOwner(p)===owner&&saved().some(r=>roomIdentity(r)===identity&&r.cord&&JSON.stringify(r.cord.bundle)===material)&&JSON.stringify(cordControlStamp(roomControls.get(loadKey)))===controlIds;
    const previous=roomGuestbooks.get(key);
    if(previous&&previous.material===material&&previous.controlIds===controlIds&&(previous.pending||Date.now()-previous.started<60000))return;
    const entry={owner,bundle,controls,material,controlIds,wraps:previous&&previous.material===material?previous.wraps:[],pending:true,started:Date.now()};
    if(roomGuestbooks.size>=100)roomGuestbooks.delete(roomGuestbooks.keys().next().value);
    roomGuestbooks.set(key,entry);
    const cacheKey=envelopeCacheKey(loadKey,'guestbook:'+owner);
    try{
      const cached=await cachedEnvelopes(cacheKey);if(!current())return;
      entry.wraps=mergeEnvelopes(entry.wraps,cached);entry.projection=null;
      if(cached.length)preserveChatScroll(()=>backgroundRender());
      const seed=reader.inspectControl(bundle,controls),plane=typeof cordPlaneContext==='function'?cordPlaneContext(p,bundle,controls,room):null;
      // Older readers/transports must not silently query a private plane with the user's AUTH.
      if(!plane||!seed.guestbookPubkeys||!seed.guestbookPubkeys.length)return;
      const wraps=await queryEnvelopeHistory(p,roomRelays(bundle),seed.guestbookPubkeys,entry.wraps,{plane,purpose:'concord guestbook '+loadKey});
      if(!current())return;
      entry.wraps=wraps;entry.projection=null;
      await cacheEnvelopes(cacheKey,wraps.filter(e=>!cached.some(old=>old.id===e.id)));
      if(current())preserveChatScroll(()=>backgroundRender());
    }catch(e){if(current())console.warn('Concord member history unavailable',e);}
    finally{entry.pending=false;}
  }
  /* An envelope this client just published to the guestbook, folded in without waiting for the next read. */
  function noteOwnGuestbookEvent(room,wrap,owner){
    try{
      const entry=roomGuestbooks.get(owner+'\n'+roomIdentity(room));
      if(!entry||!wrap)return;
      entry.wraps=mergeEnvelopes(entry.wraps,[wrap]);entry.projection=null;
      const loadKey=room.communityId||room.naddr;
      void cacheEnvelopes(envelopeCacheKey(loadKey,'guestbook:'+owner),[wrap]).catch(()=>{});
    }catch(_){ }
  }
  /* WHO EXACTLY IS BEING BANNED. "Ban Todd?" named two different people: the owner's ban went to "Dr. Todd Smith"
   * while THE TODDSTR stayed in the community (2026-10-18). The confirmation names the ACCOUNT -- display name, NIP-05
   * and the npub's two ends -- never a display name alone. */
  function banLabel(pubkey,profile,npub){
    const pr=profile||{},name=String(pr.display_name||pr.name||'').trim()||'this account';
    const short=npub&&npub.length>20?npub.slice(0,12)+'…'+npub.slice(-6):(npub||String(pubkey||'').slice(0,12)+'…');
    return name+' — '+(pr.nip05?String(pr.nip05)+', ':'')+short;
  }
  /* "BAN BY NPUB": somebody who is not on the member list right now -- a banned person's second account, a kicked
   * member who keeps rejoining -- could not be banned at all, because the only ban was a member-row menu. The paste
   * is an npub (or hex); anything else, yourself or the owner is refused with the reason. */
  function banTargetFromInput(raw,{viewer='',owner='',decode=null}={}){
    let v=String(raw||'').trim().replace(/^nostr:/i,'');
    if(!v)return {error:''};
    let hex='';
    if(/^[0-9a-f]{64}$/i.test(v))hex=v.toLowerCase();
    else if(/^npub1[02-9ac-hj-np-z]+$/i.test(v)&&decode){try{const d=decode(v.toLowerCase());if(d&&d.type==='npub')hex=String(d.data).toLowerCase();}catch(_){}}
    if(!/^[0-9a-f]{64}$/.test(hex))return {error:'That is not an npub.'};
    if(hex===String(viewer).toLowerCase())return {error:'You cannot ban yourself.'};
    if(hex===String(owner).toLowerCase())return {error:'The owner cannot be banned.'};
    return {target:hex};
  }
  function roomParticipants(room,viewerPubkey=''){
    ownGuestbookViewer(viewerPubkey);
    const observed=channelsOf(room).flatMap(channel=>testMessages(channelStoreId(room,channel.name))).filter(Boolean),fromMessages=observed.map(message=>message.pubkey);
    let owner='',grantees=[],banned=new Set(),transport=new Set(channelsOf(room).flatMap(channel=>channel.streamPubkeys||[]).map(pk=>String(pk).toLowerCase()));
    try{
      const loadKey=room&&(room.communityId||room.naddr),bundle=room&&room.cord&&room.cord.bundle,
            reader=window.PosterCordReader,wraps=loadKey?roomControls.get(loadKey):null;
      if(reader&&reader.inspectControl&&bundle&&loadKey){
        const hit=_partsCache.get(loadKey);
        let known;
        if(hit&&hit.bundle===bundle&&hit.wraps===wraps)known=hit.known;
        else{
          // inspectControl validates the owner's binding to this community before trusting it.
          const view=reader.inspectControl(bundle,wraps||[]);
          known={owner:bundle.owner||'',members:(view&&view.members)||[],banned:(view&&view.banned)||[],transport:[...(view&&view.controlPubkeys||[]),...(view&&view.guestbookPubkeys||[]),
            ...((view&&view.channels)||[]).flatMap(channel=>channel.streamPubkeys||[])]};
          _partsCache.clear();_partsCache.set(loadKey,{bundle,wraps,known});
        }
        owner=known.owner;grantees=known.members;banned=new Set(known.banned);for(const pk of known.transport)transport.add(String(pk).toLowerCase());
        const guestbook=roomGuestbooks.get(viewerPubkey+'\n'+roomIdentity(room));
        if(guestbook&&guestbook.owner===viewerPubkey&&guestbook.material===JSON.stringify(bundle)&&guestbook.controlIds===JSON.stringify(cordControlStamp(wraps))&&reader.inspectGuestbook){
          const activity=observed.map(m=>({pubkey:m.pubkey,at:Number(m.at)})),signature=JSON.stringify(activity);
          if(!guestbook.projection||guestbook.activity!==signature){guestbook.projection=reader.inspectGuestbook(bundle,wraps||[],guestbook.wraps,activity);guestbook.activity=signature;}
          return guestbook.projection.members;
        }
      }
    }catch(_){ /* Visible message authors remain available while metadata is unavailable. */ }
    const valid=pk=>typeof pk==='string'&&/^[0-9a-f]{64}$/i.test(pk);
    return [...new Set([viewerPubkey,owner,...grantees,...fromMessages].filter(valid).map(pk=>pk.toLowerCase()))]
      .filter(pk=>!banned.has(pk)&&(pk===String(owner).toLowerCase()||!transport.has(pk)));
  }
  function mentionAliases(profile,pubkey,fallback=''){
    const aliases=new Set([profile&&profile.display_name,profile&&profile.name,fallback,pubkey]
      .map(value=>String(value||'').trim().toLowerCase()).filter(Boolean));
    for(const value of [...aliases])aliases.add(value.replace(/\s+/g,'_'));
    return aliases;
  }
  function typedMentionRecipients(text,participants,profileOf){
    const tokens=new Set([...String(text||'').matchAll(/(?:^|\s)@([\w.-]+)/g)].map(match=>match[1].toLowerCase()));
    if(!tokens.size)return [];
    return [...new Set((participants||[]).filter(pubkey=>{
      const profile=profileOf?profileOf(pubkey)||{}:{};
      /* THE SAME FALLBACK THE PICKER INSERTS, or its own suggestion cannot be typed back.
       *
       * `drawMentions` labels a participant `display_name || name || pubkey.slice(0,12)` and
       * `acceptMention` writes that label into the box — so for anybody with no profile name the
       * handle on screen is a 12-character pubkey prefix. This resolver built its aliases WITHOUT
       * that fallback, and `mentionAliases` only ever carries the FULL 64-character key, so the
       * prefix matched nothing: tab-completing tagged them (the picker records the handle in
       * `mentionRecipients`), and typing the identical text by hand tagged nobody, silently. Two
       * call sites, two different ideas of what a person is called. */
      const fallback=String(profile.display_name||profile.name||String(pubkey||'').slice(0,12));
      return [...mentionAliases(profile,pubkey,fallback)].some(alias=>tokens.has(alias));
    }))];
  }
  function textMentionsViewer(text,handles){
    const tokens=new Set([...String(text||'').matchAll(/(?:^|\s)@([\w.-]+)/g)].map(match=>match[1].toLowerCase()));
    return (handles||[]).some(handle=>tokens.has(String(handle||'').trim().toLowerCase().replace(/\s+/g,'_')));
  }
  /* VECTOR NOTIFIES ON `@npub1…` TEXT AND ON NOTHING ELSE (it reads no `p` tag for mentions), so a
   * mention typed here as `@Alice` pinged nobody on Vector while our own readers, who read the `p`
   * tag, were fine. The wire text carries `@npub1…` for every person the composer tagged; the `p`
   * tag stays, and the renderer turns `@npub1…` back into `@Name`. Longest handle first, so
   * `@alice_b` is not eaten by `@alice`. Pure, for tests/client/concord_vector_interop_runtime.mjs. */
  function wireMentionText(text,pairs,npubFor){
    let out=String(text||'');
    const esc=v=>String(v).replace(/[.*+?^${}()|[\]\\]/g,'\\$&');
    for(const [handle,pk] of [...(pairs||[])].filter(([h,k])=>h&&k).sort((a,b)=>String(b[0]).length-String(a[0]).length)){
      const np=npubFor(pk); if(!np)continue;
      out=out.replace(new RegExp('(^|\\s)@'+esc(handle)+'(?![\\w.-])','gi'),(whole,pre)=>pre+'@'+np);
    }
    return out;
  }
  function npubOf(pk){ try{ return window.NostrTools.nip19.npubEncode(String(pk).toLowerCase()); }catch(_){ return ''; } }
  /* EDITING A MESSAGE KEEPS ITS MENTIONS, AND CAN ADD THEM ("username completion/tagging functionality missing from
   * concord edit modal", 2026-10-21). The edit box was a bare textarea: no @-picker, and what it sent was the raw text,
   * so `@Alice` typed in an edit tagged nobody -- and the message's existing mentions were shown as `@npub1…`. This is
   * the composer's picker for any textarea (same rows, same tap-on-release, arrows/Tab/Enter on a keyboard), plus the
   * composer's own wire rules on the way out (wireMentionText + typedMentionRecipients). */
  function textareaMentions(p,box,room){
    const viewer=p.viewer?p.viewer():{},recipients=new Map();let choices=[],index=0;
    const list=document.createElement('div');list.className='cc-mentions cc-box-mentions';list.setAttribute('role','listbox');
    list.setAttribute('aria-label','Mention a member');list.hidden=true;box.parentElement.insertBefore(list,box.nextSibling);
    const token=()=>String(box.value||'').slice(0,box.selectionStart).match(/(?:^|\s)@([\w.-]*)$/);
    const paint=()=>{
      if(!choices.length){list.hidden=true;list.innerHTML='';return;}
      list.hidden=false;
      list.innerHTML=choices.map((c,i)=>{const pr=p.profOf?p.profOf(c.pk)||{}:{};
        return '<button type="button" role="option" class="cc-mention-opt'+(i===index?' on':'')+'" aria-selected="'+(i===index)+'" data-cc-mention="'+i+'">'
          +'<img src="'+p.enc(pr.picture||p.LOGO||'')+'" alt=""><span>@'+p.enc(c.name)+'</span></button>';}).join('');
    };
    const draw=()=>{
      const m=token();if(!m){choices=[];paint();return;}
      const q=m[1].toLowerCase();
      choices=roomParticipants(room,viewer.pubkey).map(pk=>{const pr=p.profOf?p.profOf(pk)||{}:{},name=String(pr.display_name||pr.name||String(pk).slice(0,12));
        return {pk,name,aliases:mentionAliases(pr,pk,name)};}).filter(x=>!q||[...x.aliases].some(a=>a.includes(q))).slice(0,8);
      index=0;paint();
    };
    const accept=i=>{
      const m=token(),c=choices[i];if(!m||!c)return false;
      const handle=c.name.replace(/\s+/g,'_'),end=box.selectionStart,start=end-m[1].length-1;
      recipients.set(handle.toLowerCase(),c.pk);box.setRangeText('@'+handle+' ',start,end,'end');
      choices=[];paint();box.focus();return true;
    };
    let sx=0,sy=0,moved=false;
    list.addEventListener('pointerdown',ev=>{if(!ev.target.closest||!ev.target.closest('[data-cc-mention]'))return;sx=ev.clientX;sy=ev.clientY;moved=false;ev.preventDefault();});
    list.addEventListener('pointermove',ev=>{if(Math.abs(ev.clientX-sx)>8||Math.abs(ev.clientY-sy)>8)moved=true;});
    list.addEventListener('pointerup',ev=>{const row=ev.target.closest&&ev.target.closest('[data-cc-mention]');if(!row||moved)return;ev.preventDefault();accept(Number(row.dataset.ccMention)||0);});
    box.addEventListener('input',draw);
    box.addEventListener('keydown',e=>{
      if(!choices.length)return;
      if(e.key==='ArrowDown'||e.key==='ArrowUp'){e.preventDefault();e.stopImmediatePropagation();index=(index+(e.key==='ArrowDown'?1:-1)+choices.length)%choices.length;paint();return;}
      if(e.key==='Tab'||(e.key==='Enter'&&!e.ctrlKey&&!e.metaKey)){e.preventDefault();e.stopImmediatePropagation();accept(index);return;}
      if(e.key==='Escape'){e.preventDefault();e.stopImmediatePropagation();choices=[];paint();}
    },true);
    return {
      /* `@npub1…` (the wire form) as the `@Name` a person types, each one remembered so it goes back out the same. */
      readable(text){
        return String(text||'').replace(/(^|\s)@(npub1[023456789acdefghjklmnpqrstuvwxyz]{58})(?![0-9a-z])/gi,(whole,pre,np)=>{
          let pk='';try{const d=window.NostrTools.nip19.decode(np);pk=d.type==='npub'?d.data:'';}catch(_){}
          if(!pk)return whole;
          const pr=p.profOf?p.profOf(pk)||{}:{},handle=String(pr.display_name||pr.name||String(pk).slice(0,12)).replace(/\s+/g,'_');
          recipients.set(handle.toLowerCase(),pk);return pre+'@'+handle;});
      },
      /* {text, tags}: the wire text and a `p` tag per person mentioned, exactly as a new message is sent. */
      wire(text){
        const lower=String(text||'').toLowerCase(),tagged=new Set(),pairs=[];
        for(const [handle,pk] of recipients)if(lower.includes('@'+handle)){tagged.add(pk);pairs.push([handle,pk]);}
        for(const pk of typedMentionRecipients(text,roomParticipants(room,viewer.pubkey),p.profOf)){
          tagged.add(pk);const pr=p.profOf?p.profOf(pk)||{}:{};
          for(const alias of mentionAliases(pr,pk,String(pr.display_name||pr.name||String(pk).slice(0,12))))pairs.push([alias,pk]);
        }
        return {text:wireMentionText(text,pairs,npubOf),tags:[...tagged].map(pk=>['p',pk])};
      },
    };
  }
  /* `@npub1…` → `nostr:npub1…` before linkify, which renders a profile link reading `@Name`. Left as
   * `@npub1…` it rendered `@@Name` (linkify keeps the character before the entity). */
  function readableMentions(text){ return String(text||'').replace(/(^|\s)@((?:npub1)[023456789acdefghjklmnpqrstuvwxyz]{58})(?![0-9a-z])/gi,'$1nostr:$2'); }
  /* The same, for plain text that is not linkified (a notification body). */

  function mentionNames(text,profOf){
    return String(text||'').replace(/(^|\s)@(npub1[023456789acdefghjklmnpqrstuvwxyz]{58})(?![0-9a-z])/gi,(whole,pre,np)=>{
      try{ const pk=window.NostrTools.nip19.decode(np).data,pr=profOf?profOf(pk)||{}:{}; return pre+'@'+(pr.display_name||pr.name||np.slice(0,12)+'…'); }catch(_){ return whole; }
    });
  }
  /* A MEMBER WITH NO PROFILE ON THIS RELAY IS LOOKED UP WHERE THE ROOM LIVES ("why is vyram not displaying
   * correctly", 2026-10-08). Names come from `p.profOf`, i.e. what this instance's relay holds -- and an Armada
   * user often writes with a SEPARATE key (Vyram's room key a4c44c8e…, his posting key b3f585f3…) that is in nobody's
   * web of trust here, so its kind-0 was never synced and he showed as a bare hex prefix. Each such key is asked
   * ONCE per session, batched, from the ROOM'S OWN relays only (`roomRelays`) -- never an outside profile relay:
   * the batch IS the room's member list, and an open room's traffic stays on its bundle relays (concord_runtime.mjs
   * enforces that). Vyram's room-key kind-0 is on three of the Developer's Quarters relays, measured. What comes back
   * is saved to the shared Store and the room repainted. A key with no profile there simply stays a prefix. */
  const profileAsked=new Set(),profileWant=new Set();let profileTimer=null;
  function wantProfiles(p,pks,room){
    try{
      for(const pk of pks||[]){
        if(!/^[0-9a-f]{64}$/.test(String(pk||''))||profileAsked.has(pk))continue;
        const pr=p&&p.profOf?p.profOf(pk):null;
        if(pr&&(pr.name||pr.display_name))continue;
        profileWant.add(pk);
      }
      if(!profileWant.size||profileTimer)return;
      const bundle=room&&room.cord&&room.cord.bundle;if(!bundle)return;
      const relays=roomRelays(bundle);if(!relays.length)return;
      profileTimer=setTimeout(async()=>{
        profileTimer=null;
        const batch=[...profileWant].slice(0,60);batch.forEach(pk=>{profileWant.delete(pk);profileAsked.add(pk);});
        let got=0;
        try{
          const evs=await cordQuery(p,relays,[{kinds:[0],authors:batch,limit:batch.length}],{purpose:'concord member profiles',timeout:6000});
          for(const e of evs||[]){try{if(e&&e.kind===0&&window.Store&&window.Store.saveProfile){window.Store.saveProfile(e);got++;}}catch(_){}}
        }catch(_){}
        if(got)try{backgroundRender();}catch(_){}
        if(profileWant.size)wantProfiles(p,[],room);
      },250);
    }catch(_){}
  }
  /* Per ACCOUNT (READ_KEY + pubkey): one account reading a room it shares with another is not the
   * other account having read it. The shared keys of older builds are copied in when a room is
   * adopted (adoptLegacyRoom), never read directly. */
  const READ_KEY='pc.concord.read.v2.',STAR_KEY='pc.concord.star.v2.';
  function channelReadKey(room,name){ return READ_KEY+accountPk()+'.'+(room&&room.naddr||'')+':'+(name||'general'); }
  function seenAt(room,name){
    if(!room||!room.naddr||!accountPk())return 0;
    /* ...AND WHAT THIS ACCOUNT READ ON ANY OTHER DEVICE. The unread dots and the sidebar count read only
     * this device's cursor, while the synced `pcai:concord-read` mark fed the mention bell alone -- so a
     * room read on the phone stayed bold on the desktop ("it's confusing on multiple devices"). Both are
     * the newest message seen, in ms; the later one wins. */
    const synced=readMark(room,name),exact=Number(localStorage.getItem(channelReadKey(room,name))||0);
    if(exact)return Math.max(exact,synced);
    /* Migration fallback for the release that stored one timestamp for the whole community. */
    return Math.max(synced,Number(localStorage.getItem(READ_KEY+accountPk()+'.'+room.naddr)||0));
  }
  /* READ UP TO THE NEWEST MESSAGE SEEN, not up to this device's clock. A message is unread while its `at` (the SENDER's
   * clock) is newer than the mark, so a phone whose clock ran behind a sender's kept the dot on a channel it had just
   * opened ("i open room but you still see that dot", 2026-10-21) -- and a git row timed by its relay the same. */
  function markRead(room,name){
    if(!(room&&room.naddr&&accountPk()))return;
    let newest=Date.now();
    try{ for(const m of testMessages(channelStoreId(room,name))||[])newest=Math.max(newest,Number(m.at)||0); }catch(_){}
    try{ const c=channelsOf(room).find(x=>x.name===(name||'general'));if(c&&window.PCConcordGit&&PCConcordGit.channelHasGit(c))newest=Math.max(newest,Number(PCConcordGit.latestAt(room,c))||0); }catch(_){}
    localStorage.setItem(channelReadKey(room,name),String(newest));
  }
  function isUnread(room){ return channelsOf(room).some(c=>testMessages(channelStoreId(room,c.name)).some(m=>(Number(m.at)||0)>seenAt(room,c.name))||gitUnread(room,c)); }
  function gitUnread(room,c){ try{ return !!(window.PCConcordGit&&PCConcordGit.channelHasGit(c)&&PCConcordGit.latestAt(room,c)>seenAt(room,c.name)); }catch(_){ return false; } }
  /* THE SIDEBAR BADGE. Communities has its own nav row now, so it needs its own count — as a tab
   * inside Messages it had nowhere to put one and a community with unread messages looked exactly
   * like a quiet one unless you opened Messages and looked. Rooms, not messages: the number people
   * act on is how many communities want them, and counting every message across every channel of
   * every room on every repaint is work nobody reads. */
  function unreadRooms(){ try{ return saved().filter(r=>isUnread(r)).length; }catch(_){ return 0; } }
  function paintUnreadBadge(){
    try{
      const n=unreadRooms();
      (document.querySelectorAll('#cc-badge,#cc-badge-m')||[]).forEach(b=>{
        if(n){ b.textContent=n>99?'99+':String(n); b.classList.remove('hidden'); }
        else b.classList.add('hidden');
      });
      /* The phone's bar has no Rooms button any more, so the ☰ badge is where an unread community
         shows. Painting only `#cc-badge-m` would leave the count visible exclusively inside the
         sheet somebody has to open to see it. */
      try{ const p=PC(); if(p && p.bumpMoreBadge) p.bumpMoreBadge(); }catch(_){ }
    }catch(_){ }
  }
  /* A DRAWER IS A CLASS, NOT A REPAINT ("going through the menus is clunky, it's not smooth at all").
   * Measured at 390x844 with the CPU throttled 4x and a 200-message room: the menu button and the
   * backdrop both went through render(), which rebuilds the whole workspace (rail, channels, all 200
   * message cards, the composer) and rebinds every handler, to flip ONE class: a 225-281 ms long task
   * per tap, every message card replaced (tests/client/test_concord_menus_stay_smooth_full_app.py).
   * The drawer's whole state is `drawer-open` on .cc-app, so that is all that changes now. Closing it
   * is the moment the conversation becomes visible again, which is when render() used to mark the
   * channel read; that one side effect is done here, with the two unread marks it clears. A repaint
   * that was deferred meanwhile (backgroundRenderPending) still gets its full render. */
  function setMobileDrawer(open){
    const app=document.querySelector?document.querySelector('.cc-app'):null,room=state.community==null?null:saved()[state.community];
    mobileDrawerOpen=!!open;
    if(!app||!room||!mobileChatOpen||backgroundRenderPending||!app.classList){render();drawerSlide();return;}
    app.classList.toggle('drawer-open',mobileDrawerOpen);
    if(mobileDrawerOpen){drawerSlide();return;}
    const name=state.channel||'general';markRead(room,name);
    try{
      const row=[...app.querySelectorAll('[data-cc-channel]')].find(b=>b.dataset.ccChannel===name);if(row)row.classList.remove('unread');
      const server=app.querySelector('.cc-server.active');if(server&&!isUnread(room))server.classList.remove('unread');
    }catch(_){}
    paintUnreadBadge();
  }
  /* THE DRAWER SLIDES WHEN SOMEBODY OPENS IT, NOT WHEN IT IS REDRAWN. The slide is keyed on
   * `drawer-opening` (concord.css), set here and nowhere in render()'s markup: keyed on `drawer-open`,
   * every rebuild while the drawer stood open replayed it, and a community switch from inside the
   * drawer rebuilds two or three times -- the list jerked 28px sideways each time. */
  function drawerSlide(){
    const app=document.querySelector?document.querySelector('.cc-app'):null;
    if(!app||!app.classList||!mobileDrawerOpen)return;
    app.classList.add('drawer-opening');
    const done=()=>{if(app.classList)app.classList.remove('drawer-opening');};
    try{app.addEventListener('animationend',done,{once:true});}catch(_){}
    setTimeout(done,400);
  }
  /* THE GLOWS KEEP THEIR BEAT ACROSS A REBUILD. The active channel's pulse and an unread community's
   * glow are infinite animations on elements every render() replaces, so each switch restarted them
   * from their first keyframe -- a visible blink in the channel list on every room change. Every one
   * of them is put on the DOCUMENT's clock instead (startTime 0), so a freshly built element is at
   * exactly the phase a never-replaced one would be: a rebuild, a retained rail being re-inserted
   * (which restarts its animations too) and a drawer opening onto the list are all invisible. Done
   * twice: straight after the rebuild for what is on screen (no frame is drawn at the wrong phase),
   * and on `animationstart` for an element that only starts later (the phone's drawer, opened). */
  const BEAT_ANIMATIONS=new Set(['ccThemePulse','ccUnreadGlow']);
  function onTheBeat(anim){ try{ if(anim&&BEAT_ANIMATIONS.has(anim.animationName)&&anim.startTime!==0)anim.startTime=0; }catch(_){ } }
  function keepGlowsOnTheBeat(root){
    try{
      if(!root||!root.querySelectorAll)return;
      for(const el of root.querySelectorAll('.cc-channel.active,.cc-server.unread'))
        if(el.getAnimations)for(const anim of el.getAnimations())onTheBeat(anim);
    }catch(_){ }
  }
  try{ document.addEventListener('animationstart',e=>{ if(!BEAT_ANIMATIONS.has(e.animationName)||!e.target||!e.target.getAnimations)return;
    for(const anim of e.target.getAnimations())if(anim.animationName===e.animationName)onTheBeat(anim); },true); }catch(_){ }
  function conversationIsVisible(narrow,chatOpen,drawerOpen){ return !narrow||(!!chatOpen&&!drawerOpen); }
  function messageId(m){ return String((m&&m.id)||`${Number(m&&m.at)||0}:${String(m&&m.pubkey||'')}`); }
  function imetaFields(tag){
    const out={}; if(!Array.isArray(tag)||tag[0]!=='imeta')return out;
    for(const raw of tag.slice(1)){const s=String(raw||''),i=s.indexOf(' ');if(i>0)out[s.slice(0,i)]=s.slice(i+1);}
    return out;
  }
  /* Armada encrypts room attachments independently from the already-encrypted chat rumor. The URL
   * therefore points at ciphertext; rendering it through the ordinary link/media helper makes an
   * extensionless blob look like a video, then disappear as soon as Play discovers invalid bytes. */
  function encryptedAttachments(m){
    const out=[];
    for(const tag of (m&&m.tags||[])){
      const f=imetaFields(tag),alg=String(f['encryption-algorithm']||'').toLowerCase();
      if(alg!=='aes-gcm'||!/^https:\/\//i.test(f.url||''))continue;
      if(!/^[0-9a-f]{64}$/i.test(f['decryption-key']||'')||!/^[0-9a-f]{24,32}$/i.test(f['decryption-nonce']||'')||!/^[0-9a-f]{64}$/i.test(f.ox||''))continue;
      const mime=/^[\w.+-]+\/[\w.+-]+$/.test(f.m||'')?f.m.toLowerCase():'application/octet-stream';
      const dm=/^(\d{1,5})x(\d{1,5})$/.exec(String(f.dim||''));
      out.push({url:f.url,key:f['decryption-key'],nonce:f['decryption-nonce'],hash:f.ox.toLowerCase(),mime,name:String(f.name||'attachment').slice(0,120),
                w:dm?+dm[1]:0,h:dm?+dm[2]:0});
    }
    return out;
  }
  /* CONCORD ATTACHMENTS ARE END-TO-END ENCRYPTED, THE WAY VECTOR WRITES THEM (crates/vector-core/
   * src/community/attachments.rs): a fresh AES-256-GCM key and 16-byte nonce per file, the Blossom
   * blob is the ciphertext, and the imeta carries `encryption-algorithm aes-gcm`, the key, the
   * nonce, the plaintext sha256 as `ox`, `size`, `name` and (for an image) `dim`. Ours used to be a
   * plaintext imeta, so every image posted to an encrypted room sat readable on a public Blossom
   * server. The reader (encryptedAttachments) has always opened this shape. */
  async function sealAttachment(file){
    const plain=new Uint8Array(await file.arrayBuffer());
    const keyBytes=crypto.getRandomValues(new Uint8Array(32)),nonce=crypto.getRandomValues(new Uint8Array(16));
    const key=await crypto.subtle.importKey('raw',keyBytes,'AES-GCM',false,['encrypt']);
    const cipher=new Uint8Array(await crypto.subtle.encrypt({name:'AES-GCM',iv:nonce},key,plain));
    let dim='';
    if(/^image\//i.test(file.type||'')&&typeof createImageBitmap==='function'){try{const bm=await createImageBitmap(new Blob([plain],{type:file.type}));dim=bm.width+'x'+bm.height;if(bm.close)bm.close();}catch(_){}}
    return {blob:new Blob([cipher],{type:'application/octet-stream'}),key:bytesHex(keyBytes),nonce:bytesHex(nonce),ox:bytesHex(await crypto.subtle.digest('SHA-256',plain)),size:plain.byteLength,dim};
  }
  function attachmentImeta(url,mime,name,sealed){
    const tag=['imeta',`url ${url}`,`m ${mime}`,'encryption-algorithm aes-gcm',`decryption-key ${sealed.key}`,`decryption-nonce ${sealed.nonce}`];
    if(sealed.size)tag.push(`size ${sealed.size}`);
    tag.push(`ox ${sealed.ox}`,`name ${String(name).replace(/[\r\n]/g,' ').slice(0,120)}`);
    if(sealed.dim)tag.push(`dim ${sealed.dim}`);
    return tag;
  }
  /* The composer shows an attachment as its URL so deleting the URL cancels it; on the wire the
   * file is the imeta, and the caption is the text without it — a URL left in `content` is a raw
   * Blossom link printed above the file in Vector. */
  function wireAttachmentText(text,urls){
    let out=String(text||'');
    for(const url of urls||[])out=out.split(url).join('');
    return out.replace(/[ \t]{2,}/g,' ').replace(/[ \t]+\n/g,'\n').trim();
  }
  function publicAttachments(m){
    const out=[];
    for(const tag of (m&&m.tags||[])){
      const f=imetaFields(tag),mime=String(f.m||'').toLowerCase();
      if(f['encryption-algorithm']||mime==='application/x-webxdc'||mime==='application/webxdc+zip'||mime==='application/vnd.webxdc+zip'||!/^https:\/\//i.test(f.url||''))continue;
      if(!/^[\w.+-]+\/[\w.+-]+$/.test(mime))continue;
      const dm=/^(\d{1,5})x(\d{1,5})$/.exec(String(f.dim||''));
      out.push({url:f.url,mime,name:String(f.name||'attachment').slice(0,120),w:dm?+dm[1]:0,h:dm?+dm[2]:0});
    }
    return out;
  }
  /* A POLL IS A MESSAGE, DRAWN AS A POLL.
   *
   * Armada's reader accepts kind 1068 beside 9 and 1111, so a poll posted there arrives here as an
   * ordinary message whose content is the question and whose options are `option` tags — and
   * Concord, which had no idea 1068 existed, drew the question as bare text with the options
   * nowhere. The answers are kind-1018 votes sealed in the same channel stream (see the pollVotes
   * patch in cord-reader.js); a public NIP-88 tally query finds none of them.
   *
   * Drawn in this app's own chat style rather than the timeline's poll card, which is a note with
   * an avatar, a header and an action bar — none of which belongs inside a chat bubble. */
  function pollOf(m){
    if(Number(m&&m.kind)!==1068)return null;
    const options=(m.tags||[]).filter(t=>t[0]==='option'&&t[1]).map(t=>({id:String(t[1]),label:String(t[2]||t[1])}));
    if(!options.length)return null;
    const endsAt=Number(((m.tags||[]).find(t=>t[0]==='endsAt')||[])[1]||0);
    return { options, multi:String(((m.tags||[]).find(t=>t[0]==='polltype')||[])[1]||'')==='multiplechoice',
             endsAt, ended:!!endsAt&&endsAt<Math.floor(Date.now()/1000), votes:m.votes||[] };
  }
  function pollHtml(p,m){
    const poll=pollOf(m); if(!poll)return '';
    const viewer=p.viewer?p.viewer():{},mine=new Set();
    /* ONE PERSON, ONE ANSWER — their LATEST. A vote is an ordinary event, so somebody who changes
     * their mind leaves two behind and counting both would let anyone inflate a poll by voting
     * repeatedly. */
    const latest=new Map();
    for(const v of poll.votes){const at=Number(v&&v.ms)||0,held=latest.get(v&&v.pubkey);
      if(!held||at>=held.ms)latest.set(v.pubkey,{ms:at,optionIds:(v&&v.optionIds)||[]});}
    const tally=new Map();
    for(const [pk,v] of latest){ for(const id of v.optionIds){ tally.set(id,(tally.get(id)||0)+1);
      if(pk===viewer.pubkey)mine.add(id); } }
    const total=latest.size;
    const rows=poll.options.map(o=>{
      const n=tally.get(o.id)||0,pct=total?Math.round(n*100/total):0,voted=mine.has(o.id);
      return `<button class="cc-poll-opt${voted?' voted':''}" data-cc-poll="${p.enc(messageId(m))}" `+
        `data-cc-option="${p.enc(o.id)}"${poll.ended?' disabled':''}>`+
        `<span class="cc-poll-bar" style="width:${pct}%"></span>`+
        `<span class="cc-poll-label">${p.enc(o.label)}</span>`+
        `<span class="cc-poll-count">${pct}%</span></button>`;
    }).join('');
    return `<div class="cc-poll">${rows}<div class="cc-poll-foot">`+
      `${total} ${total===1?'vote':'votes'}${poll.multi?' · multiple choice':''}`+
      `${poll.ended?' · ended':''}</div></div>`;
  }
  /* ✨ ON A LINK, as Telegram has it ("Concord -> Add sparkle button next to links like we do for
   * telegram"): the links in ONE message, summarized by the shared chatassist.js sheet. Shown only
   * where ✨ is allowed at all (PCChatAssist.shown(): AI access on this account), and only for a real
   * link — attachment URLs are stripped first, so an uploaded picture is not offered as "a link".
   * Bound in bindMessages() with the other message buttons (it re-runs for patched rows too) (no document-wide click handler: one of those can
   * repaint Concord after another app owns the shared feed). */
  const sumText=new Map();
  function linkSparkleHtml(p,m){
    const A=window.PCChatAssist;if(!A||!A.shown||!A.shown()||!m||m.pending||m.failed)return '';
    let text=String(m.text||'').slice(0,65536);
    for(const f of [...encryptedAttachments(m),...publicAttachments(m)])text=text.split(f.url).join('').trim();
    if(!A.hasLink(text))return '';
    const mid=messageId(m);sumText.set(mid,text);if(sumText.size>2000)sumText.delete(sumText.keys().next().value);
    return `<button type="button" class="tg-sum cc-sum" data-cc-sum="${p.enc(mid)}" title="Summarize with AI">✨ Summarize ${A.isVideo(text)?'video':'link'}</button>`;
  }

  /* ```lang\n…\n``` blocks out of chat text; everything else stays prose. A fence only opens at the
     start of a line and only counts when it closes — an unclosed one is shown exactly as typed. */
  function codeParts(text){
    const out=[],re=/(^|\n)```[^\n`]*\n([\s\S]*?)\n?```(?=\n|$)/g;let at=0,mm;
    while((mm=re.exec(text))){const start=mm.index+mm[1].length;if(start>at)out.push({text:text.slice(at,start)});out.push({block:mm[2]});at=re.lastIndex;}
    if(at<text.length)out.push({text:text.slice(at)});
    return out;
  }
  function escCode(s){ return String(s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }
  function messageContentHtml(p,m,room,channelName){
    if(m.kind===1740){const seconds=Number((m.tags||[]).find(t=>t[0]==='timer')?.[1]||0),duration=seconds%86400===0?seconds/86400+' days':seconds%3600===0?seconds/3600+' hours':seconds+' seconds';return '<span class="cc-timer-notice">'+p.enc(seconds?'Set disappearing messages to '+duration:'Turned off disappearing messages')+'</span>';}

    const files=encryptedAttachments(m),publicFiles=publicAttachments(m);
    /* Chat content is relay input. Keep the complete rumor in memory/cache, but never hand a
     * multi-megabyte corrupt field to linkify, preview detection and innerHTML on the launch path. */
    let text=String(m&&m.text||'').slice(0,65536);
    for(const f of [...files,...publicFiles])text=text.split(f.url).join('').trim();
    /* A Webxdc imeta is an attachment, not two messages. When its playable card is available,
     * remove only that exact attachment URL from prose so linkify/link-preview cannot paint a raw
     * Blossom link above the card. Keep the URL when Webxdc is unavailable: it remains the user's
     * download/open fallback instead of turning into an invisible attachment. */
    const mini=webxdcOf(m,room,channelName),canPlayMini=!!(mini&&window.PCWebxdc&&PCWebxdc.cardHtml);
    if(canPlayMini)text=text.split(mini.url).join('').replace(/\s{2,}/g,' ').trim();
    const viewer=p.viewer?p.viewer():{};
    /* No display-label fallback here: `me` is a LABEL ("You" when there is no profile) and feeding
       it in would paint "@you" as the reader's own mention in anybody's message. The identity
       handles — profile name, display name, npub, pubkey — are what a mention can actually be. */
    const prose=t=>{const linked=p.linkify?p.linkify(readableMentions(t)):p.enc(mentionNames(t,p.profOf));
      const escaped=p.renderCustomEmojis?p.renderCustomEmojis(linked,m):linked;
      return paintMentions(escaped,viewerHandles(viewer,''));};
    let body='';
    if(text&&text.indexOf('`')<0)body=`<p>${prose(text)}</p>${p.linkCardHtml?p.linkCardHtml(text):''}`;
    else if(text){
      /* CODE — "concord is not rendering console text": shell output pasted in backticks showed the
         raw ticks. Fenced blocks and `inline` spans are cut out BEFORE linkify/mentions/emoji ever see
         the text, so a URL, an @name or a :shortcode: inside code stays literal and escaped, and a
         URL in code never becomes a link preview. A <pre> never sits inside a <p>. */
      const parts=codeParts(text),out=[];
      for(const part of parts){
        if(part.block){out.push(`<pre class="cc-codeblock"><code>${escCode(part.block)}</code></pre>`);continue;}
        const spans=[];
        const held=part.text.replace(/```([^`\n](?:[^`]|`(?!``))*)```|``([^`\n](?:[^`]|`(?!`))*)``|`([^`\n]+)`/g,(_,a,b,c)=>{spans.push(a??b??c);return '\uE000'+(spans.length-1)+'\uE001';});
        if(!held.trim())continue;
        out.push('<p>'+prose(held).replace(/\uE000(\d+)\uE001/g,(_,i)=>`<code class="cc-code">${escCode(spans[+i])}</code>`)+'</p>');
      }
      const plain=parts.filter(x=>!x.block).map(x=>x.text.replace(/`[^`]*`/g,' ')).join('\n');
      body=out.join('')+(p.linkCardHtml?p.linkCardHtml(plain):'');
    }
    const poll=pollHtml(p,m);
    const publicMedia=publicFiles.map(f=>{const url=p.enc(f.url),label=p.enc(f.name||'attachment');if(f.mime.startsWith('image/')){/* `dim` reserves the box before the picture arrives -- see attachmentBoxStyle */const box=attachmentBoxStyle(f);return `<div class="cc-plain-attachment"${box?' data-cc-sized':''}${box}><img src="${url}" alt="${label}" loading="lazy"></div>`;}if(f.mime.startsWith('video/'))return `<div class="cc-plain-attachment cc-attachment-media"><video src="${url}" controls playsinline preload="metadata" title="Double-click to expand"></video></div>`;if(f.mime.startsWith('audio/'))return `<div class="cc-plain-attachment">${p.trackCard?p.trackCard(f.url,f.name,'metadata'):`<audio src="${url}" controls preload="metadata"></audio>`}</div>`;return `<div class="cc-plain-attachment"><a href="${url}" download="${label}">Download ${label}</a></div>`;}).join('');
    const media=files.map((f,i)=>{ const vis=f.mime.startsWith('image/')||f.mime.startsWith('video/'),got=vis?attachmentCache.get(attachmentKey(f)):null, d=vis?attachmentSize(f):null;
      return `<div class="cc-encrypted-attachment" data-cc-attachment="${p.enc(messageId(m))}" data-cc-attachment-index="${i}"${got?' data-cc-ready="1"':''}>${got?(f.mime.startsWith('video/')?attachmentVideoHtml(p,f,got):attachmentImageHtml(p,f,got))
        :`<span class="cc-attachment-wait${d?' cc-sized':''}"${attachmentBoxStyle(f)}>🔒 Decrypting ${p.enc(f.name||f.mime)}…</span>`}</div>`; }).join('');
    /* Paint the canonical room-aware card with the message. A deferred generic link card could
     * otherwise win the render race and replace Armada's explicit webxdc-topic. */
    const miniCard=canPlayMini?PCWebxdc.cardHtml(mini):'';
    return body+poll+miniCard+publicMedia+media;
  }
  function attachmentFailText(e){
    const stage=e&&e.stage,host=String(e&&e.host||'the file host').replace(/[<>&"]/g,'');
    if(stage==='unreachable')return 'Couldn’t reach '+host+' to download this attachment';
    if(stage==='refused')return 'Couldn’t download this attachment ('+String(e.detail||'').replace(/[<>&"]/g,'')+' from '+host+')';
    if(stage==='too-large')return 'This attachment is too large to open here';
    if(stage==='integrity')return 'This attachment does not match what was sent';
    return 'Could not decrypt attachment';
  }
  async function decryptAttachment(file){
    const ck=file.url+'\0'+file.hash; if(attachmentCache.has(ck))return attachmentCache.get(ck);
    if(attachmentLoads.has(ck))return attachmentLoads.get(ck);
    const work=(async()=>{
      /* SAY WHICH STEP FAILED. Every failure here -- a host that refused the download, one this network cannot reach,
       * a file over the cap, a wrong key -- was shown as "Could not decrypt attachment", which sends people chasing
       * keys when the host was simply unreachable ("now getting 'Could not decrypt attachment' errors on laptop",
       * 2026-10-15). Each step names itself; attachmentFailText turns it into the line under the attachment. */
      const host=(()=>{try{return new URL(file.url).host;}catch(_){return 'the file host';}})(),fail=(stage,detail)=>Object.assign(new Error(stage),{stage,detail,host});
      let res;try{res=await fetch(file.url,{credentials:'omit'});}catch(e){throw fail('unreachable','');}
      if(!res.ok)throw fail('refused','HTTP '+res.status);
      const cipher=await res.arrayBuffer();if(cipher.byteLength>50*1024*1024)throw fail('too-large','');
      let plain;try{const key=await crypto.subtle.importKey('raw',hexBytes(file.key),'AES-GCM',false,['decrypt']);plain=new Uint8Array(await crypto.subtle.decrypt({name:'AES-GCM',iv:hexBytes(file.nonce)},key,cipher));}
      catch(_){throw fail('decrypt','');}
      const hash=bytesHex(await crypto.subtle.digest('SHA-256',plain));if(hash!==file.hash)throw fail('integrity','');const value={url:URL.createObjectURL(new Blob([plain],{type:file.mime})),mime:file.mime,name:file.name};attachmentCache.set(ck,value);while(attachmentCache.size>64){const [old,v]=attachmentCache.entries().next().value;attachmentCache.delete(old);try{URL.revokeObjectURL(v.url);}catch(_){}}return value;})();
    attachmentLoads.set(ck,work);try{return await work;}finally{attachmentLoads.delete(ck);}
  }
  function attachmentLightbox(p,host,url,kind){
    if(!p.openLightbox)return;
    const media=[...(host.closest('.cc-message-body')||document).querySelectorAll('.cc-encrypted-attachment img,.cc-encrypted-attachment video')];
    const items=media.map(el=>({src:el.currentSrc||el.src,kind:el.tagName==='VIDEO'?'video':null})).filter(x=>x.src);
    const here=items.findIndex(x=>x.src===url);
    p.openLightbox(url,kind||null,items.length>1?{items,i:Math.max(0,here)}:null);
  }
  function wireRoomMedia(p){
    if(!p.openLightbox||!document.querySelectorAll)return;
    const media=[...document.querySelectorAll('.cc-message-body img,.cc-message-body video')]
      .filter(el=>!el.closest('.cc-encrypted-attachment'));
    const items=media.map(el=>({src:el.currentSrc||el.src,kind:el.tagName==='VIDEO'?'video':null}))
      .filter(x=>x.src);
    for(const el of media){
      if(el.dataset.ccViewer==='1')continue; el.dataset.ccViewer='1';
      const open=e=>{
        e.preventDefault();e.stopPropagation();
        const url=el.currentSrc||el.src;if(!url)return;
        const here=items.findIndex(x=>x.src===url);
        p.openLightbox(url,el.tagName==='VIDEO'?'video':null,
          items.length>1?{items,i:Math.max(0,here)}:null);
      };
      /* A click on a <video controls> is also how Chromium reports presses on Play, seek, volume
       * and fullscreen. Installing the image-style onclick handler there consumed those controls
       * and made the attachment disappear into a repainted lightbox when somebody pressed Play.
       * Images open on one click; videos keep native controls and use double-click to expand. */
      if(el.tagName==='VIDEO'){
        el.ondblclick=open;
        if(!el.title)el.title='Double-click to expand';
      }else el.onclick=open;
    }
  }
  /* AN IMAGE-HEAVY ROOM DECRYPTED ITS PICTURES ONE AT A TIME, OLDEST FIRST ("decryption slow in image
   * heavy rooms"). The hydrator awaited each attachment in DOM order -- one download, one AES-GCM, one
   * hash, then the next -- so a room opened at its newest message (where a person is looking) painted
   * the pictures they could see LAST: measured on 40 photos (21 MB) over a 150 ms / 8 MB/s link, the
   * two on screen arrived after 8.6 s, behind 38 off-screen ones, with never more than one download in
   * flight. Now an attachment is decrypted when its box comes NEAR the visible part of the pane (an
   * IntersectionObserver on the pane itself, so the margin is measured in the scroller, not the page),
   * at most ATTACHMENT_LANES at a time, the ones on screen first and then by distance. A box that
   * scrolls far away before its turn leaves the queue; one already decrypting finishes into the cache.
   * The finished bytes paint EVERY live box for that file, so a repaint mid-download (a live room
   * repaints constantly) never decrypts twice and never waits for a second pass. */
  const ATTACHMENT_LANES=4,ATTACHMENT_NEAR=1600,attachmentWaiting=new Map(),attachmentHostFile=new WeakMap();
  let attachmentActive=0,attachmentObservers=[],attachmentPumpQueued=false;
  async function hydrateEncryptedAttachments(messages){
    if(!document.querySelectorAll)return;
    const byId=new Map((messages||[]).map(m=>[messageId(m),m]));
    for(const o of attachmentObservers)try{o.disconnect();}catch(_){}
    attachmentObservers=[];const roots=new Map();
    for(const host of document.querySelectorAll('.cc-encrypted-attachment[data-cc-attachment]')){
      const m=byId.get(host.dataset.ccAttachment),file=m&&encryptedAttachments(m)[Number(host.dataset.ccAttachmentIndex)||0];if(!file)continue;
      attachmentHostFile.set(host,file);const ck=attachmentKey(file),got=attachmentCache.get(ck);
      if(got){paintAttachmentHost(host,file,got);continue;}
      if(!window.IntersectionObserver){scheduleAttachment(file,1);continue;}
      const root=host.closest('.cc-messages')||null,key=root||document;
      if(!roots.has(key)){const o=new IntersectionObserver(entries=>attachmentsSeen(entries,root),{root,rootMargin:`${ATTACHMENT_NEAR}px 0px`});roots.set(key,o);attachmentObservers.push(o);}
      roots.get(key).observe(host);
    }
  }
  function attachmentsSeen(entries,root){
    const r=root&&root.isConnected?root.getBoundingClientRect():{top:0,bottom:window.innerHeight||0};
    for(const e of entries){const file=attachmentHostFile.get(e.target);if(!file)continue;const ck=attachmentKey(file);
      if(!e.isIntersecting){const w=attachmentWaiting.get(ck);if(w&&![...attachmentHostsFor(ck)].some(h=>h!==e.target&&nearPane(h,root)))attachmentWaiting.delete(ck);continue;}
      const q=e.boundingClientRect,distance=q.bottom>r.top&&q.top<r.bottom?0:Math.min(Math.abs(q.top-r.bottom),Math.abs(r.top-q.bottom));
      scheduleAttachment(file,distance);}
  }
  function nearPane(host,root){ if(!host.isConnected)return false;const r=root&&root.isConnected?root.getBoundingClientRect():{top:0,bottom:window.innerHeight||0},q=host.getBoundingClientRect();return q.bottom>r.top-ATTACHMENT_NEAR&&q.top<r.bottom+ATTACHMENT_NEAR; }
  function attachmentHostsFor(ck){ return [...document.querySelectorAll('.cc-encrypted-attachment[data-cc-attachment]')].filter(h=>{const f=attachmentHostFile.get(h);return f&&attachmentKey(f)===ck;}); }
  /* `distance` is how far the box is from the visible pane in px (0 = on screen): the queue's order. */
  function scheduleAttachment(file,distance){
    const ck=attachmentKey(file);
    if(attachmentCache.has(ck)){const got=attachmentCache.get(ck);for(const h of attachmentHostsFor(ck))paintAttachmentHost(h,file,got);return;}
    if(attachmentLoads.has(ck))return;
    const w=attachmentWaiting.get(ck);if(w){w.distance=Math.min(w.distance,distance);return;}
    attachmentWaiting.set(ck,{file,distance});
    /* Pumped once the WHOLE batch is queued: an observer reports its boxes top-down in one callback,
     * and starting lanes per entry handed them to the oldest off-screen boxes before the on-screen
     * one at the bottom was even in the queue. */
    if(!attachmentPumpQueued){attachmentPumpQueued=true;Promise.resolve().then(()=>{attachmentPumpQueued=false;pumpAttachments();});}
  }
  function pumpAttachments(){
    while(attachmentActive<ATTACHMENT_LANES&&attachmentWaiting.size){
      let best=null;for(const [ck,w] of attachmentWaiting)if(!best||w.distance<best[1].distance)best=[ck,w];
      const [ck,{file}]=best;attachmentWaiting.delete(ck);attachmentActive++;
      decryptAttachment(file).then(async got=>{await learnImageSize(file,got);for(const h of attachmentHostsFor(ck))paintAttachmentHost(h,file,got);},
        e=>{try{console.warn('[concord] attachment',e&&e.stage,e&&e.host,e&&e.detail);}catch(_){}
          for(const h of attachmentHostsFor(ck))keepPlaceAround(h,()=>{h.innerHTML='<span class="cc-attachment-error">'+attachmentFailText(e)+'</span> <button type="button" class="btn btn-ghost small cc-attachment-retry">Retry</button>';
            const b=h.querySelector('.cc-attachment-retry');if(b)b.onclick=()=>{h.innerHTML='';scheduleAttachment(file,0);};});})
        .finally(()=>{attachmentActive--;pumpAttachments();});
    }
  }
  /* A picture with no `dim` is DECODED BEFORE it replaces its placeholder, so its box arrives at its
   * real size in one step. Swapped in undecoded, the <img> is 0 px tall for a frame: the pane SHRINKS
   * under a reader pinned to the bottom, the browser clamps scrollTop, and that clamp's scroll event --
   * read a frame later, after the picture has grown -- looked like the reader scrolling up, unpinning
   * the room (measured: 6 of 8 loads of a 15-photo room left the reader 4,858 px above the bottom). */
  async function learnImageSize(file,got){
    if(!got||!String(got.mime||'').startsWith('image/')||attachmentSize(file))return;
    try{const im=new Image();im.src=got.url;await im.decode();if(im.naturalWidth&&im.naturalHeight)attachmentDims.set(attachmentKey(file),{w:im.naturalWidth,h:im.naturalHeight});}catch(_){}
  }
  /* A PICTURE THAT LANDS MOVES NOTHING THE READER CAN SEE. A decrypted attachment replaces its
   * placeholder in a promise task, and the scroller's own correction (watchPinnedRoomGrowth) is a
   * ResizeObserver that answers a frame later -- so for one frame a reader at the newest message saw
   * the room 154px above it, then a jump back: measured on a 390px phone opening a channel, a layout
   * shift of 0.15 from one picture with no `dim`. The place is now put back in the SAME task as the
   * swap: pinned stays at the bottom, and growth wholly above an unpinned reader is added to their
   * offset. A hand on the scroller is left alone (writing scrollTop kills a fling); the observer
   * still corrects once it lets go. */
  function keepPlaceAround(host,fn){
    const box=host&&host.closest?host.closest('.cc-messages'):null;
    if(!box||chatHandOn()){fn();return;}
    const key=box.dataset&&box.dataset.ccScrollKey||scrollKey(),st=readScroll(key),top0=box.scrollTop,h0=host.offsetHeight,
      atBottom=box.scrollHeight-box.clientHeight-box.scrollTop<=2,above=host.getBoundingClientRect().bottom<=box.getBoundingClientRect().top+1;
    fn();
    if(!box.isConnected||key!==scrollKey())return;
    if(st.pinned!==false||atBottom){if(Math.abs(box.scrollHeight-box.clientHeight-box.scrollTop)>1)setProgrammaticScroll(box,box.scrollHeight);}
    else if(above){const d=host.offsetHeight-h0;if(d)setProgrammaticScroll(box,top0+d);}
  }
  function paintAttachmentHost(host,file,got){
    if(!host.isConnected)return;
    keepPlaceAround(host,()=>paintAttachmentHostNow(host,file,got));
  }
  function paintAttachmentHostNow(host,file,got){
    try{const p=PC(),url=p.enc(got.url),label=p.enc(got.name||'attachment');if(got.mime.startsWith('image/')){if(host.dataset.ccReady!=='1'){host.innerHTML=attachmentImageHtml(p,file,got);host.dataset.ccReady='1';}const img=host.querySelector('img');if(img&&!attachmentDims.has(attachmentKey(file))){const note=()=>{if(img.naturalWidth&&img.naturalHeight)attachmentDims.set(attachmentKey(file),{w:img.naturalWidth,h:img.naturalHeight});};if(img.complete)note();else img.addEventListener('load',note,{once:true});}const open=host.querySelector('.cc-attachment-open');if(open)open.onclick=e=>{e.preventDefault();e.stopPropagation();attachmentLightbox(p,host,got.url,null);};}else if(got.mime.startsWith('video/')){if(host.dataset.ccReady!=='1'||!host.querySelector('video')){host.innerHTML=attachmentVideoHtml(p,file,got);host.dataset.ccReady='1';}const vid0=host.querySelector('video');if(vid0&&!attachmentDims.has(attachmentKey(file))){const note=()=>{if(vid0.videoWidth&&vid0.videoHeight)attachmentDims.set(attachmentKey(file),{w:vid0.videoWidth,h:vid0.videoHeight});};if(vid0.readyState>=1)note();else vid0.addEventListener('loadedmetadata',note,{once:true});}const openVideo=e=>{e.preventDefault();e.stopPropagation();attachmentLightbox(p,host,got.url,'video');};const video=host.querySelector('video');if(video)video.ondblclick=openVideo;const open=host.querySelector('.cc-attachment-expand');if(open)open.onclick=openVideo;}else if(got.mime.startsWith('audio/')){/* Built ONCE, like the video: a repaint that rewrote it stopped a playing track. */if(host.dataset.ccReady!=='1'||!host.querySelector('audio')){host.innerHTML=p.trackCard?p.trackCard(got.url,got.name||file.name,'metadata'):`<audio src="${url}" controls preload="metadata"></audio>`;host.dataset.ccReady='1';}}else host.innerHTML=`<a href="${url}" download="${label}">Download ${label}</a>`;}catch(e){console.warn('Concord attachment paint failed',e);}
  }
  function channelStarKey(room,name){ return `${STAR_KEY}${accountPk()}.${room&&(room.communityId||room.naddr||room.url)||'unknown'}:${name||'general'}`; }
  function channelStarred(room,name){ try{return localStorage.getItem(channelStarKey(room,name))==='1';}catch(_){return false;} }
  function setChannelStarred(room,name,on){ try{if(on)localStorage.setItem(channelStarKey(room,name),'1');else localStorage.removeItem(channelStarKey(room,name));}catch(_){} }
  function orderedChannels(room){ return channelsOf(room).map((channel,index)=>({channel,index,starred:channelStarred(room,channel.name)})).sort((a,b)=>Number(b.starred)-Number(a.starred)||a.index-b.index).map(x=>x.channel); }
  function canManageChannels(p,room){
    const me=p.viewer?p.viewer().pubkey:'',owner=String(room&&room.cord&&room.cord.bundle&&(room.cord.bundle.owner||'')||'');
    return !!(room&&room.cord&&!room.local&&room.protocol!=='nip29'&&me&&(owner===me||memberMay(room,me,'MANAGE_CHANNELS')));
  }
  function channelRowsHtml(p,room,channels){
    const manage=canManageChannels(p,room),S=window.PCCordSpaces,callable=!!(S&&room&&room.cord&&!room.local&&room.protocol!=='nip29');
    return (channels||[]).map(c=>{const people=callable&&c.view!=='forum'?voicePeople(room,c.id):[],here=callable&&voiceInCall(room,c.id);return `<div class="cc-channel-row${channelStarred(room,c.name)?' starred':''}"><button class="cc-channel${(state.channel||'general')===c.name?' active':''}${testMessages(channelStoreId(room,c.name)).some(m=>(Number(m.at)||0)>seenAt(room,c.name))||gitUnread(room,c)?' unread':''}" data-cc-channel="${p.enc(c.name)}">${S?S.glyphHtml(c.view,people.length>0):'<span class="cc-channel-kind" aria-hidden="true">#</span>'}<span class="cc-channel-name">${p.enc(c.name)}</span>${window.PCConcordGit&&PCConcordGit.channelHasGit(c)?'<svg class="ic ccg-chan-badge" role="img" aria-label="Connected to a git repository"><use href="#i-bug"></use></svg>':''}${c.private?'<svg class="ic cc-channel-lock" role="img" aria-label="Private channel"><use href="#i-lock"></use></svg>':''}</button><button class="cc-channel-star" data-cc-star="${p.enc(c.name)}" aria-pressed="${channelStarred(room,c.name)}" title="${channelStarred(room,c.name)?'Remove from starred channels':'Star channel'}" aria-label="${channelStarred(room,c.name)?'Unstar':'Star'} #${p.enc(c.name)}"><svg class="ic${channelStarred(room,c.name)?' cc-starred':''}" aria-hidden="true"><use href="#i-star"></use></svg></button>${callable?S.voiceJoinHtml(c,people.length>0,here):''}${manage&&c.id?`<button class="cc-channel-more" data-cc-channel-more="${p.enc(c.id)}" title="Channel options" aria-label="Options for #${p.enc(c.name)}"><svg class="ic" aria-hidden="true"><use href="#i-more"></use></svg></button>`:''}</div>${callable?S.voicePeopleHtml(p,c.id,people):''}`;}).join('');
  }
  /* Offer channel creation only to the owner or folded MANAGE_CHANNELS staff.
   * Local sandboxes and NIP-29 groups do not have this CORD control plane. */
  function communityPermission(p,room,key){
    if(room?.local)return true;
    const reader=window.PosterCordReader,viewer=p.viewer?.().pubkey,bundle=room?.cord?.bundle;
    if(!viewer||!bundle||room.protocol==='nip29')return false;
    try{const info=reader?.inspectControl?.(bundle,roomControls.get(room.communityId||room.naddr)||[]);return (info?.[key]||[bundle.owner]).includes(viewer);}catch(_){return false;}
  }
  function communityTimer(room){
    try{const wraps=roomControls.get(room.communityId||room.naddr);if(wraps)return window.PosterCordReader.inspectControl(room.cord.bundle,wraps).message_expiration||0;}catch(_){}
    return Number.isSafeInteger(room?.message_expiration)&&room.message_expiration>0?room.message_expiration:0;
  }
  /* KEEP HISTORY -- a PER-COMMUNITY policy for how long PosterChan keeps this community's messages on a
   * member's device. CORD-02 §6 reserves top-level metadata for the protocol, so it lives in `custom` under
   * a prefixed key and other clients carry it through untouched. 0 / absent / malformed = FOREVER (the
   * keeping direction). It is a local-cache policy ONLY: it never adds `expiration` tags and never sends a
   * deletion (CORD-08's timer is the protocol's tool for that, and still wins over it: an expired message is
   * purged whatever this says). docs/CONCORD_RETENTION.md. */
  const RETENTION_KEY='posterchan/history_retention';
  function retentionDays(v){const n=Number(v);return Number.isSafeInteger(n)&&n>0&&n<=36500?n:0;}
  function communityRetention(room){
    try{const wraps=roomControls.get(room.communityId||room.naddr);if(wraps&&room.cord?.bundle)return retentionDays((window.PosterCordReader.inspectControl(room.cord.bundle,wraps).custom||{})[RETENTION_KEY]);}catch(_){}
    return retentionDays(room?.history_retention);
  }
  function retentionSettingsHtml(p,room){
    const days=communityRetention(room),options=[[0,'Forever'],[365,'1 year'],[90,'90 days'],[30,'30 days']];
    if(days&&!options.some(([v])=>v===days))options.push([days,days+' days (current)']);
    return '<label class="cc-label" for="cc-history-retention">Keep history</label><select class="input" id="cc-history-retention"'+(communityPermission(p,room,'metadataManagers')?'':' disabled')+'>'+options.map(([value,label])=>'<option value="'+value+'"'+(value===days?' selected':'')+'>'+p.enc(label)+'</option>').join('')+'</select><p class="cc-member-help">How long members’ PosterChan apps keep this community’s messages on their devices. Forever never clears them. Disappearing messages, when on, still removes a message when it expires.</p>';
  }
  function timerSettingsHtml(p,room){
    const timer=communityTimer(room),options=[[0,'Off'],[86400,'1 day'],[604800,'1 week'],[2592000,'30 days'],[7776000,'90 days']];
    if(timer&&!options.some(([value])=>value===timer))options.push([timer,timer+' seconds (current)']);
    return '<label class="cc-label" for="cc-message-expiration">Disappearing messages</label><select class="input" id="cc-message-expiration"'+(communityPermission(p,room,'metadataManagers')?'':' disabled')+'>'+options.map(([value,label])=>'<option value="'+value+'"'+(value===timer?' selected':'')+'>'+p.enc(label)+'</option>').join('')+'</select><p class="cc-member-help">Applies to new messages in every channel. Existing messages keep their original timer.</p>';
  }
  async function ownedInviteContext(p,room,creationCurrent=null){
    const base=p.cordDirectContext(),identity=roomIdentity(room),root=room.cord?.bundle?.community_root;
    const current=()=>base.isCurrent()&&(creationCurrent?creationCurrent():saved().some(r=>roomIdentity(r)===identity&&r.cord?.bundle?.community_root===root));
    const api=await p.cordInviteLinksModule();if(!current())throw new Error('Concord membership changed');
    const relays=[...new Set([...roomRelays(room.cord.bundle),...CORD_RELAYS])].slice(0,8);
    const context={...base,isCurrent:current,
      query:async(kind,author,bootstrap=[])=>{
        if(!current())throw new Error('Concord membership changed');
        const targets=[...new Set([...bootstrap,...relays])].slice(0,8),report={};
        const events=await p.relayQueryFrom(targets,[{kinds:[kind],authors:[author],...(kind===33301?{'#d':['']}:{}),limit:65}],{timeout:10000,max:8,exact:true,allowBlocked:true,purpose:'concord owned invitation',report,urgent:true});
        if(!current())throw new Error('Concord membership changed');
        const missing=unanswered(targets,report);
        return {events,complete:targets.length>0&&!missing.length,missing};
      },
      publish:async(event,bootstrap=[])=>{
        if(!current())throw new Error('Concord membership changed');
        const ack=await p.relayPublishRoom([...new Set([...bootstrap,...relays])].slice(0,8),event);
        if(!current())throw new Error('Concord membership changed');
        if(!ack?.ok)throw new Error('No relay accepted the invitation update');
        return true;
      }};
    return {api,context,relays};
  }
  /* THESE READS GATE A WRITE OF A REPLACEABLE LIST, SO THEY ASK EVERY RELAY (`urgent`).
   *
   * `Relay.queryFrom` skips a relay, with no error, while another read of the same slot is in flight
   * there and for a while after it failed once -- right for background sweeps, wrong here: a skipped
   * relay is reported as one that did not answer, so the Invite List read was "incomplete" without
   * anybody being asked, and creating a community failed ("Invite List sync is incomplete"). Every
   * Armada/Vector community shares these relays and is swept in the background, so that was the
   * ordinary case. An urgent read opens its own socket; a relay that is asked and does not answer
   * still makes the read incomplete, and the error names it. */
  function inviteSyncError(what,missing,api=null){
    if(api&&api.incompleteError)return api.incompleteError(what,missing);
    const list=[...new Set((missing||[]).filter(Boolean))],error=new Error(what+' sync is incomplete'+(list.length?' — '+list.length+' relay(s) did not answer ('+list.join(', ')+')':'')+'; retry before changing it');
    error.incomplete=true;error.missing=list;return error;
  }
  function unanswered(relays,report){return (relays||[]).filter(url=>!report.ok?.includes(url));}
  async function refreshOwnedInviteLinks(p,room){
    const {api,context,relays}=await ownedInviteContext(p,room),reader=window.PosterCordReader,bundle=room.cord.bundle;
    const loadKey=room.communityId||room.naddr,known=roomControls.get(loadKey)||[],seed=reader.inspectControl(bundle,known);
    const report={};
    const fresh=await cordQuery(p,relays,[{kinds:[1059],authors:seed.controlPubkeys,limit:1000}],{timeout:10000,max:8,plane:cordPlaneContext(p,bundle,known,room),report,urgent:true,home:false});
    if(fresh.length>=1000||report.failed?.length||report.held?.length||report.cooled?.length||report.unheld?.length||!relays.every(url=>report.ok?.includes(url)))throw inviteSyncError('Current invitation registry',unanswered(relays,report));
    if(!context.isCurrent())throw new Error('Concord membership changed');
    const wraps=mergeEnvelopes(known,fresh),info=reader.inspectControl(bundle,wraps);
    roomControls.set(loadKey,wraps);if(info.relaysAuthoritative)relays.splice(0,relays.length,...[...new Set([...info.relays,...CORD_RELAYS])].slice(0,8));const plane=cordPlaneContext(p,bundle,wraps,room),guarded={...context,isCurrent:()=>context.isCurrent()&&plane.current()};
    const list=await api.readList(guarded);if(!list.complete)throw inviteSyncError('Invite List',list.missing,api);
    const live=new Set(info.registriesByCreator?.[context.pubkey]||[]);let refreshed=0;
    for(const entry of list.list.entries.filter(e=>e.community_id===bundle.community_id)){
      if(!context.isCurrent())throw new Error('Concord membership changed');
      const status=await api.linkState(entry,guarded);
      if(!status.complete)throw inviteSyncError('Invitation relay',status.missing,api);
      if(status.retired){await api.forget(entry,guarded);continue;}
      if(!live.has(api.details(entry).pubkey))continue;
      const event=await api.refreshEvent(entry,{...bundle,channels:[],relays:info.relays},status.events,guarded);
      if(!guarded.isCurrent())throw new Error('Concord invitation permission changed');
      await context.publish(event,api.details(entry).relays);refreshed++;
    }
    return refreshed;
  }
  async function ownedInviteRegistry(p,room,scope){
    const reader=window.PosterCordReader,bundle=room.cord.bundle,key=room.communityId||room.naddr;
    const known=roomControls.get(key)||[],seed=reader.inspectControl(bundle,known),report={};
    const fresh=await cordQuery(p,scope.relays,[{kinds:[1059],authors:seed.controlPubkeys,limit:1000}],{timeout:10000,max:8,plane:cordPlaneContext(p,bundle,known,room),report,urgent:true,home:false});
    if(!scope.context.isCurrent())throw new Error('Concord membership changed');
    if(fresh.length>=1000||report.failed?.length||report.held?.length||report.cooled?.length||report.unheld?.length||!scope.relays.every(url=>report.ok?.includes(url)))throw inviteSyncError('Current invitation registry',unanswered(scope.relays,report));
    const wraps=mergeEnvelopes(known,fresh),info=reader.inspectControl(bundle,wraps);
    if(!info.inviteCreators.includes(scope.context.pubkey))throw new Error('Your account cannot manage invite links');
    roomControls.set(key,wraps);
    if(info.relaysAuthoritative)scope.relays.splice(0,scope.relays.length,...[...new Set([...info.relays,...CORD_RELAYS])].slice(0,8));
    return {wraps,info,key,plane:cordPlaneContext(p,bundle,wraps,room)};
  }
  const ownedInviteWrites=new Map();
  function changeOwnedInviteLink(p,room,entry=null,options={}){
    const owner=p.viewer?.().pubkey,key=JSON.stringify([owner,roomIdentity(room)]),prior=ownedInviteWrites.get(key)||Promise.resolve();
    const job=prior.catch(()=>{}).then(()=>{if(p.viewer?.().pubkey!==owner)throw new Error('Concord account changed');return changeOwnedInviteLinkNow(p,room,entry,options);});
    ownedInviteWrites.set(key,job);job.finally(()=>{if(ownedInviteWrites.get(key)===job)ownedInviteWrites.delete(key);}).catch(()=>{});return job;
  }
  async function changeOwnedInviteLinkNow(p,room,entry=null,{adopt=false}={}){
    const scope=await ownedInviteContext(p,room),{api,context,relays}=scope,reader=window.PosterCordReader;
    const registry=await ownedInviteRegistry(p,room,scope),links=new Set(registry.info.registriesByCreator[context.pubkey]||[]);
    const assertCurrent=()=>{if(!context.isCurrent()||!registry.plane.current())throw new Error('Concord invitation permission changed');};
    const guarded={...context,isCurrent:()=>context.isCurrent()&&registry.plane.current()};
    let made,retirement=null;
    if(entry&&!adopt){
      if(entry.community_id!==room.cord.bundle.community_id||!links.has(api.details(entry).pubkey))throw new Error('This account does not own an active registry entry');
      const state=await api.linkState(entry,guarded);if(!state.complete)throw inviteSyncError('Invitation relay',state.missing,api);
      made={entry,event:api.revokeEvent(entry,state.events,guarded)};links.delete(api.details(entry).pubkey);
      if(registry.info.liveInviteLinks.every(pk=>pk===api.details(entry).pubkey)){
        if(!window.PCConcord.reviewRefoundingRecipients||!window.PCConcord.refoundRoom||window.PCConcord.refoundingBeforeEvents!==true)throw new Error('Retiring the final link requires community refounding support');
        retirement=await window.PCConcord.reviewRefoundingRecipients(p,room);assertCurrent();if(!retirement)return null;
      }
    }else{
      if(adopt){
        if(entry.community_id!==room.cord.bundle.community_id||context.pubkey!==room.cord.bundle.owner)throw new Error('Only the original creator can recover this link');
        const status=await api.linkState(entry,guarded);if(!status.complete||status.retired)throw new Error('Existing link is retired or has not finished syncing');
        made={entry,event:await api.refreshEvent(entry,{...room.cord.bundle,channels:[],relays:registry.info.relays},status.events,guarded)};
      }else made=await api.create({...room.cord.bundle,channels:[],relays:registry.info.relays},guarded,{base:publicInviteBase(p)});
      // Preserve the signing key before publishing a usable link, so failed registry delivery is recoverable.
      await api.remember(made.entry,guarded);links.add(api.details(made.entry).pubkey);
    }
    assertCurrent();const update=await reader.createInviteRegistryWrap(room.cord.bundle,registry.wraps,[...links],context.pubkey,context.sign);assertCurrent();
    if(retirement){await window.PCConcord.refoundRoom(p,room,{...retirement,controlUpdates:[update.wrap],beforeEvents:[made.event],beforeRelays:[...new Set([...api.details(made.entry).relays,...relays])].slice(0,8)});}
    else{
      await guarded.publish(made.event,api.details(made.entry).relays);assertCurrent();
      const ack=await p.relayPublishRoom(relays,update.wrap,cordPlaneAuth(p,registry.plane,update.wrap.pubkey,relays));
      assertCurrent();if(!ack?.ok)throw new Error('Link event was published but its registry update needs retry');
      roomControls.set(registry.key,mergeEnvelopes(registry.wraps,[update.wrap]));
    }
    // Refounding changes the root; use a fresh captured context for terminal list cleanup.
    if(entry&&!adopt){const latest=saved().find(r=>roomIdentity(r)===roomIdentity(room));if(!latest||p.viewer?.().pubkey!==context.pubkey)throw new Error('Concord account changed');await api.forget(entry,(await ownedInviteContext(p,latest)).context);}
    return made.entry;
  }
  async function showOwnedInviteLinks(p,room){
    const scope=await ownedInviteContext(p,room),read=await scope.api.readList(scope.context);
    if(!scope.context.isCurrent())return;
    p.modal('<h3>Community invite links</h3><p>Links let anyone with the URL join. Retiring the last active link also rotates community keys.</p><div id="cc-owned-links"></div><button class="btn" id="cc-mint-link">Create invite link</button>',root=>{
      const list=root.querySelector('#cc-owned-links');
      const entries=read.list.entries.filter(e=>e.community_id===room.cord.bundle.community_id);
      if(!entries.length)list.textContent='No links created by this account.';
      if(room.url){const copy=document.createElement('button');copy.className='btn';copy.textContent='Copy current community link';copy.onclick=()=>p.copyValue(shareableInviteUrl(p,room.url));list.appendChild(copy);}
      const secrets=room.cord?.secrets;
      if(secrets?.token&&secrets?.linkSignerSk&&room.url&&room.cord.bundle.owner===scope.context.pubkey&&!entries.some(e=>e.token===secrets.token)&&!read.list.tombstones.some(e=>e.token===secrets.token)){
        const recover=document.createElement('button');recover.className='btn';recover.textContent='Manage original link';recover.onclick=async()=>{recover.disabled=true;try{await changeOwnedInviteLink(p,room,{token:secrets.token,signer_sk:secrets.linkSignerSk,community_id:room.cord.bundle.community_id,url:room.url},{adopt:true});p.closeModal();await showOwnedInviteLinks(p,room);}catch(e){p.toast(e.message||String(e));recover.disabled=false;}};list.appendChild(recover);
      }
      for(const entry of entries){const row=document.createElement('div'),label=document.createElement('span');label.textContent=entry.label||'Invite link';row.appendChild(label);
        for(const [text,action] of [['Copy',()=>p.copyValue(shareableInviteUrl(p,entry.url))],['Retire',()=>changeOwnedInviteLink(p,room,entry)]]){const button=document.createElement('button');button.className='btn';button.textContent=text;button.onclick=async()=>{button.disabled=true;try{const result=await action();if(text==='Retire'&&result){p.closeModal();p.toast('Invitation retired');}}catch(e){p.toast(e.message||String(e));}finally{button.disabled=false;}};row.appendChild(button);}list.appendChild(row);}
      const mintButton=root.querySelector('#cc-mint-link');mintButton.disabled=!communityPermission(p,room,'inviteCreators');if(mintButton.disabled)mintButton.title='Only authorized community staff can create links';
      mintButton.onclick=async event=>{const button=event.currentTarget;button.disabled=true;try{const entry=await changeOwnedInviteLink(p,room);if(entry){p.copyValue(shareableInviteUrl(p,entry.url));p.closeModal();p.toast('Invitation created and copied');}}catch(e){p.toast(e.message||String(e));}finally{button.disabled=false;}};
    });
  }
  /* PUBLISH OUR OWN GUESTBOOK JOIN / LEAVE (CORD-02 §5, CORD-05 §6). Best effort by design: the
   * guestbook is off-consensus, a missed Join heals by observation the moment we post, and joining or
   * leaving must never fail because a relay did. A Leave goes out BEFORE the keys are dropped -- it
   * cannot be wrapped afterwards. Returns whether a relay accepted it. */
  async function publishGuestbook(p,room,verb){
    try{
      const reader=window.PosterCordReader,bundle=room&&room.cord&&room.cord.bundle,me=p.viewer&&p.viewer().pubkey;
      if(!reader||!reader.createGuestbookWrap||!bundle||!me||room.local)return false;
      const hex=v=>{const t=String(v||'').trim();if(/^[0-9a-f]{64}$/i.test(t))return t.toLowerCase();try{const d=window.NostrTools.nip19.decode(t);return d.type==='npub'?d.data:'';}catch(_){return '';}};
      const invite=verb==='join'&&bundle.creator_npub?{creator:hex(bundle.creator_npub),label:bundle.label||''}:null;
      const made=await reader.createGuestbookWrap(bundle,verb,me,p.signTemplate,invite),relays=roomRelays(bundle);
      const scope=cordPlaneContext(p,bundle,roomControls.get(room.communityId||room.naddr)||[],room);
      const r=await p.relayPublishRoom(relays,made.wrap,cordPlaneAuth(p,scope,made.wrap.pubkey,relays));
      return !!(r&&r.ok);
    }catch(e){ try{console.warn('Concord guestbook '+verb+' not published',e);}catch(_){} return false; }
  }
  /* A COMMUNITY ICON IS AN ENCRYPTED ImageRef {url,key,nonce,hash,ext} (CORD-02 metadata `icon`,
   * which is what Armada and Vector read), never a plain `picture` URL — Vector showed no icon at all
   * on a room we created. Re-drawn through a canvas first: that drops EXIF (a phone photo's GPS
   * position would otherwise ride along, readable by every member) and caps it at 512px. */
  async function sealImageRef(p,url){
    const res=await fetch(url,{credentials:'omit'}); if(!res.ok)throw new Error('could not read the icon image');
    const src=await res.blob();
    /* AN ANIMATED GIF STAYS ANIMATED (Vyram, 2026-10-21: "Do gifs when used as a concord community avatar
     * animate?"). The canvas below flattens to ONE PNG frame. A GIF needs none of what the canvas is for:
     * the format has no EXIF block (no GPS position to strip), and members display the decrypted bytes
     * as-is (imageMime sniffs GIF), so its own bytes are sealed. Bounded at 2 MB — a bigger one is still
     * redrawn, as a still, rather than every member downloading it on every icon paint. */
    { const head=new Uint8Array(await src.slice(0,6).arrayBuffer());
      if(head[0]===0x47&&head[1]===0x49&&head[2]===0x46&&src.size<=2*1024*1024)return sealIconBytes(p,new Uint8Array(await src.arrayBuffer()),'gif'); }
    const bm=await createImageBitmap(src),scale=Math.min(1,512/Math.max(bm.width,bm.height)||1);
    const w=Math.max(1,Math.round(bm.width*scale)),h=Math.max(1,Math.round(bm.height*scale));
    const canvas=typeof OffscreenCanvas==='function'?new OffscreenCanvas(w,h):Object.assign(document.createElement('canvas'),{width:w,height:h});
    canvas.getContext('2d').drawImage(bm,0,0,w,h); if(bm.close)bm.close();
    const png=canvas.convertToBlob?await canvas.convertToBlob({type:'image/png'}):await new Promise(r=>canvas.toBlob(r,'image/png'));
    return sealIconBytes(p,new Uint8Array(await png.arrayBuffer()),'png');
  }
  async function sealIconBytes(p,plain,ext){
    const keyBytes=crypto.getRandomValues(new Uint8Array(32)),nonce=crypto.getRandomValues(new Uint8Array(16));
    const key=await crypto.subtle.importKey('raw',keyBytes,'AES-GCM',false,['encrypt']);
    const cipher=new Uint8Array(await crypto.subtle.encrypt({name:'AES-GCM',iv:nonce},key,plain));
    const stored=await p.uploadBlob(new Blob([cipher],{type:'application/octet-stream'}),{keep:true});
    return {url:stored,key:bytesHex(keyBytes),nonce:bytesHex(nonce),hash:bytesHex(await crypto.subtle.digest('SHA-256',plain)),ext};
  }
  /* AN ICON UPLOAD IS SAID OUT LOUD, AND IT STARTS AT THE PICK. "Upload image…" used to put a blob: URL
   * in the box and nothing else: the encrypt + upload ran inside Save, with no sign it had started,
   * finished or failed ("just hit Save changes and hope it uploads"). Now the pick starts it, the line
   * under the button says uploading / uploaded (with the picture) / failed + Retry, and Save is held
   * while it runs or after it failed -- a community is never saved naming an icon that did not land.
   * Save reuses the sealed ref, so the bytes go up once. Keyed on the blob: URL the box holds, so a
   * typed URL or a cleared box is simply not an upload any more. */
  const ICON_BOXES=['cc-settings-icon','cc-community-icon'],iconUploads=new Map();
  function startIconUpload(p,blobUrl){
    const job={state:'uploading',ref:null,error:''};
    job.promise=sealImageRef(p,blobUrl).then(ref=>{job.state='done';job.ref=ref;return ref;},
      error=>{job.state='failed';job.error=String(error&&error.message||error||'upload failed');throw error;});
    job.promise.catch(()=>{}).finally(()=>{if(iconUploads.get(blobUrl)===job)ICON_BOXES.forEach(paintIconStatus);});
    iconUploads.set(blobUrl,job);ICON_BOXES.forEach(paintIconStatus);return job;
  }
  async function finishedIconUpload(blobUrl){
    const job=iconUploads.get(blobUrl);
    try{return await job.promise;}catch(e){throw new Error('the community icon did not upload ('+job.error+') — retry it or choose another image');}
  }
  function paintIconStatus(id){
    const q=sel=>document.querySelector?document.querySelector(sel):null;
    const box=q('#'+id),line=q('#'+id+'-status');if(!box||!line)return;
    const save=q(id==='cc-settings-icon'?'#cc-settings-save':'#cc-create-go');
    const job=iconUploads.get(String(box.value||'').trim());
    const held=!!job&&job.state!=='done';
    if(save){
      if(held){save.disabled=true;save.dataset.ccIconHeld='1';save.title=job.state==='uploading'?'Waiting for the icon upload to finish':'The icon did not upload — retry it or choose another image';}
      else if(save.dataset.ccIconHeld){delete save.dataset.ccIconHeld;save.disabled=false;save.removeAttribute('title');}
    }
    if(!job){line.hidden=true;line.textContent='';line.className='cc-icon-status';return;}
    line.hidden=false;line.className='cc-icon-status '+job.state;line.textContent='';
    const img=document.createElement('img');img.src=box.value;img.alt='';line.appendChild(img);
    const text=document.createElement('span');
    text.textContent=job.state==='uploading'?'Uploading icon…':job.state==='done'?'✓ Icon uploaded — Save to apply it':'Icon upload failed: '+job.error;
    line.appendChild(text);
    if(job.state==='failed'){const retry=document.createElement('button');retry.type='button';retry.className='btn btn-ghost cc-icon-retry';retry.textContent='Retry';
      retry.onclick=()=>{const p=PC();if(p)startIconUpload(p,String(box.value||'').trim());};line.appendChild(retry);}
  }
  async function saveCommunitySettings(p,room,values){
    const owner=p.viewer?.().pubkey,identity=roomIdentity(room),loadKey=room.communityId||room.naddr;
    const stillHere=()=>p.viewer?.().pubkey===owner&&saved().some(r=>roomIdentity(r)===identity);
    let timerChanged=false,noticesFailed=0,timer=communityTimer(room),keep=communityRetention(room);
    if(!room.local){
      const reader=window.PosterCordReader,bundle=room.cord?.bundle,relays=roomRelays(bundle);
      if(!reader?.createMetadataWrap||!bundle)throw new Error('community profile is not ready');
      /* THE WRITE IS BUILT ON A FRESH READ, NEVER ON THE CACHE ALONE. The metadata write starts from the
       * newest metadata it can SEE (createMetadataWrap's priorBody) and replaces it, so a cached control set
       * that missed the latest edition rewrote the community from an older one -- "uploading banner blew
       * away all the community settings". Fresh read UNION cache (never fewer events than we had); and a
       * read that came back with nothing is "could not ask" -- a community always has its creation controls
       * -- so nothing is written on the strength of it. */
      values={...values};
      if(Object.prototype.hasOwnProperty.call(values,'icon')){
        /* An unchanged icon is not re-uploaded: the settings box holds the room's current icon,
         * which for an encrypted one is the blob: URL of the decrypted copy. */
        if(values.icon===room.icon)delete values.icon;
        else if(iconUploads.has(values.icon)){ values.iconRef=await finishedIconUpload(values.icon); }
        else if(/^(https?|blob):/i.test(values.icon||'')){
          try{ values.iconRef=await sealImageRef(p,values.icon); }
          catch(e){ if(/^blob:/i.test(values.icon))throw e; /* a URL we cannot fetch (CORS): the plain `picture` fallback */ }
        }
      }
      /* CONTROLS THAT MOVED WHILE SIGNING ARE A REASON TO REBUILD, NOT TO GIVE UP. Signing can take seconds (a
       * remote signer, a phone) and Concord's background refresh re-stores the control set meanwhile, so the
       * old check -- abort on ANY change -- failed the save every time on an active community ("can't save
       * community settings, concord membership changed while signing"). A changed MEMBERSHIP still stops it;
       * moved controls mean the write may rest on stale metadata, so it is read, folded and signed again
       * (nothing was published yet), at most three times. */
      let wraps,scope,before,made;
      for(let attempt=0;;attempt++){
        const seed=reader.inspectControl(bundle,[]);
        const fresh=await cordQuery(p,relays,[{kinds:[1059],authors:seed.controlPubkeys,limit:1000}],{timeout:10000,max:8,urgent:true,plane:cordPlaneContext(p,bundle,[],room)});
        if(!Array.isArray(fresh)||!fresh.length)throw new Error('could not read this community’s current settings from its relays — nothing was changed, try again');
        const byId=new Map();for(const w of [...(roomControls.get(loadKey)||[]),...fresh])if(w&&w.id)byId.set(w.id,w);
        wraps=[...byId.values()];
        scope=cordPlaneContext(p,bundle,wraps,room);before=reader.inspectControl(bundle,wraps);
        if(!scope.membershipCurrent())throw new Error('Concord membership changed while opening settings');
        made=await reader.createMetadataWrap(bundle,wraps,values,owner,p.signTemplate);
        if(!scope.membershipCurrent())throw new Error('Concord membership changed while signing');
        if(scope.current())break;
        if(attempt>=2)throw new Error('this community’s settings kept changing while saving — nothing was changed, try again');
      }
      const auth=cordPlaneAuth(p,scope,made.wrap.pubkey,relays),accepted=await p.relayPublishRoom(relays,made.wrap,auth);
      if(!accepted?.ok)throw new Error('community relays rejected the profile update');
      /* AND STRAIGHT TO THE COMMUNITY'S OWN RELAYS. This node's relay answers OK once ANY one upstream took the
       * write, and its upstreams are the relays it mirrors -- not necessarily the ones the community lists, nor
       * every one of them. A settings edition that reached computingcache only was invisible to every refresh
       * that read the community's relays: the icon and the banner each showed and vanished in turn ("banner
       * can't be used at the same time as the community icon"). Settings writes are rare and must be seen
       * everywhere, so they are also sent to every listed relay; a duplicate is harmless (same id). */
      if(p.relayPublishTo){try{await p.relayPublishTo(relays,made.wrap,{includeManaged:true,detailed:true,authScope:auth});}catch(e){console.warn('Concord settings direct publish failed',e);}}
      if(values.bannerRef||values.banner===null)bannerStage=null;
      if(!scope.membershipCurrent())throw new Error('Concord membership changed during profile update');   // published already: only a real membership change is an error here
      wraps=[...(wraps||[]),made.wrap];roomControls.set(loadKey,wraps);
      const after=reader.inspectControl(bundle,wraps);timer=after.message_expiration||0;timerChanged=timer!==(before.message_expiration||0);
      keep=retentionDays((after.custom||{})[RETENTION_KEY]);
      if(values.bannerRef||values.banner===null)await applyRoomBanner(room,after,loadKey);
      /* THE ICON TOO, from what was just folded -- "setting concord community icon again says saved but does not
       * show in community list". The room kept only the box's blob: URL, which room storage strips (it dies with
       * the page), and never the encrypted pointer, so the list had nothing to draw until some later refresh. */
      if(values.iconRef)await applyRoomIconMetadata(room,after,loadKey);   // an ENCRYPTED icon: a plain emoji/URL is stored as typed, below
      // The metadata is authoritative and must be accepted before any informational notice.
      if(timerChanged){
        const noticeScope=cordPlaneContext(p,bundle,wraps,room);
        for(const channel of after.channels||[]){
          if(!noticeScope.current())throw new Error('Concord membership changed after the timer update');
          try{
            const notice=await reader.createChatWrap(bundle,wraps,channel.id,'',owner,p.signTemplate,[['timer',String(timer)]],1740);
            if(!noticeScope.current())throw new Error('Concord membership changed before the timer notice');
            const result=await p.relayPublishRoom(relays,notice.wrap,cordPlaneAuth(p,noticeScope,notice.wrap.pubkey,relays));
            if(!result?.ok)throw new Error('notice rejected');
            await cacheEnvelopes(envelopeCacheKey(loadKey,channel.id),[notice.wrap]);
            if(noticeScope.current())await absorbChatWraps(p,reader,bundle,wraps,room,channel,[notice.wrap],channelStoreId(room,channel.name));
          }catch(error){if(!noticeScope.current())throw error;noticesFailed++;}
        }
      }
    }else{
      if(Object.prototype.hasOwnProperty.call(values,'message_expiration'))timer=Number(values.message_expiration)||0;
      if(values.customPatch&&Object.prototype.hasOwnProperty.call(values.customPatch,RETENTION_KEY))keep=retentionDays(values.customPatch[RETENTION_KEY]);
    }
    if(!stillHere())throw new Error('community changed before saving settings');
    const rooms=saved(),index=rooms.findIndex(r=>roomIdentity(r)===identity);
    rooms[index]={...rooms[index],description:values.description??rooms[index].description,icon:values.icon??rooms[index].icon,message_expiration:timer,history_retention:keep};
    if(values.iconRef){
      rooms[index].icon=room.icon??rooms[index].icon;
      if(room.iconPointer)rooms[index].iconPointer=room.iconPointer;else delete rooms[index].iconPointer;
    }
    if(values.bannerRef||values.banner===null){if(room.bannerPointer)rooms[index].bannerPointer=room.bannerPointer;else delete rooms[index].bannerPointer;}
    save(rooms);
    return {timerChanged,noticesFailed};
  }
  /* THE BANNER IN COMMUNITY SETTINGS (CORD-02 `banner`). Picking a file encrypts and uploads it AT
   * ONCE, with its progress on screen, and Save publishes the pointer -- Save stays disabled while
   * the upload runs, so a half-uploaded banner can never be published. Like the icon it is re-drawn
   * through a canvas first: that drops EXIF (a phone photo's GPS position would otherwise reach every
   * member) and bounds a banner at 1920px wide. The staged choice survives a repaint of the sheet. */
  let bannerStage=null;   // {roomId, busy, label, pointer|null, preview}
  function stagedBanner(room){
    if(!bannerStage||bannerStage.busy||bannerStage.roomId!==roomIdentity(room))return {};
    return bannerStage.pointer?{bannerRef:bannerStage.pointer}:{banner:null};
  }
  async function bannerImageBytes(file){
    const bm=await createImageBitmap(file),scale=Math.min(1,1920/Math.max(1,bm.width));
    const w=Math.max(1,Math.round(bm.width*scale)),h=Math.max(1,Math.round(bm.height*scale));
    const canvas=typeof OffscreenCanvas==='function'?new OffscreenCanvas(w,h):Object.assign(document.createElement('canvas'),{width:w,height:h});
    canvas.getContext('2d').drawImage(bm,0,0,w,h);if(bm.close)bm.close();
    const out=canvas.convertToBlob?await canvas.convertToBlob({type:'image/jpeg',quality:.9}):await new Promise(r=>canvas.toBlob(r,'image/jpeg',.9));
    return new Uint8Array(await out.arrayBuffer());
  }
  function bindBannerSettings(p,$){
    const file=$('#cc-banner-file');if(!file||!window.PCCordSpaces)return;
    const room=saved()[state.community],roomId=roomIdentity(room);
    const say=t=>{const el=$('#cc-banner-status');if(el)el.textContent=t;};
    const saveButton=()=>$('#cc-settings-save');
    const show=url=>{const box=$('#cc-banner-preview');if(!box)return;box.classList.toggle('empty',!url);box.innerHTML=url?`<img src="${p.enc(url)}" alt="New banner">`:'<span>No banner</span>';};
    if(bannerStage&&bannerStage.roomId===roomId){say(bannerStage.label);show(bannerStage.pointer===null?'':bannerStage.preview);if(bannerStage.busy){const b=saveButton();if(b)b.disabled=true;}}
    const pick=$('#cc-banner-pick');if(pick)pick.onclick=()=>{const f=$('#cc-banner-file');if(f)f.click();};
    const remove=$('#cc-banner-remove');if(remove)remove.onclick=()=>{bannerStage={roomId,busy:false,pointer:null,label:'The banner will be removed when you save.'};say(bannerStage.label);show('');};
    file.onchange=async()=>{
      const f=file.files&&file.files[0];if(!f)return;
      if(!/^image\//.test(f.type||'')){say('Choose an image for the banner.');return;}
      if(f.size>BANNER_MAX_BYTES){say('That image is too large (16 MB max).');return;}
      const stage={roomId,busy:true,label:'Preparing banner…',pointer:undefined,preview:URL.createObjectURL(f)};bannerStage=stage;
      const settle=()=>{const b=saveButton();if(b&&bannerStage===stage)b.disabled=!!stage.busy;};
      say(stage.label);show(stage.preview);settle();
      try{
        const plain=await bannerImageBytes(f),sealed=await PCCordSpaces.sealImageBytes(plain);
        if(bannerStage!==stage)return;
        stage.label='Uploading banner… 0%';say(stage.label);
        const url=await p.uploadBlob(new Blob([sealed.cipher],{type:'application/octet-stream'}),{keep:true,onProgress:({loaded,total})=>{
          if(bannerStage!==stage)return;stage.label='Uploading banner… '+Math.min(100,Math.round(100*(Number(loaded)||0)/Math.max(1,Number(total)||1)))+'%';say(stage.label);}});
        if(bannerStage!==stage)return;
        if(!/^https:\/\//i.test(String(url||'')))throw new Error('the upload did not return an https address');
        stage.pointer={url:String(url),key:sealed.key,nonce:sealed.nonce,hash:sealed.hash,ext:'jpg'};stage.busy=false;
        stage.label='Banner ready — Save changes to publish it.';say(stage.label);settle();
      }catch(e){ if(bannerStage===stage){bannerStage=null;const b=saveButton();if(b)b.disabled=false;} say('The banner could not be uploaded: '+(e&&e.message||e)); }
    };
  }
  async function createPrivateRoomChannel(p,room,name,recipients){
    const reader=window.PosterCordReader,identity=roomIdentity(room),loadKey=room.communityId||room.naddr,owner=p.viewer?.().pubkey;
    if(!reader?.createPrivateChannelPlan||!p.sendCordDirectInvite)throw new Error('Private channel invitations are not ready');
    let controls=roomControls.get(loadKey)||[];const bundle=room.cord.bundle,scope=cordPlaneContext(p,bundle,controls,room);
    const plan=await reader.createPrivateChannelPlan(bundle,controls,{name,recipients},owner,p.signTemplate);
    if(!scope.current())throw new Error('Concord membership changed while signing private access');
    // Preserve the only copy of the new independent key before publishing any
    // channel definition. A failed control publish may leave a recoverable key,
    // never an acknowledged private channel whose creator lost its secret.
    const rooms=saved(),index=rooms.findIndex(r=>roomIdentity(r)===identity);
    rooms[index]={...rooms[index],cord:{...rooms[index].cord,bundle:plan.bundle}};save(rooms);
    if(!await persistArmadaMembership(p,rooms[index]))throw new Error('Private key saved locally; encrypted membership backup is unavailable');
    let currentRoom=saved().find(r=>roomIdentity(r)===identity),active=cordPlaneContext(p,plan.bundle,controls,currentRoom);
    if(!active.current()||p.viewer?.().pubkey!==owner)throw new Error('Concord membership changed before creating private access');
    const relays=roomRelays(plan.bundle);
    for(const wrap of plan.wraps){
      if(!active.current())throw new Error('Concord membership changed during private channel creation');
      const result=await p.relayPublishRoom(relays,wrap,cordPlaneAuth(p,active,wrap.pubkey,relays));
      if(!result?.ok)throw new Error('Private key saved, but community relays did not accept all access records');
      if(!active.current())throw new Error('Concord membership changed after publishing private access');
      controls=[...controls,wrap];roomControls.set(loadKey,controls);
      await cacheEnvelopes(envelopeCacheKey(loadKey,'control'),[wrap]);
      active=cordPlaneContext(p,plan.bundle,controls,currentRoom);
    }
    let failures=0;
    for(const recipient of plan.recipients.filter(pk=>pk!==owner)){
      if(!active.current())throw new Error('Concord membership changed before private key delivery');
      // Recheck current proof immediately before each independent gift wrap.
      const invite=reader.privateChannelInvite(plan.bundle,roomControls.get(loadKey)||[],plan.channelId,recipient);
      try{await p.sendCordDirectInvite(invite,recipient,{isCurrent:active.current});}catch(error){if(!active.current())throw error;failures++;}
    }
    if(!active.current())throw new Error('Concord membership changed after private key delivery');
    const latest=saved(),at=latest.findIndex(r=>roomIdentity(r)===identity);
    latest[at]={...latest[at],channels:reader.inspectControl(plan.bundle,controls).channels};save(latest);
    return {...plan,failures};
  }
  function canAddChannel(p,room){
    try{
      if(!room||room.local||!room.cord||!room.cord.bundle)return false;
      if(room.protocol==='nip29')return false;            // NIP-29 groups are not CORD channels
      const viewer=p.viewer?p.viewer():{},owner=String(room.cord.bundle.owner||'');
      return !!(viewer.pubkey&&owner&&communityPermission(p,room,'channelManagers')
                &&window.PosterCordReader&&window.PosterCordReader.createChannelWrap);
    }catch(_){ return false; }
  }
  function channelSectionsHtml(p,room,channels){
    const starred=(channels||[]).filter(c=>channelStarred(room,c.name)),regular=(channels||[]).filter(c=>!channelStarred(room,c.name));
    const projects=window.PCConcordGit&&room&&PCConcordGit.hasRepos(room)?`<button class="cc-channel ccg-projects-entry" id="ccg-projects-open"><svg class="ic" aria-hidden="true"><use href="#i-bug"></use></svg><span class="cc-channel-name">Projects</span></button>`:'';
    return `${projects}${starred.length?`<div class="cc-section cc-starred-section">STARRED</div>${channelRowsHtml(p,room,starred)}`:''}<div class="cc-section cc-section-head">TEXT CHANNELS</div>${channelRowsHtml(p,room,regular)}`;
  }
  /* render() replaces the workspace often (history, focus and room navigation). Replacing the
   * entire community rail also replaces every decoded <img>, producing a visible initials/image
   * flash on every click. Retain the old rail and patch only buttons whose actual icon changed. */
  /* A REBUILD KEEPS THE VIDEO PLAYERS. A live room rebuilds the conversation whenever something real
   * changes -- a new message, a reaction -- and a fresh <video> starts over: a clip someone was watching
   * stopped and reloaded on every message ("video in room Lounge causes chat position to keep moving").
   * The players on screen are carried into the new markup when they show the same file. */
  /* ONLY THE MESSAGES CHANGED: PATCH THE LIST, DON'T REBUILD THE ROOM ("the concord video in Lounge room
   * keeps glitching every few seconds, you see the circle reload, plays fine though"). A new message used to
   * rebuild the whole room; the player was carried over (keepVideos) but a moved <video> re-buffers --
   * measured, 'waiting' fired on it -- which is the loading circle, every few seconds in a busy room. When
   * everything outside the message list is the same, the list is patched in place: a message whose markup
   * did not change keeps its very node (so its player is never detached), new ones are inserted around it,
   * gone ones removed. Anything else changing still rebuilds, as before. */
  const LIST_OPEN='<!--cc-list-->', LIST_CLOSE='<!--/cc-list-->';
  function splitList(html){
    const a=html.indexOf(LIST_OPEN), b=html.lastIndexOf(LIST_CLOSE);
    if(a<0||b<a)return null;
    return { rest: html.slice(0,a)+html.slice(b), inner: html.slice(a+LIST_OPEN.length,b) };
  }
  function listKey(el,i){
    if(el.dataset&&el.dataset.messageId)return 'm:'+el.dataset.messageId;
    const t=el.querySelector&&el.querySelector('[data-cc-thread-expand]');
    return t?'s:'+t.dataset.ccThreadExpand:'i:'+i;
  }
  function stampList(list){ [...list.children].forEach(el=>{ el.__ccSrc=el.outerHTML; }); }
  function patchList(list,inner){
    const tpl=document.createElement('template'); tpl.innerHTML=inner;
    const fresh=[...tpl.content.children], old=new Map();
    [...list.children].forEach((el,i)=>old.set(listKey(el,i),el));
    const keep=new Set(); let cursor=list.firstElementChild;
    fresh.forEach((nu,i)=>{
      const k=listKey(nu,i), prev=old.get(k), src=nu.outerHTML;
      const node=(prev&&prev.__ccSrc===src&&!keep.has(prev))?prev:nu;
      if(node===nu)node.__ccSrc=src;
      keep.add(node);
      if(node===cursor){ cursor=cursor.nextElementSibling; return; }
      list.insertBefore(node,cursor);
    });
    /* A message that DID change (a reaction on the video's own message) is a new node -- its player is
     * carried over from the old one, as a full rebuild does, rather than loaded again. */
    const gone=[...list.children].filter(el=>!keep.has(el)), oldVideos=new Map();
    gone.forEach(el=>{ try{ for(const v of el.querySelectorAll('video'))oldVideos.set(videoKey(v),v); }catch(_){} });
    try{ keepVideos(oldVideos,list); }catch(_){}
    gone.forEach(el=>{ if(el.parentNode===list)el.remove(); });
  }
  /* Repaint a pane holding the message list: patch the list when only it changed, else rebuild the pane
   * and carry its players over. Returns nothing; the caller re-binds as it always did. */
  function paintPane(box,html){
    const sp=splitList(html), list=box.querySelector&&box.querySelector('.cc-message-list');
    if(sp&&list&&list.parentNode===box){
      /* Everything around the list the same? Compared as the browser serializes both, so no state to keep. */
      const shape=root=>{ const l=root.querySelector('.cc-message-list');
        return [...root.childNodes].map(n=>n===l?l.cloneNode(false).outerHTML:(n.outerHTML!=null?n.outerHTML:n.textContent)).join('\u0001'); };
      const tpl=document.createElement('template'); tpl.innerHTML=sp.rest;
      if(shape(tpl.content)===shape(box)){ patchList(list,sp.inner); return; }
    }
    const oldVideos=new Map(); try{ for(const v of box.querySelectorAll('.cc-message video'))oldVideos.set(videoKey(v),v); }catch(_){}
    box.innerHTML=html;
    try{ keepVideos(oldVideos,box); }catch(_){}
    try{ const nl=box.querySelector('.cc-message-list'); if(nl)stampList(nl); }catch(_){}
  }
  function videoKey(v){ const host=v.closest('.cc-encrypted-attachment[data-cc-attachment]');
    return (host?host.dataset.ccAttachment+':'+(host.dataset.ccAttachmentIndex||'0')+'|':'')+(v.getAttribute('src')||''); }
  function keepVideos(old,feed){
    if(!old||!old.size||!feed||!feed.querySelectorAll)return;
    for(const v of feed.querySelectorAll('.cc-message video')){
      const prev=old.get(videoKey(v)); if(!prev||prev===v)continue;
      old.delete(videoKey(v)); try{ v.replaceWith(prev); }catch(_){}
    }
  }
  /* COMMUNITY FOLDERS, AS ARMADA HAS THEM ("add ability to put communities in groups like Armada does
   * it"). Armada's model, verbatim (src/lib/railLayout.ts): an ordered list of nodes, each a single
   * community {type:'item',key} or a named folder {type:'folder',id,name,keys}. A key is `c2:<community
   * id>` (`c1:` for Concord V1, read as the same community) or a NIP-29 server's normalized relay URL.
   * Kept in this account's own kind-30078 doc, d=pcai:concord-rail, NIP-44-sealed to itself; on the
   * first visit with none, the folders made in Armada (its own `armada/metadata` settings doc) are read
   * -- and never written: that doc holds every other Armada setting, and a write from here that missed
   * one would wipe them (the replaceable-doc wipe). Whether a folder is open is this device's. */
  const RAIL_D='pcai:concord-rail', RAIL_OPEN_KEY='pc.concord.railOpen';
  function railKeyOf(r){
    if(!r)return '';
    if(r.protocol==='nip29')return String(r.relay||r.url||'').trim().toLowerCase().replace(/\/+$/,'');
    const id=String(r.communityId||'').toLowerCase();
    return /^[0-9a-f]{64}$/.test(id)?'c2:'+id:'n:'+String(r.naddr||r.url||r.name||'');
  }
  function sameRailKey(a,b){ const n=k=>String(k||'').replace(/^c1:/,'c2:').toLowerCase().replace(/\/+$/,''); return n(a)===n(b); }
  function railNormalize(nodes){
    const seen=new Set(),ids=new Set(),out=[];
    for(const n of Array.isArray(nodes)?nodes:[]){
      if(n&&n.type==='item'&&typeof n.key==='string'){ const k=n.key.replace(/^c1:/,'c2:'); if(seen.has(k))continue; seen.add(k); out.push({type:'item',key:n.key}); }
      else if(n&&n.type==='folder'&&typeof n.id==='string'&&!ids.has(n.id)){
        ids.add(n.id);
        const keys=(Array.isArray(n.keys)?n.keys:[]).filter(k=>typeof k==='string'&&!seen.has(k.replace(/^c1:/,'c2:'))&&seen.add(k.replace(/^c1:/,'c2:')));
        if(!keys.length)continue;
        if(keys.length===1){ out.push({type:'item',key:keys[0]}); continue; }   // Armada: a one-item folder dissolves
        out.push({type:'folder',id:n.id,name:String(n.name||'Folder').slice(0,40),keys});
      }
    }
    return out;
  }
  function railWithout(nodes,key){
    return nodes.map(n=>n.type==='folder'?{...n,keys:n.keys.filter(k=>!sameRailKey(k,key))}:n).filter(n=>!(n.type==='item'&&sameRailKey(n.key,key)));
  }
  /* One edit, applied to whatever layout it is given -- so a save re-applies it to the freshest copy. */
  function railApply(nodes,op){
    let out=railNormalize(nodes);
    if(op.t==='into'){                                // a community into an existing or a new folder
      const at=out.findIndex(n=>(n.type==='item'&&sameRailKey(n.key,op.key))||(n.type==='folder'&&n.keys.some(k=>sameRailKey(k,op.key))));
      out=railWithout(out,op.key);
      const f=out.find(n=>n.type==='folder'&&n.id===op.id);
      if(f)f.keys.push(op.key);
      else { const node={type:'folder',id:op.id,name:String(op.name||'Folder').slice(0,40),keys:[...(op.with?[op.with]:[]),op.key].filter((k,i,a)=>a.findIndex(x=>sameRailKey(x,k))===i)};
        if(op.with)out=railWithout(out,op.with);
        out.splice(Math.max(0,Math.min(at<0?out.length:at,out.length)),0,node); }
    }else if(op.t==='out'){                           // out of its folder, right after it
      const fi=out.findIndex(n=>n.type==='folder'&&n.keys.some(k=>sameRailKey(k,op.key)));
      if(fi>=0){ out[fi]={...out[fi],keys:out[fi].keys.filter(k=>!sameRailKey(k,op.key))}; out.splice(fi+1,0,{type:'item',key:op.key}); }
    }else if(op.t==='rename'){ out=out.map(n=>n.type==='folder'&&n.id===op.id?{...n,name:String(op.name||n.name).slice(0,40)}:n); }
    else if(op.t==='ungroup'){ const fi=out.findIndex(n=>n.type==='folder'&&n.id===op.id); if(fi>=0)out.splice(fi,1,...out[fi].keys.map(key=>({type:'item',key}))); }
    return railNormalize(out);
  }
  /* The rail as drawn: the layout's order and folders over the rooms this device has, every room the
   * layout does not mention appended in its own order (a room joined later is never hidden). */
  function railView(rooms,layout){
    const idx=new Map(); (rooms||[]).forEach((r,i)=>{ const k=railKeyOf(r); if(k&&!idx.has(k.replace(/^c1:/,'c2:')))idx.set(k.replace(/^c1:/,'c2:'),i); });
    const find=k=>idx.get(String(k||'').replace(/^c1:/,'c2:').toLowerCase().replace(/\/+$/,'')), used=new Set(), out=[];
    for(const n of railNormalize(layout)){
      if(n.type==='item'){ const i=find(n.key); if(i!=null&&!used.has(i)){ used.add(i); out.push({type:'item',i}); } }
      else { const members=n.keys.map(find).filter(i=>i!=null&&!used.has(i)); members.forEach(i=>used.add(i));
        if(members.length>=2)out.push({type:'folder',id:n.id,name:n.name,members}); else if(members.length===1)out.push({type:'item',i:members[0]}); }
    }
    (rooms||[]).forEach((_,i)=>{ if(!used.has(i))out.push({type:'item',i}); });
    return out;
  }
  const railDoc={owner:'',answered:false,layout:[],fromArmada:false,loading:null,saving:null,ops:[]};
  function railOpen(){ try{ return new Set(JSON.parse(localStorage.getItem(RAIL_OPEN_KEY)||'[]')); }catch(_){ return new Set(); } }
  function setRailOpen(set){ try{ localStorage.setItem(RAIL_OPEN_KEY,JSON.stringify([...set].slice(0,64))); }catch(_){} }
  /* Did the relays ANSWER a read of one of the account's own documents (the rail, the read marks)?
   * Every relay EOSE'd -- or THIS NODE'S relay did (relay.js homeAnswered): it holds them, every write
   * goes to it, and with "use my own relays" one relay in the pool that never answers made `complete`
   * false for ever, so the first folder or read mark an account ever saved was never written. */
  function docAnswered(evs){
    if(!Array.isArray(evs))return false;
    if(evs.complete!==false)return true;
    try{ const R=(window.PCConcordRelay||window.Relay); return !!(R&&R.homeAnswered&&R.homeAnswered(evs)); }catch(_){ return false; }
  }
  async function fetchRailDoc(p,owner,d){
    if(!p.relayQuery||!p.nip44dec)return null;
    let evs=null;
    try{ evs=await p.relayQuery([{authors:[owner],kinds:[30078],'#d':[d],limit:1}],8000); }catch(_){ evs=null; }
    let cached=[];try{cached=window.Store&&window.Store.query?window.Store.query([{authors:[owner],kinds:[30078],'#d':[d]}])||[]:[];}catch(_){}
    const all=[...(Array.isArray(evs)?evs:[]),...cached].filter(e=>e&&e.pubkey===owner&&typeof e.content==='string');
    if(!all.length&&!docAnswered(evs))return null;     // could not ask: never "no folders"
    const ev=all.sort((a,b)=>b.created_at-a.created_at)[0];
    if(!ev)return {layout:null};
    try{ const doc=JSON.parse(await p.nip44dec(owner,ev.content)||'null');
      const layout=d===RAIL_D?(doc&&doc.layout):(doc&&doc.railLayout);
      return {layout:Array.isArray(layout)?layout:null}; }catch(_){ return null; }
  }
  function loadRail(p){
    const owner=p&&p.viewer&&p.viewer().pubkey;
    if(!owner)return Promise.resolve(railDoc);
    if(railDoc.owner!==owner)Object.assign(railDoc,{owner,answered:false,layout:[],fromArmada:false,loading:null,saving:null,ops:[]});
    if(railDoc.answered)return Promise.resolve(railDoc);
    if(railDoc.loading)return railDoc.loading;
    railDoc.loading=(async()=>{
      const got=await fetchRailDoc(p,owner,RAIL_D);
      if(railDoc.owner!==owner||!got)return railDoc;
      railDoc.answered=true;
      if(got.layout){ railDoc.layout=railNormalize(got.layout); }
      else { const arm=await fetchRailDoc(p,owner,'armada/metadata').catch(()=>null);      // read-only, first visit only
        if(railDoc.owner===owner&&arm&&arm.layout&&arm.layout.length){ railDoc.layout=railNormalize(arm.layout); railDoc.fromArmada=true; } }
      for(const op of railDoc.ops)railDoc.layout=railApply(railDoc.layout,op);
      backgroundRender();
      if(railDoc.ops.length)saveRail(p);
      return railDoc;
    })().finally(()=>{ railDoc.loading=null; });
    return railDoc.loading;
  }
  function saveRail(p){
    const owner=railDoc.owner;
    if(!owner||!railDoc.answered||!p.nip44enc||!p.signTemplate||!p.relayPublish)return;
    if(railDoc.saving){ railDoc.again=true; return; }
    const ops=railDoc.ops.slice();
    railDoc.saving=(async()=>{
      const fresh=await fetchRailDoc(p,owner,RAIL_D);
      if(!fresh||railDoc.owner!==owner)throw new Error('could not re-read the folders');   // write nothing
      let layout=fresh.layout?railNormalize(fresh.layout):railDoc.layout;   // first write: what is on screen (incl. an Armada import)
      for(const op of ops)layout=railApply(layout,op);
      const content=await p.nip44enc(owner,JSON.stringify({v:1,layout}));
      const ev=await p.signTemplate({kind:30078,created_at:Math.floor(Date.now()/1000),content,tags:[['d',RAIL_D]],pubkey:owner});
      const res=await p.relayPublish(ev);
      if(res&&res.ok===false)throw new Error('not accepted');
      railDoc.ops=railDoc.ops.slice(ops.length); railDoc.layout=layout; for(const op of railDoc.ops)railDoc.layout=railApply(railDoc.layout,op);
    })().catch(()=>{ railDoc.again=true; }).finally(()=>{ railDoc.saving=null; if(railDoc.again&&railDoc.ops.length){ railDoc.again=false; setTimeout(()=>saveRail(p),4000); } });
  }
  function editRail(p,op){
    railDoc.ops.push(op); railDoc.layout=railApply(railDoc.layout,op); render();
    if(railDoc.answered)saveRail(p); else loadRail(p);
  }
  function railHtml(p,rooms){
    const open=railOpen(), view=railView(rooms,railDoc.layout);
    const btn=i=>{const r=rooms[i];return `<button class="cc-server${state.community===i?' active':''}${isUnread(r)?' unread':''}" data-cc-server="${i}" title="${p.enc(roomName(r,i))}">${roomIcon(p,r,i)}</button>`;};
    return view.map(n=>{
      if(n.type==='item')return btn(n.i);
      const isOpen=open.has(n.id),unread=n.members.some(i=>isUnread(rooms[i])),active=n.members.includes(state.community);
      const minis=n.members.slice(0,4).map(i=>`<span class="cc-folder-mini">${roomIcon(p,rooms[i],i)}</span>`).join('');
      return `<div class="cc-folder${isOpen?' open':''}" data-cc-folder-box="${p.enc(n.id)}"><button class="cc-server cc-folder-btn${unread?' unread':''}${active&&!isOpen?' active':''}" data-cc-folder="${p.enc(n.id)}" title="${p.enc(n.name)}" aria-label="Folder ${p.enc(n.name)}" aria-expanded="${isOpen?'true':'false'}">${isOpen?'<span class="cc-folder-glyph" aria-hidden="true">📁</span>':`<span class="cc-folder-grid">${minis}</span>`}</button>${isOpen?n.members.map(btn).join(''):''}</div>`;
    }).join('');
  }
  function railSig(rooms){ try{ return JSON.stringify([railView(rooms,railDoc.layout),[...railOpen()]]); }catch(_){ return ''; } }
  function retainCommunityRail(oldRail,newRail){
    if(!oldRail||!newRail)return;
    // Folders: a different arrangement (or a folder opened) is a different rail, never the old one patched.
    if(((oldRail.dataset||{}).ccSig||'')!==((newRail.dataset||{}).ccSig||''))return;
    const oldServers=[...oldRail.querySelectorAll('[data-cc-server]')],newServers=[...newRail.querySelectorAll('[data-cc-server]')];
    if(oldServers.length!==newServers.length)return;
    for(let i=0;i<oldServers.length;i++)if(oldServers[i].dataset.ccServer!==newServers[i].dataset.ccServer)return;
    for(let i=0;i<oldServers.length;i++){
      const old=oldServers[i],fresh=newServers[i];old.className=fresh.className;old.title=fresh.title;
      if(old.innerHTML!==fresh.innerHTML)old.innerHTML=fresh.innerHTML;
    }
    newRail.replaceWith(oldRail);
  }
  /* THREADS. The protocol half has been right the whole time — a reply is a kind-1111 with an `e`
   * tag, which is what Armada reads and writes — and the presentation half did not exist: replies
   * were flattened into the channel with a quoted line on top, so three conversations interleaved
   * and you reconstructed them by eye. Nothing said a message HAD replies, so a thread was
   * invisible unless you happened to scroll past one.
   *
   * These are pure and DOM-free on purpose: the parent walk is the same one `threadParticipants`
   * already does for notifications, and it is the only part worth testing away from a browser. */
  function replyParentId(m){
    return String((m && m.reply && m.reply.id)
      || (((m && m.tags) || []).find(t => t[0] === 'e' || t[0] === 'E') || [])[1] || '');
  }
  /* The root a message belongs to — itself when it starts one. Cycle-safe: a malformed `e` chain
   * from a hostile relay must not spin here. */
  function threadRootId(messages, m){
    const byId = messages instanceof Map ? messages
      : new Map((messages || []).map(x => [messageId(x), x]));
    const seen = new Set();
    let node = m, last = messageId(m);
    while(node && !seen.has(messageId(node))){
      last = messageId(node); seen.add(last);
      const parent = replyParentId(node);
      node = parent && byId.get(parent);
    }
    return last;
  }
  /* rootId -> the messages hanging off it, oldest first. Built once per draw. */
  function threadIndex(messages){
    const rows = messages || [], byId = new Map(rows.map(m => [messageId(m), m])), out = new Map();
    for(const m of rows){
      if(!replyParentId(m)) continue;              // a root is not a reply to anything
      const root = threadRootId(byId, m);
      if(root === messageId(m)) continue;          // a broken parent link is not a thread
      if(!out.has(root)) out.set(root, []);
      out.get(root).push(m);
    }
    for(const list of out.values()) list.sort((a, b) => Number(a.at) - Number(b.at));
    return out;
  }
  /* THREAD REPLIES LOOK LIKE THREAD REPLIES, AND A BUSY ROOM CAN FOLD THEM AWAY ("I see no
   * differentiation between thread replies and main chat replies (can be helpful to declutter busy
   * rooms)" -- the Lounge's owner). On the wire there is no difference: CORD-03 has one kind of reply,
   * a 1111 naming its parent, so the split is drawn here. In the channel a reply is marked as part of a
   * thread (indented, an accent bar, an "in thread" line that opens it), and "Hide thread replies"
   * leaves only the conversations' first messages, each with its "N replies" button. The choice is
   * this device's, for every room. The index is built ONCE per message list -- it used to be rebuilt
   * per message to count replies, which is quadratic in exactly the busy rooms this is for. */
  const threadMemo = new WeakMap();
  function threadInfo(messages){
    const rows = messages || [];
    let info = threadMemo.get(rows);
    if(info) return info;
    const index = threadIndex(rows), rootOf = new Map();
    for(const [root, list] of index) for(const m of list) rootOf.set(messageId(m), root);
    info = { index, rootOf };
    try{ threadMemo.set(rows, info); }catch(_){ }
    return info;
  }
  /* REPLIES COLLAPSE UNDER THE MESSAGE THEY ANSWER ("make the thread collapse under the message being
   * responded to. Right now, it is treated separately on the back end but everything looks almost
   * exactly the same in the chat"). By default a thread's replies leave the channel's flow; the message
   * they answer carries a summary row (count, who, the last reply) that expands them in place, under
   * it. The header button switches this device to the flat view (every reply in the chat, marked). */
  const THREADS_IN_CHAT_KEY = 'pc.concord.threadsInChat';
  function threadsInChat(){ try{ return localStorage.getItem(THREADS_IN_CHAT_KEY) === '1'; }catch(_){ return false; } }
  function setThreadsInChat(on){ try{ localStorage.setItem(THREADS_IN_CHAT_KEY, on ? '1' : '0'); }catch(_){ } }
  const expandedThreads = new Set();          // roots whose replies are open in place (this visit)
  function threadSummaryHtml(p, rootId, replies){
    const list = replies || [], n = list.length; if(!n) return '';
    const open = expandedThreads.has(rootId), last = list[n - 1] || {};
    const people = [...new Set(list.map(r => r.pubkey))].slice(0, 3);
    const faces = people.map(pk => { const pr = p.profOf ? p.profOf(pk) || {} : {};
      return `<img class="cc-ts-face" src="${p.enc(pr.picture || p.LOGO || '')}" alt="">`; }).join('');
    const said = String(last.text || last.content || '').replace(/\s+/g, ' ').slice(0, 80);
    return `<div class="cc-thread-summary${open ? ' open' : ''}"><button type="button" class="cc-ts-toggle" data-cc-thread-expand="${p.enc(rootId)}" aria-expanded="${open ? 'true' : 'false'}">`
      + `<span class="cc-ts-faces">${faces}</span><b>${n} ${n === 1 ? 'reply' : 'replies'}</b>`
      + `<span class="cc-ts-last">${open ? 'Hide replies' : `${p.enc(last.by || 'member')}: ${p.enc(said)}`}</span></button>`
      + `<button type="button" class="cc-ts-open" data-cc-thread="${p.enc(rootId)}" title="Open the thread">Open thread</button></div>`;
  }
  /* THE TOGGLE IS A HEADER BUTTON, NOT A BAR. It was a full-width strip across the top of every channel
   * with a thread ("hide threaded replies looks very ugly and a screen waste"): a row of screen for one
   * switch. Now it sits with the pins in the header, says how many replies are in threads, and only
   * exists in a channel that has any. */
  function threadToggleHtml(p,room){
    if(!room||state.thread)return '';
    let n=0; try{ n=threadInfo(activeMessages(room)||[]).rootOf.size; }catch(_){ n=0; }
    if(!n)return '';
    const flat=threadsInChat(),label=(flat?'Collapse replies under their messages':'Show replies in the chat')+' ('+n+')';
    return `<button class="cc-head-btn cc-threads-toggle${flat?' on':''}" id="cc-threads-toggle" aria-pressed="${flat?'true':'false'}" title="${label}" aria-label="${label}"><svg class="ic"><use href="#i-reply"></use></svg><small>${n}</small></button>`;
  }
  /* The channel as drawn. Flat: every message in time order. Collapsed (the default): only messages that
   * are not part of a thread, each open thread's replies placed straight under the message they answer. */
  function channelRows(messages, flat){
    const rows = messages || [];
    if(flat) return rows;
    const { rootOf, index } = threadInfo(rows), out = [];
    for(const m of rows){
      const id = messageId(m);
      if(rootOf.has(id)) continue;
      out.push(m);
      if(expandedThreads.has(id)) for(const r of (index.get(id) || [])) out.push(r);
    }
    return out;
  }
  /* What the thread view shows: the root, then every descendant in time order. */
  function threadView(messages, rootId){
    const rows = messages || [], byId = new Map(rows.map(m => [messageId(m), m]));
    const root = byId.get(String(rootId));
    if(!root) return [];
    return [root].concat((threadIndex(rows).get(String(rootId)) || []));
  }

  function threadParticipants(messages,target,viewerPubkey){
    const rows=messages||[],byId=new Map(rows.map(m=>[messageId(m),m])),people=new Set();
    const parentId=node=>String((node&&node.reply&&node.reply.id)||
      (((node&&node.tags)||[]).find(t=>t[0]==='e'||t[0]==='E')||[])[1]||'');
    const rootId=node=>{const seen=new Set();let last=messageId(node);while(node&&!seen.has(messageId(node))){last=messageId(node);seen.add(last);const parent=parentId(node);node=parent&&byId.get(parent);}return last;};
    const root=rootId(target);
    /* Reply UX follows Armada/Element: a reply notifies everybody already participating in this
     * thread, not only the selected message's ancestor chain. This matters most when replying to
     * the root after several branches exist — the old upward-only walk silently omitted them. */
    for(const node of [target,...rows]){
      if(!node||rootId(node)!==root)continue;
      if(node.pubkey&&node.pubkey!==viewerPubkey)people.add(node.pubkey);
    }
    return [...people];
  }
  function webxdcOf(m,room,channelName){
    const channel=(room&&room.channels||[]).find(c=>c.name===(channelName||'general')),protocol=room&&room.protocol==='nip29'?'nip29':(room&&room.cord?'concord2':''),scope=protocol==='nip29'?`nip29|${room.relay}|${room.groupId}`:(protocol==='concord2'&&channel&&channel.id?`concord2|${channel.id}`:''),transport=protocol?{protocol,room:roomIdentity(room),channel:channelName||'general',channelId:channel&&channel.id,relay:room.relay,groupId:room.groupId}:null;
    /* Every imeta, parsed once. An attachment is a mini app when it SAYS so (the three mimes) or when it is
     * a .xdc by NAME or URL -- a sealed Armada/Vector attachment can carry a generic type, and then the
     * only card left was the bare-URL one below, with no key: "not a zip file" on a game that is fine. */
    const metas=[];
    for(const t of (m&&m.tags||[]).slice(0,100)){ if(t[0]!=='imeta')continue; const f={}; for(let i=1;i<Math.min(t.length,30);i++){ const s=String(t[i]||'').slice(0,4096),j=s.indexOf(' '); if(j>0)f[s.slice(0,j)]=s.slice(j+1); } if(/^https?:\/\//i.test(f.url||'')||/^[0-9a-f]{64}$/i.test(String(f.x||'')))metas.push(f); }
    const isXdc=f=>{const mime=(f.m||'').toLowerCase();return mime==='application/vnd.webxdc+zip'||mime==='application/webxdc+zip'||mime==='application/x-webxdc'||/\.xdc$/i.test(String(f.name||'').trim())||/\.xdc(?:[?#]|$)/i.test(f.url||'');};
    /* Armada/Vector SEAL the .xdc like any room attachment: the URL is ciphertext, `x` its hash, `ox` the game's.
     * Hand the player the key, or it unzips random bytes. A seal we cannot open is not offered as a game. */
    const encOf=f=>{const alg=String(f['encryption-algorithm']||'').toLowerCase();if(!alg)return null;if(alg!=='aes-gcm')return false;return {key:f['decryption-key']||'',nonce:f['decryption-nonce']||'',ox:f.ox||''};};
    /* AN ATTACHMENT WITH NO `url` IS FOUND BY ITS HASH. Josephus's client wrote `["imeta","url ", ... "x <hash>"]`
     * with the link only in the message text (2026-10-15): matched by URL, the attachment and the link never met,
     * so the card that was drawn had no key, no hash and no session ("looks encrypted" / "not a zip" on a game
     * that decrypts fine), and with no link in the text there was no card at all. A Blossom URL names its blob,
     * so a text link carrying the attachment's `x` IS that attachment. */
    const textLinks=(String(m&&m.text||'').slice(0,200000).match(/https?:\/\/[^\s<>]+/gi)||[]).slice(0,50);
    const hashOf=f=>/^[0-9a-f]{64}$/i.test(String(f.x||''))?String(f.x).toLowerCase():'';
    const linkFor=f=>{const h=hashOf(f);return h?(textLinks.find(u=>u.toLowerCase().includes(h))||''):'';};
    for(const f of metas){ if(!isXdc(f))continue; const enc=encOf(f); if(enc===false)continue;
      const where=String(f.url||'').trim()||linkFor(f); if(!where)continue;
      /* `webxdc-topic` is the current Armada/Vector Iroh lobby contract and wins over the legacy alias if both are present. */
      const explicit=f['webxdc-topic']||f.webxdc||'';
      return {url:where,sha:f.x||'',uuid:explicit,urlTopicMessageId:explicit?'':messageId(m),name:(f.summary||String(f.name||'').replace(/\.xdc$/i,'')||'Mini app').slice(0,80),transport,...(enc?{enc}:{})}; }
    /* Bare links have no attachment tag to carry a minted topic. Armada/Vector derive it from the
     * canonical URL plus this message id; deriving from room scope silently creates a private lobby.
     * A bare link that IS one of this message's sealed attachments still gets that attachment's key. */
    const url=(String(m&&m.text||'').slice(0,200000).match(/https?:\/\/[^\s<>]+\.xdc(?:\?[^\s<>]*)?/i)||[])[0]; if(!url)return null;
    const same=metas.find(f=>f.url===url||(hashOf(f)&&url.toLowerCase().includes(hashOf(f)))), enc=same?encOf(same):null; if(enc===false)return null;
    return {url,sha:same&&same.x||'',uuid:'',urlTopicMessageId:messageId(m),name:'Mini app',transport,...(enc?{enc}:{})};
  }
  /* A delegated Webxdc click may observe a card produced by the generic link hydrator before the
   * room-specific render settles. Resolve identity from the authoritative decrypted CORD message
   * at launch time, so stale DOM can never move a player into a URL-derived private lobby. */
  function resolveWebxdcCard(card,fallback){
    /* An explicit topic is the attachment author's lobby and is authoritative. A partially hydrated
     * room cache may temporarily expose the same URL as a bare link; re-deriving from that row's id
     * silently moves a running Armada game into a different lobby. */
    /* ...but its KEY is not the card's to forget: a card drawn by the generic link pass may lack the seal's key
     * while the decrypted message holds it ("looks encrypted" over a game that plays from the other card). */
    if(fallback&&fallback.uuid){if(fallback.enc)return fallback;try{const row=card&&card.closest&&card.closest('.cc-message[data-message-id]'),room=saved()[state.community],id=row&&row.dataset&&row.dataset.messageId,m=room&&activeMessages(room).find(x=>messageId(x)===id),got=m&&webxdcOf(m,room,state.channel);if(got&&got.enc&&got.url===fallback.url)return Object.assign({},fallback,{enc:got.enc});}catch(_){}return fallback;}
    try{const row=card&&card.closest&&card.closest('.cc-message[data-message-id]'),room=saved()[state.community],id=row&&row.dataset&&row.dataset.messageId,m=room&&activeMessages(room).find(x=>messageId(x)===id),resolved=m&&webxdcOf(m,room,state.channel);return (resolved&&resolved.uuid?resolved:fallback)||resolved;}catch(_){return fallback;}
  }
  async function deriveWebxdcUrlTopic(url,messageId){if(!window.PCWebxdc||!PCWebxdc.deriveUrlTopic)throw new Error('Webxdc topic support is unavailable');return PCWebxdc.deriveUrlTopic(url,messageId);}
  function mintWebxdcTopic(){if(!window.PCWebxdc||!PCWebxdc.mintTopic)throw new Error('Webxdc topic support is unavailable');return PCWebxdc.mintTopic();}
  function hydrateWebxdcCards(room){if(!window.PCWebxdc||!PCWebxdc.cardHtml||!document.querySelectorAll)return;const epoch=++webxdcHydrationEpoch,byId=new Map(activeMessages(room).map(m=>[messageId(m),m])),rows=[...document.querySelectorAll('.cc-message[data-message-id]')];let at=0;const batch=()=>{if(epoch!==webxdcHydrationEpoch)return;const end=Math.min(rows.length,at+8);for(;at<end;at++){const el=rows[at],m=byId.get(el.dataset.messageId),app=m&&webxdcOf(m,room,state.channel),html=app&&PCWebxdc.cardHtml(app),body=el.querySelector('.cc-message-body'),old=body&&body.querySelector('.xdc-card');if(!html||!body)continue;let stale=!old;if(old){try{const prior=JSON.parse(old.dataset.xdc||'null');stale=!prior||prior.uuid!==app.uuid||prior.urlTopicMessageId!==app.urlTopicMessageId||prior.url!==app.url||JSON.stringify(prior.transport||null)!==JSON.stringify(app.transport||null);}catch(_){stale=true;}}if(stale){if(old)old.remove();body.insertAdjacentHTML('beforeend',html);}}if(at<rows.length)(window.requestAnimationFrame||setTimeout)(batch);};(window.requestAnimationFrame||setTimeout)(batch);}
  function hexBytes(s){ const h=String(s||''); if(!/^[0-9a-f]+$/i.test(h)||h.length%2)throw new Error('invalid encrypted image key'); return new Uint8Array(h.match(/../g).map(x=>parseInt(x,16))); }
  function bytesHex(a){ return [...new Uint8Array(a)].map(x=>x.toString(16).padStart(2,'0')).join(''); }
  function imageMime(a){ return a[0]===0x89&&a[1]===0x50?'image/png':a[0]===0xff&&a[1]===0xd8?'image/jpeg':a[0]===0x47&&a[1]===0x49?'image/gif':a[0]===0x52&&a[1]===0x49&&a[8]===0x57?'image/webp':''; }
  async function boundedImageBytes(res,max){
    const announced=Number(res.headers&&res.headers.get&&res.headers.get('content-length'))||0;
    if(announced>max)throw new Error('community icon is too large');
    if(!res.body||!res.body.getReader){const bytes=new Uint8Array(await res.arrayBuffer());if(bytes.byteLength>max)throw new Error('community icon is too large');return bytes;}
    const reader=res.body.getReader(),parts=[];let size=0;
    try{while(true){const {done,value}=await reader.read();if(done)break;size+=value.byteLength;if(size>max){await reader.cancel();throw new Error('community icon is too large');}parts.push(value);}}finally{try{reader.releaseLock();}catch(_){}}
    const out=new Uint8Array(size);let at=0;for(const part of parts){out.set(part,at);at+=part.byteLength;}return out;
  }
  /* THE CACHE KEY FOR ONE ICON, AND WHY IT IS NOT `JSON.stringify(pointer)`.
   *
   * Three call sites build that pointer object independently — `hydrateStoredRoomIcon` from the
   * localStorage round trip, `applyControl` from whatever `inspectControl` reconstructs out of the
   * bundle, and the metadata refresh straight off the relay event — and `JSON.stringify` is KEY
   * ORDER SENSITIVE. `putIcon` stores one row per room, so two orderings of the same four fields
   * are two different `ref`s overwriting each other: every read misses, every visit re-downloads
   * and re-decrypts an image that was already on disk, and the cache looks broken while working
   * exactly as written. A re-ordered pointer also reads as a CHANGED icon, so it re-decrypts on
   * every metadata pass as well.
   *
   * The pointer already carries `hash` — the sha256 of the plaintext image, which
   * `decryptImagePointer` verifies before it will hand the bytes back. That is the image's
   * identity, it cannot be reordered, and it is the same on every path. The stringified form
   * survives only as a fallback for a pointer with no usable hash, which cannot be decrypted here
   * anyway. */
  function iconRef(value){
    if(typeof value==='string')return value;
    if(!value||typeof value!=='object')return '';
    const h=String(value.hash||'').toLowerCase();
    return /^[0-9a-f]{64}$/.test(h)?'h:'+h:'j:'+JSON.stringify(value);
  }
  async function decryptImagePointer(pointer,loadKey,ref,maxBytes=0){
    try{const cached=window.PCConcordCache&&await window.PCConcordCache.getIcon(loadKey,ref);if(cached)return URL.createObjectURL(new Blob([cached.bytes],{type:cached.mime||'image/*'}));}catch(e){console.warn('Concord icon cache read failed',e);}
    const url=new URL(String(pointer&&pointer.url||''),location.href);if(!/^https?:$/.test(url.protocol))throw new Error('invalid community icon URL');
    const rawKey=hexBytes(pointer.key),nonce=hexBytes(pointer.nonce);
    /* CORD-02/Armada/Vector use a 16-byte AES-GCM nonce for encrypted community
     * images. Older PosterChan-created pointers used WebCrypto's common 12-byte
     * nonce, so retain read compatibility without accepting arbitrary IV sizes. */
    if(rawKey.byteLength!==32||(nonce.byteLength!==16&&nonce.byteLength!==12)||!(/^[0-9a-f]{64}$/i.test(String(pointer.hash||''))))throw new Error('invalid encrypted image pointer');
    const max=maxBytes||(window.PCConcordCache&&PCConcordCache.MAX_ICON_BYTES)||5*1024*1024,
      res=await fetch(url.href); if(!res.ok)throw new Error('community icon download failed');
    const encrypted=await boundedImageBytes(res,max+32),key=await crypto.subtle.importKey('raw',rawKey,'AES-GCM',false,['decrypt']);
    const plain=new Uint8Array(await crypto.subtle.decrypt({name:'AES-GCM',iv:nonce},key,encrypted));if(plain.byteLength>max)throw new Error('community icon is too large');
    const hash=bytesHex(await crypto.subtle.digest('SHA-256',plain)); if(hash!==String(pointer.hash).toLowerCase())throw new Error('community icon failed integrity check');
    const mime=imageMime(plain);if(!mime)throw new Error('community icon is not a supported image');try{if(window.PCConcordCache)await window.PCConcordCache.putIcon(loadKey,ref,plain,mime);}catch(e){console.warn('Concord icon cache write failed',e);}return URL.createObjectURL(new Blob([plain],{type:mime}));
  }
  /* AN EMPTY ICON FROM A CONTROL READ IS "COULD NOT READ IT", NEVER "IT WAS CLEARED".
   *
   * Reported as "Concord communities avatar keeps disappearing and coming back", and it is the
   * empty-read wipe this codebase keeps re-learning, on a four-second timer. `inspectControl`
   * ALWAYS returns an `icon` key and it is `""` whenever the folded control state carried no
   * metadata — which is any pass whose wraps came back thin. That is the ORDINARY case here, not
   * an edge: `refreshRoomMetadata` runs every 4s while its network read is throttled to 60s, so
   * fourteen passes out of fifteen see only what the envelope cache gives them, and that cache
   * answers `[]` for a failure as well as for a miss (another window holding the IndexedDB at a
   * different version is enough — the desktop shell plus a browser tab does it). The wipe was
   * then PERSISTED with save(), so the community lost its picture until a complete read put it
   * back. The line above this one already knew: `name` is written as `info.name||room.name`.
   *
   * The trade is stated rather than hidden: an owner who deletes their community's icon upstream
   * leaves everyone else's copy showing the old one. That is decoration against a rail that
   * flickers every four seconds, and the owner's own "Community settings" save still clears it
   * locally — it writes room.icon directly and does not come through here. */
  /* THE BANNER (CORD-02 metadata `banner`, an encrypted ImageRef exactly like `icon`) — Vector and
   * Armada show one above a community's channel list; ours parsed it and drew nothing. Decrypted
   * through the same bounded, integrity-checked path as the icon; held in memory only (the pointer
   * is on the room, so a restart re-decrypts), and a broken pointer backs off like the icon's. */
  const roomBanners=new Map(),bannerLoads=new Set();
  /* Armada's ceiling for an icon/banner read (image.ts MAX_IMAGE_BYTES). A banner is a wide picture
   * and the icon's 5 MB cap refused real ones; the disk cache still keeps only what fits its cap. */
  const BANNER_MAX_BYTES=16*1024*1024;
  /* `seed` follows applyRoomIconMetadata's rule: it is inspectControl(bundle,[]), and a read that
   * matches it in every profile field folded NO metadata -- "could not read it", which must never
   * clear a banner (it did, on every thin 4-second refresh, so a banner flashed and vanished). A
   * caller passing no seed asserts its answer (the owner's own Save). The decrypted copy is cached
   * under its OWN key: sharing the icon's `loadKey` row made the two evict each other on every load. */
  async function applyRoomBanner(room,info,loadKey,seed=null){
    const value=info&&info.banner;
    if(!value||typeof value!=='object'){
      const thin=!!seed&&!info?.description&&!info?.icon&&String(info?.name||'')===String(seed.name||'');
      if(thin||!room.bannerPointer)return false;
      delete room.bannerPointer;const held=roomBanners.get(loadKey);roomBanners.delete(loadKey);
      if(held&&held.url&&/^blob:/i.test(held.url))try{URL.revokeObjectURL(held.url);}catch(_){}
      return true;
    }
    const ref='b:'+iconRef(value),held=roomBanners.get(loadKey); if(held&&held.ref===ref){room.bannerPointer=value;return false;}
    const failed=failedIconRefs.get('banner:'+loadKey); if(failed&&failed.ref===ref&&Date.now()-failed.at<60000)return false;
    try{ const url=await decryptImagePointer(value,'banner:'+loadKey,ref,BANNER_MAX_BYTES); if(held&&held.url&&/^blob:/i.test(held.url))try{URL.revokeObjectURL(held.url);}catch(_){}
      roomBanners.set(loadKey,{ref,url}); room.bannerPointer=value; failedIconRefs.delete('banner:'+loadKey); return true;
    }catch(e){ failedIconRefs.set('banner:'+loadKey,{ref,at:Date.now()}); console.warn('Concord banner could not be shown',e&&e.message||e); return false; }
  }
  /* A room saved with a banner pointer shows it after a restart without waiting for the metadata
   * rotation to come round to it: the first draw that wants it decrypts it (disk cache first). */
  function roomBannerUrl(room){
    const key=room&&(room.communityId||room.naddr),b=key&&roomBanners.get(key);
    if(b)return b.url||'';
    if(key&&room.bannerPointer&&!bannerLoads.has(key)){
      bannerLoads.add(key);
      void applyRoomBanner({...room},{banner:room.bannerPointer},key).then(changed=>{
        if(changed&&typeof document!=='undefined'&&document.body.classList.contains('concord-view'))backgroundRender();
      }).finally(()=>bannerLoads.delete(key));
    }
    return '';
  }
  async function applyRoomIconMetadata(room,info,loadKey,seed=null){
    if(!room||!info||!Object.prototype.hasOwnProperty.call(info,'icon'))return false;
    const value=info.icon,ref=iconRef(value);
    /* `seed` is inspectControl(bundle,[]) — the same community with NO control events folded in.
     * A read that matches it in every profile field folded no metadata at all, which is the only
     * way to tell "the owner cleared the picture" from "this pass saw nothing". A caller that
     * passes no seed is asserting its answer (the icon dialog's own path). */
    const thin=!!seed&&!info.description&&!value&&String(info.name||'')===String(seed.name||'');
    if(thin&&(room.iconPointer||room.icon||(roomIconRefs.get(loadKey)||{}).url))return false;
    const cached=roomIconRefs.get(loadKey);
    if(cached&&cached.ref===ref)return false;
    const failed=failedIconRefs.get(loadKey);
    if(failed&&failed.ref===ref&&Date.now()-failed.at<60000)return false;
    try{
      const icon=value?(typeof value==='string'?value:await decryptImagePointer(value,loadKey,ref)):'';
      if(cached&&cached.url&&/^blob:/i.test(cached.url)&&cached.url!==icon)try{URL.revokeObjectURL(cached.url);}catch(_){}
      roomIconRefs.set(loadKey,{ref,url:icon});
      failedIconRefs.delete(loadKey);
      const before=JSON.stringify([room.icon,room.iconPointer]);
      if(value&&typeof value==='object'){room.iconPointer=value;if(/^blob:/i.test(String(room.icon||'')))room.icon='';}
      else{delete room.iconPointer;room.icon=icon;}
      return before!==JSON.stringify([room.icon,room.iconPointer]);
    }catch(error){
      /* An icon is decoration, never a prerequisite for channels or history. Back off this exact
       * broken pointer for a minute: metadata refresh runs every four seconds, and retrying an
       * invalid encrypted value on every pass starves message work and floods Android's renderer. */
      failedIconRefs.set(loadKey,{ref,at:Date.now()});
      console.warn('Concord community icon could not be loaded',error);
      return false;
    }
  }
  /* PAINT WHAT IS ALREADY HELD, BEFORE THE FIRST NETWORK AWAIT — the rule every other list in
   * this app follows, and the one the community icons never did. `roomIcon()` is synchronous: with
   * nothing in `roomIconRefs` it draws the letter glyph and only THEN kicks a per-room IndexedDB
   * read, so a warm cache still showed two initials on every community and filled them in one
   * repaint at a time. One transaction for the whole list, one repaint, and a cold icon falls back
   * to the glyph exactly as before. */
  /* RE-RUNNABLE, KEYED ON THE ROOM LIST — measured, not assumed. On a real account the saved list
   * grows during the visit (1 room at first paint, 4 a moment later) and a room's icon POINTER
   * only lands when its membership/metadata sync does, so a once-per-session boolean warms exactly
   * the rooms that needed it least. The signature includes each room's pointer, so a room that
   * gains one is re-warmed; rooms already resolved are skipped inside the loop and the whole read
   * is still one transaction. */
  let iconsWarmedFor='';
  function iconWarmSignature(){
    try{ return saved().map(r=>roomIdentity(r)+'|'+iconRef(r&&(r.iconPointer||r.icon))).join('\n'); }
    catch(_){ return ''; }
  }
  async function warmRoomIcons(){
    if(!window.PCConcordCache||!PCConcordCache.allIcons)return;
    const sig=iconWarmSignature();
    if(!sig||sig===iconsWarmedFor)return;
    iconsWarmedFor=sig;
    let rows=[];
    try{ rows=await PCConcordCache.allIcons()||[]; }
    catch(e){
      /* NOT LATCHED. A cache that could not be opened (another tab holding an older version) is
       * "could not ask", never "there is nothing there" — leave the flag clear so the next entry
       * tries again rather than showing initials for the rest of the session. */
      iconsWarmedFor=''; console.warn('Concord icon warm failed',e); return;
    }
    /* LATCH ON THE RESULT, NEVER ON THE ATTEMPT — the shape this codebase keeps re-learning.
     *
     * `iconsWarmedFor` is set BEFORE the read, and the two early exits below then mean "this room
     * list is warmed" when nothing was warmed at all. An empty store is the ORDINARY first visit:
     * the icons are written by applyRoomIconMetadata a moment later, and by then the signature has
     * not changed, so nothing re-reads and every community shows its two initials for the rest of
     * the session. The error path above already understood this ("could not ask" is never "there is
     * nothing there"); an empty answer needed the same treatment.
     *
     * Only retried while there is something to wait FOR: a room with no icon at all must not make
     * this re-read IndexedDB on every repaint. */
    const wanted=saved().some(room=>iconRef(room&&(room.iconPointer||room.icon)));
    if(!rows.length){ if(wanted)iconsWarmedFor=''; return; }
    const byKey=new Map(rows.map(r=>[String(r.key),r]));
    let filled=0;
    for(const room of saved()){
      const loadKey=roomIdentity(room);
      if(!loadKey||roomIconRefs.has(loadKey))continue;
      const want=iconRef(room.iconPointer||room.icon);
      const row=byKey.get(loadKey);
      if(!row||!want||String(row.ref)!==want)continue;
      try{
        roomIconRefs.set(loadKey,{ref:want,
          url:URL.createObjectURL(new Blob([row.bytes],{type:row.mime||'image/*'}))});
        filled++;
      }catch(_){ }
    }
    /* Same rule at the other end: a pass that painted nothing while an icon was still wanted has
     * not warmed anything, and must not claim it did. */
    if(!filled&&wanted)iconsWarmedFor='';
    if(filled&&document.body.classList.contains('concord-view'))backgroundRender();
  }

  async function hydrateStoredRoomIcon(p,room,loadKey){
    if(!room||!room.iconPointer||!loadKey||storedIconLoads.has(loadKey))return;
    storedIconLoads.add(loadKey);
    try{
      const changed=await applyRoomIconMetadata(room,{icon:room.iconPointer},loadKey);
      if(changed){const rooms=saved(),i=rooms.findIndex(x=>roomIdentity(x)===roomIdentity(room));if(i>=0){rooms[i].iconPointer=room.iconPointer;if(/^blob:/i.test(String(rooms[i].icon||'')))rooms[i].icon='';save(rooms);}}
      /* Repaint only after hydration produced an icon. On a corrupt pointer applyRoomIconMetadata
       * records backoff and returns false. Repainting anyway calls roomIcon() again after this
       * promise clears storedIconLoads, producing an unbounded fail -> render -> fail microtask
       * loop (observed at ~7 GB / 90% CPU in the packaged desktop). */
      if(roomIconRefs.has(loadKey)&&document.body.classList.contains('concord-view'))backgroundRender();
    }finally{storedIconLoads.delete(loadKey);}
  }
  function reactionSummary(p,m){
    const reactions=m&&m.reactions&&typeof m.reactions==='object'?m.reactions:{};
    const urls=m&&m.reactionUrls&&typeof m.reactionUrls==='object'?m.reactionUrls:{};
    const viewer=p.viewer?p.viewer():{};
    return Object.entries(reactions).map(([emoji,people])=>{ const n=Array.isArray(people)?people.length:0,mine=!!(viewer.pubkey&&people.includes(viewer.pubkey)),url=/^https?:\/\//i.test(String(urls[emoji]||''))?String(urls[emoji]):'',face=url?`<img class="cc-reaction-emoji" src="${p.enc(url)}" alt="${p.enc(emoji)}" title="${p.enc(emoji)}" loading="lazy">`:`<span>${p.enc(emoji)}</span>`; return n?`<button class="cc-reaction${mine?' mine':''}" aria-pressed="${mine}" data-cc-react-toggle="${p.enc(messageId(m))}" data-cc-emoji="${p.enc(emoji)}" title="${n} reaction${n===1?'':'s'}">${face}<b>${n}</b></button>`:''; }).join('');
  }
  function zapSummary(p,m){
    const zaps=Array.isArray(m&&m.zaps)?m.zaps:[];if(!zaps.length)return '';
    const sats=zaps.reduce((n,z)=>n+Math.max(0,Number(z&&z.sats)||0),0);
    return `<span class="cc-zap-tally" title="${zaps.length} verified private zap${zaps.length===1?'':'s'}">⚡ ${sats.toLocaleString()} sats</span>`;
  }
  /* KEYED ON THE ROOM'S IDENTITY, NOT ITS naddr. `roomIdentity` is communityId||naddr||url and is
   * what every other per-room store here uses; `naddr` alone is absent on a NIP-29 room and on one
   * joined by community id, and those rooms got no mention notifications at all — the guard below
   * returned before looking. A room that HAS an naddr keeps its existing cursor, so no history is
   * re-announced; a room that never had one starts a cursor silently on its next read. */
  /* DOES THIS MESSAGE MENTION ME? One rule, used by the notifier AND the renderer.
   *
   * It was only ever asked by `notifyMentions`, which fires an OS notification and nothing else —
   * so a message that tagged you looked identical to every other message in the channel. Reported
   * as "its not tagging users in concord right so they get the mention or highlight like armada
   * does it": the tags were published correctly and nothing on screen ever said so.
   *
   * A `p`/`P` tag is the authoritative answer (that is what the composer publishes for everyone
   * tagged); the text check keeps a message from a client that only writes the handle. */
  function viewerHandles(viewer,me){
    const profile=(viewer&&viewer.profile)||{};
    return [me,profile.name,profile.display_name,viewer&&viewer.npub,viewer&&viewer.pubkey].filter(Boolean);
  }
  function messageMentionsViewer(m,viewer,me){
    if(!m||!viewer||!viewer.pubkey)return false;
    if(m.pubkey===viewer.pubkey)return false;                   // your own message is not a mention
    if((m.tags||[]).some(t=>(t[0]==='p'||t[0]==='P')&&String(t[1]||'')===viewer.pubkey))return true;
    return textMentionsViewer(String(m.text||''),viewerHandles(viewer,me));
  }
  /* PAINT THE @HANDLE. Runs over the ALREADY-ESCAPED html, so nothing here can introduce markup —
   * the only thing it adds is a span around a run that is already inert (the same rule the mail
   * linkifier follows). The `(^|\s)` guard is what keeps it off `https://x.com/@someone`, where the
   * @ is preceded by a slash. */
  function paintMentions(html,handles){
    const mine=new Set((handles||[]).map(h=>String(h||'').trim().toLowerCase().replace(/\s+/g,'_')).filter(Boolean));
    return String(html||'').replace(/(^|\s)@([\w.-]+)/g,(whole,pre,name)=>
      pre+'<span class="cc-mention'+(mine.has(String(name).toLowerCase())?' cc-mention-me':'')+'">@'+name+'</span>');
  }
  function mentionSeenKey(room,channel,pk=accountPk()){ return 'pc.concord.seen.v2.'+pk+'.'+roomIdentity(room)+':'+(channel||'general'); }
  /* A MENTION IS KEPT UNTIL IT IS READ, NOT ANNOUNCED ONCE AND FORGOTTEN.
   *
   * "i got tagged twice in a concord room today but never got notification" -- tagged while no
   * PosterChan was running anywhere, then nothing on login. A Concord message is encrypted to the
   * room's keys, so the server cannot see a mention to push it; the only signal was a one-shot OS
   * notification from a RUNNING client, and none at all on a device's first read of a channel. The
   * ledger is what the bell counts and what Notifications lists ("2 mentions in Lounge Chat · #general"),
   * shared by every window of this app through localStorage, and an entry leaves only when that channel
   * is actually on screen. notifs.js reads the same key without loading this module. */
  /* Per account, like everything else here: the ledger is "mentions OF YOU", and the bell of the
   * account switched to must not count the other one's. notifs.js reads the same `<base><pubkey>`. */
  const MENTION_LEDGER='pc.concord.mentions.v1.', MENTION_LOOKBACK_MS=7*86400000;
  function mentionLedger(pk=accountPk()){ const key=accountKey(MENTION_LEDGER,pk); if(!key)return {}; try{ const v=JSON.parse(localStorage.getItem(key)||'{}'); return v&&typeof v==='object'&&!Array.isArray(v)?v:{}; }catch(_){ return {}; } }
  function saveMentionLedger(l,pk=accountPk()){
    const key=accountKey(MENTION_LEDGER,pk); if(!key)return;
    try{ localStorage.setItem(key,JSON.stringify(l)); }catch(_){ }
    try{ const pc=PC(); if(pc&&pc.bumpNotif)pc.bumpNotif(); }catch(_){ }
  }
  function mentionLedgerKey(room,channel){ return roomIdentity(room)+'\n'+(channel||'general'); }
  function viewingChannel(room,channel){
    try{
      if(typeof document==='undefined'||!document.body||!document.body.classList.contains('concord-view'))return false;
      if(document.visibilityState&&document.visibilityState!=='visible')return false;
      const open=saved()[state.community];
      return !!open&&roomIdentity(open)===roomIdentity(room)&&(state.channel||'general')===(channel||'general');
    }catch(_){ return false; }
  }
  function recordMention(room,channel,m,pk=accountPk()){
    const l=mentionLedger(pk),key=mentionLedgerKey(room,channel),id=messageId(m);
    const row=l[key]||{room:roomIdentity(room),name:'',channel:channel||'general',ids:[],at:0,last:''};
    if((row.ids||[]).includes(id))return false;
    row.ids=[...(row.ids||[]),id].slice(-50);
    row.name=String(room.name||row.name||'').slice(0,80);
    if((Number(m.at)||0)>=row.at){ row.at=Number(m.at)||0; row.last=id; }
    l[key]=row; saveMentionLedger(l,pk); return true;
  }
  function clearMentions(room,channel){
    const l=mentionLedger(),key=mentionLedgerKey(room,channel);
    if(l[key]){ delete l[key]; saveMentionLedger(l); }
  }
  /* READ FOLLOWS THE ACCOUNT ("old concord notifications appear as new notifications when you load
   * concord on a different device"). The ledger and the per-channel cursor are localStorage, i.e. one
   * browser profile -- and a device's FIRST read of a channel deliberately puts the last week's
   * mentions in the ledger (the "tagged while nothing was running" case). So a mention read on the
   * phone came back as new on the laptop, the desktop app and the APK, one by one.
   *
   * Where each channel was read up to now lives in ONE kind-30078 doc, `d=pcai:concord-read`,
   * NIP-44-sealed to the user's own key: { "<room>\n<channel>": <ms> }. A mention at or before that
   * mark is not new on any device. The replaceable-doc rules: nothing is written until a relay has
   * ANSWERED a read (else a slow pool publishes {} over every mark), and each save re-reads and keeps
   * the LATER mark per channel, so two devices reading different channels cannot erase each other.
   * Every failure resolves to the old per-device behaviour -- a mention shown twice, never one lost. */
  const READ_D='pcai:concord-read', READ_CAP=500;
  const readDoc={owner:'',answered:false,marks:{},loading:null,saving:null,dirty:false};
  function readMark(room,channel){ return Number(readDoc.marks[mentionLedgerKey(room,channel)])||0; }
  function mergeMarks(into,from){
    let changed=false;
    for(const [k,v] of Object.entries(from||{})){
      const n=Number(v);
      if(typeof k!=='string'||!k.includes('\n')||!Number.isFinite(n)||n<=0)continue;
      if(n>(Number(into[k])||0)){ into[k]=n; changed=true; }
    }
    return changed;
  }
  function trimMarks(m){
    const rows=Object.entries(m).sort((a,b)=>b[1]-a[1]).slice(0,READ_CAP);
    return Object.fromEntries(rows);
  }
  /* The mentions this account has already read elsewhere leave the ledger (and so the bell). */
  function pruneReadMentions(){
    const l=mentionLedger();let changed=false;
    for(const [key,row] of Object.entries(l)){
      const mark=Number(readDoc.marks[key])||0;
      if(mark&&row&&(Number(row.at)||0)<=mark){ delete l[key]; changed=true; }
    }
    if(changed)saveMentionLedger(l);
  }
  /* {answered, marks} from the relays, or null when nobody could be asked. */
  async function fetchReadDoc(p,owner){
    if(!p.relayQuery||!p.nip44dec)return null;
    let evs=null;
    try{ evs=await p.relayQuery([{authors:[owner],kinds:[30078],'#d':[READ_D],limit:1}],8000); }catch(_){ evs=null; }
    let cached=[];try{cached=window.Store&&window.Store.query?window.Store.query([{authors:[owner],kinds:[30078],'#d':[READ_D]}])||[]:[];}catch(_){}
    const all=[...(Array.isArray(evs)?evs:[]),...cached].filter(e=>e&&e.pubkey===owner&&typeof e.content==='string');
    // "Nothing came back" counts as an answer only when the relays SAID so; a timeout is not "no doc".
    if(!all.length&&!docAnswered(evs))return null;
    const ev=all.sort((a,b)=>b.created_at-a.created_at)[0];
    if(!ev)return {marks:{}};
    try{
      const doc=JSON.parse(await p.nip44dec(owner,ev.content)||'null');
      return {marks:doc&&typeof doc.marks==='object'&&!Array.isArray(doc.marks)?doc.marks:{}};
    }catch(_){ return null; }   // could not read it: never treat as empty, never overwrite it
  }
  function loadReadDoc(p){
    const owner=p&&p.viewer&&p.viewer().pubkey;
    if(!owner)return Promise.resolve(readDoc);
    if(readDoc.owner!==owner){ Object.assign(readDoc,{owner,answered:false,marks:{},loading:null,saving:null,dirty:false}); }
    if(readDoc.answered)return Promise.resolve(readDoc);
    if(readDoc.loading)return readDoc.loading;
    readDoc.loading=(async()=>{
      const got=await fetchReadDoc(p,owner);
      if(readDoc.owner!==owner)return readDoc;              // the account changed while we were reading
      if(got){ mergeMarks(readDoc.marks,got.marks); readDoc.answered=true; pruneReadMentions(); if(readDoc.dirty)saveReadDoc(p); }
      return readDoc;
    })().finally(()=>{ readDoc.loading=null; });
    return readDoc.loading;
  }
  function saveReadDoc(p){
    const owner=readDoc.owner;
    if(!owner||!readDoc.answered||!p.nip44enc||!p.signTemplate||!p.relayPublish){ return; }
    if(readDoc.saving){ readDoc.dirty=true; return; }
    readDoc.dirty=false;
    readDoc.saving=(async()=>{
      await new Promise(r=>setTimeout(r,1500));            // a channel read in bursts is one write
      const fresh=await fetchReadDoc(p,owner);
      if(!fresh||readDoc.owner!==owner){ readDoc.dirty=true; return; }   // could not re-read: write nothing
      mergeMarks(readDoc.marks,fresh.marks);
      readDoc.marks=trimMarks(readDoc.marks);
      const content=await p.nip44enc(owner,JSON.stringify({v:1,marks:readDoc.marks}));
      const ev=await p.signTemplate({kind:30078,created_at:Math.floor(Date.now()/1000),content,tags:[['d',READ_D]],pubkey:owner});
      await p.relayPublish(ev);
    })().catch(()=>{ readDoc.dirty=true; }).finally(()=>{ readDoc.saving=null; if(readDoc.dirty&&readDoc.answered)setTimeout(()=>saveReadDoc(p),5000); });
  }
  /* WHAT ANOTHER DEVICE READ, WHILE THIS ONE IS OPEN. loadReadDoc answers once per session, so a desktop
   * left open never learned that the phone had read a room. Three ways in, all merge-only (MAX per
   * channel -- a copy can never un-read anything): a live subscription to the document, a re-read when the
   * page comes back to the foreground, and a re-read at most once a minute from the background sweep.
   * A re-read that could not ask changes nothing. */
  let readSync={owner:'',close:null,at:0,busy:false};
  function readMarksChanged(){
    pruneReadMentions();
    try{ paintUnreadBadge(); }catch(_){ }
    try{ if(document.body&&document.body.classList.contains('concord-view'))preserveChatScroll(()=>backgroundRender()); }catch(_){ }
  }
  async function adoptReadDoc(p,owner,ev){
    try{
      const doc=JSON.parse(await p.nip44dec(owner,ev.content)||'null');
      if(readDoc.owner!==owner||!doc||typeof doc.marks!=='object'||Array.isArray(doc.marks))return;
      if(mergeMarks(readDoc.marks,doc.marks))readMarksChanged();
    }catch(_){ }
  }
  async function refreshReadDoc(p,force){
    const owner=p&&p.viewer&&p.viewer().pubkey;
    if(!owner||readSync.busy||(!force&&Date.now()-readSync.at<60000))return;
    readSync.busy=true;readSync.at=Date.now();
    try{
      if(readDoc.owner!==owner||!readDoc.answered){ await loadReadDoc(p); if(readDoc.answered)readMarksChanged(); return; }
      const got=await fetchReadDoc(p,owner);
      if(got&&readDoc.owner===owner&&mergeMarks(readDoc.marks,got.marks))readMarksChanged();
    }finally{ readSync.busy=false; }
  }
  function startReadSync(p){
    const owner=p&&p.viewer&&p.viewer().pubkey;
    if(!owner||readSync.owner===owner)return;
    if(readSync.close){ try{ readSync.close(); }catch(_){ } }
    readSync={owner,close:null,at:0,busy:false};
    if(p.relaySubscribe&&p.nip44dec){
      try{
        const sub=p.relaySubscribe([{authors:[owner],kinds:[30078],'#d':[READ_D]}],{live:true,onEvent:ev=>{
          if(!ev||ev.pubkey!==owner||Number(ev.kind)!==30078||typeof ev.content!=='string'
             ||!(ev.tags||[]).some(t=>t&&t[0]==='d'&&t[1]===READ_D))return;
          if(readSync.owner===owner)adoptReadDoc(p,owner,ev);
        }});
        readSync.close=typeof sub==='function'?sub:(()=>{ try{ if(p.relayClose)p.relayClose(sub); }catch(_){ } });   // through the app's wrapper, like discovery
      }catch(_){ }
    }
    try{ if(!startReadSync.wired&&typeof document!=='undefined'&&document.addEventListener){ startReadSync.wired=true;
      document.addEventListener('visibilitychange',()=>{ if(document.visibilityState==='visible'){ const q=PC(); if(q)refreshReadDoc(q,true).catch(()=>{}); } }); } }catch(_){ }
    refreshReadDoc(p,true).catch(()=>{});
  }
  /* This channel is on screen up to `at`: read here, and so read everywhere. */
  function noteChannelRead(p,room,channel,at){
    const n=Number(at)||0;if(!room||!n)return;
    loadReadDoc(p);
    const key=mentionLedgerKey(room,channel);
    if(n<=(Number(readDoc.marks[key])||0))return;
    readDoc.marks[key]=n;
    if(readDoc.answered)saveReadDoc(p); else readDoc.dirty=true;
  }
  function mentionsUnread(){ return Object.values(mentionLedger()).reduce((n,r)=>n+((r&&r.ids)||[]).length,0); }
  /* COMMUNITY NOTIFICATIONS ARE A SWITCH, PER ACCOUNT (2026-10-21: "the bell only says enabled, never disabled" --
   * it only ever asked the OS for permission and reported the answer, so there was no way to turn them off).
   * Off = no OS notification from Concord (mentions, repository activity); the in-app badges and the mention
   * ledger still record everything, so nothing is lost while it is off. */
  const NOTIFY_KEY=pk=>'pc.concord.notify.v1.'+pk;
  function concordNotifyOn(){ const pk=accountPk(); if(!pk)return true; try{   // no account to read a choice for: the default (on)
    return localStorage.getItem(NOTIFY_KEY(pk))!=='off'; }catch(_){ return true; } }
  function setConcordNotify(on){ const pk=accountPk(); if(!pk)return; try{ localStorage.setItem(NOTIFY_KEY(pk),on?'on':'off'); }catch(_){} }
  function notifyMentions(p,room,messages,viewer,me,channel=state.channel||'general'){
    if(!room||!roomIdentity(room)||!messages.length||!viewer.pubkey)return;
    const key=mentionSeenKey(room,channel,viewer.pubkey), newest=Math.max(...messages.map(m=>Number(m.at)||0));
    /* The original release stored one cursor for the whole community. Only #general can inherit
     * that value safely: applying its newest timestamp to every channel lets a newer general post
     * permanently suppress an older (but newly fetched) #support mention. */
    /* No fallback to the shared cursors of older builds: one advanced while ANOTHER account read this
     * room would bury this account's mentions for good. Without it a device's first read here is a
     * first read -- the week's mentions go to the ledger once, minus whatever READ_D says was read. */
    let seen=Number(localStorage.getItem(key)||0);
    /* A device's FIRST read of a channel still raises no OS notification (opening history must not
     * alert) -- but a mention from the last week goes into the ledger, which is exactly the case of
     * being tagged while nothing was running. */
    // …unless this account read the channel past that point on another device (READ_D above). The doc
    // may still be on its way: when it lands, pruneReadMentions takes back what it covers.
    loadReadDoc(p);
    const first=!seen, floor=Math.max(first?Date.now()-MENTION_LOOKBACK_MS:seen, readMark(room,channel)), looking=viewingChannel(room,channel);
    for(const m of messages){
      const at=Number(m.at)||0;
      if(at<=floor||!messageMentionsViewer(m,viewer,me))continue;
      if(!looking)recordMention(room,channel,m,viewer.pubkey);
      if(!first&&p.osNotify&&concordNotifyOn()) p.osNotify(`Mention in #${channel}`,`${m.by||'Someone'}: ${mentionNames(String(m.text||''),p.profOf)}`,{tag:'concord-mention-'+roomIdentity(room)+':'+channel+':'+messageId(m),route:notificationRoute(room,channel,m)});
    }
    if(newest>seen)localStorage.setItem(key,String(newest));
  }
  function inviteParts(raw){
    let u; try{ u=new URL(String(raw||'').trim()); }catch(_){ return null; }
    const m=u.pathname.match(/\/invite\/(naddr1[023456789acdefghjklmnpqrstuvwxyz]+)\/?$/i);
    return m&&u.hash.length>3 ? {url:u.href,naddr:m[1],secret:u.hash.slice(1)} : null;
  }
  /* AN INVITE IS FOR SOMEBODY ELSE, SO ITS HOST MUST BE ONE THEY CAN REACH. Both mint paths passed
   * `location.origin`, which is this instance in a browser tab and `https://localhost` in the APK
   * (`app://posterchan` on the desktop) -- so every invite a phone ever minted pointed at the
   * recipient's OWN phone. The instance the app talks to is `p.serverOrigin()`; with no instance
   * (or one only this machine can reach) the canonical CORD-05 host is used, the same one
   * `inviteRefUrl` builds for Armada's membership list. The host is never part of the secret: the
   * naddr and the # fragment are the whole invitation, which is also why an invite ALREADY minted
   * against localhost still opens (`inviteParts` reads the path, not the host) and is rewritten to a
   * public host whenever it is copied again (`shareableInviteUrl`). */
  const PUBLIC_INVITE_HOST='https://armada.buzz';
  function unreachableOrigin(raw){
    try{
      const u=new URL(String(raw||''));if(!/^https?:$/.test(u.protocol))return true;
      const h=u.hostname.replace(/^\[|\]$/g,'').toLowerCase();
      return h==='localhost'||h.endsWith('.localhost')||h==='::1'||h==='0.0.0.0'||/^127\./.test(h);
    }catch(_){ return true; }
  }
  function publicInviteBase(p){
    let origin='';try{origin=String((p&&p.serverOrigin&&p.serverOrigin())||'');}catch(_){}
    return origin&&!unreachableOrigin(origin)?new URL(origin).origin:PUBLIC_INVITE_HOST;
  }
  function shareableInviteUrl(p,raw){
    const parts=inviteParts(raw);if(!parts||!unreachableOrigin(parts.url))return parts?parts.url:String(raw||'');
    const u=new URL(parts.url);return publicInviteBase(p)+u.pathname+u.hash;
  }
  /* An invite is an in-app Concord action, not a web page.  Linkified chat text gives every URL a
   * normal anchor; on desktop a same-instance /invite link was therefore allowed through Electron
   * and opened the server's Classic UI in another PosterChan window.  Keep the complete URL (the
   * fragment is the decryption secret), show it in the existing join surface, and use the exact same
   * join button as a pasted invite so the two paths cannot drift. */
  /* PREVIEW, NEVER JOIN. This used to auto-click the join button, so tapping a link somebody put in
   * a message enrolled you in their community before you had seen its name. Being sent an invitation
   * is not accepting it. The second parameter is GONE rather than defaulted off: both call sites
   * passed `true` explicitly, so a default would have changed nothing, and a flag that silently
   * enrols somebody is not worth keeping for a caller that does not exist. */
  function openInviteLink(raw){
    const parsed=inviteParts(raw); if(!parsed)return false;
    const panel=document.querySelector('#cc-join'),input=document.querySelector('#cc-invite-url');
    if(!panel||!input)return false;
    // Opening it again is reconsidering it — see forgetDeclinedInvite.
    forgetDeclinedInvite(parsed.url);
    panel.classList.remove('hidden'); input.value=parsed.url; input.focus();
    setTimeout(()=>{const go=document.querySelector('#cc-join-go');if(go&&!go.disabled)go.click();},0);
    return true;
  }
  function discoverInvites(text,source){
    const content=String(text||''),pattern=/https?:\/\/[^\s<>]+\/invite\/naddr1[023456789acdefghjklmnpqrstuvwxyz]+#[A-Za-z0-9_-]+/gi;
    const matches=[...content.matchAll(pattern)];
    const blurb=content.replace(pattern,'').replace(/#[\w-]+/g,'').replace(/\s+/g,' ').trim();
    return matches.map(match=>{
      const url=match[0].replace(/[),.;!?]+$/,''),parsed=inviteParts(url);if(!parsed)return null;
      // Directory announcements contain several distinct invites. Use the label beside this
      // link, not the announcement's shared opening sentence, and never merge by display name.
      const before=content.slice(0,match.index).replace(/[ \t]*\r?\n[ \t]*$/,'');
      const line=before.slice(before.lastIndexOf('\n')+1).trim();
      const label=/^([^:\r\n]+):\s*$/.exec(line);
      const name=label?label[1].replace(/^[-*•]\s+/,'').trim():blurb;
      return {...parsed,name:name.slice(0,80)||'Public Concord community',description:blurb,source};
    }).filter(Boolean);
  }
  /* v2 BECAUSE v1 HOLDS RECORDS NOTHING CAN CLEAR, AND THAT IS A COMMUNITY HIDDEN FOR EVER.
   *
   * A build of this file seeded the ledger from EVERY winning tombstone, and a tombstone with no
   * membership entry beside it always wins (`added_at` defaults to 0). So a community that had
   * merely been tombstoned once -- with nothing to compare the tombstone against -- was written
   * into this device's permanent local blacklist, and `wasLocallyLeft` refused it on every pass
   * afterwards. Reported as "my concord community for posterchan is no longer appearing in
   * Concord", and still true after the seeding rule was corrected: the correction stops NEW bad
   * records being written and cannot remove the ones already on disk. Measured on the laptop after
   * that fix shipped -- one row, for the community the person was asking about, removedAt the
   * afternoon the bad build ran.
   *
   * The heal beside it cannot reach these either: it requires a live entry AND a visible tombstone,
   * and the whole point of a stranded record is that there is no entry to find.
   *
   * So this build starts with an empty ledger, and that is SAFE rather than merely expedient:
   *   * a genuine leave is re-seeded on the very next membership pass, by the corrected rule -- a
   *     tombstone that beat a real entry, which is the shape a real leave has;
   *   * a community left with a tombstone and no entry is still removed every pass by `dead`, which
   *     does not consult this ledger at all;
   *   * only a room rebuilt from the OWNER'S OWN invite announcement, carrying no community_id, is
   *     unprotected until it is left once more -- and a community that comes back once, which one
   *     press puts away again, is not the same kind of loss as a community that can never come back.
   * v1 is deliberately left on disk rather than deleted: another device may still be running the
   * older build, and this key is that build's memory. */
  /* AN INVITATION IS A QUESTION, AND IT HAD NO "NO".
   *
   * `#cc-join-go` is LABELLED "Preview invite" and joined outright — hydrate, push into the room
   * list, switch to it, publish the membership. Clicking an invite in a chat message did the same
   * thing without even that button, because openInviteLink auto-clicked it. So being sent a link
   * was the same act as accepting it, and there was no way to look at a community first or to say
   * no to one.
   *
   * DECLINING IS A DISMISSAL, NOT A BLACKLIST, and that distinction is load-bearing. The ledger
   * three lines below exists because a build wrote communities into a permanent local refusal and
   * `wasLocallyLeft` then hid one for ever — "my concord community for posterchan is no longer
   * appearing in Concord" — and a corrected rule could not remove the records already on disk. So a
   * declined invitation is remembered ONLY so the same link stops re-asking: it is keyed on the
   * invite, it never touches `leftCommunities`, and it is overridden the moment somebody opens that
   * invite again or joins the community by any other route. Saying no to an invitation must not be
   * a decision you cannot take back. */
  /* WHAT AN INVITATION LOOKS LIKE BEFORE YOU ANSWER IT.
   *
   * Everything shown here comes from the invite the person was actually handed — hydrateInvite
   * decrypts the bundle and gives the community's own name; nothing is invented and nothing is
   * asserted that has not been read. An invite whose bundle cannot be opened never reaches this
   * point, so the card cannot describe a community that is not there.
   *
   * The name is escaped: an invite is written by whoever sent it, and a community name is their
   * text. */
  function mergeDirectInviteRoom(existing,incoming){
    const old=existing?.cord?.bundle,fresh=incoming?.cord?.bundle;
    if(!old||!fresh||old.community_id!==fresh.community_id)throw new Error('Invitation does not match this community');
    if(BigInt(old.root_epoch)!==BigInt(fresh.root_epoch)||old.community_root!==fresh.community_root||
       (fresh.control_pk!==undefined&&fresh.control_pk!==old.control_pk))
      throw new Error('Refresh this community before accepting an invitation from a different key epoch');
    const channels=new Map((old.channels||[]).map(channel=>[channel.id,{...channel}]));
    for(const grant of fresh.channels||[]){
      const held=channels.get(grant.id);
      if(!held){channels.set(grant.id,{...grant});continue;}
      const oldEpoch=BigInt(held.epoch),newEpoch=BigInt(grant.epoch);
      if(oldEpoch===newEpoch){if(held.key!==grant.key)throw new Error('Conflicting channel key at the same epoch');continue;}
      if(newEpoch<oldEpoch)throw new Error('Invitation carries an older channel key');
      const current={...held,...grant};
      const prior=newEpoch>oldEpoch?held:grant;
      current.held_keys=[...(held.held_keys||[]).filter(key=>BigInt(key.epoch)!==BigInt(prior.epoch)),{epoch:prior.epoch,key:prior.key}];
      channels.set(grant.id,current);
    }
    const merged=mergeRoom(existing,incoming);
    merged.cord={...(merged.cord||{}),bundle:{...old,channels:[...channels.values()]}};
    return merged;
  }

  /* AN INVITATION YOU CAN SEE. "New Concord invitation — open Concord to review it" led to a Concord that said
   * nothing about it (2026-10-21): the review lived only behind the header's "Invites" button, which a phone
   * folds into the ⋯ menu. The count of pending invitations is kept here, refreshed when one arrives (dms.js
   * fires pc-concord-direct-invites) and on every render, and drawn as a card on the Concord home AND above the
   * channel list, plus a number on Invites. Reading the count never touches the network (api.pending is the
   * parked list). */
  let pendingInviteCount=0,inviteCountBusy=false;
  async function refreshInviteCount(){
    if(inviteCountBusy)return;inviteCountBusy=true;
    try{const p=PC();if(!p||!p.cordDirectContext||!p.cordDirectModule)return;const context=p.cordDirectContext();if(!context||!context.pubkey)return;
      const api=await p.cordDirectModule(),n=(api.pending(context)||[]).length;
      if(n!==pendingInviteCount){pendingInviteCount=n;backgroundRender();}}
    catch(_){}finally{inviteCountBusy=false;}
  }
  try{window.addEventListener('pc-concord-direct-invites',()=>{void refreshInviteCount();});}catch(_){}
  function inviteCardHtml(p){
    const n=pendingInviteCount;if(!n)return '';
    return `<button type="button" class="cc-invite-card" data-cc-review-invites><span aria-hidden="true">✉</span><span><b>${n===1?'You have an invitation':'You have '+n+' invitations'}</b><small>Review before joining — no community is contacted until you accept</small></span><strong>Review</strong></button>`;
  }
  /* THE INVITATIONS, EACH ONE NAMED. The list used to be a column of identical "Review invitation" buttons --
   * you could not tell one community from another until you opened it. Every invitation is opened (decrypted
   * on this device; no community is contacted) and drawn as a card: the community, who invited you, Accept and
   * Decline. Tapping the card shows the full invitation. Accept goes through the review sheet's own button, so
   * there is one join path, not two. */
  async function showDirectInvitations(){
    const p=PC(),context=p.cordDirectContext(),api=await p.cordDirectModule();
    if(!context.isCurrent())return;
    const wraps=api.pending(context);
    const asInvite=invite=>{const bundle=invite.bundle;return {directId:invite.id,account:context.pubkey,by:invite.inviter,
      room:{communityId:bundle.community_id,name:bundle.name||'Concord community',description:'',channels:[{name:'general',private:false}],local:false,cord:{bundle}}};};
    const show=inv=>{ pendingInvite=inv; p.closeModal(); render(); };
    p.modal('<h3>Concord invitations</h3><p class="muted">Nothing is shared with a community until you accept.</p><div id="cc-direct-list" class="cc-direct-list"></div>',async root=>{
      const list=root.querySelector('#cc-direct-list');
      if(!wraps.length){list.innerHTML='<p class="muted">No pending invitations.</p>';return;}
      list.innerHTML='<p class="muted" id="cc-direct-loading">Opening '+wraps.length+' invitation'+(wraps.length===1?'':'s')+'…</p>';
      const opened=[];
      for(const wrap of wraps){
        try{ opened.push(asInvite(await api.open(wrap,context))); }
        catch(e){ opened.push({error:String(e&&e.message||e),wrap}); }
        if(!context.isCurrent()||!root.isConnected)return;
      }
      list.innerHTML='';
      for(const inv of opened){
        const card=document.createElement('div');card.className='cc-direct-card';
        if(inv.error){
          card.innerHTML='<div class="cc-direct-main"><b>An invitation that could not be read</b><small>'+p.enc(inv.error.slice(0,160))+'</small></div>'
            +'<div class="cc-direct-btns"><button type="button" class="btn btn-ghost" data-cc-direct-decline>Remove</button></div>';
          card.querySelector('[data-cc-direct-decline]').onclick=()=>{ try{ api.dismiss(inv.wrap.id,context); }catch(_){} card.remove(); void refreshInviteCount(); if(!list.children.length)list.innerHTML='<p class="muted">No pending invitations.</p>'; };
          list.appendChild(card);continue;
        }
        const name=String(inv.room.name||'Concord community'),who=inviterOf(p,inv);
        card.innerHTML='<button type="button" class="cc-direct-main" data-cc-direct-view aria-label="Show the invitation to '+p.enc(name)+'">'
          +'<span class="concord-mark" aria-hidden="true">'+p.enc(name.slice(0,1).toUpperCase()||'C')+'</span>'
          +'<span class="cc-direct-text"><b>'+p.enc(name)+'</b><small>Invited by '+p.enc(who.name||'someone')+'</small></span></button>'
          +'<div class="cc-direct-btns"><button type="button" class="btn btn-ghost" data-cc-direct-decline>Decline</button>'
          +'<button type="button" class="btn btn-neon" data-cc-direct-accept>Accept</button></div>';
        card.querySelector('[data-cc-direct-view]').onclick=()=>show(inv);
        card.querySelector('[data-cc-direct-accept]').onclick=()=>{ show(inv); const a=document.querySelector('#cc-invite-accept'); if(a)a.click(); };
        card.querySelector('[data-cc-direct-decline]').onclick=()=>{
          if(inv.account===p.viewer()?.pubkey)window.PCCordDirectInvites.dismiss(inv.directId,p.cordDirectContext());
          card.remove(); void refreshInviteCount(); p.toast('Invitation declined');
          if(!list.children.length)list.innerHTML='<p class="muted">No pending invitations.</p>';
        };
        list.appendChild(card);
      }
    });
  }
  function showDirectInviteSender(room){
    const p=PC(),source=room?.cord?.bundle;if(!source){p.toast('Open a Concord community first');return;}
    const account=p.viewer()?.pubkey;
    // A member can grant only the keys already held; never include staff/vault material.
    const bundle={};
    for(const key of ['community_id','owner','owner_salt','community_root','root_epoch','control_pk','channels','relays','name','icon','expires_at','creator_npub','label'])
      if(source[key]!==undefined)bundle[key]=source[key];
    if(Array.isArray(bundle.channels))bundle.channels=bundle.channels.map(ch=>({id:ch.id,key:ch.key,epoch:ch.epoch,...(ch.name!==undefined?{name:ch.name}:{})}));
    p.modal('<h3>Invite someone to '+p.enc(room.name||'this community')+'</h3><p class="muted">Sends the community and channel keys you hold to this account.</p><label for="cc-direct-recipient">Recipient</label><input class="input" id="cc-direct-recipient" autocomplete="off" placeholder="Search name or paste npub"><button class="btn btn-neon" id="cc-direct-send-confirm">Send invitation</button>',root=>{
      const input=root.querySelector('#cc-direct-recipient'),button=root.querySelector('#cc-direct-send-confirm');
      const cleanup=p.attachUserAutocomplete?.(input);
      const observer=new MutationObserver(()=>{if(!root.isConnected){cleanup?.();observer.disconnect();}});
      observer.observe(document.documentElement,{childList:true,subtree:true});
      button.onclick=async()=>{
        if(p.viewer()?.pubkey!==account)return;
        button.disabled=true;
        try{
          bundle.relays=roomRelays(source);
          await p.sendCordDirectInvite(bundle,input.value);
          if(p.viewer()?.pubkey!==account||!root.isConnected)return;
          p.closeModal();p.toast('Concord invitation sent');
        }catch(e){if(p.viewer()?.pubkey===account&&root.isConnected){button.disabled=false;p.toast(e.message||String(e));}}
      };
    });
  }

  /* AN INVITATION IS SHOWN AS WHAT IT IS: the community, who invited you, and two answers. It used to be drawn
   * INSIDE the "Join or create a community" dialog -- under a heading and a box asking to paste a link, with the
   * invitation squeezed into the card below; on a phone that card ran off the right edge and Join was a sliver
   * ("when you click review, it asks you to paste a url? That is terrible!", 2026-10-21). It is its own sheet now
   * (#cc-invite-sheet). Nothing here is fetched from the community: no icon or banner until you accept, because
   * loading one would tell its host who was invited. The inviter is shown from the profile this client holds. */
  function inviterOf(p,inv){
    const by=String(inv&&inv.by||'');
    if(/^[0-9a-f]{64}$/i.test(by)){
      let prof={};try{prof=(p.profOf&&p.profOf(by))||{};}catch(_){}
      let npub='';try{npub=window.NostrTools?NostrTools.nip19.npubEncode(by):'';}catch(_){}
      return {name:String(prof.display_name||prof.name||(npub?npub.slice(0,12)+'…':'someone')),picture:String(prof.picture||''),pk:by};
    }
    return {name:by,picture:'',pk:''};
  }
  function invitePreviewHtml(p,inv){
    const room=inv&&inv.room||{};
    const name=String(room.name||'Concord community');
    const desc=String(room.description||'');
    const known=saved().some(r=>sameRoom(r,room));
    const who=inv?.by?inviterOf(p,inv):null;
    const chans=(room.cord?.bundle?.channels||[]).filter(c=>c&&!c.private).length;
    return '<div class="cc-join-card cc-invite-review" id="cc-invite-preview" role="dialog" aria-modal="true" aria-labelledby="cc-invite-title">'
      + '<button type="button" class="cc-invite-close" id="cc-invite-close" aria-label="Not now" title="Not now">×</button>'
      + '<div class="cc-invite-kicker">'+(known?'You are already in':'You are invited to join')+'</div>'
      + '<div class="cc-invite-head"><div class="concord-mark" aria-hidden="true">'+p.enc(name.slice(0,1).toUpperCase()||'C')+'</div>'
      + '<h2 id="cc-invite-title">'+p.enc(name)+'</h2></div>'
      + (who&&who.name?'<div class="cc-invite-by">'+(who.picture?'<img class="cc-invite-av" src="'+p.enc(who.picture)+'" alt="" loading="lazy" referrerpolicy="no-referrer">':'<span class="cc-invite-av" aria-hidden="true">'+p.enc(who.name.slice(0,1).toUpperCase())+'</span>')
          +'<span>Invited by <b>'+p.enc(who.name)+'</b></span></div>':'')
      + (desc?'<p class="cc-invite-desc">'+p.enc(desc.slice(0,240))+'</p>':'')
      + (chans>1?'<p class="cc-invite-meta">'+chans+' public channels</p>':'')
      + '<p class="cc-invite-note">'+(known
          ? 'You are already in this community — joining again just opens it.'
          : 'Nothing is shared with the community until you accept. Joining publishes your membership so your other devices see it too.')+'</p>'
      + '<div class="cc-invite-actions">'
      + '<button class="btn btn-neon" id="cc-invite-accept">'+(known?'Open community':'Accept and join')+'</button>'
      + '<button class="btn btn-ghost" id="cc-invite-decline">Decline</button>'
      + '</div></div>';
  }
  /* A decline is one account's answer. Not carried over from the shared v1 list: an invite this
   * account never answered must not stay hidden because another account turned it down -- at worst a
   * declined invite is offered once more. */
  const DECLINED_INVITES_KEY='pc.concord.declined.v2.';
  let pendingInvite=null;          // {url, room, by} — the invitation on screen, before any answer
  function declinedInvites(){
    const store=accountKey(DECLINED_INVITES_KEY); if(!store)return [];
    try{ const v=JSON.parse(localStorage.getItem(store)||'[]'); return Array.isArray(v)?v:[]; }
    catch(_){ return []; }
  }
  function declineInvite(url){
    const key=inviteKey({url})||String(url||''),store=accountKey(DECLINED_INVITES_KEY); if(!key||!store)return false;
    const kept=[key,...declinedInvites().filter(x=>x!==key)].slice(0,200);
    try{ localStorage.setItem(store,JSON.stringify(kept)); }catch(_){}
    return true;
  }
  function inviteWasDeclined(url){
    const key=inviteKey({url})||String(url||''); return !!key&&declinedInvites().includes(key);
  }
  /* Opening it again IS reconsidering it. Nothing else needs to clear this, and nothing else may:
   * a record that only grows is the failure this file already carries a v2 key because of. */
  function forgetDeclinedInvite(url){
    const key=inviteKey({url})||String(url||''); if(!key)return;
    const store=accountKey(DECLINED_INVITES_KEY); if(!store)return;
    try{ localStorage.setItem(store,JSON.stringify(declinedInvites().filter(x=>x!==key))); }catch(_){}
  }

  const LEFT_COMMUNITIES_KEY='pc.concord.left.v2';
  function leftCommunities(pubkey){
    if(!pubkey)return [];
    try{const all=JSON.parse(localStorage.getItem(LEFT_COMMUNITIES_KEY)||'{}'),rows=all&&all[pubkey];return Array.isArray(rows)?rows:[];}catch(_){return [];}
  }
  function saveLeftCommunities(pubkey,rows){
    if(!pubkey)return;
    let all={};try{all=JSON.parse(localStorage.getItem(LEFT_COMMUNITIES_KEY)||'{}')||{};}catch(_){}
    all[pubkey]=(rows||[]).slice(-256);localStorage.setItem(LEFT_COMMUNITIES_KEY,JSON.stringify(all));
  }
  function leftMatches(row,room){
    const id=roomIdentity(room),naddr=String(room&&room.naddr||''),url=String(room&&room.url||'');
    return !!row&&(!!id&&row.communityId===id||!!naddr&&row.naddr===naddr||!!url&&row.url===url);
  }
  function rememberLeftCommunity(pubkey,room,removedAt=Date.now()){
    const rows=leftCommunities(pubkey).filter(row=>!leftMatches(row,room));
    rows.push({communityId:roomIdentity(room),naddr:String(room&&room.naddr||''),url:String(room&&room.url||''),removedAt:Number(removedAt)||Date.now()});
    saveLeftCommunities(pubkey,rows);
  }
  function forgetLeftCommunity(pubkey,room){saveLeftCommunities(pubkey,leftCommunities(pubkey).filter(row=>!leftMatches(row,room)));}
  function wasLocallyLeft(pubkey,room){return leftCommunities(pubkey).some(row=>leftMatches(row,room));}
  /* A LEAVE IS AN ACCOUNT DECISION, AND THIS LEDGER IS A DEVICE'S MEMORY OF ONE.
   *
   * The vault tombstone is the durable half and it is keyed on `community_id`; this ledger is what
   * the resurrection paths actually consult, and every one of them (a discovered announcement, an
   * old public invite card) knows a naddr and a URL and NO community id at all. So the tombstones
   * are replayed into this ledger on every membership pass — that is the only reason a laptop
   * knows about a community left on a phone. Written only when it says something new, because
   * this runs on a timer. */
  function noteLeftFromVault(pubkey,room,removedAt){
    if(!pubkey||!roomIdentity(room))return false;
    const known=leftCommunities(pubkey).find(row=>leftMatches(row,room));
    const naddr=String(room&&room.naddr||''),url=String(room&&room.url||'');
    if(known&&(!naddr||known.naddr===naddr)&&(!url||known.url===url))return false;
    rememberLeftCommunity(pubkey,{...room,naddr:naddr||(known&&known.naddr)||'',url:url||(known&&known.url)||''},
      known&&!removedAt?known.removedAt:removedAt);
    return true;
  }
  /* AN ANNOUNCEMENT MUST NOT BE ACTED ON BEFORE THE VAULT HAS BEEN READ.
   *
   * Discovery answers off a warm relay in milliseconds and the membership read is several
   * round trips behind it, so the owner's own kind-1 rebuilt the room, on screen, before anything
   * had learned it was tombstoned — and `recoveredOwnedInvites` then made that decision final for
   * the session. Hold the item instead and replay it once this account's vault has actually been
   * decoded. Nothing is lost: an announcement is not membership, so waiting costs a repaint. */
  let membershipReadFor='';
  const pendingOwnedInvites=[];
  function flushOwnedInvites(p){
    const queued=pendingOwnedInvites.splice(0,pendingOwnedInvites.length);
    for(const item of queued)recoverOwnedInvite(p,item);
  }
  function recoverOwnedInvite(p,item){
    const viewer=p.viewer?p.viewer():{};
    if(!item||!viewer.pubkey||item.source.pubkey!==viewer.pubkey||recoveredOwnedInvites.has(inviteKey(item)))return;
    if(membershipReadFor!==viewer.pubkey){
      if(!pendingOwnedInvites.some(x=>sameRoom(x,item)))pendingOwnedInvites.push(item);
      return;
    }
    /* An announcement is not membership. Owners commonly announce the invite once and later leave
     * the Soapbox room; discovery replays that old kind-1 on every reconnect. Never turn it back
     * into a joined room after an explicit leave. An intentional Join clears this ledger only
     * after its replacement membership document publishes successfully. */
    if(wasLocallyLeft(viewer.pubkey,item))return;
    recoveredOwnedInvites.add(inviteKey(item));
    const rooms=saved();
    if(rooms.some(r=>sameRoom(r,item)))return;
    rooms.push({url:item.url,naddr:item.naddr,name:item.name,description:item.description||'',channels:[{name:'general',private:false}],local:false});
    save(rooms);
  }
  const DIRECTORY_FILTERS=[{kinds:[1],search:'armada.buzz/invite',limit:100},{kinds:[1],search:'poster.place/invite',limit:100}];
  /* {events, ok, why}: `ok` only when a relay ANSWERED (EOSE). This node's relay first (its mirror of the
   * directory), then the directory relays themselves. */
  async function readDirectory(p,signal){
    if(!p.relayQueryFrom)return {events:[],ok:false,why:'relay reads unavailable'};
    const h=typeof homeRelayUrl==='function'?homeRelayUrl(p):'';
    if(h){
      const reach={};let got=[];
      try{got=await p.relayQueryFrom([h],DIRECTORY_FILTERS,{timeout:12000,max:1,exact:true,urgent:true,allowBlocked:true,signal,purpose:'concord directory (this server)',report:reach})||[];}catch(_){got=[];}
      if((reach.ok||[]).includes(h))return {events:got,ok:true};
      if(signal&&signal.aborted)return {events:[],ok:false,why:'aborted'};
    }
    const reach={};let got=[];
    try{got=await p.relayQueryFrom(DISCOVER_RELAYS,DIRECTORY_FILTERS,{timeout:9000,max:2,exact:true,urgent:true,allowBlocked:true,signal,purpose:'concord discover listings',report:reach})||[];}catch(_){got=[];}
    if((reach.ok||[]).length)return {events:got,ok:true};
    return {events:got,ok:false,why:(reach.failed||[]).length?'the directory relay did not answer':'the directory relay could not be asked'};
  }
  function startDiscovery(p){
    if(discoveryStarted||state.community!=null||!p.relaySubscribe)return; discoveryStarted=true;
    discoveryAbortController=typeof AbortController==='function'?new AbortController():null;
    const signal=discoveryAbortController&&discoveryAbortController.signal;
    const listings=[];
    /* Some relay pools replay their in-memory result set synchronously from subscribe(). Calling
     * render() from each callback re-entered the launch render (or replaced the home DOM hundreds
     * of times in one task), leaving Concord apparently frozen. Coalesce discovery paint to the
     * next task; the room list itself is updated immediately. */
    const paintDiscovery=()=>{if(discoveryPaintPending)return;discoveryPaintPending=true;setTimeout(()=>{discoveryPaintPending=false;if(state.community==null)backgroundRender();},0);};
    /* Public cards are summaries of their announcement event. Do not hydrate every invite merely
       because it scrolled into Discover: that multiplied 24 cards by every bootstrap/control relay
       (including legacy Ditto invites). Full bootstrap is reserved for the explicit Join action. */
    const onEvent=ev=>{if(signal&&signal.aborted)return;for(const item of discoverInvites(ev.content,ev)){const index=listings.findIndex(old=>sameRoom(old,item));if(index<0)listings.push(item);else if(Number(ev.created_at)>Number(listings[index].source.created_at))listings[index]=mergeRoom(listings[index],item);recoverOwnedInvite(p,item);}discovered=uniqueRooms(listings).sort((a,b)=>Number(b.source.created_at)-Number(a.source.created_at));paintDiscovery();};
    const filters=DIRECTORY_FILTERS;
    /* THE LIST IS DECIDED BY A READ SOMEBODY ANSWERED, NEVER BY THE FIRST EOSE.
     *
     * "No public communities found" was painted on the shared pool's EOSE — i.e. by this node's relay,
     * which held none of the invite notes — while the directory relay, asked through `queryFrom`
     * WITHOUT `urgent`, was silently SKIPPED whenever it was cooling down or another Concord read held
     * it (both ordinary when Concord opens: the membership sweep asks the same relay, with a 30-minute
     * failure cooldown), and that skip came back as `[]` too. The directory relay held 70 invites.
     *
     * Now: this node's relay answers the directory from the copy it fetched itself; if it cannot
     * (`unavailable:`, no socket), the directory relay is asked directly and urgently; and if NOBODY
     * answered, the panel says it could not reach the directory and offers Retry. The pool
     * subscription stays for invites that arrive live, and decides nothing. */
    try{ const _dsub=p.relaySubscribe(filters,{onEvent,live:true});
         discoverySubscription=_dsub?{close:()=>{if(p.relayClose)p.relayClose(_dsub);}}:null; }catch(_){ }
    void readDirectory(p,signal).then(res=>{
      if(signal&&signal.aborted)return;
      (res.events||[]).forEach(onEvent);
      if(res.ok){discoveryLoaded=true;discoveryUnreachable='';}
      else{discoveryLoaded=false;discoveryUnreachable=res.why||'no relay answered';}
      paintDiscovery();
    });
  }
  function stopDiscovery(){
    const subscription=discoverySubscription;discoverySubscription=null;discoveryStarted=false;
    const aborter=discoveryAbortController;discoveryAbortController=null;if(aborter)aborter.abort();
    if(subscription&&typeof subscription.close==='function')try{subscription.close();}catch(_){}
  }
  async function hydrateInvite(p,url,signal=null){
    if(!window.PosterCord||!p.relayQueryFrom)throw new Error('CORD protocol is unavailable');
    const parts=inviteParts(url); if(!parts)throw new Error('that is not a Concord invite link');
    const decoded=window.PosterCord.openInvite;
    /* openInvite validates the naddr and fragment; use its decoded bootstrap relays by first trying
       the shared CORD set plus the current pool. queryFrom covers relays outside the pool, query covers inside. */
    const parsed=window.PosterCord.inviteDetails(url);
    const filter=[{kinds:[33301],authors:[parsed.linkSigner],'#d':[''],limit:100}];
    const relays=[...new Set([...(parsed&&parsed.bootstrapRelays||[]),...CORD_RELAYS])];
    const [pool,external]=await Promise.all([p.relayQuery?p.relayQuery(filter,8000):[],p.relayQueryFrom(relays,filter,{timeout:10000,max:200,signal,purpose:'concord explicit invite',allowBlocked:true,failureCooldown:1800000})]);
    // Resolve the signed addressable coordinate once. A revoked/newer bundle must never
    // fall back to an older live copy; unsigned relay garbage cannot replace a signed one.
    const candidates=[...(pool||[]),...(external||[])].filter((ev,i,a)=>ev&&a.findIndex(x=>x&&x.id===ev.id)===i);
    const opened=decoded(url,candidates,{forJoin:false});
    const bundle=opened.bundle;
    /* `community_id` is the durable key used by Armada's 13302 membership vault.  Keeping it only
     * inside `cord.bundle` made persistArmadaMembership() return false, so an apparently successful
     * direct invite vanished on another device or after browser storage was cleared. */
    return {url,naddr:parts.naddr,communityId:bundle.community_id,name:bundle.name||'Concord community',description:'',channels:[{name:'general',private:false}],local:false,cord:{bundle,parsed:opened.parsed}};
  }
  async function hydrateDiscoveredIcon(p,item){
    if(!item||item.icon||discoveryIconLoads.has(item.naddr)||!window.PosterCordReader||!p.relayQueryFrom)return;
    discoveryIconLoads.add(item.naddr);
    try{
      const signal=discoveryAbortController&&discoveryAbortController.signal,room=await hydrateInvite(p,item.url,signal);if(signal&&signal.aborted)return;const bundle=room.cord.bundle,reader=window.PosterCordReader;
      const seed=reader.inspectControl(bundle,[]),relays=roomRelays(bundle);
      const wraps=await cordQuery(p,relays,[{kinds:[1059],authors:seed.controlPubkeys,limit:1000}],{timeout:10000,max:8,signal,plane:cordPlaneContext(p,bundle,[],null)});
      const info=reader.inspectControl(bundle,wraps||[]);
      item.name=info.name||item.name; item.description=info.description||item.description;
      if(info.icon)item.icon=typeof info.icon==='string'?info.icon:await decryptImagePointer(info.icon);
      if(state.community==null)backgroundRender();
    }catch(_){} finally{ discoveryIconLoads.delete(item.naddr); }
  }
  function inviteRefUrl(ref){ const s=String(ref||''); if(/^https?:/i.test(s))return s; const [naddr,frag]=s.split('#'); return naddr&&frag?`https://armada.buzz/invite/${naddr}#${frag}`:''; }
  function normalizeRelay(value){try{const u=new URL(String(value||''));if(u.protocol!=='ws:'&&u.protocol!=='wss:')return '';return u.href.replace(/\/$/,'');}catch(_){return '';}}
  function nip29MembershipTags(value){
    const tags=Array.isArray(value)?value:[],groups=[],relays=[];
    for(const tag of tags){if(!Array.isArray(tag))continue;if(tag[0]==='group'){
      const id=String(tag[1]||'').trim(),relay=normalizeRelay(tag[2]);if(id&&relay)groups.push({id,relay,name:String(tag[3]||'').trim()});
    }else if(tag[0]==='r'){const relay=normalizeRelay(tag[1]);if(relay)relays.push(relay);}}
    return {groups:[...new Map(groups.map(g=>[g.relay+'\0'+g.id,g])).values()],relays:[...new Set(relays)]};
  }
  /* WHERE A MEMBERSHIP LIST IS LOOKED FOR, AND HOW OFTEN — one rule, used by both protocols.
   *
   * THE CHURN AND THE CORRECTNESS ARE DIFFERENT QUESTIONS, AND ANSWERING THEM TOGETHER BROKE
   * COMMUNITIES. Opening six external sockets in parallel with the pool read — on startup and then
   * again on every 60/120s recovery tick — is what filled a console with failed
   * jskitty/asia.vectorapp/Damus connections and made Concord slow to appear. The fix for that was
   * to skip the external read whenever the cache or the user's own relay answered at all, and that
   * is the same mistake this codebase keeps making in a different costume: "the local copy
   * returned SOMETHING" is not "the local copy is COMPLETE". Measured on this deployment, one
   * account holds a 13302 from 08-27 and a 33302 from 08-24 on its own relay and NO kind-10009 at
   * all — so an Armada vault newer than 08-27, or any NIP-29 list, lives only on the relays that
   * gate then refused to open. Reported as "my concord community also does not show anymore".
   *
   * So the external read is a UNION THAT HAPPENS ONCE, never a fallback a local hit cancels: the
   * first pass of a session reaches every compatibility relay (including the two historically
   * blocked ones, which is where these lists actually live) and every pass after it is local-only.
   * The recovery TIMER therefore costs nothing, which was the whole complaint; a fresh browser
   * still finds its rooms, which is the whole feature.
   *
   * THE MARK BEATS `legacyRecovery`, AND THAT IS NOT A DETAIL. Nothing here is person-triggered:
   * the "recovery" pass is what render() runs on an ordinary launch that already has a room open,
   * so letting it ignore the mark fires the union from INSIDE that room — and an active room may
   * only ever touch the relays in its own bundle (concord_runtime.mjs pins exactly that, and it
   * caught this). One bounded cross-relay recovery per session, whichever path asks for it first,
   * which is what the recovery pass was documented to be. `legacyRecovery` now only decides how
   * wide a single sweep reaches. */
  const _externalDone = new Set();
  let _externalViewer = '';
  function externalAllowed(pubkey, tag){
    /* Keyed per account anyway; the reset only bounds the set when somebody switches accounts on a
     * page that stays open. */
    if(_externalViewer !== pubkey){ _externalViewer = pubkey; _externalDone.clear(); }
    return !_externalDone.has(tag);
  }
  function externalDone(tag){ _externalDone.add(tag); }

  async function nip29Memberships(p,viewer,signal=null,legacyRecovery=false){
    if(!viewer.pubkey)return {groups:[],relays:[]};
    /* CACHE, THEN THE USER'S OWN POOL, AND ONLY THEN ANYBODY ELSE'S RELAY — the same rule
       `membershipEvents` was given, and this copy was left behind. Opening six external sockets in
       parallel with the pool read, on startup and then again on the 60/120s recovery timer, is what
       filled a Firefox console with "can't establish a connection to wss://jskitty.com/nostr",
       "…asia.vectorapp.io/nostr" and a cooling-down Damus while Concord sat there getting ready. A
       kind-10009 membership list is a REPLACEABLE document the user published: if the cache or
       their own relay has it, no external relay can improve on it, and if neither does then this is
       a fresh browser and the recovery read is worth its cost. The two historically blocked relays
       stay reserved for an explicit recovery pass. */
    const filters=[{kinds:[10009],authors:[viewer.pubkey],limit:8}];
    let cached=[];try{cached=window.Store&&window.Store.query?window.Store.query(filters)||[]:[];}catch(_){}
    const pool=p.relayQuery?await Promise.resolve(p.relayQuery(filters,8000)).catch(()=>[]):[];
    const local=[...(cached||[]),...(pool||[])].filter(e=>e&&e.id);
    const tag=viewer.pubkey+':10009',sweep=externalAllowed(viewer.pubkey,tag);
    const relays=[...CORD_RELAYS,...LEGACY_RECOVERY_RELAYS];
    let listed=[];
    if(sweep&&p.relayQueryFrom){
      listed=await Promise.resolve(p.relayQueryFrom(relays,filters,{timeout:8000,max:12,exact:true,signal,purpose:'concord nip29 memberships',minInterval:1800000,allowBlocked:true,failureCooldown:1800000})).catch(()=>[]);
      externalDone(tag);
    }
    const queried=[...new Map([...local,...(listed||[])].map(e=>[e.id,e])).values()],events=p.verifyRelayEvents?await p.verifyRelayEvents(queried):[],
      event=events.sort((a,b)=>Number(b.created_at)-Number(a.created_at)||String(a.id).localeCompare(String(b.id)))[0];
    if(!event)return {groups:[],relays:[]};
    const all=[...(event.tags||[])];
    const ciphertext=String(event.content||'').trim();
    if(ciphertext){const methods=/\?iv=/.test(ciphertext)?['nip04dec','nip44dec']:['nip44dec','nip04dec'];for(const method of methods){if(!p[method])continue;try{const privateTags=JSON.parse(await p[method](viewer.pubkey,ciphertext));if(Array.isArray(privateTags))all.push(...privateTags);break;}catch(_){}}}
    return nip29MembershipTags(all);
  }
  async function nip29RelayQuery(p,relay,filters,timeout=8000,signal=null){if(!p.relayQueryFrom||!p.verifyRelayEvents)throw new Error('verified listed relay queries are unavailable');const events=await p.relayQueryFrom([relay],filters,{timeout,max:1,exact:true,signal,purpose:'concord nip29 room',allowBlocked:true,failureCooldown:1800000})||[];return await p.verifyRelayEvents(events);}
  /* LEAVING A NIP-29 GROUP TAKES IT OFF THE ACCOUNT'S kind-10009, or every other device (and this one,
   * on a fresh browser) re-adds it from that list. Edits the NEWEST list it can read and removes only
   * this group's tag -- public and, when it decrypts, private; content it cannot decrypt is carried
   * through byte for byte. No list read means nothing is written: an empty read rewritten would drop
   * every other group (the replaceable-document wipe). */
  async function forgetNip29Group(p,viewer,room){
    if(!p.signTemplate||!p.relayPublishTo||!room||!room.groupId)return false;
    const filters=[{kinds:[10009],authors:[viewer.pubkey],limit:8}];
    let cached=[];try{cached=window.Store&&window.Store.query?window.Store.query(filters)||[]:[];}catch(_){}
    const pool=p.relayQuery?await Promise.resolve(p.relayQuery(filters,8000)).catch(()=>[]):[];
    const far=p.relayQueryFrom?await Promise.resolve(p.relayQueryFrom(CORD_RELAYS,filters,{timeout:8000,max:12,exact:true,purpose:'concord nip29 leave'})).catch(()=>[]):[];
    const all=[...new Map([...(cached||[]),...(pool||[]),...(far||[])].filter(e=>e&&e.id).map(e=>[e.id,e])).values()];
    const events=p.verifyRelayEvents?await p.verifyRelayEvents(all):[];
    const event=(events||[]).filter(e=>e.kind===10009&&e.pubkey===viewer.pubkey).sort((a,b)=>Number(b.created_at)-Number(a.created_at)||String(a.id).localeCompare(String(b.id)))[0];
    if(!event)return false;
    const relay=normalizeRelay(room.relay),hit=t=>Array.isArray(t)&&t[0]==='group'&&String(t[1]||'').trim()===room.groupId&&normalizeRelay(t[2])===relay;
    const tags=(event.tags||[]).filter(t=>!hit(t));
    let content=String(event.content||''),changed=tags.length!==(event.tags||[]).length;
    if(content.trim()&&!/\?iv=/.test(content)&&p.nip44dec&&p.nip44enc){
      try{const priv=JSON.parse(await p.nip44dec(viewer.pubkey,content));
        if(Array.isArray(priv)&&priv.some(hit)){content=await p.nip44enc(viewer.pubkey,JSON.stringify(priv.filter(t=>!hit(t))));changed=true;}}catch(_){}
    }
    if(!changed)return true;
    const now=Math.max(Math.floor(Date.now()/1000),Number(event.created_at)+1);
    const ev=await p.signTemplate({kind:10009,created_at:now,tags,content});
    return !!(await p.relayPublishTo(membershipWriteRelays(p,CORD_RELAYS),ev,{includeManaged:true,max:16}));
  }
  async function nip29Metadata(p,relay,groupIds=[],signal=null){
    const filter={kinds:[39000],limit:200};if(groupIds.length)filter['#d']=groupIds;
    const events=await nip29RelayQuery(p,relay,[filter],8000,signal),newest=new Map(),requested=new Set(groupIds);
    for(const event of events){if(Number(event.kind)!==39000)continue;const id=String(((event.tags||[]).find(t=>t[0]==='d')||[])[1]||'');if(!id||(requested.size&&!requested.has(id)))continue;const old=newest.get(id);if(!old||Number(event.created_at)>Number(old.created_at))newest.set(id,event);}
    return [...newest].map(([id,event])=>{let meta={};try{meta=JSON.parse(event.content||'{}');}catch(_){}const tags=Object.fromEntries((event.tags||[]).filter(t=>['name','about','picture'].includes(t[0])).map(t=>[t[0],t[1]]));return{id,relay,name:String(tags.name||meta.name||meta.display_name||id),description:String(tags.about||meta.about||meta.description||''),icon:normalizeIcon(tags.picture||meta.picture||meta.icon||''),source:event};});
  }
  async function syncNip29Memberships(p,viewer,allowActive=false){
    if((state.community!=null&&!allowActive)||nip29Busy||!viewer.pubkey)return;nip29Busy=true;let recovered=false;
    const signal=discoveryAbortController&&discoveryAbortController.signal;
    try{const membership=await nip29Memberships(p,viewer,signal,allowActive);if((signal&&signal.aborted)||(state.community!=null&&!allowActive))return;const byRelay=new Map();for(const g of membership.groups){if(!byRelay.has(g.relay))byRelay.set(g.relay,[]);byRelay.get(g.relay).push(g);}
      const found=[];for(const [relay,listed] of byRelay){if(state.community!=null&&!allowActive)return;let metas=[];try{metas=await nip29Metadata(p,relay,listed.map(g=>g.id),signal);}catch(_){}if(state.community!=null&&!allowActive)return;const metaById=new Map(metas.map(m=>[m.id,m]));for(const g of listed){const meta=metaById.get(g.id)||g;found.push({...meta,id:g.id,relay,name:g.name||meta.name||g.id});}}recovered=found.length>0;
      if(found.length){const rooms=saved(),adopted=[];let changed=false;for(const g of found){const identity='nip29:'+g.relay+'#'+g.id;/* A group LEFT on this device stays left even while the account's kind-10009 still lists it ("it goes away in communities, then comes back"): this pass runs every 60-120s. */if(wasLocallyLeft(viewer.pubkey,{communityId:identity,naddr:identity}))continue;const i=rooms.findIndex(r=>roomIdentity(r)===identity),room={protocol:'nip29',communityId:identity,naddr:identity,groupId:g.id,relay:g.relay,name:g.name||g.id,description:g.description||'',icon:g.icon||'',channels:[{name:'general',id:g.id,private:false}],local:false};if(i<0){/* this account's own kind-10009 names the group: a pre-separation record of it is this account's to keep */const legacy=adoptLegacyRoom(viewer.pubkey,{communityId:identity,naddr:identity}),merged=legacy?{...legacy,...room}:room;rooms.push(merged);if(legacy)adopted.push(merged);changed=true;}else if(rooms[i].protocol==='nip29'&&JSON.stringify(rooms[i])!==JSON.stringify({...rooms[i],...room})){rooms[i]={...rooms[i],...room};changed=true;}}if(changed){if(adopted.length)saveAdopted(viewer.pubkey,rooms,adopted);else save(rooms);backgroundRender();}}
    }catch(e){console.warn('NIP-29 membership sync failed',e);}finally{nip29Busy=false;clearTimeout(nip29RetryTimer);if(state.community==null)nip29RetryTimer=setTimeout(()=>syncNip29Memberships(p,p.viewer?p.viewer():viewer),recovered?60000:120000);}
  }
  function foldNip29History(events,p,groupId){const scoped=events.filter(e=>(e.tags||[]).some(t=>t[0]==='h'&&t[1]===groupId)).sort((a,b)=>Number(a.created_at)-Number(b.created_at)),deletions=[],deleted=new Set(),byId=new Map(),reactions=[];for(const e of scoped){if(e.kind===5){deletions.push(e);continue;}if(e.kind===7){reactions.push(e);continue;}if(![9,10,11,12,1111].includes(e.kind))continue;const pr=p.profOf?p.profOf(e.pubkey):{};byId.set(e.id,{id:e.id,pubkey:e.pubkey,by:pr.display_name||pr.name||e.pubkey.slice(0,12)+'…',text:e.content,at:Number(e.created_at)*1000,kind:e.kind,tags:e.tags||[],reactions:{},reactionIds:{},remote:true});}const reactionById=new Map(reactions.map(e=>[e.id,e]));for(const deletion of deletions)for(const t of deletion.tags||[])if(t[0]==='e'){const target=byId.get(t[1])||reactionById.get(t[1]);if(target&&target.pubkey===deletion.pubkey)deleted.add(t[1]);}for(const id of deleted)byId.delete(id);for(const e of reactions){if(deleted.has(e.id))continue;const target=((e.tags||[]).find(t=>t[0]==='e')||[])[1],m=byId.get(target);if(!m)continue;const emoji=e.content==='+'?'👍':e.content||'👍';(m.reactions[emoji]||(m.reactions[emoji]=[])).push(e.pubkey);(m.reactionIds[emoji]||(m.reactionIds[emoji]={}))[e.pubkey]=e.id;}for(const m of byId.values())if(m.kind===1111){const target=byId.get(((m.tags||[]).find(t=>t[0]==='e')||[])[1]);if(target)m.reply={id:target.id,by:target.by,text:target.text};}return [...byId.values()].sort((a,b)=>a.at-b.at);}
  async function nip29History(p,room){const events=await nip29RelayQuery(p,room.relay,[{kinds:[5,7,9,10,11,12,1111],'#h':[room.groupId],limit:500}],10000);return foldNip29History(events,p,room.groupId);}
  // Fold verified history and live events together; replacing a folded snapshot loses concurrent
  // arrivals, while merging folded rows would resurrect legitimately deleted messages.
  let nip29EventOwner=null,nip29SeedRestored=true;
  const nip29EventStreams=new Map();
  function mergeNip29Live(p,room,events){
    const owner=deliveryOwner(p),changedOwner=nip29EventOwner!==owner;
    if(changedOwner){if(nip29EventOwner!==null)nip29SeedRestored=false;nip29EventStreams.clear();nip29EventOwner=owner;}
    const key=roomIdentity(room);
    // A restored/handoff store may already contain verified rows before this renderer sees a live
    // event. Seed it once, but never carry the previous account's plaintext into a new owner.
    if(!nip29EventStreams.has(key)&&nip29SeedRestored){
      const seed=[];
      for(const m of testMessages(channelStoreId(room,'general')))if(m.remote&&(m.tags||[]).some(t=>t[0]==='h'&&t[1]===room.groupId)){
        seed.push({id:messageId(m),kind:m.kind,pubkey:m.pubkey,created_at:Number(m.at)/1000,content:m.text,tags:m.tags});
        for(const [emoji,people] of Object.entries(m.reactionIds||{}))for(const [pubkey,id] of Object.entries(people||{}))
          seed.push({id,kind:7,pubkey,created_at:Number(m.at)/1000,content:emoji,tags:[['h',room.groupId],['e',messageId(m)]]});
      }
      nip29EventStreams.set(key,seed);
    }
    const byId=new Map((nip29EventStreams.get(key)||[]).map(e=>[e.id,e]));
    for(const event of events||[])if(event&&event.id&&(event.tags||[]).some(t=>t[0]==='h'&&t[1]===room.groupId))byId.set(event.id,event);
    const merged=[...byId.values()].sort((a,b)=>Number(a.created_at)-Number(b.created_at)||String(a.id).localeCompare(String(b.id))).slice(-5000);
    nip29EventStreams.set(key,merged);
    const pending=testMessages(channelStoreId(room,'general')).filter(m=>!m.remote&&m.pubkey===owner&&!byId.has(messageId(m)));
    return mergeRelayMessages(pending,foldNip29History(merged,p,room.groupId));
  }
  async function readNip29Live(p,room,events=[]){
    const owner=deliveryOwner(p),identity=roomIdentity(room);
    mergeNip29Live(p,room,events);
    const history=await nip29RelayQuery(p,room.relay,[{kinds:[5,7,9,10,11,12,1111],'#h':[room.groupId],limit:500}],10000);
    if(deliveryOwner(p)!==owner||!saved().some(r=>roomIdentity(r)===identity))return null;
    return mergeNip29Live(p,room,history);
  }
  async function hydrateNip29Room(p,index){
    const room=saved()[index];if(!room||room.protocol!=='nip29')return;
    const messages=await readNip29Live(p,room);if(!messages)return;
    const storeId=channelStoreId(room,'general');markRemoteStore(storeId);saveTestMessages(storeId,messages);
    const latest=saved(),at=latest.findIndex(item=>sameRoom(item,room));if(at<0)return;
    latest[at]={...latest[at],nip29Hydrated:true};save(latest);if(sameRoom(latest[state.community],room))backgroundRender();
  }
  const membershipFragmentReads=new Map();
  /* A membership copy newer than anything this node's relay holds (written before membership writes reached it, or by
   * another client) is handed to it: the event is the owner's own signed copy, so this repairs, never invents. */
  function healHomeMembership(p,pubkey,local,remote){
    const home=p&&p.homeRelay?p.homeRelay():'';if(!home||!p.relayPublishTo)return;
    const coord=e=>e.kind+':'+(((e.tags||[]).find(t=>t[0]==='d')||[])[1]||''),best=new Map();
    for(const e of local||[])if(e&&e.pubkey===pubkey){const k=coord(e),o=best.get(k);if(!o||e.created_at>o.created_at)best.set(k,e);}
    for(const e of remote||[]){
      if(!e||e.pubkey!==pubkey||![13302,33302].includes(e.kind))continue;
      const o=best.get(coord(e));if(o&&o.created_at>=e.created_at)continue;
      best.set(coord(e),e);void Promise.resolve(p.relayPublishTo([home],e,{includeManaged:true})).catch(()=>{});
    }
  }
  async function membershipEvents(p,pubkey,{external=true,legacyRecovery=false,signal=null,fullFragments=false}={}){
    /* Match Armada's wire query exactly. A mixed [13302,33302] request looks harmless, but several
       relays close the WHOLE subscription when one kind is unsupported/blocked. That made a valid
       13302 vault look absent and a fresh browser showed no communities. 13302 is a legacy
       replaceable vault; current 33302 fragments are queried separately. */
    const query=async filter=>{
      let cached=[];try{cached=window.Store&&window.Store.query?window.Store.query([filter])||[]:[];}catch(_){}
      const pool=p.relayQuery?await Promise.resolve(p.relayQuery([filter],8000)).catch(()=>[]):[];
      /* Cache and the user's connected relay are authoritative enough for normal startup. Opening
         every Armada compatibility relay before painting a saved room made Concord slow and filled
         Firefox with failed Damus/Ditto/Vector sockets. External recovery is a fallback only. The
         two historically blocked relays are reserved for an explicit recovery action. */
      const local=[...(cached||[]),...(pool||[])].filter(e=>e&&e.id);
      const tag=pubkey+':'+String(filter.kinds&&filter.kinds[0])+(filter['#d']?':d:'+filter['#d'].join(','):'');
      const sweep=external&&externalAllowed(pubkey,tag);
      let remote=[];
      if(sweep&&p.relayQueryFrom){
        remote=await Promise.resolve(p.relayQueryFrom([...CORD_RELAYS,...LEGACY_RECOVERY_RELAYS],[filter],{timeout:8000,max:6,signal,purpose:'concord armada memberships '+String(filter.kinds&&filter.kinds[0])+(filter['#d']?':legacy':''),minInterval:1800000,allowBlocked:true,failureCooldown:1800000})).catch(()=>[]);
        externalDone(tag);
      }
      if(remote&&remote.length)healHomeMembership(p,pubkey,local,remote);
      return [...new Map([...(cached||[]),...(pool||[]),...(remote||[])].filter(e=>e&&e.id).map(e=>[e.id,e])).values()];
    };
    const released=await query({kinds:[13302],authors:[pubkey],limit:1});
    /* CORD-02 v2 is fragmented: Vector addresses fragment zero as d="0" (then 1, 2, ...),
       while the short-lived pre-fragment migration used d="". Ask the unrestricted kind as its
       own subscription so every coordinate is returned. Keeping the two precise queries matters
       for relays which implement tag filters but cap an unfiltered replaceable-kind result. */
    const [legacy,migrated]=await Promise.all([
      query({kinds:[33302],authors:[pubkey],'#d':[''],limit:20}),
      query({kinds:[33302],authors:[pubkey],limit:64}),
    ]);
    let recovered=membershipFragmentReads.get(pubkey);
    if(!recovered){recovered={heads:new Map(),cursor:0};membershipFragmentReads.set(pubkey,recovered);}
    let all=[...released,...legacy,...migrated,...recovered.heads.values()].filter(event=>event.pubkey===pubkey);
    if(p.verifyRelayEvents)all=await p.verifyRelayEvents(all);
    const retain=events=>{for(const event of events){const d=event.kind===33302&&(event.tags||[]).find(t=>t[0]==='d')?.[1];if(!/^(0|[1-9]\d*)$/.test(String(d)))continue;const index=Number(d);if(!Number.isSafeInteger(index))continue;const old=recovered.heads.get(index);if(!old||event.created_at>old.created_at||event.created_at===old.created_at&&event.id<old.id)recovered.heads.set(index,event);}};
    retain(all);
    let count=0,countAt=-1;
    if(p.nip44dec)for(const event of recovered.heads.values())try{const doc=cordJsonParse(await p.nip44dec(pubkey,event.content));if(Number.isSafeInteger(doc.frags)&&doc.frags>0&&(event.created_at>countAt||event.created_at===countAt&&doc.frags>count)){count=doc.frags;countAt=event.created_at;}}catch(_){}
    // A relay's initial page is not the whole list. Recover exact missing coordinates in
    // small batches and retain ciphertext heads across passes. A hole must not prevent
    // fetching later fragments. Foreground writes may read several batches; ordinary
    // startup does one, so an offline relay cannot block the UI on a huge declared count.
    const deadline=Date.now()+30000,budget=fullFragments?16:1;let walked=0;
    for(let pass=0;count&&pass<budget&&walked<count&&Date.now()<deadline&&!signal?.aborted;pass++){
      const missing=[];
      while(missing.length<16&&walked<count){const index=recovered.cursor%count;recovered.cursor=(index+1)%count;walked++;if(!recovered.heads.has(index))missing.push(String(index));}
      if(!missing.length)break;
      let found=await query({kinds:[33302],authors:[pubkey],'#d':missing,limit:missing.length});
      if(p.verifyRelayEvents)found=await p.verifyRelayEvents(found);
      found=found.filter(event=>event.pubkey===pubkey);retain(found);all.push(...found);
    }
    return [...new Map([...all,...recovered.heads.values()].map(e=>[e.id,e])).values()].sort((a,b)=>Number(b.created_at)-Number(a.created_at)||String(a.id).localeCompare(String(b.id)));
  }
  function cordListHex(value){
    const s=String(value||''); if(s.length!==43)return s;
    try{const raw=atob(s.replace(/-/g,'+').replace(/_/g,'/')+'=');return [...raw].map(c=>c.charCodeAt(0).toString(16).padStart(2,'0')).join('');}catch(_){return s;}
  }
  // Preserve integer tokens before JavaScript rounds them. Raw JSON values keep
  // opaque extension numbers numeric too; only named membership fields normalize.
  function cordJsonParse(text){
    // Compare decimal values without converting the source through Number. A
    // token such as 1.0000000000000000001 otherwise rounds to a 'safe' integer.
    const decimal=token=>{
      const m=/^(-?)(\d+)(?:\.(\d+))?(?:[eE]([+-]?\d+))?$/.exec(token);
      if(!m)return null;
      let digits=(m[2]+(m[3]||'')).replace(/^0+/,'');if(!digits)return '0';
      const trimmed=digits.replace(/0+$/,'');
      return m[1]+trimmed+'e'+String(BigInt(m[4]||0)-BigInt((m[3]||'').length)+BigInt(digits.length-trimmed.length));
    };
    return JSON.parse(text,(_key,value,context)=>{
      if(typeof value==='number'&&(!Number.isFinite(value)||Number.isInteger(value)&&!Number.isSafeInteger(value)||context?.source&&decimal(context.source)!==decimal(String(value)))){
        if(!context?.source||!JSON.rawJSON)throw new Error('lossless membership numbers require a newer browser');
        return JSON.rawJSON(context.source);
      }
      return value;
    });
  }
  function cordU64(value){
    const raw=JSON.isRawJSON?.(value)?value.rawJSON:value;
    if(typeof raw==='number'&&!Number.isSafeInteger(raw))throw new Error('unsafe membership integer');
    let text=String(raw??0);
    if(JSON.isRawJSON?.(value)){
      const match=/^(0|[1-9]\d*)(?:\.(\d+))?(?:[eE]([+-]?\d+))?$/.exec(text);
      if(!match)throw new Error('invalid membership u64');
      let digits=match[1]+(match[2]||''),shift=Number(match[3]||0)-(match[2]||'').length;
      if(!Number.isSafeInteger(shift)||shift>20||shift < -digits.length)throw new Error('invalid membership u64');
      if(shift<0){if(!/^0*$/.test(digits.slice(shift)))throw new Error('invalid membership u64');digits=digits.slice(0,shift);}
      else digits+='0'.repeat(shift);
      text=digits.replace(/^0+(?=.)/,'')||'0';
    }
    if(!/^(0|[1-9]\d*)$/.test(text)||BigInt(text)>18446744073709551615n)throw new Error('invalid membership u64');
    return BigInt(text);
  }
  function cordInteger(value){const n=cordU64(value);return n<=BigInt(Number.MAX_SAFE_INTEGER)?Number(n):String(n);}
  function cordIntegerMax(...values){return cordInteger(values.reduce((a,b)=>cordU64(a)>cordU64(b)?a:b,0));}
  function cordWireInteger(value){
    const n=cordU64(value);if(n<=BigInt(Number.MAX_SAFE_INTEGER))return Number(n);
    if(!JSON.rawJSON)throw new Error('lossless membership numbers require a newer browser');
    return JSON.rawJSON(String(n));
  }
  function cordListMaterial(material,communityId){
    const m=material&&typeof material==='object'?material:{};
    /* Fragment encoding applies to every 32-byte value at every depth. `held_roots` and a private
       channel's `priors` are deliberately open extension fields, but Vector uses them for history
       across root/channel rotations. Leaving those values as base64url lets the room appear while
       silently making its older control plane and messages unreadable. */
    const held=(m.held_roots||[]).map(root=>({...root,key:cordListHex(root&&root.key)}));
    const channels=(m.channels||[]).map(c=>({...c,epoch:cordInteger(c.epoch||0),id:cordListHex(c.id),key:c.key?cordListHex(c.key):c.key,
      priors:(c.priors||[]).map(prior=>({...prior,key:cordListHex(prior&&prior.key)}))}));
    return {...m,root_epoch:cordInteger(m.root_epoch||0),community_id:cordListHex(communityId),owner:cordListHex(m.owner),owner_salt:cordListHex(m.owner_salt),community_root:cordListHex(m.community_root),control_pk:m.control_pk?cordListHex(m.control_pk):m.control_pk,control_root:m.control_root?cordListHex(m.control_root):m.control_root,held_roots:held,channels};
  }
  /* Armada's membership `current` value is a control snapshot, while the invite-derived bundle
   * contains channel secrets and older roots needed to decrypt history.  Refreshing membership must
   * update newer scalar material without throwing away those join-only fields. */
  function mergeArmadaBundle(prior,current){
    const old=prior&&typeof prior==='object'?prior:{},fresh=current&&typeof current==='object'?current:{},next={...old};
    for(const [key,value] of Object.entries(fresh))if(value!==undefined&&value!==null&&value!=='')next[key]=value;
    next.channels=Array.isArray(fresh.channels)&&fresh.channels.length?fresh.channels:(Array.isArray(old.channels)?old.channels:[]);
    next.held_roots=Array.isArray(fresh.held_roots)&&fresh.held_roots.length?fresh.held_roots:(Array.isArray(old.held_roots)?old.held_roots:[]);
    next.relays=[...new Set([...(Array.isArray(fresh.relays)?fresh.relays:[]),...(Array.isArray(old.relays)?old.relays:[])])];
    return next;
  }
  /* SAME DATA, WHATEVER ORDER ITS KEYS ARE IN. A room rebuilt with spreads (`{...existing,...hydrated,…}`)
     serialises its keys in a different order from the stored copy, so a plain JSON compare called
     identical rooms different -- and every "changed" triggered a save and a full repaint. */
  function _canon(v){ return JSON.stringify(v,(k,x)=>x&&typeof x==='object'&&!Array.isArray(x)
    ? Object.keys(x).sort().reduce((o,kk)=>(o[kk]=x[kk],o),{}) : x); }
  function sameJson(a,b){try{return _canon(a)===_canon(b);}catch(_){return false;}}
  /* SAID ONCE, UNTIL IT WORKS.
   *
   * This re-toasted the same message for the same room every thirty seconds, for as long as the
   * condition lasted. On a phone the condition lasts — a community whose relays are slow or
   * unreachable is not a passing blip — so the screen filled with "could not refresh room history"
   * on a loop, over a room the client was already retrying every four seconds. Reported as exactly
   * that, over and over.
   *
   * A DIFFERENT message still speaks, and the notice is cleared on success (see the channel
   * handler), so a room that recovers and then breaks again says so again. What is gone is the
   * repetition of one unchanged fact nobody can act on. */
  function roomLoadWarning(p,key,prefix,error){
    const message=String(error&&error.message||error);
    /* DEDUPE ON THE FAILURE, NOT ON ITS WORDING. The most common message here is deliberately
     * diagnostic — "no channels readable yet - 4 relay(s) asked, 191 control event(s) held, 3 new"
     * - so its COUNTS change on every attempt. Compared whole, no two are ever equal, the guard
     * below never matches, and a condition that repeats every four seconds toasts every four
     * seconds. That is the wall of messages this was reported as. The numbers are what make the
     * line worth printing once, and exactly what makes it useless as an identity. */
    const shape=message.replace(/\d+/g,'#'),old=roomLoadNotices.get(key);
    if(old&&old.shape===shape)return;
    roomLoadNotices.set(key,{message,shape,at:Date.now()});
    p.toast(prefix+message);
  }
  function decodeMembershipLists(decrypted){
    /* Resolve addressable coordinates exactly as relays do, before unioning fragments. Without
       this, an older d=0 sibling can resurrect rooms that a newer fragment tombstoned. */
    const ordinary=[],coordinates=new Map();
    for(const row of decrypted){
      const doc=row.doc||{},d=((row.event.tags||[]).find(t=>t[0]==='d')||[])[1];
      if(row.event.kind!==33302||!/^(0|[1-9]\d*)$/.test(String(d))||!Number.isInteger(Number(doc.frags))){ordinary.push(doc);continue;}
      const old=coordinates.get(Number(d)),wins=!old||Number(row.event.created_at)>Number(old.event.created_at)||(Number(row.event.created_at)===Number(old.event.created_at)&&String(row.event.id)<String(old.event.id));
      if(wins)coordinates.set(Number(d),row);
    }
    let fragmentCount=0,fragmentTime=-1;
    for(const row of coordinates.values())if(row.event.created_at>fragmentTime||row.event.created_at===fragmentTime&&row.doc.frags>fragmentCount){fragmentTime=row.event.created_at;fragmentCount=row.doc.frags;}
    const docs=[...ordinary,...[...coordinates].filter(([index])=>index<fragmentCount).sort((a,b)=>a[0]-b[0]).map(([,x])=>x.doc)],entries=[],tombstones=[];
    for(const doc of docs){
      /* THE VAULT KEY IS NOT ALWAYS A COMMUNITY ID, AND IT MUST NEVER BE WRITTEN INTO THE BUNDLE.
       *
       * A room joined by a plain invite link has no community_id, so its vault entry is keyed on
       * `roomIdentity` -- the naddr. `cordListMaterial` injects whatever key it is handed as the
       * material's `community_id`, which for such an entry OVERWRITES the real 32-byte id inside
       * the join bundle with a naddr. The room then lists fine and every read of it throws
       * "invalid Concord join material" from inspectControl -- on the OTHER device, never on the
       * one that published it, because there the local row is kept as-is.
       *
       * So the key is used as material only when it IS one. Otherwise the bundle keeps its own id,
       * which is the value the reader has always needed. */
      for(const e of Array.isArray(doc.entries)?doc.entries:[]){const cid=cordListHex(e.community_id),source=e.current||e.seed;if(!source)continue;const real=/^[0-9a-f]{64}$/i.test(String(cid||''))?cid:null,seedSrc=e.seed||source;const current=cordListMaterial(source,real||(source||{}).community_id),seed=cordListMaterial(seedSrc,real||(seedSrc||{}).community_id);entries.push({...e,added_at:cordInteger(e.added_at||0),community_id:cid,current,seed});}
      for(const t of Array.isArray(doc.tombstones)?doc.tombstones:[])tombstones.push({...t,removed_at:cordInteger(t.removed_at||0),community_id:cordListHex(t.community_id)});
    }
    const merged=new Map();
    for(const entry of entries){
      const old=merged.get(entry.community_id);
      if(!old){merged.set(entry.community_id,entry);continue;}
      try{const wire=cordMergeEntry(old,entry),id=entry.community_id;merged.set(id,{...wire,community_id:id,current:cordListMaterial(wire.current,id),seed:cordListMaterial(wire.seed||wire.current,id)});}
      catch(_){if(cordU64(entry.added_at||0)>cordU64(old.added_at||0))merged.set(entry.community_id,entry);}
    }
    return {entries:[...merged.values()],tombstones};
  }
  async function syncArmadaMemberships(p,viewer,localOnly=false){
    if((state.community!=null&&!localOnly)||membershipBusy||!viewer.pubkey||!p.nip44dec)return; membershipBusy=true;
    let recovered=false,vaultEmpty=false;
    try{
      if(membershipViewer!==viewer.pubkey){membershipViewer=viewer.pubkey;membershipDocs.clear();}
      const activeRecovery=localOnly==='recovery',signal=localOnly&&!activeRecovery?null:discoveryAbortController&&discoveryAbortController.signal;
      const candidates=await membershipEvents(p,viewer.pubkey,{external:!localOnly||activeRecovery,legacyRecovery:activeRecovery,signal});
      vaultEmpty=!candidates.length;
      if(state.community!=null&&!localOnly)return;
      // Armada has emitted both kinds and may leave several list shards on relays. Decode every
      // valid snapshot: choosing one newest event can hide communities stored in another shard.
      const entries=new Map(),tombs=new Map(),decrypted=[];
      for(const event of candidates){
        try{
          let doc=membershipDocs.get(event.id);
          if(!doc){doc=cordJsonParse(await p.nip44dec(viewer.pubkey,event.content));membershipDocs.set(event.id,doc);while(membershipDocs.size>256)membershipDocs.delete(membershipDocs.keys().next().value);}
          decrypted.push({event,doc}); recovered=true;
        }catch(_){}
      }
      const list=decodeMembershipLists(decrypted),tombRefs=new Map();
      for(const t of list.tombstones){if(!t||!t.community_id)continue;tombs.set(t.community_id,cordIntegerMax(tombs.get(t.community_id)||0,t.removed_at||0));
        /* A tombstone written by this build names the invite it retired. Older ones (and Armada's)
         * name only the community id, which no announcement carries — the entry beside it is then
         * the only place the invite_ref survives, so both are merged below. */
        const ref=tombRefs.get(t.community_id)||{};tombRefs.set(t.community_id,{invite_ref:t.invite_ref||ref.invite_ref||'',naddr:t.naddr||ref.naddr||''});}
      for(const e of list.entries){
        if(!e||!e.community_id||!(e.current||e.seed))continue;
        /* The first released 13302 writers stored only `seed`; `current` was added for instant
           latest-epoch recovery. The protocol defines a missing current snapshot as the seed, and
           the CORD reader already accepts that material. Rejecting it here hid every such joined
           Vector room before validation or hydration had a chance to run. */
        const compatible=e.current?e:{...e,current:e.seed};
        const old=entries.get(e.community_id);if(!old||cordU64(e.added_at||0)>cordU64(old.added_at||0))entries.set(e.community_id,compatible);
      }
      /* A JOIN NEWER THAN THIS DEVICE'S LEAVE ANSWERS THE LEAVE -- no tombstone needed.
       *
       * Reported as "i updated apk but not in developers room yet": the room was in the vault,
       * its invite resolved, and the phone still hid it, because this ledger said "left" and the
       * only heal (below) waits for a tombstone to be VISIBLE. A rejoin made on another device
       * removes the tombstone from the fragment it writes, so on that path the heal can never
       * fire and the room stays hidden on this device for ever. The timestamps already say which
       * decision came last: a leave always outranks the join it retired (leaveArmadaMembership
       * stamps max(now, added_at)), so a stale relay replaying that old join still loses, while a
       * join made AFTER this device's leave wins. A row with no removedAt is not judged. */
      for(const e of entries.values()){
        if(cordU64(e.added_at||0)<=cordU64(tombs.get(e.community_id)||0))continue;
        const url=inviteRefUrl(e.invite_ref),ref={communityId:e.community_id,url,naddr:url?String((inviteParts(url)||{}).naddr||''):''},
              rows=leftCommunities(viewer.pubkey).filter(row=>leftMatches(row,ref));
        if(rows.length&&rows.every(row=>Number(row.removedAt)>0&&cordU64(e.added_at||0)>BigInt(Math.floor(Number(row.removedAt)))))
          forgetLeftCommunity(viewer.pubkey,ref);
      }
      const live=[...entries.values()].filter(e=>cordU64(e.added_at||0)>cordU64(tombs.get(e.community_id)||0)&&
        !wasLocallyLeft(viewer.pubkey,{communityId:e.community_id,url:inviteRefUrl(e.invite_ref)}));
      let rooms=saved(),changed=false;
      /* A tombstone must also REMOVE a room already rebuilt by cached membership or an old public
       * invite. The previous fold only declined to add it, leaving the stale local row untouched. */
      const dead=new Set([...tombs].filter(([id,at])=>cordU64(at)>=cordU64((entries.get(id)||{}).added_at||0)).map(([id])=>id));
      /* THE TOMBSTONE TEACHES THIS DEVICE, EVERY PASS. Reported as "on laptop, I loaded
       * Communities and it brought me back to Soapbox which I left many times": the vault said the
       * community was left and the laptop believed it — right up until discovery replayed the
       * OWNER'S OWN invite announcement, which `recoverOwnedInvite` rebuilt into a joined room
       * because the only thing that had ever refused it was a localStorage ledger belonging to the
       * device the Leave button was pressed on. The rebuilt room carries no `community_id` either,
       * so the `dead` filter below could never see it. Seeding the ledger from the winning
       * tombstones — with the invite reference, which is the key an announcement actually has —
       * closes both halves at once.
       *
       * ONLY THE WINNING ONES. A deliberate re-join publishes an entry newer than its tombstone
       * and is therefore not in `dead`; re-seeding from the stale tombstone beside it would refuse
       * the join the person had just made. */
      for(const id of dead){
        /* CORD-02 removes a retired entry from the fragment. Its explicit invite reference
         * still identifies the discovery announcement to suppress; a later entry outranking
         * the permanent tombstone clears this ledger below. Legacy unidentifiable tombstones
         * retain the cautious behavior until an entry supplies their invite identity. */
        if(!entries.has(id)&&!tombRefs.get(id)?.invite_ref&&!tombRefs.get(id)?.naddr) continue;
        const ref=tombRefs.get(id)||{},entry=entries.get(id)||{},
          url=inviteRefUrl(ref.invite_ref||entry.invite_ref||''),
          naddr=String(ref.naddr||'')||(url?String((inviteParts(url)||{}).naddr||''):'');
        noteLeftFromVault(viewer.pubkey,{communityId:id,naddr,url},Number(tombs.get(id))||0);
      }
      /* AND IT HEALS, BUT ONLY AGAINST A TOMBSTONE IT CAN SEE.
       *
       * A community whose entry BEATS its tombstone is one this account is in, so a local record
       * saying otherwise is stale -- from a re-join on another device, or from the over-eager
       * seeding this used to do. Clearing it brings a wrongly-hidden community back with nobody
       * having to find a setting.
       *
       * `tombs.has(id)` is what makes that safe, and it is not a formality: a relay that has not
       * yet received the tombstone answers with the old JOIN and nothing else, and healing on that
       * would undo a leave the person really made -- the empty-read wipe this codebase has paid for
       * more than once. No tombstone is "I cannot see one", never "there is none". The runtime
       * scenario in tests/client/concord_runtime.mjs drives exactly that stale relay. */
      for(const [id, entry] of entries){
        if(dead.has(id) || !tombs.has(id)) continue;
        const ref=tombRefs.get(id)||{},
          url=inviteRefUrl(ref.invite_ref||(entry&&entry.invite_ref)||''),
          naddr=String(ref.naddr||'')||(url?String((inviteParts(url)||{}).naddr||''):'');
        if(wasLocallyLeft(viewer.pubkey,{communityId:id,naddr,url}))
          forgetLeftCommunity(viewer.pubkey,{communityId:id,naddr,url});
      }
      /* MATCH THE WAY THE TOMBSTONE WAS KEYED. A leave writes its tombstone under `roomIdentity`,
       * so removing on `room.communityId` alone never fires for an invite-link room -- the room the
       * key change was made for. Leave it on the phone, and the laptop keeps showing it for ever
       * while the backfill correctly declines to re-add it, so nothing ever reconciles. Both forms
       * are checked because an older tombstone names only the community id. */
      const activeId=state.community==null?'':roomIdentity(rooms[state.community]),kept=rooms.filter(room=>!dead.has(room.communityId)&&!dead.has(roomIdentity(room))&&!wasLocallyLeft(viewer.pubkey,room));
      if(kept.length!==rooms.length){
        rooms=kept;changed=true;
        if(activeId){const at=rooms.findIndex(room=>roomIdentity(room)===activeId);state.community=at>=0?at:(rooms.length?0:null);state.channel=state.community==null?null:'general';}
        save(rooms);
        if(state.community==null)activeSet(null);else activeSet(String(state.community));
      }
      /* A ROOM THIS ACCOUNT'S VAULT NAMES, KEPT ON THIS DEVICE FROM BEFORE ACCOUNTS WERE SEPARATED,
       * IS HANDED OVER WHOLE -- its joined bundle, channels and settings, with no invite round trip.
       * Only from a DECODED vault (`live` is built from nothing else), so a vault that could not be
       * read adopts nothing and leaves the old list exactly as it was. Saved at once: the loop below
       * can return early, and an adoption that is marked but never saved would strand the room. */
      if(recovered){
        const added=[];
        for(const e of live){
          const url=inviteRefUrl(e.invite_ref),ref={communityId:e.community_id,url,naddr:url?String((inviteParts(url)||{}).naddr||''):''};
          if(rooms.some(r=>sameRoom(r,ref)))continue;
          const got=adoptLegacyRoom(viewer.pubkey,ref);if(got){rooms.push(got);added.push(got);}
        }
        if(saveAdopted(viewer.pubkey,rooms,added)){rooms=saved();changed=true;}
      }
      /* A ROOM THIS DEVICE IS IN AND THE VAULT HAS NEVER HEARD OF GETS WRITTEN INTO IT.
       *
       * `persistArmadaMembership` is called on create, on join and on discover -- so a room joined
       * before the vault existed, or by a path that skipped it, is known to ONE device for ever.
       * Measured on a real account: three joined rooms, one of them absent from the vault entirely,
       * and it was the one the person could see on their laptop and nowhere else.
       *
       * `recovered` is what makes this safe, and it is the only guard that could be. It means a
       * membership document was DECODED this pass -- so `entries` and `tombs` describe the account,
       * rather than being the empty pair a dead relay also produces. Republishing on that empty
       * pair would put back every community the person has ever left, which is the wipe this file
       * has paid for more than once. A tombstoned room is skipped too: `dead` is about to remove it
       * locally and re-adding it would be a resurrection loop between two devices.
       *
       * Only rooms with an invite `url` can go: that url carries the `#fragment` another device
       * needs to decrypt the room, and persist refuses without one. A purely local room stays local,
       * which is what it is. */
      if(recovered){
        const missing=rooms.filter(room=>{
          const rid=roomIdentity(room);
          return rid&&room.url&&!entries.has(rid)&&!tombs.has(rid)
                 &&!wasLocallyLeft(viewer.pubkey,room);
        });
        /* ONE write for all of them -- see persistArmadaMemberships. A room per call raced itself
         * and lost the first room every time. */
        if(missing.length) try{ await persistArmadaMemberships(p,missing); }
                           catch(_){ /* next pass tries again */ }
      }
      if(!live.length){if(changed)backgroundRender();return;}
      for(const e of live){
        if(state.community!=null&&!localOnly)return;
        const current=e.current||{},url=inviteRefUrl(e.invite_ref),
              i=rooms.findIndex(r=>sameRoom(r,{communityId:e.community_id,url}));
        // Armada's vault `current` is a CONTROL SNAPSHOT (owner/root/relays/name), not the complete
        // join bundle. Passing it to inspectControl rejects with "invalid Concord join material";
        // the catch used to `continue`, silently hiding every otherwise valid Armada membership on
        // a fresh device. Existing hydrated rooms need no work. For a missing room, resolve the
        // invite_ref exactly as Armada does and use the bundle carried by that invite.
        if(i>=0&&rooms[i].cord&&!rooms[i].cord.armadaList)continue;
        const existing=i>=0?rooms[i]:null,priorBundle=existing&&existing.cord&&existing.cord.bundle;
        let hydrated=null,bundle=mergeArmadaBundle(priorBundle,current);
        try{
          if(!window.PosterCordReader||!window.PosterCordReader.inspectControl)continue;
          window.PosterCordReader.inspectControl(bundle,[]);
        }catch(_){
          /* `localOnly` IS TRUE ON THE PASS THAT ACTUALLY RUNS. render() calls this with
           * 'recovery' whenever a room is already open — which is every ordinary launch for
           * somebody who has joined anything — and that pass is explicitly the one allowed to go
           * to the network (it asks for `external:true` a few lines up). Treating it as local-only
           * here skipped the invite hydration, so the repair above found the empty bundle,
           * correctly refused to skip the room, and then declined to fix it: measured after the
           * first attempt, `bundleChannels` was still 0. Only a genuinely local pass abstains. */
          if(!url||(localOnly&&!activeRecovery))continue;
          try{hydrated=await hydrateInvite(p,url);if(state.community!=null&&!localOnly)return;bundle=mergeArmadaBundle(hydrated.cord.bundle,current);}
          catch(__){continue;}
        }
        const channels=(bundle.channels||[]).map(c=>({name:c.name||'private',private:true,id:c.id}))
          .filter(c=>c.name!=='general');
        const derivedChannels=[{name:'general',private:false},...channels],bundleUnchanged=!!priorBundle&&sameJson(priorBundle,bundle);
        const room={...(existing||{}),...(hydrated||{}),communityId:e.community_id,
          name:current.name||(existing&&existing.name)||(hydrated&&hydrated.name)||'Concord community',
          description:(existing&&existing.description)||(hydrated&&hydrated.description)||'',
          channels:channels.length?derivedChannels:(existing&&existing.channels||derivedChannels),local:false,
          naddr:url?(inviteParts(url)||{}).naddr:(existing&&existing.naddr)||'community-'+e.community_id,
          url:url||(existing&&existing.url)||'',
          cord:{...(existing&&existing.cord||{}),...(hydrated&&hydrated.cord||{}),bundle,armadaList:true,
            hydrated:!!(existing&&existing.cord&&existing.cord.hydrated&&bundleUnchanged)}};
        if(i<0){rooms.push(room);changed=true;}
        else if((!rooms[i].cord||rooms[i].cord.armadaList)&&!sameJson(rooms[i],room)){rooms[i]=room;changed=true;}
      }
      if(changed){save(rooms);backgroundRender();}
    }catch(e){ console.warn('Concord membership sync failed',e); }
    finally{
      /* "A DOCUMENT WAS OPENED" IS THE ONLY HONEST LATCH. Candidates that decoded nothing means
       * "could not open", never "there is nothing there"; an empty candidate set means every
       * source — cache, own relay and, on the first pass, the compatibility relays — agreed this
       * account has published no vault, which is a real answer. Anything else leaves owned-invite
       * recovery held, and holding it only costs a community the invite link can restore. */
      if(viewer.pubkey&&(recovered||vaultEmpty)){membershipReadFor=viewer.pubkey;flushOwnedInvites(p);}
      membershipBusy=false;
      // Native desktop starts with an empty origin and relays may not be connected on first paint.
      // Retry a failed/empty recovery instead of permanently hiding the user's Armada rooms.
      clearTimeout(membershipRetryTimer); if(state.community==null)membershipRetryTimer=setTimeout(()=>syncArmadaMemberships(p,p.viewer?p.viewer():viewer),recovered?60000:120000);
    }
  }
  /* "CHECK MY COMMUNITIES" SAYS WHAT THIS DEVICE MEASURED, ROOM BY ROOM.
   *
   * "i updated phone ... I don't see the developers room on phone": the vault on the home relay held
   * the room, its invite resolved in 2.7s from a desktop, and the phone still showed nothing -- with
   * nothing on screen and nothing in any log, because every step of the membership pass fails into a
   * silent `continue` (a vault that would not decrypt, an invite that would not load, a local "left"
   * record). There is no device here to attach a debugger to, so the device reports: how many
   * membership documents it found, how many it could decrypt, and for each community in them whether
   * it is shown and, if not, which step stopped it. Read-only; the repair is a separate button. */
  async function checkMemberships(p){
    const viewer=p.viewer?p.viewer():{},pk=viewer.pubkey,lines=[],rows=[];
    if(!pk||!p.nip44dec)return {lines:['Sign in with a key that can decrypt to check your communities.'],rows};
    let events=[];
    try{events=await membershipEvents(p,pk,{external:true,legacyRecovery:true,fullFragments:true});}
    catch(e){lines.push('Could not read your membership list: '+String(e&&e.message||e).slice(0,160));legacyReportRows(pk,lines,rows);return {lines,rows};}
    const kinds=[...new Set(events.map(e=>e.kind===33302?'33302:'+(((e.tags||[]).find(t=>t[0]==='d')||[])[1]??''):String(e.kind)))];
    lines.push('Membership documents found: '+events.length+(kinds.length?' ('+kinds.join(', ')+')':''));
    const decrypted=[];let failed=0,firstErr='';
    for(const event of events){
      try{decrypted.push({event,doc:cordJsonParse(await p.nip44dec(pk,event.content))});}
      catch(e){failed++;if(!firstErr)firstErr=String(e&&e.message||e).slice(0,160);}
    }
    lines.push('Decrypted: '+decrypted.length+' of '+events.length+(failed?' -- failed: '+firstErr:''));
    const list=decodeMembershipLists(decrypted),tombs=new Map(),best=new Map();
    for(const t of list.tombstones)if(t&&t.community_id)tombs.set(t.community_id,cordIntegerMax(tombs.get(t.community_id)||0,t.removed_at||0));
    for(const e of list.entries){if(!e||!e.community_id)continue;const o=best.get(e.community_id);if(!o||cordU64(e.added_at||0)>cordU64(o.added_at||0))best.set(e.community_id,e);}
    const rooms=saved();
    for(const e of best.values()){
      const name=String((e.current&&e.current.name)||(e.seed&&e.seed.name)||'Community'),url=inviteRefUrl(e.invite_ref),
            ref={communityId:e.community_id,url,naddr:url?String((inviteParts(url)||{}).naddr||''):''};
      let status,fixable=false;
      if(cordU64(e.added_at||0)<=cordU64(tombs.get(e.community_id)||0))status='left (your account left it)';
      else if(rooms.some(r=>sameRoom(r,ref)))status='shown';
      else if(wasLocallyLeft(pk,ref)){const row=leftCommunities(pk).find(r=>leftMatches(r,ref))||{};fixable=true;
        status='hidden: this device recorded leaving it'+(row.removedAt?' on '+new Date(Number(row.removedAt)).toLocaleString():'');}
      else if(!url)status='missing: its membership entry has no invite link';
      else{fixable=true;try{await hydrateInvite(p,url);status='missing: its invite loads fine here';}
        catch(err){status='missing: its invite did not load -- '+String(err&&err.message||err).slice(0,120);}}
      rows.push({name,status,ref,fixable});
    }
    if(!best.size)lines.push('No communities in your membership list.');
    legacyReportRows(pk,lines,rows);
    return {lines,rows};
  }
  /* COMMUNITIES THIS DEVICE KEPT BEFORE ACCOUNTS WERE SEPARATED, WHICH NOTHING TIES TO THIS ACCOUNT.
   * Listed, never joined on anyone's behalf: the old list can hold several accounts' rooms, and only
   * the person knows which of the rest are theirs. Claiming one (`legacy` rows, their own button) is
   * that answer; the vault backfill then publishes it to this account's membership list. */
  function legacyReportRows(pk,lines,rows){
    const rest=unclaimedLegacyRooms(pk);if(!rest.length)return;
    lines.push('Saved on this device before accounts were kept apart, not linked to this account: '+rest.length);
    for(const room of rest)rows.push({name:String(room.name||'Community'),status:'on this device, not linked to this account',
      ref:{communityId:room.communityId||'',naddr:room.naddr||'',url:room.url||''},fixable:false,legacy:true});
  }
  /* The repair is the ordinary recovery pass, after forgetting this device's "left" record for the
   * rooms a person just chose to restore -- choosing to restore IS the decision that record stands in
   * for. Everything else (tombstones, the account's own leaves) is untouched. */
  async function restoreMemberships(p,rows){
    const viewer=p.viewer?p.viewer():{};if(!viewer.pubkey)return;
    for(const r of rows||[])if(r.fixable)forgetLeftCommunity(viewer.pubkey,r.ref);
    for(let i=0;membershipBusy&&i<40;i++)await new Promise(res=>setTimeout(res,250));
    await syncArmadaMemberships(p,viewer,'recovery');
  }
  function membershipReportText(report){
    return [...report.lines,...report.rows.map(r=>'- '+r.name+': '+r.status)].join('\n');
  }
  async function showMembershipCheck(p){
    p.modal('<h3>Check my communities</h3><p class="muted" id="cc-mcheck-status">Reading your membership list…</p><pre id="cc-mcheck-out" class="cc-mcheck-out"></pre><div class="cc-primary-actions"><button class="btn btn-neon" id="cc-mcheck-fix" hidden>Restore missing communities</button><button class="btn btn-ghost" id="cc-mcheck-copy" hidden>Copy report</button></div><div class="cc-primary-actions" id="cc-mcheck-legacy"></div>',async root=>{
      const status=root.querySelector('#cc-mcheck-status'),out=root.querySelector('#cc-mcheck-out'),
            fix=root.querySelector('#cc-mcheck-fix'),copy=root.querySelector('#cc-mcheck-copy');
      const run=async()=>{
        let report;try{report=await checkMemberships(p);}catch(e){report={lines:['Check failed: '+String(e&&e.message||e).slice(0,160)],rows:[]};}
        out.textContent=membershipReportText(report);status.textContent='';
        copy.hidden=false;copy.onclick=()=>p.copyValue(out.textContent);
        const legacy=root.querySelector('#cc-mcheck-legacy'),claims=report.rows.filter(r=>r.legacy);
        if(legacy){legacy.innerHTML=claims.map((r,i)=>`<button class="btn btn-ghost" data-cc-claim="${i}">Add ${p.enc(r.name)} to this account</button>`).join('');
          legacy.querySelectorAll('[data-cc-claim]').forEach(b=>b.onclick=async()=>{const row=claims[Number(b.dataset.ccClaim)],pk=accountPk();if(!row||!pk)return;b.disabled=true;
            const added=claimLegacyRooms(pk,[row.ref]);p.toast(added?row.name+' added to this account':'could not add '+row.name);backgroundRender();await run();});}
        const broken=report.rows.filter(r=>r.fixable);fix.hidden=!broken.length;
        fix.onclick=async()=>{fix.disabled=true;status.textContent='Restoring…';
          try{await restoreMemberships(p,broken);backgroundRender();status.textContent='Checking again…';await run();}
          catch(e){status.textContent='Restore failed: '+String(e&&e.message||e).slice(0,160);}
          finally{fix.disabled=false;}};
      };
      await run();
    });
  }
  /* A COMMUNITY THIS ACCOUNT IS IN BELONGS IN THE VAULT, WHETHER OR NOT IT HAS A community_id.
   *
   * This refused any room without one, silently -- so such a room lived in ONE device's
   * localStorage and no other device could ever learn about it. Reported as "my room for posterchan
   * is still not appearing", then narrowed to "I see posterchan on laptop in concord but that is
   * the only place". Measured on that laptop: three joined rooms, and the one that was missing
   * everywhere else was the only one with `communityId: (none)` and `cord.armadaList: false` -- a
   * room joined through a plain invite link rather than rebuilt from the vault. Its two neighbours
   * carried hex ids, were in the vault, and appeared on every device.
   *
   * `roomIdentity` is this file's own notion of what a room IS -- community id, else naddr, else
   * url -- and it is what every other identity comparison here already uses (removeCommunityByIdentity,
   * hydrateRoomStreams, the active-room restore). Using it as the vault key is therefore not a new
   * concept, it is the existing one applied to the one place that had its own rule.
   *
   * Safe against the reader: `cordListHex` converts only 43-character base64url keys and passes
   * everything else through untouched, so a naddr survives decode unchanged, and `decodeMembershipLists`
   * asks only that `community_id` be non-empty. `invite_ref` still carries the FULL url including its
   * `#fragment`, which is the decryption key -- without it another device can list the room and never
   * open it. */
  // CORD-02 §8: canonical, self-encrypted addressable membership fragments.
  /* WHERE A MEMBERSHIP LIST IS WRITTEN: this instance's own relay FIRST, then the public and CORD relays.
   * It went to `relayUrls()` + CORD only -- and relayUrls() is the PUBLIC write set, never the home relay -- while
   * every device READS its own relay on every pass and the outside relays once per session. So whichever device
   * joined a room wrote a list the others never read: "concord on desktop I am in a developer room but phone
   * don't show room" (2026-10-08: the 10-08 vault was on nos.lol/primal/jskitty, poster.place held the 09-10 one). */
  function membershipWriteRelays(p, extra){
    const home = p && p.homeRelay ? p.homeRelay() : '';
    return [...new Set([...(home ? [home] : []), ...(p && p.relayUrls ? p.relayUrls() : []), ...(extra || [])])];
  }
  const membershipWrites=new Map(),membershipPublished=new Map();
  function cordCanonical(value){
    if(JSON.isRawJSON?.(value))return value.rawJSON;
    if(Array.isArray(value))return '['+value.map(cordCanonical).join(',')+']';
    if(value&&typeof value==='object')return '{'+Object.keys(value).filter(k=>value[k]!==undefined).sort().map(k=>JSON.stringify(k)+':'+cordCanonical(value[k])).join(',')+'}';
    return JSON.stringify(value);
  }
  function cordCanonicalCompare(a,b){
    const encoder=new TextEncoder(),x=encoder.encode(cordCanonical(a)),y=encoder.encode(cordCanonical(b));
    for(let i=0;i<Math.min(x.length,y.length);i++)if(x[i]!==y[i])return x[i]-y[i];
    return x.length-y.length;
  }
  /* The 32-byte community id in wire form, or '' when `value` is not one. A room that has never had
   * its Concord bundle opened -- or a NIP-29 group -- is known by its naddr/identity string, which is
   * not a 32-byte key; asking cordListB64 about it threw "membership contains an invalid 32-byte key"
   * and Leave stopped dead ("could not leave concord community ... something about a 32 bit key"). */
  function cordListIdOrEmpty(value){try{return value?cordListB64(value):'';}catch(_){return '';}}
  function cordListB64(value){
    const hex=cordListHex(value);
    if(!/^[0-9a-f]{64}$/i.test(hex))throw new Error('membership contains an invalid 32-byte key');
    return btoa(hex.match(/../g).map(b=>String.fromCharCode(parseInt(b,16))).join('')).replace(/\+/g,'-').replace(/\//g,'_').replace(/=+$/,'');
  }
  function cordWireMaterial(source){
    const result={...source};
    for(const key of ['community_id','icon','expires_at','creator_npub','label','invite_ref'])delete result[key];
    for(const key of ['owner','owner_salt','community_root','control_pk','control_root'])if(result[key]!==undefined)result[key]=cordListB64(result[key]);
    result.root_epoch=cordWireInteger(result.root_epoch||0);
    result.channels=(result.channels||[]).map(ch=>{const row={...ch,epoch:cordWireInteger(ch.epoch||0)};for(const key of ['id','key'])if(row[key]!==undefined)row[key]=cordListB64(row[key]);return row;});
    return result;
  }
  function cordWireEntry(entry){
    const current=cordWireMaterial(entry.current||entry.seed),seed=cordWireMaterial(entry.seed||entry.current),out={...entry,community_id:cordListB64(entry.community_id),current,added_at:cordInteger(entry.added_at||0)};
    seed.name=current.name;seed.relays=current.relays;
    const names=new Map(current.channels.map(ch=>[ch.id,ch.name]));
    seed.channels=seed.channels.map(ch=>({...ch,...(names.has(ch.id)?{name:names.get(ch.id)}:{})}));
    if(cordCanonical(seed)===cordCanonical(current))delete out.seed;else out.seed=seed;
    return out;
  }
  function cordMergeOpaque(a,b){
    const out=Object.assign(Object.create(null),a);for(const [key,value]of Object.entries(b||{}))if(out[key]===undefined||cordCanonicalCompare(value,out[key])<0)out[key]=value;return out;
  }
  function cordMergeEntry(a,b){
    if(!a)return cordWireEntry(b);
    a=cordWireEntry(a);b=cordWireEntry(b);
    const choose=(x,y,low)=>{const xe=cordU64(x.root_epoch||0),ye=cordU64(y.root_epoch||0);const winner=xe!==ye?((low?xe<ye:xe>ye)?x:y):(cordCanonicalCompare(x,y)<=0?x:y),other=winner===x?y:x;return {...other,...winner};};
    return cordWireEntry({...cordMergeOpaque(a,b),community_id:a.community_id,added_at:cordIntegerMax(a.added_at||0,b.added_at||0),
      current:choose(a.current,b.current,false),seed:choose(a.seed||a.current,b.seed||b.current,true)});
  }
  function cordMergeLists(docs){
    const entries=new Map(),tombs=new Map(),extras=Object.create(null);
    for(const doc of docs){
      for(const [key,value]of Object.entries(doc||{}))if(!['entries','tombstones','frags'].includes(key)&&extras[key]===undefined)extras[key]=value;
      for(const e of doc.entries||[]){
        if(!e||!(e.current||e.seed))continue;
        // Migrate the old invite-address vault key to the actual community commitment.
        // A row with no usable 32-byte id is skipped, never thrown on: one such row used to make the
        // WHOLE merge throw, so no community in the list could be read, joined or left.
        const cid=cordListIdOrEmpty(/^[0-9a-f]{64}$/i.test(cordListHex(e.community_id))?e.community_id:(e.current||e.seed).community_id);
        if(!cid){ try{console.warn('Concord membership: skipped an entry with no 32-byte community id');}catch(_){} continue; }
        entries.set(cid,cordMergeEntry(entries.get(cid),{...e,community_id:cid}));
      }
      for(const t of doc.tombstones||[]){const cid=cordListIdOrEmpty(t.community_id);if(!cid)continue;const old=tombs.get(cid);tombs.set(cid,{...cordMergeOpaque(old,t),community_id:cid,removed_at:cordIntegerMax(old?.removed_at||0,t.removed_at||0)});}
    }
    for(const [cid,e]of entries)if(cordU64(e.added_at||0)<=cordU64(tombs.get(cid)?.removed_at||0))entries.delete(cid);
    return {...extras,entries:[...entries.values()].sort((a,b)=>(a.community_id<b.community_id?-1:a.community_id>b.community_id?1:0)),tombstones:[...tombs.values()].sort((a,b)=>(a.community_id<b.community_id?-1:a.community_id>b.community_id?1:0))};
  }
  async function cordMembershipState(p,owner){
    const candidates=[...await membershipEvents(p,owner,{fullFragments:true}),...(membershipPublished.get(owner)||[])],coordinates=new Map(),legacy=[];
    for(const event of candidates){
      const d=(event.tags||[]).find(t=>t[0]==='d')?.[1];
      if(event.kind===33302&&/^(0|[1-9]\d*)$/.test(String(d))){const index=Number(d),old=coordinates.get(index);if(!Number.isSafeInteger(index))continue;if(!old||event.created_at>old.created_at||event.created_at===old.created_at&&event.id<old.id)coordinates.set(index,event);}
      else if(event.kind===13302||event.kind===33302)legacy.push(event);
    }
    const fragments=new Map();let count=coordinates.size?1:0,countAt=-1;
    for(const [index,event]of coordinates){
      const doc=cordJsonParse(await p.nip44dec(owner,event.content));
      if(!Number.isSafeInteger(doc.frags)||doc.frags<1)throw new Error('invalid membership fragment count');
      if(event.created_at>countAt||event.created_at===countAt&&doc.frags>count){count=doc.frags;countAt=event.created_at;}
      fragments.set(index,{event,doc});
    }
    // Read migration material too: an old client may still have a membership only there.
    const legacyDocs=[];for(const event of legacy)legacyDocs.push(cordJsonParse(await p.nip44dec(owner,event.content)));
    const complete=fragments.size===0||[...fragments.keys()].filter(index=>index<count).length===count;
    const ordered=[...fragments].sort((a,b)=>a[0]-b[0]);
    const merged=cordMergeLists([...ordered.filter(([i])=>i<count).map(([,row])=>row.doc),...legacyDocs]);
    return {fragments,count:count||1,complete,merged};
  }
  async function cordWriteMembership(p,change){
    const owner=p.viewer?.().pubkey;if(!owner||!p.nip44enc||!p.nip44dec||!p.signTemplate||!p.relayPublishTo)throw new Error('membership signing is unavailable');
    const prior=membershipWrites.get(owner)||Promise.resolve();
    const job=prior.catch(()=>{}).then(async()=>{
      const live=()=>{if(p.viewer?.().pubkey!==owner)throw new Error('account changed during membership update');};live();
      const state=await cordMembershipState(p,owner);live();
      const before=state.merged,desired=change(before),changed=new Set(desired.changed),all=cordMergeLists([desired.list]);
      // cordListIdOrEmpty, not cordListB64: ONE malformed entry anywhere in the stored list used to throw
      // here and block every join and leave for the account.
      const indexOf=cid=>{for(const [index,row]of state.fragments)if([...(row.doc.entries||[]),...(row.doc.tombstones||[])].some(e=>cordListIdOrEmpty(e.community_id)===cid))return index;return 0;};
      const touched=new Set([...changed].map(indexOf)),docs=new Map();
      if(!state.fragments.size)touched.add(0);
      for(const index of touched){
        if(!state.complete&&!state.fragments.has(index))throw new Error('load this membership fragment before updating it');
        const original=state.fragments.get(index)?.doc||{entries:[],tombstones:[]};
        const own=cordMergeLists([original]);
        for(const cid of changed){
          if(indexOf(cid)!==index)continue;
          own.entries=own.entries.filter(e=>e.community_id!==cid);own.tombstones=own.tombstones.filter(e=>e.community_id!==cid);
          own.entries.push(...all.entries.filter(e=>e.community_id===cid));own.tombstones.push(...all.tombstones.filter(e=>e.community_id===cid));
        }
        docs.set(index,state.fragments.size?own:all);
      }
      // Migration adds legacy-only memberships only with a complete list. Unknown fragment
      // fields move intact to zero; partial targeted writes preserve their own source fields.
      if(state.complete){
        const known=new Set([...state.fragments.values()].flatMap(row=>[...(row.doc.entries||[]),...(row.doc.tombstones||[])].map(e=>cordListIdOrEmpty(e.community_id)||String(e.community_id||''))));
        const extraEntries=all.entries.filter(e=>!known.has(e.community_id)&&!changed.has(e.community_id)),extraTombs=all.tombstones.filter(e=>!known.has(e.community_id)&&!changed.has(e.community_id));
        if(extraEntries.length||extraTombs.length){const zero=docs.get(0)||cordMergeLists([state.fragments.get(0)?.doc||{entries:[],tombstones:[]}]);zero.entries.push(...extraEntries);zero.tombstones.push(...extraTombs);docs.set(0,zero);}
      }
      if(state.complete){
        const extras={...before};delete extras.entries;delete extras.tombstones;delete extras.frags;
        const move=[...state.fragments].filter(([index,row])=>index!==0&&Object.keys(row.doc).some(key=>!['entries','tombstones','frags'].includes(key)));
        if(move.length){
          const zero=docs.get(0)||cordMergeLists([state.fragments.get(0)?.doc||{entries:[],tombstones:[]}]);docs.set(0,{...extras,...zero,...extras});
          for(const [index,row]of move){const doc=docs.get(index)||cordMergeLists([row.doc]);docs.set(index,{entries:doc.entries,tombstones:doc.tombstones});}
        }
      }
      let count=state.count;
      const stamp=index=>Math.max(Math.floor(Date.now()/1000),Number(state.fragments.get(index)?.event.created_at||0)+1);
      const seal=async(index,doc,frags)=>{live();const content=await p.nip44enc(owner,cordCanonical({...doc,entries:(doc.entries||[]).map(e=>({...e,added_at:cordWireInteger(e.added_at||0)})),tombstones:(doc.tombstones||[]).map(t=>({...t,removed_at:cordWireInteger(t.removed_at||0)})),frags}));live();const ev=await p.signTemplate({kind:33302,pubkey:owner,created_at:stamp(index),tags:[['d',String(index)]],content});live();return ev;};
      // Size the actual encrypted+signed wire event. Keep old fragment items in place and
      // append overflow, so publishing new fragments first cannot strand keys on interruption.
      const max=65536,wireSize=ev=>new TextEncoder().encode(JSON.stringify(ev)).length,shrinking=new Map();
      for(const [index,doc]of [...docs]){
        // A stranded oversized fragment must still permit a scoped leave/repair.
        // Compare fully encrypted and signed bytes, including NIP-44 padding.
        const previous=state.fragments.get(index)?.event;
        if(previous&&wireSize(previous)>max){
          const candidate=await seal(index,doc,count);
          if(wireSize(candidate)<wireSize(previous)){shrinking.set(index,candidate);continue;}
        }
        let pending=[...(doc.entries||[]).map(value=>({type:'entries',value})),...(doc.tombstones||[]).map(value=>({type:'tombstones',value}))];
        const extras={...doc};delete extras.entries;delete extras.tombstones;delete extras.frags;
        const chunks=[];let chunk={...extras,entries:[],tombstones:[]};
        for(const item of pending){
          chunk[item.type].push(item.value);
          // A conservative plaintext screen avoids repeatedly asking hardware signers to sign.
          if(new TextEncoder().encode(cordCanonical(chunk)).length>39000&&chunk.entries.length+chunk.tombstones.length>1){chunk[item.type].pop();chunks.push(chunk);chunk={entries:[],tombstones:[],[item.type]:[item.value]};}
        }chunks.push(chunk);
        for(let n=0;n<chunks.length;n++){
          if(n&&!state.complete)throw new Error('load all membership fragments before splitting this list');
          const dest=n?count++:index;docs.set(dest,chunks[n]);
        }
      }
      if(count!==state.count){
        if(!state.complete)throw new Error('load all membership fragments before repacking');
        for(const [index,row]of state.fragments)if(index<state.count&&!docs.has(index))docs.set(index,cordMergeLists([row.doc]));
      }
      let events=[];
      for(const [index,doc]of docs){
        const ev=(count===state.count&&shrinking.get(index))||await seal(index,doc,count),size=wireSize(ev);
        const previous=state.fragments.get(index)?.event,strictShrink=previous&&size<wireSize(previous);
        if(size>max&&!strictShrink)throw new Error('one membership or extension exceeds the event size limit; its keys were preserved');
        events.push({index,ev,size});
      }
      // Re-read before publishing to catch another local/background sync observing a newer
      // coordinate while the signer was open. Never overwrite that freshly observed copy.
      const latest=await cordMembershipState(p,owner);live();
      for(const {index}of events){const seen=latest.fragments.get(index)?.event,was=state.fragments.get(index)?.event;if(seen&&(!was||seen.created_at>was.created_at||seen.created_at===was.created_at&&seen.id<was.id))throw new Error('membership changed while signing; retry this update');}
      events.sort((a,b)=>(a.index>=state.count?0:1)-(b.index>=state.count?0:1)||(a.index>=state.count?b.index-a.index:a.index-b.index));
      /* THIS NODE'S RELAY GETS EVERY MEMBERSHIP WRITE, AND MUST TAKE IT. Relay.publishTo skips a relay already in the
       * managed pool unless told otherwise -- and the instance relay is ALWAYS in the pool -- so every join and leave
       * went to the outside relays only, "accepted" by any one of them. A fresh session reads this node first and got
       * a weeks-old list: "I left communities on my phone, I just logged in Firefox and I am in those communities"
       * (2026-10-16; the node held the 10-08 fragment, the 14:10 leave existed only outside). */
      const home=p.homeRelay?p.homeRelay():'';
      for(const {ev}of events){live();
        if(home){const own=await p.relayPublishTo([home],ev,{includeManaged:true,detailed:true});live();if(!(own&&own.ok))throw new Error('this server did not take the membership update: '+String(own&&own.msg||'no answer'));}
        const accepted=home?1:await p.relayPublishTo(membershipWriteRelays(p,CORD_RELAYS),ev,{includeManaged:true,max:16});live();
        if(home)void Promise.resolve(p.relayPublishTo(membershipWriteRelays(p,CORD_RELAYS).filter(u=>normalizeRelay(u)!==normalizeRelay(home)),ev,{includeManaged:true,max:16})).catch(()=>{});
        if(!(accepted===true||typeof accepted==='number'&&accepted>0||accepted?.ok===true||Number(accepted?.accepted)>0))throw new Error('membership relays rejected the update');
        const held=membershipPublished.get(owner)||[];membershipPublished.set(owner,[...held.filter(e=>(e.tags||[]).find(t=>t[0]==='d')?.[1]!==ev.tags[0][1]),ev]);}
      return true;
    });membershipWrites.set(owner,job);return job;
  }
  async function persistArmadaMembership(p,room){ return persistArmadaMemberships(p,[room]); }
  /* THE INSTANCE'S COMMUNITY (Admin -> Nostr Relay -> "Community every member joins"). A new account made
   * here used to land in Concord with nothing, and whatever room it reached it reached by a stale link
   * ("Posterchan Community Not Loading", 2026-10-21). Members ask this node for the invite (never in the
   * public config: it carries the # secret) and join it ONCE per account: the invite handled is
   * remembered, so leaving the community is final, and a changed invite is offered once more. The join is
   * the ordinary one -- saved, recorded in the Armada membership vault, announced in the guestbook -- and
   * only opens the room when nothing else is open. Every failure is silent and retried next session. */
  const INSTANCE_JOIN_KEY=pk=>'pc.concord.autojoin.v1.'+pk;
  let instanceJoinBusy=false,instanceJoinSession='';
  async function joinInstanceCommunity(p,hydrate=hydrateInvite){
    const pk=accountPk();if(!pk||instanceJoinBusy||instanceJoinSession===pk)return;
    if(typeof window.__PC_API_BASE__==='string'?!window.__PC_API_BASE__:!(typeof location!=='undefined'&&/^https?:$/.test(location.protocol)))return;   // no instance to ask
    instanceJoinBusy=true;instanceJoinSession=pk;
    try{
      const r=await fetch('/client/concord/default-invite',{credentials:'include',cache:'no-store'});if(!r.ok)return;
      const url=String((await r.json()).invite||'').trim();if(!url||!inviteParts(url))return;
      let done='';try{done=localStorage.getItem(INSTANCE_JOIN_KEY(pk))||'';}catch(_){}
      if(done===url||accountPk()!==pk)return;
      const room=await hydrate(p,url);if(accountPk()!==pk)return;
      const mark=()=>{try{localStorage.setItem(INSTANCE_JOIN_KEY(pk),url);}catch(_){}};
      if(saved().some(x=>sameRoom(x,room))||wasLocallyLeft(pk,room)){mark();return;}
      if(room.cord?.bundle&&window.PosterCordReader?.validateInviteBundle)window.PosterCordReader.validateInviteBundle(room.cord.bundle,{forJoin:true});
      const a=saved();a.push(room);save(a);mark();
      if(state.community==null&&!discoveryOpen){state.community=a.findIndex(x=>sameRoom(x,room));state.channel='general';activeSet(String(state.community));}
      await persistArmadaMembership(p,room);
      void publishGuestbook(p,room,'join');
      render();
    }catch(_){instanceJoinSession='';}   // could not ask / could not read: silent, tried again next session
    finally{instanceJoinBusy=false;}
  }
  /* EVERY ROOM IN ONE WRITE, BECAUSE 13302 IS REPLACEABLE AND EACH WRITE IS A READ-MODIFY-WRITE.
   *
   * Called once per room, this reads the prior document, adds one entry and publishes the WHOLE
   * list back. Called in a loop it therefore races itself: the second call's read can be answered
   * before the first call's publish is queryable, and the document it then writes is the old one
   * plus its own entry -- silently dropping the entry before it.
   *
   * Measured, not reasoned about. The backfill ran over three rooms and the vault afterwards held
   * the LAST TWO, written 18:20:09 and 18:20:17, with the first one missing entirely: "still don't
   * see my concord community for posterchan on webui". This is the same replaceable-document hazard
   * Budget serialises with `chain`, and awaiting each call is not enough -- the await orders the
   * PUBLISHES, and it is the READS that overlap them.
   *
   * So the list is the unit of work. One read, every entry, one publish. */
  async function persistArmadaMemberships(p,rooms){
    const viewer=p.viewer?p.viewer():{};
    if(!viewer.pubkey||!p.nip44enc)return false;
    const wanted=(Array.isArray(rooms)?rooms:[rooms])
      .filter(room=>room&&roomIdentity(room)&&(room.url||room.cord?.bundle?.community_id));
    if(!wanted.length)return false;
    await cordWriteMembership(p,list=>{
      const now=Date.now(),changed=[];
      for(const room of wanted){
        const bundle=room.cord&&room.cord.bundle||{},cid=cordListIdOrEmpty(bundle.community_id)||cordListIdOrEmpty(room.communityId);
        if(!cid)continue;          // no 32-byte id: nothing the vault can key this room on
        const held=window.PosterCordReader?.withChannelHistory?window.PosterCordReader.withChannelHistory(bundle,roomControls.get(room.communityId||room.naddr)||[]):bundle;
        const current={...held,name:room.name};
        const prior=list.entries.find(e=>e.community_id===cid),removed=cordU64(list.tombstones.find(t=>t.community_id===cid)?.removed_at||0);
        const entry=cordMergeEntry(prior,{community_id:cid,current,added_at:cordIntegerMax(now,removed+1n),invite_ref:room.url});
        /* ONLY WHEN SOMETHING CHANGED. Every caller (join, discover, rekey, staff adoption, the
         * vault backfill) used to re-sign the whole 33302 fragment with `added_at = now` even when
         * the vault already held this exact membership — a fresh, near-identical replaceable event
         * on every public relay in the pool plus the four Concord relays, for no new information.
         * An entry that differs from the one already held in nothing but its timestamp is not a
         * change; with no change the write below publishes nothing at all. */
        if(prior&&cordCanonical({...entry,added_at:0})===cordCanonical({...prior,added_at:0}))continue;
        list.entries=list.entries.filter(e=>e.community_id!==cid);list.entries.push(entry);changed.push(cid);
      }
      return {list,changed};
    });
    for(const room of wanted)forgetLeftCommunity(viewer.pubkey,room);
    return true;
  }
  /* LEAVING USES THE SAME IDENTITY AS JOINING, or a room the vault knows by its naddr could be
   * left on one device and come straight back from the vault on the next -- the exact standoff the
   * tombstone/entry pair exists to settle. This used to return true for any room without a
   * community_id: a silent success that wrote no tombstone at all. */
  async function leaveIdFor(p,owner,room){
    const key=inviteKey(room),naddr=String(room.naddr||(inviteParts(room.url)||{}).naddr||'').toLowerCase();
    if(!key&&!naddr)return '';
    const state=await cordMembershipState(p,owner);
    for(const e of (state.merged&&state.merged.entries)||[]){
      const url=inviteRefUrl(e.invite_ref),id=cordListIdOrEmpty(e.community_id||(e.current||e.seed||{}).community_id);
      if(!id||!url)continue;
      const refNaddr=String((inviteParts(url)||{}).naddr||(String(e.invite_ref||'').match(/naddr1[0-9a-z]+/i)||[])[0]||'').toLowerCase();
      if(url===String(room.url||'')||(key&&inviteKey({url})===key)||(naddr&&refNaddr===naddr))return id;
    }
    if(room.url){ const h=await hydrateInvite(p,room.url); return cordListIdOrEmpty(h&&h.cord&&h.cord.bundle&&h.cord.bundle.community_id); }
    return '';
  }
  async function leaveArmadaMembership(p,room){
    const viewer=p.viewer?p.viewer():{};
    const cid=roomIdentity(room);
    if(!room||!cid)return true;
    if(!viewer.pubkey||!p.nip44enc||!p.nip44dec)throw new Error('sign in before leaving this community');
    // The vault keys a membership on the 32-byte community id. A room with none (never opened, or a
    // NIP-29 group) cannot have a vault entry to tombstone -- leaving it is this device's ledger alone.
    // Decided BEFORE the guestbook says "leave", so a leave that is going to fail tells nobody.
    let wireId=cordListIdOrEmpty(room.cord?.bundle?.community_id)||cordListIdOrEmpty(room.communityId)||cordListIdOrEmpty(cid);
    /* A DEVICE THAT NEVER OPENED THE ROOM STILL HAS TO LEAVE IT FOR THE ACCOUNT ("the chinese room I left
       came back on desktop"). The 32-byte id lives in the decrypted bundle, so a room known here only by
       its invite has none -- and leaving it used to be remembered on THIS device alone, while the
       account's vault kept the membership and every other device kept (or restored) the room. Find the
       id: the vault entry joined with this same invite, else the invite's own bundle. */
    if(!wireId&&room.protocol!=='nip29'){ try{ wireId=await leaveIdFor(p,viewer.pubkey,room); }catch(_){ wireId=''; } }
    const removedAt=Date.now(),leftRef=String(room.url||''),leftNaddr=String(room.naddr||'');
    if(!wireId){
      rememberLeftCommunity(viewer.pubkey,room,removedAt);
      if(room.protocol==='nip29') try{ await forgetNip29Group(p,viewer,room); }catch(_){ /* the ledger above still keeps it off this device */ }
      return true;
    }
    // CORD-02 §5: say so on the guestbook first -- after the keys are gone it cannot be wrapped.
    await publishGuestbook(p,room,'leave');
    await cordWriteMembership(p,list=>{
      const entries=new Map(list.entries.map(e=>[e.community_id,e])),tombs=new Map(list.tombstones.map(t=>[t.community_id,t]));
      const latest=cordIntegerMax(removedAt,entries.get(wireId)?.added_at||0,tombs.get(wireId)?.removed_at||0);
      entries.delete(wireId);
      tombs.set(wireId,{...tombs.get(wireId),community_id:wireId,removed_at:latest,...(leftRef?{invite_ref:leftRef}:{}),...(leftNaddr?{naddr:leftNaddr}:{})});
      return {list:{...list,entries:[...entries.values()],tombstones:[...tombs.values()]},changed:[wireId]};
    });
    rememberLeftCommunity(viewer.pubkey,room,removedAt);
    if(typeof leaveMirror==='function')void leaveMirror(p,room).catch(()=>{});
    return true;
  }
  // Only encrypted, signed envelopes cross the durable boundary. ACK completion atomically moves the
  // ciphertext into history, never decoded text. Owner/room/channel scope prevents another account retrying.
  const deliveries=new Map(),deliveryLoads=new Set();
  const deliveryOwner=p=>String(p&&p.viewer&&p.viewer().pubkey||'');
  const deliveryId=(owner,rumor)=>JSON.stringify([owner,rumor]);
  function deliveryStream(d,receipt=false){return envelopeCacheKey(d.loadKey,JSON.stringify(['delivery-v1',d.owner,d.channelId,receipt?'accepted':'pending']));}
  /* THE ROOM A QUEUED MESSAGE BELONGS TO IS FOUND THE WAY EVERY OTHER PATH FINDS A ROOM. `roomIdentity` is
   * communityId, else naddr, else url -- and a room joined by link GAINS its communityId later, when the membership
   * sync hydrates it. A message queued before that kept the old identity, matched no room, and Retry answered
   * "Delivery needs attention: sending account or community changed" in the person's OWN room, for ever
   * (2026-10-08). The exact identity still wins; failing that, `sameRoom` (community key / invite keys) against the
   * room as it was when the message was written. */
  function deliveryRoom(d){
    const rooms=saved();
    return rooms.find(r=>roomIdentity(r)===d.roomId)||(d.roomRef?rooms.find(r=>sameRoom(r,d.roomRef)):null)||null;
  }
  function deliveryAllowed(p,d){const room=deliveryRoom(d);return deliveryOwner(p)===d.owner&&room&&(room.channels||[]).some(c=>c.id===d.channelId);}
  async function persistDelivery(d,receipt=false){
    const cache=window.PCConcordCache,key=deliveryStream(d);
    if(!cache||!cache.putDelivery||!cache.getDeliveries||!cache.completeDelivery)throw new Error('encrypted delivery storage is unavailable');
    if(receipt)return cache.completeDelivery(key,d.made.wrap.id,envelopeCacheKey(d.loadKey,d.channelId));
    if(messageExpired(d.made))throw new Error('this message expired before it could be sent');
    await cache.putDelivery(key,d.made.wrap);
    const at=(d.made.tags||[]).find(t=>t[0]==='expiration')?.[1];
    if(cache.expireEvents&&typeof at==='string'&&/^(0|[1-9][0-9]*)$/.test(at)&&Number.isSafeInteger(Number(at)))await cache.expireEvents(key,[[d.made.wrap.id,Number(at)]]);
    if(!(await cache.getDeliveries(key)).some(ev=>ev.id===d.made.wrap.id))throw new Error('encrypted delivery could not be saved');
  }
  function paintDelivery(d,status){
    d.status=status;
    const rows=testMessages(d.storeId),row=rows.find(m=>m.id===d.made.rumorId);
    if(row&&row.pubkey===d.owner){row.delivery=status;row.pending=status==='sending';row.failed=status==='failed';row.remote=status==='sent';saveTestMessages(d.storeId,rows);}
    if(deliveryOwner(PC())===d.owner)backgroundRender();
  }
  async function deliverPrepared(p,d){
    if(d.status==='sent')return d.made;
    if(d.job)return d.job;
    const job=(async()=>{
      try{await persistDelivery(d);}catch(e){d.status='failed';e.noPublish=true;throw e;}
      if(!deliveryAllowed(p,d))throw new Error('sending account or community changed');
      if(!p.relayPublishRoom)throw new Error('room delivery requires an updated client');
      paintDelivery(d,'sending');
      let result;
      try{const room=deliveryRoom(d),bundle=room&&room.cord&&room.cord.bundle;
        /* AUTHENTICATE WITH THE KEYS THE MESSAGE WAS WRITTEN WITH. The wrap was made from `d.controls`; the auth
         * used to be built from whatever roomControls held by now -- after a refresh that came back partial or
         * older, or with nothing loaded, the channel's stream key was not in it: "Concord plane key is not held by
         * this membership" and the message stuck at "Delivery unknown" (2026-10-15). The union can only ADD keys. */
        const seen=new Set(),controls=[...(d.controls||[]),...(roomControls.get(d.loadKey)||[])].filter(w=>w&&w.id&&!seen.has(w.id)&&seen.add(w.id));
        result=await p.relayPublishRoom(d.relays,d.made.wrap,cordPlaneAuth(p,cordPlaneContext(p,bundle,controls,room),d.made.wrap.pubkey,d.relays));}
      catch(e){paintDelivery(d,'unknown');throw e;}
      if(!result||!result.ok){
        paintDelivery(d,result&&result.uncertain?'unknown':'failed');
        throw new Error(result&&result.msg||'room delivery was not acknowledged');
      }
      // Persist the ACK even if the user changed accounts while the room socket was pending.
      await persistDelivery(d,true);
      paintDelivery(d,'sent');return d.made;
    })();
    d.job=job;
    try{return await job;}catch(e){if(d.status==='sending')paintDelivery(d,'unknown');e.delivery=d;throw e;}
    finally{if(d.job===job)d.job=null;}
  }
  async function recoverDeliveries(p,room,channel){
    const owner=deliveryOwner(p),reader=window.PosterCordReader,controls=roomControls.get(room.communityId||room.naddr);
    if(!owner||!channel||!channel.id||!room.cord||!reader||!controls)return;
    const scope={owner,loadKey:room.communityId||room.naddr,roomId:roomIdentity(room),channelId:channel.id,storeId:channelStoreId(room,channel.name),relays:roomRelays(room.cord.bundle)};
    const key=deliveryStream(scope);if(deliveryLoads.has(key))return;deliveryLoads.add(key);
    try{
      const cache=window.PCConcordCache;if(!cache)return;
      if(!cache.getDeliveries)return;const pending=await cache.getDeliveries(key);let changed=false;
      for(const wrap of pending){
        const opened=await reader.inspectChat(room.cord.bundle,controls,channel.id,[wrap]);
        if(cache.expireEvents&&opened.expirations?.length)await cache.expireEvents(key,opened.expirations);
        if(!deliveryAllowed(p,scope))return;
        for(const m of opened.messages||[]){
          if(m.pubkey!==owner||![9,1111].includes(m.kind))continue;
          const id=deliveryId(owner,m.id);if(deliveries.has(id))continue;
          const d={...scope,made:{wrap,rumorId:m.id,ms:m.at,tags:m.tags||[],kind:m.kind},status:'unknown'};deliveries.set(id,d);
          const rows=testMessages(scope.storeId);markRemoteStore(scope.storeId);
          const existing=rows.find(x=>x.id===m.id);
          if(existing&&existing.remote&&!existing.pending&&!existing.failed&&!existing.delivery){await persistDelivery(d,true);d.status='sent';continue;}
          const row={id:m.id,pubkey:owner,by:'You',text:m.text,at:m.at,kind:m.kind,tags:m.tags||[],reactions:{},remote:false,pending:false,delivery:'unknown'};
          if(existing)Object.assign(existing,row);else rows.push(row);
          saveTestMessages(scope.storeId,rows);changed=true;
        }
      }
      if(changed&&deliveryOwner(p)===owner)backgroundRender();
    }catch(e){deliveryLoads.delete(key);console.warn('Concord pending delivery recovery failed',e);}
  }
  function deliveryHtml(p,m){
    if(!m.delivery&&!m.pending&&!m.failed)return '';
    const status=m.delivery||(m.pending?'signing':'failed');
    const label={signing:'Waiting for signer',sending:'Sending',unknown:'Delivery unknown',failed:'Not sent',sent:'Sent'}[status]||'Delivery unknown';
    const d=deliveries.get(deliveryId(deliveryOwner(p),m.id));
    const statusHtml=status==='sent'
      ? '<span class="cc-delivery-status cc-delivery-confirmed" role="status" aria-label="Sent" title="Sent"><svg class="ic" aria-hidden="true"><use href="#i-check"></use></svg></span>'
      : `<span class="cc-delivery-status" role="status">${label}</span>`;
    return statusHtml+
      (d&&['failed','unknown'].includes(status)?`<button data-cc-retry-delivery="${p.enc(m.id)}" title="Resend the same signed message; no new signature">Retry delivery</button>`:'');
  }
  /* DISSOLUTION (kind 3308 vsk 10 at the community's dissolution key, CORD / Vector). The reader
   * could recognise one and nothing ever asked, so a room its owner had dissolved in Vector just went
   * quiet here, with a composer that still looked usable. Once seen, the room is frozen: history stays
   * readable (the control set is sealed at what we held) and nothing more can be sent. */
  async function checkDissolution(p,room){
    const r=window.PosterCordReader,bundle=room&&room.cord&&room.cord.bundle; if(!r||!r.inspectDissolution||!r.dissolutionPubkey||!bundle||bundle.dissolved||room.local)return false;
    const relays=roomRelays(bundle),wraps=await cordQuery(p,relays,[{kinds:[1059],authors:[r.dissolutionPubkey(bundle)],limit:5}],{timeout:8000,max:4,plane:cordPlaneContext(p,bundle,roomControls.get(room.communityId||room.naddr)||[],room)});
    const found=r.inspectDissolution(bundle,wraps||[]); if(!found)return false;
    markDissolved(p,room); return true;
  }
  function markDissolved(p,room){
    const identity=roomIdentity(room),latest=saved(),at=latest.findIndex(x=>roomIdentity(x)===identity); if(at<0)return;
    const loadKey=room.communityId||room.naddr,held=(roomControls.get(loadKey)||[]).map(e=>e.id);
    latest[at]={...latest[at],cord:{...latest[at].cord,sealed_controls:held,bundle:{...latest[at].cord.bundle,dissolved:true}}};
    save(latest); if(roomIdentity(saved()[state.community])===identity){stopChatLive();render();}
    p.toast((room.name||'A community')+' was dissolved by its owner — its history stays readable');
  }
  async function dissolveCommunity(p,room){
    const r=window.PosterCordReader,bundle=room.cord.bundle,me=p.viewer?p.viewer().pubkey:'';
    if(!r||!r.createDissolutionWrap||bundle.owner!==me)throw new Error('only the owner may dissolve a community');
    const made=await r.createDissolutionWrap(bundle,me,p.signTemplate),relays=roomRelays(bundle),scope=cordPlaneContext(p,bundle,roomControls.get(room.communityId||room.naddr)||[],room);
    const res=await p.relayPublishRoom(relays,made.wrap,cordPlaneAuth(p,scope,made.wrap.pubkey,relays)); if(!res||!res.ok)throw new Error('the community relays did not accept it');
    markDissolved(p,room);
  }
  async function hydrateRoomStreams(p,index,expectedIdentity=''){
    const rooms=saved(),room=rooms[index],reader=window.PosterCordReader,bundle=room&&room.cord&&room.cord.bundle;
    if(!room||!bundle||!reader)return;
    const loadKey=room.communityId||room.naddr,identity=roomIdentity(room),owner=deliveryOwner(p),jobKey=owner+'\n'+loadKey;
    const hydrationScope=cordPlaneContext(p,bundle,[],room),currentOwner=()=>hydrationScope.membershipCurrent();
    if(!bundle.dissolved)void checkDissolution(p,room).catch(()=>{});
    if(roomLoads.has(jobKey))return roomLoads.get(jobKey);
    const job=(async()=>{
      /* Membership refresh and Leave can finish while relay history is in flight. Persist by room
       * identity, never the numeric array slot captured before an await, or a slow response can
       * overwrite another community or resurrect one the user left. */
      const persistRoom=()=>{if(!currentOwner())return false;const latest=saved(),at=latest.findIndex(item=>roomIdentity(item)===identity);if(at<0)return false;latest[at]={...latest[at],...room,cord:{...(latest[at].cord||{}),...(room.cord||{})}};save(latest);return true;};
      const seed=reader.inspectControl(bundle,[]), relays=roomRelays(bundle);
      const controlKey=envelopeCacheKey(loadKey,'control');
      let controlWraps=await cachedEnvelopes(controlKey);if(!currentOwner())return;
      const applyControl=wraps=>{if(!currentOwner())return 0;const admitted=bundle.dissolved?(wraps||[]).filter(e=>(room.cord.sealed_controls||[]).includes(e.id)):(wraps||[]);const info=reader.inspectControl(bundle,admitted);roomControls.set(loadKey,admitted);if(info.relaysAuthoritative)relays.splice(0,relays.length,...roomRelays(bundle));room.name=info.name||room.name;room.description=info.description||room.description;if(Number.isSafeInteger(info.message_expiration))room.message_expiration=info.message_expiration;room.history_retention=retentionDays(info.custom&&info.custom[RETENTION_KEY]);room.banned=Array.isArray(info.banned)?info.banned:room.banned||[];room.moderators=Array.isArray(info.moderators)?info.moderators:room.moderators||[];/* An encrypted icon can require an IndexedDB read, a remote download, AES-GCM and hashing. It is decoration, so never hold the cached channel list or first history paint behind it. Plain/cleared icons still mutate synchronously before this promise yields. Persist and repaint the icon when its bounded job finishes. */void applyRoomIconMetadata(room,info,loadKey,seed).then(changed=>{if(!changed)return;if(!persistRoom())return;const active=saved()[state.community];if(roomIdentity(active)===identity)backgroundRender();});void applyRoomBanner(room,info,loadKey,seed).then(changed=>{if(!changed)return;persistRoom();if(typeof document!=='undefined'&&document.body.classList.contains('concord-view'))backgroundRender();});const channels=(info.channels||[]).map(c=>({id:c.id,name:c.name,private:!!c.private,readOnly:!!c.readOnly,view:c.view==='forum'?'forum':'chat',streamPubkeys:c.streamPubkeys,repositories:Array.isArray(c.repositories)?c.repositories:[]})).filter(c=>c.name);if(info.deletedChannels?.length)room.deletedChannels=[...new Set([...(room.deletedChannels||[]),...info.deletedChannels])];if(channels.length)room.channels=channels.filter(c=>!room.deletedChannels?.includes(c.id));else if(room.deletedChannels?.length)room.channels=(room.channels||[]).filter(c=>!room.deletedChannels.includes(c.id));for(const channel of room.channels||[]){markRemoteStore(channelStoreId(room,channel.name));if(reader.inspectPinList?.(bundle,admitted,channel.id)?.entries?.length)void window.PCConcord?.reconcilePins?.(p,room,channel.id)?.catch(e=>console.warn("Pin deletion reconciliation is pending",e));}persistRoom();void refreshGuestbookMembers(p,room,wraps||[]);return channels.length;};
      const applyChannel=async(channel,wraps)=>{
        /* THROUGH readChat, NEVER reader.inspectChat DIRECTLY. The readable channel set is built
         * from the control events and from nothing else, so a saved channel whose id the control
         * set does not carry is refused with "channel is not readable with this membership" — and
         * readChat is the one place that re-derives the id by NAME before giving up. Calling the
         * reader raw here is what made hydration the only path in this file without that repair. */
        if(!currentOwner())return;
        const opened=await readChat(p,reader,bundle,controlWraps||[],room,channel,wraps||[]);
        if(!currentOwner())return;
        const storeId=channelStoreId(room,channel.name);markRemoteStore(storeId);
        const next=mergeCordTimeline(testMessages(storeId),opened,p,deliveryOwner(p)+'\n'+storeId);
        saveTestMessages(storeId,next);
        /* A MENTION IN A CHANNEL YOU ARE NOT LOOKING AT IS STILL A MENTION.
         *
         * Both other callers pass `state.channel`, so only the channel on screen could ever raise
         * one — and the channel on screen is the one you are already reading. Hydration walks every
         * channel in the room, which makes this the only place that can see the rest.
         *
         * Announcing history is prevented by notifyMentions itself: a channel with no cursor yet
         * records one and returns without notifying, so the first read of a room is silent and
         * only genuinely newer messages ever raise anything. */
        const who=p.viewer?p.viewer():{},prof=who.profile||{};
        notifyMentions(p,room,next,who,
                       prof.display_name||prof.name||(who.npub?who.npub.slice(0,12)+'…':'You'),
                       channel.name);
      };
      /* Paint the encrypted on-device copy before opening sockets. This is the offline/reload path,
       * and also prevents relay latency from looking like an empty room. */
      let cachedChannelCount=0,cachedHistoryRendered=false;const cachedStale=[];
      if(controlWraps.length){
        cachedChannelCount=applyControl(controlWraps);
        /* Control metadata is enough to replace the saved placeholder channel list. Paint it now,
         * before decrypting any chat page, and choose a channel that actually exists in that list.
         * A fresh renderer commonly starts at `general`; Armada rooms are not required to have one.
         * Keeping that missing name selected made a healthy cache paint an empty conversation until
         * the user left and re-entered the room. */
        const activeNow=saved()[state.community];
        if(roomIdentity(activeNow)===identity){
          const names=(room.channels||[]).map(channel=>channel.name),wanted=state.channel||'general';
          if(names.length&&!names.includes(wanted)){state.channel=names[0];state.thread=null;}
          backgroundRender();
        }
        const selected=state.channel||((room.channels||[])[0]&&room.channels[0].name)||'general',ordered=[...(room.channels||[])].sort((a,b)=>(a.name===selected?-1:b.name===selected?1:0));
        /* Decrypt only the newest page first and paint the selected channel immediately. Loading
         * 5,000 envelopes for every channel before the first render made a healthy encrypted cache
         * look indistinguishable from a relay miss, and could exhaust an Android WebView renderer. */
        const replayCached=async channel=>{
          const wraps=await cachedEnvelopePage(envelopeCacheKey(loadKey,channel.id),300);
          if(!wraps.length)return;
          /* ONE UNREADABLE CHANNEL MUST NOT COST THE ROOM, AND HERE IT COST IT FOR EVER.
           *
           * This replay is the ON-DISK cache, so it is deterministic: a saved channel the control
           * set does not carry threw "channel is not readable with this membership" out of the
           * whole job on every single open, and the rescue below could not catch it because it
           * needs `cachedHistoryRendered`, which is set on the line that just threw. The reader
           * saw "could not refresh community: channel is not readable with this membership" every
           * time they clicked a room they are a member of, with no way out but clearing storage.
           *
           * The network half already treats a channel that will not load as recorded-and-skipped
           * (`stalled`, left to the live tick). The cached half now does the same. */
          try{ await applyChannel(channel,wraps); if(channel.name===selected)cachedHistoryRendered=true; }
          catch(e){ if(!/not readable with this membership/i.test(String(e&&e.message||e))) throw e;
                    cachedStale.push(channel.name); }
        };
        /* ONLY THE CHANNEL ON SCREEN IS ON THE CRITICAL PATH — the cached half never learned what
         * the network half below already knows.
         *
         * This was one serial loop over EVERY channel, each decrypting a 300-envelope page before
         * the next began, and the network pass could not start until all of it had finished. It is
         * NIP-44 on the main thread: measured against Soapbox's community on real data, a 300-wrap
         * page costs ~560ms, so its thirteen channels spent five to seven seconds decrypting twelve
         * conversations nobody had opened — before the first relay was asked. That is the whole
         * "Concord is slow to open a room", and it gets WORSE the longer the app is used, because
         * the cache it re-reads is the thing that grows. Armada is not doing anything cleverer here;
         * it simply is not doing this.
         *
         * The rest is prefetch, so it runs behind the room instead of in front of it, yielding
         * between channels so the renderer can paint, and stops if the reader leaves. Switching to a
         * channel it has not reached yet is still instant-ish: `refreshActiveChannel` fetches
         * whichever channel is actually open, and with no prior messages its `since` is 0. */
        const [cachedHead,...cachedRest]=ordered;
        if(cachedHead){
          /* PAINT A SCREENFUL, NOT A PAGE. Even after the other channels moved off the critical
           * path, the one channel left still decrypted 300 envelopes before showing a single
           * message — and a phone is several times slower at NIP-44 than the box this was measured
           * on. Nobody is waiting for message 300; they are waiting for the last twenty. The rest of
           * the page follows immediately behind, in the same prefetch as the other channels, so
           * scrolling up still finds history. */
          const quick=await cachedEnvelopePage(envelopeCacheKey(loadKey,cachedHead.id),FIRST_PAINT_WRAPS);
          if(quick.length){
            try{
              await applyChannel(cachedHead,quick); cachedHistoryRendered=true;
              /* Only when the allowance was actually SPENT (a short page means the cache simply
               * holds no more, and re-reading it would find the same thing). One top-up, never a
               * loop: if the deeper page still folds short, the channel really is that quiet and
               * the background prefetch behind this covers the rest. */
              if(quick.length>=FIRST_PAINT_WRAPS){
                let _shown=0;
                try{ _shown=(testMessages(channelStoreId(room,cachedHead.name))||[]).length; }catch(_){ _shown=FIRST_PAINT_MESSAGES; }
                if(_shown<FIRST_PAINT_MESSAGES){
                  const deeper=await cachedEnvelopePage(envelopeCacheKey(loadKey,cachedHead.id),FIRST_PAINT_WRAPS*5);
                  if(deeper.length>quick.length) await applyChannel(cachedHead,deeper);
                }
              }
            }
            catch(e){ if(!/not readable with this membership/i.test(String(e&&e.message||e))) throw e;
                      cachedStale.push(cachedHead.name); }
          }
          if(roomIdentity(saved()[state.community])===identity)backgroundRender();
        }
        /* The head's FULL page is prefetch too — it is the deeper history behind the screenful that
         * has already been painted, not the thing being waited for. */
        if(cachedHead)cachedRest.unshift(cachedHead);
        if(cachedRest.length)void (async()=>{
          for(const channel of cachedRest){
            await new Promise(resolve=>setTimeout(resolve,0));
            if(roomIdentity(saved()[state.community])!==identity)return;
            /* A prefetch that fails is a channel that will be read when it is opened, never a
             * reason to break the room somebody is already looking at. */
            try{ await replayCached(channel); }catch(_){ }
          }
          if(roomIdentity(saved()[state.community])===identity)backgroundRender();
          /* Named, not counted, and not reported to the reader: the network pass re-reads these
           * with a fresh control set and usually resolves them, so a toast here would announce a
           * problem that is about to fix itself. It is in the console because the next report of a
           * half-empty room should say WHICH channel. */
          if(cachedStale.length)console.warn('Concord: cached history unreadable for',cachedStale.join(', '),'— retrying over the network');
        })();
      }
      try{
      /* Ask this node to hold the community BEFORE reading it (bounded: a node that does not answer costs
       * six seconds once, then the room reads its own relays as it always did). */
      if(typeof ensureMirror==='function')await Promise.race([ensureMirror(p,room,bundle,controlWraps),new Promise(r=>setTimeout(r,6000))]);
      if(!currentOwner())return;
      const controlRead=await queryControlUntilAsked(p,relays,seed.controlPubkeys,controlWraps,{plane:cordPlaneContext(p,bundle,controlWraps,room)}),
        completeControl=controlRead.wraps,fetchedControl=completeControl.filter(ev=>!controlWraps.some(old=>old.id===ev.id));
      if(!currentOwner())return;
      controlWraps=completeControl;await cacheEnvelopes(controlKey,fetchedControl);
      if(!currentOwner())return;
      const channelCount=applyControl(controlWraps);
      if(!channelCount){
        /* SAY WHAT WAS ACTUALLY MEASURED. "the control stream returned no readable channels" is
         * true and useless: it is the same sentence whether no relay answered, every relay answered
         * with nothing, or the answers could not be opened with this membership — three different
         * problems with three different fixes. Reported from a phone as one line I could not act on
         * and could not reproduce, for every room at once.
         *
         * The numbers are cheap and they are the whole diagnosis: relays tried, wraps held after
         * the fetch, and how many of those were new. */
        /* NOBODY ANSWERED IS NOT "NOTHING HERE". With the control set unread, no channel is ever fetched,
         * so no channel had a reach recorded — and an unrecorded channel drew "This is the start of this
         * encrypted channel" over a room whose relays (and this node's mirror) could not be asked. */
        const r=controlRead.reach||{};
        if(!(r.ok||[]).length&&((r.failed||[]).length||(r.unasked||[]).length||(r.unheld||[]).length))
          for(const ch of room.channels||[])noteChannelReach(channelStoreId(room,ch.name),{failed:r.failed||[],cooled:r.unasked||[],unheld:r.unheld||[]},relays);
        throw new Error(unreadableControlMessage(controlRead.reach,relays,(controlWraps||[]).length,(fetchedControl||[]).length));
      }
      /* The channels are known now, so their streams can join the mirror (a no-op when they already have). */
      if(typeof ensureMirror==='function')await Promise.race([ensureMirror(p,room,bundle,controlWraps),new Promise(r=>setTimeout(r,6000))]);
      if(!currentOwner())return;
      const selected=state.channel||'general',networkOrder=[...room.channels].sort((a,b)=>(a.name===selected?-1:b.name===selected?1:0));
      const fetchChannel=async channel=>{
        if(!currentOwner())return;
        const reach={};
        const cacheKey=envelopeCacheKey(loadKey,channel.id),cached=await cachedEnvelopes(cacheKey),
          wraps=await queryEnvelopeHistory(p,relays,channel.streamPubkeys,cached,{plane:cordPlaneContext(p,bundle,controlWraps,room),report:reach}),
          fetched=wraps.filter(ev=>!cached.some(old=>old.id===ev.id));
        noteChannelReach(channelStoreId(room,channel.name),reach,relays);
        if(!currentOwner())return;
        await cacheEnvelopes(cacheKey,fetched);await applyChannel(channel,wraps);
      };
      /* THE CHANNEL ON SCREEN FIRST — it is the only one anybody is waiting for.
       *
       * IT USED TO BE ALLOWED TO THROW, on the reasoning that failing to load the conversation
       * somebody just opened is worth saying out loud. On a desktop that is true. On a PHONE it is
       * the ordinary case — one relay slow past the six-second window is enough — and the throw took
       * the whole hydration with it: the toast said "could not refresh room history" AND the room
       * showed no messages at all, because every other channel behind it was abandoned and the
       * control set was never applied. Reported from the APK as exactly those two things together.
       *
       * So it is recorded like any other channel now. What is left is the honest report at the
       * bottom: a room where NOTHING could be read still says so, once, and a room that got some of
       * itself shows what it got. */
      const head=networkOrder[0];
      let headFailed=false;
      if(head){
        try{ await fetchChannel(head); }
        catch(_){ headFailed=true; }
        if(roomIdentity(saved()[state.community])===identity)backgroundRender();
      }
      /* …AND THE REST CONCURRENTLY, BECAUSE THIS USED TO BE ONE SERIAL QUEUE.
       *
       * Every channel waited on the previous channel's relay round trip, so a ten-channel community
       * cost ten of them before the room was usable — reported as Concord being slow, and worst on
       * exactly the big Armada rooms where it matters most.
       *
       * AND ONE CHANNEL'S FAILURE IS NOT THE ROOM'S FAILURE. A single throw in that loop abandoned
       * every channel behind it, surfaced as "could not refresh room history", and left `hydrated`
       * unset — so the whole room was fetched again on the next click, which made the next failure
       * likelier and the room slower still. A channel that would not load is recorded and left to
       * the live tick, which fetches whatever channel is actually open. */
      /* THE REST IS PREFETCH, AND PREFETCH IS NOT WORTH WAITING FOR.
       *
       * This was awaited, so opening a room blocked until every channel in it had been fetched —
       * thirteen relay round trips on the Soapbox community before it was usable, while the person
       * was looking at ONE channel. Measured here: a one-channel room ready in five seconds, a
       * thirteen-channel room not ready in eighty.
       *
       * Nothing is lost by letting it run behind: `refreshActiveChannel` fetches whatever channel
       * is actually open on its next tick, and with no prior messages its `since` is 0 — the full
       * history. So a channel that has not been prefetched yet fills the moment somebody opens it,
       * which is the only moment it matters. */
      const queue=networkOrder.slice(1),stalled=[];
      const prefetch=Promise.all(Array.from({length:Math.min(4,queue.length)},async()=>{
        for(;;){
          const channel=queue.shift(); if(!channel)return;
          try{ await fetchChannel(channel); }
          catch(_){ stalled.push(channel.name); }
        }
      })).then(()=>{
        /* Recorded when it finishes, not before — `stalled` is what the room reports about itself. */
        const latest=saved(),at=latest.findIndex(x=>roomIdentity(x)===identity);
        if(at>=0&&latest[at].cord){ latest[at].cord.stalled=stalled; save(latest); }
      },()=>{});
      /* Awaited only when the open channel itself failed: then the prefetch is the only thing that
       * can still tell us whether this community is reachable at all, and the report below needs
       * that answer rather than an empty list. */
      if(headFailed) await prefetch;
      if(headFailed && head) stalled.unshift(head.name);
      room.cord.stalled=stalled;
      /* NOTHING TO THROW HERE. A room that reached no channel yet is a room still loading on a
       * slow radio, and the client is already retrying it every four seconds — raising made the
       * caller toast "could not refresh room history" on a condition nobody can act on, over and
       * over, which is exactly how this was reported from the phone. What the room could not read
       * is recorded in `stalled` and shown where it belongs. */
      if(!currentOwner())return;
      room.cord.hydrated=true;hydratedRoomViews.add(identity);if(!persistRoom())return;
      /* A relay answer may return after the reader chose another community. Persisting the fetched
       * room is still useful, but repainting/scrolling the new room is not. Notification launches
       * pass the durable identity explicitly because saved array positions can change meanwhile. */
      const currentRooms=saved(),current=state.community==null?null:currentRooms[state.community],stillSelected=roomIdentity(current)===identity&&
        (!expectedIdentity||roomIdentity(current)===expectedIdentity);
      /* Explicit room/channel handlers own the initial jump to latest. A network refresh finishing
       * later must preserve a reader who has deliberately scrolled up meanwhile. */
      if(stillSelected)backgroundRender();
      }catch(e){
        /* Cached control plus selected-channel history is a usable room, even when every live relay
         * path is temporarily down. Leave this renderer view unmarked so the next click retries the
         * network refresh, but do not turn a successful offline room open into a failure toaster. */
        if(cachedChannelCount&&cachedHistoryRendered){console.warn('Concord room refresh failed; using cached history',e);return;}
        throw e;
      }
    })().finally(()=>roomLoads.delete(jobKey)); roomLoads.set(jobKey,job); return job;
  }
  async function publishCordNative(p,room,channelName,text,extraTags=[],kind=9,onPrepared){
    const viewer=p.viewer?p.viewer():{},reader=window.PosterCordReader,bundle=room&&room.cord&&room.cord.bundle;
    if(!viewer.pubkey||!reader||!reader.createChatWrap||!bundle)throw new Error('CORD publishing is unavailable');
    const channel=(room.channels||[]).find(c=>c.name===channelName); if(!channel||!channel.id)throw new Error('channel key is unavailable');
    const loadKey=room.communityId||room.naddr,relays=roomRelays(bundle);
    let controlWraps=roomControls.get(loadKey);
    if(!controlWraps){ const seed=reader.inspectControl(bundle,[]),key=envelopeCacheKey(loadKey,'control'),cached=await cachedEnvelopes(key);controlWraps=await queryEnvelopeHistory(p,relays,seed.controlPubkeys,cached,{plane:cordPlaneContext(p,bundle,cached,room)});await cacheEnvelopes(key,controlWraps.filter(ev=>!cached.some(old=>old.id===ev.id)));
      /* AN EMPTY CONTROL SET IS "COULD NOT ASK", NOT "THIS COMMUNITY HAS NO CHANNELS".
       *
       * A community's channels are DEFINED by its control events — Armada's own reader builds
       * `channelsView` from `folded.channels`, which comes from those wraps and from nothing else.
       * So with none of them the readable channel set is empty and `inspectChat` correctly refuses
       * every id with "channel is not readable with this membership".
       *
       * `roomControls.set(loadKey, [])` then made that permanent for the session: `[]` is truthy,
       * so the guard above reads it as "already fetched" and the room can never recover — the
       * error repeats on every four-second live tick, which is exactly how it was reported. One
       * unreachable relay at the wrong moment cost the whole visit.
       *
       * Remember only an answer that actually contained something; leave the miss unrecorded so
       * the next tick asks again. */
      /* DO NOT REMEMBER A MISS — but do not abandon the rest of hydration either. Returning here
       * skipped everything below, which is what makes the room vanish from the list rather than
       * merely stay unread. Leave the cache unset so the next pass asks again, and carry on. */
      if((controlWraps||[]).length) roomControls.set(loadKey,controlWraps);
      else roomLoadWarning(p,loadKey,'could not read this community yet: ','no control events came back — retrying'); }
    /* The read path already repairs a saved channel id against the current control stream by NAME.
     * Publishing did not. A renamed/rekeyed channel therefore displayed perfectly (readChat used
     * its repaired id) and then createChatWrap rejected the same membership as non-writable.
     *
     * Retry only that precise structural refusal. First refresh the control history—including the
     * encrypted disk copy—then resolve the requested channel by name. Do not silently choose the
     * first channel and do not rewrite the saved list from a possibly partial relay response. */
    let writeChannel=channel,made;
    try{ made=await reader.createChatWrap(bundle,controlWraps||[],writeChannel.id,text,viewer.pubkey,p.signTemplate,extraTags,kind); }
    catch(e){
      if(!/not writable with this membership/i.test(String(e&&e.message||e)))throw e;
      const seed=reader.inspectControl(bundle,[]),key=envelopeCacheKey(loadKey,'control'),cached=await cachedEnvelopes(key),
        known=[...cached,...(controlWraps||[])].filter((ev,i,a)=>ev&&ev.id&&a.findIndex(x=>x&&x.id===ev.id)===i),
        fresh=await queryEnvelopeHistory(p,relays,seed.controlPubkeys,known,{plane:cordPlaneContext(p,bundle,known,room)}),
        added=fresh.filter(ev=>!known.some(old=>old.id===ev.id));
      if(added.length)await cacheEnvelopes(key,added);
      if(fresh.length){controlWraps=fresh;roomControls.set(loadKey,fresh);}
      const fixed=reconcileChannels(reader,bundle,controlWraps||[],room,channelName);
      if(!fixed)throw new Error('this channel is no longer available with your current community membership');
      writeChannel=fixed;
      made=await reader.createChatWrap(bundle,controlWraps||[],writeChannel.id,text,viewer.pubkey,p.signTemplate,extraTags,kind);
    }
    if(![9,1111].includes(kind)){
      if(deliveryOwner(p)!==viewer.pubkey)throw new Error('sending account changed');
      if(!p.relayPublishRoom)throw new Error('room delivery requires an updated client');
      const result=await p.relayPublishRoom(relays,made.wrap,cordPlaneAuth(p,cordPlaneContext(p,bundle,controlWraps,room),made.wrap.pubkey,relays));
      if(!result||!result.ok)throw new Error(result&&result.msg||'room delivery was not acknowledged');
      await cacheEnvelopes(envelopeCacheKey(loadKey,writeChannel.id),[made.wrap]);return made;
    }
    const d={owner:viewer.pubkey,loadKey,roomId:roomIdentity(room),roomRef:{communityId:room.communityId,naddr:room.naddr,url:room.url,inviteAliases:room.inviteAliases,cord:room.cord&&room.cord.bundle?{bundle:{community_id:room.cord.bundle.community_id}}:undefined},channelId:writeChannel.id,
      storeId:channelStoreId(room,channelName),relays,made,status:'unknown',controls:controlWraps||[]};   // the set the wrap was MADE with (memory only)
    deliveries.set(deliveryId(d.owner,made.rumorId),d);
    if(onPrepared)onPrepared(d);
    return deliverPrepared(p,d);
  }
  function nip29PreviousTags(messages,viewerPubkey){const ids=(messages||[]).filter(m=>m.remote&&!m.pending&&m.pubkey!==viewerPubkey).slice(-3).map(m=>messageId(m).slice(0,8));return ids.length?[['previous',...ids]]:[];}
  async function publishNip29Message(p,room,channelName,text,extraTags=[],kind=9){
    if(!p.publishNip29Authed||!room.relay||!room.groupId)throw new Error('authenticated NIP-29 publishing is unavailable');
    const viewer=p.viewer?p.viewer():{},tags=[['h',room.groupId],...nip29PreviousTags(testMessages(channelStoreId(room,channelName)),viewer.pubkey),...extraTags];
    const event=await p.publishNip29Authed(room.relay,{kind,created_at:Math.floor(Date.now()/1000),content:text,tags});
    if(deliveryOwner(p)===viewer.pubkey&&saved().some(r=>sameRoom(r,room)))mergeNip29Live(p,room,[event]);
    return{...event,rumorId:event.id,ms:Number(event.created_at)*1000};
  }
  async function publishCordMessage(p,room,channelName,text,extraTags=[],kind=9,onPrepared){
    if(p.customEmojiTags) extraTags=await p.customEmojiTags(text,extraTags);
    return room&&room.protocol==='nip29'?publishNip29Message(p,room,channelName,text,extraTags,kind):publishCordNative(p,room,channelName,text,extraTags,kind,onPrepared);
  }
  /* A CHANNEL THE CONTROL SET DOES NOT CONTAIN IS A STALE NAME, NOT A REFUSED MEMBERSHIP.
   *
   * `room.channels` is saved to localStorage and `applyControl` only replaces it when a control
   * fetch yields something, so a room can keep a channel that the community has since renamed,
   * removed, or that a partial fetch once produced. `refreshActiveChannel` then picks that channel
   * BY NAME out of the saved list and hands its id to the reader, which builds its readable set
   * from the control events and from nothing else — so it refuses, correctly, with "channel is not
   * readable with this membership".
   *
   * That reads as a permissions problem and is a bookkeeping one, and because the caller runs on a
   * live timer it repeated for ever: the room never syncs and the console fills up. Reported
   * verbatim, with the whole stack, from a live tick.
   *
   * So: re-derive the channel list from the control set we are about to read WITH, and move to a
   * channel that is actually in it. Only called when the id is already known to be missing, so an
   * ordinary tick pays nothing. An empty readable set is "could not ask" — leave everything alone
   * and let the next tick retry, exactly as the control fetch itself does. */
  function reconcileChannels(reader,bundle,controlWraps,room,wantName){
    let info=null; try{ info=reader.inspectControl(bundle,controlWraps||[]); }catch(_){ return null; }
    const live=((info&&info.channels)||[]).filter(c=>c&&c.name);
    if(!live.length) return null;
    /* IT LOOKS UP AN ID. IT DOES NOT REWRITE THE ROOM.
     *
     * This used to replace `room.channels` with whatever this control read yielded, and readChat
     * persisted that. A control read is often PARTIAL — some events back, not all, which is the
     * ordinary case on a slow relay — so one stale id was enough to DELETE every channel that
     * partial read did not mention. Those channels then reported "channel is not readable with this
     * membership", for ever, and the community looked empty. Reported on Android and then on the
     * desktop, and it got worse the more retries I added, because every retry was another chance to
     * overwrite the list with less of it.
     *
     * And only BY NAME. Falling back to the first live channel silently moved somebody to a
     * different conversation and called it a repair. */
    return live.find(c=>c.name===wantName)||null;
  }
  /* Read the channel, and repair the saved list rather than throwing on every tick. The repair is
   * announced on screen — silently moving somebody to a different channel is worse than the error,
   * because the messages they were looking at simply stop. */
  async function acknowledgeDeliveryEcho(p,room,channel,wraps,opened){
    if(window.PCConcordCache?.expireEvents && opened.expirations?.length)
      try{await window.PCConcordCache.expireEvents(envelopeCacheKey(room.communityId||room.naddr,channel.id),opened.expirations);}catch(e){console.warn('Concord expiry sweep failed',e);}
    const ids=new Set((wraps||[]).map(w=>w.id)),rumors=new Set((opened.messages||[]).map(m=>m.id));
    /* ONCE. A sent message stays in `deliveries` until it expires, and every read of the room -- the
     * 4 s tick, every live wrap -- sees its echo again. Re-acknowledging it each time re-saved it and
     * repainted the WHOLE message list every couple of seconds: "communities is flashing now on
     * desktop", with the renderer's DOM node count climbing by ~370k a minute. */
    for(const d of deliveries.values())if(d.status!=='sent'&&d.owner===deliveryOwner(p)&&d.roomId===roomIdentity(room)&&d.channelId===channel.id&&ids.has(d.made.wrap.id)&&rumors.has(d.made.rumorId)){
      await persistDelivery(d,true);paintDelivery(d,'sent');
    }
    return opened;
  }
  async function readChat(p,reader,bundle,controlWraps,room,channel,wraps){
    const live=saved().find(r=>roomIdentity(r)===roomIdentity(room));
    if(live?.cord?.bundle?.dissolved){room=live;bundle=live.cord.bundle;const ids=new Set(live.cord.sealed_controls||[]);controlWraps=controlWraps.filter(e=>ids.has(e.id));}
    try{ void window.PCConcord?.reconcilePins?.(p,room,channel.id,wraps)?.catch(e=>console.warn("Pin deletion reconciliation is pending",e));return await acknowledgeDeliveryEcho(p,room,channel,wraps,await reader.inspectChat(bundle,controlWraps,channel.id,wraps,bundle.dissolved?(room.cord.sealed_history?.[channel.id]||[]):[])); }
    catch(e){
      if(!/not readable with this membership/i.test(String(e&&e.message||e))) throw e;
      const fixed=reconcileChannels(reader,bundle,controlWraps,room,channel.name);
      if(!fixed) throw e;
      /* Nothing is persisted here: the repair is an id lookup for THIS read, and the saved
       * channel list belongs to applyControl, which sees the whole control stream. */
      if(fixed.name!==channel.name){ state.channel=fixed.name; if(p&&p.toast)p.toast('#'+channel.name+' is no longer in this community — showing #'+fixed.name); }
      return await acknowledgeDeliveryEcho(p,room,fixed,wraps,await reader.inspectChat(bundle,controlWraps,fixed.id,wraps,bundle.dissolved?(room.cord.sealed_history?.[fixed.id]||[]):[]));
    }
  }
  /* MERGE WHAT CAME BACK AND PAINT IT. One implementation, because there are now two ways for a
   * message to arrive — the periodic tick and the live subscription below — and two copies of a
   * merge that dedupes, folds reactions and preserves scroll is how they drift. */
  /* MESSAGES ARRIVE, THEY ARE NOT FETCHED FOR.
   *
   * The tick below runs every four seconds, but the query it makes carries `minInterval:60000` —
   * so on any community whose relays are not already in the shared pool, a message could take a
   * FULL MINUTE to appear. That is the "live message updates" complaint, and no amount of tightening
   * the timer fixes it: polling a relay that is perfectly capable of pushing is the wrong shape.
   *
   * `p.relaySubscribe` already exists and discovery already uses it. Chat now does too.
   *
   * WRAPS ARE BUFFERED, NOT DECRYPTED ONE AT A TIME. Opening a wrap is real cryptography, and a
   * busy channel would otherwise run it per event on the main thread. A short debounce turns a
   * burst into one batch, which is also what makes a single repaint out of ten arrivals.
   *
   * The subscription is keyed on room+channel and closed whenever either changes, so switching
   * channels cannot leave an old one feeding the new one's store. */
  let chatSub=null,chatSubKey='',chatBuffer=[],chatFlush=null;
  function stopChatLive(){
    const sub=chatSub;chatSub=null;chatSubKey='';chatBuffer=[];
    if(chatFlush){clearTimeout(chatFlush);chatFlush=null;}
    if(sub&&typeof sub.close==='function')try{sub.close();}catch(_){}
  }
  function chatLiveKey(p,room,channel){if(room&&room.protocol==='nip29')return roomIdentity(room)+'\n'+String(channel&&channel.id||'');return JSON.stringify([roomIdentity(room),String(channel&&channel.id||''),deliveryOwner(p),room&&room.cord&&room.cord.bundle,cordControlStamp(roomControls.get(room&&(room.communityId||room.naddr)))]);}
  function startChatLive(p,room,channel){
    const membership=room&&room.cord&&room.cord.bundle;
    if(membership&&(membership.removed||membership.dissolved||(membership.removed_channels||[]).includes(channel&&channel.id))){stopChatLive();return;}
    const key=chatLiveKey(p,room,channel);
    if(chatSub&&chatSubKey===key)return;
    stopChatLive();
    const nip29=room&&room.protocol==='nip29',authors=(channel&&channel.streamPubkeys)||[];
    const R=(window.PCConcordRelay||window.Relay);
    if(!p||!R||!R.subscribe||!R.subscribeFrom||(!nip29&&!authors.length))return;
    chatSubKey=key;
    /* A small look-back, not none: between the room's opening history read and this subscription
     * being armed there is a gap, and a message that lands inside it would otherwise wait for the
     * next tick — which is the very delay this exists to remove. */
    const since=Math.max(0,Math.floor(Date.now()/1000)-180);
    const onEvent=ev=>{
      if(!ev||chatSubKey!==key)return;
      if(nip29){if(![5,7,9,10,11,12,1111].includes(Number(ev.kind)))return;}
      else if(Number(ev.kind)===21059){void noteTyping(p,room,channel,ev);return;}
      else if(Number(ev.kind)!==1059)return;
      chatBuffer.push(ev);
      if(chatFlush)return;
      chatFlush=setTimeout(()=>{chatFlush=null;void flushChatLive(p,key);},350);
    };
    const urls=nip29?[room.relay]:roomRelays(room&&room.cord&&room.cord.bundle),
      filters=nip29?[{kinds:[5,7,9,10,11,12,1111],'#h':[room.groupId],since}]:[{kinds:[1059],authors,since},{kinds:[21059],authors,since:Math.floor(Date.now()/1000)-30}];
    try{
      const plane=!nip29&&window.PosterCordReader&&window.PosterCordReader.createPlaneAuth&&
        cordPlaneContext(p,room.cord.bundle,roomControls.get(room.communityId||room.naddr)||[],room);
      const pooled=plane?null:R.subscribe(filters,{onEvent,live:true}),
        external=plane?cordPlaneSubscribe(p,R,urls,filters,{onEvent,timeout:0,live:true},plane):R.subscribeFrom(urls,filters,{onEvent,timeout:0,live:true});
      const close=()=>{try{R.close(pooled);}catch(_){}try{external();}catch(_){}};
      chatSub={close,pooled,external};
      const gates=[];
      if(pooled&&R.waitForSubscription)gates.push(R.waitForSubscription(pooled,urls).then(ok=>{if(!ok)throw new Error('managed room relay did not open');}));
      if(external.hasTargets&&external.ready)gates.push(external.ready.then(ok=>{if(!ok)throw new Error('external room relay did not open');}));
      /* WARN, NEVER TEAR DOWN. Neither gate can prove a subscription is dead: the managed
       * pool legitimately does not carry a room's own invite relays (so waitForSubscription
       * times out at 8s on a live subscription), and external.ready only reports what this
       * browser managed to dial. Closing on that killed the live stream a few seconds after
       * every room open, and because stopChatLive() clears chatSubKey the 4s tick re-armed it
       * — reopening sockets to up to eight relays, waiting 8s, and tearing them down again,
       * for as long as the room was on screen. Unconfirmed simply falls back to the history
       * refresh, which is what carried the room before these gates existed. */
      if(gates.length)void Promise.any(gates).catch(e=>{if(chatSubKey===key)console.warn('Concord live room subscription is unconfirmed; leaving it open',e);});
    }catch(e){ chatSubKey='';console.warn('Concord live room subscription failed',e); }
  }
  /* TYPING (kind 23311 in an ephemeral 21059, Vector's send_typing). Received: who, until when,
   * painted into one line under the messages without repainting the pane. Sent: at most every 4s
   * while the composer has text — and NOT from a remote signer (NIP-46/55/07), where every one of
   * these would be a signing round trip or a prompt for something nobody asked to sign. */
  const typers=new Map();let typingTimer=null,typingSentAt=0;
  async function noteTyping(p,room,channel,ev){
    try{ const r=window.PosterCordReader,bundle=room&&room.cord&&room.cord.bundle; if(!r||!r.inspectTyping||!bundle)return;
      const me=p.viewer?p.viewer().pubkey:'',who=await r.inspectTyping(bundle,roomControls.get(room.communityId||room.naddr)||[],channel.id,[ev]);
      for(const pk of who)if(pk!==me)typers.set(channel.id+'\n'+pk,Date.now()+6000);
      paintTyping(p);
    }catch(_){}
  }
  function typingNames(p,channelId){
    const now=Date.now(),out=[];
    for(const [k,until] of [...typers]){ if(until<now){typers.delete(k);continue;} const [cid,pk]=k.split('\n'); if(cid!==channelId)continue; const pr=p.profOf?p.profOf(pk)||{}:{}; out.push(pr.display_name||pr.name||pk.slice(0,10)); }
    return out;
  }
  function typingText(names){ return !names.length?'':names.length===1?names[0]+' is typing…':names.length===2?names[0]+' and '+names[1]+' are typing…':'Several people are typing…'; }
  function paintTyping(p){
    const room=saved()[state.community],id=channelIdOf(room,state.channel),el=typeof document.querySelector==='function'?document.querySelector('#cc-typing'):null; if(!el||!el.classList)return;
    const text=typingText(id?typingNames(p,id):[]); el.textContent=text; el.classList.toggle('on',!!text);
    clearTimeout(typingTimer); if(text)typingTimer=setTimeout(()=>paintTyping(p),1500);
  }
  function sendTyping(p){
    const room=saved()[state.community],viewer=p.viewer?p.viewer():{}; if(!room||!room.cord||room.local||room.protocol==='nip29'||viewer.mode!=='local')return;
    if(Date.now()-typingSentAt<4000)return; typingSentAt=Date.now();
    const r=window.PosterCordReader,bundle=room.cord.bundle,id=channelIdOf(room,state.channel); if(!r||!r.createTypingWrap||!id)return;
    void (async()=>{ try{ const wraps=roomControls.get(room.communityId||room.naddr)||[],made=await r.createTypingWrap(bundle,wraps,id,viewer.pubkey,p.signTemplate),relays=roomRelays(bundle),scope=cordPlaneContext(p,bundle,wraps,room);
      await p.relayPublishRoom(relays,made.wrap,cordPlaneAuth(p,scope,made.wrap.pubkey,relays)); }catch(_){} })();
  }
  async function flushChatLive(p,key){
    const wraps=chatBuffer;chatBuffer=[];
    if(!wraps.length||chatSubKey!==key)return;
    const rooms=saved(),room=rooms[state.community];
    const channel=room&&(room.channels||[]).find(c=>c.name===(state.channel||'general'));
    if(!room||!channel)return;
    /* THE VIEW MOVED WHILE THOSE BYTES WERE IN FLIGHT. Without this the events of the channel you
     * just left are merged into the store of the one you just opened. */
    if(chatLiveKey(p,room,channel)!==key)return;
    if(room.protocol==='nip29'){
      const storeId=channelStoreId(room,channel.name),owner=deliveryOwner(p),identity=roomIdentity(room),
        needsContext=wraps.some(ev=>[5,7].includes(Number(ev.kind)));
      let merged;
      try{merged=needsContext?await readNip29Live(p,room,wraps):mergeNip29Live(p,room,wraps);}
      catch(e){
        console.warn('Concord NIP-29 history refresh failed; retaining received events',e);
        if(deliveryOwner(p)!==owner||!saved().some(r=>roomIdentity(r)===identity))return;
        merged=mergeNip29Live(p,room,[]);
      }
      if(!merged)return;
      const prior=testMessages(storeId);
      if(JSON.stringify(merged)!==JSON.stringify(prior)){saveTestMessages(storeId,merged);if(document.body.classList.contains('concord-view'))preserveChatScroll(()=>backgroundRender());}
      return;
    }
    const bundle=room&&room.cord&&room.cord.bundle,reader=window.PosterCordReader;
    if(!bundle||!reader)return;
    const loadKey=room.communityId||room.naddr,controlWraps=roomControls.get(loadKey);
    if(!controlWraps)return;
    const storeId=channelStoreId(room,channel.name);
    try{
      await cacheEnvelopes(envelopeCacheKey(loadKey,channel.id),wraps);
      await absorbChatWraps(p,reader,bundle,controlWraps,room,channel,wraps,storeId);
    }catch(e){ console.warn('Concord live message failed',e); }
  }
  function mergeCordTimeline(prior,opened,p,scope){
    const owner=deliveryOwner(p);
    if(mergeCordTimeline.owner!==owner){mergeCordTimeline.owner=owner;mergeCordTimeline.deleted=new Map();}
    /* THE READER'S VERDICT IS THE AUTHORITY ON WHO MAY DELETE WHAT.
     *
     * This file re-derives deletion from `opened.deletions` — "a signed delete must match the
     * message's own author" — which is the SELF-delete rule and nothing else. It is applied to
     * messages already on screen, so it overrode the fold: a moderator's deletion removed the
     * message from `opened.messages` and this merge put it straight back from `prior`.
     *
     * `deletedMessageIds` is what `inspectChat` concluded after applying CORD's MANAGE_MESSAGES
     * rule (permission + outranks, owner always). Honouring it is what makes a room moderatable;
     * the author rule below stays exactly as it was, so nothing a person could delete before
     * stops being deletable. */
    prior=uniqueMessages(prior).filter(m=>m.kind!==1740||!Array.isArray(opened.metadataManagers)||opened.metadataManagers.includes(m.pubkey));
    const moderated=new Set(opened.deletedMessageIds||[]);
    const tombstones=mergeCordTimeline.deleted,deletes=new Map((opened.deletions||[]).map(([id,authors])=>[id,new Set(authors)])),old=new Map(prior.map(m=>[messageId(m),m]));
    const incoming=(opened.messages||[]).map(m=>{const pr=p.profOf?p.profOf(m.pubkey):{},before=old.get(m.id),edited=tags=>Number(((tags||[]).find(t=>t[0]==='edited')||[])[1]||0),keepEdit=before&&edited(before.tags)>edited(m.tags);return {id:m.id,pubkey:m.pubkey,by:pr.display_name||pr.name||m.pubkey.slice(0,12)+'…',text:keepEdit?before.text:m.text,at:m.at,kind:m.kind,tags:keepEdit?before.tags:m.tags||[],editedExpires:keepEdit?before.editedExpires:m.editedExpires,originalText:keepEdit?before.originalText:m.originalText,remote:true};});
    const merged=mergeRelayMessages(prior.map(m=>({...m})),incoming),byId=new Map(merged.map(m=>[messageId(m),m]));
    for(const [id,m] of byId){
      const key=scope+'\n'+id;
      if(messageExpired(m)){byId.delete(id);continue;}
      /* A moderated id is remembered under the ACTOR that removed it, not the author — the author
         never signed anything. Recorded in the same tombstone map so an incremental read that no
         longer carries the deletion (a bounded history page) cannot resurrect it. */
      if(moderated.has(id)){tombstones.set(key,'@mod');byId.delete(id);continue;}
      if(deletes.get(id)?.has(m.pubkey)||tombstones.get(key)===m.pubkey||tombstones.get(key)==='@mod'){
        tombstones.set(key,tombstones.get(key)==='@mod'?'@mod':m.pubkey);byId.delete(id);}
    }
    while(tombstones.size>5000)tombstones.delete(tombstones.keys().next().value);
    const reactionExpirations=new Map(opened.reactionExpirations||[]),reactionTimes=new Map(opened.reactionTimes||[]),reactionIds=new Map(opened.reactionIds||[]),urls=new Map(opened.reactionUrls||[]);
    for(const [target,groups] of opened.reactions||[]){const m=byId.get(target);if(!m)continue;
      m.reactions={...(m.reactions||{})};m.reactionExpirations={...(m.reactionExpirations||{})};m.reactionIds={...(m.reactionIds||{})};m.reactionTimes={...(m.reactionTimes||{})};m.reactionUrls={...(m.reactionUrls||{})};
      for(const [emoji,people] of groups){m.reactions[emoji]=[...new Set([...(m.reactions[emoji]||[]),...people])];}
      for(const [emoji,entries] of reactionIds.get(target)||[]){const ids={...(m.reactionIds[emoji]||{})};for(const [pubkey,id] of entries){const previous=ids[pubkey],at=reactionTimes.get(id)||0;if(!previous||at>=(m.reactionTimes[previous]||0)){ids[pubkey]=id;m.reactionTimes[id]=at;if(reactionExpirations.has(id))m.reactionExpirations[id]=reactionExpirations.get(id);}}m.reactionIds[emoji]=ids;}
      for(const [emoji,url] of urls.get(target)||[])m.reactionUrls[emoji]=url;
    }
    // Absence from a bounded history page is not a removal. A signed delete must match the
    // stored reaction author and exact rumor ID before removing that person's contribution.
    for(const m of byId.values()){
      m.reactions={...(m.reactions||{})};m.reactionIds={...(m.reactionIds||{})};m.reactionUrls={...(m.reactionUrls||{})};
      for(const [emoji,entries] of Object.entries(m.reactionIds)){const ids={...entries};for(const [pubkey,id] of Object.entries(ids)){
        const key=scope+'\n'+id;if(deletes.get(id)?.has(pubkey)||tombstones.get(key)===pubkey){tombstones.set(key,pubkey);delete ids[pubkey];m.reactions[emoji]=(m.reactions[emoji]||[]).filter(pk=>pk!==pubkey);}
      }m.reactionIds[emoji]=ids;if(!(m.reactions[emoji]||[]).length){delete m.reactions[emoji];delete m.reactionIds[emoji];delete m.reactionUrls[emoji];}}
    }
    while(tombstones.size>5000)tombstones.delete(tombstones.keys().next().value);
    for(const [target,list] of opened.pollVotes||[]){const m=byId.get(target);if(!m)continue;const votes=new Map((m.votes||[]).map(v=>[v.pubkey,v]));for(const v of list)if(!votes.has(v.pubkey)||Number(v.ms)>=Number(votes.get(v.pubkey).ms))votes.set(v.pubkey,v);m.votes=[...votes.values()];}
    const claims=new Map();for(const [target,m] of byId)for(const z of m.zaps||[])claims.set(z.id,{target,z});
    for(const [target,list] of opened.zaps||[])if(byId.has(target))for(const z of list)claims.set(z.id,{target,z});
    const paid=new Set();for(const m of byId.values())m.zaps=[];
    for(const {target,z} of [...claims.values()].sort((a,b)=>Number(a.z.ms||0)-Number(b.z.ms||0)||String(a.z.id).localeCompare(String(b.z.id)))){
      const proof=z.paymentId||z.id;if(paid.has(proof))continue;paid.add(proof);byId.get(target).zaps.push(z);
    }
    for(const m of byId.values()){const parent=byId.get(wireReplyParentId(m));if(parent)m.reply={id:parent.id,by:parent.by,text:parent.text,expires:(parent.tags||[]).find(t=>t[0]==='expiration')?.[1]};}
    return [...byId.values()].sort((a,b)=>Number(a.at)-Number(b.at));
  }
  /* WHICH MESSAGE THIS ONE ANSWERS. A thread reply (kind 1111) names its parent in lowercase `e`
   * (CORD-03 §3). Vector's everyday reply is a plain kind-9 message carrying a NIP-C7 `q` tag
   * (["q", parent, relay, author]) -- read as nothing, every Vector reply showed up here as a
   * message out of nowhere. Pure, for tests/client/concord_vector_interop_runtime.mjs. */
  function wireReplyParentId(m){
    const tags=(m&&m.tags)||[];
    if(m&&m.kind===1111)return (tags.find(t=>t[0]==='e')||[])[1]||'';
    if(m&&m.kind===9)return (tags.find(t=>t[0]==='q'&&/^[0-9a-f]{64}$/i.test(String(t[1]||'')))||[])[1]||'';
    return '';
  }
  async function absorbChatWraps(p,reader,bundle,controlWraps,room,channel,wraps,storeId){
    const owner=deliveryOwner(p),roomId=roomIdentity(room);
    // Serialize folds of one store: an older decrypt must not replace newer reaction state.
    const pending=absorbChatWraps.pending||(absorbChatWraps.pending=new Map()),key=owner+'\n'+storeId;
    const run=async()=>{
    if(deliveryOwner(p)!==owner||!saved().some(r=>roomIdentity(r)===roomId))return;
    // History plus incremental events supplies the author context for reactions, edits and deletes.
    // The reader memoizes successful decrypts; replaying this bounded encrypted cache does not
    // repeat signature/decryption work for every event on each live batch.
    const cached=await cachedEnvelopes(envelopeCacheKey(room.communityId||room.naddr,channel.id));
    if(deliveryOwner(p)!==owner||!saved().some(r=>roomIdentity(r)===roomId))return;
    // Keep the same bounded envelope context when IndexedDB is unavailable. Only encrypted
    // wraps live here, and an account transition discards the previous owner's context.
    if(absorbChatWraps.historyOwner!==owner){absorbChatWraps.historyOwner=owner;absorbChatWraps.history=new Map();}
    const history=absorbChatWraps.history;
    for(const ev of [...cached,...(wraps||[])])history.set(key+'\n'+ev.id,{key,ev});
    while(history.size>5000)history.delete(history.keys().next().value); // existing per-stream cache bound
    const complete=[...history.values()].filter(item=>item.key===key).map(item=>item.ev);
    const opened=await readChat(p,reader,bundle,controlWraps,room,channel,complete);
    if(deliveryOwner(p)!==owner||!saved().some(r=>roomIdentity(r)===roomId))return;
    // Another live batch or history refresh may have committed while decryption was pending.
    // Merge into the current store after the await, so late completion cannot erase newer arrivals.
    const prior=testMessages(storeId),next=mergeCordTimeline(prior,opened,p,key);
    if(JSON.stringify(next)!==JSON.stringify(prior)){const viewer=p.viewer?p.viewer():{},profile=viewer.profile||{},me=profile.display_name||profile.name||(viewer.npub?viewer.npub.slice(0,12)+'…':'You');notifyMentions(p,room,next,viewer,me,channel.name);saveTestMessages(storeId,next);paintUnreadBadge();if(document.body.classList.contains('concord-view'))whenHandLeaves(()=>{if(document.body.classList.contains('concord-view'))preserveChatScroll(()=>backgroundRender());});}

    };
    const task=(pending.get(key)||Promise.resolve()).then(run,run);pending.set(key,task);
    try{await task;}finally{if(pending.get(key)===task)pending.delete(key);}
  }
  /* THE OTHER COMMUNITIES, TOO. Only the channel on screen was ever fetched live, so a community you
   * were not looking at could not know it had new posts: the rail's unread glow and the Communities
   * badge are computed from messages this device HOLDS, and nothing fetched those ("on desktop, I
   * didn't see any way to know if a community has new posts"). A slow sweep asks each other joined
   * room's channels for what arrived since the newest message held — one room at a time, at most once
   * a minute per room (the same minInterval as the live tick), through the SAME merge the live tick
   * uses, so a message found here is exactly the message the channel will show when opened. */
  /* PUSHED, NOT POLLED. Every other joined room's channels are SUBSCRIBED — the same kind of
   * subscription the open channel has — so a new post in any community reaches this device the
   * moment a relay has it and lights that community's icon. ("90s? seems too long": a sweep that
   * visits one room at a time is only the catch-up for what arrived while this device was away.)
   * One subscription per room, keyed on the account and the exact set of channels and relays, so it
   * is re-armed only when membership changes. An event is filed by its AUTHOR: every channel has its
   * own stream keys, and those keys are what the filter asked for. */
  let roomsLive=new Map();
  /* A ROOM WHOSE PLANE KEY THIS MEMBERSHIP DOES NOT HOLD IS REFUSED ONCE PER MEMBERSHIP STATE, NOT
   * EVERY TICK. startRoomsLive runs on every 4 s live tick and only remembered SUCCESSES, so a room it
   * could not subscribe to was rebuilt from scratch on every tick for the life of the page -- the
   * control fold, the key search and a console.warn carrying the Error each time. Measured on a
   * PosterChanOS desktop: 18,502 of the last 20,000 log lines were that one warning, two rooms twice
   * every few seconds for nine hours, in a renderer that had grown to 4-7 GB and stopped responding.
   * The failure is keyed on the SAME key a subscription is (owner, relays, channels and their stream
   * keys), so a membership that changes -- a rekey, a new grant -- is tried again at once; otherwise
   * it is retried at most every ROOM_LIVE_RETRY_MS. */
  const roomsLiveFailed=new Map(), ROOM_LIVE_RETRY_MS=600000;
  /* A ROOM WHOSE KEYS ARE NOT LOADED YET IS NOT A FAILED ROOM. This loop runs from the first tick after
   * start, usually before a room's control envelopes are back -- and with no controls every stream author
   * throws "Concord plane key is not held by this membership". That was recorded as a FAILURE, and a failure
   * is only retried after ROOM_LIVE_RETRY_MS (10 minutes), so the keys arriving a second later did nothing:
   * the room took no live messages for ten minutes ("laptop concord behind, not updating room", "more
   * concord plane key errors", 2026-10-15). Now a room without controls waits silently -- reading the cached
   * controls meanwhile, in their own map so nothing else treats a cache read as a refresh -- and the
   * subscription key carries the controls it was built with, so new keys are tried at once. */
  const liveCachedControls=new Map(), liveControlReads=new Set();
  function liveControls(loadKey){
    const held=roomControls.get(loadKey);
    if(held&&held.length)return held;
    const cached=liveCachedControls.get(loadKey);
    if(cached&&cached.length)return cached;
    if(!liveControlReads.has(loadKey)&&typeof cachedEnvelopes==='function'){
      liveControlReads.add(loadKey);
      Promise.resolve().then(()=>cachedEnvelopes(envelopeCacheKey(loadKey,'control')))
        .then(w=>{ if(w&&w.length)liveCachedControls.set(loadKey,w); },()=>{});
    }
    return null;
  }
  function startRoomsLive(p){
    try{
      const R=(window.PCConcordRelay||window.Relay),reader=window.PosterCordReader;
      if(!p||!R||!R.subscribeFrom||!reader)return;
      const owner=deliveryOwner(p),want=new Map();
      for(const room of saved()){
        const bundle=room&&room.cord&&room.cord.bundle;
        if(!bundle||bundle.dissolved||bundle.removed||room.protocol==='nip29')continue;
        const channels=channelsOf(room).filter(c=>c&&c.streamPubkeys&&c.streamPubkeys.length);
        if(!channels.length)continue;
        const controls=liveControls(room.communityId||room.naddr);
        if(!controls)continue;            // keys not loaded yet: not a failure, try again next tick
        const relays=roomRelays(bundle),key=[owner,roomIdentity(room),relays.join(','),channels.map(c=>c.id+':'+c.streamPubkeys.join('.')).join('|'),cordControlStamp(controls).join(',')].join('\n');
        want.set(roomIdentity(room),{key,room,bundle,channels,relays,controls});
      }
      for(const [id,sub] of roomsLive){ const w=want.get(id); if(!w||w.key!==sub.key){ try{ sub.close(); }catch(_){ } roomsLive.delete(id); } }
      for(const id of [...roomsLiveFailed.keys()])if(!want.has(id))roomsLiveFailed.delete(id);
      for(const [id,w] of want){
        if(roomsLive.has(id))continue;
        const failed=roomsLiveFailed.get(id);
        if(failed&&failed.key===w.key&&Date.now()-failed.at<ROOM_LIVE_RETRY_MS)continue;
        const byAuthor=new Map();
        for(const c of w.channels)for(const pk of c.streamPubkeys)byAuthor.set(String(pk).toLowerCase(),c);
        const buffers=new Map();
        const flush=async(channel)=>{
          const wraps=buffers.get(channel.id)||[];buffers.delete(channel.id);if(!wraps.length)return;
          const room=saved().find(r=>roomIdentity(r)===id);if(!room||deliveryOwner(p)!==owner)return;
          const loadKey=room.communityId||room.naddr;
          let controls=roomControls.get(loadKey);
          if(!controls){ try{ controls=await cachedEnvelopes(envelopeCacheKey(loadKey,'control')); }catch(_){ controls=null; } }
          if(!controls||!controls.length)return;
          try{ await cacheEnvelopes(envelopeCacheKey(loadKey,channel.id),wraps); }catch(_){ }
          await absorbChatWraps(p,reader,room.cord.bundle,controls,room,channel,wraps,channelStoreId(room,channel.name)).catch(()=>{});
          try{ paintUnreadBadge(); }catch(_){ }
          if(document.body.classList.contains('concord-view'))preserveChatScroll(()=>backgroundRender());
        };
        const onEvent=ev=>{
          if(!ev||Number(ev.kind)!==1059)return;
          const channel=byAuthor.get(String(ev.pubkey||'').toLowerCase());if(!channel)return;
          const list=buffers.get(channel.id)||[];list.push(ev);buffers.set(channel.id,list);
          if(list.length===1)setTimeout(()=>{ void flush(channel); },500);
        };
        const authors=[...byAuthor.keys()],since=Math.floor(Date.now()/1000)-60,filters=[{kinds:[1059],authors,since}];
        try{
          const plane=reader.createPlaneAuth&&cordPlaneContext(p,w.bundle,w.controls,w.room);
          const close=plane?cordPlaneSubscribe(p,R,w.relays,filters,{onEvent,timeout:0,live:true},plane):R.subscribeFrom(w.relays,filters,{onEvent,timeout:0,live:true});
          roomsLive.set(id,{key:w.key,close:()=>{ try{ close(); }catch(_){ } }});
          roomsLiveFailed.delete(id);
        }catch(e){
          if(!failed||failed.key!==w.key)console.warn('Concord room subscription failed',String(e&&e.message||e));
          roomsLiveFailed.set(id,{key:w.key,at:Date.now()});
        }
      }
    }catch(_){ }
  }
  let memberQuery='',memberFocus=null;
  /* RIGHT-CLICK A NAME IN A MESSAGE -> the same member menu the Known members list opens. Messages are
   * repainted in place (patchMessageList) without a fresh bind(), so the listener is ONE delegated
   * contextmenu on the document, and bind() hands it the current room's menu. */
  let openMemberMenuNow=null,authorMenuWired=false;
  function wireAuthorMenu(){
    if(authorMenuWired||typeof document==='undefined'||typeof document.addEventListener!=='function')return;
    authorMenuWired=true;
    document.addEventListener('contextmenu',e=>{
      const el=e.target&&e.target.closest&&e.target.closest('.cc-message [data-cc-author]');
      if(!el||!el.dataset.ccAuthor||typeof openMemberMenuNow!=='function')return;
      e.preventDefault();openMemberMenuNow(e,el.dataset.ccAuthor);
    });
  }
  function applyMemberSearch(){ try{ applyMemberSearchNow(); }catch(_){ } }   // a search box must never cost a render
  function applyMemberSearchNow(){
    if(typeof document==='undefined'||typeof document.querySelectorAll!=='function')return;
    const q=String(memberQuery||'').trim().toLowerCase(), terms=q.split(/\s+/).filter(Boolean);
    document.querySelectorAll('.cc-members-scroll, .cc-member-list').forEach(list=>{
      let shown=0;
      list.querySelectorAll('.cc-member').forEach(row=>{ const hit=!terms.length||terms.every(t=>(row.dataset.q||'').includes(t)); row.classList.toggle('hidden',!hit); if(hit)shown++; });
      let none=list.querySelector('.cc-member-none');
      if(terms.length&&!shown){ if(!none){ none=document.createElement('div'); none.className='cc-empty-side cc-member-none'; list.appendChild(none); } none.textContent='No member matches “'+memberQuery.trim()+'”.'; }
      else if(none)none.remove();
    });
  }
  let sweepBusy=false,sweepAt=0,sweepNext=0;
  async function sweepOtherRooms(p){
    try{ startReadSync(p); refreshReadDoc(p,false).catch(()=>{}); }catch(_){ }
    if(sweepBusy||Date.now()-sweepAt<SWEEP_MS)return;
    sweepBusy=true;sweepAt=Date.now();let stored=false;
    try{
      const reader=window.PosterCordReader,rooms=saved();if(!reader||!rooms.length)return;
      const owner=deliveryOwner(p);
      for(let k=0;k<rooms.length;k++){
        const i=(sweepNext+k)%rooms.length,room=rooms[i],bundle=room&&room.cord&&room.cord.bundle;
        if(!bundle||bundle.dissolved||room.protocol==='nip29')continue;
        const loadKey=room.communityId||room.naddr;
        let controlWraps=roomControls.get(loadKey);
        if(!controlWraps){ try{ controlWraps=await cachedEnvelopes(envelopeCacheKey(loadKey,'control')); }catch(_){ controlWraps=null; } }
        if(!controlWraps||!controlWraps.length)continue;
        const relays=roomRelays(bundle),identity=roomIdentity(room);
        for(const channel of channelsOf(room).slice(0,SWEEP_CHANNELS)){
          if(!channel||!channel.streamPubkeys||!channel.streamPubkeys.length)continue;
          if(i===state.community&&channel.name===(state.channel||'general'))continue;   // the live tick owns that one
          if(deliveryOwner(p)!==owner)return;
          const storeId=channelStoreId(room,channel.name),prior=testMessages(storeId),
            newest=prior.reduce((n,m)=>Math.max(n,Number(m.at)||0),0),
            since=newest?Math.max(0,Math.floor((newest-60000)/1000)):Math.floor(Date.now()/1000)-7*86400;
          const wraps=await cordQuery(p,relays,[{kinds:[1059],authors:channel.streamPubkeys,since,limit:100}],
            {timeout:6000,max:8,plane:cordPlaneContext(p,bundle,controlWraps,room),signal:ownRoomReads(identity),
             purpose:'concord room sweep '+loadKey+' '+channel.id,minInterval:60000}).catch(()=>[]);
          if(!wraps||!wraps.length)continue;
          try{ await cacheEnvelopes(envelopeCacheKey(loadKey,channel.id),wraps); }catch(_){ }
          await absorbChatWraps(p,reader,bundle,controlWraps,room,channel,wraps,storeId).catch(()=>{});
          if(sweepSignature(testMessages(storeId))!==sweepSignature(prior))stored=true;
        }
        sweepNext=(i+1)%rooms.length;
        break;                                    // one room per sweep: the next sweep takes the next room
      }
      try{ paintUnreadBadge(); }catch(_){ }
      /* ONLY WHEN THE SWEEP STORED SOMETHING. This repaint was unconditional, so every 20 s -- with
       * nothing new anywhere -- the whole workspace was rebuilt: all message cards, the rail, the
       * composer, every handler. Measured at 4x CPU throttle on a 200-message room: a 170-190 ms long
       * task, and it is what turned an open drawer or menu into a rebuilt one mid-tap ("going
       * through the menus is clunky"). The badge above is cheap and stays unconditional. */
      if(stored&&document.body.classList.contains('concord-view'))preserveChatScroll(()=>backgroundRender());
    }catch(_){ }finally{ sweepBusy=false; }
  }
  function sweepSignature(rows){ const last=rows&&rows.length?rows[rows.length-1]:null; return (rows?rows.length:0)+':'+(last?messageId(last)+':'+(Number(last.at)||0):''); }
  const SWEEP_MS=20000, SWEEP_CHANNELS=6;
  /* Before Communities has ever been opened in this page the live timer is not running, and the
   * sidebar's Communities badge would still know nothing. The sweep runs on its own then — only for a
   * signed-in account, and never alongside the live timer, which calls it itself. */
  /* ONE PAGE PER DESK SWEEPS. On PosterChanOS every app window is its own copy of the client, and each ran this sweep --
   * a live subscription to every relay of every community, through this account's tunnel share. Nine windows (Notes,
   * Mail, Texts, Terminal, ...) held 4-10 tunnels each, filled the 128-tunnel share, and every other device of the
   * account was refused: "0 of 5 of this community's relays answered", no community icons or banners on the laptop and
   * in Firefox (2026-10-16, measured per window over a minute). A window that is not Communities leaves it to the
   * desktop page (whose badge it is) and to the Communities window. */
  function sweepHere(){ try{ const W=window.PCOSWin; return !(W&&W.isWindow&&W.isWindow()&&W.viewOf&&W.viewOf()!=='concord'); }catch(_){ return true; } }
  if(typeof setInterval==='function'){ const sweepTimer=setInterval(()=>{ try{ const p=PC(); if(document.hidden||liveResting())return; if(!liveTimer&&sweepHere()&&p&&p.viewer&&p.viewer()&&p.viewer().pubkey&&saved().length){ startRoomsLive(p); sweepOtherRooms(p); } }catch(_){ } },SWEEP_MS); try{ if(sweepTimer&&sweepTimer.unref)sweepTimer.unref(); }catch(_){ } }   // never keep a process alive
  async function refreshActiveChannel(p){
    const foreground=document.body.classList.contains('concord-view'),parked=window.PCOS&&PCOS.isOn&&PCOS.isOn()&&PCOS.parkedSlot&&PCOS.parkedSlot('concord');
    if(liveBusy||state.community==null||(!foreground&&!parked))return; liveBusy=true;
    try{
      const rooms=saved(),room=rooms[state.community],channel=room&&(room.channels||[]).find(c=>c.name===(state.channel||'general')),
        bundle=room&&room.cord&&room.cord.bundle,reader=window.PosterCordReader;
      if(!room||!channel||!bundle||!reader||bundle.dissolved)return;
      const owner=deliveryOwner(p),identity=roomIdentity(room),
        stillOwned=()=>deliveryOwner(p)===owner&&saved().some(r=>roomIdentity(r)===identity);
      const loadKey=room.communityId||room.naddr,controlWraps=roomControls.get(loadKey);
      if(!controlWraps)return;
      // Repair transport ownership before a slow/failed history read can hold the room deaf.
      startChatLive(p,room,channel);
      const relays=roomRelays(bundle),storeId=channelStoreId(room,channel.name),prior=testMessages(storeId),
        since=Math.max(0,Math.floor((prior.reduce((n,m)=>Math.max(n,Number(m.at)||0),0)-60000)/1000)),
        reach={},
        wraps=await cordQuery(p,relays,[{kinds:[1059],authors:channel.streamPubkeys,since,limit:500}],
          {timeout:6000,max:8,plane:cordPlaneContext(p,bundle,controlWraps,room),signal:ownRoomReads(identity),purpose:'concord room live '+loadKey,minInterval:60000,report:reach});
      /* The live tick is what runs for a channel opened after hydration prefetched past it, so it
       * owns the verdict for that channel just as much as the first load does. A tick that was
       * rate-limited off every relay records `skipped`, which is exactly the honest answer. */
      noteChannelReach(storeId,reach,relays);
      if(!stillOwned())return;
      await cacheEnvelopes(envelopeCacheKey(loadKey,channel.id),wraps);
      if(!stillOwned())return;
      // A completed old-room refresh can fill its own history, but cannot replace the current live stream.
      const active=saved()[state.community],activeChannel=active&&(active.channels||[]).find(c=>c.name===(state.channel||'general'));
      if(roomIdentity(active)===identity&&activeChannel&&activeChannel.id===channel.id)startChatLive(p,active,activeChannel);
      await absorbChatWraps(p,reader,bundle,controlWraps,room,channel,wraps||[],storeId);
    }catch(e){
      /* SAY IT ONCE PER CHANNEL, NOT EVERY FOUR SECONDS.
       *
       * This tick runs on a timer, so a condition that persists — a control stream the relay has
       * not answered for yet, which on a phone is ordinary — printed the same line for ever and
       * filled the console. Reported from Android as a wall of "channel is not readable with this
       * membership". The condition is worth reporting; repeating it is not, and a log nobody can
       * read is a log nobody reads when something new goes wrong.
       *
       * Cleared by a successful read, so a channel that recovers reports again if it breaks again,
       * and a DIFFERENT failure on the same channel is still printed the first time it happens. */
      const _why=String((e&&e.message)||e);
      const _seen=roomIdentity(saved()[state.community])+'\n'+(state.channel||'general')+'\n'+_why;
      if(liveWarned!==_seen){ liveWarned=_seen; console.warn('Concord live sync failed',e); }
    }finally{liveBusy=false;}
  }
  // CORD06 subscriptions are owned by an account AND an exact membership snapshot.
  // Decryption and persistence may outlive navigation; neither may cross that boundary.
  function reviewRefoundingRecipients(p,room,removed=[]){
    return new Promise(resolve=>{
      const overlay=document.createElement('div'),card=document.createElement('div');overlay.className='cc-join';card.className='cc-join-card';card.setAttribute('role','dialog');card.setAttribute('aria-modal','true');card.setAttribute('aria-label','Review community access');overlay.append(card);
      const title=document.createElement('h2');title.textContent='Review community access';card.append(title);
      const explanation=document.createElement('p');explanation.textContent='Everyone left out will lose future access. The known member list may be incomplete. Add any missing members, and check each private channel separately. Enter public npub addresses, separated by spaces or new lines.';card.append(explanation);
      const owner=room.cord.bundle.owner,known=roomParticipants(room,owner).filter(pk=>!removed.includes(pk)),scopes=[{id:'0'.repeat(64),label:'Keep community access',members:[...new Set([owner,...known])]},...(room.cord.bundle.channels||[]).map(ch=>({id:ch.id,label:'Keep access to #'+(ch.name||ch.id.slice(0,8)),members:[owner]}))],fields=new Map();
      for(const scope of scopes){const label=document.createElement('label'),input=document.createElement('textarea');label.className='cc-label';label.textContent=scope.label;input.className='input';input.rows=3;input.setAttribute('aria-label',scope.label);input.value=scope.members.map(pk=>window.NostrTools.nip19.npubEncode(pk)).join('\n');label.append(input);card.append(label);fields.set(scope.id,input);}
      const acknowledgment=document.createElement('label'),checked=document.createElement('input');checked.type='checkbox';acknowledgment.append(checked,document.createTextNode(' I checked that every retained member is included.'));card.append(acknowledgment);
      const error=document.createElement('p');error.setAttribute('role','alert');card.append(error);
      const actions=document.createElement('div');actions.className='cc-join-actions';const cancel=document.createElement('button'),confirm=document.createElement('button');cancel.className='btn btn-ghost';cancel.textContent='Cancel';confirm.className='btn btn-primary';confirm.textContent='Rotate keys';confirm.disabled=true;checked.onchange=()=>{confirm.disabled=!checked.checked;};actions.append(cancel,confirm);card.append(actions);
      const previous=document.activeElement,finish=value=>{overlay.remove();previous?.focus?.();resolve(value);};cancel.onclick=()=>finish(null);overlay.onkeydown=e=>{if(e.key==='Escape'){e.preventDefault();finish(null);}else if(e.key==='Tab'){const items=[...card.querySelectorAll('textarea,input,button')].filter(n=>!n.disabled),first=items[0],last=items[items.length-1];if(e.shiftKey&&document.activeElement===first){e.preventDefault();last.focus();}else if(!e.shiftKey&&document.activeElement===last){e.preventDefault();first.focus();}}};
      confirm.onclick=()=>{try{
        const parse=input=>[...new Set(input.value.trim().split(/[\s,]+/).filter(Boolean).map(value=>{if(/^[0-9a-f]{64}$/i.test(value))return value.toLowerCase();const decoded=window.NostrTools.nip19.decode(value);if(decoded.type!=='npub')throw new Error('Use public npub addresses only.');return decoded.data;}))].map(pubkey=>({pubkey}));
        const recipients=parse(fields.get('0'.repeat(64))),channelRecipients={};if(!recipients.some(r=>r.pubkey===owner))throw new Error('The owner must keep access.');
        for(const scope of scopes.slice(1))channelRecipients[scope.id]=parse(fields.get(scope.id));
        if(recipients.some(r=>removed.includes(r.pubkey)))throw new Error('The removed member cannot keep access.');
        finish({recipients,channelRecipients,removed});
      }catch(e){error.textContent=e.message||'Check the member addresses.';}};
      document.body.append(overlay);fields.values().next().value?.focus();
    });
  }
  /* Can this signer rotate community keys? Rotation needs NIP-44 over raw bytes, which only a key held on this device
   * can do; a remote signer says so at once (no round trip). Asked by encrypting 40 zero bytes to ourselves. */
  async function canRotateKeys(p){
    const me=p.viewer?p.viewer().pubkey:'';if(!me||!p.cordRekeyEncrypt)return false;
    try{ await p.cordRekeyEncrypt(me,new Uint8Array(40)); return true; }catch(_){ return false; }
  }
  async function quickBanReview(p,room,target){
    const owner=room.cord.bundle.owner,keep=[...new Set([owner,...roomParticipants(room,owner)])].filter(pk=>pk&&pk!==target);
    const label=banLabel(target,p.profOf&&p.profOf(target),(()=>{try{return window.NostrTools.nip19.npubEncode(target);}catch(_){return '';}})());
    const ok=p.uiConfirm?await p.uiConfirm(`Ban ${label} from this community?`,{ok:'Ban',danger:true}):true;
    if(!ok)return null;
    // Each private channel keeps the members holding its channel role (and the owner) -- the same people who hold its key.
    const channelRecipients={},reader=window.PosterCordReader,loadKey=room.communityId||room.naddr;
    if((room.cord.bundle.channels||[]).length){
      const info=reader.inspectControl(room.cord.bundle,roomControls.get(loadKey)||[]),kept=new Set(keep);
      for(const ch of room.cord.bundle.channels){
        const roleIds=new Set((info.roles||[]).filter(r=>r.scope==='channel'&&r.channelId===ch.id).map(r=>r.id));
        const members=Object.entries(info.grants||{}).filter(([pk,ids])=>kept.has(pk)&&(ids||[]).some(id=>roleIds.has(id))).map(([pk])=>pk);
        channelRecipients[ch.id]=[...new Set([owner,...members])].map(pubkey=>({pubkey}));
      }
    }
    return {recipients:keep.map(pubkey=>({pubkey})),channelRecipients,removed:[target]};
  }
  function bindRefoundingControl(p,$){
    const saveButton=$('#cc-settings-save'),room=saved()[state.community],owner=deliveryOwner(p);
    if(!saveButton||!room?.cord?.bundle||room.cord.bundle.owner!==owner||room.cord.bundle.dissolved||$('#cc-refound'))return;
    const button=document.createElement('button');button.id='cc-refound';button.className='btn btn-ghost';button.textContent=room.cord.refounding?'Resume key rotation':'Rotate community keys';saveButton.parentNode.insertBefore(button,saveButton);
    button.onclick=async()=>{button.disabled=true;try{
      const options=room.cord.refounding?{}:await reviewRefoundingRecipients(p,room);if(!options)return;
      if(deliveryOwner(p)!==owner)throw new Error('The signing account changed during access review.');
      await refoundRoom(p,room,options);p.toast('Community keys rotated.');
    }catch(e){p.toast('Key rotation needs attention: '+(e.message||e));}finally{button.disabled=false;}};
  }

  async function acquireRefoundingControls(p,room,current){
    const reader=window.PosterCordReader,bundle=room.cord.bundle,relays=roomRelays(bundle),loadKey=room.communityId||room.naddr,
      cached=await cachedEnvelopes(envelopeCacheKey(loadKey,'control')),authors=reader.inspectControl(bundle,[]).controlPubkeys,all=new Map(cached.map(e=>[e.id,e]));
    for(const author of authors)for(const relay of relays){
      let until;const seen=new Set();
      for(let page=0;page<100;page++){
        if(!current())throw new Error('refounding account or membership changed');
        const report={},auth=reader.createPlaneAuth(bundle,cached,author,[relay]),filter={kinds:[1059],authors:[author],limit:1000,...(until===undefined?{}:{until})};
        const events=await p.relayQueryFrom([relay],[filter],{timeout:10000,max:1,exact:true,authScope:auth?{...auth,current}:null,report,minInterval:0,allowBlocked:false,purpose:'concord refounding history'});
        if(!current())throw new Error('refounding account or membership changed');
        if(!(report.ok||[]).map(normalizeRelay).includes(normalizeRelay(relay))||(report.failed||[]).length||(report.unheld||[]).length)throw new Error('every control relay must complete before refounding');
        let added=0;for(const ev of events||[])if(ev&&ev.pubkey===author&&ev.kind===1059){all.set(ev.id,ev);if(!seen.has(ev.id)){seen.add(ev.id);added++;}}
        if((events||[]).length<1000)break;
        const oldest=Math.min(...events.map(e=>Number(e.created_at)));
        // Inclusive pagination never skips same-second editions. A saturated
        // timestamp or a nonadvancing relay aborts instead of losing a head.
        if(!added||!Number.isFinite(oldest)||oldest<0||page===99)throw new Error('control history is too large or incomplete to refound safely');
        until=oldest;
      }
    }
    return [...all.values()];
  }
  async function refoundRoom(p,room,options){
    const reader=window.PosterCordReader,owner=deliveryOwner(p),identity=roomIdentity(room),initial=saved().find(r=>roomIdentity(r)===identity);
    if(!initial||!owner||!reader?.prepareRefounding||!p.cordRekeyEncrypt)throw new Error('refounding is unavailable for this signer');
    let plan=initial.cord.refounding,prior=plan?plan.prior:initial.cord.bundle;
    const current=()=>deliveryOwner(p)===owner&&saved().some(r=>roomIdentity(r)===identity&&r.cord?.bundle&&!r.cord.bundle.dissolved&&
      JSON.stringify({...r.cord.bundle,refounding_pending:undefined})===JSON.stringify({...prior,refounding_pending:undefined}));
    if(plan&&plan.actor!==owner)throw new Error('only the originating account can resume this refounding');
    if(!plan){
      let controls=await acquireRefoundingControls(p,initial,current);if(!current())throw new Error('refounding membership changed');
      if(options?.controlUpdates){if(!Array.isArray(options.controlUpdates)||options.controlUpdates.length>1000)throw new Error('invalid refounding control updates');controls=[...controls,...options.controlUpdates];}
      if(options?.banTarget){const ban=await reader.createBanWrap(prior,controls,options.banTarget,owner,p.signTemplate);controls=[...controls,ban.wrap];}
      plan=await reader.prepareRefounding(prior,controls,{...options,historyComplete:true},owner,p.signTemplate,p.cordRekeyEncrypt);
      if(!current())throw new Error('refounding membership changed while signing');
    }
    const relays=roomRelays(prior),loadKey=initial.communityId||initial.naddr;
    const persist=async checkpoint=>{
      if(!current())throw new Error('refounding account or membership changed');
      const rooms=saved(),at=rooms.findIndex(r=>roomIdentity(r)===identity);rooms[at].cord={...rooms[at].cord,bundle:{...prior,refounding_pending:true},refounding:JSON.parse(JSON.stringify(checkpoint))};save(rooms);
      startRekeyLive(p,rooms[at],roomControls.get(loadKey)||[]);
    };
    const next=await reader.resumeRefounding(plan,{current,persist,publish:async(event,phase)=>{
      if(phase==='before'){
        // Every bootstrap target must confirm retirement. A retry republishes the exact
        // signed event; partial success never advances the durable prerequisite cursor.
        for(const relay of plan.beforeRelays){
          if(!current())throw new Error('refounding account or membership changed');
          const accepted=await p.relayPublishRoom([relay],event,{current});
          if(!current())throw new Error('refounding account or membership changed');
          if(!accepted?.ok)return {ok:false};
        }
        return {ok:true};
      }
      const material=['root','channels'].includes(phase)?prior:plan.next,controls=phase==='control'?plan.phases[1].events:[],auth=reader.createPlaneAuth(material,controls,event.pubkey,relays);
      if(!current())throw new Error('refounding account or membership changed');
      return p.relayPublishRoom(relays,event,auth?{...auth,current}:null);
    }});
    if(!current())throw new Error('refounding account or membership changed');
    const rooms=saved(),at=rooms.findIndex(r=>roomIdentity(r)===identity),updated={...rooms[at],banned:reader.inspectControl(next,plan.phases[1].events).banned,cord:{...rooms[at].cord,bundle:next}};delete updated.cord.refounding;rooms[at]=updated;save(rooms);
    roomControls.set(loadKey,[...(roomControls.get(loadKey)||[]),...plan.phases[1].events]);
    await cacheEnvelopes(envelopeCacheKey(loadKey,'control'),plan.phases[1].events);
    if(deliveryOwner(p)!==owner)return;
    await persistArmadaMembership(p,updated);if(deliveryOwner(p)!==owner)return;
    if(window.PCConcord?.refreshOwnedInviteLinks)try{await window.PCConcord.refreshOwnedInviteLinks(p,updated);}catch(e){console.warn('Rotated community invite refresh is pending',e);}
    if(deliveryOwner(p)!==owner)return;
    startRekeyLive(p,updated,roomControls.get(loadKey));backgroundRender();return next;
  }

  const pinReconciliations=new Map();
  async function reconcilePins(p,room,channelId,observed=[]){
    const reader=window.PosterCordReader,owner=deliveryOwner(p),identity=roomIdentity(room),bundle=room?.cord?.bundle;
    if(!owner||!bundle||bundle.dissolved||bundle.removed||bundle.refounding_pending||!reader?.inspectPinList||!p.signTemplate)return;
    const loadKey=room.communityId||room.naddr,key=owner+'\n'+identity+'\n'+channelId;
    let entry=pinReconciliations.get(key);
    if(entry){entry.pending=true;for(const w of observed)entry.wraps.set(w.id,w);return entry.job;}
    entry={pending:true,wraps:new Map(observed.map(w=>[w.id,w]))};pinReconciliations.set(key,entry);
    entry.job=(async()=>{
      // Coalesce witnesses/edits before re-reading the authoritative head. No
      // timer survives an operation, and account/head checks gate every write.
      await new Promise(resolve=>setTimeout(resolve,100+Math.floor(Math.random()*300)));
      while(entry.pending){entry.pending=false;
        const active=saved().find(r=>roomIdentity(r)===identity),snapshot=JSON.stringify(active?.cord?.bundle);
        const current=()=>deliveryOwner(p)===owner&&saved().some(r=>roomIdentity(r)===identity&&JSON.stringify(r.cord?.bundle)===snapshot&&!r.cord.bundle.dissolved&&!r.cord.bundle.removed&&!r.cord.bundle.refounding_pending);
        if(!current())return;
        const local=roomControls.get(loadKey)||[];if(!reader.canPinMessages(active.cord.bundle,local,channelId,owner))return;
        const visible=reader.inspectPinList(active.cord.bundle,local,channelId);
        if(!visible.available||!visible.entries.length){if(entry.pending)continue;return;}
        const cached=await cachedEnvelopes(envelopeCacheKey(loadKey,channelId));if(!current())return;
        for(const w of cached)entry.wraps.set(w.id,w);
        const witnesses=[...entry.wraps.values()],filtered=reader.inspectPinList(active.cord.bundle,local,channelId,witnesses);
        if(filtered.entries.length===visible.entries.length){if(entry.pending)continue;return;}
        const fetched=await acquireRefoundingControls(p,active,current);if(!current())return;
        const controls=[...new Map([...fetched,...(roomControls.get(loadKey)||[])].map(e=>[e.id,e])).values()];
        const before=reader.inspectPinList(active.cord.bundle,controls,channelId),after=reader.inspectPinList(active.cord.bundle,controls,channelId,witnesses);
        if(!before.available||!after.available||before.entries.length===after.entries.length){if(entry.pending)continue;return;}
        const stamp=JSON.stringify(cordControlStamp(roomControls.get(loadKey)||[])),owned=()=>current()&&JSON.stringify(cordControlStamp(roomControls.get(loadKey)||[]))===stamp;
        const made=await reader.createPinListWrap(active.cord.bundle,controls,channelId,after.entries.map(e=>e.proof),owner,p.signTemplate);if(!owned()){if(current()&&entry.pending)continue;return;}
        const relays=roomRelays(active.cord.bundle),auth=reader.createPlaneAuth(active.cord.bundle,controls,made.wrap.pubkey,relays);
        const accepted=await p.relayPublishRoom(relays,made.wrap,{...auth,current:owned});if(!owned()){if(current()&&entry.pending)continue;return;}
        if(!accepted?.ok)throw new Error('pin deletion publication was not confirmed');
        roomControls.set(loadKey,[...controls,made.wrap]);await cacheEnvelopes(envelopeCacheKey(loadKey,'control'),[made.wrap]);if(!current())return;
        backgroundRender();
      }
    })();
    try{return await entry.job;}finally{if(pinReconciliations.get(key)===entry)pinReconciliations.delete(key);}
  }
  const rekeySubscriptions=new Map();
  const staffAdoptions=new Map();
  async function adoptStaffGrant(p,room,controls){
    const reader=window.PosterCordReader,bundle=room?.cord?.bundle,owner=deliveryOwner(p),identity=roomIdentity(room);
    if(!bundle?.control_pk||bundle.control_root||bundle.removed||bundle.dissolved||bundle.refounding_pending||!owner||!reader?.adoptStaffControlWrap)return;
    const snapshot=JSON.stringify(bundle),loadKey=room.communityId||room.naddr,stamp=JSON.stringify(cordControlStamp(controls||[])),key=owner+'\n'+identity+'\n'+snapshot+'\n'+stamp;
    if(staffAdoptions.has(key))return staffAdoptions.get(key);
    const currentControls=()=>roomControls.get(loadKey)||controls||[];
    const current=()=>deliveryOwner(p)===owner&&JSON.stringify(cordControlStamp(currentControls()))===stamp&&saved().some(r=>roomIdentity(r)===identity&&JSON.stringify(r.cord?.bundle)===snapshot);
    const job=(async()=>{
      const next=await reader.adoptStaffControlWrap(bundle,controls||[],owner,p.cordRekeyDecrypt,currentControls);
      if(next===bundle||!current())return;
      const rooms=saved(),at=rooms.findIndex(r=>roomIdentity(r)===identity),updated={...rooms[at],cord:{...rooms[at].cord,bundle:next}};
      rooms[at]=updated;save(rooms);await persistArmadaMembership(p,updated);
      if(deliveryOwner(p)!==owner)return;
      startRekeyLive(p,updated,controls);backgroundRender();
    })();staffAdoptions.set(key,job);
    try{await job;}finally{if(staffAdoptions.get(key)===job)staffAdoptions.delete(key);}
  }

  async function freezeSealedHistory(room){
    const loadKey=room.communityId||room.naddr,history={},channels=[...(room.channels||[]),...(room.cord.bundle.channels||[])];
    for(const id of new Set(channels.map(c=>c.id).filter(Boolean)))history[id]=(await cachedEnvelopes(envelopeCacheKey(loadKey,id))).map(e=>e.id);
    return {sealed_history:history,sealed_controls:(roomControls.get(loadKey)||[]).map(e=>e.id)};
  }
  function stopRekeyLive(){for(const entry of rekeySubscriptions.values())entry.close();rekeySubscriptions.clear();}
  function startRekeyLive(p,room,controls){
    void adoptStaffGrant(p,room,controls).catch(e=>{console.warn('Staff key delivery could not be adopted',e);});
    const reader=window.PosterCordReader,R=(window.PCConcordRelay||window.Relay),bundle=room&&room.cord&&room.cord.bundle,owner=deliveryOwner(p);
    if(!reader||!reader.inspectRekeyStreams||!bundle||!owner||!R||!R.subscribeFrom||bundle.dissolved||bundle.removed)return;
    const identity=roomIdentity(room),snapshot=JSON.stringify(bundle),controlStamp=JSON.stringify(cordControlStamp(controls)),key=owner+'\n'+identity;
    for(const [oldKey,old] of rekeySubscriptions)if(old.owner!==owner){old.close();rekeySubscriptions.delete(oldKey);}
    const prior=rekeySubscriptions.get(key);if(prior&&prior.snapshot===snapshot&&prior.controlStamp===controlStamp){if(prior.refresh)prior.refresh();return;}
    if(prior)prior.close();
    const authors=reader.inspectRekeyStreams(bundle);if(!authors.length)return;
    let closed=false,warned=false,pooled=null,external=null,chain=Promise.resolve();const wraps=new Map(),relays=roomRelays(bundle);
    const current=()=>!closed&&deliveryOwner(p)===owner&&saved().some(r=>roomIdentity(r)===identity&&JSON.stringify(r.cord&&r.cord.bundle)===snapshot);
    const close=()=>{closed=true;try{R.close(pooled);}catch(_){}try{if(external)external();}catch(_){}};
    const entry={owner,snapshot,controlStamp,close};rekeySubscriptions.set(key,entry);
    const apply=async()=>{
      if(!current())return;
      const result=await reader.inspectRekeys(bundle,roomControls.get(room.communityId||room.naddr)||controls||[],[...wraps.values()],owner,p.cordRekeyDecrypt);
      if(!current())return;
      if(result.blocked&&result.blocked.length&&!warned){warned=true;if(p.toast)p.toast('A community key update could not be opened. Your signer may not support binary rekeys.');}
      let next=reader.applyRekeyUpdates(reader.withChannelHistory?reader.withChannelHistory(bundle,roomControls.get(room.communityId||room.naddr)||controls||[]):bundle,result.updates);
      const tombstone=reader.inspectDissolution&&reader.inspectDissolution(bundle,[...wraps.values()]);
      let frozen=null;
      if(tombstone){frozen=await freezeSealedHistory(room);if(!current())return;next={...bundle,dissolved:tombstone};}
      else if(bundle.refounding_pending)return;
      // Missing chunks never enter this list; keep archived keys for reading history.
      for(const removal of result.removed){if(result.updates.some(u=>u.scope===removal.scope&&u.epoch===removal.epoch))continue;
        if(removal.scope==='0'.repeat(64))next.removed=true;
        else{next.removed_channels=[...new Set([...(next.removed_channels||[]),removal.scope])];}}
      if(JSON.stringify(next)===snapshot)return;
      const rooms=saved(),at=rooms.findIndex(r=>roomIdentity(r)===identity);if(at<0||!current())return;
      const updated={...rooms[at],cord:{...rooms[at].cord,bundle:next,...(frozen||{})}};rooms[at]=updated;save(rooms);
      if(roomIdentity(saved()[state.community])===identity)stopChatLive();
      close();if(rekeySubscriptions.get(key)===entry)rekeySubscriptions.delete(key);
      if(deliveryOwner(p)!==owner)return;
      // Membership publication owns its own account checks; local adoption is durable
      // even if a relay is temporarily unavailable.
      await persistArmadaMembership(p,updated);
      if(deliveryOwner(p)!==owner)return;
      if(!next.dissolved&&!next.removed&&window.PCConcord?.refreshOwnedInviteLinks)try{await window.PCConcord.refreshOwnedInviteLinks(p,updated);}catch(e){console.warn('Rotated community invite refresh is pending',e);}
      if(deliveryOwner(p)!==owner)return;
      startRekeyLive(p,updated,controls);backgroundRender();
    };
    const onEvent=ev=>{if(!current()||!ev||Number(ev.kind)!==1059||!authors.includes(ev.pubkey)||wraps.has(ev.id))return;
      if(wraps.size>=10000)return;wraps.set(ev.id,ev);chain=chain.then(apply).catch(e=>{if(current())console.warn('Concord rekey was not applied',e);});};
    const filters=[{kinds:[1059],authors}],plane=cordPlaneContext(p,bundle,controls||[],room);
    let refreshing=false,lastRefresh=0;
    entry.refresh=()=>{if(refreshing||!current()||lastRefresh&&Date.now()-lastRefresh<60000)return;refreshing=true;lastRefresh=Date.now();
      void queryEnvelopeHistory(p,relays,authors,[...wraps.values()],{plane,purpose:'concord rekeys '+key,minInterval:60000}).then(events=>{if(current())for(const ev of events||[])onEvent(ev);}).catch(e=>{if(current())console.warn('Concord rekey history unavailable',e);}).finally(()=>{refreshing=false;});};
    try{pooled=plane?null:R.subscribe(filters,{onEvent,live:true});external=plane?cordPlaneSubscribe(p,R,relays,filters,{onEvent,timeout:0,live:true,max:1},plane):R.subscribeFrom(relays,filters,{onEvent,timeout:0,live:true});}
    catch(e){close();rekeySubscriptions.delete(key);console.warn('Concord rekey subscription failed',e);}
    if(!closed)entry.refresh();
  }

  async function refreshRoomMetadata(p){
    if(metadataBusy||!document.body.classList.contains('concord-view')||!window.PosterCordReader)return;
    const rooms=saved(),eligible=rooms.map((room,index)=>({room,index})).filter(x=>x.room&&!x.room.local&&x.room.cord&&x.room.cord.bundle&&!x.room.cord.bundle.dissolved);
    if(!eligible.length)return; metadataBusy=true;
    try{
      /* Rotate through every joined community. Metadata belongs to the community rail as much as
       * the active header, so refreshing only state.community leaves all other icons stale until
       * somebody navigates away and back. */
      const selected=eligible[metadataCursor++%eligible.length],room=selected.room,bundle=room.cord.bundle,
        reader=window.PosterCordReader,loadKey=room.communityId||room.naddr,
        seed=reader.inspectControl(bundle,[]),relays=roomRelays(bundle),context=cordPlaneContext(p,bundle,roomControls.get(loadKey)||[],room),
        cachedWraps=await cachedEnvelopes(envelopeCacheKey(loadKey,'control')),wraps=await queryEnvelopeHistory(p,relays,seed.controlPubkeys,cachedWraps,{plane:context,signal:roomIdentity(room)===roomReadIdentity&&roomReadAbortController?roomReadAbortController.signal:null,purpose:'concord room metadata '+loadKey,minInterval:60000}),freshWraps=wraps.filter(ev=>!cachedWraps.some(old=>old.id===ev.id)),
        info=reader.inspectControl(bundle,wraps||[]);
      if(!context.current())return;
      await cacheEnvelopes(envelopeCacheKey(loadKey,'control'),freshWraps);if(!context.current())return;roomControls.set(loadKey,wraps||[]);
      void refreshGuestbookMembers(p,room,wraps||[]);
      startRekeyLive(p,room,wraps||[]);
      let changed=false;
      const assign=(key,value)=>{if(value!==undefined&&JSON.stringify(room[key])!==JSON.stringify(value)){room[key]=value;changed=true;}};
      assign('name',info.name||room.name); assign('description',info.description===undefined?room.description:info.description);
      assign('banned',Array.isArray(info.banned)?info.banned:room.banned||[]);
      /* Alongside `banned`, and for the same reason: the room object is what the UI reads
         when it decides whether to OFFER the remove control. */
      assign('moderators',Array.isArray(info.moderators)?info.moderators:room.moderators||[]);
      if(await applyRoomIconMetadata(room,info,loadKey,seed))changed=true;
      if(await applyRoomBanner(room,info,loadKey,seed))changed=true;
      if(!context.membershipCurrent())return;
      const remembered=reader.withChannelHistory?reader.withChannelHistory(bundle,wraps):bundle;
      const currentBundle=info.relaysAuthoritative?{...remembered,relays:info.relays}:remembered;
      if(JSON.stringify(bundle)!==JSON.stringify(currentBundle)){room.cord={...room.cord,bundle:currentBundle};changed=true;}
      const channels=(info.channels||[]).map(c=>({id:c.id,name:c.name,private:!!c.private,readOnly:!!c.readOnly,view:c.view==='forum'?'forum':'chat',streamPubkeys:c.streamPubkeys,repositories:Array.isArray(c.repositories)?c.repositories:[]})).filter(c=>c.name);
      if(info.deletedChannels?.length)assign('deletedChannels',[...new Set([...(room.deletedChannels||[]),...info.deletedChannels])]);
      if(channels.length)assign('channels',channels.filter(c=>!room.deletedChannels?.includes(c.id)));else if(room.deletedChannels?.length)assign('channels',(room.channels||[]).filter(c=>!room.deletedChannels.includes(c.id)));
      if(changed){const latest=saved(),at=latest.findIndex(item=>sameRoom(item,room));if(at>=0){latest[at]=mergeRoom(latest[at],room);save(latest);preserveChatScroll(()=>backgroundRender());}}
      const active=saved()[state.community],activeChannel=active&&(active.channels||[]).find(c=>c.name===(state.channel||'general'));
      if(roomIdentity(active)===roomIdentity(room)&&activeChannel)startChatLive(p,active,activeChannel);
    }catch(e){console.warn('Concord metadata sync failed',e);}finally{metadataBusy=false;}
  }
  function stopLiveSync(){ stopVoiceWatch(); stopRekeyLive(); if(liveTimer)clearTimeout(liveTimer); liveTimer=null; stopChatLive(); }
  /* THE LIVE LOOP RESTS. It was started on the first visit to Communities and never stopped (stopLiveSync has no caller),
   * so every 4 s for the life of the page -- in the background too, since the APK's foreground service keeps the WebView's
   * timers running -- it refreshed metadata, swept every room's relays, re-deduplicated every room and swept IndexedDB
   * (2026-10-21 audit). Now: nothing while the relay pool sleeps (Relay.sleep() after 20 s in the background), and every
   * 5th tick (20 s, the badge sweep's own rate) while Communities is not the screen being looked at. */
  let liveTick=0;
  const liveResting=()=>{ try{ const Relay=(window.PCConcordRelay||window.Relay); return !!(Relay&&Relay.asleep&&Relay.asleep()); }catch(_){ return false; } };
  const liveOffScreen=()=>document.hidden||!document.body.classList.contains('concord-view');
  function startLiveSync(p){ if(liveTimer||!document.body.classList.contains)return; liveTimer=setInterval(()=>{if(liveResting())return;if(liveOffScreen()&&(++liveTick%5))return;refreshRoomMetadata(p);refreshActiveChannel(p);startRoomsLive(p);sweepOtherRooms(p);let changed=false;for(const [id,rows] of remoteMessages){const clean=uniqueMessages(rows);if(clean.length!==rows.length||clean.some((m,i)=>m!==rows[i])){remoteMessages.set(id,clean);changed=true;}}if(changed&&document.body.classList.contains('concord-view'))preserveChatScroll(()=>backgroundRender());if(window.PosterCordReader?.sweepExpiredChat)window.PosterCordReader.sweepExpiredChat();for(const [id,d] of deliveries)if(messageExpired(d.made))deliveries.delete(id);if(window.PCConcordCache?.sweepExpired)window.PCConcordCache.sweepExpired().catch(()=>{});},4000); }
  /* ONE SIGNED LISTING PER ROOM PER ACCOUNT, RE-SENT — NEVER RE-SIGNED.
   *
   * The Discover listing is a public kind-1 `${name}\n\n${url}` with the same two `t` tags every
   * time. "Publish listing" used to sign a NEW one on every click (and after creating the room,
   * which had already announced it), so each click put another note with identical content and tags
   * but a different id on the user's public relays. That is precisely what Amethyst's AntiSpamFilter
   * keys on (hash of content+tags, a second id = spam, five = the author is hidden): reported as
   * "users are getting censored on amethyst because posterchan is sending multiple similar events
   * for concord". Re-sending the SAME signed event is idempotent at every relay and counts once. */
  const listingKey=(pk,url)=>'pc.concord.listing.v1.'+pk+'.'+String(url||'');
  function rememberListing(pk,url,ev){
    try{if(pk&&url&&ev&&ev.id&&ev.sig&&ev.pubkey===pk)localStorage.setItem(listingKey(pk,url),JSON.stringify(ev));}catch(_){}
  }
  function heldListing(pk,url){
    try{const ev=JSON.parse(localStorage.getItem(listingKey(pk,url))||'null');
      return ev&&ev.id&&ev.sig&&ev.pubkey===pk&&ev.kind===1&&String(ev.content||'').includes(String(url))?ev:null;}catch(_){return null;}
  }
  async function announceListing(p,room){
    const pk=String(p&&p.viewer&&p.viewer().pubkey||'');
    if(!pk||!room||!room.url)throw new Error('sign in to list this community');
    const held=heldListing(pk,room.url);
    if(held){
      const accepted=await p.relayPublishTo([...new Set([...(p.relayUrls?.()||[]),...DISCOVER_RELAYS])],held);
      if(!accepted)throw new Error('Armada discovery relays rejected the listing');
      return held;
    }
    const made=await p.publish(1,`${room.name}\n\n${room.url}`,[['t','concord'],['t','community']]);
    const ev=made&&made.ev;if(!ev)throw new Error('the listing could not be signed');
    // Remembered BEFORE the discovery send, so a refused send is retried with this same event.
    rememberListing(pk,room.url,ev);
    const accepted=await p.relayPublishTo(DISCOVER_RELAYS,ev);
    if(!accepted)throw new Error('Armada discovery relays rejected the listing');
    return ev;
  }
  /* A VOTE THAT REPEATS YOUR CURRENT ANSWER IS NOT A VOTE. The tally keeps each voter's newest
   * answer, so re-sending the same one changes nothing on screen — but it is a fresh signed 1018
   * each click. Same answer (as a set; order is not meaning) → publish nothing. */
  function pollVoteChanged(poll,pubkey,picked){
    let mine=null,newest=-1;
    for(const v of (poll&&poll.votes)||[])if(v&&v.pubkey===pubkey&&(Number(v.ms)||0)>=newest){newest=Number(v.ms)||0;mine=new Set(v.optionIds||[]);}
    const next=new Set(picked||[]);
    if(!mine)return next.size>0;
    return mine.size!==next.size||[...next].some(o=>!mine.has(o));
  }
  async function mintPublicRoom(p,name,icon){
    const viewer=p.viewer?p.viewer():{}; if(!viewer.pubkey||!window.PosterCord)throw new Error('sign in before creating a relay community');
    /* MADE HERE, HELD HERE. On an instance the community's one relay is this node's own: every member
     * here reads and writes it with no other socket, and members elsewhere reach it like any relay (it
     * serves and accepts kind-1059 wraps from anyone). With no instance, the shared CORD relays as before. */
    const home=typeof homeRelayUrl==='function'?homeRelayUrl(p):'';
    const relays=home?[home]:[...new Set([...CORD_RELAYS,...(p.relayUrls?p.relayUrls():[])])].slice(0,8);
    const creator=p.cordDirectContext();
    const made=await window.PosterCord.createCommunity({name,icon,owner:viewer.pubkey,relays,base:publicInviteBase(p),signEvent:creator.sign});
    if(!creator.isCurrent())throw new Error('creating account changed');
    const bundle={community_id:made.communityId,owner:viewer.pubkey,owner_salt:made.secrets.ownerSalt,community_root:made.secrets.root,control_pk:made.secrets.controlPk,control_root:made.secrets.controlRoot,root_epoch:0,channels:[],relays,name,creator_npub:viewer.pubkey};
    const controls=made.events.filter(ev=>ev.kind===1059),draft={communityId:made.communityId,name,url:made.url,cord:{bundle}};
    const guardedCall=async(fn,...args)=>{if(!creator.isCurrent())throw new Error('creating account changed');const result=await fn(...args);if(!creator.isCurrent())throw new Error('creating account changed');return result;};
    const backup={...p,nip44enc:(...args)=>guardedCall(creator.encrypt,...args),nip44dec:(...args)=>guardedCall(creator.decrypt,...args),signTemplate:(...args)=>guardedCall(creator.sign,...args),relayPublishTo:(...args)=>guardedCall(p.relayPublishTo,...args)};
    if(!await persistArmadaMembership(backup,draft))throw new Error('Owner membership backup was not accepted');
    const {api,context}=await ownedInviteContext(p,draft,creator.isCurrent);
    const entry={token:made.secrets.token,signer_sk:made.secrets.linkSignerSk,community_id:made.communityId,url:made.url,created_at:Math.floor(Date.now()/1000)};
    /* THE COMMUNITY IS NOT HOSTAGE TO ITS INVITE LIST. Remembering the link's signing key in the
     * creator's encrypted Invite List is bookkeeping for managing the link later; the key also stays
     * in this room's own `cord.secrets`, and Links -> "Manage original link" saves it into the list
     * from there. So a list that cannot be saved right now (a relay that did not answer: the list
     * read is then incomplete and is rightly never written over) is retried a few times and then
     * REPORTED -- it used to throw, and the whole new community was thrown away with it. Only an
     * incomplete READ is tolerated: a relay that REFUSES the write would refuse the community too,
     * and that still aborts before anything usable is published. */
    const retries=[1000,3000];let inviteListPending='';
    for(let attempt=0;;attempt++){
      try{await api.remember(entry,context);break;}
      catch(e){
        if(!context.isCurrent()||!e||!e.incomplete)throw e;
        if(attempt>=retries.length){inviteListPending='its invite link could not be saved to your Invite List yet ('+(e&&e.message||e)+') — retry from Links → Manage original link';console.warn('[concord] new community kept; Invite List save is pending',e);break;}
        await new Promise(resolve=>setTimeout(resolve,retries[attempt]));
        if(!context.isCurrent())throw new Error('creating account changed');
      }
    }
    const registry=await window.PosterCordReader.createInviteRegistryWrap(bundle,controls,[api.details(entry).pubkey],viewer.pubkey,creator.sign);
    if(!context.isCurrent())throw new Error('creating account changed');
    made.events=[...controls,registry.wrap,...made.events.filter(ev=>ev.kind!==1059)];
    const plane=cordPlaneContext(p,bundle,made.events.filter(ev=>ev.kind===1059));
    for(const ev of made.events){
      if(!context.isCurrent())throw new Error('creating account changed');
      const accepted=await p.relayPublishRoom(relays,ev,ev.kind===1059?cordPlaneAuth(p,plane,ev.pubkey,relays):null);
      if(!accepted||!accepted.ok)throw new Error('CORD relays rejected an event');
    }
    if(!context.isCurrent())throw new Error('creating account changed');
    const announcement=await creator.sign({kind:1,created_at:Math.floor(Date.now()/1000),content:`${name}\n\n${made.url}`,tags:[['t','concord'],['t','community']]});
    if(!context.isCurrent())throw new Error('creating account changed');
    rememberListing(viewer.pubkey,made.url,announcement);
    const announced=await p.relayPublishRoom(DISCOVER_RELAYS,announcement);
    if(!context.isCurrent())throw new Error('creating account changed');
    if(!announced?.ok)throw new Error('Community was created but its discovery announcement was not accepted');
    return {communityId:made.communityId,name,icon,description:'',channels:[{name:'general',private:false,id:made.generalChannelId}],local:false,naddr:inviteParts(made.url).naddr,url:made.url,cord:{...made,bundle},...(inviteListPending?{inviteListPending}:{})};
  }
  async function activateJoinedRoom(p,index,inDrawer=false,expectedIdentity=''){
    let rooms=saved();
    if(expectedIdentity){const currentIndex=rooms.findIndex(room=>roomIdentity(room)===expectedIdentity);if(currentIndex<0)return false;index=currentIndex;}
    let room=rooms[index];if(!room)return false;
    const identity=roomIdentity(room);
    discoveryOpen=false;activeSet(String(index));state.thread=null;state.community=index;state.channel='general';mobileChatOpen=!!inDrawer;mobileDrawerOpen=!!inDrawer;
    /* Pin FIRST, so render() lands the new room at its newest message in the same task. */
    enterChatBottom();render();
    try{
      if(room.url&&(!room.cord||!room.cord.bundle)){room=mergeRoom(room,await hydrateInvite(p,room.url));rooms=saved();const at=rooms.findIndex(item=>roomIdentity(item)===identity||sameRoom(item,room));if(at<0)return false;const selected=rooms[state.community];rooms[at]=room;save(rooms);index=rooms.findIndex(item=>sameRoom(item,room));if(sameRoom(selected,room)){state.community=index;render();}}
      if(room.cord&&!hydratedRoomViews.has(roomIdentity(room)))await hydrateRoomStreams(p,index,roomIdentity(room));
      else if(room.protocol==='nip29'&&!room.nip29Hydrated)await hydrateNip29Room(p,index);
      room=saved()[state.community];const liveChannel=room&&(room.channels||[]).find(c=>c.name===(state.channel||'general'));
      if(room&&liveChannel)startChatLive(p,room,liveChannel);
      roomLoadNotices.delete(roomIdentity(room));return true;
    }catch(e){
      if(room.protocol==='nip29'){rooms=saved();const at=rooms.findIndex(item=>roomIdentity(item)===identity);if(at>=0){rooms[at].nip29Hydrated=false;save(rooms);}}
      roomLoadWarning(p,identity,'could not refresh community: ',e);return false;
    }finally{
      const active=saved()[state.community];if(roomIdentity(active)===identity)enterChatBottom(true);
    }
  }
  async function resumeActiveRoom(p,identity){
    const rooms=saved(),index=rooms.findIndex(room=>roomIdentity(room)===identity),room=rooms[index];
    if(index<0||state.community==null||roomIdentity(rooms[state.community])!==identity)return;
    try{
      if(room.cord&&!hydratedRoomViews.has(identity))await hydrateRoomStreams(p,index,identity);
      else if(room.cord)await refreshActiveChannel(p);
      else if(room.protocol==='nip29'&&!room.nip29Hydrated)await hydrateNip29Room(p,index);
      const active=saved()[state.community],liveChannel=active&&(active.channels||[]).find(c=>c.name===(state.channel||'general'));
      if(active&&liveChannel)startChatLive(p,active,liveChannel);
    }catch(e){roomLoadWarning(p,identity,'could not refresh community: ',e);}
  }
  function wake(){resumeRequested=true;}
  let socialEmojisWarmed = false;
  /* THE MESSAGES PANE, BUILT ONCE AND PAINTED BY TWO PAINTERS.
   *
   * render() below draws the whole workspace, which REPLACES the composer — and a replaced
   * textarea loses focus, which on Android closes the soft keyboard mid-sentence. That is why
   * backgroundRender() defers a repaint while somebody is typing. Deferring the WHOLE paint is
   * what made arriving messages invisible until the next send or room switch, so patchMessageList()
   * repaints exactly this pane and leaves the composer where it is. One builder, so the two paints
   * can never disagree about what a message looks like. */
  /* "NO MESSAGES" IS A CLAIM, AND THIS IS WHERE IT GETS MADE.
   *
   * An empty channel drew "This is the start of this encrypted channel" whatever the reason it was
   * empty — including the reason that nothing could be read. Two joined Vector rooms sat like that:
   * their relay set includes one that gates every kind-1059 filter behind an AUTH it then refuses
   * ("relay needs serviceUrl to be configured before AUTH can work"), so its history is unreadable,
   * and half an hour of failure cooldown later the client stops even trying and gets a silent `[]`.
   * The room announced itself brand new to somebody who could see its messages in another client.
   *
   * Same rule Trending already follows here: a query no relay answered is "the relays never spoke",
   * never "there is nothing". Say which relays, and offer the retry — the cooldown is the thing
   * standing in the way, so the button has to clear it rather than just re-asking. */
  function emptyChannelHtml(p,room){
    if(room&&room.local)return `<div class="cc-welcome"><div class="cc-welcome-hash">#</div><h2>Welcome to #${p.enc(state.channel||'general')}</h2><p>This local test room lets you validate the chat UI before publishing or joining a relay community.</p></div>`;
    const unread=room?channelUnread(room,state.channel||'general'):null;
    if(!unread)return `<div class="cc-welcome"><div class="cc-welcome-hash">#</div><h2>Welcome to #${p.enc(state.channel||'general')}</h2><p>This is the start of this encrypted channel.</p></div>`;
    const hosts=unread.missed.map(u=>{try{return new URL(u).host;}catch(_){return String(u);}}),
      read=unread.ok.length,total=unread.total||read+hosts.length;
    return `<div class="cc-welcome cc-welcome-unread"><div class="cc-welcome-hash">!</div>`
      +`<h2>#${p.enc(state.channel||'general')} could not be read</h2>`
      +`<p>${read} of ${total} of this community's relays answered${hosts.length?', and '+p.enc(hosts.join(', '))+' did not':''}.`
      +`${unread.unheld.length?` ${unread.unheld.length} stream${unread.unheld.length===1?'':'s'} in this channel need a membership key this account does not hold.`:''}`
      +` This is not the same as the channel being empty.</p>`
      +`<button id="cc-retry-channel" class="cc-welcome-retry">Try these relays again</button></div>`;
  }
  /* A FORUM CHANNEL (CORD-03 §2 view "forum") opens as its posts, not as a chat. Same messages, same
   * threads: a post is a titled kind-9 root, its comments are the thread, and opening one is the
   * ordinary thread view (state.thread), where the composer replies to it. */
  function currentChannelOf(room){return room&&(room.channels||[]).find(c=>c&&c.name===(state.channel||'general'))||null;}
  /* THE BAR ABOVE AN OPEN THREAD says where you are and how to get back. It read "← Back | Thread | 1b84c5567b12…" --
   * a raw key where a forum post's TITLE belongs, in a floating bordered box unlike every other header ("the thread
   * bar looks out of place and bad", 2026-10-21). A forum post now shows "‹ All posts" and its title; a chat thread
   * "‹ #channel", "Thread" and who started it, by the name this client knows for them. */
  function threadBarHtml(p,room,messages){
    const root=messages.find(x=>messageId(x)===state.thread)||{},name=state.channel||'general';
    const ch=room?channelsOf(room).find(c=>c.name===name):null,forum=!!(ch&&ch.view==='forum');
    const title=forum&&window.PCCordSpaces?String(window.PCCordSpaces.subjectOf(root)||''):'';
    let who='';try{const pr=root.pubkey&&p.profOf?p.profOf(root.pubkey)||{}:{};who=String(pr.display_name||pr.name||'');}catch(_){}
    if(!who&&root.by&&!/^[0-9a-f]{8,}/i.test(String(root.by)))who=String(root.by);
    return '<div class="cc-thread-bar'+(forum?' is-forum':'')+'"><button id="cc-thread-back" aria-label="'+(forum?'Back to all posts':'Back to #'+p.enc(name))+'">'
      +'<span aria-hidden="true">\u2039</span><span>'+(forum?'All posts':'#'+p.enc(name))+'</span></button>'
      +'<div class="cc-thread-what"><b>'+p.enc(forum?(title||'Post'):'Thread')+'</b>'+(!forum&&who?'<small>started by '+p.enc(who)+'</small>':'')+'</div></div>';
  }
  function forumFeedHtml(p,messages,current){
    const S=window.PCCordSpaces,ch=currentChannelOf(current);
    if(!S||state.community==null||state.thread||!ch||S.channelView(ch)!=='forum')return '';
    const index=threadInfo(messages||[]).index,posts=S.forumPosts(messages||[],id=>index.get(id)||[]);
    return S.feedHtml(p,posts,{channel:ch.name,canPost:!ch.readOnly&&!(current.cord&&current.cord.bundle&&current.cord.bundle.dissolved)});
  }
  async function publishForumPost(p,room,channelName,title,body){
    const S=window.PCCordSpaces,viewer=p.viewer?p.viewer():{},storeId=channelStoreId(room,channelName),tags=S.subjectTags(title);
    const at=Date.now(),tempId='pending-'+(crypto.randomUUID?crypto.randomUUID():at+'-'+Math.random().toString(36).slice(2));
    const rows=testMessages(storeId),pr=p.profOf?p.profOf(viewer.pubkey)||{}:{};
    rows.push({id:tempId,by:pr.display_name||pr.name||'You',pubkey:viewer.pubkey||'',text:body,at,kind:9,tags,reactions:{},pending:!room.local,remote:false,delivery:room.local?'':'signing'});
    if(!room.local)markRemoteStore(storeId);
    saveTestMessages(storeId,rows);render();
    if(room.local)return;
    try{
      const made=await publishCordMessage(p,room,channelName,body,tags,9,d=>{const list=testMessages(storeId),row=list.find(x=>x.id===tempId);if(row){row.id=d.made.rumorId;row.tags=d.made.tags||row.tags;row.delivery='sending';saveTestMessages(storeId,list);if(deliveryOwner(p)===viewer.pubkey)backgroundRender();}});
      const list=testMessages(storeId),sent=list.find(x=>x.id===tempId||x.id===made.rumorId);
      if(sent&&deliveryOwner(p)===viewer.pubkey){sent.id=made.rumorId;sent.at=made.ms;sent.tags=made.tags||sent.tags;sent.pending=false;sent.remote=true;sent.delivery='sent';saveTestMessages(storeId,list);}
      render();
    }catch(e){
      const list=testMessages(storeId),failed=list.find(x=>x.id===tempId||e&&e.delivery&&x.id===e.delivery.made.rumorId);
      if(failed){failed.pending=false;failed.failed=true;failed.delivery=e&&e.delivery?e.delivery.status:'failed';saveTestMessages(storeId,list);}
      render();p.toast('post was not sent: '+(e&&e.message||e));
    }
  }
  function messagesPaneHtml(p,messages,current,viewer,me){
    wantProfiles(p,(messages||[]).map(m=>m&&m.pubkey),current);
    { const forum=forumFeedHtml(p,messages,current); if(forum)return forum; }
    /* DERIVED HERE, not read from `render()`. This function is a SIBLING of render, not its
       closure — the same trap `bind()` documents above, and referring to render's locals throws on
       the first message drawn (caught by concord_runtime.mjs: "canModerate is not defined").
       `current` is the room and is already a parameter, so both facts come off it. */
    const ownerPk=String((current&&current.cord&&current.cord.bundle&&
      (current.cord.bundle.owner||current.cord.bundle.creator_npub))||'');
    const canModerate=(!!ownerPk&&ownerPk===viewer.pubkey)
      ||(Array.isArray(current&&current.moderators)
         && current.moderators.indexOf(viewer.pubkey)>=0);
    const joinedRooms=''; // Active communities use the server rail/channel navigator, not home-page cards.
    /* GIT ACTIVITY (concord-git.js): a channel with an Armada repository attachment shows that
       repository's issues, comments, status changes and CI between its messages. */
    const _gitChannel=current&&state.community!=null&&window.PCConcordGit?channelsOf(current).find(c=>c.name===(state.channel||'general')):null,
      _gitHere=!!(_gitChannel&&PCConcordGit.channelHasGit(_gitChannel));
    return `${state.community==null?`<div class="cc-discover">${inviteCardHtml(p)}<div class="concord-mark">C</div><h2>Find your community</h2><p>Join an Armada-compatible CORD-05 invite or create a public relay community.</p><div class="cc-primary-actions"><button class="btn btn-neon" id="cc-create">Create community</button><button class="btn btn-ghost" id="cc-welcome-join">Join with invite</button><button class="btn btn-ghost" id="cc-check-memberships">Check my communities</button></div>${(()=>{const n=unclaimedLegacyRooms(accountPk()).length;return n?`<p class="muted small cc-legacy-hint">${n} ${n===1?'community':'communities'} saved on this device before accounts were kept apart ${n===1?'is':'are'} not linked to this account. Check my communities to add yours.</p>`:'';})()}${joinedRooms}<section class="cc-public"><div><h3>Public communities</h3><small>Public CORD invites discovered on Armada relays</small></div>${discovered.length?discovered.map((r,i)=>{const pr=p.profOf?p.profOf(r.source.pubkey):{};return `<button data-cc-discover="${i}" class="cc-public-room"><span class="cc-public-icon">${publicRoomIcon(p,r)}</span><span class="cc-public-copy"><b>${p.enc(r.name)}</b><small>${p.enc((r.description||'Public Concord community').slice(0,120))}</small><em>${p.enc(pr.name||pr.display_name||'Nostr community')}</em></span><strong>Join</strong></button>`;}).join(''):(discoveryLoaded?'<div class="cc-public-empty"><b>No public communities found</b><span>Publish or paste a public Armada/CORD invite to list it.</span></div>':discoveryUnreachable?'<div class="cc-public-empty cc-public-unreachable" role="status"><b>Couldn’t reach the community directory</b><span>'+p.enc(discoveryUnreachable)+'. Your communities are not affected.</span><button class="btn btn-ghost" id="cc-discover-retry">Retry</button></div>':'<div class="cc-public-empty"><b>Searching relays…</b><span>Looking for public Armada/CORD invite notes.</span></div>')}</section></div>`:((messages.length||_gitHere)?`${state.thread?threadBarHtml(p,current,messages):''}<div class="cc-message-list"><!--cc-list-->${(()=>{
          /* A THREAD WHOSE ROOT IS NOT HERE MUST NOT EMPTY THE CHANNEL.
           *
           * `threadView` answers [] for a root it cannot find, and a repaint can easily happen with
           * a thread id that is momentarily stale — history reloaded, the room re-hydrated, a live
           * batch replacing the list. Rendered literally that is a community with no messages in
           * it, which is indistinguishable from the community being gone. Reported as "my
           * posterchan concord community just disappeared", and it was mine, from the threads work
           * an hour earlier.
           *
           * An empty thread view is treated as "no thread": fall back to the channel and drop the
           * filter, so the worst case is losing your place rather than losing the room. */
          if(!state.thread) return _gitHere?PCConcordGit.paneRows(current,_gitChannel,channelRows(messages,threadsInChat())):channelRows(messages,threadsInChat());
          const _t=threadView(messages,state.thread);
          if(!_t.length){ state.thread=null; return messages; }
          return _t;
        })().map(m=>{if(m&&m.ccGit)return m.html;const mp=p.profOf?p.profOf(m.pubkey):{},mid=messageId(m),_ti=threadInfo(messages),_replies=(_ti.index.get(mid)||[]).length,_flat=threadsInChat(),_root=state.thread||!_flat?'':(_ti.rootOf.get(mid)||''),_inline=!state.thread&&!_flat&&_ti.rootOf.has(mid),_canZap=!!(current&&current.cord&&!current.local&&m.pubkey&&m.pubkey!==viewer.pubkey&&p.payPrivateConcordZap);return `<article class="cc-message${messageMentionsViewer(m,viewer,me)?' cc-mentions-me':''}${_root?' cc-in-thread':''}${_inline?' cc-thread-inline':''}" data-message-id="${p.enc(mid)}"><img class="cc-message-avatar" src="${p.enc(mp.picture||p.LOGO||'')}" alt=""${m.pubkey?` data-cc-author="${p.enc(m.pubkey)}"`:''}><div class="cc-message-body">${m.reply&&!(_inline&&replyParentId(m)===_ti.rootOf.get(mid))?`<div class="cc-message-reply">${_root?`<button type="button" class="cc-in-thread-tag" data-cc-thread="${p.enc(_root)}" title="Open this thread">\u21b3 in thread</button> `:''}<b>@${p.enc(m.reply.by||'member')}</b> ${p.enc(String(m.reply.text||'').slice(0,100))}</div>`:''}<b class="cc-message-author"${m.pubkey?` data-cc-author="${p.enc(m.pubkey)}"`:''}>${p.enc(m.by)}</b><time>${new Date(m.at).toLocaleTimeString([],{hour:'2-digit',minute:'2-digit'})}</time>${(m.tags||[]).some(t=>t[0]==='edited')?'<span class="cc-edited" title="This message was edited">(edited)</span>':''}${window.PCCordSpaces?PCCordSpaces.subjectHtml(m):''}${messageContentHtml(p,m,current,state.channel)}${linkSparkleHtml(p,m)}${deliveryHtml(p,m)}<div class="cc-reactions">${reactionSummary(p,m)}${zapSummary(p,m)}</div><div class="cc-message-actions" role="toolbar" aria-label="Message actions"><button class="cc-action-trigger" data-cc-actions="${p.enc(mid)}" aria-expanded="false" title="Message actions"><svg class="ic" aria-hidden="true"><use href="#i-more"></use></svg></button><button data-cc-react="${p.enc(mid)}" title="Add reaction">☺</button>${_canZap?`<button data-cc-zap="${p.enc(mid)}" title="Private zap">⚡</button>`:''}<button data-cc-reply="${p.enc(mid)}" title="Reply">↩</button>${m.pubkey&&viewer.pubkey&&m.pubkey!==viewer.pubkey&&current&&current.cord&&!current.local&&!m.pending&&!m.failed&&p.openReport?`<button data-cc-report="${p.enc(mid)}" title="Report to this room’s moderators">🚩</button>`:''}${canEditMessage(m,viewer,current)?`<button data-cc-edit="${p.enc(mid)}" title="Edit message">✎</button>`:''}${canPinHere(current,state.channel,viewer)&&!m.pending&&!m.failed&&(m.kind===9||m.kind===1111)?`<button data-cc-pin="${p.enc(mid)}" title="${pinsOf(current,state.channel).some(x=>x.id===mid)?'Unpin message':'Pin message'}">${pinsOf(current,state.channel).some(x=>x.id===mid)?'📍':'📌'}</button>`:''}${_replies&&!state.thread&&_flat?`<button class="cc-thread-open" data-cc-thread="${p.enc(mid)}" title="Open thread">${_replies} ${_replies===1?'reply':'replies'}</button>`:''}<button data-cc-delete="${p.enc(mid)}" class="cc-delete-action ${m.pubkey&&(m.pubkey===viewer.pubkey||(canModerate&&m.pubkey!==ownerPk))?'':'hidden'}" title="${m.pubkey===viewer.pubkey?'Delete message':'Remove this message'}">⌫</button></div></div></article>${_replies&&!state.thread&&!_flat?threadSummaryHtml(p,mid,_ti.index.get(mid)):''}`;}).join('')}<!--/cc-list--></div>`:emptyChannelHtml(p,current))}`;
  }
  let _ccLastHtml='',_ccLastRoot=null;   // what render() last drew, and its root -- see below
  let renderedAccount='';
  function render(){
    if(!TUNNEL.ticket&&!TUNNEL.off&&tunnelBase(PC()))refreshTunnel();
    // An explicit/user render supersedes any coalesced background paint. A focusout listener from
    // the old workspace may still fire, but it observes false and cannot paint twice.
    backgroundRenderPending=false;backgroundFocusHost=null;
    const p=PC();
    if(pendingInvite?.directId&&pendingInvite.account!==p?.viewer?.()?.pubkey)pendingInvite=null;
    /* Every async Concord path eventually calls render(). The feed belongs to the CURRENT app, not
     * to whichever request finished last. This guard protects Code and every other shared-feed view
     * from relay/discovery/deferred Concord work completing after navigation. */
    if(!p || typeof p.isView!=='function' || !p.isView('concord')) return;
    if(window.PCOS && PCOS.isOn && PCOS.isOn() &&
       (!PCOS.ownsFeedView || !PCOS.ownsFeedView('concord'))) return;
    const feed=p.$('#feed'); if(!feed) return;
    if(!socialEmojisWarmed && p.loadCustomEmojis){
      socialEmojisWarmed = true;
      p.loadCustomEmojis().then(rows => { if(rows?.length) backgroundRender(); }).catch(()=>{});
    }
    captureComposer();
    startLiveSync(p);
    // Covers the stale-service-worker compatibility entry too, which does not run switchView().
    document.body.classList.add('concord-view','rb-off');
    ccTheme.forOwner(p.viewer?.()?.pubkey);   // whose Concord this is decides its theme
    /* THE ACCOUNT ON SCREEN IS PART OF WHAT IS DRAWN. The switcher reloads, so this normally runs once
     * per page; should the signed-in key change under a live page anyway, the open room index belongs
     * to the OTHER account's list and must not be carried across. */
    { const acct=accountPk();
      if(acct!==renderedAccount){
        if(renderedAccount){state.community=null;state.channel=null;state.thread=null;discoveryOpen=false;stopChatLive();}
        renderedAccount=acct;
        if(acct)adoptOwnedLegacyRooms(acct);
      }
      if(acct&&pendingHandoff)acceptHandoff(pendingHandoff); }
    void warmRoomIcons();
    void joinInstanceCommunity(p);
    void refreshInviteCount();
    const rooms=saved();
    const viewer=p.viewer?p.viewer():{};
    let autoOpen=-1;
    if(state.community==null&&rooms.length&&!discoveryOpen){
      const wanted=Number(activeGet()||0);
      state.community=Number.isInteger(wanted)&&wanted>=0&&wanted<rooms.length?wanted:0;
      state.channel='general';state.thread=null;
      autoOpen=state.community;
    }
    /* Discovery/membership fan-out is allowed only while the Discover surface is visible. Resolve
     * the saved active room first so an ordinary launch never opens Ditto/Vector sockets for a
     * transient state.community=null frame. */
    if(state.community==null){
      ownRoomReads('');
      startDiscovery(p);syncArmadaMemberships(p,viewer);syncNip29Memberships(p,viewer);void mirrorAllJoined(p);
    }else{
      /* Restore every membership already cached or present on PosterChan without opening any public
       * discovery relay. This is what makes all joined rooms appear on an ordinary launch. */
      /* One bounded cross-relay recovery is required even with a selected room: membership shards
         are distributed and the local relay may hold only the newest subset. This is not public
         listing discovery and schedules no active-room retry. */
      syncArmadaMemberships(p,viewer,'recovery');syncNip29Memberships(p,viewer,true);void mirrorAllJoined(p);
      stopDiscovery();
      clearTimeout(membershipRetryTimer);membershipRetryTimer=null;
      clearTimeout(nip29RetryTimer);nip29RetryTimer=null;
    }
    const profile=viewer.profile||{};
    const me=profile.display_name||profile.name||(profile.nip05&&p.niceNip05(profile.nip05))||(viewer.npub?viewer.npub.slice(0,12)+'…':'You');
    const current=state.community==null?null:rooms[state.community];
    if(current)ownRoomReads(roomIdentity(current));
    const visibleChannels=current?orderedChannels(current):[];
    if(current&&visibleChannels.length&&!visibleChannels.some(c=>c.name===(state.channel||'general')))state.channel=visibleChannels[0].name;
    /* On phones a selected community does not mean its conversation is visible: the initial
     * workspace and the open drawer show rooms/channels instead. Relay and metadata repaints used
     * to call markRead here anyway, erasing the bold unread marker before the person opened the
     * channel. Desktop always shows the conversation; mobile marks it only after the drawer closes. */
    const narrow=!!(window.matchMedia&&window.matchMedia('(max-width:820px)').matches);
    if(current&&conversationIsVisible(narrow,mobileChatOpen,mobileDrawerOpen))markRead(current,state.channel||'general');
    const currentChannel=current?visibleChannels.find(c=>c.name===(state.channel||'general')):null;
    if(!window.PCConcordGit)loadConcordGit();
    if(window.PCConcordGit){
      if(!render.gitBound){render.gitBound=true;PCConcordGit.bindHost({repaint:()=>backgroundRender(),room:()=>saved()[state.community]||null,
        viewing:(room,name)=>viewingChannel(room,name),route:(room,name,id)=>notificationRoute(room,name,{id}),
        members:()=>{const r=saved()[state.community];return r?roomParticipants(r,''):[];}});}
      if(current&&!current.local&&current.protocol!=='nip29')PCConcordGit.watch(current,currentChannel,false);
    }
    if(current&&currentChannel)void recoverDeliveries(p,current,currentChannel);
    const draftKey=composerKey(current,state.channel),draft=composerDrafts.get(draftKey)||null;
    replyTarget=draft&&draft.replyTarget||null;
    activeMentionState=draft?{choices:[...(draft.mentionChoices||[])],index:Number(draft.mentionIndex)||0,
      recipients:new Map(draft.mentionRecipients||[])}:{choices:[],index:0,recipients:new Map()};
    const channelPrivate=!!(currentChannel&&currentChannel.private);
    const channelReadOnly=!!currentChannel?.readOnly||!!(current?.deletedChannels?.length&&!currentChannel)||!!current?.cord?.bundle?.dissolved;
    const messages=current&&(current.local||current.cord||current.protocol==='nip29')?paintedMessages(current):[];
    const ownerPk=String((current&&current.cord&&current.cord.bundle&&(current.cord.bundle.owner||current.cord.bundle.creator_npub))||''),
      isOwner=!!ownerPk&&ownerPk===viewer.pubkey,banned=new Set(current&&current.banned||[]),
      /* MAY THIS VIEWER MODERATE? The room's control stream says so (`inspectControl.moderators`
         = everyone holding CORD's MANAGE_MESSAGES). Used ONLY to decide whether to offer the
         control: what a deletion actually removes is decided by the fold, which applies the
         permission and the rank ordering per message. A stale or missing list therefore costs an
         offered button that the fold declines, never a message removed by somebody who may not. */
      canModerate=isOwner||(Array.isArray(current&&current.moderators)
        && current.moderators.indexOf(viewer.pubkey)>=0),
      memberPks=current?roomParticipants(current,viewer.pubkey).filter(pk=>!banned.has(pk)):[],
      canBanAny=!!current&&!current.local&&!!current.cord&&(isOwner||memberMay(current,viewer.pubkey,'BAN'));
    let membersHidden=localStorage.getItem('pc.concord.members.hidden')==='1';
    wantProfiles(p,memberPks,saved()[state.community]);
    const memberRows=memberPks.map(pk=>{const pr=p.profOf?p.profOf(pk):{},name=pk===viewer.pubkey?me:(pr.display_name||pr.name||pk.slice(0,12)+'…');let npub='';try{npub=window.NostrTools.nip19.npubEncode(pk);}catch(_){}/* the form people copy and paste -- the hex alone found nobody from an npub */const q=[name,pr.name,pr.display_name,pr.nip05,npub,pk].filter(Boolean).join(' ').toLowerCase();return `<button class="cc-member" data-cc-member="${p.enc(pk)}" data-q="${p.enc(q)}" aria-label="${p.enc(name)} — ${pk===ownerPk?'Owner':'Member'}"><img src="${p.enc(pr.picture||p.LOGO||'')}" alt=""><span><b>${p.enc(name)}</b><small>${pk===ownerPk?'Owner':'Member'}</small></span></button>`;}).join('');
    notifyMentions(p,current,messages,viewer,me,state.channel||'general');
    if(current&&viewingChannel(current,state.channel||'general')){
      clearMentions(current,state.channel||'general');   // on screen = read
      noteChannelRead(p,current,state.channel||'general',Math.max(0,...messages.map(m=>Number(m.at)||0)));
    }
    const oldCommunityRail=feed.querySelector&&feed.querySelector('.cc-communities');
    /* A RE-RENDER MUST NOT CLOSE A SHEET SOMEBODY IS USING. Every sheet is rebuilt `hidden`, and a
       render arrives whenever the relays answer — so "Join with invite", opened the moment
       Communities appeared, shut itself a second later when public communities came in, and took
       the pasted invite with it. Open sheets, what is typed in them and the focus are carried over;
       a sheet closed on purpose (Cancel, a finished join) was hidden before this ran and stays shut. */
    /* THE REBUILT MESSAGE LIST STARTS AT THE TOP OF THE ROOM'S HISTORY. Every repaint below replaces
       `.cc-messages` wholesale — four times for one sent message (optimistic row, signing, sent, the
       echo) and once per message received — and the new scroller is born at scrollTop 0. The
       position was only put back a frame later (scrollChatBottom / preserveChatScroll run in rAF),
       which leaves a window in which the room is showing its OLDEST messages: invisible in a
       headless browser, which always runs that rAF before painting, and the most plausible source of
       "when I send message or message is received, chat room location jumps" on the desktop, where
       the frame is composited by a GPU process shared with every other window. The position is now
       written in the same task as the rebuild, so no frame can ever see the top of the history; the
       rAF passes still run afterwards and refine it (anchors, late images). */
    let keepScroll=null;
    /* `key` is the conversation that scroller SHOWS, not the one about to be drawn: a switch has already
       changed state.channel/community by now, and keyed on scrollKey() the old channel's offset was
       carried into the new one. */
    try{ const ob=feed.querySelector('.cc-messages'); if(ob)keepScroll={key:ob.dataset&&ob.dataset.ccScrollKey||'',top:ob.scrollTop,
      atBottom:ob.scrollHeight-ob.clientHeight-ob.scrollTop<=2}; }catch(_){ }
    /* AN OPEN "…" MENU SURVIVES A REPAINT. Every message that arrived, and every channel refresh,
       rebuilt the room and dropped `cc-actions-open` with it — the menu shut under the person using
       it, and on a phone (no hover to hold the toolbar up) it vanished outright: "the concord post
       ... menu keeps flashing". Which message's menu, and whether its button had the keyboard, are
       carried across the rebuild like the sheets below. */
    let openActions='',actionsFocused=false;
    try{ const o=feed.querySelector('.cc-message.cc-actions-open[data-message-id]'); openActions=o?o.dataset.messageId:'';
      actionsFocused=!!(openActions&&document.activeElement&&o.contains(document.activeElement)&&document.activeElement.matches&&document.activeElement.matches('[data-cc-actions]')); }catch(_){ }
    let openSheets=[],focusedId='';
    // #cc-invite-sheet is drawn from `pendingInvite`, never carried over: "Not now" must stay closed.
    try{ openSheets=[...feed.querySelectorAll('.cc-join:not(.hidden)[id]:not(#cc-invite-sheet)')].map(d=>({id:d.id,
      fields:[...d.querySelectorAll('input[id],textarea[id]')].map(f=>[f.id,f.value])}));
      focusedId=document.activeElement&&feed.contains(document.activeElement)?document.activeElement.id:'';
      { const ae=document.activeElement; memberFocus=ae&&ae.classList&&ae.classList.contains('cc-member-search')&&feed.contains(ae)
          ?{dlg:!!ae.closest('#cc-members-dialog'),at:ae.selectionStart}:null; } }catch(_){ }
    const _html=`<div class="cc-app${mobileChatOpen||state.community==null?' show-chat':''}${mobileDrawerOpen?' drawer-open':''}${state.community==null?' home-view':''}">
      <button class="cc-drawer-backdrop" id="cc-drawer-backdrop" aria-label="Close rooms and channels"></button>
      <aside class="cc-communities" data-cc-sig="${p.enc(railSig(rooms))}"><button class="cc-brand" id="cc-home" title="Your rooms" aria-label="Your rooms"><span aria-hidden="true">🕊</span></button><button class="cc-server cc-discovery-button" id="cc-discovery" title="Discover public communities" aria-label="Discover public communities">◎</button>${railHtml(p,rooms)}<button class="cc-server cc-add" id="cc-add" title="Create or join a community" aria-label="Create or join a community">+</button></aside>
      <aside class="cc-channels">${roomBannerUrl(current)?`<div class="cc-banner"><img src="${p.enc(roomBannerUrl(current))}" alt=""></div>`:''}<header><button class="cc-mobile-back" id="cc-back-communities" aria-label="Communities">‹</button><div><b>${state.community==null?'Concord':p.enc(roomName(current,state.community))}</b><small>${current&&current.local?'Local test community':'End-to-end encrypted'}</small></div>${current?'<button class="cc-head-btn" id="cc-edit-icon" title="Set community icon" aria-label="Set community icon"><svg class="ic"><use href="#i-image"></use></svg></button><button class="cc-head-btn danger" id="cc-leave-room" title="Leave community" aria-label="Leave community"><svg class="ic"><use href="#i-logout"></use></svg></button>':''}${canAddChannel(p,current)?'<button class="cc-head-btn" id="cc-add-channel" title="New channel" aria-label="New channel">+</button>':''}</header>
        <div class="cc-channel-list">${inviteCardHtml(p)}${state.community==null?'<div class="cc-empty-side">Choose or join a community</div>':channelSectionsHtml(p,current,visibleChannels)}</div>
        <footer class="cc-identity"><span class="cc-status"></span><div><b>${p.enc(me)}</b><small>You</small></div><button class="cc-head-btn" id="cc-theme" title="Concord theme" aria-label="Concord theme"><svg class="ic"><use href="#i-palette"></use></svg></button><button class="cc-head-btn${concordNotifyOn()?'':' cc-notify-off'}" id="cc-notify" title="Notification settings: ${concordNotifyOn()?'community notifications on — tap to turn off':'community notifications off — tap to turn on'}" aria-label="Community notifications ${concordNotifyOn()?'on':'off'}" aria-pressed="${concordNotifyOn()}"><svg class="ic"><use href="#${concordNotifyOn()?'i-bell':'i-bell-off'}"></use></svg></button></footer>
      </aside>
      <main class="cc-conversation"><header><button class="cc-mobile-back" id="cc-back-channels" aria-label="${state.community==null?'Back to rooms':'Rooms and channels'}">${state.community==null?'‹':'☰'}</button><span class="cc-hash">#</span><b>${p.enc(state.community==null?'Communities':state.channel||'general')}</b><span class="cc-visibility ${channelPrivate?'private':'public'}">${channelPrivate?'Private':'Public'}</span><span class="cc-topic">${p.enc(current?.cord?.bundle?.dissolved?'Dissolved by its owner — history is read-only':(current&&current.description)||(channelPrivate?'Invite-only channel':'Visible to all community members'))}</span><span class="cc-spacer"></span>${current&&current.cord&&!current.local?`<button class="cc-head-btn" id="cc-pins" title="Pinned messages" aria-label="Pinned messages">📌${(pinsOf(current,state.channel).length||'')?`<small>${pinsOf(current,state.channel).length}</small>`:''}</button>`:''}${threadToggleHtml(p,current)}${current?'<button class="cc-head-btn" id="cc-publish-listing" title="Publish to Armada Discover" aria-label="Publish to Armada Discover"><svg class="ic"><use href="#i-share"></use></svg></button><button class="cc-head-btn" id="cc-direct-send" title="Invite an account" aria-label="Invite an account">Invite</button><button class="cc-head-btn" id="cc-manage-links" title="Manage invitation links" aria-label="Manage invitation links">Links</button><button class="cc-head-btn" id="cc-copy-link" title="Copy room invite link" aria-label="Copy room invite link"><svg class="ic"><use href="#i-link"></use></svg></button><button class="cc-head-btn danger" id="cc-leave-shortcut" title="Leave community" aria-label="Leave community"><svg class="ic"><use href="#i-logout"></use></svg></button><button class="cc-head-btn" id="cc-call" title="Start voice call"><svg class="ic"><use href="#i-phone"></use></svg></button>':''}<button class="cc-head-btn cc-notify-head${concordNotifyOn()?'':' cc-notify-off'}" id="cc-notify-head" title="Community notifications ${concordNotifyOn()?'on':'off'}" aria-label="Community notifications ${concordNotifyOn()?'on':'off'}" aria-pressed="${concordNotifyOn()}"><svg class="ic"><use href="#${concordNotifyOn()?'i-bell':'i-bell-off'}"></use></svg></button><button class="cc-head-btn" id="cc-direct-inbox" title="Review direct invitations" aria-label="Review direct invitations${pendingInviteCount?' ('+pendingInviteCount+' waiting)':''}">Invites${pendingInviteCount?`<small class="cc-invite-count">${pendingInviteCount}</small>`:''}</button>${current&&window.PCConcordGit&&PCConcordGit.channelHasGit(channelsOf(current).find(c=>c.name===(state.channel||'general')))?'<button class="cc-head-btn" id="ccg-head-issues" title="Issues and pull requests" aria-label="Issues and pull requests"><svg class="ic"><use href="#i-bug"></use></svg></button>':''}<button class="cc-head-btn" id="cc-members" title="Members" aria-label="Members"><svg class="ic"><use href="#i-users"></use></svg></button><button class="cc-head-btn cc-head-more" id="cc-head-more" title="More" aria-label="More room actions"><svg class="ic" aria-hidden="true"><use href="#i-more"></use></svg></button></header>
        <div class="cc-messages">${messagesPaneHtml(p,messages,current,viewer,me)}</div>
        <div class="cc-reply${replyTarget?'':' hidden'}" id="cc-reply">${replyTarget?`<span>Replying to <b>${p.enc(replyTarget.by||'member')}</b>: ${p.enc(String(replyTarget.text||'').slice(0,90))}</span><button id="cc-reply-cancel" aria-label="Cancel reply">×</button>`:''}</div><div class="cc-typing" id="cc-typing" aria-live="polite"></div><div class="cc-compose"><button class="cc-compose-btn" id="cc-attach" title="Attach file"><svg class="ic"><use href="#i-paperclip"></use></svg></button><input type="file" id="cc-file" multiple hidden><textarea id="cc-input" data-cc-draft-key="${p.enc(draftKey)}" rows="1" placeholder="${channelReadOnly?'History only: current channel access is unavailable':'Message #'+p.enc(state.channel||'general')}" ${state.community==null||channelReadOnly?'disabled':''}>${p.enc(draft&&draft.value||'')}</textarea><button class="cc-compose-btn" id="cc-emoji" title="Emoji"><svg class="ic"><use href="#i-smile"></use></svg></button><button class="btn btn-neon" id="cc-send" ${state.community==null||channelReadOnly?'disabled':''}>Send</button></div>
      </main></div><div class="cc-join hidden" id="cc-join"><div class="cc-join-card"><div class="concord-mark">C</div><h2>Join or create a community</h2><p class="muted">Paste an Armada or other CORD-05 invite. Its # secret stays in this browser.</p><input class="input" id="cc-invite-url" inputmode="url" autocomplete="off" autocapitalize="none" spellcheck="false" placeholder="https://…/invite/naddr1…#…" value=""><div class="cc-join-actions"><button class="btn btn-ghost" id="cc-join-cancel">Cancel</button><button class="btn btn-neon" id="cc-join-go">Preview invite</button></div><div class="cc-join-alt"><span>or start your own</span><button type="button" class="btn btn-ghost" id="cc-join-create">Create a community</button></div></div></div><div class="cc-join cc-invite-sheet${pendingInvite?'':' hidden'}" id="cc-invite-sheet">${pendingInvite?invitePreviewHtml(p,pendingInvite):''}</div><div class="cc-join hidden" id="cc-create-dialog"><div class="cc-join-card"><div class="concord-mark">C</div><h2>Create a public community</h2><p class="muted">Publishes an Armada-compatible CORD community and public #general channel to your relays.</p><label class="cc-label" for="cc-community-name">Community name</label><input class="input" id="cc-community-name" maxlength="64" autocomplete="off" placeholder="My community"><label class="cc-label" for="cc-community-icon">Icon <span class="muted">(emoji or image URL)</span></label><input class="input" id="cc-community-icon" maxlength="2048" autocomplete="off" placeholder="🚀 or https://…/icon.png"><button type="button" class="btn btn-ghost cc-icon-pick" data-cc-icon-pick="cc-community-icon">Upload image…</button><div class="cc-icon-status" id="cc-community-icon-status" role="status" aria-live="polite" hidden></div><div class="cc-join-actions"><button class="btn btn-ghost" id="cc-create-cancel">Cancel</button><button class="btn btn-neon" id="cc-create-go">Create on relays</button></div></div></div><div class="cc-join hidden" id="cc-icon-dialog"><div class="cc-join-card"><div class="concord-mark">C</div><h2>Community icon</h2><p class="muted">Use an emoji or a direct HTTP(S) image URL. Leave blank to restore the initials.</p><label class="cc-label" for="cc-icon-value">Icon</label><input class="input" id="cc-icon-value" maxlength="2048" autocomplete="off" placeholder="🌌 or https://…/icon.png"><button type="button" class="btn btn-ghost cc-icon-pick" data-cc-icon-pick="cc-icon-value">Upload image…</button><div class="cc-join-actions"><button class="btn btn-ghost" id="cc-icon-cancel">Cancel</button><button class="btn btn-neon" id="cc-icon-save">Save icon</button></div></div></div>`;
    /* THE SAME SCREEN IS NOT DRAWN TWICE. "Communities: keeps flashing every few seconds" -- measured on
     * a real desktop: 46 full rebuilds of #feed in two minutes with nothing new to show, every one
     * tearing down the rail, the avatars and the messages. Three loops ask for a render whenever they
     * believe something changed (membership sync, room metadata, the background sweep -- and render()
     * itself starts the membership sync, so each rebuild scheduled the next). Whatever they believe,
     * identical markup on the live screen is nothing to draw: the bindings made by the paint that
     * produced it are still on these exact nodes, so returning here is equivalent to having drawn it. */
    /* The composer's text is left out of the comparison: the draft in the markup was READ from the
     * live box (captureComposer), so a difference there is only the person typing -- and rebuilding
     * for it replaced the text box under their fingers. */
    /* ...and the KNOWN MEMBERS list is part of what is drawn, though it is inserted after this markup: a change
     * to it alone (a kick, a join, a profile name landing) left identical markup, this returned, and the panel kept
     * the old list -- "i just kicked a member out of the community and it does not work, he still there". */
    const _key=_html.replace(/(<textarea id="cc-input"[^>]*>)[\s\S]*?(<\/textarea>)/,'$1$2')
      +'\u0001'+String(memberRows);
    if(_key===_ccLastHtml&&_ccLastRoot&&_ccLastRoot.isConnected&&_ccLastRoot.parentNode===feed)return;
    /* A FULL REBUILD, deliberately: everything outside the message list is bound with listeners that a
     * kept node would collect once per repaint (the composer's own input handlers among them). The
     * players are carried across (keepVideos -- a synchronous move does not pause or reload one), and
     * the list's rows are stamped so the next LIVE tick (patchMessageList) can keep every unchanged one. */
    const oldVideos=new Map(); try{ for(const v of feed.querySelectorAll('.cc-message video'))oldVideos.set(videoKey(v),v); }catch(_){}
    feed.innerHTML=_html;_ccLastHtml=_key;_ccLastRoot=feed.firstElementChild;
    try{ const nb=feed.querySelector('.cc-messages'); if(nb&&nb.dataset)nb.dataset.ccScrollKey=scrollKey(); }catch(_){ }
    retainCommunityRail(oldCommunityRail,feed.querySelector&&feed.querySelector('.cc-communities'));
    keepGlowsOnTheBeat(feed);
    try{ keepVideos(oldVideos,feed); }catch(_){}
    try{ const nl=feed.querySelector('.cc-message-list'); if(nl)stampList(nl); }catch(_){}
    try{
      if(openActions){
        const row=[...feed.querySelectorAll('.cc-message[data-message-id]')].find(r=>r.dataset.messageId===openActions);
        if(row){ row.classList.add('cc-actions-open'); const t=row.querySelector('[data-cc-actions]');
          if(t){ t.setAttribute('aria-expanded','true'); if(actionsFocused)t.focus({preventScroll:true}); } }
      }
    }catch(_){ }
    try{
      if(keepScroll&&keepScroll.key===scrollKey()){ const nb=feed.querySelector('.cc-messages');
        if(nb){ const st=readScroll(keepScroll.key);
          setProgrammaticScroll(nb,st.pinned!==false||keepScroll.atBottom?nb.scrollHeight:keepScroll.top); } }
      /* A SWITCHED-TO ROOM OR CHANNEL LANDS IN THIS TASK TOO. The same-room case above was already
         written here; a switch was left to restoreChatScroll's rAF, and the first frame of the new
         channel was drawn wherever the browser had put the fresh scroller (measured on a desktop:
         136px above the newest message, then a jump down) -- one of the two steps of "concord shakes
         when changing room/community". Pinned (the default for a room entered) means the bottom. */
      else if(current){ const nb=feed.querySelector('.cc-messages');
        if(nb){ const st=readScroll(scrollKey());
          setProgrammaticScroll(nb,st.pinned!==false?nb.scrollHeight:Math.max(0,Number(st.top)||0)); } }
    }catch(_){ }
    try{
      const byId=id=>feed.querySelector('[id="'+String(id).replace(/["\\]/g,'')+'"]');
      openSheets.forEach(({id,fields})=>{ const d=byId(id); if(!d)return; d.classList.remove('hidden');
        fields.forEach(([fid,v])=>{ const f=byId(fid); if(f&&v)f.value=v; }); });
      if(focusedId){ const f=byId(focusedId); if(f&&f!==document.activeElement)f.focus({preventScroll:true}); }
    }catch(_){ }
    paintUnreadBadge();
    /* The tab bar is gone: Communities is its own view, reached from the sidebar. */
    if(current){
      const conversation=p.$('.cc-conversation');
      if(conversation&&conversation.insertAdjacentHTML)conversation.insertAdjacentHTML('afterend',`<aside class="cc-members-pane${membersHidden?' hidden':''}" aria-label="Community members"><header title="People known from verified membership records and loaded messages"><b>Known members</b><span>${memberPks.length}</span></header>${memberPks.length>8?`<input class="input cc-member-search" type="search" autocomplete="off" placeholder="Search members" aria-label="Search members" value="${p.enc(memberQuery)}">`:''}<div class="cc-members-scroll">${memberRows||'<div class="cc-empty-side">No members have appeared yet.</div>'}</div>${canBanAny?'<button type="button" class="btn btn-ghost small cc-ban-npub">Ban by npub…</button>':''}</aside>`);
      feed.insertAdjacentHTML('beforeend',`<div class="cc-join hidden" id="cc-members-dialog"><div class="cc-join-card"><h2>Known members <span class="muted">${memberPks.length}</span></h2><p class="cc-member-help">People known from verified membership records and loaded messages. This is not a complete roster. Tap a member to view their profile. Right-click or hold for more options.</p>${memberPks.length>8?`<input class="input cc-member-search" type="search" autocomplete="off" placeholder="Search members" aria-label="Search members" value="${p.enc(memberQuery)}">`:''}<div class="cc-member-list">${memberRows}</div><div class="cc-join-actions">${canBanAny?'<button type="button" class="btn btn-ghost cc-ban-npub">Ban by npub…</button>':''}<button class="btn btn-ghost" id="cc-members-close">Close</button><button class="btn btn-neon" id="cc-members-invite">Invite people</button></div></div></div><div class="cc-join hidden" id="cc-settings-dialog" role="dialog" aria-modal="true" aria-labelledby="cc-settings-title"><div class="cc-join-card"><div class="cc-sheet-head"><button type="button" class="cc-sheet-back" id="cc-settings-back" aria-label="Back to the community" title="Back">‹</button><h2 id="cc-settings-title">Community settings</h2><button type="button" class="cc-sheet-close" id="cc-settings-close" aria-label="Close community settings" title="Close (Esc)"><svg class="ic" aria-hidden="true"><use href="#i-close"></use></svg></button></div><label class="cc-label" for="cc-description-value">Description</label><textarea class="input cc-settings-description" id="cc-description-value" maxlength="1000" rows="3" placeholder="What is this community about?">${p.enc(current.description||'')}</textarea><label class="cc-label" for="cc-settings-icon">Icon</label><div class="cc-icon-current" id="cc-icon-current" title="Current icon">${roomIcon(p,current,state.community)}</div><input class="input" id="cc-settings-icon" maxlength="2048" value="${p.enc(current.icon||'')}" placeholder="🌌 or https://…/icon.png"><button type="button" class="btn btn-ghost cc-icon-pick" data-cc-icon-pick="cc-settings-icon">Upload image…</button><div class="cc-icon-status" id="cc-settings-icon-status" role="status" aria-live="polite" hidden></div>${window.PCCordSpaces&&current&&!current.local&&current.cord?PCCordSpaces.bannerSettingsHtml(roomBannerUrl(current),communityPermission(p,current,'metadataManagers')):''}${timerSettingsHtml(p,current)}${retentionSettingsHtml(p,current)}${window.PCConcordGit&&current&&current.cord&&!current.local&&current.protocol!=='nip29'?PCConcordGit.settingsHtml(current,canManageChannels(p,current)):''}<div class="cc-label">#${p.enc(state.channel||'general')} visibility</div><div class="cc-visibility-line" id="cc-channel-visibility" role="note"><b>${channelPrivate?'Private — invited members only':'Public — all community members'}</b><span>Chosen when a channel is created; a private channel is a new channel, made with + (New channel).</span></div>${current&&current.cord&&current.cord.bundle&&!current.local&&!current.cord.bundle.dissolved&&current.cord.bundle.owner===(p.viewer?p.viewer().pubkey:'')?'<button type="button" class="btn btn-ghost danger cc-dissolve" id="cc-dissolve">Dissolve community…</button>':''}<div class="cc-join-actions"><div class="cc-dialog-error cc-settings-error" id="cc-settings-error" role="alert"${settingsError?'':' hidden'}>${p.enc(settingsError)}</div><button class="btn btn-ghost" id="cc-settings-cancel">Cancel</button><button class="btn btn-neon" id="cc-settings-save"${settingsSaving?' disabled aria-busy="true"':''}>${settingsSaving?'Saving…':'Save changes'}</button></div></div></div>`);
      /* SEARCH THE KNOWN MEMBERS ("will be useful for large rooms"). By name, NIP-05 or key. The query
       * is the module's, so the repaint every arriving message causes keeps it and the filtered list;
       * the box that had focus gets it back (render() re-creates it). */
      /* THE SHEETS BUILT HERE WERE NEVER REOPENED. The restore above runs before this block appends
       * the Known members dialog, room settings and the rest, so an open one came back HIDDEN on
       * every repaint: on a phone, tap into a channel and then Members, and the dialog shut 0.1s
       * later when the channel's messages arrived ("Known Members is not working"). Reopen any
       * that were open now that they exist; the restore above stays for the ones it already found. */
      try{
        const byIdLate=id=>feed.querySelector('[id="'+String(id).replace(/["\\]/g,'')+'"]');
        openSheets.forEach(({id,fields})=>{ const d=byIdLate(id); if(!d||!d.classList.contains('hidden'))return; d.classList.remove('hidden');
          fields.forEach(([fid,v])=>{ const f=byIdLate(fid); if(f&&v)f.value=v; }); });
      }catch(_){ }
      applyMemberSearch();
      if(memberFocus){ const f=p.$(memberFocus.dlg?'#cc-members-dialog .cc-member-search':'.cc-members-pane .cc-member-search');
        if(f){ f.focus({preventScroll:true}); try{ f.setSelectionRange(memberFocus.at,memberFocus.at); }catch(_){ } } }
      p.$$('.cc-member-search').forEach(inp=>inp.oninput=()=>{ memberQuery=inp.value; p.$$('.cc-member-search').forEach(o=>{ if(o!==inp)o.value=inp.value; }); applyMemberSearch(); });
      const settingsActions=p.$('#cc-settings-dialog .cc-join-actions');
      if(settingsActions&&settingsActions.insertAdjacentHTML)settingsActions.insertAdjacentHTML('afterbegin','<button class="btn btn-ghost danger" id="cc-leave-community">Leave community</button>');
      if(settingsActions&&current.cord?.bundle?.owner===p.viewer?.().pubkey&&p.sendCordDirectInvite)settingsActions.insertAdjacentHTML('afterbegin','<button class="btn btn-ghost" id="cc-private-channel">New private channel</button>');
    }
    if(p.hydrateLinkCards)p.hydrateLinkCards(feed);
    wireRoomMedia(p);
    hydrateEncryptedAttachments(messages);
    hydrateWebxdcCards(current);
    bind(me);
    if(draft){const input=p.$('#cc-input');if(input){const end=String(input.value||'').length,start=Math.max(0,Math.min(Number(draft.start)||0,end)),finish=Math.max(start,Math.min(Number(draft.end)||start,end));try{input.setSelectionRange(start,finish,draft.direction||'none');}catch(_){}if(draft.focused){const later=window.requestAnimationFrame||((fn)=>setTimeout(fn,0));later(()=>{if(input.isConnected!==false&&(!document.activeElement||document.activeElement===document.body||document.activeElement===document.documentElement))input.focus({preventScroll:true});});}}}
    if(autoOpen>=0){const identity=roomIdentity(rooms[autoOpen]);setTimeout(()=>{void activateJoinedRoom(p,autoOpen,false,identity);},0);}
    /* render() replaces the scroller on EVERY relay/history/metadata repaint, not only when the app
     * first returns to the feed. Restore after every replacement: pinned rooms land at the newest
     * message; a person who deliberately scrolled up keeps that saved offset. The old `returning`
     * gate is why entering a room briefly reached the bottom and then jumped into the middle when
     * its asynchronous history hydration rendered again. */
    if(current){restoreChatScroll();if(resumeRequested){resumeRequested=false;const identity=roomIdentity(current);setTimeout(()=>{void resumeActiveRoom(p,identity);},0);}}
  }
  /* A notification identifies content, not merely the Concord application. Select with durable
   * room identity (saved array positions change), paint the named channel, then keep looking while
   * relay history hydrates. A missing target leaves the requested room/channel open; it must never
   * silently jump to some other community. */
  function openNotification(target){
    const t=target&&typeof target==='object'?target:{},rooms=saved(),identity=String(t.community||''),
      index=rooms.findIndex(room=>roomIdentity(room)===identity);
    if(index<0)return false;
    const requested=String(t.channel||'general').slice(0,80),channels=channelsOf(rooms[index]);
    state.community=index;
    state.channel=channels.some(c=>c.name===requested)?requested:(channels[0]&&channels[0].name)||'general';
    mobileChatOpen=true;mobileDrawerOpen=false;discoveryOpen=false;
    activeSet(String(index));
    render();
    const id=String(t.message||'');
    if(!id)return true;
    let tries=0;
    const reveal=()=>{
      if(state.community!==index||state.channel!==(channels.some(c=>c.name===requested)?requested:state.channel))return;
      const row=[...document.querySelectorAll('.cc-message[data-message-id]'),...document.querySelectorAll('.ccg-row[data-message-id]')]
        .find(el=>el.dataset&&el.dataset.messageId===id);
      if(window.PCConcordGit&&PCConcordGit.openTicket(id,rooms[index]))return;
      if(row){const st=readScroll(scrollKey());st.pinned=false;row.scrollIntoView({block:'center'});row.classList.add('cc-message-target');setTimeout(()=>{if(row.isConnected)row.classList.remove('cc-message-target');},2400);return;}
      if(++tries<50)setTimeout(reveal,100);
    };
    reveal();
    /* Polling the DOM cannot create history. A cold/background launch commonly has only the saved
     * encrypted bundle, so follow the same invite/control/chat hydration path as a real room click.
     * Keep the requested channel across the metadata repaint and never let a late result take over
     * after the reader selected another community. */
    if(rooms[index]&&rooms[index].cord&&!rooms[index].cord.hydrated){
      (async()=>{try{
        await hydrateRoomStreams(PC(),index,identity);
        if(state.community!==index||roomIdentity(saved()[index])!==identity)return;
        const fresh=saved()[index],available=channelsOf(fresh);
        if(available.some(c=>c.name===requested))state.channel=requested;
        render();reveal();
      }catch(e){console.warn('Concord notification room hydration failed',e);}})();
    }
    return true;
  }
  function bind(me){
    const p=PC(), $=p.$, $$=p.$$;
    /* bind() is a sibling of render(), not its closure. Member actions must derive their own
     * viewer/owner state here; referring to render's `isOwner` made right-click throw immediately. */
    const viewer=p.viewer?p.viewer():{},boundRoom=saved()[state.community],
      boundOwnerPk=String((boundRoom&&boundRoom.cord&&boundRoom.cord.bundle&&
        (boundRoom.cord.bundle.owner||boundRoom.cord.bundle.creator_npub))||''),
      isOwner=!!boundOwnerPk&&boundOwnerPk===viewer.pubkey;
    const scroller=document.querySelector('.cc-messages'); if(scroller){ scroller.onscroll=()=>{ if(scroller.dataset.osParking||programmaticScrollEvent(scroller)||!scroller.isConnected||!document.body.classList.contains('concord-view'))return; const key=scrollKey(),st=readScroll(key); st.top=scroller.scrollTop;st.height=scroller.scrollHeight;st.pinned=scroller.scrollHeight-scroller.scrollTop-scroller.clientHeight<80;writeScroll(key,st); }; scroller.querySelectorAll('a').forEach(a=>a.addEventListener('pointerdown',()=>{ const key=scrollKey(),st=readScroll(key); st.top=scroller.scrollTop;st.height=scroller.scrollHeight;st.pinned=scroller.scrollHeight-scroller.scrollTop-scroller.clientHeight<80;writeScroll(key,st); },{passive:true})); scroller.addEventListener('click',e=>{const a=e.target&&e.target.closest&&e.target.closest('a[href]');if(!a||!inviteParts(a.href))return;e.preventDefault();e.stopPropagation();openInviteLink(a.href);},true);watchPinnedRoomGrowth(scroller); }
    const openJoin=()=>{ $('#cc-join').classList.remove('hidden'); setTimeout(()=>$('#cc-invite-url').focus(),20); };
    const home=$('#cc-home'); if(home)home.onclick=()=>{ const rooms=saved(),wanted=Number(activeGet()||0); discoveryOpen=!rooms.length; state.community=rooms.length&&wanted>=0&&wanted<rooms.length?wanted:(rooms.length?0:null); state.channel=state.community==null?null:'general'; mobileChatOpen=false; mobileDrawerOpen=false; render(); };
    const discovery=$('#cc-discovery'); if(discovery)discovery.onclick=()=>{ discoveryOpen=true; state.community=null; state.channel=null; mobileChatOpen=false; mobileDrawerOpen=false; render(); };
    ['#cc-add','#cc-welcome-join'].forEach(s=>{ const b=$(s); if(b)b.onclick=openJoin; });
    { const b=$('#cc-check-memberships'); if(b)b.onclick=()=>showMembershipCheck(p); }
    { const b=$('#cc-discover-retry'); if(b)b.onclick=()=>{ stopDiscovery(); discoveryLoaded=false; discoveryUnreachable=''; render(); }; }
    /* CREATING A COMMUNITY WAS REACHABLE ONLY WHILE YOU HAD NONE.
     *
     * "Create community" lives on the `state.community==null` discover pane, and the moment you
     * join or create anything that pane is replaced by the message list — so the button ceased to
     * exist and the only way back was the unlabelled ◎ in the rail, whose title says "Discover
     * public communities". Reported as "i don;t see a way to create a new Concord room": the
     * feature was there and nothing on screen said so.
     *
     * The rail's + now advertises both, and the join sheet offers the other half. One opener for
     * all three entry points, because three copies of "show the dialog and focus the name" is how
     * two of them come to disagree about which. */
    const openCreate=()=>{ const dlg=$('#cc-create-dialog'); if(!dlg)return;
      $('#cc-join').classList.add('hidden');
      dlg.classList.remove('hidden'); setTimeout(()=>{const n=$('#cc-community-name'); if(n)n.focus();},20); };
    ['#cc-create','#cc-join-create'].forEach(sel=>{ const b=$(sel); if(b)b.onclick=openCreate; });
    const createCancel=$('#cc-create-cancel'); if(createCancel)createCancel.onclick=()=>$('#cc-create-dialog').classList.add('hidden');
    const createGo=$('#cc-create-go'); if(createGo)createGo.onclick=async()=>{ const name=String($('#cc-community-name').value||'').trim(); if(!name){ p.toast('name your community'); return; } createGo.disabled=true; try{ p.toast('creating encrypted community…'); const icon=normalizeIcon($('#cc-community-icon').value); let room=await mintPublicRoom(p,name,/^(https?|blob):/i.test(icon)?'':icon); const pending=room.inviteListPending||''; delete room.inviteListPending; const a=saved(); a.push(room); save(a);/* The creator already has the freshly generated control/channel state. Treat this renderer as hydrated so the first paint is not delayed by reading the just-published room back from relays. */hydratedRoomViews.add(roomIdentity(room));state.community=a.length-1; state.channel='general';room=a[state.community];render(); await persistArmadaMembership(p,room); p.copyValue(shareableInviteUrl(p,room.url)); p.toast(pending?'public community created — invite link copied; '+pending:'public community created — invite link copied'); if(/^(https?|blob):/i.test(icon))void saveCommunitySettings(p,room,{name:room.name,description:'',icon}).then(()=>render()).catch(e=>p.toast('the community icon was not saved: '+(e&&e.message||e))); }catch(e){ createGo.disabled=false; p.toast('community creation failed: '+(e&&e.message||e)); } };
    const editIcon=$('#cc-edit-icon'); if(editIcon)editIcon.onclick=()=>{ $('#cc-settings-dialog').classList.remove('hidden'); setTimeout(()=>$('#cc-description-value').focus(),20); };
    /* From the device: the picked file becomes a blob: URL in the same box, and saving turns it into
     * the encrypted ImageRef (sealImageRef) — one path for a typed URL and an upload. */
    $$('[data-cc-icon-pick]').forEach(b=>b.onclick=()=>{ const f=document.createElement('input');f.type='file';f.accept='image/*';f.onchange=()=>{const file=f.files&&f.files[0],box=document.getElementById(b.dataset.ccIconPick);if(!file||!box)return;if(!/^image\//.test(file.type||'')){p.toast('choose an image');return;}if(file.size>5*1024*1024){p.toast('that image is too large (5 MB max)');return;}box.value=URL.createObjectURL(file);box.dispatchEvent(new Event('input',{bubbles:true}));startIconUpload(p,box.value);};f.click(); });
    for(const id of ICON_BOXES){try{const box=$('#'+id);if(box&&box.dataset&&box.addEventListener&&!box.dataset.ccIconWatch){box.dataset.ccIconWatch='1';box.addEventListener('input',()=>paintIconStatus(id));}paintIconStatus(id);}catch(e){console.warn('Concord icon status failed',e);}}
    const iconCancel=$('#cc-icon-cancel'); if(iconCancel)iconCancel.onclick=()=>$('#cc-icon-dialog').classList.add('hidden');
    /* The compact icon dialog used to mutate only this renderer's localStorage and immediately say
       "updated". The next control-stream hydration correctly restored relay metadata, so the icon
       appeared to randomly disappear (and another device never saw it at all). Funnel both icon
       entry points through the authoritative Community settings publisher. */
    const iconSave=$('#cc-icon-save'); if(iconSave)iconSave.onclick=()=>{ const target=$('#cc-settings-icon'),saveButton=$('#cc-settings-save'); if(!target||!saveButton)return; target.value=normalizeIcon($('#cc-icon-value').value); $('#cc-icon-dialog').classList.add('hidden'); return saveButton.click(); };
    /* The composer is looked up AT THE PICK, never captured at the open: a message arriving while the
       picker is open repaints the room and replaces #cc-input, and the pick then went into the
       detached textarea -- the emoji you chose simply never appeared. */
    const emoji=$('#cc-emoji'), input=$('#cc-input'); if(emoji&&input)emoji.onclick=()=>{ if(p.openEmojiPopover)p.openEmojiPopover(emoji,(value,close)=>{ if(close)close(); const box=$('#cc-input')||input; if(p.insertAt)p.insertAt(box,value); else box.value+=value; box.focus(); }); };
    let mentionChoices=[...(activeMentionState.choices||[])],mentionIndex=Number(activeMentionState.index)||0;
    const mentionRecipients=new Map(activeMentionState.recipients||[]);
    const syncMentionState=()=>{activeMentionState={choices:[...mentionChoices],index:mentionIndex,recipients:new Map(mentionRecipients)};};
    /* THE PICKER HAD NO PICTURE, AND ON A PHONE THAT MEANS IT DOES NOT EXIST.
     *
     * `mentionChoices` was module state and nothing ever painted it: the only way to choose was
     * `input.onkeydown` — ArrowUp/ArrowDown to move, Tab or Enter to accept. A phone keyboard has
     * no arrows and no Tab, and Enter sends the message, so typing `@` in a room did nothing
     * whatsoever and there was nothing on screen to say a list existed. Reported as "@ tagging is
     * not working at all on mobile".
     *
     * The list is drawn above the composer and every row is a real target. Keyboard selection is
     * untouched — the highlighted row is the SAME `mentionIndex` the arrows move, so the two ways
     * of choosing cannot disagree. */
    const closeMentions=()=>{ mentionChoices=[];syncMentionState();paintMentionPicker(); };
    function paintMentionPicker(){
      const anchor=document.querySelector('.cc-compose');
      let list=document.querySelector('.cc-mentions');
      if(!mentionChoices.length||!anchor){ if(list)list.remove(); return; }
      if(!list||list.parentElement!==anchor.parentElement){
        if(list)list.remove();
        list=document.createElement('div');
        list.className='cc-mentions'; list.setAttribute('role','listbox');
        list.setAttribute('aria-label','Mention a member');
        anchor.parentElement.insertBefore(list,anchor);
        /* ONE delegated pair, bound once — the rows are rebuilt on every keystroke, so per-row
         * handlers would be re-attached hundreds of times and lost in between.
         *
         * `pointerdown` is the event, not `mousedown`: it is the one a finger, a mouse and a pen
         * all send. preventDefault there keeps the textarea focused (and the phone keyboard up)
         * without cancelling `pointerup`, so the choice is made on RELEASE and a drag that was
         * meant to scroll this list does not tag somebody. */
        let sx=0,sy=0,moved=false;
        list.addEventListener('pointerdown',ev=>{ if(!ev.target.closest||!ev.target.closest('[data-cc-mention]'))return;
          sx=ev.clientX;sy=ev.clientY;moved=false;ev.preventDefault(); });
        list.addEventListener('pointermove',ev=>{ if(Math.abs(ev.clientX-sx)>8||Math.abs(ev.clientY-sy)>8)moved=true; });
        list.addEventListener('pointerup',ev=>{ const row=ev.target.closest&&ev.target.closest('[data-cc-mention]');
          if(!row||moved)return; ev.preventDefault(); acceptMention(Number(row.dataset.ccMention)||0); });
      }
      list.innerHTML=mentionChoices.map((choice,i)=>{
        const pr=p.profOf?p.profOf(choice.pk):{};
        return `<button type="button" role="option" class="cc-mention-opt${i===mentionIndex?' on':''}"`
          +` aria-selected="${i===mentionIndex?'true':'false'}" data-cc-mention="${i}">`
          +`<img src="${p.enc(pr.picture||p.LOGO||'')}" alt="">`
          +`<span>@${p.enc(choice.name)}</span></button>`;
      }).join('');
      const on=list.querySelector('.cc-mention-opt.on');
      if(on&&on.scrollIntoView)try{ on.scrollIntoView({block:'nearest'}); }catch(_){ }
    }
    const mentionToken=()=>{ const before=input.value.slice(0,input.selectionStart); return before.match(/(?:^|\s)@([\w.-]*)$/); };
    const drawMentions=()=>{ const match=mentionToken(); if(!match){closeMentions();return;} const room=saved()[state.community],viewer=p.viewer?p.viewer():{},pks=roomParticipants(room,viewer.pubkey),q=match[1].toLowerCase(); mentionChoices=pks.map(pk=>{const pr=p.profOf?p.profOf(pk):{},name=String(pr.display_name||pr.name||(pk===viewer.pubkey?me:pk.slice(0,12)));return {pk,name,aliases:mentionAliases(pr,pk,name)};}).filter(x=>!q||[...x.aliases].some(alias=>alias.includes(q))).slice(0,8); if(!mentionChoices.length){closeMentions();return;} mentionIndex=Math.min(mentionIndex,mentionChoices.length-1);syncMentionState();paintMentionPicker(); };
    const acceptMention=(i=mentionIndex)=>{ const match=mentionToken(),choice=mentionChoices[i]; if(!match||!choice)return false; const handle=choice.name.replace(/\s+/g,'_'),end=input.selectionStart,start=end-match[1].length-1;mentionRecipients.set(handle.toLowerCase(),choice.pk);input.setRangeText('@'+handle+' ',start,end,'end'); closeMentions();syncMentionState(); input.focus(); return true; };
    if(input&&input.addEventListener){input.addEventListener('input',drawMentions);input.addEventListener('input',()=>{if(String(input.value||'').trim())sendTyping(p);});}
    const attach=$('#cc-attach'), file=$('#cc-file');
    const insertBlossomAttachment=({url,type,ext})=>{ const input=liveComposer(); if(!url||!input)return; const mime=String(type||'application/octet-stream'),raw=String(url).split(/[?#]/)[0].split('/').pop()||'file',name=raw+(ext&&!raw.includes('.')?'.'+ext:''); let tag=['imeta',`url ${url}`,`m ${mime}`,`name ${name.slice(0,120)}`]; if(mime==='application/x-webxdc'||mime==='application/webxdc+zip'||mime==='application/vnd.webxdc+zip'){const topic=mintWebxdcTopic();tag.push(`webxdc-topic ${topic}`,`webxdc ${topic}`,`summary ${name.replace(/\.xdc$/i,'').slice(0,80)}`);} pendingAttachments.set(url,tag); input.value+=(input.value&&!/\s$/.test(input.value)?' ':'')+url; input.dispatchEvent(new Event('input',{bubbles:true})); input.focus(); };
    if(attach&&file)attach.onclick=()=>{ if(!p.blossomPicker||!p.modal){file.click();return;} p.modal(`<h3>Attach to #${p.enc(state.channel||'general')}</h3><p class="muted">Choose a new file from this device or reuse one from Files.</p><div class="cc-attach-choices"><button class="btn btn-ghost" id="cc-attach-device">From device</button><button class="btn btn-neon" id="cc-attach-blossom">📁 Files</button></div>`,root=>{ const local=root.querySelector('#cc-attach-device'),blossom=root.querySelector('#cc-attach-blossom'); local.onclick=()=>{p.closeModal();file.click();}; blossom.onclick=()=>{p.closeModal();p.blossomPicker(null,insertBlossomAttachment,{title:'📁 Attach from Files'});}; }); };
    const uploadAttachments=async files=>{ for(const f of files){ if(f.size>CONCORD_UPLOAD_MAX){ p.toast(f.name+' is too large ('+Math.round(CONCORD_UPLOAD_MAX/1048576)+' MB max)'); continue; } try{ p.toast('uploading '+f.name+'…'); const isXdc=/\.xdc$/i.test(f.name)||f.type==='application/x-webxdc'||f.type==='application/webxdc+zip'||f.type==='application/vnd.webxdc+zip',bytes=isXdc?new Uint8Array(await f.arrayBuffer()):null,clean=(!isXdc&&window.PCExifStrip&&window.PCExifStrip.cleanFile)?await window.PCExifStrip.cleanFile(f).catch(()=>f):f,sealed=isXdc||!(saved()[state.community]||{}).cord||(saved()[state.community]||{}).protocol==='nip29'?null:await sealAttachment(clean),url=await p.uploadBlob(sealed?sealed.blob:clean,{keep:true,ciphertext:!!sealed}),mime=isXdc?'application/vnd.webxdc+zip':String(clean.type||'application/octet-stream'),tag=sealed?attachmentImeta(url,mime,String(clean.name||'file'),sealed):['imeta',`url ${url}`,`m ${mime}`,`name ${String(clean.name||'file').slice(0,120)}`]; if(isXdc){ const sha=bytesHex(await crypto.subtle.digest('SHA-256',bytes)),topic=mintWebxdcTopic(),name=f.name.replace(/\.xdc$/i,'').slice(0,80);tag.push(`x ${sha}`,`webxdc-topic ${topic}`,`webxdc ${topic}`,`summary ${name}`); }pendingAttachments.set(url,tag); const box=liveComposer(); if(!box)throw new Error('the composer went away while '+f.name+' was uploading'); box.value+=(box.value&&!/\s$/.test(box.value)?' ':'')+url; box.dispatchEvent(new Event('input',{bubbles:true})); }catch(e){ p.toast('could not attach '+f.name+(e&&e.message?': '+e.message:'')); } } };
    if(file&&input)file.onchange=async()=>{ await uploadAttachments([...file.files]); file.value=''; };
    /* PASTE IS BOUND TO THE DOCUMENT, NOT TO A CAPTURED #cc-input.
     *
     * `bind()` runs after a render, and `input` here is the element as it was at that moment — but
     * this pane re-renders on every arriving message, profile, icon and decrypted attachment, and
     * each render REPLACES the composer node. The handler then sat on a node no longer in the
     * document and Ctrl+V did nothing at all, silently: reported as "i tried ctrl v in concord chat
     * room just now, where the fuck is the image!". It is the same detached-node bug the ATTACH
     * button had, and `insertBlossomAttachment` above already shows the cure — resolve the composer
     * when the event happens (`liveComposer()`), never when the handler is installed.
     *
     * `clipboardData.files` as well as `.items`: a screenshot pasted from some platforms arrives
     * only in `files`, and reading one list alone is why a paste can work in one browser and not
     * another. Bound ONCE (the flag), because bind() runs per render and stacking a listener per
     * render would upload the same screenshot N times. */
    /* The LISTENER is installed once; the UPLOADER it calls is refreshed on every bind. Closing
     * over the first render's `uploadAttachments` would pin the room and channel it was built for,
     * so a screenshot pasted after switching rooms would upload into the previous one — a stale
     * closure is exactly the bug this whole fix is about, one level up. */
    _cordPasteUpload = uploadAttachments;
    if(!document.__pcCordPaste){
      document.__pcCordPaste = true;
      document.addEventListener('paste', event=>{
        const composer = liveComposer();
        if(!composer) return;
        /* WHOSE PASTE IS IT.  Requiring the composer to be FOCUSED was too strict for the way the
           screen is actually used: you open a room, hit Ctrl+V, and the textarea has never been
           clicked — so the paste was ignored and the report was the same as before the fix ("i
           tried ctrl v in concord chat room just now, where the fuck is the image!").  A paste
           belongs to this composer when the composer has focus, OR when nothing else does — the
           document body and the room's own scroll container are not editors and hold no text a
           person could be pasting into.  Any other input, textarea or contenteditable keeps it. */
        const at = document.activeElement, tag = at && at.tagName;
        const elsewhere = at && at !== composer &&
          (tag === 'INPUT' || tag === 'TEXTAREA' || at.isContentEditable === true);
        if(elsewhere) return;
        const cd = event.clipboardData; if(!cd) return;
        const seen = new Set(), images = [];
        for(const f of [...(cd.files||[])]){
          if(f && String(f.type||'').startsWith('image/')){ images.push(f); seen.add(f.name+':'+f.size); }
        }
        for(const item of [...(cd.items||[])]){
          if(!item || item.kind!=='file' || !String(item.type||'').startsWith('image/')) continue;
          const f = item.getAsFile && item.getAsFile();
          if(f && !seen.has(f.name+':'+f.size)) images.push(f);
        }
        if(!images.length) return;
        event.preventDefault();
        if(_cordPasteUpload) void _cordPasteUpload(images);
      });
    }
    const members=$('#cc-members'); if(members)members.onclick=()=>{if(!window.matchMedia||window.matchMedia('(max-width:820px)').matches){$('#cc-members-dialog').classList.remove('hidden');return;}const pane=$('.cc-members-pane');if(!pane)return;const hide=localStorage.getItem('pc.concord.members.hidden')!=='1';pane.classList.toggle('hidden',hide);localStorage.setItem('pc.concord.members.hidden',hide?'1':'0');};
    /* A PHONE HAS ROOM FOR FOUR HEADER BUTTONS, NOT TWELVE. "no way to see concord room members on
     * android": Members was the LAST of a dozen buttons and the header ran out of screen before it.
     * At phone width (client.css) the room's secondary actions fold into this ⋯ and Members stays in
     * the header; each menu row presses the real (hidden) button, so there is one handler per action. */
    { const more=$('#cc-head-more'); if(more)more.onclick=()=>{
        // Call too, when the header has no room for it (under 370px it is hidden): a phone that cannot see
        // the 📞 must still be able to start a call.
        const callEl=$('#cc-call'), callHidden=callEl&&!callEl.getBoundingClientRect().width;
        const rows=[...(callHidden?[['cc-call','Start a call']]:[]),['ccg-head-issues','Issues and pull requests'],['cc-members','Members'],['cc-direct-send','Invite an account'],
          ['cc-manage-links','Invitation links'],['cc-direct-inbox','Review invitations'],['cc-publish-listing','Publish to Armada Discover'],
          ['cc-leave-shortcut','Leave community']].filter(([id])=>!!$('#'+id));
        if(!p.openMenuPopover||!rows.length)return;
        p.openMenuPopover(more,rows,a=>{ const b=$('#'+a); if(b)b.click(); });
      }; }
    const membersClose=$('#cc-members-close'); if(membersClose)membersClose.onclick=()=>$('#cc-members-dialog').classList.add('hidden');
    $$('.cc-ban-npub').forEach(b=>{b.onclick=async()=>{
      const room=saved()[state.community],owner=String(room?.cord?.bundle?.owner||'');
      const raw=p.uiPrompt?await p.uiPrompt('Ban by npub — paste the account\'s npub',{value:'',ok:'Continue'}):'';
      const got=banTargetFromInput(raw,{viewer:viewer.pubkey,owner,decode:v=>window.NostrTools.nip19.decode(v)});
      if(!got.target){if(got.error)p.toast(got.error);return;}
      const isOwnerNow=!!owner&&owner===viewer.pubkey;
      void (isOwnerNow?banMember(got.target):staffModerate(got.target,'ban'));
    };});
    const banMember=async target=>{
      const actor=deliveryOwner(p),room=saved()[state.community];if(!room?.cord?.bundle)return p.toast('Community moderation is not ready.');
      if(room.cord.refounding)return p.toast('Resume the pending key rotation in community settings first.');
      try{
        /* "why is banning UI so complex? I only care about the person I am banning" / "a simple button to ban like
         * every other program" (2026-10-16). The only decision IS who is banned: everyone else keeps exactly the access
         * they have -- the community to everyone on Known members, each private channel to the members its channel
         * role is granted to (CORD's own rule: privateChannelInvite refuses anyone without it). quickBanReview. */
        const reviewed=await quickBanReview(p,room,target);if(!reviewed)return;
        if(deliveryOwner(p)!==actor)throw new Error('The signing account changed during access review.');
        /* A REMOTE SIGNER CANNOT ROTATE THE KEYS, AND THAT MUST NOT MEAN "NO BAN". Rotation wraps raw 40-136 byte
         * secrets with NIP-44 (CORD-06), and NIP-46/NIP-07/NIP-55 only encrypt TEXT -- so the owner's ban, which is
         * a rotation, failed before anything was published and the member stayed ("Dr Todd still appears in known
         * members despite me banning him", 2026-10-16, signed in through a remote signer). The ban itself is a
         * control-plane write the owner may always sign: publish that, and say the keys were not rotated. */
        /* THE BAN IS PUBLISHED FIRST, AND KEY ROTATION CAN NEVER UNDO IT. Rotation reads the whole control history from
         * EVERY relay the community lists before it will start, and PosterChan's lists a relay this server refuses
         * (relay.ditto.pub) -- so the owner's ban failed with "every control relay must complete before refounding"
         * and nothing was published: "banning todd failed again!" (2026-10-16). The ban is the control-plane write
         * the owner may always sign; rotation, when this signer and these relays allow it, follows it. */
        await publishModeration(room,target,'ban'); render();
        if(!(await canRotateKeys(p))){ p.toast('Member banned. The community keys were not rotated: this signer cannot do it (sign in with your key on this device to rotate them).'); return; }
        try{ await refoundRoom(p,saved()[state.community]||room,{...reviewed,banTarget:target}); render(); p.toast('Member banned and community keys rotated.'); }
        catch(e){ render(); p.toast('Member banned. The community keys were not rotated: '+(e&&e.message||e)); }
      }catch(e){p.toast('Community moderation needs attention: '+(e.message||e));}
    };
    /* KICK (anyone holding KICK, the owner included) and a STAFF BAN (a holder of BAN who is not the
     * owner): Vector's delegated moderation. A kick removes the member from the guestbook — they can
     * rejoin with an invite; a ban stops them for good. The owner's ban stays banMember above, which
     * also rotates the keys (only the owner can refound). */
    /* Publish a kick or a ban as one control/guestbook write (no key rotation); throws when it did not land. */
    const publishModeration=async(room,target,verb)=>{
        const reader=window.PosterCordReader,bundle=room?.cord?.bundle,me=viewer.pubkey;
        const loadKey=room.communityId||room.naddr,relays=roomRelays(bundle),wraps=roomControls.get(loadKey)||[];
        const scope=cordPlaneContext(p,bundle,wraps,room);
        const made=verb==='kick'?await reader.createKickWrap(bundle,wraps,target,me,p.signTemplate):await reader.createBanWrap(bundle,wraps,target,me,p.signTemplate);
        if(!scope.current())throw new Error('Concord membership changed while signing');
        const r=await p.relayPublishRoom(relays,made.wrap,cordPlaneAuth(p,scope,made.wrap.pubkey,relays));
        if(!r?.ok)throw new Error('the community relays did not accept it');
        if(verb==='ban')roomControls.set(loadKey,[...wraps,made.wrap]);
        /* A KICK IS A GUESTBOOK EVENT, and the member list is folded from the guestbook THIS client holds -- which
         * is re-read at most once a minute and never contained our own kick. So the kicked member stayed on the
         * list ("i just kicked a member out of the community and it does not work, he still there", 2026-10-16).
         * The accepted kick joins the held guestbook (and its cache) at once, exactly as if it had been read back. */
        if(verb==='kick')noteOwnGuestbookEvent(room,made.wrap,deliveryOwner(p));
        if(verb==='ban'){ const rooms=saved(),at=rooms.findIndex(r=>roomIdentity(r)===roomIdentity(room));
          if(at>=0){ const banned=[...new Set([...(rooms[at].banned||[]),target])]; rooms[at]={...rooms[at],banned}; save(rooms); } }
    };
    const staffModerate=async(target,verb)=>{
      const room=saved()[state.community],reader=window.PosterCordReader,bundle=room?.cord?.bundle;
      if(!bundle||!reader)return p.toast('Community moderation is not ready.');
      const name=banLabel(target,p.profOf&&p.profOf(target),(()=>{try{return window.NostrTools.nip19.npubEncode(target);}catch(_){return '';}})());
      const ok=p.uiConfirm?await p.uiConfirm(verb==='kick'?`Kick ${name}? They can rejoin with an invite.`:`Ban ${name} from this community?`,{ok:verb==='kick'?'Kick':'Ban',danger:true}):true; if(!ok)return;
      try{
        await publishModeration(room,target,verb);
        render(); p.toast(verb==='kick'?'Member kicked.':'Member banned.');
      }catch(e){ p.toast('Community moderation needs attention: '+(e&&e.message||e)); }
    };
    /* ROLES (Vector's Admin / Moderator): reuse the community's server role of that name, creating it
     * first if nobody has (vsk 1), then write the member's grant (vsk 3). A role with staff control
     * bits delivers the control key to the member inside the grant, which needs binary NIP-44 — the
     * local key signer has it, a remote signer is told so. */
    const setMemberRole=async(target,preset)=>{
      const room=saved()[state.community],r=window.PosterCordReader,bundle=room?.cord?.bundle,me=viewer.pubkey;
      if(!bundle||!r?.createRoleWrap)return p.toast('Community roles are not ready.');
      try{
        const loadKey=room.communityId||room.naddr,relays=roomRelays(bundle);let wraps=roomControls.get(loadKey)||[];
        const publish=async made=>{const scope=cordPlaneContext(p,bundle,wraps,room);if(!scope.current())throw new Error('Concord membership changed while signing');const res=await p.relayPublishRoom(relays,made.wrap,cordPlaneAuth(p,scope,made.wrap.pubkey,relays));if(!res?.ok)throw new Error('the community relays did not accept it');wraps=[...wraps,made.wrap];roomControls.set(loadKey,wraps);};
        let info=r.inspectControl(bundle,wraps);const staff=info.roles.filter(x=>x.scope==='server'&&(x.name==='Admin'||x.name==='Moderator')).map(x=>x.id);
        let roleId='';
        if(preset){ const name=preset==='admin'?'Admin':'Moderator',have=info.roles.filter(x=>x.scope==='server'&&x.name===name).sort((a,b)=>a.position-b.position)[0];
          if(have)roleId=have.id; else { const made=await r.createRoleWrap(bundle,wraps,preset,me,p.signTemplate); await publish(made); roleId=made.roleId; } }
        info=r.inspectControl(bundle,wraps);
        const ids=[...(info.grants[target]||[]).filter(id=>!staff.includes(id)),...(roleId?[roleId]:[])];
        const made=await r.createRoleGrantWrap(bundle,wraps,target,ids,me,p.signTemplate,p.cordRekeyEncrypt);
        await publish(made); render();
        p.toast(preset?'Role granted.':'Role removed.');
      }catch(e){ p.toast('The role was not changed: '+(e&&e.message||e)); }
    };
    const closeMemberMenu=()=>{const old=document.querySelector('.cc-member-menu');if(old)old.remove();};
    /* THE MENU IS PLACED IN SCREEN PIXELS AND WRITTEN IN THE PAGE'S. A PosterChanOS desk runs the client at a UI
     * scale (body{zoom:1.25} on a 4K screen): getBoundingClientRect/innerWidth are screen pixels, but a fixed
     * element's left/top are scaled by the zoom -- so the member list's right edge (x 1249) became left 1495 in a
     * 1386-wide window and the menu opened off-screen: "the right click on person in Known Members in concord
     * don't work ... it came back!" (2026-10-16; the old test ran at zoom 1). Measured with its real size, it
     * opens beside the row, flips to the row's left when the right has no room, and is clamped into the window. */
    const placeMemberMenu=(menu,rect,event)=>{const zv=parseFloat(getComputedStyle(document.body).zoom),z=zv>0&&isFinite(zv)?zv:1,m=menu.getBoundingClientRect(),W=window.innerWidth,H=window.innerHeight;let x=rect?rect.right+6:(event.clientX||12);if(x+m.width>W-8)x=rect?rect.left-m.width-6:W-m.width-8;x=Math.max(8,Math.min(x,W-m.width-8));const y=Math.max(8,Math.min(rect?rect.top:(event.clientY||12),H-m.height-8));menu.style.left=(x/z)+'px';menu.style.top=(y/z)+'px';};
    const openMemberMenu=(event,target)=>{closeMemberMenu();const _room=saved()[state.community],notSelf=target!==viewer.pubkey&&target!==boundOwnerPk,canBan=notSelf&&(isOwner||memberMay(_room,viewer.pubkey,'BAN')),canKick=notSelf&&!_room?.local&&(isOwner||memberMay(_room,viewer.pubkey,'KICK')),canRoles=notSelf&&!_room?.local&&(isOwner||memberMay(_room,viewer.pubkey,'MANAGE_ROLES')),heldRoles=canRoles?memberRoleNames(_room,target):[],canMessage=target!==viewer.pubkey,menu=document.createElement('div');menu.className='cc-member-menu';menu.setAttribute('role','menu');menu.innerHTML=`<button data-cc-member-profile="${p.enc(target)}" role="menuitem">View profile</button>${canMessage?`<button data-cc-member-message="${p.enc(target)}" role="menuitem">Message</button>`:''}${canRoles?`${heldRoles.includes('Moderator')?'':`<button data-cc-member-role="moderator" role="menuitem">Make moderator</button>`}${heldRoles.includes('Admin')||!isOwner?'':`<button data-cc-member-role="admin" role="menuitem">Make admin</button>`}${heldRoles.length?`<button data-cc-member-role="" role="menuitem">Remove ${p.enc(heldRoles.join(' / '))} role</button>`:''}`:''}${canKick?`<button class="danger" data-cc-member-kick="${p.enc(target)}" role="menuitem">Kick from community</button>`:''}${canBan?`<button class="danger" data-cc-member-ban="${p.enc(target)}" role="menuitem">Ban from community</button>`:''}`;document.body.appendChild(menu);const anchor=event.currentTarget||(event.target&&event.target.closest&&event.target.closest('[data-cc-member]')),rect=anchor&&anchor.getBoundingClientRect?anchor.getBoundingClientRect():null;placeMemberMenu(menu,rect,event);menu.querySelector('[data-cc-member-profile]').onclick=()=>{closeMemberMenu();if(p.openProfile)p.openProfile(target);};const message=menu.querySelector('[data-cc-member-message]');if(message)message.onclick=()=>{closeMemberMenu();if(p.messageUser)p.messageUser(target);};const ban=menu.querySelector('[data-cc-member-ban]');if(ban)ban.onclick=()=>{closeMemberMenu();void (isOwner?banMember(target):staffModerate(target,'ban'));};menu.querySelectorAll('[data-cc-member-role]').forEach(b=>b.onclick=()=>{closeMemberMenu();void setMemberRole(target,b.dataset.ccMemberRole);});const kick=menu.querySelector('[data-cc-member-kick]');if(kick)kick.onclick=()=>{closeMemberMenu();void staffModerate(target,'kick');};setTimeout(()=>document.addEventListener('pointerdown',e=>{if(!menu.contains(e.target))closeMemberMenu();},{once:true}),0);};
    openMemberMenuNow=openMemberMenu;wireAuthorMenu();
    $$('[data-cc-member]').forEach(row=>{const target=row.dataset.ccMember;let held=null,longPressed=false;row.onclick=e=>{e.preventDefault();/* Android/iOS synthesize click after a completed long press. Consume that click or the menu is immediately replaced by Profile. Resolve the viewport now, not when this row was rendered: rotation and desktop window resizing can cross the responsive boundary without causing a Concord repaint. */const action=memberTapAction(memberViewportIsNarrow(),longPressed);longPressed=false;if(action==='consume')return;if(action==='profile'){if(p.openProfile)p.openProfile(target);return;}openMemberMenu(e,target);};row.oncontextmenu=e=>{e.preventDefault();longPressed=false;openMemberMenu(e,target);};row.onpointerdown=e=>{if(e.pointerType==='mouse')return;longPressed=false;held=setTimeout(()=>{held=null;longPressed=true;openMemberMenu(e,target);},550);};row.onpointerup=row.onpointercancel=row.onpointermove=()=>{if(held){clearTimeout(held);held=null;}};});
    /* CREATE A CHANNEL. Same publish path as the community-profile save below: read the control
     * plane, write one control event, publish it to the room's relays under a plane auth. The room
     * is NOT rewritten from the result — `reconcileChannels` already refuses to do that, because a
     * partial control read has deleted whole channel lists before. The new channel is appended to
     * the saved list and the next control read confirms it. */
    const addChannel=$('#cc-add-channel'); if(addChannel)addChannel.onclick=async()=>{
      const rooms=saved(),room=rooms[state.community],roomId=roomIdentity(room);
      if(!room||!room.cord||!room.cord.bundle)return;
      const chosen=window.PCCordSpaces?await PCCordSpaces.askNewChannel({ok:'Create channel',git:!!window.PCConcordGit}):{name:String(await (p.uiPrompt?p.uiPrompt('Name the new channel',{value:'',ok:'Create channel'}):'')||''),view:'chat'};
      /* ARMADA'S REPOSITORY CHANNEL: the repository is attached in the channel's FIRST edition. */
      let repository=null;
      if(chosen&&chosen.git&&window.PCConcordGit){
        const connected=new Set((room.channels||[]).flatMap(c=>PCConcordGit.attachmentsOf(c).filter(a=>a.detachedAt===undefined).map(a=>a.address.coordinate)));
        const picked=await PCConcordGit.pickRepository({connected});if(!picked)return;
        repository={address:picked.address.coordinate,relayHints:picked.relayHints};
        if(!String(chosen.name||'').trim())chosen.name=String(picked.address.identifier||'').toLowerCase().replace(/[^a-z0-9._-]+/g,'-').slice(0,64);
      }
      const name=String(chosen&&chosen.name||'').trim().replace(/^#/,'').slice(0,64),view=chosen&&chosen.view==='forum'?'forum':'chat';
      if(!name)return;
      if((room.channels||[]).some(c=>c&&String(c.name||'').toLowerCase()===name.toLowerCase())){
        p.toast('this community already has #'+name); return; }
      addChannel.disabled=true;
      try{
        const viewer=p.viewer?p.viewer():{},reader=window.PosterCordReader,
          bundle=room.cord.bundle,loadKey=room.communityId||room.naddr,relays=roomRelays(bundle);
        let wraps=roomControls.get(loadKey);
        if(!wraps){ const seed=reader.inspectControl(bundle,[]),report={};
          /* `urgent`: this read decides what the new channel event is built on. Without it a relay busy
             with the room's own opening load (or resting after one failure) is SKIPPED and answers [],
             and the channel is built on an empty control set -- refused as "not permitted", or worse
             written on nothing. A write must not follow a read that asked nobody. */
          wraps=await cordQuery(p,relays,[{kinds:[1059],authors:seed.controlPubkeys,limit:1000}],
            {timeout:10000,max:8,plane:cordPlaneContext(p,bundle,[],room),report,urgent:true});
          if(!(report.ok||[]).length&&!(wraps||[]).length)throw new Error('could not read this community from its relays yet — try again in a moment'); }
        const scope=cordPlaneContext(p,bundle,wraps||[],room),
          made=await reader.createChannelWrap(bundle,wraps||[],repository?{name,view,repository}:{name,view},viewer.pubkey,p.signTemplate);
        if(!scope.current())throw new Error('Concord membership changed while signing');
        const accepted=await p.relayPublishRoom(relays,made.wrap,cordPlaneAuth(p,scope,made.wrap.pubkey,relays));
        if(!accepted||!accepted.ok)throw new Error('community relays rejected the new channel');
        roomControls.set(loadKey,[...(wraps||[]),made.wrap]);
        /* Re-read the list by durable identity: signing can take long enough for the active room to
           have moved, and writing back a stale index is how a channel lands in the wrong community. */
        const latest=saved(),at=latest.findIndex(item=>roomIdentity(item)===roomId);
        if(at>=0){ if(!Array.isArray(latest[at].channels))latest[at].channels=[];
          if(!latest[at].channels.some(c=>c&&c.name===made.name))
            latest[at].channels.push({name:made.name,private:false,id:made.channelId,view:made.view==='forum'?'forum':'chat',repositories:made.repositories||[]});
          save(latest); }
        if(repository){state.channel=made.name;state.thread=null;} render(); p.toast(repository?'Repository channel created':(made.view==='forum'?'Forum #':'#')+made.name+' created');
      }catch(e){ addChannel.disabled=false; p.toast('channel was not created: '+((e&&e.message)||e)); }
    };
    /* A GIT ATTACH / DETACH is a channel edition too (cord-reader createChannelEditWrap with
     * attachRepository / detachRepository — Armada's wire shape). Same read-then-write discipline as
     * rename: an urgent control read when none is held, the membership re-checked after signing. */
    const editChannelGit=async(room,id,changes)=>{
      const reader=window.PosterCordReader,bundle=room.cord.bundle,loadKey=room.communityId||room.naddr,relays=roomRelays(bundle),roomId=roomIdentity(room),viewer=p.viewer?p.viewer():{};
      let wraps=roomControls.get(loadKey); if(!wraps){const seed=reader.inspectControl(bundle,[]),report={};wraps=await cordQuery(p,relays,[{kinds:[1059],authors:seed.controlPubkeys,limit:1000}],{timeout:10000,max:8,plane:cordPlaneContext(p,bundle,[],room),report,urgent:true});if(!(report.ok||[]).length&&!(wraps||[]).length)throw new Error('could not read this community from its relays yet — try again in a moment');}
      const scope=cordPlaneContext(p,bundle,wraps||[],room),made=await reader.createChannelEditWrap(bundle,wraps||[],id,changes,viewer.pubkey,p.signTemplate);
      if(!scope.current())throw new Error('Concord membership changed while signing');
      const r=await p.relayPublishRoom(relays,made.wrap,cordPlaneAuth(p,scope,made.wrap.pubkey,relays)); if(!r||!r.ok)throw new Error('the community relays did not accept it');
      roomControls.set(loadKey,[...(wraps||[]),made.wrap]);
      const latest=saved(),at=latest.findIndex(x=>roomIdentity(x)===roomId);
      if(at>=0){ const list=latest[at].channels||[],i=list.findIndex(c=>c&&c.id===id); if(i>=0)list[i]={...list[i],repositories:made.repositories||[]}; latest[at].channels=list; save(latest); }
      render(); return made;
    };
    if(window.PCConcordGit){
      const room=saved()[state.community];
      const po=$('#ccg-projects-open'); if(po)po.onclick=()=>{ if(room)PCConcordGit.openProjects(room); };
      const hi=$('#ccg-head-issues'); if(hi)hi.onclick=()=>{ const ch=room&&channelsOf(room).find(c=>c.name===(state.channel||'general')); if(room)PCConcordGit.openProjects(room,{channel:ch,tab:'issues'}); };
      const gs=document.querySelector('#cc-settings-dialog .ccg-settings'); if(gs&&room)PCConcordGit.bindSettings(gs,room,(id,changes)=>editChannelGit(saved()[state.community]||room,id,changes));
    }
    /* RENAME / DELETE A CHANNEL — a new edition of the channel entity (createChannelEditWrap), which is
     * what Vector writes. The saved list is updated by id; the next control read confirms it. */
    $$('[data-cc-channel-more]').forEach(b=>b.onclick=e=>{ if(e&&e.stopPropagation)e.stopPropagation();
      const room=saved()[state.community],id=b.dataset.ccChannelMore,ch=(room&&room.channels||[]).find(c=>c&&c.id===id); if(!ch||!p.modal)return;
      const gitOn=window.PCConcordGit?PCConcordGit.attachmentsOf(ch).filter(a=>a.detachedAt===undefined):[];
      p.modal(`<h3>#${p.enc(ch.name)}</h3><div class="cc-attach-choices"><button class="btn btn-ghost" data-cc-ch-rename>Rename</button>${window.PCConcordGit&&!room.local?`<button class="btn btn-ghost" data-cc-ch-git>${gitOn.length?'Connect another repository':'Connect a git repository'}</button>${gitOn.map(a=>`<button class="btn btn-ghost danger" data-cc-ch-ungit="${p.enc(a.address.coordinate)}">Disconnect ${p.enc(a.address.identifier)}</button>`).join('')}`:''}<button class="btn btn-ghost danger" data-cc-ch-delete>Delete channel</button></div>`,root=>{
        const git=root.querySelector('[data-cc-ch-git]'); if(git)git.onclick=()=>{ p.closeModal(); void PCConcordGit.connectFlow(room,(cid,changes)=>editChannelGit(saved()[state.community]||room,cid,changes),id); };
        root.querySelectorAll('[data-cc-ch-ungit]').forEach(b=>b.onclick=async()=>{ p.closeModal(); const ok=p.uiConfirm?await p.uiConfirm('Disconnect '+b.dataset.ccChUngit.split(':').slice(2).join(':')+' from #'+ch.name+'? Historical activity remains attached to its original interval.',{ok:'Disconnect',danger:true}):true; if(!ok)return;
          try{ await editChannelGit(saved()[state.community]||room,id,{detachRepository:b.dataset.ccChUngit}); p.toast('repository disconnected'); }catch(err){ p.toast('the repository was not disconnected: '+(err&&err.message||err)); } });
        const run=async(changes)=>{ try{
          const reader=window.PosterCordReader,bundle=room.cord.bundle,loadKey=room.communityId||room.naddr,relays=roomRelays(bundle),roomId=roomIdentity(room),viewer=p.viewer?p.viewer():{};
          let wraps=roomControls.get(loadKey); if(!wraps){const seed=reader.inspectControl(bundle,[]),report={};wraps=await cordQuery(p,relays,[{kinds:[1059],authors:seed.controlPubkeys,limit:1000}],{timeout:10000,max:8,plane:cordPlaneContext(p,bundle,[],room),report,urgent:true});if(!(report.ok||[]).length&&!(wraps||[]).length)throw new Error('could not read this community from its relays yet — try again in a moment');}   // urgent: an edit built on a read that asked nobody (see channel creation above)
          const scope=cordPlaneContext(p,bundle,wraps||[],room),made=await reader.createChannelEditWrap(bundle,wraps||[],id,changes,viewer.pubkey,p.signTemplate);
          if(!scope.current())throw new Error('Concord membership changed while signing');
          const r=await p.relayPublishRoom(relays,made.wrap,cordPlaneAuth(p,scope,made.wrap.pubkey,relays)); if(!r||!r.ok)throw new Error('the community relays did not accept it');
          roomControls.set(loadKey,[...(wraps||[]),made.wrap]);
          const latest=saved(),at=latest.findIndex(x=>roomIdentity(x)===roomId);
          if(at>=0){ const list=(latest[at].channels||[]); const i=list.findIndex(c=>c&&c.id===id);
            if(i>=0){ if(changes.deleted){ const gone=list.splice(i,1)[0]; if(state.channel===gone.name)state.channel=(list[0]&&list[0].name)||'general'; } else { if(state.channel===list[i].name)state.channel=made.channel.name; list[i]={...list[i],name:made.channel.name}; } }
            latest[at].channels=list; save(latest); }
          render(); p.toast(changes.deleted?'#'+ch.name+' deleted':'renamed to #'+made.channel.name);
        }catch(err){ p.toast('the channel was not changed: '+(err&&err.message||err)); } };
        root.querySelector('[data-cc-ch-rename]').onclick=async()=>{ p.closeModal(); const name=String(await (p.uiPrompt?p.uiPrompt('Rename #'+ch.name,{value:ch.name,ok:'Rename'}):'')||'').trim().replace(/^#/,'').slice(0,64); if(!name||name===ch.name)return; if((room.channels||[]).some(c=>c&&c.id!==id&&String(c.name||'').toLowerCase()===name.toLowerCase())){p.toast('this community already has #'+name);return;} await run({name}); };
        root.querySelector('[data-cc-ch-delete]').onclick=async()=>{ p.closeModal(); const ok=p.uiConfirm?await p.uiConfirm('Delete #'+ch.name+' for everyone? This cannot be undone.',{ok:'Delete',danger:true}):true; if(ok)await run({deleted:true}); };
      }); });
    const dissolveBtn=$('#cc-dissolve'); if(dissolveBtn)dissolveBtn.onclick=async()=>{ const room=saved()[state.community]; if(!room?.cord?.bundle)return;
      const ok=p.uiConfirm?await p.uiConfirm('Dissolve '+(room.name||'this community')+' for everyone? Nobody will be able to post in it again, in PosterChan, Vector or Armada. This cannot be undone.',{ok:'Dissolve',danger:true}):false; if(!ok)return;
      dissolveBtn.disabled=true; try{ await dissolveCommunity(p,room); const d=$('#cc-settings-dialog'); if(d)d.classList.add('hidden'); }catch(e){ dissolveBtn.disabled=false; p.toast('the community was not dissolved: '+(e&&e.message||e)); } };
    const privateChannel=$('#cc-private-channel');if(privateChannel)privateChannel.onclick=async()=>{
      const room=saved()[state.community];if(!room)return;
      const name=await p.uiPrompt('Name the private channel',{value:'',ok:'Continue'});if(!name?.trim())return;
      const answer=await p.uiPrompt('Who can access this channel? Paste hex public keys separated by commas. Leave empty for only you.',{value:'',ok:'Create private channel'});if(answer==null)return;
      privateChannel.disabled=true;
      try{
        const recipients=String(answer).split(/[\s,]+/).filter(Boolean).map(value=>p.refToPk?.(value)||value);
        const made=await createPrivateRoomChannel(p,room,name,recipients);render();
        p.toast(made.failures?'Private channel created; '+made.failures+' invitations need to be sent again':'Private channel created');
      }catch(error){privateChannel.disabled=false;p.toast('Private channel setup stopped: '+(error?.message||error));}
    };
    const membersInvite=$('#cc-members-invite'); if(membersInvite)membersInvite.onclick=()=>{ $('#cc-members-dialog').classList.add('hidden'); $('#cc-join').classList.remove('hidden'); };
    const manageLinks=$('#cc-manage-links');if(manageLinks)manageLinks.onclick=async()=>{const room=saved()[state.community];if(!room?.cord?.bundle)return;try{await showOwnedInviteLinks(p,room);}catch(e){p.toast(e.message||String(e));}};
    const copyLink=$('#cc-copy-link'); if(copyLink)copyLink.onclick=async()=>{ const a=saved(),room=a[state.community]; if(!room)return; if(room.url){p.copyValue(shareableInviteUrl(p,room.url));return;} if(room.cord?.bundle){try{await showOwnedInviteLinks(p,room);}catch(e){p.toast(e.message||String(e));}return;} if(room.url){ p.copyValue(shareableInviteUrl(p,room.url)); return; } copyLink.disabled=true; try{ p.toast('upgrading this room to a public relay community…'); const priorMessages=testMessages(room.naddr), upgraded=await mintPublicRoom(p,room.name,room.icon); upgraded.description=room.description||''; a[state.community]=upgraded; save(a); if(priorMessages.length)saveTestMessages(upgraded.naddr,priorMessages); render(); p.copyValue(shareableInviteUrl(p,upgraded.url)); p.toast('room upgraded — invite link copied'); }catch(e){ copyLink.disabled=false; p.toast('could not create invite: '+(e&&e.message||e)); } };
    const publishListing=$('#cc-publish-listing'); if(publishListing)publishListing.onclick=async()=>{ const room=saved()[state.community]; if(!room||!room.url||!room.cord||!Array.isArray(room.cord.events)){ p.toast('This is an old local sandbox; create a relay community to list it'); return; } publishListing.disabled=true; try{ p.toast('publishing to Armada relays…'); for(const ev of room.cord.events)await p.relayPublishTo(CORD_RELAYS,ev); await announceListing(p,room); p.toast('published to Armada Discover'); }catch(e){ p.toast('could not publish listing: '+(e&&e.message||e)); }finally{ publishListing.disabled=false; } };
    for(const id of ['#cc-settings-cancel','#cc-settings-close','#cc-settings-back']){const b=$(id);if(b)b.onclick=()=>{closeCommunitySettings();};}
    const leave=$('#cc-leave-community');if(leave)leave.onclick=async()=>{const initial=saved(),index=state.community,room=initial[index],leavingId=roomIdentity(room);if(!room||!leavingId)return;/* NEVER A NATIVE DIALOG. In the desktop shell `window.confirm` opens a real OS window and leaves the renderer unfocusable; in the APK's WebView it can be suppressed outright, and this confirm was the ONLY gate on Leave — suppressed, it answers false and the button silently does nothing, which is exactly "mobile has no way to leave concord communities". */if(p.uiConfirm&&!await p.uiConfirm('Leave '+roomName(room,index)+'?',{ok:'Leave',danger:true}))return;leave.disabled=true;try{await leaveArmadaMembership(p,room);/* Signing and relay publication can take long enough for membership sync or navigation to change the list. Reload it and remove by durable identity, never by the stale numeric index captured above. */const latest=saved(),activeBefore=latest[state.community],activeId=roomIdentity(activeBefore),removed=removeCommunityByIdentity(latest,leavingId),rooms=removed.rooms;save(rooms);await clearRoomCache(room);if(activeId===leavingId||!activeId){state.community=rooms.length?Math.min(Math.max(removed.index,0),rooms.length-1):null;state.channel=state.community==null?null:'general';mobileChatOpen=false;}else{const activeIndex=rooms.findIndex(item=>roomIdentity(item)===activeId);state.community=activeIndex>=0?activeIndex:(rooms.length?0:null);}if(state.community!=null)activeSet(String(state.community));else activeSet(null);render();p.toast('community left');}catch(e){leave.disabled=false;p.toast('could not leave community: '+(e&&e.message||e));}};
    /* THE CONVERSATION HEADER IS NOT A SURFACE ON A PHONE. `.cc-conversation` is display:none until
     * a channel is opened, so the only Leave control lived behind a tap somebody has no reason to
     * make first — "mobile has no way to leave concord communities", with the button measurably
     * present and measurably 0x0. It belongs beside the community name, on the pane a phone opens
     * on, which is where a desktop looks for it too. Both delegate to the one real handler so the
     * three cannot drift. */
    const leaveByHeader=()=>{const action=$('#cc-leave-community');if(action)action.click();};
    const leaveShortcut=$('#cc-leave-shortcut');if(leaveShortcut)leaveShortcut.onclick=leaveByHeader;
    const leaveRoom=$('#cc-leave-room');if(leaveRoom)leaveRoom.onclick=leaveByHeader;
    const timerSelect=$('#cc-message-expiration');if(timerSelect)timerSelect.onchange=()=>{timerSelect.dataset.edited='1';};
    const keepSelect=$('#cc-history-retention');if(keepSelect)keepSelect.onchange=()=>{keepSelect.dataset.edited='1';};
    const settingsSave=$('#cc-settings-save');if(settingsSave)settingsSave.onclick=async()=>{
      if(settingsSaving)return;
      const room=saved()[state.community];if(!room)return;
      /* ONLY WHAT WAS CHANGED GOES OUT. Every field used to be sent on every Save -- name (this device's copy),
       * the description box and the icon box -- so a banner-only Save also wrote a blank description, an
       * empty icon (= "remove it") and a stale name over the community's real ones. A field is sent when its
       * value differs from the value the sheet opened with (defaultValue); the writer keeps the rest. */
      const values={},descBox=$('#cc-description-value'),iconBox=$('#cc-settings-icon');
      if(descBox&&descBox.value!==descBox.defaultValue)values.description=String(descBox.value||'').trim();
      if(iconBox&&iconBox.value!==iconBox.defaultValue)values.icon=normalizeIcon(iconBox.value);
      if(timerSelect?.dataset.edited==='1')values.message_expiration=Number(timerSelect.value);
      // Forever is the absence of the key (null removes it from `custom`), so the default costs nothing on the wire.
      if(keepSelect?.dataset.edited==='1')values.customPatch={[RETENTION_KEY]:retentionDays(keepSelect.value)||null};
      Object.assign(values,stagedBanner(room));
      /* Every write below goes to the LIVE sheet ($ at the time), never the button this handler was bound
       * to: render() can rebuild the sheet while the relays answer, and a write to the detached copy is
       * a Save that stays greyed out and an error nobody sees. */
      const paint=()=>{const b=$('#cc-settings-save');if(b){b.disabled=settingsSaving;b.textContent=settingsSaving?'Saving…':'Save changes';try{if(settingsSaving)b.setAttribute('aria-busy','true');else b.removeAttribute('aria-busy');}catch(_){}}
        const err=$('#cc-settings-error');if(err){err.textContent=settingsError;err.hidden=!settingsError;}};
      settingsSaving=true;settingsError='';paint();
      try{const result=await saveCommunitySettings(p,room,values);
        settingsSaving=false;
        /* Saved: close the sheet ITSELF and land back on the community. It used to rely on render() shutting every sheet built after the open-sheet restore -- the bug that also shut Known members mid-use. */
        closeCommunitySettings();paint();render();
        p.toast(result.noticesFailed?'Settings saved; '+result.noticesFailed+' channel notices could not be delivered':'Settings saved');}
      catch(error){
        /* Refused: the sheet STAYS, with the person's edits in it and the reason written inside it -- a
         * toast fades in three seconds and the edits are the only copy. */
        settingsSaving=false;const why='Not saved: '+(error?.message||error);
        if(communitySettingsOpen())settingsError=why;
        paint();
        p.toast('community settings could not be saved: '+(error?.message||error));}
    };
    bindRefoundingControl(p,$);
    bindBannerSettings(p,$);
    const themeBtn=$('#cc-theme'); if(themeBtn)themeBtn.onclick=()=>ccTheme.openPicker(p);
    const notifyHead=$('#cc-notify-head'); if(notifyHead)notifyHead.onclick=()=>{ const n=$('#cc-notify'); if(n)n.click(); };   // the phone's copy of the bell
    const notify=$('#cc-notify'); if(notify)notify.onclick=async()=>{
      /* Three states: never set up (no record -- the first tap ASKS the OS, as it always did), on, off. Only a tap on an
         ON switch turns it off; the OS permission itself cannot be read reliably inside the APK, so it is not the state. */
      let rec='';try{rec=localStorage.getItem(NOTIFY_KEY(accountPk()))||'';}catch(_){}
      if(rec==='on'){ setConcordNotify(false); p.toast('Community notifications off'); render(); return; }
      const result=p.askOsNotify?await p.askOsNotify():'unsupported';
      if(result==='granted'){ setConcordNotify(true); p.toast('Community notifications on'); render(); }
      else p.toast(result==='denied'?'notifications are blocked for this app — allow them in system settings first':'notifications are unavailable here'); };
    const call=$('#cc-call'); if(call)call.onclick=async()=>{try{const room=saved()[state.community];if(room&&room.protocol==='nip29'){const me=p.viewer&&p.viewer().pubkey,peers=roomParticipants(room,me).filter(pk=>pk!==me);if(!peers.length){p.toast('No other community members are available to call yet');return;}p.startGroupCall(peers,false);return;}await startCordCall(p,room,state.channel||'general');}catch(e){p.toast(e.message||'The call could not start');}};
    /* The button gives its focus up: a focused control inside the feed DEFERS every background
     * repaint (backgroundRender protects the composer), so a focused voice button kept the list from
     * ever showing who joined -- including this device. */
    $$('[data-cc-voice-join]').forEach(b=>b.onclick=async ev=>{ try{ev&&ev.stopPropagation();b.blur();}catch(_){}
      try{await startCordCall(p,saved()[state.community],b.dataset.ccVoiceChannel,{joinOnOpen:true});}catch(e){p.toast(e&&e.message||'The call could not start');} });
    { const np=$('#cc-forum-new'); if(np)np.onclick=async()=>{ const room=saved()[state.community],channel=state.channel||'general';
      const post=await PCCordSpaces.askNewPost();if(!post)return;
      if(saved()[state.community]!==room&&roomIdentity(saved()[state.community])!==roomIdentity(room))return;
      await publishForumPost(p,room,channel,post.title,post.body); }; }
    void startVoiceWatch(p,saved()[state.community]);
    const cancel=$('#cc-join-cancel'); if(cancel) cancel.onclick=()=>$('#cc-join').classList.add('hidden');
    /* PREVIEW, THEN ANSWER. The button said "Preview invite" and joined outright; now it fetches
     * the community, shows what it is, and waits. `acceptInvite` below is the join half, unchanged
     * except that it is reached by a deliberate press. */
    const directInbox=$('#cc-direct-inbox');if(directInbox)directInbox.onclick=()=>showDirectInvitations().catch(e=>p.toast(e.message||String(e)));
    $$('[data-cc-review-invites]').forEach(b=>b.onclick=()=>showDirectInvitations().catch(e=>p.toast(e.message||String(e))));
    const directSend=$('#cc-direct-send');if(directSend)directSend.onclick=()=>showDirectInviteSender(saved()[state.community]);   // the room open NOW — `current` is the header renderer's, not in scope here
    const go=$('#cc-join-go'); if(go) go.onclick=async()=>{ const raw=String($('#cc-invite-url').value||'').trim(),v=inviteParts(raw); if(!v){ p.toast('that is not a Concord invite link'); return; } go.disabled=true; try{ p.toast('fetching and decrypting community…'); const room=await hydrateInvite(p,raw); pendingInvite={url:raw,room}; { const j=$('#cc-join'); if(j)j.classList.add('hidden'); } go.disabled=false; render(); }catch(e){ go.disabled=false; p.toast('could not read that invite: '+(e&&e.message||e)); } };
    const acceptInvite=async(raw,room,direct=false,onSaved=null)=>{ if(room.cord?.bundle&&window.PosterCordReader?.validateInviteBundle)window.PosterCordReader.validateInviteBundle(room.cord.bundle,{forJoin:true});const a=saved(),i=a.findIndex(x=>sameRoom(x,room)); if(i<0)a.push(room);else a[i]=direct?mergeDirectInviteRoom(a[i],room):mergeRoom(a[i],room); save(a); if(onSaved)try{onSaved();}catch(_){} state.community=a.findIndex(x=>sameRoom(x,room)); state.channel='general'; render(); await persistArmadaMembership(p,room);
      /* ANNOUNCE THE JOIN. Only the Discover list published the guestbook Join (CORD-02 §5), so
       * somebody who joined through an invite link or a direct invite — the ordinary way into a
       * Vector community — arrived silently: Vector draws "X has joined" from that kind-3306 and from
       * nothing else ("I joined vector room fine but users say they seen no join notification").
       * Best effort, like the Discover path: a missed Join must never fail the join itself. */
      void publishGuestbook(p,saved()[state.community]||room,'join');
      /* Joining is already the user's request to enter this room.  Waiting for a later channel click
       * left the placeholder #general on screen with no id, icon or history, so a successful Armada
       * invite looked like an empty broken community until somebody switched away and back. */
      await hydrateRoomStreams(p,state.community); enterChatBottom(); p.toast('community joined'); };
    /* The two answers to the invitation on screen. `pendingInvite` is cleared BEFORE either one
     * acts, so the repaint each of them causes cannot draw the card again over its own result. */
    const accept=$('#cc-invite-accept'); if(accept) accept.onclick=async()=>{ const inv=pendingInvite; if(!inv)return; accept.disabled=true;
      try{
        if(inv.directId&&inv.account!==p.viewer()?.pubkey)throw new Error('The signed-in account changed');
        if(inv.room?.cord?.bundle)window.PosterCordReader.validateInviteBundle(inv.room.cord.bundle,{forJoin:true});
        /* CLOSE THE SHEET BEFORE THE JOIN REPAINTS. render() reopens every sheet that was open when it began (so a
         * background refresh cannot shut one somebody is typing in), and this one was still open -- "click Preview,
         * then Join, the modal never disappears". Decline hid it after its render; Join never did. */
        { for(const id of ['#cc-join','#cc-invite-sheet']){ const sheet=$(id); if(sheet)sheet.classList.add('hidden'); } const u=$('#cc-invite-url'); if(u)u.value=''; }
        /* The invitation is ANSWERED the moment the membership is saved -- not after the room's history loads, which
         * on a slow or unreachable relay can take a long while (or fail), leaving "You have an invitation" up for a
         * community you are already in. */
        const answered=()=>{ if(inv.directId&&inv.account===p.viewer()?.pubkey){ window.PCCordDirectInvites.dismiss(inv.directId,p.cordDirectContext()); void refreshInviteCount(); } };
        forgetDeclinedInvite(inv.url);pendingInvite=null;await acceptInvite(inv.url,inv.room,!!inv.directId,answered);
      }
      catch(e){ accept.disabled=false; p.toast('could not join: '+(e&&e.message||e)); } };
    const decline=$('#cc-invite-decline'); if(decline) decline.onclick=()=>{ const inv=pendingInvite; pendingInvite=null;
      if(inv?.directId){if(inv.account===p.viewer()?.pubkey)window.PCCordDirectInvites.dismiss(inv.directId,p.cordDirectContext());}
      else if(inv)declineInvite(inv.url);
      render();
      for(const id of ['#cc-join','#cc-invite-sheet']){ const panel=$(id); if(panel)panel.classList.add('hidden'); }
      void refreshInviteCount();
      p.toast(inv?.directId?'Invitation declined':'invitation declined — the link still works if you change your mind'); };
    /* "Not now": the invitation stays where it was (in Invites, or the link still works) and nothing is answered. */
    const notNow=$('#cc-invite-close'); if(notNow) notNow.onclick=()=>{ pendingInvite=null; render(); };

    $$('[data-cc-server]').forEach(b=>b.onclick=()=>{if(b._ccHeld){b._ccHeld=false;return;}const i=+b.dataset.ccServer,inDrawer=mobileChatOpen&&mobileDrawerOpen;void activateJoinedRoom(p,i,inDrawer);});
    /* FOLDERS: a folder opens/closes on click; a community or a folder has its menu on right-click or a
     * long press (a phone has no right button). Every edit goes through editRail, synced with the account. */
    loadRail(p);
    $$('[data-cc-folder]').forEach(b=>b.onclick=()=>{ if(b._ccHeld){ b._ccHeld=false; return; } const set=railOpen(),id=b.dataset.ccFolder; if(set.has(id))set.delete(id); else set.add(id); setRailOpen(set); render(); });
    const railMenu=(b)=>{
      if(!p.openMenuPopover)return;
      const rooms=saved(),folderId=b.dataset.ccFolder;
      if(folderId){
        const f=railView(rooms,railDoc.layout).find(n=>n.type==='folder'&&n.id===folderId); if(!f)return;
        p.openMenuPopover(b,[['rename','Rename folder'],['ungroup','Ungroup']],async a=>{
          if(a==='ungroup'){ editRail(p,{t:'ungroup',id:folderId}); return; }
          const name=p.uiPrompt?await p.uiPrompt('Folder name',{value:f.name,ok:'Rename'}):null;
          if(name&&String(name).trim())editRail(p,{t:'rename',id:folderId,name:String(name).trim()});
        });
        return;
      }
      const i=+b.dataset.ccServer,room=rooms[i],key=railKeyOf(room); if(!key)return;
      const view=railView(rooms,railDoc.layout),folders=view.filter(n=>n.type==='folder');
      const mine=folders.find(n=>n.members.includes(i));
      /* A folder is made from TWO communities, as in Armada (a one-community folder dissolves). */
      const items=[];
      for(const f of folders)if(f!==mine)items.push(['into:'+f.id,'Add to “'+f.name+'”']);
      for(const n of view)if(n.type==='item'&&n.i!==i&&items.length<14)items.push(['with:'+n.i,'New folder with “'+roomName(rooms[n.i],n.i)+'”']);
      if(mine)items.push(['out','Remove from “'+mine.name+'”']);
      if(!items.length)return;
      p.openMenuPopover(b,items,async a=>{
        if(a==='out'){ editRail(p,{t:'out',key}); return; }
        if(a.startsWith('into:')){ editRail(p,{t:'into',key,id:a.slice(5)}); return; }
        const other=railKeyOf(rooms[+a.slice(5)]); if(!other)return;
        const name=p.uiPrompt?await p.uiPrompt('Name the new folder',{value:'Folder',ok:'Create'}):'Folder';
        if(name===null||name===undefined)return;
        const id=Math.random().toString(36).slice(2,10)+Date.now().toString(36);
        const set=railOpen(); set.add(id); setRailOpen(set);
        editRail(p,{t:'into',key,with:other,id,name:String(name).trim()||'Folder'});
      });
    };
    $$('[data-cc-server],[data-cc-folder]').forEach(b=>{
      if(b.id==='cc-add'||b.id==='cc-discovery')return;
      b.oncontextmenu=e=>{ e.preventDefault(); railMenu(b); };
      /* Once per button: the rail is KEPT across repaints (retainCommunityRail), so listeners added on every
       * render piled up and one long press opened the menu once per repaint since. railMenu is read at
       * press time through the button, so the first binding stays current. */
      b._ccRailMenu=()=>railMenu(b);
      if(b._ccRailWired)return; b._ccRailWired=true;
      let t=0; b.addEventListener('pointerdown',e=>{ if(e.pointerType==='mouse')return; clearTimeout(t); t=setTimeout(()=>{ b._ccHeld=true; b._ccRailMenu(); },550); });
      ['pointerup','pointercancel','pointerleave'].forEach(ev=>b.addEventListener(ev,()=>clearTimeout(t)));
    });
    $$('[data-cc-discover]').forEach(b=>b.onclick=async()=>{
      const v=discovered[+b.dataset.ccDiscover];if(!v)return;
      const a=saved();let i=a.findIndex(x=>sameRoom(x,v));if(i<0){a.push(v);i=a.length-1;}
      save(a);i=a.findIndex(x=>sameRoom(x,v));state.community=i;state.channel='general';enterChatBottom();render();
      p.toast('fetching and decrypting community…');
      try{
        const hydrated=await hydrateInvite(p,v.url),latest=saved(),selected=latest[state.community],at=latest.findIndex(x=>sameRoom(x,v));
        // A second join, membership refresh, or Leave can finish during invite I/O.
        if(at<0)return;
        latest[at]=mergeRoom(latest[at],hydrated);save(latest);
        const joined=latest.findIndex(x=>sameRoom(x,hydrated));
        if(sameRoom(selected,v))state.community=joined;
        else if(selected)state.community=latest.findIndex(x=>sameRoom(x,selected));
        await persistArmadaMembership(p,latest[joined]);void publishGuestbook(p,latest[joined],'join');await hydrateRoomStreams(p,joined);p.toast('community joined');
      }catch(e){p.toast('could not load community: '+(e&&e.message||e));}
      finally{if(sameRoom(saved()[state.community],v))enterChatBottom(true);}
    });
    $$('[data-cc-channel]').forEach(b=>b.onclick=async()=>{ const community=state.community,channel=b.dataset.ccChannel; state.channel=channel; state.thread=null; replyTarget=null; mobileChatOpen=true; mobileDrawerOpen=false; enterChatBottom(); render(); const rooms=saved(),room=rooms[community],noticeKey=roomIdentity(room)+':'+channel; try{if(room&&room.cord&&!hydratedRoomViews.has(roomIdentity(room)))await hydrateRoomStreams(p,community);else if(room&&room.protocol==='nip29'&&!room.nip29Hydrated)await hydrateNip29Room(p,community);roomLoadNotices.delete(noticeKey);}catch(e){roomLoadWarning(p,noticeKey,'could not refresh room history: ',e);} if(state.community===community&&state.channel===channel)enterChatBottom(true); });
    $$('[data-cc-star]').forEach(b=>b.onclick=e=>{ if(e&&e.stopPropagation)e.stopPropagation(); const room=saved()[state.community],name=b.dataset.ccStar; if(!room||!name)return; setChannelStarred(room,name,!channelStarred(room,name)); render(); });
    const bc=$('#cc-back-communities'); if(bc)bc.onclick=()=>{ discoveryOpen=true; state.community=null; state.channel=null; render(); };
    const bh=$('#cc-back-channels'); if(bh)bh.onclick=()=>{ if(state.community==null){ const rooms=saved(),wanted=Number(activeGet()||0); discoveryOpen=false; state.community=rooms.length&&wanted>=0&&wanted<rooms.length?wanted:(rooms.length?0:null); state.channel=state.community==null?null:'general'; mobileChatOpen=false; mobileDrawerOpen=false; }else if(mobileChatOpen){setMobileDrawer(!mobileDrawerOpen);return;}else mobileChatOpen=true; render(); };
    const drawerBackdrop=$('#cc-drawer-backdrop');if(drawerBackdrop)drawerBackdrop.onclick=()=>setMobileDrawer(false);
    const send=$('#cc-send'); if(send&&input){
      send.onclick=async()=>{ const text=String(input.value||'').trim(),key=input.dataset&&input.dataset.ccDraftKey||composerKey(saved()[state.community],state.channel); if(!text||sendingDrafts.has(key))return; const a=saved(), room=a[state.community],sendChannel=state.channel,storeId=channelStoreId(room,sendChannel); if(!room||(!room.local&&!room.cord&&room.protocol!=='nip29')){ p.toast('relay messaging becomes available after the invite is decrypted'); return; } const used=[...pendingAttachments].filter(([url])=>text.includes(url)),attachmentTags=used.map(([,tag])=>tag),target=replyTarget,_shown=(target&&!state.thread)?(()=>{ /* replying from the channel: open that thread, or the reply vanishes into the collapse as it is sent */ try{ expandedThreads.add(threadRootId(testMessages(storeId)||[],target)); }catch(_){} return 1; })():0,replyTags=[],viewer=p.viewer?p.viewer():{},m=testMessages(storeId),lowerText=text.toLowerCase(),mentionTags=[],taggedPeople=new Set();const mentionPairs=[];for(const [handle,pk] of mentionRecipients){if(lowerText.includes('@'+handle)){taggedPeople.add(pk);mentionPairs.push([handle,pk]);}}for(const pk of typedMentionRecipients(text,roomParticipants(room,viewer.pubkey),p.profOf)){taggedPeople.add(pk);const pr=p.profOf?p.profOf(pk)||{}:{};for(const alias of mentionAliases(pr,pk,String(pr.display_name||pr.name||String(pk).slice(0,12))))mentionPairs.push([alias,pk]);}for(const pk of taggedPeople){mentionTags.push(['p',pk]);}const mentioned=wireMentionText(text,mentionPairs,npubOf),wireText=room.cord&&room.protocol!=='nip29'?wireAttachmentText(mentioned,used.filter(([,tag])=>tag.includes('encryption-algorithm aes-gcm')).map(([url])=>url)):mentioned; if(target){replyTags.push(...cordReplyTags(target,messageId(target),threadParticipants(m,target,viewer.pubkey),viewer.pubkey));} const submittedDraft=beginComposerSend(key);sendingDrafts.add(key);const extraTags=[...attachmentTags,...mentionTags,...replyTags],wireKind=target?1111:9,at=Date.now(),tempId='pending-'+(crypto.randomUUID?crypto.randomUUID():`${at}-${Math.random().toString(36).slice(2)}`),optimistic={id:tempId,by:me,pubkey:viewer.pubkey||'',text:wireText,at,kind:wireKind,tags:extraTags,reply:target?{id:messageId(target),by:target.by,text:target.text,expires:(target.tags||[]).find(t=>t[0]==='expiration')?.[1]}:null,reactions:{},pending:!room.local,remote:false,delivery:room.local?'':'signing'}; if(!room.local)markRemoteStore(storeId);m.push(optimistic); saveTestMessages(storeId,m); render(); scrollChatBottom(); const finish=()=>{sendingDrafts.delete(key);if(deliveryOwner(p)!==viewer.pubkey)return;for(const [url] of used)pendingAttachments.delete(url);render();scrollChatBottom();}; if(room.local){finish();return;} try{ const made=await publishCordMessage(p,room,sendChannel,wireText,extraTags,wireKind,d=>{const rows=testMessages(storeId),row=rows.find(x=>x.id===tempId);if(row){row.id=d.made.rumorId;row.tags=d.made.tags||row.tags;row.delivery='sending';saveTestMessages(storeId,rows);if(deliveryOwner(p)===viewer.pubkey)backgroundRender();}}),latest=testMessages(storeId),sent=latest.find(x=>x.id===tempId||x.id===made.rumorId); if(sent&&deliveryOwner(p)===viewer.pubkey){sent.id=made.rumorId;sent.at=made.ms;sent.tags=made.tags||sent.tags;sent.pending=false;sent.remote=true;sent.delivery='sent';saveTestMessages(storeId,latest);} finish(); }catch(e){ sendingDrafts.delete(key);if((!e.delivery||e.noPublish)&&deliveryOwner(p)===viewer.pubkey)restoreFailedComposer(key,submittedDraft);const latest=testMessages(storeId),failed=latest.find(x=>x.id===tempId||e.delivery&&x.id===e.delivery.made.rumorId);if(failed&&failed.pubkey===viewer.pubkey){failed.pending=false;failed.failed=true;failed.delivery=e.delivery?e.delivery.status:'failed';saveTestMessages(storeId,latest);if(deliveryOwner(p)===viewer.pubkey)preserveChatScroll(()=>render());} if(deliveryOwner(p)===viewer.pubkey)p.toast((e.delivery?'Delivery needs attention: ':'message was not sent: ')+(e&&e.message||e)); } };
      input.onkeydown=e=>{ const enter=e.key==='Enter'||e.code==='Enter'; if(mentionChoices.length){ if(e.key==='ArrowDown'||e.key==='ArrowUp'){e.preventDefault();mentionIndex=(mentionIndex+(e.key==='ArrowDown'?1:-1)+mentionChoices.length)%mentionChoices.length;syncMentionState();drawMentions();return;} if(e.key==='Tab'||(enter&&!e.ctrlKey&&!e.metaKey)){e.preventDefault();acceptMention();return;} if(e.key==='Escape'){e.preventDefault();closeMentions();return;} } /* ENTER SENDS ("make enter sent message instead of ctrl+enter, ctrl+enter should add" a line). Shift+Enter
         and Ctrl/⌘+Enter put a line break in instead; Enter while an IME is composing (Japanese, Chinese…)
         belongs to the IME and never sends half a word. */
      if(enter&&(e.ctrlKey||e.metaKey)){ e.preventDefault();
        if(typeof input.setRangeText==='function'){ input.setRangeText('\n',input.selectionStart,input.selectionEnd,'end'); }
        else input.value=String(input.value||'')+'\n';
        try{ input.dispatchEvent(new Event('input',{bubbles:true})); }catch(_){ }
        return; }
      if(enter&&!e.shiftKey&&!e.altKey&&!e.isComposing&&e.keyCode!==229){ e.preventDefault(); return send.onclick(); } };
    }
    const replyCancel=$('#cc-reply-cancel'); if(replyCancel)replyCancel.onclick=()=>{ replyTarget=null; render(); };
    bindMessages(); paintTyping(p);
  }
  /* THE MESSAGE ROWS BIND SEPARATELY FROM THE WORKSPACE AROUND THEM.
   *
   * Everything above stays put across a partial repaint — the rail, the channel list, the
   * composer — so only these handlers have to be re-attached when patchMessageList() replaces the
   * pane. Split out rather than copied: a second copy of a delete or zap handler is a second copy
   * that can drift from the one render() uses. */
  /* CORD-03 §3 (examples.md §2.2): a reply's UPPERCASE K/E/P name the THREAD ROOT and are inherited
   * from the parent VERBATIM -- `P` is the root's author, and nobody else. Lowercase k/e/p name the
   * parent, and people to notify (thread participants, @mentions) ride in lowercase `p` only. We
   * used to push ['P',pk] for every participant and every mention, so a spec reader could not tell
   * who wrote the thread root. Pure, for tests/client/test_concord_reply_tags.py. */
  /* Your own text message, sent and confirmed, in a relay community: the ones a kind-3302 can edit
   * (the fold applies an edit only from the original author). */
  /* Does this member hold `permission` (a CORD Permissions key) with a Grant this device has synced? */
  /* WHO HEARS A REPORT OF A MESSAGE: the room's owner and every member the control plane names as a
   * moderator -- minus the reporter, and minus the member reported (a moderator reported is not the one
   * to judge it). Hex keys only, deduplicated, bounded. */
  function _reportHex(v){ v=String(v||''); if(/^[0-9a-f]{64}$/i.test(v))return v.toLowerCase();
    try{ const d=window.NostrTools&&NostrTools.nip19.decode(v); if(d&&d.type==='npub')return String(d.data).toLowerCase(); }catch(_){ } return ''; }
  function reportRecipients(room,viewerPk,reported){
    const b=room&&room.cord&&room.cord.bundle||{};
    const all=[_reportHex(b.owner||b.creator_npub),...(Array.isArray(room&&room.moderators)?room.moderators:[]).map(_reportHex)].filter(Boolean);
    return [...new Set(all)].filter(pk=>pk!==viewerPk&&pk!==reported).slice(0,20);
  }
  /* This node's admins, for the opt-in "also tell this server's admins": only when the room lives on
   * THIS node's relay (otherwise they have no standing to see it) and the node names its admins. */
  function reportAdmins(p,room,viewerPk,exclude){
    try{ const cfg=(p.clientConfig&&p.clientConfig())||{},home=normalizeRelay(p.homeRelay&&p.homeRelay());
      if(!home||!Array.isArray(cfg.admin_npubs)||!cfg.admin_npubs.length)return [];
      if(!roomRelays(room&&room.cord&&room.cord.bundle).includes(home))return [];
      const skip=new Set([viewerPk,...(exclude||[])]);
      return [...new Set(cfg.admin_npubs.map(_reportHex).filter(Boolean))].filter(pk=>!skip.has(pk)).slice(0,10);
    }catch(_){ return []; }
  }
  function reportText(p,room,channel,m,reporter,label,comment,toAdmins){
    const name=pk=>{ const q=(p.profOf&&p.profOf(pk))||{}; const n=q.display_name||q.name||''; let np=pk; try{ np=NostrTools.nip19.npubEncode(pk); }catch(_){ } return (n?n+' ':'')+'nostr:'+np; };
    const text=String(m.text||'').replace(/\s+/g,' ').trim().slice(0,1500);
    return ['🚩 Concord report'+(toAdmins?' (also sent to this server’s admins)':''),
      'Community: '+String(room&&room.name||'(unnamed)')+' · #'+channel,
      'Reported member: '+name(m.pubkey),
      'Reason: '+label,
      comment?'Comment: '+comment:'',
      'Message: “'+text+'”',
      'Reported by: '+name(reporter),
      'Open: '+notificationRoute(room,channel,m)].filter(Boolean).join('\n');
  }
  /* MEMOISED ON THE CONTROL STATE IT WAS READ FROM, like canPinHere. authorityCitation folds the whole control stream,
   * signatures included; the members list started asking for BAN on EVERY render ("Ban by npub", 2026-10-18), and a
   * community that renders on each incoming message held a phone's main thread until the WebView stopped answering
   * (the APK's every-view smoke test: 21 s). A new control array is a new answer; the same one is a Map lookup. */
  const mayMemo=new WeakMap(),_noWraps=[];
  function memberMay(room,pubkey,permission){
    try{ const r=window.PosterCordReader,bundle=room&&room.cord&&room.cord.bundle; if(!r||!r.authorityCitation||!bundle||!pubkey)return false;
      const wraps=roomControls.get(room.communityId||room.naddr)||_noWraps;
      let m=mayMemo.get(wraps); if(!m){m=new Map();mayMemo.set(wraps,m);}
      const k=JSON.stringify([bundle.community_id||'',bundle.community_root||'',pubkey,permission]);
      if(!m.has(k)){ let ok=false; try{ r.authorityCitation(bundle,wraps,pubkey,permission); ok=true; }catch(_){ ok=false; } m.set(k,ok); }
      return m.get(k); }catch(_){ return false; }
  }
  /* PINS (CORD vsk 11 per channel, up to 25 proofs — Vector's pin_message/unpin_message). The list
   * is folded from the control plane; pinsOf is memoised on the control array it was read from, so a
   * repaint costs a Map lookup. */
  const pinMemo=new WeakMap(),canPinMemo=new WeakMap();
  function channelIdOf(room,name){ const c=(room&&room.channels||[]).find(x=>x&&x.name===(name||'general')); return c&&c.id||''; }
  function pinsOf(room,name){
    try{ const r=window.PosterCordReader,bundle=room&&room.cord&&room.cord.bundle,wraps=bundle&&roomControls.get(room.communityId||room.naddr),id=channelIdOf(room,name);
      if(!r||!r.inspectPinList||!bundle||!wraps||!id)return [];
      let byChannel=pinMemo.get(wraps); if(!byChannel){byChannel=new Map();pinMemo.set(wraps,byChannel);}
      if(!byChannel.has(id)){ const st=r.inspectPinList(bundle,wraps,id,[]); byChannel.set(id,st.available?st.entries:[]); }
      return byChannel.get(id);
    }catch(_){ return []; }
  }
  function canPinHere(room,name,viewer){
    try{ const r=window.PosterCordReader,bundle=room&&room.cord&&room.cord.bundle,wraps=bundle&&roomControls.get(room.communityId||room.naddr),id=channelIdOf(room,name);
      if(!(r&&r.canPinMessages&&bundle&&wraps&&id&&viewer&&viewer.pubkey&&!room.local))return false;
      let m=canPinMemo.get(wraps); if(!m){m=new Map();canPinMemo.set(wraps,m);}
      const k=id+'\n'+viewer.pubkey; if(!m.has(k))m.set(k,!!r.canPinMessages(bundle,wraps,id,viewer.pubkey)); return m.get(k);
    }catch(_){ return false; }
  }
  async function togglePin(p,room,name,messageIdHex){
    const r=window.PosterCordReader,bundle=room.cord.bundle,loadKey=room.communityId||room.naddr,relays=roomRelays(bundle),viewer=p.viewer?p.viewer():{},id=channelIdOf(room,name);
    if(!id)throw new Error('this channel is not synced yet');
    let wraps=roomControls.get(loadKey); if(!wraps){const seed=r.inspectControl(bundle,[]);wraps=await cordQuery(p,relays,[{kinds:[1059],authors:seed.controlPubkeys,limit:1000}],{timeout:10000,max:8,plane:cordPlaneContext(p,bundle,[],room)});}
    const chat=[...await cachedEnvelopes(envelopeCacheKey(loadKey,id)),...[...((absorbChatWraps.history&&absorbChatWraps.history.values())||[])].map(x=>x.ev)];
    const state=r.inspectPinList(bundle,wraps,id,chat),current=state.entries||[],pinned=current.some(e=>e.id===messageIdHex);
    let entries=current.map(e=>e.proof);
    if(pinned)entries=current.filter(e=>e.id!==messageIdHex).map(e=>e.proof);
    else{
      if(entries.length>=25)throw new Error('this channel already holds 25 pins; unpin one first');
      let proof=null; for(const w of chat){ try{ const pr=r.makePinProof(bundle,wraps,id,w),o=r.verifyPinProof(pr,id); if(o&&o.id===messageIdHex){proof=pr;break;} }catch(_){} }
      if(!proof)throw new Error('this message is not in this device’s history');
      entries.push(proof);
    }
    const scope=cordPlaneContext(p,bundle,wraps,room),made=await r.createPinListWrap(bundle,wraps,id,entries,viewer.pubkey,p.signTemplate,{firstPin:!state.version});
    if(!scope.current())throw new Error('Concord membership changed while signing');
    const res=await p.relayPublishRoom(relays,made.wrap,cordPlaneAuth(p,scope,made.wrap.pubkey,relays)); if(!res||!res.ok)throw new Error('the community relays did not accept it');
    roomControls.set(loadKey,[...wraps,made.wrap]);
    return !pinned;
  }
  function memberRoleNames(room,pk){
    try{ const r=window.PosterCordReader,bundle=room&&room.cord&&room.cord.bundle; if(!r||!bundle)return [];
      const info=r.inspectControl(bundle,roomControls.get(room.communityId||room.naddr)||[]),held=new Set(info.grants&&info.grants[pk]||[]);
      return [...new Set((info.roles||[]).filter(x=>held.has(x.id)&&x.scope==='server').map(x=>x.name))];
    }catch(_){ return []; }
  }
  function canEditMessage(m,viewer,room){
    return !!(m&&viewer&&viewer.pubkey&&m.pubkey===viewer.pubkey&&(m.kind===9||m.kind===1111)&&!m.pending&&!m.failed
      &&!String(m.id||'').startsWith('pending-')&&room&&room.cord&&!room.local&&room.protocol!=='nip29');
  }
  function cordReplyTags(target, targetId, participants, self){
    const out=[], tags=(target&&target.tags)||[];
    const root=tags.filter(t=>['K','E','P'].includes(t[0]));
    if(root.some(t=>t[0]==='E')) out.push(...root.map(t=>t.slice()));
    else out.push(['K',String(target.kind||9)],['E',targetId,'',target.pubkey||''],...(target.pubkey?[['P',target.pubkey]]:[]));
    out.push(['k',String(target.kind||9)],['e',targetId,'',target.pubkey||'']);
    const seen=new Set();
    // Notify the parent's author and every participant once -- never the sender (replying to your own message).
    for(const pk of [target.pubkey,...(participants||[])]){ if(pk&&pk!==self&&!seen.has(pk)){ seen.add(pk); out.push(['p',pk]); } }
    return out;
  }
  function bindMessages(){
    const p=PC(), $=p.$, $$=p.$$;
    $$('[data-cc-sum]').forEach(b=>b.onclick=e=>{e.preventDefault();e.stopPropagation();const text=sumText.get(b.dataset.ccSum);if(text&&window.PCChatAssist)window.PCChatAssist.summarizeLinks(text,'concord');});
    /* The retry has to LIFT THE COOLDOWN, not merely ask again: a relay that failed is held out of
     * `queryFrom` for half an hour on Concord's room reads, so a plain re-read would be a button
     * that measurably does nothing for thirty minutes. */
    const retry=$('#cc-retry-channel');
    if(retry)retry.onclick=async()=>{
      const room=state.community==null?null:saved()[state.community];
      if(!room)return;
      retry.disabled=true;retry.textContent='Trying…';
      try{
        const relays=roomRelays(room.cord&&room.cord.bundle);
        if(p.relayRetryRelays)p.relayRetryRelays(relays);
        channelReach.delete(channelStoreId(room,state.channel||'general'));
        hydratedRoomViews.delete(roomIdentity(room));
        if(room.cord)room.cord.hydrated=false;
        await hydrateRoomStreams(p,state.community,roomIdentity(room));
      }catch(e){ p.toast&&p.toast('could not reach this community: '+(e&&e.message||e)); }
      finally{ backgroundRender(); }
    };
    const closeMessageActions=()=>{$$('.cc-message.cc-actions-open').forEach(x=>{x.classList.remove('cc-actions-open');const t=x.querySelector('[data-cc-actions]');if(t)t.setAttribute('aria-expanded','false');});const picker=document.querySelector('.cc-reaction-picker');if(picker)picker.remove();reactionTarget=null;};
    if(actionDismissOff){actionDismissOff();actionDismissOff=null;}
    const dismissPointer=e=>{if(!(e.target&&e.target.closest&&e.target.closest('.cc-message-actions,.cc-reaction-picker,.emoji-pop')))closeMessageActions();};
    const dismissKey=e=>{if(e.key==='Escape')closeMessageActions();};
    const dismissBlur=()=>closeMessageActions();
    document.addEventListener('pointerdown',dismissPointer,true);document.addEventListener('keydown',dismissKey,true);window.addEventListener('blur',dismissBlur);
    actionDismissOff=()=>{document.removeEventListener('pointerdown',dismissPointer,true);document.removeEventListener('keydown',dismissKey,true);window.removeEventListener('blur',dismissBlur);};
    $$('[data-cc-delete]').forEach(button=>button.onclick=async()=>{ closeMessageActions();const room=saved()[state.community],storeId=channelStoreId(room,state.channel),messages=testMessages(storeId),id=button.dataset.ccDelete,found=messages.find(m=>messageId(m)===id),viewer=p.viewer?p.viewer():{}; if(!found||!viewer.pubkey)return;
      /* A MODERATOR MAY REMOVE SOMEBODY ELSE'S. This used to refuse anything but a self-delete and
         return SILENTLY, so an owner clearing spam pressed a button that did nothing and said
         nothing. The kind-5 goes out the same way either way; what differs is who the fold will
         let it remove, and that is checked there rather than restated here. */
      const own=found.pubkey===viewer.pubkey;
      /* Derived from `room`, never from an enclosing `isOwner`: this handler is a sibling of the
         function that defines one, and reaching for it throws on the first click (measured by
         concord_runtime.mjs: "isOwner is not defined"). Same rule as `bind()` documents. */
      const _ownerPk=String((room&&room.cord&&room.cord.bundle&&
        (room.cord.bundle.owner||room.cord.bundle.creator_npub))||'');
      const mayModerate=(!!_ownerPk&&_ownerPk===viewer.pubkey)
        ||(Array.isArray(room&&room.moderators)
           && room.moderators.indexOf(viewer.pubkey)>=0);
      if(!own&&!mayModerate){ p.toast('only this message\u2019s author or a moderator can remove it'); return; }
      const confirmed=p.uiConfirm?await p.uiConfirm(own?'Delete this message?'
        :'Remove this message from the room? Everyone will stop seeing it.',
        {ok:own?'Delete':'Remove',danger:true}):true; if(!confirmed)return; button.disabled=true; try{ const citation=!own&&!room.local&&room.cord&&window.PosterCordReader&&PosterCordReader.authorityCitation&&_ownerPk!==viewer.pubkey?PosterCordReader.authorityCitation(room.cord.bundle,roomControls.get(room.communityId||room.naddr)||[],viewer.pubkey,'MANAGE_MESSAGES'):[]; if(!room.local)await publishCordMessage(p,room,state.channel,'',[['e',id],['k',String(found.kind||9)],...citation],5); saveTestMessages(storeId,messages.filter(m=>messageId(m)!==id)); if(!removeMessageRow(id))preserveChatScroll(()=>render()); }catch(e){ button.disabled=false; p.toast('message was not deleted: '+(e&&e.message||e)); } });
    /* VOTING IS A MESSAGE TOO — kind 1018 into this channel's own stream, which is what makes the
     * answers readable by everybody in the room and by nobody outside it. `response` is Armada's
     * tag (its reader collects exactly that), and the `e` tag names the poll. A single-choice poll
     * replaces the voter's previous answer rather than adding to it: one person, one answer, and
     * the tally below already resolves to each voter's latest. */
    $$('[data-cc-poll]').forEach(b=>b.onclick=async()=>{
      const room=saved()[state.community],storeId=channelStoreId(room,state.channel),
        messages=testMessages(storeId),id=b.dataset.ccPoll,option=b.dataset.ccOption,
        found=messages.find(m=>messageId(m)===id),poll=found&&pollOf(found),
        viewer=p.viewer?p.viewer():{};
      if(!found||!poll||poll.ended||!viewer.pubkey)return;
      const mine=new Set();
      let newest=-1;
      for(const v of poll.votes||[]) if(v&&v.pubkey===viewer.pubkey&&(Number(v.ms)||0)>=newest){
        newest=Number(v.ms)||0; mine.clear(); for(const o of v.optionIds||[])mine.add(o); }
      const picked=poll.multi
        ? (mine.has(option)?[...mine].filter(x=>x!==option):[...mine,option])
        : [option];
      if(!pollVoteChanged(poll,viewer.pubkey,picked))return;
      b.disabled=true;
      try{
        if(!room.local){
          const tags=[['e',id],...picked.map(o=>['response',o])];
          await publishCordMessage(p,room,state.channel,'',tags,1018);
        }
        /* Painted from our own vote straight away. The relay echo arrives through the live
         * subscription and merges over this by pubkey — see the tally, which keeps only each
         * voter's newest answer, so the optimistic row cannot double-count itself. */
        found.votes=[...(found.votes||[]).filter(v=>v&&v.pubkey!==viewer.pubkey),
                     {pubkey:viewer.pubkey,optionIds:picked,ms:Date.now()}];
        saveTestMessages(storeId,messages);
        preserveChatScroll(()=>render());
      }catch(e){ b.disabled=false; p.toast('vote was not sent: '+(e&&e.message||e)); }
    });
    $$('[data-cc-thread]').forEach(b=>b.onclick=ev=>{
      // The "in thread" tag sits inside the quoted line, whose own click shows the original message.
      try{ ev&&ev.stopPropagation(); }catch(_){}
      state.thread=b.dataset.ccThread;
      /* Replying inside a thread means replying to it, not to the channel — so the composer is
         pointed at the root the moment the thread opens. */
      const room=saved()[state.community],all=activeMessages(room);
      replyTarget=all.find(x=>messageId(x)===state.thread)||null;
      render();
    });
    { const back=$('#cc-thread-back'); if(back)back.onclick=()=>{ state.thread=null; replyTarget=null; render(); }; }
    { const tt=$('#cc-threads-toggle'); if(tt)tt.onclick=()=>{ setThreadsInChat(!threadsInChat()); render(); }; }
    $$('[data-cc-thread-expand]').forEach(b=>b.onclick=ev=>{ try{ ev&&ev.stopPropagation(); }catch(_){}
      const id=b.dataset.ccThreadExpand; if(expandedThreads.has(id))expandedThreads.delete(id); else expandedThreads.add(id); render(); });
    /* EDIT (kind 3302, CORD / Vector build_edit_rumor): `e` names your own message, the content is
     * the replacement, and custom-emoji tags ride along (publishCordMessage adds them). */
    $$('[data-cc-pin]').forEach(b=>b.onclick=async()=>{ closeMessageActions(); const room=saved()[state.community],name=state.channel; if(!room)return; b.disabled=true;
      try{ const now=await togglePin(p,room,name,b.dataset.ccPin); render(); p.toast(now?'Message pinned':'Message unpinned'); }catch(e){ b.disabled=false; p.toast('the pin was not changed: '+(e&&e.message||e)); } });
    const pinsBtn=$('#cc-pins'); if(pinsBtn)pinsBtn.onclick=()=>{ const room=saved()[state.community],list=pinsOf(room,state.channel); if(!p.modal)return;
      p.modal(`<h3>📌 Pinned in #${p.enc(state.channel||'general')}</h3>${list.length?`<div class="cc-pin-list">${list.map(e=>{const pr=p.profOf?p.profOf(e.pubkey)||{}:{};return `<button class="cc-pin-item" data-cc-pin-jump="${p.enc(e.id)}"><b>${p.enc(pr.display_name||pr.name||String(e.pubkey).slice(0,12))}</b><span>${p.enc(mentionNames(String(e.content||''),p.profOf).slice(0,280))}</span>${e.edited?'<small>(edited)</small>':''}</button>`;}).join('')}</div>`:'<p class="muted">Nothing is pinned in this channel yet.</p>'}`,root=>{
        root.querySelectorAll('[data-cc-pin-jump]').forEach(x=>x.onclick=()=>{ p.closeModal(); const row=document.querySelector(`.cc-message[data-message-id="${CSS.escape(x.dataset.ccPinJump)}"]`); if(row){ row.scrollIntoView({block:'center'}); row.classList.add('cc-flash'); setTimeout(()=>row.classList.remove('cc-flash'),1600); } else p.toast('that message is older than the history loaded here'); });
      }); };
    $$('[data-cc-edit]').forEach(b=>b.onclick=()=>{ closeMessageActions();const room=saved()[state.community],channel=state.channel,storeId=channelStoreId(room,channel),found=testMessages(storeId).find(x=>messageId(x)===b.dataset.ccEdit),viewer=p.viewer?p.viewer():{}; if(!found||!canEditMessage(found,viewer,room)||!p.modal)return;
      p.modal(`<h3>Edit message</h3><textarea class="input cc-edit-text" rows="4" maxlength="65536"></textarea><div class="cc-join-actions"><button class="btn btn-ghost" data-cc-edit-cancel>Cancel</button><button class="btn btn-neon" data-cc-edit-save>Save</button></div>`,root=>{
        const box=root.querySelector('.cc-edit-text'),save=root.querySelector('[data-cc-edit-save]'),mentions=textareaMentions(p,box,room);box.value=mentions.readable(found.text);setTimeout(()=>{box.focus();box.setSelectionRange(box.value.length,box.value.length);},20);
        root.querySelector('[data-cc-edit-cancel]').onclick=()=>p.closeModal();
        box.onkeydown=e=>{if((e.key==='Enter')&&(e.ctrlKey||e.metaKey)){e.preventDefault();save.click();}};
        save.onclick=async()=>{ const typed=String(box.value||'').trim(); if(!typed){p.toast('an edit cannot be empty — delete the message instead');return;} const out=mentions.wire(typed),text=out.text; if(text===String(found.text||'').trim()){p.closeModal();return;} save.disabled=true;
          try{ await publishCordMessage(p,room,channel,text,[['e',messageId(found)],...out.tags],3302);
            const rows=testMessages(storeId),row=rows.find(x=>messageId(x)===messageId(found));
            if(row){ if(typeof row.originalText!=='string')row.originalText=row.text; row.text=text; row.tags=[...(row.tags||[]).filter(t=>t[0]!=='edited'),['edited',String(Math.floor(Date.now()/1000))]]; saveTestMessages(storeId,rows); }
            p.closeModal(); preserveChatScroll(()=>render());
          }catch(e){ save.disabled=false; p.toast('the edit was not sent: '+(e&&e.message||e)); } };
      }); });
    /* 🚩 REPORT A MESSAGE -- PRIVATELY. The room is encrypted, so a public NIP-56 (kind 1984) naming or
       quoting this message would publish its text and who is in the room. The report goes, as NIP-17
       gift-wrapped DMs, to the room's owner and moderators (reportRecipients: they can already read
       it), never to the member reported, and to this node's admins only when the person ticks the box.
       The sheet is the app's ONE report sheet (menus.js openReport) in its private mode. */
    $$('[data-cc-report]').forEach(b=>b.onclick=()=>{ closeMessageActions();
      const viewer=p.viewer?p.viewer():{},room=saved()[state.community],found=activeMessages(room).find(x=>messageId(x)===b.dataset.ccReport);
      if(!found||!found.pubkey||!viewer.pubkey||found.pubkey===viewer.pubkey||!p.openReport)return;
      const channel=state.channel||'general',target=found.pubkey,mods=reportRecipients(room,viewer.pubkey,target),
        admins=reportAdmins(p,room,viewer.pubkey,mods.concat([target]));
      p.openReport(target,null,{message:true,title:'Report message',
        subject:`A message in <b>${p.enc(room.name||'this community')}</b> · #${p.enc(channel)}`,
        note:mods.length?`This report is <b>not public</b>. It goes, end-to-end encrypted, only to this room’s owner and moderators (${mods.length}) — they can already read the room. The member you report is not told.`
                        :'This room has no other owner or moderator to send a report to. As its moderator you can remove the message or ban the member yourself.',
        adminOption:admins.length>0,
        deliver:async({label,comment,alsoAdmins})=>{
          const to=mods.concat(alsoAdmins?admins:[]);
          if(!to.length)return{ok:false,msg:'nobody in this room can receive a report'};
          const text=reportText(p,room,channel,found,viewer.pubkey,label,comment,alsoAdmins);
          let sent=0,last='';
          for(const pk of to){ try{ await p.sendDm(pk,text); sent++; }catch(e){ last=(e&&e.message)||String(e); } }
          if(!sent)return{ok:false,msg:last||'the report could not be sent'};
          return{ok:true,said:sent===to.length?(alsoAdmins&&admins.length?'Report sent to the room’s moderators and this server’s admins.':'Report sent to the room’s moderators.')
                                               :`Report sent to ${sent} of ${to.length} — the rest could not be reached.`};
        }});
    });
    $$('[data-cc-reply]').forEach(b=>b.onclick=()=>{ closeMessageActions();const room=saved()[state.community],m=activeMessages(room),found=m.find(x=>messageId(x)===b.dataset.ccReply); if(!found)return; replyTarget=found; render(); const box=$('#cc-input'); if(box)box.focus(); });
    $$('.cc-message-reply').forEach(b=>{ b.setAttribute('role','button');b.tabIndex=0;b.title='Show original message';const row=b.closest('.cc-message'),room=saved()[state.community],messages=activeMessages(room),message=row&&messages.find(x=>messageId(x)===row.dataset.messageId),original=message&&message.reply&&String(message.reply.id||'');const jump=()=>{const target=[...document.querySelectorAll('.cc-message[data-message-id]')].find(x=>x.dataset.messageId===original);if(!target){p.toast('original message is not in the loaded room history');return;}const st=readScroll(scrollKey());st.pinned=false;target.scrollIntoView({block:'center',behavior:'smooth'});target.classList.add('cc-message-target');setTimeout(()=>{if(target.isConnected)target.classList.remove('cc-message-target');},1800);setTimeout(()=>{const box=document.querySelector('.cc-messages');if(box){st.top=box.scrollTop;st.height=box.scrollHeight;writeScroll(scrollKey(),st);}},500);};b.onclick=jump;b.onkeydown=e=>{if(e.key==='Enter'||e.key===' '){e.preventDefault();jump();}}; });
    const toggleReaction=async(id,emoji)=>{ const room=saved()[state.community],storeId=channelStoreId(room,state.channel),m=testMessages(storeId),found=m.find(x=>messageId(x)===id),viewer=p.viewer?p.viewer():{},who=viewer.pubkey||'local-user'; if(!found)return; if(!found.reactions||typeof found.reactions!=='object')found.reactions={};
      /* A CUSTOM EMOJI IS A SHORTCODE PLUS A URL, and without the URL it reaches every other
         client as the literal text ':partyblob:'. NIP-30 carries it in an `emoji` tag beside the
         content, which is what `reactionUrls` on the read side has always expected — the picker was
         simply never able to produce one, because it offered eight hardcoded faces and none of the
         community's own. Reported as "not displaying custom emojis on the server". */
      const _sc=/^:([A-Za-z0-9_+\-]{1,40}):$/.exec(String(emoji||''));
      const _url=_sc&&p.instEmojiUrl?String(p.instEmojiUrl(_sc[1])||''):'';
      const _extra=(_sc&&/^https?:\/\//i.test(_url))?[['emoji',_sc[1],_url]]:[];
      const people=Array.isArray(found.reactions[emoji])?found.reactions[emoji]:[],i=people.indexOf(who); try{if(!room.local){if(i<0)await publishCordMessage(p,room,state.channel,emoji,[['e',id],['p',found.pubkey||''],['k',String(found.kind||9)],..._extra],7);else{const rid=found.reactionIds&&found.reactionIds[emoji]&&found.reactionIds[emoji][who];if(!rid)throw new Error('refresh the room before removing this reaction');await publishCordMessage(p,room,state.channel,'',[['e',rid],['k','7']],5);}}}catch(e){p.toast('reaction was not sent: '+(e&&e.message||e));return;} if(i<0)people.push(who);else people.splice(i,1); if(people.length)found.reactions[emoji]=people;else delete found.reactions[emoji];
      // So the optimistic repaint draws the picture rather than the raw shortcode.
      if(_extra.length){ if(!found.reactionUrls||typeof found.reactionUrls!=='object')found.reactionUrls={}; found.reactionUrls[emoji]=_url; } saveTestMessages(storeId,m); reactionTarget=null; preserveChatScroll(()=>render()); };
    $$('[data-cc-actions]').forEach(b=>b.onclick=e=>{e.stopPropagation();const row=b.closest('.cc-message'),open=!row.classList.contains('cc-actions-open');$$('.cc-message.cc-actions-open').forEach(x=>{x.classList.remove('cc-actions-open');const t=x.querySelector('[data-cc-actions]');if(t)t.setAttribute('aria-expanded','false');});row.classList.toggle('cc-actions-open',open);b.setAttribute('aria-expanded',String(open));});
    $$('[data-cc-react-toggle]').forEach(b=>b.onclick=()=>toggleReaction(b.dataset.ccReactToggle,b.dataset.ccEmoji));
    $$('[data-cc-react]').forEach(b=>b.onclick=()=>{ const target=b.dataset.ccReact;
      /* MEASURED FIRST. closeMessageActions() hides this very button under `(hover:none)`, and a
         button with no box places the picker in the corner of the screen. */
      let measured=null;
      try{ const r=b.getBoundingClientRect(); measured={left:r.left,top:r.top,right:r.right,bottom:r.bottom,width:r.width,height:r.height,offsetWidth:b.offsetWidth}; }catch(_){ measured=null; }
      closeMessageActions();reactionTarget=target;
      /* ONE PICKER, NOT TWO. The app already has an emoji popover with the instance's own packs,
         search, recents and thousands of custom emoji — the composer beside this button uses it,
         and its own comment says it was built to serve reactions. This offered eight hardcoded
         faces and none of the community's, which is what "not displaying custom emojis on the
         server" was. Reusing it also retires the second placement implementation that put the
         picker in the corner of the screen.
         The inline grid stays as a FALLBACK for a shell whose bundle predates the popover: losing
         eight faces is a smaller failure than a react button that does nothing. */
      if(p.openEmojiPopover){
        p.openEmojiPopover(b,(emoji,close)=>{ if(close)close(); const id=target; closeMessageActions(); toggleReaction(id,emoji); },{anchored:true,anchorRect:measured && measured.width>0 && measured.height>0 ? measured : b.closest('.cc-message')?.getBoundingClientRect()});
        return;
      }
      const choices=['👍','❤️','😂','😮','😢','😡','🎉','💯']; const pop=document.createElement('div'); pop.className='cc-reaction-picker'; pop.innerHTML=choices.map(x=>`<button data-emoji="${x}">${x}</button>`).join(''); placeReactionPicker(b,pop,measured); pop.querySelectorAll('button').forEach(x=>x.onclick=e=>{ e.stopPropagation();const id=reactionTarget;closeMessageActions();toggleReaction(id,x.dataset.emoji); }); });
    $$('[data-cc-retry-delivery]').forEach(b=>b.onclick=async()=>{
      const d=deliveries.get(deliveryId(deliveryOwner(p),b.dataset.ccRetryDelivery));if(!d||d.job)return;
      b.disabled=true;try{await deliverPrepared(p,d);}catch(e){if(deliveryOwner(p)===d.owner)p.toast('Delivery needs attention: '+(e.message||e));}finally{backgroundRender();}
    });
    $$('[data-cc-zap]').forEach(b=>b.onclick=()=>{closeMessageActions();const room=saved()[state.community],storeId=channelStoreId(room,state.channel),messages=testMessages(storeId),target=messages.find(m=>messageId(m)===b.dataset.ccZap);if(!target)return;const lightning=async amount=>{b.disabled=true;let proof=null;try{p.toast('paying private zap…');proof=await p.payPrivateConcordZap(target.pubkey,amount);const tags=[['e',messageId(target)],['p',target.pubkey],['k',String(target.kind||9)],['amount',String(proof.amountMsats)],['bolt11',proof.bolt11],['preimage',proof.preimage]];const made=await publishCordNative(p,room,state.channel,'',tags,9735);target.zaps=[...(target.zaps||[]),{id:made.rumorId,pubkey:(p.viewer&&p.viewer().pubkey)||'',sats:amount,comment:'',rail:'lightning'}];saveTestMessages(storeId,messages);preserveChatScroll(()=>render());p.toast('⚡ privately zapped '+amount+' sats');}catch(e){b.disabled=false;p.toast((proof?'payment succeeded, but the private tally was not posted: ':'private zap failed: ')+(e&&e.message||e));}};return p.startConcordTip(target.pubkey,lightning);});
  }
  async function webxdcCordParts(ctx){
    const p=PC(),room=saved().find(r=>roomIdentity(r)===ctx.room),reader=window.PosterCordReader,bundle=room&&room.cord&&room.cord.bundle,
      channel=room&&(room.channels||[]).find(c=>c.id===ctx.channelId||c.name===ctx.channel);
    if(!p||!room||!reader||!bundle||!channel)throw new Error('Concord Webxdc channel is unavailable');
    const loadKey=room.communityId||room.naddr,relays=roomRelays(bundle),initial=cordPlaneContext(p,bundle,roomControls.get(loadKey)||[],room);
    let controls=roomControls.get(loadKey);
    if(!controls){
      const seed=reader.inspectControl(bundle,[]);
      controls=await queryEnvelopeHistory(p,relays,seed.controlPubkeys,await cachedEnvelopes(envelopeCacheKey(loadKey,'control')),{plane:initial});
      if(!initial.current())throw new Error('Concord membership changed');
      roomControls.set(loadKey,controls||[]);
    }
    if(!initial.membershipCurrent())throw new Error('Concord membership changed');
    const view=reader.inspectControl(bundle,controls||[]),wireChannel=view.channels.find(c=>c.id===channel.id);
    return{p,room,reader,bundle,channel,loadKey,relays,controls:controls||[],streamPubkeys:wireChannel&&wireChannel.streamPubkeys||[],plane:cordPlaneContext(p,bundle,controls||[],room)};
  }
  async function callModule(file, name) {
    if(window[name])return window[name];
    if(!callModules.has(file))callModules.set(file,new Promise((resolve,reject)=>{
      const script=document.createElement('script');
      script.src=concordScriptUrl?new URL(file,concordScriptUrl).href:'/static/js/client/'+file;
      script.onload=()=>resolve(window[name]);script.onerror=()=>{callModules.delete(file);reject(new Error('Concord call components could not load'));};
      document.head.appendChild(script);
    }));
    return callModules.get(file);
  }
  /* VOICE ON THE CHANNEL LIST (CORD-07). "Every Channel is callable -- there is no separate 'voice
   * Channel' type": a call is simply started in a channel. What a member reported as "I don't see
   * voice channels" is that nothing on the list ever said a call was LIVE -- the only way in was the
   * header's phone button for the channel already open, and who was in it showed nowhere outside
   * the call panel. Armada draws a speaker on an occupied channel and the people under it; this is
   * that, from the same signed presence (kind 23313 in an ephemeral 21059 wrap on the channel's own
   * stream, folded by PCCordVoice.fold: latest per member, "joined" within 90 s, one SFU identity).
   *
   * ONE live subscription per community, for every callable channel's stream, held while the
   * community is open. Presence is ephemeral -- relays store nothing -- so a list opened mid-call
   * fills in at the next 30 s heartbeat, never from history. Forum channels offer no call (Armada).
   * The listening time and what was heard are handed to the call panel, which is what lets a row tap
   * join at once instead of listening for another 30 s (CORD-07 §5's rendezvous needs only that
   * someone listened). */
  const voiceWatch={key:'',quick:'',room:'',since:0,sig:'',stops:[],rows:new Map(),timer:null,retry:null};
  let voiceActive=null;   // {room, channelId} while THIS device is in a channel's call
  function stopVoiceWatch(){
    for(const stop of voiceWatch.stops)try{stop();}catch(_){}
    if(voiceWatch.timer)clearInterval(voiceWatch.timer);if(voiceWatch.retry)clearTimeout(voiceWatch.retry);
    Object.assign(voiceWatch,{key:'',quick:'',room:'',since:0,sig:'',stops:[],rows:new Map(),timer:null});
  }
  function voicePeople(room,channelId){
    if(!room||!channelId||roomIdentity(room)!==voiceWatch.room||!window.PCCordVoice)return [];
    const rows=voiceWatch.rows.get(channelId);if(!rows||!rows.size)return [];
    try{return PCCordVoice.fold([...rows.values()]).sort((a,b)=>a.pubkey<b.pubkey?-1:a.pubkey>b.pubkey?1:0);}catch(_){return [];}
  }
  function voiceInCall(room,channelId){return !!(voiceActive&&room&&voiceActive.room===roomIdentity(room)&&voiceActive.channelId===channelId);}
  function voiceRepaint(){
    const room=saved().find(r=>roomIdentity(r)===voiceWatch.room);
    const sig=[...voiceWatch.rows.keys()].sort().map(id=>id+':'+voicePeople(room,id).map(x=>x.pubkey).join('+')).join('|')+'|'+JSON.stringify(voiceActive);
    if(sig===voiceWatch.sig)return;voiceWatch.sig=sig;
    if(typeof document!=='undefined'&&document.body.classList.contains('concord-view'))preserveChatScroll(()=>backgroundRender());
  }
  async function voiceIngest(key,identity,channelId,wraps){
    const reader=window.PosterCordReader;if(!key||voiceWatch.key!==key||!reader)return;
    const room=saved().find(r=>roomIdentity(r)===identity);if(!room||!room.cord||!room.cord.bundle)return;
    let rows=[];
    try{rows=await reader.inspectVoicePresence(room.cord.bundle,roomControls.get(room.communityId||room.naddr)||[],channelId,wraps);}catch(_){return;}
    if(voiceWatch.key!==key)return;
    const held=voiceWatch.rows.get(channelId)||new Map();
    for(const e of rows){const old=held.get(e.pubkey);if(!old||e.at>old.at||(e.at===old.at&&String(e.id)<String(old.id)))held.set(e.pubkey,e);}
    while(held.size>256)held.delete(held.keys().next().value);
    voiceWatch.rows.set(channelId,held);voiceRepaint();
  }
  async function startVoiceWatch(p,room){
    try{
      const open=typeof document!=='undefined'&&document.body.classList.contains('concord-view');
      if(!open||!room||room.local||room.protocol==='nip29'||!room.cord||!room.cord.bundle||room.cord.bundle.dissolved){if(voiceWatch.key)stopVoiceWatch();return;}
      const reader=window.PosterCordReader,loadKey=room.communityId||room.naddr,controls=roomControls.get(loadKey);
      if(!reader||!reader.voiceMaterial||!reader.inspectVoicePresence||!window.PCCordSpaces)return;
      /* The control history arrives after the first paint; nothing else repaints just because it
       * did, so ask again shortly rather than leaving the list without presence for the visit. */
      if(!controls){if(!voiceWatch.retry)voiceWatch.retry=setTimeout(()=>{voiceWatch.retry=null;void startVoiceWatch(p,saved()[state.community]);},2000);return;}
      const owner=String(p.viewer&&p.viewer().pubkey||''),bundle=room.cord.bundle;
      const quick=owner+'|'+roomIdentity(room)+'|'+controls.length+'|'+String(bundle.root_epoch||'')+'|'+(bundle.channels||[]).length;
      if(voiceWatch.quick===quick)return;
      const streams=[];
      for(const c of reader.inspectControl(bundle,controls).channels.filter(c=>c.view!=='forum'))
        try{streams.push({id:c.id,stream:reader.voiceMaterial(bundle,controls,c.id).stream});}catch(_){}
      const key=owner+'|'+roomIdentity(room)+'|'+streams.map(x=>x.id+':'+x.stream).join(',');
      if(voiceWatch.key===key){voiceWatch.quick=quick;return;}
      stopVoiceWatch();voiceWatch.quick=quick;
      if(!streams.length)return;
      Object.assign(voiceWatch,{key,room:roomIdentity(room),since:Date.now()});
      await callModule('cord-voice.js','PCCordVoice');if(voiceWatch.key!==key)return;
      const byStream=new Map(streams.map(x=>[x.stream,x.id])),identity=roomIdentity(room);
      voiceWatch.stops.push(cordPlaneSubscribe(p,(window.PCConcordRelay||window.Relay),roomRelays(bundle),
        [{kinds:[21059],authors:streams.map(x=>x.stream),since:Math.floor(Date.now()/1000)-90}],
        {onEvent:wrap=>{const id=byStream.get(wrap&&wrap.pubkey);if(id)void voiceIngest(key,identity,id,[wrap]);},live:true,timeout:0,max:1},
        cordPlaneContext(p,bundle,controls,room)));
      // A member who stops heart-beating drops off after 90 s with no event to say so.
      voiceWatch.timer=setInterval(voiceRepaint,5000);
    }catch(e){console.warn('Concord voice presence unavailable',e&&e.message||e);}
  }
  async function startCordCall(p, initial, channelName, opts={}) {
    const owner=String(p.viewer&&p.viewer().pubkey||''),identity=roomIdentity(initial);
    if(!owner||!initial||initial.local||initial.protocol==='nip29')throw new Error('This community does not support Concord calls');
    const current=()=>String(p.viewer&&p.viewer().pubkey||'')===owner&&saved().some(r=>roomIdentity(r)===identity);
    let channelId=null;
    const parts=()=>{
      if(!current())throw new Error('The call membership changed');
      const room=saved().find(r=>roomIdentity(r)===identity),bundle=room.cord&&room.cord.bundle,
        reader=window.PosterCordReader,controls=roomControls.get(room.communityId||room.naddr)||[];
      if(!reader||!reader.voiceMaterial||!bundle)throw new Error('Wait for the community keys to load');
      const channel=reader.inspectControl(bundle,controls).channels.find(c=>channelId?c.id===channelId:c.name===channelName);
      if(!channel)throw new Error('The channel is no longer available');
      channelId=channel.id;
      return {room,bundle,reader,controls,channel,relays:roomRelays(bundle)};
    };
    await callModule('cord-voice.js','PCCordVoice');const calls=await callModule('cord-call.js','PCCordCall');
    if(!current())return;
    let subscription=null;
    return calls.open({name:channelName,current,
      preferred:localStorage.getItem('pc.concord.call-broker')||'https://armada.buzz',
      joinOnOpen:!!opts.joinOnOpen,
      listened:()=>voiceWatch.key&&voiceWatch.room===identity?Date.now()-voiceWatch.since:0,
      seed:()=>voiceWatch.room===identity&&channelId?[...(voiceWatch.rows.get(channelId)||new Map()).values()]:[],
      remember:value=>{if(value)localStorage.setItem('pc.concord.call-broker',value);},
      label:pk=>{const profile=p.profOf&&p.profOf(pk)||{};return profile.display_name||profile.name||pk.slice(0,12);},
      material:()=>{const x=parts();return x.reader.voiceMaterial(x.bundle,x.controls,x.channel.id);},
      subscribe:onEvent=>{const x=parts(),material=x.reader.voiceMaterial(x.bundle,x.controls,x.channel.id);
        subscription=cordPlaneSubscribe(p,(window.PCConcordRelay||window.Relay),x.relays,[{kinds:[21059],authors:[material.stream],since:Math.floor(Date.now()/1000)-90}],
          {onEvent,live:true,timeout:0},cordPlaneContext(p,x.bundle,x.controls,x.room));return subscription;},
      decode:wrap=>{const x=parts();return x.reader.inspectVoicePresence(x.bundle,x.controls,x.channel.id,[wrap]);},
      presence:async(verb,peer,broker)=>{
        const x=parts(),plane=cordPlaneContext(p,x.bundle,x.controls,x.room);
        const sign=async template=>{if(!current())throw new Error('The call account changed');const signed=await p.signTemplate(template);if(!current())throw new Error('The call account changed');return signed;};
        const wrap=await x.reader.createVoicePresence(x.bundle,x.controls,x.channel.id,verb,owner,sign,peer,broker);
        if(!plane.current())throw new Error('The call keys changed while signing');
        /* Our own row first: a relay need not echo an ephemeral event back to its sender. */
        voiceActive=verb==='joined'?{room:identity,channelId:x.channel.id}:(voiceActive&&voiceActive.channelId===x.channel.id?null:voiceActive);
        void voiceIngest(voiceWatch.key,identity,x.channel.id,[wrap]);voiceRepaint();
        if(subscription&&subscription.publish&&subscription.publish(wrap))return;
        const result=await p.relayPublishRoom(x.relays,wrap,cordPlaneAuth(p,plane,wrap.pubkey,x.relays));
        if(!result||!result.ok)throw new Error('Call presence could not reach the community relays');
      }
    });
  }
  async function webxdcQuery(ctx,uuid){const p=PC();if(ctx.protocol==='nip29')return nip29RelayQuery(p,ctx.relay,[{kinds:[9450],'#h':[ctx.groupId],'#i':[uuid],limit:500}],10000);const x=await webxdcCordParts(ctx),key=envelopeCacheKey(x.loadKey,x.channel.id),cached=await cachedEnvelopes(key),wraps=await queryEnvelopeHistory(x.p,x.relays,x.streamPubkeys,cached,{plane:cordPlaneContext(x.p,x.bundle,x.controls,x.room)}),fresh=wraps.filter(ev=>!cached.some(old=>old.id===ev.id));await cacheEnvelopes(key,fresh);const rows=await x.reader.inspectWebxdc(x.bundle,x.controls,x.channel.id,wraps,uuid,false);if(!x.plane.current())return [];try{window.PCWebxdc&&PCWebxdc.rtDiagnostic('static-replay',uuid+' '+rows.length+'/'+wraps.length);}catch(_){}return rows;}
  async function webxdcPublish(ctx,uuid,content,meta,realtime,liveSub){const p=PC(),tags=[['i',uuid],['alt',realtime?'Webxdc realtime':'Webxdc update']];if(realtime)tags.push(['rt','1']);for(const n of ['info','document','summary'])if(meta&&meta[n])tags.push([n,String(meta[n]).slice(0,200)]);if(ctx.protocol==='nip29')return p.publishNip29Authed(ctx.relay,{kind:realtime?24450:9450,created_at:Math.floor(Date.now()/1000),content,tags:[['h',ctx.groupId],...tags]});const x=await webxdcCordParts(ctx),viewer=x.p.viewer(),made=await x.reader.createWebxdcWrap(x.bundle,x.controls,x.channel.id,content,viewer.pubkey,x.p.signTemplate,tags,realtime);if(!x.plane.current())throw new Error('Concord membership changed while signing');
    /* publishTo deliberately skips managed relays. It was the receive-side bug's mirror image: a
     * perfectly connected room relay meant every Webxdc packet was sent to zero sockets. Reuse the
     * subscription's external sockets and send to one matching managed socket, never a random relay. */
    if(realtime){const sent=(!x.reader.createPlaneAuth&&p.relayPublishFastTo?p.relayPublishFastTo(x.relays,made.wrap):0)+(liveSub&&liveSub.publish?liveSub.publish(made.wrap):0);if(!sent)throw new Error('no live room relay');return made;}
    if(x.reader.createPlaneAuth){const result=await p.relayPublishRoom(x.relays,made.wrap,cordPlaneAuth(x.p,cordPlaneContext(x.p,x.bundle,x.controls,x.room),made.wrap.pubkey,x.relays));if(!result?.ok)throw new Error(result?.msg||'room relays rejected the update');return made;}
    /* The ROOM's relays only. This used to also hand the envelope to the user's general relay pool
     * (public relays that have nothing to do with the room), so every Webxdc move went out twice
     * over and landed on relays no member reads. relayPublishRoom includes a managed socket that
     * IS a room relay, which is the case publishTo alone skipped. */
    const result=await p.relayPublishRoom(x.relays,made.wrap,null);if(!result||!result.ok)throw new Error(result&&result.msg||'room relays rejected the update');return made;}
  async function webxdcSubscribe(ctx,uuid,realtime,onEvent){
    const R=(window.PCConcordRelay||window.Relay);if(!R||!R.subscribe||!R.subscribeFrom)throw new Error('relay subscription unavailable');
    let urls,filters,receive,plane=null;
    if(ctx.protocol==='nip29'){
      urls=[ctx.relay];filters=[{kinds:[realtime?24450:9450],'#h':[ctx.groupId],'#i':[uuid],since:Math.floor(Date.now()/1000)-120}];receive=onEvent;
    }else{
      const x=await webxdcCordParts(ctx),kind=realtime?21059:1059,filter={kinds:[kind],authors:x.streamPubkeys};
      if(realtime)filter.since=Math.floor(Date.now()/1000)-120;
      urls=x.relays;filters=[filter];plane=x.reader.createPlaneAuth&&cordPlaneContext(x.p,x.bundle,x.controls,x.room);receive=async wrap=>{try{const rows=await x.reader.inspectWebxdc(x.bundle,x.controls,x.channel.id,[wrap],uuid,realtime);if(!x.plane.current())return;for(const row of rows)onEvent(row);}catch(_){}};
    }
    /* subscribeFrom intentionally skips URLs already owned by Relay's managed pool. Using it alone
     * therefore subscribed to NOTHING for the common case where a Concord relay was also a normal
     * account relay. Listen on the pool and only use temporary sockets for the remaining URLs. */
    const pooled=plane?null:R.subscribe(filters,{onEvent:receive}),external=plane?cordPlaneSubscribe(PC(),R,urls,filters,{onEvent:receive},plane):R.subscribeFrom(urls,filters,{onEvent:receive});
    const gates=[];
    if(pooled&&R.waitForSubscription)gates.push(R.waitForSubscription(pooled,urls).then(ok=>{if(!ok)throw new Error('managed room relay did not open');}));
    if(external.hasTargets&&external.ready)gates.push(external.ready.then(ok=>{if(!ok)throw new Error('external room relay did not open');}));
    try{if(gates.length)await Promise.any(gates);}catch(_){R.close(pooled);external();throw new Error('room relay subscription could not open');}
    const close=()=>{try{R.close(pooled);}catch(_){}try{external();}catch(_){}};
    close.publish=event=>(!plane&&R.publishFastTo&&R.publishFastTo(urls,event)?1:0)+(external.publish?external.publish(event):0);
    return close;
  }
  /* CORD-04 Webxdc lobby signalling. Armada/Vector put the topic in the encrypted JSON body and
   * intentionally omit the update stream's `i` tag. Reusing webxdcPublish/webxdcSubscribe would
   * therefore create a private PosterChan-only lobby: our reader filters updates by `i`, while the
   * ecosystem peers never emit one. */
  async function webxdcPeerPublish(ctx,content,liveSub){
    if(!ctx||ctx.protocol!=='concord2')throw new Error('Iroh peer signalling requires a Concord channel');
    const x=await webxdcCordParts(ctx),viewer=x.p.viewer(),made=await x.reader.createWebxdcWrap(x.bundle,x.controls,x.channel.id,content,viewer.pubkey,x.p.signTemplate,[],false);if(!x.plane.current())throw new Error('Concord membership changed while signing');
    if(x.reader.createPlaneAuth){const result=await x.p.relayPublishRoom(x.relays,made.wrap,cordPlaneAuth(x.p,cordPlaneContext(x.p,x.bundle,x.controls,x.room),made.wrap.pubkey,x.relays));if(!result?.ok)throw new Error(result?.msg||'room relays rejected the peer signal');return made;}
    /* Publish on the room subscription's actual sockets. A generic pool success may be an unrelated
     * account relay and cannot prove an Armada peer can see this advertisement. */
    const sent=(x.p.relayPublishFastTo?x.p.relayPublishFastTo(x.relays,made.wrap):0)+(liveSub&&liveSub.publish?liveSub.publish(made.wrap):0);
    if(!sent){const accepted=await x.p.relayPublishTo(x.relays,made.wrap);if(!accepted)throw new Error('room relays rejected the peer signal');}
    return made;
  }
  async function webxdcPeerQuery(ctx){
    if(!ctx||ctx.protocol!=='concord2')throw new Error('Iroh peer signalling requires a Concord channel');
    const x=await webxdcCordParts(ctx);if(!x.reader.inspectWebxdcSignals)throw new Error('peer signalling unavailable');
    const filters=[{kinds:[1059],authors:x.streamPubkeys,limit:5000}],history=await cordQuery(x.p,x.relays,filters,{timeout:10000,max:8,plane:cordPlaneContext(x.p,x.bundle,x.controls,x.room)});
    const rows=await x.reader.inspectWebxdcSignals(x.bundle,x.controls,x.channel.id,history);return x.plane.current()?rows:[];
  }
  async function webxdcPeerSubscribe(ctx,onEvent){
    if(!ctx||ctx.protocol!=='concord2')throw new Error('Iroh peer signalling requires a Concord channel');
    const R=(window.PCConcordRelay||window.Relay),x=await webxdcCordParts(ctx);
    if(!R||!R.subscribe||!R.subscribeFrom||!x.reader.inspectWebxdcSignals)throw new Error('peer signalling unavailable');
    const seen=new Set(),receive=async wrap=>{try{const rows=await x.reader.inspectWebxdcSignals(x.bundle,x.controls,x.channel.id,[wrap]);if(!x.plane.current())return;for(const row of rows)if(!seen.has(row.id)){seen.add(row.id);onEvent(row);}}catch(_){}};
    const plane=x.reader.createPlaneAuth&&cordPlaneContext(x.p,x.bundle,x.controls,x.room),filters=[{kinds:[1059],authors:x.streamPubkeys,limit:1000}],pooled=plane?null:R.subscribe(filters,{onEvent:receive}),external=plane?cordPlaneSubscribe(x.p,R,x.relays,filters,{onEvent:receive},plane):R.subscribeFrom(x.relays,filters,{onEvent:receive});
    /* Backfill after opening the live subscription, so an advertisement published during the query
     * cannot fall into the gap. The id set makes the overlap harmless. */
    /* Do not hold joinRealtimeChannel (and Quake's host-election burst) behind a ten-second history
     * query. The live listener is already installed; fold the durable advertisements when their
     * backfill arrives. */
    void cordQuery(x.p,x.relays,filters,{timeout:10000,max:8,plane:cordPlaneContext(x.p,x.bundle,x.controls,x.room)}).then(history=>x.reader.inspectWebxdcSignals(x.bundle,x.controls,x.channel.id,history)).then(rows=>{if(!x.plane.current())return;for(const row of rows)if(!seen.has(row.id)){seen.add(row.id);onEvent(row);}}).catch(()=>{});
    const close=()=>{try{R.close(pooled);}catch(_){}try{external();}catch(_){}};
    close.publish=event=>(!plane&&R.publishFastTo&&R.publishFastTo(x.relays,event)?1:0)+(external.publish?external.publish(event):0);
    return close;
  }
  window.PCConcord={cordVia,mirrorAllJoined,refreshTunnel,cordDeps,cordRelay,communitySettingsOpen,closeCommunitySettings,legacyRooms,unclaimedLegacyRooms,claimLegacyRooms,adoptOwnedLegacyRooms,announceListing,pollVoteChanged,render,backgroundRender,refoundingBeforeEvents:true,reviewRefoundingRecipients,refoundRoom,acquireRefoundingControls,createPrivateRoomChannel,mergeDirectInviteRoom,showDirectInvitations,showDirectInviteSender,saveCommunitySettings,timerSettingsHtml,unreadRooms,paintUnreadBadge,emptyChannelHtml,channelUnread,noteChannelReach,invitePreviewHtml,declineInvite,inviteWasDeclined,forgetDeclinedInvite,declinedInvites,sameRoom,uniqueRooms,wake,iconRef,readChat,reconcileChannels,startChatLive,stopChatLive,pollOf,pollHtml,refreshActiveChannel,warmRoomIcons,hydrateRoomStreams,replyParentId,threadRootId,threadIndex,threadView,openInvite:openInviteLink,openNotification,notificationRoute,inviteParts,normalizeIcon,roomIcon,roomRelays,reactionSummary,reactionPickerPosition,notifyMentions,discoverInvites,recoverOwnedInvite,membershipEvents,decodeMembershipLists,mergeArmadaBundle,syncArmadaMemberships,nip29MembershipTags,nip29Memberships,nip29Metadata,nip29History,foldNip29History,nip29PreviousTags,publishNip29Message,syncNip29Memberships,hydrateNip29Room,activateJoinedRoom,resumeActiveRoom,threadParticipants,cordReplyTags,roomParticipants,typedMentionRecipients,textMentionsViewer,paintMentions,messageMentionsViewer,conversationIsVisible,repaintScrollTop,pendingEchoMatch,applyRoomIconMetadata,channelSectionsHtml,removeCommunityByIdentity,persistArmadaMembership,persistArmadaMemberships,leaveArmadaMembership,checkMemberships,restoreMemberships,leftCommunities,rememberLeftCommunity,forgetLeftCommunity,wasLocallyLeft,memberTapAction,memberViewportIsNarrow,encryptedAttachments,publicAttachments,messageContentHtml,wireRoomMedia,handoffState,acceptHandoff,beginComposerSend,restoreFailedComposer,webxdcOf,resolveWebxdcCard,deriveWebxdcUrlTopic,hydrateWebxdcCards,webxdcQuery,webxdcPublish,webxdcSubscribe,webxdcPeerQuery,webxdcPeerPublish,webxdcPeerSubscribe,refreshOwnedInviteLinks,changeOwnedInviteLink,showOwnedInviteLinks,mintPublicRoom};
  /* A monitor destination may load this module only after its frame-handoff callback has returned.
   * Adopt the one-shot room/channel before app.js invokes render(), then remove it so an ordinary
   * later Communities open cannot replay an old monitor move. */
  /* Two doors for the runtime tests, and nothing else uses them: the view position and the
   * channel store are module-private on purpose, and a live-message test cannot assert anything
   * without being able to say "the room is open" and "what is in the store now". */
  window.PCConcord.__testState=v=>{ if(v&&'community' in v)state.community=v.community;
                                    if(v&&'channel' in v)state.channel=v.channel;
                                    if(v&&v.controls)roomControls.set(v.controls[0],v.controls[1]);
                                    return state; };
  window.PCConcord.notificationsOn=()=>concordNotifyOn();
  window.PCConcord.__testInvite=v=>{ pendingInvite=v||null; render(); };
  window.PCConcord.__instanceJoin=(hydrate)=>{ instanceJoinSession=''; return joinInstanceCommunity(PC(),hydrate); };   // the instance community, with a fixture invite reader   // an invitation on screen without fetching one
  window.PCConcord.__voiceWatch=()=>({key:voiceWatch.key,room:voiceWatch.room,listening:voiceWatch.key?Date.now()-voiceWatch.since:0,channels:[...voiceWatch.rows].map(([id,rows])=>[id,rows.size]),active:voiceActive});   // what the channel list believes about calls
  window.PCConcord.__testMessages=id=>testMessages(id);
  /* The third door: the LIVE repaint exactly as an arriving message performs it. A scroll test that
     calls backgroundRender() directly is testing a path no message ever takes — it skips both the
     gesture hold and the anchor preservation, which are the two things that decide whether the
     reader keeps their place. */
  window.PCConcord.__testLiveRepaint=()=>whenHandLeaves(()=>preserveChatScroll(()=>backgroundRender()));
  window.PCConcord.__testEnterChatBottom=()=>enterChatBottom(true);
  window.PCConcord.__testStartRoomsLive=p=>startRoomsLive(p);
  window.PCConcord.__testPatchServerRail=()=>patchServerRail();
  window.PCConcord.__testSetControls=(loadKey,wraps)=>roomControls.set(loadKey,wraps);
  window.PCConcord.mentionsUnread=()=>mentionsUnread();
  window.PCConcord.__testNotifyMentions=(p,room,msgs,viewer,me,ch)=>notifyMentions(p,room,msgs,viewer,me,ch);
  window.PCConcord.__testClearMentions=(room,ch)=>clearMentions(room,ch);
  window.PCConcord.__testNoteChannelRead=(p,room,ch,at)=>noteChannelRead(p,room,ch,at);
  window.PCConcord.__testRefreshReadDoc=(p,force)=>refreshReadDoc(p,force);
  window.PCConcord.__testStartReadSync=p=>startReadSync(p);
  window.PCConcord.__testReadDoc=p=>loadReadDoc(p).then(()=>({answered:readDoc.answered,marks:{...readDoc.marks},saving:readDoc.saving}));
  if(window.__pcConcordHandoff){
    try{acceptHandoff(window.__pcConcordHandoff);}finally{delete window.__pcConcordHandoff;}
  }
})();
