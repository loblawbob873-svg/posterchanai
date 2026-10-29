/* The desktop assistant's panel ("Do it" in the command bar). os.js owns the plan (the model call) and the actions;
 * this only shows the answer and one button per step. Nothing runs until a button is pressed -- "Run all" is a
 * press too -- and every step says afterwards whether it worked. Loaded on first use, never at boot. */
window.PCDeskAssist = (function(){
  let box = null, lastFocus = null;
  const enc = s => String(s == null ? '' : s).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c]);
  const ICON = { open: '🗔', snap: '◧', arrange: '▦', workspace: '⧉', move: '⇄', volume: '🔊', screenshot: '📸', record: '⏺️',
                 note: '📝', post: '✏️', search: '🔎', close: '✕' };
  function detail(st){
    if(st.do === 'note' || st.do === 'post' || st.do === 'search') return st.text;
    if(st.do === 'volume') return st.mute != null ? (st.mute ? 'Muted' : 'Unmuted') : st.level + '%';
    return '';
  }
  function css(){
    if(document.getElementById('pc-assist-css')) return;
    const s = document.createElement('style'); s.id = 'pc-assist-css';
    s.textContent = `
.pc-assist{position:fixed;inset:0;z-index:2147483000;display:flex;justify-content:center;align-items:flex-start;font-family:Inter,system-ui,sans-serif;color:var(--text,#edeefa)}
.pc-assist .as-back{position:absolute;inset:0;background:rgba(5,5,12,.5);backdrop-filter:blur(3px)}
.pc-assist .as-panel{position:relative;margin-top:12vh;width:min(560px,calc(100vw - 32px));max-height:76vh;overflow:auto;padding:16px;
  background:var(--panel2,#1a1a2a);border:1px solid rgba(var(--accent-rgb,60,232,255),.55);border-radius:var(--r,14px);
  box-shadow:var(--glow,0 0 18px -2px rgba(60,232,255,.45)),0 24px 60px rgba(0,0,0,.55)}
.pc-assist h3{margin:0 0 4px;font:700 13px Orbitron,Inter,sans-serif;letter-spacing:.12em;text-transform:uppercase;color:var(--neon2,#ff5cf0)}
.pc-assist .as-q{font-size:15px;font-weight:600;margin-bottom:10px;word-break:break-word}
.pc-assist .as-ans{color:var(--muted,#c3c5e0);margin:6px 0 12px;line-height:1.45}
.pc-assist .as-steps{display:flex;flex-direction:column;gap:6px}
.pc-assist .as-step{display:flex;align-items:center;gap:10px;width:100%;padding:10px 12px;border-radius:10px;cursor:pointer;text-align:left;font:inherit;color:inherit;
  background:rgba(var(--accent-rgb,60,232,255),.06);border:1px solid rgba(var(--accent-rgb,60,232,255),.3)}
.pc-assist .as-step:hover,.pc-assist .as-step:focus-visible{outline:none;border-color:var(--neon,#3ce8ff);box-shadow:0 0 10px rgba(var(--accent-rgb,60,232,255),.4)}
.pc-assist .as-step .as-i{flex:0 0 22px;text-align:center;font-size:16px}
.pc-assist .as-step .as-t{flex:1;min-width:0;display:flex;flex-direction:column}
.pc-assist .as-step .as-t i{font-style:normal;font-size:12px;color:var(--muted,#9fa1c6);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.pc-assist .as-step .as-s{font-size:12px;color:var(--muted,#9fa1c6)}
.pc-assist .as-step.done{border-color:#28e07a}.pc-assist .as-step.done .as-s{color:#28e07a}
.pc-assist .as-step.bad{border-color:#ff2d6f}.pc-assist .as-step.bad .as-s{color:#ff2d6f}
.pc-assist .as-acts{display:flex;gap:8px;justify-content:flex-end;margin-top:12px;flex-wrap:wrap}
@media(max-width:640px){.pc-assist .as-panel{margin-top:0;width:100vw;border-radius:0 0 14px 14px}}`;
    document.head.appendChild(s);
  }
  function close(){ if(!box) return; box.remove(); box = null; try{ lastFocus && lastFocus.focus({ preventScroll: true }); }catch(_){} }

  async function open(q, api){
    close(); css(); lastFocus = document.activeElement;
    box = document.createElement('div'); box.className = 'pc-assist'; box.setAttribute('role', 'dialog'); box.setAttribute('aria-label', 'Desktop assistant');
    box.innerHTML = `<div class="as-back"></div><div class="as-panel"><h3>🪄 Assistant</h3><div class="as-q">${enc(q)}</div>
      <div class="as-body"><div class="spinner"></div><div class="as-ans">Planning…</div></div></div>`;
    document.documentElement.appendChild(box);
    const me = box;
    box.querySelector('.as-back').onclick = close;
    box.addEventListener('keydown', e => { if(e.key === 'Escape'){ e.preventDefault(); e.stopPropagation(); close(); } });
    const body = box.querySelector('.as-body');
    let plan;
    try{ plan = await api.ask(); }
    catch(e){
      if(box !== me) return;
      body.innerHTML = `<div class="as-ans">${enc((e && e.message) || e)}</div><div class="as-acts"><button type="button" class="btn btn-ghost as-close">Close</button><button type="button" class="btn btn-neon as-retry">Try again</button></div>`;
      body.querySelector('.as-close').onclick = close; body.querySelector('.as-retry').onclick = () => open(q, api);
      return;
    }
    if(box !== me) return;
    const steps = (plan && plan.steps) || [];
    body.innerHTML = `<div class="as-ans">${enc(plan && plan.answer)}</div>
      <div class="as-steps">${steps.map((st, i) => `<button type="button" class="as-step" data-i="${i}"><span class="as-i">${ICON[st.do] || '•'}</span>
        <span class="as-t"><b>${enc(st.label)}</b>${detail(st) ? `<i>${enc(detail(st))}</i>` : ''}</span><span class="as-s"></span></button>`).join('')}</div>
      <div class="as-acts"><button type="button" class="btn btn-ghost as-close">Close</button>${steps.length > 1 ? '<button type="button" class="btn btn-neon as-all">Run all</button>' : ''}</div>`;
    body.querySelector('.as-close').onclick = close;
    const runOne = async i => {
      const b = body.querySelector(`.as-step[data-i="${i}"]`); if(!b || b.classList.contains('done')) return true;
      const s = b.querySelector('.as-s'); s.textContent = '…';
      try{ await api.run(steps[i]); b.classList.add('done'); b.classList.remove('bad'); s.textContent = '✓ done'; return true; }
      catch(e){ b.classList.add('bad'); s.textContent = String((e && e.message) || e); return false; }
    };
    body.querySelectorAll('.as-step').forEach(b => b.onclick = () => runOne(+b.dataset.i));
    const all = body.querySelector('.as-all');
    if(all) all.onclick = async () => {
      all.disabled = true;
      // In order, stopping at the first that fails: a later step may depend on it (snap the window it opened).
      for(let i = 0; i < steps.length; i++){ if(!(await runOne(i))) break; await new Promise(r => setTimeout(r, 250)); }
      all.disabled = false;
      if(box === me && steps.every((_, i) => body.querySelector(`.as-step[data-i="${i}"]`).classList.contains('done'))) setTimeout(() => { if(box === me) close(); }, 700);
    };
    const first = all || body.querySelector('.as-step'); if(first) try{ first.focus({ preventScroll: true }); }catch(_){}
  }
  return { open, close, isOpen: () => !!box };
})();
