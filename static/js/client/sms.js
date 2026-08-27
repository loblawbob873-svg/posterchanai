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
  const HWM_FIX = () => HWM() + '_oldest_first_v1';
  /* A SEPARATE MIGRATION MARKER FOR BLOSSOM STORAGE.
   *
   * Reusing HWM_FIX here was the reason an established phone showed zero Messages/MMS files. That
   * flag had already been written by the older ordering repair, so when message bodies moved from
   * inline Nostr events to encrypted Blossom the phone kept its high-water mark at "today" and
   * never revisited any of the rows that needed migrating. A schema migration needs its own marker;
   * otherwise a completed, unrelated migration silently opts the account out of this one. */
  /* v3 re-audits installs whose v2 latch was written before attachment failures, capped MMS reads
   * and stalled migrations were reported honestly. Those phones can have a "complete" marker and
   * zero portable MMS hashes; keeping the same marker makes every later fix unreachable. */
  /* v4 deliberately invalidates the v3 completion latch. A v3 phone could mark the migration
   * complete after copying message bodies while its MMS part table was empty/refused; subsequent
   * releases then trusted that stale success forever, leaving every older conversation text-only. */
  /* v5 invalidates the v4 latch because v4 could still declare the migration complete when the
   * SMS provider answered but the independently-guarded MMS table refused. That exact state leaves
   * every old conversation present as text while only new picture messages have media. */
  const HWM_BLOSSOM = () => HWM() + '_blossom_v5';
  /* AND A SEPARATE MARKER FOR THE REWIND ITSELF, because rewinding is a ONE-TIME ACT and finishing
   * the migration is a different question entirely.
   *
   * The rewind below used to be keyed on HWM_BLOSSOM — "have we finished?" — so every ordinary
   * sweep taken before the migration completed dragged the high-water mark back to the thirty-day
   * boundary again. On a phone where the migration cannot complete (one attachment the provider
   * will not hand over is enough) that is not a slow start, it is a PERMANENT LOOP: every sweep
   * restarts at the same point, hits the same row, and republishes the same thirty days for ever.
   * Keyed on having rewound, the mark moves forward the way it is documented to. */
  const HWM_REWOUND = () => HWM() + '_blossom_rewound_v4';
  /* AND A WAY BACK OUT OF "DONE", because every one of these markers is a LATCH and a latch that
   * was set wrongly is permanent.
   *
   * `HWM_BLOSSOM` is read at the top of migrateLocalHistory and returns immediately when set, so a
   * phone that once decided it had copied everything never walks its history again — no matter how
   * many bugs are fixed underneath it afterwards. Every fix so far has been to the code that DECIDES
   * to set it; none of them helps a device that already did. That device installs the new build,
   * opens Texts, sees a full screen of messages and a migration that does nothing, and there is
   * nothing on screen to say why or to press.
   *
   * Clearing the high-water mark too, not just the completion flag: the mark is what makes the
   * ordinary sweep start at "now" rather than at the beginning, so leaving it would re-run a
   * migration that still could not reach anything old. */
  function resetArchiveMarkers(){
    /* CLEARED AND THEN CHECKED, because this is the one control whose whole job is to unstick a
     * device and a silent no-op here leaves the person pressing a button that does nothing.
     *
     * `removeItem` is the right call and every real browser has it, but this app also runs inside
     * two WebViews and a bundled desktop shell, and a storage shim that implements only
     * get/set is not hypothetical -- the test harness itself was one, which is how a reset that
     * cleared nothing passed as working. An empty string reads back falsy through `getItem`, so the
     * fallback satisfies every check that guards these latches. */
    for(const key of [HWM_BLOSSOM(), HWM_REWOUND(), HWM_FIX(), HWM()]){
      try{ localStorage.removeItem(key); }catch(_){ }
      try{ if(localStorage.getItem(key)) localStorage.setItem(key, ''); }catch(_){ }
    }
    _migrationFailed.clear();
  }

  /* Re-read the phone and re-publish anything the archive is missing. The deliberate, person-pressed
   * version of what every visit does quietly -- offered because the failure this exists for is
   * SILENT: the screen is full, the status line says nothing is wrong, and the pictures are simply
   * not on the other devices. */
  async function rescan(){
    resetArchiveMarkers();
    S.archive.error = '';
    S.archive.published = 0;
    await loadFromPhone();
    const r = await migrateLocalHistory();
    await mirror();
    return r;
  }

  let _messagesFolderReady = false;
  const _migrationFailed = new Set();
  /* A cancelled outbox command is its own tombstone. Relay pools and the local Store can hand us
   * an older request after the newer cancellation (including in a later refresh); without keeping
   * this watermark, that stale request recreates a sending bubble and can reach drainOutbox again. */
  const _cancelledOutboxAt = new Map();

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
    /* AND THE PICTURE TABLE ANSWERED, BUT NOT ALL OF IT. A third fact, not a shade of the two
       above: `MmsStore.MAX_ROWS` hands back the newest 2,000 picture messages and there is no way
       to ask for the rest. Read as an exhausted store, the archive walks what it was given, finds
       nothing left to do and reports that it has copied the phone — so the OLDEST pictures are not
       slow to reach Blossom, they are never offered to it, and every screen says it finished. */
    mmsCapped: false,
    lastRead: null,      // rows the provider returned on the last read — see countLine
    loading: false,
    error: '',
    archive: { running:false, published:0, error:'', attempted:false },
    /* One conversation can be parked while another is opened. Keep the scroll state by thread,
       not as one module-wide number, or returning to a conversation borrows the previous one's
       offset. `bottom` is intent: new content may keep a person at latest only when they were
       already there. */
    scroll: Object.create(null),
    attach: null,         // File waiting in the open conversation's composer
    /* THE FLOOR FOR NOTIFICATIONS, set once when the module loads. A first sync pulls a phone's
       whole history through the subscription, and every one of those is "new" to this device — a
       thousand notifications for messages read weeks ago. Only something that arrived AFTER this
       page did is an event; everything older is history. */
    since: Date.now() - 120000,
  };
  let blossomLaunch=false;
  function clearAttachment(){ S.attach=null; }
  function isMmsFile(file){ return !!file&&(/^(?:image|video)\//i.test(file.type||'')||/\.(?:jpe?g|png|gif|webp|heic|heif|avif|mp4|m4v|mov|webm|3gp)$/i.test(file.name||'')); }

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
                * a different piece of work. One boolean for both is what lets a
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
      /* A background handset answers a desktop send by replacing its outbox request with a signed,
       * encrypted completion. That receipt is the only authoritative moment the desktop knows the
       * radio accepted it. Turn it into the ordinary sent bubble, durably, instead of leaving the
       * thread unchanged after showing a transient "sent" toast. */
      if(d.startsWith(D_OUT) && ev.content){
        if((_cancelledOutboxAt.get(d) || 0) >= Number(ev.created_at || 0)) continue;
        try{
          const ack = JSON.parse(await PC.nip44dec(ME().pubkey, ev.content));
          /* Cancellation is not a failed message. Retire every local rendering of this command
           * before trying to interpret its old request payload; different devices may have keyed
           * the placeholder at slightly different receipt times, but the outbox id is shared. */
          if(ack && ack.done && ack.cancelled){
            _cancelledOutboxAt.set(d, Number(ev.created_at || 0));
            for(const [oldDoc, old] of S.msgs){
              if(old && old.outbox === d)
                S.msgs.set(oldDoc, {doc:oldDoc, _at:ev.created_at, gone:true});
            }
            continue;
          }
          /* Old web builds put the request behind a Blossom envelope. New requests stay inline so
           * Android's background service can perform them without a WebView or Blossom client. */
          const sent = ack && ack.request ? await openMessageBody(ack.request) : await openMessageBody(ack);
          if(sent && sent.to && (sent.body || sent.attachment)){
            const at = Number(sent.at) || Number(ev.created_at || 0) * 1000 || Date.now();
            const sentParts=sent.attachment?[{id:0,ct:sent.attachment.mime||'application/octet-stream',name:sent.attachment.name||'',bytes:Number(sent.attachment.bytes)||-1,sha:String(sent.attachment.sha||''),thumb:'',nothumb:1}]:[];
            /* The provider's MMS document includes its attachment identity. Filing the pending
             * request/receipt as a text-only document guarantees a second bubble when the handset
             * subsequently mirrors the provider row. Use the same portable type/name/size key on
             * both sides; ordinary SMS remains byte-for-byte unchanged. */
            const md = await docIdFor(sent.to, at, sent.body || '', false, partsKeyOf(sentParts));
            const have = S.msgs.get(md);
            /* A queued bubble is keyed at the ASK time; a provider row is keyed at the actual
             * radio time. A phone that wakes later therefore has a different correct document id.
             * Retire the placeholder tied to this outbox before installing the receipt, otherwise
             * both bubbles remain and the user sees every remotely-sent message twice. */
            if(ack.done) for(const [oldDoc, old] of S.msgs){
              if(oldDoc !== md && old && old.pending && old.outbox === d)
                S.msgs.set(oldDoc, {doc:oldDoc, _at:ev.created_at, gone:true});
            }
            if(!ack.done){
              if(!have || !Number(have._at) || have._at < ev.created_at)
                S.msgs.set(md, { doc:md, address:sent.to, body:sent.body || '', date:at,
                                 incoming:false, name:'', parts:sentParts, pending:true, outbox:d,
                                 _at:ev.created_at });
            }else if(ack.done && ack.ok && ack.pending){
              /* Android has accepted an asynchronous MMS transaction, but its carrier callback
               * has not happened yet. `done` retires the command so no phone sends it twice;
               * `pending` keeps the bubble honest until provider mirroring replaces it with the
               * eventual SENT/FAILED row. */
              if(!have || have.pending || have.failed || !Number(have._at) || have._at < ev.created_at)
                S.msgs.set(md, { doc:md, address:sent.to, body:sent.body || '', date:at,
                                 incoming:false, name:'', parts:sentParts, pending:true, outbox:d,
                                 _at:ev.created_at });
            }else if(ack.done && ack.ok){
              if(!have || have.pending || have.failed || !Number(have._at) || have._at < ev.created_at)
                S.msgs.set(md, { doc:md, address:sent.to, body:sent.body || '', date:at,
                                 incoming:false, name:'', parts:sentParts, _at:ev.created_at });
            }else if(!have || have.pending || have.failed){
              /* FAILURE MUST ALSO BE RECONSTRUCTABLE FROM THE RECEIPT ALONE. The pending bubble is
               * keyed at ask time, while `md` is keyed at the phone's actual attempt time. Above we
               * correctly retire the ask-time bubble, but this branch used to require `have` at the
               * new id before drawing the failure. On a fresh desktop—or an ordinary delayed send—
               * there is no such entry, so the failed MMS vanished completely and could neither be
               * inspected nor deleted. Keep the outbox id so remove() can tombstone this receipt and
               * prevent it recreating the failed bubble on the next refresh. An existing successful
               * provider/archive row still wins: it is stronger evidence than an ambiguous failure. */
              S.msgs.set(md, { doc:md, address:sent.to, body:sent.body || '', date:at,
                               incoming:false, name:'', parts:sentParts, failed:true, outbox:d,
                               error:String(ack.error || 'not sent'), _at:ev.created_at });
            }
          }
        }catch(_){}
        continue;
      }
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
      try{
        const envelope = JSON.parse(await PC.nip44dec(ME().pubkey, ev.content));
        obj = await openMessageBody(envelope);
        if(envelope && envelope.blob) obj._blob = envelope.blob;
      }
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
    /* Android providers commonly return the address itself in the display-name column when their
     * contact join has not warmed yet. That is not a name and must not prevent the portable Contacts
     * index below from resolving it on this device. Compare through the same phone-number key so
     * formatting differences (+1, spaces, parentheses) do not disguise the fallback value. */
    if(n && key(n) !== key(address)) return n;
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

  let _cacheDrain = null;
  async function load(force){
    if(S.ready && !force) return;
    S.loading = true;
    // CACHE FIRST, network behind it — the rule every list in this app follows, and the archive is
    // entirely the user's own already-synced data.
    let cached = [];
    try{ cached = Store().query([FILTER()]) || []; }catch(_){ cached = []; }
    /* FIRST PAINT IS A PAGE, NOT THE WHOLE ARCHIVE. FILTER permits 20,000 addressable documents and
     * every one is NIP-44 encrypted. Awaiting them serially before drawing made Texts an endless
     * spinner—especially with a signer, where decrypt is not a cheap local function. Newest-first is
     * already absorb's conflict rule, so paint the newest page and drain history in bounded batches.
     * Older events cannot replace a newer `_at`, even when this continuation overlaps a relay read. */
    cached.sort((a,b) => (b.created_at||0) - (a.created_at||0));
    const first = cached.splice(0, 32);
    await absorb(first);
    S.ready = true;
    S.loading = false;
    paint();
    if(cached.length && !_cacheDrain){
      _cacheDrain = (async () => {
        while(cached.length){
          await absorb(cached.splice(0, 128));
          if(textsOnScreen()) paint();
          /* Give navigation, typing and the compositor a turn between decrypt batches. */
          await new Promise(resolve => setTimeout(resolve, 0));
        }
      })().catch(() => {}).finally(() => { _cacheDrain = null; });
    }
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
        /* A cancellation/tombstone mutates an existing entry to `gone`; the Map size is unchanged.
         * Repainting only when size changed left deleted attachments visible on an open phone until
         * navigation/reload. Always repaint the active Texts view after a live archive event. */
        if(textsOnScreen()) paint();
        if(S.msgs.size !== before){
          notifyNew(ev);
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

  /* MESSAGE ARCHIVING IS A DRIVE TRANSACTION. Each encrypted upload mutates FilesIdx, whose normal
   * save is deliberately debounced. Android may freeze the WebView as soon as the user leaves
   * Texts; a debounce that has not fired then leaves the ciphertext on Blossom but no folder/file
   * entry pointing at it — exactly "no Messages/MMS folder" despite successful uploads. Pull once
   * before the transaction, batch every body/attachment, and await endBatch before reporting the
   * mirror pass complete. FilesIdx batches are counted, so this composes safely with another import. */
  async function beginArchiveDrive(){
    const fi = PC.filesIdx ? PC.filesIdx() : null;
    if(!fi || !fi.addFolder || !fi.beginBatch || !fi.endBatch)
      throw new Error('encrypted message storage is unavailable');
    if(fi.pull) await fi.pull();
    fi.beginBatch();
    return fi;
  }

  async function endArchiveDrive(fi){
    if(!fi) return false;
    const ok = await fi.endBatch();
    if(!ok) throw new Error('could not save the Messages/MMS file index');
    return true;
  }

  async function ensureMmsFolder(){
    const fi = PC.filesIdx ? PC.filesIdx() : null;
    if(!fi || !fi.addFolder) throw new Error('encrypted message storage is unavailable');
    if(!fi.folders || !fi.folders().includes('MMS')) fi.addFolder('MMS', true);
  }

  async function ensureMessagesFolder(){
    if(_messagesFolderReady) return;
    const fi = PC.filesIdx ? PC.filesIdx() : null;
    if(!fi || !fi.addFolder) throw new Error('encrypted message storage is unavailable');
    if(!fi.folders || !fi.folders().includes('Messages')) fi.addFolder('Messages', true);
    _messagesFolderReady = true;
  }

  /* Message bodies belong in the encrypted Blossom drive too. The relay keeps the small encrypted
   * pointer needed for live sync and deletion ordering, never the message payload. Existing inline
   * NIP-44 records remain readable so this is a storage migration, not a history reset. */
  async function archiveMessageBody(doc, body){
    if(!PC.uploadEncFile) throw new Error('encrypted message storage is unavailable');
    await ensureMessagesFolder();
    const file = new File([JSON.stringify(body)], 'message-' + doc.slice(-24) + '.json',
                          {type:'application/json'});
    const sha = await PC.uploadEncFile(file, 'Messages');
    return {v:1, blob:sha, mime:'application/json'};
  }

  async function openMessageBody(envelope){
    if(!envelope || !/^[0-9a-f]{64}$/i.test(String(envelope.blob||''))) return envelope;
    if(!PC.encFileUrl) throw new Error('encrypted message storage is unavailable');
    const url = await PC.encFileUrl(envelope.blob, envelope.mime || 'application/json');
    return await fetch(url).then(r=>r.json());
  }

  /* Store one provider attachment in the account's encrypted Blossom drive. The Nostr document
   * carries only the ciphertext hash; the media server never sees the photo and a relay event never
   * has to carry megabytes of base64. A failed upload fails this message's mirror pass so the high
   * water mark stays behind it and the next sweep retries instead of permanently archiving a hollow
   * attachment. */
  async function archivePart(p){
    if(p.sha && (p.thumb || p.nothumb || !isImage(p.ct))) return p.sha;
    const d = await partData(p);
    if(!d || !d.blob) throw new Error((d && d.why) || 'could not read MMS attachment');
    if(!PC.uploadEncFile) throw new Error('encrypted file storage is unavailable');
    const name = p.name || ('mms.' + (String(p.ct || '').split('/')[1] || 'bin'));
    if(!p.sha) p.sha = await PC.uploadEncFile(new File([d.blob], name, {
      type: p.ct || d.blob.type || 'application/octet-stream'
    }), 'MMS');
    /* A thread needs a picture, not the original camera file.  Archive one small encrypted preview
     * beside it so every laptop/tablet does not download several megabytes merely by opening the
     * conversation.  The original is fetched only when the thumbnail is tapped. */
    /* A PREVIEW THAT COULD NOT BE MADE IS RECORDED AS A FACT, not left as an unmet requirement.
     *
     * The original is stored and the message is complete; only the bandwidth saving is missing. But
     * `needsPartUpgrade` read a missing thumbnail as "this MMS is not archived yet", so a picture
     * the WebView cannot decode — a format it has no decoder for, a truncated part, a decode that
     * lost a race with memory pressure — was NEVER done: offered again on every migration batch,
     * republished on every pass, blocking the completion marker, and so dragging the high-water
     * mark back thirty days on every sweep behind it. Nothing was logged, because from each pass's
     * own point of view the upload succeeded.
     *
     * `nothumb` rides with the attachment and is published, so the other devices agree rather than
     * each re-deriving "incomplete" from the same absence. */
    if(isImage(p.ct) && !p.thumb && !p.nothumb){
      try{
        const bm = await createImageBitmap(d.blob);
        const scale = Math.min(1, 512 / Math.max(bm.width, bm.height));
        const c = document.createElement('canvas');
        c.width = Math.max(1, Math.round(bm.width * scale));
        c.height = Math.max(1, Math.round(bm.height * scale));
        c.getContext('2d').drawImage(bm, 0, 0, c.width, c.height);
        if(bm.close) bm.close();
        const preview = await new Promise(resolve => c.toBlob(resolve, 'image/jpeg', .72));
        if(preview) p.thumb = await PC.uploadEncFile(
          new File([preview], 'thumb-' + name.replace(/\.[^.]*$/, '') + '.jpg', {type:'image/jpeg'}),
          'MMS');
        else p.nothumb = 1;
      }catch(_){
        /* The original is still complete; a thumbnail failure must not hollow the MMS — nor make it
         * eternally pending. */
        p.nothumb = 1;
      }
    }
    return p.sha;
  }

  async function publishOne(m){
    const body = {
      address: m.address, body: m.body, date: m.date,
      incoming: !!m.incoming, name: m.name || '',
    };
    /* MMS bytes live in the encrypted `MMS` Blossom folder. Provider row ids remain local to the
     * handset; the content hash is portable and decryptable by every client holding the drive key. */
    if(m.mms) body.mms = true;
    if(m.failed) body.failed = true;
    if(m.pending) body.pending = true;
    if(m.error) body.error = String(m.error);
    if(m.parts && m.parts.length){
      await ensureMmsFolder();
      body.att = [];
      for(const p of m.parts){
        const sha = await archivePart(p);
        const att = { ct: p.ct, name: p.name, bytes: p.bytes, sha, thumb: p.thumb || '' };
        // Only when it is true: an absent key keeps an ordinary picture message's document
        // byte-identical to the one every earlier build published.
        if(p.nothumb) att.nt = 1;
        body.att.push(att);
      }
    }

    /* Uploads can succeed before the relay publish fails. Keep that encrypted pointer on the local
     * pending row so a reconnect retries the small event instead of creating another encrypted
     * body (and another copy of every MMS attachment) on Blossom. `_pendingBlob` is never put on
     * the wire; `_blob` still means the relay-backed archive is complete. */
    const envelope = m._pendingBlob || await archiveMessageBody(m.doc, body);
    m._pendingBlob = envelope;
    m._blob = envelope.blob;
    const ct = await PC.nip44enc(ME().pubkey, JSON.stringify(envelope));
    const r = await PC.publish(KIND, ct, [['d', m.doc], ['l', L_TAG]], {quiet:true, noQueue:true});
    const ok = !!(r && r.ok);
    if(ok) delete m._pendingBlob;
    return ok;
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
    /* Repair archives made by the old newest-first limited query. Its mark could sit beyond rows
     * the provider never returned, so changing the order alone cannot recover them. Rewind each
     * account once to the documented first-sync boundary; complete archive rows are cheap skips,
     * while missing texts and hollow MMS are published again. */
    try{
      if(!localStorage.getItem(HWM_FIX())){
        since = Math.min(since || Date.now(), Date.now() - FIRST_RUN_DAYS * 86400000);
        localStorage.setItem(HWM(), String(since));
        localStorage.setItem(HWM_FIX(), '1');
      }
    }catch(_){ }
    /* Existing archives predate encrypted Blossom. Rewind once even when the ordering migration
     * above has already run, so those provider rows are offered to needsArchiveUpgrade(), which can
     * replace their inline/body-only documents and upload their MMS bytes. */
    try{
      if(!localStorage.getItem(HWM_BLOSSOM()) && !localStorage.getItem(HWM_REWOUND())){
        since = Math.min(since || Date.now(), Date.now() - FIRST_RUN_DAYS * 86400000);
        localStorage.setItem(HWM(), String(since));
        localStorage.setItem(HWM_REWOUND(), '1');
      }
    }catch(_){ }
    if(!since) since = Date.now() - FIRST_RUN_DAYS * 86400000;
    // MMS dates have one-second precision. Overlap the last second and deduplicate by document so
    // two messages filed in that second cannot be skipped by the strict provider predicate.
    const querySince = Math.max(0, since - 1000);
    let rows = [], migrationRemaining = 0;
    if(opts && opts.fullMigration){
      /* loadFromPhone has already read the complete provider into S. Use THAT set, not a `since`
       * query: `since` deliberately reads forwards from the high-water mark and therefore cannot
       * discover years of older history. `_local` is set only by fromRow, so a relay-only laptop
       * can never mistake its cached archive for a phone provider. */
      rows = Array.from(S.msgs.values()).filter(m => m && m._local
          && !_migrationFailed.has(m.doc) && needsArchiveUpgrade(m, m))
        .sort((a,b) => (a.date || 0) - (b.date || 0));
      migrationRemaining = rows.length;
      rows = rows.slice(0, Math.max(1, (opts && opts.limit) || 60));
    }else{
      try{ rows = ((await P.list({ since: querySince, limit: (opts && opts.limit) || 400 })) || {}).messages || []; }
      catch(_){ return { published:0, skipped:'could not read the phone' }; }
    }

    let n = 0, top = since, drive = null, archiveError = '', rowErrors = 0;
    /* ONCE A ROW HAS FAILED, THE MARK STOPS MOVING — but the sweep carries on. See the failure
     * branch below: those are two separate promises and they used to be made with one `break`. */
    let stuck = false;
    S.archive.running = true;
    S.archive.attempted = true;
    S.archive.error = '';
    try{ drive = await beginArchiveDrive(); }
    catch(e){
      S.archive.running = false;
      S.archive.error = String((e && e.message) || e);
      if(textsOnScreen()) paint();
      return { published:0, skipped:S.archive.error };
    }
    try{
      for(const r of rows){
        if(!r || !r.doc) continue;
        const old = S.msgs.get(r.doc);
        if(old && !needsArchiveUpgrade(r, old)) { if(!stuck && r.date > top) top = r.date; continue; }
        // The contact's name is resolved on the phone against the phone's OWN address book and
        // carried, so a laptop — which has no phone book — shows a name instead of a number.
        const m = fromRow(r);
        let ok = false;
        try{ ok = await publishOne(m); }
        catch(e){ archiveError = String((e && e.message) || e); ok = false; }
        if(!ok){
          /* ONE MMS THE PROVIDER WILL NOT HAND OVER MUST NOT BLOCK THE REST OF THE PHONE FOR EVER,
           * AND THAT IS TRUE OF THIS SWEEP TOO.
           *
           * The migration path was taught this and the ORDINARY sweep — the one that actually runs,
           * on every visit and every foreground — kept its `break`. Two facts turn that into a
           * total, permanent stop rather than a delay: the provider answers `since` queries OLDEST
           * FIRST, so the wall stands in front of everything newer than the bad row; and the sweep
           * restarts before it every time, because the mark stays behind it by design. One picture
           * message the provider will not hand over therefore froze the whole archive at that date
           * — texts included — with a full Texts screen in front of the person, and the reason none
           * of the media reached Blossom was that nothing after that row was ever offered to it.
           *
           * The mark still stays behind the failed row, which is the promise that matters: no
           * message is ever skipped and the next run retries this one. What changes is that the
           * rows behind the wall are no longer collateral. */
          _migrationFailed.add(r.doc);
          /* Retain successful encrypted uploads for the next foreground retry, but do not retain
           * `_blob`: that field means a relay document points at the body, which is not true yet. */
          delete m._blob;
          S.msgs.set(m.doc, Object.assign({}, old || {}, m));
          rowErrors++;
          archiveError = '';
          stuck = true;                // the mark freezes here; the rest of the batch still lands
          continue;
        }
        S.msgs.set(m.doc, m);
        n++;
        if(!stuck && r.date > top) top = r.date;
      }
    }finally{
      try{ await endArchiveDrive(drive); }
      catch(e){ archiveError = String((e && e.message) || e); }
    }
    if(n){ rebuild(); }
    // Advanced only past messages that really landed. A partial batch resumes; it never skips.
    try{ if(top > since) localStorage.setItem(HWM(), String(top)); }catch(_){ }
    /* Mark the migration only after the drive transaction itself committed. A failed signer,
     * upload, relay publish or index save leaves it unset, so the next foreground pass retries the
     * same rows instead of declaring a hollow archive complete. */
    /* AND A TRUNCATED PROVIDER READ CANNOT COMPLETE A MIGRATION OF THE PHONE.
     *
     * Everything else in this condition is about whether the rows we WERE GIVEN landed. `mmsCapped`
     * is about whether we were given the phone: past the ceiling the oldest picture messages are
     * not in `S.msgs` at all, so the candidate set is empty for the honest reason that nothing
     * asked for them. Marked complete on that, the migration never runs again and those pictures
     * are never archived — with the screen saying it finished, which is the worst version of it. */
    if(!archiveError && !rowErrors && !_migrationFailed.size && !S.mmsRefused && !S.mmsCapped
       && opts && opts.fullMigration && migrationRemaining <= rows.length){
      try{ localStorage.setItem(HWM_BLOSSOM(), '1'); }catch(_){ }
    }
    S.archive.running = false;
    S.archive.published = n;
    S.archive.error = archiveError || (rowErrors || _migrationFailed.size
      ? _migrationFailed.size + ' MMS attachment' + (_migrationFailed.size === 1 ? '' : 's')
        + ' could not be read; the other messages were copied'
      : '');
    if(textsOnScreen()) paint();
    return { published:n, remaining:Math.max(0, migrationRemaining - rows.length),
             failed:_migrationFailed.size, skipped:archiveError || undefined };
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
    let mmsRef = false, mmsCap = false;
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
        /* TRUNCATED IS NOT EXHAUSTED — the same distinction one step along, and the one the
         * completion marker depends on. Older APKs never send it; absent is false, which is what
         * those builds have always effectively claimed. */
        if(answer.mmsCapped) mmsCap = true;
      }catch(_){ break; }
      // A short answer means the store is exhausted; a full one means there may be more behind it.
      if(rows.length < STEPS[i]) break;
    }
    S.lastRead = rows.length;      // what the PROVIDER returned, before any of our filtering
    for(const r of rows){
      if(!r || !r.doc) continue;
      const old = S.msgs.get(r.doc);
      if(old && !needsArchiveUpgrade(r, old)) continue;
      const local = fromRow(r);
      /* On the handset, prefer its live provider part ids so the attachment can be opened now.
       * Preserve portable hashes already present in the archive by position while a repair upload
       * is pending; replacing those with empty strings would make a working remote copy regress. */
      if(old && old.parts) for(let i=0;i<local.parts.length;i++){
        if(!old.parts[i]) continue;
        if(old.parts[i].sha) local.parts[i].sha = old.parts[i].sha;
        if(old.parts[i].thumb) local.parts[i].thumb = old.parts[i].thumb;
        if(old.parts[i].nothumb) local.parts[i].nothumb = 1;
      }
      S.msgs.set(r.doc, Object.assign({}, old || {}, local));
      total++;
    }
    S.mmsRefused = mmsRef;
    S.mmsCapped = mmsCap;
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
    let total = 0, quiet = 0;
    /* START AT NOW, NOT AT THE OLDEST ROW ALREADY ON SCREEN.
     *
     * `loadFromPhone` deliberately puts provider rows in S before this runs, including MMS whose
     * attachment has never reached encrypted Blossom. Using the oldest S date as the first cursor
     * skipped every one of those visible-but-unarchived rows and asked only for messages older than
     * them. This is especially destructive after a capped MMS read: the newest 2,000 pictures look
     * complete locally, none of their bytes are portable, and “Bring in older messages” walks away
     * from all of them. Audit from now; needsArchiveUpgrade makes completed archive rows cheap skips. */
    let edge = Date.now() + 1;
    for(let round = 0; round < 400 && quiet < 2; round++){
      let rows = [];
      try{ rows = ((await P.list({ before: edge, limit: 400 })) || {}).messages || []; }
      catch(_){ return { published: total, why: 'could not read the phone' }; }
      /* An older APK ignores `before` and answers with its newest page. Do not mistake that for the
       * beginning of history and set the completion latch. Strict pages also eliminate timestamp
       * overlap if an OEM provider treats `<` as `<=`. */
      const offered = rows.length;
      rows = rows.filter(r => r && Number(r.date) < edge);
      if(offered && !rows.length)
        return { published:total, why:'this phone build cannot page older message history yet' };
      let n = 0, oldest = edge;
      for(const r of rows){
        if(!r || !r.doc) continue;
        /* PAGINATION FOLLOWS THE PROVIDER PAGE, not the number of new archive writes.  On an
         * established phone the newest page is normally already complete; advancing `oldest` only
         * after publish reread that page twice, tripped the quiet guard and never reached page 2. */
        if(Number(r.date) && Number(r.date) < oldest) oldest = Number(r.date);
        const old = S.msgs.get(r.doc);
        if(old && !needsArchiveUpgrade(r, old)) continue;
        const m = fromRow(r);
        let ok = false;
        try{ ok = await publishOne(m); }catch(_){ ok = false; }
        // The relay stopped taking them. Stop where we are and report — the next run resumes,
        // because nothing here depends on a mark that has already moved past this point.
        if(!ok) return { published: total, why: 'the relay stopped accepting messages' };
        S.msgs.set(m.doc, m);
        n++; total++;
      }
      if(n) S.localRead = true;      // rows came out of THIS device's store — see noteWhere
      quiet = n ? 0 : quiet + 1;
      if(n){ rebuild(); if(onProgress) try{ onProgress(total); }catch(_){ } }
      if(rows.length < 400) break;            // provider exhausted: reached the beginning of time
      edge = oldest;                          // strict-before makes the next page non-overlapping
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
  async function fileB64(file){
    const bytes = new Uint8Array(await file.arrayBuffer());
    let s = '';
    for(let i=0;i<bytes.length;i+=0x8000) s += String.fromCharCode(...bytes.subarray(i,i+0x8000));
    return btoa(s);
  }

  /* ── A FILE TOO BIG FOR A PICTURE MESSAGE GOES AS A LINK ─────────────────────────────────────
   *
   * An oversized MMS does not fail in a way anybody can act on. The carrier's MMSC re-compresses it
   * into mush, or accepts it and delivers nothing, or the transaction times out minutes later with
   * the message sitting in the thread looking sent. There is no error to show and nothing to retry.
   *
   * So the size is checked BEFORE sending, against the number this carrier actually published
   * (SmsPlugin.mmsLimit → the platform's own MMS carrier config, the same one the transport applies)
   * rather than against a constant compiled into an app that has never met this SIM.
   *
   * Over the line, the file is encrypted under a FRESH RANDOM KEY, the ciphertext is uploaded, and
   * what goes out is a text message with a link whose FRAGMENT carries the key. A fragment is never
   * transmitted to a server, so the node holds ciphertext it cannot read; `/f/<sha>` is a page that
   * decrypts in the recipient's own browser, which is what makes this work for somebody who has
   * never heard of this app. The honest limit of it is that the link IS the secret — anyone holding
   * it can open the file — and a text message is not a confidential channel. The page says so, and
   * so does the note appended to the message.
   *
   * Plaintext was the obvious alternative and is worse than it looks: Blossom has no read
   * authorization at all and `GET /list/<pubkey>` enumerates a sender's blobs, so an unencrypted
   * attachment is not merely guessable by URL, it is LISTABLE by anyone who knows the sender. */
  const MMS_HEADROOM = 8 * 1024;      // PDU headers, the text part, and the carrier's own slack
  const MMS_FLOOR = 64 * 1024;        // below this a "limit" is a misreport, not a policy

  let _mmsLimit = null;
  async function mmsLimit(){
    if(_mmsLimit) return _mmsLimit;
    const P = plug('mmsLimit');
    let bytes = 0, measured = false;
    if(P && P.mmsLimit){
      try{
        const r = (await P.mmsLimit()) || {};
        bytes = Number(r.bytes) || 0;
        measured = !!r.measured;
      }catch(_){ /* an older APK has no such method — fall through to the floor */ }
    }
    // A carrier that reports something absurd is not obeyed: a 4KB "limit" would send every photo
    // as a link, which is a worse outcome than one oversized MMS.
    if(!(bytes > MMS_FLOOR)){ bytes = 300 * 1024; measured = false; }
    _mmsLimit = { bytes, measured };
    return _mmsLimit;
  }

  /* Build the recipient's link out of what uploadSharedEnc produced.
   *
   * It returns a reference to the BLOSSOM blob (`https://…/<sha>.enc#pcenc1=<meta>`), which only
   * this client understands. The page that anyone can open lives on THIS instance at `/f/<sha>`, so
   * the sha is lifted out and the fragment carried across untouched — the fragment is the key and
   * must never be regenerated, logged, or round-tripped through anything that could alter it.
   *
   * When the blob did NOT land on this instance (the account points at somebody else's media
   * server) the page has nowhere to fetch it from, so the blob's own URL is added to the meta as
   * `u`. That is still inside the fragment, so it stays off the server — and it is only added when
   * it is needed, because every character here is one more chance for a linkifier to clip the link. */
  function shareLinkFor(ref){
    const cut = String(ref || '').split('#pcenc1=');
    if(cut.length !== 2) return '';
    const head = cut[0];
    let meta = cut[1];
    const sha = (head.match(/([0-9a-f]{64})/i) || [])[1];
    if(!sha) return '';
    const base = String((typeof window !== 'undefined' && window.__PC_API_BASE__) || '')
                 || (typeof location !== 'undefined' ? location.origin : '');
    if(!base) return '';
    let sameHost = false;
    try{ sameHost = new URL(head, base).origin === new URL(base).origin; }catch(_){ sameHost = false; }
    if(!sameHost){
      try{
        const dec = JSON.parse(new TextDecoder().decode(_b64uDec(meta)));
        dec.u = head;
        meta = _b64u(new TextEncoder().encode(JSON.stringify(dec)));
      }catch(_){ return ''; }     // an unreadable descriptor must not become a half-formed link
    }
    return base.replace(/\/+$/, '') + '/f/' + sha.toLowerCase() + '#pcenc1=' + meta;
  }
  const _b64u = b => btoa(String.fromCharCode.apply(null, b))
                       .replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
  function _b64uDec(str){
    let t = String(str || '').replace(/-/g, '+').replace(/_/g, '/');
    while(t.length % 4) t += '=';
    const raw = atob(t), out = new Uint8Array(raw.length);
    for(let i = 0; i < raw.length; i++) out[i] = raw.charCodeAt(i);
    return out;
  }

  /* Send `file` as a link instead of as an attachment. Returns null when it cannot, so the caller
   * can fall back to trying the MMS rather than refusing to send anything at all. */
  async function sendAsLink(to, body, file, limit, remote){
    if(!PC.uploadSharedEnc) return null;
    let ref = '';
    try{ ref = await PC.uploadSharedEnc(file); }
    catch(_){
      /* OFFLINE MUST NOT DISABLE THE PHONE'S RADIO. A normal camera photo is almost always larger
       * than a carrier MMS ceiling, so the preferred encrypted-link route is attempted first. But
       * Blossom/relay connectivity and carrier connectivity are independent: rejecting here made
       * Add photo silently depend on the account being online even while SMS itself worked. `null`
       * means "this optional route is unavailable" and makes send() try the actual MMS transport.
       * The MMS library will resize for the carrier; if that transport rejects it, its own error is
       * the one the person needs to see. */
      return null;
    }
    const link = shareLinkFor(ref);
    if(!link) return null;
    const note = (body ? body + '\n\n' : '')
               + (file.name ? file.name + ' \u00b7 ' : '')
               + fmtBytes(file.size) + ' \u2014 too big to send as a picture message, so here it is '
               + 'as a private link:\n' + link;
    /* A WEB/DESKTOP DEVICE HAS NO RADIO. Queue the resulting TEXT command for the phone instead
       of requiring a local Sms plugin—the old requirement made the safe oversize path impossible
       on precisely the surfaces that needed it. `file` is null, so this recursive call cannot
       re-enter the attachment/link branch. */
    if(remote){
      const queued = await send(to, note, null);
      if(!queued || !queued.ok) return queued || {ok:false,error:'could not queue the link'};
      return {ok:true, where:'queued-link', link, limit:limit&&limit.bytes, doc:queued.doc};
    }
    const P = plug('send');
    if(!P || !P.send) return null;
    try{
      const r = await P.send({ to, body: note });
      if(!r || !r.ok) return { ok:false, error:(r && r.error) || 'the phone would not send it' };
    }catch(e){ return { ok:false, error:String(e) }; }
    return { ok:true, where:'link', link, limit: limit && limit.bytes };
  }

  function fmtBytes(n){
    n = Number(n) || 0;
    const u = ['B', 'KB', 'MB', 'GB'];
    let i = 0;
    while(n >= 1024 && i < u.length - 1){ n /= 1024; i++; }
    return (i ? n.toFixed(1) : String(n)) + ' ' + u[i];
  }

  async function send(to, body, file){
    if(!to || (!body && !file)) return { ok:false, error:'nothing to send' };
    if(file&&!isMmsFile(file))return {ok:false,error:'MMS supports photos and videos'};
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
    /* CHECKED ON EVERY COMPOSING DEVICE, not only the phone. A desktop used to encrypt and queue a
       multi-megabyte MMS request without ever taking this branch; the phone then inherited an
       oversized carrier send with no chance to offer the safe link. A remote device cannot measure
       that SIM, so mmsLimit() uses the documented conservative default. */
    if(file){
      const limit = await mmsLimit();
      if(file.size > Math.max(MMS_FLOOR, limit.bytes - MMS_HEADROOM)){
        const viaLink = await sendAsLink(to, body, file, limit, !st0.telephony);
        // `null` means encrypted storage was unavailable. A phone may still try its MMS transport;
        // a desktop falls through to the existing encrypted outbox attachment rather than losing it.
        if(viaLink) return viaLink;
      }
    }
    if(st0.telephony){
      const P = plug(file ? 'sendMms' : 'send');
      if(!P) return { ok:false, error:'no messages plugin' };
      let r = null;
      try{
        r = file ? await P.sendMms({ to, body, data:await fileB64(file),
                                     mime:file.type||'image/jpeg', name:file.name||'photo.jpg' })
                 : await P.send({ to, body });
      }catch(e){ return { ok:false, error:String(e) }; }
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
            /* A successful MMS on a phone that is not the default Messages app has no provider
             * row for `mirror` to recover. Preserve the photo in the encrypted archive now, just
             * as the remote-send path does, or every other client receives a text-only bubble. */
            if(file){
              try{
                await ensureMmsFolder();
                const sha = await PC.uploadEncFile(file, 'MMS');
                m.mms = true;
                m.parts = [{ ct:file.type||'image/jpeg', name:file.name||'photo.jpg',
                             bytes:file.size, sha }];
              }catch(_){
                /* The carrier send already succeeded. Keep the truthful text record even when
                 * encrypted media storage is temporarily unreachable; never report it unsent. */
              }
            }
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
    let attachment = null;
    if(file){
      if(!/^image\//i.test(file.type||'')) return {ok:false,error:'MMS currently supports photos'};
      try{
        const sha = await PC.uploadEncFile(file, 'MMS');
        attachment = {sha, mime:file.type||'image/jpeg', name:file.name||'photo.jpg', bytes:file.size};
      }catch(e){ return {ok:false,error:'could not encrypt attachment: '+String(e&&e.message||e)}; }
    }
    /* COMMANDS STAY INLINE. Message archives use encrypted Blossom to keep history off the relay,
     * but the Android background service is deliberately tiny: it decrypts NIP-44 and talks to the
     * radio while no WebView is running. Hiding `to` and `body` behind a Blossom pointer made every
     * background web send a valid-looking no-op. The event is still NIP-44 encrypted to ourselves. */
    const request = { to, body, at, attachment };
    const ct = await PC.nip44enc(ME().pubkey, JSON.stringify(request));
    const r = await PC.publish(KIND, ct, [['d', doc], ['l', L_TAG]], {quiet:true, noQueue:true});
    if(r && r.ok){
      const pendingParts=attachment?[{id:0,ct:attachment.mime,name:attachment.name,bytes:attachment.bytes,sha:attachment.sha,thumb:'',nothumb:1}]:[];
      const md = await docIdFor(to, at, body || '', false, partsKeyOf(pendingParts));
      S.msgs.set(md, { doc:md, address:to, body:body || '', date:at, incoming:false, name:'',
                       parts:pendingParts,
                       pending:true, outbox:doc, _at:Number((r.ev && r.ev.created_at) || now()) });
      rebuild();
      return { ok:true, where:'queued', doc };
    }
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
  /* A THREAD OF BLANK BUBBLES IS ALSO A THREAD THAT IS LOSING MESSAGES.
   *
   * The archive address is a hash of who, when, direction, body and — since the MMS work — the
   * attachments. A message with NEITHER text nor attachments therefore hashes the same as every
   * other empty one to the same person in the same second, and the second silently replaces the
   * first. So "my moms message is all empty bubbles" and "some threads are missing my part of the
   * convo" are one fault seen from two sides: the blank ones are the evidence for the missing ones.
   *
   * Nothing here invents content. It counts, so the screen can say a conversation came back
   * unreadable instead of quietly presenting it as short. */
  function blankCount(msgs){
    let n = 0;
    for(const m of (msgs || [])){
      if(m && !m.gone && !String(m.body || '').trim() && !((m.parts || []).length)) n++;
    }
    return n;
  }

  function blankNote(n){
    if(!n) return '';
    return '<div class="muted small" style="padding:8px 12px">' + n + ' message'
         + (n === 1 ? '' : 's') + ' in this conversation could not be displayed.</div>';
  }

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
             _local: true,
             // Carried rather than inferred from `parts` being non-empty: a picture message whose
             // attachments could not be read is still a picture message.
             mms: !!r.mms, parts: cleanParts(r.parts),
             failed: !!r.failed, pending: !!r.pending, error: String(r.error || '') };
  }

  /* WHAT THE PHONE SAID WAS ATTACHED, reduced to what the archive carries. The provider ROW IDS are
   * kept for this device only — `id` is local to one handset, so it is never published (see
   * publishOne), the same rule that keeps a restored backup from re-minting every document. */
  function cleanParts(parts){
    return (parts || []).map(p => ({ id: Number(p.id) || 0, ct: String(p.ct || ''),
                                     name: String(p.name || ''),
                                     bytes: p.bytes === undefined ? -1 : Number(p.bytes),
                                     sha: /^[0-9a-f]{64}$/i.test(String(p.sha || ''))
                                       ? String(p.sha).toLowerCase() : '',
                                     thumb: /^[0-9a-f]{64}$/i.test(String(p.thumb || ''))
                                       ? String(p.thumb).toLowerCase() : '',
                                     // `nt` on the wire, `nothumb` in memory — see archivePart.
                                     nothumb: (p.nothumb || p.nt) ? 1 : 0 }));
  }

  /* An archived MMS is complete only when every attachment has a portable encrypted-store address.
   * A body-only version of the same document is not a duplicate to skip: older clients published
   * exactly that shape, and the phone still has the provider part ids needed to repair it. */
  function needsPartUpgrade(phone, archived){
    const src = (phone && phone.parts) || [];
    if(!src.length) return false;
    const dst = (archived && archived.parts) || [];
    return dst.length !== src.length || dst.some(p =>
      !/^[0-9a-f]{64}$/i.test(String(p.sha || '')) ||
      (isImage(p.ct) && !p.nothumb && !/^[0-9a-f]{64}$/i.test(String(p.thumb || ''))));
  }

  function needsArchiveUpgrade(phone, archived){
    /* Carrier callbacks change state without changing message identity or attachment. An existing
       body blob must not freeze web/desktop at the pre-callback state forever. */
    const stateChanged = !!archived &&
      (!!phone.failed !== !!archived.failed || !!phone.pending !== !!archived.pending
       || String(phone.error || '') !== String(archived.error || ''));
    return !archived || !archived._blob || needsPartUpgrade(phone, archived) || stateChanged;
  }

  let _fullMigration = null;
  function migrateLocalHistory(){
    if(_fullMigration) return _fullMigration;
    try{ if(localStorage.getItem(HWM_BLOSSOM())) return Promise.resolve({published:0,remaining:0}); }
    catch(_){ }
    _fullMigration = (async () => {
      let total = 0, lastRemaining = Infinity;
      for(let batch=0; batch<1000; batch++){
        const r = await mirror({fullMigration:true, limit:60});
        total += Number(r && r.published) || 0;
        if(!r || r.skipped || !r.remaining) return {published:total, remaining:(r&&r.remaining)||0,
                                                     failed:(r&&r.failed)||0, skipped:r&&r.skipped};
        /* A BATCH THAT DID NOT SHORTEN THE QUEUE WILL NOT SHORTEN IT NEXT TIME EITHER.
         *
         * Loop-until-dry, never loop-until-the-safety-limit. Every pass PULLS and SAVES the
         * encrypted file index (see beginArchiveDrive), so a queue that has stopped moving used to
         * cost a thousand rewrites of a replaceable document for one opening of the Texts screen —
         * on a phone, on a radio, while the person waits for the screen. Anything that can make a
         * row perpetually pending (the thumbnail rule was one, for years of somebody's pictures)
         * turns that safety limit into the ordinary path rather than the impossible one. */
        if(r.remaining >= lastRemaining)
          return {published:total, remaining:r.remaining, failed:(r && r.failed) || 0,
                  skipped:'message migration stopped making progress'};
        lastRemaining = r.remaining;
        /* Let rendering/input and Android lifecycle callbacks run between upload batches. */
        await new Promise(resolve => setTimeout(resolve, 0));
      }
      return {published:total, remaining:1, skipped:'message migration safety limit reached'};
    })().finally(() => { _fullMigration = null; });
    return _fullMigration;
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
  let _drainingOutbox = null;
  async function drainOutbox(){
    /* Coalesce the visibility hook, Texts load and the live foreground poll. Without this guard
     * two relay queries can see the same still-unclaimed request and race toward the radio. */
    if(_drainingOutbox) return _drainingOutbox;
    _drainingOutbox = drainOutboxOnce().finally(() => { _drainingOutbox = null; });
    return _drainingOutbox;
  }

  async function drainOutboxOnce(){
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
      let req = null, request = null;
      try{
        request = JSON.parse(await PC.nip44dec(ME().pubkey, ev.content));
        req = await openMessageBody(request);
      }catch(_){ continue; }
      if(!req || req.done) continue;
      if(!req.to || (!req.body && !req.attachment)) continue;
      if(Date.now() - (req.at || 0) > MAX_AGE_MS){
        /* A terminal receipt must retain the request just like a carrier completion does. Without
         * it absorb() cannot derive the message id or find the ask-time placeholder tied to this
         * outbox document, so an expired request remains visibly `sending` on every other device
         * forever. It was deliberately NOT sent; represent that truth as a failed/expired bubble
         * which the sender can inspect or delete, never as a request eligible for automatic retry. */
        await mark(d, { done:true, ok:false, dropped:'too old', error:'request expired before phone was reachable',
                        request:Object.assign({}, request,
                          {at:Number(req.at) || Number(request && request.at) || Date.now()}) });
        continue;
      }
      const P = plug(req.attachment ? 'sendMms' : 'send');
      if(!P) return done;
      let r = null;
      try{
        if(req.attachment){
          const u = await PC.encFileUrl(req.attachment.sha, req.attachment.mime);
          const blob = await fetch(u).then(x=>x.blob());
          r = await P.sendMms({to:req.to, body:req.body||'', data:await fileB64(blob),
                               mime:req.attachment.mime, name:req.attachment.name, outbox:d});
        }else r = await P.send({ to:req.to, body:req.body, outbox:d });
      }catch(_){ r = null; }
      // The background service won the device-local atomic claim. It owns both the radio send and
      // the durable completion marker; publishing another marker here can race its result.
      if(r && r.claimed === false) continue;
      // MARKED BEFORE ANYTHING ELSE, and marked even when the send FAILED. A text that went out and
      // whose marker did not is a text that goes out again on the next drain; there is no undo for
      // that, so a failed send is reported in the marker rather than retried blindly.
      const completedRequest = Object.assign({}, request,
        { at:Number(r && r.sentAt) || Number(request && request.at) || Date.now() });
      await mark(d, { done:true, ok: !!(r && r.ok), error: (r && r.error) || '',
                      request:completedRequest });
      done++;
    }
    if(done) mirror({ limit: 50 });
    return done;
  }

  async function mark(doc, obj){
    try{
      const ct = await PC.nip44enc(ME().pubkey, JSON.stringify(obj));
      const r = await PC.publish(KIND, ct, [['d', doc], ['l', L_TAG]], {quiet:true, noQueue:true});
      return !!(r && r.ok);
    }catch(_){ return false; }
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

    /* One remote send can temporarily have an ask-time placeholder and a receipt-time bubble.
     * Treat the outbox id as their transaction id and remove every local/archive rendering in one
     * action, otherwise the unselected twin remains on the phone. */
    const selectedOutboxes = new Set();
    for(const d of docs){ const m=S.msgs.get(d); if(m && m.outbox) selectedOutboxes.add(m.outbox); }
    if(selectedOutboxes.size){
      for(const [d,m] of S.msgs)
        if(m && selectedOutboxes.has(m.outbox) && !docs.includes(d)) docs.push(d);
    }

    /* A pending laptop bubble has TWO documents: the ordinary message-shaped placeholder and the
     * addressable outbox command the phone will execute. Hiding only the placeholder is not a
     * deletion — the phone can still send it hours later. Replace the command with a terminal
     * cancellation first. A phone that has already claimed the radio operation may still finish;
     * no distributed system can unsend an SMS, but a request not yet claimed is now actually gone. */
    const pendingOutboxes = [];
    for(const d of docs){
      const m = S.msgs.get(d);
      if(m && m.pending && m.outbox && String(m.outbox).startsWith(D_OUT))
        pendingOutboxes.push(String(m.outbox));
    }
    for(const d of [...new Set(pendingOutboxes)]){
      const cancelled = await mark(d,
        { done:true, ok:false, cancelled:true, error:'cancelled by sender' });
      /* Never hide a request we failed to cancel: that would say "deleted" while a phone can still
       * execute it later. Keeping the bubble visible gives the person a truthful retry target. */
      if(!cancelled) return { archive:0, phone:0, refused:true, error:'could not cancel pending send' };
    }

    /* A completed/failed outbox receipt can reconstruct its generated message on every absorb.
     * Once it is no longer live, tombstone that source document as part of deleting the bubble. */
    for(const d of selectedOutboxes){
      if(pendingOutboxes.includes(d)) continue; // keep the explicit cancellation marker durable
      const r = await PC.publish(KIND, '', [['d', d], ['l', L_TAG]], {quiet:true, noQueue:true});
      if(!r || !r.ok) return {archive:0,phone:0,refused:true,error:'could not delete send receipt'};
      try{ await PC.publish(5, '', [['a', KIND+':'+ME().pubkey+':'+d]], {quiet:true,noQueue:true}); }catch(_){}
    }

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
    return { archive, phone, cancelled:pendingOutboxes.length };
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
    const r = await loadFromPhone();
    if(r && r.refused){
      S.emptyWhy = 'This phone allowed the permission, but its message store still would not '
                 + 'answer. Your messages have not been changed.';
      S.emptyFix = '';
    }
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
    const sha = String((p && p.sha) || '');
    if(sha && PC.encFileUrl){
      try{
        const previewSha = isImage(p.ct) && p.thumb ? p.thumb : sha;
        const url = await PC.encFileUrl(previewSha,
          previewSha === sha ? (p.ct || 'application/octet-stream') : 'image/jpeg');
        const blob = await fetch(url).then(r => r.blob());
        return { url, blob, ct: p.ct || blob.type || '', preview: previewSha !== sha };
      }catch(_){
        /* A handset may still have the provider part locally. Desktop/web never does, so they get
         * the truthful encrypted-storage error below; the phone can fall through to its own bytes. */
        if(!id) return { why: attLabel(p) + ' \u00b7 could not be opened from encrypted storage' };
      }
    }
    if(!id) return { why: attLabel(p) + ' \u00b7 on your phone' };
    if(ATT.has(id)) return ATT.get(id);
    const P = plug('attachment');
    if(!P || !P.attachment){
      const r = { why: attLabel(p) + ' \u00b7 this app is too old to open it' };
      attRemember(id, r);
      return r;
    }
    let a = null;
    /* New APKs stream bounded pieces. Besides avoiding three simultaneous 16 MB+ copies across
     * Java/base64/JS, this means one large video no longer blocks the migration high-water mark and
     * every message behind it. Older APKs ignore/omit the chunk fields; fall back to their answer. */
    try{
      const chunks = [];
      let offset = 0, chunked = false;
      for(let guard = 0; guard < 256; guard++){
        const q = await P.attachment({ part:id, offset, max:768 * 1024 });
        if(!q || q.offset === undefined){ a = q; break; }
        chunked = true;
        if(q.error){ a = q; break; }
        if(q.data){
          const bin = atob(q.data), u8 = new Uint8Array(bin.length);
          for(let i=0;i<bin.length;i++) u8[i] = bin.charCodeAt(i);
          chunks.push(u8); offset += u8.length;
        }
        if(q.done){
          const blob = new Blob(chunks, {type:p.ct || 'application/octet-stream'});
          a = { blob, bytes:offset, chunked:true };
          break;
        }
        if(!q.data) { a = q; break; }
      }
      if(chunked && !a) a = { error:'attachment exceeded the safe transfer limit' };
    }catch(_){ a = null; }
    let r;
    if(a && a.blob){
      r = { url: URL.createObjectURL(a.blob), blob:a.blob, ct:p.ct || '' };
    } else if(a && a.data){
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
      img.onclick = async () => {
        let url = d.url;
        /* A remote thread initially holds only the thumbnail. Fetch/decrypt the original on intent,
         * not on render; this is the bandwidth saving the separate preview exists to provide. */
        if(d.preview && p.sha && PC.encFileUrl){
          try{ url = await PC.encFileUrl(p.sha, p.ct || 'image/jpeg'); }
          catch(_){ PC.toast('could not open the full picture'); return; }
        }
        try{ PC.openLightbox(url, 'image'); }catch(_){ }
      };
      el.appendChild(img);
      return;
    }
    if(isVideo(p.ct) || isAudio(p.ct)){
      const v = document.createElement(isVideo(p.ct) ? 'video' : 'audio');
      v.className = 'sms-att-img';
      v.controls = true;
      v.src = d.url;
      el.appendChild(v);
      if(isVideo(p.ct)){
        /* WebView retargets taps inside the native controls' shadow tree to the <video> itself.
         * An onclick on `v` therefore cannot distinguish "play/seek" from "open viewer" and used
         * to replace every attempted control tap with the lightbox. Keep controls native and give
         * the fitted viewer one explicit, thumb-sized action beside them. */
        const full = document.createElement('button');
        full.className = 'sms-att-open';
        full.type = 'button';
        full.textContent = 'Full screen';
        full.setAttribute('aria-label', 'Open video full screen');
        full.onclick = () => { try{ PC.openLightbox(d.url, 'video'); }catch(_){} };
        el.appendChild(full);
      }
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

  function textsOnScreen(){
    if(!PC) return false;
    try{
      if(window.PCOS && PCOS.isOn && PCOS.isOn() && PCOS.ownsFeedView)
        return !!PCOS.ownsFeedView('texts');
    }catch(_){ }
    return PC.VIEW === 'texts';
  }

  function paint(){
    if(!textsOnScreen()) return;
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
        <div class="muted small" id="sms-archive" style="display:none;margin-top:6px"></div>
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
    { const retry = PC.$('#sms-archive-retry'); if(retry) retry.onclick = async () => {
        retry.disabled = true;
        await mirror();
      }; }
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
        S.emptyWhy = 'This phone allowed the permission, but its message store still would not '
                   + 'answer. Your messages have not been changed.';
        S.emptyFix = '';
      }
      paint();
    };
    feed.querySelectorAll('.sms-thread').forEach(b => {
      b.onclick = () => { clearAttachment(); S.open = b.dataset.k; paint(); };
    });
    noteWhere();
  }

  /* WHERE THE MESSAGES COME FROM, said on the screen. "This device is not your phone" and "you have
   * no messages" look identical, and only one of them is a problem. */
  async function noteWhere(){
    const el = PC.$('#sms-note');
    if(!el) return;
    const st = await phoneState();
    const archive = PC.$('#sms-archive');
    if(archive){
      if(S.archive.running){
        archive.style.display = '';
        archive.textContent = 'Encrypting and copying messages to Blossom…';
      }else if(S.archive.error){
        archive.style.display = '';
        archive.innerHTML = 'Message backup stopped: ' + PC.enc(S.archive.error)
          + ' <button class="btn small" id="sms-archive-retry">Retry now</button>';
        const retry = archive.querySelector('#sms-archive-retry');
        if(retry) retry.onclick = async () => { retry.disabled = true; await mirror(); };
      }else if(S.archive.attempted && S.archive.published){
        archive.style.display = '';
        archive.textContent = S.archive.published + ' message'
          + (S.archive.published === 1 ? '' : 's') + ' copied to encrypted Blossom storage';
      }else{
        archive.style.display = 'none';
        archive.textContent = '';
      }
      /* THE RE-SCAN IS OFFERED EVEN WHEN NOTHING LOOKS WRONG, and that is the point of it.
       *
       * The failure it answers -- a completion marker set by an older build -- shows as a full
       * Texts screen, no error, and pictures that never reach the other devices. Attached only to
       * the error branch it would be invisible in exactly the state it exists for. Only on a device
       * that can read this phone's store: a laptop re-scanning somebody else's phone is a button
       * that cannot do anything. */
      if(st.canRead && !S.archive.running){
        archive.style.display = '';
        if(!archive.querySelector('#sms-rescan')){
          const b = document.createElement('button');
          b.className = 'btn small';
          b.id = 'sms-rescan';
          b.textContent = 'Re-scan phone messages';
          b.title = 'Read the whole phone again and copy anything the archive is missing';
          archive.appendChild(document.createTextNode(' '));
          archive.appendChild(b);
        }
        const b = archive.querySelector('#sms-rescan');
        if(b) b.onclick = async () => {
          b.disabled = true;
          b.textContent = 'Re-scanning\u2026';
          try{ await rescan(); }
          finally{ paint(); }
        };
      }
    }
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
    /* AND THE CEILING, SAID OUT LOUD FOR THE SAME REASON. It is a smaller loss than a refusal and
     * an identical silence: the newest picture messages are all present and correct, so nothing on
     * the screen suggests the oldest ones were never asked for. */
    if(S.mmsCapped){
      el.style.display = '';
      el.textContent = 'This phone has more picture messages than PosterChan can read in one pass, '
        + 'so the newest 2,000 are shown and copied. The older ones are still on the phone.';
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

  function scrollState(el){
    return el ? {top:Number(el.scrollTop)||0,
      bottom:el.scrollHeight-el.scrollTop-el.clientHeight<80} : null;
  }

  function putScroll(el, pos){
    if(!el) return;
    el.scrollTop = !pos || pos.bottom ? el.scrollHeight : Math.max(0, Number(pos.top)||0);
  }

  /* RETRY IS AN EXPLICIT NEW SEND, NEVER A GUESS ABOUT A CARRIER CALLBACK.
   *
   * A definite failed receipt keeps enough information to make another attempt, including the
   * encrypted attachment hash.  Ambiguous carrier outcomes deliberately stay excluded: sending
   * those again can duplicate a message that actually reached the recipient.  Queue/send the
   * replacement first and only then retire the failed receipt, so a relay, phone, or attachment
   * read failure cannot turn a visible failed message into a lost one. */
  function ambiguousMmsError(error){const s=String(error||'');return s.startsWith('carrier send status is pending')||s.startsWith('delivery unknown');}
  async function retryFailed(m){
    if(!m || m.incoming || !m.failed ||
       ambiguousMmsError(m.error))
      return {ok:false, error:'this message is not safe to retry'};
    let file = null;
    const parts = m.parts || [];
    if(parts.length){
      const p = parts.find(x => isImage(x.ct)||String(x.ct||'').startsWith('video/'));
      if(!p) return {ok:false, error:'MMS supports photos and videos'};
      const d = await partData(p);
      if(!d || !d.blob)
        return {ok:false, error:(d && d.why) || 'could not read attachment'};
      file = new File([d.blob], p.name || 'photo.jpg',
                      {type:p.ct || d.blob.type || 'image/jpeg'});
    }
    const sent = await send(m.address, m.body || '', file);
    if(!sent || !sent.ok) return sent || {ok:false, error:'could not retry'};
    const gone = await remove([m.doc]);
    if(gone && gone.refused)
      return Object.assign({}, sent, {warning:gone.error ||
        'replacement accepted, but the old failed copy could not be removed'});
    return sent;
  }

  function paintThread(feed, enc){
    /* A focus change, attachment draft, receipt, contact refresh, or relay event can repaint the
       whole thread. Capture the OLD element before replacing it. Its data key is authoritative:
       the room-list click changes S.open before this function runs, while the DOM still belongs to
       the conversation being left. */
    const oldList = feed.querySelector && feed.querySelector('.sms-msgs');
    if(oldList){
      const oldKey = oldList.dataset.threadKey || '';
      if(oldKey) S.scroll[oldKey] = scrollState(oldList);
    }
    const t = S.threads.find(x => x.key === S.open);
    if(!t){ S.open = ''; return paint(); }
    const who = whoIs((t.msgs[t.msgs.length-1] || {}).name, t.address);
    feed.innerHTML = `
      <div class="sms-wrap">
        <div class="sms-head">
          <button class="btn small" id="sms-back">${ICO('arrow-left','b-ic')}</button>
          <div class="sms-title">${enc(who)}</div>
        </div>
        ${blankNote(blankCount(t.msgs))}
        <div class="sms-msgs dm-msgs" id="sms-msgs" data-thread-key="${enc(t.key)}">${t.msgs.map((m, i) => {
          /* THE SAME BUBBLE AS A DM, not a second one that looks nearly like it.
           *
           * Texts had its own parallel set of classes -- sms-msg/sms-bub/sms-meta -- built to the
           * same idea and drifting from it: different padding, no grouping, no run of messages
           * collapsing into one column. Two implementations of one thing means every future change
           * to a conversation has to be made twice, and the one nobody remembers is the one that
           * looks wrong. Reported as "Texts UI looks ugly and should look like DM's".
           *
           * `.bubble .in/.out` and `.grp`/`.cont` are the DM's own, so Texts inherits its shape,
           * its spacing and any later change to either for free. What stays sms-specific is the
           * part DMs do not have: MMS attachments inside the bubble. */
          const prev = t.msgs[i-1];
          const grp = !prev || !!prev.incoming !== !!m.incoming ? ' grp' : ' cont';
          const atts = (m.parts||[]).map((p, j) => attHtml(p, enc, i, j)).join('');
          const retryable = !m.incoming && m.failed &&
            !ambiguousMmsError(m.error);
          return `<div class="bubble ${m.incoming ? 'in' : 'out'}${grp}${atts ? ' has-att' : ''}" data-doc="${enc(m.doc)}">`
            + atts
            /* THE ATTACHMENTS COME FIRST AND THE CAPTION UNDER THEM, which is where every messages
               app puts it -- and a bubble whose only content is an attachment must not also render
               an empty text node, or it collapses to a sliver. */
            + (m.body ? `<span class="b-txt">${enc(m.body)}</span>` : '')
            + `<span class="b-meta">${enc(when(m.date))}${ambiguousMmsError(m.error)?' · carrier status pending':m.pending?' · sending':m.failed?' · not sent':''}</span>`
            + (retryable ? `<button class="btn small sms-retry" data-sms-retry="${enc(m.doc)}">Retry</button>` : '')
            + `</div>`;
        }).join('')}</div>
        ${S.attach?`<div class="sms-attachment-draft"><span>${ICO(String(S.attach.type||'').startsWith('video/')?'film':'image','b-ic')}<b>${enc(S.attach.name||'Attachment')}</b><small>${enc(fmtBytes(S.attach.size))} · ready to send as MMS</small></span><button id="sms-attach-clear" aria-label="Remove attachment">×</button></div>`:''}
        <div class="sms-compose">
          <button class="btn small" id="sms-attach" title="Add photo or video">${ICO('paperclip','b-ic')}</button>
          <input id="sms-file" type="file" accept="image/*,video/*" hidden>
          <input id="sms-camera" type="file" accept="image/*" capture="environment" hidden>
          <button class="btn small" id="sms-emoji" title="Add emoji" aria-label="Add emoji">${ICO('smile','b-ic')}</button>
          ${(PC.gifEnabled && PC.gifEnabled())?`<button class="btn small" id="sms-gif" title="Add GIF" aria-label="Add GIF">${ICO('film','b-ic')}</button>`:''}
          <input class="input" id="sms-in" placeholder="Text message">
          <button class="btn btn-neon" id="sms-send">${ICO('send','b-ic')}Send</button>
        </div>
      </div>`;
    PC.$('#sms-back').onclick = () => { clearAttachment(); S.open = ''; paint(); };
    const input = PC.$('#sms-in'), btn = PC.$('#sms-send');
    const emojiBtn = PC.$('#sms-emoji');
    emojiBtn.onclick = () => PC.openEmojiPopover(emojiBtn, (emoji, close) => {
      const start = Number.isInteger(input.selectionStart) ? input.selectionStart : input.value.length;
      const end = Number.isInteger(input.selectionEnd) ? input.selectionEnd : start;
      input.setRangeText(emoji, start, end, 'end');
      input.dispatchEvent(new Event('input', {bubbles:true}));
      close(); input.focus();
    }, {unicodeOnly:true});
    const gifBtn = PC.$('#sms-gif');
    if(gifBtn) gifBtn.onclick = () => { if(PC.gifPicker) PC.gifPicker(input); };
    const pick = PC.$('#sms-file'), camera = PC.$('#sms-camera'), attachBtn = PC.$('#sms-attach');
    const acceptFile = file => {
      if(file&&!isMmsFile(file)){PC.toast('MMS supports photos and videos');return false;}
      if(file){ S.attach=file; paint(); }
      return !!file;
    };
    const fromBlossom = () => PC.blossomPicker(null, async ({url,type,ext,name}) => {
      try{
        if(!/^(?:image|video)\//.test(String(type||''))) throw new Error('MMS supports photos and videos');
        const res=await fetch(url); if(!res.ok)throw new Error('Blossom returned '+res.status);
        const blob=await res.blob();
        const pickedName=name||((String(url).split(/[?#]/)[0].split('/').pop()||'file')
                   +(ext&&!String(url).split(/[?#]/)[0].includes('.')?'.'+ext:''));
        acceptFile(new File([blob],pickedName,{type:type||blob.type||'application/octet-stream'}));
      }catch(e){ PC.toast('could not attach Blossom media: '+String(e&&e.message||e)); }
    }, {title:'📁 Attach photo or video from Files',filter:b=>/^(?:image|video)\//.test(String(b.type||''))});
    if(blossomLaunch){blossomLaunch=false;setTimeout(fromBlossom,0);}
    const fromDevice = async () => {
      /* Electron's hidden file input is not reliable when the Texts window has just changed focus
       * between compositor surfaces. Use the desktop's native, explicitly user-confirmed picker;
       * browsers and Android keep the ordinary input. Bytes are bounded in the main process before
       * crossing IPC, then restored as a real File so every existing MMS/upload path stays shared. */
      if(window.pcHost && pcHost.pickFile){
        try{
          const chosen=await pcHost.pickFile({accept:['image/*','video/*'],max:32*1024*1024,title:'Add a photo or video to Texts'});
          if(!chosen)return false;
          const file=new File([chosen.data],chosen.name,{type:chosen.type});
          return acceptFile(file);
        }catch(e){ PC.toast('could not attach media: '+String(e&&e.message||e)); }
        return false;
      }
      pick.click();
      return true;
    };
    attachBtn.onclick = async () => {
      /* Do not let the native desktop picker bypass this choice. A signed-in desktop is also an
       * account-scoped Files client, and hiding that source made web Texts offer Blossom while the
       * installed app only offered the local disk. `Device` still uses the reliable host dialog. */
      if(PC.blossomPicker && PC.modal){
        PC.modal('<h3>Add a photo or video</h3><div class="sms-attach-sources"><button class="btn" id="sms-src-camera">Camera photo</button><button class="btn" id="sms-src-device">Device</button><button class="btn" id="sms-src-blossom">📁 Files</button></div>', root=>{
          root.querySelector('#sms-src-camera').onclick=()=>{PC.closeModal();camera.click();};
          root.querySelector('#sms-src-device').onclick=()=>{PC.closeModal();fromDevice();};
          root.querySelector('#sms-src-blossom').onclick=()=>{PC.closeModal();fromBlossom();};
        });
      }else await fromDevice();
    };
    pick.onchange = () => {
      const file=(pick.files||[])[0]||null;
      acceptFile(file); pick.value='';
    };
    camera.onchange=()=>{acceptFile((camera.files||[])[0]||null);camera.value='';};
    const clear=PC.$('#sms-attach-clear');if(clear)clear.onclick=()=>{clearAttachment();paint();};
    /* THE BUTTON'S DISABLED STATE DOES NOT GUARD THE KEYBOARD. Two Enter keydowns can arrive while
       the first radio/upload promise is pending; programmatic clicks can do the same. Without a
       function-level latch both calls pass the empty-body check and put the same message on the
       carrier twice. Keep the latch around the whole attempt, including failures, and release it
       in `finally` so a rejected plugin call cannot permanently disable this conversation. */
    let sending = false;
    const go = async () => {
      if(sending) return;
      const body = input.value.trim();
      if(!body && !S.attach) return;
      const attachment=S.attach;
      sending = true;
      btn.disabled = true;
      try{
        const r = await send(t.address, body, attachment);
        if(!r.ok){ PC.toast(r.error || 'could not send'); return; }
        input.value = '';
        if(S.attach===attachment)clearAttachment();
        /* `link` is already a successful local SMS send: the media was encrypted into Files and
         * its private link crossed this phone's radio. Calling that "waiting for your phone" made a
         * completed oversize send look stuck. Only queued/queued-link are genuinely waiting on a
         * different device. */
        PC.toast(r.where === 'link' ? 'sent as a private Files link'
                 : r.where === 'phone' ? 'sent'
                 : 'waiting for your phone to send it');
        paint();
      }finally{
        sending = false;
        btn.disabled = false;
      }
    };
    btn.onclick = go;
    input.onkeydown = e => { if(e.key === 'Enter'){ e.preventDefault(); go(); } };
    feed.querySelectorAll('[data-sms-retry]').forEach(retry => {
      retry.onclick = async e => {
        e.stopPropagation();
        if(retry.disabled) return;
        retry.disabled = true;
        const r = await retryFailed(S.msgs.get(retry.dataset.smsRetry));
        if(!r || !r.ok){
          PC.toast((r && r.error) || 'could not retry');
          retry.disabled = false;
          return;
        }
        PC.toast(r.warning || (r.where === 'phone' ? 'retry accepted' :
          'retry queued for your phone'));
        paint();
      };
    });
    /* `.bubble[data-doc]`, because the bubble IS a DM bubble now and `data-doc` is what makes it a
     * text rather than a DM. Selected on the class it actually has: this was `.sms-msg`, and moving
     * the markup to the shared bubble would have matched nothing -- so right-clicking a message
     * would silently stop offering to delete it, on a screen that still drew perfectly. */
    feed.querySelectorAll('.bubble[data-doc]').forEach(el => {
      let hold=0, startX=0, startY=0;
      const stopHold=()=>{if(hold){clearTimeout(hold);hold=0;}};
      const removeMessage=async () => {
        stopHold();
        if(!await PC.uiConfirm('Delete this message from your archive' +
             (await isPhone() ? ' and from this phone' : '') + '?')) return;
        const r = await remove([el.dataset.doc]);
        // SAY WHICH COPIES WENT, and never promise the ones this device cannot reach. Other phones
        // and laptops drop theirs when the tombstone reaches them.
        if(r.refused) PC.toast('this phone would not delete it — nothing was changed');
        else PC.toast(r.phone ? 'deleted here and from your archive' : 'deleted from your archive');
        paint();
      };
      el.oncontextmenu = e => {
        if(e.target.closest('button,a,input')) return;
        e.preventDefault(); removeMessage();
      };
      /* A HOLD IS THE MOBILE MESSAGE MENU. Pointer events cover touch and pen without also
         installing touch handlers that fire twice on Android. Cancel as soon as the finger moves:
         scrolling a conversation must never become a destructive gesture. */
      el.onpointerdown=e=>{
        if(e.target.closest('button,a,input'))return;
        if(e.pointerType==='mouse')return;
        startX=e.clientX;startY=e.clientY;stopHold();
        hold=setTimeout(()=>{hold=0;removeMessage();},550);
      };
      el.onpointermove=e=>{
        if(hold&&(Math.abs(e.clientX-startX)>10||Math.abs(e.clientY-startY)>10))stopHold();
      };
      el.onpointerup=stopHold;
      el.onpointercancel=stopHold;
      el.onpointerleave=stopHold;
    });
    const list = feed.querySelector('.sms-msgs');
    const saved = S.scroll[t.key];
    putScroll(list, saved);
    if(list) list.onscroll = () => {
      /* Reparenting during desktop window parking fires synthetic scroll events. The OS restores
         the exact offset itself; do not replace that saved intent with the transient zero. */
      if(list.dataset.osParking === '1') return;
      S.scroll[t.key] = scrollState(list);
    };
    /* THE PICTURES ARRIVE AFTER THE DRAW, and each one that lands pushes everything below it
       down. A thread opens at its newest message (the line above), so re-pinning as they land is
       what keeps it there instead of drifting backwards through the conversation as the photos
       above resolve. Guarded on there BEING attachments, so an ordinary text thread does no work. */
    if(t.msgs.some(m => (m.parts || []).length)){
      const before = list ? { top:list.scrollTop, height:list.scrollHeight,
        bottom:list.scrollHeight-list.scrollTop-list.clientHeight<80 } : null;
      hydrateAtt(feed, t.msgs).then(() => {
        const l = feed.querySelector('.sms-msgs');
        /* Hydration belongs to THIS rendered element, not merely this thread key. A focus sync can
           repaint the same conversation while an encrypted photo is loading; a user can also open
           another conversation. In both cases the old promise used to apply its height delta to the
           new DOM and throw that reader toward the middle/bottom. The replacement paint owns its
           own restoration/hydration pass, so stale work must not touch it. */
        if(!l || l !== list || !before) return;
        /* Media resolving above the viewport must not move the message being read. Compensate by
           the height gained; a bottom-pinned reader remains bottom-pinned. */
        l.scrollTop = before.bottom ? l.scrollHeight
          : Math.max(0, before.top + (l.scrollHeight - before.height));
        S.scroll[t.key] = {top:l.scrollTop,
          bottom:l.scrollHeight-l.scrollTop-l.clientHeight<80};
      }, () => {});
    }
  }

  async function composeNew(){
    const to = await PC.uiPrompt('Phone number');
    if(!to) return;
    clearAttachment(); S.open = key(to);
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
    watch();
    /* Do not paint a convincing empty inbox while the encrypted cache is still being opened. The
     * route already installed a spinner; awaiting the first cache pass makes the first visit behave
     * exactly like the second one instead of requiring the person to close and reopen Texts. */
    if(!S.ready) await load();
    else paint();
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
      const r = await loadFromPhone();
      if(r && r.refused && !S.msgs.size){
        S.emptyWhy = 'This phone allowed the permission, but its message store still would not '
                   + 'answer. Your messages have not been changed.';
        S.emptyFix = '';
      }
      paint();
      /* Only now do we possess the phone's COMPLETE provider read. The old call to mirror()
       * below can publish recent arrivals, but its forward-only query cannot migrate older
       * history. Walk the local set in resumable encrypted-drive batches. Awaiting also prevents
       * the recent sweep from uploading the same rows alongside this one. */
      await migrateLocalHistory();
    }
    /* PUBLISHING needs to READ; performing a send another device asked for needs a RADIO. Two
     * jobs, two gates -- and this one used to name the role for the second, which contradicted the
     * function it calls.
     *
     * `drainOutbox` was corrected to gate on telephony, with its own comment explaining why: sending
     * needs SEND_SMS, and the role only decides whether messages arrive and whether the phone's own
     * store may be written. The CALL SITES were left asking for the role, so on a handset that had
     * not been made the default the drain was never reached at all -- the laptop's request sat
     * unperformed on a phone perfectly able to send it, and the laptop said "waiting for your phone"
     * for ever, which is true and useless. The fix inside a function is not a fix while the only
     * thing that calls it disagrees. */
    if(st.canRead) await mirror();
    if(st.telephony) drainOutbox();
  }

  function init(){
    PC = window.__PC;
    if(!PC){ return setTimeout(init, 50); }
    /* The handset publishes and drains WITHOUT the screen being open — that is the whole point of an
     * archive. Behind `load` so it never runs before the client has a key, and on visibility rather
     * than a timer: a poll here would run for the life of the battery on a device that already holds
     * the HOME role. */
    /* DESKTOP FOREGROUND IS NOT A PHONE FOREGROUND. The old handler returned immediately when the
     * device had neither READ_SMS nor a radio, which is every web/PosterChanOS session. A live
     * subscription interrupted by sleep, relay reconnect, or a window handoff therefore stayed
     * stale until Texts was closed and reopened. Always run the encrypted-archive catch-up query;
     * only the provider migration and outbox drain are handset-only.
     *
     * Chromium can emit both visibilitychange and focus for one return. Coalesce them so one click
     * does not decrypt the archive twice or race two provider migrations. */
    let foregrounding = null;
    async function foreground(){
      if(document.visibilityState !== 'visible') return;
      if(foregrounding) return foregrounding;
      foregrounding = (async () => {
      await load();
      await refresh();
      const st = await phoneState();
      /* A foreground is also the retry boundary for the COMPLETE archive, not merely its recent
       * high-water sweep. A phone can be suspended, lose its signer, or go offline half-way through
       * migrating years of messages. The old path only called mirror(), whose timestamp cursor
       * walks forward and can never discover that older tail again. Re-read the provider, then let
       * the resumable full migration drain before the recent sweep. migrateLocalHistory coalesces
       * concurrent callers, and its completion latch makes this cheap after convergence. */
      if(st.canRead){
        await loadFromPhone();
        /* The set prevents one unreadable attachment from spinning the current migration loop.
         * It must not become a session-long blacklist: a relay/signing/upload failure is often
         * transient, and the documented retry boundary is this foreground. */
        _migrationFailed.clear();
        await migrateLocalHistory();
        await mirror();
      }
      if(st.telephony) drainOutbox();
      })().finally(() => { foregrounding = null; });
      return foregrounding;
    }
    document.addEventListener('visibilitychange', foreground);
    if(window.addEventListener) window.addEventListener('focus', foreground);
    /* A request can be published while Texts is already open. Previously there was no relay
     * subscription or subsequent query in that state: the handset checked at load/foreground and
     * then remained deaf until the user backgrounded it. Poll only while visible, and only on a
     * device with telephony; this costs nothing on web/desktop and gives a newly queued MMS a
     * bounded pickup time. drainOutbox() coalesces slow queries and sends. */
    const outboxPoll = setInterval(async () => {
      if(document.visibilityState !== 'visible') return;
      const st = await phoneState();
      if(st.telephony) drainOutbox();
    }, 3000);
    /* Node's browser simulator returns a ref-counted Timeout; browsers return a number. Do not let
       the production poll turn a completed regression suite into a process that never exits. */
    if(outboxPoll && typeof outboxPoll.unref === 'function') outboxPoll.unref();
  }
  init();

  window.PCSms = { render, mirror, importAll, loadFromPhone, emptyWhy, ensureRead, phoneState,
                   openBlossom: address => { const to=String(address||'').trim();if(!to)return;clearAttachment();S.open=key(to);if(!S.threads.some(t=>t.key===S.open))S.threads.unshift({key:S.open,address:to,msgs:[],date:0,unread:0});blossomLaunch=true;paint(); },
                   // Clear the archive's latches and walk the whole phone again -- see rescan().
                   rescan, resetArchiveMarkers,
                   // The oversized-attachment fallback — see sendAsLink.
                   mmsLimit, shareLinkFor,
                   drainOutbox, send, remove, load,
                   _absorb: absorb,
                   // Explicit failed-send retry is exported for the protocol simulator.
                   _retryFailed: retryFailed,
                   // The real batched migration loop, for tests/client/test_sms_attachments.py —
                   // its convergence is the property that matters and it is invisible from a single
                   // mirror() call.
                   migrateAll: migrateLocalHistory,
                   // Contacts can finish after Texts has already painted on a desktop. Rebuild the
                   // thread labels from the same messages; no relay or phone read is necessary.
                   refreshNames: () => { rebuild(); if(textsOnScreen()) paint(); },
                   _state: () => S, _key: key, _outboxId: outboxId, _docId: docIdFor,
                   // Pure scroll primitives let the window-lifecycle suite exercise actual state
                   // transitions without replacing this implementation with a test-only copy.
                   _scrollState: scrollState, _putScroll: putScroll,
                   // The attachment identity rules, for tests/test_android_mms.py — which runs them
                   // against SmsKeys.partKey/partsKey in Java, because a picture message filed at
                   // two addresses appears twice in the thread.
                   _partKey: partKey, _partsKey: partsKeyOf };
})();
