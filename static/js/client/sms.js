/* #texts — the phone's text messages, on every device the person owns.
 *
 * WHAT IS AUTHORITATIVE, STATED FIRST, because getting this wrong is what cost this project five
 * days on folder sync. On the handset, the SYSTEM MESSAGE STORE is the truth: only the default
 * messages app may write `content://sms`, and it must, because every other app on the phone and
 * every backup reads it. What lives here is an ARCHIVE — one encrypted Nostr document per message —
 * so a laptop can read and answer the same conversation. It mirrors; it never replaces. When the two
 * disagree, the phone wins.
 *
 * ENCRYPTED, AND GENUINELY DELETABLE. Each message is a kind-30078 event NIP-44-sealed to the user's
 * OWN key, direct-published to their own relay. Deleting one publishes a TOMBSTONE at the same
 * address (an addressable event's newest version is what every client sees, so the old ciphertext
 * stops being served) and a NIP-09 kind 5 beside it. On a public relay that would be a request. Here
 * it is a delete, because the relay is the user's own and these events replicate nowhere — the same
 * property the folder-sync records rely on. The UI says exactly which copies went and does not
 * promise the ones it cannot reach.
 *
 * KIND 30078 IS NOT AN ARBITRARY CHOICE. Three auto-cleaners in this codebase have each, separately,
 * eaten a private library and left nothing in any log; Notes learned all three the hard way. 30078
 * is already exempt from the relay's NIP-40 expiration sweep and from the paid-retention tier's
 * prunable-kinds rule. The third — the CLIENT cache's newest-N eviction — is keyed on the `d` prefix
 * and had to be told: `pcai:sms` is in `_isPinned` (store.js) and in `_CARRY_D` (app.js). Miss either
 * and a few minutes of reading the global feed erases a year of somebody's texts from the one device
 * that has no other copy.
 */
(function(){
  const KIND = 30078;
  const L_TAG = 'pcai-sms';
  const D_MSG = 'pcai:sms:';
  const D_OUT = 'pcai:smsout:';
  /* How far back a phone publishes on its very first sync. A phone with ten years of texts would
   * otherwise spend an afternoon on it and fill the relay in one go; the person can ask for more. */
  const FIRST_RUN_DAYS = 30;
  const HWM = () => 'pc_sms_hwm_' + (PC && PC.ME && PC.ME.pubkey ? PC.ME.pubkey.slice(0, 12) : 'anon');

  let PC = null;
  const S = {
    msgs: new Map(),     // docId -> {address, body, date, incoming, id, gone}
    threads: [],         // built from msgs
    open: '',            // the address whose conversation is on screen
    q: '',
    ready: false,
    loading: false,
    error: '',
    scroll: 0,
    /* THE FLOOR FOR NOTIFICATIONS, set once when the module loads. A first sync pulls a phone's
       whole history through the subscription, and every one of those is "new" to this device — a
       thousand notifications for messages read weeks ago. Only something that arrived AFTER this
       page did is an event; everything older is history. */
    since: Date.now() - 120000,
  };

  const now = () => Math.floor(Date.now() / 1000);
  const ME = () => PC.ME || {};
  const Relay = () => window.Relay;
  const Store = () => window.Store;
  const FILTER = () => ({ authors:[ME().pubkey], kinds:[KIND], '#l':[L_TAG], limit:20000 });

  function plug(method){
    try{ return PC.capPlugin ? PC.capPlugin('Sms', method) : null; }catch(_){ return null; }
  }

  /* THIS DEVICE IS THE PHONE. Only a device holding the SMS role publishes the archive or performs a
   * send another device asked for — everything else reads. Re-asked rather than remembered: the role
   * can be handed to another app in Settings while this page is open, and a cached "yes" is how an
   * app ends up reporting a message as sent that nothing sent. */
  async function isPhone(){
    const P = plug('status');
    if(!P) return false;
    try{ return !!((await P.status()) || {}).isDefault; }catch(_){ return false; }
  }

  // ---------------------------------------------------------------- the archive

  async function absorb(evs){
    const list = (evs || []).slice().sort((a,b) => (b.created_at||0) - (a.created_at||0));
    for(const ev of list){
      const d = ((ev.tags||[]).find(t => t[0]==='d') || [])[1] || '';
      if(!d.startsWith(D_MSG)) continue;
      const have = S.msgs.get(d);
      if(have && have._at >= ev.created_at) continue;
      /* An empty body is a TOMBSTONE, and it is KEPT rather than deleted.
       *
       * Deleting the entry outright looks equivalent and is not: this pass walks newest-first, so
       * the tombstone is seen BEFORE the message it buries, removes nothing, and the older real
       * version is then absorbed on top — the message comes back, every read, for ever.
       *
       * One relay never sends both (an addressable event has one newest version) and the local cache
       * cannot either (Store.query collapses them). A POOL can: `Relay.query` merges answers from
       * every relay, and a relay that never received the tombstone is still serving the message.
       * That is the ordinary state of affairs for a few seconds after any delete.
       *
       * NB: notes.js's `_absorb` has the same shape and the same exposure.
       *
       * Kept as a marker, the ordinary "newest wins" rule handles it, and `rebuild` skips it. */
      if(!ev.content){ S.msgs.set(d, { doc:d, _at: ev.created_at, gone:true }); continue; }
      let obj = null;
      try{ obj = JSON.parse(await PC.nip44dec(ME().pubkey, ev.content)); }
      catch(_){ continue; }                       // not ours, or not decryptable with this key
      if(!obj || typeof obj !== 'object') continue;
      obj.doc = d; obj._at = ev.created_at;
      S.msgs.set(d, obj);
    }
    rebuild();
  }

  /* Conversations, newest first, grouped by the LAST SEVEN DIGITS of the number.
   *
   * That rule is copied deliberately from the phone's own (SmsKeys.sameNumber, and the platform's
   * PhoneNumberUtils.compare before it): the same contact is written `+1 555 010 4477`,
   * `(555) 010-4477` and `5550104477` by three different apps, and a thread that splits into three
   * is a thread nobody can read. A short code — fewer than seven digits — must match exactly, or
   * every five-digit sender becomes one conversation. */
  function key(addr){
    /* SmsKeys.matchKey, in JavaScript. It is the same rule in two languages on purpose and
       tests/test_android_sms.py runs them against each other, because it decides the address a
       send-request is filed under: compute it differently in the two halves and the phone's
       completion marker lands where nothing is watching, so it sends the message again on every
       drain, for ever, and there is no way to un-send a text. */
    const digits = String(addr||'').replace(/[^0-9]/g, '');
    if(!digits) return String(addr||'').replace(/[^0-9+]/g, '');
    return digits.length < 7 ? digits : digits.slice(-7);
  }

  function rebuild(){
    const by = new Map();
    for(const m of S.msgs.values()){
      if(m.gone) continue;                      // a tombstone — see absorb()
      const k = key(m.address);
      let t = by.get(k);
      if(!t){ t = { key:k, address:m.address, msgs:[], date:0, unread:0 }; by.set(k, t); }
      t.msgs.push(m);
      if(m.date > t.date){ t.date = m.date; t.address = m.address; }
    }
    for(const t of by.values()) t.msgs.sort((a,b) => (a.date||0) - (b.date||0));
    S.threads = Array.from(by.values()).sort((a,b) => (b.date||0) - (a.date||0));
  }

  async function load(force){
    if(S.ready && !force) return;
    S.loading = true;
    // CACHE FIRST, network behind it — the rule every list in this app follows, and the archive is
    // entirely the user's own already-synced data.
    let cached = [];
    try{ cached = Store().query([FILTER()]) || []; }catch(_){ cached = []; }
    await absorb(cached);
    S.ready = true;
    S.loading = false;
    paint();
    refresh();
  }

  let _refreshing = false;
  async function refresh(){
    if(_refreshing) return;
    _refreshing = true;
    try{
      const live = await Relay().query([FILTER()]);
      // FOLDED IN, NEVER OVER. A relay that returns nothing — unreachable, throttled, merely slow —
      // must leave the archive alone. That asymmetry is the anti-wipe rule this codebase keeps
      // relearning, and here the local copy may be the only one outside the handset.
      if(live && live.length){ await absorb(live); paint(); }
    }catch(_){ }
    finally{ _refreshing = false; }
  }

  let _sub = null;
  function watch(){
    if(_sub || !Relay().subscribe) return;
    try{
      const f = Object.assign(FILTER(), { since: now() - 120 });
      delete f.limit;
      _sub = Relay().subscribe([f], { live:true, onEvent: async (ev) => {
        const before = S.msgs.size;
        await absorb([ev]);
        if(S.msgs.size !== before){
          notifyNew(ev);
          if(PC.VIEW === 'texts') paint();
        }
      }});
    }catch(_){ _sub = null; }
  }

  /* A TEXT ARRIVING ON YOUR LAPTOP. The handset posts its own Android notification; every other
   * device only learns through this subscription, and without this the archive would fill silently.
   *
   * Never for a message this device published (the phone already showed it), never for one we sent,
   * and never for anything older than the page — a first sync of a thousand messages must not fire a
   * thousand notifications. */
  async function notifyNew(ev){
    try{
      if(await isPhone()) return;
      const d = ((ev.tags||[]).find(t => t[0]==='d') || [])[1] || '';
      const m = S.msgs.get(d);
      if(!m || m.gone || !m.incoming) return;
      // The message's own timestamp against the floor, NOT "have I notified recently" — the latter
      // suppresses the second message of a conversation, which is the one people are waiting for.
      if((m.date || 0) < S.since) return;
      const who = m.name || m.address || 'a message';
      // Through the app's ONE notification path — it knows that Android's WebView implements the
      // Notifications API by doing nothing, and routes to the native builder there instead.
      if(PC.osNotify) PC.osNotify(who, m.body || '', { tag:'sms' });
      else PC.toast(who + ': ' + String(m.body||'').slice(0, 60));
    }catch(_){ }
  }

  // ---------------------------------------------------------------- publishing (the phone only)

  async function publishOne(m){
    const body = {
      address: m.address, body: m.body, date: m.date,
      incoming: !!m.incoming, name: m.name || '',
    };
    const ct = await PC.nip44enc(ME().pubkey, JSON.stringify(body));
    const r = await PC.publish(KIND, ct, [['d', m.doc], ['l', L_TAG]], {quiet:true, noQueue:true});
    return !!(r && r.ok);
  }

  /* PUBLISH WHAT THE PHONE HAS AND THE ARCHIVE DOES NOT.
   *
   * The high-water mark is a TIMESTAMP, not a row id, and that is the load-bearing choice: a row id
   * is local to one handset, so a restored backup renumbers every message and would republish the
   * entire history. The mark only ever moves FORWARD and only once a batch has actually landed. */
  async function mirror(opts){
    const P = plug('list');
    if(!P || !(await isPhone())) return { published:0, skipped:'not the phone' };
    let since = 0;
    try{ since = Number(localStorage.getItem(HWM()) || 0) || 0; }catch(_){ }
    if(!since) since = Date.now() - FIRST_RUN_DAYS * 86400000;
    let rows = [];
    try{ rows = ((await P.list({ since, limit: (opts && opts.limit) || 400 })) || {}).messages || []; }
    catch(_){ return { published:0, skipped:'could not read the phone' }; }

    let n = 0, top = since;
    for(const r of rows){
      if(!r || !r.doc) continue;
      if(S.msgs.has(r.doc)) { if(r.date > top) top = r.date; continue; }
      const m = {
        doc: r.doc, address: r.address, body: r.body, date: r.date,
        incoming: !!r.incoming,
        // The contact's name, resolved on the phone against the phone's OWN address book. Carried
        // so a laptop — which has no phone book — can show a name instead of a number.
        name: r.name || '',
      };
      let ok = false;
      try{ ok = await publishOne(m); }catch(_){ ok = false; }
      if(!ok) break;                 // the relay stopped taking them; the mark stays where it was
      S.msgs.set(m.doc, m);
      n++;
      if(r.date > top) top = r.date;
    }
    if(n){ rebuild(); }
    // Advanced only past messages that really landed. A partial batch resumes; it never skips.
    try{ if(top > since) localStorage.setItem(HWM(), String(top)); }catch(_){ }
    return { published:n };
  }

  // ---------------------------------------------------------------- sending

  /* SENDING FROM A LAPTOP.
   *
   * The other device cannot reach a radio, so it writes an ENCRYPTED REQUEST at `pcai:smsout:<id>`
   * and the handset performs it. The handset replaces that same document with `{done:true}` when it
   * has, which is what stops a request being performed twice — an addressable event has exactly one
   * newest version, so the marker cannot race the request it answers.
   *
   * IT NEEDS THE PHONE TO BE REACHABLE, and says so rather than pretending. A request published to a
   * handset that is switched off sits there until the app is next opened; the UI reports it as
   * "waiting for your phone", never as sent. */
  async function send(to, body){
    if(!to || !body) return { ok:false, error:'nothing to send' };
    if(await isPhone()){
      const P = plug('send');
      if(!P) return { ok:false, error:'no messages plugin' };
      let r = null;
      try{ r = await P.send({ to, body }); }catch(e){ return { ok:false, error:String(e) }; }
      if(r && r.ok){
        // Published from the provider on the next mirror rather than made up here: the phone's row
        // is the message, and inventing a document for one that failed to send would put a message
        // into the archive that nobody ever received.
        mirror({ limit: 20 });
        return { ok:true, where:'phone' };
      }
      return { ok:false, error:(r && r.error) || 'the phone refused it' };
    }
    const at = Date.now();
    const doc = await outboxId(to, body, at);
    const ct = await PC.nip44enc(ME().pubkey, JSON.stringify({ to, body, at }));
    const r = await PC.publish(KIND, ct, [['d', doc], ['l', L_TAG]], {quiet:true, noQueue:true});
    if(r && r.ok) return { ok:true, where:'queued', doc };
    return { ok:false, error:'could not reach your relay' };
  }

  /* The id must match the phone's SmsKeys.outboxId byte for byte, or the handset files a completion
   * marker at an address nothing is watching and the request is performed for ever. Same hash, same
   * canonical string, in both languages — tests/test_android_sms.py runs them against each other. */
  async function outboxId(address, body, askedMs){
    const canon = key(address) + '\n' + askedMs + '\n' + (body || '');
    return D_OUT + (await sha256hex(canon)).slice(0, 24);
  }

  async function sha256hex(s){
    const buf = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(s));
    return Array.from(new Uint8Array(buf)).map(b => b.toString(16).padStart(2, '0')).join('');
  }

  /* THE HANDSET'S HALF: perform whatever other devices have asked for, and mark each one done.
   *
   * A request older than a day is DISCARDED rather than sent. A phone that was off for a week must
   * not wake up and deliver a week of messages whose moment has passed — that is the shape of every
   * "my phone sent it twice, three days late" story, and there is no way to un-send a text. */
  const MAX_AGE_MS = 86400000;
  async function drainOutbox(){
    if(!(await isPhone())) return 0;
    let evs = [];
    try{ evs = await Relay().query([Object.assign(FILTER(), { limit: 200 })]); }catch(_){ return 0; }
    let done = 0;
    for(const ev of evs || []){
      const d = ((ev.tags||[]).find(t => t[0]==='d') || [])[1] || '';
      if(!d.startsWith(D_OUT) || !ev.content) continue;
      let req = null;
      try{ req = JSON.parse(await PC.nip44dec(ME().pubkey, ev.content)); }catch(_){ continue; }
      if(!req || req.done) continue;
      if(!req.to || !req.body) continue;
      if(Date.now() - (req.at || 0) > MAX_AGE_MS){
        await mark(d, { done:true, dropped:'too old' });
        continue;
      }
      const P = plug('send');
      if(!P) return done;
      let r = null;
      try{ r = await P.send({ to:req.to, body:req.body }); }catch(_){ r = null; }
      // MARKED BEFORE ANYTHING ELSE, and marked even when the send FAILED. A text that went out and
      // whose marker did not is a text that goes out again on the next drain; there is no undo for
      // that, so a failed send is reported in the marker rather than retried blindly.
      await mark(d, { done:true, ok: !!(r && r.ok), error: (r && r.error) || '' });
      done++;
    }
    if(done) mirror({ limit: 50 });
    return done;
  }

  async function mark(doc, obj){
    try{
      const ct = await PC.nip44enc(ME().pubkey, JSON.stringify(obj));
      await PC.publish(KIND, ct, [['d', doc], ['l', L_TAG]], {quiet:true, noQueue:true});
    }catch(_){ }
  }

  // ---------------------------------------------------------------- deleting

  /* A DELETE IS TWO DELETES, AND THE UI SAYS WHICH ONES HAPPENED.
   *
   * The archive's copy goes by tombstone + kind 5 — a real delete, because these events are
   * direct-published to the user's own relay and replicate nowhere. The handset's copy goes through
   * the provider, and only this device can do that, and only if this device IS the handset. Removing
   * one without the other means the next mirror publishes it straight back. */
  async function remove(docs){
    docs = (docs || []).filter(Boolean);
    if(!docs.length) return { archive:0, phone:0 };

    /* THE PHONE'S COPY FIRST, AND IT IS THE WHOLE GUARD.
     *
     * Tombstone the archive first and a provider delete that fails leaves the message ON the phone
     * with no archive document — which the next mirror publishes straight back. The delete would
     * undo itself and report success, which is the exact shape of the folder-sync bug that cost this
     * project days: a silent act re-derived into its opposite on the next pass.
     *
     * So: delete on the handset, find out how many rows really went, and only tombstone what is
     * genuinely gone. On any other device there is nothing to delete here and the archive copy is
     * the only one, so it proceeds. */
    let phone = 0, refused = false;
    const P = plug('delete'), L = plug('list');
    const onPhone = await isPhone();
    if(onPhone && P && L){
      /* The provider's ROW IDS, looked up by document address. They are deliberately NOT in the
         archive: a row id is local to one handset, so carrying it across devices would be carrying a
         number that means something different on each of them. */
      try{
        const rows = ((await L.list({ limit: 5000 })) || {}).messages || [];
        const want = new Set(docs);
        const ids = [];
        for(const r of rows) if(want.has(r.doc) && r.id) ids.push(r.id);
        if(ids.length){
          phone = (((await P.delete({ ids })) || {}).deleted) || 0;
          // Asked to remove rows and removed none: the provider refused. Leave the archive alone —
          // a half-delete that the next sync reverses is worse than one that plainly did not happen.
          if(!phone) refused = true;
        }
      }catch(_){ refused = true; }
    }
    if(refused) return { archive:0, phone:0, refused:true };

    let archive = 0;
    for(const d of docs){
      const r = await PC.publish(KIND, '', [['d', d], ['l', L_TAG]], {quiet:true, noQueue:true});
      if(r && r.ok) archive++;
      try{ await PC.publish(5, '', [['a', KIND+':'+ME().pubkey+':'+d]], {quiet:true, noQueue:true}); }catch(_){ }
      // A MARKER, not a removal — the same reason absorb() keeps one: a cached copy of the original
      // read back later would otherwise walk straight over the hole and restore the message.
      S.msgs.set(d, { doc:d, _at: now(), gone:true });
    }
    rebuild();
    return { archive, phone };
  }

  // ---------------------------------------------------------------- view

  function paint(){
    if(!PC || PC.VIEW !== 'texts') return;
    const feed = PC.$('#feed');
    if(!feed) return;
    const enc = PC.enc;
    if(S.open){ return paintThread(feed, enc); }

    const rows = S.threads.filter(t => {
      if(!S.q) return true;
      const q = S.q.toLowerCase();
      return String(t.address||'').toLowerCase().includes(q)
          || (t.msgs.some(m => String(m.body||'').toLowerCase().includes(q)))
          || String((t.msgs[t.msgs.length-1]||{}).name||'').toLowerCase().includes(q);
    });

    feed.innerHTML = `
      <div class="sms-wrap">
        <div class="sms-head">
          <input class="input" id="sms-q" placeholder="Search messages" value="${enc(S.q)}">
          <button class="btn btn-neon small" id="sms-new">${ICO('plus','b-ic')}New</button>
        </div>
        <div class="muted small" id="sms-note"></div>
        <div class="sms-threads">${rows.map(t => {
          const last = t.msgs[t.msgs.length-1] || {};
          const who = last.name || t.address || '';
          return `<button class="sms-thread" data-k="${enc(t.key)}">
            <div class="sms-av">${enc(initials(who))}</div>
            <div class="sms-body">
              <div class="sms-row1"><span class="sms-who">${enc(who)}</span>
                <span class="sms-when muted">${enc(when(last.date))}</span></div>
              <div class="sms-snip muted">${enc(String(last.body||'').slice(0,90))}</div>
            </div></button>`;
        }).join('') || '<div class="muted" style="padding:24px;text-align:center">No messages here yet</div>'}
        </div>
      </div>`;

    const q = PC.$('#sms-q');
    if(q) q.oninput = () => { S.q = q.value; paint(); q.focus(); };
    const nw = PC.$('#sms-new');
    if(nw) nw.onclick = composeNew;
    feed.querySelectorAll('.sms-thread').forEach(b => {
      b.onclick = () => { S.open = b.dataset.k; paint(); };
    });
    noteWhere();
  }

  /* WHERE THE MESSAGES COME FROM, said on the screen. "This device is not your phone" and "you have
   * no messages" look identical, and only one of them is a problem. */
  async function noteWhere(){
    const el = PC.$('#sms-note');
    if(!el) return;
    if(await isPhone()){
      el.textContent = 'This phone. Messages are stored in the phone’s own message app as well, '
        + 'so nothing else on the phone loses them.';
      // NOT mirror() — this runs on every repaint, and a repaint happens on every keystroke in the
      // search box. Publishing is driven by render() and by the app coming to the foreground.
    } else {
      el.textContent = 'An encrypted copy of your phone’s messages. Sending from here asks your '
        + 'phone to send it, so your phone has to be reachable.';
    }
  }

  function paintThread(feed, enc){
    const t = S.threads.find(x => x.key === S.open);
    if(!t){ S.open = ''; return paint(); }
    const who = (t.msgs[t.msgs.length-1] || {}).name || t.address;
    feed.innerHTML = `
      <div class="sms-wrap">
        <div class="sms-head">
          <button class="btn small" id="sms-back">${ICO('arrow-left','b-ic')}</button>
          <div class="sms-title">${enc(who)}</div>
        </div>
        <div class="sms-msgs">${t.msgs.map(m => `
          <div class="sms-msg ${m.incoming ? 'them' : 'me'}" data-doc="${enc(m.doc)}">
            <div class="sms-bub">${enc(m.body||'')}</div>
            <div class="sms-meta muted">${enc(when(m.date))}</div>
          </div>`).join('')}</div>
        <div class="sms-compose">
          <input class="input" id="sms-in" placeholder="Text message">
          <button class="btn btn-neon" id="sms-send">${ICO('send','b-ic')}Send</button>
        </div>
      </div>`;
    PC.$('#sms-back').onclick = () => { S.open = ''; paint(); };
    const input = PC.$('#sms-in'), btn = PC.$('#sms-send');
    const go = async () => {
      const body = input.value.trim();
      if(!body) return;
      btn.disabled = true;
      const r = await send(t.address, body);
      btn.disabled = false;
      if(!r.ok){ PC.toast(r.error || 'could not send'); return; }
      input.value = '';
      PC.toast(r.where === 'phone' ? 'sent' : 'waiting for your phone to send it');
      paint();
    };
    btn.onclick = go;
    input.onkeydown = e => { if(e.key === 'Enter') go(); };
    feed.querySelectorAll('.sms-msg').forEach(el => {
      el.oncontextmenu = async (e) => {
        e.preventDefault();
        if(!await PC.uiConfirm('Delete this message from your archive' +
             (await isPhone() ? ' and from this phone' : '') + '?')) return;
        const r = await remove([el.dataset.doc]);
        // SAY WHICH COPIES WENT, and never promise the ones this device cannot reach. Other phones
        // and laptops drop theirs when the tombstone reaches them.
        if(r.refused) PC.toast('this phone would not delete it — nothing was changed');
        else PC.toast(r.phone ? 'deleted here and from your archive' : 'deleted from your archive');
        paint();
      };
    });
    const list = PC.$('.sms-msgs');
    if(list) list.scrollTop = list.scrollHeight;
  }

  async function composeNew(){
    const to = await PC.uiPrompt('Phone number');
    if(!to) return;
    S.open = key(to);
    if(!S.threads.some(t => t.key === S.open)){
      S.threads.unshift({ key:S.open, address:to, msgs:[], date:0, unread:0 });
    }
    paint();
  }

  function initials(label){
    const s = String(label || '').trim();
    if(!s) return '?';
    const digits = s.replace(/[^0-9]/g, '');
    if(digits.length >= s.length - 3) return digits.slice(-2) || '?';
    const parts = s.split(/\s+/);
    if(parts.length >= 2 && parts[1]) return (parts[0][0] + parts[1][0]).toUpperCase();
    return s[0].toUpperCase();
  }

  function when(ms){
    if(!ms) return '';
    const d = new Date(ms);
    const today = new Date();
    if(d.toDateString() === today.toDateString()){
      return d.toLocaleTimeString(undefined, { hour:'2-digit', minute:'2-digit' });
    }
    return d.toLocaleDateString(undefined, { month:'short', day:'numeric' });
  }

  async function render(){
    load();
    watch();
    paint();
    // The publish and the drain belong HERE and on foreground, not in paint(): paint runs on every
    // keystroke in the search box, and a mirror per keystroke is a provider read and a relay write
    // per keystroke.
    if(await isPhone()){ await load(); mirror(); drainOutbox(); }
  }

  function init(){
    PC = window.__PC;
    if(!PC){ return setTimeout(init, 50); }
    /* The handset publishes and drains WITHOUT the screen being open — that is the whole point of an
     * archive. Behind `load` so it never runs before the client has a key, and on visibility rather
     * than a timer: a poll here would run for the life of the battery on a device that already holds
     * the HOME role. */
    document.addEventListener('visibilitychange', async () => {
      if(document.visibilityState !== 'visible') return;
      if(!(await isPhone())) return;
      await load();
      mirror();
      drainOutbox();
    });
  }
  init();

  window.PCSms = { render, mirror, drainOutbox, send, remove, load,
                   _state: () => S, _key: key, _outboxId: outboxId };
})();
