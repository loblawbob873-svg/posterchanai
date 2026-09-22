/* Media Center — the Jellyfin-backed library, player, subtitles and watch progress (sidebar →
 * Media Center). Split out of app.js; its chrome lives in media-center-ui.js as before.
 *
 * Loaded on first use: app.js keeps a small block of entry points (search for `_mediaCenterDeps`)
 * and builds this factory the first time the screen opens. The code below is BYTE-IDENTICAL to what
 * it replaced in app.js apart from its reads of app.js's live `let` bindings, which the parser
 * rewrote to `S.<name>` (getters/setters on `dep.state`) at exact identifier offsets. The window
 * `resize` listener that refits the player is registered when the module is built rather than at
 * boot — it has nothing to fit until then.
 */
window.PCMediaCenterFactory = function(dep){
  const S = dep.state;   // live app.js bindings: S.CFG, S.VIEW, S._aiAuth, S._aiToken
  const {
    $, _instanceBase, _setAiToken, attachUserAutocomplete, copyValue, enc, ensureAiSession,
    loadHls, toast,
  } = dep;


  let _mediaCenterLibraryTab=null;
  let _mediaCenterRenderGeneration=0;
  let _mediaCenterSubtitleUrl=null;
  let _mediaCenterFolderCleanup=()=>{};
  let _mediaCenterPollTimer=null;
  let _mediaCenterSession=null, _mediaCenterPlayGeneration=0, _mediaCenterHls=null, _mediaCenterArtObserver=null, _mediaCenterArtUrls=[], _mediaCenterArtGeneration=0;
  async function _mediaCenterFetch(url, opts={}){
    const request=async()=>{
      const controller=new AbortController();
      const timeout=setTimeout(()=>controller.abort(),url.includes('subtitle-')?120000:20000);
      const abort=()=>controller.abort();opts.signal?.addEventListener('abort',abort,{once:true});
      if(opts.signal?.aborted)controller.abort();
      try{
        const headers=new Headers(opts.headers||{});
        if(S._aiToken)headers.set('Authorization','Bearer '+S._aiToken);
        return await fetch(url,{credentials:'include',...opts,headers,signal:controller.signal});
      }catch(error){
        if(error.name==='AbortError')throw new Error('The media server took too long to respond. Try again.');
        throw error;
      }finally{clearTimeout(timeout);opts.signal?.removeEventListener('abort',abort);}
    };
    const sentToken=S._aiToken;
    let response=await request();
    if(response.status===401){
      if(S._aiToken===sentToken){S._aiAuth=null;_setAiToken('');}
      const loading=typeof document==='undefined'?null:document.getElementById('mc-loading');
      if(loading)loading.textContent='Signing in to Media Center… Check your signer if it asks for approval.';
      const force=Boolean(_mediaCenterFetch.retryAuth);_mediaCenterFetch.retryAuth=false;
      let timer;
      try{
        await Promise.race([ensureAiSession({force}),new Promise((_,reject)=>{
          timer=setTimeout(()=>{_mediaCenterFetch.retryAuth=true;reject(new Error('Sign-in timed out. Check your signer, then press Retry.'));},30000);
        })]);
      }finally{clearTimeout(timer);}
      response=await request();
    }
    return response;
  }
  function clearMediaCenterArt(){
    _mediaCenterArtGeneration++;
    if(_mediaCenterArtObserver){_mediaCenterArtObserver.disconnect();_mediaCenterArtObserver=null;}
    for(const url of _mediaCenterArtUrls)URL.revokeObjectURL(url);
    _mediaCenterArtUrls=[];
  }
  function releaseMediaCenterSession(url){
    if(!url)return Promise.resolve();
    const ticket=new URL(url,_instanceBase()||location.origin).searchParams.get('ticket');
    return _mediaCenterFetch('/api/media-center/sessions/stop',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({ticket})}).catch(()=>{});
  }
  let _mediaCenterSaveProgress=null;
  function mediaResumePosition(name, seconds){
    if(!(seconds>0))return Promise.resolve(0);
    const at=Math.floor(seconds),label=[Math.floor(at/3600),Math.floor(at/60)%60,at%60].map(value=>String(value).padStart(2,'0')).join(':');
    const dialog=document.createElement('dialog');dialog.className='mc-resume-dialog';
    dialog.innerHTML='<form method="dialog"><h3>'+enc(name)+'</h3><p>Resume at '+label+'?</p><div class="mc-player-toolbar"><button class="btn btn-neon" value="resume">Resume</button><button class="btn btn-ghost" value="start">Start from beginning</button><button class="btn btn-ghost" value="cancel">Cancel</button></div></form>';
    document.body.append(dialog);dialog.returnValue='cancel';
    return new Promise(resolve=>{
      let settled=false;const finish=value=>{if(settled)return;settled=true;dialog.close();dialog.remove();resolve(value==='resume'?seconds:value==='start'?0:null);};
      dialog.querySelectorAll('button').forEach(button=>button.onclick=event=>{event.preventDefault();finish(button.value);});
      dialog.oncancel=event=>{event.preventDefault();finish('cancel');};dialog.onclose=()=>finish(dialog.returnValue);dialog.showModal();
    });
  }
  let _mediaCenterUserPickers=[];
  function stopMediaCenter(clearArt=true){
    if(clearArt){_mediaCenterUserPickers.splice(0).forEach(dispose=>dispose());}
    if(_mediaCenterSaveProgress){_mediaCenterSaveProgress();_mediaCenterSaveProgress=null;}
    if(_mediaCenterSubtitleUrl){URL.revokeObjectURL(_mediaCenterSubtitleUrl);_mediaCenterSubtitleUrl=null;}
    document.querySelectorAll('#mc-player track').forEach(track=>track.remove());
    _mediaCenterPlayGeneration++;
    const session=_mediaCenterSession;_mediaCenterSession=null;
    if(clearArt){_mediaCenterFolderCleanup();clearTimeout(_mediaCenterPollTimer);_mediaCenterPollTimer=null;clearMediaCenterArt();}
    if(_mediaCenterHls){ _mediaCenterHls.destroy(); _mediaCenterHls=null; }
    const video=document.getElementById('mc-player');
    if(video){video.onpause=null;video.ontimeupdate=null;video.onended=null; video.pause(); video.removeAttribute('src'); video.load(); }
    return releaseMediaCenterSession(session);
  }
  /* THE PLAYER FITS THE SPACE IT IS SHOWN IN. The video used to be `max-height:65vh` under a
   * toolbar of three full-width selects: on a phone that put ~250px of controls ABOVE a 330px
   * picture, and inside a desktop window `vh` is the SCREEN, not the window, so the picture ran off
   * the bottom of any window shorter than the monitor. Now the title, the picture and the compact
   * controls are sized together to the scroller they live in (the `#feed`, whether that is the
   * phone's content area or an os.js window body — the window ADOPTS the feed, so there is no
   * iframe and no `vh` that means the window), and the video takes whatever height that leaves.
   * Measured in visual px and converted back through the element's own zoom, because `body{zoom}`
   * sits between getBoundingClientRect and a px written into a style (client-display-scaling). */
  let _mediaCenterFitRO=null;
  function _mediaCenterFitPlayer(){
    const box=document.getElementById('mc-playback'),stage=box&&box.querySelector('.mc-stage');
    if(!box||!stage||box.hidden)return;
    if(document.fullscreenElement===box){box.style.removeProperty('--mc-max-h');return;}
    let sc=box.parentElement;
    while(sc&&sc!==document.body&&sc!==document.documentElement){
      const o=getComputedStyle(sc).overflowY;
      if(o==='auto'||o==='scroll')break;
      sc=sc.parentElement;
    }
    const vh=window.innerHeight||document.documentElement.clientHeight;
    let top=0,bottom=vh;
    if(sc&&sc!==document.body&&sc!==document.documentElement){
      const r=sc.getBoundingClientRect();top=Math.max(0,r.top);bottom=Math.min(vh,r.bottom);
    }
    const br=box.getBoundingClientRect(),sr=stage.getBoundingClientRect();
    const zoom=box.offsetHeight?br.height/box.offsetHeight:1;
    const chrome=br.height-sr.height;               // title + controls + padding, visual px
    // From where the player starts, not the scroller top (page padding pushed controls off-screen).
    const avail=(bottom-Math.max(top,br.top))-chrome-8;
    const px=Math.max(160,Math.floor(avail/(zoom||1)));
    box.style.setProperty('--mc-max-h',px+'px');
    // Re-fit when the scroller (a window being resized) or the title (a long name wrapping onto
    // another line) changes size. Re-observed only when the TARGETS change: observing is itself a
    // notification, so re-observing on every fit would call this for ever.
    const title=box.querySelector('#mc-playing'),scroller=sc&&sc!==document.body&&sc!==document.documentElement?sc:null;
    if(typeof ResizeObserver!=='undefined'&&(!_mediaCenterFitRO||_mediaCenterFitRO.t!==title||_mediaCenterFitRO.s!==scroller)){
      if(_mediaCenterFitRO)_mediaCenterFitRO.ro.disconnect();
      const ro=new ResizeObserver(()=>_mediaCenterFitPlayer());
      if(scroller)ro.observe(scroller);
      if(title)ro.observe(title);
      _mediaCenterFitRO={ro,t:title,s:scroller};
    }
  }
  if(typeof window!=='undefined')window.addEventListener('resize',()=>_mediaCenterFitPlayer());
  // Watching is its own screen: set the library aside so the player fills the view (it sat under it).
  let _mcWatch=null;
  function _mediaCenterWatch(on){
    const box=document.getElementById('mc-playback'),gal=box&&box.closest('.mc-gallery');
    if(!box||!gal)return;
    if(on){
      if(_mcWatch&&_mcWatch.box===box)return;
      let sc=box.parentElement;
      while(sc&&sc!==document.body){const o=getComputedStyle(sc).overflowY;if(o==='auto'||o==='scroll')break;sc=sc.parentElement;}
      const scroller=sc&&sc!==document.body?sc:document.scrollingElement;
      const hidden=[];
      for(let n=box;n&&n!==gal;n=n.parentElement){
        for(const sib of n.parentElement.children)if(sib!==n&&!sib.classList.contains('mc-watch-hide')){sib.classList.add('mc-watch-hide');hidden.push(sib);}
      }
      _mcWatch={box,gal,hidden,scroller,top:scroller?scroller.scrollTop:0};
      gal.classList.add('mc-watching');
      if(scroller)scroller.scrollTop=0;
    }else{
      if(!_mcWatch)return;
      const w=_mcWatch;_mcWatch=null;
      for(const el of w.hidden)el.classList.remove('mc-watch-hide');
      w.gal.classList.remove('mc-watching');
      if(w.scroller)w.scroller.scrollTop=w.top;
    }
    _mediaCenterFitPlayer();
  }
  async function renderMediaCenter(openLibraryId=null){
    const renderGeneration=++_mediaCenterRenderGeneration;
    stopMediaCenter();
    const feed=$('#feed');
    feed.innerHTML='<div class="empty" id="mc-loading" role="status">Loading Media Center…</div>';
    const api=async(path='',method='GET',body)=>{
      const r=await _mediaCenterFetch('/api/media-center'+path,{method,keepalive:path.includes('/progress/'),headers:{'Content-Type':'application/json'},
        ...(body===undefined?{}:{body:JSON.stringify(body)})});
      if(!r.ok){const e=await r.json().catch(()=>({}));const error=new Error(typeof e.detail==='string'?e.detail:'Media Center request failed');error.status=r.status;throw error;}
      return r.json();
    };
    try{
      const data=await api();
      if(S.VIEW!=='media-center'||renderGeneration!==_mediaCenterRenderGeneration)return;
      const ownCount=data.libraries.filter(lib=>!lib.shared_with_me).length,sharedCount=data.libraries.length-ownCount;
      if(openLibraryId){const requested=data.libraries.find(lib=>lib.id===openLibraryId);if(requested)_mediaCenterLibraryTab=requested.shared_with_me?'shared':'mine';}
      // A VIEWER WHO CANNOT CREATE A LIBRARY CAN NEVER HAVE ONE OF THEIR OWN, so "My libraries" is
      // never the tab to land them on and "No libraries of your own yet." is never the thing to tell
      // them. With nothing shared yet that message was the whole screen: it answers a question they
      // did not ask, names nothing they can act on, and reads exactly like "this server has no media"
      // — which is how a share that was never made looked identical to one that was.
      if(!_mediaCenterLibraryTab)_mediaCenterLibraryTab=ownCount||(!sharedCount&&data.can_create)?'mine':'shared';
      const visibleLibraries=data.libraries.filter(lib=>Boolean(lib.shared_with_me)===(_mediaCenterLibraryTab==='shared'));

      feed.innerHTML=`<div class="mc-gallery"><div class="xdc-gal-top"><div class="muted"><h2>Media Center</h2>Your movies, shows and music. Pick a library and press play.</div><span class="mc-private">Private · Server library</span></div><div class="mc-library-tabs" role="tablist" aria-label="Media libraries"><button id="mc-tab-mine" type="button" role="tab" aria-controls="mc-library-panel" aria-selected="${_mediaCenterLibraryTab==='mine'}" tabindex="${_mediaCenterLibraryTab==='mine'?0:-1}">My libraries <span>${ownCount}</span></button><button id="mc-tab-shared" type="button" role="tab" aria-controls="mc-library-panel" aria-selected="${_mediaCenterLibraryTab==='shared'}" tabindex="${_mediaCenterLibraryTab==='shared'?0:-1}">Shared with me <span>${sharedCount}</span></button></div><div id="mc-library-panel" role="tabpanel" aria-labelledby="mc-tab-${_mediaCenterLibraryTab}"><div class="mc-tools">
        ${data.can_create?`<details class="mc-tool-card"><summary><span class="mc-tool-icon"><svg class="ic"><use href="#i-folder"></use></svg></span><span><b>Add a server folder</b><small>Bring your movies and music into the library</small></span><span class="mc-tool-expand">+</span></summary><form id="mc-add" class="mc-tool-body">
          <p><label>Library name <input name="name" required maxlength="150" placeholder="Movies"></label></p>
          <p id="mc-roots" class="mc-config-note muted small">Checking allowed folders on the media server…</p>
          <p><label>Server folder <input name="folder" required placeholder="/var/lib/posterchanai/media/Movies"></label></p>
          <p><label>Transcoding <select name="encoder"><option value="auto">Automatic GPU / CPU</option><option value="cpu">CPU</option><option value="nvidia">NVIDIA</option><option value="amd">AMD</option><option value="vaapi">VA-API GPU</option></select></label></p>
          <button type="submit" class="btn btn-neon">Add to library</button><p class="muted small">Scanning continues in the background. Titles appear as they are found.</p></form></details>`:''}
        ${data.can_create?`<details class="mc-tool-card"><summary><span class="mc-tool-icon"><svg class="ic"><use href="#i-bars"></use></svg></span><span><b>Bandwidth &amp; resources</b><small id="mc-limit-summary">200 KB/s per user · GPU / CPU transcoding</small></span><span class="mc-tool-expand">+</span></summary><form id="mc-limits" class="mc-tool-body">
          <p class="muted">Hard limits, enforced by the server in kbps (1,000 kbps = 1 Mbps). The per-user cap is shared across their tabs. The server cap covers all viewers.</p>
          <p><label>Total bandwidth <input name="server_kbps" type="number" min="650" max="1000000" required></label></p>
          
          <p><label>Simultaneous streams <input name="max_streams" type="number" min="1" max="100" required></label></p>
          <p><label>Concurrent transcodes <input name="max_transcodes" type="number" min="1" max="16" required></label></p>
          <p><label>Segment cache (MB) <input name="cache_mb" type="number" min="32" max="1048576" required></label></p>
          <button type="submit" class="btn btn-neon">Save limits</button></form></details>`:''}
        <details id="mc-jellyfin" class="mc-tool-card"><summary><span class="mc-tool-icon"><svg class="ic"><use href="#i-tv"></use></svg></span><span><b>Connect an app</b><small>Pair a Jellyfin app with Quick Connect</small></span><span class="mc-tool-expand">+</span></summary><div class="mc-tool-body">
          <p class="mc-step"><span>1</span> Add this server in your Jellyfin app</p><code id="mc-jellyfin-server" class="mc-server-address"></code><p class="mc-step"><span>2</span> Choose Quick Connect, then enter its code below</p>
          <form id="mc-jellyfin-approve"><label>Code shown in your app <input name="code" inputmode="numeric" autocomplete="off" pattern="[0-9]{6}" minlength="6" maxlength="6" required placeholder="123456"></label>
            <p><button type="submit" class="btn btn-neon">Connect this app</button></p></form>
          <p class="muted">Only approve a code displayed by an app you are connecting. It gets access to your shared Media Center libraries.</p>
          <h4>Connected Jellyfin devices</h4><div id="mc-jellyfin-devices" role="status">Loading devices…</div><button id="mc-jellyfin-refresh" type="button" class="btn btn-ghost">Refresh devices</button>
          <button id="mc-jellyfin-revoke" type="button" class="btn btn-ghost">Disconnect all apps</button>
        </div></details>
        </div><p id="mc-status" class="mc-status muted" role="status"></p><div id="mc-libraries" class="mc-libraries"></div>
        <div id="mc-playback" class="mc-playback" hidden><h3 id="mc-playing" class="mc-now-title"></h3>
          <div class="mc-stage"><video id="mc-player" controls playsinline tabindex="0" aria-label="Media player" preload="metadata"></video></div>
          <div class="mc-player-toolbar mc-controls"><div class="mc-ctl-btns"><button id="mc-fullscreen" class="btn btn-ghost" type="button" aria-label="Enter full screen">Full screen</button><button id="mc-close-player" class="btn btn-ghost" type="button">Close player</button></div>
          <div class="mc-ctl-sel"><label><span>Quality</span><select id="mc-quality"><option value="auto">Best quality within limit</option>
            <option value="240p">240p · ~0.4 Mbps</option><option value="360p">360p · ~0.7 Mbps</option><option value="480p">480p · ~1 Mbps</option>
            <option value="720p">720p · ~2.6 Mbps</option><option value="1080p">1080p · ~5.6 Mbps</option></select></label><label><span>Audio</span><select id="mc-audio"><option value="-1">Default</option></select></label><label><span>Subtitles</span><select id="mc-subtitles"><option value="-1">Off</option></select></label></div></div>
        </div><div class="mc-browse"><label class="mc-search" hidden>Search this library <input id="mc-search" type="search" class="input" placeholder="Find a title or folder…"></label></div><nav id="mc-folder-nav" aria-label="Media folders"></nav><div id="mc-items"></div></div></div>`;
      for(const tab of feed.querySelectorAll('[role=tab]')){
        tab.onclick=async()=>{_mediaCenterLibraryTab=tab.id==='mc-tab-shared'?'shared':'mine';await renderMediaCenter();document.getElementById(tab.id)?.focus({preventScroll:true});};
        tab.onkeydown=e=>{if(['ArrowLeft','ArrowRight','Home','End'].includes(e.key)){e.preventDefault();const next=e.key==='Home'?'mine':e.key==='End'?'shared':tab.id==='mc-tab-mine'?'shared':'mine';document.getElementById('mc-tab-'+next).click();}};
      }
      for(const card of feed.querySelectorAll('.mc-tool-card'))card.ontoggle=()=>{
        if(card.open)for(const other of feed.querySelectorAll('.mc-tool-card'))if(other!==card)other.open=false;
      };
      const status=$('#mc-status');
      const fullscreen=$('#mc-fullscreen'),playerBox=$('#mc-playback');
      fullscreen.onclick=async()=>{
        try{
          if(document.fullscreenElement){await document.exitFullscreen();return;}
          if(playerBox.requestFullscreen)await playerBox.requestFullscreen();
          else if($('#mc-player').webkitEnterFullscreen)$('#mc-player').webkitEnterFullscreen();
          else status.textContent='Use your device’s video player full-screen control.';
        }catch(_){status.textContent='Full screen is unavailable in this window. Use the video player controls.';}
      };
      playerBox.onfullscreenchange=()=>{_mediaCenterFitPlayer();const active=document.fullscreenElement===playerBox;fullscreen.textContent=active?'Exit full screen':'Full screen';fullscreen.setAttribute('aria-label',fullscreen.textContent);};
      playerBox.onkeydown=e=>{
        if(e.target.matches('input,select,textarea'))return;
        if(e.key.toLowerCase()==='f'){e.preventDefault();fullscreen.click();}
      };
      $('#mc-close-player').onclick=async()=>{
        if(document.fullscreenElement===playerBox)await document.exitFullscreen().catch(()=>{});
        stopMediaCenter(false);playerBox.hidden=true;_mediaCenterWatch(false);
      };
      const act=async(button,fn)=>{button.disabled=true;status.textContent='Working…';try{await fn();status.textContent='';}catch(e){status.textContent=e.message;}finally{button.disabled=false;}};
      $('#mc-jellyfin-server').textContent=(_instanceBase()||location.origin).replace(/\/$/,'')+'/jellyfin';
      const loadDevices=async()=>{
        const result=await api('/jellyfin-account');const list=$('#mc-jellyfin-devices');
        if(!list||renderGeneration!==_mediaCenterRenderGeneration)return;
        list.replaceChildren();
        if(!result.devices?.length){list.textContent='No connected devices.';return;}
        for(const device of result.devices){
          const row=document.createElement('div');row.className='mc-device-row';
          const info=document.createElement('div'),name=document.createElement('strong'),detail=document.createElement('small');
          name.textContent=device.name;detail.textContent=[device.client,device.version,device.created?'Connected '+new Date(device.created*1000).toLocaleDateString():''].filter(Boolean).join(' · ');
          info.append(name,detail);const revoke=document.createElement('button');revoke.className='btn btn-ghost';revoke.textContent='Revoke';
          revoke.onclick=()=>act(revoke,async()=>{const response=await _mediaCenterFetch('/api/media-center/jellyfin-account/devices/'+encodeURIComponent(device.id),{method:'DELETE'});if(!response.ok)throw new Error('Unable to revoke device');await loadDevices();});
          row.append(info,revoke);list.append(row);
        }
      };
      $('#mc-jellyfin-refresh').onclick=e=>act(e.currentTarget,loadDevices);
      loadDevices().catch(error=>{const list=$('#mc-jellyfin-devices');if(list)list.textContent=error.message;});
      $('#mc-jellyfin-approve').onsubmit=e=>{e.preventDefault();const form=e.currentTarget;act(form.querySelector('button'),async()=>{
        await api('/jellyfin-account/authorize','POST',{code:form.elements.code.value});
        await loadDevices();form.reset();toast('Jellyfin app approved. Return to the app to finish connecting.');
      });};
      $('#mc-jellyfin-revoke').onclick=e=>act(e.currentTarget,async()=>{
        const response=await _mediaCenterFetch('/api/media-center/jellyfin-account',{method:'DELETE'});
        if(!response.ok)throw new Error('Could not disconnect Jellyfin apps');
        await loadDevices();toast('All Jellyfin apps disconnected.');
      });
      const rootsNote=$('#mc-roots');
      if(rootsNote){
        try{
          const config=await api('/roots');if(S.VIEW!=='media-center'||renderGeneration!==_mediaCenterRenderGeneration)return;
          rootsNote.textContent='Media server: '+config.host+'. Allowed folders: '+
            (config.roots.map(root=>root.path+(!root.exists?' (missing)':!root.readable?' (not readable)':'')).join(', ')||'none configured')+
            '. Use a folder inside one of these paths. Change POSTERCHANAI_MEDIA_ROOTS on that server to allow another folder.';
        }catch(e){rootsNote.textContent=e.message;}
      }
      const limitsForm=$('#mc-limits');
      if(limitsForm){
        const limits=await api('/limits');if(S.VIEW!=='media-center'||renderGeneration!==_mediaCenterRenderGeneration)return;
        $('#mc-limit-summary').textContent=Math.round(limits.server_kbps/8)+' KB/s total · '+limits.max_streams+' simultaneous streams';
        for(const [key,value] of Object.entries(limits)){if(limitsForm.elements[key])limitsForm.elements[key].value=value;}
        limitsForm.onsubmit=e=>{e.preventDefault();act(limitsForm.querySelector('button'),async()=>{
          await api('/limits','PUT',Object.fromEntries(Array.from(new FormData(limitsForm),([k,v])=>[k,Number(v)])));
          await renderMediaCenter();
        });};
      }
      for(const option of Array.from($('#mc-quality').options)){if(option.value!=='auto'&&!data.profiles.includes(option.value))option.remove();}
      const libraryRows=new Map();
      let folderArtObserver=null, folderArtQueue=[], folderArtActive=0;
      const folderArtUrls=[];
      const clearFolderArt=()=>{folderArtObserver?.disconnect();folderArtObserver=null;folderArtQueue=[];for(const url of folderArtUrls)URL.revokeObjectURL(url);folderArtUrls.length=0;};
      _mediaCenterFolderCleanup=clearFolderArt;
      const fillFolderArt=(card,ids)=>{
        if(card.dataset.artReady||!ids?.length)return;
        card.dataset.artReady='true';card._artIds=ids.slice(0,3);folderArtObserver?.observe(card);
      };
      const drainFolderArt=()=>{
        while(folderArtActive<2&&folderArtQueue.length){
          const {card,id}=folderArtQueue.shift();if(!card.isConnected||S.VIEW!=='media-center')continue;
          folderArtActive++;
          _mediaCenterFetch('/api/media-center/'+card.dataset.library+(id==='folder-art'?'/folder-art?path='+encodeURIComponent(card.dataset.path):'/art/'+id)).then(r=>r.ok?r.blob():null).then(blob=>{
            if(!blob||!card.isConnected||S.VIEW!=='media-center')return;
            const url=URL.createObjectURL(blob);folderArtUrls.push(url);
            const img=document.createElement('img');img.alt='';img.decoding='async';img.src=url;
            if(id==='folder-art')card.querySelector('.mc-directory-art').querySelectorAll('img').forEach(image=>image.remove());
            if(id==='folder-art'||!card.dataset.folderPng)card.querySelector('.mc-directory-art').append(img);card.classList.add('has-art');
          }).catch(()=>{}).finally(()=>{folderArtActive--;drainFolderArt();});
        }
      };
      let currentFolder='.', folderLibrary=null, folderRequest=0;
      const browseFolder=async(lib,path)=>{
        const request=++folderRequest;
        const result=await api('/'+lib.id+'/folders?path='+encodeURIComponent(path));
        if(request!==folderRequest||S.VIEW!=='media-center')return;
        currentFolder=result.path;folderLibrary=lib.id;
        clearFolderArt();
        folderArtObserver=new IntersectionObserver(entries=>{
          for(const entry of entries)if(entry.isIntersecting){folderArtObserver.unobserve(entry.target);for(const id of entry.target._artIds||[])folderArtQueue.push({card:entry.target,id});}
          drainFolderArt();
        },{rootMargin:'150px'});
        const nav=$('#mc-folder-nav');nav.replaceChildren();
        const trail=document.createElement('div');trail.className='mc-folder-trail';nav.append(trail);
        const crumb=(label,path)=>{const button=document.createElement('button');button.type='button';button.className='btn btn-ghost';button.textContent=label;button.onclick=()=>act(button,()=>browseFolder(lib,path));trail.append(button);};
        crumb(lib.name,'.');
        if(currentFolder!=='.'){let prefix='';for(const part of currentFolder.split('/')){prefix=prefix?prefix+'/'+part:part;crumb(part,prefix);}}
        const grid=document.createElement('div');grid.className='mc-directory-grid';nav.append(grid);
        for(const folder of result.folders){
          const button=document.createElement('button');button.type='button';button.className='mc-directory';
          button.dataset.path=folder.path;button.dataset.library=lib.id;
          let hue=0;for(const character of folder.name)hue=(hue*31+character.codePointAt(0))%360;button.style.setProperty('--folder-hue',hue);
          button.innerHTML='<span class="mc-directory-art"><span class="mc-directory-initial" aria-hidden="true"></span></span><span class="mc-directory-label"><b></b><small><svg class="ic" aria-hidden="true"><use href="#i-folder"></use></svg> Browse folder <span aria-hidden="true">›</span></small></span>';
          button.querySelector('b').textContent=folder.name;
          button.querySelector('.mc-directory-initial').textContent=folder.name.slice(0,2).toLocaleUpperCase();
          if(folder.has_folder_art)button.dataset.folderPng='true';
          fillFolderArt(button,folder.has_folder_art?['folder-art']:folder.art);
          button.onclick=()=>act(button,()=>browseFolder(lib,folder.path));grid.append(button);
        }
        const search=$('#mc-search');search.value='';search.oninput?.();
      };
      const schedulePoll=(delay=3000)=>{
        clearTimeout(_mediaCenterPollTimer);
        _mediaCenterPollTimer=setTimeout(async()=>{
          if(S.VIEW!=='media-center'||!libs.isConnected)return;
          if(document.hidden){schedulePoll();return;}
          try{
            const latest=await api();if(S.VIEW!=='media-center'||!libs.isConnected)return;
            let pending=false;
            for(const fresh of latest.libraries){
              const entry=libraryRows.get(fresh.id);if(!entry)continue;
              Object.assign(entry.lib,fresh);entry.update();
              const list=$('#mc-items');
              if(list.dataset.library===fresh.id&&list.dataset.revision!==fresh.revision){
                if(!entry.open.disabled)await entry.open.onclick();
                else pending=true;
              }
              if(fresh.scan?.state==='running')pending=true;
            }
            if(pending)schedulePoll();
          }catch(_){if(S.VIEW==='media-center'&&libs.isConnected)schedulePoll(6000);}
        },delay);
      };
      const add=$('#mc-add');
      if(add)add.onsubmit=e=>{e.preventDefault();act(add.querySelector('button'),async()=>{
        const library=await api('','POST',Object.fromEntries(new FormData(add)));
        if(S.VIEW==='media-center')await renderMediaCenter(library.id);
      });};
      const libs=$('#mc-libraries');
      if(!visibleLibraries.length){
        libs.replaceChildren();$('#mc-folder-nav').hidden=true;
        if(_mediaCenterLibraryTab!=='shared')libs.textContent='No libraries of your own yet.';
        else{
          // The server counts what it holds and cannot show this viewer, so the two empty states are
          // told apart by a measurement rather than by a guess. An older or unpatched media node
          // sends neither field; the sentence then falls back to what it always said.
          const waiting=Number(data.unshared)||0,message=document.createElement('p');
          message.className='muted';
          message.textContent=waiting?('No libraries have been shared with you yet. This server has '+waiting+
            (waiting===1?' library':' libraries')+' you cannot open — send its owner the key below and ask them to share.')
            :'No libraries have been shared with you yet. Ask the owner to share with your Nostr public key.';
          libs.append(message);
          if(data.viewer){
            // THE KEY THE SERVER ACTUALLY CHECKED, not one the page derived: the owner pastes this
            // exact string into "Share with Nostr users", and a delegated proxy hop is the one place
            // it could differ from the key this browser signed in with.
            const row=document.createElement('p');row.className='mc-share-key';
            const key=document.createElement('code');key.textContent=data.viewer;
            const copy=document.createElement('button');copy.type='button';copy.className='btn btn-ghost small';
            copy.textContent='Copy my key';
            copy.onclick=()=>copyValue(data.viewer,'key copied — send it to the library owner');
            row.append(key,' ',copy);libs.append(row);
          }
        }
      }
      for(const lib of visibleLibraries){
        const row=document.createElement('div');row.className='mc-library';
        const open=document.createElement('button');open.className='btn btn-ghost mc-library-open';row.append(open);
        const progress=document.createElement('span');progress.className='muted small';progress.setAttribute('role','status');row.append(progress);
        let scanButton=null;
        const update=()=>{
          open.textContent=lib.name+' · '+(lib.count||0);
          progress.textContent=lib.scan?.state==='running'?'Scanning · '+(lib.scan.count||0)+' found':lib.scan?.state==='failed'?lib.scan.error:lib.scan?.state==='interrupted'?'Scan interrupted · saved titles available · Rescan to continue':'';
          if(scanButton)scanButton.disabled=lib.scan?.state==='running';
        };
        libraryRows.set(lib.id,{lib,open,update});update();
        if(lib.can_manage){
          const scan=document.createElement('button');scanButton=scan;scan.className='btn btn-ghost small';scan.textContent='Rescan';row.append(' ',scan);update();
          scan.onclick=()=>act(scan,async()=>{await api('/'+lib.id+'/scan','POST');lib.scan={state:'running',count:0};update();schedulePoll(0);toast('Scan started. You can keep browsing.');});
          const share=document.createElement('details');share.className='mc-share';
          /* SAY WHAT IT TAKES, AND SAY THAT IT WORKED.
           *
           * "Enter npubs" was true and unhelpful: the owner knows the person as
           * `matthew@poster.place` — a name THIS node granted — and had no way to type it, so they
           * used the profile permission instead. That grants the Media Center FEATURE and not the
           * library, and the share silently never happened. The server accepts a granted name now;
           * this is the half that tells anyone so.
           *
           * And the save said NOTHING on success, so "I shared it" and "I thought I shared it" felt
           * identical — which is how this went a day without being noticed. It reports what the
           * server stored, which is also the only honest confirmation: a name that resolved to
           * nobody cannot be counted. */
          share.innerHTML='<summary>Share with Nostr users</summary><p class="muted">Type a username to find people. One per line: an npub, a public key, or a name this server granted (e.g. <b>someone@'+enc((typeof S.CFG!=='undefined'&&S.CFG&&S.CFG.nip05_domain)||'this server')+'</b>). They sign in here. Remove an entry to revoke access. Nothing is federated.</p><textarea aria-label="Nostr public keys" rows="3" style="width:100%"></textarea><button>Save sharing</button><span class="mc-share-done muted small" style="margin-inline-start:8px"></span>';
          share.querySelector('textarea').value=lib.shared_with.join('\n');
          _mediaCenterUserPickers.push(attachUserAutocomplete(share.querySelector('textarea'),{multiline:true}));
          share.querySelector('button').onclick=()=>act(share.querySelector('button'),async()=>{
            const saved=await api('/'+lib.id+'/sharing','PUT',{shared_with:share.querySelector('textarea').value.split(/[\s,]+/).filter(Boolean)});
            const n=((saved&&saved.shared_with)||[]).length;
            const said=share.querySelector('.mc-share-done');
            if(said){ said.textContent = n ? ('✓ shared with '+n+' '+(n===1?'person':'people')) : '✓ sharing cleared'; }
            toast(n ? ('Shared with '+n+' '+(n===1?'person':'people')) : 'Sharing cleared');
          });row.append(share);
        }
        open.onclick=()=>act(open,async()=>{
          // Refresh the directory list too: moved/renamed folders must not survive a rescan.
          try{await browseFolder(lib,folderLibrary===lib.id?currentFolder:'.');}
          catch(error){if(error.status===400&&currentFolder!=='.')await browseFolder(lib,'.');else throw error;}
          const result=await api('/'+lib.id+'/items');if(S.VIEW!=='media-center'||renderGeneration!==_mediaCenterRenderGeneration)return;
          const list=$('#mc-items'),refresh=list.dataset.library===lib.id;
          if(!refresh){clearMediaCenterArt();list.replaceChildren();}
          else if(_mediaCenterArtObserver)_mediaCenterArtObserver.disconnect();
          for(const card of $('#mc-folder-nav').querySelectorAll('.mc-directory'))fillFolderArt(card,result.items.filter(item=>item.folder===card.dataset.path||item.folder?.startsWith(card.dataset.path+'/')).slice(0,3).map(item=>item.id));
          list.dataset.library=lib.id;list.dataset.revision=result.revision||lib.revision||'';
          if(refresh&&list.firstChild?.nodeType===Node.TEXT_NODE)list.replaceChildren();
          const artGeneration=_mediaCenterArtGeneration, artQueue=[];let artActive=0;
          const drainArt=()=>{
            while(artActive<2&&artQueue.length&&artGeneration===_mediaCenterArtGeneration){
              const card=artQueue.shift();artActive++;
              _mediaCenterFetch('/api/media-center/'+lib.id+'/art/'+card.dataset.item).then(r=>r.ok?r.blob():null).then(blob=>{
                if(!blob||!card.isConnected||artGeneration!==_mediaCenterArtGeneration)return;
                const url=URL.createObjectURL(blob);_mediaCenterArtUrls.push(url);
                const image=document.createElement('img');image.alt='';image.decoding='async';image.src=url;
                card.querySelector('.xdc-cover').replaceChildren(image);
              }).catch(()=>{}).finally(()=>{artActive--;drainArt();});
            }
          };
          _mediaCenterArtObserver=new IntersectionObserver(entries=>{
            for(const entry of entries){if(entry.isIntersecting){_mediaCenterArtObserver.unobserve(entry.target);artQueue.push(entry.target);}}
            drainArt();
          },{rootMargin:'150px'});
          let folder=null,grid=null;
          const existing=new Map(Array.from(list.querySelectorAll('.mc-tile'),card=>[card.dataset.item,card]));
          const sections=new Map(Array.from(list.querySelectorAll('.mc-folder'),section=>[section.dataset.folder,section]));
          const remaining=new Set(result.items.map(item=>item.id));
          for(const [id,card] of existing)if(!remaining.has(id))card.remove();
          const search=$('#mc-search');if(!refresh)search.value='';search.parentElement.hidden=false;
          search.oninput=()=>{
            const query=search.value.trim().toLocaleLowerCase();
            for(const section of list.children){let visible=0;for(const card of section.querySelectorAll('.mc-tile')){card.hidden=query?!card.dataset.search.includes(query):section.dataset.folder!==currentFolder;if(!card.hidden)visible++;}section.hidden=!visible;}
          };
          for(const button of libs.querySelectorAll('.mc-library-open'))button.classList.toggle('active',button===open);
          for(const item of result.items){
            if(folder!==item.folder){
              folder=item.folder;let section=sections.get(folder);
              if(!section){
                section=document.createElement('section');section.className='mc-folder';section.dataset.folder=folder;
                const heading=document.createElement('h3');heading.textContent=folder==='.'?lib.name:folder;section.append(heading);
                grid=document.createElement('div');grid.className='xdc-grid';section.append(grid);sections.set(folder,section);
              }else grid=section.querySelector('.xdc-grid');
              list.append(section);
            }
            if(existing.has(item.id)){const card=existing.get(item.id);grid.append(card);if(!card.querySelector('img'))_mediaCenterArtObserver.observe(card);continue;}
            const card=document.createElement('article');card.className='xdc-tile mc-tile';card.dataset.item=item.id;
            card.dataset.search=(item.name+' '+item.folder).toLocaleLowerCase();
            const duration=Math.max(1,Math.round(item.duration/60));
            card.innerHTML=`<div class="xdc-cover xdc-cover-none"><svg class="ic" aria-hidden="true"><use href="#i-${item.video?'tv':'music'}"></use></svg></div>
              <div class="xdc-tmeta"><b title="${enc(item.name)}">${enc(item.name)}</b><span class="muted small">${item.video?'Video':'Audio'} · ${duration} min</span>
              <span class="muted small xdc-tfoot">${enc(item.folder==='.'?lib.name:item.folder)}</span></div>
              <div class="xdc-tacts"><button class="btn btn-neon small">${item.video?'Play':'Listen'}</button></div>`;
            grid.append(card);_mediaCenterArtObserver.observe(card);
            const play=card.querySelector('button');
            card.querySelector('.xdc-cover').onclick=()=>play.click();
            play.onclick=()=>act(play,async()=>{
              const resumeAt=await mediaResumePosition(item.name,item.progress?.position||0);if(resumeAt===null)return;
              await stopMediaCenter(false);const playGeneration=_mediaCenterPlayGeneration;
              const session=await api('/'+lib.id+'/play/'+item.id,'POST');
              if(S.VIEW!=='media-center'||playGeneration!==_mediaCenterPlayGeneration){await releaseMediaCenterSession(session.url);return;}
              _mediaCenterSession=session.url;
              { const np=$('#mc-playing'); np.textContent=item.name; np.title=item.name; }
              $('#mc-playback').hidden=false;_mediaCenterWatch(true);
              const video=$('#mc-player'),quality=$('#mc-quality');quality.value='auto';
              video.addEventListener('loadedmetadata',_mediaCenterFitPlayer,{once:true});
              let lastProgress=0;
              const savePosition=()=>{
                if(video.readyState<1)return;
                lastProgress=Date.now();const position=video.currentTime;item.progress={position};
                api('/'+lib.id+'/progress/'+item.id,'POST',{position}).then(saved=>{item.progress=saved;}).catch(()=>{});
              };
              _mediaCenterSaveProgress=savePosition;
              video.ontimeupdate=()=>{if(Date.now()-lastProgress>=15000)savePosition();};
              video.onpause=savePosition;video.onended=savePosition;
              video.addEventListener('loadedmetadata',()=>{if(resumeAt>0)video.currentTime=resumeAt;},{once:true});
              await api('/'+lib.id+'/progress/'+item.id,'POST',{position:resumeAt});
              let source=new URL(session.url,_instanceBase()||location.origin).href;
              const audio=$('#mc-audio'),subtitles=$('#mc-subtitles');
              audio.replaceChildren(new Option('Default','-1'));subtitles.replaceChildren(new Option('Off','-1'));
              const trackData=await api('/'+lib.id+'/tracks/'+item.id);
              if(S.VIEW!=='media-center'||playGeneration!==_mediaCenterPlayGeneration)return;
              const languages={eng:'English',jpn:'Japanese',spa:'Spanish',fra:'French',deu:'German',ita:'Italian',zho:'Chinese',kor:'Korean',rus:'Russian',ara:'Arabic',por:'Portuguese',und:'Unknown language'};
              const label=track=>[languages[track.language]||track.language,track.title,track.codec].filter(Boolean).join(' · ');
              for(const track of trackData.tracks){const option=new Option(label(track),String(track.index));
                if(track.type==='audio')audio.append(option);
                if(track.type==='subtitle'){option.dataset.text=String(track.text);if(!track.text)option.textContent+=' · rendered in video';subtitles.append(option);}}
              const reloadSource=()=>{
                const at=video.currentTime;quality.value='auto';
                if(_mediaCenterHls){_mediaCenterHls.config.startPosition=at;_mediaCenterHls.loadSource(source);_mediaCenterHls.startLoad(at);}
                else{video.src=source;video.onloadedmetadata=()=>{video.currentTime=at;video.play().catch(()=>{});};}
              };
              audio.onchange=()=>{const url=new URL(source);url.searchParams.set('audio',audio.value);source=url.href;reloadSource();};
              let subtitleGeneration=0;
              subtitles.onchange=async()=>{
                const generation=++subtitleGeneration;
                video.querySelectorAll('track').forEach(track=>track.remove());
                if(_mediaCenterSubtitleUrl){URL.revokeObjectURL(_mediaCenterSubtitleUrl);_mediaCenterSubtitleUrl=null;}
                const url=new URL(source),oldBurn=url.searchParams.get('subtitle')||'-1';
                const burn=subtitles.value!=='-1'&&subtitles.selectedOptions[0].dataset.text==='false'?subtitles.value:'-1';
                url.searchParams.set('subtitle',burn);source=url.href;if(oldBurn!==burn)reloadSource();
                if(subtitles.value==='-1'||burn!=='-1')return;
                try{
                  status.textContent='Loading subtitles…';
                  const response=await _mediaCenterFetch(source.replace('master.m3u8','subtitle-'+subtitles.value+'.vtt'));
                  if(!response.ok)throw new Error('Unable to load subtitles');
                  const blob=await response.blob();if(generation!==subtitleGeneration||playGeneration!==_mediaCenterPlayGeneration)return;
                  _mediaCenterSubtitleUrl=URL.createObjectURL(blob);
                  const track=document.createElement('track');track.kind='subtitles';track.label=subtitles.selectedOptions[0].textContent;
                  track.src=_mediaCenterSubtitleUrl;track.default=true;video.append(track);track.track.mode='showing';status.textContent='';
                }catch(error){status.textContent=error.message;}
              };
              video.onerror=()=>{status.textContent='Playback failed. Try a lower quality or rescan the library.';};
              await loadHls();if(S.VIEW!=='media-center'||playGeneration!==_mediaCenterPlayGeneration)return;
              if(window.Hls&&Hls.isSupported()){
                const hls=new Hls({startLevel:0,maxBufferLength:18});_mediaCenterHls=hls;
                hls.loadSource(source);hls.attachMedia(video);
                hls.on(Hls.Events.MANIFEST_PARSED,()=>video.play().catch(()=>{}));
                hls.on(Hls.Events.ERROR,(_,e)=>{if(e.fatal)status.textContent='Playback failed. Reopen the media or check server transcoding.';});
                quality.onchange=()=>{hls.currentLevel=quality.value==='auto'?-1:data.profiles.indexOf(quality.value);};
              }else if(video.canPlayType('application/vnd.apple.mpegurl')){
                video.src=source;video.play().catch(()=>{});
                quality.onchange=()=>{const at=video.currentTime;video.src=source.replace('master.m3u8',(quality.value==='auto'?'master':quality.value)+'.m3u8');video.onloadedmetadata=()=>{video.currentTime=at;video.play().catch(()=>{});};};
              }else{status.textContent='This browser does not support HLS playback.';}
            });
          }
          for(const section of sections.values())if(!section.querySelector('.mc-tile'))section.remove();
          search.oninput();
          if(!result.items.length)list.textContent=result.scan?.state==='running'?'Looking for media… Titles will appear here as they are found. You can leave this page.':'No playable media found. Check the folder and FFmpeg installation.';
          if(lib.skipped)status.textContent=lib.skipped+' files could not be read during the last scan.';
        });libs.append(row);
      }
      const selected=libraryRows.get(openLibraryId)||libraryRows.values().next().value;
      if(selected)await selected.open.onclick();
      if(visibleLibraries.some(lib=>lib.scan?.state==='running'))schedulePoll();
    }catch(e){if(S.VIEW==='media-center'&&renderGeneration===_mediaCenterRenderGeneration){feed.innerHTML='<div class="empty">'+enc(e.message)+'<p><button class="btn btn-ghost" id="mc-retry">Retry</button></p></div>';$('#mc-retry').onclick=renderMediaCenter;}}
  }
  return {
    renderMediaCenter, stopMediaCenter,
    // Read by scripts/check_media_center.py, which drives two viewers and compares their sessions.
    get _mediaCenterSession(){ return _mediaCenterSession; },
  };
};
