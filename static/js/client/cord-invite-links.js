/* CORD-05 creator-owned link coordinates and encrypted kind13303 bookkeeping. */
(function(){
  'use strict';
  const NT=()=>globalThis.NostrTools;
  const hex=(value,n)=>typeof value==='string'&&new RegExp('^[0-9a-f]{'+n+'}$').test(value);
  const fromHex=value=>Uint8Array.from(value.match(/../g),byte=>parseInt(byte,16));
  const toHex=value=>Array.from(value,byte=>byte.toString(16).padStart(2,'0')).join('');
  const clone=value=>JSON.parse(JSON.stringify(value));
  function active(context){if(!context||!hex(context.pubkey,64)||!context.isCurrent())throw new Error('The account or invitation permission changed');}
  function canonical(value){if(Array.isArray(value))return '['+value.map(canonical).join(',')+']';if(value&&typeof value==='object')return '{'+Object.keys(value).sort().map(key=>JSON.stringify(key)+':'+canonical(value[key])).join(',')+'}';return JSON.stringify(value);}
  function lesser(a,b){const x=new TextEncoder().encode(canonical(a)),y=new TextEncoder().encode(canonical(b));for(let i=0;i<Math.min(x.length,y.length);i++)if(x[i]!==y[i])return x[i]<y[i]?a:b;return x.length<=y.length?a:b;}
  function mergeFields(a,b){const result={...a};for(const key of Object.keys(b)){if(!Object.prototype.hasOwnProperty.call(result,key))Object.defineProperty(result,key,{value:b[key],writable:true,enumerable:true,configurable:true});else if(canonical(result[key])!==canonical(b[key])){const x=result[key],y=b[key];Object.defineProperty(result,key,{value:x&&y&&typeof x==='object'&&typeof y==='object'&&!Array.isArray(x)&&!Array.isArray(y)?mergeFields(x,y):lesser(x,y),writable:true,enumerable:true,configurable:true});}}return result;}
  function details(entry){
    if(!entry||!hex(entry.token,32)||!hex(entry.signer_sk,64)||!hex(entry.community_id,64)||typeof entry.url!=='string'||entry.url.length>4096)throw new Error('Invalid creator invite entry');
    const url=new URL(entry.url),decoded=NT().nip19.decode(url.pathname.split('/').filter(Boolean).pop());
    const pk=NT().getPublicKey(fromHex(entry.signer_sk));
    if(!['https:','http:'].includes(url.protocol)||decoded.type!=='naddr'||decoded.data.kind!==33301||decoded.data.identifier!==''||decoded.data.pubkey!==pk||decoded.data.relays.length)throw new Error('Invite coordinate does not match its signing key');
    const fragment=Uint8Array.from(atob(url.hash.slice(1).replace(/-/g,'+').replace(/_/g,'/')),c=>c.charCodeAt(0));
    if(fragment.length<18||fragment[0]!==4||toHex(fragment.slice(-16))!==entry.token)throw new Error('Invite token does not match its URL');
    const parsed=globalThis.PosterCord?.inviteDetails(entry.url);
    if(!parsed)throw new Error('Invite URL parser is unavailable');
    return {pubkey:pk,relays:parsed.bootstrapRelays||[]};
  }
  function mergeLists(documents){
    let result={};const entries=new Map(),tombs=new Map();
    for(const source of documents){
      if(!source||typeof source!=='object'||!Array.isArray(source.entries)||!Array.isArray(source.tombstones)||source.entries.length>4096||source.tombstones.length>4096)throw new Error('Invalid or oversized Invite List');
      const rest={...source};delete rest.entries;delete rest.tombstones;result=mergeFields(result,rest);
      for(const entry of source.entries){details(entry);const prior=entries.get(entry.token);if(prior&&['signer_sk','community_id','url'].some(key=>prior[key]!==entry[key]))throw new Error('Conflicting immutable invite entry');entries.set(entry.token,entries.has(entry.token)?mergeFields(entries.get(entry.token),entry):clone(entry));}
      for(const tomb of source.tombstones){if(!tomb||!hex(tomb.token,32)||!hex(tomb.community_id,64))throw new Error('Invalid invite tombstone');tombs.set(tomb.token,tombs.has(tomb.token)?mergeFields(tombs.get(tomb.token),tomb):clone(tomb));}
    }
    result.entries=[...entries.values()].filter(entry=>!tombs.has(entry.token)).sort((a,b)=>a.token.localeCompare(b.token));
    result.tombstones=[...tombs.values()].sort((a,b)=>a.token.localeCompare(b.token));
    return result;
  }
  async function verified(events,context,kind,author){
    active(context);if(!Array.isArray(events)||events.length>65)throw new Error('Invite history exceeds the safe merge limit');const copy=clone(events);
    const valid=await context.verify(copy);active(context);
    return valid.filter(event=>event.kind===kind&&event.pubkey===author&&Number.isSafeInteger(event.created_at)&&event.created_at>=0);
  }
  const cached=new Map(),writes=new Map();
  const cacheKey=pubkey=>'pc.concord.invite-list.v1.'+pubkey;
  function cachedEvent(context){if(cached.has(context.pubkey))return cached.get(context.pubkey);try{const raw=localStorage.getItem(cacheKey(context.pubkey));return raw&&raw.length<150000?JSON.parse(raw):null;}catch(_){return null;}}
  async function readList(context){
    active(context);const query=await context.query(13303,context.pubkey);active(context);
    const prior=cachedEvent(context),events=await verified([...(prior?[prior]:[]),...(query.events||[])],context,13303,context.pubkey);
    const docs=[];
    for(const event of events){const text=await context.decrypt(context.pubkey,event.content);active(context);if(new TextEncoder().encode(text).length>65535)throw new Error('Invite List exceeds NIP-44 size limit');docs.push(JSON.parse(text));}
    return {list:mergeLists(docs.length?docs:[{entries:[],tombstones:[]}]),complete:query.complete===true,latest:Math.max(0,...events.map(event=>event.created_at))};
  }
  async function writeList(context,mutate){
    active(context);const previous=writes.get(context.pubkey)||Promise.resolve();
    const job=previous.catch(()=>{}).then(async()=>{
      const read=await readList(context);if(!read.complete)throw new Error('Invite List sync is incomplete; retry before changing it');
      const next=mergeLists([mutate(clone(read.list))]);const text=JSON.stringify(next);
      if(new TextEncoder().encode(text).length>65535)throw new Error('Invite List is too large; existing entries were preserved');
      const content=await context.encrypt(context.pubkey,text);active(context);
      const event=await context.sign({kind:13303,created_at:Math.max(Math.floor(Date.now()/1000),read.latest+1),tags:[],content});active(context);
      if(!(await verified([event],context,13303,context.pubkey)).length)throw new Error('Invalid Invite List signature');
      const acknowledgment=await context.publish(event);active(context);if(acknowledgment===false)throw new Error('Invite List was not accepted by a relay');
      cached.set(context.pubkey,clone(event));try{localStorage.setItem(cacheKey(context.pubkey),JSON.stringify(event));}catch(_){}
      return next;
    });writes.set(context.pubkey,job);return job;
  }
  function remember(entry,context){details(entry);return writeList(context,list=>{if(list.tombstones.some(t=>t.token===entry.token))throw new Error('This invitation was retired');return {...list,entries:[...list.entries,entry]};});}
  function forget(entry,context){details(entry);return writeList(context,list=>({...list,entries:list.entries.filter(e=>e.token!==entry.token),tombstones:[...list.tombstones,{token:entry.token,community_id:entry.community_id}]}));}
  async function bundleKey(token){const key=await crypto.subtle.importKey('raw',fromHex(token),'HKDF',false,['deriveBits']);return new Uint8Array(await crypto.subtle.deriveBits({name:'HKDF',hash:'SHA-256',salt:new Uint8Array(),info:new Uint8Array([...new TextEncoder().encode('concord/invite-key'),0,...new Uint8Array(32)])},key,256));}
  function publicBundle(input){const result={};for(const key of ['community_id','owner','owner_salt','community_root','root_epoch','control_pk','channels','relays','name','icon','expires_at','creator_npub','label'])if(input[key]!==undefined)result[key]=input[key];if(!Array.isArray(result.channels)||result.channels.length>256)throw new Error('Invalid channels');result.channels=result.channels.map(ch=>({id:ch.id,key:ch.key,epoch:ch.epoch,...(ch.name!==undefined?{name:ch.name}:{})}));return globalThis.PosterCordReader.validateInviteBundle(result,{forJoin:true});}
  function fragment(relays,token){const dictionary=['wss://jskitty.com/nostr','wss://asia.vectorapp.io/nostr','wss://relay.ditto.pub','wss://relay.dreamith.to'];const selected=[...new Set(relays)].slice(0,3),out=[4,0,selected.length];for(const relay of selected){const index=dictionary.indexOf(relay);if(index>=0)out.push(index+1);else{const secure=relay.startsWith('wss://'),bytes=new TextEncoder().encode(secure?relay.slice(6):relay);if(bytes.length>255)throw new Error('Bootstrap relay URL is too long');out.push(secure?0:255,bytes.length,...bytes);}}out.push(...fromHex(token));return btoa(String.fromCharCode(...out)).replace(/\+/g,'-').replace(/\//g,'_').replace(/=+$/,'');}
  async function create(input,context,options={}){
    active(context);const bundle=publicBundle(input),token=toHex(crypto.getRandomValues(new Uint8Array(16))),sk=NT().generateSecretKey(),pk=NT().getPublicKey(sk);
    const pointer=NT().nip19.naddrEncode({kind:33301,pubkey:pk,identifier:'',relays:[]});
    const base=String(options.base||'https://poster.place').replace(/\/$/,'');
    const entry={token,signer_sk:toHex(sk),community_id:bundle.community_id,url:base+'/invite/'+pointer+'#'+fragment(bundle.relays,token),created_at:Math.floor(Date.now()/1000),...(options.label?{label:String(options.label)}:{}),...(bundle.expires_at!==undefined?{expires_at:bundle.expires_at}:{})};
    details(entry);return {entry,event:await refreshEvent(entry,bundle,[],context)};
  }
  async function linkState(entry,context){const info=details(entry),query=await context.query(33301,info.pubkey,info.relays);active(context);const events=(await verified(query.events,context,33301,info.pubkey)).filter(event=>event.tags.filter(t=>t[0]==='d').length===1&&event.tags.some(t=>t[0]==='d'&&t[1]===''));return {events,complete:query.complete===true,retired:events.some(event=>event.tags.some(t=>t[0]==='vsk'&&t[1]==='9'))};}
  async function refreshEvent(entry,input,observed,context){
    active(context);details(entry);const bundle=publicBundle({...input,...(entry.expires_at!==undefined?{expires_at:entry.expires_at}:{})});if(bundle.community_id!==entry.community_id)throw new Error('Invitation belongs to a different community');
    if(observed.some(event=>event.tags?.some(t=>t[0]==='vsk'&&t[1]==='9')))throw new Error('A retired link cannot be refreshed');
    const key=await bundleKey(entry.token);active(context);
    return NT().finalizeEvent({kind:33301,created_at:Math.max(Math.floor(Date.now()/1000),...observed.map(event=>event.created_at+1)),tags:[['d',''],['vsk','6']],content:NT().nip44.encrypt(JSON.stringify(bundle),key)},fromHex(entry.signer_sk));
  }
  function revokeEvent(entry,observed,context){active(context);details(entry);return NT().finalizeEvent({kind:33301,created_at:Math.max(Math.floor(Date.now()/1000),...observed.map(event=>event.created_at+1)),tags:[['d',''],['vsk','9']],content:''},fromHex(entry.signer_sk));}
  globalThis.PCCordInviteLinks=Object.freeze({details,mergeLists,readList,remember,forget,create,linkState,refreshEvent,revokeEvent});
})();
