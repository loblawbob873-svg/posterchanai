/* PosterChan Nostr client controller. Talks only to the built-in relay (window.Relay) and the
 * built-in Blossom server. Crypto runs in the worker (local key) or the NIP-07 extension. */
(function(){
  const NT = () => window.NostrTools;
  const $ = (s,r=document)=>r.querySelector(s);
  const $$ = (s,r=document)=>[...r.querySelectorAll(s)];
  const enc = s => (s==null?'':String(s)).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const LOGO = '/static/posterchan-relay.png';

  let CFG = {}, ME = null, FOLLOWS = new Set(), MUTED = new Set(), VIEW = 'home', IS_ADMIN = false;
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
    return {  // local key — crypto in the worker
      mode, pubkey,
      signEvent: (tpl) => Relay.worker.call('sign', { event: tpl }),
      nip04enc: (peer, txt) => Relay.worker.call('nip04enc', { peer, text: txt }).then(r=>r.ct),
      nip04dec: (peer, ct) => Relay.worker.call('nip04dec', { peer, ct }).then(r=>r.pt),
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

  // ---------- boot ----------
  async function boot(){
    CFG = await fetch('/client/config').then(r=>r.json()).catch(()=>({}));
    await Store.init();
    if ('serviceWorker' in navigator) navigator.serviceWorker.register('/client/sw.js',{scope:'/client'}).catch(()=>{});
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
    // publish initial profile if a name was given
    const nm = $('#signup-name').value.trim();
    startApp();
    if (nm){ try { await publish(0, JSON.stringify({ name: nm }), []); } catch(_){}}
  }

  // ---------- app start ----------
  function startApp(){
    IS_ADMIN = Array.isArray(CFG.admin_npubs) && CFG.admin_npubs.includes(ME.npub);
    try{ if(window.Notification && Notification.permission==='default') Notification.requestPermission(); }catch(_){}
    $('#auth-gate').classList.add('hidden'); $('#app').classList.remove('hidden');
    $('#btn-logout').onclick = logout;
    $$('.nav-item[data-view]').forEach(b=> b.onclick = ()=>switchView(b.dataset.view));
    $('#btn-compose').onclick = ()=>compose(); $('#btn-compose-m').onclick = ()=>compose();
    $('#btn-refresh').onclick = ()=>renderView(true);
    bindSearch();
    bindFeedActions();
    // Run initial queries only once the relay socket is open (otherwise the REQs are dropped
    // and profiles/follows never resolve — names would show as raw npubs).
    Relay.onReady = ()=>{ fetchFollows(); fetchMutes(); fetchMyProfile(); watchNotifications(); watchDMs(); };
    Relay.connect(CFG.relay_url);
    renderMe();
    switchView('home');
  }
  function logout(){ Session.clear(); Relay.worker.call('clearKey',{}); location.reload(); }

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
  const _profQ = new Set(); let _profT = null;
  function needProfile(pk){ if(pk && !Store.haveProfile(pk)){ _profQ.add(pk); if(!_profT) _profT=setTimeout(flushProfiles,120);} }
  async function flushProfiles(){
    _profT=null; const pks=[..._profQ]; _profQ.clear(); if(!pks.length) return;
    const evs = await Relay.query([{ authors:pks, kinds:[0], limit:pks.length }]);
    let changed=false; for(const e of evs){ Store.saveProfile(e); changed=true; }
    if(changed){ renderMe(); decorateProfiles(); }
  }
  async function fetchMyProfile(){ const e=await Relay.query([{authors:[ME.pubkey],kinds:[0],limit:1}]); if(e.length){Store.saveProfile(e.sort((a,b)=>b.created_at-a.created_at)[0]); renderMe();} }
  function decorateProfiles(){
    $$('.note[data-pk]').forEach(n=>{ const p=Store.profile(n.dataset.pk); if(p){
      const a=n.querySelector('.av'); if(p.picture && a) a.src=p.picture;
      const nm=n.querySelector('.name'); if(nm) nm.textContent=p.name||p.display_name||nm.textContent;
      const h=n.querySelector('.handle'); const nip=niceNip05(p.nip05); if(h && nip) h.textContent=nip;
    }});
  }

  // ---------- view routing ----------
  function switchView(v){
    VIEW = v;
    $$('.nav-item[data-view]').forEach(b=> b.classList.toggle('active', b.dataset.view===v));
    $('#view-title').textContent = { home:'Home', global:'Global', notifications:'Notifications', messages:'Messages', blossom:'Files', profile:'Profile' }[v]||v;
    renderView(true);
  }
  function renderView(reset){
    const feed = $('#feed');
    if (reset) feed.innerHTML = '<div class="spinner"></div>';
    if (VIEW==='home' || VIEW==='global') return renderTimeline(VIEW, reset);
    if (VIEW==='notifications') return renderNotifications();
    if (VIEW==='messages') return renderMessages();
    if (VIEW==='blossom') return renderBlossom();
    if (VIEW==='profile') return renderProfile(ME.pubkey);
  }

  // ---------- timeline ----------
  function timelineFilter(){
    if (VIEW==='home') return [{ kinds:[1,6], authors:[...FOLLOWS], limit:80 }];
    return [{ kinds:[1,6], limit:120 }];
  }
  function renderTimeline(view, reset){
    const feed = $('#feed');
    const fn = view==='home' ? (ev=>FOLLOWS.has(ev.pubkey)) : null;
    const draw = ()=>{
      const notes = Store.feed(e=>(!fn||fn(e))&&!MUTED.has(e.pubkey)).filter(e=>!isReply(e)).slice(0,200);
      feed.innerHTML = notes.length ? notes.map(noteHtml).join('') : `<div class="empty">No posts yet. ${view==='home'?'Follow people or check Global.':''}</div>`;
      hydrate(feed);
    };
    draw();
    if (subs[view]) Relay.close(subs[view]);
    subs[view] = Relay.subscribe(timelineFilter(), {
      onEvent: ev => { if (Store.saveEvent(ev)){ invalidateCounts(); needProfile(ev.pubkey); if (VIEW===view && (ev.kind===1||ev.kind===6)) prependNote(ev, fn); } },
      onEose: ()=>{ if(VIEW===view) draw(); }
    });
  }
  function isReply(ev){ return ev.kind===1 && ev.tags.some(t=>t[0]==='e'); }
  function prependNote(ev, fn){
    if (ev.kind===1 && isReply(ev)) return;
    if (MUTED.has(ev.pubkey)) return;
    if (fn && !fn(ev)) return;
    const feed=$('#feed'); const sp=feed.querySelector('.spinner'); if(sp)sp.remove(); const em=feed.querySelector('.empty'); if(em)em.remove();
    const div=document.createElement('div'); div.innerHTML=noteHtml(ev); const node=div.firstElementChild;
    if(node){ feed.insertBefore(node, feed.firstChild); hydrate(node.parentElement); }
  }

  // ---------- note rendering ----------
  function profOf(pk){ return Store.profile(pk)||{}; }
  function noteHtml(ev){
    if (ev.kind===6){  // repost
      let inner=null; try{ inner=JSON.parse(ev.content); }catch(_){}
      const orig = inner || Store.get((ev.tags.find(t=>t[0]==='e')||[])[1]);
      const rp = profOf(ev.pubkey);
      const body = orig ? noteCard(orig, `<div class="repost-tag">↻ ${enc(rp.name||'someone')} reposted</div>`) :
        `<div class="repost-tag">↻ reposted</div><div class="muted small">original not loaded</div>`;
      if (orig) needProfile(orig.pubkey);
      return body;
    }
    return noteCard(ev);
  }
  function noteCard(ev, prefix=''){
    const p = profOf(ev.pubkey); needProfile(ev.pubkey);
    const name = p.name||p.display_name||(NT().nip19.npubEncode(ev.pubkey).slice(0,12)+'…');
    const av = p.picture || LOGO;
    const handle = niceNip05(p.nip05) || ('@'+NT().nip19.npubEncode(ev.pubkey).slice(4,12));
    const counts = countsFor(ev.id);
    const liked = myReaction(ev.id);
    const mine = ev.pubkey===ME.pubkey;
    return `<article class="note" data-id="${ev.id}" data-pk="${ev.pubkey}">
      <img class="av" src="${enc(av)}" onerror="this.src='${LOGO}'">
      <div class="body">${prefix}
        <div class="hd"><span class="name" data-prof="${ev.pubkey}">${enc(name)}</span>
          <span class="handle">${enc(handle)}</span><span class="time">${timeAgo(ev.created_at)}</span></div>
        <div class="txt">${linkify(ev.content)}</div>
        ${quoteHtml(ev)}
        <div class="acts">
          <button class="act" data-a="reply" title="reply">💬 <span class="n">${counts.replies||''}</span></button>
          <button class="act rt ${counts.iRt?'on':''}" data-a="repost" title="repost">🔁 <span class="n">${counts.reposts||''}</span></button>
          <button class="act" data-a="quote" title="quote post">🗨️</button>
          <button class="act ${liked?'on':''}" data-a="like" title="like">${liked||'🤍'} <span class="n">${counts.reactions||''}</span></button>
          <button class="act" data-a="react" title="react">😀</button>
          ${mine?`<button class="act" data-a="delete" title="delete">🗑️</button>`:''}
          ${(IS_ADMIN && !mine)?`<button class="act" data-a="block" title="block author on relay">🚫</button>`:''}
        </div>
      </div></article>`;
  }
  function quoteHtml(ev){
    const q=(ev.tags.find(t=>t[0]==='q')||[])[1]; if(!q) return '';
    const o=Store.get(q); if(!o){ needEvent(q); return `<div class="quoted muted small">quoted post loading…</div>`; }
    const p=profOf(o.pubkey); needProfile(o.pubkey);
    return `<div class="quoted" data-open="${o.id}"><div class="hd"><b>${enc(p.name||'anon')}</b> <span class="handle">${timeAgo(o.created_at)}</span></div><div class="txt">${linkify(o.content)}</div></div>`;
  }
  const _evQ=new Set(); let _evT=null;
  function needEvent(id){ if(id&&!Store.get(id)){ _evQ.add(id); if(!_evT)_evT=setTimeout(flushEvents,150);} }
  async function flushEvents(){ _evT=null; const ids=[..._evQ]; _evQ.clear(); if(!ids.length)return; const evs=await Relay.query([{ids}]); for(const e of evs){Store.saveEvent(e); needProfile(e.pubkey);} if(VIEW==='home'||VIEW==='global') decorateProfiles(); }

  // reaction/repost counts — built ONCE per render pass (single scan of the store) instead of
  // re-scanning the whole store for every rendered note (was O(notes × store)).
  let CIDX = null;
  function invalidateCounts(){ CIDX = null; }
  function buildCounts(){
    const c = { replies:{}, reactions:{}, reposts:{}, myRt:new Set(), myReact:{} };
    const lastE = e => { for(let i=e.tags.length-1;i>=0;i--) if(e.tags[i][0]==='e') return e.tags[i][1]; return null; };
    for(const e of Store.all()){
      const id = lastE(e); if(!id) continue;
      if(e.kind===1) c.replies[id]=(c.replies[id]||0)+1;
      else if(e.kind===7){ c.reactions[id]=(c.reactions[id]||0)+1; if(e.pubkey===ME.pubkey) c.myReact[id]=(e.content==='+'||e.content===''?'❤️':e.content); }
      else if(e.kind===6){ c.reposts[id]=(c.reposts[id]||0)+1; if(e.pubkey===ME.pubkey) c.myRt.add(id); }
    }
    CIDX = c;
  }
  function countsFor(id){ if(!CIDX) buildCounts(); return { replies:CIDX.replies[id]||0, reactions:CIDX.reactions[id]||0, reposts:CIDX.reposts[id]||0, iRt:CIDX.myRt.has(id) }; }
  function myReaction(id){ if(!CIDX) buildCounts(); return CIDX.myReact[id]||null; }
  // (reaction display: '+' shows as ❤️, custom emoji shown as-is — see buildCounts/doReact)

  // ---------- interactions ----------
  function bindFeedActions(){
    $('#feed').addEventListener('click', async (e)=>{
      const mn=e.target.closest('.mention'); if(mn){ e.preventDefault(); const pk=safePk(mn.dataset.np); if(pk) renderProfileView(pk); return; }
      const av=e.target.closest('.av'); if(av){ const n=e.target.closest('.note'); if(n){ renderProfileView(n.dataset.pk); return; } }
      const prof=e.target.closest('[data-prof]'); if(prof){ renderProfileView(prof.dataset.prof); return; }
      const q=e.target.closest('[data-open]'); if(q){ openThread(q.dataset.open); return; }
      const btn=e.target.closest('.act'); if(!btn) return;
      const art=e.target.closest('.note'); const id=art.dataset.id; const pk=art.dataset.pk;
      const a=btn.dataset.a;
      if(a==='like') return doReact(id,pk,'+',btn);
      if(a==='react') return pickEmoji(id,pk,btn);
      if(a==='repost') return doRepost(id,pk,btn);
      if(a==='quote') return compose({quote:id});
      if(a==='reply') return compose({reply:id, replyPk:pk});
      if(a==='delete') return doDelete(id,art);
      if(a==='block') return doBlock(pk);
    });
  }
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
  async function doReact(id,pk,emoji,btn){
    if(myReaction(id)){ toast('already reacted'); return; }
    await publish(7, emoji, eTags(id,pk));
    btn.classList.add('on'); btn.firstChild.textContent = (emoji==='+'?'❤️':emoji)+' ';
    const n=btn.querySelector('.n'); n.textContent=(parseInt(n.textContent||'0')+1);
  }
  function pickEmoji(id,pk){
    const emojis=['❤️','🔥','😂','😮','😢','👍','🤙','💀','⚡','🚀'];
    modal(`<h3>react</h3><div class="emoji-pick">${emojis.map(x=>`<button data-e="${x}">${x}</button>`).join('')}</div>`, root=>{
      $$('[data-e]',root).forEach(b=> b.onclick=async()=>{ closeModal(); await publish(7,b.dataset.e,eTags(id,pk)); toast('reacted '+b.dataset.e); renderView(); });
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
  function compose({reply=null, replyPk=null, quote=null}={}){
    const title = reply?'Reply':quote?'Quote post':'New post';
    let qhtml=''; if(quote){ const o=Store.get(quote); if(o) qhtml=`<div class="quoted"><b>${enc((profOf(o.pubkey).name)||'anon')}</b><div class="txt">${linkify(o.content)}</div></div>`; }
    modal(`<h3>${title}</h3>${qhtml}<textarea id="cmp" placeholder="what's happening on the net?"></textarea>
      <div class="row"><button class="mini" id="cmp-img">📎 attach</button><input type="file" id="cmp-file" multiple hidden>
      <span class="spacer"></span><button class="btn btn-neon" id="cmp-send">Post ▶</button></div>
      <div class="muted small" id="cmp-status"></div>`, root=>{
      const ta=$('#cmp',root); attachMentionAutocomplete(ta);
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
      $('#cmp-file',root).onchange=async e=>{ const files=[...e.target.files]; if(!files.length)return;
        for(let i=0;i<files.length;i++){ $('#cmp-status',root).textContent=`uploading ${i+1}/${files.length}…`;
          try{ const url=await uploadBlob(files[i]); ta.value+=(ta.value?'\n':'')+url; }
          catch(err){ $('#cmp-status',root).textContent='upload failed: '+err.message; return; } }
        $('#cmp-status',root).textContent=''; e.target.value=''; };
      $('#cmp-send',root).onclick=async()=>{
        const text=ta.value.trim(); if(!text)return;
        let tags=[];
        if(reply){ const o=Store.get(reply); tags=replyTags(o, reply, replyPk); }
        if(quote){ tags.push(['q',quote]); const o=Store.get(quote); if(o)tags.push(['p',o.pubkey]); }
        mentionTags(text).forEach(t=>{ if(!tags.some(x=>x[0]==='p'&&x[1]===t[1])) tags.push(t); });
        closeModal(); await publish(1, text, tags); toast('posted'); if(VIEW==='home'||VIEW==='global') renderView(true);
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

  // @-autocomplete: type "@name" and pick a known profile -> inserts a nostr:npub mention
  function attachMentionAutocomplete(ta){
    let box=null; const close=()=>{ if(box){box.remove();box=null;} };
    ta.addEventListener('input', ()=>{
      const pos=ta.selectionStart, left=ta.value.slice(0,pos), m=left.match(/@([a-z0-9_]{1,30})$/i);
      if(!m){ close(); return; }
      const q=m[1].toLowerCase();
      const matches=Store.profileList().filter(p=>(((p.meta.name||'')+(p.meta.display_name||'')+(p.meta.nip05||'')).toLowerCase().includes(q))).slice(0,6);
      if(!matches.length){ close(); return; }
      close(); box=document.createElement('div'); box.className='mention-box';
      box.innerHTML=matches.map(p=>`<div class="mention-opt" data-pk="${p.pubkey}"><img src="${enc(p.meta.picture||LOGO)}" onerror="this.src='${LOGO}'"><span><b>${enc(p.meta.name||p.meta.display_name||'anon')}</b> <span class="muted small">${enc(niceNip05(p.meta.nip05)||'')}</span></span></div>`).join('');
      ta.insertAdjacentElement('afterend', box);
      box.querySelectorAll('[data-pk]').forEach(el=> el.onmousedown=ev=>{ ev.preventDefault();
        const np=NT().nip19.npubEncode(el.dataset.pk);
        const newLeft=left.replace(/@[a-z0-9_]{1,30}$/i,'nostr:'+np+' ');
        ta.value=newLeft+ta.value.slice(pos); ta.focus(); ta.selectionStart=ta.selectionEnd=newLeft.length; close(); });
    });
    ta.addEventListener('blur', ()=>setTimeout(close,200));
  }
  function mentionTags(text){
    const out=[];
    for(const m of text.matchAll(/nostr:(npub1[0-9a-z]+)/gi)){ const pk=safePk(m[1]); if(pk && !out.some(t=>t[1]===pk)) out.push(['p',pk]); }
    return out;
  }

  // ---------- Blossom uploads + file browser ----------
  function mediaServer(){ return (ClientSettings.get('mediaServer') || CFG.blossom_url || '').replace(/\/$/,''); }
  async function sha256hex(buf){ const h=await crypto.subtle.digest('SHA-256', buf); return [...new Uint8Array(h)].map(b=>b.toString(16).padStart(2,'0')).join(''); }
  const _MIME_EXT={'image/jpeg':'jpg','image/png':'png','image/gif':'gif','image/webp':'webp','image/avif':'avif',
    'video/mp4':'mp4','video/webm':'webm','video/quicktime':'mov','audio/mpeg':'mp3','audio/ogg':'ogg','audio/wav':'wav','audio/mp4':'m4a','audio/aac':'aac','audio/flac':'flac'};
  function extFor(file){ const n=(file.name||'').match(/\.([a-z0-9]{2,5})$/i); if(n) return n[1].toLowerCase(); return _MIME_EXT[file.type]||''; }
  async function uploadBlob(file){
    const server=mediaServer(); if(!server) throw new Error('no media server set');
    const buf=await file.arrayBuffer(); const hash=await sha256hex(buf);
    const auth=await sign(24242,'Upload blob',[['t','upload'],['x',hash],['expiration',String(Math.floor(Date.now()/1000)+3600)]]);
    const res=await fetch(server+'/upload',{ method:'PUT', headers:{ 'Authorization':'Nostr '+btoa(JSON.stringify(auth)), 'Content-Type':file.type||'application/octet-stream' }, body:buf });
    if(!res.ok){ const t=await res.text().catch(()=>res.status); throw new Error(res.headers.get('x-reason')||t); }
    const d=await res.json();
    // Our Blossom URLs are extensionless (/<sha256>); append the file extension so clients (incl.
    // linkify below) can detect the media type and embed/play it. The server ignores the suffix.
    const ext=extFor(file); return (d.url||server+'/'+hash) + (ext?('.'+ext):'');
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
      const isVid=/video/.test(b.type||''); const isAud=/audio/.test(b.type||'');
      const media=isVid?`<video src="${enc(b.url)}" muted></video>`:isAud?`<div style="display:grid;place-items:center;height:140px">🎵</div>`:`<img src="${enc(b.url)}" loading="lazy">`;
      return `<div class="file-card" data-sha="${b.sha256}"><a href="${enc(b.url)}" target="_blank">${media}</a><button class="del" data-sha="${b.sha256}">✕</button><div class="meta"><span>${((b.size||0)/1024|0)}KB</span><span>${(b.type||'').split('/')[1]||''}</span></div></div>`;
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
    Relay.subscribe([{ '#p':[ME.pubkey], kinds:[1,6,7], limit:60 }], {
      onEvent: ev => { if(ev.pubkey===ME.pubkey) return; if(Store.saveEvent(ev)){ invalidateCounts(); needProfile(ev.pubkey);
        if(ev.created_at>seenNotif.last){ bumpNotif(); if(_notifReady) notifPing(ev); }
        if(VIEW==='notifications') renderNotifications(); } },
      onEose: ()=>{ _notifReady=true; if(VIEW==='notifications') renderNotifications(); }   // backlog done — only ping LIVE ones
    });
  }
  function notifPing(ev){
    const p=profOf(ev.pubkey); const who=p.name||p.display_name||'someone';
    const what = ev.kind===7?`reacted ${ev.content==='+'||ev.content===''?'❤️':enc(ev.content)}`
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
  function notifList(){ return Store.all().filter(e=>[1,6,7].includes(e.kind) && e.pubkey!==ME.pubkey && !MUTED.has(e.pubkey) && e.tags.some(t=>t[0]==='p'&&t[1]===ME.pubkey)).sort((a,b)=>b.created_at-a.created_at).slice(0,100); }
  function bumpNotif(){ const n=notifList().filter(e=>e.created_at>seenNotif.last).length; $$('#notif-badge,#notif-badge-m').forEach(b=>{ if(n){b.textContent=n>99?'99+':n;b.classList.remove('hidden');}else b.classList.add('hidden');}); }
  function renderNotifications(){
    const list=notifList(); const feed=$('#feed');
    feed.innerHTML = list.length ? list.map(notifHtml).join('') : '<div class="empty">No notifications.</div>';
    list.forEach(e=>needProfile(e.pubkey));
    seenNotif.last = Math.floor(Date.now()/1000); localStorage.setItem('pc_notif_seen', seenNotif.last);
    $$('#notif-badge,#notif-badge-m').forEach(b=>b.classList.add('hidden'));
    feed.querySelectorAll('[data-open]').forEach(n=> n.onclick=()=>openThread(n.dataset.open));
  }
  function notifHtml(e){
    const p=profOf(e.pubkey); const av=p.picture||LOGO; const tgt=(e.tags.filter(t=>t[0]==='e').pop()||[])[1]||'';
    let cls,ic,txt;
    if(e.kind===7){cls='like';ic='♥';txt=`reacted ${enc(e.content==='+'?'❤️':e.content)} to your post`;}
    else if(e.kind===6){cls='rt';ic='↻';txt='reposted your note';}
    else if(isReply(e)){cls='reply';ic='💬';txt='replied: '+enc((e.content||'').slice(0,80));}
    else {cls='mention';ic='@';txt='mentioned you: '+enc((e.content||'').slice(0,80));}
    return `<div class="notif ${cls}" data-open="${tgt}"><span class="ic">${ic}</span><img src="${enc(av)}" onerror="this.src='${LOGO}'"><div><b>${enc(p.name||p.display_name||'anon')}</b> ${txt}<div class="muted small">${timeAgo(e.created_at)}</div></div></div>`;
  }

  // ---------- DMs (NIP-04) ----------
  const dmPeers = new Map();  // peer -> [{ev, text}]
  let dmActive = null;
  function watchDMs(){
    Relay.subscribe([{ kinds:[4], '#p':[ME.pubkey], limit:200 }, { kinds:[4], authors:[ME.pubkey], limit:200 }], {
      onEvent: ev => { if(Store.saveEvent(ev)){ ingestDM(ev); if(VIEW==='messages') renderMessages(); } },
      onEose: ()=>{ if(VIEW==='messages') renderMessages(); }
    });
  }
  async function ingestDM(ev){
    const mine = ev.pubkey===ME.pubkey;
    const peer = mine ? (ev.tags.find(t=>t[0]==='p')||[])[1] : ev.pubkey;
    if(!peer) return; needProfile(peer);
    if(!dmPeers.has(peer)) dmPeers.set(peer, []);
    const arr=dmPeers.get(peer); if(arr.find(m=>m.id===ev.id)) return;
    let text=''; try{ text=await signer.nip04dec(peer, ev.content); }catch(_){ text='[encrypted]'; }
    arr.push({ id:ev.id, mine, text, t:ev.created_at }); arr.sort((a,b)=>a.t-b.t);
    if(VIEW==='messages' && dmActive===peer) renderDmThread(peer);
  }
  function renderMessages(){
    const feed=$('#feed');
    feed.innerHTML=`<div class="dm-wrap"><div class="dm-list" id="dm-list"></div><div class="dm-thread" id="dm-thread"><div class="empty">Select a conversation, or start one.</div></div></div>`;
    const list=$('#dm-list');
    const peers=[...dmPeers.keys()].sort((a,b)=>{ const la=dmPeers.get(a).slice(-1)[0]||{}, lb=dmPeers.get(b).slice(-1)[0]||{}; return (lb.t||0)-(la.t||0); });
    list.innerHTML = `<div class="dm-peer" id="dm-new"><span class="ic">＋</span><b>New message</b></div>` + peers.map(pk=>{
      const p=profOf(pk); const last=dmPeers.get(pk).slice(-1)[0]||{};
      return `<div class="dm-peer" data-peer="${pk}"><img src="${enc(p.picture||LOGO)}" onerror="this.src='${LOGO}'"><div><b>${enc(p.name||NT().nip19.npubEncode(pk).slice(0,12))}</b><div class="muted small">${enc((last.text||'').slice(0,28))}</div></div></div>`;
    }).join('');
    $('#dm-new').onclick=()=>{ const v=prompt('Recipient npub:'); if(!v)return; const pk=NT().nip19.npubEncode?safePk(v):null; if(pk){ if(!dmPeers.has(pk))dmPeers.set(pk,[]); needProfile(pk); renderMessages(); openDm(pk);} else toast('invalid npub'); };
    $$('[data-peer]',list).forEach(el=> el.onclick=()=>openDm(el.dataset.peer));
    if(dmActive && dmPeers.has(dmActive)) renderDmThread(dmActive);
  }
  function safePk(v){ try{ if(v.startsWith('npub')){const d=NT().nip19.decode(v); return d.data;} if(/^[0-9a-f]{64}$/i.test(v))return v.toLowerCase(); }catch(_){} return null; }
  function openDm(pk){ dmActive=pk; $$('.dm-peer').forEach(e=>e.classList.toggle('active',e.dataset.peer===pk)); $('#dm-list').classList.add('has-active'); renderDmThread(pk); }
  function renderDmThread(pk){
    const wrap=$('#dm-thread'); if(!wrap)return; const p=profOf(pk); const msgs=dmPeers.get(pk)||[];
    wrap.innerHTML=`<div class="topbar"><button class="mini" id="dm-back">←</button> <b>${enc(p.name||NT().nip19.npubEncode(pk).slice(0,14))}</b></div>
      <div class="dm-msgs" id="dm-msgs">${msgs.map(m=>`<div class="bubble ${m.mine?'me':'them'}">${linkify(m.text)}</div>`).join('')}</div>
      <div class="dm-compose"><input class="input" id="dm-in" placeholder="encrypted message…"><button class="btn btn-neon" id="dm-send">▶</button></div>`;
    $('#dm-back').onclick=()=>{ $('#dm-list').classList.remove('has-active'); dmActive=null; };
    const send=async()=>{ const inp=$('#dm-in'); const t=inp.value.trim(); if(!t)return; inp.value='';
      try{ const ct=await signer.nip04enc(pk,t); await publish(4, ct, [['p',pk]]); }catch(e){ toast('dm failed: '+e.message);} };
    $('#dm-send').onclick=send; $('#dm-in').onkeydown=e=>{ if(e.key==='Enter')send(); };
    const m=$('#dm-msgs'); if(m)m.scrollTop=m.scrollHeight;
  }

  // ---------- profile ----------
  function renderProfile(pk){ renderProfileView(pk); }
  async function renderProfileView(pk){
    if(VIEW!=='profile'){ VIEW='profile'; $$('.nav-item[data-view]').forEach(b=>b.classList.toggle('active',b.dataset.view==='profile')); $('#view-title').textContent='Profile'; }
    const feed=$('#feed'); feed.innerHTML='<div class="spinner"></div>';
    if(!Store.haveProfile(pk)){ const e=await Relay.query([{authors:[pk],kinds:[0],limit:1}]); for(const x of e)Store.saveProfile(x); }
    const p=Store.profile(pk)||{}; const mine=pk===ME.pubkey;
    const notes=await Relay.query([{authors:[pk],kinds:[1],limit:40}]); notes.forEach(n=>Store.saveEvent(n));
    feed.innerHTML=`<div class="prof"><div class="banner" style="${p.banner?`background-image:url('${enc(p.banner)}');background-size:cover`:''}"></div>
      <div class="phead"><img class="pav" src="${enc(p.picture||LOGO)}" onerror="this.src='${LOGO}'">
        <div style="flex:1"></div>${mine?`<button class="btn btn-cyan small" id="edit-prof">Edit</button> <button class="btn btn-ghost small" id="open-settings">⚙ Settings</button>`:`
          <button class="btn ${FOLLOWS.has(pk)?'btn-ghost':'btn-neon'} small" id="follow-prof">${FOLLOWS.has(pk)?'Following ✓':'Follow'}</button>
          <button class="btn btn-ghost small" id="mute-prof">${MUTED.has(pk)?'Unmute':'Mute'}</button>
          <button class="btn btn-ghost small" id="dm-prof">Message</button>
          ${IS_ADMIN?`<button class="btn btn-ghost small" id="block-prof" style="color:#ff6b8b">Block</button>`:''}`}</div>
      <div class="pbody"><h2>${enc(p.name||p.display_name||'anon')}</h2><div class="muted small">${enc(NT().nip19.npubEncode(pk))}</div>
        <div class="about">${linkify(p.about||'')}</div></div></div>
      <div id="prof-notes">${Store.feed(e=>e.pubkey===pk && !isReply(e)).slice(0,40).map(noteCard).join('')||'<div class="empty">No posts.</div>'}</div>`;
    hydrate(feed);
    if(mine){ $('#edit-prof').onclick=()=>editProfile(p); $('#open-settings').onclick=openSettings; }
    else {
      const d=$('#dm-prof'); if(d)d.onclick=()=>{ switchView('messages'); setTimeout(()=>{ if(!dmPeers.has(pk))dmPeers.set(pk,[]); openDm(pk); },50); };
      const f=$('#follow-prof'); if(f)f.onclick=async()=>{ await toggleFollow(pk); renderProfileView(pk); };
      const m=$('#mute-prof'); if(m)m.onclick=async()=>{ await toggleMute(pk); renderProfileView(pk); };
      const b=$('#block-prof'); if(b)b.onclick=()=>doBlock(pk);
    }
  }
  function editProfile(p){
    modal(`<h3>Edit profile</h3>
      <input class="input" id="pf-name" placeholder="name" value="${enc(p.name||p.display_name||'')}">
      <input class="input" id="pf-pic" placeholder="picture url" value="${enc(p.picture||'')}">
      <input class="input" id="pf-banner" placeholder="banner url" value="${enc(p.banner||'')}">
      <textarea id="pf-about" placeholder="about">${enc(p.about||'')}</textarea>
      <div class="row"><button class="mini" id="pf-up">🖼 upload pic</button><input type="file" id="pf-file" accept="image/*" hidden><span class="spacer"></span><button class="btn btn-neon" id="pf-save">Save</button></div>`, root=>{
      $('#pf-up',root).onclick=()=>$('#pf-file',root).click();
      $('#pf-file',root).onchange=async e=>{ const f=e.target.files[0]; if(!f)return; try{ $('#pf-pic',root).value=await uploadBlob(f); toast('uploaded'); }catch(err){toast('upload failed');} };
      $('#pf-save',root).onclick=async()=>{ const meta={ ...p, name:$('#pf-name',root).value.trim(), picture:$('#pf-pic',root).value.trim(), banner:$('#pf-banner',root).value.trim(), about:$('#pf-about',root).value.trim() };
        closeModal(); await publish(0, JSON.stringify(meta), []); Store.saveProfile({pubkey:ME.pubkey,created_at:Math.floor(Date.now()/1000),content:JSON.stringify(meta)}); toast('profile saved'); renderMe(); renderProfileView(ME.pubkey); };
    });
  }
  function openSettings(){
    modal(`<h3>⚙ Settings</h3>
      <label class="muted small">Media upload server (Blossom)</label>
      <input class="input" id="set-media" placeholder="${enc(CFG.blossom_url||'')}" value="${enc(ClientSettings.get('mediaServer',''))}">
      <div class="muted small">Leave blank to use the built-in server (${enc(CFG.blossom_url||'none')}).</div>
      <button class="btn btn-neon full" id="set-save">Save</button>`, root=>{
      $('#set-save',root).onclick=()=>{ ClientSettings.set('mediaServer', $('#set-media',root).value.trim()); closeModal(); toast('settings saved'); };
    });
  }
  function openThread(id){ needEvent(id); const o=Store.get(id); if(o) renderProfileView(o.pubkey); }  // light: jump to author

  // ---------- search (NIP-50 posts + profile lookup) ----------
  function bindSearch(){
    const inp=$('#search-input'); if(!inp) return; let t=null;
    inp.addEventListener('input', ()=>{ clearTimeout(t); const q=inp.value.trim(); if(q.length<2) return; t=setTimeout(()=>runSearch(q),350); });
    inp.addEventListener('keydown', e=>{ if(e.key==='Enter'){ const q=inp.value.trim(); if(q) runSearch(q); } });
  }
  async function nip05Resolve(addr){
    let [name, domain] = addr.split('@'); if(!domain){ domain=name; name='_'; }
    try {
      const j = await fetch(`https://${domain}/.well-known/nostr.json?name=${encodeURIComponent(name)}`).then(r=>r.json());
      return (j && j.names && j.names[name]) || null;
    } catch(_){ return null; }
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
    html+= posts.length ? posts.map(noteCard).join('') : '<div class="empty">No matching posts.</div>';
    feed.innerHTML=html; hydrate(feed);
    $$('[data-prof]',feed).forEach(el=> el.onclick=()=>renderProfileView(el.dataset.prof));
  }

  // ---------- helpers ----------
  function hydrate(scope){ decorateProfiles(); }
  function timeAgo(ts){ const s=Math.floor(Date.now()/1000)-ts; if(s<60)return s+'s'; if(s<3600)return (s/60|0)+'m'; if(s<86400)return (s/3600|0)+'h'; return (s/86400|0)+'d'; }
  function linkify(txt){
    let h=enc(txt);
    // images / video / audio embed (extension may be followed by ?query or #frag); else link.
    h=h.replace(/(https?:\/\/[^\s<]+)/g, url=>{
      const u=url.replace(/[)\].,!?]+$/,'');          // don't swallow trailing punctuation
      const tail=url.slice(u.length);
      let tag;
      if(/\.(jpe?g|png|gif|webp|avif)(\?|#|$)/i.test(u)) tag=`<br><img src="${u}" loading="lazy">`;
      else if(/\.(mp4|webm|mov|m4v)(\?|#|$)/i.test(u)) tag=`<br><video src="${u}" controls preload="metadata" playsinline></video>`;
      else if(/\.(mp3|ogg|wav|m4a|aac|flac)(\?|#|$)/i.test(u)) tag=`<br><audio src="${u}" controls preload="none"></audio>`;
      else tag=`<a href="${u}" target="_blank" rel="noopener">${u}</a>`;
      return tag+tail;
    });
    // nostr:npub… mentions -> short handle link
    h=h.replace(/nostr:(npub1[0-9a-z]+)/gi, (m,np)=>`<a href="#" class="mention" data-np="${np}">@${np.slice(0,12)}…</a>`);
    return h;
  }

  // ---------- modal + toast ----------
  function modal(html, onMount){ const bg=document.createElement('div'); bg.className='modal-bg'; bg.innerHTML=`<div class="modal glass neon-border">${html}</div>`; bg.onclick=e=>{ if(e.target===bg) closeModal(); }; $('#modal-root').appendChild(bg); if(onMount)onMount(bg.querySelector('.modal')); }
  function closeModal(){ $('#modal-root').innerHTML=''; }
  function toast(m){ const t=document.createElement('div'); t.className='toast'; t.textContent=m; $('#toast-root').appendChild(t); setTimeout(()=>t.remove(),3200); }

  document.addEventListener('DOMContentLoaded', boot);
})();
