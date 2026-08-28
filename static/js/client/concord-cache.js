/* Durable CORD relay-envelope cache.
 *
 * Concord messages are encrypted CORD kind-1059 envelopes on the wire.  Cache those envelopes,
 * never the decrypted rumor, so an offline copy does not weaken the protocol.  The invite bundle
 * already held by Concord is required to turn these records back into room metadata/history.
 */
(function(root){
  'use strict';
  const DB='posterchan-concord-v1', STORE='envelopes', ICONS='icons', VERSION=2, MAX_PER_STREAM=5000;
  let dbPromise=null;
  function request(req){return new Promise((resolve,reject)=>{req.onsuccess=()=>resolve(req.result);req.onerror=()=>reject(req.error||new Error('IndexedDB request failed'));});}
  function done(tx){return new Promise((resolve,reject)=>{tx.oncomplete=()=>resolve();tx.onabort=tx.onerror=()=>reject(tx.error||new Error('IndexedDB transaction failed'));});}
  function open(){
    if(dbPromise)return dbPromise;
    dbPromise=new Promise((resolve,reject)=>{const q=indexedDB.open(DB,VERSION);q.onupgradeneeded=()=>{const db=q.result;if(!db.objectStoreNames.contains(STORE)){const s=db.createObjectStore(STORE,{keyPath:'key'});s.createIndex('stream','stream',{unique:false});}if(!db.objectStoreNames.contains(ICONS))db.createObjectStore(ICONS,{keyPath:'key'});};q.onsuccess=()=>resolve(q.result);q.onerror=()=>reject(q.error||new Error('Concord cache unavailable'));});
    return dbPromise;
  }
  function eventId(ev){return String(ev&&ev.id||'');}
  function eventTime(ev){return Number(ev&&ev.created_at)||0;}
  async function all(stream){
    const db=await open(),tx=db.transaction(STORE,'readonly'),completion=done(tx),rows=await request(tx.objectStore(STORE).index('stream').getAll(String(stream)));
    await completion;
    return rows.sort((a,b)=>a.created-b.created||a.id.localeCompare(b.id));
  }
  async function put(stream,events,{limit=MAX_PER_STREAM}={}){
    stream=String(stream||''); if(!stream)return 0;
    const clean=[],seen=new Set();
    for(const ev of events||[]){const id=eventId(ev);if(!id||seen.has(id))continue;seen.add(id);clean.push({key:stream+'\u0000'+id,stream,id,created:eventTime(ev),event:ev});}
    if(clean.length){const db=await open(),tx=db.transaction(STORE,'readwrite'),completion=done(tx),s=tx.objectStore(STORE);for(const row of clean)s.put(row);await completion;}
    const rows=await all(stream),extra=Math.max(0,rows.length-Math.max(1,Number(limit)||MAX_PER_STREAM));
    if(extra){const db=await open(),tx=db.transaction(STORE,'readwrite'),completion=done(tx),s=tx.objectStore(STORE);for(const row of rows.slice(0,extra))s.delete(row.key);await completion;}
    return clean.length;
  }
  async function get(stream){return (await all(stream)).map(row=>row.event);}
  async function page(stream,{before='',limit=200}={}){
    const rows=await all(stream),end=before?rows.findIndex(row=>row.key===before):rows.length,
      stop=end<0?rows.length:end,start=Math.max(0,stop-Math.max(1,Number(limit)||200)),slice=rows.slice(start,stop);
    return {events:slice.map(row=>row.event),before:start?slice[0].key:'',done:start===0};
  }
  async function drop(stream){const rows=await all(stream);if(!rows.length)return 0;const db=await open(),tx=db.transaction(STORE,'readwrite'),completion=done(tx),s=tx.objectStore(STORE);for(const row of rows)s.delete(row.key);await completion;return rows.length;}
  async function putIcon(key,ref,bytes,mime){const db=await open(),tx=db.transaction(ICONS,'readwrite'),completion=done(tx);tx.objectStore(ICONS).put({key:String(key),ref:String(ref),bytes:new Uint8Array(bytes),mime:String(mime||'image/*'),at:Date.now()});await completion;return true;}
  async function getIcon(key,ref){const db=await open(),tx=db.transaction(ICONS,'readonly'),completion=done(tx),row=await request(tx.objectStore(ICONS).get(String(key)));await completion;return row&&row.ref===String(ref)?row:null;}
  async function dropIcon(key){const db=await open(),tx=db.transaction(ICONS,'readwrite'),completion=done(tx);tx.objectStore(ICONS).delete(String(key));await completion;}
  async function dropRoom(prefix){const db=await open(),tx=db.transaction([STORE,ICONS],'readwrite'),completion=done(tx),s=tx.objectStore(STORE),rows=await request(s.getAll());for(const row of rows)if(row.stream.startsWith(String(prefix)))s.delete(row.key);tx.objectStore(ICONS).delete(String(prefix));await completion;return true;}
  root.PCConcordCache={DB,STORE,ICONS,MAX_PER_STREAM,put,get,page,drop,putIcon,getIcon,dropIcon,dropRoom,_reset(){dbPromise=null;}};
})(typeof window==='undefined'?globalThis:window);
