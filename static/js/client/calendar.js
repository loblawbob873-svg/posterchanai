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
    month:null,                // Date, first of the shown month
    sel:null,                  // 'YYYY-MM-DD' selected day
    loading:false, error:'',
    sync:null,                 // /config payload for the phone panel
    scroll:0,
  };

  function init(){
    const PC = window.__PC;
    if(!PC){ return setTimeout(init, 50); }
    const { $, $$, enc, toast, modal, closeModal, authFetch, ensureAiSession, uiConfirm } = PC;

    const inView = () => window.__PC.VIEW === 'calendar';
    const scroller = () => document.body.classList.contains('embed')
      ? (document.scrollingElement || document.documentElement) : $('#feed');

    // ---- server ------------------------------------------------------------------------------
    async function api(path, opts){
      try{ await ensureAiSession(); }catch(_){}
      const r = await authFetch(path, opts);
      let body = null;
      try{ body = await r.json(); }catch(_){}
      if(!r.ok) throw new Error((body && (body.detail || body.error)) || ('HTTP ' + r.status));
      return body || {};
    }
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
      const L = ['BEGIN:VCALENDAR','VERSION:2.0','PRODID:-//PosterChan//Calendar//EN','BEGIN:VEVENT',
                 'UID:' + uid, 'DTSTAMP:' + now];
      if(ev.allDay){
        const s = new Date(ev.date + 'T00:00:00');
        const e = new Date(s.getTime() + 86400000);          // DTEND is exclusive for a date value
        L.push('DTSTART;VALUE=DATE:' + icsDate(s), 'DTEND;VALUE=DATE:' + icsDate(e));
      }else{
        const s = new Date(`${ev.date}T${ev.start || '09:00'}:00`);
        const e = new Date(`${ev.date}T${ev.end || ev.start || '10:00'}:00`);
        L.push('DTSTART:' + icsUtc(s), 'DTEND:' + icsUtc(e.getTime() > s.getTime() ? e
                                                          : new Date(s.getTime() + 3600000)));
      }
      L.push('SUMMARY:' + icsText(ev.title || '(no title)'));
      if(ev.location) L.push('LOCATION:' + icsText(ev.location));
      if(ev.notes) L.push('DESCRIPTION:' + icsText(ev.notes));
      L.push('END:VEVENT','END:VCALENDAR');
      return { uid, ics: L.join('\r\n') + '\r\n' };
    }

    /* Parse just enough of a stored item to place it on the grid: when it starts, whether it is all
     * day, and what to call it. NOT a full iCalendar parser, deliberately — the file is stored and
     * exported verbatim, so anything this does not understand survives untouched; it only has to be
     * right about the fields the month view draws. */
    function parseItem(rec){
      const text = (rec.ics || '').replace(/\r\n[ \t]/g, '').replace(/\r\n/g, '\n');   // unfold
      const get = re => { const m = text.match(re); return m ? m[1].trim() : ''; };
      const dtRaw = get(/^DTSTART(?:;[^:\n]*)?:(.+)$/m);
      const allDay = /^DTSTART;[^:\n]*VALUE=DATE(?:;|:)/m.test(text) || /^\d{8}$/.test(dtRaw);
      let start = null;
      if(/^\d{8}T\d{6}Z$/.test(dtRaw)){
        start = new Date(Date.UTC(+dtRaw.slice(0,4), +dtRaw.slice(4,6)-1, +dtRaw.slice(6,8),
                                  +dtRaw.slice(9,11), +dtRaw.slice(11,13), +dtRaw.slice(13,15)));
      }else if(/^\d{8}T\d{6}$/.test(dtRaw)){         // floating local time, as some clients write
        start = new Date(+dtRaw.slice(0,4), +dtRaw.slice(4,6)-1, +dtRaw.slice(6,8),
                         +dtRaw.slice(9,11), +dtRaw.slice(11,13));
      }else if(/^\d{8}$/.test(dtRaw)){
        start = new Date(+dtRaw.slice(0,4), +dtRaw.slice(4,6)-1, +dtRaw.slice(6,8));
      }
      const unesc = s => s.replace(/\\n/g,'\n').replace(/\\,/g,',').replace(/\\;/g,';').replace(/\\\\/g,'\\');
      return {
        uid: rec.uid,
        cal: rec.cal,
        title: unesc(get(/^SUMMARY:(.*)$/m)) || '(no title)',
        location: unesc(get(/^LOCATION:(.*)$/m)),
        notes: unesc(get(/^DESCRIPTION:(.*)$/m)),
        allDay, start,
        key: start ? ymd(start) : '',
        time: (start && !allDay) ? `${pad(start.getHours())}:${pad(start.getMinutes())}` : '',
        todo: (rec.component || 'VEVENT').toUpperCase() === 'VTODO',
      };
    }

    const colorOf = cal => {
      const c = S.cals.find(c => c.id === cal);
      if(c && c.color) return c.color;
      const i = Math.max(0, S.cals.findIndex(c => c.id === cal));
      return PALETTE[i % PALETTE.length];
    };
    function eventsFor(key){
      const out = [];
      for(const cid of Object.keys(S.items)){
        for(const rec of (S.items[cid] || [])){
          const p = parseItem(rec);
          if(p.key === key) out.push(p);
        }
      }
      out.sort((a,b) => (a.allDay === b.allDay) ? (a.time || '').localeCompare(b.time || '')
                                                : (a.allDay ? -1 : 1));
      return out;
    }

    // ---- load --------------------------------------------------------------------------------
    async function load(){
      S.loading = true; S.error = '';
      paint();
      try{
        S.sync = await api('/api/calendar/config');
        S.enabled = !!S.sync.enabled;
        if(!S.enabled){ S.loading = false; paint(); return; }
        const r = await api('/api/calendar/calendars');
        S.cals = r.calendars || [];
        if(!S.cal || !S.cals.some(c => c.id === S.cal)) S.cal = (S.cals[0] || {}).id || '';
        const items = {};
        for(const c of S.cals){
          try{ items[c.id] = (await api('/api/calendar/items?cal=' + encodeURIComponent(c.id))).items || []; }
          catch(_){ items[c.id] = []; }
        }
        S.items = items;
      }catch(e){
        // 404 is the server being off, which is a state to explain rather than an error to report.
        S.enabled = /off on this node/i.test((e && e.message) || '') ? false : S.enabled;
        if(S.enabled !== false) S.error = (e && e.message) || 'could not load your calendars';
      }finally{
        S.loading = false; S.ready = true; paint();
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
        const dots = evs.slice(0, 4).map(e =>
          `<i class="cal-dot" style="background:${enc(colorOf(e.cal))}"></i>`).join('');
        cells += `<button class="cal-day${other?' other':''}${key===today?' today':''}${key===S.sel?' sel':''}"
                          data-key="${key}">
            <span class="cal-num">${d.getDate()}</span>
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
          <button class="btn btn-ghost small cal-edit" data-uid="${enc(e.uid)}" data-cal="${enc(e.cal)}">Edit</button>
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
      const feed = $('#feed'); if(!feed) return;
      if(S.loading && !S.ready){ feed.innerHTML = '<div class="cal-wrap"><div class="spinner"></div></div>'; return; }
      if(S.enabled === false || S.error){ feed.innerHTML = `<div class="cal-wrap">${offScreen()}</div>`; return; }
      feed.innerHTML = `<div class="cal-wrap">${head()}${grid()}${dayPanel()}</div>`;
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
      const pick = $('#cal-pick', root);
      if(pick) pick.onchange = ()=>{ S.cal = pick.value; paint(); };
      $$('.cal-day', root).forEach(b => b.onclick = ()=>{ S.sel = b.dataset.key; paint(); });
      $$('.cal-edit', root).forEach(b => b.onclick = (e)=>{
        e.stopPropagation();
        const rec = (S.items[b.dataset.cal] || []).find(r => r.uid === b.dataset.uid);
        if(rec) editEvent(Object.assign(parseItem(rec), { cal: b.dataset.cal }));
      });
      $$('.cal-ev', root).forEach(el => el.onclick = ()=>{
        const rec = (S.items[el.dataset.cal] || []).find(r => r.uid === el.dataset.uid);
        if(rec) editEvent(Object.assign(parseItem(rec), { cal: el.dataset.cal }));
      });
      const sc = $('#feed');
      if(sc) sc.onscroll = ()=>{ if(inView()) S.scroll = sc.scrollTop; };
    }

    // ---- event editor ------------------------------------------------------------------------
    function editEvent(ev){
      const isNew = !ev;
      const key = (ev && ev.key) || S.sel || todayKey();
      const cal = (ev && ev.cal) || S.cal || (S.cals[0] || {}).id || '';
      if(!cal){ makeCalendar(); return; }          // no calendar yet — make one first
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
          };
          if(!body.title){ toast('give it a title'); return; }
          const built = buildIcs(body);
          try{
            await jput('/api/calendar/items', { cal, uid: built.uid, ics: built.ics });
            closeModal(); toast('saved'); await load();
          }catch(err){ toast('could not save: ' + ((err && err.message) || 'error')); }
        };
        const del = $('#cev-del', root);
        if(del) del.onclick = async ()=>{
          if(!(await uiConfirm('Delete this event?'))) return;
          try{
            await api(`/api/calendar/items?cal=${encodeURIComponent(cal)}&uid=${encodeURIComponent(e.uid)}`,
                      { method:'DELETE' });
            closeModal(); toast('deleted'); await load();
          }catch(err){ toast('could not delete: ' + ((err && err.message) || 'error')); }
        };
      });
    }

    // ---- calendars, import/export, phone ------------------------------------------------------
    function openMenu(){
      modal(`<h3>Calendars</h3>
        <div class="cal-list">${S.cals.map(c => `<div class="cal-row">
            <i class="cal-dot" style="background:${enc(colorOf(c.id))}"></i>
            <span class="cal-name">${enc(c.displayname || c.id)}</span>
            <a class="btn btn-ghost small" href="/api/calendar/export?cal=${encodeURIComponent(c.id)}"
               download><svg class="ic b-ic" aria-hidden="true"><use href="#i-download"></use></svg>Export</a>
            <button class="btn btn-ghost small cal-del" data-id="${enc(c.id)}">Delete</button>
          </div>`).join('') || '<div class="empty">No calendars yet.</div>'}</div>
        <div class="row" style="margin-top:14px;flex-wrap:wrap;gap:8px">
          <button class="btn btn-cyan small" id="cal-add"><svg class="ic b-ic" aria-hidden="true"><use href="#i-plus"></use></svg>New calendar</button>
          <button class="btn btn-ghost small" id="cal-import"><svg class="ic b-ic" aria-hidden="true"><use href="#i-upload"></use></svg>Import .ics</button>
          <button class="btn btn-ghost small" id="cal-phone"><svg class="ic b-ic" aria-hidden="true"><use href="#i-android"></use></svg>Sync to a device</button>
        </div>
        <input type="file" id="cal-file" accept=".ics,text/calendar" hidden>`, root => {
        $('#cal-add', root).onclick = ()=>{ closeModal(); makeCalendar(); };
        $('#cal-phone', root).onclick = ()=>{ closeModal(); phonePanel(); };
        $('#cal-import', root).onclick = ()=> $('#cal-file', root).click();
        $('#cal-file', root).onchange = async (e)=>{
          const f = e.target.files && e.target.files[0]; if(!f) return;
          const fd = new FormData(); fd.append('file', f);
          try{
            await ensureAiSession();
            const target = S.cal || f.name.replace(/\.ics$/i, '');
            const r = await authFetch('/api/calendar/import?cal=' + encodeURIComponent(target),
                                      { method:'POST', body: fd }).then(r => r.json());
            closeModal();
            toast(`imported ${r.imported || 0} event${(r.imported||0)===1?'':'s'}`
                  + (r.skipped ? ` (${r.skipped} skipped)` : ''));
            await load();
          }catch(err){ toast('import failed: ' + ((err && err.message) || 'error')); }
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
            if(cp) cp.onclick = ()=>{ try{ navigator.clipboard.writeText(r.password); toast('copied'); }catch(_){ } };
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
      if(!S.month) S.month = firstOf(new Date());
      if(!S.sel) S.sel = todayKey();
      paint();
      // Repaint from state first (instant on a return trip), then refresh in the background.
      load();
    }

    window.PCCalendar = { render, reload: load };
  }
  init();
})();
