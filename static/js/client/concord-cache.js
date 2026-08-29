/* Durable CORD relay-envelope cache.
 *
 * Concord messages are encrypted CORD kind-1059 envelopes on the wire.  Cache those envelopes,
 * never the decrypted rumor, so an offline copy does not weaken the protocol.  The invite bundle
 * already held by Concord is required to turn these records back into room metadata/history.
 */
(function(root){
  'use strict';
  const DB='posterchan-concord-v1', STORE='envelopes', ICONS='icons', VERSION=3,
    MAX_PER_STREAM=5000, MAX_EVENT_BYTES=65536, MAX_TOTAL_BYTES=32*1024*1024,
    MAX_ICON_BYTES=5*1024*1024, MAX_ICON_TOTAL_BYTES=20*1024*1024, MAX_ICONS=64;
  let dbPromise=null;
  function request(req){return new Promise((resolve,reject)=>{req.onsuccess=()=>resolve(req.result);req.onerror=()=>reject(req.error||new Error('IndexedDB request failed'));});}
  function done(tx){return new Promise((resolve,reject)=>{tx.oncomplete=()=>resolve();tx.onabort=tx.onerror=()=>reject(tx.error||new Error('IndexedDB transaction failed'));});}
  function open(){
    if(dbPromise)return dbPromise;
    dbPromise=new Promise((resolve,reject)=>{const q=indexedDB.open(DB,VERSION);q.onupgradeneeded=()=>{const db=q.result,s=db.objectStoreNames.contains(STORE)?q.transaction.objectStore(STORE):db.createObjectStore(STORE,{keyPath:'key'});if(!s.indexNames.contains('stream'))s.createIndex('stream','stream',{unique:false});if(!s.indexNames.contains('streamCreated'))s.createIndex('streamCreated',['stream','created'],{unique:false});if(!db.objectStoreNames.contains(ICONS))db.createObjectStore(ICONS,{keyPath:'key'});};q.onsuccess=()=>resolve(q.result);q.onerror=()=>reject(q.error||new Error('Concord cache unavailable'));/* A VERSIONED DATABASE THAT ANOTHER TAB HOLDS OPEN AT AN OLDER VERSION BLOCKS FOR EVER, and
   with no handler this promise simply never settles: every icon read and every envelope read
   awaits it until the page is closed, with nothing thrown and nothing logged. A second window
   — or the desktop shell and a browser tab on the same profile — is enough. Rejecting turns
   a permanent hang into one slow load: the callers already treat a cache failure as a miss
   and go to the network. */q.onblocked=()=>reject(new Error('Concord cache is held open by another tab'));}).catch(e=>{dbPromise=null;throw e;});
    return dbPromise;
  }
  function eventId(ev){return String(ev&&ev.id||'');}
  function eventTime(ev){return Number(ev&&ev.created_at)||0;}
  /* Persist only the signed Nostr envelope. Relay objects sometimes grow renderer-only fields
   * (`plaintext`, decoded rumor, profile, errors); copying the whole untrusted object would turn
   * those into durable plaintext. Size and shape limits also keep one hostile relay response from
   * exhausting IndexedDB quota before the per-stream row limit gets a chance to prune it. */
  function envelope(ev){
    if(!ev||typeof ev!=='object'||Number(ev.kind)!==1059)return null;
    const out={id:String(ev.id||'').slice(0,128),pubkey:String(ev.pubkey||'').slice(0,128),
      created_at:eventTime(ev),kind:1059,
      tags:Array.isArray(ev.tags)?ev.tags.slice(0,256).map(t=>Array.isArray(t)?t.slice(0,16).map(v=>String(v).slice(0,2048)):[]):[],
      content:String(ev.content||''),sig:String(ev.sig||'').slice(0,256)};
    if(!out.id||out.content.length>MAX_EVENT_BYTES)return null;
    const size=new TextEncoder().encode(JSON.stringify(out)).byteLength;
    return size<=MAX_EVENT_BYTES?{event:out,size}:null;
  }
  async function all(stream){
    const db=await open(),tx=db.transaction(STORE,'readonly'),completion=done(tx),[rows]=await Promise.all([request(tx.objectStore(STORE).index('stream').getAll(String(stream))),completion]);
    return rows.sort((a,b)=>a.created-b.created||a.id.localeCompare(b.id));
  }
  async function put(stream,events,{limit=MAX_PER_STREAM}={}){
    stream=String(stream||''); if(!stream||stream.length>2048)return 0;
    const clean=[],seen=new Set();
    const incoming=Array.isArray(events)?events.slice(-MAX_PER_STREAM):[];
    for(const ev of incoming){const safe=envelope(ev),id=safe&&eventId(safe.event);if(!id||seen.has(id))continue;seen.add(id);clean.push({key:stream+'\u0000'+id,stream,id,created:eventTime(safe.event),event:safe.event,size:safe.size});}
    if(clean.length){const db=await open(),tx=db.transaction(STORE,'readwrite'),completion=done(tx),s=tx.objectStore(STORE);for(const row of clean)s.put(row);await completion;}
    const rows=await all(stream),extra=Math.max(0,rows.length-Math.max(1,Number(limit)||MAX_PER_STREAM));
    if(extra){const db=await open(),tx=db.transaction(STORE,'readwrite'),completion=done(tx),s=tx.objectStore(STORE);for(const row of rows.slice(0,extra))s.delete(row.key);await completion;}
    /* A malicious room can advertise many stream ids, so a per-stream limit is not a quota limit.
     * Bound the database as a whole by serialized envelope bytes, evicting oldest first. */
    {const db=await open(),read=db.transaction(STORE,'readonly'),readDone=done(read),[allRows]=await Promise.all([request(read.objectStore(STORE).getAll()),readDone]);let bytes=allRows.reduce((n,r)=>n+(Number(r.size)||new TextEncoder().encode(JSON.stringify(r.event||{})).byteLength),0);allRows.sort((a,b)=>a.created-b.created||a.key.localeCompare(b.key));const victims=[];for(const row of allRows){if(bytes<=MAX_TOTAL_BYTES)break;victims.push(row.key);bytes-=Number(row.size)||new TextEncoder().encode(JSON.stringify(row.event||{})).byteLength;}if(victims.length){const tx=db.transaction(STORE,'readwrite'),completion=done(tx),s=tx.objectStore(STORE);for(const key of victims)s.delete(key);await completion;}}
    return clean.length;
  }
  async function get(stream){return (await all(stream)).map(row=>row.event);}
  async function page(stream,{before='',limit=200}={}){
    stream=String(stream||'');const cap=Math.min(500,Math.max(1,Number(limit)||200)),db=await open(),tx=db.transaction(STORE,'readonly'),completion=done(tx),rows=[];
    /* A cursor is the memory boundary. `getAll()` followed by slice still allocates and sorts every
     * one of a room's 5,000 envelopes, which was enough to kill Android's WebView while opening a
     * busy community. Never await inside this callback: that would let the IDB transaction close
     * before cursor.continue(), silently truncating history. */
    await new Promise((resolve,reject)=>{let found=!before;const range=IDBKeyRange.bound([stream,0],[stream,Number.MAX_SAFE_INTEGER]);const q=tx.objectStore(STORE).index('streamCreated').openCursor(range,'prev');q.onerror=()=>reject(q.error||new Error('Concord cache cursor failed'));q.onsuccess=()=>{const cursor=q.result;if(!cursor||rows.length>=cap){resolve();return;}const row=cursor.value;if(!found){if(row.key===before)found=true;cursor.continue();return;}rows.push(row);cursor.continue();};});
    await completion;rows.reverse();return{events:rows.map(row=>row.event),before:rows.length===cap?rows[0].key:'',done:rows.length<cap};
  }
  async function drop(stream){const rows=await all(stream);if(!rows.length)return 0;const db=await open(),tx=db.transaction(STORE,'readwrite'),completion=done(tx),s=tx.objectStore(STORE);for(const row of rows)s.delete(row.key);await completion;return rows.length;}
  async function putIcon(key,ref,bytes,mime){const data=new Uint8Array(bytes),type=String(mime||'');if(!key||data.byteLength>MAX_ICON_BYTES||!/^image\/(png|jpeg|gif|webp)$/.test(type))return false;const db=await open(),write=db.transaction(ICONS,'readwrite'),writeDone=done(write);write.objectStore(ICONS).put({key:String(key),ref:String(ref),bytes:data,mime:type,at:Date.now(),size:data.byteLength});await writeDone;const read=db.transaction(ICONS,'readonly'),readDone=done(read),[rows]=await Promise.all([request(read.objectStore(ICONS).getAll()),readDone]);rows.sort((a,b)=>(Number(a.at)||0)-(Number(b.at)||0));let total=rows.reduce((n,r)=>n+(Number(r.size)||r.bytes&&r.bytes.byteLength||0),0),count=rows.length;const victims=[];for(const row of rows){if(count<=MAX_ICONS&&total<=MAX_ICON_TOTAL_BYTES)break;victims.push(row.key);count--;total-=Number(row.size)||row.bytes&&row.bytes.byteLength||0;}if(victims.length){const prune=db.transaction(ICONS,'readwrite'),pruneDone=done(prune),s=prune.objectStore(ICONS);for(const victim of victims)s.delete(victim);await pruneDone;}return true;}
  async function getIcon(key,ref){const db=await open(),tx=db.transaction(ICONS,'readonly'),completion=done(tx),[row]=await Promise.all([request(tx.objectStore(ICONS).get(String(key))),completion]);return row&&row.ref===String(ref)&&row.bytes&&row.bytes.byteLength<=MAX_ICON_BYTES&&/^image\/(png|jpeg|gif|webp)$/.test(String(row.mime||''))?row:null;}
  /* EVERY CACHED ICON, IN ONE TRANSACTION. `getIcon` is a read per room, and the caller only
     reaches it after a paint has already drawn the letter glyph — so a community list could never
     show its icons on the first draw however warm the cache was. One pass, one repaint. */
  async function allIcons(){const db=await open(),tx=db.transaction(ICONS,'readonly'),completion=done(tx),[rows]=await Promise.all([request(tx.objectStore(ICONS).getAll()),completion]);return (rows||[]).filter(r=>r&&r.bytes&&r.bytes.byteLength<=MAX_ICON_BYTES&&/^image\/(png|jpeg|gif|webp)$/.test(String(r.mime||'')));}
  async function dropIcon(key){const db=await open(),tx=db.transaction(ICONS,'readwrite'),completion=done(tx);tx.objectStore(ICONS).delete(String(key));await completion;}
  async function dropRoom(prefix){prefix=String(prefix);const db=await open(),read=db.transaction(STORE,'readonly'),readDone=done(read),[rows]=await Promise.all([request(read.objectStore(STORE).getAll()),readDone]),keys=[];for(const row of rows){let room='';try{const parsed=JSON.parse(row.stream);if(Array.isArray(parsed))room=String(parsed[0]||'');}catch(_){}if(room===prefix||(!room&&(row.stream===prefix||row.stream.startsWith(prefix+':')||row.stream.startsWith(prefix+'/'))))keys.push(row.key);}const tx=db.transaction([STORE,ICONS],'readwrite'),completion=done(tx),s=tx.objectStore(STORE);for(const key of keys)s.delete(key);tx.objectStore(ICONS).delete(prefix);await completion;return true;}
  root.PCConcordCache={DB,STORE,ICONS,MAX_PER_STREAM,MAX_EVENT_BYTES,MAX_TOTAL_BYTES,MAX_ICON_BYTES,put,get,page,drop,putIcon,getIcon,allIcons,dropIcon,dropRoom,_reset(){dbPromise=null;}};
})(typeof window==='undefined'?globalThis:window);
