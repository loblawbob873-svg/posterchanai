// A GIF COMMUNITY BANNER STAYS ANIMATED: the SHIPPED bannerImageBytes (sliced out of concord.js) returns a GIF's own
// bytes and never flattens it through the canvas -- which re-encoded EVERY banner as one JPEG frame. A PNG/JPEG still
// goes through the canvas (that strips a phone photo's EXIF GPS and bounds the width). Vyram, git issue "Community
// Banners Do Not Animate", 2026-10-26: "Set a gif as the community banner & it won't animate."
import fs from 'node:fs';import assert from 'node:assert/strict';
const src=fs.readFileSync('static/js/client/concord.js','utf8');
const i=src.indexOf('  async function bannerImageBytes(');assert.ok(i>=0,'bannerImageBytes moved');
const body=src.slice(i,src.indexOf('\n  }\n',i)+4);
let canvasUsed=0;
const jpeg=new Uint8Array([0xff,0xd8,0xff,0xe0,0,0x10]);
const make=new Function('createImageBitmap','OffscreenCanvas',body+'return bannerImageBytes;');
const fn=make(async()=>{canvasUsed++;return {width:3000,height:600,close(){}};},
  class{constructor(){}getContext(){return {drawImage(){}};}async convertToBlob(){return new Blob([jpeg]);}});
const gif=new Uint8Array([0x47,0x49,0x46,0x38,0x39,0x61,1,0,1,0,0,0,0,0x2c,0,0,0,0,1,0,1,0,0,2,2,0x44,1,0,0x3b]);
const png=new Uint8Array([0x89,0x50,0x4e,0x47,13,10,26,10,0,0,0,0]);

let out=await fn(new Blob([gif],{type:'image/gif'}));
assert.equal(canvasUsed,0,'a GIF banner was flattened through the canvas (its animation is lost)');
assert.deepEqual([...out],[...gif],'the banner bytes are not the GIF that was picked');

out=await fn(new Blob([png],{type:'image/png'}));
assert.equal(canvasUsed,1,'a PNG/JPEG banner must still be redrawn -- that strips EXIF GPS and bounds the width');
assert.deepEqual([...out],[...jpeg]);
console.log('OK');
