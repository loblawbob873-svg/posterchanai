/* push.js — split out of app.js by scripts/split_client_module.mjs.
 *
 * Built on first use by app.js's lazy-module loader (`_pushMod` / `_pushLoad`). The code below is
 * app.js's own, moved byte-for-byte; its reads of app.js's live `let` bindings were rewritten to
 * `S.<name>` (getters/setters on `dep.state`) at exact identifier offsets, and everything else it uses
 * arrives through `dep`. tests/test_client_module_deps.py proves every name resolves.
 */
window.PCPushFactory = function(dep){
  const S = dep.state;   // live app.js bindings: S.GUEST, S.ME
  const {
    $, _capPlugin, _instanceBase, _urlB64ToUint8, mirrorPushPrefs, sign, toast,
  } = dep;

  /* "Stay connected" is the opt-in persistent-notification fallback for packaged Android builds
   * that cannot receive distributor push. Its switch reflects the remembered preference, and a
   * refused native service start must put the control back instead of claiming a false state. */
  async function _wireStayConnected(){
    const row = $('#set-stay-row'), box = $('#set-stay');
    if(!row || !box) return;
    const P = _capPlugin('PosterChanPush', 'stayConnected');
    if(!P) return;
    let cur = null;
    try{ cur = await P.stayConnected(); }catch(_){ return; }
    row.hidden = false;
    box.checked = !!(cur && cur.on);
    box.onchange = async () => {
      const want = box.checked;
      try{
        await P.setStayConnected({ on: want });
        toast(want ? 'staying connected — see the permanent notification'
                   : 'stopped staying connected');
      }catch(e){
        box.checked = !want;
        toast('could not change it: ' + ((e && (e.message || e.errorMessage)) || 'refused'));
      }
    };
  }

  async function pushState(){   // 'unsupported' | 'denied' | 'off' | 'on'
    // Check the native plugin FIRST. A Capacitor WebView has no PushManager, so the browser test
    // below reports 'unsupported' and disables the button — on the one build that now has a working
    // transport of its own. This ordering is the difference between the APK offering notifications
    // and permanently claiming it cannot do them.
    const P=_pushPlugin();
    if(P){ try{ return ((await P.getEndpoint())||{}).endpoint ? 'on' : 'off'; }catch(_){ return 'off'; } }
    if(!('serviceWorker' in navigator) || !('PushManager' in window) || !('Notification' in window)) return 'unsupported';
    if(Notification.permission==='denied') return 'denied';
    try{ const reg=await navigator.serviceWorker.ready; return (await reg.pushManager.getSubscription()) ? 'on' : 'off'; }
    catch(_){ return 'off'; }
  }
  async function enablePush(){
    if(S.GUEST||!S.ME.pubkey){ toast('Log in first'); return; }
    // The packaged Android app has no Web Push to ask for — take the native road instead.
    const P=_pushPlugin();
    if(P) return await _enablePushNative(P);
    // Permission is requested by the CLICK HANDLER, while user activation is still valid — asking
    // again here would be a second prompt on Chromium and a rejection on WebKit.
    if(Notification.permission!=='granted' && (await Notification.requestPermission())!=='granted'){
      toast('Notifications blocked'); return; }
    const reg=await navigator.serviceWorker.ready;
    let publicKey; try{ publicKey=(await fetch('/api/push/vapid').then(r=>r.json())).publicKey; }catch(_){}
    if(!publicKey){ toast('Push not configured on the server'); return; }
    // subscribe() is where iOS actually refuses, and it refuses by THROWING. Unhandled, that landed
    // in the button's catch as "push change failed" — a message that names nothing, on the one step
    // that fails most. Surface what the browser said; it is the difference between guessing and
    // knowing. (Seen here: an installed PWA whose permission was granted in the Safari tab instead,
    // which reads as AbortError with no further detail.)
    let sub;
    try{
      sub = await reg.pushManager.subscribe({ userVisibleOnly:true, applicationServerKey:_urlB64ToUint8(publicKey) });
    }catch(e){
      const why = (e && (e.name || e.message)) ? `${e.name||''} ${e.message||''}`.trim() : 'unknown error';
      toast('This device refused to subscribe: '+why);
      return;
    }
    return await _registerPushSub(sub.toJSON());
  }
  /* The packaged Android app takes a different road to the same place. A WebView has no Web Push,
   * so the native service keeps one authenticated PosterChan socket and draws notifications itself.
   * Credentials are issued for this installation only and sealed by the native Keystore code. */
  // _capPlugin, NOT a bare Capacitor.Plugins lookup: for a NATIVELY-registered plugin that map is
  // empty, so the direct read returns undefined, pushState() falls through to 'unsupported', and the
  // APK reports it cannot do notifications at all. This repo has been bitten by that twice already.
  function _pushPlugin(){ try{ return _capPlugin('PosterChanPush','register'); }catch(_){ return null; } }
  async function _directPushAuth(action, deviceId){
    if(!deviceId) throw new Error('this installation has no notification identity');
    return await Promise.race([
      sign(27235,`posterchan-direct:${action}:${deviceId}`,[['p',S.ME.pubkey],['d',deviceId]]),
      new Promise((_,rej)=>setTimeout(()=>rej(new Error('signer timeout')),30000)),
    ]);
  }
  function _directPushSocketUrl(path='/api/push/direct/ws'){
    // Bundled Android pages live at https://localhost; HTTP is routed to the selected instance.
    // Native WebSockets must use that same instance, never the asset page's location.
    const base=_instanceBase();
    if(!base) throw new Error('Choose a server before enabling notifications');
    const socket=new URL(path,base+'/');
    if(socket.origin!==new URL(base).origin) throw new Error('Notification server does not match this instance');
    socket.protocol=socket.protocol==='https:'?'wss:':'ws:';
    return socket.href;
  }
  async function _enablePushNative(P){
    const local=(await P.getEndpoint())||{};
    const deviceId=local.deviceId||local.device_id;
    let auth;
    try{ auth=await _directPushAuth('register',deviceId); }
    catch(e){ toast('Your signer did not approve notifications: '+((e&&e.message)||e)); return; }
    const issued=await fetch('/api/push/direct/register',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({pubkey:S.ME.pubkey,device_id:deviceId,auth:btoa(JSON.stringify(auth))})
    }).then(r=>r.json()).catch(()=>null);
    if(!(issued&&issued.ok&&issued.token&&issued.websocket_url)){
      toast('Could not turn notifications on'+((issued&&issued.error)?': '+issued.error:'')); return;
    }
    const r=await P.register({socketUrl:_directPushSocketUrl(issued.websocket_url),token:issued.token,deviceId:issued.device_id||deviceId});
    if(!(r&&r.ok)){
      // Android asks for notification permission only after the signed server registration. Revoke
      // that fresh token when permission is refused instead of leaving a phantom device behind.
      try{
        const undo=await _directPushAuth('unregister',deviceId);
        await fetch('/api/push/direct/unregister',{method:'POST',headers:{'Content-Type':'application/json'},
          body:JSON.stringify({pubkey:S.ME.pubkey,device_id:deviceId,auth:btoa(JSON.stringify(undo))})});
      }catch(_){}
      toast('Could not start notifications'+((r&&r.error)?': '+r.error:'')); return;
    }
    /* Tell the server what THIS phone wants before anything can be delivered to it. A device with
     * no preferences on file receives everything (push_prefs fails open, deliberately), so without
     * this the first thing a freshly-registered phone does is exactly what the user turned off. */
    void mirrorPushPrefs(S.ME.pubkey);
    // Now say whether the OS will actually let any of it through. Being force-stopped by a battery
    // setting silences push AND password autofill, and reports nothing.
    try{ const b=await P.batteryStatus();
      if(b && !b.healthy) toast(b.backgroundRestricted
        ? 'Notifications on — but this phone has the app on a battery restriction, which blocks them. Open settings to fix.'
        : 'Notifications on — battery optimisation may delay them.');
    }catch(_){}
  }
  async function _registerPushSub(subscription){
    // A remote signer (NIP-46/Amber) can simply never answer — the phone is asleep, the bunker relay
    // is down, the approval was never tapped. Awaited bare, that hangs here forever: the browser
    // subscription exists, no row is ever written, the button sits disabled and nothing is said.
    // A bounded wait turns the worst failure mode (silence) into a sentence.
    let auth;
    try{
      auth = await Promise.race([
        sign(27235,'push-subscribe',[['p',S.ME.pubkey]]),
        new Promise((_,rej)=>setTimeout(()=>rej(new Error('signer timeout')), 30000)),
      ]);
    }catch(e){
      toast('Your signer did not approve it: '+((e&&(e.name||e.message))||e));
      return false;
    }
    const r=await fetch('/api/push/subscribe',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({ pubkey:S.ME.pubkey, subscription, auth:btoa(JSON.stringify(auth)) })}).then(r=>r.json()).catch(()=>null);
    if(!(r&&r.ok)){
      toast('Could not turn notifications on'+((r&&r.error)?': '+r.error:''));
      // Undo the browser-side subscription. Left in place, pushState() reads 'on' from it while the
      // server has no row to deliver to — a toggle that says yes and a phone that never buzzes.
      try{ const reg=await navigator.serviceWorker.ready; const s2=await reg.pushManager.getSubscription(); if(s2) await s2.unsubscribe(); }catch(_){}
      return false;
    }
    toast('🔔 Push notifications on');
    /* A device that has just registered has no preferences on the server yet, so it would receive
     * everything until the next toggle happened to mirror them. Send what this device already
     * believes, now. */
    void mirrorPushPrefs(S.ME.pubkey);
    return true;
  }
  /* Send a real notification the whole way round — server → push service → this device.
   * A local showNotification() would only prove the browser can draw one, which is never the part
   * that breaks; the failures are permission, an uninstalled iOS PWA, a stale subscription, or the
   * OS holding the app asleep. This reports which. */
  async function _pushTestWait(job, message, ms=30000){
    let timer;
    try{ return await Promise.race([job,new Promise((_,reject)=>{
      timer=setTimeout(()=>reject(new Error(message)),ms);
    })]); }finally{ clearTimeout(timer); }
  }
  const _pushTestJobs=new Map();
  async function testPush(){
    if(S.GUEST||!S.ME.pubkey){ toast('Log in first'); return; }
    const owner=S.ME.pubkey;
    if(_pushTestJobs.has(owner)) return await _pushTestJobs.get(owner);
    const job=_runPushTest(owner);
    _pushTestJobs.set(owner,job);
    try{ return await job; }
    finally{ if(_pushTestJobs.get(owner)===job) _pushTestJobs.delete(owner); }
  }
  async function _runPushTest(owner){
    toast('Checking notifications…');
    try{
      if((await _pushTestWait(pushState(),'Notification status did not answer',10000))!=='on'){
        toast('Notifications are off — turn them on first'); return;
      }
      if(S.ME.pubkey!==owner) throw new Error('Account changed; run the test again');
      const P=_pushPlugin();
      if(!P && Notification.permission!=='granted'){ toast('This browser is blocking notifications'); return; }
      let deviceId;
      if(P){
        let local=await _pushTestWait(P.getEndpoint({expectedSocketUrl:_directPushSocketUrl()}),'This device did not answer',10000);
        if(S.ME.pubkey!==owner) throw new Error('Account changed; run the test again');
        if(local && local.needsRegistration){
          toast('Repairing this device’s notification connection…');
          await _pushTestWait(_enablePushNative(P),'Notification registration did not answer');
          local=await _pushTestWait(P.getEndpoint({expectedSocketUrl:_directPushSocketUrl()}),'This device did not answer',10000);
          if(!local || local.needsRegistration || !local.endpoint) throw new Error('Enable notifications again to reconnect this device');
        }
        if(local && local.notificationsEnabled===false) throw new Error('Android is blocking message notifications; enable them in app notification settings');
        deviceId=local && (local.deviceId||local.device_id);
        if(!deviceId) throw new Error('This device needs to enable notifications again');
      }
      if(S.ME.pubkey!==owner) throw new Error('Account changed; run the test again');
      toast('Waiting for your signer to approve the notification test…');
      const auth=await _pushTestWait(sign(27235,'push-test',[['p',owner]]),'Your signer did not answer the notification test');
      if(S.ME.pubkey!==owner) throw new Error('Account changed; run the test again');
      toast('Sending notification test…');
      const controller=new AbortController();
      let r;
      try{
        r=await _pushTestWait(fetch('/api/push/test',{method:'POST',signal:controller.signal,headers:{'Content-Type':'application/json'},
          body:JSON.stringify({pubkey:owner,auth:btoa(JSON.stringify(auth)),...(deviceId?{device_id:deviceId}:{})})
        }).then(async response=>{
          if(!response.ok) throw new Error('Server returned '+response.status);
          return await response.json();
        }),'The server did not answer the notification test',30000);
      }finally{ controller.abort(); }
      if(r&&r.ok){
        if(r.queued && P){
          const status=await _pushTestWait(P.getEndpoint(),'This device did not answer',5000);
          if(status && status.connected===false){
            toast('Test queued — this device is not connected to notifications'+(status.error?': '+status.error:'. Reopen the app to reconnect.'));
            return;
          }
        }
        if(r.queued) toast('Test queued for this device — check your notifications. If it does not arrive, reopen the app and check Android notification and battery settings.');
        else if(r.accepted) toast('Push service accepted the test — check your notifications.');
        else toast('Server accepted the test — check your notifications.');
      }else toast((r&&r.error)||'The server could not send the test');
    }catch(e){ toast('Notification test failed: '+(e&&e.message||e)); }
  }
  async function disablePush(){
    const P=_pushPlugin();
    if(P){ try{
        const local=(await P.getEndpoint())||{}, deviceId=local.deviceId||local.device_id;
        if(deviceId){
          const auth=await _directPushAuth('unregister',deviceId);
          await fetch('/api/push/direct/unregister',{method:'POST',headers:{'Content-Type':'application/json'},
            body:JSON.stringify({pubkey:S.ME.pubkey,device_id:deviceId,auth:btoa(JSON.stringify(auth))})});
        }
        await P.unregister();
      }catch(e){ toast('Could not turn notifications off: '+((e&&e.message)||e)); return; }
      toast('Push notifications off'); return; }
    try{ const reg=await navigator.serviceWorker.ready; const sub=await reg.pushManager.getSubscription();
      if(sub){ await fetch('/api/push/unsubscribe',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({endpoint:sub.endpoint})});
        await sub.unsubscribe(); } }catch(_){}
    toast('Push notifications off');
  }
  async function _wirePushToggle(){
    const btn=$('#set-push-toggle'), st=$('#set-push-status'), tb=$('#set-push-test'); if(!btn) return;
    // iOS gives a Safari TAB no push at all — the PWA has to be installed to the Home Screen first.
    // Nothing surfaces that: permission can be granted, the toggle can read "on", and no notification
    // ever arrives. Say it here rather than let the Test button be the only clue.
    const iosTab = /iP(hone|ad|od)/.test(navigator.userAgent||'')
      && !(navigator.standalone===true || (matchMedia && matchMedia('(display-mode: standalone)').matches));
    const render=(s)=>{
      if(tb) tb.style.display = s==='on' ? '' : 'none';
      if(s==='unsupported'){ btn.disabled=true; btn.textContent='🔔 Not supported on this browser';
        if(st && iosTab) st.textContent='On iPhone, add this app to your Home Screen first — Safari tabs cannot receive notifications.';
        return; }
      if(s==='denied'){ btn.disabled=true; btn.textContent='🔔 Blocked in browser settings'; if(st) st.textContent='Re-enable notifications for this site in your browser settings.'; return; }
      btn.disabled=false; btn.textContent = s==='on' ? '🔕 Turn off push notifications' : '🔔 Enable push notifications';
      if(st) st.textContent = s==='on'
        ? "On — calls, messages, mentions, replies, reactions and zaps. Press Test to prove it reaches this device."
        : (iosTab ? 'On iPhone, add this app to your Home Screen first — Safari tabs cannot receive notifications.' : '');
    };
    if(tb) tb.onclick=async()=>{ tb.disabled=true; try{ await testPush(); } finally { tb.disabled=false; } };
    let cur = await pushState();
    render(cur);
    /* Ask for permission on the SYNCHRONOUS part of the click, before any await.
     *
     * This handler used to `await pushState()` first — which awaits serviceWorker.ready and
     * getSubscription() — and only then reached Notification.requestPermission(). WebKit requires
     * TRANSIENT user activation for that prompt, and those async hops spend it, so on an installed
     * iOS PWA the request was rejected before a prompt could appear: no permission, no subscription,
     * no server row, and nothing to see. Chromium is lenient here, which is why it only ever failed
     * on iPhone. `cur` is the state the button was last RENDERED with, so no await is needed to know
     * which way the toggle goes. */
    btn.onclick=()=>{
      const turningOn = cur !== 'on';
      const asked = (turningOn && typeof Notification !== 'undefined' && Notification.permission === 'default')
        ? Notification.requestPermission()      // called with activation intact; NOT awaited yet
        : Promise.resolve(typeof Notification !== 'undefined' ? Notification.permission : 'granted');
      btn.disabled = true;
      (async()=>{
        try{
          if(!turningOn) await disablePush();
          else if((await asked) !== 'granted') toast('Notifications blocked — allow them for this app in iOS Settings');
          else await enablePush();
        }catch(e){ toast('push change failed: '+((e&&(e.name||e.message))||e)); }
        cur = await pushState();
        render(cur);
      })();
    };
  }


  return {
    _pushPlugin, _wirePushToggle, _wireStayConnected, pushState,
  };
};
