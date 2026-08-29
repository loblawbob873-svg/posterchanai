import fs from 'node:fs';

const app = fs.readFileSync(new URL('../../static/js/client/app.js', import.meta.url), 'utf8');
const start = app.indexOf('  let _aiAuth = null;');
const end = app.indexOf('  // In-app Admin:', start);
if(start < 0 || end < 0) throw new Error('ensureAiSession moved');
const shipped = app.slice(start, end);

globalThis.window = globalThis;
globalThis.posts = 0;
globalThis.ME = { pubkey:'a'.repeat(64) };
globalThis.sign = async () => ({ id:'proof' });
globalThis.applyTermGate = () => {};
globalThis._sendAdminToken = () => {};
globalThis.fetch = async (url) => {
  if(url !== '/api/auth/nostr-login') throw new Error('unexpected URL ' + url);
  globalThis.posts++;
  const body = globalThis.posts === 1
    ? { user:{ can_ai:true, username:'cached-without-token' } }
    : { access_token:'fresh-token', user:{ can_ai:true, username:'renewed' } };
  return { json: async () => body };
};

const run = new Function(`return (async()=>{${shipped}
  const first=await ensureAiSession();
  const second=await ensureAiSession();
  return {first:first.username, second:second.username, posts, token:_aiToken};
})()`);
process.stdout.write(JSON.stringify(await run()));
