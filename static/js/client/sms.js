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
    localRead: false,    // this session read rows out of THIS phone's own message store
    /* THE PICTURE-MESSAGE TABLE WOULD NOT ANSWER. A separate fact from `error`, because the texts
       can be on screen and complete while every photo is missing — see loadFromPhone. */
    mmsRefused: false,
    lastRead: null,      // rows the provider returned on the last read — see countLine
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
               /* PICTURE MESSAGES, in two halves. `mms` — this build reads the phone's existing
                * ones. `mmsFetch` — an INCOMING one can be pulled off the carrier's MMSC, which is
                * a different piece of work and is still no. One boolean for both is what lets a
                * screen promise the second while delivering the first. Absent on an older APK,
                * where the honest answer to both is no. */
               mms: !!st.mms, mmsFetch: !!st.mmsFetch,
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
    /* AN EMPTY `defaultPackage` USED TO GET ITS OWN SENTENCE, and it earned its removal.
     *
     * `getDefaultSmsPackage()` returns null on a device with no telephony — which is why a tablet
     * was told to go and pick a messages app — and it ALSO returns null on phones where the role
     * simply has not been assigned. Two very different states, one sentence, and the sentence
     * instructed somebody to do a thing that in one case is impossible and in the other they had
     * already done. It was on screen for a whole day of "the checkbox in settings never works".
     *
     * The device-has-no-radio case is handled before this function is reached. What is left is a
     * phone that is not the default, which the line below already says — accurately, and without
     * claiming to know why. */
    /* NO VERDICT LEFT. This asserted "PosterChan is not the default SMS app on this phone" on the
     * strength of `isDefault`, which reads the message store's default-app ROW — a different table
     * from the ROLE Android granted, and OEM builds do not keep them in step. So it told somebody
     * looking at the switch, ON, in Settings, that they had not done the thing they had done. Every
     * branch above names what Android actually reported; when none applies there is nothing honest
     * left to say. */
    return '';
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
      /* WHICH HALF ANSWERED. The two provider tables are guarded separately on several OEM builds,
       * so "all my photos are missing" and "I have no messages" are different reports with
       * different fixes — and both look like a shorter list. */
      'picture messages: ' + (S.mmsRefused ? 'the phone refused to hand them over'
                            : d.mms === undefined ? 'not read by this build'
                            : (d.mms ? 'read from this phone' : 'not read by this build')
                              + (d.mmsFetch ? ', fetched from the carrier' : ', never fetched from the carrier')),
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
      /* THE ARCHIVE NAMES ATTACHMENTS BUT DOES NOT CARRY THEM (see publishOne), so they arrive
       * without the provider row ids this device would need to fetch them. Normalised to the same
       * shape the phone's own read produces, with `id:0` meaning "not on this device" — the
       * renderer then says "on your phone" instead of drawing a broken image. */
      obj.parts = cleanParts(obj.att || obj.parts);
      if(obj.att) delete obj.att;
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
  /* WHO A NUMBER IS, asked of the address book when the message did not say.
   *
   * On the phone the archive carries a name: the handset resolved it against its own Contacts app
   * before publishing. In the WEB app there is no handset, so a thread published without one showed
   * a bare number while the same person sat in Contacts on the next screen — "I see contacts
   * correctly in contacts but not Texts".
   *
   * The message's own name still wins. It was resolved by the device that received the text, at the
   * time, which is better evidence than an address book that may have been edited since — and it is
   * what makes an unknown number stay unknown rather than acquiring a name from a near-miss. */
  function whoIs(nameFromMsg, address){
    const n = String(nameFromMsg || '').trim();
    if(n) return n;
    try{
      const c = window.PCContacts;
      const found = c && c.nameFor ? c.nameFor(address) : '';
      if(found) return found;
    }catch(_){ }
    return String(address || '');
  }

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
      const who = whoIs(m.name, m.address) || 'a message';
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
    /* WHAT WAS ATTACHED, WITHOUT THE BYTES — and saying so is the point.
     *
     * A laptop that knows a message carried two photos can say "2 photos, on your phone" instead of
     * drawing an empty bubble, which is what "this message failed" looks like. What it must NOT do
     * is claim to have them.
     *
     * The provider ROW ID is deliberately left behind: it is local to this handset, so carrying it
     * across devices would be carrying a number that means something different on each of them —
     * the same reason a message's archive address is derived from the message and never from its
     * row. `type`/`name`/`bytes` ride in the PDU and mean the same everywhere. */
    if(m.mms) body.mms = true;
    if(m.parts && m.parts.length)
      body.att = m.parts.map(p => ({ ct: p.ct, name: p.name, bytes: p.bytes }));

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
      // The contact's name is resolved on the phone against the phone's OWN address book and
      // carried, so a laptop — which has no phone book — shows a name instead of a number.
      const m = fromRow(r);
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
  /* EVERY MESSAGE THIS PHONE HAS, for the screen. Not the archive, not the relay — the provider.
   *
   * It used to ask with `since: 0`, which the plugin answers with the NEWEST 500, then set `since`
   * to the newest date it had just seen and ask for anything newer. There is nothing newer. Two
   * empty rounds and it stopped. So the app read the most recent 500 messages and then walked
   * FORWARD into an empty future, and everything older than that was never read at all — on a phone
   * with years of history, most of it. Reported as "I SEE SOME OLD SMS BUT NOT ALL OF THEM" and
   * "MOST MESSAGES I SENT ARE MISSING", and it has nothing to do with the relay: the messages were
   * on the handset the whole time and the screen never asked for them.
   *
   * `since: 0` returns the newest N, so ONE call with a large N is the whole store. Asked in
   * growing steps rather than at the ceiling every time: most phones are answered by the first,
   * and a bigger ask is a superset of a smaller one, so a full answer is the signal to ask for
   * more. The last step is a bound, not a target — a store larger than that is not going to be
   * read into a web page, and stopping there is better than an unbounded transfer.
   */
  async function loadFromPhone(onProgress){
    const P = plug('list');
    if(!P) return { loaded: 0 };
    const STEPS = [1000, 10000, 50000];
    let total = 0, refused = false, rows = [];
    /* LOCAL, then assigned once at the end — it must describe THIS read. Latched on the state
       object it could only ever go true, so a phone whose picture table failed once wore the
       notice for the rest of the session with its photos on the screen underneath it. */
    let mmsRef = false;
    for(let i = 0; i < STEPS.length; i++){
      try{
        const answer = (await P.list({ since: 0, limit: STEPS[i] })) || {};
        rows = answer.messages || [];
        /* REFUSED IS NOT EMPTY. The plugin returns `[]` for a phone with no texts and for a provider
         * that would not let us look; without this the caller cannot tell, and the screen reported
         * "no messages" over a full inbox with nothing to do about it. */
        if(answer.refused) refused = true;
        /* AND THE TWO TABLES REFUSE INDEPENDENTLY. Several OEM builds guard `content://mms`
         * differently from `content://sms`, so a phone whose texts read perfectly can hand over no
         * picture messages at all — and folded into the flag above that reads as "you have no
         * messages", over a full inbox, which is the exact report this whole screen was rebuilt
         * for. Kept apart so the note can say which half did not answer. */
        if(answer.mmsRefused) mmsRef = true;
      }catch(_){ break; }
      // A short answer means the store is exhausted; a full one means there may be more behind it.
      if(rows.length < STEPS[i]) break;
    }
    S.lastRead = rows.length;      // what the PROVIDER returned, before any of our filtering
    for(const r of rows){
      if(!r || !r.doc) continue;
      if(S.msgs.has(r.doc)) continue;
      S.msgs.set(r.doc, fromRow(r));
      total++;
    }
    S.mmsRefused = mmsRef;
    if(total){
      S.localRead = true;          // rows came out of THIS device's store — see noteWhere
      rebuild();
      if(onProgress) try{ onProgress(total); }catch(_){ }
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
    /* READING THE HISTORY NEEDS READ_SMS, NOT THE ROLE — the same correction already made to
     * `mirror` and to `send`, left behind in the one function that back-fills the past.
     *
     * Gated on being the default SMS app, this never ran once on a phone that had granted the
     * permission and not (or not successfully) handed over its messaging. New messages still
     * arrived, because `mirror` publishes from a high-water mark forward — so the archive grew and
     * looked healthy while everything older than the day PosterChan was installed was simply never
     * imported. Reported as "i still can't see texts I wrote in the past": the past was never
     * fetched, and nothing on any screen said so.
     *
     * The role decides whether messages ARRIVE here and whether a send may be performed. Neither is
     * reading a phone's own inbox. */
    const stI = await phoneState();
    if(!stI.canRead) return { published: 0, why: 'PosterChan cannot read this phone\u2019s messages' };
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
        const m = fromRow(r);
        let ok = false;
        try{ ok = await publishOne(m); }catch(_){ ok = false; }
        // The relay stopped taking them. Stop where we are and report — the next run resumes,
        // because nothing here depends on a mark that has already moved past this point.
        if(!ok) return { published: total, why: 'the relay stopped accepting messages' };
        S.msgs.set(m.doc, m);
        n++; total++;
        if(m.date && m.date < oldest) oldest = m.date;
      }
      if(n) S.localRead = true;      // rows came out of THIS device's store — see noteWhere
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
    /* THE RADIO IS IN THIS DEVICE OR IT IS NOT — the ROLE is a different question.
     *
     * This used to ask `isPhone()`, which is "do we hold the SMS role". On a phone that has not
     * granted it, the answer is no and the message went down the branch below: published as a
     * REQUEST for "your phone" to perform — on the very phone holding it. Nothing was ever going to
     * pick that up, so a text typed on the handset sat in a queue addressed to itself.
     *
     * Sending needs SEND_SMS, which we have; only WRITING the phone's own message store needs the
     * role. So a device with a radio sends its own texts, and `stored` says whether the phone's
     * stock messages app also got a copy. */
    const st0 = await phoneState();
    /* `telephony`, NOT `present`. They are different questions and the names are close enough to
     * pick the wrong one: `present` means the Sms plugin ANSWERED — true of any Android build,
     * including a tablet with no radio — while `telephony` is whether this device can actually put
     * a message on a network. Gating on `present` sent a laptop's text down the radio path, which
     * the archive test caught by name. */
    if(st0.telephony){
      const P = plug('send');
      if(!P) return { ok:false, error:'no messages plugin' };
      let r = null;
      try{ r = await P.send({ to, body }); }catch(e){ return { ok:false, error:String(e) }; }
      if(r && r.ok){
        if(r.stored === false){
          /* WE KEEP THE COPY THE PROVIDER WOULD HAVE. `mirror` republishes from the phone's store,
           * and without the role there is no row there to find — so this message would send and
           * then be missing from the thread it was sent in, which is the exact failure the old
           * refusal was written to avoid. Made up here ONLY because the send SUCCEEDED: the thing
           * that must never be invented is a message nobody received. */
          const at = Date.now();
          try{
            /* THE ARCHIVE'S OWN ID, not `outboxId` — that is the `pcai:smsout:` namespace for a
             * send REQUEST another device performs, and filing a sent message there would put it
             * in front of the drain as a job to do. This is `pcai:sms:`, the same address the
             * phone's own publisher would give it, so if the role is granted later and the message
             * is read back out of the provider it lands on the SAME document instead of appearing
             * twice. */
            const doc = await docIdFor(to, at, body, false);
            const m = { doc, address: to, body, date: at, incoming: false, name: '' };
            /* PUBLISHED, not just remembered. `mirror` republishes from the phone's message store
             * and without the role there is no row there to find — so an in-memory copy is gone on
             * the next load, and the message that was genuinely sent vanishes from the thread it
             * was sent in. Reported as "my new sent message not showing" and "my messages in dad
             * thread are not appearing": they were sent, shown once, and lost on reload.
             *
             * It goes in the archive even if the publish fails — the text really was sent, and the
             * thread should say so on this device whatever the relay did. */
            try{ await publishOne(m); }catch(_){ }
            S.msgs.set(doc, m);
            rebuild();
          }catch(_){ }
          return { ok:true, where:'phone', stored:false };
        }
        // Published from the provider on the next mirror rather than made up here: the phone's row
        // is the message, and inventing a document for one that failed to send would put a message
        // into the archive that nobody ever received.
        mirror({ limit: 20 });
        return { ok:true, where:'phone', stored:true };
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

  /* THE ARCHIVE'S ADDRESS FOR ONE MESSAGE — `SmsKeys.docId` in JavaScript.
   *
   * Same canonical string in both languages or the two halves file the same message at two
   * addresses, and it appears twice in every thread. SECOND-resolution time, deliberately: the
   * provider stores milliseconds, a Nostr event stores seconds, and a message re-read from a
   * restored backup can come back rounded — built from seconds, the two copies still agree.
   * tests/test_android_sms.py runs this against the Java. */
  async function docIdFor(address, dateMs, body, incoming, partsKey){
    let canon = key(address) + '\n' + Math.floor(dateMs / 1000) + '\n'
              + (incoming ? 'in' : 'out') + '\n' + (body || '');
    /* THE ATTACHMENTS COUNT TOWARDS IDENTITY, and only when there are any.
     *
     * A picture message frequently has NO TEXT, and then who/when/direction/body is the identical
     * string for two photos sent inside one second — filed at one address, the second replaces the
     * first and one of the two is simply gone from every device that is not the handset.
     *
     * An EMPTY key must leave the string byte-identical to the four-argument form, or a text-only
     * message read back through the MMS path is a second document for a message already archived
     * and it appears twice in the thread. SmsKeys.docId does exactly this and
     * tests/test_android_sms.py runs the two against each other. */
    if(partsKey) canon = canon + '\n' + partsKey;
    return D_MSG + (await sha256hex(canon)).slice(0, 24);
  }

  /* One attachment's share of that identity — SmsKeys.partKey in JavaScript. Type, the name the
   * SENDER's phone chose and the length: all three ride in the PDU, so they are the same on every
   * device that received the message, unlike a provider row id. The separators are stripped out of
   * the values or a filename containing a `;` shifts every field after it. */
  function partKey(ct, name, bytes){
    const f = v => String(v == null ? '' : v).replace(/[;:\r\n]/g, '_');
    return f(ct) + ':' + f(name) + ':' + (bytes === undefined || bytes === null ? -1 : bytes);
  }

  /* IN THE ORDER THE MESSAGE CARRIES THEM, never sorted — `seq` is part of the message and two
   * attachments of the same type and length swapping places is a different message. */
  function partsKeyOf(parts){
    return (parts || []).map(p => partKey(p.ct, p.name, p.bytes)).filter(Boolean).join(';');
  }

  /* One provider row as the archive holds it. ONE definition, because it is built in three places
   * (the first mirror, the phone's own read, and the back-fill) and a field missing from one of
   * them is a picture message that is a picture on one screen and a blank bubble on another. */
  function fromRow(r){
    return { doc: r.doc, address: r.address, body: r.body, date: r.date,
             incoming: !!r.incoming, name: r.name || '',
             // Carried rather than inferred from `parts` being non-empty: a picture message whose
             // attachments could not be read is still a picture message.
             mms: !!r.mms, parts: cleanParts(r.parts) };
  }

  /* WHAT THE PHONE SAID WAS ATTACHED, reduced to what the archive carries. The provider ROW IDS are
   * kept for this device only — `id` is local to one handset, so it is never published (see
   * publishOne), the same rule that keeps a restored backup from re-minting every document. */
  function cleanParts(parts){
    return (parts || []).map(p => ({ id: Number(p.id) || 0, ct: String(p.ct || ''),
                                     name: String(p.name || ''),
                                     bytes: p.bytes === undefined ? -1 : Number(p.bytes) }));
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
    /* PERFORMING A SEND NEEDS A RADIO, NOT THE ROLE. Same correction as `send`, `mirror` and
     * `importAll`: SmsManager needs SEND_SMS, and the role only decides whether messages arrive and
     * whether the phone's own store may be written. Gated on the role, a laptop's request sat
     * unperformed on a handset perfectly able to send it — and the laptop's screen said "waiting
     * for your phone" for ever, which is true and useless.
     *
     * A device with no radio still does nothing here, which is the rule that actually matters: it
     * is what stops a laptop and a phone both answering the same request. */
    const stD = await phoneState();
    if(!stD.telephony) return 0;
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
        const ids = [], mmsIds = [];
        /* TWO PROVIDERS, TWO URIs. A picture message is a row in `content://mms` and deleting it
         * through the SMS uri removes nothing AND reports nothing — which the guard below reads as
         * a provider refusal, so the archive is (correctly) left alone and the delete quietly did
         * not happen, every time, with the message still on screen. */
        for(const r of rows){
          if(!want.has(r.doc) || !r.id) continue;
          (r.mms ? mmsIds : ids).push(r.id);
        }
        if(ids.length || mmsIds.length){
          phone = (((await P.delete({ ids, mmsIds })) || {}).deleted) || 0;
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

  /* ASK ANDROID FOR PERMISSION TO READ THIS PHONE'S MESSAGES, then actually read them.
   *
   * One implementation for both copies of the button — the empty state's and the one under the
   * header. Two copies of a flow whose difficulty is what to say when Android stops offering the
   * dialog is how one of them ends up saying nothing. */
  async function askForRead(btn){
    if(btn) btn.disabled = true;
    const ok = await ensureRead();
    if(!ok){
      /* A REFUSAL IS SAID OUT LOUD. Android stops showing its dialog after two refusals, at which
       * point the only way through is the app's own settings page — a button that silently does
       * nothing reads as a broken app. */
      S.emptyWhy = 'Android is not offering the prompt any more. Open Settings \u2192 Apps \u2192 '
                 + 'PosterChan \u2192 Permissions \u2192 SMS and allow it there.';
      S.emptyFix = '';
      if(btn) btn.disabled = false;
      paint();
      return;
    }
    S.emptyWhy = ''; S.emptyFix = '';
    await loadFromPhone();
    if(btn) btn.disabled = false;
    paint();
  }

  /* BRING IN THE HISTORY, saying how far it has got. It walks backwards in windows and can take a
   * while on a long history, and a button that looks dead for a minute is a button people press
   * again. */
  let _bfRunning = false;
  async function runBackfill(btn){
    if(_bfRunning) return;
    _bfRunning = true;
    const note = PC.$('#sms-deep-note');
    if(btn) btn.disabled = true;
    if(note) note.textContent = 'reading\u2026';
    let r = null;
    try{ r = await importAll((n) => { if(note) note.textContent = n + ' brought in\u2026'; }); }
    catch(e){ r = { published: 0, why: String((e && e.message) || e) }; }
    _bfRunning = false;
    if(btn) btn.disabled = false;
    if(note){
      note.textContent = r && r.published
        ? r.published + ' older message' + (r.published === 1 ? '' : 's') + ' brought in'
        : (r && r.why) || 'nothing older found';
    }
    paint();
  }

  /* WHAT THIS SCREEN IS ACTUALLY HOLDING, in numbers.
   *
   * "WHERE ARE MY MESSAGES!" cannot be answered from here: every link in the chain reads correctly
   * in the source, and the only thing that would settle it is what the phone measured. So the panel
   * says how many rows came back from the provider on the last read, how many documents the screen
   * is holding, how many of them are outgoing, and how many threads that makes. A missing history
   * and a missing DIRECTION look identical on screen and are different bugs. */
  function countLine(){
    let out = 0, gone = 0;
    for(const m of S.msgs.values()){
      if(m && m.gone){ gone++; continue; }
      if(m && !m.incoming) out++;
    }
    return 'last read: ' + (S.lastRead == null ? 'not attempted' : S.lastRead + ' from the phone')
         + ' \u00b7 holding: ' + (S.msgs.size - gone)
         + ' (' + out + ' sent, ' + (S.msgs.size - gone - out) + ' received)'
         + ' \u00b7 threads: ' + S.threads.length;
  }

  /* ASK ANDROID FOR THE SMS ROLE. One implementation, two buttons — the empty state's and the one
   * in the header that is reachable once messages are on screen. They were about to be two copies,
   * and two copies of a flow whose whole difficulty is what to say when it silently fails is how
   * one of them ends up saying nothing.
   *
   * THE HOME-SCREEN PLUGIN, not the Sms one. The role dialog belongs to the shell half
   * (HomePlugin.requestSms is what the settings card's switch calls), and asking the Sms plugin for
   * it gets a proxy that answers every name and then rejects — a button that does nothing, on the
   * control that exists to stop doing nothing. */
  async function askForRole(btn){
    const P = PC.capPlugin ? PC.capPlugin('HomeScreen', 'requestSms') : null;
    if(!P || !P.requestSms){
      S.emptyWhy = 'This build cannot ask for it — update the app.'; paint(); return;
    }
    if(btn) btn.disabled = true;
    let held = false;
    try{ held = !!((await P.requestSms()) || {}).isDefault; }catch(_){ held = false; }
    /* RE-ASKED AFTERWARDS RATHER THAN BELIEVED. Android refuses a role the app cannot hold by
     * starting the request activity and finishing it immediately — no dialog, no error, nothing in
     * any log — which is indistinguishable from somebody declining. The state decides what to say. */
    const st2 = await phoneState();
    if(st2.isDefault || held){
      S.emptyWhy = ''; S.emptyFix = '';
      await loadFromPhone();
      paint();
      return;
    }
    S.emptyWhy = roleLine(st2) + ' Android did not change it just now — open Default apps below '
               + 'and set Messages there.';
    S.emptyFix = 'role';
    paint();
  }

  // ---------------------------------------------------------------- attachments

  /* PICTURE MESSAGES, DRAWN.
   *
   * The bytes live in the phone's own MMS part table and NOWHERE ELSE — until the archive learns to
   * carry them, a photo can only be shown on the handset that received it. That is a fact the screen
   * has to state rather than hide: a bubble with an attachment it cannot draw must say which of the
   * four things happened, because "on your phone", "too large", "this phone refused" and "this build
   * is too old" all render as the same broken image otherwise, and only some of them are fixable.
   *
   * Fetched ONE AT A TIME, when something is about to show it: a thread of forty photos handed
   * through the Capacitor bridge as base64 in one reply is a hundred megabytes of string, and the
   * WebView holds a copy on top of the blob.
   */
  const ATT = new Map();      // provider part id -> { url, ct } | { why }
  /* A CEILING, because a decoded attachment is a live object URL and a Map is not a cache until
   * something evicts from it. Scrolling a decade of picture messages would otherwise hold every
   * photo ever drawn for the life of the page. Oldest-first, which for this map is insertion order
   * and therefore the order they were opened in. */
  const ATT_MAX = 120;

  function attRemember(id, v){
    ATT.set(id, v);
    while(ATT.size > ATT_MAX){
      const k = ATT.keys().next().value;
      const old = ATT.get(k);
      ATT.delete(k);
      if(old && old.url) try{ URL.revokeObjectURL(old.url); }catch(_){ }
    }
  }

  function isImage(ct){ return /^image\//.test(String(ct || '')); }
  function isVideo(ct){ return /^video\//.test(String(ct || '')); }
  function isAudio(ct){ return /^audio\//.test(String(ct || '')); }

  /* A human name for an attachment, for the snippet and for the bubble that cannot draw one.
   * "Photo" beats "image/jpeg" everywhere a person is reading rather than debugging. */
  function attLabel(p){
    const ct = String((p && p.ct) || '');
    if(isImage(ct)) return 'Photo';
    if(isVideo(ct)) return 'Video';
    if(isAudio(ct)) return 'Audio';
    if(/vcard|x-vcard/i.test(ct)) return 'Contact card';
    return (p && p.name) || ct || 'Attachment';
  }

  /* The one-line description of a message with no words in it. An empty snippet in the thread list
   * reads as a conversation that has gone quiet, which is the opposite of what happened. */
  function snippetOf(m){
    const body = String((m && m.body) || '').slice(0, 90);
    if(body) return body;
    const parts = (m && m.parts) || [];
    if(!parts.length) return '';
    if(parts.length === 1) return attLabel(parts[0]);
    return parts.length + ' attachments';
  }

  async function partData(p){
    const id = Number(p && p.id) || 0;
    /* NOT ON THIS DEVICE, and that is not a failure. The archive names what was attached and
     * deliberately does not carry the bytes or the provider row id — a row id means something
     * different on every phone. So a laptop knows there is a photo and knows where it is. */
    if(!id) return { why: attLabel(p) + ' \u00b7 on your phone' };
    if(ATT.has(id)) return ATT.get(id);
    const P = plug('attachment');
    if(!P || !P.attachment){
      const r = { why: attLabel(p) + ' \u00b7 this app is too old to open it' };
      attRemember(id, r);
      return r;
    }
    let a = null;
    try{ a = await P.attachment({ part: id }); }catch(_){ a = null; }
    let r;
    if(a && a.data){
      try{
        const bin = atob(a.data);
        const buf = new Uint8Array(bin.length);
        for(let i = 0; i < bin.length; i++) buf[i] = bin.charCodeAt(i);
        const blob = new Blob([buf], { type: p.ct || 'application/octet-stream' });
        r = { url: URL.createObjectURL(blob), blob, ct: p.ct || '' };
      }catch(_){ r = { why: attLabel(p) + ' \u00b7 could not be decoded' }; }
    } else if(a && a.tooBig){
      // A REAL FILE THAT WILL NOT FIT THROUGH THE BRIDGE — still openable in the phone's gallery,
      // which is worth saying, because it is a completely different situation from a refusal.
      r = { why: attLabel(p) + ' \u00b7 too large to show here \u2014 open it in your gallery' };
    } else {
      r = { why: attLabel(p) + ' \u00b7 this phone would not hand it over' };
    }
    attRemember(id, r);
    return r;
  }

  /* The placeholder an attachment is drawn into. It carries everything the hydrate needs, because
   * `paint()` rebuilds `#feed` wholesale on every keystroke and a closure over the message would be
   * a closure over a dead node. */
  function attHtml(p, enc, mi, pi){
    return '<div class="sms-att" data-m="' + mi + '" data-p="' + pi + '">'
         + '<span class="muted small">' + enc(attLabel(p)) + '\u2026</span></div>';
  }

  /* FILL THEM IN AFTER THE DRAW, one at a time, and never let one bad attachment cost the thread.
   * `noteHtml`'s lesson, one screen over: an exception inside a list builder takes the list AND
   * every binding made after it, which reads as three unrelated bugs. */
  async function hydrateAtt(root, msgs){
    const els = Array.from(root.querySelectorAll('.sms-att'));
    for(const el of els){
      const m = msgs[Number(el.dataset.m)];
      if(!m) continue;
      const p = (m.parts || [])[Number(el.dataset.p)] || null;
      if(!p) continue;
      let d = null;
      try{ d = await partData(p); }catch(_){ d = { why: 'could not be read' }; }
      if(!el.isConnected) return;              // the view moved on while we were reading
      if(d && d.url) drawAtt(el, p, d);
      else el.innerHTML = '<span class="muted small">' + PC.enc(String((d && d.why) || '')) + '</span>';
    }
  }

  function drawAtt(el, p, d){
    el.innerHTML = '';
    if(isImage(p.ct)){
      const img = document.createElement('img');
      img.className = 'sms-att-img';
      img.src = d.url;
      img.alt = p.name || 'Photo';
      // The app's own lightbox, so a photo in a text opens the way a photo in a post does.
      img.onclick = () => { try{ PC.openLightbox(d.url, 'image'); }catch(_){ } };
      el.appendChild(img);
      return;
    }
    if(isVideo(p.ct) || isAudio(p.ct)){
      const v = document.createElement(isVideo(p.ct) ? 'video' : 'audio');
      v.className = 'sms-att-img';
      v.controls = true;
      v.src = d.url;
      el.appendChild(v);
      return;
    }
    const b = document.createElement('button');
    b.className = 'btn small';
    b.textContent = 'Save ' + attLabel(p);
    /* PC.saveBlobAs, NEVER a bare `<a download>`. The APK's WebView ignores a programmatic download
     * and the desktop's app:// origin refuses one, so an anchor is a button that silently does
     * nothing on two of the three platforms this ships to. */
    b.onclick = async () => {
      try{ await PC.saveBlobAs(d.blob, p.name || ('attachment.' + (String(p.ct||'').split('/')[1] || 'bin'))); }
      catch(_){ PC.toast('could not save that attachment'); }
    };
    el.appendChild(b);
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
          || (t.msgs.some(m => snippetOf(m).toLowerCase().includes(q)))
          || String((t.msgs[t.msgs.length-1]||{}).name||'').toLowerCase().includes(q);
    });

    feed.innerHTML = `
      <div class="sms-wrap">
        <div class="sms-head">
          <input class="input" id="sms-q" placeholder="Search messages" value="${enc(S.q)}">
          <button class="btn btn-neon small" id="sms-new">${ICO('plus','b-ic')}New</button>
        </div>
        <div class="muted small" id="sms-note"></div>
        <!-- THE ROLE IS ASKED FOR HERE, WHERE IT IS ALWAYS REACHABLE.
             It used to live in the EMPTY state only, which is exactly backwards: reading works
             WITHOUT the role and sending does not, so the moment somebody's texts appeared — the
             moment they would try to reply — the only control that grants it disappeared. Reported
             as "there is no button", then "PosterChan is not this phone's messaging app when i send
             message", which is the same fact from either end.
             Hidden by default and revealed by noteWhere, because it is a question only a device
             that CAN hold the role should be asked, and only while it does not. -->
        <div id="sms-rolebar" style="display:none;margin-top:8px">
          <button class="btn btn-neon small" id="sms-role2">Make PosterChan my messages app</button>
          <button class="btn small" id="sms-defaults2" style="margin-left:8px">Android\u2019s Default apps</button>
        </div>
        <!-- THE BACK-FILL, REACHABLE WITH MESSAGES ON SCREEN. It lived in the empty state only,
             which is exactly backwards: somebody with an empty screen has nothing to compare
             against, and somebody looking at a half-filled thread is the person who KNOWS history
             is missing. "MOST MESSAGES I SENT ARE MISSING! I SEEN 1 THREAD WITH A FEW REPLIES". -->
        <!-- PERMISSION, WHERE IT CAN BE SEEN. "Allow PosterChan to read them" lived inside the
             empty-state block, the one guarded by rows.join('') or the fallback, so it appeared
             single thread on screen. With an archive published earlier, the list is never empty:
             the screen showed a couple of hundred old messages, looked like it was working, and
             the one control that would have let it read the phone was unreachable. That is where
             "I SEE SOME OLD SMS BUT NOT ALL OF THEM" came from, and no amount of fixing the reader
             helped, because the reader was never being run. -->
        <div id="sms-perm" style="display:none;margin-top:8px">
          <button class="btn btn-neon small" id="sms-allow2">Allow PosterChan to read my messages</button>
          <span class="muted small" style="margin-left:8px">showing an older copy until then</span>
        </div>
        <div id="sms-backfill" style="display:none;margin-top:8px">
          <button class="btn small" id="sms-deep2">Bring in older messages</button>
          <span class="muted small" id="sms-deep-note" style="margin-left:8px"></span>
        </div>
        <div class="muted small" style="margin-top:6px">
          <button class="btn small" id="sms-why">Why isn\u2019t this working?</button></div>
        <div class="sms-threads">${rows.map(t => {
          const last = t.msgs[t.msgs.length-1] || {};
          const who = whoIs(last.name, t.address);
          return `<button class="sms-thread" data-k="${enc(t.key)}">
            <div class="sms-av">${enc(initials(who))}</div>
            <div class="sms-body">
              <div class="sms-row1"><span class="sms-who">${enc(who)}</span>
                <span class="sms-when muted">${enc(when(last.date))}</span></div>
              <!-- A PICTURE MESSAGE HAS NO WORDS, and an empty snippet reads as a conversation
                   that has gone quiet — the opposite of what happened. -->
              <div class="sms-snip muted">${enc(snippetOf(last))}</div>
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

    /* Bound whether or not the bar is visible right now — `noteWhere` reveals it asynchronously,
       after this runs, and a button revealed with no handler is the dead-button bug one layer on. */
    { const rb = PC.$('#sms-role2'); if(rb) rb.onclick = () => askForRole(rb); }
    /* `#sms-deep` HAD NO HANDLER AT ALL. It was drawn in the empty state and bound nowhere, so
     * `importAll` — the only thing that fetches messages OLDER than the first sync — was defined,
     * exported, and called by nothing. Every message anybody had was whatever `mirror` published
     * from its high-water mark forward. Both ids are bound here so the empty-state copy works too. */
    for(const id of ['#sms-deep', '#sms-deep2']){
      const b = PC.$(id);
      if(b) b.onclick = () => runBackfill(b);
    }
    { const ab = PC.$('#sms-allow2'); if(ab) ab.onclick = () => askForRead(ab); }
    { const db = PC.$('#sms-defaults2'); if(db) db.onclick = () => {
        const P = PC.capPlugin ? PC.capPlugin('HomeScreen', 'openDefaultApps') : null;
        try{ if(P && P.openDefaultApps) P.openDefaultApps(); }catch(_){}
      }; }
    const q = PC.$('#sms-q');
    if(q) q.oninput = () => { S.q = q.value; paint(); q.focus(); };
    const nw = PC.$('#sms-new');
    if(nw) nw.onclick = composeNew;
    const dbg = PC.$('#sms-why');
    if(dbg) dbg.onclick = async () => {
      dbg.disabled = true;
      const d = await details();
      const el = PC.$('#sms-note');
      /* SHOWN, not written into a collapsed element. `noteWhere` hides this line on a phone — there
       * is nothing worth captioning there — and the diagnostic used the same element, so pressing
       * the button printed the answer into `display:none`. A diagnostic that cannot be read is
       * worse than none: it looks like the button does nothing, on the screen somebody opened
       * because nothing was working. */
      if(el){
        el.style.display = '';
        el.textContent = (d ? detailLine(d) : 'This build cannot report it — it is older than this screen.')
                       + ' \u00b7 ' + countLine();
      }
    };
    const role = PC.$('#sms-role');
    if(role) role.onclick = () => askForRole(role);
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
    /* WHAT ACTUALLY HAPPENED BEATS WHAT THE STATE SAYS.
     *
     * The last branch describes a device reading somebody ELSE'S phone over Nostr — "your phone has
     * to be reachable" — and it was chosen whenever `phoneState()` answered `canRead:false`, which
     * includes every way that call can simply fail: a plugin that did not answer, a bridge not ready,
     * an exception swallowed into the default. So a person holding their own phone, looking at their
     * own texts on the screen directly below, was told this was a remote copy. That is not a wrong
     * emphasis; it is a sentence contradicted by the thing it sits on top of.
     *
     * Having read rows out of THIS device's store is a fact, and it outranks a re-queried
     * permission. Only a `list` that returned messages can set it. */
    /* Shown only where it can do something: a device that can hold the role and does not. A tablet
     * with no radio is never offered it, and a phone that already IS the default is not asked again. */
    const bar = PC.$('#sms-rolebar');
    if(bar) bar.style.display = (st.present && !st.isDefault) ? '' : 'none';
    // Offered on any device that can read this phone's own store — that is the only device the
    // history can come FROM.
    const bf = PC.$('#sms-backfill');
    if(bf) bf.style.display = st.canRead ? '' : 'none';
    // The permission is the FIRST thing to fix: without it nothing else on this screen can reach
    // the phone, and every other offer here is noise.
    const pm = PC.$('#sms-perm');
    if(pm) pm.style.display = (st.present && !st.canRead) ? '' : 'none';
    /* THE PICTURE-MESSAGE TABLE WOULD NOT ANSWER, and this IS news — it goes above the emptying
     * branch below for exactly that reason. A thread that silently lost its photos looks like a
     * thread somebody sent fewer photos in: there is nothing on the screen to notice, and the
     * texts beside them are complete and correct. Several OEM builds guard `content://mms`
     * separately from `content://sms`, so this is a real state on a working phone. */
    if(S.mmsRefused){
      el.style.display = '';
      el.textContent = 'This phone would not let PosterChan read your picture messages, so only '
        + 'texts are shown here. The pictures are still on the phone.';
      return;
    }
    /* ON A PHONE THIS LINE SAYS NOTHING, because there is nothing to say.
     *
     * It began as an instruction about the role, shown to somebody who already held it. Replacing
     * that with "showing this phone's messages, read from the phone itself" was worse: a caption
     * describing the obvious, permanently, above somebody's messages. No messaging app narrates
     * itself.
     *
     * The line exists for the one case that IS news — this device is not the phone, so these
     * arrived over the relay and a send has to be performed by a handset that may be switched off.
     * Everywhere else it is emptied and collapsed so it takes no room. */
    if(S.localRead || st.canRead || st.isDefault){
      el.textContent = '';
      el.style.display = 'none';
      return;
    }
    el.style.display = '';
    el.textContent = 'An encrypted copy of your phone\u2019s messages. Sending from here asks your '
      + 'phone to send it, so your phone has to be reachable.';
  }

  function paintThread(feed, enc){
    const t = S.threads.find(x => x.key === S.open);
    if(!t){ S.open = ''; return paint(); }
    const who = whoIs((t.msgs[t.msgs.length-1] || {}).name, t.address);
    feed.innerHTML = `
      <div class="sms-wrap">
        <div class="sms-head">
          <button class="btn small" id="sms-back">${ICO('arrow-left','b-ic')}</button>
          <div class="sms-title">${enc(who)}</div>
        </div>
        <div class="sms-msgs">${t.msgs.map((m, i) => `
          <div class="sms-msg ${m.incoming ? 'them' : 'me'}" data-doc="${enc(m.doc)}">
            <div class="sms-bub${(m.parts||[]).length ? ' has-att' : ''}">${(m.parts||[]).map((p, j) => attHtml(p, enc, i, j)).join('')}${
              /* THE ATTACHMENTS COME FIRST AND THE CAPTION UNDER THEM, which is where every
                 messages app puts it — and a bubble whose only content is an attachment must not
                 also render an empty text node, or it collapses to a sliver. */
              m.body ? '<div class="sms-txt">' + enc(m.body) + '</div>' : ''}</div>
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
    /* THE PICTURES ARRIVE AFTER THE DRAW, and each one that lands pushes everything below it
       down. A thread opens at its newest message (the line above), so re-pinning as they land is
       what keeps it there instead of drifting backwards through the conversation as the photos
       above resolve. Guarded on there BEING attachments, so an ordinary text thread does no work. */
    if(t.msgs.some(m => (m.parts || []).length)){
      hydrateAtt(feed, t.msgs).then(() => {
        const l = PC.$('.sms-msgs');
        if(l) l.scrollTop = l.scrollHeight;
      }, () => {});
    }
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
                   _state: () => S, _key: key, _outboxId: outboxId, _docId: docIdFor,
                   // The attachment identity rules, for tests/test_android_mms.py — which runs them
                   // against SmsKeys.partKey/partsKey in Java, because a picture message filed at
                   // two addresses appears twice in the thread.
                   _partKey: partKey, _partsKey: partsKeyOf };
})();
