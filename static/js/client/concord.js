/* Concord communities — a dedicated Discord/Matrix-shaped workspace (separate from public Chat). */
(function(){
  'use strict';
  // Do not depend on the monolithic shell CSS being current: an older service worker may serve its
  // cached client.css for one navigation. Concord owns a versioned sheet and loads it itself too.
  if(!document.querySelector('link[data-concord-css]')){
    const l=document.createElement('link'); l.rel='stylesheet'; l.dataset.concordCss='1';
    l.href='/static/css/concord.css?v=4'; (document.head||document.documentElement).appendChild(l);
  }
  const PC=()=>window.__PC;
  const state={ community:null, channel:null };
  function saved(){ try{ const v=JSON.parse(localStorage.getItem('pc.concord.invites')||'[]'); return Array.isArray(v)?v:[]; }catch(_){ return []; } }
  function save(v){ try{ localStorage.setItem('pc.concord.invites',JSON.stringify(v.slice(0,50))); }catch(_){} }
  function roomName(r,i){ return (r&&r.name)||`Encrypted community ${i+1}`; }
  function testMessages(id){ try{ const v=JSON.parse(localStorage.getItem('pc.concord.test.'+id)||'[]'); return Array.isArray(v)?v:[]; }catch(_){ return []; } }
  function saveTestMessages(id,v){ try{ localStorage.setItem('pc.concord.test.'+id,JSON.stringify(v.slice(-200))); }catch(_){} }
  function inviteParts(raw){
    let u; try{ u=new URL(String(raw||'').trim()); }catch(_){ return null; }
    const m=u.pathname.match(/\/invite\/(naddr1[023456789acdefghjklmnpqrstuvwxyz]+)\/?$/i);
    return m&&u.hash.length>3 ? {url:u.href,naddr:m[1],secret:u.hash.slice(1)} : null;
  }
  function render(){
    const p=PC(), feed=p&&p.$('#feed'); if(!feed) return;
    // Covers the stale-service-worker compatibility entry too, which does not run switchView().
    document.body.classList.add('concord-view','rb-off');
    const rooms=saved();
    const viewer=p.viewer?p.viewer():{};
    const profile=viewer.profile||{};
    const me=profile.display_name||profile.name||(profile.nip05&&p.niceNip05(profile.nip05))||(viewer.npub?viewer.npub.slice(0,12)+'…':'You');
    const current=state.community==null?null:rooms[state.community];
    const messages=current&&current.local?testMessages(current.naddr):[];
    feed.innerHTML=`<div class="cc-app">
      <aside class="cc-communities"><div class="cc-brand" title="Concord">C</div>${rooms.map((r,i)=>`<button class="cc-server${state.community===i?' active':''}" data-cc-server="${i}" title="${p.enc(roomName(r,i))}">${p.enc(roomName(r,i).slice(0,2).toUpperCase())}</button>`).join('')}<button class="cc-server cc-add" id="cc-add" title="Join a community">+</button></aside>
      <aside class="cc-channels"><header><button class="cc-mobile-back" id="cc-back-communities" aria-label="Communities">‹</button><div><b>${state.community==null?'Concord':p.enc(roomName(current,state.community))}</b><small>${current&&current.local?'Local test community':'End-to-end encrypted'}</small></div><button class="cc-head-btn" id="cc-invite" title="Join with invite">+</button></header>
        <div class="cc-channel-list">${state.community==null?'<div class="cc-empty-side">Choose or join a community</div>':`<div class="cc-section">TEXT CHANNELS</div><button class="cc-channel active" data-cc-channel="general"><span>#</span> general</button><div class="cc-section">VOICE CHANNELS</div><button class="cc-channel" data-cc-channel="voice"><svg class="ic"><use href="#i-volume"></use></svg> Lounge</button>`}</div>
        <footer class="cc-identity"><span class="cc-status"></span><div><b>${p.enc(me)}</b><small>You</small></div><button class="cc-head-btn" title="Notification settings"><svg class="ic"><use href="#i-bell"></use></svg></button></footer>
      </aside>
      <main class="cc-conversation"><header><button class="cc-mobile-back" id="cc-back-channels" aria-label="Channels">‹</button><span class="cc-hash">#</span><b>${state.channel||'general'}</b><span class="cc-topic">Private Concord channel</span><span class="cc-spacer"></span><button class="cc-head-btn" title="Start voice call"><svg class="ic"><use href="#i-phone"></use></svg></button><button class="cc-head-btn" title="Members"><svg class="ic"><use href="#i-users"></use></svg></button></header>
        <div class="cc-messages">${state.community==null?`<div class="cc-discover"><div class="concord-mark">C</div><h2>Find your community</h2><p>Join an Armada-compatible CORD-05 invite or create a local test community now.</p><div class="cc-primary-actions"><button class="btn btn-neon" id="cc-create">Create community</button><button class="btn btn-ghost" id="cc-welcome-join">Join with invite</button></div><section class="cc-public"><div><h3>Public communities</h3><small>Joinable invite links shared with this browser</small></div>${rooms.filter(r=>!r.local).length?rooms.map((r,i)=>r.local?'':`<button data-cc-server="${i}" class="cc-public-room"><b>${p.enc(roomName(r,i))}</b><span>Concord invite</span></button>`).join(''):'<div class="cc-public-empty"><b>No public invites found</b><span>Concord does not expose invite secrets in a global relay directory. Paste a public invite to add it here.</span></div>'}</section></div>`:(messages.length?`<div class="cc-message-list">${messages.map(m=>`<article class="cc-message"><b>${p.enc(m.by)}</b><time>${new Date(m.at).toLocaleTimeString([],{hour:'2-digit',minute:'2-digit'})}</time><p>${p.enc(m.text)}</p></article>`).join('')}</div>`:`<div class="cc-welcome"><div class="cc-welcome-hash">#</div><h2>Welcome to #${state.channel||'general'}</h2><p>${current&&current.local?'This local test room lets you validate the chat UI before publishing or joining a relay community.':'This is the start of this encrypted channel.'}</p></div>`)}</div>
        <div class="cc-reply hidden" id="cc-reply"></div><div class="cc-compose"><button class="cc-compose-btn" title="Attach"><svg class="ic"><use href="#i-plus"></use></svg></button><textarea id="cc-input" rows="1" placeholder="Message #${state.channel||'general'}" ${state.community==null?'disabled':''}></textarea><button class="cc-compose-btn" title="Emoji"><svg class="ic"><use href="#i-smile"></use></svg></button><button class="btn btn-neon" id="cc-send" ${state.community==null?'disabled':''}>Send</button></div>
      </main></div><div class="cc-join hidden" id="cc-join"><div class="cc-join-card"><div class="concord-mark">C</div><h2>Join a Concord community</h2><p class="muted">Paste an Armada or other CORD-05 invite. Its # secret stays in this browser.</p><input class="input" id="cc-invite-url" inputmode="url" autocomplete="off" autocapitalize="none" spellcheck="false" placeholder="https://…/invite/naddr1…#…"><div class="cc-join-actions"><button class="btn btn-ghost" id="cc-join-cancel">Cancel</button><button class="btn btn-neon" id="cc-join-go">Preview invite</button></div></div></div><div class="cc-join hidden" id="cc-create-dialog"><div class="cc-join-card"><div class="concord-mark">C</div><h2>Create a test community</h2><p class="muted">Creates a private local sandbox so you can test channels and messages immediately. It is not published to relays.</p><label class="cc-label" for="cc-community-name">Community name</label><input class="input" id="cc-community-name" maxlength="64" autocomplete="off" placeholder="My community"><div class="cc-join-actions"><button class="btn btn-ghost" id="cc-create-cancel">Cancel</button><button class="btn btn-neon" id="cc-create-go">Create</button></div></div></div>`;
    bind();
  }
  function bind(){
    const p=PC(), $=p.$, $$=p.$$;
    const openJoin=()=>{ $('#cc-join').classList.remove('hidden'); setTimeout(()=>$('#cc-invite-url').focus(),20); };
    ['#cc-add','#cc-invite','#cc-welcome-join'].forEach(s=>{ const b=$(s); if(b)b.onclick=openJoin; });
    const create=$('#cc-create'); if(create)create.onclick=()=>{ $('#cc-create-dialog').classList.remove('hidden'); setTimeout(()=>$('#cc-community-name').focus(),20); };
    const createCancel=$('#cc-create-cancel'); if(createCancel)createCancel.onclick=()=>$('#cc-create-dialog').classList.add('hidden');
    const createGo=$('#cc-create-go'); if(createGo)createGo.onclick=()=>{ const name=String($('#cc-community-name').value||'').trim(); if(!name){ p.toast('name your community'); return; } const a=saved(); a.push({name,local:true,naddr:'local-'+Date.now().toString(36)}); save(a); state.community=a.length-1; state.channel='general'; render(); p.toast('local test community created'); };
    const cancel=$('#cc-join-cancel'); if(cancel) cancel.onclick=()=>$('#cc-join').classList.add('hidden');
    const go=$('#cc-join-go'); if(go) go.onclick=()=>{ const v=inviteParts($('#cc-invite-url').value); if(!v){ p.toast('that is not a Concord invite link'); return; } const a=saved(); if(!a.some(x=>x.naddr===v.naddr))a.push(v); save(a); state.community=a.findIndex(x=>x.naddr===v.naddr); state.channel='general'; render(); p.toast('invite saved — fetching encrypted community'); };
    $$('[data-cc-server]').forEach(b=>b.onclick=()=>{ state.community=+b.dataset.ccServer; state.channel='general'; render(); });
    $$('[data-cc-channel]').forEach(b=>b.onclick=()=>{ state.channel=b.dataset.ccChannel; render(); });
    const bc=$('#cc-back-communities'); if(bc)bc.onclick=()=>{ state.community=null; state.channel=null; render(); };
    const bh=$('#cc-back-channels'); if(bh)bh.onclick=()=>{ document.querySelector('.cc-app').classList.remove('show-chat'); };
    $$('[data-cc-channel]').forEach(b=>b.addEventListener('click',()=>{ const a=document.querySelector('.cc-app'); if(a)a.classList.add('show-chat'); }));
    const send=$('#cc-send'), input=$('#cc-input'); if(send&&input)send.onclick=()=>{ const text=String(input.value||'').trim(); const a=saved(), room=a[state.community]; if(!text)return; if(!room||!room.local){ p.toast('relay messaging becomes available after the invite is decrypted'); return; } const m=testMessages(room.naddr); m.push({by:me,text,at:Date.now()}); saveTestMessages(room.naddr,m); render(); };
  }
  window.PCConcord={render,inviteParts};
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
