/* apptheme.js — Settings → Appearance: your own colours and wallpaper, for the WHOLE app.
 *
 * Asked 2026-10-21 (git issue "Concord Theme match with app theme"): a look made in Communities only
 * existed there, one colour never changed with it, and it was gone the moment you left. This is the
 * app-wide layer: on top of the preset theme (data-theme, client.css) it sets your accent, background
 * and text everywhere, gives any section its own colours, and puts an image behind it all at one of nine
 * positions or tiled. Concord's "Match app theme" follows it, and Concord's picker can hand its colours
 * here ("Use for the whole app").
 *
 * Loaded in <head> before the page paints, from a localStorage copy -- no flash of the preset first.
 * Synced in the account's `pcai:client-prefs` document as `appLook`; the newer `at` wins (blossom.js).
 * Everything is CSS custom properties in one <style id="pc-look">, at a specificity above every preset's
 * `:root[data-theme=…]`, so no rule in client.css has to know this exists.
 */
(function(){
  'use strict';
  const KEY = 'pc_look_v1.', LAST = 'pc_look_v1:last';
  // The sections a person can colour, and the views each one covers. A view not listed keeps your
  // app-wide colours. Order is the order Settings lists them in.
  const SECTIONS = [['social','Timeline & social'],['concord','Communities'],['messages','Messages'],['texts','Texts'],
    ['mail','Email'],['music','Music'],['notes','Notes'],['files','Files'],['ai','PosterChan AI'],['settings','Settings']];
  const VIEWS = {home:'social',global:'social',trending:'social',notifications:'social',profile:'social',thread:'social',
    hashtag:'social',search:'social',bookmarks:'social',lists:'social',concord:'concord',messages:'messages',texts:'texts',
    mail:'mail',music:'music',__music:'music',notes:'notes',blossom:'files',ai:'ai',settings:'settings'};
  // Vyram's list: the nine anchors and tile.
  const POSITIONS = [['left top','Top left'],['center top','Top'],['right top','Top right'],['left center','Left'],
    ['center center','Centre'],['right center','Right'],['left bottom','Bottom left'],['center bottom','Bottom'],
    ['right bottom','Bottom right'],['tile','Tile']];
  const POS_IDS = POSITIONS.map(p => p[0]), SEC_IDS = SECTIONS.map(s => s[0]);

  const hexOk = v => /^#[0-9a-f]{6}$/i.test(String(v||'')) ? String(v).toLowerCase() : '';
  // An image address that cannot break out of url("…"): http(s) or this site, no quotes/brackets/spaces.
  const urlOk = v => { const s = String(v||'').trim();
    return /^(https?:\/\/|\/)[^\s"'()<>\\]+$/i.test(s) && s.length <= 2000 ? s : ''; };
  const num = (v, lo, hi, d) => { const n = Number(v); return isFinite(n) ? Math.max(lo, Math.min(hi, n)) : d; };
  function clean(v){
    if(!v || typeof v !== 'object') return null;
    const sec = {};
    for(const id of SEC_IDS){ const s = v.sec && v.sec[id]; if(s && typeof s === 'object'){
      const a = hexOk(s.accent), b = hexOk(s.bg); if(a || b) sec[id] = { accent:a, bg:b }; } }
    const w = v.wall && typeof v.wall === 'object' ? v.wall : {};
    const url = urlOk(w.url);
    return { accent:hexOk(v.accent), bg:hexOk(v.bg), text:hexOk(v.text), sec,
      wall: url ? { url, pos: POS_IDS.includes(w.pos) ? w.pos : 'center center', size: w.size === 'original' ? 'original' : 'fill',
                    dim: num(w.dim, 0, 0.9, 0.35) } : null,
      at: Math.max(0, Number(v.at) || 0) };
  }
  const EMPTY = () => ({ accent:'', bg:'', text:'', sec:{}, wall:null, at:0 });
  const load = k => { try{ return clean(JSON.parse(localStorage.getItem(k) || 'null')); }catch(_){ return null; } };
  const store = (k, v) => { try{ localStorage.setItem(k, JSON.stringify(v)); }catch(_){} };

  const rgb = h => [1,3,5].map(i => parseInt(h.slice(i, i+2), 16));
  const hex = c => '#' + c.map(x => Math.round(Math.max(0, Math.min(255, x))).toString(16).padStart(2,'0')).join('');
  const mix = (a, b, t) => { const x = rgb(a), y = rgb(b); return hex(x.map((v, i) => v + (y[i] - v) * t)); };
  const lum = h => { const [r,g,b] = rgb(h).map(x => { x /= 255; return x <= .03928 ? x/12.92 : ((x+.055)/1.055) ** 2.4; });
    return .2126*r + .7152*g + .0722*b; };

  /* The custom properties one set of colours repaints. The accent family (client.css: --neon/--cyan carry it,
   * --accent-rgb feeds every translucent use), the surfaces and the text -- text chosen from the background's
   * own lightness unless you picked one, so a pale background cannot leave white text on it. */
  function vars(accent, bg, text){
    const v = {};
    if(accent){
      const c = rgb(accent).join(',');
      // --neon2 / --accent2-rgb too: the preset's second accent (magenta, orange) otherwise survives a custom accent in
      // every two-tone place -- Communities' section headings among them when it matches the app theme (2026-10-24).
      Object.assign(v, { '--neon':accent, '--cyan':accent, '--neon2':accent, '--accent-rgb':c, '--accent2-rgb':c, '--on-accent': lum(accent) > .36 ? '#05060b' : '#ffffff',
        '--glow':`0 0 18px -2px rgba(${c},.45)`, '--glow-cy':`0 0 18px -2px rgba(${c},.4)`, '--amb1':`rgba(${c},.06)`,
        '--line':`rgba(${c},.18)` });
    }
    if(bg){
      const dark = lum(bg) < .3, ink = text || (dark ? '#edeefa' : '#1b1f2e'), w = dark ? '#ffffff' : '#000000', b2 = mix(bg, w, .04);
      Object.assign(v, { '--bg':bg, '--bg2':b2, '--panel':`rgba(${rgb(mix(bg, w, .05)).join(',')},.78)`,
        '--panel2':`rgba(${rgb(mix(bg, w, .09)).join(',')},.88)`, '--text':ink, '--muted':mix(ink, bg, .38),
        '--amb2':accent ? `rgba(${rgb(accent).join(',')},.04)` : 'transparent', 'color-scheme': dark ? 'dark' : 'light' });
      if(!accent) v['--line'] = `rgba(${rgb(ink).join(',')},.14)`;
    } else if(text){ Object.assign(v, { '--text':text }); }
    return v;
  }
  const block = (sel, v) => { const k = Object.keys(v); return k.length ? `${sel}{${k.map(n => `${n}:${v[n]}`).join(';')}}` : ''; };
  // Three :root's: (0,3,0) beats any preset's `:root[data-theme="x"]` (0,2,0) however late client.css loads.
  const ROOT = ':root:root:root';

  /* The wallpaper IS the page background: body paints var(--canvas), and every sticky bar repaints var(--canvas)
   * with background-attachment:fixed so it stays invisible -- so setting --canvas puts the image behind the
   * whole shell and the bars still line up with it. The dim is a wash of the background colour over the image,
   * which is what keeps text readable on a busy photo. */
  function canvas(t, bg){
    const w = t.wall; if(!w) return null;
    const base = bg || 'var(--bg)', wash = bg ? `rgba(${rgb(bg).join(',')},${w.dim})` : `rgba(0,0,0,${w.dim})`;
    const tile = w.pos === 'tile';
    const layer = `url("${w.url}") ${tile ? 'left top' : w.pos} / ${tile || w.size === 'original' ? 'auto' : 'cover'} ${tile ? 'repeat' : 'no-repeat'}`;
    return `linear-gradient(${wash},${wash}),${layer},${base}`;
  }
  function css(t){
    let out = '';
    const top = vars(t.accent, t.bg, t.text), cv = canvas(t, t.bg);
    if(cv) top['--canvas'] = cv;
    out += block(ROOT, top);
    for(const id of SEC_IDS){
      const s = t.sec[id]; if(!s) continue;
      const v = vars(s.accent || '', s.bg || '', s.bg ? t.text : '');
      if(s.bg && t.wall) v['--canvas'] = canvas(t, s.bg);
      else if(s.bg) v['--canvas'] = `radial-gradient(ellipse 900px 600px at 50% -8%,var(--amb1) 0%,transparent 55%),${s.bg}`;
      out += block(`${ROOT}[data-pcsec="${id}"]`, v);
    }
    if(t.wall) out += `${ROOT} body{background-attachment:fixed}`;
    return out;
  }

  let owner = '', cur = load(LAST) || EMPTY();
  function paint(t){
    try{
      const d = typeof document !== 'undefined' && document;
      if(!d || !d.head && !d.documentElement) return;
      let el = d.getElementById('pc-look');
      const text = css(t);
      if(!text){ if(el) el.remove(); return; }
      if(!el){ el = d.createElement('style'); el.id = 'pc-look'; (d.head || d.documentElement).appendChild(el); }
      if(el.textContent !== text) el.textContent = text;
      // Keep it LAST in <head>: a stylesheet appended after it (concord.css loads itself) would otherwise win ties.
      if(el.parentNode && el !== el.parentNode.lastElementChild && el.parentNode === d.head) d.head.appendChild(el);
    }catch(_){}
  }
  function remember(t){ cur = t; store(LAST, t); if(owner) store(KEY + owner, t); paint(t); notify(); }
  const listeners = new Set();
  function notify(){ for(const f of listeners){ try{ f(get()); }catch(_){} } }
  function sync(){ try{ const p = window.__PC; if(p && p.saveAppLook && owner) p.saveAppLook({ ...cur }); }catch(_){} }
  function get(){ return JSON.parse(JSON.stringify(cur)); }
  /* An edit belongs to whoever is signed in NOW. Without this, a change made before the account's synced prefs
   * arrived was stored under nobody, and the restore's forOwner() then loaded that account's (older, often
   * empty) look over it -- the colours reverted the moment you left Settings. */
  function signedIn(){ try{ const p = window.__PC, me = p && p.me && p.me(); return (me && me.pubkey) || ''; }catch(_){ return ''; } }
  function set(patch, { save = true } = {}){
    if(!owner){ const pk = signedIn(); if(pk) owner = pk; }
    const next = clean({ ...cur, ...(patch || {}), at: Date.now() });
    if(!next) return get();
    remember(next); if(save) sync(); return get();
  }
  function setSection(id, patch, opts){
    if(!SEC_IDS.includes(id)) return get();
    const sec = { ...cur.sec, [id]: { ...(cur.sec[id] || {}), ...(patch || {}) } };
    return set({ sec }, opts);
  }
  /* Whose look this is. A different account on this device gets ITS look (or none), never the last one's. */
  function forOwner(pk){
    pk = String(pk || ''); if(!pk || pk === owner) return;
    owner = pk; remember(load(KEY + pk) || EMPTY());
  }
  /* From the account's synced prefs. Newer wins either way: an older remote copy is answered with ours. */
  function adopt(remote, pk){
    const r = clean(remote); pk = String(pk || '');
    if(!r || (pk && owner && pk !== owner)) return;
    if(pk && !owner) owner = pk;
    const mine = load(KEY + owner);
    if(!mine || r.at > mine.at) remember(r);
    else if(mine.at > r.at) sync();
  }
  // switchView calls this; the section's colours apply by attribute, so nothing re-renders.
  function section(view){
    try{ const s = VIEWS[String(view || '')] || ''; const r = document.documentElement;
      if(s) r.setAttribute('data-pcsec', s); else r.removeAttribute('data-pcsec'); return s; }catch(_){ return ''; }
  }
  const isCustom = t => !!(t && (t.accent || t.bg || t.text || t.wall || Object.keys(t.sec || {}).length));

  /* A #rrggbb BOX BESIDE EVERY COLOUR PICKER ("could you replace the color picker with one that allows you to input
   * #colorcode?", Vyram, git issue "Concord Theme match with app theme", 2026-10-25). Android's picker is a wheel --
   * matching an exact red by finger is next to impossible. The picker stays (it is the one people tap); the box drives
   * it: a valid code sets the picker and fires ITS `input` (a live preview) while typing and ITS `change` (the save) on
   * Enter / leaving the box, so every caller's existing handlers do the work and nothing is saved twice. `#f00` and
   * `ff0000` are accepted; anything else is marked and never applied. */
  function hexOf(v){
    v = String(v || '').trim().replace(/^#?/, '#');
    const m = v.match(/^#([0-9a-f])([0-9a-f])([0-9a-f])$/i);
    if(m) v = '#' + m[1] + m[1] + m[2] + m[2] + m[3] + m[3];
    return /^#[0-9a-f]{6}$/i.test(v) ? v.toLowerCase() : '';
  }
  function hexFields(root){
    if(!root) return;
    root.querySelectorAll('input[type=color]').forEach(c => {
      if(c._pcHex) return;
      const t = document.createElement('input');
      t.type = 'text'; t.className = 'input look-hex'; t.maxLength = 7; t.spellcheck = false;
      t.autocomplete = 'off'; t.setAttribute('autocapitalize', 'off'); t.placeholder = '#rrggbb';
      /* defaultValue too: Settings treats a field whose value differs from its default as an UNSAVED EDIT and will not
       * redraw over it (settings.js renderUserSettings hadEdits) -- a box born with only .value set froze Settings. */
      t.defaultValue = c.value; t.value = c.value;
      const lab = c.closest('label');
      t.setAttribute('aria-label', ((lab && lab.textContent.trim()) || 'Colour') + ' as a #rrggbb code');
      (lab || c).insertAdjacentElement('afterend', t);
      c._pcHex = t;
      t.addEventListener('input', () => {
        const v = hexOf(t.value);
        t.classList.toggle('bad', !v && t.value.trim() !== '');
        if(v && v !== c.value){ c.value = v; c.dispatchEvent(new Event('input', { bubbles: true })); }
      });
      t.addEventListener('change', () => {
        const v = hexOf(t.value);
        if(!v){ t.value = c.value; t.classList.remove('bad'); return; }
        c.value = v; t.value = v; c.dispatchEvent(new Event('change', { bubbles: true }));
      });
      t.addEventListener('keydown', e => { if(e.key === 'Enter'){ e.preventDefault(); t.dispatchEvent(new Event('change')); } });
      c.addEventListener('input', () => { if(document.activeElement !== t) t.value = c.value; });
    });
  }

  paint(cur);
  window.PCLook = { SECTIONS, POSITIONS, VIEWS, get, set, setSection, reset: () => set({ ...EMPTY() }), forOwner, adopt,
    section, isCustom: () => isCustom(cur), onChange: f => { listeners.add(f); return () => listeners.delete(f); },
    hexFields, hexOf, _css: css, _clean: clean };
})();
