/* PCIcal — the iCalendar reading the Calendar screen needs, with no DOM in it.
 *
 * DOM-free ON PURPOSE, the way joplin.js is: recurrence is the one part of a calendar that is all
 * edge cases (a monthly "last Friday", a yearly birthday that stopped in 2024, an occurrence someone
 * dragged to a different hour), and it can only be trusted if it is tested against real rules rather
 * than eyeballed in a month grid. tests/test_ical_recurrence.py runs THIS file under node.
 *
 * What it is not: a full RFC 5545 implementation. It reads what the grid must draw — when something
 * starts, whether it is all day, what to call it, and which days a rule lands on inside the window
 * being painted. Items are stored and exported VERBATIM, so anything not understood here survives
 * untouched in the file; it just doesn't get its own dot on the calendar.
 */
(function(root){
  'use strict';

  /* A continuation line begins with a space or tab (RFC 5545 §3.1). Everything else here reads
   * unfolded text: a long DTSTART;TZID=… wraps mid-parameter, and a folded property is invisible to
   * any line-by-line scan. */
  function unfold(text){
    return String(text || '').replace(/\r\n/g, '\n').replace(/\r/g, '\n')
                             .replace(/\n[ \t]/g, '');
  }

  /* Split a VCALENDAR into its top-level components, each as its own text block.
   *
   * Tracks the component NAME on every END and closes the outer one when its own END lands. Counting
   * every BEGIN:V… and closing on any END:V… leaves depth stuck above zero the moment an event holds
   * a VALARM — which is most of them — and then nothing is ever emitted at all. */
  function splitComponents(text){
    const lines = unfold(text).split('\n');
    const out = [];
    let cur = [], depth = 0, kind = '';
    for(const line of lines){
      const s = line.trim();
      if(s.startsWith('BEGIN:V') && s !== 'BEGIN:VCALENDAR'){
        if(depth === 0){ cur = []; kind = s.slice(6); }
        depth++; cur.push(line); continue;
      }
      if(depth){
        cur.push(line);
        if(s.startsWith('END:V') && s !== 'END:VCALENDAR'){
          depth--;
          if(depth === 0 && s.slice(4) === kind){ out.push(cur.join('\n')); cur = []; kind = ''; }
        }
      }
    }
    return out;
  }

  const nameOf = comp => {
    const m = unfold(comp).match(/^BEGIN:(V[A-Z]+)/m);
    return m ? m[1] : 'VEVENT';
  };

  /* Every line of a component as {name, params, value}. A property is NAME;PARAM=x:VALUE, and only
   * the part before the first colon holds parameters — a DESCRIPTION mentioning "TZID=" is prose. */
  function props(comp){
    const out = [];
    for(const line of unfold(comp).split('\n')){
      const i = line.indexOf(':');
      if(i < 0) continue;
      const head = line.slice(0, i), value = line.slice(i + 1);
      const bits = head.split(';');
      const params = {};
      for(const p of bits.slice(1)){
        const j = p.indexOf('=');
        if(j > 0) params[p.slice(0, j).toUpperCase()] = p.slice(j + 1).replace(/^"|"$/g, '');
      }
      out.push({ name: bits[0].toUpperCase(), params, value });
    }
    return out;
  }
  const first = (comp, name) => props(comp).find(p => p.name === name) || null;

  const unescape_ = s => String(s || '').replace(/\\n/gi, '\n').replace(/\\,/g, ',')
                                        .replace(/\\;/g, ';').replace(/\\\\/g, '\\');

  // ---- time ---------------------------------------------------------------------------------

  /* What offset is `tzid` at this instant? Asks Intl rather than reading the VTIMEZONE table: the
   * browser already ships the full tz database, and a hand-rolled reader of RRULE-based DST rules is
   * how an appointment lands an hour out twice a year. Throws for a TZID that is not an IANA name
   * (airline exports like "GMT-0600"), which the caller treats as floating local time. */
  function zoneOffset(tzid, utcMs){
    const dtf = new Intl.DateTimeFormat('en-US', {
      timeZone: tzid, hour12: false, year: 'numeric', month: '2-digit', day: '2-digit',
      hour: '2-digit', minute: '2-digit', second: '2-digit' });
    const p = {};
    for(const part of dtf.formatToParts(new Date(utcMs))) p[part.type] = part.value;
    const asUTC = Date.UTC(+p.year, +p.month - 1, +p.day, (+p.hour) % 24, +p.minute, +p.second);
    return asUTC - utcMs;
  }

  /* Wall-clock time in a named zone → the real instant. Two passes: the offset depends on the
   * instant, and the instant is what we are solving for. The second pass settles every case except
   * the hour that DST skips, which has no correct answer anyway. */
  function fromZoned(y, mo, d, h, mi, s, tzid){
    const wall = Date.UTC(y, mo - 1, d, h, mi, s);
    let guess = wall;
    for(let i = 0; i < 2; i++) guess = wall - zoneOffset(tzid, guess);
    return new Date(guess);
  }

  /* One DTSTART/DTEND/EXDATE value → a local Date, plus whether it is a date (all day).
   *
   * Three shapes, and they are NOT interchangeable: 20260810T140000Z is an instant; 20260810T140000
   * with a TZID is wall-clock in that zone; a bare 20260810 is a DATE, which must stay on that
   * calendar day in every timezone or an all-day event shows up a day early for half the world. */
  function parseDt(value, params){
    const v = String(value || '').trim();
    const tzid = (params && params.TZID) || '';
    const isDate = (params && String(params.VALUE).toUpperCase() === 'DATE') || /^\d{8}$/.test(v);
    let m = v.match(/^(\d{4})(\d{2})(\d{2})(?:T(\d{2})(\d{2})(\d{2})?(Z)?)?$/);
    if(!m) return null;
    const [, Y, M, D, h, mi, s, z] = m;
    const y = +Y, mo = +M, d = +D, hh = +(h || 0), mm = +(mi || 0), ss = +(s || 0);
    if(isDate || !h) return { date: new Date(y, mo - 1, d), allDay: true };
    if(z) return { date: new Date(Date.UTC(y, mo - 1, d, hh, mm, ss)), allDay: false };
    if(tzid){
      try { return { date: fromZoned(y, mo, d, hh, mm, ss, tzid), allDay: false }; }
      catch(_){ /* not an IANA zone — fall through to floating */ }
    }
    return { date: new Date(y, mo - 1, d, hh, mm, ss), allDay: false };   // floating: local time
  }

  const dayKey = d => `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}`
                    + `-${String(d.getDate()).padStart(2, '0')}`;
  const atMidnight = d => new Date(d.getFullYear(), d.getMonth(), d.getDate());
  const addDays = (d, n) => new Date(d.getFullYear(), d.getMonth(), d.getDate() + n,
                                     d.getHours(), d.getMinutes(), d.getSeconds());
  const WEEKDAYS = ['SU', 'MO', 'TU', 'WE', 'TH', 'FR', 'SA'];

  // ---- rules --------------------------------------------------------------------------------

  function parseRule(value){
    const r = {};
    for(const part of String(value || '').split(';')){
      const i = part.indexOf('=');
      if(i < 0) continue;
      r[part.slice(0, i).toUpperCase()] = part.slice(i + 1);
    }
    const out = {
      freq: (r.FREQ || '').toUpperCase(),
      interval: Math.max(1, parseInt(r.INTERVAL || '1', 10) || 1),
      count: r.COUNT ? parseInt(r.COUNT, 10) : 0,
      until: null,
      byday: [], bymonthday: [], bymonth: [],
      wkst: (r.WKST || 'MO').toUpperCase(),
    };
    if(r.UNTIL){
      const p = parseDt(r.UNTIL, {});
      if(p) out.until = p.date;
    }
    if(r.BYDAY){
      for(const t of r.BYDAY.split(',')){
        const m = t.trim().toUpperCase().match(/^([+-]?\d+)?(SU|MO|TU|WE|TH|FR|SA)$/);
        if(m) out.byday.push({ ord: m[1] ? parseInt(m[1], 10) : 0, day: WEEKDAYS.indexOf(m[2]) });
      }
    }
    if(r.BYMONTHDAY) out.bymonthday = r.BYMONTHDAY.split(',').map(n => parseInt(n, 10))
                                                  .filter(n => !isNaN(n));
    if(r.BYMONTH) out.bymonth = r.BYMONTH.split(',').map(n => parseInt(n, 10)).filter(n => !isNaN(n));
    return out;
  }

  /* Every day in [from, to) that this rule lands on, as local Dates carrying DTSTART's time.
   *
   * Bounded work per call: it jumps straight to the period containing `from` instead of stepping
   * from DTSTART. A weekly event created in 2011 is 780 iterations away from today, and a daily one
   * is 5500 — done for every rule on every repaint, that is what makes a calendar feel broken. The
   * exception is COUNT, which can only be known by counting from the beginning; that is bounded by
   * COUNT itself.
   */
  function expandRule(start, rule, from, to, limit){
    const out = [];
    if(!rule.freq) return out;
    const cap = limit || 2000;
    const stop = rule.until && rule.until < to ? rule.until : to;
    const hh = start.getHours(), mi = start.getMinutes(), ss = start.getSeconds();
    const withTime = d => new Date(d.getFullYear(), d.getMonth(), d.getDate(), hh, mi, ss);
    /* Compare the OCCURRENCE, not the day it falls on. `d` is local midnight and `start` carries
     * DTSTART's time, so a `d < start` test threw away the very first occurrence of every series —
     * the one on DTSTART's own day. */
    const emit = d => {
      const t = withTime(d);
      if(t < start) return;                       // never before the series begins
      if(rule.until && t > rule.until) return;
      if(rule.bymonth.length && !rule.bymonth.includes(d.getMonth() + 1)) return;
      if(t >= from && t < to) out.push(t);
    };

    // COUNT has to be walked from the start, so the occurrence budget is the count itself.
    const counted = rule.count > 0;
    let produced = 0;
    const emitCounted = d => {
      const t = withTime(d);
      if(t < start) return true;                  // still before the series — keep looking
      if(rule.until && t > rule.until) return false;
      if(rule.bymonth.length && !rule.bymonth.includes(d.getMonth() + 1)) return true;
      produced++;
      if(t >= from && t < to) out.push(t);
      return produced < rule.count;
    };

    const s0 = atMidnight(start);
    if(rule.freq === 'DAILY'){
      const step = rule.interval;
      let d = s0;
      if(!counted){
        const gap = Math.floor((atMidnight(from) - s0) / 86400000);
        if(gap > 0) d = addDays(s0, Math.floor(gap / step) * step);
      }
      for(let i = 0; i < cap && d < stop; i++, d = addDays(d, step)){
        if(counted){ if(!emitCounted(d)) break; } else emit(d);
      }
      return out;
    }

    if(rule.freq === 'WEEKLY'){
      const days = rule.byday.length ? rule.byday.map(b => b.day) : [s0.getDay()];
      const wkst = Math.max(0, WEEKDAYS.indexOf(rule.wkst));
      const weekStart = d => addDays(atMidnight(d), -(((d.getDay() - wkst) + 7) % 7));
      let w = weekStart(s0);
      if(!counted){
        const gap = Math.floor((weekStart(from) - w) / (7 * 86400000));
        if(gap > 0) w = addDays(w, Math.floor(gap / rule.interval) * rule.interval * 7);
      }
      for(let i = 0; i < cap && w < stop; i++, w = addDays(w, 7 * rule.interval)){
        let alive = true;
        for(let k = 0; k < 7 && alive; k++){
          const d = addDays(w, k);
          if(!days.includes(d.getDay())) continue;
          if(counted) alive = emitCounted(d); else emit(d);
        }
        if(!alive) break;
      }
      return out;
    }

    if(rule.freq === 'MONTHLY' || rule.freq === 'YEARLY'){
      const yearly = rule.freq === 'YEARLY';
      const stepMonths = (yearly ? 12 : 1) * rule.interval;
      let cursor = new Date(s0.getFullYear(), s0.getMonth(), 1);
      if(!counted){
        const gap = (from.getFullYear() - cursor.getFullYear()) * 12
                  + (from.getMonth() - cursor.getMonth());
        if(gap > 0) cursor = new Date(cursor.getFullYear(),
                                      cursor.getMonth() + Math.floor(gap / stepMonths) * stepMonths, 1);
      }
      for(let i = 0; i < cap && cursor < stop; i++,
          cursor = new Date(cursor.getFullYear(), cursor.getMonth() + stepMonths, 1)){
        const y = cursor.getFullYear(), mo = cursor.getMonth();
        const last = new Date(y, mo + 1, 0).getDate();
        let days = [];
        if(rule.byday.length){
          for(const b of rule.byday){
            const hits = [];
            for(let dd = 1; dd <= last; dd++){
              const d = new Date(y, mo, dd);
              if(d.getDay() === b.day) hits.push(d);
            }
            if(!b.ord) days.push(...hits);
            else if(b.ord > 0 && hits[b.ord - 1]) days.push(hits[b.ord - 1]);
            else if(b.ord < 0 && hits[hits.length + b.ord]) days.push(hits[hits.length + b.ord]);
          }
        }else if(rule.bymonthday.length){
          for(const n of rule.bymonthday){
            const dd = n > 0 ? n : last + 1 + n;
            if(dd >= 1 && dd <= last) days.push(new Date(y, mo, dd));
          }
        }else{
          // No BY* part: the rule repeats DTSTART's own day. A 31st in a 30-day month is skipped,
          // which is what RFC 5545 says and what every other client does.
          if(s0.getDate() <= last) days.push(new Date(y, mo, s0.getDate()));
        }
        days.sort((a, b) => a - b);
        let alive = true;
        for(const d of days){
          if(!alive) break;
          if(counted) alive = emitCounted(d); else emit(d);
        }
        if(!alive) break;
      }
      return out;
    }
    return out;
  }

  // ---- resources ----------------------------------------------------------------------------

  /* One stored item (a whole VCALENDAR) → what the grid needs.
   *
   * A resource can hold SEVERAL components: a recurring master plus the occurrences someone edited,
   * each carrying RECURRENCE-ID. They share a UID and are stored together, so reading only the first
   * VEVENT would show the master's title for an occurrence that was renamed, and reading them as
   * peers would draw a phantom event on the master's start date.
   */
  function parseResource(rec){
    const ics = (rec && rec.ics) || '';
    const comps = splitComponents(ics).filter(c => nameOf(c) !== 'VTIMEZONE');
    const parsed = comps.map(c => {
      const dtstart = first(c, 'DTSTART');
      const dt = dtstart ? parseDt(dtstart.value, dtstart.params) : null;
      const rid = first(c, 'RECURRENCE-ID');
      const rrule = first(c, 'RRULE');
      const exdates = [];
      for(const p of props(c)){
        if(p.name !== 'EXDATE') continue;
        for(const v of p.value.split(',')){
          const e = parseDt(v, p.params);
          if(e) exdates.push(dayKey(e.date));
        }
      }
      return {
        component: nameOf(c),
        start: dt ? dt.date : null,
        allDay: dt ? dt.allDay : false,
        title: unescape_((first(c, 'SUMMARY') || {}).value) || '(no title)',
        location: unescape_((first(c, 'LOCATION') || {}).value) || '',
        notes: unescape_((first(c, 'DESCRIPTION') || {}).value) || '',
        rrule: rrule ? parseRule(rrule.value) : null,
        exdates,
        recurrenceId: rid ? (parseDt(rid.value, rid.params) || {}).date || null : null,
      };
    });
    const master = parsed.find(p => !p.recurrenceId) || parsed[0] || null;
    return {
      uid: (rec && rec.uid) || '',
      cal: (rec && rec.cal) || '',
      component: (rec && rec.component) || (master && master.component) || 'VEVENT',
      master,
      overrides: parsed.filter(p => p.recurrenceId && p !== master),
    };
  }

  /* Every occurrence of one resource inside [from, to). A non-recurring item yields at most one. */
  function occurrences(res, from, to){
    const m = res && res.master;
    if(!m || !m.start) return [];
    const base = {
      uid: res.uid, cal: res.cal, component: res.component,
      title: m.title, location: m.location, notes: m.notes, allDay: m.allDay,
    };
    const moved = new Map();          // dayKey of the ORIGINAL slot -> the edited occurrence
    for(const o of res.overrides || []){
      if(o.recurrenceId) moved.set(dayKey(o.recurrenceId), o);
    }
    const out = [];
    const push = (start, from_) => out.push(Object.assign({}, base, {
      start, key: dayKey(start), recurring: !!from_,
    }));

    if(!m.rrule){
      if(m.start >= from && m.start < to) push(m.start, false);
    }else{
      for(const d of expandRule(m.start, m.rrule, from, to)){
        const k = dayKey(d);
        if(m.exdates.includes(k)) continue;         // cancelled occurrence
        const o = moved.get(k);
        if(o){
          // Edited: it may have been moved to another day entirely, in which case it belongs there
          // and not here. Drawn from the override's own fields, not the master's.
          if(o.start && o.start >= from && o.start < to){
            out.push(Object.assign({}, base, {
              title: o.title, location: o.location, notes: o.notes,
              allDay: o.allDay, start: o.start, key: dayKey(o.start), recurring: true,
            }));
          }
          continue;
        }
        push(d, true);
      }
    }
    // An occurrence moved OUT of an expanded window still has to appear where it landed.
    for(const o of res.overrides || []){
      if(!o.start || o.start < from || o.start >= to) continue;
      if(out.some(x => x.start.getTime() === o.start.getTime())) continue;
      out.push(Object.assign({}, base, {
        title: o.title, location: o.location, notes: o.notes, allDay: o.allDay,
        start: o.start, key: dayKey(o.start), recurring: true,
      }));
    }
    return out;
  }

  const API = { unfold, splitComponents, nameOf, props, parseDt, parseRule, expandRule,
                parseResource, occurrences, zoneOffset, fromZoned, dayKey, WEEKDAYS };
  root.PCIcal = API;
  if(typeof module !== 'undefined' && module.exports) module.exports = API;
})(typeof globalThis !== 'undefined' ? globalThis : this);
