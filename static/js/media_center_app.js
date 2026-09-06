/* Posterchan's small host page for Jellyfin Android's WebView handshake. */
(() => {
  'use strict';
  const $ = id => document.getElementById(id), base = '/jellyfin/';
  let account, pending, timer, generation=0, parent='', trail=[], offset=0, searchTimer, hls, play, item, audio=-1, subtitle=-1, playerGeneration=0, activeSource, subtitleAbort, subtitleBlob, progressTimer, lastProgress=0;
  try { account=JSON.parse(localStorage.getItem('pc_media_app')||'null'); } catch (_) {}
  const status = text => { $('status').textContent=text; };
  async function api(path, body, anonymous=false) {
    const headers={'Content-Type':'application/json'};
    if(path==='QuickConnect/Initiate'){
      let device={deviceName:'Android / browser',appName:'Posterchan Media Center',appVersion:'1'};
      try{if(window.NativeInterface?.getDeviceInformation)device=JSON.parse(window.NativeInterface.getDeviceInformation());}catch(_){}
      const clean=value=>String(value||'').replace(/["\x00-\x1f]/g,'').slice(0,100);
      headers['X-Emby-Authorization']='MediaBrowser Client="'+clean(device.appName)+'", Device="'+clean(device.deviceName)+'", Version="'+clean(device.appVersion)+'"';
    }
    if(account&&!anonymous) headers['X-Emby-Token']=account.AccessToken;
    const response=await fetch(base+path,{method:body===undefined?'GET':'POST',headers,body:body===undefined?undefined:JSON.stringify(body),signal:AbortSignal.timeout(30000),cache:'no-store',keepalive:path.startsWith('Sessions/Playing')});
    if(!response.ok) { const error=new Error(response.status===401?'Your app login expired. Connect again.':'Request failed ('+response.status+'). Try again.');error.status=response.status;throw error; }
    return response.status===204?null:response.json();
  }
  function save(login) {
    account=login;
    if(login) {
      localStorage.setItem('pc_media_app',JSON.stringify(login));
      // The Android host reads this when it observes the capabilities request.
      localStorage.setItem('jellyfin_credentials',JSON.stringify({Servers:[{Id:login.ServerId,Address:location.origin+base,UserId:login.User.Id,AccessToken:login.AccessToken}]}));
    } else {localStorage.removeItem('pc_media_app');localStorage.removeItem('jellyfin_credentials');}
  }
  async function connect() {
    const current=++generation;clearTimeout(timer);stop();save(null);
    $('connect').hidden=false;$('library').hidden=true;$('logout').hidden=true;$('code').textContent='…';status('Creating a Quick Connect code…');
    try { pending=await api('QuickConnect/Initiate',{},true);if(current!==generation)return;$('code').textContent=pending.Code;status('Waiting for approval…'); }
    catch(error){status(error.message);return;}
    const secret=pending.Secret;
    async function poll() {
      if(current!==generation)return;
      try {
        const result=await api('QuickConnect/Connect?secret='+encodeURIComponent(secret),undefined,true);
        if(current!==generation)return;
        if(result.Authenticated) {
          const login=await api('Users/AuthenticateWithQuickConnect',{secret},true);
          if(current!==generation)return;save(login);await open();return;
        }
        timer=setTimeout(poll,3000);
      } catch(error){if(current===generation)status(error.status===404?'Code expired. Tap New code.':error.message);}
    }
    timer=setTimeout(poll,3000);
  }
  async function open() {
    await api('Users/Me');
    await api('Sessions/Capabilities/Full',{PlayableMediaTypes:['Video','Audio'],SupportsMediaControl:false});
    $('connect').hidden=true;$('library').hidden=false;$('logout').hidden=false;
    parent='';trail=[];$('heading').textContent='Your libraries';$('search').value='';await browse();
  }
  function breadcrumbs() {
    const nav=$('breadcrumbs');nav.replaceChildren();
    for(const [index,ancestor] of trail.entries()) {
      const link=document.createElement('button');link.type='button';link.textContent=ancestor.name;
      link.onclick=()=>{parent=ancestor.id;trail=trail.slice(0,index);$('heading').textContent=ancestor.name;$('search').value='';browse();};
      nav.append(link);
    }
    const current=document.createElement('span');current.textContent=$('heading').textContent;current.setAttribute('aria-current','page');nav.append(current);
  }
  function fallbackArt(entry) {
    const art=document.createElement('div');art.className='art';art.setAttribute('aria-hidden','true');
    art.textContent=String(entry.Name||'').slice(0,2).toLocaleUpperCase();return art;
  }
  async function browse(append=false) {
    const current=++generation;const term=$('search').value.trim();if(!append){offset=0;$('cards').replaceChildren();}
    breadcrumbs();$('more').hidden=true;status('Loading library…');
    try {
      const params=new URLSearchParams({ParentId:parent,StartIndex:offset,Limit:60});
      if(term){params.set('SearchTerm',term);params.set('Recursive','true');}
      const result=await api(!parent&&!term?'UserViews':'Items?'+params);
      if(current!==generation)return;
      for(const entry of result.Items) {
        const card=document.createElement('button');card.className='card';card.classList.toggle('folder-card',!!entry.IsFolder);
        if(entry.ImageTags?.Primary){const img=document.createElement('img');img.loading='lazy';img.alt='';img.src=base+'Items/'+encodeURIComponent(entry.Id)+'/Images/Primary?tag='+encodeURIComponent(entry.ImageTags.Primary);img.onerror=()=>img.replaceWith(fallbackArt(entry));card.append(img);}
        else card.append(fallbackArt(entry));
        const title=document.createElement('span');title.textContent=entry.Name;
        if(entry.IsFolder){const count=document.createElement('small');count.textContent=(entry.ChildCount||0)+' titles';title.append(count);}
        card.append(title);card.onclick=()=>{if(entry.IsFolder){trail.push({id:parent,name:$('heading').textContent});parent=entry.Id;$('heading').textContent=entry.Name;browse();}else{chooseVideo(entry);}};$('cards').append(card);
      }
      offset+=result.Items.length;$('more').hidden=offset>=result.TotalRecordCount;status(result.TotalRecordCount?result.TotalRecordCount+' available':'No media found.');
    } catch(error){if(current===generation)status(error.message);}
  }
  function clockLabel(seconds) {
    seconds=Math.floor(seconds);return (seconds>=3600?Math.floor(seconds/3600)+':':'')+String(Math.floor(seconds/60)%60).padStart(2,'0')+':'+String(seconds%60).padStart(2,'0');
  }
  async function chooseVideo(entry) {
    try {
      const latest=await api('Items/'+entry.Id);
      const position=(latest.UserData?.PlaybackPositionTicks||0)/10000000;
      let chosen=0;
      if(position>0) {
        const dialog=$('resume-dialog');$('resume-time').textContent=clockLabel(position);$('resume-title').textContent=latest.Name;
        chosen=await new Promise(resolve=>{
          dialog.returnValue='cancel';dialog.onclose=()=>resolve(dialog.returnValue==='resume'?position:dialog.returnValue==='start'?0:null);dialog.showModal();
        });
        if(chosen===null)return;
      }
      item=latest;audio=-1;subtitle=-1;start(chosen);
    } catch(error){status(error.message);}
  }
  function reportProgress() {
    const video=document.querySelector('video');if(!play||video.readyState<1)return;
    lastProgress=Date.now();
    api('Sessions/Playing/Progress',{PlaySessionId:play.PlaySessionId,PositionTicks:Math.round(video.currentTime*10000000)}).catch(()=>{});
  }
  document.querySelector('video').addEventListener('pause',()=>{if(Date.now()-lastProgress>1000)reportProgress();});
  document.querySelector('video').addEventListener('ended',reportProgress);
  function clearTextSubtitle() {
    if(subtitleAbort){subtitleAbort.abort();subtitleAbort=null;}
    document.querySelector('video').querySelectorAll('track').forEach(track=>track.remove());
    if(subtitleBlob){URL.revokeObjectURL(subtitleBlob);subtitleBlob=null;}
    $('subtitle-status').textContent='';
  }
  async function loadTextSubtitle(track) {
    clearTextSubtitle();if(!track)return;
    const controller=new AbortController();subtitleAbort=controller;
    const timeout=setTimeout(()=>controller.abort(),120000);
    $('subtitle-status').textContent='Loading subtitles…';
    try {
      const response=await fetch(new URL(track.DeliveryUrl,location.origin+base),{signal:controller.signal,cache:'no-store'});
      if(!response.ok)throw new Error('Subtitles could not be loaded ('+response.status+').');
      const text=await response.text();if(controller.signal.aborted)return;
      if(!text.startsWith('WEBVTT'))throw new Error('Invalid subtitle response.');
      subtitleBlob=URL.createObjectURL(new Blob([text],{type:'text/vtt'}));
      const node=document.createElement('track');node.kind='subtitles';node.label=track.DisplayTitle||'Subtitles';node.srclang=track.Language||'en';node.src=subtitleBlob;node.default=true;
      node.onload=()=>{if(!controller.signal.aborted&&node.isConnected){node.track.mode='showing';$('subtitle-status').textContent='';}};
      node.onerror=()=>{$('subtitle-status').textContent='This device could not display the subtitles.';};
      document.querySelector('video').append(node);node.track.mode='showing';
    } catch(error) {if(subtitleAbort===controller)$('subtitle-status').textContent=controller.signal.aborted?'Subtitle loading timed out. Select the track to retry.':error.message;}
    finally {clearTimeout(timeout);}
  }
  let fullscreen=false, fullscreenControlsTimer;
  function showFullscreenControls() {
    clearTimeout(fullscreenControlsTimer);
    $('player').classList.remove('mc-controls-hidden');
    if(fullscreen)fullscreenControlsTimer=setTimeout(()=>{
      if($('player').querySelector('.controls').contains(document.activeElement))return;
      $('player').classList.add('mc-controls-hidden');
    },3000);
  }
  for(const event of ['pointermove','pointerdown','keydown','focusout'])$('player').addEventListener(event,showFullscreenControls);

  function leaveFullscreen() {
    fullscreen=false;showFullscreenControls();document.body.classList.remove('mc-fullscreen');$('fullscreen').textContent='Full screen';
    try {window.NativeInterface?.disableFullscreen();} catch (_) {}
    if(document.fullscreenElement)document.exitFullscreen().catch(()=>{});
  }
  function closeFullscreen() {
    if(!fullscreen)return;
    leaveFullscreen();
    if(history.state?.pcMediaFullscreen)history.back();
  }
  async function enterFullscreen() {
    if(fullscreen){closeFullscreen();return;}
    fullscreen=true;document.body.classList.add('mc-fullscreen');$('fullscreen').textContent='Exit full screen';
    showFullscreenControls();
    document.activeElement?.blur();
    history.pushState({pcMediaFullscreen:true}, '', location.href);
    // Android WebView has no custom-view fullscreen handler. Its native bridge
    // hides system bars; our player fills the viewport without replacing playback.
    if(window.NativeInterface?.enableFullscreen) {
      try {window.NativeInterface.enableFullscreen();return;} catch (_) {}
    }
    try {
      if($('player').requestFullscreen)await $('player').requestFullscreen();
      else if(document.querySelector('video').webkitEnterFullscreen)document.querySelector('video').webkitEnterFullscreen();
    } catch (_) {status('Expanded player. Browser fullscreen is unavailable on this device.');}
  }
  window.addEventListener('popstate',()=>{if(fullscreen)leaveFullscreen();});
  document.addEventListener('keydown',event=>{if(event.key==='Escape'&&fullscreen){event.preventDefault();closeFullscreen();}});
  document.addEventListener('fullscreenchange',()=>{if(fullscreen&&!document.fullscreenElement&&!window.NativeInterface?.enableFullscreen)closeFullscreen();});
  function stop(keepFullscreen=false) {
    if(!keepFullscreen)closeFullscreen();
    clearTextSubtitle();activeSource=null;
    clearInterval(progressTimer);progressTimer=null;
    const stoppedPosition=document.querySelector('video').currentTime||0;
    ++playerGeneration;if(hls){hls.destroy();hls=null;}
    const video=document.querySelector('video');video.pause();video.removeAttribute('src');video.load();video.querySelectorAll('track').forEach(t=>t.remove());
    if(play){api('Sessions/Playing/Stopped',{PlaySessionId:play.PlaySessionId,PositionTicks:Math.round(stoppedPosition*10000000)}).catch(()=>{});play=null;}
    $('player').hidden=true;
  }
  async function start(position) {
    stop(true);const current=playerGeneration;status('Preparing video…');
    try {
      const info=await api('Items/'+item.Id+'/PlaybackInfo',{AudioStreamIndex:audio,SubtitleStreamIndex:subtitle});
      if(current!==playerGeneration){if(info.PlaySessionId)api('Sessions/Playing/Stopped',{PlaySessionId:info.PlaySessionId}).catch(()=>{});return;}
      const source=info.MediaSources?.[0];if(!source)throw new Error('No stream fits the configured bandwidth limit.');play=info;activeSource=source;
      const video=document.querySelector('video');$('player').hidden=false;$('playing').textContent=item.Name;
      function choices(id,type,selected){const select=$(id);select.replaceChildren(new Option(type==='Subtitle'?'Off':'Default','-1'));for(const stream of source.MediaStreams.filter(s=>s.Type===type))select.add(new Option(stream.DisplayTitle||stream.Language||type+' '+stream.Index,String(stream.Index)));select.value=String(selected);}
      choices('audio','Audio',audio);choices('subtitles','Subtitle',subtitle);
      const track=source.MediaStreams.find(s=>s.Type==='Subtitle'&&s.Index===subtitle&&s.DeliveryUrl);

      const url=new URL(source.TranscodingUrl,location.origin+base).href;
      video.onloadedmetadata=()=>{if(current===playerGeneration){if(position)video.currentTime=position;video.play().catch(()=>status('Tap Play to start.'));}};
      if(window.Hls?.isSupported()){hls=new Hls({maxBufferLength:18,maxMaxBufferLength:30});hls.loadSource(url);hls.attachMedia(video);hls.on(Hls.Events.ERROR,(_,error)=>{if(error.fatal)status('Playback interrupted. Reopen the video to retry.');});}
      else if(video.canPlayType('application/vnd.apple.mpegurl'))video.src=url;
      else throw new Error('This device cannot play HLS video.');
      await api('Sessions/Playing',{PlaySessionId:info.PlaySessionId,ItemId:item.Id,PositionTicks:Math.round(position*10000000)});progressTimer=setInterval(()=>reportProgress(),15000);status('');if(track)loadTextSubtitle(track);$('player').scrollIntoView({behavior:'smooth'});
    } catch(error){if(current===playerGeneration){stop();status(error.message);}}
  }
  $('new-code').onclick=connect;$('back').onclick=()=>{stop();const previous=trail.pop()||{id:'',name:'Your libraries'};parent=previous.id;$('search').value='';$('heading').textContent=previous.name;if(account)browse();};
  $('logout').onclick=async()=>{stop();try{await api('Sessions/Logout',{});}catch(_){}connect();};
  $('more').onclick=()=>browse(true);$('search').oninput=()=>{clearTimeout(searchTimer);searchTimer=setTimeout(()=>browse(),300);};
  $('audio').onchange=()=>{audio=Number($('audio').value);start(document.querySelector('video').currentTime);};
  $('subtitles').onchange=()=>{
    const previous=activeSource?.MediaStreams.find(track=>track.Type==='Subtitle'&&track.Index===subtitle);
    subtitle=Number($('subtitles').value);
    const selected=activeSource?.MediaStreams.find(track=>track.Type==='Subtitle'&&track.Index===subtitle);
    if(activeSource&&(!previous||previous.IsTextSubtitleStream)&&(!selected||selected.IsTextSubtitleStream))loadTextSubtitle(selected);
    else start(document.querySelector('video').currentTime);
  };
  $('stop').onclick=()=>stop();$('fullscreen').onclick=enterFullscreen;
  window.addEventListener('pagehide',()=>stop());
  if(account)open().catch(error=>{if(error.status===401||error.status===403)connect();else status(error.message);});else connect();
})();
