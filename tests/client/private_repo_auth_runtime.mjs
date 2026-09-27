/* The shipped relay transport signs in ONCE when a query for THIS account's repositories is answered
 * auth-required -- the relay withholds a private repo's 30617 from an unauthenticated connection --
 * and never signs for somebody else's repositories or for a broad listing. */
import fs from 'fs';
import vm from 'vm';

const src = fs.readFileSync(new URL('../../static/js/client/relay.js', import.meta.url), 'utf8');
const sleep = ms => new Promise(r => setTimeout(r, ms));
class WS {
  constructor(url){ this.url = url; this.readyState = 0; this.sent = []; WS.all.push(this);
    setTimeout(() => { this.readyState = 1; this.onopen && this.onopen(); }, 0); }
  send(raw){ this.sent.push(JSON.parse(raw)); }
  close(){ this.readyState = 3; }
  reply(msg){ this.onmessage && this.onmessage({ data: JSON.stringify(msg) }); }
}
WS.all = [];
const ctx = { console, setTimeout, clearTimeout, setInterval, clearInterval, WebSocket: WS, URL, atob, btoa,
  Worker: class { constructor(){} postMessage(){} }, document: { addEventListener(){}, hidden: false },
  navigator: { onLine: true } };
ctx.window = ctx; ctx.self = ctx; ctx.globalThis = ctx;
vm.createContext(ctx); vm.runInContext(src, ctx);

const R = ctx.Relay, fail = m => { throw new Error(m); };
const ME = 'a'.repeat(64), OTHER = 'd'.repeat(64);
let signed = 0;
R.setAuthSigner(async tpl => { signed++; return { ...tpl, pubkey: ME, id: String(signed).padStart(64, 'f'), sig: 'e'.repeat(128) }; }, () => ME);
R.configure({ urls: ['wss://relay.example/relay'], verify: false });
await sleep(20);
const ws = WS.all[0];
ws.reply(['AUTH', 'challenge-1']);

// Somebody else's repositories (their profile, a stranger's list): never a signature.
let otherEnded = 0;
const other = R.subscribe([{ kinds: [30617], authors: [OTHER] }], { onEvent(){}, onEose(){ otherEnded++; }, live: false });
ws.reply(['CLOSED', other, 'auth-required: private repositories need AUTH as a maintainer or reader']);
await sleep(20);
if(signed !== 0) fail('listing another account\'s repositories asked the signer to sign');
if(otherEnded !== 1) fail('a refusal nobody here can answer left the query waiting for ever');

// The Git page's own-repos query: sign once, ask again, get the private repo.
const got = [];
const id = R.subscribe([{ kinds: [30617], authors: [ME], limit: 500 }],
                       { onEvent(e){ got.push(e); }, onEose(){}, live: false });
ws.reply(['CLOSED', id, 'auth-required: private repositories need AUTH as a maintainer or reader']);
await sleep(20);
const auth = ws.sent.find(m => m[0] === 'AUTH');
if(!auth) fail('the owner\'s own repositories never authenticated -- private repos stay invisible');
if(auth[1].pubkey !== ME) fail('signed as somebody other than the account');
ws.reply(['OK', auth[1].id, true, '']);
await sleep(20);
const reqs = ws.sent.filter(m => m[0] === 'REQ' && m[1] === id);
if(reqs.length !== 2) fail(`the own-repos REQ was not replayed exactly once (${reqs.length})`);
if(signed !== 1) fail(`one challenge caused ${signed} signatures`);
console.log('ok');
process.exit(0);
