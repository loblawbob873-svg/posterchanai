// Prove PCSha256 (incremental) equals crypto.subtle byte-for-byte across sizes and chunk boundaries.
import { webcrypto } from 'node:crypto';
import { createRequire } from 'node:module';
const require = createRequire(import.meta.url);
const PCSha256 = require('../../static/js/client/sha256.js');

const subtle = webcrypto.subtle;
async function subtleHex(bytes){
  const d = await subtle.digest('SHA-256', bytes);
  return [...new Uint8Array(d)].map(b=>b.toString(16).padStart(2,'0')).join('');
}
function rnd(n){ const a=new Uint8Array(n); for(let i=0;i<n;i++) a[i]=(i*2654435761 ^ (i>>3)*40503 ^ n) & 255; return a; }

let fails = 0, checks = 0;
const sizes = [0,1,55,56,57,63,64,65,127,128,129,1000,1<<16, (1<<16)+123, 3*1024*1024+7];
for(const n of sizes){
  const data = rnd(n);
  const want = await subtleHex(data);
  // one-shot
  { const got = PCSha256.create().update(data).hex(); checks++; if(got!==want){ fails++; console.log('one-shot FAIL', n, got, want); } }
  // fed in odd-sized chunks that cross the 64-byte block boundary
  for(const ch of [1,7,31,32,33,64,65,100,4096]){
    const h = PCSha256.create();
    for(let o=0;o<n;o+=ch) h.update(data.subarray(o, Math.min(o+ch,n)));
    const got = h.hex();
    checks++; if(got!==want){ fails++; console.log('chunk FAIL', 'n='+n,'ch='+ch, got, want); }
  }
}
// digest() must be single-use
try { const h=PCSha256.create(); h.update(rnd(10)); h.hex(); h.hex(); fails++; console.log('FAIL: double digest allowed'); }
catch(_){ checks++; }

console.log(`sha256 stream: ${checks} checks, ${fails} failures`);
process.exit(fails?1:0);
