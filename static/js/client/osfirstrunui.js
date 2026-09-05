/* THE FIRST THING A NEW POSTERCHANOS MACHINE SHOWS.
 *
 * `osfirstrun.js` decides WHICH step is next and is DOM-free so it can be tested; this is the half
 * that draws it. The order — wifi, instance, Tor, sign in, account — is argued for there and not
 * repeated here.
 *
 * WHAT THIS IS NOT: a second sign-in screen, a second instance picker, or a second Tor panel. The
 * client already has all three, they are the ones the rest of the app uses, and a wizard with its
 * own copies is three surfaces that drift apart — the one on first boot being the copy nobody ever
 * looks at again, and therefore the one that rots. So the wizard OWNS only the wifi screen (which
 * exists nowhere else, because no other client is an operating system) and otherwise hands off:
 * `showAuth()` for the key, the client's own instance setting, the desktop's own Tor bridge.
 *
 * IT IS ONLY EVER SHOWN WHERE POSTERCHAN IS THE DESKTOP. In a browser tab, the APK, or the desktop
 * app on somebody's Windows machine there is no machine to set up — no radio to join, no Unix
 * account to make — and `PCOSShell.available()` is the same question asked everywhere else here.
 */
(function(root){
  'use strict';

  const FR = () => root.PCFirstRun || null;
  const SHELL = () => root.PCOSShell || null;
  const NET = () => root.pcNet || null;
  const SHELLB = () => root.pcShell || null;      // the desktop bridge — Tor lives on it
  const PC = () => root.__PC || null;

  const H = (s) => String(s == null ? '' : s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');

  /* THE TWO ANSWERS THAT ARE ONLY EVER GIVEN HERE. "I do not want an instance" and "I do not want
   * Tor" are decisions, not omissions — nothing else in the app records them, and without somewhere
   * to write them down the wizard asks the same two questions on every boot for ever. Local to the
   * machine on purpose: they are about THIS computer, not about the account. */
  const KEY_INSTANCE_SKIP = 'pc_fr_instance_skipped';
  const KEY_TOR = 'pc_fr_tor';                    // 'on' | 'off'
  /* "I do not want a key yet", recorded the same way every other answer is.
   *
   * Not a "have we run the wizard" flag -- this module deliberately reads the world each boot
   * rather than trusting one of those, because they go stale. This is an ANSWER, and it sits beside
   * instanceSkipped and torSkipped which are the same shape. Without it, somebody who deliberately
   * browses signed out is asked again on EVERY boot: `everHadAccount` stays false for ever, so the
   * welcome would nag the one person it can never help. */
  const KEY_SIGNIN_SKIP = 'pc_fr_signin_skipped';
  const get = (k) => { try{ return localStorage.getItem(k); }catch(_){ return null; } };
  const set = (k, v) => { try{ localStorage.setItem(k, v); }catch(_){} };

  /* The identity has to come from the saved session, not only from `window.ME`. ME is normally a
   * closure inside app.js, and even in builds that briefly publish it there is a startup window
   * after authentication where the session is durable but that global is not ready. readWorld
   * already used Session for this reason; provisioning used ME alone and could therefore call the
   * root helper with an EMPTY npub, then falsely report that the home could not be made. */
  function signedInNpub(){
    try{
      const S = root.Session;
      const sess = S && S.load ? S.load() : null;
      const id = sess && (sess.npub || sess.userNpub || sess.pubkey || sess.userPk);
      if(id && String(id).startsWith('npub1')) return String(id);
    }catch(_){}
    try{ return (root.ME && root.ME.npub) ? String(root.ME.npub) : ''; }catch(_){ return ''; }
  }

  function savedSession(){
    try{
      const S = root.Session;
      return (S && S.load) ? S.load() : null;
    }catch(_){ return null; }
  }

  /* WHAT THE MACHINE ACTUALLY IS RIGHT NOW — never a remembered "which step were we on" counter,
   * which is the thing that goes stale the moment somebody fixes something outside the wizard.
   *
   * `netReadable: false` is deliberately distinct from `online: false`. A machine with the radio off
   * and a machine whose NetworkManager is not answering look identical from a wifi list, and only
   * one of them can be fixed by picking a network. */
  async function readWorld(){
    const w = { netReadable: true };
    const net = NET();
    if(!net){ w.netReadable = false; }
    else{
      /* The getty is ordered after NetworkManager on current PosterChanOS images, but older images
       * and a manually-started desktop can still race D-Bus activation. A service that is starting
       * is not missing hardware. Give slow USB boots a bounded readiness window before showing the
       * fatal network screen; three seconds was shorter than startup on real live media. */
      for(let attempt = 0; attempt < 60; attempt++){
        try{
          const s = await net.status();
          w.online = !!(s && s.online);
          break;
        }catch(_){
          if(attempt === 59) w.netReadable = false;
          else await new Promise(resolve => setTimeout(resolve, 500));
        }
      }
    }
    try{ w.instance = !!(PC() && PC().apiBase && PC().apiBase()); }catch(_){ w.instance = false; }
    if(!w.instance){ try{ w.instance = !!(root.__PC_API_BASE__); }catch(_){} }
    w.instanceSkipped = get(KEY_INSTANCE_SKIP) === '1';
    const tor = get(KEY_TOR);
    w.torChosen = tor === 'on';
    w.torSkipped = tor === 'off';
    /* NOT SIGNED IN IS NOT THE SAME AS SIGNED IN AS A GUEST. The client reads the public timeline
     * without a key, so `ME` exists in both cases and only the pubkey separates them. */
    /* THE SAVED SESSION IS THE ANSWER, NOT `window.ME` -- WHICH DOES NOT EXIST.
     *
     * `ME` is a closure variable inside app.js and is not published on window, so this read was
     * ALWAYS empty and `signin` always said "todo". It went unnoticed because the old rule
     * short-circuited before it mattered: `instance !== 'done' && signin !== 'done'` is false the
     * moment there is an instance, whatever this said. The moment signin got a say of its own, a
     * machine that WAS signed in was told it was not, and the welcome came back on every boot.
     *
     * `Session.load()` is the record that decides it for real: it is what `resume()` signs back in
     * from, it survives a reload, and it is empty exactly when nobody is signed in. Read through
     * the same accessor as everHadAccount below, and `root.ME` is kept as a fallback in case a
     * later build does publish it. */
    w.pubkey = '';
    try{ if(!root.GUEST) w.pubkey = signedInNpub(); }catch(_){ }
    if(!w.pubkey){
      try{ w.pubkey = (root.ME && !root.GUEST && root.ME.npub) ? String(root.ME.npub) : ''; }
      catch(_){ w.pubkey = ''; }
    }
    /* HAS THIS MACHINE EVER HELD AN ACCOUNT? Not a "we have run before" flag -- those go stale and
     * this module deliberately reads the world each boot -- but the account switcher's own list,
     * which survives signing out precisely so you can sign back in. It is the difference between a
     * machine nobody has set up yet and one whose owner signed out an hour ago, and only the first
     * of those should be walked through a welcome. */
    /* THE LIST ITSELF, not only the accessor over it. `Session` is a module global published by
     * store.js, and this runs early in startApp -- depending on another module having loaded first
     * is a load-order assumption, and when it is wrong the answer is a silent FALSE: "nobody has
     * ever signed into this machine", which seizes the screen of somebody who has. Read through
     * Session when it is there, because that is where the shape is defined, and off the key it
     * writes when it is not. */
    w.everHadAccount = false;
    try{
      const S = root.Session;
      if(S && S.accounts) w.everHadAccount = !!S.accounts().length;
      else {
        const raw = root.localStorage && root.localStorage.getItem('pc_nostr_accounts');
        const list = raw ? JSON.parse(raw) : [];
        w.everHadAccount = Array.isArray(list) && list.length > 0;
      }
    }catch(_){ w.everHadAccount = false; }
    w.signinSkipped = get(KEY_SIGNIN_SKIP) === '1';
    /* The Unix administrator claim is the durable first-run answer. Session/localStorage can be
     * late during Electron startup; the machine claim cannot, and once it exists Welcome must not
     * seize the desktop again merely because the renderer has not resumed the account yet. */
    w.provisioned = false;
    try{ w.provisioned = !!(SHELL() && SHELL().provisioned && await SHELL().provisioned()); }
    catch(_){ w.provisioned = false; }
    w.homeReady = _homeReady;
    w.provisionFailed = _provisionFailed;
    return w;
  }

  let _homeReady = false, _provisionFailed = false, _el = null, _busy = false;

  /* ── the frame ──────────────────────────────────────────────────────────────────────────────── */

  function mount(){
    if(_el) return _el;
    _el = document.createElement('div');
    _el.className = 'osfr';
    _el.id = 'osfr';
    document.body.appendChild(_el);
    document.documentElement.classList.add('osfr-on');
    return _el;
  }

  function unmount(){
    if(_el){ try{ _el.remove(); }catch(_){} _el = null; }
    document.documentElement.classList.remove('osfr-on');
  }

  /* Every screen is this shape, so a step is a title, a sentence and a body — and the progress dots
   * are drawn from the step LIST rather than from a number, which is what keeps them honest when a
   * step is skipped because it was already satisfied. */
  function shell(step, title, sub, body, foot){
    const steps = (FR() && FR().STEPS) || [];
    const i = steps.indexOf(step);
    const dots = steps.map((s, n) =>
      `<span class="osfr-dot${n === i ? ' on' : ''}${n < i ? ' done' : ''}"></span>`).join('');
    _el.innerHTML =
      `<div class="osfr-card glass neon-border">
         <div class="osfr-dots">${dots}</div>
         <h2 class="osfr-h">${H(title)}</h2>
         <p class="osfr-sub">${H(sub)}</p>
         <div class="osfr-body">${body}</div>
         <div class="osfr-foot">${foot || ''}</div>
       </div>`;
    return _el.querySelector('.osfr-card');
  }

  const say = (m) => { try{ PC() && PC().toast(m); }catch(_){} };

  /* ── the steps ──────────────────────────────────────────────────────────────────────────────── */

  /* NETWORK. The one screen that exists nowhere else in this client, and the one that has to come
   * first: without it the instance cannot be reached, Tor cannot bootstrap and a remote signer
   * cannot be contacted — three screens that would each report their own unrelated failure. */
  async function stepNetwork(blocked){
    const net = NET();
    if(blocked || !net){
      shell('network', 'Cannot see the network service',
            'NetworkManager did not answer. Nothing after this can work, so setup stops here '
            + 'rather than reporting four more failures with the same cause.',
            '', `<button class="btn btn-ghost" data-fr="retry">Try again</button>`);
      _el.querySelector('[data-fr="retry"]').onclick = () => run();
      return;
    }
    const card = shell('network', 'Join a network',
      'Pick your wifi. If this computer is plugged into a cable, it is already online and this step '
      + 'will pass by itself.',
      `<div class="osfr-list" id="osfr-wifi"><div class="spinner"></div></div>`,
      `<button class="btn btn-ghost small" data-fr="rescan">Scan again</button>`);
    card.querySelector('[data-fr="rescan"]').onclick = () => stepNetwork(false);

    let list = null;
    try{ list = await net.wifi(true); }catch(_){ list = null; }
    if(!_el || !_el.contains(card)) return;
    const box = card.querySelector('#osfr-wifi');
    if(list === null){
      /* Could not ask is not "no networks". A room with no wifi in it and a NetworkManager that is
       * not running produce the same empty list, and only one of them is worth waiting in. */
      box.innerHTML = `<div class="osfr-none">The wifi list could not be read on this computer.</div>`;
      return;
    }
    const rows = FR().networksForPicker(list);
    box.innerHTML = rows.length
      ? rows.map(n => `<button class="osfr-row${n.active ? ' on' : ''}" data-ssid="${H(n.ssid)}"
            data-sec="${n.secure ? '1' : ''}"><span class="osfr-nm">${H(n.ssid)}</span>
            <span class="osfr-sig">${n.secure ? '🔒 ' : ''}${H(n.signal)}%</span></button>`).join('')
      : `<div class="osfr-none">Nothing in range. Plug in a cable, or move closer and scan again.</div>`;
    box.querySelectorAll('[data-ssid]').forEach(b => b.onclick = async () => {
      const ssid = b.dataset.ssid;
      let pw = '';
      if(b.dataset.sec){
        /* The app's own prompt, never `window.prompt` — that dialog does not exist in a WebView and
         * wedges Electron's focus, on the one screen a new machine cannot get past without typing. */
        try{ pw = await PC().uiPrompt('Password for ' + ssid, { password: true, ok: 'Join' }); }
        catch(_){ pw = null; }
        if(pw === null) return;
      }
      b.disabled = true;
      say('joining ' + ssid + '…');
      try{
        const r = await net.connect(ssid, pw);
        if(!r || r.ok === false){ say((r && r.why) || 'could not join ' + ssid); b.disabled = false; return; }
      }catch(e){ say(String((e && e.message) || e)); b.disabled = false; return; }
      run();
    });
  }

  /* INSTANCE. A PosterChan node to talk to, or the deliberate choice to run without one — the
   * client works relay-only and "no instance" is an ANSWER, which is why Skip writes it down. */
  function stepInstance(){
    let cur = '';
    try{ cur = (PC() && PC().apiBase && PC().apiBase()) || ''; }catch(_){}
    const card = shell('instance', 'Choose an instance',
      'A PosterChan server gives this machine AI, mail, media and file storage. You do not need one '
      + '— without it everything that is Nostr still works, and you can add one later in Settings.',
      `<input class="input osfr-input" id="osfr-inst" type="text" autocapitalize="none"
              autocorrect="off" spellcheck="false" placeholder="https://your-instance"
              value="${H(cur)}">`,
      `<button class="btn btn-ghost" data-fr="skip">Use no instance</button>
       <button class="btn btn-neon" data-fr="go">Connect</button>`);
    const inp = card.querySelector('#osfr-inst');
    card.querySelector('[data-fr="skip"]').onclick = () => { set(KEY_INSTANCE_SKIP, '1'); run(); };
    card.querySelector('[data-fr="go"]').onclick = () => {
      const v = String(inp.value || '').trim();
      if(!v){ say('type an address, or choose to use no instance'); return; }
      /* Handed to the client's OWN setter, which is what the rest of the app reads and what a
       * reload needs to find. A second copy of this in the wizard is a machine that comes back from
       * its first restart pointing at nothing. */
      try{ PC().setInstance(v); }
      catch(_){ say('this build cannot change the instance from here'); }
    };
  }

  /* TOR IS ON BY DEFAULT ON POSTERCHANOS, so this screen CONFIRMS rather than offers.
   *
   * The machine already booted through it — main.js starts it before the window loads anything —
   * so a card reading "Turn Tor on" would be asking for something that has already happened, and
   * pressing it would do nothing visible. Asking anyway is still right: it is the one decision here
   * that changes how every byte leaves the machine, and a person who did not choose it should be
   * told, once, in a place where they can decline. Both buttons WRITE; only one changes anything. */
  async function stepTor(){
    const have = !!(SHELLB() && SHELLB().tor);
    if(!have){
      /* Nothing to ask on a build with no bundled tor: recording the skip is the honest thing, and
       * it stops the wizard offering a switch that is not wired to anything. */
      set(KEY_TOR, 'off');
      return run();
    }
    let st = null;
    try{ st = await SHELLB().tor.status(); }catch(_){ st = null; }
    const on = !!(st && st.enabled);
    const where = (st && st.countryName) || ((st && st.country) ? String(st.country).toUpperCase() : '');
    const card = shell('tor',
      on ? 'This computer is using Tor' : 'Route this computer through Tor?',
      on ? ('Everything it sends goes over the Tor network' + (where ? ', leaving through ' + where : '')
            + '. It is slower and some sites refuse it, so you can turn it off — here, or later in '
            + 'Settings.')
         : ('Everything this machine sends would go over the Tor network. It is slower, some sites '
            + 'refuse it, and it can be turned on or off later in Settings.'),
      `<div class="osfr-note" id="osfr-tor-note">${on && st && st.bootstrapped < 100
          ? H('Building a circuit… ' + (st.bootstrapped || 0) + '%') : ''}</div>`,
      on ? `<button class="btn btn-ghost" data-fr="off">Turn Tor off</button>
            <button class="btn btn-neon" data-fr="keep">Keep using Tor</button>`
         : `<button class="btn btn-ghost" data-fr="no">Not now</button>
            <button class="btn btn-neon" data-fr="yes">Turn Tor on</button>`);
    const note = card.querySelector('#osfr-tor-note');
    const watch = () => {
      try{
        SHELLB().tor.onStatus((s) => {
          if(!s || !note.isConnected) return;
          note.textContent = s.bootstrapped >= 100 ? 'Tor is up.'
                           : 'Building a circuit… ' + (s.bootstrapped || 0) + '%';
        });
      }catch(_){}
    };
    const b = (k) => card.querySelector('[data-fr="' + k + '"]');
    if(b('keep')) b('keep').onclick = () => { set(KEY_TOR, 'on'); run(); };
    if(b('off')) b('off').onclick = async () => {
      note.textContent = 'Turning Tor off…';
      try{ await SHELLB().tor.set({ enabled: false }); }
      catch(e){ note.textContent = String((e && e.message) || e); return; }
      set(KEY_TOR, 'off');
      run();
    };
    if(b('no')) b('no').onclick = () => { set(KEY_TOR, 'off'); run(); };
    if(b('yes')) b('yes').onclick = async () => {
      note.textContent = 'Starting Tor…';
      watch();
      /* THE COUNTRY GOES WITH THE SWITCH. Turning Tor on with no exit country here and setting one
       * later means a first circuit built somewhere nobody chose — and on this profile the answer
       * is already US, so it is sent in the same call rather than in a second one. */
      try{ await SHELLB().tor.set({ enabled: true, country: 'us' }); }
      catch(e){ note.textContent = String((e && e.message) || e); return; }
      set(KEY_TOR, 'on');
      run();
    };
    if(on) watch();
  }

  /* SIGN IN. One explicit choice remains here because "Browse without an account" must be reachable;
   * the primary path jumps straight past the generic login and signer menus and draws the phone QR. */
  function stepSignin(){
    const card = shell('signin', 'Welcome to PosterChanOS',
      'Scan one code with PosterChan Signer on your phone. Your Nostr key becomes this computer’s '
      + 'account and gives you a private home folder.',
      `<div class="osfr-note">The QR opens immediately on the next screen.</div>`,
      `<button class="btn" data-fr="later">Browse without an account</button>`
      + `<button class="btn btn-neon" data-fr="qr">Show my sign-in QR</button>`);
    card.querySelector('[data-fr="later"]').onclick = () => {
      set(KEY_SIGNIN_SKIP, '1');
      unmount();
      try{ if(root.PCOS && root.PCOS.restore) root.PCOS.restore(); }catch(_){}
    };
    card.querySelector('[data-fr="qr"]').onclick = () => {
      unmount();
      try{ PC().showAuth('qr'); }
      catch(_){ say('the sign-in screen is not available here'); run(); }
    };
  }
  let _poll = 0;

  /* THE ACCOUNT is not a question — it is what the machine does once it knows who you are. */
  async function stepAccount(){
    shell('account', 'Setting up your home folder',
      'Making a Unix account for your key, so your files on this computer are yours alone.',
      `<div class="spinner"></div>`, '');
    if(_busy) return;
    _busy = true;
    const npub = signedInNpub();
    /* Authentication and its saved session can settle on adjacent turns of the event loop. An
     * absent identity is WAITING, not a provisioning attempt and certainly not proof that mkdir
     * failed. Keep the spinner and re-read the world instead of sending an invalid root command. */
    if(!npub){
      _busy = false;
      setTimeout(() => { if(_el) run(); }, 250);
      return;
    }
    let r = null;
    try{
      r = await SHELL().ensureAccount(npub);
      /* Creating the protected home is only half of account setup. A restored browser session can
       * reach this wizard without passing through app.js's fresh-login hook; merely provisioning
       * then leaves the graphical getty running as the locked `posterchan` bootstrap user. Require
       * the same durable OS-session switch here before declaring the home ready. */
      if(r && r.ok !== false){
        const sess = savedSession();
        const meta = { npub };
        const pk = sess && (sess.pubkey || sess.userPk);
        if(pk) meta.pubkey = String(pk);
        r = await SHELL().activateAccount(npub, sess, meta);
      }
    }catch(e){ r = { ok: false, why: String((e && e.message) || e) }; }
    _busy = false;
    if(r && r.ok !== false){ _homeReady = true; _provisionFailed = false; return run(); }
    _provisionFailed = true;
    const card = shell('account', 'Your home folder could not be made',
      'You are signed in and the desktop will work, but this computer could not give your key its '
      + 'own account. Everything you save here will land in the shared session account instead.',
      `<div class="osfr-note">${H((r && r.why) || 'the reason was not reported')}</div>`,
      `<button class="btn btn-ghost" data-fr="skip">Carry on anyway</button>
       <button class="btn btn-neon" data-fr="retry">Try again</button>`);
    card.querySelector('[data-fr="retry"]').onclick = () => { _provisionFailed = false; run(); };
    /* Deliberately allowed past: a machine that cannot be used at all is worse than one whose files
     * are in the wrong place, and the sentence above says which this is. */
    card.querySelector('[data-fr="skip"]').onclick = () => { _homeReady = true; run(); };
  }

  /* ── the loop ───────────────────────────────────────────────────────────────────────────────── */

  async function run(){
    if(!FR() || !SHELL()) return;
    if(!SHELL().available()) return unmount();
    const world = await readWorld();
    const next = FR().nextStep(world);
    if(next.done){
      if(_poll){ clearInterval(_poll); _poll = 0; }
      unmount();
      /* The desktop was never drawn under this — it is entered now, once, by the same call the
       * boot path makes. Harmless if it is already on. */
      try{ root.PCOS && PCOS.enter && PCOS.enter(); }catch(_){}
      return;
    }
    mount();
    if(next.step === 'network')       return stepNetwork(next.blocked);
    if(next.step === 'instance')      return stepInstance();
    if(next.step === 'tor')           return stepTor();
    if(next.step === 'signin')        return stepSignin();
    if(next.step === 'account')       return stepAccount();
  }

  /** Re-ask the world. Called by the app when something outside the wizard changed — a sign-in. */
  const recheck = () => run();

  /* SHOWN ON BOOT ONLY WHERE THIS IS THE OPERATING SYSTEM, and only when something is still
   * unanswered. `detect()` is awaited rather than guessed, exactly as the shell does it: the
   * bridges exist on Windows too, and a setup wizard is the last thing a Windows user needs. */
  async function boot(){
    try{
      if(!SHELL() || !(await SHELL().detect())) return false;
      const world = await readWorld();
      /* `machineUnusable`, NOT `firstRunNeeded` — see osfirstrun.js. An unanswered question is not
       * a reason to stand in front of a computer somebody is already using, and walking them to the
       * sign-in step would refuse a desktop that worked as a guest a minute earlier. */
      const unusable = FR().machineUnusable(world);
      /* SAY WHICH SCREEN THIS MACHINE DECIDED ON, AND WHY.
       *
       * This is the only decision on a fresh install that has no visible failure mode: a wizard
       * that does not appear looks exactly like a wizard that was already answered, and a machine
       * that boots past it to a desktop it cannot use looks like a machine that is simply slow.
       * Both were invisible in every log. It is also the one thing a LiveCD test can ask of a
       * running session without photographing the screen — an ISO that BOOTS proves nothing about
       * whether it boots to the welcome screen, and a picture of a framebuffer is a test of QEMU's
       * font rendering. `scripts/check_livecd_welcome.py` reads exactly this line over the guest's
       * serial console. No user content: a step name and the answered/unanswered state of five
       * questions. */
      try{
        const next = FR().nextStep(world) || {};
        console.log('[firstrun] ' + (unusable ? 'showing' : 'skipped')
                    + ' step=' + (next.step || 'none')
                    + ' blocked=' + (next.blocked ? 1 : 0)
                    + ' state=' + JSON.stringify(next.state || {}));
      }catch(_){ }
      if(!unusable) return false;
      await run();
      return true;
    }catch(_){ return false; }
  }

  const API = { boot, run, recheck, readWorld, unmount,
                needed: async () => FR().firstRunNeeded(await readWorld()) };
  root.PCFirstRunUI = API;
  if(typeof module !== 'undefined' && module.exports) module.exports = API;
})(typeof globalThis !== 'undefined' ? globalThis : this);
