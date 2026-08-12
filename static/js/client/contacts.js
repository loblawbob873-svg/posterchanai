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
    // Has a load ever COMPLETED? `books`/`cards` are only assigned on success, so before the first
    // one they are empty — which is indistinguishable from "this account has no contacts" to
    // anything downstream, and the phone-book sweep DELETES what it is not told to keep. Nothing
    // may reconcile against state no load ever produced. See the collapse guard below.
    loadedOk:false,
    // Did the LAST load fetch every book? Separate from loadedOk, which is about history: after one
    // good load, loadedOk is true for ever and cannot say that THIS load came back short.
    partial:false,
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
        /* A BOOK THAT DID NOT LOAD IS NOT A BOOK WITH NO CONTACTS IN IT.
         *
         * This swallowed a per-book failure into `[]` and then set `loadedOk` anyway, so a load that
         * fetched two books out of three looked exactly like one where a third of the address book
         * had been deleted — and the phone-book reconcile, which decides what to DELETE from the
         * handset from precisely this list, was handed a short keep-set. `loadedOk` says "a load
         * completed" and could not see it, because one had: the LAST one. So the last good copy of a
         * book that failed is kept (blanking it on screen helps nobody either), and the sweep is told
         * this load was partial. Refusing to reconcile until a whole one lands costs a stale row on
         * the phone; the alternative cost somebody their phone book, twice. */
        const got = {};
        let whole = true;
        for(const b of S.books){
          try{ got[b.id] = (await api('/api/contacts/cards?book=' + encodeURIComponent(b.id))).cards || []; }
          catch(_){ whole = false; got[b.id] = S.cards[b.id] || []; }
        }
        S.cards = got;
        S.rev = (S.rev || 0) + 1;
        S.partial = !whole;     // this load, not the screen's whole history — see the sweep
        if(whole) S.loadedOk = true;
                                // …and only here. Never cleared: a LATER failure leaves the last
                                // good books/cards in place, which is real state worth pushing.
      }catch(e){
        const msg = (e && e.message) || '';
        // 404 is the server being off, which is a state to explain rather than an error to report.
        if(/off on this node/i.test(msg)) S.enabled = false;
        else S.error = msg || 'could not load your contacts';
      }finally{
        S.loading = false; S.ready = true; paint();
      }
      // Keep the phone's own Contacts app in step, both ways. A no-op on every platform but Android
      // and for everybody who has not turned it on, and a no-op again when nothing has changed since
      // the last sweep — so it can sit on the end of every load without being thought about.
      try{ syncPhonebook(); }catch(_){}
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
     * nothing on this device can read a card. A sweep therefore happens when the app is open, and
     * that is the whole schedule. It is why there is no SyncAdapter and why "Sync now" does not
     * exist: a card is an encrypted Nostr event, and the key is in this WebView.
     *
     * TWO WAY, and the ORDER is what makes it safe: PULL → MERGE → PUSH.
     *
     * A push is an overwrite of the phone's copy. Run it first and an edit made in the phone's
     * Contacts app is gone before anything read it — no error, no trace, and the user finds out days
     * later when they ring the old number. So every sweep reads what the phone changed, merges it
     * into the encrypted card (which only this client can read), stores it, and pushes afterwards.
     * The same order applies to the sweep that runs when this screen loads and the one that runs a
     * few seconds after the app starts.
     *
     * CardDAV (⋯ → Addressbooks → Sync to a device) is untouched and remains the cross-platform
     * two-way route — a desktop, an iPhone, and anything that has to sync while this app is closed.
     *
     * OFF BY DEFAULT and per-device: this writes into somebody's phone book, so it is opt-in, and
     * the switch lives beside the addressbook list (⋯ → Addressbooks). */
    const PHONE_KEY = 'androidPhonebook';
    /* WHOSE consent this is. ClientSettings is DEVICE-wide and survives sign-out, so the switch
     * alone meant the NEXT account signed in on that handset was pushed into the phone's Contacts
     * app with no prompt and no opt-in — somebody else's decision applied to your address book.
     * Consent is per account: the switch only counts for the pubkey that flipped it. */
    const PHONE_OWNER = 'androidPhonebookOwner';
    const CSet = () => window.ClientSettings || { get:(k,d)=>d, set(){} };
    const nativeSync = (m) => (PC.capPlugin ? PC.capPlugin('ContactSync', m || 'begin') : null);
    const owner = () => { try{ const me = PC.me ? PC.me() : PC.ME; return (me && me.pubkey) || ''; }
                          catch(_){ return ''; } };
    function phonebookOn(){
      if(!CSet().get(PHONE_KEY, false)) return false;
      const me = owner();
      if(!me) return false;                       // not signed in yet: write nothing either way
      const who = _s(CSet().get(PHONE_OWNER, ''));
      // ADOPT ON FIRST USE. A device that turned this on before consent was scoped has no owner
      // recorded, and the account on it now is the one that turned it on — refusing there would
      // silently stop syncing a phone book that is already working.
      if(!who){ CSet().set(PHONE_OWNER, me); return true; }
      return who === me;
    }
    const _s = (v) => String(v == null ? '' : v);

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

    /* Everything the card↔phone mapping needs — the shape, the hash, the merge and the decision —
     * lives in vcard.js, DOM-free and tested under node (tests/test_vcard.py). This file is the
     * plumbing: fetches, bridge calls and the order they happen in. */

    /* Index of what the app holds, for the merge: uid → {card, book}. */
    function heldCards(){
      const mine = {};
      for(const b of S.books){
        for(const rec of (S.cards[b.id] || [])){
          try{
            const c = V().parse(rec.ics || '');
            c.uid = c.uid || rec.uid;
            if(c.uid && !mine[c.uid]) mine[c.uid] = { card: c, book: b.id };
          }catch(_){ /* one unreadable card must not stop the sweep */ }
        }
      }
      return mine;
    }

    /* PULL — what the phone changed since our last write, merged in and stored.
     *
     * Returns how many cards it wrote, which is also "does the push need fresh state". NOTHING is
     * acknowledged that was not stored: a failed save leaves the row dirty on the phone and the
     * change comes round again on the next sweep, rather than being marked as uploaded and lost.
     * An APK older than two-way sync has no `pull` method at all — that resolves to nothing here and
     * the sweep degrades to the one-way push it used to be. */
    async function pullPhone(){
      const P = nativeSync('pull');
      if(!P) return 0;
      const me = owner();
      if(!me) return 0;                 // never hand the owner guard a blank — see pushPhonebook
      let st = null;
      try{ st = await P.pull({ owner: me }); }catch(_){ return 0; }
      if(!st || st.granted === false) return 0;
      const rows = st.rows || [];
      if(!rows.length) return 0;
      const pushed = st.pushed || {};
      const fallback = S.book || (S.books[0] || {}).id || '';
      const plan = V().phonePlan(
        rows.map(r => Object.assign({}, r, { pushed: pushed[r.uid] || '' })), heldCards());
      const ack = [];
      let wrote = 0;
      for(const step of plan){
        const book = step.book || fallback;
        try{
          if(step.action === 'delete'){
            await api(`/api/contacts/cards?book=${encodeURIComponent(step.book)}`
                      + `&uid=${encodeURIComponent(step.uid)}`, { method:'DELETE' });
            wrote++;
          }else if(step.action === 'create' || step.action === 'update' || step.action === 'keep'){
            if(!book) continue;         // no addressbook to put it in — leave it dirty, try later
            // THE LOSER FIRST. If storing the conflict copy fails we must not have already
            // overwritten the version it is a copy of.
            if(step.copy){
              await jput('/api/contacts/cards',
                         { book, uid: step.copy.uid, vcf: V().serialize(step.copy) });
              wrote++;
            }
            if(step.card){
              await jput('/api/contacts/cards',
                         { book, uid: step.uid, vcf: V().serialize(step.card) });
              wrote++;
            }
          }
          // 'clean' (dirty but identical) and 'drop' (deleted here and there) need no write at all.
          ack.push(Object.assign({ rawId: step.row.rawId, version: step.row.version, uid: step.uid,
                                   deleted: !!step.row.deleted }, step.ack || {}));
        }catch(_){ /* not acknowledged: the phone keeps the change and we try again next sweep */ }
      }
      if(ack.length){ try{ await P.taken({ rows: ack }); }catch(_){} }
      return wrote;
    }

    /* Push what changed. Cheap by construction: `begin()` returns the hash of every card already on
     * the phone, so a visit that changed nothing sends nothing, and the first push after adding one
     * person sends one person. */
    const PUT_BUDGET = 1200000;           // bytes of JSON per bridge call — photos are the bulk
    let _pushSig = '', _pushing = null, _collapseSaid = false;
    async function pushPhonebook(force){
      const P = nativeSync('begin');
      if(!P || !phonebookOn()) return;
      if(!S.loadedOk) return;             // never reconcile against state no load ever produced
      if(S.partial) return;               // …nor against one that came back missing a book
      if(_pushing) return _pushing;       // a sweep is a sweep; two at once would fight over uids
      const list = everyCard().map(c => V().toPhone(c));
      /* ONE reading of who we are, for the whole sweep. `owner()` reads the live session, and an
       * account switch landing between two of these calls hands the plugin a different pubkey — or,
       * on sign-out, an empty one — which its owner guard reads as "somebody else" and answers by
       * wiping the phone book. It refuses an empty owner now; this is the other half. */
      const me = owner();
      if(!me) return;
      const sig = me + '|' + list.map(c => c.uid + ':' + c.h).join(',');
      if(!force && sig === _pushSig) return;
      _pushing = (async () => {
        let st = null;
        try{ st = await P.begin({ owner: me }); }catch(_){ return; }
        if(st && st.granted === false){
          // Revoked in Android's settings after the switch was turned on. Turning it back off is the
          // honest answer — a switch that says "on" while nothing is written is the worse one.
          CSet().set(PHONE_KEY, false); CSet().set(PHONE_OWNER, ''); _pushSig = '';
          toast('Android has revoked access to your contacts — phone sync turned off');
          return;
        }
        const known = (st && st.hashes) || {};
        /* THE COLLAPSE GUARD — the same one that saved the drive index and the folder-sync manifest.
         *
         * `commit({uids})` is a keep-set: everything under this account that is NOT in it is DELETED
         * from the phone — out of the dialer, the share sheet, favourites, ringtones and shortcuts.
         * So a SHORT list is the most destructive thing this bridge can be handed, and every way to
         * produce one is silent: a book whose cards never loaded, a 200 carrying `{books:[]}`, a
         * relay read that answered partially behind it.
         *
         * IT USED TO REFUSE ONLY A TOTALLY EMPTY LIST, and that is why it never fired while a real
         * phone book emptied itself twice: nine cards out of ninety is not empty. The rule is now the
         * one the plugin applies to the rows themselves — A RECONCILE THAT WOULD DELETE MORE THAN IT
         * KEEPS IS A COLLAPSE — computed here from the count the phone just reported, so the client
         * can say something useful instead of watching the bridge refuse.
         *
         * The cost is that a genuine mass delete no longer reaches the phone by itself. That is the
         * honest trade, and there is a deliberate way to do it: turning the switch off removes the
         * account and every row with it, and turning it back on writes the current book. */
        const onPhone = (st && typeof st.count === 'number')
                          ? st.count : Object.keys(known).length;
        const wouldRemove = Math.max(0, onPhone - list.length);
        if(wouldRemove > list.length){
          if(!_collapseSaid){
            _collapseSaid = true;
            toast(list.length
              ? 'your address book came back short (' + list.length + ' of ' + onPhone
                + ') — the contacts on this phone were left alone'
              : 'your address book came back empty — the ' + onPhone + ' contact'
                + (onPhone === 1 ? '' : 's') + ' on this phone were left alone');
          }
          return;                          // and NO _pushSig: the next sweep tries again
        }
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
        /* THE RECONCILE. ALWAYS, even when nothing was written: this is the half that deletes, and
         * somebody removed in the web UI is only removed from the phone here.
         *
         * It was switched off on 2026-08-11 after a real phone book emptied itself repeatedly, and
         * this is what had to be true before it came back — three guards, none of which existed then,
         * each with a test verified to fail without it:
         *   · a per-book fetch failure no longer arrives here as a short keep-set (load(), above);
         *   · a keep-set that would delete more than it keeps is refused here, out loud;
         *   · and the SAME rule is enforced inside commit() against the rows themselves, because
         *     every guard on this side is advisory — the JS is the thing that got it wrong.
         * Behind all three: /api/contacts/cards now reads the relay strictly, so "I could not ask"
         * arrives as a 503 instead of a 200 carrying fewer contacts than the user has.
         *
         * A refusal is a REFUSAL, not a failure: nothing was deleted, `_pushSig` is left alone so the
         * next sweep tries again, and it is said once rather than repeated every 9 seconds. */
        let done = null;
        try{ done = await P.commit({ uids: list.map(c => c.uid) }); }catch(_){ return; }
        if(done && done.refused){
          if(!_collapseSaid){
            _collapseSaid = true;
            toast('this phone kept its ' + (done.count || 0) + ' contacts: the update would have '
                  + 'removed ' + (done.would || 0) + ' of them');
          }
          return;
        }
        _collapseSaid = false;
        _pushSig = sig;
      })().catch(()=>{}).finally(()=>{ _pushing = null; });
      return _pushing;
    }

    /* ONE SWEEP: pull, merge, store, then push. See the section comment for why that order is not
     * negotiable. Serialized against itself — two sweeps at once would each act on a view of the
     * phone the other has already changed. */
    let _syncing = null;
    function syncPhonebook(force){
      if(!nativeSync('begin') || !phonebookOn()) return Promise.resolve();
      // A sweep both pushes and DELETES, so it needs a load that actually landed. Without this a
      // start with no network — the app opening before wifi associates — sweeps from `books:[]` and
      // takes the whole phone book with it.
      if(!S.loadedOk) return Promise.resolve();
      // …and a WHOLE one. The pull half is no safer here than the push: a card whose book failed to
      // fetch is missing from heldCards(), so the phone's row for it reads as a contact created on
      // the phone and is stored again, in whichever book happens to be first — one person, two cards.
      if(S.partial) return Promise.resolve();
      if(_syncing) return _syncing;
      _syncing = (async () => {
        let wrote = 0;
        try{ wrote = await pullPhone(); }catch(_){}
        // Re-read what the pull just stored. Without this the push would send the state from before
        // the merge — undoing the phone's edit on the phone — and the reconcile would not know about
        // a contact created there at all.
        if(wrote){ _pushSig = ''; try{ await load(); }catch(_){} }
        await pushPhonebook(force || !!wrote);
      })().catch(()=>{}).finally(()=>{ _syncing = null; });
      return _syncing;
    }

    /* WHICH ROUTE THIS DEVICE IS SHOWN FIRST.
     *
     * On Android the answer is this app's own switch: no URL, no password, no second app to install.
     * That is the entire point of the plugin, so leading with a CardDAV URL there — which is what
     * this panel used to do — makes the feature invisible and recommends the thing it replaced.
     *
     * A PLUGIN HAS TO BE PROBED, not assumed. `Capacitor.Plugins` is empty for a plugin registered
     * only in Java, and `registerPlugin()` hands back a proxy whose methods all "exist" whether the
     * native side implements them or not — so the only honest test is to CALL one. A packaged app
     * where that call fails is an APK older than this feature, and the useful thing to say is
     * "update the app", not a CardDAV URL, which is a wrong turn dressed up as an answer. */
    /* IS THIS THE PACKAGED ANDROID APP — asked WITHOUT the Capacitor bridge.
     *
     * THE QUESTION USED TO BE PUT TO THE VERY THING THAT CAN BE BROKEN. The only gate on showing
     * anything here was `Capacitor.getPlatform()`, which exists only once the native bridge's
     * injected JS has run; when it has not, it answers 'web' — the same answer Chrome gives — so the
     * panel rendered NOTHING: no switch, and not the sentence written to explain its absence either.
     * A bridge that never arrived was indistinguishable from a browser that will never have one, on
     * the one build both were written for, and the report was "there is no phone-book row at all".
     *
     * Three independent signals, any one of which settles it:
     *   androidBridge      the WebView's own message channel, attached by Java before a single script
     *                      runs — present even when every line of Capacitor's JS is missing.
     *   Capacitor          the bridge itself, when it did arrive.
     *   __PC_APP_BUILD__   baked into index.html by mobile/build-www.sh, so it is proof that this HTML
     *                      IS the packaged app rather than the website — paired with an Android user
     *                      agent, so the desktop build (same shim, no Android) is not caught by it,
     *                      and a browser on a phone (no shim) is not either.
     *
     * `why` goes on the screen. A build that cannot reach its own plugin has to say WHICH piece is
     * missing, or the next report is "nothing happens" all over again. */
    function deviceEnv(){
      const e = { android:false, cap:false, bridge:false, plugin:false, platform:'', why:'' };
      try{
        const c = window.Capacitor;
        e.cap = !!c;
        e.bridge = !!window.androidBridge;
        e.plugin = !!(c && c.Plugins && c.Plugins.ContactSync);
        try{ e.platform = (c && c.getPlatform && c.getPlatform()) || ''; }catch(_){ e.platform = ''; }
        const ua = String((typeof navigator !== 'undefined' && navigator.userAgent) || '');
        const packaged = typeof window.__PC_APP_BUILD__ !== 'undefined'
                      || typeof window.__PC_API_BASE__ !== 'undefined';
        e.android = e.platform === 'android' || e.bridge || e.plugin
                 || (packaged && /Android/i.test(ua));
        /* Enough to settle it from a SCREENSHOT. Every hypothesis this failure has produced —
         * "the plugin is not registered", "the name does not match", "the bridge is late", "it is
         * not really the app" — is a different one of these values, and guessing between them has
         * already cost more than printing them ever will. */
        let keys = 0;
        try{ keys = (c && c.Plugins) ? Object.keys(c.Plugins).length : 0; }catch(_){}
        e.why = 'platform=' + (e.platform || 'unknown') + ' capacitor=' + (e.cap ? 'yes' : 'no')
              + ' bridge=' + (e.bridge ? 'yes' : 'no') + ' plugin=' + (e.plugin ? 'yes' : 'no')
              + ' plugins=' + keys
              + ' promise=' + ((c && typeof c.nativePromise === 'function') ? 'yes' : 'no');
      }catch(_){ }
      return e;
    }
    const capPlatform = () => (deviceEnv().android ? 'android' : 'web');
    let _native;                    // undefined = not asked, false = this build has none, true = has
    async function probeNative(){
      // Known absent — do not ask on every open. But on the packaged app ASK AGAIN: the bridge can
      // arrive after the page's scripts (the same startup race that killed Folder Sync for a whole
      // session), and this is a hand-opened panel, so a second cheap call costs nothing and is the
      // difference between healing itself and being dead until the app is restarted.
      if(_native === false && !deviceEnv().android) return null;
      const P = nativeSync('status');
      if(!P){ _native = false; return null; }     // not a Capacitor build at all
      try{
        const st = (await P.status()) || {};
        _native = true;
        return st;                                // asked every time: the count is on screen
      }catch(_){
        // A first call that fails is a build without the plugin. A LATER one that fails is a blip,
        // and must not turn a working switch into "update the app".
        if(_native === undefined) _native = false;
        return _native ? {} : null;
      }
    }

    /* The switch, at the TOP of ⋯ → Addressbooks. The explanation sits beside it BEFORE it is
     * flipped, because the Android permission prompt itself says only "access your contacts" — the
     * reason has to be on screen already or the prompt is a coin toss. */
    function phonebookRow(st){
      if(!st){
        /* A packaged Android app that could not reach the plugin. SAY SO — and say which piece is
         * missing. Rendering nothing here is what made a detection bug invisible: the switch was
         * gone, the sentence explaining the switch was gone, and the panel looked exactly like the
         * one that shipped before this feature existed. Pointing at CardDAV instead would send
         * somebody to install DAVx⁵ for something this app already does. */
        const env = deviceEnv();
        if(!env.android) return '';       // a browser or the desktop app: CardDAV really is the answer
        const line = env.cap
          ? `<b>Update the app to turn this on.</b> This build can't put your contacts in the phone's
             own Contacts app yet — a newer version can, with nothing else to install.`
          : `<b>The app's native bridge didn't load.</b> Everything this app does on the phone itself
             — the Contacts app, folder sync, the share sheet — goes through it. Close PosterChan
             completely and open it again; if it keeps happening, reinstall the latest APK.`;
        return `<div class="cal-row ct-phonebook" style="flex-wrap:wrap">
          <span class="cal-name" style="flex:1 1 100%">Sync to this phone's Contacts app</span>
          <p class="muted small" style="flex:1 1 100%;margin:4px 0 0">${line}
            <br><span class="muted small" id="ctb-phonewhy">${enc(env.why)}${
              window.__PC_APP_BUILD__ ? ' build=' + enc(String(window.__PC_APP_BUILD__)) : ''}</span></p>
          <button class="btn btn-ghost small" id="ctb-phoneretry" style="margin-top:6px">Try again</button>
        </div>`;
      }
      const on = phonebookOn();
      const n = (st && st.count) || 0;
      return `<div class="cal-row ct-phonebook" style="flex-wrap:wrap">
        <label class="row" style="gap:8px;align-items:center;flex:1 1 100%">
          <input type="checkbox" id="ctb-phonebook"${on ? ' checked' : ''}>
          <span class="cal-name">Sync to this phone's Contacts app</span>
        </label>
        <p class="muted small" style="flex:1 1 100%;margin:4px 0 0">
          <b>This is all you need on this phone</b> — no address to type, no password, nothing else to
          install. Your people appear in the dialer, in messaging apps and in the share sheet, and a
          contact you add or edit in the phone's own Contacts app comes back here. Android asks for
          permission to your contacts when you turn it on; it is used for this account's cards and
          nothing else. It syncs <b>while the app is open</b> — your contacts are encrypted, so
          nothing else on the phone can read them. Everything is removed when you sign out or turn
          this off.
          ${on ? `<br><b>${n}</b> contact${n === 1 ? '' : 's'} on this phone.` : ''}</p>
      </div>`;
    }

    function wirePhonebook(root){
      /* The bridge can come up AFTER this panel was opened, so the row that says it is missing owns
       * the way back: forget the verdict and ask again, rather than making somebody restart the app
       * to find out whether it healed. */
      const again = $('#ctb-phoneretry', root);
      if(again) again.onclick = () => { _native = undefined; closeModal(); openMenu(); };
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
            box.checked = false; CSet().set(PHONE_KEY, false); CSet().set(PHONE_OWNER, '');
            toast('Android didn’t allow access to your contacts — nothing was changed');
            return;
          }
          CSet().set(PHONE_KEY, true); CSet().set(PHONE_OWNER, owner());
          toast('adding your contacts to this phone…');
          _pushSig = '';
          await syncPhonebook(true);
          toast('done — look in the phone’s Contacts app');
        }else{
          CSet().set(PHONE_KEY, false); CSet().set(PHONE_OWNER, '');
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
      const native = !!(await probeNative());
      modal(`<h3>Sync to another device</h3>
        ${native ? `<p class="muted small" style="margin-bottom:10px">
           <b>You don't need any of this for the phone you are holding.</b> “Sync to this phone's
           Contacts app”, at the top of the Addressbooks panel, is one switch and nothing to install.
           This page is for your <i>other</i> devices — a desktop, an iPhone — and for keeping them in
           step while this app is closed.</p>` : ''}
        <p class="muted small">Add a <b>CardDAV account</b> in a contacts app that speaks it — the
           built-in accounts on iOS and macOS, Thunderbird on the desktop, DAVx⁵ on another Android
           phone. It syncs <b>both ways</b>: a contact added on the device appears here.</p>
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
        `,
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
      /* ONE cheap native call, and it decides the whole shape of this panel: on a phone that can do
       * it, syncing to the phone's own Contacts app LEADS and CardDAV is the secondary route for
       * other devices. Leading with a CardDAV URL on Android tells somebody to install another app
       * for something this one already does — which is precisely what the plugin exists to avoid. */
      const st = await probeNative();
      const device = phonebookRow(st);
      modal(`<h3>Addressbooks</h3>
        ${device ? `<div class="cal-list" style="margin-bottom:12px">${device}</div>` : ''}
        <div class="cal-list">${S.books.map(b => `<div class="cal-row">
            <span class="cal-name">${enc(b.displayname || b.id)}</span>
            <a class="btn btn-ghost small" href="/api/contacts/export?book=${encodeURIComponent(b.id)}"
               download><svg class="ic b-ic" aria-hidden="true"><use href="#i-download"></use></svg>Export</a>
            <button class="btn btn-ghost small ctb-del" data-id="${enc(b.id)}">Delete</button>
          </div>`).join('') || '<div class="empty">No addressbooks yet.</div>'}</div>
        <div class="row" style="margin-top:14px;flex-wrap:wrap;gap:8px">
          <button class="btn btn-cyan small" id="ctb-add"><svg class="ic b-ic" aria-hidden="true"><use href="#i-plus"></use></svg>New addressbook</button>
          <button class="btn btn-ghost small" id="ctb-import"><svg class="ic b-ic" aria-hidden="true"><use href="#i-upload"></use></svg>Import .vcf</button>
          <button class="btn btn-ghost small" id="ctb-phone"><svg class="ic b-ic" aria-hidden="true"><use href="#i-android"></use></svg>${
            st ? 'Sync to another device' : 'Sync to a device'}</button>
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
        /* A FAILED LOAD IS NOT A VERDICT. `ready` was set on the way out of load() whatever
         * happened, so one blip — the app opening before wifi associates, a 502 while the node
         * restarts — pinned this screen to "could not load your contacts" for the life of the page,
         * and the only way back was a full reload. Coming back to the screen retries. */
        if(!S.loading && (!S.ready || S.error)) load();
      },
      reload: load,
      /* ⋯ → Addressbooks, reachable without the DOM. The row that decides whether this phone can
       * sync to its own Contacts app is inside it, and "it renders nothing at all" is precisely the
       * bug that shipped — so tests/client/contacts_device_sim.js opens THIS, under node, against
       * each shape of a half-arrived Capacitor bridge. */
      openMenu,
      /* KEEP THE TWO COPIES IN STEP WITHOUT ANYBODY OPENING CONTACTS.
       *
       * A sweep runs at the end of load(), and load() only runs when this screen is rendered — so
       * somebody who edits contacts on a laptop, or who adds one in the phone's own Contacts app and
       * never opens this screen, would have two copies drifting apart. Called from app.js a few
       * seconds after start, and it costs nothing at all unless this is the packaged Android app AND
       * the switch is on: only then does it fetch the books. */
      async syncTick(){
        if(!nativeSync('begin') || !phonebookOn()) return;
        if(!S.ready) return load();       // load() sweeps at its end
        return syncPhonebook();
      },
      /* Sign-out and account switch. The phone's copy must not outlive the session that could read
       * it — a handed-down phone would otherwise keep the previous user's people in its dialer and
       * in every share sheet. Removing the ACCOUNT takes every card with it, so there is no sweep
       * here to half-finish. `begin()`'s owner check is the second line of defence for the app that
       * was killed before this could run. */
      forgetDevice(){
        _pushSig = '';
        /* THE SWITCH GOES WITH IT. ClientSettings is device-wide and Session.clear() does not touch
         * it, so a switch left on was consent the NEXT account inherited: sign in on that handset
         * and their contacts were pushed into the phone book with nothing asked. Both keys, so
         * turning it on again is a deliberate act by whoever is signed in then. */
        try{ CSet().set(PHONE_KEY, false); CSet().set(PHONE_OWNER, ''); }catch(_){}
        const P = nativeSync('disable');
        if(!P) return Promise.resolve();
        try{ return Promise.resolve(P.disable()).catch(()=>{}); }catch(_){ return Promise.resolve(); }
      },
    };
  }

  init();
})();
