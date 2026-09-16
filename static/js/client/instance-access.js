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
  const storedKey=()=> 'pc_instance_membership:'+owner;
  function cachedOffline(){
    try{const saved=JSON.parse(localStorage.getItem(storedKey())||'null');
      if(saved?.profile===profile && saved.data?.pubkey===pc()?.viewer()?.pubkey && saved.data?.qualified===true)
        return {...saved.data,offline:true,checkedAt:0};
    }catch(_){}
    return null;
  }
  /* The stored yes, used ONLINE on a fresh load, so opening an app after a reload is not a
     "Checking your access…" screen. It is only a UI decision — every route is still gated by the
     server — and it is checked again behind the app (checkedAt:0), which re-draws the gate if the
     answer has become no. The profile may not have arrived yet on boot; the server knows it. */
  function remembered(){
    try{const saved=JSON.parse(localStorage.getItem(storedKey())||'null'),viewer=pc()?.viewer();
      const sameProfile=saved?.profile===profile||viewer?.profileKnown===false;
      if(sameProfile && saved.data?.pubkey===viewer?.pubkey && saved.data?.qualified===true)
        return {...saved.data,offline:false,remembered:true,checkedAt:0};
    }catch(_){}
    return null;
  }
  let owner='',profile='',state=null,pending=null,generation=0,error='',forceNeeded=false,profileKnown=false;
  /* The last thing this instance told us about the person, kept across a check that FAILED. `state`
     is cleared whenever a check cannot be completed, and with it went the granted address — i.e. the
     one button that fixes this screen disappeared exactly when the screen was least able to explain
     itself. Cleared with the scope, so it can never describe a different account. */
  let lastAddress='',lastDomain='',told=false;
  const pc=()=>root.__PC;
  const key=()=>String(root.__PC_API_BASE__||location.origin)+':'+(pc()?.viewer()?.pubkey||'');
  function scope(){
    const next=key(),viewer=pc()?.viewer(),known=viewer?.profileKnown!==false;
    const current=String(viewer?.profile?.nip05||'').trim();
    if(next!==owner||current!==profile||known!==profileKnown){
      /* FORCE ONLY WHEN THE PUBLISHED ADDRESS ACTUALLY CHANGED. `?refresh=1` makes the server throw
         away its own answer and re-read the relays — right after somebody edits their profile, and
         wrong on every boot. A boot hit it: the profile ARRIVING turns `profileKnown` false->true,
         which counted as a change, so every reload paid an uncached relay round trip with the app
         replaced by this gate for its whole duration. Learning what the profile says is not the same
         as the profile saying something new. (The stored verdict is dropped on exactly the condition
         that used to drop it: same account, both profiles known, a different address.) */
      forceNeeded=next===owner&&known&&profileKnown&&current!==profile;
      if(forceNeeded){try{localStorage.removeItem(storedKey());}catch(_){}}
      owner=next;profile=current;profileKnown=known;state=null;pending=null;error='';
      lastAddress='';lastDomain='';told=false;generation++;
    }
    return generation;
  }
  function accept(data,pk){
    scope();
    if(pk!==pc()?.viewer()?.pubkey || !data || data.pubkey!==pk || typeof data.qualified!=='boolean')return;
    state={...data,offline:false,checkedAt:Date.now()};error='';
    try{if(profileKnown)localStorage.setItem(storedKey(),JSON.stringify({profile,data}));}catch(_){}
  }
  function allowed(view){scope();if(!state)state=navigator.onLine===false?cachedOffline():remembered();return !(view in apps)||(state?.qualified===true&&(!state.offline||localApps.has(view)));}
  async function refresh(force=false){
    const epoch=scope(),pk=pc()?.viewer()?.pubkey;
    if(!pk)return null;
    if(navigator.onLine===false){state=cachedOffline();error='Offline. Reconnect to verify instance membership.';return state;}
    force=force||forceNeeded;
    if(pending)return pending;
    if(!force&&state&&!state.offline&&Date.now()-state.checkedAt<FRESH)return state;
    const job=Promise.resolve().then(async()=>{
      try{
        await pc().ensureAiSession();
        const response=await pc().authFetch('/api/instance-welcome/access'+(force?'?refresh=1':''));
        if(!response.ok)throw Error(response.status===403?'Sign in with your Nostr account.':'Could not verify instance membership. Please retry.');
        const data=await response.json();
        if(data.pubkey!==pk||typeof data.qualified!=='boolean')throw Error('Invalid membership response. Please retry.');
        if(scope()===epoch){accept(data,pk);if(force)forceNeeded=false;}
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
    if(!data?.qualified||!allowed(view))throw Error(error||'Save your approved instance NIP-05 address in your profile to use '+apps[view]+'.');
  }
  /* THREE ANSWERS, NEVER TWO -- and this screen used to paint the same one for all of them.
     "We have not asked yet" and "we could not ask" are not "you are not a member", but the
     membership explanation ("apply for a name ... set your NIP-05 address ... Save") was the BODY of
     the card in every one of those cases. A member's verdict is UNKNOWN on every fresh load and
     after every profile change, so opening an app greeted them with the reasons they might be
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
    if(state?.address)lastAddress=state.address;
    if(state?.domain)lastDomain=state.domain;
    /* A CONFIRMED MEMBER IS NEVER OFFERED THE FIX, because there is nothing to fix. The panel is
       also reached by somebody who qualifies and is merely OFFLINE (a cached yes cannot open a
       server-backed app), and handing them a button that republishes the address they already
       publish would read as "your address is wrong" about the one thing that is right. */
    const granted=state?.address||lastAddress,domain=state?.domain||lastDomain;
    const address=verdict()==='yes'?'':granted;
    const refused=verdict()==='no',waiting=!refused&&!!pk&&!error;
    /* ONE BUTTON, BECAUSE THE NODE ALREADY KNOWS THE ANSWER.
       This screen used to say "open Edit profile, replace the NIP-05 / verified address field
       with this address, then Save" -- in EVERY app, every time. The instance granted the name,
       so it knows exactly what the profile should say; making somebody read a paragraph and
       retype a string the app is displaying to them is work the app should be doing.
       "Edit profile" stays for anyone who wants to change something else at the same time. */
    const useHtml=(address?'<button class="btn btn-neon ia-use">Use '+esc(address)+'</button> ':'');
    if(!refused)
      /* NOBODY HAS SAID NO. Name the app, say in one line what is happening, and -- if this instance
         has already granted an address -- still offer the one click that fixes it. Never the reasons
         somebody might be refused: that is a verdict this panel has not got. */
      feed.innerHTML='<section class="instance-app-gate"><h2>'+esc(apps[view])+'</h2>'+
        (waiting?'<p><span class="spinner spinner-inline"></span>'+(cachedOffline()?'Opening ':'Checking your access to ')+esc(apps[view])+'…</p>':'')+
        '<p class="ia-status" role="status"></p>'+
        (waiting?'':useHtml+'<button class="btn '+(address?'btn-ghost':'btn-neon')+' ia-retry">Check again</button>')+
        '</section>';
    else
      /* THE ANSWER IS NO, AND IT IS SAID IN FULL ONCE PER SCOPE. Reading the same three paragraphs
         on entering the second app teaches nobody anything; by the fifth, the buttons that fix it
         are the only part still worth the space. A profile or account change resets `told`, so the
         explanation comes back whenever the situation it explains has actually changed. */
      feed.innerHTML='<section class="instance-app-gate"><h2>'+esc(apps[view])+'</h2>'+
        (told?(address?'<p>Set your instance address to use '+esc(apps[view])+'.</p>'
                      :'<p>An approved name'+(domain?' on <strong>'+esc(domain)+'</strong>':' on this instance')+' unlocks '+esc(apps[view])+'.</p>')
             :'<p>This app is available after your approved instance NIP-05 address is saved in your profile.</p>')+
        (address?'<p class="iw-address">'+esc(address)+'</p>'+(told?'':'<p>This instance has already given you that address — it just is not in your profile yet.</p>'):
         told?'':'<p>Apply for a name'+(domain?' at <strong>'+esc(domain)+'</strong>':' on this instance')+'. After approval, open <strong>Edit profile</strong>, set your NIP-05 address, and <strong>Save</strong>.</p>')+
        (told?'':'<p class="muted small">Existing app permissions still apply.</p>')+'<p class="ia-status" role="status"></p>'+
        useHtml+
        '<button class="btn '+(address?'btn-ghost':'btn-neon')+' ia-profile">Edit profile</button> <button class="btn btn-ghost ia-apply">Apply for a name</button> <button class="btn btn-ghost ia-retry">Check again</button></section>';
    if(refused)told=true;
    const card=feed.querySelector('.instance-app-gate'),status=card.querySelector('.ia-status');
    status.textContent=!pk?'Sign in to continue.':error||'';
    const edit=card.querySelector('.ia-profile'),apply=card.querySelector('.ia-apply');
    if(edit){edit.disabled=!pk;edit.onclick=()=>p.editOwnProfile();}
    if(apply){apply.disabled=!pk;apply.hidden=!!address;}
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
        /* Carry the NIP-30 name emoji through. They live in the kind-0's TAGS, not in `meta`
           (Store keeps them off meta on purpose), so an empty array here destroys them — the same
           class of loss the guard above refuses, one field over. */
        const made=await p.publish(0, JSON.stringify(meta), (p.kind0Tags&&p.kind0Tags(pk))||[]);
        if(made&&made.ok===false) throw Error(made.msg||'Could not publish your profile.');
        status.textContent='Checking\u2026';
        await refresh(true);
        if(scope()!==epoch||!card.isConnected) return;
        if(state&&state.qualified){ p.toast&&p.toast('Address set \u2014 opening '+apps[view]); p.retryInstanceView(view); return; }
        status.textContent=error||'Saved. Relays can take a moment to serve it \u2014 press Check again.';
        use.disabled=false;
      }catch(e){ status.textContent=(e&&e.message)||'Could not set your address.'; use.disabled=false; }
    };
    if(apply) apply.onclick=async()=>{
      apply.disabled=true;status.textContent='Sending your application…';
      try{const result=await root.PCInstanceWelcome.apply();status.textContent=result.already?'Your approved address is '+result.address+'. Open Edit profile, set your NIP-05 / verified address, then Save.':'Application submitted. Your approval DM will include the address and profile steps.';}
      catch(e){status.textContent=e.message;apply.disabled=false;}
    };
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
