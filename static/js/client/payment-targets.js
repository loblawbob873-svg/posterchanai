/* NIP-A3 (kind 10133). No wallet secrets or automatic payments live here. */
(function(root,factory){
  const api=factory();
  if(typeof module==='object' && module.exports) module.exports=api;
  else root.PCPaymentTargets=api;
})(typeof globalThis!=='undefined'?globalThis:this,function(){
  'use strict';
  const KIND=10133;
  const names=Object.freeze(Object.assign(Object.create(null),{lightning:'Lightning',bitcoin:'Bitcoin',monero:'Monero',ethereum:'Ethereum',
    solana:'Solana',litecoin:'Litecoin',bitcoincash:'Bitcoin Cash',zcash:'Zcash',nano:'Nano',
    dash:'Dash',bip352:'Silent Payments',bip353:'Bitcoin DNS address',cashme:'Cash App',
    paypal:'PayPal',revolut:'Revolut',venmo:'Venmo'}));
  const schemes=new Set(['lightning','bitcoin','monero','ethereum','solana','litecoin',
    'bitcoincash','zcash','nano','dash']);
  function target(type,address){
    if(typeof type!=='string' || typeof address!=='string') return null;
    type=type.trim().toLowerCase(); address=address.trim();
    if(!/^[a-z0-9][a-z0-9-]{0,63}$/.test(type) || !address || address.length>4096 || /[\s\x00-\x1f\x7f]/u.test(address)) return null;
    return {type,address};
  }
  function parse(event){
    if(!event || event.kind!==KIND || !Array.isArray(event.tags)) return [];
    const seen=new Set(),out=[];
    for(const tag of event.tags){
      if(!Array.isArray(tag) || tag[0]!=='payto') continue;
      const t=target(tag[1],tag[2]); if(!t) continue;
      const key=JSON.stringify([t.type,t.address]); if(seen.has(key)) continue;
      seen.add(key);out.push(t);
    }
    return out;
  }
  function uri(t){
    t=target(t&&t.type,t&&t.address);if(!t) return '';
    // Never use an untrusted type as a URI scheme, nor accept amount/query injection.
    const address=encodeURIComponent(t.address);
    return schemes.has(t.type)?t.type+':'+address:'payto://'+t.type+'/'+address;
  }
  function buildTags(rows,previous){
    const tags=(previous&&Array.isArray(previous.tags)?previous.tags:[])
      .filter(t=>Array.isArray(t)&&t[0]!=='payto').map(t=>t.slice());
    const seen=new Set();
    for(const row of rows){
      const t=target(row.type,row.address);if(!t) throw new Error('Enter a payment type and an address without spaces.');
      const key=JSON.stringify([t.type,t.address]);if(seen.has(key))continue;seen.add(key);
      // Keep extension fields belonging to an unchanged destination from another client.
      const old=(previous&&previous.tags||[]).find(x=>Array.isArray(x)&&x[0]==='payto'&&x[1]===t.type&&x[2]===t.address);
      tags.push(old?old.slice():['payto',t.type,t.address]);
    }
    if(!tags.some(t=>t[0]==='alt')) tags.push(['alt','Payment targets']);
    return tags;
  }
  function newest(events,owner,verify){
    return events.filter(e=>e&&e.kind===KIND&&e.pubkey===owner&&Number.isSafeInteger(e.created_at)
      &&e.created_at>=0&&/^[a-f0-9]{64}$/.test(e.id||'')&&verify(e))
      .sort((a,b)=>b.created_at-a.created_at || a.id.localeCompare(b.id))[0]||null;
  }
  function createResolver({read,local=()=>[],remember=()=>{},verify,deleted=()=>false,now=Date.now,cache=new Map(),pending=new Map()}){
    const valid=e=>{try{return !!verify(e)&&!deleted(e);}catch(_){return false;}};
    async function load(owner,{force=false}={}){
      if(!/^[a-f0-9]{64}$/.test(owner||''))throw new Error('Invalid payment recipient.');
      const held=cache.get(owner),stored=newest(local(owner)||[],owner,valid);
      // A newer signed event received by the live store invalidates the memory cache immediately.
      if(!force&&held&&now()<held.expires&&(!held.event||valid(held.event))&&(!stored||(held.event&&newest([stored,held.event],owner,valid).id===held.event.id)))return held;
      if(pending.has(owner))return pending.get(owner);
      const job=(async()=>{
        let result={events:[],complete:false};try{result=await read(owner);}catch(_){}
        const received=newest(result.events||[],owner,valid);
        const event=newest([stored,held&&held.event,received].filter(Boolean),owner,valid);
        const state={event,targets:parse(event),available:!!received||result.complete===true,
          expires:now()+(event?60000:10000)};
        if(event)remember(event);
        cache.set(owner,state);return state;
      })();
      pending.set(owner,job);
      try{return await job;}finally{pending.delete(owner);}
    }
    function accept(event){
      if(!event||!newest([event],event.pubkey,valid))throw new Error('Invalid signed payment targets.');
      remember(event);cache.set(event.pubkey,{event,targets:parse(event),available:true,expires:now()+60000});
    }
    return {load,accept};
  }
  return {KIND,names,target,parse,uri,buildTags,newest,createResolver};
});
