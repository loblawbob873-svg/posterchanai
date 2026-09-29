/* A live announcement whose session was superseded on the same stream key is OVER — shown ended
 * everywhere, and retired on the relays by its owner. The SHIPPED app.js status helpers and the
 * SHIPPED streams.js sweep, run under node.
 *
 * Reported (a git issue, with a screenshot of a profile's Streams tab): "Some livestreams I started on
 * poster.place can be seen even after I ended it." Measured on the relay: one account, one stream key,
 * a session from 01:58 still `status=live`, a newer session from 02:09 properly `ended`. The old one
 * went away without its `ended`, and the owner's clean-up probed the key's HLS feed — which answers
 * for whichever session is on it — so it never retired it.
 * Prints one JSON object: { scenario: {ok, detail} }.
 */
'use strict';
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const ROOT = path.resolve(__dirname, '..', '..');
const APP = fs.readFileSync(path.join(ROOT, 'static/js/client/app.js'), 'utf8');
const STREAMS = fs.readFileSync(path.join(ROOT, 'static/js/client/streams.js'), 'utf8');
// app.js's helpers are a let + three functions in one block; lift the block whole.
const a = APP.indexOf('  function rawStreamStatus(e)'), b = APP.indexOf('  function streamStatus(e)');
if(a < 0 || b < 0) throw new Error('the stream status helpers are missing from app.js');
const block = APP.slice(a, APP.indexOf('\n', b) + 1);

const ME = 'c0'.repeat(32), OTHER = 'ab'.repeat(32), KEY = '69f1a055ec8d20a8';
const ev = (pk, starts, status, created) => ({ id: pk.slice(0, 4) + starts, pubkey: pk, kind: 30311, created_at: created || starts,
  tags: [['d', KEY + '-' + starts], ['status', status], ['starts', String(starts + 60)], ['title', 'Testando PosterChan'],
         ['streaming', 'https://poster.place/api/streams/hls/' + KEY + '/index.m3u8']] });
const STALE = ev(ME, 1790646968, 'live', 1790647091);        // 01:58 — never ended
const NEWER = ev(ME, 1790647284, 'ended', 1790647771);       // 02:09 — ended properly
const LONE  = ev(OTHER, 1790640000, 'live');                  // somebody genuinely live

function statusHelpers(events){
  const ctx = { Date, Map, parseInt, Store: { byKind: k => events.filter(e => e.kind === k) } };
  vm.createContext(ctx);
  vm.runInContext(block + '\nthis.streamStatus=streamStatus; this.rawStreamStatus=rawStreamStatus;', ctx);
  return ctx;
}

const out = {};
async function run(name, fn){
  try{ const d = await fn(); out[name] = { ok: !!(d && d.ok), detail: d }; }
  catch(e){ out[name] = { ok: false, detail: String(e && e.stack || e) }; }
}

(async () => {
  await run('a superseded live session reads as ended; a genuine one still reads live', async () => {
    const H = statusHelpers([STALE, NEWER, LONE]);
    return { ok: H.streamStatus(STALE) === 'ended' && H.rawStreamStatus(STALE) === 'live'
                 && H.streamStatus(NEWER) === 'ended' && H.streamStatus(LONE) === 'live',
             stale: H.streamStatus(STALE), lone: H.streamStatus(LONE) };
  });

  await run('the only session on its key is not second-guessed', async () => {
    const H = statusHelpers([STALE]);
    return { ok: H.streamStatus(STALE) === 'live' };
  });

  await run('the owner retires the superseded session on the relays without probing the feed', async () => {
    const published = [], probes = [];
    const H = statusHelpers([STALE, NEWER]);
    const ctx = { console, Math, JSON, Promise, Map, Set, Array, Object, String, Number, Date, Error, parseInt,
                  setTimeout: (f, ms) => setTimeout(f, 0), clearTimeout, URL };
    ctx.window = ctx;
    ctx.fetch = async (u) => { probes.push(String(u)); return { ok: true, status: 200, text: async () => '#EXTM3U' }; };
    ctx.Relay = { query: async () => [STALE, NEWER], publishTo: async () => ({ ok: true }) };
    ctx.document = { addEventListener(){}, querySelector: () => null, getElementById: () => null };
    vm.createContext(ctx);
    vm.runInContext(STREAMS, ctx, { filename: 'streams.js' });
    const S = { GUEST: false, ME: { pubkey: ME } };
    const mod = ctx.PCStreamsFactory({
      state: S, NT: () => ({ verifyEvent: () => true }), STREAM_RELAYS: [], toast: () => {},
      publish: async (kind, content, tags) => { const e = { kind, content, tags }; published.push(e); return { ok: true, ev: e }; },
      rawStreamStatus: H.rawStreamStatus, streamStatus: H.streamStatus, streamSuperseded: () => false,
      _fetchTimeout: async (u) => ctx.fetch(u), _streamFetch: async (u) => ctx.fetch(u), enc: s => String(s),
    });
    await mod._sweepStaleOwnLive(true);
    await new Promise(r => setTimeout(r, 20));
    const ended = published.filter(p => p.kind === 30311 && p.tags.some(t => t[0] === 'status' && t[1] === 'ended'));
    const d = ended[0] && (ended[0].tags.find(t => t[0] === 'd') || [])[1];
    return { ok: ended.length === 1 && d === KEY + '-' + 1790646968 && !probes.length,
             ended: ended.length, d, probes };
  });

  process.stdout.write(JSON.stringify(out));
})();
