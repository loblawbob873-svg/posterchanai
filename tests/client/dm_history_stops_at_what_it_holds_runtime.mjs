/* A RETURNING DEVICE DOES NOT RE-READ ITS WHOLE DM HISTORY.
 *
 * The history drain stopped only when a page brought "nothing new to the Store", and the Store does not keep gift
 * wraps across sessions -- so every visit paged the entire history again, 500 wraps a page. 106 such pages in 5
 * minutes were the relay's single biggest answer on 2026-10-17. The messages are in DmCache, which persists: a page
 * that is all cached ends the drain. A device whose cache is EMPTY must still read everything.
 * Runs the SHIPPED ensureDMs from dms.js.
 */
import vm from 'node:vm';import assert from 'node:assert/strict';
import { clientSourceAt, installStateGlobals } from './client_source.mjs';

const code = clientSourceAt(process.env.PC_APP_SOURCE || new URL('../../static/js/client/app.js', import.meta.url));
function fn(header){
  const i = code.indexOf(header); assert(i >= 0, 'gone: ' + header);
  let depth = 0;
  for (let k = code.indexOf('{', i); k < code.length; k++){
    if (code[k] === '{') depth++; else if (code[k] === '}' && --depth === 0) return code.slice(i, k + 1);
  }
}
const ensureDMs = fn('async function ensureDMs()');
const PAGES = 20, NOW = 1_800_000_000;
const wrap = (p, i) => ({ id: `w${p}-${i}`, kind: 1059, created_at: NOW - p * 1000 - i, tags: [], content: '' });

async function run(cached){
  const seen = { paged: 0 };
  const c = {
    console: { info(){}, warn(){}, log(){} },
    JSON, Object, Array, String, Number, Boolean, Promise, Math, Date, Set, Infinity, isFinite, setTimeout, clearTimeout,
    _dmLoaded: false, _dmHistoryReady: false, VIEW: 'messages', ME: { pubkey: 'me'.repeat(32) },
    signer: { nip17unwrap: () => null }, _watchDMs(){},
    DmCache: { pullShared: async () => 0, pushShared: async () => {},
               hasMany: async ids => new Set(cached ? ids : []) },
    _queueDmHistory: async () => {}, ingestDM(){}, ingestWrap: async () => {}, _scheduleDmRefresh(){},
    recountDmUnread(){}, renderMessages(){},
    Store: { byKind: () => [], saveEvent: () => true },           // the Store keeps no wraps across sessions
    Relay: {
      ready: async () => true,
      query: async filters => {
        const f = filters.find(x => x.kinds && x.kinds.includes(1059)) || {};
        if (f.until === undefined) return Array.from({ length: 400 }, (_, i) => wrap(0, i));
        seen.paged++;
        const p = seen.paged;
        return p > PAGES ? [] : Array.from({ length: 500 }, (_, i) => wrap(p, i));
      },
    },
  };
  // app.js _dmListRepaint: the list in front repaints as renderMessages (a parked desktop window is not modelled here)
  c._dmListRepaint = () => { if (c.VIEW === 'messages') c.renderMessages(); };
  c.window = c;
  vm.createContext(installStateGlobals(c) && c);
  vm.runInContext(ensureDMs + '\nglobalThis.__run = ensureDMs;', c);
  await c.__run();
  for (let last = -1, i = 0; i < 100 && last !== seen.paged; i++){ last = seen.paged; await new Promise(r => setTimeout(r, 10)); }
  return seen.paged;
}

const returning = await run(true);
assert.equal(returning, 1, `a device that already holds its history paged ${returning} times through the relay`);
const fresh = await run(false);
assert.equal(fresh, PAGES + 1, `a device with an empty cache stopped after ${fresh} pages -- history would be lost`);
console.log('ok: returning device 1 page, empty cache', fresh, 'pages');
