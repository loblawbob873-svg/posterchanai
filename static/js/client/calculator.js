/* Calculator — the web client, PosterChanOS (its own window) and the Android launcher tile.
 *
 * THE EVALUATOR IS A PARSER, NEVER eval(). What is typed is arithmetic, and `eval`/`Function` on it
 * would run anything else that is typed (or pasted) with this origin's session and keys in reach.
 * Recursive descent over a fixed grammar:
 *     expr   := term (('+'|'-') term)*
 *     term   := unary (('*'|'/'|'%'|implicit) unary)*        implicit: 2π, 3(4), (1)(2)
 *     unary  := ('-'|'+') unary | power
 *     power  := postfix ('^' unary)?                          right-assoc: 2^3^2 = 2^9
 *     postfix:= atom ('!' | '²')*
 *     atom   := number | constant | func '(' expr ')' | '(' expr ')'
 * `%` after a number is percent (50% = 0.5); between two operands it is modulo.
 *
 * NUMBERS ARE SHOWN TO 12 SIGNIFICANT DIGITS so 0.1+0.2 reads 0.3 -- binary floating point is the
 * arithmetic, the display does not have to be. DOM-free core (evaluate/format), so
 * tests/client/test_calculator.py runs this exact file under node.
 */
(function(){
  'use strict';
  const FUNCS = {
    sin: 1, cos: 1, tan: 1, asin: 1, acos: 1, atan: 1,
    ln: x => Math.log(x), log: x => Math.log10(x), sqrt: x => Math.sqrt(x), abs: x => Math.abs(x),
    exp: x => Math.exp(x), round: x => Math.round(x), floor: x => Math.floor(x), ceil: x => Math.ceil(x),
  };
  const CONSTS = { pi: Math.PI, 'π': Math.PI, e: Math.E, tau: 2 * Math.PI };

  function tokenize(src){
    const s = String(src == null ? '' : src)
      .replace(/[×✕]/g, '*').replace(/[÷]/g, '/').replace(/[−–—]/g, '-').replace(/√/g, 'sqrt')
      .replace(/\*\*/g, '^').replace(/,/g, '');
    const out = [];
    let i = 0;
    while(i < s.length){
      const c = s[i];
      if(/\s/.test(c)){ i++; continue; }
      if(/[0-9.]/.test(c)){
        const m = /^(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?/.exec(s.slice(i));
        if(!m) throw new Error('bad number');
        out.push({ t: 'num', v: parseFloat(m[0]) }); i += m[0].length; continue;
      }
      if(/[a-zA-Zπ]/.test(c)){
        const m = /^(?:π|[a-zA-Z]+)/.exec(s.slice(i)); const w = m[0].toLowerCase();
        // OWN properties only: `'constructor' in CONSTS` is true through the prototype.
        if(Object.prototype.hasOwnProperty.call(CONSTS, w)) out.push({ t: 'num', v: CONSTS[w] });
        else if(Object.prototype.hasOwnProperty.call(FUNCS, w)) out.push({ t: 'fn', v: w });
        else throw new Error('unknown “' + m[0] + '”');
        i += m[0].length; continue;
      }
      if('+-*/^%()!²'.includes(c)){ out.push({ t: 'op', v: c }); i++; continue; }
      throw new Error('unexpected “' + c + '”');
    }
    return out;
  }

  function fact(n){
    if(n < 0 || !Number.isInteger(n)) throw new Error('factorial needs a whole number');
    if(n > 170) return Infinity;
    let r = 1; for(let k = 2; k <= n; k++) r *= k; return r;
  }

  function evaluate(src, opts){
    const deg = !!(opts && opts.deg);
    const toks = tokenize(src);
    if(!toks.length) return null;
    let p = 0;
    const peek = () => toks[p], take = () => toks[p++];
    const isOp = (v) => { const t = peek(); return !!t && t.t === 'op' && t.v === v; };
    const trig = (name, x) => {
      const r = deg ? x * Math.PI / 180 : x;
      if(name === 'sin') return Math.sin(r);
      if(name === 'cos') return Math.cos(r);
      if(name === 'tan'){ const c = Math.cos(r); if(Math.abs(c) < 1e-15) throw new Error('undefined'); return Math.sin(r) / c; }
      const inv = name === 'asin' ? Math.asin(x) : name === 'acos' ? Math.acos(x) : Math.atan(x);
      return deg ? inv * 180 / Math.PI : inv;
    };
    const startsAtom = (t) => !!t && (t.t === 'num' || t.t === 'fn' || (t.t === 'op' && t.v === '('));

    function atom(){
      const t = take();
      if(!t) throw new Error('incomplete');
      if(t.t === 'num') return t.v;
      if(t.t === 'fn'){
        if(!isOp('(')) throw new Error(t.v + ' needs ( )');
        take(); const x = expr();
        if(!isOp(')')) throw new Error('missing )'); take();
        const f = FUNCS[t.v];
        return typeof f === 'function' ? f(x) : trig(t.v, x);
      }
      if(t.t === 'op' && t.v === '('){
        const x = expr();
        if(!isOp(')')) throw new Error('missing )'); take();
        return x;
      }
      throw new Error('unexpected “' + t.v + '”');
    }
    function postfix(){
      let x = atom();
      for(;;){
        if(isOp('!')){ take(); x = fact(x); }
        else if(isOp('²')){ take(); x = x * x; }
        else if(isOp('%') && !startsAtom(toks[p + 1])){ take(); x = x / 100; }   // 50% → 0.5
        else return x;
      }
    }
    function power(){
      const b = postfix();
      if(isOp('^')){ take(); return Math.pow(b, unary()); }
      return b;
    }
    function unary(){
      if(isOp('-')){ take(); return -unary(); }
      if(isOp('+')){ take(); return unary(); }
      return power();
    }
    function term(){
      let x = unary();
      for(;;){
        if(isOp('*')){ take(); x *= unary(); }
        else if(isOp('/')){ take(); const d = unary(); if(d === 0) throw new Error('divide by zero'); x /= d; }
        else if(isOp('%')){ take(); const d = unary(); if(d === 0) throw new Error('divide by zero'); x %= d; }
        else if(startsAtom(peek())){ x *= unary(); }          // implicit: 2π, 3(4), (1)(2)
        else return x;
      }
    }
    function expr(){
      let x = term();
      for(;;){
        if(isOp('+')){ take(); x += term(); }
        else if(isOp('-')){ take(); x -= term(); }
        else return x;
      }
    }
    const v = expr();
    if(p < toks.length) throw new Error(toks[p].v === ')' ? 'unmatched )' : 'unexpected “' + toks[p].v + '”');
    if(Number.isNaN(v)) throw new Error('not a number');
    return v;
  }

  function format(n){
    if(n == null) return '';
    if(!Number.isFinite(n)) return n > 0 ? '∞' : n < 0 ? '-∞' : 'NaN';
    if(n === 0) return '0';
    const a = Math.abs(n);
    if(a >= 1e15 || a < 1e-9) return n.toExponential(9).replace(/\.?0+e/, 'e').replace('e+', 'e');
    const s = String(parseFloat(n.toPrecision(12)));
    const [i, f] = s.split('.');
    return i.replace(/\B(?=(\d{3})+(?!\d))/g, ',') + (f ? '.' + f : '');
  }

  // ---- the screen ------------------------------------------------------------------------------
  const HIST_KEY = 'pc_calc_history';
  const esc = s => String(s == null ? '' : s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const st = { expr: '', deg: true, sci: false, hist: [], justEvaluated: false };
  try{ const h = JSON.parse(localStorage.getItem(HIST_KEY) || '[]'); if(Array.isArray(h)) st.hist = h.slice(0, 50); }catch(_){}
  try{ const o = JSON.parse(localStorage.getItem(HIST_KEY + ':opts') || '{}'); if(o && typeof o === 'object'){ st.deg = o.deg !== false; st.sci = !!o.sci; } }catch(_){}
  const saveOpts = () => { try{ localStorage.setItem(HIST_KEY + ':opts', JSON.stringify({ deg: st.deg, sci: st.sci })); }catch(_){} };

  const KEYS = [
    ['AC','clear','fn'], ['⌫','back','fn'], ['( )','paren','fn'], ['÷','/','op'],
    ['7','7'], ['8','8'], ['9','9'], ['×','*','op'],
    ['4','4'], ['5','5'], ['6','6'], ['−','-','op'],
    ['1','1'], ['2','2'], ['3','3'], ['+','+','op'],
    ['±','neg','fn'], ['0','0'], ['.','.'], ['=','eq','eq'],
  ];
  const SCI = [
    ['sin','sin('], ['cos','cos('], ['tan','tan('], ['π','π'], ['e','e'],
    ['ln','ln('], ['log','log('], ['√','sqrt('], ['x²','²'], ['xʸ','^'],
    ['n!','!'], ['%','%'], ['(', '('], [')', ')'], ['1/x','inv'],
  ];

  let root = null, onKey = null;

  function preview(){
    if(!st.expr) return { ok: true, text: '' };
    try{ const v = evaluate(st.expr, { deg: st.deg }); return { ok: true, text: v == null ? '' : format(v) }; }
    catch(e){ return { ok: false, text: '' }; }
  }
  function paint(){
    if(!root || !root.isConnected) return;
    const d = root.querySelector('.calc-expr'), r = root.querySelector('.calc-res');
    d.textContent = st.expr || '0';
    const pv = preview();
    r.textContent = pv.text && pv.text !== st.expr ? '= ' + pv.text : '';
    root.querySelector('.calc-deg').textContent = st.deg ? 'DEG' : 'RAD';
    root.querySelector('.calc-deg').setAttribute('aria-pressed', st.deg ? 'true' : 'false');
    root.querySelector('.calc-sci-tog').setAttribute('aria-pressed', st.sci ? 'true' : 'false');
    root.querySelector('.calc-sci').hidden = !st.sci;
    const tape = root.querySelector('.calc-tape');
    tape.innerHTML = st.hist.length
      ? st.hist.map((h, i) => `<button class="calc-tape-row" data-hist="${i}" title="Use this result"><span>${esc(h.e)}</span><b>= ${esc(h.r)}</b></button>`).join('')
      : '<div class="calc-tape-empty">NO HISTORY</div>';
    d.scrollLeft = d.scrollWidth;
  }
  function err(msg){
    const r = root && root.querySelector('.calc-res');
    if(r){ r.textContent = '⚠ ' + msg; r.classList.add('calc-err'); setTimeout(() => r.classList.remove('calc-err'), 900); }
    const scr = root && root.querySelector('.calc-screen');
    if(scr){ scr.classList.remove('calc-glitch'); void scr.offsetWidth; scr.classList.add('calc-glitch'); }
  }
  function openParens(){ let n = 0; for(const c of st.expr){ if(c === '(') n++; else if(c === ')') n--; } return n; }
  function press(k){
    const last = st.expr.slice(-1);
    if(k === 'clear'){ st.expr = ''; st.justEvaluated = false; }
    else if(k === 'back'){ st.expr = st.expr.replace(/(sin|cos|tan|ln|log|sqrt)\($|.$/, ''); }
    else if(k === 'eq'){
      if(!st.expr) return paint();
      let e = st.expr; const open = openParens(); if(open > 0) e += ')'.repeat(open);
      try{
        const v = evaluate(e, { deg: st.deg });
        const r = format(v);
        st.hist.unshift({ e, r }); st.hist = st.hist.slice(0, 50);
        try{ localStorage.setItem(HIST_KEY, JSON.stringify(st.hist)); }catch(_){}
        st.expr = Number.isFinite(v) ? String(parseFloat(v.toPrecision(12))) : '';
        st.justEvaluated = true;
      }catch(e2){ err(e2.message || 'error'); return; }
    }
    else if(k === 'paren'){ st.expr += (openParens() > 0 && /[0-9)π e!²%]$/.test(st.expr)) ? ')' : '('; }
    else if(k === 'neg'){
      const m = /(-?)(\d*\.?\d+(?:e[+-]?\d+)?)$/.exec(st.expr);
      if(m){ st.expr = st.expr.slice(0, m.index) + (m[1] ? '' : '(-') + m[2] + (m[1] ? '' : ')'); }
      else st.expr += '-';
    }
    else if(k === 'inv'){ st.expr = st.expr ? '1/(' + st.expr + ')' : '1/'; }
    else {
      // A digit straight after "=" starts a new calculation; an operator continues from the result.
      if(st.justEvaluated && /^[0-9.(πe]|^(sin|cos|tan|ln|log|sqrt)\(/.test(k)) st.expr = '';
      if('+*/^%'.includes(k) && '+-*/^'.includes(last) && last) st.expr = st.expr.slice(0, -1);
      st.expr += k;
    }
    if(k !== 'eq') st.justEvaluated = false;
    paint();
  }
  const KEYMAP = { Enter: 'eq', '=': 'eq', Backspace: 'back', Escape: 'clear', Delete: 'clear',
                   '*': '*', x: '*', X: '*', '/': '/', '+': '+', '-': '-', '^': '^', '%': '%',
                   '(': '(', ')': ')', '.': '.', ',': '.', '!': '!', p: 'π', e: 'e', s: 'sin(', c: 'cos(', t: 'tan(',
                   l: 'ln(', r: 'sqrt(' };
  function keydown(e){
    if(!root || !root.isConnected){ document.removeEventListener('keydown', onKey, true); onKey = null; return; }
    if(e.ctrlKey || e.metaKey || e.altKey) {
      if((e.ctrlKey || e.metaKey) && (e.key === 'c' || e.key === 'C') && !String(window.getSelection() || '')){ e.preventDefault(); copy(); }
      return;
    }
    const t = e.target;
    if(t && t !== document.body && (t.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName || '')) && !root.contains(t)) return;
    if(document.querySelector('#modal-root .modal-bg')) return;
    // Only while it is on screen and, on the desktop, while ITS window is the focused one: keys typed
    // at another window must not become digits here.
    if(!root.offsetParent) return;
    const win = root.closest('.osw'); if(win && !win.classList.contains('focused')) return;
    const k = /^[0-9]$/.test(e.key) ? e.key : KEYMAP[e.key];
    if(!k) return;
    e.preventDefault(); e.stopPropagation();
    press(k);
    const b = root.querySelector(`[data-k="${CSS.escape(k)}"]`);
    if(b){ b.classList.add('calc-hit'); setTimeout(() => b.classList.remove('calc-hit'), 110); }
  }
  function copy(){
    const pv = preview(); const text = (pv.text || st.expr || '0').replace(/,/g, '');
    const PC = window.__PC || {};
    if(PC.copyValue) PC.copyValue(text, 'copied ' + text);
    else { try{ navigator.clipboard.writeText(text); }catch(_){} }
  }

  function render(){
    const feed = document.getElementById('feed');
    if(!feed) return;
    try{ const t = document.getElementById('view-title'); if(t) t.textContent = 'Calculator'; }catch(_){}
    feed.innerHTML = `<div class="calc-app" role="application" aria-label="Calculator">
      <div class="calc-main">
        <div class="calc-screen" aria-live="polite">
          <div class="calc-top"><span class="calc-brand">CALC//01</span>
            <button class="calc-chip calc-deg" type="button" title="Degrees or radians for sin/cos/tan"></button>
            <button class="calc-chip calc-sci-tog" type="button" title="Scientific keys">SCI</button>
            <button class="calc-chip calc-copy" type="button" title="Copy the result (Ctrl+C)">COPY</button></div>
          <div class="calc-expr" aria-label="Expression"></div>
          <div class="calc-res" aria-label="Result"></div>
        </div>
        <div class="calc-sci" hidden>${SCI.map(([l, k]) => `<button class="calc-key calc-sk" type="button" data-k="${esc(k)}">${esc(l)}</button>`).join('')}</div>
        <div class="calc-pad">${KEYS.map(([l, k, c]) => `<button class="calc-key${c ? ' calc-' + c : ''}" type="button" data-k="${esc(k)}" aria-label="${esc(l === '⌫' ? 'Backspace' : l === 'AC' ? 'Clear' : l)}">${esc(l)}</button>`).join('')}</div>
      </div>
      <aside class="calc-side"><div class="calc-side-hd"><span>TAPE</span><button class="calc-chip calc-clear-hist" type="button">CLEAR</button></div>
        <div class="calc-tape"></div></aside>
    </div>`;
    root = feed.querySelector('.calc-app');
    root.addEventListener('click', e => {
      const b = e.target.closest('button'); if(!b) return;
      if(b.dataset.k){ press(b.dataset.k); return; }
      if(b.classList.contains('calc-deg')){ st.deg = !st.deg; saveOpts(); paint(); return; }
      if(b.classList.contains('calc-sci-tog')){ st.sci = !st.sci; saveOpts(); paint(); return; }
      if(b.classList.contains('calc-copy')){ copy(); return; }
      if(b.classList.contains('calc-clear-hist')){ st.hist = []; try{ localStorage.removeItem(HIST_KEY); }catch(_){} paint(); return; }
      if(b.dataset.hist != null){ const h = st.hist[+b.dataset.hist]; if(h){ st.expr = h.r.replace(/,/g, ''); st.justEvaluated = true; paint(); } }
    });
    if(onKey) document.removeEventListener('keydown', onKey, true);
    onKey = keydown;
    document.addEventListener('keydown', onKey, true);
    paint();
  }

  const api = { evaluate, format, tokenize, render, press: k => press(k), state: () => ({ ...st }) };
  if(typeof window !== 'undefined') window.PCCalc = api;
  if(typeof module !== 'undefined') module.exports = api;
})();
