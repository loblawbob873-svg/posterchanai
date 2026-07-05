/* NIP-77 Negentropy (protocol v1) — INITIATOR / client side.
 *
 * Range-based set reconciliation with the relay: exchange a compact summary of which events each side
 * holds over a filter, so we fetch ONLY what we're missing instead of re-pulling the whole timeline.
 * The wire format matches app/services/nostr_relay/negentropy.py EXACTLY (fingerprint, big-endian
 * base-128 varints, delta-coded bounds, 16-bucket split). We implement the initiator: build the first
 * message, then process each relay reply, collecting `need` (ids the relay has that we don't) and
 * emitting the next message until fully resolved.
 *
 * Usage:  const ng = new Negentropy(items);        // items: [{ts, id(hex)}]
 *         let msg = await ng.initiate();            // hex string → NEG-OPEN
 *         // on each ["NEG-MSG", subid, serverHex]:
 *         const { nextMsg } = await ng.reconcile(serverHex);   // nextMsg hex or null (done)
 *         ng.need  // Set of hex ids to REQ from the relay
 */
(function(){
  const VERSION = 0x61, ID_SIZE = 32, FP_SIZE = 16, BUCKETS = 16;
  const MAX_U64 = (1n << 64n) - 1n, MASK256 = (1n << 256n) - 1n;

  // ---- byte / hex / bigint helpers ----
  function hexToBytes(h){ const n = h.length >> 1, u = new Uint8Array(n); for (let i = 0; i < n; i++) u[i] = parseInt(h.substr(i * 2, 2), 16); return u; }
  function bytesToHex(u){ let s = ''; for (let i = 0; i < u.length; i++) s += u[i].toString(16).padStart(2, '0'); return s; }
  function leToBig(u){ let n = 0n; for (let i = u.length - 1; i >= 0; i--) n = (n << 8n) | BigInt(u[i]); return n; }
  function bigToLe(n, len){ const u = new Uint8Array(len); for (let i = 0; i < len; i++){ u[i] = Number(n & 0xFFn); n >>= 8n; } return u; }
  // Compare two id byte arrays (a possibly a prefix) lexicographically: <0, 0, >0.
  function cmpBytes(a, b){ const n = Math.min(a.length, b.length); for (let i = 0; i < n; i++){ if (a[i] !== b[i]) return a[i] - b[i]; } return a.length - b.length; }

  // ---- varint (big-endian base-128; MSB=continuation on all but the last byte) ----
  function encodeVarint(n){ n = BigInt(n); if (n === 0n) return [0]; const o = []; while (n > 0n){ o.push(Number(n & 0x7Fn)); n >>= 7n; } o.reverse(); for (let i = 0; i < o.length - 1; i++) o[i] |= 0x80; return o; }

  class Reader{
    constructor(u){ this.u = u; this.p = 0; }
    remaining(){ return this.u.length - this.p; }
    read(n){ if (this.p + n > this.u.length) throw new Error('neg: read past end'); const s = this.u.subarray(this.p, this.p + n); this.p += n; return s; }
    readVarint(){ return Number(this.readVarintBig()); }   // small values (modes, counts, prefix lens)
    readVarintBig(){ let n = 0n; for (;;){ const b = this.read(1)[0]; n = (n << 7n) | BigInt(b & 0x7F); if (!(b & 0x80)) return n; } }
  }

  // ---- fingerprint: Σ(id bytes as LE-256) mod 2^256 → 32 LE bytes + varint(count) → SHA-256, first 16 ----
  async function fingerprint(idBytesList){
    let acc = 0n;
    for (const id of idBytesList) acc = (acc + leToBig(id)) & MASK256;
    const pre = bigToLe(acc, 32), cnt = encodeVarint(idBytesList.length);
    const buf = new Uint8Array(pre.length + cnt.length); buf.set(pre, 0); buf.set(cnt, pre.length);
    const dig = new Uint8Array(await crypto.subtle.digest('SHA-256', buf));
    return dig.subarray(0, FP_SIZE);
  }

  // ---- bounds: { ts:BigInt, id:Uint8Array(prefix) }; delta-coded timestamp (0 = infinity) ----
  function encodeBound(out, b, state){
    if (b.ts === MAX_U64){ pushBytes(out, encodeVarint(0)); state.last = MAX_U64; }
    else { const delta = b.ts - state.last; state.last = b.ts; pushBytes(out, encodeVarint(delta + 1n)); }
    pushBytes(out, encodeVarint(b.id.length)); for (const x of b.id) out.push(x);
  }
  function decodeBound(r, state){
    const enc = r.readVarintBig(); let ts;
    if (enc === 0n){ state.last = MAX_U64; ts = MAX_U64; } else { ts = state.last + (enc - 1n); state.last = ts; }
    const plen = r.readVarint(); if (plen > ID_SIZE) throw new Error('neg: bad prefix len');
    return { ts, id: r.read(plen).slice() };
  }
  function pushBytes(out, arr){ for (const x of arr) out.push(x); }
  const boundInfinity = () => ({ ts: MAX_U64, id: new Uint8Array(0) });

  // item = {ts:BigInt, id:Uint8Array}; items sorted by (ts, id). Is item strictly below bound b?
  function itemLtBound(it, b){ if (it.ts !== b.ts) return it.ts < b.ts; return cmpBytes(it.id.subarray(0, b.id.length), b.id) < 0; }
  function lowerIndex(items, b){ let lo = 0, hi = items.length; while (lo < hi){ const mid = (lo + hi) >> 1; if (itemLtBound(items[mid], b)) lo = mid + 1; else hi = mid; } return lo; }
  // Smallest bound separating prev < curr.
  function minimalBound(prev, curr){ if (prev.ts !== curr.ts) return { ts: curr.ts, id: new Uint8Array(0) }; let n = 0; while (n < ID_SIZE && n < prev.id.length && prev.id[n] === curr.id[n]) n++; return { ts: curr.ts, id: curr.id.slice(0, n + 1) }; }

  class Negentropy{
    constructor(items){
      // items: [{ts:Number, id:hex}] — sort by (ts, id) and hold as {ts:BigInt, id:Uint8Array}.
      this.items = (items || []).map(it => ({ ts: BigInt(it.ts), id: hexToBytes(it.id) }))
        .sort((a, b) => (a.ts < b.ts ? -1 : a.ts > b.ts ? 1 : cmpBytes(a.id, b.id)));
      this.need = new Set(); this.have = new Set();
    }
    async initiate(){
      const out = [VERSION]; const state = { last: 0n };
      await this._splitRange(out, state, 0, this.items.length, boundInfinity());
      return bytesToHex(new Uint8Array(out));
    }
    // Process a relay message; return { nextMsg: hex|null }. null → reconciliation complete.
    async reconcile(hex){
      const r = new Reader(hexToBytes(hex));
      if (r.read(1)[0] !== VERSION) throw new Error('neg: bad version');
      const out = [VERSION]; const oState = { last: 0n }, iState = { last: 0n };
      let lower = { ts: 0n, id: new Uint8Array(0) };
      let anyWork = false;
      while (r.remaining() > 0){
        const upper = decodeBound(r, iState);
        const mode = r.readVarint();
        const loIdx = lowerIndex(this.items, lower), hiIdx = lowerIndex(this.items, upper);
        if (mode === 0){ this._append(out, oState, upper, 0, []); }
        else if (mode === 1){
          const theirFp = r.read(FP_SIZE);
          const ourFp = await fingerprint(this._ids(loIdx, hiIdx));
          if (cmpBytes(ourFp, theirFp) === 0){ this._append(out, oState, upper, 0, []); }
          else { anyWork = true; await this._splitRange(out, oState, loIdx, hiIdx, upper); }
        } else if (mode === 2){
          const count = r.readVarint();
          const theirs = new Set();
          for (let i = 0; i < count; i++) theirs.add(bytesToHex(r.read(ID_SIZE)));
          const ours = new Set(this._ids(loIdx, hiIdx).map(bytesToHex));
          for (const id of theirs) if (!ours.has(id)) this.need.add(id);   // relay has, we don't → fetch
          for (const id of ours) if (!theirs.has(id)) this.have.add(id);   // we have, relay doesn't
          this._append(out, oState, upper, 0, []);                          // range resolved → skip
        } else throw new Error('neg: unknown mode ' + mode);
        lower = upper;
      }
      // Done when we emitted only skips (no fingerprint/idlist ranges asking for another round).
      return { nextMsg: anyWork ? bytesToHex(new Uint8Array(out)) : null };
    }
    _ids(lo, hi){ const a = []; for (let i = lo; i < hi; i++) a.push(this.items[i].id); return a; }
    _append(out, state, upper, mode, payload){ encodeBound(out, upper, state); pushBytes(out, encodeVarint(mode)); pushBytes(out, payload); }
    async _splitRange(out, state, loIdx, hiIdx, upper){
      const num = hiIdx - loIdx;
      if (num < BUCKETS * 2){
        const ids = this._ids(loIdx, hiIdx); const payload = encodeVarint(ids.length).slice();
        for (const id of ids) for (const x of id) payload.push(x);
        this._append(out, state, upper, 2, payload); return;
      }
      const per = Math.floor(num / BUCKETS), extra = num % BUCKETS; let curr = loIdx;
      for (let b = 0; b < BUCKETS; b++){
        const size = per + (b < extra ? 1 : 0);
        const bucket = this._ids(curr, curr + size); curr += size;
        const nextBound = (b === BUCKETS - 1) ? upper : minimalBound(this.items[curr - 1], this.items[curr]);
        this._append(out, state, nextBound, 1, await fingerprint(bucket));
      }
    }
  }

  window.Negentropy = Negentropy;
})();
