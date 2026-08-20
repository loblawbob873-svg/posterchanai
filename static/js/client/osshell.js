/* The PosterChanOS surface: a launcher, a taskbar of real windows, and the system panel.
 *
 * This is the half that only exists when PosterChan IS the desktop. Everywhere else — a browser
 * tab, the APK, the desktop app on somebody's Windows machine — none of these bridges exist, and
 * the whole module must then be ABSENT rather than broken: `available()` answers no and nothing
 * else here is called. That is the same rule the bridges themselves follow, and it is what lets one
 * client be an app on one machine and an operating system on another.
 *
 * WHAT IS PURE LIVES HERE AND IS TESTED; what touches the DOM is deliberately thin. The judgement
 * in a shell is not in its markup — it is in which windows belong on a taskbar, what a launcher
 * does when the app is already running, and what the panel says when it cannot read something.
 */
(function(root){
  'use strict';

  const WM = () => root.pcWM || null;
  const NET = () => root.pcNet || null;
  const POWER = () => root.pcPower || null;
  const AUDIO = () => root.pcAudio || null;
  const OS = () => root.pcOS || null;

  /* IS POSTERCHAN THE DESKTOP ON THIS MACHINE? ABSENT UNTIL PROVEN PRESENT.
   *
   * This used to be "the bridge exists", and the bridge exists in the desktop app on EVERY platform
   * — the preload exposes pcWM on Windows and macOS as readily as on Linux, because a preload
   * cannot know what it is running on top of. So the OS shell rendered itself on a Windows desktop:
   * a launcher for programs that are not there, a taskbar of compositor windows that cannot exist,
   * and a system tray reporting a machine nothing had asked. Reported as "I see PosterChan OS
   * toolbars on the windows app now, that should not happen".
   *
   * The honest question is not whether a bridge is present but whether a COMPOSITOR ANSWERS, and
   * only a real call can settle that. So the answer starts as no — the safe default everywhere this
   * is not an operating system — and `detect()` upgrades it once, after the compositor has actually
   * replied. Every entry point awaits that, so nothing is drawn on a guess. */
  let _have = null;                 // null = never asked · false = no compositor · true = one answered
  const available = () => _have === true;

  async function detect(){
    if(_have !== null) return _have;
    const wm = WM();
    if(!wm || typeof wm.windows !== 'function'){ _have = false; return false; }
    try{ _have = Array.isArray(await wm.windows()); }
    catch(_){ _have = false; }     // no socket, wrong platform, nothing listening — all the same answer
    return _have;
  }

  /* THE APPS A LAUNCHER OFFERS. Deliberately a short, fixed list rather than a scan of .desktop
   * files: this is the shell of an appliance, and a menu of ninety entries scraped from
   * /usr/share/applications is the thing PosterChanOS exists not to be. Anything else is started
   * from the terminal, which is one of the entries. */
  /* `match` is what the app's WINDOW is called, which is not what the launcher entry is called —
   * "Browser" opens firefox, and a launcher that looks for a window named "browser" never finds the
   * one it just started. It is also why this is a field rather than the id: the two are different
   * facts and conflating them makes the already-open check silently never fire. */
  /* CANDIDATE COMMAND LINES, NOT ONE PATH. Gentoo installs www-client/firefox-bin as
   * `/usr/bin/firefox-bin` and `/opt/firefox/firefox`, and NOT as `/usr/bin/firefox` — so a
   * launcher naming the obvious path starts nothing, silently, on the one machine it was written
   * for. Each candidate is a WHOLE argv rather than a path plus shared arguments, because Steam is
   * either a binary or `flatpak run com.valvesoftware.Steam` and those do not share a shape. The
   * main process takes the first whose program exists, and says so when none does — which is the
   * honest answer for Steam, an optional install on this profile.
   *
   * `match` is what the app's WINDOW is called, which is a different fact again: "Browser" opens
   * firefox, and looking for a window named "browser" never finds the one it just started. */
  const APPS = [
    { id: 'browser',  name: 'Browser',  match: 'firefox', icon: 'globe', candidates: [
        ['/usr/bin/firefox'], ['/usr/bin/firefox-bin'], ['/opt/firefox/firefox'] ] },
    /* THE TERMINAL IS POSTERCHAN'S OWN, not somebody else's emulator. It is a PTY on THIS machine
     * through the desktop bridge — no SSH, no server, nothing to reach over a network to get a
     * shell on the computer you are sitting at — and its history is ephemeral Nostr events, so it
     * follows you between your own devices and is stored by nobody. Launching `foot` here would
     * throw all of that away and open a terminal that knows none of it.
     *
     * `foot` still exists on the machine and is still bound to $mod+Return in the compositor, and
     * that is deliberate: it is the escape hatch for when the shell itself is what has gone wrong,
     * which is exactly when a terminal drawn BY the shell cannot help you. */
    { id: 'terminal', name: 'Terminal', icon: 'terminal', view: 'terminal' },
    { id: 'steam',    name: 'Steam',    match: 'steam', icon: 'gamepad', candidates: [
        ['/usr/bin/steam'], ['/usr/bin/flatpak', 'run', 'com.valvesoftware.Steam'] ] },
  ];

  /* ── THE MACHINE'S OWN APPLICATIONS ────────────────────────────────────────────────────────
   *
   * The list above is three entries and a comment arguing that a menu scraped from
   * /usr/share/applications is the thing PosterChanOS exists not to be. That argument was about a
   * menu of ninety unusable entries, and the answer to it is the SPEC — NoDisplay, Hidden,
   * NotShowIn, TryExec, Type — not refusing to look. Measured on the test laptop: 19 .desktop files
   * of which five belong in a menu, and every one of the other fourteen says so in its own file.
   *
   * A desktop you cannot start your own programs from is not a desktop. "Should be able to
   * manage/open any game/app under PosterChan Desktop" is the requirement, and each one gets a
   * PosterChan window like everything else here, because that is what `adoptAll` does with any
   * compositor window that appears.
   *
   * CACHED FOR THE SESSION, and refreshed only when asked. A scan reads and parses every .desktop
   * file on the machine, and the start menu is rebuilt on every keystroke of its search box. */
  let _apps = null, _appsAt = 0, _appsInFlight = null;

  async function machineApps(force){
    const A = root.pcApps;
    if(!A || typeof A.list !== 'function') return [];
    if(!force && _apps) return _apps;
    /* SHARED, not repeated. The start menu draws while it types and the desktop redraws on window
     * events, so without this a slow disk gets one full scan per repaint. */
    if(_appsInFlight) return _appsInFlight;
    _appsInFlight = (async () => {
      try{
        const r = await A.list();
        _apps = ((r && r.apps) || []).map(a => ({
          id: 'app:' + a.id, name: a.name, match: a.match, icon: 'window',
          comment: a.comment || '', group: a.group || 'Other', argv: a.argv, machine: true,
        }));
        _appsAt = Date.now();
      }catch(_){ _apps = _apps || []; }
      _appsInFlight = null;
      return _apps;
    })();
    return _appsInFlight;
  }

  /* WHAT THE LAUNCHER OFFERS: the built-ins first, then the machine's own, with anything the
   * built-ins already name removed — `firefox` is both "Browser" here and `firefox-bin.desktop`
   * there, and a menu with it twice under two names is a menu somebody stops trusting. Matched on
   * the WINDOW name, which is the only thing the two lists agree about. */
  function mergedApps(builtins, machine){
    const claimed = [...new Set((builtins || []).map(a => String(a.match || a.id || '').toLowerCase())
                                                .filter(Boolean))];
    /* THE SAME PROGRAM UNDER TWO NAMES, and an exact match does not find it. Measured on the test
     * laptop: the built-in Browser matches `firefox`, and Gentoo's entry is `firefox-bin.desktop`
     * whose program is `firefox-bin` — so an `===` comparison saw two different apps and the menu
     * offered "Browser" and "Mozilla Firefox (bin)", which start the same browser.
     *
     * A PREFIX AT A SEPARATOR, not `includes`. `firefox-bin` and `firefox.real` are firefox;
     * `steamlink` is not steam, and a containment test would swallow it — silently, since the
     * symptom is a program that is installed and simply never appears. */
    const sameApp = (m, want) => m === want
      || m.startsWith(want + '-') || m.startsWith(want + '.') || m.startsWith(want + '_');
    const out = (builtins || []).filter(a => !a.hidden).slice();
    for(const a of (machine || [])){
      const m = String(a.match || '').toLowerCase();
      if(m && claimed.some(w => sameApp(m, w) || sameApp(w, m))) continue;
      /* …and by NAME too, since a built-in's `match` is the binary and an entry's Name is what a
       * person reads: two rows both reading "Steam" is the same problem by a different route. */
      if(out.some(b => String(b.name || '').toLowerCase() === String(a.name || '').toLowerCase())) continue;
      out.push(a);
    }
    return out;
  }

  /** Everything the start menu should show, built-ins and installed programs together. */
  async function allApps(force){
    return mergedApps(APPS, await machineApps(force));
  }

  /* WHICH WINDOWS BELONG ON A TASKBAR. Not the shell's own window — it is the desktop, and a
   * desktop that lists itself is a mirror pointed at a mirror. Nothing without a title either: a
   * window that has not named itself yet is one that is still opening, and a nameless button
   * appearing and renaming itself a second later is worse than one that arrives late. */
  function taskbarRows(windows){
    const rows = [];
    for(const w of (windows || [])){
      const app = String(w.app || '');
      if(!app || /^posterchan(-desktop)?$/.test(app)) continue;
      const title = String(w.title || '').trim();
      if(!title) continue;
      rows.push({ id: w.id, app, title, focused: !!w.focused, xwayland: !!w.xwayland,
                  /* The label a person recognises: the window's own title, which is the page or the
                   * document — the app name is what the ICON says. */
                  label: title.length > 48 ? title.slice(0, 47) + '…' : title });
    }
    return rows;
  }

  /* LAUNCHING SOMETHING THAT IS ALREADY OPEN FOCUSES IT. Every desktop does this and the reason is
   * the same everywhere: a second browser window is almost never what "Browser" meant, and a person
   * who wanted one can ask the browser for it. */
  function existingWindow(windows, app){
    const want = String((app && (app.match || app.id)) || '').toLowerCase();
    if(!want) return null;
    for(const w of (windows || [])){
      const id = String(w.app || '').toLowerCase();
      if(id === want || id.indexOf(want) >= 0) return w;
    }
    return null;
  }

  /* How the shell opens one of the client's OWN screens. Registered by os.js, which is the half
   * that knows what a window is here; absent in a test, where "it asked to open the terminal" is
   * the whole assertion. */
  let _openView = null;
  const setViewOpener = (fn) => { _openView = (typeof fn === 'function') ? fn : null; };

  async function launch(appId){
    let app = APPS.find(a => a.id === appId);
    /* A MACHINE APP IS FOUND IN THE SCAN, not in the built-in list. Looked up from the CACHE rather
     * than re-scanned: the id came from a menu that was drawn from that same cache moments ago, and
     * a fresh scan here would be a disk full of files parsed between the press and the program. */
    if(!app && String(appId || '').startsWith('app:'))
      app = (_apps || []).find(a => a.id === appId);
    if(!app) throw new Error('no such app');
    /* A VIEW APP IS NOT A PROCESS. There is nothing to spawn and nothing to wait for a window from
     * — "launch" means open the screen, in a window on this desktop like every other app. */
    if(app.view){
      if(!_openView) return { why: 'this desktop cannot open ' + app.name + ' here' };
      try{ _openView(app.view); return { view: app.view }; }
      catch(e){ return { why: String((e && e.message) || e) }; }
    }
    const wm = WM(); if(!wm) throw new Error('no compositor here');
    let open = null;
    try{ open = existingWindow(await wm.windows(), app); }catch(_){}
    if(open){ await wm.focus(open.id); return { focused: open.id }; }
    /* CANDIDATES vs a resolved ARGV. A built-in names several possible command lines because it
     * cannot know how this distribution installed the program; a scanned entry already carries the
     * one argv its .desktop file names, resolved against this disk when it was listed. Passing an
     * argv as a candidate list would try to exec its first WORD as a whole command line. */
    const r = app.argv
      ? await wm.launch(app.argv, { waitMs: 20000 })
      : await wm.launch(app.candidates, { waitMs: 20000, candidates: true });
    /* A launch that produced no window is REPORTED, not swallowed. The most common cause is the
     * program not being installed — Steam is optional here — and "nothing happened" is the least
     * useful thing a launcher can say. */
    if(!r || !r.window) return { started: r && r.pid, window: null,
                                 why: app.name + ' did not open — is it installed?' };
    return { started: r.pid, window: r.window.id };
  }

  /* THE PANEL. One read, because four separate polls against four subsystems on a laptop is four
   * chances to be half-updated — and a panel showing yesterday's battery beside today's volume is
   * the sort of thing people stop trusting entirely. */
  async function panelState(){
    const out = { os: available() };
    const jobs = [];
    if(NET()) jobs.push(NET().status().then(s => { out.net = s; }, () => { out.net = null; }));
    if(POWER()) jobs.push(POWER().status().then(s => { out.power = s; }, () => { out.power = null; }));
    if(AUDIO()) jobs.push(AUDIO().status().then(s => { out.audio = s; }, () => { out.audio = null; }));
    /* TOR, when this build has it. On PosterChanOS it is the switch that decides how every byte
     * leaves the machine, and it lived in a menu bar the shell hides and a tray icon sway does not
     * draw — so on the one platform where it matters most it was the hardest thing here to reach. */
    const sh = root.pcShell;
    if(sh && sh.tor) jobs.push(sh.tor.status().then(s => { out.tor = s; }, () => { out.tor = null; }));
    await Promise.all(jobs);
    return out;
  }

  /* WHAT THE PANEL SAYS, INCLUDING WHEN IT CANNOT SAY ANYTHING. A subsystem that could not be read
   * shows as unknown rather than as a plausible default — a wifi icon at full strength on a machine
   * whose NetworkManager is dead is a lie that costs somebody an hour. */
  function panelSummary(state){
    const s = state || {};
    const net = !s.net ? { text: 'network unknown', known: false }
              : !s.net.online ? { text: 'offline', known: true }
              : { text: s.net.name || s.net.kind || 'online', known: true,
                  signal: s.net.kind === 'wifi' ? s.net.signal : null };
    const bat = !s.power || !s.power.battery ? { known: false }
              : !s.power.battery.present ? { known: true, present: false }
              : { known: true, present: true, percent: s.power.battery.percent,
                  charging: !!s.power.battery.charging };
    const vol = !s.audio || !s.audio.output || s.audio.output.percent == null
              ? { known: false }
              : { known: true, percent: s.audio.output.percent, muted: !!s.audio.output.muted };
    const bright = !s.power || !s.power.brightness || !s.power.brightness.available
                 ? { known: false }
                 : { known: true, percent: s.power.brightness.percent };
    /* ABSENT is not OFF. A build with no bundled tor has nothing to switch, and drawing an "off"
     * chip there is a control that can never turn anything on. */
    const tor = !s.tor ? { present: false }
              : { present: true, on: !!s.tor.enabled,
                  bootstrapped: Number(s.tor.bootstrapped || 0),
                  country: s.tor.country || '', countryName: s.tor.countryName || '' };
    return { net, battery: bat, volume: vol, brightness: bright, tor,
             canHibernate: !!(s.power && s.power.canHibernate) };
  }

  /* THE POWER MODES THIS MACHINE OFFERS, out of whatever `power.js` answered.
   *
   * They are an OBJECT — `{ available, kind, list, active }` — and the panel read them as an ARRAY.
   * An object has no `.length`, so the profile row was never drawn and a laptop whose kernel was
   * reporting `low-power balanced performance` the whole time offered no power modes at all.
   * Nothing threw: the reading was right and the panel could not show it. Reported as "clicking on
   * battery should let me change power mode".
   *
   * Pure, and here rather than inside the popover, because that is the only way this can be tested
   * — the failure is a `.length` on the wrong kind of value, which draws an empty row perfectly.
   * The array branch is kept because "a list of profiles" is what the name says and a future bridge
   * answering the obvious thing must not silently draw nothing again. */
  function profileMenu(status){
    const pf = status && status.profiles;
    if(Array.isArray(pf)) return { list: pf, active: (status && status.profile) || '' };
    if(pf && Array.isArray(pf.list) && pf.available !== false)
      return { list: pf.list, active: pf.active || '' };
    return { list: [], active: '' };
  }

  /** Provision the Unix account for whoever just signed in. Idempotent; safe on every sign-in. */
  async function ensureAccount(npub){
    const os = OS(); if(!os) return { ok: false, why: 'not PosterChanOS' };
    try{ return await os.provision(String(npub || '')); }
    catch(e){ return { ok: false, why: String((e && e.message) || e) }; }
  }

  /* ── THE VISIBLE HALF ─────────────────────────────────────────────────────────────────────────
   *
   * Deliberately small. Everything above decides; this draws. It is also the only part that cannot
   * be tested here, so it is kept to the point where reading it is enough — anything that needed
   * thinking about was moved up.
   */
  const H = (s) => String(s == null ? '' : s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');

  function panelHTML(sum){
    const s = sum || {};
    const bits = [];
    /* Every one says UNKNOWN out loud rather than showing a plausible default. A dash is not a
     * reading; it is the panel admitting it could not take one. */
    bits.push(s.net.known
      ? `<button class="os-chip" data-os="net" title="Network">${s.net.signal != null
          ? H(s.net.signal) + '%' : ''} ${H(s.net.text)}</button>`
      : `<button class="os-chip os-unknown" data-os="net" title="The network could not be read">network ?</button>`);
    if(s.volume.known)
      bits.push(`<button class="os-chip" data-os="vol" title="Volume">${s.volume.muted
        ? 'muted' : H(s.volume.percent) + '%'}</button>`);
    if(s.brightness.known)
      bits.push(`<button class="os-chip" data-os="bright" title="Brightness">${H(s.brightness.percent)}%</button>`);
    if(s.tor && s.tor.present)
      /* The BOOTSTRAP PERCENTAGE while it is coming up, because "on" and "on and actually usable"
       * are minutes apart on a slow circuit and the difference is every page failing to load. */
      bits.push(`<button class="os-chip${s.tor.on ? ' on' : ' os-off'}" data-os="tor"
          title="${s.tor.on ? 'Tor is on' : 'Tor is off'}">🧅${s.tor.on
            ? (s.tor.bootstrapped >= 100 ? '' : ' ' + H(s.tor.bootstrapped) + '%') : ' off'}</button>`);
    /* THE BATTERY IS A BUTTON, and it opens the POWER popover — the one that carries the profiles.
     * It was a `<span>`: a reading, with the thing you actually want to change from it (balanced /
     * power-saver / performance) hidden behind a separate ⏻ two chips along. Asked for as "clicking
     * on battery should let me change power mode", which is where everybody looks for it, because a
     * battery percentage and how fast it is being spent are one subject. */
    if(s.battery.known && s.battery.present)
      bits.push(`<button class="os-chip" data-os="power" title="Battery and power mode">`
        + `${H(s.battery.percent)}%${s.battery.charging ? ' ⚡' : ''}</button>`);
    /* Kept beside it, and it is the ONLY one on a desktop with no battery — which is most of them. */
    bits.push(`<button class="os-chip" data-os="power" title="Power">⏻</button>`);
    return `<div class="os-panel">${bits.join('')}</div>`;
  }

  /* ── THE PANEL'S CHIPS ACTUALLY DO SOMETHING ───────────────────────────────────────────────
   *
   * They did not. `render` bound `[data-app]` and `[data-win]` and nothing else, so the network,
   * volume, brightness and power chips were painted, looked like buttons, and were decoration:
   * "nothing happens when you click on the wifi bar and power button bar". A control that reports a
   * reading and refuses to change it is worse than no control, because it looks like the feature is
   * there.
   *
   * Each opens a small popover anchored to its own chip. Deliberately not a settings screen: this
   * is the thing you reach for one-handed to join a network or turn the volume down, and it closes
   * as soon as you have.
   */
  let _pop = null;
  function closePop(){
    if(_pop){ try{ _pop.remove(); }catch(_){} _pop = null; }
    if(_popOff){ document.removeEventListener('pointerdown', _popOff, true); _popOff = null; }
  }
  let _popOff = null;
  function openPop(anchor, html){
    closePop();
    const d = document.createElement('div');
    d.className = 'os-pop';
    d.innerHTML = html;
    document.body.appendChild(d);
    /* Anchored to the chip and kept ON SCREEN: the tray is at the right-hand end of the taskbar, so
     * a popover laid out from the chip's left edge hangs off the display. */
    try{
      const r = anchor.getBoundingClientRect();
      const w = d.offsetWidth || 280, h = d.offsetHeight || 200;
      const x = Math.max(8, Math.min(window.innerWidth - w - 8, r.left + r.width / 2 - w / 2));
      const y = Math.max(8, r.top - h - 10);
      d.style.left = Math.round(x) + 'px';
      d.style.top = Math.round(y) + 'px';
    }catch(_){}
    /* Closed by a press ANYWHERE else, captured — a click inside a window would otherwise leave it
     * open behind whatever the person went on to do. */
    _popOff = (e) => { if(_pop && !_pop.contains(e.target)) closePop(); };
    setTimeout(() => document.addEventListener('pointerdown', _popOff, true), 0);
    _pop = d;
    return d;
  }

  const toast = (m) => { try{ root.PC && root.PC.toast(m); }catch(_){} };

  /* NETWORK. A list of what is in range, strongest first, with the one we are on marked. Joining
   * asks for a password only when the network wants one — and through the app's own prompt, never
   * `window.prompt`, which does not exist in a WebView and wedges Electron. */
  async function netPop(anchor){
    const net = NET(); if(!net) return;
    const d = openPop(anchor, `<div class="os-pop-h">Network</div><div class="os-pop-b">Looking…</div>`);
    let list = [], status = null;
    try{ list = await net.wifi(true); }catch(_){ list = null; }
    try{ status = await net.status(); }catch(_){ status = null; }
    if(!_pop || _pop !== d) return;
    const body = d.querySelector('.os-pop-b');
    if(list === null){
      /* Could not ask is not "no networks". A wifi list that is empty because NetworkManager is not
       * running looks exactly like a room with no wifi in it. */
      body.innerHTML = `<div class="os-pop-none">The network could not be read on this machine.</div>`;
      return;
    }
    const here = (status && status.name) || '';
    body.innerHTML = (list.length
      ? list.map(n => `<button class="os-pop-row${n.ssid === here ? ' on' : ''}" data-ssid="${H(n.ssid)}"
             data-sec="${n.secure ? '1' : ''}">
           <span class="os-pop-nm">${H(n.ssid)}</span>
           <span class="os-pop-sig">${n.secure ? '🔒 ' : ''}${H(n.signal)}%</span></button>`).join('')
      : `<div class="os-pop-none">Nothing in range.</div>`);
    body.querySelectorAll('[data-ssid]').forEach(b => b.onclick = async () => {
      const ssid = b.dataset.ssid;
      let pw = '';
      if(b.dataset.sec && ssid !== here){
        closePop();
        try{ pw = await root.PC.uiPrompt('Password for ' + ssid, { password: true, ok: 'Join' }); }
        catch(_){ pw = null; }
        if(pw === null) return;
      }
      toast('joining ' + ssid + '…');
      try{
        const r = await net.connect(ssid, pw);
        toast(r && r.ok ? 'joined ' + ssid : (r && r.why) || 'could not join ' + ssid);
      }catch(e){ toast(String((e && e.message) || e)); }
      closePop();
      refresh();
    });
  }

  /* A SLIDER, and the reading it started from. Applied as it moves — a volume control you have to
   * confirm is one you cannot use to turn something down quickly. */
  function sliderPop(anchor, title, value, extra, onSet){
    const d = openPop(anchor, `<div class="os-pop-h">${H(title)}</div>
      <div class="os-pop-b"><input class="os-pop-range" type="range" min="0" max="100"
           value="${H(value)}" aria-label="${H(title)}"><span class="os-pop-val">${H(value)}%</span>
      </div>${extra || ''}`);
    const r = d.querySelector('.os-pop-range'), v = d.querySelector('.os-pop-val');
    let busy = false, want = null;
    const push = async () => {
      if(busy){ return; }
      busy = true;
      while(want !== null){
        const n = want; want = null;
        try{ await onSet(n); }catch(_){}
      }
      busy = false;
    };
    r.oninput = () => { v.textContent = r.value + '%'; want = Number(r.value); push(); };
    return d;
  }

  async function volPop(anchor){
    const a = AUDIO(); if(!a || !_sum || !_sum.volume.known) return;
    const muted = !!_sum.volume.muted;
    const d = sliderPop(anchor, 'Volume', _sum.volume.percent,
      `<div class="os-pop-f"><button class="os-pop-btn" data-mute="1">${muted ? 'Unmute' : 'Mute'}</button></div>`,
      (n) => a.setVolume(n, 'sink'));
    d.querySelector('[data-mute]').onclick = async () => {
      try{ await a.setMuted(!muted, 'sink'); }catch(_){}
      closePop(); await refresh();
    };
  }

  async function brightPop(anchor){
    const p = POWER(); if(!p || !_sum || !_sum.brightness.known) return;
    sliderPop(anchor, 'Brightness', _sum.brightness.percent, '', (n) => p.setBrightness(n));
  }

  /* TOR. One press to turn it on or off, and it says what that means — because it is the one
   * control here whose effect is invisible until something stops working. The exit country is
   * shown rather than offered: a full country picker belongs in Settings, and this is the tray. */
  async function torPop(anchor){
    const sh = root.pcShell;
    if(!sh || !sh.tor) return;
    let st = null;
    try{ st = await sh.tor.status(); }catch(_){ st = null; }
    if(!st){
      openPop(anchor, `<div class="os-pop-h">Tor</div>
        <div class="os-pop-none">Tor could not be read on this machine.</div>`);
      return;
    }
    const on = !!st.enabled;
    const where = st.countryName || (st.country ? String(st.country).toUpperCase() : 'anywhere');
    const d = openPop(anchor, `<div class="os-pop-h">Tor</div>
      <div class="os-pop-b">
        <div class="os-pop-none" id="os-tor-note">${on
          ? (st.bootstrapped >= 100
             ? H('On — everything this computer sends leaves through ' + where + '.')
             : H('Building a circuit… ' + (st.bootstrapped || 0) + '%'))
          : H('Off. Turning it on routes this app through the Tor network, exiting in '
              + where + '. It is slower and some sites refuse it.')}</div>
      </div>
      <div class="os-pop-b os-pop-acts">
        <button class="os-pop-row" data-tor="${on ? 'off' : 'on'}">${on ? 'Turn Tor off' : 'Turn Tor on'}</button>
        ${on ? `<button class="os-pop-row" data-tor="new">New circuit</button>` : ''}
      </div>`);
    const note = d.querySelector('#os-tor-note');
    d.querySelectorAll('[data-tor]').forEach(b => b.onclick = async () => {
      const act = b.dataset.tor;
      if(act === 'new'){
        try{ await sh.tor.newCircuit(); toast('new Tor circuit'); }
        catch(e){ toast(String((e && e.message) || e)); }
        closePop(); return;
      }
      /* NOT CLOSED FIRST. Turning Tor on reloads the page — the client's relay sockets have to be
       * re-opened through the new route — so this popover is about to be destroyed anyway, and the
       * useful thing in the seconds before that is the bootstrap number. */
      note.textContent = act === 'on' ? 'Starting Tor…' : 'Turning Tor off…';
      try{ sh.tor.onStatus((s2) => { if(s2 && note.isConnected && s2.enabled)
        note.textContent = s2.bootstrapped >= 100 ? 'Tor is up.'
                         : 'Building a circuit… ' + (s2.bootstrapped || 0) + '%'; }); }catch(_){}
      try{ await sh.tor.set({ enabled: act === 'on' }); }
      catch(e){ note.textContent = String((e && e.message) || e); return; }
      refresh();
    });
  }

  /* POWER. Suspend, hibernate, restart, shut down — and the profile, which is the one people
   * actually change day to day. Hibernate is offered only where it can work: a machine with no swap
   * cannot, and an entry that always fails is worse than one that is not there. */
  async function powerPop(anchor){
    const p = POWER(); if(!p) return;
    let st = null;
    try{ st = await p.status(); }catch(_){ st = null; }
    const pm = profileMenu(st);
    const profs = pm.list, cur = pm.active;
    const canHib = !!(st && st.canHibernate);
    const d = openPop(anchor, `<div class="os-pop-h">Power</div>
      ${profs.length ? `<div class="os-pop-b os-pop-profs">${profs.map(x =>
        `<button class="os-pop-btn${x === cur ? ' on' : ''}" data-prof="${H(x)}">${H(x)}</button>`).join('')}</div>` : ''}
      <div class="os-pop-b os-pop-acts">
        <button class="os-pop-row" data-act="suspend">Sleep</button>
        ${canHib ? `<button class="os-pop-row" data-act="hibernate">Hibernate</button>` : ''}
        <button class="os-pop-row" data-act="reboot">Restart</button>
        <button class="os-pop-row os-pop-danger" data-act="poweroff">Shut down</button>
      </div>`);
    d.querySelectorAll('[data-prof]').forEach(b => b.onclick = async () => {
      try{ await p.setProfile(b.dataset.prof); toast('power profile: ' + b.dataset.prof); }
      catch(e){ toast(String((e && e.message) || e)); }
      closePop(); refresh();
    });
    d.querySelectorAll('[data-act]').forEach(b => b.onclick = async () => {
      const act = b.dataset.act;
      /* ASKED FIRST, and only for the two that lose what is open. Sleep does not, so confirming it
       * is a dialog between somebody and closing their laptop. */
      if(act === 'reboot' || act === 'poweroff'){
        closePop();
        let ok = false;
        try{ ok = await root.PC.uiConfirm(act === 'reboot' ? 'Restart this computer?'
                                                           : 'Shut down this computer?',
                                          { ok: act === 'reboot' ? 'Restart' : 'Shut down', danger: true }); }
        catch(_){ ok = false; }
        if(!ok) return;
      }
      closePop();
      try{ await p[act](); }catch(e){ toast(String((e && e.message) || e)); }
    });
  }

  /** Wires whichever chips are present in `into`. Safe to call on markup that has none. */
  function bindPanel(into){
    if(!into) return;
    into.querySelectorAll('[data-os]').forEach(b => b.onclick = (e) => {
      e.stopPropagation();
      const kind = b.dataset.os;
      /* A second press on the SAME chip closes it. Without this the popover is dismissed by the
       * outside-press handler and reopened by the click, so the button appears not to work. */
      if(_pop && _pop.dataset.kind === kind){ closePop(); return; }
      closePop();
      const done = (d) => { if(d) d.dataset.kind = kind; };
      if(kind === 'net') netPop(b).then(() => done(_pop));
      else if(kind === 'vol') volPop(b).then(() => done(_pop));
      else if(kind === 'bright') brightPop(b).then(() => done(_pop));
      else if(kind === 'tor') torPop(b).then(() => done(_pop));
      else if(kind === 'power') powerPop(b).then(() => done(_pop));
      setTimeout(() => done(_pop), 0);
    });
  }

  function taskbarHTML(rows){
    if(!rows || !rows.length) return '';
    return `<div class="os-taskbar">` + rows.map(r =>
      `<button class="os-task${r.focused ? ' on' : ''}" data-win="${H(r.id)}" title="${H(r.title)}">`
      + `${H(r.label)}</button>`).join('') + `</div>`;
  }

  function launcherHTML(list){
    return `<div class="os-launcher">` + (list || APPS.filter(a => !a.hidden)).map(a =>
      `<button class="os-app" data-app="${H(a.id)}"${a.comment ? ` title="${H(a.comment)}"` : ''}>`
      + `${H(a.name)}</button>`).join('') + `</div>`;
  }

  /* One draw, from one read. Called on a window event and on a timer — the timer is the slow
   * backstop for the things that have no event at all (a battery draining, a wifi signal moving). */
  /* THE LAST READING, so that drawing is free. The desktop's taskbar is rebuilt on every window
   * focus and on the clock tick — several times a minute — and a panel that polls four subsystems
   * each time is four subprocesses a second on a laptop battery. The facts are refreshed on their
   * own schedule (a compositor event, or the slow timer); a draw only ever renders what was last
   * read, and says so when that is nothing. */
  let _sum = null, _rows = [], _readAt = 0;

  async function refresh(){
    let rows = [];
    try{ rows = taskbarRows(await WM().windows()); }catch(_){ rows = []; }
    let sum = null;
    try{ sum = panelSummary(await panelState()); }catch(_){ sum = null; }
    _rows = rows; _sum = sum; _readAt = Date.now();
    return { rows, sum };
  }

  /** The tray segment: what the desktop's own tray puts beside its clock. Sync — cache only. */
  function paintTray(into){
    if(!into) return;
    into.innerHTML = _sum ? panelHTML(_sum) : '';
    bindPanel(into);
  }

  function bindApps(into, after){
    into.querySelectorAll('[data-app]').forEach(b => b.onclick = async () => {
      b.disabled = true;
      try{ const r = await launch(b.dataset.app); if(r && r.why && root.PC) root.PC.toast(r.why); }
      catch(e){ if(root.PC) root.PC.toast(String((e && e.message) || e)); }
      finally{ b.disabled = false; if(after) after(); }
    });
  }

  async function render(into){
    if(!into || !(await detect())) return;
    await refresh();
    into.innerHTML = launcherHTML(await allApps()) + taskbarHTML(_rows) + (_sum ? panelHTML(_sum) : '');
    bindApps(into, () => render(into));
    into.querySelectorAll('[data-win]').forEach(b => b.onclick = async () => {
      try{ await WM().focus(Number(b.dataset.win)); }catch(_){}
      render(into);
    });
    bindPanel(into);
  }

  /* Redrawn when the compositor says something changed, not on a fast timer: a taskbar that lags a
   * window by a second is one people stop looking at, and a poll fast enough to hide that is a poll
   * running forever on a laptop battery. */
  /* `into` is either an element to draw into, or — the way the desktop uses it — a function to call
   * once the facts have been re-read, so the desktop can redraw its OWN taskbar with them. */
  async function watch(into){
    if(!(await detect())) return () => {};
    const tick = (typeof into === 'function')
      ? async () => { await refresh(); try{ into(); }catch(_){} }
      : () => render(into);
    await tick();
    let off = () => {};
    try{
      await WM().subscribe();
      off = WM().onEvent(tick);
    }catch(_){}
    const t = setInterval(tick, 30000);                // battery and signal have no events
    return () => { try{ off(); }catch(_){} clearInterval(t); };
  }

  const API = { available, detect, APPS, taskbarRows, existingWindow, launch, panelState, panelSummary,
                profileMenu, machineApps, mergedApps, allApps,
                ensureAccount, panelHTML, taskbarHTML, launcherHTML, render, watch,
                setViewOpener, refresh, paintTray, bindApps, bindPanel,
                summary: () => _sum, rows: () => _rows, readAt: () => _readAt };
  root.PCOSShell = API;
  if(typeof module !== 'undefined' && module.exports) module.exports = API;
})(typeof globalThis !== 'undefined' ? globalThis : this);
