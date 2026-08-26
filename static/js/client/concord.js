/* Concord communities — a dedicated Discord/Matrix-shaped workspace (separate from public Chat). */
(function(){
  'use strict';
  // Do not depend on the monolithic shell CSS being current: an older service worker may serve its
  // cached client.css for one navigation. Concord owns a versioned sheet and loads it itself too.
  if(!document.querySelector('link[data-concord-css]')){
    const l=document.createElement('link'); l.rel='stylesheet'; l.dataset.concordCss='1';
    l.href='/static/css/concord.css?v=16'; (document.head||document.documentElement).appendChild(l);
  }
  const PC=()=>window.__PC;
  const CORD_RELAYS=['wss://jskitty.com/nostr','wss://asia.vectorapp.io/nostr','wss://relay.ditto.pub','wss://relay.dreamith.to'];
  const DISCOVER_RELAYS=['wss://relay.ditto.pub','wss://relay.dreamith.to'];
  async function cordQuery(p,relays,filters,{timeout=8000,max=8}={}){
    /* queryFrom intentionally skips relays already owned by the shared pool. Always ask both paths:
       otherwise opening a room can silently omit the newest wraps from whichever relay is connected. */
    const jobs=[];
    if(p.relayQuery)jobs.push(Promise.resolve(p.relayQuery(filters,timeout)).catch(()=>[]));
    if(p.relayQueryFrom)jobs.push(Promise.resolve(p.relayQueryFrom(relays,filters,{timeout,max})).catch(()=>[]));
    const batches=await Promise.all(jobs),byId=new Map();
    for(const ev of batches.flat())if(ev&&ev.id)byId.set(ev.id,ev);
    return [...byId.values()];
  }
  const state={ community:null, channel:null };
  let replyTarget=null, reactionTarget=null, mobileChatOpen=false, discoveryOpen=false;
  let discovered=[], discoveryStarted=false, discoveryLoaded=false, membershipBusy=false, membershipRetryTimer=null;
  const discoveryIconLoads=new Set();
  const recoveredOwnedInvites=new Set();
  const roomLoads=new Map();
  const roomControls=new Map();
  /* Blob URLs die with their renderer. Keep encrypted icon pointer identity in memory so a saved
   * room never suppresses re-decryption after the next browser/native-shell launch. */
  const roomIconRefs=new Map();
  const pendingAttachments=new Map();
  const attachmentCache=new Map(),attachmentLoads=new Map();
  const scrollStates=new Map();
  let liveTimer=null,liveBusy=false,metadataBusy=false,metadataCursor=0;
  function saved(){ try{ const v=JSON.parse(localStorage.getItem('pc.concord.invites')||'[]'); return Array.isArray(v)?v:[]; }catch(_){ return []; } }
  function save(v){ try{ localStorage.setItem('pc.concord.invites',JSON.stringify(v.slice(0,50))); }catch(_){} }
  function scrollKey(){ const room=state.community==null?null:saved()[state.community]; return `${room&&(room.communityId||room.naddr||room.url)||'home'}:${state.channel||'general'}`; }
  function readScroll(key){ if(scrollStates.has(key))return scrollStates.get(key); try{ const v=JSON.parse(sessionStorage.getItem('pc.concord.scroll.'+key)||'null'); if(v&&typeof v==='object')return v; }catch(_){} return {pinned:true}; }
  function writeScroll(key,st){ scrollStates.set(key,st); try{ sessionStorage.setItem('pc.concord.scroll.'+key,JSON.stringify({top:Number(st.top)||0,height:Number(st.height)||0,pinned:st.pinned!==false})); }catch(_){} }
  function scrollChatBottom(){ const key=scrollKey(),st=readScroll(key); st.pinned=true; writeScroll(key,st); const later=window.requestAnimationFrame||((fn)=>setTimeout(fn,0)); later(()=>{ const box=document.querySelector('.cc-messages'); if(box){box.scrollTop=box.scrollHeight;st.top=box.scrollTop;st.height=box.scrollHeight;writeScroll(key,st);} }); }
  function preserveChatScroll(fn){ const key=scrollKey(),old=document.querySelector('.cc-messages'),st=readScroll(key),top=old?old.scrollTop:Number(st.top)||0,height=old?old.scrollHeight:Number(st.height)||0; fn(); const later=window.requestAnimationFrame||((f)=>setTimeout(f,0)); later(()=>{ const box=document.querySelector('.cc-messages'); if(box){box.scrollTop=st.pinned!==false?box.scrollHeight:top+(box.scrollHeight-height);st.top=box.scrollTop;st.height=box.scrollHeight;writeScroll(key,st);} }); }
  function restoreChatScroll(){ const key=scrollKey(),st=readScroll(key),later=window.requestAnimationFrame||((f)=>setTimeout(f,0)); later(()=>{ const box=document.querySelector('.cc-messages'); if(box){box.scrollTop=st.pinned!==false?box.scrollHeight:Number(st.top)||0;st.top=box.scrollTop;st.height=box.scrollHeight;writeScroll(key,st);} }); }
  function roomName(r,i){ return (r&&r.name)||`Encrypted community ${i+1}`; }
  function normalizeIcon(raw){
    const v=String(raw||'').trim(); if(!v)return '';
    try{ const u=new URL(v); if(u.protocol==='https:'||u.protocol==='http:'||u.protocol==='blob:')return u.href; }catch(_){}
    return Array.from(v).slice(0,4).join('');
  }
  function roomIcon(p,r,i){
    const icon=normalizeIcon(r&&r.icon);
    if(/^(https?:\/\/|blob:)/i.test(icon)) return `<img class="cc-server-img" src="${p.enc(icon)}" alt="">`;
    return `<span class="cc-server-glyph">${p.enc(icon||roomName(r,i).slice(0,2).toUpperCase())}</span>`;
  }
  function publicRoomIcon(p,r){
    const icon=normalizeIcon(r&&r.icon);
    if(/^(https?:\/\/|blob:)/i.test(icon))return `<img src="${p.enc(icon)}" alt="" loading="lazy">`;
    return `<span>${p.enc(icon||(r.name||'C').slice(0,2).toUpperCase())}</span>`;
  }
  /* ONE RUMOR, ONE ROW.
   *
   * Sending is optimistic.  While createChatWrap/publish is still resolving, the four-second live
   * query can already receive that rumor and append its permanent id.  The publish continuation
   * then renames the pending row to the SAME id.  Previously both rows were saved and the next live
   * pass built a dedupe Map but declined to persist it when no new event arrived, so one send could
   * remain visibly doubled forever.  Normalize at both storage boundaries: writes prevent the race,
   * reads repair caches produced by an older build.  Later relay data wins while retaining useful
   * optimistic fields it does not carry (for example the already-painted reply summary). */
  function uniqueMessages(v){
    const byId=new Map();
    for(const m of Array.isArray(v)?v:[]){
      if(!m||typeof m!=='object')continue;
      const id=messageId(m),old=byId.get(id);
      byId.set(id,old?{...old,...m}:m);
    }
    return [...byId.values()];
  }
  function testMessages(id){ try{ const v=JSON.parse(localStorage.getItem('pc.concord.test.'+id)||'[]'); return uniqueMessages(v); }catch(_){ return []; } }
  function saveTestMessages(id,v){ try{ localStorage.setItem('pc.concord.test.'+id,JSON.stringify(uniqueMessages(v).slice(-200))); }catch(_){} }
  function channelStoreId(room,name){ const channel=name||'general',c=room&&Array.isArray(room.channels)&&room.channels.find(x=>x.name===channel); return room&&room.naddr+(channel!=='general'&&c&&c.id?'.'+c.id:''); }
  function channelsOf(room){
    const channels=room&&Array.isArray(room.channels)?room.channels.filter(c=>c&&c.name):[];
    return channels.length?channels:[{name:'general',private:false}];
  }
  function activeMessages(room){ return testMessages(channelStoreId(room,state.channel)); }
  function lastActivity(room){ return channelsOf(room).reduce((n,c)=>Math.max(n,...testMessages(channelStoreId(room,c.name)).map(x=>Number(x.at)||0)),0); }
  function seenAt(room){ return Number(localStorage.getItem('pc.concord.read.'+(room&&room.naddr||''))||0); }
  function markRead(room){ if(room&&room.naddr)localStorage.setItem('pc.concord.read.'+room.naddr,String(Date.now())); }
  function isUnread(room){ return lastActivity(room)>seenAt(room); }
  function messageId(m){ return String((m&&m.id)||`${Number(m&&m.at)||0}:${String(m&&m.pubkey||'')}`); }
  function imetaFields(tag){
    const out={}; if(!Array.isArray(tag)||tag[0]!=='imeta')return out;
    for(const raw of tag.slice(1)){const s=String(raw||''),i=s.indexOf(' ');if(i>0)out[s.slice(0,i)]=s.slice(i+1);}
    return out;
  }
  /* Armada encrypts room attachments independently from the already-encrypted chat rumor. The URL
   * therefore points at ciphertext; rendering it through the ordinary link/media helper makes an
   * extensionless blob look like a video, then disappear as soon as Play discovers invalid bytes. */
  function encryptedAttachments(m){
    const out=[];
    for(const tag of (m&&m.tags||[])){
      const f=imetaFields(tag),alg=String(f['encryption-algorithm']||'').toLowerCase();
      if(alg!=='aes-gcm'||!/^https:\/\//i.test(f.url||''))continue;
      if(!/^[0-9a-f]{64}$/i.test(f['decryption-key']||'')||!/^[0-9a-f]{24,32}$/i.test(f['decryption-nonce']||'')||!/^[0-9a-f]{64}$/i.test(f.ox||''))continue;
      const mime=/^[\w.+-]+\/[\w.+-]+$/.test(f.m||'')?f.m.toLowerCase():'application/octet-stream';
      out.push({url:f.url,key:f['decryption-key'],nonce:f['decryption-nonce'],hash:f.ox.toLowerCase(),mime,name:String(f.name||'attachment').slice(0,120)});
    }
    return out;
  }
  function messageContentHtml(p,m){
    const files=encryptedAttachments(m); let text=String(m&&m.text||'');
    for(const f of files)text=text.split(f.url).join('').trim();
    const body=text?`<p>${p.linkify?p.linkify(text):p.enc(text)}</p>${p.linkCardHtml?p.linkCardHtml(text):''}`:'';
    const media=files.map((f,i)=>`<div class="cc-encrypted-attachment" data-cc-attachment="${p.enc(messageId(m))}" data-cc-attachment-index="${i}"><span>🔒 Decrypting ${p.enc(f.name||f.mime)}…</span></div>`).join('');
    return body+media;
  }
  async function decryptAttachment(file){
    const ck=file.url+'\0'+file.hash; if(attachmentCache.has(ck))return attachmentCache.get(ck);
    if(attachmentLoads.has(ck))return attachmentLoads.get(ck);
    const work=(async()=>{const res=await fetch(file.url,{credentials:'omit'});if(!res.ok)throw new Error('download failed');const cipher=await res.arrayBuffer();if(cipher.byteLength>50*1024*1024)throw new Error('attachment is too large');const key=await crypto.subtle.importKey('raw',hexBytes(file.key),'AES-GCM',false,['decrypt']);const plain=new Uint8Array(await crypto.subtle.decrypt({name:'AES-GCM',iv:hexBytes(file.nonce)},key,cipher));const hash=bytesHex(await crypto.subtle.digest('SHA-256',plain));if(hash!==file.hash)throw new Error('integrity check failed');const value={url:URL.createObjectURL(new Blob([plain],{type:file.mime})),mime:file.mime,name:file.name};attachmentCache.set(ck,value);while(attachmentCache.size>64){const [old,v]=attachmentCache.entries().next().value;attachmentCache.delete(old);try{URL.revokeObjectURL(v.url);}catch(_){}}return value;})();
    attachmentLoads.set(ck,work);try{return await work;}finally{attachmentLoads.delete(ck);}
  }
  async function hydrateEncryptedAttachments(messages){
    if(!document.querySelectorAll)return;
    const byId=new Map((messages||[]).map(m=>[messageId(m),m]));
    for(const host of document.querySelectorAll('.cc-encrypted-attachment[data-cc-attachment]')){
      const m=byId.get(host.dataset.ccAttachment),file=m&&encryptedAttachments(m)[Number(host.dataset.ccAttachmentIndex)||0];if(!file)continue;
      try{const got=await decryptAttachment(file);if(!host.isConnected)continue;const url=PC().enc(got.url),label=PC().enc(got.name||'attachment');if(got.mime.startsWith('image/'))host.innerHTML=`<img src="${url}" alt="${label}" loading="lazy">`;else if(got.mime.startsWith('video/'))host.innerHTML=`<video src="${url}" controls playsinline preload="metadata"></video>`;else if(got.mime.startsWith('audio/'))host.innerHTML=`<audio src="${url}" controls preload="metadata"></audio>`;else host.innerHTML=`<a href="${url}" download="${label}">Download ${label}</a>`;}catch(_){if(host.isConnected)host.innerHTML='<span class="cc-attachment-error">Could not decrypt attachment</span>';}
    }
  }
  function channelStarKey(room,name){ return `pc.concord.star.${room&&(room.communityId||room.naddr||room.url)||'unknown'}:${name||'general'}`; }
  function channelStarred(room,name){ try{return localStorage.getItem(channelStarKey(room,name))==='1';}catch(_){return false;} }
  function setChannelStarred(room,name,on){ try{if(on)localStorage.setItem(channelStarKey(room,name),'1');else localStorage.removeItem(channelStarKey(room,name));}catch(_){} }
  function orderedChannels(room){ return channelsOf(room).map((channel,index)=>({channel,index,starred:channelStarred(room,channel.name)})).sort((a,b)=>Number(b.starred)-Number(a.starred)||a.index-b.index).map(x=>x.channel); }
  function threadParticipants(messages,target,viewerPubkey){
    const byId=new Map((messages||[]).map(m=>[messageId(m),m])),seen=new Set(),people=new Set();
    let node=target;
    while(node&&!seen.has(messageId(node))){
      seen.add(messageId(node));
      if(node.pubkey&&node.pubkey!==viewerPubkey)people.add(node.pubkey);
      const parentId=(node.reply&&node.reply.id)||((node.tags||[]).find(t=>t[0]==='e'||t[0]==='E')||[])[1];
      node=parentId&&byId.get(String(parentId));
    }
    return [...people];
  }
  function webxdcOf(m){
    for(const t of (m&&m.tags||[])){ if(t[0]!=='imeta')continue; const f={}; for(let i=1;i<t.length;i++){ const s=String(t[i]||''),j=s.indexOf(' '); if(j>0)f[s.slice(0,j)]=s.slice(j+1); } if((f.m||'').toLowerCase()==='application/x-webxdc'&&/^https?:\/\//i.test(f.url||''))return {url:f.url,sha:f.x||'',uuid:f.webxdc||messageId(m),name:(f.summary||f.name||'Mini app').slice(0,80)}; }
    const url=(String(m&&m.text||'').match(/https?:\/\/[^\s<>]+\.xdc(?:\?[^\s<>]*)?/i)||[])[0]; return url?{url,sha:'',uuid:messageId(m),name:'Mini app'}:null;
  }
  function webxdcHtml(m){ const app=webxdcOf(m); return app&&window.PCWebxdc&&PCWebxdc.cardHtml?PCWebxdc.cardHtml(app):''; }
  function hydrateWebxdcCards(room){ if(!window.PCWebxdc||!PCWebxdc.cardHtml||!document.querySelectorAll)return; const byId=new Map(activeMessages(room).map(m=>[messageId(m),m])); document.querySelectorAll('.cc-message[data-message-id]').forEach(el=>{ const m=byId.get(el.dataset.messageId),html=m&&webxdcHtml(m),body=el.querySelector('.cc-message-body'); if(html&&body&&!body.querySelector('.xdc-card'))body.insertAdjacentHTML('beforeend',html); }); }
  function hexBytes(s){ const h=String(s||''); if(!/^[0-9a-f]+$/i.test(h)||h.length%2)throw new Error('invalid encrypted image key'); return new Uint8Array(h.match(/../g).map(x=>parseInt(x,16))); }
  function bytesHex(a){ return [...new Uint8Array(a)].map(x=>x.toString(16).padStart(2,'0')).join(''); }
  function imageMime(a){ return a[0]===0x89&&a[1]===0x50?'image/png':a[0]===0xff&&a[1]===0xd8?'image/jpeg':a[0]===0x47&&a[1]===0x49?'image/gif':a[0]===0x52&&a[1]===0x49&&a[8]===0x57?'image/webp':'image/*'; }
  async function decryptImagePointer(pointer){
    const res=await fetch(pointer.url); if(!res.ok)throw new Error('community icon download failed');
    const key=await crypto.subtle.importKey('raw',hexBytes(pointer.key),'AES-GCM',false,['decrypt']);
    const plain=new Uint8Array(await crypto.subtle.decrypt({name:'AES-GCM',iv:hexBytes(pointer.nonce)},key,await res.arrayBuffer()));
    const hash=bytesHex(await crypto.subtle.digest('SHA-256',plain)); if(hash!==String(pointer.hash).toLowerCase())throw new Error('community icon failed integrity check');
    return URL.createObjectURL(new Blob([plain],{type:imageMime(plain)}));
  }
  function reactionSummary(p,m){
    const reactions=m&&m.reactions&&typeof m.reactions==='object'?m.reactions:{};
    const viewer=p.viewer?p.viewer():{};
    return Object.entries(reactions).map(([emoji,people])=>{ const n=Array.isArray(people)?people.length:0,mine=!!(viewer.pubkey&&people.includes(viewer.pubkey)); return n?`<button class="cc-reaction${mine?' mine':''}" aria-pressed="${mine}" data-cc-react-toggle="${p.enc(messageId(m))}" data-cc-emoji="${p.enc(emoji)}" title="${n} reaction${n===1?'':'s'}"><span>${p.enc(emoji)}</span><b>${n}</b></button>`:''; }).join('');
  }
  function notifyMentions(p,room,messages,viewer,me){
    if(!room||!room.naddr||!messages.length||!viewer.pubkey)return;
    const key='pc.concord.seen.'+room.naddr, newest=Math.max(...messages.map(m=>Number(m.at)||0));
    let seen=Number(localStorage.getItem(key)||0);
    if(!seen){ localStorage.setItem(key,String(newest)); return; } // opening history must not alert
    const profile=viewer.profile||{}, handles=[me,profile.name,profile.display_name,viewer.npub,viewer.pubkey]
      .filter(Boolean).flatMap(v=>[String(v).toLowerCase(),String(v).toLowerCase().replace(/\s+/g,'')]);
    for(const m of messages){
      const body=String(m.text||''), lower=body.toLowerCase().replace(/\s+/g,''), fromMe=m.pubkey===viewer.pubkey;
      const mentioned=!fromMe&&handles.some(h=>h&&(lower.includes('@'+h)||lower.includes(h.startsWith('npub1')||h.length===64?h:'@'+h)));
      if((Number(m.at)||0)>seen&&mentioned&&p.osNotify) p.osNotify(`Mention in #${state.channel||'general'}`,`${m.by||'Someone'}: ${body}`,{tag:'concord-mention-'+room.naddr,route:'concord'});
    }
    if(newest>seen)localStorage.setItem(key,String(newest));
  }
  function inviteParts(raw){
    let u; try{ u=new URL(String(raw||'').trim()); }catch(_){ return null; }
    const m=u.pathname.match(/\/invite\/(naddr1[023456789acdefghjklmnpqrstuvwxyz]+)\/?$/i);
    return m&&u.hash.length>3 ? {url:u.href,naddr:m[1],secret:u.hash.slice(1)} : null;
  }
  /* An invite is an in-app Concord action, not a web page.  Linkified chat text gives every URL a
   * normal anchor; on desktop a same-instance /invite link was therefore allowed through Electron
   * and opened the server's Classic UI in another PosterChan window.  Keep the complete URL (the
   * fragment is the decryption secret), show it in the existing join surface, and use the exact same
   * join button as a pasted invite so the two paths cannot drift. */
  function openInviteLink(raw,autoJoin=true){
    const parsed=inviteParts(raw); if(!parsed)return false;
    const panel=document.querySelector('#cc-join'),input=document.querySelector('#cc-invite-url');
    if(!panel||!input)return false;
    panel.classList.remove('hidden'); input.value=parsed.url; input.focus();
    if(autoJoin)setTimeout(()=>{const go=document.querySelector('#cc-join-go');if(go&&!go.disabled)go.click();},0);
    return true;
  }
  function discoverInvites(text,source){
    const matches=String(text||'').match(/https?:\/\/[^\s<>]+\/invite\/naddr1[023456789acdefghjklmnpqrstuvwxyz]+#[A-Za-z0-9_-]+/gi)||[];
    return matches.map(url=>url.replace(/[),.;!?]+$/,'' )).map(url=>{ const parsed=inviteParts(url); if(!parsed)return null; const blurb=String(text).replace(url,'').replace(/#[\w-]+/g,'').replace(/\s+/g,' ').trim(); return {...parsed,name:blurb.slice(0,80)||'Public Concord community',description:blurb,source}; }).filter(Boolean);
  }
  function recoverOwnedInvite(p,item){
    const viewer=p.viewer?p.viewer():{};
    if(!item||!viewer.pubkey||item.source.pubkey!==viewer.pubkey||recoveredOwnedInvites.has(item.naddr))return;
    recoveredOwnedInvites.add(item.naddr);
    const rooms=saved();
    if(rooms.some(r=>r.naddr===item.naddr||r.url===item.url))return;
    rooms.push({url:item.url,naddr:item.naddr,name:item.name,description:item.description||'',channels:[{name:'general',private:false}],local:false});
    save(rooms);
  }
  function startDiscovery(p){
    if(discoveryStarted||!p.relaySubscribe)return; discoveryStarted=true;
    const bySigner=new Map();
    const onEvent=ev=>{ for(const item of discoverInvites(ev.content,ev)){ const old=bySigner.get(item.naddr); if(!old||Number(ev.created_at)>Number(old.source.created_at))bySigner.set(item.naddr,item); recoverOwnedInvite(p,item); } discovered=[...bySigner.values()].sort((a,b)=>Number(b.source.created_at)-Number(a.source.created_at)); discovered.slice(0,24).forEach(item=>hydrateDiscoveredIcon(p,item)); if(state.community==null)render(); };
    const onEose=()=>{ discoveryLoaded=true; if(state.community==null)render(); };
    const filters=[{kinds:[1],search:'armada.buzz/invite',limit:100},{kinds:[1],search:'poster.place/invite',limit:100}];
    try{ p.relaySubscribe(filters,{onEvent,onEose,live:true}); if(p.relayQueryFrom)p.relayQueryFrom(DISCOVER_RELAYS,filters,{timeout:6000,max:2}).then(events=>{events.forEach(onEvent);onEose();}); }catch(_){ discoveryLoaded=true; }
  }
  async function hydrateInvite(p,url){
    if(!window.PosterCord||!p.relayQueryFrom)throw new Error('CORD protocol is unavailable');
    const parts=inviteParts(url); if(!parts)throw new Error('that is not a Concord invite link');
    const decoded=window.PosterCord.openInvite;
    /* openInvite validates the naddr and fragment; use its decoded bootstrap relays by first trying
       the shared CORD set plus the current pool. queryFrom covers relays outside the pool, query covers inside. */
    const parsed=window.PosterCord.inviteDetails(url);
    const filter=[{kinds:[33301],authors:[parsed.linkSigner],'#d':[''],limit:100}];
    const relays=[...new Set([...(parsed&&parsed.bootstrapRelays||[]),...CORD_RELAYS])];
    const [pool,external]=await Promise.all([p.relayQuery?p.relayQuery(filter,8000):[],p.relayQueryFrom(relays,filter,{timeout:10000,max:200})]);
    /* A link signer may issue many bundles with the same replaceable d-tag. The invite fragment opens
       exactly one of them, which is not necessarily the newest. Try every relay result instead of
       handing openInvite the set (whose legacy implementation picked index zero). */
    const candidates=[...(pool||[]),...(external||[])].filter((ev,i,a)=>ev&&a.findIndex(x=>x&&x.id===ev.id)===i).sort((a,b)=>Number(b.created_at)-Number(a.created_at));
    let opened,lastError; for(const ev of candidates){ try{ opened=decoded(url,[ev]); break; }catch(e){ lastError=e; } }
    if(!opened)throw lastError||new Error('invite bundle was not found on its bootstrap relays');
    const bundle=opened.bundle;
    return {url,naddr:parts.naddr,name:bundle.name||'Concord community',description:'',channels:[{name:'general',private:false}],local:false,cord:{bundle,parsed:opened.parsed}};
  }
  async function hydrateDiscoveredIcon(p,item){
    if(!item||item.icon||discoveryIconLoads.has(item.naddr)||!window.PosterCordReader||!p.relayQueryFrom)return;
    discoveryIconLoads.add(item.naddr);
    try{
      const room=await hydrateInvite(p,item.url),bundle=room.cord.bundle,reader=window.PosterCordReader;
      const seed=reader.inspectControl(bundle,[]),relays=[...new Set([...(bundle.relays||[]),...CORD_RELAYS])].slice(0,8);
      const wraps=await cordQuery(p,relays,[{kinds:[1059],authors:seed.controlPubkeys,limit:1000}],{timeout:10000,max:8});
      const info=reader.inspectControl(bundle,wraps||[]);
      item.name=info.name||item.name; item.description=info.description||item.description;
      if(info.icon)item.icon=typeof info.icon==='string'?info.icon:await decryptImagePointer(info.icon);
      if(state.community==null)render();
    }catch(_){} finally{ discoveryIconLoads.delete(item.naddr); }
  }
  function inviteRefUrl(ref){ const s=String(ref||''); if(/^https?:/i.test(s))return s; const [naddr,frag]=s.split('#'); return naddr&&frag?`https://armada.buzz/invite/${naddr}#${frag}`:''; }
  async function membershipEvents(p,pubkey){
    /* Match Armada's wire query exactly. A mixed [13302,33302] request looks harmless, but several
       relays close the WHOLE subscription when one kind is unsupported/blocked. That made a valid
       13302 vault look absent and a fresh browser showed no communities. 13302 is CORD-02's released
       replaceable vault; the addressable migration is queried separately as a compatibility source. */
    const query=async filter=>{
      const [pool,external]=await Promise.all([
        p.relayQuery?Promise.resolve(p.relayQuery([filter],8000)).catch(()=>[]):[],
        p.relayQueryFrom?Promise.resolve(p.relayQueryFrom(CORD_RELAYS,[filter],{timeout:8000,max:4})).catch(()=>[]):[],
      ]);
      return [...new Map([...(pool||[]),...(external||[])].filter(e=>e&&e.id).map(e=>[e.id,e])).values()];
    };
    const released=await query({kinds:[13302],authors:[pubkey],limit:1});
    const migrated=await query({kinds:[33302],authors:[pubkey],'#d':[''],limit:20});
    return [...new Map([...released,...migrated].map(e=>[e.id,e])).values()].sort((a,b)=>Number(b.created_at)-Number(a.created_at));
  }
  async function syncArmadaMemberships(p,viewer){
    if(membershipBusy||!viewer.pubkey||!p.nip44dec)return; membershipBusy=true;
    let recovered=false;
    try{
      const candidates=await membershipEvents(p,viewer.pubkey);
      // Armada has emitted both kinds and may leave several list shards on relays. Decode every
      // valid snapshot: choosing one newest event can hide communities stored in another shard.
      const entries=new Map(),tombs=new Map();
      for(const event of candidates){
        try{
          const list=JSON.parse(await p.nip44dec(viewer.pubkey,event.content)); recovered=true;
          for(const t of Array.isArray(list.tombstones)?list.tombstones:[]){if(t&&t.community_id)tombs.set(t.community_id,Math.max(Number(tombs.get(t.community_id))||0,Number(t.removed_at)||0));}
          for(const e of Array.isArray(list.entries)?list.entries:[]){if(!e||!e.community_id||!e.current)continue;const old=entries.get(e.community_id);if(!old||Number(e.added_at||0)>Number(old.added_at||0))entries.set(e.community_id,e);}
        }catch(_){}
      }
      const live=[...entries.values()].filter(e=>Number(e.added_at||0)>Number(tombs.get(e.community_id)||0)); if(!live.length)return;
      const rooms=saved(); let changed=false;
      for(const e of live){
        const current=e.current||{},url=inviteRefUrl(e.invite_ref),
              i=rooms.findIndex(r=>r.communityId===e.community_id||r.url===url);
        // Armada's vault `current` is a CONTROL SNAPSHOT (owner/root/relays/name), not the complete
        // join bundle. Passing it to inspectControl rejects with "invalid Concord join material";
        // the catch used to `continue`, silently hiding every otherwise valid Armada membership on
        // a fresh device. Existing hydrated rooms need no work. For a missing room, resolve the
        // invite_ref exactly as Armada does and use the bundle carried by that invite.
        if(i>=0&&rooms[i].cord&&!rooms[i].cord.armadaList)continue;
        let hydrated=null,bundle=current;
        try{
          if(!window.PosterCordReader||!window.PosterCordReader.inspectControl)continue;
          window.PosterCordReader.inspectControl(bundle,[]);
        }catch(_){
          if(!url)continue;
          try{hydrated=await hydrateInvite(p,url);bundle=hydrated.cord.bundle;}
          catch(__){continue;}
        }
        const channels=(bundle.channels||[]).map(c=>({name:c.name||'private',private:true,id:c.id}))
          .filter(c=>c.name!=='general');
        const room={...(hydrated||{}),communityId:e.community_id,
          name:current.name||(hydrated&&hydrated.name)||'Concord community',description:'',
          channels:[{name:'general',private:false},...channels],local:false,
          naddr:url?(inviteParts(url)||{}).naddr:'community-'+e.community_id,url,
          cord:{bundle,armadaList:true}};
        if(i<0){rooms.push(room);changed=true;}
        else if(!rooms[i].cord||rooms[i].cord.armadaList){rooms[i]={...rooms[i],...room};changed=true;}
      }
      if(changed){save(rooms);render();}
    }catch(e){ console.warn('Concord membership sync failed',e); }
    finally{
      membershipBusy=false;
      // Native desktop starts with an empty origin and relays may not be connected on first paint.
      // Retry a failed/empty recovery instead of permanently hiding the user's Armada rooms.
      clearTimeout(membershipRetryTimer); membershipRetryTimer=setTimeout(()=>syncArmadaMemberships(p,p.viewer?p.viewer():viewer),recovered?60000:5000);
    }
  }
  async function persistArmadaMembership(p,room){
    const viewer=p.viewer?p.viewer():{};
    if(!viewer.pubkey||!p.nip44enc||!room||!room.communityId||!room.url)return false;
    let list={entries:[],tombstones:[]};
    try{
      const prior=(await membershipEvents(p,viewer.pubkey))[0];
      if(prior&&p.nip44dec)list=JSON.parse(await p.nip44dec(viewer.pubkey,prior.content));
    }catch(_){}
    if(!Array.isArray(list.entries))list.entries=[];
    if(!Array.isArray(list.tombstones))list.tombstones=[];
    const now=Date.now(),current={...(room.cord&&room.cord.bundle||{}),name:room.name,invite_ref:room.url};
    const entry={community_id:room.communityId,seed:current,added_at:now,current,invite_ref:room.url};
    const i=list.entries.findIndex(e=>e&&e.community_id===room.communityId);
    if(i<0)list.entries.push(entry); else list.entries[i]={...list.entries[i],...entry};
    list.tombstones=list.tombstones.filter(t=>t&&t.community_id!==room.communityId);
    const content=await p.nip44enc(viewer.pubkey,JSON.stringify(list));
    const made=await p.publish(13302,content,[]);
    if(made&&made.ev&&p.relayPublishTo)await p.relayPublishTo(CORD_RELAYS,made.ev);
    return true;
  }
  async function leaveArmadaMembership(p,room){
    const viewer=p.viewer?p.viewer():{};
    if(!room||!room.communityId)return true;
    if(!viewer.pubkey||!p.nip44enc||!p.nip44dec)throw new Error('sign in before leaving this community');
    const candidates=await membershipEvents(p,viewer.pubkey),entries=new Map(),tombs=new Map();
    for(const event of candidates){
      try{const list=JSON.parse(await p.nip44dec(viewer.pubkey,event.content));
        for(const e of Array.isArray(list.entries)?list.entries:[]){if(!e||!e.community_id)continue;const old=entries.get(e.community_id);if(!old||Number(e.added_at||0)>=Number(old.added_at||0))entries.set(e.community_id,e);}
        for(const t of Array.isArray(list.tombstones)?list.tombstones:[]){if(t&&t.community_id&&Number(t.removed_at||0)>=Number((tombs.get(t.community_id)||{}).removed_at||0))tombs.set(t.community_id,t);}
      }catch(_){}
    }
    tombs.set(room.communityId,{community_id:room.communityId,removed_at:Date.now()});
    const content=await p.nip44enc(viewer.pubkey,JSON.stringify({entries:[...entries.values()],tombstones:[...tombs.values()]})),made=await p.publish(13302,content,[]);
    if(made&&made.ev&&p.relayPublishTo){const accepted=await p.relayPublishTo(CORD_RELAYS,made.ev);if(!accepted)throw new Error('membership relays rejected the leave update');}
    return true;
  }
  async function hydrateRoomStreams(p,index){
    const rooms=saved(),room=rooms[index],reader=window.PosterCordReader,bundle=room&&room.cord&&room.cord.bundle;
    if(!room||!bundle||!reader||!p.relayQueryFrom)return;
    const loadKey=room.communityId||room.naddr; if(roomLoads.has(loadKey))return roomLoads.get(loadKey);
    const job=(async()=>{
      const seed=reader.inspectControl(bundle,[]), relays=[...new Set([...(bundle.relays||[]),...CORD_RELAYS])].slice(0,8);
      const controlWraps=await cordQuery(p,relays,[{kinds:[1059],authors:seed.controlPubkeys,limit:1000}],{timeout:10000,max:8});
      const info=reader.inspectControl(bundle,controlWraps||[]);
      roomControls.set(loadKey,controlWraps||[]);
      room.name=info.name||room.name; room.description=info.description||room.description;
      room.banned=Array.isArray(info.banned)?info.banned:room.banned||[];
      const icon=info.icon; if(icon)room.icon=typeof icon==='string'?icon:await decryptImagePointer(icon);
      const hydratedChannels=(info.channels||[]).map(c=>({id:c.id,name:c.name,private:!!c.private,streamPubkeys:c.streamPubkeys})).filter(c=>c.name);
      if(!hydratedChannels.length)throw new Error('the control stream returned no readable channels');
      room.channels=hydratedChannels;
      rooms[index]=room; save(rooms);
      for(const channel of room.channels){
        const wraps=await cordQuery(p,relays,[{kinds:[1059],authors:channel.streamPubkeys,limit:1000}],{timeout:10000,max:8});
        const opened=await reader.inspectChat(bundle,controlWraps||[],channel.id,wraps||[]);
        const reactions=new Map(opened.reactions||[]),reactionIds=new Map(opened.reactionIds||[]),msgs=(opened.messages||[]).map(m=>{ const pr=p.profOf?p.profOf(m.pubkey):{},rs={},ri={}; for(const [emoji,people] of reactions.get(m.id)||[])rs[emoji]=people;for(const [emoji,entries] of reactionIds.get(m.id)||[])ri[emoji]=Object.fromEntries(entries); return {id:m.id,pubkey:m.pubkey,by:pr.display_name||pr.name||m.pubkey.slice(0,12)+'…',text:m.text,at:m.at,kind:m.kind,tags:m.tags||[],reactions:rs,reactionIds:ri,remote:true}; });
        const msgById=new Map(msgs.map(m=>[m.id,m])); for(const m of msgs){if(m.kind!==1111)continue;const parentId=((m.tags||[]).find(t=>t[0]==='e')||[])[1],parent=msgById.get(parentId);if(parent)m.reply={id:parent.id,by:parent.by,text:parent.text};}
        const storeId=channelStoreId(room,channel.name),prior=testMessages(storeId),merged=new Map(prior.map(m=>[messageId(m),m]));
        for(const m of msgs)merged.set(m.id,{...(merged.get(m.id)||{}),...m});
        saveTestMessages(storeId,[...merged.values()].sort((a,b)=>Number(a.at)-Number(b.at)));
      }
      room.cord.hydrated=true; rooms[index]=room; save(rooms);
      render();
      scrollChatBottom();
    })().finally(()=>roomLoads.delete(loadKey)); roomLoads.set(loadKey,job); return job;
  }
  async function publishCordMessage(p,room,channelName,text,extraTags=[],kind=9){
    const viewer=p.viewer?p.viewer():{},reader=window.PosterCordReader,bundle=room&&room.cord&&room.cord.bundle;
    if(!viewer.pubkey||!reader||!reader.createChatWrap||!bundle)throw new Error('CORD publishing is unavailable');
    const channel=(room.channels||[]).find(c=>c.name===channelName); if(!channel||!channel.id)throw new Error('channel key is unavailable');
    const loadKey=room.communityId||room.naddr,relays=[...new Set([...(bundle.relays||[]),...CORD_RELAYS])].slice(0,8);
    let controlWraps=roomControls.get(loadKey);
    if(!controlWraps){ const seed=reader.inspectControl(bundle,[]); controlWraps=await cordQuery(p,relays,[{kinds:[1059],authors:seed.controlPubkeys,limit:1000}],{timeout:10000,max:8}); roomControls.set(loadKey,controlWraps||[]); }
    const made=await reader.createChatWrap(bundle,controlWraps||[],channel.id,text,viewer.pubkey,p.signTemplate,extraTags,kind);
    const accepted=await p.relayPublishTo(relays,made.wrap); if(!accepted)throw new Error('community relays rejected the message');
    return made;
  }
  async function refreshActiveChannel(p){
    if(liveBusy||state.community==null||!document.body.classList.contains('concord-view'))return; liveBusy=true;
    try{ const rooms=saved(),room=rooms[state.community],channel=room&&(room.channels||[]).find(c=>c.name===(state.channel||'general')),bundle=room&&room.cord&&room.cord.bundle,reader=window.PosterCordReader;if(!room||!channel||!bundle||!reader)return; const loadKey=room.communityId||room.naddr,controlWraps=roomControls.get(loadKey);if(!controlWraps)return; const relays=[...new Set([...(bundle.relays||[]),...CORD_RELAYS])].slice(0,8),storeId=channelStoreId(room,channel.name),prior=testMessages(storeId),since=Math.max(0,Math.floor((prior.reduce((n,m)=>Math.max(n,Number(m.at)||0),0)-60000)/1000)),wraps=await cordQuery(p,relays,[{kinds:[1059],authors:channel.streamPubkeys,since,limit:500}],{timeout:6000,max:8}),opened=await reader.inspectChat(bundle,controlWraps,channel.id,wraps||[]),byId=new Map(prior.map(m=>[messageId(m),m])); let changed=false; for(const m of opened.messages||[]){if(byId.has(m.id))continue;const pr=p.profOf?p.profOf(m.pubkey):{};byId.set(m.id,{id:m.id,pubkey:m.pubkey,by:pr.display_name||pr.name||m.pubkey.slice(0,12)+'…',text:m.text,at:m.at,kind:m.kind,tags:m.tags||[],reactions:{},remote:true});changed=true;} for(const [target,groups] of opened.reactions||[]){const m=byId.get(target);if(!m)continue;const next={};for(const [emoji,people] of groups)next[emoji]=people;if(JSON.stringify(m.reactions||{})!==JSON.stringify(next)){m.reactions=next;changed=true;}} if(changed){const merged=[...byId.values()].sort((a,b)=>Number(a.at)-Number(b.at));preserveChatScroll(()=>{saveTestMessages(storeId,merged);render();});}
    }catch(e){console.warn('Concord live sync failed',e);}finally{liveBusy=false;}
  }
  async function refreshRoomMetadata(p){
    if(metadataBusy||!document.body.classList.contains('concord-view')||!window.PosterCordReader)return;
    const rooms=saved(),eligible=rooms.map((room,index)=>({room,index})).filter(x=>x.room&&!x.room.local&&x.room.cord&&x.room.cord.bundle);
    if(!eligible.length)return; metadataBusy=true;
    try{
      /* Rotate through every joined community. Metadata belongs to the community rail as much as
       * the active header, so refreshing only state.community leaves all other icons stale until
       * somebody navigates away and back. */
      const selected=eligible[metadataCursor++%eligible.length],room=selected.room,bundle=room.cord.bundle,
        reader=window.PosterCordReader,loadKey=room.communityId||room.naddr,
        seed=reader.inspectControl(bundle,[]),relays=[...new Set([...(bundle.relays||[]),...CORD_RELAYS])].slice(0,8),
        wraps=await cordQuery(p,relays,[{kinds:[1059],authors:seed.controlPubkeys,limit:1000}],{timeout:6000,max:8}),
        info=reader.inspectControl(bundle,wraps||[]);
      roomControls.set(loadKey,wraps||[]);
      let changed=false;
      const assign=(key,value)=>{if(value!==undefined&&JSON.stringify(room[key])!==JSON.stringify(value)){room[key]=value;changed=true;}};
      assign('name',info.name||room.name); assign('description',info.description===undefined?room.description:info.description);
      assign('banned',Array.isArray(info.banned)?info.banned:room.banned||[]);
      if(Object.prototype.hasOwnProperty.call(info,'icon')){
        const ref=typeof info.icon==='string'?info.icon:JSON.stringify(info.icon||null);
        if(roomIconRefs.get(loadKey)!==ref){roomIconRefs.set(loadKey,ref);room.icon=info.icon?(typeof info.icon==='string'?info.icon:await decryptImagePointer(info.icon)):'';changed=true;}
      }
      const channels=(info.channels||[]).map(c=>({id:c.id,name:c.name,private:!!c.private,streamPubkeys:c.streamPubkeys})).filter(c=>c.name);
      if(channels.length)assign('channels',channels);
      if(changed){rooms[selected.index]=room;save(rooms);preserveChatScroll(()=>render());}
    }catch(e){console.warn('Concord metadata sync failed',e);}finally{metadataBusy=false;}
  }
  function startLiveSync(p){ if(liveTimer||!document.body.classList.contains)return; liveTimer=setInterval(()=>{refreshRoomMetadata(p);refreshActiveChannel(p);},4000); }
  async function mintPublicRoom(p,name,icon){
    const viewer=p.viewer?p.viewer():{}; if(!viewer.pubkey||!window.PosterCord)throw new Error('sign in before creating a relay community');
    const relays=[...new Set([...CORD_RELAYS,...(p.relayUrls?p.relayUrls():[])])].slice(0,8);
    const made=await window.PosterCord.createCommunity({name,icon,owner:viewer.pubkey,relays,base:location.origin,signEvent:p.signTemplate});
    for(const ev of made.events){ const accepted=await p.relayPublishTo(relays,ev); if(!accepted)throw new Error('CORD relays rejected an event'); }
    const announcement=await p.publish(1,`${name}\n\n${made.url}`,[['t','concord'],['t','community']]);
    await p.relayPublishTo(DISCOVER_RELAYS,announcement.ev);
    const bundle={community_id:made.communityId,owner:viewer.pubkey,owner_salt:made.secrets.ownerSalt,community_root:made.secrets.root,root_epoch:0,channels:[],relays,name,creator_npub:viewer.pubkey};
    return {name,icon,description:'',channels:[{name:'general',private:false,id:made.generalChannelId}],local:false,naddr:inviteParts(made.url).naddr,url:made.url,cord:{...made,bundle}};
  }
  function render(){
    const p=PC();
    /* Every async Concord path eventually calls render(). The feed belongs to the CURRENT app, not
     * to whichever request finished last. This guard protects Code and every other shared-feed view
     * from relay/discovery/deferred Concord work completing after navigation. */
    if(!p || typeof p.isView!=='function' || !p.isView('concord')) return;
    if(window.PCOS && PCOS.isOn && PCOS.isOn() &&
       (!PCOS.ownsFeedView || !PCOS.ownsFeedView('concord'))) return;
    const feed=p.$('#feed'); if(!feed) return;
    const returning=!feed.querySelector||!feed.querySelector('.cc-app');
    startDiscovery(p);
    startLiveSync(p);
    // Covers the stale-service-worker compatibility entry too, which does not run switchView().
    document.body.classList.add('concord-view','rb-off');
    const rooms=saved();
    const viewer=p.viewer?p.viewer():{};
    syncArmadaMemberships(p,viewer);
    let autoOpen=-1;
    if(state.community==null&&rooms.length&&!discoveryOpen){
      const wanted=Number(localStorage.getItem('pc.concord.active')||0);
      state.community=Number.isInteger(wanted)&&wanted>=0&&wanted<rooms.length?wanted:0;
      state.channel='general';
      autoOpen=state.community;
    }
    const profile=viewer.profile||{};
    const me=profile.display_name||profile.name||(profile.nip05&&p.niceNip05(profile.nip05))||(viewer.npub?viewer.npub.slice(0,12)+'…':'You');
    const current=state.community==null?null:rooms[state.community];
    if(current)markRead(current);
    const visibleChannels=current?orderedChannels(current):[];
    if(current&&visibleChannels.length&&!visibleChannels.some(c=>c.name===(state.channel||'general')))state.channel=visibleChannels[0].name;
    const currentChannel=current?visibleChannels.find(c=>c.name===(state.channel||'general')):null;
    const channelPrivate=!!(currentChannel&&currentChannel.private);
    const messages=current&&(current.local||current.cord)?activeMessages(current):[];
    const joinedRooms=''; // Active communities use the server rail/channel navigator, not home-page cards.
    const ownerPk=String((current&&current.cord&&current.cord.bundle&&(current.cord.bundle.owner||current.cord.bundle.creator_npub))||''),
      isOwner=!!ownerPk&&ownerPk===viewer.pubkey,banned=new Set(current&&current.banned||[]),
      memberPks=current?[...new Set([viewer.pubkey,...messages.map(m=>m.pubkey)].filter(Boolean))].filter(pk=>!banned.has(pk)):[];
    let membersHidden=localStorage.getItem('pc.concord.members.hidden')==='1';
    const memberRows=memberPks.map(pk=>{const pr=p.profOf?p.profOf(pk):{},name=pk===viewer.pubkey?me:(pr.display_name||pr.name||pk.slice(0,12)+'…');return `<div class="cc-member"><img src="${p.enc(pr.picture||p.LOGO||'')}" alt=""><div><b>${p.enc(name)}</b><small>${pk===ownerPk?'Owner':'Member'}</small></div>${isOwner&&pk!==viewer.pubkey?`<button class="btn btn-ghost small cc-ban" data-cc-ban="${p.enc(pk)}">Ban</button>`:''}</div>`;}).join('');
    notifyMentions(p,current,messages,viewer,me);
    feed.innerHTML=`<div class="cc-app${mobileChatOpen||state.community==null?' show-chat':''}${state.community==null?' home-view':''}">
      <aside class="cc-communities"><button class="cc-brand" id="cc-home" title="Your rooms" aria-label="Your rooms"><span aria-hidden="true">🕊</span></button><button class="cc-server cc-discovery-button" id="cc-discovery" title="Discover public communities" aria-label="Discover public communities">◎</button>${rooms.map((r,i)=>`<button class="cc-server${state.community===i?' active':''}${isUnread(r)?' unread':''}" data-cc-server="${i}" title="${p.enc(roomName(r,i))}">${roomIcon(p,r,i)}</button>`).join('')}<button class="cc-server cc-add" id="cc-add" title="Join a community">+</button></aside>
      <aside class="cc-channels"><header><button class="cc-mobile-back" id="cc-back-communities" aria-label="Communities">‹</button><div><b>${state.community==null?'Concord':p.enc(roomName(current,state.community))}</b><small>${current&&current.local?'Local test community':'End-to-end encrypted'}</small></div>${current?'<button class="cc-head-btn" id="cc-edit-icon" title="Set community icon" aria-label="Set community icon"><svg class="ic"><use href="#i-image"></use></svg></button>':''}<button class="cc-head-btn" id="cc-invite" title="Join with invite">+</button></header>
        <div class="cc-channel-list">${state.community==null?'<div class="cc-empty-side">Choose or join a community</div>':`<div class="cc-section">TEXT CHANNELS</div>${visibleChannels.map(c=>`<div class="cc-channel-row${channelStarred(current,c.name)?' starred':''}"><button class="cc-channel${(state.channel||'general')===c.name?' active':''}${testMessages(channelStoreId(current,c.name)).some(m=>(Number(m.at)||0)>seenAt(current))?' unread':''}" data-cc-channel="${p.enc(c.name)}"><span>#</span> ${p.enc(c.name)}</button><button class="cc-channel-star" data-cc-star="${p.enc(c.name)}" aria-pressed="${channelStarred(current,c.name)}" title="${channelStarred(current,c.name)?'Remove from starred channels':'Star channel'}" aria-label="${channelStarred(current,c.name)?'Unstar':'Star'} #${p.enc(c.name)}">${channelStarred(current,c.name)?'★':'☆'}</button></div>`).join('')}`}</div>
        <footer class="cc-identity"><span class="cc-status"></span><div><b>${p.enc(me)}</b><small>You</small></div><button class="cc-head-btn" id="cc-notify" title="Notification settings"><svg class="ic"><use href="#i-bell"></use></svg></button></footer>
      </aside>
      <main class="cc-conversation"><header><button class="cc-mobile-back" id="cc-back-channels" aria-label="${state.community==null?'Back to rooms':'Channels'}">‹</button><span class="cc-hash">#</span><b>${state.community==null?'Communities':state.channel||'general'}</b><span class="cc-visibility ${channelPrivate?'private':'public'}">${channelPrivate?'Private':'Public'}</span><span class="cc-topic">${p.enc((current&&current.description)||(channelPrivate?'Invite-only channel':'Visible to all community members'))}</span><span class="cc-spacer"></span>${current?'<button class="cc-head-btn" id="cc-publish-listing" title="Publish to Armada Discover" aria-label="Publish to Armada Discover"><svg class="ic"><use href="#i-share"></use></svg></button><button class="cc-head-btn" id="cc-copy-link" title="Copy room invite link" aria-label="Copy room invite link"><svg class="ic"><use href="#i-link"></use></svg></button><button class="cc-head-btn" id="cc-call" title="Start voice call"><svg class="ic"><use href="#i-phone"></use></svg></button>':''}<button class="cc-head-btn" id="cc-members" title="Members"><svg class="ic"><use href="#i-users"></use></svg></button></header>
        <div class="cc-messages">${state.community==null?`<div class="cc-discover"><div class="concord-mark">C</div><h2>Find your community</h2><p>Join an Armada-compatible CORD-05 invite or create a public relay community.</p><div class="cc-primary-actions"><button class="btn btn-neon" id="cc-create">Create community</button><button class="btn btn-ghost" id="cc-welcome-join">Join with invite</button></div>${joinedRooms}<section class="cc-public"><div><h3>Public communities</h3><small>Public CORD invites discovered on Armada relays</small></div>${discovered.length?discovered.map((r,i)=>{const pr=p.profOf?p.profOf(r.source.pubkey):{};return `<button data-cc-discover="${i}" class="cc-public-room"><span class="cc-public-icon">${publicRoomIcon(p,r)}</span><span class="cc-public-copy"><b>${p.enc(r.name)}</b><small>${p.enc((r.description||'Public Concord community').slice(0,120))}</small><em>${p.enc(pr.name||pr.display_name||'Nostr community')}</em></span><strong>Join</strong></button>`;}).join(''):(discoveryLoaded?'<div class="cc-public-empty"><b>No public communities found</b><span>Publish or paste a public Armada/CORD invite to list it.</span></div>':'<div class="cc-public-empty"><b>Searching relays…</b><span>Looking for public Armada/CORD invite notes.</span></div>')}</section></div>`:(messages.length?`<div class="cc-message-list">${messages.map(m=>{const mp=p.profOf?p.profOf(m.pubkey):{},mid=messageId(m);return `<article class="cc-message" data-message-id="${p.enc(mid)}"><img class="cc-message-avatar" src="${p.enc(mp.picture||p.LOGO||'')}" alt=""><div class="cc-message-body">${m.reply?`<div class="cc-message-reply"><b>@${p.enc(m.reply.by||'member')}</b> ${p.enc(String(m.reply.text||'').slice(0,100))}</div>`:''}<b>${p.enc(m.by)}</b><time>${new Date(m.at).toLocaleTimeString([],{hour:'2-digit',minute:'2-digit'})}</time>${messageContentHtml(p,m)}<div class="cc-reactions">${reactionSummary(p,m)}</div><div class="cc-message-actions" role="toolbar" aria-label="Message actions"><button class="cc-quick-react" data-cc-quick-react="${p.enc(mid)}" data-cc-emoji="👍" title="React with thumbs up">👍</button><button class="cc-quick-react" data-cc-quick-react="${p.enc(mid)}" data-cc-emoji="❤️" title="React with heart">❤️</button><span class="cc-action-sep"></span><button data-cc-react="${p.enc(mid)}" title="More reactions">☺</button><button data-cc-reply="${p.enc(mid)}" title="Reply">↩</button><button data-cc-delete="${p.enc(mid)}" class="cc-delete-action ${m.pubkey&&m.pubkey===viewer.pubkey?'':'hidden'}" title="Delete message">⌫</button></div></div></article>`;}).join('')}</div>`:`<div class="cc-welcome"><div class="cc-welcome-hash">#</div><h2>Welcome to #${state.channel||'general'}</h2><p>${current&&current.local?'This local test room lets you validate the chat UI before publishing or joining a relay community.':'This is the start of this encrypted channel.'}</p></div>`)}</div>
        <div class="cc-reply${replyTarget?'':' hidden'}" id="cc-reply">${replyTarget?`<span>Replying to <b>${p.enc(replyTarget.by||'member')}</b>: ${p.enc(String(replyTarget.text||'').slice(0,90))}</span><button id="cc-reply-cancel" aria-label="Cancel reply">×</button>`:''}</div><div class="cc-compose"><button class="cc-compose-btn" id="cc-attach" title="Attach file"><svg class="ic"><use href="#i-paperclip"></use></svg></button><input type="file" id="cc-file" multiple hidden><textarea id="cc-input" rows="1" placeholder="Message #${state.channel||'general'}" ${state.community==null?'disabled':''}></textarea><button class="cc-compose-btn" id="cc-emoji" title="Emoji"><svg class="ic"><use href="#i-smile"></use></svg></button><button class="btn btn-neon" id="cc-send" ${state.community==null?'disabled':''}>Send</button></div>
      </main></div><div class="cc-join hidden" id="cc-join"><div class="cc-join-card"><div class="concord-mark">C</div><h2>Join a Concord community</h2><p class="muted">Paste an Armada or other CORD-05 invite. Its # secret stays in this browser.</p><input class="input" id="cc-invite-url" inputmode="url" autocomplete="off" autocapitalize="none" spellcheck="false" placeholder="https://…/invite/naddr1…#…"><div class="cc-join-actions"><button class="btn btn-ghost" id="cc-join-cancel">Cancel</button><button class="btn btn-neon" id="cc-join-go">Preview invite</button></div></div></div><div class="cc-join hidden" id="cc-create-dialog"><div class="cc-join-card"><div class="concord-mark">C</div><h2>Create a public community</h2><p class="muted">Publishes an Armada-compatible CORD community and public #general channel to your relays.</p><label class="cc-label" for="cc-community-name">Community name</label><input class="input" id="cc-community-name" maxlength="64" autocomplete="off" placeholder="My community"><label class="cc-label" for="cc-community-icon">Icon <span class="muted">(emoji or image URL)</span></label><input class="input" id="cc-community-icon" maxlength="2048" autocomplete="off" placeholder="🚀 or https://…/icon.png"><div class="cc-join-actions"><button class="btn btn-ghost" id="cc-create-cancel">Cancel</button><button class="btn btn-neon" id="cc-create-go">Create on relays</button></div></div></div><div class="cc-join hidden" id="cc-icon-dialog"><div class="cc-join-card"><div class="concord-mark">C</div><h2>Community icon</h2><p class="muted">Use an emoji or a direct HTTP(S) image URL. Leave blank to restore the initials.</p><label class="cc-label" for="cc-icon-value">Icon</label><input class="input" id="cc-icon-value" maxlength="2048" autocomplete="off" placeholder="🌌 or https://…/icon.png"><div class="cc-join-actions"><button class="btn btn-ghost" id="cc-icon-cancel">Cancel</button><button class="btn btn-neon" id="cc-icon-save">Save icon</button></div></div></div>`;
    if(current){
      const conversation=p.$('.cc-conversation');
      if(conversation&&conversation.insertAdjacentHTML)conversation.insertAdjacentHTML('afterend',`<aside class="cc-members-pane${membersHidden?' hidden':''}" aria-label="Community members"><header><b>Members</b><span>${memberPks.length}</span></header><div class="cc-members-scroll">${memberRows||'<div class="cc-empty-side">No members have appeared yet.</div>'}</div></aside>`);
      feed.insertAdjacentHTML('beforeend',`<div class="cc-join hidden" id="cc-members-dialog"><div class="cc-join-card"><h2>Members <span class="muted">${memberPks.length}</span></h2><div class="cc-member-list">${memberPks.map((pk,i)=>{ const pr=p.profOf?p.profOf(pk):{}; const name=pk===viewer.pubkey?me:(pr.display_name||pr.name||pk.slice(0,12)+'…'); return `<div class="cc-member"><img src="${p.enc(pr.picture||p.LOGO||'')}" alt=""><div><b>${p.enc(name)}</b><small>${pk===ownerPk?'Owner':'Member'}</small></div>${isOwner&&pk!==viewer.pubkey?`<button class="btn btn-ghost small cc-ban" data-cc-ban="${p.enc(pk)}">Ban</button>`:''}</div>`; }).join('')}</div><div class="cc-join-actions"><button class="btn btn-ghost" id="cc-members-close">Close</button><button class="btn btn-neon" id="cc-members-invite">Invite people</button></div></div></div><div class="cc-join hidden" id="cc-settings-dialog"><div class="cc-join-card"><h2>Community settings</h2><label class="cc-label" for="cc-description-value">Description</label><textarea class="input cc-settings-description" id="cc-description-value" maxlength="1000" rows="3" placeholder="What is this community about?">${p.enc(current.description||'')}</textarea><label class="cc-label" for="cc-settings-icon">Icon</label><input class="input" id="cc-settings-icon" maxlength="2048" value="${p.enc(current.icon||'')}" placeholder="🌌 or https://…/icon.png"><label class="cc-label" for="cc-channel-visibility">#${p.enc(state.channel||'general')} visibility</label><select class="input cc-visibility-select" id="cc-channel-visibility"><option value="public"${channelPrivate?'':' selected'}>Public — all community members</option><option value="private"${channelPrivate?' selected':''}>Private — invited members only</option></select><div class="cc-join-actions"><button class="btn btn-ghost" id="cc-settings-cancel">Cancel</button><button class="btn btn-neon" id="cc-settings-save">Save changes</button></div></div></div>`);
      const settingsActions=p.$('#cc-settings-dialog .cc-join-actions');
      if(settingsActions&&settingsActions.insertAdjacentHTML)settingsActions.insertAdjacentHTML('afterbegin','<button class="btn btn-ghost danger" id="cc-leave-community">Leave community</button>');
    }
    if(p.hydrateLinkCards)p.hydrateLinkCards(feed);
    hydrateEncryptedAttachments(messages);
    hydrateWebxdcCards(current);
    bind(me);
    if(autoOpen>=0)setTimeout(()=>{ const button=document.querySelector(`[data-cc-server="${autoOpen}"]`); if(button)button.click(); },0);
    if(returning&&current)restoreChatScroll();
  }
  function bind(me){
    const p=PC(), $=p.$, $$=p.$$;
    const scroller=document.querySelector('.cc-messages'); if(scroller){ scroller.onscroll=()=>{ if(scroller.dataset.osParking||!scroller.isConnected||!document.body.classList.contains('concord-view'))return; const key=scrollKey(),st=readScroll(key); st.top=scroller.scrollTop;st.height=scroller.scrollHeight;st.pinned=scroller.scrollHeight-scroller.scrollTop-scroller.clientHeight<80;writeScroll(key,st); }; scroller.querySelectorAll('a').forEach(a=>a.addEventListener('pointerdown',()=>{ const key=scrollKey(),st=readScroll(key); st.top=scroller.scrollTop;st.height=scroller.scrollHeight;st.pinned=scroller.scrollHeight-scroller.scrollTop-scroller.clientHeight<80;writeScroll(key,st); },{passive:true})); scroller.addEventListener('click',e=>{const a=e.target&&e.target.closest&&e.target.closest('a[href]');if(!a||!inviteParts(a.href))return;e.preventDefault();e.stopPropagation();openInviteLink(a.href,true);},true); }
    const openJoin=()=>{ $('#cc-join').classList.remove('hidden'); setTimeout(()=>$('#cc-invite-url').focus(),20); };
    const home=$('#cc-home'); if(home)home.onclick=()=>{ const rooms=saved(),wanted=Number(localStorage.getItem('pc.concord.active')||0); discoveryOpen=!rooms.length; state.community=rooms.length&&wanted>=0&&wanted<rooms.length?wanted:(rooms.length?0:null); state.channel=state.community==null?null:'general'; mobileChatOpen=false; render(); };
    const discovery=$('#cc-discovery'); if(discovery)discovery.onclick=()=>{ discoveryOpen=true; state.community=null; state.channel=null; mobileChatOpen=false; render(); };
    ['#cc-add','#cc-invite','#cc-welcome-join'].forEach(s=>{ const b=$(s); if(b)b.onclick=openJoin; });
    const roomInvite=$('#cc-invite');if(roomInvite&&state.community!=null){roomInvite.title='Invite people';roomInvite.setAttribute&&roomInvite.setAttribute('aria-label','Invite people');roomInvite.onclick=()=>{const room=saved()[state.community];if(room&&room.url)p.copyValue(room.url);else $('#cc-join').classList.remove('hidden');};}
    const create=$('#cc-create'); if(create)create.onclick=()=>{ $('#cc-create-dialog').classList.remove('hidden'); setTimeout(()=>$('#cc-community-name').focus(),20); };
    const createCancel=$('#cc-create-cancel'); if(createCancel)createCancel.onclick=()=>$('#cc-create-dialog').classList.add('hidden');
    const createGo=$('#cc-create-go'); if(createGo)createGo.onclick=async()=>{ const name=String($('#cc-community-name').value||'').trim(); if(!name){ p.toast('name your community'); return; } createGo.disabled=true; try{ p.toast('creating encrypted community…'); const room=await mintPublicRoom(p,name,normalizeIcon($('#cc-community-icon').value)); const a=saved(); a.push(room); save(a); state.community=a.length-1; state.channel='general'; render(); await persistArmadaMembership(p,room); p.copyValue(room.url); p.toast('public community created — invite link copied'); }catch(e){ createGo.disabled=false; p.toast('community creation failed: '+(e&&e.message||e)); } };
    const editIcon=$('#cc-edit-icon'); if(editIcon)editIcon.onclick=()=>{ $('#cc-settings-dialog').classList.remove('hidden'); setTimeout(()=>$('#cc-description-value').focus(),20); };
    const iconCancel=$('#cc-icon-cancel'); if(iconCancel)iconCancel.onclick=()=>$('#cc-icon-dialog').classList.add('hidden');
    /* The compact icon dialog used to mutate only this renderer's localStorage and immediately say
       "updated". The next control-stream hydration correctly restored relay metadata, so the icon
       appeared to randomly disappear (and another device never saw it at all). Funnel both icon
       entry points through the authoritative Community settings publisher. */
    const iconSave=$('#cc-icon-save'); if(iconSave)iconSave.onclick=()=>{ const target=$('#cc-settings-icon'),saveButton=$('#cc-settings-save'); if(!target||!saveButton)return; target.value=normalizeIcon($('#cc-icon-value').value); $('#cc-icon-dialog').classList.add('hidden'); return saveButton.click(); };
    const emoji=$('#cc-emoji'), input=$('#cc-input'); if(emoji&&input)emoji.onclick=()=>{ if(p.openEmojiPopover)p.openEmojiPopover(emoji,(value,close)=>{ if(close)close(); if(p.insertAt)p.insertAt(input,value); else input.value+=value; input.focus(); }); };
    let mentionChoices=[],mentionIndex=0;
    const closeMentions=()=>{ mentionChoices=[]; };
    const mentionToken=()=>{ const before=input.value.slice(0,input.selectionStart); return before.match(/(?:^|\s)@([\w.-]*)$/); };
    const drawMentions=()=>{ const match=mentionToken(); if(!match){closeMentions();return;} const room=saved()[state.community],viewer=p.viewer?p.viewer():{},pks=[...new Set([viewer.pubkey,...activeMessages(room).map(m=>m.pubkey)].filter(Boolean))],q=match[1].toLowerCase(); mentionChoices=pks.map(pk=>{const pr=p.profOf?p.profOf(pk):{};return {pk,name:String(pr.display_name||pr.name||(pk===viewer.pubkey?me:pk.slice(0,12)))};}).filter(x=>!q||x.name.toLowerCase().includes(q)).slice(0,8); if(!mentionChoices.length){closeMentions();return;} mentionIndex=Math.min(mentionIndex,mentionChoices.length-1); };
    const acceptMention=(i=mentionIndex)=>{ const match=mentionToken(),choice=mentionChoices[i]; if(!match||!choice)return false; const end=input.selectionStart,start=end-match[1].length-1; input.setRangeText('@'+choice.name.replace(/\s+/g,'_')+' ',start,end,'end'); closeMentions(); input.focus(); return true; };
    if(input&&input.addEventListener)input.addEventListener('input',drawMentions);
    const attach=$('#cc-attach'), file=$('#cc-file');
    const insertBlossomAttachment=({url,type,ext})=>{ if(!url||!input)return; const mime=String(type||'application/octet-stream'),raw=String(url).split(/[?#]/)[0].split('/').pop()||'file',name=raw+(ext&&!raw.includes('.')?'.'+ext:''); let tag=['imeta',`url ${url}`,`m ${mime}`,`name ${name.slice(0,120)}`]; if(mime==='application/x-webxdc')tag.push(`webxdc ${crypto.randomUUID?crypto.randomUUID():Date.now()}`,`summary ${name.replace(/\.xdc$/i,'').slice(0,80)}`); pendingAttachments.set(url,tag); input.value+=(input.value&&!/\s$/.test(input.value)?' ':'')+url; input.dispatchEvent(new Event('input',{bubbles:true})); input.focus(); };
    if(attach&&file)attach.onclick=()=>{ if(!p.blossomPicker||!p.modal){file.click();return;} p.modal(`<h3>Attach to #${p.enc(state.channel||'general')}</h3><p class="muted">Choose a new file from this device or reuse one from a folder in your Blossom drive.</p><div class="cc-attach-choices"><button class="btn btn-ghost" id="cc-attach-device">From device</button><button class="btn btn-neon" id="cc-attach-blossom">🌸 Blossom folders</button></div>`,root=>{ const local=root.querySelector('#cc-attach-device'),blossom=root.querySelector('#cc-attach-blossom'); local.onclick=()=>{p.closeModal();file.click();}; blossom.onclick=()=>{p.closeModal();p.blossomPicker(null,insertBlossomAttachment,{title:'🌸 Attach from Blossom'});}; }); };
    if(file&&input)file.onchange=async()=>{ const files=[...file.files]; for(const f of files){ if(f.size>20*1024*1024){ p.toast(f.name+' is too large (20 MB max)'); continue; } try{ p.toast('uploading '+f.name+'…'); const isXdc=/\.xdc$/i.test(f.name)||f.type==='application/x-webxdc',bytes=isXdc?new Uint8Array(await f.arrayBuffer()):null,url=await p.uploadBlob(f,{keep:true}); if(isXdc){ const sha=bytesHex(await crypto.subtle.digest('SHA-256',bytes)),uuid=crypto.randomUUID?crypto.randomUUID():`${Date.now()}-${Math.random().toString(36).slice(2)}`,name=f.name.replace(/\.xdc$/i,'').slice(0,80); pendingAttachments.set(url,['imeta',`url ${url}`,'m application/x-webxdc',`x ${sha}`,`webxdc ${uuid}`,`summary ${name}`,`name ${f.name.slice(0,120)}`]); } input.value+=(input.value&&!/\s$/.test(input.value)?' ':'')+url; input.dispatchEvent(new Event('input',{bubbles:true})); }catch(e){ p.toast('could not attach '+f.name); } } file.value=''; };
    const members=$('#cc-members'); if(members)members.onclick=()=>{if(!window.matchMedia||window.matchMedia('(max-width:820px)').matches){$('#cc-members-dialog').classList.remove('hidden');return;}const pane=$('.cc-members-pane');if(!pane)return;const hide=localStorage.getItem('pc.concord.members.hidden')!=='1';pane.classList.toggle('hidden',hide);localStorage.setItem('pc.concord.members.hidden',hide?'1':'0');};
    const membersClose=$('#cc-members-close'); if(membersClose)membersClose.onclick=()=>$('#cc-members-dialog').classList.add('hidden');
    $$('[data-cc-ban]').forEach(button=>button.onclick=async()=>{ const rooms=saved(),room=rooms[state.community],viewer=p.viewer?p.viewer():{},bundle=room&&room.cord&&room.cord.bundle,reader=window.PosterCordReader,target=button.dataset.ccBan,loadKey=room&&(room.communityId||room.naddr),wraps=roomControls.get(loadKey); if(!bundle||!reader||!reader.createBanWrap||!wraps)return p.toast('community moderation is not ready'); if(typeof window.confirm==='function'&&!window.confirm('Ban this member from the community?'))return; button.disabled=true; try{ const made=await reader.createBanWrap(bundle,wraps,target,viewer.pubkey,p.signTemplate),relays=[...new Set([...(bundle.relays||[]),...CORD_RELAYS])].slice(0,8),accepted=await p.relayPublishTo(relays,made.wrap); if(!accepted)throw new Error('community relays rejected the ban'); room.banned=made.banned; rooms[state.community]=room; save(rooms); render(); p.toast('member banned'); }catch(e){button.disabled=false;p.toast('member was not banned: '+(e&&e.message||e));} });
    const membersInvite=$('#cc-members-invite'); if(membersInvite)membersInvite.onclick=()=>{ $('#cc-members-dialog').classList.add('hidden'); $('#cc-join').classList.remove('hidden'); };
    const copyLink=$('#cc-copy-link'); if(copyLink)copyLink.onclick=async()=>{ const a=saved(),room=a[state.community]; if(!room)return; if(room.url){ p.copyValue(room.url); return; } copyLink.disabled=true; try{ p.toast('upgrading this room to a public relay community…'); const priorMessages=testMessages(room.naddr), upgraded=await mintPublicRoom(p,room.name,room.icon); upgraded.description=room.description||''; a[state.community]=upgraded; save(a); if(priorMessages.length)saveTestMessages(upgraded.naddr,priorMessages); render(); p.copyValue(upgraded.url); p.toast('room upgraded — invite link copied'); }catch(e){ copyLink.disabled=false; p.toast('could not create invite: '+(e&&e.message||e)); } };
    const publishListing=$('#cc-publish-listing'); if(publishListing)publishListing.onclick=async()=>{ const room=saved()[state.community]; if(!room||!room.url||!room.cord||!Array.isArray(room.cord.events)){ p.toast('This is an old local sandbox; create a relay community to list it'); return; } publishListing.disabled=true; try{ p.toast('publishing to Armada relays…'); for(const ev of room.cord.events)await p.relayPublishTo(CORD_RELAYS,ev); const announcement=await p.publish(1,`${room.name}\n\n${room.url}`,[['t','concord'],['t','community']]); const accepted=await p.relayPublishTo(DISCOVER_RELAYS,announcement.ev); if(!accepted)throw new Error('Armada discovery relays rejected the listing'); p.toast('published to Armada Discover'); }catch(e){ p.toast('could not publish listing: '+(e&&e.message||e)); }finally{ publishListing.disabled=false; } };
    const settingsCancel=$('#cc-settings-cancel'); if(settingsCancel)settingsCancel.onclick=()=>$('#cc-settings-dialog').classList.add('hidden');
    const leave=$('#cc-leave-community');if(leave)leave.onclick=async()=>{const rooms=saved(),index=state.community,room=rooms[index];if(!room)return;if(typeof window.confirm==='function'&&!window.confirm('Leave '+roomName(room,index)+'?'))return;leave.disabled=true;try{await leaveArmadaMembership(p,room);rooms.splice(index,1);save(rooms);state.community=rooms.length?Math.min(index,rooms.length-1):null;state.channel=state.community==null?null:'general';mobileChatOpen=false;render();p.toast('community left');}catch(e){leave.disabled=false;p.toast('could not leave community: '+(e&&e.message||e));}};
    const settingsSave=$('#cc-settings-save'); if(settingsSave)settingsSave.onclick=async()=>{ const a=saved(),room=a[state.community]; if(!room)return; const description=String($('#cc-description-value').value||'').trim().slice(0,1000),icon=normalizeIcon($('#cc-settings-icon').value); settingsSave.disabled=true; try{ if(!room.local){const viewer=p.viewer?p.viewer():{},reader=window.PosterCordReader,bundle=room.cord&&room.cord.bundle,loadKey=room.communityId||room.naddr,relays=[...new Set([...(bundle&&bundle.relays||[]),...CORD_RELAYS])].slice(0,8);if(!reader||!reader.createMetadataWrap||!bundle)throw new Error('community profile is not ready');let wraps=roomControls.get(loadKey);if(!wraps){const seed=reader.inspectControl(bundle,[]);wraps=await cordQuery(p,relays,[{kinds:[1059],authors:seed.controlPubkeys,limit:1000}],{timeout:10000,max:8});}const made=await reader.createMetadataWrap(bundle,wraps||[],{name:room.name,description,icon},viewer.pubkey,p.signTemplate),accepted=await p.relayPublishTo(relays,made.wrap);if(!accepted)throw new Error('community relays rejected the profile update');roomControls.set(loadKey,[...(wraps||[]),made.wrap]);} room.description=description; room.icon=icon; if(!Array.isArray(room.channels))room.channels=[]; let channel=room.channels.find(c=>c.name===(state.channel||'general')); if(!channel){ channel={name:state.channel||'general'}; room.channels.push(channel); } channel.private=$('#cc-channel-visibility').value==='private'; save(a); render(); p.toast('community profile updated'); }catch(e){settingsSave.disabled=false;p.toast('community profile was not updated: '+(e&&e.message||e));} };
    const notify=$('#cc-notify'); if(notify)notify.onclick=async()=>{ const result=p.askOsNotify?await p.askOsNotify():'unsupported'; p.toast(result==='granted'?'community notifications enabled':result==='denied'?'notifications were denied':'notifications are unavailable here'); };
    const call=$('#cc-call'); if(call)call.onclick=()=>{ const room=saved()[state.community], peers=[...new Set(activeMessages(room).map(m=>m.pubkey).filter(pk=>pk&&pk!==(p.viewer&&p.viewer().pubkey)))]; if(!peers.length){ p.toast('No other community members are available to call yet'); return; } p.startGroupCall(peers,false); };
    const cancel=$('#cc-join-cancel'); if(cancel) cancel.onclick=()=>$('#cc-join').classList.add('hidden');
    const go=$('#cc-join-go'); if(go) go.onclick=async()=>{ const raw=String($('#cc-invite-url').value||'').trim(),v=inviteParts(raw); if(!v){ p.toast('that is not a Concord invite link'); return; } go.disabled=true; try{ p.toast('fetching and decrypting community…'); const room=await hydrateInvite(p,raw),a=saved(),i=a.findIndex(x=>x.naddr===v.naddr); if(i<0)a.push(room);else a[i]={...a[i],...room}; save(a); state.community=i<0?a.length-1:i; state.channel='general'; render(); await persistArmadaMembership(p,room); p.toast('community joined'); }catch(e){ go.disabled=false; p.toast('could not join: '+(e&&e.message||e)); } };
    $$('[data-cc-server]').forEach(b=>b.onclick=async()=>{ const i=+b.dataset.ccServer,a=saved(),room=a[i]; let loaded=room; discoveryOpen=false; localStorage.setItem('pc.concord.active',String(i)); state.community=i; state.channel='general'; mobileChatOpen=false; render(); scrollChatBottom(); try{ if(room&&room.url&&(!room.cord||room.cord.armadaList)){ loaded={...room,...await hydrateInvite(p,room.url)}; a[i]=loaded; save(a); render(); } if(loaded&&loaded.cord)await hydrateRoomStreams(p,i); }catch(e){ if(loaded&&loaded.cord)loaded.cord.hydrated=false; save(a); p.toast('could not load community: '+(e&&e.message||e)); } });
    $$('[data-cc-discover]').forEach(b=>b.onclick=async()=>{ const v=discovered[+b.dataset.ccDiscover]; if(!v)return; const a=saved(); let i=a.findIndex(x=>x.naddr===v.naddr); if(i<0){a.push(v);i=a.length-1;} save(a); state.community=i; state.channel='general'; render(); p.toast('fetching and decrypting community…'); try{ a[i]={...a[i],...await hydrateInvite(p,v.url)}; save(a); await persistArmadaMembership(p,a[i]); await hydrateRoomStreams(p,i); p.toast('community joined'); }catch(e){ p.toast('could not load community: '+(e&&e.message||e)); } });
    $$('[data-cc-channel]').forEach(b=>b.onclick=async()=>{ state.channel=b.dataset.ccChannel; mobileChatOpen=true; render(); scrollChatBottom(); const rooms=saved(),room=rooms[state.community]; if(room&&room.cord&&!room.cord.hydrated){ try{ await hydrateRoomStreams(p,state.community); }catch(e){ p.toast('could not load room history: '+(e&&e.message||e)); } } });
    $$('[data-cc-star]').forEach(b=>b.onclick=e=>{ if(e&&e.stopPropagation)e.stopPropagation(); const room=saved()[state.community],name=b.dataset.ccStar; if(!room||!name)return; setChannelStarred(room,name,!channelStarred(room,name)); render(); });
    const bc=$('#cc-back-communities'); if(bc)bc.onclick=()=>{ discoveryOpen=true; state.community=null; state.channel=null; render(); };
    const bh=$('#cc-back-channels'); if(bh)bh.onclick=()=>{ if(state.community==null){ const rooms=saved(),wanted=Number(localStorage.getItem('pc.concord.active')||0); discoveryOpen=false; state.community=rooms.length&&wanted>=0&&wanted<rooms.length?wanted:(rooms.length?0:null); state.channel=state.community==null?null:'general'; } mobileChatOpen=false; render(); };
    const send=$('#cc-send'); if(send&&input){
      send.onclick=async()=>{ const text=String(input.value||'').trim(); const a=saved(), room=a[state.community],storeId=channelStoreId(room,state.channel); if(!text)return; if(!room||(!room.local&&!room.cord)){ p.toast('relay messaging becomes available after the invite is decrypted'); return; } const used=[...pendingAttachments].filter(([url])=>text.includes(url)),attachmentTags=used.map(([,tag])=>tag),target=replyTarget,replyTags=[],viewer=p.viewer?p.viewer():{},m=testMessages(storeId); if(target){const inherited=(target.tags||[]).filter(t=>['K','E'].includes(t[0]));if(inherited.length)replyTags.push(...inherited);else replyTags.push(['K',String(target.kind||9)],['E',messageId(target),'',target.pubkey||'']);replyTags.push(['k',String(target.kind||9)],['e',messageId(target),'',target.pubkey||'']);for(const pk of threadParticipants(m,target,viewer.pubkey)){replyTags.push(['P',pk],['p',pk]);}} const extraTags=[...attachmentTags,...replyTags],wireKind=target?1111:9,at=Date.now(),tempId='pending-'+(crypto.randomUUID?crypto.randomUUID():`${at}-${Math.random().toString(36).slice(2)}`),optimistic={id:tempId,by:me,pubkey:viewer.pubkey||'',text,at,kind:wireKind,tags:extraTags,reply:target?{id:messageId(target),by:target.by,text:target.text}:null,reactions:{},pending:!room.local,remote:false}; m.push(optimistic); saveTestMessages(storeId,m); for(const [url] of used)pendingAttachments.delete(url); input.value=''; replyTarget=null; render(); scrollChatBottom(); if(room.local)return; try{ const made=await publishCordMessage(p,room,state.channel,text,extraTags,wireKind),latest=testMessages(storeId),sent=latest.find(x=>x.id===tempId); if(sent){sent.id=made.rumorId;sent.at=made.ms;sent.pending=false;sent.remote=true;saveTestMessages(storeId,latest);preserveChatScroll(()=>render());} }catch(e){ const latest=testMessages(storeId),failed=latest.find(x=>x.id===tempId);if(failed){failed.pending=false;failed.failed=true;saveTestMessages(storeId,latest);preserveChatScroll(()=>render());} p.toast('message was not sent: '+(e&&e.message||e)); } };
      input.onkeydown=e=>{ const enter=e.key==='Enter'||e.code==='Enter'; if(mentionChoices.length){ if(e.key==='ArrowDown'||e.key==='ArrowUp'){e.preventDefault();mentionIndex=(mentionIndex+(e.key==='ArrowDown'?1:-1)+mentionChoices.length)%mentionChoices.length;drawMentions();return;} if(e.key==='Tab'||(enter&&!e.ctrlKey&&!e.metaKey)){e.preventDefault();acceptMention();return;} if(e.key==='Escape'){e.preventDefault();closeMentions();return;} } if(enter&&(e.ctrlKey||e.metaKey)){ e.preventDefault(); return send.onclick(); } };
    }
    const replyCancel=$('#cc-reply-cancel'); if(replyCancel)replyCancel.onclick=()=>{ replyTarget=null; render(); };
    $$('[data-cc-delete]').forEach(button=>button.onclick=async()=>{ const room=saved()[state.community],storeId=channelStoreId(room,state.channel),messages=testMessages(storeId),id=button.dataset.ccDelete,found=messages.find(m=>messageId(m)===id),viewer=p.viewer?p.viewer():{}; if(!found||!viewer.pubkey||found.pubkey!==viewer.pubkey)return; if(typeof window.confirm==='function'&&!window.confirm('Delete this message?'))return; button.disabled=true; try{ if(!room.local)await publishCordMessage(p,room,state.channel,'',[['e',id],['k',String(found.kind||9)]],5); saveTestMessages(storeId,messages.filter(m=>messageId(m)!==id)); preserveChatScroll(()=>render()); }catch(e){ button.disabled=false; p.toast('message was not deleted: '+(e&&e.message||e)); } });
    $$('[data-cc-reply]').forEach(b=>b.onclick=()=>{ const room=saved()[state.community],m=activeMessages(room),found=m.find(x=>messageId(x)===b.dataset.ccReply); if(!found)return; replyTarget=found; render(); const box=$('#cc-input'); if(box)box.focus(); });
    const toggleReaction=async(id,emoji)=>{ const room=saved()[state.community],storeId=channelStoreId(room,state.channel),m=testMessages(storeId),found=m.find(x=>messageId(x)===id),viewer=p.viewer?p.viewer():{},who=viewer.pubkey||'local-user'; if(!found)return; if(!found.reactions||typeof found.reactions!=='object')found.reactions={}; const people=Array.isArray(found.reactions[emoji])?found.reactions[emoji]:[],i=people.indexOf(who); try{if(!room.local){if(i<0)await publishCordMessage(p,room,state.channel,emoji,[['e',id],['p',found.pubkey||''],['k',String(found.kind||9)]],7);else{const rid=found.reactionIds&&found.reactionIds[emoji]&&found.reactionIds[emoji][who];if(!rid)throw new Error('refresh the room before removing this reaction');await publishCordMessage(p,room,state.channel,'',[['e',rid],['k','7']],5);}}}catch(e){p.toast('reaction was not sent: '+(e&&e.message||e));return;} if(i<0)people.push(who);else people.splice(i,1); if(people.length)found.reactions[emoji]=people;else delete found.reactions[emoji]; saveTestMessages(storeId,m); reactionTarget=null; preserveChatScroll(()=>render()); };
    $$('[data-cc-quick-react]').forEach(b=>b.onclick=()=>toggleReaction(b.dataset.ccQuickReact,b.dataset.ccEmoji));
    $$('[data-cc-react-toggle]').forEach(b=>b.onclick=()=>toggleReaction(b.dataset.ccReactToggle,b.dataset.ccEmoji));
    $$('[data-cc-react]').forEach(b=>b.onclick=()=>{ reactionTarget=b.dataset.ccReact; const choices=['👍','❤️','😂','😮','😢','😡','🎉','💯']; const old=document.querySelector('.cc-reaction-picker'); if(old)old.remove(); const pop=document.createElement('div'); pop.className='cc-reaction-picker'; pop.innerHTML=choices.map(x=>`<button data-emoji="${x}">${x}</button>`).join(''); b.closest('.cc-message-body').appendChild(pop); pop.querySelectorAll('button').forEach(x=>x.onclick=e=>{ e.stopPropagation(); toggleReaction(reactionTarget,x.dataset.emoji); }); });
  }
  window.PCConcord={render,openInvite:openInviteLink,inviteParts,normalizeIcon,notifyMentions,discoverInvites,threadParticipants,encryptedAttachments,messageContentHtml};
})();
