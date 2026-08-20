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
    return !!(await phoneState()).isDefault;
  }

  /* WHAT THIS DEVICE IS ALLOWED TO DO WITH THE PHONE'S MESSAGES — asked in one call, because the two
   * answers are separate switches and were being conflated into one.
   *
   *  * `isDefault` — this app RECEIVES and SENDS. Only the default SMS app may write the provider.
   *  * `canRead`   — this app may READ. That is READ_SMS, a runtime permission, and it is neither
   *                  implied by the role nor granted by being declared in the manifest.
   *
   * Reading used to be gated on the ROLE, which is the same circularity that hid the Messages tile
   * behind the SMS role: a person trying the app out can be allowed to read their texts long before
   * they hand over their messaging, and telling them "PosterChan is not the default SMS app, so
   * Android will not let it read your messages" was simply untrue. */
  async function phoneState(){
    const P = plug('status');
    if(!P) return { isDefault:false, canRead:false, present:false };
    try{
      const st = (await P.status()) || {};
      // An older APK's `status` has no `canRead`. Absent is "it never asked", and on those builds
      // reading was gated on the role — so the role is the honest answer to give.
      return { isDefault: !!st.isDefault, present: true,
               canRead: st.canRead === undefined ? !!st.isDefault : !!st.canRead,
               // MEASURED, so the screen can state a fact rather than a verdict. Older builds report
               // neither; `telephony` defaults to true there because every APK before this one that
               // could answer `status` at all was assumed to be a phone.
               defaultPkg: String(st.defaultPackage || ''),
               pkg: String(st.package || ''),
               roleHeld: !!st.roleHeld,
               canNotify: st.canNotify === undefined ? true : !!st.canNotify,
               telephony: st.telephony === undefined ? true : !!st.telephony };
    }catch(_){ return { isDefault:false, canRead:false, present:true, defaultPkg:'', pkg:'',
                        telephony:true }; }
  }

  /* WHAT TO SAY ABOUT NOT BEING THE MESSAGES APP — and it names what Android named.
   *
   * "android keeps saying posterchan is not the phones messaging app but I see all my texts". A bare
   * verdict is unanswerable: it could be a role that was never granted, one granted in a different
   * profile, or a device with no telephony at all, and the person is left arguing with a sentence.
   * The package Android reports is the same measurement the verdict is derived from, so printing it
   * cannot contradict the verdict and it turns "you are wrong" into something checkable. */
  function roleLine(st){
    if(!st.telephony)
      return 'This device has no SIM, so it cannot be a messages app. It shows what your phone '
           + 'publishes here.';
    if(st.roleHeld && st.defaultPkg && st.pkg && st.defaultPkg !== st.pkg)
      /* THE TWO TABLES DISAGREE, AND SAYING SO IS THE ONLY USEFUL ANSWER. Android keeps the SMS
       * ROLE and the messages provider's default-app row separately, and on some builds granting
       * the role does not move the row. The row is the one that decides what is delivered, so the
       * app cannot simply believe the role — but "you are not the default" to somebody who just set
       * it is unanswerable, and this is not. */
      return 'Android has given PosterChan the messages role, but its message store still lists '
           + st.defaultPkg + ' as the messages app — so new texts are delivered there. Setting the '
           + 'default again in Settings \u2192 Apps \u2192 Default apps \u2192 SMS usually moves it.';
    if(st.defaultPkg && st.pkg && st.defaultPkg !== st.pkg)
      return 'Android says this phone\u2019s messages app is ' + st.defaultPkg + ', not PosterChan, '
           + 'so new messages arrive there. Set it in Settings \u2192 Apps \u2192 Default apps '
           + '\u2192 SMS.';
    if(!st.defaultPkg)
      return 'Android has not named a messages app for this phone. Set PosterChan in Settings '
           + '\u2192 Apps \u2192 Default apps \u2192 SMS.';
    return 'PosterChan is not the default SMS app on this phone, so new messages arrive in '
         + 'whichever app is. Set it in Settings \u2192 Apps \u2192 Default apps \u2192 SMS.';
  }

  /* WHAT THIS PHONE MEASURED, in one line, for the reports that cannot be answered from here.
   *
   * "posterchan still not working as default Messenger app despite being set as default messenger"
   * cost four rounds because from the build side the failure REPORTS SUCCESS: the role is set, the
   * screen draws, nothing throws. So the screen prints what was asked and what came back — the two
   * role tables, the permission, the four components Android demands before it will even offer the
   * role, and what the provider actually returned. It is the same reason the music panel prints its
   * counters and the /logs board measures rather than retells. */
  async function details(){
    const P = plug('diagnose');
    if(!P || !P.diagnose) return null;
    try{ return await P.diagnose(); }catch(_){ return null; }
  }

  function detailLine(d){
    if(!d) return '';
    const c = d.components || {};
    const missing = ['smsDeliver','mmsDeliver','sendTo','respondViaMessage'].filter(k => !c[k]);
    return [
      'this app: ' + (d.package || '?'),
      'message store names: ' + (d.defaultPkg || d.defaultPackage || '(nothing)'),
      'SMS role held: ' + (d.roleHeld ? 'yes' : 'no'),
      'may read messages: ' + (d.canRead ? 'yes' : 'no'),
      'may show notifications: ' + (d.canNotify ? 'yes' : 'NO — new texts arrive in silence'),
      /* ALL THREE CAPABILITY SIGNALS, because "can this device do SMS" has now been answered wrongly
       * twice — first with no check at all, then with FEATURE_TELEPHONY, which is true on Wi-Fi-only
       * tablets that ship the telephony stack and have no radio. A single boolean cannot say which
       * one lied. */
      'sms capable: ' + (d.capability
        ? (d.capability.smsCapable ? 'yes' : 'no')
          + ' (isSmsCapable=' + d.capability.isSmsCapable
          + ' feature.telephony=' + d.capability.featureTelephony
          + ' feature.messaging=' + d.capability.featureMessaging
          + ' sdk=' + d.capability.sdk
          + ' roleAvailable=' + d.capability.roleAvailable
          + ' canBeSms=' + d.capability.canBeSms + ')'
        : 'not reported by this build'),
      'last read: ' + (d.refused ? 'refused' : (d.read >= 0 ? d.read + ' found' : 'not attempted')),
      missing.length ? 'MISSING COMPONENTS: ' + missing.join(', ')
                     : 'all four SMS components installed',
    ].join(' \u00b7 ');
  }

  /* ASK ANDROID FOR PERMISSION TO READ. Resolves whether it was granted; a refusal is an answer, not
   * an error. Older APKs have no `ensureRead` method at all, and there the honest result is "no". */
  async function ensureRead(){
    const P = plug('ensureRead');
    if(!P || !P.ensureRead) return (await phoneState()).canRead;
    try{ return !!((await P.ensureRead()) || {}).granted; }catch(_){ return false; }
  }

  /* ASK TO BE ALLOWED TO ANNOUNCE A NEW TEXT. "make sure notifications work on new text messages
   * ... otherwise useless" — on Android 13+ POST_NOTIFICATIONS is a runtime grant and `notify()`
   * does nothing without it. Music and push each ask for their own flows, so somebody who used
   * neither had never been asked and every text arrived in silence. Asked once per visit, after the
   * read permission, and never on a device that has no messages plugin at all. */
  async function ensureNotify(){
    const P = plug('ensureNotify');
    if(!P || !P.ensureNotify) return false;
    try{ return !!((await P.ensureNotify()) || {}).granted; }catch(_){ return false; }
  }

  /* WHY IS THIS EMPTY? Four different answers that look identical on screen, and the difference is
   * the whole of what somebody needs to know.
   *
   * "0 of my sms messages in Text" is the report, and an empty list cannot tell you whether nothing
   * has been published yet, this device cannot publish, the phone is not the default SMS app, or
   * you genuinely have no messages. Naming it is the same rule the drive check paid for the hard
   * way: "could not ask" is never "there is nothing there".
   *
   * Deliberately about THIS device: a laptop is not broken for being unable to read a SIM, and
   * telling it to "set yourself as the default SMS app" would be nonsense. */
  async function emptyWhy(){
    if(!plug('status'))
      return { why: 'A phone publishes your messages here — this device has no SIM to read. Open '
                  + 'PosterChan on your Android phone and set it as the default SMS app.', phone: false };
    const st = await phoneState();
    /* THE PERMISSION COMES FIRST, because it is the only one of these a tap can fix and because it
     * is the one that was never named. Reading needs READ_SMS and nothing else; the role decides
     * whether messages ARRIVE here, which is a different sentence and a different screen. */
    if(!st.canRead)
      return { why: 'PosterChan has not been allowed to read this phone\u2019s messages yet.',
               phone: true, fix: 'perm' };
    if(!st.isDefault)
      return { why: roleLine(st), phone: true, fix: 'role' };
    let mark = 0;
    try{ mark = Number(localStorage.getItem(HWM()) || 0) || 0; }catch(_){ }
    if(!mark)
      return { why: 'Nothing has been copied across yet. The first pass reaches back '
                  + FIRST_RUN_DAYS + ' days.', phone: true, fix: 'mirror' };
    /* On the phone the provider is read directly, so an empty list here really is an empty inbox.
     * The 30-day window bounds what is PUBLISHED to other devices, and says nothing about this one. */
    return { why: 'No messages on this phone.', phone: true };
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
    /* PUBLISHING NEEDS TO READ, NOT TO BE THE DEFAULT APP — and that was the difference between an
     * archive and nothing at all.
     *
     * "phone conversations not on the other posterchan apps either", from a handset whose own Texts
     * screen was showing every message. The archive exists to get this device's messages to the
     * devices that cannot read a SIM, and the thing that makes this device able to do that is
     * READ_SMS. The ROLE decides whether new messages ARRIVE here and whether a send may be
     * performed — neither of which is publishing. Gated on the role, a phone that had granted the
     * permission and not (or not successfully) handed over its messaging published nothing, for
     * ever, with a full Texts screen in front of the person and nothing anywhere to say why.
     *
     * A device that cannot read still publishes nothing, which is the rule that actually matters:
     * a laptop has no plugin and no permission, so it can never fight the handset over a message's
     * newest version. */
    const st = await phoneState();
    if(!P || !st.canRead) return { published:0, skipped:'cannot read this phone' };
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

  /* ON THE PHONE, THE PHONE IS THE SOURCE. This is what an SMS app is.
   *
   * "all sms apps mirror what the phone has, why are we different" — and the answer was that this
   * one had the relationship backwards. Display was driven by the Nostr archive, so the phone showed
   * only what it had already PUBLISHED: a thirty-day window on a device holding years, with the rest
   * sitting in the provider a few centimetres away. Nobody's Messages app behaves like that.
   *
   * The archive is a TRANSPORT, and only a transport: it exists to get messages to the devices that
   * cannot read a SIM. What the phone shows comes from `Telephony.Sms`, all of it, straight away —
   * paged so a decade of history does not arrive as one allocation, and merged by doc id so a
   * message already known from the relay is not duplicated.
   *
   * Reading is not publishing. This fills the list; `mirror()` still decides, separately and on its
   * own schedule, what goes to the other devices — which is why a phone with no network still shows
   * every message it has. */
  async function loadFromPhone(onProgress){
    const P = plug('list');
    if(!P) return { loaded: 0 };
    let total = 0, since = 0, quiet = 0, refused = false;
    for(let page = 0; page < 400 && quiet < 2; page++){
      let rows = [];
      try{
        const answer = (await P.list({ since, limit: 500 })) || {};
        rows = answer.messages || [];
        /* REFUSED IS NOT EMPTY, and this is the read the Texts screen is built on. The plugin
         * returns `[]` for a phone with no texts and for a provider that would not let us look;
         * without this the caller cannot tell, so it reported "no messages" over a full inbox and
         * offered nothing to do about it. Sticky across pages: a refusal on ANY page means the
         * sweep is incomplete, and the count that comes back is not the phone's real total. */
        if(answer.refused) refused = true;
      }
      catch(_){ break; }
      let n = 0, top = since;
      for(const r of rows){
        if(!r || !r.doc) continue;
        if(r.date > top) top = r.date;
        if(S.msgs.has(r.doc)) continue;
        S.msgs.set(r.doc, { doc: r.doc, address: r.address, body: r.body, date: r.date,
                            incoming: !!r.incoming, name: r.name || '' });
        n++; total++;
      }
      quiet = n ? 0 : quiet + 1;
      if(n){ rebuild(); if(onProgress) try{ onProgress(total); }catch(_){ } }
      if(top <= since) break;            // the provider has nothing newer — we are at the end
      since = top;
    }
    return { loaded: total, refused: refused };
  }

  /* BRING IN EVERYTHING — the history behind the first pass's 30-day window.
   *
   * `mirror()` is bounded on purpose: a first sweep on a phone with a decade of SMS would publish
   * tens of thousands of events in one go, on a radio, on a battery. But the bound is invisible from
   * the outside — "I have years of texts and I can see a month" reads as broken — so the way past it
   * is a deliberate action with a count, rather than a bigger default nobody chose.
   *
   * Walks BACKWARDS in batches from the oldest message it has, because `list({since})` answers
   * forwards: each round asks for everything after a point far enough back to be sure of overlap,
   * and stops when a round publishes nothing new. Overlap is free — `S.msgs.has(doc)` skips a
   * message already held — and a gap is not, which is why the window steps rather than paginating on
   * a cursor the phone would have to keep.
   *
   * It does NOT move the high-water mark. That mark means "everything after this is published", and
   * back-filling old messages says nothing about the recent end; moving it would skip whatever
   * arrived while this ran. */
  async function importAll(onProgress){
    const P = plug('list');
    if(!P) return { published: 0, why: 'this device has no SMS plugin' };
    if(!(await isPhone())) return { published: 0, why: 'this phone is not the default SMS app' };
    const DAY = 86400000;
    let total = 0, quiet = 0;
    // Oldest we already hold — the back-fill starts from there and reaches further each round.
    let edge = Date.now();
    for(const m of S.msgs.values()) if(m && m.date && m.date < edge) edge = m.date;
    for(let round = 0; round < 400 && quiet < 2; round++){
      const from = Math.max(0, edge - 90 * DAY);
      let rows = [];
      try{ rows = ((await P.list({ since: from, limit: 400 })) || {}).messages || []; }
      catch(_){ return { published: total, why: 'could not read the phone' }; }
      let n = 0, oldest = edge;
      for(const r of rows){
        if(!r || !r.doc || S.msgs.has(r.doc)) continue;
        const m = { doc: r.doc, address: r.address, body: r.body, date: r.date,
                    incoming: !!r.incoming, name: r.name || '' };
        let ok = false;
        try{ ok = await publishOne(m); }catch(_){ ok = false; }
        // The relay stopped taking them. Stop where we are and report — the next run resumes,
        // because nothing here depends on a mark that has already moved past this point.
        if(!ok) return { published: total, why: 'the relay stopped accepting messages' };
        S.msgs.set(m.doc, m);
        n++; total++;
        if(m.date && m.date < oldest) oldest = m.date;
      }
      quiet = n ? 0 : quiet + 1;
      if(n){ rebuild(); if(onProgress) try{ onProgress(total); }catch(_){ } }
      if(from === 0) break;                  // reached the beginning of time; nothing older exists
      edge = Math.min(oldest, from);
    }
    return { published: total };
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
        <div class="muted small" style="margin-top:6px">
          <button class="btn small" id="sms-why">Why isn\u2019t this working?</button></div>
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
        }).join('') || ('<div class="sms-empty muted" style="padding:24px;text-align:center">'
             + enc(S.emptyWhy || 'No messages here yet')
             + (S.emptyFix === 'deep'
                 ? '<div style="margin-top:14px"><button class="btn btn-neon small" id="sms-deep">'
                   + 'Bring in everything</button></div>' : '')
             + (S.emptyFix === 'mirror'
                 ? '<div style="margin-top:14px"><button class="btn btn-neon small" id="sms-now">'
                   + 'Copy my messages across</button></div>' : '')
             /* THE ONE A TAP CAN FIX GETS A BUTTON. Saying "not allowed" and leaving the person to
              * find it in Android's settings is most of the way to saying nothing. */
             + (S.emptyFix === 'perm'
                 ? '<div style="margin-top:14px"><button class="btn btn-neon small" id="sms-allow">'
                   + 'Allow PosterChan to read them</button></div>' : '')
             /* AND SO DOES THE ROLE. `fix:'role'` printed a sentence and offered nothing —
              * "PosterChan still not working as default Messenger app despite being set as
              * default messenger" came back three times against a screen whose only advice was to
              * go and do it again in Android's own settings. Android's role dialog is one call
              * away and this is the screen somebody is standing on when they want it. */
             + (S.emptyFix === 'role'
                 ? '<div style="margin-top:14px"><button class="btn btn-neon small" id="sms-role">'
                   + 'Make PosterChan my messages app</button>'
                   + '<button class="btn small" id="sms-defaults" style="margin-left:8px">'
                   + 'Open Android\u2019s Default apps</button></div>' : '')
             + '</div>')}
        </div>
      </div>`;

    const q = PC.$('#sms-q');
    if(q) q.oninput = () => { S.q = q.value; paint(); q.focus(); };
    const nw = PC.$('#sms-new');
    if(nw) nw.onclick = composeNew;
    const dbg = PC.$('#sms-why');
    if(dbg) dbg.onclick = async () => {
      dbg.disabled = true;
      const d = await details();
      const el = PC.$('#sms-note');
      if(el) el.textContent = d ? detailLine(d)
                                : 'This build cannot report it — it is older than this screen.';
    };
    const role = PC.$('#sms-role');
    if(role) role.onclick = async () => {
      // THE HOME-SCREEN PLUGIN, not the Sms one. The role dialog belongs to the shell half
      // (HomePlugin.requestSms is what the settings card's switch calls), and asking the Sms plugin
      // for it gets a proxy that answers every name and then rejects — which looks like a button
      // that does nothing, on the screen this button exists to stop doing nothing.
      const P = PC.capPlugin ? PC.capPlugin('HomeScreen', 'requestSms') : null;
      if(!P || !P.requestSms){ S.emptyWhy = 'This build cannot ask for it — update the app.'; paint(); return; }
      role.disabled = true;
      let held = false;
      try{ held = !!((await P.requestSms()) || {}).isDefault; }catch(_){ held = false; }
      /* RE-ASKED AFTERWARDS RATHER THAN BELIEVED. Android refuses a role the app cannot hold by
       * starting the request activity and finishing it immediately — no dialog, no error, nothing
       * in any log — which is indistinguishable from somebody declining. The state is what decides
       * what to say, and when it did not move the person is sent to the one screen that always
       * works. */
      const st2 = await phoneState();
      if(st2.isDefault || held){ S.emptyWhy = ''; S.emptyFix = ''; await loadFromPhone(); paint(); return; }
      S.emptyWhy = roleLine(st2) + ' Android did not change it just now, so use the button below.';
      S.emptyFix = 'role';
      paint();
    };
    const defs = PC.$('#sms-defaults');
    if(defs) defs.onclick = () => {
      const P = PC.capPlugin ? PC.capPlugin('HomeScreen', 'openDefaultApps') : null;
      try{ if(P && P.openDefaultApps) P.openDefaultApps(); }catch(_){}
    };
    const allow = PC.$('#sms-allow');
    if(allow) allow.onclick = async () => {
      allow.disabled = true;
      const ok = await ensureRead();
      if(!ok){
        // A REFUSAL IS SAID OUT LOUD. A button that does nothing when pressed reads as a broken app,
        // and Android stops showing its dialog after two refusals — at which point the only way
        // through is the app's own settings page, which is what this then says.
        S.emptyWhy = 'Android is not offering the prompt any more. Open Settings \u2192 Apps \u2192 '
                   + 'PosterChan \u2192 Permissions \u2192 SMS and allow it there.';
        S.emptyFix = '';
        paint();
        return;
      }
      S.emptyWhy = ''; S.emptyFix = '';
      /* GRANTED AND STILL REFUSED IS ITS OWN STATE. `ensureRead` answering yes means Android said
       * yes; the PROVIDER can still refuse — a work profile, an OEM permission manager, a phone
       * where the messages app is locked down. Reporting "no messages" there sends somebody to look
       * for texts that are being withheld. */
      const r = await loadFromPhone();
      if(r && r.refused){
        S.emptyWhy = 'This phone allowed the permission but its message store still would not '
                   + 'answer. Open “Why isn\u2019t this working?” below for what it reported.';
        S.emptyFix = '';
      }
      paint();
    };
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
    const st = await phoneState();
    if(st.isDefault){
      el.textContent = 'This phone. Messages are stored in the phone’s own message app as well, '
        + 'so nothing else on the phone loses them.';
      // NOT mirror() — this runs on every repaint, and a repaint happens on every keystroke in the
      // search box. Publishing is driven by render() and by the app coming to the foreground.
    } else if(st.canRead){
      // READING WITHOUT THE ROLE IS AN ORDINARY STATE, not a broken one — it is what trying the app
      // out looks like, and the screen used to describe it as somebody else's phone.
      el.textContent = 'Your phone’s messages, read from the phone itself. New ones still arrive '
        + 'in whichever app is the default — make PosterChan the default to send from here.';
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
    /* WHY IT IS EMPTY, asked once per visit and never on a keystroke — `emptyWhy` calls the plugin,
     * and paint() runs on every character typed in the search box. Painted again once the answer is
     * in, so the empty list is only briefly the useless kind. */
    if(!S.msgs.size){
      try{ const e = await emptyWhy(); S.emptyWhy = e.why; S.emptyFix = e.fix || ''; }
      catch(_){ S.emptyWhy = ''; S.emptyFix = ''; }
      if(!S.msgs.size) paint();
    }
    // The publish and the drain belong HERE and on foreground, not in paint(): paint runs on every
    // keystroke in the search box, and a mirror per keystroke is a provider read and a relay write
    // per keystroke.
    /* THE PHONE READS ITS OWN MESSAGES FIRST, and does not wait for the relay to hear about them.
     * Publishing is a separate, slower thing that serves the OTHER devices. */
    /* READING AND PUBLISHING ARE TWO DIFFERENT PERMISSIONS AND TWO DIFFERENT JOBS.
     *
     * Reading this phone's own inbox needs READ_SMS. Publishing the archive and performing another
     * device's send need the ROLE, because only the default SMS app may write the provider. Both
     * used to hang off `isPhone()`, so a phone that had not been made the default showed an empty
     * Texts screen with a sentence blaming the role — while the actual blocker was a permission
     * nothing had ever asked for. */
    const st = await phoneState();
    if(st.present && !st.canRead){
      // ASKED ONCE PER VISIT, from the screen that needs it, and only when there is nothing to show
      // — a person reading their messages is not interrupted by a dialog about reading them.
      if(await ensureRead()){
        S.emptyWhy = ''; S.emptyFix = '';
        st.canRead = true;
      }
    }
    // AND TO BE ABLE TO SAY ONE ARRIVED. Only on a device that receives texts — asking a laptop for
    // notification permission about somebody else's phone is a prompt with nothing behind it.
    if(st.present && st.isDefault && !st.canNotify) await ensureNotify();
    if(st.canRead){
      await load();
      /* THE RESULT IS READ HERE TOO, not only on the Allow button. This is the ORDINARY path — the
       * one taken every time somebody opens Texts with the permission already granted — and
       * discarding the answer meant a provider refusal was silent on the one route almost everybody
       * takes. The button path had the message; the path to it did not. */
      loadFromPhone().then((r) => {
        if(r && r.refused && !S.msgs.size){
          S.emptyWhy = 'This phone allowed the permission but its message store still would not '
                     + 'answer. Open \u201cWhy isn\u2019t this working?\u201d below for what it reported.';
          S.emptyFix = '';
        }
        paint();
      }, () => {});
    }
    // PUBLISHING needs to read; PERFORMING A SEND another device asked for needs the role, because
    // only the default SMS app may write the provider. Two jobs, two gates.
    if(st.canRead) mirror();
    if(st.isDefault) drainOutbox();
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
      const st = await phoneState();
      if(!st.canRead && !st.isDefault) return;
      await load();
      if(st.canRead) mirror();
      if(st.isDefault) drainOutbox();
    });
  }
  init();

  window.PCSms = { render, mirror, importAll, loadFromPhone, emptyWhy, ensureRead, phoneState,
                   drainOutbox, send, remove, load,
                   _state: () => S, _key: key, _outboxId: outboxId };
})();
