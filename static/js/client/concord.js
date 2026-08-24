/* Concord communities — a dedicated Discord/Matrix-shaped workspace (separate from public Chat). */
(function(){
  'use strict';
  // Do not depend on the monolithic shell CSS being current: an older service worker may serve its
  // cached client.css for one navigation. Concord owns a versioned sheet and loads it itself too.
  if(!document.querySelector('link[data-concord-css]')){
    const l=document.createElement('link'); l.rel='stylesheet'; l.dataset.concordCss='1';
    l.href='/static/css/concord.css?v=9'; (document.head||document.documentElement).appendChild(l);
  }
  const PC=()=>window.__PC;
  const CORD_RELAYS=['wss://jskitty.com/nostr','wss://asia.vectorapp.io/nostr','wss://relay.ditto.pub','wss://relay.dreamith.to'];
  const DISCOVER_RELAYS=['wss://relay.ditto.pub','wss://relay.dreamith.to'];
  const state={ community:null, channel:null };
  let replyTarget=null, reactionTarget=null;
  let discovered=[], discoveryStarted=false, discoveryLoaded=false, membershipStarted=false;
  function saved(){ try{ const v=JSON.parse(localStorage.getItem('pc.concord.invites')||'[]'); return Array.isArray(v)?v:[]; }catch(_){ return []; } }
  function save(v){ try{ localStorage.setItem('pc.concord.invites',JSON.stringify(v.slice(0,50))); }catch(_){} }
  function roomName(r,i){ return (r&&r.name)||`Encrypted community ${i+1}`; }
  function normalizeIcon(raw){
    const v=String(raw||'').trim(); if(!v)return '';
    try{ const u=new URL(v); if(u.protocol==='https:'||u.protocol==='http:')return u.href; }catch(_){}
    return Array.from(v).slice(0,4).join('');
  }
  function roomIcon(p,r,i){
    const icon=normalizeIcon(r&&r.icon);
    if(/^https?:\/\//i.test(icon)) return `<img class="cc-server-img" src="${p.enc(icon)}" alt="">`;
    return `<span class="cc-server-glyph">${p.enc(icon||roomName(r,i).slice(0,2).toUpperCase())}</span>`;
  }
  function testMessages(id){ try{ const v=JSON.parse(localStorage.getItem('pc.concord.test.'+id)||'[]'); return Array.isArray(v)?v:[]; }catch(_){ return []; } }
  function saveTestMessages(id,v){ try{ localStorage.setItem('pc.concord.test.'+id,JSON.stringify(v.slice(-200))); }catch(_){} }
  function lastActivity(room){ return (room&&room.naddr?testMessages(room.naddr):[]).reduce((n,x)=>Math.max(n,Number(x.at)||0),0); }
  function seenAt(room){ return Number(localStorage.getItem('pc.concord.read.'+(room&&room.naddr||''))||0); }
  function markRead(room){ if(room&&room.naddr)localStorage.setItem('pc.concord.read.'+room.naddr,String(Date.now())); }
  function isUnread(room){ return lastActivity(room)>seenAt(room); }
  function messageId(m){ return String((m&&m.id)||`${Number(m&&m.at)||0}:${String(m&&m.pubkey||'')}`); }
  function reactionSummary(p,m){
    const reactions=m&&m.reactions&&typeof m.reactions==='object'?m.reactions:{};
    return Object.entries(reactions).map(([emoji,people])=>{ const n=Array.isArray(people)?people.length:0; return n?`<button class="cc-reaction" data-cc-react-toggle="${p.enc(messageId(m))}" data-cc-emoji="${p.enc(emoji)}" title="${n} reaction${n===1?'':'s'}"><span>${p.enc(emoji)}</span><b>${n}</b></button>`:''; }).join('');
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
  function discoverInvites(text,source){
    const matches=String(text||'').match(/https?:\/\/[^\s<>]+\/invite\/naddr1[023456789acdefghjklmnpqrstuvwxyz]+#[A-Za-z0-9_-]+/gi)||[];
    return matches.map(url=>url.replace(/[),.;!?]+$/,'' )).map(url=>{ const parsed=inviteParts(url); if(!parsed)return null; const blurb=String(text).replace(url,'').replace(/#[\w-]+/g,'').replace(/\s+/g,' ').trim(); return {...parsed,name:blurb.slice(0,80)||'Public Concord community',description:blurb,source}; }).filter(Boolean);
  }
  function startDiscovery(p){
    if(discoveryStarted||!p.relaySubscribe)return; discoveryStarted=true;
    const bySigner=new Map();
    const onEvent=ev=>{ for(const item of discoverInvites(ev.content,ev)){ const old=bySigner.get(item.naddr); if(!old||Number(ev.created_at)>Number(old.source.created_at))bySigner.set(item.naddr,item); } discovered=[...bySigner.values()].sort((a,b)=>Number(b.source.created_at)-Number(a.source.created_at)); if(state.community==null)render(); };
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
    const filter=[{kinds:[33301],authors:[parsed.linkSigner],'#d':[''],limit:1}];
    const relays=[...new Set([...(parsed&&parsed.bootstrapRelays||[]),...CORD_RELAYS])];
    const [pool,external]=await Promise.all([p.relayQuery?p.relayQuery(filter,6000):[],p.relayQueryFrom(relays,filter,{timeout:7000,max:8})]);
    const opened=decoded(url,[...(pool||[]),...(external||[])]),bundle=opened.bundle;
    return {url,naddr:parts.naddr,name:bundle.name||'Concord community',description:'',channels:[{name:'general',private:false}],local:false,cord:{bundle,parsed:opened.parsed}};
  }
  function inviteRefUrl(ref){ const s=String(ref||''); if(/^https?:/i.test(s))return s; const [naddr,frag]=s.split('#'); return naddr&&frag?`https://armada.buzz/invite/${naddr}#${frag}`:''; }
  async function syncArmadaMemberships(p,viewer){
    if(membershipStarted||!viewer.pubkey||!p.nip44dec)return; membershipStarted=true;
    try{
      const filters=[{kinds:[13302],authors:[viewer.pubkey],limit:1}];
      const [pool,external]=await Promise.all([p.relayQuery?p.relayQuery(filters,7000):[],p.relayQueryFrom?p.relayQueryFrom(CORD_RELAYS,filters,{timeout:8000,max:4}):[]]);
      const event=[...(pool||[]),...(external||[])].sort((a,b)=>b.created_at-a.created_at)[0]; if(!event)return;
      const list=JSON.parse(await p.nip44dec(viewer.pubkey,event.content)),tombs=new Map((list.tombstones||[]).map(t=>[t.community_id,Number(t.removed_at)||0]));
      const live=(list.entries||[]).filter(e=>e&&e.current&&Number(e.added_at||0)>Number(tombs.get(e.community_id)||0)); if(!live.length)return;
      const rooms=saved(); let changed=false;
      for(const e of live){ const m=e.current,url=inviteRefUrl(e.invite_ref),i=rooms.findIndex(r=>r.communityId===e.community_id||r.url===url); const room={communityId:e.community_id,name:m.name||'Concord community',description:'',channels:[{name:'general',private:false},...(m.channels||[]).map(c=>({name:c.name||'private',private:true,id:c.id}))],local:false,naddr:url?(inviteParts(url)||{}).naddr:'community-'+e.community_id,url,cord:{bundle:m,armadaList:true}}; if(i<0){rooms.push(room);changed=true;}else if(!rooms[i].cord||rooms[i].cord.armadaList){rooms[i]={...rooms[i],...room};changed=true;} }
      if(changed){save(rooms);render();}
    }catch(e){ console.warn('Concord membership sync failed',e); }
  }
  async function mintPublicRoom(p,name,icon){
    const viewer=p.viewer?p.viewer():{}; if(!viewer.pubkey||!window.PosterCord)throw new Error('sign in before creating a relay community');
    const relays=[...new Set([...CORD_RELAYS,...(p.relayUrls?p.relayUrls():[])])].slice(0,8);
    const made=await window.PosterCord.createCommunity({name,icon,owner:viewer.pubkey,relays,base:location.origin,signEvent:p.signTemplate});
    for(const ev of made.events){ const accepted=await p.relayPublishTo(relays,ev); if(!accepted)throw new Error('CORD relays rejected an event'); }
    const announcement=await p.publish(1,`${name}\n\n${made.url}`,[['t','concord'],['t','community']]);
    await p.relayPublishTo(DISCOVER_RELAYS,announcement.ev);
    return {name,icon,description:'',channels:[{name:'general',private:false,id:made.generalChannelId}],local:false,naddr:inviteParts(made.url).naddr,url:made.url,cord:made};
  }
  function render(){
    const p=PC(), feed=p&&p.$('#feed'); if(!feed) return;
    startDiscovery(p);
    // Covers the stale-service-worker compatibility entry too, which does not run switchView().
    document.body.classList.add('concord-view','rb-off');
    const rooms=saved();
    const viewer=p.viewer?p.viewer():{};
    syncArmadaMemberships(p,viewer);
    const profile=viewer.profile||{};
    const me=profile.display_name||profile.name||(profile.nip05&&p.niceNip05(profile.nip05))||(viewer.npub?viewer.npub.slice(0,12)+'…':'You');
    const current=state.community==null?null:rooms[state.community];
    if(current)markRead(current);
    const currentChannel=current&&Array.isArray(current.channels)?current.channels.find(c=>c.name===(state.channel||'general')):null;
    const channelPrivate=!!(currentChannel&&currentChannel.private);
    const messages=current&&(current.local||current.cord)?testMessages(current.naddr):[];
    notifyMentions(p,current,messages,viewer,me);
    feed.innerHTML=`<div class="cc-app">
      <aside class="cc-communities"><div class="cc-brand" title="Concord" aria-label="Concord"><span aria-hidden="true">🕊</span></div>${rooms.map((r,i)=>`<button class="cc-server${state.community===i?' active':''}${isUnread(r)?' unread':''}" data-cc-server="${i}" title="${p.enc(roomName(r,i))}">${roomIcon(p,r,i)}</button>`).join('')}<button class="cc-server cc-add" id="cc-add" title="Join a community">+</button></aside>
      <aside class="cc-channels"><header><button class="cc-mobile-back" id="cc-back-communities" aria-label="Communities">‹</button><div><b>${state.community==null?'Concord':p.enc(roomName(current,state.community))}</b><small>${current&&current.local?'Local test community':'End-to-end encrypted'}</small></div>${current?'<button class="cc-head-btn" id="cc-edit-icon" title="Set community icon" aria-label="Set community icon"><svg class="ic"><use href="#i-image"></use></svg></button>':''}<button class="cc-head-btn" id="cc-invite" title="Join with invite">+</button></header>
        <div class="cc-channel-list">${state.community==null?'<div class="cc-empty-side">Choose or join a community</div>':`<div class="cc-section">TEXT CHANNELS</div><button class="cc-channel active${isUnread(current)?' unread':''}" data-cc-channel="general"><span>#</span> general</button>`}</div>
        <footer class="cc-identity"><span class="cc-status"></span><div><b>${p.enc(me)}</b><small>You</small></div><button class="cc-head-btn" id="cc-notify" title="Notification settings"><svg class="ic"><use href="#i-bell"></use></svg></button></footer>
      </aside>
      <main class="cc-conversation"><header><button class="cc-mobile-back" id="cc-back-channels" aria-label="Channels">‹</button><span class="cc-hash">#</span><b>${state.channel||'general'}</b><span class="cc-visibility ${channelPrivate?'private':'public'}">${channelPrivate?'Private':'Public'}</span><span class="cc-topic">${p.enc((current&&current.description)||(channelPrivate?'Invite-only channel':'Visible to all community members'))}</span><span class="cc-spacer"></span>${current?'<button class="cc-head-btn" id="cc-publish-listing" title="Publish to Armada Discover" aria-label="Publish to Armada Discover"><svg class="ic"><use href="#i-share"></use></svg></button><button class="cc-head-btn" id="cc-copy-link" title="Copy room invite link" aria-label="Copy room invite link"><svg class="ic"><use href="#i-link"></use></svg></button><button class="cc-head-btn" id="cc-call" title="Start voice call"><svg class="ic"><use href="#i-phone"></use></svg></button>':''}<button class="cc-head-btn" id="cc-members" title="Members"><svg class="ic"><use href="#i-users"></use></svg></button></header>
        <div class="cc-messages">${state.community==null?`<div class="cc-discover"><div class="concord-mark">C</div><h2>Find your community</h2><p>Join an Armada-compatible CORD-05 invite or create a public relay community.</p><div class="cc-primary-actions"><button class="btn btn-neon" id="cc-create">Create community</button><button class="btn btn-ghost" id="cc-welcome-join">Join with invite</button></div><section class="cc-public"><div><h3>Public communities</h3><small>Public CORD invites discovered on Armada relays</small></div>${discovered.length?discovered.map((r,i)=>{const pr=p.profOf?p.profOf(r.source.pubkey):{};return `<button data-cc-discover="${i}" class="cc-public-room"><span class="cc-public-icon">${p.enc((r.name||'C').slice(0,2).toUpperCase())}</span><span class="cc-public-copy"><b>${p.enc(r.name)}</b><small>${p.enc((r.description||'Public Concord community').slice(0,120))}</small><em>${p.enc(pr.name||pr.display_name||'Nostr community')}</em></span><strong>Join</strong></button>`;}).join(''):(discoveryLoaded?'<div class="cc-public-empty"><b>No public communities found</b><span>Publish or paste a public Armada/CORD invite to list it.</span></div>':'<div class="cc-public-empty"><b>Searching relays…</b><span>Looking for public Armada/CORD invite notes.</span></div>')}</section></div>`:(messages.length?`<div class="cc-message-list">${messages.map(m=>{const mp=p.profOf?p.profOf(m.pubkey):{},mid=messageId(m);return `<article class="cc-message" data-message-id="${p.enc(mid)}"><img class="cc-message-avatar" src="${p.enc(mp.picture||p.LOGO||'')}" alt=""><div class="cc-message-body">${m.reply?`<div class="cc-message-reply"><b>@${p.enc(m.reply.by||'member')}</b> ${p.enc(String(m.reply.text||'').slice(0,100))}</div>`:''}<b>${p.enc(m.by)}</b><time>${new Date(m.at).toLocaleTimeString([],{hour:'2-digit',minute:'2-digit'})}</time><div class="cc-message-actions"><button data-cc-reply="${p.enc(mid)}" title="Reply">↩</button><button data-cc-react="${p.enc(mid)}" title="Add reaction">☺</button></div><p>${p.linkify?p.linkify(m.text):p.enc(m.text)}</p>${p.linkCardHtml?p.linkCardHtml(m.text):''}<div class="cc-reactions">${reactionSummary(p,m)}</div></div></article>`;}).join('')}</div>`:`<div class="cc-welcome"><div class="cc-welcome-hash">#</div><h2>Welcome to #${state.channel||'general'}</h2><p>${current&&current.local?'This local test room lets you validate the chat UI before publishing or joining a relay community.':'This is the start of this encrypted channel.'}</p></div>`)}</div>
        <div class="cc-reply${replyTarget?'':' hidden'}" id="cc-reply">${replyTarget?`<span>Replying to <b>${p.enc(replyTarget.by||'member')}</b>: ${p.enc(String(replyTarget.text||'').slice(0,90))}</span><button id="cc-reply-cancel" aria-label="Cancel reply">×</button>`:''}</div><div class="cc-compose"><button class="cc-compose-btn" id="cc-attach" title="Attach file"><svg class="ic"><use href="#i-paperclip"></use></svg></button><input type="file" id="cc-file" multiple hidden><textarea id="cc-input" rows="1" placeholder="Message #${state.channel||'general'}" ${state.community==null?'disabled':''}></textarea><button class="cc-compose-btn" id="cc-emoji" title="Emoji"><svg class="ic"><use href="#i-smile"></use></svg></button><button class="btn btn-neon" id="cc-send" ${state.community==null?'disabled':''}>Send</button></div>
      </main></div><div class="cc-join hidden" id="cc-join"><div class="cc-join-card"><div class="concord-mark">C</div><h2>Join a Concord community</h2><p class="muted">Paste an Armada or other CORD-05 invite. Its # secret stays in this browser.</p><input class="input" id="cc-invite-url" inputmode="url" autocomplete="off" autocapitalize="none" spellcheck="false" placeholder="https://…/invite/naddr1…#…"><div class="cc-join-actions"><button class="btn btn-ghost" id="cc-join-cancel">Cancel</button><button class="btn btn-neon" id="cc-join-go">Preview invite</button></div></div></div><div class="cc-join hidden" id="cc-create-dialog"><div class="cc-join-card"><div class="concord-mark">C</div><h2>Create a public community</h2><p class="muted">Publishes an Armada-compatible CORD community and public #general channel to your relays.</p><label class="cc-label" for="cc-community-name">Community name</label><input class="input" id="cc-community-name" maxlength="64" autocomplete="off" placeholder="My community"><label class="cc-label" for="cc-community-icon">Icon <span class="muted">(emoji or image URL)</span></label><input class="input" id="cc-community-icon" maxlength="2048" autocomplete="off" placeholder="🚀 or https://…/icon.png"><div class="cc-join-actions"><button class="btn btn-ghost" id="cc-create-cancel">Cancel</button><button class="btn btn-neon" id="cc-create-go">Create on relays</button></div></div></div><div class="cc-join hidden" id="cc-icon-dialog"><div class="cc-join-card"><div class="concord-mark">C</div><h2>Community icon</h2><p class="muted">Use an emoji or a direct HTTP(S) image URL. Leave blank to restore the initials.</p><label class="cc-label" for="cc-icon-value">Icon</label><input class="input" id="cc-icon-value" maxlength="2048" autocomplete="off" placeholder="🌌 or https://…/icon.png"><div class="cc-join-actions"><button class="btn btn-ghost" id="cc-icon-cancel">Cancel</button><button class="btn btn-neon" id="cc-icon-save">Save icon</button></div></div></div>`;
    if(current){
      const memberPks=[...new Set([viewer.pubkey,...messages.map(m=>m.pubkey)].filter(Boolean))];
      feed.insertAdjacentHTML('beforeend',`<div class="cc-join hidden" id="cc-members-dialog"><div class="cc-join-card"><h2>Members <span class="muted">${memberPks.length}</span></h2><div class="cc-member-list">${memberPks.map((pk,i)=>{ const pr=p.profOf?p.profOf(pk):{}; const name=pk===viewer.pubkey?me:(pr.display_name||pr.name||pk.slice(0,12)+'…'); return `<div class="cc-member"><img src="${p.enc(pr.picture||p.LOGO||'')}" alt=""><div><b>${p.enc(name)}</b><small>${i===0&&current.local?'Owner':'Member'}</small></div></div>`; }).join('')}</div><div class="cc-join-actions"><button class="btn btn-ghost" id="cc-members-close">Close</button><button class="btn btn-neon" id="cc-members-invite">Invite people</button></div></div></div><div class="cc-join hidden" id="cc-settings-dialog"><div class="cc-join-card"><h2>Community settings</h2><label class="cc-label" for="cc-description-value">Description</label><textarea class="input cc-settings-description" id="cc-description-value" maxlength="1000" rows="3" placeholder="What is this community about?">${p.enc(current.description||'')}</textarea><label class="cc-label" for="cc-settings-icon">Icon</label><input class="input" id="cc-settings-icon" maxlength="2048" value="${p.enc(current.icon||'')}" placeholder="🌌 or https://…/icon.png"><label class="cc-label" for="cc-channel-visibility">#${p.enc(state.channel||'general')} visibility</label><select class="input cc-visibility-select" id="cc-channel-visibility"><option value="public"${channelPrivate?'':' selected'}>Public — all community members</option><option value="private"${channelPrivate?' selected':''}>Private — invited members only</option></select><div class="cc-join-actions"><button class="btn btn-ghost" id="cc-settings-cancel">Cancel</button><button class="btn btn-neon" id="cc-settings-save">Save changes</button></div></div></div>`);
    }
    if(p.hydrateLinkCards)p.hydrateLinkCards(feed);
    bind(me);
  }
  function bind(me){
    const p=PC(), $=p.$, $$=p.$$;
    const openJoin=()=>{ $('#cc-join').classList.remove('hidden'); setTimeout(()=>$('#cc-invite-url').focus(),20); };
    ['#cc-add','#cc-invite','#cc-welcome-join'].forEach(s=>{ const b=$(s); if(b)b.onclick=openJoin; });
    const create=$('#cc-create'); if(create)create.onclick=()=>{ $('#cc-create-dialog').classList.remove('hidden'); setTimeout(()=>$('#cc-community-name').focus(),20); };
    const createCancel=$('#cc-create-cancel'); if(createCancel)createCancel.onclick=()=>$('#cc-create-dialog').classList.add('hidden');
    const createGo=$('#cc-create-go'); if(createGo)createGo.onclick=async()=>{ const name=String($('#cc-community-name').value||'').trim(); if(!name){ p.toast('name your community'); return; } createGo.disabled=true; try{ p.toast('creating encrypted community…'); const room=await mintPublicRoom(p,name,normalizeIcon($('#cc-community-icon').value)); const a=saved(); a.push(room); save(a); state.community=a.length-1; state.channel='general'; render(); p.copyValue(room.url); p.toast('public community created — invite link copied'); }catch(e){ createGo.disabled=false; p.toast('community creation failed: '+(e&&e.message||e)); } };
    const editIcon=$('#cc-edit-icon'); if(editIcon)editIcon.onclick=()=>{ $('#cc-settings-dialog').classList.remove('hidden'); setTimeout(()=>$('#cc-description-value').focus(),20); };
    const iconCancel=$('#cc-icon-cancel'); if(iconCancel)iconCancel.onclick=()=>$('#cc-icon-dialog').classList.add('hidden');
    const iconSave=$('#cc-icon-save'); if(iconSave)iconSave.onclick=()=>{ const a=saved(), room=a[state.community]; if(!room)return; room.icon=normalizeIcon($('#cc-icon-value').value); save(a); render(); p.toast('community icon updated'); };
    const emoji=$('#cc-emoji'), input=$('#cc-input'); if(emoji&&input)emoji.onclick=()=>{ if(p.openEmojiPopover)p.openEmojiPopover(emoji,(value,close)=>{ if(close)close(); if(p.insertAt)p.insertAt(input,value); else input.value+=value; input.focus(); }); };
    const attach=$('#cc-attach'), file=$('#cc-file'); if(attach&&file)attach.onclick=()=>file.click();
    if(file&&input)file.onchange=async()=>{ const files=[...file.files]; for(const f of files){ if(f.size>20*1024*1024){ p.toast(f.name+' is too large (20 MB max)'); continue; } try{ p.toast('uploading '+f.name+'…'); const url=await p.uploadBlob(f,{keep:true}); input.value+=(input.value&&!/\s$/.test(input.value)?' ':'')+url; input.dispatchEvent(new Event('input',{bubbles:true})); }catch(e){ p.toast('could not attach '+f.name); } } file.value=''; };
    const members=$('#cc-members'); if(members)members.onclick=()=>$('#cc-members-dialog').classList.remove('hidden');
    const membersClose=$('#cc-members-close'); if(membersClose)membersClose.onclick=()=>$('#cc-members-dialog').classList.add('hidden');
    const membersInvite=$('#cc-members-invite'); if(membersInvite)membersInvite.onclick=()=>{ $('#cc-members-dialog').classList.add('hidden'); $('#cc-join').classList.remove('hidden'); };
    const copyLink=$('#cc-copy-link'); if(copyLink)copyLink.onclick=async()=>{ const a=saved(),room=a[state.community]; if(!room)return; if(room.url){ p.copyValue(room.url); return; } copyLink.disabled=true; try{ p.toast('upgrading this room to a public relay community…'); const priorMessages=testMessages(room.naddr), upgraded=await mintPublicRoom(p,room.name,room.icon); upgraded.description=room.description||''; a[state.community]=upgraded; save(a); if(priorMessages.length)saveTestMessages(upgraded.naddr,priorMessages); render(); p.copyValue(upgraded.url); p.toast('room upgraded — invite link copied'); }catch(e){ copyLink.disabled=false; p.toast('could not create invite: '+(e&&e.message||e)); } };
    const publishListing=$('#cc-publish-listing'); if(publishListing)publishListing.onclick=async()=>{ const room=saved()[state.community]; if(!room||!room.url||!room.cord||!Array.isArray(room.cord.events)){ p.toast('This is an old local sandbox; create a relay community to list it'); return; } publishListing.disabled=true; try{ p.toast('publishing to Armada relays…'); for(const ev of room.cord.events)await p.relayPublishTo(CORD_RELAYS,ev); const announcement=await p.publish(1,`${room.name}\n\n${room.url}`,[['t','concord'],['t','community']]); const accepted=await p.relayPublishTo(DISCOVER_RELAYS,announcement.ev); if(!accepted)throw new Error('Armada discovery relays rejected the listing'); p.toast('published to Armada Discover'); }catch(e){ p.toast('could not publish listing: '+(e&&e.message||e)); }finally{ publishListing.disabled=false; } };
    const settingsCancel=$('#cc-settings-cancel'); if(settingsCancel)settingsCancel.onclick=()=>$('#cc-settings-dialog').classList.add('hidden');
    const settingsSave=$('#cc-settings-save'); if(settingsSave)settingsSave.onclick=()=>{ const a=saved(),room=a[state.community]; if(!room)return; room.description=String($('#cc-description-value').value||'').trim().slice(0,1000); room.icon=normalizeIcon($('#cc-settings-icon').value); if(!Array.isArray(room.channels))room.channels=[]; let channel=room.channels.find(c=>c.name===(state.channel||'general')); if(!channel){ channel={name:state.channel||'general'}; room.channels.push(channel); } channel.private=$('#cc-channel-visibility').value==='private'; save(a); render(); p.toast(`#${channel.name} is now ${channel.private?'private':'public to community members'}`); };
    const notify=$('#cc-notify'); if(notify)notify.onclick=async()=>{ const result=p.askOsNotify?await p.askOsNotify():'unsupported'; p.toast(result==='granted'?'community notifications enabled':result==='denied'?'notifications were denied':'notifications are unavailable here'); };
    const call=$('#cc-call'); if(call)call.onclick=()=>{ const room=saved()[state.community], peers=[...new Set(testMessages(room&&room.naddr).map(m=>m.pubkey).filter(pk=>pk&&pk!==(p.viewer&&p.viewer().pubkey)))]; if(!peers.length){ p.toast('No other community members are available to call yet'); return; } p.startGroupCall(peers,false); };
    const cancel=$('#cc-join-cancel'); if(cancel) cancel.onclick=()=>$('#cc-join').classList.add('hidden');
    const go=$('#cc-join-go'); if(go) go.onclick=async()=>{ const raw=String($('#cc-invite-url').value||'').trim(),v=inviteParts(raw); if(!v){ p.toast('that is not a Concord invite link'); return; } go.disabled=true; try{ p.toast('fetching and decrypting community…'); const room=await hydrateInvite(p,raw),a=saved(),i=a.findIndex(x=>x.naddr===v.naddr); if(i<0)a.push(room);else a[i]={...a[i],...room}; save(a); state.community=i<0?a.length-1:i; state.channel='general'; render(); p.toast('community joined'); }catch(e){ go.disabled=false; p.toast('could not join: '+(e&&e.message||e)); } };
    $$('[data-cc-server]').forEach(b=>b.onclick=async()=>{ const i=+b.dataset.ccServer,a=saved(),room=a[i]; state.community=i; state.channel='general'; render(); if(room&&room.url&&!room.cord){ try{ p.toast('decrypting saved community…'); a[i]={...room,...await hydrateInvite(p,room.url)}; save(a); render(); p.toast('community loaded'); }catch(e){ p.toast('could not load community: '+(e&&e.message||e)); } } });
    $$('[data-cc-discover]').forEach(b=>b.onclick=()=>{ const v=discovered[+b.dataset.ccDiscover]; if(!v)return; const a=saved(); if(!a.some(x=>x.naddr===v.naddr))a.push(v); save(a); state.community=a.findIndex(x=>x.naddr===v.naddr); state.channel='general'; render(); p.toast('public invite saved — fetching encrypted community'); });
    $$('[data-cc-channel]').forEach(b=>b.onclick=()=>{ state.channel=b.dataset.ccChannel; render(); });
    const bc=$('#cc-back-communities'); if(bc)bc.onclick=()=>{ state.community=null; state.channel=null; render(); };
    const bh=$('#cc-back-channels'); if(bh)bh.onclick=()=>{ document.querySelector('.cc-app').classList.remove('show-chat'); };
    $$('[data-cc-channel]').forEach(b=>b.addEventListener('click',()=>{ const a=document.querySelector('.cc-app'); if(a)a.classList.add('show-chat'); }));
    const send=$('#cc-send'); if(send&&input){
      send.onclick=()=>{ const text=String(input.value||'').trim(); const a=saved(), room=a[state.community]; if(!text)return; if(!room||(!room.local&&!room.cord)){ p.toast('relay messaging becomes available after the invite is decrypted'); return; } const m=testMessages(room.naddr), viewer=p.viewer?p.viewer():{}, at=Date.now(); m.push({id:(crypto.randomUUID?crypto.randomUUID():`${at}-${Math.random().toString(36).slice(2)}`),by:me,pubkey:viewer.pubkey||'',text,at,reply:replyTarget?{id:messageId(replyTarget),by:replyTarget.by,text:replyTarget.text}:null,reactions:{}}); replyTarget=null; saveTestMessages(room.naddr,m); render(); };
      input.onkeydown=e=>{ if(e.key==='Enter'&&(e.ctrlKey||e.metaKey)){ e.preventDefault(); send.click(); } };
    }
    const replyCancel=$('#cc-reply-cancel'); if(replyCancel)replyCancel.onclick=()=>{ replyTarget=null; render(); };
    $$('[data-cc-reply]').forEach(b=>b.onclick=()=>{ const room=saved()[state.community],m=testMessages(room&&room.naddr),found=m.find(x=>messageId(x)===b.dataset.ccReply); if(!found)return; replyTarget=found; render(); const box=$('#cc-input'); if(box)box.focus(); });
    const toggleReaction=(id,emoji)=>{ const room=saved()[state.community],m=testMessages(room&&room.naddr),found=m.find(x=>messageId(x)===id),viewer=p.viewer?p.viewer():{},who=viewer.pubkey||'local-user'; if(!found)return; if(!found.reactions||typeof found.reactions!=='object')found.reactions={}; const people=Array.isArray(found.reactions[emoji])?found.reactions[emoji]:[]; const i=people.indexOf(who); if(i<0)people.push(who);else people.splice(i,1); if(people.length)found.reactions[emoji]=people;else delete found.reactions[emoji]; saveTestMessages(room.naddr,m); reactionTarget=null; render(); };
    $$('[data-cc-react-toggle]').forEach(b=>b.onclick=()=>toggleReaction(b.dataset.ccReactToggle,b.dataset.ccEmoji));
    $$('[data-cc-react]').forEach(b=>b.onclick=()=>{ reactionTarget=b.dataset.ccReact; const choices=['👍','❤️','😂','😮','😢','😡','🎉','💯']; const old=document.querySelector('.cc-reaction-picker'); if(old)old.remove(); const pop=document.createElement('div'); pop.className='cc-reaction-picker'; pop.innerHTML=choices.map(x=>`<button data-emoji="${x}">${x}</button>`).join(''); b.closest('.cc-message-body').appendChild(pop); pop.querySelectorAll('button').forEach(x=>x.onclick=e=>{ e.stopPropagation(); toggleReaction(reactionTarget,x.dataset.emoji); }); });
  }
  window.PCConcord={render,inviteParts,normalizeIcon,notifyMentions,discoverInvites};
  // Compatibility with a stale PWA app.js served once by the previous service worker. An older
  // controller knows the injected Discover row only as an unknown view: it changes the title, then
  // restores the previous feed. Re-mount after that bubble finishes. Harmless on a current shell
  // (render is idempotent), and it means a newly deployed destination works on the first click.
  document.addEventListener('click',e=>{
    const b=e.target.closest&&e.target.closest('[data-view="concord"]'); if(!b) return;
    setTimeout(()=>{ const title=document.getElementById('view-title'); if(title) title.textContent='Concord';
      document.body.classList.add('concord-view','rb-off');
      const feed=document.getElementById('feed'); if(feed){ feed.classList.add('feed-dm'); render(); } },0);
  });
})();
