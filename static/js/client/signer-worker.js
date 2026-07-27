/* Crypto Web Worker — keeps secp256k1 work (verify/sign/NIP-04/keygen) OFF the main thread
 * so busy timelines never jank the UI or peg the page's CPU core. Uses the vendored
 * nostr-tools bundle. When the user logs in with a local nsec, the secret key lives HERE
 * (not on the main thread); NIP-07 users sign in their extension and never hit this worker
 * for signing — only verification. */
importScripts('/static/vendor/nostr/nostr.bundle.js');
const NT = self.NostrTools;

let SK = null;            // Uint8Array secret key (local-key mode only)
let PK = null;            // hex pubkey

function reply(id, ok, data, error){ self.postMessage({ id, ok, data, error: error || null }); }

self.onmessage = async (e) => {
  const { id, op, args } = e.data || {};
  try {
    switch (op) {
      case 'setKey': {                       // args.sk = hex secret
        SK = hexToBytes(args.sk);
        PK = NT.getPublicKey(SK);
        return reply(id, true, { pubkey: PK });
      }
      case 'clearKey': { SK = null; PK = null; return reply(id, true, {}); }
      case 'exportNsec': {                     // reveal the local secret key (local-login only)
        if (!SK) return reply(id, false, null, 'no local key');
        return reply(id, true, { nsec: NT.nip19.nsecEncode(SK) });
      }
      case 'genKey': {
        const sk = NT.generateSecretKey();
        const pk = NT.getPublicKey(sk);
        return reply(id, true, { nsec: NT.nip19.nsecEncode(sk), npub: NT.nip19.npubEncode(pk),
                                 sk: bytesToHex(sk), pubkey: pk });
      }
      case 'decodeNsec': {                    // validate + derive pubkey from an nsec/hex
        const sk = decodeSk(args.nsec);
        const pk = NT.getPublicKey(sk);
        return reply(id, true, { sk: bytesToHex(sk), pubkey: pk, npub: NT.nip19.npubEncode(pk) });
      }
      case 'sign': {                          // args.event = unsigned template
        if (!SK) return reply(id, false, null, 'no local key');
        const ev = NT.finalizeEvent(args.event, SK);
        return reply(id, true, ev);
      }
      case 'verify':                          // args.event
        return reply(id, true, { valid: safeVerify(args.event) });
      case 'verifyBatch': {                   // args.events -> [{id,valid}]
        const out = args.events.map(ev => ({ id: ev.id, valid: safeVerify(ev) }));
        return reply(id, true, out);
      }
      case 'nip04enc': {                       // args.peer (hex), args.text
        if (!SK) return reply(id, false, null, 'no local key');
        return reply(id, true, { ct: await NT.nip04.encrypt(SK, args.peer, args.text) });
      }
      case 'nip04dec': {                       // args.peer, args.ct
        if (!SK) return reply(id, false, null, 'no local key');
        return reply(id, true, { pt: await NT.nip04.decrypt(SK, args.peer, args.ct) });
      }
      // ---- NIP-44 (used by NIP-46 remote-signer transport; some signers reply NIP-44 not NIP-04) ----
      case 'nip44enc': {                       // args.peer (hex), args.text
        if (!SK) return reply(id, false, null, 'no local key');
        const ck = NT.nip44.getConversationKey(SK, args.peer);
        return reply(id, true, { ct: NT.nip44.encrypt(args.text, ck) });
      }
      case 'nip44dec': {                       // args.peer, args.ct
        if (!SK) return reply(id, false, null, 'no local key');
        const ck = NT.nip44.getConversationKey(SK, args.peer);
        return reply(id, true, { pt: NT.nip44.decrypt(args.ct, ck) });
      }
      // ---- NIP-17 private DMs (gift-wrapped via NIP-59 seal + NIP-44 encryption), local key only ----
      case 'nip17wrap': {                      // args.peer (hex), args.text -> two kind-1059 wraps
        if (!SK) return reply(id, false, null, 'no local key');
        // args.tags (when given) already includes the p-tag plus any NIP-30 custom-emoji tags the
        // page built for this message — the worker has no access to the emoji map.
        const rumor = { kind: 14, created_at: Math.floor(Date.now()/1000),
                        tags: args.tags || [['p', args.peer]], content: args.text };
        // a gift wrap for the recipient AND one to ourselves so we keep a copy of what we sent
        const toPeer = NT.nip59.wrapEvent(rumor, SK, args.peer);
        const toSelf = NT.nip59.wrapEvent(rumor, SK, PK);
        return reply(id, true, { toPeer, toSelf });
      }
      case 'nip17unwrap': {                     // args.wrap (kind 1059) -> inner kind-14 rumor
        if (!SK) return reply(id, false, null, 'no local key');
        return reply(id, true, { rumor: NT.nip59.unwrapEvent(args.wrap, SK) });
      }
      // ---- gift-wrap an ALREADY-BUILT seal with a fresh EPHEMERAL key (for nip07/nip46 signers,
      // whose secret key never reaches us: they build+sign the kind-13 seal themselves, we only do
      // the throwaway outer 1059 layer). args.seal (signed kind-13), args.recipient (hex). ----
      case 'giftwrapSeal': {
        const ephSk = NT.generateSecretKey();
        const ck = NT.nip44.getConversationKey(ephSk, args.recipient);
        const content = NT.nip44.encrypt(JSON.stringify(args.seal), ck);
        const created = Math.floor(Date.now()/1000) - Math.floor(Math.random() * 2 * 86400);
        const wrap = NT.finalizeEvent({ kind: 1059, created_at: created,
                                        tags: [['p', args.recipient]], content }, ephSk);
        return reply(id, true, { wrap });
      }
      default: return reply(id, false, null, 'unknown op ' + op);
    }
  } catch (err) { reply(id, false, null, String(err && err.message || err)); }
};

function safeVerify(ev){ try { return NT.verifyEvent(ev); } catch(_) { return false; } }
function decodeSk(s){
  s = (s || '').trim();
  if (s.startsWith('nsec')) { const d = NT.nip19.decode(s); return d.data; }
  if (/^[0-9a-f]{64}$/i.test(s)) return hexToBytes(s);
  throw new Error('invalid secret key');
}
function hexToBytes(h){ const a = new Uint8Array(h.length/2); for(let i=0;i<a.length;i++) a[i]=parseInt(h.substr(i*2,2),16); return a; }
function bytesToHex(b){ return Array.from(b).map(x=>x.toString(16).padStart(2,'0')).join(''); }
