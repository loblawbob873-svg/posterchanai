/* The shipped relay transport must answer an auth-required refusal for a read that is bound to
 * this account as the RECIPIENT ("shared with me"), and must still refuse to sign for a filter that
 * is bound to neither — the prompt-storm guard nip78_auth_runtime.mjs pins from the other side. */
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

// "Shared with me": somebody ELSE's documents, p-tagged to this account. No author to bind.
let ended = 0;
const id = R.subscribe([{ kinds: [30078], '#p': [ME], '#l': ['pcai-musicshare'] }],
                       { onEvent(){}, onEose(){ ended++; }, live: true });
ws.reply(['CLOSED', id, 'auth-required: NIP-78 reads require AUTH as the author or the recipient']);
await sleep(20);
const auth = ws.sent.find(m => m[0] === 'AUTH');
if(!auth) fail('the client never authenticated for a read addressed to it — "shared with me" stays empty');
if(auth[1].pubkey !== ME) fail('signed as somebody other than the account');
ws.reply(['OK', auth[1].id, true, '']);
await sleep(20);
const reqs = ws.sent.filter(m => m[0] === 'REQ' && m[1] === id);
if(reqs.length !== 2) fail(`the recipient-bound REQ was not replayed exactly once (${reqs.length})`);
if(signed !== 1) fail(`one challenge caused ${signed} signatures`);

// One attempt per relay, as before: a second refusal ends the subscription instead of re-signing.
ws.reply(['CLOSED', id, 'auth-required: still no']);
await sleep(20);
if(signed !== 1) fail('a repeated refusal signed again');
if(ended !== 1) fail('a repeated refusal left the subscription waiting for EOSE for ever');

// Addressed to SOMEBODY ELSE: this account can never satisfy it, so it must not prompt a signer.
let otherEnded = 0;
const other = R.subscribe([{ kinds: [30078], '#p': [OTHER] }],
                          { onEvent(){}, onEose(){ otherEnded++; }, live: true });
ws.reply(['CLOSED', other, 'auth-required: NIP-78 reads require AUTH as the author or the recipient']);
await sleep(20);
if(signed !== 1) fail('a filter addressed to another account prompted the signer');
if(otherEnded !== 1) fail('an impossible subscription never completed');

console.log('ok');
process.exit(0);
