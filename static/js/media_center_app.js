/* Posterchan's small host page for Jellyfin Android's WebView handshake. */
(() => {
  'use strict';
  const $ = id => document.getElementById(id), base = '/jellyfin/';
  let account, pending, timer, generation=0, parent='', offset=0, searchTimer, hls, play, item, audio=-1, subtitle=-1, playerGeneration=0;
  try { account=JSON.parse(localStorage.getItem('pc_media_app')||'null'); } catch (_) {}
  const status = text => { $('status').textContent=text; };
  async function api(path, body, anonymous=false) {
    const headers={'Content-Type':'application/json'};
    if(account&&!anonymous) headers['X-Emby-Token']=account.AccessToken;
    const response=await fetch(base+path,{method:body===undefined?'GET':'POST',headers,body:body===undefined?undefined:JSON.stringify(body),signal:AbortSignal.timeout(30000),cache:'no-store'});
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
    parent='';$('search').value='';await browse();
  }
  async function browse(append=false) {
    const current=++generation;const term=$('search').value.trim();if(!append){offset=0;$('cards').replaceChildren();}
    $('more').hidden=true;status('Loading library…');
    try {
      const params=new URLSearchParams({ParentId:parent,StartIndex:offset,Limit:60});
      if(term){params.set('SearchTerm',term);params.set('Recursive','true');}
      const result=await api(!parent&&!term?'UserViews':'Items?'+params);
      if(current!==generation)return;
      for(const entry of result.Items) {
        const card=document.createElement('button');card.className='card';
        if(entry.ImageTags?.Primary){const img=document.createElement('img');img.loading='lazy';img.alt='';img.src=base+'Items/'+encodeURIComponent(entry.Id)+'/Images/Primary?api_key='+encodeURIComponent(account.AccessToken);card.append(img);}
        else {const art=document.createElement('div');art.className='art';art.textContent=entry.IsFolder?'▣':'▶';card.append(art);}
        const title=document.createElement('span');title.textContent=entry.Name;
        if(entry.IsFolder){const count=document.createElement('small');count.textContent=(entry.ChildCount||0)+' titles';title.append(count);}
        card.append(title);card.onclick=()=>{if(entry.IsFolder){parent=entry.Id;$('heading').textContent=entry.Name;browse();}else{item=entry;audio=-1;subtitle=-1;start(0);}};$('cards').append(card);
      }
      offset+=result.Items.length;$('more').hidden=offset>=result.TotalRecordCount;status(result.TotalRecordCount?result.TotalRecordCount+' available':'No media found.');
    } catch(error){if(current===generation)status(error.message);}
  }
  function stop() {
    ++playerGeneration;if(hls){hls.destroy();hls=null;}
    const video=document.querySelector('video');video.pause();video.removeAttribute('src');video.load();video.querySelectorAll('track').forEach(t=>t.remove());
    if(play){api('Sessions/Playing/Stopped',{PlaySessionId:play.PlaySessionId}).catch(()=>{});play=null;}
    $('player').hidden=true;
  }
  async function start(position) {
    stop();const current=playerGeneration;status('Preparing video…');
    try {
      const info=await api('Items/'+item.Id+'/PlaybackInfo',{AudioStreamIndex:audio,SubtitleStreamIndex:subtitle});
      if(current!==playerGeneration){if(info.PlaySessionId)api('Sessions/Playing/Stopped',{PlaySessionId:info.PlaySessionId}).catch(()=>{});return;}
      const source=info.MediaSources?.[0];if(!source)throw new Error('No stream fits the configured bandwidth limit.');play=info;
      const video=document.querySelector('video');$('player').hidden=false;$('playing').textContent=item.Name;
      function choices(id,type,selected){const select=$(id);select.replaceChildren(new Option(type==='Subtitle'?'Off':'Default','-1'));for(const stream of source.MediaStreams.filter(s=>s.Type===type))select.add(new Option(stream.DisplayTitle||stream.Language||type+' '+stream.Index,String(stream.Index)));select.value=String(selected);}
      choices('audio','Audio',audio);choices('subtitles','Subtitle',subtitle);
      const track=source.MediaStreams.find(s=>s.Type==='Subtitle'&&s.Index===subtitle&&s.DeliveryUrl);
      if(track){const node=document.createElement('track');node.kind='subtitles';node.label=track.DisplayTitle||'Subtitles';node.srclang=track.Language||'en';node.src=track.DeliveryUrl;node.default=true;video.append(node);}
      const url=new URL(source.TranscodingUrl,location.origin+base).href;
      video.onloadedmetadata=()=>{if(current===playerGeneration){if(position)video.currentTime=position;video.play().catch(()=>status('Tap Play to start.'));}};
      if(window.Hls?.isSupported()){hls=new Hls({maxBufferLength:18,maxMaxBufferLength:30});hls.loadSource(url);hls.attachMedia(video);hls.on(Hls.Events.ERROR,(_,error)=>{if(error.fatal)status('Playback interrupted. Reopen the video to retry.');});}
      else if(video.canPlayType('application/vnd.apple.mpegurl'))video.src=url;
      else throw new Error('This device cannot play HLS video.');
      await api('Sessions/Playing',{PlaySessionId:info.PlaySessionId,ItemId:item.Id});status('');$('player').scrollIntoView({behavior:'smooth'});
    } catch(error){if(current===playerGeneration){stop();status(error.message);}}
  }
  $('new-code').onclick=connect;$('back').onclick=()=>{stop();parent='';$('search').value='';$('heading').textContent='Your libraries';if(account)browse();};
  $('logout').onclick=async()=>{stop();try{await api('Sessions/Logout',{});}catch(_){}connect();};
  $('more').onclick=()=>browse(true);$('search').oninput=()=>{clearTimeout(searchTimer);searchTimer=setTimeout(()=>browse(),300);};
  $('audio').onchange=()=>{audio=Number($('audio').value);start(document.querySelector('video').currentTime);};
  $('subtitles').onchange=()=>{subtitle=Number($('subtitles').value);start(document.querySelector('video').currentTime);};
  $('stop').onclick=stop;$('fullscreen').onclick=()=>{const video=document.querySelector('video');if(video.requestFullscreen)video.requestFullscreen().catch(()=>{});else if(video.webkitEnterFullscreen)video.webkitEnterFullscreen();};
  window.addEventListener('pagehide',stop);
  if(account)open().catch(error=>{if(error.status===401||error.status===403)connect();else status(error.message);});else connect();
})();
