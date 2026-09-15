/* Concord calls use LiveKit's per-sender E2EE frame layer (CORD-07). */
(function(root){
  'use strict';
  const scriptUrl=document.currentScript?.src||new URL('js/client/cord-call.js',location.href).href;
  const vendor=name=>new URL('../../vendor/livekit/'+name,scriptUrl).href;
  let active=null, sdkPromise=null,openQueue=Promise.resolve(),openVersion=0;
  function sdk(){
    if(root.LivekitClient)return Promise.resolve(root.LivekitClient);
    if(!sdkPromise)sdkPromise=new Promise((resolve,reject)=>{
      const s=document.createElement('script');s.src=vendor('livekit-client.umd.js');
      s.onload=()=>resolve(root.LivekitClient);s.onerror=()=>{sdkPromise=null;reject(Error('Call components could not load'));};document.head.append(s);
    });
    return sdkPromise;
  }
  function open(context){
    const version=++openVersion;
    const result=openQueue.then(()=>openNow(context,version));
    openQueue=result.then(()=>{},()=>{});return result;
  }
  async function openNow(context,version){
    if(version!==openVersion)return null;
    if(active)await active.close();
    if(version!==openVersion)return null;
    const V=root.PCCordVoice,abort=new AbortController();
    let closed=false,room=null,worker=null,subscription=null,heart=null,clock=null,membership=null;
    let joined=null,material=context.material(),moving=false,muted=false,video=false,readSeq=0,generation=0,attemptAbort=null,pendingBroker=null;
    const records=new Map(),keys=new Set(),tracks=new Map();
    const panel=document.createElement('section');panel.className='pc-cord-call';panel.setAttribute('role','dialog');panel.setAttribute('aria-label','Concord call');
    panel.style.cssText='position:fixed;z-index:12000;right:12px;bottom:12px;width:min(400px,calc(100vw - 24px));max-height:80vh;overflow:auto;padding:16px;border:1px solid var(--border,#555);border-radius:12px;background:var(--panel,#171723);color:var(--text,#eee);box-shadow:0 8px 32px #0008';
    panel.innerHTML='<header style="display:flex;justify-content:space-between"><strong></strong><button type="button" data-call="close" aria-label="Leave call">✕</button></header><p role="status"></p><label>Call server<input class="input" data-call="broker" inputmode="url" autocomplete="off"></label><div style="display:flex;flex-wrap:wrap;gap:8px;margin:12px 0"><button class="btn" data-call="join">Join voice</button><button class="btn" data-call="mute" disabled>Mute</button><button class="btn" data-call="video" disabled>Camera on</button><button class="btn" data-call="screen" disabled>Share screen</button></div><div data-call="people"></div><div data-call="media" style="display:grid;gap:8px"></div>';
    panel.querySelector('strong').textContent=context.name||'Concord call';
    const status=message=>{panel.querySelector('[role=status]').textContent=message;};
    const button=name=>panel.querySelector('[data-call="'+name+'"]');
    button('broker').value=context.preferred||'https://armada.buzz';
    document.body.append(panel);
    const valid=()=>!closed&&context.current();
    const check=()=>{if(!valid())throw Error('The account or community changed during the call');};
    const fresh=()=>V.fold([...records.values()]);
    function people(){
      const list=button('people');list.replaceChildren();
      const present=fresh();
      for(const p of room?.remoteParticipants?.values?.()||[]){
        const matches=present.filter(e=>e.identity===p.identity&&e.verified&&e.broker===joined?.broker);
        const item=document.createElement('p');item.textContent=matches.length===1?(context.label?.(matches[0].pubkey)||matches[0].pubkey.slice(0,12)):'Unverified participant';list.append(item);
      }
      // The SFU's identity alone is not member attribution. Keep media detached
      // until exactly one fresh signed presence claims it; expiry/conflicts
      // detach it again without trusting participant-supplied display names.
      for(const [track,entry] of tracks){
        const verified=present.some(p=>p.identity===entry.identity&&p.verified&&p.broker===joined?.broker);
        if(verified&&!entry.attached){const element=track.attach();element.style.cssText='width:100%;max-height:220px';element.autoplay=true;button('media').append(element);entry.attached=true;}
        else if(!verified&&entry.attached){track.detach().forEach(e=>e.remove());entry.attached=false;}
      }

    }
    async function announce(verb){
      if(!joined||!valid()||context.material().room!==joined.voiceRoom)return;
      await context.presence(verb,joined.identity,joined.broker);
    }
    async function disconnect(leave=true){
      clearInterval(heart);heart=null;
      const previous=room,previousWorker=worker,last=joined;room=null;worker=null;joined=null;keys.clear();
      // Relay/signing delays cannot keep capture devices or a departed SFU alive.
      if(leave&&last&&valid()){
        try{if(context.material().room===last.voiceRoom)void Promise.resolve(context.presence('left',last.identity,last.broker)).catch(()=>{});}catch(_){}
      }
      for(const track of tracks.keys())try{track.detach().forEach(e=>e.remove());}catch(_){}tracks.clear();
      button('media').replaceChildren();button('mute').disabled=true;button('video').disabled=true;button('screen').disabled=true;button('screen').textContent='Share screen';
      try{if(previous)await previous.disconnect();}
      finally{previousWorker?.terminate();}
    }
    async function close(){
      if(closed)return;
      // Stop accepting callbacks before awaiting network or SDK cleanup.
      const last=joined;
      const departure=last&&context.current()?Promise.resolve().then(()=>{if(context.material().room===last.voiceRoom)return context.presence('left',last.identity,last.broker);}).catch(()=>{}):null;
      closed=true;generation++;pendingBroker=null;abort.abort();attemptAbort?.abort();readSeq++;clearInterval(clock);clearInterval(membership);subscription?.();subscription=null;
      try{await disconnect(false);}finally{panel.remove();if(active?.close===close)active=null;}
      // Departure is best effort; it must not keep the panel or microphone open.
      void departure;
    }
    async function connect(preferred){
      if(moving||!valid())return;moving=true;button('join').disabled=true;
      const ownGeneration=++generation,controller=new AbortController();attemptAbort=controller;
      let attemptMaterial=null;
      const checkAttempt=()=>{check();if(ownGeneration!==generation||controller.signal.aborted||context.material().room!==attemptMaterial.room)throw Error('The call keys changed while connecting');};
      let attemptRoom=null;
      try{
        attemptMaterial=context.material();material=attemptMaterial;
        await disconnect();checkAttempt();
        const occupied=await V.brokers(attemptMaterial.room,fresh().map(p=>p.broker));
        const fallback=await V.brokers(attemptMaterial.room,[preferred]);
        const candidates=[...occupied,...fallback.filter(b=>!occupied.includes(b))];
        if(!candidates.length)throw Error('Enter an HTTPS Concord call server');
        const L=await sdk();checkAttempt();
        if(!L?.isE2EESupported())throw Error('This browser cannot encrypt Concord calls');
        let grant,last;
        for(const candidate of candidates.slice(0,3)){
          try{grant=await V.token(attemptMaterial,candidate,{signal:controller.signal});break;}catch(e){last=e;checkAttempt();}
        }
        if(!grant)throw last||Error('No call server answered');checkAttempt();
        class SenderKeys extends L.BaseKeyProvider{
          constructor(){super({sharedKey:false,ratchetWindowSize:0,failureTolerance:-1,keySize:256});}
          async install(identity){
            if(keys.has(identity))return;
            const raw=await V.senderKey(attemptMaterial.mediaRoot,identity);
            const key=await crypto.subtle.importKey('raw',raw,'HKDF',false,['deriveBits','deriveKey']);checkAttempt();
            this.onSetEncryptionKey(key,identity);keys.add(identity);
          }
        }
        const keyProvider=new SenderKeys();
        worker=new Worker(vendor('livekit-client.e2ee.worker.js'));
        room=new L.Room({adaptiveStream:true,dynacast:true,e2ee:{keyProvider,worker}});
        const thisRoom=room;attemptRoom=thisRoom;
        room.on(L.RoomEvent.ParticipantConnected,p=>{void keyProvider.install(p.identity).catch(e=>status(e.message));people();});
        room.on(L.RoomEvent.ParticipantDisconnected,people);
        room.on(L.RoomEvent.LocalTrackUnpublished,()=>{if(valid()&&room===thisRoom)button('screen').textContent=thisRoom.localParticipant.isScreenShareEnabled?'Stop sharing':'Share screen';});
        room.on(L.RoomEvent.Disconnected,()=>{if(room===thisRoom){void disconnect(false);if(!closed)status('Call disconnected');}});
        room.on(L.RoomEvent.TrackSubscribed,async(track,_publication,p)=>{
          try{await keyProvider.install(p.identity);if(!valid()||room!==thisRoom)return;
            tracks.set(track,{identity:p.identity,attached:false});people();
          }catch(e){status(e.message);}
        });
        room.on(L.RoomEvent.TrackUnsubscribed,track=>{tracks.delete(track);track.detach().forEach(e=>e.remove());});
        room.on(L.RoomEvent.EncryptionError,e=>{status('Call encryption failed: '+e.message);void close();});
        await keyProvider.install(grant.identity);await room.setE2EEEnabled(true);checkAttempt();
        await room.connect(grant.url,grant.token);checkAttempt();
        if(room.localParticipant.identity!==grant.identity)throw Error('The call server changed the assigned identity');
        joined={...grant,voiceRoom:attemptMaterial.room};
        for(const p of room.remoteParticipants.values())await keyProvider.install(p.identity);
        await room.localParticipant.setMicrophoneEnabled(!muted);checkAttempt();
        if(video){await room.localParticipant.setCameraEnabled(true);checkAttempt();}
        await announce('joined');checkAttempt();
        heart=setInterval(()=>{void announce('joined').catch(()=>status('Call presence could not be refreshed'));},30000);
        button('mute').disabled=false;button('video').disabled=false;button('screen').disabled=false;
        status('Connected · end-to-end encrypted');people();
      }catch(e){
        if(attemptRoom&&room!==attemptRoom)try{await attemptRoom.disconnect();}catch(_){}
        await disconnect();if(!closed&&!pendingBroker)status(e.message||'The call could not connect');
      }finally{
        if(attemptAbort===controller)attemptAbort=null;
        moving=false;if(!closed)button('join').disabled=false;
        const next=pendingBroker;pendingBroker=null;if(next&&valid())void connect(next);
      }
    }
    let heardUntil=Date.now()+30000;
    async function receive(wrap){
      if(!valid())return;const seq=readSeq;
      try{for(const e of await context.decode(wrap)){
        if(!valid()||seq!==readSeq)return;
        const old=records.get(e.pubkey);if(!old||e.at>old.at||(e.at===old.at&&e.id<old.id))records.set(e.pubkey,e);
      }
      while(records.size>1024)records.delete(records.keys().next().value);
      people();
      if(joined&&!moving){const ordered=await V.brokers(material.room,[joined.broker,...fresh().map(p=>p.broker)]);
        if(valid()&&ordered[0]!==joined.broker)void connect(ordered[0]);}
      }catch(_){}
    }
    try{subscription=context.subscribe(receive);}catch(e){await close();throw e;}
    clock=setInterval(()=>{people();if(!joined&&!moving){const left=Math.ceil((heardUntil-Date.now())/1000);status(left>0?'Listening for an existing call ('+left+'s)…':'Ready to join');button('join').disabled=left>0;}},1000);
    membership=setInterval(()=>{
      if(!valid()){void close();return;}
      try{const next=context.material();if(next.room!==material.room){const reconnect=joined?.broker||button('broker').value,wanted=!!joined||moving;generation++;attemptAbort?.abort();readSeq++;records.clear();subscription?.();subscription=context.subscribe(receive);material=next;if(wanted){if(moving)pendingBroker=reconnect;else void connect(reconnect);}}}catch(_){void close();}
    },2000);
    button('close').onclick=()=>{void close();};
    button('join').disabled=true;status('Listening for an existing call (30s)…');
    button('join').onclick=()=>{context.remember?.(V.origin(button('broker').value));void connect(button('broker').value);};
    button('mute').onclick=async()=>{const target=room;if(!target)return;try{await target.localParticipant.setMicrophoneEnabled(muted);if(!valid()||room!==target){await target.disconnect();return;}muted=!muted;button('mute').textContent=muted?'Unmute':'Mute';}catch(e){status(e.message);}};
    button('video').onclick=async()=>{const target=room;if(!target)return;try{await target.localParticipant.setCameraEnabled(!video);if(!valid()||room!==target){await target.disconnect();return;}video=!video;button('video').textContent=video?'Camera off':'Camera on';}catch(e){status(e.message);}};
    button('screen').onclick=async()=>{
      const target=room;if(!target||!joined||!valid())return;
      const control=button('screen');control.disabled=true;
      try{
        // Capture always starts from this user gesture, after sender-key installation
        // and E2EE setup. Rekeys/migration must never reopen the screen picker.
        await target.localParticipant.setScreenShareEnabled(!target.localParticipant.isScreenShareEnabled,{audio:true});
        if(!valid()||room!==target){
          try{await target.localParticipant.setScreenShareEnabled(false);}finally{await target.disconnect();}
          return;
        }
        control.textContent=target.localParticipant.isScreenShareEnabled?'Stop sharing':'Share screen';
      }catch(e){if(valid()&&room===target)status(e.name==='NotAllowedError'||e.name==='AbortError'?'Screen sharing canceled':('Screen sharing could not start: '+(e.message||e)));}
      finally{if(valid()&&room===target)control.disabled=false;}
    };
    active={close};return active;
  }
  root.PCCordCall=Object.freeze({open,close:()=>{openVersion++;return active?.close();}});
})(globalThis);
