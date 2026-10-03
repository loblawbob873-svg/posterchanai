/* drafts.js — split out of app.js by scripts/split_client_module.mjs.
 *
 * Loaded by its own <script> tag BEFORE app.js, and built synchronously by app.js (`_draftsMod()`) at the
 * point where this code used to sit — Drafts and Scheduled are objects the composer, the offline outbox,
 * the ☰ badge and the login pull read synchronously, so they are returned from here and reached from
 * app.js through `_lzProxy`, which cannot wait on a network load. The code below is
 * app.js's own, moved byte-for-byte; its reads of app.js's live `let` bindings were rewritten to
 * `S.<name>` (getters/setters on `dep.state`) at exact identifier offsets, and everything else it uses
 * arrives through `dep`. tests/test_client_module_deps.py proves every name resolves.
 */
window.PCDraftsFactory = function(dep){
  const S = dep.state;   // live app.js bindings: S.CFG, S.ME, S.VIEW
  const {
    $, $$, NT, _draftSnapshot, _enrichTags, _qDraftSet, _qDrafts, _queuedDraftMatches,
    _reconcileDeliveredDrafts, bumpMoreBadge, compose, enc, fetchEvent, hydrate, linkify,
    mentionTags, publish, replyKindFor, replyTags, selfProof, sign, timeAgo, toast, uiConfirm,
  } = dep;

  // ---------- drafts (local-only, per-account; never published until you send) ----------
  const Drafts = {
    key(){ return 'pc_drafts_' + ((typeof S.ME!=='undefined' && S.ME && S.ME.pubkey) || 'anon'); },
    all(){ try{ return JSON.parse(localStorage.getItem(this.key())||'[]'); }catch(_){ return []; } },
    // DATA SAFETY: keep EVERY live draft; only bound tombstones. The old flat slice(0,300)-by-ts dropped
    // whatever was oldest — so a burst of freshly-timestamped tombstones (a delete storm / rolling-autosave
    // churn) evicted real drafts out of the cap and destroyed them. A tombstone only has to survive long
    // enough to propagate its delete, so a small recent set suffices; a live draft is NEVER dropped for one.
    _cap(a){ const live=[], tomb=[]; for(const d of (a||[])){ if(d&&d.id){ (d.del?tomb:live).push(d); } }
      live.sort((x,y)=>(y.ts||0)-(x.ts||0)); tomb.sort((x,y)=>(y.ts||0)-(x.ts||0));
      return [...live.slice(0,2000), ...tomb.slice(0,120)]; },
    _save(a){ const c=this._cap(a); try{ localStorage.setItem(this.key(), JSON.stringify(c)); }catch(_){} bumpDraft(); this._sync(c); },
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
    /* Delete every live draft in ONE pass. Calling remove() in a loop re-reads, re-caps (which sorts
     * twice) and rewrites the whole store per draft, and fires the debounced push each time — the
     * same per-item full-read shape that made bookmark sync lock the browser up. Tombstones, not a
     * drop, for the reason above: a plain delete comes back from the other device's copy. */
    removeAll(){ const now=Math.floor(Date.now()/1000);
      const a=this.all(), dead=new Set(this.live().map(d=>d.id));
      if(!dead.size) return 0;
      let out=a.map(x=> dead.has(x.id) ? {id:x.id, ts:now, del:true} : x);
      out=out.filter(x=> !(x.del && now-(x.ts||0) > 2592000));
      this._save(out); return dead.size; },
    // Sync to/from a single encrypted Nostr event (kind-30078 pcai:drafts under the storage key),
    // so drafts written on one device appear on another. Push is debounced.
    /* A SYNC THAT WAS REFUSED IS NOT A SYNC, AND THIS ONE COULD NOT TELL.
     *
     * It awaited the POST and threw the answer away inside `catch(_){}` — and `fetch` does not throw
     * on an HTTP error, so a 503 ("relay rejected the write, not saved") was indistinguishable from
     * success. That matters because `pull()` unions by id and newest-ts-wins and deliberately never
     * drops a draft: if the removal never reached the server, the server's copy comes back on the
     * next load. Reported as "lots of my fedi replies get stuck in drafts" — the reply HAD been
     * posted (no kind-1 or kind-1111 refusal exists anywhere in the relay log); it was the drafts
     * document that would not save, silently.
     *
     * So: read the verdict, retry a refusal a couple of times with backoff — these failures are
     * transient by nature (a superseded AUTH challenge, a relay that blinked) — and if it still will
     * not land, SAY SO once rather than leaving someone to discover it by watching a draft
     * resurrect. `_syncFailed` keeps it to one message per failing run, not one per keystroke. */
    _sync(a){ if(typeof S.ME==='undefined'||!S.ME) return; const owner=S.ME.pubkey; clearTimeout(this._t); this._t=setTimeout(async()=>{
      if(!S.ME||S.ME.pubkey!==owner)return;
      for(let attempt=0; attempt<3; attempt++){
        if(!S.ME||S.ME.pubkey!==owner)return;
        let landed=false;
        try{ const auth=await selfProof();
          if(!S.ME||S.ME.pubkey!==owner)return;
          const res=await fetch('/client/drafts',{method:'POST',headers:{'Content-Type':'application/json'},
            body:JSON.stringify({pubkey:owner,auth:auth,drafts:a})});
          let body=null; try{ body=await res.json(); }catch(_){ body=null; }
          /* NOTHING IS KEPT ON THE SERVER FOR THIS ACCOUNT (Nostr-only, not a member here): not a failure
             to retry, and not "may come back" -- say once that drafts live on this device only. */
          if(body && body.local_only){
            if(!this._localOnlySaid){ this._localOnlySaid=true;
              try{ toast('Drafts are kept on this device only — they sync between devices with an account or a NIP-05 name on this server.'); }catch(_){ } }
            return;
          }
          landed = !!(res && res.ok && (!body || body.ok !== false));
        }catch(_){ landed=false; }
        if(landed){ this._syncFailed=false; return; }
        if(attempt<2) await new Promise(r=>setTimeout(r, 1200*(attempt+1)));
      }
      if(!this._syncFailed){
        this._syncFailed=true;
        try{ toast('Your drafts could not be saved to the server — they are safe on this device, but a draft you send may come back until this succeeds.'); }catch(_){ }
      }
    }, 900); },
    async pull(){ if(typeof S.ME==='undefined'||!S.ME) return; const owner=S.ME.pubkey;
      try{ const auth=await selfProof();
        if(!S.ME||S.ME.pubkey!==owner)return;
        const r=await fetch('/client/drafts',{method:'POST',headers:{'Content-Type':'application/json'},
          body:JSON.stringify({pubkey:owner,auth:auth})}).then(r=>r.json());
        if(!S.ME||S.ME.pubkey!==owner)return;
        if(r && r.ok && Array.isArray(r.drafts)){
          // union by id, newest ts wins — never drops a draft made offline on either device
          const map={}; [...r.drafts, ...this.all()].forEach(d=>{ if(d&&d.id&&(!map[d.id]||(d.ts||0)>=(map[d.id].ts||0))) map[d.id]=d; });
          const merged=this._cap(Object.values(map));   // live never evicted by tombstones (see _cap)
          try{ localStorage.setItem(this.key(), JSON.stringify(merged)); }catch(_){}
          bumpDraft(); if(S.VIEW==='drafts') renderDrafts();
        } }catch(_){} },
  };
  // `#draft-badge` is the sidebar's Drafts row and stays drafts-only — it labels one thing.
  function bumpDraft(){ const n=Drafts.live().length; $$('#draft-badge').forEach(b=>{ if(n){b.textContent=n>99?'99+':n;b.classList.remove('hidden');}else b.classList.add('hidden'); }); bumpMoreBadge(); }
  // ---------- Scheduled posts: sign a note with a FUTURE created_at; the backend broadcasts it at that time.
  // The server never holds your key — signing stays here — so it works for nip07 / Amber / local nsec alike. ----
  const Scheduled = {
    _cache:null, _cacheAt:0,
    _proof(){ return selfProof(); },   // shared with Drafts → opening Drafts never triggers a 2nd signer prompt
    async create(kind, content, tags, whenTs){
      const t=_enrichTags(kind, tags, content);   // same enrichment as publish() → a scheduled post is identical
      const ev=await sign(kind, content, t, whenTs);   // signed with the FUTURE created_at = the scheduled time
      try{
        const r=await fetch('/client/scheduled',{method:'POST',headers:{'Content-Type':'application/json'},
          body:JSON.stringify({pubkey:S.ME.pubkey,auth:await this._proof(),event:ev,scheduled_at:whenTs})}).then(r=>r.json());
        this._cache=null; return r;
      }catch(e){ return { ok:false, error:(e&&e.message)||'network' }; }
    },
    async list(force){ if(typeof S.ME==='undefined'||!S.ME) return [];
      const now=Date.now();
      if(!force && this._cache && (now-this._cacheAt)<4000) return this._cache;   // short TTL: avoid re-fetch on rapid re-renders
      try{ const r=await fetch('/client/scheduled/list',{method:'POST',headers:{'Content-Type':'application/json'},
        body:JSON.stringify({pubkey:S.ME.pubkey,auth:await this._proof()})}).then(r=>r.json());
        const posts=(r&&Array.isArray(r.posts))?r.posts:[]; this._cache=posts; this._cacheAt=now; return posts;
      }catch(_){ return this._cache||[]; } },
    async cancel(id){ try{ const r=await fetch('/client/scheduled/cancel',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({pubkey:S.ME.pubkey,auth:await this._proof(),id})}).then(r=>r.json()); this._cache=null; return !!(r&&r.ok); }catch(_){ return false; } },
  };
  function renderDrafts(){
    _reconcileDeliveredDrafts();
    const feed=$('#feed'); const list=Drafts.live();
    const draftsHtml = list.length ? list.map(d=>{
      const ctx = d.reply?'<span class="muted small">↩ reply</span>' : d.quote?'<span class="muted small">❝ quote</span>' : '';
      return `<div class="note draft-card" data-draft="${d.id}"><div class="draft-body">${linkify(d.text||'')}</div>
        <div class="draft-foot"><span class="muted small df-meta">${ctx} saved ${timeAgo(d.ts)}</span>
          <span class="spacer"></span>
          <div class="draft-actions">
            <button class="btn btn-cyan small" data-act="edit"><svg class="ic b-ic" aria-hidden="true"><use href="#i-pen"></use></svg>Edit</button>
            <button class="btn btn-red small" data-act="del"><svg class="ic b-ic" aria-hidden="true"><use href="#i-trash"></use></svg>Delete</button>
            <button class="btn btn-cyan small" data-act="send">Send ▶</button>
          </div></div></div>`;
    }).join('') : '<div id="drafts-empty" class="empty">No drafts. Write a post and tap 💾 Draft to save it for later.</div>';
    // Delete-all only exists when there IS something to delete — a destructive button on an empty
    // screen is just a way to mis-tap.
    const clearHtml = list.length
      ? `<div class="art-top"><button class="btn btn-red small" id="draft-clear"><svg class="ic b-ic" aria-hidden="true"><use href="#i-trash"></use></svg>Delete all drafts (${list.length})</button></div>`
      : '';
    feed.innerHTML = `<div id="sched-section"></div>` + clearHtml + draftsHtml;
    { const clr=$('#draft-clear');
      if(clr) clr.onclick=async()=>{
        const n=Drafts.live().length; if(!n) return;
        // in-app confirm, NOT native — a native dialog wedges the Electron renderer's focus
        if(await uiConfirm(`Delete all ${n} draft${n===1?'':'s'}? This cannot be undone.`,{ok:'Delete all',danger:true})){
          Drafts.removeAll(); renderDrafts(); toast('drafts deleted');
        } }; }
    feed.querySelectorAll('.draft-card').forEach(card=>{
      const id=card.dataset.draft;
      card.querySelector('[data-act="edit"]').onclick=()=>{ const d=Drafts.get(id); if(d) compose({reply:d.reply,replyPk:d.replyPk,quote:d.quote,draftId:id,text:d.text,cw:d.cw,cwReason:d.cwReason}); };
      card.querySelector('[data-act="del"]').onclick=async()=>{ if(await uiConfirm('Delete this draft?',{ok:'Delete',danger:true})){ Drafts.remove(id); renderDrafts(); } };   // in-app confirm, NOT native — a native dialog wedges the Electron renderer's focus and breaks the next composer
      { const sb=card.querySelector('[data-act="send"]'); if(sb) sb.onclick=()=>sendDraft(id, sb); }
    });
    hydrate(feed);
    _renderScheduled();   // async: fill the ⏰ Scheduled section above the drafts
  }
  async function _renderScheduled(){
    if(S.VIEW!=='drafts') return;
    const posts=await Scheduled.list();
    const host=$('#sched-section'); if(!host || S.VIEW!=='drafts') return;   // navigated away while fetching
    // Hide the "No drafts" empty-state when there ARE scheduled posts (else it reads as contradictory).
    { const de=$('#drafts-empty'); if(de) de.style.display = posts.length ? 'none' : ''; }
    if(!posts.length){ host.innerHTML=''; return; }
    host.innerHTML = `<div class="sched-head">⏰ Scheduled · ${posts.length}</div>` + posts.map(p=>{
      const when=new Date((p.scheduled_at||0)*1000).toLocaleString();
      const st=p.status;
      const label = st==='failed' ? ('⚠ failed to publish · was due '+when)
                  : st==='sending' ? ('⏰ '+when+' · sending…') : ('⏰ '+when);
      const action = st==='sending' ? '<span class="muted small">sending…</span>'
        : `<button class="btn btn-ghost small" data-act="cancel" style="color:var(--danger)">${st==='failed'?'✖ Dismiss':'✖ Cancel'}</button>`;
      return `<div class="note draft-card sched-card${st==='failed'?' sched-failed':''}" data-sid="${enc(String(p.id))}"><div class="draft-body">${linkify(p.preview||'(no text)')}</div>
        <div class="draft-foot"><span class="muted small">${enc(label)}</span><span class="spacer"></span>${action}</div></div>`;
    }).join('');
    host.querySelectorAll('.sched-card').forEach(card=>{
      const b=card.querySelector('[data-act="cancel"]'); if(!b) return;
      const failed=card.classList.contains('sched-failed');
      b.onclick=async()=>{ if(!await uiConfirm(failed?'Dismiss this failed scheduled post?':'Cancel this scheduled post?')) return; b.disabled=true;
        const ok=await Scheduled.cancel(card.dataset.sid);
        if(!ok && !failed) toast('too late — it already posted');
        if(S.VIEW==='drafts') _renderScheduled(); };
    });
  }
  // Append a quoted note as an inline `nostr:nevent` (WITH relay hint + author) to the post content.
  // A NIP-18 quote needs BOTH the `q` tag AND the inline nevent: many clients (Damus/Amethyst/Primal)
  // render the quote from the CONTENT nevent, not the q-tag, so a q-tag-only quote shows as bare text
  // there. No-op if the content already carries an nevent/note reference (user pasted one).
  function _appendQuoteNevent(content, id, pk){
    try{
      if(/nostr:(nevent1|note1)/i.test(content||'')) return content;
      const nev='nostr:'+NT().nip19.neventEncode({ id, relays:[S.CFG.relay_url].filter(Boolean), author:pk||undefined });
      return (content && content.trim() ? content.trim()+'\n\n' : '')+nev;
    }catch(_){ return content; }
  }
  const _draftSending=new Map();
  async function sendDraft(id, btn){
    /* Acknowledge the CLICK before anything that can block. Publishing goes through the signer, and
     * a remote signer can legitimately take a while (or never answer), so without this the button
     * sat inert with no toast and no spinner — reported as "click send, nothing happens". */
    if(btn){ btn.disabled = true; btn.dataset.label = btn.textContent; btn.textContent = 'sending…'; }
    const _done = () => { if(btn && btn.isConnected){ btn.disabled = false; btn.textContent = btn.dataset.label || 'Send ▶'; } };
    const key=((S.ME&&S.ME.pubkey)||'')+':'+id;
    let pending=_draftSending.get(key);
    if(!pending){pending=_sendDraft(id).finally(()=>{if(_draftSending.get(key)===pending)_draftSending.delete(key);});_draftSending.set(key,pending);}
    try{ return await pending; }
    finally{ _done(); }
  }
  async function _sendDraft(id){
    const d=Drafts.get(id);
    // Never fail silently: a Send that does nothing, with no message, is indistinguishable from a broken app.
    if(!d){ toast('couldn’t find that draft — reload and try again'); return; }
    if(!(d.text||'').trim()){ toast('that draft is empty'); return; }
    const owner=S.ME&&S.ME.pubkey,snapshot=_draftSnapshot(d);
    _reconcileDeliveredDrafts();if(!Drafts.get(id))return;
    const mappings=Object.entries(_qDrafts()).filter(([,entry])=>entry&&(typeof entry==='string'?entry:entry.id)===id && (!entry.owner||entry.owner===owner));
    for(const [eventId,entry] of mappings){
      let ev=window.Outbox&&Outbox.list().find(x=>x.ev.id===eventId)?.ev;
      if(!ev)ev=Store.get(eventId);
      if(!ev){try{ev=await fetchEvent(eventId);}catch(_){}}
      if(!S.ME||S.ME.pubkey!==owner)return;
      if(!ev||ev.pubkey!==owner||!ev.sig){toast('Could not confirm the earlier delivery. Reconnect and retry; your draft is kept.');return;}
      if(!_queuedDraftMatches(entry,Drafts.get(id),ev)){toast('An earlier version is still pending. Discard its pending copy before sending these edits.');return;}
      if(!Outbox.has(eventId))Outbox.add(ev);
      try{Relay.reviveStale();}catch(_){}
      const result=await Outbox.flush(eventId);
      if(!result.sent)toast('Still waiting for the relay; your draft is kept.');
      if(S.VIEW==='drafts')renderDrafts();
      return;
    }
    let tags=[]; let content=d.text;
    if(d.reply){ const o=Store.get(d.reply); tags=replyTags(o, d.reply, d.replyPk); }
    if(d.quote){ const o=Store.get(d.quote); const qpk=(o&&o.pubkey)||''; tags.push(['q', d.quote, S.CFG.relay_url||'', qpk]); if(qpk)tags.push(['p',qpk]); content=_appendQuoteNevent(content, d.quote, qpk); }
    mentionTags(d.text).forEach(t=>{ if(!tags.some(x=>x[0]==='p'&&x[1]===t[1])) tags.push(t); });
    if(d.cw) tags.push(['content-warning', d.cwReason||'']);   // honour a draft's 🔞 flag on direct send too
    // Remove the draft ONLY once the relay has actually stored the post. On any failure the draft stays —
    // it is the recovery path (the user retries from here), so its text is never lost.
    try{
      const r=await publish(replyKindFor(d.reply?Store.get(d.reply):null), content, tags);
      if(r && r.ok){ if(S.ME&&S.ME.pubkey===owner&&_draftSnapshot(Drafts.get(id))===snapshot)Drafts.remove(id); toast('posted'); }   // failure toast + kept draft handled by publish()
      // Queued, not sent: keep the draft as the recovery copy, but remember which event it became so
      // the flush can retire it once the relay actually takes it (see _qDrafts).
      else if(r && r.queued && r.ev) _qDraftSet(r.ev.id, id, owner, snapshot);
      if(S.VIEW==='drafts') renderDrafts();
    }
    catch(e){ toast('post failed: '+((e&&e.message)||e)); }   // signing failed → nothing was created; keep the draft
  }

  return {
    Drafts, Scheduled, _appendQuoteNevent, bumpDraft, renderDrafts,
  };
};
