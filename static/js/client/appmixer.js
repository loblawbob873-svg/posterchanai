/* The desktop's VOLUME MIXER panel -- one row per app (each open window, Music, Other sounds) with a level and mute.
 *
 * The levels live in os.js (`appVolumes`/`setAppVolume`, a hook every media element plays through); this file is only
 * the panel, loaded the first time it is opened from the tray's speaker or the command bar. A row that is playing
 * right now says so and pulses, so "which window is making that noise" has an answer. On PosterChanOS the machine's
 * own mixer (Quick Settings) still sets PosterChan's level as a whole; this splits it between PosterChan's windows.
 */
window.PCAppMixer = (function(){
  let _open = null;

  function css(){
    if(document.getElementById('pc-mixer-css')) return;
    const s = document.createElement('style'); s.id = 'pc-mixer-css';
    s.textContent = `
.pc-mixer{position:fixed;z-index:2147483000;width:min(360px,calc(100vw - 32px));max-height:min(70vh,520px);overflow:auto;
  padding:14px;border-radius:14px;background:var(--bg2,#0b0d14);border:1px solid var(--neon,#0ff);color:var(--text,#e8e8f0);
  box-shadow:0 18px 50px rgba(0,0,0,.55),0 0 22px color-mix(in srgb,var(--neon,#0ff) 35%,transparent);font:14px/1.3 Inter,system-ui,sans-serif}
.pc-mixer h3{margin:0 0 10px;font-size:15px;display:flex;align-items:center;gap:8px;letter-spacing:.02em}
.pc-mixer h3 .ic{width:18px;height:18px;color:var(--neon,#0ff)}
.pc-mix-row{display:grid;grid-template-columns:24px 1fr auto;grid-template-rows:auto auto;gap:4px 10px;align-items:center;
  padding:8px 6px;border-radius:10px}
.pc-mix-row+.pc-mix-row{border-top:1px solid var(--border,rgba(255,255,255,.08))}
.pc-mix-row .ic{width:20px;height:20px;grid-row:1/3;color:var(--muted,#9aa)}
.pc-mix-row.playing .ic{color:var(--neon,#0ff);filter:drop-shadow(0 0 6px var(--neon,#0ff))}
.pc-mix-name{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-weight:600}
.pc-mix-name small{font-weight:400;color:var(--neon,#0ff);margin-left:6px}
.pc-mix-pct{font:12px 'JetBrains Mono',ui-monospace,monospace;color:var(--muted,#9aa);min-width:4ch;text-align:right}
.pc-mix-ctl{grid-column:2/4;display:flex;align-items:center;gap:8px}
.pc-mix-ctl input{flex:1;accent-color:var(--neon,#0ff);min-height:28px}
.pc-mix-mute{appearance:none;border:1px solid var(--border,rgba(255,255,255,.15));background:none;color:inherit;border-radius:8px;
  width:34px;height:30px;cursor:pointer;display:inline-flex;align-items:center;justify-content:center}
.pc-mix-mute.on{border-color:#ff2d6f;color:#ff2d6f;box-shadow:0 0 8px #ff2d6f}
.pc-mix-mute .ic{width:16px;height:16px;grid-row:auto;color:inherit;filter:none}
.pc-mix-none{padding:14px 6px;color:var(--muted,#9aa)}`;
    document.head.appendChild(s);
  }
  const href = i => !i ? '#i-volume' : (i[0] === '#' ? i : '#' + i);
  const enc = s => String(s == null ? '' : s).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c]);

  function close(){
    if(!_open) return;
    clearInterval(_open.timer); _open.el.remove();
    document.removeEventListener('pointerdown', _open.away, true); document.removeEventListener('keydown', _open.key, true);
    _open = null;
  }

  function open(anchor, api){
    if(_open){ const was = _open.anchor; close(); if(was === anchor && anchor) return; }
    css();
    const el = document.createElement('div');
    el.className = 'pc-mixer'; el.setAttribute('role', 'dialog'); el.setAttribute('aria-label', 'Volume mixer');
    document.documentElement.appendChild(el);
    const paint = () => {
      const rows = api.rows();
      el.innerHTML = `<h3><svg class="ic" aria-hidden="true"><use href="#i-sliders"></use></svg>Volume mixer</h3>` + (rows.length ? rows.map(r => `
        <div class="pc-mix-row${r.playing ? ' playing' : ''}" data-app="${enc(r.app)}">
          <svg class="ic" aria-hidden="true"><use href="${enc(href(r.icon))}"></use></svg>
          <span class="pc-mix-name">${enc(r.label)}${r.playing ? '<small>playing</small>' : ''}</span>
          <span class="pc-mix-pct">${r.m ? 'muted' : Math.round(r.v * 100) + '%'}</span>
          <span class="pc-mix-ctl"><button type="button" class="pc-mix-mute${r.m ? ' on' : ''}" aria-pressed="${r.m}" aria-label="${r.m ? 'Unmute' : 'Mute'} ${enc(r.label)}" title="${r.m ? 'Unmute' : 'Mute'}">
            <svg class="ic" aria-hidden="true"><use href="#i-${r.m ? 'volume-mute' : 'volume'}"></use></svg></button>
            <input type="range" min="0" max="100" step="1" value="${Math.round(r.v * 100)}" aria-label="${enc(r.label)} volume"></span>
        </div>`).join('') : '<div class="pc-mix-none">Nothing is open that can make a sound.</div>');
      el.querySelectorAll('.pc-mix-row').forEach(row => {
        const app = row.dataset.app, range = row.querySelector('input'), pct = row.querySelector('.pc-mix-pct');
        range.oninput = () => { api.set(app, range.value / 100, false); pct.textContent = range.value + '%';
          const m = row.querySelector('.pc-mix-mute'); m.classList.remove('on'); m.setAttribute('aria-pressed', 'false'); };
        row.querySelector('.pc-mix-mute').onclick = () => { const r = api.rows().find(x => x.app === app); api.set(app, null, !(r && r.m)); paint(); };
      });
    };
    paint();
    // Place it: above the anchor (the taskbar is at the bottom), right-aligned to it; centred when opened by name.
    const r = anchor && anchor.getBoundingClientRect && anchor.getBoundingClientRect();
    const w = el.offsetWidth, h = el.offsetHeight;
    if(r && r.width){
      el.style.left = Math.max(16, Math.min(innerWidth - w - 16, r.right - w)) + 'px';
      el.style.top = Math.max(16, r.top - h - 10) + 'px';
    } else { el.style.left = Math.max(16, (innerWidth - w) / 2) + 'px'; el.style.top = Math.max(16, (innerHeight - h) / 3) + 'px'; }
    // Repaint the "playing" marks while open -- but never under a slider being dragged.
    const timer = setInterval(() => { if(!el.contains(document.activeElement) || document.activeElement.type !== 'range') paint(); }, 1500);
    const away = e => { if(!el.contains(e.target) && !(anchor && anchor.contains && anchor.contains(e.target))) close(); };
    const key = e => { if(e.key === 'Escape') close(); };
    document.addEventListener('pointerdown', away, true); document.addEventListener('keydown', key, true);
    _open = { el, anchor, timer, away, key };
    const first = el.querySelector('input'); if(first) try{ first.focus({ preventScroll: true }); }catch(_){}
  }

  return { open, close, isOpen: () => !!_open };
})();
