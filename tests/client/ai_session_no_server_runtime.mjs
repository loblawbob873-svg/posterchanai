/* "Could not establish app session: login response did not contain a credential" -- a member's
 * Monero wallet on the APK, 2026-10-14. Drives the SHIPPED ensureAiSession:
 *   1. with NO server configured (relays only) it says so and sends nothing -- the POST used to go to
 *      the bundle itself, which answers 200 with its own HTML page;
 *   2. a 200 that is not our JSON names its status, type and host instead of "no credential". */
import fs from 'node:fs';
import assert from 'node:assert/strict';
const app = fs.readFileSync(new URL('../../static/js/client/app.js', import.meta.url), 'utf8');
const start = app.indexOf('  let _aiAuth = null;'), end = app.indexOf('  function _aiDeps(){', start);
if(start < 0 || end < 0) throw new Error('ensureAiSession moved');
const shipped = app.slice(start, end);
globalThis.window = globalThis;
globalThis.ME = { pubkey:'a'.repeat(64), mode:'local' };
globalThis.applyTermGate = () => {}; globalThis.applyMediaGate = () => {}; globalThis._sendAdminToken = () => {};
globalThis.sign = async () => ({ id:'proof' });
globalThis.location = { href:'https://localhost/index.html', origin:'https://localhost' };
let noServer = true, posts = 0;
globalThis._standalone = () => noServer;
globalThis.fetch = async (url) => { posts++;
  return { ok:true, status:200, url:'https://localhost'+url, headers:{ get:(h)=>h==='content-type'?'text/html; charset=utf-8':null },
           json: async () => { throw new SyntaxError('Unexpected token <'); } }; };
const run = new Function(`return (async()=>{${shipped}
  const out = {};
  try{ await ensureAiSession(); out.a = 'resolved'; }catch(e){ out.a = e.message; }
  out.postsA = globalThis.__posts();
  globalThis.__setServer();
  try{ await ensureAiSession({force:true}); out.b = 'resolved'; }catch(e){ out.b = e.message; }
  return out; })()`);
globalThis.__posts = () => posts; globalThis.__setServer = () => { noServer = false; };
const out = await run();
assert.match(out.a, /without a server/, 'relays-only mode did not say there is no server: ' + out.a);
assert.equal(out.postsA, 0, 'relays-only mode still POSTed a login to the bundle');
assert.match(out.b, /HTTP 200 text\/html from localhost/, 'a non-JSON 200 did not name what answered: ' + out.b);
assert.doesNotMatch(out.b, /did not contain a credential/);
console.log('ok');
