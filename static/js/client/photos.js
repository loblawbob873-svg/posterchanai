/* Photos -- a Nostr photo app: NIP-68 PICTURE POSTS (kind 20), paired with the profile's ALBUMS (albums.js).
 *
 * Asked 2026-10-28 ("Nostr Photos ... should probably pair with that photo album NIP we added to user profiles").
 * One screen, three tabs: the pictures of the people you follow, everybody's (Global), and your Albums -- the SAME
 * albums.js the profile's Albums tab mounts, so an album made here is the album on your profile, in Olas, and in
 * every other NIP-51 client. Any picture can be SAVED into one of your albums (a picture set references posts by
 * id, so a friend's photo belongs in your "Trip" album without copying it), and "Post a photo" publishes one kind-20
 * post carrying every chosen picture (NIP-68 allows several imeta) and can drop it straight into an album.
 *
 * State lives in the MODULE, not the DOM: #feed is shared by every view and blanked on entry, so leaving and
 * coming back repaints the same tab, the same pictures and the same scroll with no refetch. The grid paints from
 * the Store BEFORE the first network await (CLAUDE.md: paint what the client already holds), and a read no relay
 * finished is "could not reach the relays", never "no photos". Album writes go through albums.js's editAlbum --
 * the one read-modify-write of a replaceable set with the guards that keep it from dropping photos.
 */
window.PCPhotosFactory = function(dep){
  const S = dep.state;   // live app.js bindings: ME, FOLLOWS, GUEST, VIEW
  const {
    $$, enc, toast, modal, closeModal, openLightbox, openThread, openProfile, profOf, isMutedView,
    uploadBlob, imetaTagsFor, publish, albums, syncedAlbums, syncedThumb, openSynced, standalone, switchView,
  } = dep;
  const PIC = 20, PAGE = 60, AUTHORS_PER_FILTER = 300;
  /* SYNCED: the folders Folder Sync keeps (a phone's camera roll, say), each directory of pictures an album --
   * "maybe photos app should show the albums in the File Manager Synced Folder, like if user syncs photos back and
   * forth from phone". Private: these never touch a relay as posts; the pictures are decrypted on this device. */
  const TABS = [['following', 'Following'], ['global', 'Global'], ['albums', 'Albums'], ['synced', 'Synced']];

  const st = {
    tab: 'following',
    feeds: { following: newFeed(), global: newFeed() },
    scroll: { following: 0, global: 0, albums: 0, synced: 0 },
    sync: { res: null, loading: false, open: null, shown: 0 },
  };
  const SYNC_PAGE = 120;
  function newFeed(){ return { byId: new Map(), oldest: 0, done: false, loading: false, unreachable: false, who: '' }; }

  // ----------------------------------------------------------------------------------------- pure halves
  const ok = u => /^https?:\/\//i.test(u || '');
  /** Every picture of one post, in order: each imeta url (NIP-68), else the url/image tags. */
  function imagesOf(ev){
    const out = [];
    for(const t of (ev && ev.tags) || []){
      if(t[0] !== 'imeta') continue;
      const u = t.find(p => /^url /.test(p || '')), m = t.find(p => /^m /.test(p || ''));
      if(u && ok(u.slice(4)) && !(m && /^video\//.test(m.slice(2)))) out.push(u.slice(4));
    }
    if(!out.length) for(const t of (ev && ev.tags) || []) if((t[0] === 'url' || t[0] === 'image') && ok(t[1])) out.push(t[1]);
    return [...new Set(out)];
  }
  /** The `t` tags a caption's #hashtags make, lowercased and unique -- how kind-20 posts are found by topic. */
  function hashtags(text){
    const seen = new Set();
    for(const m of String(text || '').matchAll(/(?:^|\s)#([\p{L}\p{N}_]{1,64})/gu)) seen.add(m[1].toLowerCase());
    return [...seen].map(t => ['t', t]);
  }
  /** One kind-20 post's tags for several uploaded pictures: one imeta each, then m/x per picture (deduped). */
  function postTags(pics, caption){
    const tags = [], seen = new Set();
    for(const p of pics){
      let im = (p.extra && p.extra[0] === 'imeta') ? p.extra.slice() : ['imeta', 'url ' + p.url];
      if(p.mime && !im.some(x => /^m /.test(x))) im.push('m ' + p.mime);
      tags.push(im);
    }
    for(const im of tags.slice()){
      for(const [k, name] of [['m ', 'm'], ['x ', 'x']]){
        const v = (im.find(x => x.startsWith(k)) || '').slice(2);
        if(v && !seen.has(name + v)){ seen.add(name + v); tags.push([name, v]); }
      }
    }
    return tags.concat(hashtags(caption));
  }
  /** Newest first; the feed's own order (created_at, then id so two in one second never swap on a repaint). */
  const order = (a, b) => (b.created_at - a.created_at) || (a.id < b.id ? -1 : 1);

  // --------------------------------------------------------------------------------------------- reads
  function authorsFor(tab){
    if(tab !== 'following') return null;
    const me = S.ME && S.ME.pubkey;
    const set = new Set(S.FOLLOWS || []); if(me) set.add(me);
    return [...set];
  }
  function filtersFor(tab, until){
    const base = { kinds: [PIC], limit: PAGE }; if(until) base.until = until;
    const who = authorsFor(tab);
    if(!who) return [base];
    const out = [];
    for(let i = 0; i < who.length; i += AUTHORS_PER_FILTER) out.push(Object.assign({}, base, { authors: who.slice(i, i + AUTHORS_PER_FILTER) }));
    return out;
  }
  function keep(ev){
    if(!ev || ev.kind !== PIC || !imagesOf(ev).length) return false;
    try{ if(isMutedView && isMutedView(ev)) return false; }catch(_){}
    return true;
  }
  function absorb(feed, evs){
    let added = 0;
    for(const ev of evs){
      if(!keep(ev) || feed.byId.has(ev.id)) continue;
      feed.byId.set(ev.id, ev); added++;
      if(!feed.oldest || ev.created_at < feed.oldest) feed.oldest = ev.created_at;
    }
    return added;
  }
  /** What the Store already holds for this tab -- painted before any socket is asked. */
  function fromCache(tab){
    let evs = [];
    try{ evs = (window.Store && Store.byKind) ? Store.byKind(PIC) : []; }catch(_){}
    const who = authorsFor(tab); if(who){ const s = new Set(who); evs = evs.filter(e => s.has(e.pubkey)); }
    return evs;
  }
  async function readPage(tab, more){
    const feed = st.feeds[tab];
    if(feed.loading || (more && feed.done)) return;
    feed.loading = true;
    try{
      try{ if(Relay.ready) await Relay.ready(); }catch(_){}
      let got = [];
      try{ got = await Relay.query(filtersFor(tab, more && feed.oldest ? feed.oldest - 1 : 0)); }catch(_){ got = Object.assign([], { complete: false }); }
      try{ got.forEach(e => Store.saveEvent(e)); }catch(_){}
      const answered = got.complete !== false || got.length > 0;
      feed.unreachable = !answered && !feed.byId.size;
      const added = absorb(feed, got);
      // "Nothing older" only from an answer, and only when it brought nothing new: a relay that never spoke
      // must not end the feed for the rest of the visit.
      if(more && answered && !added) feed.done = true;
    } finally { feed.loading = false; }
  }

  // ------------------------------------------------------------------------------------------------ UI
  const ic = n => `<svg class="ic" aria-hidden="true"><use href="#i-${n}"></use></svg>`;
  function injectCss(){
    if(document.getElementById('pc-photos-css')) return;
    const s = document.createElement('style'); s.id = 'pc-photos-css';
    s.textContent = `
.ph-wrap{padding:12px 16px 32px;max-width:1280px;margin:0 auto;box-sizing:border-box}
.ph-head{display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin-bottom:12px}
.ph-tabs{display:flex;gap:4px;padding:3px;border:1px solid var(--border,rgba(255,255,255,.12));border-radius:12px;background:var(--panel,rgba(255,255,255,.03))}
.ph-tab{appearance:none;border:0;background:none;color:var(--muted,#9aa);font:600 14px/1 Inter,system-ui,sans-serif;padding:9px 14px;border-radius:9px;cursor:pointer;min-height:36px}
.ph-tab:hover{color:var(--text,#eee)}
.ph-tab.on{color:var(--bg,#000);background:var(--neon,#0ff);box-shadow:0 0 12px var(--neon,#0ff)}
.ph-sp{flex:1}
.ph-post{display:inline-flex;align-items:center;gap:6px}
.ph-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(190px,1fr));gap:8px}
.ph-tile{position:relative;aspect-ratio:1/1;padding:0;border:1px solid var(--border,rgba(255,255,255,.1));border-radius:10px;overflow:hidden;background:rgba(127,127,127,.12);cursor:pointer;display:block;width:100%}
.ph-tile img{width:100%;height:100%;object-fit:contain;display:block;transition:transform .25s ease}
.ph-tile:hover,.ph-tile:focus-visible{border-color:var(--neon,#0ff);box-shadow:0 0 0 1px var(--neon,#0ff),0 0 16px color-mix(in srgb,var(--neon,#0ff) 45%,transparent);outline:none}
.ph-tile:hover img{transform:scale(1.04)}
.ph-by{position:absolute;left:0;right:0;bottom:0;display:flex;align-items:center;gap:6px;padding:18px 8px 6px;background:linear-gradient(transparent,rgba(0,0,0,.75));color:#fff;font:500 12px/1.2 Inter,system-ui,sans-serif;text-align:left;opacity:0;transition:opacity .2s}
.ph-tile:hover .ph-by,.ph-tile:focus-visible .ph-by{opacity:1}
.ph-by img{width:20px;height:20px;border-radius:50%;object-fit:cover;flex:none}
.ph-by span{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.ph-n{position:absolute;top:6px;right:6px;background:rgba(0,0,0,.65);color:#fff;border-radius:999px;padding:2px 7px;font:600 11px/1.4 Inter,system-ui,sans-serif;border:1px solid var(--neon,#0ff)}
.ph-more{display:flex;justify-content:center;padding:18px 0}
.ph-empty{padding:40px 12px;text-align:center;color:var(--muted,#9aa)}
.ph-view{display:flex;flex-direction:column;gap:10px;max-width:min(92vw,900px)}
.ph-view-img{position:relative;display:flex;justify-content:center;background:#000;border-radius:10px;overflow:hidden}
.ph-view-img img{max-width:100%;max-height:62vh;object-fit:contain;display:block;cursor:zoom-in}
.ph-view-strip{display:flex;gap:6px;overflow-x:auto}
.ph-view-strip button{flex:none;width:56px;height:56px;padding:0;border:1px solid var(--border,rgba(255,255,255,.15));border-radius:6px;overflow:hidden;background:none;cursor:pointer}
.ph-view-strip button.on{border-color:var(--neon,#0ff);box-shadow:0 0 8px var(--neon,#0ff)}
.ph-view-strip img{width:100%;height:100%;object-fit:contain}
.ph-author{display:flex;align-items:center;gap:8px;background:none;border:0;color:inherit;padding:0;cursor:pointer;font:600 14px Inter,system-ui,sans-serif;text-align:left}
.ph-author img{width:32px;height:32px;border-radius:50%;object-fit:cover}
.ph-caption{white-space:pre-wrap;word-break:break-word;line-height:1.45}
.ph-acts{display:flex;gap:8px;flex-wrap:wrap}
.ph-acts .btn{display:inline-flex;align-items:center;gap:6px}
.ph-pick{display:flex;flex-direction:column;gap:6px;max-height:50vh;overflow:auto;min-width:min(80vw,360px)}
.ph-pick button{justify-content:flex-start;text-align:left}
.ph-compose{display:flex;flex-direction:column;gap:10px;min-width:min(86vw,480px)}
.ph-prev{display:grid;grid-template-columns:repeat(auto-fill,minmax(84px,1fr));gap:6px}
.ph-prev img{width:100%;aspect-ratio:1/1;object-fit:contain;border-radius:8px;border:1px solid var(--border,rgba(255,255,255,.15))}
.ph-compose textarea{min-height:80px;resize:vertical}
.ph-compose-btns{display:flex;justify-content:flex-end;gap:8px}
@media(max-width:640px){
  .ph-wrap{padding:8px 0 24px}
  .ph-head{padding:0 10px}
  .ph-tabs{flex:1}.ph-tab{flex:1}
  .ph-post span{display:none}
  .ph-grid{grid-template-columns:repeat(3,1fr);gap:2px}
  .ph-tile{border-radius:0;border:0}
  .ph-by{display:none}
  .ph-alb{padding:0 10px}
  .ph-tab{padding:9px 6px;font-size:13px}
  .ph-sync-head{padding:0 10px}
  .ph-sync-grid{padding:0 10px}
}
.ph-sync-head{display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin:0 0 10px}
.ph-sync-head h3{margin:0;font-size:17px;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.ph-sync-head .muted{font-size:12px}
.ph-sync-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(170px,1fr));gap:10px}
.ph-sync-card{appearance:none;display:flex;flex-direction:column;gap:6px;padding:0;border:0;background:none;color:inherit;text-align:left;cursor:pointer;min-width:0}
.ph-sync-card .ph-tile{pointer-events:none}
.ph-sync-card:hover .ph-tile,.ph-sync-card:focus-visible .ph-tile{border-color:var(--neon,#0ff);box-shadow:0 0 0 1px var(--neon,#0ff),0 0 16px color-mix(in srgb,var(--neon,#0ff) 45%,transparent)}
.ph-sync-card:focus-visible{outline:none}
.ph-sync-card b{font-size:14px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.ph-sync-card small{color:var(--muted,#9aa);font-size:12px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.ph-glyph{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;color:var(--muted,#9aa)}
.ph-glyph .ic{width:34px;height:34px}
.ph-play{position:absolute;left:6px;bottom:6px;background:rgba(0,0,0,.65);color:#fff;border-radius:999px;padding:2px 8px;font:600 11px/1.4 Inter,system-ui,sans-serif;border:1px solid var(--neon,#0ff)}
.ph-tile img:not([src]){visibility:hidden}`;
    document.head.appendChild(s);
  }

  function nameOf(pk){ const p = (profOf && profOf(pk)) || {}; return p.display_name || p.name || (pk || '').slice(0, 8); }
  function picOf(pk){ const p = (profOf && profOf(pk)) || {}; return ok(p.picture) ? p.picture : ''; }

  function feedEl(){ return document.getElementById('feed'); }
  function render(){
    const feed = feedEl(); if(!feed) return;
    injectCss();
    const signedIn = !!(S.ME && S.ME.pubkey) && !S.GUEST;
    if(!signedIn && st.tab !== 'global') st.tab = 'global';
    if(st.tab === 'synced' && noSync()) st.tab = 'albums';
    feed.innerHTML = `<div class="ph-wrap">
      <div class="ph-head">
        <div class="ph-tabs" role="tablist">${TABS.filter(([k]) => (signedIn || k === 'global') && !(k === 'synced' && noSync())).map(([k, label]) =>
          `<button type="button" role="tab" class="ph-tab${st.tab === k ? ' on' : ''}" data-tab="${k}" aria-selected="${st.tab === k}">${label}</button>`).join('')}</div>
        <span class="ph-sp"></span>
        ${signedIn ? `<button type="button" class="btn btn-neon ph-post" aria-label="Post a photo" title="Post a photo">${ic('camera')}<span>Post a photo</span></button>
        <input type="file" class="ph-file" accept="image/*" multiple hidden>` : ''}
      </div>
      <div class="ph-body"></div></div>`;
    $$('.ph-tab', feed).forEach(b => b.onclick = () => {
      if(st.tab === b.dataset.tab) return;
      st.scroll[st.tab] = scrollTop();
      st.tab = b.dataset.tab; render();
    });
    const post = feed.querySelector('.ph-post'), file = feed.querySelector('.ph-file');
    if(post && file){
      post.onclick = () => file.click();
      file.onchange = () => { const fs = [...(file.files || [])].filter(f => /^image\//.test(f.type)); file.value = ''; if(fs.length) compose(fs); };
    }
    showTab();
  }
  function scroller(){ const f = feedEl(); return (f && f.closest('.osw-body, .main, main')) || document.scrollingElement; }
  function scrollTop(){ try{ return scroller().scrollTop || 0; }catch(_){ return 0; } }
  function restoreScroll(){ const y = st.scroll[st.tab] || 0; if(y) requestAnimationFrame(() => { try{ scroller().scrollTop = y; }catch(_){} }); }

  function body(){ const f = feedEl(); return f && f.querySelector('.ph-body'); }

  async function showTab(){
    const el = body(); if(!el) return;
    const tab = st.tab;
    if(tab === 'albums'){
      el.innerHTML = '<div class="ph-alb"><div class="muted small">Loading albums…</div></div>';
      try{ const m = await albums(); if(st.tab === 'albums' && el.isConnected) await m.mount(el.firstElementChild, S.ME.pubkey); }
      catch(e){ if(el.isConnected) el.innerHTML = `<div class="ph-empty">Could not open albums — ${enc(String((e && e.message) || e).slice(0, 120))}</div>`; }
      return;
    }
    if(tab === 'synced'){ showSynced(false); return; }
    const feed = st.feeds[tab];
    const who = authorsFor(tab), sig = who ? who.length + ':' + (S.ME && S.ME.pubkey) : 'g';
    if(feed.who !== sig){ st.feeds[tab] = newFeed(); st.feeds[tab].who = sig; }
    const f = st.feeds[tab];
    absorb(f, fromCache(tab));
    paint();
    restoreScroll();
    if(!f.fetched){
      f.fetched = true;
      await readPage(tab, false);
      if(st.tab === tab) paint();
    }
    // Opened right after sign-in, the follow list may not have arrived yet (only you are in it). Look again a
    // few times; when it lands, the tab re-reads with everybody in it instead of saying "follow people".
    if(who && who.length <= 1 && (S.ME && S.ME.pubkey)) waitForFollows(tab);
  }
  let _waiting = 0;
  function waitForFollows(tab){
    const gen = ++_waiting; let tries = 0;
    const look = () => {
      if(gen !== _waiting || st.tab !== tab || !body()) return;
      if(authorsFor(tab).length > 1){ showTab(); return; }
      if(++tries < 10) setTimeout(look, 800);
    };
    setTimeout(look, 800);
  }

  function paint(){
    const el = body(); if(!el || st.tab === 'albums' || st.tab === 'synced') return;
    const f = st.feeds[st.tab];
    const list = [...f.byId.values()].sort(order);
    if(!list.length){
      const who = authorsFor(st.tab);
      el.innerHTML = f.loading || !f.fetched ? '<div class="spinner"></div>'
        : f.unreachable ? `<div class="ph-empty">Could not reach the relays. <button type="button" class="mini ph-retry">Retry</button></div>`
        : (who && who.length <= 1) ? `<div class="ph-empty">Follow people to see their photos here — or look at <button type="button" class="mini ph-go-global">Global</button>.</div>`
        : `<div class="ph-empty">No photos yet.</div>`;
      const r = el.querySelector('.ph-retry'); if(r) r.onclick = () => { f.fetched = false; f.unreachable = false; showTab(); };
      const g = el.querySelector('.ph-go-global'); if(g) g.onclick = () => { st.tab = 'global'; render(); };
      return;
    }
    el.innerHTML = `<div class="ph-grid">${list.map(tileHtml).join('')}</div>
      <div class="ph-more">${f.done ? '<span class="muted small">That is everything the relays have.</span>'
        : `<button type="button" class="btn btn-ghost ph-load">${f.loading ? 'Loading…' : 'Load more'}</button>`}</div>`;
    $$('.ph-tile', el).forEach(t => t.onclick = () => openPhoto(t.dataset.id));
    const more = el.querySelector('.ph-load');
    if(more){
      const go = async () => { if(f.loading || f.done) return; more.textContent = 'Loading…'; const y = scrollTop(); await readPage(st.tab, true); paint(); try{ scroller().scrollTop = y; }catch(_){} };
      more.onclick = go;
      try{
        const io = new IntersectionObserver(es => { if(es.some(e => e.isIntersecting)){ io.disconnect(); go(); } }, { rootMargin: '600px' });
        io.observe(more);
      }catch(_){}
    }
  }
  function tileHtml(ev){
    const imgs = imagesOf(ev), pic = picOf(ev.pubkey);
    return `<button type="button" class="ph-tile" data-id="${ev.id}" aria-label="Photo by ${enc(nameOf(ev.pubkey))}">
      <img src="${enc(imgs[0])}" alt="" loading="lazy" decoding="async">
      ${imgs.length > 1 ? `<span class="ph-n">${imgs.length}</span>` : ''}
      <span class="ph-by">${pic ? `<img src="${enc(pic)}" alt="">` : ''}<span>${enc(nameOf(ev.pubkey))}</span></span></button>`;
  }
  function findEv(id){
    for(const f of Object.values(st.feeds)) if(f.byId.has(id)) return f.byId.get(id);
    try{ return Store.get && Store.get(id); }catch(_){ return null; }
  }

  function openPhoto(id){
    const ev = findEv(id); if(!ev) return;
    const imgs = imagesOf(ev); let at = 0;
    const signedIn = !!(S.ME && S.ME.pubkey) && !S.GUEST;
    const pic = picOf(ev.pubkey), when = new Date(ev.created_at * 1000);
    const title = ((ev.tags || []).find(t => t[0] === 'title') || [])[1] || '';
    modal(`<div class="ph-view">
      <div class="ph-view-img"><img class="ph-big" src="${enc(imgs[0])}" alt=""></div>
      ${imgs.length > 1 ? `<div class="ph-view-strip">${imgs.map((u, i) => `<button type="button" data-i="${i}" class="${i ? '' : 'on'}"><img src="${enc(u)}" alt=""></button>`).join('')}</div>` : ''}
      <button type="button" class="ph-author">${pic ? `<img src="${enc(pic)}" alt="">` : ''}<span>${enc(nameOf(ev.pubkey))}</span>
        <span class="muted small">· ${enc(when.toLocaleDateString())}</span></button>
      ${title ? `<b>${enc(title)}</b>` : ''}
      ${ev.content ? `<div class="ph-caption">${enc(ev.content)}</div>` : ''}
      <div class="ph-acts">
        ${signedIn ? `<button type="button" class="btn btn-neon ph-save">${ic('plus')}Save to album</button>` : ''}
        <button type="button" class="btn btn-ghost ph-open">${ic('chat')}Open post</button>
        <button type="button" class="btn btn-ghost ph-close">Close</button>
      </div></div>`, root => {
      const big = root.querySelector('.ph-big');
      big.onclick = () => { closeModal(); openLightbox(imgs[at], 'image', { items: imgs.map(src => ({ src, kind: 'image' })), i: at }); };
      $$('.ph-view-strip button', root).forEach(b => b.onclick = () => {
        at = +b.dataset.i; big.src = imgs[at];
        $$('.ph-view-strip button', root).forEach(x => x.classList.toggle('on', x === b));
      });
      root.querySelector('.ph-author').onclick = () => { closeModal(); openProfile(ev.pubkey); };
      root.querySelector('.ph-open').onclick = () => { closeModal(); openThread(ev.id); };
      root.querySelector('.ph-close').onclick = () => closeModal();
      const sv = root.querySelector('.ph-save'); if(sv) sv.onclick = () => { closeModal(); saveToAlbum(ev); };
    });
  }

  /** Pick one of your albums (or make one) and add this picture to it. */
  async function saveToAlbum(ev){
    const me = S.ME && S.ME.pubkey; if(!me) return;
    let m; try{ m = await albums(); }catch(_){ toast('Could not open albums — check your connection'); return; }
    modal(`<h3>Save to album</h3><div class="ph-pick"><div class="muted small">Loading your albums…</div></div>`, async root => {
      const box = root.querySelector('.ph-pick');
      const { albums: list, complete } = await m.readAlbums(me);
      if(!root.isConnected) return;
      if(!list.length && !complete){
        box.innerHTML = `<div class="muted">Could not reach the relays — nothing was changed.</div><button type="button" class="btn btn-ghost ph-x">Close</button>`;
        box.querySelector('.ph-x').onclick = () => closeModal(); return;
      }
      box.innerHTML = list.map(a => `<button type="button" class="btn btn-ghost ph-alb-pick" data-d="${enc(a.d)}">${ic('image')}${enc(a.title)}
          <span class="muted small">· ${a.ids.length}${a.ids.includes(ev.id) ? ' · already here' : ''}</span></button>`).join('')
        + `<button type="button" class="btn btn-neon ph-alb-new">${ic('plus')}New album…</button>
           <button type="button" class="btn btn-ghost ph-x">Cancel</button>`;
      box.querySelector('.ph-x').onclick = () => closeModal();
      $$('.ph-alb-pick', box).forEach(b => b.onclick = async () => {
        const a = list.find(x => x.d === b.dataset.d); if(!a) return;
        if(a.ids.includes(ev.id)){ toast('already in “' + a.title + '”'); return; }
        closeModal();
        try{ await m.editAlbum(a.d, tags => m.withPhotos(tags, [ev.id]), { known: a.ev }); toast('saved to “' + a.title + '”'); }
        catch(e){ toast(e.message); }
      });
      box.querySelector('.ph-alb-new').onclick = () => { closeModal(); m.albumForm(null, async a => {
        if(!a) return;
        try{ await m.editAlbum(a.d, tags => m.withPhotos(tags, [ev.id]), { known: a.ev }); toast('saved to “' + a.title + '”'); }
        catch(e){ toast(e.message); }
      }); };
    });
  }

  /** Caption + optional album, then ONE kind-20 post with every picture. */
  function compose(files){
    const urls = files.map(f => URL.createObjectURL(f));
    const drop = () => urls.forEach(u => { try{ URL.revokeObjectURL(u); }catch(_){} });
    modal(`<h3>Post ${files.length === 1 ? 'a photo' : files.length + ' photos'}</h3>
      <form class="ph-compose">
        <div class="ph-prev">${urls.map(u => `<img src="${u}" alt="">`).join('')}</div>
        <textarea class="input ph-cap" maxlength="4000" placeholder="Say something about it… #hashtags work"></textarea>
        <label class="small">Also add to album <select class="input ph-to"><option value="">No album</option></select></label>
        <div class="ph-st muted small"></div>
        <div class="ph-compose-btns"><button type="button" class="btn btn-ghost ph-cancel">Cancel</button>
          <button type="submit" class="btn btn-neon ph-go">Post</button></div>
      </form>`, async root => {
      const form = root.querySelector('.ph-compose'), sel = root.querySelector('.ph-to'), stl = root.querySelector('.ph-st');
      root.querySelector('.ph-cancel').onclick = () => { drop(); closeModal(); };
      setTimeout(() => { try{ root.querySelector('.ph-cap').focus(); }catch(_){} }, 30);
      let mod = null, list = [];
      albums().then(async m => { mod = m; const r = await m.readAlbums(S.ME.pubkey); list = r.albums;
        if(sel.isConnected) sel.insertAdjacentHTML('beforeend', list.map(a => `<option value="${enc(a.d)}">${enc(a.title)}</option>`).join('')); }).catch(() => {});
      form.onsubmit = async e => {
        e.preventDefault();
        const go = form.querySelector('.ph-go'); go.disabled = true;
        const caption = root.querySelector('.ph-cap').value.trim(), to = sel.value;
        const pics = [];
        try{
          for(let i = 0; i < files.length; i++){
            stl.textContent = `Uploading ${i + 1} of ${files.length}…`;
            const url = await uploadBlob(files[i]);
            if(!url) throw new Error('an upload failed — nothing was posted');
            const extra = ((await imetaTagsFor(url)) || [])[0];
            pics.push({ url, mime: files[i].type, extra });
          }
          stl.textContent = 'Posting…';
          const r = await publish(PIC, caption, postTags(pics, caption), { quiet: true });
          if(!(r && r.ev && (r.ok || r.queued))) throw new Error('the photo could not be posted — try again');
          drop(); closeModal();
          for(const k of ['following', 'global']) absorb(st.feeds[k], [r.ev]);
          if(st.tab !== 'albums') paint();
          if(to && mod){
            const a = list.find(x => x.d === to);
            try{ await mod.editAlbum(to, tags => mod.withPhotos(tags, [r.ev.id]), { known: a && a.ev }); toast('posted and added to “' + (a ? a.title : 'album') + '”'); }
            catch(err){ toast('posted, but not added to the album: ' + err.message); }
          } else toast(r.ok ? 'photo posted' : 'photo queued — it goes out when you are back online');
        }catch(err){ go.disabled = false; stl.textContent = ''; toast(err.message || String(err)); }
      };
    });
  }

  // ------------------------------------------------------------------------------------------------ Synced
  /* No instance (the bundled apps without a server) has no synced folders to read; the tab is not offered there. */
  function noSync(){ try{ return !!(standalone && standalone()) || typeof syncedAlbums !== 'function'; }catch(_){ return true; } }
  const albumKey = a => a.key + '\u0000' + a.dir;
  async function showSynced(force){
    const el = body(); if(!el) return;
    const sy = st.sync;
    // What this visit already read paints at once (#feed is blanked on every entry); a re-read runs behind it.
    if(sy.res) drawSynced(); else el.innerHTML = '<div class="ph-sync"><div class="spinner"></div><div class="muted small" style="text-align:center">Reading your synced folders…</div></div>';
    if(sy.loading) return;
    if(sy.res && !force) return;
    sy.loading = true;
    try{ sy.res = await syncedAlbums(!!force); }
    catch(e){ sy.res = { albums: [], failed: [], state: 'error', why: String((e && e.message) || e) }; }
    finally{ sy.loading = false; }
    if(st.tab === 'synced') drawSynced();
  }
  function drawSynced(){
    const el = body(); if(!el || st.tab !== 'synced') return;
    const sy = st.sync, r = sy.res || { albums: [], failed: [], state: 'error' };
    if(sy.open){
      const a = r.albums.find(x => albumKey(x) === sy.open);
      if(a) return drawSyncedAlbum(el, a);
      sy.open = null;
    }
    const refresh = `<button type="button" class="btn btn-ghost small ph-sync-refresh" title="Read the folders again">${ic('refresh')}<span>Refresh</span></button>`;
    if(r.state === 'error'){
      el.innerHTML = `<div class="ph-empty">Your synced folders could not be read just now.<br><br>${refresh}</div>`;
    }else if(r.state === 'none' || (!r.albums.length && !r.failed.length)){
      el.innerHTML = `<div class="ph-empty">${r.state === 'none'
          ? 'No synced folders yet. Sync your phone’s camera folder (or any folder of pictures) and every folder of pictures in it shows up here as an album.'
          : 'None of your synced folders hold pictures yet.'}<br><br>
        <button type="button" class="btn btn-neon ph-sync-setup">${ic('refresh')}<span>Set up Folder Sync</span></button> ${refresh}</div>`;
    }else{
      el.innerHTML = `<div class="ph-sync">
        <div class="ph-sync-head"><span class="muted">${r.albums.length} album${r.albums.length === 1 ? '' : 's'} from your synced folders${r.failed.length ? ` · could not read ${r.failed.map(enc).join(', ')}` : ''}</span><span class="ph-sp"></span>${refresh}</div>
        <div class="ph-sync-grid">${r.albums.map((a, i) => `<button type="button" class="ph-sync-card" data-i="${i}" title="${enc(a.key + (a.dir ? '/' + a.dir : ''))}">
            <span class="ph-tile">${a.cover ? '<img alt="">' : `<span class="ph-glyph">${ic('play')}</span>`}<span class="ph-n">${a.n}</span></span>
            <b>${enc(a.name)}</b><small>${enc(a.key + (a.dir ? ' / ' + a.dir : ''))}</small></button>`).join('')}</div></div>`;
      $$('.ph-sync-card', el).forEach(b => {
        const a = r.albums[+b.dataset.i];
        b.onclick = () => { st.scroll.synced = scrollTop(); sy.open = albumKey(a); sy.shown = SYNC_PAGE; drawSynced(); try{ scroller().scrollTop = 0; }catch(_){} };
        const img = b.querySelector('img'); if(img && a.cover) lazyThumb(img, a.cover);
      });
    }
    const set = el.querySelector('.ph-sync-setup'); if(set) set.onclick = () => { try{ switchView('sync'); }catch(_){} };
    const rf = el.querySelector('.ph-sync-refresh'); if(rf) rf.onclick = () => { sy.res = null; showSynced(true); };
  }
  function drawSyncedAlbum(el, a){
    const sy = st.sync, items = a.items.slice(0, sy.shown || SYNC_PAGE);
    el.innerHTML = `<div class="ph-sync">
      <div class="ph-sync-head"><button type="button" class="btn btn-ghost small ph-sync-back">← Albums</button>
        <h3 title="${enc(a.key + (a.dir ? '/' + a.dir : ''))}">${enc(a.name)}</h3><span class="muted">${a.n} item${a.n === 1 ? '' : 's'}</span></div>
      <div class="ph-grid">${items.map((it, i) => `<button type="button" class="ph-tile" data-i="${i}" title="${enc(it.name)}" aria-label="${enc(it.name)}">
          ${it.video ? `<span class="ph-glyph">${ic('play')}</span><span class="ph-play">video</span>` : '<img alt="">'}</button>`).join('')}</div>
      ${a.items.length > items.length ? `<div class="ph-more"><button type="button" class="btn btn-ghost ph-sync-more">Show ${Math.min(SYNC_PAGE, a.items.length - items.length)} more</button></div>` : ''}</div>`;
    el.querySelector('.ph-sync-back').onclick = () => { sy.open = null; drawSynced(); restoreScroll(); };
    const more = el.querySelector('.ph-sync-more');
    if(more) more.onclick = () => { const y = scrollTop(); sy.shown = (sy.shown || SYNC_PAGE) + SYNC_PAGE; drawSyncedAlbum(el, a); try{ scroller().scrollTop = y; }catch(_){} };
    $$('.ph-tile[data-i]', el).forEach(b => {
      const it = items[+b.dataset.i];
      b.onclick = () => { try{ Promise.resolve(openSynced(it)).catch(e => toast('could not open that: ' + ((e && e.message) || e))); }catch(e){ toast('could not open that'); } };
      const img = b.querySelector('img'); if(img) lazyThumb(img, it);
    });
  }
  /* Decrypted only when scrolled near: a camera roll is thousands of pictures, and Files' thumbnail cache runs a few at
   * a time and keeps the most recent ones. A picture that cannot be previewed keeps a picture glyph, never a blank. */
  let _thumbObs = null;
  function lazyThumb(img, item){
    const load = () => Promise.resolve(syncedThumb(item)).then(u => {
      if(!img.isConnected) return;
      if(u) img.src = u; else img.insertAdjacentHTML('afterend', `<span class="ph-glyph">${ic('image')}</span>`);
    }).catch(() => { if(img.isConnected) img.insertAdjacentHTML('afterend', `<span class="ph-glyph">${ic('image')}</span>`); });
    if(typeof IntersectionObserver === 'undefined'){ load(); return; }
    if(!_thumbObs) _thumbObs = new IntersectionObserver(es => es.forEach(e => {
      if(!e.isIntersecting) return; _thumbObs.unobserve(e.target); const f = e.target._phLoad; e.target._phLoad = null; if(f) f();
    }), { rootMargin: '300px' });
    img._phLoad = load; _thumbObs.observe(img);
  }

  return { render, openPhoto, saveToAlbum, imagesOf, hashtags, postTags };
};
