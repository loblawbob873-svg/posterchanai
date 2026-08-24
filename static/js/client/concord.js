/* Concord communities — a dedicated Discord/Matrix-shaped workspace (separate from public Chat). */
(function(){
  'use strict';
  // Do not depend on the monolithic shell CSS being current: an older service worker may serve its
  // cached client.css for one navigation. Concord owns a versioned sheet and loads it itself too.
  if(!document.querySelector('link[data-concord-css]')){
    const l=document.createElement('link'); l.rel='stylesheet'; l.dataset.concordCss='1';
    l.href='/static/css/concord.css?v=3'; (document.head||document.documentElement).appendChild(l);
  }
  const PC=()=>window.__PC;
  const state={ community:null, channel:null };
  function saved(){ try{ const v=JSON.parse(localStorage.getItem('pc.concord.invites')||'[]'); return Array.isArray(v)?v:[]; }catch(_){ return []; } }
  function save(v){ try{ localStorage.setItem('pc.concord.invites',JSON.stringify(v.slice(0,50))); }catch(_){} }
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
    feed.innerHTML=`<div class="cc-app">
      <aside class="cc-communities"><div class="cc-brand" title="Concord">C</div>${rooms.map((r,i)=>`<button class="cc-server${state.community===i?' active':''}" data-cc-server="${i}" title="Encrypted community ${i+1}">${i+1}</button>`).join('')}<button class="cc-server cc-add" id="cc-add" title="Join a community">+</button></aside>
      <aside class="cc-channels"><header><button class="cc-mobile-back" id="cc-back-communities" aria-label="Communities">‹</button><div><b>${state.community==null?'Concord':`Encrypted community ${state.community+1}`}</b><small>End-to-end encrypted</small></div><button class="cc-head-btn" id="cc-invite" title="Join with invite">+</button></header>
        <div class="cc-channel-list">${state.community==null?'<div class="cc-empty-side">Choose or join a community</div>':`<div class="cc-section">TEXT CHANNELS</div><button class="cc-channel active" data-cc-channel="general"><span>#</span> general</button><div class="cc-section">VOICE CHANNELS</div><button class="cc-channel" data-cc-channel="voice"><svg class="ic"><use href="#i-volume"></use></svg> Lounge</button>`}</div>
        <footer class="cc-identity"><span class="cc-status"></span><div><b>${p.enc(me)}</b><small>You</small></div><button class="cc-head-btn" title="Notification settings"><svg class="ic"><use href="#i-bell"></use></svg></button></footer>
      </aside>
      <main class="cc-conversation"><header><button class="cc-mobile-back" id="cc-back-channels" aria-label="Channels">‹</button><span class="cc-hash">#</span><b>${state.channel||'general'}</b><span class="cc-topic">Private Concord channel</span><span class="cc-spacer"></span><button class="cc-head-btn" title="Start voice call"><svg class="ic"><use href="#i-phone"></use></svg></button><button class="cc-head-btn" title="Members"><svg class="ic"><use href="#i-users"></use></svg></button></header>
        <div class="cc-messages">${state.community==null?`<div class="cc-welcome"><div class="concord-mark">C</div><h2>Your Concord communities</h2><p>Join with an Armada-compatible CORD-05 invite. Communities, channels, roles and encrypted history live on Nostr relays.</p><button class="btn btn-neon" id="cc-welcome-join">Join a community</button></div>`:`<div class="cc-welcome"><div class="cc-welcome-hash">#</div><h2>Welcome to #${state.channel||'general'}</h2><p>This is the start of this encrypted channel.</p></div>`}</div>
        <div class="cc-reply hidden" id="cc-reply"></div><div class="cc-compose"><button class="cc-compose-btn" title="Attach"><svg class="ic"><use href="#i-plus"></use></svg></button><textarea id="cc-input" rows="1" placeholder="Message #${state.channel||'general'}" ${state.community==null?'disabled':''}></textarea><button class="cc-compose-btn" title="Emoji"><svg class="ic"><use href="#i-smile"></use></svg></button><button class="btn btn-neon" id="cc-send" ${state.community==null?'disabled':''}>Send</button></div>
      </main></div><div class="cc-join hidden" id="cc-join"><div class="cc-join-card"><div class="concord-mark">C</div><h2>Join a Concord community</h2><p class="muted">Paste an Armada or other CORD-05 invite. Its # secret stays in this browser.</p><input class="input" id="cc-invite-url" inputmode="url" autocomplete="off" autocapitalize="none" spellcheck="false" placeholder="https://…/invite/naddr1…#…"><div class="cc-join-actions"><button class="btn btn-ghost" id="cc-join-cancel">Cancel</button><button class="btn btn-neon" id="cc-join-go">Preview invite</button></div></div></div>`;
    bind();
  }
  function bind(){
    const p=PC(), $=p.$, $$=p.$$;
    const openJoin=()=>{ $('#cc-join').classList.remove('hidden'); setTimeout(()=>$('#cc-invite-url').focus(),20); };
    ['#cc-add','#cc-invite','#cc-welcome-join'].forEach(s=>{ const b=$(s); if(b)b.onclick=openJoin; });
    const cancel=$('#cc-join-cancel'); if(cancel) cancel.onclick=()=>$('#cc-join').classList.add('hidden');
    const go=$('#cc-join-go'); if(go) go.onclick=()=>{ const v=inviteParts($('#cc-invite-url').value); if(!v){ p.toast('that is not a Concord invite link'); return; } const a=saved(); if(!a.some(x=>x.naddr===v.naddr))a.push(v); save(a); state.community=a.findIndex(x=>x.naddr===v.naddr); state.channel='general'; render(); p.toast('invite saved — fetching encrypted community'); };
    $$('[data-cc-server]').forEach(b=>b.onclick=()=>{ state.community=+b.dataset.ccServer; state.channel='general'; render(); });
    $$('[data-cc-channel]').forEach(b=>b.onclick=()=>{ state.channel=b.dataset.ccChannel; render(); });
    const bc=$('#cc-back-communities'); if(bc)bc.onclick=()=>{ state.community=null; state.channel=null; render(); };
    const bh=$('#cc-back-channels'); if(bh)bh.onclick=()=>{ document.querySelector('.cc-app').classList.remove('show-chat'); };
    $$('[data-cc-channel]').forEach(b=>b.addEventListener('click',()=>{ const a=document.querySelector('.cc-app'); if(a)a.classList.add('show-chat'); }));
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
