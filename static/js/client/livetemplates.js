/* Go Live: the announcement post you can edit, and templates you can reuse.
 *
 * DOM-free, so tests/test_live_templates.py runs this exact file under node.
 *
 * THE POST. Going live used to publish a fixed "🔴 I’m live now: <title> / ▶ Watch: <link>" note
 * with no way to change a word of it. It is now a text box in the Go Live sheet. `{title}` and `{link}`
 * are filled in when you go live — the link does not exist before then (its address carries the
 * broadcast's start time) — and the `nostr:` address of the stream is ALWAYS kept on the end: it is
 * what every client embeds as the player and what makes the post the stream's announcement, so
 * deleting the link from the text must not turn the post into one that points at nothing.
 *
 * TEMPLATES follow the ACCOUNT, not the browser: one kind-30078 document, `d=pcai:livetemplates`,
 * NIP-44-sealed to the user's own key, the same shape as `pcai:automute` and `pcai:desktop` (and
 * registered with them in store.js `_isPinned` and app.js `_CARRY_D` — a private doc missing from
 * either one reads as "you never saved any", which is indistinguishable from a lost one). The write
 * rule is the replaceable-doc rule: nothing is published until a relay has ANSWERED a read, because
 * publishing the local copy over an unread account document replaces every template made on another
 * device. A device that could not read keeps its change locally and says so.
 */
(function(){
  'use strict';
  const D = 'pcai:livetemplates';
  const MAX = 20;
  const DEFAULT_POST = '🔴 I’m live now: {title}\n\n▶ Watch: {link}';

  const str = (v, n) => String(v == null ? '' : v).slice(0, n);

  function clean(t){
    if(!t || typeof t !== 'object') return null;
    const name = str(t.name, 60).trim();
    if(!name) return null;
    const tags = Array.isArray(t.tags) ? t.tags.map(x => str(x, 32)).filter(Boolean).slice(0, 10)
                                       : str(t.tags, 400).split(/[\s,]+/).filter(Boolean).slice(0, 10);
    return { name, title: str(t.title, 120), summary: str(t.summary, 1000), tags,
             lang: /^[a-z]{2}$/.test(String(t.lang || '')) ? t.lang : '',
             cw: t.cw == null ? null : str(t.cw, 200), cover: /^https?:\/\//i.test(String(t.cover || '')) ? str(t.cover, 2000) : '',
             post: str(t.post, 4000), t: Number.isFinite(+t.t) ? +t.t : 0 };
  }
  // Newest first, one per name (case-insensitive), at most MAX.
  function normalize(list){
    const out = [], seen = new Set();
    (Array.isArray(list) ? list : []).map(clean).filter(Boolean)
      .sort((a, b) => b.t - a.t)
      .forEach(t => { const k = t.name.toLowerCase(); if(!seen.has(k)){ seen.add(k); out.push(t); } });
    return out.slice(0, MAX);
  }
  function upsert(list, tpl, now){
    const t = clean(Object.assign({}, tpl, { t: now || Date.now() }));
    if(!t) return normalize(list);
    return normalize([t].concat((list || []).filter(x => x && String(x.name).toLowerCase() !== t.name.toLowerCase())));
  }
  function remove(list, name){
    const k = String(name || '').toLowerCase();
    return normalize((list || []).filter(x => x && String(x.name).toLowerCase() !== k));
  }
  // The text that is published. A blank box is the default post, never an empty note.
  function renderPost(text, v){
    v = v || {};
    const title = str(v.title, 120) || 'Live stream';
    let s = String(text == null ? '' : text).trim() || DEFAULT_POST;
    s = s.split('{title}').join(title).split('{link}').join(v.link || '');
    if(v.naddr && !s.includes('nostr:' + v.naddr)) s = s.replace(/\s+$/, '') + '\n\nnostr:' + v.naddr;
    return s.slice(0, 8000);
  }

  /* The account copy. `deps` = { owner, query(filters)->events|null, publish(kind,content,tags)->{ok},
   * enc(pk,text), dec(pk,text), local: {get(), set(list)} }. Every dependency is injected so the
   * replaceable-doc rule can be tested against a fake relay. */
  function store(deps){
    let read = false;                  // a relay ANSWERED: only now may the document be replaced
    let loading = null;
    const giveUp = ms => new Promise(r => setTimeout(() => r(undefined), ms));
    function list(){ try{ return normalize(deps.local.get()); }catch(_){ return []; } }
    async function load(){
      if(loading) return loading;
      loading = (async () => {
        let evs;
        try{ evs = await Promise.race([deps.query([{ authors: [deps.owner], kinds: [30078], '#d': [D], limit: 1 }]), giveUp(9000)]); }
        catch(_){ return list(); }     // could not ask — never "there is nothing"
        if(!Array.isArray(evs)) return list();
        read = true;
        const ev = evs.sort((a, b) => b.created_at - a.created_at)[0];
        if(!ev || !ev.content) return list();
        let doc = null;
        try{ doc = JSON.parse(await deps.dec(deps.owner, ev.content)); }catch(_){ return list(); }
        // Merge by name, newest edit wins: a template made on this device while it could not read
        // survives the first successful read instead of being replaced by the account's copy.
        const merged = normalize(((doc && doc.templates) || []).concat(list()));
        try{ deps.local.set(merged); }catch(_){}
        return merged;
      })().finally(() => { loading = null; });
      return loading;
    }
    // save(mutate): mutate(list) -> new list. A FUNCTION, not a list, so the change is applied to the
    // account's copy once it has been read -- a list computed beforehand would, merged with the account
    // copy, bring a template deleted here straight back. {ok, synced, list}: ok = kept on this device,
    // synced = on the account.
    async function save(mutate){
      if(!read) await load();
      const l = normalize(mutate(list()));
      try{ deps.local.set(l); }catch(_){}
      if(!read) return { ok: true, synced: false, list: l };
      try{
        const ct = await deps.enc(deps.owner, JSON.stringify({ v: 1, templates: l }));
        const r = await deps.publish(30078, ct, [['d', D]]);
        return { ok: true, synced: !!(r && r.ok), list: l };
      }catch(_){ return { ok: true, synced: false, list: l }; }
    }
    return { load, save, list, isRead: () => read };
  }

  const api = { D, MAX, DEFAULT_POST, clean, normalize, upsert, remove, renderPost, store };
  if(typeof window !== 'undefined') window.PCLiveTpl = api;
  if(typeof module !== 'undefined') module.exports = api;
})();
