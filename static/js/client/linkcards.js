/* linkcards.js — split out of app.js by scripts/split_client_module.mjs.
 *
 * Loaded by its own <script> tag BEFORE app.js, and built by app.js at boot (`_linkcardsMod()`), so every
 * entry point answers synchronously. The code below is
 * app.js's own, moved byte-for-byte; its reads of app.js's live `let` bindings were rewritten to
 * `S.<name>` (getters/setters on `dep.state`) at exact identifier offsets, and everything else it uses
 * arrives through `dep`. tests/test_client_module_deps.py proves every name resolves.
 */
window.PCLinkCardsFactory = function(dep){
  const S = dep.state;   // live app.js bindings: S.LOGO, S.NO_IMAGES
  const {
    $$, NT, _hold, _instanceBase, _serverOrigin, actsRow, applyEmojis, emojiName, enc, linkify,
    needProfile, openNaddr, openThread, profOf, renderProfileView, safePk, timeAgo, ytId,
  } = dep;

  // ---------- link preview cards (OpenGraph via /client/preview, lazy on scroll) ----------
  // Persisted across reloads, because an UNKNOWN preview is a layout shift and a KNOWN one is not: the
  // card is rendered empty (`.link-card:empty{display:none}`) and then async-filled with an image and up
  // to three text rows, which shoves the rest of the feed down at a random moment. Once the answer is on
  // disk, linkCardHtml can emit the finished card inline on the very first paint, so a link you have seen
  // before never moves anything again. The remaining shift is a genuinely new URL, once.
  const _pv=(()=>{
    const K='pc-lpv-v1', MAX=600;
    let m=new Map(), dirty=false, t=null;
    try{ const raw=localStorage.getItem(K); if(raw) m=new Map(Object.entries(JSON.parse(raw))); }catch(_){}
    function flush(){ t=null; if(!dirty) return; dirty=false;
      try{
        if(m.size>MAX) m=new Map([...m].slice(-MAX));
        localStorage.setItem(K, JSON.stringify(Object.fromEntries(m)));
      }catch(_){}   // quota / private mode: the in-memory map still serves this session
    }
    return {
      has:(k)=>m.has(k), get:(k)=>m.get(k),
      set(k,v){ m.set(k,v); dirty=true; if(!t) t=setTimeout(flush, 2000); return this; },
    };
  })();
  function firstLink(text){
    const m=(text||'').match(/https?:\/\/[^\s<]+/g); if(!m) return null;
    for(let u of m){ u=u.replace(/[)\].,!?]+$/,''); if(ytId(u)) continue;  // YouTube is embedded inline, not carded
      if(!/\.(jpe?g|png|gif|webp|avif|mp4|webm|mov|m4v|mp3|ogg|wav|m4a|aac|flac)(\?|#|$)/i.test(u)) return u; }
    return null;
  }
  function _pvUseful(d){ return !!(d && (d.title || d.image || d.description)); }
  function _lcHost(url){ try{ return new URL(url).hostname.replace(/^www\./,''); }catch(_){ return url; } }
  // The card's inner markup. ONE implementation, used both by the synchronous cached path below and by
  // the async first-sight fill, so the two can never render a different shape for the same preview.
  function _lcInner(url, d){
    return `${d.image?_hold(`<img class="lc-img" src="${enc(d.image)}" loading="lazy" decoding="async" onerror="this.remove()">`, d.image, 'image', 'lc-img'):''}`
      + `<div class="lc-body"><div class="lc-site">${enc(d.site||_lcHost(url))}</div>`
      + `${d.title?`<div class="lc-title">${enc(d.title)}</div>`:''}`
      + `${d.description?`<div class="lc-desc">${enc(d.description.slice(0,160))}</div>`:''}</div>`;
  }
  /* A webxdc mini app attached to a post — a game, a poll, a shared editor — as a card you press to
   * play. The detection and the card both live in webxdc.js; this is only the seam, and it is written
   * to survive that module being absent (an older cached bundle, a standalone build that does not
   * ship it) by rendering nothing rather than throwing inside noteCard, where one exception replaces
   * EVERY post with "couldn't render this post". */
  /* A whole post whose SUBJECT is a mini app (kind 1063), rather than a note that happens to carry
   * one. Same chrome as any card — avatar, name, time — because it is a post and should be
   * repliable, zappable and openable like one; the difference is that the app IS the content, so the
   * description sits under it as a caption rather than the app being an attachment to some text. */
  function webxdcFileCard(ev){
    try{
      const p = profOf(ev.pubkey); needProfile(ev.pubkey);
      const name = p.display_name || p.name || safePk(ev.pubkey);
      const av = p.picture || S.LOGO;
      const desc = String(ev.content || '').trim();
      return `<article class="note" data-id="${ev.id}" data-pk="${ev.pubkey}">
        <img class="av" src="${enc(av)}" onerror="this.src='${S.LOGO}'">
        <div class="body">
          <div class="hd"><span class="name" data-prof="${ev.pubkey}">${emojiName(ev.pubkey,name)}</span>
            <span class="vchk"></span><span class="time">${timeAgo(ev.created_at)}</span></div>
          ${webxdcCardHtml(ev)}
          ${desc ? `<div class="txt">${applyEmojis(linkify(desc), ev)}</div>` : ''}
          ${actsRow(ev)}
        </div></article>`;
    }catch(_){ return ''; }
  }

  function webxdcCardHtml(ev){
    try{
      if(!window.PCWebxdc) return '';
      const app = PCWebxdc.appOf(ev);
      return app ? PCWebxdc.cardHtml(app) : '';
    }catch(_){ return ''; }
  }

  function linkCardHtml(content){
    if(S.NO_IMAGES) return '';   // data saver: no preview fetch/image, link stays clickable
    const u=firstLink(content); if(!u) return '';
    // Already know this link? Render it FINISHED, at its final size, in the first paint — no empty box, no
    // async swap, nothing below it moves. `data-done` keeps hydrateLinkCards from re-filling it.
    if(_pv.has(u)){
      const d=_pv.get(u);
      if(!_pvUseful(d)) return '';                       // known to have no preview → don't emit a card that would vanish
      return `<div class="link-card" data-url="${enc(u)}" data-done="1">${_lcInner(u, d)}</div>`;
    }
    return `<div class="link-card" data-url="${enc(u)}"></div>`;
  }
  // Fill directly on render (the empty placeholder is display:none via CSS until filled; an
  // IntersectionObserver never fires on a zero-height hidden element, which broke lazy loading).
  /* Link cards fill a FEW AT A TIME, never all at once.
   *
   * This was `forEach(fillLinkCard)`, which fires one request per card in the same tick — and a
   * timeline is hundreds of cards. Every one of them is an outbound fetch the INSTANCE performs on
   * the user's behalf: `/client/preview` opens an HTTPS connection to a third-party site, reads up
   * to 512KB and regex-parses it for OpenGraph tags, on a single uvicorn worker.
   *
   * Measured on the live node: 813 preview requests completed inside ONE ten-second window, 45
   * APScheduler jobs were "missed by" up to 18 seconds in half an hour, and `GET /client` — the page
   * itself — timed out at 20s three times running while every other route answered in milliseconds.
   * From outside that is the site being down, and it fired the uptime alert. One reader's feed did
   * it, with no bug anywhere: the fan-out IS the load.
   *
   * Four at a time turns a burst into a trickle. Cards still all fill, just visibly rather than in
   * one thundering herd — and the node stays answerable while they do. (The endpoint should cap its
   * own concurrency too; a client is not the only thing that can call it.) */
  const _LC_MAX = 4;
  const _lcQ = []; let _lcRun = 0;
  function _lcPump(){
    while(_lcRun < _LC_MAX && _lcQ.length){
      const el = _lcQ.shift(); _lcRun++;
      const fin = ()=>{ _lcRun--; _lcPump(); };
      try{ fillLinkCard(el).then(fin, fin); }catch(_){ fin(); }
    }
  }
  function hydrateLinkCards(scope){
    $$('.link-card[data-url]:not([data-done])', scope||document).forEach(el=>{
      el.setAttribute('data-done','1'); _lcQ.push(el);
    });
    _lcPump();
  }
  async function fetchPreview(url){ if(_pv.has(url)) return _pv.get(url); let d=null; try{ d=await fetch('/client/preview?url='+encodeURIComponent(url)).then(r=>r.json()); }catch(_){} _pv.set(url,d); return d; }
  async function fillLinkCard(el){
    const url=el.dataset.url; const d=await fetchPreview(url);
    if(!_pvUseful(d)){ el.remove(); return; }
    el.innerHTML=_lcInner(url, d);
  }
  // Opening a card is DELEGATED, not an onclick assigned during the fill: cards rendered straight from the
  // preview cache never pass through fillLinkCard, so a per-element handler would leave exactly the cards
  // that paint fastest unclickable. Capture phase so the click can't also reach the open-thread handler
  // under it — the same reason the carousel's nav uses capture.
  document.addEventListener('click', e=>{
    if(!e.target.closest) return;
    const card=e.target.closest('.link-card[data-url]'); if(!card) return;
    e.preventDefault(); e.stopPropagation();
    if(_openOwnEntityUrl(card.dataset.url)) return;
    window.open(card.dataset.url, '_blank', 'noopener');
  }, true);
  /* A LINK TO ONE OF THIS INSTANCE'S OWN NOSTR ADDRESSES OPENS IN THE APP, NOT OVER IT.
   *
   * "Clicking on a article link in a Social post ... should open in a new window, otherwise you
   * sometimes get back navigation issues or new social posts loading over it." A post that shares
   * `https://poster.place/naddr1…` carries a plain target=_blank link. In the APK an in-scope link
   * is a NAVIGATION of the one WebView -- the whole client reloads onto that URL, Back leaves the
   * app's history behind, and the boot's routing (`_routing`) paints the article in place over
   * whatever feed was there. On the desktop it went to the external browser instead of a PosterChan
   * window. So a link whose origin is this instance and whose path is a bech32 entity goes through
   * the same openers a `nostr:` reference uses: on PosterChanOS an article or post gets ITS OWN
   * post window (openArticle → openThread), elsewhere the in-app reader with a real history entry.
   * Any other link on this host (a Blossom file, /r/ repo pages, the admin) is left alone. */
  function _ownEntityOf(url){
    let u; try{ u=new URL(String(url||''), location.href); }catch(_){ return null; }
    if(!/^https?:$/.test(u.protocol)) return null;
    const mine=new Set([location.origin]);
    for(const b of [_instanceBase(), _serverOrigin()]){ try{ if(b) mine.add(new URL(b).origin); }catch(_){ } }
    if(!mine.has(u.origin)) return null;
    let p=u.pathname; try{ p=decodeURIComponent(p); }catch(_){ }
    const m=p.replace(/^\/client(?=\/|$)/,'').match(/^\/(?:nostr:)?((?:npub1|nprofile1|note1|nevent1|naddr1)[023456789acdefghjklmnpqrstuvwxyz]+)\/?$/i);
    return m ? m[1] : null;
  }
  function _openOwnEntityUrl(url){
    const ent=_ownEntityOf(url); if(!ent) return false;
    let d; try{ d=NT().nip19.decode(ent); }catch(_){ return false; }
    if(d.type==='naddr'){ openNaddr(d.data.pubkey, d.data.identifier, d.data.kind); return true; }
    if(d.type==='nevent'){ openThread(d.data.id, d.data.relays); return true; }
    if(d.type==='note'){ openThread(d.data); return true; }
    if(d.type==='npub'){ renderProfileView(d.data); return true; }
    if(d.type==='nprofile'){ renderProfileView(d.data.pubkey); return true; }
    return false;
  }
  document.addEventListener('click', e=>{
    if(e.defaultPrevented || e.button || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey || !e.target.closest) return;
    const a=e.target.closest('a[href]'); if(!a) return;
    /* An IN-PAGE link is never a link to an entity. Names, hashtags and quote links are `href="#"` with
       their own handlers; resolved against an address bar that holds an open post (`/nevent1…`), `#` IS
       that post, so this swallowed every tap on a name inside a post and re-opened the same post ("I
       can't click any usernames in the post on tablet"). */
    const raw=String(a.getAttribute('href')||'').trim(); if(!raw || raw.charAt(0)==='#') return;
    if(!a.closest('.note,.article-view,.dm-bubble,.quoted,.av-comments,.ac-item,.markdown')) return;
    if(!_ownEntityOf(a.href)) return;
    e.preventDefault(); e.stopPropagation();
    _openOwnEntityUrl(a.href);
  }, true);
  // YouTube video id from watch / youtu.be / shorts / embed / live URLs (else null).

  return {
    hydrateLinkCards, linkCardHtml, webxdcCardHtml, webxdcFileCard,
  };
};
