'use strict';
/* "FOR SOME REASON, USE MY OWN RELAYS GOT ENABLED AGAIN!" — twice.
 *
 * Runs the SHIPPED `seedRelaysFromNip65` against a stub ClientSettings and relay pool. Reading the
 * source is what missed this the first time: a fix went into `_persistAuthRelays` (the sign-in
 * box), which is a different function with the same symptom, and this one went on flipping the
 * switch every session.
 */
const fs = require('fs'), vm = require('vm'), assert = require('assert/strict'), path = require('path');
const root = path.resolve(__dirname, '../..');
const src = fs.readFileSync(root + '/static/js/client/app.js', 'utf8');

function slice(start, end) {
  const a = src.indexOf(start); assert(a >= 0, 'missing: ' + start);
  const b = src.indexOf(end, a + start.length); assert(b > a, 'missing: ' + end);
  return src.slice(a, b);
}

function setup(settings, nip65Tags, opts = {}) {
  const store = Object.assign({}, settings);
  const writes = [];
  /* NO HOST INTRINSICS. A vm context has its own Object/Array/Set; injecting the host's is the
     mistake this repo has paid for four times over, and it bit this very test first: the array the
     function built inside the realm failed `deepStrictEqual` against a host array on prototype
     alone. Only genuinely-host things are passed in, and everything that comes back out is rebuilt
     on this side by a JSON round trip. */
  const ctx = {
    console, URL, setTimeout,
    ClientSettings: {
      get: (k) => store[k],
      // Copy ACROSS the boundary on the way in, so assertions compare host values to host values.
      set: (k, v) => { const copy = JSON.parse(JSON.stringify(v)); writes.push([k, copy]); store[k] = copy; },
    },
    GUEST: false,
    ME: { pubkey: 'a'.repeat(64) },
    NostrTools: { verifyEvent: () => opts.verify !== false },
    userRelays: () => (store.relays || []).slice(),
    normalizeRelay: (u) => String(u || '').trim(),
    _nostrPrefsLoaded: false,
    _setRelays: [], drawRelayRows(){},
    connectRelays(){ writes.push(['connectRelays', true]); },
  };
  ctx.window = ctx;
  ctx._nip65Confirmed = false;
  ctx.Relay = {
    query: async () => {
      const evs = nip65Tags === null ? [] : [{
        kind: 10002, pubkey: ctx.ME.pubkey, id: 'e1', created_at: 10,
        tags: nip65Tags.map((u) => ['r', u]),
      }];
      evs.complete = opts.complete !== false;
      return evs;
    },
  };
  vm.createContext(ctx);
  vm.runInContext(
    'let _nip65Confirmed = false;'
    + slice('  function _relayTagUrls(ev){', '  /* Set up the relays from the')
    + slice('  async function seedRelaysFromNip65(){', '  function defaultRelays(){')
    + '\nthis.__run = seedRelaysFromNip65;', ctx);
  return { ctx, store, writes, run: () => ctx.__run() };
}

async function main() {
  let checks = 0;

  /* THE REPORT. An empty saved list is what you have right after switching your own relays OFF —
     `_dropLegacyAutoRelays` writes exactly `relays: []` + `relaysEnabled: false`. The seeder then
     read "no configuration to override" and switched it back on, every session. */
  {
    const h = setup({ relays: [], relaysEnabled: false }, ['wss://mine.example']);
    await h.run();
    assert.equal(h.store.relaysEnabled, false,
      'the seeder turned "use my own relays" on by itself — that is the report');
    assert.deepEqual(h.store.relays, ['wss://mine.example'],
      'it must still ADD what it found, so the relays are there the moment somebody asks');
    assert(!h.writes.some(([k]) => k === 'relaysEnabled'),
      'the switch was written at all: ' + JSON.stringify(h.writes));
    checks++;
  }

  /* Nobody who has never configured relays here has ASKED for their NIP-65 to replace the node's
     list — and most people have a kind-10002 published by some other client. */
  {
    const h = setup({}, ['wss://from-amethyst.example']);
    await h.run();
    assert.notEqual(h.store.relaysEnabled, true);
    assert.deepEqual(h.store.relays, ['wss://from-amethyst.example']);
    checks++;
  }

  /* With the switch already ON it stays on, the list grows, and the pool is redialled. */
  {
    const h = setup({ relays: ['wss://a.example'], relaysEnabled: true }, ['wss://b.example']);
    await h.run();
    assert.equal(h.store.relaysEnabled, true);
    assert.deepEqual(h.store.relays, ['wss://a.example', 'wss://b.example']);
    assert(h.writes.some(([k]) => k === 'connectRelays'), 'the pool changed and was not redialled');
    checks++;
  }

  /* NEVER SUBTRACTS. The saved list survives a NIP-65 that does not mention it. */
  {
    const h = setup({ relays: ['wss://keep.example'], relaysEnabled: true }, ['wss://new.example']);
    await h.run();
    assert(h.store.relays.includes('wss://keep.example'), 'a saved relay was dropped');
    checks++;
  }

  /* "Could not ask" changes nothing — the failure this function was written to avoid. */
  {
    const h = setup({ relays: [], relaysEnabled: false }, ['wss://x.example'], { complete: false });
    assert.equal(await h.run(), false);
    assert.equal(h.writes.length, 0, 'an incomplete read wrote something: ' + JSON.stringify(h.writes));
    checks++;
  }

  /* An unsigned kind-10002 is anybody's. The relay is untrusted. */
  {
    const h = setup({ relays: [], relaysEnabled: false }, ['wss://forged.example'], { verify: false });
    await h.run();
    assert.equal(h.writes.length, 0, 'a forged relay list was adopted');
    checks++;
  }

  /* The steady state writes nothing: re-running must not churn settings or redial. */
  {
    const h = setup({ relays: ['wss://a.example'], relaysEnabled: true }, ['wss://a.example']);
    await h.run();
    assert.equal(h.writes.length, 0, 'a no-op pass still wrote: ' + JSON.stringify(h.writes));
    checks++;
  }

  console.log('ok ' + checks + ' nip65 seed scenarios');
}
main().catch((e) => { console.error(e); process.exit(1); });
