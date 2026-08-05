/* REAL two-browser bookmark-sync test. Two Brave instances running the built extension
 * (extension/dist/chrome), a local nostr relay (relay.js), driven over the Chrome DevTools Protocol.
 *
 * This exists because a single-engine mock CANNOT catch the bugs that actually bit users: the socket
 * hands events to the engine CONCURRENTLY (a mock awaits each), and a local publish RACES the remote
 * absorb of the same URL. Both duplicated and/or crashed the browser at scale, and both are invisible
 * without two real browsers on a real relay.
 *
 * Usage: node two_browser_live.js   -> prints one JSON line per scenario, non-zero exit on failure.
 * Skips (exit 0 with a skip line) when node<18 (no global fetch/WebSocket), Brave, or the build is
 * missing — so it is safe to run anywhere; the pytest wrapper treats a skip as skipped.
 */
'use strict';
const cp = require('child_process'), path = require('path'), fs = require('fs');
const ROOT = path.resolve(__dirname, '..', '..');
const EXT = path.join(ROOT, 'extension', 'dist', 'chrome');
const wait = ms => new Promise(r => setTimeout(r, ms));

function findBrave() {
  const c = [
    'C:/Program Files/BraveSoftware/Brave-Browser/Application/brave.exe',
    'C:/Program Files (x86)/BraveSoftware/Brave-Browser/Application/brave.exe',
    process.env.LOCALAPPDATA && (process.env.LOCALAPPDATA.replace(/\\/g, '/') + '/BraveSoftware/Brave-Browser/Application/brave.exe'),
    '/usr/bin/brave-browser', '/usr/bin/brave', '/snap/bin/brave',
    '/Applications/Brave Browser.app/Contents/MacOS/Brave Browser',
  ].filter(Boolean);
  return c.find(p => { try { return fs.existsSync(p); } catch (_) { return false; } });
}
function skip(why) { console.log(JSON.stringify({ skip: why })); process.exit(0); }

if (typeof fetch !== 'function' || typeof WebSocket !== 'function') skip('node too old (need global fetch + WebSocket, node 21+)');
const BRAVE = findBrave();
if (!BRAVE) skip('Brave not found');
if (!fs.existsSync(path.join(EXT, 'manifest.json'))) skip('extension/dist/chrome not built');

const RELAY_PORT = 7500;
const RELAY = `ws://127.0.0.1:${RELAY_PORT + 1000}`, STATS = `http://127.0.0.1:${RELAY_PORT + 1000}`;
// Unpacked-extension id is derived from the load path; discover it from the SW target instead of hardcoding.
let EXTID = null;

function launch(profile, port) {
  return cp.spawn(BRAVE, ['--headless=new', '--disable-gpu', '--no-first-run', '--no-default-browser-check',
    '--disable-brave-update', '--disable-features=BraveRewards,BraveAds',
    `--user-data-dir=${path.join(require('os').tmpdir(), profile)}`, `--load-extension=${EXT}`,
    `--remote-debugging-port=${port}`, 'about:blank'], { detached: true, stdio: 'ignore' });
}
async function attach(port) {
  let sw = null;
  for (let i = 0; i < 60 && !sw; i++) {
    try {
      const list = await (await fetch(`http://127.0.0.1:${port}/json/list`)).json();
      sw = list.find(t => t.type === 'service_worker' && /background-chrome\.js/.test(t.url));
      if (sw) EXTID = EXTID || sw.url.split('/')[2];
      else if (EXTID) await fetch(`http://127.0.0.1:${port}/json/new?chrome-extension://${EXTID}/popup.html`, { method: 'PUT' }).catch(() => {});
    } catch (_) {}
    if (!sw) await wait(500);
  }
  if (!sw) throw new Error('no service worker on ' + port);
  const ws = new WebSocket(sw.webSocketDebuggerUrl); let id = 0; const p = new Map(); const errs = [];
  ws.onmessage = e => { const m = JSON.parse(e.data); if (m.id && p.has(m.id)) { p.get(m.id)(m); p.delete(m.id); }
    if (m.method === 'Runtime.exceptionThrown') errs.push(m.params.exceptionDetails.exception?.description || m.params.exceptionDetails.text); };
  const send = (method, params = {}) => new Promise(r => { const i = ++id; p.set(i, r); ws.send(JSON.stringify({ id: i, method, params })); });
  await new Promise(r => ws.onopen = r); await send('Runtime.enable');
  const ev = async (expr) => { const r = await send('Runtime.evaluate', { expression: `(async()=>{ ${expr} })()`, returnByValue: true, awaitPromise: true });
    if (r.result && r.result.exceptionDetails) throw new Error('EVAL THREW: ' + (r.result.exceptionDetails.exception?.description || JSON.stringify(r.result.exceptionDetails)));
    return 'value' in r.result.result ? r.result.result.value : r.result.result.description; };
  return { ws, ev, errs, keepAlive: setInterval(() => send('Runtime.evaluate', { expression: 'chrome.storage.local.get("cfg")', awaitPromise: true }).catch(() => {}), 6000) };
}
async function pair(b, cfg) { await b.ev(`
  cfg = ${JSON.stringify(cfg)}; key = V.fromB64(cfg.key); userRelays = ${JSON.stringify([RELAY])};
  await B.storage.local.set({ cfg, relays: userRelays, items: [] });
  await initBookmarks(); connect(); await self.PCBookmarks.engine.setEnabled(true); return 'ok'; `); }
const countUrl = async (b, u) => JSON.parse(await b.ev(`let c=0;const w=(ns)=>ns.forEach(n=>{if(n.url===${JSON.stringify(u)})c++;if(n.children)w(n.children)});w((await chrome.bookmarks.getTree())[0].children);return JSON.stringify(c);`));
const listUrls = async (b) => JSON.parse(await b.ev(`const u=[];const w=(ns)=>ns.forEach(n=>{if(n.url)u.push(n.url);if(n.children)w(n.children)});w((await chrome.bookmarks.getTree())[0].children);return JSON.stringify(u.sort());`));
const stats = async () => (await (await fetch(STATS)).json());

const results = [];
const check = (name, ok, detail) => { results.push({ name, ok: !!ok, detail }); };
async function reachesWithin(getter, want, secs) { for (let i = 0; i < secs; i++) { await wait(1000); if ((await getter()).includes(want)) return i + 1; } return -1; }

(async () => {
  const relay = cp.fork(path.join(__dirname, 'relay.js'), [String(RELAY_PORT)], { stdio: 'ignore' });
  await wait(700);
  const pA = launch('pcai-live-A', 9360), pB = launch('pcai-live-B', 9361);
  let A, B;
  try {
    A = await attach(9360); B = await attach(9361);
    const gen = JSON.parse(await A.ev(`const sk=NostrTools.generateSecretKey();const skhex=Array.from(sk).map(b=>b.toString(16).padStart(2,'0')).join('');const pk=NostrTools.getPublicKey(sk);const vk=crypto.getRandomValues(new Uint8Array(32));return JSON.stringify({skhex,pk,vkb64:btoa(String.fromCharCode.apply(null,vk))});`));
    const cfg = { pubkey: gen.pk, key: gen.vkb64, relay: '', relays: [RELAY], mode: 'full', sk: gen.skhex };
    await pair(A, cfg); await pair(B, cfg); await wait(1500);

    // 1. add on A -> reaches B in real time
    await A.ev(`await chrome.bookmarks.create({parentId:'1',title:'FromA',url:'https://from-a.example/'});return 1;`);
    check('an add on A reaches B', (await reachesWithin(() => listUrls(B), 'https://from-a.example/', 15)) > 0);

    // 2. same URL created on BOTH before they see each other -> one each, no dup (the "Poster-Chan" case)
    await Promise.all([
      A.ev(`await chrome.bookmarks.create({parentId:'1',title:'Same',url:'https://same.example/'});return 1;`),
      B.ev(`await chrome.bookmarks.create({parentId:'2',title:'Same',url:'https://same.example/'});return 1;`),
    ]);
    await wait(6000);
    check('same url on both -> one each (no concurrent-race dup)', (await countUrl(A, 'https://same.example/')) === 1 && (await countUrl(B, 'https://same.example/')) === 1,
      { a: await countUrl(A, 'https://same.example/'), b: await countUrl(B, 'https://same.example/') });

    // 3. a settled pair goes quiet — no edit-war publish storm
    const s0 = (await stats()).events; await wait(18000); const s1 = (await stats()).events;
    check('a settled pair goes quiet (no publish storm)', (s1 - s0) <= 4, { publishesIn18s: s1 - s0 });

    // 4. delete on A -> gone on both, stays dead
    await A.ev(`const h=(await chrome.bookmarks.search({url:'https://from-a.example/'}))[0];if(h)await chrome.bookmarks.remove(h.id);return 1;`);
    await wait(6000);
    check('delete on A removes on both and stays dead', (await countUrl(A, 'https://from-a.example/')) === 0 && (await countUrl(B, 'https://from-a.example/')) === 0);

    // 5. deleting a DUPLICATE keeps the surviving copy synced (not "only on one browser")
    await A.ev(`await chrome.bookmarks.create({parentId:'1',title:'Dup',url:'https://dup.example/'});await chrome.bookmarks.create({parentId:'1',title:'Dup',url:'https://dup.example/'});return 1;`);
    await wait(5000);
    await A.ev(`const h=(await chrome.bookmarks.search({url:'https://dup.example/'}))[0];if(h)await chrome.bookmarks.remove(h.id);return 1;`);
    await wait(6000);
    check('deleting a duplicate keeps the survivor on both', (await countUrl(A, 'https://dup.example/')) === 1 && (await countUrl(B, 'https://dup.example/')) === 1,
      { a: await countUrl(A, 'https://dup.example/'), b: await countUrl(B, 'https://dup.example/') });

    check('no service-worker errors', A.errs.length === 0 && B.errs.length === 0, { a: A.errs, b: B.errs });
  } catch (e) {
    check('harness ran', false, { error: e.message });
  } finally {
    try { clearInterval(A?.keepAlive); clearInterval(B?.keepAlive); A?.ws.close(); B?.ws.close(); } catch (_) {}
    try { process.kill(pA.pid); } catch (_) {} try { process.kill(pB.pid); } catch (_) {}
    try { relay.kill(); } catch (_) {}
  }
  console.log(JSON.stringify(results));
  process.exit(results.length && results.every(r => r.ok) ? 0 : 1);
})();
