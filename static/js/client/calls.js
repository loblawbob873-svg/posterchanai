/* calls.js — split out of app.js by scripts/split_client_module.mjs.
 *
 * Loaded by its own <script> tag BEFORE app.js, and built synchronously by app.js (`_callsMod()`) at
 * the point where this code used to sit — incoming calls, the ringtone and answering/hanging up from a
 * notification or Android's native call UI cannot wait for a lazy load. The code below is
 * app.js's own, moved byte-for-byte; its reads of app.js's live `let` bindings were rewritten to
 * `S.<name>` (getters/setters on `dep.state`) at exact identifier offsets, and everything else it uses
 * arrives through `dep`. tests/test_client_module_deps.py proves every name resolves.
 */
window.PCCallsFactory = function(dep){
  const S = dep.state;   // live app.js bindings: S.CFG, S.FOLLOWS, S.GUEST, S.LOGO, S.ME, S.VIEW, S._aiToken, S.signer
  const {
    $, $$, BUNDLED, FOLLOWERS, _capPlugin, _guestPrompt, enc, ensureAiSession, ensureMyFollowers,
    isMutedAuthor, needProfile, niceNip05, nip05Resolve, normalizeRelay, profOf, safePk, sign,
    timeAgo, toast,
  } = dep;

  // ===== Voice/Video calls (WebRTC, P2P-first, signaled over Nostr) =====
  // Signaling = ephemeral kind-25050 events, NIP-44-encrypted {v,callId,t,sdp|cand|video} to the peer's
  // p-tag. Media = RTCPeerConnection with ICE servers from
  // /api/calls/turn-credentials (STUN → direct P2P; the built-in TURN relay is only the NAT fallback).
  //
  // EVERY signaling frame ALSO goes to this instance's own relay (_callPublish), and that is what makes
  // ringing a CLOSED app possible: the server watches its own relay for kind-25050 and Web-Pushes the
  // callee (nostr_push_service). A frame that only reaches the caller's personal relay list is invisible
  // to that watcher, so the callee's phone never rings — and 25050 is EPHEMERAL, so nothing syncs it
  // afterwards and there is no second chance. Calls are an instance feature; they do not depend on which
  // relays either party has configured. (Two users on DIFFERENT instances still connect while both apps
  // are open, but the closed-app push only fires on the instance whose relay carried the invite.)
  // 1:1, no mid-call renegotiation: media (audio ± video) is fixed at call start; mute/camera just toggle
  // track.enabled. Audio-first — video is opt-in per call.
  const CALL_KIND = 25050;
  let _call = null;               // { id, peer, pc, local, remote, video, state, caller, invite, pendingIce, muted, camOff, timeout }
  let _callSub = null;            // global signaling subscription id
  const _callSeen = new Set();    // processed signaling event ids (dedup on reconnect re-arm)
  let _ringOsc = null;
  let _wakeLock = null;
  function _rid(){ return Math.random().toString(36).slice(2) + Date.now().toString(36); }
  // Screen Wake Lock — keep the screen (and the call) alive during a call; re-acquired on foreground since
  // the OS drops it when hidden. Pure JS (no native plugin); background-with-screen-off audio is a follow-up.
  async function _callWake(on){
    try{
      if(on){ if(!_wakeLock && navigator.wakeLock){ _wakeLock = await navigator.wakeLock.request('screen'); if(_wakeLock.addEventListener) _wakeLock.addEventListener('release', ()=>{ _wakeLock=null; }); } }
      else if(_wakeLock){ const w=_wakeLock; _wakeLock=null; try{ await w.release(); }catch(_){} }
    }catch(_){}
  }
  document.addEventListener('visibilitychange', ()=>{ if(_call && document.visibilityState==='visible') _callWake(true); });

  async function _fetchIceServers(){
    try{ await ensureAiSession(); }catch(_){}
    try{
      const r = await fetch('/api/calls/turn-credentials', { headers: S._aiToken?{'Authorization':'Bearer '+S._aiToken}:{} });
      if(r.ok) return await r.json();
    }catch(_){}
    return { iceServers: [], defaultVideo:false };
  }
  /* Send one signaling frame to the pool AND to this instance's relay.
   *
   * publishTo() SKIPS relays already in the pool, so when the instance relay is one of your configured
   * relays (the normal case) this costs nothing — no second socket, no duplicate EVENT. It only opens a
   * short-lived socket when the user has dropped our relay from their list, which is exactly the case
   * that otherwise silently breaks ringing: the server's push watcher subscribes to its OWN relay, so a
   * frame that never reaches it means the callee's closed phone is never woken. */
  function _callPublish(ev){
    try{ Relay.publish(ev); }catch(_){}
    // normalizeRelay is REQUIRED, not tidiness: publishTo's skip is a raw string compare against the pool
    // keys, and those are normalized (trailing slash stripped, scheme prepended) whenever relays are
    // configured. Our own default list writes 'wss://relay.poster.place/' WITH the slash, so passing
    // CFG.relay_url verbatim misses the match and opens a NEW SOCKET PER SIGNALING FRAME — and there is one
    // frame per ICE candidate, i.e. ~8-20 sockets in two seconds per call, more for a group room.
    try{ const own = (S.CFG && S.CFG.relay_url) ? [normalizeRelay(S.CFG.relay_url)].filter(Boolean) : [];   // empty on a standalone install (no instance)
      if(own.length) Relay.publishTo(own, ev).catch(()=>{}); }catch(_){}
    try{ if(_call && _call.signalRelays && _call.signalRelays.length)
      Relay.publishTo(_call.signalRelays, ev).catch(()=>{}); }catch(_){}
  }
  /* The ONE frame per call that should wake a phone, marked in the clear so the server can tell.
   *
   * The body is encrypted, so the push watcher cannot distinguish an invite from an ICE candidate or a
   * hangup — it pushed on ANY 25050 that cleared its 30s cooldown. Two consequences, both bad: a caller
   * giving up at 45s sends a `bye`, which is outside that window, so every unanswered call rang the
   * callee a SECOND time 45s after the caller left; and every frame of every call cost a rate-limit
   * check, a stats bump and a DB lookup for push subscriptions.
   *
   * `t=invite` leaks nothing the relay cannot already infer — it sees kind 25050 between these two
   * pubkeys either way — and lets the server drop ~everything else before it does any work.
   *
   * EVERY frame carries a `t`, not just invites — that is what makes the marker usable. If only invites
   * were tagged, a new client's ICE frame (no tag) would be byte-identical to an old client's invite
   * (no tag), the server would have to ring for both, and nothing would be fixed. Tagging all of them
   * makes ABSENCE mean exactly one thing: a client older than this change. */
  const _RING_FRAMES = new Set(['invite', 'ginvite']);   // 1:1 and group; the only two that ring
  /* Distinguish devices signed into the same identity. A self-addressed invite is delivered back to
   * the sending device too; without this marker it tears down its own offer as glare. sessionStorage
   * makes the marker per renderer/session, never an account identifier published anywhere else. */
  const _CALL_DEVICE_ID=(()=>{try{let v=sessionStorage.getItem('pc_call_device');if(!v){v=_rid();sessionStorage.setItem('pc_call_device',v);}return v;}catch(_){return _rid();}})();
  function _callTags(peerHex, obj){
    return [['p', peerHex], ['t', (obj && _RING_FRAMES.has(obj.t)) ? 'invite' : 'sig']];
  }
  async function _callSend(peerHex, obj){
    try{
      const payload=Object.assign({},obj,{deviceId:_CALL_DEVICE_ID});
      const ct = await S.signer.nip44enc(peerHex, JSON.stringify(payload));
      const ev = await sign(CALL_KIND, ct, _callTags(peerHex, obj));   // ephemeral, not stored, no client tag
      _callPublish(ev);
    }catch(_){}
  }
  /* Prefer H.264 on the video sender, so BOTH ends use the hardware encoder/decoder.
   *
   * Nothing on the server can accelerate a call: media is peer-to-peer and the TURN relay forwards
   * packets without ever decoding them, unlike the live-stream clamp which really does re-encode. The
   * only lever is which codec the two browsers agree on — and that decides hardware-vs-software
   * outright. H.264 has a dedicated block in essentially every phone SoC (and is the ONLY
   * hardware-accelerated video codec on iPhone), while VP8/VP9 fall back to software on many of them:
   * same call, several times the CPU, a hot phone and a flat battery.
   *
   * The live-streaming path has done this since WHIP shipped; calls were left on the browser's default
   * SDP order, which is why a call could cost more battery than broadcasting to the world.
   *
   * Best-effort by design: an unsupported browser just keeps its default order, and leaving `rest`
   * appended means we only ever REORDER the list — never remove a codec the peer might be the only
   * one able to decode. */
  function _preferH264(pc){
    try{
      const caps=(window.RTCRtpSender&&RTCRtpSender.getCapabilities)?RTCRtpSender.getCapabilities('video'):null;
      if(!caps||!caps.codecs) return;
      const h=caps.codecs.filter(c=>/h264/i.test(c.mimeType||'')), rest=caps.codecs.filter(c=>!/h264/i.test(c.mimeType||''));
      if(!h.length) return;
      pc.getTransceivers().forEach(t=>{
        if(t.sender && t.sender.track && t.sender.track.kind==='video' && t.setCodecPreferences)
          t.setCodecPreferences(h.concat(rest));
      });
    }catch(_){}
  }
  function _preferScreenCodec(pc){
    try{
      const caps=(window.RTCRtpSender&&RTCRtpSender.getCapabilities)?RTCRtpSender.getCapabilities('video'):null;
      if(!caps||!caps.codecs)return;
      const detail=caps.codecs.filter(c=>/\/(av1|vp9)$/i.test(c.mimeType||'')),rest=caps.codecs.filter(c=>!/\/(av1|vp9)$/i.test(c.mimeType||''));
      if(!detail.length)return;
      pc.getTransceivers().forEach(t=>{if(t.sender&&t.sender.track&&t.sender.track.kind==='video'&&t.setCodecPreferences)t.setCodecPreferences(detail.concat(rest));});
    }catch(_){}
  }
  function _newPc(iceServers){
    const pc = new RTCPeerConnection({ iceServers: iceServers||[], iceCandidatePoolSize: 1 });
    pc.onicecandidate = e => { if(e.candidate && _call) _callSend(_call.peer, {v:1, callId:_call.id, t:'ice', cand:e.candidate.toJSON()}); };
    pc.ontrack = e => { if(!_call) return; _call.remote = e.streams[0]; _callUI(); };
    pc.oniceconnectionstatechange = () => { if(_call && _call.pc===pc) _callUI(); };   // surface ICE progress in the status line
    pc.onconnectionstatechange = () => { if(!_call || _call.pc!==pc) return;
      const st=pc.connectionState;
      if(st==='connected'){
        // A Wi-Fi route change can report `failed`, recover after restartIce(), then become connected
        // again. Cancel the delayed teardown so a recovered Remote Desktop session stays intact.
        if(_call.iceFailureTimer){clearTimeout(_call.iceFailureTimer);_call.iceFailureTimer=null;}
        _call.state='connected'; _callUI();
      }
      else if(st==='failed'){
        /* `failed` is not necessarily terminal: Chromium can briefly enter it while switching from a
         * host candidate to TURN (observed when the Remote Desktop control data-channel first sends).
         * Tearing down synchronously made "Request control" appear to end a healthy screen share.
         * Give ICE one bounded recovery attempt; a genuinely dead peer still closes promptly. */
        if(!_call.iceFailureTimer){
          _call.state='connecting'; _callUI();
          try{pc.restartIce();}catch(_){}
          _call.iceFailureTimer=setTimeout(()=>{
            if(_call&&_call.pc===pc&&pc.connectionState==='failed'){
              toast('call couldn’t connect (network/firewall)'); _hangup(false);
            }else if(_call&&_call.pc===pc){_call.iceFailureTimer=null;}
          },10000);
        }
      }
      else if(st==='closed'){ _hangup(true); }
      else _callUI();
    };
    return pc;
  }
  /* Remote Desktop control rides a dedicated WebRTC data channel. Screen acceptance never implies
   * input acceptance: the viewer asks, the sharer explicitly allows, and either side can revoke it.
   * No relay sees keystrokes and a disconnected call destroys the channel with the PeerConnection. */
  const _RD_KEYS={Escape:1,Digit1:2,Digit2:3,Digit3:4,Digit4:5,Digit5:6,Digit6:7,Digit7:8,Digit8:9,Digit9:10,Digit0:11,
    Minus:12,Equal:13,Backspace:14,Tab:15,KeyQ:16,KeyW:17,KeyE:18,KeyR:19,KeyT:20,KeyY:21,KeyU:22,KeyI:23,KeyO:24,KeyP:25,
    BracketLeft:26,BracketRight:27,Enter:28,ControlLeft:29,KeyA:30,KeyS:31,KeyD:32,KeyF:33,KeyG:34,KeyH:35,KeyJ:36,KeyK:37,KeyL:38,
    Semicolon:39,Quote:40,Backquote:41,ShiftLeft:42,Backslash:43,KeyZ:44,KeyX:45,KeyC:46,KeyV:47,KeyB:48,KeyN:49,KeyM:50,
    Comma:51,Period:52,Slash:53,ShiftRight:54,AltLeft:56,Space:57,CapsLock:58,ControlRight:97,AltRight:100,
    ArrowUp:103,ArrowLeft:105,ArrowRight:106,ArrowDown:108,Delete:111};
  let _remoteDesktopArmed=false;
  let _remoteDesktopHost=null;
  function setRemoteDesktopHost(el){
    _remoteDesktopHost=el&&el.nodeType===1?el:null;
    const overlay=document.getElementById('call-overlay');
    if(overlay&&_call&&_call.remoteDesktop){
      const target=(_remoteDesktopHost&&_remoteDesktopHost.isConnected)?_remoteDesktopHost:document.body;
      if(overlay.parentElement!==target)target.appendChild(overlay);
    }
  }
  function _rdEnsureHost(){
    if(_remoteDesktopHost&&_remoteDesktopHost.isConnected)return _remoteDesktopHost;
    // PosterChanOS owns app windows. Ask it synchronously to create/focus the Remote Desktop
    // window before rendering; falling back to body is what painted a session over Terminal.
    try{document.dispatchEvent(new CustomEvent('pc:remote-desktop-window'));}catch(_){}
    return _remoteDesktopHost&&_remoteDesktopHost.isConnected?_remoteDesktopHost:null;
  }
  function setRemoteDesktopArmed(on){
    _remoteDesktopArmed=!!on;
    if(!_remoteDesktopArmed&&_call&&_call.remoteDesktop)_hangup(false);
    return _remoteDesktopArmed;
  }
  /* Returns whether it actually went out. Almost every caller is an input event, where a drop is
   * self-correcting (the next mouse move says the same thing again) — but the resolution agreement is
   * sent ONCE per change, so it is the one message that must not be recorded as sent when the channel
   * was not open yet. */
  function _rdSend(obj){try{
    if(_call&&_call.control&&_call.control.readyState==='open'){_call.control.send(JSON.stringify(obj));return true;}
  }catch(_){}return false;}
  function _rdReleaseNative(){try{if(window.pcRemoteControl&&pcRemoteControl.release)pcRemoteControl.release();}catch(_){}}
  async function _rdConfigureNative(stream){
    const session=_call;
    if(!session||!session.remoteDesktop)return false;
    session.nativeReady=false;
    try{
      const track=stream&&stream.getVideoTracks&&stream.getVideoTracks()[0];
      const s=track&&track.getSettings?track.getSettings():{};
      if(!s.width||!s.height)return false;
      let controlReady=false;
      if(window.pcRemoteControl&&pcRemoteControl.configure){
        const result=await pcRemoteControl.configure({width:s.width,height:s.height});
        if(!result||!result.ok)return false;
        controlReady=result.control!==false;
      }
      if(_call!==session||track.readyState==='ended')return false;
      session.nativeReady=controlReady;
      _call.localGeometry={width:s.width,height:s.height};
      _rdSend({t:'geometry',width:s.width,height:s.height});
      return true;
    }catch(_){return false;}
  }
  function _rdWatchScreen(local){
    const screen=local&&local.getVideoTracks&&local.getVideoTracks()[0];
    if(screen)try{screen.contentHint='detail';}catch(_){}
    if(screen)screen.addEventListener('ended',()=>{
      if(_call&&_call.local===local&&_call.remoteDesktop)_hangup(false);
    },{once:true});
  }
  async function _rdTuneSender(sender){
    if(!sender)return;try{
      const p=sender.getParameters();p.degradationPreference='maintain-resolution';
      if(!p.encodings||!p.encodings.length)p.encodings=[{}];
      p.encodings[0].maxBitrate=24000000;p.encodings[0].maxFramerate=24;
      await sender.setParameters(p);
    }catch(_){}
  }
  /* SEND THE SCREEN AT THE SIZE THE VIEWER CAN ACTUALLY SHOW. This is the other half of the report
   * — "terrible AND INEFFICIENT" — and until now the answer to a 4K desktop in a laptop window was
   * to encode 8.3 megapixels, put them on the wire and decode them so the compositor could discard
   * 87% of every frame. The viewer is the only endpoint that knows how big its window is, so it
   * says, over the control channel it already has, in `scaleResolutionDownBy` steps.
   *
   * Measured on a real loopback with a text-heavy 4K source (see `_rdSourceDownscale`): full
   * resolution is 2992 kbps, 22.6ms of encode per frame here and 3.0ms of decode there; the same
   * view at what the window can show is 1304 kbps, 7.0ms and 0.83ms. Zooming in raises the demand
   * back to 1 automatically, so the detail is there exactly when somebody is looking for it.
   *
   * It can only ever REDUCE work: `d` is clamped at 1, so a hostile or confused viewer can ask for
   * a smaller picture and never for a bigger encoder than the one that already shipped. */
  async function _rdApplyQuality(down){
    const session=_call;
    if(!session||!session.caller||!session.pc)return;
    const d=Math.max(1,Math.min(16,Number(down)||1));
    if(session.rdQuality===d)return;
    session.rdQuality=d;
    const sender=session.pc.getSenders?session.pc.getSenders().find(s=>s.track&&s.track.kind==='video'):null;
    if(!sender||!sender.getParameters)return;
    try{
      const p=sender.getParameters();
      p.degradationPreference='maintain-resolution';
      if(!p.encodings||!p.encodings.length)p.encodings=[{}];
      p.encodings[0].scaleResolutionDownBy=d;
      p.encodings[0].maxFramerate=24;
      // The bitrate ceiling follows the pixel count, or a quarter-size picture is handed the whole
      // 24 Mbps and spends it padding detail nobody can see.
      p.encodings[0].maxBitrate=Math.max(1500000,Math.round(24000000/(d*d)));
      await sender.setParameters(p);
    }catch(_){session.rdQuality=null;}
  }
  async function _rdSwitchScreen(){
    if(!_call||!_call.remoteDesktop||!_call.caller||!_call.pc||_call.nativeSwitching)return;
    const activeCall=_call,old=activeCall.local;let next;
    _rdGrant(false);activeCall.nativeReady=false;activeCall.nativeSwitching=true;
    try{
      try{next=await navigator.mediaDevices.getDisplayMedia({video:{cursor:'always',frameRate:{ideal:20,max:30}},audio:{echoCancellation:false,noiseSuppression:false,autoGainControl:false},systemAudio:'include'});}
      catch(e){if(_call===activeCall)toast(e&&e.name==='NotAllowedError'?'Screen switch cancelled. Sharing the previous screen without control.':_mediaErrMsg(e));return;}
      if(_call!==activeCall){next.getTracks().forEach(t=>t.stop());return;}
      const track=next.getVideoTracks()[0],sender=activeCall.pc.getSenders().find(s=>s.track&&s.track.kind==='video');
      if(!track||!sender){next.getTracks().forEach(t=>t.stop());toast('could not switch screens');return;}
      if(!await _rdConfigureNative(next)){
        next.getTracks().forEach(t=>t.stop());
        if(_call===activeCall)toast('Screen control was not configured. Sharing the previous screen without control.');
        return;
      }
      if(_call!==activeCall){next.getTracks().forEach(t=>t.stop());return;}
      const controlReady=activeCall.nativeReady;
      // Keep control revoked until both the mapping and the outgoing video have changed.
      activeCall.nativeReady=false;
      try{track.contentHint='detail';await sender.replaceTrack(track);await _rdTuneSender(sender);}
      catch(_){next.getTracks().forEach(t=>t.stop());toast('could not switch screens');return;}
      if(_call!==activeCall){next.getTracks().forEach(t=>t.stop());return;}
      if(track.readyState==='ended'){_hangup(false);return;}
      // Replace or remove the previous screen's sound too; otherwise a screen switch can
      // keep sending audio from the source the host just stopped sharing.
      try{
        const audio=next.getTracks().find(t=>t.kind==='audio')||null;
        const audioSender=activeCall.rdAudioSender||activeCall.pc.getSenders().find(s=>s.track?.kind==='audio');
        if(audioSender){await audioSender.replaceTrack(audio);activeCall.rdAudioSender=audioSender;}
        else if(audio){activeCall.rdAudioSender=activeCall.pc.addTrack(audio,activeCall.rdMediaStream||old);if(!await _renegotiate(activeCall))throw new Error('audio negotiation failed');}
      }catch(_){
        next.getTracks().forEach(t=>t.stop());
        if(_call===activeCall){toast('Screen audio could not switch. Sharing stopped.');_hangup(false);}
        return;
      }
      if(_call!==activeCall){next.getTracks().forEach(t=>t.stop());return;}
      if(track.readyState==='ended'){next.getTracks().forEach(t=>t.stop());_hangup(false);return;}
      // Keep the original WebRTC stream ID when adding sound; a new stream would replace
      // the viewer’s video stream with an audio-only ontrack event.
      // Swap local identity before stopping tracks so Stop sharing does not fire on replacement.
      activeCall.local=next;activeCall.nativeReady=controlReady;activeCall.rdQuality=null;_rdWatchScreen(next);
      if(old)old.getTracks().forEach(t=>t.stop());_callUI();
    }finally{activeCall.nativeSwitching=false;}
  }
  function _rdWireControl(ch){
    if(!_call||!_call.remoteDesktop||!ch)return;_call.control=ch;
    ch.onopen=()=>{
      if(!_call||_call.control!==ch)return;
      // Both devices explicitly opened Remote Desktop and proved the same Nostr identity. Enable
      // control as part of connecting so device-to-self sessions do not need a redundant Request
      // click. The host still grants through the channel (rather than trusting viewer-side state).
      if(!_call.caller&&_call.peer===S.ME.pubkey)_rdSend({t:'request'});
      if(_call.caller&&_call.localGeometry)_rdSend(Object.assign({t:'geometry'},_call.localGeometry));
      _callUI();
    };ch.onclose=()=>{if(_call&&_call.control===ch){_call.control=null;_call.controlGranted=false;_call.controlRequested=false;_rdReleaseNative();_callUI();}};
    ch.onmessage=e=>{if(!_call||_call.control!==ch)return;let m;try{if(String(e.data||'').length>512)return;m=JSON.parse(e.data);}catch(_){return;}
      if(m.t==='request'&&!_call.caller){return;} // only the viewer requests; only the sharing host approves
      if(m.t==='request'&&_call.caller){
        // Both endpoints proved possession of the same Nostr identity and both explicitly opened
        // Remote Desktop. That is sufficient consent for this device-to-self case; asking the user
        // to walk back to the sharing machine defeats unattended access. Other identities still
        // receive the explicit Allow/Deny prompt below.
        if(_call.peer===S.ME.pubkey){_rdGrant(true);return;}
        _call.controlRequested=true;_callUI();return;
      }
      if(m.t==='release'&&_call.caller){_call.controlRequested=false;_call.controlGranted=false;_rdReleaseNative();_callUI();return;}
      if(m.t==='grant'&&!_call.caller){_call.controlGranted=!!m.on;_callUI();return;}
      if(m.t==='geometry'&&!_call.caller){
        const width=Math.round(Number(m.width)),height=Math.round(Number(m.height));
        if(width>=64&&height>=64&&width<=32768&&height<=32768){
          _call.remoteGeometry={width,height};
          // A new screen is a new encoder: the resolution agreement went with the old one.
          _call.rdQualitySent=null;_call.rdQualityWant=null;
          _callUI();
        }
        return;
      }
      if(m.t==='quality'&&_call.caller){_rdApplyQuality(m.d);return;}
      if(m.t==='input'&&_call.caller&&_call.controlGranted&&window.pcRemoteControl&&pcRemoteControl.input)
        Promise.resolve(pcRemoteControl.input(m.e||{})).catch(()=>{});
    };
  }
  function _rdGrant(on){if(!_call||!_call.remoteDesktop||!_call.caller)return;if(on&&(_call.nativeReady===false||_call.nativeSwitching)){toast('Remote control is unavailable for this screen. Use Switch screen to choose another.');return;}_call.controlRequested=false;_call.controlGranted=!!on;if(!on)_rdReleaseNative();_rdSend({t:'grant',on:!!on});_callUI();}
  function _rdVideoPoint(video,e,geometry){
    const r=video.getBoundingClientRect();
    const vw=video.videoWidth||(geometry&&geometry.width)||r.width;
    const vh=video.videoHeight||(geometry&&geometry.height)||r.height;
    const va=vw/Math.max(1,vh),ra=r.width/Math.max(1,r.height);
    let left=r.left,top=r.top,width=r.width,height=r.height;
    if(ra>va){width=height*va;left+=(r.width-width)/2;}else{height=width/va;top+=(r.height-height)/2;}
    return {x:Math.max(0,Math.min(1,(e.clientX-left)/Math.max(1,width))),
            y:Math.max(0,Math.min(1,(e.clientY-top)/Math.max(1,height)))};
  }
  /* ZOOM AND PAN LIKE A VNC VIEWER, BECAUSE "FIT" IS NOT READABLE AND 1x/2x/3x/4x IS NOT A ZOOM.
   *
   * Reported twice. First "My desktop 4K screen is too tiny on laptop" — fitting a 3840-wide desktop
   * into a ~1400-wide window is a 36% scale, correct and unreadable. Then, about the control that
   * answered it: "the remote desktop zoom is terrible and inefficient. zooms way too much, don't fit
   * like vnc". Both complaints are the same shape — a viewer that has one number (a multiplier over
   * Fit, in 25% jumps, with no way to steer) where every real viewer has three THINGS:
   *
   *   Fit      the whole screen scaled into the window          (Ctrl+0)
   *   1:1      remote pixels at actual size, panned              (Ctrl+1)   <- what makes 4K readable
   *   free     anything between Fit and 200%, in ~8% notches     (Ctrl+wheel, Ctrl +/-)
   *
   * SCALE HERE IS A FRACTION OF 1:1, never a multiplier over Fit. That is the whole difference: the
   * readout says 100% when a remote pixel is a screen pixel, "Fit" is a scale like any other (0.36
   * on that laptop), and the step is 8% of the picture rather than 25% of whatever Fit happened to
   * be. The CSS transform still needs the multiplier, so it is DERIVED (`scale/fit`) at the one
   * place that writes it, and nothing else in this file reasons in multiplier space.
   *
   * THE ZOOM IS STILL A CSS TRANSFORM ON THE VIDEO, and that is still the load-bearing decision.
   * Every input this viewer sends is mapped through `video.getBoundingClientRect()` —
   * `_rdVideoPoint` for absolute positions and the pointer-lock branch for relative motion — and a
   * transformed element REPORTS ITS TRANSFORMED RECT. So the clicks keep landing where the pointer
   * is with no second copy of the mapping to keep in step. Sizing the element instead (width in px
   * + scroll) would have needed exactly that second copy, which is how a zoomed viewer ends up
   * clicking the wrong thing while looking perfectly fine. A transform is also the only version of
   * this that is FREE to pan: it is a compositor property, so dragging the picture repaints nothing.
   */
  const RD_ZOOM_TOP=2;            // 200% of 1:1 — past that a desktop is a magnifying glass
  const RD_ZOOM_NOTCH=1.08;       // one wheel notch, ~8%; the old control jumped 25% of Fit
  const RD_ZOOM_KEY=1.1;          // one press of + / −
  const RD_MULT_MIN=0.05, RD_MULT_MAX=32;   // sanity rails on the DERIVED multiplier, nothing more
  /* What the viewer may ask the host to send, as `scaleResolutionDownBy`. Steps, not a continuum:
   * every change is a `setParameters` on the sharing machine and a keyframe on the wire, so a
   * slider dragged across the range must not renegotiate the encoder forty times. */
  const RD_SRC_STEPS=[1,1.25,1.5,2,2.5,3,4,6,8,12,16];
  let _rdZoomPref={mode:'fit',scale:1,follow:false};
  try{
    const saved=JSON.parse(localStorage.getItem('pc_rd_zoom')||'null');
    if(saved&&typeof saved==='object')_rdZoomPref=Object.assign(_rdZoomPref,saved);
  }catch(_){}
  /* The range this window allows, in fractions of 1:1. `fit` is the bottom because below it the
   * session would show a band of black beside the desktop and call it zoom — EXCEPT when the remote
   * screen is smaller than the window (a 1280x1024 desktop on a 1440p laptop), where Fit is already
   * an enlargement and 1:1 is BELOW it. 1:1 must stay reachable in that case, so the floor is
   * `min(fit,1)` rather than `fit`, and the ceiling `max(fit,2)` for the same reason in reverse. */
  function _rdZoomBounds(stageW,stageH,remoteW,remoteH){
    const sw=Math.max(1,Number(stageW)||1),sh=Math.max(1,Number(stageH)||1);
    const rw=Math.max(1,Number(remoteW)||1),rh=Math.max(1,Number(remoteH)||1);
    const fit=Math.min(sw/rw,sh/rh);
    return {fit:fit,min:Math.min(fit,1),max:Math.max(fit,RD_ZOOM_TOP),
            content:{width:rw*fit,height:rh*fit}};
  }
  function _rdClampScale(scale,bounds){
    const b=bounds||{min:0.01,max:RD_ZOOM_TOP};
    const s=Number(scale);
    if(!isFinite(s)||s<=0)return b.min;
    return Math.max(b.min,Math.min(b.max,s));
  }
  /* Where the centre of the view may sit, in remote-normalised coordinates, at a given multiplier.
   * The visible window is `1/m` of the screen wide, so its centre cannot be nearer an edge than
   * half of that. Falls out of the transform clamp below and is kept as its own function because
   * the STATE must be clamped too: a drag that keeps pushing past the edge and is only clamped at
   * paint time accumulates an invisible offset, and the drag back does nothing for as long as it
   * took to build up. */
  function _rdClampAt(at,mult){
    const m=Math.max(1,Number(mult)||1),half=0.5/m;
    const v=(n)=>{const x=Number(n);return isFinite(x)?Math.max(half,Math.min(1-half,x)):0.5;};
    return {x:v(at&&at.x),y:v(at&&at.y)};
  }
  /* What the transform must be, given a multiplier over Fit and where in the REMOTE screen we want
   * centred. Pure: no DOM, so the maths can be run without a browser. `view` is THE PICTURE'S OWN
   * BOX in CSS pixels — the letterboxed `object-fit:contain` box, NOT the stage. Those differ
   * whenever the window is not the remote screen's aspect ratio, i.e. almost always, and using the
   * stage over-translates by exactly the letterbox: on a 16:10 laptop window showing a 16:9 desktop
   * the centred point came out ~7% off, which reads as "the zoom drifts". */
  function _rdZoomTransform(mult, view, at){
    const z=Math.max(RD_MULT_MIN,Math.min(RD_MULT_MAX,Number(mult)||1));
    const w=Math.max(1,Number(view&&view.width)||1), h=Math.max(1,Number(view&&view.height)||1);
    const p=_rdClampAt(at,z);
    // Room the magnified picture has to slide from centred. Never negative: BELOW Fit (which 1:1 is,
    // for a remote screen smaller than the window) there is nothing to pan and the offset is 0.
    // Deliberately REDUNDANT with the `_rdClampAt` above — the two bound the same thing by different
    // routes, and a mutation of either alone is provably unobservable. Kept because this is the last
    // line before a `translate()` reaches the compositor, and the failure it prevents (a band of
    // black beside the desktop, clicks mapped through a picture that is not where it should be) has
    // no other symptom.
    const slackX=Math.max(0,w*(z-1)/2), slackY=Math.max(0,h*(z-1)/2);
    const x=Math.max(-slackX,Math.min(slackX,(0.5-p.x)*w*z));
    const y=Math.max(-slackY,Math.min(slackY,(0.5-p.y)*h*z));
    return {scale:z,x:Math.round(x),y:Math.round(y)};
  }
  /* The remote point the middle of the stage is ACTUALLY showing, read back off the transform the
   * clamp produced rather than off the request. Anchored zoom has to start from where the picture
   * is, not from where it was asked to be, or a zoom taken while panned against an edge jumps. */
  function _rdZoomCentre(t,view){
    const w=Math.max(1,Number(view&&view.width)||1), h=Math.max(1,Number(view&&view.height)||1);
    const z=Math.max(RD_MULT_MIN,Number(t&&t.scale)||1);
    return {x:0.5-(Number(t&&t.x)||0)/(w*z), y:0.5-(Number(t&&t.y)||0)/(h*z)};
  }
  /* ANCHORED ZOOM: keep the point under the cursor exactly where it is. `pointer` and `centre` are
   * remote-normalised; the offset between them shrinks by the ratio of the multipliers, because the
   * same number of SCREEN pixels is now fewer remote pixels. Zooming at the centre of the window is
   * the degenerate case and comes out as a no-op, which is what makes the +/− buttons behave. */
  function _rdZoomAnchor(prevMult,nextMult,pointer,centre){
    const a=Math.max(RD_MULT_MIN,Number(prevMult)||1), b=Math.max(RD_MULT_MIN,Number(nextMult)||1);
    const px=Number(pointer&&pointer.x),py=Number(pointer&&pointer.y);
    const cx=Number(centre&&centre.x),cy=Number(centre&&centre.y);
    if(!isFinite(px)||!isFinite(py)||!isFinite(cx)||!isFinite(cy))return {x:cx||0.5,y:cy||0.5};
    const r=a/b;
    return {x:px-(px-cx)*r, y:py-(py-cy)*r};
  }
  /* HOW MUCH SCREEN THE HOST NEEDS TO SEND, and the answer to "inefficient". At Fit, a 4K desktop
   * shown 1400 pixels wide is being encoded at 3840, pushed through the network at 3840 and decoded
   * at 3840 so the compositor can throw 87% of every frame away. Measured on a loopback with a
   * text-heavy 4K source: 2992 kbps, 22.6ms of encode per frame on the sharing machine and 3.0ms of
   * decode on this one. The same picture at the resolution the window can actually show costs
   * 1304 kbps and 7.0ms/0.83ms. Nothing about the view changes; two thirds of the cost goes away.
   * `displayPx` is how wide the remote screen is DRAWN right now (so zooming in raises the demand
   * back to full resolution, which is exactly when the detail is wanted), and `dpr` is there because
   * a HiDPI laptop showing 1400 CSS pixels really is showing 2800 device pixels. */
  function _rdSourceDownscale(displayPx,remotePx,dpr){
    const shown=Math.max(1,Number(displayPx)||1)*Math.max(1,Number(dpr)||1);
    const need=Math.max(1,(Math.max(1,Number(remotePx)||1))/shown);
    let pick=1;
    for(const step of RD_SRC_STEPS)if(step<=need+1e-6)pick=step;
    return pick;
  }
  /* LAYOUT pixels, and its own function so a test can hold it to that. `getBoundingClientRect()`
   * is in VISUAL pixels — this client scales whole pages with `body{zoom}` — while a CSS
   * `translate()` is resolved in layout pixels, so measuring the stage with the rect over-translates
   * by exactly the page zoom and the pointer stops agreeing with the picture. Measured while this
   * was wrong: at 2x centred on 0.25, the middle of the stage was 0.32 of the remote screen. */
  function _rdStageBox(stage){
    if(!stage)return {width:1,height:1};
    const rect=stage.getBoundingClientRect();
    return {width:stage.clientWidth||rect.width||1, height:stage.clientHeight||rect.height||1};
  }
  /* The ratio between the two spaces above, measured off the stage itself rather than assumed. A
   * pointer delta arrives in VISUAL pixels and a pan is applied in LAYOUT ones; on a page carrying
   * `body{zoom}` (this client sets one per viewport) a drag would otherwise move the picture by the
   * page zoom too much, which is a drag that outruns the cursor. */
  function _rdVisualRatio(stage){
    if(!stage)return 1;
    const rect=stage.getBoundingClientRect(),layout=stage.clientWidth;
    if(!layout||!rect.width)return 1;
    const r=rect.width/layout;
    return isFinite(r)&&r>0.05&&r<20?r:1;
  }
  function _rdStageOf(video){
    return video&&video.parentElement&&video.parentElement.classList&&
           video.parentElement.classList.contains('rd-stage')?video.parentElement:video;
  }
  function _rdZoomState(){
    if(!_call)return null;
    if(!_call.zoom)_call.zoom={mode:_rdZoomPref.mode==='actual'||_rdZoomPref.mode==='free'?_rdZoomPref.mode:'fit',
                               scale:Number(_rdZoomPref.scale)||1,at:{x:.5,y:.5},follow:!!_rdZoomPref.follow};
    return _call.zoom;
  }
  /* The mode is remembered for the NEXT session as well as this one: somebody who works at 1:1 is
   * not choosing it per connection, and re-choosing it every time is the "terrible" in the report. */
  function _rdZoomRemember(){
    const s=_call&&_call.zoom;if(!s)return;
    _rdZoomPref={mode:s.mode,scale:s.scale,follow:!!s.follow};
    try{localStorage.setItem('pc_rd_zoom',JSON.stringify(_rdZoomPref));}catch(_){}
  }
  /* Everything the current frame needs, measured once. Returns null when there is no session video
   * to measure, so every caller degrades to "do nothing" rather than to a guess. */
  function _rdZoomMetrics(){
    const video=document.getElementById('call-remote');
    if(!video)return null;
    const stage=_rdStageOf(video),g=_call&&_call.remoteGeometry;
    /* THE MINIMISED THUMBNAIL HAS NO STAGE: `.rd-stage` is `display:contents` there, so it has no
     * box at all — `clientWidth` is 0 and `getBoundingClientRect()` is all zeros. Measured through
     * the 1x1 fallback that produced, Fit came out as 1/3840 and the derived multiplier pinned at
     * the sanity ceiling, i.e. a 132px thumbnail scaled 32x. The video is absolutely positioned over
     * the whole thumbnail in that mode, so IT is the box, and measuring it is also what lets a
     * minimised session ask the host for a thumbnail-sized stream instead of a 4K one. */
    let box=_rdStageBox(stage);
    if(box.width<8||box.height<8)box=_rdStageBox(video);
    if(box.width<8||box.height<8)return null;
    const rw=video.videoWidth||(g&&g.width)||box.width, rh=video.videoHeight||(g&&g.height)||box.height;
    const b=_rdZoomBounds(box.width,box.height,rw,rh);
    b.remote={width:Math.max(1,rw),height:Math.max(1,rh)};
    b.stage=box;b.video=video;b.stageEl=stage;b.ratio=_rdVisualRatio(stage);
    return b;
  }
  function _rdScaleNow(state,m){
    if(!state||!m)return 1;
    if(state.mode==='fit')return m.fit;
    if(state.mode==='actual')return _rdClampScale(1,m);
    return _rdClampScale(state.scale,m);
  }
  let _rdZoomFrame=0;
  /* Coalesced, because the callers are a pointer stream. The follow-the-pointer path used to write
   * `style.transform` on every `pointermove` and then read `getBoundingClientRect()` on the next
   * one, which is a forced synchronous layout per mouse move on the screen whose whole job is to
   * feel immediate. */
  function _rdApplyZoomSoon(){
    if(_rdZoomFrame)return;
    const raf=(typeof requestAnimationFrame==='function')?requestAnimationFrame:(fn)=>setTimeout(fn,16);
    _rdZoomFrame=raf(()=>{_rdZoomFrame=0;_rdApplyZoom();})||1;
  }
  /* Paint it. Fit clears the transform entirely rather than writing `scale(1)`, so the ordinary
   * session is byte-identical to what shipped before this control existed. */
  function _rdApplyZoom(){
    const state=_rdZoomState(),m=_rdZoomMetrics();
    if(!state||!m)return;
    const video=m.video;
    /* A THUMBNAIL IS ALWAYS FIT. The zoom row is hidden in that mode and there is nowhere to pan to,
     * so a remembered 1:1 must not magnify a 132px picture the moment somebody minimises a session.
     * The resolution request below still runs, because a thumbnail is exactly when the host should
     * stop encoding 4K. */
    if(video.closest&&video.closest('.call-mini')){
      if(video.style.transform){video.style.transform='';video.style.transformOrigin='';}
      video.classList.remove('rd-pannable');
      _rdRequestQuality(m,1);
      return;
    }
    const scale=_rdScaleNow(state,m),mult=scale/Math.max(1e-6,m.fit);
    state.at=_rdClampAt(state.at,mult);
    const t=_rdZoomTransform(mult,m.content,state.at);
    const css=(Math.abs(t.scale-1)<0.001&&!t.x&&!t.y)?'':'translate('+t.x+'px,'+t.y+'px) scale('+t.scale+')';
    if(video.style.transform!==css){video.style.transformOrigin=css?'center center':'';video.style.transform=css;}
    _rdZoomUI(state,m,scale,t,mult);
    _rdRequestQuality(m,mult);
  }
  /* The readout, the buttons, the slider and the two edge indicators. Every element is optional:
   * this same code runs in harnesses that mount the video and nothing else. */
  function _rdZoomUI(state,m,scale,t,mult){
    const by=(id)=>document.getElementById(id);
    const pct=Math.round(scale*100);
    const label=by('rd-zoom-level');
    if(label){label.textContent=pct+'%';
      label.title=state.mode==='fit'?'Whole screen ('+pct+'%). Click for Fit.':'Click to fit the whole screen (Ctrl+0)';}
    const out=by('rd-zoom-out');if(out)out.disabled=scale<=m.min+1e-4;
    const inn=by('rd-zoom-in');if(inn)inn.disabled=scale>=m.max-1e-4;
    const fitBtn=by('rd-zoom-fit');if(fitBtn)fitBtn.classList.toggle('on',state.mode==='fit');
    const oneBtn=by('rd-zoom-actual');if(oneBtn)oneBtn.classList.toggle('on',state.mode==='actual');
    const follow=by('rd-zoom-follow');
    if(follow){follow.classList.toggle('on',!!state.follow);follow.setAttribute('aria-pressed',state.follow?'true':'false');}
    const range=by('rd-zoom-range');
    if(range&&document.activeElement!==range){
      const v=Math.round(_rdZoomSlider(scale,m)*1000);
      if(String(v)!==range.value)range.value=String(v);
    }
    /* WHERE YOU ARE IN THE REMOTE SCREEN. A magnified desktop with no indicator is the other half of
     * "don't fit like vnc": the picture is readable and there is nothing to say whether the rest of
     * it is above, below or already on screen. Hidden at or below Fit, where there is no rest. */
    const centre=_rdZoomCentre(t,m.content),span=1/Math.max(1,mult);
    for(const axis of ['x','y']){
      const bar=by(axis==='x'?'rd-scroll-h':'rd-scroll-v');if(!bar)continue;
      const thumb=bar.firstElementChild;
      if(mult<=1.001){bar.hidden=true;continue;}
      bar.hidden=false;
      if(!thumb)continue;
      const start=Math.max(0,Math.min(1-span,centre[axis]-span/2));
      thumb.style[axis==='x'?'left':'top']=(start*100).toFixed(2)+'%';
      thumb.style[axis==='x'?'width':'height']=(span*100).toFixed(2)+'%';
    }
    if(m.video&&m.video.classList)
      m.video.classList.toggle('rd-pannable',mult>1.001);
  }
  /* The slider is LOGARITHMIC between Fit and the ceiling. Linear, the 36%-to-100% half of that
   * laptop's range — the half that contains every scale anybody reads text at — would be the first
   * third of the track, and the top two thirds would be 100% to 200%. */
  function _rdZoomSlider(scale,m){
    const lo=Math.log(Math.max(1e-6,m.min)),hi=Math.log(Math.max(1e-6,m.max));
    if(hi-lo<1e-6)return 0;
    return Math.max(0,Math.min(1,(Math.log(Math.max(1e-6,scale))-lo)/(hi-lo)));
  }
  function _rdZoomFromSlider(t,m){
    const lo=Math.log(Math.max(1e-6,m.min)),hi=Math.log(Math.max(1e-6,m.max));
    return Math.exp(lo+(hi-lo)*Math.max(0,Math.min(1,Number(t)||0)));
  }
  /* Ask the host for the resolution this window can actually show. Debounced and de-duplicated: the
   * message is cheap but `setParameters` on the sharing machine is not, and a dragged slider would
   * otherwise re-tune the encoder on every frame. */
  let _rdQualityTimer=0;
  function _rdRequestQuality(m,mult){
    if(!_call||_call.caller||!m)return;
    const shownPx=m.content.width*Math.max(1,mult);
    const want=_rdSourceDownscale(shownPx,m.remote.width,(typeof devicePixelRatio==='number'&&devicePixelRatio)||1);
    if(_call.rdQualityWant===want)return;
    _call.rdQualityWant=want;
    if(_rdQualityTimer)clearTimeout(_rdQualityTimer);
    _rdQualityTimer=setTimeout(()=>{
      _rdQualityTimer=0;
      if(!_call||_call.caller)return;
      const d=_call.rdQualityWant;
      if(d===_call.rdQualitySent)return;
      /* MARK IT ONLY IF IT WENT OUT. `_rdApplyZoom` runs from `_callUI`, which fires on the `ontrack`
       * event — routinely BEFORE the control channel finishes opening, so this message is dropped on
       * the floor at exactly the moment it is first sent. Recorded as sent anyway (the latch-before-
       * the-attempt shape) it would never be re-sent, and the session would run at full 4K for its
       * whole life with nothing in any log. Clearing `rdQualityWant` re-arms it, and the channel's
       * own `onopen` calls `_callUI` — so the first thing that happens after the channel exists is
       * another attempt. */
      if(_rdSend({t:'quality',d:d}))_call.rdQualitySent=d;
      else _call.rdQualityWant=null;
    },400);
  }
  /* --- the four things a person can do to the view ---------------------------------------- */
  function _rdZoomSetMode(mode){
    const state=_rdZoomState();if(!state)return;
    state.mode=mode==='actual'?'actual':mode==='free'?'free':'fit';
    if(state.mode==='actual')state.scale=1;
    if(state.mode==='fit')state.at={x:.5,y:.5};
    _rdApplyZoom();_rdZoomRemember();
  }
  /* `pointer` is the remote-normalised point to keep still — the cursor for Ctrl+wheel, nothing for
   * the buttons and the keyboard, which zoom about the middle of what is on screen. */
  function _rdZoomBy(factor,pointer){
    const state=_rdZoomState(),m=_rdZoomMetrics();
    if(!state||!m)return;
    const prev=_rdScaleNow(state,m),next=_rdClampScale(prev*(Number(factor)||1),m);
    if(Math.abs(next-prev)<1e-6&&!pointer)return;
    const pm=prev/Math.max(1e-6,m.fit),nm=next/Math.max(1e-6,m.fit);
    if(pointer){
      const t=_rdZoomTransform(pm,m.content,_rdClampAt(state.at,pm));
      state.at=_rdZoomAnchor(pm,nm,pointer,_rdZoomCentre(t,m.content));
    }
    state.at=_rdClampAt(state.at,nm);
    // Landing back ON Fit says so, so the readout and the highlighted button agree with the picture.
    state.mode=Math.abs(next-m.fit)<1e-3?'fit':'free';
    state.scale=next;
    _rdApplyZoom();_rdZoomRemember();
  }
  function _rdZoomToScale(scale,pointer){
    const state=_rdZoomState(),m=_rdZoomMetrics();
    if(!state||!m)return;
    const prev=_rdScaleNow(state,m);
    _rdZoomBy(_rdClampScale(scale,m)/Math.max(1e-6,prev),pointer);
  }
  /* PAN, in the VISUAL pixels a pointer delta and a wheel arrive in. Returns whether anything moved,
   * so a gesture that cannot pan can fall through to being a remote click instead of being eaten. */
  function _rdPanBy(dx,dy){
    const state=_rdZoomState(),m=_rdZoomMetrics();
    if(!state||!m)return false;
    const mult=_rdScaleNow(state,m)/Math.max(1e-6,m.fit);
    if(mult<=1.001)return false;
    const ratio=m.ratio||1;
    const before=_rdClampAt(state.at,mult);
    const next=_rdClampAt({x:before.x-(Number(dx)||0)/ratio/(m.content.width*mult),
                           y:before.y-(Number(dy)||0)/ratio/(m.content.height*mult)},mult);
    if(Math.abs(next.x-before.x)<1e-9&&Math.abs(next.y-before.y)<1e-9)return false;
    state.at=next;_rdApplyZoomSoon();return true;
  }
  /* FOLLOWING THE REMOTE POINTER IS OPT-IN AND IT EDGE-SCROLLS — it does not re-centre. The old
   * behaviour re-centred the view on the cursor whenever it moved more than 2% of the screen, which
   * is a picture that slides while you are trying to read it: half of "the zoom is terrible". On,
   * it now pans only far enough to bring the pointer back inside a margin, the way a text editor
   * scrolls to a caret; off (the default), the view never moves on its own at all. */
  function _rdFollowPointer(p){
    const state=_rdZoomState();
    if(!state||!state.follow||!p)return;
    const m=_rdZoomMetrics();if(!m)return;
    const mult=_rdScaleNow(state,m)/Math.max(1e-6,m.fit);
    if(mult<=1.001)return;
    const reach=(0.5/mult)*0.75;      // keep the cursor inside the middle 75% of the window
    const at=_rdClampAt(state.at,mult);
    let x=at.x,y=at.y,moved=false;
    if(p.x<x-reach){x=p.x+reach;moved=true;}else if(p.x>x+reach){x=p.x-reach;moved=true;}
    if(p.y<y-reach){y=p.y+reach;moved=true;}else if(p.y>y+reach){y=p.y-reach;moved=true;}
    if(!moved)return;
    state.at=_rdClampAt({x:x,y:y},mult);_rdApplyZoomSoon();
  }
  let _rdViewerCleanup=null;
  function _rdBindViewer(video){
    if(!video||video.dataset.rdControl)return;
    if(_rdViewerCleanup)_rdViewerCleanup();
    video.dataset.rdControl='1';video.tabIndex=0;
    const session=_call,held=new Set(),keys=new Set(),listeners=[];
    let position={x:.5,y:.5},locked=false;
    const active=()=>_call===session&&!!(_call&&_call.remoteDesktop&&!_call.caller&&_call.controlGranted)&&!video.closest('.call-mini');
    const listen=(target,name,fn,opts)=>{target.addEventListener(name,fn,opts);listeners.push(()=>target.removeEventListener(name,fn,opts));};
    const point=e=>{
      if(document.pointerLockElement!==video)position=_rdVideoPoint(video,e,_call&&_call.remoteGeometry);
      /* OPT-IN, and an edge-scroll rather than a re-centre. Following used to be unconditional and
       * re-centred on every 2% of movement, which is a picture sliding under somebody who is trying
       * to read it — the reported "zooms way too much". */
      _rdFollowPointer(position);
      return position;
    };
    /* Is there anything to pan? Below Fit+epsilon the whole screen is on screen, so a middle-click
     * is a middle-click and belongs to the other machine. */
    const canPan=()=>{
      const state=_call&&_call.zoom,m=_rdZoomMetrics();
      if(!state||!m)return false;
      return _rdScaleNow(state,m)/Math.max(1e-6,m.fit)>1.001;
    };
    /* DRAG-TO-PAN, and the whole difficulty is that every drag is already spoken for: when this
     * viewer has control, a press is a press on the other machine. So panning takes only the two
     * gestures that cannot be one — the MIDDLE button (while there is something to pan; at Fit it
     * still goes through as a middle click), and a PLAIN DRAG when this viewer is not sending input
     * at all, which is the view-only reading case the zoom exists for in the first place. */
    let pan=null;
    const release=()=>{
      if(_call===session){
        for(const button of held)_rdSend({t:'input',e:{type:'button',button,down:false,x:position.x,y:position.y}});
        for(const code of keys)_rdSend({t:'input',e:{type:'key',code,down:false}});
      }
      held.clear();keys.clear();
    };
    const unlock=()=>{release();if(document.pointerLockElement===video)document.exitPointerLock();if(document.activeElement===video)video.blur();};
    video.rdUnlock=unlock;
    listen(video,'pointerdown',e=>{
      if(video.closest('.call-mini'))return;
      if((e.button===1||!active())&&canPan()){
        pan={id:e.pointerId,x:e.clientX,y:e.clientY};
        try{video.setPointerCapture(e.pointerId);}catch(_){}
        video.classList.add('rd-panning');
        e.preventDefault();return;
      }
      if(!active()||e.button>2)return;
      video.focus({preventScroll:true});
      try{video.setPointerCapture(e.pointerId);}catch(_){}
      const p=point(e);held.add(e.button);
      _rdSend({t:'input',e:{type:'button',button:Math.min(2,e.button|0),down:true,x:p.x,y:p.y}});
      if(e.pointerType==='mouse'&&document.pointerLockElement!==video&&video.requestPointerLock){
        try{const pending=video.requestPointerLock();if(pending&&pending.catch)pending.catch(()=>{});}catch(_){}
      }
      e.preventDefault();
    });
    listen(video,'pointermove',e=>{
      if(pan&&pan.id===e.pointerId){
        /* Under pointer lock `clientX` is frozen, so the delta has to come from `movementX`; the
         * unlocked case uses the real coordinates because a captured pointer can outrun the
         * movement deltas on a slow frame. */
        const lockedNow=document.pointerLockElement===video;
        const dx=lockedNow?(e.movementX||0):e.clientX-pan.x, dy=lockedNow?(e.movementY||0):e.clientY-pan.y;
        if(!lockedNow){pan.x=e.clientX;pan.y=e.clientY;}
        _rdPanBy(dx,dy);e.preventDefault();return;
      }
      if(!active())return;
      if(document.pointerLockElement===video){
        // Reuse the exact contain transform, including letterboxing and every viewer resize.
        const r=video.getBoundingClientRect(),g=_call.remoteGeometry;
        const vw=video.videoWidth||(g&&g.width)||r.width,vh=video.videoHeight||(g&&g.height)||r.height;
        const scale=Math.min(r.width/vw,r.height/vh);
        position={x:Math.max(0,Math.min(1,position.x+e.movementX/Math.max(1,vw*scale))),
                  y:Math.max(0,Math.min(1,position.y+e.movementY/Math.max(1,vh*scale)))};
      }
      const p=point(e);_rdSend({t:'input',e:{type:'absolute',x:p.x,y:p.y}});e.preventDefault();
    });
    const endPan=()=>{if(!pan)return false;pan=null;video.classList.remove('rd-panning');return true;};
    const up=e=>{if(endPan())return;if(!held.delete(e.button))return;const p=point(e);_rdSend({t:'input',e:{type:'button',button:Math.min(2,e.button|0),down:false,x:p.x,y:p.y}});e.preventDefault();};
    listen(video,'pointerup',up);
    listen(video,'pointercancel',e=>{endPan();release();});
    listen(video,'lostpointercapture',()=>{endPan();if(document.pointerLockElement!==video)release();});
    /* Ctrl/Cmd + wheel is the zoom every viewer and browser already uses, and it must be taken
     * BEFORE the remote-scroll branch or the other machine scrolls instead of this one magnifying.
     * ANCHORED AT THE POINTER, one ~8% notch at a time: the old control stepped 25% of Fit about the
     * centre of the window, which is "zooms way too much" and also moves what you were looking at. */
    listen(video,'wheel',e=>{
      if(e.ctrlKey||e.metaKey){
        const at=document.pointerLockElement===video?position:_rdVideoPoint(video,e,_call&&_call.remoteGeometry);
        _rdZoomBy(e.deltaY<0?RD_ZOOM_NOTCH:1/RD_ZOOM_NOTCH,at);
        e.preventDefault();e.stopPropagation();return;
      }
      /* A viewer that is NOT sending input has no remote scroll to forward, so the wheel is the pan
       * every VNC viewer gives you and shift is its horizontal half. While controlling, the wheel
       * still belongs to the other machine (scrolling the remote window is the point), and panning
       * is the middle-drag, the arrow keys and the follow toggle. */
      if(!active()&&canPan()){
        const dx=e.shiftKey?(e.deltaY||e.deltaX):e.deltaX, dy=e.shiftKey?0:e.deltaY;
        if(_rdPanBy(-dx,-dy)){e.preventDefault();e.stopPropagation();}
      }
    },{passive:false,capture:true});
    listen(video,'wheel',e=>{if(!active()||e.ctrlKey||e.metaKey)return;const p=point(e);_rdSend({t:'input',e:{type:'absolute',x:p.x,y:p.y}});_rdSend({t:'input',e:{type:'wheel',dy:Math.max(-12,Math.min(12,Math.sign(e.deltaY)))}});e.preventDefault();},{passive:false});
    listen(video,'contextmenu',e=>{if(active())e.preventDefault();});
    /* THE VIEWER'S OWN KEYS, and they must be taken before the branch that forwards keys to the
     * other machine — both are capture-phase on `document`, so registration order is the priority.
     * Scoped to a live, non-minimised desktop session: Ctrl+0 belongs to the browser everywhere
     * else in this app, and stealing it globally would break page zoom for the whole client. */
    const viewerKeysApply=()=>_call===session&&!!(_call&&_call.remoteDesktop&&!_call.caller)&&!video.closest('.call-mini');
    listen(document,'keydown',e=>{
      if(!viewerKeysApply()||!(e.ctrlKey||e.metaKey)||e.altKey)return;
      const code=e.code,key=e.key;
      let handled=true;
      if(code==='Digit0'||key==='0')_rdZoomSetMode('fit');
      else if(code==='Digit1'||key==='1')_rdZoomSetMode('actual');
      else if(key==='+'||key==='='||code==='Equal'||code==='NumpadAdd')_rdZoomBy(RD_ZOOM_KEY);
      else if(key==='-'||key==='_'||code==='Minus'||code==='NumpadSubtract')_rdZoomBy(1/RD_ZOOM_KEY);
      /* Arrow keys pan by a quarter of the visible window — the pan gesture that exists while this
       * viewer has control and every drag is already going to the other machine. */
      else if(code==='ArrowLeft'||code==='ArrowRight'||code==='ArrowUp'||code==='ArrowDown'){
        const m=_rdZoomMetrics();
        if(!m)handled=false;
        else{
          const stepX=m.stage.width/4,stepY=m.stage.height/4;
          handled=_rdPanBy(code==='ArrowLeft'?stepX:code==='ArrowRight'?-stepX:0,
                           code==='ArrowUp'?stepY:code==='ArrowDown'?-stepY:0);
        }
      }
      else handled=false;
      if(handled){e.preventDefault();e.stopPropagation();}
    },true);
    /* Fit is a function of the WINDOW, so it has to be recomputed when the window changes — and the
     * resolution asked of the host with it, which is why a resize is an efficiency event and not
     * only a layout one. ResizeObserver rather than `window.resize` because this session lives in a
     * PosterChanOS window that is resized without the page ever resizing. */
    listen(window,'resize',()=>_rdApplyZoomSoon());
    try{
      if(typeof ResizeObserver==='function'){
        const ro=new ResizeObserver(()=>_rdApplyZoomSoon());
        ro.observe(_rdStageOf(video));
        listeners.push(()=>ro.disconnect());
      }
    }catch(_){}
    listen(document,'pointerlockchange',()=>{
      const next=document.pointerLockElement===video;
      if(locked&&!next){release();video.blur();}locked=next;
      if(next&&!active())unlock();
    });
    listen(window,'blur',unlock);
    listen(document,'visibilitychange',()=>{if(document.hidden)unlock();});
    listen(video,'blur',()=>{if(document.pointerLockElement!==video)release();});
    for(const name of ['keydown','keyup'])listen(document,name,e=>{
      if(!active()||(document.activeElement!==video&&document.pointerLockElement!==video))return;
      if(e.code==='Escape'){unlock();return;}
      const code=_RD_KEYS[e.code];if(!code)return;
      const down=name==='keydown';if(down)keys.add(code);else if(!keys.delete(code))return;
      e.preventDefault();e.stopPropagation();_rdSend({t:'input',e:{type:'key',code,down}});
    },true);
    _rdViewerCleanup=()=>{endPan();unlock();listeners.forEach(off=>off());delete video.dataset.rdControl;delete video.rdUnlock;_rdViewerCleanup=null;};
  }
  // getUserMedia failures were all reported as "permission needed", which is wrong in the most common
  // case: on an insecure origin (http:// on a LAN IP) navigator.mediaDevices is undefined, the browser
  // never prompts, and there is genuinely nothing for the user to click — hence "no way to do it".
  function _mediaErrMsg(e){
    if(!(navigator.mediaDevices && navigator.mediaDevices.getUserMedia) || !window.isSecureContext)
      return 'Calls need a secure connection — open this over HTTPS (not a plain http:// IP address).';
    const n=(e&&e.name)||'';
    if(n==='NotAllowedError')  return 'Camera/mic blocked — click the camera or 🔒 icon in the address bar, allow access, then try again.';
    if(n==='NotFoundError'||n==='OverconstrainedError') return 'No camera or microphone found on this device.';
    if(n==='NotReadableError') return 'Your camera/mic is already in use by another app.';
    return 'Could not start the camera/mic'+(n?' ('+n+')':'')+'.';
  }
  function _getMedia(video, remoteHost, remoteGuest){
    /* The HOST chooses a screen and optional system/tab sound through the browser/Electron picker;
     * the viewer sends no camera or microphone back. Keeping it on the call transport gives it the
     * same encrypted Nostr signaling and TURN fallback without pretending a camera call is a
     * desktop-sharing session. System sound must bypass speech processing: echo cancellation and
     * noise suppression can erase music/game audio from the playback monitor. */
    if(remoteHost) return navigator.mediaDevices.getDisplayMedia({video:{cursor:'always',frameRate:{ideal:20,max:30}},audio:{echoCancellation:false,noiseSuppression:false,autoGainControl:false},systemAudio:'include'});
    if(remoteGuest) return Promise.resolve(new MediaStream());
    return navigator.mediaDevices.getUserMedia({ audio:true, video: video ? {width:{ideal:640},height:{ideal:480},frameRate:{ideal:24}} : false });
  }
  function _hasLiveVideo(stream){ return !!(stream && stream.getVideoTracks().some(t=>t.readyState==='live')); }
  // Renegotiate the existing PeerConnection (used to add/drop video mid-call). Manual offer/answer over the
  // same Nostr channel with a polite-peer (higher pubkey) rollback so a simultaneous both-add doesn't wedge.
  async function _renegotiate(session=_call){
    if(!session||_call!==session||!session.pc)return false;
    const pc=session.pc,current=()=>_call===session&&session.pc===pc&&pc.signalingState!=='closed';
    try{session.makingOffer=true;
      const o=await pc.createOffer();if(!current())return false;
      await pc.setLocalDescription(o);if(!current())return false;
      await _callSend(session.peer,{v:1,callId:session.id,t:'reoffer',sdp:pc.localDescription.sdp});
      return current();
    }catch(_){return false;}finally{session.makingOffer=false;}
  }
  // Turn the camera on (add a video track + renegotiate) or off (stop + drop it) mid-call — lets an
  // audio-only call become video with one tap, in either direction.
  async function _toggleVideo(){
    if(!_call || !_call.pc || !_call.local) return;
    if(_hasLiveVideo(_call.local)){
      _call.local.getVideoTracks().forEach(t=>{ try{ t.stop(); }catch(_){} _call.local.removeTrack(t); });
      _call.pc.getSenders().filter(s=>s.track && s.track.kind==='video').forEach(s=>{ try{ _call.pc.removeTrack(s); }catch(_){} });
      _call.camOff=false; _callUI(); await _renegotiate(); return;
    }
    let vs; try{ vs=await navigator.mediaDevices.getUserMedia({video:{width:{ideal:640},height:{ideal:480},frameRate:{ideal:24}}}); }
    catch(e){ toast(_mediaErrMsg(e)); return; }
    if(!_call){ vs.getTracks().forEach(t=>t.stop()); return; }
    const vt=vs.getVideoTracks()[0]; if(!vt){ vs.getTracks().forEach(t=>t.stop()); return; }
    _call.local.addTrack(vt); _call.pc.addTrack(vt, _call.local); _call.camOff=false; _callUI();
    // The video transceiver only exists NOW — every call from the Calls view starts audio-first, so
    // this is the usual route to video in a 1:1 call and would otherwise be the one path with no
    // codec preference at all. Must precede the renegotiation offer to reach the SDP.
    _preferH264(_call.pc);
    await _renegotiate();
  }
  async function startCall(peerHex, opts){
    if(S.GUEST){ _guestPrompt(); return; }
    const remoteDesktop = !!(opts && opts.remoteDesktop);
    // Calling your own identity makes no sense for voice/video, but it is exactly how a user shares
    // one signed-in device to another. Device ids on the signaling frames prevent this renderer
    // from answering its own invite; another armed device with the same key accepts it.
    if(!peerHex || (peerHex===S.ME.pubkey && !remoteDesktop)){ return; }
    if(_call){ toast('already in a call'); return; }
    const video = remoteDesktop || !!(opts && opts.video);
    const signalRelays=[...new Set(((opts&&opts.signalRelays)||[]).map(normalizeRelay).filter(Boolean))];
    _call = { id:_rid(), peer:peerHex, pc:null, local:null, remote:null, video, remoteDesktop,
              signalRelays, signalClose:null, state:'calling', caller:true, pendingIce:[] };
    const activeCall=_call;
    if(signalRelays.length && Relay.subscribeFrom){
      _call.signalClose=Relay.subscribeFrom(signalRelays,
        [{ '#p':[S.ME.pubkey], kinds:[CALL_KIND], since:Math.floor(Date.now()/1000)-5 }],
        {onEvent:_onCallEvent,timeout:120000});
    }
    _callUI();
    let local;
    try{ local = await _getMedia(video, remoteDesktop, false); }catch(e){ if(_call===activeCall){toast(remoteDesktop && e&&e.name==='NotAllowedError'?'screen sharing cancelled':_mediaErrMsg(e));_callTeardown();}return; }
    if(_call!==activeCall){ local.getTracks().forEach(t=>t.stop()); return; }   // hung up while prompting
    _call.local = local;
    if(remoteDesktop)_call.rdMediaStream=local;
    /* The browser's native "Stop sharing" button ends the capture track without touching our call
     * state.  Treat that as an intentional hangup: otherwise the viewer is left on a frozen last
     * frame and the host still sees a misleading "connected" overlay. */
    if(remoteDesktop){
      _rdWatchScreen(local);
      if(!await _rdConfigureNative(local)){
        local.getTracks().forEach(t=>t.stop());
        if(_call===activeCall){toast('Screen sharing cancelled: the shared monitor was not selected.');_callTeardown();}
        return;
      }
      if(_call!==activeCall)return;
    }
    const ice = await _fetchIceServers();
    if(_call!==activeCall){ local.getTracks().forEach(t=>t.stop()); return; }
    const pc = _newPc(ice.iceServers); _call.pc = pc;
    if(remoteDesktop) _rdWireControl(pc.createDataChannel('posterchan-control',{ordered:true}));
    local.getTracks().forEach(t=>{const sender=pc.addTrack(t,local);if(remoteDesktop&&t.kind==='video')_rdTuneSender(sender);});
    if(remoteDesktop)_preferScreenCodec(pc);else _preferH264(pc);
    // Guard the SDP dance: a createOffer/setLocalDescription rejection (SDP/codec/hardware quirk) or a
    // hangup during these awaits must tear down cleanly, not wedge _call in 'calling' forever.
    try{
      const offer = await pc.createOffer();
      if(!_call || _call.pc!==pc){ return; }
      await pc.setLocalDescription(offer);
      if(!_call || _call.pc!==pc){ return; }
    }catch(_){ if(_call===activeCall){toast('couldn’t start the call');_hangup(false);}return; }
    _callUI();
    await _callSend(peerHex, {v:1, callId:_call.id, t:'invite', video, remoteDesktop,
                              sdp: pc.localDescription.sdp});
    if(_call===activeCall) _call.timeout = setTimeout(()=>{ if(_call && _call.state==='calling'){ toast('no answer'); _hangup(false); } }, 45000);
  }
  const _remoteDesktopResolved=new Map();
  async function _remoteDesktopAddress(peer){
    const raw=String(peer||'').trim(), direct=safePk(raw);
    if(direct)return {pk:direct,relays:_remoteDesktopResolved.get(direct)||[]};
    let name='',host=raw;
    const ai=raw.lastIndexOf('@');if(ai>0){name=raw.slice(0,ai).trim();host=raw.slice(ai+1).trim();}
    if(!host||/[\s/?#]/.test(host)||host.includes('\\')||host.includes('..'))
      throw new Error('enter an npub, IP address, host name, or name@address');
    const explicit=/^https?:\/\//i.test(host);let schemeHint='';
    if(explicit){try{const u=new URL(host);schemeHint=u.protocol.slice(0,-1);host=u.host;}catch(_){throw new Error('invalid PosterChan address');}}
    const schemes=explicit?[schemeHint]:
      ((BUNDLED||!window.isSecureContext)?['https','http']:['https']);
    /* A BARE LAN IP usually has no reverse proxy. PosterChan's direct client service listens on
     * 3051, so only trying 443/80 made the UI's documented `192.168…` form work by accident on
     * hosts that happened to run nginx. Identity is still the returned, verified npub; the IP is
     * discovery/routing information and is never trusted as a person. Browsers on HTTPS cannot
     * fetch cleartext LAN endpoints (mixed-content policy), while bundled desktop/Android can. */
    const endpoints=[];
    for(const scheme of schemes){
      endpoints.push(scheme+'://'+host);
      if(!explicit && scheme==='http' && !/:\d+$/.test(host)) endpoints.push('http://'+host+':3051');
    }
    let doc=null,last='';
    for(const base of endpoints){
      const ac=new AbortController(),tm=setTimeout(()=>ac.abort(),5000);
      try{const q=name?'?name='+encodeURIComponent(name):'';
        const r=await fetch(base+'/.well-known/nostr.json'+q,
          {signal:ac.signal,cache:'no-store'});if(r.ok){doc=await r.json();break;}last='HTTP '+r.status;
      }catch(e){last=String(e&&e.message||e);}finally{clearTimeout(tm);}
    }
    const names=Object.entries((doc&&doc.names)||{}).map(([n,p])=>({name:n,pk:safePk(String(p||''))})).filter(x=>x.pk);
    if(name){const hit=names.find(x=>x.name.toLowerCase()===name.toLowerCase());if(!hit)throw new Error('that user is not advertised by '+host);names.splice(0,names.length,hit);}
    if(!names.length)throw new Error('no PosterChan remote-desktop identity found at '+host+(last?' ('+last+')':''));
    if(names.length>1){const e=new Error('choose a user on '+host);e.remoteChoices=names.map(x=>{const relays=((doc.relays&&doc.relays[x.pk])||[]).map(normalizeRelay).filter(Boolean);_remoteDesktopResolved.set(x.pk,relays);return {label:x.name+'@'+host,value:x.pk};});throw e;}
    const pk=names[0].pk, relays=((doc.relays&&doc.relays[pk])||[]).map(normalizeRelay).filter(Boolean);
    return {pk,relays};
  }
  async function startRemoteDesktop(peer){
    if(!_remoteDesktopArmed)throw new Error('open Remote Desktop before starting a session');
    const target=await _remoteDesktopAddress(peer);
    await startCall(target.pk,{remoteDesktop:true,signalRelays:target.relays});
    return !!_call;
  }
  async function _acceptCall(){
    if(!_call || _call.state!=='ringing') return;
    _call.state='connecting'; _ringtone(false); clearTimeout(_call.timeout); _callUI();   // sync guard: a 2nd Accept tap now fails the state check
    // Tell my OTHER devices to stop ringing. Every device signed in as me received the invite (it is
    // p-tagged to my key) and each rings on its own 60s timer; the answer goes to the CALLER, so the
    // others never learn the call was picked up and keep ringing at an empty room — and now that a
    // closed phone gets a push too, that is a lock-screen notification for a call already in progress.
    // Addressed to MYSELF so only my devices see it, and marked `sig` so it never pushes.
    try{ _callSend(S.ME.pubkey, {v:1, callId:_call.id, t:'handled'}); }catch(_){}
    const invite = _call.invite;
    // A Remote Desktop viewer is receive-only. It needs no local MediaStream at all; constructing or
    // requesting one made some Electron/WebView builds enter the camera permission path and reject an
    // otherwise valid screen invitation. The remote offer supplies the receiving transceiver.
    let local=null;
    if(!_call.remoteDesktop){
      try{ local = await _getMedia(_call.video, false, false); }catch(_){ toast('microphone/camera permission needed'); _declineCall(); return; }
    }
    if(!_call){ if(local)local.getTracks().forEach(t=>t.stop()); return; }
    _call.local = local; _callUI();
    const ice = await _fetchIceServers();
    if(!_call){ if(local)local.getTracks().forEach(t=>t.stop()); return; }
    const pc = _newPc(ice.iceServers); _call.pc = pc;
    if(_call.remoteDesktop) pc.ondatachannel=e=>{if(e.channel&&e.channel.label==='posterchan-control')_rdWireControl(e.channel);};
    try{ await pc.setRemoteDescription({type:'offer', sdp:invite.sdp}); }catch(_){ _hangup(false); return; }
    if(local)local.getTracks().forEach(t=> pc.addTrack(t, local));
    _preferH264(pc);   // hardware encode/decode on both ends — see _preferH264
    for(const c of (_call.pendingIce||[])){ try{ await pc.addIceCandidate(c); }catch(_){} }
    _call.pendingIce=[];
    try{ const answer = await pc.createAnswer(); await pc.setLocalDescription(answer);
      await _callSend(_call.peer, {v:1, callId:_call.id, t:'answer', sdp: pc.localDescription.sdp}); }
    catch(_){ _hangup(false); }
  }
  /* Missed calls, recorded locally.
   *
   * kind-25050 is EPHEMERAL — the relay fans it out and stores nothing — so a call you were offline
   * for leaves no trace anywhere: no event to sync, no row, nothing to open the app and find. You
   * simply never learn it happened. (A push may have buzzed a locked phone, but a dismissed
   * notification is gone for good.)
   *
   * Local storage rather than an event: this is a per-DEVICE fact ("this device did not take that
   * call"), it must work with no network, and publishing it would leak who calls you to the relay.
   * Capped hard — a log nobody prunes is a log that eventually breaks the page it lives on. */
  const MISSED_MAX = 30;
  // Calls another of my devices already dealt with. Ephemeral signaling has no retry, so a `handled`
  // that arrives late (or a timer that fires first) would otherwise record a MISSED call for one that
  // was answered — the log lying is worse than the log being empty.
  const _handledIds = new Set();
  function _missedList(){ try{ return JSON.parse(localStorage.getItem('pc_missed_calls')||'[]'); }catch(_){ return []; } }
  function _missedAdd(pk, callId){
    if(!pk || (callId && _handledIds.has(callId))) return;
    try{
      const l=_missedList();
      l.unshift({pk, at: Math.floor(Date.now()/1000)});
      localStorage.setItem('pc_missed_calls', JSON.stringify(l.slice(0, MISSED_MAX)));
      _redrawCallsIfIdle();
    }catch(_){}
  }
  function _missedClear(){ try{ localStorage.removeItem('pc_missed_calls'); _redrawCallsIfIdle(); }catch(_){} }
  /* Redraw the Calls view only when the user is not mid-interaction. renderCalls() replaces innerHTML
   * wholesale, so an unguarded redraw — and a missed call can land at any moment — throws away a
   * half-typed npub, the focus, and any ticked group-call boxes. */
  function _redrawCallsIfIdle(){
    if(S.VIEW!=='calls') return;
    const inp=$('#call-npub'), gp=$('#grp-panel');
    if((inp && (inp.value || document.activeElement===inp)) || (gp && gp.style.display!=='none')) return;
    renderCalls();
  }
  function _declineCall(){ if(_call){
    _callSend(_call.peer, {v:1, callId:_call.id, t:'bye'});
    // Same reason as accepting: declining on one device must silence the others, or the phone in your
    // pocket keeps ringing for a call you already turned down at the desk.
    try{ if(_call.state==='ringing') _callSend(S.ME.pubkey, {v:1, callId:_call.id, t:'handled'}); }catch(_){}
    _callTeardown(); } }
  function _hangup(silent){ if(_call){ if(!silent) _callSend(_call.peer, {v:1, callId:_call.id, t:'bye'}); _callTeardown(); } }
  function _callTeardown(){
    if(!_call) return;
    if(_rdViewerCleanup)_rdViewerCleanup();
    try{ clearTimeout(_call.timeout); }catch(_){}
    try{ clearTimeout(_call.iceFailureTimer); }catch(_){}
    try{ if(_call.signalClose) _call.signalClose(); }catch(_){}
    if(_call.remoteDesktop&&_call.caller)_rdReleaseNative();
    try{ if(_call.control) _call.control.close(); }catch(_){}
    try{ if(_call.pc) _call.pc.close(); }catch(_){}
    try{ if(_call.local) _call.local.getTracks().forEach(t=>t.stop()); }catch(_){}
    _call = null; _callService(false); _callUI();
  }
  function _onCallEvent(ev){
    if(!ev || _callSeen.has(ev.id)) return; _callSeen.add(ev.id);
    if(_callSeen.size>500){ _callSeen.clear(); _callSeen.add(ev.id); }
    (async()=>{
      let msg; try{ msg = JSON.parse(await S.signer.nip44dec(ev.pubkey, ev.content)); }catch(_){ return; }
      const from = ev.pubkey;
      // The relay also fans a self-addressed frame back to its sender. Other devices have a different
      // marker and continue normally; this renderer must never answer or tear down its own offer.
      if(msg&&msg.deviceId&&msg.deviceId===_CALL_DEVICE_ID)return;
      if(msg && msg.room){ try{ _onRoomEvent(from, msg); }catch(_){} return; }   // group-call (mesh) signaling
      // Another of MY devices answered (or declined) this call — stop ringing here. Only ever sent
      // by me to me, so a stranger cannot silence someone else's phone with it.
      if(msg.t==='handled'){
        if(from!==S.ME.pubkey) return;
        if(msg.callId){ _handledIds.add(msg.callId); if(_handledIds.size>200) _handledIds.clear(); }
        if(_call && _call.id===msg.callId && _call.state==='ringing'){
          _ringtone(false); _callTeardown(); toast('answered on another device');
        }
        return;
      }
      if(msg.t==='invite'){
        if(isMutedAuthor(from)) return;   // a muted/blocked pubkey can't ring you
        // Compatibility with an older PosterChan sender that produced a screen/video offer but lost
        // the remoteDesktop field. This inference is deliberately narrow: only another device signed
        // with MY key, while this device's Remote Desktop surface is explicitly open, can qualify.
        // It must never turn a stranger's video call into an auto-accepted desktop session.
        const remoteDesktopInvite=!!msg.remoteDesktop ||
          !!(_remoteDesktopArmed && from===S.ME.pubkey && msg.video && msg.sdp && /m=video\s/.test(msg.sdp) && !/m=audio\s/.test(msg.sdp));
        /* Voice/video calls remain available globally, but a desktop session is deliberately
         * opt-in on BOTH ends. Merely being signed in must not make this device a remote endpoint. */
        if(remoteDesktopInvite&&!_remoteDesktopArmed)return;
        // Don't ring for an invite that is already over. The subscription's `since` cannot be relied on
        // to bound this: relay.js re-REQs the filters VERBATIM on re-arm, so `since` is frozen at
        // subscribe time and the window silently widens with uptime (an hour up = an hour of history
        // requested). Our relay never stores an ephemeral kind so nothing comes back today, but a
        // third-party relay that replays would ring the phone for a call that ended long ago — and
        // _callSeen is in-memory, so a page load forgets every id it had already discarded.
        if(Math.abs(Math.floor(Date.now()/1000) - (ev.created_at||0)) > 60) return;   // caller gave up at 45s
        if(_call && _call.caller && _call.peer===from && _call.state==='calling'){
          // GLARE — we're both calling each other. Deterministic tiebreak by pubkey so it doesn't fail:
          // the LOWER pubkey keeps its outgoing offer (ignore the incoming); the HIGHER drops its outgoing
          // and rings/answers theirs. Both sides converge on the lower-pubkey's single call.
          if(S.ME.pubkey < from) return;
          try{ clearTimeout(_call.timeout); if(_call.pc) _call.pc.close(); if(_call.local) _call.local.getTracks().forEach(t=>t.stop()); }catch(_){}
          _call = null;   // yield → fall through and ring their invite
        }
        if(_call){ _callSend(from, {v:1, callId:msg.callId, t:'bye'}); return; }   // busy with someone else → auto-decline
        _call = { id:msg.callId, peer:from, pc:null, local:null, remote:null, video:!!msg.video,
                  remoteDesktop:remoteDesktopInvite, state:'ringing', caller:false, invite:msg,
                  pendingIce:[] };
        // Stop ringing if the caller vanishes without a 'bye' (crash/offline) — ephemeral events can be lost.
        _call.timeout = setTimeout(()=>{ if(_call && _call.state==='ringing'){ _ringtone(false); _missedAdd(_call.peer, _call.id); _callTeardown(); } }, 60000);
        try{ needProfile(from); }catch(_){}
        /* Same account, different device: opening Remote Desktop on both ends is the explicit consent.
         * Auto-accept only that narrow case. Voice/video still rings, another identity still asks, and
         * a closed Remote Desktop app was rejected by the armed gate above. */
        if(remoteDesktopInvite&&from===S.ME.pubkey){_acceptCall().catch(()=>{});return;}
        _ringtone(true); _callUI();
        try{ if('Notification' in window && Notification.permission==='granted'){ const p=profOf(from)||{}; new Notification('📞 '+(p.name||'Incoming call'), {body:'tap to answer', tag:'pc-call'}); } }catch(_){}
        return;
      }
      if(!_call || from!==_call.peer || msg.callId!==_call.id) return;
      if(msg.t==='answer'){ if(_call.pc){ try{
          await _call.pc.setRemoteDescription({type:'answer', sdp:msg.sdp});
          for(const c of (_call.pendingIce||[])){ try{ await _call.pc.addIceCandidate(c); }catch(_){} } _call.pendingIce=[];   // drain ICE that beat the answer (relays don't order events)
          clearTimeout(_call.timeout); if(_call.state==='calling'){ _call.state='connecting'; _callUI(); }                       // answered → the 45s no-answer timer must not abort ICE negotiation
        }catch(_){} } }
      else if(msg.t==='ice'){ const c=msg.cand; if(_call.pc && _call.pc.remoteDescription){ try{ await _call.pc.addIceCandidate(c); }catch(_){} } else { (_call.pendingIce||(_call.pendingIce=[])).push(c); } }
      else if(msg.t==='reoffer'){ if(!_call.pc) return;   // mid-call renegotiation (video added/dropped)
        const polite = S.ME.pubkey > from;
        const collision = _call.makingOffer || _call.pc.signalingState!=='stable';
        if(collision && !polite) return;   // impolite peer keeps its own offer; the polite one rolls back
        try{
          if(collision){ try{ await _call.pc.setLocalDescription({type:'rollback'}); }catch(_){} }
          await _call.pc.setRemoteDescription({type:'offer', sdp:msg.sdp});
          _preferH264(_call.pc);   // their video arrived mid-call; answer with hardware codecs too
          const ans=await _call.pc.createAnswer(); await _call.pc.setLocalDescription(ans);
          await _callSend(_call.peer,{v:1,callId:_call.id,t:'reanswer',sdp:_call.pc.localDescription.sdp});
          _callUI();
        }catch(_){}
      }
      else if(msg.t==='reanswer'){ if(_call.pc){ try{ await _call.pc.setRemoteDescription({type:'answer', sdp:msg.sdp}); _callUI(); }catch(_){} } }
      else if(msg.t==='bye'){ if(_call.state==='ringing') _missedAdd(from, _call.id);   // they gave up before we picked up
        if(_call.state!=='connected') toast('call ended'); _callTeardown(); }
    })();
  }
  function startCallSignaling(){
    if(_callSub || !S.ME || !window.RTCPeerConnection) return;
    // since = the caller's 45s ring window, not 5s — and the reason is CLOCK SKEW, not catch-up. 25050 is
    // ephemeral (the relay fans it out and never stores it), so this filter only ever sees LIVE frames and
    // a socket that was down when the invite went out has missed it for good — that gap is what the
    // server-side push exists for. But `since` is still applied to those live frames, so a caller whose
    // clock ran >5s ahead had perfectly good invites dropped here. _callSeen dedups; a 45s-old call is over.
    try{ _callSub = Relay.subscribe([{ '#p':[S.ME.pubkey], kinds:[CALL_KIND], since: Math.floor(Date.now()/1000)-45 }], { onEvent:_onCallEvent, live:true }); }catch(_){}
  }
  function _ringtone(on){
    try{
      if(on){
        if(!_ringOsc){ const AC=window.AudioContext||window.webkitAudioContext; if(!AC) return; const ctx=new AC();
          const beep=()=>{ try{ const o=ctx.createOscillator(), g=ctx.createGain(); o.frequency.value=480; o.connect(g); g.connect(ctx.destination);
            g.gain.setValueAtTime(0.0001,ctx.currentTime); g.gain.exponentialRampToValueAtTime(0.14,ctx.currentTime+0.05); g.gain.exponentialRampToValueAtTime(0.0001,ctx.currentTime+0.5);
            o.start(); o.stop(ctx.currentTime+0.55); }catch(_){} };
          beep(); _ringOsc={ctx, iv:setInterval(beep,1400)}; }
        try{ navigator.vibrate && navigator.vibrate([400,200,400]); }catch(_){}
      } else { if(_ringOsc){ clearInterval(_ringOsc.iv); try{ _ringOsc.ctx.close(); }catch(_){} _ringOsc=null; } try{ navigator.vibrate && navigator.vibrate(0); }catch(_){} }
    }catch(_){}
  }
  function _callStatus(){ if(!_call) return '';
    const base={calling:'calling…', ringing:_call.caller?'ringing…':'📞 incoming call', connecting:'connecting…', connected:'connected'}[_call.state]||_call.state;
    if(_call.state==='connecting' && _call.pc){ const ic=_call.pc.iceConnectionState;
      if(ic==='checking'||ic==='new') return 'connecting… (finding a path)';
      if(ic==='failed') return 'connection failed'; if(ic==='disconnected') return 'reconnecting…'; }
    return base;
  }
  // ---- draggable self-view -------------------------------------------------------------------------
  // The little "me" rectangle is parked bottom-right, which is exactly where the other person's face
  // tends to be on a phone held in portrait — and where our own Hang-up row sits. Let it be moved.
  //
  // Position is stored as a FRACTION of the free space rather than pixels. A saved pixel offset from a
  // landscape session lands off-screen in portrait, and a phone and a desktop share this setting; a
  // fraction re-resolves correctly against whatever the window is now, which is also what makes the
  // resize/rotate handler a one-liner.
  //
  // It moves with left/top and NEVER with transform: .call-local carries transform:scaleX(-1) to mirror
  // the preview (so it reads like a mirror, the way every video app does it), and positioning by
  // transform would either fight that or un-mirror the picture.
  const _SELF_POS_KEY='callSelfPos';
  const _SELF_M=8;                                     // keep it clear of the very edge
  let _selfResizeWired=false;
  function _selfPos(){
    try{ const v=ClientSettings.get(_SELF_POS_KEY,null);
      return (v && typeof v.fx==='number' && typeof v.fy==='number') ? v : null; }catch(_){ return null; }
  }
  function _placeSelfView(el){
    const pos=_selfPos(); if(!el || !pos) return;
    // _callUI() re-runs on EVERY call event — an ICE state change, a mute, the remote track arriving —
    // and calls this. Re-placing mid-drag would yank the rectangle out from under the finger and back to
    // its last saved spot, which on a connecting call is several times in the first few seconds.
    if(el.classList.contains('dragging')) return;
    const w=el.offsetWidth, h=el.offsetHeight;
    if(!w || !h) return;                               // hidden / not laid out yet — nothing to clamp against
    const maxL=Math.max(_SELF_M, window.innerWidth-w-_SELF_M);
    const maxT=Math.max(_SELF_M, window.innerHeight-h-_SELF_M);
    el.style.left=(_SELF_M+pos.fx*(maxL-_SELF_M))+'px';
    el.style.top =(_SELF_M+pos.fy*(maxT-_SELF_M))+'px';
    el.style.right='auto'; el.style.bottom='auto';
  }
  function _dragSelfView(el){
    if(!el || el.dataset.drag) return; el.dataset.drag='1';
    let id=null, dx=0, dy=0, moved=false;
    el.addEventListener('pointerdown', e=>{
      if(el.closest('.call-mini')) return;              // minimized: the whole overlay is one tap target
      const r=el.getBoundingClientRect();
      id=e.pointerId; dx=e.clientX-r.left; dy=e.clientY-r.top; moved=false;
      // Switch from the CSS right/bottom anchoring to left/top at the CURRENT position, so the first
      // drag doesn't teleport the rectangle before it starts following the finger.
      el.style.left=r.left+'px'; el.style.top=r.top+'px';
      el.style.right='auto'; el.style.bottom='auto';
      el.classList.add('dragging');
      try{ el.setPointerCapture(id); }catch(_){ }
      e.preventDefault();
    });
    el.addEventListener('pointermove', e=>{
      if(id===null || e.pointerId!==id) return;
      const w=el.offsetWidth, h=el.offsetHeight;
      const left=Math.min(window.innerWidth-w-_SELF_M,  Math.max(_SELF_M, e.clientX-dx));
      const top =Math.min(window.innerHeight-h-_SELF_M, Math.max(_SELF_M, e.clientY-dy));
      if(Math.abs(left-(parseFloat(el.style.left)||0))>2 || Math.abs(top-(parseFloat(el.style.top)||0))>2) moved=true;
      el.style.left=left+'px'; el.style.top=top+'px';
    });
    const end=e=>{
      if(id===null || (e && e.pointerId!==id)) return;
      try{ el.releasePointerCapture(id); }catch(_){ }
      id=null; el.classList.remove('dragging');
      if(!moved) return;
      const w=el.offsetWidth, h=el.offsetHeight;
      const maxL=Math.max(_SELF_M, window.innerWidth-w-_SELF_M);
      const maxT=Math.max(_SELF_M, window.innerHeight-h-_SELF_M);
      const clamp01=n=>Math.min(1, Math.max(0, n));
      try{ ClientSettings.set(_SELF_POS_KEY, {
        fx: clamp01(maxL>_SELF_M ? ((parseFloat(el.style.left)||0)-_SELF_M)/(maxL-_SELF_M) : 1),
        fy: clamp01(maxT>_SELF_M ? ((parseFloat(el.style.top) ||0)-_SELF_M)/(maxT-_SELF_M) : 1) }); }catch(_){ }
    };
    el.addEventListener('pointerup', end);
    el.addEventListener('pointercancel', end);
    // A drag that ENDS on the video still fires a click, and the overlay's click handler restores a
    // minimized call — so a drag would otherwise double as a tap on whatever is underneath.
    el.addEventListener('click', e=>{ if(moved){ e.stopPropagation(); moved=false; } });
    if(!_selfResizeWired){
      _selfResizeWired=true;
      const re=()=>{ const lv=document.getElementById('call-local'); if(lv) _placeSelfView(lv); };
      window.addEventListener('resize', re);
      window.addEventListener('orientationchange', re);
    }
  }
  /* THE NATIVE IN-CALL SERVICE (the APK only).
   *
   * Since Android 11 an app in the BACKGROUND may not capture the microphone or camera at all unless
   * a foreground service of the matching type is running. Without one, pressing Home mid-call
   * silences your mic instantly: the other party hears nothing, this UI looks perfect, and nothing is
   * logged anywhere. The service also keeps the process off the cached-process freezer, which is what
   * lets the PeerConnection survive a locked screen. See CallService.java.
   *
   * CHEAP BY CONSTRUCTION, because this is called from _callUI/_roomUI — i.e. on every repaint, every
   * ICE state change, every mute. The signature guard turns all but the genuine TRANSITIONS into a
   * string compare, so a call costs about four Intents in total rather than one per frame. Putting it
   * on the repaint path rather than on hand-picked transitions is deliberate: a missed start is a
   * dead microphone, and there are six places a call's state can move.
   *
   * The service holds NO wake lock. The foreground status is what stops the process being frozen;
   * a PARTIAL_WAKE_LOCK on top would additionally stop the CPU idling between audio packets, which
   * is battery spent to change nothing. */
  let _callSvcSig = '', _callSvcWarned = false;
  function _callService(on, opts){
    const P = _capPlugin('CallControls');
    if(!P) return;                                   // a browser or the desktop app: nothing to do
    if(!on){
      if(!_callSvcSig) return;                       // never started: no Intent to stop it with
      _callSvcSig = '';
      try{ P.stop(); }catch(_){}
      return;
    }
    const o = opts || {};
    const sig = (o.video?'1':'0') + '|' + (o.state||'') + '|' + (o.name||'');
    if(sig === _callSvcSig) return;
    _callSvcSig = sig;
    try{
      const r = P.start(o);
      /* A REFUSED start is the exact failure this exists to prevent, and it is invisible otherwise —
       * so say it once, in the words that describe the consequence rather than the API.
       *
       * ONCE PER SESSION, not once per call. An APK built before this service existed answers every
       * start with "no implementation" (Capacitor's registerPlugin hands back a proxy either way, so
       * there is no way to ask first), and that is a real warning for exactly one reading — after
       * which it is just noise on top of a working call. */
      if(r && r.catch) r.catch(()=>{
        if(_callSvcWarned) return;
        _callSvcWarned = true;
        toast('heads up: leaving the app may mute this call — update the app to fix it');
      });
    }catch(_){}
  }
  function _callSvcName(hex){
    try{ const p = profOf(hex)||{}; return p.name || p.display_name || 'Call'; }catch(_){ return 'Call'; }
  }

  function _rdPlayRemote(video, enableSound=false){
    const session=_call;
    if(!session||!video)return;
    if(enableSound){video.muted=false;session.playbackBlocked=false;}
    const current=()=>_call===session&&video.isConnected&&video.srcObject===session.remote;
    try{
      Promise.resolve(video.play()).then(()=>{if(current())_callUI();}).catch(()=>{
        if(!current())return;
        // Phone autoplay may reject audible video. Keep the picture moving, and offer a
        // user-gesture sound button instead of swallowing the failure and freezing the picture.
        session.playbackBlocked=true;video.muted=true;
        try{Promise.resolve(video.play()).catch(()=>{});}catch(_){}
        _callUI();
      });
    }catch(_){if(current()){session.playbackBlocked=true;_callUI();}}
  }
  function _callUI(){
    let el=document.getElementById('call-overlay');
    if(_call&&!_call.controlGranted){const v=document.getElementById('call-remote');if(v&&v.rdUnlock)v.rdUnlock();}
    if(!_call){ if(el) el.remove(); _ringtone(false); _callWake(false); _callService(false); return; }
    _callWake(true);
    _callService(true, { video:!!_call.video, state:_call.state||'', name:_callSvcName(_call.peer) });
    if(_call.state!=='ringing') _ringtone(false);
    if(!el){
      el=document.createElement('div'); el.id='call-overlay';
      el.innerHTML=`<div class="rd-stage"><video id="call-remote" class="call-remote" autoplay playsinline></video>
          <div class="rd-scroll rd-scroll-v" id="rd-scroll-v" hidden><i></i></div>
          <div class="rd-scroll rd-scroll-h" id="rd-scroll-h" hidden><i></i></div></div>
        <div class="call-head"><img id="call-av" onerror="this.src='${S.LOGO}'"><div><div class="call-name" id="call-name"></div><div class="call-status" id="call-status"></div></div>
          <div class="rd-zoom" id="rd-zoom"><button type="button" id="rd-zoom-fit" title="Fit the whole screen (Ctrl+0)">Fit</button><button type="button" id="rd-zoom-actual" title="Actual size, remote pixels 1:1 (Ctrl+1)">1:1</button><button type="button" id="rd-zoom-out" title="Zoom out (Ctrl+-)" aria-label="Zoom out">&minus;</button><input type="range" id="rd-zoom-range" class="rd-zoom-range" min="0" max="1000" value="0" aria-label="Zoom"><button type="button" id="rd-zoom-in" title="Zoom in (Ctrl++)" aria-label="Zoom in">+</button><button type="button" id="rd-zoom-level" title="Click to fit the whole screen (Ctrl+0)">100%</button><button type="button" id="rd-zoom-follow" class="rd-follow" title="Follow the remote pointer" aria-label="Follow the remote pointer" aria-pressed="false">&#9678;</button></div></div>
        <video id="call-local" class="call-local" autoplay playsinline muted></video>
        <div class="call-actions" id="call-actions"></div>`;
      const callHost=_call.remoteDesktop?(_rdEnsureHost()||document.body):document.body;
      callHost.appendChild(el);
      // Tap the minimized thumbnail (anywhere but a button) to restore it. The call lives in the
      // PeerConnection, not the DOM, so minimizing/restoring never touches the media.
      el.onclick=e=>{ if(el.classList.contains('call-mini') && !e.target.closest('button')) _setOverlayMini('call-overlay', false); };
    }
    const p=profOf(_call.peer)||{};
    const av=document.getElementById('call-av'); if(av) av.src=(p.picture||S.LOGO);
    const nm=document.getElementById('call-name'); if(nm) nm.textContent=(p.name||p.display_name||'anon');
    const stx=document.getElementById('call-status'); if(stx) stx.textContent=_call.remoteDesktop
      ? (_call.state==='ringing' ? 'wants to share a desktop' : 'remote desktop · '+_callStatus()+(!_call.caller&&_call.controlGranted?' · Tap or drag to control · Esc releases mouse':'')+(_call.caller?(_call.local?.getTracks().some(t=>t.kind==='audio'&&t.readyState==='live')?' · Sharing sound':' · No shared sound from this source'):''))
      : _callStatus();
    const hasLocalVid=_hasLiveVideo(_call.local), hasRemoteVid=_hasLiveVideo(_call.remote);
    const showVid=hasLocalVid||hasRemoteVid;
    // Preserve the minimized state: _callUI re-runs on every call event (ICE, mute, remote video), and a
    // bare reassignment would drop `call-mini` and pop the overlay back to fullscreen on its own.
    const _mini=el.classList.contains('call-mini');
    el.className='call-overlay'+(_call.remoteDesktop?' rd':'')+(_call.remoteDesktop&&_call.controlGranted?' control-on':'')+(showVid?' vid':' aud')+(_call.state==='ringing'?' ring':'')+(_call.state==='connected'?' on':'')+(_mini?' call-mini':'');
    /* The zoom row belongs to a remote-desktop session and to no other call. Bound on every UI
     * pass (the overlay is rebuilt on any call event) and hidden outright for a camera call, where
     * "Fit" would be a control over somebody's face. */
    const zoomRow=document.getElementById('rd-zoom');
    if(zoomRow){
      const wanted=!!(_call&&_call.remoteDesktop&&!_call.caller)&&!el.classList.contains('call-mini');
      zoomRow.hidden=!wanted;
      if(wanted&&!zoomRow.dataset.bound){
        zoomRow.dataset.bound='1';
        const bind=(id,fn)=>{const b=document.getElementById(id);if(b)b.onclick=(ev)=>{ev.stopPropagation();fn();};};
        bind('rd-zoom-fit',()=>_rdZoomSetMode('fit'));
        bind('rd-zoom-actual',()=>_rdZoomSetMode('actual'));
        bind('rd-zoom-in',()=>_rdZoomBy(RD_ZOOM_KEY));
        bind('rd-zoom-out',()=>_rdZoomBy(1/RD_ZOOM_KEY));
        /* The readout is the way BACK: one press returns to the whole screen, which is the state
         * somebody zoomed in from and the only one that needs no aiming. */
        bind('rd-zoom-level',()=>_rdZoomSetMode('fit'));
        /* Following is OFF by default and says so. It is the control that made a magnified session
         * feel out of control, so it is a thing somebody turns on, never a thing that happens. */
        bind('rd-zoom-follow',()=>{const z=_rdZoomState();if(!z)return;z.follow=!z.follow;_rdApplyZoom();_rdZoomRemember();});
        const range=document.getElementById('rd-zoom-range');
        if(range)range.oninput=(ev)=>{
          ev.stopPropagation();
          const m=_rdZoomMetrics();if(!m)return;
          _rdZoomToScale(_rdZoomFromSlider(Number(range.value)/1000,m));
        };
      }
      if(wanted)_rdApplyZoom();
    }
    const rv=document.getElementById('call-remote'); if(rv){ rv.style.display=hasRemoteVid?'':'none'; if(_call.remote && rv.srcObject!==_call.remote){ rv.srcObject=_call.remote; if(_call.remoteDesktop&&!_call.caller)_rdPlayRemote(rv);else rv.play&&rv.play().catch(()=>{}); } if(_call.remoteDesktop&&!_call.caller)_rdBindViewer(rv); }
    const lv=document.getElementById('call-local'); if(lv){ lv.style.display=(hasLocalVid&&!_call.camOff)?'':'none'; if(_call.local && lv.srcObject!==_call.local){ lv.srcObject=_call.local; lv.play&&lv.play().catch(()=>{}); }
      // Wire + restore AFTER display is set: offsetWidth is 0 while hidden, so placing it any earlier
      // has nothing to clamp against and would silently leave it in the default corner.
      _dragSelfView(lv); _placeSelfView(lv); }
    const acts=document.getElementById('call-actions');
    const act=(id,ic,label,cls)=>`<div class="call-act"><button class="call-btn ${cls||''}" id="${id}">${ic}</button><span>${label}</span></div>`;
    const sendingVid=hasLocalVid&&!_call.camOff;
    // Show the video button once media is up (i.e. not while merely ringing/calling with no PC yet).
    const canVid=!!(_call.local && _call.pc);
    const html = (_call.state==='ringing' && !_call.caller)
      ? act('call-accept',_call.remoteDesktop?'🖥':'📞',_call.remoteDesktop?'View':'Answer','accept')+act('call-decline','✕','Decline','decline')
      : act('call-min','▁','Minimize')
        +(_call.remoteDesktop&&!_call.caller?act('call-full','⛶','Fullscreen'):'')
        +(_call.remoteDesktop&&!_call.caller&&_call.remote?.getAudioTracks().length?act('call-sound','🔊',_call.playbackBlocked||rv?.muted?'Enable sound':'Mute sound'):'')
        +(_call.remoteDesktop&&_call.caller&&_call.state==='connected'?act('call-screen','🖥','Switch screen'):'')
        +(_call.remoteDesktop&&!_call.caller&&_call.state==='connected'&&_call.control
          ? act('call-control',_call.controlGranted?'🖱️':'☝️',_call.controlGranted?'Stop control':'Request control',_call.controlGranted?'hang':''):'')
        +(_call.remoteDesktop&&_call.caller&&_call.controlRequested
          ? act('call-control-allow','✓','Allow control','accept')+act('call-control-deny','✕','Deny control','hang'):'')
        +(_call.remoteDesktop&&_call.caller&&_call.controlGranted&&!_call.controlRequested
          ? act('call-control-stop','🖱️','Stop control','hang'):'')
        +(_call.remoteDesktop?'':act('call-mute', _call.muted?'🔇':'🎙️', _call.muted?'Unmute':'Mute'))
        +(!_call.remoteDesktop&&canVid?act('call-cam', sendingVid?'🚫':'📷', sendingVid?'Stop video':'Start video'):'')
        +act('call-hang','📵',_call.remoteDesktop&&_call.caller?'Stop sharing':'End','hang');
    if(acts && acts.dataset.k!==html){ acts.dataset.k=html; acts.innerHTML=html;
      const b=id=>document.getElementById(id);
      if(b('call-accept')) b('call-accept').onclick=_acceptCall;
      if(b('call-decline')) b('call-decline').onclick=_declineCall;
      if(b('call-hang')) b('call-hang').onclick=()=>_hangup(false);
      if(b('call-min')) b('call-min').onclick=e=>{ e.stopPropagation(); _setOverlayMini('call-overlay', true); };
      if(b('call-full')) b('call-full').onclick=async()=>{ try{
        if(document.fullscreenElement) await document.exitFullscreen();
        else await el.requestFullscreen();
      }catch(_){ toast('fullscreen is unavailable in this window'); } };
      if(b('call-sound'))b('call-sound').onclick=()=>{if(!rv)return;if(rv.muted||_call?.playbackBlocked)_rdPlayRemote(rv,true);else{rv.muted=true;_callUI();}};
      if(b('call-screen'))b('call-screen').onclick=_rdSwitchScreen;
      if(b('call-control')) b('call-control').onclick=()=>{if(!_call)return;if(_call.controlGranted){_call.controlGranted=false;_rdSend({t:'release'});_callUI();}else _rdSend({t:'request'});};
      if(b('call-control-allow')) b('call-control-allow').onclick=()=>_rdGrant(true);
      if(b('call-control-deny')) b('call-control-deny').onclick=()=>_rdGrant(false);
      if(b('call-control-stop')) b('call-control-stop').onclick=()=>_rdGrant(false);
      if(b('call-mute')) b('call-mute').onclick=()=>{ if(_call&&_call.local){ _call.muted=!_call.muted; _call.local.getAudioTracks().forEach(t=>t.enabled=!_call.muted); _callUI(); } };
      if(b('call-cam')) b('call-cam').onclick=()=>_toggleVideo();
    }
  }
  // Shrink a call/room overlay to a floating thumbnail so the app stays usable during a call. Purely visual:
  // the media rides the PeerConnection, not the DOM, so this never interrupts the call. Shared by 1:1 + group.
  function _setOverlayMini(id, on){
    const el=document.getElementById(id); if(!el) return;
    if(on){const v=el.querySelector('#call-remote');if(v&&v.rdUnlock)v.rdUnlock();}
    el.classList.toggle('call-mini', !!on);
    if(on) toast('call minimized — tap it to return');
  }
  function renderCalls(){
    const feed=$('#feed'); if(!feed) return;
    if(S.GUEST){ feed.innerHTML='<div class="empty">Log in to make calls.</div>'; return; }
    /* FRIENDS, not everyone you follow.
     *
     * This listed [...FOLLOWS].slice(0,60) — an arbitrary 60 of a list that is routinely hundreds of
     * accounts, most of them people you read rather than people you would ring. A call only makes
     * sense with someone who knows you, so the list is MUTUALS: you follow them and they follow you
     * back. That is a far shorter list, so the 60-cap stops truncating anything real, and every row
     * is a profile lookup + an avatar request that no longer has to happen for a stranger.
     *
     * FOLLOWERS loads lazily (ensureMyFollowers), so until it arrives fall back to plain follows —
     * an empty Calls screen would be a worse answer than a broad one. The re-render below narrows it
     * the moment the data lands. */
    const mutuals=[...S.FOLLOWS].filter(pk=>FOLLOWERS.has(pk));
    const contacts=(mutuals.length?mutuals:[...S.FOLLOWS]).slice(0,60);
    const _narrowed=mutuals.length>0;
    feed.innerHTML=`<div class="calls-view">
      <h2 style="margin:0 0 4px"><svg class="ic h-ic" aria-hidden="true"><use href="#i-phone"></use></svg>Calls</h2>
      <p class="muted small">Voice &amp; video over Nostr — peer-to-peer, works across instances. Audio-first; toggle video in-call.</p>
      <p class="muted small">${_narrowed?'Showing friends — people you follow who follow you back. Anyone else: paste their npub or name below.':'Showing who you follow.'}</p>
      <div class="call-start">
        <input id="call-npub" class="input" placeholder="type a name, npub1…, or name@domain" autocapitalize="none" autocorrect="off" spellcheck="false">
        <div id="call-ac" class="mention-box hidden"></div>
        <button class="btn btn-neon full" id="call-start-btn"><svg class="ic b-ic" aria-hidden="true"><use href="#i-phone"></use></svg>Call</button>
        <button class="btn full" id="grp-toggle" style="margin-top:8px"><svg class="ic b-ic" aria-hidden="true"><use href="#i-users"></use></svg>Start a group call</button>
      </div>
      <div id="grp-panel" class="call-group" style="display:none">
        <p class="muted small">Pick up to 6 people, then start — everyone connects peer-to-peer (mesh).</p>
        <div class="grp-list">${contacts.map(pk=>{ const p=profOf(pk)||{}; return `<label class="grp-contact"><input type="checkbox" class="grp-pick" value="${pk}"><img src="${enc(p.picture||S.LOGO)}" onerror="this.src='${S.LOGO}'"><span>${enc(p.name||p.display_name||'anon')}</span></label>`; }).join('')}</div>
        <button class="btn btn-neon full" id="grp-start" style="margin-top:8px"><svg class="ic b-ic" aria-hidden="true"><use href="#i-phone"></use></svg>Start group call</button>
      </div>
      ${(()=>{ const m=_missedList(); if(!m.length) return '';
        return `<div class="call-missed"><div class="search-section-title" style="display:flex;align-items:center;gap:8px">
          <svg class="ic b-ic" aria-hidden="true"><use href="#i-phone"></use></svg>Missed
          <button class="btn small" id="missed-clear" style="margin-left:auto">Clear</button></div>` +
          m.map(x=>{ const p=profOf(x.pk)||{};
            return `<button class="call-contact" data-pk="${x.pk}"><img src="${enc(p.picture||S.LOGO)}" onerror="this.src='${S.LOGO}'"><span>${enc(p.name||p.display_name||'anon')}</span><span class="muted small">${enc(timeAgo(x.at))}</span></button>`;
          }).join('') + `</div>`; })()}
      <div class="call-contacts">${contacts.map(pk=>{ const p=profOf(pk)||{}; return `<button class="call-contact" data-pk="${pk}"><img src="${enc(p.picture||S.LOGO)}" onerror="this.src='${S.LOGO}'"><span>${enc(p.name||p.display_name||'anon')}</span></button>`; }).join('')}</div>
    </div>`;
    // Followers arrive lazily; when they do, redraw ONCE so the list settles on friends. Guarded on
    // _narrowed and on still being here, so it cannot loop or stomp a view the user has moved on from.
    // …but never mid-interaction. ensureMyFollowers awaits a live socket and a 1000-event query, so it
    // can land seconds later — straight into innerHTML, discarding a half-typed npub, the focus, and any
    // ticked group-call boxes. Redraw only when the form is untouched.
    if(!_narrowed) ensureMyFollowers().then(()=>{
      if([...S.FOLLOWS].some(pk=>FOLLOWERS.has(pk))) _redrawCallsIfIdle();
    }).catch(()=>{});
    const go=async(pk)=>{ if(pk) startCall(pk, {video:false}); };   // audio-first; add video mid-call with the in-call button
    // Autocomplete: as you type a name, suggest known profiles (like the DM composer); click one to call.
    { const inp=$('#call-npub',feed), ac=$('#call-ac',feed);
      if(inp && ac){ inp.addEventListener('input', ()=>{ const v=inp.value.trim();
        if(safePk(v)){ ac.classList.add('hidden'); return; }   // a full npub/hex needs no suggestions
        const q=v.replace(/^@/,'').toLowerCase(); if(q.length<2){ ac.classList.add('hidden'); return; }
        const matches=Store.profileList().filter(p=>p.pubkey!==S.ME.pubkey && (((p.meta.name||'')+(p.meta.display_name||'')+(p.meta.nip05||'')).toLowerCase().includes(q))).slice(0,6);
        if(!matches.length){ ac.classList.add('hidden'); return; }
        ac.classList.remove('hidden');
        ac.innerHTML=matches.map(p=>`<div class="mention-opt" data-pk="${p.pubkey}"><img src="${enc(p.meta.picture||S.LOGO)}" onerror="this.src='${S.LOGO}'"><b>${enc(p.meta.name||p.meta.display_name||'anon')}</b>${p.meta.nip05?`<span class="muted small">${enc(niceNip05(p.meta.nip05)||'')}</span>`:''}</div>`).join('');
        $$('[data-pk]',ac).forEach(el=> el.onmousedown=ev=>{ ev.preventDefault(); ac.classList.add('hidden'); inp.value=''; go(el.dataset.pk); });
      });
      inp.addEventListener('blur', ()=>setTimeout(()=>ac.classList.add('hidden'), 150)); } }
    $('#call-start-btn').onclick=async()=>{ let v=($('#call-npub').value||'').trim(); if(!v) return; let pk=safePk(v);
      if(!pk){ const q=v.replace(/^@/,'').toLowerCase();   // no npub → try a name match first, then NIP-05
        const m=Store.profileList().find(p=>p.pubkey!==S.ME.pubkey && (p.meta.name||'').toLowerCase()===q) || Store.profileList().find(p=>p.pubkey!==S.ME.pubkey && (((p.meta.name||'')+(p.meta.display_name||'')).toLowerCase().includes(q)));
        if(m) pk=m.pubkey; }
      if(!pk && v.includes('@')){ try{ pk=await nip05Resolve(v.toLowerCase()); }catch(_){} }
      if(!pk){ toast('could not find that person'); return; } go(pk); };
    $$('.call-contact',feed).forEach(b=> b.onclick=()=> go(b.dataset.pk));
    // Sibling of the contact buttons, not nested in one, so stopPropagation is belt-and-braces.
    { const mc=$('#missed-clear',feed); if(mc) mc.onclick=(e)=>{ e.stopPropagation(); _missedClear(); }; }
    const gt=$('#grp-toggle'), gp=$('#grp-panel'); if(gt&&gp) gt.onclick=()=>{ const on=gp.style.display==='none'; gp.style.display=on?'':'none'; gt.textContent=on?'✕ Cancel group call':'👥 Start a group call'; };
    if($('#grp-start')) $('#grp-start').onclick=()=>{ const pks=$$('.grp-pick',feed).filter(c=>c.checked).map(c=>c.value); if(!pks.length){ toast('pick at least one person'); return; } startGroupCall(pks, false); };
    contacts.forEach(pk=>needProfile(pk));
  }

  // ===== Group calls (mesh, ~5 max) — reuses kind-25050 signaling + the TURN relay =====
  // A room has an id; members are invited via p-tagged encrypted 'ginvite'. Each accepter connects P2P to
  // EVERY other (mesh). Deterministic offerer (LOWER pubkey offers) avoids glare; 'rhere' presence tells
  // peers you've joined. Per-peer offer/answer/ice carry the room id. Mutually exclusive with a 1:1 _call.
  let _room = null;   // { id, video, local, peers:Map<hex,{pc,stream,pendingIce}>, members:Set<hex>, ringing, invite, timeout }

  async function _roomSend(peerHex, obj){
    try{ const ct=await S.signer.nip44enc(peerHex, JSON.stringify(obj)); const ev=await sign(CALL_KIND, ct, _callTags(peerHex, obj)); _callPublish(ev); }catch(_){}
  }
  /* EVERY CONNECTION ATTEMPT HAS ITS OWN `sid`, and an answer or a candidate only ever lands on the
   * attempt it was made for. A joiner re-offers to any peer whose connection is still unanswered
   * (`rhere` → _roomOfferTo), because an offer that reached a still-ringing peer was dropped — but the
   * same unanswered state also describes an offer that is merely IN FLIGHT. The late answer to the
   * replaced attempt then arrived at the new one: its ICE credentials named a connection that no longer
   * existed, the pair sat in `new` for ever, and the real answer was refused as out of order. Measured
   * with three real browsers joining a room at once: 11 of 24 runs left two people unable to hear each
   * other (tests/client/test_calls_end_to_end_full_app.py). A frame with no `sid` is an older client's
   * and matches anything, which is exactly the old behaviour. */
  function _sidOk(p, sid){ return !sid || !p.sid || sid===p.sid; }
  function _roomPeer(hex){ let p=_room.peers.get(hex); if(!p){ p={pc:null,sid:'',stream:null,pendingIce:[]}; _room.peers.set(hex,p); } return p; }
  function _roomNewPc(hex, iceServers, sid){
    const pc=new RTCPeerConnection({iceServers:iceServers||[], iceCandidatePoolSize:1});
    pc.onicecandidate=e=>{ if(e.candidate && _room) _roomSend(hex,{v:1,room:_room.id,t:'rice',sid,cand:e.candidate.toJSON()}); };
    pc.ontrack=e=>{ if(!_room) return; const p=_room.peers.get(hex); if(!p||p.pc!==pc) return; p.stream=e.streams[0]; _roomUI(); };
    pc.onconnectionstatechange=()=>{ if(!_room) return; const p=_room.peers.get(hex); if(!p||p.pc!==pc) return;   // a replaced attempt closing is not the peer leaving
      const st=pc.connectionState; if(st==='failed'||st==='closed') _roomDropPeer(hex,false); };
    return pc;
  }
  async function _roomDrainIce(p){
    const keep=[];
    for(const c of p.pendingIce){ if(!_sidOk(p, c.sid)){ keep.push(c); continue; } try{ await p.pc.addIceCandidate(c.cand); }catch(_){} }
    p.pendingIce=keep;
  }
  async function _roomOfferTo(hex){
    if(!_room || !_room.local || hex===S.ME.pubkey) return;
    const p=_roomPeer(hex);
    if(p.pc && p.pc.currentRemoteDescription) return;   // already answered/connected — don't disturb
    const sid=_rid(); p.sid=sid;   // claimed BEFORE the await: a second rhere must not start a third attempt beside this one
    if(p.pc){ try{ p.pc.close(); }catch(_){} p.pc=null; }   // half-open (offer dropped by a still-ringing peer) → redo
    p.pendingIce=[];
    const ice=await _fetchIceServers(); if(!_room || _room.peers.get(hex)!==p || p.sid!==sid) return;
    p.pc=_roomNewPc(hex, ice.iceServers, sid);
    _room.local.getTracks().forEach(t=>p.pc.addTrack(t,_room.local));
    _preferH264(p.pc);   // mesh: one encode per peer, so software fallback hurts N times over
    try{ const o=await p.pc.createOffer(); if(!_room||_room.peers.get(hex)!==p) return; await p.pc.setLocalDescription(o);
      await _roomSend(hex,{v:1,room:_room.id,t:'roffer',sid,video:_room.video,sdp:p.pc.localDescription.sdp}); }
    catch(_){ _roomDropPeer(hex,false); }
  }
  async function _roomOnOffer(hex, msg){
    if(!_room || !_room.local) return;
    const p=_roomPeer(hex);
    // A NEW attempt from the offerer replaces whatever we were answering for it: it has abandoned that one.
    if(p.pc && msg.sid && p.sid && msg.sid!==p.sid){ try{ p.pc.close(); }catch(_){} p.pc=null; p.stream=null; }
    if(msg.sid) p.sid=msg.sid;
    const sid=p.sid;
    const ice=await _fetchIceServers(); if(!_room || _room.peers.get(hex)!==p || p.sid!==sid) return;
    if(!p.pc){ p.pc=_roomNewPc(hex, ice.iceServers, sid); _room.local.getTracks().forEach(t=>p.pc.addTrack(t,_room.local)); _preferH264(p.pc); }
    const pc=p.pc;
    try{ await pc.setRemoteDescription({type:'offer',sdp:msg.sdp});
      if(p.pc!==pc) return;
      await _roomDrainIce(p);
      const a=await pc.createAnswer(); await pc.setLocalDescription(a);
      if(p.pc!==pc) return;
      await _roomSend(hex,{v:1,room:_room.id,t:'ranswer',sid,sdp:pc.localDescription.sdp});
    }catch(_){ _roomDropPeer(hex,false); }
  }
  function _roomDropPeer(hex, notify){ if(!_room) return; const p=_room.peers.get(hex); if(!p) return;
    if(notify) _roomSend(hex,{v:1,room:_room.id,t:'rbye'}); try{ if(p.pc) p.pc.close(); }catch(_){} _room.peers.delete(hex); _roomUI(); }
  function _roomLeave(){ if(!_room) return; const r=_room; _room=null;
    for(const hex of r.peers.keys()) _roomSend(hex,{v:1,room:r.id,t:'rbye'});
    for(const p of r.peers.values()){ try{ p.pc&&p.pc.close(); }catch(_){} }
    try{ r.local&&r.local.getTracks().forEach(t=>t.stop()); }catch(_){} try{ clearTimeout(r.timeout); }catch(_){}
    _callWake(false); _ringtone(false); _callService(false); _roomUI();
  }
  async function startGroupCall(memberHexes, video){
    if(S.GUEST){ _guestPrompt(); return; }
    if(_call || _room){ toast('already in a call'); return; }
    const members=[...new Set(memberHexes)].filter(h=>h && h!==S.ME.pubkey).slice(0,6);
    if(!members.length) return;
    const id=_rid();
    _room={ id, video:!!video, local:null, peers:new Map(), members:new Set([...members, S.ME.pubkey]), ringing:false };
    _roomUI();
    let local; try{ local=await _getMedia(video); }catch(_){ toast('microphone/camera permission needed'); _roomLeave(); return; }
    if(!_room){ local.getTracks().forEach(t=>t.stop()); return; }
    _room.local=local; _callWake(true);
    const memberList=[...members, S.ME.pubkey];
    for(const hex of members) _roomSend(hex,{v:1,room:id,t:'ginvite',video:!!video,members:memberList});
    _roomUI();
  }
  function _roomRing(from, msg){
    if(_call || _room){ _roomSend(from,{v:1,room:msg.room,t:'rbye'}); return; }   // busy
    if(isMutedAuthor(from)) return;
    _room={ id:msg.room, video:!!msg.video, local:null, peers:new Map(), members:new Set([...(msg.members||[]), from, S.ME.pubkey]), ringing:true, invite:{from} };
    _room.timeout=setTimeout(()=>{ if(_room && _room.ringing){ _ringtone(false); _roomLeave(); } }, 60000);
    try{ needProfile(from); }catch(_){}
    _ringtone(true); _roomUI();
    try{ if('Notification' in window && Notification.permission==='granted'){ const p=profOf(from)||{}; new Notification('📞 Group call · '+(p.name||'invite'), {body:'tap to join', tag:'pc-call'}); } }catch(_){}
  }
  async function _roomAccept(){
    if(!_room || !_room.ringing) return;
    _room.ringing=false; _ringtone(false); clearTimeout(_room.timeout); _roomUI();
    let local; try{ local=await _getMedia(_room.video); }catch(_){ toast('microphone/camera permission needed'); _roomDecline(); return; }
    if(!_room){ local.getTracks().forEach(t=>t.stop()); return; }
    _room.local=local; _callWake(true);
    // Announce presence + connect to every member (lower pubkey offers → exactly one offer per pair).
    for(const hex of [..._room.members]){ if(hex===S.ME.pubkey) continue; _roomSend(hex,{v:1,room:_room.id,t:'rhere',members:[..._room.members]}); if(S.ME.pubkey<hex) _roomOfferTo(hex); }
    _roomUI();
  }
  function _roomDecline(){ if(_room){ if(_room.invite) _roomSend(_room.invite.from,{v:1,room:_room.id,t:'rbye'}); _roomLeave(); } }
  function _onRoomEvent(from, msg){
    if(msg.t==='ginvite'){ _roomRing(from, msg); return; }
    if(!_room || msg.room!==_room.id) return;
    if(Array.isArray(msg.members)) msg.members.forEach(h=>{ if(h) _room.members.add(h); });
    if(msg.t==='rhere'){ if(_room.local && from!==S.ME.pubkey && S.ME.pubkey<from) _roomOfferTo(from); return; }   // they joined → we offer if we're lower
    if(msg.t==='roffer'){ _roomOnOffer(from, msg); return; }
    if(msg.t==='ranswer'){ const p=_room.peers.get(from); if(p && p.pc && _sidOk(p, msg.sid) && p.pc.signalingState==='have-local-offer'){ const pc=p.pc;
      pc.setRemoteDescription({type:'answer',sdp:msg.sdp}).then(()=>{ if(p.pc===pc) return _roomDrainIce(p); }).catch(()=>{}); } return; }
    if(msg.t==='rice'){ const p=_roomPeer(from); if(p.pc && p.pc.remoteDescription && _sidOk(p, msg.sid)){ p.pc.addIceCandidate(msg.cand).catch(()=>{}); }
      else { p.pendingIce.push({sid:msg.sid||'', cand:msg.cand}); if(p.pendingIce.length>200) p.pendingIce.splice(0, p.pendingIce.length-200); } return; }   // early, or for an attempt whose offer has not arrived yet: it waits
    if(msg.t==='rbye'){ _roomDropPeer(from, false); return; }
  }
  function _roomUI(){
    let el=document.getElementById('room-overlay');
    if(!_room){ if(el) el.remove(); _callService(false); return; }
    // A group call is the same platform rule and the same service — see _callService. The signature
    // guard means the extra call sites cost a string compare, not an Intent.
    _callService(true, { video:!!_room.video, state:_room.ringing?'ringing':'connected', name:'Group call' });
    if(!el){ el=document.createElement('div'); el.id='room-overlay'; el.className='call-overlay room'; document.body.appendChild(el);
      el.onclick=e=>{ if(el.classList.contains('call-mini') && !e.target.closest('button')) _setOverlayMini('room-overlay', false); }; }
    const ringingIn=_room.ringing;
    const tiles=[{me:true}].concat([..._room.members].filter(h=>h!==S.ME.pubkey).map(h=>({hex:h})));
    const n=tiles.length;
    const inviter = _room.invite ? (profOf(_room.invite.from)||{}) : null;
    el.innerHTML=`<div class="room-hd">📞 Group call · ${n} ${n===1?'person':'people'}${ringingIn&&inviter?` — ${enc(inviter.name||'invite')}`:''}</div>
      <div class="call-grid" data-n="${Math.min(n,6)}">${tiles.map(t=>{
        if(t.me) return `<div class="call-tile me"><video id="room-local" autoplay playsinline muted ${_room.video?'':'style="display:none"'}></video>${_room.video?'':`<img src="${enc((profOf(S.ME.pubkey)||{}).picture||S.LOGO)}" onerror="this.src='${S.LOGO}'">`}<span class="tl-name">You</span></div>`;
        const p=profOf(t.hex)||{}; const peer=_room.peers.get(t.hex); const conn=peer&&peer.stream;
        return `<div class="call-tile" data-hex="${t.hex}"><video class="room-remote" data-hex="${t.hex}" autoplay playsinline ${_room.video&&conn?'':'style="display:none"'}></video>${(_room.video&&conn)?'':`<img src="${enc(p.picture||S.LOGO)}" onerror="this.src='${S.LOGO}'">`}<span class="tl-name">${enc(p.name||p.display_name||'…')}${conn?'':' <span class="muted">·connecting</span>'}</span></div>`;
      }).join('')}</div>
      <div class="call-actions">${ringingIn
        ? `<button class="call-btn accept" id="room-accept"><svg class="ic b-ic" aria-hidden="true"><use href="#i-phone"></use></svg>Join</button><button class="call-btn decline" id="room-decline" aria-label="Decline call"><svg class="ic x-ic" aria-hidden="true"><use href="#i-close"></use></svg></button>`
        : `<button class="call-btn" id="room-min" title="minimize"><svg class="ic x-ic" aria-hidden="true"><use href="#i-minimize"></use></svg></button><button class="call-btn" id="room-mute" title="mute">${_room.muted?'🔇':'🎙️'}</button>${_room.video?`<button class="call-btn" id="room-cam">${_room.camOff?'🚫':'📷'}</button>`:''}<button class="call-btn hang" id="room-hang" aria-label="Hang up"><svg class="ic x-ic" aria-hidden="true"><use href="#i-close"></use></svg></button>`}</div>`;
    const lv=el.querySelector('#room-local'); if(lv && _room.local && lv.srcObject!==_room.local){ lv.srcObject=_room.local; lv.play&&lv.play().catch(()=>{}); }
    el.querySelectorAll('.room-remote').forEach(v=>{ const peer=_room.peers.get(v.dataset.hex); if(peer && peer.stream && v.srcObject!==peer.stream){ v.srcObject=peer.stream; v.play&&v.play().catch(()=>{}); } });
    const b=id=>el.querySelector('#'+id);
    if(b('room-accept')) b('room-accept').onclick=_roomAccept;
    if(b('room-decline')) b('room-decline').onclick=_roomDecline;
    if(b('room-hang')) b('room-hang').onclick=_roomLeave;
    if(b('room-min')) b('room-min').onclick=e=>{ e.stopPropagation(); _setOverlayMini('room-overlay', true); };
    if(b('room-mute')) b('room-mute').onclick=()=>{ if(_room&&_room.local){ _room.muted=!_room.muted; _room.local.getAudioTracks().forEach(t=>t.enabled=!_room.muted); _roomUI(); } };
    if(b('room-cam')) b('room-cam').onclick=()=>{ if(_room&&_room.local){ _room.camOff=!_room.camOff; _room.local.getVideoTracks().forEach(t=>t.enabled=!_room.camOff); _roomUI(); } };
  }

  /* The rest of app.js reaches the call state only through these functions — never `_call`/`_room`
   * directly — so the whole calls block can live in its own module (calls.js). */
  function _hangupActiveCall(){ if(_room) _roomLeave(); else _hangup(false); }
  function _rdCallGeometry(){ return _call&&_call.remoteGeometry; }
  /* TEST-ONLY, and it can START nothing. It paints the viewer's own UI over a LOCAL stream so a
   * browser test can drive the SHIPPED controls, handlers, markup and stylesheet instead of a copy
   * of them — which is the only way to catch the class of bug this control has already had twice
   * (a mapping that looks perfect and clicks in the wrong place). There is no peer connection and
   * no signalling: the "control channel" is an array in this page, so every message the viewer
   * sends lands in `__rdOutbox()` and reaches no other machine. */
  function _rdFakeViewerSession(opts){
      const o=opts||{},outbox=[];
      _call={id:_rid(),peer:(S.ME&&S.ME.pubkey)||'',pc:null,local:null,remote:o.stream||null,video:true,
             remoteDesktop:true,caller:false,state:'connected',controlGranted:!!o.control,
             remoteGeometry:o.geometry||null,rdOutbox:outbox,
             control:{readyState:o.channel==='connecting'?'connecting':'open',
                      send:(text)=>{try{outbox.push(JSON.parse(text));}catch(_){outbox.push(text);}}}};
      _callUI();
      return !!document.getElementById('call-remote');
  }
  function _rdOutbox(){ return ((_call&&_call.rdOutbox)||[]).slice(); }
  function _rdSetControl(on){ if(!_call)return false;_call.controlGranted=!!on;_callUI();return !!_call.controlGranted; }
  function _rdOpenChannel(){ if(!_call||!_call.control)return false;_call.control.readyState='open';_callUI();return true; }


  return {
    _fetchIceServers, _hangupActiveCall, _mediaErrMsg, _preferH264, _rdApplyZoom, _rdCallGeometry,
    _rdClampAt, _rdClampScale, _rdFakeViewerSession, _rdOpenChannel, _rdOutbox, _rdPanBy,
    _rdSetControl, _rdSourceDownscale, _rdStageBox, _rdVideoPoint, _rdZoomAnchor, _rdZoomBounds,
    _rdZoomBy, _rdZoomCentre, _rdZoomMetrics, _rdZoomSetMode, _rdZoomState, _rdZoomToScale,
    _rdZoomTransform, renderCalls, setRemoteDesktopArmed, setRemoteDesktopHost, startCall,
    startCallSignaling, startGroupCall, startRemoteDesktop,
  };
};
