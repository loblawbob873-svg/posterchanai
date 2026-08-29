import fs from 'node:fs';

const app = fs.readFileSync(new URL('../../static/js/client/app.js', import.meta.url), 'utf8');
const authStart = app.indexOf('  let _aiAuth = null;');
const authEnd = app.indexOf('  // In-app Admin:', authStart);
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
