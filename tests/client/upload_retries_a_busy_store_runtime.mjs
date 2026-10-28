/* A BUSY STORE IS RETRIED, NOT LOST (2026-10-27: six uploads in a row lost while nas's backup saturated its disks).
 * The SHIPPED upload.js uploadBlob, sliced out, against a scripted fetch: our own server answering 503 + Retry-After is
 * sent again (as long as it asks, 2-30 s) up to three attempts; any other refusal, and another server's 503, is not. */
import fs from 'node:fs'; import vm from 'node:vm'; import assert from 'node:assert/strict';
const src = fs.readFileSync('static/js/client/upload.js', 'utf8');
const OWN = 'https://node.test/blossom';
function run(answers, { server = OWN } = {}){
  const calls = [], waits = [];
  const ctx = {
    console, JSON, Math, Promise, Number, String, Object, Array, Error, encodeURIComponent, Date,
    btoa: s => Buffer.from(String(s), 'binary').toString('base64'),
    setTimeout: (f, ms) => { waits.push(ms); return setTimeout(f, 0); },
    S: { _blossomOK: true, _uploadBatchAuth: null }, ClientSettings: { get: () => false },
    checkBlossomAccess: async () => {}, uploadTarget: () => ({ url: server, proto: 'blossom' }),
    _blossomBuiltin: () => ({ url: OWN }), compressMedia: async f => f, stripImageMetadata: async f => f,
    extFor: () => 'png', _notePostedAudio: async () => {}, hashFileHex: async () => 'ab'.repeat(32), sign: async () => ({ kind: 24242 }), CHUNK_OVER: 1e12,
    _putChunked: async () => null, uploadNip96: async () => { throw new Error('nip96'); },
    fetch: async (url, init) => { calls.push(init && init.method); const a = answers.shift();
      return { ok: a.status < 300, status: a.status, headers: { get: h => (a.headers || {})[h.toLowerCase()] || null },
               text: async () => a.body || '', json: async () => ({ url: OWN + '/' + 'ab'.repeat(32) + '.png', sha256: 'ab'.repeat(32) }) }; },
  };
  vm.createContext(ctx);
  // _uploadRefusal and uploadBlob, which follows it, up to the next top-level declaration (a regex and the strings
  // in them hold braces a counter would trip on).
  const i = src.indexOf('  function _uploadRefusal('), j = src.indexOf('  function _fileUnder(');
  assert.ok(i > 0 && j > i && src.slice(i, j).includes('  async function uploadBlob('), 'upload.js moved under this test');
  vm.runInContext(src.slice(i, j) + '\nthis.up = uploadBlob;', ctx);
  return { up: () => ctx.up({ name: 'a.png', type: 'image/png', size: 10 }, {}), calls, waits };
}
const busy = { status: 503, headers: { 'retry-after': '30' }, body: '{"detail":"storage temporarily unavailable, retry shortly"}' };

// Busy twice, then stored: the upload succeeds, having waited as long as the server asked.
{ const r = run([busy, busy, { status: 200 }]); const d = await r.up();
  assert.ok(typeof d === 'string' && d.startsWith(OWN), 'the upload was lost on a busy store'); assert.equal(r.calls.length, 3);
  assert.deepEqual(r.waits.filter(ms => ms >= 2000), [30000, 30000], 'did not wait as long as Retry-After asked'); }
// Busy three times: it gives up and says why -- three attempts in all, never a loop.
{ const r = run([busy, busy, busy, { status: 200 }]); await assert.rejects(r.up(), /storage temporarily unavailable/);
  assert.equal(r.calls.length, 3); }
// A refusal that is not "busy" is not retried.
{ const r = run([{ status: 500, body: 'storage error' }, { status: 200 }]); await assert.rejects(r.up(), /storage error/);
  assert.equal(r.calls.length, 1, 'a 500 was retried'); }
// Somebody else's server: its 503 is its own business -- not retried.
{ const r = run([busy, { status: 200 }], { server: 'https://other.test' }); await assert.rejects(r.up());
  assert.equal(r.calls.length, 1, "another server's 503 was retried"); }
// No Retry-After: a short default wait, never zero.
{ const r = run([{ status: 503 }, { status: 200 }]); await r.up(); assert.deepEqual(r.waits.filter(ms => ms >= 2000), [5000]); }
console.log('OK');
