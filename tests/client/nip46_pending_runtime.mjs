/* Runs the SHIPPED `_failPending` and the SHIPPED `ws.onclose` body against a stub session, so the
 * question "what happens to a request that was in flight when the relay died" is answered by the
 * code that answers it in the app. */
import { readFileSync } from 'node:fs';
import { runInNewContext } from 'node:vm';

const APP = new URL('../../static/js/client/app.js', import.meta.url);
const src = readFileSync(APP, 'utf8');

function slice(start, end) {
  const i = src.indexOf(start);
  if (i < 0) throw new Error('not found in app.js: ' + start);
  const j = src.indexOf(end, i);
  if (j < 0) throw new Error('end not found after: ' + start);
  return src.slice(i, j);
}

/** The real _failPending, plus the real body of ws.onclose, on a stub session. */
export function session({ sockets }) {
  const failPending = slice('    _failPending(why){', '\n    },');
  const onclose = slice('      ws.onclose = ()=>{', '\n      };');
  const body = onclose.slice(onclose.indexOf('{') + 1);

  const rejected = [];
  const ctx = { console: { warn() {}, log() {} }, Error, Map, Array, String };
  ctx.globalThis = ctx;
  const setup = `
    const self = {
      _socks: SOCKS,
      _pending: new Map(),
      _live(){ return (this._socks||[]).filter(w => w && w.readyState === 1); },
      _scheduleReopen(){ REOPENED.push(1); },
      ${failPending}
      },
      closeOne(ws, url){ ${body} },
    };
    for (const [id, r] of PENDING) self._pending.set(id, { rej: e => REJECTED.push([id, e.message]) });
    self.closeOne(DYING, 'wss://relay.example');
    RESULT = { pending: self._pending.size, rejected: REJECTED, reopened: REOPENED.length };
  `;
  Object.assign(ctx, {
    SOCKS: sockets.slice(), DYING: sockets[0],
    PENDING: [['a', 1], ['b', 2]], REJECTED: rejected, REOPENED: [], RESULT: null,
  });
  runInNewContext(setup, ctx, { filename: 'app.js#nip46' });
  return ctx.RESULT;
}

/** The phrases `_send` treats as FINAL (a user refusal) — anything else is retried. */
export function finalPhrases() {
  const send = slice('    _send(method, params, opts){', '\n    },');
  return [...send.matchAll(/m\.includes\('([^']+)'\)/g)].map(m => m[1]);
}
