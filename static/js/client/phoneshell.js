/* THE PHONE SHELL — the app's side of the three Android roles: home screen, messages, phone.
 *
 * The screens themselves are NATIVE (mobile/android/.../home, .../sms, .../phone) and that is
 * deliberate: a launcher that fails takes the phone's home screen with it, and this app's WebView
 * renderer is measured to die under memory pressure. So none of those screens is drawn here. What
 * lives in this file is the part that belongs to the app:
 *
 *   * the OPT-IN — the switches that ask Android for each role, and give them back;
 *   * the THEME mirror, so nine palettes drawn by Android match the one drawn by CSS;
 *   * the LANDING — a home-screen tile says which PosterChan screen to open, and this reads it.
 *
 * Everything here degrades to nothing in a browser: `capPlugin` returns null off the packaged app,
 * every entry point checks, and the settings card simply is not rendered. A permanently greyed
 * switch reads as broken, so it is absent instead.
 */
(function(){
  let PC = null;

  /* A launcher destination must run AFTER app.js has finished booting. #feed is present in the
   * server-rendered shell before boot even starts, so using its existence as "ready" races the
   * config/store load on a slower tablet: this module opens Folder Sync, then startApp restores the
   * remembered desktop over it. app.js raises pc-app-ready after that restore/initial landing. */
  function landView(v){
    const go = () => {
      try{ if(window.PCOS && PCOS.mobileLanding) PCOS.mobileLanding(); }catch(_){}
      if(v.indexOf('post:') === 0){
        const id=v.slice(5);
        try{ if(/^[0-9a-f]{64}$/i.test(id)) PC.openThread(id); }catch(_){}
      } else if(v === '__music'){
        try{ if(typeof PC.openMusic === 'function') PC.openMusic(); }catch(_){}
      } else if(v.indexOf('contact:') === 0){
        /* contacts.js may still be loading when the native dialer hands us this destination.
         * Park the number on window; PCContacts.render consumes it after its data is ready. */
        try{ window.__PC_CONTACT_PHONE = decodeURIComponent(v.slice(8)); }catch(_){
          window.__PC_CONTACT_PHONE = v.slice(8);
        }
        try{ PC.switchView('contacts'); }catch(_){}
      } else try{ PC.switchView(v); }catch(_){}
    };
    if(window.__PC_BOOTED){ go(); return; }
    document.addEventListener('pc-app-ready', go, { once:true });
  }

  function plug(method){
    try{ return PC && PC.capPlugin ? PC.capPlugin('HomeScreen', method) : null; }catch(_){ return null; }
  }

  /* THE THEME, PUSHED ACROSS THE BOUNDARY. Called from app.js's applyTheme on every change AND every
   * preview. Fire-and-forget on purpose: the theme has already been applied to this page, and a
   * failure to mirror it must never turn into an error on the settings screen. */
  function mirrorTheme(slug){
    const P = plug('setTheme');
    if(!P) return;
    try{ const r = P.setTheme({ slug: String(slug||'') }); if(r && r.catch) r.catch(()=>{}); }catch(_){}
  }

  /* WHICH SCREEN A HOME-SCREEN TILE ASKED FOR.
   *
   * The tile starts the app with an extra; the native side hands it over exactly once and then
   * forgets it, so a later resume does not jump to Notes again. Read at boot and on every resume,
   * because a tile pressed while the app is already running arrives through onNewIntent and there is
   * no page load to hang it off.
   *
   * It goes through switchView, which is the same function the sidebar uses — including its instance
   * gating, so a tile for a server-backed screen on a server-less install lands where that gating
   * sends it rather than on a blank page. */
  async function consumeLaunchView(){
    const P = plug('consumeLaunchView');
    if(!P) return '';
    let v = '';
    try{ v = ((await P.consumeLaunchView()) || {}).view || ''; }catch(_){ return ''; }
    if(!v) return '';
    /* THE PLAYER IS NOT A VIEW. app.js's own More menu spells it `__music` and opens it with
     * `openMusic()`; `switchView('__music')` would fall through to the default screen, which is
     * exactly what "clicking play on music widget opens up default posterchan app page instead of
     * music" looked like from the other end. One name, used by both. */
    if(v === '__music'){
      landView(v); return v;
    }
    landView(v);
    return v;
  }

  async function status(){
    const P = plug('status');
    if(!P) return null;
    try{ return await P.status(); }catch(_){ return null; }
  }

  /* THE SETTINGS CARD. Rendered into #phone-shell by app.js's renderSettings, and only on a build
   * that has the plugin — so this whole feature is invisible in a browser and on an APK older than
   * it, rather than being a row of switches that refuse. */
  async function renderSettings(host){
    if(!host) return;
    const st = await status();
    if(!st){ host.innerHTML = ''; return; }
    const enc = PC.enc;
    const yes = (b) => b ? 'yes' : 'no';
    host.innerHTML = `
      <section class="set-card">
        <div class="set-head"><div>
          <div class="set-title"><svg class="ic b-ic" aria-hidden="true"><use href="#i-home"></use></svg>Use PosterChan as your phone</div>
          <div class="muted small">Optional. PosterChan can be this phone's home screen, its messages
            app and its phone app — each one separately, each one given back the same way, in
            Android's own dialog. Nothing changes until you ask.</div>
        </div></div>
        <div class="set-body">
          <label class="set-stay"><input type="checkbox" id="ps-home"${st.isDefaultHome?' checked':''}>
            Home screen</label>
          <div class="muted small" id="ps-home-note">The app grid is drawn by Android, not by this
            page, so it keeps working if the app itself does not. Your apps and PosterChan's screens
            sit side by side; long-press the wallpaper to choose which.</div>

          <label class="set-stay" style="margin-top:12px"><input type="checkbox" id="ps-sms"${st.isDefaultSms?' checked':''}>
            Messages (SMS &amp; MMS)</label>
          <div class="muted small" id="ps-sms-note">Texts stay in the phone's own message store, so
            nothing else on the phone loses them, and an encrypted copy of each one goes to your relay
            so you can read and answer them from <a href="#" id="ps-texts">Texts</a> on any of your
            devices. Picture messages are downloaded through the carrier when PosterChan holds the
            Messages role; encrypted originals and small previews are mirrored to your Blossom
            <strong>MMS</strong> folder.</div>

          <label class="set-stay" style="margin-top:12px"><input type="checkbox" id="ps-dialer"${st.isDefaultDialer?' checked':''}>
            Phone (calls over the mobile network)</label>
          <div class="muted small">Different from PosterChan's own Calls screen, which is a call over
            the internet to another Nostr user. This one is the cellular dialer.</div>

          <div class="muted small" style="margin-top:12px">
            Battery: these roles are what let Android keep PosterChan running properly in the
            background — a home screen is never a background app, and the messages and phone roles are
            grounds for a battery exemption rather than a guess.
            Doze exemption: <strong>${yes(st.batteryExempt)}</strong>.
          </div>
          <div class="muted small" style="margin-top:12px">
            Weather widget temperature:
            <label class="set-stay" style="display:inline-block;margin-left:8px"><input type="radio"
              name="ps-units" id="ps-unit-f" value="imperial"${unitsPref()==='imperial'?' checked':''}>
              \u00b0F</label>
            <label class="set-stay" style="display:inline-block;margin-left:8px"><input type="radio"
              name="ps-units" id="ps-unit-c" value="metric"${unitsPref()==='metric'?' checked':''}>
              \u00b0C</label>
            <div class="muted small">Starts from this phone\u2019s region and stays wherever you put it.</div>
          </div>
          <div class="muted small" id="ps-msg" style="margin-top:8px"></div>
          <button class="btn small" id="ps-defaults" hidden style="margin-top:6px">Open Android\u2019s Default apps</button>
        </div>
      </section>`;

    const $ = (s) => host.querySelector(s);
    const msg = (t) => { const m = $('#ps-msg'); if(m) m.textContent = t || ''; };

    host.querySelectorAll('input[name="ps-units"]').forEach(r => {
      r.onchange = () => { if(r.checked) setUnits(r.value); };
    });

    async function refresh(){
      const s = await status();
      if(!s) return;
      const set = (id, on) => { const b = $(id); if(b) b.checked = !!on; };
      set('#ps-home', s.isDefaultHome);
      set('#ps-sms', s.isDefaultSms);
      set('#ps-dialer', s.isDefaultDialer);
      return s;
    }

    /* Every switch re-reads the platform afterwards rather than trusting the checkbox. A role can be
     * refused in the dialog, granted by another route, or taken away in Settings while this page is
     * open; a switch showing a state the phone is not in is worse than no switch.
     *
     * AND IT SAYS WHAT HAPPENED WHEN THE ROLE DID NOT ARRIVE. That is the bug this shape was reported
     * for — "sms does nothing when checked". Android refuses a role the app cannot hold by starting
     * the request activity and finishing it immediately with RESULT_CANCELED: no dialog, no error, no
     * log. The switch flipped, nothing appeared, and it flipped back, which is exactly what a switch
     * that was never wired up does. Now the state is compared before and after, and a request that
     * did not take is named and offered Android's own Default apps screen — which on an OEM build
     * that suppresses the role dialog is the only route there is. */
    function wire(id, ask, method, holds, capable, what){
      const box = $(id);
      if(!box) return;
      box.onchange = async () => {
        const want = box.checked;
        const P = plug(method);
        if(!P){ box.checked = !want; msg('this build has no phone-shell support'); return; }
        if(want && capable === false){
          box.checked = false;
          /* NAME THE MISSING PART. "it is impossible to check Messages in User Settings -> Phone,
           * nothing happend" — this branch's old answer was "update the app and try again", which
           * is advice nobody can act on and is wrong whenever the build is already current. Android
           * demands four components before it will offer the SMS role; `status` reports each one,
           * so the switch can say which is absent instead of shrugging. */
          const parts = (st && st.smsParts) || null;
          const gone = parts ? Object.keys(parts).filter(k => !parts[k]) : [];
          msg('Android will not offer PosterChan as your ' + what + ': '
            + (gone.length ? 'this build is missing ' + gone.join(', ')
                           : 'it does not qualify on this phone')
            + '. Open Android Default apps to review the available choices.');
          const b = $('#ps-defaults'); if(b) b.hidden = false;
          return;
        }
        try{
          if(want){ await P[method](); }
          else { await ask(); return void await refresh(); }
        }catch(e){ msg('could not change it: ' + ((e && (e.message||e.errorMessage)) || 'refused')); }
        /* RE-READ, ONCE MORE, A MOMENT LATER. Granting a role is asynchronous on the system side:
           the native half already waits for it to settle, and this is the second net under it for a
           phone that takes longer than that. Without it the switch springs back on a role that was
           in fact granted, and Android's own settings screen disagrees with ours. */
        let after = await refresh();
        if(want && after && !after[holds]){
          await new Promise(r => setTimeout(r, 900));
          after = await refresh();
        }
        if(want && after && !after[holds]){
          /* SAID LOUDLY, because "nothing happened" is what this looked like. Android refuses a role
           * by starting the request activity and finishing it immediately with RESULT_CANCELED — no
           * dialog, no error, nothing in any log — which from here is indistinguishable from a
           * switch that is not wired up. */
          msg('Android did not hand over the ' + what + ' role — it refused, or the dialog was '
            + 'dismissed. Use the button below, which always works.');
          const b = $('#ps-defaults');
          if(b) b.hidden = false;
        } else { msg(''); const b = $('#ps-defaults'); if(b) b.hidden = true; }
      };
    }

    { const b = $('#ps-defaults'); if(b) b.onclick = () => {
        const P = plug('openDefaultApps');
        if(P) P.openDefaultApps().catch(() => msg('this phone has no Default apps screen'));
      }; }

    /* COMING BACK FROM ANDROID'S OWN SCREEN. The role dialog and the Default apps screen both take
       the person out of this app entirely, and whatever they did there is invisible to us until we
       look again. Without this the switches show whatever was true when the pane was drawn. */
    if(!renderSettings._resumeBound){
      renderSettings._resumeBound = true;
      document.addEventListener('visibilitychange', () => {
        if(document.visibilityState !== 'visible') return;
        const host = document.querySelector('#phone-shell');
        if(host && host.querySelector('#ps-home')) renderSettings(host);
      });
    }

    wire('#ps-home', async () => {
      const P = plug('disableLauncher');
      const r = P ? await P.disableLauncher() : null;
      /* THE ONE REFUSAL THAT MATTERS. Turning this off disables the home component, and doing that
       * while PosterChan is the ONLY home app on the phone leaves the device with no home screen at
       * all — HOME does nothing and there is no way to install one. The native side refuses; this is
       * where the person is told why, rather than a switch that quietly springs back. */
      if(r && r.released === false){
        msg('Kept as your home screen: ' + (r.reason || 'there is no other home app on this phone') +
            '. Install another launcher first, or change it in Android Settings → Default apps.');
      } else { msg(''); }
    }, 'enableLauncher', 'isDefaultHome', true, 'home screen');

    { const t = $('#ps-texts'); if(t) t.onclick = (e) => { e.preventDefault(); PC.switchView('texts'); }; }

    wire('#ps-sms', async () => {
      msg('Android has no way for an app to give up the messages role by itself — ' +
          'choose a different messages app in Android\u2019s Default apps screen.');
      const b = $('#ps-defaults'); if(b) b.hidden = false;
    }, 'requestSms', 'isDefaultSms', st.smsCapable, 'messages app');

    wire('#ps-dialer', async () => {
      msg('Android has no way for an app to give up the phone role by itself — ' +
          'choose a different phone app in Android\u2019s Default apps screen.');
      const b = $('#ps-defaults'); if(b) b.hidden = false;
    }, 'requestDialer', 'isDefaultDialer', st.dialerCapable, 'phone app');
  }

  /* THE ONE FACT THE WEATHER WIDGET CANNOT WORK OUT FOR ITSELF.
   *
   * That widget is drawn by the LAUNCHER's process, which has no session and no localStorage, and
   * the instance is chosen at runtime by the person — one bundle serves every instance, so it
   * cannot be baked into the APK. So the base URL is mirrored across, exactly as the theme is.
   *
   * AN EMPTY BASE IS A REAL ANSWER, not a failure to send one: on a standalone install the widget
   * says "weather needs your PosterChan server", which is a different sentence from "no location
   * yet" and from "no network". Weather is the one feature here that genuinely needs the server —
   * the forecast is proxied so the upstream never sees a user.
   *
   * Fire-and-forget, like mirrorTheme: nothing on this page depends on it. */
  /* WHICH TEMPERATURE SCALE — AND IT IS NOT ALWAYS CELSIUS.
   *
   * "weather widget is in Celcius, i am in the US". `pc_units` was READ here and written by nothing,
   * anywhere, so the fallback was the answer for every person on every device: a hardcoded
   * 'metric', with no switch and no way to reach it. A default that cannot be changed is not a
   * default, it is a decision made on somebody else's behalf.
   *
   * The device already knows. `Intl.DateTimeFormat().resolvedOptions().locale` is the region the
   * phone is actually set to, and the set of places that give weather in Fahrenheit is small,
   * closed and easy to name — so the default is derived rather than assumed, and the stored
   * preference always wins over it. A person who has chosen is never re-guessed at.
   *
   * Region, never LANGUAGE: `en` is spoken in Britain, Australia and India, all of which report the
   * weather in Celsius, and guessing from the language would put half of them on Fahrenheit. */
  const FAHRENHEIT = ['US', 'BS', 'BZ', 'KY', 'FM', 'LR', 'MH', 'PW', 'PR', 'GU', 'VI', 'AS', 'MP'];

  function regionOf(){
    try{
      const l = (Intl.DateTimeFormat().resolvedOptions().locale
                || navigator.language || '').toUpperCase();
      // en-US, en-Latn-US, und-US-u-ca-gregory — take the two-letter region wherever it sits.
      const parts = l.split(/[-_]/);
      for(let i = 1; i < parts.length; i++){
        if(/^[A-Z]{2}$/.test(parts[i])) return parts[i];
      }
    }catch(_){}
    return '';
  }

  /* 'imperial' or 'metric'. A stored answer wins; otherwise the phone's own region decides. */
  function unitsPref(){
    try{
      const v = localStorage.getItem('pc_units');
      if(v === 'imperial' || v === 'metric') return v;
    }catch(_){}
    return FAHRENHEIT.indexOf(regionOf()) >= 0 ? 'imperial' : 'metric';
  }

  /* Chosen deliberately — stored, and pushed to the widget straight away so the change is visible on
   * the home screen rather than at the next forecast. */
  function setUnits(v){
    const want = v === 'imperial' ? 'imperial' : 'metric';
    try{ localStorage.setItem('pc_units', want); }catch(_){}
    syncWeather();
    return want;
  }

  function syncWeather(){
    let P = null;
    try{ P = PC && PC.capPlugin ? PC.capPlugin('Weather', 'sync') : null; }catch(_){ return; }
    if(!P) return;
    let base = '';
    try{
      base = (typeof window.__PC_API_BASE__ !== 'undefined' ? (window.__PC_API_BASE__ || '')
                                                           : location.origin) || '';
    }catch(_){ base = ''; }
    const units = unitsPref();
    try{ const r = P.sync({ base: String(base).replace(/\/+$/, ''), units });
         if(r && r.catch) r.catch(()=>{}); }catch(_){}
  }

  function init(){
    PC = window.__PC;
    if(!PC){ return setTimeout(init, 50); }
    // The theme is applied before this module loads (app.js paints the cached pc_theme pre-boot), so
    // mirror the current one once on arrival — otherwise a phone whose theme has not changed since
    // the app was updated shows native screens in the flagship theme for ever.
    try{ mirrorTheme(localStorage.getItem('pc_theme') || 'cyberpunk'); }catch(_){}
    syncWeather();

    /* THE LANDING RUNS AFTER BOOT, NEVER INSIDE IT — and that is not caution, it is a scar.
     *
     * A speculative boot-landing guard (`_viewChosen`) once shipped and BROKE the APK, because
     * applyInstanceGating can switchView during boot and the guard made the landing skip itself. A
     * home-screen tile calling switchView while boot is still choosing a screen is the same mistake
     * wearing a different hat. So: wait until the app is plainly up (#feed drawn), then navigate —
     * at which point it is an ordinary navigation, identical to tapping the sidebar. */
    const land = () => {
      if(!document.querySelector('#feed')) return setTimeout(land, 250);
      consumeLaunchView();
    };
    setTimeout(land, 600);

    /* A TILE PRESSED WHILE THE APP IS ALREADY RUNNING HAS NO PAGE LOAD TO HANG THE READ OFF, and
     * THREE things are listened to rather than one — because the obvious one is the unreliable one.
     *
     * `visibilitychange` is the page's own signal and on Android it arrives late or is coalesced
     * away entirely; that is measured, and it is the same lesson that cost the timeline a release
     * (see `_animOff` / `_tlForeground` in CLAUDE.md — the class was armed and released from
     * visibilitychange alone and the resume path drew inside the gap). Capacitor's `resume` fires
     * from the ACTIVITY, which Android never freezes, so it arrives when the WebView's own event
     * does not. And the native `launchView` event is pushed by `MainActivity.onNewIntent` itself,
     * which is the moment the press actually lands.
     *
     * Listening to all three costs nothing: `LaunchView.take()` CONSUMES, so whichever fires first
     * performs the navigation and every later one reads "" and does nothing. */
    const again = () => { if(document.querySelector('#feed')) consumeLaunchView(); };
    /* onNewIntent knows the requested view at the exact instant Android delivers the tile press.
     * Use that payload directly, then drain the parked copy. Waiting for a resume/visibility race
     * made the old screen (often Notifications) win on an already-running app. The parked carrier
     * remains the cold-start fallback, where no JS listener exists yet. */
    const launched = (e) => {
      const v = e && typeof e.view === 'string' ? e.view.trim() : '';
      if(v && document.querySelector('#feed')){
        landView(v);
      }
      consumeLaunchView();                 // consume the duplicate parked/intent carrier exactly once
    };
    document.addEventListener('visibilitychange', () => {
      if(document.visibilityState === 'visible') again();
    });
    try{
      const A = PC.capPlugin ? PC.capPlugin('App', 'addListener') : null;
      if(A && A.addListener) A.addListener('resume', again);
    }catch(_){}
    try{
      const H = plug('addListener');
      if(H && H.addListener) H.addListener('launchView', launched);
    }catch(_){}
  }
  init();

  window.PCPhone = { mirrorTheme, syncWeather, consumeLaunchView, status, renderSettings,
                     unitsPref, setUnits, regionOf };
})();
