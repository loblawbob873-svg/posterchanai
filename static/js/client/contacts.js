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
      // Keep the phone's own Contacts app in step. A no-op on every platform but Android and for
      // everybody who has not turned it on, and a no-op again when nothing has changed since the
      // last push — so it can sit on the end of every load without being thought about.
      try{ pushPhonebook(); }catch(_){}
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
      /* NEVER DRAW INTO A VIEW WE NO LONGER OWN. `#feed` is shared by every screen, and paint() is
       * called from the END of async work — a load, a phone-book sweep — which can easily finish
       * after the user has gone somewhere else. Without this guard a slow contacts load repaints
       * over whatever they are reading now, and the reverse (the calendar's load landing on top of
       * this screen) is what made a full address book look empty after one tap on "Sync to a
       * device". Every other async view here already does this; these two did not. */
      if(!inView()) return;
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

    // ---- this phone's own Contacts app (Android only) ------------------------------------------
    /* THE PHONE BOOK. Android can show these people in the dialer, the share sheet and every
     * messaging app — but only if they are in ContactsContract, which is a native database no
     * WebView can reach. So the client hands the already-decrypted cards to a Capacitor plugin
     * (place.poster.app.contacts.ContactSyncPlugin) and that writes them.
     *
     * IT HAS TO BE DRIVEN FROM HERE. Native Java has no session, no storage key and no way to ask
     * for one, and Android's own sync scheduler runs when the app is closed — which is exactly when
     * nothing on this device can read a card. A push therefore happens when the app is open, and
     * that is the whole schedule.
     *
     * ONE WAY: app → phone. Edits made in the phone's Contacts app are not read back and are
     * replaced by the next push. CardDAV (Calendar → Sync to a device) is still the two-way path and
     * is untouched by any of this.
     *
     * OFF BY DEFAULT and per-device: this writes into somebody's phone book, so it is opt-in, and
     * the switch lives beside the addressbook list (⋯ → Addressbooks). */
    const PHONE_KEY = 'androidPhonebook';
    const CSet = () => window.ClientSettings || { get:(k,d)=>d, set(){} };
    const nativeSync = (m) => (PC.capPlugin ? PC.capPlugin('ContactSync', m || 'begin') : null);
    const phonebookOn = () => !!CSet().get(PHONE_KEY, false);
    const owner = () => { try{ const me = PC.me ? PC.me() : PC.ME; return (me && me.pubkey) || ''; }
                          catch(_){ return ''; } };

    /* FNV-1a. Not a checksum for anybody else — it only has to change when the card does. */
    function hash(s){
      let h = 0x811c9dc5;
      for(let i = 0; i < s.length; i++){ h ^= s.charCodeAt(i); h = (h * 0x01000193) >>> 0; }
      return h.toString(16);
    }

    /* Every card in every book. The phone book is one list — a person is not filed under whichever
     * addressbook happens to be on screen. Memoised on S.rev so a repaint costs nothing. */
    let _all = [], _allRev = -1;
    function everyCard(){
      if(_allRev === S.rev) return _all;
      const out = [], seen = new Set();
      for(const b of S.books){
        for(const rec of (S.cards[b.id] || [])){
          try{
            const c = V().parse(rec.ics || '');
            c.uid = c.uid || rec.uid;
            if(!c.uid || seen.has(c.uid)) continue;   // one UID = one person, whatever book it is in
            seen.add(c.uid);
            out.push(c);
          }catch(_){ /* one unreadable card must not empty the phone book */ }
        }
      }
      _all = out; _allRev = S.rev;
      return out;
    }

    /* A card as the plugin wants it. `photo` is raw base64 — a data: URI would be decoded in Java
     * for no reason, and an http(s) PHOTO is a URL we deliberately do NOT fetch (a phone book sync
     * is not a licence to make requests to whatever host is written in somebody else's vCard). */
    const PHOTO_MAX = 2 * 1024 * 1024;    // base64 chars; beyond this it is not a contact thumbnail
    function nativeCard(c){
      const n = c.n || {};
      let photo = '';
      const p = String(c.photo || '');
      if(p.slice(0, 5) === 'data:'){
        const i = p.indexOf(',');
        if(i > 0 && p.length - i <= PHOTO_MAX) photo = p.slice(i + 1);
      }
      const a = (c.adrs || [])[0] || null;
      const out = {
        uid: c.uid,
        fn: V().displayName(c),
        given: n.given || '', family: n.family || '',
        org: c.org || '', title: c.title || '', note: c.note || '', bday: c.bday || '',
        tels: (c.tels || []).filter(t => t.value).map(t => ({ type: t.type || '', value: t.value })),
        emails: (c.emails || []).filter(e => e.value).map(e => ({ type: e.type || '', value: e.value })),
        adr: a ? { street: a.street||'', city: a.city||'', region: a.region||'',
                   code: a.code||'', country: a.country||'' } : null,
        photo,
      };
      /* The photo is hashed by LENGTH plus its ends, not by its bytes: a book of 500 faces is tens
       * of megabytes of base64, and re-hashing all of it on every repaint to answer "did anything
       * change" costs more than the push it is meant to avoid. Two different JPEGs of exactly the
       * same length sharing both ends is not a case that happens. */
      const forHash = Object.assign({}, out, {
        photo: photo ? (photo.length + ':' + photo.slice(0, 32) + photo.slice(-32)) : '',
      });
      out.h = hash(JSON.stringify(forHash));
      return out;
    }

    /* Push what changed. Cheap by construction: `begin()` returns the hash of every card already on
     * the phone, so a visit that changed nothing sends nothing, and the first push after adding one
     * person sends one person. */
    const PUT_BUDGET = 1200000;           // bytes of JSON per bridge call — photos are the bulk
    let _pushSig = '', _pushing = null;
    async function pushPhonebook(force){
      const P = nativeSync('begin');
      if(!P || !phonebookOn()) return;
      if(_pushing) return _pushing;       // a sweep is a sweep; two at once would fight over uids
      const list = everyCard().map(nativeCard);
      const sig = owner() + '|' + list.map(c => c.uid + ':' + c.h).join(',');
      if(!force && sig === _pushSig) return;
      _pushing = (async () => {
        let st = null;
        try{ st = await P.begin({ owner: owner() }); }catch(_){ return; }
        if(st && st.granted === false){
          // Revoked in Android's settings after the switch was turned on. Turning it back off is the
          // honest answer — a switch that says "on" while nothing is written is the worse one.
          CSet().set(PHONE_KEY, false); _pushSig = '';
          toast('Android has revoked access to your contacts — phone sync turned off');
          return;
        }
        const known = (st && st.hashes) || {};
        let batch = [], size = 0;
        const flush = async () => {
          if(!batch.length) return;
          const cards = batch; batch = []; size = 0;
          await P.put({ cards });
        };
        for(const c of list){
          if(known[c.uid] === c.h) continue;
          batch.push(c);
          size += (c.photo ? c.photo.length : 0) + 400;
          if(size >= PUT_BUDGET) await flush();
        }
        await flush();
        // ALWAYS, even when nothing was written: this is the half that deletes. Somebody removed in
        // the web UI is only removed from the phone here.
        await P.commit({ uids: list.map(c => c.uid) });
        _pushSig = sig;
      })().catch(()=>{}).finally(()=>{ _pushing = null; });
      return _pushing;
    }

    /* The switch, in ⋯ → Addressbooks. The explanation sits beside it BEFORE it is flipped, because
     * the Android permission prompt itself says only "access your contacts" — the reason has to be
     * on screen already or the prompt is a coin toss. */
    function phonebookRow(st){
      if(!nativeSync('begin')) return '';       // not the packaged Android app
      const on = phonebookOn();
      const n = (st && st.count) || 0;
      return `<div class="cal-row ct-phonebook" style="flex-wrap:wrap">
        <label class="row" style="gap:8px;align-items:center;flex:1 1 100%">
          <input type="checkbox" id="ctb-phonebook"${on ? ' checked' : ''}>
          <span class="cal-name">Show these contacts in this phone's Contacts app</span>
        </label>
        <p class="muted small" style="flex:1 1 100%;margin:4px 0 0">
          Copies your address book into the phone itself, so these people appear in the dialer, in
          messaging apps and in the share sheet — no other app needed. Android will ask for
          permission to your contacts; it is used to write this account's cards and to tidy up the
          ones it wrote before. <b>One way:</b> changes made in the phone's Contacts app are replaced
          from here. Everything is removed when you sign out or turn this off.
          ${on ? `<br><b>${n}</b> contact${n === 1 ? '' : 's'} on this phone.` : ''}</p>
      </div>`;
    }

    function wirePhonebook(root){
      const box = $('#ctb-phonebook', root);
      if(!box) return;
      box.onchange = async () => {
        const P = nativeSync('enable');
        if(!P){ box.checked = false; return; }
        if(box.checked){
          let r = null;
          try{ r = await P.enable(); }catch(_){ r = null; }
          if(!r || !r.granted){
            // A refusal must break nothing: put the switch back and say what happened.
            box.checked = false; CSet().set(PHONE_KEY, false);
            toast('Android didn’t allow access to your contacts — nothing was changed');
            return;
          }
          CSet().set(PHONE_KEY, true);
          toast('adding your contacts to this phone…');
          _pushSig = '';
          await pushPhonebook(true);
          toast('done — look in the phone’s Contacts app');
        }else{
          CSet().set(PHONE_KEY, false);
          _pushSig = '';
          try{ await P.disable(); }catch(_){}
          toast('removed from this phone');
        }
      };
    }

    /* ---- CardDAV: "Sync to a device", on THIS screen -------------------------------------------
     *
     * IT USED TO BORROW THE CALENDAR'S, and that was wrong twice over. The panel lives inside
     * calendar.js and is not exported (`window.PCCalendar` is `{render, reload, widgetTick}`), so
     * the call fell through to the fallback and SWITCHED THE VIEW — a tap labelled "Sync to a
     * device" on the Contacts screen navigated to the calendar and left the address book behind.
     * And even reached, it hands out the CALENDAR's details under a Contacts heading, which is the
     * worse failure: the URL works, the phone accepts it, and the address book comes up empty with
     * nothing to say why.
     *
     * ONE IDENTITY, TWO COLLECTION TYPES. The account, the password and the base URL are shared with
     * the calendar by design (docs/CONTACTS.md), so the password endpoints are the calendar's — but
     * the URL a client needs for an ADDRESSBOOK is that book's own collection, and that is what this
     * shows. */
    async function cardDavPanel(){
      let cfg = {};
      try{ cfg = await api('/api/calendar/config'); }catch(_){ cfg = {}; }
      const base = String(cfg.url || '');
      /* Styled inline on purpose: client.css is a shared file and this is two rules. */
      const books = S.books.map(b => `<div class="ct-davrow" style="margin:6px 0">
          <span class="cal-name" style="display:block;margin-bottom:2px">${enc(b.displayname || b.id)}</span>
          <input class="input" style="width:100%" value="${enc(base + encodeURIComponent(b.id) + '/')}"
                 readonly aria-label="Address book URL for ${enc(b.displayname || b.id)}">
        </div>`).join('');
      const native = !!nativeSync('begin');
      modal(`<h3>Sync your contacts to a device</h3>
        <p class="muted small">Add a <b>CardDAV account</b> in a contacts app that speaks it —
           DAVx⁵ on Android, the built-in accounts on iOS and macOS, Thunderbird on the desktop. It
           syncs <b>both ways</b>: a contact added on the device appears here.</p>
        <label class="fld">Server <span class="muted small">(most apps discover everything from
          this)</span><input class="input" id="ctd-url" value="${enc(base)}" readonly></label>
        <label class="fld">Username<input class="input" id="ctd-user" value="${enc(cfg.username||'')}" readonly></label>
        <div class="fld"><b>Address book URLs</b>
          <p class="muted small">For an app that asks for the collection itself rather than
             discovering it. <b>These are the contacts ones</b> — a calendar URL pasted here gives you
             an empty address book and no error.</p>
          ${books || '<div class="empty">No addressbooks yet.</div>'}</div>
        <div class="fld"><b>Password</b>
          <p class="muted small">A sync-only app password, separate from your login and <b>shared with
             the calendar</b> — one account per person, not two. Shown once; generating a new one
             immediately stops every device using the old one.</p>
          <div class="row"><button class="btn btn-cyan small" id="ctd-gen">
            ${cfg.has_password ? 'Generate a new password' : 'Generate password'}</button>
            ${cfg.has_password ? '<button class="btn btn-ghost small" id="ctd-clear">Revoke</button>' : ''}</div>
          <div id="ctd-out"></div>
        </div>
        ${native ? `<p class="muted small"><b>On this Android phone you do not need any of this.</b>
           “Show these contacts in this phone's Contacts app”, in the Addressbooks panel, puts them in
           the dialer with nothing else installed. CardDAV is the route for your <i>other</i> devices —
           a desktop, an iPhone — and for keeping them in sync when this app is closed.</p>` : ''}`,
      root => {
        $$('#ctd-url, #ctd-user, .ct-davrow input', root).forEach(i => i.onfocus = ()=> i.select());
        $('#ctd-gen', root).onclick = async ()=>{
          try{
            const r = await jpost('/api/calendar/password');
            $('#ctd-out', root).innerHTML =
              `<div class="cal-pw"><code>${enc(r.password)}</code>
                 <button class="btn btn-ghost small" id="ctd-copy">Copy</button></div>
               <p class="muted small">Copy it now — it is stored only as a hash and cannot be shown again.</p>`;
            const cp = $('#ctd-copy', root);
            if(cp) cp.onclick = ()=>{ try{ navigator.clipboard.writeText(r.password); toast('copied'); }catch(_){ } };
          }catch(err){ toast('could not generate: ' + ((err && err.message) || 'error')); }
        };
        const cl = $('#ctd-clear', root);
        if(cl) cl.onclick = async ()=>{
          if(!(await uiConfirm('Revoke the app password? Every synced device stops — calendars too.'))) return;
          try{ await api('/api/calendar/password', { method:'DELETE' });
               closeModal(); toast('revoked'); }
          catch(err){ toast('could not revoke: ' + ((err && err.message) || 'error')); }
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

    async function openMenu(){
      // The phone-book row wants to say how many are on the device. One cheap native call, and only
      // in the packaged app with the switch already on.
      let st = null;
      const SP = nativeSync('status');
      if(SP && phonebookOn()){ try{ st = await SP.status(); }catch(_){} }
      modal(`<h3>Addressbooks</h3>
        <div class="cal-list">${S.books.map(b => `<div class="cal-row">
            <span class="cal-name">${enc(b.displayname || b.id)}</span>
            <a class="btn btn-ghost small" href="/api/contacts/export?book=${encodeURIComponent(b.id)}"
               download><svg class="ic b-ic" aria-hidden="true"><use href="#i-download"></use></svg>Export</a>
            <button class="btn btn-ghost small ctb-del" data-id="${enc(b.id)}">Delete</button>
          </div>`).join('') || '<div class="empty">No addressbooks yet.</div>'}
          ${phonebookRow(st)}</div>
        <div class="row" style="margin-top:14px;flex-wrap:wrap;gap:8px">
          <button class="btn btn-cyan small" id="ctb-add"><svg class="ic b-ic" aria-hidden="true"><use href="#i-plus"></use></svg>New addressbook</button>
          <button class="btn btn-ghost small" id="ctb-import"><svg class="ic b-ic" aria-hidden="true"><use href="#i-upload"></use></svg>Import .vcf</button>
          <button class="btn btn-ghost small" id="ctb-phone"><svg class="ic b-ic" aria-hidden="true"><use href="#i-android"></use></svg>Sync to a device</button>
        </div>
        <input type="file" id="ctb-file" accept=".vcf,text/vcard" hidden>`, root => {
        wirePhonebook(root);
        $('#ctb-add', root).onclick = ()=>{ closeModal(); makeBook(); };
        $('#ctb-import', root).onclick = ()=> $('#ctb-file', root).click();
        // The CardDAV details for THIS screen's collections, and it stays on this screen — see
        // cardDavPanel. Borrowing the calendar's used to navigate away from the address book.
        $('#ctb-phone', root).onclick = ()=>{ closeModal(); cardDavPanel(); };
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
      /* KEEP THE PHONE BOOK FED WITHOUT ANYBODY OPENING CONTACTS.
       *
       * pushPhonebook runs at the end of load(), and load() only runs when this screen is rendered —
       * so somebody who edits contacts on a laptop and never opens the screen on their phone would
       * have a phone book that was filled once and never again. Called from app.js a few seconds
       * after start, and it costs nothing at all unless this is the packaged Android app AND the
       * switch is on: only then does it fetch the books. */
      async syncTick(){
        if(!nativeSync('begin') || !phonebookOn()) return;
        if(!S.ready) return load();       // load() pushes at its end
        return pushPhonebook();
      },
      /* Sign-out and account switch. The phone's copy must not outlive the session that could read
       * it — a handed-down phone would otherwise keep the previous user's people in its dialer and
       * in every share sheet. Removing the ACCOUNT takes every card with it, so there is no sweep
       * here to half-finish. `begin()`'s owner check is the second line of defence for the app that
       * was killed before this could run. */
      forgetDevice(){
        _pushSig = '';
        const P = nativeSync('disable');
        if(!P) return Promise.resolve();
        try{ return Promise.resolve(P.disable()).catch(()=>{}); }catch(_){ return Promise.resolve(); }
      },
    };
  }

  init();
})();
