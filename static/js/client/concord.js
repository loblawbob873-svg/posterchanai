/* Concord communities — a dedicated Discord/Matrix-shaped workspace (separate from public Chat). */
(function(){
  'use strict';
  // Do not depend on the monolithic shell CSS being current: an older service worker may serve its
  // cached client.css for one navigation. Concord owns a versioned sheet and loads it itself too.
  if(!document.querySelector('link[data-concord-css]')){
    const l=document.createElement('link'); l.rel='stylesheet'; l.dataset.concordCss='1';
    l.href='/static/css/concord.css?v=17'; (document.head||document.documentElement).appendChild(l);
  }
  const PC=()=>window.__PC;
  /* Automatic reads must contain only known Concord endpoints. relay.ditto.pub currently refuses
   * WebSocket handshakes, and copying the user's general pool into external discovery also created
   * redundant Damus sockets. Explicit invite/bootstrap and room bundle relays remain authoritative
   * below, even when they name one of those hosts. */
  const CORD_RELAYS=['wss://jskitty.com/nostr','wss://asia.vectorapp.io/nostr','wss://nostr.computingcache.com','wss://relay.dreamith.to'];
  const DISCOVER_RELAYS=['wss://relay.dreamith.to'];
  const LEGACY_RECOVERY_RELAYS=['wss://relay.ditto.pub','wss://relay.damus.io'];
  /* A community's invite is authoritative about where its encrypted stream lives. Appending the
   * global compatibility set to every read/write caused a room open to spray sockets at unrelated
   * relays and could report a successful send on a default relay Armada never reads. Defaults are
   * bootstrap fallback only for old bundles which carry no usable relay. */
  function roomRelays(bundle){
    const own=[...new Set((bundle&&bundle.relays||[]).map(normalizeRelay).filter(Boolean))];
    return (own.length?own:CORD_RELAYS).slice(0,8);
  }
  async function cordQuery(p,relays,filters,{timeout=8000,max=8,signal=null,purpose='concord room',minInterval=0,allowBlocked=true,failureCooldown=1800000}={}){
    /* queryFrom intentionally skips relays already owned by the shared pool. Always ask both paths:
       otherwise opening a room can silently omit the newest wraps from whichever relay is connected. */
    const jobs=[];
    if(p.relayQuery)jobs.push(Promise.resolve().then(()=>p.relayQuery(filters,timeout)));
    if(p.relayQueryFrom)jobs.push(Promise.resolve().then(()=>p.relayQueryFrom(relays,filters,{timeout,max,signal,purpose,minInterval,allowBlocked,failureCooldown})));
    const settled=await Promise.allSettled(jobs),ok=settled.filter(result=>result.status==='fulfilled');
    if(jobs.length&&!ok.length)throw settled[0].reason;
    const batches=ok.map(result=>result.value||[]),byId=new Map();
    for(const ev of batches.flat())if(ev&&ev.id)byId.set(ev.id,ev);
    return [...byId.values()];
  }
  /* Tuple encoding prevents room/channel boundary collisions (`a:b`+`c` versus `a`+`b:c`) and
   * lets cache cleanup compare the room identity exactly instead of prefix-deleting another room. */
  function envelopeCacheKey(loadKey,stream){return JSON.stringify([String(loadKey||''),String(stream||'control')]);}
  function mergeEnvelopes(...groups){const byId=new Map();for(const ev of groups.flat())if(ev&&ev.id)byId.set(ev.id,ev);return [...byId.values()];}
  async function cachedEnvelopes(key){try{return window.PCConcordCache?await window.PCConcordCache.get(key):[];}catch(e){console.warn('Concord cache read failed',e);return [];}}
  async function cachedEnvelopePage(key,limit=300){try{if(!window.PCConcordCache)return[];if(window.PCConcordCache.page){const got=await window.PCConcordCache.page(key,{limit});return got&&Array.isArray(got.events)?got.events:[];}return await window.PCConcordCache.get(key);}catch(e){console.warn('Concord cache read failed',e);return[];}}
  async function cacheEnvelopes(key,events){try{if(window.PCConcordCache&&events&&events.length)await window.PCConcordCache.put(key,events);}catch(e){console.warn('Concord cache write failed',e);}}
  async function queryEnvelopeHistory(p,relays,authors,cached=[],queryOptions={}){
    let all=mergeEnvelopes(cached),until=null;
    for(let page=0;page<6&&(page===0||all.length<5000);page++){
      const filter={kinds:[1059],authors,limit:1000};if(until!=null)filter.until=until;
      /* minInterval gates logical background REFRESHES, not pagination inside one refresh. Only
         page zero consults/arms it; later pages must still reach older history. */
      const batch=await cordQuery(p,relays,[filter],{timeout:10000,max:8,...queryOptions,minInterval:page===0?Number(queryOptions.minInterval||0):0});
      const merged=mergeEnvelopes(all,batch),oldest=batch.reduce((n,event)=>Math.min(n,Number(event.created_at)||n),Infinity);
      all=merged;if(batch.length<1000||!Number.isFinite(oldest)||oldest<=0||until===oldest-1)break;until=oldest-1;
    }
    return all.sort((a,b)=>(Number(a.created_at)||0)-(Number(b.created_at)||0)).slice(-5000);
  }
  const state={ community:null, channel:null };
  let replyTarget=null, reactionTarget=null, mobileChatOpen=false, mobileDrawerOpen=false, discoveryOpen=false;
  let discovered=[], discoveryStarted=false, discoverySubscription=null, discoveryAbortController=null, discoveryLoaded=false, membershipBusy=false, membershipRetryTimer=null;
  let discoveryPaintPending=false;
  let membershipViewer='';const membershipDocs=new Map();
  let nip29Busy=false,nip29RetryTimer=null,webxdcHydrationEpoch=0;
  let roomReadIdentity='',roomReadAbortController=null;
  function ownRoomReads(identity){
    identity=String(identity||'');if(identity===roomReadIdentity&&roomReadAbortController)return roomReadAbortController.signal;
    if(roomReadAbortController)roomReadAbortController.abort();roomReadIdentity=identity;
    roomReadAbortController=identity&&typeof AbortController==='function'?new AbortController():null;
    return roomReadAbortController&&roomReadAbortController.signal;
  }
  const discoveryIconLoads=new Set();
  const recoveredOwnedInvites=new Set();
  const roomLoads=new Map();
  /* `room.cord.hydrated` is durable membership metadata, not renderer state. Treating it as a
   * process-local cache flag made a fresh browser/Desktop process skip IndexedDB restoration and
   * show an empty room until the live poll happened to run. */
  const hydratedRoomViews=new Set();
  const roomLoadNotices=new Map();
  const roomControls=new Map();
  /* Blob URLs die with their renderer. Keep encrypted icon pointer identity in memory so a saved
   * room never suppresses re-decryption after the next browser/native-shell launch. */
  /* Durable storage keeps the encrypted pointer; only this renderer keeps its decrypted blob URL.
   * A blob: URL written to localStorage is guaranteed dead after reload. */
  const roomIconRefs=new Map(),storedIconLoads=new Set(),failedIconRefs=new Map();
  const pendingAttachments=new Map();
  /* A relay poll, profile/icon hydration or decrypted attachment can repaint the whole Concord
   * workspace while somebody is typing. The textarea is part of that workspace, so the browser
   * cannot preserve it for us. Keep a per-room/channel draft outside the DOM and restore the exact
   * selection after every repaint. This deliberately stays in renderer memory: drafts are private
   * plaintext and must not be written to localStorage. */
  const composerDrafts=new Map(),sendingDrafts=new Set();
  /* Relay messages, profiles, icons and decrypted attachments may all finish while the on-screen
   * keyboard owns a control inside Concord. Replacing #feed at that moment destroys the real DOM
   * textarea; restoring its value/focus later still closes and reopens Android's keyboard. Defer
   * background paints until focus leaves the workspace. Data is already committed to the in-memory
   * stores, and one eventual paint coalesces every pending update. User actions still call render()
   * directly, so navigation and Send are immediate. */
  let backgroundRenderPending=false,backgroundFocusHost=null;
  let activeMentionState={choices:[],index:0,recipients:new Map()};
  const attachmentCache=new Map(),attachmentLoads=new Map();
  const scrollStates=new Map();
  let liveTimer=null,liveBusy=false,metadataBusy=false,metadataCursor=0;
  let resumeRequested=false;
  let actionDismissOff=null;
  function saved(){ try{ const v=JSON.parse(localStorage.getItem('pc.concord.invites')||'[]'); return Array.isArray(v)?v:[]; }catch(_){ return []; } }
  function save(v){ try{ localStorage.setItem('pc.concord.invites',JSON.stringify(v.slice(0,50),(key,value)=>key==='icon'&&/^blob:/i.test(String(value||''))?'':value)); }catch(_){} }
  function scrollKey(){ const room=state.community==null?null:saved()[state.community]; return `${room&&(room.communityId||room.naddr||room.url)||'home'}:${state.channel||'general'}`; }
  function composerKey(room,channel){return `${room&&(room.communityId||room.naddr||room.url)||'home'}:${channel||'general'}`;}
  function captureComposer(){
    const input=(document.querySelector&&document.querySelector('#cc-input'))||(PC()&&PC().$&&PC().$('#cc-input'));
    if(!input)return;
    const room=state.community==null?null:saved()[state.community],key=input.dataset&&input.dataset.ccDraftKey||composerKey(room,state.channel),start=Number.isFinite(input.selectionStart)?input.selectionStart:String(input.value||'').length,
      end=Number.isFinite(input.selectionEnd)?input.selectionEnd:start;
    const focused=document.activeElement===input;
    composerDrafts.set(key,{value:String(input.value||''),start,end,direction:input.selectionDirection||'none',
      focused,replyTarget,mentionChoices:[...(activeMentionState.choices||[])],
      mentionIndex:Number(activeMentionState.index)||0,mentionRecipients:new Map(activeMentionState.recipients||[])});
  }
  function clearComposer(key){const input=(document.querySelector&&document.querySelector('#cc-input'))||(PC()&&PC().$&&PC().$('#cc-input'));if(input){const room=state.community==null?null:saved()[state.community],liveKey=input.dataset&&input.dataset.ccDraftKey||composerKey(room,state.channel);if(liveKey===key){input.value='';try{input.setSelectionRange(0,0);}catch(_){}}}composerDrafts.delete(key);activeMentionState={choices:[],index:0,recipients:new Map()};replyTarget=null;}
  function cloneComposerDraft(draft){return draft?{...draft,mentionChoices:[...(draft.mentionChoices||[])],mentionRecipients:new Map(draft.mentionRecipients||[])}:null;}
  /* Sending is optimistic, so the composer must look sent immediately instead of displaying a
   * duplicate copy until signing/relay I/O finishes. Keep the submitted value off-DOM solely as a
   * rollback snapshot. A later render can therefore never resurrect it from composerDrafts. */
  function beginComposerSend(key){captureComposer();const submitted=cloneComposerDraft(composerDrafts.get(key));clearComposer(key);return submitted;}
  function restoreFailedComposer(key,submitted){
    if(!submitted)return false;
    captureComposer();
    const input=(document.querySelector&&document.querySelector('#cc-input'))||(PC()&&PC().$&&PC().$('#cc-input')),
      room=state.community==null?null:saved()[state.community],liveKey=input&&(input.dataset&&input.dataset.ccDraftKey||composerKey(room,state.channel)),
      current=composerDrafts.get(key),currentValue=liveKey===key&&input?String(input.value||''):String(current&&current.value||'');
    /* Never overwrite text written while the first message was in flight. */
    if(currentValue!=='')return false;
    composerDrafts.set(key,cloneComposerDraft(submitted));
    if(liveKey===key&&input){input.value=String(submitted.value||'');try{input.setSelectionRange(Number(submitted.start)||0,Number(submitted.end)||Number(submitted.start)||0,submitted.direction||'none');}catch(_){}}
    activeMentionState={choices:[...(submitted.mentionChoices||[])],index:Number(submitted.mentionIndex)||0,recipients:new Map(submitted.mentionRecipients||[])};
    replyTarget=submitted.replyTarget||null;
    return true;
  }
  function backgroundRender(){
    const p=PC(),feed=p&&p.$&&p.$('#feed'),active=document.activeElement,
      input=document.querySelector&&document.querySelector('#cc-input'),
      concordHost=input&&(input.closest&&input.closest('.cc-shell,.cc-layout,[data-view="concord"]')),
      inside=!!(active&&active!==document.body&&active!==document.documentElement&&active.isConnected!==false&&
        (active===input||(concordHost&&concordHost.contains&&concordHost.contains(active))||
          (feed&&feed.contains&&feed.contains(active))));
    if(inside){
      backgroundRenderPending=true;
      /* The native mobile handoff can briefly detach or replace the classic #feed host while the
       * real textarea remains connected in Concord's managed window. Tying protection to #feed made
       * that exact state repaint the composer and close Android's keyboard. Follow the focused
       * control itself; focusout is the lifecycle signal we actually need and works in every host. */
      if(backgroundFocusHost!==active&&active.addEventListener){
        backgroundFocusHost=active;
        active.addEventListener('focusout',()=>{backgroundFocusHost=null;setTimeout(()=>{if(backgroundRenderPending)backgroundRender();},0);},{once:true,capture:true});
      }
      return false;
    }
    backgroundRenderPending=false;backgroundFocusHost=null;render();return true;
  }
  function handoffState(){ const room=state.community==null?null:saved()[state.community],key=scrollKey(),scroll=readScroll(key); return {room:room&&(room.communityId||room.naddr||room.url)||'',channel:state.channel||'general',mobileChatOpen:!!mobileChatOpen,mobileDrawerOpen:!!mobileDrawerOpen,scroll:{top:Number(scroll.top)||0,height:Number(scroll.height)||0,pinned:scroll.pinned!==false}}; }
  function acceptHandoff(value){ const v=value&&typeof value==='object'?value:{},rooms=saved(),i=rooms.findIndex(room=>(room.communityId||room.naddr||room.url)===String(v.room||'')); state.community=i>=0?i:(rooms.length?Math.max(0,Math.min(Number(localStorage.getItem('pc.concord.active'))||0,rooms.length-1)):null);state.channel=String(v.channel||'general').slice(0,80);mobileChatOpen=!!v.mobileChatOpen;mobileDrawerOpen=!!v.mobileDrawerOpen;if(state.community!=null&&v.scroll){const key=scrollKey(),st={top:Math.max(0,Number(v.scroll.top)||0),height:Math.max(0,Number(v.scroll.height)||0),pinned:v.scroll.pinned!==false};writeScroll(key,st);} }
  function readScroll(key){ if(scrollStates.has(key))return scrollStates.get(key); try{ const v=JSON.parse(sessionStorage.getItem('pc.concord.scroll.'+key)||'null'); if(v&&typeof v==='object')return v; }catch(_){} return {pinned:true}; }
  function writeScroll(key,st){ scrollStates.set(key,st); try{ sessionStorage.setItem('pc.concord.scroll.'+key,JSON.stringify({top:Number(st.top)||0,height:Number(st.height)||0,pinned:st.pinned!==false})); }catch(_){} }
  function setProgrammaticScroll(box,top,done){ if(!box)return;box.dataset.ccScrollRestore='1';box.scrollTop=top;const later=window.requestAnimationFrame||((fn)=>setTimeout(fn,0));later(()=>{if(box.isConnected)delete box.dataset.ccScrollRestore;if(done)done();}); }
  function scrollChatBottom(){ const key=scrollKey(),st=readScroll(key); st.pinned=true; writeScroll(key,st); const later=window.requestAnimationFrame||((fn)=>setTimeout(fn,0)); later(()=>{ const box=document.querySelector('.cc-messages'); if(box)setProgrammaticScroll(box,box.scrollHeight,()=>{st.top=box.scrollTop;st.height=box.scrollHeight;writeScroll(key,st);}); }); }
  /* Entering a room is different from preserving a room somebody is already reading. History,
   * decrypted attachments and link previews all grow the scroller asynchronously, so one rAF can
   * reach what was the bottom and still leave the person hundreds of pixels above the final bottom. */
  function enterChatBottom(){ const key=scrollKey(),token=Date.now()+Math.random();enterChatBottom.token=token;const st=readScroll(key);st.pinned=true;st.top=Number.MAX_SAFE_INTEGER;writeScroll(key,st);for(const delay of [0,60,180,450,900,1600])setTimeout(()=>{if(enterChatBottom.token!==token||scrollKey()!==key||st.pinned===false)return;const box=document.querySelector('.cc-messages');if(!box)return;setProgrammaticScroll(box,box.scrollHeight,()=>{st.top=box.scrollTop;st.height=box.scrollHeight;writeScroll(key,st);});},delay); }
  function repaintScrollTop(pinned,top,scrollHeight){ return pinned!==false?scrollHeight:Math.max(0,Number(top)||0); }
  function preserveChatScroll(fn){
    const key=scrollKey(),old=document.querySelector('.cc-messages'),st=readScroll(key),
      top=old?old.scrollTop:Number(st.top)||0;
    /* Pixel offsets only preserve appended history. Delayed backfill inserts older rows above the
     * viewport, where restoring the same number silently changes which message the reader sees.
     * Keep the first visible message and its position inside the viewport as the stronger anchor. */
    const oldRows=old&&old.querySelectorAll?[...old.querySelectorAll('.cc-message[data-message-id]')]:[],
      anchor=st.pinned===false?oldRows.find(row=>(Number(row.offsetTop)||0)+(Number(row.offsetHeight)||0)>top):null,
      anchorId=anchor&&anchor.dataset&&anchor.dataset.messageId,
      anchorGap=anchor?(Number(anchor.offsetTop)||0)-top:0;
    fn();
    const later=window.requestAnimationFrame||((f)=>setTimeout(f,0));
    later(()=>{ const box=document.querySelector('.cc-messages'); if(box){
      let next=repaintScrollTop(st.pinned,top,box.scrollHeight);
      if(st.pinned===false&&anchorId&&box.querySelectorAll){
        const row=[...box.querySelectorAll('.cc-message[data-message-id]')]
          .find(el=>el.dataset&&el.dataset.messageId===anchorId);
        if(row)next=Math.max(0,(Number(row.offsetTop)||0)-anchorGap);
      }
      box.scrollTop=next;st.top=box.scrollTop;st.height=box.scrollHeight;writeScroll(key,st);
    } });
  }
  function restoreChatScroll(){ const key=scrollKey(),st=readScroll(key),later=window.requestAnimationFrame||((f)=>setTimeout(f,0)); later(()=>{ const box=document.querySelector('.cc-messages'); if(box)setProgrammaticScroll(box,st.pinned!==false?box.scrollHeight:Number(st.top)||0,()=>{st.top=box.scrollTop;st.height=box.scrollHeight;writeScroll(key,st);}); }); }
  function viewportAnchor(scroller){
    if(!scroller||!scroller.querySelectorAll)return null;
    const top=Number(scroller.scrollTop)||0,row=[...scroller.querySelectorAll('.cc-message[data-message-id]')]
      .find(el=>(Number(el.offsetTop)||0)+(Number(el.offsetHeight)||0)>top);
    return row&&row.dataset&&row.dataset.messageId?{id:row.dataset.messageId,gap:(Number(row.offsetTop)||0)-top}:null;
  }
  function watchPinnedRoomGrowth(scroller){
    if(!scroller||typeof ResizeObserver==='undefined')return;
    const key=scrollKey(),content=scroller.querySelector('.cc-message-list')||scroller;
    let anchor=viewportAnchor(scroller);
    const remember=()=>{anchor=viewportAnchor(scroller);};
    if(scroller.addEventListener)scroller.addEventListener('scroll',remember,{passive:true});
    const observer=new ResizeObserver(()=>{const st=readScroll(key);if(!scroller.isConnected||scrollKey()!==key){if(!scroller.isConnected)observer.disconnect();return;}if(st.pinned!==false){setProgrammaticScroll(scroller,scroller.scrollHeight,()=>{st.top=scroller.scrollTop;st.height=scroller.scrollHeight;writeScroll(key,st);remember();});return;}/* Decrypted images and link cards can gain height above an unpinned reader long after render. A fixed pixel offset would replace the message in view, so restore the last visible row and its viewport gap. Growth below it naturally produces the same offset. */const row=anchor&&[...scroller.querySelectorAll('.cc-message[data-message-id]')].find(el=>el.dataset&&el.dataset.messageId===anchor.id),top=row?Math.max(0,(Number(row.offsetTop)||0)-anchor.gap):scroller.scrollTop;setProgrammaticScroll(scroller,top,()=>{st.top=scroller.scrollTop;st.height=scroller.scrollHeight;writeScroll(key,st);remember();});});
    observer.observe(content);
  }
  function removeMessageRow(id){ const box=document.querySelector('.cc-messages'),row=[...document.querySelectorAll('.cc-message[data-message-id]')].find(el=>el.dataset.messageId===id); if(!box||!row)return false; const key=scrollKey(),st=readScroll(key),top=box.scrollTop,height=box.scrollHeight,above=(Number(row.offsetTop)||0)+(Number(row.offsetHeight)||0)<=top; row.remove(); const later=window.requestAnimationFrame||((f)=>setTimeout(f,0)); later(()=>{ if(!box.isConnected)return; const lost=Math.max(0,height-box.scrollHeight); box.scrollTop=st.pinned!==false?box.scrollHeight:(above?Math.max(0,top-lost):top);st.top=box.scrollTop;st.height=box.scrollHeight;writeScroll(key,st); }); return true; }
  function roomName(r,i){ return (r&&r.name)||`Encrypted community ${i+1}`; }
  function roomIdentity(room){ return String(room&&(room.communityId||room.naddr||room.url)||''); }
  function notificationRoute(room,channel,message){
    return 'concord:'+encodeURIComponent(roomIdentity(room))+':'+
      encodeURIComponent(String(channel||'general'))+':'+encodeURIComponent(messageId(message));
  }
  function removeCommunityByIdentity(rooms,identity){
    const next=Array.isArray(rooms)?rooms.slice():[],index=next.findIndex(room=>roomIdentity(room)===String(identity||''));
    if(index>=0)next.splice(index,1);
    return {rooms:next,index};
  }
  function memberTapAction(narrow,longPressed){ return longPressed?'consume':(narrow?'profile':'menu'); }
  function memberViewportIsNarrow(){ return !!(window.matchMedia&&window.matchMedia('(max-width:820px)').matches); }
  function reactionPickerPosition(anchor,picker,viewport){
    const gap=6,margin=8,view=viewport||{},vw=Number(view.width||window.innerWidth||0),vh=Number(view.height||window.innerHeight||0),a=anchor.getBoundingClientRect(),r=picker.getBoundingClientRect(),w=r.width||picker.offsetWidth||0,h=r.height||picker.offsetHeight||0;
    const left=Math.max(margin,Math.min(a.right-w,vw-w-margin));
    const below=a.bottom+gap,above=a.top-h-gap,top=below+h<=vh-margin?below:(above>=margin?above:Math.max(margin,Math.min(below,vh-h-margin)));
    return{left,top};
  }
  function placeReactionPicker(anchor,picker){
    /* The message list scrolls and clips its descendants. Portal the picker to the viewport, then
       flip it above a bottom-edge trigger (the common last-message case) and clamp both axes. */
    document.body.appendChild(picker);const at=reactionPickerPosition(anchor,picker);picker.style.setProperty('position','fixed','important');picker.style.setProperty('inset','auto','important');picker.style.setProperty('left',at.left+'px','important');picker.style.setProperty('top',at.top+'px','important');
  }
  function normalizeIcon(raw){
    const v=String(raw||'').trim(); if(!v)return '';
    try{ const u=new URL(v); if(u.protocol==='https:'||u.protocol==='http:'||u.protocol==='blob:')return u.href; }catch(_){}
    return Array.from(v).slice(0,4).join('');
  }
  function roomIcon(p,r,i){
    const key=r&&roomIdentity(r),cached=key&&roomIconRefs.get(key);
    const icon=normalizeIcon(cached&&cached.url||r&&r.icon);
    if(r&&r.iconPointer&&!cached&&!storedIconLoads.has(key))void hydrateStoredRoomIcon(p,r,key);
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
  const remoteMessages=new Map(),remoteStoreIds=new Set();
  function testMessages(id){ if(remoteMessages.has(id))return uniqueMessages(remoteMessages.get(id));try{ const v=JSON.parse(localStorage.getItem('pc.concord.test.'+id)||'[]'); return uniqueMessages(v); }catch(_){ return []; } }
  function markRemoteStore(id){if(!remoteMessages.has(id))remoteMessages.set(id,testMessages(id));remoteStoreIds.add(id);try{localStorage.removeItem('pc.concord.test.'+id);}catch(_){} }
  function saveTestMessages(id,v){const clean=uniqueMessages(v);if(remoteStoreIds.has(id)){remoteMessages.set(id,clean.slice(-5000));try{localStorage.removeItem('pc.concord.test.'+id);}catch(_){}return;}try{localStorage.setItem('pc.concord.test.'+id,JSON.stringify(clean.slice(-200)));}catch(_){} }
  async function clearRoomCache(room){const loadKey=room&&(room.communityId||room.naddr);if(!loadKey)return;try{if(window.PCConcordCache)await window.PCConcordCache.dropRoom(loadKey);}catch(e){console.warn('Concord room cache cleanup failed',e);}for(const channel of channelsOf(room)){const id=channelStoreId(room,channel.name);remoteMessages.delete(id);remoteStoreIds.delete(id);try{localStorage.removeItem('pc.concord.test.'+id);}catch(_){}}for(const id of [...remoteMessages.keys()])if(room.naddr&&id.startsWith(room.naddr)){remoteMessages.delete(id);remoteStoreIds.delete(id);}const icon=roomIconRefs.get(loadKey);if(icon&&/^blob:/i.test(String(icon.url||'')))try{URL.revokeObjectURL(icon.url);}catch(_){}roomIconRefs.delete(loadKey);}
  function pendingEchoMatch(messages,remote){
    const candidates=(messages||[]).filter(m=>m&&m.pending&&String(m.pubkey||'')===String(remote&&remote.pubkey||'')&&String(m.text||'')===String(remote&&remote.text||'')&&Number(m.kind||9)===Number(remote&&remote.kind||9)).map(m=>({message:m,gap:Math.abs(Number(m.at||0)-Number(remote&&remote.at||0))})).filter(x=>x.gap<120000).sort((a,b)=>a.gap-b.gap);
    if(!candidates.length)return null;
    /* Two intentional identical sends can coexist. A relay echo has no trustworthy way to identify
     * either pending row from content and approximate time alone; leave it separate until createChatWrap returns the
     * permanent rumor id, rather than guessing and collapsing a real message. */
    if(candidates.length>1)return null;
    return candidates[0].message;
  }
  function mergeRelayMessages(prior,incoming){
    const out=uniqueMessages(prior),byId=new Map(out.map(m=>[messageId(m),m]));
    for(const remote of incoming||[]){
      const id=messageId(remote);if(byId.has(id)){Object.assign(byId.get(id),remote);continue;}
      /* A relay echo may arrive before createChatWrap() returns its permanent rumor id. Reconcile
       * that echo with the optimistic row by authorship/content/kind and a tight send-time window;
       * otherwise the same send is painted twice until another refresh happens. */
      const pending=pendingEchoMatch(out,remote);
      if(pending){byId.delete(messageId(pending));Object.assign(pending,remote,{pending:false,remote:true});byId.set(id,pending);}
      else{out.push(remote);byId.set(id,remote);}
    }
    return uniqueMessages(out);
  }
  function channelStoreId(room,name){ const channel=name||'general',c=room&&Array.isArray(room.channels)&&room.channels.find(x=>x.name===channel),identity=String(room&&(room.naddr||room.communityId||room.url)||''); return identity+(channel!=='general'?'.'+(c&&c.id||channel):''); }
  function channelsOf(room){
    const channels=room&&Array.isArray(room.channels)?room.channels.filter(c=>c&&c.name):[];
    return channels.length?channels:[{name:'general',private:false}];
  }
  function activeMessages(room){ return testMessages(channelStoreId(room,state.channel)); }
  /* A CORD stream may retain 5,000 decrypted messages in renderer memory. Building every row,
   * link preview and media control in one innerHTML assignment blocks Android's UI thread (and can
   * make desktop Chromium report the window as frozen). The durable encrypted cache still keeps
   * the complete history; the conversation paints a bounded newest window, matching the 300-row
   * cache page used during cold launch. */
  const MAX_PAINTED_MESSAGES=300;
  function paintedMessages(room){const all=activeMessages(room);return all.length>MAX_PAINTED_MESSAGES?all.slice(-MAX_PAINTED_MESSAGES):all;}
  function roomParticipants(room,viewerPubkey=''){
    return [...new Set([viewerPubkey,...channelsOf(room).flatMap(channel=>
      testMessages(channelStoreId(room,channel.name)).map(message=>message&&message.pubkey)
    )].filter(Boolean))];
  }
  function mentionAliases(profile,pubkey,fallback=''){
    const aliases=new Set([profile&&profile.display_name,profile&&profile.name,fallback,pubkey]
      .map(value=>String(value||'').trim().toLowerCase()).filter(Boolean));
    for(const value of [...aliases])aliases.add(value.replace(/\s+/g,'_'));
    return aliases;
  }
  function typedMentionRecipients(text,participants,profileOf){
    const tokens=new Set([...String(text||'').matchAll(/(?:^|\s)@([\w.-]+)/g)].map(match=>match[1].toLowerCase()));
    if(!tokens.size)return [];
    return [...new Set((participants||[]).filter(pubkey=>{
      const profile=profileOf?profileOf(pubkey)||{}:{};
      return [...mentionAliases(profile,pubkey)].some(alias=>tokens.has(alias));
    }))];
  }
  function textMentionsViewer(text,handles){
    const tokens=new Set([...String(text||'').matchAll(/(?:^|\s)@([\w.-]+)/g)].map(match=>match[1].toLowerCase()));
    return (handles||[]).some(handle=>tokens.has(String(handle||'').trim().toLowerCase().replace(/\s+/g,'_')));
  }
  function lastActivity(room){ return channelsOf(room).reduce((n,c)=>Math.max(n,...testMessages(channelStoreId(room,c.name)).map(x=>Number(x.at)||0)),0); }
  function channelReadKey(room,name){ return 'pc.concord.read.'+(room&&room.naddr||'')+':'+(name||'general'); }
  function seenAt(room,name){
    if(!room||!room.naddr)return 0;
    const exact=Number(localStorage.getItem(channelReadKey(room,name))||0);
    if(exact)return exact;
    /* Migration fallback for the release that stored one timestamp for the whole community. */
    return Number(localStorage.getItem('pc.concord.read.'+room.naddr)||0);
  }
  function markRead(room,name){ if(room&&room.naddr)localStorage.setItem(channelReadKey(room,name),String(Date.now())); }
  function isUnread(room){ return channelsOf(room).some(c=>testMessages(channelStoreId(room,c.name)).some(m=>(Number(m.at)||0)>seenAt(room,c.name))); }
  function conversationIsVisible(narrow,chatOpen,drawerOpen){ return !narrow||(!!chatOpen&&!drawerOpen); }
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
  function publicAttachments(m){
    const out=[];
    for(const tag of (m&&m.tags||[])){
      const f=imetaFields(tag),mime=String(f.m||'').toLowerCase();
      if(f['encryption-algorithm']||mime==='application/x-webxdc'||mime==='application/webxdc+zip'||mime==='application/vnd.webxdc+zip'||!/^https:\/\//i.test(f.url||''))continue;
      if(!/^[\w.+-]+\/[\w.+-]+$/.test(mime))continue;
      out.push({url:f.url,mime,name:String(f.name||'attachment').slice(0,120)});
    }
    return out;
  }
  function messageContentHtml(p,m,room,channelName){
    const files=encryptedAttachments(m),publicFiles=publicAttachments(m);
    /* Chat content is relay input. Keep the complete rumor in memory/cache, but never hand a
     * multi-megabyte corrupt field to linkify, preview detection and innerHTML on the launch path. */
    let text=String(m&&m.text||'').slice(0,65536);
    for(const f of [...files,...publicFiles])text=text.split(f.url).join('').trim();
    /* A Webxdc imeta is an attachment, not two messages. When its playable card is available,
     * remove only that exact attachment URL from prose so linkify/link-preview cannot paint a raw
     * Blossom link above the card. Keep the URL when Webxdc is unavailable: it remains the user's
     * download/open fallback instead of turning into an invisible attachment. */
    const mini=webxdcOf(m,room,channelName),canPlayMini=!!(mini&&window.PCWebxdc&&PCWebxdc.cardHtml);
    if(canPlayMini)text=text.split(mini.url).join('').replace(/\s{2,}/g,' ').trim();
    const body=text?`<p>${p.linkify?p.linkify(text):p.enc(text)}</p>${p.linkCardHtml?p.linkCardHtml(text):''}`:'';
    const publicMedia=publicFiles.map(f=>{const url=p.enc(f.url),label=p.enc(f.name||'attachment');if(f.mime.startsWith('image/'))return `<div class="cc-plain-attachment"><img src="${url}" alt="${label}" loading="lazy"></div>`;if(f.mime.startsWith('video/'))return `<div class="cc-plain-attachment cc-attachment-media"><video src="${url}" controls playsinline preload="metadata" title="Double-click to expand"></video></div>`;if(f.mime.startsWith('audio/'))return `<div class="cc-plain-attachment"><audio src="${url}" controls preload="metadata"></audio></div>`;return `<div class="cc-plain-attachment"><a href="${url}" download="${label}">Download ${label}</a></div>`;}).join('');
    const media=files.map((f,i)=>`<div class="cc-encrypted-attachment" data-cc-attachment="${p.enc(messageId(m))}" data-cc-attachment-index="${i}"><span>🔒 Decrypting ${p.enc(f.name||f.mime)}…</span></div>`).join('');
    /* Paint the canonical room-aware card with the message. A deferred generic link card could
     * otherwise win the render race and replace Armada's explicit webxdc-topic. */
    const miniCard=canPlayMini?PCWebxdc.cardHtml(mini):'';
    return body+miniCard+publicMedia+media;
  }
  async function decryptAttachment(file){
    const ck=file.url+'\0'+file.hash; if(attachmentCache.has(ck))return attachmentCache.get(ck);
    if(attachmentLoads.has(ck))return attachmentLoads.get(ck);
    const work=(async()=>{const res=await fetch(file.url,{credentials:'omit'});if(!res.ok)throw new Error('download failed');const cipher=await res.arrayBuffer();if(cipher.byteLength>50*1024*1024)throw new Error('attachment is too large');const key=await crypto.subtle.importKey('raw',hexBytes(file.key),'AES-GCM',false,['decrypt']);const plain=new Uint8Array(await crypto.subtle.decrypt({name:'AES-GCM',iv:hexBytes(file.nonce)},key,cipher));const hash=bytesHex(await crypto.subtle.digest('SHA-256',plain));if(hash!==file.hash)throw new Error('integrity check failed');const value={url:URL.createObjectURL(new Blob([plain],{type:file.mime})),mime:file.mime,name:file.name};attachmentCache.set(ck,value);while(attachmentCache.size>64){const [old,v]=attachmentCache.entries().next().value;attachmentCache.delete(old);try{URL.revokeObjectURL(v.url);}catch(_){}}return value;})();
    attachmentLoads.set(ck,work);try{return await work;}finally{attachmentLoads.delete(ck);}
  }
  function attachmentLightbox(p,host,url,kind){
    if(!p.openLightbox)return;
    const media=[...(host.closest('.cc-message-body')||document).querySelectorAll('.cc-encrypted-attachment img,.cc-encrypted-attachment video')];
    const items=media.map(el=>({src:el.currentSrc||el.src,kind:el.tagName==='VIDEO'?'video':null})).filter(x=>x.src);
    const here=items.findIndex(x=>x.src===url);
    p.openLightbox(url,kind||null,items.length>1?{items,i:Math.max(0,here)}:null);
  }
  function wireRoomMedia(p){
    if(!p.openLightbox||!document.querySelectorAll)return;
    const media=[...document.querySelectorAll('.cc-message-body img,.cc-message-body video')]
      .filter(el=>!el.closest('.cc-encrypted-attachment'));
    const items=media.map(el=>({src:el.currentSrc||el.src,kind:el.tagName==='VIDEO'?'video':null}))
      .filter(x=>x.src);
    for(const el of media){
      if(el.dataset.ccViewer==='1')continue; el.dataset.ccViewer='1';
      const open=e=>{
        e.preventDefault();e.stopPropagation();
        const url=el.currentSrc||el.src;if(!url)return;
        const here=items.findIndex(x=>x.src===url);
        p.openLightbox(url,el.tagName==='VIDEO'?'video':null,
          items.length>1?{items,i:Math.max(0,here)}:null);
      };
      /* A click on a <video controls> is also how Chromium reports presses on Play, seek, volume
       * and fullscreen. Installing the image-style onclick handler there consumed those controls
       * and made the attachment disappear into a repainted lightbox when somebody pressed Play.
       * Images open on one click; videos keep native controls and use double-click to expand. */
      if(el.tagName==='VIDEO'){
        el.ondblclick=open;
        if(!el.title)el.title='Double-click to expand';
      }else el.onclick=open;
    }
  }
  async function hydrateEncryptedAttachments(messages){
    if(!document.querySelectorAll)return;
    const byId=new Map((messages||[]).map(m=>[messageId(m),m]));
    for(const host of document.querySelectorAll('.cc-encrypted-attachment[data-cc-attachment]')){
      const m=byId.get(host.dataset.ccAttachment),file=m&&encryptedAttachments(m)[Number(host.dataset.ccAttachmentIndex)||0];if(!file)continue;
      try{const got=await decryptAttachment(file);if(!host.isConnected)continue;const p=PC(),url=p.enc(got.url),label=p.enc(got.name||'attachment');if(got.mime.startsWith('image/')){host.innerHTML=`<button class="cc-attachment-open" type="button" aria-label="Open ${label}"><img src="${url}" alt="${label}" loading="lazy"></button>`;const open=host.querySelector('.cc-attachment-open');if(open)open.onclick=e=>{e.preventDefault();e.stopPropagation();attachmentLightbox(p,host,got.url,null);};}else if(got.mime.startsWith('video/')){host.innerHTML=`<div class="cc-attachment-media"><video src="${url}" controls playsinline preload="metadata" title="Double-click to expand"></video><button class="cc-attachment-expand" type="button" aria-label="Open ${label}">↗</button></div>`;const openVideo=e=>{e.preventDefault();e.stopPropagation();attachmentLightbox(p,host,got.url,'video');};const video=host.querySelector('video');if(video)video.ondblclick=openVideo;const open=host.querySelector('.cc-attachment-expand');if(open)open.onclick=openVideo;}else if(got.mime.startsWith('audio/'))host.innerHTML=`<audio src="${url}" controls preload="metadata"></audio>`;else host.innerHTML=`<a href="${url}" download="${label}">Download ${label}</a>`;}catch(_){if(host.isConnected)host.innerHTML='<span class="cc-attachment-error">Could not decrypt attachment</span>';}
    }
  }
  function channelStarKey(room,name){ return `pc.concord.star.${room&&(room.communityId||room.naddr||room.url)||'unknown'}:${name||'general'}`; }
  function channelStarred(room,name){ try{return localStorage.getItem(channelStarKey(room,name))==='1';}catch(_){return false;} }
  function setChannelStarred(room,name,on){ try{if(on)localStorage.setItem(channelStarKey(room,name),'1');else localStorage.removeItem(channelStarKey(room,name));}catch(_){} }
  function orderedChannels(room){ return channelsOf(room).map((channel,index)=>({channel,index,starred:channelStarred(room,channel.name)})).sort((a,b)=>Number(b.starred)-Number(a.starred)||a.index-b.index).map(x=>x.channel); }
  function channelRowsHtml(p,room,channels){
    return (channels||[]).map(c=>`<div class="cc-channel-row${channelStarred(room,c.name)?' starred':''}"><button class="cc-channel${(state.channel||'general')===c.name?' active':''}${testMessages(channelStoreId(room,c.name)).some(m=>(Number(m.at)||0)>seenAt(room,c.name))?' unread':''}" data-cc-channel="${p.enc(c.name)}"><span>#</span> ${p.enc(c.name)}</button><button class="cc-channel-star" data-cc-star="${p.enc(c.name)}" aria-pressed="${channelStarred(room,c.name)}" title="${channelStarred(room,c.name)?'Remove from starred channels':'Star channel'}" aria-label="${channelStarred(room,c.name)?'Unstar':'Star'} #${p.enc(c.name)}">${channelStarred(room,c.name)?'★':'☆'}</button></div>`).join('');
  }
  function channelSectionsHtml(p,room,channels){
    const starred=(channels||[]).filter(c=>channelStarred(room,c.name)),regular=(channels||[]).filter(c=>!channelStarred(room,c.name));
    return `${starred.length?`<div class="cc-section cc-starred-section">STARRED</div>${channelRowsHtml(p,room,starred)}`:''}<div class="cc-section">TEXT CHANNELS</div>${channelRowsHtml(p,room,regular)}`;
  }
  function threadParticipants(messages,target,viewerPubkey){
    const rows=messages||[],byId=new Map(rows.map(m=>[messageId(m),m])),people=new Set();
    const parentId=node=>String((node&&node.reply&&node.reply.id)||
      (((node&&node.tags)||[]).find(t=>t[0]==='e'||t[0]==='E')||[])[1]||'');
    const rootId=node=>{const seen=new Set();let last=messageId(node);while(node&&!seen.has(messageId(node))){last=messageId(node);seen.add(last);const parent=parentId(node);node=parent&&byId.get(parent);}return last;};
    const root=rootId(target);
    /* Reply UX follows Armada/Element: a reply notifies everybody already participating in this
     * thread, not only the selected message's ancestor chain. This matters most when replying to
     * the root after several branches exist — the old upward-only walk silently omitted them. */
    for(const node of [target,...rows]){
      if(!node||rootId(node)!==root)continue;
      if(node.pubkey&&node.pubkey!==viewerPubkey)people.add(node.pubkey);
    }
    return [...people];
  }
  function webxdcOf(m,room,channelName){
    const channel=(room&&room.channels||[]).find(c=>c.name===(channelName||'general')),protocol=room&&room.protocol==='nip29'?'nip29':(room&&room.cord?'concord2':''),scope=protocol==='nip29'?`nip29|${room.relay}|${room.groupId}`:(protocol==='concord2'&&channel&&channel.id?`concord2|${channel.id}`:''),transport=protocol?{protocol,room:roomIdentity(room),channel:channelName||'general',channelId:channel&&channel.id,relay:room.relay,groupId:room.groupId}:null;
    for(const t of (m&&m.tags||[]).slice(0,100)){ if(t[0]!=='imeta')continue; const f={}; for(let i=1;i<Math.min(t.length,30);i++){ const s=String(t[i]||'').slice(0,4096),j=s.indexOf(' '); if(j>0)f[s.slice(0,j)]=s.slice(j+1); } const mime=(f.m||'').toLowerCase();/* `webxdc-topic` is the current Armada/Vector Iroh lobby contract and wins over the legacy alias if both are present. */if((mime==='application/vnd.webxdc+zip'||mime==='application/webxdc+zip'||mime==='application/x-webxdc')&&/^https?:\/\//i.test(f.url||'')){const explicit=f['webxdc-topic']||f.webxdc||'';return {url:f.url,sha:f.x||'',uuid:explicit,urlTopicMessageId:explicit?'':messageId(m),name:(f.summary||f.name||'Mini app').slice(0,80),transport};} }
    /* Bare links have no attachment tag to carry a minted topic. Armada/Vector derive it from the
     * canonical URL plus this message id; deriving from room scope silently creates a private lobby. */
    const url=(String(m&&m.text||'').slice(0,200000).match(/https?:\/\/[^\s<>]+\.xdc(?:\?[^\s<>]*)?/i)||[])[0]; return url?{url,sha:'',uuid:'',urlTopicMessageId:messageId(m),name:'Mini app',transport}:null;
  }
  /* A delegated Webxdc click may observe a card produced by the generic link hydrator before the
   * room-specific render settles. Resolve identity from the authoritative decrypted CORD message
   * at launch time, so stale DOM can never move a player into a URL-derived private lobby. */
  function resolveWebxdcCard(card,fallback){
    /* An explicit topic is the attachment author's lobby and is authoritative. A partially hydrated
     * room cache may temporarily expose the same URL as a bare link; re-deriving from that row's id
     * silently moves a running Armada game into a different lobby. */
    if(fallback&&fallback.uuid)return fallback;
    try{const row=card&&card.closest&&card.closest('.cc-message[data-message-id]'),room=saved()[state.community],id=row&&row.dataset&&row.dataset.messageId,m=room&&activeMessages(room).find(x=>messageId(x)===id),resolved=m&&webxdcOf(m,room,state.channel);return (resolved&&resolved.uuid?resolved:fallback)||resolved;}catch(_){return fallback;}
  }
  async function deriveWebxdcUrlTopic(url,messageId){if(!window.PCWebxdc||!PCWebxdc.deriveUrlTopic)throw new Error('Webxdc topic support is unavailable');return PCWebxdc.deriveUrlTopic(url,messageId);}
  function mintWebxdcTopic(){if(!window.PCWebxdc||!PCWebxdc.mintTopic)throw new Error('Webxdc topic support is unavailable');return PCWebxdc.mintTopic();}
  function webxdcHtml(m,room,channel){ const app=webxdcOf(m,room,channel); return app&&window.PCWebxdc&&PCWebxdc.cardHtml?PCWebxdc.cardHtml(app):''; }
  function hydrateWebxdcCards(room){if(!window.PCWebxdc||!PCWebxdc.cardHtml||!document.querySelectorAll)return;const epoch=++webxdcHydrationEpoch,byId=new Map(activeMessages(room).map(m=>[messageId(m),m])),rows=[...document.querySelectorAll('.cc-message[data-message-id]')];let at=0;const batch=()=>{if(epoch!==webxdcHydrationEpoch)return;const end=Math.min(rows.length,at+8);for(;at<end;at++){const el=rows[at],m=byId.get(el.dataset.messageId),app=m&&webxdcOf(m,room,state.channel),html=app&&PCWebxdc.cardHtml(app),body=el.querySelector('.cc-message-body'),old=body&&body.querySelector('.xdc-card');if(!html||!body)continue;let stale=!old;if(old){try{const prior=JSON.parse(old.dataset.xdc||'null');stale=!prior||prior.uuid!==app.uuid||prior.urlTopicMessageId!==app.urlTopicMessageId||prior.url!==app.url||JSON.stringify(prior.transport||null)!==JSON.stringify(app.transport||null);}catch(_){stale=true;}}if(stale){if(old)old.remove();body.insertAdjacentHTML('beforeend',html);}}if(at<rows.length)(window.requestAnimationFrame||setTimeout)(batch);};(window.requestAnimationFrame||setTimeout)(batch);}
  function hexBytes(s){ const h=String(s||''); if(!/^[0-9a-f]+$/i.test(h)||h.length%2)throw new Error('invalid encrypted image key'); return new Uint8Array(h.match(/../g).map(x=>parseInt(x,16))); }
  function bytesHex(a){ return [...new Uint8Array(a)].map(x=>x.toString(16).padStart(2,'0')).join(''); }
  function imageMime(a){ return a[0]===0x89&&a[1]===0x50?'image/png':a[0]===0xff&&a[1]===0xd8?'image/jpeg':a[0]===0x47&&a[1]===0x49?'image/gif':a[0]===0x52&&a[1]===0x49&&a[8]===0x57?'image/webp':''; }
  async function boundedImageBytes(res,max){
    const announced=Number(res.headers&&res.headers.get&&res.headers.get('content-length'))||0;
    if(announced>max)throw new Error('community icon is too large');
    if(!res.body||!res.body.getReader){const bytes=new Uint8Array(await res.arrayBuffer());if(bytes.byteLength>max)throw new Error('community icon is too large');return bytes;}
    const reader=res.body.getReader(),parts=[];let size=0;
    try{while(true){const {done,value}=await reader.read();if(done)break;size+=value.byteLength;if(size>max){await reader.cancel();throw new Error('community icon is too large');}parts.push(value);}}finally{try{reader.releaseLock();}catch(_){}}
    const out=new Uint8Array(size);let at=0;for(const part of parts){out.set(part,at);at+=part.byteLength;}return out;
  }
  async function decryptImagePointer(pointer,loadKey,ref){
    try{const cached=window.PCConcordCache&&await window.PCConcordCache.getIcon(loadKey,ref);if(cached)return URL.createObjectURL(new Blob([cached.bytes],{type:cached.mime||'image/*'}));}catch(e){console.warn('Concord icon cache read failed',e);}
    const url=new URL(String(pointer&&pointer.url||''),location.href);if(!/^https?:$/.test(url.protocol))throw new Error('invalid community icon URL');
    const rawKey=hexBytes(pointer.key),nonce=hexBytes(pointer.nonce);
    /* CORD-02/Armada/Vector use a 16-byte AES-GCM nonce for encrypted community
     * images. Older PosterChan-created pointers used WebCrypto's common 12-byte
     * nonce, so retain read compatibility without accepting arbitrary IV sizes. */
    if(rawKey.byteLength!==32||(nonce.byteLength!==16&&nonce.byteLength!==12)||!(/^[0-9a-f]{64}$/i.test(String(pointer.hash||''))))throw new Error('invalid encrypted image pointer');
    const max=(window.PCConcordCache&&PCConcordCache.MAX_ICON_BYTES)||5*1024*1024,
      res=await fetch(url.href); if(!res.ok)throw new Error('community icon download failed');
    const encrypted=await boundedImageBytes(res,max+32),key=await crypto.subtle.importKey('raw',rawKey,'AES-GCM',false,['decrypt']);
    const plain=new Uint8Array(await crypto.subtle.decrypt({name:'AES-GCM',iv:nonce},key,encrypted));if(plain.byteLength>max)throw new Error('community icon is too large');
    const hash=bytesHex(await crypto.subtle.digest('SHA-256',plain)); if(hash!==String(pointer.hash).toLowerCase())throw new Error('community icon failed integrity check');
    const mime=imageMime(plain);if(!mime)throw new Error('community icon is not a supported image');try{if(window.PCConcordCache)await window.PCConcordCache.putIcon(loadKey,ref,plain,mime);}catch(e){console.warn('Concord icon cache write failed',e);}return URL.createObjectURL(new Blob([plain],{type:mime}));
  }
  async function applyRoomIconMetadata(room,info,loadKey){
    if(!room||!info||!Object.prototype.hasOwnProperty.call(info,'icon'))return false;
    const value=info.icon,ref=typeof value==='string'?value:JSON.stringify(value||null);
    const cached=roomIconRefs.get(loadKey);
    if(cached&&cached.ref===ref)return false;
    const failed=failedIconRefs.get(loadKey);
    if(failed&&failed.ref===ref&&Date.now()-failed.at<60000)return false;
    try{
      const icon=value?(typeof value==='string'?value:await decryptImagePointer(value,loadKey,ref)):'';
      if(cached&&cached.url&&/^blob:/i.test(cached.url)&&cached.url!==icon)try{URL.revokeObjectURL(cached.url);}catch(_){}
      roomIconRefs.set(loadKey,{ref,url:icon});
      failedIconRefs.delete(loadKey);
      const before=JSON.stringify([room.icon,room.iconPointer]);
      if(value&&typeof value==='object'){room.iconPointer=value;if(/^blob:/i.test(String(room.icon||'')))room.icon='';}
      else{delete room.iconPointer;room.icon=icon;}
      return before!==JSON.stringify([room.icon,room.iconPointer]);
    }catch(error){
      /* An icon is decoration, never a prerequisite for channels or history. Back off this exact
       * broken pointer for a minute: metadata refresh runs every four seconds, and retrying an
       * invalid encrypted value on every pass starves message work and floods Android's renderer. */
      failedIconRefs.set(loadKey,{ref,at:Date.now()});
      console.warn('Concord community icon could not be loaded',error);
      return false;
    }
  }
  async function hydrateStoredRoomIcon(p,room,loadKey){
    if(!room||!room.iconPointer||!loadKey||storedIconLoads.has(loadKey))return;
    storedIconLoads.add(loadKey);
    try{
      const changed=await applyRoomIconMetadata(room,{icon:room.iconPointer},loadKey);
      if(changed){const rooms=saved(),i=rooms.findIndex(x=>roomIdentity(x)===roomIdentity(room));if(i>=0){rooms[i].iconPointer=room.iconPointer;if(/^blob:/i.test(String(rooms[i].icon||'')))rooms[i].icon='';save(rooms);}}
      /* Repaint only after hydration produced an icon. On a corrupt pointer applyRoomIconMetadata
       * records backoff and returns false. Repainting anyway calls roomIcon() again after this
       * promise clears storedIconLoads, producing an unbounded fail -> render -> fail microtask
       * loop (observed at ~7 GB / 90% CPU in the packaged desktop). */
      if(roomIconRefs.has(loadKey)&&document.body.classList.contains('concord-view'))backgroundRender();
    }finally{storedIconLoads.delete(loadKey);}
  }
  function reactionSummary(p,m){
    const reactions=m&&m.reactions&&typeof m.reactions==='object'?m.reactions:{};
    const urls=m&&m.reactionUrls&&typeof m.reactionUrls==='object'?m.reactionUrls:{};
    const viewer=p.viewer?p.viewer():{};
    return Object.entries(reactions).map(([emoji,people])=>{ const n=Array.isArray(people)?people.length:0,mine=!!(viewer.pubkey&&people.includes(viewer.pubkey)),url=/^https?:\/\//i.test(String(urls[emoji]||''))?String(urls[emoji]):'',face=url?`<img class="cc-reaction-emoji" src="${p.enc(url)}" alt="${p.enc(emoji)}" title="${p.enc(emoji)}" loading="lazy">`:`<span>${p.enc(emoji)}</span>`; return n?`<button class="cc-reaction${mine?' mine':''}" aria-pressed="${mine}" data-cc-react-toggle="${p.enc(messageId(m))}" data-cc-emoji="${p.enc(emoji)}" title="${n} reaction${n===1?'':'s'}">${face}<b>${n}</b></button>`:''; }).join('');
  }
  function mentionSeenKey(room,channel){ return 'pc.concord.seen.'+room.naddr+':'+(channel||'general'); }
  function notifyMentions(p,room,messages,viewer,me,channel=state.channel||'general'){
    if(!room||!room.naddr||!messages.length||!viewer.pubkey)return;
    const key=mentionSeenKey(room,channel), newest=Math.max(...messages.map(m=>Number(m.at)||0));
    /* The original release stored one cursor for the whole community. Only #general can inherit
     * that value safely: applying its newest timestamp to every channel lets a newer general post
     * permanently suppress an older (but newly fetched) #support mention. */
    let seen=Number(localStorage.getItem(key)||0);
    if(!seen&&channel==='general')seen=Number(localStorage.getItem('pc.concord.seen.'+room.naddr)||0);
    if(!seen){ localStorage.setItem(key,String(newest)); return; } // opening history must not alert
    const profile=viewer.profile||{}, handles=[me,profile.name,profile.display_name,viewer.npub,viewer.pubkey].filter(Boolean);
    for(const m of messages){
      const body=String(m.text||''),fromMe=m.pubkey===viewer.pubkey;
      const tagged=(m.tags||[]).some(t=>(t[0]==='p'||t[0]==='P')&&String(t[1]||'')===viewer.pubkey);
      const mentioned=!fromMe&&(tagged||textMentionsViewer(body,handles));
      if((Number(m.at)||0)>seen&&mentioned&&p.osNotify) p.osNotify(`Mention in #${channel}`,`${m.by||'Someone'}: ${body}`,{tag:'concord-mention-'+room.naddr+':'+channel,route:notificationRoute(room,channel,m)});
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
    if(discoveryStarted||state.community!=null||!p.relaySubscribe)return; discoveryStarted=true;
    discoveryAbortController=typeof AbortController==='function'?new AbortController():null;
    const signal=discoveryAbortController&&discoveryAbortController.signal;
    const bySigner=new Map();
    /* Some relay pools replay their in-memory result set synchronously from subscribe(). Calling
     * render() from each callback re-entered the launch render (or replaced the home DOM hundreds
     * of times in one task), leaving Concord apparently frozen. Coalesce discovery paint to the
     * next task; the room list itself is updated immediately. */
    const paintDiscovery=()=>{if(discoveryPaintPending)return;discoveryPaintPending=true;setTimeout(()=>{discoveryPaintPending=false;if(state.community==null)backgroundRender();},0);};
    /* Public cards are summaries of their announcement event. Do not hydrate every invite merely
       because it scrolled into Discover: that multiplied 24 cards by every bootstrap/control relay
       (including legacy Ditto invites). Full bootstrap is reserved for the explicit Join action. */
    const onEvent=ev=>{ for(const item of discoverInvites(ev.content,ev)){ const old=bySigner.get(item.naddr); if(!old||Number(ev.created_at)>Number(old.source.created_at))bySigner.set(item.naddr,item); recoverOwnedInvite(p,item); } discovered=[...bySigner.values()].sort((a,b)=>Number(b.source.created_at)-Number(a.source.created_at)); paintDiscovery(); };
    const onEose=()=>{ discoveryLoaded=true; paintDiscovery(); };
    const filters=[{kinds:[1],search:'armada.buzz/invite',limit:100},{kinds:[1],search:'poster.place/invite',limit:100}];
    try{ discoverySubscription=p.relaySubscribe(filters,{onEvent,onEose,live:true})||null; if(p.relayQueryFrom)p.relayQueryFrom(DISCOVER_RELAYS,filters,{timeout:6000,max:2,signal,purpose:'concord discover listings'}).then(events=>{if(state.community!=null)return;events.forEach(onEvent);onEose();}); }catch(_){ discoveryLoaded=true; }
  }
  function stopDiscovery(){
    const subscription=discoverySubscription;discoverySubscription=null;discoveryStarted=false;
    const aborter=discoveryAbortController;discoveryAbortController=null;if(aborter)aborter.abort();
    if(subscription&&typeof subscription.close==='function')try{subscription.close();}catch(_){}
  }
  async function hydrateInvite(p,url,signal=null){
    if(!window.PosterCord||!p.relayQueryFrom)throw new Error('CORD protocol is unavailable');
    const parts=inviteParts(url); if(!parts)throw new Error('that is not a Concord invite link');
    const decoded=window.PosterCord.openInvite;
    /* openInvite validates the naddr and fragment; use its decoded bootstrap relays by first trying
       the shared CORD set plus the current pool. queryFrom covers relays outside the pool, query covers inside. */
    const parsed=window.PosterCord.inviteDetails(url);
    const filter=[{kinds:[33301],authors:[parsed.linkSigner],'#d':[''],limit:100}];
    const relays=[...new Set([...(parsed&&parsed.bootstrapRelays||[]),...CORD_RELAYS])];
    const [pool,external]=await Promise.all([p.relayQuery?p.relayQuery(filter,8000):[],p.relayQueryFrom(relays,filter,{timeout:10000,max:200,signal,purpose:'concord explicit invite',allowBlocked:true,failureCooldown:1800000})]);
    /* A link signer may issue many bundles with the same replaceable d-tag. The invite fragment opens
       exactly one of them, which is not necessarily the newest. Try every relay result instead of
       handing openInvite the set (whose legacy implementation picked index zero). */
    const candidates=[...(pool||[]),...(external||[])].filter((ev,i,a)=>ev&&a.findIndex(x=>x&&x.id===ev.id)===i).sort((a,b)=>Number(b.created_at)-Number(a.created_at));
    let opened,lastError; for(const ev of candidates){ try{ opened=decoded(url,[ev]); break; }catch(e){ lastError=e; } }
    if(!opened)throw lastError||new Error('invite bundle was not found on its bootstrap relays');
    const bundle=opened.bundle;
    /* `community_id` is the durable key used by Armada's 13302 membership vault.  Keeping it only
     * inside `cord.bundle` made persistArmadaMembership() return false, so an apparently successful
     * direct invite vanished on another device or after browser storage was cleared. */
    return {url,naddr:parts.naddr,communityId:bundle.community_id,name:bundle.name||'Concord community',description:'',channels:[{name:'general',private:false}],local:false,cord:{bundle,parsed:opened.parsed}};
  }
  async function hydrateDiscoveredIcon(p,item){
    if(!item||item.icon||discoveryIconLoads.has(item.naddr)||!window.PosterCordReader||!p.relayQueryFrom)return;
    discoveryIconLoads.add(item.naddr);
    try{
      const signal=discoveryAbortController&&discoveryAbortController.signal,room=await hydrateInvite(p,item.url,signal);if(signal&&signal.aborted)return;const bundle=room.cord.bundle,reader=window.PosterCordReader;
      const seed=reader.inspectControl(bundle,[]),relays=roomRelays(bundle);
      const wraps=await cordQuery(p,relays,[{kinds:[1059],authors:seed.controlPubkeys,limit:1000}],{timeout:10000,max:8,signal});
      const info=reader.inspectControl(bundle,wraps||[]);
      item.name=info.name||item.name; item.description=info.description||item.description;
      if(info.icon)item.icon=typeof info.icon==='string'?info.icon:await decryptImagePointer(info.icon);
      if(state.community==null)backgroundRender();
    }catch(_){} finally{ discoveryIconLoads.delete(item.naddr); }
  }
  function inviteRefUrl(ref){ const s=String(ref||''); if(/^https?:/i.test(s))return s; const [naddr,frag]=s.split('#'); return naddr&&frag?`https://armada.buzz/invite/${naddr}#${frag}`:''; }
  function normalizeRelay(value){try{const u=new URL(String(value||''));if(u.protocol!=='ws:'&&u.protocol!=='wss:')return '';return u.href.replace(/\/$/,'');}catch(_){return '';}}
  function nip29MembershipTags(value){
    const tags=Array.isArray(value)?value:[],groups=[],relays=[];
    for(const tag of tags){if(!Array.isArray(tag))continue;if(tag[0]==='group'){
      const id=String(tag[1]||'').trim(),relay=normalizeRelay(tag[2]);if(id&&relay)groups.push({id,relay,name:String(tag[3]||'').trim()});
    }else if(tag[0]==='r'){const relay=normalizeRelay(tag[1]);if(relay)relays.push(relay);}}
    return {groups:[...new Map(groups.map(g=>[g.relay+'\0'+g.id,g])).values()],relays:[...new Set(relays)]};
  }
  async function nip29Memberships(p,viewer,signal=null){
    if(!viewer.pubkey)return {groups:[],relays:[]};
    /* relayQuery already asks the connected user pool. The external half is Concord bootstrap
       recovery only; unioning relayUrls here duplicated Damus (and any other personal relay) as a
       fresh queryFrom WebSocket on every Discover recovery. */
    const filters=[{kinds:[10009],authors:[viewer.pubkey],limit:8}],relays=[...CORD_RELAYS,...LEGACY_RECOVERY_RELAYS],
      [pool,listed]=await Promise.all([p.relayQuery?p.relayQuery(filters,8000):[],p.relayQueryFrom?p.relayQueryFrom(relays,filters,{timeout:8000,max:12,exact:true,signal,purpose:'concord nip29 memberships',minInterval:1800000,allowBlocked:true,failureCooldown:1800000}):[]]),
      queried=[...new Map([...(pool||[]),...(listed||[])].map(e=>[e.id,e])).values()],events=p.verifyRelayEvents?await p.verifyRelayEvents(queried):[],
      event=events.sort((a,b)=>Number(b.created_at)-Number(a.created_at)||String(a.id).localeCompare(String(b.id)))[0];
    if(!event)return {groups:[],relays:[]};
    const all=[...(event.tags||[])];
    const ciphertext=String(event.content||'').trim();
    if(ciphertext){const methods=/\?iv=/.test(ciphertext)?['nip04dec','nip44dec']:['nip44dec','nip04dec'];for(const method of methods){if(!p[method])continue;try{const privateTags=JSON.parse(await p[method](viewer.pubkey,ciphertext));if(Array.isArray(privateTags))all.push(...privateTags);break;}catch(_){}}}
    return nip29MembershipTags(all);
  }
  async function nip29RelayQuery(p,relay,filters,timeout=8000,signal=null){if(!p.relayQueryFrom||!p.verifyRelayEvents)throw new Error('verified listed relay queries are unavailable');const events=await p.relayQueryFrom([relay],filters,{timeout,max:1,exact:true,signal,purpose:'concord nip29 room',allowBlocked:true,failureCooldown:1800000})||[];return await p.verifyRelayEvents(events);}
  async function nip29Metadata(p,relay,groupIds=[],signal=null){
    const filter={kinds:[39000],limit:200};if(groupIds.length)filter['#d']=groupIds;
    const events=await nip29RelayQuery(p,relay,[filter],8000,signal),newest=new Map(),requested=new Set(groupIds);
    for(const event of events){if(Number(event.kind)!==39000)continue;const id=String(((event.tags||[]).find(t=>t[0]==='d')||[])[1]||'');if(!id||(requested.size&&!requested.has(id)))continue;const old=newest.get(id);if(!old||Number(event.created_at)>Number(old.created_at))newest.set(id,event);}
    return [...newest].map(([id,event])=>{let meta={};try{meta=JSON.parse(event.content||'{}');}catch(_){}const tags=Object.fromEntries((event.tags||[]).filter(t=>['name','about','picture'].includes(t[0])).map(t=>[t[0],t[1]]));return{id,relay,name:String(tags.name||meta.name||meta.display_name||id),description:String(tags.about||meta.about||meta.description||''),icon:normalizeIcon(tags.picture||meta.picture||meta.icon||''),source:event};});
  }
  async function syncNip29Memberships(p,viewer,allowActive=false){
    if((state.community!=null&&!allowActive)||nip29Busy||!viewer.pubkey)return;nip29Busy=true;let recovered=false;
    const signal=discoveryAbortController&&discoveryAbortController.signal;
    try{const membership=await nip29Memberships(p,viewer,signal);if((signal&&signal.aborted)||(state.community!=null&&!allowActive))return;const byRelay=new Map();for(const g of membership.groups){if(!byRelay.has(g.relay))byRelay.set(g.relay,[]);byRelay.get(g.relay).push(g);}
      const found=[];for(const [relay,listed] of byRelay){if(state.community!=null&&!allowActive)return;let metas=[];try{metas=await nip29Metadata(p,relay,listed.map(g=>g.id),signal);}catch(_){}if(state.community!=null&&!allowActive)return;const metaById=new Map(metas.map(m=>[m.id,m]));for(const g of listed){const meta=metaById.get(g.id)||g;found.push({...meta,id:g.id,relay,name:g.name||meta.name||g.id});}}recovered=found.length>0;
      if(found.length){const rooms=saved();let changed=false;for(const g of found){const identity='nip29:'+g.relay+'#'+g.id,i=rooms.findIndex(r=>roomIdentity(r)===identity),room={protocol:'nip29',communityId:identity,naddr:identity,groupId:g.id,relay:g.relay,name:g.name||g.id,description:g.description||'',icon:g.icon||'',channels:[{name:'general',id:g.id,private:false}],local:false};if(i<0){rooms.push(room);changed=true;}else if(rooms[i].protocol==='nip29'&&JSON.stringify(rooms[i])!==JSON.stringify({...rooms[i],...room})){rooms[i]={...rooms[i],...room};changed=true;}}if(changed){save(rooms);backgroundRender();}}
    }catch(e){console.warn('NIP-29 membership sync failed',e);}finally{nip29Busy=false;clearTimeout(nip29RetryTimer);if(state.community==null)nip29RetryTimer=setTimeout(()=>syncNip29Memberships(p,p.viewer?p.viewer():viewer),recovered?60000:120000);}
  }
  function foldNip29History(events,p,groupId){const scoped=events.filter(e=>(e.tags||[]).some(t=>t[0]==='h'&&t[1]===groupId)).sort((a,b)=>Number(a.created_at)-Number(b.created_at)),deletions=[],deleted=new Set(),byId=new Map(),reactions=[];for(const e of scoped){if(e.kind===5){deletions.push(e);continue;}if(e.kind===7){reactions.push(e);continue;}if(![9,10,11,12,1111].includes(e.kind))continue;const pr=p.profOf?p.profOf(e.pubkey):{};byId.set(e.id,{id:e.id,pubkey:e.pubkey,by:pr.display_name||pr.name||e.pubkey.slice(0,12)+'…',text:e.content,at:Number(e.created_at)*1000,kind:e.kind,tags:e.tags||[],reactions:{},reactionIds:{},remote:true});}const reactionById=new Map(reactions.map(e=>[e.id,e]));for(const deletion of deletions)for(const t of deletion.tags||[])if(t[0]==='e'){const target=byId.get(t[1])||reactionById.get(t[1]);if(target&&target.pubkey===deletion.pubkey)deleted.add(t[1]);}for(const id of deleted)byId.delete(id);for(const e of reactions){if(deleted.has(e.id))continue;const target=((e.tags||[]).find(t=>t[0]==='e')||[])[1],m=byId.get(target);if(!m)continue;const emoji=e.content==='+'?'👍':e.content||'👍';(m.reactions[emoji]||(m.reactions[emoji]=[])).push(e.pubkey);(m.reactionIds[emoji]||(m.reactionIds[emoji]={}))[e.pubkey]=e.id;}for(const m of byId.values())if(m.kind===1111){const target=byId.get(((m.tags||[]).find(t=>t[0]==='e')||[])[1]);if(target)m.reply={id:target.id,by:target.by,text:target.text};}return [...byId.values()].sort((a,b)=>a.at-b.at);}
  async function nip29History(p,room){const events=await nip29RelayQuery(p,room.relay,[{kinds:[5,7,9,10,11,12,1111],'#h':[room.groupId],limit:500}],10000);return foldNip29History(events,p,room.groupId);}
  async function hydrateNip29Room(p,index){const rooms=saved(),room=rooms[index];if(!room||room.protocol!=='nip29')return;const messages=await nip29History(p,room),storeId=channelStoreId(room,'general');markRemoteStore(storeId);saveTestMessages(storeId,messages);room.nip29Hydrated=true;rooms[index]=room;save(rooms);if(state.community===index)backgroundRender();}
  async function membershipEvents(p,pubkey,{external=true,signal=null}={}){
    /* Match Armada's wire query exactly. A mixed [13302,33302] request looks harmless, but several
       relays close the WHOLE subscription when one kind is unsupported/blocked. That made a valid
       13302 vault look absent and a fresh browser showed no communities. 13302 is CORD-02's released
       replaceable vault; the addressable migration is queried separately as a compatibility source. */
    const query=async filter=>{
      let cached=[];try{cached=window.Store&&window.Store.query?window.Store.query([filter])||[]:[];}catch(_){}
      const [pool,remote]=await Promise.all([
        p.relayQuery?Promise.resolve(p.relayQuery([filter],8000)).catch(()=>[]):[],
        external&&p.relayQueryFrom?Promise.resolve(p.relayQueryFrom([...CORD_RELAYS,...LEGACY_RECOVERY_RELAYS],[filter],{timeout:8000,max:6,signal,purpose:'concord armada memberships '+String(filter.kinds&&filter.kinds[0])+(filter['#d']?':legacy':''),minInterval:1800000,allowBlocked:true,failureCooldown:1800000})).catch(()=>[]):[],
      ]);
      return [...new Map([...(cached||[]),...(pool||[]),...(remote||[])].filter(e=>e&&e.id).map(e=>[e.id,e])).values()];
    };
    const released=await query({kinds:[13302],authors:[pubkey],limit:1});
    /* CORD-02 v2 is fragmented: Vector addresses fragment zero as d="0" (then 1, 2, ...),
       while the short-lived pre-fragment migration used d="". Ask the unrestricted kind as its
       own subscription so every coordinate is returned. Keeping the two precise queries matters
       for relays which implement tag filters but cap an unfiltered replaceable-kind result. */
    const [legacy,migrated]=await Promise.all([
      query({kinds:[33302],authors:[pubkey],'#d':[''],limit:20}),
      query({kinds:[33302],authors:[pubkey],limit:64}),
    ]);
    return [...new Map([...released,...legacy,...migrated].map(e=>[e.id,e])).values()].sort((a,b)=>Number(b.created_at)-Number(a.created_at)||String(a.id).localeCompare(String(b.id)));
  }
  function cordListHex(value){
    const s=String(value||''); if(s.length!==43)return s;
    try{const raw=atob(s.replace(/-/g,'+').replace(/_/g,'/')+'=');return [...raw].map(c=>c.charCodeAt(0).toString(16).padStart(2,'0')).join('');}catch(_){return s;}
  }
  function cordListMaterial(material,communityId){
    const m=material&&typeof material==='object'?material:{};
    /* Fragment encoding applies to every 32-byte value at every depth. `held_roots` and a private
       channel's `priors` are deliberately open extension fields, but Vector uses them for history
       across root/channel rotations. Leaving those values as base64url lets the room appear while
       silently making its older control plane and messages unreadable. */
    const held=(m.held_roots||[]).map(root=>({...root,key:cordListHex(root&&root.key)}));
    const channels=(m.channels||[]).map(c=>({...c,id:cordListHex(c.id),key:c.key?cordListHex(c.key):c.key,
      priors:(c.priors||[]).map(prior=>({...prior,key:cordListHex(prior&&prior.key)}))}));
    return {...m,community_id:cordListHex(communityId),owner:cordListHex(m.owner),owner_salt:cordListHex(m.owner_salt),community_root:cordListHex(m.community_root),control_pk:m.control_pk?cordListHex(m.control_pk):m.control_pk,control_root:m.control_root?cordListHex(m.control_root):m.control_root,held_roots:held,channels};
  }
  /* Armada's membership `current` value is a control snapshot, while the invite-derived bundle
   * contains channel secrets and older roots needed to decrypt history.  Refreshing membership must
   * update newer scalar material without throwing away those join-only fields. */
  function mergeArmadaBundle(prior,current){
    const old=prior&&typeof prior==='object'?prior:{},fresh=current&&typeof current==='object'?current:{},next={...old};
    for(const [key,value] of Object.entries(fresh))if(value!==undefined&&value!==null&&value!=='')next[key]=value;
    next.channels=Array.isArray(fresh.channels)&&fresh.channels.length?fresh.channels:(Array.isArray(old.channels)?old.channels:[]);
    next.held_roots=Array.isArray(fresh.held_roots)&&fresh.held_roots.length?fresh.held_roots:(Array.isArray(old.held_roots)?old.held_roots:[]);
    next.relays=[...new Set([...(Array.isArray(fresh.relays)?fresh.relays:[]),...(Array.isArray(old.relays)?old.relays:[])])];
    return next;
  }
  function sameJson(a,b){try{return JSON.stringify(a)===JSON.stringify(b);}catch(_){return false;}}
  function roomLoadWarning(p,key,prefix,error){
    const message=String(error&&error.message||error),now=Date.now(),old=roomLoadNotices.get(key);
    if(!old||old.message!==message||now-old.at>30000){roomLoadNotices.set(key,{message,at:now});p.toast(prefix+message);}
  }
  function decodeMembershipLists(decrypted){
    /* Resolve addressable coordinates exactly as relays do, before unioning fragments. Without
       this, an older d=0 sibling can resurrect rooms that a newer fragment tombstoned. */
    const ordinary=[],coordinates=new Map();
    for(const row of decrypted){
      const doc=row.doc||{},d=((row.event.tags||[]).find(t=>t[0]==='d')||[])[1];
      if(row.event.kind!==33302||!/^(0|[1-9]\d*)$/.test(String(d))||!Number.isInteger(Number(doc.frags))){ordinary.push(doc);continue;}
      const old=coordinates.get(Number(d)),wins=!old||Number(row.event.created_at)>Number(old.event.created_at)||(Number(row.event.created_at)===Number(old.event.created_at)&&String(row.event.id)<String(old.event.id));
      if(wins)coordinates.set(Number(d),row);
    }
    const docs=[...ordinary,...[...coordinates.values()].map(x=>x.doc)],entries=[],tombstones=[];
    for(const doc of docs){
      for(const e of Array.isArray(doc.entries)?doc.entries:[]){const cid=cordListHex(e.community_id),source=e.current||e.seed;if(!source)continue;const current=cordListMaterial(source,cid),seed=cordListMaterial(e.seed||source,cid);entries.push({...e,community_id:cid,current,seed});}
      for(const t of Array.isArray(doc.tombstones)?doc.tombstones:[])tombstones.push({...t,community_id:cordListHex(t.community_id)});
    }
    return {entries,tombstones};
  }
  async function syncArmadaMemberships(p,viewer,localOnly=false){
    if((state.community!=null&&!localOnly)||membershipBusy||!viewer.pubkey||!p.nip44dec)return; membershipBusy=true;
    let recovered=false;
    try{
      if(membershipViewer!==viewer.pubkey){membershipViewer=viewer.pubkey;membershipDocs.clear();}
      const activeRecovery=localOnly==='recovery',signal=localOnly&&!activeRecovery?null:discoveryAbortController&&discoveryAbortController.signal;
      const candidates=await membershipEvents(p,viewer.pubkey,{external:!localOnly||activeRecovery,signal});
      if(state.community!=null&&!localOnly)return;
      // Armada has emitted both kinds and may leave several list shards on relays. Decode every
      // valid snapshot: choosing one newest event can hide communities stored in another shard.
      const entries=new Map(),tombs=new Map(),decrypted=[];
      for(const event of candidates){
        try{
          let doc=membershipDocs.get(event.id);
          if(!doc){doc=JSON.parse(await p.nip44dec(viewer.pubkey,event.content));membershipDocs.set(event.id,doc);while(membershipDocs.size>256)membershipDocs.delete(membershipDocs.keys().next().value);}
          decrypted.push({event,doc}); recovered=true;
        }catch(_){}
      }
      const list=decodeMembershipLists(decrypted);
      for(const t of list.tombstones){if(t&&t.community_id)tombs.set(t.community_id,Math.max(Number(tombs.get(t.community_id))||0,Number(t.removed_at)||0));}
      for(const e of list.entries){
        if(!e||!e.community_id||!(e.current||e.seed))continue;
        /* The first released 13302 writers stored only `seed`; `current` was added for instant
           latest-epoch recovery. The protocol defines a missing current snapshot as the seed, and
           the CORD reader already accepts that material. Rejecting it here hid every such joined
           Vector room before validation or hydration had a chance to run. */
        const compatible=e.current?e:{...e,current:e.seed};
        const old=entries.get(e.community_id);if(!old||Number(e.added_at||0)>Number(old.added_at||0))entries.set(e.community_id,compatible);
      }
      const live=[...entries.values()].filter(e=>Number(e.added_at||0)>Number(tombs.get(e.community_id)||0)); if(!live.length)return;
      const rooms=saved(); let changed=false;
      for(const e of live){
        if(state.community!=null&&!localOnly)return;
        const current=e.current||{},url=inviteRefUrl(e.invite_ref),
              i=rooms.findIndex(r=>r.communityId===e.community_id||r.url===url);
        // Armada's vault `current` is a CONTROL SNAPSHOT (owner/root/relays/name), not the complete
        // join bundle. Passing it to inspectControl rejects with "invalid Concord join material";
        // the catch used to `continue`, silently hiding every otherwise valid Armada membership on
        // a fresh device. Existing hydrated rooms need no work. For a missing room, resolve the
        // invite_ref exactly as Armada does and use the bundle carried by that invite.
        if(i>=0&&rooms[i].cord&&!rooms[i].cord.armadaList)continue;
        const existing=i>=0?rooms[i]:null,priorBundle=existing&&existing.cord&&existing.cord.bundle;
        let hydrated=null,bundle=mergeArmadaBundle(priorBundle,current);
        try{
          if(!window.PosterCordReader||!window.PosterCordReader.inspectControl)continue;
          window.PosterCordReader.inspectControl(bundle,[]);
        }catch(_){
          if(!url||localOnly)continue;
          try{hydrated=await hydrateInvite(p,url);if(state.community!=null&&!localOnly)return;bundle=mergeArmadaBundle(hydrated.cord.bundle,current);}
          catch(__){continue;}
        }
        const channels=(bundle.channels||[]).map(c=>({name:c.name||'private',private:true,id:c.id}))
          .filter(c=>c.name!=='general');
        const derivedChannels=[{name:'general',private:false},...channels],bundleUnchanged=!!priorBundle&&sameJson(priorBundle,bundle);
        const room={...(existing||{}),...(hydrated||{}),communityId:e.community_id,
          name:current.name||(existing&&existing.name)||(hydrated&&hydrated.name)||'Concord community',
          description:(existing&&existing.description)||(hydrated&&hydrated.description)||'',
          channels:channels.length?derivedChannels:(existing&&existing.channels||derivedChannels),local:false,
          naddr:url?(inviteParts(url)||{}).naddr:(existing&&existing.naddr)||'community-'+e.community_id,
          url:url||(existing&&existing.url)||'',
          cord:{...(existing&&existing.cord||{}),...(hydrated&&hydrated.cord||{}),bundle,armadaList:true,
            hydrated:!!(existing&&existing.cord&&existing.cord.hydrated&&bundleUnchanged)}};
        if(i<0){rooms.push(room);changed=true;}
        else if((!rooms[i].cord||rooms[i].cord.armadaList)&&!sameJson(rooms[i],room)){rooms[i]=room;changed=true;}
      }
      if(changed){save(rooms);backgroundRender();}
    }catch(e){ console.warn('Concord membership sync failed',e); }
    finally{
      membershipBusy=false;
      // Native desktop starts with an empty origin and relays may not be connected on first paint.
      // Retry a failed/empty recovery instead of permanently hiding the user's Armada rooms.
      clearTimeout(membershipRetryTimer); if(state.community==null)membershipRetryTimer=setTimeout(()=>syncArmadaMemberships(p,p.viewer?p.viewer():viewer),recovered?60000:120000);
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
  async function hydrateRoomStreams(p,index,expectedIdentity=''){
    const rooms=saved(),room=rooms[index],reader=window.PosterCordReader,bundle=room&&room.cord&&room.cord.bundle;
    if(!room||!bundle||!reader)return;
    const loadKey=room.communityId||room.naddr,identity=roomIdentity(room);
    if(roomLoads.has(loadKey))return roomLoads.get(loadKey);
    const job=(async()=>{
      /* Membership refresh and Leave can finish while relay history is in flight. Persist by room
       * identity, never the numeric array slot captured before an await, or a slow response can
       * overwrite another community or resurrect one the user left. */
      const persistRoom=()=>{const latest=saved(),at=latest.findIndex(item=>roomIdentity(item)===identity);if(at<0)return false;latest[at]={...latest[at],...room,cord:{...(latest[at].cord||{}),...(room.cord||{})}};save(latest);return true;};
      const seed=reader.inspectControl(bundle,[]), relays=roomRelays(bundle);
      const controlKey=envelopeCacheKey(loadKey,'control');
      let controlWraps=await cachedEnvelopes(controlKey);
      const applyControl=wraps=>{const info=reader.inspectControl(bundle,wraps||[]);roomControls.set(loadKey,wraps||[]);room.name=info.name||room.name;room.description=info.description||room.description;room.banned=Array.isArray(info.banned)?info.banned:room.banned||[];/* An encrypted icon can require an IndexedDB read, a remote download, AES-GCM and hashing. It is decoration, so never hold the cached channel list or first history paint behind it. Plain/cleared icons still mutate synchronously before this promise yields. Persist and repaint the icon when its bounded job finishes. */void applyRoomIconMetadata(room,info,loadKey).then(changed=>{if(!changed)return;if(!persistRoom())return;const active=saved()[state.community];if(roomIdentity(active)===identity)backgroundRender();});const channels=(info.channels||[]).map(c=>({id:c.id,name:c.name,private:!!c.private,streamPubkeys:c.streamPubkeys})).filter(c=>c.name);if(channels.length)room.channels=channels;for(const channel of room.channels||[])markRemoteStore(channelStoreId(room,channel.name));persistRoom();return channels.length;};
      const applyChannel=async(channel,wraps)=>{
        const opened=await reader.inspectChat(bundle,controlWraps||[],channel.id,wraps||[]);
        const reactions=new Map(opened.reactions||[]),reactionIds=new Map(opened.reactionIds||[]),reactionUrls=new Map(opened.reactionUrls||[]),msgs=(opened.messages||[]).map(m=>{ const pr=p.profOf?p.profOf(m.pubkey):{},rs={},ri={},ru={}; for(const [emoji,people] of reactions.get(m.id)||[])rs[emoji]=people;for(const [emoji,entries] of reactionIds.get(m.id)||[])ri[emoji]=Object.fromEntries(entries);for(const [emoji,url] of reactionUrls.get(m.id)||[])ru[emoji]=url; return {id:m.id,pubkey:m.pubkey,by:pr.display_name||pr.name||m.pubkey.slice(0,12)+'…',text:m.text,at:m.at,kind:m.kind,tags:m.tags||[],reactions:rs,reactionIds:ri,reactionUrls:ru,remote:true}; });
        const msgById=new Map(msgs.map(m=>[m.id,m])); for(const m of msgs){if(m.kind!==1111)continue;const parentId=((m.tags||[]).find(t=>t[0]==='e')||[])[1],parent=msgById.get(parentId);if(parent)m.reply={id:parent.id,by:parent.by,text:parent.text};}
        const storeId=channelStoreId(room,channel.name);markRemoteStore(storeId);const prior=testMessages(storeId);
        saveTestMessages(storeId,mergeRelayMessages(prior,msgs).sort((a,b)=>Number(a.at)-Number(b.at)));
      };
      /* Paint the encrypted on-device copy before opening sockets. This is the offline/reload path,
       * and also prevents relay latency from looking like an empty room. */
      let cachedChannelCount=0,cachedHistoryRendered=false;
      if(controlWraps.length){
        cachedChannelCount=applyControl(controlWraps);
        /* Control metadata is enough to replace the saved placeholder channel list. Paint it now,
         * before decrypting any chat page, and choose a channel that actually exists in that list.
         * A fresh renderer commonly starts at `general`; Armada rooms are not required to have one.
         * Keeping that missing name selected made a healthy cache paint an empty conversation until
         * the user left and re-entered the room. */
        const activeNow=saved()[state.community];
        if(roomIdentity(activeNow)===identity){
          const names=(room.channels||[]).map(channel=>channel.name),wanted=state.channel||'general';
          if(names.length&&!names.includes(wanted))state.channel=names[0];
          backgroundRender();
        }
        const selected=state.channel||((room.channels||[])[0]&&room.channels[0].name)||'general',ordered=[...(room.channels||[])].sort((a,b)=>(a.name===selected?-1:b.name===selected?1:0));
        /* Decrypt only the newest page first and paint the selected channel immediately. Loading
         * 5,000 envelopes for every channel before the first render made a healthy encrypted cache
         * look indistinguishable from a relay miss, and could exhaust an Android WebView renderer. */
        for(const channel of ordered){
          const wraps=await cachedEnvelopePage(envelopeCacheKey(loadKey,channel.id),300);
          if(wraps.length){await applyChannel(channel,wraps);if(channel.name===selected)cachedHistoryRendered=true;}
          if(channel.name===selected&&roomIdentity(saved()[state.community])===identity)backgroundRender();
        }
        if(roomIdentity(saved()[state.community])===identity)backgroundRender();
      }
      try{
      const completeControl=await queryEnvelopeHistory(p,relays,seed.controlPubkeys,controlWraps),fetchedControl=completeControl.filter(ev=>!controlWraps.some(old=>old.id===ev.id));
      controlWraps=completeControl;await cacheEnvelopes(controlKey,fetchedControl);
      const channelCount=applyControl(controlWraps);
      if(!channelCount)throw new Error('the control stream returned no readable channels');
      const selected=state.channel||'general',networkOrder=[...room.channels].sort((a,b)=>(a.name===selected?-1:b.name===selected?1:0));
      for(const channel of networkOrder){
        const cacheKey=envelopeCacheKey(loadKey,channel.id),cached=await cachedEnvelopes(cacheKey),wraps=await queryEnvelopeHistory(p,relays,channel.streamPubkeys,cached),fetched=wraps.filter(ev=>!cached.some(old=>old.id===ev.id));
        await cacheEnvelopes(cacheKey,fetched);await applyChannel(channel,wraps);
      }
      room.cord.hydrated=true;hydratedRoomViews.add(identity);if(!persistRoom())return;
      /* A relay answer may return after the reader chose another community. Persisting the fetched
       * room is still useful, but repainting/scrolling the new room is not. Notification launches
       * pass the durable identity explicitly because saved array positions can change meanwhile. */
      const currentRooms=saved(),current=state.community==null?null:currentRooms[state.community],stillSelected=roomIdentity(current)===identity&&
        (!expectedIdentity||roomIdentity(current)===expectedIdentity);
      /* Explicit room/channel handlers own the initial jump to latest. A network refresh finishing
       * later must preserve a reader who has deliberately scrolled up meanwhile. */
      if(stillSelected)backgroundRender();
      }catch(e){
        /* Cached control plus selected-channel history is a usable room, even when every live relay
         * path is temporarily down. Leave this renderer view unmarked so the next click retries the
         * network refresh, but do not turn a successful offline room open into a failure toaster. */
        if(cachedChannelCount&&cachedHistoryRendered){console.warn('Concord room refresh failed; using cached history',e);return;}
        throw e;
      }
    })().finally(()=>roomLoads.delete(loadKey)); roomLoads.set(loadKey,job); return job;
  }
  async function publishCordNative(p,room,channelName,text,extraTags=[],kind=9){
    const viewer=p.viewer?p.viewer():{},reader=window.PosterCordReader,bundle=room&&room.cord&&room.cord.bundle;
    if(!viewer.pubkey||!reader||!reader.createChatWrap||!bundle)throw new Error('CORD publishing is unavailable');
    const channel=(room.channels||[]).find(c=>c.name===channelName); if(!channel||!channel.id)throw new Error('channel key is unavailable');
    const loadKey=room.communityId||room.naddr,relays=roomRelays(bundle);
    let controlWraps=roomControls.get(loadKey);
    if(!controlWraps){ const seed=reader.inspectControl(bundle,[]),key=envelopeCacheKey(loadKey,'control'),cached=await cachedEnvelopes(key);controlWraps=await queryEnvelopeHistory(p,relays,seed.controlPubkeys,cached);await cacheEnvelopes(key,controlWraps.filter(ev=>!cached.some(old=>old.id===ev.id)));roomControls.set(loadKey,controlWraps||[]); }
    const made=await reader.createChatWrap(bundle,controlWraps||[],channel.id,text,viewer.pubkey,p.signTemplate,extraTags,kind);
    const accepted=await p.relayPublishTo(relays,made.wrap); if(!accepted)throw new Error('community relays rejected the message');
    await cacheEnvelopes(envelopeCacheKey(loadKey,channel.id),[made.wrap]);
    return made;
  }
  function nip29PreviousTags(messages,viewerPubkey){const ids=(messages||[]).filter(m=>m.remote&&!m.pending&&m.pubkey!==viewerPubkey).slice(-3).map(m=>messageId(m).slice(0,8));return ids.length?[['previous',...ids]]:[];}
  async function publishNip29Message(p,room,channelName,text,extraTags=[],kind=9){
    if(!p.publishNip29Authed||!room.relay||!room.groupId)throw new Error('authenticated NIP-29 publishing is unavailable');
    const viewer=p.viewer?p.viewer():{},tags=[['h',room.groupId],...nip29PreviousTags(testMessages(channelStoreId(room,channelName)),viewer.pubkey),...extraTags];
    const event=await p.publishNip29Authed(room.relay,{kind,created_at:Math.floor(Date.now()/1000),content:text,tags});return{...event,rumorId:event.id,ms:Number(event.created_at)*1000};
  }
  async function publishCordMessage(p,room,channelName,text,extraTags=[],kind=9){return room&&room.protocol==='nip29'?publishNip29Message(p,room,channelName,text,extraTags,kind):publishCordNative(p,room,channelName,text,extraTags,kind);}
  async function refreshActiveChannel(p){
    const foreground=document.body.classList.contains('concord-view'),parked=window.PCOS&&PCOS.isOn&&PCOS.isOn()&&PCOS.parkedSlot&&PCOS.parkedSlot('concord');
    if(liveBusy||state.community==null||(!foreground&&!parked))return; liveBusy=true;
    try{ const rooms=saved(),room=rooms[state.community],channel=room&&(room.channels||[]).find(c=>c.name===(state.channel||'general')),bundle=room&&room.cord&&room.cord.bundle,reader=window.PosterCordReader;if(!room||!channel||!bundle||!reader)return; const loadKey=room.communityId||room.naddr,controlWraps=roomControls.get(loadKey);if(!controlWraps)return; const relays=roomRelays(bundle),storeId=channelStoreId(room,channel.name),prior=testMessages(storeId),since=Math.max(0,Math.floor((prior.reduce((n,m)=>Math.max(n,Number(m.at)||0),0)-60000)/1000)),wraps=await cordQuery(p,relays,[{kinds:[1059],authors:channel.streamPubkeys,since,limit:500}],{timeout:6000,max:8,signal:ownRoomReads(roomIdentity(room)),purpose:'concord room live '+loadKey,minInterval:60000});await cacheEnvelopes(envelopeCacheKey(loadKey,channel.id),wraps);const opened=await reader.inspectChat(bundle,controlWraps,channel.id,wraps||[]),incoming=(opened.messages||[]).map(m=>{const pr=p.profOf?p.profOf(m.pubkey):{};return {id:m.id,pubkey:m.pubkey,by:pr.display_name||pr.name||m.pubkey.slice(0,12)+'…',text:m.text,at:m.at,kind:m.kind,tags:m.tags||[],reactions:{},remote:true};}),merged=mergeRelayMessages(prior,incoming),byId=new Map(merged.map(m=>[messageId(m),m])); let changed=JSON.stringify(merged)!==JSON.stringify(prior),urlGroups=new Map(opened.reactionUrls||[]); for(const [target,groups] of opened.reactions||[]){const m=byId.get(target);if(!m)continue;const next={},nextUrls={};for(const [emoji,people] of groups)next[emoji]=people;for(const [emoji,url] of urlGroups.get(target)||[])nextUrls[emoji]=url;if(JSON.stringify(m.reactions||{})!==JSON.stringify(next)||JSON.stringify(m.reactionUrls||{})!==JSON.stringify(nextUrls)){m.reactions=next;m.reactionUrls=nextUrls;changed=true;}} if(changed){const next=[...byId.values()].sort((a,b)=>Number(a.at)-Number(b.at)),viewer=p.viewer?p.viewer():{},profile=viewer.profile||{},me=profile.display_name||profile.name||(viewer.npub?viewer.npub.slice(0,12)+'…':'You');notifyMentions(p,room,next,viewer,me,channel.name);if(document.body.classList.contains('concord-view'))preserveChatScroll(()=>{saveTestMessages(storeId,next);backgroundRender();});else saveTestMessages(storeId,next);}
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
        seed=reader.inspectControl(bundle,[]),relays=roomRelays(bundle),
        cachedWraps=await cachedEnvelopes(envelopeCacheKey(loadKey,'control')),wraps=await queryEnvelopeHistory(p,relays,seed.controlPubkeys,cachedWraps,{signal:roomIdentity(room)===roomReadIdentity&&roomReadAbortController?roomReadAbortController.signal:null,purpose:'concord room metadata '+loadKey,minInterval:60000}),freshWraps=wraps.filter(ev=>!cachedWraps.some(old=>old.id===ev.id)),
        info=reader.inspectControl(bundle,wraps||[]);
      await cacheEnvelopes(envelopeCacheKey(loadKey,'control'),freshWraps);roomControls.set(loadKey,wraps||[]);
      let changed=false;
      const assign=(key,value)=>{if(value!==undefined&&JSON.stringify(room[key])!==JSON.stringify(value)){room[key]=value;changed=true;}};
      assign('name',info.name||room.name); assign('description',info.description===undefined?room.description:info.description);
      assign('banned',Array.isArray(info.banned)?info.banned:room.banned||[]);
      if(await applyRoomIconMetadata(room,info,loadKey))changed=true;
      const channels=(info.channels||[]).map(c=>({id:c.id,name:c.name,private:!!c.private,streamPubkeys:c.streamPubkeys})).filter(c=>c.name);
      if(channels.length)assign('channels',channels);
      if(changed){rooms[selected.index]=room;save(rooms);preserveChatScroll(()=>backgroundRender());}
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
  async function activateJoinedRoom(p,index,inDrawer=false,expectedIdentity=''){
    let rooms=saved();
    if(expectedIdentity){const currentIndex=rooms.findIndex(room=>roomIdentity(room)===expectedIdentity);if(currentIndex<0)return false;index=currentIndex;}
    let room=rooms[index];if(!room)return false;
    const identity=roomIdentity(room);
    discoveryOpen=false;localStorage.setItem('pc.concord.active',String(index));state.community=index;state.channel='general';mobileChatOpen=!!inDrawer;mobileDrawerOpen=!!inDrawer;
    render();enterChatBottom();
    try{
      if(room.url&&(!room.cord||!room.cord.bundle)){room={...room,...await hydrateInvite(p,room.url)};rooms=saved();const at=rooms.findIndex(item=>roomIdentity(item)===identity);if(at<0)return false;rooms[at]=room;save(rooms);index=at;if(roomIdentity(rooms[state.community])===identity){state.community=at;render();}}
      if(room.cord&&!hydratedRoomViews.has(roomIdentity(room)))await hydrateRoomStreams(p,index,identity);
      else if(room.protocol==='nip29'&&!room.nip29Hydrated)await hydrateNip29Room(p,index);
      roomLoadNotices.delete(roomIdentity(room));return true;
    }catch(e){
      if(room.protocol==='nip29'){rooms=saved();const at=rooms.findIndex(item=>roomIdentity(item)===identity);if(at>=0){rooms[at].nip29Hydrated=false;save(rooms);}}
      roomLoadWarning(p,identity,'could not refresh community: ',e);return false;
    }finally{
      const active=saved()[state.community];if(roomIdentity(active)===identity)enterChatBottom();
    }
  }
  async function resumeActiveRoom(p,identity){
    const rooms=saved(),index=rooms.findIndex(room=>roomIdentity(room)===identity),room=rooms[index];
    if(index<0||state.community==null||roomIdentity(rooms[state.community])!==identity)return;
    try{
      if(room.cord&&!hydratedRoomViews.has(identity))await hydrateRoomStreams(p,index,identity);
      else if(room.cord)await refreshActiveChannel(p);
      else if(room.protocol==='nip29'&&!room.nip29Hydrated)await hydrateNip29Room(p,index);
    }catch(e){roomLoadWarning(p,identity,'could not refresh community: ',e);}
  }
  function wake(){resumeRequested=true;}
  function render(){
    // An explicit/user render supersedes any coalesced background paint. A focusout listener from
    // the old workspace may still fire, but it observes false and cannot paint twice.
    backgroundRenderPending=false;backgroundFocusHost=null;
    const p=PC();
    /* Every async Concord path eventually calls render(). The feed belongs to the CURRENT app, not
     * to whichever request finished last. This guard protects Code and every other shared-feed view
     * from relay/discovery/deferred Concord work completing after navigation. */
    if(!p || typeof p.isView!=='function' || !p.isView('concord')) return;
    if(window.PCOS && PCOS.isOn && PCOS.isOn() &&
       (!PCOS.ownsFeedView || !PCOS.ownsFeedView('concord'))) return;
    const feed=p.$('#feed'); if(!feed) return;
    captureComposer();
    startLiveSync(p);
    // Covers the stale-service-worker compatibility entry too, which does not run switchView().
    document.body.classList.add('concord-view','rb-off');
    const rooms=saved();
    const viewer=p.viewer?p.viewer():{};
    let autoOpen=-1;
    if(state.community==null&&rooms.length&&!discoveryOpen){
      const wanted=Number(localStorage.getItem('pc.concord.active')||0);
      state.community=Number.isInteger(wanted)&&wanted>=0&&wanted<rooms.length?wanted:0;
      state.channel='general';
      autoOpen=state.community;
    }
    /* Discovery/membership fan-out is allowed only while the Discover surface is visible. Resolve
     * the saved active room first so an ordinary launch never opens Ditto/Vector sockets for a
     * transient state.community=null frame. */
    if(state.community==null){
      ownRoomReads('');
      startDiscovery(p);syncArmadaMemberships(p,viewer);syncNip29Memberships(p,viewer);
    }else{
      /* Restore every membership already cached or present on PosterChan without opening any public
       * discovery relay. This is what makes all joined rooms appear on an ordinary launch. */
      /* One bounded cross-relay recovery is required even with a selected room: membership shards
         are distributed and the local relay may hold only the newest subset. This is not public
         listing discovery and schedules no active-room retry. */
      syncArmadaMemberships(p,viewer,'recovery');syncNip29Memberships(p,viewer,true);
      stopDiscovery();
      clearTimeout(membershipRetryTimer);membershipRetryTimer=null;
      clearTimeout(nip29RetryTimer);nip29RetryTimer=null;
    }
    const profile=viewer.profile||{};
    const me=profile.display_name||profile.name||(profile.nip05&&p.niceNip05(profile.nip05))||(viewer.npub?viewer.npub.slice(0,12)+'…':'You');
    const current=state.community==null?null:rooms[state.community];
    if(current)ownRoomReads(roomIdentity(current));
    const visibleChannels=current?orderedChannels(current):[];
    if(current&&visibleChannels.length&&!visibleChannels.some(c=>c.name===(state.channel||'general')))state.channel=visibleChannels[0].name;
    /* On phones a selected community does not mean its conversation is visible: the initial
     * workspace and the open drawer show rooms/channels instead. Relay and metadata repaints used
     * to call markRead here anyway, erasing the bold unread marker before the person opened the
     * channel. Desktop always shows the conversation; mobile marks it only after the drawer closes. */
    const narrow=!!(window.matchMedia&&window.matchMedia('(max-width:820px)').matches);
    if(current&&conversationIsVisible(narrow,mobileChatOpen,mobileDrawerOpen))markRead(current,state.channel||'general');
    const currentChannel=current?visibleChannels.find(c=>c.name===(state.channel||'general')):null;
    const draftKey=composerKey(current,state.channel),draft=composerDrafts.get(draftKey)||null;
    replyTarget=draft&&draft.replyTarget||null;
    activeMentionState=draft?{choices:[...(draft.mentionChoices||[])],index:Number(draft.mentionIndex)||0,
      recipients:new Map(draft.mentionRecipients||[])}:{choices:[],index:0,recipients:new Map()};
    const channelPrivate=!!(currentChannel&&currentChannel.private);
    const messages=current&&(current.local||current.cord||current.protocol==='nip29')?paintedMessages(current):[];
    const joinedRooms=''; // Active communities use the server rail/channel navigator, not home-page cards.
    const ownerPk=String((current&&current.cord&&current.cord.bundle&&(current.cord.bundle.owner||current.cord.bundle.creator_npub))||''),
      isOwner=!!ownerPk&&ownerPk===viewer.pubkey,banned=new Set(current&&current.banned||[]),
      memberPks=current?roomParticipants(current,viewer.pubkey).filter(pk=>!banned.has(pk)):[];
    let membersHidden=localStorage.getItem('pc.concord.members.hidden')==='1';
    const memberRows=memberPks.map(pk=>{const pr=p.profOf?p.profOf(pk):{},name=pk===viewer.pubkey?me:(pr.display_name||pr.name||pk.slice(0,12)+'…');return `<button class="cc-member" data-cc-member="${p.enc(pk)}" aria-label="${p.enc(name)} — ${pk===ownerPk?'Owner':'Member'}"><img src="${p.enc(pr.picture||p.LOGO||'')}" alt=""><span><b>${p.enc(name)}</b><small>${pk===ownerPk?'Owner':'Member'}</small></span></button>`;}).join('');
    notifyMentions(p,current,messages,viewer,me,state.channel||'general');
    feed.innerHTML=`<div class="cc-app${mobileChatOpen||state.community==null?' show-chat':''}${mobileDrawerOpen?' drawer-open':''}${state.community==null?' home-view':''}">
      <button class="cc-drawer-backdrop" id="cc-drawer-backdrop" aria-label="Close rooms and channels"></button>
      <aside class="cc-communities"><button class="cc-brand" id="cc-home" title="Your rooms" aria-label="Your rooms"><span aria-hidden="true">🕊</span></button><button class="cc-server cc-discovery-button" id="cc-discovery" title="Discover public communities" aria-label="Discover public communities">◎</button>${rooms.map((r,i)=>`<button class="cc-server${state.community===i?' active':''}${isUnread(r)?' unread':''}" data-cc-server="${i}" title="${p.enc(roomName(r,i))}">${roomIcon(p,r,i)}</button>`).join('')}<button class="cc-server cc-add" id="cc-add" title="Join a community">+</button></aside>
      <aside class="cc-channels"><header><button class="cc-mobile-back" id="cc-back-communities" aria-label="Communities">‹</button><div><b>${state.community==null?'Concord':p.enc(roomName(current,state.community))}</b><small>${current&&current.local?'Local test community':'End-to-end encrypted'}</small></div>${current?'<button class="cc-head-btn" id="cc-edit-icon" title="Set community icon" aria-label="Set community icon"><svg class="ic"><use href="#i-image"></use></svg></button>':''}<button class="cc-head-btn" id="cc-invite" title="Join with invite">+</button></header>
        <div class="cc-channel-list">${state.community==null?'<div class="cc-empty-side">Choose or join a community</div>':channelSectionsHtml(p,current,visibleChannels)}</div>
        <footer class="cc-identity"><span class="cc-status"></span><div><b>${p.enc(me)}</b><small>You</small></div><button class="cc-head-btn" id="cc-notify" title="Notification settings"><svg class="ic"><use href="#i-bell"></use></svg></button></footer>
      </aside>
      <main class="cc-conversation"><header><button class="cc-mobile-back" id="cc-back-channels" aria-label="${state.community==null?'Back to rooms':'Rooms and channels'}">${state.community==null?'‹':'☰'}</button><span class="cc-hash">#</span><b>${state.community==null?'Communities':state.channel||'general'}</b><span class="cc-visibility ${channelPrivate?'private':'public'}">${channelPrivate?'Private':'Public'}</span><span class="cc-topic">${p.enc((current&&current.description)||(channelPrivate?'Invite-only channel':'Visible to all community members'))}</span><span class="cc-spacer"></span>${current?'<button class="cc-head-btn" id="cc-publish-listing" title="Publish to Armada Discover" aria-label="Publish to Armada Discover"><svg class="ic"><use href="#i-share"></use></svg></button><button class="cc-head-btn" id="cc-copy-link" title="Copy room invite link" aria-label="Copy room invite link"><svg class="ic"><use href="#i-link"></use></svg></button><button class="cc-head-btn" id="cc-call" title="Start voice call"><svg class="ic"><use href="#i-phone"></use></svg></button>':''}<button class="cc-head-btn" id="cc-members" title="Members"><svg class="ic"><use href="#i-users"></use></svg></button></header>
        <div class="cc-messages">${state.community==null?`<div class="cc-discover"><div class="concord-mark">C</div><h2>Find your community</h2><p>Join an Armada-compatible CORD-05 invite or create a public relay community.</p><div class="cc-primary-actions"><button class="btn btn-neon" id="cc-create">Create community</button><button class="btn btn-ghost" id="cc-welcome-join">Join with invite</button></div>${joinedRooms}<section class="cc-public"><div><h3>Public communities</h3><small>Public CORD invites discovered on Armada relays</small></div>${discovered.length?discovered.map((r,i)=>{const pr=p.profOf?p.profOf(r.source.pubkey):{};return `<button data-cc-discover="${i}" class="cc-public-room"><span class="cc-public-icon">${publicRoomIcon(p,r)}</span><span class="cc-public-copy"><b>${p.enc(r.name)}</b><small>${p.enc((r.description||'Public Concord community').slice(0,120))}</small><em>${p.enc(pr.name||pr.display_name||'Nostr community')}</em></span><strong>Join</strong></button>`;}).join(''):(discoveryLoaded?'<div class="cc-public-empty"><b>No public communities found</b><span>Publish or paste a public Armada/CORD invite to list it.</span></div>':'<div class="cc-public-empty"><b>Searching relays…</b><span>Looking for public Armada/CORD invite notes.</span></div>')}</section></div>`:(messages.length?`<div class="cc-message-list">${messages.map(m=>{const mp=p.profOf?p.profOf(m.pubkey):{},mid=messageId(m);return `<article class="cc-message" data-message-id="${p.enc(mid)}"><img class="cc-message-avatar" src="${p.enc(mp.picture||p.LOGO||'')}" alt=""><div class="cc-message-body">${m.reply?`<div class="cc-message-reply"><b>@${p.enc(m.reply.by||'member')}</b> ${p.enc(String(m.reply.text||'').slice(0,100))}</div>`:''}<b>${p.enc(m.by)}</b><time>${new Date(m.at).toLocaleTimeString([],{hour:'2-digit',minute:'2-digit'})}</time>${messageContentHtml(p,m,current,state.channel)}<div class="cc-reactions">${reactionSummary(p,m)}</div><div class="cc-message-actions" role="toolbar" aria-label="Message actions"><button class="cc-action-trigger" data-cc-actions="${p.enc(mid)}" aria-expanded="false" title="Message actions">⋯</button><button data-cc-react="${p.enc(mid)}" title="Add reaction">☺</button><button data-cc-reply="${p.enc(mid)}" title="Reply">↩</button><button data-cc-delete="${p.enc(mid)}" class="cc-delete-action ${m.pubkey&&m.pubkey===viewer.pubkey?'':'hidden'}" title="Delete message">⌫</button></div></div></article>`;}).join('')}</div>`:`<div class="cc-welcome"><div class="cc-welcome-hash">#</div><h2>Welcome to #${state.channel||'general'}</h2><p>${current&&current.local?'This local test room lets you validate the chat UI before publishing or joining a relay community.':'This is the start of this encrypted channel.'}</p></div>`)}</div>
        <div class="cc-reply${replyTarget?'':' hidden'}" id="cc-reply">${replyTarget?`<span>Replying to <b>${p.enc(replyTarget.by||'member')}</b>: ${p.enc(String(replyTarget.text||'').slice(0,90))}</span><button id="cc-reply-cancel" aria-label="Cancel reply">×</button>`:''}</div><div class="cc-compose"><button class="cc-compose-btn" id="cc-attach" title="Attach file"><svg class="ic"><use href="#i-paperclip"></use></svg></button><input type="file" id="cc-file" multiple hidden><textarea id="cc-input" data-cc-draft-key="${p.enc(draftKey)}" rows="1" placeholder="Message #${state.channel||'general'}" ${state.community==null?'disabled':''}>${p.enc(draft&&draft.value||'')}</textarea><button class="cc-compose-btn" id="cc-emoji" title="Emoji"><svg class="ic"><use href="#i-smile"></use></svg></button><button class="btn btn-neon" id="cc-send" ${state.community==null?'disabled':''}>Send</button></div>
      </main></div><div class="cc-join hidden" id="cc-join"><div class="cc-join-card"><div class="concord-mark">C</div><h2>Join a Concord community</h2><p class="muted">Paste an Armada or other CORD-05 invite. Its # secret stays in this browser.</p><input class="input" id="cc-invite-url" inputmode="url" autocomplete="off" autocapitalize="none" spellcheck="false" placeholder="https://…/invite/naddr1…#…"><div class="cc-join-actions"><button class="btn btn-ghost" id="cc-join-cancel">Cancel</button><button class="btn btn-neon" id="cc-join-go">Preview invite</button></div></div></div><div class="cc-join hidden" id="cc-create-dialog"><div class="cc-join-card"><div class="concord-mark">C</div><h2>Create a public community</h2><p class="muted">Publishes an Armada-compatible CORD community and public #general channel to your relays.</p><label class="cc-label" for="cc-community-name">Community name</label><input class="input" id="cc-community-name" maxlength="64" autocomplete="off" placeholder="My community"><label class="cc-label" for="cc-community-icon">Icon <span class="muted">(emoji or image URL)</span></label><input class="input" id="cc-community-icon" maxlength="2048" autocomplete="off" placeholder="🚀 or https://…/icon.png"><div class="cc-join-actions"><button class="btn btn-ghost" id="cc-create-cancel">Cancel</button><button class="btn btn-neon" id="cc-create-go">Create on relays</button></div></div></div><div class="cc-join hidden" id="cc-icon-dialog"><div class="cc-join-card"><div class="concord-mark">C</div><h2>Community icon</h2><p class="muted">Use an emoji or a direct HTTP(S) image URL. Leave blank to restore the initials.</p><label class="cc-label" for="cc-icon-value">Icon</label><input class="input" id="cc-icon-value" maxlength="2048" autocomplete="off" placeholder="🌌 or https://…/icon.png"><div class="cc-join-actions"><button class="btn btn-ghost" id="cc-icon-cancel">Cancel</button><button class="btn btn-neon" id="cc-icon-save">Save icon</button></div></div></div>`;
    feed.insertAdjacentHTML('afterbegin','<nav class="messages-tabs" aria-label="Message type"><button id="messages-direct">Direct messages</button><button class="on" aria-current="page">Communities</button></nav>');
    const directMessages=p.$('#messages-direct');if(directMessages)directMessages.onclick=()=>
      (p.switchMessagesTab||p.switchView)('messages');
    if(current){
      const conversation=p.$('.cc-conversation');
      if(conversation&&conversation.insertAdjacentHTML)conversation.insertAdjacentHTML('afterend',`<aside class="cc-members-pane${membersHidden?' hidden':''}" aria-label="Community members"><header><b>Members</b><span>${memberPks.length}</span></header><div class="cc-members-scroll">${memberRows||'<div class="cc-empty-side">No members have appeared yet.</div>'}</div></aside>`);
      feed.insertAdjacentHTML('beforeend',`<div class="cc-join hidden" id="cc-members-dialog"><div class="cc-join-card"><h2>Members <span class="muted">${memberPks.length}</span></h2><p class="cc-member-help">Tap a member to view their profile. Right-click or hold for more options.</p><div class="cc-member-list">${memberRows}</div><div class="cc-join-actions"><button class="btn btn-ghost" id="cc-members-close">Close</button><button class="btn btn-neon" id="cc-members-invite">Invite people</button></div></div></div><div class="cc-join hidden" id="cc-settings-dialog"><div class="cc-join-card"><h2>Community settings</h2><label class="cc-label" for="cc-description-value">Description</label><textarea class="input cc-settings-description" id="cc-description-value" maxlength="1000" rows="3" placeholder="What is this community about?">${p.enc(current.description||'')}</textarea><label class="cc-label" for="cc-settings-icon">Icon</label><input class="input" id="cc-settings-icon" maxlength="2048" value="${p.enc(current.icon||'')}" placeholder="🌌 or https://…/icon.png"><label class="cc-label" for="cc-channel-visibility">#${p.enc(state.channel||'general')} visibility</label><select class="input cc-visibility-select" id="cc-channel-visibility"><option value="public"${channelPrivate?'':' selected'}>Public — all community members</option><option value="private"${channelPrivate?' selected':''}>Private — invited members only</option></select><div class="cc-join-actions"><button class="btn btn-ghost" id="cc-settings-cancel">Cancel</button><button class="btn btn-neon" id="cc-settings-save">Save changes</button></div></div></div>`);
      const settingsActions=p.$('#cc-settings-dialog .cc-join-actions');
      if(settingsActions&&settingsActions.insertAdjacentHTML)settingsActions.insertAdjacentHTML('afterbegin','<button class="btn btn-ghost danger" id="cc-leave-community">Leave community</button>');
    }
    if(p.hydrateLinkCards)p.hydrateLinkCards(feed);
    wireRoomMedia(p);
    hydrateEncryptedAttachments(messages);
    hydrateWebxdcCards(current);
    bind(me);
    if(draft){const input=p.$('#cc-input');if(input){const end=String(input.value||'').length,start=Math.max(0,Math.min(Number(draft.start)||0,end)),finish=Math.max(start,Math.min(Number(draft.end)||start,end));try{input.setSelectionRange(start,finish,draft.direction||'none');}catch(_){}if(draft.focused){const later=window.requestAnimationFrame||((fn)=>setTimeout(fn,0));later(()=>{if(input.isConnected!==false&&(!document.activeElement||document.activeElement===document.body||document.activeElement===document.documentElement))input.focus({preventScroll:true});});}}}
    if(autoOpen>=0){const identity=roomIdentity(rooms[autoOpen]);setTimeout(()=>{void activateJoinedRoom(p,autoOpen,false,identity);},0);}
    /* render() replaces the scroller on EVERY relay/history/metadata repaint, not only when the app
     * first returns to the feed. Restore after every replacement: pinned rooms land at the newest
     * message; a person who deliberately scrolled up keeps that saved offset. The old `returning`
     * gate is why entering a room briefly reached the bottom and then jumped into the middle when
     * its asynchronous history hydration rendered again. */
    if(current){restoreChatScroll();if(resumeRequested){resumeRequested=false;const identity=roomIdentity(current);setTimeout(()=>{void resumeActiveRoom(p,identity);},0);}}
  }
  /* A notification identifies content, not merely the Concord application. Select with durable
   * room identity (saved array positions change), paint the named channel, then keep looking while
   * relay history hydrates. A missing target leaves the requested room/channel open; it must never
   * silently jump to some other community. */
  function openNotification(target){
    const t=target&&typeof target==='object'?target:{},rooms=saved(),identity=String(t.community||''),
      index=rooms.findIndex(room=>roomIdentity(room)===identity);
    if(index<0)return false;
    const requested=String(t.channel||'general').slice(0,80),channels=channelsOf(rooms[index]);
    state.community=index;
    state.channel=channels.some(c=>c.name===requested)?requested:(channels[0]&&channels[0].name)||'general';
    mobileChatOpen=true;mobileDrawerOpen=false;discoveryOpen=false;
    localStorage.setItem('pc.concord.active',String(index));
    render();
    const id=String(t.message||'');
    if(!id)return true;
    let tries=0;
    const reveal=()=>{
      if(state.community!==index||state.channel!==(channels.some(c=>c.name===requested)?requested:state.channel))return;
      const row=[...document.querySelectorAll('.cc-message[data-message-id]')]
        .find(el=>el.dataset&&el.dataset.messageId===id);
      if(row){const st=readScroll(scrollKey());st.pinned=false;row.scrollIntoView({block:'center'});row.classList.add('cc-message-target');setTimeout(()=>{if(row.isConnected)row.classList.remove('cc-message-target');},2400);return;}
      if(++tries<50)setTimeout(reveal,100);
    };
    reveal();
    /* Polling the DOM cannot create history. A cold/background launch commonly has only the saved
     * encrypted bundle, so follow the same invite/control/chat hydration path as a real room click.
     * Keep the requested channel across the metadata repaint and never let a late result take over
     * after the reader selected another community. */
    if(rooms[index]&&rooms[index].cord&&!rooms[index].cord.hydrated){
      (async()=>{try{
        await hydrateRoomStreams(PC(),index,identity);
        if(state.community!==index||roomIdentity(saved()[index])!==identity)return;
        const fresh=saved()[index],available=channelsOf(fresh);
        if(available.some(c=>c.name===requested))state.channel=requested;
        render();reveal();
      }catch(e){console.warn('Concord notification room hydration failed',e);}})();
    }
    return true;
  }
  function bind(me){
    const p=PC(), $=p.$, $$=p.$$;
    /* bind() is a sibling of render(), not its closure. Member actions must derive their own
     * viewer/owner state here; referring to render's `isOwner` made right-click throw immediately. */
    const viewer=p.viewer?p.viewer():{},boundRoom=saved()[state.community],
      boundOwnerPk=String((boundRoom&&boundRoom.cord&&boundRoom.cord.bundle&&
        (boundRoom.cord.bundle.owner||boundRoom.cord.bundle.creator_npub))||''),
      isOwner=!!boundOwnerPk&&boundOwnerPk===viewer.pubkey;
    const scroller=document.querySelector('.cc-messages'); if(scroller){ scroller.onscroll=()=>{ if(scroller.dataset.osParking||scroller.dataset.ccScrollRestore||!scroller.isConnected||!document.body.classList.contains('concord-view'))return; const key=scrollKey(),st=readScroll(key); st.top=scroller.scrollTop;st.height=scroller.scrollHeight;st.pinned=scroller.scrollHeight-scroller.scrollTop-scroller.clientHeight<80;writeScroll(key,st); }; scroller.querySelectorAll('a').forEach(a=>a.addEventListener('pointerdown',()=>{ const key=scrollKey(),st=readScroll(key); st.top=scroller.scrollTop;st.height=scroller.scrollHeight;st.pinned=scroller.scrollHeight-scroller.scrollTop-scroller.clientHeight<80;writeScroll(key,st); },{passive:true})); scroller.addEventListener('click',e=>{const a=e.target&&e.target.closest&&e.target.closest('a[href]');if(!a||!inviteParts(a.href))return;e.preventDefault();e.stopPropagation();openInviteLink(a.href,true);},true);watchPinnedRoomGrowth(scroller); }
    const openJoin=()=>{ $('#cc-join').classList.remove('hidden'); setTimeout(()=>$('#cc-invite-url').focus(),20); };
    const home=$('#cc-home'); if(home)home.onclick=()=>{ const rooms=saved(),wanted=Number(localStorage.getItem('pc.concord.active')||0); discoveryOpen=!rooms.length; state.community=rooms.length&&wanted>=0&&wanted<rooms.length?wanted:(rooms.length?0:null); state.channel=state.community==null?null:'general'; mobileChatOpen=false; mobileDrawerOpen=false; render(); };
    const discovery=$('#cc-discovery'); if(discovery)discovery.onclick=()=>{ discoveryOpen=true; state.community=null; state.channel=null; mobileChatOpen=false; mobileDrawerOpen=false; render(); };
    ['#cc-add','#cc-invite','#cc-welcome-join'].forEach(s=>{ const b=$(s); if(b)b.onclick=openJoin; });
    const roomInvite=$('#cc-invite');if(roomInvite&&state.community!=null){roomInvite.title='Invite people';roomInvite.setAttribute&&roomInvite.setAttribute('aria-label','Invite people');roomInvite.onclick=()=>{const room=saved()[state.community];if(room&&room.url)p.copyValue(room.url);else $('#cc-join').classList.remove('hidden');};}
    const create=$('#cc-create'); if(create)create.onclick=()=>{ $('#cc-create-dialog').classList.remove('hidden'); setTimeout(()=>$('#cc-community-name').focus(),20); };
    const createCancel=$('#cc-create-cancel'); if(createCancel)createCancel.onclick=()=>$('#cc-create-dialog').classList.add('hidden');
    const createGo=$('#cc-create-go'); if(createGo)createGo.onclick=async()=>{ const name=String($('#cc-community-name').value||'').trim(); if(!name){ p.toast('name your community'); return; } createGo.disabled=true; try{ p.toast('creating encrypted community…'); const room=await mintPublicRoom(p,name,normalizeIcon($('#cc-community-icon').value)); const a=saved(); a.push(room); save(a);/* The creator already has the freshly generated control/channel state. Treat this renderer as hydrated so the first paint is not delayed by reading the just-published room back from relays. */hydratedRoomViews.add(roomIdentity(room));state.community=a.length-1; state.channel='general'; render(); await persistArmadaMembership(p,room); p.copyValue(room.url); p.toast('public community created — invite link copied'); }catch(e){ createGo.disabled=false; p.toast('community creation failed: '+(e&&e.message||e)); } };
    const editIcon=$('#cc-edit-icon'); if(editIcon)editIcon.onclick=()=>{ $('#cc-settings-dialog').classList.remove('hidden'); setTimeout(()=>$('#cc-description-value').focus(),20); };
    const iconCancel=$('#cc-icon-cancel'); if(iconCancel)iconCancel.onclick=()=>$('#cc-icon-dialog').classList.add('hidden');
    /* The compact icon dialog used to mutate only this renderer's localStorage and immediately say
       "updated". The next control-stream hydration correctly restored relay metadata, so the icon
       appeared to randomly disappear (and another device never saw it at all). Funnel both icon
       entry points through the authoritative Community settings publisher. */
    const iconSave=$('#cc-icon-save'); if(iconSave)iconSave.onclick=()=>{ const target=$('#cc-settings-icon'),saveButton=$('#cc-settings-save'); if(!target||!saveButton)return; target.value=normalizeIcon($('#cc-icon-value').value); $('#cc-icon-dialog').classList.add('hidden'); return saveButton.click(); };
    const emoji=$('#cc-emoji'), input=$('#cc-input'); if(emoji&&input)emoji.onclick=()=>{ if(p.openEmojiPopover)p.openEmojiPopover(emoji,(value,close)=>{ if(close)close(); if(p.insertAt)p.insertAt(input,value); else input.value+=value; input.focus(); }); };
    let mentionChoices=[...(activeMentionState.choices||[])],mentionIndex=Number(activeMentionState.index)||0;
    const mentionRecipients=new Map(activeMentionState.recipients||[]);
    const syncMentionState=()=>{activeMentionState={choices:[...mentionChoices],index:mentionIndex,recipients:new Map(mentionRecipients)};};
    const closeMentions=()=>{ mentionChoices=[];syncMentionState(); };
    const mentionToken=()=>{ const before=input.value.slice(0,input.selectionStart); return before.match(/(?:^|\s)@([\w.-]*)$/); };
    const drawMentions=()=>{ const match=mentionToken(); if(!match){closeMentions();return;} const room=saved()[state.community],viewer=p.viewer?p.viewer():{},pks=roomParticipants(room,viewer.pubkey),q=match[1].toLowerCase(); mentionChoices=pks.map(pk=>{const pr=p.profOf?p.profOf(pk):{},name=String(pr.display_name||pr.name||(pk===viewer.pubkey?me:pk.slice(0,12)));return {pk,name,aliases:mentionAliases(pr,pk,name)};}).filter(x=>!q||[...x.aliases].some(alias=>alias.includes(q))).slice(0,8); if(!mentionChoices.length){closeMentions();return;} mentionIndex=Math.min(mentionIndex,mentionChoices.length-1);syncMentionState(); };
    const acceptMention=(i=mentionIndex)=>{ const match=mentionToken(),choice=mentionChoices[i]; if(!match||!choice)return false; const handle=choice.name.replace(/\s+/g,'_'),end=input.selectionStart,start=end-match[1].length-1;mentionRecipients.set(handle.toLowerCase(),choice.pk);input.setRangeText('@'+handle+' ',start,end,'end'); closeMentions();syncMentionState(); input.focus(); return true; };
    if(input&&input.addEventListener)input.addEventListener('input',drawMentions);
    const attach=$('#cc-attach'), file=$('#cc-file');
    const insertBlossomAttachment=({url,type,ext})=>{ if(!url||!input)return; const mime=String(type||'application/octet-stream'),raw=String(url).split(/[?#]/)[0].split('/').pop()||'file',name=raw+(ext&&!raw.includes('.')?'.'+ext:''); let tag=['imeta',`url ${url}`,`m ${mime}`,`name ${name.slice(0,120)}`]; if(mime==='application/x-webxdc'||mime==='application/webxdc+zip'||mime==='application/vnd.webxdc+zip'){const topic=mintWebxdcTopic();tag.push(`webxdc-topic ${topic}`,`webxdc ${topic}`,`summary ${name.replace(/\.xdc$/i,'').slice(0,80)}`);} pendingAttachments.set(url,tag); input.value+=(input.value&&!/\s$/.test(input.value)?' ':'')+url; input.dispatchEvent(new Event('input',{bubbles:true})); input.focus(); };
    if(attach&&file)attach.onclick=()=>{ if(!p.blossomPicker||!p.modal){file.click();return;} p.modal(`<h3>Attach to #${p.enc(state.channel||'general')}</h3><p class="muted">Choose a new file from this device or reuse one from Files.</p><div class="cc-attach-choices"><button class="btn btn-ghost" id="cc-attach-device">From device</button><button class="btn btn-neon" id="cc-attach-blossom">📁 Files</button></div>`,root=>{ const local=root.querySelector('#cc-attach-device'),blossom=root.querySelector('#cc-attach-blossom'); local.onclick=()=>{p.closeModal();file.click();}; blossom.onclick=()=>{p.closeModal();p.blossomPicker(null,insertBlossomAttachment,{title:'📁 Attach from Files'});}; }); };
    const uploadAttachments=async files=>{ for(const f of files){ if(f.size>20*1024*1024){ p.toast(f.name+' is too large (20 MB max)'); continue; } try{ p.toast('uploading '+f.name+'…'); const isXdc=/\.xdc$/i.test(f.name)||f.type==='application/x-webxdc'||f.type==='application/webxdc+zip'||f.type==='application/vnd.webxdc+zip',bytes=isXdc?new Uint8Array(await f.arrayBuffer()):null,url=await p.uploadBlob(f,{keep:true}),mime=isXdc?'application/vnd.webxdc+zip':String(f.type||'application/octet-stream'),tag=['imeta',`url ${url}`,`m ${mime}`,`name ${String(f.name||'file').slice(0,120)}`]; if(isXdc){ const sha=bytesHex(await crypto.subtle.digest('SHA-256',bytes)),topic=mintWebxdcTopic(),name=f.name.replace(/\.xdc$/i,'').slice(0,80);tag.push(`x ${sha}`,`webxdc-topic ${topic}`,`webxdc ${topic}`,`summary ${name}`); }pendingAttachments.set(url,tag); input.value+=(input.value&&!/\s$/.test(input.value)?' ':'')+url; input.dispatchEvent(new Event('input',{bubbles:true})); }catch(e){ p.toast('could not attach '+f.name); } } };
    if(file&&input)file.onchange=async()=>{ await uploadAttachments([...file.files]); file.value=''; };
    if(input)input.onpaste=event=>{ const images=[...(event.clipboardData&&event.clipboardData.items||[])].filter(item=>item.kind==='file'&&String(item.type||'').startsWith('image/')).map(item=>item.getAsFile&&item.getAsFile()).filter(Boolean); if(!images.length)return; event.preventDefault(); void uploadAttachments(images); };
    const members=$('#cc-members'); if(members)members.onclick=()=>{if(!window.matchMedia||window.matchMedia('(max-width:820px)').matches){$('#cc-members-dialog').classList.remove('hidden');return;}const pane=$('.cc-members-pane');if(!pane)return;const hide=localStorage.getItem('pc.concord.members.hidden')!=='1';pane.classList.toggle('hidden',hide);localStorage.setItem('pc.concord.members.hidden',hide?'1':'0');};
    const membersClose=$('#cc-members-close'); if(membersClose)membersClose.onclick=()=>$('#cc-members-dialog').classList.add('hidden');
    const banMember=async target=>{ const initial=saved(),room=initial[state.community],roomId=roomIdentity(room),viewer=p.viewer?p.viewer():{},bundle=room&&room.cord&&room.cord.bundle,reader=window.PosterCordReader,loadKey=room&&(room.communityId||room.naddr),wraps=roomControls.get(loadKey); if(!bundle||!reader||!reader.createBanWrap||!wraps)return p.toast('community moderation is not ready'); if(typeof window.confirm==='function'&&!window.confirm('Ban this member from the community?'))return; try{ const made=await reader.createBanWrap(bundle,wraps,target,viewer.pubkey,p.signTemplate),relays=roomRelays(bundle),accepted=await p.relayPublishTo(relays,made.wrap); if(!accepted)throw new Error('community relays rejected the ban');/* A signer may keep this promise open while the owner changes rooms. Update the moderated room by durable identity instead of overwriting the newly active numeric index. */const latest=saved(),roomIndex=latest.findIndex(item=>roomIdentity(item)===roomId);if(roomIndex<0)throw new Error('community was removed while moderation was pending');latest[roomIndex].banned=made.banned;save(latest);render();p.toast('member banned'); }catch(e){p.toast('member was not banned: '+(e&&e.message||e));} };
    const closeMemberMenu=()=>{const old=document.querySelector('.cc-member-menu');if(old)old.remove();};
    const openMemberMenu=(event,target)=>{closeMemberMenu();const canBan=isOwner&&target!==viewer.pubkey,canMessage=target!==viewer.pubkey,menu=document.createElement('div');menu.className='cc-member-menu';menu.setAttribute('role','menu');menu.innerHTML=`<button data-cc-member-profile="${p.enc(target)}" role="menuitem">View profile</button>${canMessage?`<button data-cc-member-message="${p.enc(target)}" role="menuitem">Message</button>`:''}${canBan?`<button class="danger" data-cc-member-ban="${p.enc(target)}" role="menuitem">Ban from community</button>`:''}`;document.body.appendChild(menu);const anchor=event.currentTarget||(event.target&&event.target.closest&&event.target.closest('[data-cc-member]')),rect=anchor&&anchor.getBoundingClientRect?anchor.getBoundingClientRect():null,rows=1+(canMessage?1:0)+(canBan?1:0),x=Math.min(rect?rect.right+6:(event.clientX||12),window.innerWidth-190),y=Math.min(rect?rect.top:(event.clientY||12),window.innerHeight-(rows*42+8));menu.style.left=Math.max(8,x)+'px';menu.style.top=Math.max(8,y)+'px';menu.querySelector('[data-cc-member-profile]').onclick=()=>{closeMemberMenu();if(p.openProfile)p.openProfile(target);};const message=menu.querySelector('[data-cc-member-message]');if(message)message.onclick=()=>{closeMemberMenu();if(p.messageUser)p.messageUser(target);};const ban=menu.querySelector('[data-cc-member-ban]');if(ban)ban.onclick=()=>{closeMemberMenu();void banMember(target);};setTimeout(()=>document.addEventListener('pointerdown',e=>{if(!menu.contains(e.target))closeMemberMenu();},{once:true}),0);};
    $$('[data-cc-member]').forEach(row=>{const target=row.dataset.ccMember;let held=null,longPressed=false;row.onclick=e=>{e.preventDefault();/* Android/iOS synthesize click after a completed long press. Consume that click or the menu is immediately replaced by Profile. Resolve the viewport now, not when this row was rendered: rotation and desktop window resizing can cross the responsive boundary without causing a Concord repaint. */const action=memberTapAction(memberViewportIsNarrow(),longPressed);longPressed=false;if(action==='consume')return;if(action==='profile'){if(p.openProfile)p.openProfile(target);return;}openMemberMenu(e,target);};row.oncontextmenu=e=>{e.preventDefault();longPressed=false;openMemberMenu(e,target);};row.onpointerdown=e=>{if(e.pointerType==='mouse')return;longPressed=false;held=setTimeout(()=>{held=null;longPressed=true;openMemberMenu(e,target);},550);};row.onpointerup=row.onpointercancel=row.onpointermove=()=>{if(held){clearTimeout(held);held=null;}};});
    const membersInvite=$('#cc-members-invite'); if(membersInvite)membersInvite.onclick=()=>{ $('#cc-members-dialog').classList.add('hidden'); $('#cc-join').classList.remove('hidden'); };
    const copyLink=$('#cc-copy-link'); if(copyLink)copyLink.onclick=async()=>{ const a=saved(),room=a[state.community]; if(!room)return; if(room.url){ p.copyValue(room.url); return; } copyLink.disabled=true; try{ p.toast('upgrading this room to a public relay community…'); const priorMessages=testMessages(room.naddr), upgraded=await mintPublicRoom(p,room.name,room.icon); upgraded.description=room.description||''; a[state.community]=upgraded; save(a); if(priorMessages.length)saveTestMessages(upgraded.naddr,priorMessages); render(); p.copyValue(upgraded.url); p.toast('room upgraded — invite link copied'); }catch(e){ copyLink.disabled=false; p.toast('could not create invite: '+(e&&e.message||e)); } };
    const publishListing=$('#cc-publish-listing'); if(publishListing)publishListing.onclick=async()=>{ const room=saved()[state.community]; if(!room||!room.url||!room.cord||!Array.isArray(room.cord.events)){ p.toast('This is an old local sandbox; create a relay community to list it'); return; } publishListing.disabled=true; try{ p.toast('publishing to Armada relays…'); for(const ev of room.cord.events)await p.relayPublishTo(CORD_RELAYS,ev); const announcement=await p.publish(1,`${room.name}\n\n${room.url}`,[['t','concord'],['t','community']]); const accepted=await p.relayPublishTo(DISCOVER_RELAYS,announcement.ev); if(!accepted)throw new Error('Armada discovery relays rejected the listing'); p.toast('published to Armada Discover'); }catch(e){ p.toast('could not publish listing: '+(e&&e.message||e)); }finally{ publishListing.disabled=false; } };
    const settingsCancel=$('#cc-settings-cancel'); if(settingsCancel)settingsCancel.onclick=()=>$('#cc-settings-dialog').classList.add('hidden');
    const leave=$('#cc-leave-community');if(leave)leave.onclick=async()=>{const initial=saved(),index=state.community,room=initial[index],leavingId=roomIdentity(room);if(!room||!leavingId)return;if(typeof window.confirm==='function'&&!window.confirm('Leave '+roomName(room,index)+'?'))return;leave.disabled=true;try{await leaveArmadaMembership(p,room);/* Signing and relay publication can take long enough for membership sync or navigation to change the list. Reload it and remove by durable identity, never by the stale numeric index captured above. */const latest=saved(),activeBefore=latest[state.community],activeId=roomIdentity(activeBefore),removed=removeCommunityByIdentity(latest,leavingId),rooms=removed.rooms;save(rooms);await clearRoomCache(room);if(activeId===leavingId||!activeId){state.community=rooms.length?Math.min(Math.max(removed.index,0),rooms.length-1):null;state.channel=state.community==null?null:'general';mobileChatOpen=false;}else{const activeIndex=rooms.findIndex(item=>roomIdentity(item)===activeId);state.community=activeIndex>=0?activeIndex:(rooms.length?0:null);}if(state.community!=null)localStorage.setItem('pc.concord.active',String(state.community));else localStorage.removeItem('pc.concord.active');render();p.toast('community left');}catch(e){leave.disabled=false;p.toast('could not leave community: '+(e&&e.message||e));}};
    const settingsSave=$('#cc-settings-save'); if(settingsSave)settingsSave.onclick=async()=>{ const a=saved(),room=a[state.community]; if(!room)return; const description=String($('#cc-description-value').value||'').trim().slice(0,1000),icon=normalizeIcon($('#cc-settings-icon').value); settingsSave.disabled=true; try{ if(!room.local){const viewer=p.viewer?p.viewer():{},reader=window.PosterCordReader,bundle=room.cord&&room.cord.bundle,loadKey=room.communityId||room.naddr,relays=roomRelays(bundle);if(!reader||!reader.createMetadataWrap||!bundle)throw new Error('community profile is not ready');let wraps=roomControls.get(loadKey);if(!wraps){const seed=reader.inspectControl(bundle,[]);wraps=await cordQuery(p,relays,[{kinds:[1059],authors:seed.controlPubkeys,limit:1000}],{timeout:10000,max:8});}const made=await reader.createMetadataWrap(bundle,wraps||[],{name:room.name,description,icon},viewer.pubkey,p.signTemplate),accepted=await p.relayPublishTo(relays,made.wrap);if(!accepted)throw new Error('community relays rejected the profile update');roomControls.set(loadKey,[...(wraps||[]),made.wrap]);} room.description=description; room.icon=icon; if(!Array.isArray(room.channels))room.channels=[]; let channel=room.channels.find(c=>c.name===(state.channel||'general')); if(!channel){ channel={name:state.channel||'general'}; room.channels.push(channel); } channel.private=$('#cc-channel-visibility').value==='private'; save(a); render(); p.toast('community profile updated'); }catch(e){settingsSave.disabled=false;p.toast('community profile was not updated: '+(e&&e.message||e));} };
    const notify=$('#cc-notify'); if(notify)notify.onclick=async()=>{ const result=p.askOsNotify?await p.askOsNotify():'unsupported'; p.toast(result==='granted'?'community notifications enabled':result==='denied'?'notifications were denied':'notifications are unavailable here'); };
    const call=$('#cc-call'); if(call)call.onclick=()=>{ const room=saved()[state.community],viewerPk=p.viewer&&p.viewer().pubkey,peers=roomParticipants(room,viewerPk).filter(pk=>pk!==viewerPk); if(!peers.length){ p.toast('No other community members are available to call yet'); return; } p.startGroupCall(peers,false); };
    const cancel=$('#cc-join-cancel'); if(cancel) cancel.onclick=()=>$('#cc-join').classList.add('hidden');
    const go=$('#cc-join-go'); if(go) go.onclick=async()=>{ const raw=String($('#cc-invite-url').value||'').trim(),v=inviteParts(raw); if(!v){ p.toast('that is not a Concord invite link'); return; } go.disabled=true; try{ p.toast('fetching and decrypting community…'); const room=await hydrateInvite(p,raw),a=saved(),i=a.findIndex(x=>x.naddr===v.naddr); if(i<0)a.push(room);else a[i]={...a[i],...room}; save(a); state.community=i<0?a.length-1:i; state.channel='general'; render(); await persistArmadaMembership(p,room);
      /* Joining is already the user's request to enter this room.  Waiting for a later channel click
       * left the placeholder #general on screen with no id, icon or history, so a successful Armada
       * invite looked like an empty broken community until somebody switched away and back. */
      await hydrateRoomStreams(p,state.community); enterChatBottom(); p.toast('community joined'); }catch(e){ go.disabled=false; p.toast('could not join: '+(e&&e.message||e)); } };
    $$('[data-cc-server]').forEach(b=>b.onclick=()=>{const i=+b.dataset.ccServer,inDrawer=mobileChatOpen&&mobileDrawerOpen;void activateJoinedRoom(p,i,inDrawer);});
    $$('[data-cc-discover]').forEach(b=>b.onclick=async()=>{ const v=discovered[+b.dataset.ccDiscover]; if(!v)return; const a=saved(); let i=a.findIndex(x=>x.naddr===v.naddr); if(i<0){a.push(v);i=a.length-1;} save(a); state.community=i; state.channel='general'; render(); enterChatBottom(); p.toast('fetching and decrypting community…'); try{ a[i]={...a[i],...await hydrateInvite(p,v.url)}; save(a); await persistArmadaMembership(p,a[i]); await hydrateRoomStreams(p,i); p.toast('community joined'); }catch(e){ p.toast('could not load community: '+(e&&e.message||e)); }finally{if(state.community===i)enterChatBottom();} });
    $$('[data-cc-channel]').forEach(b=>b.onclick=async()=>{ const community=state.community,channel=b.dataset.ccChannel; state.channel=channel; mobileChatOpen=true; mobileDrawerOpen=false; render(); enterChatBottom(); const rooms=saved(),room=rooms[community],noticeKey=roomIdentity(room)+':'+channel; try{if(room&&room.cord&&!hydratedRoomViews.has(roomIdentity(room)))await hydrateRoomStreams(p,community);else if(room&&room.protocol==='nip29'&&!room.nip29Hydrated)await hydrateNip29Room(p,community);roomLoadNotices.delete(noticeKey);}catch(e){roomLoadWarning(p,noticeKey,'could not refresh room history: ',e);} if(state.community===community&&state.channel===channel)enterChatBottom(); });
    $$('[data-cc-star]').forEach(b=>b.onclick=e=>{ if(e&&e.stopPropagation)e.stopPropagation(); const room=saved()[state.community],name=b.dataset.ccStar; if(!room||!name)return; setChannelStarred(room,name,!channelStarred(room,name)); render(); });
    const bc=$('#cc-back-communities'); if(bc)bc.onclick=()=>{ discoveryOpen=true; state.community=null; state.channel=null; render(); };
    const bh=$('#cc-back-channels'); if(bh)bh.onclick=()=>{ if(state.community==null){ const rooms=saved(),wanted=Number(localStorage.getItem('pc.concord.active')||0); discoveryOpen=false; state.community=rooms.length&&wanted>=0&&wanted<rooms.length?wanted:(rooms.length?0:null); state.channel=state.community==null?null:'general'; mobileChatOpen=false; mobileDrawerOpen=false; }else if(mobileChatOpen){mobileDrawerOpen=!mobileDrawerOpen;}else mobileChatOpen=true; render(); };
    const drawerBackdrop=$('#cc-drawer-backdrop');if(drawerBackdrop)drawerBackdrop.onclick=()=>{mobileDrawerOpen=false;render();};
    const send=$('#cc-send'); if(send&&input){
      send.onclick=async()=>{ const text=String(input.value||'').trim(),key=input.dataset&&input.dataset.ccDraftKey||composerKey(saved()[state.community],state.channel); if(!text||sendingDrafts.has(key))return; const a=saved(), room=a[state.community],storeId=channelStoreId(room,state.channel); if(!room||(!room.local&&!room.cord&&room.protocol!=='nip29')){ p.toast('relay messaging becomes available after the invite is decrypted'); return; } const used=[...pendingAttachments].filter(([url])=>text.includes(url)),attachmentTags=used.map(([,tag])=>tag),target=replyTarget,replyTags=[],viewer=p.viewer?p.viewer():{},m=testMessages(storeId),lowerText=text.toLowerCase(),mentionTags=[],taggedPeople=new Set();for(const [handle,pk] of mentionRecipients){if(lowerText.includes('@'+handle))taggedPeople.add(pk);}for(const pk of typedMentionRecipients(text,roomParticipants(room,viewer.pubkey),p.profOf))taggedPeople.add(pk);for(const pk of taggedPeople){mentionTags.push(['P',pk],['p',pk]);} if(target){const inherited=(target.tags||[]).filter(t=>['K','E'].includes(t[0]));if(inherited.length)replyTags.push(...inherited);else replyTags.push(['K',String(target.kind||9)],['E',messageId(target),'',target.pubkey||'']);replyTags.push(['k',String(target.kind||9)],['e',messageId(target),'',target.pubkey||'']);for(const pk of threadParticipants(m,target,viewer.pubkey)){replyTags.push(['P',pk],['p',pk]);}} const submittedDraft=beginComposerSend(key);sendingDrafts.add(key);const extraTags=[...attachmentTags,...mentionTags,...replyTags],wireKind=target?1111:9,at=Date.now(),tempId='pending-'+(crypto.randomUUID?crypto.randomUUID():`${at}-${Math.random().toString(36).slice(2)}`),optimistic={id:tempId,by:me,pubkey:viewer.pubkey||'',text,at,kind:wireKind,tags:extraTags,reply:target?{id:messageId(target),by:target.by,text:target.text}:null,reactions:{},pending:!room.local,remote:false}; m.push(optimistic); saveTestMessages(storeId,m); render(); scrollChatBottom(); const finish=()=>{for(const [url] of used)pendingAttachments.delete(url);sendingDrafts.delete(key);render();scrollChatBottom();}; if(room.local){finish();return;} try{ const made=await publishCordMessage(p,room,state.channel,text,extraTags,wireKind),latest=testMessages(storeId),sent=latest.find(x=>x.id===tempId); if(sent){sent.id=made.rumorId;sent.at=made.ms;sent.pending=false;sent.remote=true;saveTestMessages(storeId,latest);} finish(); }catch(e){ sendingDrafts.delete(key);restoreFailedComposer(key,submittedDraft);const latest=testMessages(storeId),failed=latest.find(x=>x.id===tempId);if(failed){failed.pending=false;failed.failed=true;saveTestMessages(storeId,latest);preserveChatScroll(()=>render());} p.toast('message was not sent: '+(e&&e.message||e)); } };
      input.onkeydown=e=>{ const enter=e.key==='Enter'||e.code==='Enter'; if(mentionChoices.length){ if(e.key==='ArrowDown'||e.key==='ArrowUp'){e.preventDefault();mentionIndex=(mentionIndex+(e.key==='ArrowDown'?1:-1)+mentionChoices.length)%mentionChoices.length;syncMentionState();drawMentions();return;} if(e.key==='Tab'||(enter&&!e.ctrlKey&&!e.metaKey)){e.preventDefault();acceptMention();return;} if(e.key==='Escape'){e.preventDefault();closeMentions();return;} } if(enter&&(e.ctrlKey||e.metaKey)){ e.preventDefault(); return send.onclick(); } };
    }
    const replyCancel=$('#cc-reply-cancel'); if(replyCancel)replyCancel.onclick=()=>{ replyTarget=null; render(); };
    const closeMessageActions=()=>{$$('.cc-message.cc-actions-open').forEach(x=>{x.classList.remove('cc-actions-open');const t=x.querySelector('[data-cc-actions]');if(t)t.setAttribute('aria-expanded','false');});const picker=document.querySelector('.cc-reaction-picker');if(picker)picker.remove();reactionTarget=null;};
    if(actionDismissOff){actionDismissOff();actionDismissOff=null;}
    const dismissPointer=e=>{if(!(e.target&&e.target.closest&&e.target.closest('.cc-message-actions,.cc-reaction-picker')))closeMessageActions();};
    const dismissKey=e=>{if(e.key==='Escape')closeMessageActions();};
    const dismissBlur=()=>closeMessageActions();
    document.addEventListener('pointerdown',dismissPointer,true);document.addEventListener('keydown',dismissKey,true);window.addEventListener('blur',dismissBlur);
    actionDismissOff=()=>{document.removeEventListener('pointerdown',dismissPointer,true);document.removeEventListener('keydown',dismissKey,true);window.removeEventListener('blur',dismissBlur);};
    $$('[data-cc-delete]').forEach(button=>button.onclick=async()=>{ closeMessageActions();const room=saved()[state.community],storeId=channelStoreId(room,state.channel),messages=testMessages(storeId),id=button.dataset.ccDelete,found=messages.find(m=>messageId(m)===id),viewer=p.viewer?p.viewer():{}; if(!found||!viewer.pubkey||found.pubkey!==viewer.pubkey)return; const confirmed=p.uiConfirm?await p.uiConfirm('Delete this message?',{ok:'Delete',danger:true}):(typeof window.confirm!=='function'||window.confirm('Delete this message?')); if(!confirmed)return; button.disabled=true; try{ if(!room.local)await publishCordMessage(p,room,state.channel,'',[['e',id],['k',String(found.kind||9)]],5); saveTestMessages(storeId,messages.filter(m=>messageId(m)!==id)); if(!removeMessageRow(id))preserveChatScroll(()=>render()); }catch(e){ button.disabled=false; p.toast('message was not deleted: '+(e&&e.message||e)); } });
    $$('[data-cc-reply]').forEach(b=>b.onclick=()=>{ closeMessageActions();const room=saved()[state.community],m=activeMessages(room),found=m.find(x=>messageId(x)===b.dataset.ccReply); if(!found)return; replyTarget=found; render(); const box=$('#cc-input'); if(box)box.focus(); });
    $$('.cc-message-reply').forEach(b=>{ b.setAttribute('role','button');b.tabIndex=0;b.title='Show original message';const row=b.closest('.cc-message'),room=saved()[state.community],messages=activeMessages(room),message=row&&messages.find(x=>messageId(x)===row.dataset.messageId),original=message&&message.reply&&String(message.reply.id||'');const jump=()=>{const target=[...document.querySelectorAll('.cc-message[data-message-id]')].find(x=>x.dataset.messageId===original);if(!target){p.toast('original message is not in the loaded room history');return;}const st=readScroll(scrollKey());st.pinned=false;target.scrollIntoView({block:'center',behavior:'smooth'});target.classList.add('cc-message-target');setTimeout(()=>{if(target.isConnected)target.classList.remove('cc-message-target');},1800);setTimeout(()=>{const box=document.querySelector('.cc-messages');if(box){st.top=box.scrollTop;st.height=box.scrollHeight;writeScroll(scrollKey(),st);}},500);};b.onclick=jump;b.onkeydown=e=>{if(e.key==='Enter'||e.key===' '){e.preventDefault();jump();}}; });
    const toggleReaction=async(id,emoji)=>{ const room=saved()[state.community],storeId=channelStoreId(room,state.channel),m=testMessages(storeId),found=m.find(x=>messageId(x)===id),viewer=p.viewer?p.viewer():{},who=viewer.pubkey||'local-user'; if(!found)return; if(!found.reactions||typeof found.reactions!=='object')found.reactions={}; const people=Array.isArray(found.reactions[emoji])?found.reactions[emoji]:[],i=people.indexOf(who); try{if(!room.local){if(i<0)await publishCordMessage(p,room,state.channel,emoji,[['e',id],['p',found.pubkey||''],['k',String(found.kind||9)]],7);else{const rid=found.reactionIds&&found.reactionIds[emoji]&&found.reactionIds[emoji][who];if(!rid)throw new Error('refresh the room before removing this reaction');await publishCordMessage(p,room,state.channel,'',[['e',rid],['k','7']],5);}}}catch(e){p.toast('reaction was not sent: '+(e&&e.message||e));return;} if(i<0)people.push(who);else people.splice(i,1); if(people.length)found.reactions[emoji]=people;else delete found.reactions[emoji]; saveTestMessages(storeId,m); reactionTarget=null; preserveChatScroll(()=>render()); };
    $$('[data-cc-actions]').forEach(b=>b.onclick=e=>{e.stopPropagation();const row=b.closest('.cc-message'),open=!row.classList.contains('cc-actions-open');$$('.cc-message.cc-actions-open').forEach(x=>{x.classList.remove('cc-actions-open');const t=x.querySelector('[data-cc-actions]');if(t)t.setAttribute('aria-expanded','false');});row.classList.toggle('cc-actions-open',open);b.setAttribute('aria-expanded',String(open));});
    $$('[data-cc-react-toggle]').forEach(b=>b.onclick=()=>toggleReaction(b.dataset.ccReactToggle,b.dataset.ccEmoji));
    $$('[data-cc-react]').forEach(b=>b.onclick=()=>{ const target=b.dataset.ccReact;closeMessageActions();reactionTarget=target; const choices=['👍','❤️','😂','😮','😢','😡','🎉','💯']; const pop=document.createElement('div'); pop.className='cc-reaction-picker'; pop.innerHTML=choices.map(x=>`<button data-emoji="${x}">${x}</button>`).join(''); placeReactionPicker(b,pop); pop.querySelectorAll('button').forEach(x=>x.onclick=e=>{ e.stopPropagation();const id=reactionTarget;closeMessageActions();toggleReaction(id,x.dataset.emoji); }); });
  }
  async function webxdcCordParts(ctx){const p=PC(),room=saved().find(r=>roomIdentity(r)===ctx.room),reader=window.PosterCordReader,bundle=room&&room.cord&&room.cord.bundle,channel=room&&(room.channels||[]).find(c=>c.id===ctx.channelId||c.name===ctx.channel);if(!p||!room||!reader||!bundle||!channel)throw new Error('Concord Webxdc channel is unavailable');const loadKey=room.communityId||room.naddr,relays=roomRelays(bundle);let controls=roomControls.get(loadKey);if(!controls){const seed=reader.inspectControl(bundle,[]);controls=await queryEnvelopeHistory(p,relays,seed.controlPubkeys,await cachedEnvelopes(envelopeCacheKey(loadKey,'control')));roomControls.set(loadKey,controls||[]);}const view=reader.inspectControl(bundle,controls||[]),wireChannel=view.channels.find(c=>c.id===channel.id);return{p,room,reader,bundle,channel,loadKey,relays,controls:controls||[],streamPubkeys:wireChannel&&wireChannel.streamPubkeys||[]};}
  async function webxdcQuery(ctx,uuid){const p=PC();if(ctx.protocol==='nip29')return nip29RelayQuery(p,ctx.relay,[{kinds:[9450],'#h':[ctx.groupId],'#i':[uuid],limit:500}],10000);const x=await webxdcCordParts(ctx),key=envelopeCacheKey(x.loadKey,x.channel.id),cached=await cachedEnvelopes(key),wraps=await queryEnvelopeHistory(x.p,x.relays,x.streamPubkeys,cached),fresh=wraps.filter(ev=>!cached.some(old=>old.id===ev.id));await cacheEnvelopes(key,fresh);const rows=await x.reader.inspectWebxdc(x.bundle,x.controls,x.channel.id,wraps,uuid,false);try{window.PCWebxdc&&PCWebxdc.rtDiagnostic('static-replay',uuid+' '+rows.length+'/'+wraps.length);}catch(_){}return rows;}
  async function webxdcPublish(ctx,uuid,content,meta,realtime,liveSub){const p=PC(),tags=[['i',uuid],['alt',realtime?'Webxdc realtime':'Webxdc update']];if(realtime)tags.push(['rt','1']);for(const n of ['info','document','summary'])if(meta&&meta[n])tags.push([n,String(meta[n]).slice(0,200)]);if(ctx.protocol==='nip29')return p.publishNip29Authed(ctx.relay,{kind:realtime?24450:9450,created_at:Math.floor(Date.now()/1000),content,tags:[['h',ctx.groupId],...tags]});const x=await webxdcCordParts(ctx),viewer=x.p.viewer(),made=await x.reader.createWebxdcWrap(x.bundle,x.controls,x.channel.id,content,viewer.pubkey,x.p.signTemplate,tags,realtime);
    /* publishTo deliberately skips managed relays. It was the receive-side bug's mirror image: a
     * perfectly connected room relay meant every Webxdc packet was sent to zero sockets. Reuse the
     * subscription's external sockets and send to one matching managed socket, never a random relay. */
    if(realtime){const sent=(p.relayPublishFastTo?p.relayPublishFastTo(x.relays,made.wrap):0)+(liveSub&&liveSub.publish?liveSub.publish(made.wrap):0);if(!sent)throw new Error('no live room relay');return made;}
    const [pool,external]=await Promise.all([p.relayPublish(made.wrap),p.relayPublishTo(x.relays,made.wrap)]);if(!(pool&&pool.ok)&&!external)throw new Error(pool&&pool.msg||'room relays rejected the update');return made;}
  async function webxdcSubscribe(ctx,uuid,realtime,onEvent){
    const R=window.Relay;if(!R||!R.subscribe||!R.subscribeFrom)throw new Error('relay subscription unavailable');
    let urls,filters,receive;
    if(ctx.protocol==='nip29'){
      urls=[ctx.relay];filters=[{kinds:[realtime?24450:9450],'#h':[ctx.groupId],'#i':[uuid],since:Math.floor(Date.now()/1000)-120}];receive=onEvent;
    }else{
      const x=await webxdcCordParts(ctx),kind=realtime?21059:1059,filter={kinds:[kind],authors:x.streamPubkeys};
      if(realtime)filter.since=Math.floor(Date.now()/1000)-120;
      urls=x.relays;filters=[filter];receive=async wrap=>{try{const rows=await x.reader.inspectWebxdc(x.bundle,x.controls,x.channel.id,[wrap],uuid,realtime);for(const row of rows)onEvent(row);}catch(_){}};
    }
    /* subscribeFrom intentionally skips URLs already owned by Relay's managed pool. Using it alone
     * therefore subscribed to NOTHING for the common case where a Concord relay was also a normal
     * account relay. Listen on the pool and only use temporary sockets for the remaining URLs. */
    const pooled=R.subscribe(filters,{onEvent:receive}),external=R.subscribeFrom(urls,filters,{onEvent:receive});
    const gates=[];
    if(R.waitForSubscription)gates.push(R.waitForSubscription(pooled,urls).then(ok=>{if(!ok)throw new Error('managed room relay did not open');}));
    if(external.hasTargets&&external.ready)gates.push(external.ready.then(ok=>{if(!ok)throw new Error('external room relay did not open');}));
    try{if(gates.length)await Promise.any(gates);}catch(_){R.close(pooled);external();throw new Error('room relay subscription could not open');}
    const close=()=>{try{R.close(pooled);}catch(_){}try{external();}catch(_){}};
    close.publish=event=>(R.publishFastTo&&R.publishFastTo(urls,event)?1:0)+(external.publish?external.publish(event):0);
    return close;
  }
  /* CORD-04 Webxdc lobby signalling. Armada/Vector put the topic in the encrypted JSON body and
   * intentionally omit the update stream's `i` tag. Reusing webxdcPublish/webxdcSubscribe would
   * therefore create a private PosterChan-only lobby: our reader filters updates by `i`, while the
   * ecosystem peers never emit one. */
  async function webxdcPeerPublish(ctx,content,liveSub){
    if(!ctx||ctx.protocol!=='concord2')throw new Error('Iroh peer signalling requires a Concord channel');
    const x=await webxdcCordParts(ctx),viewer=x.p.viewer(),made=await x.reader.createWebxdcWrap(x.bundle,x.controls,x.channel.id,content,viewer.pubkey,x.p.signTemplate,[],false);
    /* Publish on the room subscription's actual sockets. A generic pool success may be an unrelated
     * account relay and cannot prove an Armada peer can see this advertisement. */
    const sent=(x.p.relayPublishFastTo?x.p.relayPublishFastTo(x.relays,made.wrap):0)+(liveSub&&liveSub.publish?liveSub.publish(made.wrap):0);
    if(!sent){const accepted=await x.p.relayPublishTo(x.relays,made.wrap);if(!accepted)throw new Error('room relays rejected the peer signal');}
    return made;
  }
  async function webxdcPeerQuery(ctx){
    if(!ctx||ctx.protocol!=='concord2')throw new Error('Iroh peer signalling requires a Concord channel');
    const x=await webxdcCordParts(ctx);if(!x.reader.inspectWebxdcSignals)throw new Error('peer signalling unavailable');
    const filters=[{kinds:[1059],authors:x.streamPubkeys,limit:5000}],history=await cordQuery(x.p,x.relays,filters,{timeout:10000,max:8});
    return x.reader.inspectWebxdcSignals(x.bundle,x.controls,x.channel.id,history);
  }
  async function webxdcPeerSubscribe(ctx,onEvent){
    if(!ctx||ctx.protocol!=='concord2')throw new Error('Iroh peer signalling requires a Concord channel');
    const R=window.Relay,x=await webxdcCordParts(ctx);
    if(!R||!R.subscribe||!R.subscribeFrom||!x.reader.inspectWebxdcSignals)throw new Error('peer signalling unavailable');
    const seen=new Set(),receive=async wrap=>{try{const rows=await x.reader.inspectWebxdcSignals(x.bundle,x.controls,x.channel.id,[wrap]);for(const row of rows)if(!seen.has(row.id)){seen.add(row.id);onEvent(row);}}catch(_){}};
    const filters=[{kinds:[1059],authors:x.streamPubkeys,limit:1000}],pooled=R.subscribe(filters,{onEvent:receive}),external=R.subscribeFrom(x.relays,filters,{onEvent:receive});
    /* Backfill after opening the live subscription, so an advertisement published during the query
     * cannot fall into the gap. The id set makes the overlap harmless. */
    /* Do not hold joinRealtimeChannel (and Quake's host-election burst) behind a ten-second history
     * query. The live listener is already installed; fold the durable advertisements when their
     * backfill arrives. */
    void cordQuery(x.p,x.relays,filters,{timeout:10000,max:8}).then(history=>x.reader.inspectWebxdcSignals(x.bundle,x.controls,x.channel.id,history)).then(rows=>{for(const row of rows)if(!seen.has(row.id)){seen.add(row.id);onEvent(row);}}).catch(()=>{});
    const close=()=>{try{R.close(pooled);}catch(_){}try{external();}catch(_){}};
    close.publish=event=>(R.publishFastTo&&R.publishFastTo(x.relays,event)?1:0)+(external.publish?external.publish(event):0);
    return close;
  }
  window.PCConcord={render,backgroundRender,wake,openInvite:openInviteLink,openNotification,notificationRoute,inviteParts,normalizeIcon,roomIcon,roomRelays,reactionSummary,reactionPickerPosition,notifyMentions,discoverInvites,membershipEvents,decodeMembershipLists,mergeArmadaBundle,nip29MembershipTags,nip29Memberships,nip29Metadata,nip29History,foldNip29History,nip29PreviousTags,publishNip29Message,syncNip29Memberships,hydrateNip29Room,hydrateRoomStreams,activateJoinedRoom,resumeActiveRoom,threadParticipants,roomParticipants,typedMentionRecipients,textMentionsViewer,conversationIsVisible,repaintScrollTop,pendingEchoMatch,applyRoomIconMetadata,channelSectionsHtml,removeCommunityByIdentity,memberTapAction,memberViewportIsNarrow,encryptedAttachments,publicAttachments,messageContentHtml,wireRoomMedia,handoffState,acceptHandoff,beginComposerSend,restoreFailedComposer,webxdcOf,resolveWebxdcCard,deriveWebxdcUrlTopic,hydrateWebxdcCards,webxdcQuery,webxdcPublish,webxdcSubscribe,webxdcPeerQuery,webxdcPeerPublish,webxdcPeerSubscribe};
  /* A monitor destination may load this module only after its frame-handoff callback has returned.
   * Adopt the one-shot room/channel before app.js invokes render(), then remove it so an ordinary
   * later Communities open cannot replay an old monitor move. */
  if(window.__pcConcordHandoff){
    try{acceptHandoff(window.__pcConcordHandoff);}finally{delete window.__pcConcordHandoff;}
  }
})();
