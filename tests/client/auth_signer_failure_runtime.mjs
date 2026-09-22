import fs from 'node:fs';
import { clientSource, clientSourceAt, installStateGlobals } from './client_source.mjs';

const app = clientSource();
const authStart = app.indexOf('  let _aiAuth = null;');
  // The AI-auth block ends where app.js's entry points for the split modules begin: the admin
  // panel comment that used to follow it moved to discover.js.
const authEnd = app.indexOf('  function _aiDeps(){', authStart);
const themeStart = app.indexOf('  async function loadThemeFromServer(){');
const themeEnd = app.indexOf('  // PWA install:', themeStart);
const shipped = app.slice(authStart, authEnd) + app.slice(themeStart, themeEnd);

globalThis.window=globalThis;
globalThis.ME={pubkey:'a'.repeat(64)};
globalThis.applyTermGate=()=>{};
globalThis.applyTheme=()=>{};
globalThis._sendAdminToken=()=>{};
globalThis.sign=async()=>{ throw new Error('Firefox signer permission was denied'); };
globalThis.requests=[];
globalThis.fetch=async(url,opts={})=>{
  requests.push([url,opts.method||'GET']);
  throw new Error('protected fetch must not run');
};

const run=new Function(`return (async()=>{${shipped}
  let surfaced=''; try{await ensureAiSession();}catch(e){surfaced=e.message;}
  await loadThemeFromServer();
  return {surfaced,requests};
})()`);
process.stdout.write(JSON.stringify(await run()));
