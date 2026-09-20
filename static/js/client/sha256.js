/* Incremental SHA-256 for hashing files WITHOUT loading them whole into memory.
 *
 * WHY THIS EXISTS: crypto.subtle.digest() takes one buffer, so hashing a file means
 * file.arrayBuffer() — the entire file resident at once. That caps uploads at whatever a browser tab
 * can allocate (~2 GB in practice), which is smaller than a PosterChanOS ISO. This lets a caller feed
 * the file in slices (file.slice(a,b) → arrayBuffer per chunk → update()), so only one chunk is in
 * memory at a time. Verified byte-for-byte against crypto.subtle in tests/client/test_sha256_stream.mjs.
 *
 * window.PCSha256.create() -> { update(Uint8Array|ArrayBuffer), hex(), digest() }  (single-use)
 * window.PCSha256.hexOfFile(file, chunkBytes?) -> Promise<hex>                      (convenience)
 */
(function(){
  'use strict';
  const K = new Uint32Array([
    0x428a2f98,0x71374491,0xb5c0fbcf,0xe9b5dba5,0x3956c25b,0x59f111f1,0x923f82a4,0xab1c5ed5,
    0xd807aa98,0x12835b01,0x243185be,0x550c7dc3,0x72be5d74,0x80deb1fe,0x9bdc06a7,0xc19bf174,
    0xe49b69c1,0xefbe4786,0x0fc19dc6,0x240ca1cc,0x2de92c6f,0x4a7484aa,0x5cb0a9dc,0x76f988da,
    0x983e5152,0xa831c66d,0xb00327c8,0xbf597fc7,0xc6e00bf3,0xd5a79147,0x06ca6351,0x14292967,
    0x27b70a85,0x2e1b2138,0x4d2c6dfc,0x53380d13,0x650a7354,0x766a0abb,0x81c2c92e,0x92722c85,
    0xa2bfe8a1,0xa81a664b,0xc24b8b70,0xc76c51a3,0xd192e819,0xd6990624,0xf40e3585,0x106aa070,
    0x19a4c116,0x1e376c08,0x2748774c,0x34b0bcb5,0x391c0cb3,0x4ed8aa4a,0x5b9cca4f,0x682e6ff3,
    0x748f82ee,0x78a5636f,0x84c87814,0x8cc70208,0x90befffa,0xa4506ceb,0xbef9a3f7,0xc67178f2]);

  function Hasher(){
    this.h = new Uint32Array([0x6a09e667,0xbb67ae85,0x3c6ef372,0xa54ff53a,0x510e527f,0x9b05688c,0x1f83d9ab,0x5be0cd19]);
    this.block = new Uint8Array(64);   // holds the partial (<64B) tail between updates
    this.used = 0;                     // bytes currently in `block`
    this.len = 0;                      // total bytes seen (for the length padding)
    this.w = new Uint32Array(64);
    this.done = false;
  }
  Hasher.prototype._compress = function(p, off){
    const w = this.w, h = this.h;
    for(let i=0;i<16;i++) w[i] = (p[off+i*4]<<24)|(p[off+i*4+1]<<16)|(p[off+i*4+2]<<8)|(p[off+i*4+3]);
    for(let i=16;i<64;i++){
      const a = w[i-15], b = w[i-2];
      const s0 = ((a>>>7)|(a<<25)) ^ ((a>>>18)|(a<<14)) ^ (a>>>3);
      const s1 = ((b>>>17)|(b<<15)) ^ ((b>>>19)|(b<<13)) ^ (b>>>10);
      w[i] = (w[i-16] + s0 + w[i-7] + s1) | 0;
    }
    let a=h[0],b=h[1],c=h[2],d=h[3],e=h[4],f=h[5],g=h[6],hh=h[7];
    for(let i=0;i<64;i++){
      const S1 = ((e>>>6)|(e<<26)) ^ ((e>>>11)|(e<<21)) ^ ((e>>>25)|(e<<7));
      const ch = (e & f) ^ (~e & g);
      const t1 = (hh + S1 + ch + K[i] + w[i]) | 0;
      const S0 = ((a>>>2)|(a<<30)) ^ ((a>>>13)|(a<<19)) ^ ((a>>>22)|(a<<10));
      const maj = (a & b) ^ (a & c) ^ (b & c);
      const t2 = (S0 + maj) | 0;
      hh=g; g=f; f=e; e=(d+t1)|0; d=c; c=b; b=a; a=(t1+t2)|0;
    }
    h[0]=(h[0]+a)|0; h[1]=(h[1]+b)|0; h[2]=(h[2]+c)|0; h[3]=(h[3]+d)|0;
    h[4]=(h[4]+e)|0; h[5]=(h[5]+f)|0; h[6]=(h[6]+g)|0; h[7]=(h[7]+hh)|0;
  };
  Hasher.prototype.update = function(data){
    if(this.done) throw new Error('sha256: update after digest');
    let p = data instanceof Uint8Array ? data : new Uint8Array(data.buffer||data);
    this.len += p.length;
    let i = 0;
    if(this.used){                                  // top up a partial block first
      while(i < p.length && this.used < 64){ this.block[this.used++] = p[i++]; }
      if(this.used === 64){ this._compress(this.block, 0); this.used = 0; }
    }
    for(; i + 64 <= p.length; i += 64) this._compress(p, i);   // full blocks straight from the input
    while(i < p.length){ this.block[this.used++] = p[i++]; }   // stash the tail
    return this;
  };
  Hasher.prototype.digest = function(){
    if(this.done) throw new Error('sha256: digest called twice');
    this.done = true;
    const bitLenHi = Math.floor(this.len / 0x20000000);        // len*8 high 32 bits
    const bitLenLo = (this.len << 3) >>> 0;
    this.block[this.used++] = 0x80;
    if(this.used > 56){ while(this.used < 64) this.block[this.used++] = 0; this._compress(this.block,0); this.used = 0; }
    while(this.used < 56) this.block[this.used++] = 0;
    const dv = new DataView(this.block.buffer);
    dv.setUint32(56, bitLenHi); dv.setUint32(60, bitLenLo);
    this._compress(this.block, 0);
    const out = new Uint8Array(32), h = this.h;
    for(let i=0;i<8;i++){ out[i*4]=(h[i]>>>24)&255; out[i*4+1]=(h[i]>>>16)&255; out[i*4+2]=(h[i]>>>8)&255; out[i*4+3]=h[i]&255; }
    return out;
  };
  Hasher.prototype.hex = function(){
    return Array.from(this.digest(), b => b.toString(16).padStart(2,'0')).join('');
  };

  async function hexOfFile(file, chunkBytes){
    const CH = chunkBytes || (8*1024*1024);
    const h = new Hasher();
    for(let off=0; off<file.size; off += CH){
      const slice = file.slice(off, Math.min(off+CH, file.size));
      h.update(new Uint8Array(await slice.arrayBuffer()));
    }
    return h.hex();
  }

  const api = { create: () => new Hasher(), hexOfFile };
  if(typeof window !== 'undefined') window.PCSha256 = api;
  if(typeof module !== 'undefined' && module.exports) module.exports = api;
})();
