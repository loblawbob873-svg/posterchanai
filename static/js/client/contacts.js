/* #contacts — the Contacts screen, on top of this node's bundled CardDAV server.
 *
 * The calendar's shape, for the calendar's reasons (see calendar.js): its own file, driven from
 * renderView via window.PCContacts.render(), and everything server-side under /api/contacts/*, which
 * reads and writes the SAME encrypted Nostr events Radicale's storage plugin does — so this screen
 * and a synced phone are looking at one addressbook rather than two that drift.
 *
 * The list lives in MODULE state, not the DOM. #feed is shared by every view and app.js blanks it on
 * entry, so coming back repaints the same book, search and scroll position with no refetch.
 *
 * Reading and writing vCards is PCVcard's job (client/vcard.js), which is DOM-free and tested under
 * node. The rule it enforces and this file must not undermine: a contact is stored as its owner's
 * phone wrote it, and editing a phone number preserves the photo, the Apple-style grouped labels and
 * every X-* field this app has no UI for.
 */
(function(){
  const LETTERS = 'ABCDEFGHIJKLMNOPQRSTUVWXYZ#'.split('');

  const S = {
    ready:false, enabled:null,
    books:[], book:'',          // addressbooks, and the one being shown
    cards:{},                   // bookId -> [{uid, ics, …}] as stored
    q:'',                       // search
    loading:false, error:'',
    scroll:0,
  };

  function init(){
    const PC = window.__PC;
    if(!PC){ return setTimeout(init, 50); }
    const { $, $$, enc, toast, modal, closeModal, authFetch, ensureAiSession, uiConfirm } = PC;

    const inView = () => window.__PC.VIEW === 'contacts';
    const V = () => window.PCVcard;

    // ---- server ------------------------------------------------------------------------------
    async function api(path, opts){
      try{ await ensureAiSession(); }catch(_){}
      const r = await authFetch(path, opts);
      let body = null;
      try{ body = await r.json(); }catch(_){}
      if(!r.ok) throw new Error((body && (body.detail || body.error)) || ('HTTP ' + r.status));
      return body || {};
    }
    const jput = (p, o) => api(p, { method:'PUT', headers:{'Content-Type':'application/json'},
                                    body: JSON.stringify(o||{}) });
    const jpost = (p, o) => api(p, { method:'POST', headers:{'Content-Type':'application/json'},
                                     body: JSON.stringify(o||{}) });

    // ---- data --------------------------------------------------------------------------------
    /* Parsed cards for the shown book. Parsed ONCE per load rather than per keystroke: a 50-card
     * book is cheap, a 5000-card one is not, and search runs on every character. */
    let _parsed = [], _parsedSig = '';
    function cards(){
      const sig = S.book + '|' + S.rev;
      if(_parsedSig === sig) return _parsed;
      const out = [];
      for(const rec of (S.cards[S.book] || [])){
        try{
          const c = V().parse(rec.ics || '');
          c.uid = c.uid || rec.uid;         // a card with no UID of its own is addressed by the store's
          c._rec = rec;
          out.push(c);
        }catch(err){ /* one unreadable card must not blank the list */ }
      }
      out.sort((a, b) => V().sortKey(a).localeCompare(V().sortKey(b)));
      _parsed = out; _parsedSig = sig;
      return out;
    }
    const shown = () => cards().filter(c => V().matches(c, S.q));

    const initials = c => {
      const n = c.n || {};
      const a = (n.given || c.fn || '').trim()[0] || '';
      const b = (n.family || '').trim()[0] || '';
      return ((a + b) || (c.fn || '?').trim()[0] || '?').toUpperCase();
    };
    const bucket = c => {
      const k = V().sortKey(c).trim()[0];
      return (k && k >= 'a' && k <= 'z') ? k.toUpperCase() : '#';
    };
    const subtitle = c => (c.tels[0] && c.tels[0].value) || (c.emails[0] && c.emails[0].value)
                        || c.org || '';

    // ---- load --------------------------------------------------------------------------------
    async function load(){
      S.loading = true; S.error = '';
      paint();
      try{
        const r = await api('/api/contacts/books');
        S.books = r.books || [];
        if(!S.book || !S.books.some(b => b.id === S.book)) S.book = (S.books[0] || {}).id || '';
        S.enabled = true;
        const got = {};
        for(const b of S.books){
          try{ got[b.id] = (await api('/api/contacts/cards?book=' + encodeURIComponent(b.id))).cards || []; }
          catch(_){ got[b.id] = []; }
        }
        S.cards = got;
        S.rev = (S.rev || 0) + 1;
      }catch(e){
        const msg = (e && e.message) || '';
        // 404 is the server being off, which is a state to explain rather than an error to report.
        if(/off on this node/i.test(msg)) S.enabled = false;
        else S.error = msg || 'could not load your contacts';
      }finally{
        S.loading = false; S.ready = true; paint();
      }
    }

    // ---- rendering ---------------------------------------------------------------------------
    function head(){
      const picker = S.books.length > 1 ? `<select class="input ct-pick" id="ct-pick">
          ${S.books.map(b => `<option value="${enc(b.id)}"${b.id===S.book?' selected':''}>${enc(b.displayname || b.id)}</option>`).join('')}
        </select>` : '';
      const n = shown().length, total = cards().length;
      return `<div class="ct-bar">
        <div class="ct-search">
          <svg class="ic" aria-hidden="true"><use href="#i-search"></use></svg>
          <input class="input" id="ct-q" type="search" placeholder="Search contacts"
                 value="${enc(S.q)}" autocomplete="off">
        </div>
        <div class="ct-tools">
          ${picker}
          <button class="btn btn-cyan small" id="ct-new"><svg class="ic b-ic" aria-hidden="true"><use href="#i-plus"></use></svg>Contact</button>
          <button class="btn btn-ghost small" id="ct-menu" aria-label="Contact options"><svg class="ic b-ic" aria-hidden="true"><use href="#i-menu"></use></svg></button>
        </div>
        <div class="ct-count muted small">${S.q ? `${n} of ${total}` : `${total} contact${total===1?'':'s'}`}</div>
      </div>`;
    }

    function row(c){
      const face = c.photo
        ? `<img class="ct-face" src="${enc(c.photo)}" alt="" loading="lazy" decoding="async">`
        : `<i class="ct-face ct-init">${enc(initials(c))}</i>`;
      const sub = subtitle(c);
      return `<button class="ct-row" data-uid="${enc(c.uid)}">
          ${face}
          <span class="ct-body">
            <span class="ct-name">${enc(V().displayName(c))}</span>
            ${sub ? `<span class="ct-sub">${enc(sub)}</span>` : ''}
          </span>
        </button>`;
    }

    function list(){
      const items = shown();
      if(!S.books.length){
        return `<div class="ct-empty">
          <div class="ct-emptyhd">No addressbook yet</div>
          <p class="muted">Make one, or import a <b>.vcf</b> from your phone or your old server.</p>
          <div class="row" style="gap:8px;justify-content:center;flex-wrap:wrap">
            <button class="btn btn-cyan small" id="ct-mkbook">New addressbook</button>
            <button class="btn btn-ghost small" id="ct-import2">Import .vcf</button>
          </div></div>`;
      }
      if(!items.length){
        return `<div class="empty">${S.q ? 'No contact matches that.' : 'No contacts in this book yet.'}</div>`;
      }
      let out = '', letter = '';
      for(const c of items){
        const b = bucket(c);
        if(b !== letter){ letter = b; out += `<div class="ct-letter" id="ct-l-${enc(b)}">${enc(b)}</div>`; }
        out += row(c);
      }
      // The A–Z rail is a jump list, not decoration: it is the only way to reach the middle of a
      // long book on a phone without flinging the list.
      const have = new Set(items.map(bucket));
      const rail = LETTERS.map(l => have.has(l)
        ? `<button class="ct-jump" data-l="${enc(l)}">${enc(l)}</button>`
        : `<span class="ct-jump off">${enc(l)}</span>`).join('');
      return `<div class="ct-listwrap"><div class="ct-list">${out}</div>
                <div class="ct-rail">${rail}</div></div>`;
    }

    function offScreen(){
      if(S.enabled === false){
        return `<div class="cal-off">
          <div class="cal-offhd">Contacts are off on this node</div>
          <p class="muted">They ride the same switch as the calendar: an admin turns it on in
             <b>Admin → Tools → Calendar server</b>. It runs inside this app — there is nothing to
             install — and your contacts are stored as encrypted Nostr events.</p></div>`;
      }
      return `<div class="ws-err">${enc(S.error || 'could not load your contacts')}</div>`;
    }

    function paint(){
      const feed = $('#feed'); if(!feed) return;
      if(S.loading && !S.ready){ feed.innerHTML = '<div class="ct-wrap"><div class="spinner"></div></div>'; return; }
      if(S.enabled === false || S.error){ feed.innerHTML = `<div class="ct-wrap">${offScreen()}</div>`; return; }
      feed.innerHTML = `<div class="ct-wrap">${head()}${list()}</div>`;
      wire(feed);
      const s = $('#feed');
      if(s) requestAnimationFrame(()=>{ try{ s.scrollTop = S.scroll || 0; }catch(_){} });
    }

    function wire(root){
      const on = (sel, fn) => { const el = $(sel, root); if(el) el.onclick = fn; };
      on('#ct-new', ()=> editCard(null));
      on('#ct-menu', openMenu);
      on('#ct-mkbook', ()=> makeBook());
      on('#ct-import2', ()=> openMenu());
      const pick = $('#ct-pick', root);
      if(pick) pick.onchange = ()=>{ S.book = pick.value; S.q = ''; paint(); };
      const q = $('#ct-q', root);
      if(q){
        // Repaint only the list: replacing the whole screen on every keystroke would blur the input
        // and lose the caret.
        q.oninput = ()=>{
          S.q = q.value;
          const wrap = $('.ct-listwrap', root) || $('.empty', root) || $('.ct-empty', root);
          const holder = $('.ct-wrap', root);
          if(!holder) return;
          const fresh = document.createElement('div');
          fresh.innerHTML = list();
          if(wrap) wrap.replaceWith(fresh.firstElementChild);
          else holder.appendChild(fresh.firstElementChild);
          const c = $('.ct-count', root);
          if(c) c.textContent = S.q ? `${shown().length} of ${cards().length}`
                                    : `${cards().length} contact${cards().length===1?'':'s'}`;
          wireList(root);
        };
      }
      wireList(root);
      const sc = $('#feed');
      if(sc) sc.onscroll = ()=>{ if(inView()) S.scroll = sc.scrollTop; };
    }

    function wireList(root){
      $$('.ct-row', root).forEach(b => b.onclick = ()=>{
        const c = cards().find(x => x.uid === b.dataset.uid);
        if(c) editCard(c);
      });
      $$('.ct-jump', root).forEach(b => b.onclick = ()=>{
        const el = $('#ct-l-' + b.dataset.l, root);
        if(el) el.scrollIntoView({ block:'start' });
      });
    }

    // ---- editor ------------------------------------------------------------------------------
    function fieldRows(kind, rows, ph){
      return (rows.length ? rows : [{ type:'', value:'' }]).map((r, i) => `
        <div class="ct-multi" data-kind="${kind}" data-i="${i}">
          <input class="input ct-mv" value="${enc(r.value||'')}" placeholder="${enc(ph)}">
          <input class="input ct-mt" value="${enc(r.type||'')}" placeholder="label">
          <button class="btn btn-ghost small ct-mdel" aria-label="Remove">✕</button>
        </div>`).join('');
    }

    function editCard(card){
      const isNew = !card;
      if(!S.book){ makeBook(); return; }
      const c = card ? JSON.parse(JSON.stringify(card)) : V().blank();
      if(card) c.other = card.other || [];        // JSON round trip keeps it, but be explicit
      const n = c.n || { family:'', given:'', middle:'', prefix:'', suffix:'' };
      const a = (c.adrs && c.adrs[0]) || { street:'', city:'', region:'', code:'', country:'' };
      modal(`<h3>${isNew ? 'New contact' : enc(V().displayName(c))}</h3>
        <div class="ct-form">
          ${c.photo ? `<img class="ct-bigface" src="${enc(c.photo)}" alt="">` : ''}
          <div class="ct-names">
            <label class="fld">First<input class="input" id="cc-given" value="${enc(n.given)}"></label>
            <label class="fld">Last<input class="input" id="cc-family" value="${enc(n.family)}"></label>
          </div>
          <div class="ct-group"><div class="ct-glabel">Phone</div>
            <div id="cc-tels">${fieldRows('tel', c.tels || [], 'Phone number')}</div>
            <button class="btn btn-ghost small ct-add" data-kind="tel">＋ Add phone</button></div>
          <div class="ct-group"><div class="ct-glabel">Email</div>
            <div id="cc-emails">${fieldRows('email', c.emails || [], 'Email address')}</div>
            <button class="btn btn-ghost small ct-add" data-kind="email">＋ Add email</button></div>
          <label class="fld">Company<input class="input" id="cc-org" value="${enc(c.org||'')}"></label>
          <label class="fld">Address<input class="input" id="cc-street" value="${enc(a.street||'')}" placeholder="Street"></label>
          <div class="ct-names">
            <label class="fld">City<input class="input" id="cc-city" value="${enc(a.city||'')}"></label>
            <label class="fld">Postcode<input class="input" id="cc-code" value="${enc(a.code||'')}"></label>
          </div>
          <label class="fld">Birthday <span class="muted small">(YYYY-MM-DD)</span><input class="input" id="cc-bday" value="${enc(c.bday||'')}"></label>
          <label class="fld">Notes<textarea class="input" id="cc-note" rows="3">${enc(c.note||'')}</textarea></label>
          ${(c.other && c.other.length) ? `<div class="muted small">Keeps ${c.other.length} field${c.other.length===1?'':'s'} this app doesn't edit (photo, labels, app data).</div>` : ''}
        </div>
        <div class="row" style="margin-top:14px">
          <button class="btn btn-cyan" id="cc-save">Save</button>
          ${isNew ? '' : '<button class="btn btn-ghost" id="cc-del">Delete</button>'}
        </div>`, root => {
        const rewire = () => {
          $$('.ct-mdel', root).forEach(b => b.onclick = ()=>{
            const row = b.closest('.ct-multi');
            if(row && row.parentElement.children.length > 1) row.remove();
            else if(row) { const v = $('.ct-mv', row); if(v) v.value = ''; }
          });
          $$('.ct-add', root).forEach(b => b.onclick = ()=>{
            const box = $(b.dataset.kind === 'tel' ? '#cc-tels' : '#cc-emails', root);
            const d = document.createElement('div');
            d.innerHTML = fieldRows(b.dataset.kind, [{type:'',value:''}],
                                    b.dataset.kind === 'tel' ? 'Phone number' : 'Email address');
            box.appendChild(d.firstElementChild);
            rewire();
          });
        };
        rewire();
        const collect = (sel, orig) => $$(sel + ' .ct-multi', root).map((row, i) => ({
          value: ($('.ct-mv', row) || {}).value.trim(),
          type: ($('.ct-mt', row) || {}).value.trim(),
          // The GROUP prefix belongs to the original property; keeping it by position means an
          // Apple-style `item1.EMAIL` stays paired with the `item1.X-ABLABEL` that names it.
          group: (orig[i] || {}).group || '',
        })).filter(x => x.value);

        $('#cc-save', root).onclick = async ()=>{
          c.n = { family: $('#cc-family', root).value.trim(), given: $('#cc-given', root).value.trim(),
                  middle: n.middle || '', prefix: n.prefix || '', suffix: n.suffix || '' };
          c.fn = [c.n.given, c.n.family].filter(Boolean).join(' ');
          c.tels = collect('#cc-tels', c.tels || []);
          c.emails = collect('#cc-emails', c.emails || []);
          c.org = $('#cc-org', root).value.trim();
          c.bday = $('#cc-bday', root).value.trim();
          c.note = $('#cc-note', root).value.trim();
          const street = $('#cc-street', root).value.trim(), city = $('#cc-city', root).value.trim(),
                code = $('#cc-code', root).value.trim();
          if(street || city || code){
            c.adrs = [Object.assign({ po:'', ext:'', region:'', country:'' }, a,
                                    { street, city, code })];
          }else if(!(c.adrs || []).length){
            c.adrs = [];
          }
          if(!c.fn && !c.tels.length && !c.emails.length && !c.org){
            toast('give them a name, a number or an email'); return;
          }
          try{
            await jput('/api/contacts/cards',
                       { book: S.book, uid: c.uid, vcf: V().serialize(c) });
            closeModal(); toast('saved'); await load();
          }catch(err){ toast('could not save: ' + ((err && err.message) || 'error')); }
        };
        const del = $('#cc-del', root);
        if(del) del.onclick = async ()=>{
          if(!(await uiConfirm('Delete this contact?'))) return;
          try{
            await api(`/api/contacts/cards?book=${encodeURIComponent(S.book)}&uid=${encodeURIComponent(c.uid)}`,
                      { method:'DELETE' });
            closeModal(); toast('deleted'); await load();
          }catch(err){ toast('could not delete: ' + ((err && err.message) || 'error')); }
        };
      });
    }

    // ---- books, import/export ------------------------------------------------------------------
    async function makeBook(){
      modal(`<h3>New addressbook</h3>
        <label class="fld">Name<input class="input" id="ctb-name" value="Contacts"></label>
        <div class="row" style="margin-top:14px"><button class="btn btn-cyan" id="ctb-save">Create</button></div>`,
        root => {
          $('#ctb-save', root).onclick = async ()=>{
            const name = $('#ctb-name', root).value.trim() || 'Contacts';
            try{
              const b = await jpost('/api/contacts/books', { name });
              closeModal(); S.book = b.id; await load();
            }catch(err){ toast('could not create: ' + ((err && err.message) || 'error')); }
          };
        });
    }

    function openMenu(){
      modal(`<h3>Addressbooks</h3>
        <div class="cal-list">${S.books.map(b => `<div class="cal-row">
            <span class="cal-name">${enc(b.displayname || b.id)}</span>
            <a class="btn btn-ghost small" href="/api/contacts/export?book=${encodeURIComponent(b.id)}"
               download><svg class="ic b-ic" aria-hidden="true"><use href="#i-download"></use></svg>Export</a>
            <button class="btn btn-ghost small ctb-del" data-id="${enc(b.id)}">Delete</button>
          </div>`).join('') || '<div class="empty">No addressbooks yet.</div>'}</div>
        <div class="row" style="margin-top:14px;flex-wrap:wrap;gap:8px">
          <button class="btn btn-cyan small" id="ctb-add"><svg class="ic b-ic" aria-hidden="true"><use href="#i-plus"></use></svg>New addressbook</button>
          <button class="btn btn-ghost small" id="ctb-import"><svg class="ic b-ic" aria-hidden="true"><use href="#i-upload"></use></svg>Import .vcf</button>
          <button class="btn btn-ghost small" id="ctb-phone"><svg class="ic b-ic" aria-hidden="true"><use href="#i-android"></use></svg>Sync to a device</button>
        </div>
        <input type="file" id="ctb-file" accept=".vcf,text/vcard" hidden>`, root => {
        $('#ctb-add', root).onclick = ()=>{ closeModal(); makeBook(); };
        $('#ctb-import', root).onclick = ()=> $('#ctb-file', root).click();
        $('#ctb-phone', root).onclick = ()=>{
          closeModal();
          // One CalDAV/CardDAV identity per user: the calendar screen owns that panel.
          if(window.PCCalendar && window.PCCalendar.phonePanel) window.PCCalendar.phonePanel();
          else if(window.__PC.switchView) window.__PC.switchView('calendar');
        };
        $('#ctb-file', root).onchange = async (e)=>{
          const f = e.target.files && e.target.files[0]; if(!f) return;
          const fd = new FormData(); fd.append('file', f);
          try{
            await ensureAiSession();
            const target = S.book || f.name.replace(/\.vcf$/i, '');
            const r = await authFetch('/api/contacts/import?book=' + encodeURIComponent(target),
                                      { method:'POST', body: fd }).then(r => r.json());
            if(r && r.detail) throw new Error(r.detail);
            closeModal();
            toast(`imported ${r.imported || 0} contact${(r.imported||0)===1?'':'s'}`
                  + (r.skipped ? ` (${r.skipped} skipped)` : ''));
            if(r.book) S.book = r.book;
            await load();
          }catch(err){ toast('import failed: ' + ((err && err.message) || 'error')); }
        };
        $$('.ctb-del', root).forEach(b => b.onclick = async ()=>{
          if(!(await uiConfirm('Delete this addressbook and every contact in it?'))) return;
          try{
            await api('/api/contacts/books/' + encodeURIComponent(b.dataset.id), { method:'DELETE' });
            closeModal(); if(S.book === b.dataset.id) S.book = ''; await load();
          }catch(err){ toast('could not delete: ' + ((err && err.message) || 'error')); }
        });
      });
    }

    // ---- entry -------------------------------------------------------------------------------
    window.PCContacts = {
      render(){
        paint();
        if(!S.ready && !S.loading) load();
      },
      reload: load,
    };
  }

  init();
})();
