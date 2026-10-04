/* Encrypted Concord attachments reach a Blossom server that stores any bytes -- never NIP-96.
 *
 * Runs the SHIPPED static/js/client/upload.js factory under node with a fake network. Reported: the
 * Lounge's owner, who this node does not store files for, failed three times to post a photo in an
 * encrypted room -- the upload fell back to nostr.build (NIP-96), which refuses opaque bytes. Vector
 * (Armada's core) uses an ordered Blossom list for encrypted attachments; so must we.
 */
import vm from 'node:vm';
import fs from 'node:fs';
import { webcrypto } from 'node:crypto';

const src = fs.readFileSync(new URL('../../static/js/client/upload.js', import.meta.url), 'utf8');
const failures = [];
const check = (ok, what) => { if(!ok) failures.push(what); };

const BUILTIN = 'https://poster.place/blossom', NB = 'https://nostr.build';

function world({ allowed, custom = null, customProto = 'blossom', list = [], refuse = [] } = {}){
  const w = { puts: [], nip96: 0, accessChecks: 0 };
  const settings = { blossomEnabled: !!custom, mediaServer: custom || '', mediaProto: customProto };
  const state = { _blossomOK: null, ME: { pubkey: 'ab'.repeat(32) }, CFG: { blossom_enabled: true }, _uploadBatchAuth: null };
  const ctx = {
    console, URL, Blob, File, FormData, TextEncoder, Uint8Array, ArrayBuffer, Math, Date, JSON, Promise, Object, Array, String, Number, Error, Set, Map, setTimeout, clearTimeout,
    crypto: webcrypto, btoa: s => Buffer.from(s, 'binary').toString('base64'), encodeURIComponent,
    ClientSettings: { get: (k, d) => (k in settings ? settings[k] : d), set: () => {} },
    Store: { query: f => (f.kinds && f.kinds[0] === 10063 && list.length) ? [{ kind: 10063, tags: list.map(u => ['server', u]) }] : [] },
    fetch: async (url, init = {}) => {
      if(String(url).startsWith(NB)){ w.nip96++; return { ok: false, status: 415, headers: { get: () => 'unsupported media type' }, text: async () => 'unsupported media type', json: async () => ({}) }; }
      if(init.method === 'PUT'){
        const base = String(url).replace(/\/upload$/, '');
        w.puts.push({ base, headers: Object.assign({}, init.headers) });
        if(refuse.includes(base)) return { ok: false, status: 403, headers: { get: k => k === 'x-reason' ? 'refused here' : null }, text: async () => 'refused here' };
        return { ok: true, status: 200, headers: { get: () => null }, json: async () => ({ url: base + '/deadbeef' }) };
      }
      return { ok: false, status: 404, headers: { get: () => null }, text: async () => '', json: async () => ({}) };
    },
  };
  ctx.window = ctx; ctx.document = { createElement: () => ({}) };
  vm.createContext(ctx);
  vm.runInContext(src, ctx, { filename: 'upload.js' });
  const noop = () => {};
  const dep = new Proxy({
    state,
    _MEDIA_META: new Map(),
    _blossomBuiltin: () => ({ url: BUILTIN, proto: 'blossom' }),
    uploadTarget: () => {
      if(settings.blossomEnabled) return { url: settings.mediaServer, proto: settings.mediaProto };
      if(state._blossomOK === false) return { url: NB, proto: 'nip96' };
      return { url: BUILTIN, proto: 'blossom' };
    },
    checkBlossomAccess: async () => { w.accessChecks++; state._blossomOK = allowed; },
    sign: async (kind, content, tags) => ({ kind, content, tags, pubkey: state.ME.pubkey, id: 'x', sig: 'y' }),
    FilesIdx: { folders: () => [], addFolder: noop, setFile: noop },
  }, { get: (t, k) => (k in t ? t[k] : noop) });
  w.api = ctx.PCUploadFactory(dep);
  return w;
}

const sealed = () => new Blob([new Uint8Array([7, 1, 200, 3, 99, 0, 255, 42])], { type: 'application/octet-stream' });

// 1. Not a member here: the ciphertext goes to Vector's first default, never to nostr.build.
{
  const w = world({ allowed: false });
  let url = '';
  try{ url = await w.api.uploadBlob(sealed(), { keep: true, ciphertext: true }); }catch(e){ failures.push('a non-member could not attach: ' + e.message); }
  check(w.nip96 === 0, 'ciphertext was sent to nostr.build (NIP-96)');
  check(w.puts.length === 1 && w.puts[0].base === 'https://blossom.ditto.pub', 'not uploaded to the first default: ' + JSON.stringify(w.puts.map(p => p.base)));
  check(/^https:\/\/blossom\.ditto\.pub\//.test(url), 'wrong URL returned: ' + url);
  // 5. No custom header to somebody else's server (a CORS preflight would refuse the upload).
  const h = (w.puts[0] || {}).headers || {};
  check(!('X-Keep' in h) && !('X-No-Mirror' in h) && !('X-Filename' in h), 'custom headers sent to a third-party server: ' + Object.keys(h));
}
// 2. A refusing server fails over to the next.
{
  const w = world({ allowed: false, refuse: ['https://blossom.ditto.pub'] });
  let url = '';
  try{ url = await w.api.uploadBlob(sealed(), { keep: true, ciphertext: true }); }catch(e){ failures.push('failover threw: ' + e.message); }
  check(JSON.stringify(w.puts.map(p => p.base)) === JSON.stringify(['https://blossom.ditto.pub', 'https://blossom.primal.net']), 'no failover: ' + JSON.stringify(w.puts.map(p => p.base)));
  check(/primal/.test(url), 'failover URL wrong: ' + url);
}
// 3. Their own BUD-03 list comes before the defaults.
{
  const w = world({ allowed: false, list: ['https://my.blossom.example/'] });
  try{ await w.api.uploadBlob(sealed(), { ciphertext: true }); }catch(e){ failures.push('own list threw: ' + e.message); }
  check(w.puts[0] && w.puts[0].base === 'https://my.blossom.example', 'their kind-10063 server was not tried first: ' + JSON.stringify(w.puts.map(p => p.base)));
}
// 4. A member uploads to this node, as before -- with its keep header.
{
  const w = world({ allowed: true });
  let url = '';
  try{ url = await w.api.uploadBlob(sealed(), { keep: true, ciphertext: true }); }catch(e){ failures.push('member threw: ' + e.message); }
  check(w.puts.length === 1 && w.puts[0].base === BUILTIN && w.puts[0].headers['X-Keep'] === '1', 'a member did not upload to this node: ' + JSON.stringify(w.puts));
  check(url.startsWith(BUILTIN), 'member URL wrong: ' + url);
}
// 6. Every server refuses: one error naming each, not a raw nostr.build message.
{
  const all = ['https://blossom.ditto.pub', 'https://blossom.primal.net', 'https://blossom.data.haus'];
  const w = world({ allowed: false, refuse: all });
  let msg = '';
  try{ await w.api.uploadBlob(sealed(), { ciphertext: true }); }catch(e){ msg = e.message; }
  check(/no media server would store it/.test(msg) && all.every(u => msg.includes(new URL(u).host)) && /refused here/.test(msg), 'unhelpful error: ' + msg);
  check(w.nip96 === 0, 'fell back to nostr.build after all refused');
}
// 7. An ordinary (not encrypted) upload by a non-member is unchanged: it still goes to nostr.build.
{
  const w = world({ allowed: false });
  let msg = '';
  try{ await w.api.uploadBlob(new Blob(['x'], { type: 'image/png' }), { noCompress: true }); }catch(e){ msg = e.message; }
  check(w.nip96 >= 1 && w.puts.length === 0, 'an ordinary upload changed route: nip96=' + w.nip96 + ' puts=' + w.puts.length);
}

// 8. Josephus, measured: his synced kind-10096 names blossom.band as NIP-96 (nostr.build's media-only API),
//    and an admin has whitelisted him here -- this node is used, not the NIP-96 host.
{
  const w = world({ allowed: true, custom: 'https://blossom.band', customProto: 'nip96' });
  let url = '';
  try{ url = await w.api.uploadBlob(sealed(), { keep: true, ciphertext: true }); }catch(e){ failures.push('whitelisted with a NIP-96 server threw: ' + e.message); }
  check(w.nip96 === 0 && w.puts[0] && w.puts[0].base === BUILTIN, 'a whitelisted person with a NIP-96 server did not use this node: ' + JSON.stringify(w.puts.map(p => p.base)) + ' nip96=' + w.nip96);
  check(w.accessChecks === 1, 'the access check was skipped because they chose a server of their own');
}
// 9. Same NIP-96 choice, NOT allowed here, with his real kind-10063 list: his own Blossom servers first.
{
  const list = ['https://blossom.band', 'https://nostr.media', 'https://nostr.download', 'https://blossom.ditto.pub'];
  const w = world({ allowed: false, custom: 'https://blossom.band', customProto: 'nip96', list, refuse: ['https://blossom.band'] });
  let url = '';
  try{ url = await w.api.uploadBlob(sealed(), { ciphertext: true }); }catch(e){ failures.push('NIP-96 choice + list threw: ' + e.message); }
  check(w.nip96 === 0, 'ciphertext went to a NIP-96 host');
  check(JSON.stringify(w.puts.map(p => p.base)) === JSON.stringify(['https://blossom.band', 'https://nostr.media']), 'his own list was not walked in order: ' + JSON.stringify(w.puts.map(p => p.base)));
}

if(failures.length){ console.log(JSON.stringify(failures, null, 1)); process.exit(1); }
console.log('ok');
