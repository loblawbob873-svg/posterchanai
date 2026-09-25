/* Photo albums -- NIP-51 PICTURE SETS (kind 30006) of NIP-68 PICTURE POSTS (kind 20).
 *
 * The profile's "Albums" tab. An album is one kind-30006 set: `d`, `title`, `description`, an optional
 * `image` (the cover) and one `e` per photo, in order. A photo is an ordinary kind-20 picture post
 * (`imeta` url/m/x/dim), so an album made here opens in Olas and any other NIP-68 client, and theirs
 * open here. Photos come from the device (uploaded to the user's media server) or from Files (the
 * Blossom drive -- never an ENCRYPTED drive file: its bytes are unreadable to everybody else).
 *
 * THE SET IS REPLACEABLE, so every change is a read-modify-write of the whole album -- the shape that
 * has wiped follow and mute lists here before. Two rules, both in `editAlbum`: a change is built on a
 * read that EVERY relay answered (`complete`), never on "no answer" read as "empty album"; and changes
 * to albums are serialized, so two quick adds cannot each start from the same old list and drop the
 * other's photo. The pure halves (albumOf / latest / withPhotos / withoutPhoto / picTags) are exported
 * for tests/client/test_albums_rules.py.
 */
window.PCAlbumsFactory = function(dep){
  const S = dep.state;   // live app.js bindings: S.ME
  const {
    $, $$, enc, toast, modal, closeModal, uiConfirm, publish, uploadBlob, imetaTagsFor, blossomPicker,
    openLightbox,
  } = dep;
  const SET = 30006, PIC = 20;

  // ---------------------------------------------------------------------------------- pure halves
  const tagVal = (ev, name) => { const t = (ev.tags || []).find(t => t[0] === name && t[1] != null); return t ? String(t[1]) : ''; };

  /** One album from its set event: {d, title, description, cover, ids, ev}. Photo order is tag order. */
  function albumOf(ev){
    const ids = [];
    for(const t of ev.tags || []) if(t[0] === 'e' && /^[0-9a-f]{64}$/.test(t[1] || '') && !ids.includes(t[1])) ids.push(t[1]);
    return { d: tagVal(ev, 'd'), title: tagVal(ev, 'title') || tagVal(ev, 'name') || 'Untitled album',
             description: tagVal(ev, 'description'), cover: /^https?:\/\//i.test(tagVal(ev, 'image')) ? tagVal(ev, 'image') : '', ids, ev };
  }

  /** The newest version of each of `pk`'s albums (replaceable: one per `d`), newest first. */
  function latest(evs, pk){
    const by = new Map();
    for(const e of evs || []){
      if(!e || e.kind !== SET || (pk && e.pubkey !== pk)) continue;
      const d = tagVal(e, 'd'); if(!d) continue;
      const cur = by.get(d);
      if(!cur || e.created_at > cur.created_at || (e.created_at === cur.created_at && e.id < cur.id)) by.set(d, e);
    }
    return [...by.values()].sort((a, b) => b.created_at - a.created_at).map(albumOf);
  }

  /** Add photo ids to a set's tags: appended in order, never duplicated, every other tag kept. */
  function withPhotos(tags, ids){
    const out = (tags || []).map(t => t.slice());
    const have = new Set(out.filter(t => t[0] === 'e').map(t => t[1]));
    for(const id of ids || []) if(id && !have.has(id)){ out.push(['e', id]); have.add(id); }
    return out;
  }

  /** Remove one photo; if it was the cover, the cover goes too (the grid falls back to the first photo). */
  function withoutPhoto(tags, id, url){
    return (tags || []).filter(t => !(t[0] === 'e' && t[1] === id) && !(url && t[0] === 'image' && t[1] === url))
                       .map(t => t.slice());
  }

  /** Replace (or set) one single-valued tag -- title / description / image. Empty removes it. */
  function withTag(tags, name, value){
    const out = (tags || []).filter(t => t[0] !== name).map(t => t.slice());
    if(value) out.push([name, String(value)]);
    return out;
  }

  /** NIP-68 kind-20 tags for one picture. `imeta` is REQUIRED there; `m`/`x` repeat its fields so
   *  clients can filter by type and hash. `extra` = an imeta tag the uploader already built (dim etc.). */
  function picTags(url, { mime, sha, title, extra } = {}){
    let imeta = (extra && extra[0] === 'imeta') ? extra.slice() : ['imeta', 'url ' + url];
    if(mime && !imeta.some(p => /^m /.test(p))) imeta.push('m ' + mime);
    if(sha && !imeta.some(p => /^x /.test(p))) imeta.push('x ' + sha);
    const tags = [imeta];
    if(title) tags.unshift(['title', title]);
    const m = (imeta.find(p => /^m /.test(p)) || '').slice(2), x = (imeta.find(p => /^x /.test(p)) || '').slice(2);
    if(m) tags.push(['m', m]);
    if(x) tags.push(['x', x]);
    return tags;
  }

  /** The picture a post shows: its imeta url (NIP-68), else a url/image tag, else an image link in the
   *  text (an album may list another client's kind-1 image note). Synchronous on purpose -- the app's
   *  _firstImage is a lazy wrapper that answers a PROMISE until upload.js has loaded. */
  function imageOf(ev){
    if(!ev) return '';
    const ok = u => /^https?:\/\//i.test(u || '');
    for(const t of ev.tags || []) if(t[0] === 'imeta'){ const u = t.find(p => /^url /.test(p)); if(u && ok(u.slice(4))) return u.slice(4); }
    for(const t of ev.tags || []) if((t[0] === 'url' || t[0] === 'image') && ok(t[1])) return t[1];
    const m = (ev.content || '').match(/https?:\/\/[^\s)<]+\.(?:jpe?g|png|gif|webp|avif)(?:\?[^\s)<]*)?/i);
    return m ? m[0] : '';
  }

  const newD = () => 'album-' + Date.now().toString(36) + '-' + Math.random().toString(36).slice(2, 8);

  // ------------------------------------------------------------------------------------- reads
  // A REQ written to a CONNECTING socket is dropped (relay.js _send), and a profile is often opened
  // right after sign-in -- so every read here waits for a socket that can answer (CLAUDE.md).
  async function ready(){ try{ if(Relay.ready) await Relay.ready(); }catch(_){} }

  async function readAlbums(pk){
    await ready();
    let evs = [];
    try{ evs = await Relay.query([{ kinds: [SET], authors: [pk], limit: 200 }]); }catch(_){ return { albums: [], complete: false }; }
    try{ evs.forEach(e => Store.saveEvent(e)); }catch(_){}
    return { albums: latest(evs, pk), complete: evs.complete !== false };
  }

  async function readPhotos(ids){
    await ready();
    const out = new Map();
    for(let i = 0; i < ids.length; i += 200){
      const chunk = ids.slice(i, i + 200).filter(id => { const hit = Store.get && Store.get(id); if(hit) out.set(id, hit); return !hit; });
      if(!chunk.length) continue;
      try{ (await Relay.query([{ ids: chunk, limit: chunk.length }])).forEach(e => { out.set(e.id, e); try{ Store.saveEvent(e); }catch(_){} }); }catch(_){}
    }
    return out;
  }

  // ------------------------------------------------------------------------------------ writes
  let _chain = Promise.resolve();
  /** Read the album from the relays, change its tags with `mutate(tags)`, publish. Serialized.
   *
   * Three refusals, each a way the replaceable set gets overwritten with less than it held:
   *  - a read no relay finished (`complete === false`) is not an empty album;
   *  - a COMPLETE read that does not contain the album is not one either, unless this is `create` -- a
   *    relay that dropped mid-query is taken out of the wait set, so the others' EOSEs can finish a read
   *    that never saw the album, and an "add photos" built on it published the new photos ALONE;
   *  - a copy OLDER than the one on screen (`known`) is a stale relay, not the album.
   * The new version is signed strictly newer than the old: two edits in one second tie on created_at and
   * NIP-01 keeps the lower id, losing the later edit about half the time. */
  function editAlbum(d, mutate, { create = false, known = null } = {}){
    const run = _chain.then(async () => {
      const me = S.ME && S.ME.pubkey; if(!me) throw new Error('sign in to edit albums');
      await ready();
      const got = await Relay.query([{ kinds: [SET], authors: [me], '#d': [d], limit: 5 }]);
      if(got.complete === false) throw new Error('the relays did not answer — nothing was changed, try again');
      const cur = latest(got, me)[0];
      if(create && cur) throw new Error('an album with that address already exists — nothing was changed');
      if(!create && !cur) throw new Error('this album was not found on the relays — nothing was changed, try again');
      if(!create && known && known.created_at > cur.ev.created_at)
        throw new Error('the relays sent an older copy of this album — nothing was changed, try again');
      const tags = mutate(cur ? cur.ev.tags.map(t => t.slice()) : [['d', d]]);
      if(!tags) return null;
      if(!tags.some(t => t[0] === 'd' && t[1] === d)) tags.unshift(['d', d]);
      const now = Math.floor(Date.now() / 1000);
      const createdAt = cur ? Math.max(now, cur.ev.created_at + 1) : now;
      const r = await publish(SET, '', tags, { quiet: true, createdAt });
      if(!(r && r.ok)) throw new Error('the album could not be saved — try again');
      return r.ev ? albumOf(r.ev) : null;
    });
    _chain = run.catch(() => {});
    return run;
  }

  /** Publish one kind-20 picture post; returns its id. */
  async function postPicture(url, meta){
    const r = await publish(PIC, '', picTags(url, meta), { quiet: true });
    // Queued offline (the Outbox takes ordinary kinds) is posted later: its id is final, so the album can
    // list it now rather than leaving the photo orphaned outside every album.
    if(!(r && r.ev && (r.ok || r.queued))) throw new Error('a photo could not be posted');
    return r.ev.id;
  }

  // ---------------------------------------------------------------------------------------- UI
  // Icons from the app's own sprite, never a text glyph: "＋" and "⋯" are not in the UI font and drew as
  // empty boxes on the first build of this screen.
  const ic = n => `<svg class="ic b-ic" aria-hidden="true"><use href="#i-${n}"></use></svg>`;
  let _gen = 0;
  const isMine = pk => !!(S.ME && S.ME.pubkey === pk);

  async function mount(el, pk){
    if(!el) return;
    const gen = ++_gen;
    el.innerHTML = '<div class="alb-status muted small">Loading albums…</div>';
    const { albums, complete } = await readAlbums(pk);
    if(gen !== _gen || !el.isConnected) return;
    const mine = isMine(pk);
    const head = mine ? `<div class="alb-bar"><button type="button" class="btn btn-neon alb-new">${ic('plus')}New album</button></div>` : '';
    if(!albums.length){
      el.innerHTML = head + (complete
        ? `<div class="empty">${mine ? 'No albums yet — make one and add photos from your phone or from Files.' : 'No albums yet.'}</div>`
        : `<div class="empty">Could not reach the relays. <button type="button" class="mini alb-retry">Retry</button></div>`);
    } else {
      el.innerHTML = head + `<div class="alb-grid">${albums.map(a => `
        <button type="button" class="alb-card" data-d="${enc(a.d)}">
          <span class="alb-cover">${a.cover ? `<img src="${enc(a.cover)}" alt="" loading="lazy">` : '<span class="alb-cover-empty">🖼️</span>'}</span>
          <span class="alb-title">${enc(a.title)}</span>
          <span class="alb-count muted small">${a.ids.length} photo${a.ids.length === 1 ? '' : 's'}</span>
        </button>`).join('')}</div>`;
      bind();
      // Covers that the set does not name: the first photo's own image, fetched in one query -- AFTER the
      // cards are live, so a slow relay never leaves the grid ignoring taps for the length of a timeout.
      const need = albums.filter(a => !a.cover && a.ids.length);
      if(need.length){
        const photos = await readPhotos(need.map(a => a.ids[0]));
        if(gen !== _gen) return;
        for(const a of need){
          const src = imageOf(photos.get(a.ids[0]));
          const slot = el.querySelector(`.alb-card[data-d="${CSS.escape(a.d)}"] .alb-cover`);
          if(src && slot) slot.innerHTML = `<img src="${enc(src)}" alt="" loading="lazy">`;
        }
      }
      return;
    }
    bind();
    function bind(){
      const nb = el.querySelector('.alb-new'); if(nb) nb.onclick = () => albumForm(null, () => mount(el, pk));
      const rb = el.querySelector('.alb-retry'); if(rb) rb.onclick = () => mount(el, pk);
      $$('.alb-card', el).forEach(c => c.onclick = () => { const a = albums.find(x => x.d === c.dataset.d); if(a) openAlbum(el, pk, a); });
    }
  }

  async function openAlbum(el, pk, album){
    const gen = ++_gen, mine = isMine(pk);
    el.innerHTML = `<div class="alb-head">
        <button type="button" class="mini alb-back">${ic('arrow-left')}Albums</button>
        <div class="alb-head-text"><h3 class="alb-name">${enc(album.title)}</h3>
          ${album.description ? `<div class="alb-desc muted">${enc(album.description)}</div>` : ''}</div>
      </div>
      ${mine ? `<div class="alb-bar alb-bar-album">
        <button type="button" class="btn btn-neon alb-add">${ic('plus')}Add photos</button>
        <button type="button" class="btn btn-ghost alb-more" aria-label="Album options" title="Album options">${ic('menu')}</button>
        <input type="file" class="alb-file" accept="image/*" multiple hidden>
      </div>` : ''}
      <div class="alb-status muted small">${album.ids.length ? 'Loading photos…' : (mine ? 'No photos yet — tap Add photos.' : 'No photos in this album.')}</div>
      <div class="alb-photos"></div>`;
    el.querySelector('.alb-back').onclick = () => mount(el, pk);
    if(mine) wireOwner(el, pk, album);
    if(!album.ids.length) return;
    const photos = await readPhotos(album.ids);
    if(gen !== _gen || !el.isConnected) return;
    // Every photo the album lists gets a tile. One the relays could not find is a placeholder -- still
    // removable by its owner, or it would sit in the album for ever with nothing to click.
    const tiles = album.ids.map(id => ({ id, src: imageOf(photos.get(id)) }));
    const shown = tiles.filter(p => p.src);
    const missing = tiles.length - shown.length;
    const st = el.querySelector('.alb-status');
    if(st) st.textContent = missing ? `${missing} photo${missing === 1 ? '' : 's'} could not be found on the relays.` : '';
    const grid = el.querySelector('.alb-photos');
    grid.innerHTML = tiles.filter(p => p.src || mine).map(p => `<div class="alb-photo${p.src ? '' : ' alb-missing'}" data-id="${p.id}">
        ${p.src ? `<img src="${enc(p.src)}" alt="" loading="lazy">` : `<span class="alb-missing-note muted small">Not found</span>`}
        ${mine ? `<button type="button" class="alb-photo-menu" aria-label="Photo options" title="Photo options">${ic('menu')}</button>` : ''}
      </div>`).join('');
    const items = shown.map(p => ({ src: p.src, kind: 'image' }));
    $$('.alb-photo img', grid).forEach(img => img.onclick = () => {
      const id = img.closest('.alb-photo').dataset.id, i = shown.findIndex(p => p.id === id);
      openLightbox(items[i].src, 'image', { items, i });
    });
    if(mine) $$('.alb-photo-menu', grid).forEach(b => b.onclick = e => {
      e.stopPropagation();
      const id = b.closest('.alb-photo').dataset.id, t = tiles.find(p => p.id === id);
      photoMenu(el, pk, album, id, t ? t.src : '');
    });
  }

  function photoMenu(el, pk, album, id, src){
    modal(`<h3>Photo</h3><div class="alb-menu">
      ${src ? `<button type="button" class="btn btn-ghost alb-cover-set">${ic('star')}Use as the album cover</button>` : ''}
      <button type="button" class="btn btn-ghost alb-remove">${ic('close')}Remove from this album</button>
      <button type="button" class="btn btn-ghost alb-cancel">Cancel</button></div>`, root => {
      root.querySelector('.alb-cancel').onclick = () => closeModal();
      const cs = root.querySelector('.alb-cover-set'); if(cs) cs.onclick = async () => {
        closeModal();
        try{ const a = await editAlbum(album.d, tags => withTag(tags, 'image', src), { known: album.ev }); toast('cover set'); openAlbum(el, pk, a || album); }
        catch(e){ toast(e.message); }
      };
      root.querySelector('.alb-remove').onclick = async () => {
        closeModal();
        if(!await uiConfirm('Remove this photo from the album? The picture post itself stays on your profile.', { ok: 'Remove' })) return;
        try{ const a = await editAlbum(album.d, tags => withoutPhoto(tags, id, src), { known: album.ev });
             toast('removed'); openAlbum(el, pk, a || album); }
        catch(e){ toast(e.message); }
      };
    });
  }

  function wireOwner(el, pk, album){
    const input = el.querySelector('.alb-file');
    el.querySelector('.alb-add').onclick = () => modal(`<h3>Add photos</h3><div class="alb-menu">
        <button type="button" class="btn btn-neon alb-add-dev">${ic('camera')}From this device</button>
        <button type="button" class="btn btn-ghost alb-add-files">${ic('folder')}From Files</button>
        <button type="button" class="btn btn-ghost alb-cancel">Cancel</button></div>`, root => {
      root.querySelector('.alb-cancel').onclick = () => closeModal();
      root.querySelector('.alb-add-dev').onclick = () => { closeModal(); input.click(); };
      root.querySelector('.alb-add-files').onclick = () => { closeModal(); pickFromFiles(el, pk, album); };
    });
    el.querySelector('.alb-more').onclick = () => modal(`<h3>${enc(album.title)}</h3><div class="alb-menu">
        <button type="button" class="btn btn-ghost alb-edit">${ic('pen')}Rename or describe</button>
        <button type="button" class="btn btn-danger alb-del">${ic('trash')}Delete album</button>
        <button type="button" class="btn btn-ghost alb-cancel">Cancel</button></div>`, root => {
      root.querySelector('.alb-cancel').onclick = () => closeModal();
      root.querySelector('.alb-edit').onclick = () => { closeModal(); albumForm(album, a => openAlbum(el, pk, a || album)); };
      root.querySelector('.alb-del').onclick = () => { closeModal(); deleteAlbum(el, pk, album); };
    });
    input.onchange = async () => {
      const files = [...(input.files || [])].filter(f => /^image\//.test(f.type));
      input.value = '';
      if(!files.length) return;
      await addPhotos(el, pk, album, files.map(f => async () => {
        const url = await uploadBlob(f);
        const extra = ((await imetaTagsFor(url)) || [])[0];   // a lazy wrapper: may answer a promise
        return postPicture(url, { mime: f.type, extra });
      }));
    };
  }

  function pickFromFiles(el, pk, album){
    blossomPicker(null, pick => {
      if(!pick || !pick.url) return;
      if(pick.enc){ toast('That file is encrypted in your drive — others could not see it. Pick an unencrypted image.'); return; }
      if(!/^image\//.test(pick.type || '')){ toast('Only images can go in a photo album.'); return; }
      addPhotos(el, pk, album, [() => postPicture(pick.url, { mime: pick.type, sha: pick.sha })]);
    }, { title: '📁 Add a photo from Files' });
  }

  async function deleteAlbum(el, pk, album){
    if(!await uiConfirm(`Delete the album “${album.title}”? The photos stay on your profile; only the album goes.`, { ok: 'Delete album', danger: true })) return;
    const me = S.ME.pubkey;
    const tags = [['a', `${SET}:${me}:${album.d}`], ['k', String(SET)]];
    if(album.ev && album.ev.id) tags.unshift(['e', album.ev.id]);
    const r = await publish(5, 'album deleted', tags, { quiet: true });
    if(r && r.ok){ try{ Store.removeEvent(album.ev.id); }catch(_){} toast('album deleted'); mount(el, pk); }
    else toast('could not delete the album — try again');
  }

  /** Post each picture, then add every one that made it to the album in ONE edit. */
  async function addPhotos(el, pk, album, jobs){
    const ids = []; let failed = 0;
    const st = el.querySelector('.alb-status');
    for(let i = 0; i < jobs.length; i++){
      if(st) st.textContent = `Adding photo ${i + 1} of ${jobs.length}…`;
      try{ ids.push(await jobs[i]()); }catch(_){ failed++; }
    }
    if(!ids.length){ toast('no photos were added' + (failed ? ` (${failed} failed)` : '')); if(st) st.textContent = ''; return; }
    try{
      const a = await editAlbum(album.d, tags => withPhotos(tags, ids), { known: album.ev });
      toast(`${ids.length} photo${ids.length === 1 ? '' : 's'} added` + (failed ? `, ${failed} failed` : ''));
      openAlbum(el, pk, a || album);
    }catch(e){ toast(e.message); if(st) st.textContent = ''; }
  }

  /** Create (album null) or rename. `done(album)` after a successful save. */
  function albumForm(album, done){
    modal(`<h3>${album ? 'Edit album' : 'New album'}</h3>
      <form class="alb-form">
        <label>Name<input class="input alb-f-title" maxlength="120" required value="${enc(album ? album.title : '')}" placeholder="Summer 2026"></label>
        <label>Description<textarea class="input alb-f-desc" rows="3" maxlength="1000" placeholder="Optional">${enc(album ? album.description : '')}</textarea></label>
        <div class="alb-form-btns"><button type="button" class="btn btn-ghost alb-f-cancel">Cancel</button>
          <button type="submit" class="btn btn-neon">${album ? 'Save' : 'Create'}</button></div>
      </form>`, root => {
      const f = root.querySelector('.alb-form');
      root.querySelector('.alb-f-cancel').onclick = () => closeModal();
      setTimeout(() => { try{ root.querySelector('.alb-f-title').focus(); }catch(_){} }, 30);
      f.onsubmit = async e => {
        e.preventDefault();
        const title = root.querySelector('.alb-f-title').value.trim(), desc = root.querySelector('.alb-f-desc').value.trim();
        if(!title) return;
        const btn = f.querySelector('[type=submit]'); btn.disabled = true;
        try{
          const d = album ? album.d : newD();
          const a = await editAlbum(d, tags => withTag(withTag(tags, 'title', title), 'description', desc),
                                    album ? { known: album.ev } : { create: true });
          closeModal(); toast(album ? 'album saved' : 'album created'); done && done(a);
        }catch(err){ btn.disabled = false; toast(err.message); }
      };
    });
  }

  return { mount, openAlbum, albumOf, latest, withPhotos, withoutPhoto, withTag, picTags, imageOf, editAlbum };
};
