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
  /* BORDERS ARE A STYLE AND A LIST OF ELEMENTS (Vyram, 2026-10-30: "Borders setting is suppose to change the borders as in
   * from a solid line to -------- dashes lines, triangles, circles, & squares. Borders of each element should be
   * selectable to hide or show." -- and "The banner does not need a border around it"). Carried in the same tag, so
   * Ditto/Armada still read show|none: ["border", "show"|"none", <style>, "<el>,<el>"]. A theme from before this (just
   * ["border","show"]) keeps what it always meant: solid, on the banner, the avatar and quoted posts. */
  const BSTYLES = ['solid', 'dashed', 'dotted', 'double', 'squares', 'triangles'];
  const BELS = { banner: 'banner', avatar: 'avatar', buttons: 'buttons', notes: 'posts', quoted: 'quoted posts', tabs: 'tabs' };
  const BOLD = ['banner', 'avatar', 'quoted'];
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
      else if(tag[0] === 'border' && (tag[1] === 'none' || tag[1] === 'show')){
        t.border = tag[1];
        t.bstyle = BSTYLES.includes(tag[2]) ? tag[2] : 'solid';
        const els = String(tag[3] || '').split(',').map(x => x.trim()).filter(x => BELS[x]);
        t.bon = tag[3] === undefined ? BOLD.slice() : els;
      }
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

  /* A pattern border, drawn here from a validated #rrggbb (never text from the event): a 24px tile whose 8px edges repeat
   * as squares or triangles pointing outward, used as border-image with an 8-px slice. */
  function borderImage(kind, colour){
    const c = HEX.test(colour || '') ? colour : '#888888';
    const shapes = [];
    for(let y = 0; y < 3; y++) for(let x = 0; x < 3; x++){
      if(x === 1 && y === 1) continue;
      const X = x * 8, Y = y * 8;
      if(kind === 'squares') shapes.push(`<rect x='${X + 1}' y='${Y + 1}' width='6' height='6'/>`);
      else{
        // pointing away from the middle: up on the top row, down on the bottom, outward at the sides
        const pts = y === 0 ? [[X+1,Y+7],[X+4,Y+1],[X+7,Y+7]] : y === 2 ? [[X+1,Y+1],[X+4,Y+7],[X+7,Y+1]]
          : x === 0 ? [[X+7,Y+1],[X+1,Y+4],[X+7,Y+7]] : [[X+1,Y+1],[X+7,Y+4],[X+1,Y+7]];
        shapes.push(`<polygon points='${pts.map(p => p.join(',')).join(' ')}'/>`);
      }
    }
    const svg = `<svg xmlns='http://www.w3.org/2000/svg' width='24' height='24' fill='${c}'>${shapes.join('')}</svg>`;
    return 'url("data:image/svg+xml,' + encodeURIComponent(svg) + '")';
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
    else if(t.border === 'show' || (t.parts && t.parts.border)){
      const st = BSTYLES.includes(t.bstyle) ? t.bstyle : 'solid';
      parts['--pt-bw'] = (st === 'squares' || st === 'triangles') ? '8px' : (st === 'double' ? '4px' : '2px');
      parts['--pt-bs'] = (st === 'squares' || st === 'triangles') ? 'solid' : st;
      if(st === 'squares' || st === 'triangles') parts['--pt-bimg'] = borderImage(st, (t.parts && t.parts.border) || primary);
    }
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
    if(t.border === 'none') out.push(['border', 'none']);
    else if(t.border === 'show'){
      const els = (Array.isArray(t.bon) ? t.bon : BOLD).filter(x => BELS[x]);
      out.push(['border', 'show', BSTYLES.includes(t.bstyle) ? t.bstyle : 'solid', els.join(',')]);
    }
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
  // Each element's border only where the theme lists it (data-pb-<el> on #feed); squares/triangles are a border-image.
  const BSEL = { banner: '.prof .banner', avatar: '.prof .pav', buttons: '.prof .prof-actions .btn', notes: '#prof-list .note',
                 quoted: '#prof-list .quoted', tabs: '.prof-tabs .prof-tab' };
  const BORDER_CSS = Object.keys(BSEL).map(k => `#feed[data-pb-${k}] ${BSEL[k]}`).join(',')
    + '{border-style:var(--pt-bs,solid);border-color:var(--pt-border,var(--line));border-width:var(--pt-bw)}'
    + Object.keys(BSEL).map(k => `#feed[data-pb-${k}][data-pbimg] ${BSEL[k]}`).join(',')
    + '{border-image:var(--pt-bimg) 8 round}';
  const CSS = '#feed[data-ptheme]{background-color:var(--pt-bg);color:var(--text);font-family:var(--pt-body,inherit);'
    + 'background-image:var(--pt-img,none);background-size:var(--pt-size,cover);background-repeat:var(--pt-repeat,no-repeat);background-position:center top}'
    + '@media (hover:hover){#feed[data-ptheme]{background-attachment:fixed}}'
    + '#feed[data-ptheme] h1,#feed[data-ptheme] h2,#feed[data-ptheme] h3,#feed[data-ptheme] .prof-tab{font-family:var(--pt-title,var(--pt-body,inherit))}'
    + '#feed[data-ptheme] .prof,#feed[data-ptheme] .note,#feed[data-ptheme] .prof-tabs,#feed[data-ptheme] .prof-track{background-color:color-mix(in srgb,var(--panel) 90%,transparent)}'
    + '#feed[data-ptheme] .prof .pbody h2,#feed[data-ptheme] #prof-list .name{color:var(--pt-name,var(--text))}'
    + '#feed[data-ptheme] .prof .about{color:var(--pt-about,var(--text))}'
    + '#feed[data-ptheme] .prof .prof-nip05s,#feed[data-ptheme] .prof .npubrow,#feed[data-ptheme] .prof .ln-addr,#feed[data-ptheme] .prof .prof-joined{color:var(--pt-meta,var(--muted))}'
    + '#feed[data-ptheme] .prof .prof-actions .btn{color:var(--pt-buttons,var(--accent));border-color:var(--pt-buttons,var(--line));'
    // Edit is btn-cyan (a gradient): every action button is the same surface on a themed profile ("Whats up with the edit button?")
    + 'background:var(--panel2);background-image:none}'
    + '#feed[data-ptheme] .prof .follow-stats,#feed[data-ptheme] .prof .statbtn{color:var(--pt-stats,var(--muted))}'
    + '#feed[data-ptheme] .prof-tabs .prof-tab{color:var(--pt-tabs,var(--muted))}'
    + '#feed[data-ptheme] #prof-list .acts .act{color:var(--pt-icons,var(--muted))}'
    + BORDER_CSS
    + '.pt-song{position:static;display:flex;align-items:center;gap:8px;width:max-content;max-width:100%;margin:6px auto;padding:6px 12px;'
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
    delete host.dataset.ptheme; delete host.dataset.pborder; delete host.dataset.pbimg;
    for(const k in BELS) host.removeAttribute('data-pb-' + k);
    const keys = live.keys = [];
    if(t){
      css();
      const v = vars(t);
      for(const k in v){ host.style.setProperty(k, v[k]); keys.push(k); }
      if(v['--pt-bw']){
        host.dataset.pborder = '1';   // borders only when the theme says so, and only on the elements it lists
        const on = t.border === 'show' ? (Array.isArray(t.bon) ? t.bon : BOLD) : (t.border === 'none' ? [] : BOLD);
        for(const k of on) if(BELS[k]) host.setAttribute('data-pb-' + k, '1');
        if(v['--pt-bimg']) host.dataset.pbimg = '1';
      }
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
  // "colours" for English (GB) and the rest of the Commonwealth, "colors" for English (US) ("Maybe if your language is set
  // to English(GB), we English(US) colors", 2026-10-30). The browser's own language decides.
  const COLOR = (() => { try{ return /^en-US\b/i.test((navigator.languages && navigator.languages[0]) || navigator.language || '') ? 'color' : 'colour'; }catch(_){ return 'colour'; } })();
  const fontFile = n => 'https://cdn.jsdelivr.net/fontsource/fonts/' + n.toLowerCase().replace(/ /g, '-') + '@latest/latin-400-normal.woff2';
  /* THEMES TO START FROM ("maybe add more profile themes that people can choose", 2026-10-29). A whole look each --
   * colours, the text font, a display font for the name and headings (so a decorative face never has to carry the
   * bio), per-part colours and borders -- and nothing else: the background picture stays the person's own. Every
   * font is one of FONTS; every text colour reads on its background (tests/client/test_profile_theme_rules.py runs
   * each one through vars() and requires its text to survive unchanged). */
  const PRESETS = {
    'Neon night': { colors: { background: '#0b0716', text: '#efe9ff', primary: '#ff4fd8' }, title: 'Orbitron', parts: { name: '#ff4fd8', tabs: '#3ce8ff' } },
    'Megumin': { colors: { background: '#1c253f', text: '#f6f0ea', primary: '#c03838' }, title: 'Bangers', parts: { name: '#ff6b6b' } },
    'Paper': { colors: { background: '#f6f1e7', text: '#2b2118', primary: '#b4472f' }, font: 'Special Elite' },
    'Toxic': { colors: { background: '#0c120a', text: '#e8ffe0', primary: '#5dff3b' }, title: 'Creepster', parts: { name: '#5dff3b' } },
    'Bubblegum': { colors: { background: '#ffebf5', text: '#21121a', primary: '#f53d99' }, font: 'Comfortaa', title: 'Pacifico', parts: { name: '#c2187a' } },
    'MySpace 2006': { colors: { background: '#e5ecf9', text: '#1d2a44', primary: '#ff6600' }, title: 'Righteous', border: 'show',
      parts: { name: '#1f3f80', tabs: '#1f3f80', border: '#6699cc', buttons: '#cc5200' } },
    'GeoCities': { colors: { background: '#000080', text: '#ffff66', primary: '#00ffff' }, font: 'Comic Neue', border: 'show',
      parts: { name: '#ff66ff', border: '#ff00ff', stats: '#66ff66' } },
    'Terminal': { colors: { background: '#050805', text: '#39ff7a', primary: '#39ff7a' }, font: 'VT323', border: 'show',
      parts: { border: '#1f8f45', meta: '#7dffa8' } },
    'Arcade': { colors: { background: '#120458', text: '#ffffff', primary: '#ffe600' }, title: 'Press Start 2P',
      parts: { name: '#ff2bd6', stats: '#ffe600', tabs: '#00f0ff' } },
    'Pixel': { colors: { background: '#2d2a32', text: '#f2efe9', primary: '#ffb000' }, font: 'Silkscreen', parts: { name: '#ffb000' } },
    'Vaporwave': { colors: { background: '#2b1055', text: '#ffe6ff', primary: '#ff71ce' }, title: 'Righteous',
      parts: { name: '#01cdfe', tabs: '#05ffa1', meta: '#b967ff' } },
    'Synthwave': { colors: { background: '#1a1033', text: '#f9f1ff', primary: '#ff8c00' }, title: 'Orbitron',
      parts: { name: '#ff2a6d', meta: '#05d9e8', icons: '#ff8c00' } },
    'Haunted': { colors: { background: '#0d0b0b', text: '#ddd5c7', primary: '#b3121f' }, font: 'Special Elite', title: 'Creepster', border: 'show',
      parts: { name: '#e0312f', border: '#4a0d12' } },
    'Graffiti': { colors: { background: '#1b1b1b', text: '#f5f5f5', primary: '#ffd400' }, title: 'Permanent Marker',
      parts: { name: '#ff3864', tabs: '#2de2e6' } },
    'Comic book': { colors: { background: '#fffbe6', text: '#111111', primary: '#e63946' }, title: 'Bangers', border: 'show',
      parts: { name: '#1d4ed8', border: '#111111' } },
    'Diner': { colors: { background: '#fff4e6', text: '#3d1f00', primary: '#d62828' }, title: 'Lobster', parts: { name: '#d62828', tabs: '#1d6f8c' } },
    'Ocean': { colors: { background: '#04293a', text: '#e0f7ff', primary: '#00b4d8' }, font: 'Comfortaa', parts: { name: '#90e0ef' } },
    'Forest': { colors: { background: '#0f1f14', text: '#e6f2e1', primary: '#7bc96f' }, font: 'Comfortaa', parts: { name: '#b5e48c' } },
    'Sunset': { colors: { background: '#2d1b2e', text: '#ffe9d6', primary: '#ff7b54' }, title: 'Pacifico', parts: { name: '#ffb26b' } },
    'Midnight': { colors: { background: '#0b132b', text: '#e0e1dd', primary: '#5bc0be' }, parts: { name: '#9fd8d6' } },
    'Monochrome': { colors: { background: '#ffffff', text: '#000000', primary: '#000000' }, border: 'show', parts: { border: '#000000' } },
  };
  // A preset as a theme object (the shape openEditor's theme() builds), for the editor and the test.
  function presetTheme(name){
    const p = PRESETS[name]; if(!p) return null;
    const font = n => n ? { name: n, url: fontFile(n) } : null;
    const fonts = {}; if(p.font || p.title){ fonts.body = font(p.font || ''); fonts.title = font(p.title || p.font || ''); }
    if(fonts.body === null) delete fonts.body;
    return { colors: Object.assign({}, p.colors), parts: Object.assign({}, p.parts || {}), border: p.border || '', fonts, bg: null, title: name };
  }
  const ED_CSS = '.pt-ed .pt-row{display:grid;grid-template-columns:1fr auto auto;align-items:center;gap:8px;margin:6px 0}'
    + '.pt-ed input[type=color]{width:44px;height:30px;padding:0;border:1px solid var(--line);border-radius:8px;background:none}'
    + '.pt-ed .pt-swatch{display:inline-flex;align-items:center;gap:6px;padding:5px 10px;border:2px solid;border-radius:999px;font:inherit;font-size:12px;cursor:pointer}'
    + '.pt-ed .pt-swatch i{width:10px;height:10px;border-radius:50%;flex:0 0 10px}.pt-ed .pt-swatch.on{outline:2px solid var(--accent);outline-offset:2px}'
    + '.pt-ed .pt-pick{display:flex;align-items:center;gap:6px}.pt-ed .pt-hex{width:88px;font-family:monospace;padding:4px 6px}'
    + '.pt-ed .pt-bels{display:flex;flex-wrap:wrap;gap:6px 14px;margin:6px 0}.pt-ed .pt-bels[hidden]{display:none}'
    + '.pt-ed .pt-presets{display:flex;flex-wrap:wrap;gap:6px;margin:8px 0;max-height:132px;overflow:auto}.pt-ed .pt-off input[type=color]{opacity:.35}'
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
                 parts: Object.assign({}, cur.parts || {}), border: cur.border || '',
                 bstyle: cur.bstyle || 'solid', bon: Array.isArray(cur.bon) ? cur.bon.slice() : BOLD.slice(), font: (cur.fonts.body && cur.fonts.body.name) || '',
                 titleFont: (cur.fonts.title && cur.fonts.title.name !== (cur.fonts.body && cur.fonts.body.name) && cur.fonts.title.name) || '',
                 bg: cur.bg ? Object.assign({}, cur.bg) : null };
    const row = (key, label, val, on) => `<div class="pt-row${on ? '' : ' pt-off'}" data-k="${key}"><span>${enc(label)}</span>`
      + `<span class="pt-pick"><input type="color" aria-label="${enc(label)}" value="${enc(val)}"><input class="input pt-hex" aria-label="${enc(label)} (hex)" value="${enc(val)}" maxlength="7" spellcheck="false" autocomplete="off"></span>` + (on === null ? '<span></span>'
      : `<button type="button" class="btn btn-ghost small pt-clear" title="Use the page's ${COLOR}">${on ? 'Reset' : 'Set'}</button>`) + '</div>';
    o.modal(`<div class="pt-ed"><h3 class="cmp-hd">Profile theme<button class="modal-x" id="pt-close" aria-label="Close">&#215;</button></h3>
      <p class="muted small">Your page's ${COLOR}s, font, background and borders -- like MySpace. ${COLOR[0].toUpperCase()+COLOR.slice(1)}s, font and background also show on Ditto and Armada; the rest is PosterChan's.</p>
      <div class="pt-presets" role="list" aria-label="Themes">${Object.keys(PRESETS).map(n => { const c = PRESETS[n].colors;
        return `<button type="button" class="pt-swatch" role="listitem" data-preset="${enc(n)}" style="background:${c.background};color:${c.text};border-color:${c.primary}"><i style="background:${c.primary}"></i>${enc(n)}</button>`; }).join('')}</div>
      ${row('background', 'Background', st.colors.background, null)}${row('text', 'Text', st.colors.text, null)}${row('primary', 'Accent', st.colors.primary, null)}
      <details><summary>More ${COLOR}s</summary>${Object.keys(PARTS).map(k => row('p:' + k, PARTS[k][0].toUpperCase() + PARTS[k].slice(1), st.parts[k] || '#888888', !!st.parts[k])).join('')}
        <label class="fld">Borders<select class="input" id="pt-border"><option value="">As the app draws them</option><option value="none">None</option>${BSTYLES.map(b => `<option value="${b}">${{solid:'Solid line',dashed:'Dashes - - -',dotted:'Circles \u25cf\u25cf\u25cf',double:'Double line',squares:'Squares \u25a0\u25a0\u25a0',triangles:'Triangles \u25b2\u25b6\u25bc\u25c0'}[b]}</option>`).join('')}</select></label>
        <div class="pt-bels" id="pt-bels">${Object.keys(BELS).map(k => `<label><input type="checkbox" data-bel="${k}"> ${enc(BELS[k][0].toUpperCase() + BELS[k].slice(1))}</label>`).join('')}</div></details>
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
    const bsel = () => st.border === 'show' ? st.bstyle : st.border;     // '' | none | a style
    const syncBorders = () => { $('#pt-border').value = bsel();
      const els = $('#pt-bels'); els.hidden = st.border !== 'show';
      els.querySelectorAll('[data-bel]').forEach(c => { c.checked = st.bon.includes(c.dataset.bel); }); };
    syncBorders(); if(st.bg) $('#pt-mode').value = st.bg.mode;
    const theme = () => {
      const url = https($('#pt-bgurl').value.trim());
      const fonts = {};
      if(st.font) fonts.body = { name: st.font, url: fontFile(st.font) };
      if(st.titleFont || st.font) fonts.title = { name: st.titleFont || st.font, url: fontFile(st.titleFont || st.font) };
      return { colors: st.colors, parts: st.parts, border: st.border, bstyle: st.bstyle, bon: st.bon, title: st.preset || 'Profile theme', fonts,
               bg: url ? { url, mode: $('#pt-mode').value, m: (st.bg && st.bg.url === url && st.bg.m) || '' } : null };
    };
    const preview = () => {
      const t = parse({ kind: KIND, tags: tags(theme()) }), pv = $('#pt-prev'), v = vars(t);
      pv.removeAttribute('style');
      for(const k in v) pv.style.setProperty(k, v[k]);
      if(t && t.bg){ pv.style.setProperty('--pt-img', 'url("' + t.bg.url + '")'); pv.style.setProperty('--pt-size', t.bg.mode === 'tile' ? 'auto' : t.bg.mode); pv.style.setProperty('--pt-repeat', t.bg.mode === 'tile' ? 'repeat' : 'no-repeat'); }
      const face = (name, prop) => { if(!name || typeof FontFace !== 'function') return; const fam = 'pt-ed-' + name.replace(/\s+/g, '-');
        new FontFace(fam, 'url("' + fontFile(name) + '")').load().then(f => { document.fonts.add(f); pv.style.setProperty(prop, '"' + fam + '"'); }, () => {}); };
      face(st.font, '--pt-body'); face(st.titleFont || st.font, '--pt-title');
    };
    box.querySelectorAll('.pt-row').forEach(r => {
      const k = r.dataset.k, inp = r.querySelector('input[type=color]'), hex = r.querySelector('.pt-hex'), clr = r.querySelector('.pt-clear');
      // The typed #hex and the picker are one value: a valid code moves the picker, the picker rewrites the code.
      if(hex){ hex.addEventListener('input', () => { const v = hex.value.trim(), h = v[0] === '#' ? v : '#' + v;
          if(HEX.test(h)){ inp.value = h.toLowerCase(); inp.dispatchEvent(new Event('input')); hex.classList.remove('bad'); } else hex.classList.add('bad'); });
        inp.addEventListener('input', () => { if(document.activeElement !== hex) hex.value = inp.value; }); }
      inp.addEventListener('input', () => { if(k.startsWith('p:')){ st.parts[k.slice(2)] = inp.value; r.classList.remove('pt-off'); if(clr) clr.textContent = 'Reset'; } else st.colors[k] = inp.value; preview(); });
      if(clr) clr.onclick = () => { const p = k.slice(2);
        if(st.parts[p]){ delete st.parts[p]; r.classList.add('pt-off'); clr.textContent = 'Set'; } else { st.parts[p] = inp.value; r.classList.remove('pt-off'); clr.textContent = 'Reset'; }
        preview(); };
    });
    /* A theme is the whole look: colours, fonts, part colours and borders replace what is set; the background picture
     * is the person's and stays. Every control is brought into step so "Save" saves what the preview shows. */
    box.querySelectorAll('[data-preset]').forEach(b => b.onclick = () => {
      const p = PRESETS[b.dataset.preset]; if(!p) return;
      st.colors = Object.assign({}, p.colors); st.parts = Object.assign({}, p.parts || {}); st.border = p.border || '';
      st.font = p.font || ''; st.titleFont = p.title || ''; st.preset = b.dataset.preset;
      for(const k of ['background', 'text', 'primary']){ const r = box.querySelector(`.pt-row[data-k="${k}"]`); if(!r) continue;
        r.querySelector('input[type=color]').value = st.colors[k]; const hx = r.querySelector('.pt-hex'); if(hx) hx.value = st.colors[k]; }
      box.querySelectorAll('.pt-row[data-k^="p:"]').forEach(r => { const k = r.dataset.k.slice(2), on = !!st.parts[k], clr = r.querySelector('.pt-clear');
        { const v = st.parts[k] || '#888888'; r.querySelector('input[type=color]').value = v; const hx = r.querySelector('.pt-hex'); if(hx) hx.value = v; } r.classList.toggle('pt-off', !on); if(clr) clr.textContent = on ? 'Reset' : 'Set'; });
      st.bstyle = p.bstyle || 'solid'; st.bon = (p.bon || BOLD).slice(); syncBorders(); $('#pt-font').value = st.font;
      box.querySelectorAll('[data-preset]').forEach(x => x.classList.toggle('on', x === b));
      preview(); });
    $('#pt-border').onchange = () => { const v = $('#pt-border').value;
      if(BSTYLES.includes(v)){ st.border = 'show'; st.bstyle = v; } else st.border = v;
      syncBorders(); preview(); };
    box.querySelectorAll('[data-bel]').forEach(c => c.onchange = () => {
      st.bon = Object.keys(BELS).filter(k => box.querySelector(`[data-bel="${k}"]`).checked); preview(); });
    $('#pt-font').onchange = () => { st.font = $('#pt-font').value; st.titleFont = ''; preview(); };
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

  root.PCProfileTheme = { KIND, PARTS, FONTS, PRESETS, presetTheme, parse, vars, tags, contrast, apply, leave, openEditor, current: () => live };
})(typeof window !== 'undefined' ? window : globalThis);
