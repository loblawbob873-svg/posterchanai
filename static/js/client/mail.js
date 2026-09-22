/* Email — the IMAP/nostr-mail client (sidebar → Email), split out of app.js.
 *
 * Loaded on first use: app.js keeps a small block of entry points (search for `_mailDeps`) and
 * builds this factory the first time one of them is called — opening Email, or the background
 * mail check that starts a few seconds after login. The code below is BYTE-IDENTICAL to what it
 * replaced in app.js apart from its reads of app.js's live `let` bindings, which the parser
 * rewrote to `S.<name>` (getters on `dep.state`) at exact identifier offsets.
 *
 * `Mail` itself is handed back as a getter: app.js reads `Mail.unread` for the badges and starts
 * the poller through `_mailLoad()`. `_mailKeysOff` and `_sheet` stayed in app.js — the first is
 * read by renderView on every navigation, the second is shared with other screens.
 */
window.PCMailFactory = function(dep){
  const S = dep.state;   // live app.js bindings: S.CFG, S.GUEST, S.LOGO, S.ME, S.VIEW, S._aiToken, S._mailKeysOff, S.signer
  const {
    $, $$, BUNDLED, NT, _fmtBytes, _instanceBase, _isDesktopApp, _officeSession, _officeable,
    _pickOne, _previewable, _sheet, _withModule, blossomPicker, bumpMail, closeModal, copyValue,
    enc, ensureAiSession, fileFromBytes, modal, notifToast, openMenuPopover, osNotify, saveBlobAs,
    sendDm, switchView, toast, uiConfirm, uiPrompt,
  } = dep;


  /* EMAIL IS ITS OWN VIEW. It used to be the second TAB of Messages, and the two share nothing but
   * a metaphor: DMs are NIP-17 events on relays that this client decrypts, mail is IMAP through the
   * instance. As a tab it could not be opened from the phone's More sheet (that sheet switches
   * VIEWS, and no view named the tab), it could not be a desktop-mode window of its own, and its tab
   * bar cost a row of every phone screen to whichever half you were not looking at.
   *
   * The separation also removes a hazard rather than moving it: renderMessages() is reached from
   * about a dozen places — an arriving DM, a notification landing, a profile resolving — and while
   * the mail client lived inside it, each of those could tear the mail client down and start a fresh
   * full IMAP sync. That render loop hammering /sync is the documented cause of the incident this
   * feature was once removed for. A DM arriving now cannot touch the mail client at all.
   *
   * The mount guard is kept anyway, because it is the cheap half of that lesson.
   */
  function renderMailView(){
    const feed=$('#feed');
    // Opening Email is LOOKING at it, so the count goes to zero — the sidebar badge and the
    // desktop's tray both read Mail.unread, and it only ever went up (sync adds; nothing subtracted).
    Mail.unread=0; bumpMail();
    const mounted=$('#mail-root', feed);
    /* Refresh BEHIND the list, never in front of it. The mounted list stays on screen and a stale
     * mailbox fills in; a fresh one asks nothing. This is the "already up — never remount, never
     * re-sync" rule kept for the REMOUNT (which would throw away scroll and the open message) and
     * dropped for the SYNC, which was never the expensive half. */
    try{ Mail.refreshIfStale(); }catch(_){}
    if(mounted && Mail.root===mounted) return;      // already up — never remount
    feed.innerHTML='<div id="mail-root" class="mail-root"></div>';
    return Mail.render($('#mail-root',feed));
  }
  // ---------- Email client (Nostr-native mailbox; Messages → 📧 Email tab) ----------
  // The mailbox lives as encrypted kind-30078 events server-side; this GUI talks to /api/mail. IMAP/
  // SMTP + at-rest encryption + Blossom attachments are all server-side. Themed via CSS vars (all 7
  // themes) and collapses to a single-pane flow on mobile (see .mail-* in client.css).
  /* ===== nostr-mail (https://nostr-mail.com, spec v0.2.0-draft) ==================================
   *
   * Email that stays email: SMTP/IMAP transport, with the BODY carried in ASCII armor blocks —
   * NIP-44/NIP-04 ciphertext or signed plaintext — between Nostr keys. This codec parses the
   * armor (new AND legacy tags, `> ` quote prefixes tolerated per spec §3.5.4) and builds the one
   * format encoders may produce.
   *
   * V1 BOUNDARIES, each the honest line rather than a guess:
   *   · NIP-44: full support both ways. Sending uses "unsigned + SEAL" (§3.3) — the one shape every
   *     signer can produce, because a remote signer signs EVENTS, not raw bytes.
   *   · Signed plaintext: displayed (the spec keeps a readable copy above the armor for exactly
   *     this), badge says "signature present" — raw Schnorr verify isn't exported by our bundle
   *     yet, so no claim of VERIFIED is ever made.
   *   · NIP-04: REFUSED, quoting the spec's own rule — §4.1 requires verify-then-decrypt, and
   *     decrypting what we cannot verify first is the padding-oracle window the rule exists for.
   *   · Glossia encoding: detected and named as unsupported; the plaintext-above-armor still shows.
   */
  const NMail = {
    _TAG: /^(?:>\s*)*-{3,}\s*(BEGIN|END)\s+NOSTR\s+(.+?)\s*-{3,}\s*$/,
    /** Parse the OUTERMOST nostr-mail message out of a text body. null = not nostr-mail. */
    parse(text){
      const lines = String(text || '').split(/\r?\n/);
      const blocks = [];                    // [{tag, body:[lines]}] in order
      let cur = null, sawAny = false;
      for(const raw of lines){
        const m = this._TAG.exec(raw);
        if(m){
          sawAny = true;
          const kind = m[1], tag = m[2].toUpperCase();
          if(kind === 'BEGIN'){ cur = { tag, body: [] }; blocks.push(cur); }
          else cur = null;                 // END NOSTR MESSAGE / END NOSTR SEAL
          continue;
        }
        if(cur) cur.body.push(raw.replace(/^(?:>\s*)+/, ''));
      }
      if(!sawAny || !blocks.length) return null;
      const plainAbove = [];
      for(const raw of lines){ if(this._TAG.test(raw)) break; plainAbove.push(raw); }
      const first = blocks[0];
      const enc = /^NIP-(44|04)\s+ENCRYPTED\s+(?:BODY|MESSAGE)$/.exec(first.tag);
      const signedBody = /^SIGNED\s+(?:BODY|MESSAGE)$/.test(first.tag);
      const sealOnly = /^SEAL$/.test(first.tag);
      const idBlock = blocks.find(b => /^SIGNATURE$/.test(b.tag)) || blocks.find(b => /^SEAL$/.test(b.tag));
      let name = '', pubkey = '', glossiaKey = false;
      if(idBlock){
        const bl = idBlock.body.map(x => x.trim()).filter(Boolean);
        for(const ln of bl){
          if(!name && ln[0] === '@'){ name = ln.slice(1); continue; }
          const pk = this._pk(ln);
          if(pk){ pubkey = pk; }
          else if(!/^[0-9a-fA-F]+$/.test(ln) || (ln.length !== 128 && ln.length !== 64)){
            // not hex-sig, not a pubkey we can read → possibly glossia-encoded identity
            if(!pubkey) glossiaKey = true;
          }
        }
      }
      const body = (first.body || []).map(x => x.trim()).filter(Boolean).join('');
      const looksB64 = /^[A-Za-z0-9+/=]+$/.test(body);
      if(enc){
        return { kind: 'nip' + enc[1], cipher: looksB64 ? body : '', glossia: !looksB64 || glossiaKey,
                 pubkey, name, signed: blocks.some(b => /^SIGNATURE$/.test(b.tag)),
                 plainAbove: plainAbove.join('\n').trim() };
      }
      if(signedBody){
        return { kind: 'signed', cipher: '', glossia: true /* signed bodies are always glossia (§3.2) */,
                 pubkey, name, signed: true, plainAbove: plainAbove.join('\n').trim() };
      }
      if(sealOnly){
        return { kind: 'sealed-plain', cipher: '', glossia: glossiaKey, pubkey, name, signed: false,
                 plainAbove: plainAbove.join('\n').trim() };
      }
      return null;
    },
    _pk(ln){
      if(/^[0-9a-f]{64}$/i.test(ln)) return ln.toLowerCase();
      if(/^npub1[a-z0-9]{20,}$/i.test(ln)){
        try{ const d = NT().nip19.decode(ln); if(d && d.type === 'npub') return d.data; }catch(_){ }
      }
      return '';
    },
    /** The one format encoders may produce: NIP-44 unsigned + SEAL (§3.3). */
    armor(cipherB64, myNpub, myName){
      return '----- BEGIN NOSTR NIP-44 ENCRYPTED BODY -----\n'
           + String(cipherB64).replace(/(.{76})/g, '$1\n').replace(/\n$/, '') + '\n'
           + '----- BEGIN NOSTR SEAL -----\n'
           + '@' + String(myName || 'anon').replace(/\n/g, ' ') + '\n'
           + myNpub + '\n'
           + '----- END NOSTR MESSAGE -----';
    },
  };

  function _mailDate(ts){ if(!ts) return ''; const d=new Date(ts*1000), now=new Date();
    return d.toDateString()===now.toDateString() ? d.toLocaleTimeString([], {hour:'2-digit',minute:'2-digit'})
      : d.toLocaleDateString([], {month:'short', day:'numeric', year: d.getFullYear()===now.getFullYear()?undefined:'numeric'}); }
  function _fileB64(file){ return new Promise((res,rej)=>{ const r=new FileReader(); r.onerror=()=>rej(r.error);
    r.onload=()=>res(String(r.result).split(',',2)[1]||''); r.readAsDataURL(file); }); }
  function _abB64(buf){ const u=new Uint8Array(buf); let s=''; const CH=0x8000; for(let i=0;i<u.length;i+=CH) s+=String.fromCharCode.apply(null, u.subarray(i,i+CH)); return btoa(s); }

  /* Every emailable contact, loaded once per session from the same encrypted CardDAV addressbooks
   * the Contacts screen and your phone sync. Shared by the 👤 picker and the recipient autocomplete
   * so the two can never disagree about who exists. */
  let _mailPeople = null;
  async function _mailContacts(){
    if(_mailPeople) return _mailPeople;
    const V = window.PCVcard;
    if(!V) return [];
    const out = [];
    try{
      try{ await ensureAiSession(); }catch(_){}
      const bs = await __PC.authFetch('/api/contacts/books').then(r => r.ok ? r.json() : {books:[]});
      for(const b of (bs.books || [])){
        const r = await __PC.authFetch('/api/contacts/cards?book=' + encodeURIComponent(b.id));
        if(!r.ok) continue;
        for(const rec of ((await r.json()).cards || [])){
          let c; try{ c = V.parse(rec.ics || ''); }catch(_){ continue; }
          for(const e of (c.emails || [])){
            if(e.value) out.push({ name: V.displayName(c), email: e.value, type: e.type || '' });
          }
        }
      }
    }catch(_){ return []; }
    out.sort((a, b) => a.name.localeCompare(b.name));
    _mailPeople = out;
    return out;
  }

  const _mailAddr = p => (p.name && !/[<>,]/.test(p.name)) ? `${p.name} <${p.email}>` : p.email;

  /* Type-ahead on To/Cc. A recipient field is where you already know who you mean, so opening a
   * modal to find them is the slow path — this completes as you type and leaves the 👤 button for
   * browsing. Matches on name OR address, and only ever completes the address being edited (the
   * one after the last comma), so it cannot eat recipients already entered. */
  function _attachRecipientAutocomplete(input){
    if(!input) return;
    let box = null, items = [], sel = -1;
    const kill = () => { if(box){ box.remove(); box = null; } items = []; sel = -1; };
    const term = () => (input.value.split(',').pop() || '').trim();
    const put = (p) => {
      const parts = input.value.split(',');
      parts[parts.length - 1] = ' ' + _mailAddr(p);
      input.value = parts.join(',').replace(/^\s+/, '') + ', ';
      kill(); input.focus();
    };
    const draw = async () => {
      const q = term().toLowerCase();
      if(q.length < 2){ kill(); return; }
      const already = input.value.toLowerCase();
      const people = (await _mailContacts()).filter(p =>
        (p.name + ' ' + p.email).toLowerCase().includes(q) &&
        !already.slice(0, already.lastIndexOf(',') + 1).includes(p.email.toLowerCase()));
      items = people.slice(0, 6);
      if(!items.length){ kill(); return; }
      if(!box){
        box = document.createElement('div');
        box.className = 'mc-auto';
        input.parentElement.style.position = input.parentElement.style.position || 'relative';
        input.insertAdjacentElement('afterend', box);
      }
      sel = 0;
      box.innerHTML = items.map((p, i) =>
        `<button class="mc-auto-item${i === 0 ? ' on' : ''}" data-i="${i}">
           <span class="mc-name">${enc(p.name)}</span>
           <span class="mc-mail muted small">${enc(p.email)}</span></button>`).join('');
      box.querySelectorAll('[data-i]').forEach(b =>
        b.onmousedown = (e) => { e.preventDefault(); put(items[+b.dataset.i]); });
    };
    input.addEventListener('input', draw);
    input.addEventListener('blur', () => setTimeout(kill, 120));
    input.addEventListener('keydown', (e) => {
      if(!items.length) return;
      if(e.key === 'ArrowDown' || e.key === 'ArrowUp'){
        e.preventDefault();
        sel = (sel + (e.key === 'ArrowDown' ? 1 : items.length - 1)) % items.length;
        box.querySelectorAll('.mc-auto-item').forEach((el, i) => el.classList.toggle('on', i === sel));
      }else if(e.key === 'Enter' || e.key === 'Tab'){
        // Enter completes the highlighted contact rather than submitting — the same bargain every
        // autocomplete makes, and Escape gets you out without one.
        if(sel >= 0){ e.preventDefault(); put(items[sel]); }
      }else if(e.key === 'Escape'){
        // Close the SUGGESTIONS, not the composer. Left to bubble, Escape reached the modal's own
        // handler and threw away the half-written email behind the dropdown.
        e.preventDefault(); e.stopPropagation(); kill();
      }
    });
  }

  async function _mailContactPicker(onPick){
    const sheet=_sheet(`<h3>👤 Contacts</h3>
      <input class="input" id="mc-q" placeholder="🔍 Search contacts…" autocomplete="off">
      <div id="mc-list" class="mc-list"><div class="spinner"></div></div>`);
    const $ = (sel, r) => (r || sheet.box).querySelector(sel);
    const $$ = (sel, r) => Array.from((r || sheet.box).querySelectorAll(sel));
    const box = $('#mc-list'); if(!box) return;
    const people = await _mailContacts();
    if(!people.length){
      box.innerHTML = `<div class="muted small">No contacts with an email address yet. Add them in
        <b>Contacts</b>, or import a .vcf there.</div>`;
      return;
    }
    const draw = (q) => {
      const hit = people.filter(p => !q || (p.name + ' ' + p.email).toLowerCase().includes(q.toLowerCase()));
      box.innerHTML = hit.length ? hit.map(p => `<button class="mc-item" data-i="${people.indexOf(p)}">
          <span class="mc-name">${enc(p.name)}</span>
          <span class="mc-mail muted small">${enc(p.email)}${p.type ? ' · ' + enc(p.type) : ''}</span>
        </button>`).join('') : '<div class="muted small">No contact matches that.</div>';
      $$('.mc-item', box).forEach(b => b.onclick = () => { onPick(people[+b.dataset.i]); sheet.close(); });
    };
    draw('');
    const q = $('#mc-q'); if(q){ q.oninput = () => draw(q.value.trim()); q.focus(); }
  }

  /* Attach from Blossom — the app's OWN picker, the same one the post composer, chat and every
   * other attach button open. It was a hand-rolled list here: no folders, no Escape, no focus trap,
   * no filtering of the octet-stream noise (encrypted ciphertext and stale index blobs), and its own
   * bug where closing it took the half-written email with it.
   *
   * Mail needs the BYTES, not a URL, because an attachment is encrypted and uploaded per message —
   * so this is the standard picker plus a fetch of what was picked.
   */
  async function _mailBlossomPicker(onPick){
    blossomPicker(null, async ({url, type, ext, name: pickedName}) => {
      /* The Blossom URL is normally content-addressed (`/<sha>`), not a filename.  The picker has
       * already reconciled the original name from FilesIdx; discarding it renamed every mail
       * attachment to a 64-character hash even though the user had tapped a clearly named tile.
       * Keep the URL leaf only as the fallback for old/unindexed rows. */
      const leaf = String(pickedName || '').trim()
                || decodeURIComponent((url.split('?')[0].split('/').pop() || 'file'));
      const name = leaf + (ext && !/\.[a-z0-9]{1,8}$/i.test(leaf) ? '.' + ext : '');
      try{
        toast('fetching…');
        const r = await fetch(url);
        if(!r.ok) throw new Error('http ' + r.status);
        onPick({ name, type: type || 'application/octet-stream', b64: _abB64(await r.arrayBuffer()) });
        toast('attached');
      }catch(err){ toast('could not attach that file'); }
    }, { title: '📁 Attach from Files' });
  }
  function _mailKeys(M){
    if(S._mailKeysOff) S._mailKeysOff();
    const onKey = (e) => {
      if(e.ctrlKey || e.metaKey || e.altKey || e.defaultPrevented) return;
      // The mail client being MOUNTED is the condition — not two globals describing where we think
      // we are. If its root is not in the document, this screen is not showing and these keys are
      // somebody else's.
      if(!M.root || !M.root.isConnected) return;
      if(document.body.classList.contains('modal-open')) return;
      const t = e.target;
      if(t && (t.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName || ''))) return;
      const open = !!(M.root && M.root.querySelector('.mail-read.has-open')) || !!M.openUid;
      const k = e.key;
      const go = (n) => {
        const rows = M.msgs || [];
        if(!rows.length) return;
        M.cursor = Math.max(0, Math.min(rows.length - 1, (M.cursor == null ? -1 : M.cursor) + n));
        M.drawList();
        const el = M.root && M.root.querySelector('.mail-item.cursor');
        if(el) el.scrollIntoView({ block: 'nearest' });
      };
      const act = (name) => {
        const m = (M.msgs || [])[M.cursor];
        if(!m) return;
        M.action(name, m, m.folder || M.folder, m.account || M.acct);
      };
      switch(k){
        case 'j': case 'ArrowDown':  e.preventDefault(); go(+1); return;
        case 'k': case 'ArrowUp':    e.preventDefault(); go(-1); return;
        case 'g': _mailG = (_mailG === 'g') ? (M.cursor = 0, M.drawList(), null) : 'g'; return;
        case 'G': e.preventDefault(); M.cursor = Math.max(0, (M.msgs||[]).length - 1); M.drawList(); return;
        case 'Enter': case 'o': {
          const m = (M.msgs || [])[M.cursor];
          if(m){ e.preventDefault(); M.open(m.uid, m.folder || M.folder, m.account); }
          return;
        }
        case 'Escape': case 'q': {
          if(!open) return;
          e.preventDefault();
          const pane = M.root && M.root.querySelector('.mail-read');
          if(pane){ pane.classList.remove('has-open'); }
          M.openUid = null; M.drawList();
          return;
        }
        case 'c': e.preventDefault(); M.compose({}); return;
        case 'r': e.preventDefault(); act('reply'); return;
        case 'a': e.preventDefault(); act('replyall'); return;
        case 'f': e.preventDefault(); act('forward'); return;
        case 'u': e.preventDefault(); act('unread'); return;
        case 'e': e.preventDefault(); act('archive'); return;
        case '#': e.preventDefault(); act('delete'); return;
        case '/': {
          const q = M.root && M.root.querySelector('#mail-search');
          if(q){ e.preventDefault(); q.focus(); q.select(); }
          return;
        }
        case '?': e.preventDefault(); _mailKeysHelp(); return;
      }
      if(k !== 'g') _mailG = null;
    };
    document.addEventListener('keydown', onKey);
    S._mailKeysOff = () => { document.removeEventListener('keydown', onKey); S._mailKeysOff = null; };
    return S._mailKeysOff;
  }
  let _mailG = null;

  function _mailKeysHelp(){
    const rows = [['j / k', 'next / previous message'], ['Enter or o', 'open'],
                  ['Esc or q', 'back to the list'], ['gg / G', 'first / last'],
                  ['c', 'compose'], ['r / a / f', 'reply / reply all / forward'],
                  ['u', 'mark unread'], ['e', 'archive'], ['#', 'delete'],
                  ['/', 'search all email accounts'], ['?', 'this list']];
    modal('<h3>⌨️ Email shortcuts</h3><div class="ks-grid">'
      + rows.map(([k, d]) => `<kbd>${enc(k)}</kbd><span>${enc(d)}</span>`).join('') + '</div>');
  }

  function _mailAttachmentUrl(m, folder, acct, i){
    const base=_instanceBase();
    // Packaged clients run at app://posterchan or https://localhost. A missing instance must be an
    // unavailable attachment, never a relative URL that silently resolves to either package host.
    if(!/^https?:\/\//i.test(base)) return '';
    /* A malformed packaged preference once supplied the WebView's own bootstrap origin as the API
     * base. It passes the protocol check above but there is no Mail server on the phone/desktop's
     * loopback listener, so every attachment looked clickable and then failed against localhost.
     * Keep loopback valid for an explicitly local WEB development server; it is never an instance
     * address a bundled client may use. */
    if(typeof BUNDLED!=='undefined'&&BUNDLED){
      try{
        const host=new URL(base).hostname.toLowerCase();
        if(host==='localhost'||/^127\./.test(host)||host==='::1'||host==='[::1]') return '';
      }catch(_){ return ''; }
    }
    return base+'/api/mail/dl/'+encodeURIComponent(m.account||acct)+'/'+encodeURIComponent(m.folder||folder)
      +'/'+encodeURIComponent(m.uid)+'/'+encodeURIComponent(i);
  }
  async function _openMailAttachment(a){
    if(!a || a.dataset.loading==='1') return false;
    a.dataset.loading='1';a.setAttribute('aria-busy','true');
    try{
      const url=a.dataset.mailUrl||'';
      if(!/^https?:\/\//i.test(url)) throw new Error('connect this app to your PosterChan instance first');
      const r=await fetch(url,{credentials:'include',headers:S._aiToken
        ?{'Authorization':'Bearer '+S._aiToken}:{}});
      if(!r.ok)throw new Error('attachment returned '+r.status);
      const blob=await r.blob();
      if(a.dataset.mailPreview==='1'){
        const P=await _withModule('preview.js','PCPreview');
        if(!P||!P.open({name:a.dataset.name||'attachment',mime:a.dataset.mime||blob.type,blob}))
          throw new Error('Preview cannot open this attachment');
      }else if(S.CFG.office_enabled && _officeable(a.dataset.name||'',a.dataset.mime||blob.type)){
        const name=a.dataset.name||'document';
        const file=fileFromBytes(await blob.arrayBuffer(),name,a.dataset.mime||blob.type);
        await _officeSession(file, updated=>saveBlobAs(updated,name));
      }else await saveBlobAs(blob,a.dataset.name||'attachment');
      return true;
    }catch(err){toast('could not open attachment: '+((err&&err.message)||err));return false;}
    finally{delete a.dataset.loading;a.removeAttribute('aria-busy');}
  }

  const Mail = {
    unread:0, root:null, accounts:[], acct:null, folder:'INBOX', folders:['INBOX','Sent','Drafts'], folderLabels:{}, msgs:[], openUid:null, openFolder:null, openAccount:null, q:'', _syncing:false, sel:null, _listSeq:0,
    async api(path, opts={}){
      await ensureAiSession();
      const r=await fetch('/api/mail'+path,{...opts,credentials:'include',headers:{...(opts.headers||{}),...(S._aiToken?{'Authorization':'Bearer '+S._aiToken}:{})}});
      if(!r.ok) throw new Error('http '+r.status); return r.json();
    },
    async render(root){
      this.root=root; root.innerHTML='<div class="mail-loading"><div class="spinner"></div></div>';
      let authError=null;
      try{ const a=await this.api('/accounts'); this.accounts=a.accounts||[]; }
      catch(e){ this.accounts=[]; authError=e; }
      /* The mail request yields. The shared feed may have moved to Notes, Terminal, or another OS
       * window while it was in flight; a late 401/empty response must not paint Mail there. */
      if(!root.isConnected||this.root!==root||S.VIEW!=='mail'||root.closest('#feed')!==$('#feed'))return;
      if(authError){
        root.innerHTML=`<div class="mail-empty"><div class="me-ico">⚠️</div><h3>Email couldn’t sign in</h3>
          <p class="muted">${enc((authError&&authError.message)||'could not establish your app session')}</p>
          <button class="btn btn-ghost" id="mail-auth-retry">Retry</button></div>`;
        const retry=$('#mail-auth-retry',root); if(retry) retry.onclick=()=>this.render(root);
        return;
      }
      if(!this.accounts.length){
        /* The nostr-mail expectation gap, answered where it forms: "Mobile still asking me to set
         * up an email — I just want to use nmail." An email ACCOUNT is nostr-mail's transport; the
         * Nostr key is only the lock, so there is no version of this that works without one — and
         * the person asking usually wants encrypted messaging WITHOUT email, which this app already
         * has under Messages. Say both here rather than leaving them to be guessed. */
        root.innerHTML=`<div class="mail-empty"><div class="me-ico">📧</div><h3>Email</h3>
          <p class="muted">No mail accounts yet. Add your IMAP/SMTP account in <b>Settings → Mail</b>, then it syncs here as an encrypted Nostr mailbox.</p>
          <p class="muted small">\ud83d\udd10 <b>Nostr-encrypted email (nostr-mail)</b> also lives here: your email account carries the message, your Nostr key locks the body \u2014 so it needs the account. Just want encrypted messaging with other Nostr users, no email involved? That\u2019s <b>Messages</b>, already built in.</p>
          <div class="row" style="gap:8px;justify-content:center">
            <button class="btn btn-neon" id="mail-go-settings">Open Settings \u2192 Mail</button>
            <button class="btn btn-ghost" id="mail-go-dms">Open Messages</button>
          </div></div>`;
        const b=$('#mail-go-settings',root); if(b) b.onclick=()=>switchView('settings');
        const d=$('#mail-go-dms',root); if(d) d.onclick=()=>switchView('messages');
        return;
      }
      if(!this.acct || (this.acct !== '__all' && !this.accounts.some(a=>a.email===this.acct))){
        // With more than one account the unified inbox is what you actually want to see first —
        // opening on whichever account happened to be first in the list hides the rest.
        this.acct = this.accounts.length > 1 ? '__all' : this.accounts[0].email;
      }
      _mailKeys(this);          // j/k/Enter/… while this screen is the one showing
      this.draw();
      // Pull fresh on OPEN, not on every mount. sync() is a full IMAP round trip per account; with
      // no floor, anything that remounted this screen started another one on top of the last.
      // The 🔄 button and loginSync still force one — this only bounds the automatic path.
      if(!this._lastSync || Date.now()-this._lastSync > 120000) this.sync();
    },
    draw(){
      const root=this.root; if(!root) return;
      root.innerHTML=`<div class="mail-wrap">
        <div class="mail-side">
          <select class="input mail-acct" id="mail-acct" title="Account">${this.accounts.length>1?`<option value="__all"${this.acct==='__all'?' selected':''}>📥 All inboxes</option>`:''}${this.accounts.map(a=>`<option value="${enc(a.email)}"${a.email===this.acct?' selected':''}>${enc(a.email)}</option>`).join('')}</select>
          <button class="btn btn-neon mail-compose" id="mail-compose">✏️ Compose</button>
          <div class="mail-folders"></div>
        </div>
        <div class="mail-list">
          <!-- SELECT-ALL RIDES THE SEARCH ROW. It used to be the only thing in a 40px bar of its own,
               which meant a permanent strip of chrome saying "Select" above a phone list that had
               already lost more than half the screen to the folder rail, the search box and the nav.
               The bar below is now the BULK ACTIONS' bar and collapses to nothing until there is a
               selection to act on (.mail-bulk:not(:has(.btn)) in client.css). NO BACKTICKS IN
               HERE: this comment lives inside a template literal, and one would close it and take
               the whole module out at parse time. -->
          <div class="mail-list-top"><label class="mail-selall" title="Select all / none"><input type="checkbox" id="mail-selall"> Select</label><input class="input mail-search" id="mail-search" placeholder="🔍 Search all accounts…" aria-label="Search all email accounts" value="${enc(this.q)}"><button class="mini mail-folders-open" id="mail-folders-open" title="Browse folders" aria-label="Browse folders">📂</button><button class="mini mail-refresh" id="mail-refresh" title="Refresh">🔄</button></div>
          <div class="mail-bulk"><span class="mail-bulk-act" id="mail-bulk-act"></span></div>
          <div class="mail-items" id="mail-items"><div class="spinner"></div></div>
        </div>
        <div class="mail-read" id="mail-read"><div class="empty">Select a message to read</div></div>
      </div>`;
      $('#mail-acct',root).onchange=e=>{ this.acct=e.target.value; this.openUid=null; this.q=''; this.folder='INBOX'; this.folders=['INBOX','Sent','Drafts']; this._allFolders=null; if(this.sel) this.sel.clear(); this.draw(); this.loadList(); this.sync(); };
      $('#mail-compose',root).onclick=()=>this.compose({});
      $$('[data-folder]',root).forEach(b=> b.onclick=()=>this.selectFolder(b.dataset.folder));
      /* THE FOLDER STRIP SCROLLS SIDEWAYS ON A PHONE, so the folder you are IN can be off-screen.
       * It is one row by design (two rows cost 130px of a 553px screen, permanently), and the price
       * of one row is that a mailbox with nine folders does not fit in it. Bring the active chip
       * back into view after every draw — by writing scrollLeft on the strip itself, never
       * scrollIntoView, which is free to scroll the whole page and every ancestor with it. */
      { const strip=$('.mail-folders',root), on=$('.mail-folder.on',root);
        if(strip && on && strip.scrollWidth > strip.clientWidth + 1){
          const l=on.offsetLeft, r=l+on.offsetWidth;
          if(l < strip.scrollLeft) strip.scrollLeft = Math.max(0, l-8);
          else if(r > strip.scrollLeft + strip.clientWidth) strip.scrollLeft = r - strip.clientWidth + 8;
        } }
      { const s=$('#mail-search',root); if(s){ let t; s.oninput=()=>{ clearTimeout(t); t=setTimeout(()=>{ if(this.root!==root||!s.isConnected)return; this.q=s.value.trim(); this.loadList(); },300); }; } }
      $('#mail-refresh',root).onclick=()=>this.sync(true);
      { const fb=$('#mail-folders-open',root); if(fb) fb.onclick=()=>this.browseFolders(); }
      /* Decide from the SELECTION, never from the box's own checked state.
       *
       * The box is not a source of truth: updateBulk() rewrites it on every redraw as
       * `n === this.msgs.length`. So the moment the list changes underneath a full selection — a
       * background sync, "Load older", switching folders — everything is still selected while the box
       * has quietly gone UNCHECKED, and the next press reads that as "select all" and re-adds them.
       * Select All then had no way to undo itself, which is the report.
       *
       * Anything selected → clear it. Nothing selected → select the list. That also gives the partial
       * case (you ticked three by hand) the obvious meaning. */
      { const sa=$('#mail-selall',root); if(sa) sa.onchange=()=>{ this.sel=this.sel||new Set();
        if(this.sel.size) this.sel.clear(); else this.msgs.forEach(m=>this.sel.add(this._key(m)));
        this.drawList(); }; }
      this.drawFolders(); this.loadList(); this.loadFolders();
    },
    _folderLabel(f){ if(this.folderLabels && this.folderLabels[f]) return this.folderLabels[f];
      const k={INBOX:'📥 Inbox',Sent:'📤 Sent',Drafts:'📝 Drafts',Trash:'🗑 Trash',Spam:'⚠️ Spam',Junk:'⚠️ Junk',Archive:'🗄 Archive'}; return k[f]||('📁 '+enc(String(f).split(/[./]/).pop()||f)); },
    async loadFolders(){
      if(!this.root) return;
      if(this.acct==='__all'){
        /* This returned immediately, so the unified view was stuck on the three folders draw()
           hardcoded — "no way to browse folders". Ask every account and keep the answers per
           account, because `_folderChoices` needs to know which folders they all share. */
        const want=this.accounts.map(a=>a.email), per={}, labels={};
        await Promise.all(want.map(async email=>{
          try{
            const r=await this.api('/folders?account='+encodeURIComponent(email));
            per[email]=(r&&r.folders)||[];
            Object.assign(labels,(r&&r.labels)||{});
          }catch(_){ per[email]=[]; }     // one unreadable account must not empty the whole strip
        }));
        if(this.acct!=='__all') return;                      // the account changed while we asked
        this._allFolders=per; this.folderLabels=labels;
        this.drawFolders();
        return;
      }
      const account=this.acct;
      let r; try{ r=await this.api('/folders?account='+encodeURIComponent(this.acct)); }catch(_){}
      if(!r || !r.folders || !r.folders.length || account!==this.acct) return;
      // `draw()` must offer a Sent button before this server round-trip finishes. If it was clicked
      // during that window, move the view to the server's real RFC-6154 Sent mailbox instead of
      // leaving it on a plausible but unrelated literal folder named "Sent".
      const resolvedSent=r.sent||'';
      const remapSent=this.folder==='Sent' && resolvedSent && resolvedSent!=='Sent';
      if(remapSent) this.folder=resolvedSent;
      this.folders=r.folders; this.folderLabels=r.labels||{};
      this.drawFolders();
      if(remapSent){ this.msgs=[]; this.loadList(); this.refreshFolder(resolvedSent); }
    },
    /* THE FOLDER LIST IS ONE RENDERER, AND IT DOES NOT GROW DOWN THE PAGE.
       It was built in two places (draw() and loadFolders()) from two different sources, which is
       how All-inboxes ended up permanently showing three hardcoded folders. A mailbox can have
       dozens, and a strip of dozens of buttons is a sidebar with no room left for mail — so the
       roles people actually switch between stay as buttons and the rest live behind one "More"
       popover, which costs a single button of space however many folders there are. */
    _ROLE_FOLDERS: ['INBOX','Sent','Drafts','Trash','Spam','Archive'],
    _folderChoices(){
      if(this.acct!=='__all') return this.folders||[];
      /* Unified view. The server resolves a ROLE (Sent/Drafts/Trash/Spam/Archive) to each account's
         own mailbox name, so those always work across accounts; a custom folder is tried literally,
         so only offer one when EVERY account has it — otherwise the folder half-works and the list
         silently omits an account. */
      const per=this._allFolders||{}, emails=Object.keys(per);
      if(!emails.length) return ['INBOX','Sent','Drafts'];
      const roles=this._ROLE_FOLDERS.filter(r=> r==='INBOX' || emails.some(e=>(per[e]||[]).some(f=>f===r||this.folderLabels[f]===this._folderLabel(r))));
      const common=(per[emails[0]]||[]).filter(f=>!this._ROLE_FOLDERS.includes(f)
        && emails.every(e=>(per[e]||[]).includes(f)));
      return roles.concat(common);
    },
    drawFolders(){
      const box=this.root&&this.root.querySelector('.mail-folders'); if(!box) return;
      const all=this._folderChoices();
      const pinned=['INBOX','Sent','Drafts'];
      // The folder you are IN is always a visible button, even when it lives in the overflow —
      // a strip that does not show where you are is worse than one that is too long.
      const shown=all.filter(f=>pinned.includes(f)||f===this.folder);
      const rest=all.filter(f=>!shown.includes(f));
      box.innerHTML=shown.map(f=>`<button class="mail-folder${f===this.folder?' on':''}" data-folder="${enc(f)}">${this._folderLabel(f)}</button>`).join('')
        +(rest.length?`<button class="mail-folder mail-folder-more" id="mail-more" title="Other folders">📁 More ▾</button>`:'');
      box.querySelectorAll('.mail-folder[data-folder]').forEach(b=> b.onclick=()=>this.selectFolder(b.dataset.folder));
      const more=box.querySelector('#mail-more');
      if(more) more.onclick=()=> openMenuPopover(more, rest.map(f=>[f,this._folderLabel(f)]), v=>this.selectFolder(v));
    },
    /* BROWSE EVERY FOLDER — the one thing the strip above cannot do.
     *
     * `.mail-folders` is ONE row by design (two cost 130px of a 553px phone, permanently), so it
     * shows INBOX/Sent/Drafts, the folder you are in, and a "More" popover for the rest. Two things
     * follow from that, and together they are "there is no way to browse the damn email folders":
     * the More button only exists when `_folderChoices()` returned something beyond the pinned
     * three, so a mailbox whose folder list has not loaded — or an All-inboxes view before
     * `_allFolders` lands — offers no way out of Inbox at all; and even when it is there, a popover
     * anchored to a chip in a sideways-scrolling strip is not a folder browser.
     *
     * This is a sheet, so it costs NOTHING when closed — which is the constraint that was asked for.
     * It lists every folder, filters as you type (a real mailbox has dozens), says plainly when the
     * list has not been read yet rather than showing an empty box, and can fetch it on the spot. */
    browseFolders(){
      const all = this._folderChoices() || [];
      const cur = this.folder;
      const rows = f => `<button class="btn btn-ghost full mail-fbrowse${f===cur?' on':''}" data-folder="${enc(f)}">${this._folderLabel(f)}</button>`;
      const empty = `<div class="muted small" style="padding:10px 2px">
          This account's folder list hasn't been read yet — that is why only Inbox, Sent and Drafts
          are offered. Fetching it asks the mail server for the list.</div>`;
      modal(`<h3>📂 Folders${this.acct && this.acct!=='__all' ? ' · '+enc(this.acct) : ''}</h3>
        <input class="input" id="mail-fbrowse-q" placeholder="🔍 Filter folders…" autocomplete="off">
        <div id="mail-fbrowse-list" style="max-height:52vh;overflow:auto;display:flex;flex-direction:column;gap:4px;margin-top:8px">
          ${all.length ? all.map(rows).join('') : empty}
        </div>
        <div class="row" style="justify-content:space-between;margin-top:10px">
          <button class="btn btn-ghost small" id="mail-fbrowse-reload">↻ Fetch folder list</button>
          <button class="btn btn-ghost small" id="mail-fbrowse-close">Close</button>
        </div>`, box => {
        const list = box.querySelector('#mail-fbrowse-list');
        const bind = () => list.querySelectorAll('[data-folder]').forEach(b =>
          b.onclick = () => { closeModal(); this.selectFolder(b.dataset.folder); });
        bind();
        { const q = box.querySelector('#mail-fbrowse-q');
          if(q) q.oninput = () => { const t = q.value.trim().toLowerCase();
            list.querySelectorAll('[data-folder]').forEach(b => {
              const hit = !t || (b.dataset.folder + ' ' + b.textContent).toLowerCase().includes(t);
              /* style.display, not the `hidden` attribute: `.btn.full:has(.b-ic){display:flex}` is an
               * AUTHOR rule and would beat the UA's `[hidden]{display:none}` the moment a folder
               * label carries an icon — a filter that silently stops filtering. */
              b.style.display = hit ? '' : 'none'; }); }; }
        { const c = box.querySelector('#mail-fbrowse-close'); if(c) c.onclick = () => closeModal(); }
        { const r = box.querySelector('#mail-fbrowse-reload');
          if(r) r.onclick = async () => {
            r.disabled = true; const was = r.textContent; r.textContent = 'Fetching…';
            /* Say what happened either way. A reload that silently redraws the same three folders
             * is indistinguishable from a button that does nothing, which is the whole complaint. */
            try{
              await this.loadFolders();
              const now = this._folderChoices() || [];
              list.innerHTML = now.length ? now.map(rows).join('')
                : '<div class="muted small" style="padding:10px 2px">The mail server did not return a folder list.</div>';
              bind();
              if(now.length <= all.length) toast('folder list refreshed — ' + now.length + ' folder(s)');
            }catch(e){ toast('could not read the folder list' + ((e && e.message) ? ': ' + e.message : '')); }
            finally{ r.disabled = false; r.textContent = was; }
          }; }
      });
    },
    async selectFolder(f){
      this.folder=f; this.openUid=null; this.q=''; this.msgs=[]; this.convSent=[]; if(this.sel) this.sel.clear();
      if(this.root) this.root.querySelectorAll('.mail-folder').forEach(b=> b.classList.toggle('on', b.dataset.folder===f));
      // SHOW WHAT IS ALREADY MIRRORED FIRST. This used to await a full IMAP pull of the folder
      // before rendering anything, so opening an Archive of thousands of messages was a spinner for
      // minutes with no way to tell whether it was working. Whatever has been synced before appears
      // at once; the pull then runs in the background and the list refreshes when it lands.
      await this.loadList();
      // A folder click means "show me what is there now". The periodic whole-mailbox sync is not a
      // substitute: it may still be walking another account/folder, which is how Sent could stop on
      // Aug 8 while a mailbox-wide search already found a message sent this morning.
      if(f!=='Drafts') this.refreshFolder(f);
    },
    refreshFolder(f){
      const mine=f, account=this.acct;
      this.setBusy(`Fetching ${this._folderLabel(f).replace(/^\S+\s*/, '')} from the mail server…`);
      this.api('/sync-folder',{method:'POST',headers:{'Content-Type':'application/json'},
                               body:JSON.stringify({account,folder:f})})
        .then(()=>{ if(this.folder===mine && this.acct===account) this.loadList(); })
        .catch(()=>{ if(this.folder===mine && this.acct===account) toast('could not fetch that folder'); })
        .finally(()=>{ if(this.folder===mine && this.acct===account) this.setBusy(''); });
    },
    /* A one-line status above the list. A big folder takes as long as it takes; what it must not do
     * is look identical to a hung screen. */
    setBusy(text){
      const box=$('#mail-items', this.root); if(!box) return;
      let bar=$('.mail-busy', this.root);
      if(!text){ if(bar) bar.remove(); return; }
      if(!bar){
        bar=document.createElement('div'); bar.className='mail-busy';
        box.parentElement.insertBefore(bar, box);
      }
      bar.innerHTML=`<span class="mail-busy-dot"></span>${enc(text)}`;
    },
    /* Append the next page. Kept separate from loadList so paging never re-reads (or re-decrypts)
     * what is already on screen — the point of a cursor. */
    async loadMore(){
      if(this.q || !this._next || this._paging) return;
      const seq=this._listSeq, root=this.root, account=this.acct, folder=this.folder, cursor=this._next;
      const current=()=>seq===this._listSeq&&root===this.root&&account===this.acct&&folder===this.folder&&!this.q;
      const paging = {};
      this._paging = paging;
      const btn=$('#mail-more-btn', this.root);
      if(btn){ btn.disabled=true; btn.textContent='Loading…'; }
      try{
        const r = await this.api('/messages?account='+encodeURIComponent(account)
                                 +'&folder='+encodeURIComponent(folder)
                                 +'&until='+encodeURIComponent(cursor));
        if(!current())return;
        const seen=new Set(this.msgs.map(m=>this._key(m)));
        for(const m of (r.messages||[])) if(!seen.has(this._key(m))){this.msgs.push(m);seen.add(this._key(m));}
        this._next = r.next_until || 0;
        this.drawList();
      }catch(_){ if(current()&&btn){ btn.disabled=false; btn.textContent='Load older'; } }
      finally{if(this._paging===paging)this._paging = false;}
    },
    async loadList(){
      const box=$('#mail-items', this.root); if(!box) return;
      const seq=++this._listSeq, root=this.root, account=this.acct, folder=this.folder, query=this.q;
      // Retire the previous list's page even when its request never finishes.
      this._paging = false; this._next = 0;
      // Only spin when there is nothing to look at. Opening the screen runs draw → loadList → sync →
      // loadList, and blanking to a spinner each time made the whole list flash and jump twice
      // before settling. A refresh over an existing list swaps the rows in place instead.
      if(!this.msgs.length) box.innerHTML='<div class="spinner"></div>';
      try{
        const r = query
          ? await this.api('/search?q='+encodeURIComponent(query))
          : await this.api('/messages?account='+encodeURIComponent(account)+'&folder='+encodeURIComponent(folder));
        if(seq!==this._listSeq || root!==this.root || account!==this.acct || folder!==this.folder || query!==this.q) return;
        this.msgs=r.messages||[];
        this._next=query?0:(r.next_until||0);
        this._listError='';
        if(query)this.convSent=[];
      }catch(_){
        if(seq!==this._listSeq || root!==this.root || account!==this.acct || folder!==this.folder || query!==this.q) return;
        this.msgs=[]; this._next=0;
        this._listError=query?'Could not search email. Try again.':'Could not load email. Try again.';
      }
      this.drawList();
      this.loadConvSent(seq);
    },
    /* YOUR HALF OF THE CONVERSATION, IN THE LIST.
     *
     * Asked for more times than anything else here: "email is still not showing my part of the
     * conversation in the message list". The reader has threaded both sides for a while — what was
     * missing is one step earlier. `loadList` fetches ONE folder, so in the Inbox the rows are built
     * from received mail only: a conversation you have answered three times still shows their last
     * message, with no sign that you replied at all.
     *
     * So the Sent folder is fetched alongside and merged into the ROWS — never into `msgs`. That
     * distinction is load-bearing: `msgs` drives paging, the cursor, and the checkbox keys that
     * delete things. Putting sent mail in there would make "delete this conversation" delete your
     * own copies too, which is not what anybody selecting a row in their Inbox means.
     *
     * Skipped in the Sent folder itself (it is already both halves) and while searching (the search
     * already spans folders). */
    async loadConvSent(seq){
      const sent = this._sentFolder();
      if(this.q || !sent || this.folder === sent || this.folder === 'Sent' || this.folder === 'Drafts'){
        this.convSent = []; return;
      }
      const account = this.acct, folder = this.folder, root = this.root;
      try{
        const r = await this.api('/messages?account='+encodeURIComponent(account)
                                 +'&folder='+encodeURIComponent(sent));
        if(seq!==this._listSeq || root!==this.root || account!==this.acct || folder!==this.folder) return;
        this.convSent = r.messages || [];
      }catch(_){
        /* A Sent folder that cannot be read leaves the list exactly as it was — their side alone is
           the old behaviour, and it is better than an empty list. */
        return;
      }
      this.drawList();
    },
    /* The IMAP name of the Sent folder for this account — 'Sent', 'Sent Messages' and 'INBOX.Sent'
     * are all in the wild, so it is resolved from the server's own labelling rather than guessed. */
    _sentFolder(){
      for(const f of Object.keys(this.folderLabels || {})){
        if(String(this.folderLabels[f]).includes('Sent')) return f;
      }
      return (this.folders || []).find(f => /(^|[./])sent/i.test(f)) || 'Sent';
    },
    async syncSent(account){
      const folder=this._sentFolder();
      try{
        await this.api('/sync-folder',{method:'POST',headers:{'Content-Type':'application/json'},
                                       body:JSON.stringify({account: account||this.acct, folder})});
      }catch(_){}
      if(this.folder===folder || this.folder==='Sent' || this.folder==='Drafts') this.loadList();
      else if(this.root && this.root.isConnected && this.openUid && this.openFolder)
        this.open(this.openUid, this.openFolder, this.openAccount||account||this.acct||'__all');
    },
    _key(m){ return (m.account||this.acct)+'|'+(m.folder||this.folder)+'|'+m.uid; },
    /* THE POINT OF A THREAD IS THAT IT IS ONE ROW.
     *
     * Reported as "threading is showing multiple messages in Inbox, the point of threads is to
     * consolidate". The reader has grouped a conversation for a while; the LIST never did, so a
     * back-and-forth with one person filled the screen with near-identical rows and the newest was
     * wherever it happened to fall.
     *
     * Grouped on the normalised subject — every reply/forward prefix stripped, not just the first,
     * because mail accretes them ("Re: Re: Fwd: quote" is ordinary after a few round trips). It is
     * the SAME rule the server threads with, so the list and the reader agree about what one
     * conversation is; disagreeing would be worse than not grouping at all.
     *
     * A message with no usable subject is its own row: grouping those together would put every
     * subject-less message in the mailbox under one heading. */
    _convKey(m){
      const raw = String((m && m.subject) || '');
      const norm = raw.replace(/^(?:\s*(?:re|fwd|fw)\s*:\s*)+/i, '').trim().toLowerCase();
      return norm ? JSON.stringify([m.account||this.acct,'subj',norm]) : 'uid:' + this._key(m);
    },
    /* One entry per conversation, newest first, each carrying the messages it stands for.
     * `this.msgs` is already newest-first, so first-seen order IS newest-first. */
    _conversations(){
      const out = [], byKey = new Map();
      for(const m of (this.msgs || [])){
        const k = this._convKey(m);
        const seen = byKey.get(k);
        if(seen){ seen.all.push(m); if(!m.read) seen.unread = true; continue; }
        const row = { key:k, head:m, all:[m], mine:[], unread:!m.read };
        byKey.set(k, row); out.push(row);
      }
      /* Your replies join a conversation that is ALREADY here; they never start a row of their own.
         A message you sent that nobody answered belongs in Sent, not in the Inbox. */
      for(const m of (this.convSent || [])){
        const row = byKey.get(this._convKey(m));
        if(row) row.mine.push(m);
      }
      for(const row of out){
        /* The row shows the LATEST message either way — that is what makes your reply visible.
           `head` stays whatever is newest; `all` (and therefore the checkbox keys) is untouched. */
        let newest = row.head;
        for(const m of row.mine) if((m.ts || 0) > (newest.ts || 0)) newest = m;
        row.head = newest;
        row.headIsMine = row.mine.indexOf(newest) >= 0;
        row.count = row.all.length + row.mine.length;
      }
      return out;
    },
    drawList(){
      const box=$('#mail-items', this.root); if(!box) return;
      this.sel=this.sel||new Set();
      if(!this.msgs.length){ box.innerHTML='<div class="empty">'+(this._listError||(this.q?'No matches across your accounts.':'No messages.'))+'</div>'; this.updateBulk(); return; }
      // Unified mode uses the logical name and has no per-account folderLabels map. Treat it as
      // Sent too, or its rows show the sender (yourself) instead of the useful "To:" recipient.
      const isSent=!this.q&&(this.folder==='Sent'||this.folderLabels[this.folder]==='📤 Sent'), unified=this.acct==='__all'||!!this.q;
      /* ONE ROW PER CONVERSATION. The reader has grouped a thread for a while and the list did
         not, so a back-and-forth filled the screen with near-identical rows — "the point of threads
         is to consolidate". The row shows the NEWEST message and a count; the checkbox selects the
         whole conversation, because deleting half of one is not something anybody means to do. */
      const convs = this._conversations();
      box.innerHTML=convs.map(c=>{ const m=c.head, keys=c.all.map(x=>this._key(x)), key=keys[0];
        const cur = (this.msgs.indexOf(c.all[0]) === this.cursor) ? ' cursor' : '';
        const openInThis = c.all.concat(c.mine || []).some(x => String(x.uid) === String(this.openUid) && (x.account||this.acct)===(this.openAccount||this.acct) && (x.folder||this.folder)===(this.openFolder||this.folder));
        const n = c.count || c.all.length;
        /* A row whose newest message is YOURS says so, the way every mail client does — otherwise
           the reply you just sent looks like another message from them. */
        const mineHead = !!c.headIsMine;
        /* Opening addresses a message THIS FOLDER holds. `head` may be your own reply, which
           lives in Sent — opening by that uid would ask the Inbox for a uid it does not have. */
        const openM = c.all[0] || m;
        return `<div class="mail-item${c.unread?' unread':''}${cur}${openInThis?' active':''}" data-uid="${enc(String(openM.uid))}" data-folder="${enc(openM.folder||this.folder)}" data-account="${enc(openM.account||'')}" data-key="${enc(key)}" data-keys="${enc(keys.join(','))}">
        <input type="checkbox" class="mi-chk"${keys.every(k=>this.sel.has(k))?' checked':''}>
        <div class="mi-content">
          <div class="mi-row"><span class="mi-from">${unified?`<span class="mi-acct">${enc(m.account||'')}</span> `:''}${mineHead?'<span class="mi-you">You:</span> ':''}${enc(((isSent||mineHead)?('To: '+(m.to||'')):(m.from||'')).slice(0,42))}${n>1?`<span class="mi-count" title="${n} messages in this conversation">${n}</span>`:''}</span><span class="mi-date">${enc(_mailDate(m.ts))}</span></div>
          <div class="mi-subj">${c.all.concat(c.mine||[]).some(x=>x.attachments)?'📎 ':''}${enc(m.subject||'(no subject)')}</div>
          <div class="mi-prev muted small">${enc(m.preview||'')}</div>
        </div></div>`; }).join('');
      if(this._next){
        box.insertAdjacentHTML('beforeend',
          `<button class="btn btn-ghost full" id="mail-more-btn">Load older</button>`);
        const mb=$('#mail-more-btn', this.root); if(mb) mb.onclick=()=>this.loadMore();
      }
      $$('.mail-item',box).forEach(el=>{
        /* The checkbox selects the WHOLE conversation — every message the row stands for. Acting
           on only the newest would delete or move half a thread and leave the rest behind. */
        const cb=el.querySelector('.mi-chk'); if(cb) cb.onclick=(e)=>{ e.stopPropagation();
          const keys=String(el.dataset.keys||el.dataset.key||'').split(',').filter(Boolean);
          for(const k of keys){ if(cb.checked) this.sel.add(k); else this.sel.delete(k); }
          this.updateBulk(); };
        const c=el.querySelector('.mi-content');
        if(c) c.onclick=()=>{ this.cursor=this.msgs.findIndex(m=>this._key(m)===el.dataset.key);
                              this.open(el.dataset.uid, el.dataset.folder, el.dataset.account); };
      });
      this.updateBulk();
    },
    updateBulk(){
      const sa=$('#mail-selall',this.root), act=$('#mail-bulk-act',this.root); const n=this.sel?this.sel.size:0;
      // Ticked whenever anything is selected, dashed when it is only some of the list. The old
      // `n===this.msgs.length` drew an EMPTY box over a full selection whenever the list had grown,
      // which is both a lie and (before the handler stopped reading it) the bug above.
      if(sa){ sa.checked = n>0; sa.indeterminate = n>0 && n!==this.msgs.length; }
      if(!act) return;
      act.innerHTML = n ? `<span class="mail-bulk-n">${n} selected</span><button class="btn small" data-bulk="read">● Read</button><button class="btn small" data-bulk="archive">🗄 Archive</button><button class="btn btn-red small" data-bulk="delete">🗑 Delete</button>` : '';
      act.querySelectorAll('[data-bulk]').forEach(b=> b.onclick=()=>this.bulk(b.dataset.bulk));
    },
    async bulk(action){
      if(!this.sel || !this.sel.size) return;
      if(action==='delete' && !await uiConfirm('Delete '+this.sel.size+' message(s)?',
                                                { ok:'Delete', danger:true })) return;
      const keys=[...this.sel]; this.sel.clear(); this.updateBulk();
      const path = action==='read' ? '/mark-read' : '/'+action;
      for(const k of keys){ const i=k.indexOf('|'), j=k.indexOf('|', i+1);
        const account=k.slice(0,i), folder=k.slice(i+1,j), uid=k.slice(j+1);
        const body={account, folder, uid}; if(action==='read') body.read=true;
        try{ await this.api(path,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}); }catch(_){}
      }
      toast(action==='read'?'marked read':(action==='delete'?'deleted':'archived')); this.loadList();
    },
    async open(uid, folder, account){
      /* `__all` WHEN NO ACCOUNT IS SELECTED, never the string "null".
       *
       * `this.acct` is null in the All-inboxes view, and `encodeURIComponent(null)` is the four
       * characters n-u-l-l. The server has no account by that name, so /thread answered 404 and the
       * `.catch(()=>{})` below threw it away: the conversation silently never upgraded past the one
       * message that was clicked. Reported as "webui not showing sent items in the thread" — the
       * threading was right, the request asking for it was not. */
      folder=folder||this.folder; const acct=account||this.acct||'__all';
      const openSeq=this._openSeq=(this._openSeq||0)+1, root=this.root;
      const current=()=>openSeq===this._openSeq&&root===this.root&&this.openUid===uid&&this.openAccount===acct&&this.openFolder===folder;
      this.openUid=uid; this.openFolder=folder; this.openAccount=acct; this.drawList();
      let msg; try{ const r=await this.api('/message?account='+encodeURIComponent(acct)+'&folder='+encodeURIComponent(folder)+'&uid='+encodeURIComponent(uid)); msg=r.message; }catch(_){}
      if(!current())return;
      if(folder==='Drafts'){   // a draft opens back into the composer (prefilled) rather than a read pane
        if(msg) this.compose({mode:'draft', draft:msg, acct});
        else toast('draft unavailable');
        return;
      }
      const pane=$('#mail-read', this.root); if(!pane) return; pane.innerHTML='<div class="spinner"></div>'; pane.classList.add('has-open');
      if(!msg){ pane.innerHTML='<div class="empty">Could not load this message.</div>'; return; }
      /* PAINT THE CONVERSATION FROM WHAT THIS PAGE ALREADY HOLDS, before asking the server.
       *
       * "the email thread should show sent messages in order with the replies" — it does, and it
       * always did; what it did not do was show them for the first ELEVEN SECONDS. /thread walks
       * the whole mailbox (measured at 11.0s cold on the reporting account, 17,921 documents of
       * NIP-44 decrypts), and until it answered the reader showed exactly one message: theirs. Ten
       * reports of "you are only showing their side" describe that window, not the threading.
       *
       * The list has already fetched the Sent folder for its conversation rows, so both halves are
       * in memory here. Sorted oldest-first, which is what "in order with the replies" means, and
       * replaced wholesale when /thread answers with the authoritative set.
       *
       * It states nothing it has not checked: with nothing cached this is the seed alone and the
       * "Loading…" line, exactly as before. */
      const _seedKey = this._convKey(msg);
      const _local = [];
      for(const x of (this.msgs || []).concat(this.convSent || [])){
        if(this._key(x) === this._key(msg)) continue;              // the seed is added below
        if(this._convKey(x) === _seedKey) _local.push(x);
      }
      _local.push(msg);
      _local.sort((a, b) => (a.ts || 0) - (b.ts || 0));
      this._renderThread(pane, _local, folder, acct, uid, _local.length > 1 ? 'loading' : 'loading');
      this.api('/thread?account='+encodeURIComponent(acct)+'&folder='+encodeURIComponent(folder)+'&uid='+encodeURIComponent(uid))
        .then(t=>{ if(!(current() && pane.isConnected)) return;
          const got = (t && t.messages) || [];
          /* Re-render either way: with the conversation when there is one, and WITHOUT the
             "Loading…" line when there is not — a spinner that never resolves is the same lie as
             no message at all. */
          if(got.length>1) this._renderThread(pane, got, folder, acct, uid);
          /* The server found nothing more — but this page may already be showing the sent side it
             matched locally, and replacing that with the seed alone would take the conversation
             back off the screen. */
          else if(_local.length > 1) this._renderThread(pane, _local, folder, acct, uid);
          else this._renderThread(pane, [msg], folder, acct, uid, 'alone'); })
        /* SAY SO. Swallowing this is what made a malformed account silent for as long as it was
           wrong — the message still opened, so nothing looked broken; the conversation just never
           arrived. The read itself must not fail, so it stays caught, but not in silence. */
        .catch(e=>{ try{ console.warn('[mail] conversation could not be loaded:', (e&&e.message)||e); }catch(_){ }
          try{ if(current() && pane.isConnected){
            const st=pane.querySelector('.mail-convo-state');
            if(st) st.textContent='The rest of this conversation could not be loaded';
          } }catch(_){ } });
    },
    /* Plain-text mail had no links at all — the body was escaped and printed, so a URL was a string
     * you had to select and copy. Run over the ALREADY-ESCAPED text, so nothing here can introduce
     * markup: the only thing it adds is an anchor around a run that is already inert. */
    _linkify(t){ return String(t||'').replace(/(^|[\s(])((?:https?:\/\/|www\.)[^\s<>"']+[^\s<>"'.,;:!?)])/g,
      (m0, pre, url) => pre + '<a href="' + (/^www\./i.test(url) ? 'https://' + url : url)
        + '" target="_blank" rel="noopener noreferrer">' + url + '</a>'); },
    /* AN EMAIL IS READ ON THE PAGE, NOT THROUGH A PORTHOLE.
     *
     * HTML mail renders in a sandboxed iframe, which is right — it is somebody else's markup. But
     * an iframe does not size to its content, so it kept a fixed 62dvh box with its own scrollbar
     * inside the page's scrollbar. On a phone that is a ~520px window onto a long message, which is
     * exactly the report: "mobile UI leaves a small window to actually read the message".
     *
     * `allow-same-origin` is what makes the height READABLE from here. It is safe only because
     * `allow-scripts` is NOT granted and must never be: with scripts off nothing executes inside
     * the frame, so it cannot use that origin for anything. The two together would be untrusted
     * mail HTML running as us — `tests/test_mail_reads_on_the_page.py` fails if they ever meet.
     *
     * Re-measured after load and on a couple of beats afterwards, because images and webfonts
     * arrive late and each one changes the height. */
    _sizeMailFrames(root){
      const frames = $$('iframe[data-mail-autosize]', root || document);
      const fit = el => {
        try{
          const doc = el.contentDocument;
          if(!doc || !doc.documentElement) return;
          const h = Math.max(doc.documentElement.scrollHeight || 0, doc.body ? doc.body.scrollHeight : 0);
          const want = Math.min(h + 8, 20000);
          /* ONLY WRITE A HEIGHT THAT CHANGED. Setting the frame's height re-lays out the document
             inside it, so an unconditional write is a feedback loop with anything that watches the
             document — which is exactly what a ResizeObserver here produced: an endless storm of
             "ResizeObserver loop completed with undelivered notifications". */
          if(h > 0 && Math.abs(want - (parseFloat(el.style.height) || 0)) > 2){
            /* `flex:1` is set on a single-message thread's body so it fills the pane. In a flex
               column that resolves flex-basis:0 and GROWS the item, which beats an inline height —
               so the frame would go back to being a fixed box with its own scrollbar, exactly the
               porthole this removes. Sized frames opt out of the stretch. */
            el.style.flex = 'none';
            el.style.height = want + 'px';
          }
        }catch(_){ /* a frame that will not be measured keeps the stylesheet's height */ }
      };
      for(const el of frames){
        fit(el);
        el.addEventListener('load', () => fit(el), { once: true });
        /* Timed re-measures rather than a ResizeObserver ON THE DOCUMENT. Observing the document
           and then setting the frame's height re-lays that same document out, so the observer fires
           again — a loop the browser reports as "ResizeObserver loop completed with undelivered
           notifications", forever, on every open mail. Images and webfonts are what change the
           height, and they arrive within a couple of seconds; a few beats cover them without
           standing in a feedback loop. */
        [120, 400, 1200, 2500, 5000].forEach(ms => setTimeout(() => { if(el.isConnected) fit(el); }, ms));
      }
    },
    _msgBlock(m, folder, acct, expanded){
      /* A relative download URL belongs to the page that rendered it. That is correct on the web,
       * but packaged clients render at app://posterchan (desktop) or https://localhost (Android),
       * neither of which hosts Mail. Always bind the attachment to the configured instance. */
      /* `attachments` IS TWO DIFFERENT THINGS WITH ONE NAME, and this is where they collide.
       *
       * The full message carries the LIST — `[{name,type,size}]`. The list-view projection
       * (`_summary` in app/routers/mail.py) carries a COUNT, because the row only needs to know
       * whether to draw a paperclip.
       *
       * AND THE CACHE-FIRST OPEN MIXES THEM. `openMsg` paints the conversation immediately from
       * `this.msgs` + `this.convSent` — list rows — with the fully-fetched seed pushed on top, and
       * upgrades when /thread answers eleven seconds later. So every SIBLING in that first paint
       * carries a count, and `(2||[]).map` throws `attachments.map is not a function`. The whole
       * reader renders nothing: reported as "can't open message", for a message whose body was
       * fine. It only bites a mail that is part of a CONVERSATION, which is why "other emails open
       * with attachments but not that one".
       *
       * `||[]` reads like a guard and is not one: it catches null and undefined, and a NUMBER sails
       * straight through it. Ask what the value IS. The sibling loses its paperclip for the moment
       * before /thread replaces it with the real document — which is what it was going to do.
       *
       */
      const attList = Array.isArray(m.attachments) ? m.attachments : [];
      const atts=attList.map((at,i)=>{
        const name=String(at.name||'attachment'), type=String(at.type||'application/octet-stream');
        const pv=_previewable(name,type);
        const url=_mailAttachmentUrl(m,folder,acct,i);
        return `<a class="mail-att" data-mail-attachment="1" data-mail-url="${enc(url)}" href="${enc(url||'#')}" target="_blank" rel="noopener" data-name="${enc(name)}" data-mime="${enc(type)}"${pv?' data-mail-preview="1"':''}>📎 ${enc(name)} <span class="muted small">${_fmtBytes(at.size||0)}</span></a>`;
      }).join('');
      /* Untrusted email HTML → sandboxed iframe (no scripts, no forms, no same-origin); else text.
       *
       * A LINK IN AN EMAIL OPENS IN THE BROWSER. With a bare `sandbox` a click did nothing at all:
       * the frame may not navigate itself (no allow-top-navigation, correctly) and may not open a
       * window (no allow-popups), so every link in every HTML mail was silently inert. `allow-popups`
       * plus `allow-popups-to-escape-sandbox` is the narrow grant that fixes it — the opened page is
       * a normal browser tab rather than another sandboxed frame — and `<base target="_blank">` is
       * what makes an ordinary <a href> take that route instead of trying to navigate in place.
       * Scripts and same-origin stay OFF, which is what actually keeps this safe.
       *
       * Same rule as a web-search result, and for the same reason: it is somebody else's page. */
      /* nostr-mail: armor in the text body wins over the HTML rendering — the text/plain part is
       * the spec's source of truth, and the HTML part of such mail is a rendering aid only. */
      const nm = (m.body_text && m.body_text.indexOf('BEGIN NOSTR') >= 0) ? NMail.parse(m.body_text) : null;
      const body = nm
        ? this._nmailHtml(nm, m)
        : (m.body_html
        ? `<iframe class="mail-html" referrerpolicy="no-referrer" data-mail-autosize="1"
                   sandbox="allow-popups allow-popups-to-escape-sandbox allow-same-origin"
                   srcdoc="${enc('<base target="_blank"><meta name="referrer" content="no-referrer">' + m.body_html)}"></iframe>`
        : `<div class="mail-text">${this._linkify(enc(m.body_text||'')).replace(/\n/g,'<br>')}</div>`);
      const sender=String(m.from||m.from_email||'').replace(/\s*<[^>]*>\s*$/,'').trim()||String(m.from_email||'?');
      const initial=Array.from(sender)[0]||'?';
      const preview=String(m.preview||m.body_text||'').replace(/\s+/g,' ').trim().slice(0,110);
      return `<div class="mail-msg${expanded?' open':''}">
        <div class="mail-msg-hd" role="button" tabindex="0" aria-expanded="${expanded?'true':'false'}">
          <span class="mm-avatar" aria-hidden="true">${enc(initial.toUpperCase())}</span>
          <div class="mm-who"><b class="mm-sender" data-from="${enc(m.from_email||m.from||'')}" data-name="${enc(m.from||'')}" title="View sender">${enc(m.from||'')}</b><div class="muted small">To: ${enc((m.to||'').slice(0,90))}</div></div>
          ${preview?`<span class="mm-preview muted">${enc(preview)}</span>`:''}<span class="muted small mm-date">${enc(_mailDate(m.ts))}</span><span class="mm-chevron" aria-hidden="true">⌄</span>
        </div>
        <div class="mail-msg-body">${atts?`<div class="mail-atts">${atts}</div>`:''}<div class="mail-body">${body}</div></div>
      </div>`;
    },
    /* The nostr-mail card. Decryption is a signer round trip, so the card paints its state first
     * and swaps the plaintext in when it lands — the same rule every slow surface here follows. */
    _nmailHtml(nm, m){
      const who = nm.name ? '@' + nm.name : (nm.pubkey ? nm.pubkey.slice(0, 12) + '\u2026' : 'unknown sender');
      const head = `<div class="muted small">\ud83d\udd10 nostr-mail \u00b7 ${enc(who)}`
                 + (nm.signed ? ' \u00b7 signature present (not verified here yet)' : '') + `</div>`;
      if(nm.kind === 'nip04'){
        return `<div class="mail-text">${head}<div class="nt-warn" style="margin-top:8px">This message uses NIP-04, `
             + `which must be signature-verified BEFORE decryption (spec \u00a74.1) \u2014 and this client `
             + `cannot verify raw signatures yet, so it will not decrypt it. Ask the sender to use NIP-44.</div>`
             + (nm.plainAbove ? `<div style="margin-top:8px">${this._linkify(enc(nm.plainAbove)).replace(/\n/g,'<br>')}</div>` : '') + `</div>`;
      }
      if(nm.kind === 'signed' || nm.kind === 'sealed-plain' || nm.glossia || !nm.cipher || !nm.pubkey){
        const why = nm.kind === 'signed' ? '' :
          (nm.glossia ? `<div class="muted small" style="margin-top:6px">Part of this message is glossia-encoded, which this client cannot decode yet.</div>` :
           (!nm.pubkey ? `<div class="muted small" style="margin-top:6px">No readable sender key in the SEAL/SIGNATURE block \u2014 cannot decrypt.</div>` : ''));
        return `<div class="mail-text">${head}${why}`
             + (nm.plainAbove ? `<div style="margin-top:8px">${this._linkify(enc(nm.plainAbove)).replace(/\n/g,'<br>')}</div>`
                              : `<div class="muted small" style="margin-top:8px">(no plaintext copy in this message)</div>`) + `</div>`;
      }
      const id = 'nm-' + Math.random().toString(36).slice(2, 8);
      setTimeout(() => this._nmailReveal(id, nm), 0);
      return `<div class="mail-text" id="${id}">${head}<div class="muted small" style="margin-top:8px"><span class="spinner"></span> decrypting\u2026 your signer may need to approve</div></div>`;
    },
    async _nmailReveal(id, nm){
      /* `signer` is the app's own binding — inside app.js there is no `PC`, the trap this file has
       * now sprung twice on other features. */
      let out = null, err = '';
      try{
        if(!S.signer || !S.signer.nip44dec) throw new Error('this login method can\u2019t decrypt (needs NIP-44)');
        out = await S.signer.nip44dec(nm.pubkey, nm.cipher);
      }catch(e){ out = null; err = (e && e.message) || String(e); }
      const el = document.getElementById(id); if(!el) return;
      const head = el.querySelector('.muted.small');
      const headHtml = head ? head.outerHTML : '';
      el.innerHTML = headHtml + (out != null
        ? `<div style="margin-top:8px">${this._linkify(enc(out)).replace(/\n/g,'<br>')}</div>`
        : `<div class="nt-warn" style="margin-top:8px">couldn\u2019t decrypt: ${enc(err || 'the signer did not answer')} \u2014 was this encrypted to your key?</div>`);
    },
    _renderThread(pane, thread, folder, acct, seedUid, convo){
      const latest=thread[thread.length-1];
      // actions target the message the user actually OPENED (the seed), not just the newest in the thread
      const target=thread.find(m=>String(m.uid)===String(seedUid)) || latest;
      pane.innerHTML=`<div class="mail-read-hd"><button class="mini mail-back" id="mail-back" title="Back">←</button>
          <div class="mr-meta"><div class="mr-subj">${enc(latest.subject||'(no subject)')}</div>
            ${thread.length>1?`<div class="muted small">${thread.length} messages</div>`
              /* SILENCE IS BEING READ AS ABSENCE, and that is the whole of "still not showing my
                 part of the conversation", asked about ten times. The conversation IS found — the
                 seed is painted at once and upgraded when /thread answers — but that call walks the
                 whole mailbox, MEASURED AT 11.0s cold on the reporting account (17,921 documents of
                 NIP-44 decrypts; 0.000s once the 60s cache is warm, and every service restart
                 empties it). For eleven seconds the reader showed one message and said nothing, so
                 the only available conclusion was that the sent mail is missing. Now it says it is
                 looking, and says so when it has looked and there was nothing. */
              : (convo === 'loading'
                  ? `<div class="muted small mail-convo-state">Loading the rest of this conversation…</div>`
                  : convo === 'alone'
                    ? `<div class="muted small mail-convo-state">No other messages in this conversation</div>`
                    : '')}</div></div>
        <!-- SPRITE ICONS, NOT EMOJI. This row was a filing-cabinet, a wastebasket and an arrow — glyphs from
             the font, on a screen that has an icon set precisely so the UI does not depend on one.
             A platform without the emoji font (a minimal Gentoo install, a WebView) draws them as
             nothing at all, which is how it was reported: "i open email app and none of the bottom
             buttons have icons". The arrows fared no better: ↩↩ is two characters pretending to be
             a symbol. Every name here is defined in sprite.js — an icon that is not renders as
             blank space with no error, which is the same bug one layer down. -->
        <div class="mail-actions">
          <button class="btn btn-cyan small icon-only" data-act="reply" title="Reply" aria-label="Reply"><svg class="ic b-ic" aria-hidden="true"><use href="#i-reply"></use></svg></button>
          <button class="btn small icon-only" data-act="replyall" title="Reply all" aria-label="Reply all"><svg class="ic b-ic" aria-hidden="true"><use href="#i-repost"></use></svg></button>
          <button class="btn small icon-only" data-act="forward" title="Forward" aria-label="Forward"><svg class="ic b-ic" aria-hidden="true"><use href="#i-forward"></use></svg></button>
          <button class="btn small icon-only" data-act="ai" title="AI tools" aria-label="AI tools"><svg class="ic b-ic" aria-hidden="true"><use href="#i-wand"></use></svg></button>
          <button class="btn small icon-only" data-act="unread" title="Mark unread" aria-label="Mark unread"><svg class="ic b-ic" aria-hidden="true"><use href="#i-eye"></use></svg></button>
          <button class="btn small icon-only" data-act="move" title="Move" aria-label="Move"><svg class="ic b-ic" aria-hidden="true"><use href="#i-folder"></use></svg></button>
          <button class="btn btn-red small icon-only" data-act="delete" title="Delete" aria-label="Delete"><svg class="ic b-ic" aria-hidden="true"><use href="#i-trash"></use></svg></button>
        </div>
        <div class="mail-thread">${thread.map((m,i)=>this._msgBlock(m, folder, acct, i===thread.length-1 || String(m.uid)===String(seedUid))).join('')}
          <div class="mail-thread-reply"><button class="btn btn-cyan" data-thread-reply="reply"><svg class="ic b-ic" aria-hidden="true"><use href="#i-reply"></use></svg> Reply</button><button class="btn" data-thread-reply="forward"><svg class="ic b-ic" aria-hidden="true"><use href="#i-forward"></use></svg> Forward</button></div>
        </div>`;
      // Size every HTML body to its content so the PAGE scrolls, not a box inside it.
      try{ this._sizeMailFrames(pane); }catch(_){ }
      $('#mail-back',pane).onclick=()=>{ pane.classList.remove('has-open'); this.openUid=null; this.drawList(); };
      $$('.mail-msg .mail-msg-hd',pane).forEach(hd=> hd.onclick=(e)=>{
        // The sender's name is a button inside the header, and the header collapses the message.
        // Without this, asking who sent it also folds away what they wrote.
        if(e.target.closest('.mm-sender')) return;
        const open=hd.parentElement.classList.toggle('open');
        hd.setAttribute('aria-expanded',open?'true':'false');
      });
      $$('.mail-msg .mail-msg-hd',pane).forEach(hd=>hd.onkeydown=e=>{
        if(e.key==='Enter'||e.key===' '){e.preventDefault();hd.click();}
      });
      $$('.mm-sender',pane).forEach(b=> b.onclick=(e)=>{ e.stopPropagation(); this.senderCard(b.dataset.from, b.dataset.name); });
      $$('[data-act]',pane).forEach(b=> b.onclick=()=>this.action(b.dataset.act, target, target.folder||folder, target.account||acct));
      $$('[data-thread-reply]',pane).forEach(b=>b.onclick=()=>this.action(b.dataset.threadReply,latest,latest.folder||folder,latest.account||acct));
      $$('[data-mail-attachment]',pane).forEach(a=> a.onclick=async e=>{
        e.preventDefault();
        await _openMailAttachment(a);
      });

    },
    /* The open message as plain text, headers first — what every AI action reads. HTML mail is
     * reduced with DOMParser, never by reading the rendered iframe: that frame is sandboxed WITHOUT
     * allow-same-origin (it is somebody else's page), so its document is unreachable from here, and
     * DOMParser parses without executing anything. */
    _msgText(msg){
      let body = String(msg.body_text || '');
      if(!body.trim() && msg.body_html){
        try{ body = new DOMParser().parseFromString(String(msg.body_html), 'text/html').body.innerText || ''; }
        catch(_){ body = String(msg.body_html).replace(/<[^>]+>/g, ' '); }
      }
      if(!body.trim()) return '';
      /* To: is the line that knows the USER'S name — without it the model literally cannot sign
       * a reply correctly, which is where every [Your Name] placeholder came from. */
      return 'Subject: ' + (msg.subject || '') + '\nFrom: ' + (msg.from || msg.from_email || '')
           + '\nTo: ' + (msg.to || '')
           + '\nDate: ' + (msg.ts ? new Date(msg.ts * 1000).toISOString().slice(0, 10) : '')
           + '\n\n' + body.slice(0, 16000);
    },
    // window.__PC.authFetch's exact shape, called locally — inside app.js there is no `PC` binding.
    async _aiPost(url, payload){
      await ensureAiSession();
      const isForm = (typeof FormData !== 'undefined') && (payload instanceof FormData);
      const r = await fetch(url, { method:'POST', body: isForm ? payload : JSON.stringify(payload),
                                   credentials:'include',
                                   headers: Object.assign(isForm ? {} : {'Content-Type':'application/json'},
                                                          S._aiToken ? {'Authorization':'Bearer '+S._aiToken} : {}) });
      if(!r.ok){
        let why=''; try{ why=((await r.json())||{}).detail||''; }catch(_){}
        throw new Error(r.status===401||r.status===403 ? 'your account can\u2019t use AI features'
                        : (why || 'the request failed'));
      }
      return r.json();
    },
    /* A MODAL THAT LIVES AS LONG AS THE WAIT, not a toast that outlives nothing. An AI call from
     * mail is a signer round trip (approving on a phone takes as long as it takes) followed by a
     * model that may have to LOAD first — the LLM deliberately unloads when idle on these nodes, so
     * the first answer after a quiet spell can take a minute. A two-second toast in front of that
     * reads as "nothing happened", which is exactly how it was reported. The modal shows the wait,
     * names the slow parts, and a failure lands IN it instead of nowhere. */
    _aiHold(title, what){
      modal(`<h3>${title}</h3>
        <div id="ma-hold" class="mail-ai-summary"><span class="spinner"></span> ${enc(what)}\u2026
          <div class="muted small" style="margin-top:8px">Your signer may need to approve, and the first
          answer after a quiet spell loads the model \u2014 this can take up to a minute.</div></div>`);
      return {
        fail: (err) => { const h=$('#ma-hold'); if(h) h.innerHTML =
          '\u26a0\ufe0f ' + enc((err && err.message) || String(err) || 'that didn\u2019t work'); },
        done: () => closeModal(),
      };
    },
    async aiSummarize(msg){
      const text = this._msgText(msg);
      if(!text){ toast('this message has no text to read'); return; }
      const hold = this._aiHold('\ud83d\udcdd Summary', 'reading the email');
      try{
        const d = await this._aiPost('/api/mail/ai', { mode:'summarize', text });
        hold.done();
        modal(`<h3>\ud83d\udcdd Summary</h3>
          <div class="mail-ai-summary">${enc(String(d.content||''))}</div>
          <div class="row" style="margin-top:10px"><button class="btn btn-ghost small" id="ma-copy">\ud83d\udccb Copy</button></div>`,
          root => { const c=$('#ma-copy',root); if(c) c.onclick=()=>copyValue(String(d.content||'')); });
      }catch(err){ hold.fail(err); }
    },
    async aiReply(msg, folder, acct){
      const instr = await uiPrompt('How should it reply?\n\nE.g. \u201cpolitely decline\u201d, '
        + '\u201csay yes, ask for the invoice as PDF\u201d, \u201cshort thank-you\u201d.', { ok:'Draft it' });
      if(instr === null) return;
      if(!String(instr).trim()){ toast('say how to reply'); return; }
      const text = this._msgText(msg);
      if(!text){ toast('this message has no text to read'); return; }
      const hold = this._aiHold('\u21a9\ufe0f AI reply', 'drafting your reply');
      try{
        /* The user's own name, from the To header's display name — the ONLY grounded source. An
         * ungrounded model signed a real reply "Best, Jordan": a person who does not exist. */
        const myName = ((String(msg.to || '').match(/^\s*"?([^"<@]+?)"?\s*</) || [])[1] || '').trim();
        const d = await this._aiPost('/api/mail/ai', { mode:'reply', text, instruction: String(instr).trim(),
                                                       myName: myName || undefined });
        hold.done();
        /* Into the COMPOSER, never sent: the draft lands where every reply lands, with To/Subject
         * prefilled by the ordinary reply path and Send exactly one deliberate click away. */
        this.compose({ mode:'reply', msg, folder, acct, body: String(d.content||'') });
      }catch(err){ hold.fail(err); }
    },
    async addToBills(msg){
      const text0 = this._msgText(msg);
      if(!text0){ toast('this message has no text to read'); return; }
      const hold = this._aiHold('\ud83d\udcb8 Add to Budget', 'reading the bill');
      try{
        const text = text0;
        const fd = new FormData();
        fd.append('file', new File([text], 'email.txt', { type: 'text/plain' }));
        const d = await this._aiPost('/api/budget/scan', fd);
        // type:'text' carries the REASON it couldn't pin the fields down — show that, not a shrug.
        if(!d || d.type !== 'bill'){ hold.fail(new Error((d && d.content) || 'couldn\u2019t read a bill out of this email')); return; }
        if(!window.PCBudget){ hold.fail(new Error('budget module not loaded')); return; }
        hold.done();
        window.PCBudget.reviewParsed(d);
      }catch(err){ hold.fail(err); }
    },
    /* WHO SENT THIS. A mail header is `Some Name <someone@example.com>` rendered as one string, and
     * the address — the part that says who it actually is — was only visible when the display name
     * happened to be missing. Clicking the sender opens what is actually known about them.
     *
     * Deliberately NOT a Nostr profile: this is an email address, and the only honest identity for
     * it is the address itself. Every action goes through machinery that already exists — the
     * composer, and the mailbox search this screen is already driven by — rather than a second path
     * that can disagree with the first.
     */
    senderCard(email, name){
      const addr = String(email || '').trim();
      const disp = String(name || '').replace(/\s*<[^>]*>\s*$/, '').trim();
      const rows = [];
      if(disp && disp !== addr) rows.push(['Name', disp]);
      rows.push(['Email', addr || '(no address)']);
      const self = this;
      modal(`<h3>${enc(disp || addr || 'Sender')}</h3>
        <div class="mail-sender-card">
          ${rows.map(([k, v]) => `<div class="msc-row"><span class="muted small">${enc(k)}</span><b>${enc(v)}</b></div>`).join('')}
        </div>
        <div class="modal-actions">
          ${addr ? '<button class="btn btn-cyan small" id="msc-write">✉ Write to them</button>' : ''}
          ${addr ? '<button class="btn small" id="msc-find">🔎 Their messages</button>' : ''}
          ${addr ? '<button class="btn small" id="msc-copy">Copy address</button>' : ''}
        </div>`, root => {
        const b = id => root.querySelector('#' + id);
        if(b('msc-write')) b('msc-write').onclick = () => { closeModal(); self.compose({ to: addr }); };
        /* Their messages: the mailbox search this screen already runs, driven the way the search box
         * drives it. A second, separately-filtered list is a second answer to the same question. */
        if(b('msc-find')) b('msc-find').onclick = () => {
          closeModal();
          self.q = addr;
          const box = self.root && self.root.querySelector('#mail-search');
          if(box) box.value = addr;                    // …and SAY what is being listed
          const pane = self.root && self.root.querySelector('#mail-read');
          if(pane){ pane.classList.remove('has-open'); pane.innerHTML = '<div class="empty">Select a message to read</div>'; }
          self.openUid = null;
          self.loadList();
        };
        if(b('msc-copy')) b('msc-copy').onclick = () => copyValue(addr, 'address copied', 'Address:');
      });
    },
    async action(act, msg, folder, acct){
      acct=acct||this.acct; const uid=msg.uid;
      if(act==='reply'||act==='replyall'||act==='forward') return this.compose({mode:act, msg, folder, acct});
      /* "ADD TO BILLS" — the email IS the bill, so it rides the exact pipeline "Add Bill with AI"
       * uses (/api/budget/scan → CommandService._bill_command → PCBudget's editable review modal),
       * with the OCR step skipped because the words already are words. The write still happens in
       * the CLIENT and still behind the review — the budget doc is encrypted to the user's own key
       * and an unreviewed amount is the one error that silently corrupts the totals.
       *
       * HTML mail is reduced with DOMParser, never by reading the rendered iframe: that frame is
       * sandboxed WITHOUT allow-same-origin (deliberately — it is somebody else's page), so its
       * document is unreachable from here, and DOMParser parses without ever executing anything. */
      if(act==='ai'){
        /* One ✨ menu, not a button per action — the row is a grid and every new action would cost
         * it a column on a phone. Same sheet the Move menu uses, so it is already right on both
         * desktop and mobile. Every entry ENDS IN THE USER'S HANDS: a summary is read, a reply
         * draft opens in the composer unsent, a bill parse opens Budget's editable review. */
        const pick = await _pickOne('\u2728 AI', [
          { v:'sum',  l:'\ud83d\udcdd Summarize this email' },
          { v:'reply', l:'\u21a9\ufe0f AI reply\u2026' },
          { v:'bill', l:'\ud83d\udcb8 Add to Budget' },
        ]);
        if(pick==='sum') return this.aiSummarize(msg);
        if(pick==='reply') return this.aiReply(msg, folder, acct);
        if(pick==='bill') return this.addToBills(msg);
        return;
      }
      if(act==='unread'){ try{ await this.api('/mark-read',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({account:acct,folder,uid,read:false})}); }catch(_){}; toast('marked unread'); this.loadList(); return; }
      /* MOVE — the folder picker Archive is one entry of.
       *
       * Archive was a button of its own and "move this to the folder I keep receipts in" was not
       * possible at all, so the two are one control: Archive first (it is the common one, and it is
       * the only entry that may CREATE its destination), then every folder this account actually
       * has. The list comes from /folders — the same call the sidebar uses — so it cannot offer a
       * mailbox the server does not have.
       */
      if(act==='move'){
        let fs = { folders: [], labels: {} };
        try{ fs = await this.api('/folders?account=' + encodeURIComponent(acct)) || fs; }catch(_){}
        const rows = [{ v:'__archive', l:'🗄 Archive' }].concat(
          (fs.folders || [])
            .filter(f => f !== folder && f !== 'Drafts')     // where it already is, and the local one
            .map(f => ({ v:f, l:(fs.labels || {})[f] || f })));
        const dest = await _pickOne('Move to…', rows);
        if(!dest) return;
        try{
          if(dest === '__archive'){
            await this.api('/archive', {method:'POST', headers:{'Content-Type':'application/json'},
                                        body:JSON.stringify({account:acct, folder, uid})});
          }else{
            await this.api('/move', {method:'POST', headers:{'Content-Type':'application/json'},
                                     body:JSON.stringify({account:acct, folder, uid, dest})});
          }
        }catch(e){ toast('could not move that message'); return; }
        toast(dest === '__archive' ? 'archived'
                                   : 'moved to ' + ((fs.labels || {})[dest] || dest).replace(/^\S+\s/, ''));
        const pane=$('#mail-read',this.root); if(pane){ pane.classList.remove('has-open'); pane.innerHTML='<div class="empty">Select a message to read</div>'; }
        this.openUid=null; this.loadList(); return;
      }
      if(act==='archive'||act==='delete'){
        if(act==='delete' && !await uiConfirm('Delete this message?', { ok:'Delete', danger:true })) return;
        try{ await this.api('/'+act,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({account:acct,folder,uid})}); }catch(_){ toast(act+' failed'); return; }
        toast(act==='delete'?'deleted':'archived');
        const pane=$('#mail-read',this.root); if(pane){ pane.classList.remove('has-open'); pane.innerHTML='<div class="empty">Select a message to read</div>'; }
        this.openUid=null; this.loadList(); return;
      }
    },
    compose(opts){
      opts=opts||{}; const m=opts.msg; let to='', cc='', subj='', body='', draftUid=null;
      const self=this, atts=[];
      if(opts.mode==='reply'||opts.mode==='replyall'){ subj=/^re:/i.test(m.subject||'')?m.subject:('Re: '+(m.subject||'')); to=m.from_email||m.from||''; if(opts.mode==='replyall'&&m.to) cc=m.to; if(opts.body) body=String(opts.body); }
      else if(opts.to) to=String(opts.to);      // "Write to them", from the sender card
      else if(opts.mode==='forward'){ subj=/^fwd:/i.test(m.subject||'')?m.subject:('Fwd: '+(m.subject||'')); body=`\n\n---------- Forwarded ----------\nFrom: ${m.from||''}\nSubject: ${m.subject||''}\n\n${m.body_text||''}`; }
      else if(opts.mode==='draft'){ const dr=opts.draft||{}; to=dr.to||''; cc=dr.cc||''; subj=(dr.subject==='(no subject)'?'':(dr.subject||'')); body=dr.body_text||''; draftUid=dr.uid||null;
        // Same collision, same answer: a draft summarised for a list carries a count, not a list.
        (Array.isArray(dr.attachments)?dr.attachments:[]).forEach(a=>{ if(a&&a.b64) atts.push({name:a.name,type:a.type||'application/octet-stream',b64:a.b64}); }); }
      const titles={forward:'Forward', reply:'Reply', replyall:'Reply all', draft:'Draft'};
      /* THE MESSAGE'S OWN ACCOUNT FOR REPLY/FORWARD — which this comment already promised and the
       * code did not do.
       *
       * `/reply` and `/forward` use ONE account for two different jobs: the identity the mail is
       * sent as, and the mailbox the ORIGINAL is looked up in by uid+folder. In the unified "All
       * accounts" view `this.acct` is '__all', so this fell through to `accounts[0]` — a different
       * mailbox from the one the message is actually in. The uid then resolves to nothing:
       *
       *     get_message_by_id: account=yummy@yummythai.restaurant, uid=1813, folder=INBOX.Archive
       *     ERROR: Original message not found: yummy@yummythai.restaurant/1813
       *
       * and the composer says "send failed" with no clue that the wrong mailbox was searched. The
       * message carries `account` (the unified list prints it on every row), so replying to
       * something uses the mailbox it came from. A NEW message is unchanged: it still follows the
       * From selector, which is where choosing an identity belongs. */
      const msgAcct = (m && m.account) ? m.account : '';
      const answering = opts.mode==='reply' || opts.mode==='replyall' || opts.mode==='forward';
      const fromAcct=(answering && msgAcct) ? msgAcct
        : (opts.acct && opts.acct!=='__all') ? opts.acct
        : (this.acct!=='__all' ? this.acct : (msgAcct || (this.accounts[0]||{}).email||''));
      const fromSel=this.accounts.length>1
        ? `<label class="muted small mail-from">From <select class="input" id="cm-from">${this.accounts.map(a=>`<option value="${enc(a.email)}"${a.email===fromAcct?' selected':''}>${enc(a.email)}</option>`).join('')}</select></label>`
        : `<div class="muted small" style="margin:-4px 0 8px">From: ${enc(fromAcct)}</div>`;
      /* Send lives in the HEADER. It is the one action this window exists for, and at the bottom of
       * a full-height composer it sits below a body that grows — so on a long message, or a short
       * window, the primary button is the thing you have to go looking for. The footer keeps the
       * secondary actions, on one wrapping row so they line up instead of straddling two. */
      /* STICKY, LIKE THE POST COMPOSER. A stray tap on the backdrop threw away a half-written
       * email — the same loss `compose()` was made sticky for, on the one screen where the text is
       * longest and least likely to exist anywhere else. The ✕ beside Send is what keeps that
       * honest: a sheet that refuses the backdrop and shows no way out is a trap on a phone with no
       * hardware Back button. Escape and Back still close it. */
      modal(`<div class="cm-head"><h3>✉️ ${titles[opts.mode]||'New message'}</h3><span class="spacer"></span>
          <button class="modal-x" id="cm-close" title="Close" aria-label="Close">&#215;</button>
          <button class="btn btn-neon small" id="cm-send"><svg class="ic b-ic" aria-hidden="true"><use href="#i-send"></use></svg>Send</button></div>
        ${fromSel}
        <input class="input" id="cm-to" placeholder="To (comma-separated)" value="${enc(to)}" autocomplete="off">
        <input class="input" id="cm-cc" placeholder="Cc (optional)" value="${enc(cc)}" autocomplete="off">
        <input class="input" id="cm-subj" placeholder="Subject" value="${enc(subj)}">
        <textarea class="input" id="cm-body" placeholder="Write your message…">${enc(body)}</textarea>
        <div class="row cm-actions"><button class="btn btn-ghost small" id="cm-contacts">👤 Contacts</button><button class="btn btn-ghost small" id="cm-attach">📎 Attach</button><button class="btn btn-ghost small" id="cm-blossom">📁 Files</button><button class="btn btn-ghost small" id="cm-draft">💾 Save draft</button><button class="btn btn-ghost small" id="cm-nmail" title="Encrypt the body to a Nostr key (nostr-mail): the mail travels as ordinary email, unreadable to every server on the way">🔐 Encrypt</button><input type="file" id="cm-file" multiple hidden><span id="cm-atts" class="muted small cm-atts"></span></div>
        <div class="fld hidden" id="cm-nmail-row"><label class="muted small">Recipient's Nostr key (npub)<input class="input" id="cm-nmail-pk" placeholder="npub1\u2026" autocomplete="off" style="font-size:16px"></label>
          <label class="bg-chk muted small"><input type="checkbox" id="cm-nmail-dm" checked> Also notify them by Nostr DM (sends the subject)</label>
          <div class="muted small">The body is NIP-44-encrypted to this key; the subject stays readable. The recipient opens it with nostr-mail or PosterChan.</div></div>`,
        box => box.classList.add('mail-compose-modal', 'modal-sticky'));
      { const x=$('#cm-close'); if(x) x.onclick=()=>closeModal(); }
      const drawAtts=()=>{ const e=$('#cm-atts'); if(e) e.innerHTML=atts.map(a=>'📎 '+enc(a.name)).join('  '); };
      drawAtts();
      $('#cm-attach').onclick=()=>$('#cm-file').click();
      _attachRecipientAutocomplete($('#cm-to'));
      _attachRecipientAutocomplete($('#cm-cc'));
      _mailContacts();                      // warm the list so the first keystroke completes
      { const cb=$('#cm-contacts'); if(cb) cb.onclick=()=>_mailContactPicker(p=>{
          // Append to whichever field is focused; otherwise To, falling back to Cc once To is filled,
          // which is what "add another person" means in every mail client.
          const focused=document.activeElement;
          const box=(focused && (focused.id==='cm-cc')) ? $('#cm-cc')
                  : ($('#cm-to').value.trim() && !focused ? $('#cm-cc') : $('#cm-to'));
          const cur=box.value.trim();
          const addr = _mailAddr(p);
          if(cur.split(',').map(x=>x.trim()).some(x=>x.includes(p.email))){ toast('already added'); return; }
          box.value = cur ? cur.replace(/,\s*$/,'') + ', ' + addr : addr;
          toast('added ' + p.email);
        }); }
      { const bb=$('#cm-blossom'); if(bb) bb.onclick=()=>_mailBlossomPicker(a=>{ atts.push(a); drawAtts(); }); }
      /* PASTE AN ATTACHMENT INTO THE MAIL COMPOSER.
         Bound to the composer's own body field rather than the document: mail is a modal, the body
         is where a person pastes, and a document listener would also fire for every other screen
         behind it.  Text keeps the browser's default (pasting a quote into an email must still
         paste text) — only FILES are intercepted, and `files` and `items` are both read because a
         screenshot arrives in one list on some platforms and the other elsewhere. */
      const bodyBox=$('#cm-body');
      if(bodyBox)bodyBox.addEventListener('paste', async e=>{
        const cd=e.clipboardData; if(!cd) return;
        const seen=new Set(); let picked=[];
        for(const f of [...(cd.files||[])]){ if(f){ picked.push(f); seen.add(f.name+':'+f.size); } }
        for(const it of [...(cd.items||[])]){
          if(!it||it.kind!=='file') continue;
          const f=it.getAsFile&&it.getAsFile();
          if(f&&!seen.has(f.name+':'+f.size)) picked.push(f);
        }
        if(!picked.length) return;            // plain text stays the browser's business
        e.preventDefault();
        for(const f of picked){
          // A pasted screenshot has no filename on most platforms; give it one or the attachment
          // arrives as "" and the recipient sees a nameless blob.
          const name=f.name||('pasted-'+new Date().toISOString().replace(/[:.]/g,'-')+'.'+((f.type||'application/octet-stream').split('/')[1]||'bin').split('+')[0]);
          try{ atts.push({name,type:f.type||'application/octet-stream',b64:await _fileB64(f)}); }
          catch(_){ toast('could not attach '+name); }
        }
        drawAtts();
      });
      $('#cm-file').onchange=async ev=>{ for(const f of [...ev.target.files]){ try{ atts.push({name:f.name,type:f.type||'application/octet-stream',b64:await _fileB64(f)}); }catch(_){} } ev.target.value=''; drawAtts(); };
      const gather=()=>({to:$('#cm-to').value.trim(), cc:$('#cm-cc').value.trim(), subject:$('#cm-subj').value.trim(), body:$('#cm-body').value, attachments:atts});
      const sendAcct=()=>{ const s=$('#cm-from'); return (s&&s.value)||fromAcct; };
      // 💾 Save draft → encrypted Nostr doc in the Drafts folder (overwrites the same uid on re-save).
      $('#cm-draft').onclick=async()=>{
        const btn=$('#cm-draft'); btn.disabled=true; btn.textContent='Saving…';
        try{ const r=await self.api('/draft',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({account:sendAcct(), draft:{...gather(), uid:draftUid}})});
          if(r.ok){ draftUid=r.uid; toast('draft saved'); closeModal(); if(self.folder==='Drafts') self.loadList(); }
          else { toast('save failed'); btn.disabled=false; btn.textContent='💾 Save draft'; } }
        catch(_){ toast('save failed'); btn.disabled=false; btn.textContent='💾 Save draft'; }
      };
      { const nb=$('#cm-nmail'), nr=$('#cm-nmail-row');
        if(nb&&nr) nb.onclick=()=>{ nr.classList.toggle('hidden'); nb.classList.toggle('btn-cyan', !nr.classList.contains('hidden')); }; }
      $('#cm-send').onclick=async()=>{
        const payload={account:sendAcct(), ...gather()};
        /* nostr-mail: wrap the body in the spec's one producible format (NIP-44 unsigned + SEAL)
         * BEFORE it leaves — encryption happens here, at the signer; the server relays ciphertext. */
        { const nr=$('#cm-nmail-row');
          if(nr && !nr.classList.contains('hidden')){
            const raw=($('#cm-nmail-pk')&&$('#cm-nmail-pk').value||'').trim();
            const pk=NMail._pk(raw);
            if(!pk){ toast('give a valid npub (or hex key) to encrypt to'); return; }
            if(!S.signer || !S.signer.nip44enc){ toast('this login method can\u2019t encrypt (needs NIP-44)'); return; }
            try{
              const ct=await S.signer.nip44enc(pk, String(payload.body||''));
              const myNpub=NT().nip19.npubEncode(S.ME.pubkey);
              const myName=((Store.profile(S.ME.pubkey)||{}).name)||'';
              payload.body=NMail.armor(ct, myNpub, myName);
              /* The spec's optional notification: the SUBJECT as a Nostr DM, so the recipient's
               * Nostr client tells them an encrypted email is waiting in an inbox they may not be
               * watching. Fire-and-forget AFTER the mail is handed off, and its failure is only a
               * note — the email is the message, the DM is a doorbell. */
              const dmBox=$('#cm-nmail-dm');
              payload._nmailDm = (dmBox && dmBox.checked) ? pk : '';
            }catch(e){ toast('couldn\u2019t encrypt: '+((e&&e.message)||e)); return; }
          } }
        let path='/send';
        if(opts.mode==='reply'||opts.mode==='replyall'){ path='/reply'; payload.uid=m.uid; payload.folder=opts.folder; payload.reply_all=opts.mode==='replyall'; }
        else if(opts.mode==='forward'){ path='/forward'; payload.uid=m.uid; payload.folder=opts.folder; }
        if((path==='/send'||path==='/forward') && !payload.to){ toast('add a recipient'); return; }
        const btn=$('#cm-send'); btn.disabled=true; btn.textContent='Sending…';
        const _nmailDm = payload._nmailDm; delete payload._nmailDm;   // client-side only — never sent to the server
        try{ const r=await self.api(path,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});
          if(r.ok){ toast('sent ✉️');
            if(_nmailDm){
              try{ sendDm(_nmailDm, '\ud83d\udce7\ud83d\udd10 I sent you an encrypted email'
                + (payload.subject ? ': \u201c' + payload.subject + '\u201d' : '')
                + ' \u2014 open it in a nostr-mail client.'); }
              catch(_){ toast('the email went out, but the DM doorbell didn\u2019t'); }
            }
            if(draftUid){ try{ await self.api('/delete',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({account:sendAcct(),folder:'Drafts',uid:draftUid})}); }catch(_){} }
            closeModal();
            // Pull the SENT folder. The server files the sent copy over IMAP, but this client only
            // refreshed when you happened to be LOOKING at Sent — so a message you had just sent
            // was in Thunderbird and missing here, which reads as "it never sent".
            self.syncSent(sendAcct()); }
          else { toast(r.error||'send failed'); btn.disabled=false; btn.textContent='Send ▶'; } }
        catch(_){ toast('send failed'); btn.disabled=false; btn.textContent='Send ▶'; }
      };
    },
    async sync(manual){
      if(this._syncing) return; this._syncing=true; this._lastSync=Date.now();
      // A background poll has no UI to drive: `this.root` is null unless Messages is open, and every
      // element lookup below is already scoped to it.
      const rb=$('#mail-refresh',this.root); if(rb){ rb.textContent='⏳'; rb.disabled=true; }
      try{ const r=await this.api('/sync',{method:'POST'});
        const total=Object.values(r.new||{}).reduce((a,b)=>a+(+b||0),0);
        if(total){ this.unread+=total;
          if(manual) toast(total+' new message'+(total>1?'s':''));
          else notifToast('📧 <b>'+total+' new email'+(total>1?'s':'')+'</b>', S.LOGO,
                          () => switchView('mail'), 'email');
          // The badge is Email's OWN now, and it is not raised while you are LOOKING at the mailbox.
          if(S.VIEW==='mail') Mail.unread=0;
          bumpMail();
          // …and an OS notification, like a DM gets: mail that arrives while the app is behind
          // another window is exactly the case a toast inside the app cannot reach.
          if(S.VIEW!=='mail') osNotify('📧 New email', total+' new message'+(total>1?'s':''),
                                     { tag:'pc-mail', icon:S.LOGO, route:'mail', onClick:()=>switchView('mail') }); }
        if(this.root && this.acct) this.loadList();
      }catch(_){ if(manual) toast('mail sync failed'); }
      this._syncing=false; const rb2=$('#mail-refresh',this.root); if(rb2){ rb2.textContent='🔄'; rb2.disabled=false; }
    },
    // "log in → fetch your mail": pull IMAP → mailbox on login (background), notify on new mail, and
    // surface the count on the Email tab badge — even before the user opens Messages.
    /* Keep checking for mail after login. Without this the client learned about new mail exactly
     * twice — once at login, and whenever someone pressed refresh — so a message arriving while the
     * app sat open produced no badge, no card and no chime until the next reload.
     *
     * Ten minutes: an IMAP sync costs a round trip per account on the server, and this runs in every
     * open tab. Skipped while OFFLINE (the request would just fail) and while the tab is HIDDEN — a
     * background tab that nobody is looking at does not need to know within ten minutes, and it fires
     * on becoming visible again, which is the moment it actually matters. */
    _pollT: 0,
    _lastSync: 0,          // sync() assigns this; declared so the staleness test below is not NaN
    POLL_MS: 2 * 60 * 1000,
    /* "WHEN A USER OPENS THE APP, IT SHOULD REFRESH." It did not.
     *
     * The only catch-up was the poller's `visibilitychange` handler, and it is gated on the last
     * check being POLL_MS (ten minutes) old — right for a tab switch, wrong for coming back to the
     * app, which is the moment somebody is actually looking. Opening the Email view refreshed
     * nothing at all: `renderMailView` returns early when the list is already mounted, and that
     * early return is the common case.
     *
     * One rule in one place, so a resume, a widget press and opening the view cannot disagree about
     * what "fresh enough" means. It never toasts and never blocks a paint: the list is already on
     * screen from cache and this fills in behind it. */
    FRESH_MS: 60 * 1000,
    refreshIfStale(){
      if(S.GUEST) return false;
      if(navigator.onLine === false) return false;
      if(this._syncing) return false;
      if(!(this.accounts && this.accounts.length)) return false;
      if(Date.now() - (this._lastSync || 0) < this.FRESH_MS) return false;
      this.sync(false).catch(()=>{});               // non-manual → notifies rather than toasting
      return true;
    },
    startPolling(){
      if(this._pollT) return;                       // one timer per tab, not one per call
      const tick = () => {
        if(S.GUEST) return;
        if(navigator.onLine === false) return;
        // Same occlusion trap as the torrent poll above: on the desktop app `hidden` also means
        // "another window is in front", and a mail check that stops for that is a mail check that
        // stops whenever you are working.
        if(document.visibilityState === 'hidden' && !_isDesktopApp()) return;
        if(this._syncing) return;
        this.sync(false).catch(()=>{});             // non-manual → notifies rather than toasting
      };
      this._pollT = setInterval(tick, this.POLL_MS);
      /* The listener is bound ONCE for the life of the page, not once per startPolling().
       *
       * The interval is guarded by `_pollT`, but this was not: it sits after that guard, so it is
       * only safe while nothing ever calls stopPolling() — which is true today and is exactly the
       * kind of thing that stops being true without anyone noticing. Every stop/start cycle would
       * otherwise leave another live listener behind, and each one fires its own tick() on the next
       * visibility change: N IMAP syncs per tab switch, growing the longer the window is open. */
      if(!this._visWired){
        this._visWired = true;
        document.addEventListener('visibilitychange', () => {
          // Catch up on becoming visible, but only if the last check is actually stale — switching
          // tabs twice in a minute must not mean two IMAP syncs.
          if(this._pollT && document.visibilityState === 'visible') this.refreshIfStale();
        });
      }
    },
    stopPolling(){ if(this._pollT){ clearInterval(this._pollT); this._pollT = 0; } },

    async loginSync(){
      try{
        const a=await this.api('/accounts'); if(!(a.accounts||[]).length) return;
        const r=await this.api('/sync',{method:'POST'});
        const total=Object.values(r.new||{}).reduce((x,y)=>x+(+y||0),0);
        if(total){ this.unread+=total;
          // A NOTIFICATION, not a plain toast: this is the one moment the client learns mail has
          // arrived, and going through notifToast is what gives it the desktop card and the chime.
          notifToast('📧 <b>'+total+' new email'+(total>1?'s':'')+'</b>', S.LOGO,
                     () => switchView('mail'), 'email');
          if(S.VIEW==='mail'){ this.unread=0; try{ this.loadList(); }catch(_){} }
          bumpMail();
          if(S.VIEW!=='mail') osNotify('📧 New email', total+' new message'+(total>1?'s':''),
                                     { tag:'pc-mail', icon:S.LOGO, route:'mail', onClick:()=>switchView('mail') }); }
      }catch(_){}
    },
  };
  return {
    renderMailView,
    get Mail(){ return Mail; },
  };
};
