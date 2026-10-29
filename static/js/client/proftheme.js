/* PROFILE THEMES AND PROFILE MUSIC -- the MySpace page ("Vyram wants profiles to be themeable like myspace", 2026-10-29).
 * Loaded by profile.js when a profile opens; not part of the page's own scripts.
 *
 * A THEME IS DITTO'S KIND 16767, read and written as Ditto and Armada do (measured on relay.ditto.pub: 200 of them, 123
 * from Ditto, 73 from Armada): `c <#hex> background|text|primary`, `f <family> <url> body|title`,
 * `bg "url <https>" "mode cover|tile|contain" "m <mime>"`, plus `title`/`alt`/`client`. So a theme made here shows
 * on those clients and theirs show here. The MUSIC is the profile's own kind-0 `fields` song (profile.js
 * _profileMusicFields); what is new is that it starts when the profile opens.
 *
 * ANYBODY CAN PUBLISH ONE, so nothing from the event is written into CSS text: colours must be #rrggbb and are set
 * through style.setProperty; image and font addresses must parse as https URLs (the URL parser percent-encodes the
 * quotes and backslashes that could close a url("...")); fonts load only from the font hosts the themes in the wild
 * use -- a theme that could name any server would make every visitor's browser call it; and text that would be
 * unreadable on its background is replaced by black or white. */
(function(root){
  'use strict';
  const KIND = 16767;
  const HEX = /^#[0-9a-f]{6}$/i;
  const FONT_HOSTS = ['cdn.jsdelivr.net', 'fonts.gstatic.com'];
  const ROLES = ['background', 'text', 'primary'];
  /* PER-ELEMENT COLOURS (Vyram's marked-up profile, 2026-10-29: "color changeable" on the buttons, the name, the
   * address lines, the bio, the stats, the tabs and the post icons; "border or no border, color changeable" on the
   * banner, the avatar and quoted posts). Extra `c` roles in the same kind 16767 -- other clients ignore a role they
   * do not know, so the three above still travel to Ditto and Armada and these stay PosterChan's -- plus
   * ["border", "none"|"show"]. Each maps to one CSS variable, read only by the rules in CSS below. */
  const PARTS = { name: 'the name', about: 'the bio', meta: 'addresses and dates', buttons: 'buttons',
                  stats: 'posts / following / followers', tabs: 'tabs', icons: 'post icons', border: 'borders' };

  function https(u){
    try{ const x = new URL(String(u || '')); return x.protocol === 'https:' ? x.href : ''; }catch(_){ return ''; }
  }
  function fontUrl(u){
    const h = https(u); if(!h) return '';
    try{ return FONT_HOSTS.includes(new URL(h).hostname) ? h : ''; }catch(_){ return ''; }
  }
  const family = s => String(s || '').replace(/[^A-Za-z0-9 _-]/g, '').trim().slice(0, 48);

  /* The event, as a theme -- or null. Values from a tag like ["bg","url x","mode cover"] are "key value" pairs. */
  function parse(ev){
    if(!ev || ev.kind !== KIND || !Array.isArray(ev.tags)) return null;
    const t = { colors: {}, fonts: {}, bg: null, title: '' };
    for(const tag of ev.tags){
      if(!Array.isArray(tag)) continue;
      if(tag[0] === 'c' && HEX.test(String(tag[1] || '')) && ROLES.includes(tag[2])) t.colors[tag[2]] = String(tag[1]).toLowerCase();
      else if(tag[0] === 'c' && HEX.test(String(tag[1] || '')) && PARTS[tag[2]]) (t.parts = t.parts || {})[tag[2]] = String(tag[1]).toLowerCase();
      else if(tag[0] === 'border' && (tag[1] === 'none' || tag[1] === 'show')) t.border = tag[1];
      else if(tag[0] === 'f' && (tag[3] === 'body' || tag[3] === 'title')){
        const name = family(tag[1]), url = fontUrl(tag[2]);
        if(name) t.fonts[tag[3]] = { name, url };
      }else if(tag[0] === 'bg'){
        const kv = {};
        for(const p of tag.slice(1)){ const s = String(p || ''), i = s.indexOf(' '); if(i > 0) kv[s.slice(0, i)] = s.slice(i + 1); }
        const url = https(kv.url);
        if(url) t.bg = { url, mode: ['cover', 'tile', 'contain'].includes(kv.mode) ? kv.mode : 'cover', m: String(kv.m || '') };
      }else if(tag[0] === 'title') t.title = String(tag[1] || '').slice(0, 80);
    }
    if(!t.colors.background || !t.colors.text) return t.bg ? t : null;   // a background alone is still a theme
    return t;
  }

  // WCAG relative luminance and contrast, so a theme never makes a profile unreadable.
  function lum(hex){
    const n = parseInt(hex.slice(1), 16), c = [n >> 16 & 255, n >> 8 & 255, n & 255].map(v => {
      v /= 255; return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4); });
    return 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2];
  }
  function contrast(a, b){ const x = lum(a), y = lum(b); return (Math.max(x, y) + 0.05) / (Math.min(x, y) + 0.05); }
  function mix(a, b, w){   // a*(1-w) + b*w, both #rrggbb
    const A = parseInt(a.slice(1), 16), B = parseInt(b.slice(1), 16);
    const ch = s => Math.round((A >> s & 255) * (1 - w) + (B >> s & 255) * w);
    return '#' + ((1 << 24) | (ch(16) << 16) | (ch(8) << 8) | ch(0)).toString(16).slice(1);
  }

  /* The app's own colour variables, re-derived from the theme's three colours -- set on the profile's container,
   * they restyle everything inside it (cards, lines, muted text, buttons) and nothing outside it. */
  function vars(t){
    if(!t || !t.colors.background || !t.colors.text) return {};
    const bg = t.colors.background;
    let text = t.colors.text;
    if(contrast(text, bg) < 4.5) text = contrast('#000000', bg) >= contrast('#ffffff', bg) ? '#000000' : '#ffffff';
    /* The theme's own primary colour is kept exactly where it FILLS (buttons, borders, highlights -- --accent, with
     * its text colour chosen against it); where it is TEXT on the background (links: --cyan/--neon) it is blended
     * toward the text colour just until it reads (3:1). Armada's "Megumin" red on navy is 2.6:1 -- replacing it
     * outright threw away the one colour the theme is about. */
    const primary = t.colors.primary || text;
    // A colour used as TEXT on the background, blended toward the text colour just until it reads (3:1).
    const readable = c => { let x = c; for(let w = 0.1; contrast(x, bg) < 3 && w <= 1; w += 0.1) x = mix(c, text, w); return x; };
    const ink = readable(primary);
    const parts = {};
    for(const k in (t.parts || {})) parts['--pt-' + k] = k === 'border' ? t.parts[k] : readable(t.parts[k]);
    if(t.border === 'none') parts['--pt-bw'] = '0px';
    else if(t.border === 'show' || (t.parts && t.parts.border)) parts['--pt-bw'] = '2px';
    return Object.assign(parts, {
      '--bg': bg, '--bg2': mix(bg, text, 0.04), '--panel': mix(bg, text, 0.07), '--panel2': mix(bg, text, 0.11),
      '--line': mix(bg, text, 0.2), '--text': text, '--muted': mix(text, bg, 0.35),
      '--accent': primary, '--magenta': primary, '--cyan': ink, '--neon': ink,
      '--on-accent': contrast('#000000', primary) >= contrast('#ffffff', primary) ? '#000000' : '#ffffff',
      '--pt-bg': bg,
    });
  }

  /* Tags for the kind 16767 this client publishes -- the shape Ditto and Armada write. */
  function tags(t, client){
    const out = [];
    for(const r of ROLES) if(t.colors && HEX.test(t.colors[r] || '')) out.push(['c', t.colors[r].toLowerCase(), r]);
    for(const r in PARTS) if(t.parts && HEX.test(t.parts[r] || '')) out.push(['c', t.parts[r].toLowerCase(), r]);
    if(t.border === 'none' || t.border === 'show') out.push(['border', t.border]);
    for(const r of ['body', 'title']){ const f = t.fonts && t.fonts[r]; if(f && family(f.name) && fontUrl(f.url)) out.push(['f', family(f.name), fontUrl(f.url), r]); }
    if(t.bg && https(t.bg.url)){
      const b = ['bg', 'url ' + https(t.bg.url), 'mode ' + (['cover', 'tile', 'contain'].includes(t.bg.mode) ? t.bg.mode : 'cover')];
      if(t.bg.m) b.push('m ' + String(t.bg.m).replace(/[^a-z0-9./+-]/gi, ''));
      out.push(b);
    }
    out.push(['alt', 'Active profile theme'], ['title', String(t.title || 'Profile theme').slice(0, 80)], ['client', client || 'PosterChan']);
    return out;
  }

  // ---- the page (everything below touches the DOM) ----------------------------------------------------------------
  const CSS = '#feed[data-ptheme]{background-color:var(--pt-bg);color:var(--text);font-family:var(--pt-body,inherit);'
    + 'background-image:var(--pt-img,none);background-size:var(--pt-size,cover);background-repeat:var(--pt-repeat,no-repeat);background-position:center top}'
    + '@media (hover:hover){#feed[data-ptheme]{background-attachment:fixed}}'
    + '#feed[data-ptheme] h1,#feed[data-ptheme] h2,#feed[data-ptheme] h3,#feed[data-ptheme] .prof-tab{font-family:var(--pt-title,var(--pt-body,inherit))}'
    + '#feed[data-ptheme] .prof,#feed[data-ptheme] .note,#feed[data-ptheme] .prof-tabs,#feed[data-ptheme] .prof-track{background-color:color-mix(in srgb,var(--panel) 90%,transparent)}'
    + '#feed[data-ptheme] .prof .pbody h2,#feed[data-ptheme] #prof-list .name{color:var(--pt-name,var(--text))}'
    + '#feed[data-ptheme] .prof .about{color:var(--pt-about,var(--text))}'
    + '#feed[data-ptheme] .prof .prof-nip05s,#feed[data-ptheme] .prof .npubrow,#feed[data-ptheme] .prof .ln-addr,#feed[data-ptheme] .prof .prof-joined{color:var(--pt-meta,var(--muted))}'
    + '#feed[data-ptheme] .prof .prof-actions .btn{color:var(--pt-buttons,var(--accent));border-color:var(--pt-buttons,var(--line))}'
    + '#feed[data-ptheme] .prof .follow-stats,#feed[data-ptheme] .prof .statbtn{color:var(--pt-stats,var(--muted))}'
    + '#feed[data-ptheme] .prof-tabs .prof-tab{color:var(--pt-tabs,var(--muted))}'
    + '#feed[data-ptheme] #prof-list .acts .act{color:var(--pt-icons,var(--muted))}'
    + '#feed[data-pborder] .prof .banner,#feed[data-pborder] .prof .pav,#feed[data-pborder] #prof-list .quoted{border-style:solid;'
    + 'border-color:var(--pt-border,var(--line));border-width:var(--pt-bw)}'
    + '.pt-song{position:sticky;top:6px;z-index:5;display:flex;align-items:center;gap:8px;width:max-content;max-width:100%;margin:6px auto;padding:6px 12px;'
    + 'border:1px solid var(--line);border-radius:999px;background:var(--panel2);color:var(--text);font-size:13px}'
    + '.pt-song button{all:unset;cursor:pointer;padding:2px 8px;border-radius:999px;background:var(--accent);color:var(--on-accent);font-weight:600}'
    + '.pt-song span{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}';
  let live = null;   // the one profile that is themed / playing now

  function css(){
    if(document.getElementById('pc-ptheme-css')) return;
    const s = document.createElement('style'); s.id = 'pc-ptheme-css'; s.textContent = CSS; document.head.appendChild(s);
  }
  function loadFont(role, f, host){
    if(!f || !f.url || typeof FontFace !== 'function') return;
    const fam = 'pt-' + role + '-' + f.name.replace(/\s+/g, '-');
    try{
      const face = new FontFace(fam, 'url("' + f.url + '")');
      face.load().then(ff => { document.fonts.add(ff); if(live && live.host === host) host.style.setProperty('--pt-' + role, '"' + fam + '", ' + 'var(--sans, sans-serif)'); }, () => {});
    }catch(_){}
  }

  /* Leaving the profile -- by any path: the theme comes off and the song stops (an <audio> taken out of the page
   * keeps playing). Watched, not called: every way out of a profile replaces #feed's content. */
  function leave(){
    const l = live; live = null;
    if(!l) return;
    try{ l.obs && l.obs.disconnect(); }catch(_){}
    try{ if(l.audio){ l.audio.pause(); } }catch(_){}
    const h = l.host;
    if(h){
      delete h.dataset.ptheme; delete h.dataset.pborder;
      for(const k of l.keys) h.style.removeProperty(k);
    }
  }

  /* Theme the profile under `marker` (an element of that profile's markup inside `host`, i.e. #feed) and, when
   * allowed, start its song. opts: { theme: bool, autoplay: bool, busy: () => bool } -- the viewer's two switches
   * and "is something else playing". */
  function apply(host, marker, ev, opts){
    opts = opts || {};
    if(!host || !marker || !marker.isConnected) return null;
    // The SAME profile again (a newer theme arrived from the relays): repaint, and leave its song alone.
    if(live && live.marker === marker && live.host === host){ style(host, opts.theme === false ? null : parse(ev)); return parse(ev); }
    leave();
    live = { host, marker, keys: [], audio: null, obs: null };
    const t = style(host, opts.theme === false ? null : parse(ev));
    // Gone from the page = left. A MutationObserver on the host sees every replacement of its content.
    try{
      live.obs = new MutationObserver(() => { if(live && !live.marker.isConnected) leave(); });
      live.obs.observe(host, { childList: true });
    }catch(_){}
    song(host, marker, opts);
    return t;
  }
  function style(host, t){
    for(const k of live.keys) host.style.removeProperty(k);
    delete host.dataset.ptheme; delete host.dataset.pborder;
    const keys = live.keys = [];
    if(t){
      css();
      const v = vars(t);
      for(const k in v){ host.style.setProperty(k, v[k]); keys.push(k); }
      if(v['--pt-bw']) host.dataset.pborder = '1';   // borders only when the theme says so
      if(t.bg){
        host.style.setProperty('--pt-img', 'url("' + t.bg.url + '")');
        host.style.setProperty('--pt-size', t.bg.mode === 'tile' ? 'auto' : t.bg.mode);
        host.style.setProperty('--pt-repeat', t.bg.mode === 'tile' ? 'repeat' : 'no-repeat');
        keys.push('--pt-img', '--pt-size', '--pt-repeat');
        if(!v['--pt-bg']){ host.style.setProperty('--pt-bg', 'transparent'); keys.push('--pt-bg'); }
      }
      keys.push('--pt-body', '--pt-title');
      host.dataset.ptheme = '1';
      loadFont('body', t.fonts.body, host); loadFont('title', t.fonts.title || t.fonts.body, host);
    }
    return t;
  }

  /* The profile's song: the first track of its music block. Not over something already playing, never with the
   * switch off; a browser that refuses to start sound without a tap gets a one-tap chip instead of silence. */
  function song(host, marker, opts){
    const a = host.querySelector('.prof-track audio');
    if(!a || !live) return;
    live.audio = a;
    const busy = (() => { try{ return !!(opts.busy && opts.busy()); }catch(_){ return false; } })();
    if(opts.autoplay === false || busy) return;
    css();
    const name = (a.closest('.prof-track') && a.closest('.prof-track').querySelector('.prof-track-name') || {}).textContent || 'Profile song';
    const chip = document.createElement('div');
    chip.className = 'pt-song'; chip.setAttribute('role', 'status');
    const label = document.createElement('span'); label.textContent = '♪ ' + name;
    const btn = document.createElement('button'); btn.type = 'button';
    chip.append(label, btn);
    const paint = () => { btn.textContent = a.paused ? 'Play' : 'Mute'; btn.setAttribute('aria-label', a.paused ? 'Play the profile song' : 'Stop the profile song'); };
    btn.onclick = () => { if(a.paused){ a.play().catch(() => {}); } else { a.pause(); } };
    a.addEventListener('play', paint); a.addEventListener('pause', paint);
    marker.parentNode && marker.parentNode.insertBefore(chip, marker);
    paint();
    try{ a.preload = 'auto'; }catch(_){}
    const p = a.play();
    if(p && p.catch) p.then(paint, paint);
  }


  // ---- the editor (your own profile: the Theme button) -----------------------------------------------------------
  const FONTS = ['Inter', 'Silkscreen', 'Comfortaa', 'Press Start 2P', 'Pacifico', 'Lobster', 'Bangers', 'VT323', 'Orbitron',
                 'Comic Neue', 'Permanent Marker', 'Creepster', 'Special Elite', 'Righteous'];
  const fontFile = n => 'https://cdn.jsdelivr.net/fontsource/fonts/' + n.toLowerCase().replace(/ /g, '-') + '@latest/latin-400-normal.woff2';
  const PRESETS = {
    'Neon night': { background: '#0b0716', text: '#efe9ff', primary: '#ff4fd8' },
    'Megumin': { background: '#1c253f', text: '#f6f0ea', primary: '#c03838' },
    'Paper': { background: '#f6f1e7', text: '#2b2118', primary: '#b4472f' },
    'Toxic': { background: '#0c120a', text: '#e8ffe0', primary: '#5dff3b' },
    'Bubblegum': { background: '#ffebf5', text: '#21121a', primary: '#f53d99' },
  };
  const ED_CSS = '.pt-ed .pt-row{display:grid;grid-template-columns:1fr auto auto;align-items:center;gap:8px;margin:6px 0}'
    + '.pt-ed input[type=color]{width:44px;height:30px;padding:0;border:1px solid var(--line);border-radius:8px;background:none}'
    + '.pt-ed .pt-presets{display:flex;flex-wrap:wrap;gap:6px;margin:8px 0}.pt-ed .pt-off input[type=color]{opacity:.35}'
    + '.pt-prev{margin:10px 0;border-radius:12px;overflow:hidden;border:1px solid var(--line);background:var(--bg);color:var(--text);'
    + 'background-image:var(--pt-img,none);background-size:var(--pt-size,cover);background-repeat:var(--pt-repeat,no-repeat);font-family:var(--pt-body,inherit)}'
    + '.pt-prev .pv-band{height:52px;border-bottom:var(--pt-bw,0) solid var(--pt-border,transparent);background:color-mix(in srgb,var(--accent) 35%,transparent)}'
    + '.pt-prev .pv-body{padding:10px 12px;background:color-mix(in srgb,var(--panel) 88%,transparent)}'
    + '.pt-prev b{color:var(--pt-name,var(--text));font-family:var(--pt-title,inherit)}.pt-prev .pv-meta{color:var(--pt-meta,var(--muted));font-size:12px}'
    + '.pt-prev .pv-btn{display:inline-block;margin-top:6px;padding:3px 10px;border-radius:999px;border:1px solid var(--pt-buttons,var(--line));color:var(--pt-buttons,var(--accent))}'
    + '.pt-prev a{color:var(--cyan)}';

  /* o: { current (the newest kind 16767 or null), modal, closeModal, toast, enc, uploadBlob, publish, onSaved } */
  function openEditor(o){
    const enc = o.enc;
    const cur = parse(o.current) || { colors: {}, fonts: {}, bg: null, parts: {}, border: '' };
    const st = { colors: Object.assign({ background: '#10131d', text: '#e5f0f5', primary: '#ff4fd8' }, cur.colors),
                 parts: Object.assign({}, cur.parts || {}), border: cur.border || '', font: (cur.fonts.body && cur.fonts.body.name) || '',
                 bg: cur.bg ? Object.assign({}, cur.bg) : null };
    const row = (key, label, val, on) => `<div class="pt-row${on ? '' : ' pt-off'}" data-k="${key}"><span>${enc(label)}</span>`
      + `<input type="color" aria-label="${enc(label)}" value="${enc(val)}">` + (on === null ? '<span></span>'
      : `<button type="button" class="btn btn-ghost small pt-clear" title="Use the page's colour">${on ? 'Reset' : 'Set'}</button>`) + '</div>';
    o.modal(`<div class="pt-ed"><h3 class="cmp-hd">Profile theme<button class="modal-x" id="pt-close" aria-label="Close">&#215;</button></h3>
      <p class="muted small">Your page's colours, font, background and borders -- like MySpace. Colours, font and background also show on Ditto and Armada; the rest is PosterChan's.</p>
      <div class="pt-presets">${Object.keys(PRESETS).map(n => `<button type="button" class="btn btn-ghost small" data-preset="${enc(n)}">${enc(n)}</button>`).join('')}</div>
      ${row('background', 'Background', st.colors.background, null)}${row('text', 'Text', st.colors.text, null)}${row('primary', 'Accent', st.colors.primary, null)}
      <details><summary>More colours</summary>${Object.keys(PARTS).map(k => row('p:' + k, PARTS[k][0].toUpperCase() + PARTS[k].slice(1), st.parts[k] || '#888888', !!st.parts[k])).join('')}
        <label class="fld">Borders<select class="input" id="pt-border"><option value="">As the app draws them</option><option value="show">Show, in the border colour</option><option value="none">None</option></select></label></details>
      <label class="fld">Font<select class="input" id="pt-font"><option value="">The app's font</option>${FONTS.map(f => `<option${f === st.font ? ' selected' : ''}>${enc(f)}</option>`).join('')}</select></label>
      <div class="fld"><span>Background picture</span><div class="row"><button type="button" class="btn btn-ghost small" id="pt-up">Upload</button>
        <input class="input" id="pt-bgurl" placeholder="https://…" value="${enc(st.bg ? st.bg.url : '')}">
        <select class="input" id="pt-mode"><option value="cover">Fill</option><option value="tile">Tile</option><option value="contain">Fit</option></select></div>
        <input type="file" id="pt-file" accept="image/*" hidden></div>
      <div class="pt-prev" id="pt-prev" aria-label="Preview"><div class="pv-band"></div><div class="pv-body"><b>Your name</b><div class="pv-meta">you@poster.place · joined</div>
        <div>Thanks for the add. <a>a link</a></div><span class="pv-btn">Follow</span></div></div>
      <div class="row"><button type="button" class="btn btn-ghost" id="pt-remove">Remove theme</button><span style="flex:1"></span><button type="button" class="btn btn-cyan" id="pt-save">Save theme</button></div></div>`);
    if(!document.getElementById('pc-pted-css')){ const c = document.createElement('style'); c.id = 'pc-pted-css'; c.textContent = ED_CSS; document.head.appendChild(c); }
    const box = document.querySelector('.pt-ed'); if(!box) return;
    const $ = sel => box.querySelector(sel);
    $('#pt-border').value = st.border; if(st.bg) $('#pt-mode').value = st.bg.mode;
    const theme = () => {
      const url = https($('#pt-bgurl').value.trim());
      return { colors: st.colors, parts: st.parts, border: st.border, title: 'Profile theme',
               fonts: st.font ? { body: { name: st.font, url: fontFile(st.font) }, title: { name: st.font, url: fontFile(st.font) } } : {},
               bg: url ? { url, mode: $('#pt-mode').value, m: (st.bg && st.bg.url === url && st.bg.m) || '' } : null };
    };
    const preview = () => {
      const t = parse({ kind: KIND, tags: tags(theme()) }), pv = $('#pt-prev'), v = vars(t);
      pv.removeAttribute('style');
      for(const k in v) pv.style.setProperty(k, v[k]);
      if(t && t.bg){ pv.style.setProperty('--pt-img', 'url("' + t.bg.url + '")'); pv.style.setProperty('--pt-size', t.bg.mode === 'tile' ? 'auto' : t.bg.mode); pv.style.setProperty('--pt-repeat', t.bg.mode === 'tile' ? 'repeat' : 'no-repeat'); }
      if(st.font && typeof FontFace === 'function'){ const fam = 'pt-ed-' + st.font.replace(/\s+/g, '-');
        new FontFace(fam, 'url("' + fontFile(st.font) + '")').load().then(f => { document.fonts.add(f); pv.style.setProperty('--pt-body', '"' + fam + '"'); pv.style.setProperty('--pt-title', '"' + fam + '"'); }, () => {}); }
    };
    box.querySelectorAll('.pt-row').forEach(r => {
      const k = r.dataset.k, inp = r.querySelector('input'), clr = r.querySelector('.pt-clear');
      inp.addEventListener('input', () => { if(k.startsWith('p:')){ st.parts[k.slice(2)] = inp.value; r.classList.remove('pt-off'); if(clr) clr.textContent = 'Reset'; } else st.colors[k] = inp.value; preview(); });
      if(clr) clr.onclick = () => { const p = k.slice(2);
        if(st.parts[p]){ delete st.parts[p]; r.classList.add('pt-off'); clr.textContent = 'Set'; } else { st.parts[p] = inp.value; r.classList.remove('pt-off'); clr.textContent = 'Reset'; }
        preview(); };
    });
    box.querySelectorAll('[data-preset]').forEach(b => b.onclick = () => {
      Object.assign(st.colors, PRESETS[b.dataset.preset]);
      for(const k of ['background', 'text', 'primary']){ const i = box.querySelector(`.pt-row[data-k="${k}"] input`); if(i) i.value = st.colors[k]; }
      preview(); });
    $('#pt-border').onchange = () => { st.border = $('#pt-border').value; preview(); };
    $('#pt-font').onchange = () => { st.font = $('#pt-font').value; preview(); };
    $('#pt-bgurl').oninput = preview; $('#pt-mode').onchange = preview;
    $('#pt-up').onclick = () => $('#pt-file').click();
    $('#pt-file').onchange = async e => {
      const f = e.target.files && e.target.files[0]; if(!f) return;
      $('#pt-up').disabled = true; $('#pt-up').textContent = 'Uploading…';
      try{ const u = await o.uploadBlob(f); if(!u) throw new Error('no address came back');
        $('#pt-bgurl').value = u; st.bg = { url: u, mode: $('#pt-mode').value, m: f.type || '' }; preview();
      }catch(err){ o.toast('The picture did not upload: ' + ((err && err.message) || err)); }
      $('#pt-up').disabled = false; $('#pt-up').textContent = 'Upload';
    };
    const send = async (t, done) => {
      try{ const r = await o.publish(16767, '', t ? tags(t) : [['alt', 'Active profile theme'], ['title', 'No theme'], ['client', 'PosterChan']]);
        // publish() answers { ev, ok, msg }: only an ACCEPTED theme closes the editor and repaints the page.
        if(!r || !r.ok || !r.ev){ if(r && r.msg) o.toast(r.msg); return; }
        o.closeModal(); o.toast(done); if(o.onSaved) o.onSaved(r.ev);
      }catch(err){ o.toast('Not saved: ' + ((err && err.message) || err)); }
    };
    $('#pt-save').onclick = () => send(theme(), 'Theme saved');
    // A blank theme, not a deletion request: a replaceable event that every relay and client takes as "no theme".
    $('#pt-remove').onclick = () => send(null, 'Theme removed');
    $('#pt-close').onclick = () => o.closeModal();
    preview();
  }

  root.PCProfileTheme = { KIND, PARTS, FONTS, parse, vars, tags, contrast, apply, leave, openEditor, current: () => live };
})(typeof window !== 'undefined' ? window : globalThis);
