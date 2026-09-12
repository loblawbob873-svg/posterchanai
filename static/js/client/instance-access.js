/* Instance app access is verified by the server, not inferred from profile text. */
(function(root){
  'use strict';
  const apps=Object.freeze({news:'News',meme:'Meme Builder',mail:'Email',office:'Documents',
    sync:'Folder Sync',vault:'Passwords',torrents:'Torrents',analytics:'My Analytics',
    'media-center':'Media Center',repos:'Git',texts:'Texts',notes:'Notes',
    wallet:'Monero Wallet',exodus:'Wallet',websearch:'Web Search'});
  const localApps=new Set(['notes','vault','texts','analytics']);
  const storedKey=()=> 'pc_instance_membership:'+owner;
  function cachedOffline(){
    try{const saved=JSON.parse(localStorage.getItem(storedKey())||'null');
      if(saved?.profile===profile && saved.data?.pubkey===pc()?.viewer()?.pubkey && saved.data?.qualified===true)
        return {...saved.data,offline:true,checkedAt:0};
    }catch(_){}
    return null;
  }
  let owner='',profile='',state=null,pending=null,generation=0,error='',forceNeeded=false,profileKnown=false;
  const pc=()=>root.__PC;
  const key=()=>String(root.__PC_API_BASE__||location.origin)+':'+(pc()?.viewer()?.pubkey||'');
  function scope(){
    const next=key(),viewer=pc()?.viewer(),known=viewer?.profileKnown!==false;
    const current=String(viewer?.profile?.nip05||'').trim();
    if(next!==owner||current!==profile||known!==profileKnown){forceNeeded=next===owner;if(forceNeeded&&known&&profileKnown){try{localStorage.removeItem(storedKey());}catch(_){}}owner=next;profile=current;profileKnown=known;state=null;pending=null;error='';generation++;}
    return generation;
  }
  function accept(data,pk){
    scope();
    if(pk!==pc()?.viewer()?.pubkey || !data || data.pubkey!==pk || typeof data.qualified!=='boolean')return;
    state={...data,offline:false,checkedAt:Date.now()};error='';
    try{if(profileKnown)localStorage.setItem(storedKey(),JSON.stringify({profile,data}));}catch(_){}
  }
  function allowed(view){scope();if(!state&&navigator.onLine===false)state=cachedOffline();return !(view in apps)||(state?.qualified===true&&(!state.offline||localApps.has(view)));}
  async function refresh(force=false){
    const epoch=scope(),pk=pc()?.viewer()?.pubkey;
    if(!pk)return null;
    if(navigator.onLine===false){state=cachedOffline();error='Offline. Reconnect to verify instance membership.';return state;}
    force=force||forceNeeded;
    if(pending)return pending;
    if(!force&&state&&!state.offline&&Date.now()-state.checkedAt<30000)return state;
    const job=Promise.resolve().then(async()=>{
      try{
        await pc().ensureAiSession();
        const response=await pc().authFetch('/api/instance-welcome/access'+(force?'?refresh=1':''));
        if(!response.ok)throw Error(response.status===403?'Sign in with your Nostr account.':'Could not verify instance membership. Please retry.');
        const data=await response.json();
        if(data.pubkey!==pk||typeof data.qualified!=='boolean')throw Error('Invalid membership response. Please retry.');
        if(scope()===epoch){accept(data,pk);if(force)forceNeeded=false;}
        return scope()===epoch?state:null;
      }catch(e){if(scope()===epoch){error=e.message||'Could not verify instance membership. Please retry.';state=cachedOffline();return state;}return null;}
      finally{if(pending===job)pending=null;}
    });
    pending=job;return job;
  }
  async function requireApp(view){
    if(!(view in apps))return;
    const data=await refresh();
    if(!data?.qualified||!allowed(view))throw Error(error||'Save your approved instance NIP-05 address in your profile to use '+apps[view]+'.');
  }
  function gate(view,host){
    scope();
    if(allowed(view)){
      if(view in apps && state && Date.now()-state.checkedAt>=30000){
        const epoch=generation;refresh().then(result=>{if(result&&!result.qualified&&scope()===epoch&&pc()?.isView(view))pc().retryInstanceView(view);});
      }
      return false;
    }
    const feed=host||document.getElementById('feed');if(!feed)return true;
    const p=pc(),pk=p?.viewer()?.pubkey,epoch=generation;
    const esc=p?.enc||String;
    const address=state?.address||'',domain=state?.domain||'';
    feed.innerHTML='<section class="instance-app-gate"><h2>'+esc(apps[view])+'</h2>'+
      '<p>This app is available after your approved instance NIP-05 address is saved in your profile.</p>'+
      (address?'<p class="iw-address">'+esc(address)+'</p><p>This instance has already given you that address \u2014 it just is not in your profile yet.</p>':
       '<p>Apply for a name'+(domain?' at <strong>'+esc(domain)+'</strong>':' on this instance')+'. After approval, open <strong>Edit profile</strong>, set your NIP-05 address, and <strong>Save</strong>.</p>')+
      '<p class="muted small">Existing app permissions still apply.</p><p class="ia-status" role="status"></p>'+
      /* ONE BUTTON, BECAUSE THE NODE ALREADY KNOWS THE ANSWER.
         This screen used to say "open Edit profile, replace the NIP-05 / verified address field
         with this address, then Save" -- in EVERY app, every time. The instance granted the name,
         so it knows exactly what the profile should say; making somebody read a paragraph and
         retype a string the app is displaying to them is work the app should be doing.
         "Edit profile" stays for anyone who wants to change something else at the same time. */
      (address?'<button class="btn btn-neon ia-use">Use '+esc(address)+'</button> ':'')+
      '<button class="btn '+(address?'btn-ghost':'btn-neon')+' ia-profile">Edit profile</button> <button class="btn btn-ghost ia-apply">Apply for a name</button> <button class="btn btn-ghost ia-retry">Check again</button></section>';
    const card=feed.querySelector('.instance-app-gate'),status=card.querySelector('.ia-status');
    status.textContent=!pk?'Sign in to continue.':error||(!state?'Checking membership…':'');
    const edit=card.querySelector('.ia-profile'),apply=card.querySelector('.ia-apply');
    edit.disabled=!pk;apply.disabled=!pk;apply.hidden=!!address;
    edit.onclick=()=>p.editOwnProfile();
    /* Publish the profile this instance is waiting for, then go where the person was heading.
       A kind-0 is REPLACEABLE and carries the whole document, so the current profile is read and
       ONE field changed -- writing a fresh object here would silently drop their name, picture,
       lud16 and every payment alias, which is the replaceable-document wipe this repo keeps
       re-learning. */
    const use=card.querySelector('.ia-use');
    if(use) use.onclick=async()=>{
      use.disabled=true; status.textContent='Setting your address\u2026';
      try{
        /* REFUSE RATHER THAN PUBLISH A BARE PROFILE. A kind-0 REPLACES the whole document, so
           publishing `{nip05}` alone would erase the name, picture, lud16 and every payment alias.
           The first draft of this called `p.profile()`, which the shared surface does not export
           (it is `profOf`) -- undefined, `||{}`, and the wipe. So read it, and if there is nothing
           to read, send the person to Edit profile instead of destroying what is there. */
        const cur=(p.profOf&&p.profOf(pk))||null;
        if(!cur||typeof cur!=='object'||!Object.keys(cur).length)
          throw Error('Could not read your current profile — open Edit profile and set it there, so nothing else is lost.');
        const meta={...cur, nip05:address};
        const made=await p.publish(0, JSON.stringify(meta), []);
        if(made&&made.ok===false) throw Error(made.msg||'Could not publish your profile.');
        status.textContent='Checking\u2026';
        await refresh(true);
        if(scope()!==epoch||!card.isConnected) return;
        if(state&&state.qualified){ p.toast&&p.toast('Address set \u2014 opening '+apps[view]); p.retryInstanceView(view); return; }
        status.textContent=error||'Saved. Relays can take a moment to serve it \u2014 press Check again.';
        use.disabled=false;
      }catch(e){ status.textContent=(e&&e.message)||'Could not set your address.'; use.disabled=false; }
    };
    apply.onclick=async()=>{
      apply.disabled=true;status.textContent='Sending your application…';
      try{const result=await root.PCInstanceWelcome.apply();status.textContent=result.already?'Your approved address is '+result.address+'. Open Edit profile, set your NIP-05 / verified address, then Save.':'Application submitted. Your approval DM will include the address and profile steps.';}
      catch(e){status.textContent=e.message;apply.disabled=false;}
    };
    const update=async(force)=>{
      const button=card.querySelector('.ia-retry');button.disabled=true;
      await refresh(force);
      if(scope()!==epoch||!card.isConnected)return;
      if(allowed(view)){p.retryInstanceView(view);return;}
      gate(view,feed);
    };
    card.querySelector('.ia-retry').onclick=()=>update(true);
    // A failed or negative response waits for explicit retry; never recurse into a request loop.
    if(pk&&!state&&!error)update(false);
    return true;
  }
  root.PCInstanceAccess={apps,allowed,refresh,accept,gate,require:requireApp};
})(window);
