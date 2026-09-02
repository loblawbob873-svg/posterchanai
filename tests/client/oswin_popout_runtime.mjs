/* Drives the SHIPPED oswin.js `open`/`routable` against a stub DOM, and the SHIPPED os.js
 * `popOutView` rule against the same nav, so the question "what view would this window open on"
 * is answered by the code that answers it in the app. */
import { readFileSync } from 'node:fs';
import { runInNewContext } from 'node:vm';

const OSWIN = new URL('../../static/js/client/oswin.js', import.meta.url);
const OS_JS = new URL('../../static/js/client/os.js', import.meta.url);

/** A document whose nav knows exactly these views — the list the desktop draws icons from. */
function stubDoc(navViews) {
  return {
    querySelector(sel) {
      const m = /^\.nav-item\[data-view="(.*)"\]$/.exec(sel);
      if (m) return navViews.includes(m[1]) ? { dataset: { view: m[1] } } : null;
      return null;
    },
    documentElement: { classList: { add() {} } },
  };
}

export function oswin({ nav = ['home', 'global', 'settings'], toplevels = true, wm = true } = {}) {
  const opened = [];
  const ctx = {
    console, JSON, Math, String, Object, Array, RegExp, Error, encodeURIComponent, URLSearchParams,
  };
  ctx.globalThis = ctx;
  ctx.document = stubDoc(nav);
  ctx.location = { pathname: '/client', search: '' };
  ctx.localStorage = { getItem: k => (k === 'pc_os_toplevels' && toplevels ? '1' : null) };
  if (wm) ctx.pcWM = { focus() {} };
  ctx.open = (url, target, features) => { opened.push({ url, features }); return { __stub: true }; };
  runInNewContext(readFileSync(OSWIN, 'utf8'), ctx, { filename: 'oswin.js' });
  return { api: ctx.PCOSWin, opened };
}

/** The shipped `popOutView` rule, lifted from os.js and run against the same stub nav. */
export function popOutView(win, nav = ['home', 'global', 'settings']) {
  const src = readFileSync(OS_JS, 'utf8');
  const start = src.indexOf('  function popOutView(w){');
  if (start < 0) throw new Error('popOutView is gone from os.js');
  const end = src.indexOf('\n  }', start) + 4;
  const ctx = { document: stubDoc(nav), String, RegExp };
  ctx.globalThis = ctx;
  runInNewContext(src.slice(start, end) + '\nresult = popOutView(WIN);',
                  Object.assign(ctx, { WIN: win, result: null }), { filename: 'os.js#popOutView' });
  return ctx.result;
}
