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
      (address?'<p class="iw-address">'+esc(address)+'</p><p>Open <strong>Edit profile</strong>, replace the <strong>NIP-05 / verified address</strong> field with this address, then <strong>Save</strong>.</p>':
       '<p>Apply for a name'+(domain?' at <strong>'+esc(domain)+'</strong>':' on this instance')+'. After approval, open <strong>Edit profile</strong>, set your NIP-05 address, and <strong>Save</strong>.</p>')+
      '<p class="muted small">Existing app permissions still apply.</p><p class="ia-status" role="status"></p>'+
      '<button class="btn btn-neon ia-profile">Edit profile</button> <button class="btn btn-ghost ia-apply">Apply for a name</button> <button class="btn btn-ghost ia-retry">Check again</button></section>';
    const card=feed.querySelector('.instance-app-gate'),status=card.querySelector('.ia-status');
    status.textContent=!pk?'Sign in to continue.':error||(!state?'Checking membership…':'');
    const edit=card.querySelector('.ia-profile'),apply=card.querySelector('.ia-apply');
    edit.disabled=!pk;apply.disabled=!pk;apply.hidden=!!address;
    edit.onclick=()=>p.editOwnProfile();
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
