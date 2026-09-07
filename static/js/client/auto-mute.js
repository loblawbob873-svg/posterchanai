/* Reciprocal filtering is separate from the user's NIP-51 manual mute list.
 * Only a verified, newer public list can change an automatic mute. */
(function (root) {
  'use strict';
  const KEY = /^[0-9a-f]{64}$/;
  const MAX_AUTHORS = 5000;
  const PAGE = 500;
  const validKey = value => typeof value === 'string' && KEY.test(value);
  const newer = (a, b) => !b || a.created_at > b.created_at ||
    (a.created_at === b.created_at && a.id < b.id);

  function create(options) {
    const owner = options.owner;
    if (!validKey(owner)) throw new Error('An account is required');
    const now = options.now || (() => Date.now());
    let generation = 0, destroyed = false, running = null, runningGeneration = -1;
    let state = { owner, enabled: false, records: {}, ignored: [], lastChecked: 0, summary: null };
    const cached = options.read();
    if (cached && cached.owner === owner) {
      state.enabled = cached.enabled === true;
      state.lastChecked = Number.isFinite(cached.lastChecked) ? cached.lastChecked : 0;
      state.summary = cached.summary || null;
      for (const [pk, record] of Object.entries(cached.records || {}).slice(0, MAX_AUTHORS)) {
        if (validKey(pk) && pk !== owner && record && validKey(record.id) &&
            Number.isSafeInteger(record.created_at) && record.created_at >= 0 &&
            typeof record.muted === 'boolean') state.records[pk] = { ...record };
      }
      state.ignored = [...new Set((Array.isArray(cached.ignored) ? cached.ignored : [])
        .filter(pk => validKey(pk) && pk !== owner))].slice(0, MAX_AUTHORS);
    }
    let activeKeys;
    function index() {
      const ignored = new Set(state.ignored);
      activeKeys = new Set(Object.keys(state.records).filter(pk => state.records[pk].muted && !ignored.has(pk)));
    }
    index();
    const current = () => !destroyed && options.isCurrent(owner);
    const list = () => current() && state.enabled ? [...activeKeys] : [];
    const snapshot = () => JSON.parse(JSON.stringify(state));
    function commit(next) {
      if (!current()) throw new Error('Account changed');
      options.write(next); // A failed durable write must not silently change filtering.
      state = next;
      index();
      if (options.onChange) { try { options.onChange(snapshot()); } catch (_) {} }
    }
    function setEnabled(enabled) {
      generation++;
      commit({ ...snapshot(), enabled: enabled === true });
    }
    function exclude(pk) {
      if (!validKey(pk) || !current() || !state.records[pk]?.muted) return;
      generation++;
      const next = snapshot();
      if (!next.ignored.includes(pk)) next.ignored.push(pk);
      commit(next);
    }
    async function verified(event) {
      return event && event.kind === 10000 && validKey(event.pubkey) && event.pubkey !== owner &&
        validKey(event.id) && Number.isSafeInteger(event.created_at) && event.created_at >= 0 &&
        event.created_at <= Math.floor(now() / 1000) + 300 && Array.isArray(event.tags) &&
        await options.verify(event);
    }
    async function perform() {
      const version = generation;
      const active = () => current() && state.enabled && generation === version;
      const requireActive = () => { if (!active()) throw new Error('Update cancelled'); };
      const completeQuery = async filters => {
        requireActive();
        const rows = await options.query(filters);
        requireActive();
        if (!Array.isArray(rows) || rows.complete !== true) throw new Error('Relay check incomplete; previous automatic mutes kept');
        return rows;
      };
      requireActive();
      const authors = new Set(Object.keys(state.records));
      let until, limited = false;
      // Find positive candidates, then query their latest lists WITHOUT #p. An unmute
      // removes that tag, so a #p-only query can never prove that someone unmuted us.
      for (let page = 0; page < 10; page++) {
        const filter = { kinds: [10000], '#p': [owner], limit: PAGE };
        if (until !== undefined) filter.until = until;
        const rows = await completeQuery([filter]);
        for (const event of rows) {
          requireActive();
          if (await verified(event) && event.tags.some(t => t[0] === 'p' && t[1] === owner)) {
            if (authors.size < MAX_AUTHORS || authors.has(event.pubkey)) authors.add(event.pubkey);
            else limited = true;
          }
        }
        if (rows.length < PAGE) break;
        const oldest = Math.min(...rows.map(e => e.created_at).filter(Number.isSafeInteger));
        if (!Number.isFinite(oldest) || oldest === until || page === 9) { limited = true; break; }
        until = oldest; // Inclusive boundary avoids dropping equal-timestamp events.
      }
      const latest = new Map();
      const keys = [...authors];
      for (let offset = 0; offset < keys.length; offset += 64) {
        const batch = keys.slice(offset, offset + 64), allowed = new Set(batch);
        const rows = await completeQuery([{ kinds: [10000], authors: batch, limit: batch.length }]);
        for (const event of rows) {
          requireActive();
          if (allowed.has(event.pubkey) && await verified(event) && newer(event, latest.get(event.pubkey)))
            latest.set(event.pubkey, event);
        }
      }
      requireActive();
      const before = new Set(list()), next = snapshot(), ignored = new Set(next.ignored);
      for (const [pk, event] of latest) {
        const previous = next.records[pk];
        if (previous && !newer(event, previous) && event.id !== previous.id) continue;
        const muted = event.tags.some(t => t[0] === 'p' && t[1] === owner);
        next.records[pk] = { id: event.id, created_at: event.created_at, muted };
        // A manual exception applies to the current mute. Once they unmute, a future
        // fresh mute can be discovered normally. Missing events never clear exceptions.
        if (!muted) ignored.delete(pk);
      }
      next.ignored = [...ignored];
      const after = new Set(Object.keys(next.records).filter(pk => next.records[pk].muted && !ignored.has(pk)));
      next.lastChecked = now();
      next.summary = { checked: latest.size, added: [...after].filter(pk => !before.has(pk)).length,
        removed: [...before].filter(pk => !after.has(pk)).length, count: after.size, limited };
      commit(next);
      return { ok: true, ...next.summary };
    }
    function update() {
      if (!current() || !state.enabled) return Promise.resolve({ ok: false, error: 'Auto-mute is off' });
      if (running && runningGeneration === generation) return running;
      runningGeneration = generation;
      const job = perform().catch(error => ({ ok: false, error: error.message || 'Could not update automatic mutes' }))
        .finally(() => { if (running === job) running = null; });
      running = job;
      return job;
    }
    return { owner, setEnabled, exclude, update, snapshot, list,
      has: pk => current() && state.enabled && activeKeys.has(pk),
      destroy() { destroyed = true; generation++; } };
  }
  root.PCAutoMute = { create };
})(typeof window !== 'undefined' ? window : globalThis);
