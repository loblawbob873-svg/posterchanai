/* Instance app access is verified by the server, not inferred from profile text. */
(function(root){
  'use strict';
  const apps=Object.freeze({news:'News',meme:'Meme Builder',mail:'Email',office:'Documents',
    sync:'Folder Sync',vault:'Passwords',torrents:'Torrents',analytics:'My Analytics',
    'media-center':'Media Center',repos:'Git',texts:'Texts',notes:'Notes',
    wallet:'Monero Wallet',exodus:'Wallet',websearch:'Web Search'});
  const localApps=new Set(['notes','vault','texts','analytics']);
  /* A VERIFIED MEMBER IS NOT RE-ASKED ON EVERY APP. The server remembers the grant (instance_membership
     GRANT_FRESH) and so does this page: within FRESH an app opens with no request at all, and after it
     the app still opens at once while the check runs behind it. */
  const FRESH=600000;
  /* THE VERDICT BELONGS TO THE ACCOUNT, NOT TO WHAT ITS PROFILE SAYS. Membership is the name this
     instance granted -- the server never reads the profile -- so editing your own NIP-05 (to an
     address of your own elsewhere, say) changes nothing here, and must not drop a stored yes and put
     a "Checking your access" panel in front of the next app you open. Keyed on instance + account. */
  const storedKey=()=> 'pc_instance_membership:'+owner;
  function stored(){
    try{const saved=JSON.parse(localStorage.getItem(storedKey())||'null');
      if(saved?.data?.pubkey===pc()?.viewer()?.pubkey && saved.data?.qualified===true) return saved.data;
    }catch(_){}
    return null;
  }
  function cachedOffline(){const d=stored();return d?{...d,offline:true,checkedAt:0}:null;}
  /* The stored yes, used ONLINE on a fresh load, so opening an app after a reload is not a
     "Checking your access…" screen. It is only a UI decision — every route is still gated by the
     server — and it is checked again behind the app (checkedAt:0), which re-draws the gate if the
     answer has become no. */
  function remembered(){const d=stored();return d?{...d,offline:false,remembered:true,checkedAt:0}:null;}
  let owner='',state=null,pending=null,generation=0,error='';
  let lastDomain='',told=false;
  const pc=()=>root.__PC;
  const key=()=>String(root.__PC_API_BASE__||location.origin)+':'+(pc()?.viewer()?.pubkey||'');
  function scope(){
    const next=key();
    if(next!==owner){
      owner=next;state=null;pending=null;error='';
      lastDomain='';told=false;generation++;
    }
    return generation;
  }
  function accept(data,pk){
    scope();
    if(pk!==pc()?.viewer()?.pubkey || !data || data.pubkey!==pk || typeof data.qualified!=='boolean')return;
    state={...data,offline:false,checkedAt:Date.now()};error='';
    try{if(data.qualified)localStorage.setItem(storedKey(),JSON.stringify({data}));else localStorage.removeItem(storedKey());}catch(_){}
  }
  function allowed(view){scope();if(!state)state=navigator.onLine===false?cachedOffline():remembered();return !(view in apps)||(state?.qualified===true&&(!state.offline||localApps.has(view)));}
  async function refresh(force=false){
    const epoch=scope(),pk=pc()?.viewer()?.pubkey;
    if(!pk)return null;
    if(navigator.onLine===false){state=cachedOffline();error='Offline. Reconnect to verify instance membership.';return state;}
    if(pending)return pending;
    if(!force&&state&&!state.offline&&Date.now()-state.checkedAt<FRESH)return state;
    const job=Promise.resolve().then(async()=>{
      try{
        await pc().ensureAiSession();
        const response=await pc().authFetch('/api/instance-welcome/access'+(force?'?refresh=1':''));
        if(!response.ok)throw Error(response.status===403?'Sign in with your Nostr account.':'Could not verify instance membership. Please retry.');
        const data=await response.json();
        if(data.pubkey!==pk||typeof data.qualified!=='boolean')throw Error('Invalid membership response. Please retry.');
        if(scope()===epoch)accept(data,pk);
        return scope()===epoch?state:null;
      }catch(e){if(scope()===epoch){error=e.message||'Could not verify instance membership. Please retry.';
        // A member whose re-check could not reach the server stays a member, as on the server.
        // …and is not re-asked on every app it opens: wait a minute before the next attempt.
        if(!force&&state?.qualified&&!state.offline){state={...state,checkedAt:Date.now()-FRESH+60000};return state;}
        state=cachedOffline();return state;}return null;}
      finally{if(pending===job)pending=null;}
    });
    pending=job;return job;
  }
  async function requireApp(view){
    if(!(view in apps))return;
    scope();if(!state&&navigator.onLine!==false)state=remembered();
    if(state?.qualified&&!state.offline){
      if(Date.now()-state.checkedAt>=FRESH)void refresh();
      if(allowed(view))return;
    }
    const data=await refresh();
    if(!data?.qualified||!allowed(view))throw Error(error||'An approved name on this instance unlocks '+apps[view]+'.');
  }
  /* THREE ANSWERS, NEVER TWO -- and this screen used to paint the same one for all of them.
     "We have not asked yet" and "we could not ask" are not "you are not a member", but the
     membership explanation ("apply for a name ... set your NIP-05 address ... Save") was the BODY of
     the card in every one of those cases. A member's verdict is UNKNOWN on every fresh load and
     after every account switch, so opening an app greeted them with the reasons they might be
     refused while the check that says otherwise was still in flight -- reported as "the nip05
     message you get when running every fucking app". Nothing about WHO IS ALLOWED changes here
     (`allowed` is untouched, and the server gates every route regardless): only what is shown, and
     how often. The explanation is painted for exactly one answer now -- a server that said no --
     and every other case is a short panel that continues into the app by itself. */
  const verdict=()=>state&&typeof state.qualified==='boolean'?(state.qualified?'yes':'no'):'unknown';
  function gate(view,host){
    scope();
    if(allowed(view)){
      if(view in apps && state && Date.now()-state.checkedAt>=FRESH){
        const epoch=generation;refresh().then(result=>{if(result&&!result.qualified&&scope()===epoch&&pc()?.isView(view))pc().retryInstanceView(view);});
      }
      return false;
    }
    const feed=host||document.getElementById('feed');if(!feed)return true;
    const p=pc(),pk=p?.viewer()?.pubkey,epoch=generation;
    const esc=p?.enc||String;
    if(state?.domain)lastDomain=state.domain;
    /* MEMBERSHIP IS THE NAME THIS INSTANCE GRANTED -- NEVER WHAT THE PROFILE SAYS. This panel used to
       end in "open Edit profile, replace your NIP-05 with this address, Save", and a one-click "Use
       <address>" that overwrote the profile's nip05: anybody with an identity of their own
       (bob@nostrplebs.com) had to give it up to use an app here. The server no longer reads the profile
       at all, so a granted name IS access and there is nothing in a profile to fix -- the only next
       step for somebody refused is a name, and their own NIP-05 stays exactly where it is. */
    const domain=state?.domain||lastDomain;
    const refused=verdict()==='no',waiting=!refused&&!!pk&&!error,blocked=refused&&state?.reason==='blocked';
    if(!refused)
      /* NOBODY HAS SAID NO. Name the app and say in one line what is happening -- never the reasons
         somebody might be refused: that is a verdict this panel has not got. */
      feed.innerHTML='<section class="instance-app-gate"><h2>'+esc(apps[view])+'</h2>'+
        (waiting?'<p><span class="spinner spinner-inline"></span>'+(cachedOffline()?'Opening ':'Checking your access to ')+esc(apps[view])+'…</p>':'')+
        '<p class="ia-status" role="status"></p>'+
        (waiting?'':'<button class="btn btn-neon ia-retry">Check again</button>')+
        '</section>';
    else if(blocked)
      feed.innerHTML='<section class="instance-app-gate"><h2>'+esc(apps[view])+'</h2>'+
        '<p>This account is blocked on this instance.</p><p class="ia-status" role="status"></p>'+
        '<button class="btn btn-ghost ia-retry">Check again</button></section>';
    else
      /* THE ANSWER IS NO, AND IT IS SAID IN FULL ONCE PER SCOPE. Reading the same paragraph on
         entering the second app teaches nobody anything; an account change resets `told`. */
      feed.innerHTML='<section class="instance-app-gate"><h2>'+esc(apps[view])+'</h2>'+
        '<p>An approved name'+(domain?' on <strong>'+esc(domain)+'</strong>':' on this instance')+' unlocks '+esc(apps[view])+'.</p>'+
        (told?'':'<p>Apply for one — as soon as an admin approves it, every app here opens. Keep whatever NIP-05 your profile shows: you do not need to change it, and both addresses are shown on your profile.</p>'+
                 '<p class="muted small">Existing app permissions still apply.</p>')+
        '<p class="ia-status" role="status"></p>'+
        '<button class="btn btn-neon ia-apply">Apply for a name</button> <button class="btn btn-ghost ia-retry">Check again</button></section>';
    if(refused)told=true;
    const card=feed.querySelector('.instance-app-gate'),status=card.querySelector('.ia-status');
    status.textContent=!pk?'Sign in to continue.':error||'';
    const apply=card.querySelector('.ia-apply');
    if(apply){
      apply.disabled=!pk;
      apply.onclick=async()=>{
        apply.disabled=true;status.textContent='Sending your application…';
        try{
          const result=await root.PCInstanceWelcome.apply();
          if(result.already){await update(true);return;}   // a name was granted meanwhile: that is access
          status.textContent='Application submitted. You will get a DM when it is approved.';
        }catch(e){status.textContent=e.message;apply.disabled=false;}
      };
    }
    const update=async(force)=>{
      const button=card.querySelector('.ia-retry');if(button)button.disabled=true;
      await refresh(force);
      if(scope()!==epoch||!card.isConnected)return;
      if(allowed(view)){p.retryInstanceView(view);return;}
      gate(view,feed);
    };
    /* The waiting panel carries no Check again button -- a check IS running, and a button that
       re-asks the question already in flight is a button that does nothing. */
    const retry=card.querySelector('.ia-retry');if(retry)retry.onclick=()=>update(true);
    // A failed or negative response waits for explicit retry; never recurse into a request loop.
    if(pk&&!state&&!error)update(false);
    return true;
  }
  root.PCInstanceAccess={apps,allowed,refresh,accept,gate,require:requireApp};
})(window);
