/* #calendar — the Calendar screen, on top of this node's bundled CalDAV server.
 *
 * Kept OUT of app.js (own file, like News/Notes/Web Search) and driven from renderView via
 * window.PCCalendar.render(). Everything server-side is /api/calendar/*, which reads and writes the
 * SAME encrypted Nostr events Radicale's storage plugin does — so this screen and a synced phone are
 * looking at one calendar rather than two that drift.
 *
 * Shape, and why:
 *
 *   The month lives in MODULE state, not the DOM. #feed is one element every view shares and app.js
 *   blanks it on entry, so a view that keeps its data in the page loses it the moment you glance at
 *   Messages. Coming back repaints the same month, the same selection, the same calendars — from `S`.
 *
 *   ICS is generated HERE and stored verbatim. The server never rewrites an event, so what a phone
 *   sees is what this screen wrote, and an export is what every other calendar program reads. That
 *   also means we must emit correct iCalendar rather than something that merely round-trips through
 *   our own parser: all-day is VALUE=DATE, timed is UTC with a Z, and every text field is escaped.
 */
(function(){
  const DAYS = ['Mon','Tue','Wed','Thu','Fri','Sat','Sun'];
  const MONTHS = ['January','February','March','April','May','June','July','August','September',
                  'October','November','December'];
  const PALETTE = ['#3ce8ff','#ff5cf0','#00ff88','#ffcf2b','#ff5a7a','#9b8cff','#4ade80','#fb923c'];

  const S = {
    ready:false, enabled:null,
    cals:[], cal:'',           // calendars, and the one being edited into
    items:{},                  // calendarId -> [{uid, ics, …}]
    rev:0,                     // bumped on every load; the occurrence index keys off it
    month:null,                // Date, first of the shown month
    sel:null,                  // 'YYYY-MM-DD' selected day
    loading:false, error:'',
    sync:null,                 // /config payload for the phone panel
    scroll:0,
    owner:'', loadGen:0,
  };

  function init(){
    const PC = window.__PC;
    if(!PC){ return setTimeout(init, 50); }
    const { $, $$, enc, toast, modal, closeModal, authFetch, ensureAiSession, uiConfirm } = PC;

    const inView = () => window.__PC.VIEW === 'calendar';
    const owner = () => { try{ return ((PC.me && PC.me()) || PC.ME || {}).pubkey || ''; }
                         catch(_){ return ''; } };
    const scroller = () => document.body.classList.contains('embed')
      ? (document.scrollingElement || document.documentElement) : $('#feed');

    // ---- server ------------------------------------------------------------------------------
    async function api(path, opts){
      try{ await ensureAiSession(); }catch(_){}
      const r = await authFetch(path, opts);
      let body = null;
      try{ body = await r.json(); }catch(_){}
      if(!r.ok){
        /* A STRUCTURED `detail` HAS TO SURVIVE. FastAPI's detail is usually a string, but it can be
         * an object — the calendar subscription answers `{error, certificate:true}` so the client can
         * offer "subscribe anyway" instead of dead-ending. `new Error(anObject)` stringifies it to
         * "[object Object]" and drops the flag entirely, which is a branch that could never run. */
        const d = body && body.detail;
        const obj = d && typeof d === 'object' && !Array.isArray(d);
        const e = new Error((obj ? (d.error || d.detail) : d) || (body && body.error)
                            || ('HTTP ' + r.status));
        if(obj) e.detail = d;
        e.status = r.status;
        throw e;
      }
      return body || {};
    }
    /* A SUBSCRIBED calendar mirrors somebody else's published .ics (see caldav_subscribe.py). Up
     * here because both the editor's guard and the manager's rows ask. */
    const subOf = (c) => (c && c.subscribe && c.subscribe.url) ? c.subscribe : null;
    const jpost = (p, o) => api(p, { method:'POST', headers:{'Content-Type':'application/json'},
                                     body: JSON.stringify(o||{}) });
    const jput = (p, o) => api(p, { method:'PUT', headers:{'Content-Type':'application/json'},
                                    body: JSON.stringify(o||{}) });

    // ---- dates -------------------------------------------------------------------------------
    const pad = n => String(n).padStart(2, '0');
    const ymd = d => `${d.getFullYear()}-${pad(d.getMonth()+1)}-${pad(d.getDate())}`;
    const firstOf = d => new Date(d.getFullYear(), d.getMonth(), 1);
    const addMonths = (d, n) => new Date(d.getFullYear(), d.getMonth()+n, 1);
    const todayKey = () => ymd(new Date());
    // Monday-first, which is what the rest of the world (and every calendar app here) uses.
    const gridStart = m => { const d = firstOf(m); const back = (d.getDay() + 6) % 7;
                             return new Date(d.getFullYear(), d.getMonth(), 1 - back); };

    /* iCalendar timestamps. A timed event is written in UTC (…Z) rather than with a VTIMEZONE block:
     * an absolute instant needs no timezone table, every client renders it in local time, and a
     * wrong hand-rolled VTIMEZONE is how an appointment lands an hour out twice a year. An ALL-DAY
     * event is the opposite — it is a date, not an instant, so it must be VALUE=DATE or a phone in
     * another timezone shows it on the wrong day. */
    const icsUtc = d => `${d.getUTCFullYear()}${pad(d.getUTCMonth()+1)}${pad(d.getUTCDate())}T`
                      + `${pad(d.getUTCHours())}${pad(d.getUTCMinutes())}00Z`;
    const icsDate = d => `${d.getFullYear()}${pad(d.getMonth()+1)}${pad(d.getDate())}`;
    // RFC 5545 §3.3.11: backslash, semicolon and comma are escaped; a newline becomes \n.
    const icsText = s => String(s||'').replace(/\\/g,'\\\\').replace(/;/g,'\\;')
                                      .replace(/,/g,'\\,').replace(/\r?\n/g,'\\n');

    function buildIcs(ev){
      const uid = ev.uid || (crypto.randomUUID ? crypto.randomUUID() : String(Date.now()) + '-pc');
      const now = icsUtc(new Date());
      const raw = ev.raw || { keep: [], overrides: [], timezones: [], component: 'VEVENT' };
      // A VTODO stays a VTODO. Rewriting one as a VEVENT turns a task into an appointment on every
      // synced device — 10 of one real imported calendar's items are todos.
      const comp = (raw.component === 'VTODO') ? 'VTODO' : 'VEVENT';
      const endProp = (comp === 'VTODO') ? 'DUE' : 'DTEND';
      const L = ['BEGIN:VCALENDAR','VERSION:2.0','PRODID:-//PosterChan//Calendar//EN'];
      // Timezone tables the untouched parts still refer to (an edited occurrence keeps its TZID).
      for(const tz of (raw.timezones || [])) L.push(...tz.split('\n'));
      L.push('BEGIN:' + comp, 'UID:' + uid, 'DTSTAMP:' + now);
      if(ev.allDay){
        const s = new Date(ev.date + 'T00:00:00');
        const e = new Date(s.getTime() + 86400000);          // DTEND is exclusive for a date value
        L.push('DTSTART;VALUE=DATE:' + icsDate(s), endProp + ';VALUE=DATE:' + icsDate(e));
      }else{
        const s = new Date(`${ev.date}T${ev.start || '09:00'}:00`);
        const e = new Date(`${ev.date}T${ev.end || ev.start || '10:00'}:00`);
        L.push('DTSTART:' + icsUtc(s), endProp + ':' + icsUtc(e.getTime() > s.getTime() ? e
                                                              : new Date(s.getTime() + 3600000)));
      }
      L.push('SUMMARY:' + icsText(ev.title || '(no title)'));
      if(ev.location) L.push('LOCATION:' + icsText(ev.location));
      if(ev.notes) L.push('DESCRIPTION:' + icsText(ev.notes));
      // Everything this form does not manage — the repeat rule and its exceptions, VALARM
      // reminders, ATTENDEE/ORGANIZER, STATUS, CATEGORIES, URL, X- properties — verbatim. Dropping
      // the rule alone turned "fix a typo in the title of a weekly delivery" into "delete every
      // future delivery", and dropping the rest silently deleted people's reminders.
      for(const line of (raw.keep || [])) L.push(line);
      L.push('END:' + comp);
      // Occurrences edited individually are their own components under the same UID; they are not
      // reachable from this form, so they travel through untouched.
      for(const o of (raw.overrides || [])) L.push(...o.split('\n'));
      L.push('END:VCALENDAR');
      return { uid, ics: L.join('\r\n') + '\r\n' };
    }

    /* Reading iCalendar is PCIcal's job (client/ical.js) — it is DOM-free so recurrence can be
     * tested against real rules under node instead of eyeballed in a month grid. This file only
     * decides how an occurrence is drawn. */
    const decorate = o => Object.assign({}, o, {
      time: (o.start && !o.allDay) ? `${pad(o.start.getHours())}:${pad(o.start.getMinutes())}` : '',
      todo: String(o.component || 'VEVENT').toUpperCase() === 'VTODO',
    });

    /* One stored item → the fields the editor shows, taken from the series MASTER.
     *
     * `raw` carries the parts this screen has no UI for — the repeat rule, its exceptions, and any
     * individually-edited occurrences. Saving re-emits them verbatim: rebuilding an event from the
     * form alone would silently flatten a weekly series into one appointment the first time someone
     * fixed a typo in its title. */
    function parseItem(rec){
      const I = window.PCIcal;
      const res = I.parseResource(rec);
      const m = res.master || {};
      return {
        uid: rec.uid, cal: rec.cal,
        title: m.title || '(no title)', location: m.location || '', notes: m.notes || '',
        allDay: !!m.allDay, start: m.start || null,
        key: m.start ? ymd(m.start) : '',
        time: (m.start && !m.allDay) ? `${pad(m.start.getHours())}:${pad(m.start.getMinutes())}` : '',
        todo: String(rec.component || res.component || 'VEVENT').toUpperCase() === 'VTODO',
        repeats: !!(m.rrule && m.rrule.freq),
        raw: rawSeries(rec.ics || ''),
      };
    }

    /* EVERYTHING in the stored master this form has no field for, kept verbatim — plus any
     * RECURRENCE-ID components and the timezone tables.
     *
     * This is the rule vcard.js follows for contacts, and it belongs here just as much. The editor
     * has fields for eight properties; a real event carries far more. An earlier version kept only
     * the repeat rule, so saving a change of title silently deleted the VALARM reminders (200 of one
     * real 707-event calendar had one), the ATTENDEE and ORGANIZER lines, STATUS, CATEGORIES, URL,
     * every X- property a phone had written — and rewrote a VTODO as a VEVENT. The event still
     * looked right on this screen and lost half of itself everywhere else.
     *
     * MANAGED is the small set the form rewrites; a nested VALARM is copied through as a whole
     * block, since its lines are not the event's own.
     */
    const MANAGED = ['UID','DTSTAMP','DTSTART','DTEND','DUE','DURATION','SUMMARY','LOCATION',
                     'DESCRIPTION','LAST-MODIFIED'];
    function rawSeries(ics){
      const I = window.PCIcal;
      const all = I.splitComponents(ics);
      const comps = all.filter(c => I.nameOf(c) !== 'VTIMEZONE');
      const overrides = comps.filter(c => /^RECURRENCE-ID[;:]/m.test(I.unfold(c)));
      const master = comps.find(c => overrides.indexOf(c) < 0);
      const keep = [];
      let component = 'VEVENT';
      if(master){
        component = I.nameOf(master);
        let depth = 0;
        for(const raw of I.unfold(master).split('\n')){
          const line = raw.trim();
          if(!line) continue;
          if(/^BEGIN:V/.test(line) && line !== 'BEGIN:' + component){ depth++; keep.push(line); continue; }
          if(depth){                                  // inside a VALARM (or anything else nested)
            keep.push(line);
            if(/^END:V/.test(line)) depth--;
            continue;
          }
          if(line === 'BEGIN:' + component || line === 'END:' + component) continue;
          const name = line.split(/[;:]/)[0].toUpperCase();
          if(MANAGED.indexOf(name) < 0) keep.push(line);
        }
      }
      return { keep, overrides, component,
               timezones: all.filter(c => I.nameOf(c) === 'VTIMEZONE') };
    }

    /* The occurrences of every calendar, for the 42 days the grid shows, keyed by day.
     *
     * Built ONCE per (month, load) rather than per cell: the old code re-parsed every stored item
     * inside all 42 cells, which on a 700-event calendar is ~30,000 parses per repaint. Recurrence
     * expansion on top of that would have made every month change visibly stutter.
     */
    /* ---- The NOSTR layer: NIP-52 public calendar events (kinds 31922 date / 31923 time) ------
     *
     * The community's events, read straight off the relay pool the way Git reads repos — the WoT
     * relay IS the discovery filter, so no author list is needed. A MIRROR like a subscribed .ics:
     * drawn, openable, never written to CalDAV and never editable here. Latest replaceable wins per
     * (kind, author, d). Off/on with one switch, remembered per device. */
    const NOSTR_CAL = '__nostr';
    const nostrOn = () => { try{ return localStorage.getItem('pc_cal_nostr') !== '0'; }catch(_){ return true; } };
    const setNostrOn = (on) => { try{ localStorage.setItem('pc_cal_nostr', on ? '1' : '0'); }catch(_){}
                                 if(on && _n52evs === null){ _n52evs = []; loadNostr(); }
                                 S.rev++; paint(); };
    let _n52evs = null;          // parsed events, or null = never loaded
    let _n52at = 0;              // when they were fetched — the network moves while a tab stays open
    function _n52parse(ev){
      const tag = (n) => (((ev.tags || []).find(t => t[0] === n) || [])[1] || '');
      const d = tag('d'); if(!d) return null;
      const title = tag('title') || tag('name') || '(untitled event)';
      let start = null, end = null, allDay = false;
      if(ev.kind === 31922){
        allDay = true;
        const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(tag('start')); if(!m) return null;
        start = new Date(+m[1], +m[2] - 1, +m[3]);
        const me2 = /^(\d{4})-(\d{2})-(\d{2})$/.exec(tag('end'));
        end = me2 ? new Date(+me2[1], +me2[2] - 1, +me2[3]) : null;   // NIP-52: end is EXCLUSIVE
      }else{
        const t = parseInt(tag('start'), 10); if(!isFinite(t) || t <= 0) return null;
        start = new Date(t * 1000);
        const te = parseInt(tag('end'), 10);
        end = (isFinite(te) && te > t) ? new Date(te * 1000) : null;
      }
      return { uid: ev.kind + ':' + ev.pubkey + ':' + d, pk: ev.pubkey, kind: ev.kind,
               created_at: ev.created_at || 0, title: String(title).slice(0, 200),
               location: tag('location').slice(0, 200), notes: String(ev.content || '').slice(0, 4000),
               allDay, start, end };
    }
    async function loadNostr(){
      if(typeof Relay === 'undefined' || !Relay.query) return;
      let evs = [];
      try{ if(Relay.ready) await Relay.ready(6000); }catch(_){}
      /* YOUR OWN EVENTS ARE ASKED FOR BY NAME, never left to the firehose. A bare
       * {kinds, limit:500} answers with the 500 NEWEST calendar events on the relay — so the
       * moment the network carries more than 500, the user's own appointments (and their
       * follows') are exactly what gets crowded out: "I see nostr events in the calendar, but
       * not mine". An authors filter has its own answer budget, so their events always land;
       * the merge below dedups whatever both filters return. */
      let mine = [];
      try{
        const me = (PC.me && PC.me() || {}).pubkey || null;
        if(me){
          let follows = [];
          try{
            const l = await Relay.query([{ authors: [me], kinds: [3], limit: 1 }], 6000);
            const c = (l || []).sort((a, b) => b.created_at - a.created_at)[0];
            follows = ((c && c.tags) || []).filter(t => t[0] === 'p' && t[1]).map(t => t[1]).slice(0, 900);
          }catch(_){}
          mine = await Relay.query([{ kinds: [31922, 31923], authors: [me, ...follows], limit: 500 }], 8000) || [];
        }
      }catch(_){ mine = []; }
      try{ evs = await Relay.query([{ kinds: [31922, 31923], limit: 500 }], 8000) || []; }
      catch(_){ evs = []; }
      evs = mine.concat(evs);
      const best = {};                       // replaceable: latest per (kind, author, d)
      for(const ev of evs){
        const p2 = _n52parse(ev); if(!p2) continue;
        if(!best[p2.uid] || best[p2.uid].created_at < p2.created_at) best[p2.uid] = p2;
      }
      _n52evs = Object.values(best);
      _n52at = Date.now();
      S.rev++;
      if(inView()) paint();
    }
    /** The layer's occurrences inside the 42-day window, in the shape the grid already draws. */
    function _n52occ(start, end){
      const out = [];
      for(const e of (_n52evs || [])){
        if(!e.start) continue;
        if(e.allDay){
          // A date-based event covers [start, end) — every day gets a chip, capped at the window.
          let d = new Date(Math.max(e.start, start));
          const stop = Math.min(e.end ? e.end.getTime() : e.start.getTime() + 86400000, end.getTime());
          for(let n = 0; d.getTime() < stop && n < 62; n++){
            out.push({ cal: NOSTR_CAL, uid: e.uid, title: e.title, location: e.location,
                       notes: e.notes, allDay: true, start: new Date(d), key: ymd(d),
                       component: 'VEVENT' });
            d = new Date(d.getFullYear(), d.getMonth(), d.getDate() + 1);
          }
        }else if(e.start >= start && e.start < end){
          out.push({ cal: NOSTR_CAL, uid: e.uid, title: e.title, location: e.location,
                     notes: e.notes, allDay: false, start: e.start, key: ymd(e.start),
                     component: 'VEVENT' });
        }
      }
      return out;
    }
    let _index = null, _indexSig = '';
    function occurrenceIndex(){
      const m = S.month || firstOf(new Date());
      const start = gridStart(m);
      const end = new Date(start.getFullYear(), start.getMonth(), start.getDate() + 42);
      const sig = `${ymd(start)}|${S.rev}`;
      if(_index && _indexSig === sig) return _index;
      const I = window.PCIcal, map = {};
      for(const cid of Object.keys(S.items)){
        for(const rec of (S.items[cid] || [])){
          let occ = [];
          // One malformed item must not blank the whole month.
          try{ occ = I.occurrences(I.parseResource(Object.assign({ cal: cid }, rec)), start, end); }
          catch(err){ occ = []; }
          for(const o of occ) (map[o.key] = map[o.key] || []).push(decorate(o));
        }
      }
      if(nostrOn()) for(const o of _n52occ(start, end)) (map[o.key] = map[o.key] || []).push(decorate(o));
      for(const k of Object.keys(map)){
        map[k].sort((a, b) => (a.allDay === b.allDay) ? (a.time || '').localeCompare(b.time || '')
                                                      : (a.allDay ? -1 : 1));
      }
      _index = map; _indexSig = sig;
      return map;
    }

    const colorOf = cal => {
      if(cal === NOSTR_CAL) return '#a78bfa';           // the network's events, one fixed violet
      const c = S.cals.find(c => c.id === cal);
      if(c && c.color) return c.color;
      const i = Math.max(0, S.cals.findIndex(c => c.id === cal));
      return PALETTE[i % PALETTE.length];
    };
    const eventsFor = key => occurrenceIndex()[key] || [];

    /* The month a stored item's series STARTS in, as [year, monthIndex] — cheap, because it only
     * reads DTSTART rather than expanding anything. */
    function itemMonths(){
      const out = [];
      for(const cid of Object.keys(S.items)){
        for(const rec of (S.items[cid] || [])){
          try{
            const m = window.PCIcal.parseResource(rec).master;
            if(m && m.start) out.push(m.start);
          }catch(_){}
        }
      }
      return out;
    }

    /* An imported calendar is usually a HISTORY: of one real 707-event export, exactly one event
     * fell in the month it was imported in. Landing on an empty grid after a successful import is
     * indistinguishable from the import having failed, so after one we move to a month that has
     * something in it. Only after an import — never on an ordinary load, which would fight the
     * person's own navigation. */
    function jumpToContent(){
      if(Object.keys(occurrenceIndex()).length) return;      // this month already shows something
      const all = itemMonths();
      if(!all.length) return;
      const now = new Date();
      // The nearest month to today that has an event, preferring the future when it is a tie.
      let best = null;
      for(const d of all){
        if(!best || Math.abs(d - now) < Math.abs(best - now)) best = d;
      }
      if(!best) return;
      S.month = firstOf(best);
      S.sel = ymd(best);
      paint();
    }

    /* When the shown month is empty but the calendar is not, say so — and offer the jump rather than
     * making someone page backwards through years of empty grids. */
    function emptyHint(){
      if(Object.keys(occurrenceIndex()).length) return '';
      const all = itemMonths();
      if(!all.length) return '';
      const now = new Date();
      let near = all[0];
      for(const d of all) if(Math.abs(d - now) < Math.abs(near - now)) near = d;
      const n = all.length;
      return `<div class="cal-hint">
        <span>Nothing in ${MONTHS[(S.month||firstOf(now)).getMonth()]}
          ${(S.month||firstOf(now)).getFullYear()} — this calendar has ${n} item${n===1?'':'s'},
          nearest ${MONTHS[near.getMonth()]} ${near.getFullYear()}.</span>
        <button class="btn btn-ghost small" id="cal-jump">Go there</button></div>`;
    }

    // ---- load --------------------------------------------------------------------------------
    /* THE OFFLINE CACHE.
     *
     * Calendar items come from `/api/calendar/*`, so with no network the screen was a spinner and
     * then an error — on the app that keeps Notes, Passwords and the timeline working offline. It is
     * also the difference between a month grid that appears instantly on open and one that appears
     * after two round trips.
     *
     * IndexedDB, not localStorage: a real calendar is hundreds of KB of raw iCalendar, localStorage
     * is a shared ~5 MB quota for the whole origin, and a quota error there is thrown at whatever
     * happens to write next — which would be somebody's note.
     *
     * WHAT IS STORED IS ALREADY-DECRYPTED ICS, which is a real trade and worth stating: the events
     * are readable to anything that can read this device's IndexedDB. The same is true of the CalDAV
     * copy on the phone's own calendar app, and of everything else this screen shows — the calendar
     * is explicitly the one part of this app the SERVER can read too (see docs/CALENDAR.md), so a
     * device-local cache is not a new exposure. Notes and the vault, which the server CANNOT read,
     * are not cached this way and must not be. */
    const CalCache = {
      DB: 'pccal', VER: 1, STORE: 'cal', _db: null,
      _open(){
        if(this._db) return Promise.resolve(this._db);
        return new Promise((res, rej) => {
          let rq; try{ rq = indexedDB.open(this.DB, this.VER); }catch(e){ return rej(e); }
          rq.onupgradeneeded = () => { const db = rq.result;
            if(!db.objectStoreNames.contains(this.STORE)) db.createObjectStore(this.STORE); };
          rq.onsuccess = () => { this._db = rq.result; res(this._db); };
          rq.onerror = () => rej(rq.error || new Error('indexeddb unavailable'));
        });
      },
      async _tx(mode, fn){
        const db = await this._open();
        return new Promise((res, rej) => {
          const tx = db.transaction(this.STORE, mode), st = tx.objectStore(this.STORE);
          let out; try{ out = fn(st); }catch(e){ return rej(e); }
          // `'result' in out`, not `out.result !== undefined` — a MISS gives a request whose result
          // is undefined, and unwrapping on that hands back the REQUEST OBJECT. Spelled the way the
          // music cache and folder sync spell it, because that trap has been paid for twice.
          tx.oncomplete = () => res(out && typeof out === 'object' && ('result' in out) ? out.result : out);
          tx.onerror = () => rej(tx.error); tx.onabort = () => rej(tx.error);
        });
      },
      _key(which){ const me = owner(); return me ? which + ':' + me : ''; },
      async save(cals, items){
        const key = this._key('snapshot'); if(!key) return;
        try{ await this._tx('readwrite', st => st.put({ cals, items, at: Date.now() }, key)); }
        catch(_){ /* a full or unavailable IDB must never fail a load that worked */ }
      },
      async read(){
        const key = this._key('snapshot'); if(!key) return null;
        try{ return await this._tx('readonly', st => st.get(key)); }
        catch(_){ return null; }
      },
      async clear(){ const key=this._key('snapshot'); if(!key)return;
        try{ await this._tx('readwrite', st => st.delete(key)); }catch(_){} },
      // The pending WRITES, kept beside the snapshot in the same store — one database, one upgrade.
      async saveQ(q){ const key=this._key('queue'); if(!key)return;
        try{ await this._tx('readwrite', st => st.put(q, key)); }catch(_){} },
      async readQ(){ const key=this._key('queue'); if(!key)return [];
        try{ return await this._tx('readonly', st => st.get(key)); }catch(_){ return []; } },
    };

    /* THE OFFLINE WRITE QUEUE — the half the read cache does not cover.
     *
     * Notes can be written on a train and published later; the calendar could not, because every
     * write is an HTTP call and a failed one was a toast and nothing else. "Add an event" is exactly
     * the thing people do while offline (on the train, deciding when to meet), so losing it is the
     * worst possible moment to lose one.
     *
     * A queued write is applied to the LOCAL COPY immediately, so the event appears on the grid the
     * way it would have — the difference is a line saying it has not reached the server yet.
     *
     * REPLAY IS IDEMPOTENT by construction: an item is addressed by (calendar, uid) and `put_item`
     * REPLACES, so re-sending is a no-op, and a delete for something already gone answers 404 and is
     * dropped. That is what makes a blind flush safe here where it is not for Notes, whose queue has
     * to check for a newer version first — a note is a document only the author can decrypt, so
     * nothing else can tell the client it is stale, while the calendar's server answer is the truth
     * and the next load overwrites everything anyway. */
    const CalQueue = {
      async read(){ const s = await CalCache.readQ(); return Array.isArray(s) ? s : []; },
      async add(op){
        const q = await this.read();
        // One entry per (op, cal, uid): editing the same event five times offline should send once.
        const key = (x) => x.op + '|' + x.cal + '|' + x.uid;
        const out = q.filter(x => key(x) !== key(op));
        out.push(op);
        await CalCache.saveQ(out.slice(-500));
        S.queued = out.length;
      },
      async flush(){
        const q = await this.read();
        if(!q.length) return 0;
        const left = [], refused = [];
        let sent = 0;
        for(const op of q){
          try{
            if(op.op === 'del'){
              await api(`/api/calendar/items?cal=${encodeURIComponent(op.cal)}&uid=${encodeURIComponent(op.uid)}`,
                        { method:'DELETE' });
            }else{
              await jput('/api/calendar/items', { cal: op.cal, uid: op.uid, ics: op.ics });
            }
            sent++;
          }catch(err){
            // A 4xx is the server REFUSING it — replaying that for ever is a queue that never drains
            // and an error that is never seen. Only a transport failure is worth keeping.
            if(err && err.status && err.status < 500){ refused.push(op); continue; }
            left.push(op);
          }
        }
        await CalCache.saveQ(left);
        S.queued = left.length;
        /* A REFUSED write has to be said out loud. The person was told "saved on this device — it
         * will sync when you are back online", and dropping it quietly makes that a lie they find
         * out about weeks later, when the appointment does not happen. */
        if(refused.length){
          toast(refused.length === 1
            ? 'an event you made offline was refused by the server and has been dropped'
            : refused.length + ' events you made offline were refused by the server and dropped');
        }
        return sent;
      },
    };

    /* Paint from the cache BEFORE the network is asked. Returns whether anything was drawn, so a
     * failed load can say "showing your saved calendar" rather than "could not load". */
    async function loadCached(){
      // BEFORE the early return: the badge is about the QUEUE, not about the snapshot, and on a
      // return visit (when the live data is already in memory) the early return would skip it. Within
      // one session `add`/`flush` keep it current; this is what makes it right on the first paint
      // after a reload, which is exactly when there is something queued to tell someone about.
      try{ S.queued = ((await CalQueue.read()) || []).length; }catch(_){}
      if(S.ready || S.cals.length) return false;          // the live data is already here
      const snap = await CalCache.read();
      if(!snap || !Array.isArray(snap.cals) || !snap.cals.length) return false;
      S.cals = snap.cals;
      S.items = snap.items || {};
      if(!S.cal || !S.cals.some(c => c.id === S.cal)) S.cal = (S.cals[0] || {}).id || '';
      S.rev++;                       // invalidates the occurrence index
      S.cached = true;
      paint();
      return true;
    }

    /* Apply a queued write to what this device is showing. Without it an event added offline is
     * simply absent from the grid, which reads as the save having failed — and the point of the queue
     * is that it did not. */
    function _applyLocal(cal, item){
      const list = (S.items[cal] = (S.items[cal] || []).filter(x => x.uid !== item.uid));
      if(!item.remove) list.push({ cal, uid: item.uid, ics: item.ics, component: item.component, ts: Date.now()/1000 });
      S.rev++;
      CalCache.save(S.cals, S.items);
    }

    async function load(){
      const mine = owner(), gen = ++S.loadGen;
      if(mine !== S.owner){
        S.owner = mine; S.ready = false; S.loading = false; S.enabled = null; S.cals = []; S.cal = '';
        S.items = {}; S.sync = null; S.cached = false; S.error = ''; S.rev++;
      }
      const stale = () => gen !== S.loadGen || mine !== owner();
      S.loading = true; S.error = '';
      paint();
      // The Nostr layer rides its own socket and its own clock: fired here, drawn when it lands,
      // never blocking the personal calendar it sits beside. Refreshed when stale — an event
      // published after this tab opened must not need a reload to exist.
      if(nostrOn() && (_n52evs === null || Date.now() - _n52at > 10 * 60000)){
        if(_n52evs === null) _n52evs = [];
        loadNostr();
      }
      try{
        const sync = await api('/api/calendar/config');
        if(stale()) return;
        S.sync = sync;
        // The config call reaching the server IS the proof that it is reachable, so this is the
        // cheapest correct moment to drain — before the read, so what comes back already includes it.
        try{ const n = await CalQueue.flush();
             if(stale()) return;
             if(n) toast(n === 1 ? 'an event you made offline has synced'
                                 : n + ' events you made offline have synced'); }catch(_){}
        S.enabled = !!S.sync.enabled;
        if(!S.enabled){ S.loading = false; paint(); return; }
        const r = await api('/api/calendar/calendars');
        if(stale()) return;
        const cals = r.calendars || [];
        const items = {};
        for(const c of cals){
          try{ items[c.id] = (await api('/api/calendar/items?cal=' + encodeURIComponent(c.id))).items || []; }
          catch(_){ items[c.id] = []; }
          if(stale()) return;
        }
        S.cals = cals;
        if(!S.cal || !S.cals.some(c => c.id === S.cal)) S.cal = (S.cals[0] || {}).id || '';
        S.items = items;
        S.rev++;                        // invalidates the occurrence index
        S.cached = false;
        CalCache.save(S.cals, items);   // fire and forget: a cache write must not slow a load down
      }catch(e){
        if(stale()) return;
        // 404 is the server being off, which is a state to explain rather than an error to report.
        S.enabled = /off on this node/i.test((e && e.message) || '') ? false : S.enabled;
        // A failure WITH a cache behind it is not a failure worth a red box: the month you are
        // looking at is real, it is just not fresh. Say which, and let it be read.
        if(S.enabled !== false) S.error = S.cals.length
          ? 'showing your saved calendar — could not reach the server'
          : ((e && e.message) || 'could not load your calendars');
      }finally{
        if(stale()) return;
        S.loading = false; S.ready = true; paint();
        // After the data, never before: pushWidget reads S.items, and pushing an empty set would
        // blank a correct widget for as long as the load took.
        try{ pushWidget(); }catch(_){}
      }
    }

    // ---- rendering ---------------------------------------------------------------------------
    function head(){
      const m = S.month || firstOf(new Date());
      const picker = S.cals.length ? `<select class="input cal-pick" id="cal-pick">
          ${S.cals.map(c => `<option value="${enc(c.id)}"${c.id===S.cal?' selected':''}>${enc(c.displayname || c.id)}</option>`).join('')}
        </select>` : '';
      return `<div class="cal-bar">
        <div class="cal-nav">
          <button class="btn btn-ghost small" id="cal-prev" aria-label="Previous month">‹</button>
          <button class="btn btn-ghost small" id="cal-today">Today</button>
          <button class="btn btn-ghost small" id="cal-next" aria-label="Next month">›</button>
          <div class="cal-title">${MONTHS[m.getMonth()]} ${m.getFullYear()}</div>
          ${S.queued ? `<span class="cal-pending" title="written on this device, not on the server yet"
            >${S.queued} waiting to sync</span>` : ''}
          ${S.cached ? '<span class="cal-pending cached" title="the server could not be reached — this is your saved copy">offline copy</span>' : ''}
        </div>
        <div class="cal-tools">
          ${picker}
          <button class="btn btn-cyan small" id="cal-new"><svg class="ic b-ic" aria-hidden="true"><use href="#i-plus"></use></svg>Event</button>
          <button class="btn btn-ghost small" id="cal-menu" aria-label="Calendar options"><svg class="ic b-ic" aria-hidden="true"><use href="#i-menu"></use></svg></button>
        </div>
      </div>`;
    }

    function grid(){
      const m = S.month || firstOf(new Date());
      const start = gridStart(m), today = todayKey();
      let cells = '';
      for(let i = 0; i < 42; i++){
        const d = new Date(start.getFullYear(), start.getMonth(), start.getDate() + i);
        const key = ymd(d), evs = eventsFor(key);
        const other = d.getMonth() !== m.getMonth();
        /* A dot says "something happens" and nothing else, which is no use when the question is
         * "what". Each event gets a short chip — the start time and as much of the title as fits —
         * the way every other calendar does it. BOTH forms are emitted and the stylesheet picks:
         * a phone's month cell is ~46px wide, where a chip can show four characters and a dot is
         * genuinely the better answer. Doing it in CSS rather than JS means rotating the device or
         * resizing the window switches over with no repaint and nothing to keep in sync. */
        const chips = evs.slice(0, 3).map(e =>
          `<i class="cal-chip" style="--c:${enc(colorOf(e.cal))}" title="${enc(e.title)}">${
            e.allDay ? '' : `<b>${enc(String(e.time||'').split('–')[0].split('-')[0].trim())}</b> `
          }${enc(e.title)}</i>`).join('');
        const dots = evs.slice(0, 4).map(e =>
          `<i class="cal-dot" style="background:${enc(colorOf(e.cal))}"></i>`).join('');
        cells += `<button class="cal-day${other?' other':''}${key===today?' today':''}${key===S.sel?' sel':''}"
                          data-key="${key}">
            <span class="cal-num">${d.getDate()}</span>
            <span class="cal-chips">${chips}${evs.length>3?`<i class="cal-more">+${evs.length-3} more</i>`:''}</span>
            <span class="cal-dots">${dots}${evs.length>4?`<i class="cal-more">+${evs.length-4}</i>`:''}</span>
          </button>`;
      }
      return `<div class="cal-grid">${DAYS.map(d=>`<div class="cal-dow">${d}</div>`).join('')}${cells}</div>`;
    }

    function dayPanel(){
      const key = S.sel || todayKey();
      const evs = eventsFor(key);
      const d = new Date(key + 'T00:00:00');
      const list = evs.length ? evs.map(e => `<div class="cal-ev" data-uid="${enc(e.uid)}" data-cal="${enc(e.cal)}">
          <i class="cal-evbar" style="background:${enc(colorOf(e.cal))}"></i>
          <div class="cal-evbody">
            <div class="cal-evtitle">${enc(e.title)}</div>
            <div class="cal-evmeta">${e.allDay ? 'All day' : enc(e.time)}${e.location?' · '+enc(e.location):''}</div>
          </div>
          ${e.cal === NOSTR_CAL ? '' : `<button class="btn btn-ghost small cal-edit" data-uid="${enc(e.uid)}" data-cal="${enc(e.cal)}">Edit</button>`}
        </div>`).join('')
        : '<div class="empty">Nothing on this day.</div>';
      return `<div class="cal-day-panel">
        <div class="cal-day-hd">${DAYS[(d.getDay()+6)%7]} ${d.getDate()} ${MONTHS[d.getMonth()]}</div>
        ${list}</div>`;
    }

    function offScreen(){
      if(S.enabled === false){
        return `<div class="cal-off">
          <div class="cal-offhd">The calendar server is off on this node</div>
          <p class="muted">An admin turns it on in <b>Admin → Tools → Calendar server</b>. It runs inside
             this app — there is nothing to install — and your calendars are stored as encrypted Nostr
             events.</p></div>`;
      }
      return `<div class="ws-err">${enc(S.error || 'could not load your calendars')}</div>`;
    }

    function paint(){
      /* NEVER DRAW INTO A VIEW WE NO LONGER OWN — see the same guard in contacts.js. This one has
       * two live triggers: `widgetTick` calls load() a few seconds after app start, from whatever
       * screen the user is actually on, and a load started here finishes after they have navigated
       * away. Both used to replace #feed with a calendar. */
      if(!inView()) return;
      const feed = $('#feed'); if(!feed) return;
      if(S.loading && !S.ready){ feed.innerHTML = '<div class="cal-wrap"><div class="spinner"></div></div>'; return; }
      if(S.enabled === false || S.error){ feed.innerHTML = `<div class="cal-wrap">${offScreen()}</div>`; return; }
      feed.innerHTML = `<div class="cal-wrap">${head()}${emptyHint()}${grid()}${dayPanel()}</div>`;
      wire(feed);
      const s = scroller();
      if(s) requestAnimationFrame(()=>{ try{ s.scrollTop = S.scroll || 0; }catch(_){} });
    }

    function wire(root){
      const on = (sel, fn) => { const el = $(sel, root); if(el) el.onclick = fn; };
      on('#cal-prev', ()=>{ S.month = addMonths(S.month || firstOf(new Date()), -1); paint(); });
      on('#cal-next', ()=>{ S.month = addMonths(S.month || firstOf(new Date()), 1); paint(); });
      on('#cal-today', ()=>{ S.month = firstOf(new Date()); S.sel = todayKey(); paint(); });
      on('#cal-new', ()=> editEvent(null));
      on('#cal-menu', openMenu);
      on('#cal-jump', ()=>{ jumpToContent(); });
      const pick = $('#cal-pick', root);
      if(pick) pick.onchange = ()=>{ S.cal = pick.value; paint(); };
      $$('.cal-day', root).forEach(b => b.onclick = ()=>{ S.sel = b.dataset.key; paint(); });
      $$('.cal-edit', root).forEach(b => b.onclick = (e)=>{
        e.stopPropagation();
        const rec = (S.items[b.dataset.cal] || []).find(r => r.uid === b.dataset.uid);
        if(rec) editEvent(Object.assign(parseItem(rec), { cal: b.dataset.cal }));
      });
      $$('.cal-ev', root).forEach(el => el.onclick = ()=>{
        if(el.dataset.cal === NOSTR_CAL){ nostrDetails(el.dataset.uid); return; }
        const rec = (S.items[el.dataset.cal] || []).find(r => r.uid === el.dataset.uid);
        if(rec) editEvent(Object.assign(parseItem(rec), { cal: el.dataset.cal }));
      });
      const sc = $('#feed');
      if(sc) sc.onscroll = ()=>{ if(inView()) S.scroll = sc.scrollTop; };
    }

    // ---- event editor ------------------------------------------------------------------------
    /* A network event opens as a CARD: what, when, where, who — with the organizer's profile a tap
     * away — and no Edit, because it is somebody's published statement, not a row of yours. */
    function nostrDetails(uid){
      const e = (_n52evs || []).find(x => x.uid === uid); if(!e) return;
      const p2 = (PC.profOf && PC.profOf(e.pk)) || {}; if(PC.needProfile) PC.needProfile(e.pk);
      const when = e.allDay
        ? (ymd(e.start) + (e.end ? ' \u2192 ' + ymd(new Date(e.end.getTime() - 86400000)) : '') + ' \u00b7 all day')
        : (e.start.toLocaleString() + (e.end ? ' \u2192 ' + e.end.toLocaleTimeString([], {hour:'2-digit',minute:'2-digit'}) : ''));
      PC.modal(`<h3>\ud83d\udfe3 ${enc(e.title)}</h3>
        <div class="muted small">${enc(when)}${e.location ? ' \u00b7 \ud83d\udccd ' + enc(e.location) : ''}</div>
        ${e.notes ? `<div style="white-space:pre-wrap;overflow-wrap:anywhere;margin-top:10px;max-height:50vh;overflow-y:auto">${enc(e.notes)}</div>` : ''}
        <div class="row" style="margin-top:12px;align-items:center;gap:8px">
          <img src="${enc(p2.picture || PC.LOGO)}" onerror="this.src='${PC.LOGO}'" style="width:28px;height:28px;border-radius:50%">
          <button class="btn btn-ghost small" id="n52-who">${enc(p2.name || p2.display_name || 'organizer')}</button>
        </div>`, root => {
          const w = root.querySelector('#n52-who');
          if(w) w.onclick = ()=>{ PC.closeModal(); PC.openProfile && PC.openProfile(e.pk); };
        });
    }
    /* Publishing one is a plain signed 31923 — time-based, the common case — through the app's own
     * publish(). Nothing touches CalDAV: a public event is a statement to the network, and your
     * private calendar is a different thing on purpose. */
    function publishNostrEvent(){
      const today = todayKey();
      PC.modal(`<h3>\ud83d\udfe3 Publish a Nostr event</h3>
        <div class="muted small">Public: anyone on your relays can see it.</div>
        <label class="fld">Title<input class="input" id="n52-t" maxlength="120"></label>
        <label class="fld">Date<input class="input" id="n52-d" type="date" value="${enc(today)}"></label>
        <div class="row" style="gap:8px">
          <label class="fld" style="flex:1">Starts<input class="input" id="n52-s" type="time" value="18:00"></label>
          <label class="fld" style="flex:1">Ends<input class="input" id="n52-e" type="time" value="19:00"></label>
        </div>
        <label class="fld">Location (optional)<input class="input" id="n52-l" maxlength="200"></label>
        <label class="fld">Details (optional)<textarea class="input" id="n52-n" rows="4"></textarea></label>
        <button class="btn btn-cyan full" id="n52-go">Publish</button>`, root => {
          root.querySelector('#n52-go').onclick = async ()=>{
            const v = (id) => (root.querySelector(id).value || '').trim();
            const title = v('#n52-t'); if(!title) return PC.toast('give it a title');
            const day = v('#n52-d'); if(!day) return PC.toast('pick a date');
            const st = new Date(day + 'T' + (v('#n52-s') || '18:00'));
            let en = new Date(day + 'T' + (v('#n52-e') || ''));
            if(!(en > st)) en = new Date(st.getTime() + 3600000);
            const tags = [['d', 'pc-' + Math.random().toString(36).slice(2, 10)],
                          ['title', title],
                          ['start', String(Math.floor(st.getTime() / 1000))],
                          ['end', String(Math.floor(en.getTime() / 1000))]];
            if(v('#n52-l')) tags.push(['location', v('#n52-l')]);
            const btn = root.querySelector('#n52-go');
            btn.disabled = true; btn.textContent = 'publishing\u2026';
            try{
              const r = await PC.publish(31923, v('#n52-n'), tags);
              if(r && r.ok === false) throw new Error('the relay did not store it');
              PC.closeModal(); PC.toast('\ud83d\udfe3 published');
              loadNostr();
            }catch(err){
              btn.disabled = false; btn.textContent = 'Publish';
              PC.toast('not published: ' + ((err && err.message) || err));
            }
          };
        });
    }
    function editEvent(ev){
      const isNew = !ev;
      const key = (ev && ev.key) || S.sel || todayKey();
      const cal = (ev && ev.cal) || S.cal || (S.cals[0] || {}).id || '';
      if(!cal){ makeCalendar(); return; }          // no calendar yet — make one first
      /* A SUBSCRIBED calendar is a MIRROR, so an edit here is not saved-then-lost, it is saved and
       * then silently replaced by the next refresh — which is worse, because it looks like it
       * worked. Refuse with the reason and offer somewhere the edit can actually live. */
      { const sc = S.cals.find(c => c.id === cal);
        if(sc && subOf(sc)){
          const other = S.cals.find(c => !subOf(c));
          toast(other ? `“${sc.displayname || sc.id}” follows a feed — pick another calendar to add to`
                      : `“${sc.displayname || sc.id}” follows a feed, so it cannot be edited`);
          if(other) S.cal = other.id;
          return;
        } }
      const e = ev || { title:'', date:key, start:'09:00', end:'10:00', allDay:false, location:'', notes:'' };
      const dateVal = e.key || e.date || key;
      modal(`<h3>${isNew ? 'New event' : 'Event'}</h3>
        <div class="cal-form">
        <label class="fld">Title<input class="input" id="cev-title" value="${enc(e.title||'')}" placeholder="What is it?"></label>
        <label class="fld">Day<input class="input" id="cev-date" type="date" value="${enc(dateVal)}"></label>
        <label class="fld cal-allday"><input type="checkbox" id="cev-allday"${e.allDay?' checked':''}> All day</label>
        <div class="cal-times" id="cev-times"${e.allDay?' style="display:none"':''}>
          <label class="fld">From<input class="input" id="cev-start" type="time" value="${enc(e.time || e.start || '09:00')}"></label>
          <label class="fld">To<input class="input" id="cev-end" type="time" value="${enc(e.end || '')}"></label>
        </div>
        <label class="fld">Where <span class="muted small">(optional)</span><input class="input" id="cev-loc" value="${enc(e.location||'')}"></label>
        <label class="fld">Notes <span class="muted small">(optional)</span><textarea class="input" id="cev-notes" rows="3">${enc(e.notes||'')}</textarea></label>
        ${e.repeats ? '<div class="muted small cal-repeats">This event repeats. Saving changes every'
                      + ' occurrence; the repeat rule itself is kept as it is.</div>' : ''}
        </div>
        <div class="row" style="margin-top:14px">
          <button class="btn btn-cyan" id="cev-save">Save</button>
          ${isNew ? '' : '<button class="btn btn-ghost" id="cev-del">Delete</button>'}
        </div>`, root => {
        const ad = $('#cev-allday', root), times = $('#cev-times', root);
        if(ad) ad.onchange = ()=>{ times.style.display = ad.checked ? 'none' : ''; };
        $('#cev-save', root).onclick = async ()=>{
          const body = {
            uid: e.uid || '', title: $('#cev-title', root).value.trim(),
            date: $('#cev-date', root).value || dateVal,
            allDay: !!(ad && ad.checked),
            start: $('#cev-start', root).value, end: $('#cev-end', root).value,
            location: $('#cev-loc', root).value.trim(), notes: $('#cev-notes', root).value.trim(),
            raw: e.raw || null,          // repeat rule, exceptions and edited occurrences, verbatim
          };
          if(!body.title){ toast('give it a title'); return; }
          const built = buildIcs(body);
          try{
            await jput('/api/calendar/items', { cal, uid: built.uid, ics: built.ics });
            closeModal(); toast('saved'); await load();
          }catch(err){
            // A REFUSAL is not a network failure. A 4xx means the server read it and said no, and
            // queueing that would retry a rejection for ever while telling the user it was saved.
            if(err && err.status && err.status < 500){
              toast('could not save: ' + ((err && err.message) || 'error')); return;
            }
            await CalQueue.add({ op:'put', cal, uid: built.uid, ics: built.ics, at: Date.now() });
            _applyLocal(cal, { uid: built.uid, ics: built.ics, component: built.component || 'VEVENT' });
            closeModal(); toast('saved on this device — it will sync when you are back online');
            paint();
          }
        };
        const del = $('#cev-del', root);
        if(del) del.onclick = async ()=>{
          if(!(await uiConfirm('Delete this event?'))) return;
          try{
            await api(`/api/calendar/items?cal=${encodeURIComponent(cal)}&uid=${encodeURIComponent(e.uid)}`,
                      { method:'DELETE' });
            closeModal(); toast('deleted'); await load();
          }catch(err){
            if(err && err.status && err.status < 500){
              toast('could not delete: ' + ((err && err.message) || 'error')); return;
            }
            await CalQueue.add({ op:'del', cal, uid: e.uid, at: Date.now() });
            _applyLocal(cal, { uid: e.uid, remove: true });
            closeModal(); toast('deleted here — it will sync when you are back online');
            paint();
          }
        };
      });
    }

    // ---- calendars, import/export, phone ------------------------------------------------------
    function openMenu(){
      modal(`<h3>Calendars</h3>
        <div class="cal-list">${S.cals.map(c => `<div class="cal-row">
            <i class="cal-dot" style="background:${enc(colorOf(c.id))}"></i>
            <span class="cal-name">${enc(c.displayname || c.id)}${subOf(c) ? ' <span class="cal-sub-tag">subscribed</span>' : ''}</span>
            ${subOf(c) ? `<span class="cal-sub-when">${enc(subWhen(subOf(c)))}</span>` : ''}
            ${subOf(c) ? `<button class="btn btn-ghost small cal-refresh" data-id="${enc(c.id)}">Refresh</button>` : ''}
            ${subOf(c) ? `<button class="btn btn-ghost small cal-unsub" data-id="${enc(c.id)}">Unsubscribe</button>` : ''}
            <a class="btn btn-ghost small" href="/api/calendar/export?cal=${encodeURIComponent(c.id)}"
               download><svg class="ic b-ic" aria-hidden="true"><use href="#i-download"></use></svg>Export</a>
            <button class="btn btn-ghost small cal-del" data-id="${enc(c.id)}">Delete</button>
          </div>`).join('') || '<div class="empty">No calendars yet.</div>'}
        <div class="cal-row">
            <i class="cal-dot" style="background:#a78bfa"></i>
            <span class="cal-name">Nostr events <span class="cal-sub-tag">network</span></span>
            <label class="switch" title="Show the network's public NIP-52 events on your grid"><input type="checkbox" id="cal-nostr-on"${nostrOn()?' checked':''}><span class="slider"></span></label>
            <button class="btn btn-ghost small" id="cal-nostr-pub">\ud83d\udfe3 Publish an event</button>
        </div></div>
        <div class="row" style="margin-top:14px;flex-wrap:wrap;gap:8px">
          <button class="btn btn-cyan small" id="cal-add"><svg class="ic b-ic" aria-hidden="true"><use href="#i-plus"></use></svg>New calendar</button>
          <button class="btn btn-cyan small" id="cal-sub"><svg class="ic b-ic" aria-hidden="true"><use href="#i-link"></use></svg>Subscribe to a URL</button>
          <button class="btn btn-ghost small" id="cal-import"><svg class="ic b-ic" aria-hidden="true"><use href="#i-upload"></use></svg>Import .ics</button>
          <button class="btn btn-ghost small" id="cal-phone"><svg class="ic b-ic" aria-hidden="true"><use href="#i-android"></use></svg>Sync to a device</button>
        </div>
        <input type="file" id="cal-file" accept=".ics,text/calendar" hidden>`, root => {
        $('#cal-add', root).onclick = ()=>{ closeModal(); makeCalendar(); };
        { const nr = root.querySelector('#cal-nostr-on');
          if(nr) nr.onchange = ()=> setNostrOn(nr.checked);
          const np = root.querySelector('#cal-nostr-pub');
          if(np) np.onclick = ()=>{ closeModal(); publishNostrEvent(); }; }
        $('#cal-sub', root).onclick = ()=>{ closeModal(); subscribePanel(); };
        $$('.cal-refresh', root).forEach(b => b.onclick = async ()=>{
          b.disabled = true; b.textContent = 'checking…';
          try{
            const r = await jpost('/api/calendar/subscribe/refresh?cal=' + encodeURIComponent(b.dataset.id), {});
            const one = (r.refreshed || [])[0] || {};
            toast(one.ok === false ? ('could not refresh: ' + (one.error || 'error'))
                 : one.unchanged ? 'no changes'
                 : `updated — ${one.count || 0} event${one.count === 1 ? '' : 's'}`);
          }catch(err){ toast('could not refresh: ' + ((err && err.message) || 'error')); }
          closeModal(); await load();
        });
        $$('.cal-unsub', root).forEach(b => b.onclick = async ()=>{
          if(!await uiConfirm('Stop following this feed? The events it already gave you stay — '
                            + 'it just stops updating.', { ok: 'Unsubscribe' })) return;
          try{ await jpost('/api/calendar/unsubscribe?cal=' + encodeURIComponent(b.dataset.id), {});
               toast('unsubscribed'); }
          catch(err){ toast('could not unsubscribe: ' + ((err && err.message) || 'error')); }
          closeModal(); await load();
        });
        $('#cal-phone', root).onclick = ()=>{ closeModal(); phonePanel(); };
        $('#cal-import', root).onclick = ()=> $('#cal-file', root).click();
        $('#cal-file', root).onchange = async (e)=>{
          const f = e.target.files && e.target.files[0]; if(!f) return;
          const fd = new FormData(); fd.append('file', f);
          /* A real calendar is hundreds of events and each one is its own signed, encrypted write,
           * so this takes tens of seconds. With no feedback the modal just sits there and the only
           * honest reading is "nothing happened" — so the panel becomes a progress state, and it
           * says what is being worked on rather than spinning anonymously. */
          const busy = document.createElement('div');
          busy.className = 'cal-importing';
          busy.innerHTML = `<div class="spinner"></div>
            <div>Importing <b>${enc(f.name)}</b>…</div>
            <div class="muted small">Every event is signed and encrypted on the way in. A few
              hundred takes a moment; you can leave this open.</div>`;
          const body = root.querySelector('.cal-list');
          if(body) body.replaceWith(busy); else root.appendChild(busy);
          $$('.btn', root).forEach(b => { b.disabled = true; });
          try{
            await ensureAiSession();
            const target = S.cal || f.name.replace(/\.ics$/i, '');
            const r = await authFetch('/api/calendar/import?cal=' + encodeURIComponent(target),
                                      { method:'POST', body: fd }).then(r => r.json());
            if(r && r.detail) throw new Error(r.detail);
            closeModal();
            toast(`imported ${r.imported || 0} event${(r.imported||0)===1?'':'s'}`
                  + (r.skipped ? ` (${r.skipped} skipped)` : ''));
            if(r.calendar) S.cal = r.calendar;
            await load();
            jumpToContent();
          }catch(err){
            closeModal();
            toast('import failed: ' + ((err && err.message) || 'error'));
          }
        };
        $$('.cal-del', root).forEach(b => b.onclick = async ()=>{
          if(!(await uiConfirm('Delete this calendar and everything in it?'))) return;
          try{
            await api('/api/calendar/calendars/' + encodeURIComponent(b.dataset.id), { method:'DELETE' });
            closeModal(); toast('calendar deleted'); await load();
          }catch(err){ toast('could not delete: ' + ((err && err.message) || 'error')); }
        });
      });
    }

    function makeCalendar(){
      modal(`<h3>New calendar</h3>
        <label class="fld">Name<input class="input" id="cnew-name" placeholder="Work, Family, …"></label>
        <label class="fld">Colour<div class="cal-swatches" id="cnew-colors">
          ${PALETTE.map((c,i)=>`<button type="button" class="cal-sw${i===0?' on':''}" data-c="${c}"
                                        style="background:${c}" aria-label="colour ${i+1}"></button>`).join('')}
        </div></label>
        <div class="row" style="margin-top:14px"><button class="btn btn-cyan" id="cnew-save">Create</button></div>`,
      root => {
        let color = PALETTE[0];
        $$('.cal-sw', root).forEach(b => b.onclick = ()=>{
          color = b.dataset.c; $$('.cal-sw', root).forEach(x => x.classList.toggle('on', x === b)); });
        $('#cnew-save', root).onclick = async ()=>{
          const name = $('#cnew-name', root).value.trim();
          if(!name){ toast('give it a name'); return; }
          try{
            const c = await jpost('/api/calendar/calendars', { name, color });
            closeModal(); S.cal = c.id; toast('calendar created'); await load();
          }catch(err){ toast('could not create: ' + ((err && err.message) || 'error')); }
        };
      });
    }

    /* A SUBSCRIBED calendar mirrors somebody else's published .ics — a school term, a fixture list.
     * The subscription lives on the calendar's own metadata (see app/services/caldav_subscribe.py),
     * so everything else about it is an ordinary calendar: it lists, exports and syncs to a phone. */
    function subWhen(sub){
      if(sub.error) return '⚠ ' + sub.error.slice(0, 60);
      const t = Number(sub.refreshed || 0);
      if(!t) return 'not fetched yet';
      const d = Math.max(0, Math.floor(Date.now() / 1000) - t);
      if(d < 3600) return 'updated ' + Math.max(1, Math.round(d / 60)) + 'm ago';
      if(d < 172800) return 'updated ' + Math.round(d / 3600) + 'h ago';
      return 'updated ' + Math.round(d / 86400) + 'd ago';
    }

    /* THE ANDROID HOME-SCREEN WIDGET.
     *
     * A calendar item is an encrypted document; the widget is drawn by the LAUNCHER, which has no key
     * and no session. So the decrypting happens here — once, in the code that already does it — and
     * the widget is handed the answer. Anything else means a second iCalendar parser and a second
     * recurrence expander in Java, which is how the widget and the app end up disagreeing about what
     * day something is on.
     *
     * SEVERAL DAYS, keyed by LOCAL DATE, because the widget decides which day is "today" at DRAW time
     * rather than trusting when this was written — that is what keeps it right through midnight and
     * through the app not being opened for a week. It also fills its rows from the days after today,
     * the way the desktop "Today" widget does.
     *
     * Cheap and idempotent: it runs after a load, and a load is already the expensive part. */
    async function pushWidget(){
      const P = PC.capPlugin ? PC.capPlugin('CalendarWidget', 'push') : null;
      if(!P) return;                                    // not the packaged app
      /* ASK the widget how far ahead it reads. A second constant here is how the app ends up
       * pushing five days into a widget that draws seven and shows nothing for two of them. The
       * fallback only matters for an APK older than `window()`, which drew a week. */
      let span = 7;
      try{ span = Number(((await P.window()) || {}).days) || 7; }catch(_){}
      span = Math.max(1, Math.min(62, span));
      const I = window.PCIcal;
      if(!I) return;
      const now = new Date();
      const from = new Date(now.getFullYear(), now.getMonth(), now.getDate());
      const to = new Date(from.getFullYear(), from.getMonth(), from.getDate() + span);
      const days = {};
      for(const cid of Object.keys(S.items || {})){
        for(const rec of (S.items[cid] || [])){
          let occ = [];
          // One malformed item must not empty the widget.
          try{ occ = I.occurrences(I.parseResource(Object.assign({ cal: cid }, rec)), from, to); }
          catch(_){ occ = []; }
          for(const o of occ){
            if(!o || !o.start) continue;
            (days[o.key] = days[o.key] || []).push({
              t: o.allDay ? '' : `${pad(o.start.getHours())}:${pad(o.start.getMinutes())}`,
              s: String(o.title || '(no title)').slice(0, 80),
              // `p` = already finished. The widget dims those rather than dropping them: a day whose
              // entries disappear as it goes on reads as a calendar losing things.
              p: !o.allDay && o.start < now,
              /* `w` = who it is with, as the calendar wrote them (mailto:/tel:/bare address).
                 The home-screen widget ignores this; the phone's dialer and messages app read it to
                 put "you have a meeting with them at 3" beside a caller, matching against the
                 phone's OWN address book rather than against anything decrypted out here. Capped,
                 because a fifty-person invitation would otherwise ride into a SharedPreferences blob
                 fifty times over. */
              w: (o.who || []).slice(0, 12),
            });
          }
        }
      }
      for(const k of Object.keys(days)){
        days[k].sort((a, b) => (a.t || '').localeCompare(b.t || ''));
        days[k] = days[k].slice(0, 12);        // a widget shows four; this is the "+N more" count
      }
      try{ await P.push({ days }); }catch(_){}
    }

    function subscribePanel(prefill){
      modal(`<h3><svg class="ic h-ic" aria-hidden="true"><use href="#i-link"></use></svg>Subscribe to a calendar</h3>
        <p class="muted small">Paste the address of a published calendar — a school term, a team's
           fixtures, a holiday feed. It is copied into a calendar of your own and re-checked a few
           times a day, so it reaches your phone too. <b>Read-only:</b> the feed is the source, and
           anything you change here is replaced on the next update.</p>
        <label class="fld">Address<input class="input" id="csub-url" spellcheck="false"
          placeholder="https://example.org/calendar/feed/ical.ics"
          value="${enc((prefill && prefill.url) || '')}"></label>
        <label class="fld">Name <span class="muted small">(optional — the feed's own name is used if it has one)</span>
          <input class="input" id="csub-name" maxlength="80" value="${enc((prefill && prefill.name) || '')}"></label>
        <div id="csub-warn" class="cal-sub-warn" hidden></div>
        <div class="set-actions"><button class="btn btn-neon" id="csub-go">Subscribe</button>
          <button class="btn btn-ghost" id="csub-x">Cancel</button></div>`, root => {
        $('#csub-x', root).onclick = ()=> closeModal();
        // `insecure` is only ever set by the person, after being shown WHY — see the certificate
        // branch below. It skips certificate checking for this one feed and nothing else.
        let insecure = false;
        const go = $('#csub-go', root);
        go.onclick = async ()=>{
          const url = $('#csub-url', root).value.trim();
          if(!url){ toast('paste the calendar address'); return; }
          go.disabled = true; go.textContent = 'fetching…';
          try{
            const r = await jpost('/api/calendar/subscribe',
                                  { url, name: $('#csub-name', root).value.trim(), insecure });
            closeModal();
            toast(`subscribed — ${r.count || 0} event${r.count === 1 ? '' : 's'}`);
            S.cal = r.id; await load();
          }catch(err){
            go.disabled = false; go.textContent = 'Subscribe';
            /* A CERTIFICATE PROBLEM IS NOT A TYPO, and dead-ending on it makes the feature look
             * broken for feeds that are perfectly fine. Measured on the one this was built for: its
             * chain ends at a Let's Encrypt root no trust store here carries yet. So say whose
             * problem it is and offer the choice, once, in plain words. */
            const d = (err && err.detail) || {};
            if(d && d.certificate){
              const w = $('#csub-warn', root);
              w.hidden = false;
              w.innerHTML = `<b>That site's security certificate could not be verified.</b>
                Usually the site's own misconfiguration, or an authority this server does not know
                yet — not something you can fix. You can subscribe anyway; the events would then be
                fetched without that check.`;
              go.textContent = 'Subscribe anyway';
              insecure = true;
              return;
            }
            toast('could not subscribe: ' + ((err && err.message) || 'error'));
          }
        };
      });
    }

    function phonePanel(){
      const cfg = S.sync || {};
      modal(`<h3>Sync to a device</h3>
        <p class="muted small">Add a <b>CalDAV account</b> on your phone or desktop calendar app with
           these details. It syncs both ways — an event added on your phone appears here.</p>
        <label class="fld">Server<input class="input" id="cph-url" value="${enc(cfg.url||'')}" readonly></label>
        <label class="fld">Username<input class="input" id="cph-user" value="${enc(cfg.username||'')}" readonly></label>
        <div class="fld"><b>Password</b>
          <p class="muted small">A CalDAV-only app password, separate from your login — a phone stores
             it forever, so it should not be the password that owns your account. Shown once;
             generating a new one immediately stops every device using the old one.</p>
          <div class="row"><button class="btn btn-cyan small" id="cph-gen">
            ${cfg.has_password ? 'Generate a new password' : 'Generate password'}</button>
            ${cfg.has_password ? '<button class="btn btn-ghost small" id="cph-clear">Revoke</button>' : ''}</div>
          <div id="cph-out"></div>
        </div>`, root => {
        $('#cph-gen', root).onclick = async ()=>{
          try{
            const r = await jpost('/api/calendar/password');
            $('#cph-out', root).innerHTML =
              `<div class="cal-pw"><code>${enc(r.password)}</code>
                 <button class="btn btn-ghost small" id="cph-copy">Copy</button></div>
               <p class="muted small">Copy it now — it is stored only as a hash and cannot be shown again.</p>`;
            const cp = $('#cph-copy', root);
            if(cp) cp.onclick = ()=> PC.copyValue ? PC.copyValue(r.password, 'copied', 'Your calendar password:')
                                                 : toast('this build cannot reach the clipboard');
            S.sync = Object.assign({}, S.sync, { has_password: true });
          }catch(err){ toast('could not generate: ' + ((err && err.message) || 'error')); }
        };
        const cl = $('#cph-clear', root);
        if(cl) cl.onclick = async ()=>{
          if(!(await uiConfirm('Revoke the app password? Every synced device stops.'))) return;
          try{ await api('/api/calendar/password', { method:'DELETE' });
               S.sync = Object.assign({}, S.sync, { has_password:false });
               closeModal(); toast('revoked'); }
          catch(err){ toast('could not revoke: ' + ((err && err.message) || 'error')); }
        };
      });
    }

    // ---- the view ----------------------------------------------------------------------------
    function render(){
      const mine = owner();
      if(mine !== S.owner){
        S.owner = mine; ++S.loadGen; S.ready = false; S.loading = false; S.enabled = null; S.cals = []; S.cal = '';
        S.items = {}; S.sync = null; S.cached = false; S.error = ''; S.rev++;
      }
      if(!S.month) S.month = firstOf(new Date());
      if(!S.sel) S.sel = todayKey();
      paint();
      /* The CACHE first — instant on open and correct with no network at all — then the network.
       * loadCached no-ops once the live data is in memory, so a return trip still repaints from
       * state rather than re-reading IndexedDB. */
      loadCached().catch(()=>{}).then(()=> load());
    }

    /* KEEP THE HOME-SCREEN WIDGET FED WITHOUT OPENING THIS SCREEN.
     *
     * pushWidget runs at the end of `load()`, and `load()` only runs when the Calendar is rendered —
     * so somebody who adds events on a laptop and only ever glances at the phone's widget would see
     * a widget that was never filled at all. It has to be reachable from app start.
     *
     * CHEAP BY CONSTRUCTION, which is the whole point of doing it here rather than in a WorkManager
     * job: the snapshot is already on disk, so the common case is one IndexedDB read and a push of a
     * few KB — no network, no parse of anything that was not already parsed, and nothing that needs
     * the app to be running at a particular moment. The network is only spent when the snapshot has
     * aged past `maxAgeH`, which on a calendar is the right timescale: these change on human
     * schedules, and the widget already holds a MONTH, so being a few hours behind costs nothing.
     *
     * (A native background fetch was the obvious alternative and is the wrong one: it would mean a
     * second iCalendar parser and a second recurrence expander in Java — the thing that makes the
     * widget and the app disagree about what day something is on.) */
    async function widgetTick(maxAgeH){
      if(!PC.capPlugin || !PC.capPlugin('CalendarWidget', 'push')) return;   // not the packaged app
      let snap = null;
      try{ snap = await CalCache.read(); }catch(_){}
      if(snap && Array.isArray(snap.cals) && snap.cals.length && !S.cals.length){
        S.cals = snap.cals; S.items = snap.items || {}; S.rev++; S.cached = true;
      }
      // Draw from what is already here FIRST — a widget filled from a four-hour-old snapshot beats an
      // empty one while a request is in flight, and beats it entirely if the request fails.
      if(S.cals.length) await pushWidget();
      const age = snap && snap.at ? (Date.now() - snap.at) / 3600000 : Infinity;
      if(age >= (maxAgeH == null ? 6 : maxAgeH)) await load();   // load() pushes again at its end
    }

    window.PCCalendar = { render, reload: load, widgetTick };
  }
  init();
})();
