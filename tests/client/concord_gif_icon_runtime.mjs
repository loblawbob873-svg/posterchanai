// A GIF COMMUNITY ICON STAYS ANIMATED: the SHIPPED sealImageRef (sliced out of concord.js) seals a GIF's
// own bytes and never flattens it through the canvas; a PNG/JPEG still goes through the canvas (that is
// what strips a phone photo's EXIF GPS). Vyram, 2026-10-21: "Do gifs when used as a concord community
// avatar animate? If they are suppose to they aren't".
import fs from 'node:fs';import assert from 'node:assert/strict';import {webcrypto} from 'node:crypto';
const src=fs.readFileSync('static/js/client/concord.js','utf8');
const cut=name=>{const i=src.indexOf('  async function '+name+'(');assert.ok(i>=0,name);return src.slice(i,src.indexOf('\n  }\n',i)+4);};
const bytesHex=b=>Buffer.from(b).toString('hex');
const make=new Function('crypto','bytesHex','fetch','createImageBitmap','OffscreenCanvas',cut('sealImageRef')+cut('sealIconBytes')+'return {sealImageRef};');
const gif=new Uint8Array([0x47,0x49,0x46,0x38,0x39,0x61,1,0,1,0,0,0,0,0x2c,0,0,0,0,1,0,1,0,0,2,2,0x44,1,0,0x3b]);
const png=new Uint8Array([0x89,0x50,0x4e,0x47,13,10,26,10,0,0,0,0]);
let canvasUsed=0,uploaded=null;
const p={uploadBlob:async b=>{uploaded=new Uint8Array(await b.arrayBuffer());return 'https://blossom.example/x';}};
const env=bytes=>[webcrypto,bytesHex,async()=>({ok:true,blob:async()=>new Blob([bytes])}),
  async()=>{canvasUsed++;return {width:10,height:10,close(){}};},
  class{constructor(){}getContext(){return {drawImage(){}};}async convertToBlob(){return new Blob([png]);}}];
const decrypt=async(ref,cipher)=>{const key=await webcrypto.subtle.importKey('raw',Buffer.from(ref.key,'hex'),'AES-GCM',false,['decrypt']);
  return new Uint8Array(await webcrypto.subtle.decrypt({name:'AES-GCM',iv:Buffer.from(ref.nonce,'hex')},key,cipher));};

let ref=await make(...env(gif)).sealImageRef(p,'blob:x');
assert.equal(canvasUsed,0,'a GIF was flattened through the canvas (its animation is lost)');
assert.equal(ref.ext,'gif');
assert.deepEqual([...await decrypt(ref,uploaded)],[...gif],'the sealed bytes are not the GIF that was picked');
assert.equal(ref.hash,bytesHex(await webcrypto.subtle.digest('SHA-256',gif)),'hash must be of the plaintext (CORD-02)');

ref=await make(...env(png)).sealImageRef(p,'blob:y');
assert.equal(canvasUsed,1,'a PNG/JPEG must still be redrawn — that strips EXIF GPS');
assert.equal(ref.ext,'png');

const big=new Uint8Array(3*1024*1024);big.set(gif.slice(0,6));canvasUsed=0;
ref=await make(...env(big)).sealImageRef(p,'blob:z');
assert.equal(canvasUsed,1,'an oversized GIF is redrawn as a still, not shipped to every member');
console.log('OK');
