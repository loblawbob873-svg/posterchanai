/* MESSAGES OPENED FROM A LAUNCHER TILE MUST NOT SPEND ITS HISTORY READ ON A DEAD SOCKET.
 *
 *     "when I click on Messages from the android launcher, it does not load messages for me"
 *
 * The ROUTE was never the problem — the shipped phoneshell.js reaches `switchView('messages')` and
 * the screen paints correctly. What arrives empty is the conversation list, and the reason is the
 * rule CLAUDE.md already states for the timeline, the profile, the notification flush and Trending:
 * a REQ written to a CONNECTING socket is silently dropped by `relay.js _send`, so a view that
 * queries on entry must `await Relay.ready()` first.
 *
 * Messages is the view most exposed to that and was the one that never waited. A tile lands the
 * instant `pc-app-ready` fires, and `pc-app-ready` is dispatched in the same turn as
 * `connectRelays()` — the socket cannot be open yet, by construction. So `ensureDMs` fired its
 * history query into nothing, `_dmLoaded` latched TRUE, and the 60-second watcher (`if(!_dmLoaded)`)
 * never retried. An empty list, painted perfectly, with nothing in any log.
 *
 * Two halves, and the second is what makes the fix safe rather than merely quiet:
 *   * the one-shot HISTORY read waits, and reports "I could not ask" by leaving itself un-loaded;
 *   * the LIVE subscriptions do NOT wait — they re-arm themselves when a socket opens, so a message
 *     arriving during the gap is still delivered. Gating them would trade an empty list for a
 *     silent one.
 *
 * Runs the SHIPPED ensureDMs, extracted from app.js, so it cannot drift from what deploys.
 */
import fs from 'node:fs';import vm from 'node:vm';import assert from 'node:assert/strict';

const code = fs.readFileSync(process.env.PC_APP_SOURCE
  || new URL('../../static/js/client/app.js', import.meta.url), 'utf8');

function fn(header){
  const i = code.indexOf(header);
  assert(i >= 0, 'gone from app.js — re-read this test: ' + header);
  let depth = 0, j = code.indexOf('{', i);
  for (let k = j; k < code.length; k++){
    if (code[k] === '{') depth++;
    else if (code[k] === '}' && --depth === 0) return code.slice(i, k + 1);
  }
  throw new Error('unbalanced braces after ' + header);
}

const ensureDMs = fn('async function ensureDMs()');

function run({ ready }){
  const seen = { watched: 0, queried: 0, painted: 0, readyMs: null };
  const c = {
    console: { info(){}, warn(){}, log(){} },
    JSON, Object, Array, String, Number, Boolean, Promise, Math, Date, setTimeout, clearTimeout,
    _dmLoaded: false,
    _dmHistoryReady: false,
    VIEW: 'messages',
    ME: { pubkey: 'me'.repeat(32) },
    // A local key: `modern` is true, which is the path every current client takes.
    signer: { nip17unwrap: () => null },
    _watchDMs(){ seen.watched++; },
    DmCache: { pullShared: async () => 0, pushShared: async () => {} },
    _queueDmHistory: async () => {},
    ingestDM(){},
    ingestWrap: async () => {},
    _scheduleDmRefresh(){},
    recountDmUnread(){},
    renderMessages(){ seen.painted++; },
    Store: { byKind: () => [], saveEvent: () => true },
    Relay: {
      ready: async ms => { seen.readyMs = ms; return ready; },
      query: async () => { seen.queried++; return []; },
    },
  };
  c.window = c;
  vm.createContext(c);
  vm.runInContext(ensureDMs + '\nglobalThis.__run = ensureDMs;', c);
  return c.__run().then(() => ({ seen, loaded: c._dmLoaded }));
}

/* ---- 1. NO SOCKET: the history read is deferred, and says so by staying un-loaded ---- */
{
  const { seen, loaded } = await run({ ready: false });
  assert.equal(seen.queried, 0,
    'the history REQ went out on a socket that cannot answer — it is dropped and never EOSEs');
  assert.equal(loaded, false,
    '_dmLoaded latched true with no history, so nothing ever retries: an empty Messages for the '
    + 'rest of the session');
  assert(seen.readyMs > 3000,
    'the wait is the 3s default — a phone waking its radio needs longer than the boot itself');
}

/* ---- 2. LIVE DELIVERY IS NEVER GATED ON IT ---- */
{
  const { seen } = await run({ ready: false });
  assert.equal(seen.watched, 1,
    'the live subscriptions were skipped too — that trades an empty list for a SILENT one, and a '
    + 'live sub re-arms itself when the socket opens');
}

/* ---- 3. WITH A SOCKET, nothing changes: the read happens and the view loads ---- */
{
  const { seen, loaded } = await run({ ready: true });
  assert.equal(seen.queried > 0, true, 'the history read never happened on a healthy socket');
  assert.equal(loaded, true, 'a completed load must not ask again on every repaint');
  assert.equal(seen.watched, 1);
}

console.log('dm history: waits for a socket that can answer, keeps live delivery ungated, '
          + 'and reports "could not ask" instead of "no messages"');
process.exit(0);
