// The shipped LiveKit worker encrypts/decrypts actual byte frames with independently derived keys.
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import {webcrypto,hkdfSync,createHash} from 'node:crypto';
const workerSource=fs.readFileSync(new URL('../../static/vendor/livekit/livekit-client.e2ee.worker.js',import.meta.url),'utf8');
const options={sharedKey:false,ratchetSalt:'LKFrameEncryptionKey',ratchetWindowSize:0,failureTolerance:-1,keyringSize:16,keySize:256};
const identity='independent-publisher',root=Buffer.alloc(32,7);
const material=id=>Buffer.from(hkdfSync('sha256',root,Buffer.alloc(0),Buffer.concat([Buffer.from('concord/voice-sender\0'),createHash('sha256').update(id).digest()]),32));
async function worker(id){
 const messages=[];
 const ctx={crypto:webcrypto,ReadableStream,WritableStream,TransformStream,Uint8Array,Uint16Array,Uint32Array,ArrayBuffer,DataView,TextEncoder,TextDecoder,Map,Set,Promise,Date,Math,Number,Object,JSON,Error,TypeError,console,setTimeout,clearTimeout,performance,onmessage:null,postMessage:m=>messages.push(m)};ctx.self=ctx;
 vm.createContext(ctx);vm.runInContext(workerSource,ctx);
 const send=(kind,data)=>ctx.onmessage({data:{kind,data}});
 send('init',{keyProviderOptions:options,loglevel:'silent'});
 const key=await webcrypto.subtle.importKey('raw',material(id),'HKDF',false,['deriveBits','deriveKey']);
 send('setKey',{participantIdentity:identity,isPublisher:true,key,keyIndex:0});send('enable',{participantIdentity:identity,enabled:true});
 return {send,messages};
}
async function transform(w,kind,frames){
 let finish;const done=new Promise(r=>finish=r),out=[];
 const readableStream=new ReadableStream({start(c){for(const f of frames)c.enqueue(f);c.close();}});
 const writableStream=new WritableStream({write(f){out.push(f);},close(){finish();},abort(){finish();}});
 w.send(kind,{participantIdentity:identity,readableStream,writableStream,trackId:kind,isReuse:false});
 await Promise.race([done,new Promise((_,reject)=>{const t=setTimeout(()=>reject(Error('worker transform stalled')),2000);t.unref();})]);return out;
}
const frame=(bytes,time)=>({data:Uint8Array.from(bytes).buffer,timestamp:time,getMetadata:()=>({synchronizationSource:42})});
const plain=[0xf8,1,2,3,4,5,6,7,8,9,10,11];
const encoder=await worker(identity),decoder=await worker(identity);
const encrypted=await transform(encoder,'encode',[frame(plain,1000),frame(plain,1001)]);
assert.equal(encrypted.length,2);
assert.notDeepEqual([...new Uint8Array(encrypted[0].data)],plain,'plaintext must not reach SFU');
assert.notDeepEqual([...new Uint8Array(encrypted[0].data)],[...new Uint8Array(encrypted[1].data)],'successive frames must not reuse nonce');
const ciphertext=encrypted.map(f=>frame(new Uint8Array(f.data),f.timestamp));
const decoded=await transform(decoder,'decode',encrypted);
assert.equal(decoded.length,2);assert.deepEqual([...new Uint8Array(decoded[0].data)],plain);
const wrong=await worker('different-publisher');
assert.equal((await transform(wrong,'decode',ciphertext)).length,0,'a different sender key must not decode frames');
console.log('shipped frame worker encryption, independent sender derivation and nonce separation passed');
