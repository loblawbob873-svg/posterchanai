/* A REFUSED SIGN-IN IS REMEMBERED, NOT RE-ASKED EVERY MINUTE (2026-10-22).
 *
 * poster.place had new-user registration closed, so `/api/auth/nostr-login` answered 403 for a new key -- and the
 * minute reminder poller called ensureAiSession(), which signed a fresh proof and asked again, for the life of the
 * page (a 403 every 60 s in the router log). The refusal also reached Telegram as "Not authenticated" instead of the
 * server's own sentence. Drives the SHIPPED ensureAiSession, not a copy.
 */
import { clientSource } from './client_source.mjs';

const app = clientSource();
const start = app.indexOf('  let _aiAuth = null;');
const end = app.indexOf('  function _aiDeps(){', start);
if(start < 0 || end < 0) throw new Error('ensureAiSession moved');
const shipped = app.slice(start, end);

globalThis.window = globalThis;
globalThis._standalone = () => false;
globalThis.ME = { pubkey:'a'.repeat(64), mode:'local' };
globalThis.applyTermGate = () => {};
globalThis.applyMediaGate = () => {};
globalThis._sendAdminToken = () => {};
globalThis.signs = 0;
globalThis.sign = async () => { globalThis.signs++; return { id:'proof'+globalThis.signs }; };
globalThis.posts = 0;
globalThis.answer = 'closed';
globalThis.fetch = async (url) => {
  if(url !== '/api/auth/nostr-login') throw new Error('unexpected URL ' + url);
  globalThis.posts++;
  if(globalThis.answer === 'closed')
    return { ok:false, status:403, url, headers:{get:()=>'application/json'}, json: async () => ({ detail:'New user registration is disabled on this server.' }) };
  return { ok:true, status:200, url, headers:{get:()=>'application/json'}, json: async () => ({ access_token:'tok', user:{ username:'u' } }) };
};

const run = new Function(`return (async()=>{${shipped}
  const out = {}, err = async f => { try{ await f(); return null; }catch(e){ return e; } };
  const e1 = await err(() => ensureAiSession());
  out.firstSaid = e1 && e1.message; out.firstRefused = !!(e1 && e1.refused); out.postsAfterFirst = globalThis.posts;
  for(let i = 0; i < 10; i++) await err(() => ensureAiSession());
  out.postsAfterTen = globalThis.posts; out.signsAfterTen = globalThis.signs;
  const e2 = await err(() => ensureAiSession());
  out.rememberedSays = e2 && e2.message; out.rememberedRefused = !!(e2 && e2.refused);
  await err(() => ensureAiSession({force:true}));
  out.postsAfterForce = globalThis.posts;
  ME.pubkey = 'b'.repeat(64);
  await err(() => ensureAiSession());
  out.postsAfterOtherAccount = globalThis.posts;
  globalThis.answer = 'open';
  const ok = await ensureAiSession({force:true});
  out.openedAs = ok && ok.username;
  return out;
})()`);

const out = await run();
const fail = m => { console.error('FAIL: ' + m + '\n' + JSON.stringify(out, null, 1)); process.exit(1); };
if(!/registration is disabled/.test(out.firstSaid || '') || !out.firstRefused) fail('the refusal did not carry the server\'s own sentence');
if(out.postsAfterFirst !== 1) fail('the first call should ask the server once');
if(out.postsAfterTen !== 1 || out.signsAfterTen !== 1) fail('a refused account was asked again (and signed again) on every call');
if(!/registration is disabled/.test(out.rememberedSays || '') || !out.rememberedRefused) fail('the remembered refusal lost its reason');
if(out.postsAfterForce !== 2) fail('Retry (force) must ask the server again');
if(out.postsAfterOtherAccount !== 3) fail('another account inherited this account\'s refusal');
if(out.openedAs !== 'u') fail('a later success did not open the session');
console.log('ai session refusal: ok');
