/* CORD-07: blind broker grants, sender keys and authenticated call presence. */
(function(root){
  'use strict';
  const utf8=new TextEncoder(), hex=b=>Array.from(new Uint8Array(b),v=>v.toString(16).padStart(2,'0')).join('');
  const unhex=s=>{if(!/^[0-9a-f]{64}$/.test(s))throw Error('Invalid call room');return Uint8Array.from(s.match(/../g),h=>parseInt(h,16));};
  const hash=b=>crypto.subtle.digest('SHA-256',b);
  function origin(raw){
    try{const u=new URL(raw);return u.protocol==='https:'&&!u.username&&!u.password?u.origin:null;}catch(_){return null;}
  }
  async function rank(room,broker){
    const b=origin(broker);if(!b)throw Error('A call broker must use HTTPS');
    const text=utf8.encode(b),wire=new Uint8Array(32+text.length);wire.set(unhex(room));wire.set(text,32);
    return hex(await hash(wire));
  }
  async function brokers(room,values){
    const all=[...new Set(values.map(origin).filter(Boolean))].slice(0,32);
    const ranked=await Promise.all(all.map(async value=>({value,rank:await rank(room,value)})));
    return ranked.sort((a,b)=>a.rank<b.rank?-1:a.rank>b.rank?1:0).map(r=>r.value);
  }
  async function senderKey(mediaRoot,identity){
    if(!(mediaRoot instanceof Uint8Array)||mediaRoot.length!==32||typeof identity!=='string'||!identity||identity.length>512)
      throw Error('Invalid call sender key material');
    const id=new Uint8Array(await hash(utf8.encode(identity))),label=utf8.encode('concord/voice-sender');
    const info=new Uint8Array(label.length+1+32);info.set(label);info.set(id,label.length+1);
    const key=await crypto.subtle.importKey('raw',mediaRoot,'HKDF',false,['deriveBits']);
    return new Uint8Array(await crypto.subtle.deriveBits({name:'HKDF',hash:'SHA-256',salt:new Uint8Array(),info},key,256));
  }
  function fold(events,now=Date.now()){
    const latest=new Map();
    for(const e of events){
      if(!e||!Number.isSafeInteger(e.at)||e.at>now+60000||!/^([0-9a-f]{64})$/.test(e.pubkey)||!Array.isArray(e.tags))continue;
      if(e.content!=='joined'&&e.content!=='left')continue;
      const old=latest.get(e.pubkey);
      if(!old||e.at>old.at||(e.at===old.at&&String(e.id)<String(old.id)))latest.set(e.pubkey,e);
    }
    const present=[];
    for(const e of latest.values()){
      if(e.content!=='joined'||now-e.at>=90000)continue;
      const identities=e.tags.filter(t=>t[0]==='identity'),bs=e.tags.filter(t=>t[0]==='broker');
      if(identities.length!==1||bs.length!==1)continue;
      const identity=identities[0][1],broker=origin(bs[0][1]);
      if(typeof identity!=='string'||!identity||identity.length>512||!broker||broker!==bs[0][1])continue;
      present.push({pubkey:e.pubkey,identity,broker,at:e.at});
    }
    const claims=new Map();for(const p of present)claims.set(p.identity,(claims.get(p.identity)||0)+1);
    return present.map(p=>({...p,verified:claims.get(p.identity)===1}));
  }
  async function token(material,broker,{fetcher=root.fetch,signal,now=Date.now()}={}){
    const base=origin(broker);if(!base)throw Error('A call broker must use HTTPS');
    const timeout=new AbortController(),timer=setTimeout(()=>timeout.abort(),8000);
    const abort=()=>timeout.abort();signal?.addEventListener('abort',abort,{once:true});
    if(signal?.aborted)timeout.abort();
    try{
      const endpoint=base+'/.well-known/concord/av';
      const opts={credentials:'omit',redirect:'error',referrerPolicy:'no-referrer',signal:timeout.signal};
      const probe=await fetcher(endpoint,opts);
      if(timeout.signal.aborted)throw Error('Call canceled');
      if(probe.status!==204)throw Error('This server does not provide Concord calls');
      const url=endpoint+'/'+material.room;
      const nonce=hex(crypto.getRandomValues(new Uint8Array(16)));
      const grant=root.NostrTools.finalizeEvent({kind:27235,created_at:Math.floor(now/1000),content:'',
        tags:[['u',url],['method','GET'],['nonce',nonce]]},material.signingKey);
      const response=await fetcher(url,{...opts,method:'GET',headers:{Authorization:'Concord '+btoa(JSON.stringify(grant))}});
      if(!response.ok)throw Error('The call broker could not issue a token ('+response.status+')');
      const body=await response.json(),u=new URL(body.url);
      if(timeout.signal.aborted)throw Error('Call canceled');
      if(typeof body.token!=='string'||!body.token||body.token.length>32768||typeof body.identity!=='string'||!body.identity||body.identity.length>512||u.protocol!=='wss:'||u.username||u.password)
        throw Error('The call broker returned an invalid response');
      return {token:body.token,identity:body.identity,url:u.href,broker:base};
    }finally{clearTimeout(timer);signal?.removeEventListener('abort',abort);}
  }
  root.PCCordVoice=Object.freeze({origin,rank,brokers,senderKey,fold,token});
})(globalThis);
