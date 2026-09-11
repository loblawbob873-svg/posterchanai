/* RUN a shipped EXTRA painter with NO WINDOW AROUND IT.
 *
 * This is the whole claim being tested. On PosterChanOS these apps are real compositor toplevels,
 * so the painter runs in a window document where there is no `w`, no `.osw-slot` and no frame to
 * close — and the only honest way to know it can is to call it that way. A text assertion that
 * `w.` does not appear says the painter compiles; it does not say it paints, and it says nothing at
 * all about the polling timer, which is the part that outlives a window and keeps calling the
 * system bridge for ever.
 */
import { readFileSync } from 'node:fs';
import { runInNewContext } from 'node:vm';

const OS_JS = new URL('../../static/js/client/os.js', import.meta.url);

/** A DOM element just real enough for these painters: children, a class list, and querySelector. */
function el(tag = 'div') {
  const node = {
    tag, children: [], _html: '', className: '', hidden: false, textContent: '', value: '',
    dataset: {}, style: { setProperty(){}, removeProperty(){} },
    classList: { _s: new Set(),
      add(...c){ c.forEach(x => this._s.add(x)); }, remove(...c){ c.forEach(x => this._s.delete(x)); },
      toggle(c, on){ on ? this._s.add(c) : this._s.delete(c); }, contains(c){ return this._s.has(c); } },
    get innerHTML(){ return node._html; },
    set innerHTML(v){ node._html = String(v); },
    querySelector(){ return el(); },
    querySelectorAll(){ return []; },
    appendChild(c){ node.children.push(c); return c; },
    addEventListener(){}, focus(){}, remove(){},
    getBoundingClientRect(){ return { left:0, top:0, width:800, height:600 }; },
  };
  return node;
}

/** Lift one painter out of the shipped os.js and run it against stubs. */
export function runPainter(name, { system = null, calls = [] } = {}) {
  const src = readFileSync(OS_JS, 'utf8');
  const start = src.indexOf(`  function ${name}(`);
  if (start < 0) throw new Error(`${name} is gone from os.js`);
  const body = src.slice(start, src.indexOf('\n  }', start) + 4);

  const timers = new Map();
  let seq = 0;
  const ctx = {
    console, JSON, Math, String, Number, Object, Array, Promise, Error, RegExp, Date,
    setTimeout: (fn) => { calls.push('setTimeout'); return ++seq; },
    clearTimeout(){},
    setInterval(fn, ms){ const id = ++seq; timers.set(id, { fn, ms }); return id; },
    clearInterval(id){ timers.delete(id); },
    $: (sel, root) => (root || el()).querySelector(sel),
    enc: (v) => String(v == null ? '' : v),
    _sysBytes: (n) => `${n}B`,
    PC: () => ({
      toast(){}, uiConfirm: async () => true, uiPrompt: async () => '',
      setRemoteDesktopArmed(v){ calls.push(`armed:${v}`); },
      setRemoteDesktopHost(v){ calls.push(`host:${v === null ? 'null' : 'set'}`); },
      viewer: () => null, startRemoteDesktop: async () => true,
    }),
    pcSystem: system,
    pcVM: { list: async () => ({ available: false, error: 'no libvirt here' }),
            details: async () => ({ ok: false }), pickIso: async () => '',
            create: async () => ({ ok: false }), action: async () => ({ ok: false }),
            view: async () => ({ ok: false }) },
  };
  ctx.globalThis = ctx;
  ctx.window = ctx;
  ctx.document = { getElementById: () => null, createElement: () => el(), body: el() };

  runInNewContext(body + `\nSLOT = SLOT_MAKER();\nSTOP = ${name}(SLOT${name === 'paintVmManager' ? ', null' : ''});`,
                  Object.assign(ctx, { SLOT_MAKER: el, SLOT: null, STOP: null }),
                  { filename: `os.js#${name}` });

  return {
    painted: String(ctx.SLOT._html || ''),
    stop: ctx.STOP,
    timers: () => timers.size,
    calls,
  };
}


/** Drive the shipped `renderExtra` — the thing a window document actually calls — against a stub
 *  `#feed`, so the routing, the host class, the paint and the one-timer rule are all measured at
 *  once. Views are rendered in the order given; the answer reports the state after each. */
export async function runRenderExtra(views) {
  const src = readFileSync(OS_JS, 'utf8');
  const painter = (name) => {
    const i = src.indexOf(`  function ${name}(`);
    if (i < 0) throw new Error(`${name} is gone from os.js`);
    return src.slice(i, src.indexOf('\n  }', i) + 4);
  };
  /* LIFT THE THREE PIECES BY NAME, never a span between two landmarks. `EXTRA_RENDER` moved up
     beside `EXTRA_WINDOWS` (it is read by `_extraOpensAsWindow`, and a const further down the file
     is in its temporal dead zone until then) — a slice from it to the PCOS export then swallowed
     eight thousand lines of unrelated desktop. */
  const mapAt = src.indexOf('  const EXTRA_RENDER = {');
  if (mapAt < 0) throw new Error('EXTRA_RENDER is gone from os.js');
  const map = src.slice(mapAt, src.indexOf('\n  };', mapAt) + 5);

  const feed = el();
  const timers = new Map();
  let seq = 0;
  const toasts = [];
  const ctx = {
    console, JSON, Math, String, Number, Object, Array, Promise, Error, RegExp, Date,
    setTimeout: () => ++seq, clearTimeout(){},
    setInterval(fn, ms){ const id = ++seq; timers.set(id, { fn, ms }); return id; },
    clearInterval(id){ timers.delete(id); },
    $: (sel, root) => (root || el()).querySelector(sel),
    enc: (v) => String(v == null ? '' : v),
    _sysBytes: (n) => `${n}B`,
    PC: () => ({ toast: (m) => toasts.push(String(m)), uiConfirm: async () => true,
                 uiPrompt: async () => '', adoptView(){}, setRemoteDesktopArmed(){},
                 setRemoteDesktopHost(){}, viewer: () => null, startRemoteDesktop: async () => true }),
    pcSystem: null,
    pcVM: { list: async () => ({ available: false, error: 'none' }), details: async () => ({ ok: false }),
            pickIso: async () => '', create: async () => ({ ok: false }),
            action: async () => ({ ok: false }), view: async () => ({ ok: false }) },
    /* System Settings has its own renderer and its own tests; stub it so this harness is about the
       routing and the teardown rule rather than about the settings screen. */
    renderSystemSettings: () => { feed.className = 'feed feed-ossettings';
                                  feed.innerHTML = '<settings/>'; return Promise.resolve(); },
    wins: [],
  };
  ctx.globalThis = ctx;
  ctx.window = ctx;
  ctx.window.addEventListener = () => {};
  ctx.document = { getElementById: (id) => (id === 'feed' ? feed : null),
                   createElement: () => el(), body: el() };

  const code = painter('paintTaskManager') + painter('paintVmManager') + painter('paintRemoteDesktop')
             + map + painter('_paintExtraInFeed') + painter('renderExtra');
  runInNewContext(code + '\nRENDER = renderExtra;', Object.assign(ctx, { RENDER: null }),
                  { filename: 'os.js#renderExtra' });

  const steps = [];
  for (const v of views) {
    const answered = ctx.RENDER(v);
    /* renderExtra answers immediately and paints on a MICROTASK — deliberately, so a caller
       that returns on `true` is not blocked on a bridge read. Drain before measuring, or
       this harness reports an unpainted host and agrees with a bug that is not there. */
    await new Promise(r => setTimeout(r, 0));
    steps.push({ view: v, answered, cls: feed.className, html: String(feed.innerHTML || '').length,
                 timers: timers.size });
  }
  return { steps, toasts };
}
