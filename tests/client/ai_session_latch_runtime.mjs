/* A SLOW APP-SESSION MUST NOT BECOME A PERMANENT ONE.
 *
 * `_aiAuthP` dedupes concurrent callers so an external signer prompts once. It was also set BEFORE
 * the attempt and cleared only in a `finally`, so an attempt that never settled owned the view for
 * ever — and the Retry button, which calls back into the same function, adopted the very promise it
 * was meant to escape. Reported as "AI Chat just a spinning circle", on a LOCAL nsec as well as the
 * built-in signer: the wait was never about which signer was asked, but about who was allowed to
 * ask again.
 *
 * Drives the SHIPPED ensureAiSession, not a copy.
 */
import fs from 'node:fs';

const app = fs.readFileSync(new URL('../../static/js/client/app.js', import.meta.url), 'utf8');
const start = app.indexOf('  let _aiAuth = null;');
const end = app.indexOf('  // In-app Admin:', start);
if(start < 0 || end < 0) throw new Error('ensureAiSession moved');
const shipped = app.slice(start, end);

globalThis.window = globalThis;
globalThis.ME = { pubkey:'a'.repeat(64), mode:'local' };
globalThis.applyTermGate = () => {};
globalThis._sendAdminToken = () => {};

// The first sign() never answers — a signer that is asleep, or a request destroyed while a socket
// redialled. Every later one is instant, which is what a person pressing "Start over" experiences.
globalThis.signs = 0;
let releaseFirst;
globalThis.sign = async () => {
  globalThis.signs++;
  if(globalThis.signs === 1) return new Promise(r => { releaseFirst = () => r({ id:'late-proof' }); });
  return { id:'proof' };
};

globalThis.posts = 0;
globalThis.fetch = async (url) => {
  if(url !== '/api/auth/nostr-login') throw new Error('unexpected URL ' + url);
  globalThis.posts++;
  return { ok:true, status:200,
           json: async () => ({ access_token:'tok'+globalThis.posts, user:{ can_ai:true, username:'user'+globalThis.posts } }) };
};

const run = new Function('releaseAll', `return (async()=>{${shipped}
  const out = {};

  // 1. DEDUPE STILL HOLDS: two callers arriving together share one attempt, so ONE sign — which is
  //    the whole point (an external signer must not prompt twice). Measured as work done, not as
  //    promise identity: ensureAiSession is an async function, so every call returns its own wrapper
  //    and an identity check would pass while two signers were being asked.
  const a = ensureAiSession();
  const b = ensureAiSession();
  await new Promise(r => setTimeout(r, 10));
  out.signsAfterConcurrent = globalThis.signs;
  void b;

  // 2. …AND IT IS STILL STUCK, because the first sign has not answered.
  const raced = await Promise.race([a, new Promise(r => setTimeout(() => r('still-waiting'), 20))]);
  out.stuckWithoutForce = (raced === 'still-waiting');

  // 3. FORCE ESCAPES IT. This is the Start-over button; it must not adopt the pending promise.
  const forced = await ensureAiSession({force:true});
  out.forcedUsername = forced.username;
  out.signsAfterForce = globalThis.signs;

  // 4. The abandoned first attempt settling LATE must not clear the live session or the live
  //    in-flight slot — otherwise the next caller starts a third attempt (a third signer prompt).
  releaseAll();
  await new Promise(r => setTimeout(r, 10));
  const after = await ensureAiSession();
  out.afterLateSettleUsername = after.username;
  out.posts = globalThis.posts;
  return out;
})()`);
const result = await run(() => releaseFirst && releaseFirst());
process.stdout.write(JSON.stringify(result));
