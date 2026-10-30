/* System Settings → Window effects, loaded only when that page is opened (os.js _loadPage): it exists on PosterChanOS
 * alone, so no other boot pays for it (tests/test_client_boot_payload_budget.py).
 *
 * "we need the window effects on posterchan os, wobbly windows, exploding windows, etc. its own settings menu" and
 * "put effect in a diff .js file to keep it small and modular" (2026-10-29). The effects are Wayfire's own -- animate,
 * extra-animations and wobbly -- chosen through pc-window-effects (desktop/windoweffects.js); this page draws the
 * choices the helper OFFERS on this machine and applies each change at once. PosterChan's app windows are compositor
 * windows on PosterChanOS, so they wobble and explode like any other; the desktop surface itself never does. */
(function(root){
  'use strict';
  const NAMES = { none:'None', fade:'Fade', zoom:'Zoom', fire:'Burn', zap:'Zap', spin:'Spin', shatter:'Explode',
    vortex:'Vortex', helix:'Helix', blinds:'Blinds', melt:'Melt', squeezimize:'Squeeze into the taskbar' };
  const SPEEDS = { slow:'Slow', normal:'Normal', fast:'Fast' };
  const WOBBLE = { low:'A little', medium:'Medium', high:'Jelly' };
  /* One press for a whole look; each is only offered when every effect in it is on this machine. */
  const PRESETS = [
    ['Classic', { open:'zoom', close:'zoom', minimize:'squeezimize', speed:'normal', wobbly:false }],
    ['Wobbly', { open:'zoom', close:'zoom', minimize:'squeezimize', speed:'normal', wobbly:true, wobbliness:'medium' }],
    ['Explosive', { open:'fire', close:'shatter', minimize:'squeezimize', speed:'normal', wobbly:true, wobbliness:'high' }],
    ['Inferno', { open:'fire', close:'fire', minimize:'squeezimize', speed:'slow', wobbly:false, fire_rainbow:true }],
    ['Calm', { open:'fade', close:'fade', minimize:'fade', speed:'slow', wobbly:false }],
    ['Off', { open:'none', close:'none', minimize:'none', speed:'normal', wobbly:false }],
  ];
  const CSS = '.wfx-color{width:56px;height:34px;padding:2px;border:1px solid var(--line,#333);border-radius:8px;background:none;cursor:pointer}.wfx-color:disabled{opacity:.4}.wfx-presets{display:flex;flex-wrap:wrap;gap:8px}.wfx-presets .btn.on{outline:2px solid var(--accent);outline-offset:1px}'
    + '.wfx-note{margin:8px 0 0;font-size:13px}.wfx-note.warn{color:var(--warn,#ffb020)}';

  const offers = (st, k) => ((st.offered || {})[k] || []);
  const fits = (st, c) => ['open','close','minimize'].every(k => offers(st, k).includes(c[k])) && (!c.wobbly || offers(st, 'wobbly') === true);
  const same = (a, b) => ['open','close','minimize','speed','wobbly'].every(k => a[k] === b[k]) && (!a.wobbly || (a.wobbliness || 'medium') === (b.wobbliness || 'medium'))
    && (b.fire_rainbow === undefined || !!a.fire_rainbow === b.fire_rainbow);

  function page(st, err, d){
    const head = `<header class="os-set-pagehead"><div>${d.iconSvg('i-wand')}</div><span><h2>Window effects</h2><p>Wobbly windows, and how a window opens, closes and minimizes. Changes apply at once.</p></span></header>`;
    if(err) return head + `<div class="empty" data-wfx-error>${d.enc(err)}</div>`;
    const c = st.chosen || {}, enc = d.enc;
    const opt = (k, labels) => offers(st, k).map(v => `<option value="${enc(v)}" ${c[k]===v?'selected':''}>${enc(labels[v] || v)}</option>`).join('');
    const row = (title, sub, ctl) => `<section class="os-setting-row os-set-control"><div><b>${title}</b><span>${sub}</span></div>${ctl}</section>`;
    const presets = PRESETS.filter(([, p]) => fits(st, p))
      .map(([name, p]) => `<button class="btn${same(c, p) ? ' on' : ''}" data-wfx-preset="${enc(name)}">${enc(name)}</button>`).join('');
    return head
      + `<section class="os-set-card"><div class="os-set-cardhead"><b>Looks</b><span>Start from one of these, then change any part below.</span></div><div class="wfx-presets">${presets}</div></section>`
      + (offers(st, 'wobbly') === true ? row('Wobbly windows', 'A window bends and springs back as you drag it.',
          `<label class="os-set-switch"><input data-wfx="wobbly" type="checkbox" ${c.wobbly?'checked':''} aria-label="Wobbly windows"><span>${c.wobbly?'On':'Off'}</span></label>`)
        + row('How much they wobble', 'From a slight sway to jelly.',
          `<select class="input" data-wfx="wobbliness" aria-label="How much windows wobble" ${c.wobbly?'':'disabled'}>${opt('wobbliness', WOBBLE)}</select>`) : '')
      + row('Opening a window', 'The effect a window appears with.', `<select class="input" data-wfx="open" aria-label="Opening a window">${opt('open', NAMES)}</select>`)
      + row('Closing a window', 'Explode breaks it into pieces; Burn sets it on fire.', `<select class="input" data-wfx="close" aria-label="Closing a window">${opt('close', NAMES)}</select>`)
      + ((c.open === 'fire' || c.close === 'fire') ? row('Fire colour', 'The colour Burn\u2019s flames take.',
          `<input class="wfx-color" type="color" data-wfx="fire_color" value="${enc(/^#[0-9a-f]{6}$/i.test(c.fire_color||'') ? c.fire_color : '#b22303')}" aria-label="Fire colour" ${c.fire_rainbow?'disabled':''}>`)
        + row('Rainbow fire', 'Every flame its own colour.',
          `<label class="os-set-switch"><input data-wfx="fire_rainbow" type="checkbox" ${c.fire_rainbow?'checked':''} aria-label="Rainbow fire"><span>${c.fire_rainbow?'On':'Off'}</span></label>`) : '')
      + row('Minimizing a window', 'The effect a window leaves the screen with.', `<select class="input" data-wfx="minimize" aria-label="Minimizing a window">${opt('minimize', NAMES)}</select>`)
      + row('Speed', 'How long each effect takes.', `<select class="input" data-wfx="speed" aria-label="Effect speed">${opt('speed', SPEEDS)}</select>`)
      + `<p class="wfx-note muted" data-wfx-note>${st.error ? '' : 'Open or close any window to see it.'}</p>`
      + (st.error ? `<p class="wfx-note warn" data-wfx-warn>${enc(st.error)}</p>` : '');
  }

  async function mount(slot, d){
    if(!slot) return;
    if(!document.getElementById('pc-wfx-css')){
      const s = document.createElement('style'); s.id = 'pc-wfx-css'; s.textContent = CSS; document.head.appendChild(s);
    }
    const bridge = root.pcWindowEffects && root.pcWindowEffects.available() ? root.pcWindowEffects : null;
    let st = {}, err = '';
    if(!bridge) err = 'Window effects are only available on PosterChanOS.';
    else{
      const r = await d.read(bridge.status(), 'window effects');
      if(!d.alive()) return;
      if(r.ok) st = r.value || {};
      else err = r.timedOut ? 'The window effects helper did not answer.' : 'Could not read the window effects: ' + r.error;
    }
    const paint = () => { slot.innerHTML = page(st, err, d); if(bridge && !err) bind(); };
    /* One change at a time: a select changed while the last one is still being applied waits for it, then sends the
     * newest whole choice -- never two helpers racing to write the same file. */
    let busy = null, queued = null;
    const apply = async (choice) => {
      queued = choice;
      if(busy) return;
      while(queued){
        const next = queued; queued = null;
        busy = d.read(bridge.set(next), 'window effects');
        const r = await busy; busy = null;
        if(!d.alive()) return;
        if(r.ok) st = r.value || st;
        else st = Object.assign({}, st, { error: r.timedOut ? 'The window effects helper did not answer.' : 'Not saved: ' + r.error });
      }
      paint();
    };
    function bind(){
      slot.querySelectorAll('[data-wfx]').forEach(el => el.onchange = () => {
        const c = Object.assign({}, st.chosen || {});
        c[el.dataset.wfx] = el.type === 'checkbox' ? el.checked : el.value;
        st = Object.assign({}, st, { chosen: c, error: '' });
        if(el.type === 'checkbox'){ const s = el.parentNode.querySelector('span'); if(s) s.textContent = el.checked ? 'On' : 'Off'; }
        const w = slot.querySelector('[data-wfx="wobbliness"]'); if(w) w.disabled = !c.wobbly;
        const fc = slot.querySelector('[data-wfx="fire_color"]'); if(fc) fc.disabled = !!c.fire_rainbow;
        // choosing or leaving Burn shows or hides its colour rows
        if((el.dataset.wfx === 'open' || el.dataset.wfx === 'close') && !!slot.querySelector('[data-wfx="fire_color"]') !== (c.open === 'fire' || c.close === 'fire')) paint();
        apply(c);
      });
      slot.querySelectorAll('[data-wfx-preset]').forEach(b => b.onclick = () => {
        const p = (PRESETS.find(x => x[0] === b.dataset.wfxPreset) || [])[1]; if(!p) return;
        const c = Object.assign({}, st.chosen || {}, p);
        st = Object.assign({}, st, { chosen: c, error: '' });
        paint(); apply(c);
      });
    }
    paint();
  }
  root.PCWindowEffects = { page, mount, PRESETS };
})(typeof window !== 'undefined' ? window : globalThis);
